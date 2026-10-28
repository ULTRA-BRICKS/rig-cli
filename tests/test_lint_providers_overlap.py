"""Overlap resolution: one TypeScript typecheck, project scripts outrank tools, distinct tools never dedupe each other."""
from __future__ import annotations

from riglib import lint_providers as lp
from tests.lint_providers_helpers import (
    JS,
    PY,
    TS,
    install,
    make_repo,
    plan,
    runnable,
    scope,
    script,
    state,
    tool,
    ts_info,
)

TSC_KEY = "typecheck:typescript:."


def _tsc_repo(tmp_path, *, scripts=(), tools=(), major=5):
    root = make_repo(tmp_path)
    for name in ("tsc", "oxlint", "oxfmt", "eslint"):
        install(root, f"node_modules/.bin/{name}")
    sc = scope(".", (TS,), tools=tools, scripts=scripts, typescript=ts_info(major=major))
    return plan(root, sc, which={"npm": "/bin/npm"})


def test_tsc_alone_is_the_typecheck(tmp_path):
    checks = _tsc_repo(tmp_path)
    assert checks["tsc@."].unavailable is None and TSC_KEY in checks["tsc@."].covers


def test_typecheck_script_supersedes_tsc(tmp_path):
    checks = _tsc_repo(tmp_path, scripts=(script("typecheck", ("typecheck",), runs=("tsc:typecheck",)),))
    assert state(checks["tsc@."]) == ("not-applicable", "covered-by:script:package.json:typecheck@.")
    assert checks["tsc@."].argv is None
    assert checks["script:package.json:typecheck@."].argv == ("/bin/npm", "run", "typecheck")


def test_oxlint_type_check_option_supersedes_tsc_when_prerequisites_hold(tmp_path):
    oxlint = tool("oxlint", "lint", options={"type_check": "true"})
    tsgolint = tool("oxlint-tsgolint", "lint")
    checks = _tsc_repo(tmp_path, tools=(oxlint, tsgolint), major=7)
    assert TSC_KEY in checks["oxlint@."].covers
    assert state(checks["tsc@."]) == ("not-applicable", "covered-by:oxlint@.")
    assert checks["oxlint@."].unavailable is None


def test_oxlint_type_check_option_does_not_cover_on_typescript_5(tmp_path):
    oxlint = tool("oxlint", "lint", options={"type_check": "true"})
    checks = _tsc_repo(tmp_path, tools=(oxlint, tool("oxlint-tsgolint", "lint")), major=5)
    assert TSC_KEY not in checks["oxlint@."].covers and checks["tsc@."].unavailable is None


def test_exactly_one_typecheck_with_script_oxlint_and_tsc(tmp_path):
    oxlint = tool("oxlint", "lint", options={"type_check": "true"})
    scripts = (script("typecheck", ("typecheck",), runs=("tsc:typecheck",)),)
    checks = _tsc_repo(tmp_path, scripts=scripts, tools=(oxlint, tool("oxlint-tsgolint", "lint")), major=7)
    dedicated = [c.id for c in checks.values() if c.unavailable is None and c.category in ("typecheck", "scripts")]
    assert dedicated == ["script:package.json:typecheck@."]
    assert state(checks["tsc@."])[1].startswith("covered-by:")
    # Oxlint cannot run without its configured typeCheck half, so it keeps running as the linter.
    assert checks["oxlint@."].unavailable is None


def test_lint_script_supersedes_configured_lint_tool_but_not_formatter(tmp_path):
    root = make_repo(tmp_path)
    install(root, "node_modules/.bin/eslint")
    install(root, "node_modules/.bin/prettier")
    tools = (tool("eslint", "lint", config=("e.js",)), tool("prettier", "format"))
    sc = scope(".", (JS,), tools=tools, scripts=(script("lint", ("lint",), runs=("eslint:lint",)),))
    checks = plan(root, sc, which={"npm": "/bin/npm"})
    assert state(checks["eslint@."]) == ("not-applicable", "covered-by:script:package.json:lint@.")
    assert checks["prettier@."].unavailable is None


def test_skipped_script_does_not_hide_the_tool_check(tmp_path):
    root = make_repo(tmp_path)
    install(root, "node_modules/.bin/eslint")
    sc = scope(".", (JS,), tools=(tool("eslint", "lint", config=("e.js",)),),
               scripts=(script("lint", ("lint",), rig_lint=True),))
    checks = plan(root, sc, which={"npm": "/bin/npm"})
    assert checks["eslint@."].unavailable is None
    assert state(checks["script:package.json:lint@."]) == ("not-applicable", "self-referential-script")
    guarded = plan(root, sc, which={"npm": "/bin/npm"}, depth=1)
    assert guarded["eslint@."].unavailable is None


def test_two_distinct_configured_linters_both_run(tmp_path):
    root = make_repo(tmp_path)
    for name in ("eslint", "biome"):
        install(root, f"node_modules/.bin/{name}")
    sc = scope(".", (JS,), tools=(tool("eslint", "lint", config=("e.js",)), tool("biome", "lint")))
    checks = plan(root, sc)
    assert {"eslint@.", "biome-lint@."} <= runnable(checks)


def test_mypy_and_pyright_both_run(tmp_path):
    root = make_repo(tmp_path)
    install(root, ".venv/bin/mypy")
    install(root, ".venv/bin/pyright")
    sc = scope(".", (PY,), tools=(tool("mypy", "typecheck"), tool("pyright", "typecheck")))
    assert {"mypy@.", "pyright@."} <= runnable(plan(root, sc))


def test_complementary_formatters_and_linters_all_run(tmp_path):
    root = make_repo(tmp_path)
    for name in ("biome", "prettier"):
        install(root, f"node_modules/.bin/{name}")
    sc = scope(".", (JS,), tools=(tool("biome", "lint"), tool("prettier", "format")))
    checks = plan(root, sc)
    assert {"biome-format@.", "biome-lint@.", "prettier@."} <= runnable(checks)


def _check(check_id, tool_name, category, *, precedence=1, argv=("x",), runs=(), unavailable=None):
    return lp.PlannedCheck(check_id, tool_name, category, ".", tool_name, precedence, frozenset(), argv, ".",
                           unavailable, runs=frozenset(runs))


def test_not_applicable_checks_claim_nothing():
    na = lp.Unavailable("not-applicable", "x", "d")
    skipped_script = _check("s@.", "npm", "scripts", precedence=0, argv=None, runs=("eslint:lint",), unavailable=na)
    eslint = _check("eslint@.", "eslint", "lint")
    assert lp.resolve_overlaps([skipped_script, eslint])[1].unavailable is None


def test_missing_tool_claimant_still_claims_so_the_gap_is_not_hidden_by_a_duplicate():
    gap = lp.Unavailable("missing-tool", "tool-not-installed", "d")
    script_check = _check("s@.", "npm", "scripts", precedence=0, argv=None, runs=("eslint:lint",), unavailable=gap)
    eslint = _check("eslint@.", "eslint", "lint")
    result = lp.resolve_overlaps([eslint, script_check])
    assert result[0].unavailable.reason == "covered-by:s@." and result[1] == script_check


def test_same_category_without_a_claim_never_dedupes():
    eslint = _check("eslint@.", "eslint", "lint")
    oxlint = _check("oxlint@.", "oxlint", "lint")
    assert lp.resolve_overlaps([oxlint, eslint]) == (oxlint, eslint)


def test_claim_applies_only_to_the_same_scope():
    script_check = _check("s@web", "npm", "scripts", precedence=0, runs=("eslint:lint",))
    script_check = lp.PlannedCheck(**{**script_check.__dict__, "scope": "web", "cwd": "web"})
    eslint = _check("eslint@.", "eslint", "lint")
    assert lp.resolve_overlaps([script_check, eslint])[1].unavailable is None


def test_identical_tool_scope_and_argv_is_a_true_duplicate():
    first = _check("a@.", "eslint", "lint", argv=("eslint", "."))
    second = _check("b@.", "eslint", "lint", argv=("eslint", "."))
    other_args = _check("c@.", "eslint", "lint", argv=("eslint", "src"))
    result = lp.resolve_overlaps([first, second, other_args])
    assert result[0].unavailable is None and result[2].unavailable is None
    assert result[1].unavailable.reason == "covered-by:a@."


def test_partial_claim_keeps_the_other_checks_and_order_is_preserved():
    cover = _check("s@.", "npm", "scripts", precedence=0, runs=("tsc:typecheck",))
    wide = _check("w@.", "oxlint", "lint")
    tsc = _check("tsc@.", "tsc", "typecheck")
    result = lp.resolve_overlaps([wide, tsc, cover])
    assert [c.id for c in result] == ["w@.", "tsc@.", "s@."]
    assert result[0].unavailable is None and result[1].unavailable.reason == "covered-by:s@."


def test_checks_without_claims_are_never_deduped():
    a = _check("a@.", "a", "limits", precedence=0, argv=("a",))
    b = _check("b@.", "b", "limits", precedence=3, argv=("b",))
    assert lp.resolve_overlaps([a, b]) == (a, b)
