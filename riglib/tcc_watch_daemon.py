"""tcc-watch daemon — Phase 1 (DETECTION-ONLY) macOS TCC permission-prompt observability.

## What this is

The actual long-running process a `tcc-watch` LaunchAgent execs (see :mod:`riglib.tcc_watch`
for the provisioning side). It tails macOS's own unified log stream, filtered to the
``com.apple.TCC`` subsystem (the daemon — ``tccd`` — that decides and prompts for every
Photos/Contacts/Calendar/Screen-Recording/Full-Disk-Access/Automation/etc access grant), and
appends a durable JSON-lines record of every event that looks like a live permission REQUEST
to ``events.jsonl`` next to this script.

This is Phase 1 of the phased design (rig-cli ticket referenced in
:mod:`riglib.tcc_watch`'s module docstring) — SEE THAT TICKET for the full rationale. Phase 1 is
deliberately narrow:

- observe and log only;
- it does NOT touch the actual system dialog (no auto-dismiss/click — that is Phase 2, gated on
  a separate design review because driving the Accessibility API to interact with a live system
  UI element is a materially higher-risk action than reading a log stream);
- it does NOT read or write ``TCC.db``;
- it does NOT yet attribute the event to a specific agent process or send any notification
  (Phases 3-4).

## Why `log stream`, not the Accessibility API, for detection

`log stream --predicate 'subsystem == "com.apple.TCC"'` is Apple's own sanctioned, documented
mechanism for observing TCC activity (`man log`) — no private API, no TCC.db access, no
Accessibility permission needed just to detect that a request happened. It is the same
technique published by Apple-admin tooling vendors (Omnissa/Filewave) for TCC troubleshooting.
The ``--debug`` flag is required: the useful per-service ``kTCCService*`` / ``AttributionChain``
detail lines are at debug level and are suppressed by a plain ``log stream`` without it.

## Why this runs as a separate privileged daemon, not inside the eval-tool sandbox

This script is invoked directly by a machine-wide macOS LaunchAgent (`riglib.tcc_watch`'s
plist), started once at login and kept alive by launchd — exactly like `tg-ctl`
(`riglib/tg_ctl.py`) and the model-freshness `schedule` (`riglib/schedule.py`). It is NOT part
of the sandboxed eval-tool subprocess any running agent invokes, and it does not run inside any
particular agent's process tree. `osascript`/System-Events introspection is banned for the
*eval tool* specifically because an agent-controlled subprocess driving that is itself a
plausible sandbox-escape vector; this watchdog is the opposite shape — a rig-provisioned,
human-owned, always-on background process that outlives and is independent of every individual
agent session, observing the OS from the outside. The distinction is WHO controls the process,
not which OS API it calls.

## Design for testability without a live TCC dialog

:func:`parse_tcc_log_line` and :func:`run` are pure/injectable: `parse_tcc_log_line` takes one
NDJSON text line and returns a :class:`TccEvent` or ``None`` — no subprocess, no I/O. `run`
accepts a `stream_factory` returning anything with a `.stdout` iterable of lines and a `.wait()`
+ `.returncode` (a fake in tests replays a canned/mocked line sequence) and a `max_reconnects`
bound so a test can make the infinite watch loop terminate deterministically. Tests exercise
this module by importing it directly and REPLAYING canned/sample log lines — never a real TCC
prompt.

Stdlib-only. This file is copied byte-for-byte to the machine (see
`TccWatchPlan.render_watcher_script`) and executed directly via `python3 <path>`, so it must
never depend on the rest of the `riglib` package being importable at runtime.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator, Protocol

# `--debug` is REQUIRED: the useful per-service kTCCService*/AttributionChain detail lines are
# debug-level and are silently dropped by a plain `log stream` predicate match without it.
LOG_STREAM_CMD = [
    "log", "stream",
    "--style", "ndjson",
    "--debug",
    "--predicate", 'subsystem == "com.apple.TCC"',
]

# Substrings (matched case-insensitively) identifying an ``eventMessage`` that plausibly
# corresponds to a live permission REQUEST/decision, as opposed to routine tccd bookkeeping
# chatter on the same subsystem. Grounded in the real, documented com.apple.TCC log vocabulary:
# every per-service decision message embeds the ``kTCCService*`` identifier being evaluated, and
# Apple-admin TCC-troubleshooting guidance (Omnissa/Filewave) filters on ``AttributionChain`` —
# the attribution record naming the requesting/responsible/accessing process — to catch exactly
# this. This is a recall-favoring, best-effort filter: Phase 1's job is "don't miss a real
# prompt", not "zero false positives"; a fast-follow tuning pass against a live machine's actual
# stream (never simulated here) can tighten it once real-world samples are collected.
_PROMPT_MARKERS = (
    "ktccservice",
    "attributionchain",
    "requesting access",
    "prompting policy",
    "will prompt",
)


@dataclass(frozen=True)
class TccEvent:
    """One detected TCC permission-request-shaped log event."""

    timestamp: str
    process: str | None
    pid: int | None
    message: str


def parse_tcc_log_line(line: str) -> TccEvent | None:
    """Parse one `log stream --style ndjson` output line.

    Returns a :class:`TccEvent` iff the line is well-formed JSON, is on the ``com.apple.TCC``
    subsystem, and its ``eventMessage`` matches a :data:`_PROMPT_MARKERS` substring. Returns
    ``None`` for anything else — including a malformed/partial line, which `log stream` can emit
    on a buffered flush boundary. Never raises: a parse failure is a skip, not a crash, because a
    crash would kill the whole always-on daemon over one bad line.
    """
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("subsystem") != "com.apple.TCC":
        return None
    message = obj.get("eventMessage") or ""
    if not isinstance(message, str) or not message:
        return None
    lowered = message.lower()
    if not any(marker in lowered for marker in _PROMPT_MARKERS):
        return None
    pid = obj.get("processID")
    return TccEvent(
        timestamp=str(obj.get("timestamp") or ""),
        process=obj.get("processImagePath") or obj.get("process") or None,
        pid=pid if isinstance(pid, int) else None,
        message=message,
    )


def iter_events(lines: Iterable[str]) -> Iterator[TccEvent]:
    """Filter an iterable of raw log-stream lines down to the :class:`TccEvent`s among them."""
    for line in lines:
        event = parse_tcc_log_line(line)
        if event is not None:
            yield event


def append_event(events_log: Path, event: TccEvent) -> None:
    """Durably append one detected event to ``events_log`` as a JSON line (create-if-missing)."""
    events_log.parent.mkdir(parents=True, exist_ok=True)
    with events_log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(event), sort_keys=True) + "\n")


class _StreamProc(Protocol):
    stdout: Iterable[str] | None
    returncode: int | None

    def wait(self) -> int: ...


def _spawn_log_stream() -> subprocess.Popen:
    return subprocess.Popen(
        LOG_STREAM_CMD, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1,
    )


def _log_err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def run(
    events_log: Path,
    *,
    stream_factory: Callable[[], _StreamProc] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_reconnects: int | None = None,
) -> int:
    """The watch loop: spawn `log stream`, append every detected event, reconnect with backoff
    if the subprocess exits (log daemon restart, transient failure, etc).

    Never returns under normal (production) operation — launchd's ``KeepAlive`` restarts the
    whole process if this loop itself ever exits. ``max_reconnects`` bounds the number of spawn
    attempts (``None`` = unbounded, the production default); tests pass a small bound plus a
    fake ``stream_factory`` so the loop terminates deterministically. Returns the count of
    events appended (test convenience; meaningless — ignored — in the unbounded production path).
    """
    factory = stream_factory or _spawn_log_stream
    backoff = 1.0
    attempts = 0
    appended = 0
    while max_reconnects is None or attempts < max_reconnects:
        attempts += 1
        try:
            proc = factory()
        except OSError as exc:
            _log_err(f"tcc-watch: failed to spawn log stream: {exc}")
            sleep(min(backoff, 60.0))
            backoff = min(backoff * 2, 60.0)
            continue
        backoff = 1.0
        for line in proc.stdout or ():
            event = parse_tcc_log_line(line)
            if event is not None:
                append_event(events_log, event)
                appended += 1
                _log_err(
                    f"tcc-watch: permission-prompt event detected: {event.message!r} "
                    f"(pid={event.pid}, process={event.process})"
                )
        proc.wait()
        _log_err(f"tcc-watch: log stream exited (rc={proc.returncode}); reconnecting")
        sleep(1.0)
    return appended


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    events_log = Path(argv[0]) if argv else Path.home() / ".config" / "rig" / "tcc-watch" / "events.jsonl"
    run(events_log)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
