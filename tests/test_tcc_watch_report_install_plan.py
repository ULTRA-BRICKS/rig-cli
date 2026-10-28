"""tcc-watch tests: report, install via the runner, and plan building."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from riglib import tcc_watch
from riglib import tcc_watch_daemon as daemon
from riglib.actions import runner
from riglib.actions import tcc_watch_action as tcc_watch_action_mod
from tests.tcc_watch_helpers import (
    _ROUTINE_MESSAGES,
    _T0,
    _action,
    _build,
    _counts,
    _FakeStreamProc,
    _force_darwin,
    _isolate_home,
    _seeded_store,
    _store_path,
    _stub_launchctl,
    _tcc_line,
    open_store,
    restore_real_tcc_watch,
)


@pytest.fixture
def store(tmp_path):
    yield from open_store(tmp_path)


@pytest.fixture(autouse=True)
def _real_tcc_watch(monkeypatch, live_launchd_home):
    restore_real_tcc_watch(monkeypatch)


def test_report_reads_the_store_beside_the_deployed_script(tmp_path, monkeypatch):
    """With a custom config_dir the deployed watch.py and its store sit together there."""
    custom = tmp_path / "custom-config-dir"
    custom.mkdir()
    (custom / daemon.STORE_NAME).write_bytes(b"")
    monkeypatch.setattr(daemon, "__file__", str(custom / "watch.py"))
    assert daemon._report_store() == custom / daemon.STORE_NAME
    (custom / daemon.STORE_NAME).unlink()
    assert daemon._report_store() == daemon._default_store()


def test_trim_refuses_any_other_table(store):
    with pytest.raises(ValueError):
        store._trim("templates; DROP TABLE counts", 1)


def test_a_crash_is_reported_through_the_size_gated_writer(tmp_path, monkeypatch):
    def boom(_path):
        raise TypeError("a bug nobody foresaw")

    monkeypatch.setattr(daemon, "run", boom)
    monkeypatch.setattr(daemon.signal, "signal", lambda *_a: None)
    monkeypatch.setattr(daemon.os, "umask", lambda _m: 0)
    err = tmp_path / "err.log"
    with err.open("a", encoding="utf-8") as fh:
        monkeypatch.setattr(sys, "stderr", fh)
        assert daemon.main([str(tmp_path / "s.sqlite3")]) == 1
    assert "crashed: Traceback" in err.read_text(encoding="utf-8")
    assert "TypeError: a bug nobody foresaw" in err.read_text(encoding="utf-8")
    # once err.log is past the cap, a crash-looping bug adds nothing more
    monkeypatch.setattr(daemon, "MAX_STDERR_BYTES", 10)
    size = err.stat().st_size
    with err.open("a", encoding="utf-8") as fh:
        monkeypatch.setattr(sys, "stderr", fh)
        assert daemon.main([str(tmp_path / "s.sqlite3")]) == 1
    assert err.stat().st_size == size


def test_a_store_with_an_unknown_layout_is_moved_aside_not_misread(tmp_path):
    path = _store_path(tmp_path)
    s = daemon.TccStore(path)
    s.db.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    s.db.commit()
    s.db.close()
    fresh = daemon.open_store(path, sleep=lambda _s: None)
    assert fresh.db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone() == ("1",)
    fresh.db.close()
    assert any(p.name.startswith(f"{daemon.STORE_NAME}.unreadable-") for p in path.parent.iterdir())


def test_run_flushes_counts_on_its_minute_cadence(tmp_path):
    path = _store_path(tmp_path)
    ticks = iter(range(0, 10**7, 61))  # every clock() call is 61 s later
    seen_at_spawn = []

    def stream():
        yield _tcc_line(_ROUTINE_MESSAGES[0])
        yield _tcc_line(_ROUTINE_MESSAGES[0])  # this line's timer check flushes both
        reader = daemon.TccStore(path, readonly=True)
        seen_at_spawn.append(reader.totals())
        reader.close()

    daemon.run(path, stream_factory=lambda: _FakeStreamProc(stream()), sleep=lambda _s: None,
               clock=lambda: float(next(ticks)), max_reconnects=1)
    assert seen_at_spawn == [(2, 0)]  # visible to another connection before the stream ended


def test_a_line_without_a_timestamp_is_counted_now(store):
    obs = daemon.parse_observation(_tcc_line(_ROUTINE_MESSAGES[0], ts="garbage"))
    store.observe(obs)
    store.flush()
    assert _counts(store)[0][0] == _T0  # the store clock's hour


def test_report_prints_what_fills_the_log(tmp_path, capsys):
    path = _seeded_store(tmp_path)
    assert daemon.main(["report", "--store", str(path), "--days", "2"]) == 0
    out = capsys.readouterr().out
    assert "TCC log lines since" in out and ": 6" in out
    assert "noise" in out and "prompting" in out
    assert "com.example.chat" in out
    assert "AUTHREQ_PROMPTING" in out


def test_report_json(tmp_path, capsys):
    path = _seeded_store(tmp_path)
    assert daemon.main(["report", "--store", str(path), "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["lines"] == 6
    assert summary["recent_prompts"][0]["kind"] == "prompting"


def test_report_without_a_store_fails_cleanly(tmp_path, capsys):
    assert daemon.main(["report", "--store", str(tmp_path / "missing.sqlite3")]) == 1
    assert "no store" in capsys.readouterr().err
    assert not (tmp_path / "missing.sqlite3").exists()


# ── install (runner) — write + (re)load, idempotent, conflict ─────────────────────────────

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
    tw = tcc_watch_action_mod.tcc_watch_plan_from_action(a)
    assert tw.label == tcc_watch.DEFAULT_LABEL
