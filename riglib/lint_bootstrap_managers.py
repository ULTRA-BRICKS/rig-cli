"""Package-manager commands, manager selection and manifest facts for the ``rig lint`` bootstrap.

Context: the exact add-dev / frozen-install argv per JS manager (plan D7: exact pins, lifecycle
scripts disabled, lockfile updated), the rule that picks the manager (the one repo evidence selects,
never a substitute), and the read-only manifest probes the planner needs. Python has no row here on
purpose: Ruff is never added to a project's ``pyproject.toml``/lock or run through a syncing
``uv run`` -- it lives in the Rig tool cache (:mod:`lint_bootstrap_cache`) so linting never installs,
nor changes the dependency set of, the application. Nothing here writes or spawns.
Stdlib-only at import time.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Callable, Literal, Sequence

from .lint_bootstrap_model import BootstrapBlocked
from .lint_discovery_model import PackageManager, ProjectScope, join
from .lint_providers import PinnedPackage
from .lint_run_config import LintConfig

LOCKFILE_DEFAULT = {
    "npm": "package-lock.json", "pnpm": "pnpm-lock.yaml", "yarn": "yarn.lock", "yarn-berry": "yarn.lock",
    "bun": "bun.lock",
}
MANIFEST = "package.json"
_EXECUTABLE = {"yarn-berry": "yarn"}
_NPM_DEP_BLOCKS = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")

_ADD_DEV: dict[str, tuple[str, ...]] = {
    "npm": ("npm", "install", "--save-dev", "--save-exact", "--ignore-scripts", "--no-audit", "--no-fund"),
    "pnpm": ("pnpm", "add", "--save-dev", "--save-exact", "--ignore-scripts"),
    "yarn": ("yarn", "add", "--dev", "--exact", "--ignore-scripts"),
    "yarn-berry": ("yarn", "add", "--dev", "--exact", "--mode=skip-build"),
    "bun": ("bun", "add", "--dev", "--exact", "--ignore-scripts"),
}
# Managers whose add command refuses to target a workspace root without an explicit flag.
_WORKSPACE_FLAG = {"pnpm": "-w", "yarn": "-W"}

_FROZEN_INSTALL: dict[str, tuple[str, ...]] = {
    "npm": ("npm", "ci", "--ignore-scripts"),
    "pnpm": ("pnpm", "install", "--frozen-lockfile", "--ignore-scripts"),
    "yarn": ("yarn", "install", "--frozen-lockfile", "--ignore-scripts"),
    "yarn-berry": ("yarn", "install", "--immutable", "--mode=skip-build"),
    "bun": ("bun", "install", "--frozen-lockfile", "--ignore-scripts"),
}


def _spec(pkg: PinnedPackage) -> str:
    return f"{pkg.name}@{pkg.version}"


def manager_argv(manager: str, packages: Sequence[PinnedPackage], *, workspace_root: bool) -> tuple[str, ...]:
    """The add-dev command: exact pins, lifecycle scripts disabled, lockfile updated.

    ``workspace_root`` selects the workspace-root flag (``pnpm -w`` / ``yarn -W``) for managers that
    refuse to add to a workspace root without it; it is ignored by the others.
    """
    base = _ADD_DEV.get(manager)
    if base is None:
        raise ValueError(f"no bootstrap command for package manager {manager!r}")
    flag = (_WORKSPACE_FLAG[manager],) if workspace_root and manager in _WORKSPACE_FLAG else ()
    return (*base, *flag, *(_spec(p) for p in packages))


def frozen_install_argv(manager: str) -> tuple[str, ...]:
    """Install what the lockfile already pins (a declared tool whose binary is not installed yet)."""
    return _FROZEN_INSTALL[manager]


# ── manager selection ──────────────────────────────────────────────────────────────


def manager_for(scope: ProjectScope, ecosystem: Literal["js", "python"]) -> PackageManager | None:
    """The scope's manager of ``ecosystem``; a JS repo with loose Python scripts has no Python one."""
    for pm in (scope.package_manager, *scope.other_managers):
        if pm is not None and pm.ecosystem == ecosystem:
            return pm
    return None


def _not_installed(scope: str, executable: str, pm: PackageManager) -> BootstrapBlocked:
    return BootstrapBlocked(
        scope, "", "manager-not-installed",
        f"{executable} selects this repo ({pm.source}) but is not on PATH; install {executable} "
        "(Rig never falls back to another package manager)",
    )


def select_js_manager(
    scope: ProjectScope, which: Callable[[str], str | None]
) -> PackageManager | BootstrapBlocked:
    """Accept the manager discovery chose (packageManager field > one lockfile > workspace marker > npm).

    Conflicting evidence stays blocked, and a manager that is not on PATH is never swapped for
    another one: installing with a different tool would fork the lockfile. ``BootstrapBlocked.tool``
    is left empty; the caller names the tool that needed the install.
    """
    pm = manager_for(scope, "js")
    if pm is None:
        return BootstrapBlocked(scope.rel, "", "no-manifest", "no package.json owns this scope; Rig never creates one")
    if not pm.name:
        candidates = ", ".join(pm.ambiguous) or "several"
        return BootstrapBlocked(
            scope.rel, "", "ambiguous-package-manager",
            f"conflicting package-manager evidence ({candidates}); set the `packageManager` field in package.json "
            "or delete the stale lockfile, then re-run `rig lint`",
        )
    executable = _EXECUTABLE.get(pm.name, pm.name)
    return pm if which(executable) is not None else _not_installed(scope.rel, executable, pm)


def route_js_manager(
    scope: ProjectScope, which: Callable[[str], str | None]
) -> PackageManager | BootstrapBlocked | None:
    """The JS manager for a manifest-modifying install, a block, or ``None`` = no package.json (tool cache)."""
    return None if manager_for(scope, "js") is None else select_js_manager(scope, which)


# ── manifest facts ─────────────────────────────────────────────────────────────────


def dir_of(base: Path, rel: str) -> Path:
    return base if rel == "." else base / rel


def read_npm_manifest(directory: Path) -> dict[str, object] | None:
    """The parsed ``package.json`` object; ``None`` when missing, a symlink, unreadable or malformed."""
    manifest = directory / "package.json"
    if manifest.is_symlink() or not manifest.is_file():
        return None
    try:
        doc = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def read_npm_deps(directory: Path) -> dict[str, str] | None:
    """Declared dependency names -> spec; ``None`` when the manifest is missing/unreadable/malformed."""
    doc = read_npm_manifest(directory)
    if doc is None:
        return None
    out: dict[str, str] = {}
    for block in _NPM_DEP_BLOCKS:
        deps = doc.get(block)
        if isinstance(deps, dict):
            out.update({str(k): str(v) for k, v in deps.items()})
    return out


def has_workspaces(directory: Path, manager: str) -> bool:
    if manager == "pnpm":
        return (directory / "pnpm-workspace.yaml").is_file()
    doc = read_npm_manifest(directory)
    return doc is not None and "workspaces" in doc


def declared_names(root: Path, scope: ProjectScope, pm: PackageManager) -> set[str]:
    """Dependency names declared by the manifest at the manager root and at the scope."""
    names: set[str] = set()
    for rel in {pm.root, scope.rel}:
        names.update(read_npm_deps(dir_of(root, rel)) or {})
    return names


def manifest_blocker(root: Path, pm: PackageManager, scope: str, tool: str) -> BootstrapBlocked | None:
    directory = dir_of(root, pm.root)
    path = directory / MANIFEST
    shown = join(pm.root, MANIFEST)
    if path.is_symlink() or not path.is_file():
        return BootstrapBlocked(scope, tool, "no-manifest", f"{shown} does not exist; Rig never creates a manifest")
    if read_npm_deps(directory) is None:
        return BootstrapBlocked(scope, tool, "no-manifest", f"{shown} is not valid JSON; fix it first")
    return None


def pin(pkg: PinnedPackage, cfg: LintConfig) -> PinnedPackage:
    override = cfg.bootstrap.versions.get(pkg.name)
    return replace(pkg, version=override) if override else pkg
