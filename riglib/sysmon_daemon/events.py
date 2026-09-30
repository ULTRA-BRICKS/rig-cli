"""The event side of the monitor: one ``log stream`` child, read without polling.

macOS publishes the early signals of this failure mode as unified-log lines, so the daemon
subscribes to them instead of scanning for them:

- WindowServer ``Timer delayed by N s`` (subsystem ``com.apple.coreanimation``). On 2026-09-29
  they began at 12:21, ran 6-14 per minute with delays up to 8.81 s, and stopped at 12:43:19;
  the WindowServer watchdog panicked the kernel at 12:47:02, "no successful checkins in 236
  seconds". 26 minutes of warning.
- watchdogd lines about WindowServer check-ins.
- authd ``Validating session owner … for system.privilege.taskport.debug`` — something asked
  for task_for_pid with Developer Mode off, so a SecurityAgent password dialog is on screen and
  every task_for_pid caller (htop, ``sample``, ``footprint``, ``lsof -p``) hangs behind it.

The daemon ``select()``s on the child's stdout, so an event is handled the moment it arrives;
the numeric sampling in :mod:`.probe` runs on the ``select`` timeout. One predicate, one child,
default log level only (no ``--info``/``--debug``: those multiply ``logd`` work).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections import deque
from typing import Callable, Deque, Dict, List, Optional

PREDICATE = (
    '(process == "WindowServer" AND eventMessage CONTAINS "Timer delayed")'
    ' OR (process == "watchdogd" AND eventMessage CONTAINS[c] "WindowServer")'
    ' OR (process == "authd" AND eventMessage CONTAINS "system.privilege.taskport")'
)

LOG_STREAM_ARGV = ["log", "stream", "--style", "ndjson", "--level", "default", "--predicate", PREDICATE]

_DELAY = re.compile(r"Timer delayed by\s+([\d.]+)\s*s")


def parse_event(line: str, now: Optional[float] = None) -> Optional[Dict[str, object]]:
    """One ``log stream --style ndjson`` line → ``{kind, at, value, text}`` or ``None``.

    ``at`` is the receive time: the stream is live, so arrival order is event order and the
    daemon never parses the log's localized timestamp format.
    """
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    if not isinstance(rec, dict):
        return None
    message = str(rec.get("eventMessage") or "")
    process = os.path.basename(str(rec.get("processImagePath") or rec.get("process") or ""))
    at = time.time() if now is None else now
    if process == "WindowServer":
        m = _DELAY.search(message)
        if m:
            return {"kind": "display_delay", "at": at, "value": float(m.group(1)), "text": message[:200]}
        return None
    if process == "watchdogd":
        return {"kind": "watchdog", "at": at, "value": None, "text": message[:300]}
    if process == "authd" and "system.privilege.taskport" in message:
        return {"kind": "taskport_prompt", "at": at, "value": None, "text": message[:300]}
    return None


class EventWindow:
    """The last ``horizon_s`` seconds of parsed events."""

    def __init__(self, horizon_s: float = 600.0) -> None:
        self.horizon_s = horizon_s
        self._events: Deque[Dict[str, object]] = deque()

    def add(self, event: Dict[str, object]) -> None:
        self._events.append(event)

    def recent(self, now: float) -> List[Dict[str, object]]:
        while self._events and now - float(self._events[0]["at"]) > self.horizon_s:
            self._events.popleft()
        return list(self._events)


Spawner = Callable[..., "subprocess.Popen[bytes]"]


class LogStream:
    """A restartable ``log stream`` child whose stdout the daemon can ``select()`` on.

    ``fileno()`` is ``None`` while there is no live child (off macOS, disabled, or backing off
    after an exit); :meth:`read_lines` returns complete lines only, buffering a partial tail.
    """

    def __init__(
        self,
        argv: Optional[List[str]] = None,
        *,
        spawn: Spawner = subprocess.Popen,
        clock: Callable[[], float] = time.time,
        backoff_s: float = 30.0,
    ) -> None:
        self.argv = argv or LOG_STREAM_ARGV
        self._spawn = spawn
        self._clock = clock
        self._backoff_s = backoff_s
        self._proc: Optional["subprocess.Popen[bytes]"] = None
        self._buf = b""
        self._next_start = 0.0
        self.restarts = 0

    def ensure(self) -> None:
        """Start (or restart after the back-off) the child when it is not running."""
        if self._proc is not None and self._proc.poll() is None:
            return
        if self._proc is not None:
            self._proc = None
            self._next_start = self._clock() + self._backoff_s
            self.restarts += 1
        if self._clock() < self._next_start:
            return
        try:
            self._proc = self._spawn(
                self.argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL
            )
        except OSError:
            self._proc = None
            self._next_start = self._clock() + self._backoff_s

    def fileno(self) -> Optional[int]:
        if self._proc is None or self._proc.stdout is None or self._proc.poll() is not None:
            return None
        return self._proc.stdout.fileno()

    def read_lines(self) -> List[str]:
        """Read what is available (call only after ``select`` said readable)."""
        fd = self.fileno()
        if fd is None:
            return []
        chunk = os.read(fd, 65536)
        if not chunk:  # EOF: the child exited; ensure() restarts it after the back-off
            self.stop()
            self._next_start = self._clock() + self._backoff_s
            return []
        self._buf += chunk
        *lines, self._buf = self._buf.split(b"\n")
        return [line.decode("utf-8", "replace") for line in lines]

    def stop(self) -> None:
        if self._proc is None:
            return
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=5)
        self._proc = None
