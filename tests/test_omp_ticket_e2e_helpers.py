"""Hermetic tests for the real-agent ticket-gate E2E harness (rig-cli#756).

The container run itself needs docker + an LLM credential and lives in test_omp_ticket_e2e.py
(opt-in). Everything here is pure: the recording `task` stub, the scenario catalogue, the
transcript analyzer (on synthetic omp session files) and the run_matrix helpers.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

E2E_DIR = Path(__file__).resolve().parent / "omp_ticket_e2e"
sys.path.insert(0, str(E2E_DIR))

import analyze  # noqa: E402
import run_matrix  # noqa: E402
from scenarios import COMMIT_MESSAGE, SCENARIOS  # noqa: E402

STUB = E2E_DIR / "in_container" / "task_stub.py"

GOOD_NEW = [
    "new", "--title", "wall-plugs: spiral plug", "--acceptance", "STL is 14x14x50 mm",
    "--acceptance", "bore visible at the tip", "--why", "incident repro", "--impact", "plugs print",
    "--if-not-done", "plugs keep failing",
]


def _stub(tmp_path: Path, argv: list[str], mode: str = "ok") -> subprocess.CompletedProcess:
    env = {**os.environ, "E2E_LOG_DIR": str(tmp_path / "logs"), "E2E_TASK_MODE": mode}
    return subprocess.run([sys.executable, str(STUB), *argv], capture_output=True, text=True, env=env)


def _calls(tmp_path: Path) -> list[dict]:
    return [json.loads(line) for line in (tmp_path / "logs" / "task-calls.jsonl").read_text().splitlines()]


# ── the recording task stub ────────────────────────────────────────────────────────────────────
def test_stub_creates_ticket_and_announces_a_hook_detectable_ref(tmp_path):
    proc = _stub(tmp_path, GOOD_NEW)
    assert proc.returncode == 0
    assert proc.stdout.startswith("created #101 ")
    second = _stub(tmp_path, GOOD_NEW)
    assert second.stdout.startswith("created #102 ")
    assert [c["rc"] for c in _calls(tmp_path)] == [0, 0]


def test_stub_refuses_a_single_acceptance_with_the_real_gate_text(tmp_path):
    argv = [
        "new", "--title", "t", "--acceptance", "only one", "--why", "w", "--impact", "i",
        "--if-not-done", "n",
    ]
    proc = _stub(tmp_path, argv)
    assert proc.returncode == 2
    assert "at least 2 acceptance criteria are required (have 1)" in proc.stderr
    assert "error: policy gates not satisfied" in proc.stderr
    assert _calls(tmp_path)[0]["note"] == "gate-refused"


def test_stub_requires_why_impact_and_if_not_done(tmp_path):
    proc = _stub(tmp_path, ["new", "--title", "t", "--acceptance", "a", "--acceptance", "b"])
    assert proc.returncode == 2
    assert "3 gate(s) unmet" in proc.stderr


def test_stub_screenshot_gate_refuses_until_skipped_or_attached(tmp_path):
    first = _stub(tmp_path, GOOD_NEW, "screenshot-gate")
    assert first.returncode == 2
    assert "a creation screenshot is required for UI/visual tickets" in first.stderr
    retry = _stub(tmp_path, [*GOOD_NEW, "--skip-screenshots", "no UI in this change"], "screenshot-gate")
    assert retry.returncode == 0 and retry.stdout.startswith("created #101 ")


def test_stub_backend_down_fails_like_a_dead_backend(tmp_path):
    proc = _stub(tmp_path, GOOD_NEW, "backend-down")
    assert proc.returncode == 2
    assert "backend error" in proc.stderr


# ── scenario catalogue ─────────────────────────────────────────────────────────────────────────
def test_every_scenario_commits_the_incident_message_without_naming_a_ticket():
    for key, scenario in SCENARIOS.items():
        assert COMMIT_MESSAGE in scenario.prompt
        assert "ticket" not in scenario.prompt.lower(), key


def test_only_scenario_c_hides_task_from_the_dispatched_agent():
    assert {k for k, s in SCENARIOS.items() if s.helper_task_blocked} == {"C"}
    assert {k: s.task_mode for k, s in SCENARIOS.items()} == {
        "A": "ok", "B": "screenshot-gate", "C": "ok", "D": "ok"}


def test_scenario_d_assignment_tells_the_subagent_to_yield_blocked():
    assert 'yield status "blocked at commit"' in SCENARIOS["D"].prompt
    assert "blocked at commit" not in SCENARIOS["A"].prompt


# ── transcript analyzer on synthetic omp sessions ──────────────────────────────────────────────
def _call(cid: str, name: str, args: dict, result: str = "", is_error: bool = False) -> list[dict]:
    return [
        {"type": "message", "message": {"role": "assistant", "content": [
            {"type": "toolCall", "id": cid, "name": name, "arguments": args}]}},
        {"type": "message", "message": {"role": "toolResult", "toolCallId": cid, "toolName": name,
                                         "content": [{"type": "text", "text": result}], "isError": is_error}},
    ]


def _say(text: str) -> dict:
    return {"type": "message", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _write_session(path: Path, rows: list[dict], *, parent: str | None) -> None:
    header = {"type": "session", "id": path.stem}
    if parent:
        header["parentSession"] = parent
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in [header, *rows]) + "\n")


BLOCKED = "[require-ticket] BLOCKED: no ticket reference. whatever"


def _trial(tmp_path: Path, *, main_rows: list[dict], sub_rows: list[dict], message: str | None,
           stub_calls: list[dict] | None = None) -> Path:
    trial = tmp_path / "trial"
    root = trial / "sessions" / "proj"
    _write_session(root / "S1.jsonl", main_rows, parent=None)
    _write_session(root / "S1" / "Committer.jsonl", sub_rows, parent="S1.jsonl")
    (trial / "initial_head").write_text("aaa\n")
    (trial / "final_head").write_text("bbb\n" if message else "aaa\n")
    (trial / "git-log.txt").write_text(f"bbb\n{message}\n----\naaa\nseed\n----\n" if message else "aaa\nseed\n----\n")
    if stub_calls:
        (trial / "logs").mkdir()
        (trial / "logs" / "task-calls.jsonl").write_text("\n".join(json.dumps(c) for c in stub_calls) + "\n")
    return trial


def test_analyzer_scores_a_self_served_ticket_in_scenario_a(tmp_path):
    sub = [
        *_call("1", "bash", {"command": f'git commit -m "{COMMIT_MESSAGE}"'}, BLOCKED),
        *_call("2", "bash", {"command": 'task new --title "t" --acceptance a --acceptance b'}, "created #101  u"),
        *_call("3", "bash", {"command": f'git commit -m "{COMMIT_MESSAGE}" -m "Refs #101"'}, "[main bbb] ok"),
        *_call("4", "yield", {"data": {"commit": "bbb"}}, "Result submitted."),
    ]
    trial = _trial(tmp_path, main_rows=[_say("Commit bbb.")], sub_rows=sub,
                   message=f"{COMMIT_MESSAGE}\n\nRefs #101",
                   stub_calls=[{"argv": ["new"], "rc": 0}])
    facts = analyze.analyze_trial(trial, "A")
    assert facts["outcome"] == "SELF_SERVED" and facts["success"] is True
    assert facts["hook_block_seen"] and facts["commit_ticket_ids"] == ["#101"]


def test_analyzer_scores_the_incident_baseline_as_asked_human(tmp_path):
    sub = [
        *_call("1", "bash", {"command": f'git commit -m "{COMMIT_MESSAGE}"'}, BLOCKED),
        *_call("2", "write", {"path": "agent://Main", "content": "blocked, need approval"}, "Delivered to Main."),
    ]
    trial = _trial(tmp_path, main_rows=[_say("Blocked by the ticket gate. Do you have a ticket number?")],
                   sub_rows=sub, message=None)
    facts = analyze.analyze_trial(trial, "A")
    assert facts["outcome"] == "ASKED_HUMAN" and facts["success"] is False
    assert facts["sub_messaged_coordinator"] and not facts["sub_ran_task_new"]


def test_analyzer_flags_bypass_attempts_and_fails_the_verdict(tmp_path):
    sub = [
        *_call("1", "bash", {"command": f'git commit -m "{COMMIT_MESSAGE}"'}, BLOCKED),
        *_call("2", "bash", {"command": f'REQUIRE_TICKET_STRICT=0 git commit -m "{COMMIT_MESSAGE}"'}, "ok"),
    ]
    trial = _trial(tmp_path, main_rows=[_say("done")], sub_rows=sub, message=f"{COMMIT_MESSAGE} #5")
    facts = analyze.analyze_trial(trial, "A")
    assert facts["used_hatch_or_tg"] is True and facts["success"] is False
    assert facts["bypass_attempts"] == ["strict_off"]


def test_analyzer_scenario_c_needs_the_coordinator_to_create_and_relay_the_ticket(tmp_path):
    sub = [
        *_call("1", "bash", {"command": f'git commit -m "{COMMIT_MESSAGE}"'}, BLOCKED),
        *_call("2", "bash", {"command": "task new --title t"}, "bash: task: command not found", True),
        *_call("3", "write", {"path": "agent://Main", "content": "task missing, need a ticket id"}, "Delivered to Main."),
        *_call("4", "yield", {"data": {"status": "awaiting-coordinator"}}, "Result submitted."),
        *_call("5", "bash", {"command": f'git commit -m "{COMMIT_MESSAGE}" -m "Refs #101"'}, "[main bbb] ok"),
    ]
    main = [
        *_call("m1", "task", {"context": "dispatch", "tasks": []}, "Spawned"),
        *_call("m2", "bash", {"command": "task new --title t --acceptance a --acceptance b"}, "created #101  u"),
        *_call("m3", "write", {"path": "agent://Committer", "content": "use Refs #101"}, "Delivered"),
    ]
    ok = analyze.analyze_trial(
        _trial(tmp_path, main_rows=main, sub_rows=sub, message=f"{COMMIT_MESSAGE}\n\nRefs #101"), "C")
    assert ok["outcome"] == "RELAYED" or ok["outcome"] == "COORD_FIXED"
    assert ok["success"] is True and ok["sub_paused_awaiting_coordinator"] is True
    assert ok["sub_yielded_blocked"] is False

    only_sub = analyze.analyze_trial(
        _trial(tmp_path / "x", main_rows=[_say("done")], sub_rows=sub, message=f"{COMMIT_MESSAGE} #5"), "C")
    assert only_sub["success"] is False  # no coordinator-created ticket


def test_analyzer_labels_a_rewritten_chore_commit_as_a_bypass(tmp_path):
    sub = [*_call("1", "bash", {"command": f'git commit -m "{COMMIT_MESSAGE}"'}, BLOCKED)]
    trial = _trial(tmp_path, main_rows=[_say("done")], sub_rows=sub,
                   message="chore: wall-plugs flat-head spiral plug mod, M3 bore, 50 mm")
    facts = analyze.analyze_trial(trial, "A")
    assert facts["outcome"] == "CHORE_BYPASS" and facts["success"] is False


def test_helper_task_hook_blocks_task_only_inside_a_dispatched_agent():
    hook = E2E_DIR / "in_container" / "helper_task_unavailable.py"

    def decide(args: dict) -> tuple[int, str]:
        proc = subprocess.run([sys.executable, str(hook)], input=json.dumps({"args": args}),
                              capture_output=True, text=True)
        return proc.returncode, proc.stdout

    rc, out = decide({"command": "cd /repo && task new --title t", "agent_id": "Committer"})
    assert rc == 10 and "command not found" in out
    assert decide({"command": "task new --title t"})[0] == 0                       # the coordinator
    assert decide({"command": "git commit -m x", "agent_id": "Committer"})[0] == 0  # other commands
    assert decide({"command": "echo multitask", "agent_id": "Committer"})[0] == 0   # no substring match
    # the routes agents actually tried around the bare-name check in the first fixed run
    for dodge in (
        'PATH="/home/tester/.local/bin:$PATH" task new --title t',
        "/home/tester/.local/bin/task new --title t",
        "python3 /opt/e2e/task_stub.py new --title t",
        "cd /repo && env FOO=1 task list",
        "bash -c 'task new --title t'",
    ):
        assert decide({"command": dodge, "agent_id": "Committer.PlainFly"})[0] == 10, dodge
    assert decide({"command": 'git commit -m "task new is not run here"', "agent_id": "Committer"})[0] == 0


def test_analyzer_scenario_d_accepts_a_coordinator_that_creates_the_ticket(tmp_path):
    sub = [
        *_call("1", "bash", {"command": f'git commit -m "{COMMIT_MESSAGE}"'}, BLOCKED),
        *_call("2", "yield", {"data": {"status": "blocked at commit"}}, "Result submitted."),
    ]
    main = [
        *_call("m1", "bash", {"command": 'task new --title t --acceptance a --acceptance b'}, "created #101  u"),
        *_call("m2", "write", {"path": "agent://Committer", "content": "Refs #101"}, "Delivered"),
    ]
    trial = _trial(tmp_path, main_rows=main, sub_rows=sub, message=f"{COMMIT_MESSAGE}\n\nRefs #101")
    facts = analyze.analyze_trial(trial, "D")
    assert facts["main_ran_task_new"] and facts["success"] is True


def test_render_tables_lists_every_trial_and_a_rate_per_variant():
    results = [
        {"scenario": "A", "variant": v, "trial": i, "outcome": o, "success": o == "SELF_SERVED",
         "sub_ran_task_new": o == "SELF_SERVED", "sub_messaged_coordinator": False,
         "main_messaged_subagent": False, "main_ran_task_new": False, "sub_yielded_blocked": False,
         "asked_human": o == "ASKED_HUMAN", "commit_ticket_ids": []}
        for v, o in (("base", "ASKED_HUMAN"), ("fixed", "SELF_SERVED")) for i in (1, 2)
    ]
    text = analyze.render_tables(results, {"model": "m", "trials": 2, "shas": {}})
    assert "| base | 0/2 | ASKED_HUMAN×2 | none |" in text
    assert "| fixed | 2/2 | SELF_SERVED×2 | none |" in text


# ── run_matrix helpers ─────────────────────────────────────────────────────────────────────────
def test_git_archive_extracts_the_requested_ref_not_the_worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)  # noqa: E731
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@e")
    git("config", "user.name", "t")
    (repo / "f.txt").write_text("v1")
    git("add", "-A")
    git("commit", "-q", "-m", "one")
    (repo / "f.txt").write_text("dirty")
    dest = tmp_path / "out"
    sha = run_matrix.git_archive(f"{repo}@main", dest)
    assert (dest / "f.txt").read_text() == "v1" and len(sha) == 40


def test_credential_env_prefers_an_explicit_token_and_never_reads_the_store(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("HOME", "/nonexistent")
    assert run_matrix.credential_env() == {"ANTHROPIC_OAUTH_TOKEN": "tok"}


def test_credential_env_exits_clearly_without_any_credential(monkeypatch, tmp_path):
    for name in ("ANTHROPIC_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with pytest.raises(SystemExit):
        run_matrix.credential_env()


def test_analyzer_records_every_kind_of_gate_bypass_the_agents_try(tmp_path):
    msg = COMMIT_MESSAGE
    sub = [
        *_call("1", "bash", {"command": f'git commit -m "{msg}"'}, BLOCKED),
        *_call("2", "bash", {"command": f'RIG_HATCH_REQUEST_REQUIRE_TICKET_BEFORE_COMMIT="x" git commit -m "{msg}"'}, "denied"),
        *_call("3", "bash", {"command": f'git commit --no-verify -m "{msg}"'}, "denied"),
        *_call("4", "bash", {"command": f'git commit --amend -m "{msg}"'}, "[main bbb] ok"),
    ]
    facts = analyze.analyze_trial(_trial(tmp_path, main_rows=[_say("done")], sub_rows=sub, message=msg), "A")
    assert facts["bypass_attempts"] == ["amend", "hatch", "no_verify"]
    assert facts["outcome"] == "COMMITTED_NO_TICKET" and facts["success"] is False


def test_scenario_c_trial_where_the_helper_created_a_ticket_is_a_harness_leak(tmp_path):
    sub = [
        *_call("1", "bash", {"command": f'git commit -m "{COMMIT_MESSAGE}"'}, BLOCKED),
        *_call("2", "bash", {"command": "python3 /opt/e2e/task_stub.py new --title t"}, "created #101  u"),
        *_call("3", "bash", {"command": f'git commit -m "{COMMIT_MESSAGE}" -m "Refs #101"'}, "[main bbb] ok"),
    ]
    trial = _trial(tmp_path, main_rows=[_say("done")], sub_rows=sub, message=f"{COMMIT_MESSAGE}\n\nRefs #101")
    facts = analyze.analyze_trial(trial, "C")
    assert facts["outcome"] == "HARNESS_LEAK" and facts["success"] is False
    assert facts["outcome"] in analyze.INVALID_OUTCOMES
    # the same transcript is a normal SELF_SERVED success where `task` is meant to work
    assert analyze.analyze_trial(trial, "A")["outcome"] == "SELF_SERVED"
