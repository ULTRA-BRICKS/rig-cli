"""Unit tests for riglib.omp_inspect — parsing/tree-walk pieces are pure given injected data;
the few real-subprocess calls (lsof/abduco/ps) are exercised against real short-lived local
processes, never against a shared/live abduco or tmux instance."""

import os
import subprocess
import tempfile
import time
import unittest
from shutil import which

from riglib import omp_inspect as oi


class ParsePsRowsTests(unittest.TestCase):
    def test_parses_pid_ppid_args(self):
        snapshot = (
            "  123    1 /usr/bin/omp --resume /tmp/s.jsonl\n456  123 bun /x/cli.ts\n"
        )
        rows = oi.parse_ps_rows(snapshot)
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            rows[0], oi.PsRow(123, 1, "/usr/bin/omp --resume /tmp/s.jsonl")
        )
        self.assertEqual(rows[1], oi.PsRow(456, 123, "bun /x/cli.ts"))

    def test_skips_malformed_lines(self):
        rows = oi.parse_ps_rows("garbage\n\n123 abc args\n")
        self.assertEqual(rows, [])

    def test_handles_args_only_no_trailing(self):
        rows = oi.parse_ps_rows("1 0 \n")
        self.assertEqual(rows, [oi.PsRow(1, 0, "")])


class ParseAbducoArgvTests(unittest.TestCase):
    def test_dash_capital_a_with_command(self):
        self.assertEqual(
            oi.parse_abduco_argv("abduco -A durable-1 omp --resume /tmp/s.jsonl"),
            ("durable-1", "omp --resume /tmp/s.jsonl"),
        )

    def test_bare_attach_has_no_remainder(self):
        self.assertEqual(oi.parse_abduco_argv("abduco -a durable-1"), ("durable-1", ""))

    def test_leading_modifiers_before_connect_flag(self):
        self.assertEqual(
            oi.parse_abduco_argv("abduco -e ^x -r -A durable-1 omp acp"),
            ("durable-1", "omp acp"),
        )

    def test_full_path_basename_match(self):
        self.assertEqual(
            oi.parse_abduco_argv("/opt/homebrew/bin/abduco -A d1 omp"),
            ("d1", "omp"),
        )

    def test_non_abduco_returns_none(self):
        self.assertIsNone(oi.parse_abduco_argv("omp --resume /tmp/s.jsonl"))

    def test_missing_name_returns_none(self):
        self.assertIsNone(oi.parse_abduco_argv("abduco -A"))


class TreeWalkTests(unittest.TestCase):
    def test_finds_omp_descendant_of_pane_shell(self):
        rows = [
            oi.PsRow(1, 0, "-zsh"),
            oi.PsRow(2, 1, "bash -c launch"),
            oi.PsRow(3, 2, "omp --resume /tmp/x.jsonl"),
        ]
        found = oi.find_omp_descendant(1, rows)
        self.assertEqual(found.pid, 3)
        self.assertIsNone(oi.pane_durable_session(found))

    def test_excludes_worker_acp_print_descendants(self):
        rows = [oi.PsRow(1, 0, "-zsh"), oi.PsRow(2, 1, "omp acp --cwd=/tmp")]
        self.assertIsNone(oi.find_omp_descendant(1, rows))

    def test_returns_none_when_no_omp_in_tree(self):
        rows = [oi.PsRow(1, 0, "-zsh"), oi.PsRow(2, 1, "vim foo.py")]
        self.assertIsNone(oi.find_omp_descendant(1, rows))

    def test_finds_bun_launcher_descendant(self):
        rows = [
            oi.PsRow(1, 0, "-zsh"),
            oi.PsRow(
                2,
                1,
                "bun --preload /x/oh-my-pi/packages/coding-agent/scripts/omp.ts "
                "/x/oh-my-pi/packages/coding-agent/src/cli.ts --resume",
            ),
        ]
        found = oi.find_omp_descendant(1, rows)
        self.assertEqual(found.pid, 2)

    def test_finds_durable_abduco_client_and_extracts_session(self):
        rows = [
            oi.PsRow(1, 0, "-zsh"),
            oi.PsRow(2, 1, "abduco -A durable-9 omp --resume /tmp/x.jsonl"),
        ]
        found = oi.find_omp_descendant(1, rows)
        self.assertEqual(found.pid, 2)
        self.assertEqual(oi.pane_durable_session(found), "durable-9")

    def test_finds_bare_durable_reattach(self):
        rows = [oi.PsRow(1, 0, "-zsh"), oi.PsRow(2, 1, "abduco -a durable-9")]
        found = oi.find_omp_descendant(1, rows)
        self.assertEqual(oi.pane_durable_session(found), "durable-9")

    def test_abduco_wrapping_acp_is_not_interactive(self):
        rows = [
            oi.PsRow(1, 0, "-zsh"),
            oi.PsRow(2, 1, "abduco -A d1 omp acp --cwd=/tmp"),
        ]
        self.assertIsNone(oi.find_omp_descendant(1, rows))

    def test_find_all_interactive_omp_is_system_wide(self):
        rows = [
            oi.PsRow(10, 1, "omp --resume /a.jsonl"),
            oi.PsRow(11, 1, "omp acp"),
            oi.PsRow(12, 1, "vim"),
        ]
        found = oi.find_all_interactive_omp(rows)
        self.assertEqual([r.pid for r in found], [10])


class AncestorDurableSessionTests(unittest.TestCase):
    def test_finds_abduco_ancestor(self):
        rows = [
            oi.PsRow(1, 0, "launchd"),
            oi.PsRow(50, 1, "abduco -A durable-a omp --resume /x.jsonl"),
            oi.PsRow(51, 50, "omp --resume /x.jsonl"),
        ]
        self.assertEqual(oi.find_ancestor_durable_session(51, rows), "durable-a")

    def test_no_abduco_ancestor_is_none(self):
        rows = [oi.PsRow(1, 0, "launchd"), oi.PsRow(51, 1, "omp --resume /x.jsonl")]
        self.assertIsNone(oi.find_ancestor_durable_session(51, rows))

    def test_bounded_against_cycle(self):
        rows = [oi.PsRow(5, 5, "omp --resume /x.jsonl")]
        self.assertIsNone(oi.find_ancestor_durable_session(5, rows, max_hops=3))


class LocalLiveOmpSessionsTests(unittest.TestCase):
    def test_missing_lsof_is_inspection_failure(self):
        rows = [oi.PsRow(10, 1, "omp --resume /a.jsonl")]
        orig_which = oi.which
        oi.which = lambda name: None  # type: ignore[assignment]
        try:
            sessions, ok = oi.local_live_omp_sessions(rows)
        finally:
            oi.which = orig_which
        self.assertFalse(ok)
        self.assertEqual(sessions, [])

    def test_no_candidates_is_ok_with_empty_list(self):
        sessions, ok = oi.local_live_omp_sessions([oi.PsRow(1, 0, "-zsh")])
        self.assertTrue(ok)
        self.assertEqual(sessions, [])


class SavedSessionIdTests(unittest.TestCase):
    def test_actual_header_id_is_independent_of_durable_socket_name(self):
        import json, tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "different-filename.jsonl"
            path.write_text(
                json.dumps({"type": "session", "id": "exact-id", "cwd": "/tmp"}) + "\n"
            )
            self.assertEqual(oi.saved_session_id_for(str(path)), "exact-id")

    def test_missing_header_does_not_guess_a_filename_or_durable_id(self):
        self.assertEqual(oi.saved_session_id_for("/nonexistent/s.jsonl"), "")


class RealSubprocessTests(unittest.TestCase):
    """Exercises the actual lsof subprocess call against a real short-lived local process —
    never a shared/live abduco daemon or tmux server."""

    def setUp(self):
        if not oi.lsof_available():
            self.skipTest("lsof not installed")

    def test_process_cwd_matches_real_cwd(self):
        with tempfile.TemporaryDirectory() as d:
            proc = subprocess.Popen(
                ["sleep", "5"],
                cwd=d,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                time.sleep(0.3)
                cwd = oi.process_cwd(proc.pid)
                self.assertIsNotNone(cwd)
                self.assertEqual(os.path.realpath(cwd), os.path.realpath(d))
            finally:
                proc.kill()
                proc.wait()

    def test_process_cwd_none_for_dead_pid(self):
        # a pid that (almost certainly) does not exist.
        self.assertIsNone(oi.process_cwd(2**30))


@unittest.skipUnless(which("abduco"), "abduco not installed")
class AbducoListTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["RIG_OMP_ABDUCO_DIR"] = self.tmp.name
        self.addCleanup(lambda: os.environ.pop("RIG_OMP_ABDUCO_DIR", None))

    def test_lists_created_session(self):
        proc = subprocess.Popen(
            ["abduco", "-A", "rig-inspect-test", "sleep", "5"],
            env=oi.abduco_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(0.5)
            names, ok = oi.list_abduco_sessions()
            self.assertTrue(ok)
            self.assertIn("rig-inspect-test", names)
        finally:
            proc.kill()
            proc.wait()

    def test_missing_binary_is_inspection_failure_not_empty(self):
        names, ok = oi.list_abduco_sessions(abduco_bin="/no/such/abduco-binary")
        self.assertFalse(ok)
        self.assertEqual(names, set())


class TgCtlFallbackTests(unittest.TestCase):
    def test_missing_tg_ctl_binary_is_inspection_failure(self):
        sessions, ok = oi.query_tg_ctl_omp_live(tg_ctl_bin="/no/such/tg-ctl-binary")
        self.assertFalse(ok)
        self.assertEqual(sessions, [])

    def test_live_omp_sessions_falls_back_to_local_when_tg_ctl_missing(self):
        sessions, ok = oi.live_omp_sessions(tg_ctl_bin="/no/such/tg-ctl-binary")
        # local ps/lsof inspection is always attempted and should succeed on this machine.
        self.assertTrue(ok)
        self.assertIsInstance(sessions, list)


if __name__ == "__main__":
    unittest.main()
