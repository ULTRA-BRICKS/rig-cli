"""Regression coverage for the release's manual-use and bounded-read contracts."""
from __future__ import annotations

import os

import pytest

from riglib import lint_check_scope as scope
from riglib.lint_checks_ci import is_immutable_source
from test_lint_checks_plan import _lint_actions, _plan
from tests.lint_check_helpers import git, make_repo, put, run_check, use_config


@pytest.mark.usefixtures("pinned_rig")
def test_manual_lint_installs_analyzer_with_dispatcher_and_ci_disabled(fake_agent_tools, tmp_path):
    plan = _plan(fake_agent_tools, tmp_path, {
        "ci": {"enabled": False},
        "git_hooks": {"dispatcher": {"enabled": False}},
    })
    assert set(_lint_actions(plan)) == {"install_cccc"}


def test_parent_swapped_for_symlink_after_enumeration_is_not_read(tmp_path):
    repo = make_repo(tmp_path)
    put(repo, "nested/a.txt", "inside\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.txt").write_text("outside\n" * 50)
    selected = scope.worktree_scope(str(repo), [], lambda _: True)
    candidate = next(f for f in selected.files if f.rel == "nested/a.txt")
    os.rename(repo / "nested", repo / "original")
    os.symlink(outside, repo / "nested", target_is_directory=True)
    with pytest.raises(OSError):
        scope.inspect_file(candidate.path)
    assert (outside / "a.txt").read_text() == "outside\n" * 50


def test_staged_blob_batch_over_resource_limit_is_incomplete(tmp_path, monkeypatch, capsys):
    use_config(monkeypatch, complexity_enabled=False)
    monkeypatch.setattr(scope, "MAX_STAGED_OUTPUT_BYTES", 128, raising=False)
    repo = make_repo(tmp_path)
    put(repo, "large.txt", "x\n" * 200)
    git(repo, "add", "large.txt")
    code, doc = run_check(capsys, repo, staged=True)
    assert code == 3
    assert doc["incomplete"]
    assert "limit" in doc["incomplete"][0]["detail"]
    assert (repo / "large.txt").read_text() == "x\n" * 200


def test_exempt_json_does_not_consume_staged_blob_resource_limit(tmp_path, monkeypatch, capsys):
    use_config(monkeypatch, complexity_enabled=False)
    monkeypatch.setattr(scope, "MAX_STAGED_OUTPUT_BYTES", 128, raising=False)
    repo = make_repo(tmp_path)
    put(repo, "large.json", "[\n" + ",\n".join("0" for _ in range(200)) + "\n]\n")
    put(repo, "small.txt", "ok\n")
    git(repo, "add", "large.json", "small.txt")
    code, doc = run_check(capsys, repo, staged=True)
    assert code == 0 and doc["incomplete"] == []


@pytest.mark.parametrize("port", [0, 65536, 99999])
def test_immutable_source_rejects_invalid_port(port):
    assert not is_immutable_source(f"git+https://example.test:{port}/org/rig@" + "a" * 40)


@pytest.mark.parametrize("port", [1, 443, 65535])
def test_immutable_source_accepts_valid_port(port):
    assert is_immutable_source(f"git+https://example.test:{port}/org/rig@" + "a" * 40)


def test_explicit_staged_path_may_be_missing_from_working_tree(tmp_path, monkeypatch, capsys):
    use_config(monkeypatch, complexity_enabled=False)
    repo = make_repo(tmp_path)
    put(repo, "indexed.txt", "x\n" * 2401)
    git(repo, "add", "indexed.txt")
    (repo / "indexed.txt").unlink()
    code, doc = run_check(capsys, repo, "indexed.txt", staged=True)
    assert code == 1 and doc["findings"][0]["value"] == 2401


def test_case_insensitive_staged_file_directory_collision_is_incomplete(tmp_path, monkeypatch, capsys):
    use_config(monkeypatch, complexity_enabled=False)
    repo = make_repo(tmp_path)
    put(repo, "A", "one\n")
    put(repo, "a/child.txt", "two\n")
    git(repo, "add", "A", "a/child.txt")
    monkeypatch.setattr(scope, "_case_insensitive", lambda _: True)
    code, doc = run_check(capsys, repo, staged=True)
    assert code == 3
    assert {item["path"] for item in doc["incomplete"]} == {"A", "a/child.txt"}


def test_unmerged_index_is_incomplete_not_a_clean_scan(tmp_path, monkeypatch, capsys):
    import subprocess
    use_config(monkeypatch, complexity_enabled=False)
    repo = make_repo(tmp_path)
    put(repo, "conflict.txt", "base\n")
    git(repo, "add", "conflict.txt")
    git(repo, "commit", "-qm", "base")
    main = git(repo, "branch", "--show-current").strip()
    git(repo, "checkout", "-qb", "side")
    put(repo, "conflict.txt", "side\n")
    git(repo, "commit", "-qam", "side")
    git(repo, "checkout", "-q", main)
    put(repo, "conflict.txt", "main\n")
    git(repo, "commit", "-qam", "main")
    result = subprocess.run(["git", "-C", str(repo), "merge", "side"], capture_output=True)
    assert result.returncode == 1
    code, doc = run_check(capsys, repo, staged=True)
    assert code == 3 and any("unmerged" in item["detail"] for item in doc["incomplete"])


def test_analyzer_output_limit_is_incomplete(tmp_path, monkeypatch):
    import sys
    from riglib import lint_check_analyzer as analyzer
    script = tmp_path / "analyzer"
    script.write_text(f"#!{sys.executable}\nimport sys\nsys.stdout.write('x' * 200)\n")
    script.chmod(0o755)
    source = tmp_path / "a.py"
    source.write_text("print('x')\n")
    monkeypatch.setattr(analyzer, "ANALYZER_OUTPUT_LIMIT", 128, raising=False)
    reports, errors = analyzer.analyze(str(script), [scope.ScopedFile("a.py", str(source))], str(tmp_path))
    assert not reports and errors
    assert errors[0].reason == "analyzer-output-limit"


def test_capture_bounds_stderr_and_stops_a_live_producer(tmp_path):
    import sys
    from riglib.lint_check_capture import OutputLimitError, capture
    with pytest.raises(OutputLimitError):
        capture([sys.executable, "-c", "import os\nwhile True: os.write(2, b'x'*65536)"],
                cwd=str(tmp_path), timeout=3, limit=128)


def test_capture_timeout_and_success_preserve_process_results(tmp_path):
    import subprocess
    import sys
    from riglib.lint_check_capture import capture
    with pytest.raises(subprocess.TimeoutExpired):
        capture([sys.executable, "-c", "import time; time.sleep(5)"],
                cwd=str(tmp_path), timeout=0.05, limit=1024)
    result = capture([sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(7)"],
                     cwd=str(tmp_path), timeout=3, limit=1024)
    assert result.returncode == 7 and result.stdout == b"out\n" and result.stderr == b"err\n"
