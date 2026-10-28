"""Validators for the ``ci``, ``agent_hooks``, ``mcp`` and ``git_hooks`` config blocks.

These four blocks share one shape (fixed knobs plus an open ``items`` map) and are kept apart
from the top-level orchestrator so each stays a small, individually testable function. Error
wording and ``schema_path`` values are part of the published contract; do not reword them.
"""

from __future__ import annotations

from typing import Any

from .config_checks import (
    check_bool,
    check_str,
    check_str_list,
    reject_unknown_keys,
    validate_string_list,
)
from .config_constants import (
    GITHUB_OWNER_RE,
    MCP_ITEM_KEYS,
    SHIP_POLICY_OWNER_KEYS,
    VALID_LEGACY_AGENT_HOOK_TARGET_KINDS,
    VALID_ON_ERROR,
    VALID_TIERS,
)
from .config_errors import ConfigError


def _check_ship_policy(ship: dict[str, Any]) -> None:
    """``ci.items.ship`` policy knobs: a strict bool and exact GitHub owner lists."""
    if "require_external_review" in ship and type(ship["require_external_review"]) is not bool:
        raise ConfigError(
            "ci.items.ship.require_external_review must be a bool",
            schema_path="ci.items.ship.require_external_review",
        )
    for key in SHIP_POLICY_OWNER_KEYS:
        if key not in ship:
            continue
        owners = ship[key]
        if not isinstance(owners, list) or not all(
            isinstance(owner, str) and GITHUB_OWNER_RE.fullmatch(owner) for owner in owners
        ):
            raise ConfigError(
                f"ci.items.ship.{key} must be a list of exact GitHub owner names",
                schema_path=f"ci.items.ship.{key}",
            )


def _check_item_tiers(items: dict[str, Any]) -> None:
    for name, spec in items.items():
        if not isinstance(spec, dict):
            continue
        tier = spec.get("tier")
        if tier is not None and tier not in VALID_TIERS:
            raise ConfigError(
                f"ci.items.{name}.tier must be one of {sorted(VALID_TIERS)}, got {tier!r}",
                fix=f"use one of: {', '.join(sorted(VALID_TIERS))}",
                schema_path=f"ci.items.{name}.tier",
            )


def validate_ci(ci: dict[str, Any]) -> None:
    # Reject an unknown FIXED knob (enabled/target/all); the open `items` map keeps arbitrary
    # gate names valid (a bad gate NAME is a catalog error caught later, not a schema typo).
    reject_unknown_keys(ci, "ci")
    check_bool(ci, "enabled", "ci.enabled")
    check_str(ci, "target", "ci.target")
    check_bool(ci, "all", "ci.all")
    check_str_list(ci, "known", "ci.known")
    items = ci.get("items", {})
    if not isinstance(items, dict):
        raise ConfigError("ci.items must be a mapping", schema_path="ci.items")
    ship = items.get("ship", {})
    if isinstance(ship, dict):
        _check_ship_policy(ship)
    _check_item_tiers(items)


def _check_legacy_target_kind(ah: dict[str, Any]) -> None:
    target_kind = ah.get("target_kind")
    if target_kind is None:
        return
    if not isinstance(target_kind, str):
        raise ConfigError(
            f"agent_hooks.target_kind must be a string, got {target_kind!r}",
            schema_path="agent_hooks.target_kind",
        )
    if target_kind not in VALID_LEGACY_AGENT_HOOK_TARGET_KINDS:
        raise ConfigError(
            "agent_hooks.target_kind is a legacy ignored key and must be one of "
            f"{sorted(VALID_LEGACY_AGENT_HOOK_TARGET_KINDS)}, got {target_kind!r}",
            fix="remove agent_hooks.target_kind; rig now derives hook targets from harness.kind/kinds",
            schema_path="agent_hooks.target_kind",
        )


def _check_agent_hook_items(items: dict[str, Any]) -> None:
    for name, spec in items.items():
        if not isinstance(spec, dict):
            continue
        on_error = spec.get("on_error")
        if on_error is not None and on_error not in VALID_ON_ERROR:
            raise ConfigError(
                f"agent_hooks.items.{name}.on_error must be one of "
                f"{sorted(VALID_ON_ERROR)}, got {on_error!r}",
                fix=f"use one of: {', '.join(sorted(VALID_ON_ERROR))}",
                schema_path=f"agent_hooks.items.{name}.on_error",
            )


def validate_agent_hooks(ah: dict[str, Any]) -> None:
    reject_unknown_keys(ah, "agent_hooks")
    check_bool(ah, "enabled", "agent_hooks.enabled")
    check_str(ah, "target", "agent_hooks.target")
    _check_legacy_target_kind(ah)
    check_bool(ah, "all", "agent_hooks.all")
    # The two per-repo workflow knobs are booleans in the published schema; type-check them
    # here so the strict validator and schema/rig.schema.json agree (else `worktree_only: "no"`
    # would pass the validator but violate the schema). See tests/test_workflow_guard_knobs.py.
    check_bool(ah, "worktree_only", "agent_hooks.worktree_only")
    check_bool(ah, "orchestrator_only", "agent_hooks.orchestrator_only")
    check_str_list(ah, "known", "agent_hooks.known")
    items = ah.get("items", {})
    if not isinstance(items, dict):
        raise ConfigError("agent_hooks.items must be a mapping", schema_path="agent_hooks.items")
    _check_agent_hook_items(items)


def _check_mcp_env(env: Any, path: str) -> None:
    if not isinstance(env, dict):
        raise ConfigError(f"{path}.env must be a mapping", schema_path=f"{path}.env")
    for env_key, env_value in env.items():
        if not isinstance(env_key, str):
            raise ConfigError(f"{path}.env keys must be strings", schema_path=f"{path}.env")
        if not isinstance(env_value, str):
            raise ConfigError(
                f"{path}.env.{env_key} must be a string, got {env_value!r}",
                schema_path=f"{path}.env.{env_key}",
            )


def _check_mcp_item(spec: Any, path: str) -> None:
    if not isinstance(spec, dict):
        raise ConfigError(f"{path} must be a mapping", schema_path=path)
    unknown = sorted(set(spec) - MCP_ITEM_KEYS)
    if unknown:
        bad = unknown[0]
        raise ConfigError(
            f"unknown {path} key: {bad}",
            why=f"{bad} is not a known MCP item key",
            fix=f"use one of: {', '.join(sorted(MCP_ITEM_KEYS))}",
            schema_path=f"{path}.{bad}",
        )
    check_bool(spec, "enabled", f"{path}.enabled")
    check_str(spec, "server", f"{path}.server")
    check_str(spec, "command", f"{path}.command")
    if "args" in spec:
        validate_string_list(spec["args"], f"{path}.args")
    env = spec.get("env")
    if env is not None:
        _check_mcp_env(env, path)


def validate_mcp(mcp: dict[str, Any]) -> None:
    """Validate the ``mcp`` block — MCP server registrations.

    Fail-closed on an unknown fixed key — the open ``items`` map (arbitrary server names)
    stays valid; a bad SERVER name is a catalog error caught later, not a schema typo.
    """
    if not isinstance(mcp, dict):
        raise ConfigError("mcp must be a mapping", schema_path="mcp")
    reject_unknown_keys(mcp, "mcp")
    enabled = mcp.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise ConfigError(f"mcp.enabled must be a bool, got {enabled!r}", schema_path="mcp.enabled")
    target = mcp.get("target")
    if target is not None and not isinstance(target, str):
        raise ConfigError(f"mcp.target must be a string, got {target!r}", schema_path="mcp.target")
    check_str_list(mcp, "known", "mcp.known")
    items = mcp.get("items", {})
    if not isinstance(items, dict):
        raise ConfigError("mcp.items must be a mapping", schema_path="mcp.items")
    for name, spec in items.items():
        _check_mcp_item(spec, f"mcp.items.{name}")


def _check_dispatcher(disp: dict[str, Any]) -> None:
    reject_unknown_keys(disp, "git_hooks.dispatcher")
    for boolkey in ("enabled", "set_global_hooks_path", "install_local_retrofit_script"):
        value = disp.get(boolkey)
        if value is not None and not isinstance(value, bool):
            raise ConfigError(
                f"git_hooks.dispatcher.{boolkey} must be a bool, got {value!r}",
                schema_path=f"git_hooks.dispatcher.{boolkey}",
            )
    for strkey in ("dir", "runner"):
        value = disp.get(strkey)
        if value is not None and not isinstance(value, str):
            raise ConfigError(
                f"git_hooks.dispatcher.{strkey} must be a string, got {value!r}",
                schema_path=f"git_hooks.dispatcher.{strkey}",
            )


def validate_git_hooks(gh: dict[str, Any]) -> None:
    """Validate the ``git_hooks`` block — the global-hook dispatcher.

    Fail-closed on an unknown ``git_hooks`` / ``git_hooks.dispatcher`` key (the dispatcher keeps
    its open ``fragments`` map) and a non-bool dispatcher bool knob. An EMPTY/absent block is fine.
    """
    if not isinstance(gh, dict):
        raise ConfigError("git_hooks must be a mapping", schema_path="git_hooks")
    if not gh:
        return
    reject_unknown_keys(gh, "git_hooks")
    disp = gh.get("dispatcher")
    if disp is None:
        return
    if not isinstance(disp, dict):
        raise ConfigError(
            f"git_hooks.dispatcher must be a mapping, got {disp!r}",
            schema_path="git_hooks.dispatcher",
        )
    _check_dispatcher(disp)
