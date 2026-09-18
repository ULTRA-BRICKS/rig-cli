"""``rig worktree create``/``remove`` — standardized agent worktree lifecycle.

The ecosystem around rig had grown several different, uncoded conventions for where an
agent/harness worktree lands: Claude Code's own throwaway ``.claude/worktrees/`` (a separate,
already-solved problem — see ``GITIGNORE_DEFAULT_ENTRIES`` in :mod:`riglib.config``, ignored
machine-wide via the GLOBAL git excludes file), a bare ``.worktrees/`` some repos had adopted by
hand INSIDE the repo, and the same ``.worktrees/`` name used one directory level ABOVE the repo by
some harnesses. None of it was rig-owned, so nothing kept the three in sync or registered the
directory anywhere.

This module is the one place a NEW rig-created worktree gets planted, so every repo converges on
a single standardized, discoverable, IN-repo location:

    <repo>/.worktrees/<name>

Before running ``git worktree add``, it registers ``.worktrees/`` in the repo's ``.git/info/
exclude`` — never the committed ``.gitignore``, since this is a local, per-machine scratch-space
convention, not something every clone/contributor should carry — so a freshly created worktree
never shows up as an untracked path in ``git status``. Deliberately reconciled BEFORE the ``git
worktree add`` (not after): the reconcile needs no worktree to exist first, so doing it first
means a marker conflict or an unwritable exclude file is reported WITHOUT ever creating a
worktree — no half-done "created but not ignored" state to explain or clean up. NOTE: ``info/
exclude`` is the repo's COMMON (shared) exclude file — every worktree of a repo, linked or
primary, resolves to the SAME physical file (verified: `git -C <linked-worktree> rev-parse
--git-path info/exclude` names the primary checkout's `.git/info/exclude`, not a per-worktree
private copy) — so this reconcile affects `git status` in every worktree of the repo, not just
the one being created. ``git rev-parse --git-path`` is still required rather than a naive
``<repo>/.git/info/exclude`` string join, because inside a linked worktree ``.git`` is a FILE
(a pointer), not a directory. That reconcile is idempotent (repeat creations don't duplicate the
entry) and lives in :mod:`riglib.actions.runner` (:func:`riglib.actions.runner.
reconcile_worktrees_exclude`), next to the ship-delegator/opencode-bridge excludes it shares its
marker-splice machinery with.

Result shape: like :mod:`riglib.codex_update`, this returns a plain dataclass with an
``exit_code`` (from :mod:`riglib.errors`) rather than raising a :class:`riglib.errors.RigError` —
worktree creation is an imperative one-shot git operation, not a config/catalog/plan validation
path, so it follows the OTHER established result-object precedent for that class of command.

``rig worktree remove`` is the inverse: it tears down a ``.worktrees/<name>`` created above.
``git worktree remove`` alone leaves the branch behind (a bare retry of ``create`` then fails with
"a branch named '<name>' already exists" — the same gotcha :func:`_partial_state_cleanup_hint`
below documents for a FAILED ``create``), so ``remove()`` also deletes the branch, in the same
order a human would recover by hand: worktree first, then branch. The branch to delete is read
from ``git worktree list --porcelain`` rather than assumed to equal ``name`` — ``create``'s
``--branch`` can diverge from the directory name, and guessing wrong would delete nothing (or
worse, the wrong ref). The lookup runs BEFORE any git mutation (same "reconcile before mutate"
ordering ``create`` uses for its exclude registration): if the listing itself can't be trusted —
the command failed, timed out, or exited non-zero — ``remove()`` refuses outright rather than
guessing "no branch, must be detached" and silently stranding a real one; a stranded branch is
exactly the failure this whole command exists to prevent.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import errors
from .actions.runner import reconcile_worktrees_exclude
from .config import WORKTREES_DIR_NAME

_GIT_TIMEOUT_S = 60

# Generous upper bound for a real owner pid recorded in an owner marker (see the "owner markers"
# section below): comfortably above every real platform's actual `pid_t` range (Linux
# `pid_max` tops out at 2**22 by default; macOS/BSD pids are far smaller), used to reject an
# out-of-range marker `pid` BEFORE calling `_pid_alive` rather than let it read as a nonsensical
# "STILL RUNNING" claim about a pid that could never have been a real process.
_MAX_REASONABLE_PID = 2**31 - 1

# Shared across the `create()`-failure hint below and `remove()`'s own branch-delete-failure
# message: `git worktree remove` only detaches the worktree, it never touches the branch ref, so
# whenever a worktree goes away the branch needs its own explicit `git branch -D`.
_WORKTREE_REMOVE_ALONE_LEAVES_BRANCH = "`git worktree remove` alone leaves the branch behind"


class _BranchLookupError(Exception):
    """``git worktree list --porcelain`` itself could not be trusted (failed to run, timed out, or
    exited non-zero) — NOT the same as a definitive "no branch" answer (detached HEAD, or the
    target simply isn't a registered worktree). Callers must not treat this as "no branch": doing
    so would remove the worktree, skip ``git branch -D``, and report success while the branch is
    quietly left behind.

    Carries its own ``exit_code`` (an :mod:`riglib.errors` ``EXIT_*`` constant) rather than always
    mapping to one fixed code — a timeout, a missing git binary, and an ordinary non-zero exit are
    different failure classes (see :func:`_find_worktree_branch`), and the CLI epilog documents
    them as different exit codes; collapsing all three into one would make the documented contract
    wrong for two of them.
    """

    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _partial_state_cleanup_hint(target: Path, branch_name: str) -> str:
    """Copy-pasteable recovery for a ``target``/branch git may have left behind after a failed
    ``git worktree add -b <branch_name>``.

    Both the timeout path and the hook-failure path below can leave this same partial state, so
    they share this one hint (kept in sync in one place) rather than duplicating the wording.
    Quoted with :func:`shlex.quote` — ``target``/``branch_name`` are user-controlled (a worktree
    name may contain spaces; :func:`_invalid_name_reason` only rejects ``/``, ``\\``, empty, and
    ``.``/``..``) and these are meant to be pasted straight into a shell.
    """
    quoted_target = shlex.quote(str(target))
    quoted_branch = shlex.quote(branch_name)
    return (
        f"check `git worktree list` and `git branch`, then `git worktree remove --force "
        f"{quoted_target}` and `git branch -D {quoted_branch}` before retrying — "
        f"{_WORKTREE_REMOVE_ALONE_LEAVES_BRANCH}, so a bare retry fails with 'a branch named "
        f"{branch_name!r} already exists'"
    )


@dataclass(frozen=True)
class WorktreeResult:
    """The outcome of :func:`create`/:func:`remove`. ``status`` is ``"created"``, ``"removed"``,
    or ``"error"``."""

    status: str
    message: str
    path: Path | None = None
    branch: str | None = None
    exclude_note: str = ""
    exit_code: int = 0


def worktree_path(repo_root: Path, name: str) -> Path:
    """The standardized path for a NEW agent worktree: ``<repo>/.worktrees/<name>``."""
    return repo_root / WORKTREES_DIR_NAME / name


# ── owner markers: disambiguate a live collision from an abandoned leftover ──────
#
# rig-cli grooming#275: `create()`'s "target already exists" refusal used to be a bare
# string, giving the caller zero signal about whether the existing `.worktrees/<name>` was a
# STILL-LIVE worktree another dispatched agent is actively using right now, or a long-abandoned
# leftover that's safe to remove and reuse. The incident this closes: two dispatched agents
# independently picked the same worktree name/branch, and the second one's `create()` silently
# collided, picking up unrelated stray uncommitted changes from the first agent's still-running
# task mid-work. A lightweight PID-based marker — written next to (never inside) the worktree
# directory it describes — lets the refusal message say which situation the caller is actually
# in, using POSIX signal-0 liveness probing (`os.kill(pid, 0)`) against the marker's recorded
# pid, with no lock file or IPC required.
_OWNERS_SUBDIR_NAME = ".owners"


def _owner_marker_path(repo_root: Path, name: str) -> Path:
    """Where the owner marker for worktree ``name`` lives: a SIBLING of the worktree directory
    itself (``.worktrees/.owners/<name>.json``, never ``.worktrees/<name>/...``) so it survives
    independently of whatever git or the agent does inside the worktree, and so it's covered by
    the SAME ``/.worktrees/`` entry `reconcile_worktrees_exclude` already registers in
    ``.git/info/exclude`` — no separate exclude wiring needed.
    """
    return repo_root / WORKTREES_DIR_NAME / _OWNERS_SUBDIR_NAME / f"{name}.json"


def _write_owner_marker(repo_root: Path, name: str) -> None:
    """Record the dispatching process as this worktree's owner. Best-effort: a marker write
    failure (read-only filesystem, disk full, ...) must never fail an otherwise-successful
    ``create()`` — the marker is a diagnostic aid for the NEXT collision, not a correctness
    requirement for this one.

    Records ``os.getppid()`` (the process that invoked this `rig` process — e.g. the dispatching
    agent/shell), not ``os.getpid()`` (this short-lived `rig` invocation itself, which will have
    exited long before a collision could be detected).
    """
    marker_path = _owner_marker_path(repo_root, name)
    try:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(
            json.dumps(
                {"pid": os.getppid(), "created_at": datetime.now(timezone.utc).isoformat()}
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def _remove_owner_marker(repo_root: Path, name: str) -> None:
    """Best-effort cleanup of ``name``'s owner marker once its worktree is actually gone."""
    try:
        _owner_marker_path(repo_root, name).unlink(missing_ok=True)
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    """Whether ``pid`` still refers to a live process, via POSIX signal-0 (`os.kill(pid, 0)`)
    liveness probing — sends no actual signal, just checks whether delivery would succeed.

    Fails safe TOWARD "alive" on an ambiguous result: `ProcessLookupError` is the only
    unambiguous "gone" (the kernel has no such pid); any other `OSError` (e.g. `PermissionError`
    for a pid owned by another user) means the process exists but we can't signal it, which is
    still evidence it's running.

    POSIX-only by design (guarded, not just documented): signal value ``0`` is numerically
    ``signal.CTRL_C_EVENT`` on Windows, so `os.kill(pid, 0)` there does NOT probe liveness — it
    delivers a REAL Ctrl+C to `pid`'s console process group, which could interrupt or kill the
    very live owner this check exists to detect. Skip the syscall entirely off POSIX and fail
    safe toward "alive" (matches this function's existing ambiguous-result stance) rather than
    risk that side effect.

    ``pid <= 0`` is likewise short-circuited to "alive" WITHOUT calling `os.kill` — pid ``0``
    signals our OWN process group, and a negative pid signals a process GROUP rather than a
    process, either of which could misreport. `_describe_existing_target` already validates this
    before calling in, but the guard lives here too so this stays self-enforcing for any future
    caller, not just the one caller disciplined enough to check first. For the same reason, a
    `pid` too large for the platform's `pid_t` (`os.kill` raises `OverflowError`, not `OSError`)
    is also caught here rather than left for the caller to catch.

    Known limitation, accepted rather than fixed here: this is unqualified PID liveness, not
    process identity. If the OS recycles the recorded pid for an unrelated process before the
    worktree is revisited, or the owning process is a long-lived orchestrator that dispatches
    every `create()` call (so every marker shares its always-alive pid), the "STILL RUNNING"
    branch below can misfire on a genuinely abandoned worktree. `create()` still refuses the
    collision in EVERY case regardless, so the blast radius is a misleading diagnostic string,
    never incorrect worktree behavior or data loss.
    """
    if pid <= 0 or os.name != "posix":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, OverflowError):
        return True
    return True


def _describe_existing_target(repo_root: Path, name: str, target: Path) -> str:
    """The enriched "already exists" refusal message for `create()`, built from `name`'s owner
    marker when one is readable. Every branch's returned string contains the literal substring
    "already exists" (an existing test asserts it) so callers/tests can rely on that regardless
    of which branch fired.

    ``name`` is quoted with :func:`shlex.quote` everywhere it appears inside a copy-pasteable
    `` `rig worktree remove ...` `` hint — matching :func:`_partial_state_cleanup_hint`'s
    convention — since :func:`_invalid_name_reason` allows spaces, quotes, and shell metachars in
    a worktree name: an unquoted name here would let a maliciously-named worktree turn an
    innocent copy-paste into arbitrary shell execution.
    ``created_at`` comes from the marker file, which is only ever partially trusted (see the
    parsing `except` below) — rendered with ``!r`` rather than interpolated raw so a
    hand-crafted marker can't smuggle terminal control sequences into the message.
    """
    base = f"{target} already exists"
    marker_path = _owner_marker_path(repo_root, name)
    quoted_name = shlex.quote(name)
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        pid = int(marker["pid"])
        created_at = str(marker["created_at"])
        # `pid <= 0` is never a real owner pid we recorded (`os.getppid()` is always positive):
        # `os.kill(0, 0)` signals our OWN process group (always "succeeds", falsely "alive"), and
        # a negative pid targets a process GROUP rather than a process — both would otherwise
        # slip past `_pid_alive` as a false "STILL RUNNING" on a corrupted/adversarial marker.
        # A pid too large for any real platform's `pid_t` (`_MAX_REASONABLE_PID`) is validated
        # here too, rather than left to `_pid_alive`'s own `OverflowError` catch: `_pid_alive`
        # fails safe toward "alive" for a value it can't signal, which for THIS message would
        # read as a nonsensical "pid 10**30 is still running, wait for it to finish" — the
        # generic malformed-marker fallback below is the honest answer for a value this shape.
        if not 0 < pid <= _MAX_REASONABLE_PID:
            raise ValueError(f"marker pid out of range: {pid}")
        alive = _pid_alive(pid)
    except (OSError, ValueError, KeyError, TypeError, OverflowError, RecursionError):
        # `RecursionError` specifically: a marker file is only ever partially trusted (see the
        # module note above), and `json.loads` on adversarially deep nesting raises it before any
        # `pid` check runs. Every other malformed shape already falls into this same generic
        # fallback; this closes the last uncaught one.
        return f"{base} — refusing to overwrite it; inspect it, or `rig worktree remove {quoted_name}` first"

    if alive:
        return (
            f"{base} — created by pid {pid}, which is STILL RUNNING (since {created_at!r}). This "
            f"looks like a second dispatch reusing a name a live worker already owns, which can "
            f"corrupt one agent's uncommitted work with another's. Give this dispatch its own "
            f"unique worktree name instead of reusing {name!r}, or wait for pid {pid} to finish "
            f"and `rig worktree remove {quoted_name}` first"
        )
    return (
        f"{base} — created by pid {pid} (since {created_at!r}), which is no longer running. This "
        f"looks abandoned, not actively in use, but still refuses to overwrite it silently — "
        f"inspect it, or `rig worktree remove {quoted_name}` first if you want to reuse the name"
    )


def _invalid_name_reason(name: str) -> str | None:
    """Reason ``name`` is unsafe as a single path segment under ``.worktrees/``, else ``None``.

    ``name`` becomes a literal directory component (``worktree_path``) and a default git branch
    name — reject anything that could escape ``.worktrees/`` (a path separator, ``.``/``..``) or
    is empty, rather than let a malformed name silently create a worktree somewhere unexpected.

    ``_OWNERS_SUBDIR_NAME`` (``.owners``) is ALSO reserved: it is where owner markers for every
    OTHER worktree live (:func:`_owner_marker_path`), a sibling of `.worktrees/<name>/` for every
    name but this one. A worktree actually named ``.owners`` would make git create a real linked
    worktree AT that same path, so every other worktree's marker read/write/remove would target
    a file INSIDE that live checkout instead of a bare marker directory — corrupting the
    occupant's tree on write and destroying every other worktree's owner history on
    `git worktree remove --force .owners`. Compared
    case-INSENSITIVELY (mirrors `riglib/config.py`'s ``p.lower() == ".git"``): on a
    case-insensitive-but-preserving filesystem (macOS APFS default, NTFS), ``.OWNERS``/
    ``.Owners``/etc. name the SAME on-disk directory as ``.owners`` — an exact-match check would
    let a differently-cased name straight through to create a real linked worktree that physically
    aliases the marker directory. Also compared after stripping TRAILING dots/spaces: Windows
    silently strips trailing ``.``/`` `` from a filename at creation time, so ``.owners.`` or
    ``.owners `` would otherwise alias ``.owners`` there too.
    """
    if not name or not name.strip():
        return "worktree name must not be empty"
    if name in (".", ".."):
        return f"worktree name {name!r} is not a valid directory name"
    if "/" in name or "\\" in name:
        return f"worktree name {name!r} must be a single path segment (no / or \\)"
    if name.lower().rstrip(". ") == _OWNERS_SUBDIR_NAME:
        return f"worktree name {name!r} is reserved for owner-marker storage"
    return None


def _invalid_ref_reason(label: str, value: str) -> str | None:
    """Reason ``value`` (a ``--branch``/``--from`` argument) is unsafe to hand to git, else None.

    Both become POSITIONAL arguments on a ``git worktree add`` command line
    (``-b <branch> [<base_ref>]``). A value starting with ``-`` is not a valid git ref/branch
    name anyway (`git check-ref-format` rejects a leading dash), but git's own ARGUMENT PARSER
    reads it as an OPTION before ref-validation ever runs — e.g. a ``base_ref`` of
    ``--no-checkout`` is silently accepted as the flag, git exits 0, and the worktree is left
    unpopulated (verified empirically: `git worktree add <path> -b <b> --no-checkout` creates an
    empty worktree, no error). Rejecting a leading ``-`` up front closes that option-injection
    path outright, rather than trying to enumerate which flags are dangerous.
    """
    if value.startswith("-"):
        return f"{label} {value!r} must not start with '-' (would be read as a git option)"
    return None


def _target_escapes_repo(repo_root: Path, target: Path) -> bool:
    """True if ``target``'s ``.worktrees`` parent resolves OUTSIDE ``repo_root``.

    ``name`` is already validated to be a single path segment with no ``/`` or ``..``
    (:func:`_invalid_name_reason`), so the only way ``target`` (``repo_root/.worktrees/<name>``)
    could land outside the repo is if the ``.worktrees`` directory ITSELF is a symlink pointing
    elsewhere — a stray leftover from a hand-rolled setup, or worse. A first-ever worktree has no
    ``.worktrees`` dir yet, so there is nothing to escape through: it does not exist AND isn't a
    symlink, so `git worktree add` creates it fresh as a plain subdirectory of ``repo_root``.

    Checks ``is_symlink()`` independently of (and before) ``exists()``: a DANGLING symlink
    (pointing at a path that doesn't itself exist — e.g. ``.worktrees -> /outside/already-
    deleted``) makes ``Path.exists()`` return ``False`` too, since ``exists()`` follows the link
    and reports on its TARGET — which would otherwise read as "no ``.worktrees`` dir yet, nothing
    to escape through" and let a dangling escape straight past this guard. A symlink resolves
    outside the repo exactly as much whether or not its target currently exists, so it gets the
    same refusal either way (using a non-strict ``resolve()`` here, since a dangling target has
    nothing on disk for ``strict=True`` to find).
    """
    worktrees_dir = target.parent
    if not worktrees_dir.exists() and not worktrees_dir.is_symlink():
        return False
    resolved_worktrees_dir = worktrees_dir.resolve()
    resolved_repo_root = repo_root.resolve(strict=True)
    try:
        resolved_worktrees_dir.relative_to(resolved_repo_root)
    except ValueError:
        return True
    return False


def _validate_create_args(name: str, branch_name: str, base_ref: str | None) -> str | None:
    """First applicable rejection reason for ``create``'s arguments, else ``None``."""
    for reason in (
        _invalid_name_reason(name),
        _invalid_ref_reason("--branch", branch_name),
        _invalid_ref_reason("--from", base_ref) if base_ref else None,
    ):
        if reason is not None:
            return reason
    return None


def _run_git_worktree_add(
    repo_root: Path, target: Path, branch_name: str, base_ref: str | None
) -> WorktreeResult | None:
    """Run ``git worktree add``. Returns an error :class:`WorktreeResult`, or ``None`` on success.

    Exit-code classification: git not runnable at all (binary missing) is a MISSING DEPENDENCY
    (127); git ran but rejected the request for a domain reason (branch already exists, bad ref,
    a dirty index blocking checkout, …) is a USAGE-class problem — the same bucket as an invalid
    name or an already-occupied target (``EXIT_CONFIG``) — not "rig broke" (``EXIT_INTERNAL``,
    reserved for rig's own unexpected failures, e.g. the exclude reconcile below). A timeout is
    reported as a TIMEOUT, not misreported as "could not run git" (git DID run — it just didn't
    finish within the bound, plausible on a large repo/slow disk).
    """
    cmd = ["git", "-C", str(repo_root), "worktree", "add", str(target), "-b", branch_name]
    if base_ref:
        cmd.append(base_ref)
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=_GIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return WorktreeResult(
            "error",
            f"git worktree add timed out after {_GIT_TIMEOUT_S}s (git may have left a partial "
            f"worktree/branch behind — {_partial_state_cleanup_hint(target, branch_name)})",
            exit_code=errors.EXIT_INTERNAL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return WorktreeResult(
            "error", f"could not run git: {exc}", exit_code=errors.EXIT_MISSING_DEP
        )
    if res.returncode != 0:
        detail = res.stderr.strip() or res.stdout.strip() or f"exit {res.returncode}"
        if target.exists():
            # Git had already created and registered the worktree AND the branch by the time a
            # later step — typically a post-checkout hook — exited non-zero (empirically
            # verified: `create()` already refused if `target` pre-existed, so `target` only
            # exists here because THIS `git worktree add` call created it). Say so explicitly
            # instead of just reporting "failed": a bare failure with no cleanup guidance leads
            # a retry straight into "already exists" — and `git worktree remove` alone leaves
            # the branch behind (verified: a follow-up `git worktree add -b <branch>` then fails
            # with "a branch named '<branch>' already exists"), so both must be named.
            return WorktreeResult(
                "error",
                f"git worktree add failed: {detail} ({target} was created before the failure, "
                f"likely by a post-checkout hook — "
                f"{_partial_state_cleanup_hint(target, branch_name)})",
                exit_code=errors.EXIT_CONFIG,
            )
        return WorktreeResult(
            "error", f"git worktree add failed: {detail}", exit_code=errors.EXIT_CONFIG
        )
    return None


def create(
    repo_root: Path,
    name: str,
    *,
    branch: str | None = None,
    base_ref: str | None = None,
) -> WorktreeResult:
    """Create a linked worktree at ``<repo_root>/.worktrees/<name>``, ignore-registered first.

    ``branch`` defaults to ``name`` (a fresh branch, mirroring plain ``git worktree add <path>
    -b <name>``); ``base_ref`` defaults to git's own default (the current ``HEAD``) when omitted.

    Order matters (see the module docstring): the ``.git/info/exclude`` reconcile runs BEFORE
    ``git worktree add``, so a marker conflict or an unwritable exclude file is reported without
    ever creating a worktree. Only once that succeeds does git actually create the worktree.
    """
    branch_name = branch or name
    reason = _validate_create_args(name, branch_name, base_ref)
    if reason is not None:
        return WorktreeResult("error", reason, exit_code=errors.EXIT_CONFIG)

    target = worktree_path(repo_root, name)
    if target.exists():
        return WorktreeResult(
            "error",
            _describe_existing_target(repo_root, name, target),
            path=target,
            exit_code=errors.EXIT_CONFIG,
        )
    if _target_escapes_repo(repo_root, target):
        return WorktreeResult(
            "error",
            f"{target.parent} is a symlink pointing outside {repo_root} — refusing to create "
            "a worktree through it",
            exit_code=errors.EXIT_CONFIG,
        )

    exclude_ok, exclude_note = reconcile_worktrees_exclude(repo_root)
    if not exclude_ok:
        return WorktreeResult(
            "error",
            f"{exclude_note} — refusing to create the worktree until .worktrees/ can be "
            "registered in .git/info/exclude",
            exclude_note=exclude_note,
            exit_code=errors.EXIT_INTERNAL,
        )

    add_error = _run_git_worktree_add(repo_root, target, branch_name, base_ref)
    if add_error is not None:
        return add_error
    _write_owner_marker(repo_root, name)

    return WorktreeResult(
        "created",
        f"created worktree at {target} (branch {branch_name})",
        path=target,
        branch=branch_name,
        exclude_note=exclude_note,
        exit_code=0,
    )


def _classify_run_oserror(exc: OSError | subprocess.SubprocessError) -> int:
    """The exit code an ``OSError``/``SubprocessError`` from one of ``remove()``'s three git
    subprocess steps should map to.

    By the time any of these three run, the CLI preflight (`_resolve_worktree_repo_env`) has
    already confirmed `git` is on PATH — and the LATER two steps run only after an EARLIER git
    invocation in the same `remove()` call already spawned successfully, so "git is not
    installed" gets less true with every step. ``FileNotFoundError`` (the executable genuinely
    couldn't be found/exec'd — e.g. a PATH/binary race right after the preflight) is still
    legitimately ``EXIT_MISSING_DEP``; any OTHER ``OSError`` (``EMFILE``, ``ENOMEM``, a fork
    failure, …) is rig hitting an unexpected runtime problem, not a missing dependency, and maps
    to ``EXIT_INTERNAL`` instead. Applied identically across all three steps so the same failure
    class never gets two different diagnoses depending on which step happened to hit it.
    """
    return errors.EXIT_MISSING_DEP if isinstance(exc, FileNotFoundError) else errors.EXIT_INTERNAL


def _decode_message(data: bytes) -> str:
    """Best-effort decode of a subprocess's captured stdout/stderr for a human-readable message.

    Deliberately NOT ``text=True`` on the ``subprocess.run`` calls below: text mode decodes with
    ``locale.getpreferredencoding()`` (not guaranteed UTF-8 — e.g. ``LANG=C`` on a CI box) and
    would raise an uncaught, crash-the-whole-command ``UnicodeDecodeError`` on any non-ASCII byte
    git writes (a non-ASCII path in a worktree-add/remove/branch-delete message, say). ``errors=
    "replace"`` never raises — an undecodable byte becomes U+FFFD in the message, which is fine
    here since this decoded text is DISPLAY-only (never re-parsed or compared).
    """
    return data.decode("utf-8", errors="replace")


def _find_worktree_branch(repo_root: Path, target: Path) -> str | None:
    """The branch checked out at ``target`` per ``git worktree list --porcelain``, or ``None`` when
    ``target`` is DEFINITIVELY branchless (not a registered worktree, or detached HEAD).

    Raises :class:`_BranchLookupError` (with a matching ``exit_code``) when the listing command
    itself can't be trusted — timed out, couldn't run, or exited non-zero — since that is a
    DIFFERENT outcome from "no branch" and must never be collapsed into it (see the class
    docstring).

    Parses with ``-z`` (NUL-terminated fields, git >= 2.36) rather than the default newline
    format, which C-quotes (octal-escapes) any "unusual" byte in a listed path — and "unusual"
    reaches plain ASCII too: a literal ``"`` in a worktree name is legal (``_invalid_name_reason``
    only rejects ``/``, ``\\``, empty, and ``.``/``..``; ``git check-ref-format --branch`` confirms
    ``"`` is a legal branch-name character), and `git` would quote it in the default format. A
    naive line-based parse does no unquoting, so a quoted path would silently fail to match
    ``target`` and masquerade as "no branch found here". ``-z`` sidesteps the whole quoting class
    by emitting every value raw.

    Captures RAW BYTES (no ``text=True``) and splits/decodes them by hand: POSIX filenames may
    contain any byte except NUL and ``/``, and text mode's locale-dependent decoding plus
    universal-newline translation could either crash on or silently corrupt such a byte in the
    middle of parsing this exact NUL-delimited stream — the very class of "quietly mismatches and
    strands the branch" bug ``-z`` was chosen to prevent in the first place. ``errors=
    "surrogateescape"`` round-trips every byte losslessly through ``str``/``Path`` (matching how
    ``os.fsdecode``/``os.fsencode`` handle filesystem paths), so the eventual ``resolve() ==
    resolved_target`` comparison — and feeding the returned branch name back into ``git branch -D``
    argv — still works correctly even for a non-UTF-8 path or branch name.
    """
    cmd = ["git", "-C", str(repo_root), "worktree", "list", "--porcelain", "-z"]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=_GIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise _BranchLookupError(
            f"git worktree list timed out after {_GIT_TIMEOUT_S}s", errors.EXIT_INTERNAL
        ) from None
    except (OSError, subprocess.SubprocessError) as exc:
        raise _BranchLookupError(
            f"could not run git worktree list: {exc}", _classify_run_oserror(exc)
        ) from exc
    if res.returncode != 0:
        detail = _decode_message(res.stderr).strip() or _decode_message(res.stdout).strip()
        raise _BranchLookupError(f"git worktree list failed: {detail or f'exit {res.returncode}'}", errors.EXIT_CONFIG)

    # Exact match is the fast, common path — a case-insensitive fallback covers a worktree
    # registered on a case-insensitive filesystem (macOS/Windows default) whose recorded case
    # differs from `target`'s (e.g. `create`d as "Agent-1", `remove`d as "agent-1"): the
    # filesystem treats them as the SAME path, and refusing to recognize that would silently
    # report "no branch found — detached?" while the real branch survives, exactly the stranding
    # bug this lookup exists to prevent. The fallback takes the FIRST case-insensitive match, not
    # necessarily a unique one — deliberately fine, not just unchecked: a case-insensitive
    # filesystem cannot itself hold two entries differing only by case, and even a same-repo
    # cross-platform-listing edge case is backstopped downstream, since `_run_git_worktree_remove`
    # targets `target`'s own EXACT-case path — a wrong ci-fallback guess makes THAT step fail
    # ("not a working tree") before any branch is ever deleted, not the wrong branch getting
    # silently removed.
    resolved_target = target.resolve()
    resolved_target_ci = str(resolved_target).casefold()
    current_path: Path | None = None
    ci_fallback: str | None = None
    for raw_field in res.stdout.split(b"\x00"):
        field = raw_field.decode("utf-8", errors="surrogateescape")
        if field.startswith("worktree "):
            current_path = Path(field[len("worktree ") :])
        elif field.startswith("branch refs/heads/") and current_path is not None:
            resolved_current = current_path.resolve()
            if resolved_current == resolved_target:
                return field[len("branch refs/heads/") :]
            if ci_fallback is None and str(resolved_current).casefold() == resolved_target_ci:
                ci_fallback = field[len("branch refs/heads/") :]
    return ci_fallback


def _run_git_worktree_remove(repo_root: Path, target: Path) -> WorktreeResult | None:
    """Run ``git worktree remove --force <target>``. Returns an error result, or ``None`` on success.

    ``--force`` matches this command's intent (tearing an agent-created worktree down wholesale,
    same as ``create``'s counterpart) — a worktree with uncommitted or untracked changes is still
    removed rather than blocking on them. A LOCKED worktree still refuses even with one ``--force``
    (git requires unlocking, or ``--force`` twice); that refusal surfaces as an ordinary error here
    rather than being silently retried, so a lock made on purpose isn't blown through by accident.
    Captures raw bytes, not ``text=True`` — see :func:`_decode_message`.
    """
    cmd = ["git", "-C", str(repo_root), "worktree", "remove", "--force", str(target)]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=_GIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return WorktreeResult(
            "error",
            f"git worktree remove timed out after {_GIT_TIMEOUT_S}s ({target} may be left in a "
            "partial state — check `git worktree list`)",
            path=target,
            exit_code=errors.EXIT_INTERNAL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return WorktreeResult(
            "error", f"could not run git: {exc}", path=target, exit_code=_classify_run_oserror(exc)
        )
    if res.returncode != 0:
        detail = _decode_message(res.stderr).strip() or _decode_message(res.stdout).strip()
        return WorktreeResult(
            "error",
            f"git worktree remove failed: {detail or f'exit {res.returncode}'}",
            path=target,
            exit_code=errors.EXIT_CONFIG,
        )
    return None


def _run_git_branch_delete(repo_root: Path, branch_name: str, target: Path) -> WorktreeResult | None:
    """Run ``git branch -D <branch_name>``. Returns an error result, or ``None`` on success.

    Only called AFTER :func:`_run_git_worktree_remove` already succeeded, so any failure here
    means the worktree itself is gone but the branch ref survives — worth naming explicitly
    (:data:`_WORKTREE_REMOVE_ALONE_LEAVES_BRANCH`) with the exact copy-pasteable follow-up, rather
    than a bare "git branch -D failed" that leaves the caller to rediscover the branch name.
    Captures raw bytes, not ``text=True`` — see :func:`_decode_message`.
    """
    cmd = ["git", "-C", str(repo_root), "branch", "-D", branch_name]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=_GIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return WorktreeResult(
            "error",
            f"git branch -D timed out after {_GIT_TIMEOUT_S}s (worktree at {target} was already "
            f"removed; branch {branch_name!r} may still exist — "
            f"`git branch -D {shlex.quote(branch_name)}`)",
            path=target,
            branch=branch_name,
            exit_code=errors.EXIT_INTERNAL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return WorktreeResult(
            "error",
            f"could not run git: {exc}",
            path=target,
            branch=branch_name,
            exit_code=_classify_run_oserror(exc),
        )
    if res.returncode != 0:
        detail = _decode_message(res.stderr).strip() or _decode_message(res.stdout).strip()
        return WorktreeResult(
            "error",
            f"worktree at {target} was removed, but git branch -D failed: "
            f"{detail or f'exit {res.returncode}'} — {_WORKTREE_REMOVE_ALONE_LEAVES_BRANCH} — "
            f"delete it by hand: `git branch -D {shlex.quote(branch_name)}`",
            path=target,
            branch=branch_name,
            exit_code=errors.EXIT_CONFIG,
        )
    return None


def _resolve_branch_to_delete(repo_root: Path, target: Path) -> tuple[str | None, WorktreeResult | None]:
    """``(branch_name, None)`` on a definitive answer (``branch_name`` may be ``None`` — detached),
    or ``(None, error_result)`` when the lookup itself couldn't be trusted (see
    :func:`_find_worktree_branch`) — refusing to guess "detached" and silently strand a real branch.
    """
    try:
        return _find_worktree_branch(repo_root, target), None
    except _BranchLookupError as exc:
        return None, WorktreeResult(
            "error",
            f"could not determine the branch checked out at {target}: {exc} — refusing to "
            "remove the worktree without knowing whether a branch would be left behind (retry, "
            "or check `git worktree list` / `git branch` by hand)",
            path=target,
            exit_code=exc.exit_code,
        )


def remove(repo_root: Path, name: str) -> WorktreeResult:
    """Remove the linked worktree at ``<repo_root>/.worktrees/<name>`` AND its branch.

    The inverse of :func:`create`: wraps ``git worktree remove --force <path>`` followed by
    ``git branch -D <branch>`` (see the module docstring and
    :data:`_WORKTREE_REMOVE_ALONE_LEAVES_BRANCH` for why both steps are required). The branch to
    delete is read from ``git worktree list --porcelain`` via :func:`_find_worktree_branch`
    rather than assumed to equal ``name`` — ``create --branch`` can diverge from the directory
    name. That lookup runs BEFORE any git mutation (mirrors ``create``'s "reconcile before
    mutate" ordering): a lookup failure refuses the whole removal rather than guessing "detached"
    and silently stranding a real branch. A worktree that genuinely IS detached (no branch to
    find) is removed with just the first step; that is reported as success, not an error, since
    there is nothing left for the second step to do.

    Deliberately has NO ``target.exists()`` precondition (unlike ``create``'s "target already
    exists" check, which guards the opposite direction) — ``git worktree remove --force`` is
    itself the authority on whether ``target`` is a real worktree to tear down, and it is MORE
    capable than a filesystem check: a worktree whose directory was deleted by hand (`rm -rf`
    instead of `git worktree remove`) is still REGISTERED with git and still has a live branch —
    `git worktree remove --force` recognizes and prunes that stale registration (verified
    empirically), which is exactly the recovery this command should offer, not refuse. A `name`
    that was never created at all still fails cleanly: git's own "is not a working tree" error
    surfaces through :func:`_run_git_worktree_remove` below.

    Refuses if ``target`` ITSELF (not just its ``.worktrees`` parent, which
    :func:`_target_escapes_repo` already covers) is a symlink: a genuinely rig-created (or any
    normal `git worktree add`-created) worktree root is NEVER a symlink — git always creates a
    real directory there — so this refusal has no legitimate worktree to false-positive on. A
    hand-crafted ``.worktrees/<name> -> /elsewhere`` symlink would otherwise resolve straight
    through both the branch lookup and the git mutation, letting a name that LOOKS local silently
    operate on whatever the symlink points to (dangling, or another worktree this same repo
    happens to have registered elsewhere).
    """
    reason = _invalid_name_reason(name)
    if reason is not None:
        return WorktreeResult("error", reason, exit_code=errors.EXIT_CONFIG)

    target = worktree_path(repo_root, name)
    if _target_escapes_repo(repo_root, target):
        return WorktreeResult(
            "error",
            f"{target.parent} is a symlink pointing outside {repo_root} — refusing to remove "
            "through it",
            path=target,
            exit_code=errors.EXIT_CONFIG,
        )
    if target.is_symlink():
        return WorktreeResult(
            "error",
            f"{target} is itself a symlink — refusing to remove through it (a real worktree "
            "root is never a symlink)",
            path=target,
            exit_code=errors.EXIT_CONFIG,
        )

    branch_name, lookup_error = _resolve_branch_to_delete(repo_root, target)
    if lookup_error is not None:
        return lookup_error

    remove_error = _run_git_worktree_remove(repo_root, target)
    if remove_error is not None:
        return remove_error
    _remove_owner_marker(repo_root, name)

    if branch_name is None:
        return WorktreeResult(
            "removed",
            f"removed worktree at {target} (no branch found for it — was HEAD detached?)",
            path=target,
            exit_code=0,
        )

    branch_error = _run_git_branch_delete(repo_root, branch_name, target)
    if branch_error is not None:
        return branch_error

    return WorktreeResult(
        "removed",
        f"removed worktree at {target} and branch {branch_name}",
        path=target,
        branch=branch_name,
        exit_code=0,
    )
