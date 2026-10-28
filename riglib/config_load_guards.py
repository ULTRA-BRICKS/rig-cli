"""Per-layer placement guards the cascade loader runs on each raw config file before merging it.

Why a separate module: which blocks may come from the GLOBAL layer only (machine-wide policy a
committed repo ``rig.yaml`` must never set) and which from the repo only is policy, independent of
how layers are read and merged. Each guard raises the same fail-closed :class:`ConfigError` the
loader always did, so ``load`` stays a short orchestration of guards plus accumulation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config_constants import GLOBAL_LAYER_ONLY_BLOCKS, REPO_LAYER_FJ_KEYS, SHIP_POLICY_KEYS
from .config_errors import ConfigError
from .config_paths import global_config_path


def _reject_repo_layer_mode(data: dict[str, Any], path: Path, label: str) -> None:
    """``mode`` is machine-wide policy: the committed repo layer must not set it."""
    if label == "repo" and "mode" in data:
        raise ConfigError(
            "mode is a global-only config block",
            why=f"{path} is loaded as the repo layer, but mode is machine-wide policy",
            fix=f"move mode to {global_config_path()} or run `rig config set mode.name ... --global`",
            schema_path="mode",
        )


def _reject_global_only_blocks(data: dict[str, Any], path: Path, label: str) -> None:
    """Blocks in ``GLOBAL_LAYER_ONLY_BLOCKS`` may not appear in any non-global layer."""
    for block, reason in GLOBAL_LAYER_ONLY_BLOCKS.items():
        if block in data:
            raise ConfigError(
                f"{block} is a global-only config block",
                why=f"{path} is loaded as the {label} layer, but {reason} — a repo "
                "(or --config) layer must not set it",
                fix=f"move {block} to {global_config_path()} "
                f"(e.g. `rig config set {block}.<key> <value> --global`)",
                schema_path=block,
            )


def _reject_fj_machine_keys(data: dict[str, Any], path: Path, label: str) -> None:
    """A non-global layer may set only ``fj.host``; every other fj key is machine-wide."""
    fj_block = data.get("fj")
    machine_keys = sorted(set(fj_block) - REPO_LAYER_FJ_KEYS) if isinstance(fj_block, dict) else []
    if machine_keys:
        raise ConfigError(
            f"fj.{machine_keys[0]} is a global-only config key",
            why=f"{path} is loaded as the {label} layer, but fj.{', fj.'.join(machine_keys)} "
            "change the machine's fj install or the OAuth app fj and git log in with — a "
            "repo (or --config) layer may only set fj.host",
            fix=f"move them to {global_config_path()} (e.g. `rig config set fj.{machine_keys[0]} <value> --global`)",
            schema_path=f"fj.{machine_keys[0]}",
        )


def _ship_items(data: dict[str, Any]) -> Any:
    """``data["ci"]["items"]["ship"]``, or ``{}`` as soon as any level is absent or not a mapping."""
    node: Any = data.get("ci", {})
    for part in ("items", "ship"):
        node = node.get(part, {}) if isinstance(node, dict) else {}
    return node


def _reject_ship_policy(data: dict[str, Any]) -> None:
    """The external-review merge-authority policy keys are machine-level: global layer only."""
    ship = _ship_items(data)
    if isinstance(ship, dict) and any(key in ship for key in SHIP_POLICY_KEYS):
        raise ConfigError(
            "ci.items.ship external-review policy is global-only",
            why="An unreviewed repository must not define its own machine-level merge authority",
            fix=f"move these policy keys to {global_config_path()}",
            schema_path="ci.items.ship",
        )


def reject_misplaced_blocks(data: dict[str, Any], path: Path, label: str) -> None:
    """Reject machine-wide settings in a repo/--config layer (runs BEFORE the layer's lint blocks)."""
    _reject_repo_layer_mode(data, path, label)
    if label != "global":
        _reject_global_only_blocks(data, path, label)
        _reject_fj_machine_keys(data, path, label)
        _reject_ship_policy(data)


def reject_repo_only_in_global(data: dict[str, Any], path: Path, label: str) -> None:
    """``internal_dev`` is repo-specific: a global block would cascade into every repo apply."""
    if label == "global" and data.get("internal_dev"):
        raise ConfigError(
            "internal_dev is a repo-only config block",
            why=(
                f"{path} is the GLOBAL layer, but internal_dev.daemon_source_paths is "
                "repo-specific — a global block would silently cascade into every repo apply"
            ),
            fix="move internal_dev into this repo's committed ./rig.yaml",
            schema_path="internal_dev",
        )
