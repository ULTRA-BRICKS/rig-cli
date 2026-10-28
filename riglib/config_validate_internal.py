"""Validators for the ``internal_dev`` and ``ship_delegator`` blocks.

Split from the other block validators because both are small per-repo toggles with no
shared helpers beyond the common key/bool checks.
"""

from __future__ import annotations

from typing import Any

from .config_checks import check_bool, reject_unknown_keys
from .config_errors import ConfigError


def _check_daemon_source_paths(paths: Any) -> None:
    if paths is not None and (not isinstance(paths, list) or not all(isinstance(e, str) for e in paths)):
        raise ConfigError(
            f"internal_dev.daemon_source_paths must be a list of strings, got {paths!r}",
            schema_path="internal_dev.daemon_source_paths",
        )


def _check_reload_command(cmd: Any) -> None:
    if cmd is not None and not isinstance(cmd, str):
        raise ConfigError(
            f"internal_dev.reload_command must be a string, got {cmd!r}",
            schema_path="internal_dev.reload_command",
        )


def _check_enabled_has_paths(paths: list[str] | None) -> None:
    # Enabling with no usable path would install a hook that can never match anything.
    effective = [p for p in (paths if paths is not None else []) if p.strip()]
    if not effective:
        raise ConfigError(
            "internal_dev.auto_reload_on_commit is enabled but daemon_source_paths is "
            "empty or whitespace-only — that would install a post-commit hook that can "
            "never match anything",
            schema_path="internal_dev.daemon_source_paths",
        )


def validate_internal_dev(d: dict[str, Any]) -> None:
    """Validate ``internal_dev`` (per-repo daemon auto-reload post-commit hook; default off)."""
    if not isinstance(d, dict):
        raise ConfigError("internal_dev must be a mapping", schema_path="internal_dev")
    if not d:
        return
    reject_unknown_keys(d, "internal_dev")
    check_bool(d, "auto_reload_on_commit", "internal_dev.auto_reload_on_commit")
    paths = d.get("daemon_source_paths")
    _check_daemon_source_paths(paths)
    _check_reload_command(d.get("reload_command"))
    if d.get("auto_reload_on_commit") is True:
        _check_enabled_has_paths(paths)


def validate_ship_delegator(sd: dict[str, Any]) -> None:
    """Validate ``ship_delegator`` (default on; only knob is ``enabled``)."""
    if not isinstance(sd, dict):
        raise ConfigError("ship_delegator must be a mapping", schema_path="ship_delegator")
    if not sd:
        return
    reject_unknown_keys(sd, "ship_delegator")
    # `in`, not `.get()`: an explicit null must be rejected (the JSON Schema says boolean).
    if "enabled" in sd and not isinstance(sd["enabled"], bool):
        raise ConfigError(
            f"ship_delegator.enabled must be a bool, got {sd['enabled']!r}",
            schema_path="ship_delegator.enabled",
        )
