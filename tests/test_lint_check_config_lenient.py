"""`rig lint check` survives a config error OUTSIDE `lint_checks`; an error inside it stays fatal.

The machine-wide commit hook runs `rig lint check --staged --json` on every commit, so an unrelated
config problem (a key a newer rig added, another block's typo) must not block commits.
"""
from __future__ import annotations

import pytest

from riglib import lint_check
from riglib.config import ConfigError, global_config_path
from tests.lint_check_helpers import FakeAnalyzer, make_repo, put, run_check, use_analyzer


def write_global(text: str) -> None:
    path = global_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_unrelated_unknown_key_warns_and_the_lint_block_still_applies(tmp_path, capsys):
    repo = make_repo(tmp_path)
    put(repo, "rig.yaml", "version: 1\nfuture_block: {x: 1}\nlint_checks:\n  complexity: {max_cognitive: 7}\n")
    cfg = lint_check.load_config(str(repo), None)
    assert cfg.max_cognitive == 7
    err = capsys.readouterr().err
    assert err.startswith("warning: ignoring a config error outside lint_checks:") and "future_block" in err


def test_lint_layers_still_cascade_when_another_block_is_broken(tmp_path, capsys):
    write_global("version: 1\nlint_checks:\n  complexity: {max_cognitive: 9, max_cyclomatic: 30}\n")
    repo = make_repo(tmp_path)
    put(repo, "rig.yaml", "version: 1\nfuture_block: 1\nlint_checks:\n  complexity: {max_cognitive: 7}\n")
    cfg = lint_check.load_config(str(repo), None)
    assert (cfg.max_cognitive, cfg.max_cyclomatic) == (7, 30)
    assert "future_block" in capsys.readouterr().err


def test_a_clean_config_prints_no_warning(tmp_path, capsys):
    repo = make_repo(tmp_path)
    put(repo, "rig.yaml", "version: 1\nlint_checks:\n  complexity: {max_cognitive: 7}\n")
    assert lint_check.load_config(str(repo), None).max_cognitive == 7
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("lint_block", [
    "lint_checks:\n  complexity: {max_cognitive: 0}\n",
    "lint_checks:\n  typo: true\n",
    "lint_checks:\n  file_lines: {warn_above: 900, error_above: 100}\n",
])
def test_an_error_inside_lint_checks_stays_fatal_even_with_an_unrelated_error(tmp_path, lint_block):
    repo = make_repo(tmp_path)
    put(repo, "rig.yaml", "version: 1\nfuture_block: 1\n" + lint_block)
    with pytest.raises(ConfigError):
        lint_check.load_config(str(repo), None)


def test_a_broken_global_layer_outside_lint_checks_does_not_block(tmp_path, capsys):
    write_global("version: 1\nnot_a_block: true\n")
    repo = make_repo(tmp_path)
    put(repo, "rig.yaml", "version: 1\nlint_checks:\n  file_lines: {warn_above: 10, error_above: 20}\n")
    cfg = lint_check.load_config(str(repo), None)
    assert (cfg.warn_above, cfg.error_above) == (10, 20)
    assert "not_a_block" in capsys.readouterr().err


def test_an_unreadable_yaml_layer_is_still_fatal(tmp_path):
    repo = make_repo(tmp_path)
    put(repo, "rig.yaml", "version: 1\nlint_checks: [unterminated\n")
    with pytest.raises(ConfigError):
        lint_check.load_config(str(repo), None)


def test_run_exits_zero_through_an_unrelated_config_error_and_two_through_a_lint_error(tmp_path, monkeypatch, capsys):
    use_analyzer(monkeypatch, FakeAnalyzer(tmp_path / "bin").path)
    repo = make_repo(tmp_path)
    put(repo, "rig.yaml", "version: 1\nfuture_block: 1\n")
    put(repo, "a.ts", "x\n")
    code, doc = run_check(capsys, repo)
    assert code == 0 and doc["ok"] is True
    put(repo, "rig.yaml", "version: 1\nfuture_block: 1\nlint_checks:\n  typo: true\n")
    code, doc = run_check(capsys, repo)
    assert code == 2 and "config error" in doc["error"]
