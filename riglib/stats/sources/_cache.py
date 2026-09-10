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

STORAGE (issue #505): the first cut of this cache (PR #503) was one JSON file per source,
the WHOLE file rewritten on every ``flush()``. That JSON file only ever grew -- nothing
ever removed an entry once written, so it reached 88MB after a single cold run on a
275k-invocation machine, and would keep growing forever as session logs got rotated
elsewhere on disk.

The originally filed fix for #505 was to prune: drop any entry whose file no longer
exists, on every flush. Alex's review correction rejected that outright:

    "А зачем удалять? Там же может быть что-то полезное, нет? Мы же для этого и храним
    логи чтобы пользоваться ими зачем удалять? Кажется вместо этого надо сделать так как
    мы сделали в ревью CLI перешли на SQLite + gz"

    (why delete? there might be something useful there. We keep logs specifically so we
    can use them -- why delete? Seems like instead we should do what we did in review-cli:
    switch to SQLite + gz.)

So instead of pruning, this module now mirrors review-cli's own file-identity-keyed parse
cache (``reviewlib/dashboard/call_log_cache.py``, which caches parsed ``CallLog`` objects
the same way, keyed the same way, for the exact same "don't re-parse a file whose content
can't have changed" reason): SQLite, one row per file, an indexed lookup that never pays
to deserialize the rest of the history, WAL journal mode (safe across a concurrent second
``rig stats`` process), autocommit (one ``put`` is one transaction, so a mid-run crash
keeps whatever was already committed instead of losing the lot), and a 0600 db file (a row
can hold repo paths and command details from private session logs, same as every other
piece of log-derived data this repo persists). The one deliberate difference from
review-cli's version: **this cache never prunes**. review-cli's ``save()`` deletes rows for
files no longer on disk; here, a session file being deleted or rotated away must not erase
its cached parse -- the whole point, per Alex's correction, is that the log content stored
in a row can still be useful after the source file is gone. A ``get()`` for a path whose
file no longer exists still safely misses (there is no ``stat()`` left to validate
freshness against, so it can never be *served* as a hit), but the row itself is retained,
not deleted. The only thing that ever replaces a row is a genuine re-parse of the SAME path
-- its ``(mtime_ns, size)`` changed, or ``_CACHE_VERSION`` was bumped -- via
``INSERT ... ON CONFLICT DO UPDATE``: an upsert, never an accumulate-forever append.

Additionally, each row's JSON payload is gzip-compressed before it hits disk. review-cli's
own cache doesn't need this (one row there is a single parsed call, already small); a row
here is a WHOLE SESSION's invocation list, which for a long-running session can run to tens
of KB of repetitive JSON, so gzip meaningfully shrinks what actually lands on disk.
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
# same "just treat it as a cache miss" handling -- see the module docstring's "never
# fatal to the caller" contract, mirrored from review-cli's `_CACHE_ERRORS`.
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
    design rationale (SQLite + gzip, mirrored from review-cli's own parse cache; no
    pruning, per Alex's #505 correction).

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
            # also means every `put()` is already durable -- `flush()` has nothing left
            # to do.
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
        a ``session_file`` that no longer exists -- its row (if any) is retained, but
        there is no live file left to validate freshness against, so it can never be
        served as a hit."""
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
        """No-op: every ``put()`` already commits (autocommit / ``isolation_level=None``),
        so there is nothing pending to persist. Kept as a public method -- every source
        calls it once after its per-file loop -- purely so this stays a drop-in
        replacement for the pre-SQLite cache, whose ``flush()`` did the one-shot JSON
        rewrite this design no longer needs."""
        return
