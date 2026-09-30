"""Layer 4: remember which agent sessions were live, notice a crash, and resume them safely.

On 2026-09-29 this machine kernel-panicked under a runaway agent fleet and every interactive
omp session died mid-turn. Nothing recorded which sessions had been running, so recovery was
guesswork. This module closes that gap in three steps:

- :func:`snapshot` records the interactive omp hosts and the exact transcript each one holds
  OPEN (from :class:`.attrib.Attributor`, never "newest file in the cwd"), so an incident knows
  which sessions it interrupted.
- :func:`detect_crash` compares the previous heartbeat's boot id with the current one: a new
  boot without a clean shutdown is a crash, blamed on a fresh ``*.panic`` report when one exists.
- :func:`restore` relaunches each interrupted session with ``omp --resume <transcript>`` in a NEW
  detached tmux window and hands it :func:`explanation` as the first message.

The one rule restore never bends (rig-cli PR #624, ``riglib/omp_inspect.py``): a second writer
on a transcript that is still live corrupts it. Restore therefore fails CLOSED — when the host
scan failed, or any omp host's open files could not be read, it resumes nothing. It never
touches an existing pane or window. :func:`plan_restore` is the pure decision;
:func:`restore` is the only function that runs tmux.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set

from . import config, probe, store
from .attrib import Attributor
from .model import AgentHost, to_jsonable

#: How much of a transcript's head to read for its title line (omp writes it first).
TITLE_SCAN_BYTES = 8192
#: How much of a panic report to read; the panic string sits near the top.
PANIC_SCAN_BYTES = 256 * 1024
PANIC_EXCERPT_CHARS = 600
EXPLANATION_MAX_CHARS = 1500
TMUX_TIMEOUT_S = 15.0

SKIP_UNVERIFIABLE = "cannot verify liveness"
SKIP_LIVE = "still live (a second writer would corrupt it)"
SKIP_RESUMED = "already resumed"
SKIP_MISSING = "transcript missing"

DEFAULT_CONFIG = "~/.config/rig/sysmon/sysmon.json"

HostsFn = Callable[[Dict[str, Any]], Optional[List[AgentHost]]]


@dataclass(frozen=True)
class SessionRecord:
    """One live interactive agent session at snapshot time."""

    kind: str
    pid: int
    start_time: Optional[float]
    cwd: Optional[str]
    session_file: str
    session_id: Optional[str]
    pane: Optional[str]
    title: Optional[str]
    captured_at: float


def _record_from_dict(d: Any) -> Optional[SessionRecord]:
    if not isinstance(d, dict) or not isinstance(d.get("session_file"), str):
        return None
    try:
        return SessionRecord(
            kind=str(d.get("kind") or "omp"),
            pid=int(d["pid"]),
            start_time=None if d.get("start_time") is None else float(d["start_time"]),
            cwd=d.get("cwd"),
            session_file=d["session_file"],
            session_id=d.get("session_id"),
            pane=d.get("pane"),
            title=d.get("title"),
            captured_at=float(d.get("captured_at") or 0.0),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _records(items: Any) -> List[SessionRecord]:
    if not isinstance(items, list):
        return []
    return [r for r in (_record_from_dict(d) for d in items) if r is not None]


# ── snapshot ──────────────────────────────────────────────────────────────────────────────
def _transcript_head(path: str) -> Dict[str, Any]:
    """The title line and session header among the transcript's first complete lines."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(TITLE_SCAN_BYTES)
    except OSError:
        return {}
    out: Dict[str, Any] = {}
    for raw in head.split(b"\n")[:-1]:  # the last piece may be cut mid-line
        try:
            entry = json.loads(raw.decode("utf-8"))
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "title" and "title" not in out and isinstance(entry.get("title"), str):
            out["title"] = entry["title"]
        elif entry.get("type") == "session" and "session" not in out:
            out["session"] = entry
    return out


def snapshot(
    hosts: Iterable[AgentHost],
    now: float,
    start_time_of: Callable[[int], Optional[float]] = probe.proc_start_time,
) -> List[SessionRecord]:
    """Interactive omp hosts that hold a transcript open. One-shots (``omp -p``) and hosts with
    no transcript yet (omp writes a new session lazily) have nothing to resume."""
    out: List[SessionRecord] = []
    for host in hosts:
        if host.kind != "omp" or not host.interactive or not host.session_file:
            continue
        head = _transcript_head(host.session_file)
        header = head.get("session") or {}
        out.append(
            SessionRecord(
                kind=host.kind,
                pid=host.pid,
                start_time=start_time_of(host.pid),
                cwd=host.cwd or header.get("cwd"),
                session_file=host.session_file,
                session_id=host.session_id or header.get("id"),
                pane=host.pane,
                title=head.get("title"),
                captured_at=now,
            )
        )
    return out


def write_snapshot(state_dir: Path, records: Iterable[SessionRecord]) -> None:
    store.write_json(state_dir / store.SESSIONS, to_jsonable(list(records)))


def read_snapshot(state_dir: Path) -> List[SessionRecord]:
    return _records(store.read_json(state_dir / store.SESSIONS, []))


# ── crash detection ───────────────────────────────────────────────────────────────────────
def _newest_panic(panic_dirs: Sequence[Path], after: float) -> Optional[Path]:
    best: Optional[Path] = None
    best_mtime = after
    for directory in panic_dirs:
        try:
            candidates = list(Path(directory).glob("*.panic"))
        except OSError:
            continue
        for path in candidates:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime > best_mtime:
                best, best_mtime = path, mtime
    return best


def _panic_excerpt(path: Path) -> Optional[str]:
    """~600 chars starting at ``panic(``. macOS reports are a JSON header line followed by a
    JSON body whose ``panicString`` holds the text; anything else is searched raw."""
    try:
        with open(path, "rb") as fh:
            text = fh.read(PANIC_SCAN_BYTES).decode("utf-8", "replace")
    except OSError:
        return None
    try:
        body = json.loads(text.partition("\n")[2])
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("panicString"), str):
        text = body["panicString"]
    start = text.find("panic(")
    return text[max(start, 0):max(start, 0) + PANIC_EXCERPT_CHARS].strip() or None


def detect_crash(
    prev: Optional[Dict[str, Any]], boot_id: Optional[str], panic_dirs: Sequence[Path]
) -> Optional[Dict[str, Any]]:
    """A crash record when the machine rebooted since ``prev`` without a clean shutdown.

    Both boot ids must be known: an unreadable boot id cannot prove a reboot, and calling every
    daemon restart a crash would resume sessions nobody lost.
    """
    if not prev or not boot_id or not prev.get("boot_id"):
        return None
    if prev["boot_id"] == boot_id or prev.get("clean_shutdown") is True:
        return None
    heartbeat = prev.get("last_heartbeat")
    panic = _newest_panic(panic_dirs, float(heartbeat)) if isinstance(heartbeat, (int, float)) else None
    return {
        "reason": "panic" if panic else "unclean-reboot",
        "last_heartbeat": heartbeat,
        "panic_file": str(panic) if panic else None,
        "panic_excerpt": _panic_excerpt(panic) if panic else None,
        "boot_id_before": prev["boot_id"],
        "boot_id_now": boot_id,
    }


# ── liveness + planning ───────────────────────────────────────────────────────────────────
def live_hosts(
    cfg: Dict[str, Any],
    run: probe.Runner = subprocess.run,
    cwd_of: Callable[[int], Optional[str]] = probe.proc_cwd,
    open_paths_of: Callable[[int], Optional[List[str]]] = probe.proc_open_paths,
) -> Optional[List[AgentHost]]:
    """Every agent host right now, or ``None`` when liveness cannot be verified: ``ps`` failed,
    or an omp host's open files were unreadable (it might be holding a transcript we would
    otherwise resume — an unknown identity blocks restore, as in PR #624)."""
    procs = probe.census(run=run)
    if procs is None:
        return None
    unreadable: Set[int] = set()

    def paths_of(pid: int) -> Optional[List[str]]:
        paths = open_paths_of(pid)
        if paths is None:
            unreadable.add(pid)
        return paths

    sessions_root = config.expand(str(cfg["sessions"]["sessions_dir"]))
    hosts = Attributor(procs, sessions_root=sessions_root, cwd_of=cwd_of, open_paths_of=paths_of).hosts()
    if any(h.kind == "omp" and h.pid in unreadable for h in hosts):
        return None
    return hosts


def live_session_files(hosts: Optional[Iterable[AgentHost]]) -> Optional[Set[str]]:
    """Real paths of every transcript a live host holds open; ``None`` when the scan failed."""
    if hosts is None:
        return None
    return {os.path.realpath(h.session_file) for h in hosts if h.session_file}


def plan_restore(
    records: Iterable[SessionRecord], live_files: Optional[Set[str]], resumed: Set[str]
) -> List[Dict[str, Any]]:
    """One ``resume``/``skip`` decision per distinct transcript, in record order."""
    resumed_real = {os.path.realpath(p) for p in resumed}
    seen: Set[str] = set()
    plan: List[Dict[str, Any]] = []
    for rec in records:
        real = os.path.realpath(rec.session_file)
        if real in seen:
            continue
        seen.add(real)
        if live_files is None:
            reason: Optional[str] = SKIP_UNVERIFIABLE
        elif real in live_files:
            reason = SKIP_LIVE
        elif real in resumed_real:
            reason = SKIP_RESUMED
        elif not os.path.isfile(real):
            reason = SKIP_MISSING
        else:
            reason = None
        plan.append(
            {
                "session_file": rec.session_file,
                "session_id": rec.session_id,
                "cwd": rec.cwd,
                "pane": rec.pane,
                "action": "skip" if reason else "resume",
                "reason": reason or "interrupted by the incident",
            }
        )
    return plan


# ── the first message a resumed session gets ──────────────────────────────────────────────
def _one_line(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _describe(step: Any) -> str:
    if isinstance(step, dict):
        parts = [str(step[k]) for k in ("action", "comm", "pid", "status") if step.get(k) not in (None, "")]
        return " ".join(parts) or _one_line(json.dumps(step, sort_keys=True, default=str), 80)
    return _one_line(step, 80)


def _when(ts: Any) -> str:
    if not isinstance(ts, (int, float)):
        return "unknown time"
    return time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(ts))


def explanation(incident: Dict[str, Any]) -> str:
    """Why the session was resumed, and what it must re-check before continuing."""
    head = (
        f"[rig sysmon] This session was resumed by rig sysmon after incident {incident.get('id')} "
        f"({incident.get('kind', 'unknown')}, {_when(incident.get('opened_at'))}); it was interrupted."
    )
    tail = (
        "Before continuing, re-check your in-flight work: partial or half-written files, commands "
        "that may have run only halfway, worktrees/branches and background jobs. Verify the actual "
        "state instead of assuming it. Run heavy jobs (model loads, builds, test suites) only under "
        "the machine-wide heavy-run lock (mkdir /tmp/heavy-run.lock, pid in /tmp/heavy-run.lock/pid, "
        "rm -rf on exit), one at a time."
    )
    details: List[str] = []
    findings = [f for f in incident.get("findings") or [] if isinstance(f, dict) and f.get("summary")]
    if findings:
        details.append("Cause:")
        details.extend(f"- {_one_line(f['summary'], 160)}" for f in findings)
    crash = incident.get("crash")
    if isinstance(crash, dict):
        line = f"Crash: {crash.get('reason', 'unknown')}"
        if crash.get("panic_excerpt"):
            line += f" — {_one_line(crash['panic_excerpt'], 240)}"
        details.append(line)
    done = [_describe(s) for s in incident.get("heal") or []]
    if incident.get("remediation"):
        done.append(f"remediation: {_describe(incident['remediation'])}")
    if done:
        details.append("Done: " + "; ".join(done))
    budget = EXPLANATION_MAX_CHARS - len(head) - len(tail) - 2
    middle = "\n".join(details)
    if len(middle) > budget:
        middle = middle[: max(budget - 1, 0)].rstrip() + "…"
    return "\n".join(p for p in (head, middle, tail) if p)


# ── restore ───────────────────────────────────────────────────────────────────────────────
def _tmux_argv(
    cfg: Dict[str, Any], entry: Dict[str, Any], message: str, socket: Optional[str], session_exists: bool
) -> List[str]:
    """``new-window`` in the restore session, or ``new-session`` when it does not exist yet.
    Targets use tmux's ``=`` exact match so ``main`` never resolves to ``main2`` or to a window
    named ``main`` in another session."""
    sess = cfg["sessions"]
    name = str(sess["tmux_session"])
    ident = entry.get("session_id") or Path(entry["session_file"]).stem.split("_", 1)[-1]
    argv = [str(sess["tmux_bin"])] + (["-L", socket] if socket else [])
    if session_exists:
        argv += ["new-window", "-d", "-t", f"={name}:"]
    else:
        argv += ["new-session", "-d", "-s", name]
    argv += ["-n", f"sysmon-{ident[:8]}"]
    if entry.get("cwd"):
        argv += ["-c", str(entry["cwd"])]
    # Absolute: the tmux SERVER's PATH (not ours) resolves a bare name, and a server started
    # outside a login shell may lack ~/.bun/bin; the daemon's own PATH does carry it.
    omp = shutil.which(str(sess["omp_bin"])) or str(sess["omp_bin"])
    return argv + [omp, "--resume", entry["session_file"], message]


def _call(argv: List[str], run: probe.Runner) -> Optional[str]:
    """``None`` on success, else the error text."""
    try:
        proc = run(argv, capture_output=True, text=True, timeout=TMUX_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc) or type(exc).__name__
    if proc.returncode != 0:
        return (proc.stderr or "").strip() or f"exit {proc.returncode}"
    return None


def restore(
    state_dir: Path,
    incident_id: str,
    cfg: Dict[str, Any],
    *,
    tmux_socket: Optional[str] = None,
    dry_run: bool = False,
    run: probe.Runner = subprocess.run,
    hosts_fn: HostsFn = live_hosts,
) -> List[Dict[str, Any]]:
    """Resume the incident's interrupted sessions in new detached tmux windows.

    Raises ``LookupError`` for an unknown incident. ``dry_run`` plans and returns the commands
    (assuming the tmux session exists) without running tmux or writing the incident.
    """
    incident = store.load_incident(state_dir, incident_id)
    if incident is None:
        raise LookupError(f"unknown incident: {incident_id}")
    previous = [r for r in incident.get("restore") or [] if isinstance(r, dict)]
    resumed = {str(r["session_file"]) for r in previous if r.get("status") == "resumed" and r.get("session_file")}
    plan = plan_restore(_records(incident.get("sessions")), live_session_files(hosts_fn(cfg)), resumed)
    message = explanation(incident)
    base = [str(cfg["sessions"]["tmux_bin"])] + (["-L", tmux_socket] if tmux_socket else [])
    session_exists: Optional[bool] = True if dry_run else None
    results: List[Dict[str, Any]] = []
    for entry in plan:
        result = dict(entry, status="skipped", command=None, error=None, at=time.time())
        if entry["action"] == "resume":
            if session_exists is None:
                has = base + ["has-session", "-t", "=" + str(cfg["sessions"]["tmux_session"])]
                session_exists = _call(has, run) is None
            argv = _tmux_argv(cfg, entry, message, tmux_socket, session_exists)
            result["command"] = argv
            if dry_run:
                result["status"] = "planned"
            else:
                error = _call(argv, run)
                result["status"] = "failed" if error else "resumed"
                result["error"] = error
                session_exists = session_exists or error is None
        results.append(result)
    if not dry_run:
        incident["restore"] = previous + results
        incident["updated_at"] = time.time()
        store.save_incident(state_dir, incident)
    return results


def _newest_incident_with_sessions(state_dir: Path) -> Optional[str]:
    for incident in reversed(store.list_incidents(state_dir)):
        if incident.get("sessions"):
            return str(incident["id"])
    return None


def _print_table(results: List[Dict[str, Any]]) -> None:
    if not results:
        print("no sessions recorded for this incident")
        return
    for r in results:
        print(f"{r['status']:<8} {r.get('session_id') or '-':<38} {r.get('cwd') or '-'}")
        detail = r["error"] if r["status"] == "failed" else r["reason"]
        print(f"         {detail}")
        if r["status"] == "planned":
            print(f"         $ {shlex.join(r['command'][:-1])} <explanation>")


def restore_main(
    argv: Optional[List[str]] = None,
    *,
    run: probe.Runner = subprocess.run,
    hosts_fn: HostsFn = live_hosts,
) -> int:
    """``sysmon_daemon restore``: 0 when nothing failed, 3 when a launch failed, 2 on usage
    errors or an unknown incident."""
    parser = argparse.ArgumentParser(
        prog="sysmon_daemon restore", description="Resume omp sessions an incident interrupted."
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG, help=f"sysmon.json (default {DEFAULT_CONFIG})")
    parser.add_argument("--incident", help="incident id (default: the newest incident with sessions)")
    parser.add_argument("--tmux-socket", help="tmux -L socket name")
    parser.add_argument("--dry-run", action="store_true", help="print the plan; run and write nothing")
    parser.add_argument("--json", action="store_true", help="print the results as JSON")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    try:
        cfg = config.load(config.expand(args.config))
    except ValueError as exc:
        print(f"sysmon restore: bad config {args.config}: {exc}", file=sys.stderr)
        return 2
    state_dir = config.state_dir(cfg)
    incident_id = args.incident or _newest_incident_with_sessions(state_dir)
    if incident_id is None:
        print(f"sysmon restore: no incident with sessions under {state_dir}", file=sys.stderr)
        return 2
    try:
        results = restore(
            state_dir, incident_id, cfg, tmux_socket=args.tmux_socket, dry_run=args.dry_run,
            run=run, hosts_fn=hosts_fn,
        )
    except LookupError as exc:
        print(f"sysmon restore: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(results, indent=1, sort_keys=True))
    else:
        print(f"incident {incident_id}")
        _print_table(results)
    return 3 if any(r["status"] == "failed" for r in results) else 0
