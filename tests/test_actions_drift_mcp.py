"""MCP entry action tests."""

from __future__ import annotations

import json
import re
from riglib.actions import run_plan
from riglib.actions.mcp import desired_mcp_server_entry
from riglib.catalog import Catalog
from riglib.config import LoadedConfig
from riglib.drift import detect
from riglib.plan import build
from tests.actions_drift_helpers import (
    _harness_cfg,
)


def test_mcp_command_shell_quoting(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False}, "ci": {"enabled": False},
            "mcp": {"target": str(repo / "mcp-out"),
                    "items": {"fake-mcp": {"enabled": True, "command": '"/opt/my tools/run" --flag "a b"'}}},
        },
        repo_root=repo,
    )
    run_plan(build(cfg, cat, project_type="unknown"))
    data = json.loads((repo / "mcp-out" / "mcp.json").read_text())
    entry = data["mcpServers"]["fake-mcp"]
    assert entry["command"] == "/opt/my tools/run"  # quoted path with spaces kept whole
    assert entry["args"] == ["--flag", "a b"]  # quotes consumed, inner spaces preserved


def test_desired_mcp_server_entry_legacy_explicit_args_and_env():
    assert desired_mcp_server_entry({"command": 'node "server path.js" --flag'}) == {
        "command": "node",
        "args": ["server path.js", "--flag"],
    }
    assert desired_mcp_server_entry({"command": '"/opt/my tools/run" --flag "a b"'}) == {
        "command": "/opt/my tools/run",
        "args": ["--flag", "a b"],
    }
    assert desired_mcp_server_entry({"command": ""}) == {"command": "", "args": []}
    assert desired_mcp_server_entry({
        "command": "/opt/my tools/run",
        "args": ["server.js"],
        "env": {"NODE_ENV": "test"},
    }) == {
        "command": "/opt/my tools/run",
        "args": ["server.js"],
        "env": {"NODE_ENV": "test"},
    }
    assert desired_mcp_server_entry({"command": "mycmd --implicit-arg", "args": ["explicit-arg"]}) == {
        "command": "mycmd --implicit-arg",
        "args": ["explicit-arg"],
    }
    assert desired_mcp_server_entry({"command": "node", "args": [], "env": {}}) == {
        "command": "node",
        "args": [],
    }
    assert desired_mcp_server_entry({"command": "mycmd", "env": None}) == {
        "command": "mycmd",
        "args": [],
    }


def test_mcp_explicit_args_keep_command_exact_and_add_env(fake_agent_tools, tmp_path):
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
                        "command": "/opt/my tools/run",
                        "args": ["server.js", "--flag", "a b"],
                        "env": {"NODE_ENV": "test", "TOKEN": "abc"},
                    }
                },
            },
        },
        repo_root=repo,
    )
    run_plan(build(cfg, cat, project_type="unknown"))
    data = json.loads((repo / "mcp-out" / "mcp.json").read_text())
    assert data["mcpServers"]["fake-mcp"] == {
        "command": "/opt/my tools/run",
        "args": ["server.js", "--flag", "a b"],
        "env": {"NODE_ENV": "test", "TOKEN": "abc"},
    }


def test_mcp_explicit_args_do_not_shell_split_command(fake_agent_tools, tmp_path):
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
                        "command": "node server.js",
                        "args": ["--verbose"],
                    }
                },
            },
        },
        repo_root=repo,
    )
    run_plan(build(cfg, cat, project_type="unknown"))
    data = json.loads((repo / "mcp-out" / "mcp.json").read_text())
    assert data["mcpServers"]["fake-mcp"] == {
        "command": "node server.js",
        "args": ["--verbose"],
    }


def test_mcp_empty_env_is_not_written(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False}, "ci": {"enabled": False},
            "mcp": {
                "target": str(repo / "mcp-out"),
                "items": {"fake-mcp": {"enabled": True, "command": "node", "args": [], "env": {}}},
            },
        },
        repo_root=repo,
    )
    run_plan(build(cfg, cat, project_type="unknown"))
    data = json.loads((repo / "mcp-out" / "mcp.json").read_text())
    assert data["mcpServers"]["fake-mcp"] == {"command": "node", "args": []}
    assert [i for i in detect(build(cfg, cat, project_type="unknown")).items if i.category == "mcp"] == []


def test_drift_mcp_existing_without_env_matches_when_env_is_omitted(fake_agent_tools, tmp_path):
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
    mcp_dir = repo / "mcp-out"
    mcp_dir.mkdir()
    (mcp_dir / "mcp.json").write_text(
        json.dumps({"mcpServers": {"fake-mcp": {"command": "node", "args": ["server.js"]}}}),
        encoding="utf-8",
    )
    report = detect(build(cfg, cat, project_type="unknown"))
    assert [i for i in report.items if i.category == "mcp"] == []


def test_mcp_empty_command_skips_even_with_args_and_env(fake_agent_tools, tmp_path):
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
                        "command": "",
                        "args": ["server.js"],
                        "env": {"NODE_ENV": "test"},
                    }
                },
            },
        },
        repo_root=repo,
    )
    report = run_plan(build(cfg, cat, project_type="unknown"))
    result = next(r for r in report.results if r.action.kind == "register_mcp")
    assert result.status == "skipped"
    assert "no command set" in result.detail
    assert not (repo / "mcp-out" / "mcp.json").exists()


def test_harness_apply_writes_mode_and_is_idempotent(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_harness_cfg(repo, fake_agent_tools, mode="acceptEdits"), cat, project_type="unknown")

    first = run_plan(plan)
    assert not first.errors, [r.detail for r in first.errors]
    settings = repo / ".claude" / "settings.json"
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["permissions"]["defaultMode"] == "acceptEdits"
    assert any(r.status == "created" for r in first.results if r.action.category == "harness")

    second = run_plan(plan)  # idempotent: same value → skipped, no change
    h2 = [r for r in second.results if r.action.category == "harness"]
    assert h2 and all(r.status == "skipped" for r in h2)


def test_harness_apply_preserves_other_keys(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    settings = repo / ".claude" / "settings.json"
    settings.write_text(
        json.dumps({"model": "opus", "permissions": {"allow": ["Bash"]}}), encoding="utf-8"
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_harness_cfg(repo, fake_agent_tools, mode="acceptEdits"), cat, project_type="unknown")
    run_plan(plan)
    data = json.loads(settings.read_text(encoding="utf-8"))
    # the managed key is set; unrelated keys survive
    assert data["permissions"]["defaultMode"] == "acceptEdits"
    assert data["permissions"]["allow"] == ["Bash"]
    assert data["model"] == "opus"


def test_harness_apply_preserves_unmanaged_plugin_state(fake_agent_tools, tmp_path):
    """rig-cli#257 follow-up (Haft/omp provisioning investigation): Claude Code's own
    ``enabledPlugins`` / ``extraKnownMarketplaces`` top-level keys are entirely foreign to rig
    (rig has no plugin concept at all) but MUST survive a claude-code harness write byte-for-byte,
    the same as any other unmanaged key — pin this so a future settings.json writer can never
    silently regress into a full-file overwrite that drops them."""
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    settings = repo / ".claude" / "settings.json"
    plugin_state = {
        "enabledPlugins": {"superpowers@claude-plugins-official": True, "figma@claude-plugins-official": False},
        "extraKnownMarketplaces": {"claude-plugins-official": {"source": {"source": "github", "repo": "anthropics/claude-plugins-official"}}},
    }
    settings.write_text(json.dumps({"model": "opus", **plugin_state}), encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_harness_cfg(repo, fake_agent_tools, mode="acceptEdits"), cat, project_type="unknown")
    run_plan(plan)
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["permissions"]["defaultMode"] == "acceptEdits"
    assert data["enabledPlugins"] == plugin_state["enabledPlugins"]
    assert data["extraKnownMarketplaces"] == plugin_state["extraKnownMarketplaces"]


def test_harness_and_permissions_use_explicit_suffixed_settings_file(fake_agent_tools, tmp_path):
    """A suffixed Claude settings_path is a shared settings file, not a directory target."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".claude" / "settings.jsonc"
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "ship_delegator": {"enabled": False},
            "harness": {
                "kind": "claude-code",
                "settings_path": str(settings),
                "mode": "acceptEdits",
                "hook_bridge": {"enabled": False},
            },
            "permissions": {
                "kind": "claude-code",
                "settings_path": str(settings),
                "tools": ["git"],
                "deny": [],
                "ask": [],
            },
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    kinds = {a.kind for a in plan.actions}
    assert {"apply_harness", "provision_permissions"} <= kinds

    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    assert settings.is_file()
    assert not (settings / "settings.json").exists()
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["permissions"]["defaultMode"] == "acceptEdits"
    assert data["permissions"]["allow"] == ["Bash(git:*)"]
    drift = detect(plan)
    assert not [i for i in drift.items if i.category in {"harness", "permissions"}]


def test_harness_conflicting_value_backed_up(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    settings = repo / ".claude" / "settings.json"
    settings.write_text(
        json.dumps({"permissions": {"defaultMode": "default"}}), encoding="utf-8"
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_harness_cfg(repo, fake_agent_tools, mode="acceptEdits"), cat, project_type="unknown")
    report = run_plan(plan)  # default on_conflict=backup
    assert not report.errors
    # a differing prior value is backed up before converging (default policy = backup)
    assert any(p.name.startswith("settings.json.rig-bak-") for p in (repo / ".claude").iterdir())
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["permissions"]["defaultMode"] == "acceptEdits"


def test_harness_drift_missing_then_modified(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_harness_cfg(repo, fake_agent_tools, mode="acceptEdits"), cat, project_type="unknown")

    # nothing on disk yet → missing
    rep = detect(plan)
    miss = [d for d in rep.by_direction("missing") if d.category == "harness"]
    assert miss, "expected a missing harness drift item"

    # apply, then flip the value → modified drift
    run_plan(plan)
    settings = repo / ".claude" / "settings.json"
    d = json.loads(settings.read_text(encoding="utf-8"))
    d["permissions"]["defaultMode"] = "default"
    settings.write_text(json.dumps(d), encoding="utf-8")
    rep2 = detect(plan)
    mod = [x for x in rep2.by_direction("modified") if x.category == "harness"]
    assert mod and "acceptEdits" in mod[0].detail

    # re-apply converges, back in sync
    run_plan(plan)
    rep3 = detect(plan)
    assert not [x for x in rep3.items if x.category == "harness"]
