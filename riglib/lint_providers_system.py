"""System-tool providers (shell, GitHub workflows, Rust, Go) and the CCCC ``rig-check`` provider.

Their binaries come from PATH or, for shellcheck/shfmt/actionlint, from Rig's pinned tool cache. Rig never
changes a project's Rust or Go toolchain. File-list checks are chunked to stay under the argv limit and every
path is passed after ``--`` (a leading ``-`` is neutralised with ``./``) so a hostile file name is never a flag.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .lint_discovery_model import LANG_GO, LANG_SHELL, TRAIT_GH_WORKFLOWS
from .lint_providers_build import build, not_applicable, tool_check
from .lint_providers_model import PRECEDENCE_BUILTIN, PlanContext, PlannedCheck, Unavailable
from .lint_providers_scope import has_family, keys, role_keys, scope_rel_path

if TYPE_CHECKING:
    from .lint_run_config import Category


def file_operand(path: str) -> str:
    """A git file name as a tool operand: a leading ``-`` would be read as an option (``--config=evil.js``)."""
    return "./" + path if path.startswith("-") else path


def chunk_files(files: Sequence[str], *, max_bytes: int = 100_000) -> tuple[tuple[str, ...], ...]:
    """Split ``files`` into argv-size-safe chunks; a leading ``-`` is neutralised with ``./``.

    A single path larger than ``max_bytes`` still gets its own chunk: it cannot be split and failing
    to run it would hide a file.
    """
    chunks: list[tuple[str, ...]] = []
    current: list[str] = []
    size = 0
    for name in files:
        item = file_operand(name)
        cost = len(os.fsencode(item)) + 1
        if current and size + cost > max_bytes:
            chunks.append(tuple(current))
            current, size = [], 0
        current.append(item)
        size += cost
    if current:
        chunks.append(tuple(current))
    return tuple(chunks)


def _file_checks(
    ctx: PlanContext, provider_id: str, category: Category, tool: str, files_key: str, lead: tuple[str, ...],
    fix_lead: tuple[str, ...] | None, *, result_rule: Literal["exit-zero", "stdout-empty"] = "exit-zero",
) -> tuple[PlannedCheck, ...]:
    files = tuple(scope_rel_path(ctx.scope, name) for name in ctx.scope.files.get(files_key, ()))
    covers = keys(category, (files_key,), ctx.scope.rel)
    checks = []
    for index, chunk in enumerate(chunk_files(files)):
        fix = None if fix_lead is None else (*fix_lead, "--", *chunk)
        checks.append(
            tool_check(
                ctx, provider_id, category, tool, PRECEDENCE_BUILTIN, covers, (*lead, "--", *chunk), fix,
                result_rule=result_rule, suffix="" if index == 0 else f"#{index + 1}",
            )
        )
    return tuple(checks)


def plan_shellcheck(ctx: PlanContext) -> tuple[PlannedCheck, ...]:
    return _file_checks(ctx, "shellcheck", "lint", "shellcheck", LANG_SHELL, (), None)


def plan_shfmt(ctx: PlanContext) -> tuple[PlannedCheck, ...]:
    return _file_checks(ctx, "shfmt", "format", "shfmt", LANG_SHELL, ("-d",), ("-w",))


def plan_actionlint(ctx: PlanContext) -> tuple[PlannedCheck, ...]:
    return _file_checks(ctx, "actionlint", "lint", "actionlint", TRAIT_GH_WORKFLOWS, ("-no-color",), None)


def plan_gofmt(ctx: PlanContext) -> tuple[PlannedCheck, ...]:
    return _file_checks(ctx, "gofmt", "format", "gofmt", LANG_GO, ("-l",), ("-w",), result_rule="stdout-empty")


def plan_go_vet(ctx: PlanContext) -> tuple[PlannedCheck, ...]:
    if not has_family(ctx.scope, "go"):
        return ()
    covers = role_keys(ctx.scope, "go", "lint")
    return (tool_check(ctx, "go-vet", "lint", "go", PRECEDENCE_BUILTIN, covers, ("vet", "./...")),)


def _has_cargo_lock(ctx: PlanContext) -> bool:
    lock = ctx.env.root / ("Cargo.lock" if ctx.scope.rel == "." else f"{ctx.scope.rel}/Cargo.lock")
    return lock.is_file() and not lock.is_symlink()


def plan_clippy(ctx: PlanContext) -> tuple[PlannedCheck, ...]:
    if not has_family(ctx.scope, "rust"):
        return ()
    locked = ("--locked",) if _has_cargo_lock(ctx) else ()
    args = ("clippy", "--workspace", "--all-targets", *locked, "--", "-D", "warnings")
    return (tool_check(ctx, "cargo-clippy", "lint", "cargo", PRECEDENCE_BUILTIN, role_keys(ctx.scope, "rust", "lint"), args),)


def plan_cargo_fmt(ctx: PlanContext) -> tuple[PlannedCheck, ...]:
    if not has_family(ctx.scope, "rust"):
        return ()
    covers = role_keys(ctx.scope, "rust", "format")
    return (
        tool_check(
            ctx, "cargo-fmt", "format", "cargo", PRECEDENCE_BUILTIN, covers,
            ("fmt", "--all", "--", "--check"), ("fmt", "--all"),
        ),
    )


# A by-path launcher, not `-m riglib`: with cwd at the audited repo, `-m` would import a repo-controlled
# `riglib` package. The launcher strips cwd from sys.path before importing Rig's own code.
_LAUNCHER = Path(__file__).resolve().with_name("lint_check_launcher.py")


def _rig_check_argv(ctx: PlanContext, *lead: str) -> tuple[str, ...]:
    config = () if ctx.config_path is None else ("--config", ctx.config_path)
    return (sys.executable, str(_LAUNCHER), "lint", "check", *lead, "--json", "-C", str(ctx.env.root), *config)


def plan_rig_check(ctx: PlanContext) -> tuple[PlannedCheck, ...]:
    from . import cccc_install  # lazy: keeps `import lint_providers` free of the installer module

    if ctx.scope.rel != ".":
        return ()
    if not ctx.cfg.lint_checks_enabled:
        detail = "lint_checks.enabled is false, so complexity and file-line limits are not checked"
        return (not_applicable("rig-check", "limits", ctx, "rig", "disabled-by-config", detail, PRECEDENCE_BUILTIN),)
    try:
        cccc_install.pin_for((ctx.env.system, ctx.env.machine))
    except cccc_install.UnsupportedPlatform as exc:
        unavailable = Unavailable("unsupported-platform", "platform-unsupported", str(exc))
        return (build("rig-check", "limits", ctx.scope, "rig", PRECEDENCE_BUILTIN, frozenset(), unavailable=unavailable),)
    return (
        build(
            "rig-check", "limits", ctx.scope, "rig", PRECEDENCE_BUILTIN, frozenset(),
            argv=_rig_check_argv(ctx), fix_argv=_rig_check_argv(ctx, "--fix"),
        ),
    )
