"""Project scripts (``package.json`` scripts, ``Makefile`` targets) as checks.

Only allow-listed script names reach this module (discovery decides); here the command is turned into
an argv (``<pm> run <name>``, ``make -C . -- <target>``) and gated: a script that re-enters ``rig lint``,
that rewrites files, or that names a formatter without a read-only form is never run. A script claims only
the (tool, role) pairs it provably runs in full (``ProjectScript.covers_roles``); that is the sole licence
to demote Rig's own check of the same tool, so a script called ``lint`` never hides the Python or the
second JavaScript linter of a mixed repo.
"""

from __future__ import annotations

import re

from .lint_discovery_model import ProjectScope, ProjectScript, Role
from .lint_providers_build import build, missing
from .lint_providers_model import (
    PRECEDENCE_BUILTIN,
    PRECEDENCE_SCRIPT,
    PlanContext,
    PlannedCheck,
    Unavailable,
)
from .lint_providers_resolve import resolve_tool
from .lint_providers_scope import TOOL_FAMILY, manager_name, role_keys

_SCRIPT_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9:_.-]*\Z")
_ROLES: tuple[Role, ...] = ("lint", "format", "typecheck")


def _covered_keys(script: ProjectScript, scope: ProjectScope) -> frozenset[str]:
    out: set[str] = set()
    for pair in script.covers_roles:
        tool = pair.partition(":")[0]
        family = TOOL_FAMILY.get(tool)
        for role in _ROLES:
            if family is not None and pair == f"{tool}:{role}":
                out |= role_keys(scope, family, role)
    return frozenset(out)


def _gate(ctx: PlanContext, script: ProjectScript) -> tuple[str, str] | None:
    if ctx.env.depth >= 1:
        return "recursion-guard", "this run was started by `rig lint`; project scripts are skipped to avoid recursion"
    if script.invokes_rig_lint:
        return "self-referential-script", f"script {script.name!r} runs `rig lint` itself and is never executed"
    if script.writes_files:
        return "writes-files", f"script {script.name!r} rewrites files (--fix/--write/--watch) and is never executed"
    if script.unsafe_formatters:
        names = ", ".join(script.unsafe_formatters)
        detail = f"script {script.name!r} runs {names} without a read-only form (--check/-l/--diff) and would rewrite files"
        return "writes-unknown", detail
    return None


def _command(ctx: PlanContext, script: ProjectScript) -> tuple[str, tuple[str, ...], Unavailable | None]:
    """(executable name, args, blocker) for a project script; the name was validated by the caller."""
    if script.runner == "Makefile":
        return "make", ("--no-print-directory", "-C", ".", "--", script.name), None
    manager = manager_name(ctx.scope, ctx.discovery, "js")
    if manager is None:
        manager = "npm"
    if not manager:
        detail = "the package manager is ambiguous (conflicting lockfiles); set the packageManager field in package.json"
        return "npm", (), Unavailable("missing-tool", "ambiguous-package-manager", detail)
    return manager.replace("-berry", ""), ("run", script.name), None


def _check(ctx: PlanContext, script: ProjectScript) -> PlannedCheck | None:
    if not _SCRIPT_NAME.match(script.name) or not script.roles:
        return None
    check_id = f"script:{script.runner}:{script.name}@{ctx.scope.rel}"
    gate = _gate(ctx, script)
    if gate is not None:
        unavailable = Unavailable("not-applicable", gate[0], gate[1])
        return build("script", "scripts", ctx.scope, "script", PRECEDENCE_SCRIPT, frozenset(), unavailable=unavailable, check_id=check_id)
    exe, args, blocked = _command(ctx, script)
    resolution = None
    if blocked is None:
        resolution = resolve_tool(exe, ctx.scope, ctx.discovery, ctx.env)
        if resolution.kind != "path":
            blocked = missing(ctx, exe, PRECEDENCE_BUILTIN, resolution)
    argv = None if resolution is None else (*resolution.argv_prefix, *args)
    return build(
        "script", "scripts", ctx.scope, exe, PRECEDENCE_SCRIPT, _covered_keys(script, ctx.scope),
        argv=argv, unavailable=blocked, resolution=resolution, check_id=check_id,
        runs=frozenset(script.covers_roles),
        reaches_ignored=False,  # what the script's own command walks (and ignores) is not known
    )


def plan_scripts(ctx: PlanContext) -> tuple[PlannedCheck, ...]:
    checks = (_check(ctx, script) for script in ctx.scope.scripts)
    return tuple(c for c in checks if c is not None)
