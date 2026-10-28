"""Hook bridge, codex fold-in and opencode tests."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
import pytest
from riglib.actions import run_plan
from riglib.actions.hook_bridge import hook_bridge_entries
from riglib.actions.hook_bridge_codex import codex_hook_bridge_block
from riglib import tg_ctl as tg_ctl_module
from riglib.catalog import Catalog
from riglib.config import LoadedConfig
from riglib.drift import detect
from riglib.plan import build
from tests.actions_drift_helpers import (
    _bridge_cfg,
    _bridge_results,
    _codex_bridge_action,
    _codex_bridge_action_from,
    _codex_bridge_plan,
    _git_init,
    _plant_tg_ctl_script,
    _read_toml,
)


def test_hook_bridge_registers_dispatcher_in_settings(fake_agent_tools, tmp_path):
    """Apply wires PreToolUse (Bash + writes + Agent|Task + Skill + Monitor + EnterWorktree +
    Workflow), PostToolUse and Stop hooks."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".claude" / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_bridge_cfg(repo, fake_agent_tools, settings_path=settings), cat, project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    assert _bridge_results(report), "no register_hook_bridge action ran"

    data = json.loads(settings.read_text())
    hooks = data["hooks"]
    # PreToolUse: Bash + file-write tools + subagent dispatch + skill invocation + Monitor
    # calls + worktree entry all → cc_hook_bridge
    pre = hooks["PreToolUse"]
    matchers = {b["matcher"] for b in pre}
    assert "Bash" in matchers
    assert "Edit|Write|MultiEdit|NotebookEdit" in matchers
    assert "Agent|Task" in matchers
    assert "Skill" in matchers
    assert "Monitor" in matchers
    assert "EnterWorktree" in matchers
    assert "Workflow" in matchers
    for b in pre:
        cmd = b["hooks"][0]["command"]
        assert "cc_hook_bridge PreToolUse" in cmd
        # PYTHONPATH anchors on the agent-tools checkout lib/
        assert str(fake_agent_tools / "lib") in cmd
    # PostToolUse: the write-tool matcher → the post-write FEEDBACK point (lint-on-write,
    # format-on-write). Without this entry the whole agent-tools post-write point is dead
    # (agent-tools#160) — pin it so it can't silently regress.
    post = hooks["PostToolUse"]
    assert len(post) == 1
    assert post[0]["matcher"] == "Edit|Write|MultiEdit|NotebookEdit"
    post_cmd = post[0]["hooks"][0]["command"]
    assert "cc_hook_bridge PostToolUse" in post_cmd
    assert str(fake_agent_tools / "lib") in post_cmd
    # Stop: one block, no matcher (match-all), → cc_hook_bridge Stop
    stop = hooks["Stop"]
    assert len(stop) == 1 and "matcher" not in stop[0]
    assert "cc_hook_bridge Stop" in stop[0]["hooks"][0]["command"]


def test_hook_bridge_registers_explicit_non_json_suffix_settings_file(fake_agent_tools, tmp_path):
    """An explicit Claude settings_path with a suffix is a file path, not a directory."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".claude" / "settings.jsonc"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_bridge_cfg(repo, fake_agent_tools, settings_path=settings), cat, project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    assert settings.is_file()
    assert not (settings / "settings.json").exists()
    data = json.loads(settings.read_text())
    assert "cc_hook_bridge PreToolUse" in data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    after = detect(plan)
    assert not any(i.item == "hook-bridge" for i in after.items), [i.detail for i in after.items]


def test_codex_hook_bridge_registers_dispatcher_in_config_toml(fake_agent_tools, tmp_path):
    """Apply wires Codex TOML hooks while preserving unrelated config."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    settings.write_text('model = "gpt-5"\n\n[profiles.default]\nmodel = "gpt-5"\n', encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    assert _bridge_results(report), "no register_hook_bridge action ran"

    text = settings.read_text(encoding="utf-8")
    assert 'model = "gpt-5"' in text
    assert "[profiles.default]" in text
    data = _read_toml(settings)
    hooks = data["hooks"]
    pre = hooks["PreToolUse"]
    pre_by_matcher = {entry["matcher"]: entry["hooks"][0]["command"] for entry in pre}
    assert "codex_hook_bridge PreToolUse" in pre_by_matcher["Bash"]
    assert "codex_hook_bridge PreToolUse" in pre_by_matcher["apply_patch"]
    assert str(fake_agent_tools / "lib") in pre_by_matcher["Bash"]
    post = hooks["PostToolUse"]
    assert post[0]["matcher"] == "apply_patch"
    assert "codex_hook_bridge PostToolUse" in post[0]["hooks"][0]["command"]
    stop = hooks["Stop"]
    assert "matcher" not in stop[0]
    assert "codex_hook_bridge Stop" in stop[0]["hooks"][0]["command"]


def test_hook_bridge_entries_appends_extra_stop_hooks(tmp_path):
    """extra_stop_hooks (codex only) appends AFTER the bridge's own Stop entry."""
    extra = "/abs/path/tg-ctl codex-usage-hook"
    action = _codex_bridge_action(tmp_path, extra_stop_hooks=[extra])
    entries = hook_bridge_entries(action)
    stop = entries["Stop"]
    assert len(stop) == 2
    assert stop[0][0] == "" and "codex_hook_bridge Stop" in stop[0][1]
    assert stop[1] == ("", extra)


def test_hook_bridge_entries_no_extra_stop_hooks_is_unchanged():
    """Absent/empty extra_stop_hooks regresses to exactly the original single Stop entry."""
    for options_extra in (None, []):
        action = _codex_bridge_action(Path("/lib"), extra_stop_hooks=options_extra)
        stop = hook_bridge_entries(action)["Stop"]
        assert len(stop) == 1
        assert stop[0][0] == "" and "codex_hook_bridge Stop" in stop[0][1]


def test_codex_hook_bridge_block_renders_two_stop_entries(tmp_path):
    """The rendered TOML Stop array carries both hook command blocks, like PreToolUse does."""
    extra = "/abs/path/tg-ctl codex-usage-hook"
    action = _codex_bridge_action(tmp_path, extra_stop_hooks=[extra])
    block = codex_hook_bridge_block(action, include_table_header=True)
    stop_line = next(line for line in block.splitlines() if line.startswith("Stop ="))
    assert stop_line.count('type = "command"') == 2
    assert "codex_hook_bridge Stop" in stop_line
    assert extra in stop_line


def test_plan_codex_hook_bridge_folds_in_tg_ctl_default(fake_agent_tools, tmp_path):
    """With no tg_ctl: block (default) and tg-ctl actually installed, the bridge action gets
    a `bun <tg_ctl_path> codex-usage-hook` command (not the script invoked directly)."""
    plan, _, bun_path = _codex_bridge_plan(fake_agent_tools, tmp_path)
    home = Path(os.path.expanduser("~"))
    script = home / ".files" / "bin" / "tg-ctl"
    [cmd] = _codex_bridge_action_from(plan).options.get("extra_stop_hooks")
    # split rather than a raw string comparison — the real command is per-token shlex-quoted,
    # so this stays correct even if tmp_path ever contains spaces/shell metacharacters.
    assert shlex.split(cmd) == [str(bun_path), str(script), tg_ctl_module.CODEX_USAGE_HOOK_SUBCOMMAND]


def test_plan_codex_hook_bridge_no_fold_when_tg_ctl_not_installed(fake_agent_tools, tmp_path):
    """No tg-ctl script on disk (never installed / non-macOS machine) → fail-closed, no fold-in.

    Without this guard, a machine that never provisioned tg-ctl would still get a Codex Stop
    hook pointing at a binary that does not exist, firing (and failing) every session end.
    """
    plan, _, _bun = _codex_bridge_plan(
        fake_agent_tools, tmp_path, install_tg_ctl_script=False, install_bun=False,
    )
    assert not _codex_bridge_action_from(plan).options.get("extra_stop_hooks")


def test_plan_codex_hook_bridge_no_fold_when_bun_not_installed(fake_agent_tools, tmp_path, monkeypatch):
    """tg-ctl script present but bun itself absent → fail-closed, no fold-in: checking only the
    script is not enough.

    Mocks ``which bun`` off so this test's verdict does not depend on whether the machine
    running it happens to have a real bun on PATH.
    """
    monkeypatch.setattr(tg_ctl_module.shutil, "which", lambda name: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    home = Path(os.path.expanduser("~"))
    _plant_tg_ctl_script(home / ".files" / "bin" / "tg-ctl")  # script present, bun NOT planted
    settings = repo / ".codex" / "config.toml"
    cfg = _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                       hook_bridge={"enabled": True})
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(cfg, cat, project_type="unknown")
    assert not _codex_bridge_action_from(plan).options.get("extra_stop_hooks")


def test_plan_codex_hook_bridge_no_fold_when_tg_ctl_disabled(fake_agent_tools, tmp_path):
    """tg_ctl.enabled: false → the tg_ctl area itself is off, so nothing is folded in."""
    plan, _, _bun = _codex_bridge_plan(fake_agent_tools, tmp_path, tg_ctl={"enabled": False})
    assert not _codex_bridge_action_from(plan).options.get("extra_stop_hooks")


def test_plan_codex_hook_bridge_folds_in_regardless_of_boot(fake_agent_tools, tmp_path):
    """tg_ctl.boot: false still folds in — the fold-in only needs the tg-ctl script + bun on
    disk, not the daemon itself set to auto-start."""
    plan, _, _bun = _codex_bridge_plan(fake_agent_tools, tmp_path, tg_ctl={"boot": False})
    assert _codex_bridge_action_from(plan).options.get("extra_stop_hooks")


def test_plan_codex_hook_bridge_no_fold_when_codex_usage_hook_disabled(fake_agent_tools, tmp_path):
    """tg_ctl.codex_usage_hook: false opts out of ONLY the fold-in; the daemon stays enabled."""
    plan, _, _bun = _codex_bridge_plan(fake_agent_tools, tmp_path, tg_ctl={"codex_usage_hook": False})
    assert not _codex_bridge_action_from(plan).options.get("extra_stop_hooks")


def test_plan_non_codex_hook_bridge_never_folds_in_tg_ctl(fake_agent_tools, tmp_path):
    """A non-codex harness (claude-code) with a fully-configured tg_ctl block gets NO
    extra_stop_hooks — the fold-in is codex-only, documented as "no effect on non-codex
    harnesses" (docs/config-schema.md)."""
    plan, _, _bun = _codex_bridge_plan(
        fake_agent_tools, tmp_path, kind="claude-code",
        tg_ctl={"enabled": True, "codex_usage_hook": True},
    )
    assert not _codex_bridge_action_from(plan).options.get("extra_stop_hooks")


def test_plan_codex_hook_bridge_uses_custom_tg_ctl_path(fake_agent_tools, tmp_path):
    """tg_ctl.tg_ctl_path overrides the default binary path used in the folded-in command."""
    custom = tmp_path / "custom" / "tg-ctl"
    _plant_tg_ctl_script(custom)
    plan, _, bun_path = _codex_bridge_plan(
        fake_agent_tools, tmp_path, install_tg_ctl_script=False, install_bun=True,
        tg_ctl={"tg_ctl_path": str(custom)},
    )
    [cmd] = _codex_bridge_action_from(plan).options.get("extra_stop_hooks")
    assert shlex.split(cmd) == [str(bun_path), str(custom), tg_ctl_module.CODEX_USAGE_HOOK_SUBCOMMAND]


def test_codex_hook_bridge_with_tg_ctl_fold_in_idempotent_reapply(fake_agent_tools, tmp_path):
    """A second apply with the tg-ctl Stop hook folded in is a no-op (drift-free)."""
    plan, settings, _bun = _codex_bridge_plan(fake_agent_tools, tmp_path)
    first = run_plan(plan)
    assert not first.errors, [r.detail for r in first.errors]
    data = _read_toml(settings)
    stop = data["hooks"]["Stop"]
    assert len(stop) == 2
    assert "codex_hook_bridge Stop" in stop[0]["hooks"][0]["command"]
    assert tg_ctl_module.CODEX_USAGE_HOOK_SUBCOMMAND in stop[1]["hooks"][0]["command"]

    before = detect(plan)
    assert not any(i.item == "hook-bridge" for i in before.items), [i.detail for i in before.items]

    second = run_plan(plan)
    res = _bridge_results(second)
    assert res and all(r.status == "skipped" for r in res), [r.detail for r in res]

    after = detect(plan)
    assert not any(i.item == "hook-bridge" for i in after.items), [i.detail for i in after.items]


def test_opencode_hook_bridge_links_plugin(fake_agent_tools, tmp_path):
    """Apply wires opencode by symlinking the bridge plugin into the plugin directory."""
    repo = tmp_path / "repo"
    repo.mkdir()
    plugin = repo / "opencode" / "plugins" / "zz-agent-tools-hook-bridge.js"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(
            repo,
            fake_agent_tools,
            settings_path=plugin,
            kind="opencode",
            hook_bridge={"enabled": True},
        ),
        cat,
        project_type="unknown",
    )
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    assert _bridge_results(report), "no register_hook_bridge action ran"
    assert plugin.is_symlink()
    assert plugin.resolve() == (fake_agent_tools / "lib" / "opencode_hook_bridge" / "plugin.js")

    after = detect(plan)
    assert not any(i.item == "hook-bridge" for i in after.items), [i.detail for i in after.items]


def test_opencode_hook_bridge_custom_hook_target_writes_wrapper(fake_agent_tools, tmp_path):
    """A custom agent_hooks.target keeps the primary opencode bridge wired to that descriptor dir."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    plugin = repo / ".opencode" / "plugins" / "zz-agent-tools-hook-bridge.js"
    custom_hooks = repo / "custom-hooks"
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False},
            "agent_hooks": {"all": True, "target": str(custom_hooks)},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "harness": {"kind": "opencode", "auto_mode": True, "hook_bridge": {"enabled": True}},
        },
        repo_root=repo,
        repo_path=repo / "rig.yaml",
        layers=[f"repo:{repo / 'rig.yaml'}"],
    )
    plan = build(cfg, cat, project_type="unknown")

    report = run_plan(plan)

    assert not report.errors, [r.detail for r in report.errors]
    assert _bridge_results(report), "no register_hook_bridge action ran"
    assert plugin.is_file()
    assert not plugin.is_symlink()
    text = plugin.read_text(encoding="utf-8")
    assert f'process.env.OPENCODE_HOOKS_DIR = "{custom_hooks}"' in text
    assert str((fake_agent_tools / "lib" / "opencode_hook_bridge" / "plugin.js").resolve().as_uri()) in text
    assert detect(plan).in_sync


def test_opencode_hook_bridge_custom_wrapper_drift_is_repaired(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    plugin = repo / ".opencode" / "plugins" / "zz-agent-tools-hook-bridge.js"
    custom_hooks = repo / "custom-hooks"
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False},
            "agent_hooks": {"all": True, "target": str(custom_hooks)},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "harness": {"kind": "opencode", "auto_mode": True, "hook_bridge": {"enabled": True}},
        },
        repo_root=repo,
        repo_path=repo / "rig.yaml",
        layers=[f"repo:{repo / 'rig.yaml'}"],
    )
    plan = build(cfg, cat, project_type="unknown")
    first = run_plan(plan)
    assert not first.errors, [r.detail for r in first.errors]
    plugin.write_text("// stale wrapper\n", encoding="utf-8")

    drift = detect(plan)

    assert any(
        i.item == "hook-bridge"
        and i.direction == "modified"
        and "wrapper differs from config" in i.detail
        for i in drift.items
    ), [i.detail for i in drift.items]

    second = run_plan(plan)

    assert not second.errors, [r.detail for r in second.errors]
    assert f'process.env.OPENCODE_HOOKS_DIR = "{custom_hooks}"' in plugin.read_text(encoding="utf-8")
    assert detect(plan).in_sync


def test_opencode_hook_bridge_custom_wrapper_skip_leaves_existing_file(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    plugin = repo / ".opencode" / "plugins" / "zz-agent-tools-hook-bridge.js"
    plugin.parent.mkdir(parents=True)
    plugin.write_text("// user-managed plugin\n", encoding="utf-8")
    custom_hooks = repo / "custom-hooks"
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "defaults": {"on_conflict": "skip"},
            "skills": {"enabled": False},
            "agent_hooks": {"all": True, "target": str(custom_hooks)},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "harness": {"kind": "opencode", "auto_mode": True, "hook_bridge": {"enabled": True}},
        },
        repo_root=repo,
        repo_path=repo / "rig.yaml",
        layers=[f"repo:{repo / 'rig.yaml'}"],
    )
    plan = build(cfg, cat, project_type="unknown")

    report = run_plan(plan)

    res = _bridge_results(report)
    assert res and all(r.status == "skipped" for r in res), [r.detail for r in res]
    assert plugin.read_text(encoding="utf-8") == "// user-managed plugin\n"
    assert not plugin.is_symlink()


def test_opencode_hook_bridge_custom_wrapper_is_importable(fake_agent_tools, tmp_path):
    """The generated wrapper must match the plugin module export contract."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    plugin = repo / ".opencode" / "plugins" / "zz-agent-tools-hook-bridge.js"
    custom_hooks = repo / "custom-hooks"
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False},
            "agent_hooks": {"all": True, "target": str(custom_hooks)},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "harness": {"kind": "opencode", "auto_mode": True, "hook_bridge": {"enabled": True}},
        },
        repo_root=repo,
        repo_path=repo / "rig.yaml",
        layers=[f"repo:{repo / 'rig.yaml'}"],
    )
    report = run_plan(build(cfg, cat, project_type="unknown"))
    assert not report.errors, [r.detail for r in report.errors]

    script = (
        "const mod = await import(process.argv[1]);\n"
        "if (typeof mod.AgentToolsHookBridge !== 'function') process.exit(2);\n"
        "const plugin = await mod.AgentToolsHookBridge();\n"
        f"if (plugin.hooksDirAtImport !== {json.dumps(str(custom_hooks))}) {{\n"
        "  console.error(`hooks dir not visible at import: ${plugin.hooksDirAtImport}`);\n"
        "  process.exit(3);\n"
        "}\n"
    )
    res = subprocess.run(
        [node, "--input-type=module", "-e", script, plugin.resolve().as_uri()],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert res.returncode == 0, res.stderr


def test_opencode_hook_bridge_default_path_is_repo_local_ignored_and_in_sync(fake_agent_tools, tmp_path):
    """The shipped opencode default is a repo-local ordered plugin symlink that rig git-ignores."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    cat = Catalog.scan(str(fake_agent_tools))
    repo_cfg = repo / "rig.yaml"
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False},
            "agent_hooks": {"all": True},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "harness": {"kind": "opencode", "auto_mode": True, "hook_bridge": {"enabled": True}},
        },
        repo_root=repo,
        repo_path=repo_cfg,
        layers=[f"repo:{repo_cfg}"],
    )
    plan = build(cfg, cat, project_type="unknown")
    plugin = repo / ".opencode" / "plugins" / "zz-agent-tools-hook-bridge.js"

    report = run_plan(plan)

    assert not report.errors, [r.detail for r in report.errors]
    assert plugin.is_symlink()
    assert plugin.resolve() == (fake_agent_tools / "lib" / "opencode_hook_bridge" / "plugin.js")
    exclude = repo / ".git" / "info" / "exclude"
    exclude_text = exclude.read_text(encoding="utf-8")
    assert "/.opencode/plugins/zz-agent-tools-hook-bridge.js" in exclude_text
    assert "/.opencode/plugins/zz-agent-tools-hook-bridge.js.rig-bak-*" in exclude_text
    assert detect(plan).in_sync


def test_opencode_hook_bridge_drift_when_repo_local_plugin_not_ignored(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    plugin = repo / ".opencode" / "plugins" / "zz-agent-tools-hook-bridge.js"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(
            repo,
            fake_agent_tools,
            settings_path=plugin,
            kind="opencode",
            hook_bridge={"enabled": True},
        ),
        cat,
        project_type="unknown",
    )
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    (repo / ".git" / "info" / "exclude").write_text("", encoding="utf-8")

    before = detect(plan)

    assert any(
        i.item == "hook-bridge" and i.direction == "missing" and "git-ignored" in i.detail
        for i in before.items
    )
    repaired = run_plan(plan)
    assert not repaired.errors, [r.detail for r in repaired.errors]
    assert detect(plan).in_sync


def test_opencode_hook_bridge_removes_legacy_global_managed_symlink(fake_agent_tools, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    old = xdg / "opencode" / "plugins" / "agent-tools-hook-bridge.js"
    old.parent.mkdir(parents=True)
    old.symlink_to(fake_agent_tools / "lib" / "opencode_hook_bridge" / "plugin.js")
    plugin = repo / ".opencode" / "plugins" / "zz-agent-tools-hook-bridge.js"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(
            repo,
            fake_agent_tools,
            settings_path=plugin,
            kind="opencode",
            hook_bridge={"enabled": True},
        ),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)

    assert not report.errors, [r.detail for r in report.errors]
    assert plugin.is_symlink()
    assert not old.exists()
    assert "removed legacy global opencode plugin" in _bridge_results(report)[0].detail


def test_opencode_hook_bridge_errors_when_legacy_cleanup_fails(fake_agent_tools, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    old = xdg / "opencode" / "plugins" / "agent-tools-hook-bridge.js"
    old.parent.mkdir(parents=True)
    old.symlink_to(fake_agent_tools / "lib" / "opencode_hook_bridge" / "plugin.js")
    plugin = repo / ".opencode" / "plugins" / "zz-agent-tools-hook-bridge.js"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(
            repo,
            fake_agent_tools,
            settings_path=plugin,
            kind="opencode",
            hook_bridge={"enabled": True},
        ),
        cat,
        project_type="unknown",
    )
    monkeypatch.setattr(
        "riglib.actions.hook_bridge_wrappers._remove_legacy_opencode_bridge_symlink",
        lambda _plugin_path, _dest: (
            False,
            f"legacy global opencode plugin still present (could not remove {old}: denied)",
        ),
    )

    report = run_plan(plan)

    assert report.errors
    result = _bridge_results(report)[0]
    assert result.status == "error"
    assert "legacy global opencode plugin still present" in result.detail


def test_opencode_hook_bridge_drift_detects_legacy_global_symlink(fake_agent_tools, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    old = xdg / "opencode" / "plugins" / "agent-tools-hook-bridge.js"
    old.parent.mkdir(parents=True)
    old.symlink_to(fake_agent_tools / "lib" / "opencode_hook_bridge" / "plugin.js")
    plugin = repo / ".opencode" / "plugins" / "zz-agent-tools-hook-bridge.js"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(
            repo,
            fake_agent_tools,
            settings_path=plugin,
            kind="opencode",
            hook_bridge={"enabled": True},
        ),
        cat,
        project_type="unknown",
    )
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    old.symlink_to(fake_agent_tools / "lib" / "opencode_hook_bridge" / "plugin.js")

    before = detect(plan)

    assert any(
        i.item == "hook-bridge" and i.direction == "modified" and "legacy global" in i.detail
        for i in before.items
    )
    repaired = run_plan(plan)
    assert not repaired.errors, [r.detail for r in repaired.errors]
    assert not old.exists()
    assert detect(plan).in_sync


def test_opencode_hook_bridge_removes_legacy_symlink_to_prior_checkout(fake_agent_tools, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    prior = tmp_path / "old-agent-tools" / "lib" / "opencode_hook_bridge" / "plugin.js"
    prior.parent.mkdir(parents=True)
    prior.write_text("export const Old = async () => ({});\n", encoding="utf-8")
    old = xdg / "opencode" / "plugins" / "agent-tools-hook-bridge.js"
    old.parent.mkdir(parents=True)
    old.symlink_to(prior)
    plugin = repo / ".opencode" / "plugins" / "zz-agent-tools-hook-bridge.js"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(
            repo,
            fake_agent_tools,
            settings_path=plugin,
            kind="opencode",
            hook_bridge={"enabled": True},
        ),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)

    assert not report.errors, [r.detail for r in report.errors]
    assert not old.exists()
    assert plugin.is_symlink()
    old.symlink_to(prior)
    before = detect(plan)
    assert any(i.item == "hook-bridge" and "legacy global" in i.detail for i in before.items)


def test_opencode_hook_bridge_leaves_unrelated_legacy_global_symlink(fake_agent_tools, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    other = tmp_path / "other-plugin.js"
    other.write_text("export const Other = async () => ({});\n", encoding="utf-8")
    old = xdg / "opencode" / "plugins" / "agent-tools-hook-bridge.js"
    old.parent.mkdir(parents=True)
    old.symlink_to(other)
    plugin = repo / ".opencode" / "plugins" / "zz-agent-tools-hook-bridge.js"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(
            repo,
            fake_agent_tools,
            settings_path=plugin,
            kind="opencode",
            hook_bridge={"enabled": True},
        ),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)

    assert not report.errors, [r.detail for r in report.errors]
    assert old.is_symlink()
    assert old.resolve() == other
    assert not any("legacy global" in i.detail for i in detect(plan).items)


def test_opencode_hook_bridge_legacy_settings_path_does_not_delete_itself(fake_agent_tools, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    plugin = xdg / "opencode" / "plugins" / "agent-tools-hook-bridge.js"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(
            repo,
            fake_agent_tools,
            settings_path=plugin,
            kind="opencode",
            hook_bridge={"enabled": True},
        ),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)

    assert not report.errors, [r.detail for r in report.errors]
    assert plugin.is_symlink()
    assert plugin.resolve() == (fake_agent_tools / "lib" / "opencode_hook_bridge" / "plugin.js")


def test_opencode_hook_bridge_backs_up_existing_plugin_file(fake_agent_tools, tmp_path):
    """A user file at the opencode plugin path is backed up before rig links the bridge."""
    repo = tmp_path / "repo"
    repo.mkdir()
    plugin = repo / "opencode" / "plugins" / "zz-agent-tools-hook-bridge.js"
    plugin.parent.mkdir(parents=True)
    plugin.write_text("// user plugin\n", encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(
            repo,
            fake_agent_tools,
            settings_path=plugin,
            kind="opencode",
            hook_bridge={"enabled": True},
            on_conflict="backup",
        ),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)

    assert not report.errors, [r.detail for r in report.errors]
    result = _bridge_results(report)[0]
    assert result.status == "backed_up"
    assert plugin.is_symlink()
    backups = list(plugin.parent.glob("zz-agent-tools-hook-bridge.js.rig-bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "// user plugin\n"
    assert detect(plan).in_sync


def test_opencode_hook_bridge_overwrites_existing_plugin_directory(fake_agent_tools, tmp_path):
    """on_conflict=overwrite removes a directory collision before linking the opencode plugin."""
    repo = tmp_path / "repo"
    repo.mkdir()
    plugin = repo / "opencode" / "plugins" / "zz-agent-tools-hook-bridge.js"
    plugin.mkdir(parents=True)
    (plugin / "old.js").write_text("// stale directory content\n", encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(
            repo,
            fake_agent_tools,
            settings_path=plugin,
            kind="opencode",
            hook_bridge={"enabled": True},
            on_conflict="overwrite",
        ),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)

    assert not report.errors, [r.detail for r in report.errors]
    result = _bridge_results(report)[0]
    assert result.status == "updated"
    assert plugin.is_symlink()
    assert plugin.resolve() == (fake_agent_tools / "lib" / "opencode_hook_bridge" / "plugin.js")
    assert not (plugin / "old.js").exists()
    assert detect(plan).in_sync


def test_opencode_hook_bridge_repoints_existing_plugin_symlink(fake_agent_tools, tmp_path):
    """A stale opencode plugin symlink is re-pointed without backup noise."""
    repo = tmp_path / "repo"
    repo.mkdir()
    plugin = repo / "opencode" / "plugins" / "zz-agent-tools-hook-bridge.js"
    plugin.parent.mkdir(parents=True)
    wrong_dest = repo / "wrong-plugin.js"
    wrong_dest.write_text("// wrong bridge\n", encoding="utf-8")
    plugin.symlink_to(wrong_dest)
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(
            repo,
            fake_agent_tools,
            settings_path=plugin,
            kind="opencode",
            hook_bridge={"enabled": True},
        ),
        cat,
        project_type="unknown",
    )

    before = detect(plan)
    assert any(i.item == "hook-bridge" and i.direction == "modified" for i in before.items)

    report = run_plan(plan)

    assert not report.errors, [r.detail for r in report.errors]
    result = _bridge_results(report)[0]
    assert result.status == "updated"
    assert plugin.is_symlink()
    assert plugin.resolve() == (fake_agent_tools / "lib" / "opencode_hook_bridge" / "plugin.js")
    assert wrong_dest.read_text(encoding="utf-8") == "// wrong bridge\n"
    assert detect(plan).in_sync


def test_opencode_hook_bridge_suffixless_settings_path_uses_ordered_plugin_name(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    settings_dir = repo / ".opencode" / "plugins"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(
            repo,
            fake_agent_tools,
            settings_path=settings_dir,
            kind="opencode",
            hook_bridge={"enabled": True},
        ),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)

    plugin = settings_dir / "zz-agent-tools-hook-bridge.js"
    assert not report.errors, [r.detail for r in report.errors]
    assert plugin.is_symlink()
    assert plugin.resolve() == (fake_agent_tools / "lib" / "opencode_hook_bridge" / "plugin.js")
    assert detect(plan).in_sync
