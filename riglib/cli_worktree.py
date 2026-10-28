"""`rig worktree create|remove|verdict|discard|guard|gc` command handlers.

Context: thin glue over `riglib.worktree*`; the standardized `.worktrees/<name>` agent-worktree lifecycle.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from .cli_output import _dim, _err, _ok, _warn

if TYPE_CHECKING:
    from .detect import Environment


def cmd_worktree(args: argparse.Namespace) -> int:
    if not getattr(args, "worktree_command", None):
        args._worktree_parser.print_help()
        return 0
    if args.worktree_command == "create":
        return _cmd_worktree_create(args)
    if args.worktree_command == "remove":
        return _cmd_worktree_remove(args)
    if args.worktree_command == "verdict":
        return _cmd_worktree_verdict(args)
    if args.worktree_command == "discard":
        return _cmd_worktree_discard(args)
    if args.worktree_command == "guard":
        return _cmd_worktree_guard(args)
    if args.worktree_command == "gc":
        return _cmd_worktree_gc(args)
    args._worktree_parser.print_help()
    return 2


def _resolve_worktree_repo_env(cwd: str) -> tuple["Environment | None", int]:
    """Shared preflight for both `worktree create` and `worktree remove`.

    Returns ``(env, 0)`` on success, or ``(None, exit_code)`` after already printing the error.
    Checks the git binary FIRST: `detect_environment()` swallows a missing `git` as
    `is_git_repo=False` (see riglib/detect.py's `_git()` catching OSError), so without this check
    a missing-dependency machine gets the wrong diagnosis ("not a git repository", exit 6) instead
    of the documented missing-dependency exit (127).
    """
    import shutil

    from . import errors as errors_module
    from .detect import detect_environment

    if shutil.which("git") is None:
        print(_err("error: git is not installed (or not on PATH)"))
        return None, errors_module.EXIT_MISSING_DEP

    env = detect_environment(Path(cwd).resolve())
    if not env.is_git_repo:
        print(_err(f"error: {env.repo_root} is not a git repository"))
        return None, errors_module.EXIT_NOT_A_REPO
    return env, 0


def _cmd_worktree_create(args: argparse.Namespace) -> int:
    from . import worktree as worktree_mod

    env, exit_code = _resolve_worktree_repo_env(args.cwd)
    if env is None:
        return exit_code

    result = worktree_mod.create(
        env.repo_root, args.name, branch=args.branch, base_ref=args.base_ref
    )
    if result.status == "error":
        print(_err(f"error: {result.message}"))
        return result.exit_code

    print(_ok(result.message))
    if result.exclude_note:
        print(_dim(f"  {result.exclude_note}"))
    if result.path is None:  # every "created" result carries its path; never install blind
        return result.exit_code
    return _worktree_bun_install(env.repo_root, result.path)


def _worktree_bun_install(repo_root: Path, worktree_path: Path) -> int:
    """Install a new bun worktree's deps (``bun.worktree_install``), as the repo's bunfig says.

    Runs after the worktree exists, so a config or install problem never undoes the create: a
    skip prints the manual command, a failed install exits 2 with the command to finish it. Only
    the GLOBAL config can turn the install on; a repo rig.yaml can only turn it off.
    """
    from . import bun_store
    from . import errors as errors_module
    from .config import ConfigError

    if not bun_store.is_bun_project(worktree_path):
        return 0
    manual = bun_store.manual_command(worktree_path)
    try:
        decision = bun_store.resolve_install_decision(repo_root)
    except ConfigError as exc:
        print(_warn(f"  deps not installed: rig config did not load ({exc}) — run {manual}"))
        return 0
    if decision != "install":
        # not installing — but a committed write-through link is as dangerous for the install
        # the user (or the repo's own tooling) runs next, so say so on every path
        link = bun_store.write_through_link(worktree_path)
        if link is not None:
            print(_warn(f"  {bun_store.write_through_message(link, manual)}"))
        elif decision == "hint":
            print(_dim(f"  bun repo: install deps with {manual} (never symlink node_modules)"))
        return 0
    outcome = bun_store.install_worktree_deps(worktree_path)
    if outcome.status == "installed":
        print(_ok(f"  {outcome.message}"))
        return 0
    if outcome.status == "skipped":
        print(_warn(f"  {outcome.message}"))
        return 0
    print(_err(f"  error: {outcome.message}"))
    return errors_module.EXIT_CONFIG  # diagnosed (lockfile, network, script), not a rig crash


def _process_cwd_or_none() -> Path | None:
    """The process cwd, or ``None`` when it was deleted out from under the shell (the very
    "worktree directory removed by hand" case `rig worktree remove` exists to recover)."""
    try:
        return Path.cwd()
    except OSError:
        return None


def _cmd_worktree_remove(args: argparse.Namespace) -> int:
    from . import worktree as worktree_mod

    env, exit_code = _resolve_worktree_repo_env(args.cwd)
    if env is None:
        return exit_code

    result = worktree_mod.remove(
        env.repo_root, args.target, cwd=_process_cwd_or_none(), force=args.force, fetch=not args.no_fetch
    )
    if result.status == "error":
        print(_err(f"error: {result.message}"))
        return result.exit_code

    print(_ok(result.message))
    return 0


def _cmd_worktree_verdict(args: argparse.Namespace) -> int:
    """Read-only landed-removal verdict for a worktree, a local branch, a scratch ref or (``--remote``) a
    remote branch. Exit 0 = SAFE, 10 = UNSAFE, 2 = target is none of those (the message says what it is)."""
    from . import errors as errors_module
    from . import removal_explain
    from . import worktree as worktree_mod

    env, exit_code = _resolve_worktree_repo_env(args.cwd)
    if env is None:
        return exit_code

    verdict, error = worktree_mod.judge_target(
        env.repo_root, args.target, remote=args.remote, cwd=_process_cwd_or_none(), fetch=not args.no_fetch
    )
    if verdict is None:
        print(_err(f"error: {error.message if error else f'cannot resolve {args.target!r}'}"))
        return error.exit_code if error else errors_module.EXIT_CONFIG

    if args.json:
        print(json.dumps(verdict.to_dict()))
    elif verdict.safe:
        print(verdict.render())
    else:
        print(removal_explain.explain_denial([verdict]))
    return 0 if verdict.safe else errors_module.EXIT_REMOVAL_UNSAFE


def _cmd_worktree_discard(args: argparse.Namespace) -> int:
    """The owner-approved drop of work the landing proofs cannot clear (see :mod:`riglib.worktree_discard`)."""
    from . import worktree_discard

    env, exit_code = _resolve_worktree_repo_env(args.cwd)
    if env is None:
        return exit_code

    result = worktree_discard.discard(
        env.repo_root, args.targets, reason=args.reason, remote=args.remote,
        cwd=_process_cwd_or_none(), fetch=not args.no_fetch,
    )
    if result.status == "discarded":
        print(_ok(result.message))
    else:
        print(_err(result.message) if result.status == "error" else result.message)
    return result.exit_code


def _cmd_worktree_guard(args: argparse.Namespace) -> int:
    """PreToolUse removal-guard backend: one bash command on stdin → one JSON decision on stdout.
    Always exits 0 once a decision is printed; an unreadable stdin is itself a ``deny``."""
    from . import removal_guard

    try:
        command = sys.stdin.read()
    except (OSError, ValueError) as exc:
        decision = removal_guard.GuardDecision("deny", f"removal guard: DENY — could not read the command from stdin ({exc})")
    else:
        decision = removal_guard.evaluate_command(command, Path(args.cwd), fetch=not args.no_fetch)
    print(json.dumps(decision.to_dict()))
    return 0


def _resolve_gc_repo_roots(repo_arg: str | None) -> tuple[list[Path], int]:
    """``(repo_roots, exit_code)``. An EMPTY ``repo_roots`` means there is nothing left to
    process — ``exit_code`` is 0 for a genuine no-op (e.g. an empty registry, already reported)
    or non-zero for a hard failure (bad ``--repo``, unreadable registry). A NON-empty
    ``repo_roots`` can still carry a non-zero ``exit_code`` — the registry fan-out skips and
    reports any entry with an invalid path individually (see below) without dropping the OTHER,
    valid entries; the caller must fold that code into its own aggregate rather than treat it as
    an abort signal.

    ``--repo`` given: resolve it exactly like `worktree create`/`remove`'s ``-C`` (same git-repo
    preflight, same exit codes). ``--repo`` omitted: fan out to every repo the machine-local
    :mod:`riglib.repository_registry` already knows about — the SAME multi-repo discovery `rig
    config-web` uses, per this repo's own AGENTS.md; gc does not invent a second one.

    Checked with ``is not None``, NOT truthiness — a review-caught gap: `if repo_arg:` treats an
    EXPLICIT empty string (`--repo ''`, or a shell variable used for `--repo` that happened to be
    unset/empty) the SAME as "omitted", silently taking the machine-wide destructive registry
    fan-out instead of the single-repo path the caller actually asked for. An empty string is
    handed to `_resolve_worktree_repo_env` like any other explicit value, so it fails the same way
    any other bad path would (`Path("").resolve()` resolves to the current directory, which the
    usual git-repo preflight then validates or rejects on its own terms) — never silently widened
    to "every repo".
    """
    if repo_arg is not None:
        env, exit_code = _resolve_worktree_repo_env(repo_arg)
        if env is None:
            return [], exit_code
        return [env.repo_root], 0

    from . import errors as errors_module
    from .repository_registry import RegistryError, RepositoryRegistry

    try:
        registry = RepositoryRegistry.load()
    except RegistryError as exc:
        print(_err(f"error: {exc}"))
        return [], 2
    try:
        entries = registry.select()
    except TypeError as exc:
        # `select()` sorts by `entry.path` (and, for a root filter, wraps it in `Path(...)`) —
        # review-caught (Codex, round 22): a hand-edited/corrupted registry carrying a non-string
        # `path` on ANY entry (`RepositoryRegistry.load()` only type-checks the tag arrays, per
        # its own docstring) makes that comparison/conversion raise `TypeError` for the WHOLE
        # call, before this function's own per-entry path validation below ever gets a chance to
        # skip just the one bad row. Fixing `select()` itself is out of scope here (shared code
        # used by every registry consumer, not something this ticket owns) — degrade the same way
        # an unreadable registry file already does, rather than letting the exception escape as an
        # unhandled crash.
        print(_err(f"error: repository registry is malformed ({exc}) — pass --repo <path> "
                    "directly, or fix/regenerate the registry"))
        return [], errors_module.EXIT_NOT_A_REPO
    if not entries:
        print(_warn(
            "no rig-managed repositories found in the registry — pass --repo <path>, or run "
            "`python -m riglib.repository_registry discover --root <dir> --write` to populate it"
        ))
        return [], 0

    repo_roots: list[Path] = []
    worst_code = 0
    for entry in entries:
        # A structurally-valid JSON registry can still carry a non-string or empty `path` (a
        # hand-edited/corrupted registry file, same class `RepositoryRegistry.load()` only
        # type-checks the tag arrays for — see `riglib/config_web_scopes.py`'s identical guard) —
        # review-caught (Codex, round 22): `Path(None)` raised `TypeError` before ANY valid repo
        # was processed, aborting the whole fan-out; `Path("")` resolves to the CURRENT DIRECTORY,
        # which could then run a destructive `--yes` fan-out against whatever unrelated repo `rig`
        # happens to be invoked from — never validated as the actually-registered repo. Skip and
        # report per-entry, don't crash or silently widen to "wherever this process's cwd is".
        if not isinstance(entry.path, str) or not entry.path:
            print(_warn(f"registry entry {entry.id!r} has an invalid path ({entry.path!r}) — skipped"))
            worst_code = max(worst_code, errors_module.EXIT_NOT_A_REPO)
            continue
        repo_roots.append(Path(entry.path))
    return repo_roots, worst_code


def _run_worktree_gc_for_repo(args: argparse.Namespace, repo_root: Path) -> int:
    from . import errors as errors_module
    from . import worktree_gc

    # Re-preflight EVERY repo_root through the same git-installed/is-a-repo check `--repo` itself
    # goes through — load-bearing for the registry fan-out path (`--repo` omitted): a stale/
    # hand-edited registry entry pointing at a non-repo or a since-deleted path would otherwise
    # reach `git worktree list` directly and surface as a generic exit 2, not the documented exit
    # 6/127 the CLI epilog promises for "the given/discovered repo is not a git repository" /
    # "git is not installed" (a real cross-file consistency gap a review round caught).
    env, exit_code = _resolve_worktree_repo_env(str(repo_root))
    if env is None:
        return exit_code

    try:
        report = worktree_gc.run_gc(
            env.repo_root,
            dry_run=args.dry_run,
            yes=args.yes,
            include_stale=args.include_stale,
            older_than_days=args.older_than_days,
        )
    except worktree_gc.WorktreeGcError as exc:
        print(_err(f"error: {repo_root}: {exc}"))
        return exc.exit_code
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see below
        # `run_gc` reaches filesystem/subprocess calls beyond its own documented
        # `WorktreeGcError` (e.g. a `PermissionError` from `Path.exists()`/`is_symlink()` on a
        # directory whose permissions changed underneath it, or a `RuntimeError` from `.resolve()`
        # hitting a symlink loop). Catching only `WorktreeGcError` here would let ANY of those
        # abort the WHOLE multi-repo registry fan-out — one unreadable repo silently skipping
        # every repo after it in the loop, directly contradicting this module's own "one entry's
        # failure must not skip the rest" contract (review-caught). Degrade to a reported,
        # per-repo failure instead.
        print(_err(f"error: {repo_root}: unexpected failure: {exc}"))
        return errors_module.EXIT_INTERNAL

    print(worktree_gc.render_report(report))

    if any(entry.remove_error for entry in report.entries):
        return errors_module.EXIT_CONFIG
    return 0


def _cmd_worktree_gc(args: argparse.Namespace) -> int:
    if args.older_than_days <= 0:
        print(_err(f"error: --older-than-days must be positive, got {args.older_than_days}"))
        return 2

    repo_roots, exit_code = _resolve_gc_repo_roots(args.repo)
    if not repo_roots:
        # `exit_code` alone can no longer gate this: a registry fan-out with SOME invalid entries
        # returns a non-zero `exit_code` (folded into the aggregate below) ALONGSIDE non-empty
        # `repo_roots` for the entries that were fine — only an EMPTY result (every existing
        # failure path already returns one) means there is truly nothing left to process.
        return exit_code

    worst_exit_code = exit_code
    for repo_root in repo_roots:
        rc = _run_worktree_gc_for_repo(args, repo_root)
        worst_exit_code = max(worst_exit_code, rc)
    return worst_exit_code
