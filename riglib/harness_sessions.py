"""Which conversation was a harness process in? — the IO half of :mod:`riglib.harness_kinds`.

The wrapper's supervisor needs this at exactly one moment: a hosted harness just died and must be
relaunched onto the SAME conversation. Each harness leaves different evidence:

* **omp** writes ``<agent dir>/terminal-sessions/<tty>`` (cwd, session file, optional ``fresh``)
  for the terminal it runs in — the record its own ``--continue`` reads. The supervisor shares
  that pty, so the record is accepted only when written after the dead process started.
* **Claude Code** publishes ``<config dir>/sessions/<pid>.json`` (``sessionId``, ``cwd``) while it
  lives and removes it at exit, so it is sampled while the process runs
  (:meth:`Tracker.poll`). The id the wrapper assigned at launch is the fallback.
* **Codex** appends a rollout ``<codex home>/sessions/Y/M/D/rollout-*.jsonl`` whose first line is
  ``session_meta {id, cwd, thread_source}``: the ONLY interactive rollout of this cwd touched
  since the process started.
* **opencode** keeps sessions in ``opencode.db`` (``session.directory``, ``parent_id IS NULL``):
  the ONLY top-level session of this cwd updated since the process started.

Two hosted sessions of one harness in one directory can't be told apart by the last two, and
guessing could attach a second writer to a live sibling's conversation — so with more than one
candidate they answer unknown (rig-cli#664); omp and Claude Code are exact. Every reader degrades
to ``None`` ("unknown — reuse the previous command line") instead of raising.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from .harness_kinds import SessionRef, validate_kind

#: slack for clocks/filesystems that round mtimes.
_SLACK = 2.0


@dataclass
class Tracker:
    """Collects a harness process's conversation identity, sampled while it runs and read once
    more after it dies."""

    kind: str
    cwd: str
    #: the conversation known before the process started (Claude Code's assigned id).
    initial: SessionRef | None = None
    env: dict[str, str] = field(default_factory=lambda: dict(os.environ))
    home: Path = field(default_factory=Path.home)
    terminal: str | None = None
    _seen: SessionRef | None = field(default=None, init=False, repr=False)

    @property
    def needs_polling(self) -> bool:
        return self.kind == "claude"

    def poll(self, pid: int) -> None:
        """Sample what a LIVE process publishes (Claude Code only; no-op for the rest)."""
        if self.kind != "claude":
            return
        data = _read_json(_claude_dir(self.env, self.home) / "sessions" / f"{pid}.json")
        if isinstance(data, dict) and isinstance(data.get("sessionId"), str) and data["sessionId"]:
            cwd = data.get("cwd")
            self._seen = SessionRef(id=data["sessionId"], cwd=cwd if isinstance(cwd, str) else None)

    def final(self, started_wall: float) -> SessionRef | None:
        """The conversation a process that started at ``started_wall`` (Unix time) died in."""
        validate_kind(self.kind)
        if self.kind == "omp":
            return _omp_ref(self.env, self.home, self.terminal, started_wall)
        if self.kind == "claude":
            return _claude_ref(self._seen or self.initial, self.cwd, self.env, self.home)
        if self.kind == "codex":
            return _codex_ref(self.env, self.home, self.cwd, started_wall)
        return _opencode_ref(self.env, self.home, self.cwd, started_wall)


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ── omp ──────────────────────────────────────────────────────────────────────────────────────
def omp_agent_root(env: dict[str, str], home: Path) -> Path:
    """omp's agent dir: ``PI_CODING_AGENT_DIR`` (full override), else ``PI_CONFIG_DIR/agent``, else
    ``~/.omp/agent``. A relative override is relative to ``home``."""

    def expand(value: str) -> Path:
        if value.startswith("/"):
            return Path(value)
        if value == "~":
            return home
        return home / (value[2:] if value.startswith("~/") else value)

    if env.get("PI_CODING_AGENT_DIR"):
        return expand(env["PI_CODING_AGENT_DIR"])
    if env.get("PI_CONFIG_DIR"):
        return expand(env["PI_CONFIG_DIR"]) / "agent"
    return home / ".omp" / "agent"


def terminal_id() -> str | None:
    """The breadcrumb key omp derives for this pty (oh-my-pi ``getTerminalId``: the ``/dev/`` path
    with ``/`` → ``-``)."""
    try:
        path = os.ttyname(0)
    except OSError:
        return None
    return path[len("/dev/") :].replace("/", "-") if path.startswith("/dev/") else None


def _omp_ref(env: dict[str, str], home: Path, terminal: str | None, started_wall: float) -> SessionRef | None:
    """omp's per-terminal record (``cwd``, session file, optional ``fresh``/``cwdstat`` lines —
    oh-my-pi ``writeTerminalBreadcrumb``), if written at or after the process started and still
    naming something resumable."""
    if not terminal:
        return None
    path = omp_agent_root(env, home) / "terminal-sessions" / terminal
    try:
        if path.stat().st_mtime < started_wall - 1:
            return None
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    if len(lines) < 2 or not lines[0] or not lines[1]:
        return None
    if "fresh" in lines[2:]:
        return SessionRef(fresh=True)  # a lazy session that never reached disk
    session = Path(lines[0]) / lines[1]
    return SessionRef(file=str(session)) if session.is_file() else None


# ── Claude Code ──────────────────────────────────────────────────────────────────────────────
def _claude_dir(env: dict[str, str], home: Path) -> Path:
    return Path(env["CLAUDE_CONFIG_DIR"]) if env.get("CLAUDE_CONFIG_DIR") else home / ".claude"


def claude_transcript(env: dict[str, str], home: Path, cwd: str, session_id: str) -> Path:
    """``<config>/projects/<cwd with every non-alphanumeric → '-'>/<id>.jsonl``."""
    return _claude_dir(env, home) / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd) / f"{session_id}.jsonl"


def _claude_ref(seen: SessionRef | None, cwd: str, env: dict[str, str], home: Path) -> SessionRef | None:
    if seen is None or not seen.id:
        return None
    where = seen.cwd or cwd
    transcript = claude_transcript(env, home, where, seen.id)
    return SessionRef(id=seen.id, file=str(transcript) if transcript.is_file() else None, cwd=seen.cwd)


# ── Codex ────────────────────────────────────────────────────────────────────────────────────
def _codex_ref(env: dict[str, str], home: Path, cwd: str, started_wall: float) -> SessionRef | None:
    root = Path(env["CODEX_HOME"]) if env.get("CODEX_HOME") else home / ".codex"
    want = os.path.realpath(cwd)
    found: list[SessionRef] = []
    for path in _codex_rollouts(root / "sessions", started_wall):
        try:
            if path.stat().st_mtime < started_wall - _SLACK:
                continue
            with path.open(encoding="utf-8") as handle:
                meta = json.loads(handle.readline(1 << 20))
        except (OSError, ValueError):
            continue
        payload = meta.get("payload") if isinstance(meta, dict) else None
        if not isinstance(payload, dict) or meta.get("type") != "session_meta":
            continue
        if payload.get("thread_source") == "subagent" or not isinstance(payload.get("id"), str):
            continue
        rollout_cwd = payload.get("cwd")
        if not isinstance(rollout_cwd, str) or os.path.realpath(rollout_cwd) != want:
            continue
        found.append(SessionRef(id=payload["id"], file=str(path), cwd=rollout_cwd))
    # Two interactive rollouts of one cwd since the process started may belong to a hosted sibling
    # that is still alive; resuming it would attach a second writer. Unknown is the safe answer.
    return found[0] if len(found) == 1 else None


def _codex_rollouts(sessions: Path, started_wall: float):
    """``rollout-*.jsonl`` under the ``Y/M/D`` dirs from the day before the process started to
    today (local dates, as codex names them)."""
    day = time.localtime(started_wall - 86400)
    today = time.localtime()
    lo, hi = (day.tm_year, day.tm_mon, day.tm_mday), (today.tm_year, today.tm_mon, today.tm_mday)
    for year in _numbered(sessions):
        if not lo[0] <= int(year.name) <= hi[0]:
            continue
        for month in _numbered(year):
            if not lo[:2] <= (int(year.name), int(month.name)) <= hi[:2]:
                continue
            for dom in _numbered(month):
                if lo <= (int(year.name), int(month.name), int(dom.name)) <= hi:
                    yield from dom.glob("rollout-*.jsonl")


def _numbered(directory: Path) -> list[Path]:
    try:
        return [p for p in directory.iterdir() if p.name.isdigit() and p.is_dir()]
    except OSError:
        return []


# ── opencode ─────────────────────────────────────────────────────────────────────────────────
def _opencode_ref(env: dict[str, str], home: Path, cwd: str, started_wall: float) -> SessionRef | None:
    share = Path(env["XDG_DATA_HOME"]) if env.get("XDG_DATA_HOME") else home / ".local" / "share"
    db = share / "opencode" / "opencode.db"
    if not db.is_file():
        return None
    directories = sorted({cwd, os.path.realpath(cwd)})
    marks = ",".join("?" * len(directories))
    try:
        with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)) as conn:
            rows = conn.execute(
                f"SELECT id, directory FROM session WHERE directory IN ({marks}) AND parent_id IS NULL "
                "AND time_archived IS NULL AND time_updated >= ? ORDER BY time_updated DESC LIMIT 2",
                (*directories, int((started_wall - _SLACK) * 1000)),
            ).fetchall()
    except sqlite3.Error:
        return None
    # one candidate is ours; two may include a live hosted sibling (see _codex_ref)
    return SessionRef(id=rows[0][0], cwd=rows[0][1]) if len(rows) == 1 else None
