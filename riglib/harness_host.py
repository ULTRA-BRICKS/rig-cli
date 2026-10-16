"""The harness host — ``rig harness host <name>`` — the crash supervisor INSIDE an abduco session.

``rig harness run <omp|claude|codex|opencode>`` makes abduco run this instead of the harness. It
starts the harness on the abduco pty, registers the session for tg-ctl, and when the harness dies
unexpectedly relaunches it in the SAME abduco session and terminal, resuming the SAME conversation
(:func:`riglib.harness_kinds.relaunch_argv`). It never duplicates a writer: the relaunch happens
only after the dead process was reaped.

Why it exists (2026-09-30): a long omp coordinator grew to ~90 GiB, macOS memory-pressure jetsam
SIGKILLed it, and abduco reported ``session terminated with exit status 0`` because it applies
``WEXITSTATUS`` to a signal-killed status (reproduced on abduco 0.6 for SIGKILL and SIGTERM alike).
The agent stayed dead until a human noticed. Only the PARENT of the harness ever sees the real
status, so classification lives here — never in abduco's message and never in a launcher outside
the session.

What is "unexpected" (:func:`classify_exit`): a non-zero exit, or death by any signal except the
three stop requests. Exit 0 (``/exit``, Ctrl-D), the conventional 129/130/143 and SIGHUP/SIGINT/
SIGTERM are requests to stop and are honored, as is a SIGTERM/SIGHUP to the host itself
(``abduco`` teardown, ``kill``): it is forwarded to the harness and ends supervision.

Crash loops are bounded (:class:`RestartPolicy`): exponential backoff for quick crashes and a cap
on restarts inside a sliding window, after which the host gives up with the REAL status
(``128 + signal``) so abduco's final message stops lying. Each decision goes to the terminal and
to ``<abduco dir>/supervisor.log``.

Contract with tg-ctl (docs/specs/2026-09-30-harness-agnostic-control.md in tg-cli): the host
writes ``${TG_CTL_CONFIG_DIR:-~/.config/tg-cli}/hosts/<name>.json`` when supervision starts,
refreshes it on every relaunch and deletes it when supervision ends. The harness gets
``RIG_SESSION=<name>`` (membership, not role) and NO coordinator marker; subagent markers inherited
from the launching shell are scrubbed (local role model §5).

The harness command line travels in ``RIG_HOST_SPEC`` (removed before the harness starts), never
in argv: tg-ctl recognises agents by argv, and an argv naming the harness would make the abduco
server or this host look like the agent.

Stdlib-only; the rest of rig is imported lazily.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, TextIO

from . import signal_scope
from .harness_kinds import KINDS, SessionRef, relaunch_argv, validate_kind
from .harness_sessions import Tracker, terminal_id

ENV_SPEC = "RIG_HOST_SPEC"
ENV_SESSION = "RIG_SESSION"
#: subagent markers a launching shell may carry (role model §5), plus the superseded
#: ``RIG_OMP_SESSION`` and our own spec.
SCRUBBED_ENV = ("RIG_AGENT_ID", "RIG_DETACHED_AGENT", "PI_SESSION_FILE", "TG_AGENT", "RIG_OMP_SESSION", ENV_SPEC)

#: omp's own exit codes for SIGHUP/SIGINT/SIGTERM (oh-my-pi ``postmortem.ts``: 128 + signal).
_REQUESTED_STOP_CODES = frozenset({129, 130, 143})
_REQUESTED_STOP_SIGNALS = frozenset({signal.SIGHUP, signal.SIGINT, signal.SIGTERM})

_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,60}")


def validate_name(value: str) -> str:
    if not _NAME_RE.fullmatch(value):
        raise ValueError("invalid harness host name")
    return value


# ── exit classification ──────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ExitVerdict:
    crashed: bool
    description: str
    #: the status to report when supervision ends here: ``128 + signal`` for a signal death.
    exit_code: int


def classify_exit(returncode: int) -> ExitVerdict:
    """``returncode`` as :attr:`subprocess.Popen.returncode` reports it (negative = signal)."""
    if returncode < 0:
        sig = -returncode
        try:
            name = signal.Signals(sig).name
        except ValueError:
            name = f"signal {sig}"
        return ExitVerdict(sig not in _REQUESTED_STOP_SIGNALS, f"killed by {name}", 128 + sig)
    crashed = returncode != 0 and returncode not in _REQUESTED_STOP_CODES
    return ExitVerdict(crashed, f"exited with status {returncode}", returncode)


# ── restart policy ───────────────────────────────────────────────────────────────────────────
@dataclass
class RestartPolicy:
    """At most ``max_restarts`` crashes inside ``window`` seconds; the delay doubles from
    ``base_delay`` (capped at ``max_delay``) while runs keep dying before ``stable_after``."""

    max_restarts: int = 5
    window: float = 600.0
    base_delay: float = 1.0
    max_delay: float = 30.0
    stable_after: float = 60.0
    _crashes: list[float] = field(default_factory=list, init=False, repr=False)
    _streak: int = field(default=0, init=False, repr=False)

    def next_delay(self, now: float, uptime: float) -> float | None:
        """Record a crash at ``now`` of a run that lived ``uptime`` seconds; the delay before
        the relaunch, or ``None`` when the crash-loop cap is exceeded."""
        self._crashes = [t for t in self._crashes if now - t < self.window] + [now]
        if len(self._crashes) > self.max_restarts:
            return None
        self._streak = self._streak + 1 if uptime < self.stable_after else 1
        return min(self.base_delay * 2 ** (self._streak - 1), self.max_delay)


# ── what to run ──────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class HostSpec:
    harness: str
    argv: list[str]
    cwd: str
    label: str | None = None
    #: the conversation known at launch (Claude Code's assigned id).
    session: SessionRef | None = None
    #: start the harness inside its own Landlock signal domain (#704); decided once, at launch, so
    #: every crash relaunch is contained the same way.
    signal_scope: bool = False

    def to_env(self) -> str:
        body: dict = {"v": 1, "harness": self.harness, "argv": self.argv, "cwd": self.cwd}
        if self.label:
            body["label"] = self.label
        if self.session:
            body["session"] = self.session.to_json()
        if self.signal_scope:
            body["signal_scope"] = True
        return json.dumps(body, separators=(",", ":"))

    @classmethod
    def from_env(cls, text: str | None) -> "HostSpec":
        try:
            data = json.loads(text or "")
            harness, argv, cwd = data["harness"], data["argv"], data["cwd"]
            if data.get("v") != 1 or not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
                raise ValueError("bad argv")
            validate_kind(harness)
            session = data.get("session")
            ref = SessionRef(**{k: session[k] for k in ("id", "file", "cwd") if k in session}) if session else None
            return cls(harness, argv, str(cwd), data.get("label"), ref, data.get("signal_scope") is True)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{ENV_SPEC} is missing or invalid: {exc}") from exc


def child_argv(spec: HostSpec, command: list[str]) -> list[str]:
    """What the host spawns for ``command``: behind the signal-scope helper when the spec asks for
    it. ``command`` stays the bare harness argv, so relaunch/resume logic never sees the prefix;
    the helper execs in place, so ``child.pid`` is still the harness's pid."""
    return signal_scope.scoped_argv(command) if spec.signal_scope else command


# ── the command abduco runs ──────────────────────────────────────────────────────────────────
def rig_entry() -> list[str]:
    """This interpreter + this rig checkout (or installed package), so the host that runs is the
    code that launched it — never whatever ``rig`` a smaller PATH happens to find."""
    shim = Path(__file__).resolve().parents[1] / "bin" / "rig"
    return [sys.executable, str(shim)] if shim.is_file() else [sys.executable, "-m", "riglib"]


def host_command(name: str) -> list[str]:
    return [*rig_entry(), "harness", "host", validate_name(name)]


def parse_host_command(tokens: list[str]) -> tuple[str, str] | None:
    """``(host name, harness kind)`` if ``tokens`` is a :func:`host_command`, else ``None``. The
    kind is the name's prefix (``omp-6a83…``), so recognising a pane needs no IO."""
    for i in range(len(tokens) - 2):
        if tokens[i : i + 2] != ["harness", "host"] or len(tokens) != i + 3:
            continue
        entry = tokens[1:i]
        if entry == ["-m", "riglib"] or (len(entry) == 1 and PurePosixPath(entry[0]).name == "rig"):
            name = tokens[i + 2]
            kind = name.split("-", 1)[0]
            return (name, kind) if kind in KINDS else None
    return None


def host_env(base: dict[str, str], name: str) -> dict[str, str]:
    """The harness environment: membership (``RIG_SESSION``) only, subagent markers scrubbed.
    Deliberately no positive coordinator marker — it would leak into the harness's own
    children (``claude -p``, ``omp -p``) and label them coordinators."""
    env = {k: v for k, v in base.items() if k not in SCRUBBED_ENV}
    env[ENV_SESSION] = name
    return env


# ── the tg-ctl registration record ───────────────────────────────────────────────────────────
def hosts_dir(env: dict[str, str]) -> Path:
    root = Path(env["TG_CTL_CONFIG_DIR"]) if env.get("TG_CTL_CONFIG_DIR") else Path.home() / ".config" / "tg-cli"
    return root / "hosts"


def find_socket(env: dict[str, str], name: str) -> Path | None:
    """Absolute path of the abduco socket for ``name`` (``<dir>/abduco/<user>/<name>@<host>``)."""
    root = env.get("ABDUCO_SOCKET_DIR")
    if not root:
        return None
    for path in sorted(Path(root).glob(f"abduco/*/{name}@*")):
        if path.is_socket():
            return path.absolute()
    return None


def parent_is_abduco(pid: int) -> bool:
    """Whether ``pid`` is an abduco server (the record's ``serverPid``)."""
    try:
        out = subprocess.run(
            ["ps", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return os.path.basename(out.strip()) == "abduco"


class Registry:
    """Writes/refreshes/deletes ``hosts/<name>.json``; tg-ctl only reads it."""

    def __init__(self, directory: Path, name: str, spec: HostSpec, socket: Path, server_pid: int) -> None:
        self.path = directory / f"{name}.json"
        self._base = {
            "v": 1,
            "name": name,
            "harness": spec.harness,
            "cwd": spec.cwd,
            "socket": str(socket),
            "serverPid": server_pid,
            "supervisorPid": os.getpid(),
            "role": "coordinator",
            "startedAt": _now(),
        }
        if spec.label:
            self._base["label"] = spec.label

    @classmethod
    def open(
        cls,
        name: str,
        spec: HostSpec,
        env: dict[str, str],
        *,
        server_pid: int | None = None,
        is_abduco: Callable[[int], bool] = parent_is_abduco,
    ) -> "Registry | None":
        """``None`` when this host is not inside an abduco session we can name (a hand-run
        ``rig harness host``): there is nothing tg-ctl could connect to."""
        pid = server_pid if server_pid is not None else os.getppid()
        socket = find_socket(env, name)
        if socket is None or not is_abduco(pid):
            return None
        return cls(hosts_dir(env), name, spec, socket, pid)

    def write(self, *, harness_pid: int | None, session: SessionRef | None, restarts: int) -> None:
        body = {**self._base, "updatedAt": _now(), "restarts": restarts}
        if harness_pid:
            body["harnessPid"] = harness_pid
        if session and session.to_json():
            body["session"] = session.to_json()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(body, handle, indent=2)
                handle.write("\n")
            os.replace(tmp, self.path)
        finally:
            tmp.unlink(missing_ok=True)

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── the loop ─────────────────────────────────────────────────────────────────────────────────
def _duration(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m{secs:02d}s"


class _Reporter:
    def __init__(self, name: str, log_path: Path | None, out: TextIO) -> None:
        self.name = name
        self.log_path = log_path
        self.out = out

    def __call__(self, message: str) -> None:
        # The pty may still be in the dead TUI's raw mode: carriage-return explicitly.
        try:
            self.out.write(f"\r\n[rig harness host] {message}\r\n")
            self.out.flush()
        except (OSError, ValueError):
            pass
        if self.log_path is None:
            return
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        try:
            self.log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as log:
                log.write(f"{stamp} {self.name} {message}\n")
        except OSError:
            pass


def supervise(
    spec: HostSpec,
    *,
    name: str,
    env: dict[str, str],
    registry: Registry | None = None,
    policy: RestartPolicy | None = None,
    log_path: Path | None = None,
    terminal: str | None = None,
    home: Path | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval: float = 5.0,
    out: TextIO | None = None,
) -> int:
    """Run ``spec`` until it stops on purpose (or crash-loops); returns the status to exit with."""
    policy = policy or RestartPolicy()
    report = _Reporter(name, log_path, out or sys.stderr)
    child_env = host_env(env, name)
    stop: list[int] = []
    child: subprocess.Popen | None = None

    def request_stop(signum: int, _frame: object) -> None:
        stop.append(signum)
        if child is not None and child.poll() is None:
            child.send_signal(signum)

    # The harness owns the keyboard: a stray Ctrl-C/Ctrl-\ in cooked mode is its business, not
    # ours. Python-level no-ops (not SIG_IGN), so the child still starts with default dispositions.
    handlers = {
        signal.SIGTERM: request_stop,
        signal.SIGHUP: request_stop,
        signal.SIGINT: lambda *_: None,
        signal.SIGQUIT: lambda *_: None,
    }
    previous = {sig: signal.signal(sig, handler) for sig, handler in handlers.items()}
    termios_state = _save_termios()
    command = list(spec.argv)
    cwd = spec.cwd
    initial = spec.session
    restarts = 0
    try:
        while True:
            started, started_wall = clock(), time.time()
            try:
                child = subprocess.Popen(
                    child_argv(spec, command), env=child_env, cwd=cwd if os.path.isdir(cwd) else None
                )
            except OSError as exc:
                report(f"cannot start {shlex.join(command)}: {exc}")
                return 127
            if stop:  # a stop request raced the spawn
                child.send_signal(stop[0])
            if registry is not None:
                _guarded(registry.write, harness_pid=child.pid, session=initial, restarts=restarts)
            tracker = Tracker(
                spec.harness,
                cwd,
                initial=initial,
                env=child_env,
                home=home or Path.home(),
                terminal=terminal,
            )
            returncode = _wait(child, tracker, poll_interval)
            _restore_termios(termios_state)
            verdict = classify_exit(returncode)
            if stop or not verdict.crashed:
                return verdict.exit_code
            uptime = clock() - started
            ref = tracker.final(started_wall)
            initial = ref or initial
            command = relaunch_argv(spec.harness, command, ref)
            if ref and ref.cwd and os.path.isdir(ref.cwd):
                cwd = ref.cwd
            died = f"{spec.harness} pid {child.pid} {verdict.description} after {_duration(uptime)}"
            delay = policy.next_delay(clock(), uptime)
            if delay is None:
                report(
                    f"{died}; {policy.max_restarts} restarts within {_duration(policy.window)} "
                    f"— not restarting. Resume by hand: {shlex.join(command)}"
                )
                return verdict.exit_code
            report(f"{died}; restarting in {delay:g}s: {shlex.join(command)}")
            deadline = clock() + delay
            while not stop and clock() < deadline:
                sleep(min(0.25, deadline - clock()))
            if stop:
                return verdict.exit_code
            restarts += 1
    finally:
        if registry is not None:
            _guarded(registry.delete)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _guarded(fn: Callable[..., object], **kwargs: object) -> None:
    """A registry that can't be written must never take the session down with it."""
    try:
        fn(**kwargs)
    except OSError:
        pass


def _wait(child: subprocess.Popen, tracker: Tracker, interval: float) -> int:
    """``child.wait()``, sampling what the live process publishes while it runs."""
    if not tracker.needs_polling:
        return child.wait()
    done = threading.Event()

    def sample() -> None:
        while not done.wait(interval):
            tracker.poll(child.pid)

    tracker.poll(child.pid)
    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    try:
        return child.wait()
    finally:
        done.set()
        sampler.join()


def _save_termios() -> list | None:
    try:
        import termios

        return termios.tcgetattr(0) if os.isatty(0) else None
    except (ImportError, OSError):
        return None


def _restore_termios(state: list | None) -> None:
    """Undo the dead TUI's raw mode so the relaunch (and any final message) sees a sane pty."""
    if state is None:
        return
    try:
        import termios

        termios.tcsetattr(0, termios.TCSANOW, state)
    except (ImportError, OSError):
        pass


def main(argv: list[str]) -> int:
    """``rig harness host <name>`` — what ``rig harness run`` puts inside abduco."""
    from . import omp_inspect

    if len(argv) != 1:
        print("usage: rig harness host <name>  (started by `rig harness run`)", file=sys.stderr)
        return 2
    try:
        name = validate_name(argv[0])
        spec = HostSpec.from_env(os.environ.get(ENV_SPEC))
    except ValueError as exc:
        print(f"rig harness host: {exc}", file=sys.stderr)
        return 2
    env = dict(os.environ)
    return supervise(
        spec,
        name=name,
        env=env,
        registry=Registry.open(name, spec, env),
        log_path=omp_inspect.abduco_socket_dir() / "supervisor.log",
        terminal=terminal_id(),
    )

