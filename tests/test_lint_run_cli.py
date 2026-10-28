"""`rig lint` CLI surface: bare == run, flag-order permutations, backward compatibility of check/rules.

`rig lint check` internals are covered by tests/test_lint_check_*; here only the argv/exit/JSON
contract of the front-end is pinned (identical output with and without the new parent flags).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from riglib import lint_run
from riglib.cli import build_parser, main
from tests.lint_check_helpers import commit_all, make_repo, put
from tests.lint_run_helpers import write_exe


class Captured:
    def __init__(self) -> None:
        self.opts: list[lint_run.RunOptions] = []

    def __call__(self, opts, **_):
        self.opts.append(opts)
        return lint_run.RunReport(0, {"schema": "rig-lint-run/v1", "ok": True, "exit_code": 0}, "ok")


@pytest.fixture
def captured(monkeypatch):
    seen = Captured()
    monkeypatch.setattr(lint_run, "execute", seen)
    return seen


def test_help_lists_run_check_and_rules(capsys):
    with pytest.raises(SystemExit) as stop:
        main(["lint", "--help"])
    assert stop.value.code == 0
    out = capsys.readouterr().out
    for action in ("run", "check", "rules"):
        assert action in out
    assert "127" in out and "--no-setup" in out and "--plan" in out


def test_bare_lint_routes_to_run_with_default_options(captured, tmp_path):
    assert main(["lint"]) == 0
    assert main(["lint", "run"]) == 0
    bare, explicit = captured.opts
    assert bare == explicit
    assert (bare.cwd, bare.json, bare.plan, bare.no_setup, bare.fix, bare.ci) == (".", False, False, False, False, None)
    assert (bare.only, bare.skip, bare.scopes, bare.timeout, bare.config) == ((), (), (), None, None)


@pytest.mark.parametrize("argv", [
    ["lint", "--json"], ["lint", "run", "--json"], ["lint", "--json", "run"], ["lint", "run", "--json", "--json"],
])
def test_json_flag_survives_every_position(captured, argv):
    main(argv)
    assert captured.opts[-1].json is True


@pytest.mark.parametrize("argv", [
    ["lint", "-C", "d", "run"], ["lint", "run", "-C", "d"], ["lint", "-C", "d"], ["lint", "--cwd=d", "run"],
])
def test_cwd_survives_every_position(captured, argv):
    main(argv)
    assert captured.opts[-1].cwd == "d"


def test_all_run_flags_map_to_options(captured):
    main(["lint", "run", "-C", "d", "--config", "c.yaml", "--json", "--plan", "--no-setup", "--ci",
          "--only", "lint,format", "--only", "scripts", "--skip", "limits", "--scope", "a", "--scope", "b",
          "--timeout", "9"])
    o = captured.opts[0]
    assert o == lint_run.RunOptions(cwd="d", config="c.yaml", json=True, plan=True, no_setup=True, fix=False, ci=True,
                                    only=("lint", "format", "scripts"), skip=("limits",), scopes=("a", "b"),
                                    timeout=9)


def test_fix_flag_before_and_after_run(captured):
    main(["lint", "--fix"])
    main(["lint", "run", "--fix"])
    assert [o.fix for o in captured.opts] == [True, True]


@pytest.mark.parametrize("argv", [["lint", "--timeout", "abc"], ["lint", "run", "--bogus"], ["lint", "frobnicate"]])
def test_argparse_usage_errors_exit_2(argv, captured, capsys):
    with pytest.raises(SystemExit) as stop:
        main(argv)
    assert stop.value.code == 2 and captured.opts == []
    assert "usage" in capsys.readouterr().err.lower()


def test_run_never_raises_an_internal_error_reads_as_incomplete(monkeypatch, capsys):
    def boom(opts, **_):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(lint_run, "execute", boom)
    assert main(["lint", "--json"]) == 3
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    assert doc["exit_code"] == 3 and doc["ok"] is False and "kaboom" in doc["error"]
    assert "kaboom" in captured.err


# ── real runs through main() ─────────────────────────────────────────────────────

def test_not_a_git_repo_exits_2_with_json_error_doc_on_stdout_and_message_on_stderr(tmp_path, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert main(["lint", "run", "-C", str(plain), "--json"]) == 2
    out = capsys.readouterr()
    doc = json.loads(out.out)
    assert (doc["schema"], doc["ok"], doc["exit_code"]) == ("rig-lint-run/v1", False, 2)
    assert "git" in doc["error"] and doc["error"] in out.err


def test_text_mode_error_goes_to_stderr_only(tmp_path, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert main(["lint", "-C", str(plain)]) == 2
    out = capsys.readouterr()
    assert out.out == "" and out.err.startswith("error:")


def test_invalid_only_category_is_exit_2_json_error(tmp_path, capsys):
    repo = make_repo(tmp_path)
    assert main(["lint", "-C", str(repo), "--only", "nonsense", "--json"]) == 2
    assert "--only" in json.loads(capsys.readouterr().out)["error"]


def test_end_to_end_mixed_repo_reports_passed_failed_and_missing_tool(tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path)
    put(repo, "package.json", json.dumps({"devDependencies": {"eslint": "9.0.0", "prettier": "3.0.0"}}))
    put(repo, "eslint.config.js", "export default [];\n")
    put(repo, ".prettierrc", "{}\n")
    put(repo, ".gitignore", "node_modules\n")
    put(repo, "run.sh", "#!/bin/sh\necho hi\n")
    write_exe(repo / "node_modules" / ".bin" / "eslint", "print('1:1 error no-undef'); raise SystemExit(1)\n")
    write_exe(repo / "node_modules" / ".bin" / "prettier", "raise SystemExit(0)\n")
    commit_all(repo)
    git_dir = tmp_path / "gitonly"
    git_dir.mkdir()
    (git_dir / "git").symlink_to(shutil.which("git"))
    monkeypatch.setenv("PATH", str(git_dir))  # git only: no shellcheck, no package manager, no global linters
    rc = main(["lint", "-C", str(repo), "--json", "--no-setup", "--only", "lint,format"])
    doc = json.loads(capsys.readouterr().out)
    states = {c["id"]: c["state"] for c in doc["checks"]}
    assert states["eslint@."] == "failed" and states["prettier@."] == "passed"
    assert states["shellcheck@."] == "missing-tool"
    assert rc == doc["exit_code"] == 127 and doc["ok"] is False
    eslint = next(c for c in doc["checks"] if c["id"] == "eslint@.")
    assert eslint["exit_code"] == 1 and "no-undef" in eslint["output"]["stdout"]


def test_text_output_for_a_real_run_ends_with_summary_and_result(tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path)
    put(repo, "a.kt", "fun main() {}\n")
    commit_all(repo)
    git_dir = tmp_path / "gitonly"
    git_dir.mkdir()
    (git_dir / "git").symlink_to(shutil.which("git"))
    monkeypatch.setenv("PATH", str(git_dir))
    # Kotlin has no provider: nothing was verified, so this is exit 127, not a quiet 0
    assert main(["lint", "-C", str(repo), "--no-setup", "--skip", "limits"]) == 127
    lines = capsys.readouterr().out.rstrip().splitlines()
    assert any("kotlin" in line for line in lines)
    assert lines[-2].startswith("summary:") and lines[-1].startswith("result: INCOMPLETE")


def test_python_dash_m_entry_point_matches_main(tmp_path):
    import subprocess
    plain = tmp_path / "plain"
    plain.mkdir()
    root = str(Path(lint_run.__file__).resolve().parent.parent)
    proc = subprocess.run([sys.executable, "-m", "riglib", "lint", "run", "-C", str(plain), "--json"],
                          capture_output=True, text=True, env={**os.environ, "PYTHONPATH": root})
    assert proc.returncode == 2 and json.loads(proc.stdout)["exit_code"] == 2


# ── `rig lint check` / `rig lint rules` stay as they were ────────────────────────

def _disabled_check_repo(tmp_path):
    repo = make_repo(tmp_path)
    put(repo, "rig.yaml", "version: 1\nlint_checks:\n  enabled: false\n")
    put(repo, "a.py", "x = 1\n")
    commit_all(repo)
    return repo


def test_check_parser_keeps_its_flags_and_defaults():
    args = build_parser().parse_args(["lint", "check", "src", "--staged", "--json", "-C", "d", "--config", "c"])
    assert (args.lint_command, args.paths, args.staged, args.json, args.cwd, args.config, args.fix) == (
        "check", ["src"], True, True, "d", "c", False)
    bare = build_parser().parse_args(["lint", "check"])
    assert (bare.paths, bare.staged, bare.json, bare.cwd, bare.config, bare.fix) == ([], False, False, ".", None, False)


def test_check_json_is_identical_with_flags_before_or_after_the_action(tmp_path, capsys):
    repo = _disabled_check_repo(tmp_path)
    assert main(["lint", "check", "--json", "-C", str(repo)]) == 0
    reference = capsys.readouterr().out
    assert json.loads(reference)["schema"] == "rig-lint-check/v1"
    assert main(["lint", "--json", "-C", str(repo), "check"]) == 0
    assert capsys.readouterr().out == reference


def test_check_exit_codes_unchanged_usage_error_for_staged_with_fix(tmp_path, capsys):
    repo = _disabled_check_repo(tmp_path)
    assert main(["lint", "check", "--staged", "--fix", "--json", "-C", str(repo)]) == 2
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema"] == "rig-lint-check/v1" and doc["exit_code"] == 2


def test_check_not_a_repo_is_still_exit_2(tmp_path, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert main(["lint", "check", "-C", str(plain)]) == 2


def test_rules_json_carries_active_and_inactive_reason(tmp_path, capsys):
    repo = make_repo(tmp_path)
    assert main(["lint", "rules", "--json", "-C", str(repo)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data and all({"active", "inactive_reason", "rule", "effective", "provider"} <= set(r) for r in data)
    assert all(r["inactive_reason"] == "" for r in data if r["active"])
    assert all(r["inactive_reason"] for r in data if not r["active"])


def test_rules_honors_cwd_given_before_the_action(tmp_path, capsys):
    repo = make_repo(tmp_path)
    put(repo, "rig.yaml", "version: 1\nlinters:\n  rules:\n    all: true\n")
    assert main(["lint", "-C", str(repo), "--json", "rules", "anti-slop/no-optional-function-parameters"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["reason"] == "all=true"


def test_rules_unknown_rule_still_exits_4(tmp_path, capsys):
    repo = make_repo(tmp_path)
    assert main(["lint", "rules", "no/such-rule", "-C", str(repo)]) == 4


@pytest.mark.parametrize("argv", [
    ["lint", "--plan", "--fix", "check"], ["lint", "--ci", "--fix", "check"], ["lint", "--scope", "pkg", "check"],
    ["lint", "--only", "lint", "rules"], ["lint", "--timeout", "5", "check"],
])
def test_run_only_options_before_check_or_rules_are_a_usage_error(argv, capsys):
    assert main(argv) == 2
    assert "only applies to `rig lint run`" in capsys.readouterr().err
