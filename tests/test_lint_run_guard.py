"""The read-only guard: a non-fix check that changes the worktree is `wrote-files` (exit 3).

Static classification cannot recognise every writer (`oxfmt .` in a `format:check` script, a bare
`ruff format`, a Makefile recipe, a wrapper script), so `run_checks` compares `git status` + content
digests before and after every check. These tests use a fake runner that writes (or not) and a real
subprocess writer; nothing touches the network or a real tool.
"""
from __future__ import annotations

import os
import sys

import pytest

from riglib import lint_run as lr
from tests.lint_check_helpers import commit_all, git, make_repo, put
from tests.lint_run_helpers import LOADER_ENV, FakeRunner, outcome, planned

ENV = {"PATH": os.defpath, **LOADER_ENV}


def run(checks, repo, runner, *, fix=False):
    return lr.run_checks(checks, root=repo, default_timeout_s=60, fix=fix, environ=ENV, runner=runner)


@pytest.fixture
def repo(tmp_path):
    repo = make_repo(tmp_path)
    put(repo, "a.txt", "v1\n")
    commit_all(repo)
    return repo


def writer(repo, rel, text, code=0):
    def act(spec):
        (repo / rel).write_text(text, encoding="utf-8")
        return outcome(code)
    return act


@pytest.mark.parametrize("rel,text", [
    ("new.txt", "created\n"),          # untracked file appears
    ("a.txt", "rewritten\n"),          # clean tracked file changes
])
def test_a_check_that_writes_is_wrote_files_even_when_its_exit_code_is_zero(repo, rel, text):
    runner = FakeRunner({("fmt", "."): writer(repo, rel, text, code=0)})
    (result,) = run([planned(id="script:package.json:format:check@.", argv=("fmt", "."))], repo, runner)
    assert (result.state, result.reason) == ("failed", "wrote-files")
    assert rel in result.detail and "read-only" in result.detail
    assert lr.exit_code_for([result], bootstrap_failed=False, problems=()) == lr.EXIT_INCOMPLETE


def test_a_writing_check_with_findings_is_still_wrote_files_not_findings(repo):
    runner = FakeRunner({("fmt",): writer(repo, "a.txt", "x\n", code=1)})
    (result,) = run([planned(argv=("fmt",))], repo, runner)
    assert result.reason == "wrote-files"


def test_rewriting_an_already_dirty_file_is_detected(repo):
    (repo / "a.txt").write_text("dirty\n", encoding="utf-8")
    runner = FakeRunner({("fmt",): writer(repo, "a.txt", "dirty+formatted\n")})
    (result,) = run([planned(argv=("fmt",))], repo, runner)
    assert result.reason == "wrote-files" and "a.txt" in result.detail


def test_a_dirty_worktree_that_the_check_leaves_alone_is_fine(repo):
    (repo / "a.txt").write_text("dirty\n", encoding="utf-8")
    put(repo, "untracked.txt", "u\n")
    (result,) = run([planned(argv=("chk",))], repo, FakeRunner())
    assert (result.state, result.reason) == ("passed", "ok")


def test_a_failed_check_that_does_not_write_keeps_its_findings_verdict(repo):
    (result,) = run([planned(argv=("chk",))], repo, FakeRunner(default=outcome(1, "bad")))
    assert (result.state, result.reason) == ("failed", "findings")


def test_only_the_writing_check_is_blamed_and_later_checks_still_run(repo):
    runner = FakeRunner({("w",): writer(repo, "a.txt", "x\n")})
    results = run([planned(id="ok@.", argv=("o",)), planned(id="w@.", argv=("w",)),
                   planned(id="late@.", argv=("l",))], repo, runner)
    assert [(r.check.id, r.reason) for r in results] == [("ok@.", "ok"), ("w@.", "wrote-files"), ("late@.", "ok")]


def test_the_guard_never_reverts_or_restages(repo):
    runner = FakeRunner({("w",): writer(repo, "a.txt", "changed\n")})
    run([planned(argv=("w",))], repo, runner)
    assert (repo / "a.txt").read_text() == "changed\n"
    assert git(repo, "diff", "--cached", "--name-only") == ""


def test_a_real_subprocess_writer_is_caught(repo):
    check = planned(argv=(sys.executable, "-c", "open('made.txt', 'w').write('x')"))
    (result,) = run([check], repo, lr.subprocess_runner)
    assert (result.state, result.reason) == ("failed", "wrote-files") and "made.txt" in result.detail


def test_many_written_files_are_summarised_not_dumped(repo):
    def act(spec):
        for i in range(12):
            (repo / f"f{i}.txt").write_text("x", encoding="utf-8")
        return outcome(0)
    (result,) = run([planned(argv=("w",))], repo, FakeRunner({("w",): act}))
    assert "12 file(s)" in result.detail and "..." in result.detail and "f9.txt" not in result.detail


def test_fix_mode_allows_the_fixer_to_write_but_guards_the_recheck(repo):
    fix_check = planned(argv=("chk",), fix_argv=("chk", "--fix"))
    ok = FakeRunner({("chk", "--fix"): writer(repo, "a.txt", "fixed\n", code=1)}, default=outcome(0))
    (fixed,) = run([fix_check], repo, ok, fix=True)
    assert (fixed.state, fixed.fix["changed_files"]) == ("passed", ["a.txt"])

    def recheck_writes(spec):
        (repo / "a.txt").write_text("again\n", encoding="utf-8")
        return outcome(0)
    bad = FakeRunner({("chk", "--fix"): outcome(0), ("chk",): recheck_writes})
    (still,) = run([fix_check], repo, bad, fix=True)
    assert still.reason == "wrote-files"


def test_unavailable_and_unrunnable_checks_never_touch_git(tmp_path):
    from riglib.lint_providers import Unavailable
    check = planned(argv=None, unavailable=Unavailable("missing-tool", "tool-not-installed", "install it"))
    (result,) = lr.run_checks([check], root=tmp_path / "not-a-repo", default_timeout_s=5, fix=False,
                              environ=ENV, runner=FakeRunner())
    assert result.state == "missing-tool"
