"""Value types shared by the ``rig lint`` bootstrap modules.

Context: :mod:`riglib.lint_bootstrap` plans and applies the repo-local tooling bootstrap; the
planning helpers (:mod:`lint_bootstrap_managers`, :mod:`lint_bootstrap_cache`,
:mod:`lint_bootstrap_config`) and the orchestrator all speak these dataclasses, so they live in one
leaf module that imports nothing from its siblings. Stdlib-only at import time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .lint_discovery_model import PackageManager
from .lint_providers import BootstrapNeed, PinnedPackage
from .plan import InstallPlan

BlockedReason = Literal[
    "ambiguous-package-manager", "manager-not-installed", "no-manifest",
    "no-lock-capable-manager", "disabled-by-config", "read-only-mode", "unsupported-platform",
    "required-policy-unavailable", "conflicting-config",
]
StepState = Literal["applied", "noop", "failed", "blocked"]
StepKind = Literal[
    "add-dev-packages", "install-dependencies", "write-config", "install-tool-cache", "install-toolchain-component",
    "git-exclude",
]
# Steps that write nothing inside the repository (tool cache, ~/.rustup), so CI may run them.
CI_SAFE_KINDS: frozenset[str] = frozenset({"install-tool-cache", "install-toolchain-component"})


@dataclass(frozen=True)
class BootstrapStep:
    # "js-add-dev@<scope>" | "js-install@<scope>"
    # | "write-config@<scope>:<rel>" | "py-cache-venv@<pkg>" | "py-cache-install@<pkg>" | "js-cache-install@<pkg>"
    id: str
    kind: StepKind
    scope: str
    manager: str
    argv: tuple[str, ...]  # () for write-config
    cwd: str  # repo-relative; "." for tool-cache steps, which run inside the cache instead
    packages: tuple[PinnedPackage, ...]
    lock: Literal["lockfile", "n/a"]  # n/a for write-config and tool-cache steps; a manager without a lock is routed to the cache
    writes: tuple[str, ...]  # repo-relative files the step may create/modify; () for tool-cache steps
    cache_dir: str = ""  # install-tool-cache only: absolute venv / npm prefix under the Rig tool cache
    creates: str = ""  # install-tool-cache only: absolute path that must exist afterwards (idempotence marker)
    release: tuple[str, ...] = ()  # install-tool-cache only: (url, sha256, kind) of a pinned release asset instead of argv
    copy: tuple[str, ...] = ()  # install-tool-cache only: (source dir, destination dir) verbatim copy into the cache
    content: str = ""  # install-tool-cache only: file bytes to write at `creates` (the rendered policy)


@dataclass(frozen=True)
class BootstrapBlocked:
    scope: str
    tool: str
    reason: BlockedReason
    detail: str


@dataclass(frozen=True)
class BootstrapPlan:
    steps: tuple[BootstrapStep, ...]
    blocked: tuple[BootstrapBlocked, ...]
    install_plan: InstallPlan  # on_conflict="skip": a file a user wrote is never replaced
    notes: tuple[str, ...]

    @property
    def policy_unavailable(self) -> bool:
        """The Rig-owned policy could not be applied in full: never a green setup, even when nothing was installed."""
        return any(b.reason == "required-policy-unavailable" for b in self.blocked)

    @property
    def setup_incomplete(self) -> bool:
        """Setup cannot make the lint verdict trustworthy (policy missing, or two Oxlint configs in one directory)."""
        return any(b.reason in ("required-policy-unavailable", "conflicting-config") for b in self.blocked)

    def is_empty(self) -> bool:
        """No step => nothing to spawn or write (the idempotent re-run state)."""
        return not self.steps


@dataclass(frozen=True)
class BootstrapOutcome:
    results: tuple[tuple[str, StepState, str], ...]  # (step id, state, detail)
    failed: bool


@dataclass
class Install:
    """Everything one JS package-manager invocation at one workspace root has to do (planner-internal)."""

    pm: PackageManager
    needs: list[BootstrapNeed]
    wanted: dict[str, PinnedPackage]  # missing from the manifest: add exact pins
    unresolved: list[BootstrapNeed]  # declared but binary absent: restore from the lock
