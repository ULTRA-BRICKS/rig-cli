"""Explicit automatic-JSX-runtime evidence from ``tsconfig.json`` (JSON/JSONC only, never executed).

Context: under the automatic runtime (``compilerOptions.jsx`` = ``react-jsx`` / ``react-jsxdev``) JSX no
longer needs ``React`` in scope, so Oxlint's ``react/react-in-jsx-scope`` is a false positive that
pushes users to add an unneeded ``import React``
(https://oxc.rs/docs/guide/usage/linter/rules/react/react-in-jsx-scope). The Rig-rendered policy turns
the rule off only on this evidence. A tsconfig inherits ``jsx`` through a relative ``extends`` chain; the
chain is bounded, package-name ``extends`` (resolved through node_modules) is not followed, and any
unreadable or malformed file in the chain means no evidence. Repository content is attacker-controlled:
reads are size-capped and never follow symlinks.
"""

from __future__ import annotations

import os
from pathlib import Path

from .lint_discovery_io import MAX_MANIFEST_BYTES, parse_jsonc

AUTOMATIC_JSX_VALUES = frozenset({"react-jsx", "react-jsxdev"})
MAX_EXTENDS_DEPTH = 8


def _read_json_object(path: Path) -> dict[str, object] | None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            return None
        parsed = parse_jsonc(raw.decode("utf-8"))
    except (OSError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _extends_targets(document: dict[str, object], directory: Path) -> list[Path]:
    """Relative ``extends`` entries (string or list), later entries overriding earlier ones."""
    value = document.get("extends")
    entries = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    targets: list[Path] = []
    for entry in entries:
        if not isinstance(entry, str) or not entry.startswith("."):
            continue
        target = directory / entry
        targets.append(target if target.suffix == ".json" else target.with_name(target.name + ".json"))
    return targets


class _Malformed(Exception):
    """A file in the extends chain is unreadable, malformed, cyclic or too deep: no evidence at all."""


def _jsx_of(path: Path, depth: int, seen: frozenset[Path]) -> str | None:
    if depth > MAX_EXTENDS_DEPTH or path in seen:
        raise _Malformed
    document = _read_json_object(path)
    if document is None:
        raise _Malformed
    options = document.get("compilerOptions")
    own = options.get("jsx") if isinstance(options, dict) else None
    if isinstance(own, str):
        return own.lower()
    inherited: str | None = None
    for target in _extends_targets(document, path.parent):
        found = _jsx_of(target, depth + 1, seen | {path})
        inherited = found if found is not None else inherited
    return inherited


def tsconfig_jsx(tsconfig: Path) -> str | None:
    """The effective lowercase ``compilerOptions.jsx`` of ``tsconfig``, or ``None`` (absent / malformed)."""
    try:
        return _jsx_of(tsconfig, 0, frozenset())
    except (_Malformed, ValueError):
        return None


def automatic_jsx_runtime(root: Path, scope_rel: str) -> bool:
    """Whether the nearest ``tsconfig.json`` (scope directory up to ``root``) selects the automatic runtime."""
    directory = root if scope_rel == "." else root / scope_rel
    while True:
        candidate = directory / "tsconfig.json"
        if candidate.is_file():
            return tsconfig_jsx(candidate) in AUTOMATIC_JSX_VALUES
        if directory == root or directory == directory.parent:
            return False
        directory = directory.parent
