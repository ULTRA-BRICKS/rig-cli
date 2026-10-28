"""The Rig anti-slop Oxlint policy hosted as a third-party dependency in the Rig tool cache.

Context: the anti-slop plugin is upstream code vendored into agent-tools (never published to npm)
that is not written to its own rules and does not satisfy the CCCC complexity limits. Copying it
into a user's repository would make it the user's source: linted, formatted, commit-gated. Instead
``rig lint`` hosts it where third-party code belongs, outside the repository, in a pinned,
reproducible directory of the tool cache::

    <cache>/policy/<key>/anti-slop/          the catalog bundle, copied verbatim (key = digest of its bytes)
    <cache>/policy/<key>/node_modules/       @oxlint/plugins at its pin (the bundle imports it)
    <cache>/policy/<key>/oxlint.policy.json  the rendered policy; its ``jsPlugins`` entry is absolute

and the Oxlint check runs with ``-c <policy file>`` when no Oxlint config governs the scope (the
provider asks :func:`cached_policy_file`). The repository gains no bundle, no generated config and no
``@oxlint/plugins`` dependency; CI reproduces the cache with the same pinned steps (they write
nothing in the repo). A user config always wins: with an Oxlint config in the scope chain there is no
cache policy. Without the agent-tools catalog the policy cannot be hosted: that is reported as
``required-policy-unavailable`` (non-green) while Oxlint still runs on the base severities.

Planning is pure (stats the cache only). Stdlib-only at import time.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .lint_discovery_model import Discovery, ProjectScope, contains
from .lint_run_config import LintConfig
from .lint_tool_cache import cache_root

POLICY_FILE = "oxlint.policy.json"
BUNDLE_DIR = "anti-slop"
_PLACEHOLDER = "{POLICY_DIR}"
_TSGOLINT = "oxlint-tsgolint"


@dataclass(frozen=True)
class CachePolicy:
    key: str
    directory: Path
    content: str  # the policy file's bytes, absolute specifier included
    bundle_source: Path  # the catalog carrier directory
    bundle_sha256: str
    plugins_version: str

    @property
    def policy_file(self) -> Path:
        return self.directory / POLICY_FILE

    @property
    def bundle_dir(self) -> Path:
        return self.directory / BUNDLE_DIR

    @property
    def plugins_marker(self) -> Path:
        return self.directory / "node_modules" / "@oxlint" / "plugins" / "package.json"


def anti_slop_wanted(cfg: LintConfig) -> bool:
    """Whether the resolved policy enables any anti-slop rule (and linters are enabled at all)."""
    from .lint_policy import anti_slop_required, resolve_rule_severities

    if not cfg.linters_enabled:
        return False
    try:
        return anti_slop_required(resolve_rule_severities(dict(cfg.linters_rules)))
    except ValueError:
        return False


def has_oxlint_config(discovery: Discovery, scope_rel: str) -> bool:
    """A user (or earlier Rig) Oxlint config governs this scope: it is authoritative, no cache policy."""
    return any(t.tool == "oxlint" and t.config_files for s in discovery.ancestors(scope_rel) for t in s.tools)


def _frameworks(discovery: Discovery, scope_rel: str) -> tuple[str, ...]:
    return tuple(sorted({f for s in discovery.scopes if contains(scope_rel, s.rel) for f in s.frameworks}))


def typescript_7(scope: ProjectScope) -> bool:
    major = scope.typescript.major if scope.typescript is not None else None
    return major is not None and major >= 7


def _type_aware(scope: ProjectScope, discovery: Discovery, incoming: frozenset[str]) -> bool:
    """TypeScript >= 7 AND ``oxlint-tsgolint`` declared (or being added by this very run)."""
    declared = _TSGOLINT in incoming or any(
        t.tool == _TSGOLINT and t.declared for s in discovery.ancestors(scope.rel) for t in s.tools
    )
    return typescript_7(scope) and declared


def _bundle_digest(source: Path) -> str:
    from .linter_carriers import _tree_snapshot

    files = _tree_snapshot(source)
    digest = hashlib.sha256()
    for rel in sorted(files):
        digest.update(rel.encode("utf-8") + b"\0" + hashlib.sha256(files[rel]).digest())
    return digest.hexdigest()


def build_cache_policy(
    scope: ProjectScope, discovery: Discovery, cfg: LintConfig, catalog_source: Path | None,
    environ: Mapping[str, str], plugins_version: str, incoming: frozenset[str] = frozenset(),
) -> CachePolicy | str:
    """The policy for ``scope`` or the reason it cannot be hosted (no catalog / no carrier / bad rules).

    ``incoming`` are package names this run adds to the scope's project (tsgolint rides along with Oxlint
    on TypeScript 7), so the first rendered policy already carries the type-aware portion.
    """
    from .catalog import CatalogError, rule_provider_carrier
    from .lint_discovery_jsx import automatic_jsx_runtime
    from .lint_policy import framework_plugins, render_oxlint_config

    if catalog_source is None:
        return "no agent-tools catalog to take the anti-slop bundle from (set RIG_AGENT_TOOLS_SOURCE or clone agent-tools)"
    try:
        carrier_rel, carrier = rule_provider_carrier(catalog_source, "anti-slop")
        digest = _bundle_digest(carrier)
        template = render_oxlint_config(
            dict(cfg.linters_rules), flavor="json", type_aware=_type_aware(scope, discovery, incoming),
            plugins=framework_plugins(_frameworks(discovery, scope.rel)),
            plugin_specifier=f"{_PLACEHOLDER}/{BUNDLE_DIR}/index.ts",
            automatic_jsx=automatic_jsx_runtime(discovery.root, scope.rel),
        )
    except (CatalogError, OSError, ValueError) as exc:
        return f"the anti-slop policy cannot be built ({exc})"
    key = hashlib.sha256(json.dumps([digest, template, plugins_version, carrier_rel]).encode("utf-8")).hexdigest()[:16]
    directory = cache_root(environ) / "policy" / key
    return CachePolicy(key, directory, template.replace(_PLACEHOLDER, str(directory)), carrier, digest, plugins_version)


def cached_policy_file(
    scope: ProjectScope, discovery: Discovery, cfg: LintConfig, environ: Mapping[str, str]
) -> Path | None:
    """The installed policy file the Oxlint check should pass with ``-c`` for ``scope``, or ``None``.

    ``None`` when a user config governs the scope, anti-slop is not required, the catalog is absent,
    or the cache is not provisioned yet (the check then falls back to the base severities and the
    bootstrap plan says why).
    """
    from .catalog import CatalogError, resolve_source
    from .lint_providers import PINS

    if has_oxlint_config(discovery, scope.rel) or not anti_slop_wanted(cfg):
        return None
    try:
        source: Path | None = resolve_source(None)
    except CatalogError:
        source = None
    version = cfg.bootstrap.versions.get("@oxlint/plugins") or PINS["@oxlint/plugins"].version
    policy = build_cache_policy(scope, discovery, cfg, source, environ, version)
    if isinstance(policy, str):
        return None
    ready = policy.policy_file.is_file() and (policy.bundle_dir / "index.ts").is_file() and policy.plugins_marker.is_file()
    return policy.policy_file if ready else None
