"""Model-freshness daily-schedule provisioning — config, plan, install, drift.

Cross-platform is exercised on either host via the ``RIG_SCHEDULE_PLATFORM`` test seam
(``riglib.schedule.detect_platform``). launchd plists and systemd units go to a tmp HOME;
``launchctl`` / ``systemctl --user`` / ``crontab`` are mocked at their subprocess seams so the
suite never touches the real user launchd, systemd manager or crontab.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from riglib import config as cfgmod
from riglib import drift as driftmod
from riglib import schedule as sched
from riglib.actions import runner
from riglib.actions.runner import ApplyReport, run_plan
from riglib.catalog import Catalog
from riglib.config import ConfigError, LoadedConfig, validate
from riglib.drift import detect
from riglib.plan import build


# captured at import (before any monkeypatch runs) — the genuine handler.
_REAL_PROVISION = runner._do_provision_schedule
_REAL_TIMER_ACTIVE = runner.schedule_timer_active


@pytest.fixture(autouse=True)
def _real_scheduler(monkeypatch, live_launchd_home):
    """Restore the REAL provision_schedule handler for THIS module's tests.

    conftest's autouse `_isolate_scheduler` stubs `_do_provision_schedule` (+ the `_HANDLERS`
    entry) to a no-op so e2e tests can't touch the host scheduler. These dedicated tests
    EXERCISE the real install logic — with their own HOME-isolated tmp dirs + stubbed daemon
    seams — so they restore the real handler. (conftest still stubs the daemon `_launchctl*`
    /`_systemctl_user`/`_*_crontab` seams; these tests re-stub them per-test as needed, and get
    the real timer probe back so it runs against the stubbed `_systemctl_user`.)
    """
    monkeypatch.setattr(runner, "_do_provision_schedule", _REAL_PROVISION)
    monkeypatch.setitem(runner._HANDLERS, "provision_schedule", _REAL_PROVISION)
    monkeypatch.setattr(runner, "schedule_timer_active", _REAL_TIMER_ACTIVE)
    monkeypatch.setattr(driftmod, "schedule_timer_active", _REAL_TIMER_ACTIVE)


# ── config validation ───────────────────────────────────────────────────────────────────
def test_models_block_accepted():
    validate({"version": 1, "models": {"enabled": True, "schedule": {"time": "12:00"}}})


def test_models_block_empty_ok():
    validate({"version": 1, "models": {}})


def test_models_unknown_key_rejected():
    with pytest.raises(ConfigError):
        validate({"version": 1, "models": {"nope": 1}})


def test_models_enabled_must_be_bool():
    with pytest.raises(ConfigError):
        validate({"version": 1, "models": {"enabled": "yes"}})


@pytest.mark.parametrize("bad", ["noon", "25:00", "12:60", "12", "12:00:00", "-1:00"])
def test_models_schedule_time_rejected(bad):
    with pytest.raises(ConfigError):
        validate({"version": 1, "models": {"schedule": {"time": bad}}})


@pytest.mark.parametrize("good", ["00:00", "12:00", "23:59", "09:30"])
def test_models_schedule_time_accepted(good):
    validate({"version": 1, "models": {"schedule": {"time": good}}})


def test_parse_hhmm():
    assert cfgmod.parse_hhmm("12:00") == (12, 0)
    assert cfgmod.parse_hhmm("09:30") == (9, 30)


# ── schedule artifact rendering (pure) ───────────────────────────────────────────────────
def test_build_schedule_launchd(monkeypatch):
    monkeypatch.setenv("RIG_SCHEDULE_PLATFORM", "launchd")
    s = sched.build_schedule(checker_path=Path("/checkout/lib/checker/model_freshness.py"))
    assert s.platform == "launchd"
    assert s.hour == 12 and s.minute == 0
    assert s.plist_path is not None and s.plist_path.name == f"{sched.DEFAULT_LABEL}.plist"
    xml = s.plist_xml()
    assert sched.DEFAULT_LABEL in xml
    assert "<key>Hour</key>" in xml and "<integer>12</integer>" in xml
    assert "model_freshness.py" in xml


def test_systemd_unit_dir_honors_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    s = sched.build_schedule(checker_path=Path("/c.py"), platform="systemd")
    assert s.unit_dir == tmp_path / "xdg" / "systemd" / "user"


def test_build_schedule_systemd(monkeypatch, tmp_path):
    monkeypatch.setenv("RIG_SCHEDULE_PLATFORM", "systemd")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    s = sched.build_schedule(checker_path=Path("/checkout/lib/checker/model_freshness.py"), hour=9, minute=30)
    assert s.platform == "systemd"
    files = s.unit_files()
    unit_dir = tmp_path / ".config" / "systemd" / "user"
    service = files[unit_dir / f"{sched.DEFAULT_LABEL}.service"]
    timer = files[unit_dir / f"{sched.DEFAULT_LABEL}.timer"]
    assert "Type=oneshot" in service
    assert 'ExecStart=/bin/bash -lc "python3 /checkout/lib/checker/model_freshness.py"' in service
    assert "OnCalendar=*-*-* 09:30:00" in timer and "Persistent=true" in timer
    assert f"Unit={sched.DEFAULT_LABEL}.service" in timer and "WantedBy=timers.target" in timer


def test_detect_platform_linux_is_systemd(monkeypatch):
    monkeypatch.delenv("RIG_SCHEDULE_PLATFORM", raising=False)
    monkeypatch.setattr(sched.sys, "platform", "linux")
    assert sched.detect_platform() == "systemd"


def test_default_checker_path():
    p = sched.default_checker_path("/some/agent-tools")
    assert p == Path("/some/agent-tools/lib/checker/model_freshness.py")
    assert sched.default_checker_path(None) is None


# ── plan building ────────────────────────────────────────────────────────────────────────
def _cfg(data: dict, repo_root: Path) -> LoadedConfig:
    return LoadedConfig(data=data, repo_root=repo_root)


def test_plan_emits_schedule_action(fake_agent_tools, tmp_path, monkeypatch):
    monkeypatch.setenv("RIG_SCHEDULE_PLATFORM", "launchd")
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"agent_tools_source": str(fake_agent_tools), "models": {"enabled": True, "schedule": {"time": "12:00"}},
         "skills": {"enabled": False}, "agent_hooks": {"enabled": False}, "ci": {"enabled": False},
         "mcp": {"enabled": False}},
        tmp_path,
    )
    plan = build(cfg, cat, project_type="unknown")
    sched_actions = [a for a in plan.actions if a.kind == "provision_schedule"]
    assert len(sched_actions) == 1
    a = sched_actions[0]
    assert a.category == "models" and a.item == "model-freshness"
    assert a.options["hour"] == 12 and a.options["minute"] == 0
    assert a.options["platform"] == "launchd"
    assert a.options["checker_path"].endswith("lib/checker/model_freshness.py")


def test_plan_no_schedule_when_absent(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"agent_tools_source": str(fake_agent_tools)}, tmp_path)
    plan = build(cfg, cat, project_type="unknown")
    assert not [a for a in plan.actions if a.kind == "provision_schedule"]


def test_plan_no_schedule_when_disabled(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"agent_tools_source": str(fake_agent_tools), "models": {"enabled": False}}, tmp_path)
    plan = build(cfg, cat, project_type="unknown")
    assert not [a for a in plan.actions if a.kind == "provision_schedule"]


def test_plan_custom_time(fake_agent_tools, tmp_path, monkeypatch):
    monkeypatch.setenv("RIG_SCHEDULE_PLATFORM", "systemd")
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"agent_tools_source": str(fake_agent_tools), "models": {"schedule": {"time": "06:15"}}},
        tmp_path,
    )
    plan = build(cfg, cat, project_type="unknown")
    a = next(a for a in plan.actions if a.kind == "provision_schedule")
    assert a.options["hour"] == 6 and a.options["minute"] == 15


def test_plan_explicit_checker_path(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    custom = tmp_path / "custom" / "checker.py"
    custom.parent.mkdir(parents=True, exist_ok=True)
    custom.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    cfg = _cfg(
        {"agent_tools_source": str(fake_agent_tools),
         "models": {"checker_path": str(custom)}},
        tmp_path,
    )
    plan = build(cfg, cat, project_type="unknown")
    a = next(a for a in plan.actions if a.kind == "provision_schedule")
    assert a.options["checker_path"] == str(custom)


def test_plan_skips_when_checker_missing(fake_agent_tools, tmp_path):
    """A checker_path that doesn't exist on disk → no action, a plan note (not a silent cron)."""
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"agent_tools_source": str(fake_agent_tools),
         "models": {"checker_path": str(tmp_path / "nope" / "missing.py")}},
        tmp_path,
    )
    plan = build(cfg, cat, project_type="unknown")
    assert not [a for a in plan.actions if a.kind == "provision_schedule"]
    assert any("checker not found" in n for n in plan.notes)


# ── install (launchd) — idempotent install-if-missing ───────────────────────────────────
def _launchd_action(tmp_home: Path):
    """A provision_schedule action whose launchd plist lands under tmp_home."""
    from riglib.plan import Action

    label = sched.DEFAULT_LABEL
    return Action(
        kind="provision_schedule",
        category="models",
        item="model-freshness",
        source=tmp_home,
        target=tmp_home / "Library" / "LaunchAgents" / f"{label}.plist",
        options={
            "platform": "launchd",
            "label": label,
            "hour": 12,
            "minute": 0,
            "checker_path": "/checkout/lib/checker/model_freshness.py",
        },
    )


def test_launchd_install_then_idempotent(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    loaded = {"v": False}
    calls = []

    def fake_launchctl(verb, arg):
        calls.append((verb, arg))
        if verb == "load":
            loaded["v"] = True
        elif verb == "unload":
            loaded["v"] = False
        return 0

    monkeypatch.setattr(runner, "_launchctl", fake_launchctl)
    monkeypatch.setattr(runner, "_launchctl_loaded", lambda label: loaded["v"])

    action = _launchd_action(home)
    res1 = runner._do_provision_schedule(action, "backup")
    assert res1.status == "created"
    plist = home / "Library" / "LaunchAgents" / f"{sched.DEFAULT_LABEL}.plist"
    assert plist.is_file()
    assert ("load", str(plist)) in calls

    # re-apply: present + current + loaded → no-op
    calls.clear()
    res2 = runner._do_provision_schedule(action, "backup")
    assert res2.status == "skipped"
    assert calls == []  # no launchctl churn on the idempotent path


def test_launchd_reinstall_when_unloaded(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(runner, "_launchctl", lambda verb, arg: 0)
    # plist exists & current, but launchd says NOT loaded → must reload (not skip)
    action = _launchd_action(home)
    plist = action.target
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(runner.schedule_plan_from_action(action).plist_xml(), encoding="utf-8")
    monkeypatch.setattr(runner, "_launchctl_loaded", lambda label: False)
    res = runner._do_provision_schedule(action, "backup")
    assert res.status in ("created", "updated")


def test_launchd_conflict_skip_does_not_touch_daemon(tmp_path, monkeypatch):
    """on_conflict=skip + a plist that DIFFERS → report 'skipped', never (re)load launchd.

    The desired schedule was not written (write_file skipped on the conflict), so unloading
    /loading the stale plist would mutate launchd with the WRONG schedule and reporting
    'updated' would mask the unresolved drift. We must surface 'skipped' and leave the daemon
    untouched.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    calls = []
    monkeypatch.setattr(runner, "_launchctl", lambda verb, arg: calls.append((verb, arg)) or 0)
    # not loaded — so without the conflict-skip guard the code would proceed to (re)load.
    monkeypatch.setattr(runner, "_launchctl_loaded", lambda label: False)

    action = _launchd_action(home)
    plist = action.target
    plist.parent.mkdir(parents=True, exist_ok=True)
    # an EXISTING plist that differs from desired (a stale, hand-edited schedule).
    stale = runner.schedule_plan_from_action(action).plist_xml().replace("<integer>12</integer>", "<integer>6</integer>")
    plist.write_text(stale, encoding="utf-8")

    res = runner._do_provision_schedule(action, "skip")
    assert res.status == "skipped", res.detail
    assert calls == []  # daemon never mutated
    # the stale plist on disk is left exactly as-is (drift NOT silently reconciled).
    assert plist.read_text(encoding="utf-8") == stale


# ── install (systemd --user timer) — idempotent install-if-missing ──────────────────────
def _systemd_action(home: Path):
    from riglib.plan import Action

    unit_dir = home / ".config" / "systemd" / "user"
    return Action(
        kind="provision_schedule",
        category="models",
        item="model-freshness",
        source=home,
        target=unit_dir / f"{sched.DEFAULT_LABEL}.timer",
        options={
            "platform": "systemd",
            "label": sched.DEFAULT_LABEL,
            "hour": 12,
            "minute": 0,
            "checker_path": "/checkout/lib/checker/model_freshness.py",
        },
    )


@pytest.fixture
def systemd_home(tmp_path, monkeypatch, live_launchd_home):
    """A tmp HOME treated as the login home, plus a fake user manager: records every
    `systemctl --user` argv and reports the timer active once `enable --now` ran."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("RIG_SCHEDULE_DRY_RUN", raising=False)
    state = {"calls": [], "enabled": False}

    def fake_systemctl(*args):
        state["calls"].append(args)
        if args[:2] == ("enable", "--now"):
            state["enabled"] = True
        if args[0] in ("is-enabled", "is-active"):
            return 0 if state["enabled"] else 1
        return 0

    monkeypatch.setattr(runner, "_systemctl_user", fake_systemctl)
    monkeypatch.setattr(
        runner, "_systemctl_user_output",
        lambda *args: "yes" if args[-1] in state.get("stale", ()) else "no",
    )
    return home, state


def test_systemd_install_then_idempotent(systemd_home):
    home, state = systemd_home
    action = _systemd_action(home)
    res1 = runner._do_provision_schedule(action, "backup")
    assert res1.status == "created", res1.detail
    assert "installed" in res1.detail
    unit_dir = home / ".config" / "systemd" / "user"
    assert (unit_dir / f"{sched.DEFAULT_LABEL}.service").is_file()
    assert "OnCalendar=*-*-* 12:00:00" in (unit_dir / f"{sched.DEFAULT_LABEL}.timer").read_text(encoding="utf-8")
    assert ("daemon-reload",) in state["calls"]
    assert ("enable", "--now", f"{sched.DEFAULT_LABEL}.timer") in state["calls"]

    # re-apply: units current + timer active → skipped, no further systemctl mutation
    state["calls"].clear()
    res2 = runner._do_provision_schedule(action, "backup")
    assert res2.status == "skipped", res2.detail
    assert all(c[0] in ("is-enabled", "is-active") for c in state["calls"])


def test_systemd_reenables_inactive_timer(systemd_home):
    """Current unit files but a disabled/stopped timer is NOT installed — apply re-enables it."""
    home, state = systemd_home
    action = _systemd_action(home)
    runner._do_provision_schedule(action, "backup")
    state["enabled"] = False
    state["calls"].clear()
    res = runner._do_provision_schedule(action, "backup")
    assert res.status in ("created", "backed_up") and "updated" in res.detail
    assert ("enable", "--now", f"{sched.DEFAULT_LABEL}.timer") in state["calls"]


def test_systemd_reloads_when_manager_holds_stale_units(systemd_home):
    """Units written by a dry-run (or behind a failed daemon-reload) match on disk, but the
    manager still runs the old schedule — apply must reload + restart, and drift must say so."""
    home, state = systemd_home
    action = _systemd_action(home)
    runner._do_provision_schedule(action, "backup")
    state["stale"] = {f"{sched.DEFAULT_LABEL}.service"}  # only the service changed (checker_path)
    state["calls"].clear()
    from riglib.plan import InstallPlan

    assert [d for d in detect(InstallPlan(actions=[action])).items if d.category == "models"]
    runner._do_provision_schedule(action, "backup")
    assert ("daemon-reload",) in state["calls"]
    assert ("restart", f"{sched.DEFAULT_LABEL}.timer") in state["calls"]


def test_systemd_unreachable_manager_is_an_error(systemd_home, monkeypatch):
    home, _state = systemd_home
    monkeypatch.setattr(runner, "_systemctl_user", lambda *args: 1)
    res = runner._do_provision_schedule(_systemd_action(home), "backup")
    assert res.status == "error"
    assert "daemon-reload" in res.detail


def test_systemd_install_removes_legacy_crontab_pair(systemd_home, monkeypatch):
    home, _state = systemd_home
    store = {
        "content": (
            "0 3 * * * /usr/bin/backup.sh\n"
            f"{sched.CRON_SENTINEL_PREFIX} {sched.DEFAULT_LABEL}\n"
            "0 12 * * * python3 /checkout/lib/checker/model_freshness.py\n"
        )
    }
    monkeypatch.setattr(runner, "_read_crontab", lambda: (True, store["content"]))

    def fake_write(contents):
        store["content"] = contents
        return 0

    monkeypatch.setattr(runner, "_write_crontab", fake_write)
    res = runner._do_provision_schedule(_systemd_action(home), "backup")
    assert "removed legacy crontab line" in res.detail
    assert store["content"] == "0 3 * * * /usr/bin/backup.sh\n"  # user line kept, ours gone


def test_systemd_unreadable_crontab_is_not_an_error(systemd_home, monkeypatch):
    """The NoNewPrivs agent shell (rig-cli#721): `crontab -l` fails → install still succeeds."""
    home, _state = systemd_home
    monkeypatch.setattr(runner, "_read_crontab", lambda: (False, ""))
    monkeypatch.setattr(runner, "_write_crontab", lambda c: pytest.fail("crontab written"))
    res = runner._do_provision_schedule(_systemd_action(home), "backup")
    assert res.status == "created" and "crontab" not in res.detail


def test_systemd_retries_legacy_crontab_removal_when_already_installed(systemd_home, monkeypatch):
    """First apply from a NoNewPrivs shell can't read the crontab; a later apply with the timer
    already active must still remove the legacy line (else the checker runs twice daily)."""
    home, _state = systemd_home
    action = _systemd_action(home)
    monkeypatch.setattr(runner, "_read_crontab", lambda: (False, ""))
    assert runner._do_provision_schedule(action, "backup").status == "created"
    store = {"content": f"{sched.CRON_SENTINEL_PREFIX} {sched.DEFAULT_LABEL}\n0 12 * * * python3 x.py\n"}
    monkeypatch.setattr(runner, "_read_crontab", lambda: (True, store["content"]))
    monkeypatch.setattr(runner, "_write_crontab", lambda c: store.update(content=c) or 0)
    res = runner._do_provision_schedule(action, "backup")
    assert res.status == "updated" and "removed legacy crontab line" in res.detail
    assert store["content"] == ""


def test_systemd_restart_failure_is_an_error(systemd_home, monkeypatch):
    home, state = systemd_home
    real = runner._systemctl_user
    monkeypatch.setattr(runner, "_systemctl_user", lambda *a: 1 if a[0] == "restart" else real(*a))
    res = runner._do_provision_schedule(_systemd_action(home), "backup")
    assert res.status == "error" and "restart" in res.detail


def test_crontab_without_managed_strips_pair():
    content = (
        "0 3 * * * backup\n"
        f"{sched.CRON_SENTINEL_PREFIX} {sched.DEFAULT_LABEL}\n"
        "12 12 * * * python3 checker.py\n"
        "30 4 * * * other\n"
    )
    out = runner._crontab_without_managed(content, sched.DEFAULT_LABEL)
    assert "backup" in "\n".join(out)
    assert "other" in "\n".join(out)
    assert sched.DEFAULT_LABEL not in "\n".join(out)
    assert "checker.py" not in "\n".join(out)


def test_systemd_exec_quoting_escapes_specials(tmp_path, monkeypatch):
    """`%` is a systemd specifier and `$` expands; both must reach bash literally."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    s = sched.build_schedule(checker_path=Path("/weird/pa%th $x/model_freshness.py"), platform="systemd")
    service = next(t for p, t in s.unit_files().items() if p.suffix == ".service")
    assert "pa%%th $$x" in service
    assert "ExecStart=/bin/bash -lc \"python3 '/weird/pa%%th $$x/model_freshness.py'\"" in service


# ── drift ────────────────────────────────────────────────────────────────────────────────
def test_drift_launchd_missing(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    # drift.py imports the helpers by name → patch the names in the drift namespace.
    monkeypatch.setattr(driftmod, "_launchctl_loaded", lambda label: False)
    from riglib.plan import InstallPlan

    plan = InstallPlan(actions=[_launchd_action(home)])
    report = detect(plan)
    sched_drift = [d for d in report.items if d.category == "models"]
    assert sched_drift and sched_drift[0].direction == "missing"


def test_drift_launchd_in_sync(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(driftmod, "_launchctl_loaded", lambda label: True)
    action = _launchd_action(home)
    action.target.parent.mkdir(parents=True, exist_ok=True)
    action.target.write_text(runner.schedule_plan_from_action(action).plist_xml(), encoding="utf-8")
    from riglib.plan import InstallPlan

    report = detect(InstallPlan(actions=[action]))
    assert not [d for d in report.items if d.category == "models"]


def test_drift_systemd_missing(systemd_home):
    home, _state = systemd_home
    from riglib.plan import InstallPlan

    report = detect(InstallPlan(actions=[_systemd_action(home)]))
    drift = [d for d in report.items if d.category == "models"]
    assert drift and drift[0].direction == "missing"


def test_drift_systemd_modified(systemd_home):
    home, _state = systemd_home
    action = _systemd_action(home)
    runner._do_provision_schedule(action, "backup")
    timer = home / ".config" / "systemd" / "user" / f"{sched.DEFAULT_LABEL}.timer"
    timer.write_text(timer.read_text(encoding="utf-8").replace("12:00:00", "06:00:00"), encoding="utf-8")
    from riglib.plan import InstallPlan

    report = detect(InstallPlan(actions=[action]))
    drift = [d for d in report.items if d.category == "models"]
    assert drift and drift[0].direction == "modified"


def test_drift_systemd_inactive_timer(systemd_home):
    home, state = systemd_home
    action = _systemd_action(home)
    runner._do_provision_schedule(action, "backup")
    state["enabled"] = False
    from riglib.plan import InstallPlan

    report = detect(InstallPlan(actions=[action]))
    drift = [d for d in report.items if d.category == "models"]
    assert drift and "not enabled+active" in drift[0].detail


def test_drift_systemd_in_sync(systemd_home):
    home, _state = systemd_home
    action = _systemd_action(home)
    runner._do_provision_schedule(action, "backup")
    from riglib.plan import InstallPlan

    report = detect(InstallPlan(actions=[action]))
    assert not [d for d in report.items if d.category == "models"]


def test_schedule_dry_run_skips_daemon_launchd(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("RIG_SCHEDULE_DRY_RUN", "1")
    called = []
    monkeypatch.setattr(runner, "_launchctl", lambda verb, arg: called.append(verb) or 0)
    res = runner._do_provision_schedule(_launchd_action(home), "backup")
    assert res.status in ("created", "updated")
    assert called == []  # the daemon was never touched
    assert (home / "Library" / "LaunchAgents" / f"{sched.DEFAULT_LABEL}.plist").is_file()


def test_schedule_dry_run_skips_daemon_systemd(systemd_home, monkeypatch):
    home, state = systemd_home
    monkeypatch.setenv("RIG_SCHEDULE_DRY_RUN", "1")
    res = runner._do_provision_schedule(_systemd_action(home), "backup")
    assert res.status == "created"
    assert state["calls"] == []  # the user manager was never touched
    assert (home / ".config" / "systemd" / "user" / f"{sched.DEFAULT_LABEL}.timer").is_file()


# ── full apply path through run_plan (systemd, mocked) ──────────────────────────────────
def test_run_plan_provisions_schedule(systemd_home):
    home, _state = systemd_home
    from riglib.plan import InstallPlan

    report: ApplyReport = run_plan(InstallPlan(actions=[_systemd_action(home)]))
    assert not report.errors
    assert report.changed == 1


# ── a host config with no models: block is drift (rig-cli#721) ─────────────────────────
def _status_scan(tmp_path, data):
    from riglib.detect import detect_environment
    from riglib.drift import compute_drift_report
    from riglib.plan import InstallPlan

    gpath = tmp_path / "config.yaml"
    gpath.write_text("{}\n", encoding="utf-8")
    loaded = LoadedConfig(data=data, repo_root=tmp_path, global_path=gpath)
    env = detect_environment(tmp_path)
    return compute_drift_report(InstallPlan(actions=[]), loaded, env).report, gpath


def test_status_flags_missing_models_block(tmp_path):
    report, gpath = _status_scan(tmp_path, {"skills": {"enabled": False}, "agent_hooks": {"enabled": False}})
    drift = [d for d in report.items if d.category == "models"]
    assert drift and drift[0].direction == "missing" and drift[0].target == gpath
    assert "rig config set --global models.enabled true" in drift[0].detail


@pytest.mark.parametrize("label", ["../evil", "a/b", "has space", "--oops"])
def test_models_label_must_be_unit_name(label):
    with pytest.raises(ConfigError):
        validate({"version": 1, "models": {"schedule": {"label": label}}})


def test_status_quiet_on_explicit_models_opt_out(tmp_path):
    report, _ = _status_scan(
        tmp_path, {"skills": {"enabled": False}, "agent_hooks": {"enabled": False}, "models": {"enabled": False}}
    )
    assert not [d for d in report.items if d.category == "models"]
