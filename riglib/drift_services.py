"""Drift checks for scheduled / background services: schedule, spotlight, tg-ctl, tgctl-reaper, sysmon, internal dev hook."""

from __future__ import annotations

import sys

from .actions.live_env import _schedule_dry_run
from .actions.schedule import schedule_plan_from_action, schedule_timer_active
from .actions.service_ctl import _launchctl_gui_loaded, _launchctl_loaded
from .actions.spotlight import _spotlight_dry_run
from .actions.sysmon_action import (
    _sysmon_dry_run,
    _sysmon_service_loaded,
    sysmon_desired_files,
    sysmon_plan_from_action,
)
from .actions.tg_ctl_action import _tg_ctl_dry_run, tg_ctl_plan_from_action
from .actions.tgctl_reaper_action import _tgctl_reaper_dry_run, tgctl_reaper_plan_from_action
from .plan import Action
from .tg_ctl import STALE_PREDECESSOR_LABEL
from .drift_model import DriftItem, DriftNote, DriftReport, _on_darwin


def _check_schedule(action: Action, report: DriftReport) -> None:
    """Flag drift between the configured daily model-freshness schedule and disk.

    missing  — the launchd plist / systemd unit files are absent (or the plist exists but the
               job is not loaded / the timer is not enabled+active).
    modified — the artifact on disk differs from the desired one (e.g. someone changed the
               run time, or the checker path). ``rig apply`` converges.
    Cross-platform: launchd plist content + loaded state on macOS; the systemd --user service +
    timer on Linux. Shares the desired-artifact computation with the install handler.
    """
    sched = schedule_plan_from_action(action)
    if sched.platform == "launchd":
        _check_schedule_launchd(action, report, sched)
        return
    _check_schedule_systemd(action, report, sched)


def _check_schedule_launchd(action: Action, report: DriftReport, sched) -> None:
    plist = sched.plist_path
    if plist is None or not plist.is_file():
        report.items.append(
            DriftItem("missing", "models", action.item, plist or action.target, "launchd plist not installed")
        )
        return
    if plist.read_text(encoding="utf-8") != sched.plist_xml():
        report.items.append(
            DriftItem("modified", "models", action.item, plist, "launchd plist differs from configured schedule")
        )
        return
    # Apply skips the live load under dry-run / a sandboxed HOME (rig-cli#116), so status
    # suppresses the matching live probe to stay consistent (as spotlight / tmux already do).
    if not _schedule_dry_run() and not _launchctl_loaded(sched.label):
        report.items.append(
            DriftItem("missing", "models", action.item, plist, f"launchd job '{sched.label}' not loaded")
        )


def _check_schedule_systemd(action: Action, report: DriftReport, sched) -> None:
    # systemd --user branch (rig-cli#721): both unit files current, then the timer enabled +
    # active. Apply skips the systemctl calls under dry-run / a sandboxed HOME (rig-cli#116), so
    # status skips the live probe there too — else a sandboxed run could never converge.
    for path, text in sched.unit_files().items():
        if not path.is_file():
            report.items.append(
                DriftItem("missing", "models", action.item, path, f"systemd unit {path.name} not installed")
            )
            return
        if path.read_text(encoding="utf-8") != text:
            report.items.append(
                DriftItem("modified", "models", action.item, path, f"{path.name} differs from configured schedule")
            )
            return
    if not _schedule_dry_run() and not schedule_timer_active(sched):
        report.items.append(
            DriftItem("missing", "models", action.item, action.target,
                      f"systemd timer '{sched.timer_name}' not enabled+active")
        )


def _check_spotlight(action: Action, report: DriftReport) -> None:
    """Flag drift between the configured Spotlight-exclude state and disk.

    All independent facts accumulate (a drifted sentinel, a drifted plist, AND an unloaded agent
    are each surfaced): an ABSENT plist is the only short-circuit — you can't probe the loaded
    state of a plist that isn't there. A content-drifted plist still reports its load state.

    missing  — dependency/build dirs the last sweep VISITED still lack the
               ``.metadata_never_index`` sentinel (the sweep failed them, or one was hand-deleted)
               → ``rig apply`` / ``rig spotlight-sweep`` re-covers them. On macOS the launchd
               re-sweep plist is absent, or (when live) its agent is not loaded.
    modified — the launchd re-sweep plist on disk differs from the configured schedule/argv.
    note     — uncovered dirs the last sweep NEVER SAW, i.e. they appeared since. Not drift: every
               test run mints ``__pycache__`` dirs and the next sweep covers them. Printed dim
               beside "in sync" so a stalled sweep is still visible. The split lives in
               :class:`riglib.spotlight.Coverage`.

    Cross-platform: the sentinel sweep is cross-platform, so its coverage is checked everywhere; the
    launchd plist only exists on macOS (the runner writes none elsewhere), so the plist surface is
    macOS-only — asserting a missing plist on Linux would be a false positive. The launchd
    ``loaded`` probe is skipped under ``RIG_SPOTLIGHT_DRY_RUN`` (mirrors verify), so the hermetic
    suite never shells out to the real ``launchctl``.
    """
    from . import spotlight

    opts = action.options
    roots, deny, max_depth = spotlight.sweep_args_from_options(opts)
    label = str(opts.get("label") or spotlight.DEFAULT_BOOT_LABEL)

    # 1) sentinel coverage — the SAME `scan_coverage` predicate the post-apply verify step uses, on
    # the FULL matched set. `iter_target_dirs` already walks the whole tree (the dominant cost,
    # unavoidable here), so classifying each dir is one extra stat — cheap. The stale/pending split
    # is what keeps this honest: only dirs the sweep should have covered count as drift.
    selector = spotlight.SweepSelector(roots, deny, max_depth)
    record = spotlight.last_sweep(selector)
    cov = spotlight.scan_coverage(selector, record)
    if cov.stale:
        report.items.append(
            DriftItem(
                "missing", "spotlight", action.item, cov.stale[0],
                f"{len(cov.stale)}/{cov.total} dependency/build dirs the last sweep visited still "
                f"lack {spotlight.SENTINEL_NAME} — run `rig apply commit` "
                f"(or `rig spotlight-sweep`) to re-cover",
            )
        )
    if cov.pending:
        # `pending` exists only relative to a consulted record (the Coverage invariant).
        assert record is not None
        # Off macOS rig installs no re-sweep agent, so promising "the next sweep" there would be a
        # promise nothing keeps — name the manual command instead. The age of the last sweep is
        # what exposes a loaded-but-failing agent (it happened: 9 runs, 9 import errors).
        next_sweep = (
            "the daily re-sweep agent covers them" if _on_darwin()
            else "run `rig spotlight-sweep` to cover them (no periodic sweep agent off macOS)"
        )
        report.notes.append(
            DriftNote(
                "spotlight", action.item,
                f"{len(cov.pending)} of {cov.total} dependency/build dirs appeared since the last "
                f"sweep ({spotlight.describe_age(record.at)}) — {next_sweep}",
            )
        )

    # 2) the launchd re-sweep agent (macOS only — no plist is written on other hosts).
    if not _on_darwin():
        return
    sweep_cmd = tuple(str(a) for a in opts.get("sweep_cmd", spotlight.default_sweep_cmd()))
    plan = spotlight.build_spotlight(
        roots=roots, deny=deny, sweep_cmd=sweep_cmd, label=label, max_depth=max_depth
    )
    plist = plan.plist_path
    if plist is None or not plist.is_file():
        report.items.append(
            DriftItem("missing", "spotlight", action.item, plist or action.target,
                      "launchd re-sweep plist not installed (new projects would stop being covered)")
        )
        return
    try:
        current = plist.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        report.items.append(
            DriftItem("modified", "spotlight", action.item, plist,
                      f"launchd re-sweep plist unreadable — apply rewrites it ({type(exc).__name__})")
        )
        return
    if current != plan.plist_xml():
        report.items.append(
            DriftItem("modified", "spotlight", action.item, plist,
                      "launchd re-sweep plist differs from configured schedule/argv")
        )
        # do NOT return — a content-drifted plist can ALSO be unloaded; both are true, surface both.
    if not _spotlight_dry_run() and not _launchctl_loaded(label):
        report.items.append(
            DriftItem("missing", "spotlight", action.item, plist,
                      f"launchd re-sweep agent '{label}' not loaded")
        )


def _check_tg_ctl(action: Action, report: DriftReport) -> None:
    """Flag drift on the rig-managed tg-ctl LaunchAgent (macOS).

    missing  — boot enabled but the plist is absent, OR present + byte-identical but the agent is
               NOT loaded in the gui domain (launchd never picked it up → daemon not running).
    modified — the plist on disk differs from the desired byte-exact render (a hand edit, or an
               upgrade that changed the args/PATH). ``rig apply`` reconciles + (re)loads.
    extra    — a leftover plist when boot is DISABLED (it still auto-starts the daemon), OR the
               stale predecessor ``com.ultra.codex-tg-bot`` plist (apply boots it out + removes it).
    Off darwin (no launchd) tg-ctl provisioning is a no-op, so there is nothing to check.
    Shares the desired-plist computation with the install handler, so apply and status can never
    disagree.
    """
    if not _on_darwin():
        return
    plan = tg_ctl_plan_from_action(action)

    # the stale predecessor service is always drift while it exists (apply removes it).
    if plan.stale_plist_path.is_file():
        report.items.append(
            DriftItem("extra", "tg_ctl", action.item, plan.stale_plist_path,
                      f"stale predecessor '{STALE_PREDECESSOR_LABEL}' LaunchAgent present — "
                      f"apply boots it out and removes it")
        )

    if not plan.boot_enabled:
        # boot disabled: a leftover managed plist still auto-starts the daemon → surface it as a
        # disk->config extra (apply never deletes the user's own file).
        if plan.plist_path.is_file():
            report.items.append(
                DriftItem("extra", "tg_ctl", action.item, plan.plist_path,
                          "tg-ctl boot plist present but tg_ctl.boot is disabled "
                          "(it still starts the daemon at login — remove it or re-enable boot)")
            )
        return

    desired = plan.render_plist()
    if not plan.plist_path.is_file():
        report.items.append(
            DriftItem("missing", "tg_ctl", action.item, plan.plist_path,
                      "tg-ctl boot LaunchAgent not installed")
        )
        return
    if plan.plist_path.read_text(encoding="utf-8") != desired:
        report.items.append(
            DriftItem("modified", "tg_ctl", action.item, plan.plist_path,
                      "tg-ctl boot LaunchAgent differs from the configured plist")
        )
        return
    # Apply skips the bootstrap under dry-run / a sandboxed HOME (rig-cli#116), so status
    # suppresses the matching live probe to stay consistent (as spotlight / tmux already do).
    if not _tg_ctl_dry_run() and not _launchctl_gui_loaded(plan.boot_label):
        report.items.append(
            DriftItem("missing", "tg_ctl", action.item, plan.plist_path,
                      f"tg-ctl LaunchAgent '{plan.boot_label}' installed but not loaded")
        )


def _check_tgctl_reaper(action: Action, report: DriftReport) -> None:
    """Flag drift on the rig-managed tgctl-reaper periodic orphan-sweep LaunchAgent (macOS).

    Full drift/status parity with ``tg_ctl`` (Alex explicitly wants durable "control" here, not
    the weaker ``tcc_watch`` shortcut — see :mod:`riglib.tgctl_reaper`'s module docstring):

    missing  — the plist is absent, OR present + byte-identical but the agent is NOT loaded in
               the gui domain (launchd never picked it up → the StartInterval timer never fires).
    modified — the plist OR the sweeper script on disk differs from the desired byte-exact
               render (a hand edit, an upgrade that changed args/interval, or a machine that ran
               the OLD manually-installed `agent-tools/ops/tgctl-omp-acp-reaper/install.sh` under
               the SAME label with a different script path/shape). `rig apply` reconciles + (re)loads.
    Off darwin (no launchd) tgctl-reaper provisioning is a no-op, so there is nothing to check.
    Shares the desired-plist/script computation with the install handler, so apply and status
    can never disagree.
    """
    if not _on_darwin():
        return
    plan = tgctl_reaper_plan_from_action(action)

    desired_script = plan.render_sweeper_script()
    if not plan.sweeper_script_path.is_file():
        report.items.append(
            DriftItem("missing", "tgctl_reaper", action.item, plan.sweeper_script_path,
                      "tgctl-reaper sweeper script not installed")
        )
        return
    if plan.sweeper_script_path.read_text(encoding="utf-8") != desired_script:
        report.items.append(
            DriftItem("modified", "tgctl_reaper", action.item, plan.sweeper_script_path,
                      "tgctl-reaper sweeper script differs from the configured source")
        )
        return

    desired_plist = plan.render_plist()
    if not plan.plist_path.is_file():
        report.items.append(
            DriftItem("missing", "tgctl_reaper", action.item, plan.plist_path,
                      "tgctl-reaper LaunchAgent not installed")
        )
        return
    if plan.plist_path.read_text(encoding="utf-8") != desired_plist:
        report.items.append(
            DriftItem("modified", "tgctl_reaper", action.item, plan.plist_path,
                      "tgctl-reaper LaunchAgent differs from the configured plist")
        )
        return
    # Apply skips the bootstrap under dry-run / a sandboxed HOME (rig-cli#116), so status
    # suppresses the matching live probe to stay consistent (as tg_ctl/spotlight/tmux already do).
    if not _tgctl_reaper_dry_run() and not _launchctl_gui_loaded(plan.label):
        report.items.append(
            DriftItem("missing", "tgctl_reaper", action.item, plan.plist_path,
                      f"tgctl-reaper LaunchAgent '{plan.label}' installed but not loaded")
        )


def _check_sysmon(action: Action, report: DriftReport) -> None:
    """Flag drift on the rig-managed sysmon resource monitor (rig-cli#622).

    missing  — a package module, ``sysmon.json`` or the service file is absent; the service is
               not loaded; or it is loaded but its heartbeat is stale (the daemon is crash-looping
               or wedged — a monitor that does not run must not read as healthy).
    modified — a package module / ``sysmon.json`` / the service file differs from the render
               (a hand edit, or a rig upgrade not yet applied). ``rig apply`` reconciles.
    Shares every desired byte with the install handler (``sysmon_plan_from_action``).
    """
    if sys.platform != "darwin" and not sys.platform.startswith("linux"):
        return
    plan = sysmon_plan_from_action(action)
    desired_files = sysmon_desired_files(plan)
    clean = _report_sysmon_files(action, report, desired_files)
    service_path = plan.plist_path if sys.platform == "darwin" else plan.unit_path
    desired = plan.render_plist() if sys.platform == "darwin" else plan.render_unit()
    if not service_path.is_file():
        report.items.append(DriftItem("missing", "sysmon", action.item, service_path, "sysmon service not installed"))
        return
    if service_path.read_text(encoding="utf-8") != desired:
        report.items.append(DriftItem("modified", "sysmon", action.item, service_path,
                                      "sysmon service differs from the configured render"))
        return
    if not clean or _sysmon_dry_run():
        return
    _check_sysmon_live(action, report, plan, service_path)


def _report_sysmon_files(action: Action, report: DriftReport, desired_files: dict) -> bool:
    """Report up to 3 missing and 3 differing package files; True when none were missing or differing."""
    missing = [p for p in desired_files if not p.is_file()]
    differs = [p for p, text in desired_files.items() if p.is_file() and p.read_text(encoding="utf-8") != text]
    for path in missing[:3]:
        report.items.append(DriftItem("missing", "sysmon", action.item, path, "sysmon file not installed"))
    for path in differs[:3]:
        report.items.append(DriftItem("modified", "sysmon", action.item, path,
                                      "sysmon file differs from the configured render"))
    return not (missing or differs)


def _check_sysmon_live(action: Action, report: DriftReport, plan, service_path) -> None:
    """Live probes: the service is loaded and its daemon heartbeat is fresh."""
    if not _sysmon_service_loaded(plan):
        report.items.append(DriftItem("missing", "sysmon", action.item, service_path,
                                      f"sysmon service '{plan.label}' installed but not loaded"))
        return
    from .sysmon_daemon.cli import health
    from .sysmon_daemon.config import load

    h = health(load(plan.config_path))
    if h["state"] != "running":
        age = f", heartbeat {h['age_s']:.0f}s old" if h["age_s"] is not None else ""
        report.items.append(DriftItem("missing", "sysmon", action.item, plan.state_dir,
                                      f"sysmon service loaded but the daemon is {h['state']}{age}"))


def _check_internal_dev(action: Action, report: DriftReport) -> None:
    """Flag drift on the per-repo daemon auto-reload post-commit hook.

    Compares the rendered hook against the file at the resolved (worktree-correct) git-hooks
    path — the SAME rendering the runner writes, so status and apply can never disagree. The
    machine-global composer trampoline is a live/global apply-time concern (mirrors tg_ctl's
    boot plist) and is not checked here; a missing repo-local hook already surfaces the drift
    that matters to the repo.
    """
    from . import dev_reload

    repo_root = action.source
    dplan = dev_reload.build_dev_reload(
        repo_root=repo_root,
        daemon_source_paths=action.options.get("daemon_source_paths", []),
        reload_command=action.options.get("reload_command", dev_reload.DEFAULT_RELOAD_COMMAND),
    )
    target = dev_reload.post_commit_hook_path(repo_root)
    if not target.is_file():
        report.items.append(
            DriftItem("missing", "internal_dev", action.item, target,
                      "dev-reload post-commit hook not installed")
        )
        return
    if target.read_text(encoding="utf-8") != dplan.render_hook():
        report.items.append(
            DriftItem("modified", "internal_dev", action.item, target,
                      "post-commit hook differs from the rig-generated dev-reload hook")
        )
