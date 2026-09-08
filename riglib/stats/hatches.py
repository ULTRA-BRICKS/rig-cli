"""Escape-hatch usage analysis for ``rig stats show``.

Accessed via: ``rig stats show`` → ``command.run`` / ``build_report`` → ``collect_hatches``
→ json/tui/web renderers.

Assumptions: JSONL is best-effort (a bad line is skipped); a missing file is empty, never
an error. ``--home`` relocates ``~/.config/agent-tools/{overrides.log,ship-audit.jsonl}``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .sources.base import parse_iso

_OVERRIDES_NAME = "overrides.log"
_SHIP_AUDIT_NAME = "ship-audit.jsonl"
_AGENT_TOOLS_DIR = Path(".config") / "agent-tools"

# Data-driven broken-gate classifiers. Keep the patterns here, not sprinkled in parsers.
_QUORUM_BROKEN = re.compile(
    r"distinct_models|2 of 3|0/3|quota|weekly limit|rate.?limit|one model",
    re.IGNORECASE,
)
_THIN_BROKEN = re.compile(
    r"subagent|dispatched worker|leaf worker|I am .*sub",
    re.IGNORECASE,
)
_GATE_WRONG = re.compile(
    r"gate is wrong|false positive|incorrectly (?:runs|blocks|counts)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class HatchEvent:
    timestamp: datetime | None
    hook: str
    kind: str  # "broken-gate" | "lazy"
    decision: str
    reason: str
    source: str  # "overrides" | "ship-audit"


@dataclass
class HatchAggregate:
    total: int = 0
    by_hook: dict[str, dict[str, int]] = field(default_factory=dict)
    by_kind: dict[str, int] = field(default_factory=lambda: {"broken-gate": 0, "lazy": 0})

    def to_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "by_kind": {
                "broken-gate": self.by_kind.get("broken-gate", 0),
                "lazy": self.by_kind.get("lazy", 0),
            },
            "by_hook": self.by_hook,
        }


def classify_hatch(
    *,
    hook: str,
    reason: str,
    command: str,
    detail: str,
    models: object = None,
) -> str:
    """Return ``broken-gate`` or ``lazy`` for one hatch event."""
    reason_blob = f"{reason}\n{detail}"
    command_blob = f"{reason}\n{command}"
    if hook == "ship-review-quorum" and (
        _models_is_zero(models) or _QUORUM_BROKEN.search(reason_blob)
    ):
        return "broken-gate"
    if hook == "orchestrator-stays-thin" and _THIN_BROKEN.search(command_blob):
        return "broken-gate"
    if _GATE_WRONG.search(reason_blob):
        return "broken-gate"
    return "lazy"


def collect_hatches(
    home: Path | str | None = None,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> HatchAggregate:
    """Read hatch JSONL under ``home`` and aggregate by hook / kind.

    Missing files yield an empty aggregate. Dedup is only across sources: the same
    ``ts``+hook in both files counts once (``overrides.log`` wins). Rows inside
    ``overrides.log`` are not collapsed, including empty/invalid timestamps.
    """
    root = Path(home) if home is not None else Path.home()
    cfg = root / _AGENT_TOOLS_DIR
    seen: set[tuple[str, str]] = set()
    events: list[HatchEvent] = []

    for ev in _iter_overrides(cfg / _OVERRIDES_NAME):
        seen.add(_dedup_key(ev))
        if _in_window(ev.timestamp, since, until):
            events.append(ev)

    for ev in _iter_ship_audit(cfg / _SHIP_AUDIT_NAME):
        key = _dedup_key(ev)
        if key in seen:
            continue
        seen.add(key)
        if _in_window(ev.timestamp, since, until):
            events.append(ev)

    return aggregate_hatches(events)


def aggregate_hatches(events: list[HatchEvent]) -> HatchAggregate:
    by_hook: dict[str, dict[str, int]] = {}
    by_kind = {"broken-gate": 0, "lazy": 0}
    for ev in events:
        by_kind[ev.kind] = by_kind.get(ev.kind, 0) + 1
        row = by_hook.setdefault(
            ev.hook,
            {"total": 0, "broken_gate": 0, "lazy": 0, "approved": 0, "denied": 0},
        )
        row["total"] += 1
        if ev.kind == "broken-gate":
            row["broken_gate"] += 1
        else:
            row["lazy"] += 1
        decision = ev.decision.lower()
        if "denied" in decision:
            row["denied"] += 1
        elif "approved" in decision:
            row["approved"] += 1
    ordered = dict(sorted(by_hook.items(), key=lambda kv: (-kv[1]["total"], kv[0])))
    return HatchAggregate(total=len(events), by_hook=ordered, by_kind=by_kind)


def _dedup_key(ev: HatchEvent) -> tuple[str, str]:
    ts = ev.timestamp.isoformat() if ev.timestamp is not None else ""
    return (ts, ev.hook)


def _in_window(
    ts: datetime | None,
    since: datetime | None,
    until: datetime | None,
) -> bool:
    if since is None and until is None:
        return True
    if ts is None:
        return False
    if since is not None and ts < since:
        return False
    if until is not None and ts > until:
        return False
    return True


def _models_is_zero(models: object) -> bool:
    if isinstance(models, bool) or models is None:
        return False
    if isinstance(models, (int, float)) and models == 0:
        return True
    if isinstance(models, str) and models.strip() == "0":
        return True
    return False


def _text(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    rows: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def _iter_overrides(path: Path) -> list[HatchEvent]:
    events: list[HatchEvent] = []
    for obj in _read_jsonl(path):
        hook = _text(obj.get("hatch")).strip()
        if not hook:
            continue
        reason = _text(obj.get("reason"))
        command = _text(obj.get("command"))
        detail = _text(obj.get("detail"))
        decision = _text(obj.get("decision"))
        ts = parse_iso(_text(obj.get("ts")) or None)
        kind = classify_hatch(
            hook=hook,
            reason=reason,
            command=command,
            detail=detail,
            models=obj.get("models"),
        )
        events.append(
            HatchEvent(
                timestamp=ts,
                hook=hook,
                kind=kind,
                decision=decision,
                reason=reason,
                source="overrides",
            )
        )
    return events


def _iter_ship_audit(path: Path) -> list[HatchEvent]:
    events: list[HatchEvent] = []
    for obj in _read_jsonl(path):
        decision = _text(obj.get("decision"))
        if "bypass:" not in decision:
            continue
        if "external-review" in decision:
            hook = "ship-external-review"
        else:
            hook = "ship-review-quorum"
        reason = _text(obj.get("override_reason") or obj.get("reason"))
        detail = _text(obj.get("detail"))
        ts = parse_iso(_text(obj.get("ts")) or None)
        kind = classify_hatch(
            hook=hook,
            reason=reason,
            command="",
            detail=detail,
            models=obj.get("models"),
        )
        events.append(
            HatchEvent(
                timestamp=ts,
                hook=hook,
                kind=kind,
                decision=decision,
                reason=reason,
                source="ship-audit",
            )
        )
    return events
