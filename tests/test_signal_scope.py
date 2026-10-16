"""Tests for riglib/signal_scope.py: the Landlock signal-scope launch helper (#704).

Unit tests use fakes and run everywhere; kernel tests need Landlock ABI >= 6 and
prove the real containment against an outside canary process.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from riglib import signal_scope as ss

needs_landlock = pytest.mark.skipif(
    ss.landlock_abi() < 6, reason="needs Landlock ABI >= 6"
)


def _recorder(calls):
    def exec_fn(file, args):
        calls.append((file, args, os.environ.get("RIG_SIGNAL_SCOPED")))
        return 0

    return exec_fn


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("RIG_SIGNAL_SCOPED", raising=False)
    monkeypatch.delenv("RIG_SIGNAL_SCOPE", raising=False)


def test_scoped_argv_is_absolute_and_shell_free():
    argv = ss.scoped_argv(["/opt/my omp/omp", "-r", "a b"])
    assert argv == [
        sys.executable,
        "-I",
        "-S",
        str(ss.HELPER),
        "--",
        "/opt/my omp/omp",
        "-r",
        "a b",
    ]
    assert Path(argv[3]).is_absolute()


def test_restricted_exec_marks_env():
    calls = []
    ss.main(
        ["--", "omp", "-x"],
        restrict=lambda: (ss.Outcome.RESTRICTED, ""),
        exec_fn=_recorder(calls),
    )
    assert calls == [("omp", ["omp", "-x"], "1")]


def test_unsupported_warns_once_and_still_execs(capsys):
    calls = []
    ss.main(
        ["--", "omp"],
        restrict=lambda: (ss.Outcome.UNSUPPORTED, "landlock ABI 0 < 6"),
        exec_fn=_recorder(calls),
    )
    assert (
        capsys.readouterr().err
        == "rig signal-scope: not contained (landlock ABI 0 < 6); starting omp unrestricted\n"
    )
    assert calls == [("omp", ["omp"], None)]


def test_failed_warns_and_still_execs(capsys):
    calls = []
    ss.main(
        ["--", "omp"],
        restrict=lambda: (ss.Outcome.FAILED, "restrict_self: Invalid argument"),
        exec_fn=_recorder(calls),
    )
    assert (
        capsys.readouterr().err
        == "rig signal-scope: not contained (restrict_self: Invalid argument); starting omp unrestricted\n"
    )
    assert calls == [("omp", ["omp"], None)]


def test_nesting_limit_execs_silently(capsys):
    calls = []
    ss.main(
        ["--", "omp"],
        restrict=lambda: (ss.Outcome.NESTING_LIMIT, "E2BIG"),
        exec_fn=_recorder(calls),
    )
    assert capsys.readouterr().err == ""
    assert calls == [("omp", ["omp"], None)]


def test_kill_switch_skips_restrict(monkeypatch):
    monkeypatch.setenv("RIG_SIGNAL_SCOPE", "0")
    calls = []

    def restrict():
        raise AssertionError("restrict must not run")

    ss.main(["--", "omp"], restrict=restrict, exec_fn=_recorder(calls))
    assert calls == [("omp", ["omp"], None)]


def test_missing_command_is_usage_error():
    assert ss.main([]) == 2 and ss.main(["--"]) == 2


def test_exec_failure_returns_127(capsys):
    def exec_fn(file, args):
        raise FileNotFoundError(2, "No such file or directory")

    rc = ss.main(
        ["--", "nope"], restrict=lambda: (ss.Outcome.RESTRICTED, ""), exec_fn=exec_fn
    )
    assert rc == 127
    assert "rig signal-scope: cannot exec nope: " in capsys.readouterr().err


def test_helper_imports_no_riglib():
    tree = ast.parse(ss.HELPER.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(a.name.split(".")[0] == "riglib" for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and (node.module or "").split(".")[0] != "riglib"


@pytest.fixture
def canary():
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait()


def run(argv):
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("RIG_SIGNAL_SCOPE", "RIG_SIGNAL_SCOPED")
    }
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=30, env=env, check=False
    )


@needs_landlock
def test_contained_child_cannot_signal_outside(canary):
    code = (
        "import os,signal\nfor s in (0, signal.SIGTERM):\n"
        f"  try: os.kill({canary.pid}, s); print('sent')\n"
        "  except PermissionError: print('EPERM')"
    )
    r = run(ss.scoped_argv([sys.executable, "-c", code]))
    assert r.stdout.split() == ["EPERM", "EPERM"], r.stderr
    assert canary.poll() is None


@needs_landlock
def test_contained_child_kills_its_own_child(canary):
    r = run(ss.scoped_argv(["bash", "-c", "sleep 20 & kill $!; wait $!; echo $?"]))
    assert r.stdout.strip() == "143", r.stderr


@needs_landlock
def test_kill_minus_one_spares_outside(canary):
    r = run(ss.scoped_argv(["bash", "-c", "sleep 20 & kill -TERM -1; wait"]))
    assert canary.poll() is None, r.stderr


@needs_landlock
def test_nested_scopes_still_start():
    argv = ["true"]
    for _ in range(17):
        argv = ss.scoped_argv(argv)
    r = run(argv)
    assert r.returncode == 0, r.stderr
    assert "not contained" not in r.stderr
