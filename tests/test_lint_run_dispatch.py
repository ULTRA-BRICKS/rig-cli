"""Run-level flags written before `check`/`rules` must never become a `check --fix` rewrite.

argparse hands a parent-parser `--fix` to the `check` subcommand as the same attribute its own
`--fix` sets, while `--plan`/`--ci` (the flags that would have made it safe) are dropped. These tests
run the real CLI on a real temp repo holding a file the fixer WOULD rewrite and prove the refused forms
write nothing and never reach the check engine, while the legacy `rig lint check --fix` still reaches it
with the same arguments as before.
"""
from __future__ import annotations

import argparse

import pytest

from pathlib import Path

from riglib import lint_check, lint_check_launcher
from riglib.cli import build_parser, main
from tests.lint_check_helpers import commit_all, make_repo, put
from tests.lint_run_helpers import tree_digest

LAUNCHER = Path(lint_check_launcher.__file__).resolve()
# else-after-return: exactly what `rig lint check --fix` rewrites
FIXABLE = "function f(x) {\n  if (x) {\n    return 1;\n  } else {\n    return 2;\n  }\n}\n"


@pytest.fixture
def repo(tmp_path):
    repo = make_repo(tmp_path)
    put(repo, "src/a.ts", FIXABLE)
    commit_all(repo)
    return repo


@pytest.fixture
def check_calls(monkeypatch):
    """Records every call into the check engine instead of running it."""
    calls: list[argparse.Namespace] = []

    def fake(args):
        calls.append(args)
        return 0

    monkeypatch.setattr(lint_check, "run", fake)
    return calls


@pytest.mark.parametrize("flags", [
    ["--plan", "--fix"], ["--ci", "--fix"], ["--fix"], ["--no-setup", "--fix"], ["--only", "lint", "--fix"],
])
@pytest.mark.parametrize("action", ["check", "rules"])
def test_run_level_flags_before_the_action_are_refused_and_write_nothing(repo, check_calls, capsys, flags, action):
    before = tree_digest(repo)
    code = main(["lint", *flags, action, "-C", str(repo)])
    err = capsys.readouterr().err
    assert code == 2
    assert f"rig lint {action}:" in err and "only applies to `rig lint run`" in err
    assert check_calls == [], "the check engine must never be reached"
    assert tree_digest(repo) == before, "nothing may be written"


def test_the_refusal_names_every_misplaced_flag(repo, capsys):
    assert main(["lint", "--plan", "--fix", "--only", "lint", "check", "-C", str(repo)]) == 2
    err = capsys.readouterr().err
    assert "--fix" in err and "--plan" in err and "--only" in err


def test_a_run_level_fix_in_front_is_refused_even_when_check_also_has_its_own(repo, check_calls, capsys):
    assert main(["lint", "--fix", "check", "--fix", "-C", str(repo)]) == 2
    assert check_calls == []


@pytest.mark.parametrize("argv", [
    ["lint", "check", "--fix"], ["lint", "check", "--fix", "--json"], ["lint", "check", "--fix", "src/a.ts"],
    ["lint", "-C", ".", "check", "--fix"],
])
def test_legacy_check_fix_still_reaches_the_engine_with_fix_set(repo, check_calls, argv):
    argv = [str(repo) if part == "." else part for part in argv]
    if "src/a.ts" in argv:
        argv += ["-C", str(repo)]
    elif "-C" not in argv:
        argv += ["-C", str(repo)]
    assert main(argv) == 0
    (args,) = check_calls
    assert args.fix is True and args.lint_command == "check"
    assert getattr(args, "run_fix", False) is False, "check's own --fix is not the run-level flag"
    assert (args.plan, args.ci, args.no_setup, args.only, args.skip, args.scope, args.timeout) == (
        False, False, False, None, None, None, None)


def test_legacy_check_without_fix_is_unchanged(repo, check_calls):
    assert main(["lint", "check", "--staged", "-C", str(repo)]) == 0
    (args,) = check_calls
    assert args.fix is False and args.staged is True


@pytest.mark.parametrize("argv", [["lint", "--fix"], ["lint", "run", "--fix"], ["lint", "--fix", "run"]])
def test_run_level_fix_is_still_accepted_for_the_run_command(argv):
    args = build_parser().parse_args(argv)
    assert args.fix is True and args.run_fix is True and getattr(args, "lint_command", None) in (None, "run")


def test_ordinary_run_flags_before_run_are_untouched(repo):
    parsed = build_parser().parse_args(["lint", "--plan", "--json", "run", "-C", str(repo)])
    assert (parsed.plan, parsed.json, parsed.lint_command) == (True, True, "run")


@pytest.mark.parametrize("flags", [["--plan", "--fix"], ["--ci", "--fix"], ["--fix"]])
def test_real_subprocess_cli_refuses_and_leaves_the_tree_byte_identical(repo, flags):
    """No monkeypatching at all: the real entry point, a real process, a file the fixer would rewrite."""
    import subprocess
    import sys

    before = tree_digest(repo)
    proc = subprocess.run([sys.executable, str(LAUNCHER), "lint", *flags, "check", "-C", str(repo)],
                          capture_output=True, text=True, cwd=repo, check=False)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "only applies to `rig lint run`" in proc.stderr
    assert tree_digest(repo) == before


# ── a value that happens to be falsy is still "given" ────────────────────────────

@pytest.mark.parametrize("flags,named", [
    (["--timeout", "0"], "--timeout"), (["--only", ""], "--only"), (["--skip", ""], "--skip"),
    (["--scope", ""], "--scope"),
])
@pytest.mark.parametrize("action", ["check", "rules"])
def test_falsy_run_option_values_are_still_refused_before_check_or_rules(repo, check_calls, capsys, flags, named, action):
    before = tree_digest(repo)
    assert main(["lint", *flags, action, "-C", str(repo)]) == 2
    err = capsys.readouterr().err
    assert named in err and "only applies to `rig lint run`" in err
    assert check_calls == [] and tree_digest(repo) == before


def test_untouched_defaults_are_not_mistaken_for_given_options(repo, check_calls):
    assert main(["lint", "check", "-C", str(repo)]) == 0
    assert len(check_calls) == 1


def test_rules_turns_a_resolver_value_error_into_a_clean_exit_2(repo, monkeypatch, capsys):
    from riglib import lint_rules_cli

    def boom(cfg):
        raise ValueError("unknown lint rule(s) in linters.rules.enable: no/such")
    monkeypatch.setattr(lint_rules_cli, "resolve_rule_severities", boom)
    assert main(["lint", "rules", "-C", str(repo)]) == 2
    captured = capsys.readouterr()
    assert "unknown lint rule(s)" in captured.err and "Traceback" not in captured.err + captured.out
