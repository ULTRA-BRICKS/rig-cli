"""Per-source on-disk cache of parsed session files, keyed by file identity.

``rig stats show`` walks every session log file on the machine, on every single
invocation, to build the tool-adoption report -- including the categorize/taxonomy work
(``riglib.stats.taxonomy.categorize``, which quote-aware-tokenizes every shell command).
Once a harness session ends, its log file is append-only-then-frozen: the file never
changes again. Re-reading + re-JSON-decoding + re-categorizing every line of every
historical session on every run is pure waste -- the exact same "full corpus scan" redone
from scratch each time, even though almost none of the corpus changed since the last run.

This cache stores the FULLY-PARSED, UNFILTERED invocation list for each source file
(never a ``--repo``/``--harness``/``--since``-scoped subset), so a cache entry is reusable
for ANY future query -- filtering by repo/harness/date stays a cheap in-memory step applied
AFTER a cache hit. A hit requires the file's ``(mtime_ns, size)`` to match exactly what was
cached; a new file, a grown/rewritten file (the handful of sessions still in progress), or
a first-ever run are misses and pay the real parse cost for just that one file.

STORAGE + PRUNING (issue #505): the first cut of this cache (PR #503) was one JSON file
per source, the WHOLE file rewritten on every ``flush()``, and NOTHING ever removed an
entry once written -- it reached 88MB after a single cold run on a 275k-invocation
machine, and would keep growing forever as session logs got rotated elsewhere on disk.

The original fix proposed for #505 was pruning on flush. Alex's first review pass on that
pushed back -- "why delete, we keep logs to use them" -- and asked for the storage engine
to switch to SQLite + gzip (mirroring review-cli's own file-identity-keyed parse cache,
``reviewlib/dashboard/call_log_cache.py``) instead. A follow-up correction clarified the
actual distinction that matters:

    "так если это просто кэш то его надо будет в результате удалить чтобы он не валялся
    со старым ненужным грузом и не забивал диск"

    (if it's just a cache, it eventually needs pruning so old unneeded load doesn't sit
    around and fill the disk.)

So: the underlying session/log files are the real source of truth and are NEVER touched
or deleted by this module -- only what's stored here, in a cache DERIVED from them, is
ever pruned. A row is dropped once its session file is confirmed gone from disk (deleted,
rotated, moved) -- exactly review-cli's own ``save()`` behavior on its parse cache, applied
here on ``flush()`` (called once per source after its per-file loop) rather than needing a
directory glob, since a stored row's own path is the thing to check.

Storage itself mirrors review-cli's cache design: SQLite, one row per file, an indexed
lookup that never pays to deserialize the rest of the history, WAL journal mode (safe
across a concurrent second ``rig stats`` process), autocommit (one ``put`` is one
transaction, so a mid-run crash keeps whatever was already committed instead of losing the
lot), and a 0600 db file (a row can hold repo paths and command details from private
session logs, same as every other piece of log-derived data this repo persists). Each
row's JSON payload is additionally gzip-compressed before it hits disk: review-cli's own
cache doesn't need this (one row there is a single parsed call, already small); a row here
is a WHOLE SESSION's invocation list, which for a long-running session can run to tens of
KB of repetitive JSON, so gzip meaningfully shrinks what actually lands on disk.
"""

from __future__ import annotations

import gzip
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from ..model import ToolInvocation

# Bump whenever this module's ROW SHAPE or (de)serialization changes in a way that would
# make an already-stored row unreadable or wrong for the current code -- e.g. a new
# `ToolInvocation` field, a change to `_serialize`/`_deserialize`. A row whose (mtime_ns,
# size) still match the file but whose `cache_version` doesn't is treated as a miss and
# re-parsed, same as a genuine content change -- see `get()`. Independent of the old
# per-file-JSON cache's own `_CACHE_VERSION` (PR #503): that format lived in a `.json`
# file, this one in a `.sqlite3` file, so there is no shared numbering to stay compatible
# with.
_CACHE_VERSION = 1

# `sqlite3.Error` is not the only way this can fail: a genuinely corrupt db file, a
# permissions problem, or a decompress/decode failure on a hand-edited row all need the
# same "just treat it as a cache miss" handling -- mirrored from review-cli's own
# `_CACHE_ERRORS`.
_CACHE_ERRORS = (sqlite3.Error, OSError, ValueError, TypeError, KeyError)


def _serialize(inv: ToolInvocation) -> dict:
    return {
        "timestamp": inv.timestamp.isoformat() if inv.timestamp is not None else None,
        "harness": inv.harness,
        "repo": inv.repo,
        "session": inv.session,
        "tool_name": inv.tool_name,
        "category": inv.category,
        "raw_tool": inv.raw_tool,
        "detail": inv.detail,
    }


def _deserialize(row: dict) -> ToolInvocation:
    ts = row.get("timestamp")
    return ToolInvocation(
        timestamp=datetime.fromisoformat(ts) if isinstance(ts, str) else None,
        harness=str(row["harness"]),
        repo=str(row["repo"]),
        session=str(row["session"]),
        tool_name=str(row["tool_name"]),
        category=str(row["category"]),
        raw_tool=str(row["raw_tool"]),
        detail=str(row.get("detail", "")),
    )


class SessionFileCache:
    """One SQLite db per source (``<home>/.cache/rig/stats/<source>.sqlite3``), one row
    per session file, keyed by its full path. See the module docstring for the full
    design rationale (SQLite + gzip mirrored from review-cli's own parse cache; pruning
    of orphaned rows on ``flush()``, per Alex's #505 correction -- this is a cache, the
    session/log files it's derived from are the untouched source of truth).

    ``get``/``put`` are never fatal to the caller: any storage failure (missing db,
    corrupt file, a bad row) is treated as a cache miss, exactly like the pre-SQLite
    cache's "persistence is a pure perf optimization" contract.
    """

    def __init__(self, home: Path, source_name: str) -> None:
        self._path = home / ".cache" / "rig" / "stats" / f"{source_name}.sqlite3"
        # The pre-#505 per-file JSON cache (PR #503) lived at this same directory under
        # `<source>.json`. It's a disposable perf cache, never user data, so dropping a
        # leftover copy in the old format is safe -- worst case, it costs one more cold
        # parse per file it used to cover. Best-effort: a failure here must not block
        # opening the new (`.sqlite3`) cache.
        self._stale_json_path = home / ".cache" / "rig" / "stats" / f"{source_name}.json"
        self._conn: sqlite3.Connection | None = None
        self._connect()

    def _connect(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self._stale_json_path.unlink()
            except OSError:
                pass
            # check_same_thread=False + isolation_level=None (autocommit): each source
            # only ever uses its `SessionFileCache` from the single thread that created
            # it today, but matching review-cli's connection settings costs nothing and
            # means this module doesn't silently break if that ever changes. Autocommit
            # also means every `put()` is already durable -- `flush()`'s only remaining
            # job is pruning.
            conn = sqlite3.connect(
                str(self._path), timeout=30.0, isolation_level=None, check_same_thread=False
            )
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS entries ("
                "path TEXT PRIMARY KEY, mtime_ns INTEGER NOT NULL, "
                "size INTEGER NOT NULL, cache_version INTEGER NOT NULL, "
                "data BLOB NOT NULL)"
            )
            # Unconditional, every connect (not just on first creation) -- matches
            # review-cli's own reasoning: WAL's `-wal`/`-shm` sidecars hold the newest
            # cached rows (repo paths, command details from private session logs) and are
            # recreated at umask-default permissions on every fresh open.
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.chmod(f"{self._path}{suffix}", 0o600)
                except OSError:
                    pass
            self._conn = conn
        except _CACHE_ERRORS:
            self._conn = None

    def get(self, session_file: Path) -> list[ToolInvocation] | None:
        """The cached, UNFILTERED invocation list for ``session_file`` -- only if its
        ``(mtime_ns, size)`` still match what was cached. ``None`` on any miss, including
        a ``session_file`` that no longer exists (its row, if any, is left for the next
        ``flush()`` to prune -- there's no live file left to validate freshness against,
        so it can never be served as a hit)."""
        if self._conn is None:
            return None
        try:
            st = session_file.stat()
        except OSError:
            return None
        try:
            row = self._conn.execute(
                "SELECT mtime_ns, size, cache_version, data FROM entries WHERE path = ?",
                (str(session_file),),
            ).fetchone()
        except _CACHE_ERRORS:
            return None
        if row is None:
            return None
        mtime_ns, size, cache_version, blob = row
        if mtime_ns != st.st_mtime_ns or size != st.st_size or cache_version != _CACHE_VERSION:
            return None
        try:
            rows = json.loads(gzip.decompress(blob).decode("utf-8"))
        except _CACHE_ERRORS:
            return None
        if not isinstance(rows, list):
            return None
        try:
            return [_deserialize(r) for r in rows]
        except _CACHE_ERRORS:
            return None

    def put(self, session_file: Path, invocations: list[ToolInvocation]) -> None:
        """Record the fresh, UNFILTERED parse of ``session_file``. Silently skipped if the
        file vanished between the parse and this call (nothing useful to key it by). An
        existing row for this same path is replaced (upsert), never duplicated."""
        try:
            st = session_file.stat()
        except OSError:
            return
        payload = gzip.compress(
            json.dumps([_serialize(inv) for inv in invocations]).encode("utf-8")
        )
        if self._conn is None:
            return
        try:
            self._conn.execute(
                "INSERT INTO entries (path, mtime_ns, size, cache_version, data) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET "
                "mtime_ns = excluded.mtime_ns, size = excluded.size, "
                "cache_version = excluded.cache_version, data = excluded.data",
                (str(session_file), st.st_mtime_ns, st.st_size, _CACHE_VERSION, payload),
            )
        except _CACHE_ERRORS:
            pass

    def flush(self) -> None:
        """Prune rows whose session file no longer exists on disk. This module caches
        parsed session data -- it never touches the session/log files themselves, which
        remain the untouched source of truth -- but the cache itself must stay bounded:
        a row for a file that's gone (deleted, rotated, moved) has nothing left to ever
        validate or serve again, so it's dropped here rather than accumulating forever
        (issue #505). Every `put()` already committed (autocommit), so this is pruning
        only, never required for durability."""
        if self._conn is None:
            return
        try:
            rows = self._conn.execute("SELECT path FROM entries").fetchall()
        except _CACHE_ERRORS:
            return
        stale = [path for (path,) in rows if not Path(path).exists()]
        if not stale:
            return
        try:
            self._conn.executemany(
                "DELETE FROM entries WHERE path = ?", [(path,) for path in stale]
            )
        except _CACHE_ERRORS:
            pass
