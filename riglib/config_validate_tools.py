"""Validators for the machine-level helper blocks: ``tools``, ``tg_ctl``, ``tcc_watch``, ``tgctl_reaper``.

Grouped because all four are small per-MACHINE (global-layer) blocks built from the same few
typed-knob checks; keeping them together avoids four near-empty modules.
"""

from __future__ import annotations

from typing import Any

from .config_checks import check_bool, check_str, check_str_list, reject_unknown_keys
from .config_errors import ConfigError


def _check_bools(block: dict[str, Any], prefix: str, keys: tuple[str, ...]) -> None:
    for key in keys:
        check_bool(block, key, f"{prefix}.{key}")


def _check_strs(block: dict[str, Any], prefix: str, keys: tuple[str, ...]) -> None:
    for key in keys:
        check_str(block, key, f"{prefix}.{key}")


def validate_tools(t: dict[str, Any]) -> None:
    """Validate the ``tools`` block — the personal CLI ecosystem rig installs at apply.

    A per-MACHINE concern (the tool ecosystem on this dev box), so it belongs in the GLOBAL layer
    (``~/.config/rig/config.yaml``), like ``harness``/``tmux``/``tg_ctl`` — NOT a committed repo
    ``rig.yaml``. Default **OFF** (opt-in): an absent/empty block provisions nothing; a machine
    opts in by listing tools under ``items``. Fail-closed, consistent with every other block, on:
    a non-mapping block, an unknown FIXED key (typo guard — the open ``items`` map keeps arbitrary
    tool NAMES valid), a non-bool ``enabled``, a non-string ``target``, a non-mapping ``items`` or
    item, and a bad per-item ``enabled``/``repo``/``bin_dir``/``brew``/``apt``/``clone`` type
    (``brew``/``apt`` are lists of package names, checked with :func:`check_str_list`).
    """
    if not isinstance(t, dict):
        raise ConfigError("tools must be a mapping", schema_path="tools")
    if not t:
        return
    reject_unknown_keys(t, "tools")
    check_bool(t, "enabled", "tools.enabled")
    check_str(t, "target", "tools.target")
    items = t.get("items", {})
    if not isinstance(items, dict):
        raise ConfigError("tools.items must be a mapping", schema_path="tools.items")
    for name, spec in items.items():
        validate_tools_item(name, spec)


# The keys a tools.items.<name> entry accepts. Explicit set (mirrors LINTER_ITEM_KEYS) so the
# validator rejects a per-item typo — reject_unknown_keys can't, since block_child_keys only
# walks .nested, never an open_map_item block.
_TOOLS_ITEM_KEYS = {"enabled", "repo", "bin_dir", "brew", "apt", "clone"}


def _clone_is_unsafe(clone: str) -> bool:
    return not clone.strip() or clone.startswith("-") or any(c in clone for c in "\r\n\x00")


def validate_tools_item(name: str, spec: Any) -> None:
    """Validate one ``tools.items.<name>`` entry — its shape and per-item knob types."""
    path = f"tools.items.{name}"
    if not isinstance(spec, dict):
        raise ConfigError(f"{path} must be a mapping", schema_path=path)
    for key in spec:
        if key not in _TOOLS_ITEM_KEYS:
            raise ConfigError(
                f"unknown {path} key {key!r} (expected one of: {', '.join(sorted(_TOOLS_ITEM_KEYS))})",
                schema_path=path,
            )
    check_bool(spec, "enabled", f"{path}.enabled")
    check_str(spec, "repo", f"{path}.repo")
    check_str(spec, "bin_dir", f"{path}.bin_dir")
    check_str_list(spec, "brew", f"{path}.brew")
    check_str_list(spec, "apt", f"{path}.apt")
    check_str(spec, "clone", f"{path}.clone")
    clone = spec.get("clone")
    if isinstance(clone, str) and _clone_is_unsafe(clone):
        raise ConfigError(
            f"{path}.clone must be a one-line git URL that does not start with '-', got {clone!r}",
            schema_path=f"{path}.clone",
        )


# The keys the tg_ctl block accepts. Listed once so the validator rejects a typo (fail-closed,
# consistent with every other block).
_TG_CTL_KEYS = {
    "enabled",
    "boot",
    "codex_usage_hook",
    "label",
    "bun_path",
    "tg_ctl_path",
    "config_dir",
}


def validate_tg_ctl(t: dict[str, Any]) -> None:
    """Validate the ``tg_ctl`` block — rig-managed tg-ctl inbound-daemon LaunchAgent.

    This is a per-MACHINE concern (one inbound Telegram control daemon per machine), so it
    belongs in the GLOBAL layer (``~/.config/rig/config.yaml``), like ``harness``/``tmux``/
    ``git_hooks`` — NOT a committed repo ``rig.yaml``. Default **ON**: an EMPTY/absent block
    still provisions the daemon (a present block with ``enabled`` not false opts in). Fail-closed,
    consistent with every other block, on: a non-mapping block, an unknown key (typo guard), a
    non-bool ``enabled``/``boot``/``codex_usage_hook``, and a non-string
    ``label``/``bun_path``/``tg_ctl_path``/``config_dir``.
    """
    if not isinstance(t, dict):
        raise ConfigError("tg_ctl must be a mapping", schema_path="tg_ctl")
    if not t:
        return
    reject_unknown_keys(t, "tg_ctl")
    _check_bools(t, "tg_ctl", ("enabled", "boot", "codex_usage_hook"))
    _check_strs(t, "tg_ctl", ("label", "bun_path", "tg_ctl_path", "config_dir"))


# The keys the tcc_watch block accepts. Listed once so the validator rejects a typo (fail-closed,
# consistent with every other block).
_TCC_WATCH_KEYS = {
    "enabled",
    "label",
    "config_dir",
    "python_bin",
}


def validate_tcc_watch(t: dict[str, Any]) -> None:
    """Validate the ``tcc_watch`` block — rig-managed TCC-dialog-watcher LaunchAgent (Phase 1
    detection-only defense-in-depth behind the eval-tool deny-list, PR #488; see
    :mod:`riglib.tcc_watch`'s module docstring for the full phased design).

    This is a per-MACHINE concern (one watcher daemon per machine), so it belongs in the GLOBAL
    layer (``~/.config/rig/config.yaml``), like ``tg_ctl``/``tmux``/``git_hooks`` — NOT a
    committed repo ``rig.yaml``. Default **ON**: an EMPTY/absent block still provisions the
    daemon (a present block with ``enabled`` not false opts in) — mirrors ``tg_ctl``. Fail-closed,
    consistent with every other block, on: a non-mapping block, an unknown key (typo guard), a
    non-bool ``enabled``, and a non-string ``label``/``config_dir``/``python_bin``.
    """
    if not isinstance(t, dict):
        raise ConfigError("tcc_watch must be a mapping", schema_path="tcc_watch")
    if not t:
        return
    reject_unknown_keys(t, "tcc_watch")
    _check_bools(t, "tcc_watch", ("enabled",))
    _check_strs(t, "tcc_watch", ("label", "config_dir", "python_bin"))


# The keys the tgctl_reaper block accepts. Listed once so the validator rejects a typo
# (fail-closed, consistent with every other block).
_TGCTL_REAPER_KEYS = {
    "enabled",
    "interval_seconds",
    "label",
    "config_dir",
    "python_bin",
}


def _check_reaper_interval(t: dict[str, Any]) -> None:
    interval = t.get("interval_seconds")
    # bool is an int subclass — reject it so `true` can't pose as a seconds count.
    if interval is not None and (isinstance(interval, bool) or not isinstance(interval, int) or interval < 1):
        raise ConfigError(
            f"tgctl_reaper.interval_seconds must be an int >= 1, got {interval!r}",
            schema_path="tgctl_reaper.interval_seconds",
        )


def validate_tgctl_reaper(t: dict[str, Any]) -> None:
    """Validate the ``tgctl_reaper`` block — rig-managed periodic sweep for orphaned tg-ctl/omp
    test-fixture helpers (agent-tools#697; see :mod:`riglib.tgctl_reaper`'s module docstring for
    the full rationale and :mod:`riglib.tgctl_reaper_daemon` for what the sweep actually does).

    This is a per-MACHINE concern (one reaper per machine), so it belongs in the GLOBAL layer
    (``~/.config/rig/config.yaml``), like ``tg_ctl``/``tcc_watch``/``tmux`` — NOT a committed
    repo ``rig.yaml``. Default **ON** (like ``tg_ctl``/``tcc_watch``): an EMPTY/absent block
    still provisions the sweep (a present block with ``enabled`` not false opts in). Fail-closed,
    consistent with every other block, on: a non-mapping block, an unknown key (typo guard), a
    non-bool ``enabled``, a non-int ``interval_seconds`` below 1, and a non-string
    ``label``/``config_dir``/``python_bin``.
    """
    if not isinstance(t, dict):
        raise ConfigError("tgctl_reaper must be a mapping", schema_path="tgctl_reaper")
    if not t:
        return
    reject_unknown_keys(t, "tgctl_reaper")
    _check_bools(t, "tgctl_reaper", ("enabled",))
    _check_reaper_interval(t)
    _check_strs(t, "tgctl_reaper", ("label", "config_dir", "python_bin"))
