"""A tool that could not RUN is not a tool that found problems.

A Biome configuration failure once exited 1 and was reported as `findings` with 531 files "checked".
These tests feed non-zero outcomes (as shim tools would emit them) through the classifier and the
whole run: configuration errors, dependency-readiness failures and crashes get their own reason,
never count as checked files, and plain findings stay findings.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from riglib import lint_run as lr
from tests.lint_check_helpers import commit_all, make_repo, put
from tests.lint_run_helpers import LOADER_ENV, FakeRunner, ensure_repo, outcome, planned, which_in, write_exe

ENV = {"PATH": os.defpath, **LOADER_ENV}


def classify(tmp_path, provider, code, stdout="", stderr="", *, cwd=".", node_modules=False):
    root = ensure_repo(tmp_path)
    if node_modules:
        (root / "node_modules").mkdir()
    check = planned(id=f"{provider}@.", provider=provider, argv=("t",), cwd=cwd)
    (result,) = lr.run_checks([check], root=root, default_timeout_s=30, fix=False, environ=ENV,
                              runner=FakeRunner(default=outcome(code, stdout, stderr)))
    return result


BIOME_CONFIG_ERROR = ("biome.json:3:5 configuration ━━━━━━━━━━━━\n\n  × Found an unknown key `linter2`.\n")
BIOME_FINDINGS = "src/a.ts:1:1 lint/suspicious/noDebugger ━━━━━━━━\n\n  × This is an unexpected use of the debugger statement.\n"


@pytest.mark.parametrize("provider,code,stderr", [
    ("eslint", 2, "Oops! Something went wrong! :(\nESLint: 9.0.0\nNo config found"),
    ("prettier", 2, "[error] Invalid configuration for file x"),
    ("ruff-check", 2, "ruff failed\n  Cause: Failed to parse pyproject.toml"),
    ("ruff-format", 2, "error: unexpected argument"),
    ("mypy", 2, "mypy.ini: [mypy]: Unrecognized option"),
    ("pyright", 3, "Config file not found"),
    ("shellcheck", 3, "shellcheck: invalid option"),
    ("biome-lint", 1, BIOME_CONFIG_ERROR),
    ("biome-format", 1, BIOME_CONFIG_ERROR),
    ("oxlint", 1, "Failed to parse configuration file ./.oxlintrc.json: unknown field `plugins2`"),
    ("oxfmt", 1, "Failed to parse config .oxfmtrc.json"),
    ("tsc", 2, "error TS5083: Cannot read file '/repo/tsconfig.json'."),
])
def test_tool_configuration_failures_are_config_error_not_findings(tmp_path, provider, code, stderr):
    result = classify(tmp_path, provider, code, stderr=stderr)
    assert (result.state, result.reason) == ("failed", "config-error")
    assert "not a lint finding" in result.detail or "configuration" in result.detail
    # a linter rejecting the PROJECT's configuration is incomplete analysis (3), not Rig's own usage error (2)
    assert lr.exit_code_for([result], bootstrap_failed=False, problems=()) == lr.EXIT_INCOMPLETE


@pytest.mark.parametrize("provider,code,stdout,stderr", [
    ("eslint", 1, "src/a.js\n  1:1  error  no-undef", ""),
    ("ruff-check", 1, "a.py:1:1: F401 `os` imported but unused", ""),
    ("biome-lint", 1, "", BIOME_FINDINGS),
    ("oxlint", 1, "x no-debugger: `debugger` statement is not allowed", ""),
    ("tsc", 2, "src/a.ts(1,7): error TS2322: Type 'string' is not assignable to type 'number'.", ""),
    ("prettier", 1, "[warn] src/a.ts\n[warn] Code style issues found", ""),
    ("shellcheck", 1, "In a.sh line 2: SC2086 Double quote to prevent globbing", ""),
    ("unknown-tool", 7, "something", "whatever"),
])
def test_ordinary_findings_stay_findings(tmp_path, provider, code, stdout, stderr):
    result = classify(tmp_path, provider, code, stdout=stdout, stderr=stderr)
    assert (result.state, result.reason) == ("failed", "findings")


def test_unresolved_modules_without_node_modules_are_dependencies_not_installed(tmp_path):
    out = "src/a.ts(1,20): error TS2307: Cannot find module 'react' or its corresponding type declarations."
    result = classify(tmp_path, "tsc", 2, stdout=out)
    assert (result.state, result.reason) == ("missing-tool", "dependencies-not-installed")
    assert "npm ci" in result.detail and "never installs application dependencies" in result.detail
    assert lr.exit_code_for([result], bootstrap_failed=False, problems=()) == lr.EXIT_MISSING


def test_unresolved_modules_with_installed_dependencies_are_real_findings(tmp_path):
    out = "src/a.ts(1,20): error TS2307: Cannot find module './typo' or its corresponding type declarations."
    result = classify(tmp_path, "tsc", 2, stdout=out, node_modules=True)
    assert (result.state, result.reason) == ("failed", "findings")


def test_node_modules_in_an_ancestor_counts_as_installed(tmp_path):
    root = ensure_repo(tmp_path)
    (root / "node_modules").mkdir()
    (root / "web").mkdir()
    check = planned(provider="tsc", argv=("t",), cwd="web")
    out = "error TS2307: Cannot find module 'x'."
    (result,) = lr.run_checks([check], root=root, default_timeout_s=30, fix=False, environ=ENV,
                              runner=FakeRunner(default=outcome(2, out)))
    assert result.reason == "findings"


@pytest.mark.parametrize("provider,code,stderr", [
    ("eslint", 1, "TypeError: x is not a function\n    at Object.<anonymous> (/repo/node_modules/eslint/lib/cli.js:10:5)"),
    ("mypy", 1, "Traceback (most recent call last):\n  File \"x\", line 1\nRuntimeError: boom"),
    ("black", 123, "error: cannot format a.py: internal error"),
    ("cargo-clippy", 101, "thread 'main' panicked at src/main.rs:1:1"),
])
def test_tool_crashes_are_crash_not_findings(tmp_path, provider, code, stderr):
    result = classify(tmp_path, provider, code, stderr=stderr)
    assert (result.state, result.reason) == ("failed", "crash")
    assert lr.exit_code_for([result], bootstrap_failed=False, problems=()) == lr.EXIT_INCOMPLETE


def test_a_zero_exit_is_never_reclassified(tmp_path):
    result = classify(tmp_path, "biome-lint", 0, stderr=BIOME_CONFIG_ERROR)
    assert (result.state, result.reason) == ("passed", "ok")


# ── end to end with shim tools ───────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    for name in ("CI", "GITHUB_ACTIONS", "RIG_LINT_DEPTH"):
        monkeypatch.delenv(name, raising=False)


def biome_repo(tmp_path: Path, shim_body: str) -> Path:
    repo = make_repo(tmp_path)
    put(repo, "package.json", json.dumps({"devDependencies": {"@biomejs/biome": "1.9.0"}}))
    put(repo, "biome.json", '{"linter2": {}}\n')
    put(repo, "src/a.ts", "export const a = 1;\n")
    put(repo, ".gitignore", "node_modules\n")
    write_exe(repo / "node_modules" / ".bin" / "biome", shim_body)
    commit_all(repo)
    return repo


def run_repo(repo: Path):
    opts = lr.RunOptions(cwd=str(repo), config=None, json=True, plan=False, no_setup=True, fix=False, ci=None,
                         only=("lint", "format"), skip=(), scopes=(), timeout=60)
    env = {"PATH": os.environ.get("PATH", os.defpath), **LOADER_ENV}
    return lr.execute(opts, environ=env, runner=lr.subprocess_runner, which=which_in())


def test_biome_configuration_failure_is_config_error_and_checks_no_files(tmp_path):
    shim = ("import sys\n"
            "sys.stderr.write('biome.json:1:2 configuration ━━━━━━\\n\\n  × Found an unknown key `linter2`.\\n')\n"
            "sys.exit(1)\n")
    report = run_repo(biome_repo(tmp_path, shim))
    rows = {r["id"]: r for r in report.doc["checks"]}
    assert (rows["biome-lint@."]["state"], rows["biome-lint@."]["reason"]) == ("failed", "config-error")
    assert report.doc["summary"]["files_checked"] == 0 and report.doc["complete"] is False
    assert report.doc["summary"]["files_uncovered"] >= 1
    assert report.exit_code == lr.EXIT_INCOMPLETE and report.doc["ok"] is False
    assert "findings" not in rows["biome-lint@."]["reason"]


def test_biome_real_findings_do_count_the_files_as_checked(tmp_path):
    shim = "import sys\nsys.stderr.write('src/a.ts:1:1 lint/suspicious/noDebugger ━━\\n')\nsys.exit(1)\n"
    report = run_repo(biome_repo(tmp_path, shim))
    rows = {r["id"]: r for r in report.doc["checks"]}
    assert rows["biome-lint@."]["reason"] == "findings"
    assert report.doc["summary"]["files_checked"] >= 1
    assert report.exit_code == 1
