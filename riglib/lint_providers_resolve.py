"""Find the executable for a tool without running anything.

Resolution order (docs/plans/2026-10-27-universal-lint.md 3.4, amended by acceptance review F6/F7):

1. ``node_modules/.bin/<tool>`` then ``.venv/bin/<tool>``, nearest directory first, walking from the scope
   up to the repository root and never above it.
2. ``uv run --frozen --`` / ``poetry run`` / ``pdm run`` for a tool the Python project declares.
3. PATH, only for system tools and package-manager executables.
4. The Rig-owned pinned tool cache, only for tools Rig itself provisions and only when the project does not
   declare them (a declared-but-uninstalled tool stays "missing": ``npm install`` is the remedy).

A PATH-global Oxlint/Ruff/ESLint/... is never accepted: a teammate or CI could not reproduce it.

Escape rule: running a tool never writes outside the repo, so the safety concern is path traversal, not
where the package manager keeps its store. The rule therefore applies to the location of the ``.bin``
entry (it must sit in the repo under no symlinked directory), not to its final target. A target outside
the repo (pnpm global store, monorepo hoisting) is accepted and noted as ``resolved_outside_repo``.
"""

from __future__ import annotations

import os
import posixpath
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from .lint_discovery_model import Discovery, ProjectScope
from .lint_providers_model import PINS, SYSTEM_TOOL_VERSIONS, PinnedPackage, ToolEnv, ToolResolution
from .lint_providers_scope import TOOL_FAMILY, chain_tool, manager_for, manager_name

SYSTEM_TOOLS = frozenset({"shellcheck", "shfmt", "actionlint", "cargo", "go", "gofmt", "make"})
MANAGER_EXES = frozenset({"npm", "pnpm", "yarn", "bun", "uv", "poetry", "pdm"})
# Provisioned by Rig into its own cache when the project/system lacks them.
CACHEABLE_TOOLS = frozenset({"oxlint", "oxfmt", "ruff", "shellcheck", "shfmt", "actionlint"})
# `uv run` is deliberately absent: it syncs the whole project environment (application runtime dependencies,
# possibly huge or native) just to lint. poetry/pdm `run` use the existing environment and never install.
_MANAGER_RUN: Mapping[str, tuple[str, ...]] = {
    "poetry": ("poetry", "run"),
    "pdm": ("pdm", "run"),
}

Verdict = Literal["absent", "escaped", "ok"]


def walk_up(rel: str) -> tuple[str, ...]:
    """``rel`` and every ancestor up to ``.``; never above the repo root."""
    parts = [p for p in posixpath.normpath(rel).split("/") if p not in ("", ".", "..")]
    return tuple("/".join(parts[:n]) or "." for n in range(len(parts), -1, -1))


def _within(real: str, root_real: str) -> bool:
    return real == root_real or real.startswith(root_real.rstrip(os.sep) + os.sep)


def _symlinked_component(root: Path, path: Path) -> bool:
    """Whether a DIRECTORY between the repo root and ``path`` (exclusive) is a symlink."""
    try:
        parts = path.parent.relative_to(root).parts
    except ValueError:
        return True
    current = root
    for part in parts:
        current = current / part
        if os.path.islink(current):
            return True
    return False


def _usable(path: Path, windows: bool) -> bool:
    real = os.path.realpath(path)
    return os.path.isfile(real) and (windows or os.access(real, os.X_OK))


def _probe(path: Path, root: Path, windows: bool) -> Verdict:
    if not os.path.lexists(path):
        return "absent"
    if _symlinked_component(root, path):
        return "escaped"
    return "ok" if _usable(path, windows) else "absent"


def _local_candidates(tool: str, windows: bool) -> tuple[str, ...]:
    """Where a repo-local executable of ``tool`` may live; a JS tool never resolves through a Python venv."""
    node = (f"node_modules/.bin/{tool}.cmd", f"node_modules/.bin/{tool}") if windows else (f"node_modules/.bin/{tool}",)
    venv = (f".venv/Scripts/{tool}.exe",) if windows else (f".venv/bin/{tool}",)
    return node if TOOL_FAMILY.get(tool) == "js" else (*node, *venv)


def _found_local(path: Path, env: ToolEnv) -> ToolResolution:
    shown = os.path.relpath(path, env.root).replace(os.sep, "/")
    real = os.path.realpath(path)
    note = ""
    if not _within(real, os.path.realpath(env.root)):
        note = f"resolved_outside_repo: {real}"
    return ToolResolution("local-bin", (str(path),), shown, note)


def _resolve_local(tool: str, scope: ProjectScope, env: ToolEnv) -> ToolResolution | None:
    root = env.root
    windows = env.system == "windows"
    # Per directory: node_modules first, then .venv; nearest directory first.
    for directory in walk_up(scope.rel):
        for sub in _local_candidates(tool, windows):
            path = root / sub if directory == "." else root / directory / sub
            verdict = _probe(path, root, windows)
            if verdict == "escaped":
                return ToolResolution("escaped", (), None, f"{path} sits below a symlinked directory")
            if verdict == "ok":
                return _found_local(path, env)
    return None


def _resolve_manager_run(tool: str, scope: ProjectScope, discovery: Discovery, env: ToolEnv) -> ToolResolution | None:
    """``uv run --frozen -- tool`` and friends, only for a Python tool the Python project declares.

    A JS tool (tsc, eslint, ...) in a mixed repo must never go through the Python manager: ``uv run tsc``
    is not a thing, so such a tool is reported missing and ``npm install`` stays the remedy.
    """
    if TOOL_FAMILY.get(tool) != "py":
        return None
    prefix = _MANAGER_RUN.get(manager_name(scope, discovery, "python") or "")
    declared = chain_tool(discovery, scope, tool)
    if prefix is None or declared is None or not declared[1].declared:
        return None
    if env.which(prefix[0]) is None:
        return ToolResolution("missing", (), None, f"{prefix[0]} is not on PATH")
    return ToolResolution("manager-run", (*prefix, tool), None)


def _cache_version(tool: str, pins: Mapping[str, PinnedPackage]) -> str | None:
    pin = pins.get(tool)
    return pin.version if pin is not None else SYSTEM_TOOL_VERSIONS.get(tool)


def _locked_version(tool: str, scope: ProjectScope, discovery: Discovery, env: ToolEnv) -> str | None:
    """The version the Python project itself locks for ``tool``: the cached copy must match the project's pin."""
    from .lint_locked_versions import locked_version

    pm = manager_for(scope, discovery, "python")
    declared = chain_tool(discovery, scope, tool)
    if pm is None or declared is None or not declared[1].declared:
        return None
    return locked_version(env.root if pm.root == "." else env.root / pm.root, tool)


def _resolve_cache(
    tool: str, env: ToolEnv, pins: Mapping[str, PinnedPackage], locked: str | None = None
) -> ToolResolution | None:
    from .lint_tool_cache import find_cached_tool  # lazy: keeps planning free of the installer module

    version = locked or _cache_version(tool, pins)
    found = find_cached_tool(tool, env.environ, version=version, windows=env.system == "windows")
    if found is None:
        return None
    return ToolResolution("rig-cache", (str(found),), str(found))


def _resolve_path(tool: str, env: ToolEnv, pins: Mapping[str, PinnedPackage]) -> ToolResolution:
    found = env.which(tool)
    if found:
        return ToolResolution("path", (found,), found)
    cached = _resolve_cache(tool, env, pins) if tool in CACHEABLE_TOOLS else None
    return cached or ToolResolution("missing", (), None, f"{tool} is not on PATH")


def _cache_allowed(tool: str, declared: bool) -> bool:
    """Ruff is always taken from the pinned cache when the project has no ready copy: running it through the
    project's manager would install the application's whole runtime environment just to lint. Other Rig
    defaults use the cache only when the project does not declare them (a declared, uninstalled tool keeps
    `npm install`/`uv sync` as its remedy)."""
    return tool in CACHEABLE_TOOLS and (tool == "ruff" or not declared)


def resolve_tool(
    tool: str, scope: ProjectScope, discovery: Discovery, env: ToolEnv, *, pins: Mapping[str, PinnedPackage] | None = None
) -> ToolResolution:
    """Find ``tool`` for ``scope`` without executing anything (see the module docstring for the order).

    ``pins`` are the effective pins (config overrides applied); the default is the shipped ``PINS``.
    System tools and package-manager executables come from PATH only: a repo-local ``node_modules/.bin/npm``
    must never stand in for the manager that Rig itself invokes.
    """
    effective = PINS if pins is None else pins
    if tool in SYSTEM_TOOLS or tool in MANAGER_EXES:
        return _resolve_path(tool, env, effective)
    local = _resolve_local(tool, scope, env)
    if local is not None:
        return local
    managed = _resolve_manager_run(tool, scope, discovery, env)
    if managed is not None:
        return managed
    declared = chain_tool(discovery, scope, tool)
    if _cache_allowed(tool, declared is not None and declared[1].declared):
        cached = _resolve_cache(tool, env, effective, _locked_version(tool, scope, discovery, env))
        if cached is not None:
            return cached
    return ToolResolution("missing", (), None, f"{tool} is not in the repository toolchain")
