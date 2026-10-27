"""`fix/else-after-return`: AST rewrite behavior, safety, and `rig lint check --fix` orchestration.

Equivalence fixtures are plain JavaScript executed in real node before and after the rewrite;
the TypeScript compiler is the target-repo dependency (RIG_TEST_TYPESCRIPT_DIR or a read-only
copy found on the maintainer workstation; RIG_LINT_REQUIRE_REAL=1 turns a missing compiler into a failure).
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from riglib import lint_fix_else
from riglib.lint_fix_else import Rewrite, Skipped
from tests.lint_check_helpers import (FakeAnalyzer, commit_all, else_ifs, fn, git, link_typescript,
                                      make_repo, mode_of, put, require_real_cccc, require_typescript, rules,
                                      run_check, use_analyzer, use_config)


@pytest.fixture
def ts_repo(tmp_path):
    require_typescript()
    repo = make_repo(tmp_path)
    link_typescript(repo)
    return repo


def node(path) -> tuple[int, str]:
    # only reached through `ts_repo`, whose `require_typescript` already skipped/failed without node
    proc = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=60)
    return proc.returncode, proc.stdout


def fix(repo, rel):
    return lint_fix_else.fix_file(os.path.realpath(repo), rel)


FIXED = {
    "side_effects_in_condition": """
const log = [];
function cond(v) { log.push('cond:' + v); return v; }
function f(v) {
  if (cond(v)) {
    log.push('then');
    return 'T';
  } else {
    log.push('else');
  }
  log.push('after');
  return 'A';
}
process.stdout.write(JSON.stringify([f(true), f(false), log]) + '\\n');
""",
    "lexical_scope_class_function_and_tdz": """
const x = 'outer';
function f(c) {
  if (c) {
    return 'then';
  } else {
    const x = 'inner';
    class K { v() { return x; } }
    function g() { return x; }
    return [x, new K().v(), g()];
  }
}
function h(c) {
  if (c) { return 1; } else {
    try { return typeof y; } catch (e) { return e.name; }
    let y = 2;
  }
}
process.stdout.write(JSON.stringify([f(true), f(false), h(true), h(false)]) + '\\n');
""",
    "outer_if_false_falls_through_var": """
function f(c) {
  if (c) { return 'then'; } else { var seen = 'else'; }
  return seen;
}
process.stdout.write(JSON.stringify([f(true), f(false)]) + '\\n');
""",
    "loop_body_with_throw": """
function f(a) {
  const out = [];
  for (const x of a) {
    if (x > 1) { throw new Error('big' + x); } else { out.push(x); }
  }
  return out;
}
function g(a) { try { return f(a); } catch (e) { return e.message; } }
process.stdout.write(JSON.stringify([g([0, 1]), g([0, 5, 1])]) + '\\n');
""",
    "else_if_chains": """
function f(n) {
  if (n < 0) { return 'neg'; } else if (n === 0) { return 'zero'; } else if (n < 10) { return 'small'; } else { return 'big'; }
}
function m(n) {
  if (n < 0) { return 'neg'; } else if (n === 0) { process.stdout.write('side\\n'); } else { return 'pos'; }
  return 'end';
}
process.stdout.write(JSON.stringify([-1, 0, 5, 50].map(f)) + ' ' + JSON.stringify([-1, 0, 5].map(m)) + '\\n');
""",
    "async_await": """
async function f(c) { if (c) { return await Promise.resolve('t'); } else { await null; return 'e'; } }
async function g(c) { if (c) { throw new Error('boom'); } else { return 'ok'; } }
(async () => {
  const r = [await f(true), await f(false), await g(false)];
  try { await g(true); } catch (e) { r.push(e.message); }
  process.stdout.write(JSON.stringify(r) + '\\n');
})();
""",
    "non_block_else_statement_and_else_if": """
function f(a) { if (a) { return 1; } else process.stdout.write('side\\n'); return 2; }
function g(a) { if (a) return 'x'; else if (a === 0) return 'zero'; return 'y'; }
function h(a, k) { if (a) return 1; else (k || (() => 9))(); return 5; }
process.stdout.write(JSON.stringify([f(true), f(false), g(true), g(0), g(false), h(true), h(false)]) + '\\n');
""",
    "comments_are_kept": """
function f(c) {
  if (c) {
    return 1; // then
  } // after-then
  else /* before-block */ {
    // inside else
    return 2;
  }
}
process.stdout.write(f(true) + ' ' + f(false) + '\\n');
""",
    "strings_templates_regex_untouched": """
function f(c) {
  const t = `} else {
  return 1;`;
  const r = /} else {/.source;
  if (c) { return t + r; } else { return "else { return 'x' }" + r; }
}
process.stdout.write(JSON.stringify([f(true), f(false)]) + '\\n');
""",
}

UNCHANGED = {
    "return_nested_in_inner_constructs": """
function f(a, b) { if (a) { if (b) { return 1; } } else { return 2; } return 3; }
function g(a) { if (a) { for (;;) { return 1; } } else { return 2; } return 3; }
function h(a) { if (a) { try { return 1; } finally { } } else { return 2; } return 3; }
function s(a) { if (a) { switch (a) { default: return 1; } } else { return 2; } return 3; }
function i(a) { if (a) { process.stdout.write(String(a)); } else { return 2; } return 3; }
function j(a) { if (a) { return 1; process.stdout.write(String(a)); } else { return 2; } }
""",
    "unbraced_nested_if": """
function f(a, b) { if (a) if (b) return 1; else return 2; return 3; }
function g(l) { for (const x of l) if (x) return 1; else process.stdout.write(String(x)); return 0; }
function h(x) { lbl: if (x) return 1; else process.stdout.write(String(x)); return 2; }
function w(x) { while (x) if (x) return 1; else x--; }
function c(x) { switch (x) { case 1: if (x) { return 1; } else { return 2; } } }
""",
    "asi_hazard_is_skipped": """
function f(a, g) {
  if (a) return 1
  else (g || (() => 9))()
  return 5
}
function h(a) {
  if (a) return 1
  else [1].forEach(console.log)
  return 5
}
""",
    "declaration_else_is_skipped": """
function f(a) { if (a) { return 1; } else var q = 2; return q; }
""",
}


@pytest.mark.parametrize("name", sorted(FIXED))
def test_rewrite_is_behavior_equivalent_idempotent_and_reparses(ts_repo, name):
    path = put(ts_repo, "case.js", FIXED[name])
    before = node(path)
    assert before[0] == 0 and before[1].strip()
    outcome = fix(ts_repo, "case.js")
    assert isinstance(outcome, Rewrite) and outcome.count >= 1
    assert path.read_bytes() == outcome.written != outcome.original
    assert node(path) == before
    assert isinstance(fix(ts_repo, "case.js"), Skipped)  # idempotent: nothing left to rewrite
    assert path.read_bytes() == outcome.written


@pytest.mark.parametrize("name", sorted(UNCHANGED))
def test_non_qualifying_shapes_are_left_byte_identical(ts_repo, name):
    path = put(ts_repo, "case.js", UNCHANGED[name])
    original = path.read_bytes()
    outcome = fix(ts_repo, "case.js")
    assert isinstance(outcome, Skipped) and outcome.reason == "no-fixable-pattern"
    assert path.read_bytes() == original


def test_exact_output_keeps_braces_indentation_and_comments(ts_repo):
    put(ts_repo, "a.ts", "function f(c: boolean): number {\n  if (c) {\n    return 1;\n  } else {\n"
                         "    // keep me\n    return 2;\n  }\n}\n")
    assert isinstance(fix(ts_repo, "a.ts"), Rewrite)
    assert (ts_repo / "a.ts").read_text() == (
        "function f(c: boolean): number {\n  if (c) {\n    return 1;\n  }\n  {\n"
        "    // keep me\n    return 2;\n  }\n}\n")


def test_comments_around_else_survive_exactly(ts_repo):
    path = put(ts_repo, "case.js", FIXED["comments_are_kept"])
    assert isinstance(fix(ts_repo, "case.js"), Rewrite)
    assert path.read_text() == (
        "\nfunction f(c) {\n  if (c) {\n    return 1; // then\n  } // after-then\n"
        "  /* before-block */ {\n    // inside else\n    return 2;\n  }\n}\nprocess.stdout.write(f(true) + ' ' + f(false) + '\\n');\n")


def test_else_if_chain_becomes_sequential_ifs_with_final_bare_block(ts_repo):
    put(ts_repo, "a.ts", "function f(a: number) {\n  if (a < 0) {\n    return 'n';\n  } else if (a === 0) {\n"
                         "    return 'z';\n  } else {\n    return 'p';\n  }\n}\n")
    assert isinstance(fix(ts_repo, "a.ts"), Rewrite)
    assert (ts_repo / "a.ts").read_text() == (
        "function f(a: number) {\n  if (a < 0) {\n    return 'n';\n  }\n  if (a === 0) {\n"
        "    return 'z';\n  }\n  {\n    return 'p';\n  }\n}\n")


def test_template_regex_and_string_contents_are_byte_preserved(ts_repo):
    path = put(ts_repo, "case.js", FIXED["strings_templates_regex_untouched"])
    assert isinstance(fix(ts_repo, "case.js"), Rewrite)
    text = path.read_text()
    assert "`} else {\n  return 1;`" in text and "/} else {/" in text and "\"else { return 'x' }\"" in text


def test_crlf_files_stay_crlf(ts_repo):
    source = "function f(c) {\r\n  if (c) {\r\n    return 1;\r\n  } else {\r\n    return 2;\r\n  }\r\n}\r\n"
    path = put(ts_repo, "a.js", source)
    assert isinstance(fix(ts_repo, "a.js"), Rewrite)
    data = path.read_bytes()
    assert data.count(b"\n") == data.count(b"\r\n") and b"else" not in data
    assert data == b"function f(c) {\r\n  if (c) {\r\n    return 1;\r\n  }\r\n  {\r\n    return 2;\r\n  }\r\n}\r\n"


def test_bom_and_non_ascii_content_are_preserved(ts_repo):
    source = "\ufeff// \u65e5\u672c\u8a9e\nfunction f(c) {\n  const s = '\U0001F600 \u00e9';\n  if (c) {\n    return s;\n  } else {\n    return s + '!';\n  }\n}\nprocess.stdout.write(f(true) + ' ' + f(false) + '\\n');\n"
    path = put(ts_repo, "a.js", source)
    before = node(path)
    assert isinstance(fix(ts_repo, "a.js"), Rewrite)
    data = path.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf") and "\U0001F600 \u00e9".encode() in data
    assert node(path) == before


def test_file_mode_is_preserved_and_no_temp_files_remain(ts_repo):
    path = put(ts_repo, "bin/tool.js", FIXED["outer_if_false_falls_through_var"])
    path.chmod(0o751)
    assert isinstance(fix(ts_repo, "bin/tool.js"), Rewrite)
    assert mode_of(path) == 0o751
    assert [p.name for p in path.parent.iterdir()] == ["tool.js"]


def test_syntax_error_file_is_untouched_and_reported(ts_repo):
    path = put(ts_repo, "bad.ts", "function f(c) {\n  if (c) { return 1; } else { return 2; }\n")
    original = path.read_bytes()
    outcome = fix(ts_repo, "bad.ts")
    assert isinstance(outcome, Skipped) and outcome.reason == "parse-error"
    assert path.read_bytes() == original


def test_non_utf8_file_is_untouched(ts_repo):
    path = put(ts_repo, "a.js", b"function f(c){ if (c) { return '\xff'; } else { return 2; } }\n")
    original = path.read_bytes()
    outcome = fix(ts_repo, "a.js")
    assert isinstance(outcome, Skipped) and outcome.reason == "not-utf8"
    assert path.read_bytes() == original


def test_concurrent_modification_aborts_without_writing(ts_repo, monkeypatch):
    path = put(ts_repo, "a.js", FIXED["outer_if_false_falls_through_var"])
    real = lint_fix_else.run_parser

    def parse_then_edit(*args):
        verdict = real(*args)
        path.write_text(path.read_text() + "// edited meanwhile\n")
        return verdict

    monkeypatch.setattr(lint_fix_else, "run_parser", parse_then_edit)
    outcome = fix(ts_repo, "a.js")
    assert isinstance(outcome, Skipped) and outcome.reason == "modified-concurrently"
    assert path.read_text().endswith("// edited meanwhile\n") and "else" in path.read_text()


def test_symlinked_file_and_directory_targets_are_never_written(ts_repo, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = put(outside, "t.js", FIXED["outer_if_false_falls_through_var"])
    original = target.read_bytes()
    os.symlink(target, ts_repo / "link.js")
    os.symlink(outside, ts_repo / "dirlink")
    for rel in ("link.js", "dirlink/t.js"):
        outcome = fix(ts_repo, rel)
        assert isinstance(outcome, Skipped) and outcome.reason == "unsafe-path", rel
    assert target.read_bytes() == original


def test_missing_parser_is_explicit_and_leaves_the_file_alone(tmp_path):
    repo = make_repo(tmp_path)
    path = put(repo, "a.js", FIXED["outer_if_false_falls_through_var"])
    original = path.read_bytes()
    outcome = fix(repo, "a.js")
    assert isinstance(outcome, Skipped) and outcome.reason == "parser-missing"
    assert path.read_bytes() == original


def test_missing_runtime_is_a_parser_missing_diagnostic(ts_repo, monkeypatch):
    put(ts_repo, "a.js", FIXED["outer_if_false_falls_through_var"])
    monkeypatch.setattr(lint_fix_else, "find_runtime", lambda: None)
    outcome = fix(ts_repo, "a.js")
    assert isinstance(outcome, Skipped) and outcome.reason == "parser-missing" and "node" in outcome.detail


def test_nearest_typescript_is_found_walking_up_and_stops_at_repo_root(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "node_modules" / "typescript").mkdir(parents=True)
    (repo / "node_modules" / "typescript" / "package.json").write_text("{}")
    put(repo, "deep/er/a.ts", "x\n")
    root = os.path.realpath(repo)
    assert lint_fix_else.find_typescript(root, "deep/er/a.ts") == os.path.join(root, "node_modules", "typescript")
    outer = tmp_path / "node_modules" / "typescript"  # above the repo root: must not be picked up
    outer.mkdir(parents=True)
    (outer / "package.json").write_text("{}")
    assert lint_fix_else.find_typescript(os.path.realpath(make_repo(tmp_path, "r2")), "a.ts") is None


SAME_LINE = ("export function hot(a) { if (a) { return 1 } else { a = 2 } return [1].map((x) => "
             "{ if (x) { return 1 } else { x = 3 } return x }) }\n")


def test_a_same_line_anonymous_closure_is_not_owned_by_a_differently_named_target(ts_repo):
    path = put(ts_repo, "a.ts", SAME_LINE)
    outcome = lint_fix_else.fix_file(os.path.realpath(ts_repo), "a.ts", [(1, "hot")])
    assert isinstance(outcome, Rewrite) and outcome.count == 1
    text = path.read_text()
    assert text.count("else") == 1 and "{ if (x) { return 1 } else { x = 3 } return x }" in text  # closure intact


def test_an_anonymous_function_is_rewritten_only_when_the_analyzer_flagged_it(ts_repo):
    src = "export const hot = (xs) => xs.map((x) => { if (x) { return 1 } else { return 2 } })\n"
    path = put(ts_repo, "a.ts", src)
    root = os.path.realpath(ts_repo)
    outcome = lint_fix_else.fix_file(root, "a.ts", [(1, "hot")])
    assert isinstance(outcome, Skipped) and outcome.reason == "no-fixable-pattern" and path.read_text() == src
    assert isinstance(lint_fix_else.fix_file(root, "a.ts", [(1, "<anonymous>")]), Rewrite)
    assert "else" not in path.read_text()


def test_two_functions_matching_one_target_are_both_left_alone(ts_repo):
    body = "{ if (a) { return 1 } else { return 2 } }"
    src = f"run(() => {body}, () => {body})\n"
    path = put(ts_repo, "a.ts", src)
    root = os.path.realpath(ts_repo)
    outcome = lint_fix_else.fix_file(root, "a.ts", [(1, "<anonymous>")])
    assert isinstance(outcome, Skipped) and outcome.reason == "no-fixable-pattern" and path.read_text() == src
    assert lint_fix_else.probe(root, "a.ts", [(1, "<anonymous>")]) == [0]


def commit_malicious_typescript(repo):
    marker = repo / "PWNED"
    put(repo, "node_modules/typescript/package.json", '{"name": "typescript", "main": "index.js"}')
    put(repo, "node_modules/typescript/index.js",
        f"require('fs').writeFileSync({str(marker)!r}, 'x'); module.exports = {{}};\n")
    put(repo, "a.ts", else_ifs(3, "hot"))
    commit_all(repo)
    return marker


def test_a_typescript_dir_committed_to_the_repo_is_never_loaded_by_fix(tmp_path):
    repo = make_repo(tmp_path)
    marker = commit_malicious_typescript(repo)
    root = os.path.realpath(repo)
    outcome = lint_fix_else.fix_file(root, "a.ts", [(1, "hot")])
    assert isinstance(outcome, Skipped) and outcome.reason == "parser-untrusted"
    assert lint_fix_else.probe(root, "a.ts", [(1, "hot")]) is None
    assert not marker.exists()


@pytest.mark.parametrize("fix_flag", [False, True])
def test_committed_typescript_never_runs_in_a_check_or_a_fix_run(tmp_path, monkeypatch, capsys, fix_flag):
    repo = make_repo(tmp_path)
    marker = commit_malicious_typescript(repo)
    fake = FakeAnalyzer(tmp_path / "bin", functions={"a.ts": [fn("hot", 99)]})
    use_analyzer(monkeypatch, fake.path)
    use_config(monkeypatch)
    code, doc = run_check(capsys, repo, fix=fix_flag)
    assert code == 1 and not marker.exists() and doc["fix"]["applied"] == []
    assert rules(doc)[0]["fixable"] is False


def test_a_plain_check_never_spawns_the_parser(ts_repo, tmp_path, monkeypatch, capsys):
    fake = FakeAnalyzer(tmp_path / "bin", functions={"a.ts": [fn("hot", 99)]})
    use_analyzer(monkeypatch, fake.path)
    use_config(monkeypatch)
    put(ts_repo, "a.ts", else_ifs(3, "hot"))

    def forbidden(*args, **kwargs):
        raise AssertionError("the read-only path must never run the repository's parser")

    monkeypatch.setattr(lint_fix_else, "run_parser", forbidden)
    monkeypatch.setattr(lint_fix_else, "probe", forbidden)
    code, doc = run_check(capsys, ts_repo)
    assert code == 1 and rules(doc)[0]["fixable"] is False


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not installed")
def test_bun_can_run_the_helper_too(ts_repo, monkeypatch):
    require_typescript()
    monkeypatch.setattr(lint_fix_else, "find_runtime", lambda: shutil.which("bun"))
    put(ts_repo, "a.js", FIXED["outer_if_false_falls_through_var"])
    assert isinstance(fix(ts_repo, "a.js"), Rewrite)


# ---- `rig lint check --fix` orchestration (real analyzer unless noted) -----------------------


def test_fix_lowers_cognitive_below_the_limit_and_reports_only_vanished_findings(ts_repo, monkeypatch, capsys):
    use_analyzer(monkeypatch, require_real_cccc())
    use_config(monkeypatch)
    put(ts_repo, "a.ts", else_ifs(8))  # cognitive 16 -> 8 once the elses go
    code, before = run_check(capsys, ts_repo)
    assert code == 1 and rules(before)[0]["value"] == 16 and rules(before)[0]["fixable"] is False  # parser only under --fix
    code, doc = run_check(capsys, ts_repo, fix=True)
    assert code == 0 and doc["findings"] == [] and doc["ok"] is True
    (applied,) = doc["fix"]["applied"]
    assert applied["rule"] == "fix/else-after-return" and applied["sites"] == 8
    assert applied["resolved"] == [{"rule": "complexity/cognitive", "function": "f"}]
    assert applied["parser"].startswith("typescript ")
    assert doc["fix"]["analyzer_rerun"] is True and doc["summary"]["fixed"] == 1
    assert doc["fix"]["reverted"] == [] and doc["fix"]["unresolved"] == []
    assert run_check(capsys, ts_repo, fix=True)[1]["fix"]["applied"] == []  # idempotent


def test_remaining_violation_is_never_claimed_fixed(ts_repo, monkeypatch, capsys):
    use_analyzer(monkeypatch, require_real_cccc())
    use_config(monkeypatch)
    put(ts_repo, "a.ts", else_ifs(19))  # cognitive 38 -> 19: still above 15 (cyclomatic 20 passes)
    code, doc = run_check(capsys, ts_repo, fix=True)
    assert code == 1
    (finding,) = rules(doc, "complexity/cognitive")
    assert finding["value"] == 19
    (applied,) = doc["fix"]["applied"]
    assert applied["resolved"] == [] and doc["summary"]["fixed"] == 0
    (unresolved,) = doc["fix"]["unresolved"]
    assert (unresolved["path"], unresolved["reason"], unresolved["value"]) == ("a.ts", "remaining-after-fix", 19)


def test_fix_run_reports_post_fix_state_and_preserves_mode(ts_repo, monkeypatch, capsys):
    use_analyzer(monkeypatch, require_real_cccc())
    use_config(monkeypatch)
    path = put(ts_repo, "tool.ts", else_ifs(8))
    path.chmod(0o755)
    commit_all(ts_repo)
    code, doc = run_check(capsys, ts_repo, fix=True)
    assert code == 0 and mode_of(path) == 0o755
    assert "else" not in path.read_text()
    assert git(ts_repo, "diff", "--cached", "--name-only") == ""  # never restages


def test_unfixable_extension_and_other_rules_stay_unresolved(tmp_path, monkeypatch, capsys):
    fake = FakeAnalyzer(tmp_path / "bin", functions={"a.py": [fn("hot", cognitive=99, cyclomatic=50)]})
    use_analyzer(monkeypatch, fake.path)
    use_config(monkeypatch)
    monkeypatch.setattr(lint_fix_else, "fix_file", lambda *a: pytest.fail("python has no fixer"))
    repo = make_repo(tmp_path)
    put(repo, "a.py", "x = 1\n")
    code, doc = run_check(capsys, repo, fix=True)
    assert code == 1 and doc["fix"]["applied"] == [] and doc["fix"]["analyzer_rerun"] is False
    assert {(u["rule"], u["reason"]) for u in doc["fix"]["unresolved"]} == {
        ("complexity/cognitive", "no-safe-fix"), ("complexity/cyclomatic", "no-safe-fix")}


def test_missing_parser_is_reported_through_the_run_and_nothing_is_written(tmp_path, monkeypatch, capsys):
    fake = FakeAnalyzer(tmp_path / "bin", functions={"a.ts": [fn("hot", cognitive=99)]})
    use_analyzer(monkeypatch, fake.path)
    use_config(monkeypatch)
    repo = make_repo(tmp_path)
    path = put(repo, "a.ts", else_ifs(3, "hot"))
    original = path.read_bytes()
    code, doc = run_check(capsys, repo, fix=True)
    assert code == 1 and doc["fix"]["applied"] == [] and doc["summary"]["fixed"] == 0
    assert doc["fix"]["unresolved"][0]["reason"].startswith("parser-missing")
    assert path.read_bytes() == original


def test_parse_error_file_stays_untouched_and_run_is_incomplete(ts_repo, monkeypatch, capsys):
    use_analyzer(monkeypatch, require_real_cccc())
    use_config(monkeypatch)
    path = put(ts_repo, "bad.ts", "function f(c) {\n  if (c) { return 1; } else { return 2; }\n")
    original = path.read_bytes()
    code, doc = run_check(capsys, ts_repo, fix=True)
    assert code == 3 and doc["fix"]["applied"] == []
    assert path.read_bytes() == original


@pytest.mark.parametrize(("label", "after"), [
    ("function-set", [fn("hot", 5), fn("extra", 1)]),
    ("renamed", [fn("other", 5)]),
    ("cognitive-up", [fn("hot", 200)]),
    ("cyclomatic", [fn("hot", 5, 9)]),
])
def test_rewrite_whose_analyzer_report_changes_unexpectedly_is_reverted(ts_repo, tmp_path, monkeypatch, capsys,
                                                                       label, after):
    fake = FakeAnalyzer(tmp_path / "bin", functions={"a.ts": [fn("hot", 99)]})
    use_analyzer(monkeypatch, fake.path)
    use_config(monkeypatch)
    path = put(ts_repo, "a.ts", else_ifs(3, "hot"))
    original = path.read_bytes()
    real_revert = lint_fix_else.revert_file

    def revert_then_restore_report(root, rewrite):
        problem = real_revert(root, rewrite)
        fake.configure(functions={"a.ts": [fn("hot", 99)]})  # the restored file reports as before
        return problem

    after_fix(monkeypatch, lambda: fake.configure(functions={"a.ts": after}))
    monkeypatch.setattr(lint_fix_else, "revert_file", revert_then_restore_report)
    code, doc = run_check(capsys, ts_repo, fix=True)
    assert path.read_bytes() == original
    assert doc["fix"]["applied"] == [] and doc["summary"]["fixed"] == 0
    (reverted,) = doc["fix"]["reverted"]
    assert reverted["path"] == "a.ts" and reverted["restored"] is True
    assert doc["fix"]["unresolved"][0]["reason"].startswith("reverted:")


def after_fix(monkeypatch, hook):
    """Run `hook` right after the real fixer has written a file (what the analyzer sees next)."""
    real = lint_fix_else.fix_file

    def wrapped(root, rel, targets):
        outcome = real(root, rel, targets)
        if isinstance(outcome, Rewrite):
            hook()
        return outcome

    monkeypatch.setattr(lint_fix_else, "fix_file", wrapped)


def report_after_fix(monkeypatch, fake, after):
    """Make the fake analyzer report `after` once the real fixer has written the file."""
    after_fix(monkeypatch, lambda: fake.configure(functions={"a.ts": after}))


def test_rewrite_that_resolves_nothing_is_reverted(ts_repo, tmp_path, monkeypatch, capsys):
    fake = FakeAnalyzer(tmp_path / "bin", functions={"a.ts": [fn("hot", 99)]})
    use_analyzer(monkeypatch, fake.path)
    use_config(monkeypatch)
    path = put(ts_repo, "a.ts", else_ifs(3, "hot"))
    original = path.read_bytes()
    code, doc = run_check(capsys, ts_repo, fix=True)
    assert code == 1 and path.read_bytes() == original
    assert doc["fix"]["applied"] == [] and doc["summary"]["fixed"] == 0
    (reverted,) = doc["fix"]["reverted"]
    assert reverted["restored"] is True and reverted["reason"].startswith("no-effect")


def test_partial_reduction_inside_a_flagged_function_is_kept_but_not_claimed_resolved(ts_repo, tmp_path,
                                                                                    monkeypatch, capsys):
    fake = FakeAnalyzer(tmp_path / "bin", functions={"a.ts": [fn("hot", 99)]})
    use_analyzer(monkeypatch, fake.path)
    use_config(monkeypatch)
    report_after_fix(monkeypatch, fake, [fn("hot", 40)])
    path = put(ts_repo, "a.ts", else_ifs(3, "hot"))
    code, doc = run_check(capsys, ts_repo, fix=True)
    assert code == 1 and "else" not in path.read_text()
    (applied,) = doc["fix"]["applied"]
    assert applied["resolved"] == [] and doc["summary"]["fixed"] == 0 and doc["fix"]["reverted"] == []
    assert doc["fix"]["unresolved"][0]["reason"] == "remaining-after-fix"


def test_fix_requires_the_complexity_check(tmp_path, monkeypatch, capsys):
    use_config(monkeypatch, complexity_enabled=False)
    repo = make_repo(tmp_path)
    code, doc = run_check(capsys, repo, fix=True)
    assert code == 2 and "complexity" in doc["error"]


def test_fix_never_writes_for_a_symlinked_directory_escape(ts_repo, tmp_path, monkeypatch, capsys):
    use_analyzer(monkeypatch, require_real_cccc())
    use_config(monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = put(outside, "t.ts", else_ifs(8))
    original = target.read_bytes()
    put(ts_repo, "real/t.ts", "x\n")
    commit_all(ts_repo)
    shutil.rmtree(ts_repo / "real")
    os.symlink(outside, ts_repo / "real")
    code, doc = run_check(capsys, ts_repo, fix=True)
    assert code == 0 and doc["fix"]["applied"] == []
    assert target.read_bytes() == original


def test_rewrite_that_worsens_the_file_lines_band_is_reverted(ts_repo, tmp_path, monkeypatch, capsys):
    # else -> bare block adds one line per site: 6 lines (at the limit) become 9 (a warning).
    fake = FakeAnalyzer(tmp_path / "bin", functions={"a.ts": [fn("hot", 99)]})
    use_analyzer(monkeypatch, fake.path)
    use_config(monkeypatch, warn_above=6, error_above=100)
    report_after_fix(monkeypatch, fake, [fn("hot", 5)])
    path = put(ts_repo, "a.ts", else_ifs(3, "hot"))
    original = path.read_bytes()
    code, doc = run_check(capsys, ts_repo, fix=True)
    assert path.read_bytes() == original and code in (0, 1)  # file restored byte-for-byte
    assert doc["fix"]["applied"] == [] and doc["summary"]["fixed"] == 0
    (reverted,) = doc["fix"]["reverted"]
    assert reverted["restored"] is True
    assert reverted["reason"].startswith("introduced-by-fix: new file-lines/max-lines warning")
    assert rules(doc, "file-lines/max-lines") == []  # the reverted file is back within its band


def test_rewrite_that_keeps_the_line_band_is_not_reverted(ts_repo, tmp_path, monkeypatch, capsys):
    fake = FakeAnalyzer(tmp_path / "bin", functions={"a.ts": [fn("hot", 99)]})
    use_analyzer(monkeypatch, fake.path)
    use_config(monkeypatch, warn_above=50, error_above=100)
    report_after_fix(monkeypatch, fake, [fn("hot", 5)])
    put(ts_repo, "a.ts", else_ifs(3, "hot"))
    code, doc = run_check(capsys, ts_repo, fix=True)
    assert doc["fix"]["reverted"] == [] and len(doc["fix"]["applied"]) == 1


def test_failed_revert_forces_incomplete_exit_3_with_a_reason(ts_repo, tmp_path, monkeypatch, capsys):
    fake = FakeAnalyzer(tmp_path / "bin", functions={"a.ts": [fn("hot", 99)]})
    use_analyzer(monkeypatch, fake.path)
    use_config(monkeypatch)
    put(ts_repo, "a.ts", else_ifs(3, "hot"))
    after_fix(monkeypatch, lambda: fake.configure(functions={"a.ts": [fn("other", 5)]}))
    monkeypatch.setattr(lint_fix_else, "revert_file", lambda root, rewrite: "disk on fire")
    code, doc = run_check(capsys, ts_repo, fix=True)
    assert code == 3 and doc["ok"] is False
    (reverted,) = doc["fix"]["reverted"]
    assert reverted["restored"] is False and reverted["detail"] == "disk on fire"
    assert ("a.ts", "fix", "revert-failed", "disk on fire") in {
        (i["path"], i["check"], i["reason"], i["detail"]) for i in doc["incomplete"]}
    assert doc["fix"]["applied"] == [] and doc["summary"]["fixed"] == 0


def test_the_parser_never_runs_in_the_repository_and_ignores_its_bunfig(ts_repo, monkeypatch):
    bun = shutil.which("bun")
    if bun is None:
        if os.environ.get("RIG_LINT_REQUIRE_REAL") == "1":
            pytest.fail("bun is required (RIG_LINT_REQUIRE_REAL=1)")
        pytest.skip("bun not installed")
    marker = ts_repo / "PRELOADED"
    put(ts_repo, "preload.js", f"require('fs').writeFileSync({str(marker)!r}, 'x')\n")
    put(ts_repo, "bunfig.toml", 'preload = ["./preload.js"]\n')
    put(ts_repo, "a.js", FIXED["outer_if_false_falls_through_var"])
    monkeypatch.setattr(lint_fix_else, "find_runtime", lambda: bun)
    assert isinstance(fix(ts_repo, "a.js"), Rewrite)
    assert not marker.exists()


def test_the_parser_cwd_is_a_private_directory_outside_the_repo(ts_repo, monkeypatch):
    seen = []
    real_run = subprocess.run

    def spy(argv, **kwargs):
        if "cwd" in kwargs:
            seen.append(kwargs["cwd"])
        return real_run(argv, **kwargs)

    monkeypatch.setattr(lint_fix_else.subprocess, "run", spy)
    put(ts_repo, "a.js", FIXED["outer_if_false_falls_through_var"])
    assert isinstance(fix(ts_repo, "a.js"), Rewrite)
    assert seen and all(not str(c).startswith(os.path.realpath(ts_repo)) for c in seen)
