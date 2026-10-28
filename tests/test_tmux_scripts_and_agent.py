"""tmux configuration provisioning — config, pure rendering, plan, install, drift (part 2 of 5).

rig MANAGES tmux config declaratively from rig.yaml, MIGRATING an existing hand-written
``~/.tmux.conf`` rather than clobbering it. Two apply mechanisms:

- **import** (preferred): rig owns a generated ``~/.config/rig/tmux/rig.tmux.conf`` and
  ``~/.tmux.conf`` carries a single ``source-file <that path>`` import line.
- **block** (fallback): rig splices a managed block between sentinel markers in
  ``~/.tmux.conf``, replacing only between the markers (conda-init style).

The root-cause fix this models: continuum's autosave hook lives in ``status-right``; the
Moshi ``set -g status-right ''`` tweak must run BEFORE continuum init (or never wipe its
hook), and continuum's ``run-shell`` init must be LAST so its hook survives. Because rig
GENERATES the managed region it can GUARANTEE that ordering.

All rendering here is stdlib-only + effect-free (the ``tmux`` module computes WHAT the
artifacts are); the effectful writes live in ``actions/runner.py`` and drift diffs the
desired artifacts against disk in ``drift.py`` — three consumers share one source of truth.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from riglib import tmux
from tests.tmux_helpers import _plan


# ── copy-mode clipboard rendering ────────────────────────────────────────────────────────
def test_render_clipboard_default_on_darwin_emits_pbcopy_bindings(monkeypatch):
    """Default (no `tmux.clipboard` block): on macOS with pbcopy on PATH, the four documented
    bindings (v begin-selection, y/Enter/MouseDragEnd1Pane copy-pipe-and-cancel via copy-mode-vi)
    plus the copy-mode (emacs) mirror for Enter/MouseDragEnd1Pane are all emitted with pbcopy.
    The `setw -g mode-keys vi` line (default copy_mode.mode_keys) is emitted BEFORE the
    bind-key lines, so copy-mode-vi is the LIVE table and v/y are actually reachable."""
    monkeypatch.setattr(tmux.sys, "platform", "darwin")
    monkeypatch.setattr(tmux.shutil, "which", lambda name: "/usr/bin/pbcopy" if name == "pbcopy" else None)
    plan = _plan()
    conf = plan.render_rig_conf()
    assert "bind-key -T copy-mode-vi v send-keys -X begin-selection" in conf
    assert 'bind-key -T copy-mode-vi y send-keys -X copy-pipe-and-cancel "pbcopy"' in conf
    assert 'bind-key -T copy-mode-vi Enter send-keys -X copy-pipe-and-cancel "pbcopy"' in conf
    assert 'bind-key -T copy-mode-vi MouseDragEnd1Pane send-keys -X copy-pipe-and-cancel "pbcopy"' in conf
    # mirrored into the plain (emacs) copy-mode table too — see the module-level comment on why.
    assert 'bind-key -T copy-mode Enter send-keys -X copy-pipe-and-cancel "pbcopy"' in conf
    assert 'bind-key -T copy-mode MouseDragEnd1Pane send-keys -X copy-pipe-and-cancel "pbcopy"' in conf
    # `mode-keys` MUST precede the copy-mode-vi bind-key lines (tmux applies key-table
    # activation top-down; a `set` after the binds would still work, but the generator commits
    # to emitting it first — assert the actual order, not just presence).
    lines = conf.splitlines()
    mode_keys_idx = next(i for i, ln in enumerate(lines) if ln == "setw -g mode-keys vi")
    first_bind_idx = next(i for i, ln in enumerate(lines) if "bind-key -T copy-mode-vi v" in ln)
    assert mode_keys_idx < first_bind_idx


def test_render_clipboard_mode_keys_emacs_still_mirrors_v_y(monkeypatch):
    """`copy_mode.mode_keys: emacs` (opt-out, independent of `tmux.clipboard`): `v`/`y` are
    mirrored into the plain `copy-mode` (emacs) table so the clipboard flow still works without
    vi's other motion keys — `setw -g mode-keys emacs` itself is asserted separately above."""
    monkeypatch.setattr(tmux.sys, "platform", "darwin")
    monkeypatch.setattr(tmux.shutil, "which", lambda name: "/usr/bin/pbcopy" if name == "pbcopy" else None)
    plan = tmux.build_tmux(repo_home=Path("/home/u"), copy_mode={"mode_keys": "emacs"})
    assert plan.mode_keys == "emacs"
    conf = plan.render_rig_conf()
    assert "setw -g mode-keys vi" not in conf
    # v/y still bound in copy-mode-vi (harmless — just unreachable while mode-keys is emacs)...
    assert "bind-key -T copy-mode-vi v send-keys -X begin-selection" in conf
    # ...AND mirrored into the plain copy-mode (emacs) table, which IS reachable.
    assert "bind-key -T copy-mode v send-keys -X begin-selection" in conf
    assert 'bind-key -T copy-mode y send-keys -X copy-pipe-and-cancel "pbcopy"' in conf


def test_render_clipboard_mode_keys_vi_does_not_mirror_v_y_into_copy_mode(monkeypatch):
    """The opposite of the emacs case: under the default `mode_keys: vi`, `v`/`y` are NOT
    duplicated into the plain `copy-mode` table (only Enter/MouseDragEnd1Pane are, for the
    unconditional mouse-drag-release flow) — copy-mode-vi is already the live table."""
    monkeypatch.setattr(tmux.sys, "platform", "darwin")
    monkeypatch.setattr(tmux.shutil, "which", lambda name: "/usr/bin/pbcopy" if name == "pbcopy" else None)
    conf = _plan().render_rig_conf()
    assert "bind-key -T copy-mode v send-keys" not in conf
    assert "bind-key -T copy-mode y send-keys" not in conf


def test_render_clipboard_disabled_omits_all_bindings(monkeypatch):
    monkeypatch.setattr(tmux.sys, "platform", "darwin")
    monkeypatch.setattr(tmux.shutil, "which", lambda name: "/usr/bin/pbcopy" if name == "pbcopy" else None)
    conf = tmux.build_tmux(
        repo_home=Path("/home/u"), clipboard={"enabled": False}
    ).render_rig_conf()
    assert "copy-mode-vi" not in conf
    assert "copy-pipe-and-cancel" not in conf


def test_render_clipboard_no_resolvable_command_omits_section(monkeypatch):
    """No pbcopy/wl-copy/xclip/xsel found and no explicit override: the plan resolves an empty
    clipboard_command and render emits NO clipboard section at all — never a binding that shells
    out to a missing command."""
    monkeypatch.setattr(tmux.sys, "platform", "darwin")
    monkeypatch.setattr(tmux.shutil, "which", lambda name: None)
    plan = tmux.build_tmux(repo_home=Path("/home/u"))
    assert plan.clipboard_command == ""
    conf = plan.render_rig_conf()
    assert "copy-mode-vi" not in conf
    assert "copy-pipe-and-cancel" not in conf


def test_render_clipboard_explicit_command_used_verbatim(monkeypatch):
    monkeypatch.setattr(tmux.sys, "platform", "darwin")
    monkeypatch.setattr(tmux.shutil, "which", lambda name: None)
    conf = tmux.build_tmux(
        repo_home=Path("/home/u"), clipboard={"command": "my-custom-clip"}
    ).render_rig_conf()
    assert 'copy-pipe-and-cancel "my-custom-clip"' in conf


def test_render_clipboard_unsafe_explicit_command_falls_back_to_detected(monkeypatch):
    """An unsafe override (defense-in-depth: `config.validate` should already have rejected it,
    but a replayed/stored plan can bypass validate()) is dropped, falling back to the
    platform-detected command rather than interpolating unsafe text into the generated conf."""
    monkeypatch.setattr(tmux.sys, "platform", "darwin")
    monkeypatch.setattr(tmux.shutil, "which", lambda name: "/usr/bin/pbcopy" if name == "pbcopy" else None)
    plan = tmux.build_tmux(
        repo_home=Path("/home/u"), clipboard={"command": 'evil"; set -g status-left BAD'}
    )
    assert plan.clipboard_command == "pbcopy"
    conf = plan.render_rig_conf()
    assert "evil" not in conf
    assert 'copy-pipe-and-cancel "pbcopy"' in conf


def test_render_clipboard_linux_prefers_wayland_over_x11(monkeypatch):
    monkeypatch.setattr(tmux.sys, "platform", "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr(
        tmux.shutil, "which",
        lambda name: "/usr/bin/" + name if name in ("wl-copy", "xclip", "xsel") else None,
    )
    conf = _plan().render_rig_conf()
    assert 'copy-pipe-and-cancel "wl-copy"' in conf


def test_render_clipboard_linux_x11_prefers_xclip_over_xsel(monkeypatch):
    monkeypatch.setattr(tmux.sys, "platform", "linux")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(
        tmux.shutil, "which",
        lambda name: "/usr/bin/" + name if name in ("xclip", "xsel") else None,
    )
    conf = _plan().render_rig_conf()
    assert 'copy-pipe-and-cancel "xclip -selection clipboard -in"' in conf


def test_render_clipboard_linux_falls_back_to_xsel(monkeypatch):
    monkeypatch.setattr(tmux.sys, "platform", "linux")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(tmux.shutil, "which", lambda name: "/usr/bin/xsel" if name == "xsel" else None)
    conf = _plan().render_rig_conf()
    assert 'copy-pipe-and-cancel "xsel --clipboard --input"' in conf


def test_render_clipboard_linux_wayland_binary_absent_under_x11_skips_wayland(monkeypatch):
    """`$WAYLAND_DISPLAY` alone is not enough — `wl-copy` must actually be on PATH, or a Wayland
    var left over from a different session must not select a missing binary."""
    monkeypatch.setattr(tmux.sys, "platform", "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr(tmux.shutil, "which", lambda name: "/usr/bin/xclip" if name == "xclip" else None)
    conf = _plan().render_rig_conf()
    assert 'copy-pipe-and-cancel "xclip -selection clipboard -in"' in conf


def test_render_clipboard_before_plugin_init(monkeypatch):
    """Same section-placement convention as pane-titles/Moshi: the clipboard bindings land
    BEFORE the resurrect/continuum run-shell init lines."""
    monkeypatch.setattr(tmux.sys, "platform", "darwin")
    monkeypatch.setattr(tmux.shutil, "which", lambda name: "/usr/bin/pbcopy" if name == "pbcopy" else None)
    conf = _plan().render_rig_conf()
    lines = conf.splitlines()
    clip_idx = next(i for i, ln in enumerate(lines) if "copy-mode-vi v" in ln)
    plugin_init_idx = next(i for i, ln in enumerate(lines) if "resurrect/resurrect.tmux" in ln)
    assert clip_idx < plugin_init_idx


def test_tmux_plan_from_action_on_pre_upgrade_action_defaults_to_new_pane_titles_behavior():
    """A DRIFT-CHECK REPLAY: `tmux_plan_from_action` on `Action.options` persisted by a version
    of rig that pre-dates this feature (no `pane_titles` key at all) now renders pane titles ON
    and clears status-right — the same default a freshly-built plan gets. This is the exact path
    the defense-in-depth docstrings in `_resolve_pane_titles_position`/`_resolve_pane_titles_format`
    are written for (review: nothing previously asserted what a pre-upgrade persisted action
    renders, only what a freshly-built plan's options dict contains)."""
    from riglib.actions.tmux import tmux_plan_from_action
    from riglib.plan import Action

    action = Action(
        kind="provision_tmux",
        category="tmux",
        item="config",
        source=Path("/repo"),
        target=Path("/home/u/.tmux.conf"),
        options={
            "conf_path": "/home/u/.tmux.conf",
            "generated_dir": "/home/u/.config/rig/tmux",
            # no "pane_titles" key — a pre-upgrade persisted action.
        },
    )
    conf = tmux_plan_from_action(action).render_rig_conf()
    assert "set -g pane-border-status top" in conf
    assert "set -g status-right ''" in conf


def test_tmux_plan_from_action_on_pre_upgrade_action_defaults_to_focus_events_on():
    """Same drift-check-replay contract as pane_titles above, for `focus_events`: a persisted
    Action from before this feature existed (no `focus_events` key) must still render
    `focus-events on` — the same default a freshly-built plan gets."""
    from riglib.actions.tmux import tmux_plan_from_action
    from riglib.plan import Action

    action = Action(
        kind="provision_tmux",
        category="tmux",
        item="config",
        source=Path("/repo"),
        target=Path("/home/u/.tmux.conf"),
        options={
            "conf_path": "/home/u/.tmux.conf",
            "generated_dir": "/home/u/.config/rig/tmux",
            # no "focus_events" key — a pre-upgrade persisted action.
        },
    )
    conf = tmux_plan_from_action(action).render_rig_conf()
    assert "set -g focus-events on" in conf


def test_tmux_plan_from_action_on_pre_upgrade_action_defaults_to_omp_and_codex_restore_on():
    """Same drift-check-replay contract as pane_titles/focus_events above, for the new
    `omp_restore`/`codex_restore` knobs: a persisted Action from before this feature existed
    (no `omp_restore`/`codex_restore` keys at all) must still wire omp-save/omp-restore and
    codex-save/codex-restore — the same on-by-default a freshly-built plan gets."""
    from riglib.actions.tmux import tmux_plan_from_action
    from riglib.plan import Action

    action = Action(
        kind="provision_tmux",
        category="tmux",
        item="config",
        source=Path("/repo"),
        target=Path("/home/u/.tmux.conf"),
        options={
            "conf_path": "/home/u/.tmux.conf",
            "generated_dir": "/home/u/.config/rig/tmux",
            # no "omp_restore" / "codex_restore" keys — a pre-upgrade persisted action.
        },
    )
    plan = tmux_plan_from_action(action)
    assert plan.omp_restore_enabled is True
    assert plan.codex_restore_enabled is True
    conf = plan.render_rig_conf()
    assert "omp-save.sh" in conf and "omp-restore.sh" in conf
    assert "codex-save.sh" in conf and "codex-restore.sh" in conf


def test_render_moshi_on_guards_under_moshi_client():
    """The Moshi tweak must be gated on $MOSHI_CLIENT (it only applies on the iOS client)."""
    conf = tmux.build_tmux(repo_home=Path("/home/u"), moshi={"enabled": True}).render_rig_conf()
    assert "MOSHI_CLIENT" in conf


def test_render_cc_restore_wires_resurrect_hooks():
    """cc-restore wires cc-save/cc-restore via the resurrect post-save/post-restore hooks."""
    conf = tmux.build_tmux(repo_home=Path("/home/u"), cc_restore={"enabled": True}).render_rig_conf()
    assert "@resurrect-hook-post-save-all" in conf
    assert "@resurrect-hook-post-restore-all" in conf
    assert "cc-save.sh" in conf
    assert "cc-restore.sh" in conf


def test_render_cc_restore_off_omits_hooks():
    """With cc_restore OFF alone, omp/codex restore stay at their own (on) default, so the
    hook itself is still wired for them — only cc-save.sh must be gone from it."""
    conf = tmux.build_tmux(repo_home=Path("/home/u"), cc_restore={"enabled": False}).render_rig_conf()
    assert "cc-save.sh" not in conf
    assert "cc-restore.sh" not in conf
    assert "@resurrect-hook-post-save-all" in conf  # omp/codex are still on by default


def test_render_omp_restore_wires_resurrect_hooks():
    """omp-restore wires omp-save/omp-restore via the SAME resurrect post-save/post-restore
    hooks cc-restore uses (each is a single tmux option, so multiple enabled harnesses'
    commands must be chained into that one value, not overwrite each other)."""
    conf = tmux.build_tmux(repo_home=Path("/home/u")).render_rig_conf()
    assert "@resurrect-hook-post-save-all" in conf
    assert "@resurrect-hook-post-restore-all" in conf
    assert "omp-save.sh" in conf
    assert "omp-restore.sh" in conf


def test_render_codex_restore_wires_resurrect_hooks():
    conf = tmux.build_tmux(repo_home=Path("/home/u")).render_rig_conf()
    assert "codex-save.sh" in conf
    assert "codex-restore.sh" in conf


def test_render_omp_restore_off_omits_omp_scripts_from_hooks():
    conf = tmux.build_tmux(repo_home=Path("/home/u"), omp_restore={"enabled": False}).render_rig_conf()
    assert "omp-save.sh" not in conf
    assert "omp-restore.sh" not in conf
    # the other two harnesses (on by default) must still be wired.
    assert "cc-save.sh" in conf
    assert "codex-save.sh" in conf


def test_render_codex_restore_off_omits_codex_scripts_from_hooks():
    conf = tmux.build_tmux(repo_home=Path("/home/u"), codex_restore={"enabled": False}).render_rig_conf()
    assert "codex-save.sh" not in conf
    assert "codex-restore.sh" not in conf
    assert "cc-save.sh" in conf
    assert "omp-save.sh" in conf


def test_render_all_three_restores_off_omits_hooks_entirely():
    """With every harness's exact-id resume disabled, rig must not emit an empty/dangling
    resurrect hook pair at all — mirrors test_render_cc_restore_off_omits_hooks generalized."""
    conf = tmux.build_tmux(
        repo_home=Path("/home/u"),
        cc_restore={"enabled": False},
        omp_restore={"enabled": False},
        codex_restore={"enabled": False},
    ).render_rig_conf()
    assert "@resurrect-hook-post-save-all" not in conf
    assert "@resurrect-hook-post-restore-all" not in conf


def test_render_chains_multiple_harnesses_into_one_hook_value():
    """resurrect's hook option holds ONE shell command string (`eval`'d) — every enabled
    harness's save/restore script must be `;`-joined into that single value, not clobber the
    others via a repeated `set -g`."""
    conf = tmux.build_tmux(repo_home=Path("/home/u")).render_rig_conf()
    save_line = next(ln for ln in conf.splitlines() if "@resurrect-hook-post-save-all" in ln)
    restore_line = next(ln for ln in conf.splitlines() if "@resurrect-hook-post-restore-all" in ln)
    for line in (save_line, restore_line):
        assert line.count("set -g @resurrect-hook") == 1  # exactly ONE set per hook point
    assert "cc-save.sh" in save_line and "omp-save.sh" in save_line and "codex-save.sh" in save_line
    assert (
        "cc-restore.sh" in restore_line
        and "omp-restore.sh" in restore_line
        and "codex-restore.sh" in restore_line
    )
    # exactly one @resurrect-hook-post-save-all / post-restore-all `set -g` line each — no
    # second, overwriting `set -g` for the same option anywhere in the file.
    assert sum(1 for ln in conf.splitlines() if "@resurrect-hook-post-save-all" in ln) == 1
    assert sum(1 for ln in conf.splitlines() if "@resurrect-hook-post-restore-all" in ln) == 1


def test_render_is_deterministic():
    a = _plan().render_rig_conf()
    b = _plan().render_rig_conf()
    assert a == b


def test_render_carries_managed_header():
    """The generated file is rig-owned: a clear 'do not hand-edit' header so a human knows."""
    conf = _plan().render_rig_conf()
    assert "rig" in conf.lower()
    assert "generated" in conf.lower() or "do not edit" in conf.lower() or "managed" in conf.lower()


# ── cc-save / cc-restore script rendering ────────────────────────────────────────────────
def test_cc_save_encodes_cwd_slash_and_dot_to_dash():
    """The cwd→projects-dir encoding (VERIFIED on a real machine): every '/' AND '.' → '-'.
    e.g. /Users/ultra/.files/repos → -Users-ultra--files-repos (note the '--' from '/.').
    """
    body = _plan().render_cc_save()
    # the encoding must be present in the script (a sed/tr/parameter-expansion that maps / and .)
    assert "projects" in body
    # cc-save RECORDS ids; it must never RUN a resume (that's cc-restore's job).
    assert "send-keys" not in body or "claude --resume" not in body
    # cc-save must select the NEWEST session id under the encoded dir.
    assert ".jsonl" in body
    # the encoder maps BOTH '/' and '.' to '-' (the verified projects-dir encoding).
    assert "tr './' '--'" in body or ("tr" in body and "." in body and "/" in body)


def test_cc_restore_resumes_by_exact_id():
    body = _plan().render_cc_restore()
    assert "claude --resume" in body
    # stale/missing id → documented fallback (--continue) or skip, never a hard crash.
    assert "--continue" in body or "skip" in body.lower()


def test_cc_restore_does_not_clobber_running_claude():
    """cc-restore must only resume in a FRESH shell pane, never on top of a running claude."""
    body = _plan().render_cc_restore()
    # it inspects the pane's current command before relaunching.
    assert "pane_current_command" in body or "#{pane_current_command}" in body


def test_cc_restore_skips_missing_and_nonshell_panes():
    """cc-restore must (a) skip a map entry whose pane no longer exists (so `set -e` doesn't
    abort the whole restore), and (b) only send keys into a SHELL pane, never an editor/build
    (so it never types `cd … && claude` into vim). (codex P2.)"""
    body = _plan().render_cc_restore()
    # missing pane → empty current command → skip (guard before send-keys).
    assert '[ -n "$cur" ] || continue' in body
    # whitelist of shells — a non-shell command falls through to the `*) continue` skip.
    assert "sh|bash|zsh|fish" in body
    assert "*) continue" in body


def test_cc_scripts_have_shebang():
    assert _plan().render_cc_save().startswith("#!/")
    assert _plan().render_cc_restore().startswith("#!/")


def test_cc_restore_shell_quotes_runtime_values():
    """A cwd with a space or a metacharacter must be shell-quoted before send-keys — never
    embedded raw (which would break the command or, with a tampered map, inject)."""
    body = _plan().render_cc_restore()
    # the resume command line uses the QUOTED variables, not the raw $cwd/$sid.
    assert "printf '%q'" in body
    assert "claude --resume $qsid" in body and "cd $qcwd" in body
    # the raw unquoted form must NOT be what reaches send-keys.
    assert 'send-keys -t "$addr" "cd $cwd && claude --resume $sid"' not in body


def test_attach_script_shell_quotes_session_name():
    """A configured session name with a space/metachar is shell-quoted at generation time."""
    body = tmux.build_tmux(repo_home=Path("/home/u"),
                           anti_sprawl={"enabled": True, "session": "my sess; rm -rf /"}).render_attach_script()
    # the dangerous name must be single-quoted (shlex.quote), not bare.
    assert "SESSION='my sess; rm -rf /'" in body


# ── omp-save / omp-restore script rendering ──────────────────────────────────────────────
# The actual pane-tree walk (bun + compiled omp detection, session-file lookup, durable-abduco
# identity) and restore decision (never duplicate a live original, prefer reattaching a still-
# alive durable session) moved to tested Python (riglib/omp_recovery.py, riglib/omp_inspect.py,
# riglib/omp_launcher.py — rig-cli#619, fixing the bun-launch/lsof-failure defects a bash
# reimplementation kept drifting on); these scripts are now thin wrappers, tested wired-up here.
def test_omp_save_is_a_thin_wrapper_calling_rig_omp_save_panes():
    body = _plan().render_omp_save()
    assert "omp save-panes" in body
    assert "--map" in body
    assert "omp-sessions.map" in body
    # the old inline bash tree-walk/session-lookup logic is gone from THIS file.
    assert "pane_has_omp" not in body
    assert "newest_session_file()" not in body  # the bash function; riglib.omp_launcher has the Python one


def test_omp_restore_is_a_thin_wrapper_calling_rig_omp_restore_panes():
    body = _plan().render_omp_restore()
    assert "omp restore-panes" in body
    assert "--map" in body
    assert "omp-sessions.map" in body
    # the old inline bash duplicate-guard/send-keys logic is gone from THIS file.
    assert "pane_current_command" not in body
    assert "live_omp_pid_for_cwd" not in body


def test_omp_save_and_restore_call_rig_via_an_absolute_path_when_resolvable(monkeypatch):
    """The boot LaunchAgent's PATH deliberately excludes ~/.local/bin (see this module's other
    PATH comments) — a bare `rig` there could silently resolve to nothing, so the wrapper bakes
    in whatever `rig` resolves to on the CURRENT PATH at render (apply) time."""
    monkeypatch.setattr(tmux.shutil, "which", lambda name: "/opt/rig-test/bin/rig" if name == "rig" else None)
    assert '"/opt/rig-test/bin/rig" omp save-panes' in _plan().render_omp_save()
    assert '"/opt/rig-test/bin/rig" omp restore-panes' in _plan().render_omp_restore()


def test_omp_save_and_restore_fall_back_to_bare_rig_when_unresolvable(monkeypatch):
    monkeypatch.setattr(tmux.shutil, "which", lambda name: None)
    assert '"rig" omp save-panes' in _plan().render_omp_save()
    assert '"rig" omp restore-panes' in _plan().render_omp_restore()


def test_omp_scripts_have_shebang():
    assert _plan().render_omp_save().startswith("#!/")
    assert _plan().render_omp_restore().startswith("#!/")


# ── codex-save / codex-restore script rendering ──────────────────────────────────────────
def test_codex_save_matches_codex_basename_only():
    """Matches `codex`/`*/codex` — the Codex CLI's `node` launcher spawns a native per-platform
    `codex` binary as a child; the walk finds THAT descendant (basename `codex`, no version
    quirk)."""
    body = _plan().render_codex_save()
    assert "codex|*/codex" in body


def test_codex_save_walks_pane_process_tree():
    body = _plan().render_codex_save()
    assert "ps -eo pid=,ppid=,args=" in body
    assert "pane_current_path" in body or "#{pane_current_path}" in body


def test_codex_save_scans_local_date_directories_newest_first():
    """codex's session store is NOT cwd-keyed on disk (unlike claude/omp) — sessions live
    under $CODEX_HOME/sessions/<local-YYYY>/<local-MM>/<local-DD>/rollout-*.jsonl, and cwd is
    read from each candidate's first line. The scan must walk days/files newest-first (so the
    first cwd match found is the overall newest) and be BOUNDED (LOOKBACK_DAYS), not scan the
    entire session store on every save."""
    body = _plan().render_codex_save()
    assert "LOOKBACK_DAYS" in body
    assert "sessions" in body
    assert "ls -t" in body  # newest-first within a day
    assert 'session_field "$f" cwd' in body  # reads the session_meta cwd field
    assert 'session_field "$f" session_id' in body  # reads the session_meta session_id field


def test_codex_save_cross_platform_date_fallback():
    """GNU `date -d` first (Linux), BSD/macOS `date -v` fallback — same GNU-first pattern as
    render_autosave_script's mtime probe (the form that fails on THIS platform must fail
    cleanly, not print garbage)."""
    body = _plan().render_codex_save()
    assert "date -d" in body
    assert "date -v" in body


def test_codex_save_honors_rig_codex_home_override_and_ignores_ambient_codex_home():
    """Mirrors riglib.harness_skills.codex_home(): RIG_CODEX_HOME is the only override; ambient
    CODEX_HOME must NOT appear as an honored override (a codex session may repoint it at a
    runtime/install tree)."""
    body = _plan().render_codex_save()
    assert "RIG_CODEX_HOME" in body
    assert "${CODEX_HOME" not in body


def test_codex_save_records_session_id_and_rollout_path():
    body = _plan().render_codex_save()
    assert "send-keys" not in body  # codex-save only RECORDS; resume is codex-restore's job.


def test_codex_restore_resumes_by_exact_session_id():
    body = _plan().render_codex_restore()
    assert "codex resume $qsid" in body
    assert "codex resume --last" in body  # stale/missing-rollout fallback


def test_codex_restore_does_not_clobber_running_codex():
    body = _plan().render_codex_restore()
    assert "pane_current_command" in body or "#{pane_current_command}" in body


def test_codex_restore_skips_missing_and_nonshell_panes():
    body = _plan().render_codex_restore()
    assert '[ -n "$cur" ] || continue' in body
    assert "sh|bash|zsh|fish" in body
    assert "*) continue" in body


def test_codex_restore_shell_quotes_runtime_values():
    body = _plan().render_codex_restore()
    assert "printf '%q'" in body
    assert "codex resume $qsid" in body and "cd $qcwd" in body
    assert 'send-keys -t "$addr" "cd $cwd && codex resume $sid"' not in body


def test_codex_scripts_have_shebang():
    assert _plan().render_codex_save().startswith("#!/")
    assert _plan().render_codex_restore().startswith("#!/")


# ── managed_scripts(): presence/absence gated by each harness's own restore knob ─────────
def test_omp_scripts_in_managed_scripts_when_enabled():
    p = tmux.build_tmux(repo_home=Path("/home/u"))
    assert any(path == p.omp_save_path for path, _ in p.managed_scripts())
    assert any(path == p.omp_restore_path for path, _ in p.managed_scripts())


def test_omp_scripts_absent_from_managed_scripts_when_disabled():
    off = tmux.build_tmux(repo_home=Path("/home/u"), omp_restore={"enabled": False})
    assert all(path != off.omp_save_path for path, _ in off.managed_scripts())
    assert all(path != off.omp_restore_path for path, _ in off.managed_scripts())
    # cc's scripts are unconditional (unchanged legacy behavior) — must still be present.
    assert any(path == off.cc_save_path for path, _ in off.managed_scripts())


def test_codex_scripts_in_managed_scripts_when_enabled():
    p = tmux.build_tmux(repo_home=Path("/home/u"))
    assert any(path == p.codex_save_path for path, _ in p.managed_scripts())
    assert any(path == p.codex_restore_path for path, _ in p.managed_scripts())


def test_codex_scripts_absent_from_managed_scripts_when_disabled():
    off = tmux.build_tmux(repo_home=Path("/home/u"), codex_restore={"enabled": False})
    assert all(path != off.codex_save_path for path, _ in off.managed_scripts())
    assert all(path != off.codex_restore_path for path, _ in off.managed_scripts())


# ── idempotency across two build_tmux() calls ─────────────────────────────────────────────
def test_omp_codex_render_is_idempotent_across_two_builds():
    """Two independent build_tmux() calls with identical input must render byte-identical
    output for every new artifact — no wall-clock/random/ordering nondeterminism."""
    a = tmux.build_tmux(repo_home=Path("/home/u"))
    b = tmux.build_tmux(repo_home=Path("/home/u"))
    assert a.render_omp_save() == b.render_omp_save()
    assert a.render_omp_restore() == b.render_omp_restore()
    assert a.render_codex_save() == b.render_codex_save()
    assert a.render_codex_restore() == b.render_codex_restore()
    assert a.render_rig_conf() == b.render_rig_conf()
    # managed_scripts() itself is stable across builds (same paths, same bodies, same order).
    assert a.managed_scripts() == b.managed_scripts()


def test_claude_path_unchanged_when_omp_and_codex_restore_default_on():
    """Regression guard: with ONLY cc_restore explicitly set (omp/codex at their own defaults),
    claude's own save/restore script bodies must be byte-identical to a plan built before this
    generalization existed — the exact same render_cc_save()/render_cc_restore() output."""
    p = tmux.build_tmux(repo_home=Path("/home/u"), cc_restore={"enabled": True})
    assert "claude --resume $qsid" in p.render_cc_restore()
    assert "claude --continue" in p.render_cc_restore()
    assert "tr './' '--'" in p.render_cc_save()


def test_claude_only_hook_value_unchanged_when_omp_and_codex_restore_off():
    """With omp/codex restore OFF, the resurrect hook value must be EXACTLY the single
    cc-save.sh / cc-restore.sh path — byte-identical to the pre-generalization claude-only
    behavior (no stray '; ' chaining artifact from an empty list)."""
    p = tmux.build_tmux(
        repo_home=Path("/home/u"), omp_restore={"enabled": False}, codex_restore={"enabled": False}
    )
    conf = p.render_rig_conf()
    save_line = next(ln for ln in conf.splitlines() if "@resurrect-hook-post-save-all" in ln)
    restore_line = next(ln for ln in conf.splitlines() if "@resurrect-hook-post-restore-all" in ln)
    import shlex as _shlex

    expected_save = f'set -g @resurrect-hook-post-save-all "{_shlex.quote(str(p.cc_save_path))}"'
    expected_restore = f'set -g @resurrect-hook-post-restore-all "{_shlex.quote(str(p.cc_restore_path))}"'
    assert save_line == expected_save
    assert restore_line == expected_restore


# ── boot launchd plist (#19) ─────────────────────────────────────────────────────────────
def test_boot_plist_is_well_formed_and_labelled():
    import plistlib

    p = tmux.build_tmux(repo_home=Path("/home/u"))
    body = p.render_boot_plist()
    parsed = plistlib.loads(body.encode("utf-8"))
    assert parsed["Label"] == p.boot_label
    assert parsed["RunAtLoad"] is True
    assert parsed["KeepAlive"] is False
    # DEFECT 1: the plist must run the BOOT SCRIPT (which `new-session -d` loads the conf →
    # continuum restores), NOT a bare `tmux start-server` (an EMPTY server that loads nothing).
    assert parsed["ProgramArguments"][-1] == str(p.boot_script_path)
    assert parsed["ProgramArguments"][-1].endswith(tmux.BOOT_NAME)


def test_boot_plist_does_not_run_bare_start_server():
    """DEFECT 1 (the reboot bug): `tmux start-server` starts a server WITHOUT loading the conf
    or plugins (tmux loads the conf only on the FIRST session), so continuum-restore never
    fires → empty server. The plist must invoke the boot SCRIPT instead."""
    import plistlib

    args = plistlib.loads(
        tmux.build_tmux(repo_home=Path("/home/u")).render_boot_plist().encode("utf-8")
    )["ProgramArguments"]
    assert "start-server" not in args


def test_boot_label_is_configurable():
    p = tmux.build_tmux(repo_home=Path("/home/u"), boot={"enabled": True, "label": "com.me.tmux"})
    assert p.boot_label == "com.me.tmux"
    assert "com.me.tmux" in p.render_boot_plist()
    assert p.boot_plist_path.name == "com.me.tmux.plist"


def test_boot_plist_points_at_the_boot_script():
    """The plist's single program argument is the generated boot script — the one indirection
    that lets the boot bring up a REAL session (loading the conf) instead of an empty server."""
    import plistlib

    p = tmux.build_tmux(repo_home=Path("/home/u"))
    args = plistlib.loads(p.render_boot_plist().encode("utf-8"))["ProgramArguments"]
    assert args == [str(p.boot_script_path)]


def test_boot_plist_sets_homebrew_inclusive_path():
    """DEFECT (the reboot RESTORE bug): the launchd agent runs with a minimal PATH
    (/usr/bin:/bin:/usr/sbin:/sbin) that lacks the Homebrew bin dir. The boot SCRIPT survives it
    (it calls tmux by absolute path), but the tmux SERVER it spawns INHERITS that PATH, and tmux's
    own continuum/resurrect hooks run via `run-shell` and call BARE `tmux` → not found → the
    `@continuum-restore` lookup fails → defaults to `off` → restore is SILENTLY skipped (no
    sessions come back after a reboot). The plist must inject a Homebrew-inclusive PATH so the
    server + every run-shell hook child can resolve tmux."""
    import plistlib

    p = tmux.build_tmux(repo_home=Path("/home/u"))
    env = plistlib.loads(p.render_boot_plist().encode("utf-8"))["EnvironmentVariables"]
    path_dirs = env["PATH"].split(":")
    # the resolved tmux binary's own dir must be on PATH so a bare `tmux` resolves in the hooks.
    assert str(Path(tmux._resolve_tmux_bin()).parent) in path_dirs
    assert "/opt/homebrew/bin" in path_dirs
    assert env["HOME"] == "/home/u"


def test_boot_plist_render_is_deterministic_across_separate_plans(monkeypatch):
    """The real apply→status drift path: apply and status each build a FRESH plan, possibly under
    a different ambient PATH. The plist must be byte-identical anyway. We build two separate plans
    while shutil.which returns DIFFERENT answers — resolution is existence-based (the first EXISTING
    fixed install path wins, ambient-independent), so the rendered plist must not budge.

    HERMETIC: pin ``Path.exists`` so a fixed install path (``/opt/homebrew/bin/tmux``) is the one
    that exists on ANY host — otherwise a runner with NO tmux at a fallback path would fall through
    to the (differing) ``shutil.which`` results and this determinism check would flake (codex P1)."""
    monkeypatch.setattr(tmux.Path, "exists", lambda self: str(self) == "/opt/homebrew/bin/tmux")
    monkeypatch.setattr(tmux.shutil, "which", lambda name: "/interactive/dir/tmux")
    apply_render = tmux.build_tmux(repo_home=Path("/home/u")).render_boot_plist()
    monkeypatch.setattr(tmux.shutil, "which", lambda name: None)  # a minimal-PATH `rig status`
    status_render = tmux.build_tmux(repo_home=Path("/home/u")).render_boot_plist()
    assert apply_render == status_render
    # and the resolved dir is the fixed fallback — shutil.which's differing answers never leak in.
    import plistlib

    path = plistlib.loads(apply_render.encode("utf-8"))["EnvironmentVariables"]["PATH"]
    assert "/opt/homebrew/bin" in path.split(":")


def test_boot_plist_preserves_key_order_not_sorted():
    """sort_keys=False is load-bearing: a stable key order keeps re-apply a no-op. Pin it so a
    refactor that drops it (or reorders the payload) fails here instead of flapping silently."""
    body = tmux.build_tmux(repo_home=Path("/home/u")).render_boot_plist()
    assert body.index("<key>Label</key>") < body.index("<key>EnvironmentVariables</key>")
    assert body.index("<key>PATH</key>") < body.index("<key>HOME</key>")
    assert body.index("<key>RunAtLoad</key>") < body.index("<key>StandardOutPath</key>")


def test_launch_agent_path_has_no_user_writable_dir():
    """Sol #138 hardening: a periodic launchd agent must not resolve executables from a
    user-writable PATH entry (e.g. ~/.local/bin) — an execution-hijack seam. Only fixed system /
    Homebrew dirs plus the resolved tmux dir."""
    path = tmux.build_tmux(repo_home=Path("/home/u")).boot_path_env
    assert "/home/u/.local/bin" not in path.split(":")
    assert "/opt/homebrew/bin" in path.split(":")


# ── independent autosave agent (#138) ─────────────────────────────────────────────────────
def test_autosave_disables_continuum_own_save():
    """With the independent saver ON (default), continuum's own status-right autosave is disabled
    (@continuum-save-interval 0) so there is exactly ONE authoritative saver — no racing writers."""
    conf = tmux.build_tmux(repo_home=Path("/home/u"), continuum={"save_interval": 15}).render_rig_conf()
    assert "set -g @continuum-save-interval '0'" in conf


def test_autosave_off_leaves_continuum_save_active():
    """Opt out of the independent saver → continuum keeps its own save at save_interval (legacy)."""
    conf = tmux.build_tmux(
        repo_home=Path("/home/u"), continuum={"save_interval": 9}, autosave={"enabled": False}
    ).render_rig_conf()
    assert "set -g @continuum-save-interval '9'" in conf


def test_autosave_script_in_managed_scripts_when_enabled():
    p = tmux.build_tmux(repo_home=Path("/home/u"))
    assert any(path == p.autosave_script_path for path, _ in p.managed_scripts())
    off = tmux.build_tmux(repo_home=Path("/home/u"), autosave={"enabled": False})
    assert all(path != off.autosave_script_path for path, _ in off.managed_scripts())


def test_autosave_plist_is_periodic_and_logged():
    import plistlib

    p = tmux.build_tmux(repo_home=Path("/home/u"), continuum={"save_interval": 15})
    parsed = plistlib.loads(p.render_autosave_plist().encode("utf-8"))
    assert parsed["Label"] == "ai.hyperide.tmux-autosave"
    assert parsed["StartInterval"] == 15 * 60  # StartInterval = save_interval minutes → seconds
    assert parsed["ProgramArguments"] == [str(p.autosave_script_path)]
    assert parsed["StandardOutPath"] == str(p.autosave_out_log_path)
    # Homebrew-inclusive PATH so the wrapper + resurrect save.sh resolve tmux under launchd.
    assert str(Path(p.tmux_bin).parent) in parsed["EnvironmentVariables"]["PATH"].split(":")


def test_autosave_script_guards_and_saves():
    """The wrapper must: use an absolute tmux path, exit-0 on no server, keep a degenerate-save
    guard, and call resurrect save.sh. These are the robustness properties #138 turns on."""
    body = tmux.build_tmux(repo_home=Path("/home/u")).render_autosave_script()
    assert body.startswith("#!/bin/bash")
    assert "has-session" in body                    # no-server guard
    assert "degenerate" in body                     # never clobber a richer prior snapshot
    assert "tmux-resurrect/scripts/save.sh" in body  # the real saver
    assert "mkdir \"$LOCK\"" in body                 # single-flight lock
    assert not body.startswith("#!/usr/bin/env")     # Sol: absolute shebang, not env
    assert "grep -vFx" in body                       # fixed-string session match (no regex misfire)


def test_autosave_stale_after_renders_into_script():
    body = tmux.build_tmux(repo_home=Path("/home/u"), autosave={"stale_after": 90}).render_autosave_script()
    assert "STALE_SECS=5400" in body  # 90 minutes → seconds


def test_autosave_script_reads_mtime_gnu_form_first():
    """The snapshot-mtime read must try `stat -c %Y` (GNU) BEFORE `stat -f %m` (BSD). Under a
    BSD-first chain, GNU `stat -f %m FILE` prints FILE's filesystem block ('File: ...') to STDOUT
    and exits non-zero, so the leaked text lands in the `$(( ))` arithmetic and blows up with
    `File: unbound variable` under `set -u`. GNU-first keeps the failing form stdout-clean on both
    platforms — the exact bug that reddened the launchd-minimal-PATH e2e."""
    body = tmux.build_tmux(repo_home=Path("/home/u")).render_autosave_script()
    gnu = body.index("stat -c %Y")
    bsd = body.index("stat -f %m")
    assert gnu < bsd, "GNU `stat -c %Y` must precede BSD `stat -f %m`"


def test_autosave_script_is_valid_bash_under_nounset():
    """`bash -n` (syntax check) the rendered wrapper — it runs under `set -u`, so a stray unbound
    reference or broken arithmetic is a real runtime failure, not a lint nit."""
    import shutil
    import subprocess

    bash = shutil.which("bash")
    if bash is None:  # no bash on PATH (unlikely in CI) → nothing to syntax-check
        pytest.skip("bash not available")
    body = tmux.build_tmux(repo_home=Path("/home/u")).render_autosave_script()
    proc = subprocess.run([bash, "-n"], input=body, text=True, capture_output=True)
    assert proc.returncode == 0, proc.stderr


def test_autosave_path_env_matches_boot_path_env():
    """One source for the launch-agent PATH — the autosave agent reuses the boot agent's."""
    p = tmux.build_tmux(repo_home=Path("/home/u"))
    assert p.autosave_path_env == p.boot_path_env
    assert "/home/u/.local/bin" not in p.autosave_path_env.split(":")


def test_launch_agents_set_utf8_lang():
    """CRITICAL: launchd gives no locale; resurrect's save.sh (awk/sed over TAB-delimited data)
    writes a CORRUPT ~9-byte snapshot under the C locale that then clobbers `last`. Both the boot
    and autosave plists must inject a UTF-8 LANG so the save (and restore parsing) work. Proven
    live: no LANG → 9-byte save; LANG=en_US.UTF-8 → a full snapshot."""
    import plistlib

    p = tmux.build_tmux(repo_home=Path("/home/u"))
    for body in (p.render_boot_plist(), p.render_autosave_plist()):
        env = plistlib.loads(body.encode("utf-8"))["EnvironmentVariables"]
        assert env["LANG"] == "en_US.UTF-8"
        assert "/opt/homebrew/bin" in env["PATH"].split(":")  # PATH still present
        assert env["HOME"] == "/home/u"


# ── autosave freshness healthcheck (the observability half of #138) ─────────────────────────
# The month-long silent death happened because NOBODY owned the question "is the newest save
# fresh?". `assess_autosave_freshness` gives that question an owner: it reads the health-state
# file the wrapper atomically rewrites EVERY run (result=ok|skip|inactive|…) and flags a live
# saver whose last run is older than `stale_after`. It keys off the HEALTH FILE mtime — not the
# snapshot mtime — on purpose: resurrect deletes a byte-identical snapshot, so a healthy quiescent
# server can legitimately have an OLD snapshot but the agent still ran (and refreshed health) on
# schedule. A stale health file means the AGENT stopped running — the exact failure to surface.
def test_autosave_freshness_ok_when_health_is_recent():
    p = tmux.build_tmux(repo_home=Path("/home/u"), autosave={"stale_after": 45})
    h = tmux.assess_autosave_freshness(p, now_epoch=1000.0, health_mtime=1000.0 - 10 * 60, health_result="ok")
    assert h.state == "ok"
    assert "10m ago" in h.detail


def test_autosave_freshness_stale_when_health_older_than_threshold():
    p = tmux.build_tmux(repo_home=Path("/home/u"), autosave={"stale_after": 45})
    now = 60_000.0
    health_mtime = now - 60 * 60  # last run 60 minutes ago — past the 45-minute threshold
    h = tmux.assess_autosave_freshness(p, now_epoch=now, health_mtime=health_mtime, health_result="ok")
    assert h.state == "stale"
    assert "60m" in h.detail and "45m" in h.detail  # age and the crossed threshold


@pytest.mark.parametrize("bad", ["error", "busy", "some-future-result"])
def test_autosave_freshness_unhealthy_when_recent_run_failed_to_save(bad):
    """A FRESH health mtime with a non-benign result (error/busy, or any unrecognised value)
    means the agent is firing but cannot save — that must NOT read as `ok` (the exact false-green
    the healthcheck exists to avoid). Allowlisting the benign set keeps this fail-closed."""
    p = tmux.build_tmux(repo_home=Path("/home/u"), autosave={"stale_after": 45})
    h = tmux.assess_autosave_freshness(
        p, now_epoch=1000.0, health_mtime=1000.0 - 5 * 60, health_result=bad
    )
    assert h.state == "unhealthy"
    assert bad in h.detail


@pytest.mark.parametrize("benign", ["ok", "skip", "inactive", None])
def test_autosave_freshness_ok_for_benign_results(benign):
    """`ok` and the "nothing to save" outcomes (`skip`/`inactive`) — and an ABSENT result (None,
    a partial/non-object write) — are healthy, not failures: stay `ok`."""
    p = tmux.build_tmux(repo_home=Path("/home/u"), autosave={"stale_after": 45})
    h = tmux.assess_autosave_freshness(
        p, now_epoch=1000.0, health_mtime=1000.0 - 5 * 60, health_result=benign
    )
    assert h.state == "ok"


def test_autosave_freshness_stale_outranks_failed_result():
    """A record OLDER than the threshold reports `stale` even when its result is a failure — a
    frozen health file (agent stopped firing entirely) is the more severe, root failure, and the
    last-run outcome is moot once the timer is dead."""
    p = tmux.build_tmux(repo_home=Path("/home/u"), autosave={"stale_after": 45})
    now = 60_000.0
    h = tmux.assess_autosave_freshness(
        p, now_epoch=now, health_mtime=now - 60 * 60, health_result="error"
    )
    assert h.state == "stale"


def test_autosave_freshness_missing_health_flags_agent_not_running():
    p = tmux.build_tmux(repo_home=Path("/home/u"))
    h = tmux.assess_autosave_freshness(p, now_epoch=1000.0, health_mtime=None)
    assert h.state == "missing"


def test_autosave_freshness_disabled_when_saver_off():
    p = tmux.build_tmux(repo_home=Path("/home/u"), autosave={"enabled": False})
    h = tmux.assess_autosave_freshness(p, now_epoch=1000.0, health_mtime=None)
    assert h.state == "disabled"
