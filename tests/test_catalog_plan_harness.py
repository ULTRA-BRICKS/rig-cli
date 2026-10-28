"""Harness provisioning plans and hook-bridge emission."""

from __future__ import annotations

from pathlib import Path

from riglib.catalog import Catalog
from riglib.config import LoadedConfig
from riglib.plan import build, resolve_category_target, resolve_category_targets
from tests.catalog_plan_helpers import _bridge_action, _cfg, _harness_action


# ── harness (auto-mode / permission provisioning) ─────────────────────────────────
def test_plan_no_harness_block_no_action(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"skills": {"enabled": False}}, tmp_path)  # no harness key at all
    plan = build(cfg, cat, project_type="unknown")
    assert _harness_action(plan) is None


def test_plan_harness_auto_mode_on_maps_to_user_auto(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"skills": {"enabled": False}, "harness": {"kind": "claude-code", "auto_mode": True}},
        tmp_path,
    )
    a = _harness_action(build(cfg, cat, project_type="unknown"))
    assert a is not None
    assert a.options["mode_value"] == "auto"
    assert a.options["auto_mode"] is True
    # `auto` is honored only from the user's machine settings (CC strips it from project
    # scope), so the default target is ~/.claude/settings.json, NOT the repo.
    import os

    assert a.target == Path(os.path.expanduser("~/.claude/settings.json"))


def test_plan_harness_nonauto_mode_writes_project(fake_agent_tools, tmp_path):
    # a non-auto mode IS committable at project scope → target stays the repo settings file
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"skills": {"enabled": False},
         "harness": {"kind": "claude-code", "mode": "acceptEdits"}},
        tmp_path,
    )
    a = _harness_action(build(cfg, cat, project_type="unknown"))
    assert a is not None
    assert a.options["mode_value"] == "acceptEdits"
    assert a.target == (tmp_path / ".claude" / "settings.json")


def test_plan_harness_auto_mode_off_maps_to_default(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"skills": {"enabled": False}, "harness": {"auto_mode": False}},
        tmp_path,
    )
    a = _harness_action(build(cfg, cat, project_type="unknown"))
    assert a is not None and a.options["mode_value"] == "default"


def test_plan_harness_explicit_mode_overrides_auto_mapping(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"skills": {"enabled": False}, "harness": {"auto_mode": True, "mode": "acceptEdits"}},
        tmp_path,
    )
    a = _harness_action(build(cfg, cat, project_type="unknown"))
    assert a is not None and a.options["mode_value"] == "acceptEdits"


def test_plan_harness_disabled_no_action(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"skills": {"enabled": False}, "harness": {"enabled": False, "auto_mode": True}},
        tmp_path,
    )
    assert _harness_action(build(cfg, cat, project_type="unknown")) is None


def test_plan_harness_custom_settings_path(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"skills": {"enabled": False},
         "harness": {"auto_mode": True, "settings_path": "~/.claude/settings.json"}},
        tmp_path,
    )
    a = _harness_action(build(cfg, cat, project_type="unknown"))
    import os

    assert a is not None
    assert a.target == Path(os.path.expanduser("~/.claude/settings.json"))


# ── hook bridge (agents-hooks/v1 → CC settings.json) ──────────────────────────────
def test_plan_hook_bridge_emitted_with_harness(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"harness": {"kind": "claude-code", "auto_mode": True}}, tmp_path)
    a = _bridge_action(build(cfg, cat, project_type="unknown"))
    assert a is not None
    # anchored on the resolved agent-tools checkout's lib/ for PYTHONPATH
    assert a.options["lib_dir"] == str(fake_agent_tools / "lib")
    assert a.target == (tmp_path / ".claude" / "settings.json")


def test_plan_claude_agent_hooks_default_to_claude_hooks(fake_agent_tools, tmp_path, monkeypatch):
    import os

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {
            "skills": {"enabled": False},
            "agent_hooks": {"all": True},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "harness": {"kind": "claude-code"},
        },
        tmp_path,
    )

    plan = build(cfg, cat, project_type="unknown")

    hook_actions = [a for a in plan.actions if a.kind == "install_agent_hook"]
    assert hook_actions
    assert {a.target for a in hook_actions} == {Path(os.path.expanduser("~/.claude/hooks"))}


def test_plan_codex_hook_bridge_emitted_with_harness(fake_agent_tools, tmp_path, monkeypatch):
    import os

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"harness": {"kind": "codex", "hook_bridge": {"enabled": True}}}, tmp_path)
    a = _bridge_action(build(cfg, cat, project_type="unknown"))
    assert a is not None
    assert a.options["kind"] == "codex"
    assert a.options["module"] == "codex_hook_bridge"
    assert a.options["lib_dir"] == str(fake_agent_tools / "lib")
    assert a.target == Path(os.path.expanduser("~/.codex/config.toml"))


def test_plan_codex_surfaces_ignore_ambient_codex_home(fake_agent_tools, tmp_path, monkeypatch):
    home = tmp_path / "home"
    ambient_codex_home = tmp_path / "ambient-codex-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(ambient_codex_home))
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {
            "harness": {"kind": "codex", "hook_bridge": {"enabled": True}},
            "agent_hooks": {"all": True},
        },
        tmp_path,
    )

    plan = build(cfg, cat, project_type="unknown")

    skill_targets = {a.target for a in plan.actions if a.kind == "link_skill_harness"}
    assert any(str(target).startswith(str(home / ".codex" / "skills")) for target in skill_targets)
    hook_targets = {a.target for a in plan.actions if a.kind == "install_agent_hook"}
    assert hook_targets == {home / ".codex" / "hooks"}
    assert resolve_category_target(cfg, "agent_hooks") == home / ".codex" / "hooks"
    assert resolve_category_targets(cfg, "agent_hooks") == [home / ".codex" / "hooks"]
    bridge = _bridge_action(plan)
    assert bridge is not None
    assert bridge.target == home / ".codex" / "config.toml"
    assert "hooks_dir" not in bridge.options


def test_plan_codex_surfaces_follow_explicit_rig_codex_home(fake_agent_tools, tmp_path, monkeypatch):
    from riglib.actions.hook_bridge import hook_bridge_entries

    home = tmp_path / "home"
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("RIG_CODEX_HOME", str(codex_home))
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {
            "harness": {"kind": "codex", "hook_bridge": {"enabled": True}},
            "agent_hooks": {"all": True},
        },
        tmp_path,
    )

    plan = build(cfg, cat, project_type="unknown")

    skill_targets = {a.target for a in plan.actions if a.kind == "link_skill_harness"}
    assert any(str(target).startswith(str(codex_home / "skills")) for target in skill_targets)
    hook_targets = {a.target for a in plan.actions if a.kind == "install_agent_hook"}
    assert hook_targets == {codex_home / "hooks"}
    assert resolve_category_target(cfg, "agent_hooks") == codex_home / "hooks"
    assert resolve_category_targets(cfg, "agent_hooks") == [codex_home / "hooks"]
    bridge = _bridge_action(plan)
    assert bridge is not None
    assert bridge.target == codex_home / "config.toml"
    assert bridge.options["hooks_dir"] == str(codex_home / "hooks")
    assert "CODEX_HOOKS_DIR=" in str(hook_bridge_entries(bridge))


def test_plan_claude_primary_additional_codex_provisions_codex_surfaces(
    fake_agent_tools, tmp_path, monkeypatch
):
    import os

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {
            "harness": {
                "kind": "claude-code",
                "kinds": ["codex"],
                "settings_path": "~/.claude/settings.json",
                "hook_bridge": {"enabled": True},
            },
            "agent_hooks": {"all": True},
        },
        tmp_path,
    )

    plan = build(cfg, cat, project_type="unknown")

    skill_targets = {a.target for a in plan.actions if a.kind == "link_skill_harness"}
    assert any(str(target).startswith(str(home / ".claude" / "skills")) for target in skill_targets)
    assert any(str(target).startswith(str(home / ".codex" / "skills")) for target in skill_targets)

    hook_targets = {a.target for a in plan.actions if a.kind == "install_agent_hook"}
    assert home / ".claude" / "hooks" in hook_targets
    assert home / ".codex" / "hooks" in hook_targets

    bridges = [a for a in plan.actions if a.kind == "register_hook_bridge"]
    assert {a.options["kind"] for a in bridges} == {"claude-code", "codex"}
    codex_bridge = next(a for a in bridges if a.options["kind"] == "codex")
    assert codex_bridge.target == Path(os.path.expanduser("~/.codex/config.toml"))


def test_plan_claude_primary_additional_codex_bridge_gets_tg_ctl_fold_in(
    fake_agent_tools, tmp_path, monkeypatch
):
    """The tg-ctl fold-in (tg-cli#308) follows ANY configured codex hook bridge, not only a
    PRIMARY codex harness: here harness.kind is claude-code with codex as a secondary
    harness.kinds entry, and the codex bridge still gets tg-ctl's Stop hook — the claude-code
    bridge does not."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    script = home / ".files" / "bin" / "tg-ctl"
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/env bun\n", encoding="utf-8")
    bun = home / "bun-bin" / "bun"
    bun.parent.mkdir(parents=True)
    bun.write_text("#!/bin/sh\n", encoding="utf-8")
    bun.chmod(0o755)
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {
            "harness": {
                "kind": "claude-code",
                "kinds": ["codex"],
                "settings_path": "~/.claude/settings.json",
                "hook_bridge": {"enabled": True},
            },
            "agent_hooks": {"all": True},
            "tg_ctl": {"bun_path": str(bun)},
        },
        tmp_path,
    )

    plan = build(cfg, cat, project_type="unknown")

    bridges = [a for a in plan.actions if a.kind == "register_hook_bridge"]
    codex_bridge = next(a for a in bridges if a.options["kind"] == "codex")
    claude_bridge = next(a for a in bridges if a.options["kind"] == "claude-code")
    assert codex_bridge.options.get("extra_stop_hooks")
    assert "extra_stop_hooks" not in claude_bridge.options


def test_plan_custom_hook_target_registers_configured_bridges_with_override(
    fake_agent_tools, tmp_path, monkeypatch
):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    cat = Catalog.scan(str(fake_agent_tools))
    explicit_hooks = tmp_path / "one-hook-dir"
    cfg = _cfg(
        {
            "harness": {"kind": "claude-code", "kinds": ["codex"], "hook_bridge": {"enabled": True}},
            "agent_hooks": {"all": True, "target": str(explicit_hooks)},
        },
        tmp_path,
    )

    plan = build(cfg, cat, project_type="unknown")

    hook_targets = {a.target for a in plan.actions if a.kind == "install_agent_hook"}
    assert hook_targets == {explicit_hooks}
    bridges = [a for a in plan.actions if a.kind == "register_hook_bridge"]
    assert {a.options["kind"] for a in bridges} == {"claude-code", "codex"}
    assert {a.options["hooks_dir"] for a in bridges} == {str(explicit_hooks)}
    assert any("registering configured bridges" in note for note in plan.notes)


def test_plan_explicit_hook_target_does_not_carry_dead_target_kind(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    explicit_hooks = tmp_path / "codex-hooks"
    cfg = _cfg(
        {
            "harness": {"kind": "codex"},
            "agent_hooks": {"all": True, "target": str(explicit_hooks)},
        },
        tmp_path,
    )

    plan = build(cfg, cat, project_type="unknown")

    hook_actions = [a for a in plan.actions if a.kind == "install_agent_hook"]
    assert hook_actions
    assert {a.target for a in hook_actions} == {explicit_hooks}
    assert not any("target_kind" in a.options for a in hook_actions)


def test_plan_default_hook_targets_fan_out(
    fake_agent_tools, tmp_path, monkeypatch
):
    import os

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {
            "harness": {"kind": "claude-code", "kinds": ["codex"]},
            "agent_hooks": {"all": True},
        },
        tmp_path,
    )

    plan = build(cfg, cat, project_type="unknown")

    hook_actions = [a for a in plan.actions if a.kind == "install_agent_hook"]
    assert hook_actions
    assert {a.target for a in hook_actions} == {
        Path(os.path.expanduser("~/.claude/hooks")),
        Path(os.path.expanduser("~/.codex/hooks")),
    }
    assert not any("target_kind" in a.options for a in hook_actions)


def test_plan_scaffolded_defaults_fan_out_hook_targets(
    fake_agent_tools, tmp_path, monkeypatch
):
    import os
    from riglib.state import default_state

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    data = default_state(agent_tools_source=str(fake_agent_tools), project_type="unknown")
    data["harness"]["kind"] = "opencode"
    data["harness"]["kinds"] = ["codex"]
    cfg = _cfg(data, tmp_path)
    cat = Catalog.scan(str(fake_agent_tools))

    plan = build(cfg, cat, project_type="unknown")

    hook_targets = {a.target for a in plan.actions if a.kind == "install_agent_hook"}
    assert hook_targets == {
        Path(os.environ["XDG_CONFIG_HOME"]) / "opencode/hooks",
        Path(os.path.expanduser("~/.codex/hooks")),
    }
    assert not any("target_kind" in a.options for a in plan.actions if a.kind == "install_agent_hook")
    bridges = [a for a in plan.actions if a.kind == "register_hook_bridge"]
    assert {a.options["kind"] for a in bridges} == {"codex"}
    assert not any("hooks_dir" in a.options for a in bridges)


def test_plan_scaffolded_legacy_hook_target_still_fans_out(
    fake_agent_tools, tmp_path, monkeypatch
):
    import os
    from riglib.state import default_state

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    data = default_state(agent_tools_source=str(fake_agent_tools), project_type="unknown")
    data["harness"]["kind"] = "claude-code"
    data["harness"]["kinds"] = ["codex"]
    cfg = _cfg(data, tmp_path)
    cat = Catalog.scan(str(fake_agent_tools))

    plan = build(cfg, cat, project_type="unknown")

    hook_targets = {a.target for a in plan.actions if a.kind == "install_agent_hook"}
    assert hook_targets == {
        Path(os.path.expanduser("~/.claude/hooks")),
        Path(os.path.expanduser("~/.codex/hooks")),
    }
    assert not any("target_kind" in a.options for a in plan.actions if a.kind == "install_agent_hook")
    assert resolve_category_target(cfg, "agent_hooks") == Path(os.path.expanduser("~/.claude/hooks"))
    bridges = [a for a in plan.actions if a.kind == "register_hook_bridge"]
    assert {a.options["kind"] for a in bridges} == {"claude-code", "codex"}


def test_plan_scaffolded_codex_hook_target_matches_status_target(
    fake_agent_tools, tmp_path, monkeypatch
):
    import os
    from riglib.state import default_state

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    data = default_state(agent_tools_source=str(fake_agent_tools), project_type="unknown")
    data["harness"]["kind"] = "codex"
    cfg = _cfg(data, tmp_path)
    cat = Catalog.scan(str(fake_agent_tools))

    plan = build(cfg, cat, project_type="unknown")

    hook_targets = {a.target for a in plan.actions if a.kind == "install_agent_hook"}
    assert hook_targets == {Path(os.path.expanduser("~/.codex/hooks"))}
    assert resolve_category_target(cfg, "agent_hooks") == Path(os.path.expanduser("~/.codex/hooks"))


def test_plan_custom_hook_default_registers_configured_bridges_with_override(
    fake_agent_tools, tmp_path, monkeypatch
):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    cat = Catalog.scan(str(fake_agent_tools))
    custom_hooks = tmp_path / "custom-default-hooks"
    cfg = _cfg(
        {
            "defaults": {"hooks_target": str(custom_hooks)},
            "harness": {"kind": "claude-code", "kinds": ["codex"], "hook_bridge": {"enabled": True}},
            "agent_hooks": {"all": True},
        },
        tmp_path,
    )

    plan = build(cfg, cat, project_type="unknown")

    hook_targets = {a.target for a in plan.actions if a.kind == "install_agent_hook"}
    assert hook_targets == {custom_hooks}
    bridges = [a for a in plan.actions if a.kind == "register_hook_bridge"]
    assert {a.options["kind"] for a in bridges} == {"claude-code", "codex"}
    assert {a.options["hooks_dir"] for a in bridges} == {str(custom_hooks)}
    assert any("registering configured bridges" in note for note in plan.notes)


def test_plan_custom_hook_default_ignores_scaffolded_legacy_hook_target(
    fake_agent_tools, tmp_path, monkeypatch
):
    import os
    from riglib.state import default_state

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    custom_hooks = tmp_path / "custom-default-hooks"
    data = default_state(agent_tools_source=str(fake_agent_tools), project_type="unknown")
    data["defaults"]["hooks_target"] = str(custom_hooks)
    data["harness"]["kinds"] = ["codex"]
    cfg = _cfg(data, tmp_path)
    cat = Catalog.scan(str(fake_agent_tools))

    plan = build(cfg, cat, project_type="unknown")

    hook_targets = {a.target for a in plan.actions if a.kind == "install_agent_hook"}
    assert hook_targets == {custom_hooks}
    assert resolve_category_target(cfg, "agent_hooks") == custom_hooks
    bridges = [a for a in plan.actions if a.kind == "register_hook_bridge"]
    assert {a.options["kind"] for a in bridges} == {"claude-code", "codex"}
    assert {a.options["hooks_dir"] for a in bridges} == {str(custom_hooks)}


def test_plan_equivalent_legacy_hooks_default_still_fans_out(
    fake_agent_tools, tmp_path, monkeypatch
):
    import os

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {
            "defaults": {"hooks_target": str(home / ".claude" / "hooks")},
            "harness": {"kind": "claude-code", "kinds": ["codex"], "hook_bridge": {"enabled": True}},
            "agent_hooks": {"all": True},
        },
        tmp_path,
    )

    plan = build(cfg, cat, project_type="unknown")

    hook_targets = {a.target for a in plan.actions if a.kind == "install_agent_hook"}
    assert hook_targets == {
        Path(os.path.expanduser("~/.claude/hooks")),
        Path(os.path.expanduser("~/.codex/hooks")),
    }
    bridges = [a for a in plan.actions if a.kind == "register_hook_bridge"]
    assert {a.options["kind"] for a in bridges} == {"claude-code", "codex"}


def test_plan_opencode_hook_bridge_emitted_with_harness(fake_agent_tools, tmp_path, monkeypatch):
    import os

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    cat = Catalog.scan(str(fake_agent_tools))
    repo_cfg = tmp_path / "rig.yaml"
    cfg = LoadedConfig(
        data={"harness": {"kind": "opencode", "hook_bridge": {"enabled": True}}},
        repo_root=tmp_path,
        repo_path=repo_cfg,
        layers=[f"repo:{repo_cfg}"],
    )
    a = _bridge_action(build(cfg, cat, project_type="unknown"))
    assert a is not None
    assert a.options["kind"] == "opencode"
    assert a.options["module"] == "opencode_hook_bridge"
    assert a.options["format"] == "opencode-plugin"
    assert a.options["lib_dir"] == str(fake_agent_tools / "lib")
    assert a.target == tmp_path / ".opencode/plugins/zz-agent-tools-hook-bridge.js"


def test_plan_opencode_default_hook_bridge_skipped_without_repo_config(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    global_cfg = tmp_path / "config.yaml"
    cfg = LoadedConfig(
        data={"harness": {"kind": "opencode", "hook_bridge": {"enabled": True}}},
        repo_root=tmp_path,
        global_path=global_cfg,
        layers=[f"global:{global_cfg}"],
    )

    plan = build(cfg, cat, project_type="unknown")

    assert _bridge_action(plan) is None
    assert any("repo-local opencode" in n for n in plan.notes), plan.notes


def test_plan_opencode_default_hook_bridge_emitted_with_explicit_repo_config(
    fake_agent_tools, tmp_path
):
    cat = Catalog.scan(str(fake_agent_tools))
    explicit_cfg = tmp_path / "custom-rig.yaml"
    cfg = LoadedConfig(
        data={"harness": {"kind": "opencode", "hook_bridge": {"enabled": True}}},
        repo_root=tmp_path,
        repo_path=explicit_cfg,
        layers=[f"config:{explicit_cfg}"],
    )

    action = _bridge_action(build(cfg, cat, project_type="unknown"))

    assert action is not None
    assert action.target == tmp_path / ".opencode/plugins/zz-agent-tools-hook-bridge.js"


def test_plan_opencode_explicit_home_plugin_path_allowed_without_repo_config(
    fake_agent_tools, tmp_path, monkeypatch
):
    import os

    cat = Catalog.scan(str(fake_agent_tools))
    global_cfg = tmp_path / "config.yaml"
    cfg = LoadedConfig(
        data={
            "harness": {
                "kind": "opencode",
                "settings_path": "~/.config/opencode/plugins/zz-agent-tools-hook-bridge.js",
                "hook_bridge": {"enabled": True},
            }
        },
        repo_root=tmp_path,
        global_path=global_cfg,
        layers=[f"global:{global_cfg}"],
    )

    action = _bridge_action(build(cfg, cat, project_type="unknown"))

    assert action is not None
    assert action.target == Path(
        os.path.expanduser("~/.config/opencode/plugins/zz-agent-tools-hook-bridge.js")
    )
