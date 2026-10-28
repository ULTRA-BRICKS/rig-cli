"""`rig status` — detect and report drift in both directions (config <-> disk).

Context: orchestrates the area, drift and service renderers; also owns the core.bare handling shared with `rig doctor`.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from .cli_apply import _print_tracked_files_status
from .cli_output import _bold, _dim, _err, _err_block, _ok, _warn
from .cli_plan_print import (
    _include_repo_config,
    _load_plan,
    _print_hook_bridge_coverage_gaps,
    _resolve_explicit_config,
)
from .cli_status_areas import (
    _known_summary_note,
    _render_area_summary,
    _render_drift_by_layer,
    _render_known_by_layer,
)
from .cli_status_services import (
    _print_harness_mode_status,
    _print_schedule_status,
    _print_stale_worktree_status,
    _print_sysmon_status,
    _print_tg_ctl_status,
    _print_tgctl_reaper_status,
    _print_tmux_autosave_status,
    _print_tmux_pty_pressure_status,
)


def _scan_core_bare() -> list:
    """Scan the cwd repo + its worktrees for the core.bare corruption class.

    A working checkout with ``core.bare=true`` silently breaks every git op there + ship's
    main-refresh; this surfaces it as a structured finding with the one-line fix. Returns ``[]``
    when cwd is not a repo / git is absent (the scanner degrades to nothing, never raises).
    """
    from .core_bare import scan_repo

    try:
        cwd = Path.cwd()
    except OSError:
        return []  # cwd was deleted out from under us — honor the "never raises" contract
    return scan_repo(cwd)


def _print_non_git_note() -> None:
    """The one-line 'this is not a git repository' note shown under the status header.

    Single source for the wording so the normal status path and the catalog-failed fallback
    (``_print_non_git_status``) can never drift apart — the smoke greps for "not a git
    repository", and only one of two phrasings would be a regression waiting to happen.
    """
    print(_dim("  (not a git repository — repo layer / rig.yaml N/A here)"))


def _print_non_git_status(env, config: str | None = None) -> int:
    """Render a minimal `rig status` for a non-git dir when the catalog couldn't be resolved.

    The catalog scan feeds the REPO layer (what this repo would install); a non-git dir has no
    repo layer, so its failure is irrelevant here. We still owe the user the header + the
    explicit "not a git repository" note (never the "should be committed" nag). Returns 0 — a
    non-git dir is a valid place to ask "what's my status?", not an error.

    Config loading precedes the (failed) catalog scan, so the layer provenance is still
    reportable; we re-load it here rather than thread `loaded` through the exception path.
    """
    from .config import ConfigError, load

    print(_bold("rig status"))
    print(f"  repo: {env.repo_root}")
    print(f"  toolchain: {env.stack}  type: {env.project_type}")
    layers = "(none — built-in defaults)"
    try:
        explicit = _resolve_explicit_config(env, config)
        loaded = load(
            env.repo_root,
            explicit_config=explicit,
            include_repo=_include_repo_config(
                env,
                explicit,
                allow_repo_autodiscovery_in_non_git=False,
            ),
        )
        layers = ", ".join(loaded.layers) or layers
    except ConfigError:
        pass  # a malformed config still shouldn't stop us reporting "not a git repository"
    print(f"  config layers: {layers}")
    _print_non_git_note()
    return 0


def _print_stack_preset(loaded) -> None:
    """Print the declared stack preset (or the soft-require warning + detected guess).

    Distinct from the toolchain line above: this is the by-stack curation axis. A declared stack
    is shown plainly; an absent one prints the migration-phase soft-require warning so the user
    knows to set it (per-repo stack is mandatory by policy, warned-not-enforced for now)."""
    from .config import stack_requirement_warning

    if loaded.stack:
        print(f"  stack-preset: {loaded.stack}")
        return
    warning = stack_requirement_warning(loaded)
    if warning:
        print(_warn(f"  {warning}"))


def _load_status_plan(args: argparse.Namespace):
    """``(plan, loaded, env)`` for `rig status`, or the exit code (after printing why) when it cannot be built."""
    from .catalog import CatalogError
    from .config import ConfigError
    from .plan import PlanError

    try:
        return _load_plan(
            args.cwd,
            args.config,
            allow_repo_autodiscovery_in_non_git=False,
        )
    except CatalogError as exc:
        # The catalog only matters for the REPO layer (what this repo would install). A non-git
        # dir (e.g. ~) has no repo layer at all, so an unresolved agent-tools checkout must NOT
        # mask the one thing status owes that user: "you're not in a git repository". Detecting
        # the env is independent of (and cheap relative to) the catalog scan that just failed.
        from .detect import detect_environment

        env = detect_environment(Path(args.cwd).resolve())
        if not env.is_git_repo:
            return _print_non_git_status(env, args.config)
        # A real repo with a broken/missing agent-tools source IS a genuine config failure.
        print(_err(f"error: {exc}"))
        return 2
    except (ConfigError, PlanError) as exc:
        print(_err_block(exc))
        return 2


def _print_status_header(env, loaded) -> None:
    print(_bold("rig status"))
    print(f"  repo: {env.repo_root}")
    print(f"  toolchain: {env.stack}  type: {env.project_type}")
    _print_stack_preset(loaded)
    cfg_src = ", ".join(loaded.layers) or "(none — built-in defaults)"
    print(f"  config layers: {cfg_src}")

    # The REPO layer only exists inside a git repository. In a non-git dir (e.g. ~) there is no
    # repo at all — do NOT nag "no rig.yaml, should be committed" (that advice applies only in a
    # real repo). Show that the repo layer is N/A and report the GLOBAL layer only.
    if not env.is_git_repo:
        _print_non_git_note()
    elif loaded.repo_path is None:
        # A REAL repo with no committed rig.yaml: make the fix PROMINENT, not a one-liner
        # buried above a long drift dump.
        print()
        print(_warn(_bold("  ▸ no committed rig.yaml in this repository")))
        print(_warn("    rig.yaml is the committed source of truth for this repo's setup."))
        print(_ok("    fix: run `rig init` to create one"))


def _print_service_details(effective_plan, report, plan, loaded, env, config_arg: str | None) -> None:
    """Richer detail for the GLOBAL areas the summary counts but can't fully describe: the
    model-freshness schedule (the daily cron time), the tg-ctl inbound daemon (its launchd boot
    label / unsupported-off-darwin state), sysmon, tmux and the harness mode."""
    _print_schedule_status(effective_plan, report)
    _print_tg_ctl_status(effective_plan, report)
    _print_tgctl_reaper_status(effective_plan, report)
    _print_sysmon_status(effective_plan, report)
    _print_tmux_autosave_status(effective_plan)
    _print_tmux_pty_pressure_status(effective_plan)
    _print_harness_mode_status(effective_plan, report)
    if env.is_git_repo:
        _print_stale_worktree_status(env.repo_root)
        _print_tracked_files_status(plan, loaded, env, config_arg)


def _print_dead_target_details(dead_targets: list) -> None:
    """A hook command in the harness settings.json that points at a file gone from disk (the
    dead-rtk-hook case) surfaces PROACTIVELY here, before it bites at runtime as a generic
    "PreToolUse error". Independent of config↔disk drift."""
    if not dead_targets:
        return
    print()
    print(_err(_bold(f"  ▸ missing targets ({len(dead_targets)}) — config points at files that are gone:")))
    for f in dead_targets:
        print(f"    {_err('✗')} {f.what}")
        print(f"      {_dim('why:')} {f.why}")
        print(f"      {_ok('fix:')} {f.fix}")


def _print_drift_detail(report, loaded, env) -> None:
    # render the full drift report whenever there IS drift — the user must SEE every problem,
    # regardless of which exit code we ultimately surface.
    _render_drift_by_layer(report, loaded, env)
    _render_known_by_layer(report, loaded, env)
    # LOUD reassurance: apply NEVER deletes on-disk-not-declared items. A user must not fear
    # `rig apply` will nuke a hand-added skill (it won't — extras are surfaced, never removed).
    print()
    print(_ok("  ✔ safe: `rig apply` NEVER deletes on-disk extras — items present on disk but"))
    print(_ok("    not declared in any layer are left for you to decide. apply only ADDS/UPDATES."))
    print(_dim("\n  run `rig apply commit` to converge config→disk (extras above are left as-is)"))


def _status_exit_code(report, dead_targets: list) -> int:
    """EXIT-CODE PRECEDENCE — a dead target OUTRANKS ordinary drift. A missing hook script will
    FAIL at runtime (a generic "PreToolUse error"); drift is merely "config and disk disagree".
    The more-actionable, higher-severity class wins so a script following the stable exit-code
    contract sees EXIT_MISSING_TARGET (5) even when drift is also present — the drift exit (3)
    must never MASK it. Both are printed above; only the exit code is single-valued."""
    from . import errors

    if not dead_targets:
        return errors.EXIT_DRIFT
    if not report.in_sync:
        print(_dim(
            f"\n  exit: missing-target ({errors.EXIT_MISSING_TARGET}) takes precedence over "
            f"config↔disk drift ({errors.EXIT_DRIFT}) — the dead reference fails at runtime; "
            "fix it first, then re-run."
        ))
    else:
        print(_dim("\n  (no config↔disk drift, but a missing target above needs attention)"))
    return errors.EXIT_MISSING_TARGET


def cmd_status(args: argparse.Namespace) -> int:
    from .drift import compute_drift_report

    loaded_plan = _load_status_plan(args)
    if isinstance(loaded_plan, int):
        return loaded_plan
    plan, loaded, env = loaded_plan
    _print_status_header(env, loaded)

    # compute_drift_report() partitions plan.actions down to GLOBAL-only itself when
    # env.is_git_repo is False (mirrors what `rig apply` skips outside git) — see its docstring in
    # riglib/drift.py. It does NOT mutate the plan we passed in — `scan.plan` is the EFFECTIVE
    # plan drift was computed against, and every render call below must use THAT, not the
    # original `plan`, to see the same GLOBAL-only view in the non-git case.
    scan = compute_drift_report(plan, loaded, env)
    report, dead_targets, effective_plan = scan.report, scan.dead_targets, scan.plan
    if not env.is_git_repo and scan.repo_actions_dropped:
        print(_dim(
            "  (repo-scoped areas are N/A outside a git repository: "
            f"{scan.repo_actions_dropped} action(s) not evaluated)"
        ))
    # AREA SUMMARY — the headline: every reconciled area (grouped by layer) with its in-sync vs
    # drift counts, so the user sees the FULL picture of what rig manages, not a skill-dominated
    # wall of drift lines. Printed in BOTH the in-sync and drift cases; the per-item drift dump
    # below is the detail. Printed FIRST so it is the headline, with the richer per-area detail
    # lines following it.
    _render_area_summary(effective_plan, report, env, extra_configured_actions=scan.dropped_ship_delegator,
                         held_back_actions=getattr(plan, "diverted", []) if env.is_git_repo else ())

    # A harness kind with zero agents-hooks bridge coverage (currently pi/commandcode) has NO
    # register_hook_bridge action at all, so the area summary line above can only say
    # "not configured" — indistinguishable from a harness block the user simply never touched.
    # Only the coverage-gap notes print here (never every informational note): this is a
    # targeted call-out, not `_print_notes`.
    _print_hook_bridge_coverage_gaps(effective_plan)
    _print_service_details(effective_plan, report, plan, loaded, env, args.config)
    _print_dead_target_details(dead_targets)

    # KNOWN items (on disk, not rig-managed, origin named) are informational: printed AFTER the
    # problems, on a clean machine too, and never part of the exit-code decision (rig-cli#357).
    if report.in_sync:
        _render_known_by_layer(report, loaded, env)
        if not dead_targets:
            print(_ok(f"\n  in sync — config and disk agree{_known_summary_note(report)}"))
            return 0
    else:
        _print_drift_detail(report, loaded, env)
    return _status_exit_code(report, dead_targets)


def _handle_core_bare(do_fix: bool) -> bool:
    """Report (and, when ``do_fix``, repair) core.bare corruption in the cwd repo + worktrees.

    Returns True iff an UNFIXED corruption remains — the caller folds that into a non-zero exit
    (the repo-corrupt class takes precedence over a mere missing dependency). A clean scan prints
    nothing and returns False.
    """
    from .core_bare import finding_to_error, fix_core_bare

    findings = _scan_core_bare()
    if not findings:
        return False
    print(_err(_bold(f"\n  ▸ corrupted git config ({len(findings)}) — a working checkout claims core.bare=true:")))
    unfixed = False
    for finding in findings:
        err = finding_to_error(finding)
        print(f"    {_err('✗')} {err.what}")
        print(f"      {_dim('why:')} {err.why}")
        if do_fix and fix_core_bare(finding):
            print(f"      {_ok('fixed:')} set core.bare=false on {finding.path}")
            continue
        # not repaired: show the manual command. Suggest `--fix` only when it was NOT already
        # tried (a failed --fix would just fail again — don't advise re-running it). The command
        # writes the LOCAL scope; if `true` is sourced from a worktree/include scope it won't bite,
        # so always note that possibility rather than imply the one command is guaranteed.
        if do_fix:
            hint = f"{err.fix}   (if this doesn't help, `true` comes from a --worktree/[include] scope — clear it there)"
        else:
            hint = f"{err.fix}   (or re-run `rig doctor --fix`)"
        print(f"      {_ok('fix:')} {hint}")
        unfixed = True
    return unfixed
