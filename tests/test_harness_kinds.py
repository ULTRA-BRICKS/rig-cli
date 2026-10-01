"""Tests for riglib.harness_kinds — which launches are interactive sessions and how each harness is
relaunched onto the conversation it was in. Pure: no processes, no files."""

from __future__ import annotations

import pytest

from riglib import harness_kinds as hk
from riglib.harness_kinds import SessionRef


# ── omp: the salvaged restart rule, mirrored from oh-my-pi's own /restart ────────────────────
@pytest.mark.parametrize(
    ("argv", "ref", "expected"),
    [
        (["/o/omp", "--resume", "/a.jsonl"], SessionRef(file="/b.jsonl"), ["/o/omp", "--resume", "/b.jsonl"]),
        (["/o/omp", "--resume=/a.jsonl"], SessionRef(file="/b.jsonl"), ["/o/omp", "--resume", "/b.jsonl"]),
        (["/o/omp"], SessionRef(file="/b.jsonl"), ["/o/omp", "--resume", "/b.jsonl"]),
        # configuration flags (and their values) survive; the one-shot session choice and the
        # initial prompt do not — the prompt is already in the resumed transcript.
        (
            ["/o/omp", "--model", "opus", "-c", "--no-lsp", "--", "fix it"],
            SessionRef(file="/b.jsonl"),
            ["/o/omp", "--model", "opus", "--no-lsp", "--resume", "/b.jsonl"],
        ),
        (["/o/omp", "--fork", "/x.jsonl", "do it"], SessionRef(file="/b.jsonl"), ["/o/omp", "--resume", "/b.jsonl"]),
        # a lazy session that never reached disk: relaunch fresh with the same configuration.
        (["/o/omp", "-r", "/a.jsonl", "--model", "m"], SessionRef(fresh=True), ["/o/omp", "--model", "m"]),
        # nothing known about the conversation: the command line already names what it resumed.
        (["/o/omp", "--resume", "/a.jsonl"], None, ["/o/omp", "--resume", "/a.jsonl"]),
    ],
)
def test_omp_relaunch(argv, ref, expected):
    assert hk.relaunch_argv("omp", argv, ref) == expected


# ── the other harnesses' native resume ───────────────────────────────────────────────────────
def test_claude_resumes_a_persisted_conversation_by_id():
    ref = SessionRef(id="9f1c", file="/t/9f1c.jsonl")
    assert hk.relaunch_argv("claude", ["/c/claude", "--model", "opus", "--session-id", "old"], ref) == [
        "/c/claude", "--model", "opus", "--resume", "9f1c",
    ]  # fmt: skip


def test_claude_that_died_before_its_first_message_restarts_under_the_same_id():
    # `--resume` needs a transcript on disk; without one the conversation is created again.
    assert hk.relaunch_argv("claude", ["/c/claude", "--session-id", "9f1c"], SessionRef(id="9f1c")) == [
        "/c/claude", "--session-id", "9f1c",
    ]  # fmt: skip


@pytest.mark.parametrize(
    "argv",
    [
        ["/c/claude", "--resume", "old", "--model", "opus"],
        ["/c/claude", "-r", "old", "--model", "opus"],
        ["/c/claude", "--continue", "--model", "opus"],
        ["/c/claude", "--resume", "--model", "opus"],  # `--resume` with its optional value omitted
        ["/c/claude", "--model", "opus", "--fork-session", "--resume=old"],
        ["/c/claude", "--model", "opus", "fix the build"],  # the initial prompt is not replayed
    ],
)
def test_claude_relaunch_drops_the_one_shot_session_choice(argv):
    got = hk.relaunch_argv("claude", argv, SessionRef(id="new", file="/t/new.jsonl"))
    assert got == ["/c/claude", "--model", "opus", "--resume", "new"]


def test_claude_variadic_options_keep_all_their_values_but_not_the_prompt_after_a_flag():
    argv = ["/c/claude", "--allowedTools", "Edit", "Bash", "--verbose", "go"]
    assert hk.relaunch_argv("claude", argv, SessionRef(id="s", file="/t/s.jsonl")) == [
        "/c/claude", "--allowedTools", "Edit", "Bash", "--verbose", "--resume", "s",
    ]  # fmt: skip


def test_codex_resumes_with_the_resume_subcommand_after_its_global_options():
    argv = ["/x/codex", "-c", 'model="o3"', "--search", "-m", "gpt-5", "fix the tests"]
    assert hk.relaunch_argv("codex", argv, SessionRef(id="019d-aa")) == [
        "/x/codex", "-c", 'model="o3"', "--search", "-m", "gpt-5", "resume", "019d-aa",
    ]  # fmt: skip


def test_codex_relaunch_of_a_resumed_session_moves_to_the_new_id():
    argv = ["/x/codex", "--no-alt-screen", "resume", "--last", "old-id"]
    assert hk.relaunch_argv("codex", argv, SessionRef(id="new-id")) == [
        "/x/codex", "--no-alt-screen", "resume", "new-id",
    ]  # fmt: skip


def test_opencode_resumes_with_session_and_keeps_the_project_operand():
    argv = ["/o/opencode", "/repo/app", "-m", "zai/glm", "--continue", "--prompt", "go", "--fork"]
    assert hk.relaunch_argv("opencode", argv, SessionRef(id="ses_abc")) == [
        "/o/opencode", "/repo/app", "-m", "zai/glm", "--session", "ses_abc",
    ]  # fmt: skip


@pytest.mark.parametrize("kind", hk.KINDS)
def test_without_a_known_conversation_the_previous_command_is_reused(kind):
    argv = [f"/x/{kind}", "--model", "m"]
    assert hk.relaunch_argv(kind, argv, None) == argv


@pytest.mark.parametrize("kind", ["claude", "codex", "opencode"])
def test_a_conversation_that_never_persisted_relaunches_fresh(kind):
    argv = [f"/x/{kind}", "--model", "m"]
    assert hk.relaunch_argv(kind, argv, SessionRef(fresh=True)) == argv


def test_relaunch_is_idempotent():
    ref = SessionRef(id="s", file="/t/s.jsonl")
    once = hk.relaunch_argv("claude", ["/c/claude", "--model", "m", "prompt"], ref)
    assert hk.relaunch_argv("claude", once, ref) == once


# ── a fresh launch learns its conversation id up front (Claude Code only) ────────────────────
def test_claude_is_started_under_an_id_the_wrapper_chose():
    argv, ref = hk.prepare_launch("claude", ["/c/claude", "--model", "m"], new_id="11111111-2222-3333-4444-555555555555")
    assert argv == ["/c/claude", "--model", "m", "--session-id", "11111111-2222-3333-4444-555555555555"]
    assert ref == SessionRef(id="11111111-2222-3333-4444-555555555555")


@pytest.mark.parametrize(
    ("given", "expected_id"),
    [
        (["--resume", "abc"], "abc"),
        (["-r", "abc"], "abc"),
        (["--resume=abc"], "abc"),
        (["--session-id", "abc"], "abc"),
        (["--continue"], None),
        (["--from-pr", "12"], None),
    ],
)
def test_claude_launch_that_names_its_own_conversation_is_left_alone(given, expected_id):
    argv = ["/c/claude", *given]
    prepared, ref = hk.prepare_launch("claude", argv, new_id="never-used")
    assert prepared == argv
    assert (ref.id if ref else None) == expected_id


@pytest.mark.parametrize("kind", ["omp", "codex", "opencode"])
def test_other_harnesses_are_started_as_given(kind):
    argv = [f"/x/{kind}", "--model", "m"]
    assert hk.prepare_launch(kind, argv) == (argv, None)


# ── which invocations are a session worth hosting ────────────────────────────────────────────
@pytest.mark.parametrize(
    ("kind", "args", "interactive"),
    [
        ("omp", [], True),
        ("omp", ["--resume", "/s.jsonl"], True),
        ("omp", ["-p", "hi"], False),
        ("omp", ["models"], False),
        ("omp", ["--version"], False),
        ("claude", [], True),
        ("claude", ["--model", "opus", "fix it"], True),
        ("claude", ["--resume", "abc"], True),
        ("claude", ["-p", "hi"], False),
        ("claude", ["--print", "--model", "opus", "hi"], False),
        ("claude", ["--add-dir", "a", "b", "-p", "x"], False),
        ("claude", ["--bg"], False),
        ("claude", ["mcp", "list"], False),
        ("claude", ["--model", "opus", "doctor"], False),
        ("claude", ["update"], False),
        ("claude", ["--version"], False),
        ("claude", ["-h"], False),
        ("codex", [], True),
        ("codex", ["fix the build"], True),
        ("codex", ["-m", "gpt-5", "--search"], True),
        ("codex", ["resume", "--last"], True),
        ("codex", ["fork"], True),
        ("codex", ["exec", "do it"], False),
        ("codex", ["-c", "x=1", "review"], False),
        ("codex", ["login"], False),
        ("codex", ["--version"], False),
        ("opencode", [], True),
        ("opencode", ["/repo"], True),
        ("opencode", ["--session", "ses_a"], True),
        ("opencode", ["pr", "12"], True),
        ("opencode", ["run", "hi"], False),
        ("opencode", ["serve"], False),
        ("opencode", ["models"], False),
        ("opencode", ["--version"], False),
    ],
)
def test_is_interactive(kind, args, interactive):
    assert hk.is_interactive(kind, args) is interactive


def test_unknown_harness_is_refused():
    with pytest.raises(ValueError, match="unknown harness"):
        hk.is_interactive("pi", [])
