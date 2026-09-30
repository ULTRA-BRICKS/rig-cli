"""Telegram alerts through tg-cli, rate-limited and deduplicated.

The monitor must be loud about a dangerous state and quiet otherwise, so :func:`decide` is a
pure function over persisted state:

- a finding key alerts when it first reaches ``min_severity`` (default ``critical``), again
  when its severity ESCALATES, and otherwise at most once per ``cooldown_minutes``;
- a DISK finding (kind ``disk``: a full data or swap volume stays full for days, until someone
  cleans up) alerts at most once per ``disk_cooldown_minutes`` (default 6 h) per key AND severity
  level. That memory survives the key clearing, so a volume hovering at its threshold cannot
  re-page as "new" either; a level not sent within the window, up or down, still alerts (with
  the default ``min_severity`` critical: one page per 6 h);
- every due finding of one tick goes into ONE message, and at most ``max_per_hour`` messages
  leave per rolling hour (an escalation to critical is still held to that budget: a storm of
  flapping findings must never become a storm of Telegram messages);
- a key that was alerted and then clears produces one "resolved" line (batched too).

Delivery is ``tg --tag problem`` (resolved: ``--tag report``) with an HTML body; the dedup
state lives in ``memory.json`` so a daemon restart never re-sends what already went out.
"""

from __future__ import annotations

import html
import socket
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .config import DEFAULTS
from .model import SEVERITY_RANK, Finding, Offender

HOUR = 3600.0
RESOLVE_AFTER_S = 300.0
DISK_KIND = "disk"


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
    keys: Dict[str, Dict[str, Any]] = {k: dict(v) for k, v in (state.get("keys") or {}).items()}
    sent = [t for t in state.get("sent") or [] if now - float(t) < HOUR]
    # key -> {severity: last sent}; entries older than the window are forgotten.
    disk_sent: Dict[str, Dict[str, float]] = {}
    for k, levels in (state.get("disk_sent") or {}).items():
        fresh = {sev: float(t) for sev, t in levels.items() if now - float(t) < disk_cooldown}
        if fresh:
            disk_sent[k] = fresh

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
        prev = keys.get(f.key)
        escalated = prev is not None and SEVERITY_RANK[f.severity] > SEVERITY_RANK.get(prev.get("severity"), 0)
        if prev is None or escalated or now - float(prev.get("last_sent") or 0) >= cooldown:
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
    resolved = gone if a.get("resolved", True) else []
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
        if f.kind == DISK_KIND:
            disk_sent.setdefault(f.key, {})[f.severity] = now
    sent.extend([now] * ((1 if due else 0) + (1 if resolved else 0)))
    return due, resolved, {"keys": keys, "sent": sent, "disk_sent": disk_sent}


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
    worst = max((f.severity for f in findings), key=lambda s: SEVERITY_RANK[s], default="warning")
    prefix = "TEST — " if test else ""
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


def send(
    title: str, body: str, cfg: Dict[str, Any], *, tag: str = "problem", run: Runner = subprocess.run
) -> Dict[str, Any]:
    """Deliver through ``tg``. ``alert.dry_run`` records the message without sending it."""
    a = cfg["alert"]
    argv = [str(a.get("tg_bin") or "tg"), "--tag", tag, "--subagent", "sysmon",
            "--title", title, "--format", "html", body]
    record: Dict[str, Any] = {"at": time.time(), "title": title, "tag": tag}
    if a.get("dry_run"):
        record.update(outcome="dry_run", argv=argv)
        return record
    try:
        proc = run(argv, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        record.update(outcome="error", detail=str(exc)[:300])
        return record
    record.update(
        outcome="sent" if proc.returncode == 0 else "error",
        detail=(proc.stdout or proc.stderr or "").strip()[-300:],
        returncode=proc.returncode,
    )
    return record
