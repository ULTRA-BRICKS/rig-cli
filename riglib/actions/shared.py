"""Small helpers used by several handler areas: backup predicate, executable bit, nested-key access and clone."""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import fsutil


_CHANGED_STATUSES = frozenset({"created", "updated", "backed_up"})


def _chmod_x_if_changed(path: Path, outcome: fsutil.WriteOutcome) -> None:
    """Ensure the exec bit on a managed script, without violating ``on_conflict=skip``.

    chmod when rig wrote the file (created/updated/backed_up) OR when the content is already
    identical (the file IS the managed one — restoring a lost exec bit is correct
    convergence). Do NOT chmod a *conflict*-skip: there the existing file is a DIFFERENT
    user file left untouched by policy, so touching its mode would violate skip.
    """
    if not path.is_file():
        return
    is_change = outcome.status in _CHANGED_STATUSES
    is_identical = outcome.status == "skipped" and "identical" in outcome.detail
    if is_change or is_identical:
        path.chmod(path.stat().st_mode | 0o111)


def _set_nested(data: dict, key_path: tuple[str, ...], value: object) -> None:
    """Set ``key_path`` to ``value``; a STRING parent of a ``*`` key is opencode's scalar shorthand
    and is expanded to ``{"*": <old>}`` first (the probe already vetted every other shape)."""
    cur = data
    for seg in key_path[:-1]:
        nxt = cur.get(seg)
        if isinstance(nxt, str) and key_path[-1] == "*":
            nxt = {"*": nxt}
            cur[seg] = nxt
        elif not isinstance(nxt, dict):
            nxt = {}
            cur[seg] = nxt
        cur = nxt
    cur[key_path[-1]] = value


def approval_lookup(data: dict, key_path: tuple[str, ...]) -> tuple[str, object]:
    """READ-ONLY probe of one managed key. Returns ``("missing"|"present"|"shape", value)`` —
    ``shape`` when an intermediate parent exists as a non-dict (value is the offending segment).
    Never mutates ``data`` (the check phase must not create empty parents it may never fill)."""
    cur: object = data
    for seg in key_path[:-1]:
        if not isinstance(cur, dict):
            return "shape", seg
        cur = cur.get(seg)
        if cur is None:
            return "missing", None  # explicit null parent == no parent (the write creates it)
        if not isinstance(cur, dict):
            return "shape", seg
    if not isinstance(cur, dict):
        return "missing", None
    return (("present", cur[key_path[-1]]) if key_path[-1] in cur else ("missing", None))


def _should_backup(on_conflict: str) -> bool:
    """One predicate for "back up settings.json before mutating it", applied uniformly."""
    return on_conflict == "backup"


def _timestamped_backup_path(base: Path) -> Path:
    """Turn a fixed backup path (``<conf>.rig-bak``) into a UNIQUE timestamped one
    (``<conf>.rig-bak-<UTC>``). A fixed name was written only on the FIRST migration
    (guarded by ``not exists()``), so a later apply — after the user hand-edited the conf —
    would neutralize those edits WITHOUT a backup. A timestamped name never collides, so every
    migrating apply keeps its own restore point. Microsecond precision avoids a same-second
    collision. (CTO 2026-06-16)
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    path = base.with_name(f"{base.name}-{stamp}")
    n = 1
    while path.exists():  # paranoia: two applies inside the same microsecond
        path = base.with_name(f"{base.name}-{stamp}-{n}")
        n += 1
    return path


def _git_clone(repo: str, dest: Path) -> int:
    """Shallow-clone ``repo`` to ``dest``. Returns 0 on success, non-zero on any failure (so the
    caller treats an offline/no-git machine as 'plugin not installed', never an apply abort)."""
    if not shutil.which("git"):
        return 127
    try:
        res = subprocess.run(
            ["git", "clone", "--depth", "1", repo, str(dest)],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return 1
    return res.returncode
