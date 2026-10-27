"""Strict parsing of the ``lint_checks`` config block (complexity + file-line limits).

Context: ``rig lint check`` and its activation hook read these knobs from the cascaded config
(defaults < ~/.config/rig/config.yaml < ./rig.yaml). The block is valid in BOTH layers, so each
layer is validated alone (:func:`validate_block`) and the merged result is validated again
(:func:`resolve`) because ``warn_above <= error_above`` can only be judged after the cascade.

Fail-closed on purpose: a typo'd threshold must never silently fall back to a default that makes a
failing repo look green, so unknown keys, wrong types, bools posing as ints, nulls and
out-of-range numbers all raise :class:`riglib.config.ConfigError` naming the key and the layer.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .config import ConfigError

MAX_THRESHOLD = 10_000_000
_EXT_RE = re.compile(r"\A[a-z0-9]+\Z")

_TOP_KEYS = ("enabled", "complexity", "file_lines", "ci")
_COMPLEXITY_KEYS = ("enabled", "max_cognitive", "max_cyclomatic")
_FILE_LINES_KEYS = ("enabled", "warn_above", "error_above", "exempt_extensions")


@dataclass(frozen=True)
class LintChecksConfig:
    enabled: bool
    complexity_enabled: bool
    max_cognitive: int
    max_cyclomatic: int
    file_lines_enabled: bool
    warn_above: int
    error_above: int
    exempt_extensions: tuple[str, ...]


DEFAULTS = LintChecksConfig(
    enabled=True,
    complexity_enabled=True,
    max_cognitive=15,
    max_cyclomatic=20,
    file_lines_enabled=True,
    warn_above=1200,
    error_above=2400,
    exempt_extensions=("json",),
)


def _err(path: str, layer: str, what: str, fix: str) -> ConfigError:
    return ConfigError(f"{path} {what} (layer: {layer})", fix=fix, schema_path=path)


def _mapping(value: Any, path: str, layer: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _err(path, layer, f"must be a mapping, got {value!r}", f"write {path} as a YAML mapping")
    return value


def _reject_unknown(block: Mapping[str, Any], allowed: tuple[str, ...], path: str, layer: str) -> None:
    unknown = sorted(str(k) for k in block if k not in allowed)
    if unknown:
        raise ConfigError(
            f"unknown {path} key: {', '.join(unknown)} (layer: {layer})",
            why=f"{', '.join(unknown)} is not a known {path} key",
            fix=f"remove it, or use one of: {', '.join(allowed)}",
            schema_path=f"{path}.{unknown[0]}",
        )


def _check_bool(block: Mapping[str, Any], key: str, path: str, layer: str) -> None:
    if key in block and not isinstance(block[key], bool):
        raise _err(f"{path}.{key}", layer, f"must be a bool (true/false), got {block[key]!r}", "use true or false")


def _check_threshold(block: Mapping[str, Any], key: str, path: str, layer: str) -> None:
    if key not in block:
        return
    value = block[key]
    where = f"{path}.{key}"
    if isinstance(value, bool) or not isinstance(value, int):
        raise _err(where, layer, f"must be an integer >= 1, got {value!r}", "use a whole number such as 15")
    if value < 1 or value > MAX_THRESHOLD:
        raise _err(where, layer, f"must be between 1 and {MAX_THRESHOLD}, got {value!r}", "use a positive whole number")


def _check_extensions(block: Mapping[str, Any], path: str, layer: str) -> None:
    key = "exempt_extensions"
    if key not in block:
        return
    where = f"{path}.{key}"
    value = block[key]
    if not isinstance(value, list):
        raise _err(where, layer, f"must be a list of extensions, got {value!r}", "write e.g. [json]")
    seen: set[str] = set()
    for ext in value:
        if not isinstance(ext, str) or not _EXT_RE.match(ext):
            raise _err(
                where,
                layer,
                f"entries must be lowercase alphanumeric extensions without a leading dot, got {ext!r}",
                "write json, not .json or JSON; empty entries are not allowed",
            )
        if ext in seen:
            raise _err(where, layer, f"contains duplicate extension {ext!r}", "list each extension once")
        seen.add(ext)


def validate_block(block: Any, *, layer: str) -> None:
    """Validate one layer's raw ``lint_checks`` block (shape + per-key values, not cross-key)."""
    if block is None:
        raise _err("lint_checks", layer, "is empty (null)", "write `lint_checks: {}` or remove it")
    top = _mapping(block, "lint_checks", layer)
    _reject_unknown(top, _TOP_KEYS, "lint_checks", layer)
    _check_bool(top, "enabled", "lint_checks", layer)
    if "complexity" in top:
        comp = _mapping(top["complexity"], "lint_checks.complexity", layer)
        _reject_unknown(comp, _COMPLEXITY_KEYS, "lint_checks.complexity", layer)
        _check_bool(comp, "enabled", "lint_checks.complexity", layer)
        _check_threshold(comp, "max_cognitive", "lint_checks.complexity", layer)
        _check_threshold(comp, "max_cyclomatic", "lint_checks.complexity", layer)
    if "file_lines" in top:
        fl = _mapping(top["file_lines"], "lint_checks.file_lines", layer)
        _reject_unknown(fl, _FILE_LINES_KEYS, "lint_checks.file_lines", layer)
        _check_bool(fl, "enabled", "lint_checks.file_lines", layer)
        _check_threshold(fl, "warn_above", "lint_checks.file_lines", layer)
        _check_threshold(fl, "error_above", "lint_checks.file_lines", layer)
        _check_extensions(fl, "lint_checks.file_lines", layer)
    if "ci" in top:
        from . import lint_checks_ci

        lint_checks_ci.validate(top["ci"], layer=layer)


def _layer_of(key: tuple[str, str], layer_blocks: Mapping[str, Any] | None) -> str:
    """The highest-precedence layer that sets ``section.key`` ("default" when none does)."""
    section, name = key
    found = "default"
    for label, block in (layer_blocks or {}).items():
        sub = block.get(section) if isinstance(block, Mapping) else None
        if isinstance(sub, Mapping) and name in sub:
            found = label
    return found


def resolve(data: Mapping[str, Any], layer_blocks: Mapping[str, Any] | None = None) -> LintChecksConfig:
    """Resolve the effective config from the CASCADED config dict (defaults fill the gaps).

    ``layer_blocks`` (label -> that layer's raw block, lowest precedence first) is optional and
    only used to name the layers behind a cross-key violation.
    """
    block = data.get("lint_checks", {})
    validate_block(block, layer="merged")
    comp = block.get("complexity", {})
    fl = block.get("file_lines", {})
    d = DEFAULTS
    cfg = LintChecksConfig(
        enabled=block.get("enabled", d.enabled),
        complexity_enabled=comp.get("enabled", d.complexity_enabled),
        max_cognitive=comp.get("max_cognitive", d.max_cognitive),
        max_cyclomatic=comp.get("max_cyclomatic", d.max_cyclomatic),
        file_lines_enabled=fl.get("enabled", d.file_lines_enabled),
        warn_above=fl.get("warn_above", d.warn_above),
        error_above=fl.get("error_above", d.error_above),
        exempt_extensions=tuple(fl.get("exempt_extensions", d.exempt_extensions)),
    )
    if cfg.warn_above > cfg.error_above:
        warn_layer = _layer_of(("file_lines", "warn_above"), layer_blocks)
        err_layer = _layer_of(("file_lines", "error_above"), layer_blocks)
        raise ConfigError(
            f"lint_checks.file_lines.warn_above ({cfg.warn_above}, layer: {warn_layer}) must be <= "
            f"lint_checks.file_lines.error_above ({cfg.error_above}, layer: {err_layer})",
            fix="lower warn_above or raise error_above",
            schema_path="lint_checks.file_lines.warn_above",
        )
    return cfg
