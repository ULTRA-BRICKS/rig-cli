"""sysmon daemon behavior (rig-cli#622): census parsing, attribution + the never-touch predicate,
detectors (incl. the 2026-09-29 wired-memory and display-freeze regressions), the log-event
parser/stream, alert dedup + budget, and the monitor's evaluation path end to end with fakes.
Heal/remediation and session restore have their own files (test_sysmon_heal/_sessions)."""

from __future__ import annotations

import errno
import json
import os
import subprocess
from pathlib import Path

import pytest

from riglib.sysmon_daemon import alert, attrib, config, daemon, detect, events, probe, store
from riglib.sysmon_daemon.model import SEVERITY_CRITICAL, SEVERITY_WARNING, AgentHost, Finding, Proc

ME = os.getuid()
GIB = probe.GIB


def _proc(pid, ppid, comm="sleep", args=None, uid=ME, tty="??", stat="S", etime=100.0, rss=1000):
    return Proc(pid=pid, ppid=ppid, pgid=pid, uid=uid, tty=tty, stat=stat, etime_s=etime, rss_kb=rss,
                cpu_pct=0.0, comm=comm, args=args or comm)


PAGE = 16384  # this Mac's VM page (vm.pagesize)


def _sample(**over):
    base = dict(
        taken_at=1000.0, ncpu=14, load1=2.0, load5=2.0, load15=2.0, mem_total=24 * GIB, wired=5 * GIB,
        free=1 * GIB, inactive=6 * GIB, speculative=0, purgeable=0, reclaimable=7 * GIB,
        compressor_pages=100, compressor_limit_pages=1000, swap_total=4 * GIB, swap_used=1 * GIB,
        pressure_level=1, proc_total=700, proc_max=6000, disk_free=50 * GIB, boot_id="boot-a",
        page_size=PAGE, data_volume="/System/Volumes/Data", data_used=200 * GIB, data_free=250 * GIB,
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
    ("free_gb", 0.06, "ok"), ("free_gb", 0.05, SEVERITY_WARNING), ("free_gb", 0.02, SEVERITY_CRITICAL),
    ("data_used_pct", 89.9, "ok"), ("data_used_pct", 90.0, SEVERITY_WARNING), ("data_used_pct", 95.0, SEVERITY_CRITICAL),
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


@pytest.mark.parametrize("pages, sev", [
    # Calibration (this Mac, 2026-09-30): the pageout daemon holds free pages near
    # vm_page_free_target (4000 x 16 KB = 62.5 MB); the 2026-09-29 panic's 4,610 free pages sat
    # at that same level, so it must stay silent here.
    (4000, None),
    (3000, SEVERITY_WARNING),  # under vm_page_free_min (3500): the pageout daemon is losing
    (1000, SEVERITY_CRITICAL),  # near vm_page_free_reserved (910): only VM-privileged allocations
])
def test_free_pages_are_their_own_threshold(pages, sev):
    found = {f.key: f for f in detect.sample_findings(_sample(free=pages * PAGE), TH)}
    assert (found["free"].severity if "free" in found else None) == sev
    if sev:
        assert found["free"].kind == "memory" and f"{pages:,} pages" in found["free"].summary


def test_a_momentary_free_page_dip_is_normal_load_and_only_a_sustained_one_counts():
    """10,616 reads at 50 ms over 10 minutes of normal load on this Mac (median 17,974 pages): 25
    under the warn line, 1.25 s in all, one of them at 911 pages (the reserve itself). A 10-s
    sample landing on such a dip must not page; staying under the line for 3 samples does."""
    dip = _sample(taken_at=1020.0, free=911 * PAGE)
    calm = [_sample(taken_at=1000.0, free=6000 * PAGE), _sample(taken_at=1010.0, free=5000 * PAGE)]
    assert "free" not in {f.key for f in detect.sample_findings(dip, TH, calm)}
    low = [_sample(taken_at=1000.0, free=1200 * PAGE), _sample(taken_at=1010.0, free=1500 * PAGE)]
    [f] = [f for f in detect.sample_findings(dip, TH, low) if f.key == "free"]
    assert f.severity == SEVERITY_CRITICAL and "911 pages" in f.summary


@pytest.mark.parametrize("used_gb, free_gb, sev, pct", [
    (91, 9, SEVERITY_WARNING, "91.0%"),
    (96, 4, SEVERITY_CRITICAL, "96.0%"),
])
def test_data_volume_percent_full_names_the_volume_and_percent(used_gb, free_gb, sev, pct):
    s = _sample(data_used=used_gb * GIB, data_free=free_gb * GIB)
    [f] = [f for f in detect.sample_findings(s, TH) if f.key == "data_volume"]
    assert f.kind == "disk" and f.severity == sev
    assert "/System/Volumes/Data" in f.summary and pct in f.summary and f"{free_gb:.1f} GB free" in f.summary


def test_data_volume_percent_is_used_over_used_plus_available_like_df():
    """APFS volumes share one container: df's Capacity is Used / (Used + Available), not Used /
    Size. ``df -k /System/Volumes/Data`` on 2026-09-30: Size 482797652, Used 427549236,
    Available 20400976 KB → 96% (95.4 exact); Used / Size would read 88.6% and never warn."""
    s = _sample(data_used=427549236 * 1024, data_free=20400976 * 1024)
    assert round(s.data_used_pct, 1) == 95.4
    assert detect.level("data_used_pct", s.data_used_pct, TH) == SEVERITY_CRITICAL


def test_free_pages_exclude_the_speculative_ones_like_vm_stat():
    """host_statistics64's free_count INCLUDES speculative pages; vm_stat's "Pages free" (and so
    the free threshold) is free_count - speculative_count, and reclaimable counts them once."""
    from types import SimpleNamespace

    stats = SimpleNamespace(free_count=37622, speculative_count=34058, inactive_count=419722,
                            purgeable_count=698, wire_count=500000)
    mem = probe.vm_page_bytes(stats, PAGE)
    assert mem["free"] == 3564 * PAGE and mem["speculative"] == 34058 * PAGE
    assert mem["reclaimable"] == (3564 + 34058 + 419722 + 698) * PAGE


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
    rec = alert.send("t", "b", cfg, severity=SEVERITY_CRITICAL, key="k", run=lambda *a, **k: pytest.fail("tg must not run"))
    assert rec["outcome"] == "dry_run" and rec["via"] == "tg-ctl alert" and rec["message_id"] is None
    assert rec["argv"][:3] == ["tg-ctl", "alert", "send"] and rec["fallback_argv"][:3] == ["tg", "--tag", "problem"]


def _completed(argv, code, out="", err=""):
    return subprocess.CompletedProcess(list(argv), code, out, err)


OLD_TG_CTL_USAGE = "Usage:\n  tg-ctl start [--pane %N]\n  tg-ctl run             # foreground\n  tg-ctl omp-live send <pid> …\n"


def test_send_goes_through_tg_ctl_alert_with_severity_key_and_html_body():
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return _completed(argv, 0, "OK tg#4210 sessions=1 (Supervisor (pid 7))\n")

    rec = alert.send("sysmon CRITICAL on mac: load", "<b>CRITICAL</b> load 38", _cfg(), severity=SEVERITY_CRITICAL,
                     key="load;swap", run=run)
    assert calls == [[
        "tg-ctl", "alert", "send", "--source", "sysmon", "--severity", "critical", "--key", "load;swap",
        "--title", "sysmon CRITICAL on mac: load", "--html", "--text", "<b>CRITICAL</b> load 38",
    ]]
    assert alert.flag_value(calls[0], "--severity") == "critical" and alert.flag_value(calls[0], "--key") == "load;swap"
    assert calls[0][-2:] == ["--text", "<b>CRITICAL</b> load 38"] and "--html" in calls[0]
    assert rec["outcome"] == "sent" and rec["via"] == "tg-ctl alert" and rec["message_id"] == 4210
    assert rec["severity"] == "critical" and rec["key"] == "load;swap" and "fallback_reason" not in rec


def test_send_falls_back_to_tg_only_for_an_older_or_missing_tg_ctl():
    for first, reason in ((_completed(["tg-ctl"], 1, "", OLD_TG_CTL_USAGE), "tg-ctl without `alert`"), (None, "tg-ctl not installed")):
        calls = []

        def run(argv, first=first, **kw):
            calls.append(argv)
            if argv[0] == "tg-ctl":
                if first is None:
                    raise FileNotFoundError(argv[0])
                return first
            return _completed(argv, 0, "OK tg#9\n")

        rec = alert.send("t", "b", _cfg(), severity=alert.SEVERITY_INFO, key="resolved:x", run=run)
        assert [c[0] for c in calls] == ["tg-ctl", "tg"]
        assert calls[1] == ["tg", "--tag", "report", "--subagent", "sysmon", "--title", "t", "--format", "html", "b"]
        assert rec["outcome"] == "sent" and rec["via"] == "tg" and rec["message_id"] == 9 and rec["fallback_reason"] == reason


@pytest.mark.parametrize("failure", [
    _completed(["tg-ctl"], 1, "FAILED tg#? sessions=0\n", "tg-ctl alert: Telegram post failed (tg exit 1): no creds\n"),
    _completed(["tg-ctl"], -9, "", ""),  # killed after it may have posted
    _completed(["tg-ctl"], 1, "", "Usage:\n  tg-ctl run\n  tg-ctl alert send …\n"),  # a NEW tg-ctl's usage (bad argv)
    subprocess.TimeoutExpired(["tg-ctl"], 60),
    OSError("broken pipe"),
])
def test_any_other_tg_ctl_failure_is_an_error_and_never_a_second_post(failure):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        if isinstance(failure, Exception):
            raise failure
        return failure

    rec = alert.send("t", "b", _cfg(), severity=SEVERITY_WARNING, key="k", run=run)
    assert [c[0] for c in calls] == ["tg-ctl"]
    assert rec["outcome"] == "error" and rec["via"] == "tg-ctl alert" and rec["message_id"] is None


def test_send_rejects_an_unknown_severity_before_running_anything():
    with pytest.raises(ValueError):
        alert.send("t", "b", _cfg(), severity="notice", key="k", run=lambda *a, **k: pytest.fail("must not run"))


def test_alert_tg_ctl_bin_and_tg_bin_name_the_senders():
    rec = alert.send("t", "b", _cfg(dry_run=True, tg_ctl_bin="/opt/tg-ctl", tg_bin="/opt/tg"), severity=SEVERITY_WARNING, key="k")
    assert rec["argv"][0] == "/opt/tg-ctl" and rec["fallback_argv"][0] == "/opt/tg"
    rec = alert.send("t", "b", _cfg(dry_run=True, tg_ctl_bin=""), severity=SEVERITY_WARNING, key="k")
    assert rec["argv"][0] == "tg-ctl"


def test_both_senders_missing_is_one_error_record():
    def run(argv, **kw):
        raise FileNotFoundError(argv[0])

    rec = alert.send("t", "b", _cfg(), severity=SEVERITY_WARNING, key="k", run=run)
    assert rec["outcome"] == "error" and rec["via"] == "tg" and rec["fallback_reason"] == "tg-ctl not installed"
    assert rec["detail"].startswith("tg: cannot run") and rec["message_id"] is None


@pytest.mark.parametrize("exc", [
    PermissionError(errno.EACCES, "Permission denied"),
    NotADirectoryError(errno.ENOTDIR, "Not a directory"),
    OSError(errno.ENOEXEC, "Exec format error"),
])
def test_an_exec_time_refusal_proves_tg_ctl_never_ran_and_falls_back_to_tg(exc):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        if argv[0] == "tg-ctl":
            raise exc
        return _completed(argv, 0, "OK tg#3\n")

    rec = alert.send("t", "b", _cfg(), severity=SEVERITY_WARNING, key="k", run=run)
    assert [c[0] for c in calls] == ["tg-ctl", "tg"]
    assert rec["outcome"] == "sent" and rec["via"] == "tg" and rec["fallback_reason"] == "tg-ctl not installed"


def test_unsupported_alert_subcommand_accepts_a_lowercase_usage_head():
    assert alert.unsupported_alert_subcommand(_completed(["tg-ctl"], 1, "", OLD_TG_CTL_USAGE.replace("Usage:", "usage:", 1)))
    assert not alert.unsupported_alert_subcommand(_completed(["tg-ctl"], 1, "", "usage: tg-ctl run\n  tg-ctl alert send\n"))
    assert not alert.unsupported_alert_subcommand(_completed(["tg-ctl"], 2, "", OLD_TG_CTL_USAGE))


def test_a_sent_alert_without_an_ok_line_keeps_outcome_sent_and_no_message_id():
    rec = alert.send("t", "b", _cfg(), severity=SEVERITY_WARNING, key="k", run=lambda argv, **k: _completed(argv, 0, "sent, no id\n"))
    assert rec["outcome"] == "sent" and rec["message_id"] is None and rec["detail"] == "sent, no id"


# ── alert.route: subscriber (rig-cli#700) ─────────────────────────────────────────────────
def _holder_run(calls, code=0, out="OK holder s-1 pid 11\n", err=""):
    def run(argv, **kw):
        calls.append(argv)
        return _completed(argv, code, out, err)
    return run


def _before_text(argv):
    """The flags before the body: ``--text`` is the last flag, its value the last item."""
    return argv[:argv.index("--text")]


def test_route_subscriber_delivers_to_the_holder_only():
    calls = []
    rec = alert.send("sysmon WARNING on home: swap", "<b>WARNING</b> swap", _cfg(route="subscriber"),
                     severity=SEVERITY_WARNING, key="swap", run=_holder_run(calls))
    assert len(calls) == 1
    argv = calls[0]
    assert argv[:3] == ["tg-ctl", "alert", "send"] and "--to-holder" in _before_text(argv)
    assert argv[-2:] == ["--text", "<b>WARNING</b> swap"]  # the body stays intact and last
    assert alert.flag_value(argv, "--source") == "sysmon" and alert.flag_value(argv, "--key") == "swap"
    assert rec["via"] == "holder" and rec["outcome"] == "sent" and rec["message_id"] is None
    assert not any(c[0] == "tg" or "--to-holder" not in c for c in calls)


@pytest.mark.parametrize("code,outcome,vacant,reason", [
    (3, "undelivered", True, "no subscriber"),
    (5, "deferred", None, "holder suspended"),
    (1, "error", None, None),
])
def test_route_subscriber_maps_exit_codes(code, outcome, vacant, reason):
    calls = []
    rec = alert.send("t", "b", _cfg(route="subscriber"), severity=SEVERITY_CRITICAL, key="k",
                     run=_holder_run(calls, code, "VACANT sysmon\n" if code == 3 else "", "boom" if code != 3 else ""))
    assert len(calls) == 1  # no fallback inside send: the daemon decides it
    assert rec["via"] == "holder" and rec["outcome"] == outcome and rec.get("vacant") is vacant
    assert rec.get("reason") == reason and rec["returncode"] == code
    if outcome in ("deferred", "undelivered"):
        assert rec["detail"] and len(rec["detail"]) <= 300


def test_route_subscriber_old_tg_ctl_is_an_error_not_silence():
    calls = []
    rec = alert.send("t", "b", _cfg(route="subscriber"), severity=SEVERITY_CRITICAL, key="k",
                     run=_holder_run(calls, 1, "", "tg-ctl alert: unknown flag: --to-holder"))
    assert rec["outcome"] == "error" and "unknown flag" in rec["detail"] and len(calls) == 1


def test_route_subscriber_missing_tg_ctl_is_an_error():
    def run(argv, **kw):
        raise FileNotFoundError(errno.ENOENT, "No such file", argv[0])

    rec = alert.send("t", "b", _cfg(route="subscriber"), severity=SEVERITY_CRITICAL, key="k", run=run)
    assert rec["via"] == "holder" and rec["outcome"] == "error" and "cannot run" in rec["detail"]


def test_route_subscriber_dry_run_runs_nothing():
    rec = alert.send("t", "<b>b</b>", _cfg(route="subscriber", dry_run=True), severity=SEVERITY_WARNING, key="k",
                     run=lambda *a, **k: pytest.fail("must not run"))
    assert rec["via"] == "holder" and rec["outcome"] == "dry_run"
    assert "--to-holder" in _before_text(rec["argv"]) and rec["argv"][-1] == "<b>b</b>"


def test_send_telegram_is_the_tg_ctl_alert_path():
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return _completed(argv, 0, "OK tg#4210 sessions=1 (Supervisor (pid 7))\n")

    rec = alert.send_telegram("sysmon CRITICAL on mac: load", "<b>CRITICAL</b> load 38", _cfg(route="subscriber"),
                              severity=SEVERITY_CRITICAL, key="load;swap", run=run)
    assert calls == [[
        "tg-ctl", "alert", "send", "--source", "sysmon", "--severity", "critical", "--key", "load;swap",
        "--title", "sysmon CRITICAL on mac: load", "--html", "--text", "<b>CRITICAL</b> load 38",
    ]]
    assert rec["outcome"] == "sent" and rec["via"] == "tg-ctl alert" and rec["message_id"] == 4210


def test_holder_argv_puts_to_holder_among_the_flags():
    argv = alert.holder_argv("tg-ctl", "--text", "--looks-like-a-flag", "warning", "k")
    assert argv[-3:] == ["--to-holder", "--text", "--looks-like-a-flag"] and argv.count("--to-holder") == 1
    assert alert.flag_value(argv, "--title") == "--text"  # a title that looks like the body flag


def test_route_subscriber_a_hung_tg_ctl_is_a_bounded_error():
    def run(argv, **kw):
        assert kw["timeout"] == alert.HOLDER_SEND_TIMEOUT_S
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    rec = alert.send("t", "b", _cfg(route="subscriber"), severity=SEVERITY_CRITICAL, key="k", run=run)
    assert rec["via"] == "holder" and rec["outcome"] == "error" and "timed out" in rec["detail"]


def test_a_sysmon_json_from_before_the_route_rename_stays_loud(tmp_path):
    """A daemon reading a ``route: agents`` sysmon.json (rendered before rig-cli#700) alerts via
    Telegram instead of raising out of the tick."""
    path = tmp_path / "sysmon.json"
    path.write_text(json.dumps({"alert": {"route": "agents"}}))
    assert config.load(path)["alert"]["route"] == "telegram"


def test_route_rejects_an_unknown_value_before_running_anything():
    with pytest.raises(ValueError):
        alert.send("t", "b", _cfg(route="pager"), severity=SEVERITY_WARNING, key="k",
                   run=lambda *a, **k: pytest.fail("must not run"))


def test_worst_severity_and_batch_key():
    assert alert.worst_severity([_f("a", SEVERITY_WARNING), _f("b")]) == SEVERITY_CRITICAL
    assert alert.worst_severity([]) == SEVERITY_WARNING
    assert alert.batch_key(["b", "a"]) == alert.batch_key(["a", "b"]) == "a;b"
    assert alert.batch_key(["a"], prefix="resolved:") == "resolved:a"
    long_a, long_b = alert.batch_key(["k" * 200, "x"]), alert.batch_key(["k" * 200, "y"])
    assert len(long_a) == len(long_b) == alert.KEY_MAX and long_a != long_b and long_a.startswith("k" * 100)


def test_resolved_notice_goes_out_as_an_info_alert_keyed_by_the_cleared_findings(tmp_path):
    hot, calm = _sample(wired=int(23 * GIB)), _sample()
    mon, run = _monitor(tmp_path, [hot, calm, calm])
    mon.tick(1000.0)
    mon.tick(1010.0)
    mon.tick(1010.0 + alert.RESOLVE_AFTER_S + 1)
    sends = _tg_sends(run)
    assert [alert.flag_value(a, "--severity") for a in sends] == [SEVERITY_CRITICAL, alert.SEVERITY_INFO]
    assert alert.flag_value(sends[0], "--key") == "wired" and alert.flag_value(sends[1], "--key") == "resolved:wired"
    assert alert.flag_value(sends[1], "--title").startswith("sysmon resolved on ")


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
    return [c for c in run.calls if c and (c[0] == "tg" or c[1:3] == ["alert", "send"])]


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


def test_a_big_storm_writes_its_incident_once_per_heal_batch_not_once_per_offender(tmp_path, monkeypatch):
    """Each heal result used to rewrite incident.json, which lists every offender: a 684-process
    coalition made that 684 writes of a 684-offender file per tick (~10 s of the daemon's loop)."""
    census = _Census([_proc(70, 1, comm="bash"), *[_proc(1000 + i, 70, comm="sleep") for i in range(320)]])
    mon, band = _heal_monitor(tmp_path, census)
    saves = []
    real = store.save_incident
    monkeypatch.setattr(store, "save_incident", lambda d, inc: saves.append(len(inc.get("heal") or [])) or real(d, inc))
    mon.tick(1000.0)
    assert len(band.bg) == 320  # every storm member was throttled ...
    assert len(saves) <= 4 and saves[-1] >= 320  # ... and the incident was written a handful of times


# ── a full data volume: the reclaimable-candidates report (rig-cli#645; report only) ──────
T0 = 1_800_000_000.0
DAY = 86400.0
FULL = dict(data_used=96 * GIB, data_free=4 * GIB)
APPLE_DF = {"images": {"total": 15, "active": 2, "sizeInBytes": 4452966400, "reclaimable": 3679227904}}


class _Host:
    """The commands a reclaim scan runs, answered from tables. Every argv and its timeout is
    recorded, so a test sees exactly what ran — and that nothing that removes anything did."""

    def __init__(self, du_kb, *, binaries=("container",), docker_df="", slow=()):
        self.du_kb, self.binaries, self.docker_df, self.slow = du_kb, set(binaries), docker_df, set(slow)
        self.calls = []

    def which(self, name):
        return f"/fake/bin/{name}" if name in self.binaries else None

    def __call__(self, argv, **kw):
        import subprocess

        self.calls.append((list(argv), kw.get("timeout")))
        name = os.path.basename(argv[0])
        if name == "du":
            if argv[-1] in self.slow:
                raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
            kb = self.du_kb.get(argv[-1])
            return subprocess.CompletedProcess(argv, 0 if kb else 1, f"{kb}\t{argv[-1]}\n" if kb else "", "")
        if name == "container":
            return subprocess.CompletedProcess(argv, 0, json.dumps(APPLE_DF), "")
        if name == "docker":
            return subprocess.CompletedProcess(argv, 0, self.docker_df, "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def ran(self, name):
        return [a for a, _ in self.calls if os.path.basename(a[0]) == name]


def _tree(tmp_path):
    """Two dev roots holding rig worktrees of different ages, plus a review-cli job cache."""

    def worktree(root, repo, name, idle_days, git_active_days=None):
        d = tmp_path / root / repo / ".worktrees" / name
        d.mkdir(parents=True)
        if git_active_days is not None:  # a checkout whose git index moved recently is in use
            admin = tmp_path / root / repo / ".git" / "worktrees" / name
            admin.mkdir(parents=True)
            (admin / "index").write_text("")
            os.utime(admin / "index", (T0 - git_active_days * DAY,) * 2)
            (d / ".git").write_text(f"gitdir: {admin}\n")
        os.utime(d, (T0 - idle_days * DAY,) * 2)
        return str(d)

    cache = tmp_path / "cache" / "review-cli" / "jobs"
    cache.mkdir(parents=True)
    return {
        "old_big": worktree("xp", "app", "old-big", 30),
        "old_small": worktree("xp", "app", "old-small", 10),
        "fresh": worktree("xp", "app", "fresh", 1),
        "busy": worktree("work", "lib", "busy", 40, git_active_days=0.5),
        "cache": str(cache),
    }


def _du_table(paths):
    return {paths["old_big"]: 3_000_000, paths["old_small"]: 20_000, paths["fresh"]: 5_000_000,
            paths["busy"]: 4_000_000, paths["cache"]: 70_000}


def _reclaim_cfg(tmp_path, **alert_over):
    cfg = config.load(None)
    cfg["state_dir"] = str(tmp_path / "state")
    cfg["events"]["enabled"] = False
    cfg["alert"].update(alert_over)
    cfg["reclaim"].update(
        worktree_roots=[str(tmp_path / "xp"), str(tmp_path / "work"), str(tmp_path / "absent")],
        cache_dirs=[str(tmp_path / "cache" / "review-cli" / "jobs"), str(tmp_path / "absent-cache")],
    )
    return cfg


def _disk_monitor(tmp_path, host, samples, **alert_over):
    it = iter(samples)
    return daemon.Monitor(_reclaim_cfg(tmp_path, **alert_over), sampler=lambda now=None: next(it),
                          censor=lambda: [], run=host, which=host.which)


def test_critical_data_volume_alert_lists_reclaimable_candidates_by_size_and_deletes_nothing(tmp_path, monkeypatch):
    import shutil

    paths = _tree(tmp_path)
    host = _Host(_du_table(paths))
    mon = _disk_monitor(tmp_path, host, [_sample(**FULL)])
    removed = []
    for owner, name in ((os, "remove"), (os, "unlink"), (os, "rmdir"), (os, "removedirs"), (shutil, "rmtree"),
                        (shutil, "move"), (Path, "unlink"), (Path, "rmdir")):
        monkeypatch.setattr(owner, name, lambda *a, _n=name, **k: removed.append(_n))
    mon.tick(T0)
    monkeypatch.undo()

    report = store.load_incident(mon.state_dir, mon.memory["open_incident"])["reclaimable"]
    assert [(c["kind"], c["path"]) for c in report["candidates"]] == [
        ("container-images", "container"), ("worktree", paths["old_big"]),
        ("review-cache", paths["cache"]), ("worktree", paths["old_small"]),
    ]  # the fresh worktree and the one whose git index moved today are in use, never listed
    [tg] = host.ran("tg-ctl")
    body = tg[-1]
    order = [body.index(s) for s in ("3.4 GB", f"2.9 GB worktree {paths['old_big']}",
                                     f"68 MB review-cli job cache {paths['cache']}", "20 MB worktree")]
    assert order == sorted(order) and "idle 30 d" in body
    assert "deleted nothing" in body and paths["fresh"] not in body and paths["busy"] not in body
    # nothing was removed: no removal call, no removing command, every directory still there
    assert removed == []
    assert not [a for a, _ in host.calls
                if os.path.basename(a[0]) in ("rm", "rmdir", "unlink") or {"rm", "rmi", "prune", "delete"} & set(a[1:3])]
    assert all(Path(p).is_dir() for p in paths.values())


def _pages(host):
    """The problem alerts sent (``tg-ctl alert send … --severity warning|critical``), not the resolved lines."""
    return [a for a in host.ran("tg-ctl") if a[1:3] == ["alert", "send"] and alert.flag_value(a, "--severity") != alert.SEVERITY_INFO]


def test_a_full_disk_pages_once_per_six_hours_per_level_always_with_the_candidates(tmp_path):
    """Owner decision 2026-09-30: a full disk stays full until someone cleans up, so it must not
    page every 30-minute cooldown. Each level pages at most once per ``disk_cooldown_minutes``
    (6 h), clearing and coming back inside that window does not page again, an escalation to a
    level not yet sent does, and every disk page lists the reclaim candidates."""
    paths = _tree(tmp_path)
    host = _Host(_du_table(paths))
    warn, crit, calm = _sample(data_used=91 * GIB, data_free=9 * GIB), _sample(**FULL), _sample()
    hour = 3600.0
    ticks = [
        (T0, warn, 1),                     # first warning: pages
        (T0 + 10, crit, 2),                # escalation to critical: pages
        (T0 + 10 + 30 * 60, crit, 2),      # the old 30-min re-page: silent now
        (T0 + 2 * hour, calm, 2),          # clears ...
        (T0 + 2 * hour + 600, calm, 2),    # ... for 10 min (one "resolved" line, not a page)
        (T0 + 3 * hour, crit, 2),          # back at critical inside 6 h: silent
        (T0 + 3 * hour + 10, warn, 2),     # warning was paged at T0: silent
        (T0 + 10 + 6 * hour - 1, crit, 2), # a second short of 6 h: silent
        (T0 + 10 + 6 * hour, crit, 3),     # 6 h after the critical page: pages again
    ]
    mon = _disk_monitor(tmp_path, host, [s for _, s, _ in ticks], min_severity="warning")
    for now, _, pages in ticks:
        mon.tick(now)
        assert len(_pages(host)) == pages, now - T0
    assert len(host.ran("tg-ctl")) == 4  # the three pages and the one resolved line
    for page in _pages(host):
        body = page[-1]
        assert f"worktree {paths['old_big']}" in body and "review-cli job cache" in body
        assert "unused Apple container images" in body and "deleted nothing" in body
    assert all(Path(p).is_dir() for p in paths.values())


def test_the_scan_is_reused_inside_the_alert_cooldown(tmp_path):
    paths = _tree(tmp_path)
    host = _Host(_du_table(paths))
    warn, crit = _sample(data_used=91 * GIB, data_free=9 * GIB), _sample(**FULL)
    mon = _disk_monitor(tmp_path, host, [warn, crit], min_severity="warning")
    mon.tick(T0)
    scanned = len(host.ran("du"))
    assert scanned > 0 and len(_pages(host)) == 1
    mon.tick(T0 + 10)  # the escalation pages with the report the warning scanned 10 s ago
    assert len(host.ran("du")) == scanned and len(_pages(host)) == 2


def test_a_memory_finding_keeps_its_own_cooldown_beside_a_quiet_disk():
    cfg = _cfg()
    disk = Finding(key="data_volume", kind="disk", severity=SEVERITY_CRITICAL, summary="full")
    due, _, st = alert.decide([disk, _f()], {}, cfg, 0.0)
    assert [f.key for f in due] == ["data_volume", "wired"]
    due, _, st = alert.decide([disk, _f()], st, cfg, 30 * 60.0)
    assert [f.key for f in due] == ["wired"]
    due, _, st = alert.decide([disk, _f()], st, cfg, 6 * 3600.0)
    assert [f.key for f in due] == ["data_volume", "wired"]


def test_a_disk_page_held_by_the_hourly_budget_is_sent_later_and_each_volume_has_its_own_window():
    """A disk page the hourly budget holds must not count as sent (else a full disk stays silent
    for 6 h), and the data and swap volumes keep separate windows."""
    cfg = _cfg(max_per_hour=1)
    data = Finding(key="data_volume", kind="disk", severity=SEVERITY_CRITICAL, summary="full")
    swap = Finding(key="disk", kind="disk", severity=SEVERITY_CRITICAL, summary="swap volume full")
    due, _, st = alert.decide([_f()], {}, cfg, 0.0)
    assert [f.key for f in due] == ["wired"]  # the budget is spent
    due, _, st = alert.decide([_f(), data], st, cfg, 60.0)
    assert due == []  # held
    due, _, st = alert.decide([data], st, cfg, 3600.0)
    assert [f.key for f in due] == ["data_volume"]  # sent once the budget frees up
    due, _, st = alert.decide([_f(), data, swap], st, cfg, 2 * 3600.0)
    assert [f.key for f in due] == ["wired", "disk"]  # the swap volume's first page is its own


def test_a_du_that_times_out_is_counted_unmeasured_and_every_command_is_bounded(tmp_path):
    from riglib.sysmon_daemon import reclaim

    paths = _tree(tmp_path)
    host = _Host(_du_table(paths), binaries=(), slow={paths["old_big"]})
    report = reclaim.scan(_reclaim_cfg(tmp_path), T0, run=host, which=host.which)
    assert [c["path"] for c in report["candidates"]] == [paths["cache"], paths["old_small"]]
    assert report["unmeasured"] == 1
    assert {os.path.basename(a[0]) for a, _ in host.calls} == {"du"}  # no container runtime: none asked
    assert all(t is not None and 0 < t <= reclaim.COMMAND_TIMEOUT_S for _, t in host.calls)


def test_docker_images_come_from_system_df_and_an_unsized_answer_is_skipped(tmp_path):
    from riglib.sysmon_daemon import reclaim

    cfg = _reclaim_cfg(tmp_path)
    cfg["reclaim"].update(worktree_roots=[], cache_dirs=[])
    rows = [{"Type": "Images", "TotalCount": "5", "Active": "1", "Size": "2.4GB", "Reclaimable": "1.2GB (50%)"},
            {"Type": "Containers", "TotalCount": "2", "Active": "0", "Size": "10MB", "Reclaimable": "10MB (100%)"}]
    host = _Host({}, binaries=("docker",), docker_df="\n".join(json.dumps(r) for r in rows))
    [c] = reclaim.scan(cfg, T0, run=host, which=host.which)["candidates"]
    assert (c["kind"], c["size_bytes"]) == ("docker-images", 1_200_000_000)
    # the Apple-container `docker` front prints a table with N/A sizes: nothing to report
    table = "TYPE  TOTAL  ACTIVE  SIZE  RECLAIMABLE\nImages  15  15  N/A  0B\n"
    host = _Host({}, binaries=("docker",), docker_df=table)
    assert reclaim.scan(cfg, T0, run=host, which=host.which)["candidates"] == []


def test_a_full_disk_does_not_force_a_census_every_tick(tmp_path):
    """A full disk is no process problem: unlike a memory or storm finding it must not turn the
    ps census on for every sample while it lasts (days, until someone cleans up). On APFS the
    data and swap volumes share one container's free space, so both disk findings go critical
    together."""
    census = _Census([_proc(70, 1, comm="bash")])
    host = _Host({}, binaries=())
    full = _sample(disk_free=1 * GIB, **FULL)
    mon = daemon.Monitor(_reclaim_cfg(tmp_path), sampler=lambda now=None: full, censor=census,
                         run=host, which=host.which)
    for i in range(3):
        mon.tick(T0 + 10 * i)
    assert {f.key for f in mon.findings if f.severity == SEVERITY_CRITICAL} == {"data_volume", "disk"}
    assert census.calls == 1 and mon.incident is not None


def test_test_alert_keys_every_invocation_uniquely_so_the_throttle_never_swallows_a_repeat(tmp_path, capsys, monkeypatch):
    from riglib.sysmon_daemon import cli

    monkeypatch.setattr(cli.time, "time_ns", lambda: 7)  # a frozen clock must not collide keys
    keys = []
    for _ in range(2):
        assert cli.main(["test-alert", "--dry-run", "--config", str(tmp_path / "absent.json")]) == 0
        rec = json.loads(capsys.readouterr().out)
        assert rec["outcome"] == "dry_run" and rec["key"].startswith("test-")
        keys.append(rec["key"])
    assert keys[0] != keys[1]


# ── route subscriber through the monitor (rig-cli#700) ────────────────────────────────────
HOLDER_BOUND = json.dumps({"source": "sysmon", "state": "bound", "request": None,
                           "holder": {"sessionId": "s-1", "pid": 11, "title": "h", "since": 1}})
HOLDER_VACANT = json.dumps({"source": "sysmon", "state": "vacant", "holder": None, "request": None})


class _Script:
    """A runner answering by argv shape; ``to_holder`` / ``holder`` / ``window`` are mutable per tick."""

    def __init__(self, to_holder=0, holder=3, window=None, to_holder_err=""):
        self.to_holder, self.holder, self.window, self.to_holder_err = to_holder, holder, window, to_holder_err
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if argv[1:3] == ["alert", "holder"]:
            return _completed(argv, self.holder, HOLDER_BOUND if self.holder == 0 else HOLDER_VACANT)
        if argv[1:3] == ["alert", "send"]:
            if "--to-holder" in argv:
                return _completed(argv, self.to_holder, "OK holder\n" if self.to_holder == 0 else "", self.to_holder_err)
            return _completed(argv, 0, "OK tg#5\n")
        if argv[1:3] == ["omp-live", "status"]:
            return _completed(argv, 0, "[]")
        if argv[1] == "list-panes":
            return _completed(argv, 0, self.window) if self.window else _completed(argv, 1, "", "can't find window")
        if argv[1] == "has-session":
            return _completed(argv, 0)
        if argv[1] == "new-window":
            self.window = "@5 4242\n"
            return _completed(argv, 0, "@5 4242\n")
        return _completed(argv, 0)

    def holder_sends(self):
        return [c for c in self.calls if c[1:3] == ["alert", "send"] and "--to-holder" in c]

    def telegram_sends(self):
        return [c for c in self.calls if c[0] == "tg" or (c[1:3] == ["alert", "send"] and "--to-holder" not in c)]

    def ran(self, verb):
        return [c for c in self.calls if c[0] == "tmux" and c[1] == verb]


HOT, WARM = _sample(wired=int(23 * GIB)), _sample(wired=int(19 * GIB))


def _sub_monitor(tmp_path, samples, run, **alert_over):
    cfg = config.load(None)
    cfg["state_dir"] = str(tmp_path / "state")
    cfg["events"]["enabled"] = False
    cfg["heal"]["enabled"] = False
    cfg["alert"].update(route="subscriber", **alert_over)
    it = iter(samples)
    return daemon.Monitor(cfg, sampler=lambda now=None: next(it), censor=lambda: [], run=run)


def test_subscriber_route_bound_holder_gets_the_alert_and_no_telegram(tmp_path):
    run = _Script(to_holder=0, holder=0)
    mon = _sub_monitor(tmp_path, [HOT], run)
    mon.tick(1000.0)
    assert len(run.holder_sends()) == 1 and run.telegram_sends() == []
    assert run.ran("new-window") == run.ran("new-session") == [] and not [c for c in run.calls if c[1:3] == ["alert", "holder"]]
    assert mon.incident["alerts"][-1]["via"] == "holder" and mon.incident["alerts"][-1]["outcome"] == "sent"


def test_subscriber_route_vacant_critical_spawns_once(tmp_path):
    run = _Script(to_holder=3, holder=3)
    mon = _sub_monitor(tmp_path, [HOT, HOT], run)
    mon.tick(1000.0)
    assert len(run.ran("new-window")) == 1 and mon.memory["handler"]["last_spawn_at"] == 1000.0
    assert run.telegram_sends() == []  # vacant, never spawned before: queued, not posted
    mon.tick(1010.0)
    assert len(run.ran("new-window")) == 1
    assert mon.incident["handler"][0]["outcome"] == "spawned"
    assert mon.memory["recent_handler"][0]["window"] == "main:sysmon-handler"


def test_subscriber_route_vacant_fresh_warning_neither_spawns_nor_posts(tmp_path):
    run = _Script(to_holder=3, holder=3)
    mon = _sub_monitor(tmp_path, [WARM], run, min_severity="warning")
    mon.tick(1000.0)
    assert run.ran("new-window") == [] and run.telegram_sends() == []
    rec = mon.memory["recent_alerts"][-1]
    assert rec["outcome"] == "undelivered" and rec["reason"] == "no subscriber"
    assert list(mon.memory["handler"]["pending"]["keys"]) == ["wired"]


def test_vacant_critical_is_queued_and_reaches_the_spawned_handler(tmp_path):
    run = _Script(to_holder=3, holder=3)
    mon = _sub_monitor(tmp_path, [HOT, HOT], run)
    mon.tick(1000.0)
    assert len(run.ran("new-window")) == 1 and "wired" in mon.memory["handler"]["pending"]["keys"]
    run.holder, run.to_holder = 0, 0
    mon.tick(1000.0 + 120)
    replays = run.holder_sends()[1:]
    assert len(replays) == 1 and alert.flag_value(replays[0], "--key") == "wired"
    assert mon.memory["handler"]["pending"]["keys"] == {} and run.telegram_sends() == []


def test_deferred_alert_is_redelivered_when_the_holder_returns(tmp_path):
    run = _Script(to_holder=5, holder=0)
    mon = _sub_monitor(tmp_path, [HOT, HOT, HOT], run)
    mon.tick(1000.0)
    rec = mon.incident["alerts"][-1]
    assert rec["outcome"] == "deferred" and rec["reason"] == "holder suspended"
    assert "wired" in mon.memory["handler"]["pending"]["keys"]
    mon.tick(1000.0 + 61)  # holder bound again, the replay is deferred again
    assert "wired" in mon.memory["handler"]["pending"]["keys"]
    run.to_holder = 0
    mon.tick(1000.0 + 122)
    sent = [c for c in run.holder_sends()[2:]]
    assert len(sent) == 1 and alert.flag_value(sent[0], "--key") == "wired"
    assert mon.memory["handler"]["pending"]["keys"] == {} and run.telegram_sends() == []


def test_failed_start_posts_to_telegram_once(tmp_path):
    run = _Script(to_holder=3, holder=3)
    mon = _sub_monitor(tmp_path, [HOT] * 4, run, cooldown_minutes=1)
    mon.tick(1000.0)
    assert run.telegram_sends() == []
    mon.tick(1000.0 + 300)  # nobody took the slot within 5 min
    posts = run.telegram_sends()
    assert len(posts) == 2  # the due re-alert falls back (window older than 5 min), then the notice
    assert sum("sysmon handler did not start" in alert.flag_value(c, "--title") for c in posts) == 1
    mon.tick(1000.0 + 420)  # the next due CRITICAL takes the fallback; the notice is not repeated
    posts = run.telegram_sends()
    assert sum("sysmon handler did not start" in alert.flag_value(c, "--title") for c in posts) == 1
    assert len(posts) == 3 and mon.incident["alerts"][-1]["fallback"] == "telegram"


def test_failed_start_after_warning_spawn_never_posts(tmp_path):
    run = _Script(to_holder=3, holder=3)
    mon = _sub_monitor(tmp_path, [WARM] * 4, run, min_severity="warning", cooldown_minutes=1)
    mon.tick(1000.0)
    mon.tick(1000.0 + 15 * 60)
    assert len(run.ran("new-window")) == 1 and mon.memory["handler"]["spawn_severity"] == SEVERITY_WARNING
    mon.tick(1000.0 + 20 * 60)
    assert mon.memory["handler"]["last_spawn_ok"] is False
    assert any(r.get("event") == "startup" and r["ok"] is False for r in mon.memory["recent_handler"])
    rec = mon.memory["recent_alerts"][-1]
    assert rec["outcome"] == "undelivered" and rec["reason"] == "warning-no-telegram"
    assert run.telegram_sends() == []


def test_resolved_notice_never_falls_back(tmp_path):
    run = _Script(to_holder=0, holder=0)
    calm = _sample()
    mon = _sub_monitor(tmp_path, [HOT, calm, calm], run)
    mon.tick(1000.0)
    run.to_holder, run.holder = 3, 3
    mon.tick(1010.0)
    mon.tick(1010.0 + alert.RESOLVE_AFTER_S + 61)
    infos = [c for c in run.holder_sends() if alert.flag_value(c, "--severity") == alert.SEVERITY_INFO]
    assert len(infos) == 1 and run.telegram_sends() == []
    assert "resolved:wired" not in (mon.memory["handler"].get("pending") or {}).get("keys", {})


def test_an_undelivered_resolved_notice_gives_its_budget_back(tmp_path):
    run = _Script(to_holder=0, holder=0)
    calm = _sample()
    mon = _sub_monitor(tmp_path, [HOT, calm, calm], run)
    mon.tick(1000.0)
    run.to_holder, run.holder = 3, 3
    mon.tick(1010.0)
    mon.tick(1010.0 + alert.RESOLVE_AFTER_S + 61)
    assert mon.memory["alert"]["sent"] == [1000.0]  # the resolved notice reached nobody


def test_alert_enabled_false_posts_no_handler_notice(tmp_path):
    run = _Script(to_holder=3, holder=3)
    run.window = None
    mon = _sub_monitor(tmp_path, [HOT, HOT], run, enabled=False)
    mon.tick(1000.0)
    assert len(run.ran("new-window")) == 1
    mon.tick(1000.0 + 300)
    assert mon.memory["handler"]["last_spawn_ok"] is False and run.telegram_sends() == []


@pytest.mark.parametrize("severity", [SEVERITY_CRITICAL, SEVERITY_WARNING])
def test_old_tg_ctl_falls_back_to_telegram(tmp_path, severity):
    run = _Script(to_holder=1, holder=1, to_holder_err="tg-ctl alert: unknown flag: --to-holder")
    mon = _sub_monitor(tmp_path, [HOT if severity == SEVERITY_CRITICAL else WARM], run, min_severity="warning")
    mon.tick(1000.0)
    log = (mon.incident or {}).get("alerts") or mon.memory["recent_alerts"]
    if severity == SEVERITY_CRITICAL:
        assert len(run.telegram_sends()) == 1 and log[-1]["fallback"] == "telegram"
    else:
        assert run.telegram_sends() == [] and log[-1]["reason"] == "warning-no-telegram"
    assert run.ran("new-window") == []  # holder unknown: no spawn


def test_tick_never_raises_when_tmux_and_tg_ctl_are_missing(tmp_path):
    def run(argv, **kw):
        raise FileNotFoundError(errno.ENOENT, "missing", argv[0])

    mon = _sub_monitor(tmp_path, [HOT, HOT], run)
    mon.tick(1000.0)
    mon.tick(1010.0)
    assert mon.memory["handler"]["holder"]["state"] == "unknown"


def test_post_crash_alert_takes_the_fallback_on_an_old_tg_ctl(tmp_path):
    state = tmp_path / "state"
    store.write_json(state / store.STATUS, {"heartbeat": {"boot_id": "boot-old", "last_heartbeat": 1.0,
                                                           "clean_shutdown": False}})
    run = _Script(to_holder=1, holder=1, to_holder_err="unknown flag: --to-holder")
    mon = _sub_monitor(tmp_path, [_sample(boot_id="boot-new")], run)
    mon.cfg["sessions"]["panic_dirs"] = [str(tmp_path / "none")]
    mon.check_crash(2000.0)
    assert len(run.telegram_sends()) == 1 and mon.memory["handler"]["post_crash_pending"] is True


def test_spawn_request_file_is_honoured_once(tmp_path):
    run = _Script(to_holder=0, holder=0)
    mon = _sub_monitor(tmp_path, [_sample(), _sample()], run)
    req = tmp_path / "state" / "handler" / "spawn-request"
    req.parent.mkdir(parents=True)
    req.write_text("")
    mon.tick(1000.0)
    assert len(run.ran("new-window")) == 1 and not req.exists()
    req.write_text("")
    mon.tick(1010.0)
    assert len(run.ran("new-window")) == 1 and not req.exists()
    assert mon.memory["recent_handler"][-1]["outcome"] == "refused: window exists"


def test_subscriber_route_never_launches_remediation_even_from_a_raw_json_config(tmp_path, monkeypatch):
    """rig's validator rejects auto_remediate with route subscriber; a hand-written --config JSON
    must not get both agents for one incident either."""
    monkeypatch.setattr(daemon.remediate, "maybe_spawn", lambda *a, **k: pytest.fail("remediation launched"))
    run = _Script(to_holder=0, holder=0)
    mon = _sub_monitor(tmp_path, [HOT], run)
    mon.cfg["auto_remediate"] = True
    mon.tick(1000.0)
    assert mon.incident is not None and mon.incident["remediation"] is None


def test_simulation_never_spawns_or_calls_tg_ctl(tmp_path, monkeypatch):
    cfg = config.load(None)
    cfg["state_dir"] = str(tmp_path / "state")
    cfg["alert"]["route"] = "subscriber"
    calls = []
    monkeypatch.setattr(daemon.probe, "census", lambda **kw: [])
    monkeypatch.setattr(daemon.probe, "tmux_panes", lambda *a, **k: {})
    monkeypatch.setattr(daemon.probe, "launchd_jobs", lambda **k: {})
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: calls.append(argv) or _completed(argv, 0))
    inc = daemon.simulate(cfg, [_f("display_freeze")])
    assert not [c for c in calls if c[0] in ("tg-ctl", "tg") or c[1:2] in (["new-window"], ["new-session"], ["kill-window"])]
    assert [r["outcome"] for r in inc["handler"]] == ["would_spawn"]


# ── rig sysmon handler status|spawn, test-alert under route subscriber ────────────────────
def _cli_config(tmp_path, **over):
    path = tmp_path / "sysmon.json"
    path.write_text(json.dumps({"state_dir": str(tmp_path / "state"), "alert": {"route": "subscriber"}, **over}))
    return str(path)


def test_cli_handler_status_prints_holder_window_and_last_spawn(tmp_path, capsys, monkeypatch):
    from riglib.sysmon_daemon import cli, handler

    conf = _cli_config(tmp_path, handler={"tmux_session": "0"})
    store.write_json(tmp_path / "state" / store.MEMORY, {"handler": {"last_spawn_at": 0.0, "last_spawn_ok": True}})
    monkeypatch.setattr(handler, "read_holder", lambda cfg, run: handler.Holder("bound", "s-1", 11))
    monkeypatch.setattr(handler, "find_window", lambda cfg, run: handler.Window("@5", 4242))
    assert cli.main(["handler", "--config", conf, "status"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "holder: bound s-1 pid 11"
    assert out[1] == "window: 0:sysmon-handler @5 pane 4242"
    assert out[2].startswith("last spawn: 1970-01-01T") and out[2].endswith(" ok")
    monkeypatch.setattr(handler, "read_holder", lambda cfg, run: handler.Holder("vacant"))
    monkeypatch.setattr(handler, "find_window", lambda cfg, run: None)
    store.write_json(tmp_path / "state" / store.MEMORY, {})
    assert cli.main(["handler", "--config", conf, "status"]) == 0
    assert capsys.readouterr().out.splitlines() == ["holder: vacant", "window: 0:sysmon-handler absent",
                                                    "last spawn: never"]
    for bad in ({"handler": {"last_spawn_at": 1, "last_spawn_ok": "oops"}}, ["not", "a", "map"]):
        store.write_json(tmp_path / "state" / store.MEMORY, bad)
        assert cli.main(["handler", "--config", conf, "status"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "last spawn: never"


def test_cli_handler_spawn_refuses_when_the_window_exists(tmp_path, capsys, monkeypatch):
    from riglib.sysmon_daemon import cli, handler

    monkeypatch.setattr(handler, "find_window", lambda cfg, run: handler.Window("@5", 4242))
    assert cli.main(["handler", "--config", _cli_config(tmp_path), "spawn"]) == 3
    assert "handler window exists" in capsys.readouterr().err
    assert not (tmp_path / "state" / "handler" / "spawn-request").exists()


def test_cli_handler_spawn_writes_a_request_and_reports_the_window(tmp_path, capsys, monkeypatch):
    from riglib.sysmon_daemon import cli, handler

    answers = iter([None, None, handler.Window("@5", 4242), handler.Window("@5", 4242)])
    monkeypatch.setattr(handler, "find_window", lambda cfg, run: next(answers))
    req = tmp_path / "state" / "handler" / "spawn-request"
    written = []

    def sleep(seconds):
        written.append(req.exists())
        if len(written) == 2:  # the daemon spawned and recorded its window
            store.write_json(tmp_path / "state" / store.MEMORY, {"handler": {"window_id": "@5", "pane_pid": 4242}})

    monkeypatch.setattr(cli.time, "sleep", sleep)
    assert cli.main(["handler", "--config", _cli_config(tmp_path), "spawn"]) == 0
    assert written == [True, True]  # the request waited for the daemon
    assert not req.exists()  # a window raised by a concurrent request leaves ours no stale file
    assert capsys.readouterr().out.strip() == "spawned: main:sysmon-handler pane 4242"


def test_cli_handler_spawn_needs_route_subscriber_and_a_sane_wait(tmp_path, capsys, monkeypatch):
    from riglib.sysmon_daemon import cli, handler

    monkeypatch.setattr(handler, "find_window", lambda cfg, run: None)
    conf = _cli_config(tmp_path, alert={"route": "telegram"})
    assert cli.main(["handler", "--config", conf, "spawn"]) == cli.SPAWN_WRONG_ROUTE == 4
    assert "needs sysmon.alert.route subscriber" in capsys.readouterr().err
    assert not (tmp_path / "state" / "handler" / "spawn-request").exists()
    for bad in ("nan", "inf", "-1"):
        with pytest.raises(SystemExit):
            cli.main(["handler", "--config", _cli_config(tmp_path), "spawn", "--wait", bad])


def test_cli_handler_spawn_never_takes_a_foreign_window_for_its_spawn(tmp_path, capsys, monkeypatch):
    from riglib.sysmon_daemon import cli, handler

    answers = iter([None, handler.Window("@9", 77)])
    monkeypatch.setattr(handler, "find_window", lambda cfg, run: next(answers, handler.Window("@9", 77)))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert cli.main(["handler", "--config", _cli_config(tmp_path), "spawn", "--wait", "0"]) == 1
    assert "did not spawn" in capsys.readouterr().err


def test_cli_handler_spawn_times_out_with_exit_1(tmp_path, capsys, monkeypatch):
    from riglib.sysmon_daemon import cli, handler

    monkeypatch.setattr(handler, "find_window", lambda cfg, run: None)
    assert cli.main(["handler", "--config", _cli_config(tmp_path), "spawn", "--wait", "0"]) == 1
    err = capsys.readouterr().err
    assert "requested; the daemon did not spawn within 0 s" in err and "is ai.hyperide.sysmon running?" in err
    assert handler.request_wait_s(config.load(None)) == 30.0


def test_test_alert_with_route_subscriber_sends_only_to_the_holder(tmp_path, capsys):
    from riglib.sysmon_daemon import cli

    log = tmp_path / "argv.log"
    fake = tmp_path / "tg-ctl"
    fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {log}\necho OK holder\n')
    fake.chmod(0o755)
    conf = _cli_config(tmp_path, alert={"route": "subscriber", "tg_ctl_bin": str(fake), "tg_bin": str(tmp_path / "no-tg")})
    assert cli.main(["test-alert", "--config", conf]) == 0
    rec = json.loads(capsys.readouterr().out)
    assert rec["via"] == "holder" and rec["outcome"] == "sent"
    calls = [line for line in log.read_text().splitlines() if line.startswith("alert ")]  # the body spans lines
    assert len(calls) == 1 and calls[0].startswith("alert send ") and " --to-holder " in calls[0]
    assert not (tmp_path / "state" / "handler").exists()  # test-alert never spawns
