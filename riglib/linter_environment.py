"""Detect whether a repository is ready for Rig-managed Oxc policy.

Stdlib-only. The gate is deliberately repository-local and reproducible: a globally installed
binary is not enough for a committed generated config whose imports/scripts must also work in CI.
This module does not mutate package manifests; it produces an actionable gate and a copy-ready
migration prompt for a human or coding agent. Detection facts come from ``linter_environment_facts``
(``lint_discovery`` in a git toplevel, a root-only reader elsewhere).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .linter_environment_facts import RepoFacts, gather_facts

MIN_TYPE_AWARE_TYPESCRIPT = 7


def required_oxc_packages(*, anti_slop: bool, type_aware: bool) -> tuple[str, ...]:
    """Oxc packages the active policy imports: `@oxlint/plugins` only for the anti-slop plugin, tsgolint only for type-aware rules."""
    return ("oxlint",) + (("@oxlint/plugins",) if anti_slop else ()) + (("oxlint-tsgolint",) if type_aware else ())


@dataclass(frozen=True)
class LinterEnvironment:
    ready: bool
    oxlint_present: bool
    foreign_linters: tuple[str, ...]
    missing_oxc_packages: tuple[str, ...]
    reason: str
    agent_prompt: str
    # Type-aware Oxlint needs oxlint-tsgolint AND a resolved TypeScript >= 7 (decision D10).
    type_aware: bool = False
    type_aware_reason: str = ""
    config_files: tuple[str, ...] = ()   # existing oxlint config files at the repo root


def _join_names(names: tuple[str, ...]) -> str:
    if len(names) <= 2:
        return " and ".join(names)
    return ", ".join(names[:-1]) + ", and " + names[-1]


def _prompt(*, foreign: tuple[str, ...], required: tuple[str, ...], missing: tuple[str, ...]) -> str:
    foreign_text = ", ".join(foreign) if foreign else "no existing linter"
    missing_text = ", ".join(missing) if missing else "none"
    return (
        "Prepare this repository for Rig-managed Oxc lint policy. "
        f"Current lint environment: {foreign_text}. Missing Oxc packages: {missing_text}. "
        f"Install compatible {_join_names(required)} as repository development "
        "dependencies using the existing package manager; do not rely on a globally installed "
        "binary because CI and other developers must reproduce the toolchain. Migrate lint/format "
        "scripts and CI from any foreign linter to Oxc without deleting project-specific semantics; "
        "remove obsolete linter configuration only after its behavior is represented; run the "
        "repository tests and lint; then run `rig apply` again. Do not hand-author "
        "oxlint.config.ts: Rig owns that file and its rule policy comes from global Rig config plus "
        "rig.yaml."
    )


@dataclass(frozen=True)
class FormatterEnvironment:
    ready: bool
    oxfmt_present: bool
    foreign_formatters: tuple[str, ...]
    reason: str
    agent_prompt: str


def inspect_formatter_environment(repo_root: Path) -> FormatterEnvironment:
    """Return repository-local Oxfmt readiness independently of the lint gate."""
    facts = gather_facts(repo_root)
    oxfmt_present = "oxfmt" in facts.packages
    names = tuple(sorted(facts.foreign_formatters))
    if oxfmt_present:
        reason = "Repository-local Oxfmt is declared; Rig can apply the formatter baseline."
    elif names:
        reason = "Rig formatter baseline is blocked because this repository uses " + ", ".join(names) + " but repository-local Oxfmt is not declared."
    else:
        reason = "Rig formatter baseline is blocked because no repository-local Oxfmt dependency was detected."
    prompt = (
        "Prepare this JS/TS repository for the Rig-managed Oxfmt baseline. "
        f"Current formatter environment: {', '.join(names) if names else 'no existing formatter'}. "
        "Install compatible oxfmt as a repository development dependency using the existing package manager; "
        "migrate format scripts and CI without deleting project-specific formatting semantics; remove obsolete "
        "formatter configuration only after equivalent behavior is represented; then run `rig apply` again. "
        "Do not hand-edit the Rig-managed .oxfmtrc.jsonc target; change Rig/agent-tools policy instead."
    )
    return FormatterEnvironment(oxfmt_present, oxfmt_present, names, reason, prompt)


def _type_aware(facts: RepoFacts) -> tuple[bool, str]:
    """Whether type-aware Oxlint rules can run, with a sentence saying why (or why not)."""
    major = facts.typescript_major
    if "oxlint-tsgolint" not in facts.packages:
        return False, "oxlint-tsgolint is not declared"
    if major is None:
        return False, f"TypeScript is not declared or installed; type-aware Oxlint needs TypeScript >= {MIN_TYPE_AWARE_TYPESCRIPT}"
    if major < MIN_TYPE_AWARE_TYPESCRIPT:
        return False, f"TypeScript {major} is older than the {MIN_TYPE_AWARE_TYPESCRIPT} that type-aware Oxlint needs"
    return True, f"oxlint-tsgolint is declared and TypeScript {major} >= {MIN_TYPE_AWARE_TYPESCRIPT}"


def _blocked_reason(foreign: tuple[str, ...]) -> str:
    if foreign:
        return "Rig rule policy is blocked because this repository uses " + ", ".join(foreign) + " but repository-local Oxlint is not declared."
    return "Rig rule policy is blocked because no repository-local Oxlint dependency was detected."


def inspect_linter_environment(repo_root: Path, *, anti_slop: bool = False) -> LinterEnvironment:
    """Return the reproducible Oxc readiness gate for one repository."""
    facts = gather_facts(repo_root)
    # A PATH-global `oxlint` is intentionally NOT sufficient: the committed generated config and
    # package scripts have to work on a clean CI/developer checkout too.
    oxlint_present = "oxlint" in facts.packages
    foreign = tuple(sorted(facts.foreign_linters))
    type_aware, type_reason = _type_aware(facts)
    # tsgolint is wanted as soon as the repo resolves TypeScript >= 7 (the only case type-aware rules can run)
    wanted = facts.typescript_major is not None and facts.typescript_major >= MIN_TYPE_AWARE_TYPESCRIPT
    required = required_oxc_packages(anti_slop=anti_slop, type_aware=wanted)
    missing = tuple(pkg for pkg in required if pkg not in facts.packages)
    reason = "Repository-local Oxlint is declared; Rig can apply the rule policy." if oxlint_present else _blocked_reason(foreign)
    return LinterEnvironment(
        ready=oxlint_present, oxlint_present=oxlint_present, foreign_linters=foreign, missing_oxc_packages=missing,
        reason=reason, agent_prompt=_prompt(foreign=foreign, required=required, missing=missing),
        type_aware=type_aware, type_aware_reason=type_reason, config_files=facts.oxlint_configs,
    )
