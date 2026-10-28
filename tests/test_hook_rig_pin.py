"""`rig apply` pins the rig that provisioned an agent hook into the hook's installed descriptor.

The agent-tools `block-unlanded-removal` hook shells out to `rig worktree guard`. Resolving `rig`
through the hook's PATH lets a shadowing binary (or a stale install) answer for the guard, so the
source descriptor carries `"args": ["--rig", "/ABSOLUTE/PATH/TO/rig"]` and rig replaces that exact
placeholder, at install time and in the drift check alike, with the absolute path of the launcher
that ran the apply. The runner (agents-hooks/v1) appends descriptor `args` to the hook's argv.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest
from conftest import _write

from riglib.actions import run_plan
from riglib.actions.agent_hook_install import RIG_PIN_PLACEHOLDER, pinned_rig_path
from riglib.catalog import Catalog
from riglib.config import LoadedConfig
from riglib.drift import detect
from riglib.plan import build

DESCRIPTOR = "pinned-hook.pre-bash.json"


def _launcher(directory: Path, name: str = "rig") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _plan(fake_agent_tools: Path, repo: Path, args: list[str] | None):
    spec: dict = {
        "id": "pinned-hook",
        "point": "pre-bash",
        "cmd": "/ABSOLUTE/PATH/TO/agent-hooks/pinned-hook/pinned_hook.py",
        "on_error": "closed",
    }
    if args is not None:
        spec["args"] = args
    _write(fake_agent_tools / "agent-hooks" / "pinned-hook" / DESCRIPTOR, json.dumps(spec))
    _write(fake_agent_tools / "agent-hooks" / "pinned-hook" / "pinned_hook.py", "#!/usr/bin/env python3\n")
    _write(fake_agent_tools / "agent-hooks" / "pinned-hook" / "README.md", "# pinned-hook\npins rig\n")
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "defaults": {"hooks_target": str(repo / "hooks-out"), "on_conflict": "overwrite"},
            "skills": {"enabled": False},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "agent_hooks": {"enabled": True, "items": {"pinned-hook": {"enabled": True}}},
        },
        repo_root=repo,
    )
    return build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="cli")


def _installed(repo: Path) -> dict:
    return json.loads((repo / "hooks-out" / DESCRIPTOR).read_text(encoding="utf-8"))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    return path


def test_placeholder_is_replaced_by_the_launcher_that_ran_the_apply(fake_agent_tools, repo, tmp_path, monkeypatch):
    launcher = _launcher(tmp_path / "bin")
    monkeypatch.setattr(sys, "argv", [str(launcher), "apply", "commit"])
    plan = _plan(fake_agent_tools, repo, ["--rig", RIG_PIN_PLACEHOLDER])

    report = run_plan(plan)

    assert not report.errors, [r.detail for r in report.errors]
    assert _installed(repo)["args"] == ["--rig", str(launcher)]


def test_only_the_exact_placeholder_argument_is_replaced(fake_agent_tools, repo, tmp_path, monkeypatch):
    launcher = _launcher(tmp_path / "bin")
    monkeypatch.setattr(sys, "argv", [str(launcher)])
    other = f"--note={RIG_PIN_PLACEHOLDER}"
    plan = _plan(fake_agent_tools, repo, ["--first", RIG_PIN_PLACEHOLDER, other, "--last"])

    run_plan(plan)

    assert _installed(repo)["args"] == ["--first", str(launcher), other, "--last"]


def test_a_descriptor_without_the_placeholder_is_untouched(fake_agent_tools, repo, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["/definitely/not/rig"])
    monkeypatch.setenv("PATH", "")
    plan = _plan(fake_agent_tools, repo, ["--verbose"])

    report = run_plan(plan)

    assert not report.errors
    assert _installed(repo)["args"] == ["--verbose"]


def test_a_descriptor_with_no_args_key_stays_without_args(fake_agent_tools, repo, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["/definitely/not/rig"])
    plan = _plan(fake_agent_tools, repo, None)

    run_plan(plan)

    assert "args" not in _installed(repo)


def test_launcher_is_found_on_path_when_the_process_is_not_the_rig_script(fake_agent_tools, repo, tmp_path, monkeypatch):
    """`python -m riglib` / an embedding process: argv[0] is not a `rig` launcher, so the pin falls
    back to the `rig` on PATH — an absolute path, not the bare word."""
    launcher = _launcher(tmp_path / "pathbin")
    monkeypatch.setattr(sys, "argv", ["/usr/lib/python3/site-packages/riglib/__main__.py"])
    monkeypatch.setenv("PATH", str(launcher.parent))

    assert pinned_rig_path() == str(launcher)
    assert os.path.isabs(pinned_rig_path())


def test_a_relative_argv0_is_made_absolute(tmp_path, monkeypatch):
    launcher = _launcher(tmp_path / "bin")
    monkeypatch.chdir(launcher.parent)
    monkeypatch.setattr(sys, "argv", ["./rig"])

    assert pinned_rig_path() == str(launcher)


def test_an_argv0_named_rig_that_is_not_an_executable_file_is_not_trusted(tmp_path, monkeypatch):
    fallback = _launcher(tmp_path / "pathbin")
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "ghost" / "rig")])  # does not exist
    monkeypatch.setenv("PATH", str(fallback.parent))

    assert pinned_rig_path() == str(fallback)


def test_an_argv0_that_is_a_directory_a_non_executable_or_not_named_rig_is_not_trusted(tmp_path, monkeypatch):
    fallback = _launcher(tmp_path / "pathbin")
    monkeypatch.setenv("PATH", str(fallback.parent))
    directory = tmp_path / "dir" / "rig"
    directory.mkdir(parents=True)  # a directory is "executable" (searchable) too
    plain = tmp_path / "plain" / "rig"
    plain.parent.mkdir()
    plain.write_text("#!/bin/sh\n", encoding="utf-8")
    plain.chmod(0o644)
    other = _launcher(tmp_path / "other", name="pytest")  # executable, but not a rig launcher

    for argv0 in (directory, plain, other):
        monkeypatch.setattr(sys, "argv", [str(argv0)])
        assert pinned_rig_path() == str(fallback), argv0


def test_no_launcher_anywhere_fails_the_install_loudly_and_writes_nothing(fake_agent_tools, repo, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["/definitely/not/rig"])
    monkeypatch.setenv("PATH", "")
    plan = _plan(fake_agent_tools, repo, ["--rig", RIG_PIN_PLACEHOLDER])

    report = run_plan(plan)

    assert any("rig" in r.detail and "pin" in r.detail for r in report.errors), [r.detail for r in report.results]
    assert not (repo / "hooks-out" / DESCRIPTOR).exists()


def test_drift_is_clean_for_the_same_launcher_and_modified_when_it_moves(fake_agent_tools, repo, tmp_path, monkeypatch):
    first = _launcher(tmp_path / "bin-a")
    second = _launcher(tmp_path / "bin-b")
    monkeypatch.setattr(sys, "argv", [str(first)])
    plan = _plan(fake_agent_tools, repo, ["--rig", RIG_PIN_PLACEHOLDER])
    run_plan(plan)

    assert not [i for i in detect(plan).items if i.item == "pinned-hook"]

    monkeypatch.setattr(sys, "argv", [str(second)])  # rig is now launched from elsewhere
    drift = [i for i in detect(plan).items if i.item == "pinned-hook"]
    assert drift and drift[0].direction == "modified"

    run_plan(plan)  # re-apply re-pins
    assert _installed(repo)["args"] == ["--rig", str(second)]
    assert not [i for i in detect(plan).items if i.item == "pinned-hook"]


def test_the_pin_is_idempotent(fake_agent_tools, repo, tmp_path, monkeypatch):
    launcher = _launcher(tmp_path / "bin")
    monkeypatch.setattr(sys, "argv", [str(launcher)])
    plan = _plan(fake_agent_tools, repo, ["--rig", RIG_PIN_PLACEHOLDER])

    run_plan(plan)
    second = run_plan(plan)

    assert second.changed == 0


def test_drift_says_so_when_the_pin_cannot_be_resolved_instead_of_reporting_clean(fake_agent_tools, repo, tmp_path, monkeypatch):
    """`rig status` must not read clean for a descriptor the next `rig apply` would refuse to write."""
    launcher = _launcher(tmp_path / "bin")
    monkeypatch.setattr(sys, "argv", [str(launcher)])
    plan = _plan(fake_agent_tools, repo, ["--rig", RIG_PIN_PLACEHOLDER])
    run_plan(plan)

    monkeypatch.setattr(sys, "argv", ["/definitely/not/rig"])
    monkeypatch.setenv("PATH", "")
    drift = [i for i in detect(plan).items if i.item == "pinned-hook"]

    assert drift and drift[0].direction == "modified" and "pin" in drift[0].detail


def test_an_unreadable_source_descriptor_is_still_left_to_the_install_action(fake_agent_tools, repo, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["/definitely/not/rig"])
    plan = _plan(fake_agent_tools, repo, ["--verbose"])
    run_plan(plan)
    (fake_agent_tools / "agent-hooks" / "pinned-hook" / DESCRIPTOR).write_text("{not json", encoding="utf-8")

    assert not [i for i in detect(plan).items if i.item == "pinned-hook"]

