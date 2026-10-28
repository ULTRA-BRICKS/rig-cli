"""Shared steps for the script-plus-plist LaunchAgent handlers (tcc-watch, tgctl-reaper).

Both handlers render a helper script and a launchd plist, write each honoring ``on_conflict``,
then (re)load the agent. The dry-run seam (``_*_dry_run``) stays in the handler module and is
passed in as a plain ``bool``; the launchd/live-env seams are looked up on their modules at call
time so tests can monkeypatch them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import fsutil
from . import live_env
from . import service_ctl
from ..plan import Action
from .result import ActionResult


@dataclass
class LaunchAgentRun:
    """What one provision run has done so far, plus the per-handler message vocabulary."""

    prefix: str  # e.g. "tcc_watch/watch" — leads every detail string
    dry_env: str  # e.g. "RIG_TCC_WATCH_DRY_RUN" — named in dry-run skip notes
    script_noun: str  # e.g. "watcher script" — used in the backup note
    details: list[str] = field(default_factory=list)
    changed: bool = False
    backup: Path | None = None

    def result(self, action: Action) -> ActionResult:
        """The terminal "something changed" result."""
        return ActionResult(action, "backed_up" if self.backup else "created",
                            f"{self.prefix}: {'; '.join(self.details)}", self.backup)

    def write_script(
        self, action: Action, path: Path, desired: str, on_conflict: str
    ) -> ActionResult | None:
        """Write the helper script (idempotent on identical bytes); an error result, or None."""
        already = path.is_file()
        current = path.read_text(encoding="utf-8") if already else ""
        if already and current == desired:
            return None
        out = fsutil.write_file(path, desired, on_conflict)
        if out.status == "error":
            return ActionResult(action, "error", f"{self.prefix}: {out.detail}", out.backup)
        if out.backup:
            self.backup = self.backup or out.backup
            self.details.append(f"backed up prior {self.script_noun} → {out.backup.name}")
        if out.status != "skipped":
            self.changed = True
            self.details.append(f"wrote {path.name}")
        path.chmod(0o755)
        return None

    def already_current(self, action: Action, plan, dry: bool) -> ActionResult | None:
        """The install-if-missing no-op: every artifact present AND byte-identical.

        Under dry-run / a sandboxed HOME (rig-cli#116) this is the WHOLE no-op — never probe the
        real launchd domain, and say so. None when the agent still has to be loaded.
        """
        if dry:
            return ActionResult(
                action, "skipped",
                f"{self.prefix}: plist {plan.plist_path} already current "
                f"({live_env._live_skip_reason(self.dry_env, 'launchctl loaded-probe')})",
            )
        if service_ctl._launchctl_gui_loaded(plan.label):
            return ActionResult(
                action, "skipped",
                f"{self.prefix}: launchd agent '{plan.label}' already installed and loaded",
            )
        return None

    def write_plist(
        self, action: Action, plan, desired: str, on_conflict: str, existing: tuple[bool, str]
    ) -> ActionResult | None:
        """Write the plist honoring ``on_conflict``; a final result when the run must stop.

        ``existing`` is ``(already_on_disk, current_text)``.
        """
        already, current = existing
        out = fsutil.write_file(plan.plist_path, desired, on_conflict)
        if out.status == "error":
            return ActionResult(action, "error", f"{self.prefix}: {out.detail}", out.backup)
        if out.backup:
            self.backup = self.backup or out.backup
            self.details.append(f"backed up prior plist → {out.backup.name}")
        # Conflict-skip: the existing plist DIFFERS but on_conflict=skip said don't write it. The
        # desired agent never hit disk, so we must NOT (re)load the stale plist and must NOT
        # report a change — surface the unresolved drift instead.
        if out.status == "skipped" and already and current != desired:
            if self.changed:  # the script still changed — report it, plus the plist skip.
                self.details.append(
                    f"{plan.plist_path.name} differs but on_conflict=skip — left unchanged "
                    f"(drift NOT reconciled; re-run with on_conflict=backup/overwrite)"
                )
                return self.result(action)
            return ActionResult(
                action, "skipped",
                f"{self.prefix}: launchd plist {plan.plist_path} differs but on_conflict=skip — "
                f"left unchanged (drift NOT reconciled; re-run with on_conflict=backup/overwrite)",
            )
        if out.status != "skipped":
            self.changed = True
            self.details.append(f"wrote {plan.plist_path.name}")
        return None

    def dry_result(self, action: Action) -> ActionResult:
        """Result of a dry run: artifacts written, live launchd mutation skipped."""
        return ActionResult(
            action,
            "backed_up" if self.backup else ("created" if self.changed else "skipped"),
            f"{self.prefix}: {'; '.join(self.details) or 'artifacts already current'} "
            f"({live_env._live_skip_reason(self.dry_env, 'launchctl bootstrap')})",
            self.backup,
        )

    def load_agent(self, action: Action, plan) -> ActionResult | None:
        """bootout (a harmless no-op when not loaded) then bootstrap; an error result, or None."""
        service_ctl._launchctl_bootout(str(plan.plist_path))
        rc = service_ctl._launchctl_bootstrap(str(plan.plist_path))
        if rc != 0:
            return ActionResult(
                action, "error",
                f"{self.prefix}: wrote artifacts but `launchctl bootstrap` failed (rc={rc})",
                self.backup,
            )
        if not self.changed:  # artifacts byte-identical but the agent wasn't loaded → loading IS the change.
            self.changed = True
            self.details.append(f"loaded launchd agent '{plan.label}'")
        else:
            self.details.append(f"(re)loaded launchd agent '{plan.label}'")
        return None

    def provision(self, action: Action, plan, script: tuple[Path, str], on_conflict: str, dry: bool) -> ActionResult:
        """Run script -> plist -> (re)load for ``plan`` and build the final result.

        ``script`` is ``(path, desired_text)``; the config dir is created first so launchd can
        open StandardOut/ErrorPath.
        """
        plan.config_dir.mkdir(parents=True, exist_ok=True)

        failure = self.write_script(action, script[0], script[1], on_conflict)
        if failure is not None:
            return failure

        plan.plist_path.parent.mkdir(parents=True, exist_ok=True)
        desired_plist = plan.render_plist()
        plist_already = plan.plist_path.is_file()
        current_plist = plan.plist_path.read_text(encoding="utf-8") if plist_already else ""

        if plist_already and current_plist == desired_plist and not self.changed:
            noop = self.already_current(action, plan, dry)
            if noop is not None:
                return noop

        failure = self.write_plist(action, plan, desired_plist, on_conflict, (plist_already, current_plist))
        if failure is not None:
            return failure

        if dry:
            return self.dry_result(action)

        # (re)load via the per-user gui domain so a changed plist is picked up without a reboot.
        if self.changed or not service_ctl._launchctl_gui_loaded(plan.label):
            failure = self.load_agent(action, plan)
            if failure is not None:
                return failure

        if not self.changed:
            return ActionResult(action, "skipped",
                                f"{self.prefix}: launchd agent '{plan.label}' already installed and loaded")
        return self.result(action)
