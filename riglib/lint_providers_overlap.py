"""Overlap resolution: demote a check only when another check provably repeats it.

Acceptance review F3: sharing a language and a category is NOT proof. ESLint and Oxlint have different
rules, Black and Ruff's formatter differ, ``lint:web`` and ``lint:api`` scripts cover different trees, and
a script called ``lint`` may not lint every language. So a check is demoted (``not-applicable``,
``covered-by:<id>``) in exactly two cases:

* another check of the same scope has the identical tool and effective argv (a true duplicate);
* another check of the same scope lists the demoted check's ``<tool>:<category>`` in ``runs`` -- a project
  script that runs that tool unrestricted in a read-only form, or Oxlint with a TypeScript >= 7 type-check
  (which makes a separate ``tsc`` pass redundant).

A check that is itself ``not-applicable`` claims nothing, so a skipped script never hides a tool check;
a ``missing-tool`` check still claims, so a gap is reported once instead of being hidden by a duplicate.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from .lint_providers_model import PlannedCheck, Unavailable


def as_unavailable(check: PlannedCheck, unavailable: Unavailable) -> PlannedCheck:
    return replace(check, argv=None, fix_argv=None, unavailable=unavailable)


def _active(check: PlannedCheck) -> bool:
    return check.unavailable is None or check.unavailable.state != "not-applicable"


def _pair(check: PlannedCheck) -> str:
    return f"{check.tool}:{check.category}"


def _order(checks: Sequence[PlannedCheck]) -> list[int]:
    # Claimants first at equal precedence so a composite check (Oxlint + type-check) wins over the tool it subsumes.
    return sorted(range(len(checks)), key=lambda i: (checks[i].precedence, not checks[i].runs, checks[i].id))


def _demote(check: PlannedCheck, winner: str) -> PlannedCheck:
    detail = f"{check.id} would repeat what {winner} already checks"
    return as_unavailable(check, Unavailable("not-applicable", f"covered-by:{winner}", detail))


def resolve_overlaps(checks: Sequence[PlannedCheck]) -> tuple[PlannedCheck, ...]:
    """Demote checks another check of higher authority provably repeats (see the module docstring)."""
    claims: dict[tuple[str, str], str] = {}
    identities: dict[tuple[str, str, tuple[str, ...]], str] = {}
    result = list(checks)
    for index in _order(checks):
        check = checks[index]
        if not _active(check):
            continue
        identity = (check.scope, check.tool, check.argv or ())
        winner = claims.get((check.scope, _pair(check)))
        if winner is None and check.argv is not None:
            winner = identities.get(identity)
        if winner is not None and winner != check.id:
            result[index] = _demote(check, winner)
            continue
        for pair in check.runs:
            claims.setdefault((check.scope, pair), check.id)
        if check.argv is not None:
            identities.setdefault(identity, check.id)
    return tuple(result)
