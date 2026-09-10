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
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from ..model import ToolInvocation

_CACHE_VERSION = 1


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
    """One JSON file per source (``<home>/.cache/rig/stats/<source>.json``). Loaded once
    per ``rig stats`` run, mutated in memory as files are (re)parsed, and written back once
    via :meth:`flush` -- a single read and at most a single write per invocation, never one
    per session file.

    A write failure (read-only filesystem, disk full, ...) must never break ``rig stats``
    itself: the cache is a pure perf optimization, so every I/O op here degrades silently.
    """

    def __init__(self, home: Path, source_name: str) -> None:
        self._path = home / ".cache" / "rig" / "stats" / f"{source_name}.json"
        self._dirty = False
        self._entries: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict) or raw.get("version") != _CACHE_VERSION:
            return
        entries = raw.get("files")
        if isinstance(entries, dict):
            self._entries = entries

    def get(self, session_file: Path) -> list[ToolInvocation] | None:
        """The cached, UNFILTERED invocation list for ``session_file`` -- only if its
        ``(mtime_ns, size)`` still match what was cached. ``None`` on any miss."""
        try:
            st = session_file.stat()
        except OSError:
            return None
        entry = self._entries.get(str(session_file))
        if entry is None:
            return None
        if entry.get("mtime_ns") != st.st_mtime_ns or entry.get("size") != st.st_size:
            return None
        rows = entry.get("invocations")
        if not isinstance(rows, list):
            return None
        try:
            return [_deserialize(row) for row in rows]
        except (KeyError, ValueError, TypeError):
            return None

    def put(self, session_file: Path, invocations: list[ToolInvocation]) -> None:
        """Record the fresh, UNFILTERED parse of ``session_file``. Silently skipped if the
        file vanished between the parse and this call (nothing useful to key it by)."""
        try:
            st = session_file.stat()
        except OSError:
            return
        self._entries[str(session_file)] = {
            "mtime_ns": st.st_mtime_ns,
            "size": st.st_size,
            "invocations": [_serialize(inv) for inv in invocations],
        }
        self._dirty = True

    def flush(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(
                json.dumps({"version": _CACHE_VERSION, "files": self._entries}),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except OSError:
            pass
