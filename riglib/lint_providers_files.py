"""Pass enumerated files instead of ``.`` when git-ignored trees sit below a scope.

Why: discovery enumerates only tracked + untracked-not-ignored files, but a tool run with ``.`` walks the
directory itself. Biome, ESLint, mypy and flake8 do not read ``.gitignore``, so a git-ignored sibling tree
(``.worktrees/<branch>`` checkouts with their own nested Biome roots, ``dist``, ...) is linted too and can
fail the whole run on files that are not part of the project. When such a directory exists under the scope,
the tool gets the enumerated in-scope files of its own extensions (chunked to stay under the argv limit);
otherwise ``.`` keeps the argv short and identical to what a developer would type. No ignore rule is added
anywhere and the ignored trees are never touched.

Tracked-but-ignored files (``git add -f vendor/x.ts`` with ``vendor/`` in ``.gitignore``) are a second case: a
tool that walks ``.`` while honouring ``.gitignore`` (Oxlint, Oxfmt, Ruff, Black, Prettier) never opens them, so
:func:`with_ignored_reach` adds explicit-operand checks for exactly those files (never the ignored untracked
trees) where the tool analyzes explicit operands, and marks the walking check as not reaching them; Prettier
silently skips an explicit ignored operand, so its files stay uncovered (`tracked-but-ignored`).

Directories every tool already skips by default (``node_modules``, virtualenvs, caches) do not trigger it.
"""

from __future__ import annotations

import posixpath
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .lint_discovery_model import contains
from .lint_providers_build import not_applicable, tool_check
from .lint_providers_model import PlanContext, PlannedCheck
from .lint_providers_scope import scope_rel_path
from .lint_providers_system import chunk_files

if TYPE_CHECKING:
    from .lint_run_config import Category

_SKIPPED_BY_TOOLS = frozenset({
    "node_modules", ".git", ".venv", "venv", "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache", ".tox",
})
JS_EXTENSIONS = frozenset({".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"})
BIOME_EXTENSIONS = JS_EXTENSIONS | {".json", ".jsonc", ".css"}
PY_EXTENSIONS = frozenset({".py", ".pyi"})


def _recursed_ignored(scope_rel: str, ignored: str) -> bool:
    if not contains(scope_rel, ignored):
        return False
    return not _SKIPPED_BY_TOOLS & set(ignored.split("/"))


def scope_targets(ctx: PlanContext, extensions: frozenset[str]) -> tuple[str, ...] | None:
    """``None`` when ``.`` is safe; else the scope-relative files of ``extensions`` (possibly none)."""
    discovery, scope = ctx.discovery, ctx.scope
    if not any(_recursed_ignored(scope.rel, d) for d in discovery.ignored_dirs):
        return None
    wanted = (
        path for path in discovery.enumerated_files
        if contains(scope.rel, posixpath.dirname(path) or ".") and posixpath.splitext(path)[1].lower() in extensions
    )
    return tuple(scope_rel_path(scope, path) for path in wanted)


def targeted_checks(
    ctx: PlanContext, provider_id: str, category: Category, tool: str, precedence: int, covers: frozenset[str],
    lead: Sequence[str], fix_lead: Sequence[str] | None, dot: Sequence[str], extensions: frozenset[str],
) -> tuple[PlannedCheck, ...]:
    """The check for ``tool``: ``<lead> <dot>`` normally, ``<lead> <files...>`` chunks when ignored trees exist."""
    files = scope_targets(ctx, extensions)
    if files is None:
        fix = None if fix_lead is None else (*fix_lead, *dot)
        return (tool_check(ctx, provider_id, category, tool, precedence, covers, (*lead, *dot), fix),)
    if not files:
        detail = f"{provider_id} has no tracked files of its kind in this scope (git-ignored trees are never scanned)"
        return (not_applicable(provider_id, category, ctx, tool, "no-matching-files", detail, precedence),)
    checks = []
    for index, chunk in enumerate(chunk_files(files)):
        fix = None if fix_lead is None else (*fix_lead, *chunk)
        checks.append(
            tool_check(
                ctx, provider_id, category, tool, precedence, covers, (*lead, *chunk), fix,
                suffix="" if index == 0 else f"#{index + 1}",
            )
        )
    return tuple(checks)



@dataclass(frozen=True)
class GitignoreWalk:
    """A tool whose directory walk honours ``.gitignore``, so its ``.``/no-operand check skips tracked-but-ignored files."""

    extensions: frozenset[str]
    # True: the tool analyzes an explicit ignored operand (verified for Oxlint, Oxfmt, Ruff, Black), so the files get
    # their own check. False (Prettier): it skips ignored operands too, so nothing Rig can pass reaches them.
    explicit_operands: bool = True


def tracked_ignored_targets(ctx: PlanContext, extensions: frozenset[str]) -> tuple[str, ...]:
    """Scope-relative tracked-but-ignored files of ``extensions`` under the scope (explicit operands, never trees)."""
    discovery, scope = ctx.discovery, ctx.scope
    wanted = (
        path for path in discovery.tracked_ignored
        if contains(scope.rel, posixpath.dirname(path) or ".") and posixpath.splitext(path)[1].lower() in extensions
    )
    return tuple(scope_rel_path(scope, path) for path in wanted)


def _tail(argv: tuple[str, ...], args: tuple[str, ...]) -> tuple[str, ...]:
    """The resolution prefix of ``argv`` (``argv`` is ``<prefix> <args>``)."""
    return argv[: len(argv) - len(args)]


def _explicit_checks(
    check: PlannedCheck, files: tuple[str, ...], args: tuple[str, ...], fix_args: tuple[str, ...] | None
) -> list[PlannedCheck]:
    """The explicit-operand copies of ``check``: same resolved executable, the ``.`` swapped for ``files`` chunks."""
    assert check.argv is not None
    walk_lead = (_tail(check.argv, args), tuple(a for a in args if a != "."))
    fix_lead = None
    if check.fix_argv is not None and fix_args is not None:
        fix_lead = (_tail(check.fix_argv, fix_args), tuple(a for a in fix_args if a != "."))
    out: list[PlannedCheck] = []
    for index, chunk in enumerate(chunk_files(files)):
        suffix = "#tracked-ignored" if index == 0 else f"#tracked-ignored-{index + 1}"
        fix = None if fix_lead is None else (*fix_lead[0], *fix_lead[1], *chunk)
        out.append(replace(check, id=f"{check.id}{suffix}", argv=(*walk_lead[0], *walk_lead[1], *chunk), fix_argv=fix, reaches_ignored=True))
    return out


def with_ignored_reach(
    ctx: PlanContext, checks: tuple[PlannedCheck, ...], walk: GitignoreWalk, args: tuple[str, ...],
    fix_args: tuple[str, ...] | None,
) -> tuple[PlannedCheck, ...]:
    """Mark the walking checks as not reaching tracked-but-ignored files and append the explicit-operand checks.

    ``args``/``fix_args`` are the walking argv tails the checks were built from; the explicit checks reuse the
    same resolved executable, swap the ``.`` operand for the files, and carry ``reaches_ignored=True``.
    """
    files = tracked_ignored_targets(ctx, walk.extensions) if walk.explicit_operands else ()
    out: list[PlannedCheck] = []
    for check in checks:
        if check.argv is None:
            out.append(check)
            continue
        out.append(replace(check, reaches_ignored=False))
        if files:
            out.extend(_explicit_checks(check, files, args, fix_args))
    return tuple(out)
