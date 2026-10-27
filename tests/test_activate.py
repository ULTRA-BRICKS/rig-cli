"""``rig activate`` — probe the machine, switch on what it can run, report what it cannot.

Hermetic: a fake agent-tools checkout, a throwaway HOME (the suite-wide autouse fixture) and a
``Probe`` whose seams stand in for the machine. The commit tests restrict the plan to the
categories that only copy files, so no test clones a repo, installs a package or loads a service.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from test_profile import BASE_CONFIG, _auto_sync_machine, _load, world  # noqa: F401 — the shared fixture

from riglib import activate
from riglib.cli import main
from riglib.config import global_config_path

_PROBE = activate.Probe  # captured before the tests replace ``activate.Probe`` with a factory
EVERYTHING = {"git", "tmux", "abduco", "omp", "claude", "codex", "opencode", "systemctl"}


def _probe(have=EVERYTHING, platform="linux", service=True, homes=(), cccc=None) -> activate.Probe:
    """``cccc``: the reason no pinned analyzer exists on the fake machine (``None`` = supported)."""
    return _PROBE(
        platform=platform,
        which=lambda name: f"/usr/bin/{name}" if name in have else None,
        exists=lambda path: path in homes,
        systemd_user=lambda: service,
        cccc_unsupported=lambda: cccc,
    )


def _by_id(act: activate.Activation) -> dict[str, activate.Area]:
    return {area.id: area for area in act.areas}


# ── probing ──────────────────────────────────────────────────────────────────────────────────
def test_a_fully_equipped_machine_activates_everything_the_config_does_not_mention():
    act = activate.build_activation({}, _probe())
    areas = _by_id(act)
    assert {a.status for a in act.areas if a.id not in ("tg_ctl", "tcc_watch", "tgctl_reaper", "spotlight")} == {
        activate.ACTIVATE
    }
    assert act.config_changes == [
        "harness: kind omp, kinds claude-code, codex, opencode",
        "tmux.enabled: true",
        "harness_wrappers.enabled: true",
        "tools.enabled: true",
        "tools.items.ctmux: {}",
    ]
    assert act.doc["harness"] == {
        "enabled": True, "kind": "omp", "auto_mode": True, "kinds": ["claude-code", "codex", "opencode"],
    }
    assert act.doc["tools"] == {"enabled": True, "items": {"ctmux": {}}}
    assert "omp" in areas["agent_hooks"].detail
    assert {"skills", "agent_hooks", "harness", "tmux", "sysmon", "tools", "lint_checks_machine"} <= act.included


def test_existing_config_keys_are_never_overridden():
    doc = {
        "version": 1,
        "harness": {"kind": "claude-code"},
        "tmux": {"enabled": False},
        "harness_wrappers": {"enabled": False},
        "tools": {"enabled": True, "items": {"ctmux": {"enabled": False}, "tg": {}}},
    }
    act = activate.build_activation(doc, _probe())
    assert act.config_changes == []
    assert act.doc == doc
    assert act.doc["tmux"] == {"enabled": False}


def test_a_tools_block_that_is_not_enabled_is_left_alone():
    act = activate.build_activation({"version": 1, "tools": {"items": {"tg": {}}}}, _probe())
    assert _by_id(act)["tools"].status == activate.LEFT_ALONE
    assert "tools" not in act.included
    assert act.doc["tools"] == {"items": {"tg": {}}}


def test_ctmux_is_added_to_an_enabled_tools_block_exactly_once():
    doc = {"version": 1, "tools": {"enabled": True, "items": {"tg": {}}}}
    act = activate.build_activation(doc, _probe())
    assert act.config_changes == ["harness: kind omp, kinds claude-code, codex, opencode", "tmux.enabled: true",
                                  "harness_wrappers.enabled: true", "tools.items.ctmux: {}"]
    again = activate.build_activation(act.doc, _probe())
    assert again.config_changes == []


def test_what_the_machine_lacks_is_listed_with_the_reason_and_excluded():
    act = activate.build_activation({}, _probe(have=set(), service=False))
    areas = _by_id(act)
    reasons = {a.id: a.detail for a in act.unsupported}
    assert "git is not installed" in reasons["git_hooks"] and "git is not installed" in reasons["tools"]
    assert "no coding-agent harness found" in areas["harness"].detail
    assert areas["harness"].status == activate.ACTIVATE  # global instructions need no installed binary
    assert "tmux is not installed" in reasons["tmux"]
    assert "abduco" in reasons["harness_wrappers"]
    assert "no service manager" in reasons["sysmon"] and "no service manager" in reasons["models"]
    assert all("macOS only" in reasons[a] for a in ("tg_ctl", "tcc_watch", "tgctl_reaper", "spotlight"))
    assert areas["skills"].status == activate.ACTIVATE
    assert act.config_changes == []  # nothing to switch on: no harness, tmux or abduco here
    assert not ({"harness", "tmux", "sysmon", "tools", "git_hooks"} & act.included)
    assert {"skills", "agent_hooks", "env", "instructions"} <= act.included


def test_the_lint_check_machine_area_owns_the_analyzer_and_hook_category():
    act = activate.build_activation({}, _probe())
    area = _by_id(act)["lint_checks"]
    assert area.status == activate.ACTIVATE and area.categories == frozenset({"lint_checks_machine"})
    assert "lint_checks_machine" in act.included and "lint_checks" in activate.AREA_IDS


def test_an_unsupported_analyzer_platform_is_listed_with_the_reason_never_an_error():
    reason = "no pinned cccc release for plan9/mips"
    act = activate.build_activation({}, _probe(cccc=reason))
    area = _by_id(act)["lint_checks"]
    assert area.status == activate.UNSUPPORTED and area.detail == reason
    assert "lint_checks_machine" not in act.included
    assert "lint_checks" in [a.id for a in act.unsupported]


def test_the_real_probe_reports_the_pin_table_verdict(monkeypatch):
    from riglib import cccc_install

    monkeypatch.setattr(cccc_install, "platform_key", lambda: ("plan9", "mips"))
    assert "plan9/mips" in activate.Probe().cccc_unsupported()
    monkeypatch.setattr(cccc_install, "platform_key", lambda: ("linux", "x86_64"))
    assert activate.Probe().cccc_unsupported() is None


def test_a_global_opt_out_leaves_the_lint_check_area_alone():
    act = activate.build_activation({"version": 1, "lint_checks": {"enabled": False}}, _probe())
    assert _by_id(act)["lint_checks"].status == activate.LEFT_ALONE
    assert "lint_checks_machine" not in act.included
    assert not any("lint_checks" in change for change in act.config_changes)  # an explicit off switch is never overridden


def test_macos_gets_launchd_areas_and_needs_no_systemd():
    act = activate.build_activation({"version": 1, "harness": {"kind": "omp"}}, _probe(platform="darwin", service=False))
    areas = _by_id(act)
    assert areas["sysmon"].status == activate.ACTIVATE and areas["tg_ctl"].status == activate.ACTIVATE


def test_a_harness_is_detected_from_its_config_dir_too():
    act = activate.build_activation({}, _probe(have={"git"}, homes={"~/.omp"}))
    assert act.doc["harness"]["kind"] == "omp" and "kinds" not in act.doc["harness"]


def test_the_rendered_table_names_every_area_and_the_config_changes():
    act = activate.build_activation({}, _probe(have={"git", "tmux"}, service=False))
    text = "\n".join(activate.render(act, preview=True))
    assert "✓ skills" in text and "✗ sysmon" in text and "+ tmux.enabled: true" in text
    assert "will activate" in text
    assert "activating" in "\n".join(activate.render(act, preview=False))


# ── the command ──────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def machine(fake_agent_tools, tmp_path, monkeypatch):
    """A global config pointing at the fake catalog, a probe that sees git + tmux, and a plan
    restricted to the file-copying categories."""
    gpath = global_config_path()
    gpath.parent.mkdir(parents=True, exist_ok=True)
    gpath.write_text(f"version: 1\nagent_tools_source: {fake_agent_tools.as_posix()}\n", encoding="utf-8")
    monkeypatch.setattr(activate, "Probe", lambda: _probe(have={"git", "tmux", "omp"}))
    monkeypatch.setenv("RIG_TOOLS_DRY_RUN", "1")  # ctmux is only reported, never cloned
    return gpath


def _run(*argv, capsys):
    rc = main(["activate", *argv])
    out = capsys.readouterr()
    return rc, out.out + out.err


def test_preview_changes_nothing_and_points_at_commit(machine, capsys):
    before = machine.read_bytes()
    rc, out = _run(capsys=capsys)
    assert rc == 0, out
    assert "PREVIEW (nothing was changed)" in out and "rig activate commit" in out
    assert "+ tmux.enabled: true" in out and "+ tools.items.ctmux: {}" in out
    assert machine.read_bytes() == before
    assert not (Path.home() / ".agents").exists() and not (Path.home() / ".omp").exists()


def test_preview_json_is_machine_readable(machine, capsys):
    rc, out = _run("--json", capsys=capsys)
    assert rc == 0, out
    data = json.loads(out)
    assert data["mode"] == "info" and "tmux.enabled: true" in data["config_changes"]
    assert "harness_wrappers" in data["unsupported"]  # abduco is not in the test probe
    assert {a["id"] for a in data["areas"]} >= {"skills", "agent_hooks", "tmux", "tools"}


def test_json_cannot_be_combined_with_commit(machine, capsys):
    rc, out = _run("commit", "--json", capsys=capsys)
    assert rc == 2 and "preview-only" in out


def test_dry_run_under_commit_is_still_a_preview(machine, capsys):
    before = machine.read_bytes()
    rc, out = _run("commit", "--dry-run", capsys=capsys)
    assert rc == 0 and "PREVIEW" in out
    assert machine.read_bytes() == before


def test_commit_writes_the_switches_applies_the_plan_and_is_idempotent(machine, capsys):
    rc, out = _run("commit", "--only", "skills,agent_hooks,harness,tmux,tools", capsys=capsys)
    assert rc == 0, out
    doc = yaml.safe_load(machine.read_text(encoding="utf-8"))
    assert doc["tmux"] == {"enabled": True}
    assert doc["tools"] == {"enabled": True, "items": {"ctmux": {}}}
    assert doc["harness"]["kind"] == "omp"
    assert doc["agent_tools_source"]  # what was there is kept
    assert list(machine.parent.glob("config.yaml.rig-bak-*"))  # the previous file was backed up
    # omp is the harness of this machine: its hooks dir is maintained (rig-cli#743)
    assert list((Path.home() / ".omp" / "agent" / "hooks").glob("*.json"))

    rc2, out2 = _run("commit", "--only", "skills,agent_hooks,harness,tmux,tools", capsys=capsys)
    assert rc2 == 0, out2
    assert "0 changed" in out2
    assert not list(machine.parent.glob("config.yaml.rig-bak-*.1"))  # the second run wrote no config


def test_commit_keeps_an_explicit_off_switch(machine, capsys):
    machine.write_text(machine.read_text(encoding="utf-8") + "tmux:\n  enabled: false\n", encoding="utf-8")
    rc, out = _run("commit", "--only", "skills,tmux", capsys=capsys)
    assert rc == 0, out
    assert yaml.safe_load(machine.read_text(encoding="utf-8"))["tmux"] == {"enabled": False}


def test_commit_without_any_global_config_creates_one(fake_agent_tools, monkeypatch, capsys):
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setattr(activate, "Probe", lambda: _probe(have={"git"}))
    monkeypatch.setenv("RIG_TOOLS_DRY_RUN", "1")
    assert not global_config_path().exists()
    rc, out = _run("commit", "--only", "skills,tools", capsys=capsys)
    assert rc == 0, out
    doc = yaml.safe_load(global_config_path().read_text(encoding="utf-8"))
    assert doc["version"] == 1 and doc["tools"]["items"] == {"ctmux": {}}


def test_a_missing_catalog_is_a_clear_error_not_a_crash(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("RIG_AGENT_TOOLS_SOURCE", raising=False)
    gpath = global_config_path()
    gpath.parent.mkdir(parents=True, exist_ok=True)
    gpath.write_text(f"version: 1\nagent_tools_source: {tmp_path / 'nowhere'}\n", encoding="utf-8")
    rc, out = _run(capsys=capsys)
    assert rc == 2 and "agent_tools_source" in out


def test_commit_syncs_the_profile_first_even_without_auto_sync(world, fake_agent_tools, monkeypatch, capsys):  # noqa: F811
    from riglib import profile

    local = world.machine("a")
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools.as_posix()}\n" + BASE_CONFIG.split("\n", 1)[1],
        encoding="utf-8",
    )
    assert profile.init(world.url, auto_sync=False, notify=world.notify).result == "pushed"
    assert not profile.auto_sync_enabled()
    remote = _load(local)
    remote["git"]["config"]["init.defaultBranch"] = "trunk"
    world.push_remote_edit(yaml.safe_dump(remote))
    world.machine("a")
    monkeypatch.setattr(activate, "Probe", lambda: _probe(have={"git"}))
    rc, out = _run("commit", "--only", "skills", capsys=capsys)
    assert rc == 0, out
    assert "pulled" in out
    assert _load(local)["git"]["config"]["init.defaultBranch"] == "trunk"


def test_commit_names_the_areas_the_machine_cannot_support(fake_agent_tools, monkeypatch, capsys):
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setattr(activate, "Probe", lambda: _probe(have=set(), service=False))
    rc, out = _run("commit", "--only", "skills,tmux,sysmon", capsys=capsys)
    assert rc == 0, out
    assert "could not activate on this machine" in out
    assert "tmux: tmux is not installed" in out and "sysmon: no service manager" in out


def test_only_rejects_an_unknown_area(machine, capsys):
    rc, out = _run("--only", "skills,bogus", capsys=capsys)
    assert rc == 2 and "bogus" in out and "valid:" in out


def test_only_limits_what_is_listed_and_written(machine, capsys):
    rc, out = _run("--only", "tmux", capsys=capsys)
    assert rc == 0, out
    assert "✓ tmux" in out and "skills" not in out.split("config changes")[0].split("areas:")[1]
    assert "tools.items.ctmux" not in out


def test_the_preview_reports_an_unconfigured_profile_without_touching_it(machine, capsys):
    rc, out = _run(capsys=capsys)
    assert rc == 0 and "profile: not configured" in out


def test_commit_activates_the_lint_check_analyzer_and_hook_through_the_one_engine(machine, capsys):
    machine.write_text(machine.read_text(encoding="utf-8") + "git_hooks:\n  dispatcher:\n    enabled: true\n", encoding="utf-8")
    rc, out = _run("commit", "--only", "lint_checks", capsys=capsys)  # RIG_CCCC_DRY_RUN=1 (suite-wide): nothing is downloaded
    assert rc == 0, out
    assert "✓ lint_checks" in out
    (log,) = (Path.home() / ".cache" / "rig").glob("apply-*.log")
    text = log.read_text(encoding="utf-8")
    assert "lint_checks_machine/cccc" in text and "lint_checks_machine/pre-commit-hook" in text
    assert "lint_checks/ci-workflow" not in text  # a machine-level activation never plans a repo's workflow
    assert (Path.home() / ".config" / "git" / "global-hooks.d" / "pre-commit" / "30-lint-check").is_file()


def test_commit_skips_an_unsupported_analyzer_platform_without_failing(fake_agent_tools, monkeypatch, capsys):
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setattr(activate, "Probe", lambda: _probe(cccc="no pinned cccc release for plan9/mips"))
    rc, out = _run("commit", "--only", "lint_checks", capsys=capsys)
    assert rc == 0, out
    assert "lint_checks: no pinned cccc release for plan9/mips" in out and "BLOCKED" not in out
