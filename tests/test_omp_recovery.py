"""No test sends signals to, restarts, or stops a real tmux server."""

import unittest
from riglib.omp_recovery import (
    OriginalSession,
    SavedPane,
    ServerIdentity,
    RestorePlan,
    is_interactive_omp,
    plan_restore,
    plan_server_recovery,
)


class OriginalCommandTests(unittest.TestCase):
    def test_compiled_and_bun_launchers(self):
        for command in [
            "omp --resume /tmp/s.jsonl",
            "/opt/homebrew/bin/omp --resume",
            "bun --preload /Users/u/xp/oh-my-pi/packages/coding-agent/scripts/omp.ts /Users/u/xp/oh-my-pi/packages/coding-agent/src/cli.ts --resume",
            "bun /Users/u/xp/oh-my-pi/packages/coding-agent/src/cli.ts",
        ]:
            with self.subTest(command=command):
                self.assertTrue(is_interactive_omp(command))

    def test_workers_acp_print_and_unrelated_bun_are_not_interactive(self):
        for command in [
            "omp acp --cwd=/tmp",
            "omp -p hello",
            "omp --print hi",
            "omp --mode=rpc",
            "omp --mode rpc",
            "omp models",
            "omp __omp_worker_daemon_broker",
            "bun /some/unrelated/src/cli.ts --resume",
            'python -c "omp --resume"',
            "bun /Users/u/xp/oh-my-pi/packages/coding-agent/src/cli.ts acp",
        ]:
            with self.subTest(command=command):
                self.assertFalse(is_interactive_omp(command))


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.saved = SavedPane(
            "%1", "/same/cwd", "session-a", "/state/a.jsonl", "durable-a"
        )
        self.live = OriginalSession(
            1234, "instance-a", "/same/cwd", "session-a", "durable-a"
        )

    def test_existing_durable_session_attaches_without_resume(self):
        plan = plan_restore(
            self.saved,
            [self.live],
            {"durable-a"},
            inspection_ok=True,
            session_file_exists=True,
            pane_is_empty_shell=True,
        )
        self.assertEqual(plan.action, "attach")
        self.assertEqual(plan.owner_pid, 1234)
        self.assertNotIn("--resume", plan.argv)

    def test_bare_live_original_is_preserved_not_restarted(self):
        original = OriginalSession(1234, "instance-a", "/same/cwd", "session-a", None)
        plan = plan_restore(
            self.saved,
            [original],
            set(),
            inspection_ok=True,
            session_file_exists=True,
            pane_is_empty_shell=True,
        )
        self.assertEqual(plan.action, "blocked")
        self.assertEqual(plan.reason, "live_original_not_attachable")

    def test_same_cwd_other_session_does_not_steal_saved_identity(self):
        other = OriginalSession(42, "instance-b", "/same/cwd", "session-b", None)
        plan = plan_restore(
            self.saved,
            [other],
            set(),
            inspection_ok=True,
            session_file_exists=True,
            pane_is_empty_shell=True,
        )
        self.assertEqual(plan.action, "resume")
        self.assertEqual(plan.argv[-2:], ("--resume", "/state/a.jsonl"))

    def test_unknown_inspector_or_legacy_identity_cannot_allow_duplicate(self):
        unknown = OriginalSession(42, "instance-unknown", "/same/cwd", None, None)
        for live, ok in [([], False), ([unknown], True)]:
            with self.subTest(ok=ok):
                plan = plan_restore(
                    self.saved,
                    live,
                    set(),
                    inspection_ok=ok,
                    session_file_exists=True,
                    pane_is_empty_shell=True,
                )
                self.assertEqual(plan.action, "blocked")

    def test_duplicate_session_ids_fail_closed(self):
        other = OriginalSession(5678, "instance-b", "/same/cwd", "session-a", None)
        plan = plan_restore(
            self.saved,
            [self.live, other],
            {"durable-a"},
            inspection_ok=True,
            session_file_exists=True,
            pane_is_empty_shell=True,
        )
        self.assertEqual(plan.reason, "multiple_live_owners")

    def test_missing_session_never_guesses_continue(self):
        plan = plan_restore(
            self.saved,
            [],
            set(),
            inspection_ok=True,
            session_file_exists=False,
            pane_is_empty_shell=True,
        )
        self.assertEqual(plan.action, "blocked")
        self.assertNotIn("--continue", plan.argv)

    def test_busy_pane_is_never_overwritten(self):
        plan = plan_restore(
            self.saved,
            [],
            set(),
            inspection_ok=True,
            session_file_exists=True,
            pane_is_empty_shell=False,
        )
        self.assertEqual(plan.action, "skip")

    def test_durable_socket_and_session_identity_mismatch_is_blocked(self):
        wrong = OriginalSession(
            1234, "instance-a", "/same/cwd", "session-b", "durable-a"
        )
        plan = plan_restore(
            self.saved,
            [wrong],
            {"durable-a"},
            inspection_ok=True,
            session_file_exists=True,
            pane_is_empty_shell=True,
        )
        self.assertEqual(plan.action, "blocked")


class TmuxRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.old = ServerIdentity(
            100, 501, "/bin/tmux", "start-1", "/private/tmp/tmux-501/default"
        )

    def plan(
        self, current, socket_ok=False, inspection_ok=True, paused=False, cooldown=False
    ):
        return plan_server_recovery(
            self.old,
            current,
            socket_responding=socket_ok,
            inspection_ok=inspection_ok,
            paused=paused,
            cooldown_active=cooldown,
        )

    def test_healthy_server_untouched(self):
        self.assertEqual(self.plan(self.old, socket_ok=True).action, "none")

    def test_missing_socket_live_exact_server_gets_recovery_not_start(self):
        p = self.plan(self.old)
        self.assertEqual((p.action, p.pid), ("restore_socket", 100))

    def test_pid_reuse_is_never_signalled(self):
        reused = ServerIdentity(
            100, 501, "/bin/tmux", "start-2", "/private/tmp/tmux-501/default"
        )
        self.assertEqual(self.plan(reused).action, "blocked")

    def test_missing_inspection_is_not_dead_server(self):
        self.assertEqual(self.plan(None, inspection_ok=False).action, "blocked")

    def test_gone_server_can_restart_but_pause_and_cooldown_are_respected(self):
        self.assertEqual(self.plan(None).action, "start")
        self.assertEqual(self.plan(None, paused=True).action, "none")
        self.assertEqual(self.plan(None, cooldown=True).action, "none")


if __name__ == "__main__":
    unittest.main()
