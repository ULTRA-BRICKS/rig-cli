"""Only an explicit `--fix` may load the target repo's TypeScript module; plain and `--staged` scans never do."""
from __future__ import annotations

import pytest

from riglib import lint_fix_else
from tests.lint_check_helpers import (else_ifs, git, link_typescript, make_repo, put, require_real_cccc,
                                      require_typescript, rules, run_check, use_analyzer, use_config)


@pytest.fixture
def real(monkeypatch):
    use_analyzer(monkeypatch, require_real_cccc())
    use_config(monkeypatch)


def _forbid_parser(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("repo parser executed by a read-only scan")
    monkeypatch.setattr(lint_fix_else, "probe", boom)
    monkeypatch.setattr(lint_fix_else, "run_parser", boom)


def test_plain_scan_never_runs_the_parser(tmp_path, monkeypatch, real, capsys):
    repo = make_repo(tmp_path)
    put(repo, "a.js", else_ifs(8))
    _forbid_parser(monkeypatch)
    code, doc = run_check(capsys, repo)
    assert code == 1 and [f["fixable"] for f in rules(doc, "complexity/cognitive")] == [False]


def test_staged_scan_never_runs_the_parser(tmp_path, monkeypatch, real, capsys):
    repo = make_repo(tmp_path)
    put(repo, "a.js", else_ifs(8))
    git(repo, "add", "a.js")
    _forbid_parser(monkeypatch)
    code, doc = run_check(capsys, repo, staged=True)
    assert code == 1 and [f["fixable"] for f in rules(doc, "complexity/cognitive")] == [False]


def test_explicit_fix_still_rewrites_and_is_idempotent(tmp_path, real, capsys):
    repo = make_repo(tmp_path)
    link_typescript(repo)
    path = put(repo, "a.js", else_ifs(8))
    code, doc = run_check(capsys, repo, fix=True)
    assert code == 0 and doc["summary"]["fixed"] == 1
    text = path.read_text()
    assert "else" not in text
    assert run_check(capsys, repo, fix=True)[1]["fix"]["applied"] == []
    assert path.read_text() == text


def _sentinel_typescript(repo, marker):
    """A private `node_modules/typescript` shim that records its load, then delegates to the real compiler."""
    real_dir = require_typescript()
    shim = repo / "node_modules" / "typescript"
    shim.mkdir(parents=True)
    (shim / "package.json").write_text('{"name":"typescript","version":"0.0.0","main":"index.js"}')
    (shim / "index.js").write_text(
        f"require('fs').appendFileSync({str(marker)!r}, 'loaded\\n');\nmodule.exports = require({real_dir!r});\n")
    (repo / ".gitignore").write_text("node_modules/\n")


def test_sentinel_parser_loads_only_under_fix(tmp_path, real, capsys):
    repo = make_repo(tmp_path)
    marker = tmp_path / "parser-loaded.marker"
    _sentinel_typescript(repo, marker)
    put(repo, "a.js", else_ifs(8))
    git(repo, "add", "a.js")
    assert run_check(capsys, repo)[0] == 1 and not marker.exists()
    assert run_check(capsys, repo, staged=True)[0] == 1 and not marker.exists()
    code, doc = run_check(capsys, repo, fix=True)
    assert code == 0 and doc["summary"]["fixed"] == 1 and marker.exists()
