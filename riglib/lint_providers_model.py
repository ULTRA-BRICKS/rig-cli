"""Data model, pins and injected seams of the lint provider registry (re-exported by ``riglib.lint_providers``).

Context: ``lint_discovery`` turns a worktree into facts; the provider modules turn those facts into
:class:`PlannedCheck` rows (argv, cwd, fix argv, availability) that ``lint_run`` executes serially. The
classes live here so every provider module can build them without importing the registry (no cycle).
Pure data: stdlib only, no I/O.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .lint_discovery import Discovery, ProjectScope, Role
    from .lint_run_config import Category, LintConfig

CheckState = Literal["passed", "failed", "missing-tool", "not-applicable", "unsupported-platform"]
UnavailableState = Literal["missing-tool", "not-applicable", "unsupported-platform"]
Family = Literal["js", "py", "rust", "go"]

PRECEDENCE_SCRIPT, PRECEDENCE_CONFIGURED, PRECEDENCE_BOOTSTRAP, PRECEDENCE_BUILTIN = 0, 1, 2, 3


@dataclass(frozen=True)
class PinnedPackage:
    name: str
    version: str
    ecosystem: Literal["npm", "pypi"]


# Exact versions, verified against the npm / PyPI registries on 2026-10-27; overridable per key by
# `lint.bootstrap.versions`. Bumping a pin is a deliberate code change.
PINS: Mapping[str, PinnedPackage] = {
    "oxlint": PinnedPackage("oxlint", "1.90.0", "npm"),
    "oxfmt": PinnedPackage("oxfmt", "0.75.0", "npm"),
    "oxlint-tsgolint": PinnedPackage("oxlint-tsgolint", "7.0.2005", "npm"),
    "@oxlint/plugins": PinnedPackage("@oxlint/plugins", "1.90.0", "npm"),
    "ruff": PinnedPackage("ruff", "0.17.3", "pypi"),
}

# Release versions of the user-space binaries Rig may provision for shell/workflow checks (verified
# 2026-10-27, universal-lint-versions.md). Checksums and download live with the installer, not here.
SYSTEM_TOOL_VERSIONS: Mapping[str, str] = {"shellcheck": "0.11.0", "shfmt": "3.14.1", "actionlint": "1.7.12"}

Ecosystem = Literal["js", "python", "system", "rustup"]


@dataclass(frozen=True)
class ToolEnv:
    """Injected seams; ``system``/``machine`` use the ``cccc_install.platform_key()`` vocabulary."""

    root: Path
    which: Callable[[str], str | None]
    system: str
    machine: str
    node_version: tuple[int, int, int] | None
    environ: Mapping[str, str]
    depth: int


@dataclass(frozen=True)
class ToolResolution:
    """How a tool was found. ``detail`` carries notes such as ``resolved_outside_repo: <path>``."""

    kind: Literal["local-bin", "manager-run", "path", "rig-cache", "missing", "escaped"]
    argv_prefix: tuple[str, ...]
    path: str | None
    detail: str = ""


@dataclass(frozen=True)
class Unavailable:
    state: UnavailableState
    reason: str
    detail: str


@dataclass(frozen=True)
class PlannedCheck:
    id: str
    provider: str
    category: Category
    scope: str
    tool: str
    precedence: int
    covers: frozenset[str]
    argv: tuple[str, ...] | None
    cwd: str
    unavailable: Unavailable | None
    timeout_s: int | None = None
    fix_argv: tuple[str, ...] | None = None
    result_rule: Literal["exit-zero", "stdout-empty"] = "exit-zero"
    resolution: ToolResolution | None = None
    # "<tool>:<category>" pairs this check provably runs in full besides its own (a project script, or Oxlint
    # with a TypeScript-7 type-check): the only licence for demoting another check of the same scope.
    runs: frozenset[str] = frozenset()
    # Whether this check can analyze a file that git tracks although an ignore rule matches it. A directory walk
    # that honours `.gitignore` (Oxlint/Oxfmt/Ruff/Black/Prettier `.`) and a project script cannot be shown to;
    # a check that names the files explicitly, or a tool that ignores `.gitignore`, can. Coverage counts such a
    # file as checked only through a check with this flag.
    reaches_ignored: bool = True


@dataclass(frozen=True)
class PlanContext:
    discovery: Discovery
    scope: ProjectScope
    cfg: LintConfig
    env: ToolEnv
    # Forwarded to the nested `rig lint check` so it reads the same config file as this run.
    config_path: str | None = None


@dataclass(frozen=True)
class BootstrapSpec:
    ecosystem: Ecosystem
    role: Role
    packages: tuple[str, ...]


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    tool: str
    category: Category
    languages: tuple[str, ...]
    platforms: frozenset[str] | None
    bootstrap: BootstrapSpec | None
    fix_modes: tuple[str, ...]
    plan: Callable[[PlanContext], tuple[PlannedCheck, ...]]


@dataclass(frozen=True)
class BootstrapNeed:
    scope: str
    ecosystem: Ecosystem
    role: Role
    tool: str
    provider: str
    packages: tuple[PinnedPackage, ...]
