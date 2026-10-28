"""Operator-facing behavior of `rig lint`: findings in text mode, explicit --ci, and fail-closed linters config.

Fake runner only; no tool or network. The CCCC limits row is the one that embeds a full
`rig-lint-check/v1` document, so its text rendering must show findings, never the tail of that JSON.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from riglib import lint_run as lr
from riglib.lint_providers import PINS
from riglib.lint_run_plan import ci_from
from riglib.lint_tool_cache import environment_dir, tool_executable
from tests.lint_check_helpers import commit_all, make_repo, put
from tests.lint_run_helpers import FakeRunner, outcome, tree_digest, which_in, write_exe

FINDINGS_DOC = {
    "schema": "rig-lint-check/v1", "ok": False, "exit_code": 1,
    "findings": [
        {"rule": "file-lines/max-lines", "severity": "warning", "path": "big.py", "line": None,
         "message": "file has 1500 lines (warn at 1200)"},
        {"rule": "complexity/cognitive", "severity": "error", "path": "src/a.py", "line": 42, "function": "f",
         "message": "cognitive complexity 21 > 15"},
    ],
    "summary": {"errors": 1, "warnings": 1},
}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    for name in ("CI", "GITHUB_ACTIONS", "RIG_LINT_DEPTH"):
        monkeypatch.delenv(name, raising=False)


def _repo(tmp_path: Path, **files: str) -> Path:
    repo = make_repo(tmp_path)
    put(repo, "src/a.py", "x = 1\n")
    for rel, text in files.items():
        put(repo, rel.replace("__", "/"), text)
    commit_all(repo)
    return repo


def _execute(repo: Path, tmp_path: Path, runner: FakeRunner | None = None, *, environ=None, **over):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    base = {"cwd": str(repo), "config": None, "json": False, "plan": False, "no_setup": True, "fix": False, "ci": None,
            "only": ("limits",), "skip": (), "scopes": (), "timeout": None}
    return lr.execute(lr.RunOptions(**{**base, **over}), environ={"PATH": str(bindir), **(environ or {})},
                      runner=runner or FakeRunner(default=lambda spec: outcome(0)), which=which_in(bindir))


# ---------------------------------------------------------------- F4: failed limits row shows findings


def test_failed_limits_row_prints_findings_not_the_json_tail(tmp_path):
    repo = _repo(tmp_path)
    raw = json.dumps(FINDINGS_DOC, indent=2)
    runner = FakeRunner(default=lambda spec: outcome(1, raw))
    report = _execute(repo, tmp_path, runner)
    assert report.exit_code == 1
    assert "src/a.py:42 error complexity/cognitive: cognitive complexity 21 > 15" in report.text
    assert "big.py warning file-lines/max-lines" in report.text
    assert report.text.index("src/a.py:42") < report.text.index("big.py"), "errors are listed first"
    for fragment in ('"schema"', '"summary"', "rig-lint-check/v1", '"findings"', "{", "}"):
        assert fragment not in report.text, f"raw JSON leaked into the text report: {fragment!r}"


def test_non_report_failures_keep_the_output_tail(tmp_path):
    repo = _repo(tmp_path)
    runner = FakeRunner(default=lambda spec: outcome(1, "plain stderr-ish line from a tool"))
    report = _execute(repo, tmp_path, runner)
    assert "plain stderr-ish line from a tool" in report.text


# ---------------------------------------------------------------- F3: explicit --ci is never retracted


def _opts(**over):
    base = {"cwd": ".", "config": None, "json": True, "plan": False, "no_setup": False, "fix": False, "ci": None,
            "only": (), "skip": (), "scopes": (), "timeout": None}
    return lr.RunOptions(**{**base, **over})


@pytest.mark.parametrize("env", [{}, {"CI": "0"}, {"CI": "false"}, {"CI": "banana"}, {"GITHUB_ACTIONS": "off"}])
def test_explicit_ci_flag_is_not_overridden_by_the_environment(env):
    assert ci_from(_opts(ci=True), env) is True


@pytest.mark.parametrize(("env", "expected"), [
    ({}, False), ({"CI": "1"}, True), ({"CI": "true"}, True), ({"GITHUB_ACTIONS": "true"}, True),
    ({"CI": "banana"}, False), ({"CI": "2"}, False), ({"CI": "y"}, False), ({"CI": "0"}, False),
])
def test_environment_only_adds_ci_through_the_single_predicate(env, expected):
    assert ci_from(_opts(ci=None), env) is expected


def test_ci_flag_run_reports_ci_even_with_a_false_environment(tmp_path):
    repo = _repo(tmp_path)
    cache = tmp_path / "tool-cache"
    environ = {"CI": "0", "RIG_LINT_TOOL_CACHE": str(cache)}
    # The pinned Ruff is already in the (private) tool cache, so a CI run has nothing to install and never reaches the network.
    write_exe(tool_executable(environment_dir(environ, "pypi", "ruff", PINS["ruff"].version), "pypi", "ruff"), "")
    before = tree_digest(repo)
    report = _execute(repo, tmp_path, environ=environ, ci=True, json=True, no_setup=False)
    assert report.doc["mode"]["ci"] is True and report.doc["mode"]["setup"] == "nothing-to-do"
    assert tree_digest(repo) == before


def test_biome_repo_acquires_no_oxlint_and_no_anti_slop_requirement_without_a_catalog(tmp_path, monkeypatch):
    monkeypatch.delenv("RIG_AGENT_TOOLS_SOURCE", raising=False)  # no catalog anywhere: a required policy would block
    repo = make_repo(tmp_path)
    put(repo, "package.json", '{"name": "app", "devDependencies": {"@biomejs/biome": "1.9.4"}}\n')
    put(repo, "biome.json", "{}\n")
    put(repo, "src/index.ts", "export const a = 1;\n")
    commit_all(repo)
    before = tree_digest(repo)
    report = _execute(repo, tmp_path, ci=True, json=True, no_setup=False, plan=True, only=("lint", "format"))
    boot = report.doc["bootstrap"]
    # What the host has installed (npm, a lockfile) decides whether the dependency restore is a step or blocked;
    # that is not under test. No Oxlint/Oxfmt/anti-slop policy is acquired or required either way.
    blocked_tools = {row["tool"] for row in boot["blocked"]}
    step_text = json.dumps(boot["steps"])
    assert not blocked_tools & {"oxlint", "oxfmt", "anti-slop"}
    assert "oxlint" not in step_text and "oxfmt" not in step_text and "anti-slop" not in step_text
    assert "required-policy-unavailable" not in json.dumps(report.doc)
    providers = {c["provider"] for c in report.doc["checks"]}
    assert {"biome-lint", "biome-format"} <= providers and not providers & {"oxlint", "oxfmt"}
    assert tree_digest(repo) == before


# ---------------------------------------------------------------- F2: bad linters.rules is a usage error


@pytest.mark.parametrize("severity", ["off", "on", "no"])
def test_unquoted_yaml_boolean_severity_is_a_clean_config_error(tmp_path, severity):
    repo = _repo(tmp_path)
    put(repo, "rig.yaml", f"linters:\n  rules:\n    severity:\n      typescript/no-unsafe-type-assertion: {severity}\n")
    report = _execute(repo, tmp_path, json=True, only=())
    assert report.exit_code == 2 and report.doc["ok"] is False
    error = report.doc["error"]
    assert "linters.rules.severity.typescript/no-unsafe-type-assertion" in error
    assert 'severity: "off"' in error and "boolean" in error
    assert "Traceback" not in report.text and "internal error" not in error


def test_unknown_rule_name_under_severity_is_named(tmp_path):
    repo = _repo(tmp_path)
    put(repo, "rig.yaml", "linters:\n  rules:\n    severity:\n      no-such/rule: \"off\"\n")
    report = _execute(repo, tmp_path, json=True, only=())
    assert report.exit_code == 2 and "no-such/rule" in report.doc["error"]


# ---------------------------------------------------------------- Python 3.10 without a TOML parser is not green


def test_run_without_a_toml_parser_is_incomplete_with_an_actionable_remedy(tmp_path, monkeypatch):
    from riglib import lint_discovery_io

    monkeypatch.setattr(lint_discovery_io, "_import_toml_parser", lambda: None)
    repo = _repo(tmp_path, **{"pyproject.toml": "[tool.ruff]\nline-length = 100\n"})
    report = _execute(repo, tmp_path, json=True, only=("lint",))
    # 127 (Ruff is not installed in this sandbox) outranks 3, but either way the run is never green
    assert report.exit_code in (3, 127) and report.doc["ok"] is False
    rows = [c for c in report.doc["checks"] if c["provider"] == "discovery"]
    assert rows and rows[0]["state"] == "failed" and rows[0]["reason"] == "unreadable"
    assert "pip install tomli" in report.text


# ---------------------------------------------------------------- `rig lint rules` with an invalid severity


@pytest.mark.parametrize("flags", [[], ["--json"]])
def test_lint_rules_with_a_yaml_boolean_severity_is_a_clean_exit_2(tmp_path, monkeypatch, capsys, flags):
    from riglib.cli import main

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "rig.yaml").write_text(
        "version: 1\nlinters:\n  rules:\n    severity:\n      typescript/no-unsafe-type-assertion: off\n", encoding="utf-8")
    code = main(["lint", "rules", "-C", str(repo), "--config", str(repo / "rig.yaml"), *flags])
    captured = capsys.readouterr()
    assert code == 2
    assert "linters.rules.severity.typescript/no-unsafe-type-assertion" in captured.err
    assert 'severity: "off"' in captured.err
    assert "Traceback" not in captured.err and captured.out == ""
