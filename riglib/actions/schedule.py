"""``provision_schedule`` handler: launchd, systemd user timers and crontab cleanup."""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import fsutil
from ..plan import Action
from . import live_env
from . import service_ctl
from .result import ActionResult


# ── model-freshness schedule provisioning (launchd / systemd --user) ──────────────────────
def schedule_plan_from_action(action: Action):
    """Rebuild the pure :class:`~riglib.schedule.SchedulePlan` an action describes.

    Shared by the install handler and the drift check so both agree on the exact desired
    artifact (plist XML / systemd unit files) from the action's options. Lazy import keeps the
    actions package import-light.
    """
    from ..schedule import build_schedule

    opts = action.options
    return build_schedule(
        checker_path=Path(opts["checker_path"]),
        hour=int(opts.get("hour", 12)),
        minute=int(opts.get("minute", 0)),
        label=str(opts.get("label", "")),
        platform=str(opts.get("platform", "")) or None,
    )


def _read_crontab() -> tuple[bool, str]:
    """Return (readable, contents) of the user's crontab — only to remove the LEGACY managed pair
    earlier rig versions installed. A missing crontab, or one an agent shell cannot read
    (``NoNewPrivs=1`` defeats the setgid ``crontab`` binary, rig-cli#721), is (False, "")."""
    try:
        res = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False, ""
    if res.returncode != 0:
        # `crontab -l` exits non-zero when no crontab exists for the user — treat as empty.
        return False, ""
    return True, res.stdout


def _write_crontab(contents: str) -> int:
    try:
        res = subprocess.run(
            ["crontab", "-"], input=contents, capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return 1
    return res.returncode


def _crontab_without_managed(contents: str, label: str) -> list[str]:
    """Strip rig's legacy managed sentinel pair (comment + following cron line) for ``label``.

    Removes the ``# rig-managed: <label>`` comment AND the cron line immediately after it,
    leaving every other (user) line untouched.
    """
    from ..schedule import CRON_SENTINEL_PREFIX

    sentinel = f"{CRON_SENTINEL_PREFIX} {label}"
    lines = contents.splitlines()
    out: list[str] = []
    skip_next = False
    for line in lines:
        if skip_next:
            skip_next = False
            continue
        if line.strip() == sentinel:
            skip_next = True  # drop the cron line that follows the sentinel
            continue
        out.append(line)
    return out


def _do_provision_schedule(action: Action, on_conflict: str) -> ActionResult:
    """Install the daily model-freshness schedule IF MISSING (idempotent).

    The "check whether the cron exists and install it if missing" rule: a re-apply that finds the schedule
    already present and current is a no-op (``skipped``); a missing/drifted schedule is
    (re)installed. Cross-platform: launchd plist + ``launchctl load`` on macOS, a systemd --user
    service + timer on Linux (rig-cli#721). ``on_conflict`` is honored for the plist / unit files
    (a user-modified file at the path is backed up before rewrite under ``backup``).
    ``RIG_SCHEDULE_DRY_RUN`` writes the artifact files but skips the live daemon mutation.
    """
    sched = schedule_plan_from_action(action)
    if sched.platform == "launchd":
        return _provision_launchd(action, sched, on_conflict)
    return _provision_systemd(action, sched, on_conflict)


def _launchd_already_installed(action: Action, sched, plist_path: Path) -> ActionResult | None:
    """No-op result when a CURRENT plist is present (and, outside dry-run, loaded)."""
    # Under dry-run / a sandboxed HOME the loaded-probe would query the REAL launchd domain
    # (rig-cli#116) — a current plist is the whole no-op then. Otherwise: present AND current
    # AND loaded → nothing to do (the install-if-missing no-op).
    if live_env._schedule_dry_run():
        return ActionResult(
            action, "skipped",
            f"models/{action.item}: launchd plist {plist_path} already current "
            f"({live_env._live_skip_reason('RIG_SCHEDULE_DRY_RUN', 'launchctl loaded-probe')})",
        )
    if service_ctl._launchctl_loaded(sched.label):
        return ActionResult(action, "skipped", f"models/{action.item}: launchd job '{sched.label}' already installed at {sched.human_time}")
    return None


def _launchd_reload(action: Action, sched, plist_path: Path, out, already: bool) -> ActionResult:
    """(Re)load the agent so launchd picks up the (possibly new) calendar interval."""
    # unload first is safe — it is a no-op when the label isn't loaded.
    service_ctl._launchctl("unload", str(plist_path))
    rc = service_ctl._launchctl("load", str(plist_path))
    if rc != 0:
        return ActionResult(
            action, "error",
            f"models/{action.item}: wrote {plist_path} but `launchctl load` failed (rc={rc})",
            out.backup,
        )
    # We reach here only when the early-return no-op did NOT fire — i.e. the plist was
    # written/changed OR the job was not loaded. So the run made a real change: report the
    # file-write status, but never a misleading 'skipped' (the file may be byte-identical yet
    # we still (re)loaded the job into launchd, which IS the change).
    status = out.status if out.status in ("created", "backed_up") else "updated"
    verb = "installed" if not already else "updated"
    return ActionResult(
        action, status,
        f"models/{action.item}: launchd job '{sched.label}' {verb} → daily {sched.human_time} ({plist_path})",
        out.backup,
    )


def _provision_launchd(action: Action, sched, on_conflict: str) -> ActionResult:
    plist_path = sched.plist_path
    if plist_path is None:
        return ActionResult(action, "error", "launchd: no plist path resolved")
    desired = sched.plist_xml()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    log_dir = (sched.log_path or Path.home() / "Library" / "Logs").parent
    log_dir.mkdir(parents=True, exist_ok=True)

    already = plist_path.is_file()
    current = plist_path.read_text(encoding="utf-8") if already else ""
    if already and current == desired:
        noop = _launchd_already_installed(action, sched, plist_path)
        if noop is not None:
            return noop

    out = fsutil.write_file(plist_path, desired, on_conflict)
    if out.status == "error":
        return ActionResult(action, "error", f"models/{action.item}: {out.detail}", out.backup)
    # Conflict-skip: the existing plist DIFFERS from desired but on_conflict=skip told us not to
    # write it. The desired schedule never hit disk, so we must NOT unload/load the stale plist
    # (that would mutate launchd with the wrong schedule) and must NOT report 'updated' (that
    # would mask the unresolved drift). Surface 'skipped' so the drift is visible. We reach this
    # only when `desired != current` (the byte-identical case took the early-return no-op above
    # or write_file's identical-bytes path, which still proceeds to a needed (re)load below).
    if out.status == "skipped" and already and current != desired:
        return ActionResult(
            action, "skipped",
            f"models/{action.item}: launchd plist {plist_path} differs but on_conflict=skip — "
            f"left unchanged (drift NOT reconciled; re-run with on_conflict=backup/overwrite)",
        )
    if live_env._schedule_dry_run():
        return ActionResult(
            action, out.status if out.status != "skipped" else "created",
            f"models/{action.item}: wrote plist {plist_path} "
            f"({live_env._live_skip_reason('RIG_SCHEDULE_DRY_RUN', 'launchctl load')})",
            out.backup,
        )
    return _launchd_reload(action, sched, plist_path, out, already)


def schedule_timer_active(sched) -> bool:
    """True when the schedule's systemd --user timer is enabled (survives re-login), active
    (armed now) and loaded from the CURRENT unit files. Files written by a dry-run apply or
    behind a failed daemon-reload leave the manager on the old schedule (NeedDaemonReload)."""
    return (
        service_ctl._systemctl_user("is-enabled", "--quiet", sched.timer_name) == 0
        and service_ctl._systemctl_user("is-active", "--quiet", sched.timer_name) == 0
        and all(
            service_ctl._systemctl_user_output("show", "--property=NeedDaemonReload", "--value", unit) == "no"
            for unit in (sched.timer_name, sched.service_name)
        )
    )


def _remove_legacy_crontab(label: str) -> str:
    """Drop the crontab pair earlier rig versions installed for ``label``; return a detail
    suffix (empty when there was nothing to do). Best-effort: an unreadable crontab (the
    NoNewPrivs agent shell) is skipped, never an error — the timer is the schedule now."""
    from ..schedule import CRON_SENTINEL_PREFIX

    readable, current = _read_crontab()
    if not readable:
        return ""
    if f"{CRON_SENTINEL_PREFIX} {label}" not in (ln.strip() for ln in current.splitlines()):
        return ""
    kept = _crontab_without_managed(current, label)
    if _write_crontab("\n".join(kept).rstrip("\n") + "\n" if kept else "") != 0:
        return "; legacy crontab line NOT removed (`crontab -` failed) — remove it by hand"
    return "; removed legacy crontab line"


def _systemd_already_installed(action: Action, sched) -> ActionResult | None:
    """No-op result when every unit file is current (and, outside dry-run, the timer is live)."""
    if live_env._schedule_dry_run():
        return ActionResult(
            action, "skipped",
            f"models/{action.item}: systemd units for '{sched.label}' already current "
            f"({live_env._live_skip_reason('RIG_SCHEDULE_DRY_RUN', 'systemctl --user probe')})",
        )
    if schedule_timer_active(sched):
        # retried here too: a first apply from a NoNewPrivs shell could not read the crontab,
        # so a later apply from a normal shell must still drop the legacy line
        legacy = _remove_legacy_crontab(sched.label)
        return ActionResult(
            action, "updated" if legacy else "skipped",
            f"models/{action.item}: systemd timer '{sched.timer_name}' already installed at {sched.human_time}"
            + legacy,
        )
    return None


def _write_unit_files(action: Action, files, current, on_conflict: str):
    """Write every unit file; return ``(backup, early_result)`` — early_result is set on a
    write error or an on_conflict=skip drift that must not be reconciled."""
    backup = None
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        out = fsutil.write_file(path, text, on_conflict)
        if out.status == "error":
            return backup, ActionResult(action, "error", f"models/{action.item}: {out.detail}", out.backup)
        if out.status == "skipped" and current[path] is not None and current[path] != text:
            return backup, ActionResult(
                action, "skipped",
                f"models/{action.item}: {path} differs but on_conflict=skip — left unchanged "
                f"(drift NOT reconciled; re-run with on_conflict=backup/overwrite)",
            )
        backup = backup or out.backup
    return backup, None


def _systemd_enable_timer(action: Action, sched, backup) -> ActionResult | None:
    """daemon-reload, enable --now and restart the timer; the first failure's error result."""
    if service_ctl._systemctl_user("daemon-reload") != 0:
        return ActionResult(
            action, "error",
            f"models/{action.item}: wrote units but `systemctl --user daemon-reload` failed "
            "(no reachable user manager? see `rig doctor`)",
            backup,
        )
    if service_ctl._systemctl_user("enable", "--now", sched.timer_name) != 0:
        return ActionResult(
            action, "error",
            f"models/{action.item}: `systemctl --user enable --now {sched.timer_name}` failed",
            backup,
        )
    # a changed OnCalendar only takes effect once the running timer is restarted
    if service_ctl._systemctl_user("restart", sched.timer_name) != 0:
        return ActionResult(
            action, "error",
            f"models/{action.item}: `systemctl --user restart {sched.timer_name}` failed",
            backup,
        )
    return None


def _provision_systemd(action: Action, sched, on_conflict: str) -> ActionResult:
    files = sched.unit_files()
    current = {p: (p.read_text(encoding="utf-8") if p.is_file() else None) for p in files}
    if all(current[p] == text for p, text in files.items()):
        noop = _systemd_already_installed(action, sched)
        if noop is not None:
            return noop

    backup, early = _write_unit_files(action, files, current, on_conflict)
    if early is not None:
        return early
    if live_env._schedule_dry_run():
        return ActionResult(
            action, "created",
            f"models/{action.item}: wrote {sched.timer_name} + {sched.service_name} "
            f"({live_env._live_skip_reason('RIG_SCHEDULE_DRY_RUN', 'systemctl --user enable')})",
            backup,
        )
    failed = _systemd_enable_timer(action, sched, backup)
    if failed is not None:
        return failed
    verb = "installed" if all(c is None for c in current.values()) else "updated"
    return ActionResult(
        action, "backed_up" if backup else "created",
        f"models/{action.item}: systemd timer '{sched.timer_name}' {verb} → daily {sched.human_time}"
        + _remove_legacy_crontab(sched.label),
        backup,
    )
