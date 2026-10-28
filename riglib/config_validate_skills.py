"""Validator for the ``skills`` config block and its by_stack / plugins item maps.

Separate from the orchestrator because the skills block has its own sub-blocks
(universal/by_type/by_stack/plugins) with per-item shape checks. Error text and
``schema_path`` values are contract; do not reword.
"""

from __future__ import annotations

from typing import Any

from .config_checks import check_bool, check_str, check_str_list, reject_unknown_keys
from .config_errors import ConfigError

_SKILL_SUBBLOCKS = ("universal", "by_type", "by_stack", "plugins")


def _check_harness_knobs(sk: dict[str, Any]) -> None:
    harness_link = sk.get("harness_link")
    if harness_link is not None and not isinstance(harness_link, bool):
        raise ConfigError(
            f"skills.harness_link must be a bool, got {harness_link!r}",
            schema_path="skills.harness_link",
        )
    harness_skill_dir = sk.get("harness_skill_dir")
    if harness_skill_dir is not None and not isinstance(harness_skill_dir, str):
        raise ConfigError(
            f"skills.harness_skill_dir must be a string, got {harness_skill_dir!r}",
            schema_path="skills.harness_skill_dir",
        )


def _check_subblocks(sk: dict[str, Any]) -> None:
    # The schema closes each sub-block's FIXED keys; the catalog-keyed `items` maps stay open.
    for sub in _SKILL_SUBBLOCKS:
        block = sk.get(sub)
        if block is None:
            continue
        if not isinstance(block, dict):
            raise ConfigError(
                f"skills.{sub} must be a mapping, got {block!r}", schema_path=f"skills.{sub}"
            )
        reject_unknown_keys(block, f"skills.{sub}")


def validate_skills(sk: dict[str, Any]) -> None:
    """Validate the skill-discovery knobs (``harness_link`` / ``harness_skill_dir``)."""
    # ``validate()`` runs the "category must be a mapping" check before this, so ``sk`` is a
    # dict here (a bare ``skills:`` → None is rejected there). Guard defensively anyway.
    if not isinstance(sk, dict):
        return
    reject_unknown_keys(sk, "skills")
    check_bool(sk, "enabled", "skills.enabled")
    check_str(sk, "target", "skills.target")
    check_bool(sk, "all", "skills.all")
    check_str_list(sk, "known", "skills.known")
    _check_harness_knobs(sk)
    _check_subblocks(sk)
    validate_by_stack_items(sk.get("by_stack"))
    validate_skills_plugins(sk.get("plugins"))


def validate_by_stack_items(bs: Any) -> None:
    """Fail-closed on a malformed ``skills.by_stack.items.<name>`` spec.

    Each item must be a mapping and a present ``enabled`` a real bool, so a typo'd
    ``items.foo: "yes"`` is not silently coerced or ignored."""
    if not isinstance(bs, dict):
        return
    items = bs.get("items")
    if items is None:
        return
    if not isinstance(items, dict):
        raise ConfigError(
            "skills.by_stack.items must be a mapping", schema_path="skills.by_stack.items"
        )
    for name, spec in items.items():
        path = f"skills.by_stack.items.{name}"
        if not isinstance(spec, dict):
            raise ConfigError(f"{path} must be a mapping, got {spec!r}", schema_path=path)
        enabled = spec.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            raise ConfigError(
                f"{path}.enabled must be a bool, got {enabled!r}", schema_path=f"{path}.enabled"
            )


def validate_skills_plugins(spec: Any) -> None:
    """Fail-closed on a malformed ``skills.plugins.items.<name>`` spec.

    ``<name>`` is a Claude Code plugin name; ``marketplace`` names the marketplace it came from
    (see :mod:`riglib.plugin_skills`). Each item must be a mapping with a non-empty string
    ``marketplace`` so a typo fails here rather than being unresolvable at apply time."""
    if not isinstance(spec, dict):
        return
    items = spec.get("items")
    if items is None:
        return
    if not isinstance(items, dict):
        raise ConfigError(
            "skills.plugins.items must be a mapping", schema_path="skills.plugins.items"
        )
    for name, item_spec in items.items():
        path = f"skills.plugins.items.{name}"
        if not isinstance(item_spec, dict):
            raise ConfigError(f"{path} must be a mapping, got {item_spec!r}", schema_path=path)
        marketplace = item_spec.get("marketplace")
        if not isinstance(marketplace, str) or not marketplace:
            raise ConfigError(
                f"{path}.marketplace must be a non-empty string, got {marketplace!r}",
                schema_path=f"{path}.marketplace",
            )
