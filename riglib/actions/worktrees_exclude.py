"""``.git/info/exclude`` entry for the worktrees directory."""

from __future__ import annotations

from pathlib import Path

from ..config import (
    WORKTREES_DIR_NAME,
    WORKTREES_EXCLUDE_BEGIN_MARKER,
    WORKTREES_EXCLUDE_COMMENT,
    WORKTREES_EXCLUDE_END_MARKER,
)
from .git_exclude import reconcile_exclude_block, repo_info_exclude_path


# ── `rig worktree create` — per-repo ``.worktrees/`` git-exclude registration ───────
# `rig worktree create` (riglib/worktree.py) standardizes WHERE a new agent worktree lands
# (`<repo>/.worktrees/<name>`) and, immediately after `git worktree add` succeeds, calls
# `reconcile_worktrees_exclude` below so the new directory never shows up as an untracked file in
# `git status` for the primary checkout. Lives here (not in worktree.py) because it reuses the same
# marker-splice primitives (`_find_marker_lines`, `_atomic_write_exclude`) and the same worktree-
# aware `repo_info_exclude_path` resolver as the ship-delegator/opencode-bridge excludes above —
# one home for every "register a rig-owned entry in .git/info/exclude" reconciler.
def worktrees_exclude_block_text() -> str:
    """The exact marker-delimited block rig owns for the ``.worktrees/`` entry."""
    return "\n".join(
        [
            WORKTREES_EXCLUDE_BEGIN_MARKER,
            WORKTREES_EXCLUDE_COMMENT,
            f"/{WORKTREES_DIR_NAME}/",
            WORKTREES_EXCLUDE_END_MARKER,
        ]
    )


def reconcile_worktrees_exclude(repo_root: Path) -> tuple[bool, str]:
    """Idempotently ensure ``.worktrees/`` is ignored in ``repo_root``'s ``.git/info/exclude``.

    Returns ``(ok, note)``. Mirrors :func:`_reconcile_ship_exclude` byte-for-byte (collapse a
    prior non-idempotent write back to one block, refuse an unbalanced/misordered marker pair
    rather than guess, preserve every other line verbatim) — the same contract every managed
    per-repo exclude entry in this file honors. ``exclude_path`` comes from
    :func:`repo_info_exclude_path`, so this resolves correctly when called from a freshly created
    linked worktree too — ``info/exclude`` lives in the repo's COMMON gitdir and is SHARED by
    every worktree (verified: `git -C <linked-worktree> rev-parse --git-path info/exclude`
    names the primary checkout's `.git/info/exclude`, not a private per-worktree copy), so one
    reconcile call keeps `git status` clean in every worktree of the repo, not only the one being
    created. ``repo_info_exclude_path`` is still needed rather than a naive ``<repo>/.git/info/
    exclude`` string join, because ``.git`` is a FILE (not a directory) inside a linked worktree.
    A repo with no git (``exclude_path is None``) is a no-op — ``ok=True``, nothing to reconcile.
    ``ok=False`` means the ignore could NOT be established (unreadable/unwritable exclude, or a
    marker conflict) — the caller must surface this as a real problem, not a misleading success,
    since an un-ignored ``.worktrees/`` dirties `git status` the moment a worktree is created
    under it.
    """
    exclude_path = repo_info_exclude_path(repo_root)
    if exclude_path is None:
        return True, "no git repo — .worktrees/ not registered (nothing to reconcile)"
    ok, outcome = reconcile_exclude_block(
        exclude_path,
        worktrees_exclude_block_text(),
        WORKTREES_EXCLUDE_BEGIN_MARKER,
        WORKTREES_EXCLUDE_END_MARKER,
        "worktrees",
    )
    if not ok:
        return False, outcome
    if outcome == "already":
        return True, f"already registered in {exclude_path}"
    return True, f"registered .worktrees/ in {exclude_path}"
