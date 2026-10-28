"""tmux configuration provisioning — config, pure rendering, plan, install, drift (part 5 of 5).

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

from riglib import tmux
from riglib.actions import live_env as live_env_mod
from riglib.actions import service_ctl as service_ctl_mod
from riglib.actions import shared as shared_mod
from riglib.actions import tmux as tmux_mod
from tests.tmux_helpers import _stage_live_tmux_state, _tmux_action


# ── drift ────────────────────────────────────────────────────────────────────────────────
def test_drift_tmux_missing(tmp_path, monkeypatch):
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    report = detect(InstallPlan(actions=[_tmux_action(home)]))
    drift = [d for d in report.items if d.category == "tmux"]
    assert drift and drift[0].direction == "missing"


def test_drift_tmux_in_sync_after_apply(tmp_path, monkeypatch):
    from riglib.actions import runner
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    a = _tmux_action(home)
    runner._do_provision_tmux(a, "backup")
    _stage_live_tmux_state(home)  # a real apply would have created plugins + resurrect dir
    report = detect(InstallPlan(actions=[a]))
    assert not [d for d in report.items if d.category == "tmux"]


def test_drift_tmux_live_state_missing(tmp_path, monkeypatch, live_launchd_home):
    """DEFECTS 4/6 drift: a missing resurrect dir / plugin checkout is surfaced by `rig status`
    so a clean machine doesn't read as in-sync while apply still has live work to do (codex
    finding). Here the live state is NOT staged → drift reports both."""
    from riglib.actions import runner
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    a = _tmux_action(home)
    runner._do_provision_tmux(a, "backup")  # writes the config artifacts (dry-run: no live activation)
    # status checks the LIVE state only on a real machine (under RIG_TMUX_DRY_RUN apply skips the
    # live activation, so status correctly suppresses the matching live drift to stay consistent).
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    drift = [d for d in detect(InstallPlan(actions=[a])).items if d.category == "tmux"]
    assert any("resurrect" in d.detail and d.direction == "missing" for d in drift)
    assert any("plugin" in d.detail and "tpm" in str(d.target) for d in drift)


def test_drift_tmux_live_state_suppressed_under_dry_run(tmp_path, monkeypatch):
    """Under RIG_TMUX_DRY_RUN apply skips the LIVE activation (no plugin clone, no resurrect
    dir), so status MUST suppress the matching live-state drift — else status would report drift
    apply deliberately won't converge (apply/status disagree, and status could never read in-sync
    under the flag, e.g. CI/smoke). The file-artifact drift stays checked; only the live half is
    gated. (The autouse _isolate_tmux_activation fixture already sets the flag for this test.)"""
    from riglib.actions import runner
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    a = _tmux_action(home)
    runner._do_provision_tmux(a, "backup")  # writes config artifacts; dry-run skips live activation
    # the resurrect dir + plugin checkouts are absent (dry-run never created them), but with the
    # flag set status must NOT flag them — only the live half is gated, file artifacts stay in-sync.
    drift = [d for d in detect(InstallPlan(actions=[a])).items if d.category == "tmux"]
    assert not any("resurrect" in d.detail or "plugin" in d.detail for d in drift), drift


def test_drift_tmux_modified_generated_file(tmp_path, monkeypatch):
    from riglib.actions import runner
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    a = _tmux_action(home)
    runner._do_provision_tmux(a, "backup")
    # tamper with the generated file → drift on the managed region.
    rig_conf = home / ".config" / "rig" / "tmux" / "rig.tmux.conf"
    rig_conf.write_text("HAND EDITED\n", encoding="utf-8")
    report = detect(InstallPlan(actions=[a]))
    drift = [d for d in report.items if d.category == "tmux"]
    assert drift and drift[0].direction == "modified"


def test_drift_tmux_missing_import_line(tmp_path, monkeypatch):
    """The generated file is correct but ~/.tmux.conf lost its import line → drift."""
    from riglib.actions import runner
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    a = _tmux_action(home)
    runner._do_provision_tmux(a, "backup")
    (home / ".tmux.conf").write_text("set -g mouse on\n", encoding="utf-8")  # import dropped
    report = detect(InstallPlan(actions=[a]))
    drift = [d for d in report.items if d.category == "tmux"]
    assert drift and drift[0].direction == "missing"


def test_drift_tmux_flags_stale_import_alongside_current(tmp_path, monkeypatch):
    """The current import is present BUT an old `source-file …/rig.tmux.conf` (from a moved
    generated_dir) is ALSO live → modified drift (apply removes the stale one) (codex P2)."""
    from riglib.actions import runner
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    a = _tmux_action(home)
    runner._do_provision_tmux(a, "backup")
    conf = home / ".tmux.conf"
    # append a STALE rig import (old dir) on top of the correct current one.
    conf.write_text(conf.read_text() + "source-file /old/place/rig.tmux.conf\n", encoding="utf-8")
    drift = [d for d in detect(InstallPlan(actions=[a])).items if d.category == "tmux"]
    assert any(d.direction == "modified" and "STALE" in d.detail for d in drift)


def test_drift_tmux_flags_line_after_the_import(tmp_path, monkeypatch):
    """A user line AFTER rig's import undoes the ordering guarantee (it runs after rig.tmux.conf)
    → modified drift; apply re-appends the import last (codex P2)."""
    from riglib.actions import runner
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    a = _tmux_action(home)
    runner._do_provision_tmux(a, "backup")
    conf = home / ".tmux.conf"
    # a trailing executable line AFTER the rig import (the classic re-breaks-the-bug case).
    conf.write_text(conf.read_text() + "set -g status-right ''\n", encoding="utf-8")
    drift = [d for d in detect(InstallPlan(actions=[a])).items if d.category == "tmux"]
    assert any(d.direction == "modified" and "LAST line" in d.detail for d in drift)


def test_drift_tmux_block_mode_missing_and_modified(tmp_path, monkeypatch):
    from riglib.actions import runner
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    a = _tmux_action(home, apply_mode="block")
    runner._do_provision_tmux(a, "backup")
    _stage_live_tmux_state(home)  # a real apply would have created plugins + resurrect dir
    # in sync right after apply
    assert not [d for d in detect(InstallPlan(actions=[a])).items if d.category == "tmux"]
    # tamper INSIDE the managed block → 'modified' (content compared, not just sentinels)
    conf = home / ".tmux.conf"
    text = conf.read_text().replace("@resurrect-processes", "@resurrect-TAMPERED", 1)
    conf.write_text(text, encoding="utf-8")
    d1 = [d for d in detect(InstallPlan(actions=[a])).items if d.category == "tmux" and d.target == conf]
    assert d1 and d1[0].direction == "modified"
    # remove the block entirely → 'missing'
    conf.write_text("set -g mouse on\n", encoding="utf-8")
    d2 = [d for d in detect(InstallPlan(actions=[a])).items if d.category == "tmux" and d.target == conf]
    assert d2 and d2[0].direction == "missing"


def test_drift_tmux_boot_plist(tmp_path, monkeypatch):
    import sys as _sys

    from riglib.actions import runner
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "darwin")
    a = _tmux_action(home, boot={"enabled": True})
    runner._do_provision_tmux(a, "backup")
    _stage_live_tmux_state(home)  # a real apply would have created plugins + resurrect dir
    assert not [d for d in detect(InstallPlan(actions=[a])).items if d.category == "tmux"]
    # delete the boot plist → drift surfaces it (it was previously unchecked).
    (home / "Library" / "LaunchAgents" / "ai.hyperide.tmux-boot.plist").unlink()
    drift = [d for d in detect(InstallPlan(actions=[a])).items if d.category == "tmux"]
    assert any("boot launchd plist" in d.detail and d.direction == "missing" for d in drift)


def test_drift_tmux_flags_stripped_exec_bit_on_scripts(tmp_path, monkeypatch):
    """A cc script that lost its +x is drift — the resurrect hook invokes it by path (codex P2)."""
    from riglib.actions import runner
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    a = _tmux_action(home)
    runner._do_provision_tmux(a, "backup")
    cc = home / ".config" / "rig" / "tmux" / "cc-save.sh"
    cc.chmod(0o644)  # strip the exec bit (content unchanged)
    drift = [d for d in detect(InstallPlan(actions=[a])).items if d.category == "tmux"]
    assert any("not executable" in d.detail and d.target == cc for d in drift)


def test_drift_tmux_flags_stale_boot_plist_when_disabled(tmp_path, monkeypatch):
    """boot disabled + a leftover plist on disk → reported as a disk→config extra (codex P2)."""
    import sys as _sys

    from riglib.actions import runner
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "darwin")
    # apply WITH boot on → plist written
    runner._do_provision_tmux(_tmux_action(home, boot={"enabled": True}), "backup")
    plist = home / "Library" / "LaunchAgents" / "ai.hyperide.tmux-boot.plist"
    assert plist.is_file()
    # now the config DISABLES boot — apply doesn't delete it, so drift must flag the leftover.
    a_off = _tmux_action(home, boot={"enabled": False})
    drift = [d for d in detect(InstallPlan(actions=[a_off])).items if d.category == "tmux"]
    assert any(d.direction == "extra" and "boot" in d.detail.lower() and d.target == plist for d in drift)


def test_drift_tmux_flags_stale_attach_script_when_disabled(tmp_path, monkeypatch):
    """anti_sprawl disabled + a leftover tmux-attach.sh on disk → reported as extra (codex P2)."""
    from riglib.actions import runner
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    # apply WITH anti-sprawl on → tmux-attach.sh written
    runner._do_provision_tmux(_tmux_action(home, anti_sprawl={"enabled": True}), "backup")
    attach = home / ".config" / "rig" / "tmux" / "tmux-attach.sh"
    assert attach.is_file()
    # now DISABLE anti-sprawl — apply wouldn't delete it, so drift must flag the leftover.
    a_off = _tmux_action(home, anti_sprawl={"enabled": False})
    drift = [d for d in detect(InstallPlan(actions=[a_off])).items if d.category == "tmux"]
    assert any(d.direction == "extra" and d.target == attach for d in drift)


def test_apply_surfaces_generated_file_backups(tmp_path, monkeypatch):
    """on_conflict=backup that moves a differing generated file must surface the .rig-bak-* path
    in the result (the 'backup-noted' contract), not silently drop it (codex P3)."""
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    gdir = home / ".config" / "rig" / "tmux"
    gdir.mkdir(parents=True)
    # pre-seed a DIFFERING rig.tmux.conf so apply backs it up under on_conflict=backup.
    (gdir / "rig.tmux.conf").write_text("OLD HAND-EDITED CONFIG\n", encoding="utf-8")
    res = runner._do_provision_tmux(_tmux_action(home), "backup")
    assert res.status == "backed_up"
    assert res.backup is not None and ".rig-bak" in res.backup.name
    assert "backups:" in res.detail


# ── DEFECTS 1/4/5/6: live activation (plugins, resurrect dir, first save, launchctl, cleanup) ──
def _activation_seams(monkeypatch, *, plugins_present=False, launchctl_loaded=False, autosave_loaded=True):
    """Stub the live seams an activation touches and return a record of the calls made.

    Records git clones, launchctl verbs, tmux `resurrect save` runs, and continuum boot-cleanup
    runs — so a test can assert WHICH side effects an activation performed without any real
    network / daemon / tmux-server access. ``autosave_loaded`` defaults True so the independent
    autosave agent (#138) is a steady-state no-op unless a test opts to see its first bootstrap.
    """

    rec = {"clones": [], "launchctl": [], "load_w": [], "saves": 0, "cleanups": 0,
           "autosave_bootstrap": [], "autosave_bootout": []}

    def _clone(repo, dest):
        rec["clones"].append((repo, str(dest)))
        Path(dest).mkdir(parents=True, exist_ok=True)
        return 0

    def _load_w(plist):
        rec["load_w"].append(str(plist))
        return 0

    def _cleanup(plan):
        rec["cleanups"] += 1
        return True

    # rig-cli#116: these tests fake HOME on purpose and assert on the LIVE (stubbed) launchctl
    # path — opt out of the sandboxed-HOME auto-skip, which would otherwise route them dry.
    monkeypatch.setattr(live_env_mod, "_home_is_sandboxed", lambda: False)
    monkeypatch.setattr(shared_mod, "_git_clone", _clone)
    monkeypatch.setattr(service_ctl_mod, "_launchctl", lambda verb, arg: rec["launchctl"].append((verb, arg)) or 0)
    monkeypatch.setattr(service_ctl_mod, "_launchctl_load_enable", _load_w)
    monkeypatch.setattr(service_ctl_mod, "_launchctl_loaded", lambda label: launchctl_loaded)
    monkeypatch.setattr(tmux_mod, "_tmux_resurrect_save", lambda plan: rec.update(saves=rec["saves"] + 1) or 0)
    monkeypatch.setattr(tmux_mod, "_clean_stale_continuum_boot", _cleanup)
    # the independent autosave agent (#138) uses the gui-domain launchctl verbs.
    monkeypatch.setattr(service_ctl_mod, "_launchctl_gui_loaded", lambda label: autosave_loaded)
    monkeypatch.setattr(service_ctl_mod, "_launchctl_bootstrap", lambda p: rec["autosave_bootstrap"].append(str(p)) or 0)
    monkeypatch.setattr(service_ctl_mod, "_launchctl_bootout", lambda p: rec["autosave_bootout"].append(str(p)) or 0)
    return rec


def test_activation_creates_resurrect_dir(tmp_path, monkeypatch):
    """DEFECT 4: ~/.tmux/resurrect must EXIST after apply, else resurrect never writes a
    snapshot (tmux_resurrect_*.txt) → nothing to restore on reboot."""
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)  # run the real activation
    _activation_seams(monkeypatch)
    runner._do_provision_tmux(_tmux_action(home), "backup")
    assert (home / ".tmux" / "resurrect").is_dir()


def test_activation_clones_missing_plugins(tmp_path, monkeypatch):
    """DEFECT 6: on a CLEAN machine ~/.tmux/plugins is empty, so the @plugin declarations don't
    resolve. Activation must clone tpm + resurrect + continuum."""
    from riglib import tmux as tmod
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    rec = _activation_seams(monkeypatch)
    runner._do_provision_tmux(_tmux_action(home), "backup")
    cloned = {repo for repo, _dest in rec["clones"]}
    assert cloned == {url for url, _entry in tmod.PLUGINS.values()}


def test_activation_skips_already_present_plugins(tmp_path, monkeypatch):
    """Idempotent: a COMPLETE plugin checkout that already exists is NOT re-cloned."""
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    # pre-create a COMPLETE tpm checkout (its `tpm` entrypoint present) so it's "already installed".
    tpm = home / ".tmux" / "plugins" / "tpm"
    tpm.mkdir(parents=True)
    (tpm / "tpm").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    rec = _activation_seams(monkeypatch)
    runner._do_provision_tmux(_tmux_action(home), "backup")
    cloned_dests = {dest for _repo, dest in rec["clones"]}
    assert not any(d.endswith("/tpm") for d in cloned_dests)  # tpm not re-cloned
    assert any(d.endswith("/tmux-resurrect") for d in cloned_dests)  # the missing ones still cloned


def test_activation_recloned_partial_plugin_dir(tmp_path, monkeypatch):
    """A partial/broken plugin dir (from a failed clone) is NOT treated as installed — it is
    cleared and re-cloned, so the 'offline retries next apply' contract holds (codex finding)."""
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    # a tpm dir that EXISTS but is missing its `tpm` entrypoint → partial → must be re-cloned.
    partial = home / ".tmux" / "plugins" / "tpm"
    partial.mkdir(parents=True)
    (partial / "stray.txt").write_text("leftover from a failed clone\n", encoding="utf-8")
    rec = _activation_seams(monkeypatch)
    runner._do_provision_tmux(_tmux_action(home), "backup")
    assert any(d.endswith("/tpm") for _r, d in rec["clones"]), "partial tpm was not re-cloned"


def test_activation_launchctl_loads_the_boot_agent_with_w(tmp_path, monkeypatch):
    """DEFECT 1: `rig apply` must `launchctl load -w` the boot agent (it previously wrote the
    plist but NEVER loaded it → the agent didn't fire). `-w` enables it across reboots."""
    import sys as _sys

    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "darwin")
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    rec = _activation_seams(monkeypatch)
    runner._do_provision_tmux(_tmux_action(home, boot={"enabled": True}), "backup")
    # a `launchctl load -w <plist>` must have been issued for the boot plist (via the dedicated
    # _launchctl_load_enable helper, which builds `launchctl load -w <plist>` with -w as its own
    # token — see the helper's docstring for why it is separate from the 2-arg _launchctl).
    plist = str(home / "Library" / "LaunchAgents" / "ai.hyperide.tmux-boot.plist")
    assert rec["load_w"] == [plist], rec["load_w"]


def test_activation_bootstraps_autosave_agent(tmp_path, monkeypatch):
    """The independent autosave agent (#138) is a stateless periodic daemon → activation loads it
    NOW (gui-domain bootstrap) so autosave starts immediately, even for the current server —
    unlike the boot agent which only fires at the next login. Not-loaded → bootstrap."""
    import sys as _sys

    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "darwin")
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    rec = _activation_seams(monkeypatch, autosave_loaded=False)  # not yet loaded → first bootstrap
    runner._do_provision_tmux(_tmux_action(home, autosave={"enabled": True}), "backup")
    plist = str(home / "Library" / "LaunchAgents" / "ai.hyperide.tmux-autosave.plist")
    assert rec["autosave_bootstrap"] == [plist], rec["autosave_bootstrap"]


def _autosave_activation_home(tmp_path, monkeypatch):
    import sys as _sys

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "darwin")
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    return home


def test_activation_reloads_autosave_when_plist_changed_and_loaded(tmp_path, monkeypatch):
    """Loaded already + the plist was (re)written this apply → bootout THEN bootstrap (refresh)."""
    from riglib.actions import runner

    home = _autosave_activation_home(tmp_path, monkeypatch)
    rec = _activation_seams(monkeypatch, autosave_loaded=True)  # already loaded
    # first apply writes the plist (a change) → since loaded, it must reload (bootout+bootstrap).
    runner._do_provision_tmux(_tmux_action(home, autosave={"enabled": True}), "backup")
    plist = str(home / "Library" / "LaunchAgents" / "ai.hyperide.tmux-autosave.plist")
    assert rec["autosave_bootout"] == [plist]
    assert rec["autosave_bootstrap"] == [plist]


def test_activation_autosave_steady_state_is_noop(tmp_path, monkeypatch):
    """Loaded + the plist unchanged (a second apply) → neither bootout nor bootstrap is called."""
    from riglib.actions import runner

    home = _autosave_activation_home(tmp_path, monkeypatch)
    a = _tmux_action(home, autosave={"enabled": True})
    _activation_seams(monkeypatch, autosave_loaded=True)
    runner._do_provision_tmux(a, "backup")  # first apply writes + reloads
    rec = _activation_seams(monkeypatch, autosave_loaded=True)  # fresh recorder for the 2nd apply
    runner._do_provision_tmux(a, "backup")  # plist identical now → no launchctl churn
    assert rec["autosave_bootout"] == [] and rec["autosave_bootstrap"] == []


def test_activation_suppresses_autosave_load_when_script_conflict_skipped(tmp_path, monkeypatch):
    """A differing tmux-autosave.sh under on_conflict=skip (stale/unmanaged) suppresses the load —
    we never bootstrap an agent whose wrapper script rig didn't write."""
    from riglib.actions import runner

    home = _autosave_activation_home(tmp_path, monkeypatch)
    a = _tmux_action(home, autosave={"enabled": True})
    runner._do_provision_tmux(a, "backup")  # everything current
    # a user-owned foreign wrapper at rig's path → conflict-skip on the next apply.
    (home / ".config" / "rig" / "tmux" / "tmux-autosave.sh").write_text("# foreign\n", encoding="utf-8")
    rec = _activation_seams(monkeypatch, autosave_loaded=False)
    runner._do_provision_tmux(a, "skip")
    assert rec["autosave_bootstrap"] == []  # suppressed — stale wrapper never loaded


def test_activation_suppresses_autosave_load_when_plist_conflict_skipped(tmp_path, monkeypatch):
    """A differing autosave PLIST under on_conflict=skip suppresses the load (autosave_plist_conflicted
    → autosave_load_safe False), same as a conflict-skipped script."""
    from riglib.actions import runner

    home = _autosave_activation_home(tmp_path, monkeypatch)
    a = _tmux_action(home, autosave={"enabled": True})
    runner._do_provision_tmux(a, "backup")  # everything current
    # a differing plist at rig's path → conflict-skip on the next apply.
    (home / "Library" / "LaunchAgents" / "ai.hyperide.tmux-autosave.plist").write_text(
        "<plist>foreign</plist>", encoding="utf-8"
    )
    rec = _activation_seams(monkeypatch, autosave_loaded=False)
    runner._do_provision_tmux(a, "skip")
    assert rec["autosave_bootstrap"] == []  # suppressed — stale plist never loaded


def test_activation_suppresses_autosave_load_when_rig_conf_conflict_skipped(tmp_path, monkeypatch):
    """A conflict-skipped rig.tmux.conf may still carry the OLD nonzero @continuum-save-interval;
    bootstrapping the autosave agent while continuum is also saving = the two-writer race this
    feature removes. So a stale generated config must also suppress the autosave load (codex P2)."""
    from riglib.actions import runner

    home = _autosave_activation_home(tmp_path, monkeypatch)
    a = _tmux_action(home, autosave={"enabled": True})
    runner._do_provision_tmux(a, "backup")  # everything current
    # a differing generated rig.tmux.conf at rig's path → conflict-skip on the next apply.
    (home / ".config" / "rig" / "tmux" / "rig.tmux.conf").write_text(
        "# stale\nset -g @continuum-save-interval '15'\n", encoding="utf-8"
    )
    rec = _activation_seams(monkeypatch, autosave_loaded=False)
    runner._do_provision_tmux(a, "skip")
    assert rec["autosave_bootstrap"] == []  # suppressed — stale config could keep continuum saving


def test_activation_autosave_bootstrap_failure_is_a_warning(tmp_path, monkeypatch):
    """A non-zero `launchctl bootstrap` is surfaced as a warning, never a silent success."""
    from riglib.actions import runner

    home = _autosave_activation_home(tmp_path, monkeypatch)
    rec = _activation_seams(monkeypatch, autosave_loaded=False)
    monkeypatch.setattr(service_ctl_mod, "_launchctl_bootstrap", lambda p: rec["autosave_bootstrap"].append(str(p)) or 1)
    res = runner._do_provision_tmux(_tmux_action(home, autosave={"enabled": True}), "backup")
    assert "autosave agent NOT loaded" in res.detail


def test_apply_skips_autosave_plist_off_darwin(tmp_path, monkeypatch):
    import sys as _sys

    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "linux")
    runner._do_provision_tmux(_tmux_action(home, autosave={"enabled": True}), "backup")
    assert not (home / "Library" / "LaunchAgents" / "ai.hyperide.tmux-autosave.plist").exists()


def test_activation_takes_a_first_resurrect_save(tmp_path, monkeypatch):
    """DEFECT 6: after apply, take a first `resurrect save` so there is a snapshot to restore."""
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    rec = _activation_seams(monkeypatch)
    runner._do_provision_tmux(_tmux_action(home), "backup")
    assert rec["saves"] >= 1


def test_activation_cleans_stale_continuum_boot(tmp_path, monkeypatch):
    """DEFECT 5: continuum's own osx_iterm/terminal_start_tmux.sh register as macOS Login Items
    and compete with rig's launchd agent. Activation must clean them (osx_disable.sh / bootout)."""
    import sys as _sys

    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "darwin")
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    rec = _activation_seams(monkeypatch)
    runner._do_provision_tmux(_tmux_action(home, boot={"enabled": True}), "backup")
    assert rec["cleanups"] >= 1


def test_activation_suppresses_boot_load_when_plist_conflict_skipped(tmp_path, monkeypatch):
    """on_conflict=skip + a DIFFERING boot plist on disk: activation must NOT launchctl-load it
    (loading a stale/unmanaged boot path despite skip semantics — codex finding)."""
    import sys as _sys

    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "darwin")
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    rec = _activation_seams(monkeypatch)
    # pre-seed a DIFFERING boot plist so on_conflict=skip leaves it stale (conflict-skipped).
    la = home / "Library" / "LaunchAgents"
    la.mkdir(parents=True)
    (la / "ai.hyperide.tmux-boot.plist").write_text("<plist>STALE</plist>\n", encoding="utf-8")
    runner._do_provision_tmux(_tmux_action(home, boot={"enabled": True}), "skip")
    assert rec["load_w"] == []  # the stale boot agent was NOT loaded
    # AND the stale-continuum-boot cleanup must NOT run: rig has not loaded its replacement, so
    # stripping continuum's own autostart now would leave NO tmux autostart at all (review finding).
    assert rec["cleanups"] == 0


def test_activation_does_not_reload_already_loaded_unchanged_agent(tmp_path, monkeypatch):
    """A steady-state re-apply (agent already loaded, plist unchanged) must NOT unload/reload the
    boot agent — so it never restarts it / re-spawns `main` every apply, and a transient load
    failure can't disable a working agent (review Medium/Low). The launchctl load is NOT called."""
    import sys as _sys

    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "darwin")
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    rec = _activation_seams(monkeypatch, launchctl_loaded=True)
    # first apply writes the plist (changed) and would load; then everything is current + loaded.
    runner._do_provision_tmux(_tmux_action(home, boot={"enabled": True}), "backup")
    rec["load_w"].clear()
    rec["launchctl"].clear()
    # second apply: plist unchanged + agent loaded → NO load, NO unload.
    runner._do_provision_tmux(_tmux_action(home, boot={"enabled": True}), "backup")
    assert rec["load_w"] == []  # not (re)loaded
    assert not any(v == "unload" for v, _a in rec["launchctl"])  # not unloaded


def test_activation_reloads_when_plist_changed_and_agent_loaded(tmp_path, monkeypatch):
    """When the plist is REWRITTEN (e.g. an upgrade changes the boot script path) AND the agent is
    already loaded, activation unloads + load -w's it so launchd picks up the new definition."""
    import sys as _sys

    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "darwin")
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    rec = _activation_seams(monkeypatch, launchctl_loaded=True)
    # pre-seed a DIFFERING plist so this apply REWRITES it (boot_plist_changed) under backup.
    la = home / "Library" / "LaunchAgents"
    la.mkdir(parents=True)
    (la / "ai.hyperide.tmux-boot.plist").write_text("<plist>OLD</plist>\n", encoding="utf-8")
    runner._do_provision_tmux(_tmux_action(home, boot={"enabled": True}), "backup")
    plist = str(la / "ai.hyperide.tmux-boot.plist")
    assert rec["load_w"] == [plist]  # reloaded the changed plist
    assert ("unload", plist) in rec["launchctl"]  # unloaded first to refresh the definition


def test_failed_clone_warning_surfaced_on_changed_apply(tmp_path, monkeypatch):
    """A clone failure on a FIRST (config-writing → changed) apply must still surface the
    'plugin NOT installed' warning in the result detail, not be swallowed (review Low-4)."""
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    _activation_seams(monkeypatch)
    monkeypatch.setattr(shared_mod, "_git_clone", lambda repo, dest: 1)  # offline: every clone fails
    # a FIRST apply (no config yet) → config is written → status is created/changed.
    res = runner._do_provision_tmux(_tmux_action(home), "backup")
    assert res.status in ("created", "backed_up")
    assert "NOT installed" in res.detail  # the offline-clone warning reached the user


def test_activation_suppresses_boot_load_when_boot_script_conflict_skipped(tmp_path, monkeypatch):
    """on_conflict=skip + a DIFFERING tmux-boot.sh on disk: activation must NOT launchctl-load the
    agent (the plist runs THAT script, so loading would run a stale boot script — review P1)."""
    import sys as _sys

    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "darwin")
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    rec = _activation_seams(monkeypatch)
    # pre-seed a DIFFERING boot script so on_conflict=skip leaves it stale (conflict-skipped).
    gen = home / ".config" / "rig" / "tmux"
    gen.mkdir(parents=True)
    (gen / "tmux-boot.sh").write_text("#!/usr/bin/env bash\necho STALE BOOT\n", encoding="utf-8")
    runner._do_provision_tmux(_tmux_action(home, boot={"enabled": True}), "skip")
    assert rec["load_w"] == []  # the agent that runs the stale boot script was NOT loaded


def test_activation_does_not_clean_continuum_boot_when_rig_boot_disabled(tmp_path, monkeypatch):
    """The stale-boot cleanup is gated on rig owning boot: if the user opted OUT of rig boot
    (boot.enabled false) but relies on continuum's own autostart, activation must NOT nuke it
    (opus finding)."""
    import sys as _sys

    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "darwin")
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    rec = _activation_seams(monkeypatch)
    runner._do_provision_tmux(_tmux_action(home, boot={"enabled": False}), "backup")
    assert rec["cleanups"] == 0  # boot disabled → the user's own autostart is left alone


def test_activation_re_apply_is_a_noop(tmp_path, monkeypatch):
    """Idempotency (opus/codex finding): a second real activation with everything already present
    (plugins complete, boot agent loaded, a snapshot on disk) makes NO changes → the apply is a
    `skipped` no-op, never a spurious `created` on every run."""
    import sys as _sys

    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "darwin")
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    _activation_seams(monkeypatch, launchctl_loaded=True)  # agent already loaded → no re-load change
    # nothing stale to clean on a settled machine → cleanup is a no-op (not a change).
    monkeypatch.setattr(tmux_mod, "_clean_stale_continuum_boot", lambda plan: False)
    # pre-stage a fully-activated machine: complete plugin checkouts + a resurrect snapshot.
    _stage_live_tmux_state(home)
    (home / ".tmux" / "resurrect" / "tmux_resurrect_20260101T000000.txt").write_text(
        "snap\n", encoding="utf-8")
    # first apply writes the config artifacts; second must be a pure no-op.
    runner._do_provision_tmux(_tmux_action(home, boot={"enabled": True}), "backup")
    res2 = runner._do_provision_tmux(_tmux_action(home, boot={"enabled": True}), "backup")
    assert res2.status == "skipped", res2.detail


def test_activation_skips_first_save_when_snapshot_exists(tmp_path, monkeypatch):
    """The FIRST save fires ONLY when no snapshot exists — a re-apply must not re-save and risk
    clobbering a good snapshot with an empty/partial one (opus finding). Exercises the REAL
    _tmux_resurrect_save guard (not the seam stub)."""
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    rec = _activation_seams(monkeypatch)
    # a snapshot already exists on disk.
    resurrect = home / ".tmux" / "resurrect"
    resurrect.mkdir(parents=True)
    (resurrect / "tmux_resurrect_20260101T000000.txt").write_text("snap\n", encoding="utf-8")
    runner._do_provision_tmux(_tmux_action(home), "backup")
    assert rec["saves"] == 0  # snapshot present → no re-save


def test_failed_clone_is_a_warning_not_a_change(tmp_path, monkeypatch):
    """A failed plugin clone (offline) is surfaced as a WARNING but must NOT mark the apply
    `changed` — else every re-apply falsely reports `created` (codex/opus idempotency finding).
    With nothing else to do (snapshot present, plugins the only gap), a clone-failure-only run is
    `skipped`, and the warning is in the detail."""
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    _activation_seams(monkeypatch)  # stub launchctl/cleanup/save; the clone is overridden below
    # make every clone fail (offline) …
    monkeypatch.setattr(shared_mod, "_git_clone", lambda repo, dest: 1)
    # nothing stale to clean (so a clone-failure is the ONLY activation outcome).
    monkeypatch.setattr(tmux_mod, "_clean_stale_continuum_boot", lambda plan: False)
    # … and pre-stage the resurrect dir + a snapshot so NOTHING else is a change.
    resurrect = home / ".tmux" / "resurrect"
    resurrect.mkdir(parents=True)
    (resurrect / "tmux_resurrect_20260101T000000.txt").write_text("snap\n", encoding="utf-8")
    # second apply (config already current from a first) so only activation could change anything.
    runner._do_provision_tmux(_tmux_action(home), "backup")
    res2 = runner._do_provision_tmux(_tmux_action(home), "backup")
    assert res2.status == "skipped", res2.detail  # a failed clone did NOT inflate `changed`
    assert "NOT installed" in res2.detail  # the warning is still surfaced


def test_resurrect_save_does_not_start_a_server(tmp_path, monkeypatch):
    """_tmux_resurrect_save must NOT start a server just to snapshot it (it would save an empty
    pre-restore state). With no live server it returns non-zero and runs no save (opus finding)."""
    import shutil
    import subprocess

    from riglib.actions import tmux as tmux_mod

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    # install a real save.sh so the function gets past its existence check.
    save = home / ".tmux" / "plugins" / "tmux-resurrect" / "scripts" / "save.sh"
    save.parent.mkdir(parents=True)
    save.write_text("#!/usr/bin/env bash\ntouch ${SAVED_MARKER:-/dev/null}\n", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        import subprocess as sp
        # `tmux list-sessions` → non-zero (no server). Anything else shouldn't be reached.
        return sp.CompletedProcess(cmd, 1, stdout="", stderr="no server running")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/tmux" if n == "tmux" else None)
    plan = tmux.build_tmux(repo_home=home)
    rc = tmux_mod._tmux_resurrect_save(plan)
    assert rc != 0  # no live server → no save
    # it probed for a live server (list-sessions) but NEVER ran save.sh and NEVER started a server.
    assert any("list-sessions" in " ".join(c) for c in calls)
    assert not any("save.sh" in " ".join(c) for c in calls)
    assert not any("new-session" in " ".join(c) for c in calls)


def test_dry_run_skips_all_live_activation(tmp_path, monkeypatch):
    """RIG_TMUX_DRY_RUN=1 writes the on-disk artifacts but performs NO live effect (clone /
    launchctl / save / cleanup) — the seam the unit suite + CI rely on."""
    import sys as _sys

    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "darwin")
    monkeypatch.setenv("RIG_TMUX_DRY_RUN", "1")
    rec = _activation_seams(monkeypatch)
    runner._do_provision_tmux(_tmux_action(home, boot={"enabled": True}), "backup")
    # the files still land …
    assert (home / ".config" / "rig" / "tmux" / "rig.tmux.conf").is_file()
    # … but NOTHING live ran.
    assert rec["clones"] == [] and rec["launchctl"] == []
    assert rec["saves"] == 0 and rec["cleanups"] == 0


def test_clean_continuum_boot_is_idempotent_noop_when_absent(tmp_path, monkeypatch):
    """The cleanup is idempotent: with no stale continuum boot present it does nothing and
    never errors (a clean machine has nothing to clean)."""
    import sys as _sys

    from riglib.actions import tmux as tmux_mod

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "darwin")
    monkeypatch.setattr(service_ctl_mod, "_launchctl", lambda verb, arg: 0)
    plan = tmux.build_tmux(repo_home=home)
    # must not raise even though no Tmux.Start.plist / osx_disable.sh exists.
    tmux_mod._clean_stale_continuum_boot(plan)


def test_drift_tmux_reports_all_drifted_regions(tmp_path, monkeypatch):
    """Two regions drift at once → BOTH reported (no early-return masking the second)."""
    from riglib.actions import runner
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    a = _tmux_action(home)
    runner._do_provision_tmux(a, "backup")
    gdir = home / ".config" / "rig" / "tmux"
    (gdir / "rig.tmux.conf").write_text("TAMPERED\n", encoding="utf-8")   # region 1
    (gdir / "cc-save.sh").unlink()                                          # region 2
    drift = [d for d in detect(InstallPlan(actions=[a])).items if d.category == "tmux"]
    targets = {d.target.name for d in drift}
    assert "rig.tmux.conf" in targets and "cc-save.sh" in targets
