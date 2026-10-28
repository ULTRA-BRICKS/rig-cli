"""Per-service status lines for `rig status` (schedule, tg-ctl, sysmon, tmux, worktrees, deps).

Context: each `_print_*_status` reads one plan action and prints one compact block.
"""
from __future__ import annotations

import json
import os as _os
import subprocess
import time
from pathlib import Path

from .cli_output import _bold, _dim, _dim_cmd, _err, _ok, _warn
from .harness_mode import harness_mode_rows


def _print_schedule_status(plan, report) -> None:
    """Report the model-freshness daily schedule: installed / drifted / not configured."""
    drifted = [d for d in report.items if d.category == "models"]
    sched_actions = [a for a in plan.actions if a.kind == "provision_schedule"]
    if not sched_actions:
        # no action: either an explicit opt-out (quiet) or no models: block at all, which drift
        # reports with the enabling command (rig-cli#721)
        if drifted:
            print(f"\n  model-freshness schedule: {_warn('not configured')}  " + _dim(f"({drifted[0].detail})"))
        return
    action = sched_actions[0]
    opts = action.options
    when = f"{int(opts.get('hour', 12)):02d}:{int(opts.get('minute', 0)):02d}"
    platform = opts.get("platform", "?")
    if drifted:
        state = _warn(f"drifted ({drifted[0].detail})")
    else:
        state = _ok("installed")
    print(f"\n  model-freshness schedule: {state}  "
          + _dim(f"(daily {when}, {platform}, '{opts.get('label', '')}')"))


def _print_tg_ctl_status(plan, report) -> None:
    """Report the tg-ctl inbound-daemon boot agent (GLOBAL): installed / drifted / disabled / unsupported.

    A per-machine LaunchAgent, so it lives in the GLOBAL section of status (not per-repo). Resolves
    the desired state through the SAME plan builder apply/drift use (so the label/boot flag never
    drift). Off macOS the provisioner is a no-op, so status says 'unsupported' — never a misleading
    'installed' (codex P2). Stays silent only when no ``provision_tg_ctl`` action is in the plan
    (default-on, so that is unusual — keeps the helper defensive)."""
    from .actions.tg_ctl_action import tg_ctl_plan_from_action
    from .drift import _on_darwin

    tg_actions = [a for a in plan.actions if a.kind == "provision_tg_ctl"]
    if not tg_actions:
        return
    tg = tg_ctl_plan_from_action(tg_actions[0])  # shared resolution → boot_label / boot_enabled
    if not _on_darwin():
        state = _dim("unsupported (macOS-only; no-op off darwin)")
    elif not tg.boot_enabled:
        state = _dim("disabled (tg_ctl.boot=false)")
    else:
        drifted = [d for d in report.items if d.category == "tg_ctl" and d.direction != "extra"]
        state = _warn(f"drifted ({drifted[0].detail})") if drifted else _ok("installed")
    print(f"\n  [GLOBAL] tg-ctl inbound daemon: {state}  " + _dim(f"(launchd boot agent, '{tg.boot_label}')"))


def _print_sysmon_details(status: dict) -> None:
    """The daemon's last-read numbers, current findings and any open incident."""
    sample = status.get("sample") or {}
    if sample:
        gb = 1024 ** 3
        print(_dim(
            f"    load {sample['load1']:.1f}/{sample['ncpu']} CPU · wired {(sample.get('wired') or 0) / gb:.1f}"
            f"/{sample['mem_total'] / gb:.1f} GB · reclaimable {(sample.get('reclaimable') or 0) / gb:.1f} GB · "
            f"swap {sample['swap_used'] / gb:.1f}/{sample['swap_total'] / gb:.1f} GB · {sample['proc_total']} procs"
        ))
    for f in status.get("findings") or []:
        line = f"    {f['severity'].upper()} {f['key']}: {f['summary']}"
        print(_warn(line) if f["severity"] == "critical" else _dim(line))
    if status.get("open_incident"):
        print(_warn(f"    open incident {status['open_incident']} — rig sysmon incidents --show {status['open_incident']}"))


def _print_sysmon_status(plan, report) -> None:
    """Report the sysmon resource monitor (GLOBAL): service state from drift plus the daemon's
    LIVE health from its heartbeat — the numbers it last read and what it currently flags — so
    `rig status` answers "is the machine being watched, and is it healthy right now?"."""
    from .actions.sysmon_action import sysmon_plan_from_action
    from .sysmon_daemon.cli import health
    from .sysmon_daemon.config import load

    actions = [a for a in plan.actions if a.kind == "provision_sysmon"]
    if not actions:
        return
    mon = sysmon_plan_from_action(actions[0])
    drifted = [d for d in report.items if d.category == "sysmon" and d.direction != "extra"]
    h = health(load(mon.config_path))
    if drifted:
        state = _warn(f"drifted ({drifted[0].detail})")
    elif h["state"] == "running":
        state = _ok(f"running (heartbeat {h['age_s']:.0f}s ago)")
    else:
        state = _warn(h["state"])
    print(f"\n  [GLOBAL] sysmon resource monitor: {state}  " + _dim(f"('{mon.label}', {mon.config_dir})"))
    _print_sysmon_details(h["status"] or {})


def _print_tgctl_reaper_status(plan, report) -> None:
    """Report the tgctl-reaper periodic orphan-sweep (GLOBAL): installed / drifted / unsupported.
    Mirrors :func:`_print_tg_ctl_status`'s shape (full drift/status parity, unlike tcc_watch's
    documented "known gap: no rig status drift line yet") minus a `boot`-like secondary knob:
    ``tgctl_reaper.enabled: false`` (unlike tg_ctl's ``boot``) means NO action is ever emitted for
    the plan at all (mirrors ``tcc_watch``), so there is no distinct "installed but disabled"
    state to render here — the general area-summary "not configured" line already covers that.

    A per-machine LaunchAgent, so it lives in the GLOBAL section of status (not per-repo).
    Resolves the desired state through the SAME plan builder apply/drift use (so the label/
    interval never drift). Off macOS the provisioner is a no-op, so status says 'unsupported' —
    never a misleading 'installed'. Stays silent only when no ``provision_tgctl_reaper`` action
    is in the plan (default-on, so that is unusual — keeps the helper defensive)."""
    from .actions.tgctl_reaper_action import tgctl_reaper_plan_from_action
    from .drift import _on_darwin

    reaper_actions = [a for a in plan.actions if a.kind == "provision_tgctl_reaper"]
    if not reaper_actions:
        return
    reaper = tgctl_reaper_plan_from_action(reaper_actions[0])  # shared resolution → label/interval
    if not _on_darwin():
        state = _dim("unsupported (macOS-only; no-op off darwin)")
    else:
        drifted = [d for d in report.items if d.category == "tgctl_reaper" and d.direction != "extra"]
        state = _warn(f"drifted ({drifted[0].detail})") if drifted else _ok("installed")
    print(f"\n  [GLOBAL] tgctl-reaper orphan sweep: {state}  "
          + _dim(f"(launchd StartInterval={reaper.interval_seconds}s, '{reaper.label}')"))


def _print_stale_worktree_status(repo_root: Path) -> None:
    """Reuse `worktree_gc`'s classifier (dry-run, no removal, no disk-size walk) to report a
    one-line stale-worktree summary for the current repo.

    Best-effort by design: `rig status` must stay usable even when the underlying checks can't
    run (no `gh` auth, no network, `pgrep`/`lsof` unavailable, a repo with a huge worktree list
    timing out) — a failure here is swallowed, not surfaced as a status error, mirroring the same
    resilience-over-completeness call `_read_rig_metadata` makes in repository_registry.py.

    `RIG_STATUS_SKIP_WORKTREE_GC=1` skips this check entirely (mirrors the `RIG_*_DRY_RUN`
    opt-outs for other live/network side effects, e.g. `RIG_TMUX_DRY_RUN`) — a repo with linked
    worktrees still pays for one `gh pr list` + one `pgrep`/`lsof` snapshot even after the
    per-worktree batching fix, and a fully offline/no-`gh` environment may want to skip that
    outright rather than rely on the best-effort degrade-and-warn path.
    """
    if _os.environ.get("RIG_STATUS_SKIP_WORKTREE_GC"):
        return
    from . import worktree_gc

    try:
        counts = worktree_gc.stale_worktree_counts(repo_root)
    except Exception:
        return
    stale_total = sum(counts.get(c, 0) for c in worktree_gc.STALE_CLASSIFICATIONS)
    if stale_total == 0:
        return
    breakdown = ", ".join(
        f"{counts[c]} {c}" for c in worktree_gc.STALE_CLASSIFICATIONS_ORDERED if counts.get(c)
    )
    print(_warn(f"  {stale_total} stale worktree(s) ({breakdown}) — run `rig worktree gc` to review"))


def _print_harness_mode_status(plan, report) -> None:
    """One line per configured harness kind: the auto/permission-mode key rig manages (or cannot).

    The harness area summary counts the writes; this names them — which key, which file, which
    value — and, for a kind with NO such setting (pi/commandcode) or one whose key is owned by the
    permissions approval action (omp), says so explicitly instead of leaving a silent gap
    (rig-cli#355: "drift is surfaced, never silently reconciled"). Silent without a harness block.
    """
    rows = harness_mode_rows(plan)
    if not rows:
        return
    drifted = {(d.category, d.item) for d in report.items}
    print(_bold("\n  harness auto-mode per kind:"))
    for row in rows:
        if row.value is None:
            state = _dim(row.note)
        else:
            state = f"{row.key} = {row.value!r} in {row.path}"
            state += "  " + (_err("drift") if (row.category, row.item) in drifted else _ok("in sync"))
            if row.note:
                state += _dim(f"  ({row.note})")
        print(f"    {row.kind:<12} {state}")


def _read_autosave_health(health_path: Path) -> tuple[float | None, str | None]:
    """``(mtime, result)`` of the autosave health record; ``(None, None)`` when absent or unreadable."""
    if not health_path.is_file():
        return None, None
    try:
        health_mtime = health_path.stat().st_mtime
        data = json.loads(health_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None  # unreadable/corrupt → treat as missing (flag it)
    # a dict WITHOUT a "result" key (or "result": null) must stay None, not the string
    # "None" — str(None) is truthy and would print a confusing `result=None` note on the
    # exact corrupt/partial-write path this feature exists to surface.
    raw_result = data.get("result") if isinstance(data, dict) else None
    return health_mtime, str(raw_result) if raw_result is not None else None


def _autosave_state_text(verdict) -> str:
    # `disabled` is filtered upstream (the autosave_enabled early-return), so it never reaches here;
    # map the reachable states explicitly and fail closed on anything unforeseen rather than silently
    # mislabel a future/leaked state as "no health record".
    if verdict.state == "ok":
        return _ok(f"fresh ({verdict.detail})")
    if verdict.state == "stale":
        return _warn(f"STALE ({verdict.detail})")
    if verdict.state == "unhealthy":
        return _warn(f"UNHEALTHY ({verdict.detail})")
    if verdict.state == "missing":
        return _warn(f"no health record ({verdict.detail})")
    raise AssertionError(f"unhandled autosave freshness state: {verdict.state!r}")


def _print_tmux_autosave_status(plan) -> None:
    """Report the independent tmux autosave agent's FRESHNESS (the observability half of #138).

    This is an ADVISORY runtime-health line, NOT config↔disk drift: a stale save is not something
    ``rig apply`` converges (the plist/script may be byte-perfect while the agent silently stopped
    firing), so it is surfaced here — printed under the area summary like the schedule / tg-ctl
    detail lines — and deliberately does NOT change the status exit code. It gives an OWNER to the
    question "is the newest save fresh?" that went unanswered for the month-long silent death.

    Reads the health-state file the wrapper atomically rewrites every run; ``assess_autosave_freshness``
    turns its mtime into a verdict. Silent when the saver isn't in the plan (no tmux action) or is
    disabled — no noise for repos that don't manage tmux persistence.
    """
    from riglib.actions.tmux import tmux_plan_from_action
    from riglib.tmux import assess_autosave_freshness

    tmux_actions = [a for a in plan.actions if a.kind == "provision_tmux"]
    if not tmux_actions:
        return
    tplan = tmux_plan_from_action(tmux_actions[0])
    if not tplan.autosave_enabled:
        return  # nothing to report; the disabled-plist leftover (if any) is already drift
    # Only assess freshness where the agent is actually INSTALLED (its launchd plist is on disk).
    # On a machine that never applied tmux — or off darwin, where the plist is never written —
    # there is nothing to be fresh, so stay silent instead of nagging "no health record" on every
    # `rig status`. Once installed, a MISSING health record is a real signal (installed but never ran).
    if not tplan.autosave_plist_path.is_file():
        return
    health_mtime, health_result = _read_autosave_health(tplan.autosave_health_path)
    verdict = assess_autosave_freshness(
        tplan, now_epoch=time.time(), health_mtime=health_mtime, health_result=health_result
    )
    state = _autosave_state_text(verdict)
    print(f"\n  tmux autosave agent: {state}  " + _dim(f"('{tplan.autosave_label}')"))


def _live_pty_count() -> int | None:
    """Count currently allocated pty slave devices (`/dev/ttys*`) — each is one live zpty/forkpty
    consumer against the machine-wide `kern.tty.ptmx_max` ceiling (NOT per-process). None on any
    read failure — pressure reporting stays silent rather than risk a false reading.
    """
    try:
        return sum(1 for _ in Path("/dev").glob("ttys*"))
    except OSError:
        return None


def _ptmx_max() -> int | None:
    """Read macOS's system-wide pty ceiling via `sysctl kern.tty.ptmx_max`. None off darwin (the
    sysctl doesn't exist there) or on any failure — pressure reporting stays silent, never a false
    positive/negative from a misread.
    """
    try:
        proc = subprocess.run(
            ["sysctl", "-n", "kern.tty.ptmx_max"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return None


def _print_tmux_pty_pressure_status(plan) -> None:
    """Report pressure against macOS's system-wide pty ceiling (`kern.tty.ptmx_max`) — an
    OUTSIDE-of-rig KERNEL limit, not a launchd resource limit rig's plists control, but one rig's
    own anti-sprawl + continuum-restore + boot design (panes that never die across reboots) makes
    a long-lived tmux server more likely to climb toward than a bare Terminal.app tab.

    Advisory only — like the autosave freshness line, this does NOT affect the status exit code —
    and stays silent off darwin / when tmux isn't managed / when the read fails, so it never nags
    a repo or OS where it doesn't apply.
    """
    from riglib.drift import _on_darwin
    from riglib.tmux import assess_pty_pressure

    if not _on_darwin():
        return
    tmux_actions = [a for a in plan.actions if a.kind == "provision_tmux"]
    if not tmux_actions:
        return
    verdict = assess_pty_pressure(_live_pty_count(), _ptmx_max())
    if verdict.state == "unavailable":
        return  # nothing actionable to report
    elif verdict.state == "high":
        state = _warn(f"HIGH ({verdict.detail})")
    elif verdict.state == "ok":
        state = _ok(verdict.detail)
    else:
        raise AssertionError(f"unhandled pty pressure state: {verdict.state!r}")
    print(f"\n  tmux pty pressure: {state}")


def _print_dep_statuses(report) -> None:
    """Print the per-dependency present/absent lines + the install hint for each absent one."""
    for st in report.statuses:
        tag = "required" if st.dep.required else "optional"
        if st.present:
            print(f"  {_ok('✔')} {st.dep.name:<12} {_dim('(' + tag + ')')}  {st.dep.why}")
            continue
        mark = _err("✗") if st.dep.required else _warn("○")
        print(f"  {mark} {st.dep.name:<12} {_dim('(' + tag + ')')}  {st.dep.why}")
        if st.install_cmd:
            print(f"      install: {_dim_cmd(st.install_cmd)}")
        else:
            print(f"      {_dim('install: (no package mapping for this OS — install manually)')}")
        if st.pep668_fallback:
            # the plain install_cmd above WILL fail here: this interpreter's stdlib carries
            # PEP 668's EXTERNALLY-MANAGED marker (common on modern Homebrew/distro Python), so
            # a bare uv/pip refuses. Offer the explicit bypass as a human-chosen fallback for
            # someone intentionally installing into THIS interpreter (e.g. rig's own dev
            # checkout) rather than switching to a pipx/uv-tool managed install.
            print(f"      {_warn('this interpreter is PEP-668 externally-managed — the command above will refuse.')}")
            print(f"      {_dim('either: pipx install hyper-rig   (recommended — isolates rig cleanly)')}")
            print(f"          or: {_dim_cmd(st.pep668_fallback)}")
