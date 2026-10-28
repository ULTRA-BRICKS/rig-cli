"""What Rig would provision (bootstrap needs) and which configured tools it has no provider for.

``bootstrap_needs`` is pure planning over the CURRENT disk state: it says what the defaults would install,
independent of whether bootstrapping is allowed to run (``lint_bootstrap`` reports ``disabled-by-config``).
Four kinds of need exist:

* ``js`` / ``python``: the pinned Oxlint+Oxfmt / Ruff default for a role with no configured tool;
* ``system``: shellcheck/shfmt/actionlint for a scope with shell/workflow files when the binary is on neither
  PATH nor Rig's cache (user-space pinned release, never apt/sudo; not offered on Windows);
* ``rustup``: the clippy/rustfmt components of the CURRENT Rust toolchain when ``rustup`` exists (idempotent,
  never a toolchain change).

Go needs nothing: ``go vet`` and ``gofmt`` ship with the toolchain and Rig never changes a project's Go.
"""

from __future__ import annotations

from collections.abc import Mapping

from .lint_discovery_model import LANG_SHELL, TRAIT_GH_WORKFLOWS, Discovery, ProjectScope, TypeScriptInfo
from .lint_providers_build import effective_pins, pinned
from .lint_providers_model import BootstrapNeed, BootstrapSpec, Family, PlanContext, ProviderSpec, ToolEnv
from .lint_providers_registry import providers
from .lint_providers_resolve import resolve_tool
from .lint_providers_scope import COMPANION_TOOLS, gets_default, has_family, typescript_info
from .lint_run_config import LintConfig

_ECOSYSTEM_FAMILY: Mapping[str, Family] = {"js": "js", "python": "py"}
_SYSTEM_FILES: Mapping[str, str] = {"shellcheck": LANG_SHELL, "shfmt": LANG_SHELL, "actionlint": TRAIT_GH_WORKFLOWS}


def _anti_slop_required(cfg: LintConfig) -> bool:
    if not cfg.linters_enabled:
        return False
    from .lint_policy import anti_slop_required, resolve_rule_severities

    return anti_slop_required(resolve_rule_severities(cfg.linters_rules))


def _package_keys(spec: BootstrapSpec, ts: TypeScriptInfo | None, anti_slop: bool) -> tuple[str, ...]:
    """Extra Oxc packages ride along with Oxlint only when their prerequisites hold (D10, anti-slop plugin)."""
    keys = list(spec.packages)
    if "oxlint" in keys:
        if ts is not None and ts.major is not None and ts.major >= 7:
            keys.append("oxlint-tsgolint")
        if anti_slop:
            keys.append("@oxlint/plugins")
    return tuple(keys)


def _default_need(
    discovery: Discovery, scope: ProjectScope, spec: ProviderSpec, boot: BootstrapSpec, cfg: LintConfig, env: ToolEnv,
    anti_slop: bool,
) -> BootstrapNeed | None:
    if not gets_default(discovery, scope, _ECOSYSTEM_FAMILY[boot.ecosystem], boot.role):
        return None
    ts = typescript_info(PlanContext(discovery, scope, cfg, env))
    packages = tuple(pinned(key, cfg) for key in _package_keys(boot, ts, anti_slop))
    return BootstrapNeed(scope.rel, boot.ecosystem, boot.role, spec.tool, spec.id, packages)


def _system_need(
    discovery: Discovery, scope: ProjectScope, spec: ProviderSpec, boot: BootstrapSpec, cfg: LintConfig, env: ToolEnv
) -> BootstrapNeed | None:
    if env.system == "windows" or not scope.files.get(_SYSTEM_FILES.get(spec.tool, "")):
        return None
    if resolve_tool(spec.tool, scope, discovery, env, pins=effective_pins(cfg)).kind != "missing":
        return None
    return BootstrapNeed(scope.rel, "system", boot.role, spec.tool, spec.id, ())


def _rustup_need(scope: ProjectScope, spec: ProviderSpec, boot: BootstrapSpec, env: ToolEnv) -> BootstrapNeed | None:
    if not has_family(scope, "rust") or env.which("rustup") is None:
        return None
    return BootstrapNeed(scope.rel, "rustup", boot.role, spec.id, spec.id, ())


def _need(
    discovery: Discovery, scope: ProjectScope, spec: ProviderSpec, cfg: LintConfig, env: ToolEnv, anti_slop: bool
) -> BootstrapNeed | None:
    boot = spec.bootstrap
    if boot is None:
        return None
    if boot.ecosystem == "system":
        return _system_need(discovery, scope, spec, boot, cfg, env)
    if boot.ecosystem == "rustup":
        return _rustup_need(scope, spec, boot, env)
    return _default_need(discovery, scope, spec, boot, cfg, env, anti_slop)


def bootstrap_needs(discovery: Discovery, cfg: LintConfig, env: ToolEnv) -> tuple[BootstrapNeed, ...]:
    """What the bootstrap defaults would install; independent of whether bootstrapping is allowed to run."""
    anti_slop = _anti_slop_required(cfg)
    found = (_need(discovery, scope, spec, cfg, env, anti_slop) for scope in discovery.scopes for spec in providers())
    return tuple(need for need in found if need is not None)


def unsupported_tools(discovery: Discovery) -> tuple[tuple[str, str], ...]:
    """(scope, tool) pairs the project configures that Rig has no provider for; Rig never replaces them."""
    supported = {spec.tool for spec in providers()} | COMPANION_TOOLS
    pairs = {(scope.rel, tool.tool) for scope in discovery.scopes for tool in scope.tools if tool.tool not in supported}
    return tuple(sorted(pairs))
