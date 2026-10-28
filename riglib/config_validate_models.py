"""Validators for the ``models`` (freshness schedule) and ``agents_md`` blocks, plus ``parse_hhmm``.

Kept apart because ``parse_hhmm`` is shared with the plan builder (``plan_system``), which must not
pull in the rest of the block validators just to parse a time.
"""

from __future__ import annotations

import re
from typing import Any

from .config_checks import reject_unknown_keys
from .config_errors import ConfigError

# The model-freshness schedule defaults to NOON (run once a day, at noon). A `time:` override
# is "HH:MM" 24h. Keeping the parse here (not just in plan/actions) makes a malformed time a
# fail-closed config error, consistent with every other block.
DEFAULT_SCHEDULE_TIME = "12:00"


def parse_hhmm(value: str) -> tuple[int, int]:
    """Parse an "HH:MM" 24-hour time → (hour, minute). Raises ConfigError on a bad value.

    Shared by config validation and the plan builder so the accepted format never drifts.
    """
    parts = str(value).split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ConfigError(
            f"models.schedule.time must be 'HH:MM', got {value!r}",
            fix="use a 24-hour time like '12:00'",
            schema_path="models.schedule.time",
        )
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ConfigError(
            f"models.schedule.time out of range (00:00–23:59), got {value!r}",
            schema_path="models.schedule.time",
        )
    return hour, minute


def _validate_schedule_label(label: Any) -> None:
    if label is None:
        return
    if not isinstance(label, str):
        raise ConfigError(
            f"models.schedule.label must be a string, got {label!r}",
            schema_path="models.schedule.label",
        )
    # the label is the plist / systemd unit FILE name and the `systemctl` unit argument, so it
    # must be a plain unit-name token: no `/` or `..` that would escape the unit dir, and no
    # leading `-` that systemctl would parse as an option.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:_.-]*", label) or ".." in label:
        raise ConfigError(
            f"models.schedule.label must match [A-Za-z0-9][A-Za-z0-9:_.-]* without '..', got {label!r}",
            schema_path="models.schedule.label",
        )


def _validate_schedule(schedule: Any) -> None:
    if not isinstance(schedule, dict):
        raise ConfigError("models.schedule must be a mapping", schema_path="models.schedule")
    reject_unknown_keys(schedule, "models.schedule")
    if "time" in schedule:
        parse_hhmm(schedule["time"])  # fail-closed on a bad time
    _validate_schedule_label(schedule.get("label"))


def validate_models(m: dict[str, Any]) -> None:
    """Validate the ``models`` block — the model-freshness checker's daily schedule.

    The block provisions a daily cron (launchd on macOS, a systemd --user timer on Linux) that runs the
    agent-tools checker. Fail-closed on: a non-mapping block, a non-bool ``enabled``, a
    malformed ``schedule.time`` (must be ``HH:MM``), and unknown keys (typo guard). An
    EMPTY/absent block means "no schedule provisioned" — rig leaves the system cron alone.
    """
    if not isinstance(m, dict):
        raise ConfigError("models must be a mapping", schema_path="models")
    if not m:
        return
    reject_unknown_keys(m, "models")
    enabled = m.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise ConfigError(f"models.enabled must be a bool, got {enabled!r}", schema_path="models.enabled")
    checker_path = m.get("checker_path")
    if checker_path is not None and not isinstance(checker_path, str):
        raise ConfigError(
            f"models.checker_path must be a string, got {checker_path!r}",
            schema_path="models.checker_path",
        )
    _validate_schedule(m.get("schedule", {}))


def validate_agents_md(am: dict[str, Any]) -> None:
    """Validate the ``agents_md`` block — AGENTS.md/CLAUDE.md canonical+symlink provisioning.

    Default **ON**: every repo gets one canonical agent-guide file (``AGENTS.md``) plus a
    ``CLAUDE.md`` symlink to it, so every harness reads the same instructions. Opt out with
    ``agents_md: { enabled: false }`` (or the equivalent ``{ symlink: false }``). An absent
    block means "provision it" — there is nothing to validate then. Fail-closed on non-bool
    knobs and unknown keys (typo guard), consistent with every other block.
    """
    if not isinstance(am, dict):
        raise ConfigError("agents_md must be a mapping", schema_path="agents_md")
    if not am:
        return
    reject_unknown_keys(am, "agents_md")
    for knob in ("enabled", "symlink"):
        value = am.get(knob)
        if value is not None and not isinstance(value, bool):
            raise ConfigError(
                f"agents_md.{knob} must be a bool, got {value!r}",
                schema_path=f"agents_md.{knob}",
            )
