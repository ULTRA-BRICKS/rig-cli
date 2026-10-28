"""Validator for the ``harness`` config block.

Why a separate module: ``harness`` is the largest block (kinds, fan-out, hook bridge, web search,
idle TTL, codex skills budget) and each sub-block has its own fail-closed shape rules; keeping
them here, one helper per sub-block, keeps every function inside the complexity gate.
"""

from __future__ import annotations

from typing import Any

from .config_checks import check_bool, check_int_min, check_str, reject_unknown_keys
from .config_constants import DEPRECATED_HARNESS_KINDS, RESERVED_HARNESS_KINDS, VALID_HARNESS_KINDS
from .config_errors import ConfigError


def validate_harness_fan_out(h: dict[str, Any]) -> None:
    """Validate the claude-rotate fan-out keys (rig-cli#368): ``settings_paths`` + ``discover_config_dirs``.

    ``settings_paths`` lists EXTRA user-scope ``settings.json`` files the claude-code write reaches
    (each must be a ``.json`` path); ``discover_config_dirs`` (bool, default true) toggles the
    automatic ``~/.claude-accounts/account-*`` discovery. Fail closed on any other shape.
    """
    paths = h.get("settings_paths")
    if paths is not None:
        if not isinstance(paths, list) or not all(isinstance(p, str) and p for p in paths):
            raise ConfigError(
                f"harness.settings_paths must be a list of non-empty strings, got {paths!r}",
                schema_path="harness.settings_paths",
            )
        for p in paths:
            if not p.endswith(".json"):
                raise ConfigError(
                    f"harness.settings_paths entries must end in .json (claude-code settings files), got {p!r}",
                    schema_path="harness.settings_paths",
                )
    discover = h.get("discover_config_dirs")
    if discover is not None and not isinstance(discover, bool):
        raise ConfigError(
            f"harness.discover_config_dirs must be a bool, got {discover!r}",
            schema_path="harness.discover_config_dirs",
        )


def _supported_kinds() -> str:
    return ", ".join(sorted(VALID_HARNESS_KINDS))


def _check_kind(h: dict[str, Any]) -> None:
    kind = h.get("kind", "claude-code")
    if not isinstance(kind, str):
        raise ConfigError(f"harness.kind must be a string, got {kind!r}", schema_path="harness.kind")
    if kind in DEPRECATED_HARNESS_KINDS:
        raise ConfigError(
            f"harness.kind '{kind}' is no longer supported (deprecated). "
            f"{DEPRECATED_HARNESS_KINDS[kind]}",
            fix=f"remove the harness block or use one of: {_supported_kinds()}",
            schema_path="harness.kind",
        )
    if kind in RESERVED_HARNESS_KINDS:
        raise ConfigError(
            f"harness.kind '{kind}' is documented but not implemented in this rig "
            f"(supported: {sorted(VALID_HARNESS_KINDS)}). Remove the harness block or "
            f"use a supported kind.",
            fix=f"use one of: {_supported_kinds()}, or remove the harness block",
            schema_path="harness.kind",
        )
    if kind not in VALID_HARNESS_KINDS:
        raise ConfigError(
            f"harness.kind must be one of {sorted(VALID_HARNESS_KINDS)}, got {kind!r}",
            fix=f"use one of: {_supported_kinds()}",
            schema_path="harness.kind",
        )


def _check_extra_kind(extra_kind: str) -> None:
    if extra_kind in DEPRECATED_HARNESS_KINDS:
        raise ConfigError(
            f"harness.kinds entry '{extra_kind}' is no longer supported (deprecated). "
            f"{DEPRECATED_HARNESS_KINDS[extra_kind]}",
            fix=f"use one of: {_supported_kinds()}",
            schema_path="harness.kinds",
        )
    if extra_kind in RESERVED_HARNESS_KINDS:
        raise ConfigError(
            f"harness.kinds entry '{extra_kind}' is documented but not implemented in this rig "
            f"(supported: {sorted(VALID_HARNESS_KINDS)}).",
            fix=f"use one of: {_supported_kinds()}",
            schema_path="harness.kinds",
        )
    if extra_kind not in VALID_HARNESS_KINDS:
        raise ConfigError(
            f"harness.kinds entries must be one of {sorted(VALID_HARNESS_KINDS)}, got {extra_kind!r}",
            fix=f"use one of: {_supported_kinds()}",
            schema_path="harness.kinds",
        )


def _check_kinds(h: dict[str, Any]) -> None:
    kinds = h.get("kinds")
    if kinds is None:
        return
    if not isinstance(kinds, list) or not all(isinstance(v, str) for v in kinds):
        raise ConfigError(
            f"harness.kinds must be a list of strings, got {kinds!r}",
            schema_path="harness.kinds",
        )
    for extra_kind in kinds:
        _check_extra_kind(extra_kind)


def _sub_block(h: dict[str, Any], key: str) -> dict[str, Any] | None:
    """The ``harness.<key>`` mapping (unknown keys rejected), or None when the block is absent."""
    block = h.get(key)
    if block is None:
        return None
    path = f"harness.{key}"
    if not isinstance(block, dict):
        raise ConfigError(f"{path} must be a mapping, got {block!r}", schema_path=path)
    reject_unknown_keys(block, path)
    return block


def _check_hook_bridge(h: dict[str, Any]) -> None:
    # harness.hook_bridge — wires the agents-hooks/v1 → harness dispatcher into the harness's
    # native hook surface so installed agent-hooks actually FIRE (agent-tools#18). enabled
    # defaults true; a custom python interpreter is optional. Fail-closed on wrong types.
    bridge = _sub_block(h, "hook_bridge")
    if bridge is None:
        return
    check_bool(bridge, "enabled", "harness.hook_bridge.enabled")
    check_str(bridge, "python", "harness.hook_bridge.python")


def _check_web_search(h: dict[str, Any]) -> None:
    # harness.web_search — omp providers.webSearchExclude (rig-cli#447). Default ON when
    # the block is absent; enabled:false / exclude:[] opts out. Fail-closed on types.
    ws = _sub_block(h, "web_search")
    if ws is None:
        return
    check_bool(ws, "enabled", "harness.web_search.enabled")
    exclude = ws.get("exclude")
    if exclude is not None and (
        not isinstance(exclude, list) or not all(isinstance(v, str) and v for v in exclude)
    ):
        raise ConfigError(
            f"harness.web_search.exclude must be a list of non-empty strings, got {exclude!r}",
            schema_path="harness.web_search.exclude",
        )


def _check_idle_ttl(h: dict[str, Any]) -> None:
    # harness.idle_ttl — omp task.agentIdleTtlMs grace window (rig-cli#445/#464/#555).
    # Default ON when the block is absent; enabled:false skips provisioning, ms overrides
    # the grace window rig writes (<=0 restores the pre-#555 never-park behavior).
    idle_ttl = _sub_block(h, "idle_ttl")
    if idle_ttl is None:
        return
    check_bool(idle_ttl, "enabled", "harness.idle_ttl.enabled")
    ms = idle_ttl.get("ms")
    if ms is not None and (isinstance(ms, bool) or not isinstance(ms, int)):
        raise ConfigError(
            f"harness.idle_ttl.ms must be an int, got {ms!r}",
            schema_path="harness.idle_ttl.ms",
        )


def _check_codex_skills_budget(h: dict[str, Any]) -> None:
    # harness.codex_skills_budget — codex [skills].max_context_tokens. Default ON when the
    # block is absent; enabled:false skips provisioning. max_context_tokens must be a positive
    # int (no <=0 special case, unlike idle_ttl.ms — a budget of 0 has no meaning here).
    budget = _sub_block(h, "codex_skills_budget")
    if budget is None:
        return
    check_bool(budget, "enabled", "harness.codex_skills_budget.enabled")
    check_int_min(
        budget, "max_context_tokens", "harness.codex_skills_budget.max_context_tokens", 1
    )


def validate_harness(h: dict[str, Any]) -> None:
    """Validate the ``harness`` block — the agent harness's skill + auto/permission provisioning.

    Fail-closed on an unknown ``kind`` (typo guard), a DEPRECATED ``kind`` (``gemini`` — helpful
    "no longer supported" message), and a non-bool ``auto_mode``. Every harness
    rig knows a skill/instruction discovery convention for (claude-code, opencode, omp, codex,
    pi, commandcode — :data:`VALID_HARNESS_KINDS`) is ACCEPTED here: rig provisions its SKILL
    discovery, and the auto/permission-MODE write is keyed off :mod:`riglib.harness_mode` (a kind
    with no such setting, pi/commandcode, gets a visible plan note — see plan.py ``_build_harness``)
    rather than being rejected at validation.
    """
    if not isinstance(h, dict):
        raise ConfigError("harness must be a mapping", schema_path="harness")
    if not h:
        return
    reject_unknown_keys(h, "harness")
    _check_kind(h)
    _check_kinds(h)
    check_bool(h, "auto_mode", "harness.auto_mode")
    validate_harness_fan_out(h)
    # self_merge gates a security-sensitive global carve-out; plan.py coerces it via bool(...),
    # so a non-bool like the string "false" would read truthy and ENABLE what the user meant to
    # disable. Fail closed, mirroring auto_mode.
    check_bool(h, "self_merge", "harness.self_merge")
    check_str(h, "mode", "harness.mode")
    _check_hook_bridge(h)
    _check_web_search(h)
    _check_idle_ttl(h)
    _check_codex_skills_budget(h)
