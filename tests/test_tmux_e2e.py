"""REAL tmux e2e — the acceptance test for the rig-tmux-v2 reboot-cycle fix.

What this is
------------
The #24/#26 tmux provisioning passed unit + tmux parse-check but the LIVE reboot cycle
(apply -> save -> REBOOT -> restore) broke on a real machine — multiple defects that a
pure-render/unit suite cannot catch. This file is the acceptance gate the CTO asked for: it
drives REAL ``tmux`` (a real server, real sessions, the actual generated scripts) in a throwaway
``$HOME`` and asserts the whole cycle works with ZERO manual steps. It is the proof that a
clean-machine ``rig apply`` leaves tmux persistence FULLY working.

How it is reached
-----------------
The plugin-cloning tests are OPT-IN (``@_requires_tmux_e2e`` → ``RIG_TMUX_E2E=1`` + tmux + git +
network); a plain ``pytest`` SKIPS them so default CI stays hermetic. The socket-leak REGRESSION
(``test_teardown_unlinks_the_private_socket_file``) is gated on ``@_requires_tmux`` instead
(tmux-present only, NO network) so it runs in default hermetic CI — it guards the leak that killed
the dev's server, so it must NOT be hidden behind the network gate (INCIDENT 2026-06-17, follow-up
a). Every tmux call goes through a PRIVATE ``-L <socket>`` (a per-test ``tmux`` shim on PATH injects
``-L``), so it NEVER touches the developer's real tmux server. A session-scoped teardown kills every
spawned server/socket. The unit suite already covers the render/plan/drift logic hermetically.

What it proves (maps 1:1 to the six defects)
--------------------------------------------
1. boot: the generated boot script (NOT a bare ``start-server``) brings a server UP with the
   config LOADED and a session present; ``rig apply`` ``launchctl load -w``s the agent (asserted
   on the artifact + the load call, since a test can't reboot).
2. cc-save: a FAKE ``claude`` child under a pane's shell makes cc-save write a NON-EMPTY
   cwd->id map (the old command-string filter wrote nothing); cc-restore would relaunch
   ``claude --resume <id>`` into a fresh shell pane.
3. login shell: the generated config sets a login-shell ``default-command``.
4. resurrect dir: ``~/.tmux/resurrect`` exists and a real ``.txt`` snapshot is written.
5. old-boot cleanup: the stale continuum Login Items / ``Tmux.Start`` agent are removed.
6. plugins: tpm + resurrect + continuum are installed so the ``@plugin`` decls resolve, and a
   first ``resurrect save`` lands a snapshot.

Invariants
----------
- PRIVATE socket only (``-L``). Never the default server. Teardown kills the server AND unlinks
  its socket file — ``kill-server`` ends the process but leaks the socket inode on macOS, which
  once accumulated ~185 ``rigtest-*`` files in ``/tmp/tmux-501/`` and starved the dev's server.
- The generated scripts are run UNMODIFIED (via the PATH ``tmux`` shim) — testing the real
  artifact rig writes, not a paraphrase of it.
- The per-test gating granularity (leak regression tmux-only, plugin tests opt-in network) is
  PINNED by ``tests/test_tmux_e2e_gating.py`` — a separate, tamper-proof guard so a re-added
  blanket ``pytestmark`` can never silently re-hide the leak regression (INCIDENT follow-up a).
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


from riglib import tmux as tmod
from tests.tmux_e2e_helpers import (
    tmux_env,
    _requires_tmux_e2e,
    _requires_tmux,
    _requires_abduco,
    _pane_text,
    _wait_for_restored_pane_text,
    _wait_for_claude_descendant,
    _apply_with_real_plugins,
)


@_requires_tmux_e2e
def test_clean_machine_apply_brings_tmux_up_with_config_and_session(tmux_env, monkeypatch):
    """DEFECTS 1/3/4/6: a clean-HOME apply installs plugins + scripts + config + boot agent, the
    boot script brings a REAL server up WITH the config loaded AND a session present, and the
    config sets a login-shell default-command."""
    home, socket, run = tmux_env
    res, loads = _apply_with_real_plugins(home, monkeypatch)
    assert res.status in ("created", "backed_up"), res.detail

    gen = home / ".config" / "rig" / "tmux"
    # DEFECT 6: plugins cloned so @plugin decls resolve.
    for name in ("tpm", "tmux-resurrect", "tmux-continuum"):
        assert (home / ".tmux" / "plugins" / name).is_dir(), f"{name} not installed"
    # DEFECT 4: resurrect snapshot dir exists.
    assert (home / ".tmux" / "resurrect").is_dir()
    # DEFECT 1: the boot agent plist points at the boot SCRIPT (not a bare start-server) and
    # rig launchctl-load-enabled it (the load call recorded; a test can't actually reboot).
    plist = home / "Library" / "LaunchAgents" / "ai.hyperide.tmux-boot.plist"
    if os.uname().sysname == "Darwin":
        assert plist.is_file()
        assert loads == [str(plist)], loads
    boot_script = gen / "tmux-boot.sh"
    assert boot_script.is_file() and os.access(boot_script, os.X_OK)

    # DEFECT 3: the generated config sets a login-shell default-command.
    conf_text = (gen / "rig.tmux.conf").read_text()
    assert "set -g default-command" in conf_text and "-l" in conf_text

    # the ~/.tmux.conf imports the generated file (so a new session loads it).
    assert f"source-file '{gen / 'rig.tmux.conf'}'" in (home / ".tmux.conf").read_text()

    # DEFECT 1 — EXECUTE the boot entrypoint (can't reboot in a test): it must bring a server UP
    # with the config LOADED and a session present. Run it via the shimmed PATH (private socket).
    r = run([str(boot_script)])
    assert r.returncode == 0, f"boot script failed: {r.stderr}"
    time.sleep(1.0)  # let the detached session + plugin inits settle before querying the server.
    # a server is now running with the canonical session.
    ls = run(["tmux", "ls"])
    assert ls.returncode == 0, f"`tmux ls` says no server: {ls.stderr or ls.stdout}"
    assert "main" in ls.stdout, ls.stdout
    # the CONFIG was actually loaded by that first session: a rig-set option is live on the server.
    # Use @continuum-restore (stable at 'on') rather than @continuum-save-interval — the latter is
    # now toggled by the independent-autosave feature (#138 sets it to 0 when the saver owns saving).
    opt = run(["tmux", "show-options", "-g", "@continuum-restore"])
    assert "on" in opt.stdout, f"config not loaded — continuum option absent: {opt.stdout!r} {opt.stderr!r}"
    # idempotent: a second boot does NOT create a duplicate session (anti-sprawl at boot).
    run([str(boot_script)])
    ls2 = run(["tmux", "ls"])
    assert ls2.stdout.count("main") == 1, f"boot spawned a duplicate session: {ls2.stdout}"


@_requires_tmux_e2e
def test_cc_save_populates_map_from_a_real_claude_child(tmux_env, monkeypatch):
    """DEFECT 2 (the headline reboot bug): a FAKE `claude` running as a CHILD of a pane's shell
    must make cc-save write a NON-EMPTY cwd->session-id map — the OLD `pane_current_command ==
    claude` filter wrote nothing because cc shows up as its version string, not `claude`."""
    home, socket, run = tmux_env
    _apply_with_real_plugins(home, monkeypatch)
    gen = home / ".config" / "rig" / "tmux"

    # A fake `claude` whose process `comm` reports `claude` (the production case: cc shows up as a
    # VERSION string in pane_current_command, the real `claude` is a CHILD). The process must have a
    # `comm` whose basename is `claude` on BOTH platforms — and the two single-trick approaches each
    # fail on one OS: `exec -a claude sleep` rewrites only argv[0] (Linux `comm` still reads `sleep`
    # → descendant invisible → CI failed); a COPY of the `sleep` binary won't run on macOS (SIP
    # refuses to exec an unsigned copy of a system binary). A SYMLINK named `claude` → the real
    # `sleep` works on both: the kernel sets `comm` from the invoked name, so `comm`'s basename is
    # `claude` on Linux AND macOS. Run by a LAUNCHER that keeps it a genuine child of the pane shell
    # (background it, the shell stays alive) — a bare send-keys `claude &` gets reparented by
    # job-control and detaches.
    work = home / "proj"
    work.mkdir()
    fake_claude = home / "fakebin" / "claude"
    fake_claude.parent.mkdir()
    real_sleep = shutil.which("sleep") or "/bin/sleep"
    fake_claude.symlink_to(real_sleep)  # symlink named `claude` → comm basename == claude on both OSes
    launcher = home / "launch.sh"
    launcher.write_text(
        f"#!/usr/bin/env bash\n{shlex.quote(str(fake_claude))} 300 &\nsleep 300\n", encoding="utf-8"
    )
    launcher.chmod(0o755)
    # the pane RUNS the launcher (so claude is a real descendant), in the known cwd.
    run(["tmux", "new-session", "-d", "-s", "main", "-c", str(work), str(launcher)])
    _wait_for_claude_descendant(run, timeout_s=10)

    # seed a Claude Code session file under the encoded projects dir for that cwd, so cc-save has
    # an id to record (encoding: every '/' and '.' -> '-').
    enc = str(work).replace("/", "-").replace(".", "-")
    proj = home / ".claude" / "projects" / enc
    proj.mkdir(parents=True)
    sid = "11111111-2222-3333-4444-555555555555"
    (proj / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")

    # RUN the real generated cc-save (via the shimmed tmux → private socket).
    r = run(["bash", str(gen / "cc-save.sh")])
    assert r.returncode == 0, f"cc-save failed: {r.stderr}"

    map_file = gen / "cc-sessions.map"
    assert map_file.is_file(), "cc-save wrote no map file"
    lines = [ln for ln in map_file.read_text().splitlines() if ln.strip()]
    assert lines, "DEFECT 2: cc-save map is EMPTY — the claude child was not detected via the tree"
    # the recorded line is <addr>\t<cwd>\t<session-id> for our claude pane.
    assert any(str(work) in ln and sid in ln for ln in lines), lines


def _poll_cc_save_for_pane(run, gen, map_file, work, sid, *, timeout_s=15):
    """Re-run the real cc-save until its map records OUR pane (cwd + sid) or the timeout lapses.

    Returns ``(found, last_map_lines)``."""
    deadline = time.time() + timeout_s
    lines: list[str] = []
    while time.time() < deadline:
        r = run(["bash", str(gen / "cc-save.sh")])
        assert r.returncode == 0, f"cc-save failed: {r.stderr}"
        # cc-save has finished writing (sequential — `run` returns before we read), so the read is
        # not racing a concurrent writer. Break ONLY when OUR versioned pane (this cwd + sid) is in
        # the map — not merely on any non-empty map: cc-save scans ALL panes, so an unrelated claude
        # process on the host (a parallel test, the dev's own session) could populate it without our
        # descendant yet being visible, and an early break would then flake the final assertion.
        lines = _read_map_lines(map_file)
        if any(str(work) in ln and sid in ln for ln in lines):
            return True, lines
        time.sleep(0.3)
    return False, lines


def _read_map_lines(map_file):
    if not map_file.is_file():
        return []
    return [ln for ln in map_file.read_text().splitlines() if ln.strip()]


@_requires_tmux_e2e
def test_cc_save_detects_the_versioned_binary_install(tmux_env, monkeypatch):
    """THE 2026-06-17 INCIDENT (versioned-binary half of DEFECT 2): cc installs as a symlink
    ``~/.local/bin/claude`` → ``…/claude/versions/<version>``. Launched by the RESOLVED path (not
    the ``claude`` symlink), the process's name is the VERSION string (``2.1.179``), NOT ``claude``
    — so a basename-only ``claude``/``*/claude`` match missed it and the map stayed empty (cc never
    resumed after a reboot, the live incident). cc-save must still detect it via the
    ``…/claude/versions/`` path arm of the tree-walk match (which reads ``ps -o args`` so the path
    is visible on both macOS and Linux — Linux ``comm`` is the truncated basename with no path).

    We reproduce the EXACT install shape: a ``claude/versions/<version>`` symlink → ``sleep`` run by
    its resolved PATH, as a real child of a pane shell, so its ``args`` (argv[0]) is
    ``…/claude/versions/<version>``. A SYMLINK (not a copy) is used so the launcher works on macOS
    too: macOS SIP refuses to exec an unsigned COPY of a protected system binary, but exec'ing a
    symlink to it is allowed, and ``args`` reflects the invoked path either way.
    """
    home, socket, run = tmux_env
    _apply_with_real_plugins(home, monkeypatch)
    gen = home / ".config" / "rig" / "tmux"

    work = home / "verproj"
    work.mkdir()
    version = "2.1.179"
    versions_dir = home / ".local" / "share" / "claude" / "versions"
    versions_dir.mkdir(parents=True)
    versioned = versions_dir / version
    real_sleep = shutil.which("sleep") or "/bin/sleep"
    versioned.symlink_to(real_sleep)  # argv[0] == the versioned path under claude/versions/
    launcher = home / "launch-ver.sh"
    # run the versioned binary BY ITS RESOLVED PATH (the failing production case), backgrounded so
    # it stays a genuine descendant of the launcher (which keeps the pane shell alive).
    launcher.write_text(
        f"#!/usr/bin/env bash\n{shlex.quote(str(versioned))} 300 &\nsleep 300\n", encoding="utf-8"
    )
    launcher.chmod(0o755)

    # seed a Claude Code session file for that cwd so cc-save has an id to record.
    enc = str(work).replace("/", "-").replace(".", "-")
    proj = home / ".claude" / "projects" / enc
    proj.mkdir(parents=True)
    sid = "99999999-8888-7777-6666-555555555555"
    (proj / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")

    run(["tmux", "new-session", "-d", "-s", "main", "-c", str(work), str(launcher)])
    # POLL the REAL generated cc-save until it records the pane (or time out). We assert on cc-save's
    # OWN output — the production acceptance criterion — instead of a separate `ps` probe, so the
    # test validates the exact production matcher on the exact platform (a flat `ps args` probe
    # diverged from how the shimmed-socket tree walk sees the pane and flaked on CI). This loop also
    # absorbs the launch→ps-visibility race. If cc-save can detect the versioned descendant, the map
    # is non-empty; if it can't (the regression / a real platform gap), the loop times out and fails.
    map_file = gen / "cc-sessions.map"
    found, lines = _poll_cc_save_for_pane(run, gen, map_file, work, sid)
    # the production matcher (the `.../claude/versions/` arm) found OUR versioned pane and recorded
    # its exact cwd→session-id. A timeout here is the INCIDENT regression (or a real platform gap).
    assert found, (
        "INCIDENT: cc-save did NOT record the VERSIONED-binary cc pane "
        f"(cwd={work}, sid={sid}); map lines={lines!r}"
    )


@_requires_tmux_e2e
def test_cc_restore_relaunches_claude_resume_into_fresh_shell(tmux_env, monkeypatch):
    """DEFECT 2 (restore half): with a seeded map, cc-restore sends `cd <cwd> && claude --resume
    <id>` into a FRESH shell pane (never on top of a running claude / an editor)."""
    home, socket, run = tmux_env
    _apply_with_real_plugins(home, monkeypatch)
    gen = home / ".config" / "rig" / "tmux"

    work = home / "proj"
    work.mkdir()
    # A FAKE `claude` on the pane's PATH so the resume that cc-restore types runs a harmless
    # sleep (NOT the real Claude Code, which would launch its onboarding TUI). `exec -a claude`
    # makes its `comm` report `claude`. We then assert the resume command was TYPED into the pane.
    fakebin = home / "fakebin"
    fakebin.mkdir()
    (fakebin / "claude").write_text(
        "#!/usr/bin/env bash\nexec -a claude sleep 300\n", encoding="utf-8"
    )
    (fakebin / "claude").chmod(0o755)
    # a fresh shell pane (bash, no rc) with the fake claude FIRST on PATH, in the known cwd.
    run(["tmux", "new-session", "-d", "-s", "main", "-c", str(work),
         f"PATH={fakebin}:$PATH exec bash --norc -i"])
    # resolve the real pane addr (window base-index may be 1, not 0).
    addr = run(["tmux", "list-panes", "-a", "-F",
                "#{session_name}:#{window_index}.#{pane_index}"]).stdout.splitlines()[0]
    # seed the projects session file (so the id is "live" → --resume, not --continue).
    enc = str(work).replace("/", "-").replace(".", "-")
    proj = home / ".claude" / "projects" / enc
    proj.mkdir(parents=True)
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    (proj / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")
    # seed the map cc-restore reads.
    (gen / "cc-sessions.map").write_text(f"{addr}\t{work}\t{sid}\n", encoding="utf-8")

    # RUN the real cc-restore: it must send `cd <cwd> && claude --resume <id>` into the pane.
    r = run(["bash", str(gen / "cc-restore.sh")])
    assert r.returncode == 0, f"cc-restore failed: {r.stderr}"
    time.sleep(0.5)
    text = _pane_text(run, addr)
    # the resume command line was typed into the fresh shell pane.
    assert "claude --resume" in text, text
    assert sid in text, text
    assert str(work) in text, text


@_requires_tmux
@_requires_abduco
def test_omp_restore_resumes_by_exact_path_when_no_orphan_is_live(tmux_env):
    """Positive control for the 2026-09-25 orphan guard below: with NO other omp process alive
    anywhere for the mapped cwd, omp-restore still relaunches normally — the new system-wide
    `lsof` check must never produce a false positive against the pane's OWN fresh shell (which
    is not yet running omp at all) or an unrelated cwd. "Relaunches" is proven end to end: the
    fake omp records the argv it was started with, so the typed `rig harness run omp` must really reach
    omp through abduco with the exact saved session path."""
    home, socket, run = tmux_env
    plan = tmod.build_tmux(repo_home=home)
    gen = plan.generated_dir
    gen.mkdir(parents=True, exist_ok=True)
    (gen / "omp-restore.sh").write_text(plan.render_omp_restore(), encoding="utf-8")
    (gen / "omp-restore.sh").chmod(0o755)

    work = home / "proj"
    work.mkdir()
    fakebin = home / "fakebin"
    fakebin.mkdir()
    argv_log = home / "omp-argv.log"
    # write-then-rename: the poll below keys on the file existing, so it must never see it empty.
    log_q = shlex.quote(str(argv_log))
    (fakebin / "omp").write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {log_q}.tmp && mv {log_q}.tmp {log_q}\n", encoding="utf-8"
    )
    (fakebin / "omp").chmod(0o755)
    # abduco's socket is <dir>/abduco/<user>/<session>@<host>; under the pytest tmp dir that
    # exceeds the unix-socket path limit (~104 bytes on macOS) and abduco fails with "File name
    # too long" — keep its dir short.
    durable = Path(tempfile.mkdtemp(prefix="rigab-", dir="/tmp"))
    try:
        run(["tmux", "new-session", "-d", "-s", "main", "-c", str(work),
             f"PATH={fakebin}:$PATH RIG_OMP_ABDUCO_DIR={durable} exec bash --norc -i"])
        addr = run(["tmux", "list-panes", "-a", "-F",
                    "#{session_name}:#{window_index}.#{pane_index}"]).stdout.splitlines()[0]

        sf = home / "fake-session.jsonl"
        sf.write_text(json.dumps({"type":"session","id":"test-no-orphan","cwd":str(work)})+"\n", encoding="utf-8")
        (gen / "omp-sessions.map").write_text(f"{addr}\t{work}\t{sf}\n", encoding="utf-8")

        r = run(["bash", str(gen / "omp-restore.sh")])
        assert r.returncode == 0, f"omp-restore failed: {r.stderr}"
        text = _wait_for_restored_pane_text(run, addr, argv_log)
        assert "rig harness run omp --host-id omp-recover-" in text, text
        assert argv_log.exists(), f"omp was never launched in the restored pane:\n{text}"
        assert argv_log.read_text(encoding="utf-8").splitlines() == ["--resume", str(sf)]
        assert not (gen / "omp-restore-warnings.log").exists()
    finally:
        shutil.rmtree(durable, ignore_errors=True)


@_requires_tmux
def test_omp_restore_skips_and_warns_when_omp_already_live_for_cwd(tmux_env):
    """REGRESSION (2026-09-25 duplicate-session incident): tmux's server crashed and stayed
    down for ~17h with no automatic recovery; the user, finding tmux dead, started a REPLACEMENT
    omp directly in a plain terminal tty — never inside any tmux pane. When tmux was finally
    restored, resurrect recreated the old pane as a bare fresh shell, and the OLD pane-only
    "don't clobber a running omp" guard — `pane_current_command` on that ONE pane — was
    structurally blind to the orphan and would have launched a SECOND, independent
    `omp --resume` for the identical cwd. This drives the real fix: omp-restore.sh must instead
    detect the orphan (a live `omp` process, cwd-matched via `lsof`, that is NOT in any tmux
    pane at all) and skip + WARN instead of duplicating it."""
    home, socket, run = tmux_env
    plan = tmod.build_tmux(repo_home=home)
    gen = plan.generated_dir
    gen.mkdir(parents=True, exist_ok=True)
    (gen / "omp-restore.sh").write_text(plan.render_omp_restore(), encoding="utf-8")
    (gen / "omp-restore.sh").chmod(0o755)

    work = home / "proj"
    work.mkdir()

    # the orphan: a background `omp` process whose cwd is `work`, started OUTSIDE any tmux pane
    # — exactly the incident's shape (a plain terminal session tmux can never see).
    orphan = subprocess.Popen(["bash", "-c", "exec -a omp sleep 300"], cwd=str(work))
    try:
        deadline = time.time() + 10
        seen = False
        while time.time() < deadline:
            probe = subprocess.run(
                ["lsof", "-a", "-p", str(orphan.pid), "-d", "cwd", "-Fn"],
                capture_output=True, text=True, timeout=5,
            )
            if str(work) in probe.stdout:
                seen = True
                break
            time.sleep(0.2)
        assert seen, "fake orphan omp process never became lsof-visible within timeout"

        # a fresh shell pane resurrect "recreated" — unrelated dir, distinct from the orphan.
        run(["tmux", "new-session", "-d", "-s", "main", "-c", str(home), "bash --norc -i"])
        addr = run(["tmux", "list-panes", "-a", "-F",
                    "#{session_name}:#{window_index}.#{pane_index}"]).stdout.splitlines()[0]

        sf = home / "fake-session.jsonl"
        sf.write_text(json.dumps({"type":"session","id":"test-orphan","cwd":str(work)})+"\n", encoding="utf-8")
        # the map records the ORPHAN's cwd for this pane (mirrors the incident: the saved
        # snapshot's pane cwd is the project dir the orphan is now independently working in).
        (gen / "omp-sessions.map").write_text(f"{addr}\t{work}\t{sf}\n", encoding="utf-8")

        r = run(["bash", str(gen / "omp-restore.sh")])
        assert r.returncode == 0, f"omp-restore failed: {r.stderr}"
        time.sleep(0.5)
        text = _pane_text(run, addr)
        # it must NOT have launched a second, independent omp for that cwd.
        assert "omp --resume" not in text, text
        assert "omp --continue" not in text, text
        # it must have visibly warned in the pane instead — impossible to miss.
        assert "unidentified_live_owner" in text, text
        assert str(orphan.pid) in text, text
        # and left an audit trail on disk.
        warn_log = gen / "omp-restore-warnings.log"
        assert warn_log.exists()
        log_text = warn_log.read_text()
        assert str(work) in log_text
        assert str(orphan.pid) in log_text
    finally:
        orphan.kill()
        orphan.wait(timeout=5)


@_requires_tmux
def test_omp_restore_skips_and_warns_for_every_live_omp_cwd_with_multiple_orphans(tmux_env):
    """REGRESSION: `LIVE_OMP_CWDS` used to be built as
    `LIVE_OMP_CWDS="$LIVE_OMP_CWDS$(printf '%s\\t%s\\n' "$pid" "$pcwd")"` — command substitution
    strips ALL trailing newlines, so as soon as a SECOND live `omp` process was found its record
    glued onto the first with no separator (`"111\\t/a" + "222\\t/b"` -> `"111\\t/a222\\t/b"`),
    corrupting the awk lookup so BOTH cwds (not just the second) could silently miss and the
    orphan launched anyway. With two real orphans in two distinct cwds, every mapped pane whose
    cwd matches a live orphan must be skipped + warned, not just the first one found."""
    home, socket, run = tmux_env
    plan = tmod.build_tmux(repo_home=home)
    gen = plan.generated_dir
    gen.mkdir(parents=True, exist_ok=True)
    (gen / "omp-restore.sh").write_text(plan.render_omp_restore(), encoding="utf-8")
    (gen / "omp-restore.sh").chmod(0o755)

    work_a = home / "proj-a"
    work_b = home / "proj-b"
    work_a.mkdir()
    work_b.mkdir()

    # two orphans, two distinct cwds — both started outside any tmux pane.
    orphan_a = subprocess.Popen(["bash", "-c", "exec -a omp sleep 300"], cwd=str(work_a))
    orphan_b = subprocess.Popen(["bash", "-c", "exec -a omp sleep 300"], cwd=str(work_b))
    try:
        for orphan, work in ((orphan_a, work_a), (orphan_b, work_b)):
            deadline = time.time() + 10
            seen = False
            while time.time() < deadline:
                probe = subprocess.run(
                    ["lsof", "-a", "-p", str(orphan.pid), "-d", "cwd", "-Fn"],
                    capture_output=True, text=True, timeout=5,
                )
                if str(work) in probe.stdout:
                    seen = True
                    break
                time.sleep(0.2)
            assert seen, f"fake orphan omp process (cwd={work}) never became lsof-visible"

        # two fresh shell panes resurrect "recreated", one per orphan's cwd.
        run(["tmux", "new-session", "-d", "-s", "main", "-c", str(home), "bash --norc -i"])
        run(["tmux", "new-window", "-t", "main", "-c", str(home), "bash --norc -i"])
        addrs = run(["tmux", "list-panes", "-a", "-F",
                     "#{session_name}:#{window_index}.#{pane_index}"]).stdout.splitlines()
        assert len(addrs) >= 2, addrs
        addr_a, addr_b = addrs[0], addrs[1]

        sf_a = home / "fake-session-a.jsonl"
        sf_b = home / "fake-session-b.jsonl"
        sf_a.write_text(json.dumps({"type":"session","id":"test-orphan-a","cwd":str(work_a)})+"\n", encoding="utf-8")
        sf_b.write_text(json.dumps({"type":"session","id":"test-orphan-b","cwd":str(work_b)})+"\n", encoding="utf-8")
        (gen / "omp-sessions.map").write_text(
            f"{addr_a}\t{work_a}\t{sf_a}\n{addr_b}\t{work_b}\t{sf_b}\n", encoding="utf-8"
        )

        r = run(["bash", str(gen / "omp-restore.sh")])
        assert r.returncode == 0, f"omp-restore failed: {r.stderr}"
        time.sleep(0.5)

        for addr, work, orphan in ((addr_a, work_a, orphan_a), (addr_b, work_b, orphan_b)):
            text = _pane_text(run, addr)
            assert "omp --resume" not in text, (addr, text)
            assert "omp --continue" not in text, (addr, text)
            assert "unidentified_live_owner" in text, (addr, text)
            assert str(orphan.pid) in text, (addr, text)

        log_text = (gen / "omp-restore-warnings.log").read_text()
        assert str(work_a) in log_text and str(orphan_a.pid) in log_text
        assert str(work_b) in log_text and str(orphan_b.pid) in log_text
    finally:
        orphan_a.kill()
        orphan_a.wait(timeout=5)
        orphan_b.kill()
        orphan_b.wait(timeout=5)


@_requires_tmux_e2e
def test_resurrect_writes_a_real_snapshot(tmux_env, monkeypatch):
    """DEFECTS 4/6: with the resurrect dir present + the plugin installed, a real `resurrect save`
    writes a `tmux_resurrect_*.txt` snapshot — so a reboot has something to restore."""
    home, socket, run = tmux_env
    _apply_with_real_plugins(home, monkeypatch)

    resurrect_dir = home / ".tmux" / "resurrect"
    assert resurrect_dir.is_dir()
    save_script = home / ".tmux" / "plugins" / "tmux-resurrect" / "scripts" / "save.sh"
    assert save_script.is_file(), "resurrect plugin not installed (save.sh missing)"

    # a real session to snapshot, then run resurrect's own save (private socket via the shim).
    run(["tmux", "new-session", "-d", "-s", "main"])
    r = run(["bash", str(save_script)])
    assert r.returncode == 0, f"resurrect save failed: {r.stderr}"
    snaps = list(resurrect_dir.glob("tmux_resurrect_*.txt"))
    assert snaps, f"no resurrect snapshot written in {resurrect_dir}"


# ── rig-cli#674: a slow boot-time conf load must not make continuum skip the restore ──────────
# tmux-continuum starts the layout restore only when the server is younger than
# @continuum-restore-max-delay (10 s) at the moment continuum.tmux runs during the conf load. After
# the 2026-10-01 reboot the load average hit ~140 and the boot script's conf load took ~16 s, so
# the restore was silently skipped and `main` stayed a bare one-pane session. The generated boot
# script holds a marker file during its `new-session`, and the generated conf widens the window
# only while that marker is fresh.
