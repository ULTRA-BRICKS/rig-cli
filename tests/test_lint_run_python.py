"""Python lint tools must run WITHOUT installing the application's runtime environment.

An HCB-style project lists heavy or unobtainable runtime dependencies (`torch==9999`). Running Ruff
through `uv run` would sync that whole environment, so the Ruff check has to come from the Rig-owned
pinned tool cache (or an already materialized `.venv`), and no planned command may be a bare
syncing `uv run`/`uv sync`. A shim `uv` that fails on ANY invocation proves it is never needed.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from riglib import lint_run as lr
from tests.lint_check_helpers import commit_all, make_repo, put
from tests.lint_run_helpers import LOADER_ENV, write_exe

PYPROJECT = """[project]
name = "heavy"
version = "0.1.0"
dependencies = ["torch==9999", "ntgcalls==0.0.0"]

[dependency-groups]
dev = ["pytest"]
"""


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    for name in ("CI", "GITHUB_ACTIONS", "RIG_LINT_DEPTH"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def heavy_repo(tmp_path):
    repo = make_repo(tmp_path)
    put(repo, "pyproject.toml", PYPROJECT)
    put(repo, "uv.lock", "version = 1\n")
    put(repo, "app.py", "import os\n")
    put(repo, ".gitignore", ".venv\n")
    commit_all(repo)
    return repo


@pytest.fixture
def failing_uv(tmp_path):
    """A `uv` that records every call and always fails: any sync/run would break the test."""
    bindir = tmp_path / "bin"
    log = tmp_path / "uv.log"
    write_exe(bindir / "uv", f"import sys\nopen({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\nsys.exit(99)\n")
    return bindir, log


def cached_ruff(tmp_path: Path) -> Path:
    cache = tmp_path / "toolcache"
    write_exe(cache / "py" / "ruff-0.17.3" / "bin" / "ruff", "import sys; print('All checks passed!'); sys.exit(0)\n")
    return cache


def run(repo, environ, **over):
    opts = dict(cwd=str(repo), config=None, json=True, plan=False, no_setup=True, fix=False, ci=None,
                only=("lint", "format"), skip=(), scopes=(), timeout=60)
    return lr.execute(lr.RunOptions(**{**opts, **over}), environ={**LOADER_ENV, **environ}, runner=lr.subprocess_runner,
                      which=lambda name: None)


def syncing_uv(argv) -> bool:
    return bool(argv) and os.path.basename(argv[0]) == "uv" and not (
        "--no-sync" in argv or "--no-project" in argv or "--frozen" in argv and "--no-sync" in argv)


def test_ruff_runs_from_the_tool_cache_and_never_touches_uv(tmp_path, heavy_repo, failing_uv):
    bindir, log = failing_uv
    cache = cached_ruff(tmp_path)
    environ = {"PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', os.defpath)}", "RIG_LINT_TOOL_CACHE": str(cache),
               "HOME": str(tmp_path / "home")}
    report = run(heavy_repo, environ)
    ruff = [r for r in report.doc["checks"] if r["provider"] in ("ruff-check", "ruff-format")]
    assert ruff and all(r["state"] == "passed" for r in ruff), [(r["id"], r["state"], r["reason"]) for r in ruff]
    assert all(str(cache) in r["argv"][0] for r in ruff)
    assert not any(syncing_uv(r["argv"]) for r in report.doc["checks"] if r["argv"])
    assert not log.exists(), "uv must never be invoked to lint"
    assert report.doc["summary"]["files_checked"] >= 1


def test_the_bootstrap_plan_for_a_fresh_heavy_project_never_syncs(tmp_path, heavy_repo, failing_uv):
    bindir, log = failing_uv
    environ = {"PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', os.defpath)}",
               "RIG_LINT_TOOL_CACHE": str(tmp_path / "empty-cache"), "HOME": str(tmp_path / "home")}
    report = run(heavy_repo, environ, plan=True)
    steps = report.doc["bootstrap"]["steps"]
    assert not any(syncing_uv(step["argv"]) for step in steps), steps
    assert not any(syncing_uv(c["argv"]) for c in report.doc["checks"] if c["argv"])
    assert not log.exists()
    assert (heavy_repo / "uv.lock").read_text() == "version = 1\n"
    assert report.exit_code == 0 and report.doc["mode"]["plan"] is True


def test_without_any_ruff_the_check_is_missing_tool_not_a_uv_sync(tmp_path, heavy_repo, failing_uv):
    bindir, log = failing_uv
    environ = {"PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', os.defpath)}",
               "RIG_LINT_TOOL_CACHE": str(tmp_path / "empty-cache"), "HOME": str(tmp_path / "home")}
    report = run(heavy_repo, environ)
    ruff = [r for r in report.doc["checks"] if r["provider"] == "ruff-check"]
    assert ruff and all(r["state"] == "missing-tool" and r["argv"] is None for r in ruff)
    assert report.exit_code == 127 and not log.exists()
