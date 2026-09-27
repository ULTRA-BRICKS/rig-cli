"""Bounded, identity-checked tmux recovery. Never infer health from a socket file alone.

A healthy protocol response pins PID/start/uid/executable. A lost socket on that same
server is repaired with SIGUSR1; a positively dead server uses the configured boot
script. Unknown inspection results never authorize signalling or another server.
"""

from __future__ import annotations
import fcntl
import json
import os
import signal
import stat
import subprocess
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from .omp_recovery import ServerIdentity, ServerPlan, plan_server_recovery

DEFAULT_COOLDOWN_SECONDS = 60
FORENSIC_LOG_MAX_BYTES = 1_000_000
_HELD_LOCKS: dict[str, int] = {}


class InspectionUnavailable(RuntimeError):
    pass


class WatchdogBusy(RuntimeError):
    pass


def state_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".config/rig/tmux"


def identity_path(home: Path | None = None) -> Path:
    return state_dir(home) / "tmux-server-identity.json"


def cooldown_path(home: Path | None = None) -> Path:
    return state_dir(home) / "tmux-watchdog-cooldown.json"


def lock_path(home: Path | None = None) -> Path:
    return state_dir(home) / "tmux-watchdog.lock"


def forensic_log_path(home: Path | None = None) -> Path:
    return state_dir(home) / "tmux-watchdog-forensics.log"


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _read_json(path: Path) -> dict | None:
    try:
        st = path.lstat()
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_uid != os.getuid()
            or st.st_size > 16384
        ):
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, UnicodeError):
        return None


def valid_identity(identity: ServerIdentity) -> bool:
    return (
        type(identity.pid) is int
        and identity.pid > 1
        and identity.uid == os.getuid()
        and Path(identity.executable).name in {"tmux", "tmux: server"}
        and bool(identity.started_at)
        and Path(identity.socket_path).is_absolute()
    )


def _read_identity_for_pid(
    pid: int, socket_path: str, *, timeout: float = 5.0
) -> ServerIdentity | None:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "uid=,lstart=,comm="],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise InspectionUnavailable("process inspection unavailable") from e
    if result.returncode == 1 and not result.stdout.strip():
        return None
    if result.returncode != 0:
        raise InspectionUnavailable("process inspection failed")
    parts = result.stdout.strip().split(None, 6)
    if len(parts) != 7:
        raise InspectionUnavailable("malformed process identity")
    try:
        return ServerIdentity(
            pid,
            int(parts[0]),
            parts[6],
            " ".join(parts[1:6]),
            str(Path(socket_path).resolve()),
        )
    except ValueError as e:
        raise InspectionUnavailable("malformed uid") from e


def probe_saved_identity(
    saved: ServerIdentity, timeout: float = 5.0
) -> ServerIdentity | None:
    return _read_identity_for_pid(saved.pid, saved.socket_path, timeout=timeout)


def query_server(
    socket_path: Path, tmux_bin: str = "tmux", timeout: float = 4.0
) -> ServerIdentity | None:
    if socket_path.is_symlink():
        raise InspectionUnavailable("symlink socket refused")
    try:
        result = subprocess.run(
            [tmux_bin, "-S", str(socket_path), "display-message", "-p", "#{pid}"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise InspectionUnavailable("tmux protocol probe unavailable") from e
    if result.returncode:
        return None
    try:
        pid = int(result.stdout.strip())
    except ValueError as e:
        raise InspectionUnavailable("invalid server pid") from e
    identity = _read_identity_for_pid(pid, str(socket_path), timeout=timeout)
    if identity is None or not valid_identity(identity):
        raise InspectionUnavailable("unverified tmux server identity")
    return identity


def save_identity(identity: ServerIdentity, home: Path | None = None) -> None:
    _atomic_json(identity_path(home), asdict(identity))


def load_identity(home: Path | None = None) -> ServerIdentity | None:
    data = _read_json(identity_path(home))
    try:
        return ServerIdentity(**data) if data else None
    except TypeError:
        return None


def cooldown_active(
    home: Path | None = None,
    *,
    now: float | None = None,
    seconds: int = DEFAULT_COOLDOWN_SECONDS,
) -> bool:
    data = _read_json(cooldown_path(home)) or {}
    last = data.get("at")
    if not isinstance(last, (int, float)):
        return False
    age = (time.time() if now is None else now) - last
    return 0 <= age < seconds


def record_cooldown(home: Path | None = None, *, now: float | None = None) -> None:
    _atomic_json(cooldown_path(home), {"at": time.time() if now is None else now})


def acquire_lock(home: Path | None = None, *, now: float | None = None) -> Path:
    path = lock_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path)
    if key in _HELD_LOCKS:
        raise WatchdogBusy("watchdog already executing")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if os.fstat(fd).st_uid != os.getuid():
            raise WatchdogBusy("foreign watchdog lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
    except Exception:
        os.close(fd)
        raise WatchdogBusy("watchdog lock held or unsafe")
    _HELD_LOCKS[key] = fd
    return path


def release_lock(home: Path | None = None) -> None:
    fd = _HELD_LOCKS.pop(str(lock_path(home)), None)
    if fd is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    # Never unlink a locked inode: another process may already be waiting on it.


def append_forensic_log(message: str, home: Path | None = None) -> None:
    path = forensic_log_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        return
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {message[:3000]}\n"
    try:
        if path.exists() and path.stat().st_size > FORENSIC_LOG_MAX_BYTES:
            with path.open("rb") as f:
                f.seek(-FORENSIC_LOG_MAX_BYTES // 2, 2)
                tail = f.read()
            path.write_bytes(tail)
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
        path.chmod(0o600)
    except OSError:
        pass


def preserve_crash_reports(home: Path | None = None) -> None:
    home = home or Path.home()
    source = home / "Library/Logs/DiagnosticReports"
    dest = state_dir(home) / "crash-reports"
    if not source.is_dir():
        return
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        for file in sorted(
            source.glob("tmux-*.ips"), key=lambda p: p.stat().st_mtime, reverse=True
        )[:8]:
            if (
                file.is_symlink()
                or not file.is_file()
                or file.stat().st_size > 4_000_000
            ):
                continue
            target = dest / file.name
            if not target.exists():
                target.write_bytes(file.read_bytes())
                target.chmod(0o600)
        for stale in sorted(
            dest.glob("tmux-*.ips"), key=lambda p: p.stat().st_mtime, reverse=True
        )[8:]:
            stale.unlink()
    except OSError:
        pass


def recover_socket(pid: int) -> bool:
    try:
        os.kill(pid, signal.SIGUSR1)
        return True
    except OSError:
        return False


def run_once(
    socket_path: Path,
    *,
    home: Path | None = None,
    lsof_bin: str = "lsof",
    paused: bool = False,
    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
    boot_script: Path | None = None,
    tmux_bin: str = "tmux",
) -> ServerPlan:
    try:
        acquire_lock(home)
    except (WatchdogBusy, OSError):
        return ServerPlan("none", "watchdog_busy")
    try:
        preserve_crash_reports(home)
        if paused or (state_dir(home) / "recovery-paused").exists():
            return ServerPlan("none", "recovery_paused")
        try:
            healthy = query_server(socket_path, tmux_bin)
            if healthy is not None:
                save_identity(healthy, home)
                return ServerPlan("none", "server_healthy")
            saved = load_identity(home)
            if saved is None:
                return ServerPlan("blocked", "server_identity_not_recorded")
            if (
                not valid_identity(saved)
                or Path(saved.socket_path).resolve() != socket_path.resolve()
            ):
                return ServerPlan("blocked", "invalid_saved_server_identity")
            if cooldown_active(home, seconds=cooldown_seconds):
                return ServerPlan("none", "cooldown")
            current = probe_saved_identity(saved)
            plan = plan_server_recovery(
                saved,
                current,
                socket_responding=False,
                inspection_ok=True,
                paused=False,
                cooldown_active=False,
            )
        except InspectionUnavailable:
            return ServerPlan("blocked", "inspection_unavailable")
        if plan.action == "restore_socket":
            # Recheck immediately before signalling, including process birth time.
            try:
                if probe_saved_identity(saved) != saved:
                    return ServerPlan("blocked", "server_identity_changed")
            except InspectionUnavailable:
                return ServerPlan("blocked", "inspection_unavailable")
            record_cooldown(home)
            if not recover_socket(saved.pid):
                return ServerPlan("blocked", "socket_recovery_signal_failed")
            for _ in range(20):
                time.sleep(0.05)
                try:
                    if query_server(socket_path, tmux_bin) == saved:
                        append_forensic_log(
                            f"restored same server pid={saved.pid}", home
                        )
                        return plan
                except InspectionUnavailable:
                    break
            return ServerPlan("blocked", "socket_recovery_not_confirmed")
        if plan.action == "start":
            if boot_script is None or not boot_script.is_file():
                return ServerPlan("blocked", "boot_script_unavailable")
            record_cooldown(home)
            env = {k: v for k, v in os.environ.items() if k != "TMUX"}
            env["RIG_TMUX_RECOVERY_SOCKET"] = str(socket_path)
            try:
                result = subprocess.run(
                    ["/bin/bash", str(boot_script)],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    env=env,
                )
                if result.returncode:
                    return ServerPlan("blocked", "boot_failed")
                fresh = query_server(socket_path, tmux_bin)
            except (OSError, subprocess.TimeoutExpired, InspectionUnavailable):
                fresh = None
            if fresh is None:
                return ServerPlan("blocked", "boot_not_confirmed")
            save_identity(fresh, home)
            append_forensic_log(f"restarted absent server pid={fresh.pid}", home)
        if plan.action != "none":
            append_forensic_log(f"{plan.action}: {plan.reason}", home)
        return plan
    finally:
        release_lock(home)
