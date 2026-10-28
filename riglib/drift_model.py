"""Drift report data model and the tiny helpers every drift checker shares."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from .provenance import PERMISSION_KINDS


@dataclass
class DriftItem:
    direction: str  # "missing" (config→disk) | "extra" (disk→config) | "modified"
    category: str
    item: str
    target: Path
    detail: str


@dataclass
class DriftNote:
    """A fact about a reconciled area that is NOT drift but must stay visible (rig-cli#375).

    Unlike :class:`KnownItem` (an on-disk item rig could place but does not manage), a note is a
    free-form observation a checker wants printed next to its area's state — today: the Spotlight
    dependency dirs created since the last sweep, which no sweep could have covered yet. Reporting
    them as drift makes ``rig status`` permanently red on a machine that builds anything; dropping
    them silently is how a dead sweep agent hides for a month. So: shown dim beside "in sync",
    never counted in the drift totals, never part of the exit code.
    """

    category: str
    item: str
    detail: str


@dataclass
class KnownItem:
    """An on-disk item rig does NOT manage but CAN place (rig-cli#357) — informational, never drift.

    ``kind`` is a :mod:`riglib.provenance` grouping key (``KIND_LABELS`` names it for the user),
    ``by`` the installer it names (empty when the kind carries none), ``name`` the display label
    (the skill/hook/workflow name, or the permission entry itself). ``container`` is set for a
    ``permissions`` item only: the list the entry sits in, ``<harness kind>:<dotted key path>``
    (``claude-code:permissions.deny``), which the renderer groups by. Kept OUT of
    :attr:`DriftReport.items` so ``in_sync``, the drift exit code and the area counts never see it.
    """

    category: str
    item: str
    target: Path
    kind: str
    name: str
    by: str = ""
    container: str = ""


@dataclass
class DriftReport:
    items: list[DriftItem] = field(default_factory=list)
    known: list[KnownItem] = field(default_factory=list)
    notes: list[DriftNote] = field(default_factory=list)

    @property
    def in_sync(self) -> bool:
        return not self.items

    @property
    def known_placed(self) -> list[KnownItem]:
        """Known items whose origin is another installer / the config / the repo — "not managed by rig"."""
        return [k for k in self.known if k.kind not in PERMISSION_KINDS]

    @property
    def known_kept(self) -> list[KnownItem]:
        """Permission entries beyond the rig baseline — "your additions, kept"."""
        return [k for k in self.known if k.kind in PERMISSION_KINDS]

    def by_direction(self, direction: str) -> list[DriftItem]:
        return [i for i in self.items if i.direction == direction]


def _managed_block_body(conf_text: str, begin: str, end: str) -> str | None:
    """The text BETWEEN the managed sentinels (exclusive), or None when absent/malformed."""
    b = conf_text.find(begin)
    e = conf_text.find(end)
    if b == -1 or e == -1 or e <= b:
        return None
    return conf_text[b + len(begin):e].strip("\n")


def _on_darwin() -> bool:
    return sys.platform == "darwin"
