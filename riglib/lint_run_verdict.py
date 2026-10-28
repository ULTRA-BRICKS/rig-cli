"""Turn a finished command into a verdict, and verdicts into the run's exit code.

Context: `lint_run` executes; this module decides what an outcome MEANS. Fail-closed throughout: only
an exit 0 (and, for `rig-check`, a valid `rig-lint-check/v1` document) is a pass; anything the Rig
cannot interpret is a failure that reads as "analysis incomplete" (exit 3), never as a pass. Exit
priority is 2 > 127 > 3 > 1 > 0, identical to `rig lint check`.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from .lint_run_model import (
    EXIT_FINDINGS, EXIT_INCOMPLETE, EXIT_MISSING, EXIT_OK, EXIT_USAGE, CheckResult, State, Verdict,
)
from .lint_run_proc import CommandOutcome
from .lint_run_signals import tool_failure

if TYPE_CHECKING:
    from .lint_discovery import DiscoveryProblem
    from .lint_providers import PlannedCheck

CHECK_SCHEMA = "rig-lint-check/v1"
EXCERPT_BYTES = 32 * 1024
# Discovery problems that mean "a manifest/config could not be trusted"; a symlink-escape is a
# reported refusal to read outside the repo, not a broken file.
INCOMPLETE_PROBLEMS = frozenset({"malformed-manifest", "malformed-config", "unreadable"})
_UNAVAILABLE_STATES = frozenset({"missing-tool", "unsupported-platform"})
_MAX_LISTED_WRITES = 5


def excerpt(text: str) -> tuple[str, bool]:
    if len(text) <= EXCERPT_BYTES:
        return text, False
    half = EXCERPT_BYTES // 2
    return f"{text[:half]}\n[... {len(text) - EXCERPT_BYTES} characters omitted ...]\n{text[-half:]}", True


def _parse_check_report(stdout: str) -> Mapping[str, object] | None:
    try:
        parsed = json.loads(stdout)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) and parsed.get("schema") == CHECK_SCHEMA else None


def _check_counts(report: Mapping[str, object] | None) -> dict[str, int] | None:
    summary = report.get("summary") if report else None
    if not isinstance(summary, dict):
        return None
    return {k: v for k in ("errors", "warnings") if isinstance(v := summary.get(k), int)} or None


_RIG_CHECK_EXIT = {
    0: ("passed", "ok", "complexity and file-line limits hold"),
    1: ("failed", "findings", "complexity or file-line limits exceeded"),
    3: ("failed", "incomplete", "`rig lint check` could not analyze every file"),
}


def _rig_check_verdict(outcome: CommandOutcome) -> Verdict:
    report = _parse_check_report(outcome.stdout)
    code = outcome.exit_code
    if code == 127:
        return Verdict("missing-tool", "analyzer-unavailable",
                       "the pinned complexity analyzer is not installed; run `rig apply commit`", report=report)
    if code == 2:
        return Verdict("failed", "config-error", "`rig lint check` rejected the configuration (exit 2)", report=report)
    if outcome.truncated:
        return Verdict("failed", "output-limit", "`rig lint check` output exceeded the capture limit", report=report)
    if report is None:
        return Verdict("failed", "malformed-output", f"`rig lint check` printed no {CHECK_SCHEMA} document (exit {code})")
    state, reason, detail = _RIG_CHECK_EXIT.get(code, ("failed", "crash", f"`rig lint check` exited {code}"))
    return Verdict(state, reason, detail, _check_counts(report), report)


def _abnormal_verdict(check: PlannedCheck, outcome: CommandOutcome, timeout_s: int) -> Verdict | None:
    if outcome.spawn_error is not None:
        if outcome.spawn_error.startswith("FileNotFoundError"):
            return Verdict("missing-tool", "tool-not-installed",
                           f"`{check.argv[0]}` could not be started ({outcome.spawn_error}); install it")
        return Verdict("failed", "crash", f"could not start the check: {outcome.spawn_error}")
    if outcome.timed_out:
        return Verdict("failed", "timeout",
                       f"timed out after {timeout_s}s; raise lint.timeout_seconds or lint.providers.{check.provider}.timeout_seconds")
    return None


def _stdout_rule_verdict(outcome: CommandOutcome) -> Verdict:
    listed = [line for line in outcome.stdout.splitlines() if line.strip()]
    if outcome.exit_code == 0 and not listed:
        return Verdict("passed", "ok", "nothing to report", {"files": 0})
    return Verdict("failed", "findings", f"{len(listed)} file(s) reported" if listed else f"exit {outcome.exit_code}",
                   {"files": len(listed)} if listed else None)


def classify(check: PlannedCheck, outcome: CommandOutcome, timeout_s: int, root: Path | None = None) -> Verdict:
    abnormal = _abnormal_verdict(check, outcome, timeout_s)
    if abnormal is not None:
        return abnormal
    if check.provider == "rig-check":
        return _rig_check_verdict(outcome)
    code = outcome.exit_code
    if code is None or code < 0 or code == 126:
        return Verdict("failed", "crash", f"the command did not finish normally (exit {code})")
    if code == 127:
        return Verdict("missing-tool", "tool-not-installed",
                       "the command, or a program it runs, is not installed; install the project's dependencies")
    if check.result_rule == "stdout-empty":
        return _stdout_rule_verdict(outcome)
    if code == 0:
        return Verdict("passed", "ok", "no findings")
    # A tool that failed to RUN (bad configuration, missing dependencies, crash) found nothing: it is
    # never reported as findings, and its files are not counted as checked.
    failure = tool_failure(check, outcome, root)
    if failure is not None:
        return failure
    # Linters disagree on exit codes beyond 0/1 (cargo clippy uses 101); any non-zero is a failure
    # that can never read as a pass, and only crashes/timeouts are "incomplete".
    return Verdict("failed", "findings", f"exited {code}")


def wrote_files_verdict(written: Sequence[str]) -> Verdict:
    listed = ", ".join(written[:_MAX_LISTED_WRITES]) + (" ..." if len(written) > _MAX_LISTED_WRITES else "")
    return Verdict(
        "failed", "wrote-files",
        f"the check changed {len(written)} file(s) ({listed}); a check must be read-only, so its verdict is "
        "not trusted. Use the tool's check form (--check, -l, -d) or run `rig lint --fix`",
    )


def static_result(check: PlannedCheck, state: State, reason: str, detail: str) -> CheckResult:
    return CheckResult(check, state, reason, detail, None, None, None, "", "", False, None, None)


def executed_result(check: PlannedCheck, verdict: Verdict, outcome: CommandOutcome, timeout_s: int,
                    duration_ms: int, fix: Mapping[str, object] | None) -> CheckResult:
    stdout, cut_out = excerpt(outcome.stdout)
    stderr, cut_err = excerpt(outcome.stderr)
    return CheckResult(
        replace(check, timeout_s=timeout_s), verdict.state, verdict.reason, verdict.detail, outcome.exit_code,
        duration_ms, verdict.counts, stdout, stderr, outcome.truncated or cut_out or cut_err, fix, verdict.report,
    )


def _analysis_incomplete(failed: Sequence[CheckResult], bootstrap_failed: bool,
                         problems: Sequence[DiscoveryProblem]) -> bool:
    # Anything failed for a reason other than plain findings is analysis we could not complete.
    return (bootstrap_failed or any(r.reason != "findings" for r in failed)
            or any(p.kind in INCOMPLETE_PROBLEMS for p in problems))


def exit_code_for(results: Sequence[CheckResult], *, bootstrap_failed: bool,
                  problems: Sequence[DiscoveryProblem], uncovered_files: int = 0) -> int:
    failed = [r for r in results if r.state == "failed"]
    # Exit 2 is Rig's own usage/config error: only the nested `rig lint check` rejecting ITS
    # configuration. A linter rejecting the PROJECT's configuration is incomplete analysis (3).
    if any(r.reason == "config-error" and r.check.provider == "rig-check" for r in failed):
        return EXIT_USAGE
    # Source files nobody verified are as much "a check could not run" as a missing tool: they rank
    # with it (127), above incomplete analysis (3) and findings (1).
    if uncovered_files or any(r.state in _UNAVAILABLE_STATES for r in results):
        return EXIT_MISSING
    if _analysis_incomplete(failed, bootstrap_failed, problems):
        return EXIT_INCOMPLETE
    if failed:
        return EXIT_FINDINGS
    return EXIT_OK
