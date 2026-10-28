"""Plan the cache-hosted anti-slop policy steps (see :mod:`riglib.lint_policy_cache`).

Context: for every JS scope that runs Rig's default Oxlint (no Oxlint config in its chain) while the
policy requires anti-slop, three ``install-tool-cache`` steps provision a pinned directory of the Rig
tool cache -- the catalog bundle copied verbatim, ``@oxlint/plugins`` at its pin, and the rendered
policy -- through the same ``lint_bootstrap_step`` action and ``actions.run_plan`` as every other
bootstrap step. They write nothing in the repository, so CI may run them. Planning is pure.
Stdlib-only at import time.
"""

from __future__ import annotations

from pathlib import Path

from .lint_bootstrap_cache import CacheSteps, step_action
from .lint_bootstrap_managers import pin
from .lint_bootstrap_model import BootstrapBlocked, BootstrapStep
from .lint_ci_env import ci_env_truthy
from .lint_discovery_model import Discovery, ProjectScope
from .lint_policy_cache import CachePolicy, anti_slop_wanted, build_cache_policy, has_oxlint_config, typescript_7
from .lint_providers import PINS, BootstrapNeed, PinnedPackage, ToolEnv
from .lint_run_config import LintConfig

REASON = "required-policy-unavailable"
_PACKAGE_JSON = '{"name": "rig-lint-policy", "private": true, "type": "module"}\n'


def _blocked(scope: str, detail: str) -> BootstrapBlocked:
    return BootstrapBlocked(
        scope, "oxlint", REASON,
        f"the Rig anti-slop policy is required but cannot be hosted ({detail}); Oxlint runs on the base severities "
        "(correctness/suspicious/perf at error) until it is available",
    )


def _step(step_id: str, scope: str, policy: CachePolicy, creates: Path, **fields: object) -> BootstrapStep:
    return BootstrapStep(
        step_id, "install-tool-cache", scope, "policy-cache", fields.pop("argv", ()), ".", fields.pop("packages", ()),  # type: ignore[arg-type]
        "n/a", (), cache_dir=str(policy.directory), creates=str(creates), **fields,  # type: ignore[arg-type]
    )


def policy_steps(
    policy: CachePolicy, scope: str, env: ToolEnv, plugins: PinnedPackage, root: Path
) -> CacheSteps | BootstrapBlocked:
    """The missing steps of one policy directory, in dependency order; empty when fully provisioned."""
    steps: list[BootstrapStep] = []
    if not (policy.bundle_dir / "index.ts").is_file():
        copy = (str(policy.bundle_source), str(policy.bundle_dir))
        steps.append(_step(f"policy-bundle@{policy.key}", scope, policy, policy.bundle_dir / "index.ts", copy=copy))
    if not (policy.directory / "package.json").is_file():
        # `type: module` marks the hosted TypeScript plugin as ESM, so Node does not warn on every lint run.
        steps.append(_step(f"policy-package@{policy.key}", scope, policy, policy.directory / "package.json", content=_PACKAGE_JSON))
    if not policy.plugins_marker.is_file():
        if env.which("npm") is None:
            return _blocked(scope, "npm is not on PATH, so @oxlint/plugins cannot be installed for it; install Node.js/npm")
        argv = ("npm", "install", "--prefix", str(policy.directory), "--ignore-scripts", "--no-audit", "--no-fund",
                f"{plugins.name}@{plugins.version}")
        steps.append(_step(f"policy-plugins@{policy.key}", scope, policy, policy.plugins_marker, argv=argv, packages=(plugins,)))
    if not policy.policy_file.is_file():
        steps.append(_step(f"policy-config@{policy.key}", scope, policy, policy.policy_file, content=policy.content))
    return CacheSteps(tuple(steps), tuple(step_action(s, root) for s in steps), ())


def _oxlint_scopes(discovery: Discovery, needs: list[BootstrapNeed], skip: set[str]) -> list[ProjectScope]:
    """JS scopes that run Rig's default Oxlint: needed now, or already declared without any config file.

    ``skip`` are scopes whose Oxlint install is itself blocked (no manager, ambiguous evidence...): a
    policy for a tool that cannot be installed is moot and the install block already says why.
    """
    wanted = {n.scope for n in needs if n.tool == "oxlint" and n.ecosystem == "js"}
    out: list[ProjectScope] = []
    for scope in discovery.scopes:
        declared = any(t.tool == "oxlint" and t.declared for t in scope.tools)
        if scope.rel not in skip and (scope.rel in wanted or declared) and not has_oxlint_config(discovery, scope.rel):
            out.append(scope)
    return out


def _declares(discovery: Discovery, scope: ProjectScope, tool: str) -> bool:
    return any(t.tool == tool and t.declared for s in discovery.ancestors(scope.rel) for t in s.tools)


def _scope_notes(policy: CachePolicy, scope: ProjectScope, discovery: Discovery, incoming: frozenset[str], plugins: PinnedPackage) -> list[str]:
    notes: list[str] = []
    if typescript_7(scope) and "oxlint-tsgolint" not in incoming and not _declares(discovery, scope, "oxlint-tsgolint"):
        notes.append(
            f"{scope.rel}: type-aware linting is disabled (TypeScript >= 7 but oxlint-tsgolint is not installed); "
            "the type-info rules are omitted and the rest of the policy is unchanged"
        )
    notes.append(
        f"anti-slop policy hosted in the Rig tool cache for {scope.rel}: {policy.directory} "
        f"(bundle sha256 {policy.bundle_sha256[:12]}, @oxlint/plugins {plugins.version}); nothing is added to the repository"
    )
    return notes


def _scope_policy(
    scope: ProjectScope, discovery: Discovery, cfg: LintConfig, env: ToolEnv, needs: list[BootstrapNeed],
    catalog_source: Path | None, plugins: PinnedPackage,
) -> tuple[CacheSteps, list[str]] | BootstrapBlocked:
    """The steps and notes for one scope's hosted policy, or why it cannot be hosted."""
    # Packages this run adds to the project manifest only count when that step can actually run: under CI it
    # is refused, the check later sees the manifest WITHOUT them, and a policy rendered with them would be
    # cached under a key the check never looks up (silently dropping the anti-slop rules).
    incoming = frozenset() if ci_env_truthy(env.environ) else frozenset(
        p.name for n in needs if n.scope == scope.rel for p in n.packages
    )
    policy = build_cache_policy(scope, discovery, cfg, catalog_source, env.environ, plugins.version, incoming)
    if isinstance(policy, str):
        return _blocked(scope.rel, policy)
    found = policy_steps(policy, scope.rel, env, plugins, discovery.root)
    if isinstance(found, BootstrapBlocked):
        return found
    return found, _scope_notes(policy, scope, discovery, incoming, plugins)


def plan_policy(
    discovery: Discovery, cfg: LintConfig, env: ToolEnv, needs: list[BootstrapNeed], catalog_source: Path | None,
    seen: set[str], install_blocked: set[str],
) -> tuple[CacheSteps, list[str]]:
    """Cache steps (and report notes) for every scope that needs the hosted policy; blocks when it cannot be hosted."""
    if not anti_slop_wanted(cfg):
        return CacheSteps((), (), ()), []
    plugins = pin(PINS["@oxlint/plugins"], cfg)
    steps: list[BootstrapStep] = []
    actions: list = []
    blocked: list[BootstrapBlocked] = []
    notes: list[str] = []
    for scope in _oxlint_scopes(discovery, needs, install_blocked):
        result = _scope_policy(scope, discovery, cfg, env, needs, catalog_source, plugins)
        if isinstance(result, BootstrapBlocked):
            blocked.append(result)
            continue
        found, scope_notes = result
        notes.extend(scope_notes)
        fresh = [(s, a) for s, a in zip(found.steps, found.actions, strict=True) if s.id not in seen]
        seen.update(s.id for s, _ in fresh)
        steps.extend(s for s, _ in fresh)
        actions.extend(a for _, a in fresh)
    return CacheSteps(tuple(steps), tuple(actions), tuple(blocked)), notes
