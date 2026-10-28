"""A file git TRACKS although an ignore rule matches it must be checked, or reported uncovered -- never "checked".

`git add -f vendor/lib/tracked.ts` with `vendor/` in `.gitignore`: Oxlint, Oxfmt, Ruff, Black and Prettier walk `.`
honouring `.gitignore` and never open it (probed with the real binaries), yet discovery enumerates it, so a naive
coverage count called it verified. These tests pin the three halves: discovery knows the file, the providers add an
explicit-operand check for exactly those files (never the ignored untracked trees) where the tool analyzes explicit
operands, and coverage counts a file as checked only through a check that can reach it (Prettier, which skips ignored
operands too, leaves them `tracked-but-ignored`). The last section runs the REAL pinned tools end to end.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from riglib import lint_providers as lp
from riglib.lint_discovery import discover
from riglib.lint_run_coverage import TRACKED_IGNORED, coverage
from riglib.lint_run_model import CheckResult
from tests.lint_discovery_helpers import make_repo as git_repo
from tests.lint_discovery_helpers import pkg
from tests.lint_providers_helpers import config, install, tool_env
from tests.lint_real_helpers import (
    build_repo,
    lint_env,
    require_agent_tools_source,
    run_lint,
    session_cache,
    settle,
    states,
)

LOCK = "# lock\n"
IGNORE = ".gitignore"
VENDORED_TS = "vendor/lib/tracked.ts"
VENDORED_PY = "vendor/lib/tracked.py"


def _force_add(root: Path, *rels: str) -> None:
    subprocess.run(["git", "add", "-f", "--", *rels], cwd=root, check=True)


def _js_repo(tmp_path, tools=("oxlint", "oxfmt"), **extra):
    files = {
        IGNORE: "node_modules/\nvendor/\n",
        "package.json": pkg(devDependencies={t: "1.0.0" for t in tools}), "package-lock.json": LOCK,
        "src/a.ts": "export const a = 1;\n", VENDORED_TS: "export const v = 1;\n",
        "vendor/untracked.ts": "export const u = 1;\n", "node_modules/dep/index.ts": "export const d = 1;\n", **extra,
    }
    root = git_repo(tmp_path, files)
    _force_add(root, VENDORED_TS)
    for tool in tools:
        install(root, f"node_modules/.bin/{tool}")
    return root


def _py_repo(tmp_path, **extra):
    root = git_repo(tmp_path, {
        IGNORE: "vendor/\n.venv/\n", "ruff.toml": "line-length = 100\n", "pyproject.toml": "[tool.black]\nline-length = 100\n",
        "src/a.py": "x = 1\n", VENDORED_PY: "y = 1\n", "vendor/untracked.py": "z = 1\n", **extra,
    })
    _force_add(root, VENDORED_PY)
    install(root, ".venv/bin/ruff")
    install(root, ".venv/bin/black")
    return root


def _plan(root, **env_kwargs):
    disc = discover(root)
    env = tool_env(root, which={"npm": "/bin/npm"}, **env_kwargs)
    return disc, {c.id: c for c in lp.plan_checks(disc, config(), env)}


def _operands(check, exe_name):
    """The argv after the executable (checks run from the scope directory)."""
    assert check.argv is not None and check.argv[0].endswith(exe_name), check.argv
    return check.argv[1:]


# ── discovery ──────────────────────────────────────────────────────────────────────


def test_discovery_lists_exactly_the_tracked_files_an_ignore_rule_matches(tmp_path):
    root = _js_repo(tmp_path)
    disc = discover(root)
    assert disc.tracked_ignored == (VENDORED_TS,)
    assert VENDORED_TS in disc.enumerated_files and "vendor/untracked.ts" not in disc.enumerated_files


def test_a_repo_without_ignore_rules_has_no_tracked_ignored_files(tmp_path):
    root = git_repo(tmp_path, {"package.json": pkg(name="x"), "src/a.ts": "export const a = 1;\n"})
    assert discover(root).tracked_ignored == ()


# ── providers: explicit operands for exactly those files ───────────────────────────


def test_oxlint_and_oxfmt_get_an_explicit_operand_check_for_the_tracked_ignored_file_only(tmp_path):
    disc, checks = _plan(_js_repo(tmp_path))
    lint, fmt = checks["oxlint@."], checks["oxfmt@."]
    assert not lint.reaches_ignored and not fmt.reaches_ignored, "the walking checks do not reach it"
    extra_lint, extra_fmt = checks["oxlint@.#tracked-ignored"], checks["oxfmt@.#tracked-ignored"]
    assert extra_lint.reaches_ignored and extra_fmt.reaches_ignored
    assert _operands(extra_fmt, "oxfmt")[-1] == VENDORED_TS and "--check" in _operands(extra_fmt, "oxfmt")
    assert _operands(extra_lint, "oxlint")[-1] == VENDORED_TS
    assert extra_fmt.covers == fmt.covers and extra_lint.covers == lint.covers
    every = " ".join(" ".join(c.argv or ()) for c in checks.values())
    assert "untracked.ts" not in every and "node_modules/dep" not in every, "ignored untracked trees are never operands"


def test_the_explicit_oxfmt_fix_variant_formats_only_that_file(tmp_path):
    disc, checks = _plan(_js_repo(tmp_path))
    extra = checks["oxfmt@.#tracked-ignored"]
    assert extra.fix_argv is not None and extra.fix_argv[1:] == (VENDORED_TS,), "bare `oxfmt <file>` writes; no --check"
    assert checks["oxlint@.#tracked-ignored"].fix_argv[1:] == ("--fix", VENDORED_TS) or "--fix" in checks["oxlint@.#tracked-ignored"].fix_argv


def test_ruff_and_black_get_explicit_operand_checks(tmp_path):
    disc, checks = _plan(_py_repo(tmp_path))
    assert _operands(checks["ruff-check@.#tracked-ignored"], "ruff") == ("check", VENDORED_PY)
    assert _operands(checks["ruff-format@.#tracked-ignored"], "ruff") == ("format", "--check", VENDORED_PY)
    assert _operands(checks["black@.#tracked-ignored"], "black") == ("--check", VENDORED_PY)
    assert checks["ruff-check@.#tracked-ignored"].fix_argv[1:] == ("check", "--fix", VENDORED_PY)
    assert checks["black@.#tracked-ignored"].fix_argv[1:] == (VENDORED_PY,)
    assert not checks["ruff-check@."].reaches_ignored and not checks["black@."].reaches_ignored


def test_a_leading_dash_file_name_is_never_read_as_an_option(tmp_path):
    root = git_repo(tmp_path, {IGNORE: "node_modules/\n/-evil.ts\n", "package.json": pkg(devDependencies={"oxlint": "1"}),
                               "package-lock.json": LOCK, "-evil.ts": "export const e = 1;\n", "src/a.ts": "export const a = 1;\n"})
    _force_add(root, "-evil.ts")
    install(root, "node_modules/.bin/oxlint")
    disc, checks = _plan(root)
    assert disc.tracked_ignored == ("-evil.ts",)
    assert _operands(checks["oxlint@.#tracked-ignored"], "oxlint")[-1] == "./-evil.ts"


def test_a_nested_scope_gets_scope_relative_operands(tmp_path):
    root = git_repo(tmp_path, {
        IGNORE: "node_modules/\nvendor/\n", "app/package.json": pkg(devDependencies={"oxlint": "1"}), "app/package-lock.json": LOCK,
        "app/src/a.ts": "export const a = 1;\n", "app/vendor/t.ts": "export const t = 1;\n"})
    _force_add(root, "app/vendor/t.ts")
    install(root, "app/node_modules/.bin/oxlint")
    disc, checks = _plan(root)
    extra = checks["oxlint@app#tracked-ignored"]
    assert extra.cwd == "app" and _operands(extra, "oxlint")[-1] == "vendor/t.ts"


def test_without_tracked_ignored_files_the_argv_is_unchanged(tmp_path):
    root = git_repo(tmp_path, {IGNORE: "node_modules/\n", "package.json": pkg(devDependencies={"oxlint": "1", "oxfmt": "1"}),
                               "package-lock.json": LOCK, "src/a.ts": "export const a = 1;\n"})
    install(root, "node_modules/.bin/oxlint")
    install(root, "node_modules/.bin/oxfmt")
    disc, checks = _plan(root)
    assert not [c for c in checks if "#tracked-ignored" in c]
    assert _operands(checks["oxfmt@."], "oxfmt") == ("--check",)


def test_many_files_are_chunked_under_the_argv_limit(tmp_path):
    names = [f"vendor/{'d' * 150}/f{i}.ts" for i in range(900)]
    root = git_repo(tmp_path, {IGNORE: "vendor/\nnode_modules/\n", "package.json": pkg(devDependencies={"oxlint": "1"}),
                               "package-lock.json": LOCK, **{n: "export const x = 1;\n" for n in names}})
    _force_add(root, *names)
    install(root, "node_modules/.bin/oxlint")
    disc, checks = _plan(root)
    extras = {i: c for i, c in checks.items() if i.startswith("oxlint@.#tracked-ignored")}
    assert len(extras) >= 2 and "oxlint@.#tracked-ignored-2" in extras
    operands = [a for c in extras.values() for a in c.argv if a.startswith("vendor/")]
    assert sorted(operands) == sorted(names), "every file is an operand of exactly one chunk"


# ── Prettier cannot be made to analyze them ────────────────────────────────────────


def test_prettier_gets_no_explicit_check_and_does_not_reach_the_ignored_file(tmp_path):
    root = _js_repo(tmp_path, tools=("prettier",))
    (root / ".prettierrc").write_text("{}\n", encoding="utf-8")
    disc, checks = _plan(root)
    assert "prettier@." in checks and not [c for c in checks if "#tracked-ignored" in c and c.startswith("prettier")]
    assert not checks["prettier@."].reaches_ignored


# ── coverage: only a check that can reach the file counts ──────────────────────────


def _result(check, state="passed", reason="ok") -> CheckResult:
    return CheckResult(check, state, reason, "", 0, 1, None, "", "", False, None, None)  # type: ignore[arg-type]


def _coverage(disc, checks, only=None):
    results = [_result(c) for i, c in checks.items() if only is None or only(i)]
    return coverage(disc, results)


def test_a_walking_check_alone_never_counts_the_tracked_ignored_file_as_checked(tmp_path):
    disc, checks = _plan(_js_repo(tmp_path))
    walking_only = _coverage(disc, checks, only=lambda i: "#tracked-ignored" not in i)
    assert walking_only.files_uncovered == 1 and walking_only.complete is False
    [group] = [g for g in walking_only.uncovered if g.why == TRACKED_IGNORED]
    assert group.examples == (VENDORED_TS,) and group.language == "typescript"
    full = _coverage(disc, checks)
    assert full.files_uncovered == 0 and full.complete is True and full.files_checked == 2


def test_a_failed_explicit_check_does_not_let_the_walking_check_vouch_for_the_file(tmp_path):
    disc, checks = _plan(_js_repo(tmp_path))
    results = [_result(c, "failed", "crash") if "#tracked-ignored" in i else _result(c) for i, c in checks.items()]
    cov = coverage(disc, results)
    assert cov.files_uncovered == 1 and cov.uncovered[0].why == TRACKED_IGNORED and not cov.complete


def test_prettier_only_repo_reports_the_file_uncovered_with_an_actionable_reason(tmp_path):
    root = _js_repo(tmp_path, tools=("prettier",))
    (root / ".prettierrc").write_text("{}\n", encoding="utf-8")
    disc, checks = _plan(root)
    cov = _coverage(disc, checks, only=lambda i: i.startswith("prettier"))
    assert cov.files_uncovered == 1 and cov.uncovered[0].why == TRACKED_IGNORED and cov.complete is False
    from riglib.lint_run_report import _uncovered_line

    line = _uncovered_line({"language": "typescript", "files": 1, "examples": [VENDORED_TS], "why": TRACKED_IGNORED})
    assert "un-ignore it, untrack it, or accept" in line and VENDORED_TS in line


def test_a_project_script_cannot_vouch_for_a_tracked_ignored_file(tmp_path):
    root = git_repo(tmp_path, {
        IGNORE: "node_modules/\nvendor/\n", "package.json": pkg(scripts={"lint": "eslint ."}, devDependencies={"eslint": "9"}),
        "package-lock.json": LOCK, "eslint.config.js": "export default [];\n", "src/a.ts": "export const a = 1;\n",
        VENDORED_TS: "export const v = 1;\n"})
    _force_add(root, VENDORED_TS)
    install(root, "node_modules/.bin/eslint")
    install(root, "node_modules/.bin/npm")
    disc, checks = _plan(root)
    script = next(c for c in checks.values() if c.provider == "script")
    assert not script.reaches_ignored


# ── real pinned tools end to end ───────────────────────────────────────────────────

JS_ARGS = ("--only", "lint,format")


@pytest.fixture
def env(tmp_path) -> dict[str, str]:
    return lint_env(tmp_path / "home", session_cache(), catalog=require_agent_tools_source())


def _repo_with_vendored(tmp_path, files, vendored: dict[str, str]):
    repo = build_repo(tmp_path, "vend", {IGNORE: "node_modules/\n.venv/\n__pycache__/\nvendor/\n", **files})
    for rel, text in vendored.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _force_add(repo, *vendored)
    subprocess.run(["git", "commit", "-q", "-m", "vendored"], cwd=repo, check=True)
    return repo


def test_real_oxlint_and_oxfmt_fail_on_a_tracked_ignored_file_with_findings(tmp_path, env):
    repo = _repo_with_vendored(tmp_path, {"src/a.ts": "export const a = 1;\n"}, {VENDORED_TS: "debugger;\nexport   const   v  =  1\n"})
    doc = settle(run_lint(repo, env, *JS_ARGS), "oxlint@.", "oxfmt@.")
    got = states(doc)
    assert doc["exit_code"] == 1 and doc["ok"] is False, got
    assert got["oxlint@.#tracked-ignored"] == "failed" and got["oxfmt@.#tracked-ignored"] == "failed"
    assert got["oxlint@."] == "passed" and got["oxfmt@."] == "passed", "the walking checks never saw the file"


def test_real_tools_count_a_clean_tracked_ignored_file_as_checked_and_stay_green(tmp_path, env):
    repo = _repo_with_vendored(tmp_path, {"src/a.ts": "export const a = 1;\n"}, {VENDORED_TS: "export const v = 1;\n"})
    doc = settle(run_lint(repo, env, *JS_ARGS), "oxlint@.", "oxfmt@.")
    got = states(doc)
    assert got["oxlint@.#tracked-ignored"] == got["oxfmt@.#tracked-ignored"] == "passed"
    assert doc["exit_code"] == 0 and doc["complete"] is True and doc["coverage"]["files_checked"] == 2
    assert doc["coverage"]["files_uncovered"] == 0


def test_real_ruff_fails_on_a_tracked_ignored_python_file_and_never_scans_untracked_ignored_ones(tmp_path, env):
    repo = _repo_with_vendored(
        tmp_path, {"src/a.py": "x = 1\n", "vendor/untracked.py": "import os\n"}, {VENDORED_PY: "import os\n\nVALUE = 1\n"}
    )
    (repo / "vendor/untracked.py").write_text("import sys\n", encoding="utf-8")  # an ignored UNTRACKED finding
    doc = settle(run_lint(repo, env, *JS_ARGS), "ruff-check@.", "ruff-format@.")
    got = states(doc)
    assert doc["exit_code"] == 1 and got["ruff-check@.#tracked-ignored"] == "failed", got
    check = next(c for c in doc["checks"] if c["id"] == "ruff-check@.#tracked-ignored")
    text = check["output"]["stdout"] + check["output"]["stderr"]
    assert "tracked.py" in text and "untracked.py" not in text, "never widened to ignored untracked trees"
