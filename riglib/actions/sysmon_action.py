"""``provision_sysmon`` handler: the system monitor service."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import fsutil
from ..plan import Action
from . import live_env
from . import service_ctl
from .result import ActionResult


# ── sysmon resource monitor (LaunchAgent on macOS, systemd --user unit on Linux, #622) ────
def _sysmon_dry_run() -> bool:
    """Honor RIG_SYSMON_DRY_RUN — write every artifact but skip the live service (re)load.
    ALSO true under a sandboxed HOME (:func:`_home_is_sandboxed`, rig-cli#116): launchctl and
    ``systemctl --user`` act on the REAL user session whatever ``$HOME`` says."""
    return live_env._env_flag("RIG_SYSMON_DRY_RUN") or live_env._home_is_sandboxed()


def sysmon_plan_from_action(action: Action):
    """Rebuild the pure :class:`~riglib.sysmon.SysmonPlan` an action describes — shared by the
    install handler, drift, verify and status so all four agree on every rendered byte."""
    from ..sysmon import build_sysmon

    return build_sysmon(
        home=Path.home(),
        block=dict(action.options.get("block") or {}),
        gpu_env=dict(action.options.get("gpu_env") or {}),
    )


def sysmon_desired_files(plan) -> dict[Path, str]:
    """Every rig-owned file of the monitor except the service definition: the package modules
    and ``sysmon.json``. These are GENERATED (config lives in rig.yaml), so a hand edit is
    drift that apply overwrites without a backup; the service file honors ``on_conflict``."""
    files = {plan.package_dir / name: text for name, text in plan.render_package().items()}
    files[plan.config_path] = plan.render_config()
    return files


def _sysmon_service_loaded(plan) -> bool:
    if sys.platform == "darwin":
        return service_ctl._launchctl_gui_loaded(plan.label)
    return service_ctl._systemctl_user("is-active", "--quiet", plan.unit_name) == 0


def _do_provision_sysmon(action: Action, on_conflict: str) -> ActionResult:
    """Provision the sysmon resource monitor (idempotent): package + config + service.

    - copy ``riglib/sysmon_daemon/*.py`` byte-for-byte to ``<config_dir>/sysmon_daemon/`` and
      render ``<config_dir>/sysmon.json`` from the ``sysmon:`` block (overwritten when they
      differ — generated artifacts, see :func:`sysmon_desired_files`);
    - render the service: the ``RunAtLoad``/``KeepAlive`` LaunchAgent on macOS, a systemd
      ``--user`` unit on Linux (backup-noted per ``on_conflict``);
    - (re)load it when anything changed or it is not loaded, so a new package or config takes
      effect on this apply: ``launchctl bootout``/``bootstrap`` in ``gui/<uid>``, or
      ``systemctl --user daemon-reload`` + ``enable`` + ``restart``. The daemon is stateless
      w.r.t. user sessions (its state is files), so a restart never disturbs a live session.

    ``RIG_SYSMON_DRY_RUN`` / a sandboxed HOME writes the artifacts and skips the live (re)load.
    Any other platform is a ``skipped`` no-op.
    """
    label = "sysmon/monitor"
    if sys.platform != "darwin" and not sys.platform.startswith("linux"):
        return ActionResult(action, "skipped", f"{label}: skipped (no launchd/systemd on {sys.platform})")
    plan = sysmon_plan_from_action(action)
    dry = _sysmon_dry_run()
    run = _SysmonRun()

    failure = _write_support_files(action, plan, label, run)
    if failure is not None:
        return failure

    service_path = plan.plist_path if sys.platform == "darwin" else plan.unit_path
    failure = _write_service_file(action, plan, service_path, on_conflict, label, run)
    if failure is not None:
        return failure

    return _load_and_report(action, plan, service_path, label, run, dry)


def _load_and_report(
    action: Action, plan, service_path: Path, label: str, run: _SysmonRun, dry: bool
) -> ActionResult:
    """(Re)load the service when due (never under dry-run) and build the final result."""
    if dry:
        return ActionResult(
            action,
            "backed_up" if run.backup else ("created" if run.changed else "skipped"),
            f"{label}: {'; '.join(run.details) or 'artifacts already current'} "
            f"({live_env._live_skip_reason('RIG_SYSMON_DRY_RUN', 'service (re)load')})",
            run.backup,
        )

    if run.changed or not _sysmon_service_loaded(plan):
        rc = _reload_service(plan, service_path)
        if rc != 0:
            return ActionResult(action, "error",
                                f"{label}: wrote artifacts but the service (re)load failed (rc={rc})",
                                run.backup)
        run.details.append(f"(re)loaded service '{plan.label}'")
        run.changed = True

    if not run.changed:
        return ActionResult(action, "skipped", f"{label}: service '{plan.label}' already installed and running")
    return ActionResult(action, "backed_up" if run.backup else "created",
                        f"{label}: {'; '.join(run.details)}", run.backup)


@dataclass
class _SysmonRun:
    """What one provision run has written so far (feeds the final ActionResult)."""

    details: list[str] = field(default_factory=list)
    changed: bool = False
    backup: Path | None = None


def _write_support_files(action: Action, plan, label: str, run: _SysmonRun) -> ActionResult | None:
    """Write the generated package + config (always overwrite); an error result, or None."""
    plan.config_dir.mkdir(parents=True, exist_ok=True)
    plan.state_dir.mkdir(parents=True, exist_ok=True)
    wrote: list[str] = []
    for path, text in sysmon_desired_files(plan).items():
        out = fsutil.write_file(path, text, "overwrite")
        if out.status == "error":
            return ActionResult(action, "error", f"{label}: {out.detail}")
        if out.status != "skipped":
            wrote.append(path.name)
    if wrote:
        run.changed = True
        run.details.append(f"wrote {', '.join(wrote[:6])}{' …' if len(wrote) > 6 else ''}")
    return None


def _write_service_file(
    action: Action, plan, service_path: Path, on_conflict: str, label: str, run: _SysmonRun
) -> ActionResult | None:
    """Write the launchd plist / systemd unit honoring ``on_conflict``; a final result when the
    run must stop here (error, or conflict skipped), else None."""
    desired_service = plan.render_plist() if sys.platform == "darwin" else plan.render_unit()
    service_path.parent.mkdir(parents=True, exist_ok=True)
    current = service_path.read_text(encoding="utf-8") if service_path.is_file() else None
    if current == desired_service:
        return None
    out = fsutil.write_file(service_path, desired_service, on_conflict)
    if out.status == "error":
        return ActionResult(action, "error", f"{label}: {out.detail}", out.backup)
    if out.status == "skipped":
        run.details.append(
            f"{service_path.name} differs but on_conflict=skip — left unchanged "
            "(drift NOT reconciled; re-run with on_conflict=backup/overwrite)"
        )
        return ActionResult(
            action, "created" if run.changed else "skipped", f"{label}: {'; '.join(run.details)}"
        )
    if out.backup:
        run.backup = out.backup
        run.details.append(f"backed up prior {service_path.name} → {out.backup.name}")
    run.changed = True
    run.details.append(f"wrote {service_path.name}")
    return None


def _reload_service(plan, service_path: Path) -> int:
    """(Re)load the service in the platform's manager; the first non-zero rc, else 0."""
    if sys.platform == "darwin":
        service_ctl._launchctl_bootout(str(service_path))
        return service_ctl._launchctl_bootstrap(str(service_path))
    return service_ctl._systemctl_user("daemon-reload") or service_ctl._systemctl_user("enable", plan.unit_name) \
        or service_ctl._systemctl_user("restart", plan.unit_name)
