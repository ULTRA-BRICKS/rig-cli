"""Plan stage for the agents-hooks bridge: make ``agents-hooks/v1`` descriptors fire in each harness.

Split out of :mod:`riglib.plan`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import os
from .catalog import Catalog
from .claude_config_dirs import SettingsTarget
from .claude_config_dirs import fan_out_item
from .config import LoadedConfig
from .config import OMP_HOOK_BRIDGE_EXTENSION_NAME
from .config import OPENCODE_HOOK_BRIDGE_PLUGIN_NAME
from .harness_skills import codex_user_path as _codex_user_path
from .harness_skills import omp_user_path as _omp_user_path
from .plan_harness import _claude_settings_targets
from .plan_model import Action
from .plan_model import InstallPlan
from .plan_targets import HOOK_BRIDGE_NO_SUPPORT_PHRASE
from .plan_targets import _DEFAULT_HARNESS_KIND
from .plan_targets import _HARNESS_AGENT_HOOK_TARGETS
from .plan_targets import _additional_hook_targets_enabled
from .plan_targets import _expand
from .plan_targets import _hook_bridge_kinds
from .plan_targets import _resolve_agent_hooks_targets
from .plan_targets import _same_dir


_HOOK_BRIDGE_HARNESSES = {
    "claude-code": {
        "module": "cc_hook_bridge",
        "settings": ".claude/settings.json",
        "format": "json",
    },
    "codex": {
        "module": "codex_hook_bridge",
        "settings": "~/.codex/config.toml",
        "format": "toml",
    },
    "opencode": {
        "module": "opencode_hook_bridge",
        "settings": f".opencode/plugins/{OPENCODE_HOOK_BRIDGE_PLUGIN_NAME}",
        "format": "opencode-plugin",
    },
    "omp": {
        "module": "omp_hook_bridge",
        "settings": f"~/.omp/agent/extensions/{OMP_HOOK_BRIDGE_EXTENSION_NAME}",
        "format": "omp-extension",
    },
}


def _hook_bridge_spec_for_kind(kind: str) -> dict[str, str] | None:
    spec = _HOOK_BRIDGE_HARNESSES.get(kind)
    if spec is None:
        return None
    if kind == "codex":
        return {**spec, "settings": _codex_user_path("config.toml")}
    if kind == "omp":
        # dynamic like codex: the extensions dir lives under the resolved omp agent root, not
        # the literal ``~/.omp/agent`` placeholder above.
        return {**spec, "settings": _omp_user_path(f"extensions/{OMP_HOOK_BRIDGE_EXTENSION_NAME}")}
    return spec


def _build_hook_bridge(config: LoadedConfig, catalog: Catalog, plan: InstallPlan) -> None:
    """Plan the agents-hooks/v1 → harness bridge registration, if applicable.

    Harnesses do not run the ``agent_hooks`` descriptors directly; they need a bridge registered
    in their own config. This emits one ``register_hook_bridge`` action that wires the relevant
    dispatcher from ``agent-tools/lib`` into that harness config.

    Gated on a harness block being present, enabled, of a supported kind, AND
    ``agent_hooks`` being enabled (a bridge with no installed descriptors is pointless) AND
    ``harness.hook_bridge`` not turned off. Anchored on the resolved agent-tools checkout so
    the dispatcher command imports the harness bridge from ``<checkout>/lib``.
    """
    h = config.data.get("harness")
    if not isinstance(h, dict) or not h or h.get("enabled") is False:
        return
    ah = config.category("agent_hooks")
    if ah.get("enabled") is False:
        plan.notes.append(
            "hook_bridge: skipped — agent_hooks disabled, so no descriptors to dispatch"
        )
        return
    primary_kind = str(h.get("kind", _DEFAULT_HARNESS_KIND))
    hooks_dir: Path | None = None
    if not _additional_hook_targets_enabled(config):
        hooks_dir = _resolve_agent_hooks_targets(config)[0]
        plan.notes.append(
            "hook_bridge: agent_hooks target/default pins one descriptor dir; registering "
            "configured bridges with that descriptor-dir override"
        )
    for kind in _hook_bridge_kinds(config):
        _build_hook_bridge_for_kind(config, catalog, plan, h, kind, primary_kind, hooks_dir=hooks_dir)


def _build_hook_bridge_for_kind(
    config: LoadedConfig,
    catalog: Catalog,
    plan: InstallPlan,
    h: dict[str, Any],
    kind: str,
    primary_kind: str,
    hooks_dir: Path | None = None,
) -> None:
    bridge_cfg = h.get("hook_bridge")
    # The explicit opt-out check runs FIRST, before the "no known bridge surface" branch below —
    # an explicit `hook_bridge.enabled: false` is a documented key with documented semantics
    # (quiet the bridge entirely), and it must stay honored even for a kind with no bridge at
    # all. Ordering it AFTER the unconditional gap note (an earlier version of this function
    # did) made that opt-out dead code for pi/commandcode: bridge_spec is None returns before
    # the enabled-False branch is ever reached, so a user who already knows pi/commandcode has
    # no bridge and deliberately silenced it would get nagged on every build with no way to
    # suppress it (review-cli caught this regression on rig-cli#342).
    if isinstance(bridge_cfg, dict) and bridge_cfg.get("enabled") is False:
        return
    bridge_spec = _hook_bridge_spec_for_kind(kind)
    if bridge_spec is None:
        # Reaching this branch means the harness has skill/instruction discovery but no known
        # hook bridge surface yet (currently pi/commandcode). Surfaced UNCONDITIONALLY whenever
        # the user has NOT explicitly opted out above — a real coverage gap the user should see
        # on every plan build, not only when they thought to ask for a bridge that does not
        # exist. (Silently staying quiet unless hook_bridge.enabled: true was explicitly set let
        # this gap hide forever for anyone who never tried opting in — rig-cli#337/#342.)
        plan.notes.append(
            f"hook_bridge: skipped — kind '{kind}' {HOOK_BRIDGE_NO_SUPPORT_PHRASE}"
        )
        return
    lib_dir = catalog.source / "lib"
    # Fail-CLOSED: never wire a harness-config command that would error at runtime. The
    # catalog only checks for skills/ + agent-hooks/, so an older agent-tools checkout can
    # lack a runnable bridge package — wiring it anyway means every tool call hits a broken
    # hook (which, fail-open, is harmless but noisy). Skip with a clear,
    # actionable note instead.
    module = str(bridge_spec["module"])
    bridge_format = str(bridge_spec["format"])
    bridge_dir = lib_dir / module
    bridge_missing = _bridge_missing_files(bridge_dir, bridge_format)
    if bridge_missing:
        plan.notes.append(
            f"hook_bridge: skipped — {bridge_dir} is incomplete in this agent-tools checkout "
            f"(missing {', '.join(bridge_missing)}; update agent-tools to a version that ships "
            "the runnable dispatcher)"
        )
        return
    explicit_settings_path = h.get("settings_path") if kind == primary_kind else None
    settings_path = explicit_settings_path or bridge_spec["settings"]
    suffix_note = _settings_suffix_mismatch_note(
        kind, bridge_format, settings_path, explicit_settings_path
    )
    if suffix_note:
        plan.notes.append(suffix_note)
        return
    if _opencode_path_needs_repo_layer(config, bridge_format, settings_path):
        plan.notes.append(
            "hook_bridge: skipped — repo-local opencode plugin path requires a repo config layer"
        )
        return
    options = _bridge_options(config, bridge_cfg, kind, lib_dir, module, bridge_format, hooks_dir)
    # the claude-code JSON bridge fans out to every claude-rotate config dir (rig-cli#368);
    # every other format/kind resolves to its single primary file
    primary = _expand(str(settings_path), config.repo_root)
    targets = (
        _claude_settings_targets(config, kind, primary)
        if bridge_format == "json"
        else [SettingsTarget(primary, None)]
    )
    for target in targets:
        plan.actions.append(
            Action(
                kind="register_hook_bridge",
                category="harness",
                item=fan_out_item("hook-bridge", target),
                source=catalog.source,
                target=target.path,
                options=dict(options),
            )
        )


def _opencode_path_needs_repo_layer(
    config: LoadedConfig, bridge_format: str, settings_path: Any
) -> bool:
    has_repo_config_layer = any(layer.startswith(("repo:", "config:")) for layer in config.layers)
    return (
        bridge_format == "opencode-plugin"
        and not Path(os.path.expanduser(str(settings_path))).is_absolute()
        and not has_repo_config_layer
    )


def _bridge_options(
    config: LoadedConfig,
    bridge_cfg: Any,
    kind: str,
    lib_dir: Path,
    module: str,
    bridge_format: str,
    hooks_dir: Path | None,
) -> dict[str, Any]:
    options: dict[str, Any] = {
        "kind": kind,
        "lib_dir": str(lib_dir),
        "module": module,
        "format": bridge_format,
    }
    effective_hooks_dir = _effective_hooks_dir(config, kind, hooks_dir)
    if effective_hooks_dir is not None:
        options["hooks_dir"] = str(effective_hooks_dir)
    if isinstance(bridge_cfg, dict) and bridge_cfg.get("python"):
        options["python"] = str(bridge_cfg["python"])
    if kind == "codex":
        extra_stop_hooks = _codex_extra_stop_hooks(config)
        if extra_stop_hooks:
            options["extra_stop_hooks"] = extra_stop_hooks
    return options


def _bridge_missing_files(bridge_dir: Path, bridge_format: str) -> list[str]:
    bridge_required = [bridge_dir / "dispatch.py", bridge_dir / "__main__.py"]
    if bridge_format == "opencode-plugin":
        bridge_required.append(bridge_dir / "plugin.js")
    if bridge_format == "omp-extension":
        bridge_required.append(bridge_dir / "extension.ts")
    return [p.name for p in bridge_required if not p.is_file()]


def _settings_suffix_mismatch_note(
    kind: str,
    bridge_format: str,
    settings_path: Any,
    explicit_settings_path: Any,
) -> str | None:
    expected_suffix = {
        "toml": ".toml",
        "opencode-plugin": ".js",
        "omp-extension": ".ts",
    }.get(bridge_format, ".json")
    actual_suffix = Path(str(settings_path)).suffix
    if (
        bridge_format in {"toml", "opencode-plugin", "omp-extension"}
        and explicit_settings_path
        and actual_suffix
        and actual_suffix != expected_suffix
    ):
        return (
            f"hook_bridge: skipped — kind '{kind}' expects a {expected_suffix} settings_path, "
            f"got {settings_path}"
        )
    return None


def _effective_hooks_dir(config: LoadedConfig, kind: str, hooks_dir: Path | None) -> Path | None:
    if hooks_dir is not None or kind != "codex":
        return hooks_dir
    codex_hooks_dir = _expand(
        _codex_user_path("hooks"),
        config.repo_root,
    )
    # _codex_user_path never returns empty for codex; call it directly and keep the baseline
    # literal ~/.codex/hooks so RIG_CODEX_HOME-driven divergence is detected.
    baseline_codex_hooks_dir = _expand(_HARNESS_AGENT_HOOK_TARGETS["codex"], config.repo_root)
    if not _same_dir(codex_hooks_dir, baseline_codex_hooks_dir):
        return codex_hooks_dir
    # NOTE: omp deliberately has NO equivalent divergence block here, unlike codex above. The
    # divergence exists for codex because ``RIG_CODEX_HOME`` is a rig-owned env var the codex
    # dispatcher does not know about on its own — without threading it through explicitly, rig
    # would write descriptors to the RIG_CODEX_HOME-resolved dir while the dispatcher kept
    # reading the literal ~/.codex/hooks default, a real mismatch. omp's dispatcher instead
    # PORTS rig's own ``omp_agent_root()`` precedence verbatim (``PI_CODING_AGENT_DIR`` /
    # ``PI_CONFIG_DIR``, see lib/omp_hook_bridge/dispatch.py's ``_omp_agent_root()``), so it
    # independently resolves the SAME directory rig writes descriptors to with no override
    # needed — baking one in here would only turn a plain symlink into an unnecessary wrapper.
    return None


def _codex_extra_stop_hooks(config: LoadedConfig) -> list[str]:
    """tg-ctl's Codex Stop usage-telemetry hook, folded into rig's own bridge (tg-cli#308).

    Delegates entirely to :func:`riglib.tg_ctl.codex_usage_hook_command` — the single place
    that decides whether to fold in and resolves the command — so this planner never
    independently re-derives tg-ctl's bun/tg_ctl_path resolution (that would risk disagreeing
    with the actual provisioned daemon, and with tg-ctl's own grep-based detection of this
    command in the rendered config.toml). tg-ctl's paths are HOME-anchored per-machine (not
    repo-relative), same reasoning as ``_build_tg_ctl``, so this resolves against ``Path.home()``
    rather than the repo-relative ``_expand`` helper.
    """
    from .tg_ctl import codex_usage_hook_command

    t = config.data.get("tg_ctl") or {}
    cmd = codex_usage_hook_command(t, home=Path.home())
    return [cmd] if cmd else []
