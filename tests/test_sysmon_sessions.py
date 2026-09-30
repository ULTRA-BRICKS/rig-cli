"""sysmon Layer 4 (rig-cli#622): session snapshot, crash detection and fail-closed restore."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from riglib.sysmon_daemon import config, sessions, store
from riglib.sysmon_daemon.model import AgentHost, to_jsonable

SID_A = "01a0ed2f-5456-7066-9401-34bbf12338a9"
SID_B = "01a07b46-07c7-7192-b523-7633800227fc"


def _transcript(root: Path, sid: str, title: str = "") -> Path:
    path = root / "-xp-app" / f"2026-09-29T12-41-37-110Z_{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if title:
        lines.append(json.dumps({"type": "title", "v": 1, "title": title, "pad": " " * 100}))
    lines.append(json.dumps({"type": "session", "version": 3, "id": sid, "cwd": "/xp/app"}))
    lines.append(json.dumps({"type": "message", "message": {"role": "user", "content": "x" * 9000}}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _record(path: Path, sid: str, cwd: str = "/xp/app") -> sessions.SessionRecord:
    return sessions.SessionRecord(
        kind="omp", pid=500, start_time=1.0, cwd=cwd, session_file=str(path), session_id=sid,
        pane="tmux:main:1.0", title=None, captured_at=10.0,
    )


def _cfg(tmp_path: Path) -> dict:
    cfg = config.load(None)
    cfg["state_dir"] = str(tmp_path / "state")
    cfg["sessions"].update({"omp_bin": "/opt/bin/omp", "tmux_bin": "tmux", "tmux_session": "main"})
    return cfg


class FakeTmux:
    """Records every argv; ``has-session`` answers per ``session_exists``."""

    def __init__(self, session_exists: bool = False, fail: bool = False) -> None:
        self.session_exists = session_exists
        self.fail = fail
        self.calls = []

    def __call__(self, cmd, **kwargs):
        assert kwargs.get("stdin") is subprocess.DEVNULL and kwargs.get("timeout")
        self.calls.append(list(cmd))
        if "has-session" in cmd:
            return subprocess.CompletedProcess(cmd, 0 if self.session_exists else 1, "", "")
        return subprocess.CompletedProcess(cmd, 1 if self.fail else 0, "", "boom" if self.fail else "")


# ── snapshot ──────────────────────────────────────────────────────────────────────────────
def test_snapshot_keeps_only_interactive_omp_hosts_with_a_transcript(tmp_path):
    a = _transcript(tmp_path, SID_A, title="Ship the monitor")
    b = _transcript(tmp_path, SID_B)
    hosts = [
        AgentHost(pid=10, kind="omp", args="omp", cwd="/xp/app", session_file=str(a), session_id=SID_A,
                  pane="tmux:main:1.0"),
        AgentHost(pid=11, kind="omp", args="omp -p hi", session_file=str(b), session_id=SID_B,
                  interactive=False),
        AgentHost(pid=12, kind="omp", args="omp", cwd="/xp/app"),
        AgentHost(pid=13, kind="claude-code", args="claude", session_file="/x/projects/p/s.jsonl"),
    ]

    records = sessions.snapshot(hosts, now=99.0, start_time_of=lambda pid: 1000.0 + pid)

    assert records == [
        sessions.SessionRecord(
            kind="omp", pid=10, start_time=1010.0, cwd="/xp/app", session_file=str(a), session_id=SID_A,
            pane="tmux:main:1.0", title="Ship the monitor", captured_at=99.0,
        )
    ]


def test_snapshot_round_trips_and_skips_malformed_entries(tmp_path):
    state = tmp_path / "state"
    rec = _record(tmp_path / "s.jsonl", SID_A)
    sessions.write_snapshot(state, [rec])
    data = json.loads((state / store.SESSIONS).read_text())
    store.write_json(state / store.SESSIONS, data + [{"pid": "x"}, "junk"])

    assert sessions.read_snapshot(state) == [rec]
    assert sessions.read_snapshot(tmp_path / "nowhere") == []


# ── crash detection ───────────────────────────────────────────────────────────────────────
def _panic(dirpath: Path, name: str, mtime: float, body: str) -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    path = dirpath / name
    path.write_text(body, encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def test_boot_change_after_unclean_stop_is_a_crash_and_picks_the_newest_fresh_panic(tmp_path):
    reports = tmp_path / "DiagnosticReports"
    _panic(reports, "old.panic", 500.0, "panic(cpu 0): ancient")
    _panic(reports, "mid.panic", 1100.0, "panic(cpu 1): mid")
    newest = _panic(reports, "new.panic", 1200.0, '{"bug_type":"210"}\n' + "noise " * 50 + "panic(cpu 3 caller 0x1): wired")
    _panic(reports, "later.ips", 1300.0, "panic(not a panic file)")
    prev = {"boot_id": "boot-1", "last_heartbeat": 1000.0, "clean_shutdown": False}

    crash = sessions.detect_crash(prev, "boot-2", [tmp_path / "missing", reports])

    assert crash is not None
    assert crash["reason"] == "panic"
    assert crash["panic_file"] == str(newest)
    assert crash["panic_excerpt"].startswith("panic(cpu 3 caller 0x1): wired")
    assert (crash["boot_id_before"], crash["boot_id_now"], crash["last_heartbeat"]) == ("boot-1", "boot-2", 1000.0)


def test_panic_older_than_the_heartbeat_is_not_blamed(tmp_path):
    reports = tmp_path / "r"
    _panic(reports, "old.panic", 900.0, "panic(cpu 0): before the heartbeat")
    prev = {"boot_id": "boot-1", "last_heartbeat": 1000.0}

    crash = sessions.detect_crash(prev, "boot-2", [reports])

    assert crash is not None
    assert crash["reason"] == "unclean-reboot"
    assert crash["panic_file"] is None and crash["panic_excerpt"] is None


def test_panic_excerpt_reads_the_panic_string_of_a_macos_json_report(tmp_path):
    body = json.dumps({"build": "x", "panicString": "panic(cpu 2 caller 0xff): wired memory\nBacktrace"})
    _panic(tmp_path / "r", "panic-full.panic", 2000.0, '{"bug_type":"210"}\n' + body)

    crash = sessions.detect_crash({"boot_id": "a", "last_heartbeat": 1000.0}, "b", [tmp_path / "r"])

    assert crash["panic_excerpt"] == "panic(cpu 2 caller 0xff): wired memory\nBacktrace"


def test_no_crash_on_clean_shutdown_same_boot_or_first_start(tmp_path):
    prev = {"boot_id": "boot-1", "last_heartbeat": 1000.0, "clean_shutdown": False}
    assert sessions.detect_crash({**prev, "clean_shutdown": True}, "boot-2", []) is None
    assert sessions.detect_crash(prev, "boot-1", []) is None
    assert sessions.detect_crash(None, "boot-2", []) is None
    # an unknown boot id on either side cannot prove a reboot
    assert sessions.detect_crash(prev, None, []) is None


# ── liveness + planning ───────────────────────────────────────────────────────────────────
def test_live_session_files_is_none_when_the_scan_failed(tmp_path):
    held = tmp_path / "a.jsonl"
    assert sessions.live_session_files(None) is None
    assert sessions.live_session_files([AgentHost(pid=1, kind="omp", args="omp", session_file=str(held))]) == {
        os.path.realpath(held)
    }


def _ps_run(fixed: str, args: str):
    def run(cmd, **kwargs):
        out = args if "pid=,args=" in cmd else fixed
        return subprocess.CompletedProcess(cmd, 0, out, "")
    return run


def test_live_hosts_fails_closed_when_an_omp_hosts_open_files_are_unreadable(tmp_path):
    cfg = _cfg(tmp_path)
    run = _ps_run(
        "  500     1   500   501 ttys001 S+   01:00  1000  0.0 omp\n",
        "  500 /usr/local/bin/omp\n",
    )

    unreadable = sessions.live_hosts(cfg, run=run, cwd_of=lambda pid: "/xp", open_paths_of=lambda pid: None)
    readable = sessions.live_hosts(cfg, run=run, cwd_of=lambda pid: "/xp", open_paths_of=lambda pid: [])

    assert unreadable is None
    assert readable is not None and [h.pid for h in readable] == [500]
    assert sessions.live_hosts(cfg, run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "")) is None


def test_plan_restore_skips_unverifiable_live_resumed_missing_and_dedups(tmp_path):
    a = _transcript(tmp_path, SID_A)
    b = _transcript(tmp_path / "b", SID_B)
    c = _transcript(tmp_path / "c", "01a0cccc-0000-7000-8000-000000000000")
    gone = tmp_path / "gone.jsonl"
    records = [_record(a, SID_A), _record(a, SID_A), _record(b, SID_B), _record(c, "c"), _record(gone, "g")]

    blind = sessions.plan_restore(records, None, set())
    plan = sessions.plan_restore(records, {os.path.realpath(b)}, {str(c)})

    assert [(p["action"], p["reason"]) for p in blind] == [("skip", "cannot verify liveness")] * 4
    assert [(p["session_file"], p["action"]) for p in plan] == [
        (str(a), "resume"), (str(b), "skip"), (str(c), "skip"), (str(gone), "skip"),
    ]
    assert plan[1]["reason"] == "still live (a second writer would corrupt it)"
    assert "already resumed" in plan[2]["reason"]
    assert "missing" in plan[3]["reason"]


# ── explanation ───────────────────────────────────────────────────────────────────────────
def _incident(records, **extra) -> dict:
    base = {
        "id": "20260929T031500-crash",
        "kind": "post-crash",
        "opened_at": 1_790_000_000.0,
        "findings": [
            {"key": "wired", "kind": "wired", "severity": "critical", "summary": "wired memory 95.7% (13 whisper-cli)"},
            {"key": "storm", "kind": "storm", "severity": "warning", "summary": "orphaned bash children: 212"},
        ],
        "sessions": [to_jsonable(r) for r in records],
        "crash": {"reason": "panic", "panic_excerpt": "panic(cpu 3 caller 0x1): wired memory exhausted"},
        "heal": [{"action": "kill", "comm": "whisper-cli", "pid": 4242, "status": "ok"}],
    }
    base.update(extra)
    return base


def test_explanation_names_the_incident_causes_and_asks_for_a_recheck():
    text = sessions.explanation(_incident([]))

    assert "20260929T031500-crash" in text
    assert "wired memory 95.7% (13 whisper-cli)" in text
    assert "orphaned bash children: 212" in text
    assert "panic(cpu 3 caller 0x1)" in text
    assert "whisper-cli" in text  # what the heal ladder did
    assert "heavy-run lock" in text


def test_explanation_stays_short_with_many_long_findings():
    findings = [{"summary": f"finding {i} " + "y" * 400} for i in range(30)]
    text = sessions.explanation(_incident([], findings=findings))

    assert len(text) <= 1500
    assert "20260929T031500-crash" in text and "heavy-run lock" in text


# ── restore ───────────────────────────────────────────────────────────────────────────────
def _setup_incident(tmp_path, records) -> Path:
    state = tmp_path / "state"
    store.save_incident(state, _incident(records))
    return state


def test_restore_launches_new_session_then_new_window_with_exact_argv(tmp_path):
    a = _transcript(tmp_path, SID_A)
    b = _transcript(tmp_path / "b", SID_B)
    state = _setup_incident(tmp_path, [_record(a, SID_A, cwd="/xp/a"), _record(b, SID_B, cwd="/xp/b")])
    tmux = FakeTmux(session_exists=False)
    cfg = _cfg(tmp_path)

    results = sessions.restore(state, "20260929T031500-crash", cfg, tmux_socket="sock", run=tmux,
                               hosts_fn=lambda cfg: [])

    message = sessions.explanation(store.load_incident(state, "20260929T031500-crash"))
    assert tmux.calls == [
        ["tmux", "-L", "sock", "has-session", "-t", "=main"],
        ["tmux", "-L", "sock", "new-session", "-d", "-s", "main", "-n", "sysmon-01a0ed2f", "-c", "/xp/a",
         "/opt/bin/omp", "--resume", str(a), message],
        ["tmux", "-L", "sock", "new-window", "-d", "-t", "=main:", "-n", "sysmon-01a07b46", "-c", "/xp/b",
         "/opt/bin/omp", "--resume", str(b), message],
    ]
    assert [r["status"] for r in results] == ["resumed", "resumed"]
    saved = store.load_incident(state, "20260929T031500-crash")["restore"]
    assert [(r["session_file"], r["status"]) for r in saved] == [(str(a), "resumed"), (str(b), "resumed")]
    assert all(isinstance(r["at"], float) for r in saved)


def test_restore_second_run_skips_already_resumed_sessions(tmp_path):
    a = _transcript(tmp_path, SID_A)
    state = _setup_incident(tmp_path, [_record(a, SID_A)])
    cfg = _cfg(tmp_path)
    sessions.restore(state, "20260929T031500-crash", cfg, run=FakeTmux(session_exists=True), hosts_fn=lambda cfg: [])
    again = FakeTmux(session_exists=True)

    results = sessions.restore(state, "20260929T031500-crash", cfg, run=again, hosts_fn=lambda cfg: [])

    assert again.calls == []
    assert [(r["status"], r["reason"]) for r in results] == [("skipped", "already resumed")]


def test_restore_never_launches_when_liveness_is_unknown_or_the_session_is_live(tmp_path):
    a = _transcript(tmp_path, SID_A)
    state = _setup_incident(tmp_path, [_record(a, SID_A)])
    cfg = _cfg(tmp_path)
    live = [AgentHost(pid=77, kind="omp", args="omp", session_file=str(a))]
    tmux = FakeTmux(session_exists=True)

    blind = sessions.restore(state, "20260929T031500-crash", cfg, run=tmux, hosts_fn=lambda cfg: None)
    busy = sessions.restore(state, "20260929T031500-crash", cfg, run=tmux, hosts_fn=lambda cfg: live)

    assert tmux.calls == []
    assert [r["status"] for r in blind + busy] == ["skipped", "skipped"]


def test_restore_records_a_failed_tmux_launch(tmp_path):
    a = _transcript(tmp_path, SID_A)
    state = _setup_incident(tmp_path, [_record(a, SID_A)])

    results = sessions.restore(state, "20260929T031500-crash", _cfg(tmp_path),
                               run=FakeTmux(session_exists=True, fail=True), hosts_fn=lambda cfg: [])

    assert results[0]["status"] == "failed" and "boom" in results[0]["error"]


def test_restore_dry_run_runs_nothing_and_writes_nothing(tmp_path):
    a = _transcript(tmp_path, SID_A)
    state = _setup_incident(tmp_path, [_record(a, SID_A, cwd="/xp/a")])
    path = store.incident_dir(state, "20260929T031500-crash") / store.INCIDENT_FILE
    before = path.read_bytes()
    tmux = FakeTmux()

    results = sessions.restore(state, "20260929T031500-crash", _cfg(tmp_path), dry_run=True, run=tmux,
                               hosts_fn=lambda cfg: [])

    assert tmux.calls == []
    assert path.read_bytes() == before
    assert results[0]["status"] == "planned"
    assert results[0]["command"][-3:-1] == ["--resume", str(a)]


# ── CLI ───────────────────────────────────────────────────────────────────────────────────
def _write_config(tmp_path: Path) -> Path:
    path = tmp_path / "sysmon.json"
    path.write_text(json.dumps({"state_dir": str(tmp_path / "state"), "sessions": {"omp_bin": "/opt/bin/omp"}}))
    return path


def test_restore_main_exit_codes(tmp_path, capsys):
    a = _transcript(tmp_path, SID_A)
    state = _setup_incident(tmp_path, [_record(a, SID_A)])
    store.save_incident(state, {"id": "20260930T000000-newer", "kind": "live", "sessions": []})
    cfg_path = _write_config(tmp_path)
    no_hosts = lambda cfg: []  # noqa: E731

    assert sessions.restore_main(["--config", str(cfg_path), "--incident", "nope"], hosts_fn=no_hosts) == 2
    assert sessions.restore_main(["--bogus"], hosts_fn=no_hosts) == 2
    capsys.readouterr()

    # default incident = newest one that HAS sessions (the "newer" one has none)
    code = sessions.restore_main(["--config", str(cfg_path), "--dry-run", "--json"], hosts_fn=no_hosts)
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert [(r["session_file"], r["status"]) for r in out] == [(str(a), "planned")]

    failing = FakeTmux(session_exists=True, fail=True)
    assert sessions.restore_main(["--config", str(cfg_path)], run=failing, hosts_fn=no_hosts) == 3
    assert "failed" in capsys.readouterr().out
