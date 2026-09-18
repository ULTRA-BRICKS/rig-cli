"""Type-escape-hatch usage analysis for ``rig stats show``.

Distinct from :mod:`riglib.stats.hatches` (which counts agent-hook GATE BYPASSES —
``overrides.log`` / ``ship-audit.jsonl`` events). This module counts a different kind of
"hatch": TypeScript code that reaches for ``any``, ``as any``, ``as unknown as``,
``@ts-ignore``/``@ts-expect-error``, or a loose ``Record<string, unknown>`` instead of
modeling the real type — the anti-patterns the ``no-type-escape-hatches`` convention
(``skill://no-type-escape-hatches``) calls out. A rising count is drift toward unsafe
typing that the tool-adoption dashboard would otherwise never surface.

Grep-based on purpose (per AGENTS.md's stdlib-only-at-import-time rule: this module never
imports anything beyond the standard library, so ``rig stats show``/``rig --help`` stay
fast and dependency-free). It does not parse TypeScript — a string literal or comment
containing ``as any`` would false-positive, the same tradeoff the ``no-type-escape-hatches``
skill accepts for its own grep-based pre-commit gate. A count moving is the signal; exact
provenance of every hit is not the goal.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

# Directories that are never "our code": generated output, vendored/third-party trees, VCS
# internals. Any directory NAME starting with "." is also skipped (.git, .next, .turbo, …).
_SKIP_DIRS: frozenset[str] = frozenset({"node_modules", "dist", "build", "out", "coverage", "vendor"})
_TS_EXTENSIONS: tuple[str, ...] = (".ts", ".tsx")

# Each pattern targets one bullet from the `no-type-escape-hatches` skill. Kept as separate,
# independently-defined regexes (not one combined alternation) so a new hatch kind is one
# tuple to add, and so the per-kind breakdown never depends on match ORDER. Patterns CAN
# overlap on real code (`as any[]` matches both `as-any` and `bare-any`; `as unknown as any`
# matches both `as-unknown-as` and `as-any`) — `scan_repo` resolves that by scanning in THIS
# tuple's order and skipping a later match whose span overlaps an already-claimed one, so
# summing `by_kind` never double-counts a single hatch.
_KIND_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("as-unknown-as", re.compile(r"\bas\s+unknown\s+as\b")),
    ("as-any", re.compile(r"\bas\s+any\b")),
    ("ts-ignore", re.compile(r"@ts-ignore\b")),
    ("ts-expect-error", re.compile(r"@ts-expect-error\b")),
    # bare `any` in a type position: `x: any`, `Array<any>`, `Map<string, any>`, `any[]`.
    ("bare-any", re.compile(r":\s*any\b|<\s*any\s*>|,\s*any\s*>|\bany\[\]")),
    # the skill's exact "lazy" shape — `Record<string, any>` is already caught by bare-any's
    # `,\s*any\s*>` above, so this stays scoped to the `unknown` variant to avoid double-count.
    ("loose-record", re.compile(r"\bRecord<\s*string\s*,\s*unknown\s*>")),
)


@dataclass
class TypeHatchAggregate:
    """Escape-hatch counts across the TypeScript sources of one or more repos."""

    total: int = 0
    files_scanned: int = 0
    by_repo: dict[str, int] = field(default_factory=dict)  # repo path -> count, count-desc
    by_kind: dict[str, int] = field(default_factory=dict)  # kind -> count, count-desc

    def to_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "files_scanned": self.files_scanned,
            "by_kind": dict(self.by_kind),
            "by_repo": dict(self.by_repo),
        }


def _resolve_repo_path(repo: str) -> Path | None:
    if not repo:
        return None
    try:
        return Path(os.path.expanduser(repo))
    except (TypeError, ValueError):
        return None


def _iter_ts_files(root: Path) -> Iterator[Path]:
    """Every ``.ts``/``.tsx`` file under ``root``, skipping generated/vendored/VCS dirs.

    ``.d.ts`` ambient declaration files are excluded: those commonly describe third-party
    shapes rig doesn't own and routinely need ``any`` to model an untyped upstream API —
    counting them would inflate the signal with code nobody here can fix.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if name.endswith(".d.ts"):
                continue
            if name.endswith(_TS_EXTENSIONS):
                yield Path(dirpath) / name


def scan_repo(path: Path) -> tuple[int, int, Counter[str]]:
    """Scan one repo directory. Returns ``(files_scanned, total_hatches, by_kind)``.

    Patterns are scanned in ``_KIND_PATTERNS`` priority order and matches are tracked by
    character span: a match overlapping a span already claimed by an earlier-priority kind
    is skipped. Without this, a construct like ``as any[]`` matches BOTH ``as-any`` (`as
    any`) and ``bare-any`` (`any[]`) and gets counted twice for one real hatch; span
    tracking keeps the invariant "one hatch, one count" true across independently-defined
    patterns instead of only within a single pattern's own `findall`.
    """
    files_scanned = 0
    by_kind: Counter[str] = Counter()
    for file_path in _iter_ts_files(path):
        try:
            text = file_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        files_scanned += 1
        claimed: list[tuple[int, int]] = []
        for kind, pattern in _KIND_PATTERNS:
            for match in pattern.finditer(text):
                start, end = match.span()
                if any(start < c_end and end > c_start for c_start, c_end in claimed):
                    continue
                claimed.append((start, end))
                by_kind[kind] += 1
    return files_scanned, sum(by_kind.values()), by_kind


def scan_type_hatches(repos: Iterable[str]) -> TypeHatchAggregate:
    """Scan every repo in ``repos`` (deduped, non-existent/non-directory paths skipped) and
    aggregate escape-hatch counts. Pure filesystem read — no caching, no network."""
    seen: set[str] = set()
    by_repo: dict[str, int] = {}
    by_kind: Counter[str] = Counter()
    files_scanned = 0

    for repo in repos:
        path = _resolve_repo_path(repo)
        if path is None:
            continue
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if not path.is_dir():
            continue
        n_files, n_total, kind_counts = scan_repo(path)
        files_scanned += n_files
        if n_total:
            by_repo[key] = n_total
            by_kind.update(kind_counts)

    ordered_repo = dict(sorted(by_repo.items(), key=lambda kv: (-kv[1], kv[0])))
    ordered_kind = dict(sorted(by_kind.items(), key=lambda kv: (-kv[1], kv[0])))
    return TypeHatchAggregate(
        total=sum(by_kind.values()),
        files_scanned=files_scanned,
        by_repo=ordered_repo,
        by_kind=ordered_kind,
    )
