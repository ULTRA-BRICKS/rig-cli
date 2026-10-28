"""tcc-watch tests: config validation, plist rendering, line parsing, the store and legacy import."""

from __future__ import annotations

import json
import os
import plistlib
import sqlite3
from pathlib import Path

import pytest

from riglib import tcc_watch
from riglib import tcc_watch_daemon as daemon
from riglib.config import ConfigError, validate
from tests.tcc_watch_helpers import (
    _REAL_PROMPT_MESSAGES,
    _ROUTINE_MESSAGES,
    _T0,
    _TCCD,
    _TS,
    _counts,
    _legacy_row,
    _legacy_rows,
    _mirror,
    _obs,
    _plan,
    _seed_legacy,
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

def test_render_is_well_formed_and_labelled():
    p = _plan()
    body = p.render_plist()
    parsed = plistlib.loads(body.encode("utf-8"))
    assert parsed["Label"] == tcc_watch.DEFAULT_LABEL
    assert parsed["ProgramArguments"] == [str(p.python_bin), str(p.watcher_script_path), str(p.store_path)]
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
    assert p.store_path == Path("/home/u/.config/rig/tcc-watch/tcc-watch.sqlite3")
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


# ── detection logic: parse_observation, replayed against CAPTURED log-stream lines ─────────
# Every record below has the exact shape `log stream --style ndjson --debug` emitted on a real
# Mac (rig-cli#607); only bundle ids, paths and pids are neutralised. No real permission prompt
# is triggered to run these tests.


@pytest.mark.parametrize(("kind", "service", "message"), _REAL_PROMPT_MESSAGES)
def test_parse_classifies_real_prompts_and_denials(kind, service, message):
    obs = daemon.parse_observation(_tcc_line(message, pid=4242))
    assert obs is not None
    assert obs.kind == kind and obs.is_prompt
    assert obs.service == service
    assert obs.pid == 4242
    assert obs.process == "tccd" and obs.process_path == _TCCD
    assert obs.message == message


@pytest.mark.parametrize("message", _ROUTINE_MESSAGES)
def test_parse_classifies_routine_chatter_as_noise(message):
    """rig-cli#607: preflight/attribution/entitlement/read-only lines are NOT prompts."""
    obs = daemon.parse_observation(_tcc_line(message))
    assert obs is not None
    assert obs.kind == daemon.NOISE and not obs.is_prompt


def test_parse_keys_a_line_by_its_format_string_and_extracts_the_call():
    fmt = ("SEND: %d/%d synchronous to %{public}s: request: msgID=%{public}s, function=%{public}s, "
           "service=%{public}@,")
    obs = daemon.parse_observation(_tcc_line(_ROUTINE_MESSAGES[2], fmt=fmt, process="/usr/libexec/ua"))
    assert obs.template == fmt
    assert obs.process == "ua"
    assert obs.service == "kTCCServiceAccessibility"
    assert obs.function == "TCCAccessCopyInformation"
    assert obs.key == ("ua", "access", fmt, "kTCCServiceAccessibility", "TCCAccessCopyInformation", "", "noise")


@pytest.mark.parametrize(("message", "function"), [
    (_ROUTINE_MESSAGES[3], "TCCAccessRequest preflight"),  # REQUEST_MSG, quoted function
    (_ROUTINE_MESSAGES[4], "preflight"),  # AUTHREQ_CTX, function=<private>
    (_ROUTINE_MESSAGES[1], "TCCAccessRequest"),
    (_ROUTINE_MESSAGES[0], ""),
])
def test_parse_marks_the_tcc_call_and_preflight(message, function):
    assert daemon.parse_observation(_tcc_line(message)).function == function


@pytest.mark.parametrize(("message", "subject"), [
    (_ROUTINE_MESSAGES[0], "com.example.chat"),
    (_REAL_PROMPT_MESSAGES[3][2], "com.example.agent"),
    ("Policy disallows prompt for Sub:{/usr/libexec/routined}Resp:{...}; access to kTCCServiceWillow denied",
     "/usr/libexec/routined"),
    (_ROUTINE_MESSAGES[1], ""),
])
def test_parse_extracts_the_subject_app(message, subject):
    assert daemon.parse_observation(_tcc_line(message)).subject == subject


def test_parse_without_a_format_string_falls_back_to_the_leading_token():
    obs = daemon.parse_observation(_tcc_line(_ROUTINE_MESSAGES[5], fmt=None))
    assert obs.template == "<TCCDProcess"
    assert daemon.parse_observation(_tcc_line("", fmt=None)).template == "(empty)"


def test_parse_drops_malformed_timestamp_and_pid_fields():
    line = json.dumps({
        "subsystem": "com.apple.TCC", "eventMessage": "display_prompt: called for <private>",
        "timestamp": {"not": "a string"}, "processID": True,
    })
    obs = daemon.parse_observation(line)
    assert obs.epoch is None and obs.timestamp == ""
    assert obs.pid is None
    assert obs.process == "?" and obs.process_path is None


def test_parse_timestamp_to_epoch():
    assert daemon.parse_observation(_tcc_line("x", ts="2026-09-15 16:58:21.810739+0200")).epoch == 1789484301
    assert daemon.parse_observation(_tcc_line("x", ts="2026-09-15 16:58:21+0200")).epoch == 1789484301
    assert daemon.parse_observation(_tcc_line("x", ts="yesterday")).epoch is None


@pytest.mark.parametrize("line", [
    "", "   \n", "{not valid json", "[1, 2, 3]", '"just a string"',
    'Filtering the log data using "subsystem == \\"com.apple.TCC\\""',
    _tcc_line("display_prompt: x", subsystem="com.apple.something.else"),
])
def test_parse_ignores_anything_that_is_not_a_tcc_record(line):
    assert daemon.parse_observation(line) is None


def test_parse_caps_an_oversize_message():
    obs = daemon.parse_observation(_tcc_line("display_prompt: " + "x" * 20000))
    assert len(obs.message) == daemon.MAX_MESSAGE_CHARS


def test_log_stream_reads_the_whole_tcc_subsystem():
    """Everything is counted at the source now — the predicate no longer drops the chatter."""
    cmd = daemon.LOG_STREAM_CMD
    assert cmd[:2] == ["log", "stream"]
    assert "--debug" in cmd
    assert cmd[cmd.index("--predicate") + 1] == 'subsystem == "com.apple.TCC"'


def test_spawn_log_stream_decodes_leniently(monkeypatch):
    """A non-UTF-8 byte in a log message must not raise out of the read loop."""
    seen: dict = {}

    def fake_popen(cmd, **kwargs):
        seen.update(cmd=cmd, **kwargs)
        return "proc"

    monkeypatch.setattr(daemon.subprocess, "Popen", fake_popen)
    assert daemon._spawn_log_stream() == "proc"
    assert seen["cmd"] == daemon.LOG_STREAM_CMD
    assert seen["text"] is True
    assert seen["errors"] == "replace"


# ── the store ─────────────────────────────────────────────────────────────────────────────

def test_store_counts_lines_per_hour_per_template(store):
    for _ in range(3):
        store.observe(_obs(_ROUTINE_MESSAGES[0], epoch=_T0 + 10))
    store.observe(_obs(_ROUTINE_MESSAGES[0], epoch=_T0 + 3600 + 5))
    store.observe(_obs("SEND: other", epoch=_T0 + 20, fmt="SEND: %d"))
    assert _counts(store) == []  # buffered until flush
    store.flush()
    assert _counts(store) == [
        (_T0, 3600, "%{public}s", "com.example.chat", 3),
        (_T0, 3600, "SEND: %d", "", 1),
        (_T0 + 3600, 3600, "%{public}s", "com.example.chat", 1),
    ]
    example = store.db.execute("SELECT example FROM templates WHERE format = '%{public}s'").fetchone()[0]
    assert example == _ROUTINE_MESSAGES[0]


def test_store_counts_accumulate_across_flushes_and_reopens(store, tmp_path):
    store.observe(_obs(_ROUTINE_MESSAGES[1]))
    store.flush()
    store.observe(_obs(_ROUTINE_MESSAGES[1]))
    store.close()
    again = daemon.TccStore(store.path)
    again.observe(_obs(_ROUTINE_MESSAGES[1]))
    again.flush()
    assert again.totals() == (3, 0)
    assert again.db.execute("SELECT COUNT(*) FROM templates").fetchone()[0] == 1
    again.db.close()


def test_store_records_its_schema_version(store):
    row = store.db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    assert row == (str(daemon.SCHEMA_VERSION),)


def test_store_keeps_prompts_in_full_and_commits_them_at_once(store):
    kind, service, message = _REAL_PROMPT_MESSAGES[0]
    store.observe(_obs(message))
    other = sqlite3.connect(str(store.path))
    row = other.execute("SELECT ts, kind, service, process_path, pid, message FROM prompts").fetchone()
    other.close()
    assert row == (_TS, kind, service, _TCCD, 672, message)


def test_store_maintain_rolls_old_hours_into_days_and_keeps_the_total(store):
    old_day = _T0 - 20 * 86400 - _T0 % 86400
    for hour in (1, 2, 5):
        for _ in range(hour):
            store.observe(_obs(_ROUTINE_MESSAGES[0], epoch=old_day + hour * 3600))
    store.observe(_obs(_ROUTINE_MESSAGES[0], epoch=_T0))
    store.flush()
    assert store.totals() == (9, 0)
    store.maintain()
    assert store.totals() == (9, 0)
    assert _counts(store) == [
        (old_day, 86400, "%{public}s", "com.example.chat", 8),
        (_T0, 3600, "%{public}s", "com.example.chat", 1),
    ]
    # a second rollup of the same day (more late hours) merges into the same daily row
    store.observe(_obs(_ROUTINE_MESSAGES[0], epoch=old_day + 7 * 3600))
    store.maintain()
    assert _counts(store)[0] == (old_day, 86400, "%{public}s", "com.example.chat", 9)


def test_store_maintain_trims_prompts_and_diagnostics(store, monkeypatch):
    monkeypatch.setattr(daemon, "MAX_PROMPT_ROWS", 3)
    monkeypatch.setattr(daemon, "MAX_DIAGNOSTIC_ROWS", 2)
    for i in range(6):
        store.observe(_obs(f"display_prompt: called for <private> for service kTCCServicePhotos #{i}"))
        store.diagnostic(f"note {i}")
    store.maintain()
    msgs = [r[0] for r in store.db.execute("SELECT message FROM prompts ORDER BY id")]
    assert [m[-2:] for m in msgs] == ["#3", "#4", "#5"]
    assert [r[0] for r in store.db.execute("SELECT message FROM diagnostics ORDER BY id")] == ["note 4", "note 5"]


def test_store_size_is_bounded_by_templates_not_by_volume(store):
    """The 2.1 GB of #607 was ~180 templates repeated: 100k lines must stay a few KB of rows."""
    for i in range(100_000):
        store.observe(_obs(_ROUTINE_MESSAGES[i % len(_ROUTINE_MESSAGES)], epoch=_T0 + i % 3600))
    store.flush()
    store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert store.totals() == (100_000, 0)
    assert store.db.execute("SELECT COUNT(*) FROM counts").fetchone()[0] == len({_obs(m).key for m in _ROUTINE_MESSAGES})
    assert store.path.stat().st_size < 64 * 1024


def test_store_summary_answers_what_fills_the_log(store):
    for i in range(90):
        store.observe(_obs(_ROUTINE_MESSAGES[0], process="/usr/libexec/syspolicyd"))
    for i in range(9):
        store.observe(_obs(_ROUTINE_MESSAGES[1], process="/usr/sbin/contactsd"))
    store.observe(_obs(_REAL_PROMPT_MESSAGES[1][2]))
    summary = store.summary(since=_T0)
    assert summary["lines"] == 100
    assert summary["daily_utc"] == [{"day": "2026-09-15", "lines": 100}]
    assert summary["by_kind"][0] == {"kind": "noise", "lines": 99}
    assert {"kind": "prompt_displayed", "lines": 1} in summary["by_kind"]
    assert summary["by_process"][0] == {"process": "syspolicyd", "lines": 90}
    top = summary["top_templates"][0]
    assert (top["process"], top["subject"], top["lines"]) == ("syspolicyd", "com.example.chat", 90)
    assert summary["top_templates"][1]["function"] == "TCCAccessRequest"
    assert summary["recent_prompts"][0]["kind"] == "prompt_displayed"


# ── one-time import of the pre-#607 line log ───────────────────────────────────────────────

def test_import_legacy_family_counts_every_line_and_keeps_every_prompt(store):
    d = store.path.parent
    older, newer = _legacy_rows(), _legacy_rows(10)
    _seed_legacy(d, "events.jsonl.1", older)
    _seed_legacy(d, "events.jsonl", newer)
    notes = daemon.import_legacy_logs(store, d)
    assert not (d / "events.jsonl").exists() and not (d / "events.jsonl.1").exists()
    assert store.totals() == (len(older) + len(newer), 2 * len(_REAL_PROMPT_MESSAGES))
    assert notes[0].startswith(f"imported events.jsonl.1: {len(older)} lines (5 prompts/denials)")
    kinds = [r[0] for r in store.db.execute("SELECT kind FROM prompts ORDER BY id")]
    assert kinds == [k for k, _, _ in _REAL_PROMPT_MESSAGES] * 2
    # counted per UTC day, under the legacy template
    assert store.db.execute("SELECT DISTINCT span FROM counts").fetchall() == [(86400,)]
    assert store.db.execute("SELECT DISTINCT category FROM templates").fetchall() == [("legacy",)]
    meta = json.loads(store.db.execute("SELECT value FROM meta WHERE key = 'legacy_import:events.jsonl'").fetchone()[0])
    assert (meta["lines"], meta["prompts"]) == (len(newer), 5)


def test_import_rolls_back_and_keeps_the_file_when_totals_do_not_verify(store, monkeypatch):
    d = store.path.parent
    path = _seed_legacy(d, "events.jsonl", _legacy_rows())
    real_totals = daemon.TccStore.totals
    calls = {"n": 0}

    def off_by_one(self):
        calls["n"] += 1
        lines, prompts = real_totals(self)
        return (lines + 1, prompts) if calls["n"] == 2 else (lines, prompts)

    monkeypatch.setattr(daemon.TccStore, "totals", off_by_one)
    notes = daemon.import_legacy_logs(store, d)
    monkeypatch.setattr(daemon.TccStore, "totals", real_totals)
    assert path.exists()
    assert store.totals() == (0, 0)
    assert store.db.execute("SELECT COUNT(*) FROM templates").fetchone()[0] == 0
    assert "kept it" in notes[0]


def test_import_does_not_double_count_a_file_it_could_not_remove(store):
    d = store.path.parent
    rows = _legacy_rows()
    path = _seed_legacy(d, "events.jsonl", rows)
    backup = d / "keep"
    backup.write_bytes(path.read_bytes())
    stat_before = path.stat()
    daemon.import_legacy_logs(store, d)
    backup.rename(path)  # as if the unlink had failed: same bytes, same mtime
    os.utime(path, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))
    notes = daemon.import_legacy_logs(store, d)
    assert notes == ["events.jsonl was already imported; removed it"]
    assert store.totals() == (len(rows), len(_REAL_PROMPT_MESSAGES))


def test_the_mirror_format_matches_a_line_the_pre_607_daemon_really_printed():
    """Captured from the owner's launchd.tcc-watch.err.log on 2026-09-26 (the pre-#607 daemon
    printed f"tcc-watch: permission-prompt event detected: {event.message!r} (pid=..., process=...)").
    Stripping is verified against this format, so it must match history, not just itself."""
    captured = ("tcc-watch: permission-prompt event detected: 'SEND: 0/7 synchronous to com.apple.tccd.system:"
                " request: msgID=517.35048, function=TCCAccessRequest, service=kTCCServiceDeveloperTool,"
                " target_token={pid:8864, auid:501, euid:501},' (pid=517, process=/usr/libexec/syspolicyd)")
    row = {"message": "SEND: 0/7 synchronous to com.apple.tccd.system: request: msgID=517.35048, "
                      "function=TCCAccessRequest, service=kTCCServiceDeveloperTool, "
                      "target_token={pid:8864, auid:501, euid:501},",
           "pid": 517, "process": "/usr/libexec/syspolicyd"}
    assert daemon._LEGACY_MIRROR.format(**row) == captured
    assert _mirror(row) == captured + "\n"


def test_an_interrupted_strip_survives_err_log_having_been_deleted(store):
    d = store.path.parent
    (d / f"{daemon.LEGACY_ERR_LOG_NAME}.kept").write_text("kept line\n", encoding="utf-8")
    daemon.import_legacy_logs(store, d)
    assert (d / daemon.LEGACY_ERR_LOG_NAME).read_text(encoding="utf-8") == "kept line\n"


def test_import_strips_err_log_mirror_lines_only_once_all_are_verified(store):
    d = store.path.parent
    rows = _legacy_rows(3)
    _seed_legacy(d, "events.jsonl", rows)
    err = d / daemon.LEGACY_ERR_LOG_NAME
    traceback = "Traceback (most recent call last):\nOSError: [Errno 28] No space left on device\n"
    # the last row has no mirror line: the old daemon died between the append and the print
    err.write_text("".join(_mirror(r) for r in rows[:10]) + traceback + "".join(_mirror(r) for r in rows[10:-1]),
                   encoding="utf-8")
    notes = daemon.import_legacy_logs(store, d)
    assert err.read_text(encoding="utf-8") == traceback
    assert notes[-1] == f"err.log: dropped {len(rows) - 1} verified duplicate event lines, kept 2 other lines"


def test_import_leaves_err_log_alone_when_a_mirror_line_has_no_row(store):
    d = store.path.parent
    rows = _legacy_rows(3)
    _seed_legacy(d, "events.jsonl", rows)
    err = d / daemon.LEGACY_ERR_LOG_NAME
    stray = _mirror(_legacy_row("AttributionChain: never recorded"))
    content = "".join(_mirror(r) for r in rows[:5]) + stray + "".join(_mirror(r) for r in rows[5:])
    err.write_text(content, encoding="utf-8")
    notes = daemon.import_legacy_logs(store, d)
    assert err.read_text(encoding="utf-8") == content
    assert notes[-1].startswith(f"err.log: 5 of {len(rows) + 1} old event lines matched")


@pytest.mark.parametrize("value", ["3", "null", "not json{", "[1, 2]"])
def test_a_malformed_import_record_never_crashes_the_import(store, value):
    d = store.path.parent
    rows = _legacy_rows(2)
    path = _seed_legacy(d, "events.jsonl", rows)
    store.db.execute("INSERT INTO meta (key, value) VALUES ('legacy_import:events.jsonl', ?)", (value,))
    store.db.commit()
    notes = daemon.import_legacy_logs(store, d)
    assert notes == [f"imported events.jsonl: {len(rows)} lines (5 prompts/denials); removed it"]
    assert not path.exists()


def test_an_interrupted_first_start_is_resumed_and_the_strip_finished_later(store):
    """First start imports events.jsonl.1 and is stopped before events.jsonl: the verified
    mirror count is committed with .1, so the next start resumes there and strips err.log."""
    d = store.path.parent
    older, newer = _legacy_rows(2), _legacy_rows(3)
    _seed_legacy(d, "events.jsonl.1", older)
    _seed_legacy(d, "events.jsonl", newer)
    err = d / daemon.LEGACY_ERR_LOG_NAME
    err.write_text("".join(_mirror(r) for r in older) + "keep me\n" + "".join(_mirror(r) for r in newer),
                   encoding="utf-8")
    parked = d / "parked"
    (d / "events.jsonl").rename(parked)  # the first start never got to it
    notes = daemon.import_legacy_logs(store, d)
    assert notes[-1].endswith("left it as is") and "keep me" in err.read_text(encoding="utf-8")
    assert daemon._verified_mirror_lines(store) == len(older)
    parked.rename(d / "events.jsonl")
    notes = daemon.import_legacy_logs(store, d)
    assert err.read_text(encoding="utf-8") == "keep me\n"
    assert notes[-1] == f"err.log: dropped {len(older) + len(newer)} verified duplicate event lines, kept 1 other line"
    assert daemon._verified_mirror_lines(store) == 0
    assert store.totals() == (len(older) + len(newer), 2 * len(_REAL_PROMPT_MESSAGES))


def test_a_verified_strip_that_failed_is_retried_with_no_legacy_files_left(store):
    d = store.path.parent
    rows = _legacy_rows(2)
    err = d / daemon.LEGACY_ERR_LOG_NAME
    err.write_text("".join(_mirror(r) for r in rows) + "Traceback ...\n", encoding="utf-8")
    store.db.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (daemon._MIRROR_VERIFIED_KEY, str(len(rows))))
    store.db.commit()
    notes = daemon.import_legacy_logs(store, d)
    assert err.read_text(encoding="utf-8") == "Traceback ...\n"
    assert notes[-1].startswith(f"err.log: dropped {len(rows)} verified duplicate event lines")


def test_a_file_growing_during_its_import_is_not_imported(store, monkeypatch):
    d = store.path.parent
    path = _seed_legacy(d, "events.jsonl", _legacy_rows())
    real = daemon._file_identity
    calls = {"n": 0}

    def growing(p):
        calls["n"] += 1
        identity = real(p)
        return {**identity, "size": identity["size"] + 100} if calls["n"] > 1 else identity

    monkeypatch.setattr(daemon, "_file_identity", growing)
    notes = daemon.import_legacy_logs(store, d)
    assert notes == ["could not import events.jsonl (the file changed while it was being imported);"
                     " kept it and the files after it"]
    assert path.exists() and store.totals() == (0, 0)


def test_a_failed_file_stops_the_import_so_no_unimported_line_is_ever_verified(store, monkeypatch):
    """.3 walks its mirror lines and then rolls back: .2 must not commit a verified count that
    includes them, or a later start (with .3 gone) would strip lines whose rows were never stored."""
    d = store.path.parent
    oldest, older = _legacy_rows(2), _legacy_rows(3)
    _seed_legacy(d, "events.jsonl.3", oldest)
    _seed_legacy(d, "events.jsonl.2", older)
    err = d / daemon.LEGACY_ERR_LOG_NAME
    content = "".join(_mirror(r) for r in oldest + older)
    err.write_text(content, encoding="utf-8")
    real_totals = daemon.TccStore.totals
    calls = {"n": 0}

    def first_file_off_by_one(self):
        calls["n"] += 1
        lines, prompts = real_totals(self)
        return (lines + 1, prompts) if calls["n"] == 2 else (lines, prompts)

    monkeypatch.setattr(daemon.TccStore, "totals", first_file_off_by_one)
    notes = daemon.import_legacy_logs(store, d)
    monkeypatch.setattr(daemon.TccStore, "totals", real_totals)
    assert notes[0] == ("could not import events.jsonl.3 (store totals do not match the file);"
                        " kept it and the files after it")
    assert (d / "events.jsonl.3").exists() and (d / "events.jsonl.2").exists()  # .2 not attempted
    assert daemon._verified_mirror_lines(store) == 0
    (d / "events.jsonl.3").unlink()  # even if the failed file then disappears...
    daemon.import_legacy_logs(store, d)
    assert err.read_text(encoding="utf-8") == content  # ...its lines are never "verified" away


def test_readonly_store_never_creates_a_file(tmp_path):
    missing = tmp_path / "gone" / daemon.STORE_NAME
    with pytest.raises(sqlite3.OperationalError):
        daemon.TccStore(missing, readonly=True)
    assert not missing.exists() and not missing.parent.exists()


def test_import_with_no_legacy_files_is_a_no_op(store):
    assert daemon.import_legacy_logs(store, store.path.parent) == []


# ── the watch loop, fully injected (no subprocess, no real log stream) ─────────────────────
