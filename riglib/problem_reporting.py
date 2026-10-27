"""Canonical problem-reporting instructions and their non-destructive reconciliation.

This policy belongs to rig, not to a developer's home AGENTS.md or agent-tools checkout.
The planner sends it to each configured harness's global instruction file. Apply and drift
use the same resolver, so a missing, stale, or malformed policy cannot appear up to date.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .actions.runner import ActionResult
    from .plan import Action


BEGIN_MARKER = "# >>> rig-managed problem reporting (do not edit) >>>"
END_MARKER = "# <<< rig-managed problem reporting (do not edit) <<<"


def block_text() -> str:
    """The complete requirement, installed even without permissions or a repo guide."""
    return "\n".join([
        BEGIN_MARKER,
        "## Problem and blocker reports",
        "Every problem or blocker report MUST explain all three:",
        "1. What I already did: the specific actions already attempted and their observed results.",
        "   Include concrete evidence such as command output, an error, test result, or task/PR link.",
        "2. What I am doing now: the independent action already started to solve the problem.",
        "   Start the next safe, authorized investigation or fix before reporting; do not merely promise it.",
        "   If no safe, authorized action can start, say so and give the exact reason instead of inventing activity.",
        "3. What happens next: the concrete next step and next verification point.",
        "   Name the verification command/check and its time or event trigger.",
        "   If truly blocked, name the exact dependency, why it prevents the next action, and what",
        "   result, access, decision, or event will unblock it. Continue any independent work.",
        'Merely naming a problem, saying "under observation", or asking the user to take over is insufficient.',
        "A task ID, assigned helper, or promise alone is not remediation; report what actually ran and its results.",
        "Distinguish attempted, running, and verified work; never invent actions or results.",
        END_MARKER,
    ])


def _chain_holds_only_preparation(root: Path, target: Path) -> bool:
    """Each level down to ``target`` holds only the path to it, and the last only our artifacts.

    Every directory is listed once: an empty level (nothing but a just-created directory) is
    decisive on that snapshot, so a writer racing the scan cannot change the verdict.
    """
    cursor = root
    for component in target.relative_to(root).parts[:-1]:
        entries = list(cursor.iterdir())
        if not entries:
            return True
        if entries != [cursor / component]:
            return False
        cursor = cursor / component
    return all(_is_preparation_artifact(artifact, target) for artifact in cursor.iterdir())


def _is_preparation_artifact(artifact: Path, target: Path) -> bool:
    """The target, one of its temp/backup siblings, holding only a complete (or in-flight) block."""
    is_temporary = artifact.name.startswith(target.name + ".") and artifact.name.endswith(".rig-tmp")
    is_backup = artifact.name.startswith(target.name + ".rig-bak-")
    if artifact != target and not is_temporary and not is_backup:
        return False
    content = artifact.read_text(encoding="utf-8").strip()
    complete = (
        content.startswith(BEGIN_MARKER) and content.endswith(END_MARKER)
        and content.count(BEGIN_MARKER) == content.count(END_MARKER) == 1
    )
    # A concurrent activation may observe the writer's own empty/partial temp file.
    partial = is_temporary and (BEGIN_MARKER.startswith(content) or content.startswith(BEGIN_MARKER))
    return complete or partial


def only_reporting_policy(kind: str, config_home: str) -> bool:
    """Whether a config directory contains only preparation artifacts, not a client.

    A policy-only activation must not make the next activation infer an installed harness
    from the directory it just created and consequently enable that harness's auto mode.
    Empty directories also arise before temporary-file creation or after a failed write.
    Native artifact paths still come from the shared harness registry.
    """
    from .harness_skills import global_instruction_targets
    from .paths import expand_user_path

    root = expand_user_path(config_home)
    try:
        if not any(root.iterdir()):
            return True
    except OSError:
        return False
    targets = [
        path for _, path in global_instruction_targets(kind, {}, Path.home())
        if path.is_relative_to(root)
    ]
    if len(targets) != 1:
        return False
    try:
        return _chain_holds_only_preparation(root, targets[0])
    except (OSError, UnicodeError, RuntimeError):
        return False


@dataclass(frozen=True)
class Resolution:
    state: str  # create_file | create | update | ok | conflict
    content: str = ""
    detail: str = ""
    original: bytes | None = None
    destination: Path | None = None


def resolve(path: Path) -> Resolution:
    """Read-only convergence decision; all bytes outside managed regions are retained."""
    from .actions.runner import _splice_managed_block

    desired = block_text()
    try:
        destination = path.resolve()  # surface a loop even where Path.exists() reports False
        if path.is_symlink() and not path.exists():
            return Resolution("conflict", detail=f"dangling instruction-file symlink at {path}; restore its target before applying")
        if not path.exists():
            return Resolution("create_file", desired + "\n", "global instruction file not written", destination=destination)
        with path.open(encoding="utf-8", newline="") as stream:
            content = stream.read()
        links = path.stat().st_nlink
    except (OSError, UnicodeError, RuntimeError) as exc:
        return Resolution("conflict", detail=f"cannot read {path}: {exc}")
    updated, state, detail = _splice_managed_block(content, desired, BEGIN_MARKER, END_MARKER)
    if links > 1 and state not in {"ok", "conflict"}:
        return Resolution("conflict", detail=f"{path} has multiple hard links; use one real file with symlink aliases before applying")
    if state == "create":
        # The shared splicer normalizes trailing blank lines when appending. Global instruction
        # files may contain user-owned CRLF/spacing: retain their complete prefix byte-for-byte.
        separator = "" if not content or content.endswith("\n\n") else ("\n" if content.endswith("\n") else "\n\n")
        updated = content + separator + desired + "\n"
        detail = "no rig-managed problem reporting block (apply adds it)"
    elif state == "update":
        detail = "rig-managed problem reporting block differs (apply reconciles it)"
    return Resolution(state, updated, detail, content.encode("utf-8"), destination)


def _default_file_mode() -> int:
    """The mode a plain ``open()`` would give a new file (tempfile always creates 0600)."""
    umask = os.umask(0)
    os.umask(umask)
    return 0o666 & ~umask


def _write_atomically(path: Path, result: Resolution) -> None:
    """Write first, then check for intervening edits before replacing the real file."""
    destination = result.destination
    if destination is None:
        raise ValueError("instruction update has no resolved destination")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=destination.parent,
            prefix=destination.name + ".", suffix=".rig-tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(result.content)
            stream.flush()
            os.fsync(stream.fileno())
        if path.resolve() != destination:
            raise OSError(f"{path} changed during reconciliation; re-run apply to inspect the new target")
        try:
            current = destination.read_bytes()
        except FileNotFoundError:
            current = None
        if current != result.original:
            raise OSError(f"{path} changed during reconciliation; re-run apply to preserve the new content")
        if current is None:
            temporary.chmod(_default_file_mode())
        else:
            current_stat = destination.stat()
            if current_stat.st_nlink > 1:
                raise OSError(f"{path} acquired another hard link during reconciliation; left unchanged")
            temporary.chmod(stat.S_IMODE(current_stat.st_mode))
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink()


def apply(action: Action, on_conflict: str) -> ActionResult:
    """Create, append, or update only our block; preserve aliases and back up stale content."""
    from .actions.runner import ActionResult
    from .instruction_files import instruction_file_lock

    try:
        with instruction_file_lock(action.target):
            return _apply_locked(action, on_conflict)
    except (OSError, RuntimeError) as exc:
        return ActionResult(action, "error", f"instructions/{action.item}: {exc}")


def _apply_locked(action: Action, on_conflict: str) -> ActionResult:
    """The complete read/reconcile/write transaction, under the shared target lock."""
    from .actions import fsutil
    from .actions.runner import ActionResult

    path = action.target
    result = resolve(path)
    label = f"instructions/{action.item}"
    if result.state == "conflict":
        return ActionResult(action, "error", f"{label}: {result.detail}")
    if result.state == "ok":
        return ActionResult(action, "skipped", f"{label}: already current in {path}")
    if result.state == "update" and on_conflict == "skip":
        return ActionResult(action, "skipped", f"{label}: {path} differs but on_conflict=skip; drift remains")
    backup = None
    if result.state == "update" and on_conflict == "backup":
        backup = fsutil.backup_path(path)
        shutil.copy2(path, backup)
    try:
        _write_atomically(path, result)
    except (OSError, RuntimeError) as exc:
        suffix = f"; prior backup at {backup}" if backup else ""
        return ActionResult(action, "error", f"{label}: {exc}{suffix}", backup)
    status = "backed_up" if backup else ("created" if result.state == "create_file" else "updated")
    suffix = f"; backed up prior to {backup}" if backup else ""
    return ActionResult(action, status, f"{label}: reconciled {path}{suffix}", backup)
