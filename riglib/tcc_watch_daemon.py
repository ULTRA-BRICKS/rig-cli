"""tcc-watch daemon — Phase 1 (DETECTION-ONLY) macOS TCC permission-prompt observability.

## What this is

The actual long-running process a `tcc-watch` LaunchAgent execs (see :mod:`riglib.tcc_watch`
for the provisioning side). It tails macOS's own unified log stream for the ``com.apple.TCC``
subsystem (the daemon — ``tccd`` — that decides and prompts for every
Photos/Contacts/Calendar/Screen-Recording/Full-Disk-Access/Automation/etc access grant) and
keeps two things in one small SQLite store (``tcc-watch.sqlite3`` next to this script):

- every REAL permission prompt or denial, in full (the ``prompts`` table);
- a COUNT of every TCC log line, per hour, per message template (the ``counts`` +
  ``templates`` tables) — so "what is tccd busy with, which app drives it, since when" stays
  answerable without storing millions of near-identical lines.

``python3 watch.py report`` prints both (``--json`` for machines).

This is Phase 1 of the phased design (rig-cli ticket referenced in
:mod:`riglib.tcc_watch`'s module docstring) — SEE THAT TICKET for the full rationale. Phase 1 is
deliberately narrow:

- observe and record only;
- it does NOT touch the actual system dialog (no auto-dismiss/click — that is Phase 2, gated on
  a separate design review because driving the Accessibility API to interact with a live system
  UI element is a materially higher-risk action than reading a log stream);
- it does NOT read or write ``TCC.db``;
- it does NOT yet attribute the event to a specific agent process or send any notification
  (Phases 3-4).

## Why `log stream`, not the Accessibility API, for detection

`log stream --predicate 'subsystem == "com.apple.TCC"'` is Apple's own sanctioned, documented
mechanism for observing TCC activity (`man log`) — no private API, no TCC.db access, no
Accessibility permission needed just to detect that a request happened. It is the same
technique published by Apple-admin tooling vendors (Omnissa/Filewave) for TCC troubleshooting.
The ``--debug`` flag is required: tccd's per-request detail lines are at debug level and are
suppressed by a plain ``log stream`` without it.

## Why counts + templates instead of a line log (rig-cli#607)

The first cut appended every line containing ``kTCCService``/``AttributionChain`` to
``events.jsonl`` AND printed it to stderr. Under ``--debug`` that is almost all of tccd's
routine traffic — preflight ``SEND:``/``REQUEST_MSG:``/``AUTHREQ_CTX:``, ``AttributionChain:``
records, entitlement checks, read-only ``TCCAccessCopy*`` queries: 5.28M lines / 2.1 GB (plus a
2.0 GB stderr copy) in 16 days on a real Mac, of which only 212 were real prompts or denials.

Each ``log stream --style ndjson`` record carries ``formatString`` — the os_log format the line
was rendered from. That (plus the process, category, the ``kTCCService*`` named and the
subject app's identifier) is the template key; a line becomes ``counts[hour, template] += 1``.
The template keeps its first rendered line as an example. Real prompts/denials — the lines whose
``eventMessage`` starts with one of :data:`PROMPT_SIGNATURES` — are ALSO stored in full:

- ``AUTHREQ_PROMPTING:`` — tccd decided to show a consent dialog (names service + requester);
- ``display_prompt:`` — the dialog is on screen;
- ``CFUserNotification response:`` — the user answered it (``0x0``/``0x1`` + service + subject);
- ``Policy disallows prompt for`` — a request that would need a prompt was denied outright;
- ``Update Access Record: kTCCService`` — a grant/denial was written (dialog or System Settings).

The signatures are tccd's wording as captured on macOS 26.5 (2026-09). A release that rewords
them does not lose data — the lines are still counted under their templates — but they would
no longer be copied into ``prompts``; after a major macOS upgrade, check ``report`` for new
prompt-looking templates.

## Bounded by design

- ``counts``: hourly rows for :data:`HOURLY_RETENTION_DAYS`, then rolled up into one row per
  UTC day per template, kept forever — the rows grow with the number of distinct templates
  active per day (capped, see below), never with the line volume.
- ``templates``: past :data:`MAX_TEMPLATES`, new keys share one overflow template per kind
  (something logging under com.apple.TCC with non-constant format strings, categories or
  process names cannot grow it); every key field is length-capped.
- ``prompts``: the newest :data:`MAX_PROMPT_ROWS` rows, trimmed every :data:`PROMPT_TRIM_EVERY`
  inserts; ``diagnostics``: the newest :data:`MAX_DIAGNOSTIC_ROWS`.
- stderr (launchd's ``StandardErrorPath``) gets diagnostics only, never events, and nothing
  once that file is past :data:`MAX_STDERR_BYTES`. Diagnostics also go to the store — except
  the ones about the store itself failing to open, which only stderr can carry.
  It is never truncated: whatever is in it stays.
- Counts are buffered in memory and written at the first line at least :data:`FLUSH_SECONDS`
  after the previous write, and on exit; if tccd goes quiet, at most that last minute of counts
  waits in memory for the next line (or SIGTERM). Prompts are written immediately. A multi-statement step (a legacy import, a rollup) is all or
  nothing, even when SIGTERM interrupts it — nothing is ever counted twice.

## Upgrading from the line log

The import runs before the first ``log stream`` spawn — minutes for a multi-GB file — so a
prompt shown during that one-time upgrade window is not recorded.

At startup, the pre-#607 ``events.jsonl`` family next to the store is imported — prompts in
full, every line counted under a coarse legacy template in its day — each file in ONE
transaction that commits only if the store's totals grew by exactly the file's line count; only
then is the file removed. launchd's err.log is stripped of the old per-event mirror lines only
when EVERY one of them equals, byte for byte, the line the old daemon printed for an imported
row; any other line in it is kept.

## Why this runs as a separate privileged daemon, not inside the eval-tool sandbox

This script is invoked directly by a machine-wide macOS LaunchAgent (`riglib.tcc_watch`'s
plist), started once at login and kept alive by launchd — exactly like `tg-ctl`
(`riglib/tg_ctl.py`) and the model-freshness `schedule` (`riglib/schedule.py`). It is NOT part
of the sandboxed eval-tool subprocess any running agent invokes, and it does not run inside any
particular agent's process tree. `osascript`/System-Events introspection is banned for the
*eval tool* specifically because an agent-controlled subprocess driving that is itself a
plausible sandbox-escape vector; this watchdog is the opposite shape — a rig-provisioned,
human-owned, always-on background process that outlives and is independent of every individual
agent session, observing the OS from the outside. The distinction is WHO controls the process,
not which OS API it calls.

## Design for testability without a live TCC dialog

:func:`parse_observation` is pure; :class:`TccStore` takes a path (``tmp_path`` in tests);
:func:`run` accepts a `stream_factory` returning anything with a `.stdout` iterable of lines and
a `.wait()` + `.returncode`, injectable `sleep`/`clock`, and a `max_reconnects` bound so a test
can make the infinite watch loop terminate deterministically. Tests replay captured log lines —
never a real TCC prompt.

Stdlib-only. This file is copied byte-for-byte to the machine (see
`TccWatchPlan.render_watcher_script`) and executed directly via `python3 <path>`, so it must
never depend on the rest of the `riglib` package being importable at runtime.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, NamedTuple, Protocol, TextIO

# (kind, eventMessage prefix) for every tccd line that marks a real prompt or denial.
PROMPT_SIGNATURES: tuple[tuple[str, str], ...] = (
    ("prompting", "AUTHREQ_PROMPTING:"),
    ("prompt_displayed", "display_prompt:"),
    ("prompt_answered", "CFUserNotification response:"),
    ("denied_without_prompt", "Policy disallows prompt for"),
    ("access_record_updated", "Update Access Record: kTCCService"),
)
NOISE = "noise"

# `--debug` is REQUIRED: tccd's per-request detail lines are debug-level and are silently
# dropped by a plain `log stream` predicate match without it.
LOG_STREAM_CMD = [
    "log", "stream",
    "--style", "ndjson",
    "--debug",
    "--predicate", 'subsystem == "com.apple.TCC"',
]

STORE_NAME = "tcc-watch.sqlite3"
LEGACY_EVENTS_NAME = "events.jsonl"
LEGACY_BACKUPS = 3  # the pre-#607 rotation depth: events.jsonl.1 .. .3 — history, do not change
LEGACY_ERR_LOG_NAME = "launchd.tcc-watch.err.log"
OUT_LOG_NAME = "launchd.tcc-watch.out.log"
# meta key: how many err.log mirror lines are verified against committed rows (see import_legacy_logs)
_MIRROR_VERIFIED_KEY = "legacy_err_log_verified_lines"
_MIRROR_PREFIX = "tcc-watch: permission-prompt event detected: "
_LEGACY_MIRROR = _MIRROR_PREFIX + "{message!r} (pid={pid}, process={process})"

HOUR = 3600
DAY = 86400
HOURLY_RETENTION_DAYS = 14
# Distinct templates are normally a few hundred. Past this cap (something logging under
# com.apple.TCC with non-constant format strings, categories or process names), new keys are
# counted under one overflow template per kind, so the store stays bounded whatever the input.
MAX_TEMPLATES = 20_000
OVERFLOW_TEMPLATE = "(over the template limit)"
MAX_PROMPT_ROWS = 20_000
# ...and trimmed back to it every this many prompt inserts, not only by the hourly maintain().
PROMPT_TRIM_EVERY = 500
MAX_DIAGNOSTIC_ROWS = 1_000
MAX_MESSAGE_CHARS = 4096
MAX_EXAMPLE_CHARS = 300
MAX_KEY_CHARS = 512  # each template key field (formatString, subject, ...)
FLUSH_SECONDS = 60.0
MAINTAIN_SECONDS = 3600.0
# stderr is launchd's StandardErrorPath (err.log): no more writes once it is past this.
MAX_STDERR_BYTES = 1024 * 1024
# A `log stream` that ran at least this long was healthy: the reconnect backoff resets. One
# that exits sooner doubles the backoff (capped), so a crash-looping stream reconnects (and
# logs) once a minute, not once a second.
HEALTHY_STREAM_SECONDS = 30.0
MAX_BACKOFF_SECONDS = 60.0

_SERVICE_RE = re.compile(r"kTCCService\w+")
# The TCC call a request line carries (`function=TCCAccessRequest`, `function="TCCAccessCopyInformation"`)
# and whether it is a preflight (a check that never prompts).
_FUNCTION_RE = re.compile(r'function="?([A-Za-z_]+)')
_PREFLIGHT_RE = re.compile(r"preflight=(?:yes|true)")
# The app a line is about: `identifier=com.x.y` (TCCDProcess records) or `Sub:{com.x.y}` /
# `Sub:{/path/to/binary}` (prompt/denial subjects).
_SUBJECT_RE = re.compile(r"(?:identifier=|Sub:\{)([A-Za-z0-9._/\-]+)")
# A legacy row has no formatString: its template is the message's leading token, digits folded.
_LEGACY_HEAD_RE = re.compile(r"^[<\-\[]?[^:{(<=,\n\]]{1,60}")
_DIGITS_RE = re.compile(r"\d+")

# Recorded in meta at creation, so a later layout change can tell which layout a store has.
SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY,
    process TEXT NOT NULL,
    category TEXT NOT NULL,
    format TEXT NOT NULL,
    service TEXT NOT NULL,
    function TEXT NOT NULL,
    subject TEXT NOT NULL,
    kind TEXT NOT NULL,
    example TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    UNIQUE (process, category, format, service, function, subject, kind)
);
CREATE TABLE IF NOT EXISTS counts (
    bucket INTEGER NOT NULL,
    span INTEGER NOT NULL,
    template_id INTEGER NOT NULL REFERENCES templates(id),
    n INTEGER NOT NULL,
    PRIMARY KEY (bucket, span, template_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS prompts (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    service TEXT,
    process_path TEXT,
    pid INTEGER,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS diagnostics (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, message TEXT NOT NULL);
"""
_UPSERT_COUNT = (
    "INSERT INTO counts (bucket, span, template_id, n) VALUES (?, ?, ?, ?) "
    "ON CONFLICT (bucket, span, template_id) DO UPDATE SET n = n + excluded.n"
)


def classify_message(message: str) -> str | None:
    """The :data:`PROMPT_SIGNATURES` kind whose prefix ``message`` starts with, else ``None``."""
    for kind, prefix in PROMPT_SIGNATURES:
        if message.startswith(prefix):
            return kind
    return None


@dataclass(frozen=True)
class Observation:
    """One TCC log line, reduced to its template key plus what a prompt row needs."""

    epoch: int | None
    timestamp: str
    process: str
    category: str
    template: str
    service: str
    function: str
    subject: str
    kind: str
    pid: int | None
    process_path: str | None
    message: str

    @property
    def key(self) -> tuple[str, str, str, str, str, str, str]:
        return (self.process, self.category, self.template, self.service, self.function, self.subject, self.kind)

    @property
    def is_prompt(self) -> bool:
        return self.kind != NOISE


def _epoch(timestamp: object) -> int | None:
    """``2026-09-26 15:21:15.068951+0200`` → unix seconds, else ``None``."""
    if not isinstance(timestamp, str):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S%z"):
        try:
            return int(datetime.strptime(timestamp, fmt).timestamp())
        except ValueError:
            continue
    return None


def _observe(*, message: str, template: str, category: str, process_path: object,
             timestamp: object, pid: object) -> Observation:
    path = process_path if isinstance(process_path, str) and process_path else None
    service = _SERVICE_RE.search(message)
    function = _FUNCTION_RE.search(message)
    subject = _SUBJECT_RE.search(message)
    call = " ".join(
        part for part in (function.group(1) if function else "", "preflight" if _PREFLIGHT_RE.search(message) else "")
        if part
    )
    return Observation(
        epoch=_epoch(timestamp),
        timestamp=timestamp[:64] if isinstance(timestamp, str) else "",
        process=(path.rsplit("/", 1)[-1] if path else "?")[:MAX_KEY_CHARS],
        category=category[:MAX_KEY_CHARS],
        template=template[:MAX_KEY_CHARS],
        service=service.group(0)[:MAX_KEY_CHARS] if service else "",
        function=call[:MAX_KEY_CHARS],
        subject=subject.group(1)[:MAX_KEY_CHARS] if subject else "",
        kind=classify_message(message) or NOISE,
        pid=pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
        process_path=path[:MAX_MESSAGE_CHARS] if path else None,
        message=message[:MAX_MESSAGE_CHARS],
    )


def parse_observation(line: str) -> Observation | None:
    """Parse one `log stream --style ndjson` output line into an :class:`Observation`.

    ``None`` for anything that is not a well-formed ``com.apple.TCC`` record — including a
    malformed/partial line (a buffered flush boundary) and `log stream`'s own non-JSON
    ``Filtering the log data ...`` banner. Never raises: one bad line must not kill the daemon.
    """
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict) or obj.get("subsystem") != "com.apple.TCC":
        return None
    message = obj.get("eventMessage")
    if not isinstance(message, str):
        message = ""
    template = obj.get("formatString")
    if not isinstance(template, str) or not template:
        template = _legacy_template(message)
    category = obj.get("category")
    return _observe(
        message=message,
        template=template,
        category=category if isinstance(category, str) else "",
        process_path=obj.get("processImagePath") or obj.get("process"),
        timestamp=obj.get("timestamp"),
        pid=obj.get("processID"),
    )


def _legacy_template(message: str) -> str:
    head = _LEGACY_HEAD_RE.match(message)
    return _DIGITS_RE.sub("#", head.group(0).strip()) if head else "(empty)"


def _owner_only(path: Path) -> None:
    """The store holds TCC decisions (apps, services, grants): owner-only, like TCC.db itself —
    its directory, its WAL files and launchd's logs beside it. New files get 0600 from main()'s
    umask; this also fixes files created before that."""
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    siblings = (f"{path.name}-wal", f"{path.name}-shm", LEGACY_ERR_LOG_NAME, OUT_LOG_NAME)
    for member in (path, *(path.with_name(name) for name in siblings)):
        try:
            os.chmod(member, 0o600)
        except OSError:
            pass


def _overflow(obs: Observation) -> Observation:
    """The one row every over-the-cap key of this kind is counted under. Nothing sender-controlled
    (process name, category, format, subject) is kept, so no input can mint new rows."""
    return replace(obs, process="*", category="", template=OVERFLOW_TEMPLATE, service="", function="", subject="")


class TccStore:
    """The SQLite store: template-keyed counts, full prompt rows, diagnostics.

    ``readonly=True`` (for ``report``) never writes, so it never waits on the daemon's write
    lock — WAL readers are not blocked by a writer, even one in a long import.
    """

    def __init__(self, path: Path, *, now: Callable[[], float] = time.time, readonly: bool = False) -> None:
        self.path = path
        self._now = now
        self.readonly = readonly
        if not readonly:
            path.parent.mkdir(parents=True, exist_ok=True)
        if readonly:
            self.db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=rw", uri=True, timeout=30)
        else:
            self.db = sqlite3.connect(str(path), timeout=30)
        if not readonly:
            try:
                self.db.execute("PRAGMA journal_mode=WAL")
                _owner_only(path)
                self.db.execute("PRAGMA synchronous=NORMAL")
                self.db.executescript(_SCHEMA)
                self.db.execute(
                    "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
                )
                self.db.commit()
                version = self.db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
                if version != str(SCHEMA_VERSION):
                    raise sqlite3.DatabaseError(f"store layout {version}, this daemon writes {SCHEMA_VERSION}")
            except BaseException:
                self.db.close()
                raise
        # Bounded: at most MAX_TEMPLATES template keys plus one overflow key per kind.
        self._template_ids: dict[tuple[str, str, str, str, str, str, str], int] = {}
        self._templates_full = False
        self._pending: dict[tuple[int, int, int], int] = {}
        self._prompts_since_trim = 0

    def close(self) -> None:
        if not self.readonly:
            self.flush()
        self.db.close()

    # ── writes ─────────────────────────────────────────────────────────────────────────
    def _template_id(self, obs: Observation) -> int:
        tid = self._template_ids.get(obs.key)
        if tid is not None:
            return tid
        row = self.db.execute(
            "SELECT id FROM templates WHERE process=? AND category=? AND format=? AND service=? AND function=?"
            " AND subject=? AND kind=?",
            obs.key,
        ).fetchone()
        if row is not None:
            tid = int(row[0])
        elif obs.key != _overflow(obs).key and self._at_template_cap():
            # Not cached under obs.key: past the cap, the cache must not grow with new keys either.
            return self._template_id(_overflow(obs))
        else:
            cur = self.db.execute(
                "INSERT INTO templates (process, category, format, service, function, subject, kind, example,"
                " first_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*obs.key, obs.message[:MAX_EXAMPLE_CHARS], obs.timestamp),
            )
            tid = int(cur.lastrowid)
        self._template_ids[obs.key] = tid
        return tid

    def _at_template_cap(self) -> bool:
        if not self._templates_full:  # templates are never deleted: once full, always full
            count = self.db.execute("SELECT COUNT(*) FROM templates").fetchone()[0]
            self._templates_full = count >= MAX_TEMPLATES
        return self._templates_full

    def observe(self, obs: Observation, *, span: int = HOUR, commit: bool = True) -> None:
        """Count ``obs`` in its bucket (buffered until :meth:`flush`); a prompt is also stored
        in full — and committed right away unless ``commit=False`` (a caller-owned transaction).
        ``prompts`` is trimmed every :data:`PROMPT_TRIM_EVERY` inserts, so even a flood of
        prompt-shaped lines between two :meth:`maintain` runs stays bounded."""
        epoch = obs.epoch if obs.epoch is not None else int(self._now())
        key = (epoch - epoch % span, span, self._template_id(obs))
        self._pending[key] = self._pending.get(key, 0) + 1
        if obs.is_prompt:
            self.db.execute(
                "INSERT INTO prompts (ts, kind, service, process_path, pid, message) VALUES (?, ?, ?, ?, ?, ?)",
                (obs.timestamp, obs.kind, obs.service or None, obs.process_path, obs.pid, obs.message),
            )
            if commit:
                # Not inside a caller-owned transaction: an import verifies its prompt count, and
                # trimming mid-transaction would break that (maintain() trims afterwards).
                self._prompts_since_trim += 1
                if self._prompts_since_trim >= PROMPT_TRIM_EVERY:
                    self._trim("prompts", MAX_PROMPT_ROWS)
                    self._prompts_since_trim = 0
                self.db.commit()

    def write_pending(self) -> None:
        """Write the buffered counts into the current transaction (no commit).

        The buffer is taken BEFORE the write: whatever interrupts it (an error, or SIGTERM's
        SystemExit right after it) must never leave the batch in place to be written again —
        a lost minute of counts is acceptable, counting it twice is not. On a SQLite error the
        transaction is rolled back, so a half-applied batch is never committed later.
        """
        if not self._pending:
            return
        batch = [(*k, n) for k, n in self._pending.items()]
        self._pending.clear()
        try:
            self.db.executemany(_UPSERT_COUNT, batch)
        except sqlite3.Error:
            self.rollback()
            raise

    def flush(self) -> None:
        """Write the buffered counts and commit."""
        self.write_pending()
        self.db.commit()

    def rollback(self) -> None:
        """Abandon the current transaction, including buffered counts and cached template ids
        that may point at rolled-back rows."""
        self.db.rollback()
        self._pending.clear()
        self._template_ids.clear()
        self._templates_full = False

    def diagnostic(self, message: str) -> None:
        """Record one diagnostic. It commits only a transaction it opened itself: inside a
        caller's (an import in progress) it joins it, and never commits half of it."""
        owned = not self.db.in_transaction
        self.db.execute(
            "INSERT INTO diagnostics (ts, message) VALUES (?, ?)",
            (datetime.fromtimestamp(self._now(), timezone.utc).isoformat(timespec="seconds"), message),
        )
        if owned:
            self.db.commit()

    def _trim(self, table: str, cap: int) -> None:
        if table not in ("prompts", "diagnostics"):  # the only interpolated identifier in this module
            raise ValueError(f"not a trimmable table: {table!r}")
        self.db.execute(
            f"DELETE FROM {table} WHERE id <= (SELECT id FROM {table} ORDER BY id DESC LIMIT 1 OFFSET ?)",
            (cap,),
        )

    def maintain(self) -> None:
        """Roll hourly counts older than :data:`HOURLY_RETENTION_DAYS` into daily rows and trim
        ``prompts``/``diagnostics`` to their row caps — all or nothing: ANY interruption (a
        SQLite error, or SIGTERM's SystemExit) rolls the whole step back, because a committed
        rollup without its DELETE would count those hours twice."""
        cutoff = int(self._now()) - HOURLY_RETENTION_DAYS * DAY
        try:
            self.flush()
            self.db.execute(
                "INSERT INTO counts (bucket, span, template_id, n)"
                " SELECT bucket - bucket % :day, :day, template_id, SUM(n) FROM counts"
                " WHERE span = :hour AND bucket < :cutoff GROUP BY bucket - bucket % :day, template_id"
                " ON CONFLICT (bucket, span, template_id) DO UPDATE SET n = n + excluded.n",
                {"day": DAY, "hour": HOUR, "cutoff": cutoff},
            )
            self.db.execute("DELETE FROM counts WHERE span = ? AND bucket < ?", (HOUR, cutoff))
            self._trim("prompts", MAX_PROMPT_ROWS)
            self._trim("diagnostics", MAX_DIAGNOSTIC_ROWS)
            self.db.commit()
        except BaseException:
            self.rollback()
            raise

    # ── reads ──────────────────────────────────────────────────────────────────────────
    def totals(self) -> tuple[int, int]:
        """(lines counted, prompt rows) as this connection sees them (written, maybe uncommitted)."""
        lines = self.db.execute("SELECT COALESCE(SUM(n), 0) FROM counts").fetchone()[0]
        prompts = self.db.execute("SELECT COUNT(*) FROM prompts").fetchone()[0]
        return int(lines), int(prompts)

    def summary(self, *, since: int, top: int = 20, recent: int = 20) -> dict:
        """What ``report`` shows: daily line totals, the busiest templates, processes and
        kinds from the UTC day containing ``since`` (unix seconds), and the newest prompt rows."""
        if not self.readonly:
            self.flush()
        start = since - since % DAY  # whole days, so hourly and rolled-up daily rows line up
        cur = self.db.cursor()
        cur.row_factory = sqlite3.Row

        def rows(sql: str, params: tuple) -> list[dict]:
            return [dict(r) for r in cur.execute(sql, params).fetchall()]

        daily = rows(
            "SELECT bucket - bucket % ? AS day, SUM(n) AS lines FROM counts WHERE bucket >= ?"
            " GROUP BY day ORDER BY day",
            (DAY, start),
        )
        joined = " FROM counts c JOIN templates t ON t.id = c.template_id WHERE c.bucket >= ?"
        return {
            "since": datetime.fromtimestamp(start, timezone.utc).isoformat(timespec="seconds"),
            "lines": sum(r["lines"] for r in daily),
            "daily_utc": [
                {"day": datetime.fromtimestamp(r["day"], timezone.utc).date().isoformat(), "lines": r["lines"]}
                for r in daily
            ],
            "by_kind": rows(f"SELECT t.kind AS kind, SUM(c.n) AS lines{joined} GROUP BY t.kind ORDER BY lines DESC",
                            (start,)),
            "by_process": rows(
                f"SELECT t.process AS process, SUM(c.n) AS lines{joined} GROUP BY t.process ORDER BY lines DESC LIMIT ?",
                (start, top),
            ),
            "top_templates": rows(
                "SELECT t.process AS process, t.service AS service, t.function AS function, t.subject AS subject,"
                f" t.kind AS kind, t.format AS format, t.example AS example, SUM(c.n) AS lines{joined}"
                " GROUP BY c.template_id ORDER BY lines DESC LIMIT ?",
                (start, top),
            ),
            "recent_prompts": rows(
                "SELECT ts, kind, service, process_path, message FROM prompts ORDER BY id DESC LIMIT ?", (recent,)
            ),
        }


# ── diagnostics: store always, stderr while it is small ─────────────────────────────────────
def stderr_has_room(stream: TextIO, max_bytes: int) -> bool:
    """False once ``stream`` is a regular file past ``max_bytes``; a pipe/tty (or a stream with
    no file descriptor) always has room."""
    try:
        info = os.fstat(stream.fileno())
    except (OSError, ValueError):
        return True
    return not (stat.S_ISREG(info.st_mode) and info.st_size > max_bytes)


def _log_err(msg: str, store: TccStore | None = None, stream: TextIO | None = None) -> None:
    """Record one ``tcc-watch: <msg>`` diagnostic: in the store, and on stderr while stderr is
    below :data:`MAX_STDERR_BYTES`. Never raises — a diagnostic that cannot be written must not
    take the daemon down with it."""
    if store is not None:
        try:
            store.diagnostic(msg)
        except sqlite3.Error:
            pass
    out = stream if stream is not None else sys.stderr
    if out is None:  # started with fd 2 closed
        return
    try:
        if stderr_has_room(out, MAX_STDERR_BYTES):
            print(f"tcc-watch: {msg}", file=out, flush=True)
    except (OSError, ValueError):
        pass


# ── one-time import of the pre-#607 line log ────────────────────────────────────────────────
class _MirrorCheck:
    """Walks launchd's err.log alongside the imported rows: every old per-event mirror line
    must equal the line the old daemon printed for one of them, in order."""

    def __init__(self, err_log: Path) -> None:
        self.err_log = err_log
        self.read_error: OSError | None = None
        try:
            self._fh = err_log.open("r", encoding="utf-8", errors="replace") if err_log.is_file() else None
        except OSError as exc:  # unreadable: verify nothing, strip nothing
            self._fh = None
            self.read_error = exc
        self.mirror_lines = 0
        self.matched = 0
        self._awaiting: str | None = None
        self._advance()

    def _advance(self) -> None:
        self._awaiting = None
        while self._fh is not None:
            line = self._fh.readline()
            if not line:
                self.close()
                return
            if line.startswith(_MIRROR_PREFIX):
                self.mirror_lines += 1
                self._awaiting = line.rstrip("\n")
                return

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def row(self, message: str, pid: object, process: object) -> None:
        expected = _LEGACY_MIRROR.format(message=message, pid=pid, process=process)
        if self._awaiting is not None and self._awaiting == expected:
            self.matched += 1
            self._advance()

    def skip_verified(self, count: int) -> bool:
        """Count the first ``count`` mirror lines as verified by earlier starts (their rows are
        imported and gone). False when err.log no longer holds that many."""
        for _ in range(count):
            if self._awaiting is None:
                return False
            self.matched += 1
            self._advance()
        return True

    def all_matched(self) -> bool:
        while self._awaiting is not None:  # count any mirror line left unmatched
            self._advance()
        return self.mirror_lines > 0 and self.matched == self.mirror_lines


def _file_identity(path: Path) -> dict:
    info = path.stat()
    return {"size": info.st_size, "mtime_ns": info.st_mtime_ns}


def _already_imported(store: TccStore, path: Path) -> bool:
    row = store.db.execute("SELECT value FROM meta WHERE key = ?", (f"legacy_import:{path.name}",)).fetchone()
    if row is None:
        return False
    try:
        recorded = json.loads(row[0])
    except (TypeError, ValueError):
        return False
    if not isinstance(recorded, dict):
        return False
    identity = _file_identity(path)
    return recorded.get("size") == identity["size"] and recorded.get("mtime_ns") == identity["mtime_ns"]


class ImportOutcome(NamedTuple):
    """What happened to one legacy file."""

    status: str  # one of the IMPORTED / ALREADY_IMPORTED / NOT_IMPORTED constants below
    lines: int = 0
    prompts: int = 0
    error: str = ""


IMPORTED = "imported"
ALREADY_IMPORTED = "already imported"
NOT_IMPORTED = "not imported"


def _import_legacy_file(store: TccStore, path: Path, mirror: _MirrorCheck) -> ImportOutcome:
    """Import one pre-#607 JSONL file in ONE ``BEGIN IMMEDIATE`` transaction (so a second
    instance cannot import it concurrently) that commits only if the store's totals grew by
    exactly the file's line count (every line is counted; a real prompt is also stored in full),
    together with a ``meta`` record of the file's identity. Any interruption — an error, or
    SIGTERM's SystemExit — rolls it back: a partial import is never committed.

    ``ALREADY_IMPORTED`` when ``meta`` shows this very file was imported by an earlier start.
    """
    store.flush()
    lines = prompts = 0
    try:
        store.db.execute("BEGIN IMMEDIATE")
        if _already_imported(store, path):
            store.db.commit()
            return ImportOutcome(ALREADY_IMPORTED)
        before_lines, before_prompts = store.totals()
        identity_before = _file_identity(path)
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                if not raw.strip():
                    continue
                lines += 1
                try:
                    row = json.loads(raw)
                except ValueError:
                    row = None
                if not isinstance(row, dict) or not isinstance(row.get("message"), str):
                    row = {"message": ""}
                message = row["message"]
                obs = _observe(
                    message=message, template=_legacy_template(message), category="legacy",
                    process_path=row.get("process"), timestamp=row.get("timestamp"), pid=row.get("pid"),
                )
                store.observe(obs, span=DAY, commit=False)
                prompts += obs.is_prompt
                mirror.row(message, row.get("pid"), row.get("process"))
        store.write_pending()
        after_lines, after_prompts = store.totals()
        if after_lines - before_lines != lines or after_prompts - before_prompts != prompts:
            raise ValueError("store totals do not match the file")
        identity = _file_identity(path)
        if identity != identity_before:
            raise ValueError("the file changed while it was being imported")
        store.db.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (f"legacy_import:{path.name}", json.dumps({**identity, "lines": lines, "prompts": prompts})),
        )
        # How many err.log mirror lines are verified against committed rows so far — committed
        # with the rows, so a later start can resume the verification (and the strip) from here.
        store.db.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (_MIRROR_VERIFIED_KEY, str(mirror.matched))
        )
        store.db.commit()
    except (OSError, ValueError, sqlite3.Error) as exc:
        store.rollback()
        return ImportOutcome(NOT_IMPORTED, error=str(exc))
    except BaseException:
        store.rollback()
        raise
    return ImportOutcome(IMPORTED, lines, prompts)


def _verified_mirror_lines(store: TccStore) -> int:
    row = store.db.execute("SELECT value FROM meta WHERE key = ?", (_MIRROR_VERIFIED_KEY,)).fetchone()
    try:
        return int(row[0]) if row else 0
    except (TypeError, ValueError):
        return 0


def _forget_verified_mirror_lines(store: TccStore) -> None:
    store.db.execute("DELETE FROM meta WHERE key = ?", (_MIRROR_VERIFIED_KEY,))
    store.db.commit()


def _remove(path: Path) -> str:
    try:
        path.unlink()
    except OSError as exc:
        return f"could not remove it ({exc})"
    return "removed it"


def import_legacy_logs(store: TccStore, directory: Path) -> list[str]:
    """Import (and only then remove) the pre-#607 ``events.jsonl`` family in ``directory``,
    oldest first. launchd's err.log is stripped of the old per-event mirror lines only when every
    one of them equals the printed line of an imported row, in order. The count verified so far
    is committed with each file's import, so an interrupted first start is resumed — and the strip
    finished — by a later one. Otherwise err.log is left exactly as it is. Returns the notes."""
    err_log = directory / LEGACY_ERR_LOG_NAME
    notes = _finish_interrupted_strip(err_log)
    family = [directory / f"{LEGACY_EVENTS_NAME}.{i}" for i in range(LEGACY_BACKUPS, 0, -1)]
    family.append(directory / LEGACY_EVENTS_NAME)
    family = [p for p in family if p.is_file()]
    verified = _verified_mirror_lines(store)
    if not family and not verified:
        return notes
    mirror = _MirrorCheck(err_log)
    try:
        if mirror.read_error is not None:
            notes.append(f"err.log: could not read it ({mirror.read_error}); left it as is")
        consistent = mirror.skip_verified(verified)
        if not consistent:
            notes.append("err.log: shorter than when its lines were verified; left it as is")
            _forget_verified_mirror_lines(store)
        for path in family:
            outcome = _import_legacy_file(store, path, mirror)
            if outcome.status == IMPORTED:
                notes.append(
                    f"imported {path.name}: {outcome.lines} lines ({outcome.prompts} prompts/denials); {_remove(path)}"
                )
            elif outcome.status == ALREADY_IMPORTED:  # an earlier start imported it but could not remove it
                notes.append(f"{path.name} was already imported; {_remove(path)}")
            else:
                # Stop here: this file walked the mirror past rows that are not in the store, so
                # a later file must not commit a verified count that includes them. The rest are
                # imported by a later start, which resumes from the last committed count.
                notes.append(f"could not import {path.name} ({outcome.error}); kept it and the files after it")
                consistent = False
                break
        matched_all = mirror.all_matched()
        if consistent and matched_all:
            try:
                kept = _strip_mirror_lines(err_log)
            except OSError as exc:
                notes.append(f"err.log: could not strip the verified duplicate event lines ({exc}); left it as is")
            else:
                _forget_verified_mirror_lines(store)
                notes.append(
                    f"err.log: dropped {mirror.mirror_lines} verified duplicate event lines,"
                    f" kept {kept} other line{'' if kept == 1 else 's'}"
                )
        elif mirror.mirror_lines:
            notes.append(
                f"err.log: {mirror.matched} of {mirror.mirror_lines} old event lines matched imported rows;"
                " left it as is"
            )
    finally:
        mirror.close()
    return notes


def _strip_mirror_lines(err_log: Path) -> int:
    """Rewrite ``err_log`` in place without the verified mirror lines; returns the lines kept.

    In place (truncate + append) because launchd holds it open for append: a replaced file would
    be orphaned. Crash-safe: the kept lines are first written in full to ``<err.log>.kept``
    (atomically, via ``.stripping``) and only then copied back; :func:`_finish_interrupted_strip`
    completes an interrupted copy on the next start.
    """
    tmp = err_log.with_name(f"{err_log.name}.stripping")
    done = err_log.with_name(f"{err_log.name}.kept")
    kept = 0
    with err_log.open("r", encoding="utf-8", errors="replace") as src, tmp.open("w", encoding="utf-8") as out:
        for line in src:
            if not line.startswith(_MIRROR_PREFIX):
                out.write(line)
                kept += 1
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, done)
    _restore_kept_lines(err_log, done)
    return kept


def _restore_kept_lines(err_log: Path, done: Path) -> None:
    err_log.touch(exist_ok=True)
    os.truncate(err_log, 0)
    with done.open("r", encoding="utf-8") as src, err_log.open("a", encoding="utf-8") as out:
        for line in src:
            out.write(line)
    done.unlink()


def _finish_interrupted_strip(err_log: Path) -> list[str]:
    """A strip killed half-way: ``.stripping`` means err.log was not touched yet (drop the
    partial copy); ``.kept`` means the kept lines are complete, so finish copying them back."""
    notes: list[str] = []
    tmp = err_log.with_name(f"{err_log.name}.stripping")
    done = err_log.with_name(f"{err_log.name}.kept")
    try:
        if tmp.exists():
            tmp.unlink()
        if done.exists():
            _restore_kept_lines(err_log, done)
            notes.append("err.log: finished an interrupted strip of verified duplicate event lines")
    except OSError as exc:
        notes.append(f"err.log: could not finish an interrupted strip ({exc}); {done.name} kept")
    return notes


# ── the watch loop ───────────────────────────────────────────────────────────────────────────
class _StreamProc(Protocol):
    stdout: Iterable[str] | None
    returncode: int | None

    def wait(self) -> int: ...


def _spawn_log_stream() -> subprocess.Popen:
    # errors="replace": one non-UTF-8 byte in a log message must not raise UnicodeDecodeError
    # out of the read loop and crash the daemon.
    return subprocess.Popen(
        LOG_STREAM_CMD, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, errors="replace", bufsize=1,
    )


def open_store(store_path: Path, *, sleep: Callable[[float], None] = time.sleep) -> TccStore:
    """Open (or create) the store, never raising: an uncaught error would crash the daemon and
    launchd would restart it every 10 s, appending a traceback to err.log each time.

    A file that is not a database is moved aside with its WAL files (kept, never deleted) and a
    fresh store is created; anything else (a locked store, a full or failing disk) is retried.
    Every retry backs off, so nothing here can spin.
    """
    backoff = 1.0
    while True:
        try:
            return TccStore(store_path)
        # OperationalError (locked, cannot open, disk I/O) subclasses DatabaseError and must come
        # first: a merely locked store is retried, only an unreadable one is moved aside.
        except sqlite3.OperationalError as exc:
            _log_err(f"cannot open {store_path} ({exc}); retrying in {backoff:.0f}s")
        except sqlite3.DatabaseError as exc:
            # The -wal/-shm sidecars belong to the unreadable file: left behind, SQLite would pair
            # them with the new store and fail again. They are moved aside with it, never deleted.
            suffix = f".unreadable-{time.time_ns()}"
            try:
                for member in (store_path, *(store_path.with_name(f"{store_path.name}{side}") for side in ("-wal", "-shm"))):
                    if member.exists():
                        os.replace(member, member.with_name(member.name + suffix))
            except OSError as move_exc:
                _log_err(f"{store_path} is unreadable ({exc}) and could not be moved aside ({move_exc});"
                         f" retrying in {backoff:.0f}s")
            else:
                _log_err(f"{store_path} is unreadable ({exc}); kept it as {store_path.name}{suffix},"
                         f" starting a new store in {backoff:.0f}s")
        except OSError as exc:
            _log_err(f"cannot open {store_path} ({exc}); retrying in {backoff:.0f}s")
        sleep(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)


def _terminate(_signum: int, _frame: object) -> None:
    raise SystemExit(0)  # unwinds into run()'s `finally`, which flushes the buffered counts


def run(
    store_path: Path,
    *,
    stream_factory: Callable[[], _StreamProc] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    max_reconnects: int | None = None,
) -> int:
    """The watch loop: import any pre-#607 log, spawn `log stream`, count every line and store
    every prompt, reconnect with backoff if the subprocess exits.

    Never returns under normal (production) operation — launchd's ``KeepAlive`` restarts the
    whole process if this loop itself ever exits. ``max_reconnects`` bounds the number of spawn
    attempts (``None`` = unbounded, the production default). Returns the number of TCC records
    counted.
    """
    factory = stream_factory or _spawn_log_stream
    store = open_store(store_path, sleep=sleep)
    backoff = 1.0
    attempts = 0
    seen = 0
    last_flush = last_maintain = clock()

    def reconnect_later(reason: str) -> None:
        nonlocal backoff
        _log_err(f"{reason}; reconnecting in {backoff:.0f}s", store)
        sleep(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

    try:
        # The import may rewrite launchd's err.log, so nothing is written to stderr before it.
        notes: list[str] = []
        try:
            notes = import_legacy_logs(store, store_path.parent)
            store.maintain()
        except (OSError, ValueError, sqlite3.Error) as exc:
            store.rollback()
            notes.append(f"startup import/maintenance failed: {exc}")
        _log_err(f"started; counting TCC log lines into {store_path}", store)
        for note in notes:
            _log_err(note, store)
        while max_reconnects is None or attempts < max_reconnects:
            attempts += 1
            started = clock()
            try:
                proc = factory()
            except OSError as exc:
                reconnect_later(f"failed to spawn log stream: {exc}")
                continue
            for line in proc.stdout or ():
                obs = parse_observation(line)
                if obs is None:
                    continue
                try:
                    store.observe(obs)
                    seen += 1
                except sqlite3.Error as exc:
                    store.rollback()  # never commit a half-written step later (double counting)
                    what = f" ({obs.service})" if obs.is_prompt and obs.service else ""
                    _log_err(f"failed to record a {obs.kind} line{what}: {exc}", store)
                    continue
                now = clock()
                try:
                    # a failed flush/maintain still advances its timer: retried per interval, not per line
                    if now - last_flush >= FLUSH_SECONDS:
                        last_flush = now
                        store.flush()
                    if now - last_maintain >= MAINTAIN_SECONDS:
                        last_maintain = now
                        store.maintain()
                except sqlite3.Error as exc:
                    store.rollback()
                    _log_err(f"store flush/maintenance failed: {exc}", store)
            proc.wait()
            if clock() - started >= HEALTHY_STREAM_SECONDS:
                backoff = 1.0
            reconnect_later(f"log stream exited (rc={proc.returncode})")
    finally:
        try:
            store.close()
        except sqlite3.Error as exc:
            _log_err(f"failed to flush on exit: {exc}")
    return seen


# ── report ───────────────────────────────────────────────────────────────────────────────────
def render_report(summary: dict, out: TextIO) -> None:
    """Plain-text rendering of :meth:`TccStore.summary`."""
    lines = summary["lines"] or 1
    print(f"TCC log lines since {summary['since']}: {summary['lines']:,}", file=out)
    print("\nper day (UTC):", file=out)
    for row in summary["daily_utc"]:
        print(f"  {row['day']}  {row['lines']:>12,}", file=out)
    print("\nby kind:", file=out)
    for row in summary["by_kind"]:
        print(f"  {row['lines']:>12,}  {100 * row['lines'] / lines:6.2f}%  {row['kind']}", file=out)
    print("\nby process:", file=out)
    for row in summary["by_process"]:
        print(f"  {row['lines']:>12,}  {100 * row['lines'] / lines:6.2f}%  {row['process']}", file=out)
    print("\ntop templates:", file=out)
    for row in summary["top_templates"]:
        where = " ".join(x for x in (row["process"], row["service"], row["function"], row["subject"]) if x)
        print(f"  {row['lines']:>12,}  {where}  [{row['kind']}]", file=out)
        print(f"                {row['example'][:160]!r}", file=out)
    print("\nnewest prompts/denials:", file=out)
    for row in summary["recent_prompts"]:
        print(f"  {row['ts']}  {row['kind']}  {row['service'] or '-'}  {row['message'][:120]!r}", file=out)


def _default_store() -> Path:
    return Path.home() / ".config" / "rig" / "tcc-watch" / STORE_NAME


def _report_store() -> Path:
    """The store ``report`` reads by default: the one beside this script (the deployed copy lives
    in the configured config_dir, custom or not), else the default location."""
    beside = Path(__file__).resolve().parent / STORE_NAME
    return beside if beside.is_file() else _default_store()


def _store_path(arg: str) -> Path:
    path = Path(arg)
    # A plist written before rig-cli#607 passes <config_dir>/events.jsonl: use the store beside it.
    return path.with_name(STORE_NAME) if path.suffix == ".jsonl" else path


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    usage = ("usage: watch.py [STORE]            run the watcher (launchd does this)\n"
             "       watch.py report [--help]    summarize the store")
    if argv[:1] in (["-h"], ["--help"]):
        print(usage)
        return 0
    if argv[:1] == ["report"]:
        ap = argparse.ArgumentParser(prog="watch.py report", description="summarize the tcc-watch store")
        ap.add_argument("--store", default=str(_report_store()))
        ap.add_argument("--days", type=int, default=7)
        ap.add_argument("--top", type=int, default=20)
        ap.add_argument("--json", action="store_true")
        args = ap.parse_args(argv[1:])
        store_path = _store_path(args.store)
        if not store_path.is_file():
            print(f"tcc-watch: no store at {store_path}", file=sys.stderr)
            return 1
        store = None
        try:
            store = TccStore(store_path, readonly=True)
            summary = store.summary(since=int(time.time()) - max(0, args.days) * DAY, top=max(1, args.top))
        except sqlite3.Error as exc:
            print(f"tcc-watch: cannot read {store_path} ({exc})", file=sys.stderr)
            return 1
        finally:
            if store is not None:
                store.close()
        if args.json:
            json.dump(summary, sys.stdout, indent=2)
            print()
        else:
            render_report(summary, sys.stdout)
        return 0
    if len(argv) > 1 or (argv and argv[0].startswith("-")):
        # a typo'd flag must not start a second watcher on a store named after it
        print(usage, file=sys.stderr)
        return 2
    os.umask(0o077)  # the store and its WAL sidecars are created owner-only
    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)
    try:
        run(_store_path(argv[0]) if argv else _default_store())
    except Exception:  # noqa: BLE001 — anything unforeseen: launchd restarts us in 10 s
        # Through _log_err, not the interpreter's raw traceback: a crash-looping bug must not
        # grow err.log past MAX_STDERR_BYTES either.
        _log_err("crashed: " + traceback.format_exc()[-4000:])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
