"""The sysmon handler session (rig-cli#700): spawn, fallback, startup and retire decisions with an
injected clock, the pending-alert queue, the spawn/retire argv, the brief, and the tmux/tg-ctl
wrappers against fake runners."""

from __future__ import annotations

import errno
import json
import subprocess
from pathlib import Path

import pytest

from riglib.sysmon_daemon import config, handler
from riglib.sysmon_daemon.handler import Holder, Window
from riglib.sysmon_daemon.model import SEVERITY_CRITICAL, SEVERITY_WARNING, Finding

MIN = 60.0
T = 100_000.0
VACANT, BOUND = Holder("vacant"), Holder("bound", "s-user", 77, False)


def _cfg(tmp_path=None, **handler_over):
    cfg = config.load(None)
    if tmp_path is not None:
        cfg["state_dir"] = str(tmp_path / "state")
    cfg["handler"].update(handler_over)
    return cfg


def _f(key="load", sev=SEVERITY_CRITICAL):
    return Finding(key=key, kind="memory", severity=sev, summary=f"{key} bad")


def _due(findings, state=None, holder=VACANT, window=False, post_crash=False, now=T, cfg=None):
    return handler.spawn_due(findings, state or {}, now, cfg or _cfg(), holder=holder, window_exists=window,
                             post_crash=post_crash)[0]


# ── spawn ─────────────────────────────────────────────────────────────────────────────────
def test_spawn_needs_vacancy_and_a_trigger():
    assert _due([_f()]) is True
    for h in (BOUND, Holder("suspended", "s", 1), Holder("unknown")):
        assert _due([_f()], holder=h) is False
    assert _due([]) is False


def test_warning_must_persist_15_minutes():
    cfg = _cfg()
    w = [_f("swap", SEVERITY_WARNING)]
    st = handler.update_first_seen(w, {}, T)
    assert _due(w, handler.update_first_seen(w, st, T + 15 * MIN - 1), now=T + 15 * MIN - 1, cfg=cfg) is False
    st = handler.update_first_seen(w, st, T + 15 * MIN)
    assert _due(w, st, now=T + 15 * MIN, cfg=cfg) is True
    st = handler.update_first_seen([], st, T + 16 * MIN)  # cleared
    st = handler.update_first_seen(w, st, T + 17 * MIN)  # back: the clock starts over
    assert _due(w, st, now=T + 20 * MIN, cfg=cfg) is False
    assert st["first_seen"] == {"swap": T + 17 * MIN}


def test_spawn_cooldown_and_window_block():
    assert _due([_f()], {"last_spawn_at": T - 59 * MIN}) is False
    assert _due([_f()], {"last_spawn_at": T - 60 * MIN}) is True
    # Review Focus 4: a window the user closed is not respawned inside the cooldown, and an open one never
    assert _due([_f()], window=True) is False


def test_post_crash_triggers_spawn():
    assert _due([], post_crash=True) is True


# ── Telegram fallback ─────────────────────────────────────────────────────────────────────
def _fallback(state, holder=VACANT, window=None, now=T):
    return handler.telegram_fallback_due(state, now, _cfg(), holder=holder, window=window)


def test_fallback_only_when_the_handler_cannot_help():
    win = Window("@5", 100)
    assert _fallback({}, Holder("unknown")) is True  # a broken channel, fresh state
    assert _fallback({}) is False  # never spawned: the spawn check decides
    assert _fallback({"last_spawn_at": T - 90 * MIN, "last_spawn_ok": False}) is True
    assert _fallback({"last_spawn_at": T - 10 * MIN, "last_spawn_ok": True}) is True  # cooldown, no window
    spawned = {"last_spawn_at": T - 4 * MIN, "window_id": "@5", "window_created_at": T - 4 * MIN}
    assert _fallback(spawned, window=win) is False  # still starting
    assert _fallback({**spawned, "window_created_at": T - 5 * MIN}, window=win) is True
    assert _fallback({"last_spawn_ok": False}, BOUND) is False
    assert _fallback({"last_spawn_ok": False}, Holder("suspended", "s", 1)) is False


def test_foreign_handler_window_with_no_state():
    """A sysmon-handler window sysmon has no state for ages from when sysmon first saw it."""
    win = Window("@9", 300)
    st = handler.observe_window({}, win, T)
    assert handler.observe_window(st, win, T + MIN) == st  # first sight kept
    assert _fallback(st, window=win, now=T + 4 * MIN) is False
    assert _fallback(st, window=win, now=T + 5 * MIN) is True
    assert "foreign_window" not in handler.observe_window(st, None, T)


def test_startup_failed_only_when_still_vacant():
    cfg = _cfg()
    assert handler.startup_failed({}, T, cfg, holder=VACANT) is False  # never spawned
    st = {"last_spawn_at": T}
    assert handler.startup_failed(st, T + 5 * MIN - 1, cfg, holder=VACANT) is False
    assert handler.startup_failed(st, T + 5 * MIN, cfg, holder=VACANT) is True
    assert handler.startup_failed(st, T + 5 * MIN, cfg, holder=BOUND) is False  # another session took it
    eps = [{"pid": 50, "sessionId": "x"}, {"pid": 101, "sessionId": "s-h"}]
    ep = handler.find_handler_endpoint(100, eps, lambda child, anc: (child, anc) == (101, 100))
    assert ep == {"pid": 101, "sessionId": "s-h"}


def test_is_descendant_walks_proc_for_a_real_child():
    import os

    if not Path("/proc/self/stat").exists():
        pytest.skip("no /proc")
    child = subprocess.Popen(["sleep", "30"])
    try:
        assert handler.is_descendant(child.pid, os.getpid()) is True
        assert handler.is_descendant(os.getpid(), child.pid) is False
        assert handler.is_descendant(2 ** 31 - 2, os.getpid()) is False  # no such pid
    finally:
        child.terminate()
        child.wait(timeout=10)


# ── pending ───────────────────────────────────────────────────────────────────────────────
def test_pending_replays_only_current_findings():
    cfg = _cfg()
    st = handler.queue_pending({}, ["load", "swap"], T)
    found, crash, st2 = handler.due_pending(st, [_f("swap", SEVERITY_WARNING)], T + MIN, cfg)
    assert [f.key for f in found] == ["swap"] and crash is None
    assert list(st2["pending"]["keys"]) == ["swap"]
    assert handler.ack_pending(st2, ["swap"], crash=False)["pending"]["keys"] == {}
    found, _, st3 = handler.due_pending(st, [], T + MIN, cfg)
    assert found == [] and st3["pending"]["keys"] == {}
    many = handler.queue_pending({}, [f"k{i}" for i in range(20)], T)
    many = handler.queue_pending(many, ["k20"], T + 1)
    assert len(many["pending"]["keys"]) == 20 and "k20" in many["pending"]["keys"]
    # the oldest is evicted: a later key replaces the first-deferred one
    many = handler.queue_pending(handler.queue_pending({}, ["old"], T - 10), [f"k{i}" for i in range(20)], T)
    assert "old" not in many["pending"]["keys"]
    ttl = 60 * max(cfg["alert"][k] for k in ("cooldown_minutes", "disk_cooldown_minutes", "swap_cooldown_minutes"))
    found, _, _ = handler.due_pending(st, [_f("swap")], T + ttl, cfg)
    assert found == []  # older than the longest alert cooldown
    crash = {"title": "t", "body": "b", "at": T}
    st = handler.queue_pending({}, [], T, crash=crash)
    found, got, st = handler.due_pending(st, [], T + MIN, cfg)
    assert found == [] and got == crash and st["pending"]["crash"] == crash
    assert handler.ack_pending(st, [], crash=True)["pending"]["crash"] is None
    _, got, st = handler.due_pending(st, [], T + ttl, cfg)
    assert got is None and st["pending"]["crash"] is None  # a crash nobody took for that long is stale


# ── retire ────────────────────────────────────────────────────────────────────────────────
def _quiet(**over):
    return {"handler_pid": 101, "handler_session_id": "s-h", "window_id": "@5", "pane_pid": 100,
            "last_spawn_at": T - 120 * MIN, "last_finding_at": T - 31 * MIN, "last_delivery_at": T - 31 * MIN, **over}


def test_retire_only_the_spawned_idle_handler():
    cfg = _cfg()
    # Review Focus 5: the holder is the user's own session, idle for hours: no handler_pid, never retired
    assert handler.retire_due([], {"last_spawn_at": T - 600 * MIN}, T, cfg, handler_busy=False) is False
    assert handler.retire_due([], _quiet(), T, cfg, handler_busy=False) is True
    assert handler.retire_due([], _quiet(), T, cfg, handler_busy=True) is False
    assert handler.retire_due([], _quiet(), T, cfg, handler_busy=None) is False
    assert handler.retire_due([_f("swap", SEVERITY_WARNING)], _quiet(), T, cfg, handler_busy=False) is False
    assert handler.retire_due([], _quiet(last_delivery_at=T - 29 * MIN), T, cfg, handler_busy=False) is False
    assert handler.retire_due([], _quiet(last_finding_at=T - 29 * MIN), T, cfg, handler_busy=False) is False
    argvs = handler.retire_argvs(cfg, "@5", 101, "s-h", holds=False, now_ms=7)
    assert not any("release" in a for a in argvs)  # handed off: nothing to release
    assert argvs[0][:4] == ["tg-ctl", "omp-live", "send", "101"] and handler.alert.flag_value(argvs[0], "--session") == "s-h"
    assert handler.alert.flag_value(argvs[0], "--request-id") == "sysmon-handler-retire-7"
    assert argvs[0][-2] == "--" and "this handler window is being closed" in argvs[0][-1]
    assert argvs[-1] == ["tmux", "kill-window", "-t", "@5"]
    held = handler.retire_argvs(cfg, "@5", 101, "s-h", holds=True, now_ms=7)
    assert held[0] == ["tg-ctl", "alert", "release", "--source", "sysmon", "--session", "s-h"]


class _Fake:
    """Scripted runner: ``answers`` maps an argv prefix (tuple) to ``(code, stdout)`` or an exception."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        for prefix, answer in sorted(self.answers.items(), key=lambda kv: -len(kv[0])):
            if tuple(argv[:len(prefix)]) == prefix:
                if isinstance(answer, BaseException):
                    raise answer
                code, out = answer
                return subprocess.CompletedProcess(argv, code, out, "" if code in (0, 3) else out)
        return subprocess.CompletedProcess(argv, 1, "", "unscripted")

    def ran(self, *prefix):
        return [c for c in self.calls if tuple(c[:len(prefix)]) == prefix]


LIVE_HANDLER = json.dumps([{"pid": 101, "sessionId": "s-h", "busy": False}])


def test_retire_kills_the_stored_window_after_proving_it(tmp_path):
    run = _Fake({("tg-ctl", "omp-live", "status"): (0, LIVE_HANDLER),
                 ("tmux", "list-panes", "-t", "@5"): (0, "100\n"), ("tg-ctl",): (0, ""), ("tmux", "kill-window"): (0, "")})
    rec, st = handler.retire(_cfg(tmp_path), _quiet(), Holder("bound", "s-h", 101), run, T)
    assert rec["outcome"] == "retired" and "window_id" not in st and "handler_pid" not in st
    assert run.ran("tg-ctl", "alert", "release") and run.ran("tmux", "kill-window", "-t", "@5")
    assert st["last_spawn_at"] == T - 120 * MIN  # the cooldown survives


@pytest.mark.parametrize("answers", [
    {("tmux", "list-panes", "-t", "@5"): (0, "999\n")},  # the window now runs another pane
    {("tmux", "list-panes", "-t", "@5"): (0, "100\n555\n")},  # the user split it: their pane would die
    {("tmux", "list-panes", "-t", "@5"): (1, "can't find window")},
])
def test_retire_skips_a_window_that_is_not_the_spawned_one(tmp_path, answers):
    run = _Fake({("tg-ctl", "omp-live", "status"): (0, LIVE_HANDLER), **answers})
    rec, st = handler.retire(_cfg(tmp_path), _quiet(), BOUND, run, T)
    assert rec["outcome"] == "skipped" and "not the one sysmon spawned" in rec["detail"]
    assert not run.ran("tmux", "kill-window") and not run.ran("tg-ctl", "alert", "release")
    assert not run.ran("tg-ctl", "omp-live", "send") and "window_id" not in st


@pytest.mark.parametrize("raw", [
    "x", {"pending": {"keys": "k", "crash": 3}}, {"first_seen": [1]}, {"last_spawn_at": "soon"},
    {"window_id": "@5", "pane_pid": "100"}, {"holder": {"state": "bound", "evil": 1}},
    {"pending": {"keys": {"swap": "t"}, "crash": {"title": "t"}}}, {"foreign_window": {"window_id": 3}},
    {"last_spawn_at": float("inf"), "first_seen": {"load": float("inf")}},
])
def test_a_malformed_persisted_state_never_breaks_the_tick(tmp_path, raw):
    st = handler.load_state(raw)
    run = _Fake({("tg-ctl", "alert", "holder"): (3, _holder_doc()), ("tmux", "list-panes"): (1, ""),
                 ("tmux", "has-session"): (0, ""), ("tmux", "new-window"): (0, "@5 100\n")})
    _, records, _ = handler.tick([_f()], st, T, _cfg(tmp_path), run, alert_text="a")
    handler.due_pending(st, [_f()], T, _cfg())
    assert [r["outcome"] for r in records] == ["spawned"]  # nothing malformed held the spawn back
    assert handler.load_state(_quiet()) == _quiet()  # a well-formed state survives whole


def test_retire_keeps_a_handler_that_turned_busy_since_the_cached_reading(tmp_path):
    run = _Fake({("tg-ctl", "omp-live", "status"): (0, json.dumps([{"pid": 101, "sessionId": "s-h", "busy": True}])),
                 ("tmux", "list-panes", "-t", "@5"): (0, "100\n")})
    rec, st = handler.retire(_cfg(tmp_path), _quiet(), BOUND, run, T)
    assert rec["outcome"] == "skipped" and "busy" in rec["detail"]
    assert st["window_id"] == "@5" and st["handler_busy"] is True  # still sysmon's handler, retired later
    assert [c[:3] for c in run.calls] == [["tg-ctl", "omp-live", "status"]]


def test_retire_skips_a_reused_pid(tmp_path):
    run = _Fake({("tg-ctl", "omp-live", "status"): (0, json.dumps([{"pid": 101, "sessionId": "other", "busy": False}])),
                 ("tmux", "list-panes", "-t", "@5"): (0, "100\n")})
    rec, st = handler.retire(_cfg(tmp_path), _quiet(), BOUND, run, T)
    assert rec["outcome"] == "skipped"
    assert [c[:3] for c in run.calls] == [["tg-ctl", "omp-live", "status"]] and "handler_pid" not in st


# ── argv, brief, kickoff ──────────────────────────────────────────────────────────────────
def test_spawn_argv_new_window_and_new_session(monkeypatch):
    monkeypatch.setattr(handler.shutil, "which", lambda name: f"/abs/{name}")
    cfg = _cfg(tmux_session="0", workdir="/w")
    head = ["-n", "sysmon-handler", "-c", "/w", "/abs/omp", "--auto-approve", "--append-system-prompt", "/b.md", "go"]
    assert handler.spawn_argv(cfg, Path("/b.md"), "go", session_exists=True) == [
        "tmux", "new-window", "-d", "-P", "-F", "#{window_id} #{pane_pid}", "-t", "=0:", *head]
    assert handler.spawn_argv(cfg, Path("/b.md"), "go", session_exists=False) == [
        "tmux", "new-session", "-d", "-P", "-F", "#{window_id} #{pane_pid}", "-s", "0", *head]
    assert handler.tmux_session(_cfg()) == "main"  # default: sessions.tmux_session


def test_brief_has_every_section_and_the_never_list():
    brief = handler.build_brief(_cfg())
    for needle in ("tg-ctl alert subscribe --exclusive --source sysmon --ttl 0", "docker builder prune -f",
                   "memguard swap-drain", "tg --tag decision", "tg --tag problem", "tg --tag report", "swapoff",
                   '[ -n "$P" ] && ps -p "$P"', "Never", "rig sysmon incidents --show", "omp-steer list",
                   "tg-ctl alert handoff --source sysmon --to"):
        assert needle in brief, needle
    heads = [line for line in brief.splitlines() if line.startswith("## ")]
    assert [h.split(".")[0] for h in heads] == [f"## {i}" for i in range(1, 10)]


def test_kickoff_starts_with_the_subscribe_command():
    text = handler.kickoff_text("sysmon CRITICAL on home: load")
    assert text.splitlines()[0].endswith("tg-ctl alert subscribe --exclusive --source sysmon --ttl 0")
    assert text.splitlines()[1] == "sysmon CRITICAL on home: load"


# ── I/O wrappers ──────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("answer", [(1, "boom"), (0, "not json"), (0, "[]"),
                                    (3, json.dumps({"state": "bound"})),
                                    (0, json.dumps({"state": "bound", "holder": None})),
                                    (0, json.dumps({"state": "suspended", "holder": {"sessionId": 7, "pid": 1}})),
                                    OSError(errno.EIO, "io"), FileNotFoundError(errno.ENOENT, "nope")])
def test_read_holder_unknown_on_errors(answer):
    run = _Fake({("tg-ctl", "alert", "holder"): answer})
    assert handler.read_holder(_cfg(), run).state == "unknown"


def test_read_holder_bound_and_vacant():
    doc = {"source": "sysmon", "state": "bound", "holder": {"sessionId": "s-1", "pid": 11}, "request": None}
    run = _Fake({("tg-ctl", "alert", "holder"): (0, json.dumps(doc))})
    assert handler.read_holder(_cfg(), run, [{"pid": 11, "sessionId": "s-1", "busy": True}]) == Holder("bound", "s-1", 11, True)
    vacant = {"source": "sysmon", "state": "vacant", "holder": None, "request": None}
    assert handler.read_holder(_cfg(), _Fake({("tg-ctl", "alert", "holder"): (3, json.dumps(vacant))})) == VACANT


def test_find_window_and_session_exists():
    run = _Fake({("tmux", "list-panes", "-t", "=main:=sysmon-handler"): (0, "@7 4242\n"), ("tmux", "has-session"): (1, "")})
    assert handler.find_window(_cfg(), run) == Window("@7", 4242)
    assert handler.session_exists(_cfg(), run) is False
    assert handler.find_window(_cfg(), _Fake({})) is None
    assert handler.find_window(_cfg(), _Fake({("tmux",): FileNotFoundError(errno.ENOENT, "tmux")})) is None


def test_spawn_records_the_window_or_a_failure(tmp_path):
    cfg = _cfg(tmp_path)
    run = _Fake({("tmux", "has-session"): (1, ""), ("tmux", "new-session"): (0, "@3 555\n")})
    rec, st = handler.spawn(cfg, {"last_spawn_ok": False}, T, "alert", run, severity=SEVERITY_CRITICAL, why="critical")
    # Review Focus 3: no such tmux session, so new-session creates it
    assert run.ran("tmux", "new-session") and not run.ran("tmux", "new-window")
    assert rec["outcome"] == "spawned" and (st["window_id"], st["pane_pid"]) == ("@3", 555)
    assert st["last_spawn_at"] == T and st["last_spawn_ok"] is None and st["spawn_severity"] == SEVERITY_CRITICAL
    assert (tmp_path / "state" / "handler" / "brief.md").read_text().startswith("# sysmon handler")
    rec, st = handler.spawn(cfg, {}, T, "alert", _Fake({}), severity=SEVERITY_WARNING, why="x")
    assert rec["outcome"] == "failed" and st["last_spawn_ok"] is False and "window_id" not in st


def test_spawn_dry_run_runs_nothing(tmp_path):
    rec, st = handler.spawn(_cfg(tmp_path, dry_run=True), {}, T, "a", lambda *a, **k: pytest.fail("ran"),
                            severity=SEVERITY_CRITICAL, why="x")
    assert rec["outcome"] == "would_spawn" and st == {}


# ── the holder cache and tick ─────────────────────────────────────────────────────────────
def _holder_doc(state="vacant", pid=None, session=None):
    return json.dumps({"source": "sysmon", "state": state, "request": None,
                       "holder": {"pid": pid, "sessionId": session} if pid else None})


def test_the_holder_is_read_once_per_census_interval_and_deliveries_update_it():
    cfg = _cfg()
    run = _Fake({("tg-ctl", "alert", "holder"): (3, _holder_doc())})
    h, st = handler.current_holder({}, T, cfg, run)
    h2, st = handler.current_holder(st, T + 59, cfg, run)
    assert h == h2 == VACANT and len(run.calls) == 1
    # amendment 5: a cached vacant slot, then a delivery that went through: no spawn this tick
    st = handler.observe_send(st, {"via": "holder", "outcome": "sent", "returncode": 0}, T + 60 - 1)
    assert handler.current_holder(st, T + 59, cfg, run)[0].state == "bound" and st["last_delivery_at"] == T + 59
    st = handler.observe_send(st, {"via": "holder", "outcome": "deferred", "returncode": 5}, T)
    assert st["holder"]["state"] == "suspended"
    st = handler.observe_send(st, {"via": "holder", "outcome": "undelivered", "returncode": 3}, T)
    assert st["holder"]["state"] == "vacant"


@pytest.mark.parametrize("cached", [{"state": "vacant", "evil": 1}, {"state": "nonsense", "session_id": None,
                                                                   "pid": None, "busy": None}, "x", None])
def test_a_malformed_holder_cache_is_a_miss(cached):
    run = _Fake({("tg-ctl", "alert", "holder"): (3, _holder_doc())})
    h, st = handler.current_holder({"holder": cached, "last_vacancy_check_at": T}, T + 1, _cfg(), run)
    assert h == VACANT and len(run.calls) == 1


def test_tick_cached_vacant_then_a_delivery_never_spawns(tmp_path):
    cfg = _cfg(tmp_path)
    run = _Fake({("tg-ctl", "alert", "holder"): (3, _holder_doc())})
    _, st = handler.current_holder({}, T, cfg, run)
    st = handler.observe_send(st, {"via": "holder", "outcome": "sent", "returncode": 0}, T)
    st, records, _ = handler.tick([_f()], st, T + 1, cfg, run, alert_text="a")
    assert records == [] and not run.ran("tmux")


def test_tick_registers_the_handler_and_notices_a_critical_failed_start_once(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(handler, "is_descendant", lambda child, anc: (child, anc) == (101, 100))
    run = _Fake({("tg-ctl", "alert", "holder"): (3, _holder_doc()), ("tmux", "list-panes"): (1, ""),
                 ("tmux", "has-session"): (0, ""), ("tmux", "new-window"): (0, "@5 100\n"),
                 ("tg-ctl", "omp-live", "status"): (0, LIVE_HANDLER)})
    st, records, notices = handler.tick([_f()], {}, T, cfg, run, alert_text="a")
    assert [r["outcome"] for r in records] == ["spawned"] and notices == []
    st, _, _ = handler.tick([_f()], st, T + MIN, cfg, run, alert_text="a")
    assert (st["handler_pid"], st["handler_session_id"], st["handler_busy"]) == (101, "s-h", False)
    st, records, notices = handler.tick([_f()], st, T + 5 * MIN, cfg, run, alert_text="a")
    assert st["last_spawn_ok"] is False and [n["title"] for n in notices] == ["sysmon handler did not start"]
    assert notices[0]["body"].endswith("\n\na") and notices[0]["severity"] == SEVERITY_CRITICAL
    _, _, notices = handler.tick([_f()], st, T + 6 * MIN, cfg, run, alert_text="a")
    assert notices == [] and len(run.ran("tmux", "new-window")) == 1


def test_the_startup_verdict_never_uses_a_reading_from_before_the_deadline(tmp_path):
    """A long census interval must not judge a handler that subscribed after the last cached read."""
    cfg = _cfg(tmp_path, startup_timeout_minutes=1)
    cfg["census_interval_seconds"] = 600
    run = _Fake({("tg-ctl", "alert", "holder"): (0, _holder_doc("bound", 101, "s-h")),
                 ("tg-ctl", "omp-live", "status"): (0, LIVE_HANDLER)})
    st = {"last_spawn_at": T, "last_spawn_ok": None, "window_id": "@5", "pane_pid": 100, "window_created_at": T,
          "holder": {"state": "vacant", "session_id": None, "pid": None, "busy": None}, "last_vacancy_check_at": T}
    st, records, notices = handler.tick([_f()], st, T + MIN, cfg, run, alert_text="a")
    assert st["last_spawn_ok"] is True and notices == [] and records[0]["holder"] == "bound"
    assert len(run.ran("tg-ctl", "alert", "holder")) == 1


def test_tick_a_warning_spawn_that_failed_to_start_posts_nothing(tmp_path):
    cfg = _cfg(tmp_path)
    run = _Fake({("tg-ctl", "alert", "holder"): (3, _holder_doc()), ("tmux", "list-panes"): (1, ""),
                 ("tmux", "has-session"): (0, ""), ("tmux", "new-window"): (0, "@5 100\n")})
    w = [_f("swap", SEVERITY_WARNING)]
    st = handler.update_first_seen(w, {}, T - 15 * MIN)
    st, records, _ = handler.tick(w, st, T, cfg, run, alert_text="a")
    assert records[0]["outcome"] == "spawned" and st["spawn_severity"] == SEVERITY_WARNING
    st, records, notices = handler.tick(w, st, T + 5 * MIN, cfg, run, alert_text="a")
    assert notices == [] and st["last_spawn_ok"] is False and records[0]["event"] == "startup"


def test_tick_never_raises_when_tmux_and_tg_ctl_are_missing(tmp_path):
    def run(argv, **kw):
        raise FileNotFoundError(errno.ENOENT, "missing", argv[0])

    st, records, notices = handler.tick([_f()], {}, T, _cfg(tmp_path), run, alert_text="a")
    assert st["holder"]["state"] == "unknown" and records == [] and notices == []


def test_tick_dry_run_runs_nothing_and_records_would_spawn(tmp_path):
    st, records, _ = handler.tick([_f()], {}, T, _cfg(tmp_path, dry_run=True),
                                  lambda *a, **k: pytest.fail("ran"), alert_text="a")
    assert [r["outcome"] for r in records] == ["would_spawn"]


def test_tick_post_crash_flag_spawns_and_clears(tmp_path):
    cfg = _cfg(tmp_path)
    run = _Fake({("tg-ctl", "alert", "holder"): (3, _holder_doc()), ("tmux", "list-panes"): (1, ""),
                 ("tmux", "has-session"): (0, ""), ("tmux", "new-window"): (0, "@5 100\n")})
    st, records, _ = handler.tick([], {"post_crash_pending": True}, T, cfg, run, alert_text="a")
    assert records[0]["why"] == "post-crash" and st["post_crash_pending"] is False
    assert st["spawn_severity"] == SEVERITY_CRITICAL
    bound = _Fake({("tg-ctl", "alert", "holder"): (0, _holder_doc("bound", 7, "s"))})
    st, records, _ = handler.tick([], {"post_crash_pending": True}, T, cfg, bound, alert_text="a")
    assert records == [] and st["post_crash_pending"] is False


def test_tick_honours_a_spawn_request_once(tmp_path):
    cfg = _cfg(tmp_path)
    req = handler.request_path(cfg)
    req.parent.mkdir(parents=True)
    req.write_text("")
    run = _Fake({("tg-ctl", "alert", "holder"): (0, _holder_doc("bound", 7, "s")), ("tmux", "list-panes"): (1, ""),
                 ("tmux", "has-session"): (0, ""), ("tmux", "new-window"): (0, "@5 100\n")})
    st, records, _ = handler.tick([], {"last_spawn_at": T - MIN}, T, cfg, run, alert_text="a")
    assert records[0]["outcome"] == "spawned" and not req.exists()  # ignores the trigger and cooldown
    req.write_text("")
    present = _Fake({("tg-ctl", "alert", "holder"): (0, _holder_doc("bound", 7, "s")),
                     ("tmux", "list-panes"): (0, "@5 100\n")})
    _, records, _ = handler.tick([], st, T + 1, cfg, present, alert_text="a")
    assert records[0]["outcome"] == "refused: window exists" and not req.exists()
    assert not present.ran("tmux", "new-window")
