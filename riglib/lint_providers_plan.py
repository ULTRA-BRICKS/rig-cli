"""``plan_checks``: every provider x every scope, config/platform policy applied, overlaps resolved, sorted.

The result is deterministic (sorted by scope, category rank, id) so two runs over the same tree print the
same report. Nothing here executes a tool.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from .lint_discovery_model import Discovery, ProjectScope
from .lint_providers_model import PlanContext, PlannedCheck, ProviderSpec, ToolEnv, Unavailable
from .lint_providers_overlap import as_unavailable, resolve_overlaps
from .lint_providers_registry import providers
from .lint_run_config import CATEGORIES, LintConfig


def _root_scope() -> ProjectScope:
    return ProjectScope(
        rel=".", languages=(), package_manager=None, tools=(), scripts=(), typescript=None, files={}, evidence=(),
    )


def _planning_scopes(discovery: Discovery) -> tuple[ProjectScope, ...]:
    """Every discovered scope plus an empty root scope so repo-wide checks (rig-check) always have a home."""
    if discovery.scope(".") is not None:
        return discovery.scopes
    return (_root_scope(), *discovery.scopes)


def _disabled_reason(spec: ProviderSpec, cfg: LintConfig) -> str | None:
    override = cfg.providers.get(spec.id)
    if override is not None and not override.enabled:
        return f"lint.providers.{spec.id}.enabled is false"
    if not cfg.categories.get(spec.category, True):
        return f"lint.categories.{spec.category} is false"
    return None


def apply_policy(spec: ProviderSpec, check: PlannedCheck, cfg: LintConfig, env: ToolEnv) -> PlannedCheck:
    disabled = _disabled_reason(spec, cfg)
    if disabled is not None:
        return as_unavailable(check, Unavailable("not-applicable", "disabled-by-config", disabled))
    already_na = check.unavailable is not None and check.unavailable.state == "not-applicable"
    if spec.platforms is not None and env.system not in spec.platforms and not already_na:
        detail = f"{spec.id} is not supported on {env.system} (supported: {', '.join(sorted(spec.platforms))})"
        return as_unavailable(check, Unavailable("unsupported-platform", "platform-unsupported", detail))
    override = cfg.providers.get(spec.id)
    if override is not None and override.timeout_seconds is not None:
        return replace(check, timeout_s=override.timeout_seconds)
    return check


def _rank(category: str) -> int:
    return CATEGORIES.index(category) if category in CATEGORIES else len(CATEGORIES)


def check_sort_key(check: PlannedCheck) -> tuple[str, int, str]:
    return (check.scope, _rank(check.category), check.id)


Selection = Callable[[PlannedCheck], Unavailable | None]


def _selected(check: PlannedCheck, select: Selection | None) -> PlannedCheck:
    if select is None:
        return check
    excluded = select(check)
    return check if excluded is None else as_unavailable(check, excluded)


def plan_checks(
    discovery: Discovery, cfg: LintConfig, env: ToolEnv, *, config_path: str | None = None,
    select: Selection | None = None,
) -> tuple[PlannedCheck, ...]:
    """``select`` is the run's execution selection (``--only``/``--skip``/``--scope``): a check it excludes is
    ``not-applicable`` BEFORE overlaps are resolved, so a check that will not run can never claim to cover
    (and demote) one that will."""
    planned: list[PlannedCheck] = []
    for scope in _planning_scopes(discovery):
        ctx = PlanContext(discovery, scope, cfg, env, config_path)
        for spec in providers():
            planned.extend(_selected(apply_policy(spec, check, cfg, env), select) for check in spec.plan(ctx))
    return tuple(sorted(resolve_overlaps(planned), key=check_sort_key))
