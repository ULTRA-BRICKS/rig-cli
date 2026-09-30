"""sysmon Layer 3 (rig-cli#622): the heal ladder — notify → throttle → kill, fail-closed."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time

import pytest

from riglib.sysmon_daemon import config, deliver, heal, probe
from riglib.sysmon_daemon.model import AgentHost, Attribution, Finding, Offender

OMP = AgentHost(pid=900, kind="omp", args="omp", cwd="/xp/app", session_file="/s/a.jsonl", pane="tmux:main:1.0")
CLAUDE = AgentHost(pid=901, kind="claude-code", args="claude", cwd="/xp/other")


def _off(pid, *, host=None, orphan=False, protected=None, start=1000.0, comm="whisper-cli"):
    attribution = Attribution(pid=pid, chain=(pid, 1), host=host, orphan=orphan, protected_reason=protected)
    return Offender(pid=pid, comm=comm, args=f"{comm} -m big.bin", rss_kb=1_600_000, start_time=start,
                    attribution=attribution)


def _finding(severity, *offenders, key="gpu_models", healable=True):
    return Finding(key=key, kind="gpu", severity=severity, summary="13 GPU-model processes running, cap is 2",
                   metrics={"gpu_models": 13, "gpu_slots": 2}, offenders=tuple(offenders), healable=healable)


def _cfg(**heal_over):
    cfg = config.load(None)
    cfg["heal"].update(heal_over)
    return cfg


def _actions(steps):
    return [(s.action, s.pid) for s in steps]


def _run_ladder(ticks, cfg, state=None):
    """Feed ``[(now, findings), …]`` through ``plan``, every step succeeding (``record``);
    return the steps of every tick."""
    state = state or {}
    out = []
    for now, findings in ticks:
        steps, state = heal.plan(findings, state, cfg, now)
        state = heal.record(state, [_result(s, "done") for s in steps])
        out.append(steps)
    return out, state


def _result(step, outcome):
    return {"action": step.action, "pid": step.pid, "start_time": step.start_time,
            "finding_key": step.finding_key, "outcome": outcome, "detail": "", "at": 0.0}


# ── plan: who is touched ──────────────────────────────────────────────────────────────────
def test_protected_offender_never_gets_a_step_even_after_grace_and_its_reason_is_kept():
    cfg = _cfg(kill_owned=True, grace_seconds=60)
    shell = _off(10, host=OMP, orphan=True, protected="user-interactive (foreground in a tty)")
    ticks = [(0.0, [_finding("warning", shell)]), (10.0, [_finding("critical", shell)]),
             (500.0, [_finding("critical", shell)])]
    steps, state = _run_ladder(ticks, cfg)
    assert steps == [[], [], []]
    assert "user-interactive (foreground in a tty)" in json.dumps(state)


def test_disabled_or_non_healable_findings_produce_no_steps():
    orphan = _off(10, orphan=True)
    steps, _ = heal.plan([_finding("critical", orphan)], {}, _cfg(enabled=False), 0.0)
    assert steps == []
    steps, _ = heal.plan([_finding("critical", orphan, healable=False)], {}, _cfg(), 0.0)
    assert steps == []


def test_warning_notifies_each_owning_agent_once_and_does_nothing_else():
    cfg = _cfg()
    f = _finding("warning", _off(10, host=OMP), _off(11, host=OMP), _off(12, host=CLAUDE), _off(13, orphan=True))
    (first, second), _ = _run_ladder([(0.0, [f]), (10.0, [f])], cfg)
    assert sorted(_actions(first)) == [("notify", OMP.pid), ("notify", CLAUDE.pid)]
    assert second == []


def test_notify_message_names_only_that_agents_own_offenders():
    f = _finding("warning", _off(10, host=OMP), _off(12, host=CLAUDE))
    steps, _ = heal.plan([f], {}, _cfg(), 0.0)
    to_omp = next(s for s in steps if s.host == OMP)
    assert "pid 10" in to_omp.reason
    assert "pid 12" not in to_omp.reason


def test_notify_agent_off_sends_no_notify():
    steps, _ = heal.plan([_finding("critical", _off(10, host=OMP))], {}, _cfg(notify_agent=False), 0.0)
    assert [s.action for s in steps] == ["throttle"]


# ── plan: the critical ladder ─────────────────────────────────────────────────────────────
def test_critical_throttles_once_then_kills_only_orphans_after_grace():
    cfg = _cfg(grace_seconds=120, kill_orphans=True)
    owned, orphan, stray = _off(10, host=OMP), _off(20, orphan=True), _off(30)
    f = _finding("critical", owned, orphan, stray)
    steps, _ = _run_ladder([(0.0, [f]), (60.0, [f]), (119.0, [f]), (120.0, [f]), (200.0, [f])], cfg)
    assert sorted(_actions(steps[0])) == [("notify", OMP.pid), ("throttle", 10), ("throttle", 20), ("throttle", 30)]
    assert steps[1] == [] and steps[2] == []
    assert _actions(steps[3]) == [("kill", 20)]
    assert steps[4] == []  # a kill is never repeated


def test_kill_owned_is_opt_in():
    f = _finding("critical", _off(10, host=OMP))
    steps, _ = _run_ladder([(0.0, [f]), (120.0, [f])], _cfg(grace_seconds=120, kill_owned=True))
    assert _actions(steps[1]) == [("kill", 10)]


def test_without_throttle_the_kill_clock_starts_at_the_first_critical_sighting():
    f = _finding("critical", _off(20, orphan=True))
    steps, _ = _run_ladder([(0.0, [f]), (59.0, [f]), (60.0, [f])],
                           _cfg(throttle=False, grace_seconds=60, kill_orphans=True))
    assert steps[0] == [] and steps[1] == []
    assert _actions(steps[2]) == [("kill", 20)]


def test_a_dip_to_warning_restarts_the_kill_clock_without_rethrottling():
    orphan = _off(20, orphan=True)
    ticks = [(0.0, [_finding("critical", orphan)]), (50.0, [_finding("warning", orphan)]),
             (100.0, [_finding("critical", orphan)]), (150.0, [_finding("critical", orphan)]),
             (160.0, [_finding("critical", orphan)])]
    steps, _ = _run_ladder(ticks, _cfg(grace_seconds=60, kill_orphans=True))
    assert _actions(steps[0]) == [("throttle", 20)]
    assert steps[1] == [] and steps[2] == [] and steps[3] == []
    assert _actions(steps[4]) == [("kill", 20)]


def test_offender_without_start_time_is_never_signalled():
    f = _finding("critical", _off(20, orphan=True, start=None))
    steps, _ = _run_ladder([(0.0, [f]), (500.0, [f])], _cfg(grace_seconds=60))
    assert steps == [[], []]


def test_shipped_defaults_never_kill_even_a_proven_orphan():
    """Every kill path is opt-in: out of the box the ladder notifies and throttles, nothing more."""
    f = _finding("critical", _off(10, host=OMP), _off(20, orphan=True), _off(30))
    steps, _ = _run_ladder([(0.0, [f]), (10_000.0, [f])], _cfg())
    assert not [s for tick in steps for s in tick if s.action == "kill"]
    assert config.DEFAULTS["heal"]["kill_orphans"] is False and config.DEFAULTS["heal"]["kill_owned"] is False


# ── plan: state lifecycle ─────────────────────────────────────────────────────────────────
def test_a_vanished_finding_is_forgotten_so_the_ladder_restarts_on_recurrence():
    f = _finding("critical", _off(10, host=OMP))
    steps, state = _run_ladder([(0.0, [f]), (10.0, []), (20.0, [f])], _cfg())
    assert sorted(_actions(steps[2])) == [("notify", OMP.pid), ("throttle", 10)]
    _, empty = heal.plan([], state, _cfg(), 30.0)
    assert empty["findings"] == {}


def test_a_vanished_offender_is_dropped_and_a_reused_pid_is_a_new_offender():
    cfg = _cfg(grace_seconds=60, kill_orphans=True)
    first = _off(20, orphan=True, start=1000.0)
    other = _off(21, orphan=True, start=3000.0)
    reborn = _off(20, orphan=True, start=5000.0)
    ticks = [(0.0, [_finding("critical", first, other)]), (30.0, [_finding("critical", other)]),
             (40.0, [_finding("critical", reborn, other)]), (60.0, [_finding("critical", reborn, other)])]
    steps, state = _run_ladder(ticks, cfg)
    assert _actions(steps[2]) == [("throttle", 20)]  # not "already throttled": a different process
    assert _actions(steps[3]) == [("kill", 21)]  # the reborn pid 20 is still inside its own grace
    assert "1000" not in json.dumps(state)


def test_plan_is_pure_and_its_state_is_json():
    state = {}
    snapshot = json.dumps(state)
    _, new = heal.plan([_finding("critical", _off(20, orphan=True))], state, _cfg(), 0.0)
    assert json.dumps(state) == snapshot
    assert json.loads(json.dumps(new)) == new


def test_toggling_dry_run_restarts_the_ladder():
    """A dry-run throttle touched nothing, so a live ladder must not treat it as done."""
    f = _finding("critical", _off(20, orphan=True))
    _, state = heal.plan([f], {}, _cfg(dry_run=True), 0.0)
    steps, _ = heal.plan([f], state, _cfg(dry_run=False), 10.0)
    assert _actions(steps) == [("throttle", 20)]


# ── throttle restore ──────────────────────────────────────────────────────────────────────
def _tick(findings, state, cfg, now, fake):
    """One daemon tick: plan, execute against the fake process table, fold the outcomes back."""
    steps, state = heal.plan(findings, state, cfg, now)
    results = heal.execute(steps, cfg, fake.ops())
    return [(r["action"], r["pid"], r["outcome"]) for r in results], heal.record(state, results)


def test_a_throttle_is_undone_once_the_finding_clears_and_survives_a_dip_to_warning():
    fake, cfg = FakeOps({20: 1000.0}), _cfg(notify_agent=False)
    done, state = _tick([_finding("critical", _off(20))], {}, cfg, 0.0, fake)
    assert done == [("throttle", 20, "done")] and fake.bg == {20}
    done, state = _tick([_finding("warning", _off(20))], state, cfg, 10.0, fake)
    assert done == [] and fake.bg == {20}  # still under pressure: keep it throttled
    done, state = _tick([], state, cfg, 20.0, fake)
    assert done == [("restore", 20, "done")] and fake.bg == set()
    done, state = _tick([], state, cfg, 30.0, fake)
    assert done == [] and fake.calls == [("background", 20), ("foreground", 20)]


def test_a_throttle_is_undone_when_its_process_leaves_a_persisting_finding():
    fake, cfg = FakeOps({20: 1000.0, 21: 1000.0}), _cfg()
    _, state = _tick([_finding("critical", _off(20), _off(21))], {}, cfg, 0.0, fake)
    done, state = _tick([_finding("critical", _off(21))], state, cfg, 10.0, fake)
    assert done == [("restore", 20, "done")] and fake.bg == {21}


def test_restore_never_touches_a_process_that_reused_the_pid():
    fake, cfg = FakeOps({20: 1000.0}), _cfg()
    _, state = _tick([_finding("critical", _off(20))], {}, cfg, 0.0, fake)
    fake.alive[20] = 5000.0  # the throttled process exited; an unrelated one got pid 20
    fake.bg.discard(20)
    done, state = _tick([], state, cfg, 10.0, fake)
    assert done == [("restore", 20, "skipped")]
    assert ("foreground", 20) not in fake.calls


def test_a_pid_two_findings_throttled_stays_throttled_until_both_clear():
    fake, cfg = FakeOps({20: 1000.0}), _cfg()
    gpu, storm = _finding("critical", _off(20)), _finding("critical", _off(20), key="storm:whisper-cli")
    done, state = _tick([gpu, storm], {}, cfg, 0.0, fake)
    assert done == [("throttle", 20, "done")]
    done, state = _tick([storm], state, cfg, 10.0, fake)
    assert done == [] and fake.bg == {20}
    done, state = _tick([], state, cfg, 20.0, fake)
    assert done == [("restore", 20, "done")] and fake.bg == set()


def test_a_process_that_was_already_background_is_left_as_it_was():
    """Clearing the background band on restore would RAISE a process someone deliberately
    started with ``taskpolicy -b``; the ladder only undoes what it changed."""
    fake, cfg = FakeOps({20: 1000.0}, bg={20}), _cfg()
    done, state = _tick([_finding("critical", _off(20))], {}, cfg, 0.0, fake)
    assert done == [("throttle", 20, "skipped")]
    done, _ = _tick([], state, cfg, 10.0, fake)
    assert done == [] and fake.bg == {20} and fake.calls == []


def test_a_failed_throttle_is_never_restored():
    fake, cfg = FakeOps({20: 1000.0}), _cfg()
    ops = fake.ops()
    ops.background = lambda pid: "background: Operation not permitted"
    steps, state = heal.plan([_finding("critical", _off(20))], {}, cfg, 0.0)
    results = heal.execute(steps, cfg, ops)
    assert [(r["outcome"], r["detail"]) for r in results if r["action"] == "throttle"] == [
        ("failed", "background: Operation not permitted")]
    done, _ = _tick([], heal.record(state, results), cfg, 10.0, fake)
    assert done == []


def test_a_throttle_that_failed_is_retried_then_the_ladder_moves_on():
    """A transient refusal must not leave the offender unthrottled for the whole incident; a
    permanent one stops after the budget, and the kill clock still runs."""
    fake, cfg = FakeOps({20: 1000.0}), _cfg(notify_agent=False)
    ops = fake.ops()
    replies = iter(["background: Resource temporarily unavailable"])
    ops.background = lambda pid: next(replies, None) or fake.bg.add(pid)
    f, state, outcomes = _finding("critical", _off(20)), {}, []
    for now in (0.0, 10.0, 20.0):
        steps, state = heal.plan([f], state, cfg, now)
        results = heal.execute(steps, cfg, ops)
        state = heal.record(state, results)
        outcomes.append([(r["action"], r["outcome"]) for r in results])
    assert outcomes == [[("throttle", "failed")], [("throttle", "done")], []] and fake.bg == {20}

    fake, cfg = FakeOps({20: 1000.0}), _cfg(notify_agent=False, kill_orphans=True, grace_seconds=60)
    ops = fake.ops()
    ops.background = lambda pid: "background: Operation not permitted"
    f, state, actions = _finding("critical", _off(20, orphan=True)), {}, []
    for now in (0.0, 10.0, 20.0, 30.0, 70.0):
        steps, state = heal.plan([f], state, cfg, now)
        state = heal.record(state, heal.execute(steps, cfg, ops))
        actions.append([s.action for s in steps])
    assert actions == [["throttle"]] * heal.THROTTLE_ATTEMPTS + [[], ["kill"]]


def test_a_killed_process_is_not_restored():
    fake, cfg = FakeOps({20: 1000.0}), _cfg(grace_seconds=60, kill_orphans=True)
    f = _finding("critical", _off(20, orphan=True))
    _, state = _tick([f], {}, cfg, 0.0, fake)
    done, state = _tick([f], state, cfg, 60.0, fake)
    assert done == [("kill", 20, "done")]
    done, _ = _tick([], state, cfg, 70.0, fake)
    assert done == []


def test_disabling_heal_undoes_every_throttle():
    fake = FakeOps({20: 1000.0})
    _, state = _tick([_finding("critical", _off(20))], {}, _cfg(), 0.0, fake)
    done, state = _tick([_finding("critical", _off(20))], state, _cfg(enabled=False), 10.0, fake)
    assert done == [("restore", 20, "done")] and fake.bg == set()


def test_a_real_throttle_is_undone_for_real_after_switching_to_dry_run():
    fake = FakeOps({20: 1000.0})
    _, state = _tick([_finding("critical", _off(20))], {}, _cfg(), 0.0, fake)
    dry = _cfg(dry_run=True)
    done, state = _tick([_finding("critical", _off(20))], state, dry, 10.0, fake)
    assert done == []  # the real throttle still stands; dry-run must not re-plan it as new
    done, _ = _tick([], state, dry, 20.0, fake)
    assert done == [("restore", 20, "done")] and fake.bg == set()


def test_a_dry_run_throttle_is_only_ever_undone_in_dry_run():
    fake, dry = FakeOps({20: 1000.0}), _cfg(dry_run=True)
    done, state = _tick([_finding("critical", _off(20))], {}, dry, 0.0, fake)
    assert done == [("throttle", 20, "dry_run")]
    done, _ = _tick([], state, dry, 10.0, fake)
    assert done == [("restore", 20, "dry_run")] and fake.calls == []


def test_release_undoes_every_throttle_and_the_next_start_rethrottles():
    """The daemon releases its throttles when it stops, so an uninstalled or disabled monitor
    never leaves processes in the background band."""
    fake, cfg = FakeOps({20: 1000.0}), _cfg(notify_agent=False)
    f = _finding("critical", _off(20))
    _, state = _tick([f], {}, cfg, 0.0, fake)
    steps, state = heal.release(state)
    results = heal.execute(steps, cfg, fake.ops())
    assert [(r["action"], r["outcome"]) for r in results] == [("restore", "done")] and fake.bg == set()
    done, _ = _tick([f], heal.record(state, results), cfg, 10.0, fake)
    assert done == [("throttle", 20, "done")]


def test_a_planned_kill_that_did_not_happen_keeps_the_throttle_to_restore():
    """Only a kill that really killed drops the throttle record: a failed kill leaves the process
    alive in the background band, so it must still be restored."""
    fake, cfg = FakeOps({20: 1000.0}), _cfg(grace_seconds=60, kill_orphans=True)
    f = _finding("critical", _off(20, orphan=True))
    _, state = _tick([f], {}, cfg, 0.0, fake)
    ops = fake.ops()

    def refuse(pid, sig):
        raise PermissionError(1, "Operation not permitted")

    ops.signal = refuse
    steps, state = heal.plan([f], state, cfg, 60.0)
    results = heal.execute(steps, cfg, ops)
    assert [(r["action"], r["outcome"]) for r in results] == [("kill", "failed")]
    done, _ = _tick([], heal.record(state, results), cfg, 70.0, fake)
    assert done == [("restore", 20, "done")] and fake.bg == set()


def test_a_dry_run_kill_keeps_a_real_throttle_to_restore():
    fake = FakeOps({20: 1000.0})
    live = _cfg(grace_seconds=60, kill_orphans=True)
    dry = _cfg(grace_seconds=60, kill_orphans=True, dry_run=True)
    f = _finding("critical", _off(20, orphan=True))
    _, state = _tick([f], {}, live, 0.0, fake)
    _, state = _tick([f], state, dry, 10.0, fake)
    done, state = _tick([f], state, dry, 70.0, fake)
    assert done == [("kill", 20, "dry_run")]
    done, _ = _tick([], state, dry, 80.0, fake)
    assert done == [("restore", 20, "done")] and fake.bg == set() and 20 in fake.alive


def test_a_restore_that_keeps_failing_is_parked_never_forgotten():
    """Retried on the next ticks, then parked: a re-blame must know the band is sysmon's own
    (never "already background, left as it was"), and the next release retries it."""
    fake, cfg = FakeOps({20: 1000.0}), _cfg(notify_agent=False)
    f = _finding("critical", _off(20))
    _, state = _tick([f], {}, cfg, 0.0, fake)
    ops = fake.ops()
    ops.foreground = lambda pid: "foreground: Resource temporarily unavailable"
    outcomes = []
    for now in (10.0, 20.0, 30.0, 40.0):
        steps, state = heal.plan([], state, cfg, now)
        results = heal.execute(steps, cfg, ops)
        state = heal.record(state, results)
        outcomes.append([(r["action"], r["outcome"]) for r in results])
    assert outcomes == [[("restore", "failed")]] * heal.RESTORE_ATTEMPTS + [[]] * (4 - heal.RESTORE_ATTEMPTS)
    done, state = _tick([f], state, cfg, 50.0, fake)
    assert done == [] and fake.bg == {20}  # blamed again: still sysmon's throttle, not re-planned
    done, state = _tick([], state, cfg, 60.0, fake)
    assert done == [("restore", 20, "done")] and fake.bg == set()


def test_a_parked_restore_is_retried_when_the_monitor_stops():
    fake, cfg = FakeOps({20: 1000.0}), _cfg(notify_agent=False)
    _, state = _tick([_finding("critical", _off(20))], {}, cfg, 0.0, fake)
    ops = fake.ops()
    ops.foreground = lambda pid: "foreground: Resource temporarily unavailable"
    for now in range(1, heal.RESTORE_ATTEMPTS + 1):
        steps, state = heal.plan([], state, cfg, float(now))
        state = heal.record(state, heal.execute(steps, cfg, ops))
    steps, state = heal.release(state)
    heal.execute(steps, cfg, fake.ops())
    assert fake.bg == set()


def test_a_monitor_stopped_mid_batch_still_restores_what_it_already_throttled():
    """The monitor folds every outcome as it happens (``on_result``); a stop signal between two
    throttles must not strand the first one in the background band."""
    fake, cfg = FakeOps({20: 1000.0, 21: 1000.0}), _cfg(notify_agent=False)
    steps, planned = heal.plan([_finding("critical", _off(20), _off(21))], {}, cfg, 0.0)
    memory = {"heal": planned}

    class Stop(Exception):
        pass

    def fold(result):
        memory["heal"] = heal.record(memory["heal"], [result])
        raise Stop  # the stop signal lands right after the first throttle

    with pytest.raises(Stop):
        heal.execute(steps, cfg, fake.ops(), on_result=fold)
    assert fake.bg == {20}
    steps, _ = heal.release(memory["heal"])
    heal.execute(steps, cfg, fake.ops())
    assert fake.bg == set()


def test_throttles_of_a_state_written_before_the_record_existed_are_restored():
    """An upgrade must not strand what the previous version put in the background band."""
    fake, cfg = FakeOps({20: 1000.0}, bg={20}), _cfg()
    legacy = {"dry_run": False, "findings": {"gpu_models": {"notified": [], "protected": {}, "offenders": {
        "20": {"start_time": 1000.0, "throttled": True, "critical_since": 0.0, "killed": False}}}}}
    done, _ = _tick([], legacy, cfg, 10.0, fake)
    assert done == [("restore", 20, "done")] and fake.bg == set()


def test_a_note_is_retried_until_delivered_then_never_repeated():
    fake, cfg = FakeOps({OMP.pid: 1000.0}), _cfg()
    host = AgentHost(pid=OMP.pid, kind="omp", args="omp", cwd="/xp/app", start_time=1000.0)
    f = _finding("warning", _off(10, host=host))
    replies = iter(["undelivered: the input box holds text", "typed into tmux pane %7"])
    ops = fake.ops()
    ops.notify_agent = lambda h, text: next(replies)
    outcomes, state = [], {}
    for now in (0.0, 10.0, 20.0):
        steps, state = heal.plan([f], state, cfg, now)
        results = heal.execute(steps, cfg, ops)
        state = heal.record(state, results)
        outcomes.append([(r["action"], r["outcome"]) for r in results])
    assert outcomes == [[("notify", "failed")], [("notify", "done")], []]


def test_a_host_missing_from_one_census_keeps_its_notified_mark():
    """Attribution can flicker under load: a host that drops out for a tick and comes back must
    not be notified again."""
    fake, cfg = FakeOps({OMP.pid: 1000.0}), _cfg()
    host = AgentHost(pid=OMP.pid, kind="omp", args="omp", cwd="/xp/app", start_time=1000.0)
    owned, unattributed = _finding("warning", _off(10, host=host)), _finding("warning", _off(10))
    ops = fake.ops()
    ops.notify_agent = lambda h, text: "typed into tmux pane %7"
    state, notes = {}, 0
    for now, f in enumerate([owned, unattributed, owned, unattributed, owned]):
        steps, state = heal.plan([f], state, cfg, float(now))
        state = heal.record(state, heal.execute(steps, cfg, ops))
        notes += len(steps)
    assert notes == 1


def test_a_new_agent_on_a_reused_host_pid_gets_its_own_note():
    fake, cfg = FakeOps({OMP.pid: 1000.0}), _cfg()
    first = AgentHost(pid=OMP.pid, kind="omp", args="omp", cwd="/xp/app", start_time=1000.0)
    reborn = AgentHost(pid=OMP.pid, kind="omp", args="omp", cwd="/xp/other", start_time=7000.0)
    ops = fake.ops()
    ops.notify_agent = lambda h, text: "typed into tmux pane %7"
    state, notes = {}, []
    for now, host in enumerate([first, first, reborn, reborn]):
        fake.alive[OMP.pid] = host.start_time
        steps, state = heal.plan([_finding("warning", _off(10, host=host))], state, cfg, float(now))
        state = heal.record(state, heal.execute(steps, cfg, ops))
        notes.append([(s.pid, s.start_time) for s in steps])
    assert notes == [[(OMP.pid, 1000.0)], [], [(OMP.pid, 7000.0)], []]


def test_notes_that_never_get_through_stop_after_the_attempt_budget():
    fake, cfg = FakeOps({OMP.pid: 1000.0}), _cfg()
    host = AgentHost(pid=OMP.pid, kind="omp", args="omp", cwd="/xp/app", start_time=1000.0)
    f = _finding("warning", _off(10, host=host))
    state, attempts = {}, 0
    for now in range(10):
        steps, state = heal.plan([f], state, cfg, float(now))
        state = heal.record(state, heal.execute(steps, cfg, fake.ops()))
        attempts += len(steps)
    assert attempts == heal.NOTIFY_ATTEMPTS


def test_a_host_whose_start_time_is_unknown_is_never_messaged():
    """Without the start time a reused host pid cannot be ruled out: its pane may be anyone's."""
    fake = FakeOps({OMP.pid: 1000.0})
    (result,) = heal.execute([_step("notify", OMP.pid, None, OMP)], _cfg(), fake.ops())
    assert result["outcome"] == "skipped" and fake.calls == []


# ── execute ───────────────────────────────────────────────────────────────────────────────
class FakeOps:
    """A process table the test controls: ``alive`` maps pid → start_time, ``bg`` holds the
    pids currently in the OS background band."""

    def __init__(self, alive, *, dies_on=signal.SIGTERM, bg=()):
        self.alive = dict(alive)
        self.dies_on = dies_on
        self.bg = set(bg)
        self.calls = []
        self.slept = 0.0

    def ops(self):
        return heal.HealOps(start_time=self.start_time, is_background=self.is_background,
                            background=self.background, foreground=self.foreground,
                            signal=self.signal, sleep=self.sleep, notify_agent=self.notify)

    def start_time(self, pid):
        return self.alive.get(pid)

    def is_background(self, pid):
        return pid in self.bg

    def background(self, pid):
        self.calls.append(("background", pid))
        self.bg.add(pid)

    def foreground(self, pid):
        self.calls.append(("foreground", pid))
        self.bg.discard(pid)

    def signal(self, pid, sig):
        self.calls.append(("signal", pid, sig))
        if sig in (self.dies_on, signal.SIGKILL):
            self.alive.pop(pid, None)

    def sleep(self, seconds):
        self.slept += seconds

    def notify(self, host, text):
        self.calls.append(("notify", host.pid))
        return "undelivered: nobody listening"


def _step(action, pid, start=1000.0, host=None):
    return heal.HealStep(action=action, finding_key="gpu_models", pid=pid, start_time=start, reason="r", host=host)


@pytest.mark.parametrize("live_start, step_start", [(2000.0, 1000.0), (None, 1000.0), (1000.0, None)])
def test_reused_gone_or_unverifiable_pid_is_skipped_untouched(live_start, step_start):
    fake = FakeOps({20: live_start} if live_start is not None else {})
    steps = [_step("throttle", 20, step_start), _step("restore", 20, step_start), _step("kill", 20, step_start)]
    results = heal.execute(steps, _cfg(), fake.ops())
    assert [(r["outcome"], r["detail"]) for r in results] == [("skipped", "pid gone or reused")] * 3
    assert fake.calls == []


def test_start_time_within_one_second_is_the_same_process():
    fake = FakeOps({20: 1000.6})
    (result,) = heal.execute([_step("throttle", 20)], _cfg(), fake.ops())
    assert result["outcome"] == "done"
    assert fake.calls == [("background", 20)]


def test_kill_stops_at_sigterm_when_the_process_exits():
    fake = FakeOps({20: 1000.0})
    (result,) = heal.execute([_step("kill", 20)], _cfg(), fake.ops())
    assert result["outcome"] == "done"
    assert fake.calls == [("signal", 20, signal.SIGTERM)]


def test_kill_escalates_to_sigkill_after_five_seconds():
    fake = FakeOps({20: 1000.0}, dies_on=None)
    (result,) = heal.execute([_step("kill", 20)], _cfg(), fake.ops())
    assert result["outcome"] == "done"
    assert fake.calls == [("signal", 20, signal.SIGTERM), ("signal", 20, signal.SIGKILL)]
    assert fake.slept == pytest.approx(5.0)


def test_a_storm_of_kills_shares_one_grace_period():
    """SIGTERM every target first, then one shared wait: a 3-pid storm that ignores SIGTERM costs
    5 s of the daemon's loop, not 15."""
    fake = FakeOps({20: 1000.0, 21: 1000.0, 22: 1000.0}, dies_on=None)
    results = heal.execute([_step("kill", pid) for pid in (20, 21, 22)], _cfg(), fake.ops())
    assert fake.slept == pytest.approx(5.0)
    assert [c for c in fake.calls if c[2] == signal.SIGTERM] == [("signal", p, signal.SIGTERM) for p in (20, 21, 22)]
    assert [r["detail"] for r in results] == ["SIGKILL after 5s"] * 3


@pytest.mark.skipif(not probe.IS_DARWIN, reason="PRIO_DARWIN_BG is macOS-only")
def test_default_throttle_is_reversible_and_never_forks_or_renices():
    """An unprivileged process can never LOWER a nice value again (EACCES), so the throttle uses
    only the background band, which the same user can clear."""
    child = subprocess.Popen(["sleep", "30"], stdin=subprocess.DEVNULL)
    try:
        nice = os.getpriority(os.PRIO_PROCESS, child.pid)
        ops = heal.default_ops(_cfg(), run=lambda *a, **k: pytest.fail("throttle must not fork"))
        assert ops.is_background(child.pid) is False
        assert ops.background(child.pid) is None and ops.is_background(child.pid) is True
        assert ops.foreground(child.pid) is None and ops.is_background(child.pid) is False
        assert os.getpriority(os.PRIO_PROCESS, child.pid) == nice
    finally:
        child.terminate()
        child.wait()


def test_dry_run_executes_nothing_and_says_what_it_would_do():
    fake = FakeOps({20: 1000.0})
    steps = [_step("notify", OMP.pid, None, OMP), _step("throttle", 20), _step("restore", 20), _step("kill", 20)]
    results = heal.execute(steps, _cfg(dry_run=True), fake.ops())
    assert [r["outcome"] for r in results] == ["dry_run"] * 4
    assert fake.calls == []


def test_undelivered_notify_is_a_failed_result_not_an_exception():
    fake = FakeOps({OMP.pid: 1000.0})
    (result,) = heal.execute([_step("notify", OMP.pid, 1000.0, OMP)], _cfg(), fake.ops())
    assert result["outcome"] == "failed" and result["detail"].startswith("undelivered")


@pytest.mark.skipif(not (probe.IS_DARWIN or probe.IS_LINUX), reason="needs libproc or /proc")
def test_default_ops_kill_a_real_child_verified_by_its_start_time():
    child = subprocess.Popen(["sleep", "30"], stdin=subprocess.DEVNULL)
    # Reap concurrently, as a real offender's parent would: an unreaped zombie still has a start time.
    reaper = threading.Thread(target=child.wait)
    reaper.start()
    try:
        deadline = time.time() + 5
        start = probe.proc_start_time(child.pid)
        while start is None and time.time() < deadline:
            time.sleep(0.05)
            start = probe.proc_start_time(child.pid)
        (result,) = heal.execute([_step("kill", child.pid, start)], _cfg(), None)
        reaper.join(timeout=5)
        assert result["outcome"] == "done"
        assert child.returncode == -signal.SIGTERM
    finally:
        if child.returncode is None:
            child.kill()
        reaper.join()


# ── notify_agent ──────────────────────────────────────────────────────────────────────────
class FakeTerminal:
    """One agent TUI, reachable as tmux pane %7 (pane pid 800, the agent host 900 below it) and
    as a cmux surface: its input box sits between two rules above a status line, ``send-keys -l``
    / ``cmux send`` type into it, Enter submits it."""

    RULE = "─" * 50

    def __init__(self, box="", *, human_types="", human_first="", copy_mode=False, screen=None, lag=0,
                 keeps=None, shows_last=None):
        self.box = box
        self.human_types = human_types
        self.human_first = human_first  # keystrokes that land just before the typed note
        self.copy_mode = copy_mode
        self.lag = lag  # reads after typing that show only half of it (the TUI still rendering)
        self.keeps = keeps  # the box holds at most this many characters (a truncating TUI)
        self.shows_last = shows_last  # the box scrolls: only its last N characters are visible
        self.fixed_screen = screen
        self.submitted = []

    def screen(self):
        if self.fixed_screen is not None:
            return self.fixed_screen
        shown = self.box
        if self.lag and shown:
            self.lag -= 1
            shown = shown[:len(shown) // 2]
        if self.shows_last:
            shown = shown[-self.shows_last:]
        return "\n".join(["● Bash(bun test)", "  ⎿  12 passed", "", self.RULE, f"❯\xa0{shown}", self.RULE,
                          "  ⏵⏵ bypass permissions on", "", ""])

    def __call__(self, cmd):
        ok = subprocess.CompletedProcess(cmd, 0, "", "")
        verb = cmd[1]
        if verb == "list-panes":
            return subprocess.CompletedProcess(cmd, 0, f"100\t%1\t0\n800\t%7\t{int(self.copy_mode)}\n", "")
        if verb in ("capture-pane", "read-screen"):
            return subprocess.CompletedProcess(cmd, 0, self.screen(), "")
        if (verb == "send-keys" and "-l" in cmd) or verb == "send":
            self.box = (self.box + self.human_first + cmd[-1] + self.human_types)[:self.keeps]
            return ok
        if (verb == "send-keys" and cmd[-1] == "Enter") or (verb == "send-key" and cmd[-1] == "enter"):
            self.submitted.append(self.box)
            self.box = ""
            return ok
        raise AssertionError(cmd)


class FakeTgCtl:
    def __init__(self, live_pids=(), inbox_ok=True, send_ok=True, raises=None, terminal=None):
        self.live_pids = live_pids
        self.inbox_ok = inbox_ok
        self.send_ok = send_ok
        self.raises = raises
        self.terminal = terminal
        self.calls = []

    def __call__(self, cmd, **kwargs):
        assert kwargs.get("stdin") is subprocess.DEVNULL and kwargs.get("timeout")
        self.calls.append(list(cmd))
        if self.raises is not None:
            raise self.raises
        if cmd[0] in ("tmux", "cmux"):
            if self.terminal is None:
                return subprocess.CompletedProcess(cmd, 1, "", "no server running")
            return self.terminal(cmd)
        if cmd[1:3] == ["omp-live", "status"]:
            return subprocess.CompletedProcess(cmd, 0, json.dumps([{"pid": p, "cwd": "/x"} for p in self.live_pids]), "")
        if cmd[1:3] == ["omp-live", "send"]:
            if self.send_ok:
                return subprocess.CompletedProcess(cmd, 0, '{"ok":true,"status":"accepted"}', "")
            return subprocess.CompletedProcess(cmd, 1, "", '{"ok":false,"error":"delivery_failed"}')
        if cmd[1:3] == ["inbox", "queue"]:
            if self.inbox_ok:
                return subprocess.CompletedProcess(cmd, 0, "queued to app (pid 900)", "")
            return subprocess.CompletedProcess(cmd, 1, "", "no agent outside tmux matches '/xp/app'")
        raise AssertionError(cmd)


@pytest.fixture
def omp_in_pane_800(monkeypatch):
    """The omp host 900 runs under the pane process 800."""
    monkeypatch.setattr(probe, "proc_ppid", lambda pid: {900: 800}.get(pid))


NOTE = "[rig sysmon] CRITICAL gpu: 13 GPU-model processes\nYour processes behind it:\n- pid 10 whisper-cli"
NOTE_LINE = "[rig sysmon] CRITICAL gpu: 13 GPU-model processes Your processes behind it: - pid 10 whisper-cli"


def test_agent_in_a_tmux_pane_gets_the_note_typed_into_its_empty_input_box(omp_in_pane_800):
    """Incident 20260929T180907-live: omp-live was not loaded and tg-ctl's inbox refuses tmux
    agents, so both notifications failed."""
    term = FakeTerminal()
    fake = FakeTgCtl(live_pids=(), inbox_ok=False, terminal=term)
    out = heal.notify_agent(OMP, NOTE, tg_ctl_bin="tg-ctl", run=fake, sleep=lambda s: None)
    assert out == "typed into tmux pane %7"
    assert term.submitted == [NOTE_LINE]  # one line: a typed newline would submit half a note
    tmux = [c[1] for c in fake.calls if c[0] == "tmux"]
    assert tmux == ["list-panes", "capture-pane", "send-keys", "capture-pane", "send-keys"]
    assert not [c for c in fake.calls if c[1:3] == ["inbox", "queue"]]


def test_text_a_human_has_in_the_input_box_is_never_touched(omp_in_pane_800):
    term = FakeTerminal(box="can you also check the")
    fake = FakeTgCtl(inbox_ok=False, terminal=term)
    out = heal.notify_agent(OMP, NOTE, tg_ctl_bin="tg-ctl", run=fake, sleep=lambda s: None)
    assert out.startswith("undelivered: ") and "input box holds text" in out
    assert term.box == "can you also check the" and term.submitted == []
    assert not [c for c in fake.calls if c[:2] == ["tmux", "send-keys"]]


def test_keystrokes_a_human_adds_while_the_note_is_typed_are_never_submitted(omp_in_pane_800):
    term = FakeTerminal(human_types="y")
    fake = FakeTgCtl(inbox_ok=False, terminal=term)
    out = heal.notify_agent(OMP, NOTE, tg_ctl_bin="tg-ctl", run=fake, sleep=lambda s: None)
    assert out.startswith("undelivered: ") and "NOT submitted" in out
    assert term.submitted == []


def test_a_note_still_rendering_is_submitted_once_it_shows_whole(omp_in_pane_800):
    term = FakeTerminal(lag=1)
    fake = FakeTgCtl(inbox_ok=False, terminal=term)
    out = heal.notify_agent(OMP, NOTE, tg_ctl_bin="tg-ctl", run=fake, sleep=lambda s: None)
    assert out == "typed into tmux pane %7" and term.submitted == [NOTE_LINE]


def test_a_note_the_input_box_cut_short_is_never_submitted(omp_in_pane_800):
    term = FakeTerminal(keeps=20)
    fake = FakeTgCtl(inbox_ok=False, terminal=term)
    out = heal.notify_agent(OMP, NOTE, tg_ctl_bin="tg-ctl", run=fake, sleep=lambda s: None)
    assert out.startswith("undelivered: ") and "NOT submitted" in out
    assert term.submitted == []


def test_a_scrolled_box_showing_only_the_notes_tail_is_never_submitted(omp_in_pane_800):
    """The part a scrolled box hides may hold a human's keystrokes (typed just before the note
    landed): only a box showing the whole note is submitted."""
    term = FakeTerminal(human_first="y", shows_last=40)
    fake = FakeTgCtl(inbox_ok=False, terminal=term)
    out = heal.notify_agent(OMP, NOTE, tg_ctl_bin="tg-ctl", run=fake, sleep=lambda s: None)
    assert out.startswith("undelivered: ") and term.submitted == []


@pytest.mark.parametrize("term", [
    FakeTerminal(copy_mode=True),
    FakeTerminal(screen="~\n~\n\"notes.md\" 12L, 340B"),  # the pane shows vim, not the agent
    FakeTerminal(screen="\n".join([FakeTerminal.RULE, "❯ 1. Yes", "  2. No", FakeTerminal.RULE, "status"])),  # a menu
], ids=["copy-mode", "no-input-box", "menu-open"])
def test_anything_but_a_provably_empty_input_box_refuses(omp_in_pane_800, term):
    fake = FakeTgCtl(inbox_ok=False, terminal=term)
    out = heal.notify_agent(OMP, NOTE, tg_ctl_bin="tg-ctl", run=fake, sleep=lambda s: None)
    assert out.startswith("undelivered: ")
    assert not [c for c in fake.calls if c[:2] == ["tmux", "send-keys"]]


def test_agent_in_a_cmux_surface_gets_the_note_through_cmux_send():
    host = AgentHost(pid=903, kind="claude-code", args="claude", cwd="/xp/app", pane="cmux:WS-1/SF-2")
    term = FakeTerminal()
    fake = FakeTgCtl(inbox_ok=False, terminal=term)
    out = heal.notify_agent(host, NOTE, tg_ctl_bin="tg-ctl", run=fake, sleep=lambda s: None)
    assert out == "typed into cmux surface SF-2" and term.submitted == [NOTE_LINE]
    cmux = [c for c in fake.calls if c[0] == "cmux"]
    assert [c[1] for c in cmux] == ["read-screen", "send", "read-screen", "send-key"]
    assert all(c[2:6] == ["--workspace", "WS-1", "--surface", "SF-2"] for c in cmux)


def test_the_note_is_one_short_printable_line_without_escapes_cmux_would_expand():
    assert deliver.note_line("a\nb\\n\tc\x1b[0m") == "a b/n c [0m"
    long = deliver.note_line("word " * 1000)
    assert len(long) == deliver.MAX_NOTE_CHARS and long.endswith("…")


@pytest.mark.parametrize("screen, box", [
    ("out\n" + "─" * 60 + "\n" + " " * 60 + "\n" + "─" * 60 + "\n π · Opus 5.5 · ~/xp\n", ""),  # omp, empty
    ("out\n" + "─" * 60 + "\n❯\xa0\n" + "─" * 60 + "\n  ⏵⏵ bypass\n  hint\n", ""),  # claude-code, empty
    ("out\n" + "─" * 60 + "\n❯ fix the flaky\n  test please\n" + "─" * 60 + "\n  ⏵⏵ bypass\n", "fixtheflakytestplease"),
    ("╭" + "─" * 40 + "╮\n│ > hi │\n╰" + "─" * 40 + "╯\n", "hi"),  # boxed prompt
    ("─" * 60 + "\n\n" + "─" * 60 + "\n" + "\n".join(["log"] * 6), None),  # rules far above the bottom
    ("plain shell $ ", None),
])
def test_input_box_reading(screen, box):
    found = deliver.input_box(screen)
    assert (None if found is None else deliver.box_text(found)) == box


def test_live_omp_host_gets_the_message_through_omp_live_send():
    fake = FakeTgCtl(live_pids=(OMP.pid,))
    out = heal.notify_agent(OMP, "--stop now", tg_ctl_bin="tg-ctl", run=fake)
    send = fake.calls[-1]
    assert send[:4] == ["tg-ctl", "omp-live", "send", str(OMP.pid)]
    assert send[4] == "--request-id" and send[5].startswith("sysmon-")
    assert send[6:] == ["--", "--stop now"]  # a dash-leading text is never parsed as a flag
    assert "omp-live" in out and not out.startswith("undelivered")


def test_omp_host_not_live_falls_back_to_the_stop_hook_inbox():
    fake = FakeTgCtl(live_pids=(1,))
    out = heal.notify_agent(OMP, "stop", tg_ctl_bin="tg-ctl", run=fake)
    assert fake.calls[-1] == ["tg-ctl", "inbox", "queue", "/xp/app", "stop"]
    assert "inbox" in out and not out.startswith("undelivered")


def test_failed_live_send_falls_back_to_the_inbox():
    fake = FakeTgCtl(live_pids=(OMP.pid,), send_ok=False)
    heal.notify_agent(OMP, "stop", tg_ctl_bin="tg-ctl", run=fake)
    assert fake.calls[-1][1:3] == ["inbox", "queue"]


def test_non_omp_host_skips_omp_live():
    fake = FakeTgCtl()
    heal.notify_agent(CLAUDE, "stop", tg_ctl_bin="tg-ctl", run=fake)
    assert [c[1] for c in fake.calls] == ["inbox"]


@pytest.mark.parametrize("fake", [FakeTgCtl(inbox_ok=False), FakeTgCtl(raises=subprocess.TimeoutExpired("tg-ctl", 10)),
                                  FakeTgCtl(raises=FileNotFoundError("tg-ctl"))])
def test_every_channel_failing_is_undelivered_with_the_reason(fake):
    out = heal.notify_agent(OMP, "stop", tg_ctl_bin="tg-ctl", run=fake)
    assert out.startswith("undelivered: ")


def test_host_without_cwd_and_not_live_is_undelivered_without_queueing():
    fake = FakeTgCtl()
    host = AgentHost(pid=902, kind="codex", args="codex")
    assert heal.notify_agent(host, "stop", tg_ctl_bin="tg-ctl", run=fake).startswith("undelivered: ")
    assert fake.calls == []


# ── agent_message ─────────────────────────────────────────────────────────────────────────
def test_agent_message_carries_the_numbers_the_pids_and_what_happens_next():
    f = _finding("critical", _off(10, host=OMP))
    text = heal.agent_message(f, [f.offenders[0]], _cfg(grace_seconds=90, kill_orphans=True)["heal"])
    for needle in ("13 GPU-model processes", "gpu_models=13", "pid 10", "whisper-cli", "90", "/tmp/heavy-run.lock", "GPU slot"):
        assert needle in text
    # A command line is whatever the process chose: it never reaches an agent's prompt.
    assert "big.bin" not in text
