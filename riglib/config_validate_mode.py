"""Validator for the global ``mode`` config block (``mode.name`` + ``mode.autonomous.*``).

Why a separate module: the autonomous-mode policy is a deep tree of small sub-blocks with
its own presence-sensitive checks (a present-but-null key is rejected, unlike most blocks),
so it keeps private bool/str helpers instead of the shared ``None``-tolerant ones.
"""

from __future__ import annotations

from typing import Any

from .config_checks import check_int_min, reject_unknown_keys, validate_string_list
from .config_constants import VALID_AUTONOMOUS_UNTIL, VALID_MODE_NAMES
from .config_errors import ConfigError
from .config_validate_permissions import PERMISSION_RULE_RE

_AUTO = "mode.autonomous"


def check_mode_bool(block: dict[str, Any], key: str, path: str) -> None:
    value = block.get(key)
    if key in block and not isinstance(value, bool):
        raise ConfigError(f"{path} must be a bool, got {value!r}", schema_path=path)


def check_mode_str(block: dict[str, Any], key: str, path: str) -> None:
    value = block.get(key)
    if key in block and not isinstance(value, str):
        raise ConfigError(f"{path} must be a string, got {value!r}", schema_path=path)


def mode_mapping(parent: dict[str, Any], key: str, path: str) -> dict[str, Any]:
    if key not in parent:
        return {}
    value = parent[key]
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must be a mapping", schema_path=path)
    return value


def _check_name(m: dict[str, Any]) -> None:
    name = m.get("name", "standard")
    if not isinstance(name, str) or name not in VALID_MODE_NAMES:
        raise ConfigError(
            f"mode.name must be one of {sorted(VALID_MODE_NAMES)}, got {name!r}",
            fix=f"use one of: {', '.join(sorted(VALID_MODE_NAMES))}",
            schema_path="mode.name",
        )


def _check_review_fix(auto: dict[str, Any]) -> None:
    path = f"{_AUTO}.review_fix"
    review_fix = mode_mapping(auto, "review_fix", path)
    reject_unknown_keys(review_fix, path)
    check_mode_bool(review_fix, "enabled", f"{path}.enabled")
    check_int_min(review_fix, "max_iterations", f"{path}.max_iterations", 1)
    until = review_fix.get("until")
    if "until" in review_fix and (not isinstance(until, str) or until not in VALID_AUTONOMOUS_UNTIL):
        raise ConfigError(
            f"{path}.until must be one of {sorted(VALID_AUTONOMOUS_UNTIL)}, got {until!r}",
            fix=f"use one of: {', '.join(sorted(VALID_AUTONOMOUS_UNTIL))}",
            schema_path=f"{path}.until",
        )


def _check_decisions(auto: dict[str, Any]) -> None:
    path = f"{_AUTO}.decisions"
    decisions = mode_mapping(auto, "decisions", path)
    reject_unknown_keys(decisions, path)
    quorum_path = f"{path}.review_quorum"
    quorum = mode_mapping(decisions, "review_quorum", quorum_path)
    reject_unknown_keys(quorum, quorum_path)
    check_mode_bool(quorum, "enabled", f"{quorum_path}.enabled")
    check_int_min(quorum, "min_iterations", f"{quorum_path}.min_iterations", 1)
    check_int_min(quorum, "min_models", f"{quorum_path}.min_models", 2)


def _check_escalation(auto: dict[str, Any]) -> None:
    path = f"{_AUTO}.escalation"
    escalation = mode_mapping(auto, "escalation", path)
    reject_unknown_keys(escalation, path)
    check_mode_str(escalation, "framework_skill", f"{path}.framework_skill")
    check_mode_bool(
        escalation,
        "require_parallel_worktree_comparison",
        f"{path}.require_parallel_worktree_comparison",
    )


def _check_worktree_comparison(auto: dict[str, Any]) -> None:
    path = f"{_AUTO}.parallel_worktree_comparison"
    comparison = mode_mapping(auto, "parallel_worktree_comparison", path)
    reject_unknown_keys(comparison, path)
    check_mode_bool(comparison, "enabled", f"{path}.enabled")
    check_int_min(comparison, "candidates", f"{path}.candidates", 2)


def _check_development_tools(auto: dict[str, Any]) -> None:
    path = f"{_AUTO}.development_tools"
    devtools = mode_mapping(auto, "development_tools", path)
    reject_unknown_keys(devtools, path)
    if "allow" not in devtools:
        return
    validate_string_list(devtools["allow"], f"{path}.allow")
    for entry in devtools["allow"]:
        if not PERMISSION_RULE_RE.match(entry):
            raise ConfigError(
                f"{path}.allow entry {entry!r} is not a permission rule",
                schema_path=f"{path}.allow",
            )


def _check_parallelism(auto: dict[str, Any]) -> None:
    path = f"{_AUTO}.parallelism"
    parallel = mode_mapping(auto, "parallelism", path)
    reject_unknown_keys(parallel, path)
    check_int_min(parallel, "max_agents", f"{path}.max_agents", 1)
    check_int_min(parallel, "max_worktrees", f"{path}.max_worktrees", 1)
    check_int_min(parallel, "reserve_slots", f"{path}.reserve_slots", 0)
    check_mode_bool(parallel, "limit_aware", f"{path}.limit_aware")


def validate_mode(m: dict[str, Any]) -> None:
    """Validate the global ``mode`` block.

    ``mode.name: autonomous`` is a machine/global policy declaration. It does not replace the
    existing harness or permissions reconcilers; instead it feeds them with extra notes/rules while
    keeping the policy visible in the same config schema as every other rig setting.
    """
    if not isinstance(m, dict):
        raise ConfigError("mode must be a mapping", schema_path="mode")
    if not m:
        return
    reject_unknown_keys(m, "mode")
    _check_name(m)
    auto = mode_mapping(m, "autonomous", _AUTO)
    reject_unknown_keys(auto, _AUTO)
    _check_review_fix(auto)
    _check_decisions(auto)
    _check_escalation(auto)
    _check_worktree_comparison(auto)
    _check_development_tools(auto)
    _check_parallelism(auto)
