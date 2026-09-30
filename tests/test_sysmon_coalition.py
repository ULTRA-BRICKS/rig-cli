"""sysmon machine-wide fan-out (rig-cli#644): processes counted per launchd coalition and
same-name storms across every user, each as an absolute count AND a percent of the process-table
limit, attributed to the coalition's launchd label (the 2026-09-27 crash: 684 in one coalition)."""

from __future__ import annotations

import os
import subprocess

import pytest

from riglib.sysmon_daemon import config, daemon, detect, heal, probe
from riglib.sysmon_daemon.model import SEVERITY_CRITICAL, LaunchdJob, Offender, Proc

from tests.test_sysmon_daemon import _attributor, _census, _proc, _sample

ME = os.getuid()
TH = config.DEFAULT_THRESHOLDS
TGCTL = "ai.hyperide.tg-ctl"
CMUX = "application.com.cmuxterm.app.698704579.698704585"


def _coalitions(procs, coalition_of, labels, proc_max=6000, thresholds=TH):
    at = _attributor(procs)

    def offender(p: Proc) -> Offender:
        return Offender(p.pid, p.comm, p.args, p.rss_kb, 1.0, at.attribute(p.pid))

    return detect.coalition_findings(procs, coalition_of, labels, proc_max, thresholds, offender)


def _leak(n, comm="tg-ctl", first=5000):
    """``n`` processes of one coalition, the job's own daemon first (the 2026-09-27 tg-ctl leak)."""
    daemon_proc = _proc(first, 1, comm="tg-ctl", args="tg-ctl daemon", etime=90000.0)
    return [daemon_proc] + [_proc(first + 1 + i, first, comm=comm if i % 2 else "bash", etime=60.0)
                            for i in range(n - 1)]


# ── probe ─────────────────────────────────────────────────────────────────────────────────
def test_the_coalition_of_this_very_process_is_readable_and_shared_with_its_parent():
    if not probe.IS_DARWIN:
        pytest.skip("launchd coalitions exist on macOS only")
    mine = probe.proc_coalition(os.getpid())
    assert mine is not None and mine == probe.proc_coalition(os.getppid())
    assert probe.proc_coalition(2 ** 30) is None  # no such pid


@pytest.mark.parametrize("cgroup, unit", [
    ("0::/user.slice/user-1000.slice/user@1000.service/app.slice/tg-ctl.service\n", "tg-ctl.service"),
    ("0::/system.slice/docker-3f2a.scope\n", "docker-3f2a.scope"),
    ("12:pids:/\n0::/\n", None),
])
def test_linux_groups_by_the_systemd_unit_of_the_cgroup(cgroup, unit):
    assert probe.unit_from_cgroup(cgroup) == unit


def test_coalition_labels_come_from_the_job_pids_launchd_reports():
    jobs = {TGCTL: LaunchdJob(TGCTL, 5000, 0), CMUX: LaunchdJob(CMUX, 673, 0),
            "ai.hyperide.tmux-autosave": LaunchdJob("ai.hyperide.tmux-autosave", None, 0)}
    coalition_of = {5000: "901", 5001: "901", 673: "715", 700: "715"}
    assert detect.coalition_labels(jobs, coalition_of) == {"901": TGCTL, "715": CMUX}


# ── detect: coalitions ────────────────────────────────────────────────────────────────────
def test_684_processes_in_one_coalition_is_critical_and_names_its_launchd_label():
    procs = _leak(684)
    [f] = _coalitions(procs, {p.pid: "901" for p in procs}, {"901": TGCTL})
    assert f.key == f"coalition:{TGCTL}" and f.kind == "coalition" and f.severity == SEVERITY_CRITICAL
    assert f.metrics["coalition_procs"] == 684 and f.metrics["coalition_pct"] == pytest.approx(11.4, abs=0.05)
    assert f.metrics["label"] == TGCTL and f.restart_label == TGCTL and f.healable
    assert TGCTL in f.summary and "684" in f.summary and "tg-ctl" in f.summary
    assert len(f.offenders) == 684


def test_a_coalition_under_both_lines_is_silent():
    procs = _leak(120)
    assert _coalitions(procs, {p.pid: "901" for p in procs}, {"901": TGCTL}) == []


def test_the_percent_line_raises_the_severity_on_a_small_process_table():
    procs = _leak(250)
    [f] = _coalitions(procs, {p.pid: "901" for p in procs}, {"901": TGCTL}, proc_max=2000)
    assert f.severity == SEVERITY_CRITICAL and f.metrics["coalition_pct"] == 12.5  # 250 alone is under warn


def test_a_coalition_with_no_live_launchd_job_is_named_by_its_oldest_member_and_never_restarted():
    procs = _leak(700)
    [f] = _coalitions(procs, {p.pid: "1142" for p in procs}, {})
    assert f.key == "coalition:1142" and f.restart_label is None and f.metrics["label"] is None
    assert "no live launchd job" in f.summary and "tg-ctl pid 5000" in f.summary


def test_a_gui_app_coalition_alerts_but_never_carries_a_restart():
    procs = _leak(700)
    [f] = _coalitions(procs, {p.pid: "715" for p in procs}, {"715": CMUX})
    assert f.severity == SEVERITY_CRITICAL and f.metrics["label"] == CMUX
    steps, _ = heal.plan([f], {}, _heal_cfg(), 1000.0)
    steps, _ = heal.plan([f], _, _heal_cfg(), 2000.0)
    assert [s for s in steps if s.action == "restart"] == []


# ── detect: machine-wide storms ───────────────────────────────────────────────────────────
def test_a_storm_counts_every_users_processes_and_other_users_stay_protected():
    procs = [_proc(1000 + i, 1) for i in range(150)] + [_proc(2000 + i, 1, uid=ME + 1) for i in range(400)]
    [f] = _census(procs, proc_max=6000)
    assert f.key == "storm:sleep" and f.severity == SEVERITY_CRITICAL and len(f.offenders) == 550
    assert f.metrics["storm_same_name"] == 550 and f.metrics["storm_pct"] == pytest.approx(9.17, abs=0.01)
    theirs = [o for o in f.offenders if o.pid >= 2000]
    assert theirs and all(o.attribution.protected_reason == "another user's process" for o in theirs)
    steps, _ = heal.plan([f], {}, _heal_cfg(), 1000.0)
    assert {s.pid for s in steps if s.action == "throttle"} == {1000 + i for i in range(150)}


def test_the_storm_percent_line_can_raise_the_severity_above_the_absolute_one():
    procs = [_proc(1000 + i, 1) for i in range(100)]
    assert _census(procs, proc_max=6000) == []  # 100 of 6000: under both lines
    [f] = _census(procs, proc_max=1000)          # 100 of 1000 = 10 %: over the critical percent
    assert f.severity == SEVERITY_CRITICAL and f.metrics["storm_pct"] == 10.0


# ── heal: an exploded daemon coalition restarts the daemon ────────────────────────────────
def _heal_cfg(**over):
    cfg = config.load(None)
    cfg["heal"].update({"grace_seconds": 60, **over})
    return cfg


def test_a_critical_coalition_of_an_allowlisted_daemon_is_restarted_after_the_grace_period():
    procs = _leak(684)
    [f] = _coalitions(procs, {p.pid: "901" for p in procs}, {"901": TGCTL})
    cfg = _heal_cfg()
    steps, state = heal.plan([f], {}, cfg, 1000.0)
    assert [s for s in steps if s.action == "restart"] == []  # grace first
    steps, _ = heal.plan([f], state, cfg, 1060.0)
    assert [(s.action, s.label) for s in steps if s.action == "restart"] == [("restart", TGCTL)]


# ── the monitor: census, status and sample show the coalitions ────────────────────────────
class _Launchctl:
    def __call__(self, argv, **kw):
        out = "PID\tStatus\tLabel\n5000\t0\tai.hyperide.tg-ctl\n" if argv[:2] == ["launchctl", "list"] else ""
        return subprocess.CompletedProcess(argv, 0, out, "")


def test_the_monitor_attributes_a_coalition_storm_to_its_launchd_label(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "IS_DARWIN", True)
    procs = _leak(684)
    monkeypatch.setattr(probe, "proc_coalition", lambda pid: "901")
    cfg = config.load(None)
    cfg["state_dir"] = str(tmp_path / "state")
    cfg["events"]["enabled"] = False
    cfg["gpu"]["slot_dir"] = str(tmp_path / "slots")
    cfg["heal"]["dry_run"] = True
    mon = daemon.Monitor(cfg, sampler=lambda now=None: _sample(proc_total=900), censor=lambda: list(procs),
                         run=_Launchctl(), start_time_of=lambda pid: 1.0)
    mon.tick(1000.0)
    [f] = [f for f in mon.findings if f.kind == "coalition"]
    assert f.key == f"coalition:{TGCTL}" and f.severity == SEVERITY_CRITICAL
    [top] = mon.top_coalitions(1)
    assert top == {"coalition": "901", "label": TGCTL, "procs": 684, "pct": pytest.approx(11.4, abs=0.05),
                   "leader": "tg-ctl pid 5000"}
