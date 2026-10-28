"""`lint_run_coverage`: which source files did the run really verify, and exit 127 unsupported-language.

Pure data tests: a `Discovery` listing source files + `CheckResult`s with `covers` keys. The rule is
that a file counts as checked only when an EXECUTED lint/format/typecheck check for its language
(JS and TS are one family) covers an ancestor directory and reached a verdict about the code.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from riglib import lint_run as lr
from riglib.lint_discovery_model import Discovery, UnknownLanguage
from riglib.lint_providers import Unavailable
from riglib.lint_run_coverage import coverage
from tests.lint_run_helpers import planned


def discovery(source=None, unknown=()):
    return Discovery(root=Path("/repo"), scopes=(), unknown=tuple(unknown), problems=(), skipped_symlinks=(),
                     file_count=0, toml_parser=True, source_files=source or {})


def row(state, reason, covers, *, category="lint", id="c@."):
    unavailable = None if state in ("passed", "failed") else Unavailable(state, reason, "d")
    check = planned(id=id, category=category, covers=covers, argv=("t",) if unavailable is None else None,
                    unavailable=unavailable)
    return lr.CheckResult(check, state, reason, "d", None, None, None, "", "", False, None, None)


def test_executed_checks_cover_every_file_under_their_scope_and_js_is_one_family():
    d = discovery({"typescript": ("web/a.ts", "web/deep/b.ts", "api/c.ts"), "javascript": ("web/x.js",)})
    cov = coverage(d, [row("passed", "ok", {"lint:javascript:web"})])
    assert (cov.files_total, cov.files_checked, cov.files_uncovered) == (4, 3, 1)
    assert [(u.language, u.files, u.examples) for u in cov.uncovered] == [("typescript", 1, ("api/c.ts",))]
    assert cov.complete is False


def test_root_scope_covers_nested_files_and_a_full_run_is_complete():
    d = discovery({"python": ("tool.py", "pkg/mod.py")})
    cov = coverage(d, [row("failed", "findings", {"lint:python:."})])
    assert (cov.files_checked, cov.files_uncovered, cov.complete) == (2, 0, True)


@pytest.mark.parametrize("state,reason", [
    ("missing-tool", "tool-not-installed"), ("missing-tool", "dependencies-not-installed"),
    ("not-applicable", "filtered"), ("not-applicable", "disabled-by-config"),
    ("unsupported-platform", "platform-unsupported"), ("planned", "planned"),
])
def test_a_check_that_did_not_reach_a_verdict_leaves_files_uncovered_but_not_checker_less(state, reason):
    d = discovery({"python": ("a.py",)})
    cov = coverage(d, [row(state, reason, {"lint:python:."})])
    assert (cov.files_checked, cov.files_uncovered, cov.no_checker, cov.complete) == (0, 1, 0, False)


@pytest.mark.parametrize("reason", [
    "timeout", "crash", "wrote-files", "malformed-output", "config-error", "dependencies-not-installed",
])
def test_a_tool_that_failed_to_run_checks_no_file_even_though_it_planned_to(reason):
    """Planned `covers` alone never imply coverage (a real Biome config failure once read as 531 files checked)."""
    cov = coverage(discovery({"python": ("a.py",)}), [row("failed", reason, {"lint:python:."})])
    assert (cov.files_checked, cov.files_uncovered, cov.complete) == (0, 1, False)
    assert [u.why for u in cov.uncovered] == ["not-verified"] and cov.no_checker == 0


def test_limits_checks_never_verify_a_language():
    d = discovery({"python": ("a.py",)})
    cov = coverage(d, [row("passed", "ok", {"lint:python:."}, category="limits")])
    assert cov.files_checked == 0 and cov.files_uncovered == 1


def test_other_languages_do_not_cover_each_other():
    d = discovery({"python": ("a.py",), "shell": ("run.sh",)})
    cov = coverage(d, [row("passed", "ok", {"lint:python:."})])
    assert [(u.language, u.why) for u in cov.uncovered] == [("shell", "no-check")]


def test_unknown_languages_are_uncovered_with_the_no_provider_reason():
    d = discovery({"python": ("a.py",)}, unknown=[UnknownLanguage("kotlin", 12, ("A.kt", "B.kt", "C.kt"))])
    cov = coverage(d, [row("passed", "ok", {"format:python:."})])
    assert (cov.files_checked, cov.files_uncovered, cov.files_total, cov.complete) == (1, 12, 13, False)
    assert [(u.language, u.why) for u in cov.uncovered] == [("kotlin", "no-provider")]


def test_a_repository_without_source_files_is_complete():
    cov = coverage(discovery(), [])
    assert (cov.files_total, cov.files_uncovered, cov.complete) == (0, 0, True)


def test_as_dict_is_json_shaped():
    cov = coverage(discovery({"python": ("a.py",)}), [])
    doc = cov.as_dict()
    assert doc["uncovered"] == [{"language": "python", "files": 1, "examples": ["a.py"], "why": "no-check"}]
    assert set(doc) == {"files_total", "files_checked", "files_uncovered", "complete", "uncovered",
                                  "template_uncovered", "incomplete_checks"}


# ── exit code: unsupported-language (127) ────────────────────────────────────────

def test_nothing_applicable_but_source_exists_is_127_not_ok():
    results = [row("not-applicable", "filtered", set()), row("passed", "ok", set(), category="limits", id="rig-check@.")]
    assert lr.exit_code_for(results, bootstrap_failed=False, problems=(), uncovered_files=3) == lr.EXIT_MISSING


def test_no_source_files_stays_ok_but_any_uncovered_source_file_is_never_green():
    only_limits = [row("passed", "ok", set(), category="limits")]
    assert lr.exit_code_for(only_limits, bootstrap_failed=False, problems=(), uncovered_files=0) == lr.EXIT_OK
    # a passing Python check does not excuse an unchecked Kotlin file next to it
    verified = [row("passed", "ok", {"lint:python:."})]
    assert lr.exit_code_for(verified, bootstrap_failed=False, problems=(), uncovered_files=2) == lr.EXIT_MISSING


def test_unverified_source_ranks_with_missing_tools_above_incomplete_and_findings():
    """Priority 2 > 127 > 3 > 1 > 0 with unverified source counted as 127: it can never be outvoted into green."""
    findings = [row("failed", "findings", {"lint:python:."})]
    assert lr.exit_code_for(findings, bootstrap_failed=False, problems=(), uncovered_files=2) == lr.EXIT_MISSING
    timeout = [row("failed", "timeout", {"lint:python:."})]
    assert lr.exit_code_for(timeout, bootstrap_failed=False, problems=(), uncovered_files=2) == lr.EXIT_MISSING
    assert lr.exit_code_for(timeout, bootstrap_failed=False, problems=(), uncovered_files=0) == lr.EXIT_INCOMPLETE


# ── `complete` also requires every applicable check to have finished its analysis ──

@pytest.mark.parametrize("state,reason,listed", [
    ("failed", "crash", True), ("failed", "timeout", True), ("failed", "config-error", True),
    ("failed", "wrote-files", True), ("failed", "malformed-output", True),
    ("missing-tool", "tool-not-installed", True), ("missing-tool", "dependencies-not-installed", True),
    ("unsupported-platform", "platform-unsupported", True),
    ("passed", "ok", False), ("failed", "findings", False),
    ("not-applicable", "filtered", False), ("planned", "planned", False),
])
def test_complete_needs_every_applicable_check_to_finish(state, reason, listed):
    d = discovery({"python": ("a.py",)})
    results = [row("passed", "ok", {"lint:python:."}, id="ruff-check@."), row(state, reason, set(), id="other@.")]
    cov = coverage(d, results)
    assert (cov.files_checked, cov.files_uncovered) == (1, 0), "the covered file is still checked"
    assert cov.complete is (not listed)
    assert cov.incomplete_checks == (("other@.",) if listed else ())
