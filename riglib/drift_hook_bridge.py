"""Drift checks for the hook bridge registered into claude-code / opencode / omp / codex."""

from __future__ import annotations

import json
from pathlib import Path

from .actions.hook_bridge import (
    find_managed_bridge_hook,
    hook_bridge_entries,
    hook_bridge_format,
    hook_bridge_module,
    hook_bridge_settings_file,
    managed_bridge_hook_drift_reason,
    opencode_hook_bridge_plugin_target,
)
from .actions.hook_bridge_codex import (
    codex_hook_bridge_block_bounds,
    codex_hook_bridge_block_malformed,
    codex_hook_bridge_conflict,
    codex_hook_bridge_disabled_features,
    merge_codex_hook_bridge_toml,
)
from .actions.hook_bridge_wrappers import omp_hook_bridge_extension_target
from .plan import Action
from .drift_model import DriftItem, DriftReport


def _check_hook_bridge(action: Action, report: DriftReport) -> None:
    """Flag drift between the configured hook-bridge wiring and the harness config.

    missing  — the settings file is absent, or a managed dispatcher hook (one whose command
               carries ``cc_hook_bridge``) is not present for an (event, matcher) we ship.
    modified — the settings file is malformed JSON, OR a managed hook is present but its
               TYPE/COMMAND differs from what apply would write (stale PYTHONPATH / moved
               checkout / changed ``hook_bridge.python`` / malformed hook type). ``rig apply``
               rewrites it.
    Drift and apply share ``find_managed_bridge_hook`` + ``hook_bridge_entries`` so they
    never diverge. Only OUR managed blocks are checked; the user's other hooks are ignored.
    """
    config_file = hook_bridge_settings_file(action)
    if hook_bridge_format(action) == "opencode-plugin":
        _check_opencode_hook_bridge(action, config_file, report)
        return
    if hook_bridge_format(action) == "omp-extension":
        _check_omp_hook_bridge(action, config_file, report)
        return
    if not config_file.is_file():
        report.items.append(
            DriftItem("missing", "harness", action.item, config_file, "harness settings file not written")
        )
        return
    if hook_bridge_format(action) == "toml":
        _check_codex_hook_bridge(action, config_file, report)
        return
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
    except ValueError:
        report.items.append(
            DriftItem("modified", "harness", action.item, config_file, "harness settings file is malformed JSON")
        )
        return
    hooks = data.get("hooks") if isinstance(data, dict) else None
    hooks = hooks if isinstance(hooks, dict) else {}
    _check_json_hook_entries(action, config_file, hooks, report)


def _check_json_hook_entries(action: Action, config_file: Path, hooks: dict, report: DriftReport) -> None:
    for event, pairs in hook_bridge_entries(action).items():
        blocks = hooks.get(event)
        for matcher, command in pairs:
            label = f"{event}[{matcher or '*'}]"
            hk = find_managed_bridge_hook(blocks, matcher, hook_bridge_module(action))
            if hk is None:
                report.items.append(
                    DriftItem("missing", "harness", action.item, config_file,
                              f"{hook_bridge_module(action)} not wired for {label}")
                )
                continue
            reason = managed_bridge_hook_drift_reason(hk, command)
            if reason is not None:
                report.items.append(
                    DriftItem("modified", "harness", action.item, config_file,
                              f"{hook_bridge_module(action)} hook for {label}: {reason}")
                )


def _check_opencode_hook_bridge(action: Action, config_file: Path, report: DriftReport) -> None:
    plugin_path, dest = opencode_hook_bridge_plugin_target(action)
    from .actions.hook_bridge_wrappers import (
        legacy_opencode_bridge_needs_cleanup,
        opencode_hook_bridge_uses_wrapper,
        opencode_hook_bridge_wrapper_text,
    )
    from .actions.skills import _link_targets_itself

    if _link_targets_itself(plugin_path, dest) and dest.is_file():
        # source == target with the real plugin present: the runner skips UNCONDITIONALLY here
        # (before the wrapper/symlink split, after its own dest.is_file() check) — it never
        # self-symlinks nor overwrites the real module. Drift must read this as in sync, else
        # status reports "real file (apply will replace it)" forever while apply keeps skipping,
        # an apply/status loop. Gated on the SAME `dest.is_file()` the runner checks first (same
        # path, same symlink-following semantics), so drift and apply agree on "real file
        # present": when it is ABSENT (or a broken self-symlink loop) the runner errors ("bridge
        # plugin missing"), so we fall through to the normal checks that surface it — never mask.
        return

    if opencode_hook_bridge_uses_wrapper(action):
        _check_opencode_wrapper(action, config_file, plugin_path, report, opencode_hook_bridge_wrapper_text)
    elif not plugin_path.is_symlink():
        _check_opencode_unlinked(action, config_file, plugin_path, report)
    else:
        _check_opencode_symlink(action, config_file, plugin_path, dest, report)
    if legacy_opencode_bridge_needs_cleanup(plugin_path, dest):
        report.items.append(
            _omp_item(action, "modified", config_file,
                      "legacy global opencode hook bridge symlink still present")
        )


def _check_opencode_exclude(action: Action, plugin_path: Path, report: DriftReport) -> None:
    from .actions.hook_bridge_wrappers import _opencode_bridge_exclude_context, _opencode_exclude_has_entry

    ctx = _opencode_bridge_exclude_context(plugin_path)
    if ctx is not None:
        exclude_path, rel_path = ctx
        if not _opencode_exclude_has_entry(exclude_path, rel_path):
            report.items.append(
                _omp_item(action, "missing", exclude_path, "opencode hook bridge plugin is not git-ignored")
            )


def _check_opencode_wrapper(action: Action, config_file: Path, plugin_path: Path,
                            report: DriftReport, wrapper_text) -> None:
    if not plugin_path.is_file() or plugin_path.is_symlink():
        if plugin_path.exists() or plugin_path.is_symlink():
            report.items.append(
                _omp_item(action, "modified", config_file,
                          "opencode hook bridge wrapper is not a regular file")
            )
        else:
            report.items.append(
                _omp_item(action, "missing", config_file, "opencode hook bridge wrapper not written")
            )
        return
    try:
        text = plugin_path.read_text(encoding="utf-8")
    except OSError as exc:
        report.items.append(
            _omp_item(action, "modified", config_file, f"opencode hook bridge wrapper unreadable: {exc}")
        )
        return
    if text != wrapper_text(action):
        report.items.append(
            _omp_item(action, "modified", config_file, "opencode hook bridge wrapper differs from config")
        )
    else:
        _check_opencode_exclude(action, plugin_path, report)


def _check_opencode_unlinked(action: Action, config_file: Path, plugin_path: Path,
                             report: DriftReport) -> None:
    if plugin_path.exists():
        report.items.append(
            _omp_item(action, "modified", config_file,
                      "opencode hook bridge plugin is a real file (apply will replace it)")
        )
    else:
        report.items.append(
            _omp_item(action, "missing", config_file, "opencode hook bridge plugin not linked")
        )


def _check_opencode_symlink(action: Action, config_file: Path, plugin_path: Path,
                            dest: Path, report: DriftReport) -> None:
    from .actions.skills import _same_link_dest

    try:
        current = plugin_path.readlink()
    except OSError as exc:
        report.items.append(
            _omp_item(action, "modified", config_file, f"opencode hook bridge symlink unreadable: {exc}")
        )
        return
    if not _same_link_dest(plugin_path, current, dest):
        report.items.append(
            _omp_item(action, "modified", config_file,
                      f"opencode hook bridge plugin points elsewhere, expected → {dest}")
        )
    else:
        _check_opencode_exclude(action, plugin_path, report)


def _check_omp_hook_bridge(action: Action, config_file: Path, report: DriftReport) -> None:
    """Flag drift for the omp extension symlink/wrapper — simpler than opencode's: no
    repo-local git-exclude reconciliation and no legacy predecessor to detect (the omp bridge
    is new; every install is user-level from day one)."""
    extension_path, dest = omp_hook_bridge_extension_target(action)
    # local import matching _check_opencode_hook_bridge's own established pattern just above
    # (it locally imports the same class of runner helpers rather than hoisting them to the
    # module-level import block) — consistency with the sibling function, not a new anti-pattern.
    from .actions.hook_bridge_wrappers import omp_hook_bridge_uses_wrapper, omp_hook_bridge_wrapper_text
    from .actions.skills import _link_targets_itself

    if _link_targets_itself(extension_path, dest) and dest.is_file():
        # source == target with the real extension present: the runner skips unconditionally
        # here too — see the parallel comment in _check_opencode_hook_bridge for why this must
        # read as in-sync rather than forever-drifting.
        return

    if omp_hook_bridge_uses_wrapper(action):
        _check_omp_wrapper(action, config_file, extension_path, report, omp_hook_bridge_wrapper_text)
        return
    _check_omp_extension_link(action, config_file, extension_path, dest, report)


def _omp_item(action: Action, direction: str, config_file: Path, detail: str) -> DriftItem:
    return DriftItem(direction, "harness", action.item, config_file, detail)


def _check_omp_wrapper(action: Action, config_file: Path, extension_path: Path,
                       report: DriftReport, wrapper_text) -> None:
    if not extension_path.is_file() or extension_path.is_symlink():
        if extension_path.exists() or extension_path.is_symlink():
            report.items.append(
                _omp_item(action, "modified", config_file, "omp hook bridge wrapper is not a regular file")
            )
        else:
            report.items.append(
                _omp_item(action, "missing", config_file, "omp hook bridge wrapper not written")
            )
        return
    try:
        text = extension_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        report.items.append(
            _omp_item(action, "modified", config_file, f"omp hook bridge wrapper unreadable: {exc}")
        )
        return
    if text != wrapper_text(action):
        report.items.append(
            _omp_item(action, "modified", config_file, "omp hook bridge wrapper differs from config")
        )


def _check_omp_extension_link(action: Action, config_file: Path, extension_path: Path,
                              dest: Path, report: DriftReport) -> None:
    from .actions.skills import _same_link_dest

    if not extension_path.is_symlink():
        if extension_path.exists():
            report.items.append(
                _omp_item(action, "modified", config_file,
                          "omp hook bridge extension is a real file (apply will replace it)")
            )
        else:
            report.items.append(
                _omp_item(action, "missing", config_file, "omp hook bridge extension not linked")
            )
        return
    try:
        current = extension_path.readlink()
    except OSError as exc:
        report.items.append(
            _omp_item(action, "modified", config_file, f"omp hook bridge symlink unreadable: {exc}")
        )
        return
    if not _same_link_dest(extension_path, current, dest):
        report.items.append(
            _omp_item(action, "modified", config_file,
                      f"omp hook bridge extension points elsewhere, expected → {dest}")
        )


def _check_codex_hook_bridge(action: Action, config_file: Path, report: DriftReport) -> None:
    try:
        text = config_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        report.items.append(
            DriftItem("modified", "harness", action.item, config_file, f"harness config unreadable: {exc}")
        )
        return
    if codex_hook_bridge_block_malformed(text):
        report.items.append(
            DriftItem("modified", "harness", action.item, config_file,
                      "managed Codex hook bridge markers are incomplete")
        )
        return
    bounds = codex_hook_bridge_block_bounds(text)
    stripped = text
    if bounds is not None:
        start, end = bounds
        stripped = text[:start] + text[end:]
    conflict = codex_hook_bridge_conflict(stripped)
    if conflict:
        report.items.append(
            DriftItem(
                "modified",
                "harness",
                action.item,
                config_file,
                f"{conflict} in {config_file}; rig apply will not overwrite unmanaged Codex hooks TOML",
            )
        )
        return
    disabled = codex_hook_bridge_disabled_features(text)
    if disabled:
        report.items.append(
            DriftItem(
                "modified",
                "harness",
                action.item,
                config_file,
                f"Codex hooks disabled by {', '.join(disabled)} in {config_file} (apply will enable)",
            )
        )
        return
    if bounds is None:
        report.items.append(
            DriftItem("missing", "harness", action.item, config_file,
                      f"codex_hook_bridge not wired in {config_file}")
        )
        return
    merged, merge_conflict = merge_codex_hook_bridge_toml(text, action)
    if merge_conflict:
        report.items.append(
            DriftItem(
                "modified",
                "harness",
                action.item,
                config_file,
                f"{merge_conflict} in {config_file}; rig apply will not overwrite unmanaged Codex hooks TOML",
            )
        )
        return
    if merged != text:
        report.items.append(
            DriftItem("modified", "harness", action.item, config_file,
                      "codex_hook_bridge TOML block is stale (apply will rewrite)")
        )
