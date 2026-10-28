"""Turns a ``Draft`` (a directory plus the files it owns) into a frozen ``ProjectScope`` for the lint discovery.

Context: this is where manifests, config names, dependency declarations, scripts and the file
ownership computed by ``lint_discovery_ownership`` are combined. The JS and Python halves are
independent and each contributes tools, a package manager and evidence; a directory holding both keeps
the JS manager as primary and reports the Python one in ``other_managers``. Nothing here executes
repository code.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterator, Mapping

from .lint_discovery_io import Reader
from .lint_discovery_js import (PackageJsonFacts, collect_config_files, collect_package_json, select_js_manager,
                                tsconfigs_in, typescript_info)
from .lint_discovery_model import ConfiguredTool, Evidence, PackageManager, ProjectScope, ProjectScript, TypeScriptInfo, join
from .lint_discovery_ownership import Draft, manifest_names
from .lint_discovery_python import collect_python, select_python_manager
from .lint_discovery_scripts import MAX_MAKEFILE_BYTES, classify, makefile_targets
from .lint_discovery_tools import ToolCollector

_FRAMEWORK_DEPENDENCIES: Mapping[str, str] = {
    "react": "react", "react-dom": "react", "next": "next", "vue": "vue", "nuxt": "nuxt",
    "svelte": "svelte", "@sveltejs/kit": "svelte",
}
_FRAMEWORK_EXTENSIONS: Mapping[str, str] = {".tsx": "react", ".jsx": "react", ".vue": "vue", ".svelte": "svelte"}
_IMPLIED: Mapping[str, str] = {"next": "react", "nuxt": "vue"}


@dataclass(frozen=True)
class Context:
    root: str
    reader: Reader
    names_by_dir: Mapping[str, frozenset[str]]


@dataclass(frozen=True)
class JsPart:
    manager: PackageManager
    evidence: tuple[Evidence, ...]
    typescript: TypeScriptInfo | None
    facts: PackageJsonFacts | None


def makefile_scripts(reader: Reader, directory: str, names: frozenset[str]) -> tuple[ProjectScript, ...]:
    if "Makefile" not in names:
        return ()
    text = reader.text(join(directory, "Makefile"), limit=MAX_MAKEFILE_BYTES)
    if text is None:
        return ()
    scripts = (classify("Makefile", name, command) for name, command in sorted(makefile_targets(text).items()))
    return tuple(s for s in scripts if s is not None)


def _makefile_mentions(rel: str, scripts: tuple[ProjectScript, ...], collector: ToolCollector) -> None:
    """Allow-listed Makefile targets are the only Makefile text read, and they count as script mentions of the tools they run."""
    for script in scripts:
        for tool in script.covers_tools:
            if tool != "tsc":
                collector.script(tool, script.name, rel)


def _js_part(ctx: Context, directory: str, names: frozenset[str], collector: ToolCollector) -> JsPart:
    rel = join(directory, "package.json")
    manifest = ctx.reader.json(rel)
    configs = tsconfigs_in(directory, names)
    facts = collect_package_json(rel, manifest, configs, collector) if manifest is not None else None
    manager, evidence = select_js_manager(ctx.reader, ctx.names_by_dir, directory)
    info = typescript_info(ctx.reader, ctx.root, directory, manager.root, facts, configs)
    return JsPart(manager, evidence, info, facts)


def _languages(draft: Draft, js: JsPart | None) -> tuple[str, ...]:
    found = set(draft.orphans)
    if draft.has_python:
        found.add("python")
    if "go.mod" in draft.manifests:
        found.add("go")
    if "Cargo.toml" in draft.manifests:
        found.add("rust")
    if js is not None:
        found |= _js_languages(draft, js)
    return tuple(sorted(found))


def _js_languages(draft: Draft, js: JsPart) -> set[str]:
    typed = "typescript" in draft.owned or js.typescript is not None
    found = {"typescript"} if typed else set()
    if "javascript" in draft.owned or not typed:
        found.add("javascript")
    return found


def frameworks_of(js: JsPart | None, extensions: set[str]) -> tuple[str, ...]:
    found = {_FRAMEWORK_EXTENSIONS[e] for e in extensions}
    declared = js.facts.declared if js is not None and js.facts is not None else {}
    found |= {framework for name, framework in _FRAMEWORK_DEPENDENCIES.items() if name in declared}
    found |= {_IMPLIED[f] for f in found if f in _IMPLIED}
    return tuple(sorted(found))


def _managers(js: JsPart | None, python: tuple[PackageManager, tuple[Evidence, ...]] | None) -> tuple[PackageManager | None, tuple[PackageManager, ...]]:
    if js is not None:
        return js.manager, (python[0],) if python else ()
    return (python[0] if python else None), ()


def build_scope(ctx: Context, draft: Draft) -> ProjectScope:
    names = ctx.names_by_dir.get(draft.rel, frozenset())
    collector = ToolCollector()
    collect_config_files(ctx.reader, draft.rel, names, collector)
    tables = collect_python(ctx.reader, draft.rel, names, collector)
    js = _js_part(ctx, draft.rel, names, collector) if draft.has_js else None
    python = select_python_manager(ctx.names_by_dir, draft.rel, tables) if draft.has_python else None
    makefile = makefile_scripts(ctx.reader, draft.rel, names)
    _makefile_mentions(join(draft.rel, "Makefile"), makefile, collector)
    primary, others = _managers(js, python)
    evidence = [Evidence(join(draft.rel, n), "manifest") for n in sorted(manifest_names(names))]
    evidence += (js.evidence if js else ()) + (python[1] if python else ()) + tuple(draft.orphan_evidence())
    scripts = (js.facts.scripts if js and js.facts else ()) + makefile
    return ProjectScope(
        rel=draft.rel, languages=_languages(draft, js), package_manager=primary, tools=collector.build(),
        scripts=scripts, typescript=js.typescript if js else None,
        files={key: tuple(sorted(items)) for key, items in sorted(draft.files.items())},
        evidence=tuple(evidence), other_managers=others, frameworks=frameworks_of(js, draft.extensions),
    )


def _strict_ancestors(rel: str) -> Iterator[str]:
    current = rel
    while current != ".":
        current = current.rsplit("/", 1)[0] if "/" in current else "."
        yield current


def _inherited(tool: ConfiguredTool, ancestors: list[ProjectScope]) -> bool:
    """A tool the scope merely re-declares while an ancestor already owns its config files (monorepo root config)."""
    if tool.config_files or tool.scripts:
        return False
    return any(t.tool == tool.tool and t.role == tool.role and t.config_files for a in ancestors for t in a.tools)


def drop_inherited_tools(scopes: list[ProjectScope]) -> list[ProjectScope]:
    by_rel = {s.rel: s for s in scopes}
    result = []
    for scope in scopes:
        ancestors = [by_rel[a] for a in _strict_ancestors(scope.rel) if a in by_rel]
        result.append(replace(scope, tools=tuple(t for t in scope.tools if not _inherited(t, ancestors))))
    return result
