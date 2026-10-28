"""A default `rig lint` is never green while something applicable went unchecked.

CI consumes the exit code, so one passing language must not hide an unsupported neighbour (Ruff passing
next to Kotlin), a template gap (Oxlint passing next to Vue templates it cannot lint), or an
incomplete analysis. An explicit narrowing (`--only/--skip/--scope`) returns the selected checks'
verdict with `complete: false` and the omissions listed; `--plan` is a preview (0); files the repo
ignores (`.gitignore`) are not missing coverage.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from riglib import lint_run as lr
from tests.lint_check_helpers import commit_all, make_repo, put
from tests.lint_run_helpers import LOADER_ENV, FakeRunner, outcome, which_in, write_exe

RIG_CHECK_DOC = json.dumps({"schema": "rig-lint-check/v1", "ok": True, "summary": {"errors": 0, "warnings": 0}})


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    # A hermetic agent-tools checkout: without one the required anti-slop policy is (correctly) reported
    # `required-policy-unavailable` for a declared Oxlint, which is not what these coverage tests are about.
    carrier = tmp_path / "agent-tools" / "vendor/anti-slop/skills/install-anti-slop/assets/anti-slop"
    carrier.mkdir(parents=True)
    (carrier / "index.ts").write_text("export default {};\n", encoding="utf-8")
    for name in ("skills", "agent-hooks"):
        (tmp_path / "agent-tools" / name).mkdir()
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(tmp_path / "agent-tools"))
    for name in ("CI", "GITHUB_ACTIONS", "RIG_LINT_DEPTH"):
        monkeypatch.delenv(name, raising=False)


def passing_runner() -> FakeRunner:
    def answer(spec):
        if len(spec.argv) > 1 and spec.argv[1].endswith("lint_check_launcher.py"):
            return outcome(0, RIG_CHECK_DOC)
        return outcome(0)
    return FakeRunner(default=answer)  # type: ignore[arg-type]


def local_tool(repo: Path, name: str) -> None:
    write_exe(repo / "node_modules" / ".bin" / name, "import sys; sys.exit(0)\n")


def run(repo: Path, tmp_path: Path, **over):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    base = dict(cwd=str(repo), config=None, json=True, plan=False, no_setup=True, fix=False, ci=None,
                only=(), skip=(), scopes=(), timeout=None)
    return lr.execute(lr.RunOptions(**{**base, **over}), environ={"PATH": str(bindir)}, runner=passing_runner(),
                      which=which_in(bindir))


def python_kotlin_repo(tmp_path: Path) -> Path:
    repo = make_repo(tmp_path)
    put(repo, "pyproject.toml", "[tool.ruff]\nline-length = 100\n")
    put(repo, "app.py", "x = 1\n")
    put(repo, "src/Main.kt", "fun main() {}\n")
    put(repo, ".gitignore", ".venv\n")
    write_exe(repo / ".venv" / "bin" / "ruff", "import sys; sys.exit(0)\n")
    commit_all(repo)
    return repo


def vue_repo(tmp_path: Path) -> Path:
    repo = make_repo(tmp_path)
    put(repo, "package.json", json.dumps({"dependencies": {"vue": "3.5.0"}, "devDependencies": {"oxlint": "1.90.0", "oxfmt": "0.75.0"}}))
    put(repo, "src/App.vue", "<template><p>{{ msg }}</p></template>\n<script setup>\nconst msg = 'hi'\n</script>\n")
    put(repo, "src/main.js", "export const a = 1;\n")
    put(repo, ".oxlintrc.json", "{}\n")  # a project-owned config: no Rig policy (and no catalog) is involved
    put(repo, ".gitignore", "node_modules\n")
    commit_all(repo)
    local_tool(repo, "oxlint")
    local_tool(repo, "oxfmt")
    return repo


# ── Ruff passes, Kotlin is unchecked ─────────────────────────────────────────────

def test_python_passing_next_to_kotlin_is_nonzero_and_names_the_gap(tmp_path):
    report = run(python_kotlin_repo(tmp_path), tmp_path)
    doc = report.doc
    ruff = [r for r in doc["checks"] if r["provider"] in ("ruff-check", "ruff-format")]
    assert ruff and all(r["state"] == "passed" for r in ruff), [(r["id"], r["state"], r["reason"]) for r in doc["checks"]]
    assert report.exit_code == 127 and doc["ok"] is False and doc["complete"] is False
    assert [u["language"] for u in doc["coverage"]["uncovered"]] == ["kotlin"]
    assert "kotlin" in report.text and "unsupported-language" in report.text


def test_narrowed_run_returns_the_selected_checks_verdict_with_the_omission_listed(tmp_path):
    repo = python_kotlin_repo(tmp_path)
    report = run(repo, tmp_path, only=("lint",))
    assert report.exit_code == 0 and report.doc["complete"] is False
    assert report.doc["coverage"]["uncovered"], "the omission stays visible in the document"
    assert "NOT COMPLETE" in report.text


def test_scope_narrowing_returns_the_verdict_of_the_scope(tmp_path):
    report = run(python_kotlin_repo(tmp_path), tmp_path, scopes=("src",))
    # A deliberate `--scope` narrowing keeps the omission visible but is not itself a failure.
    assert report.doc["complete"] is False and report.exit_code == 0
    assert report.doc["coverage"]["files_uncovered"] == 2


def test_plan_is_a_preview_and_exits_zero_even_with_gaps(tmp_path):
    report = run(python_kotlin_repo(tmp_path), tmp_path, plan=True)
    assert report.exit_code == 0 and report.doc["ok"] is None and report.doc["complete"] is False
    assert "PLAN ONLY" in report.text


# ── Vue template gap ─────────────────────────────────────────────────────────────

def test_vue_templates_unlinted_make_a_passing_js_run_nonzero(tmp_path):
    report = run(vue_repo(tmp_path), tmp_path)
    doc = report.doc
    oxlint = next(r for r in doc["checks"] if r["id"] == "oxlint@.")
    assert oxlint["state"] == "passed"
    assert [(g["scope"], g["framework"], g["reason"]) for g in doc["coverage"]["template_uncovered"]] == [
        (".", "vue", "template-lint-uncovered")]
    assert report.exit_code == 127 and doc["ok"] is False and doc["complete"] is False
    assert "template-lint-uncovered" in report.text and "ESLint" in report.text


def test_a_configured_eslint_covers_the_templates(tmp_path):
    repo = vue_repo(tmp_path)
    put(repo, "eslint.config.js", "export default [];\n")
    put(repo, "package.json", json.dumps({"dependencies": {"vue": "3.5.0"},
                                          "devDependencies": {"eslint": "9.0.0", "oxlint": "1.90.0", "oxfmt": "0.75.0"}}))
    commit_all(repo)
    local_tool(repo, "eslint")
    report = run(repo, tmp_path)
    assert report.doc["coverage"]["template_uncovered"] == []
    assert report.exit_code == 0 and report.doc["complete"] is True, report.text


def test_template_gap_with_explicit_narrowing_keeps_the_selected_verdict(tmp_path):
    report = run(vue_repo(tmp_path), tmp_path, only=("lint",))
    assert report.exit_code == 0 and report.doc["complete"] is False
    assert report.doc["coverage"]["template_uncovered"]


# ── ignored files are not missing coverage ───────────────────────────────────────

def test_gitignored_vendor_tree_is_not_missing_coverage(tmp_path):
    repo = make_repo(tmp_path)
    put(repo, "pyproject.toml", "[tool.ruff]\nline-length = 100\n")
    put(repo, "app.py", "x = 1\n")
    put(repo, ".gitignore", ".venv\nvendor/\n")
    put(repo, "vendor/lib/Thing.kt", "class Thing\n")
    put(repo, "vendor/lib/tool.rb", "puts 1\n")
    write_exe(repo / ".venv" / "bin" / "ruff", "import sys; sys.exit(0)\n")
    commit_all(repo)
    assert (repo / "vendor" / "lib" / "Thing.kt").exists()
    report = run(repo, tmp_path)
    assert report.doc["coverage"]["uncovered"] == [] and report.doc["discovery"]["unknown_languages"] == []
    assert report.doc["complete"] is True and report.exit_code == 0, report.text


def test_a_tracked_kotlin_file_next_to_the_ignored_one_is_still_reported(tmp_path):
    repo = python_kotlin_repo(tmp_path)
    put(repo, ".gitignore", ".venv\nvendor/\n")
    put(repo, "vendor/Skip.kt", "class Skip\n")
    commit_all(repo)
    report = run(repo, tmp_path)
    languages = {u["language"]: u["files"] for u in report.doc["coverage"]["uncovered"]}
    assert languages == {"kotlin": 1}, "only src/Main.kt, never the ignored vendor file"


# ── incomplete analysis next to a passing check stays nonzero ────────────────────

def test_timeout_next_to_a_passing_check_is_incomplete_not_green(tmp_path):
    repo = python_kotlin_repo(tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)

    def answer(spec):
        if spec.argv[0].endswith("/ruff") and "format" in spec.argv:
            return outcome(None, timed_out=True)
        return outcome(0, RIG_CHECK_DOC if spec.argv[1:2] and str(spec.argv[1]).endswith("lint_check_launcher.py") else "")
    base = dict(cwd=str(repo), config=None, json=True, plan=False, no_setup=True, fix=False, ci=None,
                only=(), skip=(), scopes=(), timeout=None)
    report = lr.execute(lr.RunOptions(**base), environ={"PATH": str(bindir)}, runner=FakeRunner(default=answer),  # type: ignore[arg-type]
                        which=which_in(bindir))
    rows = {c["id"]: c for c in report.doc["checks"]}
    assert (rows["ruff-format@."]["state"], rows["ruff-format@."]["reason"]) == ("failed", "timeout")
    assert rows["ruff-check@."]["state"] == "passed"
    # unverified Kotlin ranks with a missing tool (127) above the incomplete timeout (3); both stay visible
    assert report.exit_code == 127 and report.doc["ok"] is False
    assert report.doc["coverage"]["uncovered"], "the unchecked Kotlin file is listed next to the timeout row"


@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("TRUE", True), (" yes ", True), ("on", True),
    ("banana", False), ("2", False), ("y", False),
    ("", False), ("0", False), ("false", False), ("no", False), ("off", False), (" FALSE ", False),
])
def test_reported_ci_matches_the_bootstrap_predicate(value, expected):
    from riglib.lint_ci_env import ci_env_truthy
    from riglib.lint_run_plan import ci_from

    opts = lr.RunOptions(cwd=".", config=None, json=True, plan=False, no_setup=False, fix=False, ci=None,
                         only=(), skip=(), scopes=(), timeout=None)
    assert ci_from(opts, {"CI": value}) is expected is ci_env_truthy({"CI": value})


# ── `--skip scripts` is not a narrowing: it must never make uncovered source green ─

@pytest.mark.parametrize("over", [{"skip": ("scripts",)}, {"skip": ("limits", "scripts")}])
def test_skipping_scripts_keeps_the_uncovered_gate(tmp_path, over):
    report = run(python_kotlin_repo(tmp_path), tmp_path, **over)
    assert report.exit_code == 127 and report.doc["ok"] is False and report.doc["complete"] is False
    assert [u["language"] for u in report.doc["coverage"]["uncovered"]] == ["kotlin"]
    assert "unsupported-language" in report.text


def test_only_scripts_is_a_real_narrowing_and_keeps_the_selected_verdict(tmp_path):
    report = run(python_kotlin_repo(tmp_path), tmp_path, only=("scripts",))
    assert report.exit_code == 0 and report.doc["complete"] is False and "NOT COMPLETE" in report.text


def test_scripts_disabled_in_rig_yaml_does_not_switch_the_gate_off(tmp_path):
    repo = python_kotlin_repo(tmp_path)
    put(repo, "rig.yaml", "version: 1\nlint:\n  categories: {scripts: false}\n")
    commit_all(repo)
    report = run(repo, tmp_path)
    assert report.exit_code == 127 and report.doc["complete"] is False


# ── `complete` also means every applicable check finished its analysis ──────────

CRASH_TRACE = "TypeError: boom\n    at Object.<anonymous> (/x/node_modules/typescript/lib/tsc.js:10:5)\n"


def ts_repo(tmp_path: Path, tsc_body: str) -> Path:
    repo = make_repo(tmp_path)
    put(repo, "package.json", json.dumps({"devDependencies": {"oxlint": "1.90.0", "oxfmt": "0.75.0",
                                                              "typescript": "5.6.0"}}))
    put(repo, "tsconfig.json", "{}\n")
    put(repo, "src/a.ts", "export const a = 1;\n")
    put(repo, ".oxlintrc.json", "{}\n")
    put(repo, ".gitignore", "node_modules\n")
    commit_all(repo)
    for name in ("oxlint", "oxfmt"):
        local_tool(repo, name)
    write_exe(repo / "node_modules" / ".bin" / "tsc", tsc_body)
    return repo


def run_real(repo: Path, **over):
    """Real subprocesses for the shim tools; only the nested `rig lint check` is answered by a canned document."""
    def runner(spec):
        if len(spec.argv) > 1 and spec.argv[1].endswith("lint_check_launcher.py"):
            return outcome(0, RIG_CHECK_DOC)
        return lr.subprocess_runner(spec)
    base = dict(cwd=str(repo), config=None, json=True, plan=False, no_setup=True, fix=False, ci=None,
                only=(), skip=(), scopes=(), timeout=30)
    return lr.execute(lr.RunOptions(**{**base, **over}), environ={"PATH": os.defpath, "HOME": str(repo.parent), **LOADER_ENV},
                      runner=runner, which=lambda name: None)


@pytest.mark.parametrize("tsc_body,reason", [
    (f"import sys; sys.stderr.write({CRASH_TRACE!r}); sys.exit(6)\n", "crash"),
    ("import sys; sys.stderr.write('error TS5083: Cannot read file tsconfig.json\\n'); sys.exit(2)\n", "config-error"),
    ("import time; time.sleep(60)\n", "timeout"),
])
def test_a_crashed_check_makes_the_run_incomplete_even_when_other_checks_cover_the_files(tmp_path, tsc_body, reason):
    """Real shim tsc crash / config failure / timeout next to passing Oxlint + Oxfmt over the same TS file."""
    report = run_real(ts_repo(tmp_path, tsc_body), **({"timeout": 1} if reason == "timeout" else {}))
    doc = report.doc
    rows = {c["id"]: c for c in doc["checks"]}
    assert (rows["tsc@."]["state"], rows["tsc@."]["reason"]) == ("failed", reason)
    assert rows["oxlint@."]["state"] == rows["oxfmt@."]["state"] == "passed"
    # oxlint/oxfmt really analyzed the file, so it still counts as checked ...
    assert (doc["summary"]["files_checked"], doc["summary"]["files_uncovered"]) == (1, 0)
    # ... but the run is NOT complete: a typecheck that could not finish is a hole the file count cannot show
    assert doc["complete"] is False and doc["summary"]["complete"] is False
    assert doc["coverage"]["complete"] is False and doc["coverage"]["incomplete_checks"] == ["tsc@."]
    assert report.exit_code == 3 and doc["ok"] is False
    assert "analysis not finished by: tsc@." in report.text


def test_findings_are_analysis_so_the_run_stays_complete(tmp_path):
    report = run_real(ts_repo(tmp_path, "import sys; sys.stderr.write('src/a.ts(1,7): error TS2322: bad\\n'); sys.exit(2)\n"))
    rows = {c["id"]: c for c in report.doc["checks"]}
    assert (rows["tsc@."]["state"], rows["tsc@."]["reason"]) == ("failed", "findings")
    assert report.doc["complete"] is True and report.doc["coverage"]["incomplete_checks"] == []
    assert report.exit_code == 1


def test_a_fully_passing_run_is_complete(tmp_path):
    report = run_real(ts_repo(tmp_path, "import sys; sys.exit(0)\n"))
    assert report.exit_code == 0 and report.doc["complete"] is True and report.doc["coverage"]["incomplete_checks"] == []


def test_a_missing_tool_is_listed_as_an_unfinished_check(tmp_path):
    repo = ts_repo(tmp_path, "import sys; sys.exit(0)\n")
    (repo / "node_modules" / ".bin" / "tsc").unlink()
    report = run_real(repo)
    rows = {c["id"]: c for c in report.doc["checks"]}
    assert rows["tsc@."]["state"] == "missing-tool"
    assert report.doc["complete"] is False and "tsc@." in report.doc["coverage"]["incomplete_checks"]
