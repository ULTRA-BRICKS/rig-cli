"""File inventory and language tables of the lint discovery.

Context: the discovery orchestrator needs three things from the worktree before it reads a single
manifest: which regular files exist (tracked + untracked-not-ignored, via the one shared enumerator),
which of them are symlinks (never read, reported), and which language each source file speaks. All
of it is decided by NAME, extension or the first 128 bytes of an extensionless file; nothing is
imported or executed. Vendored directories the repository tracks are real files and stay in the
inventory (the tracked list is the truth); ignored ones (node_modules, .venv) never appear.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Mapping, Sequence

from .lint_check_scope import ScopeError, _git, worktree_scope
from .lint_discovery_model import UnknownLanguage

SHEBANG_BYTES = 128

SOURCE_EXTENSIONS: Mapping[str, str] = {
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "typescript",
    ".py": "python", ".pyi": "python", ".sh": "shell", ".bash": "shell", ".go": "go", ".rs": "rust",
}

# Closed table: only code extensions. Docs/data (.md .json .yaml .toml .lock .svg ...) are never "unknown languages".
UNKNOWN_EXTENSIONS: Mapping[str, str] = {
    ".kt": "kotlin", ".kts": "kotlin", ".java": "java", ".rb": "ruby", ".php": "php", ".c": "c", ".h": "c",
    ".cpp": "cpp", ".hpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".cs": "csharp", ".swift": "swift", ".lua": "lua",
    ".ex": "elixir", ".exs": "elixir", ".scala": "scala", ".dart": "dart", ".zig": "zig", ".hs": "haskell",
    ".clj": "clojure", ".cljs": "clojure", ".pl": "perl", ".sql": "sql", ".m": "objective-c", ".mm": "objective-c",
    ".r": "r", ".jl": "julia", ".groovy": "groovy", ".vb": "vb", ".fs": "fsharp", ".erl": "erlang", ".ml": "ocaml",
}
DENO_CONFIGS = frozenset({"deno.json", "deno.jsonc"})
MAX_EXAMPLES = 3

_SHELL_SHEBANG = re.compile(rb"^#![ \t]*(?:\S*/)?(?:env[ \t]+(?:-\S+[ \t]+)*)?(?:\S*/)?(?:sh|bash|dash|ksh)(?:[ \t]|$)")


@dataclass(frozen=True)
class Inventory:
    files: tuple[str, ...]                 # sorted repo-relative regular files
    symlinks: tuple[str, ...]              # sorted repo-relative symlinks (never read)
    names: Mapping[str, frozenset[str]]    # directory ("." = root) -> basenames of its regular files

    @property
    def total(self) -> int:
        return len(self.files) + len(self.symlinks)


def directory_of(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else "."


def enumerate_inventory(root: str, prefixes: list[str], extra_excludes: Sequence[str] = ()) -> Inventory:
    """Tracked + untracked-not-ignored files below ``prefixes``; symlinks are separated, other skips dropped.

    ``extra_excludes`` are git ignore patterns that are not on disk yet (a bootstrap's pending ignore of
    ``node_modules``): they hide untracked files only, exactly as if they were already ignored.
    """
    scope = worktree_scope(root, prefixes, lambda _rel: True, extra_excludes)
    files: list[str] = []
    links: list[str] = []
    for entry in scope.files:
        if entry.skip is None:
            files.append(entry.rel)
        elif entry.skip == "symlink":
            links.append(entry.rel)
    names: dict[str, set[str]] = {}
    for rel in files:
        names.setdefault(directory_of(rel), set()).add(rel.rsplit("/", 1)[-1])
    return Inventory(tuple(sorted(files)), tuple(sorted(links)), {d: frozenset(n) for d, n in names.items()})


def symlink_escapes(root: str, rel: str) -> bool:
    """Whether the symlink at ``rel`` resolves outside the repository (a dangling link resolves to where it points)."""
    target = os.path.realpath(os.path.join(root, rel))
    return os.path.commonpath([root, target]) != root


def _read_head(path: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return b""
    try:
        return os.read(fd, SHEBANG_BYTES)
    finally:
        os.close(fd)


def has_shell_shebang(path: str) -> bool:
    first_line = _read_head(path).split(b"\n", 1)[0]
    return bool(_SHELL_SHEBANG.match(first_line))


def source_language(root: str, rel: str) -> str | None:
    """Supported language of one file: by extension, or by shebang for extensionless files."""
    ext = os.path.splitext(rel)[1].lower()
    if ext:
        return SOURCE_EXTENSIONS.get(ext)
    return "shell" if has_shell_shebang(os.path.join(root, rel)) else None


def classify_sources(root: str, files: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    """Language -> every source file of a supported language (sorted, repo-relative)."""
    found: dict[str, list[str]] = {}
    for rel in files:
        language = source_language(root, rel)
        if language:
            found.setdefault(language, []).append(rel)
    return {language: tuple(items) for language, items in sorted(found.items())}


def unknown_languages(files: tuple[str, ...], extra: Mapping[str, list[str]]) -> tuple[UnknownLanguage, ...]:
    """Files of code languages Rig has no provider for, plus ``extra`` (deno configs, orphan go/rust files)."""
    grouped: dict[str, list[str]] = {language: list(items) for language, items in extra.items()}
    for rel in files:
        language = UNKNOWN_EXTENSIONS.get(os.path.splitext(rel)[1].lower())
        if language:
            grouped.setdefault(language, []).append(rel)
    return tuple(
        UnknownLanguage(language, len(items), tuple(sorted(items)[:MAX_EXAMPLES]))
        for language, items in sorted(grouped.items())
    )


def deno_configs(files: tuple[str, ...]) -> list[str]:
    return [rel for rel in files if rel.rsplit("/", 1)[-1] in DENO_CONFIGS]


def ignored_directories(root: str) -> tuple[str, ...]:
    """Top-most git-ignored directories (``.worktrees``, ``dist``), sorted; fail-soft: a git error yields ``()``.

    Providers use it to keep nested vendored/ignored trees (other worktrees, build output) out of file lists.
    """
    try:
        out = _git(root, ["ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory"])
    except (ScopeError, OSError):
        return ()
    names = (n.decode("utf-8", "surrogateescape") for n in out.split(b"\0") if n.endswith(b"/"))
    return tuple(sorted(n.rstrip("/") for n in names))


def tracked_ignored_files(root: str, enumerated: Sequence[str]) -> tuple[str, ...]:
    """Enumerated files that git TRACKS although an ignore rule matches them (``git add -f vendor/x.ts``), sorted.

    A tool that walks a directory honouring ``.gitignore`` (Oxlint, Oxfmt, Ruff, Black, Prettier) never opens
    these, so coverage must not count them as checked unless a check names them explicitly. Fail-soft: a git
    error yields ``()``.
    """
    try:
        out = _git(root, ["ls-files", "-z", "--cached", "--ignored", "--exclude-standard"])
    except (ScopeError, OSError):
        return ()
    names = {n.decode("utf-8", "surrogateescape") for n in out.split(b"\0") if n}
    return tuple(sorted(names & set(enumerated)))

