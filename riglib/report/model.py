"""Data model for a single completion report.

Mirrors the ``REPORT_DATA`` object the ``result-report`` agent-tools skill populates
and a target reports repo's ``reports/shared/components.js`` renders (see
``skills/universal/result-report/SKILL.md``, "REPORT_DATA schema" -- ported from
HyperIDE's ``hyperide.github.io`` reference implementation).

**Upstream status as of 2026-09-07 (scaffold time):** that skill was an UNMERGED
agent-tools branch/PR (#440) when this module was written, not yet on agent-tools
main -- verify it has actually merged before treating this schema as authoritative for
a real `rig report new` implementation, and re-diff this module against the merged
`SKILL.md` for drift at that point.

This module is NOT the source of truth for the HTML/CSS/JS template itself -- that
lives in the target reports repo (``reports/shared/{styles.css,components.js}``,
``_template/report.html``). It only shapes the JS object :mod:`riglib.report.render`
hands to ``Report.init()``. Field names below are snake_case (Python convention);
:mod:`riglib.report.render` maps them back to the camelCase keys the skill's schema
and ``components.js`` actually read.

Every field is optional-with-default except the handful the skill doc marks as
always present in practice (title/date/badge/branch) -- keep it lenient: a caller
building a report incrementally should be able to construct a partial ``ReportData``
without every list populated.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Link:
    label: str
    href: str


@dataclass
class Badge:
    label: str
    type: str  # feat|fix|refactor|docs


@dataclass
class Stats:
    files: int = 0
    insertions: int = 0
    deletions: int = 0
    commits: int = 0


@dataclass
class FileRef:
    """A single file reference inside a product-change entry or an SVG diagram node."""

    path: str
    lines: str = ""  # e.g. "L10-L50"
    label: str = ""  # e.g. "file.ts:10 (functionName)"


@dataclass
class ChangeEntry:
    title: str
    desc: str
    files: list[FileRef] = field(default_factory=list)


@dataclass
class ProductChanges:
    new: list[ChangeEntry] = field(default_factory=list)
    improvements: list[ChangeEntry] = field(default_factory=list)
    fixes: list[ChangeEntry] = field(default_factory=list)


@dataclass
class FileStat:
    path: str
    dir: str
    add: int = 0
    del_: int = 0  # `del` is a Python keyword; render.py maps this back to "del"
    status: str = "modified"  # new|modified|deleted


@dataclass
class Finding:
    id: int
    severity: str  # P1|P2|P3
    status: str  # resolved|accepted
    title: str
    file: str
    lines: str
    desc: str
    fix: str = ""


@dataclass
class Commit:
    sha: str
    message: str
    date: str


@dataclass
class ReviewBadge:
    label: str
    cls: str  # e.g. "badge-p1", "badge-resolved"


@dataclass
class ReviewItem:
    severity: str
    status: str
    title: str
    file: str
    lines: str
    resolution: str = ""


@dataclass
class ReviewRound:
    title: str
    desc: str
    badges: list[ReviewBadge] = field(default_factory=list)
    items: list[ReviewItem] = field(default_factory=list)


@dataclass
class Adr:
    title: str
    problem: str
    options: str
    decision: str
    rationale: str


@dataclass
class Spec:
    id: str
    title: str
    file: str
    lines: int | None = None
    date: str = ""
    type: str = "spec"  # spec|plan


@dataclass
class ReportData:
    # Core metadata
    repo: str
    head_sha: str

    # Header
    title: str
    subtitle: str
    date: str
    badge: Badge
    branch: str
    links: list[Link] = field(default_factory=list)

    # Motivation -- WHY, shown at the top; supports HTML
    motivation: str = ""

    # Summary
    stats: Stats = field(default_factory=Stats)

    # Product changes
    product_changes: ProductChanges = field(default_factory=ProductChanges)

    # Files
    files: list[FileStat] = field(default_factory=list)

    # Review findings (code review) and commit timeline
    findings: list[Finding] = field(default_factory=list)
    commits: list[Commit] = field(default_factory=list)
    review_rounds: list[ReviewRound] = field(default_factory=list)

    # Architecture decision records and spec/plan viewer entries
    adrs: list[Adr] = field(default_factory=list)
    specs: list[Spec] = field(default_factory=list)
