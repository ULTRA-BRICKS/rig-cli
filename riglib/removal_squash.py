"""Squash-merge proof — "does one commit on the default branch carry the WHOLE batch of this branch's changes?"

A squash merge (or a rebase that rewrote the commits, or a PR re-opened under another name) leaves no commit
id, patch-id or PR head that links the branch to the default branch. What does survive is the squash commit:
it carries the diff of the whole batch of commits, and its message usually lists them. This module finds that
commit and compares DIFFS with DIFFS — never "the latest state of the touched files on the default branch",
which a later commit on those files would break:

1. **Find candidates** among the commits the default branch gained since the fork
   (``base = merge-base(default, tip)``), cheapest first:
   a. *commit-message inclusion* — the message names a commit of the branch (its sha or its subject) or the
      number of a PR opened from the branch (``(#123)``);
   b. failing that, the commits that touched at least one file the branch changed.
2. **Compare the batch with the candidate.** The batch is ``git diff base tip``; the candidate's diff is
   ``git diff candidate^ candidate``. Every file the batch changes must be in the candidate's diff, with the same
   result for a file that is deleted, binary, a link/submodule or has a mode change (the candidate's version must
   be the very same object), and, for text, every change block (the lines added and the lines removed, blank
   lines ignored) must be found contiguously in the candidate's changes of that file. A block shorter than
   :data:`TRIVIAL_CHARS` (``}``, ``pass``) matches anywhere, so it must equal a whole change block of the
   candidate. A candidate may carry MORE than the batch (a squash of several branches); it may not carry less.

Fail CLOSED: a git failure, too many files or too many commits to scan is "not proven".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import removal_content as content

GitRun = content.GitRun

TRIVIAL_CHARS = content.TRIVIAL_CHARS
MAX_FILES = 300
MAX_BRANCH_COMMITS = 300
MAX_DEFAULT_COMMITS = 3000
MAX_CANDIDATES = 60
MIN_SUBJECT_CHARS = 12
_PATH_CHUNK = 100
_BINARY_PROBE = 8000
_REF_RE = re.compile(r"#(\d+)")

Group = tuple[tuple[str, ...], tuple[str, ...]]  # (added lines, removed lines), blank lines dropped


class SquashError(Exception):
    """The comparison could not be completed. Never turned into a yes."""


@dataclass(frozen=True)
class Found:
    sha: str
    subject: str
    how: str  # "its message names the branch's commits" / "it changes the same files"


def _git(run: GitRun, what: str, *args: str) -> bytes:
    res = run(*args)
    if res.returncode != 0:
        detail = (res.stderr or res.stdout).decode("utf-8", "replace").strip()[:200] or f"exit {res.returncode}"
        raise SquashError(f"`git {what}` failed: {detail}")
    return res.stdout


def _lines(data: bytes) -> tuple[str, ...]:
    return tuple(t for t in (raw.decode("utf-8", "replace").rstrip() for raw in data.split(b"\n")) if t)


def _groups(run: GitRun, old: content.Entry | None, new: content.Entry | None) -> tuple[Group, ...]:
    """The change blocks between two blob versions (``None`` = the file does not exist on that side)."""
    if old is None and new is None:
        return ()
    if old is None:
        return ((_lines(_git(run, "cat-file", "cat-file", "blob", new[1])), ()),)  # type: ignore[index]
    if new is None:
        return (((), _lines(_git(run, "cat-file", "cat-file", "blob", old[1]))),)
    diff = _git(
        run, "diff -U0", "diff", "-U0", "--no-color", "--no-ext-diff", "--no-renames", "--diff-algorithm=patience",
        old[1], new[1],
    )
    groups: list[Group] = []
    added: list[str] = []
    removed: list[str] = []
    in_hunk = False

    def flush() -> None:
        nonlocal added, removed
        if added or removed:
            groups.append((tuple(added), tuple(removed)))
        added, removed = [], []

    for raw in diff.split(b"\n"):
        text = raw.decode("utf-8", "replace")
        if text.startswith("@@"):
            flush()
            in_hunk = True
            continue
        if not in_hunk or text.startswith("\\"):
            continue
        body = text[1:].rstrip()
        if text[:1] == "+" and body:
            added.append(body)
        elif text[:1] == "-" and body:
            removed.append(body)
    flush()
    return tuple(groups)


def _is_text(run: GitRun, entry: content.Entry | None) -> bool:
    if entry is None:
        return True
    if entry[0] in ("160000", "120000"):
        return False
    data = _git(run, "cat-file", "cat-file", "blob", entry[1])
    return b"\0" not in data[:_BINARY_PROBE] and len(data) <= content.MAX_BLOB_BYTES


def _joined(groups: tuple[Group, ...], side: int) -> str:
    """Every group's lines of one side, with a separator no source line has between groups (so a block never
    matches across two groups)."""
    return "\n" + "\n\x00\n".join("\n".join(g[side]) for g in groups if g[side]) + "\n"


def _trivial(lines: tuple[str, ...]) -> bool:
    return sum(len(x.strip()) for x in lines) < TRIVIAL_CHARS


def _block_in(haystack: str, lines: tuple[str, ...]) -> bool:
    return bool(lines) and "\n" + "\n".join(lines) + "\n" in haystack


def _contained(batch: tuple[Group, ...], cand: tuple[Group, ...]) -> bool:
    """Every change block of ``batch`` is in ``cand`` (see the module docstring)."""
    cand_added, cand_removed = _joined(cand, 0), _joined(cand, 1)
    unused = list(cand)
    for added, removed in batch:
        if _trivial(added + removed):
            if (added, removed) not in unused:
                return False
            unused.remove((added, removed))
            continue
        if added and not _block_in(cand_added, added):
            return False
        if removed and not _block_in(cand_removed, removed):
            return False
    return True


def _entries(run: GitRun, old_rev: str, new_rev: str) -> dict[str, tuple[content.Entry | None, content.Entry | None]]:
    try:
        return content._diff_entries(run, old_rev, new_rev)
    except content.ContentError as exc:
        raise SquashError(str(exc)) from exc


def batch_in_commit(run: GitRun, base: str, tip: str, commit: str) -> tuple[bool, str]:
    """``(True, "")`` when ``commit``'s diff carries the whole ``base..tip`` batch, else ``(False, why)``."""
    batch = _entries(run, base, tip)
    if not batch:
        return False, "the branch changes nothing"
    if len(batch) > MAX_FILES:
        raise SquashError(f"the branch changes {len(batch)} files; the squash comparison stops at {MAX_FILES}")
    cand = _entries(run, f"{commit}^1", commit)
    for path, (old, new) in sorted(batch.items()):
        if path not in cand:
            return False, f"{path} is not in that commit"
        cand_old, cand_new = cand[path]
        if cand_new == new:
            continue  # the very same result there
        if new is None or cand_new is None:
            return False, f"{path} is deleted on one side only"
        if new[0] != cand_new[0]:
            return False, f"{path} has another file mode there"
        if not (_is_text(run, new) and _is_text(run, cand_new) and _is_text(run, old) and _is_text(run, cand_old)):
            return False, f"{path} is a binary file, a link or too large, and differs there"
        if not _contained(_groups(run, old, new), _groups(run, cand_old, cand_new)):
            return False, f"changes to {path} are not in that commit"
    return True, ""


def _branch_commits(run: GitRun, base: str, tip: str) -> list[tuple[str, str]]:
    out = _git(
        run, "log", "log", "--no-merges", f"--max-count={MAX_BRANCH_COMMITS}", "--format=%H%x1f%s", f"{base}..{tip}"
    ).decode("utf-8", "replace")
    return [(sha, subject) for sha, _, subject in (ln.partition("\x1f") for ln in out.splitlines() if ln)]


def cited_commits(
    run: GitRun, default_sha: str, base: str, tip: str, pr_numbers: frozenset[int]
) -> list[tuple[str, str, frozenset[int]]]:
    """The default branch's commits since the fork whose MESSAGE names a commit of the branch (sha prefix or
    subject) or a PR in ``pr_numbers``: ``(sha, subject, every #N the message cites)``, best match first."""
    mine = _branch_commits(run, base, tip)
    needles = [sha[:8] for sha, _ in mine] + [s for _, s in mine if len(s) >= MIN_SUBJECT_CHARS]
    raw = _git(
        run, "log", "log", f"--max-count={MAX_DEFAULT_COMMITS}", "--format=%H%x1f%B%x1e", f"{base}..{default_sha}"
    ).decode("utf-8", "replace")
    scored: list[tuple[int, str, str, frozenset[int]]] = []
    for record in raw.split("\x1e"):
        sha, _, message = record.strip().partition("\x1f")
        if not sha:
            continue
        cites = frozenset(int(n) for n in _REF_RE.findall(message))
        score = sum(1 for needle in needles if needle in message) + (3 if cites & pr_numbers else 0)
        if score:
            scored.append((score, sha, message.splitlines()[0] if message else "", cites))
    scored.sort(key=lambda item: -item[0])
    return [(sha, subject, cites) for _, sha, subject, cites in scored]


def _touching(run: GitRun, default_sha: str, base: str, paths: list[str]) -> list[tuple[str, str]]:
    if not paths or len(paths) > _PATH_CHUNK:
        return []
    out = _git(
        run, "log", "--literal-pathspecs", "log", f"--max-count={MAX_CANDIDATES}", "--format=%H%x1f%s", f"{base}..{default_sha}",
        "--", *paths,
    ).decode("utf-8", "replace")
    return [(sha, subject) for sha, _, subject in (ln.partition("\x1f") for ln in out.splitlines() if ln)]


def find_squash(
    run: GitRun, default_sha: str, base: str, tip: str, pr_numbers: frozenset[int] = frozenset()
) -> tuple[Found | None, str]:
    """The commit on the default branch that carries the whole branch, or ``(None, why_not)``."""
    seen: set[str] = set()
    tried = 0
    ordered: list[tuple[str, str, str]] = []
    for sha, subject, _ in cited_commits(run, default_sha, base, tip, pr_numbers):
        ordered.append((sha, subject, "its message names the branch's commits"))
    batch_paths = sorted(_entries(run, base, tip))
    ordered.extend((sha, subject, "it changes the same files") for sha, subject in _touching(run, default_sha, base, batch_paths))
    for sha, subject, how in ordered:
        if sha in seen:
            continue
        seen.add(sha)
        if tried >= MAX_CANDIDATES:
            break
        tried += 1
        if run("rev-parse", "--verify", "-q", f"{sha}^1").returncode != 0:
            continue
        ok, _why = batch_in_commit(run, base, tip, sha)
        if ok:
            return Found(sha, subject, how), ""
    if not tried:
        return None, "no commit on the default branch names this branch's commits or changes its files"
    return None, f"none of the {tried} candidate commit(s) on the default branch carries the whole batch of changes"


def pr_numbers_cited_for(
    run: GitRun, default_sha: str, base: str, tip: str
) -> list[tuple[int, str, str]]:
    """``(PR number, commit, subject)`` for every ``#N`` cited by a default-branch commit whose message names the
    branch's commits: the way to find a squash-merged PR whose head branch has another name."""
    found: list[tuple[int, str, str]] = []
    for sha, subject, cites in cited_commits(run, default_sha, base, tip, frozenset()):
        found.extend((n, sha, subject) for n in sorted(cites))
    return found
