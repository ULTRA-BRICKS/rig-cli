"""`lint_run.execute`: the full flow on real tmp git repos with a scripted runner and shim tools.

Discovery, provider planning and bootstrap planning are the real modules; only process execution is
faked (or, for the recursion test, real on a shim package manager). Nothing touches the real HOME,
the network or a real npm/uv.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from riglib import lint_run as lr
from tests.lint_check_helpers import commit_all, git, make_repo, put
from tests.lint_run_helpers import LOADER_ENV, FakeRunner, outcome, tree_digest, which_in, write_exe

RIG_CHECK_DOC = json.dumps({"schema": "rig-lint-check/v1", "ok": True, "summary": {"errors": 0, "warnings": 0}})


def default_runner(**script) -> FakeRunner:
    """Every command passes; the nested `rig lint check` answers with a valid document."""
    def answer(spec):
        if len(spec.argv) > 1 and spec.argv[1].endswith("lint_check_launcher.py"):
            return outcome(0, RIG_CHECK_DOC)
        return outcome(0)
    return FakeRunner(script, default=answer)  # type: ignore[arg-type]


def opts(repo: Path, **over) -> lr.RunOptions:
    base = dict(cwd=str(repo), config=None, json=True, plan=False, no_setup=False, fix=False, ci=None,
                only=(), skip=(), scopes=(), timeout=None)
    return lr.RunOptions(**{**base, **over})


@pytest.fixture
def bindir(tmp_path, monkeypatch):
    """Shim directory. The bootstrap action handler spawns through the PROCESS PATH (not the env handed
    to `execute`), so the shims must win there too or a real package manager would run."""
    path = tmp_path / "bin"
    path.mkdir()
    monkeypatch.setenv("PATH", f"{path}{os.pathsep}{os.environ.get('PATH', os.defpath)}")
    return path


def go(repo, bindir, runner=None, *, environ=None, **over):
    runner = runner or default_runner()
    # The planner reads the tool-cache root from this environment and the apply handler from the process
    # environment: a real run has one environment, so the tests hand both the same cache variables.
    cache = {k: os.environ[k] for k in ("HOME", "RIG_LINT_TOOL_CACHE", "RIG_AGENT_TOOLS_SOURCE") if k in os.environ}
    env = {"PATH": str(bindir), **LOADER_ENV, **cache, **(environ or {})}
    report = lr.execute(opts(repo, **over), environ=env, runner=runner, which=which_in(bindir))
    return report, runner


def rows(report) -> dict[str, dict]:
    return {r["id"]: r for r in report.doc["checks"]}


def local_tool(repo: Path, name: str) -> None:
    write_exe(repo / "node_modules" / ".bin" / name, "import sys; sys.exit(0)\n")


def eslint_repo(tmp_path, name="repo") -> Path:
    repo = make_repo(tmp_path, name)
    put(repo, "package.json", json.dumps({"devDependencies": {"eslint": "9.0.0", "prettier": "3.0.0"}}))
    put(repo, "eslint.config.js", "export default [];\n")
    put(repo, ".prettierrc", "{}\n")
    put(repo, ".gitignore", "node_modules\n")
    local_tool(repo, "eslint")
    local_tool(repo, "prettier")
    commit_all(repo)
    return repo


# ── document shape (the rig-lint-run/v1 contract) ────────────────────────────────

STATES = {"passed", "failed", "missing-tool", "not-applicable", "unsupported-platform", "planned"}


def assert_valid_doc(doc: dict) -> None:
    assert doc["schema"] == "rig-lint-run/v1"
    assert isinstance(doc["exit_code"], int) and doc["ok"] in (True, False, None)
    assert Path(doc["root"]).is_absolute()
    assert set(doc["mode"]) == {"ci", "plan", "fix", "setup"}
    assert set(doc["config"]) == {"enabled", "timeout_seconds", "categories", "bootstrap", "providers"}
    assert set(doc["discovery"]) == {"scopes", "unknown_languages", "unsupported_tools", "problems", "skipped_symlinks"}
    assert set(doc["bootstrap"]) == {"steps", "blocked", "results", "notes"}
    for scope in doc["discovery"]["scopes"]:
        assert {"path", "languages", "package_manager", "tools", "scripts"} <= set(scope)
    for row in doc["checks"]:
        assert set(row) >= {"id", "provider", "category", "scope", "tool", "state", "reason", "detail", "argv", "cwd",
                            "exit_code", "duration_ms", "timeout_s", "resolution", "counts", "output", "fix", "report"}
        assert row["state"] in STATES and row["reason"] and isinstance(row["detail"], str)
        assert set(row["output"]) == {"stdout", "stderr", "truncated"}
    s = doc["summary"]
    assert s["total"] == len(doc["checks"]) and s["applicable"] == s["total"] - s["not_applicable"]
    assert set(s) == {"total", "applicable", "passed", "failed", "missing_tool", "not_applicable",
                      "unsupported_platform", "planned", "files_checked", "files_uncovered", "complete",
                      "duration_ms"}
    assert set(doc["coverage"]) == {"files_total", "files_checked", "files_uncovered", "complete", "uncovered",
                                  "template_uncovered", "incomplete_checks"}
    assert doc["complete"] is s["complete"] is doc["coverage"]["complete"]
    json.dumps(doc)


# ── aggregation ──────────────────────────────────────────────────────────────────

def test_failing_check_does_not_stop_the_rest_and_exit_is_findings(tmp_path, bindir):
    repo = eslint_repo(tmp_path)
    runner = FakeRunner(default=lambda spec: outcome(1, "src/a.js: no-unused-vars")
                        if spec.argv[0].endswith("/eslint") else outcome(0))
    report, runner = go(repo, bindir, runner, only=("lint", "format"), no_setup=True)
    states = {i: r["state"] for i, r in rows(report).items()}
    assert states["eslint@."] == "failed" and states["prettier@."] == "passed"
    assert report.exit_code == 1 and report.doc["ok"] is False
    assert len(runner.specs) == 2, "both applicable checks ran although the first failed"
    assert_valid_doc(report.doc)


def test_mixed_states_document_and_missing_tool_outranks_findings(tmp_path, bindir):
    repo = eslint_repo(tmp_path)
    put(repo, "run.sh", "#!/bin/sh\necho hi\n")
    commit_all(repo)
    runner = FakeRunner(default=lambda spec: outcome(1, "bad") if spec.argv[0].endswith("/eslint") else outcome(0))
    report, _ = go(repo, bindir, runner, only=("lint", "format"), no_setup=True)
    by_id = rows(report)
    assert by_id["eslint@."]["state"] == "failed" and by_id["prettier@."]["state"] == "passed"
    assert by_id["shellcheck@."]["state"] == "missing-tool"
    assert report.exit_code == 127
    assert_valid_doc(report.doc)
    assert by_id["shellcheck@."]["detail"]


def test_text_report_lists_every_state_and_summary(tmp_path, bindir):
    repo = eslint_repo(tmp_path)
    report, _ = go(repo, bindir, only=("lint",), no_setup=True, json=False)
    assert "eslint@." in report.text and "passed" in report.text and "summary:" in report.text
    assert f"exit {report.exit_code}" in report.text


def test_unsupported_language_only_repo_is_127_and_never_claims_success(tmp_path, bindir):
    repo = make_repo(tmp_path)
    put(repo, "src/Main.kt", "fun main() {}\n")
    put(repo, "README.md", "# hi\n")
    commit_all(repo)
    report, runner = go(repo, bindir, no_setup=True)
    doc = report.doc
    assert report.exit_code == 127 and doc["ok"] is False
    assert doc["complete"] is False and doc["summary"]["complete"] is False
    assert (doc["summary"]["files_checked"], doc["summary"]["files_uncovered"]) == (0, 1)
    unknown = doc["discovery"]["unknown_languages"]
    assert [u["language"] for u in unknown] == ["kotlin"] and unknown[0]["files"] == 1
    assert doc["coverage"]["uncovered"][0]["why"] == "no-provider"
    assert "kotlin" in report.text and "unsupported-language" in report.text
    lowered = report.text.lower()
    assert "clean" not in lowered and "all " not in lowered.split("result:")[1]
    assert_valid_doc(doc)


def test_filtering_everything_out_keeps_exit_zero_but_is_never_complete(tmp_path, bindir):
    repo = make_repo(tmp_path)
    put(repo, "src/Main.kt", "fun main() {}\n")
    commit_all(repo)
    report, _ = go(repo, bindir, only=("scripts",), no_setup=True)
    assert report.exit_code == 0 and report.doc["complete"] is False
    assert "NOT COMPLETE" in report.text


def test_unsupported_file_next_to_a_passing_language_is_still_127(tmp_path, bindir):
    """A recognised source language with no checker (Kotlin) must not hide behind a passing TS check."""
    repo = eslint_repo(tmp_path)
    put(repo, "src/a.js", "export const a = 1;\n")
    put(repo, "app/Main.kt", "fun main() {}\n")
    commit_all(repo)
    report, _ = go(repo, bindir, no_setup=True)  # nothing narrowed: every verifying category stays on
    doc = report.doc
    assert doc["summary"]["passed"] >= 1 and doc["complete"] is False
    assert report.exit_code == 127 and doc["ok"] is False
    assert "kotlin" in report.text and "unsupported-language" in report.text


@pytest.mark.parametrize("over", [{"only": ("format",)}, {"skip": ("lint",)}, {"scopes": ("src",)}])
def test_explicit_narrowing_keeps_the_checks_own_exit_code_but_stays_incomplete(tmp_path, bindir, over):
    repo = eslint_repo(tmp_path)
    put(repo, "src/a.js", "export const a = 1;\n")
    put(repo, "app/Main.kt", "fun main() {}\n")
    commit_all(repo)
    report, _ = go(repo, bindir, no_setup=True, **over)
    assert report.doc["complete"] is False
    assert report.exit_code == 0 and "NOT COMPLETE" in report.text


def test_a_category_disabled_in_rig_yaml_counts_as_explicit_narrowing(tmp_path, bindir):
    repo = eslint_repo(tmp_path)
    put(repo, "rig.yaml", "version: 1\nlint:\n  categories: {typecheck: false}\n")
    put(repo, "app/Main.kt", "fun main() {}\n")
    commit_all(repo)
    report, _ = go(repo, bindir, no_setup=True, skip=("limits",))
    assert report.doc["complete"] is False and report.exit_code == 0


def test_repository_without_source_files_is_ok_and_complete(tmp_path, bindir):
    repo = make_repo(tmp_path)
    put(repo, "README.md", "# hi\n")
    commit_all(repo)
    report, _ = go(repo, bindir, skip=("limits",), no_setup=True)
    assert report.exit_code == 0 and report.doc["complete"] is True
    assert report.doc["summary"]["applicable"] == 0 and "no applicable checks" in report.text


def test_lone_python_script_without_any_manifest_stays_visible(tmp_path, bindir):
    repo = make_repo(tmp_path)
    put(repo, "tool.py", "import os\n")
    commit_all(repo)
    report, _ = go(repo, bindir, skip=("limits",), no_setup=True)
    assert report.exit_code == 127 and report.doc["ok"] is False and report.doc["complete"] is False
    ruff = [r for r in report.doc["checks"] if r["provider"].startswith("ruff") and r["state"] != "not-applicable"]
    assert ruff and all(r["state"] == "missing-tool" for r in ruff), "the lone script is lintable once Ruff is set up"
    assert report.doc["coverage"]["files_uncovered"] == 1
    assert not any(r["state"] == "passed" for r in report.doc["checks"])


def test_missing_tool_text_never_says_passed_or_clean(tmp_path, bindir):
    repo = make_repo(tmp_path)
    put(repo, "package.json", json.dumps({"devDependencies": {"eslint": "9.0.0"}}))
    put(repo, "eslint.config.js", "export default [];\n")
    commit_all(repo)
    report, runner = go(repo, bindir, no_setup=True, skip=("limits",), json=False)
    assert report.exit_code == 127 and runner.specs == []
    lowered = report.text.lower()
    assert "passed" not in lowered and "clean" not in lowered
    assert "not verified" in lowered and report.doc["ok"] is False


def test_blocked_setup_is_reported_in_text_and_is_not_a_pass(tmp_path, bindir):
    repo = make_repo(tmp_path)
    put(repo, "package.json", "{}\n")
    put(repo, "package-lock.json", "{}\n")
    put(repo, "pnpm-lock.yaml", "lockfileVersion: 9\n")
    commit_all(repo)
    report, _ = go(repo, bindir, skip=("limits",))
    assert report.doc["mode"]["setup"] == "blocked" and "bootstrap blocked" in report.text
    assert report.exit_code == 127
    assert "passed" not in report.text.lower() and "clean" not in report.text.lower()


def test_a_check_that_writes_files_is_wrote_files_and_exit_3_end_to_end(tmp_path, bindir):
    repo = eslint_repo(tmp_path)
    put(repo, "a.js", "let a = 1\n")
    commit_all(repo)

    def writes(spec):
        (repo / "a.js").write_text("let a = 1;\n", encoding="utf-8")
        return outcome(0)
    runner = FakeRunner(default=lambda spec: writes(spec) if spec.argv[0].endswith("/prettier") else outcome(0))
    report, _ = go(repo, bindir, runner, only=("format",), no_setup=True)
    row = rows(report)["prettier@."]
    assert (row["state"], row["reason"]) == ("failed", "wrote-files") and "a.js" in row["detail"]
    assert report.exit_code == 3


def test_monorepo_ancestor_config_is_one_check_at_the_ancestor(tmp_path, bindir):
    repo = make_repo(tmp_path)
    put(repo, "package.json", json.dumps({"devDependencies": {"eslint": "9.0.0"}}))
    put(repo, "eslint.config.js", "export default [];\n")
    put(repo, "web/package.json", "{}\n")
    put(repo, "api/package.json", "{}\n")
    put(repo, ".gitignore", "node_modules\n")
    commit_all(repo)
    local_tool(repo, "eslint")
    report, _ = go(repo, bindir, only=("lint",), no_setup=True)
    eslint = [r for r in report.doc["checks"] if r["provider"] == "eslint" and r["state"] != "not-applicable"]
    assert [(r["id"], r["cwd"]) for r in eslint] == [("eslint@.", ".")]


def test_unsupported_configured_tool_is_listed_and_never_replaced(tmp_path, bindir):
    repo = make_repo(tmp_path)
    put(repo, "package.json", json.dumps({"devDependencies": {"xo": "1.0.0"}}))
    commit_all(repo)
    report, _ = go(repo, bindir, no_setup=True, skip=("limits",))
    assert {"scope": ".", "tool": "xo"} in report.doc["discovery"]["unsupported_tools"]
    assert "xo" in report.text
    assert not any(step["argv"] for step in report.doc["bootstrap"]["steps"]), "a configured tool role is never bootstrapped"


# ── read-only modes ──────────────────────────────────────────────────────────────

def _npm_shim(bindir: Path, log: Path) -> None:
    write_exe(bindir / "npm", f"""import json, os, sys
open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + "\\n")
if '--prefix' in sys.argv:  # tool-cache install (the hosted anti-slop policy): nothing touches the repo
    prefix = sys.argv[sys.argv.index('--prefix') + 1]
    os.makedirs(prefix + '/node_modules/@oxlint/plugins', exist_ok=True)
    open(prefix + '/node_modules/@oxlint/plugins/package.json', 'w').write('{{}}')
    sys.exit(0)
pkg = json.load(open('package.json'))
dev = pkg.setdefault('devDependencies', {{}})
for arg in sys.argv[1:]:
    if '@' in arg[1:] and not arg.startswith('-'):
        name, _, version = arg.rpartition('@')
        dev[name] = version
        os.makedirs('node_modules/.bin', exist_ok=True)
        tool = 'node_modules/.bin/' + name.split('/')[-1]
        open(tool, 'w').write('#!/bin/sh\\nexit 0\\n')
        os.chmod(tool, 0o755)
json.dump(pkg, open('package.json', 'w'))
open('package-lock.json', 'w').write('{{"lockfileVersion": 3}}')
""")


def bare_js_repo(tmp_path) -> Path:
    repo = make_repo(tmp_path)
    put(repo, "package.json", "{}\n")
    put(repo, ".gitignore", "node_modules\n")
    commit_all(repo)
    return repo


@pytest.mark.parametrize("label,over,environ", [
    ("skipped-plan", {"plan": True}, {}),
    ("skipped-no-setup", {"no_setup": True}, {}),
    ("skipped-ci", {"ci": True}, {}),
    ("skipped-ci", {}, {"CI": "1"}),
    ("skipped-ci", {}, {"GITHUB_ACTIONS": "true"}),
    # the report and the bootstrap refusal share one predicate: explicit true words only
    ("skipped-ci", {}, {"CI": "yes"}),
    ("skipped-ci", {}, {"CI": "on"}),
])
def test_read_only_modes_write_nothing_run_no_bootstrap_but_print_the_plan(tmp_path, bindir, label, over, environ):
    repo = bare_js_repo(tmp_path)
    log = tmp_path / "npm.log"
    _npm_shim(bindir, log)
    before = tree_digest(repo)
    report, runner = go(repo, bindir, environ=environ, skip=("limits",), **over)
    assert tree_digest(repo) == before and not log.exists()
    assert report.doc["mode"]["setup"] == label
    assert report.doc["bootstrap"]["steps"], "the would-be install is still shown"
    results = report.doc["bootstrap"]["results"]
    if label == "skipped-ci":
        # repo-editing steps are refused under CI; none may be applied
        assert results and all(r["state"] == "blocked" for r in results)
    else:
        assert results == []
    if over.get("plan"):
        assert runner.specs == [] and report.doc["ok"] is None and report.exit_code == 0
        assert report.doc["mode"]["plan"] is True
    else:
        assert report.exit_code == 127, "missing tools are reported, never passed"
        assert all(r["state"] != "passed" for r in report.doc["checks"])
    assert_valid_doc(report.doc)


def test_plan_shows_commands_for_runnable_checks_without_running_them(tmp_path, bindir):
    repo = eslint_repo(tmp_path)
    before = tree_digest(repo)
    report, runner = go(repo, bindir, plan=True)
    planned = [r for r in report.doc["checks"] if r["state"] == "planned"]
    assert planned and all(r["argv"] and r["exit_code"] is None and "would run" in r["detail"] for r in planned)
    assert runner.specs == [] and tree_digest(repo) == before


# ── local bootstrap ──────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _hermetic_cache(tmp_path, monkeypatch):
    """The hosted anti-slop policy lands in the tool cache and needs a catalog: never the real HOME or checkout."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RIG_LINT_TOOL_CACHE", str(tmp_path / "toolcache"))
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(tmp_path / "no-agent-tools"))


def _agent_tools_checkout(tmp_path, monkeypatch):
    """A hermetic agent-tools checkout (found the way `rig apply` finds it) carrying the anti-slop bundle."""
    source = tmp_path / "agent-tools"
    for rel in ("skills", "agent-hooks", "vendor/anti-slop/skills/install-anti-slop/assets/anti-slop"):
        (source / rel).mkdir(parents=True)
    (source / "vendor/anti-slop/skills/install-anti-slop/assets/anti-slop/index.ts").write_text("export default {};\n")
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(source))


def test_local_run_bootstraps_then_rediscovers_and_runs_the_new_tool(tmp_path, bindir, monkeypatch):
    _agent_tools_checkout(tmp_path, monkeypatch)
    repo = bare_js_repo(tmp_path)
    log = tmp_path / "npm.log"
    _npm_shim(bindir, log)
    report, runner = go(repo, bindir, skip=("limits",))
    assert report.doc["mode"]["setup"] == "applied", report.doc["bootstrap"]
    assert [r["state"] for r in report.doc["bootstrap"]["results"]] == ["applied"] * len(report.doc["bootstrap"]["results"])
    manifest = json.loads((repo / "package.json").read_text())
    assert "oxlint" in manifest["devDependencies"] and "oxfmt" in manifest["devDependencies"]
    assert any(argv[0].endswith("node_modules/.bin/oxlint") for argv in runner.argvs)
    assert report.exit_code == 0 and rows(report)["oxlint@."]["state"] == "passed"
    calls = log.read_text().splitlines()
    again, runner2 = go(repo, bindir, skip=("limits",))
    assert again.doc["mode"]["setup"] == "nothing-to-do"
    assert log.read_text().splitlines() == calls, "second run spawned no package manager"


def test_unavailable_policy_catalog_is_an_explicit_failed_setup_never_green(tmp_path, bindir, monkeypatch):
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(tmp_path / "nowhere"))
    repo = bare_js_repo(tmp_path)
    _npm_shim(bindir, tmp_path / "npm.log")
    report, _ = go(repo, bindir, skip=("limits",))
    assert report.doc["mode"]["setup"] == "failed" and report.exit_code != 0
    reasons = {b["reason"] for b in report.doc["bootstrap"]["blocked"]}
    assert reasons == {"required-policy-unavailable"}
    assert "required-policy-unavailable" in report.text


def test_only_limits_installs_and_configures_nothing(tmp_path, bindir, monkeypatch):
    _agent_tools_checkout(tmp_path, monkeypatch)
    repo = bare_js_repo(tmp_path)
    log = tmp_path / "npm.log"
    _npm_shim(bindir, log)
    before = sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*") if ".git" not in p.parts)
    report, _ = go(repo, bindir, only=("limits",))
    assert not report.doc["bootstrap"]["steps"] and not report.doc["bootstrap"]["blocked"]
    assert not log.exists(), "no package manager was spawned for a limits-only run"
    assert sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*") if ".git" not in p.parts) == before


def test_skip_format_installs_no_formatter(tmp_path, bindir, monkeypatch):
    _agent_tools_checkout(tmp_path, monkeypatch)
    repo = bare_js_repo(tmp_path)
    _npm_shim(bindir, tmp_path / "npm.log")
    report, _ = go(repo, bindir, skip=("limits", "format"))
    manifest = json.loads((repo / "package.json").read_text())
    assert "oxlint" in manifest["devDependencies"] and "oxfmt" not in manifest["devDependencies"]


def test_ci_with_an_unavailable_policy_keeps_skipped_ci_but_is_never_a_green_exit(tmp_path, bindir):
    """CI never sets up, so its label is not `failed`; the unapplied required policy still decides the exit."""
    repo = make_repo(tmp_path)
    put(repo, "package.json", json.dumps({"devDependencies": {"oxlint": "1.90.0", "oxfmt": "0.75.0"}}) + "\n")
    put(repo, "package-lock.json", "{}\n")
    put(repo, "src/a.ts", "export const a = 1;\n")
    commit_all(repo)
    local_tool(repo, "oxlint")
    local_tool(repo, "oxfmt")
    report, _ = go(repo, bindir, skip=("limits",), ci=True)
    assert {b["reason"] for b in report.doc["bootstrap"]["blocked"]} == {"required-policy-unavailable"}
    assert report.doc["mode"]["setup"] != "failed" or report.exit_code == 3
    assert report.exit_code == 3 and "required-policy-unavailable" in report.text


def test_failed_bootstrap_is_exit_3_and_checks_still_run(tmp_path, bindir):
    repo = bare_js_repo(tmp_path)
    write_exe(bindir / "npm", "import sys; sys.exit(9)\n")
    report, _ = go(repo, bindir, skip=("limits",))
    assert report.doc["mode"]["setup"] == "failed"
    assert report.exit_code in (3, 127)  # the missing tool outranks the failed install: 127 > 3
    assert any(r["state"] == "missing-tool" for r in report.doc["checks"])


def test_configured_linters_are_never_bootstrapped(tmp_path, bindir):
    repo = eslint_repo(tmp_path)
    log = tmp_path / "npm.log"
    _npm_shim(bindir, log)
    report, _ = go(repo, bindir, skip=("limits",))
    assert report.doc["mode"]["setup"] in ("nothing-to-do", "blocked") and not log.exists()
    assert not any(i.startswith("oxlint@") and r["state"] == "passed" for i, r in rows(report).items())


# ── recursion guard ──────────────────────────────────────────────────────────────

def script_repo(tmp_path) -> Path:
    repo = make_repo(tmp_path)
    put(repo, "package.json", json.dumps({"scripts": {"lint": "node scripts/lint.js"}}))
    commit_all(repo)
    return repo


def test_scripts_run_at_depth_zero_with_incremented_depth_for_the_child(tmp_path, bindir):
    repo = script_repo(tmp_path)
    write_exe(bindir / "npm", "import sys; sys.exit(0)\n")
    report, runner = go(repo, bindir, only=("scripts",), no_setup=True)
    script = next(r for r in report.doc["checks"] if r["provider"].startswith("script"))
    assert script["state"] == "passed" and script["argv"][1:] == ["run", "lint"]
    spec = next(s for s in runner.specs if s.argv[1:] == ("run", "lint"))
    assert spec.env["RIG_LINT_DEPTH"] == "1"


@pytest.mark.parametrize("depth", ["1", "2", "junk"])
def test_scripts_are_skipped_when_nested(tmp_path, bindir, depth):
    repo = script_repo(tmp_path)
    write_exe(bindir / "npm", "import sys; sys.exit(0)\n")
    report, runner = go(repo, bindir, environ={"RIG_LINT_DEPTH": depth}, only=("scripts",), no_setup=True)
    script = next(r for r in report.doc["checks"] if r["provider"].startswith("script"))
    assert (script["state"], script["reason"]) == ("not-applicable", "recursion-guard")
    assert runner.specs == []


def test_recursion_end_to_end_a_script_that_calls_rig_lint_terminates(tmp_path, bindir, monkeypatch):
    repo = script_repo(tmp_path)
    log = tmp_path / "nested.log"
    root = str(Path(lr.__file__).resolve().parent.parent)
    write_exe(bindir / "npm", f"""import json, os, subprocess, sys
depth = os.environ.get('RIG_LINT_DEPTH')
proc = subprocess.run([{sys.executable!r}, '-m', 'riglib', 'lint', 'run', '--json', '--no-setup', '--skip', 'limits',
                       '-C', os.getcwd()], capture_output=True, text=True, env=os.environ)
doc = json.loads(proc.stdout)
scripts = [(c['id'], c['state'], c['reason']) for c in doc['checks'] if c['provider'].startswith('script')]
open({str(log)!r}, 'a').write(json.dumps({{'depth': depth, 'nested_scripts': scripts}}) + "\\n")
""")
    # the nested `rig lint run` needs git; the shim dir stays first so `npm` is still the shim
    environ = {"PYTHONPATH": root, "HOME": str(tmp_path / "home"), "PATH": f"{bindir}{os.pathsep}{os.defpath}", **LOADER_ENV}
    report, _ = go(repo, bindir, runner=lr.subprocess_runner, environ=environ, no_setup=True, skip=("limits",))
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(entries) == 1, "the nested run must not invoke the script again"
    assert entries[0]["depth"] == "1"
    assert entries[0]["nested_scripts"] and all(s[1:] == ["not-applicable", "recursion-guard"] for s in entries[0]["nested_scripts"])
    script = next(r for r in report.doc["checks"] if r["provider"].startswith("script"))
    assert script["state"] == "passed"


# ── config, filters, scope ───────────────────────────────────────────────────────

def test_lint_disabled_by_config_is_one_not_applicable_row(tmp_path, bindir):
    repo = eslint_repo(tmp_path)
    put(repo, "rig.yaml", "version: 1\nlint:\n  enabled: false\n")
    report, runner = go(repo, bindir)
    assert [(r["state"], r["reason"]) for r in report.doc["checks"]] == [("not-applicable", "disabled-by-config")]
    assert report.exit_code == 0 and runner.specs == [] and report.doc["mode"]["setup"] == "disabled-by-config"
    assert_valid_doc(report.doc)


def test_only_and_skip_filters_mark_checks_not_applicable_filtered(tmp_path, bindir):
    repo = eslint_repo(tmp_path)
    report, runner = go(repo, bindir, only=("format",), no_setup=True)
    by_id = rows(report)
    assert (by_id["eslint@."]["state"], by_id["eslint@."]["reason"]) == ("not-applicable", "filtered")
    assert by_id["prettier@."]["state"] == "passed"
    skip_report, _ = go(repo, bindir, skip=("format", "limits"), no_setup=True)
    assert rows(skip_report)["prettier@."]["reason"] == "filtered" and rows(skip_report)["eslint@."]["state"] == "passed"


def test_scope_filter_limits_to_a_package_directory(tmp_path, bindir):
    repo = make_repo(tmp_path)
    put(repo, "web/package.json", json.dumps({"devDependencies": {"eslint": "9.0.0"}}))
    put(repo, "web/eslint.config.js", "export default [];\n")
    put(repo, "api/package.json", json.dumps({"devDependencies": {"eslint": "9.0.0"}}))
    put(repo, "api/eslint.config.js", "export default [];\n")
    commit_all(repo)
    for pkg in ("web", "api"):
        local_tool(repo / pkg, "eslint")
    report, _ = go(repo, bindir, scopes=("web",), only=("lint",), no_setup=True)
    by_id = rows(report)
    assert by_id["eslint@web"]["state"] == "passed"
    assert (by_id["eslint@api"]["state"], by_id["eslint@api"]["reason"]) == ("not-applicable", "filtered")


def test_cli_timeout_overrides_every_check(tmp_path, bindir):
    repo = eslint_repo(tmp_path)
    report, runner = go(repo, bindir, timeout=7, no_setup=True, skip=("limits",))
    assert runner.specs and {s.timeout_s for s in runner.specs} == {7}
    assert {r["timeout_s"] for r in report.doc["checks"] if r["argv"]} == {7}


def test_paths_with_spaces_in_repo_and_scope(tmp_path, bindir):
    repo = make_repo(tmp_path, "my repo")
    put(repo, "pk g/package.json", json.dumps({"devDependencies": {"eslint": "9.0.0"}}))
    put(repo, "pk g/eslint.config.js", "export default [];\n")
    commit_all(repo)
    local_tool(repo / "pk g", "eslint")
    report, runner = go(repo, bindir, only=("lint",), no_setup=True, scopes=("pk g",))
    row = rows(report)["eslint@pk g"]
    assert row["state"] == "passed" and row["cwd"] == "pk g"
    assert any(s.cwd == Path(os.path.realpath(repo)) / "pk g" for s in runner.specs)


def test_tool_resolving_into_an_external_store_is_run_not_reported_as_an_escape(tmp_path, bindir):
    """pnpm/uv stores live outside the repo; running a managed executable there writes nothing outside."""
    repo = make_repo(tmp_path)
    put(repo, "package.json", json.dumps({"devDependencies": {"eslint": "9.0.0"}}))
    put(repo, "eslint.config.js", "export default [];\n")
    put(repo, ".gitignore", "node_modules\n")
    outside = write_exe(tmp_path / "outside" / "eslint", "import sys; sys.exit(0)\n")
    (repo / "node_modules" / ".bin").mkdir(parents=True)
    (repo / "node_modules" / ".bin" / "eslint").symlink_to(outside)
    commit_all(repo)
    report, runner = go(repo, bindir, only=("lint",), no_setup=True)
    row = rows(report)["eslint@."]
    assert (row["state"], row["reason"]) == ("passed", "ok")
    assert len(runner.specs) == 1


# ── discovery problems ───────────────────────────────────────────────────────────

def test_malformed_manifest_is_a_failed_check_and_exit_3(tmp_path, bindir):
    repo = make_repo(tmp_path)
    put(repo, "web/package.json", "{ not json")
    commit_all(repo)
    report, _ = go(repo, bindir, only=("scripts",), no_setup=True)
    row = rows(report)["discovery@web/package.json"]
    assert (row["state"], row["reason"]) == ("failed", "malformed-manifest")
    assert report.exit_code == 3
    assert report.doc["discovery"]["problems"][0]["path"] == "web/package.json"


def test_discovery_problems_are_not_hidden_by_category_filters(tmp_path, bindir):
    repo = make_repo(tmp_path)
    put(repo, "package.json", "{ not json")
    commit_all(repo)
    report, _ = go(repo, bindir, only=("format",), no_setup=True)
    row = rows(report)["discovery@package.json"]
    assert (row["state"], row["reason"]) == ("failed", "malformed-manifest")
    # the default Oxfmt check is missing-tool here and 127 outranks 3; either way the run is not a pass
    assert report.exit_code in (3, 127)


# ── usage errors (exit 2) ────────────────────────────────────────────────────────

def test_not_a_git_repo_is_exit_2_with_error_doc(tmp_path, bindir):
    plain = tmp_path / "plain"
    plain.mkdir()
    report, runner = go(plain, bindir)
    assert report.exit_code == 2 and runner.specs == []
    assert report.doc == {"schema": "rig-lint-run/v1", "ok": False, "exit_code": 2, "error": report.doc["error"],
                          "fix": {"requested": False}}
    assert "git" in report.doc["error"] and report.text.startswith("error:")


@pytest.mark.parametrize("over,needle", [
    ({"only": ("bogus",)}, "--only"), ({"skip": ("lint", "nope")}, "--skip"),
    ({"timeout": 0}, "--timeout"), ({"timeout": 3601}, "--timeout"),
    ({"fix": True, "plan": True}, "--fix"), ({"fix": True, "ci": True}, "--fix"),
    ({"scopes": ("../outside",)}, "outside"),
])
def test_invalid_options_exit_2(tmp_path, bindir, over, needle):
    repo = eslint_repo(tmp_path)
    report, runner = go(repo, bindir, **over)
    assert report.exit_code == 2 and needle in report.doc["error"] and runner.specs == []
    assert report.doc["fix"] == {"requested": bool(over.get("fix"))}


def test_fix_with_ci_environment_is_exit_2(tmp_path, bindir):
    report, _ = go(eslint_repo(tmp_path), bindir, environ={"CI": "true"}, fix=True)
    assert report.exit_code == 2


def test_config_error_inside_lint_block_is_exit_2(tmp_path, bindir):
    repo = eslint_repo(tmp_path)
    put(repo, "rig.yaml", "version: 1\nlint:\n  timeout_seconds: 0\n")
    report, _ = go(repo, bindir)
    assert report.exit_code == 2 and "lint.timeout_seconds" in report.doc["error"]


def test_fix_refuses_partially_staged_files_and_writes_nothing(tmp_path, bindir):
    repo = eslint_repo(tmp_path)
    put(repo, "a.js", "let a = 1\n")
    commit_all(repo)
    put(repo, "a.js", "let a = 2\n")
    git(repo, "add", "a.js")
    put(repo, "a.js", "let a = 3\n")
    before = tree_digest(repo)
    report, runner = go(repo, bindir, fix=True, no_setup=True)
    assert report.exit_code == 2 and "a.js" in report.doc["error"] and runner.specs == []
    assert tree_digest(repo) == before


# ── --fix end to end ─────────────────────────────────────────────────────────────

def test_fix_uses_only_safe_fixers_then_rechecks(tmp_path, bindir):
    repo = eslint_repo(tmp_path)
    report, runner = go(repo, bindir, fix=True, no_setup=True, only=("lint", "format"))
    argvs = [a[1:] for a in runner.argvs]
    assert ("--fix", ".") in argvs and ("--write", ".") in argvs
    assert not any(tok.startswith("--unsafe") or tok in ("--fix-dangerously", "--fix-suggestions")
                   for argv in runner.argvs for tok in argv)
    row = rows(report)["eslint@."]
    assert row["fix"]["mode"] == "safe" and row["state"] == "passed"
    assert report.doc["mode"]["fix"] is True


# ── crash containment ────────────────────────────────────────────────────────────

def test_timeouts_become_exit_3_through_execute(tmp_path, bindir):
    repo = eslint_repo(tmp_path)
    runner = FakeRunner(default=lambda spec: outcome(None, timed_out=True) if spec.argv[0].endswith("/eslint")
                        else outcome(0))
    report, _ = go(repo, bindir, runner, only=("lint",), no_setup=True)
    row = rows(report)["eslint@."]
    assert (row["state"], row["reason"]) == ("failed", "timeout") and report.exit_code == 3


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch):
    """`execute` asks `which('node')` for its version; the fake which never finds one.

    The bootstrap action handler reads the real process environment for CI (defense in depth), and the
    real CI sets it, so the ambient markers are cleared: each test states CI explicitly instead.
    """
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    for name in ("CI", "GITHUB_ACTIONS", "RIG_LINT_DEPTH"):
        monkeypatch.delenv(name, raising=False)
