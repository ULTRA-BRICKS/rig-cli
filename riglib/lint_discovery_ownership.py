"""Which directories are project scopes, and which scope owns which file, for the lint discovery.

Context: a scope is a directory that holds a manifest. Every supported-language source file must end up
somewhere (decision F7): under a scope that already speaks its language, or else as an ORPHAN that adds
the language (with source evidence) to the nearest enclosing scope, creating a synthetic "." scope when
there is none. Go and Rust files with no Go/Cargo scope are never scoped; they are returned for the
"unknown language" report because nothing can lint them. Only names and the file inventory are used.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Callable, Iterator, Mapping

from .lint_discovery_files import Inventory, directory_of
from .lint_discovery_io import Reader, load_toml
from .lint_discovery_model import Evidence, join
from .lint_discovery_python import is_python_manifest

_MANIFESTS = frozenset({"package.json", "pyproject.toml", "setup.py", "setup.cfg", "go.mod", "Cargo.toml"})
_WORKFLOW = re.compile(r"^\.github/workflows/[^/]+\.ya?ml$")
_FRAMEWORK_EXTENSIONS = frozenset({".tsx", ".jsx", ".vue", ".svelte"})


@dataclass
class Draft:
    """Mutable scope under construction; ``scopes.build_scope`` turns it into a frozen ``ProjectScope``."""

    rel: str
    manifests: frozenset[str]
    owned: set[str] = field(default_factory=set)               # javascript/typescript/python files an existing manifest covers
    orphans: dict[str, list[str]] = field(default_factory=dict)  # language -> files that forced the language onto this scope
    files: dict[str, list[str]] = field(default_factory=dict)  # shell/go/github-workflows
    extensions: set[str] = field(default_factory=set)          # framework file extensions seen (.tsx .vue ...)

    @property
    def has_js(self) -> bool:
        return "package.json" in self.manifests

    @property
    def has_python(self) -> bool:
        return any(is_python_manifest(n) for n in self.manifests)

    def orphan_evidence(self) -> list[Evidence]:
        return [
            Evidence(items[0], "source", f"{len(items)} {language} source file(s) outside any {language} project manifest")
            for language, items in sorted(self.orphans.items())
        ]


@dataclass(frozen=True)
class Layout:
    drafts: Mapping[str, Draft]
    uncovered: Mapping[str, list[str]]    # go/rust files with no Go module / Cargo scope


def manifest_names(names: frozenset[str]) -> frozenset[str]:
    return frozenset(n for n in names if n in _MANIFESTS or is_python_manifest(n))


def _ancestors(directory: str) -> Iterator[str]:
    """``directory`` itself, then each parent up to "." (nearest first)."""
    current = directory
    while True:
        yield current
        if current == ".":
            return
        current = current.rsplit("/", 1)[0] if "/" in current else "."


def _strict_ancestors(directory: str) -> list[str]:
    return list(_ancestors(directory))[1:]


def _is_cargo_workspace(reader: Reader, directory: str) -> bool:
    return "workspace" in load_toml(reader, join(directory, "Cargo.toml")).tables


def _cargo_member(found: Mapping[str, frozenset[str]], workspaces: set[str], directory: str) -> bool:
    return "Cargo.toml" in found[directory] and directory not in workspaces and any(a in workspaces for a in _strict_ancestors(directory))


def _collect_manifests(inventory: Inventory) -> dict[str, frozenset[str]]:
    found: dict[str, frozenset[str]] = {}
    for directory, names in inventory.names.items():
        manifests = manifest_names(names)
        if manifests:
            found[directory] = manifests
    return found


def manifest_dirs(reader: Reader, inventory: Inventory) -> dict[str, frozenset[str]]:
    """Directory -> manifest file names; Cargo workspace members are not scopes of their own."""
    found = _collect_manifests(inventory)
    workspaces = {d for d, m in found.items() if "Cargo.toml" in m and _is_cargo_workspace(reader, d)}
    members = [d for d in found if _cargo_member(found, workspaces, d)]
    trimmed = {d: (found[d] - {"Cargo.toml"} if d in members else found[d]) for d in found}
    return {d: m for d, m in trimmed.items() if m}


def _nearest(drafts: Mapping[str, Draft], directory: str, accepts: Callable[[Draft], bool] = lambda _d: True) -> Draft | None:
    return next((drafts[a] for a in _ancestors(directory) if a in drafts and accepts(drafts[a])), None)


def _enclosing(drafts: Mapping[str, Draft], rel: str) -> Draft:
    """Nearest scope containing ``rel``; the synthetic "." scope guarantees one exists for every orphanable file."""
    found = _nearest(drafts, directory_of(rel))
    assert found is not None
    return found


def _place_python(drafts: Mapping[str, Draft], rel: str) -> None:
    covering = _nearest(drafts, directory_of(rel), lambda d: d.has_python)
    if covering is not None:
        covering.owned.add("python")
        return
    _enclosing(drafts, rel).orphans.setdefault("python", []).append(rel)


def _place_js(drafts: Mapping[str, Draft], language: str, rel: str) -> None:
    covering = _nearest(drafts, directory_of(rel), lambda d: d.has_js)
    if covering is not None:
        covering.owned.add(language)
        return
    _enclosing(drafts, rel).orphans.setdefault(language, []).append(rel)


def _place_covered(drafts: Mapping[str, Draft], language: str, rel: str, manifest: str, uncovered: dict[str, list[str]]) -> None:
    covering = _nearest(drafts, directory_of(rel), lambda d: manifest in d.manifests)
    if covering is None:
        uncovered.setdefault(language, []).append(rel)
    elif language == "go":
        covering.files.setdefault("go", []).append(rel)


def _place(drafts: Mapping[str, Draft], language: str, rel: str, uncovered: dict[str, list[str]]) -> None:
    if language == "python":
        _place_python(drafts, rel)
    elif language in ("javascript", "typescript"):
        _place_js(drafts, language, rel)
    elif language == "shell":
        _enclosing(drafts, rel).files.setdefault("shell", []).append(rel)
    elif language == "go":
        _place_covered(drafts, "go", rel, "go.mod", uncovered)
    else:
        _place_covered(drafts, "rust", rel, "Cargo.toml", uncovered)


def _needs_root_scope(directories: Mapping[str, frozenset[str]], sources: Mapping[str, tuple[str, ...]],
                      files: tuple[str, ...], root_makefile: bool) -> bool:
    if "." in directories:
        return False
    if root_makefile or any(_WORKFLOW.match(rel) for rel in files):
        return True
    orphanable = [rel for lang in ("python", "javascript", "typescript", "shell") for rel in sources.get(lang, ())]
    return any(not any(a in directories for a in _ancestors(directory_of(rel))) for rel in orphanable)


def _note_framework_file(drafts: Mapping[str, Draft], rel: str) -> None:
    extension = os.path.splitext(rel)[1].lower()
    owner = _nearest(drafts, directory_of(rel))
    if extension in _FRAMEWORK_EXTENSIONS and owner is not None:
        owner.extensions.add(extension)


def build_layout(directories: Mapping[str, frozenset[str]], inventory: Inventory,
                 sources: Mapping[str, tuple[str, ...]], root_makefile: bool) -> Layout:
    """Assign every source file to a scope; see the module docstring for the orphan rules."""
    drafts = {d: Draft(d, manifests) for d, manifests in directories.items()}
    if _needs_root_scope(directories, sources, inventory.files, root_makefile):
        drafts["."] = Draft(".", frozenset())
    uncovered: dict[str, list[str]] = {}
    for language, items in sources.items():
        for rel in items:
            _place(drafts, language, rel, uncovered)
    for rel in inventory.files:
        if _WORKFLOW.match(rel):
            drafts["."].files.setdefault("github-workflows", []).append(rel)
        else:
            _note_framework_file(drafts, rel)
    return Layout(drafts, uncovered)
