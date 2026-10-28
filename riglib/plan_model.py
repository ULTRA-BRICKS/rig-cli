"""Plan data model: :class:`Action`, :class:`InstallPlan` and :class:`PlanError`.

Kept dependency-free so every ``plan_*`` builder (and :mod:`riglib.lint_bootstrap`) can import the
dataclasses without importing the builders that produce them. :mod:`riglib.plan` re-exports all three.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .lint_bootstrap import BootstrapPlan


class PlanError(ValueError):
    """Raised when the plan cannot be built (fail-closed before any write).

    Notably: an unknown item name in the config. The schema promises fail-closed
    validation for unknown items; catching it here (where the catalog is available)
    keeps ``apply`` from silently exiting 0 having installed nothing.
    """


@dataclass
class Action:
    """A single planned install step. ``kind`` selects the runner in ``actions/``."""

    kind: str  # copy_skill | link_skill_harness | link_skill_plugin | install_agent_hook | sync_hook_blurbs | install_dispatcher | install_ci | register_mcp | apply_harness | provision_permissions | provision_execpolicy | install_harness_guard | provision_harness_approval | provision_instruction_policy | provision_pi_extension | register_hook_bridge | provision_omp_idle_ttl | provision_codex_skills_budget | provision_schedule | provision_agents_symlink | provision_agents_orchestrator | provision_agents_autonomous_mode | provision_project_tool | apply_ecosystem_readme | provision_github_ruleset | provision_github_merge | provision_github_ghas | provision_github_actions | provision_github_browser | provision_tmux | provision_global_excludes | provision_spotlight | provision_ship_task_prefix
    category: str
    item: str
    source: Path  # carrier path in the agent-tools checkout
    target: Path  # resolved install destination (expanded, absolute where applicable)
    options: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        # A hook item can emit several actions into one target dir (one per descriptor);
        # append the descriptor name so plan/dry-run lines stay distinguishable.
        descriptor = self.options.get("descriptor")
        dest = self.target / descriptor if descriptor else self.target
        return f"{self.category}/{self.item} → {dest}"


@dataclass
class InstallPlan:
    actions: list[Action] = field(default_factory=list)
    on_conflict: str = "backup"
    notes: list[str] = field(default_factory=list)
    # Every catalog item per extras-scanned category (skills / agent_hooks / ci / mcp), keyed by
    # the on-disk leaf name (`by-type/cli/lazy-imports` → `lazy-imports`) → EVERY source path
    # carrying that leaf (two namespaces may share one — `by-type/cli/x` and `by-stack/…/x` — and
    # the on-disk dir is named by the leaf alone), so drift can tell "rig could have written this"
    # (a catalog orphan, or a rig-installed item another repo's config selects — verified against
    # the source content) from "rig has no opinion on this" (a repo's own workflow → known).
    # ``None`` = no catalog knowledge (a hand-built plan): the scanners then treat every
    # undeclared item as drift, the conservative reading. Filled by :func:`build`. See
    # :mod:`riglib.provenance`.
    catalog_items: dict[str, dict[str, tuple[Path, ...]]] | None = None
    # The `<category>.known` config lists (see :func:`riglib.provenance.known_names_from_config`),
    # the user's side of the same classification. Carried on the plan so `detect` has ONE input
    # channel for provenance (catalog + config), not a kwarg only some callers remember to pass.
    known_names: dict[str, set[str]] | None = None
    # Actions held back from this checkout because they write a committed file the
    # ``tracked_files`` block lands on the default branch instead (riglib/tracked_files.py). Every
    # plan consumer (apply, config set, init, config-web, status/drift) sees them removed from
    # ``actions``; `rig apply commit` runs them in the landing worktree.
    diverted: list[Action] = field(default_factory=list)
    # Set only by a lint-scoped ``plan.build(..., lint_bootstrap=request)``: the structured
    # :class:`riglib.lint_bootstrap.BootstrapPlan` (steps, blocked needs, notes) whose actions are
    # already in ``actions``. ``rig lint`` reports blocked needs from it; apply-mode plans leave it
    # ``None`` so ``rig apply`` / ``rig init`` / config-web plans (and their fingerprints) never change.
    lint_bootstrap: BootstrapPlan | None = None

    def __len__(self) -> int:
        return len(self.actions)
