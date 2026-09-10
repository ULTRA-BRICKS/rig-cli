"""tcc-watch dialog-watcher daemon — config, pure plist/script rendering, install, detection.

Phase 1 (detection-only) of the defense-in-depth design behind the eval-tool TCC deny-list
(``riglib/permissions.py``, PR #488) — see :mod:`riglib.tcc_watch`'s module docstring for the
full phased design and :mod:`riglib.tcc_watch_daemon` for the detection logic under test here.

Mirrors :mod:`test_tg_ctl`'s shape: stdlib-only pure render tests for the plist/script, HOME-
isolated install/idempotency/conflict tests that NEVER touch the real ``~/Library/LaunchAgents``
or run a real ``launchctl`` (the gui-domain seams are stubbed per test), plus a dedicated section
replaying CANNED/sample ``log stream`` output through the daemon's parsing + watch-loop logic —
no real macOS permission dialog is ever triggered or required.
"""

from __future__ import annotations

import json
import plistlib
import sys
from pathlib import Path

import pytest

from riglib import tcc_watch
from riglib import tcc_watch_daemon as daemon
from riglib.actions import runner
from riglib.config import ConfigError, validate

# captured at import (before any monkeypatch runs) — the genuine handler.
_REAL_PROVISION = runner._do_provision_tcc_watch


@pytest.fixture(autouse=True)
def _real_tcc_watch(monkeypatch, live_launchd_home):
    """Restore the REAL provision_tcc_watch handler for THIS module's tests.

    conftest's autouse ``_isolate_scheduler`` stubs ``_do_provision_tcc_watch`` (+ the
    ``_HANDLERS`` entry) to a no-op so e2e tests can't touch the host launchd. These dedicated
    tests EXERCISE the real install logic — with HOME-isolated tmp dirs + stubbed launchctl
    seams — so they restore the real implementation.
    """
    monkeypatch.setattr(runner, "_do_provision_tcc_watch", _REAL_PROVISION)
    monkeypatch.setitem(runner._HANDLERS, "provision_tcc_watch", _REAL_PROVISION)
    # These tests assert on the LIVE (non-dry) provisioning path by default. A leaked ambient
    # RIG_TCC_WATCH_DRY_RUN would silently route them through the dry-run branch.
    monkeypatch.delenv("RIG_TCC_WATCH_DRY_RUN", raising=False)


def _force_darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")


def _isolate_home(monkeypatch, tmp_path) -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


# ── config validation ───────────────────────────────────────────────────────────────────
def test_tcc_watch_block_accepted():
    validate({"version": 1, "tcc_watch": {"enabled": True}})


def test_tcc_watch_block_empty_ok():
    validate({"version": 1, "tcc_watch": {}})


def test_tcc_watch_full_block_accepted():
    validate({
        "version": 1,
        "tcc_watch": {
            "enabled": True,
            "label": "ai.hyperide.tcc-watch",
            "config_dir": "~/.config/rig/tcc-watch",
            "python_bin": "/usr/bin/python3",
        },
    })


def test_tcc_watch_unknown_key_rejected():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tcc_watch": {"nope": 1}})


def test_tcc_watch_enabled_must_be_bool():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tcc_watch": {"enabled": "yes"}})


def test_tcc_watch_enabled_null_is_accepted():
    """`enabled: null` (explicit None) is valid and is NOT treated as disabled (default-on)."""
    validate({"version": 1, "tcc_watch": {"enabled": None}})


@pytest.mark.parametrize("key", ["label", "config_dir", "python_bin"])
def test_tcc_watch_string_keys_reject_non_string(key):
    with pytest.raises(ConfigError):
        validate({"version": 1, "tcc_watch": {key: 123}})


def test_tcc_watch_not_a_mapping_rejected():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tcc_watch": "nope"})


# ── pure plist rendering ─────────────────────────────────────────────────────────────────
def _plan(home="/Users/ultra", **over):
    over.setdefault("python_bin", str(Path(home) / "bin" / "python3"))
    return tcc_watch.build_tcc_watch(home=Path(home), **over)


def test_render_is_well_formed_and_labelled():
    p = _plan()
    body = p.render_plist()
    parsed = plistlib.loads(body.encode("utf-8"))
    assert parsed["Label"] == tcc_watch.DEFAULT_LABEL
    assert parsed["ProgramArguments"] == [str(p.python_bin), str(p.watcher_script_path), str(p.events_log_path)]
    assert parsed["RunAtLoad"] is True
    assert parsed["KeepAlive"] is True


def test_render_preserves_key_order_not_sorted():
    body = _plan().render_plist()
    assert body.index("<key>Label</key>") < body.index("<key>ProgramArguments</key>")
    assert body.index("<key>RunAtLoad</key>") < body.index("<key>StandardOutPath</key>")


def test_render_is_deterministic():
    assert _plan().render_plist() == _plan().render_plist()


def test_label_is_configurable():
    p = _plan(label="com.me.tcc-watch")
    assert plistlib.loads(p.render_plist().encode("utf-8"))["Label"] == "com.me.tcc-watch"
    assert p.plist_path.name == "com.me.tcc-watch.plist"


def test_paths_resolve_against_injected_home():
    p = _plan(home="/home/u", python_bin="/usr/bin/python3")
    assert p.plist_path == Path("/home/u/Library/LaunchAgents/ai.hyperide.tcc-watch.plist")
    assert p.config_dir == Path("/home/u/.config/rig/tcc-watch")
    assert p.events_log_path == Path("/home/u/.config/rig/tcc-watch/events.jsonl")
    assert p.watcher_script_path == Path("/home/u/.config/rig/tcc-watch/watch.py")


def test_explicit_config_dir_is_honored():
    p = _plan(config_dir="/var/tcc-watch")
    assert p.config_dir == Path("/var/tcc-watch")
    assert p.out_log_path == Path("/var/tcc-watch/launchd.tcc-watch.out.log")


def test_python_bin_falls_back_to_discovered_when_not_given(monkeypatch):
    monkeypatch.setattr(tcc_watch.shutil, "which", lambda name: "/opt/homebrew/bin/python3")
    p = tcc_watch.build_tcc_watch(home=Path("/home/u"))
    assert p.python_bin == Path("/opt/homebrew/bin/python3")


def test_python_bin_falls_back_to_system_path_when_not_on_path(monkeypatch, tmp_path):
    monkeypatch.setattr(tcc_watch.shutil, "which", lambda name: None)
    fake_sys_python = tmp_path / "usr_bin_python3"
    fake_sys_python.write_text("", encoding="utf-8")
    monkeypatch.setattr(tcc_watch, "_PYTHON3_FALLBACK_PATHS", (str(fake_sys_python),))
    p = tcc_watch.build_tcc_watch(home=Path("/home/u"))
    assert p.python_bin == fake_sys_python


# ── watcher script rendering: one source of truth ────────────────────────────────────────
def test_render_watcher_script_matches_daemon_module_source():
    """The rendered script is byte-for-byte the SAME source this test file imports and exercises
    directly — no drift between the tested detection logic and the deployed daemon."""
    p = _plan()
    assert p.render_watcher_script() == Path(daemon.__file__).read_text(encoding="utf-8")


def test_render_watcher_script_is_valid_python():
    compile(_plan().render_watcher_script(), "<watch.py>", "exec")


# ── detection logic: parse_tcc_log_line, replayed against SAMPLE log-stream lines ─────────
# Grounded in the real, documented com.apple.TCC `log stream --style ndjson --debug` shape:
# every per-service decision embeds a `kTCCService*` identifier, and Apple-admin TCC
# troubleshooting guidance filters on `AttributionChain` (the requesting/responsible/accessing
# process record). No real permission prompt was triggered to build these fixtures.
_SAMPLE_PROMPT_LINE = json.dumps({
    "timestamp": "2026-09-10 12:00:00.123456-0700",
    "subsystem": "com.apple.TCC",
    "category": "access",
    "eventMessage": "Prompting policy for service kTCCServicePhotos and bundle identifier com.example.agent",
    "processID": 4242,
    "processImagePath": "/usr/libexec/tccd",
})
_SAMPLE_ATTRIBUTION_LINE = json.dumps({
    "timestamp": "2026-09-10 12:00:01.000000-0700",
    "subsystem": "com.apple.TCC",
    "category": "access",
    "eventMessage": "AttributionChain: RESP={id=1, pid=500} ACC={id=2, pid=4242} REQ={id=2, pid=4242}",
    "processID": 4242,
    "processImagePath": "/usr/libexec/tccd",
})
_SAMPLE_ROUTINE_LINE = json.dumps({
    "timestamp": "2026-09-10 12:00:02.000000-0700",
    "subsystem": "com.apple.TCC",
    "category": "database",
    "eventMessage": "Opened TCC database at path /Library/Application Support/com.apple.TCC/TCC.db",
    "processID": 100,
})
_SAMPLE_OTHER_SUBSYSTEM_LINE = json.dumps({
    "timestamp": "2026-09-10 12:00:03.000000-0700",
    "subsystem": "com.apple.something.else",
    "eventMessage": "Prompting policy for service kTCCServiceCamera",
})


def test_parse_prompt_line_is_detected():
    event = daemon.parse_tcc_log_line(_SAMPLE_PROMPT_LINE)
    assert event is not None
    assert event.pid == 4242
    assert "kTCCServicePhotos" in event.message


def test_parse_attribution_line_is_detected():
    event = daemon.parse_tcc_log_line(_SAMPLE_ATTRIBUTION_LINE)
    assert event is not None
    assert "AttributionChain" in event.message


def test_parse_routine_tcc_line_is_not_a_prompt():
    assert daemon.parse_tcc_log_line(_SAMPLE_ROUTINE_LINE) is None


def test_parse_other_subsystem_is_ignored():
    assert daemon.parse_tcc_log_line(_SAMPLE_OTHER_SUBSYSTEM_LINE) is None


def test_parse_blank_line_is_none():
    assert daemon.parse_tcc_log_line("") is None
    assert daemon.parse_tcc_log_line("   \n") is None


def test_parse_malformed_json_does_not_raise():
    assert daemon.parse_tcc_log_line("{not valid json") is None


def test_parse_non_object_json_does_not_raise():
    assert daemon.parse_tcc_log_line("[1, 2, 3]") is None
    assert daemon.parse_tcc_log_line('"just a string"') is None


def test_iter_events_filters_a_replayed_stream():
    lines = [_SAMPLE_PROMPT_LINE, _SAMPLE_ROUTINE_LINE, _SAMPLE_ATTRIBUTION_LINE, "", "{bad"]
    events = list(daemon.iter_events(lines))
    assert len(events) == 2


# ── append_event + the watch loop, fully injected (no subprocess, no real log stream) ─────
def test_append_event_writes_one_json_line(tmp_path):
    events_log = tmp_path / "events.jsonl"
    event = daemon.parse_tcc_log_line(_SAMPLE_PROMPT_LINE)
    daemon.append_event(events_log, event)
    daemon.append_event(events_log, event)
    lines = events_log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    row = json.loads(lines[0])
    assert row["pid"] == 4242
    assert "kTCCServicePhotos" in row["message"]


class _FakeStreamProc:
    """A fake `log stream` subprocess handle: replays canned lines, then reports an exit code."""

    def __init__(self, lines, returncode=0):
        self.stdout = iter(lines)
        self.returncode = returncode

    def wait(self):
        return self.returncode


def test_run_appends_detected_events_from_a_replayed_stream(tmp_path):
    events_log = tmp_path / "events.jsonl"
    lines = [_SAMPLE_PROMPT_LINE, _SAMPLE_ROUTINE_LINE, _SAMPLE_ATTRIBUTION_LINE]
    sleeps: list[float] = []
    appended = daemon.run(
        events_log,
        stream_factory=lambda: _FakeStreamProc(lines),
        sleep=sleeps.append,
        max_reconnects=1,
    )
    assert appended == 2
    assert len(events_log.read_text(encoding="utf-8").strip().splitlines()) == 2


def test_run_reconnects_after_the_stream_exits(tmp_path):
    """A `log stream` exit (daemon restart, transient failure) is followed by a reconnect —
    proven by exhausting a bounded `max_reconnects` across TWO fake connections."""
    events_log = tmp_path / "events.jsonl"
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        return _FakeStreamProc([_SAMPLE_PROMPT_LINE] if calls["n"] == 2 else [])

    sleeps: list[float] = []
    appended = daemon.run(events_log, stream_factory=factory, sleep=sleeps.append, max_reconnects=2)
    assert calls["n"] == 2
    assert appended == 1
    assert sleeps  # backoff/reconnect sleep was invoked


def test_run_backs_off_on_spawn_failure(tmp_path):
    events_log = tmp_path / "events.jsonl"
    attempts = {"n": 0}

    def factory():
        attempts["n"] += 1
        raise OSError("log: command not found")

    sleeps: list[float] = []
    daemon.run(events_log, stream_factory=factory, sleep=sleeps.append, max_reconnects=3)
    assert attempts["n"] == 3
    assert not events_log.exists()  # never got a stream, so nothing to append


# ── install (runner) — write + (re)load, idempotent, conflict ─────────────────────────────
def _action(home, **over):
    from riglib.plan import Action

    return Action(
        kind="provision_tcc_watch",
        category="tcc_watch",
        item="watch",
        source=home,
        target=Path(tcc_watch.DEFAULT_LABEL),
        options={"label": None, "config_dir": None, "python_bin": str(home / "bin" / "python3"), **over},
    )


def _stub_launchctl(monkeypatch, *, loaded=False, spy=None):
    calls = spy if spy is not None else []

    def _bootout(plist_path):
        calls.append(("bootout", plist_path))
        return 0

    def _bootstrap(plist_path):
        calls.append(("bootstrap", plist_path))
        return 0

    monkeypatch.setattr(runner, "_launchctl_bootout", _bootout)
    monkeypatch.setattr(runner, "_launchctl_bootstrap", _bootstrap)
    monkeypatch.setattr(runner, "_launchctl_gui_loaded", lambda label: loaded)
    return calls


def test_apply_writes_artifacts_and_bootstraps(monkeypatch, tmp_path):
    _force_darwin(monkeypatch)
    home = _isolate_home(monkeypatch, tmp_path)
    spy = _stub_launchctl(monkeypatch, loaded=False)
    res = runner._do_provision_tcc_watch(_action(home), "backup")
    assert res.status in ("created", "backed_up"), res.detail
    plist = home / "Library" / "LaunchAgents" / f"{tcc_watch.DEFAULT_LABEL}.plist"
    script = home / ".config" / "rig" / "tcc-watch" / "watch.py"
    assert plist.is_file()
    assert script.is_file()
    assert script.read_text(encoding="utf-8") == Path(daemon.__file__).read_text(encoding="utf-8")
    assert ("bootstrap", str(plist)) in spy


def test_apply_is_idempotent_when_loaded(monkeypatch, tmp_path):
    _force_darwin(monkeypatch)
    home = _isolate_home(monkeypatch, tmp_path)
    _stub_launchctl(monkeypatch, loaded=False)
    runner._do_provision_tcc_watch(_action(home), "backup")
    _stub_launchctl(monkeypatch, loaded=True)
    res2 = runner._do_provision_tcc_watch(_action(home), "backup")
    assert res2.status == "skipped", res2.detail


def test_reapply_against_identical_artifacts_does_not_rewrite(monkeypatch, tmp_path):
    _force_darwin(monkeypatch)
    home = _isolate_home(monkeypatch, tmp_path)
    _stub_launchctl(monkeypatch, loaded=False)
    runner._do_provision_tcc_watch(_action(home), "backup")
    script = home / ".config" / "rig" / "tcc-watch" / "watch.py"
    before = script.read_text(encoding="utf-8")
    _stub_launchctl(monkeypatch, loaded=True)
    runner._do_provision_tcc_watch(_action(home), "backup")
    assert script.read_text(encoding="utf-8") == before


def test_apply_backs_up_a_differing_plist(monkeypatch, tmp_path):
    _force_darwin(monkeypatch)
    home = _isolate_home(monkeypatch, tmp_path)
    plist = home / "Library" / "LaunchAgents" / f"{tcc_watch.DEFAULT_LABEL}.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("OLD HAND-EDITED", encoding="utf-8")
    _stub_launchctl(monkeypatch, loaded=False)
    res = runner._do_provision_tcc_watch(_action(home), "backup")
    assert res.status == "backed_up", res.detail
    assert res.backup is not None and res.backup.is_file()
    assert "ai.hyperide.tcc-watch" in plist.read_text() and "OLD HAND-EDITED" not in plist.read_text()


def test_apply_off_darwin_is_skipped(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    home = _isolate_home(monkeypatch, tmp_path)
    spy = _stub_launchctl(monkeypatch, loaded=False)
    res = runner._do_provision_tcc_watch(_action(home), "backup")
    assert res.status == "skipped"
    assert spy == []
    assert not (home / "Library").exists()


def test_dry_run_writes_artifacts_but_skips_launchctl(monkeypatch, tmp_path):
    _force_darwin(monkeypatch)
    home = _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setenv("RIG_TCC_WATCH_DRY_RUN", "1")
    spy = _stub_launchctl(monkeypatch, loaded=False)
    res = runner._do_provision_tcc_watch(_action(home), "backup")
    assert (home / "Library" / "LaunchAgents" / f"{tcc_watch.DEFAULT_LABEL}.plist").is_file()
    assert spy == []
    assert "RIG_TCC_WATCH_DRY_RUN" in res.detail


# ── full apply path through run_plan ─────────────────────────────────────────────────────
def test_run_plan_provisions_tcc_watch(monkeypatch, tmp_path):
    from riglib.actions.runner import run_plan
    from riglib.plan import InstallPlan

    _force_darwin(monkeypatch)
    home = _isolate_home(monkeypatch, tmp_path)
    _stub_launchctl(monkeypatch, loaded=False)
    report = run_plan(InstallPlan(actions=[_action(home)]))
    assert not report.errors
    assert report.changed == 1


# ── plan building ────────────────────────────────────────────────────────────────────────
def _cfg(data, repo_root):
    from riglib.config import LoadedConfig

    return LoadedConfig(data=data, repo_root=repo_root)


def _build(data, repo_root, fake_agent_tools):
    from riglib.catalog import Catalog
    from riglib.plan import build

    cat = Catalog(fake_agent_tools)
    return build(_cfg(data, repo_root), cat, project_type="unknown")


def test_plan_provisions_tcc_watch_by_default(fake_agent_tools, tmp_path):
    """tcc_watch is default-on: an ABSENT block still emits a provision_tcc_watch action."""
    plan = _build({}, tmp_path, fake_agent_tools)
    assert [a for a in plan.actions if a.kind == "provision_tcc_watch"]


def test_plan_no_tcc_watch_when_disabled(fake_agent_tools, tmp_path):
    plan = _build({"tcc_watch": {"enabled": False}}, tmp_path, fake_agent_tools)
    assert not [a for a in plan.actions if a.kind == "provision_tcc_watch"]


def test_plan_emits_single_tcc_watch_action(fake_agent_tools, tmp_path):
    plan = _build({"tcc_watch": {"enabled": True}}, tmp_path, fake_agent_tools)
    acts = [a for a in plan.actions if a.kind == "provision_tcc_watch"]
    assert len(acts) == 1
    assert acts[0].category == "tcc_watch" and acts[0].item == "watch"


def test_plan_carries_knobs(fake_agent_tools, tmp_path):
    plan = _build(
        {"tcc_watch": {"config_dir": "/var/tcc-watch", "label": "com.me.tcc-watch"}},
        tmp_path, fake_agent_tools,
    )
    a = [a for a in plan.actions if a.kind == "provision_tcc_watch"][0]
    assert a.options["config_dir"] == "/var/tcc-watch"
    assert a.options["label"] == "com.me.tcc-watch"


def test_plan_label_null_resolves_to_default(fake_agent_tools, tmp_path):
    plan = _build({"tcc_watch": {"label": None}}, tmp_path, fake_agent_tools)
    a = [a for a in plan.actions if a.kind == "provision_tcc_watch"][0]
    tw = runner.tcc_watch_plan_from_action(a)
    assert tw.label == tcc_watch.DEFAULT_LABEL
