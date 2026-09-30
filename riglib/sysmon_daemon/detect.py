"""Detectors: pure functions from readings to :class:`~.model.Finding` verdicts.

Three inputs, three detectors, one output shape:

- :func:`sample_findings` — the in-process :class:`~.probe.Sample`: load per CPU, swap, WIRED
  share of RAM, reclaimable pages, compressor use against its page limit, free disk on the swap
  volume, process-table use and process-count growth, and the kernel's own memorystatus level.
  Wired memory and reclaimable pages are here because the 2026-09-29 12:47 panic sat at 95.7%
  wired (GPU buffers of 13 ``whisper-cli`` model loads) and 112 MB reclaimable for minutes
  while ``kern.memorystatus_vm_pressure_level`` still said "normal".
- :func:`census_findings` — the ``ps`` census: same-name process storms, GPU-model processes
  above the agent-tools#817 slot cap, and task_for_pid tools stuck behind a SecurityAgent
  prompt (the 2026-09-29 ``lldb`` → ``system.privilege.taskport.debug`` dialog that left 32
  ``sample`` processes hanging).
- :func:`event_findings` — the unified-log event window: WindowServer ``Timer delayed by N s``
  (the display-freeze precursor that began 26 minutes before the WindowServer-watchdog panic),
  watchdogd check-in trouble, and authd taskport prompts.

Nothing here does I/O; the daemon feeds readings in and acts on the findings.
"""

from __future__ import annotations

import os
from collections import defaultdict
from typing import Callable, Dict, Iterable, List, Optional, Sequence

from .model import (
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_RANK,
    SEVERITY_WARNING,
    Finding,
    Offender,
    Proc,
)
from .probe import GIB, Sample

#: Metrics where a LOWER value is worse.
LOWER_IS_WORSE = frozenset({"reclaimable_gb", "disk_free_gb"})

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
    add("swap", "swap", "swap_pct", s.swap_pct,
        f"swap {_fmt_gb(s.swap_used)} of {_fmt_gb(s.swap_total)} used ({(s.swap_pct or 0):.0f}%)",
        swap_used_gb=_gb(s.swap_used), swap_total_gb=_gb(s.swap_total))
    add("compressor", "memory", "compressor_pct", s.compressor_pct,
        f"memory compressor at {(s.compressor_pct or 0):.0f}% of its page limit",
        compressor_pages=s.compressor_pages, compressor_limit_pages=s.compressor_limit_pages)
    add("disk", "disk", "disk_free_gb", _gb(s.disk_free),
        f"only {_fmt_gb(s.disk_free)} free on the swap volume — swap cannot grow",
    )
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
) -> List[Finding]:
    """Findings from the ``ps`` census.

    ``offender`` turns a :class:`Proc` into an attributed :class:`Offender` (the daemon binds it
    to an :class:`~.attrib.Attributor`). ``tree_of(pid)`` returns the pids of the orphaned tree a
    GPU-model process belongs to (its reparented root and every descendant), so the heal ladder
    can stop the loop that keeps relaunching the model, not only the model itself.
    """
    out: List[Finding] = []
    by_pid = {p.pid: p for p in procs}
    mine = [p for p in procs if p.uid == my_uid]

    # same-name storms
    groups: Dict[str, List[Proc]] = defaultdict(list)
    for p in mine:
        groups[p.comm].append(p)
    for comm, members in sorted(groups.items()):
        sev = level("storm_same_name", len(members), thresholds)
        if sev == SEVERITY_OK:
            continue
        out.append(Finding(
            key=f"storm:{comm}", kind="storm", severity=sev,
            summary=f"process storm: {len(members)} × {comm}",
            metrics={"count": len(members), "comm": comm, "storm_same_name": len(members)},
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
