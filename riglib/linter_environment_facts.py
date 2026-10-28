"""Repository facts behind ``linter_environment``: what the repo root declares and configures.

Context: the Oxc readiness gate only cares about the repository ROOT scope (that is what `rig apply`
writes into). In a git worktree the facts come from ``lint_discovery`` (one detection engine for the
whole of `rig lint`); a plain directory that is not a git toplevel (tests, tarball exports) falls back
to a small root-only reader with the historical labels, so `rig apply` keeps working there.
Stdlib-only; nothing from the repository is imported or executed.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .lint_check_scope import ScopeError, git_toplevel
from .lint_discovery import Discovery, ProjectScope, discover

OXLINT_CONFIG_NAMES = (".oxlintrc.json", ".oxlintrc.jsonc", "oxlint.config.ts", "oxlint.config.mts")
_FOREIGN_LINTERS = {"eslint": "ESLint", "biome": "Biome", "standard": "StandardJS", "xo": "XO", "tslint": "TSLint"}
_FOREIGN_FORMATTERS = {"prettier": "Prettier", "biome": "Biome", "dprint": "dprint"}
# discovery tool id -> npm package name, for the packages the Oxc gate asks about
_OXC_TOOL_PACKAGES = {"oxlint": "oxlint", "oxfmt": "oxfmt", "oxlint-plugins": "@oxlint/plugins", "oxlint-tsgolint": "oxlint-tsgolint"}
_PACKAGE_LINTER_LABELS = {"eslint": "ESLint", "@biomejs/biome": "Biome", "standard": "StandardJS", "xo": "XO", "tslint": "TSLint"}
_PACKAGE_FORMATTER_LABELS = {"prettier": "Prettier", "@biomejs/biome": "Biome", "dprint": "dprint"}
_CONFIG_GLOBS = {
    "ESLint": ("eslint.config.*", ".eslintrc", ".eslintrc.*"), "Biome": ("biome.json", "biome.jsonc"), "TSLint": ("tslint.json",),
}
_FORMATTER_CONFIG_GLOBS = {
    "Prettier": (".prettierrc", ".prettierrc.*", "prettier.config.*"), "Biome": ("biome.json", "biome.jsonc"),
    "dprint": ("dprint.json", "dprint.jsonc"),
}
_SCRIPT_LINTER_TOKENS = {"eslint": "ESLint", "biome": "Biome", "standard": "StandardJS", "xo": "XO", "tslint": "TSLint"}
_SCRIPT_FORMATTER_TOKENS = {"prettier": "Prettier", "biome": "Biome", "dprint": "dprint"}
_SPEC_LOWER_BOUND = re.compile(r"^\s*(?:\^|~|>=|=)?\s*v?(\d+)(?:\.|\s*$)")


@dataclass(frozen=True)
class RepoFacts:
    packages: frozenset[str]            # declared Oxc packages: oxlint oxfmt @oxlint/plugins oxlint-tsgolint
    foreign_linters: frozenset[str]
    foreign_formatters: frozenset[str]
    oxlint_configs: tuple[str, ...]     # existing oxlint config files at the repo root
    typescript_major: int | None        # installed major, else the lower bound of the declared spec


def spec_lower_bound(spec: str | None) -> int | None:
    """Major version a dependency spec guarantees at least (`^7.0.0`, `~7`, `>=7`, `7.x`); None for ranges that guarantee nothing."""
    if not spec or "||" in spec:
        return None
    match = _SPEC_LOWER_BOUND.match(spec)
    return int(match.group(1)) if match else None


def major_of(version: str | None) -> int | None:
    head = (version or "").split(".", 1)[0]
    return int(head) if head.isdigit() else None


def _typescript_major(scope: ProjectScope) -> int | None:
    info = scope.typescript
    if info is None:
        return None
    return info.major if info.major is not None else spec_lower_bound(info.declared_spec)


def _from_discovery(discovery: Discovery) -> RepoFacts:
    scope = discovery.scope(".")
    if scope is None:
        return RepoFacts(frozenset(), frozenset(), frozenset(), (), None)
    present = {tool.tool for tool in scope.tools}
    declared = {tool.tool for tool in scope.tools if tool.declared}
    return RepoFacts(
        packages=frozenset(_OXC_TOOL_PACKAGES[t] for t in declared if t in _OXC_TOOL_PACKAGES),
        foreign_linters=frozenset(label for t, label in _FOREIGN_LINTERS.items() if t in present),
        foreign_formatters=frozenset(label for t, label in _FOREIGN_FORMATTERS.items() if t in present),
        oxlint_configs=_oxlint_configs(scope),
        typescript_major=_typescript_major(scope),
    )


def _oxlint_configs(scope: ProjectScope) -> tuple[str, ...]:
    files = {f for tool in scope.tools if tool.tool == "oxlint" for f in tool.config_files}
    return tuple(sorted(files))


def _manifest(repo_root: Path) -> dict[str, Any]:
    path = repo_root / "package.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _declared_packages(manifest: Mapping[str, Any]) -> set[str]:
    out: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        block = manifest.get(key)
        if isinstance(block, dict):
            out.update(str(name) for name in block)
    return out


def _script_tokens(manifest: Mapping[str, Any]) -> set[str]:
    """Executable/package tokens in package scripts; exact tokens avoid `standard-version` false positives."""
    scripts = manifest.get("scripts")
    out: set[str] = set()
    for value in (scripts.values() if isinstance(scripts, dict) else ()):
        if not isinstance(value, str):
            continue
        try:
            parts = shlex.split(value, posix=True)
        except ValueError:
            parts = value.split()
        out.update(p.strip().lower().replace("\\", "/").rsplit("/", 1)[-1] for p in parts)
    return {t for t in out if t and t not in {"&&", "||", ";", "|"}}


def _labels(repo_root: Path, packages: set[str], tokens: set[str], by_package: Mapping[str, str],
            globs: Mapping[str, tuple[str, ...]], by_token: Mapping[str, str]) -> frozenset[str]:
    found = {label for pkg, label in by_package.items() if pkg in packages}
    found |= {label for label, patterns in globs.items() if any(any(repo_root.glob(p)) for p in patterns)}
    found |= {label for token, label in by_token.items() if token in tokens}
    return frozenset(found)


def _installed_typescript_version(repo_root: Path) -> str | None:
    """JSON-only read, and only when no component of the path is a symlink."""
    current = repo_root
    for part in ("node_modules", "typescript", "package.json"):
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except OSError:
            return None
        if stat.S_ISLNK(mode):
            return None
    try:
        version = json.loads(current.read_text(encoding="utf-8")).get("version")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return None
    return version if isinstance(version, str) else None


def _declared_typescript(manifest: Mapping[str, Any]) -> str | None:
    for key in ("dependencies", "devDependencies"):
        block = manifest.get(key)
        spec = block.get("typescript") if isinstance(block, dict) else None
        if isinstance(spec, str):
            return spec
    return None


def _from_files(repo_root: Path) -> RepoFacts:
    manifest = _manifest(repo_root)
    packages = _declared_packages(manifest)
    tokens = _script_tokens(manifest)
    version = _installed_typescript_version(repo_root)
    return RepoFacts(
        packages=frozenset(p for p in _OXC_TOOL_PACKAGES.values() if p in packages),
        foreign_linters=_labels(repo_root, packages, tokens, _PACKAGE_LINTER_LABELS, _CONFIG_GLOBS, _SCRIPT_LINTER_TOKENS),
        foreign_formatters=_labels(repo_root, packages, tokens, _PACKAGE_FORMATTER_LABELS, _FORMATTER_CONFIG_GLOBS, _SCRIPT_FORMATTER_TOKENS),
        oxlint_configs=tuple(n for n in OXLINT_CONFIG_NAMES if (repo_root / n).is_file()),
        typescript_major=major_of(version) if version else spec_lower_bound(_declared_typescript(manifest)),
    )


def _is_git_toplevel(repo_root: Path) -> bool:
    try:
        return os.path.realpath(repo_root) == git_toplevel(str(repo_root))
    except ScopeError:
        return False


def gather_facts(repo_root: Path) -> RepoFacts:
    """Root-scope facts: discovery inside a git toplevel, the small file reader everywhere else."""
    if _is_git_toplevel(repo_root):
        return _from_discovery(discover(repo_root))
    return _from_files(repo_root)
