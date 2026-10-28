"""The `rig-check` child must run THIS Rig even when the linted repository shadows `riglib`.

`rig lint run` starts the nested `rig lint check` with the linted repo as cwd. `python -m riglib`
there would import the repository's own `riglib.py` / `riglib/` first (arbitrary repo code with the
user's rights, and a forged verdict). These tests build such a repo and run the real child.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from riglib import lint_run as lr
from riglib import lint_check_launcher
from tests.lint_check_helpers import commit_all, make_repo, put

LAUNCHER = Path(lint_check_launcher.__file__).resolve()
RIG_ROOT = LAUNCHER.parent.parent


@pytest.fixture(autouse=True)
def _ambient_markers(monkeypatch):
    for name in ("CI", "GITHUB_ACTIONS", "RIG_LINT_DEPTH", "PYTHONPATH"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def hostile_repo(tmp_path):
    """A repo whose `riglib` (module AND package) would write a sentinel and crash if ever imported."""
    sentinel = tmp_path / "SENTINEL"
    body = f"open({str(sentinel)!r}, 'w').write('shadow riglib imported')\nraise RuntimeError('shadow riglib')\n"
    repo = make_repo(tmp_path)
    put(repo, "riglib.py", body)
    put(repo, "riglib/__init__.py", body)
    put(repo, "profile.py", body)  # riglib/ must not be on sys.path either: it holds stdlib-named modules
    commit_all(repo)
    return repo, sentinel


def test_control_python_dash_m_in_the_repo_imports_the_shadow(hostile_repo):
    """Proves the fixture is hostile: this is exactly what the old argv did."""
    repo, sentinel = hostile_repo
    subprocess.run([sys.executable, "-m", "riglib", "--help"], cwd=repo, capture_output=True, check=False,
                   env={**os.environ, "PYTHONPATH": str(RIG_ROOT)})
    assert sentinel.exists()


def test_launcher_from_a_hostile_cwd_runs_the_trusted_rig_only(hostile_repo):
    repo, sentinel = hostile_repo
    proc = subprocess.run([sys.executable, str(LAUNCHER), "lint", "check", "--json", "-C", str(repo)],
                          cwd=repo, capture_output=True, text=True, check=False)
    assert not sentinel.exists(), proc.stderr
    doc = json.loads(proc.stdout)
    assert doc["schema"] == "rig-lint-check/v1"
    assert proc.returncode in (0, 1, 3, 127)  # a verdict of the real engine, never a shadow traceback


def test_launcher_ignores_a_pythonpath_that_points_at_the_hostile_repo(hostile_repo):
    repo, sentinel = hostile_repo
    proc = subprocess.run([sys.executable, str(LAUNCHER), "lint", "check", "--json", "-C", str(repo)],
                          cwd=repo, capture_output=True, text=True, check=False,
                          env={**os.environ, "PYTHONPATH": f"{repo}{os.pathsep}."})
    assert not sentinel.exists(), proc.stderr
    assert json.loads(proc.stdout)["schema"] == "rig-lint-check/v1"


def test_rig_lint_run_uses_the_trusted_child_end_to_end(hostile_repo):
    repo, sentinel = hostile_repo
    opts = lr.RunOptions(cwd=str(repo), config=None, json=True, plan=False, no_setup=True, fix=False, ci=None,
                         only=("limits",), skip=(), scopes=(), timeout=120)
    report = lr.execute(opts, environ=dict(os.environ), runner=lr.subprocess_runner)
    row = next(r for r in report.doc["checks"] if r["provider"] == "rig-check")
    assert not sentinel.exists()
    assert str(LAUNCHER) in row["argv"] and "-m" not in row["argv"]
    # a real verdict of the trusted engine: never the shadow's crash / garbage
    assert row["reason"] in ("ok", "findings", "analyzer-unavailable"), row
    if row["reason"] != "analyzer-unavailable":
        assert row["report"]["schema"] == "rig-lint-check/v1"
    assert report.exit_code in (0, 1, 127)
