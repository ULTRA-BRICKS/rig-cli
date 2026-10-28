"""Validation of the ``sysmon`` config block (the rig-managed resource monitor, rig-cli#622).

Separate from ``config.py`` because the block has dozens of leaf keys; they are checked from small
tables (one row per key) so adding a key means adding a table row, not another branch.
"""

from __future__ import annotations

from typing import Any

from .config_checks import is_int, reject_unknown_keys
from .config_errors import ConfigError

#: metrics where a LOWER value is worse (warn must sit ABOVE critical) — mirrors
#: riglib.sysmon_daemon.detect.LOWER_IS_WORSE.
SYSMON_LOWER_IS_WORSE = {"reclaimable_gb", "free_gb", "disk_free_gb", "swap_gate_available_pct"}
SYSMON_BOOLS = {
    "alert": ("enabled", "resolved", "dry_run"),
    "heal": ("enabled", "notify_agent", "throttle", "kill_orphans", "kill_owned", "dry_run", "restart_daemons"),
    "remediation": ("dry_run",),
    "handler": ("dry_run",),
    "sessions": ("restore_on_boot",),
}
SYSMON_INTS = {
    "alert": {"cooldown_minutes": 1, "disk_cooldown_minutes": 1, "swap_cooldown_minutes": 1, "max_per_hour": 1},
    "heal": {"grace_seconds": 0, "restart_cooldown_minutes": 1, "max_restarts_per_day": 1},
    "remediation": {"cooldown_minutes": 1},
    "handler": {"warn_persist_minutes": 1, "spawn_cooldown_minutes": 1, "startup_timeout_minutes": 1,
                "idle_exit_minutes": 1},
    "gpu": {"slots": 1},
    "reclaim": {"worktree_age_days": 1, "max_candidates": 1},
}
SYSMON_STRS = {"alert": ("tg_ctl_bin", "tg_bin"), "handler": ("window", "workdir"), "sessions": ("tmux_session",),
               "gpu": ("slot_dir",)}
#: lists of non-empty strings (an empty list turns that candidate source off)
SYSMON_STR_LISTS = {"reclaim": ("worktree_roots", "cache_dirs")}

_TOP_BOOLS = ("enabled", "events", "auto_remediate")
_TOP_INTS = (("sample_interval_seconds", 1), ("census_interval_seconds", 5), ("snapshot_interval_seconds", 30))
_TOP_STRS = ("label", "config_dir", "python_bin")
_SECTIONS = ("thresholds", "alert", "heal", "remediation", "handler", "sessions", "gpu", "reclaim")
_REMEDIATION_AGENTS = ("omp", "claude-code", "codex", "opencode")


def sysmon_fail(path: str, message: str) -> None:
    raise ConfigError(message, schema_path=f"sysmon.{path}" if path else "sysmon")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _section(t: dict[str, Any], name: str) -> dict[str, Any]:
    return t.get(name) or {}


def _check_top_flags_and_ints(t: dict[str, Any]) -> None:
    for key in _TOP_BOOLS:
        if key in t and not isinstance(t[key], bool):
            sysmon_fail(key, f"sysmon.{key} must be a bool, got {t[key]!r}")
    for key, low in _TOP_INTS:
        if key in t and (not is_int(t[key]) or t[key] < low):
            sysmon_fail(key, f"sysmon.{key} must be an int >= {low}, got {t[key]!r}")


def _check_top_scalars(t: dict[str, Any]) -> None:
    _check_top_flags_and_ints(t)
    if "hysteresis_pct" in t:
        margin = t["hysteresis_pct"]
        if not _is_number(margin) or not 0 <= margin <= 50:
            sysmon_fail("hysteresis_pct", f"sysmon.hysteresis_pct must be a number 0-50, got {margin!r}")
    for key in _TOP_STRS:
        if key in t and not _is_nonempty_str(t[key]):
            sysmon_fail(key, f"sysmon.{key} must be a non-empty string, got {t[key]!r}")


def _check_section_shapes(t: dict[str, Any]) -> None:
    for section in _SECTIONS:
        if section in t and not isinstance(t[section], dict):
            sysmon_fail(section, f"sysmon.{section} must be a mapping")
        if isinstance(t.get(section), dict):
            reject_unknown_keys(t[section], f"sysmon.{section}")


def _check_threshold(metric: str, pair: Any) -> None:
    where = f"thresholds.{metric}"
    if not isinstance(pair, dict):
        sysmon_fail(where, f"sysmon.{where} must be a mapping")
    reject_unknown_keys(pair, f"sysmon.{where}")
    if set(pair) != {"warn", "critical"}:
        sysmon_fail(where, f"sysmon.{where} needs both warn and critical")
    for k in ("warn", "critical"):
        if not _is_number(pair[k]) or pair[k] < 0:
            sysmon_fail(f"{where}.{k}", f"sysmon.{where}.{k} must be a number >= 0")
    lower_is_worse = metric in SYSMON_LOWER_IS_WORSE
    inverted = pair["warn"] < pair["critical"] if lower_is_worse else pair["warn"] > pair["critical"]
    if inverted:
        order = ">=" if lower_is_worse else "<="
        sysmon_fail(where, f"sysmon.{where}: warn must be {order} critical")


def _check_thresholds(t: dict[str, Any]) -> None:
    for metric, pair in _section(t, "thresholds").items():
        _check_threshold(metric, pair)


def _check_table_bools(t: dict[str, Any]) -> None:
    for section, keys in SYSMON_BOOLS.items():
        for key in keys:
            value = _section(t, section).get(key)
            if value is not None and not isinstance(value, bool):
                sysmon_fail(f"{section}.{key}", f"sysmon.{section}.{key} must be a bool, got {value!r}")


def _check_table_ints(t: dict[str, Any]) -> None:
    for section, bounds in SYSMON_INTS.items():
        for key, low in bounds.items():
            value = _section(t, section).get(key)
            if value is not None and (not is_int(value) or value < low):
                sysmon_fail(f"{section}.{key}", f"sysmon.{section}.{key} must be an int >= {low}, got {value!r}")


def _check_table_strs(t: dict[str, Any]) -> None:
    for section, keys in SYSMON_STRS.items():
        for key in keys:
            value = _section(t, section).get(key)
            if value is not None and not _is_nonempty_str(value):
                sysmon_fail(f"{section}.{key}", f"sysmon.{section}.{key} must be a non-empty string")


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(_is_nonempty_str(v) for v in value)


def _check_table_str_lists(t: dict[str, Any]) -> None:
    for section, keys in SYSMON_STR_LISTS.items():
        for key in keys:
            value = _section(t, section).get(key)
            if value is not None and not _is_str_list(value):
                sysmon_fail(f"{section}.{key}", f"sysmon.{section}.{key} must be a list of non-empty strings")


def _check_alert(t: dict[str, Any]) -> None:
    alert_block = _section(t, "alert")
    severity = alert_block.get("min_severity")
    if severity is not None and severity not in ("warning", "critical"):
        sysmon_fail("alert.min_severity", f"sysmon.alert.min_severity must be warning|critical, got {severity!r}")
    if alert_block.get("route") == "agents":
        sysmon_fail("alert.route", "sysmon.alert.route: agents was renamed to subscriber (rig-cli#700)")
    if "route" in alert_block and alert_block["route"] not in ("telegram", "subscriber"):
        sysmon_fail("alert.route", f"sysmon.alert.route must be telegram|subscriber, got {alert_block['route']!r}")
    if alert_block.get("route") == "subscriber" and t.get("auto_remediate") is True:
        sysmon_fail("auto_remediate", "sysmon.auto_remediate cannot be on with alert.route subscriber: the handler "
                                      "session replaces it (rig-cli#700)")


def _check_handler_session(t: dict[str, Any]) -> None:
    session = _section(t, "handler").get("tmux_session")
    # a string, or a non-negative int: `rig config set … 0` coerces the session name `0` to one
    valid = _is_nonempty_str(session) or (is_int(session) and session >= 0)
    if session is not None and not valid:
        sysmon_fail("handler.tmux_session",
                    f"sysmon.handler.tmux_session must be a tmux session name, got {session!r}")


def _check_remediation_and_heal(t: dict[str, Any]) -> None:
    agent = _section(t, "remediation").get("agent")
    if agent is not None and agent not in _REMEDIATION_AGENTS:
        sysmon_fail("remediation.agent",
                    f"sysmon.remediation.agent must be omp|claude-code|codex|opencode, got {agent!r}")
    labels = _section(t, "heal").get("restart_labels")
    if labels is not None and not _is_str_list(labels):
        sysmon_fail("heal.restart_labels",
                    f"sysmon.heal.restart_labels must be a list of launchd label patterns, got {labels!r}")


def validate_sysmon(t: dict[str, Any]) -> None:
    """Validate the ``sysmon`` block — the rig-managed resource monitor (rig-cli#622; see
    :mod:`riglib.sysmon`). Per-MACHINE, so GLOBAL layer only (``~/.config/rig/config.yaml``).
    Default **ON**: an EMPTY/absent block still provisions the daemon; ``enabled: false`` opts out.

    Fail-closed on every shape error (a monitor misconfigured into silence is the failure it
    exists to prevent): unknown keys at every level, non-bool flags, integers out of range,
    thresholds that are not ``{warn, critical}`` numbers or whose order is inverted (warn must be
    reached BEFORE critical: ``warn <= critical``, or ``warn >= critical`` for the lower-is-worse
    ``reclaimable_gb``/``free_gb``/``disk_free_gb``/``swap_gate_available_pct``), a
    ``hysteresis_pct`` outside 0-50, an unknown remediation agent or alert severity, and ``reclaim``
    path lists that are not lists of non-empty strings.
    """
    if not isinstance(t, dict):
        sysmon_fail("", "sysmon must be a mapping")
    if not t:
        return
    reject_unknown_keys(t, "sysmon")
    _check_top_scalars(t)
    _check_section_shapes(t)
    _check_thresholds(t)
    _check_table_bools(t)
    _check_table_ints(t)
    _check_table_strs(t)
    _check_table_str_lists(t)
    _check_alert(t)
    _check_handler_session(t)
    _check_remediation_and_heal(t)
