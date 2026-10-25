"""Content-landed analysis — "is what this branch changed already on the default branch?"

A squash merge (or a rebase that rewrote the commits, or a branch re-opened as a new PR under another
name) leaves NO trace linking the original commits to the default branch: the tip is not an ancestor,
``git cherry`` finds no patch-id twin, and the forge's PR may carry a different head branch. What does
survive is the CONTENT. This module answers, file by file, whether the work the branch added is
already on the default branch — and, when it is not, says exactly what is missing and whether the
default branch has moved on past it ("superseded") or never had it ("unique").

It is the source of the "what I found" text of every denial (:mod:`riglib.removal_explain`) and the
comparison :mod:`riglib.removal_squash` builds on. It explains; it never proves a branch landed on its own.

For every file that changed between ``base = merge-base(default, tip)`` and ``tip`` (renames off — a
rename is a delete plus an add):

- ``identical`` — the file on the default branch is byte-for-byte the branch's version (or both are
  gone);
- ``landed`` — both versions are text and every CHANGE the branch made is visible on the default
  branch: each block of lines the branch added appears there contiguously, and no block of lines the
  branch removed is still there. Blank lines never count; a block whose non-blank text is shorter than
  :data:`TRIVIAL_CHARS` (``}``, ``pass``, ``else:``) matches far too easily elsewhere, so it must also
  match together with the nearest non-blank line above AND below it;
- ``superseded`` — NOT proven, but the default branch also changed this file since the fork (or deleted
  it): the branch's version of the missing lines may simply be older than newer work on the default
  branch. Probably obsolete; a human has to say so;
- ``unique`` — NOT proven, and the default branch never touched the file (or lacks it altogether): the
  branch's change exists nowhere else.

Only ``identical`` and ``landed`` files count as proof, and only when EVERY file is one of the two. A
binary file that differs, a file too large to compare, a symlink/submodule that differs and a mode
change the default branch lacks are never proven. Fail CLOSED: any git failure raises
:class:`ContentError` and the caller reports "could not decide".

Known limit (deliberate): the check is line-block presence, not a semantic merge. A block that happens
to appear verbatim somewhere else in the file on the default branch is accepted as landed; the
:data:`TRIVIAL_CHARS` rule and :data:`WINDOW` chunking keep that to text long enough to be distinctive.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable

# `run(*git_args)` — `git -C <repo> <args>`; supplied by removal_verdict (it owns the git plumbing).
GitRun = Callable[..., "subprocess.CompletedProcess[bytes]"]

MAX_FILES = 5000
MAX_BLOB_BYTES = 2_000_000
TRIVIAL_CHARS = 20  # a block with fewer non-blank characters needs a neighbouring line to match too
WINDOW = 8  # a long block is checked in windows of this many lines (a line added inside it breaks one window)
SHOWN_COMMITS = 8
SHOWN_LINES = 3  # missing lines quoted per file
_LINE_CAP = 110
_PATH_CHUNK = 100
_BINARY_PROBE = 8000
_ZERO = "0" * 40
_GITLINK, _SYMLINK = "160000", "120000"

IDENTICAL = "identical"
LANDED = "landed"
SUPERSEDED = "superseded"
UNIQUE = "unique"
_PROVEN = frozenset({IDENTICAL, LANDED})

Entry = tuple[str, str]  # (mode, oid)


class ContentError(Exception):
    """The comparison could not be completed (git failed, limit exceeded). Never turned into a yes."""


@dataclass(frozen=True)
class FileFinding:
    path: str
    status: str
    note: str = ""  # why it is not proven ("binary file differs", "main deleted this file after the fork", …)
    missing: tuple[str, ...] = ()  # lines the branch has that the default branch lacks (+) / still has (-)


@dataclass(frozen=True)
class CommitLine:
    sha: str
    subject: str


@dataclass(frozen=True)
class Evidence:
    """Everything a human needs to judge an unproven branch: its commits, the file-by-file comparison
    with the default branch, and (set by the verdict, which owns the forge) the PRs that look like its
    successor."""

    default_ref: str
    base: str
    commits: tuple[CommitLine, ...]  # at most SHOWN_COMMITS, newest first
    commit_total: int
    files: tuple[FileFinding, ...]
    related: tuple[str, ...] = ()

    @property
    def proven(self) -> bool:
        return all(f.status in _PROVEN for f in self.files)

    def count(self, status: str) -> int:
        return sum(1 for f in self.files if f.status == status)

    @property
    def classification(self) -> str:
        """``landed`` (proven), ``unique-work`` (some file exists only on the branch) or ``superseded``
        (everything unproven sits in files the default branch has since changed)."""
        if self.proven:
            return LANDED
        return "unique-work" if self.count(UNIQUE) else SUPERSEDED

    def to_dict(self) -> dict:
        return {
            "default_ref": self.default_ref,
            "base": self.base,
            "classification": self.classification,
            "commit_total": self.commit_total,
            "commits": [{"sha": c.sha, "subject": c.subject} for c in self.commits],
            "files": [
                {"path": f.path, "status": f.status, "note": f.note, "missing": list(f.missing)} for f in self.files
            ],
            "related": list(self.related),
        }


def _stdout(res: subprocess.CompletedProcess[bytes]) -> bytes:
    return res.stdout


def _checked(res: subprocess.CompletedProcess[bytes], what: str) -> bytes:
    if res.returncode != 0:
        detail = (res.stderr or res.stdout).decode("utf-8", "replace").strip()[:200] or f"exit {res.returncode}"
        raise ContentError(f"`git {what}` failed: {detail}")
    return _stdout(res)


def _diff_entries(run: GitRun, base: str, tip: str) -> dict[str, tuple[Entry | None, Entry | None]]:
    """``{path: (base entry, tip entry)}`` for every path that differs; ``None`` = absent."""
    raw = _checked(run("diff", "--raw", "-z", "--no-renames", "--no-abbrev", "--no-ext-diff", base, tip), "diff --raw")
    tokens = raw.split(b"\0")
    entries: dict[str, tuple[Entry | None, Entry | None]] = {}
    i = 0
    while i < len(tokens) and tokens[i]:
        header = tokens[i].decode("ascii", "replace")
        if not header.startswith(":") or i + 1 >= len(tokens):
            raise ContentError("unexpected `git diff --raw` output")
        old_mode, new_mode, old_oid, new_oid, _status = header[1:].split(" ")[:5]
        path = tokens[i + 1].decode("utf-8", "surrogateescape")
        old = None if old_oid == _ZERO else (old_mode, old_oid)
        new = None if new_oid == _ZERO else (new_mode, new_oid)
        entries[path] = (old, new)
        i += 2
    if len(entries) > MAX_FILES:
        raise ContentError(f"the branch changes {len(entries)} files; the content comparison stops at {MAX_FILES}")
    return entries


def _tree_entries(run: GitRun, rev: str, paths: list[str]) -> dict[str, Entry]:
    """``{path: (mode, oid)}`` of ``paths`` that exist as files/links at ``rev``."""
    found: dict[str, Entry] = {}
    for start in range(0, len(paths), _PATH_CHUNK):
        chunk = paths[start : start + _PATH_CHUNK]
        raw = _checked(run("--literal-pathspecs", "ls-tree", "-z", rev, "--", *chunk), "ls-tree")
        for record in raw.split(b"\0"):
            if not record:
                continue
            meta, _, path = record.partition(b"\t")
            mode, kind, oid = meta.decode("ascii", "replace").split(" ")
            if kind == "tree":
                continue
            found[path.decode("utf-8", "surrogateescape")] = (mode, oid)
    return found


def _blob(run: GitRun, oid: str) -> bytes:
    return _checked(run("cat-file", "blob", oid), "cat-file")


def _lines(data: bytes) -> list[str]:
    """The non-blank lines of ``data`` with trailing whitespace removed (indentation is kept)."""
    out = []
    for raw in data.split(b"\n"):
        text = raw.decode("utf-8", "replace").rstrip()
        if text:
            out.append(text)
    return out


def _block_in(haystack: str, lines: list[str]) -> bool:
    return bool(lines) and "\n" + "\n".join(lines) + "\n" in haystack


def _windows(lines: list[str]) -> list[list[str]]:
    return [lines[i : i + WINDOW] for i in range(0, len(lines), WINDOW)]


def _trivial(lines: list[str]) -> bool:
    return sum(len(x.strip()) for x in lines) < TRIVIAL_CHARS


def _group_missing(added: list[str], removed: list[str], before: str | None, after: str | None, main: str) -> list[str]:
    """The lines of one change group that the default branch (``main`` text) does not reflect."""
    missing: list[str] = []
    for window in _windows(added):
        if _block_in(main, window):
            if not _trivial(window):
                continue
            # a few characters match anywhere: they count only together with BOTH neighbouring lines (the one
            # side that exists at a file's edge); a block with no neighbour at all is never vouched for
            neighbours = [line for line in (before, after) if line is not None]
            framed = [*([before] if before is not None else []), *window, *([after] if after is not None else [])]
            if neighbours and _block_in(main, framed):
                continue
        alone = [line for line in window if "\n" + line + "\n" not in main]
        missing.extend(f"+ {line}" for line in (alone or window))
    for window in _windows(removed):
        if _trivial(window):
            # a short block (`return`, `}`) is everywhere, so "it is not in the file" proves nothing: the deletion
            # is landed only if the block is gone from where its neighbours are; with no neighbour to anchor it,
            # it is never vouched for
            anchored = [*([before] if before is not None else []), *window, *([after] if after is not None else [])]
            pending = len(anchored) == len(window) or _block_in(main, anchored)
        else:
            pending = _block_in(main, window)
        if pending:
            missing.extend(f"- {line}" for line in window)
    return missing


def _text_missing(run: GitRun, old: Entry | None, new: Entry, main_data: bytes) -> list[str]:
    """Lines of the branch's change that the default branch lacks. ``old`` ``None`` = a new file."""
    main = "\n" + "\n".join(_lines(main_data)) + "\n"
    if old is None:
        return _group_missing(_lines(_blob(run, new[1])), [], None, None, main)
    diff = _checked(
        run("diff", "-U3", "--no-color", "--no-ext-diff", "--no-renames", "--diff-algorithm=patience", old[1], new[1]),
        "diff -U3",
    )
    missing: list[str] = []
    added: list[str] = []
    removed: list[str] = []
    before: str | None = None  # the last non-blank context line seen (the neighbour above the next change)
    group_before: str | None = None
    in_group = False

    def flush(after: str | None) -> None:
        nonlocal in_group, added, removed
        if in_group:
            missing.extend(_group_missing(added, removed, group_before, after, main))
        in_group, added, removed = False, [], []

    in_hunk = False
    for raw in diff.split(b"\n"):
        text = raw.decode("utf-8", "replace")
        if text.startswith("@@"):
            flush(None)
            in_hunk, before = True, None
            continue
        if not in_hunk or text.startswith("\\"):
            continue
        marker, body = text[:1], text[1:].rstrip()
        if marker in "+-":
            if not in_group:
                in_group, group_before = True, before
            if body:
                (added if marker == "+" else removed).append(body)
        elif marker == " " and body:  # blank context lines are skipped: the neighbour is the nearest non-blank line
            flush(body)
            before = body
    flush(None)
    return missing


def _unproven(old: Entry | None, main: Entry, note: str, path: str, missing: list[str] | None = None) -> FileFinding:
    """``superseded`` when the default branch's file is not what the branch forked from (it moved on, or
    appeared independently), else ``unique`` — the default branch never touched it."""
    status = SUPERSEDED if main != old else UNIQUE
    return FileFinding(path, status, note, tuple(x[:_LINE_CAP] for x in (missing or [])[:SHOWN_LINES]))


def _classify(run: GitRun, path: str, old: Entry | None, new: Entry | None, main: Entry | None) -> FileFinding:
    if new == main:
        return FileFinding(path, IDENTICAL)
    if new is None:  # the branch deleted it
        if old is not None and main == old:
            return FileFinding(path, UNIQUE, "the branch deleted this file; the default branch still has it unchanged")
        return FileFinding(path, SUPERSEDED, "the branch deleted this file; the default branch still has a changed version")
    if main is None:
        if old is None:
            return FileFinding(path, UNIQUE, "this file exists only on the branch")
        return FileFinding(path, SUPERSEDED, "the default branch deleted this file after the branch forked")
    if _GITLINK in (new[0], main[0]) or _SYMLINK in (new[0], main[0]):
        return _unproven(old, main, "a symlink or submodule differs", path)
    if (old is None or new[0] != old[0]) and new[0] != main[0]:
        return _unproven(old, main, f"the file mode differs ({new[0]} on the branch, {main[0]} on the default branch)", path)
    new_data, main_data = _blob(run, new[1]), _blob(run, main[1])
    if len(new_data) > MAX_BLOB_BYTES or len(main_data) > MAX_BLOB_BYTES:
        return _unproven(old, main, "too large to compare", path)
    if b"\0" in new_data[:_BINARY_PROBE] or b"\0" in main_data[:_BINARY_PROBE]:
        return _unproven(old, main, "binary file differs", path)
    missing = _text_missing(run, old, new, main_data)
    if missing:
        return _unproven(old, main, "lines of the branch's change are not in the default branch's version", path, missing)
    return FileFinding(path, LANDED)


def analyze(run: GitRun, default_sha: str, default_ref: str, tip: str) -> Evidence:
    """Compare what ``tip`` changed since it forked from ``default_sha`` with the default branch."""
    base_res = run("merge-base", default_sha, tip)
    base = _stdout(base_res).decode("ascii", "replace").strip()
    if base_res.returncode != 0 or not base:
        raise ContentError("the branch and the default branch share no history")
    entries = _diff_entries(run, base, tip)
    on_main = _tree_entries(run, default_sha, sorted(entries))
    files = tuple(
        _classify(run, path, old, new, on_main.get(path)) for path, (old, new) in sorted(entries.items())
    )
    total_raw = _checked(run("rev-list", "--count", f"{default_sha}..{tip}"), "rev-list --count").strip()
    log = _checked(
        run("log", f"--max-count={SHOWN_COMMITS}", "--format=%H%x1f%s", f"{default_sha}..{tip}"), "log"
    ).decode("utf-8", "replace")
    commits = tuple(
        CommitLine(sha[:8], subject[:_LINE_CAP])
        for sha, _, subject in (line.partition("\x1f") for line in log.splitlines() if line)
    )
    return Evidence(default_ref, base, commits, int(total_raw or 0), files)
