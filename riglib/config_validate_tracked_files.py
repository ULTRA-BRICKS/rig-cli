"""Validator for the ``tracked_files`` block (landing rig-owned committed files).

Kept apart because it is the one block with per-path glob rules and nested ship/review
sub-blocks; the checks are split per field so each stays small.
"""

from __future__ import annotations

import shlex
from typing import Any

from .config_checks import check_int_min, check_str_list, reject_unknown_keys
from .config_errors import ConfigError
from .config_validate_linters import linter_path_escapes_repo

TRACKED_FILES_LAND_MODES = ("ship", "pr")

_SUB_BLOCKS = (("ship", ("task_code", "task_config", "command")), ("review", ("models",)))


def _typed(block: dict[str, Any], key: str, path: str, kind: type, what: str) -> None:
    # Strict: a PRESENT key must have its type — a null `land:` must not quietly mean "PR only".
    if key in block and not isinstance(block[key], kind):
        raise ConfigError(f"{path} must be {what}, got {block[key]!r}", schema_path=path)


def _check_path_entry(p: str) -> None:
    if not p.strip() or p.endswith("/") or linter_path_escapes_repo(p):
        raise ConfigError(
            f"tracked_files.paths entries must be relative file paths inside the repo, got {p!r}",
            schema_path="tracked_files.paths",
        )
    if any(c in p for c in "[]{}"):
        raise ConfigError(
            f"tracked_files.paths supports `*`, `**` and `?` globs only, not classes or braces: {p!r}",
            schema_path="tracked_files.paths",
        )


def _check_paths(tf: dict[str, Any]) -> None:
    _typed(tf, "paths", "tracked_files.paths", list, "a list of strings")
    check_str_list(tf, "paths", "tracked_files.paths")
    if tf.get("paths") == []:
        raise ConfigError(
            "tracked_files.paths must list at least one file (to land nothing, set tracked_files.enabled: false)",
            schema_path="tracked_files.paths",
        )
    for p in tf.get("paths") or []:
        _check_path_entry(p)


def _check_land(tf: dict[str, Any]) -> None:
    if "land" in tf and tf["land"] not in TRACKED_FILES_LAND_MODES:
        raise ConfigError(
            f"tracked_files.land must be one of {list(TRACKED_FILES_LAND_MODES)}, got {tf['land']!r}",
            schema_path="tracked_files.land",
        )


def _check_ship_args_and_command(block: dict[str, Any]) -> None:
    _typed(block, "args", "tracked_files.ship.args", list, "a list of strings")
    check_str_list(block, "args", "tracked_files.ship.args")
    if "command" not in block:
        return
    try:  # the background lander splits it the same way
        shlex.split(block["command"])
    except ValueError as exc:
        raise ConfigError(f"tracked_files.ship.command does not parse as a command line: {exc}",
                          schema_path="tracked_files.ship.command") from None


def _check_sub_block(tf: dict[str, Any], name: str, keys: tuple[str, ...]) -> None:
    block = tf[name]
    if not isinstance(block, dict):
        raise ConfigError(f"tracked_files.{name} must be a mapping", schema_path=f"tracked_files.{name}")
    reject_unknown_keys(block, f"tracked_files.{name}")
    for key in keys:
        _typed(block, key, f"tracked_files.{name}.{key}", str, "a string")
        if key in block and not block[key].strip():
            raise ConfigError(
                f"tracked_files.{name}.{key} must not be empty (omit the key for its default)",
                schema_path=f"tracked_files.{name}.{key}",
            )
    if name == "ship":
        _check_ship_args_and_command(block)


def validate_tracked_files(tf: Any) -> None:
    """Validate ``tracked_files``: presence opts in, ``enabled: false`` opts out."""
    if tf is None:
        # a bare `tracked_files:` names nothing: say which spelling is meant
        raise ConfigError(
            "tracked_files is empty (null) — write `tracked_files: {}` to land with the defaults, "
            "or remove the line",
            schema_path="tracked_files",
        )
    if not isinstance(tf, dict):
        raise ConfigError("tracked_files must be a mapping", schema_path="tracked_files")
    if not tf:
        return
    reject_unknown_keys(tf, "tracked_files")
    for key in ("enabled", "normalize_worktrees"):
        _typed(tf, key, f"tracked_files.{key}", bool, "a bool")
    check_int_min(tf, "harvest_min_worktrees", "tracked_files.harvest_min_worktrees", 1)
    check_int_min(tf, "normalize_min_age_minutes", "tracked_files.normalize_min_age_minutes", 0)
    _check_paths(tf)
    _check_land(tf)
    for name, keys in _SUB_BLOCKS:
        if name in tf:
            _check_sub_block(tf, name, keys)
