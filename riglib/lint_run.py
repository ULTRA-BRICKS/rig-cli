"""`rig lint run`: execute every applicable check, aggregate, report (`rig-lint-run/v1`).

Context: `lint_discovery` says what the repository contains, `lint_providers` turns that into
`PlannedCheck`s, `lint_bootstrap` plans/applies the local tool install (through `plan.build` +
`actions.run_plan`). This module owns the middle and the end: running the checks one after another,
the read-only guard, `--fix`, and assembling the report. Its collaborators:

- `lint_run_plan`     options, setup, discovery, planning, filters
- `lint_run_proc`     one bounded argv-only subprocess
- `lint_run_verdict`  what an outcome means, and the exit code
- `lint_run_coverage` which source files were really verified
- `lint_run_report`   JSON document and text
- `lint_run_fix`      worktree snapshots (read-only guard, `--fix` delta)

`rig lint check` stays a separate engine (`lint_check`), reached through the `rig-check` provider as
a subprocess. Trust boundary: commands are argv lists produced by providers (never a shell, never
configurable from rig.yaml); a check is read-only by contract and the worktree is compared before and
after every one (a writer is reported as `wrote-files`, exit 3). A check that is missing, not
applicable or unsupported is NEVER a pass, and one failing check never stops the remaining ones.
Exit priority 2 > 127 > 3 > 1 > 0, the same as `rig lint check`.
"""
from __future__ import annotations

import os
import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from . import lint_run_fix
from .lint_check_scope import ScopeError
from .lint_run_coverage import Coverage, coverage
from .lint_run_model import (
    EXIT_FINDINGS, EXIT_INCOMPLETE, EXIT_MISSING, EXIT_OK, EXIT_USAGE, INCOMPLETE_REASONS, SCHEMA,
    CheckResult, RunOptions, RunReport, State,
)
from .lint_run_config import CATEGORIES
from .lint_run_plan import (
    Planned, Refusal, ci_from, disabled_result, plan_run, planned_result, prepare, problem_rows, usage_report,
)
from .lint_run_proc import (
    MAX_CAPTURE_BYTES, CommandOutcome, CommandRunner, CommandSpec, child_env, subprocess_runner,
)
from .lint_run_report import bootstrap_doc, discovery_doc, document, empty_discovery_doc, render_text
from .lint_run_verdict import (
    CHECK_SCHEMA, exit_code_for, classify, executed_result, static_result, wrote_files_verdict,
)

if TYPE_CHECKING:
    from .lint_providers import PlannedCheck

__all__ = [
    "CHECK_SCHEMA", "CheckResult", "CommandOutcome", "CommandRunner", "CommandSpec", "EXIT_FINDINGS",
    "EXIT_INCOMPLETE", "EXIT_MISSING", "EXIT_OK", "EXIT_USAGE", "INCOMPLETE_REASONS", "MAX_CAPTURE_BYTES",
    "RunOptions", "RunReport", "SCHEMA", "State", "child_env", "exit_code_for", "execute", "render_text",
    "run_checks", "subprocess_runner",
]

# Tokens no safe fix may carry; a provider bug must degrade to check mode, never to a risky rewrite.
_UNSAFE_FIX_TOKENS = ("--unsafe", "--fix-dangerously", "--fix-suggestions", "--unsafe-fixes")
# Only the direct language categories narrow a run. `limits` reads every file but verifies no language, and
# `scripts` is a second route to checks the direct providers already plan: leaving it out (`--skip scripts`)
# keeps the direct tool checks running, so it must never switch the uncovered-source gate off.
_VERIFYING_CATEGORIES = frozenset({"lint", "format", "typecheck"})


def _spec(argv: Sequence[str], check: PlannedCheck, root: Path, timeout_s: int, env: Mapping[str, str]) -> CommandSpec:
    return CommandSpec(tuple(argv), root / check.cwd, timeout_s, env, MAX_CAPTURE_BYTES)


def _safe_fix_argv(check: PlannedCheck) -> tuple[str, ...] | None:
    argv = check.fix_argv
    if argv is None or any(tok.startswith(_UNSAFE_FIX_TOKENS) for tok in argv):
        return None
    return argv


def _run_check_mode(check: PlannedCheck, root: Path, timeout_s: int, env: Mapping[str, str],
                    runner: CommandRunner, fix_doc: Mapping[str, object] | None, spent_ms: int = 0) -> CheckResult:
    """Run the read-only form of a check and prove it left the worktree as it found it."""
    assert check.argv is not None
    try:
        written_since = lint_run_fix.watch(root)
    except lint_run_fix.SnapshotError as exc:
        return static_result(check, "failed", "crash", f"cannot prove the check is read-only: {exc}")
    outcome = runner(_spec(check.argv, check, root, timeout_s, env))
    try:
        written = written_since()
    except lint_run_fix.SnapshotError as exc:
        return static_result(check, "failed", "crash", f"cannot prove the check is read-only: {exc}")
    verdict = wrote_files_verdict(written) if written else classify(check, outcome, timeout_s, root)
    return executed_result(check, verdict, outcome, timeout_s, spent_ms + outcome.duration_ms, fix_doc)


def _run_fix(check: PlannedCheck, fix_argv: tuple[str, ...], root: Path, timeout_s: int,
             env: Mapping[str, str], runner: CommandRunner) -> CheckResult:
    try:
        before = lint_run_fix.snapshot(root)
    except lint_run_fix.SnapshotError as exc:
        return static_result(check, "failed", "crash", f"cannot run the safe fix without a worktree snapshot: {exc}")
    fixed = runner(_spec(fix_argv, check, root, timeout_s, env))
    try:
        changed = lint_run_fix.changed_files(before, lint_run_fix.snapshot(root))
        restage = lint_run_fix.needs_restage(root, changed)
    except (lint_run_fix.SnapshotError, ScopeError) as exc:
        return static_result(check, "failed", "crash", f"cannot report what the fix changed: {exc}")
    fix_doc = {"requested": True, "mode": "safe", "changed_files": changed, "needs_restage": restage}
    if fixed.spawn_error is not None or fixed.timed_out:
        verdict = classify(check, fixed, timeout_s, root)
        return executed_result(check, verdict, fixed, timeout_s, fixed.duration_ms, fix_doc)
    # The verdict is the post-fix state of the check itself, not the fixer's exit code.
    return _run_check_mode(check, root, timeout_s, env, runner, fix_doc, spent_ms=fixed.duration_ms)


def _run_one(check: PlannedCheck, *, root: Path, default_timeout_s: int, fix: bool,
             env: Mapping[str, str], runner: CommandRunner) -> CheckResult:
    if check.argv is None:
        unavailable = check.unavailable
        if unavailable is None:
            return static_result(check, "failed", "crash", "provider produced neither a command nor a reason")
        return static_result(check, unavailable.state, unavailable.reason, unavailable.detail)
    timeout_s = check.timeout_s or default_timeout_s
    if not fix:
        return _run_check_mode(check, root, timeout_s, env, runner, None)
    fix_argv = _safe_fix_argv(check)
    if fix_argv is None:
        doc = {"requested": True, "mode": "unsupported", "changed_files": [], "needs_restage": []}
        return _run_check_mode(check, root, timeout_s, env, runner, doc)
    return _run_fix(check, fix_argv, root, timeout_s, env, runner)


def run_checks(checks: Sequence[PlannedCheck], *, root: Path, default_timeout_s: int, fix: bool,
               environ: Mapping[str, str], runner: CommandRunner = subprocess_runner) -> tuple[CheckResult, ...]:
    env = child_env(environ)
    return tuple(_run_one(c, root=root, default_timeout_s=default_timeout_s, fix=fix, env=env, runner=runner)
                 for c in checks)


def _run_planned(opts: RunOptions, checks: Sequence[PlannedCheck], root: Path, config_timeout_s: int,
                 environ: Mapping[str, str], runner: CommandRunner) -> tuple[CheckResult, ...]:
    timeout_s = opts.timeout or config_timeout_s
    if opts.plan:
        return tuple(planned_result(c, timeout_s) for c in checks)
    if opts.timeout:  # an explicit --timeout beats provider overrides too
        checks = [replace(c, timeout_s=opts.timeout) for c in checks]
    return run_checks(checks, root=root, default_timeout_s=timeout_s, fix=opts.fix, environ=environ, runner=runner)


def _narrowed(opts: RunOptions, cfg) -> bool:
    enabled = {name for name, on in cfg.categories.items() if on}
    selected = (set(opts.only or CATEGORIES) - set(opts.skip)) & enabled
    return bool(opts.scopes) or not _VERIFYING_CATEGORIES <= selected


def _setup_incomplete(planned: Planned) -> bool:
    """The Rig policy could not be applied in full (or two Oxlint configs conflict): never a green exit, even in CI."""
    return bool(getattr(planned.setup.plan, "setup_incomplete", False))


def _finish(root: Path, opts: RunOptions, ci: bool, cfg, planned: Planned, executed: Sequence[CheckResult],
            started: float) -> RunReport:
    results = (*problem_rows(planned.discovery), *executed)
    cov = coverage(planned.discovery, results)
    # A user who narrowed the run (a verifying category via --only/--skip or lint.categories, or
    # --scope) chose what goes unchecked. Otherwise any source file nobody verified is a non-green exit.
    uncovered = 0 if _narrowed(opts, cfg) else cov.unverified
    exit_code = EXIT_OK if opts.plan else exit_code_for(
        results, bootstrap_failed=planned.setup.label == "failed" or _setup_incomplete(planned), problems=planned.discovery.problems,
        uncovered_files=uncovered)
    doc = document(
        root=root, exit_code=exit_code, plan=opts.plan, ci=ci, fix=opts.fix, setup=planned.setup.label, cfg=cfg,
        discovery=discovery_doc(planned.discovery, _unsupported_tools(planned)),
        bootstrap=bootstrap_doc(planned.setup.plan, planned.setup.outcome), results=results, cov=cov,
        duration_ms=int((time.monotonic() - started) * 1000))
    return RunReport(exit_code, doc, render_text(doc))


def _unsupported_tools(planned: Planned) -> tuple[tuple[str, str], ...]:
    from . import lint_providers  # the registry is built on import; only the full run needs it

    return lint_providers.unsupported_tools(planned.discovery)


def _disabled_report(root: Path, opts: RunOptions, ci: bool, cfg, started: float) -> RunReport:
    # `lint.enabled: false` runs nothing and discovers nothing, so no coverage claim is possible.
    results = (disabled_result(),)
    doc = document(
        root=root, exit_code=EXIT_OK, plan=opts.plan, ci=ci, fix=opts.fix, setup="disabled-by-config", cfg=cfg,
        discovery=empty_discovery_doc(), bootstrap=bootstrap_doc(None, None), results=results,
        cov=Coverage(0, 0, 0, 0, (), complete=False), duration_ms=int((time.monotonic() - started) * 1000))
    return RunReport(EXIT_OK, doc, render_text(doc))


def execute(opts: RunOptions, *, environ: Mapping[str, str] | None = None, runner: CommandRunner = subprocess_runner,
            which: Callable[[str], str | None] | None = None) -> RunReport:
    started = time.monotonic()
    environ = dict(os.environ if environ is None else environ)
    which = which or shutil.which
    error, cfg, root, prefixes = prepare(opts, environ)
    if error is not None:
        return usage_report(error, opts)
    ci = ci_from(opts, environ)
    if not cfg.enabled:
        return _disabled_report(root, opts, ci, cfg, started)
    try:
        planned = plan_run(opts, cfg, root, prefixes, environ, which, ci)
    except (ScopeError, Refusal) as exc:
        return usage_report(str(exc), opts)
    executed = _run_planned(opts, planned.checks, root, cfg.timeout_seconds, environ, runner)
    return _finish(root, opts, ci, cfg, planned, executed, started)
