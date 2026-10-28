"""Claude hook bridge tests."""

from __future__ import annotations

import json
import re
import shlex
import pytest
from riglib.actions import run_plan
from riglib.actions.harness_mode import SELF_MERGE_LEGACY_ALLOW
from riglib.actions.hook_bridge import HOOK_BRIDGE_TIMEOUT_S
from riglib.catalog import Catalog
from riglib.config import LoadedConfig
from riglib.drift import detect
from riglib.plan import build
from tests.actions_drift_helpers import (
    _applied_bridge_settings,
    _auto_harness_cfg,
    _bridge_cfg,
    _bridge_results,
    _managed_bridge_hooks,
    _matcher_blocks,
    _matcher_commands,
    _rewrite_managed_timeouts,
    _user_settings,
    _without_matcher,
)


def test_hook_bridge_idempotent_reapply(fake_agent_tools, tmp_path):
    """A second apply is a no-op (skipped) — no duplicate hook blocks."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".claude" / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_bridge_cfg(repo, fake_agent_tools, settings_path=settings), cat, project_type="unknown")
    run_plan(plan)
    second = run_plan(plan)
    res = _bridge_results(second)
    assert res and all(r.status == "skipped" for r in res), [r.detail for r in res]
    # exactly one managed block per matcher, not duplicated
    data = json.loads(settings.read_text())
    for matcher in (
        "Bash",
        "Edit|Write|MultiEdit|NotebookEdit",
        "Agent|Task",
        "Skill",
        "EnterWorktree",
    ):
        assert len(_matcher_blocks(data, "PreToolUse", matcher)) == 1, matcher
    assert len(data["hooks"]["PostToolUse"]) == 1


def test_hook_bridge_preserves_existing_unrelated_hooks(fake_agent_tools, tmp_path):
    """The user's own hooks (rtk-rewrite, tg-ctl) survive registration untouched."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({
        "permissions": {"defaultMode": "default"},
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "/Users/x/.claude/hooks/rtk-rewrite.sh"}]},
                {"matcher": "Agent", "hooks": [{"type": "command", "command": "/Users/x/bin/agent-wrapper.sh"}]},
            ],
            "PostToolUse": [
                {"matcher": "Write", "hooks": [{"type": "command", "command": "/Users/x/bin/format-on-save.sh"}]},
            ],
            "Notification": [
                {"matcher": "idle_prompt", "hooks": [{"type": "command", "command": "afplay glass.aiff"}]},
            ],
        },
    }, indent=2))
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_bridge_cfg(repo, fake_agent_tools, settings_path=settings), cat, project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    data = json.loads(settings.read_text())
    # the rtk hook is still there (now ALONGSIDE our Bash block, not replacing it)
    bash_cmds = _matcher_commands(data, "PreToolUse", "Bash")
    assert any("rtk-rewrite.sh" in c for c in bash_cmds), bash_cmds
    assert any("cc_hook_bridge PreToolUse" in c for c in bash_cmds), bash_cmds
    agent_cmds = _matcher_commands(data, "PreToolUse", "Agent")
    subagent_cmds = _matcher_commands(data, "PreToolUse", "Agent|Task")
    assert agent_cmds == ["/Users/x/bin/agent-wrapper.sh"]
    assert any("cc_hook_bridge PreToolUse" in c for c in subagent_cmds), subagent_cmds
    # the user's own PostToolUse hook (different matcher) survives ALONGSIDE our new
    # managed PostToolUse block — the bridge must never clobber a format-on-save-style hook
    post_cmds = [hk["command"] for b in data["hooks"]["PostToolUse"] for hk in b["hooks"]]
    assert any("format-on-save.sh" in c for c in post_cmds), post_cmds
    assert any("cc_hook_bridge PostToolUse" in c for c in post_cmds), post_cmds
    # unrelated event preserved verbatim
    assert data["hooks"]["Notification"][0]["hooks"][0]["command"] == "afplay glass.aiff"
    # the permissions block survives the hooks merge (apply_harness, which also runs under
    # auto_mode, may set its mode — the point here is the hook-bridge merge doesn't drop it).
    assert "permissions" in data


def test_hook_bridge_repoints_drifted_command(fake_agent_tools, tmp_path):
    """A managed block whose command's lib path drifted is rewritten in place (not dup'd)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    # stale managed entries pointing at an OLD lib path (PreToolUse AND PostToolUse)
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command",
         "command": "PYTHONPATH=/old/path/lib python3 -m cc_hook_bridge PreToolUse"}]},
        {"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [{"type": "command",
         "command": "PYTHONPATH=/old/path/lib python3 -m cc_hook_bridge PreToolUse"}]},
        {"matcher": "Agent|Task", "hooks": [{"type": "command",
         "command": "PYTHONPATH=/old/path/lib python3 -m cc_hook_bridge PreToolUse"}]},
    ], "PostToolUse": [
        {"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [{"type": "command",
         "command": "PYTHONPATH=/old/path/lib python3 -m cc_hook_bridge PostToolUse"}]},
    ], "Stop": [{"hooks": [{"type": "command",
              "command": "PYTHONPATH=/old/path/lib python3 -m cc_hook_bridge Stop"}]}]}}, indent=2))
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_bridge_cfg(repo, fake_agent_tools, settings_path=settings), cat, project_type="unknown")
    run_plan(plan)
    data = json.loads(settings.read_text())
    bash_blocks = [b for b in data["hooks"]["PreToolUse"] if b.get("matcher") == "Bash"]
    assert len(bash_blocks) == 1  # rewritten, not duplicated
    cmd = bash_blocks[0]["hooks"][0]["command"]
    assert "/old/path/lib" not in cmd
    assert str(fake_agent_tools / "lib") in cmd
    # the drifted PostToolUse command is likewise rewritten in place, not duplicated
    post_blocks = data["hooks"]["PostToolUse"]
    assert len(post_blocks) == 1
    post_cmd = post_blocks[0]["hooks"][0]["command"]
    assert "/old/path/lib" not in post_cmd
    assert "cc_hook_bridge PostToolUse" in post_cmd
    assert str(fake_agent_tools / "lib") in post_cmd


def test_hook_bridge_timeout_floor_covers_one_full_descriptor_budget():
    # 960s is the largest agents-hooks/v1 descriptor timeout_ms shipped (the hatch-capable
    # guards: the hatch helper's 900s tg-ctl wait + 30s process margin fits inside it).
    assert HOOK_BRIDGE_TIMEOUT_S > 960
    assert HOOK_BRIDGE_TIMEOUT_S == 1200


def test_hook_bridge_writes_outer_timeout_on_every_managed_entry(fake_agent_tools, tmp_path):
    plan, settings = _applied_bridge_settings(fake_agent_tools, tmp_path)
    managed = _managed_bridge_hooks(json.loads(settings.read_text()))
    assert managed
    assert all(hk.get("timeout") == HOOK_BRIDGE_TIMEOUT_S for hk in managed), managed
    assert detect(plan).in_sync


@pytest.mark.parametrize(
    "stale",
    [..., 300, 600, 300.5, True, "1200", float("nan"), float("inf")],
    ids=["missing", "300", "600", "float-short", "bool", "str", "nan", "inf"],
)
def test_hook_bridge_reconciles_missing_or_short_timeout(fake_agent_tools, tmp_path, stale):
    plan, settings = _applied_bridge_settings(fake_agent_tools, tmp_path)
    _rewrite_managed_timeouts(settings, stale)

    drifted = [i for i in detect(plan).items if i.item == "hook-bridge"]
    assert drifted and all(i.direction == "modified" for i in drifted), [i.detail for i in drifted]
    assert all("timeout" in i.detail for i in drifted), [i.detail for i in drifted]

    res = _bridge_results(run_plan(plan))
    assert res and all(r.status != "skipped" for r in res), [r.detail for r in res]
    managed = _managed_bridge_hooks(json.loads(settings.read_text()))
    assert all(hk["timeout"] == HOOK_BRIDGE_TIMEOUT_S for hk in managed), managed
    assert detect(plan).in_sync


@pytest.mark.parametrize("higher", [3600, 3600.0], ids=["int", "float"])
def test_hook_bridge_keeps_a_higher_operator_timeout_even_when_command_is_rewritten(
    fake_agent_tools, tmp_path, higher
):
    # JSON allows `3600.0`; a float at/above the floor is a real operator value, not "missing".
    plan, settings = _applied_bridge_settings(fake_agent_tools, tmp_path)
    data = json.loads(settings.read_text())
    for hk in _managed_bridge_hooks(data):
        hk["timeout"] = higher
        hk["command"] = hk["command"].replace(str(fake_agent_tools / "lib"), "/old/lib")
    settings.write_text(json.dumps(data, indent=2) + "\n")

    run_plan(plan)

    managed = _managed_bridge_hooks(json.loads(settings.read_text()))
    assert all("/old/lib" not in hk["command"] for hk in managed)
    assert all(hk["timeout"] == higher for hk in managed), managed  # never lowered
    assert detect(plan).in_sync


def test_hook_bridge_timeout_reconcile_leaves_user_hooks_byte_identical(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    user_bash = {"type": "command", "command": "/Users/x/.claude/hooks/rtk-rewrite.sh", "timeout": 5}
    user_notify = {"matcher": "idle_prompt", "hooks": [{"type": "command", "command": "afplay glass.aiff"}]}
    settings.write_text(json.dumps({"hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [dict(user_bash)]}],
        "Notification": [user_notify],
    }}, indent=2))
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_bridge_cfg(repo, fake_agent_tools, settings_path=settings), cat, project_type="unknown")
    run_plan(plan)
    _rewrite_managed_timeouts(settings, ...)
    run_plan(plan)

    data = json.loads(settings.read_text())
    user_hooks = [
        hk for b in data["hooks"]["PreToolUse"] for hk in b["hooks"] if "rtk-rewrite" in hk["command"]
    ]
    assert user_hooks == [user_bash]  # its own short timeout is NOT touched
    assert data["hooks"]["Notification"] == [user_notify]


def test_hook_bridge_timeout_reapply_converges_with_no_changes(fake_agent_tools, tmp_path):
    plan, settings = _applied_bridge_settings(fake_agent_tools, tmp_path)
    _rewrite_managed_timeouts(settings, 300)
    run_plan(plan)
    reconciled = settings.read_bytes()

    res = _bridge_results(run_plan(plan))

    assert res and all(r.status == "skipped" for r in res), [r.detail for r in res]
    assert settings.read_bytes() == reconciled


def test_hook_bridge_skip_leaves_short_timeout_but_drift_still_reports_it(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".claude" / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))
    run_plan(build(_bridge_cfg(repo, fake_agent_tools, settings_path=settings), cat, project_type="unknown"))
    _rewrite_managed_timeouts(settings, ...)
    skip_plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, on_conflict="skip"), cat, project_type="unknown"
    )

    run_plan(skip_plan)

    assert all("timeout" not in hk for hk in _managed_bridge_hooks(json.loads(settings.read_text())))
    assert any("timeout" in i.detail for i in detect(skip_plan).items if i.item == "hook-bridge")


def test_hook_bridge_drift_missing_then_synced(fake_agent_tools, tmp_path):
    """Before apply the bridge is missing drift; after apply, in sync."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".claude" / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_bridge_cfg(repo, fake_agent_tools, settings_path=settings), cat, project_type="unknown")
    before = detect(plan)
    assert any(i.item == "hook-bridge" and i.direction == "missing" for i in before.items), \
        [i.detail for i in before.items]
    run_plan(plan)
    after = detect(plan)
    assert not any(i.item == "hook-bridge" for i in after.items), [i.detail for i in after.items]


def test_codex_hook_bridge_drift_missing_then_synced(fake_agent_tools, tmp_path):
    """Before apply the Codex bridge is missing drift; after apply, in sync."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )
    before = detect(plan)
    assert any(i.item == "hook-bridge" and i.direction == "missing" for i in before.items), \
        [i.detail for i in before.items]
    run_plan(plan)
    after = detect(plan)
    assert not any(i.item == "hook-bridge" for i in after.items), [i.detail for i in after.items]


def test_hook_bridge_drift_missing_posttooluse_only(fake_agent_tools, tmp_path):
    """The pre-0.8.0 upgrade state — PreToolUse + Stop wired, PostToolUse absent — is
    reported as missing drift, and a re-apply converges it (this is exactly what every
    existing installation shows until its next `rig apply`)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".claude" / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_bridge_cfg(repo, fake_agent_tools, settings_path=settings), cat, project_type="unknown")
    run_plan(plan)
    # strip the PostToolUse event wholesale → the pre-0.8.0 on-disk state
    data = json.loads(settings.read_text())
    del data["hooks"]["PostToolUse"]
    settings.write_text(json.dumps(data, indent=2))
    before = detect(plan)
    missing = [i for i in before.items if i.item == "hook-bridge" and i.direction == "missing"]
    assert missing, [i.detail for i in before.items]
    assert any("PostToolUse" in i.detail for i in missing), [i.detail for i in missing]
    run_plan(plan)
    after = detect(plan)
    assert not any(i.item == "hook-bridge" for i in after.items), [i.detail for i in after.items]


def test_hook_bridge_drift_missing_agent_task_only(fake_agent_tools, tmp_path):
    """The pre-agent upgrade state has Bash/write/Stop wired but no Agent|Task matcher."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".claude" / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_bridge_cfg(repo, fake_agent_tools, settings_path=settings), cat, project_type="unknown")
    run_plan(plan)
    data = json.loads(settings.read_text())
    data["hooks"]["PreToolUse"] = [
        block for block in data["hooks"]["PreToolUse"]
        if block.get("matcher") != "Agent|Task"
    ]
    settings.write_text(json.dumps(data, indent=2))
    before = detect(plan)
    missing = [i for i in before.items if i.item == "hook-bridge" and i.direction == "missing"]
    assert missing, [i.detail for i in before.items]
    assert any("Agent|Task" in i.detail for i in missing), [i.detail for i in missing]
    run_plan(plan)
    after = detect(plan)
    assert not any(i.item == "hook-bridge" for i in after.items), [i.detail for i in after.items]


def test_hook_bridge_drift_missing_skill_only(fake_agent_tools, tmp_path):
    """The pre-skill upgrade state has Bash/write/Agent|Task/Stop wired but no Skill matcher —
    same shape as the pre-agent upgrade case (test_hook_bridge_drift_missing_agent_task_only)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".claude" / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_bridge_cfg(repo, fake_agent_tools, settings_path=settings), cat, project_type="unknown")
    run_plan(plan)
    data = json.loads(settings.read_text())
    data["hooks"]["PreToolUse"] = [
        block for block in data["hooks"]["PreToolUse"]
        if block.get("matcher") != "Skill"
    ]
    settings.write_text(json.dumps(data, indent=2))
    before = detect(plan)
    missing = [i for i in before.items if i.item == "hook-bridge" and i.direction == "missing"]
    assert missing, [i.detail for i in before.items]
    assert any("Skill" in i.detail for i in missing), [i.detail for i in missing]
    run_plan(plan)
    after = detect(plan)
    assert not any(i.item == "hook-bridge" for i in after.items), [i.detail for i in after.items]


def test_hook_bridge_drift_missing_monitor_only(fake_agent_tools, tmp_path):
    """The pre-monitor upgrade state has Bash/write/Agent|Task/Skill/Stop wired but no Monitor
    matcher — same shape as the pre-agent/pre-skill upgrade cases above. This is the state
    every existing install is actually in the moment this matcher ships (HYP-1350): drift must
    report the gap, and apply must add Monitor while leaving the other four matchers alone."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".claude" / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_bridge_cfg(repo, fake_agent_tools, settings_path=settings), cat, project_type="unknown")
    run_plan(plan)
    data = json.loads(settings.read_text())
    other_matchers_before = {
        block.get("matcher") for block in data["hooks"]["PreToolUse"]
        if block.get("matcher") != "Monitor"
    }
    data["hooks"]["PreToolUse"] = _without_matcher(data, "Monitor")
    settings.write_text(json.dumps(data, indent=2))
    before = detect(plan)
    missing = [i for i in before.items if i.item == "hook-bridge" and i.direction == "missing"]
    assert missing, [i.detail for i in before.items]
    assert any("Monitor" in i.detail for i in missing), [i.detail for i in missing]
    run_plan(plan)
    after = detect(plan)
    assert not any(i.item == "hook-bridge" for i in after.items), [i.detail for i in after.items]
    # the pre-existing matchers were left alone, not rewritten/duplicated
    data = json.loads(settings.read_text())
    matchers_after = [block.get("matcher") for block in data["hooks"]["PreToolUse"]]
    assert set(matchers_after) == other_matchers_before | {"Monitor"}
    assert len(matchers_after) == len(set(matchers_after)), matchers_after


def test_hook_bridge_drift_missing_enterworktree_only(fake_agent_tools, tmp_path):
    """The pre-worktree-enter upgrade state has Bash/write/Agent|Task/Skill/Stop wired but no
    EnterWorktree matcher — same shape as the pre-skill upgrade case
    (test_hook_bridge_drift_missing_skill_only)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".claude" / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_bridge_cfg(repo, fake_agent_tools, settings_path=settings), cat, project_type="unknown")
    run_plan(plan)
    data = json.loads(settings.read_text())
    data["hooks"]["PreToolUse"] = [
        block for block in data["hooks"]["PreToolUse"]
        if block.get("matcher") != "EnterWorktree"
    ]
    settings.write_text(json.dumps(data, indent=2))
    before = detect(plan)
    missing = [i for i in before.items if i.item == "hook-bridge" and i.direction == "missing"]
    assert missing, [i.detail for i in before.items]
    assert any("EnterWorktree" in i.detail for i in missing), [i.detail for i in missing]
    run_plan(plan)
    after = detect(plan)
    assert not any(i.item == "hook-bridge" for i in after.items), [i.detail for i in after.items]


def test_hook_bridge_drift_detects_stale_command(fake_agent_tools, tmp_path):
    """A managed hook whose command drifted (old lib path) is reported MODIFIED, not in-sync."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"hooks": {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command",
             "command": "PYTHONPATH=/old/lib python3 -m cc_hook_bridge PreToolUse"}]},
            {"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [{"type": "command",
             "command": "PYTHONPATH=/old/lib python3 -m cc_hook_bridge PreToolUse"}]},
            {"matcher": "Agent|Task", "hooks": [{"type": "command",
             "command": "PYTHONPATH=/old/lib python3 -m cc_hook_bridge PreToolUse"}]},
            {"matcher": "Skill", "hooks": [{"type": "command",
             "command": "PYTHONPATH=/old/lib python3 -m cc_hook_bridge PreToolUse"}]},
            {"matcher": "Monitor", "hooks": [{"type": "command",
             "command": "PYTHONPATH=/old/lib python3 -m cc_hook_bridge PreToolUse"}]},
            {"matcher": "EnterWorktree", "hooks": [{"type": "command",
             "command": "PYTHONPATH=/old/lib python3 -m cc_hook_bridge PreToolUse"}]},
            {"matcher": "Workflow", "hooks": [{"type": "command",
             "command": "PYTHONPATH=/old/lib python3 -m cc_hook_bridge PreToolUse"}]},
        ],
        "PostToolUse": [
            {"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [{"type": "command",
             "command": "PYTHONPATH=/old/lib python3 -m cc_hook_bridge PostToolUse"}]},
        ],
        "Stop": [{"hooks": [{"type": "command",
                  "command": "PYTHONPATH=/old/lib python3 -m cc_hook_bridge Stop"}]}],
    }}, indent=2))
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_bridge_cfg(repo, fake_agent_tools, settings_path=settings), cat, project_type="unknown")
    report = detect(plan)
    modified = [i for i in report.items if i.item == "hook-bridge" and i.direction == "modified"]
    missing = [i for i in report.items if i.item == "hook-bridge" and i.direction == "missing"]
    assert modified, [i.detail for i in report.items if i.item == "hook-bridge"]
    assert not missing, [i.detail for i in missing]
    # and apply converges it back to in-sync
    run_plan(plan)
    after = detect(plan)
    assert not any(i.item == "hook-bridge" for i in after.items), [i.detail for i in after.items]


def test_hook_bridge_drift_detects_wrong_hook_type(fake_agent_tools, tmp_path):
    """A managed hook with the right command but wrong CC hook type is modified drift."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".claude" / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_bridge_cfg(repo, fake_agent_tools, settings_path=settings), cat, project_type="unknown")
    run_plan(plan)
    data = json.loads(settings.read_text())
    bash_hook = _matcher_blocks(data, "PreToolUse", "Bash")[0]["hooks"][0]
    bash_hook["type"] = "prompt"
    settings.write_text(json.dumps(data, indent=2))
    report = detect(plan)
    modified = [i for i in report.items if i.item == "hook-bridge" and i.direction == "modified"]
    assert modified, [i.detail for i in report.items if i.item == "hook-bridge"]
    assert any("PreToolUse[Bash]" in i.detail for i in modified), [i.detail for i in modified]
    run_plan(plan)
    repaired = json.loads(settings.read_text())
    repaired_hook = _matcher_blocks(repaired, "PreToolUse", "Bash")[0]["hooks"][0]
    assert repaired_hook["type"] == "command"
    after = detect(plan)
    assert not any(i.item == "hook-bridge" for i in after.items), [i.detail for i in after.items]


def test_hook_bridge_skip_leaves_stale_command_untouched(fake_agent_tools, tmp_path):
    """on_conflict=skip must NOT rewrite a drifted managed command (file-level skip parity)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    stale = "PYTHONPATH=/old/lib python3 -m cc_hook_bridge PreToolUse"
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": stale}]},
        {"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [{"type": "command", "command": stale}]},
        {"matcher": "Agent|Task", "hooks": [{"type": "command", "command": stale}]},
        {"matcher": "Skill", "hooks": [{"type": "command", "command": stale}]},
        {"matcher": "Monitor", "hooks": [{"type": "command", "command": stale}]},
        {"matcher": "EnterWorktree", "hooks": [{"type": "command", "command": stale}]},
        {"matcher": "Workflow", "hooks": [{"type": "command", "command": stale}]},
    ], "PostToolUse": [
        {"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [{"type": "command",
         "command": "PYTHONPATH=/old/lib python3 -m cc_hook_bridge PostToolUse"}]},
    ], "Stop": [{"hooks": [{"type": "command", "command": "PYTHONPATH=/old/lib python3 -m cc_hook_bridge Stop"}]}]}}, indent=2))
    cfg = _bridge_cfg(repo, fake_agent_tools, settings_path=settings)
    cfg.data["defaults"] = {"on_conflict": "skip"}
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(cfg, cat, project_type="unknown")
    report = run_plan(plan)
    res = _bridge_results(report)
    assert res and all(r.status == "skipped" for r in res), [r.detail for r in res]
    data = json.loads(settings.read_text())
    bash_cmd = next(b["hooks"][0]["command"] for b in data["hooks"]["PreToolUse"] if b["matcher"] == "Bash")
    subagent_cmd = next(b["hooks"][0]["command"] for b in data["hooks"]["PreToolUse"] if b["matcher"] == "Agent|Task")
    skill_cmd = next(b["hooks"][0]["command"] for b in data["hooks"]["PreToolUse"] if b["matcher"] == "Skill")
    assert bash_cmd == stale  # left untouched under skip
    assert subagent_cmd == stale
    assert skill_cmd == stale


def test_hook_bridge_skip_leaves_wrong_hook_type_untouched(fake_agent_tools, tmp_path):
    """on_conflict=skip must NOT repair a managed hook with the wrong CC hook type."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".claude" / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_bridge_cfg(repo, fake_agent_tools, settings_path=settings), cat, project_type="unknown")
    run_plan(plan)
    data = json.loads(settings.read_text())
    bash_hook = next(
        b["hooks"][0] for b in data["hooks"]["PreToolUse"]
        if b.get("matcher") == "Bash"
    )
    bash_hook["type"] = "prompt"
    settings.write_text(json.dumps(data, indent=2))
    cfg = _bridge_cfg(repo, fake_agent_tools, settings_path=settings)
    cfg.data["defaults"] = {"on_conflict": "skip"}
    skip_plan = build(cfg, cat, project_type="unknown")
    report = run_plan(skip_plan)
    res = _bridge_results(report)
    assert res and all(r.status == "skipped" for r in res), [r.detail for r in res]
    skipped = json.loads(settings.read_text())
    skipped_hook = next(
        b["hooks"][0] for b in skipped["hooks"]["PreToolUse"]
        if b.get("matcher") == "Bash"
    )
    assert skipped_hook["type"] == "prompt"


def test_hook_bridge_skip_leaves_malformed_settings_untouched(fake_agent_tools, tmp_path):
    """on_conflict=skip leaves a malformed settings.json untouched (returns skipped)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{ this is not json")
    cfg = _bridge_cfg(repo, fake_agent_tools, settings_path=settings)
    cfg.data["defaults"] = {"on_conflict": "skip"}
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(cfg, cat, project_type="unknown")
    report = run_plan(plan)
    res = _bridge_results(report)
    assert res and all(r.status == "skipped" for r in res), [r.detail for r in res]
    assert settings.read_text() == "{ this is not json"  # untouched


def test_hook_bridge_quotes_python_interpreter(fake_agent_tools, tmp_path):
    """A configured python path is shlex-quoted in the command (spaces / injection safe)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".claude" / "settings.json"
    cfg = _bridge_cfg(repo, fake_agent_tools, settings_path=settings,
                      hook_bridge={"python": "/opt/my py/bin/python3"})
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(cfg, cat, project_type="unknown")
    run_plan(plan)
    data = json.loads(settings.read_text())
    bash_cmd = next(b["hooks"][0]["command"] for b in data["hooks"]["PreToolUse"] if b["matcher"] == "Bash")
    # the space-containing interpreter path must be quoted, not left bare
    assert "'/opt/my py/bin/python3'" in bash_cmd, bash_cmd


def test_self_merge_migrates_legacy_ship_script_allow_rules(fake_agent_tools, tmp_path):
    # rig-cli#635: older rig wrote Bash(*/pr-ship.sh:*) / Bash(*/ship.sh:*) into permissions.allow;
    # Claude Code rejects that syntax (never matches, warns every start). Apply removes exactly those
    # two strings (an exact-string migration — the user's other allow entries survive) and status
    # reports them until then.
    repo = tmp_path / "repo"
    repo.mkdir()
    _user_settings().parent.mkdir(parents=True, exist_ok=True)
    _user_settings().write_text(
        json.dumps({"permissions": {"defaultMode": "auto", "allow": ["Bash(docker:*)", *SELF_MERGE_LEGACY_ALLOW]}}),
        encoding="utf-8",
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools), cat, project_type="unknown")
    assert [x for x in detect(plan).items if x.category == "harness" and "legacy" in x.detail]

    run_plan(plan)
    perms = json.loads(_user_settings().read_text(encoding="utf-8"))["permissions"]
    assert "Bash(docker:*)" in perms["allow"]
    assert not [r for r in perms["allow"] if r in SELF_MERGE_LEGACY_ALLOW]
    assert not [x for x in detect(plan).items if x.category == "harness"]
    second = run_plan(plan)
    assert all(r.status == "skipped" for r in second.results if r.action.category == "harness")


def test_self_merge_leaves_malformed_permissions_deny_untouched(fake_agent_tools, tmp_path):
    # a NON-list permissions.deny is a shape error, same as a malformed allow: apply_harness must not
    # overwrite it, and drift must not add a second 'ship rules absent' row for it.
    repo = tmp_path / "repo"
    repo.mkdir()
    _user_settings().parent.mkdir(parents=True, exist_ok=True)
    _user_settings().write_text(
        json.dumps({"permissions": {"defaultMode": "auto", "allow": ["Bash(docker:*)"], "deny": "oops"}}),
        encoding="utf-8",
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools), cat, project_type="unknown")
    assert not [x for x in detect(plan).items if x.category == "harness" and "ship rules absent" in x.detail]
    run_plan(plan)
    perms = json.loads(_user_settings().read_text(encoding="utf-8"))["permissions"]
    assert perms["deny"] == "oops"
    assert perms["allow"] == ["Bash(docker:*)"]


def test_omp_hooks_dir_is_written_and_drifts_when_a_repo_pins_another_primary_kind(
    fake_agent_tools, tmp_path, monkeypatch
):
    """rig-cli#743 with REAL files: a repo rig.yaml pinning ``harness.kind: claude-code`` over a
    global ``omp`` primary still gets ``~/.omp/agent/hooks`` written by apply, and a stale
    descriptor there is reported by status."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.delenv("PI_CONFIG_DIR", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False},
            "agent_hooks": {"all": True},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "harness": {"kind": "claude-code", "hook_bridge": {"enabled": False}},
        },
        repo_root=repo,
        global_data={"harness": {"kind": "omp"}},
    )
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="cli")
    run_plan(plan)
    omp_descriptor = home / ".omp" / "agent" / "hooks" / "block-no-verify.pre-bash.json"
    assert omp_descriptor.is_file()
    assert not [i for i in detect(plan).items if i.category == "agent_hooks" and i.direction != "extra"]

    spec = json.loads(omp_descriptor.read_text())
    spec["cmd"] = "/stale/hand/copy"
    omp_descriptor.write_text(json.dumps(spec), encoding="utf-8")
    assert any(i.direction == "modified" and i.category == "agent_hooks" for i in detect(plan).items)
    omp_descriptor.unlink()
    assert any(i.direction == "missing" and i.category == "agent_hooks" for i in detect(plan).items)
