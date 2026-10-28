"""Strict parsing of the ``lint`` config block that steers ``rig lint run`` (the aggregator).

Context: ``rig lint`` aggregates every applicable check (see ``docs/lint-run.md``). This block
holds only switches, timeouts and exact tool versions -- NEVER commands or argv. A committed
``rig.yaml`` is attacker-reachable input (a cloned repo), so a provider's argv is fixed in
``lint_providers`` and the block can merely turn a provider on/off or bound its runtime.

The block is valid in BOTH config layers (global ``~/.config/rig/config.yaml`` and the repo
``rig.yaml``); the repo wins per key. It is read-only configuration: there is no ``rig status``
area and nothing is reconciled by ``rig apply``.

Trust boundary: stdlib only at import time. Provider ids and pin keys are validated against
``lint_providers`` through a lazy import (that module imports this one for ``LintConfig``).
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Mapping

from .config_errors import ConfigError

Category = Literal["lint", "typecheck", "format", "limits", "scripts"]
# Rank order: also the sort order of checks inside one scope.
CATEGORIES: tuple[Category, ...] = ("lint", "typecheck", "format", "limits", "scripts")

MIN_TIMEOUT, MAX_TIMEOUT = 1, 3600
DEFAULT_TIMEOUT = 300
# Exact semver (optionally with a pre-release/build suffix): a range would make the lockfile-updating
# bootstrap non-reproducible, which is the whole reason pins exist.
EXACT_VERSION_RE = re.compile(r"\A\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?\Z")

_TOP_KEYS = ("enabled", "timeout_seconds", "categories", "bootstrap", "providers")
_BOOTSTRAP_KEYS = ("enabled", "javascript", "python", "versions")
_PROVIDER_KEYS = ("enabled", "timeout_seconds")


@dataclass(frozen=True)
class ProviderOverride:
    enabled: bool = True
    timeout_seconds: int | None = None


@dataclass(frozen=True)
class BootstrapConfig:
    enabled: bool
    javascript: bool
    python: bool
    versions: Mapping[str, str]


@dataclass(frozen=True)
class LintConfig:
    enabled: bool
    timeout_seconds: int
    categories: Mapping[Category, bool]
    bootstrap: BootstrapConfig
    providers: Mapping[str, ProviderOverride]
    lint_checks_enabled: bool
    linters_enabled: bool
    linters_rules: Mapping[str, object]


DEFAULTS = LintConfig(
    enabled=True,
    timeout_seconds=DEFAULT_TIMEOUT,
    categories=MappingProxyType({name: True for name in CATEGORIES}),
    bootstrap=BootstrapConfig(enabled=True, javascript=True, python=True, versions=MappingProxyType({})),
    providers=MappingProxyType({}),
    lint_checks_enabled=True,
    linters_enabled=True,
    linters_rules=MappingProxyType({}),
)


def _err(path: str, layer: str, what: str, fix: str) -> ConfigError:
    return ConfigError(f"{path} {what} (layer: {layer})", fix=fix, schema_path=path)


def _mapping(value: Any, path: str, layer: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _err(path, layer, f"must be a mapping, got {value!r}", f"write {path} as a YAML mapping")
    return value


def _reject_unknown(block: Mapping[str, Any], allowed: tuple[str, ...], path: str, layer: str) -> None:
    unknown = sorted(str(key) for key in block if key not in allowed)
    if unknown:
        raise ConfigError(
            f"unknown {path} key: {unknown[0]} (layer: {layer})",
            why=f"allowed keys: {', '.join(allowed)}",
            fix=f"remove {path}.{unknown[0]}",
            schema_path=f"{path}.{unknown[0]}",
        )


def _check_bool(block: Mapping[str, Any], key: str, path: str, layer: str) -> None:
    if key in block and not isinstance(block[key], bool):
        raise _err(f"{path}.{key}", layer, f"must be a bool (true/false), got {block[key]!r}", "use true or false")


def _check_timeout(block: Mapping[str, Any], path: str, layer: str) -> None:
    if "timeout_seconds" not in block:
        return
    value = block["timeout_seconds"]
    where = f"{path}.timeout_seconds"
    # bool is an int subclass: `timeout_seconds: true` must not pass as 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise _err(where, layer, f"must be a whole number of seconds, got {value!r}", "use an integer such as 300")
    if not MIN_TIMEOUT <= value <= MAX_TIMEOUT:
        raise _err(where, layer, f"must be between {MIN_TIMEOUT} and {MAX_TIMEOUT}, got {value!r}",
                   "use a number of seconds in range")


def _check_categories(block: Mapping[str, Any], layer: str) -> None:
    cats = _mapping(block, "lint.categories", layer)
    _reject_unknown(cats, CATEGORIES, "lint.categories", layer)
    for name in CATEGORIES:
        _check_bool(cats, name, "lint.categories", layer)


def _check_versions(block: Any, layer: str) -> None:
    from .lint_providers import PINS

    versions = _mapping(block, "lint.bootstrap.versions", layer)
    for key, value in versions.items():
        path = f"lint.bootstrap.versions.{key}"
        if key not in PINS:
            raise _err(path, layer, f"is not a pinned package (known: {', '.join(sorted(PINS))})",
                       "use one of the listed package names as the key")
        if not isinstance(value, str) or not EXACT_VERSION_RE.match(value):
            raise _err(path, layer, f"must be an exact version like 1.2.3, got {value!r}",
                       "pin one exact version; ranges (^, ~, >=, latest) are not reproducible")


def _check_bootstrap(block: Any, layer: str) -> None:
    boot = _mapping(block, "lint.bootstrap", layer)
    _reject_unknown(boot, _BOOTSTRAP_KEYS, "lint.bootstrap", layer)
    for key in ("enabled", "javascript", "python"):
        _check_bool(boot, key, "lint.bootstrap", layer)
    if "versions" in boot:
        _check_versions(boot["versions"], layer)


def _check_providers(block: Any, layer: str) -> None:
    from .lint_providers import PROVIDER_IDS

    providers = _mapping(block, "lint.providers", layer)
    for pid, override in providers.items():
        path = f"lint.providers.{pid}"
        if pid not in PROVIDER_IDS:
            raise _err(path, layer, f"is not a known provider (known: {', '.join(sorted(PROVIDER_IDS))})",
                       "use one of the listed provider ids")
        body = _mapping(override, path, layer)
        _reject_unknown(body, _PROVIDER_KEYS, path, layer)
        _check_bool(body, "enabled", path, layer)
        _check_timeout(body, path, layer)


def validate_block(block: Any, *, layer: str) -> None:
    """Validate one layer's raw ``lint`` block (shape + per-key values)."""
    if block is None:
        raise _err("lint", layer, "is empty (null)", "write `lint: {}` or remove it")
    top = _mapping(block, "lint", layer)
    _reject_unknown(top, _TOP_KEYS, "lint", layer)
    _check_bool(top, "enabled", "lint", layer)
    _check_timeout(top, "lint", layer)
    if "categories" in top:
        _check_categories(top["categories"], layer)
    if "bootstrap" in top:
        _check_bootstrap(top["bootstrap"], layer)
    if "providers" in top:
        _check_providers(top["providers"], layer)


def _bool_or(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _linters_facts(data: Mapping[str, Any]) -> tuple[bool, Mapping[str, object]]:
    """``linters.enabled`` / ``linters.rules`` from the cascaded dict; shape errors belong to the
    ``linters`` validator (``config.validate``), so a malformed value falls back to the default here."""
    linters = data.get("linters")
    if not isinstance(linters, Mapping):
        return DEFAULTS.linters_enabled, DEFAULTS.linters_rules
    rules = linters.get("rules")
    return (
        _bool_or(linters.get("enabled"), DEFAULTS.linters_enabled),
        MappingProxyType(dict(rules)) if isinstance(rules, Mapping) else DEFAULTS.linters_rules,
    )


def resolve(data: Mapping[str, Any], layer_blocks: Mapping[str, Any] | None = None) -> LintConfig:
    """Resolve the effective config from the CASCADED config dict (defaults fill the gaps).

    ``layer_blocks`` is part of the frozen contract and mirrors ``lint_checks_config.resolve``; this
    block has no cross-key rule, so no error ever needs to name the layers behind a merged value.
    """
    block = data.get("lint", {})
    validate_block(block, layer="merged")
    cats = block.get("categories", {})
    boot = block.get("bootstrap", {})
    d = DEFAULTS
    providers = {
        pid: ProviderOverride(
            enabled=body.get("enabled", True),
            timeout_seconds=body.get("timeout_seconds"),
        )
        for pid, body in block.get("providers", {}).items()
    }
    linters_enabled, linters_rules = _linters_facts(data)
    lint_checks = data.get("lint_checks")
    return LintConfig(
        enabled=block.get("enabled", d.enabled),
        timeout_seconds=block.get("timeout_seconds", d.timeout_seconds),
        categories=MappingProxyType({name: cats.get(name, True) for name in CATEGORIES}),
        bootstrap=BootstrapConfig(
            enabled=boot.get("enabled", d.bootstrap.enabled),
            javascript=boot.get("javascript", d.bootstrap.javascript),
            python=boot.get("python", d.bootstrap.python),
            versions=MappingProxyType(dict(boot.get("versions", {}))),
        ),
        providers=MappingProxyType(providers),
        lint_checks_enabled=_bool_or(
            lint_checks.get("enabled") if isinstance(lint_checks, Mapping) else None, d.lint_checks_enabled),
        linters_enabled=linters_enabled,
        linters_rules=linters_rules,
    )


def _raw_layers_only(layers: list[tuple[str, Path]]) -> LintConfig:
    """Resolve from the raw layers, validating only ``lint`` (every other block is ignored)."""
    from .config_yaml import deep_merge, read_yaml_file

    merged: dict[str, Any] = {}
    for label, path in layers:
        data = read_yaml_file(path)
        if "lint" in data:
            validate_block(data["lint"], layer=label)
        picked = {key: data[key] for key in ("lint", "lint_checks", "linters") if key in data}
        merged = deep_merge(merged, picked)
    return resolve(merged)


def load_config(cwd: str, config_path: str | None) -> LintConfig:
    """Cascaded ``lint`` block (defaults -> global -> repo rig.yaml), validated.

    Same leniency as ``lint_check.load_config``: ``rig lint`` also runs where a newer/older rig left
    an unrelated block this version rejects, and one typo elsewhere must not disable linting. An
    error inside the ``lint`` block (or an unreadable file) stays fatal.
    """
    from .config import load
    from .detect import detect_environment
    from .lint_check import _layer_files  # the one definition of "which files does load() read"

    env = detect_environment(Path(cwd).resolve())
    explicit = None
    if config_path:
        cp = Path(config_path)
        explicit = (cp if cp.is_absolute() else env.repo_root / cp).resolve()
    try:
        loaded = load(env.repo_root, explicit_config=explicit, include_repo=env.is_git_repo or explicit is not None)
        return resolve(loaded.data)
    except ConfigError as exc:
        if exc.schema_path.split(".")[0] == "linters":
            # `linters.rules` feeds the bootstrap policy: ignoring its error would crash or mis-plan later.
            raise
        cfg = _raw_layers_only(_layer_files(env, explicit))
        print(f"warning: ignoring a config error outside lint: {exc}", file=sys.stderr)
        return cfg
