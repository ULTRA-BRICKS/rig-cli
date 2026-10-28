"""Bounded, argv-only subprocess execution for `rig lint run` (no shell, process-group termination).

Context: `lint_run` classifies outcomes; this module only starts a command and returns what it did.
Trust boundary: argv lists come from providers (never a shell string, never from rig.yaml); stdin is
closed; capture is bounded per stream (head + tail kept: causes live at the start of a tool's output,
summaries at the end); a timeout terminates the whole process group so a wrapper script cannot leave
orphans behind.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

MAX_CAPTURE_BYTES = 4 * 1024 * 1024
_READER_JOIN_S = 5


@dataclass(frozen=True)
class CommandSpec:
    argv: tuple[str, ...]
    cwd: Path
    timeout_s: int
    env: Mapping[str, str]
    max_capture_bytes: int


@dataclass(frozen=True)
class CommandOutcome:
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    truncated: bool
    spawn_error: str | None


CommandRunner = Callable[[CommandSpec], CommandOutcome]


def depth(environ: Mapping[str, str]) -> int:
    """How many `rig lint` runs enclose this process (the recursion marker, `RIG_LINT_DEPTH`)."""
    raw = environ.get("RIG_LINT_DEPTH")
    if raw is None or raw == "":
        return 0
    # A garbled marker must still engage the guard: failing open would allow runaway recursion.
    return int(raw) if raw.isdigit() else 1


def child_env(environ: Mapping[str, str]) -> dict[str, str]:
    env = dict(environ)
    env.pop("FORCE_COLOR", None)
    env["RIG_LINT_DEPTH"] = str(depth(environ) + 1)
    env["NO_COLOR"] = "1"
    # A `cargo` rustup shim would otherwise download a missing toolchain mid-check (network, minutes, an
    # unbounded write under ~/.rustup). Lint only runs what is installed; a missing toolchain is a
    # reported `missing-tool`/failure, never a silent install. Forced: the user's own value must not enable it.
    env["RUSTUP_AUTO_INSTALL"] = "0"
    return env


class _Sink:
    """Keeps the head and the tail of a stream: tool summaries live at the end, causes at the start."""

    def __init__(self, cap: int) -> None:
        self._head_cap = cap // 2
        self._tail_cap = cap - self._head_cap
        self._head = bytearray()
        self._tail = bytearray()
        self.dropped = 0

    def add(self, chunk: bytes) -> None:
        room = self._head_cap - len(self._head)
        if room > 0:
            self._head += chunk[:room]
            chunk = chunk[room:]
        if not chunk:
            return
        self._tail += chunk
        overflow = len(self._tail) - self._tail_cap
        if overflow > 0:
            del self._tail[:overflow]
            self.dropped += overflow

    def text(self) -> str:
        marker = f"\n[... {self.dropped} bytes omitted ...]\n".encode() if self.dropped else b""
        return (bytes(self._head) + marker + bytes(self._tail)).decode("utf-8", "replace")


def _drain(stream, sink: _Sink) -> None:
    try:
        while True:
            chunk = stream.read1(65536)
            if not chunk:
                return
            sink.add(chunk)
    except (OSError, ValueError):
        return


def _terminate_group(proc: subprocess.Popen) -> None:
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError):
        pass


def subprocess_runner(spec: CommandSpec) -> CommandOutcome:
    started = time.monotonic()

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    if not os.path.isdir(spec.cwd):
        return CommandOutcome(None, "", "", elapsed(), False, False, f"NotADirectoryError: {spec.cwd}")
    try:
        proc = subprocess.Popen(
            list(spec.argv), cwd=spec.cwd, env=dict(spec.env), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, start_new_session=True,
        )
    except OSError as exc:
        return CommandOutcome(None, "", "", elapsed(), False, False, f"{type(exc).__name__}: {exc}")
    sinks = (_Sink(spec.max_capture_bytes), _Sink(spec.max_capture_bytes))
    readers = [threading.Thread(target=_drain, args=(stream, sink), daemon=True)
               for stream, sink in zip((proc.stdout, proc.stderr), sinks)]
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        proc.wait(timeout=spec.timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_group(proc)  # before reaping: the pgid cannot be recycled while the leader is unreaped
        proc.wait()
    for reader in readers:
        reader.join(timeout=_READER_JOIN_S)
    for stream in (proc.stdout, proc.stderr):
        stream.close()
    return CommandOutcome(
        None if timed_out else proc.returncode, sinks[0].text(), sinks[1].text(), elapsed(),
        timed_out, any(s.dropped for s in sinks), None,
    )
