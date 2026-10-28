"""JavaScript/TypeScript facts of one project directory for the lint discovery.

Context: config files are matched by NAME only (a flat or `.eslintrc.js` config is JavaScript that is
never imported or run here); package.json is strict JSON, `.oxlintrc.json(c)` goes through the JSONC
parser, and the single fact Rig needs out of `oxlint.config.ts` (`typeCheck: true`) comes from one
bounded regex over a size-capped read. The installed TypeScript version is read from
`node_modules/typescript/package.json` as JSON; a pnpm store link is followed only when it resolves
inside the repository root.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from typing import Mapping

from .lint_discovery_io import Reader
from .lint_discovery_model import Evidence, PackageManager, ProjectScript, TypeScriptInfo, join
from .lint_discovery_scripts import TOOL_TOKENS, classify, package_scripts, tokenize
from .lint_discovery_tools import ToolCollector

_CONFIG_PATTERNS: Mapping[str, tuple[re.Pattern[str], ...]] = {
    "eslint": (re.compile(r"^eslint\.config\.(js|mjs|cjs|ts|mts|cts)$"), re.compile(r"^\.eslintrc(\.(js|cjs|json|yaml|yml))?$")),
    "biome": (re.compile(r"^biome\.jsonc?$"),),
    "prettier": (re.compile(r"^\.prettierrc(\.(json|json5|yaml|yml|toml|js|cjs|mjs|ts))?$"),
                 re.compile(r"^prettier\.config\.(js|cjs|mjs|ts|mts|cts)$")),
    "oxlint": (re.compile(r"^\.oxlintrc\.jsonc?$"), re.compile(r"^oxlint\.config\.(ts|mts)$")),
    "oxfmt": (re.compile(r"^\.oxfmtrc\.jsonc?$"),),
    "dprint": (re.compile(r"^\.?dprint\.jsonc?$"),),
    "tslint": (re.compile(r"^tslint\.(json|yaml|yml)$"),),
    "xo": (re.compile(r"^xo\.config\.(js|cjs|mjs|ts)$"), re.compile(r"^\.xo-config(\.(js|cjs|json|yaml|yml))?$")),
}
_MANIFEST_FIELDS: Mapping[str, str] = {"eslintConfig": "eslint", "prettier": "prettier", "standard": "standard", "xo": "xo"}
_DEP_TOOLS: Mapping[str, str] = {
    "eslint": "eslint", "@biomejs/biome": "biome", "prettier": "prettier", "oxlint": "oxlint", "oxfmt": "oxfmt",
    "dprint": "dprint", "tslint": "tslint", "xo": "xo", "standard": "standard",
    "oxlint-tsgolint": "oxlint-tsgolint", "tsgolint": "oxlint-tsgolint", "@oxlint/plugins": "oxlint-plugins",
}
_DEP_BLOCKS = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")
_JS_LOCKS = (("package-lock.json", "npm"), ("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("bun.lock", "bun"), ("bun.lockb", "bun"))
_LOCK_OF = {"npm": "package-lock.json", "pnpm": "pnpm-lock.yaml", "yarn": "yarn.lock", "yarn-berry": "yarn.lock", "bun": "bun.lock"}
_FIELD = re.compile(r"^(npm|pnpm|yarn|bun)@(\d+)")
_TYPECHECK_TS = re.compile(r"\btypeCheck\s*:\s*true\b")
_COMMENTS = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)
_TS_CONFIG = re.compile(r"^tsconfig.*\.json$")
MAX_CONFIG_TEXT_BYTES = 512 * 1024


@dataclass(frozen=True)
class PackageJsonFacts:
    scripts: tuple[ProjectScript, ...]
    declared: Mapping[str, str]            # dependency name -> spec


def config_tool(name: str) -> str | None:
    for tool, patterns in _CONFIG_PATTERNS.items():
        if any(p.match(name) for p in patterns):
            return tool
    return None


def tsconfigs_in(directory: str, names: frozenset[str]) -> tuple[str, ...]:
    return tuple(join(directory, n) for n in sorted(names) if _TS_CONFIG.match(n))


def _json_type_check(reader: Reader, rel: str) -> bool:
    document = reader.jsonc(rel)
    options = document.get("options") if isinstance(document, dict) else None
    return isinstance(options, dict) and options.get("typeCheck") is True


def _ts_type_check(reader: Reader, rel: str) -> bool:
    text = reader.text(rel, limit=MAX_CONFIG_TEXT_BYTES)
    return text is not None and bool(_TYPECHECK_TS.search(_COMMENTS.sub("", text)))


def _oxlint_type_check(reader: Reader, rel: str) -> bool:
    return _ts_type_check(reader, rel) if rel.endswith((".ts", ".mts")) else _json_type_check(reader, rel)


def collect_config_files(reader: Reader, directory: str, names: frozenset[str], collector: ToolCollector) -> None:
    """Tool config files that sit in ``directory`` (by name; their content is never executed)."""
    for name in sorted(names):
        tool = config_tool(name)
        if tool is None:
            continue
        rel = join(directory, name)
        collector.config(tool, rel)
        if tool == "oxlint" and _oxlint_type_check(reader, rel):
            collector.option("oxlint", "type_check", "true")


def dependency_specs(manifest: Mapping[str, object]) -> dict[str, str]:
    """Declared dependency name -> version spec over the four dependency blocks (first block wins)."""
    found: dict[str, str] = {}
    for block in _DEP_BLOCKS:
        deps = manifest.get(block)
        if isinstance(deps, dict):
            for name, spec in deps.items():
                found.setdefault(str(name), spec if isinstance(spec, str) else "")
    return found


def _declare_dependencies(rel: str, declared: Mapping[str, str], collector: ToolCollector) -> None:
    for name, spec in sorted(declared.items()):
        tool = _DEP_TOOLS.get(name)
        if tool:
            collector.declared(tool, rel, f"{name}@{spec}" if spec else name)


def _script_mentions(rel: str, scripts: Mapping[str, str], collector: ToolCollector) -> list[str]:
    """Feed EVERY script (not only allow-listed names) into the tools it mentions; return the tsc-mentioning names."""
    tsc_scripts: list[str] = []
    for name, command in sorted(scripts.items()):
        tokens = set(tokenize(command))
        for tool, token in TOOL_TOKENS.items():
            if token not in tokens:
                continue
            if tool == "tsc":
                tsc_scripts.append(name)
            else:
                collector.script(tool, name, rel)
    return tsc_scripts


def _tsc_row(rel: str, declared: Mapping[str, str], tsc_scripts: list[str], configs: tuple[str, ...], collector: ToolCollector) -> None:
    """`tsc` is a tool of the scope only when a tsconfig exists and TypeScript is declared or a script runs it."""
    if not configs or not ("typescript" in declared or tsc_scripts):
        return
    for config in configs:
        collector.config("tsc", config)
    if "typescript" in declared:
        collector.declared("tsc", rel, "typescript")
    for name in tsc_scripts:
        collector.script("tsc", name, rel)


def collect_package_json(rel: str, manifest: Mapping[str, object], configs: tuple[str, ...], collector: ToolCollector) -> PackageJsonFacts:
    for field, tool in _MANIFEST_FIELDS.items():
        if field in manifest:
            collector.config(tool, rel, detail=field, kind="field")
    declared = dependency_specs(manifest)
    _declare_dependencies(rel, declared, collector)
    scripts = package_scripts(manifest)
    _tsc_row(rel, declared, _script_mentions(rel, scripts, collector), configs, collector)
    classified = (classify("package.json", name, command) for name, command in sorted(scripts.items()))
    return PackageJsonFacts(tuple(s for s in classified if s is not None), declared)


def _manager_root(reader: Reader, names_by_dir: Mapping[str, frozenset[str]], directory: str) -> str:
    """Nearest ancestor-or-self directory that owns a lockfile, pnpm-workspace.yaml or a `workspaces` field."""
    current = directory
    while True:
        names = names_by_dir.get(current, frozenset())
        if names & {f for f, _ in _JS_LOCKS} or "pnpm-workspace.yaml" in names or _has_workspaces(reader, current, names):
            return current
        if current == ".":
            return directory
        current = current.rsplit("/", 1)[0] if "/" in current else "."


def _has_workspaces(reader: Reader, directory: str, names: frozenset[str]) -> bool:
    if "package.json" not in names:
        return False
    manifest = reader.json(join(directory, "package.json"))
    return manifest is not None and "workspaces" in manifest


def _field_manager(reader: Reader, directory: str, names_by_dir: Mapping[str, frozenset[str]]) -> tuple[str, str] | None:
    """(manager name, evidence detail) from the `packageManager` field of the package.json in ``directory``."""
    if "package.json" not in names_by_dir.get(directory, frozenset()):
        return None
    manifest = reader.json(join(directory, "package.json"))
    value = manifest.get("packageManager") if manifest else None
    match = _FIELD.match(value) if isinstance(value, str) else None
    if match is None:
        return None
    name = "yarn-berry" if match.group(1) == "yarn" and int(match.group(2)) >= 2 else match.group(1)
    return name, f"packageManager={value}"


def _lock_for(name: str, root: str, root_names: frozenset[str]) -> str | None:
    candidates = [file for file, manager in _JS_LOCKS if manager == name.replace("yarn-berry", "yarn")]
    present = [file for file in candidates if file in root_names]
    return join(root, present[0]) if present else None


def _manager_from_field(field: tuple[str, str], owner: str, root: str, root_names: frozenset[str]) -> tuple[PackageManager, tuple[Evidence, ...]]:
    name, detail = field
    lock = _lock_for(name, root, root_names)
    evidence = [Evidence(owner, "field", detail)] + ([Evidence(lock, "lockfile")] if lock else [])
    return PackageManager("js", name, "packageManager field", lock, root), tuple(evidence)


def _manager_from_markers(root: str, root_names: frozenset[str]) -> tuple[PackageManager, tuple[Evidence, ...]]:
    found = sorted({manager for file, manager in _JS_LOCKS if file in root_names})
    if len(found) > 1:
        return PackageManager("js", "", "ambiguous", None, root, tuple(found)), ()
    if found:
        name = "yarn-berry" if found[0] == "yarn" and ".yarnrc.yml" in root_names else found[0]
        lock = _lock_for(name, root, root_names)
        return PackageManager("js", name, "lockfile", lock, root), (Evidence(lock or root, "lockfile"),)
    if "pnpm-workspace.yaml" in root_names:
        return PackageManager("js", "pnpm", "workspace marker", None, root), (Evidence(join(root, "pnpm-workspace.yaml"), "config"),)
    return PackageManager("js", "npm", "default", None, root), ()


def select_js_manager(reader: Reader, names_by_dir: Mapping[str, frozenset[str]], directory: str) -> tuple[PackageManager, tuple[Evidence, ...]]:
    """Decision D7: packageManager field, then exactly one lockfile, then pnpm-workspace.yaml, then ambiguity, then npm."""
    root = _manager_root(reader, names_by_dir, directory)
    root_names = names_by_dir.get(root, frozenset())
    own = _field_manager(reader, directory, names_by_dir)
    field = own or _field_manager(reader, root, names_by_dir)
    if field:
        return _manager_from_field(field, join(directory if own else root, "package.json"), root, root_names)
    return _manager_from_markers(root, root_names)


def _resolved_typescript_manifest(root: str, directory: str) -> str | None:
    """Repo-relative ``package.json`` of the installed TypeScript, following links only while they stay inside the repository.

    pnpm links `node_modules/typescript` into `node_modules/.pnpm/...`; a link that resolves outside the
    repository root (or a dangling one) is never read.
    """
    link = os.path.join(root, directory, "node_modules", "typescript") if directory != "." else os.path.join(root, "node_modules", "typescript")
    real = os.path.realpath(link)
    if os.path.commonpath([root, real]) != root or real == root:
        return None
    rel = os.path.relpath(os.path.join(real, "package.json"), root).replace(os.sep, "/")
    try:
        return rel if stat.S_ISREG(os.lstat(os.path.join(root, rel)).st_mode) else None
    except OSError:
        return None


def _installed_version(reader: Reader, root: str, directory: str) -> str | None:
    rel = _resolved_typescript_manifest(root, directory)
    manifest = reader.json(rel) if rel else None
    version = manifest.get("version") if manifest else None
    return version if isinstance(version, str) else None


def typescript_info(reader: Reader, root: str, directory: str, manager_root: str, facts: PackageJsonFacts | None,
                    tsconfigs: tuple[str, ...]) -> TypeScriptInfo | None:
    declared = facts.declared.get("typescript") if facts else None
    if declared is None and not tsconfigs:
        return None
    version = _installed_version(reader, root, directory)
    if version is None and manager_root != directory:
        version = _installed_version(reader, root, manager_root)
    major = int(version.split(".", 1)[0]) if version and version.split(".", 1)[0].isdigit() else None
    return TypeScriptInfo(declared, version, major, tsconfigs)


def is_js_config_name(name: str) -> bool:
    return config_tool(name) is not None
