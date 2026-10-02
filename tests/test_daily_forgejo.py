"""``rig daily`` reads merged PRs from Forgejo for a host-prefixed repo spec, never via ``gh``."""

from __future__ import annotations

import pytest

from riglib import forgejo_prs
from riglib.daily import github as daily_github
from riglib.daily.timeutil import parse_utc

SPEC = "git.hyperide.ai/ultrabricks/rig-cli"


def _rec(number, state, merged_at):
    return {"number": number, "state": state, "headRefName": "b", "mergedAt": merged_at, "closedAt": merged_at,
            "url": f"https://git.hyperide.ai/ultrabricks/rig-cli/pulls/{number}", "title": f"t{number}",
            "body": "body", "labels": [{"name": "feat"}], "isCrossRepository": False}


def _no_gh(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("gh must not be called for a Forgejo repo")

    monkeypatch.setattr(daily_github.subprocess, "run", boom)


def test_forgejo_spec_reads_merged_prs_from_forgejo(monkeypatch):
    _no_gh(monkeypatch)
    seen = {}

    def fake_fetch(target, *, state, limit, http_get=None):
        seen["state"] = state
        return [_rec(1, "MERGED", "2026-09-20T10:00:00Z"), _rec(2, "CLOSED", None), _rec(3, "MERGED", "2026-09-01T00:00:00Z")]

    monkeypatch.setattr(forgejo_prs, "fetch_pulls", fake_fetch)
    result = daily_github.fetch_merged_prs(SPEC, parse_utc("2026-09-10T00:00:00Z"))
    assert [p.number for p in result.prs] == [1]
    assert result.prs[0].repo == SPEC and result.prs[0].labels == ["feat"]
    assert seen["state"] == "closed" and result.complete is True


def test_forgejo_full_page_of_closed_prs_marks_fetch_incomplete(monkeypatch):
    _no_gh(monkeypatch)
    page = [_rec(i, "CLOSED", None) for i in range(daily_github._PAGE_LIMIT)]
    monkeypatch.setattr(forgejo_prs, "fetch_pulls", lambda *a, **k: page)
    result = daily_github.fetch_merged_prs(SPEC, parse_utc("2026-09-10T00:00:00Z"))
    assert result.complete is False


def test_forgejo_lookup_error_surfaces_as_gh_error(monkeypatch):
    _no_gh(monkeypatch)

    def fail(*a, **k):
        raise forgejo_prs.ForgeLookupError("HTTP 404")

    monkeypatch.setattr(forgejo_prs, "fetch_pulls", fail)
    with pytest.raises(daily_github.GhError, match="HTTP 404"):
        daily_github.fetch_merged_prs(SPEC, parse_utc("2026-09-10T00:00:00Z"))
