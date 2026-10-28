"""Opencode and omp hook-bridge registration handlers."""

from __future__ import annotations

import shutil

from . import fsutil
from . import hook_bridge_wrappers
from ..plan import Action
from .hook_bridge import opencode_hook_bridge_plugin_target
from .hook_bridge_wrappers import (
    _omp_symlink_is_rig_managed,
    _omp_wrapper_file_is_rig_managed,
    _omp_wrapper_text_is_rig_managed,
    _reconcile_opencode_bridge_exclude,
    omp_hook_bridge_extension_target,
    omp_hook_bridge_uses_wrapper,
    omp_hook_bridge_wrapper_text,
    opencode_hook_bridge_uses_wrapper,
    opencode_hook_bridge_wrapper_text,
)
from .result import ActionResult
from .shared import _should_backup
from .skills import _link_targets_itself, _same_link_dest



def _opencode_finalize(action: Action, plugin_path, dest, status: str, detail: str) -> ActionResult:
    """Fold the legacy-symlink cleanup and git-exclude reconcile into the registration result."""
    notes: list[str] = []
    legacy_removed, legacy_note = hook_bridge_wrappers._remove_legacy_opencode_bridge_symlink(plugin_path, dest)
    exclude_ok, exclude_changed, exclude_note = _reconcile_opencode_bridge_exclude(plugin_path)
    if not exclude_ok:
        return ActionResult(
            action,
            "error",
            f"hook_bridge/{action.item}: {detail}; {exclude_note} — plugin linked but NOT git-ignored",
        )
    if exclude_note:
        notes.append(exclude_note)
    if legacy_note and not legacy_removed:
        detail_with_notes = detail if not notes else f"{detail}; {'; '.join(notes)}"
        return ActionResult(
            action,
            "error",
            f"hook_bridge/{action.item}: {detail_with_notes}; {legacy_note}",
        )
    if legacy_note:
        notes.append(legacy_note)
    final_status = "updated" if status == "skipped" and (legacy_removed or exclude_changed) else status
    final_detail = detail if not notes else f"{detail}; {'; '.join(notes)}"
    return ActionResult(action, final_status, final_detail)


def _opencode_left_untouched(action: Action, plugin_path) -> ActionResult:
    return ActionResult(
        action,
        "skipped",
        f"hook_bridge/{action.item}: existing opencode plugin at {plugin_path} "
        "(on_conflict=skip), left untouched",
    )


def _displace_opencode_path(plugin_path, on_conflict: str) -> str:
    """Clear whatever occupies the plugin path; returns the backup note (empty if none)."""
    if plugin_path.is_symlink():
        plugin_path.unlink()
    elif _should_backup(on_conflict):
        bak = fsutil.backup_path(plugin_path)
        shutil.move(str(plugin_path), str(bak))
        return f" (backed up prior → {bak})"
    elif plugin_path.is_dir():
        shutil.rmtree(plugin_path)
    else:
        plugin_path.unlink()
    return ""


def _opencode_register_wrapper(action: Action, on_conflict: str, plugin_path, dest) -> ActionResult:
    desired = opencode_hook_bridge_wrapper_text(action)
    if plugin_path.is_file() and not plugin_path.is_symlink():
        try:
            if plugin_path.read_text(encoding="utf-8") == desired:
                return _opencode_finalize(
                    action, plugin_path, dest, "skipped",
                    f"hook_bridge/{action.item}: opencode plugin wrapper already written → {dest}",
                )
        except OSError as exc:
            return ActionResult(
                action,
                "error",
                f"hook_bridge/{action.item}: cannot read plugin wrapper {plugin_path}: {exc}",
            )

    backup_note = ""
    replaced_existing = plugin_path.exists() or plugin_path.is_symlink()
    if replaced_existing:
        if on_conflict == "skip":
            return _opencode_left_untouched(action, plugin_path)
        backup_note = _displace_opencode_path(plugin_path, on_conflict)
    plugin_path.write_text(desired, encoding="utf-8")
    status = "backed_up" if backup_note else ("updated" if replaced_existing else "created")
    return _opencode_finalize(
        action, plugin_path, dest, status,
        f"hook_bridge/{action.item}: wrote opencode plugin wrapper {plugin_path} → {dest}{backup_note}",
    )


def _opencode_register_symlink(action: Action, on_conflict: str, plugin_path, dest) -> ActionResult:
    if plugin_path.is_symlink():
        try:
            current = plugin_path.readlink()
        except OSError as exc:
            return ActionResult(action, "error", f"hook_bridge/{action.item}: cannot read symlink {plugin_path}: {exc}")
        if _same_link_dest(plugin_path, current, dest):
            return _opencode_finalize(
                action, plugin_path, dest, "skipped",
                f"hook_bridge/{action.item}: opencode plugin already linked → {dest}",
            )
        plugin_path.unlink()
        plugin_path.symlink_to(dest)
        return _opencode_finalize(
            action, plugin_path, dest, "updated",
            f"hook_bridge/{action.item}: re-pointed opencode plugin → {dest}",
        )

    backup_note = ""
    replaced_existing = plugin_path.exists()
    if replaced_existing:
        if on_conflict == "skip":
            return _opencode_left_untouched(action, plugin_path)
        backup_note = _displace_opencode_path(plugin_path, on_conflict)

    plugin_path.symlink_to(dest)
    status = "backed_up" if backup_note else ("updated" if replaced_existing else "created")
    return _opencode_finalize(
        action, plugin_path, dest, status,
        f"hook_bridge/{action.item}: linked opencode plugin {plugin_path} → {dest}{backup_note}",
    )


def _do_register_opencode_hook_bridge(action: Action, on_conflict: str) -> ActionResult:
    """Register opencode_hook_bridge by symlinking or wrapping its auto-loaded plugin."""
    plugin_path, dest = opencode_hook_bridge_plugin_target(action)
    if not dest.is_file():
        return ActionResult(action, "error", f"hook_bridge/{action.item}: bridge plugin missing: {dest}")
    if _link_targets_itself(plugin_path, dest):
        # plugin_path IS the source plugin.js — linking (or wrapper-writing) here would replace
        # the real git-tracked module with a self-symlink (or overwrite it). Refuse; leave it.
        return ActionResult(
            action, "skipped",
            f"hook_bridge/{action.item}: source == target ({dest}), skipping to avoid self-symlink",
        )
    plugin_path.parent.mkdir(parents=True, exist_ok=True)
    if opencode_hook_bridge_uses_wrapper(action):
        return _opencode_register_wrapper(action, on_conflict, plugin_path, dest)
    return _opencode_register_symlink(action, on_conflict, plugin_path, dest)


def _omp_left_untouched(action: Action, extension_path) -> ActionResult:
    return ActionResult(
        action, "skipped",
        f"hook_bridge/{action.item}: existing omp extension at {extension_path} "
        "(on_conflict=skip), left untouched",
    )


def _displace_omp_path(extension_path, on_conflict: str) -> str:
    """Clear a foreign occupant of the extension path; returns the backup note (empty if none)."""
    if _should_backup(on_conflict):
        # shutil.move on a symlink moves the LINK itself (os.rename), never its target —
        # so a foreign symlink is preserved as a restorable .rig-bak-* link.
        bak = fsutil.backup_path(extension_path)
        shutil.move(str(extension_path), str(bak))
        return f" (backed up prior → {bak})"
    if extension_path.is_symlink():
        # before is_dir(): rmtree refuses a symlink, and is_dir() follows one.
        extension_path.unlink()
    elif extension_path.is_dir():
        shutil.rmtree(extension_path)
    else:
        extension_path.unlink()
    return ""


def _omp_current_wrapper_text(action: Action, extension_path) -> tuple[str | None, ActionResult | None]:
    """(text of an existing regular file at the path, read-error result). Text is None when
    there is no such file or it is not UTF-8 — a non-UTF-8 file is never a byte-identical
    wrapper, so it is treated like any other differing/foreign file (backup/overwrite/skip per
    on_conflict), never a crash over a decode error."""
    if not extension_path.is_file() or extension_path.is_symlink():
        return None, None
    try:
        return extension_path.read_text(encoding="utf-8"), None
    except OSError as exc:
        return None, ActionResult(
            action, "error",
            f"hook_bridge/{action.item}: cannot read extension wrapper {extension_path}: {exc}",
        )
    except UnicodeDecodeError:
        return None, None


def _omp_register_wrapper(action: Action, on_conflict: str, extension_path, dest) -> ActionResult:
    desired = omp_hook_bridge_wrapper_text(action)
    current_text, read_error = _omp_current_wrapper_text(action, extension_path)
    if read_error is not None:
        return read_error
    if current_text == desired:
        return ActionResult(
            action, "skipped",
            f"hook_bridge/{action.item}: omp hook bridge wrapper already written → {dest}",
        )

    backup_note = ""
    replaced_existing = extension_path.exists() or extension_path.is_symlink()
    # A prior plain-symlink install (before hook_bridge.python/a custom agent_hooks.target
    # was added), OR an earlier wrapper WE wrote whose content is now stale (a DIFFERENT
    # python/hooks_dir configured since), is rig's OWN artifact, not foreign data — always
    # safe to replace regardless of on_conflict, exactly like the plain-symlink branch
    # does. Gating either case on on_conflict=skip made a mode/content switch silently never
    # converge: apply would report "left untouched" forever while drift kept reporting drift
    # forever, and the newly-configured python/hooks_dir override would never actually get
    # wired (review-cli findings on rig-cli#342 — both the symlink case and this wrapper
    # -content-changed case were caught, independently, across two review rounds).
    # ``current_text`` is already known not to equal ``desired`` here (the early-return
    # above catches the identical case), so a match on the header alone is enough — no need
    # to distinguish "content differs because config changed" from "some other rig version".
    # ONLY a symlink that demonstrably targets the bridge counts as ours (a user's own
    # symlink to some other extension at this path is foreign config and goes through the
    # on_conflict path — PR #352 review thread).
    is_rig_owned = _omp_symlink_is_rig_managed(extension_path, dest) or (
        current_text is not None and _omp_wrapper_text_is_rig_managed(current_text)
    )
    if is_rig_owned:
        extension_path.unlink()
    elif replaced_existing:
        if on_conflict == "skip":
            return _omp_left_untouched(action, extension_path)
        backup_note = _displace_omp_path(extension_path, on_conflict)
    extension_path.write_text(desired, encoding="utf-8")
    status = "backed_up" if backup_note else ("updated" if replaced_existing else "created")
    return ActionResult(
        action, status,
        f"hook_bridge/{action.item}: wrote omp extension wrapper {extension_path} → {dest}{backup_note}",
    )


def _omp_repoint_existing_symlink(action: Action, extension_path, dest) -> ActionResult | None:
    """Handle a symlink already at the path; None when it is a FOREIGN symlink, which is user
    configuration and falls through to the on_conflict handling (skip leaves it, backup
    preserves the link as .rig-bak-* — PR #352 review thread)."""
    try:
        current = extension_path.readlink()
    except OSError as exc:
        return ActionResult(action, "error", f"hook_bridge/{action.item}: cannot read symlink {extension_path}: {exc}")
    if _same_link_dest(extension_path, current, dest):
        return ActionResult(action, "skipped", f"hook_bridge/{action.item}: omp extension already linked → {dest}")
    if _omp_symlink_is_rig_managed(extension_path, dest):
        # ours (a prior checkout's bridge) — a stale rig symlink carries no user data, so
        # re-point it regardless of on_conflict, like link_skill_harness does.
        extension_path.unlink()
        extension_path.symlink_to(dest)
        return ActionResult(action, "updated", f"hook_bridge/{action.item}: re-pointed omp extension → {dest}")
    return None


def _omp_register_symlink(action: Action, on_conflict: str, extension_path, dest) -> ActionResult:
    if extension_path.is_symlink():
        repointed = _omp_repoint_existing_symlink(action, extension_path, dest)
        if repointed is not None:
            return repointed

    if _omp_wrapper_file_is_rig_managed(extension_path):
        # A rig-authored wrapper LOSING its hooks_dir/python config (hook_bridge.python removed,
        # or agent_hooks.target dropped back to the default) — rig's own prior artifact, always
        # safe to replace with the plain symlink regardless of on_conflict, mirroring the
        # symlink-to-symlink re-point just above. Without this, this exact wrapper→symlink
        # direction silently never converged under on_conflict=skip (review-cli, rig-cli#342).
        extension_path.unlink()
        extension_path.symlink_to(dest)
        return ActionResult(
            action, "updated",
            f"hook_bridge/{action.item}: replaced omp wrapper with plain extension symlink → {dest}",
        )

    backup_note = ""
    # `or is_symlink()`: a dangling FOREIGN symlink has exists()==False but still occupies the
    # path and is still the user's — it must go through on_conflict too, never be clobbered.
    replaced_existing = extension_path.exists() or extension_path.is_symlink()
    if replaced_existing:
        if on_conflict == "skip":
            return _omp_left_untouched(action, extension_path)
        backup_note = _displace_omp_path(extension_path, on_conflict)

    extension_path.symlink_to(dest)
    status = "backed_up" if backup_note else ("updated" if replaced_existing else "created")
    return ActionResult(
        action, status,
        f"hook_bridge/{action.item}: linked omp extension {extension_path} → {dest}{backup_note}",
    )


def _do_register_omp_hook_bridge(action: Action, on_conflict: str) -> ActionResult:
    """Register omp_hook_bridge by symlinking (or wrapping) its auto-discovered extension.

    Simpler than :func:`_do_register_opencode_hook_bridge`: there is no legacy predecessor to
    clean up (this bridge is new) and omp auto-loads every ``.ts``/``.js`` module under
    ``extensions/`` — no explicit registration step beyond the file's presence. The DEFAULT
    target is a user-level path under the resolved omp agent root, but — like codex's and
    opencode's ``settings_path`` — an explicit override can point anywhere, including inside a
    repo. Unlike opencode's DEFAULT repo-local plugin path, rig does NOT auto-exclude an
    explicit repo-local omp override from git: that convention exists for opencode's own
    default, not as a general "any harness path under a repo" rule, so a user who deliberately
    overrides ``settings_path`` into their repo owns their own ``.gitignore`` entry for it.
    """
    extension_path, dest = omp_hook_bridge_extension_target(action)
    if not dest.is_file():
        return ActionResult(action, "error", f"hook_bridge/{action.item}: bridge extension missing: {dest}")
    if _link_targets_itself(extension_path, dest):
        # extension_path IS the source extension.ts — linking (or wrapper-writing) here would
        # replace the real git-tracked module with a self-symlink (or overwrite it). Refuse.
        return ActionResult(
            action, "skipped",
            f"hook_bridge/{action.item}: source == target ({dest}), skipping to avoid self-symlink",
        )
    extension_path.parent.mkdir(parents=True, exist_ok=True)
    if omp_hook_bridge_uses_wrapper(action):
        return _omp_register_wrapper(action, on_conflict, extension_path, dest)
    return _omp_register_symlink(action, on_conflict, extension_path, dest)
