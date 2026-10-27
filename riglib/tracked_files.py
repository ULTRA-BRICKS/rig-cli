"""``tracked_files`` — land rig-owned, repo-tracked file edits ONCE on the default branch.

Why
---
Some files rig owns are COMMITTED to the repo — ``rig.yaml`` itself, task-cli's ``task.yaml``,
the ``.ship-config`` rig reconciles. When such a file changes (a config migration, rig's own
reconcile), writing it into whichever checkout ``rig apply`` runs in leaves that checkout dirty.
Repeat that in every worktree of a repo and the same uncommitted edit piles up in dozens of
worktrees while never reaching the default branch (the UB-3 task-routing migration left about 200
worktrees dirty this way). Every such worktree then blocks ``gh ship``'s clean-tree gate and
``git worktree remove``.

What
----
With a ``tracked_files:`` block in the GLOBAL config (``config.load`` rejects it from a repo layer:
it runs the ship command and decides which files leave the machine), rig treats those paths as
something to LAND, not to write in place:

1. **Divert.** :func:`riglib.plan.build` moves every action that writes a listed path out of
   ``plan.actions`` into ``plan.diverted``, so no plan consumer (apply, ``config set``, init,
   config-web, status) writes such a file into a working checkout.
2. **Harvest.** ``rig apply`` scans every checkout of the repo (the primary one included) for an
   uncommitted edit of a listed path whose committed copy (``HEAD``) still equals
   ``origin/<default>``'s. Byte-identical edits are grouped; the group at least
   ``harvest_min_worktrees`` checkouts agree on becomes the landing content. Two competing groups
   that both reach the threshold are a conflict and land nothing for that path. Only regular
   files are harvested: a deletion or a symlink never is.
3. **Land** (``rig apply commit``). In one non-interactive batch: fetch, create a locked worktree
   at ``origin/<default>`` (``<repo>/.worktrees/rig-tracked-files``), write the harvested content,
   run the diverted actions there, record a review of the staged diff when review-cli is
   installed, commit, push a fresh ``rig/tracked-files-<hash>-<utc>`` branch, open the PR (``gh``
   for GitHub, ``fj`` for Forgejo) and — with ``land: ship`` and a ticket — start the gated ship
   in the background. The background lander retries while CI is pending or the review-dwell window
   is open, merges the default branch in when the PR falls BEHIND (never a force-push), and logs
   every attempt.
4. **Normalize.** Every checkout whose uncommitted copy of a listed path equals
   ``origin/<default>``'s gets that path checked out again (a modified copy restored to ``HEAD``,
   an untracked new file removed). Locked worktrees, worktrees a live agent sits in, and copies
   changed within ``normalize_min_age_minutes`` are left alone, as is anything that differs from
   the default branch.

State lives in ``$XDG_STATE_HOME/rig/tracked-files/<repo-key>.json`` (default
``~/.local/state/…``). While a landing is in flight, ``rig apply`` reports it instead of opening a
second PR; once the default branch carries the landed content, or the background lander has
stopped, the landing is no longer in flight.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as _dt
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable

DEFAULT_PATHS: tuple[str, ...] = ("rig.yaml", "task.yaml", ".ship-config")
# Files rig GENERATES from other config (``.ship-config`` from ``task.code_prefix``): only rig's own
# reconcile writes them on the landing branch. A worktree copy is never harvested — ship reads this
# file, so a hand-made line in it must not ride along with a "rig-owned" landing.
GENERATED_PATHS: frozenset[str] = frozenset({".ship-config"})
DEFAULT_LAND = "ship"
DEFAULT_SHIP_COMMAND = "gh ship"
DEFAULT_HARVEST_MIN_WORKTREES = 2
DEFAULT_NORMALIZE_MIN_AGE_MINUTES = 120
LANDING_WORKTREE_NAME = "rig-tracked-files"
BRANCH_PREFIX = "rig/tracked-files-"
_GIT_TIMEOUT_S = 60
_NET_TIMEOUT_S = 180
# The background lander gives up after this long (CI queues on self-hosted runners can take hours).
SHIP_DEADLINE_S = 8 * 3600
SHIP_RETRY_S = 300
# A spawned lander records its pid within seconds; one that has not after this long never started.
LANDER_START_GRACE_S = 300
_WINDOWS = os.name == "nt"
# `ship` refusals that clear on their own with time or a branch update (everything else is final).
# The wordings are agent-tools ci/ship/ship.sh's `Refusing:` lines (pending CI, the review-dwell
# gate, an unreadable CI status, a head BEHIND its base). If ship rewords one, that refusal turns
# final: the lander stops and `rig status` shows the refusal — it never merges on a guess.
_BEHIND = r"BEHIND|not up to date|is behind"
_BEHIND_SHIP = re.compile(_BEHIND, re.IGNORECASE)
_RETRYABLE_SHIP = re.compile(
    r"pending check|still has \d+ pending|review-dwell|only \d+s old since its last push|"
    r"could not read CI status|" + _BEHIND,
    re.IGNORECASE,
)
# fj wraps values in Unicode isolation marks (LRI U+2066, RLI U+2067, FSI U+2068, PDI U+2069)
_BIDI_MARKS = re.compile("[" + chr(0x2066) + "-" + chr(0x2069) + "]")
# Git env that redirects repo resolution or injects config (a hook/wrapper env leaks these): every
# git call here drops them, as riglib.core_bare does, so a commit/push never hits another repo.
_SCRUBBED_GIT_ENV = frozenset({
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM",
})
_SCRUBBED_GIT_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")


def _clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in _SCRUBBED_GIT_ENV and not k.startswith(_SCRUBBED_GIT_PREFIXES)}
    env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"})
    env.update(extra or {})
    return env


@contextlib.contextmanager
def _scrubbed_process_env():
    """Drop the leaked git env from THIS process while code we do not own (riglib.worktree) runs git
    with the inherited environment; restored afterwards."""
    saved = {k: v for k, v in os.environ.items() if k in _SCRUBBED_GIT_ENV or k.startswith(_SCRUBBED_GIT_PREFIXES)}
    for k in saved:
        del os.environ[k]
    try:
        yield
    finally:
        os.environ.update(saved)


class TrackedFilesError(RuntimeError):
    """A landing step failed; the message says which and why."""


class LandingLockBusy(TrackedFilesError):
    """Another landing (or a post-merge cleanup) holds this repo's landing lock."""


# ── config ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Settings:
    """The resolved ``tracked_files`` block (defaults applied)."""

    enabled: bool = False
    paths: tuple[str, ...] = DEFAULT_PATHS
    land: str = DEFAULT_LAND
    harvest_min_worktrees: int = DEFAULT_HARVEST_MIN_WORKTREES
    normalize_worktrees: bool = True
    normalize_min_age_minutes: int = DEFAULT_NORMALIZE_MIN_AGE_MINUTES
    ship_task_code: str | None = None
    ship_task_config: str | None = None
    ship_args: tuple[str, ...] = ()
    ship_command: str = DEFAULT_SHIP_COMMAND
    review_models: str | None = None


def settings_from_config(data: dict[str, Any]) -> Settings:
    """Resolve the (already validated) ``tracked_files`` block. Block PRESENCE opts in.

    A key the block sets is taken as written (the validator already rejected empty/null values);
    only an ABSENT key falls back to its default.
    """
    block = data.get("tracked_files")
    if not isinstance(block, dict):
        return Settings(enabled=False)
    ship = block.get("ship", {})
    review = block.get("review", {})
    return Settings(
        enabled=block.get("enabled", True) is True,
        paths=tuple(block["paths"]) if "paths" in block else DEFAULT_PATHS,
        land=block.get("land", DEFAULT_LAND),
        harvest_min_worktrees=int(block.get("harvest_min_worktrees", DEFAULT_HARVEST_MIN_WORKTREES)),
        normalize_worktrees=block.get("normalize_worktrees", True) is True,
        normalize_min_age_minutes=int(block.get("normalize_min_age_minutes", DEFAULT_NORMALIZE_MIN_AGE_MINUTES)),
        ship_task_code=ship.get("task_code"),
        ship_task_config=ship.get("task_config"),
        ship_args=tuple(ship.get("args", ())),
        ship_command=ship.get("command", DEFAULT_SHIP_COMMAND),
        review_models=review.get("models"),
    )


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """git ``:(glob)`` semantics: ``*``/``?`` stay inside one path segment; ``**`` crosses them only
    as a whole segment (``**/x``, ``a/**/b``, ``a/**``) — anywhere else it is a plain ``*``."""
    out = []
    i = 0
    while i < len(pattern):
        whole_segment = (pattern.startswith("**", i) and (i == 0 or pattern[i - 1] == "/")
                         and (i + 2 == len(pattern) or pattern[i + 2] == "/"))
        if whole_segment and i + 2 == len(pattern):
            out.append(".*")  # a trailing `**`: everything below
            i += 2
        elif whole_segment:
            out.append("(?:.*/)?")  # `**/`: zero or more directories
            i += 3
        elif pattern[i] == "*":
            while i < len(pattern) and pattern[i] == "*":
                i += 1
            out.append("[^/]*")
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def path_is_tracked_owned(rel: str, paths: tuple[str, ...]) -> bool:
    """True when repo-relative ``rel`` (POSIX) is one of ``paths``.

    Globs follow git's ``:(glob)`` pathspec rules — the SAME ones the worktree scan lists files
    with — so ``*.yml`` means the repo root only and ``**/*.yml`` means any depth.
    """
    return any(rel == p or _glob_regex(p).match(rel) for p in paths)


def inside_work_tree(path: Path) -> bool:
    return _git(path, "rev-parse", "--is-inside-work-tree", check=False).stdout.strip() == b"true"


def owned_relpath(target: Path, repo_root: Path, paths: tuple[str, ...]) -> str | None:
    """``target``'s repo-relative POSIX path when it is a listed file that would dirty the checkout.

    A TRACKED file always counts, even when an ignore rule also matches it (committed first,
    ignored later — git still reports its changes). An UNTRACKED file counts unless ignored.
    """
    try:
        rel = Path(os.path.abspath(target)).relative_to(Path(os.path.abspath(repo_root))).as_posix()
    except ValueError:
        return None
    if rel in (".", ""):
        return None  # the repo root itself is never a landable file
    if not path_is_tracked_owned(rel, paths):
        return None
    if _git(repo_root, "ls-files", "--error-unmatch", "--", rel, check=False).returncode == 0:
        return rel
    if _git(repo_root, "check-ignore", "-q", "--", rel, check=False).returncode == 0:
        return None  # an ignored, untracked file never dirties the checkout; nothing to land
    return rel


# Actions whose ``target`` is a DIRECTORY and that write a fixed file inside it.
_ACTION_FILES: dict[str, tuple[str, ...]] = {
    "provision_ship_task_prefix": (".ship-config",),  # target = repo root (runner/drift use target/.ship-config)
    # target = repo root; the one rig-owned workflow. A literal here keeps this module free of the lint_checks
    # import chain; tests/test_lint_checks_plan.py pins it to lint_checks_artifacts.WORKFLOW_REL. The sibling
    # install_lint_check_hook writes under the global dispatcher dir, never inside a repo, so it can
    # never be a tracked-owned file and needs no entry.
    "install_lint_check_workflow": (".github/workflows/rig-lint-check.yml",),
}


def action_written_paths(action: Any) -> list[Path]:
    """The file(s) a plan action writes: ``target`` itself, or the fixed file(s) under it.

    A DIRECTORY target with no entry in ``_ACTION_FILES`` writes no tracked file of its own (the
    GitHub-API provisioners use the repo root only as a display target), so it is never diverted —
    however broad a ``paths`` glob is. A target that does not exist yet and has no file suffix
    may be a directory still to be created (a skill dir, say): not diverted either, so such an
    install is never dropped — at worst it is written into the checkout as without the block.
    """
    target = getattr(action, "target", None)
    if not isinstance(target, Path):
        return []
    names = _ACTION_FILES.get(getattr(action, "kind", ""), ())
    if names:
        return [target / n for n in names]
    if target.is_dir() or (not target.exists() and not target.suffix):
        return []
    return [target]


def divert_actions(actions: list[Any], repo_root: Path, settings: Settings) -> tuple[list[Any], list[Any]]:
    """Split a plan's actions into (kept, diverted): diverted ones write a tracked-owned repo file."""
    if not settings.enabled:
        return list(actions), []
    kept: list[Any] = []
    diverted: list[Any] = []
    for action in actions:
        if any(owned_relpath(p, repo_root, settings.paths) for p in action_written_paths(action)):
            diverted.append(action)
        else:
            kept.append(action)
    return kept, diverted


# ── git plumbing ──────────────────────────────────────────────────────────────────


Runner = Callable[..., subprocess.CompletedProcess]


def _git(cwd: Path, *args: str, timeout: int = _GIT_TIMEOUT_S, check: bool = True,
         input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-C", str(cwd), "-c", "core.quotepath=off", *args],
        capture_output=True, timeout=timeout, input=input_bytes,
        env=_clean_env(),
    )
    if check and proc.returncode != 0:
        msg = proc.stderr.decode("utf-8", "replace").strip() or proc.stdout.decode("utf-8", "replace").strip()
        raise TrackedFilesError(f"git {' '.join(args[:3])} failed in {cwd}: {msg}")
    return proc


def _out(cwd: Path, *args: str, **kw: Any) -> str:
    return _git(cwd, *args, **kw).stdout.decode("utf-8", "replace").strip()


def _run(cmd: list[str], cwd: Path, *, timeout: int = _NET_TIMEOUT_S, env: dict[str, str] | None = None,
         runner: Runner | None = None) -> subprocess.CompletedProcess:
    """A network-facing command (push, forge CLIs, review, ship) through the injectable runner."""
    fn = runner or subprocess.run
    return fn(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
              env=_clean_env(env))


def _push(cwd: Path, branch: str, *, runner: Runner | None = None, upstream: bool = False) -> None:
    cmd = ["git", "push", "-q", *(["-u"] if upstream else []), "origin", f"HEAD:refs/heads/{branch}"]
    proc = _run(cmd, cwd, runner=runner)
    if proc.returncode != 0:
        raise TrackedFilesError(f"git push failed: {(proc.stderr or proc.stdout).strip()[-400:]}")


def default_branch(repo_root: Path) -> str | None:
    """``origin``'s default branch from ``refs/remotes/origin/HEAD``, else main/master if present."""
    ref = _out(repo_root, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD", check=False)
    if ref.startswith("origin/") and _git(repo_root, "rev-parse", "-q", "--verify", f"refs/remotes/{ref}",
                                          check=False).returncode == 0:
        return ref[len("origin/"):]
    # (a dangling origin/HEAD — the branch was renamed on the remote — falls through)
    for name in ("main", "master"):
        if _git(repo_root, "rev-parse", "-q", "--verify", f"refs/remotes/origin/{name}", check=False).returncode == 0:
            return name
    return None


@dataclass(frozen=True)
class Checkout:
    """One working tree of the repo (the primary checkout or a linked worktree)."""

    path: Path
    head: str | None
    branch: str | None
    locked: bool


def list_checkouts(repo_root: Path) -> list[Checkout]:
    """Every non-bare working tree ``git worktree list`` knows (``primary_checkout`` names the primary)."""
    raw = _git(repo_root, "worktree", "list", "--porcelain", "-z").stdout.decode("utf-8", "replace")
    out: list[Checkout] = []
    cur: dict[str, str] = {}

    def _flush() -> None:
        if cur.get("worktree") and "bare" not in cur:
            out.append(Checkout(
                path=Path(cur["worktree"]),
                head=cur.get("HEAD"),
                branch=cur.get("branch", "").removeprefix("refs/heads/") or None,
                locked="locked" in cur,
            ))

    for record in raw.split("\0"):
        if not record:
            _flush()
            cur = {}
            continue
        key, _, value = record.partition(" ")
        cur[key] = value
    _flush()
    return out


def primary_checkout(repo_root: Path) -> Path:
    """The repo's primary working tree (``<common>/..``), whichever checkout ``repo_root`` is.

    A bare repository has none; its landing worktree then sits under ``repo_root``.
    """
    common = _common_dir(repo_root)
    return common.parent if common.name == ".git" else repo_root


def landing_worktree_path(repo_root: Path) -> Path:
    """``<primary checkout>/.worktrees/rig-tracked-files`` — ONE location per repo, whichever
    checkout ``rig apply`` runs in (the standard ``rig worktree create`` layout)."""
    return primary_checkout(repo_root) / ".worktrees" / LANDING_WORKTREE_NAME


# ── discovery: what would land, what can be normalized ────────────────────────────


@dataclass
class Candidate:
    """One uncommitted edit of a tracked-owned path in one checkout.

    ``blob`` is the id git would store for the working copy (``git hash-object --path``, so
    autocrlf/clean filters apply) — the value every comparison uses; ``content`` is the working
    bytes a landing writes (``git add`` in the landing worktree cleans them the same way).
    """

    checkout: Path
    rel: str
    content: bytes
    blob: str
    mtime: float


@dataclass
class PathLanding:
    """The content one path would land with, and which checkouts agree on it."""

    rel: str
    content: bytes
    supporters: list[Path]


@dataclass
class Discovery:
    """What ``rig apply`` found for this repo's tracked-owned paths."""

    repo_root: Path
    default: str | None
    main_rev: str | None
    landings: list[PathLanding] = field(default_factory=list)
    conflicts: dict[str, list[list[Path]]] = field(default_factory=dict)
    landed_copies: list[Candidate] = field(default_factory=list)  # uncommitted copies == main
    below_threshold: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def content_key(self) -> str:
        """A stable hash of what would land (names the landing branch)."""
        h = hashlib.sha256()
        for pl in sorted(self.landings, key=lambda p: p.rel):
            h.update(pl.rel.encode() + b"\0" + pl.content + b"\0")
        return h.hexdigest()[:12]


@dataclass
class _CheckoutScan:
    """What one checkout carries: copies already equal to the default branch (``landed``), edits
    that may land (``harvest``), and changed files only a directory-naming entry matched."""

    checkout: Path
    failed: bool = False  # `git status` could not run there
    landed: list[Candidate] = field(default_factory=list)
    harvest: list[Candidate] = field(default_factory=list)
    under_directory: set[str] = field(default_factory=set)


def _status_paths(checkout: Path, paths: tuple[str, ...],
                  under_directory: set[str] | None = None) -> list[str] | None:
    """Repo-relative tracked-owned paths with an uncommitted change (tracked or untracked).

    ``None`` when ``git status`` itself failed (a transient ``index.lock``, say), so the caller
    can say so instead of silently dropping that checkout's vote. A git pathspec also matches
    everything under a directory it names, while ``paths`` entries name files: a changed file
    that only such a directory match reported is left out and its path added to
    ``under_directory``, so the caller can say the entry names a directory.
    """
    pathspecs = [f":(glob){p}" for p in paths]
    proc = _git(checkout, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *pathspecs, check=False)
    if proc.returncode != 0:
        return None
    rels: list[str] = []
    entries = proc.stdout.decode("utf-8", "replace").split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, rel = entry[:2], entry[3:]
        if xy[0] in "RC":  # rename/copy: the next field is the source path
            i += 1
        if path_is_tracked_owned(rel, paths):
            rels.append(rel)
        elif under_directory is not None and any(
            path_is_tracked_owned(str(parent), paths) for parent in PurePosixPath(rel).parents if str(parent) != "."
        ):
            under_directory.add(rel)  # (a rename's new name matching no entry is not a directory match)
    return rels


def discover(repo_root: Path, settings: Settings, *, checkouts: list[Checkout] | None = None) -> Discovery:
    """Scan every checkout of ``repo_root`` for un-landed and already-landed tracked-owned edits.

    Read-only: no fetch (the caller fetches first on ``commit``), no writes. Only a REGULAR file
    counts: a deleted copy or a symlink (which could point anywhere, e.g. at a key) is neither
    harvested nor normalized.
    """
    dflt = default_branch(repo_root)
    disc = Discovery(repo_root=repo_root, default=dflt, main_rev=None)
    if dflt is None:
        disc.notes.append("no origin default branch (origin/HEAD, main or master) — nothing to land against")
        return disc
    disc.main_rev = main_rev = _out(repo_root, "rev-parse", f"refs/remotes/origin/{dflt}")
    if checkouts is None:
        checkouts = list_checkouts(repo_root)
    landing_wt = landing_worktree_path(repo_root).resolve()
    scan = [co for co in checkouts if co.path.is_dir() and co.path.resolve() != landing_wt]
    main_ids: dict[str, str | None] = {}

    def main_id(rel: str) -> str | None:
        if rel not in main_ids:  # benign race: two workers may both look up the same blob id
            main_ids[rel] = _blob_id_at(repo_root, main_rev, rel)
        return main_ids[rel]

    def scan_one(co: Checkout) -> _CheckoutScan:
        res = _CheckoutScan(co.path)
        rels = _status_paths(co.path, settings.paths, res.under_directory)
        if rels is None:
            res.failed = True
            return res
        for rel in rels:
            cand = _candidate(co.path, rel)
            if cand is None:
                continue
            if cand.blob == main_id(rel):
                # already landed -- but only a copy whose INDEX holds nothing of its own is safe to
                # reset (a staged blob that differs from both HEAD and main would be lost)
                if _index_is_head_or(co.path, rel, main_rev):
                    res.landed.append(cand)
            elif rel not in GENERATED_PATHS and co.head and _blob_id_at(co.path, "HEAD", rel) == main_id(rel):
                res.harvest.append(cand)
        return res

    # one checkout per task, in parallel: a repo can carry hundreds of worktrees
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(scan_one, scan))
    groups: dict[str, dict[str, list[Candidate]]] = {}
    under_directory: set[str] = set()
    for res in results:
        if res.failed:
            disc.notes.append(f"could not read `git status` in {res.checkout} — its copies were not counted")
            continue
        disc.landed_copies.extend(res.landed)
        for cand in res.harvest:
            groups.setdefault(cand.rel, {}).setdefault(cand.blob, []).append(cand)
        under_directory |= res.under_directory
    if under_directory:
        sample = ", ".join(sorted(under_directory)[:3])
        disc.notes.append(f"a tracked_files.paths entry names a directory; changed files under it ({sample}) "
                          "were not counted — list the files, or a glob such as `dir/*.yml`")
    for rel, by_content in sorted(groups.items()):
        ranked = sorted(by_content.items(), key=lambda kv: -len(kv[1]))
        qualifying = [(c, lst) for c, lst in ranked if len(lst) >= settings.harvest_min_worktrees]
        if not qualifying:
            disc.below_threshold[rel] = len(ranked[0][1])
            continue
        if len(qualifying) > 1:
            disc.conflicts[rel] = [[c.checkout for c in lst] for _, lst in qualifying]
            continue
        winner, lst = qualifying[0]
        disc.landings.append(PathLanding(rel, lst[0].content, [c.checkout for c in lst]))
        others = sum(len(group) for blob, group in ranked if blob != winner)
        if others:  # a minority edit is left alone — and said so, never silently
            disc.notes.append(f"{others} other worktree(s) carry a different edit of {rel}; left as they are")
    return disc


def _candidate(checkout: Path, rel: str) -> Candidate | None:
    """The checkout's working copy of ``rel`` — only a REGULAR file (never a symlink, never a deletion).

    The bytes are read ONCE and hashed from that same read (``hash-object --stdin --path``), so the
    content that wins the harvest vote is exactly the content that lands.
    """
    fp = checkout / rel
    if _has_symlink_component(checkout, rel) or not fp.is_file():
        return None
    try:
        mtime = fp.stat().st_mtime
        content = fp.read_bytes()
    except OSError:
        return None
    proc = _git(checkout, "hash-object", "--stdin", f"--path={rel}", check=False, input_bytes=content)
    blob = proc.stdout.decode("utf-8", "replace").strip()
    if proc.returncode != 0 or not blob:
        return None
    return Candidate(checkout, rel, content, blob, mtime)


def _has_symlink_component(root: Path, rel: str) -> bool:
    """True when ``root/rel`` or any directory between ``root`` and it is a symlink."""
    cur = root
    for part in Path(rel).parts:
        cur = cur / part
        if cur.is_symlink():
            return True
    return False


def _blob_id_at(cwd: Path, rev: str, rel: str) -> str | None:
    return _out(cwd, "rev-parse", "-q", "--verify", f"{rev}:{rel}", check=False) or None


def _index_is_head_or(checkout: Path, rel: str, main_rev: str) -> bool:
    """True when the index copy of ``rel`` equals HEAD's or ``main_rev``'s (nothing unique staged)."""
    if _git(checkout, "diff", "--cached", "--quiet", "--", rel, check=False).returncode == 0:
        return True
    return _git(checkout, "diff", "--cached", "--quiet", main_rev, "--", rel, check=False).returncode == 0


# ── state ─────────────────────────────────────────────────────────────────────────


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / "rig" / "tracked-files"


def _common_dir(repo_root: Path) -> Path:
    """The repo's shared git dir — the same for the primary checkout and every linked worktree.

    ``git rev-parse --git-common-dir`` (no ``--path-format``, so any git works) answers relative
    to the checkout it runs in; an unanswerable query is an error, never a per-checkout fallback
    (that would give each worktree its own state file and lock).
    """
    raw = _out(repo_root, "rev-parse", "--git-common-dir")
    path = Path(raw)
    return (path if path.is_absolute() else repo_root / path).resolve()


def state_path(repo_root: Path) -> Path:
    common = _common_dir(repo_root)
    key = hashlib.sha256(str(common).encode()).hexdigest()[:16]
    return state_dir() / f"{common.parent.name}-{key}.json"


def read_state(repo_root: Path) -> dict[str, Any] | None:
    try:
        return json.loads(state_path(repo_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {**state, "updated": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")}
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")  # the lander and its parent may both write
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _restore_state(path: Path, prev: dict[str, Any] | None) -> None:
    """Put back the state a landing started from (or none): it withdraws its pre-push checkpoint."""
    if prev:
        write_state(path, prev)
    else:
        path.unlink(missing_ok=True)


_IN_FLIGHT = ("pr-open", "shipping")
# the landing's PR outcome is known: a new landing may start
_SETTLED = ("merged", "closed")
# the background lander stopped but the PR may still be open (checked on the forge before landing again)
_STOPPED = ("ship-refused", "timed-out", "ship-stopped")


def in_flight(state: dict[str, Any] | None) -> bool:
    return bool(state) and state.get("status") in _IN_FLIGHT


@contextlib.contextmanager
def _landing_lock(repo_root: Path):
    """One landing at a time per repo.

    Guards the window the state file cannot (two `rig apply commit` runs that both see no landing
    yet would otherwise share the one landing worktree). A kernel lock on a lock file (``flock`` on
    POSIX, ``msvcrt.locking`` on Windows), released when its holder exits, so it never goes stale
    and no pid is ever trusted.
    """
    lock = state_path(repo_root).with_suffix(".lock")
    fd = _take_file_lock(lock)
    if fd is None:
        raise TrackedFilesError(f"no file-lock primitive on this platform for {lock}")
    if fd == -1:
        raise LandingLockBusy("another `rig apply commit` (or a post-merge cleanup) holds this repo's landing lock")
    try:
        yield
    finally:
        os.close(fd)  # releases the lock


def _take_file_lock(lock: Path) -> int | None:
    """An fd holding an exclusive, non-blocking kernel lock on ``lock``; ``-1`` when another process
    holds it; ``None`` when the platform has neither ``fcntl`` nor ``msvcrt``."""
    try:
        import fcntl
    except ImportError:
        fcntl = None  # type: ignore[assignment]
    try:
        import msvcrt
    except ImportError:
        msvcrt = None  # type: ignore[assignment]
    if fcntl is None and msvcrt is None:
        return None
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[union-attr]
    except OSError:  # BlockingIOError (flock) / PermissionError (msvcrt): held elsewhere
        os.close(fd)
        return -1
    return fd


def _lander_lock_path(repo_root: Path) -> Path:
    return state_path(repo_root).with_suffix(".lander")


def _take_lander_lock(lock: Path) -> int | None:
    """Hold ``lock`` for the background lander's lifetime (the kernel drops it when the lander
    exits). ``-1`` when another lander holds it; ``None`` without a file-lock primitive."""
    return _take_file_lock(lock)


def _lander_running(repo_root: Path) -> bool | None:
    """True while a background lander of this repo is alive — whatever the state file says (it may
    have been deleted by hand, or record a pid the OS has since recycled), so a new landing never
    removes a worktree a live lander works in. ``None`` without a file-lock primitive."""
    lock = _lander_lock_path(repo_root)
    if not lock.exists():
        return False
    fd = _take_lander_lock(lock)
    if fd is None:
        return None
    if fd == -1:
        return True
    os.close(fd)
    return False


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if _WINDOWS:
        return _win_pid_alive(pid)  # os.kill(pid, 0) TERMINATES the process on Windows
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists, owned by someone else
    return True


def _win_pid_alive(pid: int) -> bool:
    """Whether ``pid`` runs, asked without touching it (OpenProcess + GetExitCodeProcess)."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: it exists, owned by someone else
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def refresh_state(repo_root: Path, state: dict[str, Any] | None, *, persist: bool = False) -> dict[str, Any] | None:
    """Resolve a landing that is no longer in flight, from local refs only.

    ``merged``: ``origin/<default>`` now carries the landing commit's content for every landed path
    (however it merged — ship, a hand merge, a squash). ``ship-stopped``: the background lander is
    gone without a verdict. A still-open PR stays in flight. ``persist=False`` (previews, status)
    returns the resolved view without writing the state file.
    """
    if not state or state.get("status") in _SETTLED:
        return state
    commit, dflt, paths = state.get("commit"), state.get("default"), state.get("paths") or []
    new_status = None
    if commit and dflt and paths:
        same = _git(repo_root, "diff", "--quiet", commit, f"refs/remotes/origin/{dflt}", "--", *paths, check=False)
        if same.returncode == 0:
            new_status = "merged"
    # a "shipping" state without a pid yet is a lander being started, not a stopped one; with one,
    # the lander lock says whether it still runs (a recorded pid may have been recycled)
    if new_status is None and state.get("status") == "shipping" and state.get("pid"):
        running = _lander_running(repo_root)
        if not (running if running is not None else _pid_alive(state["pid"])):
            new_status = "ship-stopped"
    # ... and one that never recorded a pid long after its spawn died on the way up (an import
    # error, say): nothing ships, so say so instead of "in flight" forever
    spawned_at = state.get("spawned_at")
    if (new_status is None and state.get("status") == "shipping" and not state.get("pid")
            and isinstance(spawned_at, (int, float)) and time.time() - spawned_at > LANDER_START_GRACE_S
            and _lander_running(repo_root) is False):
        new_status = "ship-stopped"
    if new_status is None:
        return state
    state = {**state, "status": new_status}
    if persist:
        write_state(state_path(repo_root), state)
    return state


def _pr_state(repo_root: Path, state: dict[str, Any], *, runner: Runner | None = None) -> str | None:
    """The landing PR's state on the forge: ``open``/``closed``/``merged``, or ``None`` if unknown."""
    number = state.get("pr_number")
    if not number:
        return None
    forge, host = forge_of(repo_root)
    if forge == "github":
        cmd = ["gh", "pr", "view", str(number), "--json", "state", "-q", ".state"]
    else:
        cmd = ["fj"] + (["-H", host] if host else []) + ["pr", "view", str(number)]
    if shutil.which(cmd[0]) is None and runner is None:
        return None
    try:
        proc = _run(cmd, repo_root, timeout=60, runner=runner)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    text = _BIDI_MARKS.sub("", proc.stdout or "")
    if forge == "github":
        word = text.strip().lower()
    else:
        # fj's header: "<title> #N" / "By <user> — Open — +a -b"; the body below is free text
        m = re.search(r"—\s*(open|closed|merged)\b", "\n".join(text.splitlines()[:3]), re.IGNORECASE)
        word = m.group(1).lower() if m else ""
    return word if word in ("open", "closed", "merged") else None


# ── rendering (plan / status) ─────────────────────────────────────────────────────


def render_plan_lines(disc: Discovery, settings: Settings, state: dict[str, Any] | None,
                      diverted_rels: list[str]) -> list[str]:
    """The ``tracked_files`` section of ``rig apply``/``rig status``: the pending merges, in plain words."""
    lines: list[str] = []
    target = f"origin/{disc.default}" if disc.default else "the default branch"
    if in_flight(state):
        assert state is not None
        lines.append(f"landing in flight: {state.get('pr_url') or state.get('branch')} "
                     f"({state.get('status')}; log {state.get('log', '-')})")
    elif state and state.get("status") in (*_STOPPED, "error"):
        last = ((state.get("error") or "").strip().splitlines() or [""])[-1]
        tail = ("; while its PR is open no new one is opened — fix it and re-run its ship, or close it"
                if state.get("status") in _STOPPED else "")
        lines.append(f"last landing {state.get('status')}: {state.get('pr_url') or state.get('branch')} — {last} "
                     f"(log {state.get('log', '-')}){tail}")
    for pl in disc.landings:
        lines.append(f"land {pl.rel} ({len(pl.content.splitlines())} lines) from {len(pl.supporters)} worktree(s) "
                     f"→ PR against {target}" + (", then gated ship" if settings.land == "ship" else " (PR only)"))
    for rel in diverted_rels:
        lines.append(f"land rig's own reconcile of {rel} on {target} instead of writing it here")
    for rel, groups in disc.conflicts.items():
        sizes = ", ".join(str(len(g)) for g in groups)
        lines.append(f"conflict: {rel} has {len(groups)} different edits ({sizes} worktrees) — nothing lands; "
                     "make them agree or commit the one you want")
    for rel, n in disc.below_threshold.items():
        lines.append(f"skip {rel}: only {n} worktree(s) carry this edit "
                     f"(harvest_min_worktrees={settings.harvest_min_worktrees})")
    if disc.landed_copies and settings.normalize_worktrees:
        n = len({c.checkout for c in disc.landed_copies})
        lines.append(f"normalize {n} worktree(s) whose copy already equals {target}")
    if state and state.get("status") in _SETTLED and disc.landed_copies:
        why = state.get("normalize_error") or state.get("normalize_deferred")
        if why:
            lines.append(f"note: the post-merge normalize of {state.get('pr_url') or state.get('branch')} did not "
                         f"finish ({why}); `rig apply commit` normalizes these")
    if (disc.landings or diverted_rels) and settings.land == "ship" and not settings.ship_task_code:
        lines.append("note: tracked_files.ship.task_code is unset — the PR opens, but ship's review/acceptance "
                     "gates need a ticket, so ship is not started")
    lines.extend(f"note: {n}" for n in disc.notes)
    return lines


# ── the `rig apply` / `rig status` entry points (cli.py only wires these in) ─────────


@dataclass
class ApplyContext:
    """What the ``tracked_files`` block would land in one repo, for one apply/status run."""

    settings: Settings
    repo_root: Path
    discovery: Discovery
    state: dict[str, Any] | None
    landing_config: str | None  # the --config the landing worktree's plan rebuild uses
    diverted: list[Any]  # the plan's held-back actions (plan.diverted)
    diverted_pending: list[str]  # repo-relative files among them that are out of sync


def apply_context(plan: Any, config_data: dict[str, Any], repo_root: Path, *,
                  landing_config: str | None) -> ApplyContext | None:
    """Scan the repo for what would land; ``None`` when the block is off. Reads only.

    A git failure while scanning becomes a note in the rendered section, never a failed
    apply/status of everything else.
    """
    settings = settings_from_config(config_data)
    if not settings.enabled:
        return None
    diverted = list(getattr(plan, "diverted", []))
    try:
        disc = discover(repo_root, settings)
    except (TrackedFilesError, OSError, subprocess.SubprocessError) as exc:
        disc = Discovery(repo_root=repo_root, default=None, main_rev=None, notes=[f"scan failed: {exc}"])
    try:
        state = refresh_state(repo_root, read_state(repo_root))
    except (TrackedFilesError, OSError, subprocess.SubprocessError) as exc:
        state = None
        disc.notes.append(f"could not read the landing state: {exc}")
    return ApplyContext(settings, repo_root, disc, state, landing_config, diverted,
                        _safe_pending_reconcile(diverted, repo_root, settings, disc.notes))


def _safe_pending_reconcile(diverted: list[Any], repo_root: Path, settings: Settings, notes: list[str]) -> list[str]:
    """:func:`pending_reconcile`, degrading a failure to a note like the rest of the scan."""
    try:
        return pending_reconcile(diverted, repo_root, settings)
    except (TrackedFilesError, OSError, subprocess.SubprocessError) as exc:
        notes.append(f"could not check the held-back files against the default branch: {exc}")
        return []


def pending_reconcile(diverted: list[Any], repo_root: Path, settings: Settings) -> list[str]:
    """Repo-relative files the held-back actions still have to land on the default branch.

    Pending when drift says the file is out of sync in this checkout, OR when this checkout's copy
    differs from ``origin/<default>``'s: an in-sync copy the default branch lacks (a file an
    earlier rig wrote into every checkout, uncommitted) is exactly what has not landed yet. The
    landing re-derives the file on ``origin/<default>`` and lands nothing when it is already there.
    """
    from types import SimpleNamespace

    from .drift import detect  # lazy: drift pulls in the action runner

    if not diverted:
        return []
    drifting = {os.path.abspath(item.target) for item in detect(SimpleNamespace(actions=diverted)).items}
    dflt = default_branch(repo_root)
    main_rev = f"refs/remotes/origin/{dflt}" if dflt else None
    out: list[str] = []
    for action in diverted:
        for written in action_written_paths(action):
            rel = owned_relpath(written, repo_root, settings.paths)
            if not rel or rel in out:
                continue
            if os.path.abspath(written) in drifting or (main_rev and _differs_from(repo_root, rel, main_rev)):
                out.append(rel)
    return out


def _differs_from(checkout: Path, rel: str, rev: str) -> bool:
    """True when the checkout's working copy of ``rel`` is not ``rev``'s (either may be absent)."""
    here = _candidate(checkout, rel)
    return (here.blob if here else None) != _blob_id_at(checkout, rev, rel)


Emit = Callable[..., None]
Paint = Callable[[str], str]


def print_plan(ctx: ApplyContext, *, preview: bool, emit: Emit, bold: Paint) -> None:
    lines = render_plan_lines(ctx.discovery, ctx.settings, ctx.state, ctx.diverted_pending)
    if not lines:
        return
    emit()
    emit(bold("tracked_files — pending merges to the default branch:"))
    for line in lines:
        emit(f"  {'·' if preview else '→'} {line}")


def commit(ctx: ApplyContext, *, build_plan: Callable[[Path], Any], emit: Emit, ok: Paint, err: Paint,
           dim: Paint) -> int:
    """`rig apply commit`: normalize checkouts that already match the default branch, then land."""
    from .worktree_gc import snapshot_liveness_check

    rc = 0
    disc = ctx.discovery
    try:
        disc = _fetched_discovery(ctx)
    except (TrackedFilesError, OSError, subprocess.SubprocessError) as exc:
        emit(err(f"  tracked_files: re-scan after the fetch failed: {exc}"))
        rc = 1
    # the held-back files are measured against the default branch too: re-measure after the fetch
    pending = (_safe_pending_reconcile(ctx.diverted, ctx.repo_root, ctx.settings, disc.notes)
               if disc is not ctx.discovery else ctx.diverted_pending)
    # the plan of record is the scan this run acts on, not the preview's pre-fetch one
    print_plan(dataclasses.replace(ctx, discovery=disc, diverted_pending=pending), preview=False, emit=emit,
               bold=lambda s: s)
    res: NormalizeResult | None = None
    try:
        # under the landing lock: a background lander's post-merge normalize takes it too
        with _landing_lock(ctx.repo_root):
            res = normalize(disc, ctx.settings, liveness=snapshot_liveness_check())
    except LandingLockBusy:
        emit(dim("  tracked_files: normalize skipped — another landing or post-merge cleanup is running "
                 "(it normalizes the worktrees itself)"))
    except (TrackedFilesError, OSError, subprocess.SubprocessError) as exc:
        emit(err(f"  tracked_files: normalize failed: {exc}"))
        rc = 1
    if res is not None and (res.reset or res.skipped):
        paint = ok if res.reset else dim
        emit(paint(f"  tracked_files: checked out {len(res.reset)} file(s) that already match the default branch"))
        for path, rel, why in res.skipped:
            emit(dim(f"    kept {path}/{rel}: {why}"))
    # gate on the post-fetch scan: the fetch can make an edit landable that the preview did not count
    if not (disc.landings or pending):
        return rc
    result = land(
        ctx.repo_root, ctx.settings,
        apply_diverted=lambda wt: run_diverted(wt, build_plan, ctx.settings),
        progress=lambda msg: emit(dim(f"  tracked_files: {msg}"), flush=True),
    )
    marker = {"error": err("✗"), "in-flight": dim("·")}.get(result.status, ok("✓"))
    emit(f"  {marker} tracked_files: {result.message}")
    return max(rc, 1 if result.status == "error" else 0)


def _fetched_discovery(ctx: ApplyContext) -> Discovery:
    """The scan to normalize against, measured on a freshly fetched ``origin/<default>``.

    The preview scan does not fetch; when the fetch moves the default branch, the checkouts are
    scanned again so a copy is never reset for matching an outdated default branch. A failed
    fetch (offline) keeps the preview scan — the best knowledge there is.
    """
    disc = ctx.discovery
    if not disc.default or not disc.main_rev:
        return disc
    fetched = _git(ctx.repo_root, "fetch", "-q", "origin", disc.default, timeout=_NET_TIMEOUT_S, check=False)
    if fetched.returncode != 0:
        return disc
    if _out(ctx.repo_root, "rev-parse", f"refs/remotes/origin/{disc.default}", check=False) == disc.main_rev:
        return disc
    return discover(ctx.repo_root, ctx.settings)


# ── normalize ─────────────────────────────────────────────────────────────────────


@dataclass
class NormalizeResult:
    reset: list[tuple[Path, str]] = field(default_factory=list)
    skipped: list[tuple[Path, str, str]] = field(default_factory=list)


def normalize(disc: Discovery, settings: Settings, *, liveness: Callable[[Path], bool] | None = None,
              now: float | None = None) -> NormalizeResult:
    """Check out every tracked-owned path whose uncommitted copy already equals ``origin/<default>``.

    Only ``disc.landed_copies`` are touched — by construction their content equals the default
    branch, so nothing unique is lost. Skips locked worktrees, worktrees with a live agent in
    them (``liveness``), and files changed within ``normalize_min_age_minutes``.
    """
    res = NormalizeResult()
    if not settings.normalize_worktrees:
        return res
    now = time.time() if now is None else now
    min_age = settings.normalize_min_age_minutes * 60
    locked = {co.path.resolve() for co in list_checkouts(disc.repo_root) if co.locked}
    for cand in disc.landed_copies:
        if cand.checkout.resolve() in locked:
            res.skipped.append((cand.checkout, cand.rel, "locked"))
            continue
        if liveness is not None and liveness(cand.checkout):
            res.skipped.append((cand.checkout, cand.rel, "a live agent is working here"))
            continue
        if now - cand.mtime < min_age:
            res.skipped.append((cand.checkout, cand.rel, f"changed within {settings.normalize_min_age_minutes} min"))
            continue
        # re-check right before touching it: the scan may be minutes old (a plan ran in between)
        fresh = _candidate(cand.checkout, cand.rel)
        if (fresh is None or fresh.blob != cand.blob or fresh.mtime != cand.mtime
                or not _index_is_head_or(cand.checkout, cand.rel, disc.main_rev or "HEAD")):
            res.skipped.append((cand.checkout, cand.rel, "changed since the scan"))
            continue
        try:
            _restore_to_head(cand.checkout, cand.rel)
        except (TrackedFilesError, subprocess.SubprocessError) as exc:
            # another git process holds that worktree's index.lock, say: this copy waits for the
            # next pass, the others still go
            res.skipped.append((cand.checkout, cand.rel, f"git failed: {str(exc)[-200:]}"))
            continue
        res.reset.append((cand.checkout, cand.rel))
    return res


def _restore_to_head(checkout: Path, rel: str) -> None:
    """Make ``rel`` match ``HEAD`` in both the index and the working tree."""
    if _git(checkout, "cat-file", "-e", f"HEAD:{rel}", check=False).returncode == 0:
        _git(checkout, "restore", "--source=HEAD", "--staged", "--worktree", "--", rel)
        return
    # a new file HEAD does not have: unstage it if it was added, then delete it
    _git(checkout, "rm", "--cached", "--quiet", "--ignore-unmatch", "--", rel)
    target = checkout / rel
    if target.is_file() and not target.is_symlink():
        target.unlink()


# ── landing ───────────────────────────────────────────────────────────────────────


def forge_of(repo_root: Path) -> tuple[str, str | None]:
    """``("github", None)`` for a GitHub origin (github.com, or a ``github.*`` Enterprise host),
    else ``("forgejo", host)``.

    Reads the CONFIGURED ``remote.origin.url`` (not ``git remote get-url``, which applies
    ``insteadOf`` rewrites): the forge is the host the user named, whatever transport git uses.
    """
    url = _out(repo_root, "config", "--get", "remote.origin.url", check=False)
    m = re.match(r"^(?:[a-z+]+://)?(?:[^@/]+@)?([^/:]+)", url)
    host = m.group(1).lower() if m else None
    if host and (host in ("github.com", "www.github.com") or host.startswith("github.")):
        return "github", None
    return "forgejo", host


def _origin_slug(repo_root: Path) -> str | None:
    """``owner/name`` from the configured origin URL (https, ssh or scp form)."""
    url = _out(repo_root, "config", "--get", "remote.origin.url", check=False)
    m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$", url)
    return m.group(1) if m else None


def pr_body(disc: Discovery, diverted_rels: list[str]) -> str:
    rels = sorted({pl.rel for pl in disc.landings} | set(diverted_rels))
    lines = [
        "## What",
        "",
        f"Lands rig-owned committed file(s) once on `{disc.default}`: " + ", ".join(f"`{r}`" for r in rels) + ".",
        "",
        "## Why",
        "",
        "`rig apply` (the `tracked_files` config block) lands these files on the default branch instead "
        "of writing them into each worktree, so the worktrees stay clean.",
        "",
        "## Where it came from",
        "",
    ]
    for pl in disc.landings:
        lines.append(f"- `{pl.rel}`: byte-identical uncommitted edit in {len(pl.supporters)} worktree(s)")
    for rel in diverted_rels:
        lines.append(f"- `{rel}`: rig's own reconcile, run on the default branch")
    lines += ["", "After it merges, `rig apply commit` checks the file out again in every worktree whose copy "
              "equals the default branch."]
    return "\n".join(lines) + "\n"


def open_pr(repo_root: Path, cwd: Path, branch: str, base: str, title: str, body: str,
            *, runner: Runner | None = None) -> tuple[str, int | None]:
    """Open the PR with the forge's CLI. Returns ``(url, number)``."""
    forge, host = forge_of(repo_root)
    if forge == "github":
        cmd = ["gh", "pr", "create", "--base", base, "--head", branch, "--title", title, "--body", body]
    else:
        cmd = ["fj"] + (["-H", host] if host else []) + ["pr", "create", "--base", base, "--head", branch,
                                                          "--body", body, title]
    if shutil.which(cmd[0]) is None and runner is None:
        raise TrackedFilesError(f"`{cmd[0]}` is not installed — cannot open the PR for {branch}")
    proc = _run(cmd, cwd, runner=runner)
    if proc.returncode != 0:
        raise TrackedFilesError(f"{cmd[0]} pr create failed: {(proc.stderr or proc.stdout).strip()[-400:]}")
    text = _BIDI_MARKS.sub("", f"{proc.stdout}\n{proc.stderr}")
    m = re.search(r"https?://\S+/pulls?/(\d+)", text)
    if m:
        return m.group(0).rstrip(").,"), int(m.group(1))
    m = re.search(r"pull request #(\d+)", text, re.IGNORECASE)  # fj: "created pull request #N: <title>"
    if m is None:
        return (text.strip().splitlines() or [branch])[-1], None
    number = int(m.group(1))
    slug = _origin_slug(repo_root)
    url = f"https://{host}/{slug}/pulls/{number}" if forge == "forgejo" and host and slug else f"#{number}"
    return url, number


def commit_message(disc: Discovery, diverted_rels: list[str]) -> str:
    rels = sorted({pl.rel for pl in disc.landings} | set(diverted_rels))
    body = ["", "Landed once on the default branch by `rig apply` (tracked_files) instead of being",
            "written into each worktree.", ""]
    for pl in disc.landings:
        body.append(f"- {pl.rel}: identical uncommitted edit in {len(pl.supporters)} worktree(s)")
    for rel in diverted_rels:
        body.append(f"- {rel}: rig's own reconcile, run on the default branch")
    return "\n".join([f"chore(rig): land {', '.join(rels)} on {disc.default}", *body]) + "\n"


@dataclass
class LandResult:
    status: str  # nothing | in-flight | pr-open | shipping | error
    message: str
    branch: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None
    log: str | None = None


ApplyDiverted = Callable[[Path], list[str]]
Progress = Callable[[str], None]


def run_diverted(landing_wt: Path, build_plan: Callable[[Path], Any], settings: Settings) -> list[str]:
    """Rebuild the plan IN the landing worktree (so it reads the landed ``rig.yaml``) and run only
    its held-back actions there. Returns the repo-relative files they wrote.

    ``build_plan(path)`` is the caller's plan builder (the CLI's config + catalog + plan.build).
    """
    from .actions import run_plan  # lazy: the runner is heavy

    plan = build_plan(landing_wt)
    held = list(getattr(plan, "diverted", []))
    if not held:
        return []
    # a committed symlink (or a symlinked directory) would redirect the write outside the worktree —
    # a relative link can even resolve only from the landing worktree's deeper path
    for action in held:
        for written in action_written_paths(action):
            try:
                rel = Path(os.path.abspath(written)).relative_to(Path(os.path.abspath(landing_wt))).as_posix()
            except ValueError:
                raise TrackedFilesError(f"a held-back action writes outside the landing worktree: {written}") from None
            if _has_symlink_component(landing_wt, rel):
                raise TrackedFilesError(f"{rel} (or a directory above it) is a symlink — refusing to write through it")
    plan.actions, plan.diverted = held, []
    report = run_plan(plan)
    if report.errors:
        raise TrackedFilesError(
            "rig's own reconcile failed in the landing worktree: " + "; ".join(r.detail for r in report.errors)
        )
    rels = (owned_relpath(p, landing_wt, settings.paths) for a in held for p in action_written_paths(a))
    return sorted({rel for rel in rels if rel})


def land(repo_root: Path, settings: Settings, *, apply_diverted: ApplyDiverted | None = None,
         runner: Runner | None = None, spawn_ship: bool = True, progress: Progress | None = None) -> LandResult:
    """Fetch, re-discover, and land the harvested (+ diverted) content in one batch.

    ``apply_diverted(landing_worktree)`` runs rig's own tracked-owned reconcile actions against
    the landing worktree and returns the repo-relative paths they wrote. Never raises: every
    failure comes back as ``status="error"``.
    """
    try:
        with _landing_lock(repo_root):
            return _land(repo_root, settings, apply_diverted=apply_diverted, runner=runner, spawn_ship=spawn_ship,
                         progress=progress or (lambda msg: None))
    except LandingLockBusy as exc:
        # another run is landing right now: the same outcome as a landing in flight, not a failure
        return LandResult("in-flight", str(exc))
    except Exception as exc:  # noqa: BLE001 — the contract: a landing failure is a result, never a traceback
        return LandResult("error", str(exc) or type(exc).__name__)


def _blocking_landing(repo_root: Path, prev: dict[str, Any] | None, *, runner: Runner | None) -> str | None:
    """Why an earlier landing still blocks a new one, or ``None``.

    Asks the forge whether that PR is still open, so a PR closed without merging (or merged by
    hand) never pins the repo. When the answer is unknown — the forge CLI failed, or the PR number
    was never read — it BLOCKS: a duplicate PR is the failure this feature exists to prevent. Only
    a failed landing that never opened a PR, or a settled one, lets a new landing start.
    """
    if prev and prev.get("status") in (*_IN_FLIGHT, *_SETTLED):
        # its background lander still runs (it may be mid-retry, or cleaning up after the merge):
        # the lander lock answers that; the recorded pid only where flock is unavailable
        running = _lander_running(repo_root)
        if running is None:
            running = _pid_alive(prev.get("pid"))
        if running:
            return (f"the background lander (pid {prev.get('pid')}) of {prev.get('pr_url') or prev.get('branch')} "
                    f"is still running (log {prev.get('log', '-')})")
    if not prev or prev.get("status") in _SETTLED:
        return None
    if prev.get("status") == "error" and not prev.get("pr_number") and not prev.get("pushed"):
        return None  # it never opened a PR, and left no branch on the forge
    where = f"{prev.get('pr_url') or prev.get('branch')} ({prev.get('status')}; log {prev.get('log', '-')})"
    if prev.get("status") == "error" and prev.get("pushed") and not prev.get("pr_number"):
        return (f"the earlier landing pushed {prev.get('branch')} but opened no PR — open its PR, or delete that "
                f"branch and {state_path(repo_root)}, by hand; not opening a second one")
    if not prev.get("pr_number"):
        return (f"the earlier landing {where} has no readable PR number — not opening a second PR; close that PR "
                f"(or delete {state_path(repo_root)}) to start over")
    pr = _pr_state(repo_root, prev, runner=runner)
    if pr in _SETTLED:
        write_state(state_path(repo_root), {**prev, "status": pr})
        return None
    if pr == "open":
        return f"the landing PR {where} is still open"
    return (f"could not read the state of the landing PR {where} from the forge — not opening a second PR; "
            "re-run once the forge CLI works")


def _land(repo_root: Path, settings: Settings, *, apply_diverted: ApplyDiverted | None, runner: Runner | None,
          spawn_ship: bool, progress: Progress) -> LandResult:
    spath = state_path(repo_root)
    dflt = default_branch(repo_root)
    if dflt is None:
        return LandResult("nothing", "no origin default branch")
    _git(repo_root, "fetch", "-q", "origin", dflt, timeout=_NET_TIMEOUT_S)
    prev = refresh_state(repo_root, read_state(repo_root), persist=True)
    blocking = _blocking_landing(repo_root, prev, runner=runner)
    if blocking is not None:
        assert prev is not None
        return LandResult("in-flight", blocking, branch=prev.get("branch"), pr_url=prev.get("pr_url"),
                          log=prev.get("log"))
    if _lander_running(repo_root):
        # the state file says nothing is in flight (deleted by hand?) yet a lander still works in
        # the landing worktree: never pull that worktree out from under it
        return LandResult("in-flight", "a background lander of an earlier landing is still running "
                          f"(lock {_lander_lock_path(repo_root)}); re-run once it finishes")
    disc = discover(repo_root, settings)
    if disc.default is None or disc.main_rev is None:
        return LandResult("nothing", "; ".join(disc.notes) or "no default branch")
    forge_cli = "gh" if forge_of(repo_root)[0] == "github" else "fj"
    if runner is None and shutil.which(forge_cli) is None:
        # checked before the review and the push: without it no PR can open, so nothing is spent
        return LandResult("error", f"`{forge_cli}` is not installed — cannot open the landing PR; nothing was pushed")

    from . import worktree as _wt  # lazy: worktree imports the action runner

    anchor = primary_checkout(repo_root)
    wt_path = _wt.worktree_path(anchor, LANDING_WORKTREE_NAME)
    on = ""
    if wt_path.exists():
        # rig's own leftover sits on a rig/tracked-files-* branch; anything else at this path is
        # someone's work, which the discard below would destroy
        on = (_out(wt_path, "branch", "--show-current", check=False)
              if (wt_path / ".git").is_file() else "")
        if not on.startswith(BRANCH_PREFIX):
            return LandResult("error", f"{wt_path} holds {'a worktree on ' + on if on else 'something'} rig did not "
                              "create — move it away; tracked_files lands through that path")
        if Path.cwd().resolve().is_relative_to(wt_path.resolve()):
            return LandResult("error", f"this run sits inside the leftover landing worktree {wt_path}, which a "
                              "landing replaces — run `rig apply commit` from another checkout")
    # rig's own leftover (locked, possibly registered without a directory) from an earlier landing
    # that is no longer in flight: idempotent unlock + remove + prune, and its LOCAL branch — the
    # one it has checked out and the one the state names (a pushed copy stays on the forge)
    _discard_landing_worktree(repo_root, wt_path, on or (prev or {}).get("branch"))
    if on and (prev or {}).get("branch") not in (None, on):
        _git(repo_root, "branch", "-D", prev["branch"], check=False)
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d%H%M%S")
    branch = f"{BRANCH_PREFIX}{disc.content_key()}-{stamp}{os.urandom(2).hex()}"
    with _scrubbed_process_env():
        created = _wt.create(anchor, LANDING_WORKTREE_NAME, branch=branch, base_ref=disc.main_rev)
    if created.status != "created":
        return LandResult("error", created.message)
    _git(repo_root, "worktree", "lock", "--reason", "rig apply: tracked_files landing", str(wt_path), check=False)
    pushed = False
    try:
        for pl in disc.landings:
            target = wt_path / pl.rel
            if _has_symlink_component(wt_path, pl.rel):
                raise TrackedFilesError(f"{pl.rel} (or a directory above it) is a symlink on {dflt} — refusing to write through it")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(pl.content)
        try:
            with _scrubbed_process_env():  # the plan builder and runner call git with the inherited env
                diverted_rels = apply_diverted(wt_path) if apply_diverted is not None else []
        except TrackedFilesError:
            raise
        except Exception as exc:  # noqa: BLE001 — a broken landed rig.yaml must not crash the batch
            raise TrackedFilesError(f"rig's own reconcile could not run in the landing worktree: {exc}") from exc
        owned = sorted({pl.rel for pl in disc.landings} | set(diverted_rels))
        if owned:
            _git(wt_path, "add", "-A", "--", *owned)
        if _git(wt_path, "diff", "--cached", "--quiet", check=False).returncode == 0:
            _discard_landing_worktree(repo_root, wt_path, branch)
            here = ""
            if diverted_rels:
                # the default branch is right; the checkout this ran from is what differs
                here = (f"; this checkout's {', '.join(diverted_rels)} differs from it — merge origin/{dflt} "
                        "or check the file out from it")
            return LandResult("nothing", f"nothing to land — origin/{dflt} already carries every tracked-owned "
                              f"edit{here}")
        progress("recording a review of the staged landing diff (review-cli; this can take minutes)…")
        _review_staged(wt_path, settings, runner=runner)
        message = commit_message(disc, diverted_rels)
        _git(wt_path, "commit", "-q", "-F", "-", input_bytes=message.encode())
        head_sha = _out(wt_path, "rev-parse", "HEAD")
        # checkpoint BEFORE the branch leaves the machine: if this run dies between the push and
        # the PR (Ctrl-C, SIGTERM), the next run finds a pushed branch without a PR and opens no
        # second one; every path below overwrites or withdraws this record
        write_state(spath, {"status": "error", "branch": branch, "pushed": True, "worktree": str(wt_path),
                            "error": "interrupted between pushing the landing branch and opening its PR",
                            "repo_root": str(repo_root), "default": dflt})
        try:
            _push(wt_path, branch, runner=runner, upstream=True)
        except (TrackedFilesError, OSError, subprocess.SubprocessError):
            _restore_state(spath, prev)
            raise
        pushed = True
        url, number = open_pr(repo_root, wt_path, branch, dflt, message.splitlines()[0], pr_body(disc, diverted_rels),
                              runner=runner)
    except (TrackedFilesError, OSError, subprocess.SubprocessError) as exc:
        if not pushed:
            # nothing left the machine: drop the landing worktree so a re-run starts clean
            _discard_landing_worktree(repo_root, wt_path, branch)
            return LandResult("error", str(exc))
        # the branch is on the forge but no PR was opened: take the branch back, so a re-run
        # neither leaves it behind nor opens a second PR next to one opened by hand
        try:
            deleted = _run(["git", "push", "-q", "origin", "--delete", branch], wt_path, timeout=_NET_TIMEOUT_S,
                           runner=runner).returncode == 0
        except (OSError, subprocess.SubprocessError):
            deleted = False
        if deleted:
            _restore_state(spath, prev)
            _discard_landing_worktree(repo_root, wt_path, branch)
            return LandResult("error", f"{exc} (the pushed branch {branch} was deleted again; re-run once that works)")
        write_state(spath, {"status": "error", "branch": branch, "pushed": True, "error": str(exc),
                            "worktree": str(wt_path), "repo_root": str(repo_root), "default": dflt})
        return LandResult("error", f"{exc} (branch {branch} is pushed and could not be deleted: open its PR — or "
                          f"delete the branch and {spath} — by hand)", branch=branch)
    log_path = spath.with_suffix(".log")
    state: dict[str, Any] = {
        "status": "pr-open", "branch": branch, "commit": head_sha, "pr_url": url, "pr_number": number, "default": dflt,
        "worktree": str(wt_path), "repo_root": str(repo_root), "paths": owned, "log": str(log_path),
        "supporters": {pl.rel: [str(p) for p in pl.supporters] for pl in disc.landings},
        "settings": asdict(settings),
    }
    if settings.land == "ship" and settings.ship_task_code and number is not None and spawn_ship:
        state.update(status="shipping", spawned_at=time.time())
        write_state(spath, state)  # the lander reads this file first thing, then records its own pid
        try:
            _spawn_ship_loop(spath, log_path)
        except OSError as exc:
            state.update(status="ship-refused", error=f"could not start the background lander: {exc}")
            write_state(spath, state)
            return LandResult("pr-open", f"opened {url}, but the gated ship could not start: {exc}",
                              branch=branch, pr_url=url, pr_number=number, log=str(log_path))
        return LandResult("shipping", f"opened {url}; gated ship started in the background (log {log_path})",
                          branch=branch, pr_url=url, pr_number=number, log=str(log_path))
    write_state(spath, state)
    if settings.land == "pr" or not spawn_ship:
        why = ""
    elif not settings.ship_task_code:
        why = " — set tracked_files.ship.task_code so rig can start the gated ship"
    else:  # number is None: the forge CLI's output named no PR number
        why = " — could not read the PR number from the forge CLI, so ship was not started; run `gh ship` on it by hand"
    return LandResult("pr-open", f"opened {url}{why}", branch=branch, pr_url=url, pr_number=number)


def _review_staged(wt_path: Path, settings: Settings, *, runner: Runner | None = None) -> None:
    """Record a review of the staged landing diff when review-cli is installed.

    Ship's review-quorum gate reads these iterations (keyed by the ticket code); the ecosystem's
    review-gate commit hook reads the stamp. Without review-cli there is nothing to record — the
    commit hook (if any) and ship will say so.
    """
    if shutil.which("review") is None and runner is None:
        return
    cmd = ["review", "diff", "--staged"]
    if settings.ship_task_code:
        cmd += ["--task", settings.ship_task_code]
    if settings.review_models:
        cmd += ["-m", settings.review_models]
    proc = _run(cmd, wt_path, timeout=3600, runner=runner)
    if proc.returncode != 0:
        raise TrackedFilesError(f"review diff --staged failed (exit {proc.returncode}): "
                                f"{(proc.stderr or proc.stdout).strip()[-400:]}")


def _discard_landing_worktree(repo_root: Path, wt_path: Path, branch: str | None) -> None:
    _git(repo_root, "worktree", "unlock", str(wt_path), check=False)
    _git(repo_root, "worktree", "remove", "--force", str(wt_path), check=False)
    _git(repo_root, "worktree", "prune", check=False)
    if branch:
        _git(repo_root, "branch", "-D", branch, check=False)


def _spawn_ship_loop(spath: Path, log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # a symlinked bin/rig finds riglib through sys.path, not site-packages: hand the child the
    # same package root so `python -m riglib.tracked_files` resolves the SAME rig
    pkg_root = str(Path(__file__).resolve().parent.parent)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in (pkg_root, os.environ.get("PYTHONPATH")) if p)}
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "riglib.tracked_files", "ship-loop", str(spath)],
            stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True,
            cwd=str(spath.parent), env=env,
        )
    return proc.pid


# ── the background lander: gated ship with retries ────────────────────────────────


def ship_command(state: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    """``gh ship <pr> [--task-config P] [args…]`` plus the ticket env ship's gates read."""
    s = state.get("settings") or {}
    cmd = [*shlex.split(s.get("ship_command") or DEFAULT_SHIP_COMMAND), str(state["pr_number"])]
    if s.get("ship_task_config"):
        cmd += ["--task-config", os.path.expanduser(s["ship_task_config"])]
    cmd += list(s.get("ship_args") or ())
    return cmd, {"REVIEW_TASK_CODE": s.get("ship_task_code") or ""}


def ship_loop(spath: Path, *, runner: Runner | None = None, sleep: Callable[[float], None] = time.sleep,
              deadline_s: int = SHIP_DEADLINE_S, retry_s: int = SHIP_RETRY_S,
              liveness_factory: Callable[[], Callable[[Path], bool]] | None = None) -> str:
    """Run the gated ship until it merges, refuses for good, or the deadline passes.

    Retries only refusals that clear by themselves (pending CI, the review-dwell window, an
    unreadable CI status) or by updating the branch (BEHIND: merge the default branch in and push
    — never a force-push). On merge: drop the landing worktree and normalize every worktree, with
    a liveness snapshot taken at that moment (``liveness_factory``), not at start.

    Holds the repo's lander lock throughout (see :func:`_lander_running`) and records its own pid,
    so the spawning `rig apply commit` never writes the state file after the spawn.
    """
    state = json.loads(spath.read_text(encoding="utf-8"))
    lock = _lander_lock_path(Path(state["repo_root"]))
    fd = _take_lander_lock(lock)
    for _attempt in range(25):  # a `rig apply commit` probing the lock holds it for an instant
        if fd != -1:
            break
        sleep(0.2)
        fd = _take_lander_lock(lock)
    if fd == -1:
        print("[tracked_files] another lander of this repo is running — stopping", flush=True)
        return "superseded"
    try:
        return _ship_loop(spath, state, runner=runner, sleep=sleep, deadline_s=deadline_s, retry_s=retry_s,
                          liveness_factory=liveness_factory)
    finally:
        if fd is not None:
            os.close(fd)


def _ship_loop(spath: Path, state: dict[str, Any], *, runner: Runner | None, sleep: Callable[[float], None],
               deadline_s: int, retry_s: int, liveness_factory: Callable[[], Callable[[Path], bool]] | None) -> str:
    wt = Path(state["worktree"])
    repo_root = Path(state["repo_root"])
    started = time.time()
    attempt = 0

    def _still_ours() -> bool:
        """The state file still describes THIS landing (no newer landing took the repo over)."""
        try:
            return json.loads(spath.read_text(encoding="utf-8")).get("branch") == state["branch"]
        except (OSError, ValueError):
            return False

    def _finish(status: str, *, final: bool = True, **extra: Any) -> str:
        """Record ``status``; a FINAL write drops the pid (this lander is done), so a recycled pid
        can never pin later landings."""
        state.update(status=status, **extra)
        if final:
            state["pid"] = None
        if _still_ours():
            write_state(spath, state)
        return status

    # the lander records its own pid: the spawning run writes nothing after the spawn, so it can
    # never overwrite a verdict a fast lander already recorded
    state["pid"] = os.getpid()
    if _still_ours():
        write_state(spath, state)
    while True:
        if not _still_ours():
            print("[tracked_files] a newer landing owns this repo's state — stopping", flush=True)
            return "superseded"
        attempt += 1
        try:
            cmd, env = ship_command(state)
        except (ValueError, KeyError) as exc:  # e.g. an unbalanced quote in ship.command
            return _finish("ship-refused", error=f"could not build the ship command: {exc}")
        print(f"[tracked_files] attempt {attempt}: {shlex.join(cmd)}", flush=True)
        try:
            proc = _run(cmd, wt, timeout=3600, env=env, runner=runner)
            out = f"{proc.stdout}\n{proc.stderr}"
            ok = proc.returncode == 0
        except subprocess.TimeoutExpired:
            ok, out = False, "could not read CI status: ship ran past 3600s"  # retried like pending CI
        except OSError as exc:
            return _finish("ship-refused", error=f"could not run {cmd[0]}: {exc}")
        print(out.strip()[-4000:], flush=True)
        if ok:
            _finish("merged", final=False, merged_at=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"))
            if _still_ours():  # never tear down a newer landing's worktree
                _after_merge(repo_root, state, liveness_factory=liveness_factory)
            _finish("merged")  # writes only while the state file is still ours
            return "merged"
        if not _RETRYABLE_SHIP.search(out):
            return _finish("ship-refused", error=out.strip()[-1500:])
        if time.time() - started > deadline_s:
            return _finish("timed-out", error=out.strip()[-1500:])
        if _BEHIND_SHIP.search(out):
            try:
                _update_branch(wt, state, runner=runner)
            except TrackedFilesError as exc:
                return _finish("ship-refused", error=f"could not update the branch: {exc}")
        state.update(attempts=attempt, last_refusal=out.strip()[-600:])
        if _still_ours():
            write_state(spath, state)
        sleep(retry_s)


def _update_branch(wt: Path, state: dict[str, Any], *, runner: Runner | None = None) -> None:
    dflt = state["default"]
    _git(wt, "fetch", "-q", "origin", dflt, timeout=_NET_TIMEOUT_S)
    merge = _git(wt, "merge", "-q", "--no-edit", f"origin/{dflt}", check=False)
    if merge.returncode != 0:
        _git(wt, "merge", "--abort", check=False)
        raise TrackedFilesError(f"merging origin/{dflt} into {state['branch']} conflicts")
    _push(wt, state["branch"], runner=runner)


def _after_merge(repo_root: Path, state: dict[str, Any], *,
                 liveness_factory: Callable[[], Callable[[Path], bool]] | None) -> None:
    """Drop the landing worktree and normalize — under the same per-repo lock as a landing, so a new
    `rig apply commit` cannot create its landing worktree while this one is torn down. If the lock
    is busy, the cleanup is left to that run (its own normalize pass covers the worktrees)."""
    raw = state.get("settings") or {}
    known = set(Settings.__dataclass_fields__)
    try:
        # tolerate a rig upgraded between landing and merge: keep only fields this rig knows
        s = Settings(**{k: tuple(v) if isinstance(v, list) else v for k, v in raw.items() if k in known})
    except (TypeError, ValueError) as exc:
        state["normalize_error"] = f"unreadable landing settings: {exc}"
        return
    try:
        with _landing_lock(repo_root):
            _git(repo_root, "fetch", "-q", "origin", state["default"], timeout=_NET_TIMEOUT_S)
            _discard_landing_worktree(repo_root, Path(state["worktree"]), state["branch"])
            liveness = liveness_factory() if liveness_factory is not None else None
            res = normalize(discover(repo_root, s), s, liveness=liveness)
        state["normalized"] = [f"{p}:{rel}" for p, rel in res.reset]
        state["normalize_skipped"] = [f"{p}:{rel} ({why})" for p, rel, why in res.skipped]
    except LandingLockBusy:
        state["normalize_deferred"] = "another landing holds the lock; its own normalize pass covers the worktrees"
    except (TrackedFilesError, OSError, subprocess.SubprocessError) as exc:
        state["normalize_error"] = str(exc)


def main(argv: list[str] | None = None) -> int:
    """Internal entry for the detached background lander (not a user-facing verb)."""
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 2 and argv[0] == "ship-loop":
        from .worktree_gc import snapshot_liveness_check

        outcome = ship_loop(Path(argv[1]), liveness_factory=snapshot_liveness_check)
        print(f"[tracked_files] {outcome}", flush=True)
        return 0 if outcome == "merged" else 1
    print("usage: python -m riglib.tracked_files ship-loop <state.json>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
