"""sysmon daemon behavior (rig-cli#622): census parsing, attribution + the never-touch predicate,
detectors (incl. the 2026-09-29 wired-memory and display-freeze regressions), the log-event
parser/stream, alert dedup + budget, and the monitor's evaluation path end to end with fakes.
Heal/remediation and session restore have their own files (test_sysmon_heal/_sessions)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from riglib.sysmon_daemon import alert, attrib, config, daemon, detect, events, probe, store
from riglib.sysmon_daemon.model import SEVERITY_CRITICAL, SEVERITY_WARNING, AgentHost, Finding, Proc

ME = os.getuid()
GIB = probe.GIB


def _proc(pid, ppid, comm="sleep", args=None, uid=ME, tty="??", stat="S", etime=100.0, rss=1000):
    return Proc(pid=pid, ppid=ppid, pgid=pid, uid=uid, tty=tty, stat=stat, etime_s=etime, rss_kb=rss,
                cpu_pct=0.0, comm=comm, args=args or comm)


def _sample(**over):
    base = dict(
        taken_at=1000.0, ncpu=14, load1=2.0, load5=2.0, load15=2.0, mem_total=24 * GIB, wired=5 * GIB,
        free=1 * GIB, inactive=6 * GIB, speculative=0, purgeable=0, reclaimable=7 * GIB,
        compressor_pages=100, compressor_limit_pages=1000, swap_total=4 * GIB, swap_used=1 * GIB,
        pressure_level=1, proc_total=700, proc_max=6000, disk_free=50 * GIB, boot_id="boot-a",
    )
    base.update(over)
    return probe.Sample(**base)


TH = config.DEFAULT_THRESHOLDS


# ── probe: ps parsing ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("etime, seconds", [("05", 5), ("01:05", 65), ("02:01:05", 7265), ("3-02:01:05", 266465)])
def test_parse_etime(etime, seconds):
    assert probe.parse_etime(etime) == seconds


def _procargs2(argc, *strings):
    return argc.to_bytes(4, "little", signed=True) + b"/bin/omp\0\0\0\0" + b"\0".join(strings) + b"\0"


@pytest.mark.parametrize("raw, env", [
    (_procargs2(2, b"omp", b"--resume", b"CMUX_WORKSPACE_ID=WS", b"HOME=/h"), [b"CMUX_WORKSPACE_ID=WS", b"HOME=/h"]),
    (_procargs2(1, b"true"), []),                         # started with an empty environment
    (_procargs2(9, b"omp", b"HOME=/h"), []),               # argc past the payload: no environment
    (_procargs2(-3, b"omp", b"HOME=/h"), None),            # malformed argc
    (b"\x01\x00", None),                                   # truncated payload
], ids=["env-after-argv", "empty-env", "argc-overruns", "negative-argc", "truncated"])
def test_procargs2_environment_is_what_follows_argv(raw, env):
    found = probe.procargs2_environ(raw)
    assert (None if found is None else [s for s in found if s]) == env


def test_parse_census_joins_argv_and_keeps_executable_names_with_spaces():
    fixed = (
        "  101     1   101   501 ??       Ss   01:00   2048   0.5 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome\n"
        "  202   101   202   501 ttys001  S+   00:10    512   1,5 /bin/sleep\n"
        "garbage line\n"
    )
    argv = "  101 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --flag\n  202 sleep 600\n"
    procs = {p.pid: p for p in probe.parse_census(fixed, argv)}
    assert set(procs) == {101, 202}
    assert procs[101].comm == "Google Chrome" and procs[101].args.endswith("--flag")
    assert procs[202].foreground and procs[202].cpu_pct == 1.5 and procs[202].args == "sleep 600"


# ── attribution ───────────────────────────────────────────────────────────────────────────
OMP_SRC = "bun --preload /x/oh-my-pi/packages/coding-agent/scripts/omp.ts /x/oh-my-pi/packages/coding-agent/src/cli.ts"


@pytest.mark.parametrize("args, comm, expected", [
    (OMP_SRC, "bun", ("omp", True)),
    (OMP_SRC + " -p hello", "bun", ("omp", False)),
    ("/Users/u/.bun/bin/bun /x/oh-my-pi/src/cli.ts __omp_worker_daemon_broker", "bun", None),
    ("/Users/u/.bun/bin/omp --resume abc", "omp", ("omp", True)),
    ("claude --resume x", "claude", ("claude-code", True)),
    ("claude -p do it", "claude", ("claude-code", False)),
    ("/opt/homebrew/bin/codex exec fix", "codex", ("codex", False)),
    ("/opt/homebrew/bin/opencode run --agent r", "opencode", ("opencode", False)),
    ("/bin/sleep 5", "sleep", None),
])
def test_classify_host(args, comm, expected):
    assert attrib.classify_host(_proc(9, 1, comm=comm, args=args)) == expected


def _attributor(procs, *, panes=None, launchd=frozenset(), open_paths=None, starts=None, lineage=None, env=None):
    return attrib.Attributor(
        procs, panes=panes or {}, launchd_pids=set(launchd) if launchd is not None else None, my_uid=ME,
        sessions_root=Path("/s"), cwd_of=lambda pid: "/work",
        open_paths_of=lambda pid: (open_paths or {}).get(pid, []),
        starts={p.pid: 1000.0 + p.pid for p in procs} if starts is None else starts,
        lineage=lineage, env_of=lambda pid: (env or {}).get(pid),
    )


def _seen_over(*censuses, launchd=frozenset(), live=None):
    """The attributor of the LAST census, after every census went through one Lineage
    (``(procs, starts)`` pairs; a start time names the process, so a new start = a new process).
    ``live`` maps the pids a live read still finds (a host the census missed) to their start."""
    lineage = attrib.Lineage(lambda pid: (live or {}).get(pid))
    at = None
    for procs, starts in censuses:
        at = _attributor(procs, launchd=launchd, starts=starts, lineage=lineage)
        lineage.observe(at)
    return at


def _starts(procs):
    return {p.pid: 1000.0 + p.pid for p in procs}


def test_agent_child_is_attributed_to_host_session_and_pane():
    procs = [
        _proc(10, 1, comm="tmux"), _proc(20, 10, comm="zsh", tty="ttys001"),
        _proc(30, 20, comm="bun", args=OMP_SRC, tty="ttys001", stat="S+"),
        _proc(40, 30, comm="bash"), _proc(50, 40, comm="whisper-cli", stat="S+", tty="ttys001"),
    ]
    a = _attributor(procs, panes={20: "tmux:main:3.1"},
                    open_paths={30: ["/s/-work/2026_abc/Sub.jsonl", "/s/-work/2026_abc.jsonl"]}).attribute(50)
    assert a.host is not None and a.host.pid == 30 and a.host.session_file == "/s/-work/2026_abc.jsonl"
    assert a.host.session_id == "abc" and a.pane == "tmux:main:3.1"
    # a foreground job an AGENT started is not the user's interactive job
    assert a.protected_reason is None and not a.interactive and not a.orphan


@pytest.mark.parametrize("proc, reason", [
    (_proc(60, 20, comm="htop", tty="ttys002", stat="S+"), "user-interactive"),
    (_proc(61, 1, comm="zsh", tty="ttys003"), "shell"),
    (_proc(62, 1, comm="tmux"), "terminal"),
    (_proc(63, 1, comm="Code", args="/Applications/Code.app/Contents/MacOS/Code"), "GUI app"),
    (_proc(64, 1, comm="sleep", uid=ME + 1), "another user"),
    (_proc(65, 1, comm="cfprefsd", args="/usr/libexec/cfprefsd agent"), "system binary"),
    (_proc(66, 1, comm="tg-ctl"), "launchd job"),
])
def test_never_touch_predicate(proc, reason):
    a = _attributor([_proc(20, 1, comm="zsh", tty="ttys002"), proc], launchd={66}).attribute(proc.pid)
    assert a.protected_reason and reason in a.protected_reason


OMP_HOST = _proc(30, 20, comm="bun", args=OMP_SRC, tty="ttys001", stat="S+")
LOOP = "bash -c while true; do stt a.wav; done"


def test_tree_seen_under_an_agent_host_that_died_is_an_orphan_naming_it():
    """The 2026-09-29 shape: the omp host died, its `bash -c 'while …; stt …'` loop was
    reparented to launchd and kept relaunching whisper-cli."""
    before = [_proc(20, 1, comm="zsh", tty="ttys001"), OMP_HOST, _proc(70, 30, comm="bash", args=LOOP),
              _proc(71, 70, comm="whisper-cli")]
    after = [_proc(20, 1, comm="zsh", tty="ttys001"), _proc(70, 1, comm="bash", args=LOOP),
             _proc(72, 70, comm="whisper-cli")]  # a model the loop relaunched after the host died
    at = _seen_over((before, _starts(before)), (after, _starts(after)))
    for pid in (70, 72):
        a = at.attribute(pid)
        assert a.orphan and a.dead_owner == "omp pid 30" and a.protected_reason is None and a.host is None
    assert at.orphan_root(72) == 70


def test_detached_tree_without_a_recorded_dead_owner_is_no_orphan():
    """An agent-browser daemon double-forks straight to ppid 1; a nohup'd job of a LIVE agent is
    reparented while its agent still runs. Neither is an orphan with a dead owner."""
    daemon_tree = [_proc(80, 1, comm="node", args="node agent-browser daemon")]
    assert not _seen_over((daemon_tree, _starts(daemon_tree)), (daemon_tree, _starts(daemon_tree))).attribute(80).orphan
    before = [_proc(20, 1, comm="zsh", tty="ttys001"), OMP_HOST, _proc(81, 30, comm="bash", args="bash ship-loop.sh")]
    after = [_proc(20, 1, comm="zsh", tty="ttys001"), OMP_HOST, _proc(81, 1, comm="bash", args="bash ship-loop.sh")]
    assert not _seen_over((before, _starts(before)), (after, _starts(after))).attribute(81).orphan


def test_a_host_pid_taken_by_another_process_still_counts_as_dead():
    before = [OMP_HOST, _proc(70, 30, comm="bash", args=LOOP)]
    after = [_proc(30, 1, comm="node", args="node other"), _proc(70, 1, comm="bash", args=LOOP)]
    at = _seen_over((before, _starts(before)), (after, {30: 9999.0, 70: 1070.0}))
    assert at.attribute(70).orphan


def test_a_host_one_census_missed_is_read_live_and_its_tree_is_no_orphan():
    """A census row can drop out under load; with kill_orphans on, that alone must never turn a
    live agent's tree into a kill target."""
    before = [OMP_HOST, _proc(70, 30, comm="bash", args=LOOP)]
    after = [_proc(70, 30, comm="bash", args=LOOP)]  # the host's row is missing, the host is not
    at = _seen_over((before, _starts(before)), (after, _starts(after)), live={30: _starts(before)[30]})
    assert not at.attribute(70).orphan
    gone = _seen_over((before, _starts(before)), (after, _starts(after)))
    assert gone.attribute(70).orphan


def test_a_process_that_reused_a_recorded_pid_inherits_no_evidence():
    before = [OMP_HOST, _proc(70, 30, comm="bash", args=LOOP)]
    after = [_proc(70, 1, comm="bash", args="bash unrelated.sh")]
    at = _seen_over((before, _starts(before)), (after, {70: 5000.0}))
    assert not at.attribute(70).orphan


def test_orphan_needs_known_launchd_jobs_and_excludes_them():
    before = [OMP_HOST, _proc(80, 30, comm="node", args="node server.js")]
    after = [_proc(80, 1, comm="node", args="node server.js")]
    censuses = ((before, _starts(before)), (after, _starts(after)))
    assert _seen_over(*censuses).attribute(80).orphan
    assert not _seen_over(*censuses, launchd={80}).attribute(80).orphan
    assert not _seen_over(*censuses, launchd=None).attribute(80).orphan  # launchctl failed → fail closed


def test_cmux_attribution_names_the_workspace_and_surface():
    """Before: every process under cmux was attributed to the literal "cmux"."""
    procs = [_proc(673, 1, comm="cmux", args="/Applications/cmux.app/Contents/MacOS/cmux"),
             _proc(700, 673, comm="login", args="/usr/bin/login -flp ultra"), _proc(701, 700, comm="-zsh", tty="ttys004"),
             _proc(702, 701, comm="claude", args="claude"), _proc(703, 702, comm="whisper-cli")]
    env = {702: {"CMUX_WORKSPACE_ID": "WS-1", "CMUX_SURFACE_ID": "SF-2"}, 703: {"CMUX_WORKSPACE_ID": "WS-1",
                                                                             "CMUX_SURFACE_ID": "SF-2"}}
    a = _attributor(procs, env=env).attribute(703)
    assert a.pane == "cmux:WS-1/SF-2" and a.host is not None and a.host.pane == "cmux:WS-1/SF-2"
    assert _attributor(procs).attribute(703).pane == "cmux"  # environment unreadable: still says where


# ── detectors ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("metric, value, sev", [
    ("wired_pct", 74.9, "ok"), ("wired_pct", 75.0, SEVERITY_WARNING), ("wired_pct", 85.0, SEVERITY_CRITICAL),
    ("reclaimable_gb", 2.1, "ok"), ("reclaimable_gb", 2.0, SEVERITY_WARNING), ("reclaimable_gb", 0.9, SEVERITY_CRITICAL),
])
def test_level_boundaries_both_directions(metric, value, sev):
    assert detect.level(metric, value, TH) == sev


def test_panic_values_are_critical_although_memorystatus_says_normal():
    """2026-09-29 12:47 panic: 95.7% wired, ~112 MB reclaimable, memorystatus level 1 (normal)."""
    s = _sample(wired=int(24.65 * GIB), mem_total=int(25.77 * GIB), reclaimable=112 * 1024 * 1024, pressure_level=1)
    keys = {f.key: f.severity for f in detect.sample_findings(s, TH)}
    assert keys["wired"] == SEVERITY_CRITICAL and keys["reclaimable"] == SEVERITY_CRITICAL
    assert "pressure" not in keys


def test_healthy_sample_has_no_findings():
    assert detect.sample_findings(_sample(), TH) == []


def test_process_growth_rate_uses_the_window():
    old = _sample(taken_at=940.0, proc_total=700)
    s = _sample(taken_at=1000.0, proc_total=1000)
    [f] = [f for f in detect.sample_findings(s, TH, [old]) if f.key == "proc_growth"]
    assert f.severity == SEVERITY_WARNING and f.metrics["proc_growth_per_min"] == 300.0
    assert detect.proc_growth_per_min(s, [_sample(taken_at=995.0)]) is None  # too recent to judge


def _census(procs, **kw):
    at = _attributor(procs)

    def offender(p):
        from riglib.sysmon_daemon.model import Offender
        return Offender(p.pid, p.comm, p.args, p.rss_kb, 1.0, at.attribute(p.pid))

    args = dict(my_uid=ME, gpu_patterns=config.DEFAULT_GPU_PATTERNS, gpu_slots=2, gpu_slots_held=2,
                offender=offender, tree_of=lambda pid: [pid])
    args.update(kw)
    return detect.census_findings(procs, TH, **args)


def test_storm_is_per_user_and_name():
    procs = [_proc(1000 + i, 1) for i in range(150)] + [_proc(2000 + i, 1, uid=ME + 1) for i in range(400)]
    [f] = _census(procs)
    assert f.key == "storm:sleep" and f.severity == SEVERITY_WARNING and f.healable and len(f.offenders) == 150


def test_gpu_models_over_cap_is_critical_and_blames_the_whole_orphan_tree():
    procs = [_proc(90, 1, comm="bash"), *[_proc(91 + i, 90, comm="whisper-cli") for i in range(3)]]
    [f] = _census(procs, tree_of=lambda pid: [90, 91, 92, 93])
    assert f.key == "gpu_models" and f.severity == SEVERITY_CRITICAL
    assert {o.pid for o in f.offenders} == {90, 91, 92, 93}
    assert _census(procs[:3]) == []  # two model processes = at the cap, fine


def test_stuck_taskport_tools_only_flag_while_securityagent_is_up():
    stuck = [_proc(300 + i, 1, comm="sample", etime=90.0) for i in range(5)]
    assert _census(stuck) == []
    [f] = _census(stuck + [_proc(299, 1, comm="SecurityAgent", uid=ME + 1)])
    assert f.key == "taskport_blocked" and f.severity == SEVERITY_CRITICAL


def _delays(values, at=1000.0):
    return [{"kind": "display_delay", "at": at - i, "value": v} for i, v in enumerate(values)]


@pytest.mark.parametrize("values, sev", [
    ([1.0], None), ([1.0, 1.1], SEVERITY_WARNING), ([1.0] * 5, SEVERITY_CRITICAL), ([3.5], SEVERITY_CRITICAL),
])
def test_display_freeze_precursor(values, sev):
    found = detect.event_findings(_delays(values), TH, 1000.0)
    assert (found[0].severity if found else None) == sev


def test_display_events_older_than_a_minute_do_not_count():
    assert detect.event_findings(_delays([9.0], at=900.0), TH, 1000.0) == []


# ── events ────────────────────────────────────────────────────────────────────────────────
def _line(process, message):
    return json.dumps({"processImagePath": f"/System/Library/{process}", "eventMessage": message})


def test_parse_event_kinds():
    ev = events.parse_event(_line("WindowServer", "Timer delayed by 8.81s"), now=5.0)
    assert ev == {"kind": "display_delay", "at": 5.0, "value": 8.81, "text": "Timer delayed by 8.81s"}
    assert events.parse_event(_line("WindowServer", "invalidated after the last release")) is None
    auth = events.parse_event(_line("authd", "Validating session owner u (501) for system.privilege.taskport.debug"))
    assert auth["kind"] == "taskport_prompt"
    assert events.parse_event("Filtering the log data using ...") is None


class _FakeProc:
    def __init__(self, rfd):
        self.stdout = os.fdopen(rfd, "rb", buffering=0)
        self.alive = True

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.alive = False

    def wait(self, timeout=None):
        return 0


def test_log_stream_buffers_partial_lines_and_restarts_after_eof():
    spawned = []

    def spawn(argv, **kw):
        r, w = os.pipe()
        spawned.append(w)
        return _FakeProc(r)

    clock = [0.0]
    ls = events.LogStream(spawn=spawn, clock=lambda: clock[0], backoff_s=30.0)
    ls.ensure()
    os.write(spawned[0], b'{"a":1}\n{"b"')
    assert ls.read_lines() == ['{"a":1}']
    os.write(spawned[0], b':2}\n')
    assert ls.read_lines() == ['{"b":2}']
    os.close(spawned[0])
    assert ls.read_lines() == [] and ls.fileno() is None
    ls.ensure()
    assert len(spawned) == 1  # backing off
    clock[0] = 31.0
    ls.ensure()
    assert len(spawned) == 2 and ls.fileno() is not None
    os.close(spawned[1])
    ls.stop()


# ── alerts ────────────────────────────────────────────────────────────────────────────────
def _cfg(**alert_over):
    cfg = config.load(None)
    cfg["alert"].update(alert_over)
    return cfg


def _f(key="wired", sev=SEVERITY_CRITICAL):
    return Finding(key=key, kind="memory", severity=sev, summary=f"{key} bad")


def test_alert_dedup_cooldown_and_escalation():
    cfg = _cfg(min_severity="warning")
    due, _, st = alert.decide([_f(sev=SEVERITY_WARNING)], {}, cfg, 0.0)
    assert [f.key for f in due] == ["wired"]
    due, _, st = alert.decide([_f(sev=SEVERITY_WARNING)], st, cfg, 60.0)
    assert due == []  # same severity inside the cooldown
    due, _, st = alert.decide([_f(sev=SEVERITY_CRITICAL)], st, cfg, 120.0)
    assert [f.severity for f in due] == [SEVERITY_CRITICAL]  # escalation re-alerts at once
    due, _, st = alert.decide([_f()], st, cfg, 120.0 + 30 * 60)
    assert len(due) == 1  # cooldown elapsed


def test_warning_below_min_severity_is_silent():
    assert alert.decide([_f(sev=SEVERITY_WARNING)], {}, _cfg(), 0.0)[0] == []


def test_budget_holds_alerts_and_releases_them_later():
    cfg = _cfg(max_per_hour=1)
    due, _, st = alert.decide([_f("a")], {}, cfg, 0.0)
    assert len(due) == 1
    due, _, st = alert.decide([_f("a"), _f("b")], st, cfg, 10.0)
    assert due == []  # budget spent: b is held, not dropped
    due, _, st = alert.decide([_f("a"), _f("b")], st, cfg, 3700.0)
    assert [f.key for f in due] == ["a", "b"]  # one message carries both


def test_resolved_only_after_staying_clear():
    cfg = _cfg()
    _, _, st = alert.decide([_f()], {}, cfg, 0.0)
    _, resolved, st = alert.decide([], st, cfg, 10.0)
    assert resolved == []
    due, resolved, st = alert.decide([_f()], st, cfg, 100.0)
    assert due == [] and resolved == []  # a flap back inside the window is neither new nor resolved
    _, _, st = alert.decide([], st, cfg, 200.0)
    _, resolved, st = alert.decide([], st, cfg, 200.0 + alert.RESOLVE_AFTER_S)
    assert resolved == ["wired"]


def test_render_names_the_owning_agent_and_test_label():
    from riglib.sysmon_daemon.model import Attribution, Offender

    host = AgentHost(pid=30, kind="omp", args="omp", cwd="/w", session_id="01a0e21a-ffff", pane="tmux:main:6.1")
    off = Offender(50, "whisper-cli", "whisper-cli -m x", 1_600_000, 1.0, Attribution(pid=50, chain=(50, 30), host=host))
    finding = Finding("gpu_models", "gpu", SEVERITY_CRITICAL, "13 GPU-model processes", {}, (off,), True)
    title, body = alert.render([finding], incident_id="I1", test=True, host="mac")
    assert title.startswith("TEST — sysmon CRITICAL on mac")
    assert "omp pid 30 (tmux:main:6.1 session 01a0e21a, /w)" in body and "TEST MESSAGE" in body


def test_send_dry_run_never_calls_tg():
    cfg = _cfg(dry_run=True)
    rec = alert.send("t", "b", cfg, run=lambda *a, **k: pytest.fail("tg must not run"))
    assert rec["outcome"] == "dry_run" and rec["argv"][:3] == ["tg", "--tag", "problem"]


# ── the monitor's evaluation path ─────────────────────────────────────────────────────────
class _Run:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        import subprocess

        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")


def _monitor(tmp_path, samples, **cfg_over):
    cfg = config.load(None)
    cfg["state_dir"] = str(tmp_path / "state")
    cfg["events"]["enabled"] = False
    for k, v in cfg_over.items():
        cfg[k].update(v) if isinstance(v, dict) else cfg.__setitem__(k, v)
    run = _Run()
    it = iter(samples)
    mon = daemon.Monitor(cfg, sampler=lambda now=None: next(it), censor=lambda: [], run=run)
    return mon, run


def _tg_sends(run):
    return [c for c in run.calls if c and c[0] == "tg"]


def test_critical_opens_one_incident_alerts_once_and_closes_after_quiet(tmp_path):
    hot = _sample(wired=int(23 * GIB))
    calm = _sample()
    mon, run = _monitor(tmp_path, [hot, hot, calm, calm])
    mon.tick(1000.0)
    mon.tick(1010.0)
    assert len(_tg_sends(run)) == 1
    inc_id = mon.memory["open_incident"]
    assert inc_id and store.load_incident(mon.state_dir, inc_id)["findings"][0]["key"] == "wired"
    mon.tick(1020.0)
    assert mon.incident is not None  # not closed yet: quiet for only 10 s
    mon.tick(1010.0 + daemon.CLOSE_AFTER_S)
    assert mon.incident is None and store.load_incident(mon.state_dir, inc_id)["closed_at"]
    hb = store.read_json(mon.state_dir / store.STATUS)["heartbeat"]
    assert hb["clean_shutdown"] is False and hb["boot_id"] == "boot-a"


def test_crash_since_the_last_heartbeat_opens_a_post_crash_incident(tmp_path):
    state = tmp_path / "state"
    store.write_json(state / store.STATUS, {"heartbeat": {"boot_id": "boot-old", "last_heartbeat": 1.0,
                                                           "clean_shutdown": False}})
    store.write_json(state / store.SESSIONS, [{
        "kind": "omp", "pid": 5, "start_time": 1.0, "cwd": "/w", "session_file": "/s/-w/x_abc.jsonl",
        "session_id": "abc", "pane": "tmux:main:1.1", "title": "t", "captured_at": 1.0,
    }])
    mon, run = _monitor(tmp_path, [_sample(boot_id="boot-new")], sessions={"panic_dirs": [str(tmp_path / "none")]})
    inc = mon.check_crash(2000.0)
    assert inc["kind"] == "post-crash" and inc["crash"]["reason"] == "unclean-reboot"
    assert [s["session_id"] for s in inc["sessions"]] == ["abc"]
    assert len(_tg_sends(run)) == 1 and inc["remediation"]["status"] == "disabled"


def test_clean_shutdown_is_not_a_crash(tmp_path):
    state = tmp_path / "state"
    store.write_json(state / store.STATUS, {"heartbeat": {"boot_id": "boot-old", "last_heartbeat": 1.0,
                                                           "clean_shutdown": True}})
    mon, run = _monitor(tmp_path, [_sample(boot_id="boot-new")])
    assert mon.check_crash(2000.0) is None and _tg_sends(run) == []


def test_simulation_never_messages_telegram_or_signals(tmp_path, monkeypatch):
    cfg = config.load(None)
    cfg["state_dir"] = str(tmp_path / "state")
    calls = []
    monkeypatch.setattr(daemon.probe, "census", lambda **kw: [])
    monkeypatch.setattr(daemon.probe, "tmux_panes", lambda *a, **k: {})
    monkeypatch.setattr(daemon.probe, "launchd_jobs", lambda **k: {})
    monkeypatch.setattr(daemon.alert, "send", lambda *a, **k: calls.append(a[2]["alert"]["dry_run"]) or {"outcome": "dry_run"})
    inc = daemon.simulate(cfg, [_f("display_freeze")])
    assert inc["kind"] == "simulated" and calls == [True]
    assert not (tmp_path / "state" / store.STATUS).exists()


def test_gpu_blame_never_widens_to_a_launchd_started_app_tree(tmp_path):
    """Regression (live, 2026-09-29): whisper under cmux (ppid 1, a GUI app launchd runs) made
    the orphan-tree helper blame every process under cmux — shells, other agents' test runs."""
    cmux = _proc(673, 1, comm="cmux", args="/Applications/cmux.app/Contents/MacOS/cmux")
    shell = _proc(700, 673, comm="zsh", tty="ttys001")
    vitest = _proc(701, 700, comm="node", args="node vitest run")
    whisper = _proc(702, 700, comm="whisper-server")
    before = [cmux, shell, vitest, whisper, OMP_HOST, _proc(800, 30, comm="bash", args=LOOP),
              _proc(801, 800, comm="whisper-cli")]
    after = [cmux, shell, vitest, whisper, _proc(800, 1, comm="bash", args=LOOP), _proc(801, 800, comm="whisper-cli")]
    mon, _ = _monitor(tmp_path, [])
    mon.attributor = _seen_over((before, _starts(before)), (after, _starts(after)))
    assert mon.orphan_tree(702) == [702]
    assert sorted(mon.orphan_tree(801)) == [800, 801]


# ── the monitor: heal wiring, census snapshot, event debounce ─────────────────────────────
class _Census:
    """A process table the test rewrites between ticks, with the start time of every pid."""

    def __init__(self, procs, starts=None):
        self.procs, self.starts, self.calls = procs, starts or _starts(procs), 0

    def __call__(self):
        self.calls += 1
        return list(self.procs)


class _BandOps:
    """Heal side effects against the fake census: which pids sit in the background band."""

    def __init__(self, census):
        self.census, self.bg, self.signals = census, set(), []

    def ops(self):
        from riglib.sysmon_daemon import heal

        return heal.HealOps(
            start_time=lambda pid: self.census.starts.get(pid) if any(p.pid == pid for p in self.census.procs) else None,
            is_background=lambda pid: pid in self.bg, background=lambda pid: self.bg.add(pid),
            foreground=lambda pid: self.bg.discard(pid), signal=lambda pid, sig: self.signals.append((pid, sig)),
            sleep=lambda s: None, notify_agent=lambda host, text: "undelivered: test",
            restart=lambda label: "restart: not in this test")


def _heal_monitor(tmp_path, census, **heal_over):
    cfg = config.load(None)
    cfg["state_dir"] = str(tmp_path / "state")
    cfg["events"]["enabled"] = False
    cfg["gpu"]["slot_dir"] = str(tmp_path / "slots")
    cfg["heal"].update(heal_over)
    band = _BandOps(census)
    ops = band.ops()
    mon = daemon.Monitor(cfg, sampler=lambda now=None: _sample(), censor=census, run=_Run(),
                         start_time_of=ops.start_time, heal_ops=ops)
    return mon, band


def _models(n, ppid):
    return [_proc(900 + i, ppid, comm="whisper-cli", rss=1_600_000) for i in range(n)]


def test_the_monitor_restores_its_throttles_when_the_pressure_clears_and_when_it_stops(tmp_path):
    census = _Census([_proc(20, 1, comm="zsh", tty="ttys001"), OMP_HOST, *_models(3, 30)])
    mon, band = _heal_monitor(tmp_path, census)
    mon.tick(1000.0)
    assert band.bg == {900, 901, 902}
    census.procs = census.procs[:3]  # 901/902 finished: the cap holds again (seen at the next census)
    mon.tick(1060.0)
    assert 900 not in band.bg  # the survivor runs at normal priority again; the exited pids are skipped
    census.procs = [*census.procs[:2], *_models(3, 30)]
    band.bg.clear()
    mon.tick(1120.0)
    assert band.bg == {900, 901, 902}
    mon.release_throttles()
    assert band.bg == set()


def test_throttles_are_on_disk_before_a_slow_note_so_a_sigkill_there_strands_nothing(tmp_path):
    """The throttle burst runs first and is written to disk before the slow steps (typing a note,
    a kill grace): a daemon SIGKILLed while blocked there leaves a record the next start restores."""
    census = _Census([_proc(20, 1, comm="zsh", tty="ttys001"), OMP_HOST, *_models(3, 30)])
    mon, band = _heal_monitor(tmp_path, census)

    class Killed(BaseException):
        pass

    def notify(host, text):
        raise Killed  # SIGKILL while the note to the owning agent is being typed

    mon.heal_ops.notify_agent = notify
    with pytest.raises(Killed):
        mon.tick(1000.0)
    assert band.bg == {900, 901, 902}  # the mitigation already happened
    census.procs = census.procs[:3]  # the other models finished: the cap holds again
    restarted, band2 = _heal_monitor(tmp_path, census)
    band2.bg = set(band.bg)
    restarted.tick(1060.0)
    assert 900 not in band2.bg  # restored from the record on disk (901/902 exited: skipped)


def test_a_detached_model_tree_is_throttled_but_never_killed_even_with_kill_orphans(tmp_path):
    loop = _proc(70, 1, comm="bash", args=LOOP)
    census = _Census([loop, *_models(3, 70)])
    mon, band = _heal_monitor(tmp_path, census, kill_orphans=True, grace_seconds=0)
    for now in (1000.0, 1060.0, 1120.0):
        mon.tick(now)
    assert band.bg == {900, 901, 902}  # the detached loop is not blamed without evidence
    assert band.signals == []


def test_a_proven_orphan_tree_is_killed_only_when_kill_orphans_is_on(tmp_path):
    alive = [_proc(20, 1, comm="zsh", tty="ttys001"), OMP_HOST, _proc(70, 30, comm="bash", args=LOOP)]
    orphaned = [_proc(20, 1, comm="zsh", tty="ttys001"), _proc(70, 1, comm="bash", args=LOOP), *_models(3, 70)]
    for kill_orphans, killed in ((False, set()), (True, {70, 900, 901, 902})):
        census = _Census(alive)
        mon, band = _heal_monitor(tmp_path / str(kill_orphans), census, kill_orphans=kill_orphans, grace_seconds=0)
        mon.tick(1000.0)
        census.procs, census.starts = orphaned, {**census.starts, **_starts(orphaned)}
        for now in (1060.0, 1070.0):
            mon.tick(now)
        assert {pid for pid, _ in band.signals} == killed


def test_the_pid_reuse_guard_uses_the_start_time_the_census_saw(tmp_path):
    """The census can be a minute old: reading the start time live when the stale census is
    evaluated again would vouch for whatever process holds the pid by then."""
    census = _Census([_proc(70, 1, comm="bash"), *_models(3, 70)])
    mon, _ = _heal_monitor(tmp_path, census)
    mon.take_census(1000.0)
    census.starts = {**census.starts, 901: 7777.0}  # pid 901 exited and was reused
    [f] = [f for f in mon.census_findings() if f.key == "gpu_models"]
    assert {o.pid: o.start_time for o in f.offenders}[901] == 1901.0


def test_a_burst_of_log_events_reuses_a_fresh_census(tmp_path):
    census = _Census([_proc(70, 1, comm="bash")])
    mon, _ = _heal_monitor(tmp_path, census)
    mon.tick(1000.0)
    assert census.calls == 1
    for i, now in enumerate((1001.0, 1002.0, 1005.0)):
        mon.window.add({"kind": "display_delay", "at": now, "value": 4.0 + i, "text": "Timer delayed"})
        mon.on_events(now)
    assert census.calls == 1
    mon.window.add({"kind": "display_delay", "at": 1011.0, "value": 5.0, "text": "Timer delayed"})
    mon.on_events(1011.0)
    assert census.calls == 2
