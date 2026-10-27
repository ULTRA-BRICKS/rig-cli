"""Deterministic sequencing pin for ``_wait_for_restored_pane_text`` (rig-cli#772).

The omp-restore e2e test used to capture the pane as soon as the fake omp's argv log existed, but
abduco is still attached to tmux's alternate screen at that moment, so the capture was blank. This
pins the wait ordering with a fake tmux and a fake clock: no real tmux, runs in the default suite.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace


def _helper():
    return importlib.import_module("tests.test_tmux_e2e")._wait_for_restored_pane_text


class _FakeTmux:
    """Answers ``display-message #{alternate_on}`` from a script, one answer per query, and
    ``capture-pane`` with a text that depends on the last alternate_on answer."""

    def __init__(self, alternate_answers):
        self.alternate_answers = list(alternate_answers)
        self.last = None
        self.captures = []

    def __call__(self, cmd, **kw):
        if cmd[1] == "display-message":
            self.last = self.alternate_answers.pop(0) if len(self.alternate_answers) > 1 else self.alternate_answers[0]
            return SimpleNamespace(stdout=f"{self.last}\n")
        assert cmd[1] == "capture-pane"
        text = "" if self.last == "1" else "rig harness run omp --host-id omp-recover-x"
        self.captures.append(text)
        return SimpleNamespace(stdout=text)


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.now += s


def test_keeps_waiting_while_alternate_screen_is_on_even_if_argv_log_exists(tmp_path):
    argv_log = tmp_path / "argv.log"
    argv_log.write_text("x", encoding="utf-8")  # present from the start
    run = _FakeTmux(["1", "1", "1", "0"])
    clock = _Clock()
    text = _helper()(run, "main:0.0", argv_log, sleep=clock.sleep, clock=clock)
    assert text == "rig harness run omp --host-id omp-recover-x"
    assert run.captures == [text]  # one capture, only after the alt screen went off
    assert run.alternate_answers == ["0"]


def test_waits_for_argv_log_even_when_alternate_screen_is_off(tmp_path):
    argv_log = tmp_path / "argv.log"
    run = _FakeTmux(["0"])
    clock = _Clock()

    def sleep(s):
        clock.sleep(s)
        if clock.now >= 1.0:
            argv_log.write_text("x", encoding="utf-8")

    _helper()(run, "main:0.0", argv_log, sleep=sleep, clock=clock)
    assert clock.now >= 1.0


def test_deadline_is_bounded_and_still_captures_for_the_caller_to_assert(tmp_path):
    argv_log = tmp_path / "argv.log"
    argv_log.write_text("x", encoding="utf-8")
    run = _FakeTmux(["1"])  # never leaves the alternate screen
    clock = _Clock()
    text = _helper()(run, "main:0.0", argv_log, timeout_s=15, sleep=clock.sleep, clock=clock)
    assert text == ""
    assert 15 <= clock.now < 15.5
