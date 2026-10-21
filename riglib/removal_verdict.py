"""Landed-removal verdict — "is deleting this worktree / local branch / remote branch guaranteed to
lose nothing?"

ONE function family answers that for every entry point (``rig worktree remove``/``verdict``, the
PreToolUse removal guard in :mod:`riglib.removal_guard`): the answer is SAFE only when every
condition below holds, and UNSAFE — with machine-readable codes AND human text — on anything else.
Every error, timeout, missing tool and ambiguity resolves to UNSAFE (fail CLOSED); nothing is
assumed, each proof is run.

**Clean (worktree only).** ``git status --porcelain --untracked-files=all`` is empty (staged,
unstaged and untracked all count; ignored-only files are allowed), no merge/rebase/cherry-pick/
revert/bisect is in progress, and no live process has the worktree (or a subdirectory) as its cwd.
Never the primary checkout, never the default branch, never a branch checked out in another
worktree. Two things git ignores yet that are not disposable also block: another registered worktree
living INSIDE the target (rig ignores ``.worktrees/`` in every worktree) and an ignored directory
that is itself a git repository. Any OTHER ignored file (an ``.env``, a build artifact) is NOT
protected — "ignored-only files are allowed" is a deliberate rule, and ``git worktree remove
--force`` deletes them.

**Landed.** Every commit unique to the branch/HEAD is on ``origin/<default>`` (the default branch is
RESOLVED from ``refs/remotes/origin/HEAD``, never hardcoded) or is still held by a branch that
survives the removal. That means ANY of, tried in this order, each proven rather than assumed:

1. ``ancestor`` — the tip is an ancestor of ``origin/<default>``;
2. ``pr-merged`` — the forge has a MERGED (not merely closed) PR for the head branch whose base is
   the default branch, whose merge/squash commit is an ancestor of ``origin/<default>``, AND the
   tip equals the PR's recorded head oid or is an ancestor of it — a commit made AFTER the PR head
   is therefore NOT covered;
3. ``content-noop`` / ``cherry`` — ``git merge-tree --write-tree origin/<default> <tip>`` yields
   the very tree ``origin/<default>`` already has (merging would change nothing), or ``git cherry``
   shows only ``-`` lines (every commit has a patch-id twin upstream — a rebase merge; refused when
   the branch contains merge commits, which ``git cherry`` skips);
4. ``preserved`` — the tip is reachable from ANOTHER branch of the same remote (``origin`` for a
   worktree or local branch) that the removal leaves in place: a PR closed as superseded, whose
   commits were pushed to an ``archive/*`` or follow-up branch, loses nothing when its own branch
   goes. The holder must still exist on the remote at the very commit the local tracking ref shows
   (``git ls-remote``), so a stale tracking ref of a branch that was deleted or moved is never
   trusted, and an unreachable remote proves nothing. It is NEVER the target's own branch, a
   same-named branch on the remote, the remote branch the target is configured to track, or — for a
   remote deletion — any branch the same ``git push``
   also deletes: deleting ``a`` and ``b`` together cannot be vouched for by ``a`` and ``b`` (a local
   branch or worktree removal touches no remote ref, so every remote holder stays). The verdict
   names the holder in ``notes``.

Whether the target's OWN branch was pushed is irrelevant (the branch ref is what deletion removes;
a same-named or tracked remote branch never counts) and a stash or the reflog is never relied on. A forge
outage only removes proof 2, an unreachable remote only proof 4; proofs 1 and 3 are local.

A caller about to mutate re-checks the tip and cleanliness it was promised with
(:func:`changed_since`): verdict and delete are separate steps, so this narrows — never closes — the
window in which a concurrent writer adds a file or a commit.

The forge and the process scanner are injectable so the whole thing is testable against real git
with no network. This module is read-only with respect to the repository: the only thing it may
write is remote-tracking refs / FETCH_HEAD when asked to refresh (``fetch=True``) or to bring in a
remote branch's tip object, plus unreachable tree objects from ``git merge-tree --write-tree``.

Known limits (deliberate): a process whose cwd cannot be read although it belongs to this user
(``PermissionError`` on ``/proc/<pid>/cwd``, e.g. a non-dumpable sandboxed helper) is skipped, not
treated as "unknown" — failing closed on it would block every removal on a normal desktop; a
process of ANOTHER user is never inspected. The forge's PR list is capped by the shared gc fetch
(``worktree_gc._GH_PAGE_LIMIT``); a PR older than that simply reads as "no PR" and falls through to
the local proofs. Landed content that a later upstream commit deliberately reverted still counts as
landed — the question asked is whether the work reached the default branch, not whether it stayed.
Proof 4 is judged per command: two sessions that each delete the only other holder of the same
commits at the same instant can both pass, and a holder deleted later is judged on its own (it is
then the target, and its commits must be landed or held elsewhere). A holder is trusted at the
moment of the check: a LATER non-deleting rewrite of it (a force-push, a rebase) is not a removal
and this guard never sees it, which is why the rescue names a fresh ``archive/<name>-<sha>`` branch
rather than a working branch. Proof 4 costs one ``git ls-remote`` per target that has a candidate
holder, so a very large batch can exhaust the hook's 120 s budget (then it is denied, fail closed):
split it.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from . import worktree_gc

LANDING_REMOTE = "origin"
PRESERVED = "preserved"  # the `landed_by` of proof 4
FAST_FORWARD = "fast-forward"  # overwrite only: the old tip is an ancestor of the new start
NEW_BRANCH = "new-branch"  # overwrite only: the branch does not exist yet
_GIT_TIMEOUT_S = 30
_NET_TIMEOUT_S = 30
_PS_TIMEOUT_S = 10
_OID_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")
_BAD_BRANCH_CHARS = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")

# `git rev-parse --git-path <marker>` for each in-progress operation a removal would destroy.
_IN_PROGRESS_MARKERS = (
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "REBASE_HEAD",
    "BISECT_LOG",
    "BISECT_START",
    "rebase-merge",
    "rebase-apply",
    "sequencer",
)


TargetKind = Literal["worktree", "local-branch", "remote-branch"]


class ForgeUnavailable(Exception):
    """The forge PR list could not be fetched or trusted (missing CLI, auth, network, timeout)."""


@dataclass(frozen=True)
class PrRecord:
    """One same-repository pull request as the verdict needs it. ``state`` is ``OPEN``/``MERGED``/
    ``CLOSED``; oids are full hex object names (``None`` when the forge did not report one)."""

    number: int
    state: str
    head_ref: str
    head_oid: str | None
    base_ref: str | None
    merge_commit_oid: str | None
    url: str = ""


ForgeLookup = Callable[[Path], list[PrRecord]]
ProcessCwds = Callable[[], "list[Path] | None"]


@dataclass(frozen=True)
class Reason:
    code: str
    message: str


@dataclass(frozen=True)
class Verdict:
    """SAFE/UNSAFE for one removal target. ``reasons`` explains an UNSAFE (empty when SAFE);
    ``landed_by`` names the proof that held; ``notes`` carries non-fatal observations (a failed
    refresh fetch, a forge outage that was routed around)."""

    kind: TargetKind
    target: str
    safe: bool
    reasons: tuple[Reason, ...] = ()
    landed_by: str | None = None
    notes: tuple[str, ...] = ()
    tip: str | None = None  # the commit judged (worktree HEAD / branch tip / remote tip), when known
    overwrite: bool = False  # the branch is being RE-POINTED (git branch -f/-M/-C, checkout -B), not deleted

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "target": self.target,
            "safe": self.safe,
            "verdict": "SAFE" if self.safe else "UNSAFE",
            "tip": self.tip,
            "landed_by": self.landed_by,
            "reasons": [{"code": r.code, "message": r.message} for r in self.reasons],
            "notes": list(self.notes),
        }

    def render(self) -> str:
        verb = "overwrite" if self.overwrite else "remove"
        head = f"{'SAFE' if self.safe else 'UNSAFE'} to {verb} {self.kind} {self.target}"
        lines = [head]
        if self.safe and self.landed_by == PRESERVED:
            lines.append("  its commits are held by another remote branch that stays (see the note)")
        elif self.safe and self.landed_by == FAST_FORWARD:
            lines.append("  the new start contains every commit the branch has now (a fast-forward)")
        elif self.safe and self.landed_by == NEW_BRANCH:
            lines.append("  the branch does not exist yet, so there is nothing to overwrite")
        elif self.safe and self.landed_by:
            lines.append(f"  landed on the default branch via: {self.landed_by}")
        lines.extend(f"  - [{r.code}] {r.message}" for r in self.reasons)
        lines.extend(f"  note: {n}" for n in self.notes)
        return "\n".join(lines)


def _unsafe(
    kind: TargetKind,
    target: str | Path,
    reasons: list[Reason],
    notes: list[str],
    tip: str | None = None,
    overwrite: bool = False,
) -> Verdict:
    return Verdict(kind, str(target), False, tuple(reasons), None, tuple(notes), tip, overwrite)


def rescue_hint(kind: TargetKind, target: str, repo_name: str = "<repo>", tip: str | None = None) -> str:
    """The sanctioned rescue for work you really mean to drop — ONE wording for every refusal. The
    route it names is the ``preserved`` proof: the commits must sit on a branch of the SAME remote
    that the removal leaves in place (a remote-branch target is ``<remote>/<branch>``). ``tip`` is the
    commit the verdict judged: it makes the archive name unique per commit (a reused branch name must
    never tempt a force-push over an earlier archive) and, for a remote branch, is pushed by sha since
    the tracking ref can be stale or absent."""
    keep = "and retry (a commit held by another remote branch that stays counts as preserved)"
    suffix = tip[:8] if tip else "<ts>"
    if kind == "worktree":
        name = Path(target).name or "wt"
        return (
            f"rescue its content first, running both commands from inside {target}: "
            f"`git add -A && git diff --binary HEAD > ~/.cache/{repo_name}-recovery/{name}-<ts>.patch`, "
            "then push the commits to a branch that is not being deleted — "
            f"`git push {LANDING_REMOTE} HEAD:refs/heads/archive/{name}-{suffix}` — {keep} once the tree is clean "
            "(commit what the patch holds, or discard it)"
        )
    remote, _, branch = target.partition("/") if kind == "remote-branch" else (LANDING_REMOTE, "", target)
    source = tip if kind == "remote-branch" and tip else target
    return (
        "push the commits to a branch that is not being deleted — "
        f"`git push {remote} {source}:refs/heads/archive/{branch or target}-{suffix}` — {keep}"
    )


# ── git plumbing ─────────────────────────────────────────────────────────────────
class GitFailure(Exception):
    """git could not be run, or timed out — never collapsed into a yes/no answer."""


_VERDICT_ERRORS = (GitFailure, worktree_gc.WorktreeGcError, OSError)  # every "could not decide" → UNSAFE


def _git_env() -> dict[str, str]:
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}


def run_git(cwd: Path, *args: str, timeout: int = _GIT_TIMEOUT_S) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args], capture_output=True, timeout=timeout, env=_git_env()
        )
    except subprocess.TimeoutExpired as exc:
        raise GitFailure(f"`git {' '.join(args[:2])}` timed out after {timeout}s") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitFailure(f"could not run git: {exc}") from exc


def _out(res: subprocess.CompletedProcess[bytes]) -> str:
    return os.fsdecode(res.stdout).strip()


def _err(res: subprocess.CompletedProcess[bytes]) -> str:
    return (os.fsdecode(res.stderr).strip() or os.fsdecode(res.stdout).strip() or f"exit {res.returncode}")[:300]


def _is_ancestor(repo: Path, maybe_ancestor: str, descendant: str) -> bool | None:
    """True/False for a definitive answer; ``None`` when git could not decide (missing object,
    error) — callers treat ``None`` as "not proven"."""
    res = run_git(repo, "merge-base", "--is-ancestor", maybe_ancestor, descendant)
    if res.returncode == 0:
        return True
    if res.returncode == 1:
        return False
    return None


def is_git_repo(path: Path) -> bool:
    """True when ``path`` is a directory git can run in (inside a work tree or a bare repo)."""
    try:
        return path.is_dir() and run_git(path, "rev-parse", "--git-dir").returncode == 0
    except GitFailure:
        return False


def local_branch_exists(repo: Path, name: str) -> bool:
    """True when ``refs/heads/<name>`` exists (``name`` must already be a valid branch name)."""
    if invalid_branch_name_reason(name) is not None:
        return False
    try:
        return run_git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{name}").returncode == 0
    except GitFailure:
        return False


def same_path(a: str | Path, b: str | Path) -> bool:
    """True when ``a`` and ``b`` name the same location: equal after resolving symlinks, or — only
    when both exist — the very same file (``samefile``, so a case-insensitive filesystem treats
    ``Foo`` and ``foo`` as one while a case-sensitive one never conflates them)."""
    try:
        ra, rb = Path(a).resolve(), Path(b).resolve()
        if ra == rb:
            return True
        return ra.exists() and rb.exists() and os.path.samefile(ra, rb)
    except OSError:
        return False


# ── branch-name validation ───────────────────────────────────────────────────────
def invalid_branch_name_reason(name: str) -> str | None:
    """Why ``name`` must never reach git as a branch name, else ``None``. Stricter than
    ``git check-ref-format``: also rejects option-looking, ``refs/``-prefixed and ``@{…}`` names that
    git itself would happily reinterpret."""
    if not name:
        return "empty branch name"
    if name.startswith("-"):
        return "branch name starts with '-' (looks like an option)"
    if _BAD_BRANCH_CHARS.search(name):
        return "branch name contains whitespace, a control character or one of ~^:?*[\\"
    if name == "@" or "@{" in name or ".." in name or name.endswith(("/", ".", ".lock")) or name.startswith(("/", "refs/")):
        return "branch name is not a plain branch name"
    return None


def _check_ref_format(repo: Path, name: str) -> str | None:
    reason = invalid_branch_name_reason(name)
    if reason is not None:
        return reason
    res = run_git(repo, "check-ref-format", f"refs/heads/{name}")
    return None if res.returncode == 0 else "git rejects it as a ref name"


# ── default branch resolution ────────────────────────────────────────────────────
@dataclass(frozen=True)
class _Default:
    name: str
    ref: str  # refs/remotes/origin/<name>
    sha: str


def _resolve_default(repo: Path) -> tuple[_Default | None, Reason | None]:
    res = run_git(repo, "symbolic-ref", "-q", f"refs/remotes/{LANDING_REMOTE}/HEAD")
    prefix = f"refs/remotes/{LANDING_REMOTE}/"
    target = _out(res)
    if res.returncode != 0 or not target.startswith(prefix) or len(target) == len(prefix):
        return None, Reason(
            "no-default-ref",
            f"cannot resolve the default branch: refs/remotes/{LANDING_REMOTE}/HEAD is missing "
            f"(run `git remote set-head {LANDING_REMOTE} -a`)",
        )
    name = target[len(prefix) :]
    sha_res = run_git(repo, "rev-parse", "--verify", "-q", "--end-of-options", f"{target}^{{commit}}")
    sha = _out(sha_res)
    if sha_res.returncode != 0 or not _OID_RE.match(sha):
        return None, Reason(
            "no-default-ref",
            f"{target} does not exist locally (run `git fetch {LANDING_REMOTE}`) — cannot prove anything landed",
        )
    return _Default(name, target, sha), None


def _refresh_default(repo: Path, default_name: str, notes: list[str]) -> None:
    """Best-effort ``git fetch`` of the default branch. A failure is NOT fatal: the proofs run
    against the refs we have, which can only make a landing look LATER, never earlier."""
    refspec = f"+refs/heads/{default_name}:refs/remotes/{LANDING_REMOTE}/{default_name}"
    try:
        res = run_git(repo, "fetch", "--quiet", "--no-tags", LANDING_REMOTE, refspec, timeout=_NET_TIMEOUT_S)
    except GitFailure as exc:
        notes.append(f"refresh fetch of {LANDING_REMOTE}/{default_name} failed ({exc}); using the refs already present")
        return
    if res.returncode != 0:
        notes.append(
            f"refresh fetch of {LANDING_REMOTE}/{default_name} failed ({_err(res)}); using the refs already present"
        )


def _default_after_refresh(
    repo: Path, fetch: bool, notes: list[str]
) -> tuple[_Default | None, Reason | None]:
    """Resolve the default branch, optionally refreshing ``origin/<default>`` first. Exactly one of
    the pair is ``None``."""
    default, reason = _resolve_default(repo)
    if default is not None and fetch:
        _refresh_default(repo, default.name, notes)
        default, reason = _resolve_default(repo)
    if default is None and reason is None:
        reason = Reason("no-default-ref", "the default branch could not be resolved")
    return default, reason


def refresh_default_ref(repo_root: Path) -> list[str]:
    """Best-effort refresh of ``origin/<default>`` once, for a caller about to run several
    verdicts (so each one need not fetch). Returns the non-fatal notes (e.g. a failed fetch)."""
    notes: list[str] = []
    try:
        default, _ = _resolve_default(repo_root)
        if default is not None:
            _refresh_default(repo_root, default.name, notes)
    except GitFailure as exc:
        notes.append(f"could not refresh {LANDING_REMOTE}/<default>: {exc}")
    return notes


# ── landed proofs ────────────────────────────────────────────────────────────────
def _unique_commit_count(repo: Path, default: _Default, tip: str) -> int | None:
    res = run_git(repo, "rev-list", "--count", f"{default.sha}..{tip}")
    out = _out(res)
    return int(out) if res.returncode == 0 and out.isdigit() else None


def _prove_pr_merged(
    repo: Path, tip: str, branch: str | None, default: _Default, forge: ForgeLookup, notes: list[str]
) -> tuple[str | None, list[str]]:
    """Proof 2. Returns ``("pr-merged", [])`` or ``(None, why_not)``."""
    try:
        records = forge(repo)
    except Exception as exc:  # noqa: BLE001 — ANY forge failure just removes this one proof
        notes.append(f"forge lookup failed ({exc}); relying on local proofs only")
        return None, [f"the forge could not be queried ({exc})"]
    candidates = [r for r in records if (branch is not None and r.head_ref == branch) or (r.head_oid and r.head_oid == tip)]
    if not candidates:
        return None, ["no pull request found for this branch/commit"]
    why: list[str] = []
    for r in candidates:
        label = f"PR #{r.number}"
        if r.state != "MERGED":
            why.append(f"{label} is {r.state}, not merged")
            continue
        if r.base_ref != default.name:
            why.append(f"{label} was merged into {r.base_ref!r}, not the default branch {default.name!r}")
            continue
        if not r.merge_commit_oid or not _OID_RE.match(r.merge_commit_oid):
            why.append(f"{label} has no recorded merge commit")
            continue
        if not r.head_oid or not _OID_RE.match(r.head_oid):
            why.append(f"{label} has no recorded head commit")
            continue
        if _is_ancestor(repo, r.merge_commit_oid, default.sha) is not True:
            why.append(
                f"{label}'s merge commit {r.merge_commit_oid[:12]} is not on {default.ref} "
                f"(run `git fetch {LANDING_REMOTE}` if it merged just now)"
            )
            continue
        if tip != r.head_oid and _is_ancestor(repo, tip, r.head_oid) is not True:
            why.append(
                f"{label} merged head {r.head_oid[:12]}, but the commit {tip[:12]} is not that head or one of its "
                "ancestors — it holds work made after the PR head"
            )
            continue
        return "pr-merged", []
    return None, why


def _prove_content(repo: Path, tip: str, default: _Default) -> tuple[str | None, list[str]]:
    """Proof 3. Returns ``(proof_name_or_None, why_not)``."""
    why: list[str] = []
    mt = run_git(repo, "merge-tree", "--write-tree", default.sha, tip)
    if mt.returncode == 0:
        out_lines = _out(mt).splitlines()
        merged_tree = out_lines[0] if out_lines else ""
        tree_res = run_git(repo, "rev-parse", "--verify", "-q", f"{default.sha}^{{tree}}")
        if _OID_RE.match(merged_tree) and merged_tree == _out(tree_res):
            return "content-noop", []
        why.append("merging it into the default branch would change the default branch's content")
    elif mt.returncode == 1:
        why.append("merging it into the default branch conflicts / is not a no-op")
    else:
        why.append(f"`git merge-tree` could not decide ({_err(mt)})")
    merges = run_git(repo, "rev-list", "--merges", "--count", f"{default.sha}..{tip}")
    if merges.returncode != 0 or _out(merges) != "0":
        # `git cherry` skips merge commits, so it cannot vouch for content only a merge carries
        why.append("the branch contains merge commits, which `git cherry` cannot judge")
        return None, why
    ch = run_git(repo, "cherry", default.sha, tip)
    if ch.returncode == 0:
        lines = [ln for ln in _out(ch).splitlines() if ln.strip()]
        if all(ln.startswith("-") for ln in lines):
            return "cherry", []
        plus = sum(1 for ln in lines if ln.startswith("+"))
        why.append(f"{plus} commit(s) have no equivalent upstream (`git cherry`)")
    else:
        why.append(f"`git cherry` could not decide ({_err(ch)})")
    return None, why


def _own_names(repo: Path, branch: str | None, remote: str) -> frozenset[str]:
    """The ``remote`` branch names that are the LOCAL target's own pushed copy: its name, and the
    branch it is configured to track (where ``git push -u`` put it under a different name). Pushing
    the target itself never counts as a second copy."""
    if branch is None:
        return frozenset()
    names = {branch}
    if _out(run_git(repo, "config", "--get", f"branch.{branch}.remote")) == remote:
        merge = _out(run_git(repo, "config", "--get", f"branch.{branch}.merge"))
        if merge.startswith("refs/heads/"):
            names.add(merge.removeprefix("refs/heads/"))
    return frozenset(names)


def _prove_preserved(
    repo: Path, tip: str, remote: str, own: frozenset[str], removing: frozenset[str], notes: list[str]
) -> tuple[str | None, list[str]]:
    """Proof 4. Returns ``("preserved", [])`` — the holder named in ``notes`` — or ``(None, why_not)``.

    One cheap local call lists the ``<remote>`` tracking refs that contain ``tip``; only then does
    ``git ls-remote`` confirm that a holder still exists on the remote at that very commit, because
    a tracking ref outlives a branch that was deleted or force-moved on the server. A holder is never
    one of ``own`` (the target's own copy), one of ``removing`` (the branches the same ``git push``
    deletes; named in the failure message) or a symbolic alias of another branch, which disappears
    with it."""
    prefix = f"refs/remotes/{remote}/"
    listed = run_git(repo, "for-each-ref", f"--contains={tip}", "--format=%(objectname) %(refname)", prefix.rstrip("/"))
    if listed.returncode != 0:
        return None, [f"could not list the {remote} branches that contain it ({_err(listed)})"]
    holders: dict[str, str] = {}
    also_removed: list[str] = []
    for line in os.fsdecode(listed.stdout).splitlines():
        sha, _, ref = line.partition(" ")
        name = ref.removeprefix(prefix)
        if not ref.startswith(prefix) or name == "HEAD" or name in own or not _OID_RE.match(sha):
            continue
        if name in removing:
            also_removed.append(name)
        else:
            holders[name] = sha
    if not holders:
        gone = f" (the same command also deletes {', '.join(sorted(also_removed))})" if also_removed else ""
        return None, [f"no other {remote} branch holds it{gone}"]
    named = ", ".join(f"{remote}/{name}" for name in sorted(holders))
    ls = run_git(repo, "ls-remote", "--symref", "--heads", remote, timeout=_NET_TIMEOUT_S)
    if ls.returncode != 0:
        return None, [f"{named} cannot be confirmed: `git ls-remote {remote}` failed ({_err(ls)})"]
    live: dict[str, str] = {}
    aliases: set[str] = set()
    for line in os.fsdecode(ls.stdout).splitlines():
        left, _, ref = line.partition("\t")
        if left.startswith("ref: "):
            aliases.add(ref)
        else:
            live[ref] = left
    for name in sorted(holders):
        ref = f"refs/heads/{name}"
        if ref not in aliases and live.get(ref) == holders[name]:
            notes.append(f"the commits are also held by {remote}/{name}, which stays")
            return PRESERVED, []
    return None, [f"{named} hold(s) it only in the local tracking refs; the remote's branch is not at the commit they show (`git fetch {remote} --prune`)"]


def _prove_landed(
    repo: Path,
    tip: str,
    branch: str | None,
    default: _Default,
    forge: ForgeLookup,
    notes: list[str],
    *,
    remote: str,
    own: frozenset[str] = frozenset(),
    removing: frozenset[str] = frozenset(),
) -> tuple[str | None, list[Reason]]:
    """``(proof, [])`` when landed, else ``(None, [not-landed reason])``. Proofs run in the
    documented order and the first that holds wins (so a forge outage or a remote lookup is only
    consulted when the cheap local proofs fail). ``remote`` is where proof 4 looks for holders and
    ``own`` the target's own remote copy and ``removing`` the branch names the same push deletes there
    (see :func:`_prove_preserved`)."""
    try:
        anc = _is_ancestor(repo, tip, default.sha)
    except GitFailure as exc:
        return None, [Reason("not-landed", f"could not check ancestry: {exc}")]
    if anc is True:
        return "ancestor", []
    details: list[str] = []
    if anc is None:
        details.append(f"commit {tip[:12]} could not be compared with {default.ref}")
    try:
        proof, why = _prove_pr_merged(repo, tip, branch, default, forge, notes)
        if proof is not None:
            return proof, []
        details.extend(f"PR proof: {w}" for w in why)
        proof, why = _prove_content(repo, tip, default)
        if proof is not None:
            return proof, []
        details.extend(f"content proof: {w}" for w in why)
        proof, why = _prove_preserved(repo, tip, remote, own, removing, notes)
        if proof is not None:
            return proof, []
        details.extend(f"preserved proof: {w}" for w in why)
    except GitFailure as exc:
        return None, [Reason("not-landed", f"could not run a landing proof: {exc}")]
    try:
        count = _unique_commit_count(repo, default, tip)
    except GitFailure:
        count = None
    unique = "some commits" if count is None else f"{count} commit(s)"
    return None, [
        Reason(
            "not-landed",
            f"{unique} at {tip[:12]} are not proven to be on {default.ref} or held by another {remote} branch "
            "that stays: " + "; ".join(details),
        )
    ]


# ── worktree facts ───────────────────────────────────────────────────────────────
def _worktree_records(repo: Path) -> list[dict[str, str]]:
    return worktree_gc.worktree_records(repo)


def _proc_cwds(uid: int) -> list[Path] | None:
    """Linux: the cwd of every process of ``uid`` from ``/proc``, or ``None`` when untrustworthy."""
    cwds: list[Path] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            cwds.append(Path(os.readlink(entry / "cwd")))
        except (FileNotFoundError, ProcessLookupError):
            continue  # exited mid-scan: by definition not live
        except PermissionError:
            continue  # see the module docstring's "known limits"
        except OSError:
            return None
    return cwds or None  # this very process always has a cwd: an empty answer is a failed scan


def _lsof_cwds(uid: int) -> list[Path] | None:
    """Other POSIX: ``lsof`` for ``uid``'s cwds, or ``None``. A working scan always lists at least
    this very process, so an empty answer — even with exit 1 — is a scanner failure, not "nothing
    is running"."""
    try:
        res = subprocess.run(
            ["lsof", "-a", "-u", str(uid), "-d", "cwd", "-Fn"], capture_output=True, timeout=_PS_TIMEOUT_S
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode not in (0, 1):
        return None
    cwds = [Path(line[1:]) for line in os.fsdecode(res.stdout).split("\n") if line.startswith("n") and len(line) > 1]
    return cwds or None


def all_process_cwds() -> list[Path] | None:
    """The cwd of every process owned by this user, or ``None`` when the scan itself cannot be
    trusted. Linux reads ``/proc/<pid>/cwd``; other POSIX uses ``lsof``."""
    uid = os.getuid()
    return _proc_cwds(uid) if Path("/proc").is_dir() else _lsof_cwds(uid)


def _live_process_in(path: Path, scan: ProcessCwds) -> tuple[bool, bool]:
    """``(live, scan_trusted)``."""
    cwds = scan()
    if cwds is None:
        return False, False
    root = path.resolve()
    for cwd in cwds:
        try:
            cwd.resolve().relative_to(root)
        except (ValueError, OSError):
            continue
        return True, True
    return False, True


def _operations_in_progress(wt: Path) -> list[str]:
    """Names of merge/rebase/cherry-pick/revert/bisect state files present in ``wt``'s git dir."""
    args: list[str] = []
    for marker in _IN_PROGRESS_MARKERS:
        args += ["--git-path", marker]
    res = run_git(wt, "rev-parse", *args)
    paths = os.fsdecode(res.stdout).splitlines()
    if res.returncode != 0 or len(paths) != len(_IN_PROGRESS_MARKERS):
        raise GitFailure(f"git rev-parse --git-path failed in {wt}: {_err(res)}")
    return [m for m, p in zip(_IN_PROGRESS_MARKERS, paths) if (Path(p) if os.path.isabs(p) else wt / p).exists()]


def _dirty_reasons(wt: Path) -> list[Reason]:
    try:
        res = run_git(
            wt, "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none", "--no-renames"
        )
    except GitFailure as exc:
        return [Reason("status-failed", f"`git status` could not run in {wt}: {exc}")]
    if res.returncode != 0:
        return [Reason("status-failed", f"`git status` failed in {wt}: {_err(res)}")]
    lines = [ln for ln in os.fsdecode(res.stdout).splitlines() if ln.strip()]
    if not lines:
        return []
    sample = "; ".join(lines[:5]) + (" …" if len(lines) > 5 else "")
    return [Reason("dirty-worktree", f"{len(lines)} uncommitted path(s) (staged, unstaged or untracked): {sample}")]


def _nested_reasons(wt: Path, records: list[dict[str, str]], index: int) -> list[Reason]:
    """Content `git status` cannot see because it is IGNORED yet is not disposable: another
    registered worktree living inside ``wt`` (rig ignores ``.worktrees/`` in every worktree through
    the common exclude, so a clean-looking tree may hold a whole sibling's uncommitted work), or an
    ignored directory that is itself a git repository."""
    reasons: list[Reason] = []
    root = wt.resolve()
    for other_index, other in enumerate(records):
        if other_index == index or "worktree" not in other:
            continue
        try:
            Path(other["worktree"]).resolve().relative_to(root)
        except (ValueError, OSError):
            continue
        reasons.append(Reason("contains-worktree", f"another worktree lives inside this one: {other['worktree']}"))
    res = run_git(wt, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z")
    if res.returncode != 0:
        return reasons + [Reason("status-failed", f"`git ls-files --ignored` failed in {wt}: {_err(res)}")]
    for raw in res.stdout.split(b"\x00"):
        entry = os.fsdecode(raw)
        if not entry.endswith("/") or _is_disposable_dir(wt / entry.rstrip("/")):
            continue
        nested, incomplete = _find_nested_repository(wt / entry)
        if nested is not None:
            reasons.append(Reason("nested-repository", f"an ignored directory holds its own git repository: {nested}"))
        elif incomplete is not None:
            reasons.append(
                Reason(
                    "nested-repository-unknown",
                    f"could not finish checking the ignored directory {entry} for a nested git repository: "
                    f"{incomplete}. git ignores it, so rig cannot tell whether it holds unsaved work — delete or "
                    "move that directory, then retry",
                )
            )
    return reasons


_NESTED_SCAN_DEPTH = 8
_NESTED_SCAN_BUDGET = 20000
_ALWAYS_DISPOSABLE_DIRS = frozenset(
    {
        "node_modules", ".venv", "venv", "__pycache__", ".tox", ".nox", ".mypy_cache", ".pytest_cache",
        ".ruff_cache", ".next", ".nuxt", ".turbo", ".gradle",
    }
)
_BUILD_MANIFESTS = ("Cargo.toml", "pom.xml")  # a `target/` beside one of these is a build output


def _is_disposable_dir(path: Path) -> bool:
    """Reinstallable / generated trees the nested-repository scan skips. ``target`` is only generated
    when a build manifest sits beside it — a bare ``target/`` can hold anything."""
    if path.name in _ALWAYS_DISPOSABLE_DIRS:
        return True
    return path.name == "target" and any((path.parent / manifest).exists() for manifest in _BUILD_MANIFESTS)


def _find_nested_repository(directory: Path) -> tuple[Path | None, str | None]:
    """``(the .git entry or None, why_incomplete_or_None)`` for an ignored ``directory``. Disposable
    trees (:func:`_is_disposable_dir`) are skipped — a documented limit. Everything else the scan
    could not finish — out of entry budget, deeper than :data:`_NESTED_SCAN_DEPTH`, an unreadable
    directory — is reported as INCOMPLETE with its cause, never as "none found" (unknown is UNSAFE)."""
    budget = _NESTED_SCAN_BUDGET
    incomplete: str | None = None
    stack = [(directory, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    budget -= 1
                    if entry.name == ".git":
                        return Path(entry.path), None
                    if budget < 0:
                        return None, f"too many entries (more than {_NESTED_SCAN_BUDGET})"
                    if not entry.is_dir(follow_symlinks=False) or _is_disposable_dir(Path(entry.path)):
                        continue
                    if depth < _NESTED_SCAN_DEPTH:
                        stack.append((Path(entry.path), depth + 1))
                    else:
                        incomplete = f"nested deeper than {_NESTED_SCAN_DEPTH} levels"
        except OSError as exc:
            return None, f"unreadable directory {current} ({exc.strerror or exc})"
    return None, incomplete


def changed_since(worktree_path: Path, tip: str | None) -> str | None:
    """Why the state a SAFE verdict judged is no longer the state on disk, else ``None``. The
    verdict and the delete are separate steps; a caller about to mutate re-checks the two things a
    concurrent writer can change — the commit checked out and the uncommitted files — which narrows
    (it cannot close) that window. A worktree whose directory is already gone (prunable) has nothing
    on disk to re-check."""
    if not Path(worktree_path).exists():
        return None
    try:
        head = run_git(Path(worktree_path), "rev-parse", "--verify", "-q", "HEAD")
    except GitFailure as exc:
        return f"HEAD could not be re-read ({exc})"
    if head.returncode != 0 or _out(head) != tip:
        return "HEAD moved"
    if _dirty_reasons(Path(worktree_path)):
        return "uncommitted changes appeared"
    return None


# ── public verdicts ──────────────────────────────────────────────────────────────
def worktree_verdict(
    repo_root: Path,
    worktree_path: Path,
    *,
    forge: ForgeLookup | None = None,
    process_cwds: ProcessCwds = all_process_cwds,
    fetch: bool = False,
) -> Verdict:
    """Verdict for removing the linked worktree at ``worktree_path`` (any location git knows)."""
    forge = forge or default_forge_lookup
    kind, target = "worktree", worktree_path
    reasons: list[Reason] = []
    notes: list[str] = []
    head = ""
    try:
        records = _worktree_records(repo_root)
        default, default_reason = _default_after_refresh(repo_root, fetch, notes)
        if default_reason is not None:
            reasons.append(default_reason)

        index = next((i for i, rec in enumerate(records) if same_path(rec.get("worktree", ""), worktree_path)), None)
        if index is None or "bare" in records[index]:
            return _unsafe(kind, target, reasons + [Reason("unknown-worktree", f"{worktree_path} is not a worktree of {repo_root}")], notes)
        if index == 0:
            return _unsafe(kind, target, reasons + [Reason("primary-checkout", f"{worktree_path} is the primary checkout")], notes)
        rec = records[index]
        branch = rec.get("branch", "").removeprefix("refs/heads/") or None
        head = rec.get("HEAD", "")
        if not _OID_RE.match(head):
            reasons.append(Reason("unknown-worktree", f"git reports no HEAD commit for {worktree_path}"))

        if default is not None and branch == default.name:
            reasons.append(Reason("default-branch", f"the worktree has the default branch {branch!r} checked out"))
        for other_index, other in enumerate(records):
            if other_index != index and branch is not None and other.get("branch") == f"refs/heads/{branch}":
                reasons.append(Reason("branch-checked-out", f"branch {branch!r} is also checked out in {other.get('worktree')}"))

        if "prunable" in rec or not Path(worktree_path).exists():
            notes.append("worktree directory is already gone (prunable); only its commits were checked")
        else:
            reasons.extend(_dirty_reasons(Path(worktree_path)))
            reasons.extend(_nested_reasons(Path(worktree_path), records, index))
            ops = _operations_in_progress(Path(worktree_path))
            if ops:
                reasons.append(Reason("operation-in-progress", f"a merge/rebase/cherry-pick/revert/bisect is in progress ({', '.join(ops)})"))
            live, trusted = _live_process_in(Path(worktree_path), process_cwds)
            if not trusted:
                reasons.append(Reason("live-process-unknown", "could not scan running processes for one using this worktree"))
            elif live:
                reasons.append(Reason("live-process", "a running process has this worktree (or a subdirectory) as its cwd"))

        if default is not None and _OID_RE.match(head):
            own = _own_names(repo_root, branch, LANDING_REMOTE)
            proof, why = _prove_landed(repo_root, head, branch, default, forge, notes, remote=LANDING_REMOTE, own=own)
            if proof is None:
                reasons.extend(why)
            elif not reasons:
                return Verdict(kind, str(target), True, (), proof, tuple(notes), head)
    except _VERDICT_ERRORS as exc:
        reasons.append(Reason("verdict-error", f"could not complete the check: {exc}"))
    return _unsafe(kind, target, reasons, notes, head if _OID_RE.match(head) else None)


def branch_verdict(repo_root: Path, branch: str, *, forge: ForgeLookup | None = None, fetch: bool = False) -> Verdict:
    """Verdict for deleting the LOCAL branch ``branch`` (``git branch -D``)."""
    forge = forge or default_forge_lookup
    kind = "local-branch"
    reasons: list[Reason] = []
    notes: list[str] = []
    tip = ""
    try:
        bad = _check_ref_format(repo_root, branch)
        if bad is not None:
            return _unsafe(kind, branch, [Reason("invalid-branch-name", f"{branch!r}: {bad}")], notes)
        default, default_reason = _default_after_refresh(repo_root, fetch, notes)
        if default is None:
            return _unsafe(kind, branch, [default_reason or Reason("no-default-ref", "default branch unresolved")], notes)
        tip_res = run_git(repo_root, "rev-parse", "--verify", "-q", "--end-of-options", f"refs/heads/{branch}^{{commit}}")
        tip = _out(tip_res)
        if tip_res.returncode != 0 or not _OID_RE.match(tip):
            return _unsafe(kind, branch, [Reason("unknown-branch", f"no local branch {branch!r}")], notes)
        if branch == default.name:
            reasons.append(Reason("default-branch", f"{branch!r} is the default branch"))
        for rec in _worktree_records(repo_root):
            if rec.get("branch") == f"refs/heads/{branch}":
                reasons.append(Reason("branch-checked-out", f"branch {branch!r} is checked out in {rec.get('worktree')}"))
        own = _own_names(repo_root, branch, LANDING_REMOTE)
        proof, why = _prove_landed(repo_root, tip, branch, default, forge, notes, remote=LANDING_REMOTE, own=own)
        if proof is None:
            reasons.extend(why)
        elif not reasons:
            return Verdict(kind, branch, True, (), proof, tuple(notes), tip)
    except _VERDICT_ERRORS as exc:
        reasons.append(Reason("verdict-error", f"could not complete the check: {exc}"))
    return _unsafe(kind, branch, reasons, notes, tip if _OID_RE.match(tip) else None)


def overwrite_verdict(
    repo_root: Path,
    branch: str,
    *,
    start: str = "",
    forge: ForgeLookup | None = None,
    fetch: bool = False,
) -> Verdict:
    """Verdict for OVERWRITING the local branch ``branch`` — ``git branch -f``/``-M``/``-C``,
    ``git checkout -B``, ``git switch -C``: its current tip stops being on that branch, so the old
    commits survive only in the reflog unless the branch's work is safe elsewhere. Same proofs as a
    deletion (:func:`branch_verdict` — landed on ``origin/<default>`` or held by another remote
    branch that stays; the branch's own remote copy never counts) with three differences:

    - a branch that does not exist yet has nothing to lose (SAFE, ``new-branch``) — even with no
      resolvable default branch;
    - a FAST-FORWARD loses nothing: when ``start`` (the revision the branch is re-pointed at;
      anything git resolves, judged in ``repo_root``) contains the old tip, every commit stays
      reachable from the branch. An unresolvable ``start`` just disables that rule;
    - being the default branch, or checked out somewhere, is not a reason: git itself refuses to
      force-update a branch checked out in ANOTHER worktree, and resetting the default branch loses
      only what is not on ``origin/<default>`` — which the proofs judge.
    """
    forge = forge or default_forge_lookup
    kind = "local-branch"
    notes: list[str] = []
    tip = ""
    try:
        bad = _check_ref_format(repo_root, branch)
        if bad is not None:
            return _unsafe(kind, branch, [Reason("invalid-branch-name", f"{branch!r}: {bad}")], notes, overwrite=True)
        if run_git(repo_root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}").returncode != 0:
            return Verdict(kind, branch, True, (), NEW_BRANCH, (), None, True)
        tip_res = run_git(repo_root, "rev-parse", "--verify", "-q", "--end-of-options", f"refs/heads/{branch}^{{commit}}")
        tip = _out(tip_res)
        if tip_res.returncode != 0 or not _OID_RE.match(tip):
            return _unsafe(kind, branch, [Reason("unknown-branch", f"branch {branch!r} does not point at a commit")], notes, overwrite=True)
        if start:  # `--end-of-options` below: a start that looks like an option is just an unresolvable revision
            new_res = run_git(repo_root, "rev-parse", "--verify", "-q", "--end-of-options", f"{start}^{{commit}}")
            new_tip = _out(new_res)
            if new_res.returncode == 0 and _OID_RE.match(new_tip) and _is_ancestor(repo_root, tip, new_tip) is True:
                return Verdict(kind, branch, True, (), FAST_FORWARD, (), tip, True)
        default, default_reason = _default_after_refresh(repo_root, fetch, notes)
        if default is None:
            return _unsafe(kind, branch, [default_reason or Reason("no-default-ref", "default branch unresolved")], notes, tip, True)
        own = _own_names(repo_root, branch, LANDING_REMOTE)
        proof, why = _prove_landed(repo_root, tip, branch, default, forge, notes, remote=LANDING_REMOTE, own=own)
        if proof is not None:
            return Verdict(kind, branch, True, (), proof, tuple(notes), tip, True)
        return _unsafe(kind, branch, why, notes, tip, True)
    except _VERDICT_ERRORS as exc:
        return _unsafe(kind, branch, [Reason("verdict-error", f"could not complete the check: {exc}")], notes, tip if _OID_RE.match(tip) else None, True)



def remote_branch_verdict(
    repo_root: Path,
    remote: str,
    branch: str,
    *,
    forge: ForgeLookup | None = None,
    fetch: bool = False,
    removing: frozenset[str] = frozenset(),
) -> Verdict:
    """Verdict for deleting ``branch`` on ``remote`` (``git push <remote> --delete <branch>``).
    The REMOTE's current tip is what gets checked (via ``ls-remote``, not a possibly stale local
    tracking ref) — it can differ from the local branch tip. ``removing`` are the branch names the
    same ``git push`` deletes on ``remote`` (they never count as holders of this branch's commits)."""
    forge = forge or default_forge_lookup
    kind = "remote-branch"
    target = f"{remote}/{branch}"
    notes: list[str] = []
    try:
        if not _REMOTE_NAME_RE.match(remote):
            return _unsafe(kind, target, [Reason("invalid-remote-name", f"{remote!r} is not a plain remote name")], notes)
        bad = _check_ref_format(repo_root, branch)
        if bad is not None:
            return _unsafe(kind, target, [Reason("invalid-branch-name", f"{branch!r}: {bad}")], notes)
        default, default_reason = _default_after_refresh(repo_root, fetch, notes)
        if default is None:
            return _unsafe(kind, target, [default_reason or Reason("no-default-ref", "default branch unresolved")], notes)
        if branch == default.name:
            return _unsafe(kind, target, [Reason("default-branch", f"{branch!r} is the default branch")], notes)

        ls = run_git(repo_root, "ls-remote", "--exit-code", remote, f"refs/heads/{branch}", timeout=_NET_TIMEOUT_S)
        if ls.returncode == 2:
            return _unsafe(kind, target, [Reason("unknown-branch", f"{remote} has no branch {branch!r}")], notes)
        if ls.returncode != 0:
            return _unsafe(kind, target, [Reason("remote-lookup-failed", f"`git ls-remote {remote}` failed: {_err(ls)}")], notes)
        tip = ""
        for line in os.fsdecode(ls.stdout).splitlines():
            sha, _, ref = line.partition("\t")
            if ref == f"refs/heads/{branch}":
                tip = sha
        if not _OID_RE.match(tip):
            return _unsafe(kind, target, [Reason("remote-lookup-failed", f"{remote} returned no usable tip for {branch!r}")], notes)

        if run_git(repo_root, "cat-file", "-e", f"{tip}^{{commit}}").returncode != 0:
            got = run_git(repo_root, "fetch", "--quiet", "--no-tags", remote, f"refs/heads/{branch}", timeout=_NET_TIMEOUT_S)
            if got.returncode != 0 or run_git(repo_root, "cat-file", "-e", f"{tip}^{{commit}}").returncode != 0:
                return _unsafe(
                    kind, target,
                    [Reason("not-landed", f"the remote tip {tip[:12]} is not available locally to check ({_err(got)})")],
                    notes,
                )
        proof, why = _prove_landed(
            repo_root, tip, branch, default, forge, notes, remote=remote, own=frozenset({branch}), removing=removing
        )
        if proof is not None:
            return Verdict(kind, target, True, (), proof, tuple(notes), tip)
        return _unsafe(kind, target, why, notes, tip)
    except _VERDICT_ERRORS as exc:
        return _unsafe(kind, target, [Reason("verdict-error", f"could not complete the check: {exc}")], notes)


# ── default forge lookup (reuses gc's PR fetch) ──────────────────────────────────
def pr_records_from_raw(raw: list) -> list[PrRecord]:
    """Same-repository PRs out of the shared ``gh``/Forgejo JSON shape. Cross-repository (fork)
    PRs and malformed records are dropped, never trusted."""
    out: list[PrRecord] = []
    for item in raw:
        if not isinstance(item, dict) or item.get("isCrossRepository"):
            continue
        head_ref = item.get("headRefName")
        state = item.get("state")
        number = item.get("number")
        if not isinstance(head_ref, str) or not head_ref or state not in {"OPEN", "MERGED", "CLOSED"} or not isinstance(number, int):
            continue
        merge = item.get("mergeCommit")
        merge_oid = merge.get("oid") if isinstance(merge, dict) else None
        head_oid = item.get("headRefOid")
        base = item.get("baseRefName")
        out.append(
            PrRecord(
                number=number,
                state=state,
                head_ref=head_ref,
                head_oid=head_oid if isinstance(head_oid, str) else None,
                base_ref=base if isinstance(base, str) else None,
                merge_commit_oid=merge_oid if isinstance(merge_oid, str) else None,
                url=str(item.get("url", "")),
            )
        )
    return out


def default_forge_lookup(repo_root: Path) -> list[PrRecord]:
    try:
        raw = worktree_gc.fetch_raw_prs(repo_root)
    except worktree_gc.GhLookupError as exc:
        raise ForgeUnavailable(str(exc)) from exc
    return pr_records_from_raw(raw)
