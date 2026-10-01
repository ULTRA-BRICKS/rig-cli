"""Unit tests for riglib.omp_launcher — the tmux pane map and save/restore executors.

Hosting a harness in abduco (run/attach/list, the crash supervisor, the PATH wrapper) is
`riglib.harness_launcher` and is tested in tests/test_harness_launcher.py."""

import json
import tempfile
import time
import unittest
from pathlib import Path

from riglib import omp_launcher as ol


class EncodingAndSessionFileTests(unittest.TestCase):
    def test_encode_cwd_relative_to_home_dots_preserved(self):
        home = Path("/Users/u")
        self.assertEqual(
            ol.encode_cwd("/Users/u/xp/review-cli/.worktrees/450-mimo-seat", home),
            "-xp-review-cli-.worktrees-450-mimo-seat",
        )

    def test_encode_cwd_outside_home_falls_back_to_full_path(self):
        home = Path("/Users/u")
        self.assertEqual(ol.encode_cwd("/opt/other", home), "-opt-other")

    def test_newest_session_file_picks_latest_mtime(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            older = d / "a.jsonl"
            newer = d / "b.jsonl"
            older.write_text("{}")
            time.sleep(0.02)
            newer.write_text("{}")
            self.assertEqual(ol.newest_session_file(d), newer)

    def test_newest_session_file_ignores_nested_task_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "a.jsonl").write_text("{}")
            nested = d / "a"
            nested.mkdir()
            (nested / "child.jsonl").write_text("{}")
            self.assertEqual(ol.newest_session_file(d), d / "a.jsonl")

    def test_newest_session_file_missing_dir_is_none(self):
        self.assertIsNone(ol.newest_session_file(Path("/no/such/dir")))


class MapFileTests(unittest.TestCase):
    def test_render_and_parse_roundtrip_with_durable(self):
        line = ol.render_map_line("main:0.0", "/x", "/x/s.jsonl", "durable-1")
        self.assertEqual(
            ol.parse_map_line(line), ("main:0.0", "/x", "/x/s.jsonl", "durable-1")
        )

    def test_render_and_parse_roundtrip_without_durable(self):
        line = ol.render_map_line("main:0.0", "/x", "/x/s.jsonl", None)
        self.assertEqual(
            ol.parse_map_line(line), ("main:0.0", "/x", "/x/s.jsonl", None)
        )

    def test_parse_legacy_three_column_line_is_backward_compatible(self):
        self.assertEqual(
            ol.parse_map_line("main:0.0\t/x\t/x/s.jsonl"),
            ("main:0.0", "/x", "/x/s.jsonl", None),
        )

    def test_parse_rejects_short_lines(self):
        self.assertIsNone(ol.parse_map_line("only-one-field"))

    def test_write_map_atomic_then_read_back(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "omp-sessions.map"
            entries = [
                ("a:0.0", "/x", "/x/s.jsonl", "dur-1"),
                ("b:0.0", "/y", "/y/t.jsonl", None),
            ]
            ol.write_map_atomic(path, entries)
            self.assertEqual(ol.read_map(path), entries)
            # no leftover temp file.
            self.assertEqual(list(Path(d).glob("*.tmp-*")), [])

    def test_read_map_missing_file_is_empty(self):
        self.assertEqual(ol.read_map(Path("/no/such/map")), [])

    def test_write_map_atomic_never_leaves_partial_file_on_reread(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "m"
            ol.write_map_atomic(path, [("a", "/x", "/x/s.jsonl", None)])
            ol.write_map_atomic(
                path, []
            )  # a later empty save must fully replace, not append
            self.assertEqual(ol.read_map(path), [])


class RestoreOnePaneDecisionTests(unittest.TestCase):
    """Exercises restore_one_pane's DECISION only (the plan_restore call); the tmux
    send-keys execution is a real subprocess call left to the tmux-backed integration test."""

    def test_legacy_resume_does_not_require_abduco_listing_when_live_inspection_is_known(self):
        with tempfile.TemporaryDirectory() as d:
            session = Path(d) / "session.jsonl"
            session.write_text(json.dumps({"type": "session", "id": "sid-legacy", "cwd": "/cwd"}) + "\n")
            old_empty = ol._pane_is_empty_shell
            old_send = ol.send_keys
            old_resolve = ol.resolve_rig_binary
            sent = []
            try:
                ol._pane_is_empty_shell = lambda *a, **k: True
                ol.send_keys = lambda addr, command, tmux_bin: sent.append(command) or True
                ol.resolve_rig_binary = lambda requested=None: "/usr/local/bin/rig"
                plan = ol.restore_one_pane(
                    "addr", "/cwd", str(session), None,
                    live=[], live_ok=True, durable_sockets=set(), durable_ok=False,
                )
            finally:
                ol._pane_is_empty_shell = old_empty
                ol.send_keys = old_send
                ol.resolve_rig_binary = old_resolve
            self.assertEqual(plan.action, "resume")
            self.assertEqual(plan.reason, "original_not_running")
            self.assertEqual(len(sent), 1)
            self.assertIn("harness run omp --host-id omp-recover-", sent[0])
            self.assertIn(f"--resume {session}", sent[0])

    def test_durable_restore_still_blocks_when_abduco_listing_is_unavailable(self):
        with tempfile.TemporaryDirectory() as d:
            session = Path(d) / "session.jsonl"
            session.write_text(
                json.dumps({"type": "session", "id": "sid-durable", "cwd": "/cwd"}) + "\n"
            )
            old_empty = ol._pane_is_empty_shell
            old_send = ol.send_keys
            sent = []
            try:
                ol._pane_is_empty_shell = lambda *a, **k: True
                ol.send_keys = lambda addr, command, tmux_bin: sent.append(command) or True
                plan = ol.restore_one_pane(
                    "addr",
                    "/cwd",
                    str(session),
                    "dur-1",
                    live=[],
                    live_ok=True,
                    durable_sockets=set(),
                    durable_ok=False,
                )
            finally:
                ol._pane_is_empty_shell = old_empty
                ol.send_keys = old_send
            self.assertEqual(plan.action, "blocked")
            self.assertEqual(plan.reason, "inspection_unavailable")
            self.assertEqual(len(sent), 1)
            self.assertIn("inspection_unavailable", sent[0])
            self.assertNotIn(" harness run ", sent[0])
            self.assertNotIn(" harness attach ", sent[0])

    def test_durable_terminal_attach_does_not_call_tmux_when_pane_unreachable(self):
        plan = ol.restore_one_pane(
            "addr",
            "/cwd",
            "/x.jsonl",
            "dur-1",
            live=[],
            live_ok=True,
            durable_sockets={"dur-1"},
            durable_ok=True,
            tmux_bin="/no/such/tmux-binary",
        )
        self.assertEqual(plan.action, "blocked")
        self.assertEqual(plan.reason, "pane_unreachable")


class DurableResumeCommandTests(unittest.TestCase):
    def test_resume_reuses_the_saved_durable_name_as_the_host_id(self):
        """A saved pane whose hosted session is gone relaunches under the SAME name, so the
        tg-ctl delivery address and the next save/restore cycle stay stable."""
        with tempfile.TemporaryDirectory() as d:
            session = Path(d) / "session.jsonl"
            session.write_text(json.dumps({"type": "session", "id": "sid-1", "cwd": "/cwd"}) + "\n")
            old_empty, old_send, old_resolve = ol._pane_is_empty_shell, ol.send_keys, ol.resolve_rig_binary
            sent = []
            try:
                ol._pane_is_empty_shell = lambda *a, **k: True
                ol.send_keys = lambda addr, command, tmux_bin: sent.append(command) or True
                ol.resolve_rig_binary = lambda requested=None: "/usr/local/bin/rig"
                plan = ol.restore_one_pane(
                    "addr", "/cwd", str(session), "omp-dur1",
                    live=[], live_ok=True, durable_sockets=set(), durable_ok=True,
                )
            finally:
                ol._pane_is_empty_shell, ol.send_keys, ol.resolve_rig_binary = old_empty, old_send, old_resolve
            self.assertEqual(plan.action, "resume")
            self.assertEqual(
                sent,
                [f"cd /cwd && /usr/local/bin/rig harness run omp --host-id omp-dur1 --resume {session}"],
            )


if __name__ == "__main__":
    unittest.main()

# Never let pure argv tests create state under the real user's home.
import pytest


@pytest.fixture(autouse=True)
def private_abduco_namespace(tmp_path, monkeypatch):
    monkeypatch.setenv("RIG_OMP_ABDUCO_DIR", str(tmp_path / "abduco-test"))
