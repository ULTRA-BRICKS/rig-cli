"""How much of the repository's source did `rig lint run` actually verify?

Context: a green exit must never hide a file nobody looked at (a Kotlin-only repo, a lone `tool.py`,
a tool that was missing or rejected its configuration). Discovery lists every supported-language
source file (`source_files`, orphans included) and every unsupported-language file (`unknown`); the
planned checks say which language/scope each one covers (`PlannedCheck.covers` keys
`<role>:<language>:<scope>`). A file is CHECKED only when an executed lint/format/typecheck check
covering its language and an ancestor directory reached a real verdict (passed, or failed with
findings). A file git tracks although an ignore rule matches it (``tracked_ignored``) is skipped by every
directory walk that honours ``.gitignore``, so only a check flagged ``reaches_ignored`` counts for it; otherwise
it is uncovered with the reason ``tracked-but-ignored``. A tool that failed to run (config error, missing dependencies, crash, timeout, wrote
files) analyzed nothing, so its files stay UNCHECKED: planned coverage alone never counts.

`complete` is the strict conjunction: every APPLICABLE check actually finished its analysis (a crash,
timeout, config error, missing tool or write by any check makes it false, even when other checks cover
the same files) AND every source file was checked AND no template goes unlinted. `files_uncovered` is
everything not checked; `no_checker` is the part for which no check exists at all (the
`unsupported-language` exit rule).

Pure functions over data; no I/O.
"""
from __future__ import annotations

import posixpath
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .lint_discovery import Discovery
    from .lint_run_model import CheckResult

TEMPLATE_REASON = "template-lint-uncovered"
TRACKED_IGNORED = "tracked-but-ignored"
_VERIFYING_ROLES = frozenset({"lint", "format", "typecheck"})
_EXAMPLES = 3


@dataclass(frozen=True)
class Uncovered:
    language: str
    files: int
    examples: tuple[str, ...]
    # "no-provider": Rig has no checker for the language; "no-check": no check was planned for the
    # file; "not-verified": a check exists but did not reach a verdict about it; "tracked-but-ignored": the
    # file is tracked yet ignored, and no executed check could analyze it
    why: str


@dataclass(frozen=True)
class TemplateGap:
    """A Vue/Svelte/Nuxt scope whose templates nothing lints (Oxlint reads the script part only)."""
    scope: str
    framework: str


@dataclass(frozen=True)
class Coverage:
    files_total: int
    files_checked: int
    files_uncovered: int
    no_checker: int     # uncovered files for which no check exists at all
    uncovered: tuple[Uncovered, ...]
    # every applicable check finished its analysis, every source file reached a real verdict, no template goes unlinted
    complete: bool
    template_gaps: tuple[TemplateGap, ...] = ()
    incomplete_checks: tuple[str, ...] = ()

    @property
    def unverified(self) -> int:
        """Items a green exit would hide: unchecked files plus unlinted template scopes."""
        return self.files_uncovered + len(self.template_gaps)

    def as_dict(self) -> dict[str, object]:
        return {
            "files_total": self.files_total, "files_checked": self.files_checked,
            "files_uncovered": self.files_uncovered, "complete": self.complete,
            "uncovered": [{"language": u.language, "files": u.files, "examples": list(u.examples), "why": u.why}
                          for u in self.uncovered],
            "template_uncovered": [{"scope": g.scope, "framework": g.framework, "reason": TEMPLATE_REASON}
                                   for g in self.template_gaps],
            "incomplete_checks": list(self.incomplete_checks),
        }


def _family(language: str) -> str:
    # One JS/TS tool covers both; treating them separately would call a .ts file uncovered by `lint:javascript`.
    return "js" if language in ("javascript", "typescript") else language


def _verified(result: CheckResult) -> bool:
    return result.state == "passed" or (result.state == "failed" and result.reason == "findings")


def incomplete_checks(results: Iterable[CheckResult]) -> tuple[str, ...]:
    """Ids of applicable checks that did NOT finish analysis (crash, timeout, config error, wrote files,
    missing tool, unsupported platform). `not-applicable` and `planned` rows were never meant to analyze."""
    return tuple(r.check.id for r in results
                 if r.state in ("missing-tool", "unsupported-platform") or (r.state == "failed" and r.reason != "findings"))


def _claims(
    results: Iterable[CheckResult], *, reaching_ignored: bool = False
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """(family -> scopes with a verdict, family -> scopes with any check at all).

    ``reaching_ignored`` keeps only the checks that can analyze tracked-but-ignored files.
    """
    verified: dict[str, set[str]] = {}
    mentioned: dict[str, set[str]] = {}
    for result in results:
        if reaching_ignored and not result.check.reaches_ignored:
            continue
        if result.check.category == "limits":
            continue
        for key in result.check.covers:
            role, _, rest = key.partition(":")
            language, _, scope = rest.partition(":")
            if role not in _VERIFYING_ROLES:
                continue
            mentioned.setdefault(_family(language), set()).add(scope)
            if _verified(result):
                verified.setdefault(_family(language), set()).add(scope)
    return verified, mentioned


def _ancestors(path: str) -> Iterable[str]:
    directory = posixpath.dirname(path)
    while directory:
        yield directory
        directory = posixpath.dirname(directory)
    yield "."


def _covered(path: str, scopes: set[str] | None) -> bool:
    return bool(scopes) and any(scope in scopes for scope in _ancestors(path))


def _group(language: str, paths: Sequence[str], why: str) -> Uncovered:
    return Uncovered(language, len(paths), tuple(sorted(paths)[:_EXAMPLES]), why)


def _bucket(
    path: str, family: str, ignored: bool, verified: dict[str, set[str]], mentioned: dict[str, set[str]],
    reaching: dict[str, set[str]],
) -> str:
    """"checked" | "not-verified" | "no-check" | "tracked-but-ignored" for one supported-language file."""
    if ignored:
        if _covered(path, reaching.get(family)):
            return "checked"
        return TRACKED_IGNORED if _covered(path, mentioned.get(family)) else "no-check"
    if _covered(path, verified.get(family)):
        return "checked"
    return "not-verified" if _covered(path, mentioned.get(family)) else "no-check"


def _classify_files(
    source: Mapping[str, Sequence[str]], verified: dict[str, set[str]], mentioned: dict[str, set[str]],
    tracked_ignored: frozenset[str] = frozenset(), reaching: dict[str, set[str]] | None = None,
) -> tuple[int, list[Uncovered]]:
    """(checked files, groups of unchecked files) over the supported-language files."""
    checked = 0
    groups: list[Uncovered] = []
    for language in sorted(source):
        buckets: dict[str, list[str]] = {"not-verified": [], "no-check": [], TRACKED_IGNORED: []}
        for path in source[language]:
            kind = _bucket(path, _family(language), path in tracked_ignored, verified, mentioned, reaching or {})
            if kind == "checked":
                checked += 1
            else:
                buckets[kind].append(path)
        groups.extend(_group(language, paths, why) for why, paths in buckets.items() if paths)
    return checked, groups


def template_gaps(discovery: Discovery) -> tuple[TemplateGap, ...]:
    """(scope, framework) pairs whose templates nothing lints; the rule lives with the provider catalog."""
    from . import lint_providers  # the registry is built on import; only a full run needs it

    return tuple(TemplateGap(scope, framework)
                 for scope, framework in lint_providers.coverage(discovery, ()).template_uncovered)


def coverage(discovery: Discovery, results: Sequence[CheckResult]) -> Coverage:
    verified, mentioned = _claims(results)
    reaching, _ = _claims(results, reaching_ignored=True)
    checked, groups = _classify_files(
        discovery.source_files, verified, mentioned, frozenset(discovery.tracked_ignored), reaching
    )
    groups.extend(Uncovered(u.language, u.files, tuple(u.examples), "no-provider") for u in discovery.unknown)
    uncovered = sum(g.files for g in groups)
    no_checker = sum(g.files for g in groups if g.why not in ("not-verified", TRACKED_IGNORED))
    gaps = template_gaps(discovery)
    incomplete = incomplete_checks(results)
    return Coverage(checked + uncovered, checked, uncovered, no_checker, tuple(groups),
                    complete=uncovered == 0 and not gaps and not incomplete, template_gaps=gaps,
                    incomplete_checks=incomplete)
