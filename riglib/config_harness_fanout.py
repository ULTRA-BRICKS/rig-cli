"""Lenient reader for the ``harness`` fan-out keys, used by ``rig doctor``.

Why a separate module: ``rig doctor`` loads no full config and is documented to run with zero
third-party imports, so it needs its own tolerant read path (PyYAML when present, a stdlib
line scanner otherwise) that is independent of the strict loader and the strict validators.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import config_yaml
from .config_errors import ConfigError
from .config_paths import global_config_path
from .errors import MissingDepError

FAN_OUT_KEYS = ("settings_paths", "discover_config_dirs")

YAML_TRUE = ("true", "yes", "on")  # the YAML 1.1 booleans PyYAML itself accepts
YAML_FALSE = ("false", "no", "off")


def yaml_scalar(raw: str) -> Any:
    text = raw.split("#", 1)[0].strip().strip("'\"")
    if text.lower() in YAML_TRUE:
        return True
    if text.lower() in YAML_FALSE:
        return False
    return text


def indent(line: str) -> int:
    return len(line) - len(line.lstrip())


class _FanOutScanner:
    """Line-at-a-time state machine behind :func:`fan_out_keys_without_yaml`."""

    def __init__(self) -> None:
        self.out: dict[str, Any] = {}
        self.in_harness = False
        self.child_indent: int | None = None
        self.paths: list[str] | None = None

    def feed(self, line: str) -> None:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            return
        if not line[0].isspace():
            # `harness:  # a comment` is still the header — strip the comment like every value
            self.in_harness = stripped.split("#", 1)[0].strip() == "harness:"
            self.child_indent = None
            self.paths = None
            return
        if self.in_harness:
            self._feed_harness_line(line, stripped)

    def _feed_harness_line(self, line: str, stripped: str) -> None:
        if self.paths is not None and stripped.startswith("- "):
            self.paths.append(str(yaml_scalar(stripped[2:])))
            return
        self.paths = None
        if self.child_indent is None:
            self.child_indent = indent(line)
        key, sep, value = stripped.partition(":")
        if not sep or indent(line) != self.child_indent:
            return
        self._take_key(key.strip(), value)

    def _take_key(self, key: str, value: str) -> None:
        if key == "discover_config_dirs":
            self.out[key] = yaml_scalar(value)
        elif key == "settings_paths":
            flow = value.split("#", 1)[0].strip()
            if flow.startswith("[") and flow.endswith("]"):
                self.out[key] = [str(yaml_scalar(v)) for v in flow[1:-1].split(",") if v.strip()]
            else:
                self.paths = self.out.setdefault(key, [])


def fan_out_keys_without_yaml(text: str) -> dict[str, Any]:
    """Extract ``harness.settings_paths`` / ``harness.discover_config_dirs`` with the stdlib only.

    Understands the shapes ``rig setup`` / ``config set`` write and the docs show: a top-level
    ``harness:`` block, ``discover_config_dirs: <bool>``, and ``settings_paths`` as a flow list
    (``[a, b]``) or a block list (``- a``). The block's child indent is whatever its first key
    uses (2 or 4 spaces — a hand-edited file must not lose keys); deeper lines are nested
    mappings and skipped. Anything else is ignored (the caller falls back to the defaults for
    that key) — never a parse error.
    """
    scanner = _FanOutScanner()
    for line in text.splitlines():
        scanner.feed(line)
    return scanner.out


def harness_block(path: Path) -> Any:
    """The ``harness`` mapping of one config file — via PyYAML when present, else a stdlib
    fallback that reads ONLY the two fan-out keys. ``rig doctor`` is documented to run with zero
    third-party imports, so a missing PyYAML must not make it blind to a configured opt-out."""
    try:
        doc = config_yaml.read_yaml_file(path)
    except (ImportError, MissingDepError):
        return fan_out_keys_without_yaml(path.read_text(encoding="utf-8"))
    # an empty / whitespace-only file parses to None, a scalar or a list is not a mapping
    return doc.get("harness") if isinstance(doc, dict) else None


def load_harness_fan_out(repo_root: Path) -> dict[str, Any]:
    """The effective ``harness`` fan-out keys (``settings_paths``, ``discover_config_dirs``) — LENIENT.

    For ``rig doctor``, which loads no config: read the global layer and the repo's ``rig.yaml``
    (when present), merge only the ``harness`` block and validate only the two fan-out keys.
    Without PyYAML the two keys are read by a stdlib fallback (:func:`fan_out_keys_without_yaml`).
    Any failure — an unreadable or non-UTF-8 file, a config that fails validation elsewhere (a
    stale key another rig version added) — yields ``{}``: the built-in defaults, so doctor
    never goes blind because the rest of the config is broken.
    """
    from .config_validate_harness import validate_harness_fan_out

    harness: dict[str, Any] = {}
    for path in (global_config_path(), repo_root / "rig.yaml"):
        if not path.is_file():
            continue
        try:
            block = harness_block(path)
        except (ConfigError, OSError, ValueError):  # ValueError: UnicodeDecodeError
            return {}
        if isinstance(block, dict):
            harness = config_yaml.deep_merge(harness, block)
    fan_out = {k: harness[k] for k in FAN_OUT_KEYS if k in harness}
    try:
        validate_harness_fan_out(fan_out)
    except ConfigError:
        return {}
    return fan_out
