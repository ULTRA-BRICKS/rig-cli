"""``provision_tmux`` handler: conf splice, plugin clones, resurrect/continuum and live activation."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import fsutil
from ..plan import Action
from . import live_env
from . import service_ctl
from . import shared
from .result import ActionResult


# ── tmux configuration provisioning (generate + migrate) ───────────────────────────
def tmux_plan_from_action(action: Action):
    """Rebuild the pure :class:`~riglib.tmux.TmuxPlan` an action describes.

    Shared by the install handler and the drift check so both agree on the exact desired
    artifacts (the generated rig.tmux.conf, the cc scripts, the import line / managed block)
    from the action's options. ``Path.home()`` is the resolved HOME at apply time (a test
    monkeypatches it to a tmp HOME). Lazy import keeps the actions package import-light.
    """
    from ..tmux import build_tmux

    opts = action.options
    return build_tmux(
        repo_home=Path.home(),
        apply_mode=str(opts.get("apply_mode", "import")),
        conf_path=str(opts.get("conf_path", "~/.tmux.conf")),
        generated_dir=str(opts.get("generated_dir", "~/.config/rig/tmux")),
        resurrect=dict(opts.get("resurrect", {}) or {}),
        continuum=dict(opts.get("continuum", {}) or {}),
        moshi=dict(opts.get("moshi", {}) or {}),
        cc_restore=dict(opts.get("cc_restore", {}) or {}),
        omp_restore=dict(opts.get("omp_restore", {}) or {}),
        codex_restore=dict(opts.get("codex_restore", {}) or {}),
        anti_sprawl=dict(opts.get("anti_sprawl", {}) or {}),
        boot=dict(opts.get("boot", {}) or {}),
        login_shell=dict(opts.get("login_shell", {}) or {}),
        autosave=dict(opts.get("autosave", {}) or {}),
        recovery=dict(opts.get("recovery", {}) or {}),
        pane_titles=dict(opts.get("pane_titles", {}) or {}),
        focus_events=dict(opts.get("focus_events", {}) or {}),
        clipboard=dict(opts.get("clipboard", {}) or {}),
        copy_mode=dict(opts.get("copy_mode", {}) or {}),
    )


@dataclass
class _ApplyState:
    """Accumulator shared by the tmux artifact-writing steps of one ``provision_tmux`` apply."""

    changed: bool = False
    backup: Path | None = None
    # collect EVERY backup fsutil makes (a differing generated file moved to .rig-bak-* under
    # on_conflict=backup) so a replacement is never silently lost — the repo's "backup-noted"
    # contract. The migration backup is the headline one returned in ActionResult.backup.
    extra_backups: list[Path] = field(default_factory=list)
    details: list[str] = field(default_factory=list)
    # conflict-skipped managed scripts (left as the user's under on_conflict=skip) — surfaced in
    # the result detail but NOT counted as a change (nothing was written): unresolved drift.
    skipped_conflicts: list[str] = field(default_factory=list)
    rig_conf_conflicted: bool = False
    boot_script_conflicted: bool = False
    autosave_script_conflicted: bool = False
    boot_plist_conflicted: bool = False
    boot_plist_changed: bool = False  # plist (re)written this apply → an already-loaded agent is reloaded.
    autosave_plist_conflicted: bool = False
    autosave_plist_changed: bool = False
    conf_conflicted: bool = False


def _write_rig_conf(action: Action, plan, on_conflict: str, st: _ApplyState) -> ActionResult | None:
    """1) the generated rig.tmux.conf (wholesale, idempotent on identical bytes)."""
    # `rig_conf_conflicted` records a DIFFERING generated config left untouched under skip: it may
    # still carry the OLD nonzero @continuum-save-interval, so bootstrapping the autosave agent
    # while continuum keeps saving reintroduces the two-writer race — suppress the autosave load
    # (codex P2), same shape as the plist/script/~/.tmux.conf conflict gates.
    conf_out = fsutil.write_file(plan.rig_conf_path, plan.render_rig_conf(), on_conflict)
    if conf_out.status == "error":
        return ActionResult(action, "error", f"tmux: {conf_out.detail}")
    if conf_out.backup:
        st.extra_backups.append(conf_out.backup)
    if conf_out.status != "skipped":
        st.changed = True
        st.details.append(f"generated {plan.rig_conf_path.name}")
    elif not conf_out.detail.startswith("identical"):
        # rig OWNS rig.tmux.conf — a DIFFERING one left untouched under on_conflict=skip is stale
        # (e.g. an upgrade still carrying the old `@continuum-boot 'on'`) → unresolved drift the
        # sourced tmux still uses. Surface it (NOT silently 'already current'), like the scripts.
        st.rig_conf_conflicted = True
        st.skipped_conflicts.append(
            f"{plan.rig_conf_path.name} differs and on_conflict=skip — NOT regenerated; tmux "
            f"still sources the STALE rig config (re-run with backup/overwrite to update it)"
        )
    return None


def _note_script_conflict(plan, path: Path, st: _ApplyState) -> None:
    if path == plan.boot_script_path:
        st.boot_script_conflicted = True
    if path == plan.autosave_script_path:
        st.autosave_script_conflicted = True
    # a pre-existing DIFFERING file at rig's script path was left untouched under
    # on_conflict=skip — but the generated config wires a resurrect hook at this path, so
    # resurrect would run the user's/stale file. SURFACE it in the detail, but do NOT set
    # `changed` — nothing was written, so a re-apply must NOT report `created` or inflate
    # ApplyReport.changed every run; this is unresolved drift, reported, not a change.
    st.skipped_conflicts.append(
        f"{path.name} differs and on_conflict=skip — NOT applied; the resurrect hook "
        f"points at an unmanaged file (re-run with backup/overwrite to wire rig's script)"
    )


def _write_one_managed_script(action: Action, plan, path: Path, body: str, on_conflict: str, st: _ApplyState) -> ActionResult | None:
    out = fsutil.write_file(path, body, on_conflict)
    if out.status == "error":
        return ActionResult(action, "error", f"tmux: {out.detail}")
    if out.backup:
        st.extra_backups.append(out.backup)
    skipped = out.status == "skipped"
    identical_skip = skipped and out.detail.startswith("identical")
    if not skipped:
        path.chmod(0o755)
        st.changed = True
        st.details.append(f"installed {path.name}")
    elif identical_skip and not os.access(path, os.X_OK):
        # drift-heal: contents identical (OUR file) but +x stripped → chmod IS a real change;
        # report it (don't hide a hook-executable-bit repair behind 'already current'). We
        # NEVER chmod a conflict-skip (a differing pre-existing file is the user's under skip).
        path.chmod(0o755)
        st.changed = True
        st.details.append(f"restored +x on {path.name}")
    if skipped and not identical_skip:
        _note_script_conflict(plan, path, st)
    return None


def _write_managed_scripts(action: Action, plan, on_conflict: str, st: _ApplyState) -> ActionResult | None:
    """2) the managed scripts (chmod +x) — cc-save/cc-restore always, the anti-sprawl
    attach-or-create entry when enabled, the boot script when boot is enabled. `managed_scripts()`
    is the ONE source apply and drift share, so they can't diverge on which scripts exist.
    A DIFFERING boot script left untouched under skip is recorded (the plist the launchd agent
    runs points at THIS script, so loading the agent would run a stale boot script — review P1)."""
    for path, body in plan.managed_scripts():
        err = _write_one_managed_script(action, plan, path, body, on_conflict, st)
        if err is not None:
            return err
    return None


def _write_launchd_plist(
    action: Action, plan, path: Path, text: str, on_conflict: str, st: _ApplyState,
    *, wrote_detail: str, conflict_detail: str,
) -> tuple[ActionResult | None, bool, bool]:
    """Write one macOS launchd plist; returns ``(error_result, conflicted, rewritten)``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    out = fsutil.write_file(path, text, on_conflict)
    if out.status == "error":
        return ActionResult(action, "error", f"tmux: {out.detail}"), False, False
    if out.backup:
        st.extra_backups.append(out.backup)
    if out.status != "skipped":
        st.changed = True
        st.details.append(wrote_detail)
        return None, False, True
    if not out.detail.startswith("identical"):
        # rig OWNS the plist — a DIFFERING one left untouched under skip is stale.
        st.skipped_conflicts.append(conflict_detail)
        return None, True, False
    return None, False, False


def _write_boot_plist(action: Action, plan, on_conflict: str, st: _ApplyState) -> ActionResult | None:
    """3) the boot launchd plist (macOS) — written, never loaded (the user reboots).
    A DIFFERING plist left untouched under on_conflict=skip must make activation NOT
    `launchctl load -w` it (that would ENABLE a stale/unmanaged boot path — codex finding)."""
    if not (plan.boot_enabled and sys.platform == "darwin"):
        return None
    name = plan.boot_plist_path.name
    err, st.boot_plist_conflicted, st.boot_plist_changed = _write_launchd_plist(
        action, plan, plan.boot_plist_path, plan.render_boot_plist(), on_conflict, st,
        wrote_detail=f"wrote boot plist {name} (load on next login/reboot)",
        conflict_detail=(
            f"{name} differs and on_conflict=skip — NOT updated "
            f"(re-run with backup/overwrite to refresh the boot plist)"
        ),
    )
    return err


def _write_autosave_plist(action: Action, plan, on_conflict: str, st: _ApplyState) -> ActionResult | None:
    """3b) the INDEPENDENT autosave launchd plist (#138) — a periodic saver decoupled from
    continuum's status-right hook. Unlike the boot plist, this one is (re)loaded during
    activation (it is a stateless periodic daemon, safe to bootstrap — no live session rides
    on it). A conflict-skipped plist suppresses the load, same as the boot plist."""
    if not (plan.autosave_enabled and sys.platform == "darwin"):
        return None
    name = plan.autosave_plist_path.name
    err, st.autosave_plist_conflicted, st.autosave_plist_changed = _write_launchd_plist(
        action, plan, plan.autosave_plist_path, plan.render_autosave_plist(), on_conflict, st,
        wrote_detail=f"wrote autosave plist {name}",
        conflict_detail=(
            f"{name} differs and on_conflict=skip — NOT updated "
            f"(re-run with backup/overwrite to refresh the autosave plist)"
        ),
    )
    return err


def _wire_tmux_conf(plan, on_conflict: str, st: _ApplyState) -> None:
    """4) ~/.tmux.conf — migrate (back up an inline-settings original) then wire the managed region."""
    from ..tmux import has_inline_rig_settings, neutralize_inline_rig_lines, splice_managed_block

    conf = plan.conf_path
    existing = conf.read_text(encoding="utf-8") if conf.is_file() else ""
    # Migration backup: whenever the conf still carries rig-owned settings inline (so migration
    # will NEUTRALIZE live user lines), snapshot it FIRST under a UNIQUE timestamped name. A
    # fixed `.rig-bak` was written only on the first migration (`not exists()`), so a second
    # apply after the user hand-edited ~/.tmux.conf would neutralize those edits WITHOUT backing
    # them up — losing the in-between state. A timestamped name keeps every restore point.
    if existing and has_inline_rig_settings(existing):
        backup_target = shared._timestamped_backup_path(plan.backup_path)
        backup_target.write_text(existing, encoding="utf-8")
        st.backup = backup_target
        st.details.append(f"backed up original → {backup_target.name}")
        st.changed = True

    desired_conf = _tmux_conf_with_managed(
        plan, existing, splice_managed_block, neutralize_inline_rig_lines
    )
    if desired_conf == existing:
        return
    # Honor on_conflict=skip for the user's OWN file: if ~/.tmux.conf already exists and
    # differs, `skip` means leave it untouched (consistent with the generated artifacts,
    # which go through fsutil.write_file's skip path). A non-existent conf is always created
    # (there's nothing to conflict with). backup/overwrite both proceed to write.
    if on_conflict == "skip" and existing:
        st.conf_conflicted = True
        st.details.append(f"~/.tmux.conf differs but on_conflict=skip — left unwired ({conf.name})")
        return
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text(desired_conf, encoding="utf-8")
    st.changed = True
    st.details.append(f"wired {plan.apply_mode} into {conf.name}")


def _activate_after_write(plan, st: _ApplyState) -> None:
    """5) LIVE activation (DEFECTS 1/4/5/6) — make a CLEAN machine fully working with no manual
    steps; runs even when the file-write path was a no-op (a deleted plugin must be re-cloned on
    re-apply). RIG_TMUX_DRY_RUN skips ALL of it (CI/unit). ONLY real changes mark `changed` (so a
    steady-state re-apply stays a no-op); warnings (failed clone/launchctl — offline) are
    surfaced but never inflate ApplyReport.changed (codex/opus idempotency finding).

    boot_load_safe gates the launchctl-load: if the boot plist, the BOOT SCRIPT it runs, OR the
    ~/.tmux.conf wiring was CONFLICT-skipped (left stale/unwired), loading the agent would enable
    a stale/unmanaged boot path despite skip semantics — so we suppress the load then (review
    findings). Plugins / the resurrect dir / the first save are still safe to run."""
    act_changes, act_warnings = _tmux_activate(
        plan,
        boot_load_safe=not (st.boot_plist_conflicted or st.boot_script_conflicted or st.conf_conflicted),
        boot_plist_changed=st.boot_plist_changed,
        # rig_conf_conflicted too: a conflict-skipped STALE rig.tmux.conf may still carry the old
        # nonzero @continuum-save-interval, so bootstrapping the autosave agent while continuum is
        # ALSO still saving reintroduces the two-writer race this feature removes (codex P2).
        autosave_load_safe=not (
            st.autosave_plist_conflicted or st.autosave_script_conflicted or st.rig_conf_conflicted
        ),
        autosave_plist_changed=st.autosave_plist_changed,
    )
    if act_changes:
        st.changed = True
        st.details.extend(act_changes)
    st.skipped_conflicts.extend(act_warnings)


def _apply_result(action: Action, plan, st: _ApplyState) -> ActionResult:
    if not st.changed:
        # nothing was written. If a managed script was conflict-skipped, that is UNRESOLVED drift
        # (the hook points at an unmanaged file) — report it as `skipped` with the warning, NOT a
        # `created` change (a re-apply must stay idempotent and not inflate ApplyReport.changed).
        if st.skipped_conflicts:
            return ActionResult(
                action, "skipped",
                f"tmux/config: already current EXCEPT — {'; '.join(st.skipped_conflicts)}",
            )
        return ActionResult(action, "skipped", f"tmux/config: already current ({plan.apply_mode} mode)")
    # surface EVERY backup (migration + any generated-file .rig-bak-* moves) so a replacement's
    # restore path is never hidden — the repo's "backup-noted" contract. The headline backup is
    # the migration one if present, else the first generated-file backup.
    if st.extra_backups:
        st.details.append("backups: " + ", ".join(b.name for b in st.extra_backups))
    # a conflict-skipped script alongside other real changes is still surfaced (so the operator
    # sees the unwired hook), appended to the detail of the (genuinely-changed) result.
    st.details.extend(st.skipped_conflicts)
    headline = st.backup or (st.extra_backups[0] if st.extra_backups else None)
    status = "backed_up" if headline else "created"
    return ActionResult(action, status, f"tmux/config: {'; '.join(st.details)}", headline)


def _do_provision_tmux(action: Action, on_conflict: str) -> ActionResult:
    """Generate the rig-managed tmux artifacts and migrate ``~/.tmux.conf`` (idempotent).

    What it writes:
      - ``<generated_dir>/rig.tmux.conf`` — the rig-owned config (wholesale rewrite; the
        ordering guarantee that fixes the continuum-hook-wipe bug lives in the generator).
      - ``<generated_dir>/cc-save.sh`` + ``cc-restore.sh`` — the managed scripts, chmod +x.
      - ``~/.tmux.conf`` — in IMPORT mode, a single ``source-file`` line (the rest of the
        user's file untouched); in BLOCK mode, the managed body spliced between sentinels.
      - the boot launchd plist (macOS) when ``boot.enabled`` — written but NEVER loaded into
        launchd here (the user reloads on their own reboot; we don't disrupt a live server).

    Migration + backup: when the existing ``~/.tmux.conf`` carries rig-owned settings inline
    (resurrect/continuum/tpm/Moshi), the ORIGINAL is backed up to ``~/.tmux.conf.rig-bak``
    BEFORE the rewrite — and an existing backup is never overwritten (the true original is
    preserved). User-specific lines are left intact.

    Idempotent: a re-apply that finds every artifact already current is a ``skipped`` no-op.
    rig NEVER reloads the user's LIVE tmux server (no ``tmux source-file``) — the on-disk
    result is prepared; the user reloads when ready.
    """
    plan = tmux_plan_from_action(action)
    plan.generated_dir.mkdir(parents=True, exist_ok=True)

    st = _ApplyState()
    # tmux.clipboard is on but no clipboard command could be resolved on this platform (no
    # pbcopy/wl-copy/xclip/xsel on PATH, and no explicit override) — render_rig_conf already
    # emits NO clipboard bindings in that case (never a binding that shells out to a missing
    # command). Recorded into `skipped_conflicts` (not `details`) so it stays VISIBLE even on a
    # fully-idempotent "already current" re-apply — `details` is discarded on that path,
    # but `skipped_conflicts` is folded into the message either way (this is the established
    # "surface every apply, changed or not" slot, same as an unresolved conflict-skip).
    if plan.clipboard_enabled and not plan.clipboard_command:
        st.skipped_conflicts.append(
            "clipboard: no clipboard command found on this platform (install pbcopy/wl-copy/"
            "xclip/xsel, or set tmux.clipboard.command) — copy-mode clipboard bindings skipped"
        )

    for write_step in (_write_rig_conf, _write_managed_scripts, _write_boot_plist, _write_autosave_plist):
        err = write_step(action, plan, on_conflict, st)
        if err is not None:
            return err
    _wire_tmux_conf(plan, on_conflict, st)
    _activate_after_write(plan, st)
    return _apply_result(action, plan, st)


def _activate_resurrect_dir(plan, changes: list[str]) -> None:
    """4) the resurrect snapshot dir."""
    resurrect_dir = plan.home / ".tmux" / "resurrect"
    if not resurrect_dir.is_dir():
        resurrect_dir.mkdir(parents=True, exist_ok=True)
        changes.append("created ~/.tmux/resurrect")


def _activate_plugins(plan, changes: list[str], warnings: list[str]) -> None:
    """6) clone missing plugins (idempotent: skip a COMPLETE checkout that already exists). A
    partial dir from a prior failed clone is NOT treated as installed — it is removed and
    re-cloned, so the "offline retries next apply" contract holds (codex finding)."""
    from .. import tmux as tmod

    plugins_dir = plan.home / ".tmux" / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    for name, (repo, entrypoint) in tmod.PLUGINS.items():
        dest = plugins_dir / name
        if (dest / entrypoint).exists():
            continue
        if dest.exists():
            # a partial/broken checkout (entrypoint missing) — clear it so the clone retries clean.
            shutil.rmtree(dest, ignore_errors=True)
        rc = shared._git_clone(repo, dest)
        if rc == 0:
            changes.append(f"installed plugin {name}")
        else:
            # non-fatal (offline / no git): a WARNING, not a change — must not inflate `changed`.
            shutil.rmtree(dest, ignore_errors=True)  # drop any partial dir the failed clone left.
            warnings.append(f"plugin {name} NOT installed (clone failed rc={rc} — offline?)")


def _reload_boot_agent(plan, changes: list[str], warnings: list[str]) -> bool:
    """Refresh a CHANGED plist into the already-running agent: unload then load -w."""
    service_ctl._launchctl("unload", str(plan.boot_plist_path))
    rc = service_ctl._launchctl_load_enable(plan.boot_plist_path)
    if rc != 0:
        warnings.append(
            f"boot agent reload FAILED (launchctl rc={rc}) — it may be left unloaded; "
            f"re-run `rig apply commit` or `launchctl load -w {plan.boot_plist_path}`"
        )
        return False
    changes.append(f"reloaded boot agent {plan.boot_plist_path.name} (plist changed)")
    return True


def _load_boot_agent(plan, changes: list[str], warnings: list[str]) -> bool:
    rc = service_ctl._launchctl_load_enable(plan.boot_plist_path)
    if rc != 0:
        warnings.append(f"boot agent NOT loaded (launchctl rc={rc})")
        return False
    changes.append(f"launchctl load -w {plan.boot_plist_path.name}")
    return True


def _activate_boot_agent(
    plan, *, safe: bool, plist_changed: bool, changes: list[str], warnings: list[str]
) -> bool:
    """1) launchctl-load the boot agent (macOS) so it fires at login. `-w` enables it across reboots.

    Load ONLY when there is real work:
      - NOT loaded  → load it (no unload first — nothing to unload; a steady-state re-apply where
        it's already loaded does NOTHING, so apply stays a no-op AND we never restart it every run
        / re-spawn a `main` session on the live server: review Low).
      - loaded BUT the plist was rewritten this apply (plist_changed) → unload then load -w so
        launchd picks up the new definition (codex: a stale loaded job must be refreshed).
    We unload ONLY in the changed-plist branch, so a transient load failure can leave the agent
    off only when we deliberately refreshed a CHANGED plist (surfaced as a warning) — never for an
    unchanged steady-state agent (review Medium: unconditional unload could disable a working one).
    SUPPRESSED entirely when ``safe`` is False — the plist / boot script / ~/.tmux.conf was
    conflict-skipped (stale/unwired), so loading would enable a stale boot path (review finding).

    Returns whether rig's REPLACEMENT boot agent is actually in place afterwards — freshly loaded,
    or already-loaded-and-still-safe. The stale-boot cleanup is gated on it: removing continuum's
    own autostart while rig has NOT got a working replacement loaded would leave the machine with
    NO tmux autostart at all on the next login (review finding).
    """
    if not (plan.boot_enabled and sys.platform == "darwin" and plan.boot_plist_path.is_file()):
        return False
    if not safe:
        warnings.append(
            "boot agent NOT loaded — the boot plist, boot script, or ~/.tmux.conf was "
            "conflict-skipped (stale/unwired); re-run with on_conflict=backup/overwrite to load it"
        )
        return False
    if not service_ctl._launchctl_loaded(plan.boot_label):
        return _load_boot_agent(plan, changes, warnings)
    if plist_changed:
        return _reload_boot_agent(plan, changes, warnings)
    # already loaded + safe + unchanged → rig's boot is in place (steady-state re-apply).
    return True


def _reload_autosave_agent(plan, pstr: str, changes: list[str], warnings: list[str]) -> None:
    service_ctl._launchctl_bootout(pstr)
    rc = service_ctl._launchctl_bootstrap(pstr)
    if rc != 0:
        warnings.append(
            f"autosave agent reload FAILED (launchctl rc={rc}); re-run `rig apply commit` or "
            f"`launchctl bootstrap {service_ctl._gui_domain()} {pstr}`"
        )
    else:
        changes.append(f"reloaded autosave agent {plan.autosave_plist_path.name} (plist changed)")


def _bootstrap_autosave_agent(plan, pstr: str, changes: list[str], warnings: list[str]) -> None:
    rc = service_ctl._launchctl_bootstrap(pstr)
    if rc != 0:
        warnings.append(f"autosave agent NOT loaded (launchctl bootstrap rc={rc})")
    else:
        changes.append(f"bootstrapped autosave agent {plan.autosave_plist_path.name}")


def _activate_autosave_agent(
    plan, *, safe: bool, plist_changed: bool, changes: list[str], warnings: list[str]
) -> None:
    """1b) bootstrap the INDEPENDENT autosave agent (#138). Unlike the boot agent (RunAtLoad once
    at login), this is a stateless periodic daemon — safe to (re)load NOW (like tg_ctl / the models
    schedule): no live user session rides on it, and loading it makes autosave start immediately
    (it saves even the CURRENT server, no reboot needed). Uses the modern gui-domain
    bootstrap/bootout. Suppressed when the plist/script was conflict-skipped (stale)."""
    if not (plan.autosave_enabled and sys.platform == "darwin" and plan.autosave_plist_path.is_file()):
        return
    pstr = str(plan.autosave_plist_path)
    if not safe:
        warnings.append(
            "autosave agent NOT loaded — its plist or script was conflict-skipped (stale); "
            "re-run with on_conflict=backup/overwrite to load it"
        )
    elif not service_ctl._launchctl_gui_loaded(plan.autosave_label):
        _bootstrap_autosave_agent(plan, pstr, changes, warnings)
    elif plist_changed:
        _reload_autosave_agent(plan, pstr, changes, warnings)
    # already loaded + unchanged → steady-state no-op.


def _tmux_activate(
    plan,
    *,
    boot_load_safe: bool = True,
    boot_plist_changed: bool = False,
    autosave_load_safe: bool = True,
    autosave_plist_changed: bool = False,
) -> tuple[list[str], list[str]]:
    """Bring the rig-managed tmux LIVE on this machine (DEFECTS 1/4/5/6).

    Returns ``(changes, warnings)``: ``changes`` are real mutations the run performed (the caller
    marks the apply ``changed`` ONLY for these, so a steady-state re-apply with nothing to do is a
    genuine no-op); ``warnings`` are non-fatal degradations (a failed clone / launchctl on an
    offline machine) — surfaced to the operator but NEVER counted as a change (else every re-apply
    would falsely report ``created`` — codex/opus idempotency finding).

    ``boot_load_safe`` (caller-supplied): when False — the boot plist / boot script / ``~/.tmux.conf``
    wiring was CONFLICT-skipped (left stale/unwired) — the launchctl-load is SUPPRESSED so we never
    enable a stale/unmanaged boot path despite on_conflict=skip (review finding). The non-boot steps
    (plugins / resurrect dir / first save) still run; they don't risk activating a stale boot.

    ``boot_plist_changed`` (caller-supplied): the boot plist was (re)written this apply. We load the
    agent when it is NOT loaded; we only UNLOAD-then-reload an ALREADY-loaded agent when the plist
    CHANGED. A steady-state re-apply (loaded + unchanged) does nothing — so we never restart the
    agent every run (re-spawning a ``main`` session on the live server) and a transient load failure
    can't disable a working unchanged agent (review findings).

    ``autosave_load_safe`` / ``autosave_plist_changed`` (caller-supplied): the exact same
    conflict-skip-suppression and only-reload-when-changed contract as the boot pair, applied to the
    INDEPENDENT autosave agent (#138). The boot agent uses the LEGACY ``launchctl load -w`` (it is a
    login-fired ``RunAtLoad`` job whose established pattern is the load-w path); the autosave agent
    uses the MODERN gui-domain ``bootstrap``/``bootout`` (matching the stateless-daemon pattern
    ``tg_ctl`` uses) because it must (re)load NOW to start saving the current server without a reboot.

    Steps, each idempotent and non-fatal (a clean machine must end up FULLY working with zero
    manual steps; a partial/offline machine degrades, never aborts the whole apply):

      4) create ``~/.tmux/resurrect`` so resurrect can write its ``tmux_resurrect_*.txt``
         snapshot (absent dir = no snapshot ever written = nothing to restore on reboot).
      1b) on macOS, gui-domain ``bootstrap`` the INDEPENDENT autosave agent so periodic saving
         starts immediately (it saves even a server started under a bad PATH — no reboot needed).
      6) clone the canonical tmux plugins (tpm + resurrect + continuum) into ``~/.tmux/plugins``
         if MISSING (default branch, one-shot, never auto-upgraded — see tmux.PLUGINS' trust
         contract), so the ``@plugin`` declarations actually resolve on a clean machine.
      1) on macOS, ``launchctl load -w`` the boot agent so it FIRES at login (rig used to write
         the plist but never load it). The boot script itself is idempotent (``has-session`` →
         exit 0), so loading it never disrupts an active session.
      5) on macOS, clean continuum's OWN stale boot (``osx_iterm/terminal_start_tmux.sh`` Login
         Items + an old ``Tmux.Start`` launchd agent) that competes with rig's boot agent — gated
         on ``boot.enabled`` (if the user opted OUT of rig boot, never nuke their own autostart).
      6b) take a FIRST ``resurrect save`` ONLY when no snapshot exists yet — so a re-apply never
         re-saves (idempotency) and never clobbers a good snapshot with an empty/partial one.

    ``RIG_TMUX_DRY_RUN`` skips every live step (the file artifacts already landed in the caller),
    silently; a sandboxed HOME (rig-cli#116) skips them too but is SURFACED as a warning.
    """
    if live_env._tmux_dry_run():
        if live_env._env_flag("RIG_TMUX_DRY_RUN"):
            return [], []
        return [], [live_env._live_skip_reason("RIG_TMUX_DRY_RUN", "tmux activation (plugins/launchctl)")]

    changes: list[str] = []
    warnings: list[str] = []
    _activate_resurrect_dir(plan, changes)
    _activate_plugins(plan, changes, warnings)
    rig_boot_active = _activate_boot_agent(
        plan, safe=boot_load_safe, plist_changed=boot_plist_changed, changes=changes, warnings=warnings
    )
    _activate_autosave_agent(
        plan, safe=autosave_load_safe, plist_changed=autosave_plist_changed, changes=changes, warnings=warnings
    )

    # 5) clean continuum's stale macOS boot (Login Items + old Tmux.Start agent) — macOS only, only
    # when rig owns boot (don't remove the user's own autostart if they opted out of rig boot), AND
    # only when rig's REPLACEMENT boot is actually active (never strip the last autostart while our
    # own replacement failed to load / was conflict-skipped — review finding).
    if plan.boot_enabled and sys.platform == "darwin" and rig_boot_active:
        if _clean_stale_continuum_boot(plan):
            changes.append("cleaned stale continuum boot (Login Items / old Tmux.Start)")

    # 6b) take a FIRST resurrect save ONLY if no snapshot exists yet — so a re-apply is a no-op and
    # an existing good snapshot is never clobbered by an empty/partial one (opus finding).
    if not _resurrect_snapshot_exists(plan) and _tmux_resurrect_save(plan) == 0:
        changes.append("took first resurrect save")

    return changes, warnings


def _resurrect_snapshot_exists(plan) -> bool:
    """True if a resurrect snapshot already exists (so we DON'T re-take a first save). resurrect
    writes ``tmux_resurrect_*.txt`` files plus a ``last`` symlink to the newest; either signals a
    prior save."""
    resurrect_dir = plan.home / ".tmux" / "resurrect"
    if not resurrect_dir.is_dir():
        return False
    if (resurrect_dir / "last").exists():
        return True
    return any(resurrect_dir.glob("tmux_resurrect_*.txt"))


def _tmux_resurrect_save(plan) -> int:
    """Take a resurrect snapshot of the CURRENT live tmux server. Returns 0 on success, non-zero
    otherwise. Non-fatal: a machine with no running server (or no resurrect plugin) just doesn't
    get a save (a later apply, after the user starts tmux, retries).

    Saves the server ONLY IF one is ALREADY running — it does NOT start a server to save (opus
    finding): booting a bare ``main`` session and immediately saving could snapshot an empty,
    pre-restore state and clobber a good snapshot. resurrect ships a standalone ``scripts/save.sh``
    that writes the snapshot without a key-binding; we invoke it directly. Does NOT depend on the
    boot script existing (so it still works when ``boot.enabled`` is false but a server is up —
    the boot script is only written when boot is enabled).
    """
    tmux_bin = shutil.which("tmux")
    if not tmux_bin:
        return 127
    save_script = plan.home / ".tmux" / "plugins" / "tmux-resurrect" / "scripts" / "save.sh"
    if not save_script.is_file():
        return 1
    try:
        # only save when a server is already running — never start one just to snapshot it.
        # Probe with `list-sessions`, NOT `has-session`: a bare `has-session` (no `-t`) resolves a
        # target session from $TMUX / the most-recent session, so OUTSIDE tmux (a launchd/cron/plain
        # apply — exactly how rig runs) it can return non-zero even when a server IS up, silently
        # skipping the first save. `list-sessions` exits 0 iff any server with a session is alive,
        # regardless of attach context (1 + "no server running" otherwise). (review finding)
        probe = subprocess.run(
            [tmux_bin, "list-sessions"], capture_output=True, text=True, timeout=10
        )
        if probe.returncode != 0:
            return 1  # no live server → nothing to save (a later apply retries when one is up).
        res = subprocess.run(
            ["bash", str(save_script)], capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return 1
    return res.returncode


def _clean_stale_continuum_boot(plan) -> bool:
    """Disable/remove continuum's OWN macOS boot artifacts that compete with rig's boot agent
    (DEFECT 5). Returns True if there WAS stale state and it was cleaned; False on a clean
    machine (so a re-apply is a no-op — true idempotency, not "ran osx_disable.sh again").

    continuum, when ``@continuum-boot on`` was ever set, installs an iTerm/Terminal-coupled boot:
      - ``~/.tmux/plugins/tmux-continuum/scripts/…`` registers ``osx_iterm_start_tmux.sh`` /
        ``osx_terminal_start_tmux.sh`` as macOS Login Items, AND
      - an old ``Tmux.Start`` launchd agent (``~/Library/LaunchAgents/Tmux.Start.plist``).
    Both fight rig's single launchd boot agent. The stale-boot SIGNAL is the old
    ``Tmux.Start.plist`` (continuum writes it when its boot is enabled; rig never does). Only when
    that signal is present do we (a) run continuum's documented ``osx_disable.sh`` to un-register
    its Login Items, and (b) ``launchctl bootout`` + remove the old plist. A machine with no
    Tmux.Start plist has no stale boot → nothing to do → return False.
    """
    old_plist = plan.home / "Library" / "LaunchAgents" / "Tmux.Start.plist"
    if not old_plist.is_file():
        return False  # no stale continuum boot present — idempotent no-op.

    # un-register continuum's Login Items via its own documented disable script (if installed).
    osx_disable = (
        plan.home / ".tmux" / "plugins" / "tmux-continuum" / "scripts" / "osx_disable.sh"
    )
    if osx_disable.is_file():
        try:
            subprocess.run(["bash", str(osx_disable)], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            pass
    # bootout + unload + remove the old Tmux.Start launchd agent (continuum's iTerm boot).
    uid = os.getuid() if hasattr(os, "getuid") else 0
    service_ctl._launchctl("bootout", f"gui/{uid}/Tmux.Start")
    service_ctl._launchctl("unload", str(old_plist))
    try:
        old_plist.unlink()
    except OSError:
        pass
    return True


def _tmux_conf_with_managed(plan, existing: str, splice, neutralize) -> str:
    """The desired ``~/.tmux.conf`` text for the plan's apply mode (pure).

    - import mode: neutralize the inline rig-owned lines (so the sourced rig config is
      authoritative), then ensure the single ``source-file`` import is present exactly once at
      the end (drop a prior copy so a moved generated path doesn't leave a stale import).
    - block mode: neutralize the SAME inline rig-owned lines in the user's region (a hand-written
      conf carries the old plugin/continuum/resurrect init OUTSIDE where rig splices its block —
      without neutralizing, the user's `run-shell …/continuum.tmux` still fires the double-init
      the live machine hit), then splice the generated body between the sentinels (conda-init
      style). `neutralize` itself skips lines INSIDE the managed block (they are rig's generated
      config), so it never corrupts rig's own region — the correctness does NOT rely on splice
      overwriting the interior.
    """
    if plan.apply_mode == "block":
        neutralized = neutralize(existing) if existing else existing
        return splice(neutralized, plan.render_rig_conf())
    # import mode — neutralize the redundant/harmful inline rig-owned lines FIRST (so the
    # sourced rig.tmux.conf is authoritative; otherwise a leftover inline Moshi `status-right
    # ''` after the user's own continuum init still wipes continuum's hook), then ensure the
    # single source-file import is present exactly once at the END (after the user's lines).
    neutralized = neutralize(existing) if existing else existing
    import_line = plan.import_line()
    rig_conf_name = plan.rig_conf_path.name  # "rig.tmux.conf" — rig owns this filename
    kept = [
        ln for ln in neutralized.splitlines()
        if not _is_rig_import_line(ln, import_line, rig_conf_name)
    ]
    body = "\n".join(kept).rstrip("\n")
    if body:
        return body + "\n" + import_line + "\n"
    return import_line + "\n"


def _is_rig_import_line(line: str, import_line: str, rig_conf_name: str) -> bool:
    """True if ``line`` is rig's OWN ``source-file …/rig.tmux.conf`` import (current or a stale
    one pointing at an old generated_dir) — so it is dropped before re-appending the current
    import exactly once. A comment or a keybinding that merely mentions the path is NOT matched
    (we require the line to BE a `source-file` directive whose argument ends with the rig file).
    """
    s = line.strip()
    if s == import_line:
        return True
    if s.startswith("#"):
        return False
    parts = s.split()
    # `source-file [-q] <path>` — rig's own import names the rig.tmux.conf file as its argument.
    if parts and parts[0] == "source-file":
        arg = parts[-1].strip("'\"")
        return Path(arg).name == rig_conf_name
    return False
