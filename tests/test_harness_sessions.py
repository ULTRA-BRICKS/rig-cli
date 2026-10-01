"""Tests for riglib.harness_sessions — finding the conversation a dead harness was in.

Each harness leaves different evidence on disk; the fixtures below reproduce the real layouts
(omp terminal breadcrumb, Claude Code ``sessions/<pid>.json`` + project transcript, Codex dated
rollouts with ``session_meta``, opencode ``opencode.db``)."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from riglib import harness_sessions as hs
from riglib.harness_kinds import SessionRef


def _tracker(kind: str, tmp_path: Path, cwd: str = "/work/app", **kwargs) -> hs.Tracker:
    return hs.Tracker(kind, cwd, env={}, home=tmp_path / "home", **kwargs)


# ── omp: the terminal breadcrumb ─────────────────────────────────────────────────────────────
def _breadcrumb(home: Path, terminal: str, cwd: str, session: str, *extra: str) -> Path:
    path = home / ".omp" / "agent" / "terminal-sessions" / terminal
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([cwd, session, *extra]) + "\n")
    return path


def test_omp_breadcrumb_written_by_the_dead_process_names_its_session(tmp_path):
    session = tmp_path / "s.jsonl"
    session.write_text("{}\n")
    _breadcrumb(tmp_path / "home", "ttys001", str(tmp_path), "s.jsonl")
    got = _tracker("omp", tmp_path, terminal="ttys001").final(time.time() - 5)
    assert got == SessionRef(file=str(session))


def test_omp_breadcrumb_older_than_the_process_is_someone_elses(tmp_path):
    session = tmp_path / "s.jsonl"
    session.write_text("{}\n")
    _breadcrumb(tmp_path / "home", "ttys001", str(tmp_path), "s.jsonl")
    assert _tracker("omp", tmp_path, terminal="ttys001").final(time.time() + 60) is None


def test_omp_fresh_breadcrumb_means_nothing_reached_disk(tmp_path):
    _breadcrumb(tmp_path / "home", "ttys001", str(tmp_path), "lazy.jsonl", "fresh")
    assert _tracker("omp", tmp_path, terminal="ttys001").final(time.time() - 5) == SessionRef(fresh=True)


def test_omp_breadcrumb_pointing_at_a_missing_transcript_is_unusable(tmp_path):
    _breadcrumb(tmp_path / "home", "ttys001", str(tmp_path), "gone.jsonl")
    assert _tracker("omp", tmp_path, terminal="ttys001").final(time.time() - 5) is None


def test_omp_without_a_terminal_or_breadcrumb_is_unknown(tmp_path):
    assert _tracker("omp", tmp_path, terminal=None).final(0) is None
    assert _tracker("omp", tmp_path, terminal="ttys009").final(0) is None


def test_omp_agent_root_precedence(tmp_path):
    home = tmp_path
    assert hs.omp_agent_root({}, home) == home / ".omp" / "agent"
    assert hs.omp_agent_root({"PI_CONFIG_DIR": "~/.myomp"}, home) == home / ".myomp" / "agent"
    assert hs.omp_agent_root({"PI_CODING_AGENT_DIR": "/srv/agent", "PI_CONFIG_DIR": ".x"}, home) == Path("/srv/agent")


# ── Claude Code: sampled while alive, transcript checked at death ────────────────────────────
def _claude_session(home: Path, pid: int, session_id: str, cwd: str) -> None:
    path = home / ".claude" / "sessions" / f"{pid}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "sessionId": session_id, "cwd": cwd, "kind": "interactive"}))


def _claude_transcript(home: Path, cwd: str, session_id: str) -> Path:
    path = hs.claude_transcript({}, home, cwd, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n")
    return path


def test_claude_follows_the_live_process_session_even_after_clear(tmp_path):
    home = tmp_path / "home"
    tracker = _tracker("claude", tmp_path, initial=SessionRef(id="assigned"))
    _claude_session(home, 4242, "after-clear", "/work/app")
    transcript = _claude_transcript(home, "/work/app", "after-clear")
    tracker.poll(4242)
    assert tracker.final(0) == SessionRef(id="after-clear", file=str(transcript), cwd="/work/app")


def test_claude_keeps_the_last_sample_after_the_process_unpublishes_its_session(tmp_path):
    home = tmp_path / "home"
    tracker = _tracker("claude", tmp_path)
    _claude_session(home, 7, "sid", "/work/app")
    tracker.poll(7)
    (home / ".claude" / "sessions" / "7.json").unlink()  # Claude removes it at exit
    tracker.poll(7)
    assert tracker.final(0) == SessionRef(id="sid", cwd="/work/app")  # no transcript yet: file=None


def test_claude_falls_back_to_the_id_the_wrapper_assigned(tmp_path):
    tracker = _tracker("claude", tmp_path, initial=SessionRef(id="assigned"))
    assert tracker.final(0) == SessionRef(id="assigned")


def test_claude_with_nothing_known_is_unknown(tmp_path):
    assert _tracker("claude", tmp_path).final(0) is None


def test_claude_transcript_path_folds_every_non_alphanumeric_to_dash(tmp_path):
    path = hs.claude_transcript({}, tmp_path, "/Users/u/.config/x y", "sid")
    assert path == tmp_path / ".claude" / "projects" / "-Users-u--config-x-y" / "sid.jsonl"


def test_claude_config_dir_override_is_honoured(tmp_path):
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path / "acct")}
    assert hs.claude_transcript(env, tmp_path, "/w", "sid") == tmp_path / "acct" / "projects" / "-w" / "sid.jsonl"


def test_only_claude_needs_polling():
    assert [hs.Tracker(k, "/w").needs_polling for k in ("omp", "claude", "codex", "opencode")] == [False, True, False, False]


# ── Codex: newest interactive rollout of this cwd since the process started ──────────────────
def _rollout(home: Path, day: tuple[int, int, int], uuid: str, cwd: str, *, source: str = "user", mtime=None) -> Path:
    y, m, d = day
    path = home / ".codex" / "sessions" / f"{y:04d}" / f"{m:02d}" / f"{d:02d}" / f"rollout-{y}-{m:02d}-{d:02d}T10-00-00-{uuid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"type": "session_meta", "payload": {"id": uuid, "cwd": cwd, "thread_source": source}}
    path.write_text(json.dumps(meta) + "\n" + json.dumps({"type": "event"}) + "\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _today() -> tuple[int, int, int]:
    now = time.localtime()
    return now.tm_year, now.tm_mon, now.tm_mday


def test_codex_resumes_the_only_rollout_of_this_cwd_touched_since_start(tmp_path):
    home = tmp_path / "home"
    started = time.time() - 100
    only = _rollout(home, _today(), "new-one", "/work/app", mtime=started + 50)
    _rollout(home, _today(), "other-cwd", "/work/elsewhere", mtime=started + 90)
    _rollout(home, _today(), "long-ago", "/work/app", mtime=started - 3600)
    assert _tracker("codex", tmp_path).final(started) == SessionRef(id="new-one", file=str(only), cwd="/work/app")


def test_codex_with_two_candidates_is_unknown_never_a_live_siblings_conversation(tmp_path):
    home = tmp_path / "home"
    started = time.time() - 100
    _rollout(home, _today(), "mine", "/work/app", mtime=started + 10)
    _rollout(home, _today(), "a-live-sibling", "/work/app", mtime=started + 50)
    assert _tracker("codex", tmp_path).final(started) is None


def test_codex_ignores_rollouts_older_than_the_process_and_subagent_threads(tmp_path):
    home = tmp_path / "home"
    started = time.time() - 100
    _rollout(home, _today(), "before-start", "/work/app", mtime=started - 3600)
    _rollout(home, _today(), "a-subagent", "/work/app", source="subagent", mtime=started + 50)
    assert _tracker("codex", tmp_path).final(started) is None


def test_codex_matches_the_cwd_through_symlinks(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    home = tmp_path / "home"
    started = time.time() - 100
    _rollout(home, _today(), "via-real", str(real), mtime=started + 5)
    assert _tracker("codex", tmp_path, cwd=str(link)).final(started).id == "via-real"


def test_codex_honours_codex_home(tmp_path):
    started = time.time() - 100
    custom = tmp_path / "custom"
    y, m, d = _today()
    path = custom / "sessions" / f"{y:04d}" / f"{m:02d}" / f"{d:02d}" / "rollout-x-abc.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "abc", "cwd": "/work/app"}}) + "\n")
    tracker = hs.Tracker("codex", "/work/app", env={"CODEX_HOME": str(custom)}, home=tmp_path / "nohome")
    assert tracker.final(started - 5).id == "abc"


def test_codex_with_no_sessions_dir_is_unknown(tmp_path):
    assert _tracker("codex", tmp_path).final(time.time() - 10) is None


# ── opencode: newest top-level session of this cwd in opencode.db ────────────────────────────
def _opencode_db(home: Path, rows: list[tuple]) -> Path:
    db = home / ".local" / "share" / "opencode" / "opencode.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE session (id text PRIMARY KEY, directory text NOT NULL, parent_id text, "
        "time_updated integer NOT NULL, time_archived integer)"
    )
    conn.executemany("INSERT INTO session VALUES (?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return db


def test_opencode_two_top_level_candidates_are_unknown(tmp_path):
    started = time.time() - 100
    ms = int(started * 1000)
    _opencode_db(
        tmp_path / "home",
        [("ses_mine", "/work/app", None, ms + 5_000, None), ("ses_sibling", "/work/app", None, ms + 9_000, None)],
    )
    assert _tracker("opencode", tmp_path).final(started) is None


def test_opencode_resumes_the_only_top_level_session_of_this_cwd(tmp_path):
    started = time.time() - 100
    ms = int(started * 1000)
    _opencode_db(
        tmp_path / "home",
        [
            ("ses_old", "/work/app", None, ms - 600_000, None),
            ("ses_newer", "/work/app", None, ms + 9_000, None),
            ("ses_child", "/work/app", "ses_newer", ms + 20_000, None),  # a subagent's child session
            ("ses_other", "/work/elsewhere", None, ms + 30_000, None),
            ("ses_archived", "/work/app", None, ms + 40_000, 1),
        ],
    )
    assert _tracker("opencode", tmp_path).final(started) == SessionRef(id="ses_newer", cwd="/work/app")


def test_opencode_session_untouched_since_start_is_not_ours(tmp_path):
    started = time.time() - 100
    _opencode_db(tmp_path / "home", [("ses_old", "/work/app", None, int(started * 1000) - 600_000, None)])
    assert _tracker("opencode", tmp_path).final(started) is None


def test_opencode_without_a_database_is_unknown(tmp_path):
    assert _tracker("opencode", tmp_path).final(0) is None
