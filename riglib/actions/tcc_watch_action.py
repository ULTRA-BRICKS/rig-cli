"""``provision_tcc_watch`` handler: the macOS TCC watcher agent."""

from __future__ import annotations

import sys
from pathlib import Path

from .daemon_unit import LaunchAgentRun
from ..plan import Action
from . import live_env
from .result import ActionResult


# ── tcc-watch dialog-watcher LaunchAgent provisioning (macOS) ───────────────────────
def _tcc_watch_dry_run() -> bool:
    """Honor RIG_TCC_WATCH_DRY_RUN — write the managed plist + watcher script but make NO
    live/destructive change. Mirrors :func:`_tg_ctl_dry_run`: under dry-run the managed
    artifacts still land under the (HOME-isolated) path, but the gui-domain (re)load is skipped.
    ALSO true automatically under a sandboxed HOME (:func:`_home_is_sandboxed`, rig-cli#116).
    """
    return live_env._env_flag("RIG_TCC_WATCH_DRY_RUN") or live_env._home_is_sandboxed()


def tcc_watch_plan_from_action(action: Action):
    """Rebuild the pure :class:`~riglib.tcc_watch.TccWatchPlan` an action describes.

    Shared by the install handler (and, in a follow-up, drift detection) so both agree on the
    exact desired plist/script from the action's options. ``Path.home()`` is the resolved HOME
    at apply time (a test monkeypatches it to a tmp HOME). Lazy import keeps the actions package
    import-light.
    """
    from ..tcc_watch import DEFAULT_LABEL, build_tcc_watch

    opts = action.options
    return build_tcc_watch(
        home=Path.home(),
        label=str(opts.get("label") or DEFAULT_LABEL),
        config_dir=opts.get("config_dir") or "~/.config/rig/tcc-watch",
        python_bin=opts.get("python_bin") or None,
    )


def _do_provision_tcc_watch(action: Action, on_conflict: str) -> ActionResult:
    """Provision the tcc-watch dialog-watcher daemon as a macOS LaunchAgent (idempotent).

    Phase 1 (detection-only) of the defense-in-depth design behind the eval-tool TCC deny-list
    (PR #488) — see :mod:`riglib.tcc_watch`'s module docstring. Mirrors
    :func:`_do_provision_tg_ctl`'s shape (render -> backup-differing -> write -> (re)load), minus
    the stale-predecessor teardown (tcc-watch has no predecessor to replace) and minus any
    Codex-hook fold-in (tcc-watch is not a Codex Stop-hook source):

      - render ``~/Library/LaunchAgents/ai.hyperide.tcc-watch.plist``;
      - copy the watcher script (``riglib/tcc_watch_daemon.py``, byte-for-byte) to
        ``<config_dir>/watch.py`` so the installed daemon runs the SAME code the unit suite
        exercises directly;
      - back up any existing DIFFERING plist/script (on_conflict=backup) before rewrite;
      - ensure the config dir exists so launchd can open the logs and the daemon can write its store;
      - (re)load via ``launchctl bootout``/``bootstrap`` in the per-user gui domain.

    macOS-only (launchd). Off darwin it is a ``skipped`` no-op (no Linux equivalent yet).
    A re-apply that finds both artifacts byte-identical AND the agent loaded is a ``skipped``
    no-op. ``RIG_TCC_WATCH_DRY_RUN`` writes the artifacts but skips every live launchd mutation.
    """
    plan = tcc_watch_plan_from_action(action)

    if sys.platform != "darwin":
        return ActionResult(
            action, "skipped",
            "tcc_watch/watch: skipped (launchd is macOS-only; no Linux equivalent yet)",
        )

    run = LaunchAgentRun("tcc_watch/watch", "RIG_TCC_WATCH_DRY_RUN", "watcher script")
    return run.provision(
        action, plan,
        (plan.watcher_script_path, plan.render_watcher_script()),
        on_conflict, _tcc_watch_dry_run(),
    )
