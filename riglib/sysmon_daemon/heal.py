"""Layer 3 of the sysmon daemon (rig-cli#622): the auto-heal ladder.

The 2026-09-29 kernel panic was not one bad process but a fleet: 13 concurrent ``whisper-cli``
model loads and the orphaned bash children of a crashed omp host kept piling on while nobody
was watching. An alert alone does not stop that, and a monitor that kills on sight would sooner
or later kill somebody's live work. So the ladder climbs one rung per tick, gently first:

1. **notify** the owning agent (``warning`` and ``critical``), once per finding and agent host,
   so the agent that caused the load can stop or serialize its own work;
2. **throttle** every blamed process once the finding is ``critical``: the OS background band
   (``taskpolicy -b`` on macOS, the idle I/O class on Linux), which buys time without losing
   work. A throttle is **restored** as soon as no finding blames that process any more (the
   finding cleared, the process left it, heal was switched off, or the monitor stops). There is
   no renice: an unprivileged process can never lower a nice value again (EACCES), so a renice
   could never be undone — the 2026-09-29 incident left live workers at nice 20 for hours;
3. **kill** a blamed process only after ``grace_seconds`` of continuous critical pressure, and
   only when opted in: ``kill_orphans`` for a PROVEN orphan (its agent host was seen alive and
   has died since, see ``attrib.Lineage``), ``kill_owned`` for an agent's own child. Both are
   off by default;
4. **restart** a launchd daemon that is hung or crash-looping (rig-cli#643), after
   ``grace_seconds`` of critical: ``launchctl kickstart -k gui/<uid>/<label>``. Only a label
   launchd itself reported (``Finding.restart_label``; a process the monitor cannot attribute
   to a job is never restarted), only one matching ``restart_labels``, never the monitor's own
   label or an ``application.*`` GUI job, at most once per ``restart_cooldown_minutes`` and
   ``max_restarts_per_day`` times a day — after that :func:`budget_findings` says a human is needed.

Never touched, on any rung: an offender with ``Attribution.protected_reason`` (user-interactive,
terminals, system processes, other users; see ``attrib``) and an offender whose start time is
unknown, because only the start time proves a signal would still reach the same process.

``plan`` is pure (findings + persisted state → steps + new state) so the ladder is testable
tick by tick; ``execute`` is the only place that signals, throttles or messages, and it re-reads
the start time right before each step so a reused PID is never hit; ``record`` folds each
outcome back AS IT HAPPENS (the monitor passes it as ``on_result``), so only a throttle that
really happened is ever restored, a monitor stopped mid-batch still restores what it already
throttled, a failed restore is retried, and an agent counts as notified only once a note really
reached it.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from functools import partial
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from . import deliver, probe
from .model import SEVERITY_CRITICAL, SEVERITY_WARNING, AgentHost, Finding, Offender, same_start

#: Seconds between SIGTERM and SIGKILL, and the polling step in between.
KILL_WAIT_S = 5.0
KILL_POLL_S = 0.5
#: Every tg-ctl / ionice call is bounded: the monitor must never hang on a helper.
CMD_TIMEOUT_S = 15.0
#: Offenders listed in one agent message (a 300-process storm stays one readable message).
MAX_LISTED = 10
#: Attempts per agent host and finding to deliver a note, per offender to throttle it, and to
#: undo one throttle: a transient refusal (a busy input box, a racing process) gets another
#: tick, a permanent one stops. Independent budgets that happen to share a value.
NOTIFY_ATTEMPTS = 3
THROTTLE_ATTEMPTS = 3
RESTORE_ATTEMPTS = 3
GONE = "pid gone or reused"
#: <sys/resource.h>: the Darwin-only "which" and value behind ``taskpolicy -b``.
PRIO_DARWIN_PROCESS = 4
PRIO_DARWIN_BG = 0x1000
NO_START_TIME = "start time unknown (PID reuse cannot be ruled out)"
#: A process's name is data the process chose: in a note typed into an agent's prompt it is cut
#: to the kernel's own comm length, and its command line is never included.
MAX_COMM_CHARS = 16
DELIVERED = ("done", "dry_run")
DAY_S = 86400.0

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class HealStep:
    """One planned rung. ``pid`` is the offender (the agent host for ``notify``); ``reason``
    carries the message text for ``notify`` and the why for ``throttle``/``restore``/``kill``.

    ``dry_run`` ``None`` follows ``heal.dry_run``; a ``restore`` carries the mode its throttle
    ran in, so a real throttle is undone for real even after switching to dry-run."""

    action: str  # "notify" | "throttle" | "restore" | "kill" | "restart"
    finding_key: str
    pid: int
    start_time: Optional[float]
    reason: str
    host: Optional[AgentHost] = None
    dry_run: Optional[bool] = None
    label: Optional[str] = None  # the launchd job a ``restart`` acts on


@dataclass
class HealOps:
    """Every side effect ``execute`` may cause, injectable so tests never touch real processes.

    ``background``/``foreground``/``restart`` return ``None`` on success or a short error
    description; ``is_background`` answers ``None`` when it cannot tell."""

    start_time: Callable[[int], Optional[float]]
    is_background: Callable[[int], Optional[bool]]
    background: Callable[[int], Optional[str]]
    foreground: Callable[[int], Optional[str]]
    signal: Callable[[int, int], None]
    sleep: Callable[[float], None]
    notify_agent: Callable[[AgentHost, str], str]
    restart: Callable[[str], Optional[str]]


# ── plan (pure) ───────────────────────────────────────────────────────────────────────────
def plan(
    findings: Sequence[Finding], state: Dict[str, Any], cfg: Dict[str, Any], now: float
) -> Tuple[List[HealStep], Dict[str, Any]]:
    """The steps this tick and the new heal state (JSON; the daemon keeps it in memory.json).

    State per healable finding key: the agent hosts a note reached (``notified``) and the
    attempts per host so far, the protected offenders with their reason, and per offender pid its
    start time, whether this finding throttled it, since when it has been critical (the kill
    clock) and whether it was killed. Keys and offenders absent from ``findings`` are dropped,
    so a recurring finding climbs the ladder from the bottom. ``throttled`` is the machine-wide
    record of every throttle that really happened (filled by :func:`record`); an entry no current
    finding still blames moves to ``restoring`` and gets a restore step.
    """
    h = cfg["heal"]
    dry_run = bool(h["dry_run"])
    throttled = _throttle_records(state)
    restarts = _recent_restarts(state, now)
    if not h["enabled"]:
        due = {pid: t for pid, t in throttled.items() if not _parked(t)}
        return (_restores(due, "heal is disabled"),
                {"dry_run": dry_run, "findings": {}, "throttled": {pid: t for pid, t in throttled.items()
                                                                   if pid not in due}, "restoring": due,
                 "restarts": restarts})
    # A dry-run "throttle" touched nothing: switching modes restarts every ladder.
    previous = (state.get("findings") or {}) if state.get("dry_run", dry_run) == dry_run else {}
    notifies: List[HealStep] = []
    actions: List[HealStep] = []
    taken: Set[Tuple[str, int]] = set()  # one throttle/kill per pid per tick across findings
    new: Dict[str, Any] = {}
    for f in findings:
        if not f.healable or f.severity not in (SEVERITY_WARNING, SEVERITY_CRITICAL):
            continue
        new[f.key] = _plan_finding(f, previous.get(f.key) or {}, h, now, notifies, actions, taken,
                                   throttled, dry_run)
    # A pid stays throttled while any finding still blames it — a planned kill included: its
    # record goes only once the kill really happened (``record``), never on a dry-run or failed one.
    held = {pid: o["start_time"] for entry in new.values() for pid, o in entry["offenders"].items()
            if o["throttled"]}
    released = {pid: t for pid, t in throttled.items()
                if (pid not in held or not same_start(held[pid], t["start_time"])) and not _parked(t)}
    # A parked record stays until a finding blames that process again (the ladder then knows the
    # band is its own, and the next release gets fresh restore attempts) or the monitor stops.
    kept = {pid: (_unparked(t) if pid in held and same_start(held[pid], t["start_time"]) else t)
            for pid, t in throttled.items() if pid not in released}
    restores = _restores(released, "no finding blames it any more")
    clocks = _plan_restarts(findings, state, cfg, now, restarts, actions, dry_run)
    return notifies + actions + restores, {"dry_run": dry_run, "findings": new, "throttled": kept,
                                           "restoring": released, "restarts": restarts, "restart_clock": clocks}


# ── the restart rung ──────────────────────────────────────────────────────────────────────
def watched_label(label: str, cfg: Dict[str, Any]) -> bool:
    """A launchd label the monitor watches for hangs and crash loops and may restart: it matches
    ``heal.restart_labels``, is not the monitor's own job, and is no ``application.*`` GUI app
    (restarting the terminal app would kill every session in it)."""
    if label == cfg.get("label") or label.startswith("application."):
        return False
    return any(fnmatch.fnmatchcase(label, str(p)) for p in cfg["heal"].get("restart_labels") or ())


def _recent_restarts(state: Dict[str, Any], now: float) -> Dict[str, List[Dict[str, Any]]]:
    """``label → [{at, dry_run}]`` of the last 24 h (the budget window)."""
    out = {}
    for label, entries in (state.get("restarts") or {}).items():
        kept = [e for e in entries if now - float(e.get("at") or 0) < DAY_S]
        if kept:
            out[label] = kept
    return out


def _restart_refusal(label: str, restarts: Dict[str, List[Dict[str, Any]]], cfg: Dict[str, Any], now: float,
                     dry_run: bool) -> Optional[str]:
    """Why ``label`` must not be restarted now, or ``None``. Dry-run and real restarts keep
    separate books: switching modes must not inherit a budget nothing really spent."""
    h = cfg["heal"]
    if not watched_label(label, cfg):
        return "not a restartable launchd label"
    mine = [float(e["at"]) for e in restarts.get(label, []) if bool(e.get("dry_run")) == dry_run]
    if len(mine) >= int(h["max_restarts_per_day"]):
        return f"restart budget spent ({len(mine)} restarts in 24 h)"
    if mine and now - max(mine) < float(h["restart_cooldown_minutes"]) * 60:
        return "restarted recently (cooldown)"
    return None


def _plan_restarts(findings: Sequence[Finding], state: Dict[str, Any], cfg: Dict[str, Any], now: float,
                   restarts: Dict[str, List[Dict[str, Any]]], actions: List[HealStep],
                   dry_run: bool) -> Dict[str, float]:
    """Append one ``restart`` step per label whose finding has been critical for
    ``grace_seconds``; return the critical clocks (``finding key → since``) to keep."""
    h = cfg["heal"]
    old = state.get("restart_clock") or {}
    clocks: Dict[str, float] = {}
    planned: Set[str] = set()
    for f in findings:
        label = f.restart_label
        if not label or f.severity != SEVERITY_CRITICAL:
            continue
        clocks[f.key] = since = float(old.get(f.key, now))
        if (not h["restart_daemons"] or label in planned or now - since < float(h["grace_seconds"])
                or _restart_refusal(label, restarts, cfg, now, dry_run) is not None):
            continue
        planned.add(label)
        actions.append(HealStep("restart", f.key, int(f.metrics.get("pid") or 0), None,
                                f"{f.severity} {f.kind}: {f.summary}", label=label))
    return clocks


def budget_findings(findings: Sequence[Finding], state: Dict[str, Any], cfg: Dict[str, Any],
                    now: float) -> List[Finding]:
    """A critical finding per restartable label that is still failing after spending its daily
    restart budget: the monitor stops restarting it, and says a human is needed."""
    h = cfg["heal"]
    if not (h["enabled"] and h["restart_daemons"]):
        return []
    restarts = _recent_restarts(state, now)
    dry_run = bool(h["dry_run"])
    out: Dict[str, Finding] = {}
    for f in findings:
        label = f.restart_label
        if not label or f.severity != SEVERITY_CRITICAL or label in out or not watched_label(label, cfg):
            continue
        n = sum(1 for e in restarts.get(label, []) if bool(e.get("dry_run")) == dry_run)
        if n >= int(h["max_restarts_per_day"]):
            out[label] = Finding(
                key=f"restart_budget:{label}", kind="daemon", severity=SEVERITY_CRITICAL,
                summary=(f"{label} is still failing after {n} restarts in 24 h (budget spent): sysmon stops "
                         f"restarting it — a human is needed ({f.summary})"),
                metrics={"label": label, "restarts_24h": n},
            )
    return list(out.values())


def _parked(t: Dict[str, Any]) -> bool:
    """A restore that failed ``RESTORE_ATTEMPTS`` times in a row is not retried every tick."""
    return int(t.get("restore_failures") or 0) >= RESTORE_ATTEMPTS


def _unparked(t: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in t.items() if k != "restore_failures"}


def _throttle_records(state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Every throttle still to be undone: the ``throttled`` record, restores that did not run
    or failed (``restoring``), and — for a state written before the record existed — the live
    ladders' own throttled offenders, so an upgrade never strands a process in the background."""
    if "throttled" not in state:
        legacy = {} if state.get("dry_run", True) else {
            pid: {"start_time": o["start_time"], "dry_run": False, "finding_key": key}
            for key, f in (state.get("findings") or {}).items()
            for pid, o in (f.get("offenders") or {}).items()
            if o.get("throttled") and not o.get("killed") and o.get("start_time") is not None
        }
        return legacy
    records = {pid: dict(t) for pid, t in (state.get("restoring") or {}).items()}
    records.update((pid, dict(t)) for pid, t in (state.get("throttled") or {}).items())
    return records


def _plan_finding(
    f: Finding,
    old: Dict[str, Any],
    h: Dict[str, Any],
    now: float,
    notifies: List[HealStep],
    actions: List[HealStep],
    taken: Set[Tuple[str, int]],
    throttled: Dict[str, Dict[str, Any]],
    dry_run: bool,
) -> Dict[str, Any]:
    critical = f.severity == SEVERITY_CRITICAL
    grace = float(h["grace_seconds"])
    old_offenders = old.get("offenders") or {}
    protected: Dict[str, str] = {}
    offenders: Dict[str, Any] = {}
    by_host: Dict[int, Tuple[AgentHost, List[Offender]]] = {}
    for o in f.offenders:
        reason = o.attribution.protected_reason or (NO_START_TIME if o.start_time is None else None)
        if reason:
            protected[str(o.pid)] = reason
            continue
        host = o.attribution.host
        if host is not None:
            by_host.setdefault(host.pid, (host, []))[1].append(o)
        prev = old_offenders.get(str(o.pid)) or {}
        if not same_start(prev.get("start_time"), o.start_time):
            prev = {}  # a different process now holds this pid
        entry = {
            "start_time": o.start_time,
            "throttled": bool(prev.get("throttled")),
            "throttle_failures": int(prev.get("throttle_failures") or 0),
            "critical_since": prev.get("critical_since") if critical else None,
            "killed": bool(prev.get("killed")),
        }
        if critical and not entry["killed"]:
            # ``throttled`` means "planned"; a throttle that failed clears it again (``record``),
            # so the next tick retries it until the budget is spent.
            if h["throttle"] and not entry["throttled"] and entry["throttle_failures"] < THROTTLE_ATTEMPTS:
                entry["throttled"] = True
                if entry["critical_since"] is None or not entry["throttle_failures"]:
                    entry["critical_since"] = now
                if not _already_throttled(throttled.get(str(o.pid)), o.start_time, dry_run):
                    _take(actions, taken, HealStep("throttle", f.key, o.pid, o.start_time,
                                                   f"{f.severity} {f.kind}: {f.summary}"))
            elif entry["critical_since"] is None:
                entry["critical_since"] = now
            elif _killable(o, h) and now - float(entry["critical_since"]) >= grace:
                entry["killed"] = True
                a = o.attribution
                whose = f"orphan of {a.dead_owner}" if a.orphan else f"owned by {a.host.kind} pid {a.host.pid}"
                _take(actions, taken, HealStep("kill", f.key, o.pid, o.start_time,
                                               f"critical for {int(grace)}s ({whose}): {f.summary}"))
        offenders[str(o.pid)] = entry

    # Kept for the whole finding, not only for hosts attributed this tick: a host that drops out
    # of one census must not come back with its note budget reset. Keyed by pid AND start time:
    # a new agent that got a reused host pid is a different recipient with its own budget.
    notified = list(old.get("notified") or [])
    tries = dict(old.get("notify_tries") or {})
    host_starts = dict(old.get("host_starts") or {})
    for host_pid, (host, _) in by_host.items():
        known = host_starts.get(str(host_pid))
        if known is not None and not same_start(known, host.start_time):
            notified = [pid for pid in notified if pid != host_pid]
            tries.pop(str(host_pid), None)
        host_starts[str(host_pid)] = host.start_time
    if h["notify_agent"]:
        for host_pid, (host, owned) in sorted(by_host.items()):
            if host_pid not in notified and tries.get(str(host_pid), 0) < NOTIFY_ATTEMPTS:
                tries[str(host_pid)] = tries.get(str(host_pid), 0) + 1
                notifies.append(HealStep("notify", f.key, host_pid, host.start_time,
                                         agent_message(f, owned, h), host))
    return {"notified": sorted(notified), "notify_tries": tries, "host_starts": host_starts,
            "protected": protected, "offenders": offenders}


def _already_throttled(t: Optional[Dict[str, Any]], start: Optional[float], dry_run: bool) -> bool:
    """This very process already sits in the background band because of us (a dry-run record
    only counts while still in dry-run: switching to live must really throttle it)."""
    return t is not None and same_start(t.get("start_time"), start) and (dry_run or not t.get("dry_run"))


def _restores(throttled: Dict[str, Dict[str, Any]], why: str) -> List[HealStep]:
    return [HealStep("restore", str(t.get("finding_key") or ""), int(pid), t.get("start_time"), why,
                     dry_run=bool(t.get("dry_run")))
            for pid, t in sorted(throttled.items(), key=lambda kv: int(kv[0]))]


def _killable(o: Offender, h: Dict[str, Any]) -> bool:
    a = o.attribution
    return bool((a.orphan and h["kill_orphans"]) or (a.host is not None and h["kill_owned"]))


def _take(actions: List[HealStep], taken: Set[Tuple[str, int]], step: HealStep) -> None:
    if (step.action, step.pid) not in taken:
        taken.add((step.action, step.pid))
        actions.append(step)


def record(state: Dict[str, Any], results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Fold ``execute`` outcomes into the state (one result at a time is fine, and is what the
    monitor does): a throttle that really happened (or would have, in dry-run) is remembered with
    its start time so exactly that process is restored; a throttle that failed is planned again
    (up to ``THROTTLE_ATTEMPTS``); a real kill drops its record; a failed restore goes back to
    ``throttled`` with its failure count — retried each tick up to ``RESTORE_ATTEMPTS``, then
    parked but never forgotten (see :func:`plan`); a delivered note marks its agent host
    notified for that finding; every restart that ran (done, failed or dry-run) counts for its
    label's cooldown and daily budget — a kickstart that keeps failing must not run every tick."""
    throttled = dict(state.get("throttled") or {})
    restoring = dict(state.get("restoring") or {})
    findings = dict(state.get("findings") or {})
    restarts = {label: list(entries) for label, entries in (state.get("restarts") or {}).items()}
    for r in results:
        pid, outcome = str(r["pid"]), r["outcome"]
        if r["action"] == "throttle" and outcome in DELIVERED:
            throttled[pid] = {"start_time": r["start_time"], "dry_run": outcome == "dry_run",
                              "finding_key": r["finding_key"]}
        elif r["action"] == "throttle" and outcome == "failed":
            findings = _throttle_failed(findings, pid, r["start_time"])
        elif r["action"] == "kill" and outcome == "done":
            if pid in throttled and same_start(throttled[pid].get("start_time"), r["start_time"]):
                del throttled[pid]  # a killed process has nothing left to restore
        elif r["action"] == "restore":
            t = restoring.pop(pid, None)
            if outcome == "failed" and t is not None:
                throttled[pid] = {**t, "restore_failures": int(t.get("restore_failures") or 0) + 1}
        elif r["action"] == "notify" and outcome in DELIVERED and r["finding_key"] in findings:
            entry = findings[r["finding_key"]]
            findings[r["finding_key"]] = {**entry, "notified": sorted({*entry.get("notified", []), int(r["pid"])})}
        elif r["action"] == "restart" and outcome != "skipped" and r.get("label"):
            restarts.setdefault(str(r["label"]), []).append({"at": r["at"], "dry_run": outcome == "dry_run"})
    return {**state, "findings": findings, "throttled": throttled, "restoring": restoring, "restarts": restarts}


def _throttle_failed(findings: Dict[str, Any], pid: str, start: Optional[float]) -> Dict[str, Any]:
    """Every ladder that planned this very process's throttle counts the failure and plans it
    again next tick (one step served them all: ``_take``)."""
    out = {}
    for key, entry in findings.items():
        o = (entry.get("offenders") or {}).get(pid)
        if o is not None and o.get("throttled") and same_start(o.get("start_time"), start):
            o = {**o, "throttled": False, "throttle_failures": int(o.get("throttle_failures") or 0) + 1}
            entry = {**entry, "offenders": {**entry["offenders"], pid: o}}
        out[key] = entry
    return out


def release(state: Dict[str, Any]) -> Tuple[List[HealStep], Dict[str, Any]]:
    """Restore every throttle (the monitor is stopping) and forget that the ladders throttled,
    so the next start throttles again if the pressure is still there. A restore that fails goes
    back to ``throttled`` (:func:`record`) and is retried by the next start, or parked there."""
    findings = {
        key: {**f, "offenders": {pid: {**o, "throttled": False} for pid, o in (f.get("offenders") or {}).items()}}
        for key, f in (state.get("findings") or {}).items()
    }
    records = _throttle_records(state)
    return _restores(records, "the monitor is stopping"), {**state, "findings": findings, "throttled": {},
                                                           "restoring": records}


def agent_message(finding: Finding, offenders: Sequence[Offender], heal_cfg: Dict[str, Any]) -> str:
    """The note an agent host receives: what is wrong, what the ladder does next, how to do the
    work without overloading the machine, and which of ITS processes are to blame — in that
    order, so a note cut short for typing into a terminal still carries the ask.

    Takes the ``heal`` config because "what happens next" depends on it (grace, kill_owned)."""
    lines = [f"[rig sysmon] {finding.severity.upper()} {finding.kind}: {finding.summary}"]
    grace = int(heal_cfg["grace_seconds"])
    rungs = []
    if heal_cfg["throttle"]:
        rungs.append("moves them to the background band (lowest CPU and I/O priority, undone once the pressure clears)")
    if heal_cfg["kill_owned"]:
        rungs.append(f"kills them after {grace}s of continued critical pressure (SIGTERM, then SIGKILL)")
    elif heal_cfg["kill_orphans"]:
        rungs.append(f"kills proven orphans (their agent host died) after {grace}s of continued critical "
                     "pressure (yours are left to you)")
    if rungs:
        when = "Now" if finding.severity == SEVERITY_CRITICAL else "If this turns critical,"
        lines.append(f"{when} sysmon " + "; then ".join(rungs) + ".")
    lines.append(
        "Please stop or serialize this heavy work now: run heavy commands one at a time under the "
        "machine-wide lock (mkdir /tmp/heavy-run.lock, rmdir when done) and load at most one model "
        "at a time through the GPU slot pool."
    )
    numbers = ", ".join(f"{k}={v}" for k, v in sorted(finding.metrics.items()) if v is not None)
    if numbers:
        lines.append(f"Numbers: {numbers}")
    lines.append("Your processes behind it (see their command lines with ps -o args= -p <pid>):")
    for o in offenders[:MAX_LISTED]:
        lines.append(f"- pid {o.pid} {o.comm[:MAX_COMM_CHARS]} ({o.rss_kb / 1048576:.1f} GB RSS)")
    if len(offenders) > MAX_LISTED:
        lines.append(f"- ... and {len(offenders) - MAX_LISTED} more")
    return "\n".join(lines)


# ── execute ───────────────────────────────────────────────────────────────────────────────
def execute(
    steps: Sequence[HealStep],
    cfg: Dict[str, Any],
    ops: Optional[HealOps] = None,
    on_result: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> List[Dict[str, Any]]:
    """Run ``steps``; one ``{action, pid, start_time, finding_key, outcome, detail, at}`` per step,
    in step order, each also handed to ``on_result`` the moment its outcome is final.

    ``outcome`` is ``done`` | ``failed`` | ``skipped`` | ``dry_run``. Never raises for a
    process that vanished or refused; that is an outcome. Kills are batched: every target gets
    SIGTERM first, then ONE shared ``KILL_WAIT_S`` grace, then SIGKILL for the survivors — a
    300-process storm costs 5 s of the daemon's loop, not 300 × 5 s."""
    h = cfg["heal"]
    ops = ops or default_ops(cfg)
    results: List[Optional[Dict[str, Any]]] = [None] * len(steps)

    def settle(i: int, outcome: Tuple[str, str]) -> None:
        step = steps[i]
        results[i] = {"action": step.action, "pid": step.pid, "start_time": step.start_time,
                      "finding_key": step.finding_key, "outcome": outcome[0], "detail": outcome[1],
                      "at": time.time()}
        if step.label is not None:
            results[i]["label"] = step.label
        if on_result is not None:
            on_result(results[i])

    termed: List[Tuple[int, HealStep]] = []
    for i, step in enumerate(steps):
        if h["dry_run"] if step.dry_run is None else step.dry_run:
            settle(i, ("dry_run", _would(step)))
        elif step.action == "kill":
            outcome = _term(step, ops)
            if outcome is None:
                termed.append((i, step))
            else:
                settle(i, outcome)
        elif step.action == "restart":
            # By label: launchd resolves the job and its current pid itself, so there is no pid
            # to re-verify here — and the label came from launchd, never from a guess.
            err = ops.restart(str(step.label))
            settle(i, ("failed", err) if err else ("done", f"launchctl {_kickstart(str(step.label))}"))
        else:
            settle(i, _apply(step, ops))
    waited = 0.0
    while termed and waited < KILL_WAIT_S:
        ops.sleep(KILL_POLL_S)
        waited += KILL_POLL_S
        still = []
        for i, step in termed:
            if _same_process(step, ops):
                still.append((i, step))
            else:
                settle(i, ("done", f"exited {waited:g}s after SIGTERM"))
        termed = still
    for i, step in termed:
        settle(i, _sigkill(step, ops))
    return [r for r in results if r is not None]


def _kickstart(label: str) -> str:
    return f"kickstart -k gui/{os.getuid()}/{label}"


def _would(step: HealStep) -> str:
    if step.action == "notify":
        kind = step.host.kind if step.host else "agent"
        return f"would notify {kind} pid {step.pid}"
    if step.action == "throttle":
        return f"would move pid {step.pid} to the background band"
    if step.action == "restore":
        return f"would take pid {step.pid} out of the background band"
    if step.action == "restart":
        return f"would run launchctl {_kickstart(str(step.label))}"
    return f"would SIGTERM pid {step.pid}, SIGKILL after {KILL_WAIT_S:g}s"


def _same_process(step: HealStep, ops: HealOps) -> bool:
    return step.start_time is not None and same_start(ops.start_time(step.pid), step.start_time)


def _apply(step: HealStep, ops: HealOps) -> Tuple[str, str]:
    if step.action == "notify":
        if step.host is None:
            return "failed", "no agent host to notify"
        # A reused host pid must never be messaged (its pane is somebody else's terminal), and
        # one whose start time is unknown cannot be told apart from a reused one.
        if not _same_process(step, ops):
            return "skipped", "agent host gone or unverifiable"
        detail = ops.notify_agent(step.host, step.reason)
        return ("failed" if detail.startswith("undelivered") else "done"), detail
    if not _same_process(step, ops):
        return "skipped", GONE
    if step.action == "restore":
        err = ops.foreground(step.pid)
        return ("failed", err) if err else ("done", "taken out of the background band")
    if ops.is_background(step.pid):
        # Whoever put it there meant it; restoring later would RAISE it. Leave it untouched.
        return "skipped", "already in the background band, left as it was"
    err = ops.background(step.pid)
    return ("failed", err) if err else ("done", "moved to the background band")


def _term(step: HealStep, ops: HealOps) -> Optional[Tuple[str, str]]:
    """SIGTERM a verified target; ``None`` means sent (the caller waits), else the outcome."""
    if not _same_process(step, ops):
        return "skipped", GONE
    try:
        ops.signal(step.pid, signal.SIGTERM)
    except ProcessLookupError:
        return "skipped", GONE
    except OSError as e:
        return "failed", f"SIGTERM: {e}"
    return None


def _sigkill(step: HealStep, ops: HealOps) -> Tuple[str, str]:
    try:
        ops.signal(step.pid, signal.SIGKILL)
    except ProcessLookupError:
        return "done", "exited after SIGTERM"
    except OSError as e:
        return "failed", f"SIGKILL: {e}"
    return "done", f"SIGKILL after {KILL_WAIT_S:g}s"


# ── default side effects ──────────────────────────────────────────────────────────────────
def _call(cmd: List[str], run: Runner) -> Tuple[bool, str]:
    """``(ok, stdout)`` on exit 0, else ``(False, why)``. Never raises."""
    try:
        proc = run(cmd, capture_output=True, text=True, timeout=CMD_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"{cmd[0]}: {e}"
    if proc.returncode == 0:
        return True, (proc.stdout or "").strip()
    return False, f"{cmd[0]} {cmd[1]}: {(proc.stderr or '').strip() or f'exit {proc.returncode}'}"


def _is_background(pid: int, run: Runner) -> Optional[bool]:
    if probe.IS_DARWIN:
        return probe.proc_is_background(pid)
    if probe.IS_LINUX and shutil.which("ionice"):
        ok, out = _call(["ionice", "-p", str(pid)], run)
        return out.startswith("idle") if ok else None
    return None


def _set_band(pid: int, background: bool, run: Runner) -> Optional[str]:
    """Enter/leave the background band: CPU + I/O on macOS, the idle I/O class on Linux.

    macOS: ``setpriority(PRIO_DARWIN_PROCESS, pid, PRIO_DARWIN_BG | 0)`` — exactly what
    ``taskpolicy -b -p`` does, without forking it; the same user may clear it again."""
    what = "background" if background else "foreground"
    if probe.IS_DARWIN:
        try:
            os.setpriority(PRIO_DARWIN_PROCESS, pid, PRIO_DARWIN_BG if background else 0)
        except OSError as e:
            return f"{what}: {e.strerror or e}"
        return None
    if not (probe.IS_LINUX and shutil.which("ionice")):
        return f"{what}: no taskpolicy/ionice on this platform"
    ok, out = _call(["ionice", "-c3" if background else "-c0", "-p", str(pid)], run)
    return None if ok else out


def _restart_job(label: str, run: Runner) -> Optional[str]:
    """``launchctl kickstart -k``: launchd stops the running instance (SIGTERM, then SIGKILL after
    the job's exit timeout) and starts a fresh one — the clean restart, never a signal of ours."""
    if not probe.IS_DARWIN:
        return "restart: launchd jobs exist on macOS only"
    ok, out = _call(["launchctl"] + _kickstart(label).split(), run)
    return None if ok else out


def default_ops(cfg: Dict[str, Any], run: Runner = subprocess.run) -> HealOps:
    return HealOps(
        start_time=probe.proc_start_time,
        is_background=partial(_is_background, run=run),
        background=partial(_set_band, background=True, run=run),
        foreground=partial(_set_band, background=False, run=run),
        signal=os.kill,
        sleep=time.sleep,
        notify_agent=partial(notify_agent, tg_ctl_bin=str(cfg["heal"]["tg_ctl_bin"]),
                             tmux_bin=str(cfg["sessions"].get("tmux_bin") or "tmux"), run=run),
        restart=partial(_restart_job, run=run),
    )


def notify_agent(
    host: AgentHost, text: str, *, tg_ctl_bin: str, tmux_bin: str = "tmux", run: Runner = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Deliver ``text`` to a running agent; a short description of how, or ``undelivered: …``
    (then the Telegram alert is the only channel left).

    1. omp with the tg-ctl live extension: ``tg-ctl omp-live send`` lands in the running
       session itself (only when ``omp-live status`` lists this exact pid);
    2. an agent in a tmux pane or cmux surface: the note is typed into its terminal, only while
       its input box is provably empty (:mod:`.deliver`);
    3. otherwise ``tg-ctl inbox queue <cwd>``: the agent's Stop hook delivers it at the end
       of its next turn (tg-ctl accepts only agents running outside tmux there).
    """
    why: List[str] = []
    if host.kind == "omp":
        sent, detail = _omp_live_send(host.pid, text, tg_ctl_bin, run)
        if sent:
            return detail
        why.append(detail)
    if host.pane:
        sent, detail = deliver.to_pane(host, text, tmux_bin=tmux_bin, run=run, sleep=sleep)
        if sent:
            return detail
        why.append(detail)
    if not host.cwd:
        why.append("agent cwd unknown, no inbox to queue to")
        return "undelivered: " + "; ".join(why)
    ok, out = _call([tg_ctl_bin, "inbox", "queue", host.cwd, text], run)
    if ok:
        return f"inbox queue {host.cwd}: {out}"
    why.append(out)
    return "undelivered: " + "; ".join(why)


def _omp_live_send(pid: int, text: str, tg_ctl_bin: str, run: Runner) -> Tuple[bool, str]:
    ok, out = _call([tg_ctl_bin, "omp-live", "status", "--json"], run)
    if not ok:
        return False, out
    try:
        live = {int(item["pid"]) for item in json.loads(out) if isinstance(item, dict) and "pid" in item}
    except (ValueError, TypeError, KeyError):
        return False, "omp-live status: unreadable JSON"
    if pid not in live:
        return False, f"omp pid {pid} has no live tg-ctl extension"
    request_id = f"sysmon-{pid}-{uuid.uuid4().hex[:12]}"
    # "--" keeps a text that starts with a dash from being parsed as an option.
    ok, out = _call([tg_ctl_bin, "omp-live", "send", str(pid), "--request-id", request_id, "--", text], run)
    return (True, f"omp-live send pid {pid} ({request_id}): {out}") if ok else (False, out)
