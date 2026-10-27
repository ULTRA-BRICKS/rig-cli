"""`rig lint check` fails closed: analyzer problems, coverage gaps and config errors never pass."""
from __future__ import annotations

import os

import pytest

from riglib import lint_check, lint_check_analyzer
from riglib.config import ConfigError
from riglib.lint_check_analyzer import AnalyzerUnavailable
from tests.lint_check_helpers import (FakeAnalyzer, cognitive_source, fn, lines_source, make_repo, put,
                                      require_real_cccc, rules, run_check, use_analyzer, use_config)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    use_config(monkeypatch)
    repo = make_repo(tmp_path)
    put(repo, "a.ts", "x\n")
    put(repo, "b.ts", "y\n")
    return repo


def _incomplete(doc):
    return {(i["path"], i["reason"]) for i in doc["incomplete"]}


@pytest.mark.parametrize("message", ["analyzer not installed", "sha256 mismatch with the pin"])
def test_missing_or_mismatched_analyzer_is_127_naming_the_install_command(repo, monkeypatch, capsys, message):
    def unavailable():
        raise AnalyzerUnavailable(message)

    monkeypatch.setattr(lint_check, "resolve_analyzer", unavailable)
    put(repo, "huge.txt", lines_source(3000))
    code, doc = run_check(capsys, repo)
    assert code == 127 and doc["ok"] is False
    assert "rig apply commit" in doc["error"] and message in doc["error"]
    assert doc["analyzer"]["verified"] is False and doc["analyzer"]["path"] is None
    assert doc["checks"]["complexity"]["status"] == "incomplete"
    assert rules(doc, "file-lines/max-lines")  # other check still reported


def test_exit_priority_config_beats_analyzer_beats_incomplete_beats_findings(tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path)
    put(repo, "a.ts", "x\n")
    put(repo, "huge.txt", lines_source(3000))
    use_config(monkeypatch)

    fake = FakeAnalyzer(tmp_path / "bin", raw="garbage")
    use_analyzer(monkeypatch, fake.path)
    code, doc = run_check(capsys, repo)
    assert code == 3 and rules(doc)  # incomplete (3) outranks the error finding (1)

    def unavailable():
        raise AnalyzerUnavailable("gone")

    monkeypatch.setattr(lint_check, "resolve_analyzer", unavailable)
    assert run_check(capsys, repo)[0] == 127

    def bad_config(cwd, config_path):
        raise ConfigError("lint_checks.complexity.max_cognitive: must be an integer >= 1 (repo rig.yaml)")

    monkeypatch.setattr(lint_check, "load_config", bad_config)
    code, doc = run_check(capsys, repo)
    assert code == 2 and "max_cognitive" in doc["error"] and "repo rig.yaml" in doc["error"]


def test_findings_only_is_exit_1_and_warnings_alone_are_exit_0(tmp_path, monkeypatch, capsys):
    use_config(monkeypatch, complexity_enabled=False)
    repo = make_repo(tmp_path)
    put(repo, "w.txt", lines_source(1300))
    assert run_check(capsys, repo)[0] == 0
    put(repo, "e.txt", lines_source(2500))
    assert run_check(capsys, repo)[0] == 1


@pytest.mark.parametrize("spec", [
    {"exit": 2, "stderr": "boom"},
    {"exit": 1},
    {"exit": 101, "stderr": "panic"},
])
def test_analyzer_crash_or_nonzero_exit_is_incomplete(repo, tmp_path, monkeypatch, capsys, spec):
    use_analyzer(monkeypatch, FakeAnalyzer(tmp_path / "bin", **spec).path)
    code, doc = run_check(capsys, repo)
    assert code == 3
    assert _incomplete(doc) == {("a.ts", "analyzer-failed"), ("b.ts", "analyzer-failed")}
    assert doc["checks"]["complexity"]["status"] == "incomplete"


def test_analyzer_timeout_is_incomplete(repo, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(lint_check_analyzer, "ANALYZER_TIMEOUT_SECONDS", 1)
    use_analyzer(monkeypatch, FakeAnalyzer(tmp_path / "bin", sleep=5).path)
    code, doc = run_check(capsys, repo)
    assert code == 3 and {r for _, r in _incomplete(doc)} == {"analyzer-timeout"}


@pytest.mark.parametrize("raw", ["", "not json", "[]", "{}", '{"files": {}}', '{"files": []}',
                                 '{"files": [], "summary": {"file_count": 5}}'])
def test_malformed_or_empty_output_with_eligible_files_is_incomplete(repo, tmp_path, monkeypatch, capsys, raw):
    use_analyzer(monkeypatch, FakeAnalyzer(tmp_path / "bin", raw=raw).path)
    code, doc = run_check(capsys, repo)
    assert code == 3
    assert {r for _, r in _incomplete(doc)} <= {"malformed-output"} and len(doc["incomplete"]) == 2


def test_one_missing_file_in_a_nonempty_response_is_incomplete(repo, tmp_path, monkeypatch, capsys):
    use_analyzer(monkeypatch, FakeAnalyzer(tmp_path / "bin", omit=["b.ts"]).path)
    code, doc = run_check(capsys, repo)
    assert code == 3
    assert _incomplete(doc) == {("b.ts", "missing-from-output")}


def test_duplicate_file_record_is_incomplete_and_not_trusted(repo, tmp_path, monkeypatch, capsys):
    funcs = {"a.ts": [fn("hot", cognitive=99)]}
    use_analyzer(monkeypatch, FakeAnalyzer(tmp_path / "bin", dup=["a.ts"], functions=funcs).path)
    code, doc = run_check(capsys, repo)
    assert code == 3
    assert _incomplete(doc) == {("a.ts", "duplicate-file")}
    assert rules(doc) == []


def test_unexpected_file_record_and_prefix_confusion_are_incomplete(repo, tmp_path, monkeypatch, capsys):
    root = os.path.realpath(repo)
    extras = [f"{root}/a.tsx", f"{root}/a.ts.bak", "/etc/passwd"]
    use_analyzer(monkeypatch, FakeAnalyzer(tmp_path / "bin", extra=extras).path)
    code, doc = run_check(capsys, repo)
    assert code == 3
    assert {p for p, r in _incomplete(doc) if r == "unexpected-file"} == {os.path.normpath(e) for e in extras}


def test_relative_and_unnormalized_output_paths_still_match_exactly(repo, tmp_path, monkeypatch, capsys):
    root = os.path.realpath(repo)
    record = {"a.ts": {"path": "./a.ts"}, "b.ts": {"path": f"{root}/sub/../b.ts"}}
    use_analyzer(monkeypatch, FakeAnalyzer(tmp_path / "bin", record=record).path)
    code, doc = run_check(capsys, repo)
    assert (code, doc["incomplete"]) == (0, [])


@pytest.mark.parametrize("bad_function", [
    {"name": "f", "kind": "function", "line": 1, "cognitive": "3", "cyclomatic": 1},
    {"name": "f", "kind": "function", "line": 1, "cognitive": True, "cyclomatic": 1},
    {"name": "f", "kind": "function", "line": 1, "cognitive": 1.5, "cyclomatic": 1},
    {"name": "f", "kind": "function", "line": 1, "cognitive": -1, "cyclomatic": 1},
    {"name": "f", "kind": "function", "line": 1, "cognitive": 1},
    {"name": "f", "kind": "function", "line": 0, "cognitive": 1, "cyclomatic": 1},
    {"name": 7, "kind": "function", "line": 1, "cognitive": 1, "cyclomatic": 1},
    {"name": "f", "kind": "function", "line": 1, "cognitive": 1, "cyclomatic": 1, "children": "x"},
    {"name": "f", "kind": "function", "line": 1, "cognitive": 1, "cyclomatic": 1,
     "children": [{"name": "g", "kind": "function", "line": 2, "cognitive": None, "cyclomatic": 1}]},
])
def test_malformed_nested_functions_or_metrics_are_incomplete(repo, tmp_path, monkeypatch, capsys, bad_function):
    use_analyzer(monkeypatch, FakeAnalyzer(tmp_path / "bin", functions={"a.ts": [bad_function]}).path)
    code, doc = run_check(capsys, repo)
    assert code == 3
    assert {r for _, r in _incomplete(doc)} == {"malformed-output"}


def test_malformed_file_level_record_is_incomplete(repo, tmp_path, monkeypatch, capsys):
    use_analyzer(monkeypatch, FakeAnalyzer(tmp_path / "bin", record={"a.ts": {"cognitive": "x"}}).path)
    assert run_check(capsys, repo)[0] == 3


def test_parse_error_details_are_incomplete_even_with_exit_zero(repo, tmp_path, monkeypatch, capsys):
    fake = FakeAnalyzer(tmp_path / "bin", record={"a.ts": {"parse_errors": ["syntax error at line 3"]}})
    use_analyzer(monkeypatch, fake.path)
    code, doc = run_check(capsys, repo)
    assert code == 3
    (entry,) = doc["incomplete"]
    assert (entry["path"], entry["reason"], entry["detail"]) == ("a.ts", "parse-error", "syntax error at line 3")


def test_parse_errors_wrong_type_is_malformed(repo, tmp_path, monkeypatch, capsys):
    use_analyzer(monkeypatch, FakeAnalyzer(tmp_path / "bin", record={"a.ts": {"parse_errors": "oops"}}).path)
    assert run_check(capsys, repo)[0] == 3


def test_real_analyzer_parse_error_file_is_incomplete(tmp_path, monkeypatch, capsys):
    use_analyzer(monkeypatch, require_real_cccc())
    use_config(monkeypatch)
    repo = make_repo(tmp_path)
    put(repo, "bad.py", "def f(:\n")
    put(repo, "good.py", "def g():\n    return 1\n")
    code, doc = run_check(capsys, repo)
    assert code == 3
    assert [(i["path"], i["reason"]) for i in doc["incomplete"]] == [("bad.py", "parse-error")]
    assert doc["checks"]["complexity"]["status"] == "incomplete"


def test_real_analyzer_covers_every_file_exactly(tmp_path, monkeypatch, capsys):
    use_analyzer(monkeypatch, require_real_cccc())
    use_config(monkeypatch)
    repo = make_repo(tmp_path)
    put(repo, "-dash.ts", cognitive_source(16))
    put(repo, "sp ace.ts", "const x = 1;\n")
    put(repo, "nl\nname.js", "const y = 2;\n")
    code, doc = run_check(capsys, repo)
    assert code == 1 and doc["incomplete"] == []
    assert [f["path"] for f in rules(doc)] == ["-dash.ts"]
    assert doc["analyzer"]["version"] == "1.8.4" and doc["analyzer"]["verified"] is True


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_unreadable_file_is_incomplete_for_both_checks(repo, tmp_path, monkeypatch, capsys):
    use_analyzer(monkeypatch, FakeAnalyzer(tmp_path / "bin").path)
    (repo / "a.ts").chmod(0)
    try:
        code, doc = run_check(capsys, repo)
    finally:
        (repo / "a.ts").chmod(0o644)
    assert code == 3
    assert {(i["path"], i["check"], i["reason"]) for i in doc["incomplete"]} == {
        ("a.ts", "file_lines", "unreadable"), ("a.ts", "complexity", "unreadable")}
    assert doc["checks"]["file_lines"]["status"] == "incomplete"


def test_resolve_analyzer_default_maps_install_errors_to_unavailable(monkeypatch):
    from riglib import cccc_install

    def missing():
        raise cccc_install.AnalyzerMissing("not installed")

    monkeypatch.setattr(cccc_install, "resolve_verified_binary", missing)
    with pytest.raises(AnalyzerUnavailable, match="not installed"):
        lint_check.resolve_analyzer()


def test_json_unsafe_exit_never_prints_ok_true_on_failure(repo, tmp_path, monkeypatch, capsys):
    use_analyzer(monkeypatch, FakeAnalyzer(tmp_path / "bin", raw="garbage").path)
    code, doc = run_check(capsys, repo)
    assert doc["ok"] is False and doc["exit_code"] == code == 3
    assert doc["summary"]["incomplete"] == 2


def test_zero_files_analyzed_although_eligible_files_exist_is_incomplete(repo, tmp_path, monkeypatch, capsys):
    raw = '{"files": [], "summary": {"file_count": 0}}'
    use_analyzer(monkeypatch, FakeAnalyzer(tmp_path / "bin", raw=raw).path)
    code, doc = run_check(capsys, repo)
    assert code == 3
    assert _incomplete(doc) == {("a.ts", "missing-from-output"), ("b.ts", "missing-from-output")}


# --- summary parse errors (L4) and --fix bookkeeping when the run cannot reach the fixer (L1) ---

def _summary(**extra):
    return {"summary": {"file_count": 2, **extra}}


def test_summary_parse_error_count_without_a_carrying_record_fails_the_batch_closed(repo, tmp_path, monkeypatch, capsys):
    use_analyzer(monkeypatch, FakeAnalyzer(tmp_path / "bin", doc=_summary(parse_error_count=1)).path)
    code, doc = run_check(capsys, repo)
    assert code == 3 and doc["ok"] is False
    assert _incomplete(doc) == {("a.ts", "malformed-output"), ("b.ts", "malformed-output")}


def test_summary_named_parse_error_file_is_incomplete_even_when_its_record_is_clean(repo, tmp_path, monkeypatch, capsys):
    named = os.path.join(os.path.realpath(repo), "a.ts")
    summary = _summary(parse_error_count=1, parse_error_file_count=1, parse_error_files=[named])
    use_analyzer(monkeypatch, FakeAnalyzer(tmp_path / "bin", doc=summary).path)
    code, doc = run_check(capsys, repo)
    assert code == 3 and _incomplete(doc) == {("a.ts", "parse-error")}


def test_summary_count_larger_than_the_explained_files_adds_an_unexplained_record(repo, tmp_path, monkeypatch, capsys):
    named = os.path.join(os.path.realpath(repo), "a.ts")
    summary = _summary(parse_error_count=3, parse_error_file_count=1, parse_error_files=[named])
    use_analyzer(monkeypatch, FakeAnalyzer(tmp_path / "bin", doc=summary).path)
    code, doc = run_check(capsys, repo)
    assert code == 3
    assert _incomplete(doc) == {("a.ts", "parse-error"), ("<analyzer>", "parse-error-unexplained")}
    (extra,) = [i for i in doc["incomplete"] if i["reason"] == "parse-error-unexplained"]
    assert "3 parse error(s)" in extra["detail"] and "2 unexplained" in extra["detail"]


@pytest.mark.parametrize("extra", [{"parse_error_count": "1"}, {"parse_error_count": -1},
                                   {"parse_error_count": True}, {"parse_error_files": "a.ts"},
                                   {"parse_error_files": [1]}])
def test_malformed_parse_error_summary_fields_fail_closed(repo, tmp_path, monkeypatch, capsys, extra):
    use_analyzer(monkeypatch, FakeAnalyzer(tmp_path / "bin", doc=_summary(**extra)).path)
    code, doc = run_check(capsys, repo)
    assert code == 3 and {reason for _, reason in _incomplete(doc)} == {"malformed-output"}


def test_zero_summary_parse_errors_stay_clean(repo, tmp_path, monkeypatch, capsys):
    summary = _summary(parse_error_count=0, parse_error_file_count=0, parse_error_files=[])
    use_analyzer(monkeypatch, FakeAnalyzer(tmp_path / "bin", doc=summary).path)
    assert run_check(capsys, repo)[0] == 0


def _unavailable(monkeypatch, message):
    def gone():
        raise AnalyzerUnavailable(message)

    monkeypatch.setattr(lint_check, "resolve_analyzer", gone)


@pytest.mark.parametrize("message", ["gone", "gone; run `rig apply commit` to install the pinned analyzer"])
def test_install_hint_appears_once_and_the_gap_is_listed_as_incomplete(repo, monkeypatch, capsys, message):
    _unavailable(monkeypatch, message)
    code, doc = run_check(capsys, repo, fix=True)
    assert code == 127 and doc["error"].count("rig apply commit") == 1
    assert doc["summary"]["incomplete"] == 1 and doc["incomplete"][0]["reason"] == "analyzer-unavailable"
    assert doc["fix"]["requested"] is True and doc["fix"]["applied"] == []


def test_text_mode_reports_the_analyzer_error_exactly_once(repo, monkeypatch, capsys):
    from tests.lint_check_helpers import args_for

    _unavailable(monkeypatch, "gone")
    assert lint_check.run(args_for(repo, json_out=False)) == 127
    captured = capsys.readouterr()
    assert (captured.out + captured.err).count("analyzer unavailable") == 1


def test_fix_requested_is_reported_when_the_master_switch_is_off(tmp_path, monkeypatch, capsys):
    use_config(monkeypatch, enabled=False)
    code, doc = run_check(capsys, make_repo(tmp_path), fix=True)
    assert code == 0 and doc["fix"]["requested"] is True


def test_fix_requested_is_reported_on_a_usage_refusal_and_absent_otherwise(repo, monkeypatch, capsys):
    code, doc = run_check(capsys, repo, staged=True, fix=True)
    assert code == 2 and doc["fix"]["requested"] is True
    use_analyzer(monkeypatch, FakeAnalyzer(repo.parent / "bin").path)
    assert run_check(capsys, repo)[1]["fix"]["requested"] is False
