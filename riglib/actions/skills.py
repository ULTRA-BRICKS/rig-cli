"""Skill install handlers: copy a skill tree and link it into harness skill dirs."""

from __future__ import annotations

from pathlib import Path

from . import fsutil
from ..plan import Action
from .result import ActionResult


def _skill_backup_dir(skill_target: Path) -> Path:
    """Where a conflict-backup of an installed skill goes — a sibling ``.rig-backups/`` OUTSIDE
    the scanned skills dir (``skill_target`` = ``<skills_target>/<name>``, e.g.
    ``~/.agents/skills/naming``) — see :func:`fsutil.sibling_backup_dir` for why."""
    return fsutil.sibling_backup_dir(skill_target)


def _do_copy_skill(action: Action, on_conflict: str) -> ActionResult:
    out = fsutil.copy_tree(
        action.source, action.target, on_conflict, backup_dir=_skill_backup_dir(action.target)
    )
    return ActionResult(action, out.status, out.detail, out.backup)


def skill_harness_link_target(action: Action) -> tuple[Path, Path]:
    """The (symlink_path, desired_destination) a ``link_skill_harness`` action maintains.

    The symlink lives at ``action.target`` inside the harness skill dir; it should resolve to
    ``action.source`` (the installed skill in skills_target). Shared with drift so the install
    and the drift check agree on what "correct" means. The destination is the absolute
    installed-skill path — an absolute target keeps the link valid regardless of the relative
    distance between the two dirs.
    """
    return action.target, action.source.resolve()


def _do_link_skill_harness(action: Action, on_conflict: str) -> ActionResult:
    """Maintain an idempotent symlink making an installed skill discoverable by the harness.

    The agent harness lists/loads Skill-tool skills from its own dir (claude-code:
    ``~/.claude/skills``), NOT from ``skills_target`` (``~/.agents/skills``). So for every
    enabled skill rig symlinks ``<harness_skill_dir>/<skill> -> <skills_target>/<skill>``.

    - already a symlink to the correct destination → no-op (``skipped``).
    - a symlink to a WRONG destination → re-point it (``updated``); never honors on_conflict
      backup for a stale symlink (a symlink carries no user data to preserve).
    - a real (non-symlink) directory/file already there → LEAVE IT (``skipped`` with a
      warning). Some harness skills are real dirs (h-reason, debate-swarm,
      moshi-best-practices); clobbering them would destroy real content. on_conflict=overwrite
      does NOT override this — a real dir at the harness path is never rig's to replace.
    """
    link_path, dest = skill_harness_link_target(action)
    if not dest.exists():
        # the installed skill the link should point at isn't on disk — the copy_skill action
        # surfaces that failure; don't create a dangling link on top of it.
        return ActionResult(action, "error", f"skill-link/{action.item}: install target missing: {dest}")
    if _link_targets_itself(link_path, dest):
        # link would point the installed skill at itself — never clobber the real dir/file
        # with a self-referential symlink (see _link_targets_itself).
        return ActionResult(
            action, "skipped",
            f"skill-link/{action.item}: source == target ({dest}), skipping to avoid self-symlink",
        )
    link_path.parent.mkdir(parents=True, exist_ok=True)

    if link_path.is_symlink():
        try:
            current = link_path.readlink()
        except OSError as exc:
            return ActionResult(action, "error", f"skill-link/{action.item}: cannot read symlink {link_path}: {exc}")
        if _same_link_dest(link_path, current, dest):
            return ActionResult(action, "skipped", f"skill-link/{action.item}: already linked → {dest}")
        # stale/wrong symlink — re-point it (a symlink holds no user data; no backup needed).
        link_path.unlink()
        link_path.symlink_to(dest)
        return ActionResult(action, "updated", f"skill-link/{action.item}: re-pointed → {dest}")

    if link_path.exists():
        # a REAL dir/file already occupies the harness path (not a rig symlink). Never clobber
        # it — it may be a hand-authored skill (h-reason, debate-swarm). Leave it, warn.
        kind = "directory" if link_path.is_dir() else "file"
        return ActionResult(
            action, "skipped",
            f"skill-link/{action.item}: a real {kind} already exists at {link_path} "
            f"(not a rig symlink) — left untouched; skill may shadow the installed one",
        )

    link_path.symlink_to(dest)
    return ActionResult(action, "created", f"skill-link/{action.item}: linked {link_path} → {dest}")


def _link_targets_itself(link_path: Path, dest: Path) -> bool:
    """True when a symlink ``link_path -> dest`` would point the file at its OWN location.

    Normalizes both to an absolute path with the PARENT resolved (collapsing any symlinked
    ancestor dirs) but the leaf name kept verbatim — we must NOT ``resolve()`` ``link_path``
    itself: it may already BE the self-referential symlink we are guarding against (whose
    ``resolve()`` raises ``OSError: Too many levels of symbolic links``). When both normalize
    to the same path, creating the link would replace the real source file with a symlink to
    itself — the 2026-07-15 opencode-bridge corruption. Callers must skip in that case.
    """
    try:
        a = link_path.parent.resolve(strict=False) / link_path.name
        b = dest.parent.resolve(strict=False) / dest.name
    except OSError:
        # cannot prove the link is NOT self-referential (e.g. an ancestor symlink loop or a
        # permissions error on resolve) — fail SAFE: treat it as self and let the caller skip,
        # never fall through to the clobber-and-symlink path on an unresolvable parent.
        return True
    return a == b


def _same_link_dest(link_path: Path, current: Path, dest: Path) -> bool:
    """True when ``current`` (the symlink's stored target) resolves to the desired ``dest``.

    Handles both an absolute stored target and a relative one (resolved against the link's
    parent), so a correct link written either way is recognized as a no-op.
    """
    if current.is_absolute():
        resolved = current
    else:
        resolved = (link_path.parent / current).resolve()
    try:
        return resolved.resolve() == dest.resolve()
    except OSError:
        return False
