"""`rig lint check` complexity check: exact thresholds, finding shape, invocation hygiene."""
from __future__ import annotations

import json
import subprocess

import pytest

from riglib import lint_check, lint_check_analyzer
from tests.lint_check_helpers import (FakeAnalyzer, cognitive_source, commit_all, cyclomatic_source, fn,
                                      make_repo, put, require_real_cccc, rules, run_check, use_analyzer,
                                      use_config)


@pytest.mark.parametrize(("count", "failing"), [(15, False), (16, True)])
def test_cognitive_limit_is_strictly_greater_than_with_real_analyzer(tmp_path, monkeypatch, capsys, count, failing):
    use_analyzer(monkeypatch, require_real_cccc())
    use_config(monkeypatch)
    repo = make_repo(tmp_path)
    put(repo, "a.ts", cognitive_source(count))
    code, doc = run_check(capsys, repo)
    assert (code, doc["checks"]["complexity"]["status"]) == ((1, "fail") if failing else (0, "pass"))
    found = rules(doc, "complexity/cognitive")
    assert bool(found) is failing
    if failing:
        assert (found[0]["value"], found[0]["limit"], found[0]["function"], found[0]["line"]) == (16, 15, "f", 1)
        assert found[0]["fixable"] is False  # `if` chains without an `else` have no qualifying fix site
    assert not rules(doc, "complexity/cyclomatic")


@pytest.mark.parametrize(("count", "failing"), [(20, False), (21, True)])
def test_cyclomatic_limit_is_strictly_greater_than_with_real_analyzer(tmp_path, monkeypatch, capsys, count, failing):
    use_analyzer(monkeypatch, require_real_cccc())
    use_config(monkeypatch)
    repo = make_repo(tmp_path)
    put(repo, "a.ts", cyclomatic_source(count))
    code, doc = run_check(capsys, repo)
    assert code == (1 if failing else 0)
    found = rules(doc, "complexity/cyclomatic")
    assert bool(found) is failing
    if failing:
        assert (found[0]["value"], found[0]["limit"], found[0]["fixable"]) == (21, 20, False)


def test_custom_thresholds_apply_and_are_reported(tmp_path, monkeypatch, capsys):
    fake = FakeAnalyzer(tmp_path / "bin", functions={"a.ts": [fn("f", cognitive=5, cyclomatic=7)]})
    use_analyzer(monkeypatch, fake.path)
    use_config(monkeypatch, max_cognitive=5, max_cyclomatic=6)
    repo = make_repo(tmp_path)
    put(repo, "a.ts", "x\n")
    code, doc = run_check(capsys, repo)
    assert code == 1
    assert [f["rule"] for f in doc["findings"]] == ["complexity/cyclomatic"]
    assert doc["config"]["complexity"] == {"enabled": True, "max_cognitive": 5, "max_cyclomatic": 6}


def test_nested_functions_are_measured_and_named_by_path(tmp_path, monkeypatch, capsys):
    funcs = [fn("outer", 1, 1, line=1, children=[fn("inner", 30, 1, line=4)])]
    fake = FakeAnalyzer(tmp_path / "bin", functions={"a.ts": funcs})
    use_analyzer(monkeypatch, fake.path)
    use_config(monkeypatch)
    repo = make_repo(tmp_path)
    put(repo, "a.ts", "x\n")
    code, doc = run_check(capsys, repo)
    assert code == 1
    (finding,) = doc["findings"]
    assert (finding["function"], finding["line"], finding["value"]) == ("outer.inner", 4, 30)


def test_invocation_is_explicit_files_no_config_and_survives_odd_names(tmp_path, monkeypatch, capsys):
    fake = FakeAnalyzer(tmp_path / "bin")
    use_analyzer(monkeypatch, fake.path)
    use_config(monkeypatch)
    repo = make_repo(tmp_path)
    for name in ("-dash.ts", "sp ace.ts", "new\nline.ts"):
        put(repo, name, "x\n")
    code, doc = run_check(capsys, repo)
    assert code == 0 and doc["checks"]["complexity"]["status"] == "pass"
    (call,) = fake.calls()
    assert call[:3] == ["--no-config", "--no-cache", "--no-ignore"]
    files = call[3:]
    assert len(files) == 3 and all(f.startswith("/") for f in files)
    assert sorted(f.rsplit("/", 1)[1] for f in files) == sorted(["-dash.ts", "sp ace.ts", "new\nline.ts"])


def test_large_file_lists_are_batched_without_losing_files(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(lint_check_analyzer, "ARG_BYTES_PER_BATCH", 200)
    fake = FakeAnalyzer(tmp_path / "bin")
    use_analyzer(monkeypatch, fake.path)
    use_config(monkeypatch)
    repo = make_repo(tmp_path)
    for i in range(12):
        put(repo, f"dir/file_{i:02d}.ts", "x\n")
    code, doc = run_check(capsys, repo)
    assert code == 0
    assert len(fake.calls()) > 1
    assert sum(len(c) - 3 for c in fake.calls()) == 12
    assert doc["summary"]["files_considered"] == 12


def test_complexity_disabled_never_runs_or_needs_the_analyzer(tmp_path, monkeypatch, capsys):
    def boom():
        raise AssertionError("analyzer must not be resolved when complexity is disabled")

    monkeypatch.setattr(lint_check, "resolve_analyzer", boom)
    use_config(monkeypatch, complexity_enabled=False)
    repo = make_repo(tmp_path)
    put(repo, "a.ts", cognitive_source(40))
    code, doc = run_check(capsys, repo)
    assert code == 0
    assert doc["checks"]["complexity"]["status"] == "disabled"
    assert doc["checks"]["file_lines"]["status"] == "pass"
    assert doc["analyzer"] is None


def test_master_switch_off_reports_disabled_never_pass(tmp_path, monkeypatch, capsys):
    use_config(monkeypatch, enabled=False)
    monkeypatch.setattr(lint_check, "resolve_analyzer", lambda: pytest.fail("must not resolve"))
    repo = make_repo(tmp_path)
    put(repo, "a.ts", cognitive_source(40))
    code, doc = run_check(capsys, repo)
    assert code == 0 and doc["ok"] is True
    assert {c["status"] for c in doc["checks"].values()} == {"disabled"}
    assert doc["findings"] == []


def test_no_eligible_files_is_not_applicable_but_still_requires_the_analyzer(tmp_path, monkeypatch, capsys):
    fake = FakeAnalyzer(tmp_path / "bin")
    use_analyzer(monkeypatch, fake.path)
    use_config(monkeypatch)
    repo = make_repo(tmp_path)
    put(repo, "README.md", "hello\n")
    code, doc = run_check(capsys, repo)
    assert code == 0
    assert doc["checks"]["complexity"]["status"] == "not-applicable"
    assert fake.calls() == []
    assert doc["analyzer"]["verified"] is True and doc["analyzer"]["version"] == "1.8.4"
    assert len(doc["analyzer"]["sha256"]) == 64


def test_text_output_lists_findings_and_summary(tmp_path, monkeypatch, capsys):
    fake = FakeAnalyzer(tmp_path / "bin", functions={"a.ts": [fn("hot", cognitive=99)]})
    use_analyzer(monkeypatch, fake.path)
    use_config(monkeypatch)
    repo = make_repo(tmp_path)
    put(repo, "a.ts", "x\n")
    commit_all(repo)
    from tests.lint_check_helpers import args_for

    args = args_for(repo)
    args.json = False
    assert lint_check.run(args) == 1
    out = capsys.readouterr().out
    assert "a.ts:1: error complexity/cognitive hot:" in out
    assert "1 error(s)" in out and "complexity=fail" in out


def test_tracked_source_stays_in_scope_when_a_newer_ignore_pattern_hides_it(tmp_path, monkeypatch, capsys):
    use_analyzer(monkeypatch, require_real_cccc())
    use_config(monkeypatch)
    repo = make_repo(tmp_path)
    put(repo, "src/hot.ts", cognitive_source(16))
    commit_all(repo)
    put(repo, ".gitignore", "*.ts\n")
    put(repo, "src/new_hidden.ts", cognitive_source(16))  # untracked + ignored: not enumerated
    code, doc = run_check(capsys, repo)
    assert code == 1
    assert [f["path"] for f in rules(doc, "complexity/cognitive")] == ["src/hot.ts"]
    assert doc["incomplete"] == []


# ---- language coverage: every extension the pinned cccc accepts must be measured ------------

PERL_HOT = "sub f {\n my $x = shift;\n" + " if ($x) { $x = 1; }\n" * 16 + " return $x;\n}\n1;\n"
# Extensions the pinned cccc does NOT route to a front-end (probed): must stay not-applicable.
NOT_ANALYZED = ("ml", "mli", "ex", "exs", "erl", "hs", "lua", "r", "jl", "sh", "bash", "vue", "svelte",
                "astro", "groovy", "gradle", "m", "mm", "fs", "nim", "d", "sql", "html", "css", "yaml",
                "toml", "md", "txt", "json", "pyx", "cu", "proto", "sol", "ps1", "cmake", "mk", "lean")


@pytest.mark.parametrize("ext", sorted(lint_check_analyzer.SUPPORTED_EXTENSIONS))
def test_every_supported_extension_is_measured_not_silently_skipped(tmp_path, monkeypatch, capsys, ext):
    use_analyzer(monkeypatch, require_real_cccc())
    use_config(monkeypatch, file_lines_enabled=False)
    repo = make_repo(tmp_path)
    put(repo, f"sample.{ext}", "package main\n" if ext == "go" else "")  # the smallest valid unit
    code, doc = run_check(capsys, repo)
    assert (code, doc["incomplete"]) == (0, []), ext
    assert doc["checks"]["complexity"]["status"] == "pass", ext  # not-applicable would be fail-open


@pytest.mark.parametrize("name", ["lib/File/Path.pm", "UPPER/Module.PM", "script.pl"])
def test_perl_complexity_is_enforced_instead_of_failing_open(tmp_path, monkeypatch, capsys, name):
    use_analyzer(monkeypatch, require_real_cccc())
    use_config(monkeypatch)
    repo = make_repo(tmp_path)
    put(repo, name, PERL_HOT)
    code, doc = run_check(capsys, repo)
    assert code == 1
    (finding,) = rules(doc, "complexity/cognitive")
    assert (finding["path"], finding["value"], finding["function"]) == (name, 16, "f")
    assert finding["fixable"] is False


@pytest.mark.parametrize("ext", NOT_ANALYZED)
def test_extensions_the_analyzer_ignores_are_not_applicable_and_not_in_the_set(tmp_path, monkeypatch, capsys, ext):
    binary = require_real_cccc()
    assert ext not in lint_check_analyzer.SUPPORTED_EXTENSIONS
    use_analyzer(monkeypatch, binary)
    use_config(monkeypatch, file_lines_enabled=False)
    repo = make_repo(tmp_path)
    put(repo, f"sample.{ext}", "x\n")
    code, doc = run_check(capsys, repo)
    assert (code, doc["incomplete"]) == (0, [])
    assert doc["checks"]["complexity"]["status"] == "not-applicable"
    reported = subprocess.run([binary, "--no-config", str(repo / f"sample.{ext}")], capture_output=True, check=True)
    assert json.loads(reported.stdout)["files"] == []  # the analyzer agrees it does not analyze it


@pytest.mark.parametrize("ext", ["kts", "cxx", "hh", "pyi", "t", "scm", "clj", "vb", "csx", "el"])
def test_complexity_findings_use_the_fake_analyzer_for_extended_languages(tmp_path, monkeypatch, capsys, ext):
    name = f"a.{ext}"
    fake = FakeAnalyzer(tmp_path / "bin", functions={name: [fn("hot", cognitive=40)]})
    use_analyzer(monkeypatch, fake.path)
    use_config(monkeypatch)
    repo = make_repo(tmp_path)
    put(repo, name, "x\n")
    code, doc = run_check(capsys, repo)
    assert code == 1 and [f["path"] for f in rules(doc, "complexity/cognitive")] == [name]
