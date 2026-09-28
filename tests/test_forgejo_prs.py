"""Forgejo pull-request listing — the forge-generic seam behind worktree GC and ``rig daily``.

``fj`` has no machine-readable output, so canonical Forgejo repos are read through the Forgejo
REST API and normalized to the SAME ``gh pr list --json`` record shape the existing GitHub code
already parses. See :mod:`riglib.forgejo_prs`.
"""

from __future__ import annotations

import json

import pytest

from riglib import forgejo_prs
from riglib.forgejo_prs import ForgeLookupError, ForgejoRepo

T = ForgejoRepo(origin="https://git.hyperide.ai", owner="ultrabricks", name="rig-cli")


@pytest.mark.parametrize(
    "url",
    [
        "https://git.hyperide.ai/ultrabricks/rig-cli.git",
        "https://user@git.hyperide.ai/ultrabricks/rig-cli",
        "ssh://git@git.hyperide.ai/ultrabricks/rig-cli.git",
        "git@git.hyperide.ai:ultrabricks/rig-cli.git",
    ],
)
def test_forgejo_repo_from_canonical_remote(url):
    assert forgejo_prs.forgejo_repo_from_remote(url) == T


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/alex-mextner/rig-cli.git",
        "git@github.com:alex-mextner/rig-cli.git",
        "https://git.hyperide.ai.evil.example/ultrabricks/rig-cli",
        "/local/path/rig-cli",
        "",
    ],
)
def test_non_forgejo_remote_is_none(url):
    assert forgejo_prs.forgejo_repo_from_remote(url) is None


def test_forgejo_url_env_adds_a_trusted_host(monkeypatch):
    monkeypatch.setenv("FORGEJO_URL", "https://forge.example.org")
    got = forgejo_prs.forgejo_repo_from_remote("git@forge.example.org:o/r.git")
    assert got == ForgejoRepo("https://forge.example.org", "o", "r")


def test_parse_repo_spec_host_prefixed_is_forgejo_two_segment_is_not():
    assert forgejo_prs.forgejo_repo_from_spec("git.hyperide.ai/ultrabricks/rig-cli") == T
    assert forgejo_prs.forgejo_repo_from_spec("hyperide/hyper-saas") is None
    assert forgejo_prs.forgejo_repo_from_spec("github.com/a/b") is None


def _pull(number, *, ref="feat", state="closed", merged=False, head_repo="ultrabricks/rig-cli"):
    return {
        "number": number,
        "state": state,
        "merged": merged,
        "merged_at": "2026-09-20T10:00:00Z" if merged else None,
        "closed_at": "2026-09-20T10:00:00Z" if state == "closed" else None,
        "html_url": f"https://git.hyperide.ai/ultrabricks/rig-cli/pulls/{number}",
        "title": f"t{number}",
        "body": "b",
        "labels": [{"name": "x"}],
        "head": {"ref": ref, "repo": {"full_name": head_repo} if head_repo else None},
        "base": {"repo": {"full_name": "ultrabricks/rig-cli"}},
    }


def test_normalize_maps_state_and_cross_repo():
    merged = forgejo_prs.normalize_pull(_pull(1, merged=True))
    assert merged["state"] == "MERGED" and merged["mergedAt"] == "2026-09-20T10:00:00Z"
    assert merged["headRefName"] == "feat" and merged["isCrossRepository"] is False
    assert merged["url"].endswith("/pulls/1") and merged["labels"] == [{"name": "x"}]
    assert forgejo_prs.normalize_pull(_pull(2, state="open"))["state"] == "OPEN"
    assert forgejo_prs.normalize_pull(_pull(3))["state"] == "CLOSED"
    assert forgejo_prs.normalize_pull(_pull(4, head_repo="fork/x"))["isCrossRepository"] is True
    assert forgejo_prs.normalize_pull(_pull(5, head_repo=None))["isCrossRepository"] is True  # deleted fork


def _fake_get(pages, calls):
    def get(url, headers, timeout):
        calls.append((url, headers))
        page = int(url.split("page=")[1].split("&")[0])
        return 200, json.dumps(pages.get(page, [])), url

    return get


def test_fetch_pulls_paginates_until_empty_page_and_stops_at_limit():
    calls: list = []
    pages = {1: [_pull(i) for i in range(50)], 2: [_pull(100)]}
    got = forgejo_prs.fetch_pulls(T, state="all", limit=500, http_get=_fake_get(pages, calls))
    assert len(got) == 51 and len(calls) == 3
    assert "/api/v1/repos/ultrabricks/rig-cli/pulls?" in calls[0][0] and "state=all" in calls[0][0]
    capped = forgejo_prs.fetch_pulls(T, state="all", limit=10, http_get=_fake_get(pages, []))
    assert len(capped) == 10


def test_fetch_pulls_sends_token_only_for_matching_forgejo_url(monkeypatch):
    monkeypatch.setattr(forgejo_prs, "_fj_store_authorization", lambda _origin: None)
    monkeypatch.setenv("FORGEJO_TOKEN", "sekrit")
    monkeypatch.setenv("FORGEJO_URL", "https://other.example")
    calls: list = []
    forgejo_prs.fetch_pulls(T, state="all", limit=5, http_get=_fake_get({}, calls))
    assert "Authorization" not in calls[0][1]
    monkeypatch.setenv("FORGEJO_URL", "https://git.hyperide.ai")
    calls.clear()
    forgejo_prs.fetch_pulls(T, state="all", limit=5, http_get=_fake_get({}, calls))
    assert calls[0][1]["Authorization"] == "token sekrit"


@pytest.mark.parametrize(
    "resp",
    [(404, "{}", "u"), (200, "not json", "u"), (200, "{}", "u"), (200, "[]", "https://evil.example/x")],
)
def test_fetch_pulls_raises_lookup_error_on_untrustworthy_answer(resp):
    def get(url, headers, timeout):
        return resp[0], resp[1], resp[2] if resp[2] != "u" else url

    with pytest.raises(ForgeLookupError):
        forgejo_prs.fetch_pulls(T, state="all", limit=5, http_get=get)


def test_fetch_pulls_uses_fj_key_store_token_for_exact_host(tmp_path, monkeypatch):
    monkeypatch.delenv("FORGEJO_TOKEN", raising=False)
    monkeypatch.delenv("FORGEJO_URL", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / ".local" / "share"))
    keyfile = tmp_path / "Library" / "Application Support" / "forgejo-cli.forgejo-cli" / "keys.json"
    keyfile.parent.mkdir(parents=True)
    keyfile.write_text(
        json.dumps({"hosts": {"git.hyperide.ai": {"type": "Application", "token": "fj-secret"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(forgejo_prs.shutil, "which", lambda _name: None)
    calls = []
    forgejo_prs.fetch_pulls(T, state="all", limit=5, http_get=_fake_get({}, calls))
    assert calls[0][1]["Authorization"] == "token fj-secret"


def test_fj_key_store_never_reuses_another_hosts_token(tmp_path, monkeypatch):
    monkeypatch.delenv("FORGEJO_TOKEN", raising=False)
    monkeypatch.delenv("FORGEJO_URL", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / ".local" / "share"))
    keyfile = tmp_path / "Library" / "Application Support" / "forgejo-cli.forgejo-cli" / "keys.json"
    keyfile.parent.mkdir(parents=True)
    keyfile.write_text(
        json.dumps({"hosts": {"other.example": {"type": "Application", "token": "wrong-secret"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(forgejo_prs.shutil, "which", lambda _name: None)
    calls = []
    forgejo_prs.fetch_pulls(T, state="all", limit=5, http_get=_fake_get({}, calls))
    assert "Authorization" not in calls[0][1]
