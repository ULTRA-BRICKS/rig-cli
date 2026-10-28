"""Validator for the ``harness_wrappers`` config block.

Owns the opt-in per-harness PATH wrapper settings check (:mod:`riglib.wrapper_provision`). It is a
separate module because the block has its own path / kind-list / real_bin rules that share nothing
with the other block validators, and keeping them here keeps ``config.py`` a thin dispatcher.
"""

from __future__ import annotations

from typing import Any

from .config_checks import reject_unknown_keys
from .config_errors import ConfigError
from .harness_kinds import KINDS as HARNESS_WRAPPER_KINDS
from .wrapper_provision import ABSOLUTE_PATH_STARTS as WRAPPER_PATH_STARTS


def _known_kinds_fix() -> str:
    return f"use kinds from: {', '.join(HARNESS_WRAPPER_KINDS)}"


def _check_path(where: str, value: Any) -> None:
    if not isinstance(value, str) or not value.startswith(WRAPPER_PATH_STARTS) or "\n" in value:
        raise ConfigError(
            f"{where} must be an absolute path (or start with ~ or $VAR), got {value!r}", schema_path=where
        )


def _string_list(block: dict[str, Any], key: str) -> Any:
    """Return ``block[key]`` after requiring it to be a list of strings."""
    value = block[key]
    if not isinstance(value, list) or not all(isinstance(k, str) for k in value):
        raise ConfigError(
            f"harness_wrappers.{key} must be a list of strings, got {value!r}",
            schema_path=f"harness_wrappers.{key}",
        )
    return value


def _reject_unknown_kinds(values: list[str], key: str) -> None:
    unknown = [k for k in values if k not in HARNESS_WRAPPER_KINDS]
    if unknown:
        raise ConfigError(
            f"harness_wrappers.{key} has unknown harness {unknown[0]!r}",
            fix=_known_kinds_fix(),
            schema_path=f"harness_wrappers.{key}",
        )


def _check_kinds(block: dict[str, Any]) -> None:
    kinds = _string_list(block, "kinds")
    if not kinds:
        raise ConfigError(
            "harness_wrappers.kinds must list at least one kind (to install none, set "
            "harness_wrappers.enabled: false)",
            schema_path="harness_wrappers.kinds",
        )
    _reject_unknown_kinds(kinds, "kinds")


def _check_signal_scope(block: dict[str, Any]) -> None:
    # Unlike ``kinds``, an empty list is meaningful: it turns signal containment off.
    _reject_unknown_kinds(_string_list(block, "signal_scope"), "signal_scope")


def _check_real_bin_entry(kind: Any, path: Any, installed: Any) -> None:
    where = f"harness_wrappers.real_bin.{kind}"
    if kind not in HARNESS_WRAPPER_KINDS:
        raise ConfigError(f"{where}: unknown harness {kind!r}", fix=_known_kinds_fix(), schema_path=where)
    if kind not in installed:
        raise ConfigError(
            f"{where} is set but {kind!r} is not in harness_wrappers.kinds",
            fix=f"add {kind!r} to harness_wrappers.kinds or drop the real_bin entry",
            schema_path=where,
        )
    _check_path(where, path)


def _check_real_bin(block: dict[str, Any]) -> None:
    real_bin = block["real_bin"]
    if not isinstance(real_bin, dict):
        raise ConfigError(
            f"harness_wrappers.real_bin must be a mapping of kind -> absolute path, got {real_bin!r}",
            schema_path="harness_wrappers.real_bin",
        )
    kinds = block.get("kinds")
    installed = HARNESS_WRAPPER_KINDS if kinds is None else kinds
    for kind, path in real_bin.items():
        _check_real_bin_entry(kind, path, installed)


def validate_harness_wrappers(block: Any) -> None:
    """Validate the ``harness_wrappers`` block — the opt-in per-harness PATH wrappers
    (:mod:`riglib.wrapper_provision`).

    Default **OFF**: an absent/empty block is a no-op. GLOBAL layer only — ``config.load`` already
    rejected the block from any other layer (``GLOBAL_LAYER_ONLY_BLOCKS``). Fail-closed on a
    non-mapping block, an unknown key, a non-bool ``enabled``, a ``dir`` / ``real_bin`` path that is
    not absolute (or ``~`` / ``$VAR``), an empty or unknown ``kinds`` entry, and a ``real_bin`` for a
    kind that is not installed.
    """
    if not isinstance(block, dict):
        raise ConfigError("harness_wrappers must be a mapping", schema_path="harness_wrappers")
    if not block:
        return
    reject_unknown_keys(block, "harness_wrappers")
    if "enabled" in block and not isinstance(block["enabled"], bool):
        raise ConfigError(
            f"harness_wrappers.enabled must be a bool, got {block['enabled']!r}", schema_path="harness_wrappers.enabled"
        )
    if "dir" in block:
        _check_path("harness_wrappers.dir", block["dir"])
    if "kinds" in block:
        _check_kinds(block)
    if "signal_scope" in block:
        _check_signal_scope(block)
    if "real_bin" in block:
        _check_real_bin(block)
