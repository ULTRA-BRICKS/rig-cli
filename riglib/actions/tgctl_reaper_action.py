"""``provision_tgctl_reaper`` handler: the tg-ctl omp-acp reaper sweep agent."""

from __future__ import annotations

import sys
from pathlib import Path

from .daemon_unit import LaunchAgentRun
from ..plan import Action
from . import live_env
from .result import ActionResult


# ── tgctl-reaper periodic orphan-sweep LaunchAgent provisioning (macOS) ────────────
def _tgctl_reaper_dry_run() -> bool:
    """Honor RIG_TGCTL_REAPER_DRY_RUN — write the managed plist + sweeper script but make NO
    live/destructive change. Mirrors :func:`_tcc_watch_dry_run`: under dry-run the managed
    artifacts still land under the (HOME-isolated) path, but the gui-domain (re)load is skipped.
    ALSO true automatically under a sandboxed HOME (:func:`_home_is_sandboxed`, rig-cli#116).
    """
    return live_env._env_flag("RIG_TGCTL_REAPER_DRY_RUN") or live_env._home_is_sandboxed()


def tgctl_reaper_plan_from_action(action: Action):
    """Rebuild the pure :class:`~riglib.tgctl_reaper.TgctlReaperPlan` an action describes.

    Shared by the install handler, drift detection, and status so all three agree on the exact
    desired plist/script from the action's options. ``Path.home()`` is the resolved HOME at
    apply time (a test monkeypatches it to a tmp HOME). Lazy import keeps the actions package
    import-light.
    """
    from ..tgctl_reaper import DEFAULT_INTERVAL_SECONDS, DEFAULT_LABEL, build_tgctl_reaper

    opts = action.options
    return build_tgctl_reaper(
        home=Path.home(),
        label=str(opts.get("label") or DEFAULT_LABEL),
        interval_seconds=int(opts.get("interval_seconds") or DEFAULT_INTERVAL_SECONDS),
        config_dir=opts.get("config_dir") or "~/.config/rig/tgctl-reaper",
        python_bin=opts.get("python_bin") or None,
    )


def _do_provision_tgctl_reaper(action: Action, on_conflict: str) -> ActionResult:
    """Provision the tgctl-reaper periodic orphan-sweep as a macOS LaunchAgent (idempotent).

    The durable fix for the 2026-09-09 OOM incident (agent-tools#697) — see
    :mod:`riglib.tgctl_reaper`'s module docstring. Mirrors :func:`_do_provision_tcc_watch`'s
    shape (render -> backup-differing -> write -> (re)load) exactly, except the plist encodes a
    ``StartInterval`` periodic sweep instead of a ``RunAtLoad``/``KeepAlive`` resident daemon —
    launchd still needs the agent LOADED (bootstrapped) for the interval timer to run at all, it
    just doesn't keep a process alive between runs:

      - render ``~/Library/LaunchAgents/com.hyperide.tgctl-omp-acp-reaper.plist``;
      - copy the sweeper script (``riglib/tgctl_reaper_daemon.py``, byte-for-byte) to
        ``<config_dir>/reap.py`` so the installed sweep runs the SAME code the unit suite
        exercises directly;
      - back up any existing DIFFERING plist/script (on_conflict=backup) before rewrite — this
        is also how a machine that ran the OLD manually-installed
        ``agent-tools/ops/tgctl-omp-acp-reaper/install.sh`` (same launchd Label, different
        script path/shape) converges onto the rig-managed artifact on first apply;
      - ensure the config dir exists so launchd can open the logs;
      - (re)load via ``launchctl bootout``/``bootstrap`` in the per-user gui domain.

    macOS-only (launchd). Off darwin it is a ``skipped`` no-op (no Linux equivalent yet).
    A re-apply that finds both artifacts byte-identical AND the agent loaded is a ``skipped``
    no-op. ``RIG_TGCTL_REAPER_DRY_RUN`` writes the artifacts but skips every live launchd mutation.
    """
    plan = tgctl_reaper_plan_from_action(action)

    if sys.platform != "darwin":
        return ActionResult(
            action, "skipped",
            "tgctl_reaper/sweep: skipped (launchd is macOS-only; no Linux equivalent yet)",
        )

    run = LaunchAgentRun("tgctl_reaper/sweep", "RIG_TGCTL_REAPER_DRY_RUN", "sweeper script")
    return run.provision(
        action, plan,
        (plan.sweeper_script_path, plan.render_sweeper_script()),
        on_conflict, _tgctl_reaper_dry_run(),
    )
