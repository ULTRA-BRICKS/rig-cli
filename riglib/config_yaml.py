"""Single-file YAML reading and the deep dict merge the config cascade is built on.

Why a separate module: both the cascade loader and the one-file `config get|set` commands read YAML
and merge mappings, and neither should depend on the other. ``yaml`` is imported lazily so
``rig --help``/``doctor`` work even if PyYAML is missing; reading a file without it raises the
structured :class:`riglib.errors.MissingDepError` (exit 127), never a bare ``ModuleNotFoundError``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config_errors import ConfigError


def _import_yaml() -> Any:
    """Import PyYAML (a declared dependency) or raise the actionable missing-dependency error."""
    try:
        import yaml  # lazy: keeps `rig --help` dependency-free
    except ImportError as exc:
        from .errors import MissingDepError

        raise MissingDepError(
            what="PyYAML is required to read rig config files",
            why="the `yaml` module is not importable in this Python environment (pyyaml is a declared rig dependency)",
            fix="reinstall rig (`uv tool install --reinstall <rig source>`) or run `pip install pyyaml`",
        ) from exc
    return yaml


def read_yaml_file(path: Path) -> dict[str, Any]:
    """Parse a single YAML config file to a dict (fail-closed). Lazy yaml import; empty → {}.

    Public so the targeted `config get|set` commands can read ONE file (not the cascade) and
    inherit the same error handling: YAML syntax errors and a non-mapping top level both raise
    :class:`ConfigError` rather than leaking a PyYAML traceback or a list/scalar.
    """
    yaml = _import_yaml()

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        # fail closed with the usual error message, not a PyYAML traceback
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"config {path} must be a YAML mapping, got {type(data).__name__}")
    return data


def deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """Recursive dict merge; over wins. Lists/scalars replace wholesale."""
    out = dict(base)
    for k, v in over.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out
