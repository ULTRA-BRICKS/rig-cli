"""JavaScript/TypeScript providers: Oxlint, Oxfmt (Rig defaults) and the project's own ESLint, Biome, Prettier, tsc.

Argv is fixed here and never read from the repository. Every formatter runs in its no-write form
(`--check`) for the check and only in `fix_argv` in its writing form. Rig never adds `--type-aware`,
`--type-check` or any `--unsafe*`/`--fix-dangerously` flag.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from .lint_providers_build import not_applicable, tool_check
from .lint_providers_files import BIOME_EXTENSIONS, JS_EXTENSIONS, GitignoreWalk, targeted_checks, with_ignored_reach
from .lint_providers_model import (
    PRECEDENCE_BOOTSTRAP,
    PRECEDENCE_CONFIGURED,
    Family,
    PlanContext,
    PlannedCheck,
)
from .lint_providers_scope import (
    gets_default,
    has_config,
    own_tool,
    role_keys,
    scope_chain,
    type_aware_active,
    typescript_declared,
)

if TYPE_CHECKING:
    from .lint_discovery_model import ConfiguredTool, Role
    from .lint_run_config import Category

TSC_RUN = frozenset({"tsc:typecheck"})


def bootstrap_enabled(ctx: PlanContext, family: Family) -> bool:
    flag = ctx.cfg.bootstrap.javascript if family == "js" else ctx.cfg.bootstrap.python
    return ctx.cfg.bootstrap.enabled and flag


def own_or_default(
    ctx: PlanContext, provider_id: str, category: Category, tool: str, family: Family, role: Role,
    covers: Callable[[ConfiguredTool | None], frozenset[str]], args: tuple[str, ...], fix_args: tuple[str, ...] | None,
    runs: Callable[[ConfiguredTool | None], frozenset[str]] = lambda _own: frozenset(),
    walk: GitignoreWalk | None = None,
) -> tuple[PlannedCheck, ...]:
    """Run ``tool`` when the scope configures it; otherwise only as the bootstrap default for ``role``.

    ``walk`` says the tool's directory walk honours ``.gitignore`` (see :func:`with_ignored_reach`).
    """
    own = own_tool(ctx.scope, tool)
    if own is not None:
        checks = (tool_check(ctx, provider_id, category, tool, PRECEDENCE_CONFIGURED, covers(own), args, fix_args, runs=runs(own)),)
    elif not gets_default(ctx.discovery, ctx.scope, family, role):
        return ()
    elif not bootstrap_enabled(ctx, family):
        key = "javascript" if family == "js" else "python"
        detail = f"lint.bootstrap.{key} (or lint.bootstrap.enabled) is false, so no default {role} tool is provided"
        return (not_applicable(provider_id, category, ctx, tool, "disabled-by-config", detail, PRECEDENCE_BOOTSTRAP),)
    else:
        checks = (tool_check(ctx, provider_id, category, tool, PRECEDENCE_BOOTSTRAP, covers(None), args, fix_args, runs=runs(None)),)
    return checks if walk is None else with_ignored_reach(ctx, checks, walk, args, fix_args)


def simple_configured(
    ctx: PlanContext, provider_id: str, tool: str, family: Family, role: Role, args: tuple[str, ...],
    fix_args: tuple[str, ...] | None, walk: GitignoreWalk | None = None,
) -> tuple[PlannedCheck, ...]:
    if own_tool(ctx.scope, tool) is None:
        return ()
    category: Category = role
    covers = role_keys(ctx.scope, family, role)
    checks = (tool_check(ctx, provider_id, category, tool, PRECEDENCE_CONFIGURED, covers, args, fix_args),)
    return checks if walk is None else with_ignored_reach(ctx, checks, walk, args, fix_args)


# The Rig base policy (lint_policy) makes these categories errors. Without any Oxlint config file Oxlint's own
# defaults only WARN on correctness findings and exit 0, so CI/--no-setup runs would pass on a `debugger;`.
BASE_SEVERITY_ARGS = ("-D", "correctness", "-D", "suspicious", "-D", "perf")


def _oxlint_config_governs(ctx: PlanContext) -> bool:
    """Whether any Oxlint config file (scope or ancestor) exists; an existing config is authoritative and untouched."""
    return any(
        tool.config_files
        for member in scope_chain(ctx.discovery, ctx.scope)
        for tool in member.tools if tool.tool == "oxlint"
    )


def _oxlint_args(ctx: PlanContext) -> tuple[str, ...]:
    """Oxlint's policy arguments for the scope.

    A governing config (the user's, or a Rig-applied one) is authoritative: no arguments. Otherwise the policy
    Rig provisioned in its tool cache is passed with ``-c`` (it carries the base severities and the anti-slop
    plugin; ``-c`` disables nested config lookup, which is why it is used only when no config governs). Until
    that is provisioned the base severities are passed as flags, so findings still exit 1.
    """
    if _oxlint_config_governs(ctx):
        return ()
    from .lint_policy_cache import cached_policy_file

    policy = cached_policy_file(ctx.scope, ctx.discovery, ctx.cfg, ctx.env.environ)
    return ("-c", str(policy)) if policy is not None else BASE_SEVERITY_ARGS


def plan_oxlint(ctx: PlanContext) -> tuple[PlannedCheck, ...]:
    def covers(own: ConfiguredTool | None) -> frozenset[str]:
        keys = role_keys(ctx.scope, "js", "lint")
        if own is not None and type_aware_active(ctx, own):
            keys |= role_keys(ctx.scope, "js", "typecheck")
        return keys

    def runs(own: ConfiguredTool | None) -> frozenset[str]:
        return TSC_RUN if own is not None and type_aware_active(ctx, own) else frozenset()

    args = _oxlint_args(ctx)
    return own_or_default(
        ctx, "oxlint", "lint", "oxlint", "js", "lint", covers, args, ("--fix", *args), runs, walk=GitignoreWalk(JS_EXTENSIONS)
    )


def plan_oxfmt(ctx: PlanContext) -> tuple[PlannedCheck, ...]:
    return own_or_default(
        ctx, "oxfmt", "format", "oxfmt", "js", "format",
        lambda _own: role_keys(ctx.scope, "js", "format"), ("--check",), (), walk=GitignoreWalk(JS_EXTENSIONS),
    )


def plan_eslint(ctx: PlanContext) -> tuple[PlannedCheck, ...]:
    own = own_tool(ctx.scope, "eslint")
    if own is None:
        return ()
    if not has_config(own):
        detail = "eslint is mentioned but no ESLint config file or field exists, so Rig has nothing to run"
        return (not_applicable("eslint", "lint", ctx, "eslint", "no-config-file", detail, PRECEDENCE_CONFIGURED),)
    return targeted_checks(
        ctx, "eslint", "lint", "eslint", PRECEDENCE_CONFIGURED, role_keys(ctx.scope, "js", "lint"),
        (), ("--fix",), (".",), JS_EXTENSIONS,
    )


def plan_biome_lint(ctx: PlanContext) -> tuple[PlannedCheck, ...]:
    if own_tool(ctx.scope, "biome") is None:
        return ()
    return targeted_checks(
        ctx, "biome-lint", "lint", "biome", PRECEDENCE_CONFIGURED, role_keys(ctx.scope, "js", "lint"),
        ("lint",), ("lint", "--write"), (".",), BIOME_EXTENSIONS,
    )


def plan_biome_format(ctx: PlanContext) -> tuple[PlannedCheck, ...]:
    if own_tool(ctx.scope, "biome") is None:
        return ()
    return targeted_checks(
        ctx, "biome-format", "format", "biome", PRECEDENCE_CONFIGURED, role_keys(ctx.scope, "js", "format"),
        ("format",), ("format", "--write"), (".",), BIOME_EXTENSIONS,
    )


def plan_prettier(ctx: PlanContext) -> tuple[PlannedCheck, ...]:
    # Prettier honours `.gitignore` AND silently skips an explicit ignored operand (verified, prettier 3): no argument
    # Rig can pass makes it analyze a tracked-but-ignored file, so those files stay uncovered instead of "checked".
    return simple_configured(
        ctx, "prettier", "prettier", "js", "format", ("--check", "."), ("--write", "."),
        walk=GitignoreWalk(JS_EXTENSIONS, explicit_operands=False),
    )


def _tsconfig(ctx: PlanContext) -> str | None:
    target = "tsconfig.json" if ctx.scope.rel == "." else f"{ctx.scope.rel}/tsconfig.json"
    ts = ctx.scope.typescript
    return "tsconfig.json" if ts is not None and target in ts.tsconfigs else None


def plan_tsc(ctx: PlanContext) -> tuple[PlannedCheck, ...]:
    config = _tsconfig(ctx)
    if config is None or not (typescript_declared(ctx) or own_tool(ctx.scope, "tsc") is not None):
        return ()
    return (
        tool_check(
            ctx, "tsc", "typecheck", "tsc", PRECEDENCE_CONFIGURED, role_keys(ctx.scope, "js", "typecheck"),
            ("--noEmit", "-p", config),
        ),
    )
