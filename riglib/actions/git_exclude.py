"""``.git/info/exclude`` plumbing shared by the ship, worktrees and hook-bridge handlers."""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
from pathlib import Path


def repo_info_exclude_path(repo_root: Path) -> Path | None:
    """Resolve the repo's git exclude file (``info/exclude``), worktree-aware.

    In a plain repo this is ``<repo>/.git/info/exclude``. In a git WORKTREE, ``<repo>/.git`` is a
    FILE pointing at that worktree's private gitdir (``<common>/.git/worktrees/<name>``), so a
    naive ``<repo>/.git/info/exclude`` string join would fail there (``.git`` is a file, not a
    dir). ``info/exclude`` itself, however, is one of the files git keeps in the COMMON gitdir and
    SHARES across every worktree (unlike ``HEAD``/the index, which really are per-worktree) —
    verified empirically: `git -C <linked-worktree> rev-parse --git-path info/exclude` names the
    SAME physical file as the primary checkout's `.git/info/exclude`, not a private copy. We ask
    git for the canonical path (``git -C <repo> rev-parse --git-path info/exclude``) purely to get
    a working path from ANY worktree layout, not to isolate one worktree's ignores from another's
    — ignoring the delegator from inside a worktree affects `git status` in every worktree of the
    repo. Returns ``None`` when the path is not a git repo (git errors) so the caller treats "no
    git" as nothing-to-ignore, never a crash.
    """
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--git-path", "info/exclude"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    raw = res.stdout.strip()
    if not raw:
        return None
    p = Path(raw)
    # `--git-path` yields a path relative to the repo root (e.g. `.git/info/exclude`); anchor it.
    return p if p.is_absolute() else (repo_root / p)


def _atomic_write_exclude(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` ATOMICALLY (tmp file in the same dir + ``os.replace``).

    ``.git/info/exclude`` may hold the USER's own hand-added ignore lines. A plain truncate-rewrite
    that is interrupted (SIGINT, disk-full, OSError mid-write) would leave that content destroyed
    with no backup. Writing a sibling temp file and renaming it over the target makes the swap atomic
    on POSIX (and best-effort on Windows): either the old file or the fully-written new one is present
    — never a truncated half. ``newline=""`` writes LF verbatim (no CRLF translation), matching the
    raw read in ``_exclude_has_entry`` so a re-apply is a byte-identical no-op.
    """
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".rig-tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _find_marker_lines(content: str, marker: str) -> list[tuple[int, int]]:
    """Return ``(line_start_offset, line_end_offset)`` for each line equal to ``marker``.

    ``line_end_offset`` is the offset of the newline that terminates the line (or ``len(content)``
    for an un-terminated final line) — so a splice on ``[start, end)`` drops the marker line's text
    but not its line ending, letting the caller re-emit a clean block. A line MATCHES when its text
    with surrounding whitespace stripped equals ``marker`` (tolerant of a marker line that picked up
    trailing spaces), so such a line is still recognized and normalized on the next apply.
    """
    out: list[tuple[int, int]] = []
    pos = 0
    n = len(content)
    while pos <= n:
        nl = content.find("\n", pos)
        line_end = n if nl == -1 else nl
        if content[pos:line_end].strip() == marker:
            out.append((pos, line_end))
        if nl == -1:
            break
        pos = nl + 1
    return out


def _collapse_marker_regions(content: str, desired: str, pairs: list[tuple[int, int]]) -> str:
    """Replace the first managed region with ``desired`` and drop every later one (plus its newline)."""
    out: list[str] = []
    cursor = 0
    for idx, (r_start, r_end) in enumerate(pairs):
        out.append(content[cursor:r_start])
        if idx == 0:
            out.append(desired)
        elif content[r_end : r_end + 1] == "\n":
            r_end += 1
        cursor = r_end
    out.append(content[cursor:])
    return "".join(out)


def _pair_marker_regions(
    begins: list[tuple[int, int]], ends: list[tuple[int, int]]
) -> list[tuple[int, int]] | None:
    """Pair begin/end marker lines into ``(start, end)`` regions; ``None`` when misordered."""
    markers = sorted([(b[0], b[1], "begin") for b in begins] + [(e[0], e[1], "end") for e in ends])
    pairs: list[tuple[int, int]] = []
    expect = "begin"
    pending = -1
    for start, line_end, kind in markers:
        if kind != expect:
            return None
        if kind == "begin":
            pending = start
            expect = "end"
        else:
            pairs.append((pending, line_end))
            expect = "begin"
    return pairs


def _append_block(content: str, desired: str) -> str:
    """Append ``desired`` after the existing content verbatim, with exactly one separating newline."""
    if not content:
        return f"{desired}\n"
    lead = content if content.endswith("\n") else content + "\n"
    return f"{lead}{desired}\n"


def _create_exclude_file(exclude_path: Path, desired: str) -> tuple[bool, str]:
    try:
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_exclude(exclude_path, desired + "\n")
    except OSError as exc:
        return False, f"could not write {exclude_path}: {exc}"
    return True, "written"


def reconcile_exclude_block(
    exclude_path: Path, desired: str, begin_marker: str, end_marker: str, label: str
) -> tuple[bool, str]:
    """Splice rig's marker-delimited ``desired`` block into ``exclude_path``.

    Collapses duplicate managed regions and preserves every other line verbatim. Returns
    ``(False, message)`` on failure (unreadable/unwritable file, unbalanced or misordered markers —
    ``label`` names the marker family in those messages), else ``(True, "written" | "already")``.
    """
    # is_file (not exists): a directory at info/exclude must NOT take the fresh-write branch (it
    # would IsADirectoryError on the read below with a misleading message) — let the open() below
    # raise and surface as a clear "could not read".
    if not exclude_path.is_file() and not exclude_path.exists():
        return _create_exclude_file(exclude_path, desired)
    try:
        with exclude_path.open(encoding="utf-8", newline="") as fh:
            content = fh.read()
    except OSError as exc:
        return False, f"could not read {exclude_path}: {exc}"
    begins = _find_marker_lines(content, begin_marker)
    ends = _find_marker_lines(content, end_marker)
    if len(begins) != len(ends):
        return False, f"{exclude_path} has unbalanced rig {label} markers — reconcile by hand"
    if not begins:
        new_content = _append_block(content, desired)
    else:
        pairs = _pair_marker_regions(begins, ends)
        if pairs is None:
            return False, f"{exclude_path} has misordered rig {label} markers — reconcile by hand"
        if len(pairs) == 1 and content[pairs[0][0] : pairs[0][1]] == desired:
            return True, "already"
        new_content = _collapse_marker_regions(content, desired, pairs)
    try:
        _atomic_write_exclude(exclude_path, new_content)
    except OSError as exc:
        return False, f"could not write {exclude_path}: {exc}"
    return True, "written"
