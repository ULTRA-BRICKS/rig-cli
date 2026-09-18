"""tgctl-reaper periodic orphan-sweep provisioning — config, pure plist rendering, install, drift,
plan building, status line. Mirrors tests/test_tg_ctl.py's structure (full drift/status parity).
"""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path

import pytest

from riglib import tgctl_reaper
from riglib import tgctl_reaper_daemon as daemon
from riglib import drift as driftmod
from riglib.actions import runner
from riglib.config import ConfigError, validate

# captured at import (before any monkeypatch runs) — the genuine handler + drift check.
_REAL_PROVISION = runner._do_provision_tgctl_reaper
_REAL_CHECK = driftmod._check_tgctl_reaper


@pytest.fixture(autouse=True)
def _real_tgctl_reaper(monkeypatch, live_launchd_home):
    """Restore the REAL provision_tgctl_reaper handler + drift check for THIS module's tests.

    conftest's autouse ``_isolate_scheduler`` stubs ``_do_provision_tgctl_reaper`` (+ the
    ``_HANDLERS`` entry) and ``drift._check_tgctl_reaper`` to no-ops so e2e tests can't touch the
    host launchd. These dedicated tests EXERCISE the real install + drift logic — with
    HOME-isolated tmp dirs + stubbed launchctl seams — so they restore the real implementations.

    Also clears RIG_TGCTL_REAPER_DRY_RUN between tests (a leaked env var from a prior test would
    silently route every launchctl assertion here dry).
    """
    monkeypatch.setattr(runner, "_do_provision_tgctl_reaper", _REAL_PROVISION)
    monkeypatch.setitem(runner._HANDLERS, "provision_tgctl_reaper", _REAL_PROVISION)
    monkeypatch.setattr(driftmod, "_check_tgctl_reaper", _REAL_CHECK)
    monkeypatch.delenv("RIG_TGCTL_REAPER_DRY_RUN", raising=False)


def _force_darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")


def _isolate_home(monkeypatch, tmp_path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


# ── config validation ───────────────────────────────────────────────────────────────────
def test_tgctl_reaper_block_accepted():
    validate({"version": 1, "tgctl_reaper": {"enabled": True}})


def test_tgctl_reaper_block_empty_ok():
    validate({"version": 1, "tgctl_reaper": {}})


def test_tgctl_reaper_full_block_accepted():
    validate({
        "version": 1,
        "tgctl_reaper": {
            "enabled": True,
            "interval_seconds": 300,
            "label": "com.me.tgctl-reaper",
            "config_dir": "~/tgctl-reaper",
            "python_bin": "/usr/bin/python3",
        },
    })


def test_tgctl_reaper_unknown_key_rejected():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tgctl_reaper": {"nope": 1}})


def test_tgctl_reaper_enabled_must_be_bool():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tgctl_reaper": {"enabled": "yes"}})


def test_tgctl_reaper_enabled_null_is_accepted():
    """`enabled: null` (explicit None) is valid and is NOT treated as disabled (default-on)."""
    validate({"version": 1, "tgctl_reaper": {"enabled": None}})


def test_tgctl_reaper_interval_seconds_must_be_int():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tgctl_reaper": {"interval_seconds": "900"}})


def test_tgctl_reaper_interval_seconds_must_be_positive():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tgctl_reaper": {"interval_seconds": 0}})


@pytest.mark.parametrize("key", ["label", "config_dir", "python_bin"])
def test_tgctl_reaper_string_keys_reject_non_string(key):
    with pytest.raises(ConfigError):
        validate({"version": 1, "tgctl_reaper": {key: 123}})


def test_tgctl_reaper_not_a_mapping_rejected():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tgctl_reaper": "nope"})


# ── pure plist rendering ─────────────────────────────────────────────────────────────────
def _plan(home="/Users/ultra", **over):
    over.setdefault("python_bin", str(Path(home) / "bin" / "python3"))
    return tgctl_reaper.build_tgctl_reaper(home=Path(home), **over)


def test_render_is_well_formed_and_uses_start_interval():
    p = _plan()
    parsed = plistlib.loads(p.render_plist().encode("utf-8"))
    assert parsed["Label"] == tgctl_reaper.DEFAULT_LABEL
    assert parsed["StartInterval"] == tgctl_reaper.DEFAULT_INTERVAL_SECONDS
    assert parsed["RunAtLoad"] is False
    assert "KeepAlive" not in parsed  # a periodic sweep, never a resident daemon


def test_render_preserves_key_order_not_sorted():
    body = _plan().render_plist()
    assert body.index("<key>Label</key>") < body.index("<key>ProgramArguments</key>")
    assert body.index("<key>StartInterval</key>") < body.index("<key>StandardOutPath</key>")


def test_render_is_deterministic():
    assert _plan().render_plist() == _plan().render_plist()


def test_interval_is_configurable():
    p = _plan(interval_seconds=60)
    parsed = plistlib.loads(p.render_plist().encode("utf-8"))
    assert parsed["StartInterval"] == 60


def test_label_is_configurable():
    p = _plan(label="com.me.tgctl-reaper")
    assert plistlib.loads(p.render_plist().encode("utf-8"))["Label"] == "com.me.tgctl-reaper"
    assert p.plist_path.name == "com.me.tgctl-reaper.plist"


def test_default_label_matches_the_manually_installed_predecessor():
    """The label MUST equal the manually-installed reaper's label, so a re-apply on a machine
    that already ran agent-tools/ops/tgctl-omp-acp-reaper/install.sh by hand recognizes the SAME
    service rather than installing a colliding second LaunchAgent."""
    assert tgctl_reaper.DEFAULT_LABEL == "com.hyperide.tgctl-omp-acp-reaper"


def test_paths_resolve_against_injected_home():
    p = _plan(home="/home/u", python_bin="/usr/bin/python3")
    assert p.plist_path == Path(f"/home/u/Library/LaunchAgents/{tgctl_reaper.DEFAULT_LABEL}.plist")
    assert p.sweeper_script_path == Path("/home/u/.config/rig/tgctl-reaper/reap.py")


def test_explicit_config_dir_is_honored():
    p = _plan(config_dir="/var/tgctl-reaper")
    assert p.config_dir == Path("/var/tgctl-reaper")
    assert p.out_log_path == Path("/var/tgctl-reaper/launchd.tgctl-reaper.out.log")


def test_python_bin_falls_back_to_discovered_when_not_given(monkeypatch):
    monkeypatch.setattr(tgctl_reaper.shutil, "which", lambda name: "/opt/homebrew/bin/python3")
    p = tgctl_reaper.build_tgctl_reaper(home=Path("/home/u"))
    assert p.python_bin == Path("/opt/homebrew/bin/python3")


def test_python_bin_falls_back_to_system_path_when_not_on_path(monkeypatch, tmp_path):
    monkeypatch.setattr(tgctl_reaper.shutil, "which", lambda name: None)
    fake_sys_python = tmp_path / "usr_bin_python3"
    fake_sys_python.write_text("", encoding="utf-8")
    monkeypatch.setattr(tgctl_reaper, "_PYTHON3_FALLBACK_PATHS", (str(fake_sys_python),))
    p = tgctl_reaper.build_tgctl_reaper(home=Path("/home/u"))
    assert p.python_bin == fake_sys_python


# ── sweeper script rendering: one source of truth ────────────────────────────────────────
def test_render_sweeper_script_matches_daemon_module_source():
    p = _plan()
    assert p.render_sweeper_script() == Path(daemon.__file__).read_text(encoding="utf-8")


def test_render_sweeper_script_is_valid_python():
    compile(_plan().render_sweeper_script(), "<reap.py>", "exec")


# ── install (runner) — write + (re)load, idempotent, conflict ─────────────────────────────
def _action(home, **over):
    from riglib.plan import Action

    label = over.pop("label", tgctl_reaper.DEFAULT_LABEL)
    return Action(
        kind="provision_tgctl_reaper",
        category="tgctl_reaper",
        item="sweep",
        source=home,
        target=Path(label),
        options={"label": label, **over},
    )


def _stub_launchctl(monkeypatch, *, loaded=False, spy=None):
    calls = spy if spy is not None else []

    def _bootout(path):
        calls.append(("bootout", path))
        return 0

    def _bootstrap(path):
        calls.append(("bootstrap", path))
        return 0

    monkeypatch.setattr(runner, "_launchctl_bootout", _bootout)
    monkeypatch.setattr(runner, "_launchctl_bootstrap", _bootstrap)
    monkeypatch.setattr(runner, "_launchctl_gui_loaded", lambda label: loaded)
    return calls


def test_apply_writes_artifacts_and_bootstraps(monkeypatch, tmp_path):
    _force_darwin(monkeypatch)
    home = _isolate_home(monkeypatch, tmp_path)
    spy = _stub_launchctl(monkeypatch, loaded=False)
    res = runner._do_provision_tgctl_reaper(_action(home), "backup")
    plist = home / "Library" / "LaunchAgents" / f"{tgctl_reaper.DEFAULT_LABEL}.plist"
    script = home / ".config" / "rig" / "tgctl-reaper" / "reap.py"
    assert res.status == "created", res.detail
    assert plist.is_file() and script.is_file()
    assert ("bootstrap", str(plist)) in spy


def test_apply_is_idempotent_when_loaded(monkeypatch, tmp_path):
    _force_darwin(monkeypatch)
    home = _isolate_home(monkeypatch, tmp_path)
    _stub_launchctl(monkeypatch, loaded=False)
    runner._do_provision_tgctl_reaper(_action(home), "backup")
    _stub_launchctl(monkeypatch, loaded=True)
    res2 = runner._do_provision_tgctl_reaper(_action(home), "backup")
    assert res2.status == "skipped", res2.detail


def test_reapply_against_identical_artifacts_does_not_rewrite(monkeypatch, tmp_path):
    _force_darwin(monkeypatch)
    home = _isolate_home(monkeypatch, tmp_path)
    _stub_launchctl(monkeypatch, loaded=False)
    runner._do_provision_tgctl_reaper(_action(home), "backup")
    script = home / ".config" / "rig" / "tgctl-reaper" / "reap.py"
    before = script.read_text(encoding="utf-8")
    _stub_launchctl(monkeypatch, loaded=True)
    runner._do_provision_tgctl_reaper(_action(home), "backup")
    assert script.read_text(encoding="utf-8") == before


def test_apply_backs_up_a_differing_plist(monkeypatch, tmp_path):
    """on_conflict=backup: an existing DIFFERING plist (e.g. from the OLD manually-installed
    predecessor, same label, different shape) is backed up before rewrite."""
    _force_darwin(monkeypatch)
    home = _isolate_home(monkeypatch, tmp_path)
    plist = home / "Library" / "LaunchAgents" / f"{tgctl_reaper.DEFAULT_LABEL}.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("OLD HAND-EDITED PLIST", encoding="utf-8")
    _stub_launchctl(monkeypatch, loaded=False)
    res = runner._do_provision_tgctl_reaper(_action(home), "backup")
    assert res.status == "backed_up", res.detail
    assert res.backup is not None and res.backup.is_file()
    assert tgctl_reaper.DEFAULT_LABEL in plist.read_text() and "OLD HAND-EDITED" not in plist.read_text()


def test_apply_off_darwin_is_skipped(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    home = _isolate_home(monkeypatch, tmp_path)
    res = runner._do_provision_tgctl_reaper(_action(home), "backup")
    assert res.status == "skipped"
    assert not (home / "Library").exists()


def test_dry_run_writes_artifacts_but_skips_launchctl(monkeypatch, tmp_path):
    _force_darwin(monkeypatch)
    home = _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setenv("RIG_TGCTL_REAPER_DRY_RUN", "1")
    spy = _stub_launchctl(monkeypatch, loaded=False)
    res = runner._do_provision_tgctl_reaper(_action(home), "backup")
    plist = home / "Library" / "LaunchAgents" / f"{tgctl_reaper.DEFAULT_LABEL}.plist"
    assert plist.is_file()
    assert spy == []
    assert "RIG_TGCTL_REAPER_DRY_RUN" in res.detail


# ── full apply path through run_plan ─────────────────────────────────────────────────────
def test_run_plan_provisions_tgctl_reaper(monkeypatch, tmp_path):
    from riglib.actions.runner import run_plan
    from riglib.plan import InstallPlan

    _force_darwin(monkeypatch)
    home = _isolate_home(monkeypatch, tmp_path)
    _stub_launchctl(monkeypatch, loaded=False)
    plan = InstallPlan(on_conflict="backup")
    plan.actions.append(_action(home))
    report = run_plan(plan)
    assert report.changed == 1


# ── drift ────────────────────────────────────────────────────────────────────────────────
def test_drift_tgctl_reaper_missing(monkeypatch, tmp_path):
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    _force_darwin(monkeypatch)
    home = _isolate_home(monkeypatch, tmp_path)
    plan = InstallPlan(on_conflict="backup")
    plan.actions.append(_action(home))
    report = detect(plan)
    drift = [d for d in report.items if d.category == "tgctl_reaper"]
    assert drift and drift[0].direction == "missing"


def test_drift_tgctl_reaper_in_sync_after_apply(monkeypatch, tmp_path):
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    _force_darwin(monkeypatch)
    home = _isolate_home(monkeypatch, tmp_path)
    _stub_launchctl(monkeypatch, loaded=True)
    monkeypatch.setattr("riglib.drift._launchctl_gui_loaded", lambda label: True)
    runner._do_provision_tgctl_reaper(_action(home), "backup")
    plan = InstallPlan(on_conflict="backup")
    plan.actions.append(_action(home))
    report = detect(plan)
    assert not [d for d in report.items if d.category == "tgctl_reaper"]


def test_drift_tgctl_reaper_modified(monkeypatch, tmp_path):
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    _force_darwin(monkeypatch)
    home = _isolate_home(monkeypatch, tmp_path)
    _stub_launchctl(monkeypatch, loaded=True)
    monkeypatch.setattr("riglib.drift._launchctl_gui_loaded", lambda label: True)
    runner._do_provision_tgctl_reaper(_action(home), "backup")
    plist = home / "Library" / "LaunchAgents" / f"{tgctl_reaper.DEFAULT_LABEL}.plist"
    plist.write_text("TAMPERED", encoding="utf-8")
    plan = InstallPlan(on_conflict="backup")
    plan.actions.append(_action(home))
    report = detect(plan)
    drift = [d for d in report.items if d.category == "tgctl_reaper"]
    assert drift and drift[0].direction == "modified"


def test_drift_tgctl_reaper_not_loaded(monkeypatch, tmp_path):
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    _force_darwin(monkeypatch)
    home = _isolate_home(monkeypatch, tmp_path)
    _stub_launchctl(monkeypatch, loaded=True)
    monkeypatch.setattr("riglib.drift._launchctl_gui_loaded", lambda label: True)
    runner._do_provision_tgctl_reaper(_action(home), "backup")
    _stub_launchctl(monkeypatch, loaded=False)
    monkeypatch.setattr("riglib.drift._launchctl_gui_loaded", lambda label: False)
    plan = InstallPlan(on_conflict="backup")
    plan.actions.append(_action(home))
    report = detect(plan)
    drift = [d for d in report.items if d.category == "tgctl_reaper"]
    assert drift and "not loaded" in drift[0].detail


def test_drift_off_darwin_is_silent(monkeypatch, tmp_path):
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    monkeypatch.setattr(sys, "platform", "linux")
    home = _isolate_home(monkeypatch, tmp_path)
    plan = InstallPlan(on_conflict="backup")
    plan.actions.append(_action(home))
    report = detect(plan)
    assert not [d for d in report.items if d.category == "tgctl_reaper"]


# ── plan building ────────────────────────────────────────────────────────────────────────
def _cfg(data, repo_root):
    from riglib.config import LoadedConfig

    return LoadedConfig(data=data, repo_root=repo_root)


def _build(data, repo_root, fake_agent_tools):
    from riglib.catalog import Catalog
    from riglib.plan import build

    cat = Catalog.scan(fake_agent_tools)
    return build(_cfg(data, repo_root), cat, project_type="unknown")


def test_status_no_action_in_plan_is_silent():
    """When ``tgctl_reaper.enabled: false``, plan.py emits NO action at all (mirrors
    ``tcc_watch``) — the status line must stay silent rather than claim a stale state."""
    from riglib import cli
    from riglib.drift import DriftReport
    from riglib.plan import InstallPlan

    plan = InstallPlan(on_conflict="backup")
    report = DriftReport()
    captured: list[str] = []
    import builtins

    original_print = builtins.print
    try:
        builtins.print = lambda *a, **k: captured.append(" ".join(str(x) for x in a))
        cli._print_tgctl_reaper_status(plan, report)
    finally:
        builtins.print = original_print
    assert captured == []


def test_plan_provisions_tgctl_reaper_by_default(fake_agent_tools, tmp_path):
    """tgctl_reaper is default-on: an ABSENT block still emits a provision_tgctl_reaper action."""
    plan = _build({}, tmp_path, fake_agent_tools)
    assert [a for a in plan.actions if a.kind == "provision_tgctl_reaper"]


def test_plan_no_tgctl_reaper_when_disabled(fake_agent_tools, tmp_path):
    plan = _build({"tgctl_reaper": {"enabled": False}}, tmp_path, fake_agent_tools)
    assert not [a for a in plan.actions if a.kind == "provision_tgctl_reaper"]


def test_plan_emits_single_tgctl_reaper_action(fake_agent_tools, tmp_path):
    plan = _build({"tgctl_reaper": {"enabled": True}}, tmp_path, fake_agent_tools)
    acts = [a for a in plan.actions if a.kind == "provision_tgctl_reaper"]
    assert len(acts) == 1
    assert acts[0].category == "tgctl_reaper" and acts[0].item == "sweep"


def test_plan_carries_knobs(fake_agent_tools, tmp_path):
    plan = _build(
        {"tgctl_reaper": {"interval_seconds": 60, "label": "com.me.tgctl-reaper", "config_dir": "/var/tgr"}},
        tmp_path, fake_agent_tools,
    )
    a = [a for a in plan.actions if a.kind == "provision_tgctl_reaper"][0]
    assert a.options["interval_seconds"] == 60
    assert a.options["label"] == "com.me.tgctl-reaper"
    assert a.options["config_dir"] == "/var/tgr"


def test_plan_label_null_resolves_to_default(fake_agent_tools, tmp_path):
    plan = _build({"tgctl_reaper": {"label": None}}, tmp_path, fake_agent_tools)
    action = [a for a in plan.actions if a.kind == "provision_tgctl_reaper"][0]
    tr = runner.tgctl_reaper_plan_from_action(action)
    assert tr.label == tgctl_reaper.DEFAULT_LABEL


# ── status line (GLOBAL section) ─────────────────────────────────────────────────────────
def _status_line(monkeypatch, tmp_path, *, darwin, drift_items=None, options_over=None):
    """Render the tgctl-reaper GLOBAL status line for a one-action plan, with `_on_darwin` forced."""
    from riglib import cli
    from riglib.drift import DriftReport
    from riglib.plan import InstallPlan

    monkeypatch.setattr("riglib.drift._on_darwin", lambda: darwin)
    home = _isolate_home(monkeypatch, tmp_path)
    plan = InstallPlan(on_conflict="backup")
    plan.actions.append(_action(home, **(options_over or {})))
    report = DriftReport()
    report.items.extend(drift_items or [])
    captured: list[str] = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: captured.append(" ".join(str(x) for x in a)))
    cli._print_tgctl_reaper_status(plan, report)
    return "\n".join(captured)


def test_status_off_darwin_says_unsupported(monkeypatch, tmp_path):
    line = _status_line(monkeypatch, tmp_path, darwin=False)
    assert "unsupported" in line
    assert "installed" not in line


def test_status_on_darwin_in_sync_says_installed(monkeypatch, tmp_path):
    line = _status_line(monkeypatch, tmp_path, darwin=True, drift_items=[])
    assert "installed" in line

