"""IO layer for OMP live-process/session identity — stdlib-only, fail-closed.

Companion to :mod:`riglib.omp_recovery` (the pure decision logic): everything here talks to
the real OS (``ps``, ``lsof``, ``abduco -l``, ``tmux``), so nothing here is unit-tested by
asserting live-system behavior — callers inject a ``ps``-format snapshot string / a rows list
and get back the same :class:`~riglib.omp_recovery.OriginalSession` objects the pure module
consumes. Every subprocess call here uses ``stdin=subprocess.DEVNULL`` (never inherits a TTY)
and degrades to "inspection failed" rather than raising, so a caller can fail closed exactly
the way :func:`riglib.omp_recovery.plan_restore`/``plan_server_recovery`` expect.

Durable-session identity: NOT read back via a live process's environment. Verified on this
machine that macOS ``ps`` no longer reliably exposes another process's environment even to its
own owning user (``ps -wwE -p <pid>``/``ps -eo command=`` with env both come back empty for a
real child with a marker env var set — a hardened-runtime/SIP restriction, not a flag mistake).
Instead: ``rig harness run``/``attach`` always launch the real OMP process as a descendant of an
``abduco -A <durable-id> …``/``abduco -a <durable-id>`` process (verified: abduco forks a
DETACHED server, reparented to pid 1, whose own argv is the identical ``abduco -A <name>
rig harness host <name>`` line; the harness is that server's child's child, below the crash
supervisor). So a live OMP process's durable session is recovered by walking UP its ppid chain
for an ancestor whose argv is an abduco invocation and reading the name out of THAT — no env
access needed. The hosted session's ``RIG_SESSION`` env is a first-party self-report for
``tg-ctl omp-live status`` only; this module never reads it back.

Session IDs are OMP's real header IDs, never durable socket names. The bridge is
preferred, but a successful empty bridge list cannot hide an uninstrumented live OMP.
Legacy identities come only from that process's own TTY breadcrumb or open transcript.
Unknown identities remain visible and block unsafe restoration; no latest-by-cwd guessing.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from shutil import which

from .harness_host import parse_host_command
from .omp_recovery import OriginalSession, is_interactive_omp

_ABDUCO_CONNECT_FLAGS = {"-A", "-a", "-c", "-n"}
# flags that consume a following argument, per `man abduco`.
_ABDUCO_ARG_FLAGS = {"-e"}


@dataclass(frozen=True)
class PsRow:
    pid: int
    ppid: int
    args: str


def parse_ps_rows(snapshot: str) -> list[PsRow]:
    """Parse ``ps -eo pid=,ppid=,args=`` output. Malformed lines are skipped, never raised."""
    rows: list[PsRow] = []
    for line in snapshot.splitlines():
        line = line.strip("\n")
        if not line.strip():
            continue
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        args = parts[2] if len(parts) > 2 else ""
        rows.append(PsRow(pid, ppid, args))
    return rows


def capture_ps_snapshot(timeout: float = 5.0) -> str | None:
    """One ``ps -eo pid=,ppid=,args=`` snapshot, or ``None`` on any failure (fail closed)."""
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,args="],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def parse_abduco_argv(args: str) -> tuple[str, str] | None:
    """``(session_name, remainder_args)`` if ``args`` is an ``abduco`` invocation naming a
    session, else ``None``. Handles the ``-e detachkey``/``-r``/``-f``/``-l`` modifiers ahead
    of the ``-A|-a|-c|-n name`` pair (see ``man abduco`` SYNOPSIS)."""
    try:
        tokens = shlex.split(args)
    except ValueError:
        return None
    if not tokens:
        return None
    if PurePosixPath(tokens[0]).name != "abduco":
        return None
    i = 1
    name = None
    while i < len(tokens):
        tok = tokens[i]
        if tok in _ABDUCO_ARG_FLAGS:
            i += 2
            continue
        if tok in _ABDUCO_CONNECT_FLAGS:
            if i + 1 >= len(tokens):
                return None
            name = tokens[i + 1]
            i += 2
            break
        if tok.startswith("-"):
            i += 1
            continue
        return None
    if name is None:
        return None
    remainder = shlex.join(tokens[i:]) if i < len(tokens) else ""
    return name, remainder


def hosted_harness(remainder: str) -> str | None:
    """The harness kind when ``remainder`` (what follows ``abduco -A <name>``) is a
    ``rig harness host <name>`` command, else ``None``."""
    try:
        parsed = parse_host_command(shlex.split(remainder))
    except ValueError:
        return None
    return parsed[1] if parsed else None


def find_omp_descendant(root_pid: int, rows: list[PsRow]) -> PsRow | None:
    """BFS a pane's process tree for the first descendant (or the root itself) whose full
    command line is an interactive OMP launch, OR an ``abduco`` client wrapping one (a durably
    launched pane's client argv is ``abduco -A <id> rig harness host <id>`` — or, for a session
    started before that launcher existed, ``abduco -A <id> omp …`` — after ``rig harness
    run``/``attach`` exec'd into it). Mirrors the bash ``pane_has_omp`` tree-walk that
    ``render_omp_save`` renders, but exercised directly by unit tests here."""
    by_ppid: dict[int, list[PsRow]] = {}
    by_pid: dict[int, PsRow] = {}
    for row in rows:
        by_pid[row.pid] = row
        by_ppid.setdefault(row.ppid, []).append(row)
    queue = [root_pid]
    seen: set[int] = set()
    while queue:
        cur = queue.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        row = by_pid.get(cur)
        if row is not None:
            if is_interactive_omp(row.args):
                return row
            parsed = parse_abduco_argv(row.args)
            if parsed is not None:
                _name, remainder = parsed
                if not remainder or is_interactive_omp(remainder) or hosted_harness(remainder) == "omp":
                    return row
        for child in by_ppid.get(cur, ()):
            queue.append(child.pid)
    return None


def pane_durable_session(match: PsRow) -> str | None:
    """The durable session id for a :func:`find_omp_descendant` match, or ``None`` for a bare
    (unwrapped) omp/bun match."""
    parsed = parse_abduco_argv(match.args)
    return parsed[0] if parsed else None


def find_all_interactive_omp(rows: list[PsRow]) -> list[PsRow]:
    """Every process, system-wide, whose own command line is an interactive OMP launch —
    NOT scoped to tmux panes, so an orphan started outside any pane (the 2026-09-25 incident)
    is still found. Deliberately excludes abduco/wrapper rows themselves: the real omp/bun
    process is always a SEPARATE descendant row (see module docstring), found directly by its
    own argv."""
    return [row for row in rows if is_interactive_omp(row.args)]


def find_ancestor_durable_session(
    pid: int, rows: list[PsRow], *, max_hops: int = 32
) -> str | None:
    """Walk UP a process's ppid chain for the nearest ``abduco`` ancestor and return its
    session name, or ``None`` if the process is not durably wrapped. Bounded (``max_hops``)
    against a corrupt/cyclic ``ps`` snapshot."""
    by_pid = {row.pid: row for row in rows}
    cur = pid
    for _ in range(max_hops):
        row = by_pid.get(cur)
        if row is None:
            return None
        parsed = parse_abduco_argv(row.args)
        if parsed is not None:
            return parsed[0]
        if row.ppid == cur or row.ppid <= 1:
            return None
        cur = row.ppid
    return None


def process_cwd(pid: int, timeout: float = 5.0) -> str | None:
    """A process's cwd via ``lsof -a -p <pid> -d cwd -Fn`` (portable on macOS/BSD; no
    ``/proc/<pid>/cwd`` there). ``None`` on any failure, including a missing ``lsof`` binary —
    callers MUST treat that as inspection failure, never as "no cwd" (never a duplicate-launch
    green light)."""
    try:
        proc = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        if line.startswith("n"):
            return line[1:]
    return None


def lsof_available() -> bool:
    return which("lsof") is not None


def abduco_socket_dir() -> Path:
    """Where rig points ``ABDUCO_SOCKET_DIR`` for every abduco invocation it makes. abduco
    itself then uses ``<this>/abduco/<uid-subdir>`` (0700, per-user) for the actual sockets —
    see ``man abduco``. Overridable via ``RIG_OMP_ABDUCO_DIR`` (tests use this for isolation;
    never touch a real ``~/.abduco``)."""
    override = os.environ.get("RIG_OMP_ABDUCO_DIR")
    if override:
        return Path(override)
    return Path.home() / ".local" / "state" / "rig" / "abduco"


def abduco_env() -> dict[str, str]:
    env = dict(os.environ)
    env["ABDUCO_SOCKET_DIR"] = str(abduco_socket_dir())
    return env


def list_abduco_sessions(
    abduco_bin: str = "abduco", timeout: float = 5.0
) -> tuple[set[str], bool]:
    """``(names, inspection_ok)``. A missing/failing ``abduco`` is inspection failure, not
    "no sessions" — callers must not treat it as proof a durable session is gone."""
    try:
        proc = subprocess.run(
            [abduco_bin, "-l"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=abduco_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return set(), False
    if proc.returncode != 0:
        return set(), False
    return {
        s["name"] for s in parse_abduco_listing(proc.stdout) if not s["terminated"]
    }, True


def parse_abduco_listing(text: str) -> list[dict]:
    """Parse abduco's shared CLI format; + means the worker has terminated."""
    rows = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or not parts[-1].strip():
            continue
        rows.append(
            {
                "name": parts[-1].strip(),
                "attached": line.startswith("*"),
                "terminated": line.startswith("+"),
                "created": parts[-2].strip(),
            }
        )
    return rows


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def session_header(session_file: str) -> dict | None:
    import json

    path = Path(session_file)
    if not path.is_absolute():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            for _ in range(8):
                line = f.readline(131073)
                if not line or len(line) > 131072:
                    break
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict) and value.get("type") == "session":
                    if isinstance(value.get("id"), str) and value["id"]:
                        return value
    except (OSError, UnicodeError):
        pass
    return None


def process_session_file(pid: int, cwd: str) -> str | None:
    """Exact per-terminal breadcrumb, then a single actually-open main transcript."""
    import re

    agent_root = Path(
        os.environ.get("PI_CODING_AGENT_DIR") or (Path.home() / ".omp/agent")
    )
    candidates: list[str] = []
    try:
        tty = subprocess.run(
            ["ps", "-p", str(pid), "-o", "tty="],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout.strip()
        if re.fullmatch(r"[A-Za-z0-9_.-]+", tty) and tty not in {"?", "??"}:
            record = agent_root / "terminal-sessions" / tty
            lines = record.read_text(encoding="utf-8").splitlines()
            if len(lines) >= 2 and Path(lines[0]).resolve() == Path(cwd).resolve():
                candidates.append(lines[1])
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        pass
    for path in candidates:
        header = session_header(path)
        if (
            header
            and header.get("cwd")
            and Path(header["cwd"]).resolve() == Path(cwd).resolve()
        ):
            return path
    try:
        result = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-Fn"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    files = set()
    for line in result.stdout.splitlines():
        if not line.startswith("n"):
            continue
        path = line[1:]
        if not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T[^/]+_[0-9a-f-]+\.jsonl", Path(path).name
        ):
            continue
        header = session_header(path)
        if (
            header
            and header.get("cwd")
            and Path(header["cwd"]).resolve() == Path(cwd).resolve()
        ):
            files.add(path)
    return next(iter(files)) if len(files) == 1 else None


def local_live_omp_sessions(rows: list[PsRow]) -> tuple[list[OriginalSession], bool]:
    sessions = []
    candidates = find_all_interactive_omp(rows)
    if candidates and not lsof_available():
        return [], False
    for row in candidates:
        cwd = process_cwd(row.pid)
        if cwd is None:
            if _pid_alive(row.pid):
                return sessions, False
            continue
        file = process_session_file(row.pid, cwd)
        header = session_header(file) if file else None
        sessions.append(
            OriginalSession(
                row.pid,
                f"legacy-{row.pid}",
                cwd,
                header["id"] if header else None,
                find_ancestor_durable_session(row.pid, rows),
                file,
            )
        )
    return sessions, True


def query_tg_ctl_omp_live(
    tg_ctl_bin: str = "tg-ctl", timeout: float = 5.0
) -> tuple[list[OriginalSession], bool]:
    """Read verified original endpoints; retain unknown legacy processes separately."""
    import json

    try:
        proc = subprocess.run(
            [tg_ctl_bin, "omp-live", "status", "--json"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode:
            return [], False
        data = json.loads(proc.stdout)
        if not isinstance(data, list):
            return [], False
        result = []
        seen = set()
        for item in data:
            if not isinstance(item, dict):
                return [], False
            pid = item["pid"]
            cwd = item["cwd"]
            sid = item["sessionId"]
            instance = item["instanceId"]
            if (
                type(pid) is not int
                or pid <= 0
                or pid in seen
                or not all(isinstance(x, str) and x for x in [cwd, sid, instance])
            ):
                return [], False
            file = item.get("sessionFile")
            durable = item.get("durableSession")
            if not Path(cwd).is_absolute() or (
                file is not None
                and (not isinstance(file, str) or not Path(file).is_absolute())
            ):
                return [], False
            if durable is not None and not isinstance(durable, str):
                return [], False
            result.append(OriginalSession(pid, instance, cwd, sid, durable, file))
            seen.add(pid)
        return result, True
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, KeyError):
        return [], False


def live_omp_sessions(tg_ctl_bin: str = "tg-ctl") -> tuple[list[OriginalSession], bool]:
    bridge, bridge_ok = query_tg_ctl_omp_live(tg_ctl_bin)
    snapshot = capture_ps_snapshot()
    if snapshot is None:
        return bridge if bridge_ok else [], False
    rows = parse_ps_rows(snapshot)
    known = {s.pid for s in bridge} if bridge_ok else set()
    legacy, local_ok = local_live_omp_sessions([r for r in rows if r.pid not in known])
    live_pids = {r.pid for r in rows}
    return [s for s in bridge if s.pid in live_pids] + legacy, local_ok


def saved_session_id_for(session_file: str) -> str:
    header = session_header(session_file)
    return header["id"] if header else ""
