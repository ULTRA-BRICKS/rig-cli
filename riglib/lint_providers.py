"""Provider registry for ``rig lint run``: which checks apply to a repository, and exactly how to run them.

Context: ``lint_discovery`` turns a git worktree into facts (scopes, configured tools, scripts, files);
the provider modules turn those facts into :class:`PlannedCheck` rows (argv, cwd, fix argv, availability)
that ``lint_run`` executes serially. This module is the one stable import surface; the work is split by
concern into focused modules:

* ``lint_providers_model``    data classes, pins, injected seams
* ``lint_providers_registry`` ``register`` / ``providers`` / ``provider`` / ``PROVIDER_IDS``
* ``lint_providers_scope``    tool families, which roles are really configured, scope chains
* ``lint_providers_resolve``  ``resolve_tool`` (repo-local, manager, PATH, Rig cache)
* ``lint_providers_build``    ``PlannedCheck`` builders and missing-tool wording
* ``lint_providers_js`` / ``_python`` / ``_system`` / ``_scripts``  the providers themselves
* ``lint_providers_overlap``  demote a check only when another provably repeats it
* ``lint_providers_needs``    bootstrap needs, unsupported tools
* ``lint_providers_coverage`` files checked vs uncovered
* ``lint_providers_plan``     ``plan_checks``
* ``lint_providers_catalog``  the built-in provider specs

Trust boundary: planning reads the filesystem only to resolve tool binaries and never executes anything.
Argv is always a list (no shell); names that come from repo manifests are re-validated and never interpolated.
"""

from __future__ import annotations

from .lint_providers_build import dependency_install_hint
from .lint_providers_catalog import register_catalog
from .lint_providers_coverage import Coverage, coverage
from .lint_providers_model import (
    PINS,
    PRECEDENCE_BOOTSTRAP,
    PRECEDENCE_BUILTIN,
    PRECEDENCE_CONFIGURED,
    PRECEDENCE_SCRIPT,
    SYSTEM_TOOL_VERSIONS,
    BootstrapNeed,
    BootstrapSpec,
    CheckState,
    PinnedPackage,
    PlanContext,
    PlannedCheck,
    ProviderSpec,
    ToolEnv,
    ToolResolution,
    Unavailable,
    UnavailableState,
)
from .lint_providers_needs import bootstrap_needs, unsupported_tools
from .lint_providers_overlap import resolve_overlaps
from .lint_providers_plan import check_sort_key, plan_checks
from .lint_providers_registry import provider, providers, register
from .lint_providers_resolve import resolve_tool
from .lint_providers_system import chunk_files

register_catalog()

# Imported after the catalog registered itself: the frozenset is a snapshot of the complete built-in set.
from .lint_providers_registry import PROVIDER_IDS  # noqa: E402

__all__ = [
    "PINS",
    "PRECEDENCE_BOOTSTRAP",
    "PRECEDENCE_BUILTIN",
    "PRECEDENCE_CONFIGURED",
    "PRECEDENCE_SCRIPT",
    "PROVIDER_IDS",
    "SYSTEM_TOOL_VERSIONS",
    "BootstrapNeed",
    "BootstrapSpec",
    "CheckState",
    "Coverage",
    "PinnedPackage",
    "PlanContext",
    "PlannedCheck",
    "ProviderSpec",
    "ToolEnv",
    "ToolResolution",
    "Unavailable",
    "UnavailableState",
    "bootstrap_needs",
    "check_sort_key",
    "chunk_files",
    "coverage",
    "dependency_install_hint",
    "plan_checks",
    "provider",
    "providers",
    "register",
    "resolve_overlaps",
    "resolve_tool",
    "unsupported_tools",
]
