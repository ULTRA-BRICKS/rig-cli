"""sysmon swap alerts that mean something (rig-cli#685) and do not flap (rig-cli#699).

- a full swap is an incident only under real memory pressure: PSI ``full avg60`` (Linux), the
  kernel memorystatus level (macOS), or little MemAvailable / reclaimable memory;
- a raised finding clears only once its value is ``hysteresis_pct`` of its warn line past it;
- a finding that alerted, cleared and came back inside its cooldown is not new; an unchanged swap
  incident re-alerts once per ``swap_cooldown_minutes``;
- with ``alert.route: agents`` no resolved note is broadcast;
- the replay of 2026-10-15 on home (swap pinned at ~100 %, MemAvailable 55-90 %) pages nobody.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import subprocess
from pathlib import Path

import pytest

from riglib.sysmon_daemon import alert, config, daemon, detect, probe
from riglib.sysmon_daemon.model import SEVERITY_CRITICAL, SEVERITY_WARNING, Finding

GIB = probe.GIB
MIB = 1024 ** 2
TH = config.DEFAULT_THRESHOLDS
FIXTURE = Path(__file__).parent / "fixtures" / "sysmon_swap_home_2026-10-15.csv"


def _linux(**over):
    """A Linux-shaped sample of home (39 GB RAM, 16 GB swap): swap full, 24 GB available, no stall."""
    base = dict(
        taken_at=1000.0, ncpu=16, load1=2.0, load5=2.0, load15=2.0, mem_total=39 * GIB, wired=None,
        free=4 * GIB, inactive=8 * GIB, speculative=None, purgeable=None, reclaimable=24 * GIB,
        compressor_pages=None, compressor_limit_pages=None, swap_total=16 * GIB, swap_used=16 * GIB,
        pressure_level=None, proc_total=900, proc_max=None, disk_free=200 * GIB, boot_id="boot-a",
        page_size=4096, data_volume="/", data_used=None, data_free=None,
        psi_some_avg10=0.34, psi_some_avg60=0.15, psi_full_avg10=0.34, psi_full_avg60=0.15,
    )
    base.update(over)
    return probe.Sample(**base)


def _swap(s, *, held=(), hysteresis_pct=0.0):
    found = {f.key: f for f in detect.sample_findings(s, TH, held=held, hysteresis_pct=hysteresis_pct)}
    return found.get("swap")


# ── the pressure gate (#685) ──────────────────────────────────────────────────────────────
def test_full_swap_with_ample_memory_and_no_stall_is_cold_pages_not_a_finding():
    """2026-10-15 20:19-20:27 on home: swap 100 %, MemAvailable 24-25 GB of 39 (60-64 %), PSI ~0."""
    assert _swap(_linux(reclaimable=int(24.5 * GIB))) is None


@pytest.mark.parametrize("over, sev", [
    ({"psi_full_avg60": 6.0}, SEVERITY_WARNING),
    ({"psi_full_avg60": 25.0}, SEVERITY_CRITICAL),
    ({"reclaimable": 7 * GIB}, SEVERITY_WARNING),  # 17.9 % of RAM available
    ({"reclaimable": 3 * GIB}, SEVERITY_CRITICAL),  # 7.7 %
    ({"swap_used": int(13.6 * GIB), "psi_full_avg60": 25.0}, SEVERITY_WARNING),  # 85 % swap caps it
    ({"swap_used": 8 * GIB, "psi_full_avg60": 25.0}, None),  # pressure without a full swap: no swap finding
])
def test_swap_severity_is_the_lower_of_swap_use_and_memory_pressure(over, sev):
    f = _swap(_linux(**over))
    assert (f.severity if f else None) == sev


def test_the_finding_names_the_pressure_that_made_it_one():
    f = _swap(_linux(reclaimable=3 * GIB, psi_full_avg60=7.5))
    assert "available memory 3.0 GB (8%)" in f.summary and "PSI full avg60 7.5%" in f.summary
    assert f.metrics["swap_gate_available_pct"] == pytest.approx(7.69, abs=0.01)
    assert f.metrics["swap_gate_psi_full_avg60"] == 7.5


@pytest.mark.parametrize("reclaimable, sev", [(24 * GIB, None), (3 * GIB, SEVERITY_CRITICAL)])
def test_without_psi_the_memavailable_gate_alone_decides(reclaimable, sev):
    """An old kernel or a container without /proc/pressure/memory."""
    s = _linux(reclaimable=reclaimable, psi_some_avg10=None, psi_some_avg60=None,
               psi_full_avg10=None, psi_full_avg60=None)
    f = _swap(s)
    assert (f.severity if f else None) == sev


@pytest.mark.parametrize("level, sev", [(1, None), (2, SEVERITY_WARNING), (4, SEVERITY_CRITICAL)])
def test_macos_gates_on_the_memorystatus_level_and_reclaimable(level, sev):
    s = _linux(pressure_level=level, psi_some_avg10=None, psi_some_avg60=None,
               psi_full_avg10=None, psi_full_avg60=None, mem_total=24 * GIB, reclaimable=10 * GIB)
    f = _swap(s)
    assert (f.severity if f else None) == sev


def test_with_no_pressure_signal_at_all_swap_is_judged_alone():
    s = _linux(reclaimable=None, psi_some_avg10=None, psi_some_avg60=None, psi_full_avg10=None,
               psi_full_avg60=None)
    assert _swap(s).severity == SEVERITY_CRITICAL


PSI_TEXT = ("some avg10=0.34 avg60=0.15 avg300=0.60 total=701824196\n"
            "full avg10=0.30 avg60=0.12 avg300=0.58 total=603407037\n")


def test_probe_reads_memory_psi_some_and_full():
    assert probe.memory_psi(PSI_TEXT) == {"psi_some_avg10": 0.34, "psi_some_avg60": 0.15,
                                          "psi_full_avg10": 0.30, "psi_full_avg60": 0.12}


@pytest.mark.parametrize("text", [None, "", "garbage\n"])
def test_probe_missing_psi_is_none_not_zero(text):
    assert probe.memory_psi(text) == {"psi_some_avg10": None, "psi_some_avg60": None,
                                      "psi_full_avg10": None, "psi_full_avg60": None}


@pytest.mark.skipif(not probe.IS_LINUX, reason="reads /proc")
@pytest.mark.parametrize("psi, full60", [(PSI_TEXT, 0.12), (None, None)])
def test_the_linux_sample_carries_memory_psi(monkeypatch, psi, full60):
    files = {"/proc/meminfo": "MemTotal: 40000000 kB\nMemAvailable: 24000000 kB\nSwapTotal: 16000000 kB\n"
                              "SwapFree: 0 kB\n", "/proc/pressure/memory": psi}
    monkeypatch.setattr(probe, "_read", lambda path: files.get(path))
    s = probe.sample(1000.0)
    assert s.psi_full_avg60 == full60 and s.swap_pct == 100.0 and s.available_pct == 60.0


def test_sample_reports_available_pct():
    assert _linux(reclaimable=int(19.5 * GIB)).available_pct == pytest.approx(50.0)
    assert "available_pct" in _linux().as_dict()


# ── hysteresis on the value (#699) ────────────────────────────────────────────────────────
@pytest.mark.parametrize("metric, value, held, sev", [
    ("swap_pct", 78.0, False, "ok"),
    ("swap_pct", 78.0, True, SEVERITY_WARNING),  # raised: clears only under 80 - 5 % of 80 = 76
    ("swap_pct", 75.9, True, "ok"),
    ("swap_pct", 96.0, True, SEVERITY_CRITICAL),
    ("swap_gate_available_pct", 20.5, False, "ok"),
    ("swap_gate_available_pct", 20.5, True, SEVERITY_WARNING),  # lower is worse: clears over 21
    ("swap_gate_available_pct", 21.1, True, "ok"),
])
def test_a_raised_level_clears_only_past_the_hysteresis_margin(metric, value, held, sev):
    assert detect.level(metric, value, TH, held=held, hysteresis_pct=5.0) == sev


def test_a_held_swap_finding_survives_a_dip_just_under_both_lines():
    s = _linux(swap_used=int(0.78 * 16 * GIB), reclaimable=int(0.205 * 39 * GIB))
    assert _swap(s) is None
    assert _swap(s, held={"swap"}, hysteresis_pct=5.0).severity == SEVERITY_WARNING
    assert _swap(s, held={"swap"}, hysteresis_pct=0.0) is None


def test_a_held_process_growth_finding_clears_past_the_margin_too():
    now, base = _linux(taken_at=1060.0, proc_total=1195), _linux(taken_at=1000.0, proc_total=1000)  # 195/min
    found = detect.sample_findings(now, TH, [base], held={"proc_growth"}, hysteresis_pct=5.0)
    assert [f.severity for f in found if f.key == "proc_growth"] == [SEVERITY_WARNING]
    assert not [f for f in detect.sample_findings(now, TH, [base]) if f.key == "proc_growth"]


# ── alert.decide: cooldown across a clear, swap window, resolved under route agents ───────
def _cfg(**alert_over):
    cfg = config.load(None)
    cfg["alert"].update({"min_severity": "warning", **alert_over})
    return cfg


def _f(key="wired", kind="memory", sev=SEVERITY_WARNING):
    return Finding(key=key, kind=kind, severity=sev, summary=f"{key} bad")


def test_a_finding_that_cleared_and_came_back_inside_the_cooldown_is_not_new():
    cfg = _cfg()
    due, _, st = alert.decide([_f()], {}, cfg, 0.0)
    assert len(due) == 1
    _, _, st = alert.decide([], st, cfg, 60.0)
    _, resolved, st = alert.decide([], st, cfg, 60.0 + alert.RESOLVE_AFTER_S)
    assert resolved == ["wired"]
    due, _, st = alert.decide([_f()], st, cfg, 600.0)
    assert due == []  # same level, 10 min after the page: not a new alert
    due, _, st = alert.decide([_f(sev=SEVERITY_CRITICAL)], st, cfg, 660.0)
    assert [f.severity for f in due] == [SEVERITY_CRITICAL]  # an escalation still pages at once
    due, _, st = alert.decide([_f()], st, cfg, 720.0)
    assert due == []  # back down to warning right after a critical page: nothing new
    due, _, st = alert.decide([_f()], st, cfg, 660.0 + 30 * 60)
    assert len(due) == 1  # the cooldown since the last page at that level or above elapsed


def test_a_finding_oscillating_around_its_line_pages_once_per_cooldown():
    cfg = _cfg()
    sends = []
    st = {}
    for minute in range(0, 6 * 60):
        present = (minute // 7) % 2 == 0  # 7 min over, 7 min under: clears past RESOLVE_AFTER_S
        due, _, st = alert.decide([_f()] if present else [], st, cfg, minute * 60.0)
        sends += [minute] * len(due)
    assert sends and all(b - a >= 30 for a, b in zip(sends, sends[1:])), sends


def test_an_unchanged_swap_incident_re_alerts_once_per_swap_cooldown():
    cfg = _cfg()
    assert cfg["alert"]["swap_cooldown_minutes"] == 120
    sends = []
    st = {}
    for minute in range(0, 5 * 60):
        due, _, st = alert.decide([_f("swap", "swap")], st, cfg, minute * 60.0)
        sends += [minute] * len(due)
    assert sends == [0, 120, 240]


def test_a_memory_json_saved_before_level_sent_keeps_its_cooldown():
    """The daemon restarts onto the previous version's state: keys[] alone carries the last send."""
    old = {"keys": {"wired": {"severity": SEVERITY_CRITICAL, "last_sent": 1000.0}}, "sent": [1000.0], "disk_sent": {}}
    due, _, st = alert.decide([_f(sev=SEVERITY_CRITICAL)], old, _cfg(), 1010.0)
    assert due == [] and st["level_sent"] == {"wired": {SEVERITY_CRITICAL: 1000.0}}


def test_route_agents_never_produces_a_resolved_note():
    cfg = _cfg(route="agents")
    _, _, st = alert.decide([_f()], {}, cfg, 0.0)
    _, _, st = alert.decide([], st, cfg, 10.0)
    _, resolved, st = alert.decide([], st, cfg, 10.0 + alert.RESOLVE_AFTER_S)
    assert resolved == [] and st["keys"] == {} and len(st["sent"]) == 1  # no budget spent either


# ── replay: 2026-10-15 on home through the real monitor ───────────────────────────────────
def _rows():
    with FIXTURE.open(encoding="utf-8") as fh:
        for row in csv.DictReader(line for line in fh if not line.startswith("#")):
            yield dt.datetime.fromisoformat(row["time"]), row


def _replay_sample(at, row):
    avail, total = int(row["mem_avail_mib"]) * MIB, int(row["mem_total_mib"]) * MIB
    swap_total = int(row["swap_total_mib"]) * MIB
    return probe.Sample(
        taken_at=at.timestamp(), ncpu=16, load1=1.0, load5=1.0, load15=1.0, mem_total=total, wired=None,
        free=avail, inactive=None, speculative=None, purgeable=None, reclaimable=avail,
        compressor_pages=None, compressor_limit_pages=None, swap_total=swap_total,
        swap_used=swap_total - int(row["swap_free_mib"]) * MIB, pressure_level=None, proc_total=900,
        proc_max=None, disk_free=200 * GIB, boot_id="boot-a",
    )


class _Agents:
    """``tg-ctl omp-live``: one live session; every note it gets is kept with its sample time."""

    def __init__(self):
        self.notes = []
        self.now = None

    def __call__(self, argv, **kw):
        if argv[1:3] == ["omp-live", "status"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps([{"pid": 4242, "sessionId": "s"}]), "")
        if argv[1:3] == ["omp-live", "send"]:
            self.notes.append((self.now, argv[-1]))
        return subprocess.CompletedProcess(argv, 0, "", "")


def _replay(tmp_path, **thresholds):
    cfg = config.load(None)
    cfg["state_dir"] = str(tmp_path / "state")
    cfg["events"]["enabled"] = False
    cfg["heal"]["enabled"] = False
    # home's live alert block on 2026-10-15
    cfg["alert"].update(min_severity="warning", route="agents", cooldown_minutes=30, max_per_hour=6)
    cfg["thresholds"].update(thresholds)
    rows = list(_rows())
    samples = iter([_replay_sample(at, row) for at, row in rows])
    agents = _Agents()
    mon = daemon.Monitor(cfg, sampler=lambda now=None: next(samples), censor=lambda: [], run=agents, persist=False)
    for at, _ in rows:
        agents.now = at
        mon.tick(at.timestamp())
    swap = [at for at, text in agents.notes if text.startswith("[sysmon] sysmon ") and ": swap" in text.split("\n")[0]]
    resolved = [at for at, text in agents.notes if "resolved" in text.split("\n")[0]]
    return swap, resolved


def _assert_one_per_30_min(times):
    gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
    assert all(g >= 30 * 60 for g in gaps), [t.strftime("%H:%M") for t in times]


@pytest.mark.parametrize("swap_pct", [{"warn": 100, "critical": 101}, TH["swap_pct"]],
                         ids=["home-override-100", "defaults-80-95"])
def test_replay_of_2026_10_15_pages_nobody_for_cold_swap(tmp_path, swap_pct):
    swap, resolved = _replay(tmp_path, swap_pct=dict(swap_pct))
    _assert_one_per_30_min(swap)
    window = [t for t in swap if dt.time(20, 19) <= t.time() <= dt.time(20, 27, 59)]
    assert window == [] and resolved == []
    assert swap == []  # MemAvailable never fell under 25 % that day: no memory pressure at all


def test_replay_with_the_gate_inside_the_days_memavailable_band_still_pages_at_most_once_per_window(tmp_path):
    """Stress the same series: an available-memory line at 63 % sits inside the 55-90 % the day
    actually moved through, so the gate flips many times an hour. Hysteresis plus the clear-proof
    cooldown must still hold it to one page per swap_cooldown_minutes and no resolved notes."""
    swap, resolved = _replay(tmp_path, swap_gate_available_pct={"warn": 63, "critical": 10})
    assert swap, "the stressed gate must page at least once, or this test proves nothing"
    gaps = [(b - a).total_seconds() for a, b in zip(swap, swap[1:])]
    assert all(g >= 120 * 60 for g in gaps), [t.strftime("%H:%M") for t in swap]
    assert resolved == []


# ── swapoff in progress is no incident (#685, Main's ruling 2026-10-15) ──────────────────
def _drain(**over):
    return json.dumps({"state": "draining", "pid": 4242, "updated": 1000.0, "started": 990.0,
                       "device": "/swap_new.img", "left_kb": 9 * 1024 * 1024, **over})


@pytest.mark.parametrize("text, alive, live", [
    (_drain(), True, True),
    (_drain(updated=969.0), True, False),  # 31 s old: a crashed drain must not hide swap
    (_drain(), False, False),  # its pid is gone
    (_drain(state="idle"), True, False),
    (_drain(pid=True), True, False),
    ('{"state": "draining", "pid": 4242, "updated": NaN}', True, False),  # json parses NaN/Infinity
    ('{"state": "draining", "pid": 4242, "updated": Infinity}', True, False),
    (_drain(updated=2000.0), True, False),  # a future stamp never ages
    ('{"state": "draining", "pid": 4242, "updated": 1' + "0" * 400 + "}", True, False),  # no float
    (_drain(updated=1003.0), True, True),  # written just after the sample's clock read
    (None, True, False), ("{", True, False), ("[]", True, False),
])
def test_the_swap_drain_file_counts_only_while_a_drain_is_running(text, alive, live):
    assert probe.swap_drain_live(text, 1000.0, alive=lambda pid: alive) is live


@pytest.mark.parametrize("pid", [10 ** 30, 1 << 31, 0, -1])
def test_a_pid_no_process_can_have_is_not_alive_and_never_raises(pid):
    assert probe.pid_alive(pid) is False
    assert probe.swap_drain_live(_drain(pid=pid), 1000.0) is False


def test_a_registry_name_with_an_impossible_pid_does_not_break_the_critical_fallback(tmp_path, monkeypatch):
    (tmp_path / "omp-live").mkdir()
    (tmp_path / "omp-live" / f"{10 ** 20}-{'a' * 32}.json").write_text("{}")
    monkeypatch.setenv("TG_CTL_CONFIG_DIR", str(tmp_path))
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "[]" if argv[1] == "omp-live" else "OK tg#9\n", "")

    rec = alert.send("t", "b", _cfg(route="agents"), severity=SEVERITY_CRITICAL, key="k", run=run)
    assert rec["via"] == "tg-ctl alert" and rec["message_id"] == 9
    assert "holds no endpoint of a running omp" in rec["undelivered"]


def test_a_live_swap_drain_hides_a_full_swap_even_under_pressure():
    assert _swap(_linux(reclaimable=3 * GIB)).severity == SEVERITY_CRITICAL
    assert _swap(_linux(reclaimable=3 * GIB, swap_draining=True)) is None


def test_swaptotal_shrinking_under_its_recent_size_is_a_swapoff_not_an_incident():
    full = _linux(reclaimable=3 * GIB)
    mid_swapoff = _linux(reclaimable=3 * GIB, swap_total=9 * GIB, swap_used=9 * GIB)
    assert detect.swap_draining(mid_swapoff, [full]) and not detect.swap_draining(full, [full])
    found = detect.sample_findings(mid_swapoff, TH, [full])
    assert "swap" not in {f.key for f in found}


def test_a_swap_that_shrank_longer_ago_than_the_window_is_judged_again():
    """A removed swap device (or a 60-s sample interval keeping 90 min of history) must not hide
    real pressure for long: only a shrink inside SWAP_SHRINK_WINDOW_S counts as a swapoff."""
    old_full = _linux(taken_at=1000.0, reclaimable=3 * GIB)
    shrunk = _linux(taken_at=1000.0 + detect.SWAP_SHRINK_WINDOW_S + 60, reclaimable=3 * GIB,
                    swap_total=8 * GIB, swap_used=8 * GIB)
    assert not detect.swap_draining(shrunk, [old_full])
    assert _swap(shrunk).severity == SEVERITY_CRITICAL


def test_the_monitor_keeps_the_whole_shrink_window_at_a_one_second_interval(tmp_path):
    cfg = config.load(None)
    cfg.update(state_dir=str(tmp_path), sample_interval_seconds=1)
    mon = daemon.Monitor(cfg, persist=False)
    mon.history.append(_linux(taken_at=1000.0))
    mon.history.extend(_linux(taken_at=1000.0 + i, swap_total=9 * GIB, swap_used=9 * GIB) for i in range(1, 600))
    stalled = _linux(taken_at=1600.0, swap_total=9 * GIB, swap_used=9 * GIB, reclaimable=3 * GIB)
    assert "swap" not in {f.key for f in mon.sample_findings(stalled, list(mon.history))}


class _NoSession(_Agents):
    """``omp-live status`` listing nobody, as it did at 2026-10-15T19:28:05Z; any Telegram post is kept."""

    def __init__(self):
        super().__init__()
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if argv[1:3] == ["omp-live", "status"]:
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        return subprocess.CompletedProcess(argv, 0, "OK tg#1\n", "")


@pytest.mark.parametrize("before", [[], [16 * GIB] * 6], ids=["daemon-just-started", "drain-seen-shrinking"])
def test_replay_of_the_2026_10_15_19_28_05z_swapoff_sample_alerts_nobody(tmp_path, before):
    """21:28 local swap-drain: "WARNING swap 9.0 GB of 9.0 GB used (100%)" with 37.6 GB reclaimable
    went to Telegram. Neither the gate nor the shrinking SwapTotal lets it be a finding now."""
    cfg = config.load(None)
    cfg["state_dir"] = str(tmp_path / "state")
    cfg["events"]["enabled"] = False
    cfg["heal"]["enabled"] = False
    cfg["alert"].update(min_severity="warning", route="agents")
    cfg["thresholds"]["swap_pct"] = {"warn": 100, "critical": 101}  # home's override that day
    t0 = dt.datetime(2026, 10, 15, 19, 28, 5, tzinfo=dt.timezone.utc).timestamp()
    mem = 49_122_521_088  # home's MemTotal
    samples = [_linux(taken_at=t0 - 10 * (len(before) - i), mem_total=mem, reclaimable=int(30 * GIB),
                      swap_total=total, swap_used=int(total * 0.9), psi_full_avg60=None, psi_full_avg10=None,
                      psi_some_avg10=None, psi_some_avg60=None) for i, total in enumerate(before)]
    samples.append(_linux(taken_at=t0, mem_total=mem, reclaimable=int(37.6 * GIB), swap_total=9 * GIB,
                          swap_used=9 * GIB, psi_full_avg60=None, psi_full_avg10=None, psi_some_avg10=None,
                          psi_some_avg60=None))
    it = iter(samples)
    run = _NoSession()
    mon = daemon.Monitor(cfg, sampler=lambda now=None: next(it), censor=lambda: [], run=run, persist=False)
    for s in samples:
        found = mon.tick(s.taken_at)
    assert "swap" not in {f.key for f in found}
    assert not any(c[1] in ("alert", "omp-live") and c[2] in ("send",) for c in run.calls)
    assert not any(c[0] == "tg" for c in run.calls)


def test_a_drain_file_that_is_not_utf8_is_unreadable_not_a_crash(tmp_path):
    bad = tmp_path / "swap-drain.json"
    bad.write_bytes(b"\xff\xfe")
    assert probe._read(str(bad)) is None and probe.swap_drain_live(probe._read(str(bad)), 1000.0) is False


def test_a_dropped_warning_spends_no_budget_so_a_critical_still_reaches_telegram(tmp_path):
    """With no live session a warning is dropped; it must not use up max_per_hour and hold back
    the critical that follows (the only alert that may still fall back to Telegram)."""
    cfg = config.load(None)
    cfg.update(state_dir=str(tmp_path))
    cfg["events"]["enabled"] = False
    cfg["heal"]["enabled"] = False
    cfg["alert"].update(min_severity="warning", route="agents", max_per_hour=1)
    samples = iter([_linux(taken_at=1000.0, load1=21.0), _linux(taken_at=1010.0, load1=40.0)])  # 1.31, 2.5 per CPU
    run = _NoSession()
    mon = daemon.Monitor(cfg, sampler=lambda now=None: next(samples), censor=lambda: [], run=run, persist=False)
    mon.tick(1000.0)
    assert not [c for c in run.calls if c[1] == "alert"] and mon.memory["alert"]["sent"] == []
    mon.tick(1010.0)
    posted = [c for c in run.calls if c[1:3] == ["alert", "send"]]
    assert len(posted) == 1 and alert.flag_value(posted[0], "--severity") == SEVERITY_CRITICAL
