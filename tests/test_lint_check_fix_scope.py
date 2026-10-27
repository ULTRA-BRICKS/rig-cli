"""`rig lint check --fix` scoping and safety: flagged functions only, never worse, pinned directory writes.

Covers: only the flagged function's sites are rewritten (unflagged and nested closures stay
byte-identical), `fixable` needs a qualifying site, a rewrite that adds ANY finding or grows an
existing size violation is reverted (`introduced-by-fix`), writes and reverts go through a pinned
directory fd (a directory swapped for a symlink mid-run cannot redirect them), an exception rolls
unverified rewrites back, and a fully staged file is reported `needs-restage` (never restaged).
Real cccc + a read-only TypeScript are required (RIG_LINT_REQUIRE_REAL=1 turns a gap into a failure).
"""
from __future__ import annotations

import os

import pytest

from riglib import lint_check, lint_fix_else
from riglib.lint_fix_else import Rewrite, Skipped
from tests.lint_check_helpers import (args_for, commit_all, else_ifs, git, link_typescript, make_repo, put,
                                      require_real_cccc, require_typescript, rules, run_check, use_analyzer,
                                      use_config)

SMALL = "function small(c: boolean): number {\n  if (c) {\n    return 1;\n  } else {\n    return 2;\n  }\n}\n"
HOT_AND_SMALL = SMALL + "\n" + else_ifs(8, "hot")
DECOY = "function decoy(c: boolean) {\n  if (c) {\n    return 1;\n  } else {\n    return 2;\n  }\n}\n"


@pytest.fixture
def ts_repo(tmp_path):
    require_typescript()
    repo = make_repo(tmp_path)
    link_typescript(repo)
    return repo


@pytest.fixture
def real(monkeypatch):
    use_analyzer(monkeypatch, require_real_cccc())
    use_config(monkeypatch)


def test_fix_touches_only_the_flagged_function_and_is_idempotent(ts_repo, real, capsys):
    path = put(ts_repo, "a.ts", HOT_AND_SMALL)
    code, before = run_check(capsys, ts_repo)
    assert code == 1 and [(f["function"], f["fixable"]) for f in rules(before)] == [("hot", False)]  # parser only under --fix
    code, doc = run_check(capsys, ts_repo, fix=True)
    assert code == 0 and doc["summary"]["fixed"] == 1
    text = path.read_text()
    assert text.startswith(SMALL + "\n")  # the unflagged function is byte-identical
    assert "else" not in text.split("function hot")[1]
    assert run_check(capsys, ts_repo, fix=True)[1]["fix"]["applied"] == []
    assert path.read_text() == text


def test_nested_unflagged_closure_inside_a_flagged_function_is_not_rewritten(ts_repo, real, capsys):
    nested = "  const inner = (c: boolean) => {\n    if (c) {\n      return 1;\n    } else {\n      return 2;\n    }\n  };\n"
    path = put(ts_repo, "a.ts", else_ifs(8, "hot").replace("  return x;\n", nested + "  return x + inner(true);\n"))
    code, doc = run_check(capsys, ts_repo, fix=True)
    assert code == 0 and doc["summary"]["fixed"] == 1
    assert nested in path.read_text()


def test_flagged_function_without_a_qualifying_site_is_not_fixable_and_the_file_is_untouched(ts_repo, real, capsys):
    hard = "function hard(x: number) {\n" + "  if (x) { x = 1; }\n" * 20 + "}\n"
    path = put(ts_repo, "a.ts", SMALL + "\n" + hard)
    original = path.read_bytes()
    code, doc = run_check(capsys, ts_repo, fix=True)
    assert code == 1 and path.read_bytes() == original
    assert [(f["function"], f["fixable"]) for f in rules(doc, "complexity/cognitive")] == [("hard", False)]
    assert doc["fix"]["applied"] == [] and doc["fix"]["reverted"] == []
    assert doc["fix"]["unresolved"][0]["reason"].startswith("no-fixable-pattern")


SHAPES = {
    "arrow_const": ("export const hot = (c: boolean) => {\n  if (c) {\n    return 1;\n  } else {\n    return 2;\n  }\n};\n", 1),
    "decorated_method": ("class A {\n  @dec()\n  hot(c: boolean) {\n    if (c) {\n      return 1;\n    } else {\n      return 2;\n    }\n  }\n}\n", 3),
    "object_method": ("const o = {\n  hot(c: boolean) {\n    if (c) {\n      return 1;\n    } else {\n      return 2;\n    }\n  },\n};\n", 2),
    "function_expression_property": ("const o = {\n  hot: function (c: boolean) {\n    if (c) {\n      return 1;\n    } else {\n      return 2;\n    }\n  },\n};\n", 2),
}


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_target_matching_selects_the_function_by_analyzer_line_and_name(ts_repo, name):
    source, line = SHAPES[name]
    root = os.path.realpath(ts_repo)
    path = put(ts_repo, "a.ts", source + DECOY)
    outcome = lint_fix_else.fix_file(root, "a.ts", [(line, "hot")])
    assert isinstance(outcome, Rewrite) and outcome.count == 1
    assert path.read_text().endswith(DECOY) and path.read_text() != source + DECOY
    wrong = lint_fix_else.fix_file(root, "a.ts", [(line, "other")])
    assert isinstance(wrong, Skipped) and wrong.reason == "no-fixable-pattern"


def test_fix_that_pushes_a_passing_file_over_the_line_limit_is_reverted_as_introduced(ts_repo, real, capsys):
    source = else_ifs(8, "hot") + "// pad\n" * (1200 - 11)  # exactly 1200 lines: passes; +8 lines would warn
    path = put(ts_repo, "a.ts", source)
    assert source.count("\n") == 1200
    code, doc = run_check(capsys, ts_repo, fix=True)
    assert path.read_text() == source and code == 1  # restored; the cognitive finding stays
    (reverted,) = doc["fix"]["reverted"]
    assert reverted["restored"] is True and reverted["reason"].startswith("introduced-by-fix: new file-lines/max-lines")
    assert doc["fix"]["applied"] == [] and rules(doc, "file-lines/max-lines") == []
    assert doc["fix"]["unresolved"][0]["reason"].startswith("reverted: introduced-by-fix")


def test_fix_never_grows_an_existing_file_lines_violation(ts_repo, real, capsys):
    source = else_ifs(8, "hot") + "// pad\n" * (1300 - 11)  # already a 1300-line warning
    path = put(ts_repo, "a.ts", source)
    code, doc = run_check(capsys, ts_repo, fix=True)
    assert path.read_text() == source
    (reverted,) = doc["fix"]["reverted"]
    assert reverted["reason"].startswith("file-lines violation grew")
    assert [f["value"] for f in rules(doc, "file-lines/max-lines")] == [1300]


def _swap_directory_on_temp_name(monkeypatch, repo, outside):
    real_name = lint_fix_else._temp_name

    def swap_then_name():
        os.rename(repo / "d", repo / "d.moved")
        os.symlink(outside, repo / "d")
        return real_name()

    monkeypatch.setattr(lint_fix_else, "_temp_name", swap_then_name)


def test_directory_swapped_for_a_symlink_mid_write_cannot_redirect_the_write(ts_repo, tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = put(outside, "x.ts", else_ifs(3))
    victim_bytes = victim.read_bytes()
    put(ts_repo, "d/x.ts", else_ifs(3))
    _swap_directory_on_temp_name(monkeypatch, ts_repo, outside)
    outcome = lint_fix_else.fix_file(os.path.realpath(ts_repo), "d/x.ts")
    assert isinstance(outcome, Rewrite)
    assert victim.read_bytes() == victim_bytes and os.listdir(outside) == ["x.ts"]
    assert "else" not in (ts_repo / "d.moved" / "x.ts").read_text()


def test_revert_also_goes_through_the_pinned_directory(ts_repo, tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = put(outside, "x.ts", "keep\n")
    put(ts_repo, "d/x.ts", else_ifs(3))
    root = os.path.realpath(ts_repo)
    outcome = lint_fix_else.fix_file(root, "d/x.ts")
    assert isinstance(outcome, Rewrite)
    _swap_directory_on_temp_name(monkeypatch, ts_repo, outside)
    # the swap happens after the pinned open of this very call, so the write lands in the moved dir
    assert lint_fix_else.revert_file(root, outcome) is None
    assert victim.read_text() == "keep\n" and os.listdir(outside) == ["x.ts"]
    assert (ts_repo / "d.moved" / "x.ts").read_bytes() == outcome.original
    # a later revert sees the symlink in the path and refuses
    assert lint_fix_else.revert_file(root, outcome) is not None and victim.read_text() == "keep\n"


def test_exception_after_the_write_rolls_every_unverified_rewrite_back(ts_repo, real, monkeypatch, capsys):
    path = put(ts_repo, "a.ts", else_ifs(8))
    original = path.read_bytes()
    first = lint_check.evaluate
    calls: list[int] = []

    def evaluate_then_die(scope, cfg, binary, **kwargs):
        calls.append(1)
        if len(calls) > 1:
            assert path.read_bytes() != original  # the rewrite really was on disk when the run died
            raise KeyboardInterrupt
        return first(scope, cfg, binary, **kwargs)

    monkeypatch.setattr(lint_check, "evaluate", evaluate_then_die)
    with pytest.raises(KeyboardInterrupt):
        run_check(capsys, ts_repo, fix=True)
    assert path.read_bytes() == original


def test_fully_staged_file_is_fixed_in_the_worktree_and_flagged_needs_restage(ts_repo, real, capsys):
    path = put(ts_repo, "a.ts", else_ifs(8))
    git(ts_repo, "add", "a.ts")
    staged = git(ts_repo, "diff", "--cached")
    code, doc = run_check(capsys, ts_repo, fix=True)
    assert code == 0 and "else" not in path.read_text()
    (note,) = doc["fix"]["notes"]
    assert (note["path"], note["state"]) == ("a.ts", "needs-restage")
    assert git(ts_repo, "diff", "--cached") == staged  # rig never restages


def test_text_output_names_the_needs_restage_state(ts_repo, real, capsys):
    put(ts_repo, "a.ts", else_ifs(8))
    git(ts_repo, "add", "a.ts")
    assert lint_check.run(args_for(ts_repo, fix=True, json_out=False)) == 0
    assert "a.ts: needs-restage" in capsys.readouterr().out


def test_unstaged_fix_has_no_restage_note(ts_repo, real, capsys):
    put(ts_repo, "a.ts", else_ifs(8))
    commit_all(ts_repo)
    put(ts_repo, "a.ts", else_ifs(9))
    assert run_check(capsys, ts_repo, fix=True)[1]["fix"]["notes"] == []
