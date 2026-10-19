"""The forge PR records the removal verdict proves a squash/rebase landing from: head oid, base
branch and merge commit — read from the SAME gh / Forgejo JSON gc already fetches."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from riglib import forgejo_prs, removal_verdict as rv, worktree_gc

HEAD = "a" * 40
MERGE = "b" * 40


def _forgejo_pull(**over) -> dict:
    pull = {
        "number": 7,
        "state": "closed",
        "merged": True,
        "merged_at": "2026-10-01T10:00:00Z",
        "closed_at": "2026-10-01T10:00:00Z",
        "html_url": "https://forge/pr/7",
        "title": "t",
        "body": "",
        "labels": [],
        "merge_commit_sha": MERGE,
        "head": {"ref": "feat", "sha": HEAD, "repo": {"full_name": "o/r"}},
        "base": {"ref": "main", "repo": {"full_name": "o/r"}},
    }
    pull.update(over)
    return pull


def test_forgejo_normalize_carries_head_oid_base_and_merge_commit():
    rec = forgejo_prs.normalize_pull(_forgejo_pull())
    assert rec["headRefOid"] == HEAD
    assert rec["baseRefName"] == "main"
    assert rec["mergeCommit"] == {"oid": MERGE}


def test_forgejo_normalize_open_pull_has_no_merge_commit():
    rec = forgejo_prs.normalize_pull(_forgejo_pull(merged=False, state="open", merge_commit_sha=None))
    assert rec["mergeCommit"] is None


def test_pr_records_from_gh_shape():
    raw = [
        {
            "number": 1, "state": "MERGED", "headRefName": "feat", "headRefOid": HEAD,
            "baseRefName": "main", "mergeCommit": {"oid": MERGE}, "isCrossRepository": False, "url": "u",
        },
        {"number": 2, "state": "OPEN", "headRefName": "x", "headRefOid": HEAD, "baseRefName": "main", "mergeCommit": None},
    ]
    recs = rv.pr_records_from_raw(raw)
    assert recs[0] == rv.PrRecord(1, "MERGED", "feat", HEAD, "main", MERGE, "u")
    assert recs[1].merge_commit_oid is None and recs[1].state == "OPEN"


def test_pr_records_drop_fork_prs_and_malformed_records():
    raw = [
        {"number": 1, "state": "MERGED", "headRefName": "feat", "isCrossRepository": True},
        {"number": 2, "state": "WEIRD", "headRefName": "feat"},
        {"number": "3", "state": "MERGED", "headRefName": "feat"},
        {"number": 4, "state": "MERGED", "headRefName": ""},
        None,
        "junk",
    ]
    assert rv.pr_records_from_raw(raw) == []


def test_default_forge_lookup_maps_gc_errors_to_forge_unavailable(monkeypatch, tmp_path):
    def boom(repo_root):
        raise worktree_gc.GhLookupError("`gh` CLI not found on PATH")

    monkeypatch.setattr(worktree_gc, "fetch_raw_prs", boom)
    with pytest.raises(rv.ForgeUnavailable, match="not found"):
        rv.default_forge_lookup(tmp_path)


def test_default_forge_lookup_reuses_gcs_raw_fetch(monkeypatch, tmp_path):
    raw = [{"number": 1, "state": "MERGED", "headRefName": "f", "headRefOid": HEAD, "baseRefName": "main", "mergeCommit": {"oid": MERGE}}]
    monkeypatch.setattr(worktree_gc, "fetch_raw_prs", lambda repo_root: raw)
    assert rv.default_forge_lookup(tmp_path)[0].merge_commit_oid == MERGE


def test_gc_gh_call_requests_the_oid_fields(monkeypatch, tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/o/r.git"], cwd=repo, check=True)
    seen: dict = {}
    real_run = subprocess.run

    def fake_run(cmd, **kw):
        if cmd[:2] == ["gh", "pr"]:
            seen["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps([]), stderr="")
        return real_run(cmd, **kw)

    monkeypatch.setattr(worktree_gc.subprocess, "run", fake_run)
    assert worktree_gc.fetch_raw_prs(repo) == []
    fields = seen["cmd"][seen["cmd"].index("--json") + 1].split(",")
    assert {"headRefOid", "baseRefName", "mergeCommit"} <= set(fields)


def test_gc_pr_index_is_unchanged_by_the_extra_fields():
    raw = [{"number": 1, "state": "MERGED", "headRefName": "f", "mergedAt": "2026-10-01T10:00:00Z", "headRefOid": HEAD}]
    assert worktree_gc._index_prs_by_branch(raw)["f"].number == 1
