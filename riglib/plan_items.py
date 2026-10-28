"""Catalog-item selection for the plan builders: which skills / hooks / blurbs a config enables, and the
fail-closed validation of item names.

Split out of :mod:`riglib.plan`; :func:`riglib.plan.build` calls these first.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from .catalog import Catalog
from .catalog import Item
from .config import LoadedConfig
from .plan_model import Action
from .plan_model import InstallPlan
from .plan_model import PlanError
from .plan_targets import _resolve_agent_hooks_targets
from .tools import BLURBS_SUBDIR


def _item_enabled(cat_cfg: dict[str, Any], item: Item, *, type_enabled: bool) -> bool:
    """Resolve a single item's enabled state from category-level deltas + per-item override.

    Precedence (highest first):
      1. explicit per-item ``items.<name>.enabled``
      2. ``enable:`` / ``disable:`` delta lists
      3. ``all: true|false``
      4. project-type pull-in (by-type skills) / catalog ``default_enabled``
    """
    items = cat_cfg.get("items", {})
    if isinstance(items, dict) and item.name in items:
        spec = items[item.name]
        if isinstance(spec, dict) and "enabled" in spec:
            return bool(spec["enabled"])

    enable = set(cat_cfg.get("enable", []) or [])
    disable = set(cat_cfg.get("disable", []) or [])
    if item.name in disable:
        return False
    if item.name in enable:
        return True

    all_flag = cat_cfg.get("all")
    if all_flag is True:
        return True
    if all_flag is False:
        return False

    return type_enabled or item.default_enabled


def _dict_or_empty(parent: dict[str, Any], key: str) -> dict[str, Any]:
    """``parent[key]`` when it is a dict, else a fresh empty dict (tolerates malformed config)."""
    value = parent.get(key, {})
    return value if isinstance(value, dict) else {}


def _skills_enabled(
    config: LoadedConfig, catalog: Catalog, project_type: str, stack_preset: str | None
) -> list[Item]:
    """Skills get special handling: nested universal{} / by_type{} groups + type/stack pull-in."""
    sk = config.category("skills")
    if sk.get("enabled") is False:
        return []

    uni_cfg = _dict_or_empty(sk, "universal")
    # default for universal is all-on (opt-out model)
    uni_cfg.setdefault("all", True)

    bt_cfg = _dict_or_empty(sk, "by_type")
    enabled_kinds = _enabled_skill_kinds(bt_cfg, project_type)
    bt_items_cfg = _dict_or_empty(bt_cfg, "items")

    bs_cfg = _dict_or_empty(sk, "by_stack")
    bs_disable = set(bs_cfg.get("disable", []) or [])
    bs_items_cfg = _dict_or_empty(bs_cfg, "items")
    # `stack` is the CALLER's detect_stack_preset guess (Environment.stack_preset), computed
    # the same way/place as `project_type` — see detect.detect_environment. Declared config
    # always wins; the guess is only a fallback when the repo declares no stack:.
    effective_stack = config.stack or stack_preset

    def _selected(item: Item) -> bool:
        if item.group == "universal":
            return _item_enabled(uni_cfg, item, type_enabled=True)
        if item.group.startswith("by-type/"):
            return _by_type_item_enabled(item, enabled_kinds, bt_items_cfg)
        if item.group.startswith("by-stack/"):
            return _by_stack_item_enabled(item, effective_stack, bs_disable, bs_items_cfg)
        return False

    return [item for item in catalog.by_category("skills") if _selected(item)]


def _enabled_skill_kinds(bt_cfg: dict[str, Any], project_type: str) -> set[str]:
    """Kinds listed in ``by_type.enable``, else the detected project type's bundle."""
    enabled_kinds = set(bt_cfg.get("enable", []) or [])
    # auto-pull the detected project type's bundle unless config explicitly lists kinds
    if not enabled_kinds and project_type and project_type != "unknown":
        enabled_kinds = {project_type}
    return enabled_kinds



def _by_type_item_enabled(item: Item, enabled_kinds: set[str], items_cfg: dict[str, Any]) -> bool:
    """Whether a by-type skill is on: per-item override under ``by_type.items`` wins, else its kind bundle."""
    # per-item override under by_type.items.<by-type/kind/name>
    spec = items_cfg.get(item.name)
    if isinstance(spec, dict) and "enabled" in spec:
        return bool(spec["enabled"])
    return item.meta.get("kind", "") in enabled_kinds


def _by_stack_item_enabled(
    item: Item,
    effective_stack: str | None,
    disable: set[str],
    items_cfg: dict[str, Any],
) -> bool:
    """Whether a by-stack skill is provisioned for the repo's declared (or auto-detected) stack.

    Auto-selected when the item's stack path is a PREFIX of ``effective_stack`` (hierarchical
    — see :mod:`riglib.stack`), unless dropped via ``skills.by_stack.disable`` or a
    ``skills.by_stack.items.<name>.enabled: false`` override. An explicit
    ``items.<name>.enabled: true`` FORCES the skill in even without a matching stack (the
    escape hatch for pulling one stack skill into an off-stack repo).

    NOTE: this is a deliberately-separate resolver from :func:`_item_enabled` (universal/by_type):
    by_stack has no ``enable``/``all`` axis — selection is driven by the ``effective_stack`` prefix match,
    not a bundle-enable list — so the two do not share a code path. The only overlap is the
    per-item ``enabled`` override + the ``disable`` list, kept explicit here for readability."""
    from .stack import stack_matches

    spec = items_cfg.get(item.name)
    if isinstance(spec, dict) and "enabled" in spec:
        return bool(spec["enabled"])
    if item.name in disable:
        return False
    if not effective_stack:
        return False
    return stack_matches(effective_stack, item.meta.get("stack", ""))


def _validate_item_names(config: LoadedConfig, catalog: Catalog) -> None:
    """Fail-closed on item names that don't exist in the catalog (typo guard).

    Checks every place a config can reference a catalog item by name: ``enable``/
    ``disable`` lists and ``items.<name>`` keys, across all categories. Skills are special:
    universal/by_type are separate groups, and by_type uses fully-qualified
    ``by-type/<kind>/<name>`` keys plus bare ``<kind>`` bundle names in ``by_type.enable``.
    """
    _validate_skill_names(config, catalog)

    # agent_hooks + mcp — flat items/enable/disable
    for cat_name in ("agent_hooks", "mcp"):
        cfg = config.category(cat_name)
        if not isinstance(cfg, dict):
            continue
        known = catalog.names(cat_name)
        refs = set(cfg.get("enable", []) or []) | set(cfg.get("disable", []) or [])
        items = cfg.get("items", {})
        if isinstance(items, dict):
            refs |= set(items)
        _check_unknown_names(config, cat_name, refs, known, f"{cat_name}.items")

    # git_hooks — nested sub-groups (only 'dispatcher' is shipped in v0.1). A typo like
    # 'dispatcherr' must fail closed, not silently build no dispatcher action.
    gh = config.category("git_hooks")
    if isinstance(gh, dict):
        gh_known = catalog.names("git_hooks") | {"templates"}  # templates reserved for v0.2
        _check_unknown_names(config, "git_hooks", set(gh), gh_known, "git_hooks")


def _check_unknown_names(
    config: LoadedConfig, category: str, names: set[str], known: set[str], key_prefix: str
) -> None:
    """Raise a structured :class:`errors.UnknownItemError` for the first unknown name.

    ``category`` is the catalog category used for the removed-slot lookup + did-you-mean;
    ``key_prefix`` is the dotted config-key path (e.g. ``mcp.items``) the bad name hangs
    off, so the error names the EXACT offending key (``mcp.items.review``) + its file. The
    file is resolved by the key's PROVENANCE (``source_for_key``): a stale entry that came
    solely from the global config is reported against the global file, not the repo's.
    """
    from . import errors

    unknown = sorted(names - known)
    if unknown:
        bad = unknown[0]
        key = f"{key_prefix}.{bad}"
        raise errors.unknown_item_error(
            category=category,
            key=key,
            bad=bad,
            known=known,
            config_path=str(config.source_for_key(key)),
        )


def _validate_skill_names(config: LoadedConfig, catalog: Catalog) -> None:
    """Typo-guard the skills category: universal, by_type and by_stack groups."""
    sk = config.category("skills")
    skills = catalog.by_category("skills")
    _validate_universal_skill_names(config, sk, skills)
    _validate_by_type_skill_names(config, sk, skills)
    _validate_by_stack_skill_names(config, sk, skills)


def _validate_universal_skill_names(config: LoadedConfig, sk: Any, skills: list[Item]) -> None:
    # skills — universal group
    uni = sk.get("universal", {}) if isinstance(sk, dict) else {}
    if isinstance(uni, dict):
        uni_known = {i.name for i in skills if i.group == "universal"}
        refs = set(uni.get("enable", []) or []) | set(uni.get("disable", []) or [])
        refs |= set(k for k in uni.get("items", {}) if isinstance(uni.get("items"), dict))
        _check_unknown_names(config, "skills", refs, uni_known, "skills.universal")


def _validate_by_type_skill_names(config: LoadedConfig, sk: Any, skills: list[Item]) -> None:
    # skills — by_type group (fully-qualified item keys; bundle names checked separately)
    bt = sk.get("by_type", {}) if isinstance(sk, dict) else {}
    if isinstance(bt, dict):
        bt_known = {i.name for i in skills if i.group.startswith("by-type/")}
        bt_kinds = {i.meta.get("kind", "") for i in skills if i.group.startswith("by-type/")}
        bt_items = bt.get("items", {})
        if isinstance(bt_items, dict):
            _check_unknown_names(config, "skills", set(bt_items), bt_known, "skills.by_type.items")
        _check_unknown_names(config, "skills", set(bt.get("enable", []) or []), bt_kinds, "skills.by_type.enable")


def _validate_by_stack_skill_names(config: LoadedConfig, sk: Any, skills: list[Item]) -> None:
    # skills — by_stack group (fully-qualified `by-stack/<l1>/<lang>[/<fw>]/<name>` item keys)
    bs = sk.get("by_stack", {}) if isinstance(sk, dict) else {}
    if isinstance(bs, dict):
        bs_known = {i.name for i in skills if i.group.startswith("by-stack/")}
        bs_items = bs.get("items", {})
        refs = set(bs.get("disable", []) or [])
        if isinstance(bs_items, dict):
            refs |= set(bs_items)
        _check_unknown_names(config, "skills", refs, bs_known, "skills.by_stack.items")


def _plan_agent_hooks(plan: InstallPlan, config: LoadedConfig, catalog: Catalog) -> None:
    """Emit one ``install_agent_hook`` action per descriptor of every enabled hook.

    A hook dir may ship MORE THAN ONE ``*.json`` descriptor (e.g. a pre-bash AND a pre-write
    guard). Emit one action per descriptor so all of them land — taking only the first
    silently drops the rest (#184 gap). Fail-closed on a destination collision: descriptors
    from different hooks share one flat target dir, so two hooks shipping the same basename
    would clobber each other (one guard silently lost, drift never clean).
    """
    ah = config.category("agent_hooks")
    if ah.get("enabled") is False:
        return
    ah.setdefault("all", True)
    hook_targets = _resolve_agent_hooks_targets(config)
    seen: dict[Path, str] = {}
    for item in catalog.by_category("agent_hooks"):
        if _item_enabled(ah, item, type_enabled=False):
            spec = ah.get("items", {}).get(item.name, {})
            _plan_hook_item(plan, item, spec, hook_targets, catalog, seen)


def _plan_hook_item(
    plan: InstallPlan, item: Item, spec: Any, hook_targets: list[Path], catalog: Catalog, seen: dict[Path, str]
) -> None:
    """One ``install_agent_hook`` action per (target, descriptor) of an enabled hook."""
    # Drop empty names so a descriptor-less item can't produce a degenerate
    # ``hooks_target / ""`` (which collapses to the bare dir). A catalog item always
    # carries ≥1 descriptor; the meta fallback only guards hand-built Items.
    descriptors = tuple(d for d in (item.descriptors or (item.meta.get("descriptor", ""),)) if d)
    for hooks_target in hook_targets:
        for descriptor in descriptors:
            _claim_hook_destination(seen, item, hooks_target, descriptor)
            plan.actions.append(
                Action(
                    kind="install_agent_hook",
                    category="agent_hooks",
                    item=item.name,
                    source=item.path,
                    target=hooks_target,
                    options={
                        "descriptor": descriptor,
                        "on_error": spec.get("on_error") if isinstance(spec, dict) else None,
                        "agent_tools_source": str(catalog.source),
                    },
                )
            )


def _claim_hook_destination(seen: dict[Path, str], item: Item, hooks_target: Path, descriptor: str) -> None:
    """Record ``item`` as owner of the descriptor destination; raise on a cross-hook collision."""
    dest = hooks_target / descriptor
    if dest in seen and seen[dest] != item.name:
        raise PlanError(
            f"agent-hook descriptor collision: '{item.name}' and "
            f"'{seen[dest]}' both install '{descriptor}' into {hooks_target} — "
            f"one guard would silently clobber the other. Rename the descriptor "
            f"in agent-tools so hook basenames are unique."
        )
    seen[dest] = item.name


def _plan_hook_blurbs(plan: InstallPlan, config: LoadedConfig, catalog: Catalog, skills_target: Path) -> None:
    """Emit one ``sync_hook_blurbs`` action mirroring every ``agent-hooks/<name>/blurb.md`` under
    the resolved agent-tools checkout into ``<skills_target>/.blurbs/<name>.md`` (agent-tools#504).

    ``<skills_target>/.blurbs`` is the SAME SessionStart aggregator dir the 7 per-tool CLI
    installers already write their own ``.blurbs/<tool>.md`` into (:data:`riglib.tools.BLURBS_SUBDIR`)
    — an agent-hook policy (orchestrator-stays-thin, background-subagent-gate, …) rides the exact
    same upfront-discovery channel instead of only being learned reactively mid-task. A hook with
    no ``blurb.md`` opts out silently (see :mod:`riglib.hook_blurb_sync`).

    Gated on ``agent_hooks.enabled`` like :func:`_plan_agent_hooks`: no hook guards installed
    means no hook policy worth advertising upfront. One action regardless of ``agent_hooks.all``/
    per-item toggles — the sync walks agent-tools' own ``agent-hooks/`` dir directly (it is not
    catalog-item-scoped), same as the source of truth the descriptors themselves come from.
    """
    ah = config.category("agent_hooks")
    if ah.get("enabled") is False:
        return
    plan.actions.append(
        Action(
            kind="sync_hook_blurbs",
            category="agent_hooks",
            item="blurbs",
            source=catalog.source,
            target=skills_target / BLURBS_SUBDIR,
        )
    )


def _catalog_by_leaf(catalog: Catalog, category: str) -> dict[str, tuple[Path, ...]]:
    """Catalog item source paths grouped by on-disk leaf name (see ``InstallPlan.catalog_items``)."""
    out: dict[str, list[Path]] = {}
    for item in catalog.by_category(category):
        out.setdefault(item.name.rsplit("/", 1)[-1], []).append(item.path)
    return {leaf: tuple(paths) for leaf, paths in out.items()}
