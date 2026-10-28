"""Data model + closed vocabularies of the lint discovery (re-exported by ``riglib.lint_discovery``).

Context: ``lint_discovery`` turns a git worktree into ``ProjectScope`` facts that the provider
registry (``lint_providers``), the bootstrap planner and ``linter_environment`` all consume. The
classes live in their own module so the per-ecosystem fact collectors can build them without
importing the orchestrator (no cycle). Pure data: stdlib only, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Mapping

Role = Literal["lint", "format", "typecheck"]
EvidenceKind = Literal["manifest", "lockfile", "config", "field", "source", "script"]

# closed language/trait vocabulary used by providers
LANG_JS, LANG_TS, LANG_PY, LANG_RUST, LANG_GO, LANG_SHELL = (
    "javascript", "typescript", "python", "rust", "go", "shell")
TRAIT_GH_WORKFLOWS = "github-workflows"

# Roles each known tool can fill. A tool with two roles is reported as two ConfiguredTool rows so
# "is this role already configured in this scope" (decision D3) is a plain lookup.
TOOL_ROLES: Mapping[str, tuple[Role, ...]] = {
    "eslint": ("lint",), "biome": ("lint", "format"), "prettier": ("format",),
    "oxlint": ("lint",), "oxfmt": ("format",), "dprint": ("format",),
    "tslint": ("lint",), "xo": ("lint",), "standard": ("lint",), "tsc": ("typecheck",),
    "ruff": ("lint", "format"), "black": ("format",), "flake8": ("lint",),
    "pylint": ("lint",), "mypy": ("typecheck",), "pyright": ("typecheck",),
}


@dataclass(frozen=True)
class Evidence:
    path: str                      # repo-relative POSIX, never absolute
    kind: EvidenceKind
    detail: str = ""


@dataclass(frozen=True)
class PackageManager:
    ecosystem: Literal["js", "python"]
    name: str                      # npm|pnpm|yarn|yarn-berry|bun|uv|poetry|pdm|pip ; "" when ambiguous
    # "packageManager field"|"lockfile"|"workspace marker"|"default"|"ambiguous"; a `[tool.uv]`-style
    # table without a lock is a "workspace marker"
    source: str
    lockfile: str | None           # repo-relative
    root: str                      # repo-relative dir owning manifest+lock (workspace root); "." for repo root
    ambiguous: tuple[str, ...] = ()  # candidate names when conflicting evidence


@dataclass(frozen=True)
class ConfiguredTool:
    tool: str                      # provider-neutral id (open vocabulary, see TOOL_ROLES)
    role: Role
    declared: bool                 # dependency / tool table present in the manifest
    config_files: tuple[str, ...]  # repo-relative, existing, non-symlink
    scripts: tuple[str, ...]       # names of scripts whose tokens mention the tool
    evidence: tuple[Evidence, ...]
    options: Mapping[str, str]     # tiny facts only, e.g. {"type_check": "true"} (Oxlint options.typeCheck)


@dataclass(frozen=True)
class ProjectScript:
    runner: Literal["package.json", "Makefile"]
    name: str
    command: str                   # raw text, classification only, NEVER executed via shell
    tokens: tuple[str, ...]        # lowercased executable/package tokens
    invokes_rig_lint: bool
    writes_files: bool             # --fix/--write/-w/--watch token present
    roles: tuple[Role, ...]        # from the name allow-list (empty => not a lint/check script)
    covers_tools: tuple[str, ...]  # configured-tool ids found in tokens
    # "<tool>:<role>" pairs this script provably runs in full: the tool is named in a segment with a
    # read-only form and no path restriction. Only these may demote a Rig check (decision D9, F3).
    covers_roles: tuple[str, ...] = ()
    # Formatters the command names WITHOUT a read-only form (oxfmt ., ruff format, cargo fmt, black .):
    # such a script rewrites files, so it is never run as a check (reason `writes-unknown`).
    unsafe_formatters: tuple[str, ...] = ()


@dataclass(frozen=True)
class TypeScriptInfo:
    declared_spec: str | None
    installed_version: str | None  # read from node_modules/typescript/package.json (JSON only)
    major: int | None
    tsconfigs: tuple[str, ...]     # repo-relative tsconfig*.json that exist in the scope dir


@dataclass(frozen=True)
class ProjectScope:
    rel: str                       # "." or POSIX relative dir
    languages: tuple[str, ...]     # sorted, closed vocabulary above (traits included)
    package_manager: PackageManager | None
    tools: tuple[ConfiguredTool, ...]
    scripts: tuple[ProjectScript, ...]
    typescript: TypeScriptInfo | None
    files: Mapping[str, tuple[str, ...]]   # language/trait -> owned repo-relative files; ONLY shell, go, github-workflows
    evidence: tuple[Evidence, ...]
    # The manager of the OTHER ecosystem when one directory holds both a package.json and Python
    # manifests (`package_manager` is the JS one then). Contract amendment, see the plan doc.
    other_managers: tuple[PackageManager, ...] = ()
    # Frameworks found in declared dependencies and .tsx/.jsx/.vue files: react next vue nuxt svelte.
    frameworks: tuple[str, ...] = ()


@dataclass(frozen=True)
class UnknownLanguage:
    language: str                  # e.g. "kotlin"
    files: int
    examples: tuple[str, ...]      # <= 3 repo-relative paths


@dataclass(frozen=True)
class DiscoveryProblem:
    path: str
    kind: Literal["malformed-manifest", "malformed-config", "unreadable", "symlink-escape"]
    detail: str


@dataclass(frozen=True)
class Discovery:
    root: Path                     # resolved git toplevel
    scopes: tuple[ProjectScope, ...]       # sorted by rel; synthetic "." scope exists iff root-level shell/workflows exist
    unknown: tuple[UnknownLanguage, ...]
    problems: tuple[DiscoveryProblem, ...]
    skipped_symlinks: tuple[str, ...]
    file_count: int
    toml_parser: bool              # False on Python 3.10 without tomli: TOML facts fall back to table-header scan
    # language -> every enumerated source file of a supported language (javascript typescript python shell
    # go rust), orphans included. Lets the runner say how many files a run really covered.
    source_files: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # Every enumerated file (tracked + untracked-not-ignored, non-symlink), sorted: the SAME inventory that
    # `source_files` and the coverage figures come from, so a file counted as checked is the file a tool was
    # given. Providers whose tool would otherwise recurse into git-ignored trees (Biome, ESLint, mypy,
    # flake8) pass these instead of ``.``.
    enumerated_files: tuple[str, ...] = ()
    # Git-ignored directories (top-most, repo-relative POSIX, no trailing slash): `.worktrees/x`, `dist`, ...
    ignored_dirs: tuple[str, ...] = ()
    # Tracked files an ignore rule also matches (a subset of `enumerated_files`): directory walks that honour
    # `.gitignore` skip them, so only a check that names them explicitly has analyzed them.
    tracked_ignored: tuple[str, ...] = ()

    def scope(self, rel: str) -> ProjectScope | None:
        return next((s for s in self.scopes if s.rel == rel), None)

    def ancestors(self, rel: str) -> tuple[ProjectScope, ...]:
        """Scopes whose directory contains ``rel`` (itself included), nearest first."""
        hits = [s for s in self.scopes if contains(s.rel, rel)]
        return tuple(sorted(hits, key=lambda s: depth(s.rel), reverse=True))


def depth(rel: str) -> int:
    return 0 if rel == "." else rel.count("/") + 1


def contains(parent: str, child: str) -> bool:
    """Whether directory ``parent`` is ``child`` or an ancestor of it (both repo-relative, "." = root)."""
    return parent == "." or child == parent or child.startswith(parent + "/")


def join(directory: str, name: str) -> str:
    return name if directory == "." else f"{directory}/{name}"
