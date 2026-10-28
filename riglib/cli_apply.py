"""`rig apply` (info/commit) and `rig activate` handlers.

Context: preview-by-default; only `commit` calls `actions.run_plan` (never a forked executor).
"""
from __future__ import annotations

import argparse
import json
import os as _os
import shlex
import sys
import time
from pathlib import Path

from .cli_output import _bold, _dim, _err, _err_block, _ok, _warn
from .cli_plan_print import (
    _load_plan,
    _print_plan,
    _print_results,
    _resolve_explicit_config,
    _run_and_print_verify,
)
from .cli_profile import _apply_auto_sync_profile


def _scope_categories(only: str) -> set[str]:
    """Parse ``--only`` into the set of ACTION categories to keep.

    ``ship_env`` is a drift/status-only category (the machine env file): it has no standalone
    action — it is reconciled by whichever action OWNS it. That is the ``ship_delegator`` action
    normally, and the ``gh_ship_alias`` action in the ci-only combo (delegator disabled,
    ``ci.items.ship.gh_alias`` on). This returns only the always-safe ``ship_delegator`` alias; the
    ci-only owner is pulled in by :func:`_action_in_only_scope`, which keys on the ACTION contract
    (a ``gh_ship_alias`` action that carries ``canonical_ship``) — a bare category add would also
    drag the option-less alias action that does NOT own ``ship_env`` and could clobber a user alias.
    """
    wanted = {s.strip() for s in only.split(",")}
    if "ship_env" in wanted:
        wanted.add("ship_delegator")
    return wanted


def _action_in_only_scope(action, wanted: set[str]) -> bool:
    """Whether ``action`` survives an ``--only`` filter for the ``wanted`` categories.

    A plain category match keeps the action. The one contract-level exception: ``ship_env`` has no
    standalone action, so scoping to it must ALSO keep the ci-only owner — a ``gh_ship_alias``
    action that carries ``canonical_ship`` (only then does it reconcile the env file). The
    option-less ``gh_ship_alias`` action (normal delegator-enabled plan) does NOT own ``ship_env``
    and is correctly excluded, so ``apply --only ship_env`` can never touch an unrelated alias.
    """
    if action.category in wanted:
        return True
    if "ship_env" in wanted and action.kind == "provision_gh_ship_alias":
        return bool(str(action.options.get("canonical_ship", "")).strip())
    return False


def _refuse_without_config() -> int:
    """Fail closed: with NO config layer the empty config resolves to built-in defaults and would
    mutate HOME with no committed source of truth, so refuse and point at `rig init`."""
    print(_err("error: no rig.yaml found (and no --config / global config)."))
    print(_dim("  run `rig init` (or `rig export -o rig.yaml`) to create one first."))
    return 2


def _preview_with_tracked(plan, args: argparse.Namespace, tracked) -> int:
    from .profile import auto_sync_enabled

    rc = _apply_info(plan, args)
    if tracked is not None:
        _print_tracked_files_plan(tracked, preview=True)
    if auto_sync_enabled():
        print(_dim("  (profile.auto_sync is on: `rig apply commit` syncs the profile first)"))
    return rc


def _commit_with_tracked(plan, args: argparse.Namespace, tracked, wanted: set[str] | None) -> int:
    rc = _apply_commit(plan, args)
    if tracked is None:
        return rc
    if wanted is not None and "tracked_files" not in wanted:
        # the held-back actions still stay out of this checkout; landing waits for a run
        # whose scope includes it (said only when there is something to land)
        if tracked.discovery.landings or tracked.diverted_pending:
            print(_dim("  tracked_files: not landing (outside --only); add tracked_files to --only to land"))
        return rc
    return max(rc, _tracked_files_commit(tracked))


def cmd_apply(args: argparse.Namespace) -> int:
    from .catalog import CatalogError
    from .config import ConfigError
    from .plan import PlanError

    mode = _resolve_apply_mode(args)
    # profile.auto_sync: sync the global config BEFORE the plan is built (a pull can change the very
    # file the plan reads). Commit only — a preview mutates nothing, not even the profile.
    if mode == "commit":
        _apply_auto_sync_profile()

    try:
        plan, loaded, env = _load_plan(args.cwd, args.config)
    except (ConfigError, CatalogError, PlanError) as exc:
        print(_err_block(exc))
        return 2

    # An ancestor rig.yaml alone does not count: it contributes only task routing, which apply
    # does not consume.
    if not loaded.has_declared_config:
        return _refuse_without_config()

    # tracked_files: rig-owned COMMITTED repo files are landed on the default branch (one PR, the
    # gated ship), never written into this checkout — plan.build already held their actions back
    # in plan.diverted (riglib/tracked_files.py). Read before --only narrows plan.actions.
    tracked = _tracked_files_context(plan, loaded, env, args.config)
    wanted = _scope_categories(args.only) if args.only else None

    if wanted is not None:
        plan.actions = [a for a in plan.actions if _action_in_only_scope(a, wanted)]

    if mode == "info":
        return _preview_with_tracked(plan, args, tracked)
    return _commit_with_tracked(plan, args, tracked, wanted)


def _activate_profile_line() -> str:
    """One read-only line about the profile for the `rig activate` preview."""
    from . import profile

    if not profile.configured():
        return "profile: not configured — `rig profile init` brings your shared config, tmux and omp files"
    try:
        st = profile.status()
    except profile.ProfileError as exc:
        return f"profile: configured but not usable yet ({exc.what})"
    return f"profile: {st.url} — {st.verdict.replace('_', ' ')}; `rig activate commit` syncs it first"


def _activate_scope(args: argparse.Namespace) -> tuple[frozenset[str] | None, int | None]:
    """Parse ``--only`` into activation areas; the second item is an exit code when it is invalid."""
    from . import activate

    if not args.only:
        return None, None
    only = frozenset(part.strip() for part in args.only.split(",") if part.strip())
    unknown = sorted(only - set(activate.AREA_IDS))
    if unknown:
        print(_err(f"error: unknown area(s) for --only: {', '.join(unknown)}"), file=sys.stderr)
        print(_dim(f"  valid: {', '.join(activate.AREA_IDS)}"), file=sys.stderr)
        return only, 2
    return only, None


def _machine_plan(home: str, overrides: dict | None):
    """The plan for the machine-wide (global-only) config, optionally with an in-memory global doc."""
    plan, _loaded, _env = _load_plan(home, None, layer_overrides=overrides, include_repo=False)
    return plan


def _activate_preview(args: argparse.Namespace, act, gpath: Path, home: str) -> int:
    from . import activate

    plan = _machine_plan(home, {gpath.resolve(): act.doc})
    plan.actions = [a for a in plan.actions if a.category in act.included]
    if args.json:
        print(json.dumps({**act.to_dict(), "mode": "info", "actions": len(plan.actions),
                          "profile": _activate_profile_line()}, indent=2))
        return 0
    print(_bold("rig activate — PREVIEW (nothing was changed)"))
    print(_activate_profile_line())
    print("areas:")
    for line in activate.render(act, preview=True):
        print(line)
    print()
    _print_plan(plan, full=args.plan, preview=True, expand_notes=args.notes, preview_of="rig activate")
    print()
    print(f"  run {_bold('rig activate commit')} to execute this (preview only — nothing applied)")
    return 0


def _activate_commit_plan(act, gpath: Path, home: str):
    """Persist the activated global config (with a backup), then plan only the activated areas."""
    from .actions import fsutil
    from .config import validate
    from .state import SetupState

    if act.config_changes:
        validate(act.doc)
        out = fsutil.write_file(gpath, SetupState.from_dict(act.doc).to_yaml(), "backup")
        if out.backup is not None:
            print(_dim(f"previous global config backed up → {out.backup}"))
    plan = _machine_plan(home, None)
    plan.actions = [a for a in plan.actions if a.category in act.included]
    return plan


def _activate_apply(plan, act, args: argparse.Namespace) -> int:
    from . import activate

    print(_bold("rig activate — applying"))
    for line in activate.render(act, preview=False):
        print(line)
    rc = _apply_commit(plan, args)
    if act.unsupported:
        print()
        print(_warn("could not activate on this machine:"))
        for area in act.unsupported:
            print(f"  ✗ {area.id}: {area.detail}")
    return rc


def cmd_activate(args: argparse.Namespace) -> int:
    """`rig activate [info|commit]` — thin orchestration; probing + rendering live in :mod:`riglib.activate`."""
    from . import activate
    from .catalog import CatalogError
    from .config import ConfigError, global_config_path, read_yaml_file
    from .plan import PlanError

    commit = args.mode == "commit" and not args.dry_run
    if args.json and commit:
        print(_err("error: --json is preview-only (it cannot be combined with `commit`)"), file=sys.stderr)
        return 2
    only, bad_scope = _activate_scope(args)
    if bad_scope is not None:
        return bad_scope
    gpath = global_config_path()
    home = str(Path.home())

    try:
        if commit:
            _apply_auto_sync_profile(force=True)
        act = activate.build_activation(read_yaml_file(gpath) if gpath.is_file() else {}, only=only)
        if not commit:
            return _activate_preview(args, act, gpath, home)
        plan = _activate_commit_plan(act, gpath, home)
    except (ConfigError, CatalogError, PlanError) as exc:
        print(_err_block(exc))
        return 2
    return _activate_apply(plan, act, args)


def _tracked_files_context(plan, loaded, env, config_arg: str | None = None):
    """``riglib.tracked_files.apply_context`` for this command; ``None`` when the block is off."""
    from . import tracked_files as tf

    if not getattr(env, "is_git_repo", False):
        return None
    # the landing worktree rebuilds its plan from ITS rig.yaml: an explicit --config naming this
    # checkout's own rig.yaml must not override the landed one; any other --config applies as given
    explicit = _resolve_explicit_config(env, config_arg)
    landing_config = None if explicit is None or explicit == (env.repo_root / "rig.yaml").resolve() else str(explicit)
    return tf.apply_context(plan, loaded.data, env.repo_root, landing_config=landing_config)


def _print_tracked_files_plan(ctx, *, preview: bool) -> None:
    from . import tracked_files as tf

    tf.print_plan(ctx, preview=preview, emit=print, bold=_bold)


def _tracked_files_commit(ctx) -> int:
    from . import tracked_files as tf

    return tf.commit(ctx, build_plan=lambda p: _load_plan(str(p), ctx.landing_config)[0],
                     emit=print, ok=_ok, err=_err, dim=_dim)


def _print_tracked_files_status(plan, loaded, env, config_arg: str | None = None) -> None:
    """`rig status`: the pending tracked_files merges (informational; never changes the exit code)."""
    ctx = _tracked_files_context(plan, loaded, env, config_arg)
    if ctx is not None:
        _print_tracked_files_plan(ctx, preview=True)


def _resolve_apply_mode(args: argparse.Namespace) -> str:
    """Decide preview vs execute. Preview-by-default; `commit` (or a bare `--yes`) executes.

    ``--dry-run`` always forces a preview (back-compat: `rig apply --dry-run` never mutated). A
    bare `rig apply` (``mode is None``) is a preview UNLESS ``--yes`` is given — automation that
    said `rig apply --yes` keeps executing (commit intent).
    """
    if getattr(args, "dry_run", False):
        return "info"
    if args.mode == "commit":
        return "commit"
    if args.mode == "info":
        return "info"
    return "commit" if getattr(args, "yes", False) else "info"


def _apply_info(plan, args: argparse.Namespace) -> int:
    """Preview: build + print the plan, mutate NOTHING, point at `rig apply commit`."""
    _print_plan(
        plan,
        full=getattr(args, "plan", False),
        preview=True,
        expand_notes=getattr(args, "notes", False),
    )
    print()
    if args.mode is None:
        print(_dim("(`rig apply` is an alias for `rig apply info` — preview only, nothing applied)"))
    else:
        print(_dim("(preview only — nothing applied)"))
    print(f"  run {_bold(_apply_commit_command(args))} to execute this plan")
    return 0


def _apply_commit_command(args: argparse.Namespace) -> str:
    """The exact `rig apply commit …` line to execute THIS preview — carrying the plan-defining
    flags the user passed (`-C`, `--config`, `--only`) so the suggested command reconciles the
    SAME repo/config/scope, never a broader default plan.
    """
    parts = ["rig apply commit"]
    cwd = getattr(args, "cwd", ".")
    if cwd and cwd != ".":
        parts.append(f"-C {shlex.quote(cwd)}")
    if getattr(args, "config", None):
        parts.append(f"--config {shlex.quote(args.config)}")
    if getattr(args, "only", None):
        parts.append(f"--only {shlex.quote(args.only)}")
    return " ".join(parts)


# Action kinds whose runners do slow, live-activation work (network clones, launchctl,
# tg-ctl/schedule reloads, gh API). During a commit these get a per-phase progress line so a
# long silent tail reads as progress, not a hang.
_LIVE_ACTIVATION_KINDS = frozenset({
    "provision_tmux",
    "provision_tg_ctl",
    "provision_tcc_watch",
    "provision_tgctl_reaper",
    "provision_sysmon",
    "provision_schedule",
    "provision_spotlight",
    "provision_project_tool",
    "provision_tools",
    "provision_github_ruleset",
    "provision_github_merge",
    "provision_github_ghas",
    "provision_github_actions",
    "provision_github_browser",
    "provision_fj",  # a Homebrew install or a release download on first apply
})


def _apply_commit(plan, args: argparse.Namespace) -> int:
    """Execute the plan (today's `rig apply` behavior): per-phase progress on the slow live
    runners, a full log written to disk regardless of console verbosity, verify, then a single
    completion line that reflects the verify result."""
    from .actions import run_plan

    verbose = getattr(args, "verbose", False)
    _print_plan(
        plan,
        full=getattr(args, "plan", False),
        expand_notes=getattr(args, "notes", False),
    )
    live_phases = [a for a in plan.actions if a.kind in _LIVE_ACTIVATION_KINDS]
    if live_phases:
        cats = ", ".join(sorted({a.category for a in live_phases}))
        print(_dim(f"\napplying (live activation may take a moment: {cats})…"))

    def _on_start(action) -> None:
        # printed BEFORE the (slow) runner dispatches, so a hung clone/launchctl/gh call is
        # visible as an in-flight phase — silence during the op ≠ hang.
        if action.kind in _LIVE_ACTIVATION_KINDS:
            print(_dim(f"  → {action.category}/{action.item}…"), flush=True)

    started = time.monotonic()
    report = run_plan(plan, on_start=_on_start)
    elapsed = time.monotonic() - started
    _print_results(report, verbose=verbose, list_hint="-v")
    verify_ok = _run_and_print_verify(plan, report.results)
    log_path = _write_apply_log(plan, report, elapsed, verify_ok)
    # the completion line is the LAST thing printed and reflects verify: a green ✓ never precedes
    # a failed verify that flips the exit code to 1.
    _print_completion_line(report, elapsed, verify_ok=verify_ok, log_path=log_path)
    return 1 if (report.errors or not verify_ok) else 0


def _write_apply_log(plan, report, elapsed: float, verify_ok: bool) -> Path | None:
    """Write the FULL apply record (every action + result, regardless of console verbosity) to
    ``~/.cache/rig/apply-<UTC>.log`` and return its path. Best-effort: a log-write failure must
    never fail the apply, so any OSError degrades to ``None`` (no log path reported)."""
    import datetime

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_dir = Path(_os.path.expanduser("~/.cache/rig"))
    log_path = log_dir / f"apply-{stamp}.log"
    lines = [
        f"rig apply commit — {stamp}",
        f"on_conflict={plan.on_conflict}  actions={len(plan.actions)}  elapsed={elapsed:.2f}s",
        "",
        "PLAN:",
    ]
    lines += [f"  {a.category}/{a.item} → {a.target}" for a in plan.actions]
    if plan.notes:
        lines += ["", "NOTES:"] + [f"  {n}" for n in plan.notes]
    lines += ["", "RESULTS:"]
    lines += [f"  [{r.status}] {r.action.category}/{r.action.item}: {r.detail}" for r in report.results]
    summary = ", ".join(f"{k}={v}" for k, v in sorted(report.summary().items()))
    lines += [
        "",
        f"SUMMARY: {summary}",
        f"verify_ok={verify_ok}  errors={len(report.errors)}",
    ]
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        return None
    return log_path


def _print_completion_line(
    report, elapsed: float, *, verify_ok: bool = True, log_path: Path | None = None
) -> None:
    """The clear finish line on commit: ✓ applied N actions (C changed, M unchanged) in Xs.

    A verify FAILURE or an action error downgrades the ✓ to ✗ so the marker never lies about a
    run that exits non-zero. The full-detail log path is appended when one was written.
    """
    total = len(report.results)
    changed = report.changed
    unchanged = sum(1 for r in report.results if r.status == "skipped")
    errs = len(report.errors)
    failed = bool(errs) or not verify_ok
    icon = _err("✗") if failed else _ok("✓")
    reasons = []
    if errs:
        reasons.append(f"{errs} error(s)")
    if not verify_ok:
        reasons.append("verify FAILED")
    tail = f" — {_err(', '.join(reasons))}" if reasons else ""
    print(
        _bold(
            f"{icon} applied {total} action(s) "
            f"({changed} changed, {unchanged} unchanged) in {elapsed:.1f}s"
        )
        + tail
    )
    if log_path is not None:
        print(_dim(f"  full log: {log_path}"))
