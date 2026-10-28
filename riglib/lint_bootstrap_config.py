"""The Rig-owned Oxlint config a bootstrap writes next to a fresh JS install, and managed-artifact reporting.

Context: when bootstrap adds Oxlint to a scope that had no JS linter and the policy does NOT require
the anti-slop plugin, the base policy from ``linters.rules`` is rendered into ``oxlint.config.ts``
(``.oxlintrc.json`` on Node older than 22.18), with built-in plugins for the frameworks the scope
really uses (React, Next.js, Vue) and type-aware linting only when ``oxlint-tsgolint`` and
TypeScript >= 7 are both present. When anti-slop IS required nothing is written into the repository:
the policy and the vendored plugin live in the Rig tool cache (:mod:`riglib.lint_policy_cache`).
An existing Oxlint config of any flavor is user-authoritative: nothing is planned over it (the plan's
``on_conflict="skip"`` is the second guard). Planning is pure. Stdlib-only at import time.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Mapping

from .lint_bootstrap_managers import declared_names, dir_of
from .lint_bootstrap_model import BootstrapBlocked, BootstrapStep, Install
from .lint_discovery_model import Discovery, ProjectScope, contains, join
from .lint_policy_cache import anti_slop_wanted
from .lint_providers import PinnedPackage, ToolEnv
from .lint_run_config import LintConfig
from .plan import Action

if TYPE_CHECKING:
    from .catalog import Catalog

_OXLINT_CONFIGS = (".oxlintrc.json", ".oxlintrc.jsonc", "oxlint.config.ts", "oxlint.config.mts")
_TS_CONFIG_MIN_NODE = (22, 18, 0)
PLUGINS_PKG = "@oxlint/plugins"
_TSGOLINT_PKG = "oxlint-tsgolint"


@dataclass(frozen=True)
class ConfigPlan:
    steps: tuple[BootstrapStep, ...] = ()
    actions: tuple[Action, ...] = ()
    blocked: tuple[BootstrapBlocked, ...] = ()

    def merged(self, other: ConfigPlan) -> ConfigPlan:
        return ConfigPlan(self.steps + other.steps, self.actions + other.actions, self.blocked + other.blocked)


def existing_oxlint_config(directory: Path) -> list[str]:
    return [n for n in _OXLINT_CONFIGS if (directory / n).exists() or (directory / n).is_symlink()]


def type_aware(scope: ProjectScope, declared: set[str], wanted: Mapping[str, PinnedPackage]) -> bool:
    """D10: only TypeScript >= 7 with ``oxlint-tsgolint`` declared or being added."""
    major = scope.typescript.major if scope.typescript is not None else None
    return major is not None and major >= 7 and (_TSGOLINT_PKG in declared or _TSGOLINT_PKG in wanted)


def scope_frameworks(discovery: Discovery, scope_rel: str) -> tuple[str, ...]:
    """Frameworks detected anywhere under ``scope_rel``: its config governs every nested package."""
    return tuple(sorted({f for s in discovery.scopes if contains(scope_rel, s.rel) for f in s.frameworks}))


def resolve_policy_catalog() -> Catalog | None:
    """The agent-tools checkout ``rig apply`` would use (``RIG_AGENT_TOOLS_SOURCE`` / default candidates), or ``None``.

    Only ``Catalog.source`` is read by the Oxlint policy planning, so the catalog is not scanned.
    """
    from .catalog import Catalog, CatalogError, resolve_source

    try:
        return Catalog(source=resolve_source(None))
    except CatalogError:
        return None


# Oxlint lints script blocks only: these frameworks keep unlinted templates (oxc.rs/compatibility), so
# the Oxlint default alone is partial coverage and a configured template linter (ESLint, ...) stays needed.
_TEMPLATE_FRAMEWORKS = frozenset({"vue", "nuxt", "svelte"})


def _template_notes(frameworks: tuple[str, ...], plugins: tuple[str, ...]) -> list[str]:
    notes: list[str] = []
    partial = [f for f in frameworks if f in _TEMPLATE_FRAMEWORKS]
    if partial:
        notes.append(
            f"template-lint-uncovered: Oxlint checks only the script of {', '.join(partial)} files, not their templates; "
            "keep or add the framework's own template linter for full coverage"
        )
    if frameworks and not plugins:
        notes.append(f"no built-in Oxlint plugin for {', '.join(frameworks)}; generic defaults used")
    return notes


def _render_base(
    scope: ProjectScope, install: Install, discovery: Discovery, cfg: LintConfig, env: ToolEnv, notes: list[str]
) -> tuple[str, str] | None:
    """``(content, rel)`` of the base config, or ``None`` when the rules are invalid."""
    from .lint_discovery_jsx import automatic_jsx_runtime
    from .lint_policy import framework_plugins, render_oxlint_config

    declared = declared_names(discovery.root, scope, install.pm)
    if scope.typescript is not None and (scope.typescript.major or 0) >= 7 and not type_aware(scope, declared, install.wanted):
        notes.append(
            f"{scope.rel}: type-aware linting is disabled (TypeScript >= 7 but oxlint-tsgolint is not installed); "
            "the type-info rules are omitted and the rest of the policy is unchanged"
        )
    frameworks = scope_frameworks(discovery, scope.rel)
    plugins = framework_plugins(frameworks)
    notes.extend(_template_notes(frameworks, plugins))
    json_flavor = env.node_version is None or env.node_version < _TS_CONFIG_MIN_NODE
    flavor: Literal["ts", "json"] = "json" if json_flavor else "ts"
    try:
        content = render_oxlint_config(
            dict(cfg.linters_rules), flavor=flavor, type_aware=type_aware(scope, declared, install.wanted),
            plugins=plugins, automatic_jsx=automatic_jsx_runtime(discovery.root, scope.rel),
        )
    except ValueError as exc:
        notes.append(f"Oxlint config not written: invalid linters.rules ({exc})")
        return None
    return content, ".oxlintrc.json" if json_flavor else "oxlint.config.ts"


def _scope_config(
    scope: ProjectScope, install: Install, discovery: Discovery, cfg: LintConfig, env: ToolEnv, notes: list[str],
) -> ConfigPlan:
    existing = existing_oxlint_config(dir_of(discovery.root, scope.rel))
    if existing:
        notes.append(f"existing Oxlint config preserved: {', '.join(existing)}")
        return ConfigPlan()
    rendered = _render_base(scope, install, discovery, cfg, env, notes)
    if rendered is None:
        return ConfigPlan()
    content, name = rendered
    rel = join(scope.rel, name)
    step = BootstrapStep(f"write-config@{scope.rel}:{name}", "write-config", scope.rel, install.pm.name, (), ".", (), "n/a", (rel,))
    action = Action(
        kind="provision_linter_config", category="lint_bootstrap", item="rig-oxlint-policy",
        source=discovery.root, target=discovery.root,
        options={"tool": "oxlint", "role": "linter", "rel_path": rel, "content": content, "rig_owned": True},
    )
    return ConfigPlan((step,), (action,))


def plan_oxlint_configs(
    install: Install, discovery: Discovery, cfg: LintConfig, env: ToolEnv, notes: list[str]
) -> ConfigPlan:
    """Base config steps for every scope this install gives its first Oxlint; user configs are final.

    Nothing is written when the anti-slop policy is required: that policy is hosted in the tool cache
    (:func:`riglib.lint_bootstrap_policy.plan_policy`), never copied into the repository.
    """
    if "oxlint" not in install.wanted or not cfg.linters_enabled or anti_slop_wanted(cfg):
        return ConfigPlan()
    plan = ConfigPlan()
    for rel in sorted({n.scope for n in install.needs if n.tool == "oxlint"}):
        scope = discovery.scope(rel)
        if scope is not None:
            plan = plan.merged(_scope_config(scope, install, discovery, cfg, env, notes))
    return plan


def plan_cache_oxlint_config(scope: ProjectScope, discovery: Discovery, cfg: LintConfig, notes: list[str]) -> ConfigPlan:
    """The base Oxlint config for a scope whose Oxlint lives in the tool cache (no ``package.json``).

    Without a config Oxlint runs on its defaults, where findings are warnings and exit 0 (a false
    green). The JSON flavor needs no Node-evaluated config and no local ``oxlint`` package. Not planned
    when the anti-slop policy is required: that policy is passed to Oxlint from the tool cache instead.
    """
    from .lint_discovery_jsx import automatic_jsx_runtime
    from .lint_policy import framework_plugins, render_oxlint_config

    if not cfg.linters_enabled or anti_slop_wanted(cfg):
        return ConfigPlan()
    existing = existing_oxlint_config(dir_of(discovery.root, scope.rel))
    if existing:
        notes.append(f"existing Oxlint config preserved: {', '.join(existing)}")
        return ConfigPlan()
    frameworks = scope_frameworks(discovery, scope.rel)
    try:
        content = render_oxlint_config(
            dict(cfg.linters_rules), flavor="json", plugins=framework_plugins(frameworks),
            automatic_jsx=automatic_jsx_runtime(discovery.root, scope.rel),
        )
    except ValueError as exc:
        notes.append(f"Oxlint config not written: invalid linters.rules ({exc})")
        return ConfigPlan()
    rel = join(scope.rel, ".oxlintrc.json")
    step = BootstrapStep(f"write-config@{scope.rel}:.oxlintrc.json", "write-config", scope.rel, "cache", (), ".", (), "n/a", (rel,))
    action = Action(
        kind="provision_linter_config", category="lint_bootstrap", item="rig-oxlint-policy",
        source=discovery.root, target=discovery.root,
        options={"tool": "oxlint", "role": "linter", "rel_path": rel, "content": content, "rig_owned": True},
    )
    return ConfigPlan((step,), (action,))


def conflicting_config_blocks(discovery: Discovery) -> tuple[BootstrapBlocked, ...]:
    """A directory holding more than one Oxlint config flavor: Oxlint itself refuses to start (``Both
    '.oxlintrc.json' and 'oxlint.config.ts' found``), and picking one silently would lint against a
    policy nobody chose. Reported as an explicit ``conflicting-config`` for the user to resolve."""
    out: list[BootstrapBlocked] = []
    for scope in discovery.scopes:
        found = existing_oxlint_config(dir_of(discovery.root, scope.rel))
        if len(found) > 1:
            out.append(BootstrapBlocked(
                scope.rel, "oxlint", "conflicting-config",
                f"{', '.join(join(scope.rel, n) for n in found)} are all present; Oxlint allows exactly one config per "
                "directory -- delete the ones you do not use (Rig does not choose for you)",
            ))
    return tuple(out)
