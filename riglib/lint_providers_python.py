"""Python providers: Ruff (Rig default) and the project's own flake8, pylint, black, mypy, pyright.

Ruff's formatter runs as ``ruff format --check`` and Black as ``black --check .``: the check never
writes. Their writing forms live only in ``fix_argv``. Ruff's default also covers loose ``.py`` files in a
non-Python repo (a script under ``scripts/`` of a JS repo): discovery adds the language to that scope.
"""

from __future__ import annotations

import os

from .lint_providers_build import not_applicable
from .lint_providers_files import PY_EXTENSIONS, GitignoreWalk, targeted_checks
from .lint_providers_js import own_or_default, simple_configured
from .lint_providers_model import PRECEDENCE_CONFIGURED, PlanContext, PlannedCheck
from .lint_providers_scope import own_tool, role_keys
from .lint_providers_system import file_operand


def plan_ruff_check(ctx: PlanContext) -> tuple[PlannedCheck, ...]:
    return own_or_default(
        ctx, "ruff-check", "lint", "ruff", "py", "lint",
        lambda _own: role_keys(ctx.scope, "py", "lint"), ("check",), ("check", "--fix"), walk=GitignoreWalk(PY_EXTENSIONS),
    )


def plan_ruff_format(ctx: PlanContext) -> tuple[PlannedCheck, ...]:
    return own_or_default(
        ctx, "ruff-format", "format", "ruff", "py", "format",
        lambda _own: role_keys(ctx.scope, "py", "format"), ("format", "--check"), ("format",),
        walk=GitignoreWalk(PY_EXTENSIONS),
    )


def plan_flake8(ctx: PlanContext) -> tuple[PlannedCheck, ...]:
    if own_tool(ctx.scope, "flake8") is None:
        return ()
    return targeted_checks(
        ctx, "flake8", "lint", "flake8", PRECEDENCE_CONFIGURED, role_keys(ctx.scope, "py", "lint"),
        (), None, (), PY_EXTENSIONS,
    )


def _python_packages(ctx: PlanContext) -> tuple[str, ...]:
    """Top-level package directories of the scope: pylint needs modules, not a bare directory."""
    base = ctx.env.root if ctx.scope.rel == "." else ctx.env.root / ctx.scope.rel
    try:
        entries = sorted(os.scandir(base), key=lambda e: e.name)
    except OSError:
        return ()
    found = [
        e.name for e in entries
        if not e.name.startswith(".") and e.is_dir(follow_symlinks=False)
        and os.path.isfile(os.path.join(e.path, "__init__.py"))
    ]
    return tuple(file_operand(name) for name in found)


def plan_pylint(ctx: PlanContext) -> tuple[PlannedCheck, ...]:
    if own_tool(ctx.scope, "pylint") is None:
        return ()
    packages = _python_packages(ctx)
    if not packages:
        detail = "pylint is configured but the scope has no top-level Python package directory to lint"
        return (not_applicable("pylint", "lint", ctx, "pylint", "no-matching-files", detail, PRECEDENCE_CONFIGURED),)
    return simple_configured(ctx, "pylint", "pylint", "py", "lint", packages, None)


def plan_black(ctx: PlanContext) -> tuple[PlannedCheck, ...]:
    return simple_configured(ctx, "black", "black", "py", "format", ("--check", "."), (".",), walk=GitignoreWalk(PY_EXTENSIONS))


def plan_mypy(ctx: PlanContext) -> tuple[PlannedCheck, ...]:
    if own_tool(ctx.scope, "mypy") is None:
        return ()
    return targeted_checks(
        ctx, "mypy", "typecheck", "mypy", PRECEDENCE_CONFIGURED, role_keys(ctx.scope, "py", "typecheck"),
        (), None, (".",), PY_EXTENSIONS,
    )


def plan_pyright(ctx: PlanContext) -> tuple[PlannedCheck, ...]:
    return simple_configured(ctx, "pyright", "pyright", "py", "typecheck", (), None)
