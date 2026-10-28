"""Builders for :class:`PlannedCheck` rows shared by every provider module.

Why central: every provider must report an unavailable tool the same way (state, machine-readable reason,
one human sentence with the remediation) and must resolve tools through the one resolver, so a missing
binary can never silently become a pass.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Literal

from .lint_discovery_model import Discovery, ProjectScope
from .lint_providers_model import (
    PINS,
    PRECEDENCE_BOOTSTRAP,
    PinnedPackage,
    PlanContext,
    PlannedCheck,
    ToolResolution,
    Unavailable,
)
from .lint_providers_resolve import MANAGER_EXES, SYSTEM_TOOLS, resolve_tool
from .lint_providers_scope import TOOL_FAMILY, manager_name

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .lint_run_config import Category, LintConfig


def pinned(key: str, cfg: LintConfig) -> PinnedPackage:
    pin = PINS[key]
    override = cfg.bootstrap.versions.get(key)
    return replace(pin, version=override) if override else pin


def effective_pins(cfg: LintConfig) -> Mapping[str, PinnedPackage]:
    return {key: pinned(key, cfg) for key in PINS}


def build(
    provider_id: str,
    category: Category,
    scope: ProjectScope,
    tool: str,
    precedence: int,
    covers: frozenset[str],
    *,
    argv: tuple[str, ...] | None = None,
    fix_argv: tuple[str, ...] | None = None,
    unavailable: Unavailable | None = None,
    result_rule: Literal["exit-zero", "stdout-empty"] = "exit-zero",
    resolution: ToolResolution | None = None,
    suffix: str = "",
    check_id: str | None = None,
    runs: frozenset[str] = frozenset(),
    reaches_ignored: bool = True,
) -> PlannedCheck:
    return PlannedCheck(
        id=check_id or f"{provider_id}@{scope.rel}{suffix}",
        provider=provider_id,
        category=category,
        scope=scope.rel,
        tool=tool,
        precedence=precedence,
        covers=covers,
        argv=None if unavailable is not None else argv,
        cwd=scope.rel,
        unavailable=unavailable,
        fix_argv=None if unavailable is not None else fix_argv,
        result_rule=result_rule,
        resolution=resolution,
        runs=runs,
        reaches_ignored=reaches_ignored,
    )


def not_applicable(
    provider_id: str, category: Category, ctx: PlanContext, tool: str, reason: str, detail: str, precedence: int
) -> PlannedCheck:
    unavailable = Unavailable("not-applicable", reason, detail)
    return build(provider_id, category, ctx.scope, tool, precedence, frozenset(), unavailable=unavailable)


def _manager_install(ctx: PlanContext, tool: str) -> str:
    manager = manager_name(ctx.scope, ctx.discovery, "python" if TOOL_FAMILY.get(tool) == "py" else "js")
    install = {"uv": "uv sync", "poetry": "poetry install", "pdm": "pdm install"}.get(manager or "")
    if install is None and manager:
        install = f"{manager.replace('-berry', '')} install"
    return f"run `{install}`" if install else "install the project's dev dependencies"


_PROVISIONED_SYSTEM = frozenset({"shellcheck", "shfmt", "actionlint"})


def install_hint(ctx: PlanContext, tool: str, precedence: int) -> tuple[str, str]:
    if tool in _PROVISIONED_SYSTEM:
        detail = f"{tool} is on neither PATH nor the Rig tool cache; run `rig lint` locally (without --no-setup) to provision a pinned copy, or install it and put it on PATH"
        return "tool-not-installed", detail
    if tool in SYSTEM_TOOLS or tool in MANAGER_EXES:
        return "tool-not-installed", f"install {tool} and put it on PATH, then rerun `rig lint`"
    if precedence == PRECEDENCE_BOOTSTRAP or tool == "ruff":
        detail = f"pinned {tool} is not installed; run `rig lint` locally (without --no-setup) to provision it without installing the project's runtime environment"
        return "not-bootstrapped", detail
    hint = _manager_install(ctx, tool)
    return "tool-not-installed", f"{tool} is configured for this project but not installed; {hint}, then rerun `rig lint`"


def missing(ctx: PlanContext, tool: str, precedence: int, resolution: ToolResolution) -> Unavailable:
    if resolution.kind == "escaped":
        return Unavailable("missing-tool", "symlink-escape", f"{resolution.detail}; refusing to run a binary outside the repository")
    reason, detail = install_hint(ctx, tool, precedence)
    return Unavailable("missing-tool", reason, detail)


def tool_check(
    ctx: PlanContext,
    provider_id: str,
    category: Category,
    tool: str,
    precedence: int,
    covers: frozenset[str],
    args: tuple[str, ...],
    fix_args: tuple[str, ...] | None = None,
    *,
    result_rule: Literal["exit-zero", "stdout-empty"] = "exit-zero",
    suffix: str = "",
    runs: frozenset[str] = frozenset(),
    reaches_ignored: bool = True,
) -> PlannedCheck:
    resolution = resolve_tool(tool, ctx.scope, ctx.discovery, ctx.env, pins=effective_pins(ctx.cfg))
    if resolution.kind in ("missing", "escaped"):
        return build(
            provider_id, category, ctx.scope, tool, precedence, covers,
            unavailable=missing(ctx, tool, precedence, resolution), resolution=resolution, suffix=suffix, runs=runs,
            reaches_ignored=reaches_ignored,
        )
    prefix = resolution.argv_prefix
    return build(
        provider_id, category, ctx.scope, tool, precedence, covers,
        argv=(*prefix, *args),
        fix_argv=None if fix_args is None else (*prefix, *fix_args),
        result_rule=result_rule, resolution=resolution, suffix=suffix, runs=runs, reaches_ignored=reaches_ignored,
    )


def dependency_install_hint(discovery: Discovery, scope_rel: str) -> str:
    """Recovery sentence for a check that failed only because the scope's declared dependencies are absent."""
    scope = discovery.scope(scope_rel)
    manager = manager_name(scope, discovery, "js") if scope is not None else None
    command = f"{manager.replace('-berry', '')} install" if manager else "install the project's dependencies"
    where = "" if scope_rel == "." else f" in {scope_rel}"
    return f"the scope's declared dependencies are not installed; run `{command}`{where}, then rerun `rig lint`"
