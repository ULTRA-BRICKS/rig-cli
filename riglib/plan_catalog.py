"""Plan stages for the catalog-backed categories: skills (+ plugin skills), the git-hook dispatcher,
CI workflows and MCP registrations.

Split out of :func:`riglib.plan.build`, which now only orders the stages. Each stage appends to the
shared :class:`~riglib.plan_model.InstallPlan` and never touches disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .catalog import Catalog, Item
from .config import LoadedConfig
from .plan_items import _item_enabled, _skills_enabled
from .plan_model import Action, InstallPlan
from .plan_targets import (
    _expand,
    _resolve_harness_skill_dirs,
    _resolve_target,
    _same_dir,
    _skill_discovery_notes,
)
from .plugin_skills import resolve_plugin_skills


def _harness_link_dirs(config: LoadedConfig, skills_target: Path) -> list[Path]:
    """Harness skill dirs that need a discovery symlink: configured AND not the install dir itself.

    Compare resolved paths so a HOME symlink or a ``..`` segment that makes the two dirs textually
    differ but point at the same place is still recognized as the same dir (avoids a spurious
    self-link → real-dir warning).
    """
    return [
        link_dir
        for _kind, link_dir in _resolve_harness_skill_dirs(config)
        if not _same_dir(link_dir, skills_target)
    ]


def _harness_link_actions(item: str, installed: Path, leaf: str, link_dirs: list[Path]) -> list[Action]:
    """One ``link_skill_harness`` per harness dir: make the installed skill discoverable by the harness.

    Without this the skill sits in skills_target but the harness never lists/loads it.
    """
    return [
        Action(
            kind="link_skill_harness",
            category="skills",
            item=item,
            source=installed,  # the installed skill the symlink points at
            target=link_dir / leaf,
        )
        for link_dir in link_dirs
    ]


def plan_skills(
    plan: InstallPlan, config: LoadedConfig, catalog: Catalog, project_type: str, stack_preset: str | None
) -> Path:
    """Catalog skills + plugin skills; returns the resolved ``skills_target`` (blurb sync reuses it)."""
    skills_target = _resolve_target(config, "skills")
    link_dirs = _harness_link_dirs(config, skills_target)
    # Instruction-file harness (pi/commandcode) → no skill-link dir; record WHY so
    # ``rig status`` shows "uses <AGENTS.md>" instead of a silent empty skill-link area.
    plan.notes.extend(_skill_discovery_notes(config))
    for item in _skills_enabled(config, catalog, project_type, stack_preset):
        installed = skills_target / item.path.name
        plan.actions.append(
            Action(kind="copy_skill", category="skills", item=item.name, source=item.path, target=installed)
        )
        plan.actions.extend(_harness_link_actions(item.name, installed, item.path.name, link_dirs))
    _plan_plugin_skills(plan, config, skills_target, link_dirs)
    return skills_target


def _plugin_skill_collision(
    plugin_name: str, skill_name: str, catalog_skill_names: Any, claimed: dict[str, str]
) -> str | None:
    """The note explaining why a plugin skill is skipped, or ``None`` when it may be linked."""
    if skill_name in catalog_skill_names:
        # A plugin skill sharing a name with a real agent-tools catalog skill: the
        # catalog is rig's OWN namespace (riglib/provenance.py) — never silently shadow
        # it with a plugin-sourced symlink. Matches the precedent this feature formalizes:
        # superpowers' `systematic-debugging` skill collides with agent-tools' own skill
        # of the same name.
        return (
            f"skills.plugins.items.{plugin_name}: skill '{skill_name}' collides with an "
            "existing catalog skill of the same name — skipped, the catalog skill wins"
        )
    earlier = claimed.get(skill_name)
    if earlier is None:
        return None
    # TWO declared plugins ship a same-named skill: without this guard both would
    # emit a link_skill_plugin action at the SAME target with a DIFFERENT source —
    # every apply would flip-flop which one "wins" (non-idempotent) and drift would
    # never go clean (whichever action lost the flip always reads as "modified").
    # First-claimed (by `resolve_plugin_skills`'s stable plugin-name sort) wins;
    # the loser is recorded, never silently applied.
    return (
        f"skills.plugins.items.{plugin_name}: skill '{skill_name}' collides with the "
        f"same-named skill from plugin '{earlier}' — skipped, '{earlier}' wins "
        "(alphabetically first plugin name)"
    )


def _plan_plugin_skills(plan: InstallPlan, config: LoadedConfig, skills_target: Path, link_dirs: list[Path]) -> None:
    """Claude Code plugin-marketplace-sourced skills (``skills.plugins``).

    Resolved from the LIVE ~/.claude/plugins/installed_plugins.json on every build() call —
    same re-resolve-every-run contract as the catalog loop — so a plugin version bump
    (installed_plugins.json's installPath changes) re-points the ~/.agents/skills/<name>
    symlink at the NEXT `rig apply` instead of leaving it dangling. Gated on the same
    `skills.enabled` switch as the catalog loop (off = no skills installed at all, whether
    catalog- or plugin-sourced).
    """
    sk_cfg = config.category("skills")
    plugins_cfg = sk_cfg.get("plugins") if isinstance(sk_cfg, dict) else None
    plugins_items = plugins_cfg.get("items") if isinstance(plugins_cfg, dict) else None
    if sk_cfg.get("enabled") is False or not isinstance(plugins_items, dict) or not plugins_items:
        return
    catalog_skill_names = plan.catalog_items["skills"]
    resolved, plugin_notes = resolve_plugin_skills(plugins_items)
    plan.notes.extend(plugin_notes)
    claimed: dict[str, str] = {}  # skill_name -> claiming plugin_name
    for plugin_name, skill_name, skill_source in resolved:
        collision = _plugin_skill_collision(plugin_name, skill_name, catalog_skill_names, claimed)
        if collision is not None:
            plan.notes.append(collision)
            continue
        claimed[skill_name] = plugin_name
        installed = skills_target / skill_name
        item_id = f"{plugin_name}/{skill_name}"
        plan.actions.append(
            Action(
                kind="link_skill_plugin",
                category="skills",
                item=item_id,
                source=skill_source,  # the plugin's own skill dir (version-pinned cache path)
                target=installed,
            )
        )
        # Same harness-discovery fan-out as a catalog skill: symlink the installed (plugin-
        # sourced) skill into every configured harness skill dir too.
        plan.actions.extend(_harness_link_actions(item_id, installed, skill_name, link_dirs))


def plan_dispatcher(plan: InstallPlan, config: LoadedConfig, catalog: Catalog) -> None:
    """The ``git_hooks.dispatcher`` install (global git hook dispatcher + fragments)."""
    gh = config.category("git_hooks")
    disp_cfg = gh.get("dispatcher", {}) if isinstance(gh, dict) else {}
    if not (isinstance(disp_cfg, dict) and disp_cfg.get("enabled")):
        return
    item = catalog.get("git_hooks", "dispatcher")
    if item is None:
        return
    runner = disp_cfg.get("runner", "~/.config/git/run-global-hooks")
    plan.actions.append(
        Action(
            kind="install_dispatcher",
            category="git_hooks",
            item="dispatcher",
            source=item.path,
            target=_expand(str(disp_cfg.get("dir", "~/.config/git/global-hooks.d")), config.repo_root),
            options={
                "runner": str(_expand(str(runner), config.repo_root)),
                "set_global_hooks_path": bool(disp_cfg.get("set_global_hooks_path", True)),
                "install_local_retrofit_script": bool(disp_cfg.get("install_local_retrofit_script", True)),
                "fragments": disp_cfg.get("fragments", {}),
            },
        )
    )


def _reject_unknown_ci_names(config: LoadedConfig, catalog: Catalog, ci: dict[str, Any], ci_items: dict[str, Any]) -> None:
    """Fail closed on an unknown item name (in items:, enable:, or disable:): a config typo, not a skip.

    'ship' is a real catalog item (ci/ship/) — it is in catalog.names("ci") when present and ABSENT
    when the checkout lacks it, so a config enabling ship against a minimal checkout fails closed
    instead of dropping.
    """
    known = catalog.names("ci")
    referenced = set(ci_items) | set(ci.get("enable", []) or []) | set(ci.get("disable", []) or [])
    unknown = sorted(referenced - known)
    if not unknown:
        return
    from . import errors

    bad = unknown[0]
    key = f"ci.items.{bad}"
    raise errors.unknown_item_error(
        category="ci", key=key, bad=bad, known=known, config_path=str(config.source_for_key(key))
    )


def _ci_action(item: Item, spec: dict[str, Any], ci_target: Path | None, config: LoadedConfig) -> Action:
    name = item.name
    if name == "ship":
        return Action(
            kind="install_ci",
            category="ci",
            item="ship",
            source=item.path,
            target=_expand(str(spec.get("install_to", "~/bin")), config.repo_root),
            options={"slot": "ship", "gh_alias": bool(spec.get("gh_alias", False))},
        )
    return Action(
        kind="install_ci",
        category="ci",
        item=name,
        source=item.path,
        target=ci_target,  # type: ignore[arg-type]
        options={
            "slot": name,
            "tier": spec.get("tier", "block"),
            "variant": spec.get("variant"),
            # companions install relative to the CHECKOUT ROOT (the workflow
            # runs `bash ci/<slot>/x.sh` from there), independent of ci.target.
            "repo_root": str(config.repo_root),
        },
    )


def plan_ci(plan: InstallPlan, config: LoadedConfig, catalog: Catalog) -> None:
    """CI workflow installs; resolution is per-item override > enable/disable > all > off."""
    ci = config.category("ci")
    if ci.get("enabled") is False:
        return
    ci_target_raw = ci.get("target", config.defaults.get("ci_target", ".github/workflows"))
    export_only = ci_target_raw == "export-only"
    ci_target = None if export_only else _resolve_target(config, "ci")
    ci_items = ci.get("items", {})
    if not isinstance(ci_items, dict):
        ci_items = {}
    _reject_unknown_ci_names(config, catalog, ci, ci_items)
    for item in catalog.by_category("ci"):
        if not _item_enabled(ci, item, type_enabled=False):
            continue
        if export_only:
            plan.notes.append(f"ci/{item.name} export-only (recorded, not written)")
            continue
        spec = ci_items.get(item.name, {})
        plan.actions.append(_ci_action(item, spec if isinstance(spec, dict) else {}, ci_target, config))


def _mcp_options(spec: dict[str, Any], item: Item) -> dict[str, Any]:
    # the registration KEY is the configured server name if set, else the item
    # name — so `server: serena` registers under "serena", not the catalog key.
    options: dict[str, Any] = {"command": spec.get("command", ""), "server": str(spec.get("server") or item.name)}
    if "args" in spec:
        options["args"] = spec.get("args", [])
    if "env" in spec:
        options["env"] = spec.get("env", {})
    return options


def plan_mcp(plan: InstallPlan, config: LoadedConfig, catalog: Catalog) -> None:
    """MCP server registrations (one per enabled catalog item), skipped when export-only."""
    mcp = config.category("mcp")
    if mcp.get("enabled") is False:
        return
    mcp_target_raw = mcp.get("target", config.defaults.get("mcp_target", "~/.claude/mcp"))
    if mcp_target_raw == "export-only":
        return
    mcp_target = _expand(str(mcp_target_raw), config.repo_root)
    mcp_items = mcp.get("items", {})
    if not isinstance(mcp_items, dict):
        mcp_items = {}
    for item in catalog.by_category("mcp"):
        # honor category all/enable/disable + per-item enabled (same as other cats)
        if not _item_enabled(mcp, item, type_enabled=False):
            continue
        spec = mcp_items.get(item.name, {})
        plan.actions.append(
            Action(
                kind="register_mcp",
                category="mcp",
                item=item.name,
                source=item.path,
                target=mcp_target,
                options=_mcp_options(spec if isinstance(spec, dict) else {}, item),
            )
        )
