"""Validators for the global ``env``, ``gitignore``, ``spotlight`` and ``bun`` blocks.

These machine-wide blocks share the same "mapping, unknown keys, typed scalars" shape,
so they live together; tracked_files and internal_dev are per-repo and live elsewhere.
"""

from __future__ import annotations

from typing import Any

from .config_checks import reject_unknown_keys
from .config_errors import ConfigError
from .config_managed_constants import GITIGNORE_BEGIN_MARKER, GITIGNORE_END_MARKER
from .shell_env import ENV_VAR_KEY_PATTERN


def _is_str_list(val: Any) -> bool:
    return isinstance(val, list) and all(isinstance(e, str) for e in val)


def _check_optional_bool(block: dict[str, Any], key: str, path: str) -> None:
    value = block.get(key)
    if value is not None and not isinstance(value, bool):
        raise ConfigError(f"{path} must be a bool, got {value!r}", schema_path=path)


def _check_optional_str(block: dict[str, Any], key: str, path: str) -> None:
    value = block.get(key)
    if value is not None and not isinstance(value, str):
        raise ConfigError(f"{path} must be a string, got {value!r}", schema_path=path)


def _check_optional_str_list(block: dict[str, Any], key: str, path: str) -> None:
    value = block.get(key)
    if value is not None and not _is_str_list(value):
        raise ConfigError(f"{path} must be a list of strings, got {value!r}", schema_path=path)


def _check_env_path(e: dict[str, Any], pathkey: str) -> None:
    if pathkey not in e:
        return
    pathval = e[pathkey]
    schema_path = f"env.{pathkey}"
    if not isinstance(pathval, str):
        raise ConfigError(f"env.{pathkey} must be a string, got {pathval!r}", schema_path=schema_path)
    # An empty string would resolve to Path(".") and write the machine-wide file into the CWD.
    if not pathval:
        raise ConfigError(f"env.{pathkey} must not be empty", schema_path=schema_path)
    # The splice/drift layer is line-oriented: a multi-line import line never matches itself,
    # so every apply would append another copy forever.
    if "\n" in pathval or "\r" in pathval:
        raise ConfigError(f"env.{pathkey} must not contain a newline", schema_path=schema_path)


def _check_env_var(var_key: Any, var_value: Any) -> None:
    if not isinstance(var_key, str) or not var_key:
        raise ConfigError(
            f"env.vars keys must be non-empty strings, got {var_key!r}",
            schema_path="env.vars",
        )
    if not ENV_VAR_KEY_PATTERN.match(var_key):
        raise ConfigError(
            f"env.vars key {var_key!r} is not a valid shell identifier",
            why=(
                "the key is written UNQUOTED into `export {key}=...` in the generated "
                "rig.env.sh, which is sourced by every zsh invocation on the machine — "
                "an invalid key (a space, `;`, `=`, `$(...)`, a newline, …) would inject "
                "arbitrary shell text into that file"
            ),
            fix="use only letters, digits, and underscores; the first character must "
                "not be a digit (e.g. COLORTERM, MY_FLAG)",
            schema_path=f"env.vars.{var_key}",
        )
    if not isinstance(var_value, str):
        raise ConfigError(
            f"env.vars.{var_key} must be a string, got {var_value!r}",
            schema_path=f"env.vars.{var_key}",
        )


def validate_env(e: dict[str, Any]) -> None:
    """Validate ``env`` — rig-managed shell environment variables (global, machine-wide)."""
    if not isinstance(e, dict):
        raise ConfigError("env must be a mapping", schema_path="env")
    if not e:
        return
    reject_unknown_keys(e, "env")
    _check_optional_bool(e, "enabled", "env.enabled")
    for pathkey in ("rc_path", "generated_dir"):
        _check_env_path(e, pathkey)
    vars_block = e.get("vars")
    if vars_block is None:
        return
    if not isinstance(vars_block, dict):
        raise ConfigError("env.vars must be a mapping", schema_path="env.vars")
    for var_key, var_value in vars_block.items():
        _check_env_var(var_key, var_value)


def _check_gitignore_entry(e: str) -> None:
    # An entry with a managed marker would make every later resolve see a duplicated marker.
    if GITIGNORE_BEGIN_MARKER in e or GITIGNORE_END_MARKER in e:
        raise ConfigError(
            f"gitignore.entries may not contain a rig-managed marker line, got {e!r}",
            schema_path="gitignore.entries",
        )
    # Mirror in the JSON schema: GITIGNORE_ENTRY_JSON_PATTERN (config_schema.py).
    if not e.strip() or e.splitlines() != [e]:
        raise ConfigError(
            f"gitignore.entries must be single non-empty lines, got {e!r}",
            schema_path="gitignore.entries",
            why="a blank or multi-line entry can never match itself back as a single "
                "ignored line, and would render broken across the drift/status detail",
            fix="use one non-empty line per entry (leading/trailing spaces are fine)",
        )


def validate_gitignore(gi: dict[str, Any]) -> None:
    """Validate ``gitignore`` — rig's managed block in the global git excludes file."""
    if not isinstance(gi, dict):
        raise ConfigError("gitignore must be a mapping", schema_path="gitignore")
    if not gi:
        return
    reject_unknown_keys(gi, "gitignore")
    _check_optional_bool(gi, "enabled", "gitignore.enabled")
    _check_optional_str(gi, "excludesfile", "gitignore.excludesfile")
    entries = gi.get("entries")
    if entries is None:
        return
    _check_optional_str_list(gi, "entries", "gitignore.entries")
    for e in entries:
        _check_gitignore_entry(e)


def validate_spotlight(s: dict[str, Any]) -> None:
    """Validate ``spotlight`` — the macOS Spotlight-exclude sweep (global, default off)."""
    if not isinstance(s, dict):
        raise ConfigError("spotlight must be a mapping", schema_path="spotlight")
    if not s:
        return
    reject_unknown_keys(s, "spotlight")
    _check_optional_bool(s, "enabled", "spotlight.enabled")
    for key in ("roots", "deny", "extra"):
        _check_optional_str_list(s, key, f"spotlight.{key}")
    _check_optional_str(s, "label", "spotlight.label")
    max_depth = s.get("max_depth")
    if max_depth is not None and (isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 1):
        raise ConfigError(
            f"spotlight.max_depth must be a positive int, got {max_depth!r}",
            schema_path="spotlight.max_depth",
        )


def validate_bun(s: dict[str, Any]) -> None:
    """Validate ``bun`` — global bunfig isolated linker + globalStore splice (default off)."""
    if not isinstance(s, dict):
        raise ConfigError("bun must be a mapping", schema_path="bun")
    if not s:
        return
    reject_unknown_keys(s, "bun")
    for key in ("enabled", "worktree_install"):
        _check_optional_bool(s, key, f"bun.{key}")
