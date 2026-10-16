"""The handler session of ``alert.route: subscriber`` (rig-cli#700).

With that route sysmon alerts only the tg-ctl exclusive holder of source ``sysmon``
(tg-cli#747). When nobody holds the slot, somebody must still own the machine's resource
problems, so sysmon raises one interactive omp in tmux window ``sysmon-handler`` whose first
command takes the slot, and closes that window again once things stay quiet. This module holds
the pure decisions (spawn, Telegram fallback, startup, retire, the pending-alert queue), the argv
builders, the handler's brief, and thin tmux/tg-ctl wrappers that never raise: the sampling loop
must stay up whatever tmux or tg-ctl do.

The state lives in ``memory.json`` under ``handler``; the daemon is its only writer (``rig sysmon
handler spawn`` only drops a request file). Liveness (``tg-ctl alert holder`` plus at most one
``tg-ctl omp-live status``) is read at most once per ``census_interval_seconds`` and cached in the
state; every delivery outcome refreshes the cache for free.
"""

from __future__ import annotations

import html
import json
import math
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import alert, config
from .model import SEVERITY_CRITICAL, SEVERITY_RANK, SEVERITY_WARNING, Finding

SOURCE = "sysmon"
#: what the handler runs first (the brief and the kickoff both say it)
SUBSCRIBE = f"tg-ctl alert subscribe --exclusive --source {SOURCE} --ttl 0"
#: the Telegram notice of a CRITICAL-triggered spawn whose handler never took the slot
START_FAILED_TITLE = "sysmon handler did not start"
PENDING_MAX = 20
DESCENDANT_DEPTH = 16
CMD_TIMEOUT_S = 20
HOLDER_STATES = ("bound", "suspended", "vacant")
_CACHED_STATES = HOLDER_STATES + ("unknown",)
_TAG = re.compile(r"<[^>]+>")

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class Holder:
    """``tg-ctl alert holder`` for source sysmon: ``bound`` | ``suspended`` | ``vacant`` |
    ``unknown`` (the call failed or answered nonsense: the channel itself is broken)."""

    state: str
    session_id: Optional[str] = None
    pid: Optional[int] = None
    busy: Optional[bool] = None


@dataclass(frozen=True)
class Window:
    """The ``sysmon-handler`` tmux window as tmux lists it now."""

    window_id: str
    pane_pid: int


# ── configuration ─────────────────────────────────────────────────────────────────────────
def tmux_session(cfg: Dict[str, Any]) -> str:
    h = cfg["handler"]
    return str(h["tmux_session"] if h.get("tmux_session") is not None else cfg["sessions"]["tmux_session"])


def _minutes(cfg: Dict[str, Any], key: str) -> float:
    return float(cfg["handler"][key]) * 60.0


def _pending_ttl(cfg: Dict[str, Any]) -> float:
    """The longest alert cooldown: a deferred alert must outlive its own dedup window, or a
    six-hour disk page deferred once would never be seen."""
    a, d = cfg["alert"], config.DEFAULTS["alert"]
    return 60.0 * max(float(a.get(k) or d[k]) for k in ("cooldown_minutes", "disk_cooldown_minutes",
                                                          "swap_cooldown_minutes"))


def _raised(findings: Sequence[Finding]) -> List[Finding]:
    return [f for f in findings if SEVERITY_RANK.get(f.severity, 0) >= SEVERITY_RANK[SEVERITY_WARNING]]


# ── persisted state ───────────────────────────────────────────────────────────────────────
_TIMES = ("last_spawn_at", "window_created_at", "last_delivery_at", "last_vacancy_check_at", "last_finding_at")
_FLAGS = ("post_crash_pending", "start_failure_notified")


def _num(value: Any) -> bool:
    """A finite number (``json.loads`` accepts NaN and Infinity, which would freeze every clock)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def load_state(raw: Any) -> Dict[str, Any]:
    """``memory["handler"]`` with every field of the wrong shape dropped, so a hand-edited or
    truncated ``memory.json`` costs at most what it held, never the sampling loop."""
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Any] = {k: raw[k] for k in _TIMES if _num(raw.get(k))}
    out.update({k: raw[k] for k in _FLAGS if isinstance(raw.get(k), bool)})
    if raw.get("last_spawn_ok") in (True, False, None) and "last_spawn_ok" in raw:
        out["last_spawn_ok"] = raw["last_spawn_ok"]
    if raw.get("spawn_severity") in (SEVERITY_WARNING, SEVERITY_CRITICAL):
        out["spawn_severity"] = raw["spawn_severity"]
    if isinstance(raw.get("window_id"), str) and raw["window_id"].startswith("@") and _is_pid(raw.get("pane_pid")):
        out.update(window_id=raw["window_id"], pane_pid=raw["pane_pid"])
    if _is_pid(raw.get("handler_pid")) and isinstance(raw.get("handler_session_id"), str):
        out.update(handler_pid=raw["handler_pid"], handler_session_id=raw["handler_session_id"])
    if isinstance(raw.get("handler_busy"), bool):
        out["handler_busy"] = raw["handler_busy"]
    if _cached_holder(raw.get("holder")) is not None:
        out["holder"] = raw["holder"]
    seen = raw.get("first_seen")
    if isinstance(seen, dict):
        out["first_seen"] = {k: v for k, v in seen.items() if isinstance(k, str) and _num(v)}
    foreign = raw.get("foreign_window")
    if isinstance(foreign, dict) and isinstance(foreign.get("window_id"), str) and _num(foreign.get("since")):
        out["foreign_window"] = {"window_id": foreign["window_id"], "since": foreign["since"]}
    pending = raw.get("pending")
    if isinstance(pending, dict):
        keys = pending.get("keys")
        crash = pending.get("crash")
        out["pending"] = {
            "keys": {k: v for k, v in keys.items() if isinstance(k, str) and _num(v)} if isinstance(keys, dict) else {},
            "crash": crash if isinstance(crash, dict) and isinstance(crash.get("title"), str)
            and isinstance(crash.get("body"), str) and _num(crash.get("at")) else None,
        }
    return out


# ── pure decisions ────────────────────────────────────────────────────────────────────────
def update_first_seen(findings: Sequence[Finding], state: Dict[str, Any], now: float) -> Dict[str, Any]:
    """``first_seen[key]`` for every key present at WARNING or above (a key that clears and comes
    back starts over), and ``last_finding_at``: the last time any was."""
    raised = _raised(findings)
    seen = state.get("first_seen") or {}
    out = {**state, "first_seen": {f.key: float(seen.get(f.key, now)) for f in raised}}
    if raised:
        out["last_finding_at"] = now
    return out


def spawn_due(
    findings: Sequence[Finding], state: Dict[str, Any], now: float, cfg: Dict[str, Any], *,
    holder: Holder, window_exists: bool, post_crash: bool,
) -> Tuple[bool, str]:
    """``(due, why)``: a handler is raised only for a VACANT slot (suspended is a session resuming;
    unknown is a broken channel no handler could subscribe to either), with no handler window,
    outside the spawn cooldown, and for a CRITICAL finding, a WARNING present for
    ``warn_persist_minutes``, or a crash reboot."""
    if holder.state != "vacant":
        return False, f"holder {holder.state}"
    if window_exists:
        return False, "handler window exists"
    last = state.get("last_spawn_at")
    if last is not None and now - float(last) < _minutes(cfg, "spawn_cooldown_minutes"):
        return False, "spawn cooldown"
    raised = _raised(findings)
    critical = [f.key for f in raised if f.severity == SEVERITY_CRITICAL]
    if critical:
        return True, f"critical {', '.join(critical)}"
    seen = state.get("first_seen") or {}
    persist = _minutes(cfg, "warn_persist_minutes")
    lasting = [f.key for f in raised if f.key in seen and now - float(seen[f.key]) >= persist]
    if lasting:
        return True, f"warning persisting {', '.join(lasting)}"
    if post_crash:
        return True, "post-crash"
    return False, "no trigger"


def observe_window(state: Dict[str, Any], window: Optional[Window], now: float) -> Dict[str, Any]:
    """Remember since when a ``sysmon-handler`` window sysmon did not spawn has been there (tmux
    3.6 has no ``window_created``), so a foreign window still ages into the fallback rule."""
    if window is None or window.window_id == state.get("window_id"):
        return {k: v for k, v in state.items() if k != "foreign_window"} if "foreign_window" in state else state
    seen = state.get("foreign_window") or {}
    if seen.get("window_id") == window.window_id:
        return state
    return {**state, "foreign_window": {"window_id": window.window_id, "since": now}}


def window_age(state: Dict[str, Any], window: Window, now: float) -> Optional[float]:
    if window.window_id == state.get("window_id") and state.get("window_created_at") is not None:
        return now - float(state["window_created_at"])
    seen = state.get("foreign_window") or {}
    if seen.get("window_id") == window.window_id:
        return now - float(seen["since"])
    return None


def telegram_fallback_due(
    state: Dict[str, Any], now: float, cfg: Dict[str, Any], *, holder: Holder, window: Optional[Window],
) -> bool:
    """Whether a CRITICAL problem alert the holder did not take must go to Telegram: the channel is
    broken (holder ``unknown``), or the slot is vacant and no handler is coming — the last spawn
    failed, the cooldown blocks a new one while no window exists, or a window has held nothing past
    ``startup_timeout_minutes``."""
    if holder.state == "unknown":
        return True
    if holder.state != "vacant":
        return False
    if state.get("last_spawn_ok") is False:
        return True
    last = state.get("last_spawn_at")
    if window is None:
        return last is not None and now - float(last) < _minutes(cfg, "spawn_cooldown_minutes")
    age = window_age(state, window, now)
    return age is not None and age >= _minutes(cfg, "startup_timeout_minutes")


def handler_starting(state: Dict[str, Any], now: float, cfg: Dict[str, Any], window: Optional[Window]) -> bool:
    """The window sysmon spawned is younger than the startup timeout: its omp is still coming up."""
    if window is None or window.window_id != state.get("window_id"):
        return False
    age = window_age(state, window, now)
    return age is not None and age < _minutes(cfg, "startup_timeout_minutes")


def startup_failed(state: Dict[str, Any], now: float, cfg: Dict[str, Any], *, holder: Holder) -> bool:
    """A spawn failed to start once the startup timeout passed with the slot still vacant; another
    session holding it (bound or suspended) is a success."""
    last = state.get("last_spawn_at")
    if last is None:
        return False
    return now - float(last) >= _minutes(cfg, "startup_timeout_minutes") and holder.state == "vacant"


def is_descendant(child: int, ancestor: int) -> bool:
    """``child`` sits below ``ancestor`` (a harness wrapper may sit between tmux's pane and omp).
    Walks ``/proc/<pid>/stat`` at most :data:`DESCENDANT_DEPTH` levels; any read error is False.
    Without ``/proc`` (macOS) only an omp running as the pane's own process registers; a wrapped
    one is never registered, so it is never retired either: its window stays open, never killed
    on a guess."""
    pid = child
    for _ in range(DESCENDANT_DEPTH):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            pid = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return False
        if pid == ancestor:
            return True
        if pid <= 1:
            return False
    return False


def find_handler_endpoint(
    pane_pid: int, endpoints: Sequence[Dict[str, Any]], is_descendant: Callable[[int, int], bool],
) -> Optional[Dict[str, Any]]:
    """The omp-live endpoint running in the handler's pane: the pane's process or one below it."""
    for ep in endpoints:
        pid = ep.get("pid")
        if _is_pid(pid) and ep.get("sessionId") and (pid == pane_pid or is_descendant(pid, pane_pid)):
            return ep
    return None


def omp_busy(pid: Optional[int], session_id: Optional[str], endpoints: Optional[Sequence[Dict[str, Any]]]) -> Optional[bool]:
    """omp-live's ``busy`` for exactly ``pid`` carrying ``session_id``; ``None`` when unknown (no
    listing, or that pid now carries another session: a reused pid is not the handler)."""
    if endpoints is None or pid is None or not session_id:
        return None
    for ep in endpoints:
        if ep.get("pid") == pid and ep.get("sessionId") == session_id and isinstance(ep.get("busy"), bool):
            return ep["busy"]
    return None


# ── pending alerts (deferred or undelivered problem alerts, rig-cli#700 ruling 16) ────────
def _pending(state: Dict[str, Any]) -> Dict[str, Any]:
    p = state.get("pending") or {}
    return {"keys": dict(p.get("keys") or {}), "crash": p.get("crash")}


def queue_pending(
    state: Dict[str, Any], finding_keys: Sequence[str], now: float, *, crash: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Each finding key waits (the first deferral time kept; at most :data:`PENDING_MAX`, oldest
    evicted); a post-crash alert waits whole, since its finding never recurs."""
    p = _pending(state)
    for key in finding_keys:
        p["keys"].setdefault(key, now)
    p["keys"] = dict(sorted(p["keys"].items(), key=lambda kv: kv[1])[-PENDING_MAX:])
    if crash is not None:
        p["crash"] = crash
    return {**state, "pending": p}


def due_pending(
    state: Dict[str, Any], findings: Sequence[Finding], now: float, cfg: Dict[str, Any],
) -> Tuple[List[Finding], Optional[Dict[str, Any]], Dict[str, Any]]:
    """``(findings to re-render, crash item, new state)``: the CURRENT findings at WARNING or above
    whose keys wait, so nothing stale is replayed; keys that cleared, and keys or a crash item that
    outlived the longest alert cooldown, are dropped."""
    p = _pending(state)
    raised = {f.key: f for f in _raised(findings)}
    ttl = _pending_ttl(cfg)
    p["keys"] = {k: t for k, t in p["keys"].items() if k in raised and now - float(t) < ttl}
    if p["crash"] is not None and now - float(p["crash"].get("at") or 0) >= ttl:
        p["crash"] = None
    return [raised[k] for k in p["keys"]], p["crash"], {**state, "pending": p}


def ack_pending(state: Dict[str, Any], finding_keys: Sequence[str], *, crash: bool) -> Dict[str, Any]:
    p = _pending(state)
    for key in finding_keys:
        p["keys"].pop(key, None)
    if crash:
        p["crash"] = None
    return {**state, "pending": p}


def retire_due(
    findings: Sequence[Finding], state: Dict[str, Any], now: float, cfg: Dict[str, Any], *,
    handler_busy: Optional[bool],
) -> bool:
    """Close the handler sysmon spawned (only one that registered: ``handler_pid``) once, for
    ``idle_exit_minutes``, no finding was at WARNING or above and no alert reached the holder, and
    omp-live says that very session is idle. Unknown busy never retires; neither does the user's own
    holder session, which sysmon never spawned."""
    if not state.get("handler_pid") or handler_busy is not False or _raised(findings):
        return False
    idle = _minutes(cfg, "idle_exit_minutes")
    since = float(state.get("last_spawn_at") or 0)
    quiet_since = max(since, float(state.get("last_finding_at") or 0), float(state.get("last_delivery_at") or 0))
    return now - quiet_since >= idle


# ── argv and texts ────────────────────────────────────────────────────────────────────────
def _tmux(cfg: Dict[str, Any]) -> str:
    return str(cfg["sessions"]["tmux_bin"])


def _tg_ctl(cfg: Dict[str, Any]) -> str:
    return str(cfg["alert"].get("tg_ctl_bin") or config.DEFAULTS["alert"]["tg_ctl_bin"])


def spawn_argv(cfg: Dict[str, Any], brief: Path, kickoff: str, *, session_exists: bool) -> List[str]:
    """``new-window`` in the handler's tmux session, or ``new-session`` when it does not exist;
    ``-P -F`` prints the new window id and pane pid. ``=`` makes the session name an exact match."""
    h = cfg["handler"]
    name = tmux_session(cfg)
    fmt = ["-P", "-F", "#{window_id} #{pane_pid}"]
    argv = [_tmux(cfg)]
    if session_exists:
        argv += ["new-window", "-d", *fmt, "-t", f"={name}:"]
    else:
        argv += ["new-session", "-d", *fmt, "-s", name]
    argv += ["-n", str(h["window"]), "-c", str(config.expand(str(h["workdir"])))]
    # Absolute: the tmux SERVER's PATH (not ours) resolves a bare name (sessions._tmux_argv).
    omp_bin = str(cfg["sessions"]["omp_bin"])
    omp = shutil.which(omp_bin) or omp_bin
    return argv + [omp, "--auto-approve", "--append-system-prompt", str(brief), kickoff]


RETIRE_NOTE = "[sysmon] Nothing to handle for {minutes} minutes: this handler window is being closed."


def retire_argvs(
    cfg: Dict[str, Any], window_id: str, handler_pid: int, handler_session_id: str, holds: bool, now_ms: int,
) -> List[List[str]]:
    tg = _tg_ctl(cfg)
    note = RETIRE_NOTE.format(minutes=int(cfg["handler"]["idle_exit_minutes"]))
    out = [[tg, "alert", "release", "--source", SOURCE, "--session", handler_session_id]] if holds else []
    out.append([tg, "omp-live", "send", str(handler_pid), "--session", handler_session_id,
                "--request-id", f"sysmon-handler-retire-{now_ms}", "--", note])
    out.append([_tmux(cfg), "kill-window", "-t", window_id])
    return out


def build_brief(cfg: Dict[str, Any]) -> str:
    """The handler's appended system prompt (spec *Handler brief*, sections 1-9 in order)."""
    window = f"{tmux_session(cfg)}:{cfg['handler']['window']}"
    return "\n".join([
        "# sysmon handler",
        "",
        "## 1. Mission",
        "You own this machine's resource alerts (sysmon) until you are released. Keep the agent sessions "
        "on this machine working, and never damage the user's work.",
        "",
        "## 2. First command",
        f"Run `{SUBSCRIBE}` before anything else. If it exits 3, another agent took the slot first: report "
        "nothing and stop.",
        "",
        "## 3. Context commands",
        "- `rig sysmon sample`: one reading plus every detector, no side effects.",
        "- `rig sysmon incidents --show <id>`: an incident with its findings, sessions and steps.",
        "- `omp-steer list`: the live agent sessions.",
        "",
        "## 4. Do immediately (no data loss possible)",
        "- Stop or kill processes attributed `ORPHAN` (their agent host is gone).",
        "- Stop your own processes.",
        "- `docker builder prune -f`.",
        "- Ask an owning session to stop heavy work: `omp-steer <pid> '<text>'`.",
        "- Where `memguard` is installed and active: `memguard swap-drain` (it refuses when RAM cannot take "
        "the pages back).",
        "",
        "## 5. Ask first",
        "Ask with `tg --tag decision` and wait for the answer before: anything that deletes or moves user "
        "data (worktrees, caches other than the docker build cache, files); killing or stopping a live "
        "session's non-orphan processes; restarting a user application or service; reboot or logout; "
        "changing configuration or thresholds; `swapoff`/`swapon` by any means other than "
        "`memguard swap-drain`.",
        "",
        "## 6. Telegram",
        "- `tg --tag problem` when a CRITICAL persists 30 minutes after your actions, or you cannot act "
        "without a decision.",
        "- `tg --tag report` after any significant change: a process you killed that you did not own, "
        "`docker builder prune`, `memguard swap-drain`, an approved decision carried out, a ticket filed.",
        "- Nothing else goes to Telegram: no all-clear, no progress.",
        "",
        "## 7. Handoff",
        f"When asked for the slot, grant it (`tg-ctl alert handoff --source {SOURCE} --to <id>`) unless an "
        "action of yours is mid-way; then finish that action and grant.",
        "",
        "## 8. Owners already notified",
        "The session owning an offender already gets sysmon's heal note (or memguard's); do not repeat it "
        "more than once per finding.",
        "",
        "## 9. Never",
        "- Never change code, commit, or run test suites or builds.",
        "- Never kill by `pgrep`/`pstree`/command substitution of a value that can be empty: stop only a PID "
        "you just verified with `[ -n \"$P\" ] && ps -p \"$P\"`, or a systemd scope by name.",
        "- Never treat the scheduled `swap-drain` dip in MemAvailable as a leak.",
        "",
        f"sysmon closes this window ({window}) itself after "
        f"{int(cfg['handler']['idle_exit_minutes'])} quiet minutes.",
        "",
    ])


def kickoff_text(alert_text: str) -> str:
    return f"[sysmon] You are the sysmon handler. First run: {SUBSCRIBE}\n{alert_text}"


def plain_text(title: str, body: str) -> str:
    """An alert as the plain text the kickoff carries (the HTML body without its markup)."""
    return f"{title}\n{html.unescape(_TAG.sub('', body))}"


# ── I/O (never raises) ────────────────────────────────────────────────────────────────────
def _call(argv: Sequence[str], run: Runner) -> Tuple[Optional[int], str]:
    """``(exit code, stdout)``; ``(None, why)`` when the binary never ran."""
    proc, detail = alert.run_safe(argv, run, timeout=CMD_TIMEOUT_S)
    if proc is None:
        return None, detail
    return proc.returncode, (proc.stdout or "").strip() if proc.returncode in (0, 3) else (proc.stderr or "").strip()


def _is_pid(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value < 1 << 32


def live_endpoints(cfg: Dict[str, Any], run: Runner) -> Optional[List[Dict[str, Any]]]:
    """``tg-ctl omp-live status --json``, or ``None`` when it failed."""
    code, out = _call([_tg_ctl(cfg), "omp-live", "status", "--json"], run)
    if code != 0:
        return None
    try:
        listed = json.loads(out or "[]")
    except ValueError:
        return None
    return [e for e in listed if isinstance(e, dict) and _is_pid(e.get("pid"))] if isinstance(listed, list) else None


def read_holder(cfg: Dict[str, Any], run: Runner,
                endpoints: Optional[Sequence[Dict[str, Any]]] = None) -> Holder:
    """``tg-ctl alert holder --source sysmon --json`` (exit 0 bound/suspended, 3 vacant); anything
    else is ``unknown``. ``busy`` comes from ``endpoints`` when given."""
    code, out = _call([_tg_ctl(cfg), "alert", "holder", "--source", SOURCE, "--json"], run)
    if code not in (0, 3):
        return Holder("unknown")
    try:
        doc = json.loads(out)
    except ValueError:
        return Holder("unknown")
    state = doc.get("state") if isinstance(doc, dict) else None
    if state not in HOLDER_STATES or (code == 3) != (state == "vacant"):
        return Holder("unknown")
    rec = doc.get("holder") if isinstance(doc.get("holder"), dict) else {}
    pid = rec.get("pid") if _is_pid(rec.get("pid")) else None
    session = str(rec["sessionId"]) if isinstance(rec.get("sessionId"), str) and rec["sessionId"] else None
    if state != "vacant" and session is None:
        return Holder("unknown")  # a held slot without its session is a tg-ctl this code cannot read
    return Holder(state, session, pid, omp_busy(pid, session, endpoints))


def find_window(cfg: Dict[str, Any], run: Runner) -> Optional[Window]:
    """The ``=<session>:=<window>`` window (exact names), or ``None`` (absent, or tmux failed)."""
    target = f"={tmux_session(cfg)}:={cfg['handler']['window']}"
    code, out = _call([_tmux(cfg), "list-panes", "-t", target, "-F", "#{window_id} #{pane_pid}"], run)
    if code != 0 or not out:
        return None
    parts = out.splitlines()[0].split()
    if len(parts) != 2 or not parts[0].startswith("@") or not parts[1].isdigit():
        return None
    return Window(parts[0], int(parts[1]))


def session_exists(cfg: Dict[str, Any], run: Runner) -> bool:
    code, _ = _call([_tmux(cfg), "has-session", "-t", f"={tmux_session(cfg)}"], run)
    return code == 0


def handler_dir(cfg: Dict[str, Any]) -> Path:
    return config.state_dir(cfg) / "handler"


def request_path(cfg: Dict[str, Any]) -> Path:
    """``rig sysmon handler spawn`` drops this file; the daemon consumes it on its next tick."""
    return handler_dir(cfg) / "spawn-request"


def _holder_dict(h: Holder) -> Dict[str, Any]:
    return asdict(h)


def _cached_holder(cached: Any) -> Optional[Holder]:
    """The cached reading from ``memory.json``, or ``None`` (a cache miss) when it is not one."""
    if not isinstance(cached, dict) or set(cached) != set(_holder_dict(Holder("unknown"))):
        return None
    if cached["state"] not in _CACHED_STATES:
        return None
    return Holder(**cached)


def current_holder(
    state: Dict[str, Any], now: float, cfg: Dict[str, Any], run: Runner, *, not_before: Optional[float] = None,
) -> Tuple[Holder, Dict[str, Any]]:
    """The one holder cache (shared by delivery and :func:`tick`): ``tg-ctl alert holder`` and, while
    sysmon has a handler, one ``omp-live status`` at most once per ``census_interval_seconds``. The
    omp-live read also registers the handler's omp (``handler_pid``/``handler_session_id``) and its
    ``handler_busy``. ``not_before`` refuses a cached reading older than that moment (the startup
    verdict needs one taken after the deadline). A dry run reads nothing and sees a vacant slot."""
    if cfg["handler"].get("dry_run"):
        return Holder("vacant"), state
    cached = _cached_holder(state.get("holder"))
    last = state.get("last_vacancy_check_at")
    if cached is not None and _num(last) and now - float(last) < float(cfg["census_interval_seconds"]) \
            and (not_before is None or float(last) >= not_before):
        return cached, state
    endpoints = live_endpoints(cfg, run) if state.get("window_id") or state.get("handler_pid") else None
    h = read_holder(cfg, run, endpoints)
    out = {**state, "holder": _holder_dict(h), "last_vacancy_check_at": now}
    if endpoints is not None:
        if not out.get("handler_pid") and out.get("pane_pid"):
            ep = find_handler_endpoint(int(out["pane_pid"]), endpoints, is_descendant)
            if ep is not None:
                out.update(handler_pid=ep["pid"], handler_session_id=str(ep["sessionId"]))
        out["handler_busy"] = omp_busy(out.get("handler_pid"), out.get("handler_session_id"), endpoints)
    return h, out


def observe_send(state: Dict[str, Any], record: Dict[str, Any], now: float) -> Dict[str, Any]:
    """A ``--to-holder`` outcome is a liveness reading: sent → bound (and a delivery: the retire
    clock restarts), vacant → vacant, deferred → suspended. A delivery can never be followed by a
    spawn in the same tick."""
    if record.get("via") != "holder":
        return state
    cached = state.get("holder") if isinstance(state.get("holder"), dict) else {}
    kept = {**_holder_dict(Holder("bound")), **{k: v for k, v in cached.items() if k != "state"}}
    code = record.get("returncode")
    if record.get("outcome") == "sent":
        return {**state, "last_delivery_at": now, "holder": {**kept, "state": "bound"}, "last_vacancy_check_at": now}
    if code == alert.HOLDER_VACANT:
        return {**state, "holder": _holder_dict(Holder("vacant")), "last_vacancy_check_at": now}
    if code == alert.HOLDER_DEFERRED:
        return {**state, "holder": {**kept, "state": "suspended"}, "last_vacancy_check_at": now}
    return state


def spawn(
    cfg: Dict[str, Any], state: Dict[str, Any], now: float, alert_text: str, run: Runner, *, severity: str, why: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Write the brief and open the handler window. ``(record, new state)``; a dry run records the
    argv (``would_spawn``) and changes nothing."""
    brief = handler_dir(cfg) / "brief.md"
    window = f"{tmux_session(cfg)}:{cfg['handler']['window']}"
    record: Dict[str, Any] = {"at": now, "event": "spawn", "why": why, "severity": severity, "window": window}
    if cfg["handler"].get("dry_run"):
        argv = spawn_argv(cfg, brief, kickoff_text(alert_text), session_exists=True)
        record.update(outcome="would_spawn", argv=argv)
        return record, state
    try:
        brief.parent.mkdir(parents=True, exist_ok=True)
        brief.write_text(build_brief(cfg), encoding="utf-8")
    except OSError as exc:
        record.update(outcome="failed", detail=f"brief: {exc}"[:300])
        return record, _spawned(state, now, severity, ok=False)
    argv = spawn_argv(cfg, brief, kickoff_text(alert_text), session_exists=session_exists(cfg, run))
    record["command"] = argv
    code, out = _call(argv, run)
    parts = out.split() if code == 0 else []
    if len(parts) != 2 or not parts[0].startswith("@") or not parts[1].isdigit():
        record.update(outcome="failed", detail=(out or f"exit {code}")[:300])
        return record, _spawned(state, now, severity, ok=False)
    record.update(outcome="spawned", spawned_at=now, window_id=parts[0], pane_pid=int(parts[1]))
    new = _spawned(state, now, severity, ok=None)
    new.update(window_id=parts[0], pane_pid=int(parts[1]), window_created_at=now)
    return record, new


_HANDLER_KEYS = ("window_id", "pane_pid", "window_created_at", "handler_pid", "handler_session_id", "handler_busy")


def _cleared(state: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in state.items() if k not in _HANDLER_KEYS}


def _spawned(state: Dict[str, Any], now: float, severity: str, *, ok: Optional[bool]) -> Dict[str, Any]:
    out = _cleared(state)
    out.update(last_spawn_at=now, last_spawn_ok=ok, spawn_severity=severity, start_failure_notified=False,
               post_crash_pending=False)
    return out


def retire(
    cfg: Dict[str, Any], state: Dict[str, Any], holder: Holder, run: Runner, now: float,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Close the handler window sysmon spawned: release the slot if it still holds it, a final
    note, ``kill-window`` by the stored id. Every step first proves the target: omp-live must list
    ``handler_pid`` with ``handler_session_id`` and the window must still hold the stored pane pid
    as its only pane; otherwise only the state is cleared (nothing is ever killed by name or an
    empty value)."""
    window_id, pane_pid = str(state.get("window_id") or ""), state.get("pane_pid")
    pid, session = state.get("handler_pid"), str(state.get("handler_session_id") or "")
    record: Dict[str, Any] = {"at": now, "event": "retire", "window_id": window_id, "handler_pid": pid}
    busy = omp_busy(pid, session, live_endpoints(cfg, run))
    if busy is None:
        record.update(outcome="skipped", detail=f"omp-live no longer lists pid {pid} with session {session}")
        return record, _cleared(state)
    if busy:
        # it started a turn since the cached reading: not idle after all, try again later
        record.update(outcome="skipped", detail=f"handler pid {pid} is busy")
        return record, {**state, "handler_busy": True}
    # the window must still hold exactly the pane sysmon spawned: a pane the user split off (or a
    # renamed, reused window) is someone's work, and closing the window would take it too
    code, out = _call([_tmux(cfg), "list-panes", "-t", window_id, "-F", "#{pane_pid}"], run) \
        if window_id.startswith("@") else (None, "")
    if code != 0 or not pane_pid or out != str(pane_pid):
        record.update(outcome="skipped", detail=f"window {window_id or '?'} is not the one sysmon spawned")
        return record, _cleared(state)
    holds = holder.state in ("bound", "suspended") and holder.session_id == session
    steps = []
    for argv in retire_argvs(cfg, window_id, int(pid), session, holds, int(now * 1000)):
        step_code, detail = _call(argv, run)
        steps.append({"argv": argv[:4], "ok": step_code == 0, **({} if step_code == 0 else {"detail": detail[:200]})})
    record.update(outcome="retired", steps=steps)
    return record, _cleared(state)


def tick(
    findings: Sequence[Finding], state: Dict[str, Any], now: float, cfg: Dict[str, Any], run: Runner, *,
    alert_text: str,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """One handler round after alerts and the heal ladder: ``(state, records, notices)``. Records go
    to ``recent_handler``; a notice (``{title, body, severity}``) is a Telegram post the daemon makes
    (this module never posts): only ``sysmon handler did not start`` after a CRITICAL spawn."""
    records: List[Dict[str, Any]] = []
    notices: List[Dict[str, Any]] = []
    dry = bool(cfg["handler"].get("dry_run"))
    state = update_first_seen(findings, state, now)
    deadline = None
    if state.get("last_spawn_at") is not None and state.get("last_spawn_ok") is None:
        deadline = float(state["last_spawn_at"]) + _minutes(cfg, "startup_timeout_minutes")
    holder, state = current_holder(state, now, cfg, run,
                                   not_before=deadline if deadline is not None and now >= deadline else None)
    if holder.state != "vacant":
        state = {**state, "post_crash_pending": False}

    if deadline is not None and now >= deadline:
        ok = not startup_failed(state, now, cfg, holder=holder)
        state = {**state, "last_spawn_ok": ok}
        records.append({"at": now, "event": "startup", "ok": ok, "holder": holder.state,
                        "handler_pid": state.get("handler_pid")})
        if not ok:
            state, notice = _start_failure(state, alert_text)
            notices += notice

    requested = not dry and request_path(cfg).exists()
    if requested:
        _unlink(request_path(cfg))
        window = find_window(cfg, run)
        state = observe_window(state, window, now)
        if window is not None:
            records.append({"at": now, "event": "spawn_request", "outcome": "refused: window exists",
                            "window_id": window.window_id})
        else:
            rec, state = spawn(cfg, state, now, alert_text, run, severity=_severity(findings, state), why="requested")
            records.append(rec)
            if rec["outcome"] == "failed":
                state, notice = _start_failure(state, alert_text)
                notices += notice
    else:
        post_crash = bool(state.get("post_crash_pending"))
        due, _ = spawn_due(findings, state, now, cfg, holder=holder, window_exists=False, post_crash=post_crash)
        if due:
            window = None if dry else find_window(cfg, run)
            state = observe_window(state, window, now)
            due, why = spawn_due(findings, state, now, cfg, holder=holder, window_exists=window is not None,
                                 post_crash=post_crash)
            if due:
                rec, state = spawn(cfg, state, now, alert_text, run, severity=_severity(findings, state), why=why)
                records.append(rec)
                if rec["outcome"] == "failed":
                    state, notice = _start_failure(state, alert_text)
                    notices += notice

    if not dry and retire_due(findings, state, now, cfg, handler_busy=state.get("handler_busy")):
        rec, state = retire(cfg, state, holder, run, now)
        records.append(rec)
    return state, records, notices


def _severity(findings: Sequence[Finding], state: Dict[str, Any]) -> str:
    if state.get("post_crash_pending") or any(f.severity == SEVERITY_CRITICAL for f in findings):
        return SEVERITY_CRITICAL
    return SEVERITY_WARNING


def _start_failure(state: Dict[str, Any], alert_text: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """The failed-start notice: once per spawn, and only when a CRITICAL finding triggered it. It
    carries the current alert, which may be waiting in the pending queue for a holder."""
    if state.get("start_failure_notified") or state.get("spawn_severity") != SEVERITY_CRITICAL:
        return state, []
    window = state.get("window_id") or "none"
    body = (f"sysmon raised a handler session for a CRITICAL finding, but nothing holds the sysmon alerts "
            f"(window {html.escape(str(window))}). Its window is left for inspection; critical alerts go to "
            f"Telegram until the next spawn. <code>rig sysmon handler status</code>\n\n"
            f"{html.escape(alert_text)}")
    return {**state, "start_failure_notified": True}, [{"title": START_FAILED_TITLE, "body": body,
                                                        "severity": SEVERITY_CRITICAL}]


def status_lines(cfg: Dict[str, Any], state: Dict[str, Any], holder: Holder, window: Optional[Window]) -> List[str]:
    """``rig sysmon handler status``."""
    who = f" {holder.session_id or '-'} pid {holder.pid if holder.pid is not None else '-'}" \
        if holder.state in ("bound", "suspended") else ""
    name = f"{tmux_session(cfg)}:{cfg['handler']['window']}"
    win = f"{name} {window.window_id} pane {window.pane_pid}" if window is not None else f"{name} absent"
    last = state.get("last_spawn_at")
    ok = {True: "ok", False: "failed", None: "starting"}[state.get("last_spawn_ok")]
    spawned = "never" if last is None else \
        f"{time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(float(last)))} {ok}"
    return [f"holder: {holder.state}{who}", f"window: {win}", f"last spawn: {spawned}"]


def request_wait_s(cfg: Dict[str, Any]) -> float:
    """How long ``rig sysmon handler spawn`` waits for the daemon to open the window."""
    return float(max(30, 2 * int(cfg["sample_interval_seconds"]) + 10))


def _unlink(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
