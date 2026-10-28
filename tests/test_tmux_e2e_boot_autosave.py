"""Real-tmux e2e: slow-boot restore, hostile boot dir, autosave under launchd env, boot cleanup,
socket teardown and migrated-conf parse checks (split from test_tmux_e2e.py)."""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from riglib import tmux as tmod
from riglib.actions import runner
from riglib.actions import tmux as tmux_mod
from riglib.actions import service_ctl as service_ctl_mod
from tests.tmux_e2e_helpers import (
    tmux_env,
    _requires_tmux_e2e,
    _requires_tmux,
    _socket_path_for,
    _teardown_tmux_server,
    _action,
    _apply_with_real_plugins,
)


_SLOW_CONF_LOAD_SECONDS = 12  # comfortably past continuum's 10 s default window


def _install_ps_shim(bindir: Path) -> None:
    """A `ps` on PATH that hides every `tmux…` process. continuum's restore refuses to run when
    it counts OTHER tmux processes of this uid (another server → assume no restore wanted); on a
    developer machine that is the dev's own live tmux, which has nothing to do with this test."""
    real = shutil.which("ps")
    shim = bindir / "ps"
    shim.write_text(f'#!/usr/bin/env bash\n{real} "$@" | {{ grep -v "^tmux" || true; }}\n', encoding="utf-8")
    shim.chmod(0o755)


def _write_resurrect_snapshot(home: Path) -> list[str]:
    """A synthetic resurrect snapshot — session `main`, three windows, five panes — with `last`
    pointing at it. Returns the window name of every pane, sorted (what a full restore yields)."""
    layout = {"rig674-a": 2, "rig674-b": 1, "rig674-c": 2}
    rows = [["window", "main", str(i), f":{name}", "0", ":", "tiled", "off"] for i, name in enumerate(layout, 1)]
    for i, (name, panes) in enumerate(layout.items(), 1):
        rows += [
            ["pane", "main", str(i), "0", ":", str(p), f"{name}-{p}", f":{home}", "1", "bash", ":bash"]
            for p in range(panes)
        ]
    rows.append(["state", "main", "main"])
    resurrect_dir = home / ".tmux" / "resurrect"
    resurrect_dir.mkdir(parents=True, exist_ok=True)
    snapshot = resurrect_dir / "tmux_resurrect_20261001T120000.txt"
    snapshot.write_text("".join("\t".join(r) + "\n" for r in rows), encoding="utf-8")
    (resurrect_dir / "last").symlink_to(snapshot.name)
    return sorted(name for name, panes in layout.items() for _ in range(panes))


def _restored_pane_windows(run) -> list[str]:
    out = run(["tmux", "list-panes", "-a", "-F", "#{window_name}"]).stdout
    return sorted(name for name in out.split() if name.startswith("rig674-"))


def _server_age_seconds(run) -> float:
    return time.time() - int(run(["tmux", "display-message", "-p", "#{start_time}"]).stdout.strip())


def _wait_until_no_server(run, timeout_s=10) -> None:
    deadline = time.time() + timeout_s
    while run(["tmux", "ls"]).returncode == 0 and time.time() < deadline:
        time.sleep(0.2)


@_requires_tmux_e2e
def test_slow_boot_conf_load_still_restores_the_saved_panes_and_a_live_source_file_does_not(
    tmux_env, monkeypatch, tmp_path
):
    """The boot-script path restores the saved panes even when the conf load outlasts continuum's
    10 s window, while a plain `tmux source-file` on a server that is already live (older than
    10 s, younger than the widened 10 min) restores nothing — the widened window must not outlive
    the boot. Fails without the boot marker: the boot leaves a bare `main`."""
    home, _socket, run = tmux_env
    res, _loads = _apply_with_real_plugins(
        home, monkeypatch,
        cc_restore={"enabled": False}, omp_restore={"enabled": False}, codex_restore={"enabled": False},
    )
    assert res.status in ("created", "backed_up"), res.detail
    _install_ps_shim(tmp_path / "bin")
    expected = _write_resurrect_snapshot(home)
    gen = home / ".config" / "rig" / "tmux"
    boot_script = gen / "tmux-boot.sh"
    tmux_conf = home / ".tmux.conf"
    managed_conf = tmux_conf.read_text(encoding="utf-8")

    # 1. the reboot shape: the conf load (tmux's own `~/.tmux.conf`, ahead of rig's import) is slow.
    tmux_conf.write_text(f'run-shell "sleep {_SLOW_CONF_LOAD_SECONDS}"\n{managed_conf}', encoding="utf-8")
    boot = run([str(boot_script)], timeout=120)
    assert boot.returncode == 0, boot.stderr
    assert _server_age_seconds(run) > 10, "the conf load finished inside continuum's window: proves nothing"
    deadline = time.time() + 20
    while _restored_pane_windows(run) != expected and time.time() < deadline:
        time.sleep(0.5)
    assert _restored_pane_windows(run) == expected, (
        "slow boot did not restore the saved panes; server has: "
        + run(["tmux", "list-windows", "-a", "-F", "#{session_name}:#{window_index}:#{window_name}"]).stdout
    )
    assert not (gen / tmod.BOOT_MARKER_NAME).exists(), "the boot script left its marker behind"

    # 2. a live server (past 10 s) re-sourcing the plain conf must restore nothing: drop two
    # restored windows, re-source, and they must stay gone.
    for name in ("rig674-b", "rig674-c"):
        assert run(["tmux", "kill-window", "-t", f"main:{name}"]).returncode == 0
    tmux_conf.write_text(managed_conf, encoding="utf-8")
    assert 10 < _server_age_seconds(run) < 600
    assert run(["tmux", "source-file", str(tmux_conf)]).returncode == 0
    time.sleep(5)  # continuum's restore (when it fires) sleeps 1 s, then rebuilds the missing windows
    assert _restored_pane_windows(run) == ["rig674-a", "rig674-a"], "source-file on a live server restored windows"
    assert run(["tmux", "show-options", "-gqv", "@continuum-restore-max-delay"]).stdout.strip() == ""


@_requires_tmux
def test_boot_window_if_shell_parses_and_follows_the_marker_for_a_hostile_generated_dir(
    tmux_env, live_boot_script_pid
):
    """The generated `if-shell` survives a generated_dir with a space, quotes, `$` and `#{…}` in it
    (tmux would otherwise expand or split them), and widens the window for a fresh marker of a
    LIVE boot script only — not for a stale one, one whose boot script was SIGKILLed before it
    could clean up (dead pid, or a pid since reused by an unrelated process), nor for none."""
    home, _socket, run = tmux_env
    gen = home / "gen dir 'q' \"dq\" $HOME #{x}"
    gen.mkdir()
    plan = tmod.build_tmux(repo_home=home, generated_dir=gen)
    probe = home / "probe.conf"
    probe.write_text(
        next(ln for ln in plan.render_rig_conf().splitlines() if ln.startswith("if-shell")) + "\n", encoding="utf-8"
    )

    def window_after_boot_with_marker(minutes, owner_pid=live_boot_script_pid) -> str:
        if minutes is None:
            plan.boot_marker_path.unlink(missing_ok=True)
        else:
            plan.boot_marker_path.write_text(f"1 {owner_pid}\n", encoding="utf-8")
            stamp = time.time() - minutes * 60
            os.utime(plan.boot_marker_path, (stamp, stamp))
        r = run(["tmux", "-f", str(probe), "new-session", "-d", "-s", "probe"])
        assert r.returncode == 0, r.stderr
        value = run(["tmux", "show-options", "-gqv", "@continuum-restore-max-delay"]).stdout.strip()
        run(["tmux", "kill-server"])
        _wait_until_no_server(run)
        return value

    dead = subprocess.Popen(["true"])
    dead.wait()
    assert window_after_boot_with_marker(None) == ""
    assert window_after_boot_with_marker(0) == "600"
    assert window_after_boot_with_marker(11) == ""
    assert window_after_boot_with_marker(0, owner_pid=dead.pid) == ""
    assert window_after_boot_with_marker(0, owner_pid=os.getpid()) == ""


# ── THE launchd-minimal-PATH regression guard (the exact root cause of the month-long death) ──
# The month-long silent death: the tmux SERVER (and every `run-shell`/`#()` hook child) inherits
# launchd's minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin — NO /opt/homebrew/bin), so continuum's
# BARE `tmux` calls fail and autosave never arms. EVERY prior fix stayed invisible because the
# existing e2e runs the artifacts under the HARNESS's FULL PATH (which includes /opt/homebrew/bin),
# so the bug could not reproduce (brainstorm 2026-07-13: Fable + Sol, unanimous). These two tests
# close that acceptance hole for the INDEPENDENT autosave saver (#138): they run the generated
# wrapper under `env -i` + EXACTLY the rendered plist's EnvironmentVariables — the real launchd
# trust boundary — and prove (positive) it still resolves tmux and writes a valid snapshot, and
# (negative, darwin-only) that STRIPPING the plist's PATH injection reproduces the exact failure,
# so the positive assertion is not vacuously green.
def _start_rich_session(run) -> None:
    """Bring a private-socket server up with a non-degenerate layout (a session with a second
    window) so the wrapper's degenerate-save guard does NOT skip — we want a REAL snapshot."""
    run(["tmux", "new-session", "-d", "-s", "main", "tail -f /dev/null"])
    run(["tmux", "new-window", "-t", "main", "tail -f /dev/null"])


def _plist_env(home) -> dict[str, str]:
    """The EnvironmentVariables the autosave LaunchAgent's plist injects — the ONLY environment
    launchd hands the agent. Built from the SAME renderer apply uses (Path.home is monkeypatched to
    the throwaway home, and _resolve_tmux_bin to the shim, so the plist PATH leads with the shim
    dir → bare tmux resolves to the private socket, never the dev's default server)."""
    plan = tmod.build_tmux(repo_home=home)
    return plistlib.loads(plan.render_autosave_plist().encode("utf-8"))["EnvironmentVariables"]


@_requires_tmux_e2e
def test_autosave_wrapper_saves_under_launchd_minimal_env(tmux_env, monkeypatch):
    """POSITIVE regression guard: the independent autosave wrapper, launched with `env -i` plus
    ONLY the rendered plist's EnvironmentVariables (the real launchd env — a minimal PATH the
    plist makes Homebrew-inclusive, HOME, a UTF-8 LANG), still resolves tmux from inside resurrect's
    BARE-`tmux` save.sh and writes a valid snapshot + a health record. This is the exact scenario
    every prior fix's test could not reproduce (it ran under the harness's full PATH)."""
    home, socket, run = tmux_env
    _apply_with_real_plugins(home, monkeypatch)
    gen = home / ".config" / "rig" / "tmux"
    wrapper = gen / "tmux-autosave.sh"
    assert wrapper.is_file() and os.access(wrapper, os.X_OK), "autosave wrapper not generated"

    _start_rich_session(run)

    env = _plist_env(home)
    # `env -i` = a CLEAN slate; only the plist keys survive — precisely what launchd gives the agent.
    r = subprocess.run(
        ["env", "-i", f"PATH={env['PATH']}", f"HOME={env['HOME']}", f"LANG={env['LANG']}", str(wrapper)],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0, f"wrapper failed under launchd-minimal env: rc={r.returncode}\n{r.stdout}\n{r.stderr}"

    resurrect_dir = home / ".tmux" / "resurrect"
    snaps = list(resurrect_dir.glob("tmux_resurrect_*.txt"))
    assert snaps, f"no snapshot written under launchd-minimal env (the month-long-death scenario): {r.stdout}"
    # a real (non-degenerate) snapshot: resurrect records the panes we started.
    assert any(s.stat().st_size > 0 for s in snaps), "snapshot is empty (locale/PATH corruption?)"
    # the health record proves the saver reported success — the observability half of #138.
    health = gen / "tmux-autosave-health.json"
    assert health.is_file(), "wrapper wrote no health record"
    assert json.loads(health.read_text())["result"] == "ok", health.read_text()


@_requires_tmux_e2e
@pytest.mark.skipif(sys.platform != "darwin", reason="the launchd-minimal-PATH bug is macOS-specific; on Linux tmux lives in /usr/bin (inside the minimal PATH), so this negative control can't isolate")
def test_autosave_wrapper_fails_without_plist_path_injection(tmux_env, monkeypatch):
    """NEGATIVE teeth-check (darwin-only): strip the plist's PATH injection — run the SAME wrapper
    under the RAW launchd-minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin, no Homebrew) — and the save
    MUST fail exactly as it did for a month: resurrect's bare `tmux` is unresolvable, so no snapshot
    is written and the wrapper records a failure. This proves the positive test above is not
    vacuously green (the plist PATH injection is load-bearing). On macOS /usr/bin has no tmux, so
    the wrapper's absolute TMUX_BIN passes the guard but resurrect's own bare `tmux` cannot."""
    home, socket, run = tmux_env
    _apply_with_real_plugins(home, monkeypatch)
    gen = home / ".config" / "rig" / "tmux"
    wrapper = gen / "tmux-autosave.sh"
    _start_rich_session(run)

    r = subprocess.run(
        ["env", "-i", "PATH=/usr/bin:/bin:/usr/sbin:/sbin", f"HOME={home}", "LANG=en_US.UTF-8", str(wrapper)],
        capture_output=True, text=True, timeout=30,
    )
    resurrect_dir = home / ".tmux" / "resurrect"
    snaps = list(resurrect_dir.glob("tmux_resurrect_*.txt"))
    # the whole point: WITHOUT the Homebrew PATH injection, no valid snapshot lands.
    assert not snaps or all(s.stat().st_size == 0 for s in snaps), (
        f"a snapshot WAS written under the raw minimal PATH — the negative control is broken "
        f"(is tmux resolvable in /usr/bin here?): rc={r.returncode} snaps={snaps}"
    )
    assert r.returncode != 0, f"wrapper unexpectedly succeeded under the raw minimal PATH: {r.stdout}"


@_requires_tmux
def test_old_continuum_boot_cleanup_removes_stale_entries(tmux_env, monkeypatch):
    """DEFECT 5: a pre-existing stale continuum boot (its osx_disable.sh + an old Tmux.Start
    launch agent) is cleaned by the activation. We MOCK their presence and assert removal.

    Gated ``@_requires_tmux`` (tmux-only), NOT the network gate: this test clones NOTHING and hits
    no network — it fabricates the stale files, MOCKS ``_launchctl``, and calls
    ``_clean_stale_continuum_boot`` directly. It only needs the ``tmux_env`` fixture (which installs
    a tmux shim and tears a private server down, so it needs tmux on PATH but no GitHub). Running it
    in default hermetic CI catches a DEFECT-5 cleanup regression on every PR, not only opt-in runs."""
    home, socket, run = tmux_env
    # simulate continuum's stale boot: an osx_disable.sh under the plugin + a Tmux.Start plist.
    cont = home / ".tmux" / "plugins" / "tmux-continuum" / "scripts"
    cont.mkdir(parents=True)
    disable_ran = home / "disable-ran"
    (cont / "osx_disable.sh").write_text(
        f"#!/usr/bin/env bash\ntouch {disable_ran}\n", encoding="utf-8"
    )
    (cont / "osx_disable.sh").chmod(0o755)
    la = home / "Library" / "LaunchAgents"
    la.mkdir(parents=True)
    old_plist = la / "Tmux.Start.plist"
    old_plist.write_text("<plist></plist>\n", encoding="utf-8")

    boot_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(service_ctl_mod, "_launchctl", lambda verb, arg: boot_calls.append((verb, arg)) or 0)
    plan = tmod.build_tmux(repo_home=home)
    cleaned = tmux_mod._clean_stale_continuum_boot(plan)

    assert cleaned is True
    # continuum's documented disable script was run …
    assert disable_ran.is_file(), "osx_disable.sh was not executed"
    # … the old Tmux.Start plist was removed …
    assert not old_plist.exists(), "stale Tmux.Start.plist not removed"
    # … and we issued a bootout/unload for it.
    assert any("Tmux.Start" in arg or "Tmux.Start" in verb for verb, arg in boot_calls), boot_calls
    # idempotent: a second run (now nothing present) cleans nothing and doesn't error.
    assert tmux_mod._clean_stale_continuum_boot(plan) is False


# ── socket-leak regression (the leak in the module docstring: ~185 rigtest-* sockets) ───────
# Gated on `_requires_tmux` (tmux-present ONLY), NOT the full `_requires_tmux_e2e` network gate:
# this clones nothing and hits no network, so coupling it to RIG_TMUX_E2E + GitHub-reachability
# would hide the very regression that guards the leak which killed the dev's server (INCIDENT
# 2026-06-17 follow-up a). With this marker it runs in default hermetic CI whenever tmux exists.
@_requires_tmux
def test_teardown_unlinks_the_private_socket_file():
    """REGRESSION for the socket leak described in the module docstring: ``_teardown_tmux_server``
    must leave NO socket file on disk (``kill-server`` alone leaks the inode on macOS). Drives the
    SAME teardown the fixture uses, against a REAL server, and asserts the file — not just the
    process — is gone."""
    real_tmux = shutil.which("tmux")
    # The `@_requires_tmux` marker already guarantees tmux is on PATH; assert so that if it were
    # ever absent at runtime (marker bypassed / tmux removed mid-session) we fail with a clear
    # message instead of a confusing `None`-path TypeError further down.
    assert real_tmux, "tmux must be on PATH (guarded by @_requires_tmux)"
    label = f"rigtest-leakcheck-{uuid.uuid4().hex[:8]}"

    # scrub TMUX_TMPDIR/TMUX so the server lands at the default /tmp path, and resolve the socket
    # path under that SAME env — the whole bug being guarded is an env/path mismatch here.
    env = dict(os.environ)
    env.pop("TMUX", None)
    env.pop("TMUX_TMPDIR", None)
    sock = _socket_path_for(label, env)
    try:
        boot = subprocess.run(
            [real_tmux, "-L", label, "new-session", "-d", "-s", "probe", "tail -f /dev/null"],
            env=env, capture_output=True, text=True, timeout=15,
        )
        assert boot.returncode == 0, boot.stderr
        assert sock.exists(), f"server did not create its socket at {sock}"

        _teardown_tmux_server(real_tmux, label, env)

        assert not sock.exists(), f"LEAK: socket file lingered after teardown: {sock}"
    finally:
        # belt-and-suspenders if an assert fired mid-way — reuse the production teardown.
        _teardown_tmux_server(real_tmux, label, env)


# ── migrated-conf parse validity (the legacy-init neutralization fix, 2026-06-18) ───────────
# Gated `@_requires_tmux` (tmux-present ONLY, no network, no opt-in): it clones nothing and
# fabricates everything in a throwaway HOME, so it runs in default hermetic CI and guards the
# neutralization fix on every PR — a future change that produces a syntactically broken migrated
# ~/.tmux.conf (e.g. a mangled `# rig-migrated:` comment, an unbalanced if-shell brace) fails
# LOUDLY here instead of only on a live machine.
@_requires_tmux
def test_migrated_conf_with_neutralized_legacy_init_parses(tmux_env, monkeypatch):
    """A hand-written conf carrying the FULL old tpm/resurrect/continuum init + personal prefs is
    migrated (import mode) by the REAL runner, then loaded by a REAL tmux on a private socket:
    `tmux -L <iso> -f <migrated ~/.tmux.conf> new-session -d` must exit 0 (the neutralized lines
    are valid comments, the surviving prefs + source-file parse cleanly).

    @continuum-restore is forced OFF in the generated config (continuum.restore=False) so even if
    the (absent) continuum plugin's run-shell were reachable, NOTHING restores into the throwaway
    server. login_shell + boot are off to keep the load self-contained. Teardown kills the server
    AND unlinks the socket (the fixture's `finally`)."""
    home, socket, run = tmux_env
    real_tmux = shutil.which("tmux")
    assert real_tmux, "tmux must be on PATH (guarded by @_requires_tmux)"

    conf = home / ".tmux.conf"
    conf.write_text(
        "set -g mouse on\n"
        "set -g history-limit 100000\n"
        "set -g @plugin 'tmux-plugins/tmux-sensible'\n"   # third-party — survives, but no plugin dir
        "set -g @plugin 'tmux-plugins/tpm'\n"
        "set -g @plugin 'tmux-plugins/tmux-resurrect'\n"
        "set -g @plugin 'tmux-plugins/tmux-continuum'\n"
        "set -g @resurrect-processes 'ssh psql ~rails'\n"
        "set -g @continuum-restore 'on'\n"
        "set -g @continuum-boot 'on'\n"
        "run-shell ~/.tmux/plugins/tmux-resurrect/resurrect.tmux\n"
        "run-shell ~/.tmux/plugins/tmux-continuum/continuum.tmux\n"
        "if-shell '[ -n \"$MOSHI_CLIENT\" ]' { set -g status-right '' }\n"
        # a rig-owned option a user GUARDED inside a NESTED non-Moshi conditional — left LIVE by
        # migration (under-reach); the resulting `{ { … } }` must still PARSE. (Proves the
        # structural brace-skip never leaves a dangling/empty brace that breaks the load, including
        # nested braces where a boolean flag would have re-exposed the outer block — review.)
        "if-shell '[ -n \"$SOME_FLAG\" ]' {\n"
        "  if-shell '[ -n \"$OTHER\" ]' {\n"
        "    set -g @continuum-save-interval '7'\n"
        "  }\n"
        "  set -g @resurrect-strategy-vim 'session'\n"
        "}\n"
        "run '~/.tmux/plugins/tpm/tpm'\n",
        encoding="utf-8",
    )

    # migrate via the REAL runner (no live activation: dry-run + boot/login_shell off). With
    # continuum.restore off and login_shell off, the generated rig.tmux.conf neither restores nor
    # bakes a default-command, so loading it is side-effect-free in the throwaway server.
    monkeypatch.setenv("RIG_TMUX_DRY_RUN", "1")
    action = _action(
        home,
        boot={"enabled": False},
        login_shell={"enabled": False},
        continuum={"restore": False},
        anti_sprawl={"enabled": False},
    )
    runner._do_provision_tmux(action, "backup")

    migrated = conf.read_text()
    # sanity: the legacy continuum init is neutralized (commented), the pref survives live.
    assert "# rig-migrated (now in rig.tmux.conf): run-shell ~/.tmux/plugins/tmux-continuum" in migrated
    assert any(
        ln.strip() == "set -g mouse on" for ln in migrated.splitlines()
    ), "personal pref must survive migration"

    # load the migrated user conf in a REAL tmux on the private socket — must parse with exit 0.
    res = run([
        "tmux", "-L", socket, "-f", str(conf),
        "new-session", "-d", "-s", "parsecheck", "tail -f /dev/null",
    ])
    assert res.returncode == 0, (
        f"migrated ~/.tmux.conf failed to parse (rc={res.returncode}):\n"
        f"STDERR:\n{res.stderr}\nCONF:\n{migrated}"
    )
    # the server came up with the session — a parse abort would have left no server.
    listed = run(["tmux", "-L", socket, "list-sessions"])
    assert "parsecheck" in listed.stdout, listed.stdout + listed.stderr


@_requires_tmux
def test_comment_only_brace_body_after_moshi_neutralize_parses(tmux_env):
    """A Moshi wipe block nested inside an OUTER user conditional: migration comments the inner
    Moshi block WHOLE, leaving the outer `{ … }` around a COMMENT-ONLY body. tmux must still parse
    `{ # … }` — proven against a REAL tmux on a private socket, not only on a live machine. This is
    the one structural shape the unit suite can't fully vouch for (it can't run the tmux parser)."""
    home, socket, run = tmux_env
    real_tmux = shutil.which("tmux")
    assert real_tmux, "tmux must be on PATH (guarded by @_requires_tmux)"

    conf = home / ".tmux.conf"
    raw = (
        "set -g mouse on\n"
        "if-shell '[ -n \"$OUTER\" ]' {\n"
        "  if-shell '[ -n \"$MOSHI_CLIENT\" ]' {\n"
        "    set -g status-right ''\n"
        "  }\n"
        "}\n"
    )
    conf.write_text(raw, encoding="utf-8")
    # neutralize via the pure helper (the same one apply uses), then load the RESULT in tmux.
    migrated = tmod.neutralize_inline_rig_lines(raw)
    conf.write_text(migrated, encoding="utf-8")
    # sanity: the inner Moshi block (incl. its braces) is commented; the OUTER braces stay live →
    # a comment-only body inside `{ … }`.
    lines = migrated.splitlines()
    assert any(ln.strip() == "if-shell '[ -n \"$OUTER\" ]' {" for ln in lines), "outer brace live"
    assert not any(
        "status-right ''" in ln and not ln.lstrip().startswith("#") for ln in lines
    ), "the inner Moshi wipe must be commented"
    assert any(
        ln.lstrip().startswith(tmod.NEUTRALIZE_PREFIX) and "status-right ''" in ln for ln in lines
    ), "the wipe survives as a rig-migrated comment inside the live outer braces"

    res = run([
        "tmux", "-L", socket, "-f", str(conf),
        "new-session", "-d", "-s", "braceparse", "tail -f /dev/null",
    ])
    assert res.returncode == 0, (
        f"comment-only-brace-body conf failed to parse (rc={res.returncode}):\n"
        f"STDERR:\n{res.stderr}\nCONF:\n{migrated}"
    )
    listed = run(["tmux", "-L", socket, "list-sessions"])
    assert "braceparse" in listed.stdout, listed.stdout + listed.stderr
