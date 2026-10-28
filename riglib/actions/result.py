"""Result and report value types shared by every action handler and the plan executor."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..plan import Action


@dataclass
class ActionResult:
    action: Action
    status: str  # created | updated | skipped | backed_up | error
    detail: str
    # SINGLE backup slot — a handler that backs up TWO independently-conflicting artifacts (e.g.
    # ``_do_provision_pi_extension``'s extension dir + policy file) can only surface ONE of the two
    # paths here; carry the other in ``detail``'s free text instead of losing it. Do not widen this
    # to a list without also auditing every caller/test that assumes ``backup`` is a single
    # ``Path | None`` (the dispatcher/mcp/workflow handlers all read it that way today).
    backup: Path | None = None

    @property
    def ok(self) -> bool:
        return self.status != "error"


@dataclass
class ApplyReport:
    results: list[ActionResult] = field(default_factory=list)

    @property
    def errors(self) -> list[ActionResult]:
        return [r for r in self.results if r.status == "error"]

    @property
    def changed(self) -> int:
        return sum(1 for r in self.results if r.status in ("created", "updated", "backed_up"))

    def summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.results:
            out[r.status] = out.get(r.status, 0) + 1
        return out
