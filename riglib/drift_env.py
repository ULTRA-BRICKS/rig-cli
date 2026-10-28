"""Drift checks for the shell env vars, tmux config and tool provisioning."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from . import apt_packages
from .actions.env_action import env_plan_from_action
from .actions.live_env import _tmux_dry_run
from .actions.tmux import _is_rig_import_line, tmux_plan_from_action
from .actions.tools_provision import _brew_installed_snapshot
from .plan import Action
from .drift_model import DriftItem, DriftReport, _managed_block_body, _on_darwin

if TYPE_CHECKING:
    from .shell_env import ShellEnvPlan
    from .tmux import TmuxPlan
    from .tools import ToolSpec, ToolStatus


def _file_drift(report: DriftReport, action: Action, path: Path, desired: str, label: str) -> None:
    """Append a ``missing`` (absent) or ``modified`` (content differs) DriftItem for a rig file."""
    if not path.is_file():
        report.items.append(DriftItem("missing", "tmux", action.item, path, f"{label} not installed"))
    elif path.read_text(encoding="utf-8") != desired:
        report.items.append(DriftItem("modified", "tmux", action.item, path, f"{label} differs from generated"))


def _check_env(action: Action, report: DriftReport) -> None:
    """Flag drift on the rig-managed shell-env-vars artifacts (never the user's other rc lines).

    missing  — the generated ``rig.env.sh`` is absent, OR rig's ``source <generated file>``
               import line is entirely absent from ``rc_path`` (whether never installed, or
               only a STALE copy from an old ``generated_dir`` lingers there).
    modified — the generated ``rig.env.sh`` on disk differs from the desired render (a hand
               edit of rig's own file, or the configured ``vars`` changed), OR the current
               import line IS present but a STALE copy also coexists with it and needs
               dropping.

    The ``rc_path`` check reuses :func:`riglib.shell_env.desired_rc_text` — apply's EXACT,
    POSITION-TOLERANT predicate — rather than a separate one. A user who relocated rig's own,
    still byte-identical, import line (e.g. above their own exports) is genuinely IN SYNC: apply
    leaves a relocated line alone too (see that function's docstring), so status must not flag
    it either. Sharing one predicate makes divergence between apply and status impossible by
    construction (review finding: an earlier end-anchoring version disagreed with a looser
    drift check here — this one instead shares the exact rule apply itself follows).

    Both file reads (and the defense-in-depth key check inside ``render_env_file``) are guarded
    LOCALLY: a non-UTF-8 or concurrently-removed file, or an invalid var key that somehow
    bypassed ``config.validate``, is reported as a ``modified`` drift item rather than crashing
    the whole `rig status` command (review finding — apply already had this hardening; status
    did not).
    """
    plan = env_plan_from_action(action)
    try:
        desired_file = plan.render_env_file()
    except ValueError as exc:
        # nothing to COMPARE the generated file against, but the rc_path check below (step 2)
        # doesn't depend on this render at all (only on `plan.import_line()`) — so it still
        # runs; only the generated-file comparison itself is skipped (review: an earlier
        # version `return`ed here, silently skipping rc_path too).
        report.items.append(
            DriftItem("modified", "env", action.item, plan.generated_file_path,
                      f"configured vars cannot be rendered: {exc}")
        )
    else:
        # 1) the generated rig.env.sh. Existence is checked regardless of whether the render
        # succeeded (a render failure means "can't compare CONTENT", not "can't tell if it exists")
        # — UNLESS the render already failed, in which case that single item is enough; reporting
        # BOTH "cannot be rendered" AND "not installed" for the SAME target double-counts one
        # artifact in the drift totals (review finding).
        _check_env_generated_file(action, report, plan, desired_file)
    _check_env_rc_import(action, report, plan)


def _check_env_generated_file(action: Action, report: DriftReport, plan: ShellEnvPlan, desired_file: str) -> None:
    """Step 1: the generated rig.env.sh exists and matches the desired render."""
    path = plan.generated_file_path
    if not path.is_file():
        report.items.append(
            DriftItem("missing", "env", action.item, path, "generated rig.env.sh not installed")
        )
        return
    try:
        on_disk = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        report.items.append(DriftItem("modified", "env", action.item, path, f"could not read: {exc}"))
        return
    if on_disk != desired_file:
        report.items.append(
            DriftItem("modified", "env", action.item, path,
                      "generated rig.env.sh differs from configured vars")
        )


def _check_env_rc_import(action: Action, report: DriftReport, plan: ShellEnvPlan) -> None:
    """Step 2: the import line in ``rc_path`` (missing vs stale-copy-alongside, reported distinctly)."""
    from .shell_env import desired_rc_text

    # 2) rc_path — the SAME predicate `_do_provision_env` uses to decide whether to write.
    # Position-tolerance means a differing text has TWO distinct causes, reported distinctly
    # (review finding: an earlier version always said "missing" even when the line was actually
    # present alongside a stale copy — the opposite of the truth):
    #   missing  — the current import line is absent everywhere (never installed, or only a
    #              stale copy from an old generated_dir exists).
    #   modified — the current line IS present, but a STALE copy (old generated_dir) coexists
    #              with it and still needs dropping.
    # KNOWN LIMITATION (accepted, review round 9): "present" here is an EXACT string match
    # against `plan.import_line()`, same as `desired_rc_text`'s own `current_present` check.
    # A line that is functionally equivalent but not byte-identical — hand-quoted differently
    # (`source "..."` vs the canonical bare/`'...'` form `shlex.quote` produces), or written
    # with `$HOME` instead of the literal path — reads as "missing" here even though the file
    # IS being sourced. Only reachable by hand-editing an already-correct line (rig's own
    # output is deterministic and always canonical); apply self-heals it to the canonical form
    # on the next run, so this is a one-time inaccurate STATUS MESSAGE, never a functional bug
    # or a perpetual-drift loop. Narrowing this to semantic (full-path, not just the basename
    # `is_rig_env_import_line` matches) equivalence was judged not worth the added complexity
    # for a hand-edit-only, self-healing edge case.
    if not plan.rc_path.is_file():
        rc_text = ""
    else:
        try:
            rc_text = plan.rc_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            report.items.append(
                DriftItem("modified", "env", action.item, plan.rc_path, f"could not read: {exc}")
            )
            return
    if desired_rc_text(rc_text, plan) == rc_text:
        return
    import_line = plan.import_line()
    if any(ln.strip() == import_line for ln in rc_text.splitlines()):
        report.items.append(
            DriftItem("modified", "env", action.item, plan.rc_path,
                      f"the current import line is present in {plan.rc_path}, but a STALE "
                      "copy (an old generated_dir) also lingers there and needs dropping")
        )
    else:
        report.items.append(
            DriftItem("missing", "env", action.item, plan.rc_path,
                      f"source import line missing from {plan.rc_path}")
        )


def check_disabled_env(action: Action, report: DriftReport) -> None:
    """Flag a still-installed ``rig.env.sh`` and/or a still-live ``rc_path`` import line when
    the config disables the ``env`` category (only fires for an EXPLICIT ``enabled: false`` —
    see the note at the end).

    apply never deletes; so a machine that previously provisioned shell env vars keeps some or
    all of ``rig.env.sh`` / the live ``source`` line in ``rc_path`` even after the config turns
    the category off. With the action gone from the plan, ``_check_env`` never runs — so without
    this scan the leftover would report as "in sync" (mirrors
    :func:`check_disabled_global_excludes`). A HIGHER-consequence orphan than a stale tmux conf
    (env vars keep silently applying to every shell invocation on the machine, not just tmux
    sessions), so — unlike tmux, which has no equivalent check — this one is worth the extra
    scan (review, raised across three rounds).

    Checks BOTH halves of the pair independently — a review finding on an earlier version that
    checked only the generated file: the INVERSE orphan (the generated file was deleted by hand
    but a stale ``source`` line survives in ``rc_path``) is arguably worse, since every zsh
    invocation on the machine then prints a "no such file or directory" error at startup, and
    was previously invisible to this scan.

    Known gap (documented, not fixed here — see ``docs/config-schema.md#env``): this fires ONLY
    for an explicit ``env: {enabled: false}``. Removing the ``env:`` block ENTIRELY — arguably
    the more natural way to "turn a feature off" — runs no scan at all here (``cmd_status``'s
    caller only triggers this on ``enabled is False``), so the same still-active artifacts would
    report as in sync in that case. Probing a fixed default path with no config presence at all
    would need a broader design change (no other disabled-check in this codebase does that
    either); left as a known limitation rather than special-cased here.
    """
    from .shell_env import is_rig_env_import_line

    plan = env_plan_from_action(action)
    generated_exists = plan.generated_file_path.is_file()
    if generated_exists:
        report.items.append(
            DriftItem("extra", "env", action.item, plan.generated_file_path,
                      f"env disabled in config but rig.env.sh is still present and (if "
                      f"{plan.rc_path} still sources it) its vars keep applying to every "
                      f"shell — remove {plan.generated_file_path} and its source line to "
                      "fully turn it off")
        )

    if not plan.rc_path.is_file():
        return
    try:
        rc_text = plan.rc_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # a non-UTF-8/permission-denied/concurrently-removed rc_path must not crash `rig
        # status` (review finding — the same hardening this diff already applies to the three
        # OTHER rc_path reads in `_do_provision_env`/`_check_env`; this fourth one was missed).
        # The generated-file check above already ran and reported independently; silently
        # skipping the rc_path half here is the same "can't inspect it, don't crash" choice
        # `_check_env` makes (there it reports a `modified` item instead — this scan has none
        # of that item's target-uniqueness bookkeeping to hang a matching item off, so it just
        # skips, consistent with returning early on a missing file just above).
        return
    generated_name = plan.generated_file_path.name
    if any(is_rig_env_import_line(ln, plan.import_line(), generated_name) for ln in rc_text.splitlines()):
        detail = (
            f"env disabled in config but {plan.rc_path} still sources rig.env.sh"
            + ("" if generated_exists else f" — {plan.generated_file_path} is GONE, so every "
               "shell invocation now errors at startup ('no such file or directory')")
            + f" — remove the source line from {plan.rc_path} to fully turn it off"
        )
        report.items.append(DriftItem("extra", "env", action.item, plan.rc_path, detail))


def _check_tmux(action: Action, report: DriftReport) -> None:
    """Flag drift on the rig-MANAGED tmux region only (never the user's hand-written lines).

    missing  — the generated ``rig.tmux.conf`` is absent, OR (import mode) ``~/.tmux.conf``
               lost its ``source-file`` import line, OR (block mode) the managed block is gone.
    modified — the generated ``rig.tmux.conf`` on disk differs from the desired one (a hand
               edit of rig's own file), OR a cc script / the boot plist / the managed block differs.
    Shares the desired-artifact computation with the install handler, so apply and status can
    never disagree. User-specific lines in ``~/.tmux.conf`` are ignored entirely. Every region
    ACCUMULATES into ``report.items`` (no early return), so simultaneously-drifted regions are
    ALL surfaced, not just the first.
    """
    plan = tmux_plan_from_action(action)
    desired_conf = plan.render_rig_conf()  # render once; reused by the block-mode check below.

    # 1) the generated rig.tmux.conf — present + byte-identical to the desired render.
    _file_drift(report, action, plan.rig_conf_path, desired_conf, "generated rig.tmux.conf")
    _check_tmux_scripts(action, report, plan)
    if _on_darwin():
        _check_tmux_launchd_plists(action, report, plan)
    _check_tmux_conf_wiring(action, report, plan, desired_conf)

    # 5) live activation state — see _check_tmux_live_state.
    #
    # RIG_TMUX_DRY_RUN suppresses the LIVE half of apply (no plugin clone, no resurrect dir), so
    # status MUST suppress the matching live-state drift too — else status would report drift apply
    # is deliberately not converging (apply/status would disagree, and `rig status` could never read
    # in-sync under the flag, e.g. CI/smoke). The file artifacts (sections 1-4) are written even
    # under dry-run, so their drift stays checked above; only this live section is gated. Reuse the
    # runner's flag helper so the dry-run truthiness has ONE definition across apply + status.
    if _tmux_dry_run():
        return
    _check_tmux_live_state(action, report, plan)


def _check_tmux_scripts(action: Action, report: DriftReport, plan: TmuxPlan) -> None:
    """Section 2: the managed scripts (+x bit) and a leftover tmux-attach.sh."""
    # 2) the managed scripts (cc-save/cc-restore; attach-or-create when anti-sprawl is on) —
    # the SAME list apply writes (managed_scripts), so drift can't disagree on which exist.
    # Content AND the executable bit: a stripped +x means the resurrect hook can't run them.
    for path, body in plan.managed_scripts():
        _file_drift(report, action, path, body, path.name)
        if path.is_file() and not os.access(path, os.X_OK):
            report.items.append(
                DriftItem("modified", "tmux", action.item, path,
                          f"{path.name} is not executable (resurrect hook can't run it)")
            )
    # When anti-sprawl is DISABLED, apply stops writing tmux-attach.sh but never deletes a
    # previously-installed one — and the user may still source it from their shell rc (so
    # anti-sprawl stays active). Surface the leftover as a disk→config extra, like the boot plist.
    if not plan.anti_sprawl_enabled and plan.attach_path.is_file():
        report.items.append(
            DriftItem("extra", "tmux", action.item, plan.attach_path,
                      "tmux-attach.sh present but tmux.anti_sprawl is disabled "
                      "(it may still be wired from your shell rc — remove it or re-enable anti_sprawl)")
        )


def _check_tmux_launchd_plists(action: Action, report: DriftReport, plan: TmuxPlan) -> None:
    """Sections 3/3b: the boot + autosave launchd plists (macOS only; the caller gates on that)."""
    # 3) the boot launchd plist (macOS). When boot is ENABLED, apply writes it → check content.
    # When boot is DISABLED, apply does NOT delete a previously-written plist, so a leftover
    # plist (it still starts tmux at login) is a disk→config EXTRA — surface it, don't go silent.
    if plan.boot_enabled:
        _file_drift(report, action, plan.boot_plist_path, plan.render_boot_plist(),
                    "boot launchd plist")
    elif plan.boot_plist_path.is_file():
        report.items.append(
            DriftItem("extra", "tmux", action.item, plan.boot_plist_path,
                      "boot launchd plist present but tmux.boot is disabled "
                      "(it still starts tmux at login — remove it or re-enable boot)")
        )

    # 3b) the INDEPENDENT autosave plist (#138). Same shape as the boot plist: content-check
    # when enabled, surface a leftover as a disk→config extra when disabled.
    if plan.autosave_enabled:
        _file_drift(report, action, plan.autosave_plist_path, plan.render_autosave_plist(),
                    "autosave launchd plist")
    elif plan.autosave_plist_path.is_file():
        report.items.append(
            DriftItem("extra", "tmux", action.item, plan.autosave_plist_path,
                      "autosave launchd plist present but tmux.autosave is disabled "
                      "(it still saves periodically — remove it or re-enable autosave)")
        )


def _check_tmux_conf_wiring(action: Action, report: DriftReport, plan: TmuxPlan, desired_conf: str) -> None:
    """Section 4: the wiring in ~/.tmux.conf (the managed REGION only — user lines are ignored)."""
    conf_text = plan.conf_path.read_text(encoding="utf-8") if plan.conf_path.is_file() else ""
    if plan.apply_mode == "block":
        _check_tmux_conf_block(action, report, plan, conf_text, desired_conf)
    else:  # import mode — the EXACT import line must be present (not a substring/comment).
        _check_tmux_conf_import(action, report, plan, conf_text)


def _check_tmux_conf_block(
    action: Action, report: DriftReport, plan: TmuxPlan, conf_text: str, desired_conf: str
) -> None:
    """Block mode: the managed block is present and equals the desired render."""
    from .tmux import BLOCK_BEGIN, BLOCK_END

    if BLOCK_BEGIN not in conf_text or BLOCK_END not in conf_text:
        report.items.append(
            DriftItem("missing", "tmux", action.item, plan.conf_path,
                      "managed tmux block missing from ~/.tmux.conf")
        )
    elif _managed_block_body(conf_text, BLOCK_BEGIN, BLOCK_END) != desired_conf.strip("\n"):
        report.items.append(
            DriftItem("modified", "tmux", action.item, plan.conf_path,
                      "managed tmux block in ~/.tmux.conf differs from the configured block")
        )


def _check_tmux_conf_import(action: Action, report: DriftReport, plan: TmuxPlan, conf_text: str) -> None:
    """Import mode: the exact import line is present, not stale, and is the LAST code line."""
    import_line = plan.import_line()
    rig_conf_name = plan.rig_conf_path.name
    lines = conf_text.splitlines()
    if not any(ln.strip() == import_line for ln in lines):
        report.items.append(
            DriftItem("missing", "tmux", action.item, plan.conf_path,
                      "source-file import line missing from ~/.tmux.conf")
        )
    # A STALE rig import (a `source-file …/rig.tmux.conf` from an OLD generated_dir) is still
    # active — tmux sources that old managed file — and `rig apply` WOULD remove it. Flag it,
    # so status doesn't read in-sync while a stale managed config is live (codex P2).
    elif any(
        _is_rig_import_line(ln, import_line, rig_conf_name) and ln.strip() != import_line
        for ln in lines
    ):
        report.items.append(
            DriftItem("modified", "tmux", action.item, plan.conf_path,
                      "a STALE rig source-file import (old generated_dir) is still in "
                      "~/.tmux.conf — apply removes it")
        )
    else:
        _check_tmux_import_ordering(action, report, plan, lines, import_line)


def _check_tmux_import_ordering(
    action: Action, report: DriftReport, plan: TmuxPlan, lines: list[str], import_line: str
) -> None:
    # ORDERING drift: rig appends its import LAST so its generated config (which fixes the
    # Moshi-vs-continuum ordering) wins. If a user added an executable line AFTER the rig
    # import, tmux runs it after rig.tmux.conf and can undo the fix (e.g. a trailing
    # `status-right ''`). apply re-appends the import at the end → flag it (codex P2).
    code = [ln for ln in lines if ln.strip() and not ln.strip().startswith("#")]
    if code and code[-1].strip() != import_line:
        report.items.append(
            DriftItem("modified", "tmux", action.item, plan.conf_path,
                      "rig's source-file import is not the LAST line of ~/.tmux.conf "
                      "(a later line can undo the generated ordering) — apply re-appends it")
        )


def _check_tmux_live_state(action: Action, report: DriftReport, plan: TmuxPlan) -> None:
    """Section 5: the resurrect snapshot dir + the plugin checkouts (DEFECTS 4/6)."""
    # apply now MANAGES these; status must surface them so a clean machine's "no snapshot / no
    # plugins" doesn't read as in-sync (codex finding). A missing/partial plugin or absent
    # resurrect dir is `missing` drift apply reconciles. (launchd loaded-state is intentionally
    # NOT checked here — `rig status` must stay read-only + offline, and a `launchctl list` probe
    # is a live-daemon query; the plist content drift above already covers the agent definition.)
    from .tmux import PLUGINS

    resurrect_dir = plan.home / ".tmux" / "resurrect"
    if not resurrect_dir.is_dir():
        report.items.append(
            DriftItem("missing", "tmux", action.item, resurrect_dir,
                      "~/.tmux/resurrect missing (resurrect writes no snapshot → nothing to "
                      "restore on reboot) — apply creates it")
        )
    plugins_dir = plan.home / ".tmux" / "plugins"
    for name, (_repo, entry) in PLUGINS.items():
        dest = plugins_dir / name
        if not (dest / entry).exists():
            report.items.append(
                DriftItem("missing", "tmux", action.item, dest,
                          f"tmux plugin {name} not installed (the @plugin decl won't resolve) "
                          "— apply clones it")
            )


def _check_tools(action: Action, report: DriftReport) -> None:
    """Flag drift on the personal CLI ecosystem (a declared tool not installed/advertised/dependent).

    missing — a declared tool whose bin does NOT resolve (no managed symlink, not on PATH): the
              ecosystem install never ran or was removed. ``rig apply`` runs its install.sh. A
              declared Homebrew formula (``brew:``) that Homebrew does NOT report as installed is
              ALSO ``missing`` — one item per absent formula, checked independently of the bin/
              advertised state (a formula can go missing — e.g. ``brew uninstall`` — without
              touching the managed symlink at all). ``rig apply`` runs ``brew install`` for it —
              UNLESS ``brew`` itself isn't on PATH, in which case the detail says so instead of
              promising an action apply cannot perform (review finding).
    modified — a tool whose bin resolves but whose skill is NOT advertised (no blurb marker):
               it's reachable but agents can't auto-discover it; apply re-runs install.sh to wire
               up ``install-skill``.
    Shares the desired-spec computation (:func:`tools.tool_status`) with the install handler, and
    the SAME single-probe ``brew list --formula`` snapshot (:func:`runner._brew_installed_snapshot`)
    the install handler uses to decide whether to run ``brew install`` — ONE Homebrew shell-out for
    the whole carrier (not one per declared formula, review finding), so apply and status can never
    disagree. Cross-platform (no launchd); pure reads (no disk/Homebrew mutation).
    """
    from . import tools as toolsmod

    plan = toolsmod.plan_from_action_options(action.options)
    any_brew = any(spec.brew_formulae for spec in plan.specs)
    installed, brew_path = _brew_installed_snapshot() if any_brew else (frozenset(), None)
    for spec in plan.specs:
        _report_tool_bin_drift(report, spec, toolsmod.tool_status(spec))
        _report_missing_brew_formulae(report, spec, installed, brew_path)
    _report_missing_apt_packages(report, [spec for spec in plan.specs if spec.apt_packages])


def _report_tool_bin_drift(report: DriftReport, spec: ToolSpec, st: ToolStatus) -> None:
    """``missing`` when the tool's bin doesn't resolve, else ``modified`` when not advertised."""
    if not st.bin_resolves:
        report.items.append(
            DriftItem(
                "missing", "tools", spec.name, spec.managed_bin,
                f"tool '{spec.name}' declared but not installed "
                f"(no bin at {spec.managed_bin}, not on PATH) — apply runs its install.sh",
            )
        )
    elif not st.advertised:
        report.items.append(
            DriftItem(
                "modified", "tools", spec.name, spec.blurb_file,
                f"tool '{spec.name}' installed but not advertised "
                f"(no skill blurb at {spec.blurb_file}) — apply re-runs install-skill",
            )
        )


def _report_missing_brew_formulae(
    report: DriftReport, spec: ToolSpec, installed: frozenset[str], brew_path: str | None
) -> None:
    """One ``missing`` item per declared Homebrew formula absent from the ``installed`` snapshot."""
    for formula in spec.brew_formulae:
        if formula in installed:
            continue
        remedy = (
            f"install Homebrew, then run `brew install {formula}`" if brew_path is None
            else f"apply runs `brew install {formula}`"
        )
        report.items.append(
            DriftItem(
                "missing", "tools", f"{spec.name}:{formula}", spec.repo,
                f"tool '{spec.name}' declares Homebrew formula '{formula}' but it is not "
                f"installed (`brew list --formula` does not report it) — {remedy}",
            )
        )


def _report_missing_apt_packages(report: DriftReport, apt_specs: list[ToolSpec]) -> None:
    """One ``missing`` item per declared apt package dpkg doesn't report (apt systems only)."""
    if not apt_specs or not apt_packages.is_apt_system():
        return
    have = apt_packages.installed_snapshot()
    for spec in apt_specs:
        for package in apt_packages.missing(spec.apt_packages, have):
            remedy = (
                f"run `{apt_packages.manual_command([package])}` (apply installs it only as root or "
                f"through `sudo -n`, otherwise it just reports this line)"
            )
            report.items.append(
                DriftItem(
                    "missing", "tools", f"{spec.name}:{package}", spec.repo,
                    f"tool '{spec.name}' declares apt package '{package}' but dpkg does not report "
                    f"it installed — {remedy}",
                )
            )
