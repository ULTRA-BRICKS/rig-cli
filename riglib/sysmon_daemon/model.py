"""Shared data model for the sysmon daemon (rig-cli#622).

Every module in this package exchanges these frozen dataclasses, so the probe, the detectors,
the heal ladder, the remediation launcher and the session restore agree on one shape. Pure data,
no I/O. Stdlib-only and Python 3.9-compatible: the package is copied byte-for-byte to
``~/.config/rig/sysmon/sysmon_daemon/`` and may run under ``/usr/bin/python3``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional, Tuple

SEVERITY_OK = "ok"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"
#: An alert that is not a finding: a resolved notice (tg-ctl alert severity, never ranked).
SEVERITY_INFO = "info"
SEVERITY_RANK = {SEVERITY_OK: 0, SEVERITY_WARNING: 1, SEVERITY_CRITICAL: 2}

#: Agent harness kinds the monitor attributes processes to (and can launch for remediation).
AGENT_KINDS = ("omp", "claude-code", "codex", "opencode")

#: ``tty`` values ``ps`` prints for a process with no controlling terminal.
NO_TTY = ("", "?", "??", "-")

#: Two start-time readings of the same process may differ by rounding (µs vs clock ticks).
START_TIME_TOLERANCE_S = 1.0


def same_start(a: Optional[float], b: Optional[float]) -> bool:
    """Both start times are known and name the same process (so its pid was not reused)."""
    return a is not None and b is not None and abs(float(a) - float(b)) <= START_TIME_TOLERANCE_S


@dataclass(frozen=True)
class Proc:
    """One row of the ``ps`` census."""

    pid: int
    ppid: int
    pgid: int
    uid: int
    tty: str
    stat: str  # BSD/procps state letters; "+" marks the tty's foreground process group
    etime_s: float  # seconds since the process started
    rss_kb: int
    cpu_pct: float
    comm: str  # executable basename
    args: str  # full argv, space-joined

    @property
    def foreground(self) -> bool:
        """In its terminal's foreground process group (someone is typing at it, or it runs
        on behalf of whoever is)."""
        return "+" in self.stat and self.tty not in NO_TTY


@dataclass(frozen=True)
class AgentHost:
    """An agent harness process (omp, claude, codex, opencode) that owns a process tree."""

    pid: int
    kind: str  # one of AGENT_KINDS
    args: str
    cwd: Optional[str] = None
    session_file: Optional[str] = None  # the exact transcript the host holds open
    session_id: Optional[str] = None
    # "tmux:<session>:<window>.<pane>", "cmux:<workspace-id>/<surface-id>", "cmux" (surface
    # unreadable), or None
    pane: Optional[str] = None
    interactive: bool = True  # False for print/exec one-shot runs (omp -p, codex exec, ...)
    start_time: Optional[float] = None  # from the census snapshot: proves a later pid is still this host


@dataclass(frozen=True)
class Attribution:
    """Who a process belongs to: the nearest agent host above it, the pane it runs in, and
    whether it may ever be touched by the heal ladder."""

    pid: int
    chain: Tuple[int, ...]  # pid, ppid, grand-ppid, ... (stops at 1 or an unknown pid)
    host: Optional[AgentHost] = None
    pane: Optional[str] = None
    # Positive evidence only: this tree was seen under an agent host that has died since. A
    # detached tree with no such record (an agent-browser daemon, a nohup'd job) is NOT an orphan.
    orphan: bool = False
    dead_owner: Optional[str] = None  # "<kind> pid <n>": the agent host whose death orphaned it
    interactive: bool = False  # user-interactive: never throttled or killed
    protected_reason: Optional[str] = None  # why the heal ladder must never touch it


@dataclass(frozen=True)
class Offender:
    """A process a finding blames. ``start_time`` guards against PID reuse before any signal."""

    pid: int
    comm: str
    args: str
    rss_kb: int
    start_time: Optional[float]
    attribution: Attribution


@dataclass(frozen=True)
class Finding:
    """One detector verdict. ``key`` is the stable dedup key for alerts and heal state."""

    key: str
    kind: str
    severity: str
    summary: str
    metrics: Dict[str, Any] = field(default_factory=dict)
    offenders: Tuple[Offender, ...] = ()
    healable: bool = False  # offenders are candidates for throttle/kill
    # The launchd label whose clean restart relieves this finding. Set only from what launchd
    # itself reported (``launchctl list``/``print``), never guessed from a process name.
    restart_label: Optional[str] = None


@dataclass(frozen=True)
class LaunchdJob:
    """One launchd job of the monitor's own ``gui/<uid>`` domain. ``pid``/``status`` come from
    ``launchctl list`` (every job); the rest from ``launchctl print`` (watched jobs only)."""

    label: str
    pid: Optional[int]
    status: Optional[int]  # last exit status (negative: killed by that signal)
    state: Optional[str] = None  # "running" | "not running" | "spawn scheduled" | ...
    runs: Optional[int] = None  # how many times launchd has started it since it was loaded
    last_exit_code: Optional[int] = None
    run_interval_s: Optional[int] = None  # StartInterval jobs only


def to_jsonable(value: Any) -> Any:
    """Dataclass (or container of them) → plain JSON-serializable data."""
    if hasattr(value, "__dataclass_fields__"):
        return {k: to_jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    return value


def attribution_from_dict(d: Dict[str, Any]) -> Attribution:
    host = d.get("host")
    return Attribution(
        pid=int(d["pid"]),
        chain=tuple(int(p) for p in d.get("chain") or ()),
        host=AgentHost(**host) if isinstance(host, dict) else None,
        pane=d.get("pane"),
        orphan=bool(d.get("orphan")),
        dead_owner=d.get("dead_owner"),
        interactive=bool(d.get("interactive")),
        protected_reason=d.get("protected_reason"),
    )


def offender_from_dict(d: Dict[str, Any]) -> Offender:
    return Offender(
        pid=int(d["pid"]),
        comm=str(d.get("comm") or ""),
        args=str(d.get("args") or ""),
        rss_kb=int(d.get("rss_kb") or 0),
        start_time=d.get("start_time"),
        attribution=attribution_from_dict(d["attribution"]),
    )


def finding_from_dict(d: Dict[str, Any]) -> Finding:
    return Finding(
        key=str(d["key"]),
        kind=str(d["kind"]),
        severity=str(d["severity"]),
        summary=str(d.get("summary") or ""),
        metrics=dict(d.get("metrics") or {}),
        offenders=tuple(offender_from_dict(o) for o in d.get("offenders") or ()),
        healable=bool(d.get("healable")),
        restart_label=d.get("restart_label"),
    )
