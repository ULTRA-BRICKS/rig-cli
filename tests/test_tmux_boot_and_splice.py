"""tmux configuration provisioning — config, pure rendering, plan, install, drift (part 3 of 5).

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

import os
import subprocess
import time
from pathlib import Path

import pytest

from riglib import tmux
from tests.tmux_helpers import _plan


# ── pty pressure (macOS kern.tty.ptmx_max ceiling, the zpty/spaceship-crash observability) ──────
def test_pty_pressure_ok_comfortably_under_ratio():
    v = tmux.assess_pty_pressure(100, 511)
    assert v.state == "ok"


def test_pty_pressure_high_at_or_above_ratio():
    v = tmux.assess_pty_pressure(400, 511)
    assert v.state == "high"


def test_pty_pressure_boundary_exactly_at_warn_ratio_is_high():
    """358/511 == 0.700587... >= 70% — the >= boundary must read `high`, not `ok`."""
    v = tmux.assess_pty_pressure(358, 511)
    assert v.state == "high"


def test_pty_pressure_unavailable_when_pty_count_missing():
    v = tmux.assess_pty_pressure(None, 511)
    assert v.state == "unavailable"


def test_pty_pressure_unavailable_when_ptmx_max_missing():
    v = tmux.assess_pty_pressure(100, None)
    assert v.state == "unavailable"


def test_pty_pressure_unavailable_when_ptmx_max_zero():
    """A zero ceiling must never divide-by-zero — treated as unreadable, not a 100%+ ratio."""
    v = tmux.assess_pty_pressure(100, 0)
    assert v.state == "unavailable"


def test_pty_pressure_detail_contains_raw_counts():
    v = tmux.assess_pty_pressure(100, 511)
    assert "100" in v.detail
    assert "511" in v.detail


def _tmux_pty_pressure_status_line(
    monkeypatch, *, pty_count=100, ptmx_max=511, on_darwin=True, has_tmux_action=True
):
    """Render the `rig status` pty-pressure line for a one-action tmux plan (or a plan with no
    tmux action at all, to prove silence). The counts are a kernel sysctl reading and a live
    `/dev/ttys*` scan, not an on-disk fixture like the autosave health file, so the I/O seams
    (`cli_status_services._live_pty_count`/`cli_status_services._ptmx_max`) and the platform gate (`riglib.drift._on_darwin`) are
    mocked directly rather than faked via `tmp_path`."""
    from riglib import cli_status_services
    from riglib.plan import Action, InstallPlan

    monkeypatch.setattr(cli_status_services, "_live_pty_count", lambda: pty_count)
    monkeypatch.setattr(cli_status_services, "_ptmx_max", lambda: ptmx_max)
    monkeypatch.setattr("riglib.drift._on_darwin", lambda: on_darwin)
    actions = []
    if has_tmux_action:
        actions.append(
            Action(kind="provision_tmux", category="tmux", item="config",
                   source=Path("/x"), target=Path("/x/.tmux.conf"), options={})
        )
    plan = InstallPlan(actions=actions)
    captured: list[str] = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: captured.append(" ".join(str(x) for x in a)))
    cli_status_services._print_tmux_pty_pressure_status(plan)
    return "\n".join(captured)


def test_status_pty_pressure_line_ok(monkeypatch):
    line = _tmux_pty_pressure_status_line(monkeypatch, pty_count=40, ptmx_max=511)
    assert "tmux pty pressure" in line and "40/511" in line
    assert "HIGH" not in line


def test_status_pty_pressure_line_high(monkeypatch):
    line = _tmux_pty_pressure_status_line(monkeypatch, pty_count=400, ptmx_max=511)
    assert "HIGH" in line


def test_status_pty_pressure_silent_when_unavailable(monkeypatch):
    """An unreadable count (e.g. non-darwin or a failed sysctl call) prints nothing — no noise
    for a machine/repo where the reading can't be trusted."""
    line = _tmux_pty_pressure_status_line(monkeypatch, pty_count=None, ptmx_max=511)
    assert line == ""


def test_status_pty_pressure_silent_when_no_tmux_action(monkeypatch):
    line = _tmux_pty_pressure_status_line(monkeypatch, has_tmux_action=False)
    assert line == ""


def test_status_pty_pressure_silent_off_darwin(monkeypatch):
    line = _tmux_pty_pressure_status_line(monkeypatch, on_darwin=False)
    assert line == ""


def _tmux_autosave_status_line(monkeypatch, tmp_path, *, autosave=None, health=None, age_min=0, installed=True):
    """Render the `rig status` autosave-freshness line for a one-action tmux plan. Simulates an
    INSTALLED agent (its plist on disk) unless `installed=False`, and writes an optional health file
    (aged `age_min` minutes) into the generated dir the printer reads."""
    import json
    import os as _os
    import time as _time

    from riglib import cli_status_services
    from riglib.actions.tmux import tmux_plan_from_action
    from riglib.plan import Action, InstallPlan

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    gen = tmp_path / ".config" / "rig" / "tmux"
    gen.mkdir(parents=True)
    opts = {
        "generated_dir": str(gen),
        "conf_path": str(tmp_path / ".tmux.conf"),
        "autosave": {} if autosave is None else autosave,
    }
    action = Action(kind="provision_tmux", category="tmux", item="config",
                    source=tmp_path, target=tmp_path / ".tmux.conf", options=opts)
    plan = InstallPlan(actions=[action])
    if installed:  # the launchd plist on disk = the agent is provisioned here
        tplan = tmux_plan_from_action(action)
        tplan.autosave_plist_path.parent.mkdir(parents=True, exist_ok=True)
        tplan.autosave_plist_path.write_text("<plist/>", encoding="utf-8")
    if health is not None:
        hp = gen / tmux.AUTOSAVE_HEALTH_NAME
        # a str is written VERBATIM (to exercise corrupt / non-object JSON); a dict is serialized.
        hp.write_text(health if isinstance(health, str) else json.dumps(health), encoding="utf-8")
        if age_min:
            when = _time.time() - age_min * 60
            _os.utime(hp, (when, when))
    captured: list[str] = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: captured.append(" ".join(str(x) for x in a)))
    cli_status_services._print_tmux_autosave_status(plan)
    return "\n".join(captured)


def test_status_autosave_line_fresh_when_health_recent(monkeypatch, tmp_path):
    line = _tmux_autosave_status_line(monkeypatch, tmp_path, health={"result": "ok"}, age_min=5)
    assert "fresh" in line and "autosave agent" in line


def test_status_autosave_line_stale_when_health_old(monkeypatch, tmp_path):
    line = _tmux_autosave_status_line(
        monkeypatch, tmp_path, autosave={"stale_after": 45}, health={"result": "ok"}, age_min=90
    )
    assert "STALE" in line


def test_status_autosave_line_unhealthy_when_recent_run_failed(monkeypatch, tmp_path):
    """A fresh health record whose last run failed (result=error) renders a WARNING line, not a
    green `fresh` — a live timer with a broken save must be visible in `rig status`."""
    line = _tmux_autosave_status_line(
        monkeypatch, tmp_path, health={"result": "error"}, age_min=5
    )
    assert "UNHEALTHY" in line and "could not save" in line


def test_status_autosave_line_missing_when_no_health(monkeypatch, tmp_path):
    line = _tmux_autosave_status_line(monkeypatch, tmp_path, health=None)
    assert "no health record" in line


def test_status_autosave_line_silent_when_disabled(monkeypatch, tmp_path):
    line = _tmux_autosave_status_line(monkeypatch, tmp_path, autosave={"enabled": False})
    assert line == ""


def test_status_autosave_line_silent_when_agent_not_installed(monkeypatch, tmp_path):
    """No plist on disk = the agent was never applied here (or off darwin). Don't nag about
    freshness on every `rig status` when there is nothing installed to be fresh."""
    line = _tmux_autosave_status_line(monkeypatch, tmp_path, health=None, installed=False)
    assert line == ""


def test_status_autosave_line_missing_when_health_corrupt(monkeypatch, tmp_path):
    """A corrupt (unparseable) health file is the exact silent-corruption case this feature exists
    to surface — it must read as 'no health record', never crash."""
    line = _tmux_autosave_status_line(monkeypatch, tmp_path, health="not-json{")
    assert "no health record" in line


def test_status_autosave_line_non_dict_health_suppresses_result_note(monkeypatch, tmp_path):
    """Valid JSON that isn't an object (a partial/garbled write) → freshness still assessed from the
    file mtime, but the `result=` note is suppressed (never printed as `result=None`)."""
    line = _tmux_autosave_status_line(monkeypatch, tmp_path, health="[1, 2]", age_min=5)
    assert "fresh" in line and "result=" not in line


def test_autosave_label_configurable():
    p = tmux.build_tmux(repo_home=Path("/home/u"), autosave={"label": "com.me.tmux-save"})
    assert p.autosave_label == "com.me.tmux-save"
    assert p.autosave_plist_path.name == "com.me.tmux-save.plist"
    assert "com.me.tmux-save" in p.render_autosave_plist()


def test_autosave_plist_render_is_deterministic():
    p = tmux.build_tmux(repo_home=Path("/home/u"))
    assert p.render_autosave_plist() == p.render_autosave_plist()
    assert p.render_autosave_script() == p.render_autosave_script()


def test_boot_path_env_is_stable_when_tmux_bin_resolution_changes(monkeypatch):
    """tmux_bin is baked at plan time, so a LATER change to ambient PATH (which would make
    _resolve_tmux_bin pick a different dir) must NOT change an already-built plan's rendered
    plist — the determinism contract that avoids apply/status drift flap."""
    p = tmux.build_tmux(repo_home=Path("/home/u"))
    before = p.render_boot_plist()
    monkeypatch.setattr(tmux, "_resolve_tmux_bin", lambda: "/some/other/dir/tmux")
    assert p.render_boot_plist() == before  # baked field wins; no re-resolution at render time


def test_boot_plist_has_logging_paths():
    """Without StandardOutPath/StandardErrorPath the boot agent is a BLACK BOX: a half-working
    boot (server up, restore skipped) leaves zero diagnostics. The plist must log both streams
    into the generated dir alongside the managed scripts."""
    import plistlib

    p = tmux.build_tmux(repo_home=Path("/home/u"))
    parsed = plistlib.loads(p.render_boot_plist().encode("utf-8"))
    assert parsed["StandardOutPath"] == str(p.boot_out_log_path)
    assert parsed["StandardErrorPath"] == str(p.boot_err_log_path)
    assert p.boot_out_log_path.parent == p.generated_dir
    assert p.boot_err_log_path.parent == p.generated_dir
    assert p.boot_out_log_path.name.endswith(".log")


# ── boot script (DEFECT 1: new-session -d loads the conf → continuum restores) ──────────────
def test_boot_script_creates_a_session_to_load_the_conf():
    """The boot script must `tmux new-session -d` (which loads ~/.tmux.conf → the sourced
    rig.tmux.conf → continuum), NOT `tmux start-server` (an empty server that loads nothing)."""
    body = tmux.build_tmux(repo_home=Path("/home/u")).render_boot_script()
    assert body.startswith("#!/")
    assert "new-session -d" in body
    assert "start-server" not in body


def test_boot_script_is_idempotent_attach_or_create():
    """A second login/boot must NOT spawn a duplicate session — the boot script creates the
    canonical session only if it does not already exist (anti-sprawl at boot)."""
    body = tmux.build_tmux(
        repo_home=Path("/home/u"), anti_sprawl={"enabled": True, "session": "main"}
    ).render_boot_script()
    assert "has-session" in body
    assert "main" in body


def _new_session_line(body: str) -> str:
    """The boot script's real `tmux … new-session` invocation (not a comment, not the marker code)."""
    return next(ln for ln in body.splitlines() if "new-session" in ln and not ln.startswith("#"))


def test_boot_script_passes_f_for_custom_conf_path():
    """A non-default conf_path must reach the boot session via `-f`, else the session starts
    WITHOUT the managed config (continuum/resurrect never set → no restore)."""
    body = tmux.build_tmux(
        repo_home=Path("/home/u"), conf_path="~/.config/tmux/custom.conf"
    ).render_boot_script()
    assert " -f /home/u/.config/tmux/custom.conf " in _new_session_line(body)


def test_boot_script_omits_f_for_default_conf_path():
    """The default ~/.tmux.conf is auto-loaded by tmux, so no `-f` is emitted."""
    body = tmux.build_tmux(repo_home=Path("/home/u")).render_boot_script()
    assert " -f " not in _new_session_line(body)


def test_boot_script_tmux_bin_falls_back_to_existing_path(monkeypatch):
    """When tmux isn't on PATH, the boot script must reference an EXISTING common location, not a
    blind Apple-silicon hard-code (codex P2)."""
    monkeypatch.setattr(tmux.shutil, "which", lambda name: None)  # not on PATH
    monkeypatch.setattr(tmux.Path, "exists", lambda self: str(self) == "/usr/local/bin/tmux")
    body = tmux.build_tmux(repo_home=Path("/home/u")).render_boot_script()
    assert "/usr/local/bin/tmux" in body


# ── boot marker: a slow boot-time conf load must not make continuum skip the restore (#674) ──
# continuum starts the layout restore only when the server is younger than
# `@continuum-restore-max-delay` (10 s) at the moment continuum.tmux runs. Under a post-reboot
# load spike the boot-time conf load took ~16 s, so the restore was silently skipped and `main`
# stayed a bare one-pane session. The boot script now holds a marker file while its `new-session`
# loads the conf; the generated conf widens the window only while that marker is fresh.
_BOOT_MARKER_NAME = tmux.BOOT_MARKER_NAME


# A tmux stand-in that records every call and, at `new-session` time, whether the marker exists —
# the one moment the generated conf's `if-shell` would look at it. $FAKE_*_HOOK lets a test act
# while "tmux" is running (send the boot script a signal, or overwrite the marker).
_FAKE_TMUX = r"""#!/usr/bin/env bash
echo "$*" >> "$FAKE_TMUX_LOG"
marker="$FAKE_TMUX_MARKER"
state=absent; [ -e "$marker" ] && state="present:$(cat "$marker")"
case " $* " in
  *" has-session "*)
    echo "marker-at-has-session=$state" >> "$FAKE_TMUX_LOG"
    [ -e "$FAKE_TMUX_LOG.session" ] && exit 0   # a hook created the session
    exit "${FAKE_HAS_SESSION_RC:-1}" ;;
  *" new-session "*)
    echo "marker-at-new-session=$state" >> "$FAKE_TMUX_LOG"
    eval "${FAKE_NEW_SESSION_HOOK:-:}"
    exit "${FAKE_NEW_SESSION_RC:-0}" ;;
esac
"""


def _boot_rig(tmp_path, monkeypatch):
    """(plan, script path, log path, env) with a fake baked tmux and every path under tmp_path."""
    fake = tmp_path / "bin" / "tmux"
    fake.parent.mkdir()
    fake.write_text(_FAKE_TMUX, encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setattr(tmux, "_resolve_tmux_bin", lambda: str(fake))
    plan = tmux.build_tmux(repo_home=tmp_path / "home", generated_dir=tmp_path / "gen")
    plan.generated_dir.mkdir()
    script = plan.boot_script_path
    script.write_text(plan.render_boot_script(), encoding="utf-8")
    log = tmp_path / "tmux.log"
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path / "home"),
        "FAKE_TMUX_LOG": str(log),
        "FAKE_TMUX_MARKER": str(plan.generated_dir / _BOOT_MARKER_NAME),
    }
    return plan, script, log, env


def _run_boot(script, env, **extra_env):
    return subprocess.run(["bash", str(script)], env={**env, **extra_env}, capture_output=True, text=True, timeout=30)


def test_boot_script_holds_a_fresh_marker_while_new_session_loads_the_conf(tmp_path, monkeypatch):
    """The marker exists, stamped with this run's epoch + pid, exactly while `new-session -d` runs
    (that is when the conf — and its `if-shell` on the marker — loads), and is gone afterwards.
    The recovery-socket `-S` handling is untouched."""
    plan, script, log, env = _boot_rig(tmp_path, monkeypatch)
    before = int(time.time())
    r = _run_boot(script, env, RIG_TMUX_RECOVERY_SOCKET="/tmp/rig-sock")
    assert r.returncode == 0, r.stderr
    lines = log.read_text().splitlines()
    assert lines[:2] == ["-S /tmp/rig-sock has-session -t main", "marker-at-has-session=absent"]
    assert lines[2] == "-S /tmp/rig-sock new-session -d -s main"
    state = lines[3]
    assert state.startswith("marker-at-new-session=present:"), lines
    epoch, _pid = state.removeprefix("marker-at-new-session=present:").split()
    assert before <= int(epoch) <= int(time.time())
    assert not (plan.generated_dir / _BOOT_MARKER_NAME).exists()


def test_boot_script_warm_login_leaves_another_boots_marker_alone(tmp_path, monkeypatch):
    """`has-session` → exit 0 is the no-op path: it must not create a marker, and must not remove
    one a concurrent real boot is holding."""
    plan, script, log, env = _boot_rig(tmp_path, monkeypatch)
    marker = plan.generated_dir / _BOOT_MARKER_NAME
    marker.write_text("1700000000 424242\n", encoding="utf-8")
    r = _run_boot(script, env, FAKE_HAS_SESSION_RC="0")
    assert r.returncode == 0, r.stderr
    assert log.read_text().splitlines() == [
        "has-session -t main",
        "marker-at-has-session=present:1700000000 424242",
    ]
    assert marker.read_text() == "1700000000 424242\n"


@pytest.mark.parametrize(
    ("label", "env_extra", "expected_rc"),
    [
        ("success", {}, 0),
        ("new-session fails, no session appeared", {"FAKE_NEW_SESSION_RC": "7"}, 1),
        ("SIGTERM", {"FAKE_NEW_SESSION_HOOK": 'kill -TERM "$PPID"; sleep 0.5'}, 143),
        ("SIGINT", {"FAKE_NEW_SESSION_HOOK": 'kill -INT "$PPID"; sleep 0.5'}, 130),
        ("SIGHUP", {"FAKE_NEW_SESSION_HOOK": 'kill -HUP "$PPID"; sleep 0.5'}, 129),
    ],
)
def test_boot_script_removes_the_marker_on_every_exit_path(tmp_path, monkeypatch, label, env_extra, expected_rc):
    plan, script, log, env = _boot_rig(tmp_path, monkeypatch)
    r = _run_boot(script, env, **env_extra)
    assert r.returncode == expected_rc, (label, r.stderr)
    assert "marker-at-new-session=present:" in log.read_text(), "the marker must exist while new-session runs"
    # no marker and no temp file left behind (the marker is written via a temp name + rename).
    assert sorted(p.name for p in plan.generated_dir.iterdir()) == ["tmux-boot.sh"], label


def test_boot_script_counts_a_session_created_by_the_restore_as_success(tmp_path, monkeypatch):
    """With the widened window the restore can start while the conf is still loading and create
    `main` before the boot client's queued `new-session` runs ("duplicate session: main"). The boot
    still brought the server up with the session: exit 0 (a non-zero exit made the watchdog report
    `boot_failed` for a healthy, restored server) — and the marker is still removed."""
    plan, script, log, env = _boot_rig(tmp_path, monkeypatch)
    r = _run_boot(script, env, FAKE_NEW_SESSION_RC="1", FAKE_NEW_SESSION_HOOK='touch "$FAKE_TMUX_LOG.session"')
    assert r.returncode == 0, r.stderr
    assert log.read_text().count("has-session -t main\n") == 2  # before, and the re-check after the failure
    assert not (plan.generated_dir / _BOOT_MARKER_NAME).exists()


def test_boot_script_never_removes_a_marker_a_newer_boot_now_owns(tmp_path, monkeypatch):
    """Two boots racing (login agent + watchdog): the later one re-stamps the marker. The earlier
    one finishing first must not delete the marker out from under the later one's conf load."""
    plan, script, _log, env = _boot_rig(tmp_path, monkeypatch)
    marker = plan.generated_dir / _BOOT_MARKER_NAME
    hook = f'echo "1700000000 424242" > {marker}'
    r = _run_boot(script, env, FAKE_NEW_SESSION_HOOK=hook)
    assert r.returncode == 0, r.stderr
    assert marker.read_text() == "1700000000 424242\n"


def test_boot_script_still_boots_when_the_marker_cannot_be_written(tmp_path, monkeypatch):
    """The marker only widens continuum's window; failing to write it must never stop tmux from
    coming up (the pre-#674 behavior is the fallback)."""
    plan, script, log, env = _boot_rig(tmp_path, monkeypatch)
    detached = tmp_path / "tmux-boot.sh"
    detached.write_text(script.read_text(), encoding="utf-8")
    script.unlink()
    plan.generated_dir.rmdir()  # the marker's directory is gone → writing the marker must fail
    r = _run_boot(detached, env)
    assert r.returncode == 0, r.stderr
    assert r.stderr == ""
    assert "marker-at-new-session=absent" in log.read_text()


def test_conf_widens_the_continuum_restore_window_only_under_a_fresh_boot_marker():
    """`if-shell` on the boot marker sets @continuum-restore-max-delay, and it sits BEFORE the
    continuum init (which reads the option once, at init) — never as an unconditional `set`."""
    p = _plan()
    lines = p.render_rig_conf().splitlines()
    guarded = [i for i, ln in enumerate(lines) if ln.startswith("if-shell") and "continuum-restore-max-delay" in ln]
    assert len(guarded) == 1, lines
    line = lines[guarded[0]]
    assert _BOOT_MARKER_NAME in line and "set -g @continuum-restore-max-delay 600" in line
    assert guarded[0] < next(i for i, ln in enumerate(lines) if ln.startswith("run-shell") and "continuum.tmux" in ln)
    assert not any(ln.startswith("set -g @continuum-restore-max-delay") for ln in lines)


def test_conf_resets_the_restore_window_right_after_continuum_init():
    """The widened window must not outlive the boot: a later `tmux source-file` on a live server
    that is still younger than 10 minutes would otherwise restore over live windows. tpm's own
    re-run of continuum.tmux (next line after) must also see the default window."""
    lines = _plan().render_rig_conf().splitlines()
    init = next(i for i, ln in enumerate(lines) if ln.startswith("run-shell") and "continuum.tmux" in ln)
    reset = next(i for i, ln in enumerate(lines) if ln == "set -gu @continuum-restore-max-delay")
    tpm = next(i for i, ln in enumerate(lines) if "tpm/tpm" in ln and not ln.startswith("#"))
    assert init < reset < tpm


@pytest.mark.parametrize("over", [{"boot": {"enabled": False}}, {"continuum": {"restore": False}}])
def test_conf_has_no_boot_window_without_a_boot_script_or_restore(over):
    """No boot script → no marker ever exists; restore off → nothing to extend. Emit neither line."""
    conf = _plan(**over).render_rig_conf()
    assert "continuum-restore-max-delay" not in conf


def test_boot_marker_freshness_test_tracks_the_marker_age(tmp_path, live_boot_script_pid):
    """The shell test the conf's `if-shell` runs: true for a fresh marker of a live boot script,
    false for one older than 10 minutes and for no marker at all — also under a generated_dir with
    a space in it."""
    p = tmux.build_tmux(repo_home=tmp_path / "home", generated_dir=tmp_path / "gen dir")
    p.generated_dir.mkdir()

    def fresh() -> bool:
        return subprocess.run(["sh", "-c", p.boot_marker_fresh_test()]).returncode == 0

    assert not fresh()  # no marker
    p.boot_marker_path.write_text(f"1 {live_boot_script_pid}\n", encoding="utf-8")
    assert fresh()
    stale = time.time() - 11 * 60
    os.utime(p.boot_marker_path, (stale, stale))
    assert not fresh()
    recent = time.time() - 8 * 60
    os.utime(p.boot_marker_path, (recent, recent))
    assert fresh()


def test_boot_marker_whose_owner_is_not_a_live_boot_script_is_not_fresh(tmp_path):
    """A boot script SIGKILLed before its exit trap leaks its marker; for the next 10 minutes that
    recent file would authorize `tmux source-file` on a live server to restore over live windows.
    The marker names the boot script's pid and the test requires that pid to be a LIVE process
    running `tmux-boot.sh`: a dead owner — or a pid the OS handed to some unrelated process since —
    makes the marker worthless at once, not after the age bound."""
    p = tmux.build_tmux(repo_home=tmp_path / "home", generated_dir=tmp_path / "gen")
    p.generated_dir.mkdir()
    dead = subprocess.Popen(["true"])
    dead.wait()
    for owner in (dead.pid, os.getpid()):  # killed owner; pid reused by an unrelated live process
        p.boot_marker_path.write_text(f"{int(time.time())} {owner}\n", encoding="utf-8")
        assert subprocess.run(["sh", "-c", p.boot_marker_fresh_test()]).returncode != 0, owner


def test_boot_script_signalled_between_marker_write_and_rename_leaves_no_temp_file(tmp_path, monkeypatch):
    """The marker is written via a temp name; a signal landing after the write but before the
    rename must not strand `<marker>.<pid>` in the generated dir."""
    plan, script, _log, env = _boot_rig(tmp_path, monkeypatch)
    body = script.read_text(encoding="utf-8").replace(
        'mv -f "$BOOT_MARKER.$$" "$BOOT_MARKER"', 'kill -TERM "$$"; sleep 0.5; mv -f "$BOOT_MARKER.$$" "$BOOT_MARKER"'
    )
    script.write_text(body, encoding="utf-8")
    r = _run_boot(script, env)
    assert r.returncode == 143, r.stderr
    assert sorted(p.name for p in plan.generated_dir.iterdir()) == ["tmux-boot.sh"]


# ── anti-sprawl attach-or-create ─────────────────────────────────────────────────────────
def test_anti_sprawl_script_is_attach_or_create():
    """The anti-sprawl entry re-attaches the ONE canonical session, never spawns a duplicate."""
    body = tmux.build_tmux(repo_home=Path("/home/u"),
                           anti_sprawl={"enabled": True, "session": "main"}).render_attach_script()
    assert body.startswith("#!/")
    # attach-or-create: attach the named session, else create it (single canonical session).
    assert "attach" in body and "new-session" in body
    assert "main" in body  # the canonical session name


def test_anti_sprawl_uses_configured_session_name():
    body = tmux.build_tmux(repo_home=Path("/home/u"),
                           anti_sprawl={"enabled": True, "session": "work"}).render_attach_script()
    assert "work" in body


# ── the import line + managed-block sentinels ────────────────────────────────────────────
def test_import_line_sources_the_generated_file():
    p = tmux.build_tmux(repo_home=Path("/home/u"))
    line = p.import_line()
    assert line.startswith("source-file ")
    assert str(p.rig_conf_path) in line


def test_import_line_is_quoted_for_paths_with_spaces():
    """A generated_dir / HOME with a space must be single-quoted so tmux sources ONE path,
    not multiple args (codex P2)."""
    p = tmux.build_tmux(repo_home=Path("/Users/A B"), generated_dir="~/.config/rig/tmux")
    line = p.import_line()
    assert line == f"source-file '{p.rig_conf_path}'"
    assert "/Users/A B/" in line  # the space survives inside the quotes


def test_null_processes_normalizes_to_default():
    """A YAML `processes:` with no value (None) must NOT crash planning — normalize to default."""
    p = tmux.build_tmux(repo_home=Path("/home/u"), resurrect={"processes": None})
    conf = p.render_rig_conf()  # would TypeError on list(None) before the fix
    assert "@resurrect-processes" in conf


def test_null_nested_knobs_normalize_to_default():
    """Present-but-null nested knobs (save_interval/enabled with no value) use the default,
    never crashing int(None)/bool(None) (codex P2)."""
    p = tmux.build_tmux(
        repo_home=Path("/home/u"),
        continuum={"save_interval": None, "restore": None, "boot": None},
        cc_restore={"enabled": None}, moshi={"enabled": None},
        omp_restore={"enabled": None}, codex_restore={"enabled": None},
        anti_sprawl={"enabled": None, "session": None}, boot={"enabled": None, "label": None},
        autosave={"enabled": False},  # keep continuum's own save so save_interval-default is visible
    )
    conf = p.render_rig_conf()
    assert f"@continuum-save-interval '{tmux.DEFAULT_SAVE_INTERVAL}'" in conf
    assert p.continuum_restore is True and p.cc_restore_enabled is True  # null → default True
    assert p.omp_restore_enabled is True and p.codex_restore_enabled is True  # null → default True
    assert p.anti_sprawl_session == tmux.DEFAULT_SESSION  # null → default
    assert p.boot_label == tmux.DEFAULT_BOOT_LABEL


def test_explicit_empty_processes_list_is_honored():
    """An EXPLICIT `processes: []` clears matching — it must NOT silently fall back to the
    default (codex P2)."""
    p = tmux.build_tmux(repo_home=Path("/home/u"), resurrect={"processes": []})
    assert p.resurrect_processes == []
    line = next(ln for ln in p.render_rig_conf().splitlines() if "@resurrect-processes" in ln)
    assert line.strip() == "set -g @resurrect-processes ''"  # empty, not the default list


def test_resurrect_hook_command_is_shell_quoted_for_spaces():
    """The resurrect-hook VALUE is exec'd as a shell command, so a script path with a space
    must be shell-quoted inside it — else resurrect splits it and the hook never runs (codex P2).
    """
    p = tmux.build_tmux(repo_home=Path("/Users/A B"), cc_restore={"enabled": True})
    conf = p.render_rig_conf()
    save_line = next(ln for ln in conf.splitlines() if "post-save-all" in ln)
    # the path with a space must be shell-quoted (single quotes from shlex.quote) inside the value.
    assert "'/Users/A B/" in save_line


def test_managed_block_sentinels_present():
    assert tmux.BLOCK_BEGIN == "# === rig-managed (tmux) BEGIN ==="
    assert tmux.BLOCK_END == "# === rig-managed (tmux) END ==="


# ── splice_managed_block (pure) — replace ONLY between markers ────────────────────────────
def test_splice_appends_block_when_absent():
    original = "# my hand-written tmux\nset -g mouse on\n"
    out = tmux.splice_managed_block(original, "BODY-A\nBODY-B")
    assert "set -g mouse on" in out  # user lines preserved
    assert tmux.BLOCK_BEGIN in out and tmux.BLOCK_END in out
    assert "BODY-A" in out and "BODY-B" in out
    # the begin sentinel comes before the end sentinel and the body sits between.
    assert out.index(tmux.BLOCK_BEGIN) < out.index("BODY-A") < out.index(tmux.BLOCK_END)


def test_splice_replaces_only_between_markers():
    original = (
        "set -g mouse on\n"
        f"{tmux.BLOCK_BEGIN}\n"
        "OLD-BODY\n"
        f"{tmux.BLOCK_END}\n"
        "set -g history-limit 100000\n"  # a user line AFTER the block
    )
    out = tmux.splice_managed_block(original, "NEW-BODY")
    assert "OLD-BODY" not in out
    assert "NEW-BODY" in out
    assert "set -g mouse on" in out  # user line before block kept
    assert "set -g history-limit 100000" in out  # user line after block kept
    # exactly one managed block (no duplication).
    assert out.count(tmux.BLOCK_BEGIN) == 1 and out.count(tmux.BLOCK_END) == 1


def test_splice_is_idempotent():
    original = "set -g mouse on\n"
    once = tmux.splice_managed_block(original, "BODY")
    twice = tmux.splice_managed_block(once, "BODY")
    assert once == twice


# ── neutralizing inline rig-owned lines on migration (the root-cause completion) ──────────
def _live_lines(text):
    return [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]


def _is_neutralized(text, needle):
    """True iff `needle` appears ONLY on neutralized (rig-migrated comment) lines, never live."""
    hits = [ln for ln in text.splitlines() if needle in ln]
    return bool(hits) and all(ln.lstrip().startswith(tmux.NEUTRALIZE_PREFIX) for ln in hits)


def test_neutralize_comments_out_rig_owned_init_and_options():
    """Migration neutralizes every rig-OWNED plugin/continuum/resurrect init line — the three
    rig `@plugin` decls, all `@continuum-*`/`@resurrect-*`, both plugin-init `run-shell`s, and
    tpm's `run` — plus the Moshi wipe. This is the live-machine double-init fix (2026-06-18):
    a hand-written `run-shell …/continuum.tmux` fires continuum-restore BEFORE rig's appended
    source-file sets the login-shell default-command, so the inline init MUST be neutralized.
    """
    original = (
        "set -g @plugin 'tmux-plugins/tpm'\n"
        "set -g @plugin 'tmux-plugins/tmux-resurrect'\n"
        "set -g @plugin 'tmux-plugins/tmux-continuum'\n"
        "set -g @continuum-restore 'on'\n"
        "set -g @continuum-boot 'on'\n"
        "set -g @continuum-boot-options 'iterm'\n"
        "set -g @resurrect-processes 'ssh psql ~rails'\n"
        "set -g @resurrect-strategy-vim 'session'\n"
        "set -g @resurrect-capture-pane-contents 'on'\n"
        "run-shell ~/.tmux/plugins/tmux-resurrect/resurrect.tmux\n"
        "run-shell ~/.tmux/plugins/tmux-continuum/continuum.tmux\n"
        "if-shell '[ -n \"$MOSHI_CLIENT\" ]' {\n"
        "  set -g status-right ''\n"
        "}\n"
        "run '~/.tmux/plugins/tpm/tpm'\n"
    )
    out = tmux.neutralize_inline_rig_lines(original)
    # the Moshi if-shell wipe block IS neutralized (the original root-cause line) …
    assert not any("status-right ''" in ln for ln in _live_lines(out)), "Moshi wipe must be off"
    # … and so is EVERY rig-owned init/option/decl line (the double-init completion).
    for needle in (
        "@plugin 'tmux-plugins/tpm'",
        "@plugin 'tmux-plugins/tmux-resurrect'",
        "@plugin 'tmux-plugins/tmux-continuum'",
        "@continuum-restore", "@continuum-boot", "@continuum-boot-options",
        "@resurrect-processes", "@resurrect-strategy-vim", "@resurrect-capture-pane-contents",
        "tmux-resurrect/resurrect.tmux", "tmux-continuum/continuum.tmux",
        "tmux/plugins/tpm/tpm",
    ):
        assert _is_neutralized(out, needle), f"rig-owned line not neutralized: {needle}"


def test_neutralize_preserves_third_party_plugins_and_personal_prefs():
    """Over-neutralization guard: a third-party `@plugin` (rig does NOT own it — rig's tpm loads
    it) and every personal pref stay LIVE. Only the THREE rig-owned plugins are swept.
    """
    original = (
        "set -g mouse on\n"
        "set -g history-limit 100000\n"
        "set -g base-index 1\n"
        "set -g @plugin 'tmux-plugins/tmux-sensible'\n"  # third-party — rig does NOT own it
        "set -g @plugin 'tmux-plugins/tmux-yank'\n"       # third-party — rig does NOT own it
        "set -g @plugin 'tmux-plugins/tmux-resurrect'\n"  # rig-owned — neutralized
        "set-option -ga update-environment ' MOSHI_CLIENT'\n"  # a pref, NOT the wipe
        "set -g status-right '#{battery}'\n"              # a real value, not the empty wipe
        "bind r source-file ~/.tmux.conf\n"
    )
    out = tmux.neutralize_inline_rig_lines(original)
    live = "\n".join(_live_lines(out))
    for kept in (
        "set -g mouse on", "set -g history-limit 100000", "set -g base-index 1",
        "tmux-plugins/tmux-sensible", "tmux-plugins/tmux-yank",
        "update-environment ' MOSHI_CLIENT'", "status-right '#{battery}'",
        "bind r source-file",
    ):
        assert kept in live, f"line wrongly neutralized (over-reach): {kept}"
    # the one rig-owned plugin IS swept.
    assert _is_neutralized(out, "@plugin 'tmux-plugins/tmux-resurrect'")


def test_neutralize_does_not_over_reach_on_lookalikes():
    """Tokens that LOOK rig-owned but aren't an init must stay live: a third-party plugin whose
    name merely CONTAINS a rig plugin name, a fork under the same org, a `source-file` keybinding,
    a `status-right` VALUE that mentions a rig token, a non-set directive carrying the token, and
    a commented-out mention. The `@continuum-`/`@resurrect-` match is anchored to a set directive,
    and `@plugin` to the QUOTED spec — so none of these false-match (review findings 1 + 3)."""
    cases = (
        "set -g @plugin 'someuser/tmux-resurrect-fork'\n",   # not the rig spec
        "set -g @plugin 'tmux-plugins/tmux-resurrect-fork'\n",  # same-org fork — closing quote anchors
        "bind r source-file ~/.tmux.conf\n",                 # a keybinding, not a plugin init
        "set -g status-right 'continuum.tmux rules'\n",       # a real value, not a run-shell init
        "set -g status-right '#(tmux show-option -gv @continuum-save-interval)'\n",  # value prints opt
        "bind r run-shell 'tmux set @continuum-boot on'\n",   # keybind whose VALUE has the token
        "display-message '@resurrect-processes test'\n",      # not a set directive at all
        "# I once used tmux-continuum but stopped\n",         # a comment — never re-touched
    )
    for original in cases:
        assert tmux.neutralize_inline_rig_lines(original) == original, original


def test_neutralize_unquoted_rig_plugin_spec_but_not_fork():
    """An UNQUOTED `set -g @plugin tmux-plugins/tpm` is valid tmux and IS the rig plugin → it
    must be neutralized (the double-init source), while a fork `…/tmux-resurrect-fork` (quoted or
    bare) is matched by EXACT spec value, not a prefix, so it stays live (review findings 2 + 3)."""
    P = tmux.NEUTRALIZE_PREFIX
    for owned in (
        "set -g @plugin tmux-plugins/tpm\n",
        "set -g @plugin tmux-plugins/tmux-resurrect\n",
        "set -g @plugin 'tmux-plugins/tmux-continuum'\n",
    ):
        assert tmux.neutralize_inline_rig_lines(owned).lstrip().startswith(P), owned
    for fork in (
        "set -g @plugin tmux-plugins/tmux-resurrect-fork\n",
        "set -g @plugin 'tmux-plugins/tmux-resurrect-fork'\n",
    ):
        assert tmux.neutralize_inline_rig_lines(fork) == fork, fork


def test_neutralize_run_shell_init_anchored_not_nested_arg():
    """The plugin-init match is on the run-shell/run COMMAND, not a substring: real inits (bare,
    quoted, `-b`, absolute) are neutralized, but a command that merely MENTIONS the plugin path as
    a nested ARGUMENT (a backup/copy) stays live — over-reach would change a line the user did not
    ask to change (review finding 1)."""
    P = tmux.NEUTRALIZE_PREFIX
    for init in (
        "run-shell ~/.tmux/plugins/tmux-resurrect/resurrect.tmux\n",
        "run-shell '~/.tmux/plugins/tmux-continuum/continuum.tmux'\n",
        "run-shell -b ~/.tmux/plugins/tmux-continuum/continuum.tmux\n",
        "run-shell -b '~/.tmux/plugins/tmux-continuum/continuum.tmux'\n",  # -b AND quotes together
        "run '~/.tmux/plugins/tpm/tpm'\n",
        "run-shell /Users/me/.tmux/plugins/tmux-continuum/continuum.tmux\n",
    ):
        assert tmux.neutralize_inline_rig_lines(init).lstrip().startswith(P), init
    for nested in (
        'run-shell "tar czf bak.tgz ~/.tmux/plugins/tmux-continuum/continuum.tmux"\n',
        "run-shell 'cp ~/.tmux/plugins/tmux-continuum/continuum.tmux /tmp'\n",
    ):
        assert tmux.neutralize_inline_rig_lines(nested) == nested, nested


def test_neutralize_skips_moshi_lookalike_inside_managed_block():
    """A `status-right ''` / `@continuum-restore` lookalike INSIDE rig's managed block (block apply
    mode — rig's OWN generated config) must survive untouched: both neutralize passes skip the
    region between BLOCK_BEGIN/BLOCK_END. Guards the block-skip so a future regression that removes
    it (and lets neutralize corrupt rig's own block) fails here, not only via the splice masking."""
    inside = (
        "  set -g status-right ''\n"            # a Moshi-wipe lookalike (rig's own, when moshi on)
        "  set -g @continuum-restore 'off'\n"   # rig's own option line
        "  run-shell ~/.tmux/plugins/tmux-continuum/continuum.tmux\n"  # rig's own init
    )
    original = (
        "set -g @continuum-restore 'on'\n"      # USER line above the block — neutralized
        f"{tmux.BLOCK_BEGIN}\n{inside}{tmux.BLOCK_END}\n"
        "set -g mouse on\n"                      # USER pref below the block — live
    )
    out = tmux.neutralize_inline_rig_lines(original)
    b, e = out.index(tmux.BLOCK_BEGIN), out.index(tmux.BLOCK_END)
    assert tmux.NEUTRALIZE_PREFIX not in out[b:e], "rig's own managed block was wrongly neutralized"
    assert tmux.NEUTRALIZE_PREFIX in out[:b], "the USER line above the block must be neutralized"
    assert "set -g mouse on" in out[e:], "the USER pref below the block must stay live"


def test_neutralize_leaves_rig_option_inside_user_brace_block_live():
    """A rig-owned option a user GUARDED inside a non-Moshi `if-shell '…' { … }` conditional stays
    LIVE (multi- and single-line): commenting just the inner line would leave a dangling/empty
    brace body (a parse hazard) and break the user's deliberate condition. Under-reach is preferred
    over over-reach here (review findings 1+2). The Moshi wipe brace is still fully commented."""
    multi = "if-shell '[ -n \"$SOME\" ]' {\n  set -g @continuum-restore 'on'\n}\n"
    assert tmux.neutralize_inline_rig_lines(multi) == multi, "multi-line user brace must stay live"
    single = "if-shell '[ -n \"$SOME\" ]' { set -g @continuum-restore 'on' }\n"
    assert tmux.neutralize_inline_rig_lines(single) == single, "single-line user brace must stay live"
    # contrast: the Moshi wipe brace IS fully commented (braces included → no dangling brace).
    moshi = "if-shell '[ -n \"$MOSHI_CLIENT\" ]' {\n  set -g status-right ''\n}\n"
    out = tmux.neutralize_inline_rig_lines(moshi)
    assert not [ln for ln in out.splitlines() if not ln.lstrip().startswith("#")], \
        "the Moshi wipe block must be fully commented (no dangling brace)"
    # a top-level rig-owned option AFTER the brace block is still neutralized (brace_depth resets).
    after = multi + "set -g @continuum-restore 'on'\n"
    out2 = tmux.neutralize_inline_rig_lines(after)
    live = [ln for ln in out2.splitlines() if not ln.lstrip().startswith("#")]
    # the guarded one (inside braces) survives; the top-level one (after `}`) is neutralized.
    assert sum(1 for ln in live if "@continuum-restore" in ln) == 1, \
        "the guarded option survives; the top-level one after the brace is neutralized"


def test_neutralize_decorative_brace_in_value_does_not_leak_skip():
    """A literal `{` inside a quoted value (e.g. `status-left "…{…"`) must NOT be treated as a
    block opener — otherwise the NEXT rig-owned line would be wrongly skipped (left live → the
    double-init survives). The brace tracker keys on STRUCTURAL `{`-at-end-of-line, not a raw
    count, so the following @plugin/run-shell init is still neutralized (review finding)."""
    original = (
        "set -g status-left \"session:#S{\"\n"   # decorative brace in a value
        "set -g @plugin 'tmux-plugins/tmux-continuum'\n"
        "run-shell ~/.tmux/plugins/tmux-continuum/continuum.tmux\n"
    )
    out = tmux.neutralize_inline_rig_lines(original)
    live = [ln for ln in out.splitlines() if not ln.lstrip().startswith("#")]
    assert any("status-left" in ln for ln in live), "the status-left value must survive"
    assert not any("tmux-plugins/tmux-continuum'" in ln for ln in live), \
        "the @plugin after a decorative brace must still be neutralized"
    assert not any("continuum.tmux" in ln for ln in live), \
        "the init after a decorative brace must still be neutralized"


def test_neutralize_nested_user_braces_keep_inner_lines_live():
    """NESTED `if-shell '…' { … if-shell '…' { … } … }` conditionals: an INNER `}` must not
    re-expose the OUTER block (a boolean in-brace flag had that bug). Every rig-owned line at any
    depth > 0 stays live (no dangling brace); a line after the OUTERMOST `}` is neutralized."""
    original = (
        "if-shell '[ -n \"$A\" ]' {\n"
        "  if-shell '[ -n \"$B\" ]' {\n"
        "    set -g @continuum-restore 'on'\n"   # depth 2 — live
        "  }\n"
        "  set -g @continuum-restore 'on'\n"     # depth 1 (after inner close) — live
        "}\n"
        "set -g @continuum-restore 'on'\n"       # depth 0 — neutralized
    )
    out = tmux.neutralize_inline_rig_lines(original)
    live = [ln for ln in out.splitlines() if not ln.lstrip().startswith("#")]
    assert sum(1 for ln in live if "@continuum-restore" in ln) == 2, \
        "both nested (depth>0) lines stay live; only the top-level one is neutralized"
    assert tmux.NEUTRALIZE_PREFIX in out, "the top-level line must be neutralized"


def test_neutralize_closing_brace_with_trailing_text_decrements_depth():
    """A closing brace line that is not a BARE `}` — `} # end` or `} <directive>` — must still
    close the conditional, so a rig-owned init AFTER it is neutralized (else the double-init the
    fix targets survives silently). A `}` inside a quoted VALUE never false-closes (it starts with
    the set verb, not `}`)."""
    original = (
        "if-shell '[ -n \"$X\" ]' {\n"
        "  set -g @continuum-restore 'on'\n"   # inside — live
        "} # end conditional\n"
        "run-shell ~/.tmux/plugins/tmux-continuum/continuum.tmux\n"  # after close — neutralized
    )
    out = tmux.neutralize_inline_rig_lines(original)
    live = [ln for ln in out.splitlines() if not ln.lstrip().startswith("#")]
    assert any("@continuum-restore" in ln for ln in live), "inside-brace line stays live"
    assert not any("continuum.tmux" in ln for ln in live), \
        "the init after `} # end` must be neutralized (depth decremented past the tailed close)"
    # a literal `}` inside a value is not a closer.
    value = "set -g status-right \"}\"\nset -g @plugin 'tmux-plugins/tpm'\n"
    out2 = tmux.neutralize_inline_rig_lines(value)
    assert tmux.NEUTRALIZE_PREFIX in out2, "the @plugin after a value-`}` is still neutralized"


def test_neutralize_bare_wipe_inside_user_brace_stays_live():
    """A bare `set -g status-right ''` a user GUARDED inside their OWN non-Moshi `{ … }` stays
    live — commenting just it would dangle the brace. A bare wipe at TOP level is still
    neutralized, and the Moshi-guarded wipe block is still commented whole."""
    guarded = "if-shell '[ -n \"$X\" ]' {\n  set -g status-right ''\n}\n"
    assert tmux.neutralize_inline_rig_lines(guarded) == guarded, "guarded bare wipe must stay live"
    assert tmux.neutralize_inline_rig_lines("set -g status-right ''\n").lstrip().startswith(
        tmux.NEUTRALIZE_PREFIX
    ), "a top-level bare wipe is still neutralized"


def test_neutralize_moshi_block_with_literal_brace_in_value_does_not_overconsume():
    """The multi-line Moshi-block scan uses STRUCTURAL depth (end-of-line `{` / first-token `}`),
    so a literal `{` inside a value WITHIN the block (`status-left \"x{\"`) does not over-consume
    to EOF and swallow the lines after the block. The Moshi block is commented whole; the line
    after the closing `}` stays live."""
    original = (
        "if-shell '[ -n \"$MOSHI_CLIENT\" ]' {\n"
        "  set -g status-left \"x{\"\n"   # a literal brace in a value
        "  set -g status-right ''\n"
        "}\n"
        "set -g mouse on\n"               # MUST stay live (not consumed past the close)
    )
    out = tmux.neutralize_inline_rig_lines(original)
    live = [ln for ln in out.splitlines() if not ln.lstrip().startswith("#")]
    assert any(ln.strip() == "set -g mouse on" for ln in live), "line after the block must stay live"
    assert not any("status-right ''" in ln for ln in live), "the Moshi wipe must be commented"
    assert not any("status-left" in ln for ln in live), "the whole Moshi block must be commented"


def test_neutralize_known_limitation_set_hook_init_stays_live():
    """KNOWN LIMITATION (pinned, not a bug): a continuum init run via an indirection rig does not
    model — `set-hook -g session-created 'run-shell …/continuum.tmux'` — is NOT neutralized (the
    run-shell is a quoted VALUE, not the directive). Documented in _is_rig_owned_legacy_line; this
    test makes any future widening of the matcher a DELIBERATE change, not a silent one."""
    original = "set-hook -g session-created 'run-shell ~/.tmux/plugins/tmux-continuum/continuum.tmux'\n"
    assert tmux.neutralize_inline_rig_lines(original) == original


def test_neutralize_known_limitation_xdg_plugin_init_stays_live():
    """KNOWN LIMITATION (pinned, not a bug): rig models only the DEFAULT ~/.tmux/plugins/ location
    (it clones plugins there and inits from that path). An init from a custom/XDG plugin dir
    (~/.config/tmux/plugins/…) is NOT neutralized — but the path-independent @plugin decl + the
    @continuum-*/@resurrect-* options of such a user still ARE. Documented at _PLUGIN_INIT_ENTRYPOINTS."""
    xdg_init = "run-shell ~/.config/tmux/plugins/tmux-continuum/continuum.tmux\n"
    assert tmux.neutralize_inline_rig_lines(xdg_init) == xdg_init
    # the @plugin decl + options are still neutralized (path-independent).
    P = tmux.NEUTRALIZE_PREFIX
    assert tmux.neutralize_inline_rig_lines(
        "set -g @plugin 'tmux-plugins/tmux-continuum'\n"
    ).lstrip().startswith(P)


def test_neutralize_handles_set_option_and_setw_variants():
    """A rig-owned option set via `set-option` / `set-window-option` / `setw` (not just `set -g`)
    is neutralized too — the matcher anchors on the verb + the option name, not a literal `set -g`."""
    original = (
        "set-option -ga @continuum-restore 'on'\n"
        "set-window-option -g @resurrect-strategy-vim 'session'\n"
        "setw -g @continuum-save-interval '5'\n"
    )
    out = tmux.neutralize_inline_rig_lines(original)
    assert not [ln for ln in out.splitlines() if not ln.lstrip().startswith("#")], \
        "set-option/setw rig-owned options must be neutralized"


def test_neutralize_handles_indented_and_run_shell_tpm_variants():
    """An INDENTED rig-owned init and tpm launched via `run-shell` (not bare `run`) are both
    neutralized (the matcher works on the stripped line, and tpm accepts either run form)."""
    original = (
        "    run-shell ~/.tmux/plugins/tmux-continuum/continuum.tmux\n"
        "run-shell '~/.tmux/plugins/tpm/tpm'\n"
    )
    out = tmux.neutralize_inline_rig_lines(original)
    assert not [ln for ln in out.splitlines() if not ln.lstrip().startswith("#")], \
        "every rig-owned init (indented / run-shell tpm) must be neutralized"


def test_neutralize_bare_single_line_moshi_wipe():
    """A bare `set -g status-right ''` (not wrapped in if-shell) is also neutralized."""
    original = "set -g mouse on\nset -g status-right ''\n"
    out = tmux.neutralize_inline_rig_lines(original)
    assert "set -g mouse on" in out and not any(
        "status-right ''" in ln and not ln.lstrip().startswith("#") for ln in out.splitlines()
    )


def test_neutralize_keeps_plain_user_conf_untouched():
    """A conf with no Moshi wipe has nothing to neutralize — every line stays live."""
    plain = "set -g mouse on\nset -g history-limit 100000\nbind r source-file ~/.tmux.conf\n"
    assert tmux.neutralize_inline_rig_lines(plain) == plain


def test_neutralize_keeps_a_real_status_right_value_live():
    """A status-right with an actual value (not the empty wipe) is a user choice — keep it live."""
    original = "set -g status-right '#{battery}'\nset -g mouse on\n"
    assert tmux.neutralize_inline_rig_lines(original) == original


def test_neutralize_is_idempotent():
    original = "if-shell '[ -n \"$MOSHI_CLIENT\" ]' { set -g status-right '' }\nset -g mouse on\n"
    once = tmux.neutralize_inline_rig_lines(original)
    twice = tmux.neutralize_inline_rig_lines(once)
    assert once == twice
