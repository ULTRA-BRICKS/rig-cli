"""ReportData schema completeness against the result-report skill's REPORT_DATA
contract (skills/universal/result-report/SKILL.md, "REPORT_DATA schema" -- as of
2026-09-07 (scaffold time) an UNMERGED agent-tools branch/PR, #440; re-diff this
schema against the merged SKILL.md once it lands, per riglib/report/model.py's module
docstring). Red-first: these pin the field set a later `rig report new`/`periodic`
implementation must populate."""

from __future__ import annotations

import dataclasses

from riglib.report.model import (
    Adr,
    Badge,
    ChangeEntry,
    Commit,
    Finding,
    FileRef,
    FileStat,
    Link,
    ProductChanges,
    ReportData,
    ReviewBadge,
    ReviewItem,
    ReviewRound,
    Spec,
    Stats,
)


def _minimal_report_data() -> ReportData:
    return ReportData(
        repo="https://github.com/o/r",
        head_sha="a" * 40,
        title="HYP-1: Title",
        subtitle="Short description",
        date="2026-09-07",
        badge=Badge(label="feat", type="feat"),
        branch="feature-branch",
    )


def test_top_level_fields_match_report_data_schema():
    # Every REPORT_DATA key from the skill doc (except headSha/productChanges/
    # reviewRounds, which render.py renames — see test_report_render.py) must exist
    # as a ReportData field.
    expected = {
        "repo",
        "head_sha",
        "title",
        "subtitle",
        "date",
        "badge",
        "branch",
        "links",
        "motivation",
        "stats",
        "product_changes",
        "files",
        "findings",
        "commits",
        "review_rounds",
        "adrs",
        "specs",
    }
    actual = {f.name for f in dataclasses.fields(ReportData)}
    assert actual == expected


def test_minimal_report_data_constructs_with_empty_collections():
    data = _minimal_report_data()
    assert data.links == []
    assert data.stats == Stats()
    assert data.product_changes == ProductChanges()
    assert data.files == []
    assert data.findings == []
    assert data.commits == []
    assert data.review_rounds == []
    assert data.adrs == []
    assert data.specs == []


def test_file_stat_uses_del_underscore_for_the_reserved_word():
    # `del` is a Python keyword; the schema's `del` count must be reachable via a
    # non-keyword attribute name (render.py maps it back to "del").
    stat = FileStat(path="a.ts", dir="a", add=5, del_=2, status="modified")
    assert stat.del_ == 2
    assert not hasattr(stat, "del")


def test_full_report_data_round_trips_through_every_nested_type():
    data = ReportData(
        repo="https://github.com/o/r",
        head_sha="a" * 40,
        title="HYP-1: Title",
        subtitle="Short description",
        date="2026-09-07",
        badge=Badge(label="feat", type="feat"),
        branch="feature-branch",
        links=[Link(label="PR #1", href="https://github.com/o/r/pull/1")],
        motivation="<strong>Why:</strong> ...",
        stats=Stats(files=3, insertions=100, deletions=20, commits=5),
        product_changes=ProductChanges(
            new=[ChangeEntry(title="New thing", desc="...", files=[FileRef(path="a.ts", lines="L1-L5")])]
        ),
        files=[FileStat(path="a.ts", dir="", add=10, del_=1, status="new")],
        findings=[
            Finding(id=1, severity="P1", status="resolved", title="...", file="a.ts", lines="L1-L2", desc="...")
        ],
        commits=[Commit(sha="abc1234", message="feat: x", date="2026-09-07")],
        review_rounds=[
            ReviewRound(
                title="Round 1",
                desc="...",
                badges=[ReviewBadge(label="P1: 1", cls="badge-p1")],
                items=[
                    ReviewItem(severity="P1", status="resolved", title="...", file="a.ts", lines="L1-L2")
                ],
            )
        ],
        adrs=[Adr(title="ADR-1", problem="...", options="...", decision="...", rationale="...")],
        specs=[Spec(id="spec", title="Spec", file="docs/specs/x.md", type="spec")],
    )
    assert dataclasses.asdict(data)["product_changes"]["new"][0]["title"] == "New thing"
