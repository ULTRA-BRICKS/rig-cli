"""`lint_run` executor: aggregation, states, exit priority, timeouts, bounded capture, safe --fix.

Uses a scripted `CommandRunner` for classification/aggregation and the real `subprocess_runner`
(on tiny python/sh commands) for timeout, process-group kill, bounding and argv-only behavior.
"""
from __future__ import annotations

import itertools
import json
import os
import sys
import time

import pytest

from riglib import lint_run as lr
from riglib.lint_discovery_model import DiscoveryProblem
from riglib.lint_providers import Unavailable
from tests.lint_check_helpers import commit_all, git, make_repo, put
from tests.lint_run_helpers import LOADER_ENV, FakeRunner, ensure_repo, outcome, planned

ENV = {"PATH": os.defpath, **LOADER_ENV}


def run(checks, root, runner, *, fix=False, environ=None, timeout=300, git_repo=True):
    """`run_checks` guards every check with a git snapshot, so the root must be a worktree."""
    if git_repo:
        ensure_repo(root)
    return lr.run_checks(checks, root=root, default_timeout_s=timeout, fix=fix,
                         environ=ENV if environ is None else environ, runner=runner)


# ── aggregation ──────────────────────────────────────────────────────────────────

def test_first_failure_does_not_stop_later_checks_and_every_row_is_complete(tmp_path):
    checks = [planned(id="a@.", argv=("a",)), planned(id="b@.", argv=("b",)), planned(id="c@.", argv=("c",))]
    runner = FakeRunner({("a",): outcome(1, "bad"), ("b",): outcome(0), ("c",): outcome(1, "worse")})
    results = run(checks, tmp_path, runner)
    assert runner.argvs == [("a",), ("b",), ("c",)]
    assert [r.state for r in results] == ["failed", "passed", "failed"]
    for r in results:
        assert r.reason and r.detail and r.exit_code is not None and r.duration_ms is not None
    assert lr.exit_code_for(results, bootstrap_failed=False, problems=()) == lr.EXIT_FINDINGS


@pytest.mark.parametrize("state,reason", [
    ("missing-tool", "tool-not-installed"), ("not-applicable", "no-config-file"),
    ("unsupported-platform", "platform-unsupported"),
])
def test_unavailable_checks_are_never_passed_and_never_executed(tmp_path, state, reason):
    check = planned(argv=None, unavailable=Unavailable(state, reason, "do the thing"))
    runner = FakeRunner()
    (result,) = run([check], tmp_path, runner)
    assert runner.specs == []
    assert (result.state, result.reason, result.detail) == (state, reason, "do the thing")
    assert result.exit_code is None and result.duration_ms is None


def test_argv_none_without_reason_fails_closed(tmp_path):
    (result,) = run([planned(argv=None, unavailable=None)], tmp_path, FakeRunner())
    assert result.state == "failed" and result.reason == "crash"


@pytest.mark.parametrize("out,state,reason", [
    (outcome(0), "passed", "ok"),
    (outcome(1), "failed", "findings"),
    (outcome(2), "failed", "findings"),
    (outcome(101), "failed", "findings"),
    (outcome(127), "missing-tool", "tool-not-installed"),
    (outcome(126), "failed", "crash"),
    (outcome(-9), "failed", "crash"),
    (outcome(None, timed_out=True), "failed", "timeout"),
    (outcome(None, spawn_error="FileNotFoundError: no such file"), "missing-tool", "tool-not-installed"),
    (outcome(None, spawn_error="PermissionError: denied"), "failed", "crash"),
])
def test_outcome_classification(tmp_path, out, state, reason):
    (result,) = run([planned(argv=("t",))], tmp_path, FakeRunner(default=out))
    assert (result.state, result.reason) == (state, reason)


def test_gofmt_style_stdout_empty_rule(tmp_path):
    check = planned(provider="gofmt", argv=("gofmt", "-l"), result_rule="stdout-empty")
    (clean,) = run([check], tmp_path, FakeRunner(default=outcome(0, "")))
    (dirty,) = run([check], tmp_path, FakeRunner(default=outcome(0, "a.go\nb.go\n")))
    assert clean.state == "passed"
    assert (dirty.state, dirty.reason, dict(dirty.counts)) == ("failed", "findings", {"files": 2})


def test_plain_check_never_invents_counts(tmp_path):
    (result,) = run([planned()], tmp_path, FakeRunner(default=outcome(1, "3 errors")))
    assert result.counts is None


def test_timeout_default_provider_override_and_effective_value_recorded(tmp_path):
    runner = FakeRunner()
    results = run([planned(id="a@.", argv=("a",)), planned(id="b@.", argv=("b",), timeout_s=9)],
                  tmp_path, runner, timeout=42)
    assert [s.timeout_s for s in runner.specs] == [42, 9]
    assert [r.check.timeout_s for r in results] == [42, 9]


# ── exit codes ───────────────────────────────────────────────────────────────────

def _row(state, reason="ok", provider="oxlint"):
    check = planned(provider=provider, argv=("t",) if state == "passed" else None,
                    unavailable=None if state in ("passed", "failed") else Unavailable(state, reason, "d"))
    return lr.CheckResult(check, state, reason, "d", None, None, None, "", "", False, None, None)


_PROBLEM = DiscoveryProblem("package.json", "malformed-manifest", "bad json")
# exit 2 is Rig's own usage/config error (the nested `rig lint check` rejecting its configuration); a linter
# rejecting the PROJECT's configuration is incomplete analysis (3), so the two never share an exit code
CASES = {
    "pass": _row("passed"), "na": _row("not-applicable", "filtered"),
    "findings": _row("failed", "findings"), "timeout": _row("failed", "timeout"),
    "incomplete": _row("failed", "incomplete"), "config": _row("failed", "config-error", provider="rig-check"),
    "toolconfig": _row("failed", "config-error"),
    "missing": _row("missing-tool", "tool-not-installed"), "platform": _row("unsupported-platform", "platform-unsupported"),
}
EXPECTED = {"pass": 0, "na": 0, "findings": 1, "timeout": 3, "incomplete": 3, "config": 2, "toolconfig": 3,
            "missing": 127, "platform": 127}


@pytest.mark.parametrize("name", sorted(CASES))
def test_each_state_maps_to_its_exit_code(name):
    assert lr.exit_code_for([CASES[name]], bootstrap_failed=False, problems=()) == EXPECTED[name]


def test_priority_is_2_over_127_over_3_over_1_over_0_for_every_pair():
    rank = {2: 4, 127: 3, 3: 2, 1: 1, 0: 0}
    for a, b in itertools.combinations(sorted(CASES), 2):
        got = lr.exit_code_for([CASES[a], CASES[b]], bootstrap_failed=False, problems=())
        want = max(EXPECTED[a], EXPECTED[b], key=rank.__getitem__)
        assert got == want, (a, b)


def test_bootstrap_failure_and_malformed_manifest_are_incomplete_not_findings():
    assert lr.exit_code_for([_row("passed")], bootstrap_failed=True, problems=()) == 3
    assert lr.exit_code_for([_row("passed")], bootstrap_failed=False, problems=[_PROBLEM]) == 3
    escape = DiscoveryProblem("x/package.json", "symlink-escape", "outside")
    assert lr.exit_code_for([_row("passed")], bootstrap_failed=False, problems=[escape]) == 0
    assert lr.exit_code_for([_row("missing-tool")], bootstrap_failed=True, problems=[_PROBLEM]) == 127


def test_unknown_failure_reason_fails_closed_as_incomplete():
    assert lr.exit_code_for([_row("failed", "something-new")], bootstrap_failed=False, problems=()) == 3


def test_planned_rows_never_change_the_exit_code():
    assert lr.exit_code_for([_row("passed"), CASES["na"]], bootstrap_failed=False, problems=()) == 0


# ── rig-check provider ───────────────────────────────────────────────────────────

REPORT = {"schema": "rig-lint-check/v1", "ok": True, "summary": {"errors": 0, "warnings": 2}}


@pytest.mark.parametrize("code,stdout,state,reason", [
    (0, json.dumps(REPORT), "passed", "ok"),
    (1, json.dumps(REPORT), "failed", "findings"),
    (3, json.dumps(REPORT), "failed", "incomplete"),
    (127, json.dumps(REPORT), "missing-tool", "analyzer-unavailable"),
    (2, json.dumps(REPORT), "failed", "config-error"),
    (0, "not json", "failed", "malformed-output"),
    (0, json.dumps({"schema": "other/v1"}), "failed", "malformed-output"),
    (9, json.dumps(REPORT), "failed", "crash"),
])
def test_rig_check_exit_mapping_and_embedded_report(tmp_path, code, stdout, state, reason):
    check = planned(id="rig-check@.", provider="rig-check", category="limits", argv=("rc",))
    (result,) = run([check], tmp_path, FakeRunner(default=outcome(code, stdout)))
    assert (result.state, result.reason) == (state, reason)
    if stdout.startswith("{") and "rig-lint-check" in stdout:
        assert result.report == REPORT
    if (state, reason) == ("passed", "ok"):
        assert dict(result.counts) == {"errors": 0, "warnings": 2}


def test_rig_check_truncated_output_is_output_limit(tmp_path):
    check = planned(id="rig-check@.", provider="rig-check", category="limits", argv=("rc",))
    (result,) = run([check], tmp_path, FakeRunner(default=outcome(0, '{"schema":', truncated=True)))
    assert (result.state, result.reason) == ("failed", "output-limit")
    assert lr.exit_code_for([result], bootstrap_failed=False, problems=()) == 3


# ── environment, recursion marker ────────────────────────────────────────────────

@pytest.mark.parametrize("before,after", [(None, "1"), ("0", "1"), ("1", "2"), ("3", "4"), ("garbage", "2")])
def test_child_env_increments_depth_and_disables_color(tmp_path, before, after):
    environ = {"PATH": "/x", "FORCE_COLOR": "1", **({"RIG_LINT_DEPTH": before} if before is not None else {})}
    runner = FakeRunner()
    run([planned()], tmp_path, runner, environ=environ)
    env = runner.specs[0].env
    assert env["RIG_LINT_DEPTH"] == after and env["NO_COLOR"] == "1" and "FORCE_COLOR" not in env
    assert env["PATH"] == "/x"
    assert environ.get("RIG_LINT_DEPTH") == before  # caller's mapping is not mutated


# ── output bounding ──────────────────────────────────────────────────────────────

def test_result_output_is_excerpted_to_32k_and_flagged(tmp_path):
    big = "head\n" + "x" * 200_000 + "\ntail-marker\n"
    (result,) = run([planned()], tmp_path, FakeRunner(default=outcome(1, big, big)))
    assert len(result.stdout) < 40_000 and result.stdout.startswith("head") and result.stdout.endswith("tail-marker\n")
    assert result.truncated is True


def test_small_output_is_kept_verbatim(tmp_path):
    (result,) = run([planned()], tmp_path, FakeRunner(default=outcome(1, "a\n", "b\n")))
    assert (result.stdout, result.stderr, result.truncated) == ("a\n", "b\n", False)


# ── real subprocess runner ───────────────────────────────────────────────────────

def spec(tmp_path, argv, *, timeout=20, env=None, cap=1000):
    return lr.CommandSpec(tuple(argv), tmp_path, timeout, env if env is not None else ENV, cap)


def test_real_runner_captures_exit_streams_and_duration(tmp_path):
    out = lr.subprocess_runner(spec(tmp_path, [sys.executable, "-c",
                                              "import sys; print('o'); print('e', file=sys.stderr); sys.exit(3)"]))
    assert (out.exit_code, out.stdout.strip(), out.stderr.strip()) == (3, "o", "e")
    assert out.duration_ms >= 0 and not out.timed_out and not out.truncated and out.spawn_error is None


def test_real_runner_bounds_capture_and_keeps_draining(tmp_path):
    out = lr.subprocess_runner(spec(tmp_path, [sys.executable, "-c",
                                              "import sys; sys.stdout.write('a'*3_000_000 + 'END')"], cap=1000))
    assert out.exit_code == 0 and out.truncated
    assert len(out.stdout) < 1300 and out.stdout.endswith("END") and out.stdout.startswith("aaa")


def test_real_runner_is_argv_only_no_shell(tmp_path):
    payload = "x; touch pwned; echo $HOME `id`"
    out = lr.subprocess_runner(spec(tmp_path, [sys.executable, "-c", "import sys; print(sys.argv[1])", payload]))
    assert out.stdout.strip() == payload and not (tmp_path / "pwned").exists()


def test_real_runner_uses_only_the_given_env_and_closed_stdin(tmp_path):
    code = "import os,sys; print(os.environ.get('SECRET_FROM_PARENT','-')); print(repr(sys.stdin.read()))"
    os.environ["SECRET_FROM_PARENT"] = "leak"
    try:
        out = lr.subprocess_runner(spec(tmp_path, [sys.executable, "-c", code], env=ENV))
    finally:
        del os.environ["SECRET_FROM_PARENT"]
    assert out.stdout.split() == ["-", "''"]


def test_real_runner_missing_binary_and_missing_cwd_are_spawn_errors(tmp_path):
    gone = lr.subprocess_runner(spec(tmp_path, ["definitely-not-a-binary-xyz"]))
    assert gone.exit_code is None and gone.spawn_error.startswith("FileNotFoundError")
    nodir = lr.subprocess_runner(lr.CommandSpec(("true",), tmp_path / "absent", 5, {"PATH": os.defpath}, 100))
    assert nodir.spawn_error and not nodir.spawn_error.startswith("FileNotFoundError")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_timeout_kills_the_whole_process_group_and_reports_incomplete(tmp_path):
    pidfile = tmp_path / "child.pid"  # outside the worktree: writing it must not read as a repo write
    repo = make_repo(tmp_path)
    script = f"sleep 120 & echo $! > {pidfile}; wait"
    check = planned(argv=("sh", "-c", script), timeout_s=1)
    started = time.monotonic()
    (result,) = lr.run_checks([check], root=repo, default_timeout_s=300, fix=False,
                              environ=ENV, runner=lr.subprocess_runner)
    assert time.monotonic() - started < 15
    assert (result.state, result.reason, result.exit_code) == ("failed", "timeout", None)
    assert "1s" in result.detail
    assert lr.exit_code_for([result], bootstrap_failed=False, problems=()) == 3
    child = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while _alive(child) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(child), "grandchild survived the timeout"


# ── --fix ────────────────────────────────────────────────────────────────────────

def _fix_repo(tmp_path):
    repo = make_repo(tmp_path)
    put(repo, "a.txt", "v1\n")
    commit_all(repo)
    return repo


def _writer(repo, rel, text):
    def act(spec):
        (repo / rel).write_text(text, encoding="utf-8")
        return outcome(1, "fixer says unfixable remain")  # fixers commonly exit 1; verdict comes from the re-check
    return act


def test_fix_runs_fix_argv_then_rechecks_and_reports_post_fix_verdict(tmp_path):
    repo = _fix_repo(tmp_path)
    check = planned(argv=("chk",), fix_argv=("chk", "--fix"))
    runner = FakeRunner({("chk", "--fix"): _writer(repo, "a.txt", "v2\n")}, default=outcome(0))
    (result,) = run([check], repo, runner, fix=True)
    assert runner.argvs == [("chk", "--fix"), ("chk",)]
    assert (result.state, result.exit_code) == ("passed", 0)
    assert result.fix == {"requested": True, "mode": "safe", "changed_files": ["a.txt"], "needs_restage": []}


def test_fix_post_verdict_can_still_fail(tmp_path):
    repo = _fix_repo(tmp_path)
    check = planned(argv=("chk",), fix_argv=("chk", "--fix"))
    runner = FakeRunner({("chk",): outcome(1, "remaining")})
    (result,) = run([check], repo, runner, fix=True)
    assert (result.state, result.reason, result.fix["changed_files"]) == ("failed", "findings", [])


def test_fix_lists_needs_restage_but_never_restages(tmp_path):
    repo = _fix_repo(tmp_path)
    (repo / "a.txt").write_text("v1.5\n", encoding="utf-8")
    git(repo, "add", "a.txt")
    check = planned(argv=("chk",), fix_argv=("chk", "--fix"))
    runner = FakeRunner({("chk", "--fix"): _writer(repo, "a.txt", "v3\n")})
    (result,) = run([check], repo, runner, fix=True)
    assert result.fix["changed_files"] == ["a.txt"] and result.fix["needs_restage"] == ["a.txt"]
    assert git(repo, "show", ":a.txt") == "v1.5\n"


def test_fix_detects_a_second_change_to_an_already_dirty_file(tmp_path):
    repo = _fix_repo(tmp_path)
    (repo / "a.txt").write_text("dirty\n", encoding="utf-8")
    check = planned(argv=("chk",), fix_argv=("chk", "--fix"))
    runner = FakeRunner({("chk", "--fix"): _writer(repo, "a.txt", "dirty+fixed\n")})
    (result,) = run([check], repo, runner, fix=True)
    assert result.fix["changed_files"] == ["a.txt"]


def test_fix_without_a_safe_fixer_runs_in_check_mode_and_says_so(tmp_path):
    repo = _fix_repo(tmp_path)
    runner = FakeRunner()
    (result,) = run([planned(argv=("chk",), fix_argv=None)], repo, runner, fix=True)
    assert runner.argvs == [("chk",)]
    assert result.fix == {"requested": True, "mode": "unsupported", "changed_files": [], "needs_restage": []}


@pytest.mark.parametrize("token", ["--unsafe-fixes", "--fix-dangerously", "--fix-suggestions", "--unsafe"])
def test_unsafe_fix_modes_are_never_executed(tmp_path, token):
    repo = _fix_repo(tmp_path)
    runner = FakeRunner()
    check = planned(argv=("chk",), fix_argv=("chk", "--fix", token))
    (result,) = run([check], repo, runner, fix=True)
    assert runner.argvs == [("chk",)] and result.fix["mode"] == "unsupported"


def test_fix_timeout_is_reported_without_a_recheck(tmp_path):
    repo = _fix_repo(tmp_path)
    runner = FakeRunner({("chk", "--fix"): outcome(None, timed_out=True)})
    (result,) = run([planned(argv=("chk",), fix_argv=("chk", "--fix"))], repo, runner, fix=True)
    assert (result.state, result.reason) == ("failed", "timeout") and runner.argvs == [("chk", "--fix")]


def test_check_outside_a_git_repo_fails_closed_before_running(tmp_path):
    """The read-only guard cannot prove anything without git, so the command is not started."""
    runner = FakeRunner()
    (result,) = run([planned(argv=("chk",))], tmp_path, runner, git_repo=False)
    assert (result.state, result.reason) == ("failed", "crash") and runner.specs == []


def test_fix_outside_a_git_repo_fails_closed(tmp_path):
    runner = FakeRunner()
    (result,) = run([planned(argv=("chk",), fix_argv=("chk", "--fix"))], tmp_path, runner, fix=True,
                    git_repo=False)
    assert (result.state, result.reason) == ("failed", "crash") and runner.specs == []


def test_no_fix_requested_means_fix_is_null(tmp_path):
    (result,) = run([planned(fix_argv=("t", "--fix"))], tmp_path, FakeRunner())
    assert result.fix is None


# ── rendering ────────────────────────────────────────────────────────────────────

def test_as_dict_has_the_documented_row_keys(tmp_path):
    (result,) = run([planned()], tmp_path, FakeRunner(default=outcome(1, "o", "e")))
    row = result.as_dict()
    assert set(row) == {"id", "provider", "category", "scope", "tool", "state", "reason", "detail", "argv", "cwd",
                        "exit_code", "duration_ms", "timeout_s", "resolution", "counts", "output", "fix", "report"}
    assert row["output"] == {"stdout": "o", "stderr": "e", "truncated": False}
    json.dumps(row)


def test_unverified_files_rank_with_missing_tools_above_incomplete_and_findings():
    for rows in ([CASES["failed-findings"]] if "failed-findings" in CASES else [_row("failed")],):
        assert lr.exit_code_for(rows, bootstrap_failed=False, problems=(), uncovered_files=1) == lr.EXIT_MISSING
    assert lr.exit_code_for([_row("passed")], bootstrap_failed=True, problems=(), uncovered_files=1) == lr.EXIT_MISSING


# ── child environment: rustup must never install a toolchain mid-check ───────────

@pytest.mark.parametrize("inherited", [None, "1", "0", "yes"])
def test_child_env_forbids_rustup_toolchain_auto_install(inherited):
    environ = {"PATH": "/x", **({"RUSTUP_AUTO_INSTALL": inherited} if inherited is not None else {})}
    assert lr.child_env(environ)["RUSTUP_AUTO_INSTALL"] == "0", "forced: the user's value cannot enable it"
    assert environ.get("RUSTUP_AUTO_INSTALL") == inherited, "the caller's mapping is not mutated"


def test_every_check_runs_with_rustup_auto_install_off(tmp_path):
    runner = FakeRunner()
    run([planned(id="cargo-clippy@.", provider="cargo-clippy", argv=("cargo", "clippy"))], tmp_path, runner)
    assert runner.specs[0].env["RUSTUP_AUTO_INSTALL"] == "0"
