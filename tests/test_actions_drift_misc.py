"""Misc drift/backup tests."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from riglib.actions import run_plan
from riglib.catalog import Catalog
from riglib.config import LoadedConfig
from riglib.drift import detect
from riglib.plan import build
from tests.actions_drift_helpers import (
    _full_cfg,
)


def test_mcp_registers_under_configured_server_name(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False}, "ci": {"enabled": False},
            "mcp": {"target": str(repo / "mcp-out"), "items": {"fake-mcp": {"enabled": True, "server": "custom-name", "command": "x --mcp"}}},
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    run_plan(plan)
    data = json.loads((repo / "mcp-out" / "mcp.json").read_text())
    assert "custom-name" in data["mcpServers"]  # registered under the server name
    assert "fake-mcp" not in data["mcpServers"]


def test_drift_agent_hook_descriptor_modified(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_full_cfg(repo, fake_agent_tools), cat, project_type="cli")
    run_plan(plan)
    descriptor = repo / "hooks-out" / "block-no-verify.pre-bash.json"
    spec = json.loads(descriptor.read_text())
    spec["cmd"] = "/tampered/path"
    descriptor.write_text(json.dumps(spec), encoding="utf-8")
    report = detect(plan)
    assert any(i.direction == "modified" and i.category == "agent_hooks" for i in report.items)


def test_drift_mcp_command_modified(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_full_cfg(repo, fake_agent_tools), cat, project_type="cli")
    run_plan(plan)
    mcp_json = repo / "mcp-out" / "mcp.json"
    data = json.loads(mcp_json.read_text())
    data["mcpServers"]["fake-mcp"] = {"command": "different", "args": []}
    mcp_json.write_text(json.dumps(data), encoding="utf-8")
    report = detect(plan)
    assert any(i.direction == "modified" and i.category == "mcp" and i.item == "fake-mcp" for i in report.items)


def test_drift_mcp_env_modified(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False}, "ci": {"enabled": False},
            "mcp": {
                "target": str(repo / "mcp-out"),
                "items": {
                    "fake-mcp": {
                        "enabled": True,
                        "command": "node",
                        "args": ["server.js"],
                        "env": {"NODE_ENV": "test"},
                    }
                },
            },
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    run_plan(plan)
    mcp_json = repo / "mcp-out" / "mcp.json"
    data = json.loads(mcp_json.read_text())
    data["mcpServers"]["fake-mcp"]["env"]["NODE_ENV"] = "prod"
    mcp_json.write_text(json.dumps(data), encoding="utf-8")
    report = detect(plan)
    assert any(i.direction == "modified" and i.category == "mcp" and i.item == "fake-mcp" for i in report.items)


def test_drift_mcp_args_modified(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False}, "ci": {"enabled": False},
            "mcp": {
                "target": str(repo / "mcp-out"),
                "items": {"fake-mcp": {"enabled": True, "command": "node", "args": ["server.js"]}},
            },
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    run_plan(plan)
    mcp_json = repo / "mcp-out" / "mcp.json"
    data = json.loads(mcp_json.read_text())
    data["mcpServers"]["fake-mcp"]["args"] = ["other.js"]
    mcp_json.write_text(json.dumps(data), encoding="utf-8")
    report = detect(plan)
    assert any(i.direction == "modified" and i.category == "mcp" and i.item == "fake-mcp" for i in report.items)


def test_agent_hook_cmd_rewritten_absolute(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_full_cfg(repo, fake_agent_tools), cat, project_type="cli")
    run_plan(plan)
    descriptor = repo / "hooks-out" / "block-no-verify.pre-bash.json"
    assert descriptor.is_file()
    spec = json.loads(descriptor.read_text())
    assert "/ABSOLUTE/PATH/TO/" not in spec["cmd"]
    assert spec["cmd"].startswith(str(fake_agent_tools))
    assert Path(spec["cmd"]).is_absolute()


def test_ci_companion_root_with_custom_target(fake_agent_tools, tmp_path):
    """With a non-default ci.target, companions still vendor at the checkout root ci/<slot>/."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "ci": {"target": str(repo / ".ci/workflows"), "items": {"leftover-grep": {"enabled": True}}},
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    assert (repo / ".ci/workflows/leftover-grep.yml").is_file()  # workflow at custom target
    assert (repo / "ci/leftover-grep/leftover-grep.sh").is_file()  # companion at CHECKOUT root
    assert not (repo / ".ci/ci").exists()  # NOT mis-anchored under the custom target


def test_ci_missing_variant_fails_closed(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "ci": {"target": str(repo / ".github/workflows"),
                   "items": {"codeql": {"enabled": True, "variant": "nonexistent"}}},
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    report = run_plan(plan)
    assert report.errors  # requested variant missing → error, not silent default
    assert any("variant" in r.detail for r in report.errors)


def test_identical_content_restores_exec_bit(fake_agent_tools, tmp_path, monkeypatch):
    """A managed script with identical content but a lost exec bit gets it restored."""
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    monkeypatch.setattr("riglib.actions.git_config._set_git_global", lambda k, v: 0)
    monkeypatch.setattr("riglib.actions.git_config._git_global", lambda k: None)
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": True, "dir": str(home / "ghd"),
                                         "runner": str(home / "run-global-hooks"),
                                         "set_global_hooks_path": False,
                                         "install_local_retrofit_script": False}},
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    run_plan(plan)
    runner = home / "run-global-hooks"
    runner.chmod(0o644)  # lose the exec bit (content unchanged)
    run_plan(plan)  # re-apply: identical content, but exec bit must be restored
    assert (runner.stat().st_mode & 0o111) != 0


def test_mcp_backup_converges_differing_entry(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "defaults": {"on_conflict": "backup"},
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False}, "ci": {"enabled": False},
            "mcp": {"target": str(repo / "mcp-out"), "items": {"fake-mcp": {"enabled": True, "command": "fake-mcp --serve"}}},
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    run_plan(plan)
    # tamper the registered entry, then re-apply under backup → must converge + back up
    mcp_json = repo / "mcp-out" / "mcp.json"
    data = json.loads(mcp_json.read_text())
    data["mcpServers"]["fake-mcp"] = {"command": "stale", "args": []}
    mcp_json.write_text(json.dumps(data), encoding="utf-8")
    report = run_plan(plan)
    assert not report.errors
    converged = json.loads(mcp_json.read_text())
    assert converged["mcpServers"]["fake-mcp"] == {"command": "fake-mcp", "args": ["--serve"]}
    assert any(p.name.startswith("mcp.json.rig-bak-") for p in (repo / "mcp-out").iterdir())


def test_skip_conflict_does_not_chmod(fake_agent_tools, tmp_path, monkeypatch):
    """on_conflict=skip must leave an existing different file's mode untouched."""
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    monkeypatch.setattr("riglib.actions.git_config._set_git_global", lambda k, v: 0)
    monkeypatch.setattr("riglib.actions.git_config._git_global", lambda k: None)
    runner_path = home / "run-global-hooks"
    runner_path.parent.mkdir(parents=True)
    runner_path.write_text("#!/bin/sh\nexisting\n", encoding="utf-8")
    runner_path.chmod(0o644)  # NOT executable
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "defaults": {"on_conflict": "skip"},
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": True, "dir": str(home / "ghd"),
                                         "runner": str(runner_path), "set_global_hooks_path": False,
                                         "install_local_retrofit_script": False}},
        },
        repo_root=repo,
    )
    run_plan(build(cfg, cat, project_type="unknown"))
    # skip left the existing runner alone → mode unchanged (still 0o644, no exec bit)
    assert (runner_path.stat().st_mode & 0o111) == 0


def test_ci_companion_script_vendored(fake_agent_tools, tmp_path):
    """A workflow that shells out to ci/<slot>/<slot>.sh must get that script vendored."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "ci": {"target": str(repo / ".github/workflows"), "items": {"leftover-grep": {"enabled": True}}},
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    assert (repo / ".github/workflows/leftover-grep.yml").is_file()
    assert (repo / "ci/leftover-grep/leftover-grep.sh").is_file()  # companion vendored


def test_ci_companion_python_script_vendored(fake_agent_tools, tmp_path):
    """A workflow that runs `python3 ci/<slot>/<name>.py` gets that script (and a helper it
    imports) vendored and drift-tracked; the slot's own tests stay in agent-tools (rig-cli#725)."""
    slot = fake_agent_tools / "ci" / "python-only"
    slot.mkdir(parents=True)
    (slot / "workflow.yml").write_text(
        "name: python-only\njobs:\n  x:\n    steps:\n      - run: python3 ci/python-only/python_only.py\n",
        encoding="utf-8",
    )
    (slot / "python_only.py").write_text("#!/usr/bin/env python3\nimport helpers\n", encoding="utf-8")
    (slot / "helpers.py").write_text("X = 1\n", encoding="utf-8")
    for test_file in ("test_python_only.py", "python_only_test.py", "python_only.test.py", "conftest.py"):
        (slot / test_file).write_text("def test_x():\n    pass\n", encoding="utf-8")
    (slot / "README.md").write_text("# python-only\n", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "ci": {"target": str(repo / ".github/workflows"), "items": {"python-only": {"enabled": True}}},
        },
        repo_root=repo,
    )
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    vendored = repo / "ci/python-only/python_only.py"
    assert vendored.read_text(encoding="utf-8") == (slot / "python_only.py").read_text(encoding="utf-8")
    assert os.access(vendored, os.X_OK)
    assert (repo / "ci/python-only/helpers.py").is_file()
    for test_file in ("test_python_only.py", "python_only_test.py", "python_only.test.py", "conftest.py"):
        assert not (repo / "ci/python-only" / test_file).exists(), test_file
    assert not [i for i in detect(plan).items if i.category == "ci"]

    vendored.write_text("print('edited')\n", encoding="utf-8")
    assert any(
        i.direction == "modified" and i.item == "python-only:python_only.py" for i in detect(plan).items
    )
    vendored.unlink()
    assert any(
        i.direction == "missing" and i.item == "python-only:python_only.py" for i in detect(plan).items
    )


def test_ci_all_installs_new_python_slot_with_its_script(fake_agent_tools, tmp_path):
    """`ci.all: true` picks up a newly published slot with no per-repo enablement, so its
    workflow must never land without the `.py` it runs (rollout order in the python-only spec)."""
    slot = fake_agent_tools / "ci" / "python-only"
    slot.mkdir(parents=True)
    (slot / "workflow.yml").write_text(
        "name: python-only\njobs:\n  x:\n    steps:\n      - run: python3 ci/python-only/python_only.py\n",
        encoding="utf-8",
    )
    (slot / "python_only.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "ci": {"target": str(repo / ".github/workflows"), "all": True},
        },
        repo_root=repo,
    )
    run_plan(build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    assert (repo / ".github/workflows/python-only.yml").is_file()
    assert (repo / "ci/python-only/python_only.py").is_file()


def test_secret_scan_workflow_resolves_slotnamed(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_full_cfg(repo, fake_agent_tools), cat, project_type="cli")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    assert (repo / ".github/workflows/secret-scan.yml").is_file()


def test_mcp_idempotent_merge(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_full_cfg(repo, fake_agent_tools), cat, project_type="cli")
    run_plan(plan)
    mcp_json = repo / "mcp-out" / "mcp.json"
    data = json.loads(mcp_json.read_text())
    assert data["mcpServers"]["fake-mcp"]["command"] == "fake-mcp"
    assert data["mcpServers"]["fake-mcp"]["args"] == ["--serve"]


def test_backup_on_conflict(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _full_cfg(repo, fake_agent_tools)
    plan = build(cfg, cat, project_type="cli")
    run_plan(plan)
    # mutate an installed CI workflow, then re-apply: backup must be made
    wf = repo / ".github/workflows/codeql.yml"
    wf.write_text("name: tampered\n", encoding="utf-8")
    report = run_plan(plan)
    backed = [r for r in report.results if r.status == "backed_up"]
    assert any(r.action.item == "codeql" for r in backed)
    assert any(p.name.startswith("codeql.yml.rig-bak-") for p in wf.parent.iterdir())


def test_dry_run_writes_nothing(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_full_cfg(repo, fake_agent_tools), cat, project_type="cli")
    report = run_plan(plan, dry_run=True)
    assert all(r.status == "planned" for r in report.results)
    assert not (repo / "skills-out").exists()


def test_drift_missing_then_in_sync(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_full_cfg(repo, fake_agent_tools), cat, project_type="cli")
    # before apply: everything declared is missing
    before = detect(plan)
    assert not before.in_sync
    assert before.by_direction("missing")
    # after apply: in sync
    run_plan(plan)
    after = detect(plan)
    assert after.in_sync, [i.detail for i in after.items]


def test_drift_detects_extra_skill(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_full_cfg(repo, fake_agent_tools), cat, project_type="cli")
    run_plan(plan)
    # plant an undeclared skill on disk
    extra = repo / "skills-out" / "rogue-skill"
    extra.mkdir(parents=True)
    (extra / "SKILL.md").write_text("---\nname: rogue\n---\n", encoding="utf-8")
    report = detect(plan)
    extras = report.by_direction("extra")
    assert any(i.item == "rogue-skill" for i in extras), [i.item for i in extras]


def test_drift_detects_modified_skill(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_full_cfg(repo, fake_agent_tools), cat, project_type="cli")
    run_plan(plan)
    # tamper an installed skill
    (repo / "skills-out" / "shell-timeouts" / "SKILL.md").write_text("changed\n", encoding="utf-8")
    report = detect(plan)
    assert any(i.direction == "modified" and i.item == "shell-timeouts" for i in report.items)
