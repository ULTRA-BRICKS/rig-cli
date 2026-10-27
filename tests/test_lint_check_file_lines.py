"""`rig lint check` file-length check and file scope (tracked + untracked-not-ignored)."""
from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path

import pytest

from tests.lint_check_helpers import (commit_all, git, lines_source, make_repo, put, raw_name_dir,
                                      require_raw_name_dir, rules, run_check, use_config)


@pytest.fixture
def lines_only(monkeypatch):
    return use_config(monkeypatch, complexity_enabled=False)


@pytest.fixture
def raw_name_root(tmp_path):
    root = require_raw_name_dir(tmp_path)
    yield root
    if tmp_path not in root.parents:
        shutil.rmtree(root, ignore_errors=True)


def test_raw_name_dir_falls_back_when_tmp_refuses_non_utf8_names(tmp_path, monkeypatch):
    real_open = os.open

    def utf8only_tmp(path, *args, **kwargs):  # the CI runner's /tmp: EILSEQ for names that are not UTF-8
        if os.fsencode(path).startswith(os.fsencode(tmp_path)) and os.fsencode(path).endswith(b"\xff"):
            raise OSError(errno.EILSEQ, os.strerror(errno.EILSEQ))
        return real_open(path, *args, **kwargs)

    shm_probe = raw_name_dir(Path("/dev/shm"))
    if shm_probe is None:
        pytest.skip("/dev/shm does not accept non-UTF-8 names here either")
    shutil.rmtree(shm_probe, ignore_errors=True)
    monkeypatch.setattr(os, "open", utf8only_tmp)
    before = set(tmp_path.iterdir())
    found = raw_name_dir(tmp_path)
    assert found is not None
    try:
        assert tmp_path not in found.parents and str(found).startswith("/dev/shm/")
    finally:
        shutil.rmtree(found, ignore_errors=True)
    assert set(tmp_path.iterdir()) == before  # the refused candidate was removed


@pytest.mark.parametrize(("count", "severity"), [
    (1199, None), (1200, None), (1201, "warning"), (2400, "warning"), (2401, "error"),
])
def test_exact_line_boundaries(tmp_path, capsys, lines_only, count, severity):
    repo = make_repo(tmp_path)
    put(repo, "src/big.ts", lines_source(count))
    code, doc = run_check(capsys, repo)
    found = rules(doc, "file-lines/max-lines")
    if severity is None:
        assert (code, found) == (0, [])
        assert doc["checks"]["file_lines"]["status"] == "pass"
        return
    assert [f["severity"] for f in found] == [severity]
    assert found[0]["value"] == count
    assert found[0]["limit"] == (1200 if severity == "warning" else 2400)
    assert code == (1 if severity == "error" else 0)
    assert doc["checks"]["file_lines"]["status"] == ("fail" if severity == "error" else "pass")


def test_unterminated_last_line_counts_and_crlf_counts_once(tmp_path, capsys, lines_only):
    repo = make_repo(tmp_path)
    put(repo, "a.txt", lines_source(1201, final_newline=False))
    put(repo, "b.txt", lines_source(1201, eol="\r\n"))
    put(repo, "c.txt", lines_source(1200, final_newline=False))
    put(repo, "empty.txt", "")
    code, doc = run_check(capsys, repo)
    by_path = {f["path"]: f["value"] for f in rules(doc)}
    assert by_path == {"a.txt": 1201, "b.txt": 1201}
    assert doc["summary"]["files_considered"] == 4


def test_json_is_the_only_default_exemption_case_insensitive(tmp_path, capsys, lines_only):
    repo = make_repo(tmp_path)
    put(repo, "data.json", lines_source(3000))
    put(repo, "UPPER.JSON", lines_source(3000))
    put(repo, "uv.lock", lines_source(2401))
    put(repo, "package-lock.json.bak", lines_source(1500))
    code, doc = run_check(capsys, repo)
    assert sorted(f["path"] for f in rules(doc)) == ["package-lock.json.bak", "uv.lock"]
    assert code == 1
    skipped = {(s["path"], s["check"], s["reason"]) for s in doc["skipped"]}
    assert ("data.json", "file_lines", "exempt-extension") in skipped
    assert ("UPPER.JSON", "file_lines", "exempt-extension") in skipped


def test_configured_exempt_extensions_and_thresholds(tmp_path, monkeypatch, capsys):
    use_config(monkeypatch, complexity_enabled=False, exempt_extensions=("lock",), warn_above=10,
               error_above=20)
    repo = make_repo(tmp_path)
    put(repo, "uv.lock", lines_source(500))
    put(repo, "data.json", lines_source(15))
    put(repo, "huge.json", lines_source(25))
    code, doc = run_check(capsys, repo)
    assert {(f["path"], f["severity"]) for f in rules(doc)} == {("data.json", "warning"), ("huge.json", "error")}
    assert doc["config"]["file_lines"]["warn_above"] == 10


def test_file_lines_disabled_reports_disabled(tmp_path, monkeypatch, capsys):
    use_config(monkeypatch, complexity_enabled=False, file_lines_enabled=False)
    repo = make_repo(tmp_path)
    put(repo, "a.ts", lines_source(5000))
    code, doc = run_check(capsys, repo)
    assert code == 0 and doc["findings"] == []
    assert doc["checks"]["file_lines"]["status"] == "disabled"


def test_ignored_files_are_not_enumerated_but_tracked_files_always_are(tmp_path, capsys, lines_only):
    repo = make_repo(tmp_path)
    put(repo, "tracked_big.ts", lines_source(2500))
    put(repo, ".gitignore", "")
    commit_all(repo)
    put(repo, ".gitignore", "*.ts\nbuild/\n")
    put(repo, "ignored_new.ts", lines_source(2500))
    put(repo, "build/out.js", lines_source(2500))
    put(repo, "untracked.md", lines_source(1300))
    code, doc = run_check(capsys, repo)
    assert sorted(f["path"] for f in rules(doc)) == ["tracked_big.ts", "untracked.md"]


def test_binary_files_are_skipped_with_a_reason_not_silently(tmp_path, capsys, lines_only):
    repo = make_repo(tmp_path)
    put(repo, "blob.bin", b"\0" + b"\n" * 5000)
    put(repo, "late_nul.txt", lines_source(1300).encode() + b"\0")
    code, doc = run_check(capsys, repo)
    assert {(s["path"], s["reason"]) for s in doc["skipped"]} == {("blob.bin", "binary")}
    assert [f["path"] for f in rules(doc)] == ["late_nul.txt"]


def test_symlink_files_and_symlinked_directories_never_escape(tmp_path, capsys, lines_only):
    outside = tmp_path / "outside"
    outside.mkdir()
    put(outside, "evil.ts", lines_source(3000))
    repo = make_repo(tmp_path)
    put(repo, "real/ok.ts", "x\n")
    commit_all(repo)
    os.symlink(outside / "evil.ts", repo / "link.ts")
    (repo / "real" / "ok.ts").unlink()
    (repo / "real").rmdir()
    os.symlink(outside, repo / "real")
    code, doc = run_check(capsys, repo)
    assert code == 0 and doc["findings"] == []
    reasons = {(s["path"], s["reason"]) for s in doc["skipped"]}
    assert ("link.ts", "symlink") in reasons
    assert ("real/ok.ts", "symlink") in reasons


def test_nested_repository_is_skipped_as_submodule(tmp_path, capsys, lines_only):
    repo = make_repo(tmp_path)
    nested = repo / "vendor" / "inner"
    nested.mkdir(parents=True)
    git(nested, "init", "-q")
    put(nested, "big.ts", lines_source(3000))
    code, doc = run_check(capsys, repo)
    assert code == 0
    assert any(s["reason"] == "submodule" for s in doc["skipped"])
    assert doc["findings"] == []


def test_filenames_with_spaces_newlines_and_dashes(tmp_path, capsys, lines_only):
    repo = make_repo(tmp_path)
    names = ["-lead.txt", "sp ace.txt", "new\nline.txt", "tab\tname.txt"]
    for name in names:
        put(repo, name, lines_source(1300))
    code, doc = run_check(capsys, repo)
    assert sorted(f["path"] for f in rules(doc)) == sorted(names)


def test_path_arguments_restrict_and_must_exist_inside_the_repo(tmp_path, capsys, lines_only):
    repo = make_repo(tmp_path)
    put(repo, "a/big.txt", lines_source(1300))
    put(repo, "ab/big.txt", lines_source(1300))
    put(repo, "b/big.txt", lines_source(1300))
    code, doc = run_check(capsys, repo, "a", "b/big.txt")
    assert sorted(f["path"] for f in rules(doc)) == ["a/big.txt", "b/big.txt"]
    code, doc = run_check(capsys, repo, "nope")
    assert code == 2 and "does not exist" in doc["error"]
    code, doc = run_check(capsys, repo, str(tmp_path))
    assert code == 2 and "outside" in doc["error"]


def test_subdirectory_cwd_scans_the_whole_repo_unless_restricted(tmp_path, capsys, lines_only):
    repo = make_repo(tmp_path)
    put(repo, "top.txt", lines_source(1300))
    put(repo, "sub/inner.txt", lines_source(1300))
    code, doc = run_check(capsys, repo, cwd=repo / "sub")
    assert sorted(f["path"] for f in rules(doc)) == ["sub/inner.txt", "top.txt"]
    code, doc = run_check(capsys, repo, ".", cwd=repo / "sub")
    assert [f["path"] for f in rules(doc)] == ["sub/inner.txt"]


def test_not_a_git_repository_is_a_usage_error(tmp_path, capsys, lines_only):
    plain = tmp_path / "plain"
    plain.mkdir()
    code, doc = run_check(capsys, plain)
    assert code == 2 and "not a git repository" in doc["error"]


def test_exempt_files_are_never_opened(tmp_path, monkeypatch, capsys, lines_only):
    from riglib import lint_check

    opened: list[str] = []
    real = lint_check.inspect_file
    monkeypatch.setattr(lint_check, "inspect_file", lambda path: opened.append(os.path.basename(path)) or real(path))
    repo = make_repo(tmp_path)
    put(repo, "big.json", lines_source(5000))
    put(repo, "a.txt", "x\n")
    code, doc = run_check(capsys, repo)
    assert code == 0 and opened == ["a.txt"]
    assert {(s["path"], s["check"], s["reason"]) for s in doc["skipped"]} == {
        ("big.json", "file_lines", "exempt-extension")}
    assert doc["summary"]["files_considered"] == 1


def test_files_no_enabled_check_needs_are_not_read_or_reported(tmp_path, monkeypatch, capsys):
    from riglib import lint_check

    monkeypatch.setattr(lint_check, "inspect_file", lambda path: pytest.fail("must not read " + path))
    use_config(monkeypatch, complexity_enabled=False, file_lines_enabled=False)
    repo = make_repo(tmp_path)
    put(repo, "a.txt", lines_source(5000))
    code, doc = run_check(capsys, repo)
    assert code == 0 and doc["skipped"] == [] and doc["summary"]["files_considered"] == 0


def test_non_utf8_filename_yields_a_document_not_a_traceback(raw_name_root, capsys, lines_only):
    repo = make_repo(raw_name_root)
    raw = os.fsencode(repo) + b"/bad\xff\xfe.txt"
    with open(raw, "wb") as handle:
        handle.write(b"x\n" * 1300)
    code, doc = run_check(capsys, repo)
    assert code == 0  # a warning only; a crash would have exited 1
    (finding,) = rules(doc)
    assert finding["severity"] == "warning" and finding["path"].startswith("bad")
    assert os.fsencode(finding["path"]) == b"bad\xff\xfe.txt"


def test_non_utf8_filename_in_text_mode_and_error_output(raw_name_root, capsys, lines_only):
    from tests.lint_check_helpers import args_for
    from riglib import lint_check

    repo = make_repo(raw_name_root)
    with open(os.fsencode(repo) + b"/bad\xff.txt", "wb") as handle:
        handle.write(b"x\n" * 2500)
    args = args_for(repo)
    args.json = False
    assert lint_check.run(args) == 1  # the real error finding, not a UnicodeEncodeError crash
    out = capsys.readouterr().out
    assert "bad\\udcff.txt" in out and "1 error(s)" in out
    missing = args_for(repo, "no\udcffsuch")
    assert lint_check.run(missing) == 2
    assert "no\\udcffsuch" in capsys.readouterr().err
