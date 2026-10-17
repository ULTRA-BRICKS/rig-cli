"""`rig doctor`'s scheduler check (rig-cli#721): a present scheduler binary is not enough — rig
must be able to WRITE the schedule (reachable systemd user manager + writable unit dir)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from riglib import doctor

# captured at import, before conftest's autouse stub replaces it for the rest of the suite
_REAL = doctor.scheduler_problem


@pytest.fixture
def linux_home(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.sys, "platform", "linux")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return tmp_path


def _fake_run(rc: int, stderr: str = ""):
    def run(argv, **kwargs):
        assert argv == ["systemctl", "--user", "show-environment"]
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr=stderr)

    return run


def test_reachable_manager_and_writable_dir_is_fine(linux_home, monkeypatch):
    monkeypatch.setattr(doctor.subprocess, "run", _fake_run(0))
    assert _REAL() is None


def test_unreachable_user_manager_is_flagged(linux_home, monkeypatch):
    monkeypatch.setattr(doctor.subprocess, "run", _fake_run(1, "Failed to connect to bus: No medium found\n"))
    problem = _REAL()
    assert problem and "no reachable systemd user manager" in problem
    assert "Failed to connect to bus" in problem


def test_unwritable_unit_dir_is_flagged(linux_home, monkeypatch):
    monkeypatch.setattr(doctor.subprocess, "run", _fake_run(0))
    monkeypatch.setattr(doctor.os, "access", lambda path, mode: False)
    problem = _REAL()
    assert problem and "not writable" in problem and ".config/systemd/user" in problem
