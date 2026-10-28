"""Tell "the tool found problems in your code" apart from "the tool could not analyze anything".

Context: most linters exit non-zero for both. Calling a configuration failure `findings` would make
the report claim files were checked when no analysis happened (seen on a real Biome configuration
error). This module inspects a NON-ZERO outcome for the signals each provider's tool is known to emit
and returns a distinct verdict:

- `config-error`: the tool rejected its own or the project's configuration, or exited with the code
  it reserves for usage/configuration/internal errors (exit 2 for ESLint, Prettier, Ruff, mypy, ...).
- `dependencies-not-installed`: modules could not be resolved while no `node_modules` exists, so the
  project's dependencies were never installed (a recovery hint, not a finding). State `missing-tool`.
- `crash`: an uncaught exception / panic trace from the tool itself.

Verdicts of these kinds never count as having checked a file. Anything this module does not
recognise stays a plain failure with reason `findings`: unknown output is never promoted to "ok".
Pure: reads only the outcome and (for dependency readiness) whether `node_modules` directories exist.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from .lint_run_model import State, Verdict
from .lint_run_proc import CommandOutcome

if TYPE_CHECKING:
    from .lint_providers import PlannedCheck

# Exit codes a tool reserves for "I could not run properly" (everything else non-zero is findings).
_RESERVED_EXITS: Mapping[str, Mapping[int, str]] = {
    "eslint": {2: "configuration or internal error"},
    "prettier": {2: "usage, configuration or internal error"},
    "ruff-check": {2: "abnormal termination (configuration or internal error)"},
    "ruff-format": {2: "abnormal termination (configuration or internal error)"},
    "mypy": {2: "fatal error or bad configuration"},
    "pyright": {2: "fatal error", 3: "configuration error", 4: "illegal command line"},
    "pylint": {32: "usage error"},
    "shellcheck": {2: "failure to process some input files", 3: "invalid invocation", 4: "unsupported option"},
    "actionlint": {2: "invalid command line"},
}
_CRASH_EXITS: Mapping[str, frozenset[int]] = {"black": frozenset({123})}

# Output markers of a configuration failure, per provider; `tsc` TS5xxx are option/tsconfig errors.
_CONFIG_PATTERNS: Mapping[str, Sequence[re.Pattern[str]]] = {
    "biome-lint": (re.compile(r"configuration\s+━"), re.compile(r"Found an unknown key"),
                   re.compile(r"Biome couldn.t (load|read|parse)", re.I)),
    "oxlint": (re.compile(r"Failed to (parse|read|load) (the )?(oxlint )?config", re.I),
               re.compile(r"Invalid configuration", re.I)),
    "oxfmt": (re.compile(r"Failed to (parse|read|load) (the )?(oxfmt )?config", re.I),
              re.compile(r"Invalid configuration", re.I)),
    "tsc": (re.compile(r"error TS5\d{3}\b"),),
}
_CONFIG_PATTERNS = {**_CONFIG_PATTERNS, "biome-format": _CONFIG_PATTERNS["biome-lint"]}

# Unresolved modules: only a missing-dependency problem when nothing was ever installed.
_UNRESOLVED = re.compile(
    r"error TS(2307|2688|2792|7016)\b|Cannot find (module|package) ['\"]|ERR_MODULE_NOT_FOUND|MODULE_NOT_FOUND")
_CRASH_TRACE = (
    re.compile(r"^Traceback \(most recent call last\):", re.M),
    re.compile(r"^\s+at .+[(/\\].+:\d+:\d+\)?$", re.M),   # a Node stack frame
    re.compile(r"thread '.*' panicked at"),
)
_JS_PROVIDERS = frozenset({"eslint", "oxlint", "oxfmt", "prettier", "biome-lint", "biome-format", "tsc"})
_DEPENDENCY_HINT = (
    "the project's dependencies are not installed, so the tool could not resolve modules; install them with your "
    "package manager (for example `npm ci`, `pnpm install --frozen-lockfile`, `yarn install --immutable`); "
    "rig lint never installs application dependencies"
)


def _first_line(text: str, limit: int = 160) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:limit]
    return ""


def _has_node_modules(root: Path | None, cwd: str) -> bool:
    """Whether `cwd` or any ancestor up to the repo root holds a `node_modules` directory."""
    if root is None:
        return True  # cannot tell: do not claim dependencies are missing
    directory = (root / cwd).resolve()
    top = root.resolve()
    while True:
        if os.path.isdir(directory / "node_modules"):
            return True
        if directory == top or directory.parent == directory:
            return False
        directory = directory.parent


def _verdict(state: State, reason: str, detail: str) -> Verdict:
    return Verdict(state, reason, detail)


def _reserved_exit(check: PlannedCheck, code: int, evidence: str) -> Verdict | None:
    meaning = _RESERVED_EXITS.get(check.provider, {}).get(code)
    if meaning is not None:
        suffix = f": {evidence}" if evidence else ""
        return _verdict("failed", "config-error",
                        f"{check.tool} exited {code}, which it uses for a {meaning}; this is not a lint finding{suffix}")
    if code in _CRASH_EXITS.get(check.provider, frozenset()):
        return _verdict("failed", "crash", f"{check.tool} exited {code} (internal error)")
    return None


def _config_text(check: PlannedCheck, combined: str, evidence: str) -> Verdict | None:
    if any(pattern.search(combined) for pattern in _CONFIG_PATTERNS.get(check.provider, ())):
        return _verdict("failed", "config-error",
                        f"{check.tool} rejected its configuration, so no file was analyzed: {evidence}")
    return None


def _crash_text(check: PlannedCheck, combined: str, evidence: str) -> Verdict | None:
    if any(pattern.search(combined) for pattern in _CRASH_TRACE):
        return _verdict("failed", "crash", f"{check.tool} crashed instead of reporting results: {evidence}")
    return None


def tool_failure(check: PlannedCheck, outcome: CommandOutcome, root: Path | None) -> Verdict | None:
    """A verdict when a non-zero outcome is the tool failing rather than finding problems, else None."""
    combined = f"{outcome.stderr}\n{outcome.stdout}"
    evidence = _first_line(outcome.stderr) or _first_line(outcome.stdout)
    unresolved = check.provider in _JS_PROVIDERS and _UNRESOLVED.search(combined)
    if unresolved and not _has_node_modules(root, check.cwd):
        return _verdict("missing-tool", "dependencies-not-installed", f"{_DEPENDENCY_HINT} ({evidence})")
    return (_reserved_exit(check, outcome.exit_code or 0, evidence)
            or _config_text(check, combined, evidence)
            or _crash_text(check, combined, evidence))
