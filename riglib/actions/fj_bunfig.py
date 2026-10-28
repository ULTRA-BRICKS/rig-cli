"""Forgejo CLI and bunfig provisioning handlers."""

from __future__ import annotations

import os
import shutil

from . import fsutil
from ..plan import Action
from .result import ActionResult


def _do_provision_fj(action: Action, on_conflict: str) -> ActionResult:
    """Provision Forgejo access (see :mod:`riglib.fj`): the ``fj`` command (the real fj — rig's
    pinned copy in its store, or Homebrew's — behind the ``fj`` wrapper whose ``fj ship`` is
    ``gh ship``), the hosts in fj's ``client_ids`` (other lines kept), ``FJ_FALLBACK_HOST`` in the
    Windows user environment, and git over HTTPS to the global host using fj's login (rig's
    ``git-credential-fj`` helper as ``credential.https://<host>.helper``).

    Driven by :func:`fj.pending_changes` — the same comparison ``rig status`` reports — so a
    current machine is a no-op. A failed step is an ``error`` naming the remedy; the other steps
    still run. ``RIG_FJ_DRY_RUN=1`` reports what WOULD change without touching anything. What only
    the user can do — log in, or swap a pasted token for the browser login — is appended as a note.
    """
    from .. import fj

    opts = action.options
    notes = "".join(f"; note: {n}" for _, n in fj.login_notes(opts))
    todo = fj.pending_changes(opts)
    if not todo:
        return ActionResult(action, "skipped", f"already current: fj + login for {', '.join(opts['hosts'])}{notes}")
    if os.environ.get("RIG_FJ_DRY_RUN") == "1":
        return ActionResult(action, "skipped", "dry-run: would " + "; ".join(d for *_, d in todo) + notes)
    done, errors = fj.apply_changes(opts, todo)
    if errors:
        return ActionResult(action, "error", "; ".join(errors + done) + notes)
    return ActionResult(action, "updated", "; ".join(done) + notes)


def _do_provision_bunfig(action: Action, on_conflict: str) -> ActionResult:
    """Splice the bun isolated-linker + globalStore managed block into ``~/.bunfig.toml``.

    Additive ``create`` (append to an existing bunfig without a managed block) bypasses
    :func:`fsutil.write_file`'s skip-on-exists rule so ``on_conflict=skip`` still appends
    the block; skip only blocks rewriting a stale managed block (``update``). ``ok`` is a
    no-op. ``conflict`` (unbalanced markers) is left untouched even when
    ``on_conflict=backup``.
    """
    from .. import bunfig

    target = action.target
    result = bunfig.resolve(target)
    if result.state == "ok":
        return ActionResult(action, "skipped", f"already current: {target}")
    if result.state == "conflict":
        return ActionResult(action, "skipped", f"conflict: {result.detail}")
    if result.state == "update" and on_conflict == "skip":
        return ActionResult(
            action,
            "skipped",
            f"stale managed block (on_conflict=skip), left untouched: {target}",
        )
    if result.state == "create":
        existed = target.exists()
        target.parent.mkdir(parents=True, exist_ok=True)
        backup = None
        if existed and on_conflict == "backup":
            backup = fsutil.backup_path(target)
            shutil.move(str(target), str(backup))
        target.write_text(result.new_content, encoding="utf-8")
        if backup is not None:
            return ActionResult(
                action,
                "backed_up",
                f"backed up prior → {backup}; appended block to {target}",
                backup=backup,
            )
        status = "updated" if existed else "created"
        verb = "appended block to" if existed else "created"
        return ActionResult(action, status, f"{verb} {target}")
    out = fsutil.write_file(target, result.new_content, on_conflict)
    return ActionResult(action, out.status, out.detail, out.backup)
