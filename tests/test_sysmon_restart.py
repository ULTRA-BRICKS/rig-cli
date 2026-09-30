"""sysmon heal rung ``restart`` (rig-cli#643): a hung or crash-looping launchd daemon is restarted
with ``launchctl kickstart -k`` — only a label launchd itself reported, only an allowlisted one,
never the monitor itself or a GUI app, at most once per cooldown and a few times a day."""

from __future__ import annotations

import os
import subprocess

import pytest

from riglib.sysmon_daemon import config, daemon, detect, heal, probe
from riglib.sysmon_daemon.model import SEVERITY_CRITICAL, SEVERITY_WARNING, Attribution, Finding, LaunchdJob, Offender

UID = os.getuid()
AUTOSAVE = "ai.hyperide.tmux-autosave"
TGCTL = "ai.hyperide.tg-ctl"

LIST = (
    "PID\tStatus\tLabel\n"
    "-\t0\tai.hyperide.tmux-autosave\n"
    "19023\t0\tai.hyperide.tg-ctl\n"
    "11129\t0\tai.hyperide.sysmon\n"
    "15779\t-9\tcom.cloudflare.cloudflared.local-hyperide\n"
    "673\t0\tapplication.com.cmuxterm.app.698704579.698704585\n"
)

# `launchctl print gui/<uid>/<label>`: top-level keys are indented by ONE tab; nested blocks
# (endpoints, event triggers) repeat keys such as `state` two tabs deep.
PRINT_INTERVAL_RUNNING = (
    "gui/501/ai.hyperide.tmux-autosave = {\n"
    "\tactive count = 1\n"
    "\tpath = /Users/u/Library/LaunchAgents/ai.hyperide.tmux-autosave.plist\n"
    "\tstate = running\n"
    "\n"
    "\tprogram = /Users/u/.config/rig/tmux/tmux-autosave.sh\n"
    "\truns = 108\n"
    "\tpid = 4242\n"
    "\tlast exit code = 0\n"
    "\n"
    "\tevent triggers = {\n"
    "\t\tstate = active\n"
    "\t}\n"
    "\tspawn type = daemon (3)\n"
    "\trun interval = 900 seconds\n"
    "}\n"
)
PRINT_KEEPALIVE = (
    "gui/501/ai.hyperide.tg-ctl = {\n"
    "\tstate = {state}\n"
    "\truns = {runs}\n"
    "\tpid = 19023\n"
    "\tlast exit code = {code}\n"
    "\tspawn type = daemon (3)\n"
    "\tproperties = keepalive | runatload | inferred program\n"
    "}\n"
)


# ── probe: launchctl parsing ──────────────────────────────────────────────────────────────
def test_launchctl_list_gives_every_label_with_its_pid_and_last_status():
    jobs = probe.parse_launchctl_list(LIST)
    assert jobs[AUTOSAVE] == LaunchdJob(label=AUTOSAVE, pid=None, status=0)
    assert jobs[TGCTL].pid == 19023 and jobs["com.cloudflare.cloudflared.local-hyperide"].status == -9


def test_launchctl_print_reads_only_top_level_keys():
    job = probe.parse_launchctl_print(LaunchdJob(label=AUTOSAVE, pid=None, status=0), PRINT_INTERVAL_RUNNING)
    assert (job.state, job.pid, job.runs, job.last_exit_code, job.run_interval_s) == ("running", 4242, 108, 0, 900)
    never = probe.parse_launchctl_print(LaunchdJob(label=TGCTL, pid=1, status=0),
                                        "x = {\n\tstate = spawn scheduled\n\tlast exit code = (never exited)\n}\n")
    assert never.state == "spawn scheduled" and never.last_exit_code is None and never.runs is None


# ── detect: hung and crash-looping jobs ───────────────────────────────────────────────────
TH = config.DEFAULT_THRESHOLDS


def _interval_job(pid=4242, state="running"):
    return LaunchdJob(label=AUTOSAVE, pid=pid, status=0, state=state, runs=108, last_exit_code=0, run_interval_s=900)


@pytest.mark.parametrize("etime, sev", [(1700.0, None), (1900.0, SEVERITY_WARNING), (2800.0, SEVERITY_CRITICAL)])
def test_an_interval_job_running_far_past_its_interval_is_hung(etime, sev):
    found = detect.daemon_findings({AUTOSAVE: _interval_job()}, {}, lambda pid: etime, TH, now=5000.0)
    assert [f.severity for f in found] == ([sev] if sev else [])
    if sev:
        f = found[0]
        assert f.key == f"daemon_hung:{AUTOSAVE}" and f.kind == "daemon" and f.restart_label == AUTOSAVE
        assert AUTOSAVE in f.summary and f.metrics["pid"] == 4242 and f.metrics["run_interval_s"] == 900


def test_an_idle_interval_job_or_an_unreadable_age_is_not_hung():
    assert detect.daemon_findings({AUTOSAVE: _interval_job(pid=None, state="not running")}, {},
                                  lambda pid: 9999.0, TH, now=5000.0) == []
    assert detect.daemon_findings({AUTOSAVE: _interval_job()}, {}, lambda pid: None, TH, now=5000.0) == []


def _keepalive(runs, code):
    return LaunchdJob(label=TGCTL, pid=19023, status=code, state="running", runs=runs, last_exit_code=code)


@pytest.mark.parametrize("code, sev", [(1, SEVERITY_CRITICAL), (0, None)])
def test_a_job_relaunched_again_and_again_with_a_failing_exit_is_a_crash_loop(code, sev):
    history = {TGCTL: [(4400.0, 11), (4700.0, 14)]}
    found = detect.daemon_findings({TGCTL: _keepalive(19, code)}, history, lambda pid: 5.0, TH, now=5000.0)
    assert [f.severity for f in found] == ([sev] if sev else [])
    if sev:
        assert found[0].key == f"daemon_crashloop:{TGCTL}" and found[0].restart_label == TGCTL
        assert found[0].metrics["daemon_crashloop_runs"] == 8


def test_runs_older_than_the_window_do_not_count_as_a_crash_loop():
    history = {TGCTL: [(1000.0, 11), (4500.0, 18)]}
    assert detect.daemon_findings({TGCTL: _keepalive(19, 1)}, history, lambda pid: 5.0, TH, now=5000.0) == []


def test_an_interval_job_runs_on_schedule_is_no_crash_loop_even_when_it_fails():
    job = LaunchdJob(label=AUTOSAVE, pid=None, status=1, state="not running", runs=20, last_exit_code=1,
                     run_interval_s=60)
    found = detect.daemon_findings({AUTOSAVE: job}, {AUTOSAVE: [(4400.0, 10)]}, lambda pid: None, TH, now=5000.0)
    assert found == []  # 10 runs in 600 s is exactly what a 60 s interval does


# ── heal: the restart rung and its guards ─────────────────────────────────────────────────
def _cfg(**heal_over):
    cfg = config.load(None)
    cfg["heal"].update({"grace_seconds": 60, **heal_over})
    return cfg


def _hung(label=AUTOSAVE, severity=SEVERITY_CRITICAL):
    return Finding(key=f"daemon_hung:{label}", kind="daemon", severity=severity, summary=f"{label} hung",
                   metrics={"label": label, "pid": 4242}, restart_label=label)


def _ladder(ticks, cfg, state=None, outcome="done"):
    """``[(now, findings)]`` through plan + record; the restart steps of every tick."""
    state = state or {}
    out = []
    for now, findings in ticks:
        steps, state = heal.plan(findings, state, cfg, now)
        results = [{"action": s.action, "pid": s.pid, "start_time": s.start_time, "finding_key": s.finding_key,
                    "label": s.label, "outcome": outcome, "detail": "", "at": now} for s in steps]
        state = heal.record(state, results)
        out.append([(s.action, s.label) for s in steps if s.action == "restart"])
    return out, state


def test_a_hung_daemon_is_restarted_once_after_the_grace_period():
    out, _ = _ladder([(1000.0, [_hung()]), (1030.0, [_hung()]), (1060.0, [_hung()]), (1070.0, [_hung()])], _cfg())
    assert out == [[], [], [("restart", AUTOSAVE)], []]


def test_no_second_restart_inside_the_per_label_cooldown():
    cfg = _cfg(restart_cooldown_minutes=30)
    out, _ = _ladder([(1000.0, [_hung()]), (1060.0, [_hung()]), (1060.0 + 29 * 60, [_hung()]),
                      (1060.0 + 30 * 60, [_hung()])], cfg)
    assert out == [[], [("restart", AUTOSAVE)], [], [("restart", AUTOSAVE)]]


def test_the_daily_budget_stops_restarts_and_raises_a_finding_that_says_so():
    cfg = _cfg(restart_cooldown_minutes=1, max_restarts_per_day=2, grace_seconds=0)
    ticks = [(1000.0 + i * 120, [_hung()]) for i in range(4)]
    out, state = _ladder(ticks, cfg)
    assert out == [[("restart", AUTOSAVE)], [("restart", AUTOSAVE)], [], []]
    [spent] = heal.budget_findings([_hung()], state, cfg, now=1400.0)
    assert spent.key == f"restart_budget:{AUTOSAVE}" and spent.severity == SEVERITY_CRITICAL
    assert "2 restarts in 24 h" in spent.summary and spent.restart_label is None
    # a day later the budget is back
    assert heal.budget_findings([_hung()], state, cfg, now=1000.0 + 86400 + 400) == []


@pytest.mark.parametrize("label", ["com.apple.Safari.helper", "ai.hyperide.sysmon",
                                   "application.com.cmuxterm.app.698704579.698704585"])
def test_never_restarts_a_label_outside_the_allowlist_the_monitor_itself_or_a_gui_app(label):
    # even an allowlist that names the GUI app pattern never reaches it
    cfg = _cfg(restart_labels=["ai.hyperide.*", "application.*"], grace_seconds=0)
    out, _ = _ladder([(1000.0, [_hung(label)]), (2000.0, [_hung(label)])], cfg)
    assert out == [[], []]


def test_a_finding_without_a_launchd_label_never_restarts_anything():
    """Offenders the monitor cannot attribute to a launchd job are never restarted — the ladder
    has only its throttle/kill rungs for them, with their own guards."""
    offender = Offender(pid=70, comm="bash", args="bash loop.sh", rss_kb=10, start_time=1.0,
                        attribution=Attribution(pid=70, chain=(70, 1)))
    storm = Finding(key="storm:bash", kind="storm", severity=SEVERITY_CRITICAL, summary="300 x bash",
                    offenders=(offender,), healable=True)
    out, _ = _ladder([(1000.0, [storm]), (5000.0, [storm])], _cfg(grace_seconds=0))
    assert out == [[], []]


def test_restart_daemons_off_or_a_warning_restarts_nothing():
    assert _ladder([(1000.0, [_hung()]), (2000.0, [_hung()])], _cfg(restart_daemons=False))[0] == [[], []]
    warn = _hung(severity=SEVERITY_WARNING)
    assert _ladder([(1000.0, [warn]), (2000.0, [warn])], _cfg())[0] == [[], []]


def test_a_dry_run_restart_is_recorded_and_honours_the_cooldown():
    cfg = _cfg(dry_run=True, grace_seconds=0)
    out, state = _ladder([(1000.0, [_hung()]), (1010.0, [_hung()])], cfg, outcome="dry_run")
    assert out == [[("restart", AUTOSAVE)], []]
    [step] = heal.plan([_hung()], {}, cfg, 1000.0)[0]
    [result] = heal.execute([step], cfg, _NoOps().ops())
    assert result["outcome"] == "dry_run" and f"kickstart -k gui/{UID}/{AUTOSAVE}" in result["detail"]


class _NoOps:
    def __init__(self, error=None):
        self.error, self.restarted = error, []

    def restart(self, label):
        self.restarted.append(label)
        return self.error

    def ops(self):
        return heal.HealOps(start_time=lambda pid: None, is_background=lambda pid: None,
                            background=lambda pid: None, foreground=lambda pid: None,
                            signal=lambda pid, sig: None, sleep=lambda s: None,
                            notify_agent=lambda host, text: "undelivered", restart=self.restart)


@pytest.mark.parametrize("error, outcome", [(None, "done"), ("kickstart: exit 113", "failed")])
def test_execute_restarts_by_label_and_reports_failure(error, outcome):
    cfg = _cfg(grace_seconds=0)
    [step] = heal.plan([_hung()], {}, cfg, 1000.0)[0]
    fake = _NoOps(error)
    [result] = heal.execute([step], cfg, fake.ops())
    assert fake.restarted == [AUTOSAVE] and result["outcome"] == outcome and result["label"] == AUTOSAVE


def test_default_restart_op_is_launchctl_kickstart_k_in_the_gui_domain(monkeypatch):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(heal.probe, "IS_DARWIN", True)
    assert heal.default_ops(config.load(None), run=run).restart(AUTOSAVE) is None
    assert calls == [["launchctl", "kickstart", "-k", f"gui/{UID}/{AUTOSAVE}"]]


# ── the monitor end to end with a fake launchctl ──────────────────────────────────────────
class _Launchctl:
    """``launchctl list`` / ``print`` answers for a hung tmux-autosave run (pid 4242, 50 min into a
    15 min interval); every other command succeeds silently."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        out = ""
        if argv[:2] == ["launchctl", "list"]:
            out = LIST
        elif argv[:2] == ["launchctl", "print"] and argv[2].endswith("/" + AUTOSAVE):
            out = PRINT_INTERVAL_RUNNING
        elif argv[:2] == ["launchctl", "print"]:
            return subprocess.CompletedProcess(argv, 113, "", "Could not find service")
        return subprocess.CompletedProcess(argv, 0, out, "")


def test_the_monitor_restarts_a_hung_rig_daemon_exactly_once(tmp_path, monkeypatch):
    from tests.test_sysmon_daemon import _proc, _sample

    monkeypatch.setattr(probe, "IS_DARWIN", True)
    cfg = config.load(None)
    cfg["state_dir"] = str(tmp_path / "state")
    cfg["events"]["enabled"] = False
    cfg["gpu"]["slot_dir"] = str(tmp_path / "slots")
    cfg["heal"]["grace_seconds"] = 0
    procs = [_proc(4242, 1, comm="bash", args="/bin/bash tmux-autosave.sh", etime=3000.0)]
    ops = _NoOps()
    launchctl = _Launchctl()
    mon = daemon.Monitor(cfg, sampler=lambda now=None: _sample(), censor=lambda: list(procs), run=launchctl,
                         start_time_of=lambda pid: 1.0, heal_ops=ops.ops())
    for now in (1000.0, 1010.0, 1020.0):
        mon.tick(now)
    assert [f.key for f in mon.findings if f.kind == "daemon"] == [f"daemon_hung:{AUTOSAVE}"]
    assert ops.restarted == [AUTOSAVE]
    printed = {c[2] for c in launchctl.calls if c[:2] == ["launchctl", "print"]}
    # only allowlisted labels are probed: never the monitor itself, never a GUI app or a foreign job
    assert printed == {f"gui/{UID}/{AUTOSAVE}", f"gui/{UID}/{TGCTL}"}
