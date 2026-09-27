"""riglib.tmux_watchdog — bounded tmux server-identity recovery.

The SIGUSR1 socket-recreation path is proven against a REAL tmux server on a PRIVATE ``-L``
socket (never the user's real tmux) — killed/restarted per the ticket's own test requirement.
"""

from __future__ import annotations

import subprocess
import time
import unittest
from pathlib import Path
from shutil import which

from riglib import tmux_watchdog as wd
from riglib.omp_recovery import ServerIdentity


def _tmux_available() -> bool:
    return which("tmux") is not None


class CooldownTests(unittest.TestCase):
    def test_no_record_means_not_active(self, tmp_path=None):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            self.assertFalse(wd.cooldown_active(home))

    def test_record_then_active_then_expires(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            wd.record_cooldown(home, now=1000.0)
            self.assertTrue(wd.cooldown_active(home, now=1010.0, seconds=60))
            self.assertFalse(wd.cooldown_active(home, now=1100.0, seconds=60))


class LockTests(unittest.TestCase):
    def test_second_acquire_raises_busy(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            wd.acquire_lock(home)
            with self.assertRaises(wd.WatchdogBusy):
                wd.acquire_lock(home)
            wd.release_lock(home)
            # released -> acquirable again.
            wd.acquire_lock(home)
            wd.release_lock(home)

    def test_leftover_pid_file_is_not_a_live_kernel_lock(self):
        import os, tempfile

        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            path = wd.lock_path(home)
            path.parent.mkdir(parents=True)
            path.write_text("99999999")
            wd.acquire_lock(home)
            os.utime(path, (1, 1))
            with self.assertRaises(wd.WatchdogBusy):
                wd.acquire_lock(home)
            wd.release_lock(home)
            wd.acquire_lock(home)
            wd.release_lock(home)


class IdentityPersistTests(unittest.TestCase):
    def test_save_then_load_roundtrip(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            identity = ServerIdentity(
                100, 501, "/bin/tmux", "Mon Jan 1", "/tmp/tmux-501/default"
            )
            wd.save_identity(identity, home)
            self.assertEqual(wd.load_identity(home), identity)

    def test_load_missing_is_none(self):
        self.assertIsNone(wd.load_identity(Path("/no/such/home")))

    def test_load_corrupt_is_none(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            wd.identity_path(home).parent.mkdir(parents=True)
            wd.identity_path(home).write_text("not json")
            self.assertIsNone(wd.load_identity(home))


class ForensicLogTests(unittest.TestCase):
    def test_appends_lines(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            wd.append_forensic_log("hello", home)
            wd.append_forensic_log("world", home)
            text = wd.forensic_log_path(home).read_text()
            self.assertIn("hello", text)
            self.assertIn("world", text)

    def test_log_is_bounded_never_free_running(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            path = wd.forensic_log_path(home)
            path.parent.mkdir(parents=True)
            path.write_text("x" * (wd.FORENSIC_LOG_MAX_BYTES + 1000))
            wd.append_forensic_log("new-entry", home)
            self.assertLess(path.stat().st_size, wd.FORENSIC_LOG_MAX_BYTES + 1000)
            self.assertIn("new-entry", path.read_text())


@unittest.skipUnless(_tmux_available(), "tmux not installed")
class TmuxSigusr1RecoveryTests(unittest.TestCase):
    """Real tmux, PRIVATE `-L` socket — proves the SIGUSR1 recovery path end to end, and that
    the same server pid survives (never a second server)."""

    SOCKET_NAME = "rig-test-watchdog-e2e"

    def setUp(self):
        subprocess.run(
            ["tmux", "-L", self.SOCKET_NAME, "kill-server"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
        )
        self.addCleanup(
            lambda: subprocess.run(
                ["tmux", "-L", self.SOCKET_NAME, "kill-server"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
            )
        )

    def _socket_path(self) -> Path:
        import os

        uid = os.getuid()
        return (
            Path(f"/private/tmp/tmux-{uid}/{self.SOCKET_NAME}")
            if os.path.isdir(f"/private/tmp/tmux-{uid}")
            else Path(f"/tmp/tmux-{uid}/{self.SOCKET_NAME}")
        )

    def _server_pid(self) -> int:
        result = subprocess.run(
            ["tmux", "-L", self.SOCKET_NAME, "display-message", "-p", "#{pid}"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=True,
        )
        return int(result.stdout.strip())

    def test_socket_removed_then_run_once_recovers_same_server(self):
        subprocess.run(
            [
                "tmux",
                "-L",
                self.SOCKET_NAME,
                "new-session",
                "-d",
                "-s",
                "w1",
                "sleep 300",
            ],
            check=True,
            stdin=subprocess.DEVNULL,
        )
        time.sleep(0.3)
        sock = self._socket_path()
        self.assertTrue(sock.exists())
        pid = self._server_pid()

        import tempfile

        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            # seed the "last known good" identity from the live server, as a healthy prior
            # tick would have.
            plan_before = wd.run_once(sock, home=home)
            self.assertEqual(plan_before.action, "none")
            self.assertEqual(plan_before.reason, "server_healthy")
            identity = wd.load_identity(home)
            self.assertIsNotNone(identity)
            self.assertEqual(identity.pid, pid)

            sock.unlink()
            self.assertFalse(sock.exists())

            plan = wd.run_once(sock, home=home)
            self.assertEqual(plan.action, "restore_socket")
            self.assertEqual(plan.pid, pid)

            time.sleep(0.3)
            self.assertTrue(sock.exists(), "SIGUSR1 must recreate the socket")
            self.assertEqual(
                self._server_pid(),
                pid,
                "must recover the SAME server, never a second one",
            )

    def test_cooldown_prevents_repeat_signal_within_window(self):
        subprocess.run(
            [
                "tmux",
                "-L",
                self.SOCKET_NAME,
                "new-session",
                "-d",
                "-s",
                "w1",
                "sleep 300",
            ],
            check=True,
            stdin=subprocess.DEVNULL,
        )
        time.sleep(0.3)
        sock = self._socket_path()
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            wd.run_once(sock, home=home)  # seed identity
            sock.unlink()
            first = wd.run_once(sock, home=home, cooldown_seconds=300)
            self.assertEqual(first.action, "restore_socket")
            time.sleep(0.3)
            sock.unlink()  # simulate it going missing again immediately
            second = wd.run_once(sock, home=home, cooldown_seconds=300)
            self.assertEqual(second.action, "none")
            self.assertEqual(second.reason, "cooldown")


if __name__ == "__main__":
    unittest.main()
