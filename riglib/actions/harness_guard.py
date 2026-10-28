"""Harness guard extension install handler."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from . import fsutil
from ..paths import expand_user_path
from ..plan import Action
from .result import ActionResult
from .shared import _should_backup


# ── omp guard extension provisioning (~/.omp/agent/extensions/rig-permissions-guard.ts) ──────
# omp AUTO-DISCOVERS TS extensions; a tool_call handler returning {block, reason} blocks a bash
# call before execution (verified live, rig-cli#202). rig owns the whole file: the content is
# GENERATED from the single rule registry (riglib.permissions) by riglib.omp_guard, written
# ATOMICALLY (temp + rename), idempotent on byte-compare, and backed up before any replace.
def guard_extension_file(action: Action) -> Path:
    """The extension file an ``install_harness_guard`` action targets (shared with drift)."""
    return action.target


def desired_guard_content(action: Action) -> str:
    """The exact TS content rig owns for this action (shared by apply + drift, byte-for-byte).

    The incompatible-marker dir is baked from the action's install dir, so the guard's
    fail-closed signal always lands where drift looks for it — including PI_* override roots.
    One computation, one public name — this is the Action-typed spelling of
    :func:`expected_guard_content`."""
    return expected_guard_content(guard_extension_file(action))


def resolve_guard_target(action: Action) -> Path | None:
    """The guard extension path for this action — from the action's option when the plan
    wired it, else derived from the registry (fail closed: a guard-bearing kind NEVER
    depends on the option wiring). Shared by the interlock and drift's correlation so the
    two can never resolve differently. ``None`` for kinds without a guard surface."""
    from ..permissions import HARNESS_GUARD, guard_extension_path_for

    if action.options.get("guard_target"):
        return Path(str(action.options["guard_target"]))
    kind = str(action.options.get("kind", ""))
    if kind not in HARNESS_GUARD:
        return None
    return expand_user_path(guard_extension_path_for(kind))


def expected_guard_content(guard_path: Path) -> str:
    """The rig-generated guard content for a given install path (marker dir baked from it)."""
    from ..omp_guard import render_guard_ts

    return render_guard_ts(marker_dir=str(guard_path.parent))


def _guard_in_place(action: Action) -> str | None:
    """None when the guard belt for this action's kind is correctly installed, else the reason.

    The approval-posture write is INTERLOCKED on this: rig never relaxes a harness to
    auto-approve without the enforcement layer verifiably in place (a guard write that
    errored, or a tampered guard left by on_conflict=skip, must not end in yolo)."""
    path = resolve_guard_target(action)
    if path is None:
        return None  # no guard surface for this kind — nothing to interlock on
    if not path.is_file():
        return f"guard extension not installed at {path}"
    try:
        current = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"cannot read guard extension {path}: {exc}"
    if current != expected_guard_content(path):
        return f"guard extension at {path} is drifted (not the rig-generated content)"
    return None


def _atomic_write(path: Path, content: str) -> None:
    """Temp-file + rename in the same dir: a crash never leaves a half-written guard.
    mkstemp (collision-free, same dir) and an explicit 0o644 — the guard is code omp loads,
    so its mode is deliberate, never umask-lottery."""
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.rig-tmp-", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        tmp.chmod(0o644)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _clear_incompatible_marker(guard_path: Path) -> bool:
    """Remove a stale fail-closed marker next to a healthy guard (apply IS the recovery path
    the drift message points at — a byte-current guard should not flag forever)."""
    from ..omp_guard import INCOMPATIBLE_MARKER_NAME

    marker = guard_path.parent / INCOMPATIBLE_MARKER_NAME
    if marker.exists():
        marker.unlink()
        return True
    return False


def _do_install_harness_guard(action: Action, on_conflict: str) -> ActionResult:
    """Write the generated guard extension — atomic, idempotent, backup-before-replace.

    The file is wholly rig-owned: a DIFFERING on-disk file (hand edit or foreign file at the
    rig path) is backed up and replaced under ``on_conflict=backup``/``overwrite``, and left
    untouched (drift stays visible) under ``skip``. A byte-identical file is a true no-op.
    """
    from ..omp_guard import GUARD_TEMPLATE_VERSION, INCOMPATIBLE_MARKER_NAME, guard_provenance

    desired = desired_guard_content(action)
    path = guard_extension_file(action)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        _atomic_write(path, desired)
        cleared = _clear_incompatible_marker(path)
        note = f"; cleared stale {INCOMPATIBLE_MARKER_NAME}" if cleared else ""
        return ActionResult(action, "created", f"guard/{action.item}: wrote {path}{note}")
    try:
        current = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return ActionResult(action, "error", f"guard/{action.item}: cannot read {path}: {exc}")
    if current == desired:
        cleared = _clear_incompatible_marker(path)
        note = f"; cleared stale {INCOMPATIBLE_MARKER_NAME}" if cleared else ""
        return ActionResult(action, "skipped", f"guard/{action.item}: already current in {path}{note}")
    if on_conflict == "skip":
        return ActionResult(
            action, "skipped",
            f"guard/{action.item}: {path} differs but on_conflict=skip — left unchanged (drift NOT reconciled)",
        )
    backup_note = ""
    if _should_backup(on_conflict):
        bak = fsutil.backup_path(path)
        shutil.copy2(str(path), str(bak))
        backup_note = f" (backed up prior → {bak})"
    prov = guard_provenance(current)
    origin = f"rig template v{prov['template']}" if prov else "foreign file"
    _atomic_write(path, desired)
    cleared = _clear_incompatible_marker(path)
    cleared_note = f"; cleared stale {INCOMPATIBLE_MARKER_NAME}" if cleared else ""
    status = "backed_up" if backup_note else "updated"
    return ActionResult(
        action, status,
        f"guard/{action.item}: regenerated {path} (was {origin}, now v{GUARD_TEMPLATE_VERSION}){backup_note}{cleared_note}",
    )
