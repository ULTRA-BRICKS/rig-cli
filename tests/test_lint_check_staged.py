"""`rig lint check --staged` inspects INDEX content; `--fix` refuses staged/partially staged work."""
from __future__ import annotations

import os
import tempfile

import pytest

from riglib import lint_fix_else
from tests.lint_check_helpers import (FakeAnalyzer, cognitive_source, commit_all, fn, git, lines_source, make_repo,
                                      put, require_real_cccc, rules, run_check, use_analyzer, use_config)


@pytest.fixture
def lines_only(monkeypatch):
    return use_config(monkeypatch, complexity_enabled=False)


def test_staged_bad_with_clean_working_tree_fails_while_unstaged_run_passes(tmp_path, capsys, lines_only):
    repo = make_repo(tmp_path)
    put(repo, "big.ts", "ok\n")
    commit_all(repo)
    put(repo, "big.ts", lines_source(2500))
    git(repo, "add", "big.ts")
    put(repo, "big.ts", "ok again\n")  # cleaned working tree, bad content still staged
    code, doc = run_check(capsys, repo, staged=True)
    assert code == 1 and [(f["path"], f["value"]) for f in rules(doc)] == [("big.ts", 2500)]
    assert doc["staged"] is True
    assert run_check(capsys, repo)[0] == 0


def test_staged_good_with_bad_working_tree_passes(tmp_path, capsys, lines_only):
    repo = make_repo(tmp_path)
    put(repo, "big.ts", "ok\n")
    git(repo, "add", "big.ts")
    put(repo, "big.ts", lines_source(2500))  # unstaged bad edit must be invisible to --staged
    code, doc = run_check(capsys, repo, staged=True)
    assert code == 0 and doc["findings"] == []
    assert run_check(capsys, repo)[0] == 1


def test_staged_complexity_reads_index_content_with_real_analyzer(tmp_path, monkeypatch, capsys):
    use_analyzer(monkeypatch, require_real_cccc())
    use_config(monkeypatch)
    repo = make_repo(tmp_path)
    put(repo, "a.ts", cognitive_source(16))
    git(repo, "add", "a.ts")
    put(repo, "a.ts", cognitive_source(1))
    code, doc = run_check(capsys, repo, staged=True)
    assert code == 1 and [f["rule"] for f in doc["findings"]] == ["complexity/cognitive"]
    assert doc["findings"][0]["path"] == "a.ts"
    assert run_check(capsys, repo)[0] == 0


def test_analyzer_gets_snapshot_paths_never_working_tree_paths(tmp_path, monkeypatch, capsys):
    fake = FakeAnalyzer(tmp_path / "bin")
    use_analyzer(monkeypatch, fake.path)
    use_config(monkeypatch)
    repo = make_repo(tmp_path)
    put(repo, "src/a.ts", "x\n")
    git(repo, "add", "-A")
    code, doc = run_check(capsys, repo, staged=True)
    assert code == 0 and doc["checks"]["complexity"]["status"] == "pass"
    (call,) = fake.calls()
    (path,) = call[3:]
    assert path.endswith("/src/a.ts") and not path.startswith(os.path.realpath(repo))


def test_staged_deletions_and_renames(tmp_path, capsys, lines_only):
    repo = make_repo(tmp_path)
    put(repo, "gone.ts", lines_source(2500))
    put(repo, "old_name.ts", lines_source(2500))
    put(repo, "unrelated.ts", lines_source(2500))
    commit_all(repo)
    git(repo, "rm", "-q", "gone.ts")
    git(repo, "mv", "old_name.ts", "new_name.ts")
    code, doc = run_check(capsys, repo, staged=True)
    assert [f["path"] for f in rules(doc)] == ["new_name.ts"]  # not gone/old/unrelated
    assert doc["skipped"] == [] and doc["summary"]["files_considered"] == 1  # deletions leave no trace
    git(repo, "commit", "-q", "-m", "x")
    code, doc = run_check(capsys, repo, staged=True)
    assert code == 0 and doc["checks"]["file_lines"]["status"] == "not-applicable"


def test_only_staged_files_are_considered_and_untracked_are_ignored(tmp_path, capsys, lines_only):
    repo = make_repo(tmp_path)
    put(repo, "committed_big.ts", lines_source(2500))
    commit_all(repo)
    put(repo, "untracked_big.ts", lines_source(2500))
    put(repo, "small.ts", "x\n")
    git(repo, "add", "small.ts")
    code, doc = run_check(capsys, repo, staged=True)
    assert code == 0 and doc["summary"]["files_considered"] == 1


def test_staged_odd_names_symlinks_and_path_restriction(tmp_path, capsys, lines_only):
    repo = make_repo(tmp_path)
    for name in ("-d.txt", "sp ace.txt", "nl\nx.txt", "keep/in.txt", "other/out.txt"):
        put(repo, name, lines_source(1300))
    os.symlink("/etc/passwd", repo / "link.txt")
    git(repo, "add", "-A")
    code, doc = run_check(capsys, repo, staged=True)
    assert sorted(f["path"] for f in rules(doc)) == sorted(["-d.txt", "sp ace.txt", "nl\nx.txt", "keep/in.txt", "other/out.txt"])
    assert ("link.txt", "symlink") in {(s["path"], s["reason"]) for s in doc["skipped"]}
    code, doc = run_check(capsys, repo, "keep", staged=True)
    assert [f["path"] for f in rules(doc)] == ["keep/in.txt"]


def test_snapshot_directory_is_removed_after_the_run(tmp_path, monkeypatch, capsys, lines_only):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    monkeypatch.setattr("tempfile.tempdir", None)
    repo = make_repo(tmp_path)
    put(repo, "a.txt", "x\n")
    git(repo, "add", "-A")
    assert run_check(capsys, repo, staged=True)[0] == 0
    assert list(scratch.iterdir()) == []


def test_staged_initial_commit_without_head_works(tmp_path, capsys, lines_only):
    repo = make_repo(tmp_path)
    put(repo, "a.txt", lines_source(2500))
    git(repo, "add", "-A")
    assert run_check(capsys, repo, staged=True)[0] == 1


def test_fix_with_staged_is_refused_without_writing(tmp_path, monkeypatch, capsys):
    use_config(monkeypatch)
    monkeypatch.setattr(lint_fix_else, "fix_file", lambda *a: pytest.fail("must not write"))
    repo = make_repo(tmp_path)
    put(repo, "a.ts", "x\n")
    git(repo, "add", "-A")
    code, doc = run_check(capsys, repo, staged=True, fix=True)
    assert code == 2 and "--staged" in doc["error"]
    assert (repo / "a.ts").read_text() == "x\n"


def _hot_analyzer(tmp_path, monkeypatch):
    fake = FakeAnalyzer(tmp_path / "bin", functions={"a.ts": [fn("hot", cognitive=99)]})
    use_analyzer(monkeypatch, fake.path)
    use_config(monkeypatch)


def test_fix_refuses_a_partially_staged_file_and_never_restages(tmp_path, monkeypatch, capsys):
    _hot_analyzer(tmp_path, monkeypatch)
    monkeypatch.setattr(lint_fix_else, "fix_file", lambda *a: pytest.fail("must not write"))
    repo = make_repo(tmp_path)
    put(repo, "a.ts", "one\n")
    commit_all(repo)
    put(repo, "a.ts", "two\n")
    git(repo, "add", "a.ts")
    put(repo, "a.ts", "three\n")
    before = git(repo, "diff", "--cached")
    code, doc = run_check(capsys, repo, fix=True)
    assert code == 2 and "partially staged" in doc["error"] and "a.ts" in doc["error"]
    assert (repo / "a.ts").read_text() == "three\n"
    assert git(repo, "diff", "--cached") == before


def test_fix_proceeds_when_a_staged_file_has_no_unstaged_changes_and_does_not_restage(tmp_path, monkeypatch, capsys):
    _hot_analyzer(tmp_path, monkeypatch)
    calls = []

    def fake_fix(root, rel, targets):
        calls.append(rel)
        return lint_fix_else.Skipped(rel, "no-fixable-pattern", "none")

    monkeypatch.setattr(lint_fix_else, "fix_file", fake_fix)
    repo = make_repo(tmp_path)
    put(repo, "a.ts", "one\n")
    git(repo, "add", "a.ts")
    before = git(repo, "diff", "--cached")
    code, doc = run_check(capsys, repo, fix=True)
    assert calls == ["a.ts"] and code == 1
    assert git(repo, "diff", "--cached") == before


def test_exempt_staged_blobs_are_never_read_or_materialized(tmp_path, monkeypatch, capsys, lines_only):
    from riglib import lint_check_scope

    requested: list[list[str]] = []
    real = lint_check_scope._read_blobs
    monkeypatch.setattr(lint_check_scope, "_read_blobs", lambda root, shas: requested.append(list(shas)) or real(root, shas))
    repo = make_repo(tmp_path)
    put(repo, "huge.json", lines_source(5000))
    put(repo, "a.txt", "x\n")
    git(repo, "add", "-A")
    blob_a = git(repo, "rev-parse", ":a.txt").strip()
    code, doc = run_check(capsys, repo, staged=True)
    assert code == 0 and requested == [[blob_a]]
    assert ("huge.json", "file_lines", "exempt-extension") in {(s["path"], s["check"], s["reason"]) for s in doc["skipped"]}


def test_read_blobs_stays_in_sync_after_a_non_blob_object_and_a_missing_one(monkeypatch):
    from riglib import lint_check_scope

    stream = b"a" * 40 + b" tree 5\nxxxxx\n" + b"b" * 40 + b" missing\n" + b"c" * 40 + b" blob 2\nhi\n"
    monkeypatch.setattr(lint_check_scope, "_git", lambda root, args, stdin=None, max_stdout_bytes=None: stream)
    got = lint_check_scope._read_blobs("/unused", ["a" * 40, "b" * 40, "c" * 40])
    assert got == {"c" * 40: b"hi"}


def test_merge_in_progress_checks_every_file_differing_from_head(tmp_path, capsys, lines_only):
    repo = make_repo(tmp_path)
    put(repo, "base.txt", "x\n")
    commit_all(repo)
    git(repo, "checkout", "-q", "-b", "side")
    put(repo, "theirs.txt", lines_source(2500))
    commit_all(repo)
    git(repo, "checkout", "-q", "-")
    git(repo, "merge", "--no-commit", "--no-ff", "side")
    code, doc = run_check(capsys, repo, staged=True)
    assert code == 1 and [f["path"] for f in rules(doc)] == ["theirs.txt"]


def test_a_symlinked_tmpdir_cannot_turn_every_staged_file_unexpected(tmp_path, monkeypatch, capsys):
    # macOS: TMPDIR is /var/... but the canonical path is /private/var/...; an analyzer that
    # reports canonical paths must still match the files it was asked to read.
    real_tmp = tmp_path / "real-tmp"
    real_tmp.mkdir()
    link = tmp_path / "link-tmp"
    link.symlink_to(real_tmp, target_is_directory=True)
    monkeypatch.setattr(tempfile, "tempdir", str(link))
    fake = FakeAnalyzer(tmp_path / "bin", canonical=True)
    use_analyzer(monkeypatch, fake.path)
    use_config(monkeypatch)
    repo = make_repo(tmp_path)
    put(repo, "a.ts", "x\n")
    git(repo, "add", "a.ts")
    code, doc = run_check(capsys, repo, staged=True)
    assert code == 0 and doc["incomplete"] == [] and doc["checks"]["complexity"]["status"] == "pass"
    asked = [arg for call in fake.calls() for arg in call if arg.endswith("a.ts")]
    assert asked and all(os.path.realpath(a) == a for a in asked)


@pytest.mark.parametrize("rel", ["../evil.ts", "a/../../evil.ts", "/abs/evil.ts", "a//b.ts", "./a.ts"])
def test_a_crafted_index_path_never_writes_outside_the_snapshot(tmp_path, monkeypatch, capsys, rel):
    from riglib import lint_check_scope

    fake = FakeAnalyzer(tmp_path / "bin")
    use_analyzer(monkeypatch, fake.path)
    use_config(monkeypatch)
    snap_parent = tmp_path / "snap-parent"  # the snapshot is created under $TMPDIR: pin it so an escape lands inside the sandbox
    nested = snap_parent / "nested"
    nested.mkdir(parents=True)
    monkeypatch.setenv("TMPDIR", str(nested))
    monkeypatch.setattr("tempfile.tempdir", None)
    repo = make_repo(tmp_path)
    put(repo, "ok.ts", "x\n")
    git(repo, "add", "ok.ts")
    sha = git(repo, "rev-parse", ":ok.ts").strip()
    real = lint_check_scope._staged_entries
    monkeypatch.setattr(lint_check_scope, "_staged_entries",
                        lambda root: [*real(root), ("A", "100644", sha, rel)])
    code, doc = run_check(capsys, repo, staged=True)
    assert code == 3 and [(i["path"], i["check"]) for i in doc["incomplete"]][0][0] == rel
    assert not (tmp_path / "evil.ts").exists() and not os.path.exists("/abs/evil.ts")
    assert [p.name for p in snap_parent.iterdir()] == ["nested"] and list(nested.iterdir()) == []  # nothing escaped, nothing left


def index_case_pair(repo):
    """Stage Foo.ts and foo.ts straight into the index (independent of the working-tree filesystem)."""
    for name, body in (("Foo.ts", b"x\n"), ("foo.ts", b"y\n")):
        sha = git(repo, "hash-object", "-w", "--stdin", input=body).strip()
        git(repo, "update-index", "--add", "--cacheinfo", f"100644,{sha},{name}")


def test_staged_paths_differing_only_by_case_are_incomplete_on_a_case_insensitive_snapshot(
        tmp_path, monkeypatch, capsys):
    from riglib import lint_check_scope

    use_config(monkeypatch, complexity_enabled=False)
    monkeypatch.setattr(lint_check_scope, "_case_insensitive", lambda directory: True)
    repo = make_repo(tmp_path)
    index_case_pair(repo)
    put(repo, "other.ts", "z\n")
    git(repo, "add", "other.ts")
    code, doc = run_check(capsys, repo, staged=True)
    assert code == 3
    collided = {i["path"] for i in doc["incomplete"] if "differs only by case" in i["detail"]}
    assert collided == {"Foo.ts", "foo.ts"}


def test_case_variant_paths_are_checked_normally_on_a_case_sensitive_snapshot(tmp_path, monkeypatch, capsys):
    from riglib import lint_check_scope

    use_config(monkeypatch, complexity_enabled=False)
    monkeypatch.setattr(lint_check_scope, "_case_insensitive", lambda directory: False)
    repo = make_repo(tmp_path)
    index_case_pair(repo)
    code, doc = run_check(capsys, repo, staged=True)
    assert code == 0 and doc["incomplete"] == [] and doc["summary"]["files_considered"] == 2


def test_the_case_probe_answers_for_the_real_directory_and_cleans_up(tmp_path):
    from riglib import lint_check_scope

    probe_dir = tmp_path / "probe"  # a private dir: autouse fixtures also put files in tmp_path itself
    probe_dir.mkdir()
    (probe_dir / "Zz").write_text("x")
    expected = (probe_dir / "zz").exists()  # what this filesystem really does
    assert lint_check_scope._case_insensitive(str(probe_dir)) is expected
    assert sorted(p.name for p in probe_dir.iterdir()) == ["Zz"]


def test_explicit_path_that_selects_nothing_is_reported_not_silently_passed(tmp_path, capsys, lines_only):
    repo = make_repo(tmp_path)
    put(repo, ".gitignore", "gen.ts\n")
    put(repo, "gen.ts", lines_source(3000))
    put(repo, "ok.ts", "ok\n")
    commit_all(repo)
    code, doc = run_check(capsys, repo, "gen.ts")
    assert code == 0 and doc["summary"]["files_considered"] == 0
    assert {"path": "gen.ts", "check": "all", "reason": "not-in-scope"} in doc["skipped"]
    code, doc = run_check(capsys, repo, "ok.ts")
    assert code == 0 and doc["summary"]["files_considered"] == 1 and doc["skipped"] == []
