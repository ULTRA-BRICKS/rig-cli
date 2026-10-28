"""``provision_spotlight`` handler: the macOS Spotlight exclusion agent."""

from __future__ import annotations

import sys

from . import fsutil
from ..plan import Action
from . import live_env
from . import service_ctl
from .result import ActionResult


def _spotlight_dry_run() -> bool:
    """Honor RIG_SPOTLIGHT_DRY_RUN — drop sentinels + write the plist but DON'T ``launchctl load``.

    For CI / containers / smoke where a real ``launchctl load`` (a per-user daemon mutation HOME
    can't redirect) is unwanted. Mirrors ``RIG_SCHEDULE_DRY_RUN`` / ``RIG_TMUX_DRY_RUN``, including
    the automatic sandboxed-HOME skip (:func:`_home_is_sandboxed`, rig-cli#116).
    """
    return live_env._env_flag("RIG_SPOTLIGHT_DRY_RUN") or live_env._home_is_sandboxed()


def _current_plist_note(label: str, plist_path) -> str | None:
    """Note explaining why an already-current plist needs no (re)load, or None if a load is due."""
    # The loaded-probe queries the REAL launchd domain — never under dry-run / a sandboxed
    # HOME (rig-cli#116); a current plist is the whole no-op then.
    if _spotlight_dry_run():
        return (
            f"launchd plist {plist_path} already current "
            f"({live_env._live_skip_reason('RIG_SPOTLIGHT_DRY_RUN', 'launchctl loaded-probe')})"
        )
    if service_ctl._launchctl_loaded(label):
        return f"launchd agent '{label}' already loaded ({plist_path})"
    return None


def _do_provision_spotlight(action: Action, on_conflict: str) -> ActionResult:
    """Sweep the dev roots dropping ``.metadata_never_index`` + install the periodic re-sweep agent.

    Two steps: (1) run the shared sweep (idempotent — a dir that already has the sentinel is left
    alone); (2) on macOS, write + ``launchctl load`` the daily/RunAtLoad re-sweep LaunchAgent (with
    StandardOut/ErrorPath logging). ``RIG_SPOTLIGHT_DRY_RUN`` writes the plist but skips the live
    load. The sweep NEVER forces a full-volume reindex — the sentinels take effect as Spotlight
    re-crawls, which is the whole point (a forced reindex would be the opposite of the goal).
    """
    from .. import spotlight

    opts = action.options
    roots, deny, max_depth = spotlight.sweep_args_from_options(opts)
    label = str(opts.get("label") or spotlight.DEFAULT_BOOT_LABEL)
    sweep_cmd = tuple(str(a) for a in opts.get("sweep_cmd", spotlight.default_sweep_cmd()))

    result = spotlight.perform_sweep(roots, deny, max_depth)
    notes = [f"spotlight: sweep {result.summary()}"]

    plan = spotlight.build_spotlight(
        roots=roots, deny=deny, sweep_cmd=sweep_cmd, label=label, max_depth=max_depth
    )
    if sys.platform != "darwin" or plan.plist_path is None:
        return ActionResult(action, "created" if result.created else "skipped",
                            f"{'; '.join(notes)} (non-macOS: no launchd agent)")

    plist_path = plan.plist_path
    desired = plan.plist_xml()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    already = plist_path.is_file()
    current = plist_path.read_text(encoding="utf-8") if already else ""
    if already and current == desired:
        noop_note = _current_plist_note(label, plist_path)
        if noop_note is not None:
            notes.append(noop_note)
            return ActionResult(action, "created" if result.created else "skipped", "; ".join(notes))
    return _write_and_load_plist(action, plan, (desired, already, current), on_conflict, notes)


def _write_and_load_plist(
    action: Action, plan, existing: tuple[str, bool, str], on_conflict: str, notes: list[str]
) -> ActionResult:
    """Write the re-sweep plist honoring ``on_conflict``, then ``launchctl load`` it unless dry-run.

    ``existing`` is ``(desired_text, already_on_disk, current_text)``.
    """
    desired, already, current = existing
    plist_path = plan.plist_path
    out = fsutil.write_file(plist_path, desired, on_conflict)
    if out.status == "skipped" and already and current != desired:
        notes.append(f"launchd plist {plist_path} differs but on_conflict=skip — left unchanged")
        return ActionResult(action, "skipped", "; ".join(notes))
    if _spotlight_dry_run():
        notes.append(
            f"wrote plist {plist_path} ({live_env._live_skip_reason('RIG_SPOTLIGHT_DRY_RUN', 'launchctl load')})"
        )
        return ActionResult(action, "created", "; ".join(notes), out.backup)
    service_ctl._launchctl("unload", str(plist_path))
    rc = service_ctl._launchctl("load", str(plist_path))
    if rc != 0:
        notes.append(f"wrote {plist_path} but `launchctl load` failed (rc={rc})")
        return ActionResult(action, "error", "; ".join(notes), out.backup)
    notes.append(f"launchd agent '{plan.label}' loaded → daily {plan.human_time} ({plist_path})")
    return ActionResult(action, "created", "; ".join(notes), out.backup)
