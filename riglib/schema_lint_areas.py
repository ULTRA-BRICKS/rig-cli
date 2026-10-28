"""Wizard areas for the two lint blocks (``lint_checks`` and ``lint``).

Why a separate module: ``schema.py`` is past the repo's size/complexity limits, so new areas live here.
``build`` is evaluated while ``schema`` is still importing (inside its ``AREAS`` literal), so it imports
the helpers lazily; everything it needs is defined above ``AREAS`` in ``schema``.
"""

from __future__ import annotations

from typing import Any


def build() -> tuple[Any, ...]:
    from .schema import KIND_BOOL, KIND_INT, KIND_LIST, KIND_STR, Area, _opt

    return (
        Area(
            "lint_checks", "complexity + file-line checks", "`rig lint check`: cognitive/cyclomatic complexity (pinned cccc) and file-length limits, run on commit and in CI.",
            (
                _opt("lint_checks.enabled", KIND_BOOL, True,
                     "Master switch for `rig lint check` and its commit-time activation. Independent of "
                     "`linters.*` (Oxc/Biome). Valid in the global and the repo config; repo wins."),
                _opt("lint_checks.complexity.enabled", KIND_BOOL, True,
                     "Run the per-function complexity check (official cccc, pinned and sha256-verified)."),
                _opt("lint_checks.complexity.max_cognitive", KIND_INT, 15,
                     "Cognitive complexity limit per function; a value strictly above it is an error (integer >= 1)."),
                _opt("lint_checks.complexity.max_cyclomatic", KIND_INT, 20,
                     "Cyclomatic complexity limit per function; a value strictly above it is an error (integer >= 1)."),
                _opt("lint_checks.file_lines.enabled", KIND_BOOL, True,
                     "Run the physical file-line check."),
                _opt("lint_checks.file_lines.warn_above", KIND_INT, 1200,
                     "Files with more lines than this warn (integer >= 1, must be <= error_above)."),
                _opt("lint_checks.file_lines.error_above", KIND_INT, 2400,
                     "Files with more lines than this are errors (integer >= 1)."),
                _opt("lint_checks.file_lines.exempt_extensions", KIND_LIST, ["json"],
                     "Lowercase extensions without a dot exempt from the file-line check (default: json only; "
                     "lockfiles are NOT exempt)."),
                _opt("lint_checks.ci.rig_source", KIND_STR, None,
                     "Immutable install source for the CI workflow's rig: `git+https://<host>/<path>@<40-hex commit sha>` "
                     "of a rig-cli that has `rig lint check`. Unset => no workflow is written (a pin that does not "
                     "resolve would leave the job red). Valid in the global and the repo config; repo wins."),
            ),
        ),
        Area(
            "lint", "universal lint (`rig lint`)",
            "`rig lint`: runs every applicable check (lint, typecheck, format, limits, project scripts). Configuration only: "
            "nothing is reconciled by `rig apply`, so this area has no status row.",
            (
                _opt("lint.enabled", KIND_BOOL, True,
                     "Master switch for `rig lint run` (bare `rig lint`). Does not affect `rig lint check` / `rig lint rules`. "
                     "Valid in the global and the repo config; repo wins per key."),
                _opt("lint.timeout_seconds", KIND_INT, 300,
                     "Default per-check timeout in seconds (integer 1..3600). A check that exceeds it is `failed` (timeout), exit 3."),
                _opt("lint.categories.lint", KIND_BOOL, True, "Run linters (Oxlint, ESLint, Biome, Ruff, shellcheck, actionlint, clippy, go vet, ...)."),
                _opt("lint.categories.typecheck", KIND_BOOL, True, "Run type checkers (tsc, mypy, pyright)."),
                _opt("lint.categories.format", KIND_BOOL, True, "Run formatter checks (Oxfmt, Prettier, Biome, Ruff format, shfmt, cargo fmt, gofmt)."),
                _opt("lint.categories.limits", KIND_BOOL, True, "Run the `rig lint check` complexity + file-line limits."),
                _opt("lint.categories.scripts", KIND_BOOL, True, "Run the project's own allow-listed lint/typecheck scripts (package.json, Makefile)."),
                _opt("lint.bootstrap.enabled", KIND_BOOL, True,
                     "Let bare local `rig lint` install missing Oxlint/Oxfmt/Ruff (exact pins, lockfile-updating). Never in CI, `--plan` or `--no-setup`."),
                _opt("lint.bootstrap.javascript", KIND_BOOL, True, "Bootstrap Oxlint + Oxfmt for JS/TS scopes that configure no lint/format tool."),
                _opt("lint.bootstrap.python", KIND_BOOL, True, "Bootstrap Ruff for Python scopes that configure no lint/format tool."),
            ),
        ),
    )
