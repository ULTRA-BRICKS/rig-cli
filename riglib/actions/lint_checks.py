"""Apply handlers for the ``lint_checks`` activation (analyzer install, pre-commit hook, CI workflow).

Registered in :data:`riglib.actions.runner._HANDLERS`. All three are idempotent (a converged
machine/repo is ``skipped``), honour ``on_conflict`` for files rig did not write, and never execute
the analyzer. ``ActionResult`` and the safe-path classifier live in ``runner`` (which imports this
module at load), so they are imported inside the handlers.

The analyzer is written under whatever ``HOME``/``XDG_DATA_HOME`` rig runs with, sandboxed or not:
``rig status`` and ``rig lint check`` look for it in that same place, so skipping the install on an
alternate HOME would leave the gate permanently unsatisfiable. Only an explicit
``RIG_CCCC_DRY_RUN=1`` (tests, offline previews) reports what WOULD be installed without downloading.

The machine-wide pre-commit hook is ordered AFTER the analyzer and written only while the analyzer
is usable (verified): a failed install must never leave a newly enabled global hook that blocks every
commit on the machine with exit 127. A hook that cannot be written is a loud ``error`` (so
``rig apply commit`` exits non-zero), never a silent pass.
"""

from __future__ import annotations

from pathlib import Path

from .. import cccc_install
from .. import lint_checks_artifacts as arts
from . import fsutil


def _dry_run() -> bool:
    from .runner import _env_flag

    return _env_flag("RIG_CCCC_DRY_RUN")


def _set_aside_mismatch(dest: Path, on_conflict: str) -> tuple[str, Path | None]:
    """Honour ``on_conflict`` for a wrong file at the managed path: ``skip`` | ``backup`` | ``overwrite``.

    Works on the path itself (``lstat``), never on a symlink's target: a link is moved/removed as a
    link and the file it points at (possibly the user's own ``cccc``) is left exactly as it was.
    """
    if not dest.exists() and not dest.is_symlink():
        return "go", None
    if on_conflict == "skip":
        return "skip", None
    if on_conflict == "backup":
        bak = fsutil.backup_path(dest)
        dest.rename(bak)
        # a backed-up regular file must not stay an executable "cccc". `_strip_exec_bits` acts on the path itself
        # (lstat): a symlink (possibly to the user's own cccc) is never chmod'ed through, a directory is left
        # traversable, and a failing chmod is swallowed — it never widens a mode.
        fsutil._strip_exec_bits(bak)
        return "go", bak
    fsutil._remove_any(dest)
    return "go", None


def do_install_cccc(action, on_conflict: str):
    from .runner import ActionResult

    state = cccc_install.inspect()
    if state.state == "ok":
        return ActionResult(action, "skipped", f"cccc v{cccc_install.PINNED_VERSION} already installed and verified")
    if state.state == "unsupported":
        return ActionResult(action, "error", f"BLOCKED: {state.detail}")
    if _dry_run():
        return ActionResult(action, "skipped", f"dry-run: would install cccc v{cccc_install.PINNED_VERSION} ({state.state})")
    backup = None
    try:
        if state.state == "mismatch":
            verdict, backup = _set_aside_mismatch(state.path, on_conflict)
            if verdict == "skip":
                return ActionResult(
                    action, "skipped",
                    f"{state.path} does not match the pin; left untouched (on_conflict=skip) — `rig lint check` fails closed until it is replaced",
                )
        result = cccc_install.install()
    except (cccc_install.CcccInstallError, OSError) as exc:  # a read-only/full disk is a loud error, not a traceback
        kept = f"; the mismatching file was moved to {backup} (restore it from there if needed)" if backup else ""
        return ActionResult(action, "error", f"cccc install failed (fail closed, nothing trusted): {exc}{kept}", backup)
    return ActionResult(action, "backed_up" if backup else "created", result.detail, backup)


def _hook_blocker(action) -> tuple[str, str] | None:
    """``(status, detail)`` when the hook must NOT be written because the analyzer it needs is unusable."""
    if not action.options.get("needs_analyzer"):
        return None
    state = cccc_install.inspect()
    if state.state == "ok":
        return None
    if _dry_run():
        return "skipped", f"dry-run: hook not written until cccc v{cccc_install.PINNED_VERSION} is installed ({state.state})"
    return "error", (
        f"lint-check hook NOT written: the cccc analyzer is unusable ({state.detail}); a machine-wide hook "
        "without it would block every commit with exit 127. Fix the install error above and re-run `rig apply commit`"
    )


def do_install_hook(action, on_conflict: str):
    from .runner import ActionResult, _chmod_x_if_changed

    blocked = _hook_blocker(action)
    if blocked is not None:
        return ActionResult(action, blocked[0], blocked[1])
    content = arts.render_hook_fragment(str(action.options["rig_bin"]))
    out = fsutil.write_file(action.target, content, on_conflict)
    _chmod_x_if_changed(action.target, out)
    return ActionResult(action, out.status, f"lint-check hook {out.detail}", out.backup)


def do_install_workflow(action, on_conflict: str):
    from .runner import ActionResult, resolve_linter_config

    rel_path = str(action.options["rel_path"])
    content = arts.render_workflow(str(action.options["rig_source"]), bool(action.options["with_analyzer"]))
    r = resolve_linter_config(action.target, rel_path, content)
    if r.state == "io_error":
        return ActionResult(action, "error", f"lint-check workflow: {r.detail}")
    if r.state == "ok":
        return ActionResult(action, "skipped", f"lint-check workflow: {rel_path} already correct")
    out = fsutil.write_file(r.target_path, r.content, on_conflict)
    return ActionResult(action, out.status, f"lint-check workflow: {out.detail}", out.backup)
