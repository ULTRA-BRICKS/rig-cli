"""Scope facts the providers decide on: tool families, which roles are really configured, scope chains.

Authority rule (decision D3, amended by acceptance review F2): a role (lint / format / typecheck) counts as
configured when the scope or an ancestor holds a tool that can actually run for it -- a config file, a
config field, or a script that invokes it; a dependency alone is enough only for tools that run without a
config file (Prettier, Black, Biome, Oxlint, ...). ESLint 9 refuses to run without a config, so a
declared-but-unconfigured ESLint does NOT suppress the bootstrap default: otherwise a repo would get no lint
check at all while looking "configured".

Pure functions over ``Discovery`` facts; no I/O.
"""

from __future__ import annotations

import posixpath
from collections.abc import Mapping, Sequence

from .lint_discovery_model import (
    LANG_GO,
    LANG_JS,
    LANG_PY,
    LANG_RUST,
    LANG_TS,
    ConfiguredTool,
    Discovery,
    PackageManager,
    ProjectScope,
    Role,
    TypeScriptInfo,
)
from .lint_providers_model import Family, PlanContext

FAMILY_LANGS: Mapping[Family, frozenset[str]] = {
    "js": frozenset({LANG_JS, LANG_TS}),
    "py": frozenset({LANG_PY}),
    "rust": frozenset({LANG_RUST}),
    "go": frozenset({LANG_GO}),
}
FAMILY_ORDER: tuple[Family, ...] = ("js", "py", "rust", "go")
TYPECHECK_LANG: Mapping[Family, str] = {"js": LANG_TS, "py": LANG_PY}

# Roles a configured tool is understood to fill. Open vocabulary: unknown tools only count through the
# role discovery attached to them.
TOOL_ROLES: Mapping[str, tuple[Role, ...]] = {
    "eslint": ("lint",),
    "oxlint": ("lint",),
    "tslint": ("lint",),
    "xo": ("lint",),
    "standard": ("lint", "format"),
    "biome": ("lint", "format"),
    "prettier": ("format",),
    "oxfmt": ("format",),
    "dprint": ("format",),
    "tsc": ("typecheck",),
    "ruff": ("lint", "format"),
    "flake8": ("lint",),
    "pylint": ("lint",),
    "black": ("format",),
    "mypy": ("typecheck",),
    "pyright": ("typecheck",),
}
TOOL_FAMILY: Mapping[str, Family] = {
    **dict.fromkeys(("eslint", "oxlint", "tslint", "xo", "standard", "biome", "prettier", "oxfmt", "dprint", "tsc"), "js"),
    **dict.fromkeys(("ruff", "flake8", "pylint", "black", "mypy", "pyright"), "py"),
}
# Not a tool of their own: discovery may report them next to the tools Rig does run.
COMPANION_TOOLS = frozenset({"oxlint-tsgolint", "oxlint-plugins", "typescript"})
# Tools that cannot run at all without a config file.
NEEDS_CONFIG = frozenset({"eslint"})


def keys(role: str, langs: Sequence[str], scope_rel: str) -> frozenset[str]:
    return frozenset(f"{role}:{lang}:{scope_rel}" for lang in langs)


def scope_chain(discovery: Discovery, scope: ProjectScope) -> tuple[ProjectScope, ...]:
    """Nearest first, including ``scope`` itself (the synthetic root scope is not in the discovery)."""
    if discovery.scope(scope.rel) is None:
        return (scope,)
    return discovery.ancestors(scope.rel)


def descendants(discovery: Discovery, scope: ProjectScope) -> tuple[ProjectScope, ...]:
    prefix = "" if scope.rel == "." else scope.rel + "/"
    return tuple(s for s in discovery.scopes if s.rel != scope.rel and s.rel.startswith(prefix))


def has_family(scope: ProjectScope, family: Family) -> bool:
    return bool(FAMILY_LANGS[family] & set(scope.languages))


def has_config(tool: ConfiguredTool) -> bool:
    return bool(tool.config_files) or any(e.kind in ("config", "field") for e in tool.evidence)


def is_configured(tool: ConfiguredTool) -> bool:
    """Whether ``tool`` can really run for its role (see the module docstring)."""
    if tool.tool in NEEDS_CONFIG:
        return has_config(tool) or bool(tool.scripts)
    return True


def tool_family(tool: ConfiguredTool, owner: ProjectScope) -> Family | None:
    known = TOOL_FAMILY.get(tool.tool)
    if known is not None:
        return known
    for family in ("js", "py"):
        if has_family(owner, family):
            return family
    return None


def tools_in(scope: ProjectScope, family: Family) -> tuple[ConfiguredTool, ...]:
    return tuple(t for t in scope.tools if tool_family(t, scope) == family)


def roles_of(tools: Sequence[ConfiguredTool]) -> frozenset[str]:
    roles: set[str] = set()
    for tool in tools:
        if is_configured(tool):
            roles.update(TOOL_ROLES.get(tool.tool, ()))
            roles.add(tool.role)
    return frozenset(roles)


def chain_roles(discovery: Discovery, scope: ProjectScope, family: Family) -> frozenset[str]:
    roles: set[str] = set()
    for member in scope_chain(discovery, scope):
        roles |= roles_of(tools_in(member, family))
    return frozenset(roles)


def own_tool(scope: ProjectScope, name: str) -> ConfiguredTool | None:
    return next((t for t in scope.tools if t.tool == name), None)


def chain_tool(discovery: Discovery, scope: ProjectScope, name: str) -> tuple[ProjectScope, ConfiguredTool] | None:
    for member in scope_chain(discovery, scope):
        found = own_tool(member, name)
        if found is not None:
            return member, found
    return None


def _descendant_configures(discovery: Discovery, scope: ProjectScope, family: Family, role: Role) -> bool:
    return any(
        has_family(below, family) and role in roles_of(tools_in(below, family))
        for below in descendants(discovery, scope)
    )


def gets_default(discovery: Discovery, scope: ProjectScope, family: Family, role: Role) -> bool:
    """True when Rig's bootstrap default fills ``role`` for ``scope``.

    The default lands on the topmost unconfigured scope of the family so a workspace root gets one
    Oxlint/Ruff for the whole tree; it is withheld where a descendant already configures the role (the
    project's own tool is authoritative there, and a root-level default would lint those files twice).
    """
    if not has_family(scope, family) or role in chain_roles(discovery, scope, family):
        return False
    if _descendant_configures(discovery, scope, family, role):
        return False
    return not any(gets_default(discovery, above, family, role) for above in scope_chain(discovery, scope)[1:])


def typescript_info(ctx: PlanContext) -> TypeScriptInfo | None:
    """The TypeScript facts that apply to the scope: nearest one with a resolved version, else nearest declared."""
    infos = [m.typescript for m in scope_chain(ctx.discovery, ctx.scope) if m.typescript is not None]
    infos = [i for i in infos if i.declared_spec or i.tsconfigs or i.major is not None]
    return next((i for i in infos if i.major is not None), infos[0] if infos else None)


def typescript_declared(ctx: PlanContext) -> bool:
    return any(m.typescript is not None and m.typescript.declared_spec for m in scope_chain(ctx.discovery, ctx.scope))


def type_aware_active(ctx: PlanContext, oxlint: ConfiguredTool) -> bool:
    """Oxlint's own TypeScript diagnostics are only trusted when the type-aware prerequisites hold (D10)."""
    if oxlint.options.get("type_check") != "true":
        return False
    ts = typescript_info(ctx)
    if ts is None or ts.major is None or ts.major < 7:
        return False
    return any(chain_tool(ctx.discovery, ctx.scope, name) for name in ("oxlint-tsgolint", "tsgolint"))


def manager_for(scope: ProjectScope, discovery: Discovery, ecosystem: str) -> PackageManager | None:
    for member in scope_chain(discovery, scope):
        for pm in (member.package_manager, *member.other_managers):
            if pm is not None and pm.ecosystem == ecosystem:
                return pm
    return None


def manager_name(scope: ProjectScope, discovery: Discovery, ecosystem: str) -> str | None:
    pm = manager_for(scope, discovery, ecosystem)
    return None if pm is None else pm.name


def scope_rel_path(scope: ProjectScope, repo_rel: str) -> str:
    return repo_rel if scope.rel == "." else posixpath.relpath(repo_rel, scope.rel)


def family_langs(scope: ProjectScope, family: Family) -> tuple[str, ...]:
    present = tuple(sorted(FAMILY_LANGS[family] & set(scope.languages)))
    return present or tuple(sorted(FAMILY_LANGS[family]))[:1]


def role_keys(scope: ProjectScope, family: Family, role: Role) -> frozenset[str]:
    if role == "typecheck":
        lang = TYPECHECK_LANG.get(family)
        return keys(role, (lang,), scope.rel) if lang else frozenset()
    return keys(role, family_langs(scope, family), scope.rel)
