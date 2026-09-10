"""Tests for the per-source session-file cache (``riglib.stats.sources._cache``).

``rig stats show`` walks every session log file on the machine on every invocation; the
cache in ``_cache.py`` makes a REPEAT run skip re-parsing/re-categorizing any session file
whose ``(mtime_ns, size)`` are unchanged since the last run. These tests exercise the real
production path (``collect`` -> each source's ``iter_invocations`` -> ``SessionFileCache``),
never the cache class in isolation, so a regression in the wiring (not just the cache
class itself) would fail them -- except where a test's whole point is inspecting the raw
on-disk row (pruning, no-accumulation, gzip), which necessarily reaches into the SQLite
file directly.
"""

from __future__ import annotations

import gzip
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from riglib.stats.command import collect
from riglib.stats.model import ToolInvocation
from riglib.stats.sources._cache import SessionFileCache

from test_stats import write_claude_session, _cc_event
from test_stats_omp_source import write_omp_session, _omp_session_event, _omp_tool_event


def _db_path(home: Path, source: str) -> Path:
    return home / ".cache" / "rig" / "stats" / f"{source}.sqlite3"


def _row_count(db_path: Path, path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM entries WHERE path = ?", (str(path),)
        ).fetchone()[0]
    finally:
        conn.close()


def test_warm_cache_run_matches_cold_run_exactly(tmp_path):
    """A second `collect()` over unchanged files returns the identical invocation stream
    as the first (cold) call -- the cache must never drop, duplicate, or reorder data."""
    home = tmp_path / "home"
    write_claude_session(
        home, "-Users-ultra-xp-demo", "s1", "/Users/ultra/xp/demo",
        [_cc_event("2026-07-29T10:01:00Z", "/Users/ultra/xp/demo",
                   [{"name": "Bash", "input": {"command": "ls"}}])],
    )
    write_omp_session(
        home, "-x", "s2",
        [_omp_session_event("/r"),
         _omp_tool_event("2026-07-29T10:01:00Z", [{"name": "read", "arguments": {}}])],
    )
    cold = collect(home=home)
    assert len(cold.invocations) == 2  # sanity: both harnesses actually parsed
    warm = collect(home=home)
    assert warm.invocations == cold.invocations


def test_cache_detects_a_grown_session_file(tmp_path):
    """A session file that grows between two `collect()` calls (an in-progress session
    still being appended to) must be re-parsed, not served a stale/truncated cached list."""
    home = tmp_path / "home"
    session_file = write_omp_session(
        home, "-x", "s1",
        [_omp_session_event("/r"),
         _omp_tool_event("2026-07-29T10:01:00Z", [{"name": "bash", "arguments": {"command": "ls"}}])],
    )
    first = collect(home=home)
    assert len(first.invocations) == 1

    with session_file.open("a", encoding="utf-8") as fh:
        fh.write(_omp_tool_event("2026-07-29T10:02:00Z", [{"name": "read", "arguments": {}}]) + "\n")

    second = collect(home=home)
    assert len(second.invocations) == 2
    assert {inv.tool_name for inv in second.invocations} >= {"Bash", "Read"}


def test_cache_entry_is_reusable_across_different_repo_filters(tmp_path):
    """The cache stores the FULL unfiltered parse of a file, so a warm cache still answers
    a --repo query it was never asked before -- it must not have discarded out-of-scope
    invocations the first time they were (unfiltered) cached."""
    home = tmp_path / "home"
    for encoded, cwd in (("-repo-a", "/repo/a"), ("-repo-b", "/repo/b")):
        write_omp_session(
            home, encoded, "s",
            [_omp_session_event(cwd),
             _omp_tool_event("2026-07-29T10:01:00Z", [{"name": "read", "arguments": {}}])],
        )
    collect(home=home)  # warms the cache with the unfiltered parse of both sessions

    only_a = collect(home=home, repos=["/repo/a"])
    assert {inv.repo for inv in only_a.invocations} == {"/repo/a"}

    only_b = collect(home=home, repos=["/repo/b"])
    assert {inv.repo for inv in only_b.invocations} == {"/repo/b"}


def test_corrupted_cache_file_degrades_to_a_full_reparse(tmp_path):
    """A cache db must be a pure perf optimization: if it's corrupted (partial write,
    disk issue, hand-edited), `rig stats` still returns correct data -- it just misses the
    cache and reparses, exactly like a cold run."""
    home = tmp_path / "home"
    write_omp_session(
        home, "-x", "s1",
        [_omp_session_event("/r"),
         _omp_tool_event("2026-07-29T10:01:00Z", [{"name": "bash", "arguments": {"command": "ls"}}])],
    )
    collect(home=home)  # writes a real cache db
    cache_file = _db_path(home, "omp")
    assert cache_file.is_file()
    cache_file.write_bytes(b"not a sqlite database at all, just garbage bytes")

    result = collect(home=home)
    assert len(result.invocations) == 1


def test_cache_get_put_round_trip_preserves_invocation_fields(tmp_path):
    """Direct unit coverage of the (de)serialization boundary the sources rely on."""
    home = tmp_path / "home"
    session_file = home / "fake-session.jsonl"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text("irrelevant\n", encoding="utf-8")

    inv = ToolInvocation(
        timestamp=datetime(2026, 7, 29, 10, 1, tzinfo=timezone.utc),
        harness="omp",
        repo="/repo/a",
        session="s1",
        tool_name="Bash",
        category="baseline",
        raw_tool="bash",
        detail="ls",
    )
    cache = SessionFileCache(home, "fake")
    assert cache.get(session_file) is None
    cache.put(session_file, [inv])

    reloaded = SessionFileCache(home, "fake")
    got = reloaded.get(session_file)
    assert got == [inv]


def test_deleted_session_file_row_is_pruned_on_next_collect(tmp_path):
    """Issue #505: this cache must not grow unbounded. It never touches the session/log
    file itself (that stays the untouched source of truth), but a row DERIVED from a file
    that's gone (deleted/rotated/moved) has nothing left to ever validate or serve again,
    so a later `flush()` (run once per source, every `collect()`) prunes it."""
    home = tmp_path / "home"
    session_file = write_omp_session(
        home, "-x", "s1",
        [_omp_session_event("/r"),
         _omp_tool_event("2026-07-29T10:01:00Z", [{"name": "bash", "arguments": {"command": "ls"}}])],
    )
    collect(home=home)  # warms the cache
    assert _row_count(_db_path(home, "omp"), session_file) == 1

    session_file.unlink()
    assert session_file.parent.exists()  # sanity: only the file is gone, not its directory

    collect(home=home)  # a fresh collect() call -> cache.flush() prunes the orphaned row

    assert _row_count(_db_path(home, "omp"), session_file) == 0
    assert not session_file.exists(), "pruning must never recreate/touch the source file"


def test_reparsing_a_grown_file_replaces_its_row_not_accumulates(tmp_path):
    """A genuine re-parse of the SAME path (mtime/size changed) must upsert its row, never
    accumulate a second row for that path -- the cache stays bounded by "one row per file
    currently on disk", not "one row per parse"."""
    home = tmp_path / "home"
    session_file = write_omp_session(
        home, "-x", "s1",
        [_omp_session_event("/r"),
         _omp_tool_event("2026-07-29T10:01:00Z", [{"name": "bash", "arguments": {"command": "ls"}}])],
    )
    collect(home=home)
    with session_file.open("a", encoding="utf-8") as fh:
        fh.write(_omp_tool_event("2026-07-29T10:02:00Z", [{"name": "read", "arguments": {}}]) + "\n")
    collect(home=home)  # re-parses the grown file

    assert _row_count(_db_path(home, "omp"), session_file) == 1


def test_cache_row_payload_is_gzip_compressed(tmp_path):
    """The stored payload must actually be gzip -- the whole point of switching off the
    per-file-JSON design (issue #505's SQLite + gz correction) is compressing what lands
    on disk, not just moving the same raw JSON into a different container."""
    home = tmp_path / "home"
    session_file = home / "fake-session.jsonl"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text("irrelevant\n", encoding="utf-8")

    inv = ToolInvocation(
        timestamp=datetime(2026, 7, 29, 10, 1, tzinfo=timezone.utc),
        harness="omp", repo="/repo/a", session="s1",
        tool_name="Bash", category="baseline", raw_tool="bash", detail="ls",
    )
    cache = SessionFileCache(home, "fake")
    cache.put(session_file, [inv])

    conn = sqlite3.connect(str(_db_path(home, "fake")))
    try:
        blob = conn.execute(
            "SELECT data FROM entries WHERE path = ?", (str(session_file),)
        ).fetchone()[0]
    finally:
        conn.close()
    assert blob[:2] == b"\x1f\x8b", "row payload must be gzip-compressed"
    assert gzip.decompress(blob)  # round-trips back to valid bytes
