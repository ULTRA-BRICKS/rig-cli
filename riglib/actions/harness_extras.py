"""Harness wrapper, dev-reload hook and pi extension handlers."""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import fsutil
from ..plan import Action
from .result import ActionResult
from .shared import _CHANGED_STATUSES, _chmod_x_if_changed


def _do_harness_wrapper(action: Action, on_conflict: str) -> ActionResult:
    """Install ONE per-harness PATH wrapper through ``harness_launcher.install_wrapper``.

    Idempotent: a wrapper whose text is already current (and a real-binary record already matching
    config) is ``skipped``; a missing one is ``created``; a rig-written one with different text, or
    a real-binary record to update, is ``updated``. A file or symlink at the target that rig did
    not write is an ``error`` and is left untouched — whatever ``on_conflict`` says, because it may
    be the real binary itself and a backup copy would take the harness off PATH. A rig-written
    wrapper is regenerated output, so ``on_conflict`` does not apply to it either. Judged by the
    same :func:`riglib.wrapper_provision.resolve` state ``rig status`` and verify read.
    """
    from .. import harness_launcher, wrapper_provision

    state = wrapper_provision.resolve(action)
    if state.wrapper == "relative":
        return ActionResult(action, "error", wrapper_provision.relative_detail(state))
    if state.wrapper == "foreign":
        return ActionResult(
            action,
            "error",
            f"refusing to overwrite: {wrapper_provision.foreign_detail(state)}; move it aside and re-run "
            "`rig apply commit`, or point harness_wrappers.dir elsewhere",
        )
    if state.real_bin_invalid:
        return ActionResult(action, "error", state.real_bin_invalid)
    try:
        changed = harness_launcher.install_wrapper(state.kind, state.target, real_bin=state.real_bin)
    except (ValueError, OSError) as exc:
        return ActionResult(action, "error", f"{state.target}: {exc}")
    if not changed:
        return ActionResult(action, "skipped", f"already current: {state.target}")
    notes = [f"wrote {state.target}"] if state.wrapper != "current" else []
    if not state.real_bin_current:
        notes.append(f"recorded real {state.kind} binary {state.wanted}")
    return ActionResult(action, "created" if state.wrapper == "missing" else "updated", "; ".join(notes))


def _do_install_dev_reload_hook(action: Action, on_conflict: str) -> ActionResult:
    """Install the per-repo post-commit daemon auto-reload hook (+ global composer trampoline).

    Writes the repo-local ``<git-dir>/hooks/post-commit`` at the resolved (worktree-correct) path
    unconditionally — it is the managed, reviewable artifact. Honors ``dev_reload.DRY_RUN_ENV``:
    when set, the machine-global composer trampoline (the one live/global mutation, needed only
    when a ``core.hooksPath`` composer shadows the repo-local hook) is skipped, mirroring
    ``RIG_TG_CTL_DRY_RUN`` / ``RIG_TMUX_DRY_RUN``.
    """
    from .. import dev_reload

    repo_root = action.source
    dplan = dev_reload.build_dev_reload(
        repo_root=repo_root,
        daemon_source_paths=action.options.get("daemon_source_paths", []),
        reload_command=action.options.get("reload_command", dev_reload.DEFAULT_RELOAD_COMMAND),
    )
    hook_target = dev_reload.post_commit_hook_path(repo_root)
    out = fsutil.write_file(hook_target, dplan.render_hook(), on_conflict)
    _chmod_x_if_changed(hook_target, out)
    notes = [f"post-commit hook {out.detail}"]
    status, backup = out.status, out.backup

    if os.environ.get(dev_reload.DRY_RUN_ENV):
        return ActionResult(action, status, "; ".join(notes), backup)

    composer_target = dev_reload.composer_post_commit_path(repo_root)
    if composer_target is not None:
        c_out = fsutil.write_file(composer_target, dev_reload.render_post_commit_composer(), on_conflict)
        _chmod_x_if_changed(composer_target, c_out)
        notes.append(f"composer {c_out.detail}")
        # Surface the composer's own outcome — it is the one machine-global mutation this
        # action makes. A backup there must not be silently dropped (per the backup-on-conflict
        # rule), and a composer-only change (hook unchanged, composer newly written/backed up)
        # must not report "skipped" and hide that a real live/global write happened.
        if c_out.backup is not None:
            backup = c_out.backup
        if c_out.status in _CHANGED_STATUSES and status == "skipped":
            status = c_out.status

    return ActionResult(action, status, "; ".join(notes), backup)


def pi_extension_policy_json(action: Action) -> str:
    """The exact JSON bytes rig writes for the pi permission policy (shared with drift).

    Canonical serialization (sorted keys, 2-space indent, trailing newline) so the install and the
    drift read-back agree byte-for-byte and a re-apply is a true no-op. Reads ``options["policy"]``
    strictly (KeyError on a malformed action, matching ``options["policy_file"]``) — the plan
    always populates both, so a missing key is a bug, not a silent empty policy.
    """
    return json.dumps(action.options["policy"], indent=2, sort_keys=True) + "\n"


def _pi_extension_backup_dir(ext_target: Path) -> Path:
    """Where a conflict-backup of the pi permission-guard extension goes — a sibling
    ``.rig-backups/`` OUTSIDE the ``extensions/`` dir pi auto-scans (``ext_target`` =
    ``<pi agent root>/extensions/<name>``) — same rationale as :func:`_skill_backup_dir`, see
    :func:`fsutil.sibling_backup_dir`."""
    return fsutil.sibling_backup_dir(ext_target)


def _do_provision_pi_extension(action: Action, on_conflict: str) -> ActionResult:
    """Install the pi ``permission-guard`` extension + write the rig-owned policy it reads.

    Two idempotent, backup-noted steps (rig-cli pi permission parity):

    1. copy the extension dir (``pi-extensions/<name>/``) → pi's ``extensions/<name>/`` so pi
       auto-discovers it. ``fsutil.copy_tree`` skips when identical, backs up on conflict — the
       backup lands OUTSIDE ``extensions/`` (:func:`_pi_extension_backup_dir`) so a stale/tampered
       extension backup is never itself re-discovered as a duplicate extension.
    2. write the policy JSON → ``~/.pi/agent/rig-permission-policy.json`` (the file the extension
       loads; a missing/broken file makes the extension fall back to its baked baseline, so the
       dangerous denies fire either way).

    pi has NO built-in permission system, so this extension IS pi's permission belt — the deny/ask
    effect the other harnesses get from their config allowlists. Fail-explicit on IO.
    """
    policy_file = Path(action.options["policy_file"])
    notes: list[str] = []
    backup: Path | None = None
    statuses: list[str] = []

    ext_out = fsutil.copy_tree(
        action.source, action.target, on_conflict, backup_dir=_pi_extension_backup_dir(action.target)
    )
    statuses.append(ext_out.status)
    notes.append(f"extension {ext_out.detail}")
    if ext_out.backup:
        backup = ext_out.backup

    policy_out = fsutil.write_file(policy_file, pi_extension_policy_json(action), on_conflict)
    statuses.append(policy_out.status)
    notes.append(f"policy {policy_out.detail}")
    # ActionResult carries ONE structured backup slot; when BOTH artifacts back up, the second
    # backup path is not lost — each WriteOutcome.detail (joined into notes above) already carries
    # its own "backed up prior → <path>", so both restore paths are in the result detail.
    if policy_out.backup and backup is None:
        backup = policy_out.backup

    # overall status precedence: backed_up > updated > created > skipped (any real change is not a
    # skip). `WriteOutcome.status` (fsutil.py) is only ever "created"|"updated"|"skipped"|
    # "backed_up" — never "error" (copy_tree/write_file raise on IO failure instead; caught by
    # run_plan's outer exception handler, which reports "error" itself). "error" is kept first in
    # this precedence tuple anyway as defense-in-depth: if a future status value is ever added
    # without updating this tuple, an unrecognized value still falls safe into the explicit
    # "skipped" default below rather than raising a raw, uninformative `StopIteration`.
    overall = next((s for s in ("error", "backed_up", "updated", "created", "skipped") if s in statuses), "skipped")
    return ActionResult(
        action, overall, f"permissions/pi-extension {action.options.get('extension')}: " + "; ".join(notes), backup
    )
