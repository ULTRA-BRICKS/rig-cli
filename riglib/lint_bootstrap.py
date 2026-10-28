"""Plan and apply the repo-local tooling bootstrap that bare ``rig lint`` performs.

Context: ``lint_providers.bootstrap_needs`` says which Rig default tools a scope lacks (Oxlint +
Oxfmt for JS/TS, Ruff for Python, only for roles with no configured tool). This module turns those
needs into an :class:`~riglib.plan.InstallPlan` of ``lint_bootstrap_step`` actions and applies it
through ``actions.run_plan`` -- there is no second executor. Two routes, by project shape:

- JS/TS with a ``package.json`` and a JS package manager (npm/pnpm/yarn/bun): exact-pinned,
  lock-updating, scripts-disabled ``add`` through that manager, plus the existing
  ``provision_linter_config`` / ``provision_linter_bundle`` actions for the Rig-owned Oxlint
  policy (:mod:`lint_bootstrap_config`);
- everything else -- every Python scope (uv, poetry, pdm, pip, loose scripts) and JS without a
  ``package.json``: the tool is installed at its pin into the Rig tool cache
  (:mod:`lint_bootstrap_cache`), touching nothing in the repo. Python is deliberately never added
  to ``pyproject.toml``/the lock and never synced: a project's runtime dependencies can be heavy,
  native or unreachable, and linting must neither install them nor change them. A project that
  already declares Ruff keeps its own version (read from its lock) in the cache;
- system linters (:mod:`lint_bootstrap_system`): ShellCheck/shfmt/actionlint from sha256-pinned
  release assets into the same cache, clippy/rustfmt via ``rustup component add`` for the current
  toolchain. Go ships ``go vet``/``gofmt`` with its toolchain and needs nothing.

Trust boundary: planning is pure (reads manifests and stats the cache, never writes, never
spawns), so ``--plan``, ``--no-setup`` and CI can print exactly what would happen. Applying is the
only mutation. Under CI only tool-cache and toolchain-component steps run (they write no repository file); steps that edit
manifests, lockfiles or configs are refused. It never creates ``package.json``. A package manager is never substituted: the one the repo's own evidence
selects must be on PATH, otherwise the need is blocked with a remediation.

Stdlib-only at import time.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Mapping, Sequence

from .actions import run_plan
from .lint_ci_env import ci_env_truthy
from .lint_bootstrap_cache import USABLE_KINDS, CacheSteps, plan_cache_install
from .lint_bootstrap_config import (
    PLUGINS_PKG,
    ConfigPlan,
    conflicting_config_blocks,
    plan_cache_oxlint_config,
    plan_oxlint_configs,
    resolve_policy_catalog,
)
from .lint_bootstrap_policy import plan_policy
from .lint_bootstrap_managers import (
    LOCKFILE_DEFAULT,
    MANIFEST,
    declared_names,
    dir_of,
    frozen_install_argv,
    has_workspaces,
    read_npm_deps,
    manager_argv,
    manager_for,
    manifest_blocker,
    pin,
    route_js_manager,
    select_js_manager,
)
from .lint_bootstrap_model import (
    CI_SAFE_KINDS,
    BlockedReason,
    BootstrapBlocked,
    BootstrapOutcome,
    BootstrapPlan,
    BootstrapStep,
    Install,
    StepState,
)
from .lint_bootstrap_system import plan_system_install
from .lint_discovery_model import ConfiguredTool, Discovery, PackageManager, ProjectScope, join
from .lint_ignore import pending_entries
from .lint_locked_versions import locked_version
from .lint_providers import PINS, BootstrapNeed, PinnedPackage, ToolEnv, resolve_tool
from .lint_run_config import LintConfig
from .plan import Action, InstallPlan

if TYPE_CHECKING:
    from .catalog import Catalog

__all__ = [
    "BlockedReason",
    "BootstrapBlocked",
    "BootstrapOutcome",
    "BootstrapPlan",
    "BootstrapStep",
    "StepState",
    "apply_bootstrap",
    "build_bootstrap_plan",
    "manager_argv",
    "resolve_policy_catalog",
    "select_js_manager",
]

_DEPENDENCIES = "dependencies"  # BootstrapNeed.tool of a "restore everything the manifest declares" need
_DECLARED = "declared"  # BootstrapNeed.provider of a restore need: its pin is the project's, never overridden
# Any selected check that executes project code or config (ESLint plugins, tsc imports, scripts) needs the
# declared dependencies installed; `--only lint` must not skip the restore just because typecheck is off.
_NEEDS_DEPENDENCIES = frozenset({"lint", "typecheck", "format", "scripts"})
_ROLE_RANK = {"lint": 0, "format": 1, "typecheck": 2}
_DEFAULT_TOOL_ECOSYSTEM: Mapping[str, Literal["js", "python"]] = {"oxlint": "js", "oxfmt": "js", "ruff": "python"}


# ── planning ───────────────────────────────────────────────────────────────────────


def _declared_python_tool(scope: ProjectScope, discovery: Discovery, cfg: LintConfig, tool: str) -> tuple[PinnedPackage, ...]:
    """The project's own pin of ``tool`` (its lock / exact requirement), else the Rig default pin."""
    pm = manager_for(scope, "python")
    directory = dir_of(discovery.root, pm.root if pm is not None else scope.rel)
    version = locked_version(directory, tool)
    return (PinnedPackage(tool, version, "pypi"),) if version else (pin(PINS[tool], cfg),)


def _restorable(tool: ConfiguredTool, ecosystem: str) -> bool:
    """A Rig default tool the project already commits to: declared, or (Python) configured by a ruff config file.

    A `ruff.toml` / `[tool.ruff]` means the project wants Ruff run even with no manifest that declares it
    (source-only scripts, a lone config), so the pinned Ruff goes into the tool cache. JS configs do not count:
    an Oxlint config without the package has nothing Rig may install without writing a manifest.
    """
    return tool.declared or (ecosystem == "python" and bool(tool.config_files))


def _restore_needs(needs: Sequence[BootstrapNeed], discovery: Discovery, cfg: LintConfig) -> list[BootstrapNeed]:
    """Rig default tools a scope already declares: only their binary can be missing (fresh checkout).

    JS restores from the lock through the manager. Python never syncs the project (its runtime
    dependencies are not ours to install): the declared version goes into the Rig tool cache.
    """
    known = {(n.scope, n.tool) for n in needs}
    out: list[BootstrapNeed] = []
    for scope in discovery.scopes:
        for tool in scope.tools:
            ecosystem = _DEFAULT_TOOL_ECOSYSTEM.get(tool.tool)
            if ecosystem is None or not _restorable(tool, ecosystem) or (scope.rel, tool.tool) in known:
                continue
            if ecosystem == "js" and manager_for(scope, "js") is None:
                continue
            known.add((scope.rel, tool.tool))
            packages = _declared_python_tool(scope, discovery, cfg, tool.tool) if ecosystem == "python" else ()
            out.append(BootstrapNeed(scope.rel, ecosystem, tool.role, tool.tool, _DECLARED, packages))
    return out


def _declares_dependencies(scope: ProjectScope, discovery: Discovery, pm: PackageManager) -> bool:
    root = discovery.root
    return any(read_npm_deps(dir_of(root, rel)) for rel in {scope.rel, pm.root})


def _needs_dependency_restore(scope: ProjectScope, discovery: Discovery) -> bool:
    """A JS/TS scope whose declared dependencies are not installed (fresh checkout): checks cannot resolve imports."""
    pm = manager_for(scope, "js")
    if pm is None or not ({"javascript", "typescript"} & set(scope.languages)):
        return False
    installed = dir_of(discovery.root, pm.root)
    if (installed / "node_modules").exists() or (installed / ".pnp.cjs").exists():
        return False  # Yarn Plug'n'Play has no node_modules by design
    return _declares_dependencies(scope, discovery, pm)


def _dependency_needs(discovery: Discovery) -> list[BootstrapNeed]:
    """One restore need per JS scope whose declared dependencies are absent; the locked manager route installs them.

    Without these, `tsc`/ESLint fail on every import (TS2307) and the failure says nothing about the
    code. The restore is the lockfile's frozen install (``npm ci``, ``pnpm --frozen-lockfile``, ...):
    it never edits a manifest, a lockfile or product types. No lockfile, an ambiguous or missing
    manager each become a named blocked state with a remediation.
    """
    return [
        BootstrapNeed(scope.rel, "js", "typecheck", _DEPENDENCIES, _DECLARED, ())
        for scope in discovery.scopes
        if _needs_dependency_restore(scope, discovery)
    ]


@dataclass
class _Collector:
    """Accumulates the manifest-route installs, tool-cache steps and blocks while needs are folded in."""

    discovery: Discovery
    cfg: LintConfig
    env: ToolEnv
    installs: dict[str, Install] = field(default_factory=dict)
    blocked: list[BootstrapBlocked] = field(default_factory=list)
    cache_steps: list[BootstrapStep] = field(default_factory=list)
    cache_actions: list[Action] = field(default_factory=list)
    cache_seen: set[str] = field(default_factory=set)
    cache_config: ConfigPlan = field(default_factory=ConfigPlan)
    notes: list[str] = field(default_factory=list)

    def _switch_off(self, need: BootstrapNeed) -> bool:
        enabled = self.cfg.bootstrap.javascript if need.ecosystem == "js" else self.cfg.bootstrap.python
        if not enabled:
            key = "javascript" if need.ecosystem == "js" else "python"
            self.blocked.append(BootstrapBlocked(need.scope, need.tool, "disabled-by-config", f"lint.bootstrap.{key} is false"))
        return not enabled

    def fold(self, need: BootstrapNeed) -> None:
        """Route one need: manifest install, tool cache, or a block."""
        scope = self.discovery.scope(need.scope)
        if scope is None:
            self.blocked.append(BootstrapBlocked(need.scope, need.tool, "no-manifest", "scope is not part of the discovery result"))
            return
        if need.ecosystem in ("system", "rustup"):
            self._collect(plan_system_install(need, self.env, self.discovery.root, self.cache_seen))
            return
        if self._switch_off(need):
            return
        if self._declared_and_usable(need, scope):
            return  # nothing to install, so no package manager is needed either
        selected = route_js_manager(scope, self.env.which) if need.ecosystem == "js" else None
        if isinstance(selected, BootstrapBlocked):
            self.blocked.append(replace(selected, scope=need.scope, tool=need.tool))
        elif selected is None:
            self._fold_cache(need, scope)
        else:
            self._fold_manifest(need, scope, selected)

    def _declared_and_usable(self, need: BootstrapNeed, scope: ProjectScope) -> bool:
        if need.tool == _DEPENDENCIES or not any(t.tool == need.tool and t.declared for t in scope.tools):
            return False
        # Same notion as the cache planner: `manager-run` (poetry/pdm run) may sync the project environment, so
        # it is NOT a usable tool; the declared version goes into the tool cache instead.
        return resolve_tool(need.tool, scope, self.discovery, self.env).kind in USABLE_KINDS

    def _fold_cache(self, need: BootstrapNeed, scope: ProjectScope) -> None:
        own = need.provider == _DECLARED
        pkg = next((p if own else pin(p, self.cfg) for p in need.packages if p.name == need.tool), None)
        if pkg is None:
            return
        found = plan_cache_install(need, scope, self.discovery, self.cfg, self.env, pkg, self.cache_seen)
        self._collect(found)
        if need.tool == "oxlint" and not own and not found.blocked:
            self.cache_config = self.cache_config.merged(plan_cache_oxlint_config(scope, self.discovery, self.cfg, self.notes))

    def _collect(self, found: CacheSteps) -> None:
        self.cache_steps.extend(found.steps)
        self.cache_actions.extend(found.actions)
        # Lint and format needs of one tool report the same obstacle once.
        self.blocked.extend(b for b in found.blocked if b not in self.blocked)

    def _fold_manifest(self, need: BootstrapNeed, scope: ProjectScope, pm: PackageManager) -> None:
        problem = manifest_blocker(self.discovery.root, pm, need.scope, need.tool)
        if problem is not None:
            self.blocked.append(problem)
            return
        install = self.installs.setdefault(pm.root, Install(pm, [], {}, []))
        install.needs.append(need)
        declared = declared_names(self.discovery.root, scope, pm)
        missing = [p for p in (pin(p, self.cfg) for p in need.packages) if p.name not in declared]
        for pkg in missing:
            install.wanted.setdefault(pkg.name, pkg)
        restore = need.tool == _DEPENDENCIES  # already established as absent by _needs_dependency_restore
        if not missing and (restore or resolve_tool(need.tool, scope, self.discovery, self.env).kind == "missing"):
            install.unresolved.append(need)


def _install_steps(install: Install, root: Path, blocked: list[BootstrapBlocked]) -> tuple[BootstrapStep, ...]:
    pm = install.pm
    lockfile = pm.lockfile or join(pm.root, LOCKFILE_DEFAULT[pm.name])
    writes = (join(pm.root, MANIFEST), lockfile)
    if install.wanted:
        packages = tuple(install.wanted.values())
        workspace = has_workspaces(dir_of(root, pm.root), pm.name)
        return (BootstrapStep(
            f"js-add-dev@{pm.root}", "add-dev-packages", pm.root, pm.name,
            manager_argv(pm.name, packages, workspace_root=workspace), pm.root, packages, "lockfile", writes,
        ),)
    if not install.unresolved:
        return ()
    if pm.lockfile is None:
        first = install.unresolved[0]
        blocked.append(BootstrapBlocked(
            first.scope, first.tool, "no-lock-capable-manager",
            f"{first.tool} is declared but not installed and {pm.name} has no lockfile to restore it from; "
            f"run `{pm.name} install` once and commit the lockfile, then re-run `rig lint`",
        ))
        return ()
    return (BootstrapStep(
        f"js-install@{pm.root}", "install-dependencies", pm.root, pm.name,
        frozen_install_argv(pm.name), pm.root, (), "lockfile", writes,
    ),)


def _step_action(step: BootstrapStep, root: Path, lockfile: str) -> Action:
    return Action(
        kind="lint_bootstrap_step", category="lint_bootstrap", item=step.id, source=root, target=root,
        options={
            "step": step.id,
            "argv": list(step.argv),
            "cwd": step.cwd,
            "packages": [{"name": p.name, "version": p.version, "ecosystem": p.ecosystem} for p in step.packages],
            "writes": list(step.writes),
            "lockfile": lockfile,
            "allow_mutation": True,
            "expect_declared": step.kind == "add-dev-packages",
        },
    )


def _plan_install(install: Install, collector: _Collector) -> tuple[list[BootstrapStep], list[Action]]:
    discovery = collector.discovery
    config = plan_oxlint_configs(install, discovery, collector.cfg, collector.env, collector.notes)
    # Only the anti-slop JS plugin imports @oxlint/plugins, and it is hosted in the tool cache, never in a manifest.
    install.wanted.pop(PLUGINS_PKG, None)
    install_steps = _install_steps(install, discovery.root, collector.blocked)
    steps = list(install_steps)
    actions = [_step_action(step, discovery.root, step.writes[1]) for step in install_steps]
    if install_steps and install.wanted:
        steps.extend(config.steps)
        actions.extend(config.actions)
    return steps, actions


def _fold_selected(collector: _Collector, wanted: Sequence[BootstrapNeed], selected: set[str]) -> None:
    """Fold the needs of the selected categories; lint before format so one install lists its packages in a stable order.

    A deselected category (`--only limits`, `--skip format`, `lint.categories.<name>: false`) never
    gets tools installed for it.
    """
    chosen = (n for n in wanted if n.role in selected or (n.tool == _DEPENDENCIES and selected & _NEEDS_DEPENDENCIES))
    for need in sorted(chosen, key=lambda n: _ROLE_RANK.get(n.role, len(_ROLE_RANK))):
        collector.fold(need)


def _add_unique(blocked: list[BootstrapBlocked], found: Sequence[BootstrapBlocked]) -> None:
    blocked.extend(b for b in found if b not in blocked)


def _plan_oxlint_policy(collector: _Collector, wanted: Sequence[BootstrapNeed], catalog: Catalog | None) -> None:
    """The Oxlint policy half of the plan: the cache-hosted anti-slop policy."""
    source = catalog.source if catalog is not None else None
    moot = {b.scope for b in collector.blocked if b.tool == "oxlint"}
    policy, policy_notes = plan_policy(
        collector.discovery, collector.cfg, collector.env, [n for n in wanted if n.role == "lint"], source, collector.cache_seen, moot
    )
    collector.cache_steps.extend(policy.steps)
    collector.cache_actions.extend(policy.actions)
    _add_unique(collector.blocked, policy.blocked)
    collector.notes.extend(policy_notes)
    _add_unique(collector.blocked, conflicting_config_blocks(collector.discovery))


_INSTALL_KINDS = ("add-dev-packages", "install-dependencies")


def _prepend_dependency_ignore(steps: list[BootstrapStep], actions: list[Action], root: Path, notes: list[str]) -> None:
    """Ignore the ``node_modules`` directories this plan's installs create, as the FIRST step.

    Without an ignore rule every installed package would become an untracked source tree and the
    first scan after the install would lint (and CCCC-count) the dependencies. The entry goes into the
    local ``.git/info/exclude`` (see :mod:`riglib.lint_ignore`); a directory git already ignores is left
    alone, so an existing ``.gitignore`` with its negations is never touched or duplicated.
    """
    entries = pending_entries(root, [s.cwd for s in steps if s.kind in _INSTALL_KINDS])
    if not entries:
        return
    step = BootstrapStep(
        "git-exclude@lint-dependencies", "git-exclude", ".", "git", (), ".", (), "n/a", (".git/info/exclude",),
    )
    action = Action(
        kind="lint_bootstrap_step", category="lint_bootstrap", item=step.id, source=root, target=root,
        options={"step": step.id, "git_exclude": entries, "allow_mutation": True},
    )
    steps.insert(0, step)
    actions.insert(0, action)
    notes.append(
        f"bootstrap will ignore {', '.join(entries)} in .git/info/exclude (local, not committed) before installing, "
        "so the installed dependencies are not scanned as project code"
    )


def build_bootstrap_plan(
    needs: Sequence[BootstrapNeed], discovery: Discovery, cfg: LintConfig, env: ToolEnv, *, catalog: Catalog | None = None
) -> BootstrapPlan:
    """Derive the steps from the CURRENT disk state; a bootstrapped repo yields an empty plan."""
    if not cfg.bootstrap.enabled:
        disabled = tuple(BootstrapBlocked(n.scope, n.tool, "disabled-by-config", "lint.bootstrap.enabled is false") for n in needs)
        return BootstrapPlan((), disabled, InstallPlan(on_conflict="skip"), ())
    collector = _Collector(discovery, cfg, env)
    selected = {name for name, on in cfg.categories.items() if on}
    wanted = [*needs, *_restore_needs(needs, discovery, cfg), *_dependency_needs(discovery)]
    _fold_selected(collector, wanted, selected)
    steps: list[BootstrapStep] = []
    actions: list[Action] = []
    for key in sorted(collector.installs):
        more_steps, more_actions = _plan_install(collector.installs[key], collector)
        steps.extend(more_steps)
        actions.extend(more_actions)
    collector.blocked.extend(collector.cache_config.blocked)
    if "lint" in selected:  # the Oxlint policy is only this run's business when linting is
        _plan_oxlint_policy(collector, wanted, catalog)
    steps.extend([*collector.cache_steps, *collector.cache_config.steps])
    actions.extend([*collector.cache_actions, *collector.cache_config.actions])
    _prepend_dependency_ignore(steps, actions, discovery.root, collector.notes)
    return BootstrapPlan(
        tuple(steps), tuple(collector.blocked), InstallPlan(actions=actions, on_conflict="skip"), tuple(collector.notes)
    )


# ── applying ───────────────────────────────────────────────────────────────────────

_STATUS: dict[str, StepState] = {"created": "applied", "updated": "applied", "backed_up": "applied", "skipped": "noop", "error": "failed"}


def _apply_one(step: BootstrapStep, action: Action, resolved: Path, root: Path) -> tuple[StepState, str]:
    if Path(action.target).resolve() != resolved:
        return "failed", f"plan was built for {action.target}, not {root}"
    result = run_plan(InstallPlan(actions=[action], on_conflict="skip")).results[0]
    return _STATUS.get(result.status, "failed"), result.detail


def apply_bootstrap(plan: BootstrapPlan, *, root: Path, environ: Mapping[str, str]) -> BootstrapOutcome:
    """Run the plan through ``actions.run_plan``, one action at a time, stopping at the first failure.

    A failed install must stop the run: writing the Oxlint config next to packages that did not
    install would leave a repo that fails every later lint. Under CI the steps that edit manifests,
    lockfiles or configs are refused (reported ``blocked``, outcome ``failed``) while tool-cache
    steps, which write nothing in the repository, still run.
    """
    ci = ci_env_truthy(environ)
    resolved = root.resolve()
    results: list[tuple[str, StepState, str]] = []
    failed = refused = False
    for step, action in zip(plan.steps, plan.install_plan.actions, strict=True):
        if failed:
            results.append((step.id, "blocked", "skipped after an earlier bootstrap step failed"))
        elif ci and step.kind not in CI_SAFE_KINDS:
            results.append((step.id, "blocked", "refused: CI never bootstraps"))
            refused = True
        else:
            state, detail = _apply_one(step, action, resolved, root)
            results.append((step.id, state, detail))
            failed = state == "failed"
    return BootstrapOutcome(tuple(results), failed or refused)
