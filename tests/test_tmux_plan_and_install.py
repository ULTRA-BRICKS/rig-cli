"""tmux configuration provisioning — config, pure rendering, plan, install, drift (part 4 of 5).

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
from tests.tmux_helpers import _build, _stage_live_tmux_state, _tmux_action


# ── migration detection: rig-owned settings already inline ───────────────────────────────
def test_detect_inline_rig_settings():
    """First apply detects rig-owned settings already hand-written inline (resurrect/continuum/
    tpm/Moshi) so the migration knows there is something to lift into the import/block.
    """
    handwritten = (
        "set -g @plugin 'tmux-plugins/tpm'\n"
        "set -g @plugin 'tmux-plugins/tmux-resurrect'\n"
        "set -g @plugin 'tmux-plugins/tmux-continuum'\n"
        "set -g @continuum-restore 'on'\n"
        "run '~/.tmux/plugins/tpm/tpm'\n"
    )
    assert tmux.has_inline_rig_settings(handwritten) is True


def test_detect_no_inline_rig_settings():
    plain = "set -g mouse on\nset -g history-limit 100000\n"
    assert tmux.has_inline_rig_settings(plain) is False


def test_detect_bare_moshi_wipe_triggers_backup():
    """A conf whose ONLY rig-relevant line is a bare `set -g status-right ''` must still count as
    'has inline settings' — migration will neutralize it, so the backup contract requires a
    backup first (codex P2)."""
    bare = "set -g mouse on\nset -g status-right ''\n"
    assert tmux.has_inline_rig_settings(bare) is True


def test_detect_update_environment_moshi_client_does_not_trigger_backup():
    """`set-option -ga update-environment ' MOSHI_CLIENT'` is a PERSONAL pref migration keeps live
    (NOT the wipe). It must NOT count as 'has inline settings' — the OLD substring `MOSHI_CLIENT`
    marker made it trigger a backup on EVERY apply (perpetual churn). `has_inline_rig_settings` is
    now defined AS 'neutralization changes the region', so the backup gate stays consistent with
    what neutralization does — this pins that consistency directly (not just transitively)."""
    pref = "set -g mouse on\nset-option -ga update-environment ' MOSHI_CLIENT'\n"
    assert tmux.has_inline_rig_settings(pref) is False
    # a fully-migrated import conf (commented legacy + the pref live) is likewise NOT migratable.
    migrated = tmux.neutralize_inline_rig_lines(
        "set -g @plugin 'tmux-plugins/tpm'\nset-option -ga update-environment ' MOSHI_CLIENT'\n"
    )
    assert tmux.has_inline_rig_settings(migrated) is False


def test_plan_no_tmux_when_absent(fake_agent_tools, tmp_path):
    plan = _build({}, tmp_path, fake_agent_tools)
    assert not [a for a in plan.actions if a.kind == "provision_tmux"]


def test_plan_no_tmux_when_disabled(fake_agent_tools, tmp_path):
    plan = _build({"tmux": {"enabled": False}}, tmp_path, fake_agent_tools)
    assert not [a for a in plan.actions if a.kind == "provision_tmux"]


def test_plan_emits_tmux_action(fake_agent_tools, tmp_path):
    plan = _build({"tmux": {"enabled": True}}, tmp_path, fake_agent_tools)
    acts = [a for a in plan.actions if a.kind == "provision_tmux"]
    assert len(acts) == 1
    a = acts[0]
    assert a.category == "tmux" and a.item == "config"
    assert a.options["apply_mode"] == "import"


def test_plan_carries_pane_titles_block(fake_agent_tools, tmp_path):
    plan = _build(
        {"tmux": {"enabled": True, "pane_titles": {"position": "bottom"}}}, tmp_path, fake_agent_tools
    )
    a = [a for a in plan.actions if a.kind == "provision_tmux"][0]
    assert a.options["pane_titles"] == {"position": "bottom"}


def test_plan_defaults_pane_titles_to_empty_dict_when_absent(fake_agent_tools, tmp_path):
    plan = _build({"tmux": {"enabled": True}}, tmp_path, fake_agent_tools)
    a = [a for a in plan.actions if a.kind == "provision_tmux"][0]
    assert a.options["pane_titles"] == {}


def test_plan_to_render_round_trip_carries_pane_titles_position(fake_agent_tools, tmp_path):
    """rig.yaml → plan action → runner's tmux_plan_from_action → render, end to end."""
    from riglib.actions.tmux import tmux_plan_from_action

    plan = _build(
        {"tmux": {"enabled": True, "pane_titles": {"position": "bottom"}}}, tmp_path, fake_agent_tools
    )
    a = next(act for act in plan.actions if act.kind == "provision_tmux")
    conf = tmux_plan_from_action(a).render_rig_conf()
    assert "set -g pane-border-status bottom" in conf


def test_plan_to_render_round_trip_carries_pane_titles_format_and_clear_status_right(
    fake_agent_tools, tmp_path
):
    from riglib.actions.tmux import tmux_plan_from_action

    plan = _build(
        {
            "tmux": {
                "enabled": True,
                "pane_titles": {
                    "format": "#{pane_index}: #{pane_title}",
                    "clear_status_right": False,
                },
            }
        },
        tmp_path,
        fake_agent_tools,
    )
    a = next(act for act in plan.actions if act.kind == "provision_tmux")
    conf = tmux_plan_from_action(a).render_rig_conf()
    assert 'pane-border-format "#{pane_index}: #{pane_title}"' in conf
    assert "status-right ''" not in conf


def test_plan_carries_focus_events_block(fake_agent_tools, tmp_path):
    plan = _build(
        {"tmux": {"enabled": True, "focus_events": {"enabled": False}}}, tmp_path, fake_agent_tools
    )
    a = [a for a in plan.actions if a.kind == "provision_tmux"][0]
    assert a.options["focus_events"] == {"enabled": False}


def test_plan_defaults_focus_events_to_empty_dict_when_absent(fake_agent_tools, tmp_path):
    plan = _build({"tmux": {"enabled": True}}, tmp_path, fake_agent_tools)
    a = [a for a in plan.actions if a.kind == "provision_tmux"][0]
    assert a.options["focus_events"] == {}


def test_plan_to_render_round_trip_carries_focus_events_disabled(fake_agent_tools, tmp_path):
    """rig.yaml -> plan action -> runner's tmux_plan_from_action -> render, end to end.

    Exercises the FULL pass-through chain (`plan.py`'s options dict, then
    `runner.tmux_plan_from_action`'s kwarg) for the disabled case specifically — the other
    focus-events tests either call `tmux.build_tmux` directly (bypassing `plan.py`/`runner.py`)
    or only assert the default-on value, so a typo'd/missing pass-through in either layer
    would silently keep rendering 'on' and slip past every other test here undetected."""
    from riglib.actions.tmux import tmux_plan_from_action

    plan = _build(
        {"tmux": {"enabled": True, "focus_events": {"enabled": False}}}, tmp_path, fake_agent_tools
    )
    a = next(act for act in plan.actions if act.kind == "provision_tmux")
    conf = tmux_plan_from_action(a).render_rig_conf()
    assert "set -g focus-events off" in conf
    assert "set -g focus-events on" not in conf


def test_plan_carries_clipboard_block(fake_agent_tools, tmp_path):
    plan = _build(
        {"tmux": {"enabled": True, "clipboard": {"command": "my-clip"}}}, tmp_path, fake_agent_tools
    )
    a = [a for a in plan.actions if a.kind == "provision_tmux"][0]
    assert a.options["clipboard"] == {"command": "my-clip"}


def test_plan_defaults_clipboard_to_empty_dict_when_absent(fake_agent_tools, tmp_path):
    plan = _build({"tmux": {"enabled": True}}, tmp_path, fake_agent_tools)
    a = [a for a in plan.actions if a.kind == "provision_tmux"][0]
    assert a.options["clipboard"] == {}


def test_plan_to_render_round_trip_carries_clipboard_disabled(fake_agent_tools, tmp_path, monkeypatch):
    """rig.yaml -> plan action -> runner's tmux_plan_from_action -> render, end to end — the same
    FULL pass-through chain the focus-events round-trip test above exercises, for `clipboard`."""
    from riglib.actions.tmux import tmux_plan_from_action

    monkeypatch.setattr(tmux.sys, "platform", "darwin")
    monkeypatch.setattr(tmux.shutil, "which", lambda name: "/usr/bin/pbcopy" if name == "pbcopy" else None)
    plan = _build(
        {"tmux": {"enabled": True, "clipboard": {"enabled": False}}}, tmp_path, fake_agent_tools
    )
    a = next(act for act in plan.actions if act.kind == "provision_tmux")
    conf = tmux_plan_from_action(a).render_rig_conf()
    assert "copy-pipe-and-cancel" not in conf


def test_plan_to_render_round_trip_carries_clipboard_command_override(
    fake_agent_tools, tmp_path, monkeypatch
):
    from riglib.actions.tmux import tmux_plan_from_action

    monkeypatch.setattr(tmux.sys, "platform", "darwin")
    monkeypatch.setattr(tmux.shutil, "which", lambda name: None)
    plan = _build(
        {"tmux": {"enabled": True, "clipboard": {"command": "my-clip"}}}, tmp_path, fake_agent_tools
    )
    a = next(act for act in plan.actions if act.kind == "provision_tmux")
    conf = tmux_plan_from_action(a).render_rig_conf()
    assert 'copy-pipe-and-cancel "my-clip"' in conf


def test_plan_carries_copy_mode_block(fake_agent_tools, tmp_path):
    plan = _build(
        {"tmux": {"enabled": True, "copy_mode": {"mode_keys": "emacs"}}}, tmp_path, fake_agent_tools
    )
    a = [a for a in plan.actions if a.kind == "provision_tmux"][0]
    assert a.options["copy_mode"] == {"mode_keys": "emacs"}


def test_plan_defaults_copy_mode_to_empty_dict_when_absent(fake_agent_tools, tmp_path):
    plan = _build({"tmux": {"enabled": True}}, tmp_path, fake_agent_tools)
    a = [a for a in plan.actions if a.kind == "provision_tmux"][0]
    assert a.options["copy_mode"] == {}


def test_plan_to_render_round_trip_carries_copy_mode_emacs(fake_agent_tools, tmp_path):
    """rig.yaml -> plan action -> runner's tmux_plan_from_action -> render, end to end.

    Exercises the FULL pass-through chain (`plan.py`'s options dict, then
    `runner.tmux_plan_from_action`'s kwarg) — mirrors
    test_plan_to_render_round_trip_carries_focus_events_disabled's rationale: a typo'd/missing
    pass-through in either layer would silently keep rendering the default 'vi' and slip past
    every other test here undetected."""
    from riglib.actions.tmux import tmux_plan_from_action

    plan = _build(
        {"tmux": {"enabled": True, "copy_mode": {"mode_keys": "emacs"}}}, tmp_path, fake_agent_tools
    )
    a = next(act for act in plan.actions if act.kind == "provision_tmux")
    conf = tmux_plan_from_action(a).render_rig_conf()
    assert "setw -g mode-keys emacs" in conf
    assert "setw -g mode-keys vi" not in conf


def test_plan_disables_autosave_off_darwin(fake_agent_tools, tmp_path, monkeypatch):
    """The autosave LaunchAgent is macOS-only — on a non-darwin host the plan forces
    autosave.enabled False so the generated config keeps continuum's own save (never disabling it
    without a replacement runner → the user would lose autosave entirely). codex P1."""
    import riglib.plan_system as plan_mod

    monkeypatch.setattr(plan_mod.sys, "platform", "linux")
    plan = _build({"tmux": {"enabled": True}}, tmp_path, fake_agent_tools)
    a = [a for a in plan.actions if a.kind == "provision_tmux"][0]
    assert a.options["autosave"]["enabled"] is False


def test_plan_keeps_autosave_on_darwin(fake_agent_tools, tmp_path, monkeypatch):
    """On darwin the plan does NOT force autosave off — the default (knob absent → on) stands."""
    import riglib.plan_system as plan_mod

    monkeypatch.setattr(plan_mod.sys, "platform", "darwin")
    plan = _build({"tmux": {"enabled": True}}, tmp_path, fake_agent_tools)
    a = [a for a in plan.actions if a.kind == "provision_tmux"][0]
    assert a.options["autosave"].get("enabled") is not False


def test_plan_provisions_when_enabled_is_null(fake_agent_tools, tmp_path):
    """`enabled: null` is 'not false' → provision (a present-but-empty-ish block opts in)."""
    plan = _build({"tmux": {"enabled": None, "apply": "import"}}, tmp_path, fake_agent_tools)
    assert [a for a in plan.actions if a.kind == "provision_tmux"]


def test_plan_provisions_for_empty_tmux_block(fake_agent_tools, tmp_path):
    """An explicit empty mapping `tmux: {}` is a PRESENT block → it opts in with all defaults
    (codex P2: it must not be treated as absent)."""
    plan = _build({"tmux": {}}, tmp_path, fake_agent_tools)
    assert [a for a in plan.actions if a.kind == "provision_tmux"]


def test_plan_resolves_relative_paths_against_repo_root(fake_agent_tools, tmp_path):
    """A relative tmux.conf_path / generated_dir must resolve against the -C REPO ROOT, not the
    process CWD — so `rig apply -C /repo` from elsewhere never writes outside the repo. (codex P2.)
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = _build(
        {"tmux": {"enabled": True, "conf_path": "sub/.tmux.conf",
                  "generated_dir": "sub/gen"}},
        repo, fake_agent_tools,
    )
    a = next(a for a in plan.actions if a.kind == "provision_tmux")
    # the options carry ABSOLUTE, repo-anchored paths (not the bare relative strings).
    assert a.options["conf_path"] == str(repo / "sub" / ".tmux.conf")
    assert a.options["generated_dir"] == str(repo / "sub" / "gen")


def test_plan_carries_resolved_knobs(fake_agent_tools, tmp_path):
    plan = _build(
        {"tmux": {"enabled": True, "apply": "block", "moshi": {"enabled": True},
                  "continuum": {"save_interval": 9},
                  "login_shell": {"enabled": False, "shell": "/bin/zsh"}}},
        tmp_path, fake_agent_tools,
    )
    a = next(a for a in plan.actions if a.kind == "provision_tmux")
    assert a.options["apply_mode"] == "block"
    assert a.options["moshi"]["enabled"] is True
    assert a.options["continuum"]["save_interval"] == 9
    assert a.options["login_shell"] == {"enabled": False, "shell": "/bin/zsh"}


def test_apply_login_shell_default_command_lands_in_generated_conf(tmp_path, monkeypatch):
    """The login-shell default-command (DEFECT 3) reaches the generated rig.tmux.conf on apply."""
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    runner._do_provision_tmux(_tmux_action(home), "backup")
    conf = (home / ".config" / "rig" / "tmux" / "rig.tmux.conf").read_text()
    assert "set -g default-command" in conf and "-l" in conf


def test_apply_login_shell_disabled_omits_default_command(tmp_path, monkeypatch):
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    runner._do_provision_tmux(_tmux_action(home, login_shell={"enabled": False}), "backup")
    conf = (home / ".config" / "rig" / "tmux" / "rig.tmux.conf").read_text()
    assert "set -g default-command" not in conf


def test_apply_import_writes_generated_file_and_import_line(tmp_path, monkeypatch):
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    res = runner._do_provision_tmux(_tmux_action(home), "backup")
    assert res.status in ("created", "updated", "backed_up")
    rig_conf = home / ".config" / "rig" / "tmux" / "rig.tmux.conf"
    assert rig_conf.is_file()
    assert "@resurrect-processes" in rig_conf.read_text()
    conf = (home / ".tmux.conf").read_text()
    assert f"source-file '{rig_conf}'" in conf


def test_apply_installs_cc_scripts_executable(tmp_path, monkeypatch):
    import os

    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    runner._do_provision_tmux(_tmux_action(home), "backup")
    gdir = home / ".config" / "rig" / "tmux"
    for name in ("cc-save.sh", "cc-restore.sh"):
        p = gdir / name
        assert p.is_file()
        assert os.access(p, os.X_OK), f"{name} must be executable"


def test_apply_installs_attach_script_when_anti_sprawl_on(tmp_path, monkeypatch):
    import os

    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    runner._do_provision_tmux(_tmux_action(home, anti_sprawl={"enabled": True, "session": "main"}), "backup")
    p = home / ".config" / "rig" / "tmux" / "tmux-attach.sh"
    assert p.is_file() and os.access(p, os.X_OK)
    assert "attach-session" in p.read_text() and "new-session" in p.read_text()


def test_apply_omits_attach_script_when_anti_sprawl_off(tmp_path, monkeypatch):
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    runner._do_provision_tmux(_tmux_action(home, anti_sprawl={"enabled": False}), "backup")
    assert not (home / ".config" / "rig" / "tmux" / "tmux-attach.sh").exists()


def test_apply_import_is_idempotent(tmp_path, monkeypatch):
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    a = _tmux_action(home)
    runner._do_provision_tmux(a, "backup")
    res2 = runner._do_provision_tmux(a, "backup")
    assert res2.status == "skipped", res2.detail


def test_apply_migrates_and_backs_up_handwritten_conf(tmp_path, monkeypatch):
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    # a hand-written conf with rig-owned settings inline, the BUGGY Moshi wipe, AND a user line.
    conf = home / ".tmux.conf"
    conf.write_text(
        "set -g @plugin 'tmux-plugins/tmux-continuum'\n"
        "set -g @continuum-restore 'on'\n"
        "run-shell ~/.tmux/plugins/tmux-continuum/continuum.tmux\n"
        "if-shell '[ -n \"$MOSHI_CLIENT\" ]' { set -g status-right '' }\n"
        "set -g mouse on  # user line\n",
        encoding="utf-8",
    )
    res = runner._do_provision_tmux(_tmux_action(home), "backup")
    assert res.status in ("created", "updated", "backed_up")
    # original backed up to a UNIQUE timestamped ~/.tmux.conf.rig-bak-<UTC> (never clobbering).
    baks = list(home.glob(".tmux.conf.rig-bak-*"))
    assert len(baks) == 1, f"expected one timestamped backup, got {baks}"
    bak_text = baks[0].read_text()
    assert "set -g mouse on" in bak_text and "status-right ''" in bak_text
    # the new conf carries the import line exactly once.
    new_text = conf.read_text()
    assert sum(1 for ln in new_text.splitlines() if ln.strip().startswith("source-file")) == 1
    # the BUGGY Moshi wipe AND the rig-owned plugin/continuum init are neutralized (commented);
    # the user's own line stays LIVE.
    live = [ln for ln in new_text.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("status-right ''" in ln for ln in live), "Moshi wipe must be neutralized"
    assert not any("@continuum-restore" in ln for ln in live), \
        "rig-owned @continuum option must be neutralized (the double-init fix)"
    assert not any("continuum.tmux" in ln for ln in live), \
        "the rig-owned continuum init run-shell must be neutralized (the double-init bug)"
    assert "set -g mouse on  # user line" in new_text  # user line preserved live


_FULL_LEGACY_CONF = (
    "# --- personal prefs ---\n"
    "set -g mouse on\n"
    "set -g history-limit 100000\n"
    "set -g base-index 1\n"
    "set -g @plugin 'tmux-plugins/tmux-sensible'\n"   # third-party — must survive
    "set-option -ga update-environment ' MOSHI_CLIENT'\n"
    "# --- the old hand-written tpm/resurrect/continuum init (rig-owned) ---\n"
    "set -g @plugin 'tmux-plugins/tpm'\n"
    "set -g @plugin 'tmux-plugins/tmux-resurrect'\n"
    "set -g @plugin 'tmux-plugins/tmux-continuum'\n"
    "set -g @resurrect-processes 'ssh psql ~rails'\n"
    "set -g @resurrect-capture-pane-contents 'on'\n"
    "set -g @continuum-restore 'on'\n"
    "set -g @continuum-boot 'on'\n"
    "set -g @continuum-boot-options 'iterm'\n"
    "set -g @continuum-save-interval '5'\n"
    "run-shell ~/.tmux/plugins/tmux-resurrect/resurrect.tmux\n"
    "run-shell ~/.tmux/plugins/tmux-continuum/continuum.tmux\n"
    "if-shell '[ -n \"$MOSHI_CLIENT\" ]' { set -g status-right '' }\n"
    "run '~/.tmux/plugins/tpm/tpm'\n"
)


# the rig-owned legacy lines that MUST end up neutralized (commented) after migration.
_RIG_OWNED_NEEDLES = (
    "@plugin 'tmux-plugins/tpm'",
    "@plugin 'tmux-plugins/tmux-resurrect'",
    "@plugin 'tmux-plugins/tmux-continuum'",
    "@resurrect-processes", "@resurrect-capture-pane-contents",
    "@continuum-restore", "@continuum-boot", "@continuum-boot-options", "@continuum-save-interval",
    "tmux-resurrect/resurrect.tmux", "tmux-continuum/continuum.tmux",
    "tmux/plugins/tpm/tpm", "status-right ''",
)


# personal prefs + the third-party plugin that MUST stay live.
_PRESERVED_NEEDLES = (
    "set -g mouse on", "set -g history-limit 100000", "set -g base-index 1",
    "@plugin 'tmux-plugins/tmux-sensible'", "update-environment ' MOSHI_CLIENT'",
)


def _user_region_lines(new_text):
    """Live (non-comment) lines OUTSIDE rig's managed block. In block mode rig's OWN generated
    block legitimately carries live `@plugin`/`run-shell` lines (that IS the managed config) —
    the migration contract is only about the USER's hand-written region, so the assertions look
    there. In import mode there is no block, so this is every live line."""
    out, in_block = [], False
    for ln in new_text.splitlines():
        s = ln.strip()
        if s == tmux.BLOCK_BEGIN:
            in_block = True
            continue
        if s == tmux.BLOCK_END:
            in_block = False
            continue
        if not in_block and not s.startswith("#"):
            out.append(ln)
    return out


def _assert_full_migration(new_text):
    live_joined = "\n".join(_user_region_lines(new_text))
    # (a) every rig-owned init/option/decl line in the USER's region is neutralized (none live).
    for needle in _RIG_OWNED_NEEDLES:
        assert needle not in live_joined, f"rig-owned user line still LIVE (double-init): {needle}"
    # (b) third-party plugin + personal prefs survive uncommented.
    for needle in _PRESERVED_NEEDLES:
        assert needle in live_joined, f"preserved line wrongly neutralized: {needle}"


@pytest.mark.parametrize("apply_mode", ["import", "block"])
def test_apply_full_legacy_init_neutralized_both_modes(tmp_path, monkeypatch, apply_mode):
    """The live-machine bug fix end-to-end (2026-06-18): a hand-written conf with the FULL old
    tpm/resurrect/continuum init + personal prefs + a third-party plugin. After migration in
    BOTH apply modes: (a) every rig-owned init line is commented, (b) tmux-sensible + prefs stay
    live, (c) the source-file import / managed block is present exactly once, (d) re-applying is
    a no-op (no double-comment), and the original is backed up first.
    """
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    conf = home / ".tmux.conf"
    conf.write_text(_FULL_LEGACY_CONF, encoding="utf-8")

    res = runner._do_provision_tmux(_tmux_action(home, apply_mode=apply_mode), "backup")
    assert res.status in ("created", "updated", "backed_up")

    # the original (with the live legacy init) is backed up before the rewrite.
    baks = list(home.glob(".tmux.conf.rig-bak-*"))
    assert len(baks) == 1, f"expected one timestamped backup, got {baks}"
    assert "run-shell ~/.tmux/plugins/tmux-continuum/continuum.tmux" in baks[0].read_text()

    migrated = conf.read_text()
    _assert_full_migration(migrated)
    # (c) the managed region is present exactly once.
    if apply_mode == "import":
        n = sum(1 for ln in migrated.splitlines() if ln.strip().startswith("source-file '"))
        assert n == 1, f"expected exactly one source-file import, got {n}"
    else:
        assert migrated.count(tmux.BLOCK_BEGIN) == 1 and migrated.count(tmux.BLOCK_END) == 1

    # (d) re-applying the already-migrated conf is a no-op: no double-comment, no extra backup,
    # byte-identical result.
    runner._do_provision_tmux(_tmux_action(home, apply_mode=apply_mode), "backup")
    reapplied = conf.read_text()
    assert reapplied == migrated, "re-apply must be byte-identical (idempotent migration)"
    assert reapplied.count(tmux.NEUTRALIZE_PREFIX + tmux.NEUTRALIZE_PREFIX) == 0, "no double-comment"
    assert len(list(home.glob(".tmux.conf.rig-bak-*"))) == 1, "clean re-apply must not re-backup"


def test_apply_reusering_a_readded_rig_option_settles_to_idempotent(tmp_path, monkeypatch):
    """The re-add path is HONEST + bounded (review finding 1): if the user re-adds a live rig-owned
    option (rig owns the surface — a bare re-add inside ~/.tmux.conf is re-neutralized by design),
    the next apply backs the conf up ONCE and neutralizes it, then SETTLES — it does NOT re-backup
    or re-comment on every subsequent apply (that would break the idempotency contract)."""
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    conf = home / ".tmux.conf"
    conf.write_text("set -g mouse on\nset -g @continuum-boot-options 'iterm'\n", encoding="utf-8")

    runner._do_provision_tmux(_tmux_action(home), "backup")  # migrate
    migrated = conf.read_text()
    # clean re-applies never re-backup (already-migrated is stable).
    runner._do_provision_tmux(_tmux_action(home), "backup")
    runner._do_provision_tmux(_tmux_action(home), "backup")
    assert conf.read_text() == migrated
    assert len(list(home.glob(".tmux.conf.rig-bak-*"))) == 1, "clean re-apply must not re-backup"

    # the user RE-ADDS the option live → exactly ONE more backup, then settle.
    conf.write_text(conf.read_text() + "set -g @continuum-boot-options 'iterm'\n", encoding="utf-8")
    runner._do_provision_tmux(_tmux_action(home), "backup")
    settled = conf.read_text()
    assert len(list(home.glob(".tmux.conf.rig-bak-*"))) == 2, "a live re-add backs up once"
    runner._do_provision_tmux(_tmux_action(home), "backup")
    assert conf.read_text() == settled, "after the re-add is neutralized, apply settles (idempotent)"
    assert len(list(home.glob(".tmux.conf.rig-bak-*"))) == 2, "settled re-apply must not re-backup"


def test_apply_block_mode_preserves_user_line_after_block_end(tmp_path, monkeypatch):
    """Block mode (review finding 3): a personal line the user placed AFTER the managed BLOCK_END
    survives migration uncommented (it's outside rig's region), and a rig-owned line the user put
    after BLOCK_END is still neutralized (rig owns that surface everywhere in ~/.tmux.conf)."""
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    conf = home / ".tmux.conf"
    # first migrate a legacy conf in block mode so a managed block exists.
    conf.write_text(
        "set -g @continuum-restore 'on'\n"
        "run-shell ~/.tmux/plugins/tmux-continuum/continuum.tmux\n",
        encoding="utf-8",
    )
    runner._do_provision_tmux(_tmux_action(home, apply_mode="block"), "backup")
    # the user appends, AFTER the managed block, a personal pref + a stray rig-owned option.
    migrated = conf.read_text()
    conf.write_text(migrated + "set -g mouse on\nset -g @continuum-save-interval '99'\n", encoding="utf-8")

    runner._do_provision_tmux(_tmux_action(home, apply_mode="block"), "backup")
    final = conf.read_text()
    all_lines = final.splitlines()
    end_idx = next(i for i, ln in enumerate(all_lines) if ln.strip() == tmux.BLOCK_END)
    after_end = all_lines[end_idx + 1:]  # the lines the user appended AFTER the managed block
    live_after = [ln for ln in after_end if not ln.lstrip().startswith("#")]
    # the personal pref the user put after the block stays live, IN that after-block region …
    assert any(ln.strip() == "set -g mouse on" for ln in live_after), \
        "user pref after BLOCK_END must survive live"
    # … and the rig-owned line the user put after the block is neutralized (rig owns that surface
    # everywhere in ~/.tmux.conf, not only above the block).
    assert not any(
        "@continuum-save-interval '99'" in ln for ln in all_lines if not ln.lstrip().startswith("#")
    ), "a rig-owned line is neutralized wherever it sits in ~/.tmux.conf"
    assert any("@continuum-save-interval '99'" in ln for ln in after_end), \
        "the neutralized rig-owned line stays (as a comment) in the after-block region"
    assert final.count(tmux.BLOCK_BEGIN) == 1 and final.count(tmux.BLOCK_END) == 1


def test_apply_creates_a_fresh_timestamped_backup_each_migration(tmp_path, monkeypatch):
    """A second apply, after the user hand-edited ~/.tmux.conf back into a migrating state, must
    snapshot the NEW content under its OWN timestamped backup — not skip because an earlier
    backup exists (which would lose the in-between edits). Earlier backups stay intact. (CTO)
    """
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    conf = home / ".tmux.conf"
    # a precious backup from an earlier apply must be preserved untouched.
    precious = home / ".tmux.conf.rig-bak-20000101T000000_000000Z"
    precious.write_text("A-PRECIOUS-EARLIER-BACKUP\n", encoding="utf-8")

    moshi_wipe = "if-shell '[ -n \"$MOSHI_CLIENT\" ]' { set -g status-right '' }\n"
    # first migrating apply
    conf.write_text(moshi_wipe + "FIRST-EDIT\n", encoding="utf-8")
    runner._do_provision_tmux(_tmux_action(home), "backup")
    # user hand-edits the conf back into a migrating state (re-adds the wipe + new content)
    conf.write_text(moshi_wipe + "SECOND-EDIT\n", encoding="utf-8")
    runner._do_provision_tmux(_tmux_action(home), "backup")

    baks = sorted(home.glob(".tmux.conf.rig-bak-*"))
    contents = "".join(b.read_text() for b in baks)
    # the precious earlier backup is intact, AND each migrating apply added its own snapshot —
    # nothing lost.
    assert precious.read_text() == "A-PRECIOUS-EARLIER-BACKUP\n"
    assert "A-PRECIOUS-EARLIER-BACKUP" in contents
    assert "FIRST-EDIT" in contents and "SECOND-EDIT" in contents
    assert len(baks) >= 3


def test_apply_honors_on_conflict_skip_for_user_conf(tmp_path, monkeypatch):
    """on_conflict=skip + an existing differing ~/.tmux.conf → leave it UNWIRED (codex P2).

    The generated artifacts honor skip via fsutil; the user's own file must too — a direct
    rewrite that ignores skip violates rig's conflict contract.
    """
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    conf = home / ".tmux.conf"
    conf.write_text("set -g mouse on\n", encoding="utf-8")
    res = runner._do_provision_tmux(_tmux_action(home), "skip")
    # the user's conf is left exactly as-is (no import line spliced in).
    assert conf.read_text() == "set -g mouse on\n"
    assert "source-file" not in conf.read_text()
    assert "on_conflict=skip" in res.detail


def test_apply_skip_does_not_chmod_a_conflicting_script(tmp_path, monkeypatch):
    """on_conflict=skip + a pre-existing DIFFERING managed script (not +x) → leave it untouched,
    including its mode (don't mutate a user file under skip) (codex P2)."""
    import os

    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    gdir = home / ".config" / "rig" / "tmux"
    gdir.mkdir(parents=True)
    foreign = gdir / "cc-save.sh"
    foreign.write_text("#!/usr/bin/env bash\n# the user's own thing\n", encoding="utf-8")
    foreign.chmod(0o644)  # not executable
    runner._do_provision_tmux(_tmux_action(home), "skip")
    # the differing file is left as-is — NOT chmod'd to 0755 (skip honored on mode too).
    assert not os.access(foreign, os.X_OK)
    assert foreign.read_text() == "#!/usr/bin/env bash\n# the user's own thing\n"


def test_apply_heals_exec_bit_on_identical_script(tmp_path, monkeypatch):
    """identical-skip IS ours — a stripped +x on rig's own (identical) script is healed AND the
    repair is REPORTED (not hidden behind 'already current') (codex P3)."""
    import os

    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    runner._do_provision_tmux(_tmux_action(home), "backup")  # install
    cc = home / ".config" / "rig" / "tmux" / "cc-save.sh"
    cc.chmod(0o644)  # strip +x on rig's own (now-identical) script
    res = runner._do_provision_tmux(_tmux_action(home), "backup")  # re-apply heals it
    assert os.access(cc, os.X_OK)
    assert res.status != "skipped" and "restored +x" in res.detail  # the repair is surfaced


def test_apply_surfaces_conflict_skipped_script_not_wired(tmp_path, monkeypatch):
    """on_conflict=skip + a DIFFERING pre-existing managed script → surface that the resurrect
    hook points at an UNMANAGED file (not silently 'already current') (codex P2)."""
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    gdir = home / ".config" / "rig" / "tmux"
    gdir.mkdir(parents=True)
    (gdir / "cc-save.sh").write_text("#!/usr/bin/env bash\n# foreign\n", encoding="utf-8")
    res = runner._do_provision_tmux(_tmux_action(home), "skip")
    assert "NOT applied" in res.detail and "cc-save.sh" in res.detail


def test_apply_surfaces_conflict_skipped_generated_conf(tmp_path, monkeypatch):
    """on_conflict=skip + a DIFFERING (stale) rig.tmux.conf → surface that tmux still sources the
    STALE config (e.g. an upgrade still carrying @continuum-boot 'on'), not silently 'current'
    (codex P2)."""
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    gdir = home / ".config" / "rig" / "tmux"
    gdir.mkdir(parents=True)
    # pre-seed a STALE generated config that differs from the new render.
    (gdir / "rig.tmux.conf").write_text("# stale\nset -g @continuum-boot 'on'\n", encoding="utf-8")
    res = runner._do_provision_tmux(_tmux_action(home), "skip")
    assert "rig.tmux.conf differs" in res.detail and "STALE" in res.detail


def test_apply_conflict_skip_only_reports_skipped_not_created(tmp_path, monkeypatch):
    """A conflict-skipped script while everything else is already current must report `skipped`
    (NOT `created`) — nothing was written, so a re-apply stays idempotent and doesn't inflate
    ApplyReport.changed every run (codex P2)."""
    from riglib.actions import runner
    from riglib.actions.runner import run_plan
    from riglib.plan import InstallPlan

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    a = _tmux_action(home)
    runner._do_provision_tmux(a, "backup")  # everything current
    # now make ONE script differ (a user-owned foreign file at rig's path).
    (home / ".config" / "rig" / "tmux" / "cc-save.sh").write_text("# foreign\n", encoding="utf-8")
    res = runner._do_provision_tmux(a, "skip")
    assert res.status == "skipped"  # unresolved drift, reported — not a 'created' change
    assert "NOT applied" in res.detail
    # and ApplyReport.changed is NOT incremented for this no-write apply.
    report = run_plan(InstallPlan(actions=[a], on_conflict="skip"))
    assert report.changed == 0


def test_apply_creates_conf_when_absent_even_under_skip(tmp_path, monkeypatch):
    """skip only protects an EXISTING file — an absent ~/.tmux.conf is still created."""
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    runner._do_provision_tmux(_tmux_action(home), "skip")
    assert (home / ".tmux.conf").is_file()
    assert "source-file" in (home / ".tmux.conf").read_text()


def test_apply_block_mode_splices_only_managed_region(tmp_path, monkeypatch):
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    conf = home / ".tmux.conf"
    conf.write_text("set -g mouse on  # mine\n", encoding="utf-8")
    runner._do_provision_tmux(_tmux_action(home, apply_mode="block"), "backup")
    text = conf.read_text()
    assert tmux.BLOCK_BEGIN in text and tmux.BLOCK_END in text
    assert "set -g mouse on  # mine" in text  # user line preserved
    assert "@resurrect-processes" in text  # managed body present inline (no import)
    assert "source-file" not in text  # block mode does NOT add an import


def test_apply_preserves_user_lines_in_plain_conf(tmp_path, monkeypatch):
    """import mode into a conf with NO rig settings: keep every user line, add only the import."""
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    conf = home / ".tmux.conf"
    conf.write_text("set -g mouse on\nbind r source-file ~/.tmux.conf\n", encoding="utf-8")
    runner._do_provision_tmux(_tmux_action(home), "backup")
    text = conf.read_text()
    assert "set -g mouse on" in text
    assert "bind r source-file ~/.tmux.conf" in text
    assert (home / ".config" / "rig" / "tmux" / "rig.tmux.conf").is_file()


def test_apply_writes_boot_plist_on_darwin_only(tmp_path, monkeypatch):
    import sys as _sys

    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    # darwin → plist written
    monkeypatch.setattr(_sys, "platform", "darwin")
    runner._do_provision_tmux(_tmux_action(home, boot={"enabled": True}), "backup")
    plist = home / "Library" / "LaunchAgents" / "ai.hyperide.tmux-boot.plist"
    assert plist.is_file()
    import plistlib
    assert plistlib.loads(plist.read_bytes())["Label"] == "ai.hyperide.tmux-boot"


def test_apply_skips_boot_plist_off_darwin(tmp_path, monkeypatch):
    import sys as _sys

    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "linux")
    runner._do_provision_tmux(_tmux_action(home, boot={"enabled": True}), "backup")
    assert not (home / "Library" / "LaunchAgents" / "ai.hyperide.tmux-boot.plist").exists()


def test_apply_writes_autosave_plist_and_script_on_darwin(tmp_path, monkeypatch):
    """The independent autosave agent (#138): apply writes the launchd plist AND the wrapper
    script (executable), and continuum's own save is disabled in the generated conf."""
    import os as _os
    import sys as _sys

    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "darwin")
    runner._do_provision_tmux(_tmux_action(home, autosave={"enabled": True}), "backup")
    plist = home / "Library" / "LaunchAgents" / "ai.hyperide.tmux-autosave.plist"
    script = home / ".config" / "rig" / "tmux" / "tmux-autosave.sh"
    assert plist.is_file() and script.is_file()
    assert _os.access(script, _os.X_OK)  # the launchd agent runs it by path
    conf = (home / ".config" / "rig" / "tmux" / "rig.tmux.conf").read_text()
    assert "set -g @continuum-save-interval '0'" in conf  # one authoritative saver


def test_apply_omits_autosave_when_disabled(tmp_path, monkeypatch):
    import sys as _sys

    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "darwin")
    runner._do_provision_tmux(_tmux_action(home, autosave={"enabled": False}), "backup")
    assert not (home / "Library" / "LaunchAgents" / "ai.hyperide.tmux-autosave.plist").exists()
    assert not (home / ".config" / "rig" / "tmux" / "tmux-autosave.sh").exists()


def test_drift_tmux_autosave_plist(tmp_path, monkeypatch):
    import sys as _sys

    from riglib.actions import runner
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "darwin")
    a = _tmux_action(home, autosave={"enabled": True})
    runner._do_provision_tmux(a, "backup")
    _stage_live_tmux_state(home)
    assert not [d for d in detect(InstallPlan(actions=[a])).items if d.category == "tmux"]
    (home / "Library" / "LaunchAgents" / "ai.hyperide.tmux-autosave.plist").unlink()
    drift = [d for d in detect(InstallPlan(actions=[a])).items if d.category == "tmux"]
    assert any("autosave launchd plist" in d.detail and d.direction == "missing" for d in drift)


def test_apply_resolves_unexpanded_tilde_conf_path(tmp_path, monkeypatch):
    """The plan→action→runner path with an UNEXPANDED ~/.tmux.conf must resolve against HOME."""
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    a = _tmux_action(home, conf_path="~/.tmux.conf",
                     generated_dir="~/.config/rig/tmux")
    runner._do_provision_tmux(a, "backup")
    assert (home / ".tmux.conf").is_file()
    assert f"source-file '{home / '.config' / 'rig' / 'tmux' / 'rig.tmux.conf'}'" in (home / ".tmux.conf").read_text()


def test_apply_removes_stale_import_pointing_at_old_dir(tmp_path, monkeypatch):
    """A pre-existing rig import from an OLD generated_dir is dropped (one current import only)."""
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    conf = home / ".tmux.conf"
    conf.write_text(
        "set -g mouse on\n"
        "source-file /old/place/rig.tmux.conf\n"        # stale rig import (different dir)
        "bind r run-shell 'echo rig.tmux.conf'\n",       # a user binding that merely names it
        encoding="utf-8",
    )
    runner._do_provision_tmux(_tmux_action(home), "backup")
    text = conf.read_text()
    assert "source-file /old/place/rig.tmux.conf" not in text  # stale import removed
    assert text.count("rig.tmux.conf") >= 1
    assert "bind r run-shell 'echo rig.tmux.conf'" in text  # the user binding is preserved
    # exactly one current import line.
    cur = home / ".config" / "rig" / "tmux" / "rig.tmux.conf"
    assert text.count(f"source-file '{cur}'") == 1


def test_apply_does_not_drop_user_line_mentioning_rig_conf(tmp_path, monkeypatch):
    """A user comment/binding that merely MENTIONS rig.tmux.conf is NOT treated as the import."""
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    conf = home / ".tmux.conf"
    conf.write_text("# I source rig.tmux.conf below\nset -g mouse on\n", encoding="utf-8")
    runner._do_provision_tmux(_tmux_action(home), "backup")
    assert "# I source rig.tmux.conf below" in conf.read_text()  # comment preserved


# ── full apply path through run_plan ─────────────────────────────────────────────────────
def test_run_plan_provisions_tmux(tmp_path, monkeypatch):
    from riglib.actions.runner import run_plan
    from riglib.plan import InstallPlan

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    report = run_plan(InstallPlan(actions=[_tmux_action(home)]))
    assert not report.errors
    assert report.changed == 1
