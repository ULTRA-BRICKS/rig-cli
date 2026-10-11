"""sysmon provisioning (rig-cli#622) — config validation, plan building, rendering, install,
drift. The daemon's own behavior is covered by tests/test_sysmon_daemon.py and the slice tests.
"""

from __future__ import annotations

import json
import plistlib
import sys
import time
from pathlib import Path

import pytest

from riglib import drift as driftmod
from riglib import sysmon
from riglib.actions import runner
from riglib.config import ConfigError, validate

_REAL_PROVISION = runner._do_provision_sysmon
_REAL_CHECK = driftmod._check_sysmon


@pytest.fixture(autouse=True)
def _real_sysmon(monkeypatch, live_launchd_home):
    """Restore the real handler + drift check (conftest stubs both suite-wide)."""
    monkeypatch.setattr(runner, "_do_provision_sysmon", _REAL_PROVISION)
    monkeypatch.setitem(runner._HANDLERS, "provision_sysmon", _REAL_PROVISION)
    monkeypatch.setattr(driftmod, "_check_sysmon", _REAL_CHECK)
    monkeypatch.delenv("RIG_SYSMON_DRY_RUN", raising=False)


def _home(monkeypatch, tmp_path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HOME", str(home))
    return home


def _action(block=None, gpu_env=None):
    from riglib.plan import Action

    block = block or {}
    return Action(
        kind="provision_sysmon", category="sysmon", item="monitor", source=Path("/"),
        target=Path(block.get("label") or sysmon.DEFAULT_LABEL),
        options={"block": block, "gpu_env": gpu_env or {}},
    )


def _stub_services(monkeypatch, *, loaded=False):
    calls = []
    monkeypatch.setattr(runner, "_launchctl_bootout", lambda p: calls.append(("bootout", p)) or 0)
    monkeypatch.setattr(runner, "_launchctl_bootstrap", lambda p: calls.append(("bootstrap", p)) or 0)
    monkeypatch.setattr(runner, "_launchctl_gui_loaded", lambda label: loaded)
    monkeypatch.setattr(runner, "_systemctl_user", lambda *a: calls.append(("systemctl",) + a) or (0 if loaded or a[0] != "is-active" else 3))
    monkeypatch.setattr(driftmod, "_sysmon_service_loaded", lambda plan: loaded)
    return calls


def _heartbeat(home: Path, age_s: float, clean: bool = False) -> None:
    state = home / ".config" / "rig" / "sysmon" / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "status.json").write_text(json.dumps({"heartbeat": {
        "last_heartbeat": time.time() - age_s, "clean_shutdown": clean, "boot_id": "b", "pid": 1,
    }}))


# ── config validation ─────────────────────────────────────────────────────────────────────
def test_empty_and_full_blocks_are_accepted():
    validate({"version": 1, "sysmon": {}})
    validate({"version": 1, "sysmon": {
        "enabled": True, "auto_remediate": True, "events": False,
        "thresholds": {"wired_pct": {"warn": 70, "critical": 80}, "disk_free_gb": {"warn": 10, "critical": 3},
                       "free_gb": {"warn": 0.05, "critical": 0.02}, "data_used_pct": {"warn": 85, "critical": 92}},
        "alert": {"min_severity": "warning", "max_per_hour": 3},
        "heal": {"kill_owned": True, "grace_seconds": 0},
        "remediation": {"agent": "claude-code"},
        "sessions": {"restore_on_boot": True, "tmux_session": "work"},
        "gpu": {"slots": 3},
        "reclaim": {"worktree_roots": ["~/xp"], "worktree_age_days": 14, "cache_dirs": [], "max_candidates": 5},
    }})


@pytest.mark.parametrize("block, fragment", [
    ({"thresholds": {"wired_pct": {"warn": 90, "critical": 85}}}, "warn must be <= critical"),
    ({"thresholds": {"reclaimable_gb": {"warn": 1, "critical": 2}}}, "warn must be >= critical"),
    ({"thresholds": {"wired_pct": {"warn": 70}}}, "needs both warn and critical"),
    ({"thresholds": {"bogus": {"warn": 1, "critical": 2}}}, "bogus"),
    ({"remediation": {"agent": "gpt"}}, "remediation.agent"),
    ({"alert": {"min_severity": "info"}}, "min_severity"),
    ({"heal": {"kill_orphans": "yes"}}, "kill_orphans"),
    ({"sample_interval_seconds": 0}, "sample_interval_seconds"),
    ({"gpu": {"slots": 0}}, "gpu.slots"),
    ({"heal": {"restart_labels": "ai.hyperide.*"}}, "heal.restart_labels"),
    ({"heal": {"restart_labels": ["ai.hyperide.*", ""]}}, "heal.restart_labels"),
    ({"heal": {"restart_cooldown_minutes": "30"}}, "heal.restart_cooldown_minutes"),
    ({"heal": {"max_restarts_per_day": 0}}, "heal.max_restarts_per_day"),
    ({"heal": {"restart_daemons": "on"}}, "heal.restart_daemons"),
    ({"heal": {"restart_everything": True}}, "restart_everything"),
    ({"thresholds": {"free_gb": {"warn": 0.02, "critical": 0.05}}}, "sysmon.thresholds.free_gb: warn must be >= critical"),
    ({"thresholds": {"data_used_pct": {"warn": 96, "critical": 95}}}, "sysmon.thresholds.data_used_pct: warn must be <= critical"),
    ({"thresholds": {"data_used_pct": {"warn": 90, "critical": "95"}}}, "data_used_pct.critical must be a number"),
    ({"reclaim": {"worktree_age_days": 0}}, "reclaim.worktree_age_days"),
    ({"reclaim": {"max_candidates": True}}, "reclaim.max_candidates"),
    ({"reclaim": {"worktree_roots": "~/xp"}}, "reclaim.worktree_roots"),
    ({"reclaim": {"cache_dirs": ["~/ok", " "]}}, "reclaim.cache_dirs"),
    ({"reclaim": {"delete": True}}, "delete"),
    ({"reclaim": []}, "sysmon.reclaim must be a mapping"),
])
def test_misconfiguration_fails_closed(block, fragment):
    with pytest.raises(ConfigError) as exc:
        validate({"version": 1, "sysmon": block})
    assert fragment in str(exc.value)


# ── plan building ─────────────────────────────────────────────────────────────────────────
def _plan_actions(data, fake_agent_tools, tmp_path):
    from riglib.catalog import Catalog
    from riglib.config import LoadedConfig
    from riglib.plan import build

    plan = build(LoadedConfig(data=data, repo_root=tmp_path), Catalog.scan(str(fake_agent_tools)))
    return [a for a in plan.actions if a.kind == "provision_sysmon"]


def test_absent_block_still_provisions_and_enabled_false_opts_out(fake_agent_tools, tmp_path):
    assert len(_plan_actions({}, fake_agent_tools, tmp_path)) == 1
    assert _plan_actions({"sysmon": {"enabled": False}}, fake_agent_tools, tmp_path) == []


def test_gpu_cap_follows_rig_env_unless_set_explicitly(fake_agent_tools, tmp_path):
    env = {"env": {"vars": {"RIG_GPU_SLOTS": "3", "RIG_GPU_SLOT_DIR": "/x/slots"}}}
    [a] = _plan_actions(env, fake_agent_tools, tmp_path)
    rt = runner.sysmon_plan_from_action(a).runtime
    assert rt["gpu"]["slots"] == 3 and rt["gpu"]["slot_dir"] == "/x/slots"
    [a] = _plan_actions({**env, "sysmon": {"gpu": {"slots": 1}}}, fake_agent_tools, tmp_path)
    assert runner.sysmon_plan_from_action(a).runtime["gpu"]["slots"] == 1


# ── rendering ─────────────────────────────────────────────────────────────────────────────
def test_partial_threshold_override_keeps_the_other_defaults(tmp_path):
    plan = sysmon.build_sysmon(home=tmp_path, block={"thresholds": {"wired_pct": {"warn": 60, "critical": 70}}},
                               gpu_env={})
    th = json.loads(plan.render_config())["thresholds"]
    assert th["wired_pct"] == {"warn": 60, "critical": 70}
    assert th["swap_pct"]["critical"] == 95.0
    assert json.loads(plan.render_config())["state_dir"] == str(tmp_path / ".config/rig/sysmon/state")


def test_restart_rung_keys_render_with_the_monitors_own_label(tmp_path):
    block = {"label": "ai.example.sysmon",
             "heal": {"restart_labels": ["ai.hyperide.tg-ctl"], "restart_cooldown_minutes": 10,
                      "max_restarts_per_day": 2, "restart_daemons": False},
             "thresholds": {"daemon_overrun_x": {"warn": 3, "critical": 5}}}
    validate({"version": 1, "sysmon": block})
    rt = json.loads(sysmon.build_sysmon(home=tmp_path, block=block, gpu_env={}).render_config())
    assert rt["label"] == "ai.example.sysmon"
    assert rt["heal"]["restart_labels"] == ["ai.hyperide.tg-ctl"] and rt["heal"]["restart_daemons"] is False
    assert (rt["heal"]["restart_cooldown_minutes"], rt["heal"]["max_restarts_per_day"]) == (10, 2)
    assert rt["thresholds"]["daemon_overrun_x"] == {"warn": 3, "critical": 5}
    default = json.loads(sysmon.build_sysmon(home=tmp_path, block={}, gpu_env={}).render_config())
    assert default["label"] == "ai.hyperide.sysmon" and default["heal"]["restart_daemons"] is True


def test_alert_senders_validate_and_render_into_sysmon_json(tmp_path):
    block = {"alert": {"tg_ctl_bin": "/opt/tg-ctl", "tg_bin": "/opt/tg"}}
    validate({"version": 1, "sysmon": block})
    rt = json.loads(sysmon.build_sysmon(home=tmp_path, block=block, gpu_env={}).render_config())
    assert (rt["alert"]["tg_ctl_bin"], rt["alert"]["tg_bin"]) == ("/opt/tg-ctl", "/opt/tg")
    default = json.loads(sysmon.build_sysmon(home=tmp_path, block={}, gpu_env={}).render_config())
    assert (default["alert"]["tg_ctl_bin"], default["alert"]["tg_bin"]) == ("tg-ctl", "tg")
    for bad in ({"tg_ctl_bin": []}, {"tg_bin": 7}, {"tg_ctl_bin": " "}):
        with pytest.raises(ConfigError):
            validate({"version": 1, "sysmon": {"alert": bad}})


def test_free_and_data_volume_thresholds_and_reclaim_render_into_sysmon_json(tmp_path):
    block = {"thresholds": {"data_used_pct": {"warn": 85, "critical": 92}},
             "reclaim": {"worktree_roots": ["~/src"], "worktree_age_days": 3}}
    rt = json.loads(sysmon.build_sysmon(home=tmp_path, block=block, gpu_env={}).render_config())
    assert rt["thresholds"]["data_used_pct"] == {"warn": 85, "critical": 92}
    assert rt["thresholds"]["free_gb"] == {"warn": 0.05, "critical": 0.025}  # the calibrated default
    assert rt["reclaim"]["worktree_roots"] == ["~/src"] and rt["reclaim"]["worktree_age_days"] == 3
    assert rt["reclaim"]["max_candidates"] == 10  # the untouched keys keep their defaults


def test_plist_runs_the_installed_package_resident(tmp_path):
    plan = sysmon.build_sysmon(home=tmp_path, block={}, gpu_env={})
    p = plistlib.loads(plan.render_plist().encode())
    assert p["ProgramArguments"][1:] == ["-m", "sysmon_daemon", "run", "--config", str(plan.config_path)]
    assert p["WorkingDirectory"] == str(plan.config_dir)
    assert p["KeepAlive"] is True and p["RunAtLoad"] is True
    assert f"{tmp_path}/.local/bin" in p["EnvironmentVariables"]["PATH"]
    assert plan.render_plist() == sysmon.build_sysmon(home=tmp_path, block={}, gpu_env={}).render_plist()


def test_package_render_is_every_daemon_module_byte_for_byte(tmp_path):
    from riglib import sysmon_daemon

    files = sysmon.build_sysmon(home=tmp_path, block={}, gpu_env={}).render_package()
    pkg = Path(sysmon_daemon.__file__).parent
    assert set(files) == {p.name for p in pkg.glob("*.py")}
    # heal.py imports deliver.py, so the installed daemon must ship it too.
    assert {"__main__.py", "daemon.py", "heal.py", "sessions.py", "deliver.py"} <= set(files)
    assert files["daemon.py"] == (pkg / "daemon.py").read_text()


# ── install ───────────────────────────────────────────────────────────────────────────────
def test_apply_installs_package_config_plist_and_loads(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    home = _home(monkeypatch, tmp_path)
    calls = _stub_services(monkeypatch)
    res = runner._do_provision_sysmon(_action(), "backup")
    plan = runner.sysmon_plan_from_action(_action())
    assert res.status == "created", res.detail
    assert (plan.package_dir / "daemon.py").is_file() and plan.config_path.is_file() and plan.plist_path.is_file()
    assert plan.state_dir.is_dir()
    assert ("bootstrap", str(plan.plist_path)) in calls
    assert str(home) in str(plan.plist_path)


def test_reapply_is_a_noop_when_current_and_loaded(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    _home(monkeypatch, tmp_path)
    _stub_services(monkeypatch)
    runner._do_provision_sysmon(_action(), "backup")
    calls = _stub_services(monkeypatch, loaded=True)
    res = runner._do_provision_sysmon(_action(), "backup")
    assert res.status == "skipped", res.detail
    assert calls == []


def test_a_hand_edited_module_is_restored_and_the_daemon_reloaded(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    _home(monkeypatch, tmp_path)
    _stub_services(monkeypatch)
    runner._do_provision_sysmon(_action(), "backup")
    plan = runner.sysmon_plan_from_action(_action())
    (plan.package_dir / "heal.py").write_text("# tampered\n")
    calls = _stub_services(monkeypatch, loaded=True)
    res = runner._do_provision_sysmon(_action(), "backup")
    assert res.status == "created", res.detail
    assert (plan.package_dir / "heal.py").read_text() == plan.render_package()["heal.py"]
    assert ("bootstrap", str(plan.plist_path)) in calls


def test_config_change_rewrites_sysmon_json_and_reloads(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    _home(monkeypatch, tmp_path)
    _stub_services(monkeypatch)
    runner._do_provision_sysmon(_action(), "backup")
    calls = _stub_services(monkeypatch, loaded=True)
    runner._do_provision_sysmon(_action({"auto_remediate": True}), "backup")
    plan = runner.sysmon_plan_from_action(_action({"auto_remediate": True}))
    assert json.loads(plan.config_path.read_text())["auto_remediate"] is True
    assert any(c[0] == "bootstrap" for c in calls)


def test_dry_run_writes_artifacts_but_never_touches_launchd(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    _home(monkeypatch, tmp_path)
    monkeypatch.setenv("RIG_SYSMON_DRY_RUN", "1")
    calls = _stub_services(monkeypatch)
    res = runner._do_provision_sysmon(_action(), "backup")
    assert runner.sysmon_plan_from_action(_action()).plist_path.is_file()
    assert calls == [] and "RIG_SYSMON_DRY_RUN" in res.detail


def test_linux_installs_a_systemd_user_unit(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    _home(monkeypatch, tmp_path)
    calls = _stub_services(monkeypatch)
    res = runner._do_provision_sysmon(_action(), "backup")
    plan = runner.sysmon_plan_from_action(_action())
    assert res.status == "created", res.detail
    unit = plan.unit_path.read_text()
    assert f"ExecStart={' '.join(plan.program_arguments)}" in unit and "Restart=always" in unit
    assert ("systemctl", "enable", plan.unit_name) in calls and ("systemctl", "restart", plan.unit_name) in calls
    assert not (tmp_path / "home" / "Library").exists()


# ── drift ─────────────────────────────────────────────────────────────────────────────────
def _drift(block=None):
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    plan = InstallPlan(on_conflict="backup")
    plan.actions.append(_action(block))
    return [d for d in detect(plan).items if d.category == "sysmon"]


def test_drift_missing_before_apply(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    _home(monkeypatch, tmp_path)
    _stub_services(monkeypatch)
    items = _drift()
    assert items and {d.direction for d in items} == {"missing"}


def test_drift_clean_after_apply_with_a_fresh_heartbeat(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    home = _home(monkeypatch, tmp_path)
    _stub_services(monkeypatch)
    runner._do_provision_sysmon(_action(), "backup")
    _stub_services(monkeypatch, loaded=True)
    _heartbeat(home, age_s=3)
    assert _drift() == []


@pytest.mark.parametrize("age, clean, word", [(3600, False, "stale"), (5, True, "stopped")])
def test_a_loaded_service_whose_daemon_is_not_ticking_is_drift(monkeypatch, tmp_path, age, clean, word):
    monkeypatch.setattr(sys, "platform", "darwin")
    home = _home(monkeypatch, tmp_path)
    _stub_services(monkeypatch)
    runner._do_provision_sysmon(_action(), "backup")
    _stub_services(monkeypatch, loaded=True)
    _heartbeat(home, age_s=age, clean=clean)
    [item] = _drift()
    assert item.direction == "missing" and word in item.detail


def test_drift_flags_a_modified_module(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    home = _home(monkeypatch, tmp_path)
    _stub_services(monkeypatch)
    runner._do_provision_sysmon(_action(), "backup")
    _stub_services(monkeypatch, loaded=True)
    _heartbeat(home, age_s=3)
    plan = runner.sysmon_plan_from_action(_action())
    (plan.package_dir / "alert.py").write_text("# edited\n")
    items = _drift()
    assert [d.direction for d in items] == ["modified"] and items[0].target.name == "alert.py"
