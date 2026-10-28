"""How much of the repository's source a set of checks actually covers (acceptance review F7/F8).

A source file counts as *checked* when an executed lint/format/typecheck check lists a capability key
``<role>:<language>:<scope>`` whose scope contains the file and whose language is in the file's family
(JavaScript and TypeScript are one family). Everything else -- files of languages with no provider,
orphan ``.go``/``.rs`` without a module, files whose only candidate check is missing a tool or not
applicable -- is *uncovered*. The runner uses this so an empty or all-skipped run never reads as a clean
pass: ``complete`` is false and the exit code says so.
"""

from __future__ import annotations

import posixpath
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass

from .lint_discovery_model import LANG_JS, LANG_TS, Discovery, ProjectScope
from .lint_providers_model import PlannedCheck
from .lint_providers_resolve import walk_up
from .lint_providers_scope import chain_tool

_ROLE_CATEGORIES = frozenset({"lint", "format", "typecheck"})
_JS_FAMILY = (LANG_JS, LANG_TS)


@dataclass(frozen=True)
class Coverage:
    files_checked: int
    files_uncovered: int
    uncovered: tuple[tuple[str, int], ...]  # (language, files), sorted by language
    # (scope, framework) of template-based frameworks whose templates no configured tool lints: Oxlint
    # reads the script part of a Vue/Svelte file only, so these stay partial (note `template-lint-uncovered`).
    template_uncovered: tuple[tuple[str, str], ...] = ()

    @property
    def complete(self) -> bool:
        return self.files_uncovered == 0

    @property
    def fully_covered(self) -> bool:
        return self.complete and not self.template_uncovered


def _family(language: str) -> tuple[str, ...]:
    return _JS_FAMILY if language in _JS_FAMILY else (language,)


def _claimed_keys(checks: Iterable[PlannedCheck], executed: Collection[str] | None) -> frozenset[str]:
    keys: set[str] = set()
    for check in checks:
        live = check.unavailable is None if executed is None else check.id in executed
        if live and check.category in _ROLE_CATEGORIES:
            keys |= check.covers
    return frozenset(keys)


def _is_covered(path: str, language: str, claimed: frozenset[str]) -> bool:
    for directory in walk_up(posixpath.dirname(path) or "."):
        for lang in _family(language):
            if any(f"{role}:{lang}:{directory}" in claimed for role in _ROLE_CATEGORIES):
                return True
    return False


_TEMPLATE_FRAMEWORKS = frozenset({"vue", "nuxt", "svelte"})


def _template_gaps(discovery: Discovery) -> tuple[tuple[str, str], ...]:
    """Template frameworks in scopes where no ESLint is configured: nothing then lints their templates."""
    gaps = []
    for scope in discovery.scopes:
        if chain_tool(discovery, scope, "eslint") is None:
            gaps.extend((scope.rel, framework) for framework in scope.frameworks if framework in _TEMPLATE_FRAMEWORKS)
    return tuple(sorted(gaps))


def coverage(discovery: Discovery, checks: Sequence[PlannedCheck], executed: Collection[str] | None = None) -> Coverage:
    """``executed`` = ids of checks that really ran (passed/failed); ``None`` = every check that is runnable."""
    claimed = _claimed_keys(checks, executed)
    checked = 0
    uncovered: dict[str, int] = {}
    for language, files in discovery.source_files.items():
        for path in files:
            if _is_covered(path, language, claimed):
                checked += 1
            else:
                uncovered[language] = uncovered.get(language, 0) + 1
    for unknown in discovery.unknown:
        if unknown.language not in discovery.source_files:
            uncovered[unknown.language] = uncovered.get(unknown.language, 0) + unknown.files
    return Coverage(checked, sum(uncovered.values()), tuple(sorted(uncovered.items())), _template_gaps(discovery))
