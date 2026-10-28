"""File scope and line counting for `rig lint check`.

Why a dedicated module: the scope rules are security-relevant (a symlinked directory must never
let a read or write escape the worktree) and index-vs-worktree semantics (`--staged`) must be
decided in exactly one place. Scope = tracked files (always, even if a newer ignore pattern would
hide them) + untracked files git does not ignore. Every file that is dropped is reported with a
reason by the caller, never silently. Enumeration is NUL-separated end to end, so names with
spaces, newlines or leading dashes survive.

`--staged` materializes the INDEX blobs of the files staged for addition/modification into a
private temp snapshot at the same relative paths; the working tree is never read in that mode.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

BINARY_PROBE_BYTES = 8192
# Aggregate cat-file output is bounded before parsing/copying blobs. Hitting this bound is
# an incomplete check, never a clean result; exempt files are filtered before the request.
MAX_STAGED_OUTPUT_BYTES = 64 * 1024 * 1024
_NULL_SHA = "0" * 40
_GITLINK_MODE = "160000"
_SYMLINK_MODE = "120000"
_REGULAR_MODES = frozenset({"100644", "100755"})
_MODE_SKIPS = {_GITLINK_MODE: "submodule", _SYMLINK_MODE: "symlink"}
# Marker skip for a file no enabled check looks at (e.g. an exempt .json): its content is never
# read from disk or the index, so a huge lockfile costs nothing.
UNCHECKED = "unchecked"


class ScopeError(Exception):
    """Usage-level failure (not a repository, path outside the repository): exit 2."""


@dataclass(frozen=True)
class ScopedFile:
    """One enumerated path. `path` is where its bytes are read from (worktree file or snapshot).

    `skip` is a reason string when the file is excluded from every check; `incomplete` marks a
    file that could not be materialized/inspected (fails closed: the check cannot be trusted).
    """

    rel: str
    path: str
    skip: str | None = None
    incomplete: str | None = None


@dataclass
class Scope:
    root: str
    files: list[ScopedFile] = field(default_factory=list)
    snapshot: str | None = None

    def close(self) -> None:
        if self.snapshot is not None:
            shutil.rmtree(self.snapshot, ignore_errors=True)
            self.snapshot = None


@dataclass(frozen=True)
class FileFacts:
    binary: bool
    lines: int


class BlobLimitError(Exception):
    """The staged snapshot exceeded its memory budget: the check is incomplete."""


def _git_bounded(root: str, args: list[str], stdin: bytes | None, limit: int) -> bytes:
    # File-backed stdin/stderr prevent pipe deadlocks while stdout is read with a hard bound.
    with tempfile.TemporaryFile() as request, tempfile.TemporaryFile() as errors:
        request.write(stdin or b"")
        request.seek(0)
        with subprocess.Popen(
            ["git", "-C", root, *args], stdin=request, stdout=subprocess.PIPE, stderr=errors,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        ) as proc:
            assert proc.stdout is not None
            try:
                output = proc.stdout.read(limit + 1)
                if len(output) > limit:
                    raise BlobLimitError(f"staged snapshot output exceeds the {limit}-byte resource limit")
                returncode = proc.wait()
            except BaseException:
                proc.kill()
                proc.wait()
                raise
            if returncode:
                errors.seek(0)
                detail = errors.read(300).decode("utf-8", "replace").strip()
                raise ScopeError(f"git {args[0]} failed: {detail}")
            return output


def _git(root: str, args: list[str], *, stdin: bytes | None = None,
         max_stdout_bytes: int | None = None) -> bytes:
    if max_stdout_bytes is not None:
        return _git_bounded(root, args, stdin, max_stdout_bytes)
    proc = subprocess.run(
        ["git", "-C", root, *args], input=stdin, capture_output=True, check=False,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()[:300]
        raise ScopeError(f"git {args[0]} failed: {detail}")
    return proc.stdout


def git_toplevel(cwd: str) -> str:
    if not os.path.isdir(cwd):
        raise ScopeError(f"directory does not exist: {cwd}")
    try:
        out = _git(cwd, ["rev-parse", "--show-toplevel"])
    except (ScopeError, OSError) as exc:
        raise ScopeError(f"not a git repository: {cwd} ({exc})") from exc
    return os.path.realpath(out.decode("utf-8", "surrogateescape").rstrip("\n"))


def _restriction_prefix(root: str, base: str, raw: str, staged: bool, indexed: list[str]) -> str:
    joined = os.path.normpath(os.path.join(base, raw))
    located = joined if staged else os.path.join(os.path.realpath(os.path.dirname(joined)), os.path.basename(joined))
    rel = os.path.relpath(located, root).replace(os.sep, "/")
    if rel == ".." or rel.startswith("../"):
        raise ScopeError(f"path is outside the repository: {raw}")
    if rel == ".":
        return ""
    exists = any(_selected(p, [rel]) for p in indexed) if staged else os.path.lexists(located)
    if not exists:
        suffix = " in the index" if staged else ""
        raise ScopeError(f"path does not exist{suffix}: {raw}")
    return rel


def restrictions(root: str, cwd: str, paths: list[str], *, staged: bool = False) -> list[str]:
    """Turn PATH arguments into root-relative prefixes; an empty list selects the repository.

    Worktree arguments resolve parent symlinks, but not the final component. Staged arguments
    refer only to index names: the worktree may have moved, deleted or replaced that path.
    """
    indexed = []
    if staged and paths:
        indexed = [_decode(p) for p in _git(root, ["ls-files", "--cached", "-z"]).split(b"\0") if p]
    base = os.path.realpath(cwd)
    return [_restriction_prefix(root, base, raw, staged, indexed) for raw in paths]


def _selected(rel: str, prefixes: list[str]) -> bool:
    if not prefixes:
        return True
    return any(p == "" or rel == p or rel.startswith(p + "/") for p in prefixes)


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", "surrogateescape")


def _tracked_modes(root: str) -> dict[str, str]:
    modes: dict[str, str] = {}
    for entry in _git(root, ["ls-files", "-z", "--stage"]).split(b"\0"):
        if not entry:
            continue
        meta, _, name = entry.partition(b"\t")
        modes.setdefault(_decode(name), meta.split(b" ")[0].decode("ascii"))
    return modes


def _untracked(root: str, extra_excludes: Sequence[str] = ()) -> list[str]:
    """Untracked, not-ignored files; ``extra_excludes`` are additional git ignore patterns (they never hide tracked files)."""
    out = _git(root, ["ls-files", "-z", "--others", "--exclude-standard", *(f"--exclude={p}" for p in extra_excludes)])
    return [_decode(n) for n in out.split(b"\0") if n]


def _classify_worktree(root: str, rel: str) -> ScopedFile:
    path = os.path.join(root, rel)
    expected_parent = os.path.join(root, os.path.dirname(rel)) if os.path.dirname(rel) else root
    if os.path.realpath(os.path.dirname(path)) != expected_parent:
        return ScopedFile(rel, path, skip="symlink")
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return ScopedFile(rel, path, skip="missing")
    except OSError as exc:
        return ScopedFile(rel, path, incomplete=f"cannot stat: {exc.strerror}")
    if stat.S_ISLNK(mode):
        return ScopedFile(rel, path, skip="symlink")
    if not stat.S_ISREG(mode):
        return ScopedFile(rel, path, skip="not-regular")
    return ScopedFile(rel, path)


def _unmatched_prefix_skips(root: str, prefixes: list[str], names: set[str]) -> list[ScopedFile]:
    """An explicit PATH that selects nothing (gitignored, nothing tracked under it) is reported, not silently passed."""
    return [
        ScopedFile(p, os.path.join(root, p), skip="not-in-scope")
        for p in prefixes
        if p and not any(_selected(n.rstrip("/"), [p]) for n in names)
    ]


def worktree_scope(
    root: str, prefixes: list[str], needed: Callable[[str], bool], extra_excludes: Sequence[str] = ()
) -> Scope:
    modes = _tracked_modes(root)
    names = set(modes) | set(_untracked(root, extra_excludes))
    files: list[ScopedFile] = []
    for rel in sorted(names):
        if not _selected(rel.rstrip("/"), prefixes):
            continue
        if rel.endswith("/") or modes.get(rel) == _GITLINK_MODE:  # `ls-files --others` lists a nested repo as "dir/"
            files.append(ScopedFile(rel.rstrip("/"), os.path.join(root, rel), skip="submodule"))
        elif not needed(rel):
            files.append(ScopedFile(rel, os.path.join(root, rel), skip=UNCHECKED))
        else:
            files.append(_classify_worktree(root, rel))
    files.extend(_unmatched_prefix_skips(root, prefixes, names))
    return Scope(root, files)


def _staged_entries(root: str) -> list[tuple[str, str, str, str]]:
    """(status, mode, sha, path) of every staged add/modify/type-change/unmerged path.

    `--no-renames` reports a rename as delete + add, so the new path is inspected and the old
    one (a pure deletion) correctly disappears from scope. The diff is against HEAD, so during a
    merge (MERGE_HEAD present) every file that differs from HEAD, including the merged-in side,
    is inspected: a merge commit is checked like any other commit, intentionally not exempt.
    """
    out = _git(root, ["diff", "--cached", "--raw", "-z", "--no-renames", "--no-abbrev"])
    parts = out.split(b"\0")
    entries: list[tuple[str, str, str, str]] = []
    i = 0
    while i + 1 < len(parts):
        meta = parts[i]
        if not meta.startswith(b":"):
            break
        fields = meta[1:].decode("ascii").split(" ")
        entries.append((fields[4][0], fields[1], fields[3], _decode(parts[i + 1])))
        i += 2
    return entries


def _read_blobs(root: str, shas: list[str]) -> dict[str, bytes]:
    """Read blobs through one `git cat-file --batch` round trip.

    Every header is consumed with its body (`<sha> <type> <size>\\n<body>\\n`) whatever the type,
    so a non-blob object can never desynchronize the stream; only blobs are returned.
    """
    if not shas:
        return {}
    unique = sorted(set(shas))
    out = _git(root, ["cat-file", "--batch"], stdin=("\n".join(unique) + "\n").encode("ascii"),
               max_stdout_bytes=MAX_STAGED_OUTPUT_BYTES)
    blobs: dict[str, bytes] = {}
    pos = 0
    for sha in unique:
        eol = out.index(b"\n", pos)
        header = out[pos:eol].split(b" ")
        pos = eol + 1
        if len(header) != 3:  # `<sha> missing`: no body follows
            continue
        size = int(header[2])
        if header[1] == b"blob":
            blobs[sha] = out[pos:pos + size]
        pos += size + 1
    return blobs


def _unsafe_rel(rel: str) -> bool:
    """A crafted index entry (`update-index --cacheinfo ... ../x`) must never leave the snapshot."""
    return rel.startswith("/") or any(part in ("", ".", "..") for part in rel.split("/"))


def _staged_file(snapshot: str, entry: tuple[str, str, str, str], blobs: dict[str, bytes],
                 blob_error: str | None = None) -> ScopedFile:
    """Materialize one staged entry under the snapshot dir at its repository-relative path."""
    status, mode, sha, rel = entry
    path = os.path.join(snapshot, rel)
    if _unsafe_rel(rel):
        return ScopedFile(rel, os.path.join(snapshot, "unsafe"), incomplete="unsafe index path; refused")
    if status == "U":
        return ScopedFile(rel, path, incomplete="unmerged index entry")
    if mode in _MODE_SKIPS:
        return ScopedFile(rel, path, skip=_MODE_SKIPS[mode])
    if mode not in _REGULAR_MODES:
        return ScopedFile(rel, path, skip="not-regular")
    blob = blobs.get(sha)
    if sha == _NULL_SHA or blob is None:
        return ScopedFile(rel, path, incomplete=blob_error or "index blob unavailable")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(blob)
    return ScopedFile(rel, path)


def _staged_candidates(root: str, prefixes: list[str]) -> list[tuple[str, str, str, str]]:
    kept = [e for e in _staged_entries(root) if e[0] != "D" and _selected(e[3], prefixes)]
    return sorted(kept, key=lambda e: e[3])


def _blob_shas(entries: list[tuple[str, str, str, str]]) -> list[str]:
    return [sha for status, mode, sha, _ in entries
            if status != "U" and mode in _REGULAR_MODES and sha != _NULL_SHA]


def _case_insensitive(directory: str) -> bool:
    """Whether `directory` folds case (module-level seam: tests force either answer)."""
    probe = os.path.join(directory, ".rig-case-probe")
    with open(probe, "wb"):
        pass
    try:
        return os.path.exists(os.path.join(directory, ".RIG-CASE-PROBE"))
    finally:
        os.unlink(probe)


def _case_clashes(rels: list[str], *, insensitive: bool) -> set[str]:
    """Casefolded names shared by two or more staged paths, on a case-INSENSITIVE snapshot filesystem
    only (there they overwrite each other, so neither holds its own bytes; a case-sensitive one is fine)."""
    if not insensitive:
        return set()
    seen: set[str] = set()
    clashes: set[str] = set()
    for rel in rels:
        (clashes if rel.casefold() in seen else seen).add(rel.casefold())
    # A file can collide with a parent directory too (A versus a/child.ts).
    for rel in rels:
        parts = rel.casefold().split("/")
        for length in range(1, len(parts)):
            parent = "/".join(parts[:length])
            if parent in seen:
                clashes.update((parent, rel.casefold()))
    return clashes


def staged_scope(root: str, prefixes: list[str], needed: Callable[[str], bool]) -> Scope:
    entries = _staged_candidates(root, prefixes)
    blob_error = None
    try:
        blobs = _read_blobs(root, _blob_shas([e for e in entries if needed(e[3])]))
    except BlobLimitError as exc:
        blobs = {}
        blob_error = str(exc)
    snapshot = os.path.realpath(tempfile.mkdtemp(prefix="rig-lint-staged-"))  # analyzers may report canonical paths
    scope = Scope(root, snapshot=snapshot)
    try:
        clashes = _case_clashes([e[3] for e in entries if needed(e[3])], insensitive=_case_insensitive(snapshot))
        for entry in entries:
            rel = entry[3]
            if not needed(rel):
                scope.files.append(ScopedFile(rel, os.path.join(snapshot, rel), skip=UNCHECKED))
            elif rel.casefold() in clashes:
                scope.files.append(ScopedFile(rel, os.path.join(snapshot, rel),
                                              incomplete="staged path collides with another that differs only by case"))
            else:
                scope.files.append(_staged_file(snapshot, entry, blobs, blob_error))
    except BaseException:
        scope.close()
        raise
    return scope


def build_scope(cwd: str, paths: list[str], *, staged: bool, needed: Callable[[str], bool]) -> Scope:
    """`needed(rel)` says whether any enabled check inspects that path; others are never read."""
    root = git_toplevel(cwd)
    prefixes = restrictions(root, cwd, paths, staged=staged)
    return staged_scope(root, prefixes, needed) if staged else worktree_scope(root, prefixes, needed)


def _open_readonly_no_symlinks(path: str) -> int:
    """Pin each directory before opening the next component; never follow a swapped parent."""
    components = os.path.abspath(path).split(os.sep)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    parent = os.open(os.sep, directory_flags)
    try:
        for component in components[1:-1]:
            if not component:
                continue
            child = os.open(component, directory_flags, dir_fd=parent)
            os.close(parent)
            parent = child
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
        return os.open(components[-1], flags, dir_fd=parent)
    finally:
        os.close(parent)


def inspect_file(path: str) -> FileFacts:
    """Binary probe + physical line count in one streaming pass (raises OSError).

    Every path component is opened relative to a pinned directory descriptor with O_NOFOLLOW.
    Lines: every LF counts once (so CRLF counts once) and an unterminated final line counts one
    more, so a missing trailing newline cannot hide a line.
    """
    fd = _open_readonly_no_symlinks(path)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError("not a regular file")
        first = handle.read(BINARY_PROBE_BYTES)
        if b"\0" in first:
            return FileFacts(binary=True, lines=0)
        lines = first.count(b"\n")
        last = first[-1:]
        while True:
            chunk = handle.read(1 << 20)
            if not chunk:
                break
            lines += chunk.count(b"\n")
            last = chunk[-1:]
    if last and last != b"\n":
        lines += 1
    return FileFacts(binary=False, lines=lines)


def extension(rel: str) -> str:
    """Lowercase extension without the dot ('' when there is none)."""
    return os.path.splitext(os.path.basename(rel))[1].lstrip(".").lower()


def _changed_names(root: str, extra: list[str]) -> set[str]:
    out = _git(root, ["diff", "--name-only", "-z", "--no-renames", *extra])
    return {_decode(n) for n in out.split(b"\0") if n}


def staged_paths(root: str) -> set[str]:
    """Paths whose index content differs from HEAD (staged for the next commit)."""
    return _changed_names(root, ["--cached"])


def partially_staged(root: str) -> set[str]:
    """Paths with BOTH staged changes (index != HEAD) and unstaged changes (worktree != index).

    Rewriting such a file would silently mix the two states, so `--fix` refuses them.
    """
    return staged_paths(root) & _changed_names(root, [])
