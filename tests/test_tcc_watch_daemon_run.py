"""tcc-watch tests: the watch loop, daemon hardening and failure paths."""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from riglib import tcc_watch
from riglib import tcc_watch_daemon as daemon
from tests.tcc_watch_helpers import (
    _REAL_PROMPT_MESSAGES,
    _ROUTINE_MESSAGES,
    _T0,
    _counts,
    _FailingDb,
    _FakeStreamProc,
    _legacy_row,
    _legacy_rows,
    _mirror,
    _obs,
    _reopen,
    _seed_legacy,
    _seeded_store,
    _store_path,
    _tcc_line,
    _TerminatingDb,
    _WriteThenTerminateDb,
    open_store,
    restore_real_tcc_watch,
)


@pytest.fixture
def store(tmp_path):
    yield from open_store(tmp_path)


@pytest.fixture(autouse=True)
def _real_tcc_watch(monkeypatch, live_launchd_home):
    restore_real_tcc_watch(monkeypatch)


def test_run_counts_every_line_stores_prompts_and_never_echoes_them(tmp_path, capsys):
    """rig-cli#607: every event used to be printed to stderr too (launchd's err.log, 2.0 GB)."""
    path = _store_path(tmp_path)
    lines = [_tcc_line(m) for m in _ROUTINE_MESSAGES] * 20
    lines += [_tcc_line(m) for _, _, m in _REAL_PROMPT_MESSAGES]
    lines += ["Filtering the log data using ...", "{bad"]
    seen = daemon.run(path, stream_factory=lambda: _FakeStreamProc(lines), sleep=lambda _s: None, max_reconnects=1)
    assert seen == 20 * len(_ROUTINE_MESSAGES) + len(_REAL_PROMPT_MESSAGES)
    s = _reopen(path)
    assert s.totals() == (seen, len(_REAL_PROMPT_MESSAGES))
    s.db.close()
    err = capsys.readouterr().err
    assert err.count("\n") == 2  # "started" + "log stream exited" — diagnostics only
    for _, _, message in _REAL_PROMPT_MESSAGES:
        assert message[:30] not in err


def test_run_imports_the_legacy_log_before_the_stream_starts(tmp_path):
    path = _store_path(tmp_path)
    rows = _legacy_rows(2)
    legacy = _seed_legacy(path.parent, "events.jsonl", rows)
    at_spawn = []

    def factory():
        at_spawn.append(legacy.exists())
        return _FakeStreamProc([_tcc_line(_ROUTINE_MESSAGES[0])])

    daemon.run(path, stream_factory=factory, sleep=lambda _s: None, max_reconnects=1)
    assert at_spawn == [False]
    s = _reopen(path)
    assert s.totals() == (len(rows) + 1, len(_REAL_PROMPT_MESSAGES))
    notes = [r[0] for r in s.db.execute("SELECT message FROM diagnostics ORDER BY id")]
    s.db.close()
    assert any(n.startswith("imported events.jsonl:") for n in notes)


def test_run_flushes_buffered_counts_when_terminated(tmp_path):
    """SIGTERM (launchd bootout) raises SystemExit mid-stream; the counts still land."""
    path = _store_path(tmp_path)

    def stream():
        yield from [_tcc_line(_ROUTINE_MESSAGES[0])] * 7
        daemon._terminate(15, None)

    with pytest.raises(SystemExit):
        daemon.run(path, stream_factory=lambda: _FakeStreamProc(stream()), sleep=lambda _s: None)
    s = _reopen(path)
    assert s.totals() == (7, 0)
    s.db.close()


def test_run_reconnects_after_the_stream_exits(tmp_path):
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        return _FakeStreamProc([_tcc_line(_REAL_PROMPT_MESSAGES[0][2])] if calls["n"] == 2 else [])

    sleeps: list[float] = []
    seen = daemon.run(_store_path(tmp_path), stream_factory=factory, sleep=sleeps.append, max_reconnects=2)
    assert calls["n"] == 2
    assert seen == 1
    assert sleeps


def test_run_backs_off_exponentially_on_spawn_failure(tmp_path):
    attempts = {"n": 0}

    def factory():
        attempts["n"] += 1
        raise OSError("log: command not found")

    sleeps: list[float] = []
    daemon.run(_store_path(tmp_path), stream_factory=factory, sleep=sleeps.append, max_reconnects=8)
    assert attempts["n"] == 8
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]


def test_run_backs_off_when_the_stream_keeps_exiting_immediately(tmp_path):
    """A crash-looping `log stream` must not write a diagnostic line every second."""
    sleeps: list[float] = []
    daemon.run(
        _store_path(tmp_path),
        stream_factory=lambda: _FakeStreamProc([], returncode=64),
        sleep=sleeps.append,
        clock=lambda: 0.0,
        max_reconnects=5,
    )
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0]


def test_run_resets_the_backoff_after_a_healthy_stream(tmp_path):
    ticks = iter(range(0, 10_000, 100))  # every clock() call is 100s later
    sleeps: list[float] = []
    daemon.run(
        _store_path(tmp_path),
        stream_factory=lambda: _FakeStreamProc([]),
        sleep=sleeps.append,
        clock=lambda: float(next(ticks)),
        max_reconnects=4,
    )
    assert sleeps == [1.0, 1.0, 1.0, 1.0]


def test_run_survives_a_failing_store_write(tmp_path, monkeypatch, capsys):
    def locked(self, obs, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(daemon.TccStore, "observe", locked)
    seen = daemon.run(
        _store_path(tmp_path),
        stream_factory=lambda: _FakeStreamProc([_tcc_line(_REAL_PROMPT_MESSAGES[0][2])]),
        sleep=lambda _s: None,
        max_reconnects=1,
    )
    assert seen == 0
    err = capsys.readouterr().err
    assert "failed to record a prompting line (kTCCServiceMediaLibrary): database is locked" in err
    assert "AUTHREQ_PROMPTING" not in err  # prompt text never goes to launchd's err.log


# ── diagnostics: store always, stderr while small, never truncated ─────────────────────────
def test_log_err_records_in_the_store_and_on_stderr(store, tmp_path):
    err = tmp_path / "err.log"
    with err.open("a", encoding="utf-8") as fh:
        daemon._log_err("hello", store, stream=fh)
    assert err.read_text(encoding="utf-8") == "tcc-watch: hello\n"
    assert store.db.execute("SELECT message FROM diagnostics").fetchall() == [("hello",)]


def test_log_err_stops_writing_to_a_full_stderr_file_but_never_truncates_it(store, tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "MAX_STDERR_BYTES", 100)
    err = tmp_path / "err.log"
    err.write_text("x" * 150, encoding="utf-8")
    with err.open("a", encoding="utf-8") as fh:
        daemon._log_err("dropped from stderr", store, stream=fh)
    assert err.read_text(encoding="utf-8") == "x" * 150
    assert store.db.execute("SELECT message FROM diagnostics").fetchall() == [("dropped from stderr",)]


def test_stderr_has_room_for_pipes(tmp_path):
    r, w = os.pipe()
    with os.fdopen(w, "w") as pipe:
        assert daemon.stderr_has_room(pipe, 0) is True
    os.close(r)


def test_log_err_never_raises(tmp_path, monkeypatch, capsys):
    class _FullDisk:
        def fileno(self):
            raise OSError(28, "No space left on device")

        def write(self, _text):
            raise OSError(28, "No space left on device")

        def flush(self):
            raise OSError(28, "No space left on device")

    daemon._log_err("boom", stream=_FullDisk())
    closed = (tmp_path / "closed.log").open("a", encoding="utf-8")
    closed.close()
    daemon._log_err("boom", stream=closed)
    broken = daemon.TccStore(tmp_path / "s.sqlite3")
    broken.db.close()  # any write now raises sqlite3.ProgrammingError (a sqlite3.Error)
    daemon._log_err("boom", broken, stream=closed)
    monkeypatch.setattr(sys, "stderr", None)
    daemon._log_err("nowhere to go")
    assert capsys.readouterr().out == ""


# ── hardening: bounded cardinality, all-or-nothing writes, crash-safe upgrade ───────────────
def test_store_folds_new_templates_past_the_cap_into_one_overflow_row(store, monkeypatch):
    """A process logging under com.apple.TCC with non-constant format strings must not grow the
    store without bound: past MAX_TEMPLATES, new keys share one overflow template per kind."""
    monkeypatch.setattr(daemon, "MAX_TEMPLATES", 3)
    for i in range(50):
        store.observe(_obs(f"dynamic line {i}", fmt=f"dynamic format {i}"))
    store.flush()
    formats = [r[0] for r in store.db.execute("SELECT format FROM templates ORDER BY id")]
    assert formats == ["dynamic format 0", "dynamic format 1", "dynamic format 2", daemon.OVERFLOW_TEMPLATE]
    assert store.totals() == (50, 0)
    overflow = store.db.execute(
        "SELECT SUM(c.n) FROM counts c JOIN templates t ON t.id = c.template_id WHERE t.format = ?",
        (daemon.OVERFLOW_TEMPLATE,),
    ).fetchone()[0]
    assert overflow == 47


def test_legacy_template_folds_digits():
    assert daemon._legacy_template("SEND: 0/7 synchronous") == "SEND"
    assert daemon._legacy_template("msgID 12345 from pid 42") == "msgID # from pid #"


def test_a_failed_maintain_rolls_back_and_never_double_counts(store):
    old_day = _T0 - 20 * 86400 - _T0 % 86400
    for _ in range(4):
        store.observe(_obs(_ROUTINE_MESSAGES[0], epoch=old_day + 3600))
    store.flush()
    real = store.db
    store.db = _FailingDb(real, "DELETE FROM counts")  # the rollup INSERT succeeds, the DELETE fails
    with pytest.raises(sqlite3.OperationalError):
        store.maintain()
    store.db = real
    store.diagnostic("a later commit must not persist the half-done rollup")
    assert store.totals() == (4, 0)
    store.maintain()
    assert store.totals() == (4, 0)
    assert _counts(store) == [(old_day, 86400, "%{public}s", "com.example.chat", 4)]


def test_a_failed_count_write_is_rolled_back_not_replayed(store):
    store.observe(_obs(_ROUTINE_MESSAGES[0]))
    real = store.db
    store.db = _FailingDb(real, "INSERT INTO counts")
    with pytest.raises(sqlite3.OperationalError):
        store.flush()
    store.db = real
    store.flush()  # nothing left to replay: the failed batch was dropped, not half-applied
    assert store.totals() == (0, 0)
    store.observe(_obs(_ROUTINE_MESSAGES[0]))
    store.flush()
    assert store.totals() == (1, 0)


def test_run_survives_a_failing_startup_import(tmp_path, monkeypatch, capsys):
    def broken(*_args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(daemon, "import_legacy_logs", broken)
    seen = daemon.run(
        _store_path(tmp_path),
        stream_factory=lambda: _FakeStreamProc([_tcc_line(_ROUTINE_MESSAGES[0])]),
        sleep=lambda _s: None,
        max_reconnects=1,
    )
    assert seen == 1
    assert "startup import/maintenance failed: database is locked" in capsys.readouterr().err


def test_import_keeps_a_file_that_vanishes_mid_way(store, monkeypatch):
    d = store.path.parent
    path = _seed_legacy(d, "events.jsonl", _legacy_rows())

    def gone(_path):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(daemon, "_file_identity", gone)
    notes = daemon.import_legacy_logs(store, d)
    assert path.exists()
    assert notes == ["could not import events.jsonl ([Errno 2] No such file or directory); kept it and the files after it"]
    assert store.totals() == (0, 0)


def test_import_from_a_second_connection_sees_the_first_import(store):
    """Two daemons on one store: the identity check runs inside the IMMEDIATE transaction."""
    d = store.path.parent
    rows = _legacy_rows()
    path = _seed_legacy(d, "events.jsonl", rows)
    copy = d / "copy"
    copy.write_bytes(path.read_bytes())
    identity = path.stat()
    daemon.import_legacy_logs(store, d)
    copy.rename(path)
    os.utime(path, ns=(identity.st_atime_ns, identity.st_mtime_ns))
    other = daemon.TccStore(store.path)
    assert daemon.import_legacy_logs(other, d) == ["events.jsonl was already imported; removed it"]
    assert other.totals() == (len(rows), len(_REAL_PROMPT_MESSAGES))
    other.db.close()


def test_an_unreadable_err_log_is_reported_and_left_alone(store, monkeypatch):
    d = store.path.parent
    _seed_legacy(d, "events.jsonl", _legacy_rows(2))
    err = d / daemon.LEGACY_ERR_LOG_NAME
    err.write_text("tcc-watch: permission-prompt event detected: 'x' (pid=1, process=None)\n", encoding="utf-8")
    real_open = Path.open

    def deny(self, *args, **kwargs):
        if self == err:
            raise PermissionError(13, "Permission denied")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny)
    notes = daemon.import_legacy_logs(store, d)
    monkeypatch.setattr(Path, "open", real_open)
    assert notes[0].startswith("err.log: could not read it")
    assert err.read_text(encoding="utf-8").startswith("tcc-watch: permission-prompt event detected")


def test_an_interrupted_strip_is_finished_on_the_next_start(store):
    d = store.path.parent
    err = d / daemon.LEGACY_ERR_LOG_NAME
    err.write_text("", encoding="utf-8")  # killed right after the truncate
    (d / f"{daemon.LEGACY_ERR_LOG_NAME}.kept").write_text("Traceback ...\nOSError: [Errno 28]\n", encoding="utf-8")
    notes = daemon.import_legacy_logs(store, d)
    assert err.read_text(encoding="utf-8") == "Traceback ...\nOSError: [Errno 28]\n"
    assert not (d / f"{daemon.LEGACY_ERR_LOG_NAME}.kept").exists()
    assert notes == ["err.log: finished an interrupted strip of verified duplicate event lines"]


def test_a_strip_killed_before_the_truncate_leaves_err_log_untouched(store):
    d = store.path.parent
    err = d / daemon.LEGACY_ERR_LOG_NAME
    err.write_text("original\n", encoding="utf-8")
    (d / f"{daemon.LEGACY_ERR_LOG_NAME}.stripping").write_text("partial", encoding="utf-8")
    assert daemon.import_legacy_logs(store, d) == []
    assert err.read_text(encoding="utf-8") == "original\n"
    assert not (d / f"{daemon.LEGACY_ERR_LOG_NAME}.stripping").exists()


def test_the_template_cache_stays_bounded_past_the_cap(store, monkeypatch):
    monkeypatch.setattr(daemon, "MAX_TEMPLATES", 3)
    for i in range(500):
        store.observe(_obs(f"dynamic line {i}", fmt=f"dynamic format {i}"))
    assert len(store._template_ids) == 4  # 3 real keys + the overflow key, not 500


def test_a_prompt_flood_is_trimmed_between_maintain_runs(store, monkeypatch):
    monkeypatch.setattr(daemon, "MAX_PROMPT_ROWS", 10)
    monkeypatch.setattr(daemon, "PROMPT_TRIM_EVERY", 5)
    for i in range(100):
        store.observe(_obs(f"Policy disallows prompt for Sub:{{x}}; access to kTCCServicePhotos denied #{i}"))
    assert store.db.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] <= 10 + 5
    newest = store.db.execute("SELECT message FROM prompts ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert newest.endswith("#99")


def test_sigterm_during_the_legacy_import_commits_nothing(store, monkeypatch):
    """SystemExit (SIGTERM) is not an Exception: the import must still roll back, or run()'s
    `finally: store.close()` would commit half a file and the retry would count it twice."""
    d = store.path.parent
    path = _seed_legacy(d, "events.jsonl", _legacy_rows())
    calls = {"n": 0}
    real = daemon._legacy_template

    def killed_midway(message):
        calls["n"] += 1
        if calls["n"] == 40:
            daemon._terminate(15, None)
        return real(message)

    monkeypatch.setattr(daemon, "_legacy_template", killed_midway)
    with pytest.raises(SystemExit):
        daemon.import_legacy_logs(store, d)
    store.close()
    again = daemon.TccStore(store.path)
    assert again.totals() == (0, 0)
    assert again.db.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'legacy_import:%'").fetchone()[0] == 0
    again.db.close()
    assert path.exists()


def test_sigterm_during_maintain_commits_no_half_rollup(store):
    old_day = _T0 - 20 * 86400 - _T0 % 86400
    for _ in range(4):
        store.observe(_obs(_ROUTINE_MESSAGES[0], epoch=old_day + 3600))
    store.flush()
    real = store.db
    store.db = _TerminatingDb(real, "DELETE FROM counts")
    with pytest.raises(SystemExit):
        store.maintain()
    store.db = real
    store.close()  # what run()'s `finally` does
    again = daemon.TccStore(store.path)
    assert again.totals() == (4, 0)
    assert again.db.execute("SELECT span FROM counts").fetchall() == [(3600,)]
    again.db.close()


def test_sigterm_right_after_a_count_write_does_not_replay_it(store):
    for _ in range(3):
        store.observe(_obs(_ROUTINE_MESSAGES[0]))
    real = store.db
    store.db = _WriteThenTerminateDb(real, "INSERT INTO counts")
    with pytest.raises(SystemExit):
        store.flush()
    store.db = real
    store.flush()  # run()'s `finally`
    assert store.totals() == (3, 0)


def test_err_log_is_kept_unless_every_legacy_file_was_imported_in_this_run(store, monkeypatch):
    d = store.path.parent
    older, newer = _legacy_rows(2), _legacy_rows(3)
    _seed_legacy(d, "events.jsonl.1", older)
    _seed_legacy(d, "events.jsonl", newer)
    err = d / daemon.LEGACY_ERR_LOG_NAME
    content = "".join(_mirror(r) for r in older + newer)
    err.write_text(content, encoding="utf-8")
    real_totals = daemon.TccStore.totals
    calls = {"n": 0}

    def second_file_off_by_one(self):
        calls["n"] += 1
        lines, prompts = real_totals(self)
        return (lines + 1, prompts) if calls["n"] == 4 else (lines, prompts)

    monkeypatch.setattr(daemon.TccStore, "totals", second_file_off_by_one)
    notes = daemon.import_legacy_logs(store, d)
    assert (d / "events.jsonl").exists() and not (d / "events.jsonl.1").exists()
    assert err.read_text(encoding="utf-8") == content
    assert notes[-1].endswith("left it as is")


def test_report_reads_while_the_daemon_holds_the_write_lock(tmp_path, capsys):
    """`report` during a long legacy import must answer, not wait 30 s and traceback."""
    path = _seeded_store(tmp_path)
    writer = daemon.TccStore(path)
    writer.db.execute("BEGIN IMMEDIATE")
    writer.db.execute("INSERT INTO diagnostics (ts, message) VALUES ('t', 'importing')")
    started = time.monotonic()
    assert daemon.main(["report", "--store", str(path), "--json"]) == 0
    assert time.monotonic() - started < 5
    assert json.loads(capsys.readouterr().out)["lines"] == 6
    writer.rollback()
    writer.db.close()


def test_run_maintains_the_store_on_its_hourly_cadence(tmp_path, monkeypatch):
    calls = []
    real = daemon.TccStore.maintain

    def counting(self):
        calls.append(1)
        real(self)

    monkeypatch.setattr(daemon.TccStore, "maintain", counting)
    ticks = iter(range(0, 10**7, 4000))  # every clock() call is 4000 s later
    daemon.run(
        _store_path(tmp_path),
        stream_factory=lambda: _FakeStreamProc([_tcc_line(_ROUTINE_MESSAGES[0])] * 3),
        sleep=lambda _s: None,
        clock=lambda: float(next(ticks)),
        max_reconnects=1,
    )
    assert len(calls) == 1 + 3  # once at startup, then on every line an hour+ apart


def test_file_names_agree_with_the_provisioning_side():
    """The daemon cannot import riglib, so the shared names are pinned here instead."""
    assert daemon.STORE_NAME == tcc_watch.STORE_NAME
    assert daemon.LEGACY_ERR_LOG_NAME == tcc_watch.ERR_LOG_NAME
    assert daemon.OUT_LOG_NAME == tcc_watch.OUT_LOG_NAME
    assert tcc_watch.WATCHER_SCRIPT_NAME == "watch.py"


def test_an_import_with_more_prompts_than_the_cap_still_verifies(store, monkeypatch):
    """The per-insert trim must not fire inside the import's own transaction (it would make the
    prompt count never match and the file un-importable forever); maintain() trims afterwards."""
    monkeypatch.setattr(daemon, "MAX_PROMPT_ROWS", 10)
    monkeypatch.setattr(daemon, "PROMPT_TRIM_EVERY", 5)
    d = store.path.parent
    rows = [_legacy_row(f"display_prompt: called for <private> for service kTCCServicePhotos #{i}", i)
            for i in range(30)]
    path = _seed_legacy(d, "events.jsonl", rows)
    notes = daemon.import_legacy_logs(store, d)
    assert notes == ["imported events.jsonl: 30 lines (30 prompts/denials); removed it"]
    assert not path.exists()
    store.maintain()
    assert store.db.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 10
    assert store.totals()[0] == 30  # every line is still counted


def test_run_reports_a_failing_flush_as_such_and_keeps_going(tmp_path, monkeypatch, capsys):
    def broken_flush(self):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(daemon.TccStore, "flush", broken_flush)
    ticks = iter(range(0, 10**7, 100))
    seen = daemon.run(
        _store_path(tmp_path),
        stream_factory=lambda: _FakeStreamProc([_tcc_line(_ROUTINE_MESSAGES[0])] * 2),
        sleep=lambda _s: None,
        clock=lambda: float(next(ticks)),
        max_reconnects=1,
    )
    assert seen == 2
    err = capsys.readouterr().err
    assert "store flush/maintenance failed: disk I/O error" in err
    assert "failed to record" not in err


@pytest.mark.parametrize("argv", [["--verbose"], ["--store", "x"], ["a.sqlite3", "b.sqlite3"]])
def test_daemon_rejects_flags_and_extra_arguments(tmp_path, monkeypatch, capsys, argv):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(daemon, "run", lambda _p: pytest.fail("must not start a watcher"))
    assert daemon.main(argv) == 2
    assert "usage: watch.py" in capsys.readouterr().err
    assert list(cwd.iterdir()) == []


def test_daemon_help_prints_usage_and_creates_nothing(tmp_path, monkeypatch, capsys):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    assert daemon.main(["--help"]) == 0
    assert "watch.py report" in capsys.readouterr().out
    assert list(cwd.iterdir()) == []


def test_default_store_lives_in_the_provisioned_config_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    expected = tcc_watch.build_tcc_watch(home=tmp_path, python_bin="/usr/bin/python3").store_path
    assert daemon._default_store() == expected


def test_a_literal_overflow_format_string_cannot_bypass_the_cap(store, monkeypatch):
    monkeypatch.setattr(daemon, "MAX_TEMPLATES", 2)
    for i in range(2):
        store.observe(_obs(f"line {i}", fmt=f"format {i}"))
    for i in range(50):
        store.observe(_obs(f"identifier=com.example.app{i} x", fmt=daemon.OVERFLOW_TEMPLATE))
    assert store.db.execute("SELECT COUNT(*) FROM templates").fetchone()[0] == 3
    assert len(store._template_ids) == 3


def test_template_key_fields_are_length_capped():
    obs = daemon.parse_observation(_tcc_line("identifier=" + "a" * 5000, fmt="F" * 5000, category="c" * 5000))
    assert len(obs.template) == daemon.MAX_KEY_CHARS
    assert len(obs.category) == daemon.MAX_KEY_CHARS
    assert len(obs.subject) == daemon.MAX_KEY_CHARS


def test_store_files_are_owner_only(tmp_path):
    path = tmp_path / "s" / daemon.STORE_NAME
    path.parent.mkdir()
    path.write_bytes(b"")
    os.chmod(path, 0o644)  # a store created before the fix
    s = daemon.TccStore(path)
    s.observe(_obs(_ROUTINE_MESSAGES[0]))
    s.flush()
    daemon._owner_only(path)
    for member in path.parent.iterdir():
        assert member.stat().st_mode & 0o777 == 0o600, member.name
    s.db.close()


def test_a_diagnostic_inside_an_open_import_does_not_commit_it(store):
    store.db.execute("BEGIN IMMEDIATE")
    store.observe(_obs(_ROUTINE_MESSAGES[0]), commit=False)
    store.write_pending()
    store.diagnostic("progress note in the middle of an import")
    store.rollback()  # the import fails: nothing of it — the note included — may survive
    assert store.totals() == (0, 0)
    assert store.db.execute("SELECT COUNT(*) FROM diagnostics").fetchone()[0] == 0


def test_report_on_an_unopenable_store_exits_cleanly(tmp_path, monkeypatch, capsys):
    path = _seeded_store(tmp_path)

    def unopenable(*_args, **_kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(daemon.sqlite3, "connect", unopenable)
    assert daemon.main(["report", "--store", str(path)]) == 1
    assert "cannot read" in capsys.readouterr().err


def test_diagnostics_are_stamped_with_the_store_clock(store):
    store.diagnostic("hello")
    assert store.db.execute("SELECT ts FROM diagnostics").fetchone() == ("2026-09-15T15:00:00+00:00",)


def test_report_on_a_corrupt_store_fails_cleanly(tmp_path, capsys):
    path = tmp_path / daemon.STORE_NAME
    path.write_text("this is not a database, it is an events.jsonl renamed by hand\n" * 50, encoding="utf-8")
    assert daemon.main(["report", "--store", str(path)]) == 1
    assert "cannot read" in capsys.readouterr().err


def test_report_clamps_negative_arguments(tmp_path, capsys):
    path = _seeded_store(tmp_path)
    assert daemon.main(["report", "--store", str(path), "--json", "--top", "-5", "--days", "-1"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert len(summary["top_templates"]) == 1
    assert summary["lines"] == 6


def test_a_prompt_and_noise_sharing_a_format_are_counted_apart(store):
    """kind is part of the template key: a generic formatString must not blur the per-kind share."""
    store.observe(_obs("display_prompt: called for <private>", fmt="%{public}s"))
    store.observe(_obs("display_prompt-like, but not a prompt", fmt="%{public}s"))
    summary = store.summary(since=_T0)
    assert {r["kind"]: r["lines"] for r in summary["by_kind"]} == {"prompt_displayed": 1, "noise": 1}


def test_overflow_is_one_row_per_kind_whatever_the_sender_varies(store, monkeypatch):
    """Process name and category are sender-controlled too: they must not mint overflow rows."""
    monkeypatch.setattr(daemon, "MAX_TEMPLATES", 2)
    for i in range(2):
        store.observe(_obs(f"line {i}", fmt=f"format {i}"))
    for i in range(60):
        obs = _obs(f"line {i}", fmt=f"more {i}", process=f"/tmp/proc{i}")
        store.observe(dataclasses.replace(obs, category=f"cat{i}"))
    store.observe(_obs(_REAL_PROMPT_MESSAGES[0][2], fmt="prompt fmt"))
    rows = store.db.execute("SELECT process, category, format, kind FROM templates ORDER BY id").fetchall()
    assert rows[2:] == [("*", "", daemon.OVERFLOW_TEMPLATE, "noise"), ("*", "", daemon.OVERFLOW_TEMPLATE, "prompting")]
    assert store.db.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 1  # the prompt is still kept in full


def test_open_store_moves_a_non_database_aside_and_starts_fresh(tmp_path, capsys):
    path = _store_path(tmp_path)
    path.write_text("an events.jsonl renamed by hand\n" * 100, encoding="utf-8")
    # WAL files left by the store that used to be here: SQLite would pair them with a new file
    (path.parent / f"{path.name}-wal").write_bytes(b"\x37\x7f\x06\x82" + b"\0" * 60)
    (path.parent / f"{path.name}-shm").write_bytes(b"\0" * 64)
    sleeps: list[float] = []
    s = daemon.open_store(path, sleep=sleeps.append)
    assert s.totals() == (0, 0)
    s.db.close()
    assert sleeps == [1.0]  # backed off once, never spun
    # the old WAL files are either moved aside with it or were already dropped by SQLite; either
    # way they are not paired with the new store (which just opened cleanly)
    aside = [p.name for p in path.parent.iterdir() if ".unreadable-" in p.name]
    assert set(n.split(".unreadable-")[0] for n in aside) <= {path.name, f"{path.name}-wal", f"{path.name}-shm"}
    kept = next(p for p in path.parent.iterdir() if p.name.startswith(f"{path.name}.unreadable-"))
    assert kept.read_text(encoding="utf-8").startswith("an events.jsonl")
    assert "starting a new store" in capsys.readouterr().err


def test_open_store_retries_a_locked_or_failing_store_with_backoff(tmp_path, monkeypatch):
    real = daemon.TccStore
    attempts = {"n": 0}

    def flaky(path):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return real(path)

    monkeypatch.setattr(daemon, "TccStore", flaky)
    sleeps: list[float] = []
    s = daemon.open_store(_store_path(tmp_path), sleep=sleeps.append)
    assert sleeps == [1.0, 2.0]
    s.db.close()


def test_the_store_directory_and_launchd_logs_are_owner_only(tmp_path):
    d = tmp_path / "tcc-watch"
    d.mkdir(mode=0o755)
    err = d / daemon.LEGACY_ERR_LOG_NAME
    err.write_text("x\n", encoding="utf-8")
    os.chmod(err, 0o644)
    s = daemon.TccStore(d / daemon.STORE_NAME)
    assert d.stat().st_mode & 0o777 == 0o700
    assert err.stat().st_mode & 0o777 == 0o600
    s.db.close()


# ── main + report ──────────────────────────────────────────────────────────────────────────
def test_main_maps_an_old_events_jsonl_argument_to_the_store(tmp_path, monkeypatch):
    """A plist written before #607 still passes <config_dir>/events.jsonl."""
    got = []
    monkeypatch.setattr(daemon, "run", lambda path: got.append(path))
    handled = []
    monkeypatch.setattr(daemon.signal, "signal", lambda signum, _handler: handled.append(signum))
    umasks = []
    monkeypatch.setattr(daemon.os, "umask", umasks.append)  # never the test process's own umask
    daemon.main([str(tmp_path / "events.jsonl")])
    daemon.main([str(tmp_path / "custom.sqlite3")])
    assert got == [tmp_path / daemon.STORE_NAME, tmp_path / "custom.sqlite3"]
    assert umasks == [0o077, 0o077]
    assert handled == [daemon.signal.SIGTERM, daemon.signal.SIGINT] * 2  # Ctrl-C exits as cleanly as bootout
