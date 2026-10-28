"""`rig lint run` end to end with REAL pinned binaries: discovery -> bootstrap (tool cache) -> run -> aggregation.

Each test builds a tmp git repository and starts a bare `rig lint run` as a subprocess (no `CI`, throwaway HOME,
shared pinned tool cache), so Oxlint/Oxfmt (npm), Ruff (private venv), ShellCheck/shfmt/actionlint (sha256-pinned
release binaries) are the real thing. CI sets RIG_LINT_REQUIRE_REAL=1: a tool that cannot be obtained then FAILS
the test; locally it skips with the reason (see tests/lint_real_helpers.py).
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.lint_real_helpers import (
    build_repo,
    lint_env,
    require_agent_tools_source,
    run_lint,
    session_cache,
    settle,
    states,
    tree,
    unobtainable,
)

JS_ARGS = ("--only", "lint,format")
PY_ARGS = ("--only", "lint,format")

REACT_PACKAGE = (
    '{\n  "name": "app",\n  "version": "1.0.0",\n  "private": true,\n'
    '  "dependencies": {\n    "react": "18.3.1"\n  }\n}\n'
)
REACT_CLEAN = 'import React from "react";\n\nexport const App = () => <div>{React.version}</div>;\n'
REACT_UNFORMATTED = 'export const App = () =>   <div>x</div>\n'

# A dependency no index can resolve: Ruff must never need the project's environment.
BOGUS_PYPROJECT = (
    '[project]\nname = "heavy"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n'
    'dependencies = ["rig-no-such-heavy-dependency-xyz==99.9.9"]\n'
)
PY_CLEAN = 'def add(a: int, b: int) -> int:\n    return a + b\n'
PY_UNUSED_IMPORT = 'import os\n\n\ndef add(a: int, b: int) -> int:\n    return a + b\n'

SHELL_CLEAN = '#!/bin/sh\nset -eu\nname="$1"\necho "$name"\n'
SHELL_UNQUOTED = '#!/bin/sh\nname=$1\necho $name\n'
WORKFLOW_CLEAN = (
    "name: ci\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
    "      - run: echo hello\n"
)


@pytest.fixture(scope="session")
def tool_cache() -> Path:
    """The pinned-tool cache shared by the session: CI points RIG_TEST_LINT_TOOL_CACHE at its warm cache."""
    return session_cache()


@pytest.fixture
def env(tmp_path, tool_cache) -> dict[str, str]:
    return lint_env(tmp_path / "home", tool_cache, catalog=require_agent_tools_source())


def test_react_repo_bootstraps_pinned_oxlint_and_oxfmt_then_ci_reuses_them(tmp_path, env):
    repo = build_repo(tmp_path, "react", {"package.json": REACT_PACKAGE, "src/App.jsx": REACT_CLEAN})
    doc = settle(run_lint(repo, env, *JS_ARGS), "oxlint@.", "oxfmt@.")
    assert doc["exit_code"] == 0 and doc["mode"]["setup"] == "applied"
    assert states(doc)["oxlint@."] == states(doc)["oxfmt@."] == "passed"
    assert (repo / "node_modules" / ".bin" / "oxlint").is_file()

    ci = run_lint(repo, env, "--ci", *JS_ARGS)
    assert ci["exit_code"] == 0 and ci["mode"]["ci"] is True and ci["bootstrap"]["steps"] == []
    assert states(ci)["oxlint@."] == states(ci)["oxfmt@."] == "passed"


def test_react_formatting_finding_is_exit_1_with_real_oxfmt_output(tmp_path, env):
    repo = build_repo(tmp_path, "react", {"package.json": REACT_PACKAGE, "src/App.jsx": REACT_UNFORMATTED})
    doc = settle(run_lint(repo, env, *JS_ARGS), "oxlint@.", "oxfmt@.")
    assert doc["exit_code"] == 1 and doc["ok"] is False
    assert states(doc)["oxfmt@."] == "failed"
    oxfmt = next(c for c in doc["checks"] if c["id"] == "oxfmt@.")
    assert oxfmt["reason"] == "findings" and "App.jsx" in oxfmt["output"]["stdout"] + oxfmt["output"]["stderr"]


def test_ci_run_never_installs_into_a_repo_manifest(tmp_path):
    # COLD tool cache on purpose: the session cache is warmed by the bootstrap tests above, and a warm cache is
    # the documented reuse case (test_react_repo_bootstraps_pinned_oxlint_and_oxfmt_then_ci_reuses_them). This
    # pins the other half: with nothing cached, `--ci` blocks the manifest install and reports missing-tool.
    if shutil.which("npm") is None:
        unobtainable("npm is unobtainable here: the manifest install this test refuses is never planned")
    env = lint_env(tmp_path / "home", tmp_path / "cold-cache", catalog=require_agent_tools_source())
    repo = build_repo(tmp_path, "react", {"package.json": REACT_PACKAGE, "src/App.jsx": REACT_CLEAN})
    before = tree(repo)
    doc = run_lint(repo, env, "--ci", *JS_ARGS)
    assert tree(repo) == before and not (repo / "node_modules").exists()
    assert doc["exit_code"] == 127
    # The repo-manifest install is the step CI must refuse; the policy files hosted in the (private) tool cache are
    # not a repo write, so the overall setup mode can still read `applied`.
    results = {r["id"]: r["state"] for r in doc["bootstrap"]["results"]}
    assert results["js-add-dev@."] == "blocked", results
    assert states(doc)["oxlint@."] == "missing-tool"


def test_ci_run_with_warm_cache_reuses_oxlint_without_touching_manifest(tmp_path):
    # WARM, isolated cache: a manifest-less seed run (a user's earlier bare `rig lint run`) caches the pinned tools.
    # `--ci` on a manifest repo may then run the cached binaries, but the manifest install stays blocked and the
    # repo tree (manifest, config, lock) does not change.
    if shutil.which("npm") is None:
        unobtainable("npm is unobtainable here: the pinned tools cannot be cached")
    env = lint_env(tmp_path / "home", tmp_path / "warm-cache", catalog=require_agent_tools_source())
    seed = build_repo(tmp_path, "seed", {"seed.js": "export {};\n"})
    settle(run_lint(seed, env, *JS_ARGS), "oxlint@.", "oxfmt@.")  # the cache is really warm, or the tools are unobtainable
    repo = build_repo(tmp_path, "react", {"package.json": REACT_PACKAGE, "src/App.jsx": REACT_CLEAN})
    before = tree(repo)
    doc = run_lint(repo, env, "--ci", *JS_ARGS)
    assert tree(repo) == before and not (repo / "node_modules").exists()
    results = {r["id"]: r["state"] for r in doc["bootstrap"]["results"]}
    assert results.get("js-add-dev@.") == "blocked", results
    assert doc["exit_code"] == 0 and states(doc)["oxlint@."] == "passed"


def test_python_repo_with_unobtainable_dependency_is_linted_by_cached_ruff_without_touching_the_repo(tmp_path, env):
    repo = build_repo(tmp_path, "heavy", {"pyproject.toml": BOGUS_PYPROJECT, "heavy/app.py": PY_CLEAN})
    before = tree(repo)
    doc = settle(run_lint(repo, env, *PY_ARGS), "ruff-check@.", "ruff-format@.")
    assert doc["exit_code"] == 0, doc["checks"]
    assert states(doc)["ruff-check@."] == states(doc)["ruff-format@."] == "passed"
    assert tree(repo) == before and not (repo / ".venv").exists()

    ci = run_lint(repo, env, "--ci", *PY_ARGS)
    assert ci["exit_code"] == 0 and tree(repo) == before


def test_python_finding_is_exit_1(tmp_path, env):
    repo = build_repo(tmp_path, "heavy", {"pyproject.toml": BOGUS_PYPROJECT, "heavy/app.py": PY_UNUSED_IMPORT})
    doc = settle(run_lint(repo, env, *PY_ARGS), "ruff-check@.", "ruff-format@.")
    assert doc["exit_code"] == 1
    assert states(doc)["ruff-check@."] == "failed" and states(doc)["ruff-format@."] == "passed"


def test_shell_and_workflow_repo_uses_checksum_pinned_release_binaries(tmp_path, env):
    files = {"scripts/run.sh": SHELL_CLEAN, ".github/workflows/ci.yml": WORKFLOW_CLEAN}
    repo = build_repo(tmp_path, "shell", files, executable=("scripts/run.sh",))
    doc = settle(run_lint(repo, env, *JS_ARGS), "shellcheck@.", "shfmt@.", "actionlint@.")
    assert doc["exit_code"] == 0, doc["checks"]
    assert {states(doc)[i] for i in ("shellcheck@.", "shfmt@.", "actionlint@.")} == {"passed"}


def test_shell_finding_is_exit_1(tmp_path, env):
    repo = build_repo(tmp_path, "shell", {"run.sh": SHELL_UNQUOTED}, executable=("run.sh",))
    doc = settle(run_lint(repo, env, *JS_ARGS), "shellcheck@.", "shfmt@.")
    assert doc["exit_code"] == 1 and states(doc)["shellcheck@."] == "failed"


def test_mixed_repo_aggregates_every_language_into_one_verdict(tmp_path, env):
    files = {
        "package.json": REACT_PACKAGE, "src/App.jsx": REACT_CLEAN,
        "pyproject.toml": BOGUS_PYPROJECT, "tools/gen.py": PY_UNUSED_IMPORT,
        "run.sh": SHELL_CLEAN, ".github/workflows/ci.yml": WORKFLOW_CLEAN,
    }
    repo = build_repo(tmp_path, "mixed", files, executable=("run.sh",))
    ids = ("oxlint@.", "oxfmt@.", "ruff-check@.", "ruff-format@.", "shellcheck@.", "shfmt@.", "actionlint@.")
    doc = settle(run_lint(repo, env, *JS_ARGS), *ids)
    got = states(doc)
    assert got["ruff-check@."] == "failed"  # the one real finding decides the aggregate
    assert {got[i] for i in ids if i != "ruff-check@."} == {"passed"}
    assert doc["exit_code"] == 1 and doc["summary"]["failed"] == 1 and doc["summary"]["missing_tool"] == 0
