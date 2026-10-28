"""Lint discovery: what a git worktree contains, as facts for the provider registry and the bootstrap planner.

Context: `rig lint` must work on any repository, so it first needs an honest inventory: project scopes
(a directory per manifest), the language(s) of each, tools the project already configures (never
overwritten, decision D3), package managers, project scripts, frameworks, and every source file that
no manifest explains. Trust boundary: the repository is attacker-controlled input. Discovery never
imports or executes repository code (JSON/JSONC/TOML are parsed with the stdlib, YAML/JS/TS configs are
matched by file name, symlinked manifests are never read) and never raises for malformed content;
that becomes a ``DiscoveryProblem``. Only "not a git repository" is an error (``ScopeError``).

Public names: the dataclasses and vocabularies of ``lint_discovery_model`` are re-exported here so
consumers import one module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from .lint_check_scope import ScopeError, git_toplevel
from .lint_discovery_files import (Inventory, classify_sources, deno_configs, enumerate_inventory, ignored_directories,
                                   symlink_escapes, tracked_ignored_files, unknown_languages)
from .lint_discovery_io import Reader, parse_jsonc, toml_available
from .lint_discovery_js import is_js_config_name
from .lint_discovery_model import (LANG_GO, LANG_JS, LANG_PY, LANG_RUST, LANG_SHELL, LANG_TS, TOOL_ROLES, TRAIT_GH_WORKFLOWS,
                                   ConfiguredTool, Discovery, DiscoveryProblem, Evidence, EvidenceKind, PackageManager,
                                   ProjectScope, ProjectScript, Role, TypeScriptInfo, UnknownLanguage, contains, depth, join)
from .lint_discovery_ownership import build_layout, manifest_dirs, manifest_names
from .lint_discovery_python import is_python_config_name
from .lint_discovery_scopes import Context, build_scope, drop_inherited_tools, makefile_scripts

__all__ = [
    "LANG_GO", "LANG_JS", "LANG_PY", "LANG_RUST", "LANG_SHELL", "LANG_TS", "TOOL_ROLES", "TRAIT_GH_WORKFLOWS",
    "ConfiguredTool", "Discovery", "DiscoveryProblem", "Evidence", "EvidenceKind", "PackageManager", "ProjectScope",
    "ProjectScript", "Role", "ScopeError", "TypeScriptInfo", "UnknownLanguage", "contains", "depth", "discover", "join",
    "parse_jsonc",
]


def _prefixes(scopes: Sequence[str]) -> list[str]:
    """Repo-relative path prefixes as ``lint_check_scope`` expects them ("" selects everything)."""
    cleaned = [p.strip().replace("\\", "/").strip("/") for p in scopes]
    return ["" if p in ("", ".") else p.removeprefix("./") for p in cleaned]


def _is_manifest_or_config(name: str) -> bool:
    return bool(manifest_names(frozenset({name}))) or name == "Makefile" or is_js_config_name(name) or is_python_config_name(name)


def _symlink_problems(root: str, inventory: Inventory) -> list[DiscoveryProblem]:
    """A symlinked manifest/config is never read; one that points outside the repository is reported."""
    return [
        DiscoveryProblem(rel, "symlink-escape", "symlink resolves outside the repository; not read")
        for rel in inventory.symlinks
        if _is_manifest_or_config(rel.rsplit("/", 1)[-1]) and symlink_escapes(root, rel)
    ]


def discover(root: str | Path, *, scopes: Sequence[str] = (), assume_ignored: Sequence[str] = ()) -> Discovery:
    """Inventory the git worktree at ``root`` (``ScopeError`` when it is not a repository).

    ``scopes`` are repo-relative path prefixes limiting the enumeration; empty means the whole repository.
    ``assume_ignored`` are git ignore patterns to treat as already in force for UNTRACKED files (the pending
    ``node_modules`` ignore of a bootstrap), so a plan sees the repository as it will be after the install.
    """
    top = git_toplevel(str(root))
    problems: list[DiscoveryProblem] = []
    reader = Reader(Path(top), problems.append)
    inventory = enumerate_inventory(top, _prefixes(scopes), assume_ignored)
    problems += _symlink_problems(top, inventory)
    directories = manifest_dirs(reader, inventory)
    sources = classify_sources(top, inventory.files)
    root_makefile = bool(makefile_scripts(reader, ".", inventory.names.get(".", frozenset())))
    layout = build_layout(directories, inventory, sources, root_makefile)
    ctx = Context(top, reader, inventory.names)
    built = [build_scope(ctx, draft) for _, draft in sorted(layout.drafts.items())]
    extra = {language: list(items) for language, items in layout.uncovered.items()}
    if deno := deno_configs(inventory.files):
        extra["deno"] = deno
    return Discovery(
        root=Path(top),
        scopes=tuple(drop_inherited_tools(built)),
        unknown=unknown_languages(inventory.files, extra),
        problems=tuple(problems),
        skipped_symlinks=inventory.symlinks,
        file_count=inventory.total,
        toml_parser=toml_available(),
        source_files=sources,
        enumerated_files=inventory.files,
        ignored_dirs=ignored_directories(top),
        tracked_ignored=tracked_ignored_files(top, inventory.files),
    )
