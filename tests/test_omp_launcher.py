"""Unit + targeted integration tests for riglib.omp_launcher.

The `os.execvpe`-replacing paths (run_session/attach_session/exec_or_wrap) are tested via
dependency-injected `exec_fn` for pure argv/env assembly, and via a REAL short-lived
abduco-in-a-subprocess integration test (never touching a shared/live abduco instance — every
test here sets RIG_OMP_ABDUCO_DIR to an isolated tmp dir)."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from shutil import which

from riglib import omp_launcher as ol
from riglib.omp_recovery import OriginalSession


class BuildArgvTests(unittest.TestCase):
    def test_run_argv_is_connect_or_create(self):
        argv = ol.build_run_argv(
            ["omp", "--resume", "/x.jsonl"], "sid-1", abduco_bin="abduco"
        )
        self.assertEqual(
            argv, ["abduco", "-f", "-A", "sid-1", "omp", "--resume", "/x.jsonl"]
        )

    def test_run_argv_rejects_empty_command(self):
        with self.assertRaises(ValueError):
            ol.build_run_argv([], "sid-1")

    def test_attach_argv(self):
        self.assertEqual(ol.build_attach_argv("sid-1"), ["abduco", "-a", "sid-1"])

    def test_run_env_sets_durable_marker_and_socket_dir(self):
        env = ol.run_env("sid-1", base_env={"PATH": "/bin"})
        self.assertEqual(env["RIG_OMP_SESSION"], "sid-1")
        self.assertIn("ABDUCO_SOCKET_DIR", env)
        self.assertEqual(env["PATH"], "/bin")


class RunSessionInjectedExecTests(unittest.TestCase):
    def test_run_session_generates_id_and_calls_exec_fn(self):
        calls = []

        def fake_exec(path, argv, env):
            calls.append((path, argv, env))

        sid = ol.run_session(["omp"], exec_fn=fake_exec)
        self.assertTrue(sid.startswith("omp-"))
        self.assertEqual(len(calls), 1)
        path, argv, env = calls[0]
        self.assertEqual(path, "abduco")
        self.assertEqual(argv, ["abduco", "-f", "-A", sid, "omp"])
        self.assertEqual(env["RIG_OMP_SESSION"], sid)

    def test_run_session_honors_explicit_session_id(self):
        calls = []
        ol.run_session(
            ["omp"], session_id="fixed-id", exec_fn=lambda p, a, e: calls.append(a)
        )
        self.assertEqual(calls[0][3], "fixed-id")

    def test_attach_session_calls_exec_fn(self):
        calls = []
        ol.attach_session("sid-1", exec_fn=lambda p, a, e: calls.append((p, a, e)))
        p, a, e = calls[0]
        self.assertEqual(a, ["abduco", "-a", "sid-1"])
        self.assertEqual(e["RIG_OMP_SESSION"], "sid-1")


class AgentRootAndEncodingTests(unittest.TestCase):
    def test_default_agent_root(self):
        home = Path("/home/u")
        self.assertEqual(
            ol.resolve_agent_root(env={}, home=home), home / ".omp" / "agent"
        )

    def test_pi_coding_agent_dir_full_override(self):
        home = Path("/home/u")
        root = ol.resolve_agent_root(
            env={"PI_CODING_AGENT_DIR": "/custom/root"}, home=home
        )
        self.assertEqual(root, Path("/custom/root"))

    def test_pi_config_dir_renames_dot_omp(self):
        home = Path("/home/u")
        root = ol.resolve_agent_root(env={"PI_CONFIG_DIR": "~/.myomp"}, home=home)
        self.assertEqual(root, home / ".myomp" / "agent")

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
            self.assertIn("rig omp run --session-id", sent[0])

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
            self.assertNotIn(" rig omp run ", sent[0])
            self.assertNotIn(" rig omp attach ", sent[0])

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


class WrapperInstallTests(unittest.TestCase):
    def test_install_writes_wrapper_and_config_without_touching_real_bin(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            real_bin = d / "real-omp"
            real_bin.write_text("#!/bin/sh\necho real\n")
            target = d / "bin" / "omp"
            config = d / "cfg.json"
            ol.install_wrapper(target, str(real_bin), config)
            self.assertTrue(target.exists())
            self.assertIn("rig omp exec", target.read_text())
            self.assertEqual(ol.read_wrapper_real_bin(config), str(real_bin.resolve()))
            # the vendor binary itself is untouched.
            self.assertEqual(real_bin.read_text(), "#!/bin/sh\necho real\n")

    def test_read_wrapper_real_bin_missing_config_is_none(self):
        self.assertIsNone(ol.read_wrapper_real_bin(Path("/no/such/config.json")))

    def test_exec_or_wrap_passes_through_headless_unwrapped(self):
        with tempfile.TemporaryDirectory() as d:
            config = Path(d) / "cfg.json"
            config.write_text(json.dumps({"real_bin": "/real/omp"}))
            calls = []
            ol.exec_or_wrap(
                ["-p", "hello"],
                config_path=config,
                exec_fn=lambda p, a, e: calls.append((p, a)),
            )
            self.assertEqual(calls, [("/real/omp", ["/real/omp", "-p", "hello"])])

    def test_exec_or_wrap_wraps_interactive_launch_durably(self):
        with tempfile.TemporaryDirectory() as d:
            config = Path(d) / "cfg.json"
            config.write_text(json.dumps({"real_bin": "/real/omp"}))
            calls = []
            ol.exec_or_wrap(
                ["--resume", "/x.jsonl"],
                config_path=config,
                exec_fn=lambda p, a, e: calls.append((p, a, e)),
            )
            path, argv, env = calls[0]
            self.assertEqual(path, "abduco")
            self.assertEqual(argv[:3], ["abduco", "-f", "-A"])
            self.assertIn("/real/omp", argv)
            self.assertIn("RIG_OMP_SESSION", env)

    def test_exec_or_wrap_raises_without_installed_config(self):
        with self.assertRaises(RuntimeError):
            ol.exec_or_wrap(
                ["--resume", "/x.jsonl"], config_path=Path("/no/such/cfg.json")
            )


@unittest.skipUnless(which("abduco"), "abduco not installed")
class AbducoIntegrationTests(unittest.TestCase):
    """Real abduco, on an isolated private socket dir — never a shared/live instance. Proves
    the brief's acceptance criterion: attaching to a durable session reuses the SAME
    underlying fixture process, it is not relaunched."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["RIG_OMP_ABDUCO_DIR"] = self.tmp.name
        self.addCleanup(lambda: os.environ.pop("RIG_OMP_ABDUCO_DIR", None))
        self._pids_to_kill: list[int] = []
        self.addCleanup(self._cleanup_pids)

    def _cleanup_pids(self):
        for pid in self._pids_to_kill:
            try:
                os.kill(pid, 9)
            except OSError:
                pass

    def _wait_for_file(self, path: Path, timeout: float = 5.0) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if path.exists():
                return path.read_text().strip()
            time.sleep(0.1)
        self.fail(f"{path} never appeared")

    def test_attach_reuses_same_underlying_pid(self):
        pidfile = Path(self.tmp.name) / "fixture.pid"
        env = dict(os.environ)
        launcher = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from riglib.omp_launcher import run_session; "
                f"run_session(['bash','-c','echo $$ > {pidfile}; sleep 30'], session_id='it-attach-test')",
            ],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(lambda: (launcher.kill(), launcher.wait()))
        pid1 = self._wait_for_file(pidfile)
        self._pids_to_kill.append(int(pid1))

        names, ok = ol.list_sessions()
        self.assertTrue(ok)
        self.assertIn("it-attach-test", {s["name"] for s in names})

        attacher = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from riglib.omp_launcher import attach_session; attach_session('it-attach-test')",
            ],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(0.5)
            pid2 = pidfile.read_text().strip()
            self.assertEqual(
                pid1, pid2, "attaching must not relaunch the fixture process"
            )
        finally:
            attacher.terminate()
            attacher.wait()

    def test_restore_one_pane_attaches_via_durable_socket(self):
        """End-to-end for the recovery decision this ticket exists for: a saved pane whose
        durable socket is still alive must ATTACH, never resume/relaunch — proven against a
        real abduco session (no tmux needed for the decision itself; restore_one_pane's tmux
        calls are exercised for the pane-unreachable path above, and the tmux e2e file covers
        the full pane-in-tmux path)."""
        pidfile = Path(self.tmp.name) / "fixture2.pid"
        env = dict(os.environ)
        launcher = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from riglib.omp_launcher import run_session; "
                f"run_session(['bash','-c','echo $$ > {pidfile}; sleep 30'], session_id='it-restore-test')",
            ],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(lambda: (launcher.kill(), launcher.wait()))
        pid1 = self._wait_for_file(pidfile)
        self._pids_to_kill.append(int(pid1))
        from riglib import omp_inspect as oi

        durable_sockets, ok = oi.list_abduco_sessions()
        self.assertTrue(ok)
        saved = OriginalSession(
            int(pid1), "instance-x", "/proj", "it-restore-test", "it-restore-test"
        )
        from riglib.omp_recovery import SavedPane, plan_restore

        plan = plan_restore(
            SavedPane(
                "addr", "/proj", "it-restore-test", str(pidfile), "it-restore-test"
            ),
            [saved],
            durable_sockets,
            inspection_ok=True,
            session_file_exists=True,
            pane_is_empty_shell=True,
        )
        self.assertEqual(plan.action, "attach")
        self.assertEqual(plan.argv, ("rig", "omp", "attach", "it-restore-test"))


if __name__ == "__main__":
    unittest.main()

# Never let pure argv tests create state under the real user's home.
import pytest


@pytest.fixture(autouse=True)
def private_abduco_namespace(tmp_path, monkeypatch):
    monkeypatch.setenv("RIG_OMP_ABDUCO_DIR", str(tmp_path / "abduco-test"))
