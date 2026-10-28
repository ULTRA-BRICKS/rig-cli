"""``provision_tg_ctl`` handler: the tg-ctl boot daemon."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import fsutil
from ..plan import Action
from . import live_env
from . import service_ctl
from .result import ActionResult
from .shared import _timestamped_backup_path


# ── tg-ctl inbound-daemon LaunchAgent provisioning (macOS) ──────────────────────────
def _tg_ctl_dry_run() -> bool:
    """Honor RIG_TG_CTL_DRY_RUN — write the managed plist file but make NO live/destructive change.

    For CI / containers / smoke where a real ``launchctl bootstrap`` (a per-user daemon mutation
    HOME can't redirect) is unwanted. Under dry-run: the managed plist still lands under the
    (HOME-isolated) path (so callers can assert the artifact), but the gui-domain (re)load AND the
    stale-predecessor teardown (its ``bootout`` AND the on-disk backup+remove of its plist) are
    BOTH skipped — dry-run never mutates the live launchd domain nor deletes the predecessor file,
    it only reports what it would do. Mirrors ``RIG_SCHEDULE_DRY_RUN`` (the schedule's seam).
    ALSO true automatically under a sandboxed HOME (:func:`_home_is_sandboxed`, rig-cli#116) — the
    incident this guards against was exactly this action.
    """
    return live_env._env_flag("RIG_TG_CTL_DRY_RUN") or live_env._home_is_sandboxed()


def _tg_ctl_config_dir_default() -> str:
    """The tg-cli config dir, honoring ``$TG_CTL_CONFIG_DIR`` (the daemon's own env), else the
    documented default ``~/.config/tg-cli``. The launchd logs land here, next to the daemon's
    .env/config.yaml, so everything tg-ctl is in one place."""
    return os.environ.get("TG_CTL_CONFIG_DIR", "").strip() or "~/.config/tg-cli"


def tg_ctl_plan_from_action(action: Action):
    """Rebuild the pure :class:`~riglib.tg_ctl.TgCtlPlan` an action describes.

    Shared by the install handler and the drift check so both agree on the exact desired plist
    from the action's options. ``Path.home()`` is the resolved HOME at apply time (a test
    monkeypatches it to a tmp HOME). The bun path is discovered at apply time unless pinned.
    Lazy import keeps the actions package import-light.
    """
    from ..tg_ctl import DEFAULT_BOOT_LABEL, DEFAULT_TG_CTL_PATH, build_tg_ctl

    opts = action.options
    config_dir = opts.get("config_dir") or _tg_ctl_config_dir_default()
    # `boot` is default-ON: a YAML `boot:` with no value (None) means "use the default" (True),
    # NOT False — `bool(None)` would silently disable it (codex P1). Only an explicit `False` is off.
    boot = opts.get("boot", True)
    return build_tg_ctl(
        home=Path.home(),
        boot=True if boot is None else bool(boot),
        boot_label=str(opts.get("label") or DEFAULT_BOOT_LABEL),
        bun_path=opts.get("bun_path") or None,
        tg_ctl_path=str(opts.get("tg_ctl_path") or DEFAULT_TG_CTL_PATH),
        config_dir=str(config_dir),
    )


def _tg_ctl_teardown_stale(plan, dry: bool) -> tuple[Path | None, str | None]:
    """Tear down the dead predecessor service (``com.ultra.codex-tg-bot``) if its plist exists:
    bootout + timestamped backup + remove. Returns (backup_path, detail) or (None, None) when
    there is nothing to tear down. Under ``dry`` it touches NOTHING — neither launchd nor disk —
    and returns a "would remove" detail (the dry-run contract: no live mutation, no disk write).
    """
    stale = plan.stale_plist_path
    if not stale.is_file():
        return None, None
    if dry:
        return None, (
            f"{live_env._live_skip_reason('RIG_TG_CTL_DRY_RUN', 'launchctl bootout')} — would boot out "
            f"+ remove stale predecessor {stale.name}"
        )
    service_ctl._launchctl_bootout(str(stale))  # harmless rc!=0 if it wasn't loaded
    bak = _timestamped_backup_path(stale.with_name(stale.name + ".rig-bak"))
    bak.write_text(stale.read_text(encoding="utf-8"), encoding="utf-8")
    stale.unlink()
    return bak, f"removed stale predecessor {stale.name} (booted out, backed up → {bak.name})"


def _do_provision_tg_ctl(action: Action, on_conflict: str) -> ActionResult:
    """Provision the tg-ctl inbound daemon as a macOS LaunchAgent (idempotent).

    Mirrors the tmux-boot provisioning shape (render -> backup-differing -> write), but unlike
    tmux this agent IS (re)loaded into launchd so a clean ``rig init`` starts it without a reboot:

      - render ``~/Library/LaunchAgents/ai.hyperide.tg-ctl.plist`` (byte-exact to the working
        hand-created file — see riglib.tg_ctl.render_plist's sort_keys note);
      - back up any existing DIFFERING plist (on_conflict=backup) before rewrite;
      - ensure the log dir (the tg-cli config dir) exists so launchd can open the logs;
      - tear down the stale predecessor (``com.ultra.codex-tg-bot``): ``bootout`` + timestamped
        backup + remove its plist, if present;
      - (re)load via ``launchctl bootout``/``bootstrap`` in the per-user gui domain.

    macOS-only (launchd). Off darwin it is a ``skipped`` no-op (no Linux equivalent yet).
    A re-apply that finds the plist byte-identical AND the agent loaded is a ``skipped`` no-op —
    the idempotency contract that keeps a re-apply against the live plist from rewriting it.
    ``RIG_TG_CTL_DRY_RUN`` writes the plist but skips every live launchd mutation.
    """
    plan = tg_ctl_plan_from_action(action)

    if sys.platform != "darwin":
        return ActionResult(
            action, "skipped",
            "tg_ctl/boot: skipped (launchd is macOS-only; no Linux equivalent yet)",
        )

    run = _TgCtlRun()
    dry = _tg_ctl_dry_run()

    # 0) tear down the dead predecessor service first (bootout + backup + remove its plist).
    # Under dry-run this is a pure no-op that only REPORTS what it would do (no disk mutation).
    run.absorb_teardown(*_tg_ctl_teardown_stale(plan, dry))

    if not plan.boot_enabled:
        # boot disabled: don't write/load the agent. A leftover plist is surfaced by drift as an
        # EXTRA (it still auto-starts the daemon), but apply never deletes the user's own file.
        if not run.changed:
            return ActionResult(action, "skipped", "tg_ctl/boot: disabled (tg_ctl.boot=false) — nothing to provision")
        return run.result(action)

    # 1) ensure the log dir (tg-cli config dir) exists so launchd can open StandardOut/ErrorPath.
    plan.config_dir.mkdir(parents=True, exist_ok=True)

    # 2) write the plist (idempotent on identical bytes; honors on_conflict for a differing one).
    plan.plist_path.parent.mkdir(parents=True, exist_ok=True)
    desired = plan.render_plist()
    already = plan.plist_path.is_file()
    current = plan.plist_path.read_text(encoding="utf-8") if already else ""

    noop = _already_current_result(action, plan, dry, run, (already, current == desired))
    if noop is not None:
        return noop

    stop = _write_plist(action, plan, desired, on_conflict, (already, current), run)
    if stop is not None:
        return stop

    if dry:
        return run.dry_result(action)

    # 3) (re)load via the per-user gui domain so a changed plist is picked up without a reboot.
    # We (re)load whenever the plist was (re)written OR the agent isn't currently loaded.
    if run.changed or not service_ctl._launchctl_gui_loaded(plan.boot_label):
        failure = _load_agent(action, plan, run)
        if failure is not None:
            return failure

    if not run.changed:
        return ActionResult(action, "skipped",
                            f"tg_ctl/boot: launchd agent '{plan.boot_label}' already installed and loaded")
    return run.result(action)


@dataclass
class _TgCtlRun:
    """What one provision run has done so far (feeds the final ActionResult)."""

    details: list[str] = field(default_factory=list)
    changed: bool = False
    backup: Path | None = None

    def absorb_teardown(self, stale_backup: Path | None, stale_detail: str | None) -> None:
        """Fold the stale-predecessor teardown outcome into the run."""
        if stale_detail:
            self.details.append(stale_detail)
        if stale_backup is not None:  # a real removal happened (never under dry-run)
            self.backup = stale_backup
            self.changed = True

    def dry_result(self, action: Action) -> ActionResult:
        """Result of a dry run: plist written, live launchd mutation skipped."""
        return ActionResult(
            action,
            "backed_up" if self.backup else ("created" if self.changed else "skipped"),
            f"tg_ctl/boot: {'; '.join(self.details) or 'plist already current'} "
            f"({live_env._live_skip_reason('RIG_TG_CTL_DRY_RUN', 'launchctl bootstrap')})",
            self.backup,
        )

    def result(self, action: Action) -> ActionResult:
        """The terminal "something changed" result."""
        return ActionResult(action, "backed_up" if self.backup else "created",
                            f"tg_ctl/boot: {'; '.join(self.details)}", self.backup)


def _already_current_result(
    action: Action, plan, dry: bool, run: _TgCtlRun, plist_state: tuple[bool, bool]
) -> ActionResult | None:
    """The install-if-missing no-op: present AND byte-identical → nothing left to do.

    ``plist_state`` is ``(already_on_disk, identical_to_desired)``. Under dry-run / a sandboxed
    HOME (rig-cli#116) this is the WHOLE no-op — never probe the real launchd domain, and say
    so (mirrors the schedule/spotlight no-op paths). None when the plist is current but the
    agent still has to be loaded, or when anything else changed this run.
    """
    already, identical = plist_state
    if not (already and identical) or run.changed:
        return None
    if dry:
        return ActionResult(
            action, "skipped",
            f"tg_ctl/boot: plist {plan.plist_path} already current "
            f"({live_env._live_skip_reason('RIG_TG_CTL_DRY_RUN', 'launchctl loaded-probe')})",
        )
    if service_ctl._launchctl_gui_loaded(plan.boot_label):
        return ActionResult(
            action, "skipped",
            f"tg_ctl/boot: launchd agent '{plan.boot_label}' already installed and loaded",
        )
    return None


def _write_plist(
    action: Action, plan, desired: str, on_conflict: str, existing: tuple[bool, str], run: _TgCtlRun
) -> ActionResult | None:
    """Write the plist honoring ``on_conflict``; a final result when the run must stop, else None.

    ``existing`` is ``(already_on_disk, current_text)``.
    """
    already, current = existing
    out = fsutil.write_file(plan.plist_path, desired, on_conflict)
    if out.status == "error":
        return ActionResult(action, "error", f"tg_ctl/boot: {out.detail}", out.backup)
    if out.backup:
        run.backup = run.backup or out.backup
        run.details.append(f"backed up prior plist → {out.backup.name}")
    # Conflict-skip: the existing plist DIFFERS but on_conflict=skip said don't write it. The
    # desired agent never hit disk, so we must NOT (re)load the stale plist and must NOT report a
    # change — surface the unresolved drift instead (mirrors the schedule conflict-skip path).
    if out.status == "skipped" and already and current != desired:
        if run.changed:  # the stale-predecessor removal still happened — report it, plus the skip.
            run.details.append(
                f"{plan.plist_path.name} differs but on_conflict=skip — left unchanged "
                f"(drift NOT reconciled; re-run with on_conflict=backup/overwrite)"
            )
            return run.result(action)
        return ActionResult(
            action, "skipped",
            f"tg_ctl/boot: launchd plist {plan.plist_path} differs but on_conflict=skip — "
            f"left unchanged (drift NOT reconciled; re-run with on_conflict=backup/overwrite)",
        )
    if out.status != "skipped":
        run.changed = True
        run.details.append(f"wrote {plan.plist_path.name}")
    return None


def _load_agent(action: Action, plan, run: _TgCtlRun) -> ActionResult | None:
    """bootout (a harmless no-op when not loaded) then bootstrap; an error result, or None."""
    service_ctl._launchctl_bootout(str(plan.plist_path))
    rc = service_ctl._launchctl_bootstrap(str(plan.plist_path))
    if rc != 0:
        return ActionResult(
            action, "error",
            f"tg_ctl/boot: wrote {plan.plist_path} but `launchctl bootstrap` failed (rc={rc})",
            run.backup,
        )
    if not run.changed:  # plist byte-identical but the agent wasn't loaded → loading IS the change.
        run.changed = True
        run.details.append(f"loaded launchd agent '{plan.boot_label}'")
    else:
        run.details.append(f"(re)loaded launchd agent '{plan.boot_label}'")
    return None
