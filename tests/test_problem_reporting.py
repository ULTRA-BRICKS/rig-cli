"""The global reporting requirement must reach the files each client actually reads.

These tests run the real planner, file writer, and drift detector in a temporary HOME;
they catch a missing target, stale policy, destructive splice, or false clean status.
"""

from pathlib import Path

import pytest

from riglib.actions import run_plan
from riglib.catalog import Catalog
from riglib.config import LoadedConfig
from riglib.drift import detect
from riglib.plan import InstallPlan, build


BEGIN = "# >>> rig-managed problem reporting (do not edit) >>>"
END = "# <<< rig-managed problem reporting (do not edit) <<<"
# English translation of the user's concrete inadequate report (tg#25331).
INADEQUATE_REPORT = "Guardian home: watched chat 6ae07d22 has no open tab (under observation)"


@pytest.fixture(autouse=True)
def _isolate_opencode_flags(monkeypatch):
    monkeypatch.delenv("OPENCODE_DISABLE_CLAUDE_CODE", raising=False)
    monkeypatch.delenv("OPENCODE_DISABLE_CLAUDE_CODE_PROMPT", raising=False)


def _plan(source, repo, *, harness=None, global_harness=None, on_conflict="backup"):
    # Keep the simulated repository separate from the temporary HOME, as on a real machine.
    repo_root = repo / "project"
    repo_root.mkdir(exist_ok=True)
    data = {
        "version": 1,
        "skills": {"enabled": False},
        "agent_hooks": {"enabled": False},
        "permissions": {"enabled": False},
        "agents_md": {"enabled": False},
        "ship_delegator": {"enabled": False},
    }
    if harness is not None:
        data["harness"] = harness
    config = LoadedConfig(
        data=data,
        repo_root=repo_root,
        global_data={"harness": global_harness} if global_harness else {},
    )
    full = build(config, Catalog.scan(str(source)), project_type="unknown")
    return InstallPlan(
        actions=[a for a in full.actions if a.category == "instructions"],
        on_conflict=on_conflict,
        notes=full.notes,
    )


@pytest.mark.parametrize("kind,relative", [
    ("claude-code", ".claude/CLAUDE.md"),
    ("codex", ".codex/AGENTS.md"),
    ("pi", ".pi/agent/AGENTS.md"),
    ("commandcode", ".commandcode/AGENTS.md"),
    ("omp", ".omp/agent/RULES.md"),
    ("opencode", ".config/opencode/AGENTS.md"),
])
def test_fresh_apply_installs_full_reporting_requirement(fake_agent_tools, tmp_path, kind, relative):
    plan = _plan(fake_agent_tools, tmp_path, harness={"kind": kind})
    target = Path.home() / relative
    assert [a.target for a in plan.actions] == [target]
    assert [(i.direction, i.target) for i in detect(plan).items] == [("missing", target)]
    assert not target.exists()  # planning and drift remain read-only
    report = run_plan(plan)
    assert not report.errors
    assert report.changed == 1
    policy = target.read_text()
    for required in (
        "actions already attempted", "observed results", "action already started",
        "exact dependency", "next verification point", '"under observation"',
        "A task ID, assigned helper, or promise alone is not remediation",
    ):
        assert required in policy
    assert not detect(plan).items
    before = target.stat().st_mtime_ns
    assert run_plan(plan).changed == 0
    assert target.stat().st_mtime_ns == before


def test_default_harness_gets_policy_without_repo_guide_or_permission_actions(fake_agent_tools, tmp_path):
    plan = _plan(fake_agent_tools, tmp_path)
    assert [a.target for a in plan.actions] == [Path.home() / ".claude/CLAUDE.md"]
    assert not run_plan(plan).errors
    assert not (tmp_path / "AGENTS.md").exists()


def test_disabled_harness_emits_no_instruction_writes(fake_agent_tools, tmp_path):
    plan = _plan(fake_agent_tools, tmp_path, harness={"enabled": False, "kind": "codex"})
    assert not plan.actions


def test_global_and_repo_harnesses_both_receive_policy(fake_agent_tools, tmp_path):
    plan = _plan(
        fake_agent_tools, tmp_path,
        harness={"kind": "codex", "kinds": ["pi", "codex"]},
        global_harness={"kind": "claude-code", "kinds": ["commandcode", "codex"]},
    )
    assert {a.target for a in plan.actions} == {
        Path.home() / ".codex/AGENTS.md", Path.home() / ".pi/agent/AGENTS.md",
        Path.home() / ".claude/CLAUDE.md", Path.home() / ".commandcode/AGENTS.md",
    }
    assert len(plan.actions) == 4
    assert not run_plan(plan).errors


def test_claude_rotated_and_explicit_config_dirs_receive_policy(fake_agent_tools, tmp_path):
    home = Path.home()
    (home / ".claude-accounts/account-2").mkdir(parents=True)
    (home / ".claude-accounts/account-10").mkdir()
    (home / ".claude-accounts/current").write_text("account-2\n")
    plan = _plan(fake_agent_tools, tmp_path, harness={
        "kind": "claude-code",
        "settings_paths": ["~/custom-claude/settings.json", "~/.claude-accounts/account-2/settings.json"],
    })
    assert [a.target for a in plan.actions] == [
        home / ".claude/CLAUDE.md", home / "custom-claude/CLAUDE.md",
        home / ".claude-accounts/account-2/CLAUDE.md", home / ".claude-accounts/account-10/CLAUDE.md",
    ]
    assert len({a.item for a in plan.actions}) == 4
    assert not run_plan(plan).errors
    assert not detect(plan).items


def test_claude_discovery_opt_out_still_honors_explicit_dirs(fake_agent_tools, tmp_path):
    (Path.home() / ".claude-accounts/account-0").mkdir(parents=True)
    plan = _plan(fake_agent_tools, tmp_path, harness={
        "kind": "claude-code", "discover_config_dirs": False,
        "settings_paths": ["~/custom-claude/settings.json"],
    })
    assert {a.target for a in plan.actions} == {
        Path.home() / ".claude/CLAUDE.md", Path.home() / "custom-claude/CLAUDE.md",
    }


@pytest.mark.parametrize("settings_path", [
    "~/custom-claude/settings.json", "~/custom-claude", "$REPORTING_CLAUDE_HOME/settings.json",
])
def test_claude_custom_primary_receives_policy_and_drift(
    fake_agent_tools, tmp_path, monkeypatch, settings_path,
):
    custom = Path.home() / "custom-claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
    monkeypatch.setenv("REPORTING_CLAUDE_HOME", str(custom))
    (Path.home() / ".claude-accounts/account-0").mkdir(parents=True)
    plan = _plan(fake_agent_tools, tmp_path, harness={
        "kind": "claude-code", "settings_path": settings_path, "discover_config_dirs": False,
    })
    expected = {Path.home() / ".claude/CLAUDE.md", custom / "CLAUDE.md"}
    assert {a.target for a in plan.actions} == expected
    assert {i.target for i in detect(plan).items} == expected
    report = run_plan(plan)
    assert not report.errors
    assert report.changed == 2
    assert "actions already attempted" in (custom / "CLAUDE.md").read_text()
    assert not detect(plan).items
    assert run_plan(plan).changed == 0
    (custom / "CLAUDE.md").write_text("# Custom user instructions without the policy\n")
    assert [i.target for i in detect(plan).items] == [custom / "CLAUDE.md"]
    assert not run_plan(plan).errors
    assert not detect(plan).items


@pytest.mark.parametrize("relative,absolute", [
    (".claude/settings.json", False), (".claude", False),
    ("custom/settings.json", True), ("custom", True),
])
def test_claude_project_primary_does_not_become_global_instructions(
    fake_agent_tools, tmp_path, relative, absolute,
):
    repo = tmp_path / "project"
    settings_path = str(repo / relative) if absolute else relative
    plan = _plan(fake_agent_tools, tmp_path, harness={
        "kind": "claude-code", "settings_path": settings_path, "discover_config_dirs": False,
    })
    assert [a.target for a in plan.actions] == [Path.home() / ".claude/CLAUDE.md"]
    assert not run_plan(plan).errors
    assert not list(repo.rglob("CLAUDE.md"))


def test_additive_claude_does_not_use_other_primary_harness_settings_path(fake_agent_tools, tmp_path):
    plan = _plan(fake_agent_tools, tmp_path, harness={
        "kind": "opencode", "kinds": ["claude-code"],
        "settings_path": "~/custom-opencode/bridge.js", "discover_config_dirs": False,
    })
    assert {a.target for a in plan.actions} == {
        Path.home() / ".config/opencode/AGENTS.md", Path.home() / ".claude/CLAUDE.md",
    }


def test_global_claude_custom_primary_survives_repo_harness_override(fake_agent_tools, tmp_path):
    plan = _plan(
        fake_agent_tools, tmp_path,
        harness={"kind": "codex", "settings_path": "~/.codex/config.toml"},
        global_harness={
            "kind": "claude-code", "settings_path": "~/custom-claude",
            "discover_config_dirs": False,
        },
    )
    assert {a.target for a in plan.actions} == {
        Path.home() / ".codex/AGENTS.md", Path.home() / ".claude/CLAUDE.md",
        Path.home() / "custom-claude/CLAUDE.md",
    }
    assert not run_plan(plan).errors
    assert not detect(plan).items


@pytest.mark.parametrize("kind,env,relative", [
    ("codex", "RIG_CODEX_HOME", "custom-codex/AGENTS.md"),
    ("pi", "PI_CODING_AGENT_DIR", "custom-pi/AGENTS.md"),
])
def test_instruction_targets_follow_client_root_override(
    fake_agent_tools, tmp_path, monkeypatch, kind, env, relative,
):
    monkeypatch.setenv(env, str(Path.home() / Path(relative).parent))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "transient-codex"))
    plan = _plan(fake_agent_tools, tmp_path, harness={"kind": kind})
    assert [a.target for a in plan.actions] == [Path.home() / relative]
    assert not run_plan(plan).errors
    assert not (tmp_path / "transient-codex/AGENTS.md").exists()


def test_stale_policy_is_replaced_and_user_bytes_and_backup_survive(fake_agent_tools, tmp_path):
    plan = _plan(fake_agent_tools, tmp_path, harness={"kind": "codex"})
    target = Path.home() / ".codex/AGENTS.md"
    target.parent.mkdir()
    prefix, suffix = b"# My instructions\r\nKeep my notes.\r\n\r\n", b"\r\n# My appendix\r\n"
    stale = f"{BEGIN}\n{INADEQUATE_REPORT}\n{END}\n".encode()
    before = prefix + stale + suffix
    target.write_bytes(before)
    assert [(i.direction, i.target) for i in detect(plan).items] == [("modified", target)]
    report = run_plan(plan)
    assert not report.errors
    assert report.changed == 1
    result = report.results[0]
    assert result.status == "backed_up"
    assert result.backup.read_bytes() == before
    after = target.read_bytes()
    assert after.startswith(prefix) and after.endswith(suffix)
    assert INADEQUATE_REPORT.encode() not in after
    assert b"actions already attempted" in after
    assert not detect(plan).items
    assert run_plan(plan).changed == 0


def test_append_preserves_existing_unmanaged_bytes(fake_agent_tools, tmp_path):
    plan = _plan(fake_agent_tools, tmp_path, harness={"kind": "codex"})
    target = Path.home() / ".codex/AGENTS.md"
    target.parent.mkdir()
    before = b"# Personal instructions\r\nDo not discard this.\r\n\r\n\r\n"
    target.write_bytes(before)
    assert not run_plan(plan).errors
    assert target.read_bytes().startswith(before)
    assert BEGIN.encode() in target.read_bytes()
    assert not detect(plan).items


def test_skip_leaves_stale_policy_visible_as_drift(fake_agent_tools, tmp_path):
    plan = _plan(fake_agent_tools, tmp_path, harness={"kind": "codex"}, on_conflict="skip")
    target = Path.home() / ".codex/AGENTS.md"
    target.parent.mkdir()
    before = f"{BEGIN}\nOld policy.\n{END}\n"
    target.write_text(before)
    report = run_plan(plan)
    assert not report.errors and report.changed == 0
    assert target.read_text() == before
    assert [(i.direction, i.target) for i in detect(plan).items] == [("modified", target)]


@pytest.mark.parametrize("content", [
    f"{BEGIN}\nUnfinished block\n", f"{END}\n{BEGIN}\n", f"{BEGIN}\n{BEGIN}\n{END}\n{END}\n",
])
def test_malformed_markers_fail_without_changing_user_content(fake_agent_tools, tmp_path, content):
    plan = _plan(fake_agent_tools, tmp_path, harness={"kind": "codex"})
    target = Path.home() / ".codex/AGENTS.md"
    target.parent.mkdir()
    target.write_text(content)
    assert detect(plan).items
    report = run_plan(plan)
    assert len(report.errors) == 1
    assert "markers" in report.errors[0].detail
    assert target.read_text() == content


def test_duplicate_blocks_converge_without_removing_intervening_notes(fake_agent_tools, tmp_path):
    plan = _plan(fake_agent_tools, tmp_path, harness={"kind": "codex"})
    target = Path.home() / ".codex/AGENTS.md"
    target.parent.mkdir()
    block = f"{BEGIN}\nOld requirement.\n{END}\n"
    target.write_text(block + "\nUser middle section.\n\n" + block)
    assert not run_plan(plan).errors
    assert target.read_text().count(BEGIN) == 1
    assert "User middle section." in target.read_text()
    assert not detect(plan).items


def test_symlinked_instruction_file_keeps_alias_and_updates_read_target(fake_agent_tools, tmp_path):
    plan = _plan(fake_agent_tools, tmp_path, harness={"kind": "codex"})
    target = Path.home() / ".codex/AGENTS.md"
    target.parent.mkdir()
    real = tmp_path / "shared-instructions.md"
    real.write_text("# Shared user instructions\n")
    real.chmod(0o640)
    target.symlink_to(real)
    assert not run_plan(plan).errors
    assert target.is_symlink()
    assert "actions already attempted" in real.read_text()
    assert real.stat().st_mode & 0o777 == 0o640
    assert not detect(plan).items


def test_harness_activation_includes_global_instructions():
    from riglib.activate import Probe, build_activation

    probe = Probe(which=lambda name: None, exists=lambda path: False, systemd_user=lambda: False)
    activation = build_activation({"harness": {"kind": "codex"}}, probe, frozenset({"harness"}))
    assert "instructions" in activation.included


def test_omp_override_and_opencode_xdg_are_used(fake_agent_tools, tmp_path, monkeypatch):
    monkeypatch.setenv("PI_CODING_AGENT_DIR", "custom-omp")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(Path.home() / "custom-xdg"))
    plan = _plan(fake_agent_tools, tmp_path, harness={"kind": "omp", "kinds": ["opencode"]})
    assert {a.target for a in plan.actions} == {
        Path.home() / "custom-omp/RULES.md", Path.home() / "custom-xdg/opencode/AGENTS.md",
    }
    assert not run_plan(plan).errors
    assert not detect(plan).items


def test_cli_scoped_preview_commit_and_repeat_leave_repo_and_settings_untouched(
    fake_agent_tools, tmp_path, capsys,
):
    from riglib.cli import main

    config = tmp_path / "rig.yaml"
    config.write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "harness: {kind: codex}\nskills: {enabled: false}\nagent_hooks: {enabled: false}\n"
        "permissions: {enabled: false}\nship_delegator: {enabled: false}\n"
    )
    args = ["-C", str(tmp_path), "--only", "instructions"]
    target = Path.home() / ".codex/AGENTS.md"
    assert main(["apply", "info", *args]) == 0
    assert "instructions/codex" in capsys.readouterr().out
    assert not target.exists()
    assert main(["apply", "commit", *args, "--yes"]) == 0
    capsys.readouterr()
    assert "actions already attempted" in target.read_text()
    before = target.stat().st_mtime_ns
    assert main(["apply", "commit", *args, "--yes"]) == 0
    assert "0 changed" in capsys.readouterr().out
    assert target.stat().st_mtime_ns == before
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (Path.home() / ".codex/config.toml").exists()


def test_masked_invalid_global_settings_paths_do_not_break_instruction_plan(fake_agent_tools, tmp_path):
    # A repo override is validated after merging; a masked value in global_data may be invalid.
    plan = _plan(
        fake_agent_tools, tmp_path,
        harness={"kind": "claude-code", "settings_paths": []},
        global_harness={"kind": "claude-code", "settings_paths": [{"invalid": "path"}]},
    )
    assert [a.target for a in plan.actions] == [Path.home() / ".claude/CLAUDE.md"]


@pytest.mark.parametrize("shape", ["loop", "dangling", "directory", "invalid-utf8"])
def test_unusable_instruction_target_is_reported_without_clobbering_it(fake_agent_tools, tmp_path, shape):
    target = Path.home() / ".codex/AGENTS.md"
    target.parent.mkdir()
    if shape == "loop":
        target.symlink_to(target)
    elif shape == "dangling":
        target.symlink_to(tmp_path / "missing-parent/guide.md")
    elif shape == "directory":
        target.mkdir()
    else:
        target.write_bytes(b"\xff\xfe")
    plan = _plan(fake_agent_tools, tmp_path, harness={"kind": "codex"})
    assert [(i.direction, i.target) for i in detect(plan).items] == [("modified", target)]
    assert len(run_plan(plan).errors) == 1
    assert detect(plan).items
    if shape in {"loop", "dangling"}:
        assert target.is_symlink()
    elif shape == "directory":
        assert target.is_dir()
    else:
        assert target.read_bytes() == b"\xff\xfe"


def test_stat_permission_error_is_visible_in_drift(fake_agent_tools, tmp_path, monkeypatch):
    plan = _plan(fake_agent_tools, tmp_path, harness={"kind": "codex"})
    target = Path.home() / ".codex/AGENTS.md"
    original_exists = Path.exists

    def denied_exists(path):
        if path == target:
            raise PermissionError("instruction directory is not readable")
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", denied_exists)
    items = detect(plan).items
    assert [(i.direction, i.target) for i in items] == [("modified", target)]
    assert "not readable" in items[0].detail
    assert len(run_plan(plan).errors) == 1


def test_failed_atomic_install_keeps_original_user_bytes_and_cleans_temp(fake_agent_tools, tmp_path, monkeypatch):
    import os

    plan = _plan(fake_agent_tools, tmp_path, harness={"kind": "codex"})
    target = Path.home() / ".codex/AGENTS.md"
    target.parent.mkdir()
    before = b"# My irreplaceable instructions\r\n"
    target.write_bytes(before)

    def fail_replace(source, destination):
        raise OSError("simulated disk failure while installing completed file")

    monkeypatch.setattr(os, "replace", fail_replace)
    assert len(run_plan(plan).errors) == 1
    assert target.read_bytes() == before
    assert list(target.parent.iterdir()) == [target]


def test_opencode_updates_existing_claude_fallback_without_shadowing_it(fake_agent_tools, tmp_path):
    fallback = Path.home() / ".claude/CLAUDE.md"
    fallback.parent.mkdir()
    before = "# Existing global rules used by OpenCode\n"
    fallback.write_text(before)
    native = Path.home() / ".config/opencode/AGENTS.md"
    plan = _plan(fake_agent_tools, tmp_path, harness={"kind": "opencode"})
    assert [a.target for a in plan.actions] == [fallback]
    assert not run_plan(plan).errors
    assert fallback.read_text().startswith(before)
    assert "actions already attempted" in fallback.read_text()
    assert not native.exists()
    assert not detect(plan).items


def test_opencode_native_file_wins_and_keeps_claude_fallback_unchanged(fake_agent_tools, tmp_path):
    native = Path.home() / ".config/opencode/AGENTS.md"
    fallback = Path.home() / ".claude/CLAUDE.md"
    native.parent.mkdir(parents=True)
    fallback.parent.mkdir()
    native.write_text("# My OpenCode rules\n")
    fallback.write_text("# My Claude rules\n")
    plan = _plan(fake_agent_tools, tmp_path, harness={"kind": "opencode"})
    assert [a.target for a in plan.actions] == [native]
    assert not run_plan(plan).errors
    assert native.read_text().startswith("# My OpenCode rules\n")
    assert fallback.read_text() == "# My Claude rules\n"


@pytest.mark.parametrize("flag", ["OPENCODE_DISABLE_CLAUDE_CODE", "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT"])
def test_opencode_disabled_claude_fallback_uses_native_file(fake_agent_tools, tmp_path, monkeypatch, flag):
    monkeypatch.setenv(flag, "1")
    fallback = Path.home() / ".claude/CLAUDE.md"
    fallback.parent.mkdir()
    fallback.write_text("# Claude only\n")
    native = Path.home() / ".config/opencode/AGENTS.md"
    plan = _plan(fake_agent_tools, tmp_path, harness={"kind": "opencode"})
    assert [a.target for a in plan.actions] == [native]
    assert not run_plan(plan).errors
    assert fallback.read_text() == "# Claude only\n"


def test_opencode_keeps_broken_native_alias_visible_instead_of_using_fallback(fake_agent_tools, tmp_path):
    native = Path.home() / ".config/opencode/AGENTS.md"
    fallback = Path.home() / ".claude/CLAUDE.md"
    native.parent.mkdir(parents=True)
    native.symlink_to(tmp_path / "missing.md")
    fallback.parent.mkdir()
    fallback.write_text("# Existing fallback\n")
    plan = _plan(fake_agent_tools, tmp_path, harness={"kind": "opencode"})
    assert [a.target for a in plan.actions] == [native]
    assert len(run_plan(plan).errors) == 1
    assert fallback.read_text() == "# Existing fallback\n"
    assert detect(plan).items


def test_opencode_malformed_fallback_stays_visible_and_is_not_shadowed(fake_agent_tools, tmp_path):
    fallback = Path.home() / ".claude/CLAUDE.md"
    fallback.parent.mkdir()
    before = f"# My rules\n{BEGIN}\nUnfinished policy.\n"
    fallback.write_text(before)
    plan = _plan(fake_agent_tools, tmp_path, harness={"kind": "opencode"})
    assert [a.target for a in plan.actions] == [fallback]
    assert len(run_plan(plan).errors) == 1
    assert fallback.read_text() == before
    assert not (Path.home() / ".config/opencode/AGENTS.md").exists()


def test_opencode_and_claude_share_one_policy_write_when_using_fallback(fake_agent_tools, tmp_path):
    fallback = Path.home() / ".claude/CLAUDE.md"
    fallback.parent.mkdir()
    fallback.write_text("# Shared global instructions\n")
    plan = _plan(fake_agent_tools, tmp_path, harness={"kind": "claude-code", "kinds": ["opencode"]})
    assert [a.target for a in plan.actions] == [fallback]
    assert not run_plan(plan).errors
    assert fallback.read_text().count(BEGIN) == 1
    assert not (Path.home() / ".config/opencode/AGENTS.md").exists()
    assert not detect(plan).items
    assert run_plan(plan).changed == 0


def test_claude_settings_alias_does_not_hide_independent_account_instructions(fake_agent_tools, tmp_path):
    primary = Path.home() / ".claude"
    account = Path.home() / ".claude-accounts/account-2"
    primary.mkdir()
    account.mkdir(parents=True)
    (primary / "settings.json").write_text("{}\n")
    (account / "settings.json").symlink_to(primary / "settings.json")
    (account / "CLAUDE.md").write_text("# Account instructions\n")
    plan = _plan(fake_agent_tools, tmp_path, harness={"kind": "claude-code"})
    assert {a.target for a in plan.actions} == {primary / "CLAUDE.md", account / "CLAUDE.md"}
    assert not run_plan(plan).errors
    assert "actions already attempted" in (account / "CLAUDE.md").read_text()
    assert not detect(plan).items
    (account / "CLAUDE.md").write_text("# Account instructions without policy\n")
    assert [i.target for i in detect(plan).items] == [account / "CLAUDE.md"]


def test_claude_instruction_aliases_are_deduplicated_after_path_conversion(fake_agent_tools, tmp_path):
    primary = Path.home() / ".claude/CLAUDE.md"
    account = Path.home() / ".claude-accounts/account-2/CLAUDE.md"
    primary.parent.mkdir()
    primary.write_text("# Shared instructions\n")
    account.parent.mkdir(parents=True)
    account.symlink_to(primary)
    plan = _plan(fake_agent_tools, tmp_path, harness={"kind": "claude-code"})
    assert [a.target for a in plan.actions] == [primary]
    assert not run_plan(plan).errors
    assert account.is_symlink()
    assert "actions already attempted" in account.read_text()


def test_global_claude_instructions_exclude_project_settings_paths(fake_agent_tools, tmp_path):
    repo = tmp_path / "project"
    plan = _plan(fake_agent_tools, tmp_path, harness={
        "kind": "claude-code",
        "settings_paths": [".claude/settings.json", str(repo / "custom/settings.json"), "~/account-extra/settings.json"],
    })
    assert {a.target for a in plan.actions} == {
        Path.home() / ".claude/CLAUDE.md", Path.home() / "account-extra/CLAUDE.md",
    }
    assert not run_plan(plan).errors
    assert not (repo / ".claude/CLAUDE.md").exists()
    assert not (repo / "custom/CLAUDE.md").exists()


@pytest.mark.parametrize("harness,relative_targets", [
    ("{kind: codex}", [".codex/AGENTS.md"]),
    ("{kind: claude-code, settings_paths: ['~/extra-account/settings.json']}",
     [".claude/CLAUDE.md", "extra-account/CLAUDE.md"]),
])
def test_config_web_global_plan_and_drift_include_reporting_policy(
    fake_agent_tools, monkeypatch, harness, relative_targets,
):
    from riglib.config import global_config_path
    from riglib.config_web_plan import build_scope_plan, compute_scope_drift
    from riglib.config_web_scopes import Scope

    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    config = global_config_path()
    config.parent.mkdir(parents=True)
    config.write_text(f"version: 1\nharness: {harness}\n")
    scope = Scope("global", "Global", None, True)
    scoped = build_scope_plan(scope)
    actions = [a for a in scoped.plan.actions if a.category == "instructions"]
    assert [a.target for a in actions] == [Path.home() / p for p in relative_targets]
    before = compute_scope_drift(scoped)
    assert any(i["category"] == "instructions" for i in before["items"])
    assert not run_plan(InstallPlan(actions=actions)).errors
    after = compute_scope_drift(scoped)
    assert not any(i["category"] == "instructions" for i in after["items"])


def test_activate_without_installed_client_prepares_only_default_instructions():
    from riglib.activate import ACTIVATE, Probe, build_activation

    probe = Probe(which=lambda name: None, exists=lambda path: False, systemd_user=lambda: False)
    activation = build_activation({}, probe, frozenset({"harness"}))
    assert activation.included == frozenset({"instructions"})
    assert activation.areas[0].status == ACTIVATE
    assert "no coding-agent harness found" in activation.areas[0].detail
    assert "harness" not in activation.doc


def test_policy_only_install_does_not_enable_a_nonexistent_client_on_next_activation(fake_agent_tools, tmp_path):
    import os

    from riglib.activate import Probe, build_activation

    probe = Probe(
        which=lambda name: None,
        exists=lambda path: Path(os.path.expanduser(path)).exists(),
        systemd_user=lambda: False,
    )
    first = build_activation({}, probe, frozenset({"harness"}))
    plan = _plan(fake_agent_tools, tmp_path)
    assert not run_plan(plan).errors
    assert (Path.home() / ".claude/CLAUDE.md").exists()
    again = build_activation(first.doc, probe, frozenset({"harness"}))
    assert again.doc == first.doc
    assert again.included == frozenset({"instructions"})
    assert again.config_changes == []
    # A real executable remains decisive, even if the only config file is rig's policy.
    probe.which = lambda name: "/usr/bin/claude" if name == "claude" else None
    installed = build_activation(first.doc, probe, frozenset({"harness"}))
    assert installed.doc["harness"]["kind"] == "claude-code"


def test_policy_only_backup_update_does_not_enroll_a_nonexistent_client(fake_agent_tools, tmp_path):
    import os

    from riglib.activate import Probe, build_activation

    probe = Probe(which=lambda name: None, exists=lambda path: Path(os.path.expanduser(path)).exists())
    plan = _plan(fake_agent_tools, tmp_path)
    assert not run_plan(plan).errors
    target = Path.home() / ".claude/CLAUDE.md"
    target.write_text(target.read_text().replace("observed results", "previous wording"))
    assert run_plan(plan).results[0].status == "backed_up"
    assert list(target.parent.glob("CLAUDE.md.rig-bak-*"))
    activation = build_activation({}, probe, frozenset({"harness"}))
    assert activation.included == frozenset({"instructions"})
    assert "harness" not in activation.doc


@pytest.mark.parametrize("failure_point", ["temporary", "replace"])
def test_failed_fresh_policy_preparation_does_not_enroll_a_nonexistent_client(
    fake_agent_tools, tmp_path, monkeypatch, failure_point,
):
    import os

    from riglib import problem_reporting
    from riglib.activate import Probe, build_activation

    probe = Probe(which=lambda name: None, exists=lambda path: Path(os.path.expanduser(path)).exists())
    before = build_activation({}, probe, frozenset({"harness"}))
    during = []

    def fail(*args, **kwargs):
        during.append(build_activation(before.doc, probe, frozenset({"harness"})).doc)
        raise OSError("injected fresh preparation failure")

    with monkeypatch.context() as patch:
        if failure_point == "temporary":
            patch.setattr(problem_reporting.tempfile, "NamedTemporaryFile", fail)
        else:
            patch.setattr(problem_reporting.os, "replace", fail)
        report = run_plan(_plan(fake_agent_tools, tmp_path))
    assert len(report.errors) == 1
    assert "injected fresh preparation failure" in report.errors[0].detail
    assert list((Path.home() / ".claude").iterdir()) == []
    assert during == [before.doc]
    for _ in range(2):
        again = build_activation(before.doc, probe, frozenset({"harness"}))
        assert again.doc == before.doc
        assert again.included == frozenset({"instructions"})
        assert again.config_changes == []
    assert not run_plan(_plan(fake_agent_tools, tmp_path)).errors
    assert build_activation(before.doc, probe, frozenset({"harness"})).doc == before.doc


@pytest.mark.parametrize("empty_dir", [".omp", ".omp/agent", ".config/opencode"])
def test_empty_native_instruction_directory_chain_is_not_client_evidence(
    fake_agent_tools, tmp_path, empty_dir,
):
    import os

    from riglib.activate import Probe, build_activation

    # A prepared Claude fallback may make OpenCode's effective guide live outside its
    # own empty directory; that directory still cannot prove an installed client.
    assert not run_plan(_plan(fake_agent_tools, tmp_path)).errors
    (Path.home() / empty_dir).mkdir(parents=True)
    probe = Probe(which=lambda name: None, exists=lambda path: Path(os.path.expanduser(path)).exists())
    activation = build_activation({}, probe, frozenset({"harness"}))
    assert activation.included == frozenset({"instructions"})
    assert "harness" not in activation.doc


def test_empty_level_verdict_comes_from_one_listing_even_when_a_writer_races(monkeypatch):
    from riglib.problem_reporting import only_reporting_policy

    # omp's agent dir may be nested under an empty parent. A writer creating that child
    # between two scans of the parent must not turn "nothing but a new directory" into
    # evidence of an installed client.
    home = Path.home()
    profiles = home / ".omp/profiles"
    profiles.mkdir(parents=True)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(profiles / "agent"))
    listings = []
    iterdir = Path.iterdir

    def racing_iterdir(self):
        if self == profiles:
            listings.append(1)
            if len(listings) > 1:
                (profiles / "agent").mkdir(exist_ok=True)
        return iterdir(self)

    monkeypatch.setattr(Path, "iterdir", racing_iterdir)
    assert only_reporting_policy("omp", str(home / ".omp"))


@pytest.mark.parametrize("kind,binary,config", [
    ("claude-code", "claude", ".claude/settings.json"),
    ("omp", "omp", ".omp/agent/config.yml"),
])
def test_empty_preparation_keeps_real_binary_and_client_config_detection(kind, binary, config):
    import os

    from riglib.activate import Probe

    target = Path.home() / config
    target.parent.mkdir(parents=True)
    probe = Probe(which=lambda name: None, exists=lambda path: Path(os.path.expanduser(path)).exists())
    assert probe.detected_harnesses() == []
    probe.which = lambda name: f"/usr/bin/{binary}" if name == binary else None
    assert probe.detected_harnesses() == [kind]
    probe.which = lambda name: None
    target.write_text("{}\n")
    assert probe.detected_harnesses() == [kind]


@pytest.mark.parametrize("umask,expected", [(0o022, 0o644), (0o077, 0o600)])
def test_new_guide_file_follows_the_umask_not_the_temp_file_mode(fake_agent_tools, tmp_path, umask, expected):
    import os

    previous = os.umask(umask)
    try:
        assert not run_plan(_plan(fake_agent_tools, tmp_path, harness={"kind": "codex"})).errors
    finally:
        os.umask(previous)
    assert (Path.home() / ".codex/AGENTS.md").stat().st_mode & 0o777 == expected


def test_policy_temp_file_is_not_mistaken_for_a_client_during_apply(fake_agent_tools, tmp_path, monkeypatch):
    import os

    from riglib.activate import Probe, build_activation

    probe = Probe(which=lambda name: None, exists=lambda path: Path(os.path.expanduser(path)).exists())
    observed = []
    fsync = os.fsync

    def inspect_during_write(fd):
        fsync(fd)
        observed.append(build_activation({}, probe, frozenset({"harness"})).doc)

    monkeypatch.setattr(os, "fsync", inspect_during_write)
    assert not run_plan(_plan(fake_agent_tools, tmp_path)).errors
    assert observed == [{"version": 1}]


def test_hardlinked_user_instructions_are_not_silently_split(fake_agent_tools, tmp_path):
    import os

    target = Path.home() / ".codex/AGENTS.md"
    target.parent.mkdir()
    target.write_text("# Linked user instructions\n")
    alias = Path.home() / "shared-guide.md"
    os.link(target, alias)
    plan = _plan(fake_agent_tools, tmp_path, harness={"kind": "codex"})
    assert [i.direction for i in detect(plan).items] == ["modified"]
    report = run_plan(plan)
    assert len(report.errors) == 1
    assert "hard link" in report.errors[0].detail
    assert alias.stat().st_ino == target.stat().st_ino
    assert target.read_text() == alias.read_text() == "# Linked user instructions\n"


@pytest.mark.parametrize("existed", [False, True])
def test_concurrent_user_edit_before_replace_is_preserved(fake_agent_tools, tmp_path, monkeypatch, existed):
    import os

    target = Path.home() / ".codex/AGENTS.md"
    target.parent.mkdir()
    if existed:
        target.write_text("# Original user instructions\n")
    plan = _plan(fake_agent_tools, tmp_path, harness={"kind": "codex"})
    fsync = os.fsync

    def edit_while_writing(fd):
        fsync(fd)
        target.write_text("# Another session's new instructions\n")

    monkeypatch.setattr(os, "fsync", edit_while_writing)
    report = run_plan(plan)
    assert len(report.errors) == 1
    assert "changed during" in report.errors[0].detail
    assert target.read_text() == "# Another session's new instructions\n"
    assert list(target.parent.iterdir()) == [target]
