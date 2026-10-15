"""Alerts — to Telegram through tg-cli or into the live agent sessions — rate-limited and deduplicated.

The monitor must be loud about a dangerous state and quiet otherwise, so :func:`decide` is a
pure function over persisted state:

- a finding key alerts when it first reaches ``min_severity`` (default ``critical``), again
  when its severity ESCALATES, and otherwise at most once per ``cooldown_minutes`` (a SWAP
  finding: ``swap_cooldown_minutes``, default 2 h). The last send per key and level survives the
  key clearing, so a finding that clears and comes back inside its window is not new
  (rig-cli#699: swap hugging its line re-paged every 5-20 min as "new");
- a DISK finding (kind ``disk``: a full data or swap volume stays full for days, until someone
  cleans up) alerts at most once per ``disk_cooldown_minutes`` (default 6 h) per key AND severity
  level. That memory survives the key clearing, so a volume hovering at its threshold cannot
  re-page as "new" either; a level not sent within the window, up or down, still alerts (with
  the default ``min_severity`` critical: one page per 6 h);
- every due finding of one tick goes into ONE message, and at most ``max_per_hour`` messages
  leave per rolling hour (an escalation to critical is still held to that budget: a storm of
  flapping findings must never become a storm of Telegram messages);
- a key that was alerted and then clears produces one "resolved" line (batched too) — never with
  ``route: agents``: the note asks nothing of anyone, and broadcasting it starts a turn in every
  live session (rig-cli#699); the problem note already tells agents to check
  ``rig sysmon sample`` themselves instead of waiting for one.

Delivery is ``tg-ctl alert send --source sysmon`` (tg-cli#636) with an HTML body: tg-ctl posts
the message to Telegram — resolved notices with ``--tag report``, alerts with ``--tag
problem`` — naming in a footer every omp session that subscribed to alerts, and delivers the
alert with its Telegram message id into those sessions. An older tg-ctl without that
subcommand, or none on PATH, falls back to the same ``tg`` argv as before. With ``alert.route:
agents`` (rig-cli#696) the alert skips Telegram: a plain-text note goes into every live omp
session through ``tg-ctl omp-live`` (mem-sentinel's broadcast path), and only a CRITICAL alert
that no session took still posts to Telegram (a warning nobody took is dropped, its reason
recorded: Telegram only when no agent can handle it). The dedup state lives in ``memory.json`` so a
daemon restart never re-sends what already went out.
"""

from __future__ import annotations

import errno
import hashlib
import html
import json
import os
import re
import socket
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .config import DEFAULTS
from .model import SEVERITY_CRITICAL, SEVERITY_INFO, SEVERITY_RANK, SEVERITY_WARNING, Finding, Offender
from .probe import pid_alive

HOUR = 3600.0
RESOLVE_AFTER_S = 300.0
DISK_KIND = "disk"
SWAP_KIND = "swap"
#: the title prefix of a ``rig sysmon test-alert`` smoke test
TEST_PREFIX = "TEST — "


def decide(
    findings: Sequence[Finding], state: Dict[str, Any], cfg: Dict[str, Any], now: float
) -> Tuple[List[Finding], List[str], Dict[str, Any]]:
    """``(due, resolved_keys, new_state)``. ``due`` is empty when the hourly budget is spent."""
    a = cfg["alert"]
    min_rank = SEVERITY_RANK.get(str(a.get("min_severity") or "critical"), 2)
    cooldown = float(a.get("cooldown_minutes") or 0) * 60.0
    # Missing or 0 falls back to the default: a zero window would forget every disk page and
    # re-page each tick (the validator already rejects values under 1).
    disk_cooldown = float(a.get("disk_cooldown_minutes") or DEFAULTS["alert"]["disk_cooldown_minutes"]) * 60.0
    swap_cooldown = float(a.get("swap_cooldown_minutes") or DEFAULTS["alert"]["swap_cooldown_minutes"]) * 60.0
    keys: Dict[str, Dict[str, Any]] = {k: dict(v) for k, v in (state.get("keys") or {}).items()}
    sent = [t for t in state.get("sent") or [] if now - float(t) < HOUR]
    # key -> {severity: last sent}; entries older than the window are forgotten.
    disk_sent = _fresh_levels(state.get("disk_sent"), now, disk_cooldown)
    # The same for every other key. It outlives the key clearing (keys[] does not), so a finding
    # that clears and comes back inside its window is no new alert (rig-cli#699). keys[] holds the
    # last send of every alerted key as well, which seeds a memory.json saved before level_sent.
    saved = {k: dict(v) for k, v in (state.get("level_sent") or {}).items()}
    for k, entry in keys.items():
        if k not in disk_sent and entry.get("severity") in SEVERITY_RANK and entry.get("last_sent") is not None:
            saved.setdefault(k, {}).setdefault(entry["severity"], float(entry["last_sent"]))
    level_sent = _fresh_levels(saved, now, max(cooldown, swap_cooldown))

    due: List[Finding] = []
    present = set()
    for f in findings:
        if SEVERITY_RANK[f.severity] < min_rank:
            continue
        present.add(f.key)
        if f.kind == DISK_KIND:
            if f.severity not in disk_sent.get(f.key, {}):
                due.append(f)
            continue
        # the last send at this level or above: an escalation finds none and alerts at once
        last = max((t for sev, t in level_sent.get(f.key, {}).items()
                    if SEVERITY_RANK[sev] >= SEVERITY_RANK[f.severity]), default=None)
        if last is None or now - last >= (swap_cooldown if f.kind == SWAP_KIND else cooldown):
            due.append(f)

    # Hysteresis: a key must stay clear for RESOLVE_AFTER_S before it counts as resolved, so a
    # value flapping around a threshold neither re-alerts as "new" nor spams resolved lines.
    gone: List[str] = []
    for k, entry in keys.items():
        if k in present:
            entry.pop("clear_since", None)
            continue
        entry.setdefault("clear_since", now)
        if now - float(entry["clear_since"]) >= RESOLVE_AFTER_S:
            gone.append(k)
    # A resolved note asks nothing of anyone; with route agents it would only start a turn in
    # every live session (rig-cli#699).
    resolved = gone if a.get("resolved", True) and a.get("route") != "agents" else []
    for k in gone:
        del keys[k]

    budget_left = int(a.get("max_per_hour") or 0) - len(sent)
    if due and budget_left < 1:
        # Held, not dropped: forget their dedup entry so they are due again once budget frees up.
        for f in due:
            keys.pop(f.key, None)
        due = []
    if resolved and budget_left - (1 if due else 0) < 1:
        resolved = []  # "resolved" lines are informational; never spend a problem alert's budget
    for f in due:
        # A disk key is deduped by disk_sent above; its keys[] entry only feeds the resolve loop.
        keys[f.key] = {"severity": f.severity, "last_sent": now}
        (disk_sent if f.kind == DISK_KIND else level_sent).setdefault(f.key, {})[f.severity] = now
    sent.extend([now] * ((1 if due else 0) + (1 if resolved else 0)))
    return due, resolved, {"keys": keys, "sent": sent, "disk_sent": disk_sent, "level_sent": level_sent}


def refund(state: Dict[str, Any], at: float) -> Dict[str, Any]:
    """``state`` with the hourly-budget slot :func:`decide` counted at ``at`` given back: that
    message reached nobody (a warning no agent session took is dropped), so it must not hold back
    a later critical's Telegram fallback. Its dedup stays — no re-send every tick."""
    sent = list(state.get("sent") or [])
    if at in sent:
        sent.remove(at)
    return {**state, "sent": sent}


def _fresh_levels(
    saved: Optional[Dict[str, Dict[str, float]]], now: float, window: float,
) -> Dict[str, Dict[str, float]]:
    """``key -> {severity: last sent}`` without the sends older than ``window``."""
    out: Dict[str, Dict[str, float]] = {}
    for k, levels in (saved or {}).items():
        fresh = {sev: float(t) for sev, t in levels.items() if now - float(t) < window}
        if fresh:
            out[k] = fresh
    return out


def _who(o: Offender) -> str:
    a = o.attribution
    if a.host is not None:
        where = a.host.pane or a.pane or "no pane"
        sess = f" session {a.host.session_id[:8]}" if a.host.session_id else ""
        return f"{a.host.kind} pid {a.host.pid} ({where}{sess}, {a.host.cwd or '?'})"
    if a.orphan:
        return f"ORPHAN (its agent host {a.dead_owner} died)"
    return a.pane or "unattributed"


def render(
    findings: Sequence[Finding],
    *,
    incident_id: Optional[str],
    extra_lines: Sequence[str] = (),
    test: bool = False,
    host: Optional[str] = None,
) -> Tuple[str, str]:
    """``(title, html_body)`` for a batch of findings."""
    host = host or socket.gethostname().split(".")[0]
    worst = worst_severity(findings)
    prefix = TEST_PREFIX if test else ""
    title = f"{prefix}sysmon {worst.upper()} on {host}: " + "; ".join(f.key for f in findings)[:120]
    lines: List[str] = []
    if test:
        lines.append("<b>TEST MESSAGE</b> — forced-condition smoke test of rig sysmon alerting; "
                     "no real incident, nothing needs to be done.")
    for f in findings:
        lines.append(f"<b>{html.escape(f.severity.upper())}</b> {html.escape(f.summary)}")
        by_owner: Dict[str, List[Offender]] = {}
        for o in f.offenders:
            by_owner.setdefault(_who(o), []).append(o)
        for who, offs in list(by_owner.items())[:4]:
            names = ", ".join(sorted({o.comm for o in offs}))[:80]
            rss = sum(o.rss_kb for o in offs) / 1024
            lines.append(f"  • {html.escape(who)}: {len(offs)} proc ({html.escape(names)}), {rss:.0f} MB")
    lines.extend(html.escape(x) for x in extra_lines)
    if incident_id:
        lines.append(f"incident <code>{html.escape(incident_id)}</code> — "
                     f"<code>rig sysmon incidents --show {html.escape(incident_id)}</code>")
    return title, "\n".join(lines)


def render_resolved(keys: Sequence[str], host: Optional[str] = None) -> Tuple[str, str]:
    host = host or socket.gethostname().split(".")[0]
    return (f"sysmon resolved on {host}", "cleared: " + ", ".join(html.escape(k) for k in keys))


Runner = Callable[..., "subprocess.CompletedProcess[str]"]


#: ``tg-ctl alert`` severities: the model's warning/critical plus ``info`` for a resolved notice.
SEVERITIES = (SEVERITY_INFO, SEVERITY_WARNING, SEVERITY_CRITICAL)
_OK_LINE = re.compile(r"^OK tg#(\d+)", re.M)
KEY_MAX = 120


def worst_severity(findings: Sequence[Finding]) -> str:
    return max((f.severity for f in findings), key=lambda s: SEVERITY_RANK[s], default=SEVERITY_WARNING)


def batch_key(keys: Sequence[str], prefix: str = "") -> str:
    """The tg-ctl throttle key of a batch: ``prefix`` + its finding keys, order-independent. A
    long list keeps a readable head plus a digest of the whole, so two batches never collide
    by sharing a prefix (bounded at :data:`KEY_MAX`)."""
    full = prefix + ";".join(sorted(keys))
    if len(full) <= KEY_MAX:
        return full
    digest = hashlib.sha1(full.encode("utf-8")).hexdigest()[:12]
    return f"{full[:KEY_MAX - 13]}-{digest}"


def tg_ctl_argv(tg_ctl: str, title: str, body: str, severity: str, key: str) -> List[str]:
    return [tg_ctl, "alert", "send", "--source", "sysmon", "--severity", severity, "--key", key,
            "--title", title, "--html", "--text", body]


def tg_argv(tg: str, title: str, body: str, severity: str) -> List[str]:
    tag = "report" if severity == SEVERITY_INFO else "problem"
    return [tg, "--tag", tag, "--subagent", "sysmon", "--title", title, "--format", "html", body]


def flag_value(argv: Sequence[str], flag: str) -> Optional[str]:
    """The value after ``flag`` in an argv (tests and log readers; no magic indexes)."""
    for i, a in enumerate(argv[:-1]):
        if a == flag:
            return argv[i + 1]
    return None


def unsupported_alert_subcommand(proc: "subprocess.CompletedProcess[str]") -> bool:
    """A tg-ctl from before tg-cli#636 rejects ``alert`` with its usage text on stderr and
    exit 1 — the ONLY shape that may fall back to ``tg``. Every other failure (a crash, a
    signal, a timeout, a new tg-ctl reporting a failed post) may already have posted. The
    recognized shape is pinned by ``OLD_TG_CTL_USAGE`` in tests/test_sysmon_daemon.py."""
    err = (proc.stderr or "").lstrip()
    return (proc.returncode == 1 and err[:6].lower() == "usage:"
            and "tg-ctl " in err and "tg-ctl alert" not in err)


#: ``alert.route``: ``telegram`` posts through tg-ctl (the human chat); ``agents`` broadcasts
#: into every live omp session and keeps Telegram only as the fallback (rig-cli#696).
ROUTES = ("telegram", "agents")
#: What the agents are asked to do with a problem alert (mem-sentinel's broadcast says the same).
#: It never waits on a "resolved" note — that one may be budget-held, disabled or undelivered.
AGENT_ACTION = ("Every session: if your processes are listed above, stop or shrink them now (test suites, "
                "type checkers, builds, stray children you started); otherwise hold off on new heavy work "
                "while the pressure lasts — check it yourself with `rig sysmon sample`, not by waiting "
                "for a resolved note.")
_TAG = re.compile(r"<[^>]+>")


def agent_text(title: str, body: str, severity: str) -> str:
    """The plain-text note an agent session receives: the HTML body without its markup, plus
    the instruction line for a real problem alert (a resolved notice or a ``test-alert`` smoke
    test asks for nothing)."""
    lines = [f"[sysmon] {title}", html.unescape(_TAG.sub("", body))]
    if severity != SEVERITY_INFO and not title.startswith(TEST_PREFIX):
        lines.append(AGENT_ACTION)
    return "\n".join(lines)


def send(
    title: str, body: str, cfg: Dict[str, Any], *, severity: str, key: str, run: Runner = subprocess.run,
) -> Dict[str, Any]:
    """Deliver one alert along ``alert.route``. ``alert.dry_run`` records the message without
    sending it. ``severity`` (one of :data:`SEVERITIES`; ``info`` posts as a report, the rest as
    a problem) and ``key`` identify the alert to tg-ctl's throttle.

    - ``telegram``: ``tg-ctl alert send`` (Telegram + subscribed agent sessions), else — only for
      a tg-ctl that does not know the subcommand, or none installed — ``tg``;
    - ``agents``: the note goes into every live omp session (:func:`_to_agents`). A CRITICAL alert
      that no session took falls back to the ``telegram`` route — a dangerous state must never
      go silent; a warning or resolved notice nobody took is dropped (the owner's rule:
      Telegram only when no agent can handle it). Either way ``undelivered`` says why."""
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {SEVERITIES}, not {severity!r}")
    a = cfg["alert"]
    route = str(a.get("route") or "telegram")
    if route not in ROUTES:
        raise ValueError(f"alert.route must be one of {ROUTES}, not {route!r}")
    record: Dict[str, Any] = {"at": time.time(), "title": title, "severity": severity, "key": key, "message_id": None}
    if route == "agents":
        tg_ctl = str(a.get("tg_ctl_bin") or DEFAULTS["alert"]["tg_ctl_bin"])
        text = agent_text(title, body, severity)
        record.update(via="agents")
        if a.get("dry_run"):
            record.update(outcome="dry_run", text=text)
            return record
        delivered, failed, why = _to_agents(text, tg_ctl, run)
        record.update(delivered=delivered, failed=failed)
        if delivered:
            record.update(outcome="sent")
            return record
        # its own key: the Telegram leg may still record a tg-ctl → tg ``fallback_reason``
        record.update(undelivered=f"no live agent session took it: {why or '; '.join(failed)}"[:300])
        if severity != SEVERITY_CRITICAL:
            record.update(outcome="undelivered")
            return record
    return _to_telegram(title, body, a, severity, key, run, record)


#: The broadcast runs inside the sampling tick, BEFORE the heal ladder: each send is bounded and
#: they run side by side, so stalled endpoints delay mitigation by one timeout per
#: ``AGENT_SEND_WORKERS`` sessions, not one per session. The cap stays small on purpose: each
#: send is a tg-ctl process, and the alert usually fires while memory is already short.
AGENT_SEND_TIMEOUT_S = 20
AGENT_SEND_WORKERS = 8


#: tg-ctl's omp-live registry: ``<pid>-<instanceId>.json`` per endpoint an omp session published
_REGISTRY_ENTRY = re.compile(r"^(\d+)-[0-9a-f]{32}\.json$")


def _registry_note() -> str:
    """Why an empty ``omp-live status`` may be wrong, for the alert record: which still-running
    omp pids the registry holds endpoints of (tg-ctl drops one whose status probe fails with
    anything but a timeout). On 2026-10-15 21:28 it listed nobody while 3 omp sessions ran."""
    root = os.path.join(os.environ.get("TG_CTL_CONFIG_DIR") or os.path.expanduser("~/.config/tg-cli"), "omp-live")
    try:
        names = os.listdir(root)
    except OSError as exc:
        return f" (registry {root} unreadable: {exc.strerror})"
    live = sorted(p for p in {int(m.group(1)) for m in map(_REGISTRY_ENTRY.match, names) if m} if pid_alive(p))
    if not live:
        return f" (registry {root} holds no endpoint of a running omp)"
    return f" (registry {root} holds endpoints of running omp pids {live}: tg-ctl's probe dropped them)"


def _to_agents(text: str, tg_ctl: str, run: Runner) -> Tuple[List[int], List[str], str]:
    """``(delivered_pids, failures, why_none)``: ``text`` sent into every session ``tg-ctl
    omp-live status`` lists, the same broadcast mem-sentinel's ``omp-steer all`` does (tg-ctl
    steers a busy session and starts a turn in an idle one). ``--session`` pins each send to
    the session it was listed with; ``--`` keeps a text that starts with a dash an operand."""
    proc, detail = _run([tg_ctl, "omp-live", "status", "--json"], run, timeout=AGENT_SEND_TIMEOUT_S)
    if proc is None or proc.returncode != 0:
        return [], [], f"omp-live status: {detail or (proc.stderr if proc else '') or 'failed'}".strip()[:300]
    try:
        listed = json.loads(proc.stdout or "[]")
    except ValueError:
        return [], [], "omp-live status: unreadable JSON"
    # a malformed listing must never raise out of the sampling loop: skip what is not a session
    hosts = [h for h in listed if isinstance(h, dict) and _is_pid(h.get("pid"))] if isinstance(listed, list) else []
    if not hosts:
        return [], [], "omp-live status lists no session" + _registry_note()

    def one(h: Dict[str, Any]) -> Tuple[int, Optional[str]]:
        pid = int(h["pid"])
        argv = [tg_ctl, "omp-live", "send", str(pid), "--request-id", f"sysmon-{pid}-{uuid.uuid4().hex[:12]}"]
        if h.get("sessionId"):
            argv += ["--session", str(h["sessionId"])]
        sent, why = _run(argv + ["--", text], run, timeout=AGENT_SEND_TIMEOUT_S)
        if sent is not None and sent.returncode == 0:
            return pid, None
        return pid, f"{pid}: {(why or (sent.stderr if sent else '') or 'failed').strip()}"[:200]

    try:
        with ThreadPoolExecutor(max_workers=min(AGENT_SEND_WORKERS, len(hosts))) as pool:
            results = list(pool.map(one, hosts))  # map keeps the listing order
    except RuntimeError as exc:
        # "can't start new thread" during the very process storm being alerted: the caller
        # falls back to Telegram rather than letting it kill the monitor
        return [], [], f"broadcast could not start: {exc}"[:300]
    return [pid for pid, err in results if err is None], [err for _, err in results if err is not None], ""


def _is_pid(value: Any) -> bool:
    # `omp-live status` lists pids as JSON numbers; anything else is not a session to address
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value < 1 << 32


def _to_telegram(
    title: str, body: str, a: Dict[str, Any], severity: str, key: str, run: Runner, record: Dict[str, Any],
) -> Dict[str, Any]:
    ctl_argv = tg_ctl_argv(str(a.get("tg_ctl_bin") or DEFAULTS["alert"]["tg_ctl_bin"]), title, body, severity, key)
    fallback_argv = tg_argv(str(a.get("tg_bin") or DEFAULTS["alert"]["tg_bin"]), title, body, severity)
    record.update(tag=flag_value(fallback_argv, "--tag"), via="tg-ctl alert")
    if a.get("dry_run"):
        record.update(outcome="dry_run", argv=ctl_argv, fallback_argv=fallback_argv)
        return record
    proc, detail = _run(ctl_argv, run)
    if proc is None or unsupported_alert_subcommand(proc):
        record.update(via="tg", fallback_reason="tg-ctl not installed" if proc is None else "tg-ctl without `alert`")
        proc, detail = _run(fallback_argv, run)
    if proc is None:
        record.update(outcome="error", detail=detail)
        return record
    out = proc.stdout or ""
    m = _OK_LINE.search(out)
    record.update(
        outcome="sent" if proc.returncode == 0 else "error",
        detail=(out or proc.stderr or "").strip()[-300:],
        returncode=proc.returncode,
        message_id=int(m.group(1)) if m else None,
    )
    return record


#: errno values proving the binary never ran — ``exec`` itself refused it (not on PATH, not
#: executable, a directory, not a program); anything else may have run and posted.
_NEVER_RAN = frozenset({errno.ENOENT, errno.EACCES, errno.ENOTDIR, errno.ENOEXEC})


def _run(
    argv: Sequence[str], run: Runner, timeout: float = 60,
) -> Tuple[Optional["subprocess.CompletedProcess[str]"], str]:
    """``(proc, detail)``: ``proc`` is ``None`` only when the binary provably never started
    (:data:`_NEVER_RAN`); a timeout or any other failure is a non-zero completed process
    (it may have posted)."""
    try:
        return run(list(argv), capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL), ""
    except OSError as exc:
        if isinstance(exc, FileNotFoundError) or exc.errno in _NEVER_RAN:
            return None, f"{argv[0]}: cannot run ({exc})"[:300]
        return subprocess.CompletedProcess(list(argv), 1, "", str(exc)[:300]), ""
    except subprocess.SubprocessError as exc:
        return subprocess.CompletedProcess(list(argv), 1, "", str(exc)[:300]), ""
