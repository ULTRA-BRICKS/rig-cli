"""Worktree GC PR lookup routes a canonical Forgejo repo through Forgejo, never ``gh``."""

from __future__ import annotations

import subprocess
from pathlib import Path

from riglib import forgejo_prs, worktree_gc


def _repo_with_origin(path: Path, url: str) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", url], cwd=path, check=True)
    return path


def _boom_gh(monkeypatch):
    real = subprocess.run

    def run(cmd, *a, **kw):
        assert cmd[0] != "gh", "gh must not be called for a Forgejo repo"
        return real(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", run)


def test_forgejo_origin_uses_forgejo_pulls_not_gh(tmp_path, monkeypatch):
    repo = _repo_with_origin(tmp_path / "r", "https://git.hyperide.ai/ultrabricks/rig-cli.git")
    _boom_gh(monkeypatch)
    seen = {}

    def fake_fetch(target, *, state, limit, http_get=None):
        seen.update(target=target, state=state)
        return [
            {"number": 7, "state": "MERGED", "headRefName": "feat/x", "mergedAt": "2026-09-01T00:00:00Z",
             "closedAt": "2026-09-01T00:00:00Z", "url": "u", "isCrossRepository": False},
        ]

    monkeypatch.setattr(forgejo_prs, "fetch_pulls", fake_fetch)
    lookup = worktree_gc.make_default_pr_lookup(repo)
    info = lookup("feat/x")
    assert info is not None and info.number == 7 and info.state == "MERGED"
    assert seen["state"] == "all" and seen["target"].owner == "ultrabricks"


def test_forgejo_lookup_failure_degrades_to_no_pr_loudly(tmp_path, monkeypatch, capsys):
    repo = _repo_with_origin(tmp_path / "r", "git@git.hyperide.ai:ultrabricks/rig-cli.git")
    _boom_gh(monkeypatch)

    def fail(*a, **kw):
        raise forgejo_prs.ForgeLookupError("HTTP 404")

    monkeypatch.setattr(forgejo_prs, "fetch_pulls", fail)
    assert worktree_gc.make_default_pr_lookup(repo)("x") is None
    err = capsys.readouterr().err
    assert "worktree-gc: warning" in err and "HTTP 404" in err


def test_github_origin_still_uses_gh(tmp_path, monkeypatch):
    repo = _repo_with_origin(tmp_path / "r", "https://github.com/alex-mextner/rig-cli.git")
    real = subprocess.run
    called = []

    def run(cmd, *a, **kw):
        if cmd[0] == "gh":
            called.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
        return real(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(forgejo_prs, "fetch_pulls", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no")))
    worktree_gc.make_default_pr_lookup(repo)
    assert called and called[0][:3] == ["gh", "pr", "list"]
