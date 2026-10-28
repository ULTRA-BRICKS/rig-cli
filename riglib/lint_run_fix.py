"""Worktree bookkeeping for `rig lint`: the `--fix` delta and the read-only guard.

Context: `lint_run.run_checks` runs a provider's `fix_argv`, then re-runs the check. Fixers rewrite
files in place, so the report must say which files moved and which of those also differ in the index
(Rig never restages; the human decides). The same snapshots (`watch`) bracket every non-fix check:
a command that was supposed to only read but changed the worktree is `wrote-files`. Trust boundary:
only reads the repository through `git` and no-follow `lstat`/`open`; it never executes repository
code and never writes.

`git status` alone cannot say that an already-dirty file changed again, so the snapshot pairs each
dirty path with a digest of its bytes.
"""
from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path

from .lint_check_scope import partially_staged, staged_paths

_RENAME_CODES = frozenset("RC")


class SnapshotError(Exception):
    """`git status` failed, so the fix delta cannot be reported honestly."""


def _digest(path: str) -> str:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "gone"
    if stat.S_ISLNK(info.st_mode):
        return "link:" + os.readlink(path)
    if not stat.S_ISREG(info.st_mode):
        return "special"
    sha = hashlib.sha1(usedforsecurity=False)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            sha.update(block)
    return sha.hexdigest()


def _status_entries(root: Path) -> dict[str, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "-z", "-uall"],
            capture_output=True, check=False, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except OSError as exc:
        raise SnapshotError(f"git status could not run: {exc}") from exc
    if proc.returncode != 0:
        raise SnapshotError(f"git status failed: {proc.stderr.decode('utf-8', 'replace').strip()[:200]}")
    tokens = [t for t in proc.stdout.split(b"\0") if t]
    entries: dict[str, str] = {}
    index = 0
    while index < len(tokens):
        code, name = tokens[index][:2].decode("ascii", "replace"), tokens[index][3:]
        index += 1
        if _RENAME_CODES & set(code):
            index += 1  # the next token is the rename source, already covered by its own status
        entries[name.decode("utf-8", "surrogateescape")] = code
    return entries


def snapshot(root: Path) -> dict[str, tuple[str, str]]:
    """rel path -> (porcelain code, content digest) for every dirty or untracked path."""
    return {rel: (code, _digest(os.path.join(root, rel))) for rel, code in _status_entries(root).items()}


def changed_files(before: dict[str, tuple[str, str]], after: dict[str, tuple[str, str]]) -> list[str]:
    return sorted(rel for rel in before.keys() | after.keys() if before.get(rel) != after.get(rel))


def watch(root: Path) -> Callable[[], list[str]]:
    """Snapshot the worktree now; the returned callable lists what changed since (read-only guard).

    Runs around every non-fix check: a project script or wrapper that formats in place (`oxfmt .`,
    `ruff format`, a Makefile recipe) cannot be recognised from its text, but it cannot hide the
    files it rewrote. Both calls raise `SnapshotError` when git cannot say, which callers treat as a
    failure rather than an unverified pass.
    """
    before = snapshot(root)
    return lambda: changed_files(before, snapshot(root))


def needs_restage(root: Path, changed: Iterable[str]) -> list[str]:
    """Changed files whose index entry differs from HEAD: the fix is not what the index holds."""
    staged = staged_paths(str(root))
    return sorted(rel for rel in changed if rel in staged)


def partially_staged_under(root: Path, cwds: Iterable[str]) -> list[str]:
    """Partially staged paths a fixer running in any of `cwds` (repo-relative) could rewrite."""
    prefixes = {"" if cwd in (".", "") else cwd.rstrip("/") + "/" for cwd in cwds}
    return sorted(
        rel for rel in partially_staged(str(root))
        if any(prefix == "" or rel.startswith(prefix) for prefix in prefixes)
    )
