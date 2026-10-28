"""Plan stage for the repo-local lint tooling bootstrap that bare ``rig lint`` performs.

Context: ``rig lint`` discovers a repo, asks :mod:`riglib.lint_providers` which Rig default tools a
scope lacks, and needs those installed through the SAME ``plan.build`` + ``actions.run_plan`` path
every other rig mutation uses (no second executor). This module is the seam: it turns a
:class:`LintBootstrapRequest` (the discovery facts, as data) into ``lint_bootstrap_step`` actions plus
the Rig-owned Oxlint policy actions, by calling :func:`riglib.lint_bootstrap.build_bootstrap_plan`.

Planning is pure: it reads manifests and never writes or spawns, so ``--plan``, ``--no-setup`` and CI
print exactly what would happen. Whether a plan is EXECUTED is the caller's decision (bare local
``rig lint`` only) and is re-checked by the runner handler, which refuses under CI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

from .plan_model import InstallPlan

if TYPE_CHECKING:
    from .catalog import Catalog
    from .lint_discovery import Discovery
    from .lint_providers import BootstrapNeed, ToolEnv
    from .lint_run_config import LintConfig


@dataclass(frozen=True)
class LintBootstrapRequest:
    """Everything ``rig lint`` already computed that the bootstrap plan derives its steps from."""

    needs: Sequence[BootstrapNeed]
    discovery: Discovery
    cfg: LintConfig
    env: ToolEnv


def plan_lint_bootstrap(plan: InstallPlan, request: LintBootstrapRequest, catalog: Catalog | None) -> None:
    """Append the bootstrap actions + notes for ``request`` and record the structured plan.

    ``catalog`` is optional: without one the anti-slop bundle cannot be resolved and
    ``build_bootstrap_plan`` notes that the policy is deferred to ``rig apply``. A disabled
    ``lint.bootstrap.enabled`` yields no actions (every need is reported ``disabled-by-config``).
    """
    # Lazy: lint_bootstrap imports riglib.plan (and through it this module's package siblings).
    from .lint_bootstrap import build_bootstrap_plan

    bootstrap = build_bootstrap_plan(request.needs, request.discovery, request.cfg, request.env, catalog=catalog)
    plan.actions.extend(bootstrap.install_plan.actions)
    plan.notes.extend(bootstrap.notes)
    plan.lint_bootstrap = bootstrap
