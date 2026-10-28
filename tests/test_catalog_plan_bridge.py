"""Hook-bridge variants, skips and pinned-repo harness plans."""

from __future__ import annotations

from pathlib import Path

import pytest

from riglib.catalog import Catalog
from riglib.config import LoadedConfig
from riglib.plan import build, resolve_category_target
from tests.catalog_plan_helpers import _bridge_action, _cfg, _pinned_repo_cfg


def test_plan_hook_bridge_skipped_without_harness(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"skills": {"enabled": False}}, tmp_path)  # no harness block
    assert _bridge_action(build(cfg, cat, project_type="unknown")) is None


def test_plan_hook_bridge_disabled_explicitly(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"harness": {"auto_mode": True, "hook_bridge": {"enabled": False}}},
        tmp_path,
    )
    assert _bridge_action(build(cfg, cat, project_type="unknown")) is None


def test_plan_hook_bridge_skipped_when_agent_hooks_disabled(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {
            "harness": {"auto_mode": True, "kind": "claude-code", "kinds": ["codex"]},
            "agent_hooks": {"enabled": False},
        },
        tmp_path,
    )
    plan = build(cfg, cat, project_type="unknown")
    assert _bridge_action(plan) is None
    assert sum("agent_hooks disabled" in n for n in plan.notes) == 1


def test_plan_hook_bridge_honors_settings_path_and_python(fake_agent_tools, tmp_path):
    import os

    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"harness": {"auto_mode": True, "settings_path": "~/.claude/settings.json",
                     "hook_bridge": {"python": "/opt/py/bin/python3"}}},
        tmp_path,
    )
    a = _bridge_action(build(cfg, cat, project_type="unknown"))
    assert a is not None
    assert a.target == Path(os.path.expanduser("~/.claude/settings.json"))
    assert a.options["python"] == "/opt/py/bin/python3"


def test_plan_claude_hook_bridge_preserves_non_json_settings_suffix(fake_agent_tools, tmp_path):
    """Claude behavior is unchanged: explicit settings_path suffixes are not rejected."""
    settings = tmp_path / ".claude" / "settings.jsonc"
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"harness": {"kind": "claude-code", "settings_path": str(settings),
                     "hook_bridge": {"enabled": True}}},
        tmp_path,
    )
    a = _bridge_action(build(cfg, cat, project_type="unknown"))
    assert a is not None
    assert a.target == settings


def test_plan_codex_hook_bridge_honors_settings_path_and_python(fake_agent_tools, tmp_path):
    settings = tmp_path / ".codex" / "config.toml"
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"harness": {"kind": "codex", "settings_path": str(settings),
                     "hook_bridge": {"enabled": True, "python": "/opt/py/bin/python3"}}},
        tmp_path,
    )
    a = _bridge_action(build(cfg, cat, project_type="unknown"))
    assert a is not None
    assert a.target == settings
    assert a.options["python"] == "/opt/py/bin/python3"


def test_plan_codex_hook_bridge_accepts_suffixless_settings_dir(fake_agent_tools, tmp_path):
    from riglib.actions.hook_bridge import hook_bridge_settings_file

    settings_dir = tmp_path / "codex-config"
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"harness": {"kind": "codex", "settings_path": str(settings_dir),
                     "hook_bridge": {"enabled": True}}},
        tmp_path,
    )
    a = _bridge_action(build(cfg, cat, project_type="unknown"))
    assert a is not None
    assert a.target == settings_dir
    assert hook_bridge_settings_file(a) == settings_dir / "config.toml"


def test_plan_codex_hook_bridge_skips_wrong_settings_path_format(fake_agent_tools, tmp_path):
    settings = tmp_path / ".codex" / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"harness": {"kind": "codex", "settings_path": str(settings),
                     "hook_bridge": {"enabled": True}}},
        tmp_path,
    )
    plan = build(cfg, cat, project_type="unknown")
    assert _bridge_action(plan) is None
    assert any("expects a .toml settings_path" in n and str(settings) in n for n in plan.notes), plan.notes


def test_plan_hook_bridge_skipped_when_dispatcher_module_absent(fake_agent_tools, tmp_path):
    """Fail-closed: an agent-tools checkout without lib/cc_hook_bridge must NOT wire a
    settings.json command that would error at runtime — skip with an actionable note."""
    # remove the dispatcher the fixture ships
    (fake_agent_tools / "lib" / "cc_hook_bridge" / "dispatch.py").unlink()
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"harness": {"auto_mode": True}}, tmp_path)
    plan = build(cfg, cat, project_type="unknown")
    assert _bridge_action(plan) is None
    assert any("cc_hook_bridge" in n and "dispatch.py" in n for n in plan.notes), plan.notes


def test_plan_hook_bridge_skipped_when_entrypoint_absent(fake_agent_tools, tmp_path):
    """The managed command is `python -m cc_hook_bridge`, so __main__.py is required."""
    (fake_agent_tools / "lib" / "cc_hook_bridge" / "__main__.py").unlink()
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"harness": {"auto_mode": True}}, tmp_path)
    plan = build(cfg, cat, project_type="unknown")
    assert _bridge_action(plan) is None
    assert any("cc_hook_bridge" in n and "__main__.py" in n for n in plan.notes), plan.notes


def test_plan_codex_hook_bridge_skipped_when_dispatcher_absent(fake_agent_tools, tmp_path):
    """Fail-closed for Codex too: never register a config.toml command for a missing bridge."""
    (fake_agent_tools / "lib" / "codex_hook_bridge" / "__main__.py").unlink()
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"harness": {"kind": "codex", "hook_bridge": {"enabled": True}}}, tmp_path)
    plan = build(cfg, cat, project_type="unknown")
    assert _bridge_action(plan) is None
    assert any("codex_hook_bridge" in n and "__main__.py" in n for n in plan.notes), plan.notes


def test_plan_opencode_hook_bridge_skipped_when_plugin_absent(fake_agent_tools, tmp_path):
    """Fail-closed for opencode: never wire a plugin symlink when the bridge is incomplete."""
    (fake_agent_tools / "lib" / "opencode_hook_bridge" / "plugin.js").unlink()
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"harness": {"kind": "opencode", "hook_bridge": {"enabled": True}}}, tmp_path)
    plan = build(cfg, cat, project_type="unknown")
    assert _bridge_action(plan) is None
    assert any("opencode_hook_bridge" in n and "plugin.js" in n for n in plan.notes), plan.notes


def test_plan_codex_agent_hooks_default_to_codex_hooks(fake_agent_tools, tmp_path, monkeypatch):
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
            "harness": {"kind": "codex"},
        },
        tmp_path,
    )
    plan = build(cfg, cat, project_type="unknown")
    hook_actions = [a for a in plan.actions if a.kind == "install_agent_hook"]
    assert hook_actions
    assert {a.target for a in hook_actions} == {Path(os.path.expanduser("~/.codex/hooks"))}
    assert not any("target_kind" in a.options for a in hook_actions)
    assert {a.item for a in hook_actions} >= {"block-no-verify", "background-subagent-gate"}
    assert resolve_category_target(cfg, "agent_hooks") == Path(os.path.expanduser("~/.codex/hooks"))


def test_plan_opencode_agent_hooks_default_to_opencode_hooks(fake_agent_tools, tmp_path, monkeypatch):
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
            "harness": {"kind": "opencode"},
        },
        tmp_path,
    )
    plan = build(cfg, cat, project_type="unknown")
    hook_actions = [a for a in plan.actions if a.kind == "install_agent_hook"]
    assert hook_actions
    expected = Path(os.environ["XDG_CONFIG_HOME"]) / "opencode/hooks"
    assert {a.target for a in hook_actions} == {expected}
    assert not any("target_kind" in a.options for a in hook_actions)
    assert {a.item for a in hook_actions} >= {"block-no-verify", "background-subagent-gate"}
    assert resolve_category_target(cfg, "agent_hooks") == expected


def test_plan_omp_hook_bridge_emitted_with_harness(fake_agent_tools, tmp_path, monkeypatch):
    import os

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"harness": {"kind": "omp", "hook_bridge": {"enabled": True}}}, tmp_path)
    a = _bridge_action(build(cfg, cat, project_type="unknown"))
    assert a is not None
    assert a.options["kind"] == "omp"
    assert a.options["module"] == "omp_hook_bridge"
    assert a.options["format"] == "omp-extension"
    assert a.options["lib_dir"] == str(fake_agent_tools / "lib")
    assert a.target == Path(
        os.path.expanduser("~/.omp/agent/extensions/zz-agent-tools-hook-bridge.ts")
    )


def test_plan_omp_hook_bridge_honors_settings_path_and_python(fake_agent_tools, tmp_path):
    import os

    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"harness": {"kind": "omp", "settings_path": "~/.omp/agent/extensions/custom.ts",
                     "hook_bridge": {"enabled": True, "python": "/opt/py/bin/python3"}}},
        tmp_path,
    )
    a = _bridge_action(build(cfg, cat, project_type="unknown"))
    assert a is not None
    assert a.target == Path(os.path.expanduser("~/.omp/agent/extensions/custom.ts"))
    assert a.options["python"] == "/opt/py/bin/python3"


def test_plan_omp_hook_bridge_skips_wrong_settings_path_format(fake_agent_tools, tmp_path):
    settings = tmp_path / "omp-extensions" / "bridge.js"
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"harness": {"kind": "omp", "settings_path": str(settings),
                     "hook_bridge": {"enabled": True}}},
        tmp_path,
    )
    plan = build(cfg, cat, project_type="unknown")
    assert _bridge_action(plan) is None
    assert any("expects a .ts settings_path" in n and str(settings) in n for n in plan.notes), plan.notes


def test_plan_omp_hook_bridge_skipped_when_dispatcher_absent(fake_agent_tools, tmp_path):
    """Fail-closed for omp too: never symlink a broken/incomplete bridge extension."""
    (fake_agent_tools / "lib" / "omp_hook_bridge" / "extension.ts").unlink()
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"harness": {"kind": "omp", "hook_bridge": {"enabled": True}}}, tmp_path)
    plan = build(cfg, cat, project_type="unknown")
    assert _bridge_action(plan) is None
    assert any("omp_hook_bridge" in n and "extension.ts" in n for n in plan.notes), plan.notes


def test_plan_omp_agent_hooks_default_to_omp_hooks(fake_agent_tools, tmp_path, monkeypatch):
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
            "harness": {"kind": "omp"},
        },
        tmp_path,
    )
    plan = build(cfg, cat, project_type="unknown")
    hook_actions = [a for a in plan.actions if a.kind == "install_agent_hook"]
    assert hook_actions
    expected = home / ".omp" / "agent" / "hooks"
    assert {a.target for a in hook_actions} == {expected}
    assert resolve_category_target(cfg, "agent_hooks") == expected


def test_plan_omp_agent_hooks_follow_pi_coding_agent_dir(fake_agent_tools, tmp_path, monkeypatch):
    """PI_CODING_AGENT_DIR is a full override of the omp agent root — both hooks and the bridge
    extension must follow it.

    Unlike codex's ``RIG_CODEX_HOME`` (a rig-owned env var the codex dispatcher does not know
    about, so rig must thread it through as an explicit ``hooks_dir`` bridge option), omp's own
    dispatcher PORTS rig's ``omp_agent_root()`` precedence verbatim
    (lib/omp_hook_bridge/dispatch.py's ``_omp_agent_root()``) — it independently resolves the
    SAME ``PI_CODING_AGENT_DIR``-relative dir at runtime, with no override needed. So the bridge
    action must NOT carry a ``hooks_dir`` option here: baking one in would only turn the plain
    symlink into an unnecessary wrapper (see the plan.py comment next to the codex-only
    divergence block).
    """
    home = tmp_path / "home"
    override = tmp_path / "custom-omp-agent"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(override))
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {
            "harness": {"kind": "omp", "hook_bridge": {"enabled": True}},
            "agent_hooks": {"all": True},
        },
        tmp_path,
    )
    plan = build(cfg, cat, project_type="unknown")
    hook_targets = {a.target for a in plan.actions if a.kind == "install_agent_hook"}
    assert hook_targets == {override / "hooks"}
    bridge = _bridge_action(plan)
    assert bridge is not None
    assert bridge.target == override / "extensions" / "zz-agent-tools-hook-bridge.ts"
    assert "hooks_dir" not in bridge.options


def test_plan_codex_bridge_does_not_wire_pre_agent_yet(fake_agent_tools, tmp_path):
    from riglib.actions.hook_bridge import hook_bridge_entries

    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {
            "skills": {"enabled": False},
            "agent_hooks": {"all": True},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "harness": {"kind": "codex", "hook_bridge": {"enabled": True}},
        },
        tmp_path,
    )
    plan = build(cfg, cat, project_type="unknown")
    hook_actions = [a for a in plan.actions if a.kind == "install_agent_hook"]
    assert any(a.item == "background-subagent-gate" for a in hook_actions)
    bridge = _bridge_action(plan)
    assert bridge is not None
    assert bridge.options["kind"] == "codex"
    entries = hook_bridge_entries(bridge)
    assert set(entries) == {"PreToolUse", "PostToolUse", "Stop"}
    assert "SubagentStart" not in str(entries)
    assert "Agent" not in str(entries)
    # pre-skill (the Skill matcher) is CC-only too — same undocumented-Codex-payload gap
    # pre-agent has; a future accidental addition here would silently contradict
    # docs/config-schema.md's stated gap.
    assert "Skill" not in str(entries)
    # pre-monitor (the Monitor matcher) is CC-only too — Codex has no Monitor-equivalent
    # tool today, so a future accidental addition here would be wrong for the same reason.
    assert "Monitor" not in str(entries)
    # pre-worktree-enter (the EnterWorktree matcher) is CC-only too — Codex has no
    # worktree-switching tool of its own, same undocumented-payload gap.
    assert "EnterWorktree" not in str(entries)


def test_plan_codex_agent_hooks_preserve_custom_target(fake_agent_tools, tmp_path):
    from riglib.actions.hook_bridge import hook_bridge_entries

    custom = tmp_path / "custom-hooks"
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {
            "skills": {"enabled": False},
            "agent_hooks": {"all": True, "target": str(custom)},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "harness": {"kind": "codex"},
        },
        tmp_path,
    )
    plan = build(cfg, cat, project_type="unknown")
    hook_actions = [a for a in plan.actions if a.kind == "install_agent_hook"]
    assert hook_actions
    assert {a.target for a in hook_actions} == {custom}
    assert resolve_category_target(cfg, "agent_hooks") == custom
    bridge = _bridge_action(plan)
    assert bridge is not None
    assert bridge.options["kind"] == "codex"
    assert bridge.options["hooks_dir"] == str(custom)
    assert "CODEX_HOOKS_DIR=" in str(hook_bridge_entries(bridge))


def test_plan_codex_agent_hooks_preserve_custom_default_target(fake_agent_tools, tmp_path):
    custom = tmp_path / "default-hooks"
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {
            "skills": {"enabled": False},
            "defaults": {"hooks_target": str(custom)},
            "agent_hooks": {"all": True},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "harness": {"kind": "codex"},
        },
        tmp_path,
    )
    plan = build(cfg, cat, project_type="unknown")
    hook_actions = [a for a in plan.actions if a.kind == "install_agent_hook"]
    assert hook_actions
    assert {a.target for a in hook_actions} == {custom}
    assert resolve_category_target(cfg, "agent_hooks") == custom


def test_plan_hooks_reach_the_global_omp_dir_when_a_repo_pins_another_primary_kind(
    fake_agent_tools, tmp_path, monkeypatch
):
    """rig-cli#743: the user-scope hook dirs belong to the MACHINE's harnesses (the global config).

    A repo rig.yaml that pins ``harness.kind: claude-code`` replaces the global primary ``omp``;
    ``apply commit`` run from that repo then left ``~/.omp/agent/hooks`` on a stale descriptor
    while omp kept reading it. The global layer's kinds are always part of the fan-out.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.delenv("PI_CONFIG_DIR", raising=False)
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "skills": {"enabled": False},
            "agent_hooks": {"all": True},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "harness": {"kind": "claude-code", "kinds": ["codex"]},
        },
        repo_root=tmp_path,
        global_data={"harness": {"kind": "omp", "kinds": ["codex"]}},
    )
    plan = build(cfg, cat, project_type="unknown")
    hook_targets = {a.target for a in plan.actions if a.kind == "install_agent_hook"}
    assert hook_targets == {
        home / ".claude" / "hooks",
        home / ".codex" / "hooks",
        home / ".omp" / "agent" / "hooks",
    }


def test_plan_hooks_without_a_global_harness_keep_the_repo_kinds(fake_agent_tools, tmp_path, monkeypatch):
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
    assert {a.target for a in plan.actions if a.kind == "install_agent_hook"} == {home / ".claude" / "hooks"}


@pytest.mark.parametrize("bad_kinds", [5, True, "codex", {"a": 1}])
def test_plan_ignores_a_malformed_global_kinds_value(fake_agent_tools, tmp_path, monkeypatch, bad_kinds):
    """The global layer under a repo override is validated only inside the merged block, so a
    scalar ``kinds`` must neither crash the plan nor be unpacked character by character."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    cfg = _pinned_repo_cfg(tmp_path, {"kind": "omp", "kinds": bad_kinds})
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert {a.target for a in plan.actions if a.kind == "install_agent_hook"} == {
        home / ".claude" / "hooks",
        home / ".omp" / "agent" / "hooks",
    }


def test_plan_machine_kinds_do_not_override_an_explicit_hooks_target(fake_agent_tools, tmp_path):
    custom = tmp_path / "custom-hooks"
    cfg = _pinned_repo_cfg(tmp_path, {"kind": "omp"}, agent_hooks={"all": True, "target": str(custom)})
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert {a.target for a in plan.actions if a.kind == "install_agent_hook"} == {custom}


def test_plan_disabled_global_harness_adds_no_machine_kinds(fake_agent_tools, tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    cfg = _pinned_repo_cfg(tmp_path, {"enabled": False, "kind": "omp"})
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert {a.target for a in plan.actions if a.kind == "install_agent_hook"} == {home / ".claude" / "hooks"}


def test_plan_registers_the_omp_bridge_for_a_global_omp_kind_under_a_repo_pin(
    fake_agent_tools, tmp_path, monkeypatch
):
    """Descriptors without the bridge extension would be inert: omp reads them through it."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cfg = _pinned_repo_cfg(tmp_path, {"kind": "omp"})
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert {a.options["kind"] for a in plan.actions if a.kind == "register_hook_bridge"} >= {"claude-code", "omp"}


def test_plan_a_repo_that_disables_its_harness_opts_out_of_machine_kinds(fake_agent_tools, tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    cfg = _pinned_repo_cfg(tmp_path, {"kind": "omp"})
    cfg.data["harness"] = {"enabled": False}
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert {a.target for a in plan.actions if a.kind == "install_agent_hook"} == {home / ".claude" / "hooks"}


def test_plan_a_kindless_global_harness_block_still_counts_the_default_claude_kind(
    fake_agent_tools, tmp_path, monkeypatch
):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    cfg = _pinned_repo_cfg(tmp_path, {"auto_mode": True})
    cfg.data["harness"] = {"kind": "omp"}
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert {a.target for a in plan.actions if a.kind == "install_agent_hook"} == {
        home / ".omp" / "agent" / "hooks",
        home / ".claude" / "hooks",
    }
