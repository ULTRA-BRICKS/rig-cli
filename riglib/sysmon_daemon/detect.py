"""Detectors: pure functions from readings to :class:`~.model.Finding` verdicts.

Four inputs, five detectors, one output shape:

- :func:`sample_findings` — the in-process :class:`~.probe.Sample`: load per CPU, swap, WIRED
  share of RAM, reclaimable pages, free pages, compressor use against its page limit, free disk
  on the swap volume, how full the data volume is, process-table use and process-count growth,
  and the kernel's own memorystatus level. Wired memory and reclaimable pages are here because
  the 2026-09-29 12:47 panic sat at 95.7% wired (GPU buffers of 13 ``whisper-cli`` model loads)
  and 112 MB reclaimable for minutes while ``kern.memorystatus_vm_pressure_level`` still said
  "normal".
- :func:`census_findings` — the ``ps`` census: same-name process storms (every user's processes,
  absolute and as a percent of ``kern.maxproc``), GPU-model processes
  above the agent-tools#817 slot cap, and task_for_pid tools stuck behind a SecurityAgent
  prompt (the 2026-09-29 ``lldb`` → ``system.privilege.taskport.debug`` dialog that left 32
  ``sample`` processes hanging).
- :func:`event_findings` — the unified-log event window: WindowServer ``Timer delayed by N s``
  (the display-freeze precursor that began 26 minutes before the WindowServer-watchdog panic),
  watchdogd check-in trouble, and authd taskport prompts.
- :func:`coalition_findings` — the census grouped by launchd coalition: one job's whole fan-out
  over an absolute or percent line, named by its launchd label (rig-cli#644).
- :func:`daemon_findings` — the watched launchd jobs (``launchctl print``): an interval job whose
  run hangs far past its interval, and a job launchd keeps relaunching after failed exits
  (rig-cli#643). These findings name the label the heal ladder's ``restart`` rung acts on.

Nothing here does I/O; the daemon feeds readings in and acts on the findings.
"""

from __future__ import annotations

import os
from collections import Counter, defaultdict
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .model import (
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_RANK,
    SEVERITY_WARNING,
    Finding,
    LaunchdJob,
    Offender,
    Proc,
)
from .probe import GIB, Sample

#: Metrics where a LOWER value is worse.
LOWER_IS_WORSE = frozenset({"reclaimable_gb", "free_gb", "disk_free_gb"})

#: Free pages must stay under a line for this many consecutive samples (20 s at the default
#: 10-s cadence). Normal load dips for a moment: 10 minutes of 50-ms reads on the 16 KB-page Mac
#: (2026-09-30) put 25 of 10,616 reads (1.25 s) under the warn line and one at 911 pages, the
#: kernel's reserve itself, so a single sample landing on such a dip would page about daily.
#: With fewer samples (the first 20 s after a daemon start, ``rig sysmon sample``'s one reading)
#: the available readings decide; a critical dip is 1 in ~10,000 reads, so that window costs next
#: to nothing and keeps a one-off reading honest.
FREE_SUSTAIN_SAMPLES = 3

#: Tools that need task_for_pid on macOS; with Developer Mode off they wait on a SecurityAgent
#: password prompt (``system.privilege.taskport.debug``) and hang until someone answers it.
TASKPORT_TOOLS = frozenset({
    "sample", "footprint", "lldb", "debugserver", "spindump", "leaks", "vmmap", "heap",
    "malloc_history", "stackshot", "htop", "lsof", "dtrace", "dtruss", "gdb",
})


def level(metric: str, value: Optional[float], thresholds: Dict[str, Dict[str, float]]) -> str:
    """Severity of ``value`` for ``metric`` (``ok`` when the value or its thresholds are absent)."""
    t = thresholds.get(metric)
    if value is None or not t:
        return SEVERITY_OK
    if metric in LOWER_IS_WORSE:
        if value <= t["critical"]:
            return SEVERITY_CRITICAL
        if value <= t["warn"]:
            return SEVERITY_WARNING
        return SEVERITY_OK
    if value >= t["critical"]:
        return SEVERITY_CRITICAL
    if value >= t["warn"]:
        return SEVERITY_WARNING
    return SEVERITY_OK


def worst(findings: Iterable[Finding]) -> str:
    rank = 0
    for f in findings:
        rank = max(rank, SEVERITY_RANK[f.severity])
    return {v: k for k, v in SEVERITY_RANK.items()}[rank]


def _gb(v: Optional[int]) -> Optional[float]:
    return None if v is None else v / GIB


def _fmt_gb(v: Optional[int]) -> str:
    return "n/a" if v is None else f"{v / GIB:.1f} GB"


def _worse(a: str, b: str) -> str:
    return a if SEVERITY_RANK[a] >= SEVERITY_RANK[b] else b


def _pct(n: int, limit: Optional[int]) -> Optional[float]:
    """``n`` as a percent of the process-table limit (``None`` when the limit is unknown)."""
    return 100.0 * n / limit if limit else None


def _fmt_mb(v: Optional[int]) -> str:
    return "n/a" if v is None else f"{v / 1024 ** 2:.0f} MB"


def sample_findings(
    s: Sample, thresholds: Dict[str, Dict[str, float]], history: Sequence[Sample] = ()
) -> List[Finding]:
    """Findings from one in-process sample. ``history`` is older samples (oldest first), used
    for the process-growth rate."""
    out: List[Finding] = []

    def add(key: str, kind: str, metric: str, value: Optional[float], summary: str, **metrics) -> None:
        sev = level(metric, value, thresholds)
        if sev != SEVERITY_OK:
            metrics[metric] = None if value is None else round(value, 2)
            out.append(Finding(key=key, kind=kind, severity=sev, summary=summary, metrics=metrics))

    add("load", "load", "load_per_cpu", s.load_per_cpu,
        f"load {s.load1:.1f} on {s.ncpu} CPUs ({s.load_per_cpu:.2f} per CPU)",
        load1=round(s.load1, 2), ncpu=s.ncpu)
    add("wired", "memory", "wired_pct", s.wired_pct,
        f"wired memory {_fmt_gb(s.wired)} of {_fmt_gb(s.mem_total)} "
        f"({(s.wired_pct or 0):.1f}%) — GPU buffers and kernel memory cannot be paged or compressed",
        wired_gb=_gb(s.wired), mem_total_gb=_gb(s.mem_total))
    add("reclaimable", "memory", "reclaimable_gb", _gb(s.reclaimable),
        f"only {_fmt_gb(s.reclaimable)} reclaimable (free+inactive+speculative+purgeable)",
        free_gb=_gb(s.free))
    # A momentary dip to the reserve is normal load; judge the best of the last few readings.
    recent = [x.free for x in (*history[-(FREE_SUSTAIN_SAMPLES - 1):], s) if x.free is not None]
    held = f"; at most {_fmt_mb(max(recent))} over the last {len(recent)} samples" if len(recent) > 1 else ""
    pages = f" ({s.free_pages:,} pages{held})" if s.free_pages is not None else ""
    add("free", "memory", "free_gb", _gb(max(recent)) if recent else None,
        f"free memory down to {_fmt_mb(s.free)}{pages} — the pageout daemon is not keeping the "
        f"free-page pool filled", free_pages=s.free_pages)
    add("swap", "swap", "swap_pct", s.swap_pct,
        f"swap {_fmt_gb(s.swap_used)} of {_fmt_gb(s.swap_total)} used ({(s.swap_pct or 0):.0f}%)",
        swap_used_gb=_gb(s.swap_used), swap_total_gb=_gb(s.swap_total))
    add("compressor", "memory", "compressor_pct", s.compressor_pct,
        f"memory compressor at {(s.compressor_pct or 0):.0f}% of its page limit",
        compressor_pages=s.compressor_pages, compressor_limit_pages=s.compressor_limit_pages)
    add("disk", "disk", "disk_free_gb", _gb(s.disk_free),
        f"only {_fmt_gb(s.disk_free)} free on the swap volume — swap cannot grow",
    )
    add("data_volume", "disk", "data_used_pct", s.data_used_pct,
        f"data volume {s.data_volume} {(s.data_used_pct or 0):.1f}% full ({_fmt_gb(s.data_free)} free)",
        volume=s.data_volume, data_free_gb=_gb(s.data_free))
    add("procs", "procs", "procs_pct", s.procs_pct,
        f"{s.proc_total} processes of the {s.proc_max} process-table limit",
        proc_total=s.proc_total, proc_max=s.proc_max)
    if s.pressure_level in (2, 4):
        sev = SEVERITY_CRITICAL if s.pressure_level == 4 else SEVERITY_WARNING
        out.append(Finding(
            key="pressure", kind="memory", severity=sev,
            summary=f"kernel memory pressure is {'CRITICAL' if s.pressure_level == 4 else 'WARN'}",
            metrics={"pressure_level": s.pressure_level},
        ))
    growth = proc_growth_per_min(s, history)
    if growth is not None:
        sev = level("proc_growth_per_min", growth, thresholds)
        if sev != SEVERITY_OK:
            out.append(Finding(
                key="proc_growth", kind="procs", severity=sev,
                summary=f"process count growing {growth:.0f}/min (now {s.proc_total})",
                metrics={"proc_growth_per_min": round(growth, 1), "proc_total": s.proc_total},
            ))
    return out


def proc_growth_per_min(s: Sample, history: Sequence[Sample], window_s: float = 120.0) -> Optional[float]:
    """Process-count change per minute against the oldest sample within ``window_s``."""
    base = None
    for old in history:
        if 20.0 <= s.taken_at - old.taken_at <= window_s:
            base = old
            break
    if base is None:
        return None
    return (s.proc_total - base.proc_total) * 60.0 / (s.taken_at - base.taken_at)


# ── census ────────────────────────────────────────────────────────────────────────────────
OffenderFn = Callable[[Proc], Offender]


def _is_gpu_model(proc: Proc, patterns: Sequence[str]) -> bool:
    if proc.comm in patterns:
        return True
    head = proc.args.split(None, 1)[0] if proc.args else ""
    if os.path.basename(head) in patterns:
        return True
    # python -m mlx_lm.generate / mlx_whisper
    return any(f"-m {p}" in proc.args for p in patterns if p.startswith("mlx"))


def census_findings(
    procs: Sequence[Proc],
    thresholds: Dict[str, Dict[str, float]],
    *,
    my_uid: int,
    gpu_patterns: Sequence[str],
    gpu_slots: int,
    gpu_slots_held: Optional[int],
    offender: OffenderFn,
    tree_of: Callable[[int], List[int]],
    proc_max: Optional[int] = None,
) -> List[Finding]:
    """Findings from the ``ps`` census.

    ``offender`` turns a :class:`Proc` into an attributed :class:`Offender` (the daemon binds it
    to an :class:`~.attrib.Attributor`). ``tree_of(pid)`` returns the pids of the orphaned tree a
    GPU-model process belongs to (its reparented root and every descendant), so the heal ladder
    can stop the loop that keeps relaunching the model, not only the model itself. ``proc_max``
    (``kern.maxproc``) turns a storm's count into its share of the process table.
    """
    out: List[Finding] = []
    by_pid = {p.pid: p for p in procs}
    mine = [p for p in procs if p.uid == my_uid]

    # same-name storms, machine-wide (rig-cli#644): every user's processes fill the one process
    # table; another user's offenders are still protected from the heal ladder (attrib).
    groups: Dict[str, List[Proc]] = defaultdict(list)
    for p in procs:
        groups[p.comm].append(p)
    for comm, members in sorted(groups.items()):
        n = len(members)
        pct = _pct(n, proc_max)
        sev = _worse(level("storm_same_name", n, thresholds), level("storm_pct", pct, thresholds))
        if sev == SEVERITY_OK:
            continue
        share = f" ({pct:.1f}% of the {proc_max}-process limit)" if pct is not None else ""
        out.append(Finding(
            key=f"storm:{comm}", kind="storm", severity=sev,
            summary=f"process storm: {n} × {comm}{share}",
            metrics={"count": n, "comm": comm, "storm_same_name": n,
                     "storm_pct": None if pct is None else round(pct, 2)},
            offenders=tuple(offender(p) for p in members),
            healable=True,
        ))

    # GPU-model processes vs the machine-wide slot cap (agent-tools#817)
    gpu = [p for p in procs if _is_gpu_model(p, gpu_patterns)]
    if len(gpu) > gpu_slots:
        pids: List[int] = []
        for p in gpu:
            for pid in tree_of(p.pid) or [p.pid]:
                if pid not in pids:
                    pids.append(pid)
        out.append(Finding(
            key="gpu_models", kind="gpu", severity=SEVERITY_CRITICAL,
            summary=(f"{len(gpu)} GPU-model processes running, cap is {gpu_slots} "
                     f"({gpu_slots_held if gpu_slots_held is not None else '?'} slots held) — "
                     "each load wires its model into GPU memory"),
            metrics={"gpu_models": len(gpu), "gpu_slots": gpu_slots, "gpu_slots_held": gpu_slots_held,
                     "rss_gb": round(sum(p.rss_kb for p in gpu) / 1024 / 1024, 2)},
            offenders=tuple(offender(by_pid[pid]) for pid in pids if pid in by_pid),
            healable=True,
        ))

    # task_for_pid tools stuck behind a SecurityAgent prompt
    if any(p.comm == "SecurityAgent" for p in procs):
        stuck = [p for p in mine if p.comm in TASKPORT_TOOLS and p.etime_s >= thresholds["taskport_stuck_s"]["warn"]]
        if stuck:
            oldest = max(p.etime_s for p in stuck)
            sev = level("taskport_stuck_s", oldest, thresholds)
            if len(stuck) >= 5:
                sev = SEVERITY_CRITICAL
            names = ", ".join(sorted({p.comm for p in stuck}))
            out.append(Finding(
                key="taskport_blocked", kind="taskport", severity=sev,
                summary=(f"SecurityAgent prompt is up and {len(stuck)} task_for_pid tool(s) ({names}) "
                         f"hang behind it (oldest {oldest:.0f}s) — answer or cancel the password "
                         "dialog on the Mac; htop/sample/footprint/lsof will hang until then"),
                metrics={"stuck": len(stuck), "oldest_s": oldest, "taskport_stuck_s": oldest},
                offenders=tuple(offender(p) for p in stuck),
                healable=True,
            ))
    return out


def top_rss(procs: Sequence[Proc], offender: OffenderFn, n: int = 5) -> List[Offender]:
    """The ``n`` largest resident processes (context for memory findings, never healed)."""
    return [offender(p) for p in sorted(procs, key=lambda p: p.rss_kb, reverse=True)[:n]]


# ── launchd coalitions ────────────────────────────────────────────────────────────────────
def coalition_labels(jobs: Dict[str, LaunchdJob], coalition_of: Dict[int, str]) -> Dict[str, str]:
    """``coalition id → launchd label``: the job whose own process lives in that coalition (by
    lowest pid when several do). Only what ``launchctl list`` reported — never a guess."""
    out: Dict[str, str] = {}
    for label, job in sorted(jobs.items(), key=lambda kv: (kv[1].pid or 0, kv[0])):
        if job.pid and job.pid in coalition_of:
            out.setdefault(coalition_of[job.pid], label)
    return out


def coalition_groups(
    procs: Sequence[Proc], coalition_of: Dict[int, str], labels: Dict[str, str], proc_max: Optional[int]
) -> List[Dict[str, Any]]:
    """Every coalition with its members, largest first: ``{coalition, label, procs, pct, leader,
    members}``. ``leader`` (``"<comm> pid <n>"``) is the oldest member, which names a coalition
    no live launchd job accounts for (the job exited, its children did not)."""
    groups: Dict[str, List[Proc]] = defaultdict(list)
    for p in procs:
        cid = coalition_of.get(p.pid)
        if cid is not None:
            groups[cid].append(p)
    out = []
    for cid, members in groups.items():
        oldest = max(members, key=lambda p: (p.etime_s, -p.pid))
        pct = _pct(len(members), proc_max)
        out.append({"coalition": cid, "label": labels.get(cid), "procs": len(members),
                    "pct": None if pct is None else round(pct, 2), "leader": f"{oldest.comm} pid {oldest.pid}",
                    "members": members})
    return sorted(out, key=lambda g: (-g["procs"], g["coalition"]))


def coalition_findings(
    procs: Sequence[Proc],
    coalition_of: Dict[int, str],
    labels: Dict[str, str],
    proc_max: Optional[int],
    thresholds: Dict[str, Dict[str, float]],
    offender: OffenderFn,
) -> List[Finding]:
    """A coalition over ``coalition_procs`` (absolute) or ``coalition_pct`` (of ``kern.maxproc``)
    — the machine-wide fan-out of ONE launchd job whatever its processes are called or run as
    (the 2026-09-27 crash: 684 processes in one coalition). Attributed to the job's launchd
    label, which the heal ladder's ``restart`` rung may restart when it is an allowlisted daemon."""
    out: List[Finding] = []
    for g in coalition_groups(procs, coalition_of, labels, proc_max):
        n, pct, label = g["procs"], g["pct"], g["label"]
        sev = _worse(level("coalition_procs", n, thresholds), level("coalition_pct", pct, thresholds))
        if sev == SEVERITY_OK:
            continue
        name = label or f"(no live launchd job, leader {g['leader']})"
        share = f" ({pct:.1f}% of the {proc_max}-process limit)" if pct is not None else ""
        mostly = ", ".join(f"{comm} ×{k}" for comm, k in Counter(p.comm for p in g["members"]).most_common(3))
        out.append(Finding(
            key=f"coalition:{label or g['coalition']}", kind="coalition", severity=sev,
            summary=f"launchd coalition {name}: {n} processes{share} — mostly {mostly}",
            metrics={"coalition_procs": n, "coalition_pct": pct, "label": label, "coalition": g["coalition"],
                     "leader": g["leader"]},
            offenders=tuple(offender(p) for p in g["members"]),
            healable=True,
            restart_label=label,
        ))
    return out


# ── launchd daemons ───────────────────────────────────────────────────────────────────────
#: launchd restarts a crashing KeepAlive job every ~10 s; runs are counted over this window.
CRASHLOOP_WINDOW_S = 600.0


def daemon_findings(
    jobs: Dict[str, LaunchdJob],
    history: Dict[str, Sequence[Tuple[float, int]]],
    etime_of: Callable[[int], Optional[float]],
    thresholds: Dict[str, Dict[str, float]],
    now: float,
) -> List[Finding]:
    """Watched launchd jobs that are HUNG or CRASH-LOOPING (rig-cli#643).

    ``jobs`` carry the ``launchctl print`` fields; ``history`` is ``label → [(at, runs), …]`` as
    seen by earlier censuses; ``etime_of(pid)`` is the census age of a process.

    - hung: an interval job (``StartInterval``) whose current run is ``daemon_overrun_x`` times
      its interval old — launchd starts no new run while one is still going, so a hung run stops
      the job for good (the 2026-09-29 tmux-autosave run hung for ~55 min);
    - crash loop: launchd started the job ``daemon_crashloop_runs`` more times within
      :data:`CRASHLOOP_WINDOW_S` than its interval explains, and its last exit was a failure
      (rig-cli#116: a crash-looping tg-ctl agent was relaunched 23,641 times unseen).

    Each finding names the label, so the heal ladder's ``restart`` rung can restart exactly that job.
    """
    out: List[Finding] = []
    for label, job in sorted(jobs.items()):
        age = etime_of(job.pid) if job.pid and job.state == "running" else None
        if age is not None and job.run_interval_s:
            overrun = age / job.run_interval_s
            sev = level("daemon_overrun_x", overrun, thresholds)
            if sev != SEVERITY_OK:
                out.append(Finding(
                    key=f"daemon_hung:{label}", kind="daemon", severity=sev,
                    summary=(f"launchd job {label} (pid {job.pid}) has been on one run for {age / 60:.0f} min, "
                             f"{overrun:.1f}× its {job.run_interval_s}s interval — hung; launchd starts no new run "
                             "until it ends"),
                    metrics={"label": label, "pid": job.pid, "etime_s": age, "run_interval_s": job.run_interval_s,
                             "daemon_overrun_x": round(overrun, 2)},
                    restart_label=label,
                ))
        seen = [(at, runs) for at, runs in history.get(label) or () if now - at <= CRASHLOOP_WINDOW_S]
        if job.runs is None or not seen or not job.last_exit_code:
            continue
        at, runs = min(seen)
        scheduled = int((now - at) // job.run_interval_s) if job.run_interval_s else 0
        extra = job.runs - runs - scheduled
        sev = level("daemon_crashloop_runs", extra, thresholds)
        if sev != SEVERITY_OK:
            out.append(Finding(
                key=f"daemon_crashloop:{label}", kind="daemon", severity=sev,
                summary=(f"launchd job {label} was relaunched {extra} times in {(now - at) / 60:.0f} min and last "
                         f"exited with code {job.last_exit_code} — crash loop"),
                metrics={"label": label, "pid": job.pid, "runs": job.runs, "last_exit_code": job.last_exit_code,
                         "daemon_crashloop_runs": extra},
                restart_label=label,
            ))
    return out


# ── log events ────────────────────────────────────────────────────────────────────────────
def event_findings(events: Sequence[dict], thresholds: Dict[str, Dict[str, float]], now: float) -> List[Finding]:
    """Findings from recent unified-log events (``events.parse_event`` dicts)."""
    out: List[Finding] = []
    delays = [e for e in events if e["kind"] == "display_delay" and now - e["at"] <= 60.0]
    if delays:
        per_min = len(delays)
        longest = max(float(e.get("value") or 0.0) for e in delays)
        sev = max(
            (level("display_delays_per_min", per_min, thresholds), level("display_max_delay_s", longest, thresholds)),
            key=lambda s: SEVERITY_RANK[s],
        )
        if sev != SEVERITY_OK:
            out.append(Finding(
                key="display_freeze", kind="display", severity=sev,
                summary=(f"WindowServer timers delayed {per_min}× in the last minute (longest {longest:.2f}s) "
                         "— the display-freeze precursor of the 2026-09-29 WindowServer-watchdog panic"),
                metrics={"display_delays_per_min": per_min, "display_max_delay_s": longest},
            ))
    watchdog = [e for e in events if e["kind"] == "watchdog" and now - e["at"] <= 300.0]
    if watchdog:
        out.append(Finding(
            key="watchdog", kind="display", severity=SEVERITY_CRITICAL,
            summary=f"watchdogd reports WindowServer check-in trouble: {watchdog[-1].get('text', '')[:160]}",
            metrics={"events": len(watchdog)},
        ))
    prompts = [e for e in events if e["kind"] == "taskport_prompt" and now - e["at"] <= 300.0]
    if prompts:
        out.append(Finding(
            key="taskport_prompt", kind="taskport", severity=SEVERITY_WARNING,
            summary=("a task_for_pid authorization prompt (system.privilege.taskport) was raised — "
                     "something ran a debugger/profiler with Developer Mode off; the SecurityAgent "
                     "password dialog blocks htop/sample/footprint/lsof until it is answered"),
            metrics={"events": len(prompts)},
        ))
    return out
