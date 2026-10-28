"""Shared gates, shims, fixture and builders for the real-tmux e2e test files."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import uuid
import socket
from pathlib import Path

import pytest

from riglib.actions import runner
from riglib.actions import tmux as tmux_mod
from riglib.actions import live_env as live_env_mod
from riglib.actions import service_ctl as service_ctl_mod



def _github_reachable() -> bool:
    """True if GitHub's https port is reachable — the e2e clones the real tmux plugins from there
    (it needs the REAL resurrect ``save.sh`` to write a real snapshot, the whole acceptance point).
    Even under the opt-in flag we still skip (not fail) when offline. Cheap TCP probe, 3s timeout."""
    try:
        with socket.create_connection(("github.com", 443), timeout=3):
            return True
    except OSError:
        return False


# Most tests here drive a REAL tmux server AND clone the real plugins from GitHub — real network +
# daemon access. The repo's plain `python -m pytest -q` is documented as fast + HERMETIC (AGENTS.md),
# so those e2e tests are OPT-IN via `RIG_TMUX_E2E=1` (codex finding): default CI/pytest stays hermetic
# and offline-safe; the BFS / artifact logic they exercise is ALSO covered hermetically (the unit suite
# + `test_pane_has_claude_*`, which run with no network). The acceptance gate the CTO runs is
# `RIG_TMUX_E2E=1 pytest tests/test_tmux_e2e.py`. Even when opted in, they skip (never fail) when
# tmux/git is absent or GitHub is unreachable. The autouse RIG_TMUX_DRY_RUN guard (conftest) is
# cleared per-test where a live step is exercised.
#
# The gate is applied PER-TEST (the decorators below), NOT as a blanket module-level `pytestmark`,
# because the gate's GRANULARITY differs by what a test actually needs (2026-06-17 INCIDENT
# follow-up (a)):
#   - `_requires_tmux_e2e` — the FULL opt-in + tmux + git + network gate, for the tests that clone
#     the real plugins from GitHub (the reboot-cycle acceptance, cc-save/restore, resurrect snapshot,
#     boot-cleanup). These genuinely need the network and stay opt-in so default CI is hermetic.
#   - `_requires_tmux` — tmux-present ONLY (no opt-in flag, NO network), for the socket-leak
#     REGRESSION (`test_teardown_unlinks_the_private_socket_file`). It boots a private `-L` server
#     running `tail -f /dev/null` and asserts teardown UNLINKS the socket — it clones nothing and
#     hits no network. Coupling it to `RIG_TMUX_E2E=1` + GitHub-reachability (the old blanket
#     `pytestmark`) meant the one regression guarding the leak that actually KILLED the dev's tmux
#     server (the ~185 leaked `rigtest-*` sockets of INCIDENT 2026-06-17 — 166 of them this fixture's)
#     NEVER ran in default hermetic CI or offline — exactly when it matters. Decoupled here so it
#     runs whenever tmux is installed.
_E2E_OPTED_IN = os.environ.get("RIG_TMUX_E2E", "").strip() in ("1", "true", "yes")

# Resolved ONCE at import — `_github_reachable()` does a 3s TCP probe, so calling it per-test (the
# 6 network tests each evaluating the decorator) would add up; one probe is enough for the gate.
# `_E2E_OPTED_IN` is the FIRST operand of the `and` chain ON PURPOSE: Python short-circuits, so the
# 3s probe is NEVER run unless the e2e is opted in — a default hermetic `pytest` (RIG_TMUX_E2E unset)
# pays ZERO network cost here, even offline. (Trade-off, same as the old `pytestmark`: the result is
# precomputed at import, so a transient outage at collection skips the 6 network tests for the whole
# session even if connectivity returns — acceptable; they are the opt-in acceptance gate, not CI.)
_NETWORK_E2E_AVAILABLE = (
    _E2E_OPTED_IN
    and shutil.which("tmux") is not None
    and shutil.which("git") is not None
    and _github_reachable()
)
_requires_tmux_e2e = pytest.mark.skipif(
    not _NETWORK_E2E_AVAILABLE,
    reason="real-tmux e2e is opt-in: set RIG_TMUX_E2E=1 (needs tmux + git + network; auto-skips offline)",
)
# tmux-only, no network, no opt-in flag — the socket-leak regression runs in default hermetic CI.
_requires_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="needs tmux installed (no network / no RIG_TMUX_E2E required)",
)
# omp-restore's system-wide orphan guard (below) reads process cwds via `lsof -d cwd` (no
# /proc/<pid>/cwd on macOS/BSD). Those two tests reuse `_requires_tmux` rather than adding a new
# gate bucket to the tamper-proof classification in test_tmux_e2e_gating.py; lsof is NOT on every
# Linux image (the Forgejo Ubuntu runner lacks it), so the unit CI job installs it next to tmux and
# the gating test asserts it is on PATH in CI.
# The omp-restore POSITIVE control goes one step further: it proves the restored pane really
# relaunches omp, which `rig harness run omp` does by exec'ing abduco. abduco is a Homebrew formula but
# is not packaged for Ubuntu 24.04, so a Linux dev machine usually lacks it — skip there (an
# ADDITIONAL mark; the tmux gate stays for the classification). CI builds a pinned abduco
# (scripts/install-abduco-ci.sh) and the gating test asserts it is on PATH in CI.
_requires_abduco = pytest.mark.skipif(
    shutil.which("abduco") is None,
    reason="needs abduco installed — the restored pane relaunches omp via `rig harness run omp`, which execs abduco",
)


# ── a PATH tmux shim that pins every `tmux …` call to a private -L socket ────────────────────
def _install_tmux_shim(bindir: Path, socket: str) -> None:
    """Write a `tmux` wrapper on PATH that injects `-L <socket>` so the UNMODIFIED generated
    scripts (which call bare `tmux`) hit a private server, never the developer's default one."""
    real = shutil.which("tmux")
    shim = bindir / "tmux"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f'exec {real} -L {socket} "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)


def _install_rig_shim(bindir: Path, repo_root: Path) -> None:
    """Write a `rig` wrapper on PATH that execs THIS checkout's CLI (``python3 -m riglib.cli``)
    — the omp-save/omp-restore scripts under test now shell out to `rig omp save-panes`/
    `restore-panes` (rig-cli#619); without this, a bare `rig` on the test runner's ambient PATH
    could resolve to a DIFFERENT, possibly stale install lacking those subcommands entirely."""
    shim = bindir / "rig"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f'PYTHONPATH="{repo_root}"${{PYTHONPATH:+:$PYTHONPATH}} exec "{sys.executable}" -m riglib.cli "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)


def _socket_path_for(label: str, env: dict[str, str] | None = None) -> Path:
    """The on-disk path tmux uses for a ``-L <label>`` server: ``$TMUX_TMPDIR | /tmp`` +
    ``/tmux-<uid>/<label>``, resolved against ``env`` (defaults to ``os.environ``). Callers MUST
    pass the SAME env they launched the server with, else teardown resolves the wrong path and
    leaks the socket (see module docstring). Matches tmux's ``server_create_socket``:
    ``TMUX_TMPDIR`` is honored only when ABSOLUTE, else ``/tmp``."""
    src = os.environ if env is None else env
    tmpdir = src.get("TMUX_TMPDIR", "").strip()
    if not tmpdir or not os.path.isabs(tmpdir):
        tmpdir = "/tmp"
    return Path(tmpdir) / f"tmux-{os.getuid()}" / label


def _teardown_tmux_server(real_tmux: str, label: str, env: dict[str, str] | None = None) -> None:
    """Kill the private ``-L <label>`` server AND remove its socket file, resolving the socket
    path under ``env`` (the env the server was launched with). Both steps run best-effort: a
    failed/timed-out kill (e.g. no server ever started) must STILL let the unlink run, and a
    missing socket file must not raise. ``kill-server`` ends the process but leaks the socket
    inode on macOS — see the module docstring."""
    try:
        subprocess.run(
            [real_tmux, "-L", label, "kill-server"],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass  # the unlink below must run regardless of how the kill went
    finally:
        _socket_path_for(label, env).unlink(missing_ok=True)


def _pane_text(run, addr: str) -> str:
    """The pane's visible text with tmux's soft wraps undone (``capture-pane -J``). Without
    ``-J`` every wrapped row comes back right-trimmed, so a command that wraps right after a
    space (``…/bin/rig ⏎harness run``) reads back as ``righarness run`` and a substring check depends on
    the tmp path's length — how the omp-restore positive control failed on CI (rig-cli#651)."""
    return run(["tmux", "capture-pane", "-p", "-J", "-t", addr]).stdout


def _wait_for_restored_pane_text(run, addr: str, argv_log: Path, *, timeout_s=15, sleep=time.sleep, clock=time.time) -> str:
    """Poll until the fake omp has logged its argv AND the pane is back on its normal screen,
    then return the pane text. abduco attaches on tmux's ALTERNATE screen and the fake omp writes
    ``argv_log`` while it is still attached, so capturing as soon as the file exists reads the
    blank alternate screen instead of the typed command line (the intermittent empty-pane CI
    failure, rig-cli#772). Bounded by ``timeout_s``; on expiry the pane is captured anyway so the
    caller's assertions fail with the real pane text."""
    deadline = clock() + timeout_s
    while clock() < deadline:
        if argv_log.exists() and run(["tmux", "display-message", "-p", "-t", addr, "#{alternate_on}"]).stdout.strip() == "0":
            break
        sleep(0.2)
    return _pane_text(run, addr)


@pytest.fixture
def tmux_env(tmp_path, monkeypatch):
    """A throwaway HOME + a private tmux socket + a PATH shim, with teardown that kills the
    server. Yields (home, socket, run) where `run` executes a command with the shimmed PATH."""
    home = tmp_path / "home"
    home.mkdir()
    socket = f"rigtest-{uuid.uuid4().hex[:8]}"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _install_tmux_shim(bindir, socket)
    _install_rig_shim(bindir, Path(__file__).resolve().parents[1])

    real_tmux = shutil.which("tmux")
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    # NB: the private `-L <socket>` already isolates the server; do NOT also set a deep
    # TMUX_TMPDIR — a unix socket path has a ~104-char limit on macOS and a pytest tmp dir blows
    # it ("File name too long"). The unique -L name under the default /tmp tmpdir is short + safe.
    # don't let an inherited $TMUX (we may run inside tmux) confuse nested calls.
    env.pop("TMUX", None)
    env.pop("TMUX_TMPDIR", None)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    # CRITICAL: the generated boot script bakes its tmux path at GENERATION time via
    # `_resolve_tmux_bin` -> `shutil.which("tmux")`, which (in-process) is the REAL tmux on the
    # DEFAULT socket — NOT the shim. Left unpatched, the boot script would create a session on
    # the user's real tmux server (the exact thing the private -L socket is meant to prevent).
    # Point the resolver at the shim so EVERY tmux the rig artifacts invoke hits the private socket.
    from riglib import tmux as _tmod
    monkeypatch.setattr(_tmod, "_resolve_tmux_bin", lambda: str(bindir / "tmux"))
    # Same reasoning for `rig`: omp-save/omp-restore now shell out to `rig omp save-panes`/
    # `restore-panes` (rig-cli#619) — pin it to the checkout-under-test's own CLI, not whatever
    # (possibly stale) `rig` happens to be first on the runner's ambient PATH.
    monkeypatch.setattr(_tmod, "_resolve_rig_bin", lambda: str(bindir / "rig"))

    def run(cmd, **kw):
        return subprocess.run(
            cmd, env=env, capture_output=True, text=True, timeout=kw.pop("timeout", 30), **kw
        )

    try:
        yield home, socket, run
    finally:
        # kill the private server AND unlink its socket file — never leave a stray tmux server
        # OR a leaked socket inode behind (kill-server alone leaks the file on macOS). Resolve the
        # socket path under the SAME scrubbed `env` the server ran with (it pops TMUX_TMPDIR).
        _teardown_tmux_server(real_tmux, socket, env)


def _wait_for_claude_descendant(run, *, timeout_s=10):
    """Poll until a process whose `comm` is `claude` is a descendant of some tmux pane on the
    private socket — removes the race between launching the fake claude and `ps` seeing it.

    Uses the SAME tree-walk the cc-save script does, so if this sees the descendant, cc-save will
    too. Returns the pane addr when found; raises if it never appears (a real failure, not a flake).
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        panes = run(["tmux", "list-panes", "-a", "-F", "#{session_name}:#{window_index}.#{pane_index} #{pane_pid}"])
        tree, comm = _process_snapshot()
        for ln in panes.stdout.splitlines():
            addr, _, pane_pid = ln.partition(" ")
            if _subtree_has_claude(pane_pid.strip(), tree, comm):
                return addr
        time.sleep(0.3)
    raise AssertionError("fake claude never became a visible descendant of a pane within timeout")


def _process_snapshot():
    """``(children-by-ppid, comm-by-pid)`` from one `ps` snapshot."""
    snap = subprocess.run(["ps", "-eo", "pid=,ppid=,comm="], capture_output=True, text=True, timeout=10).stdout
    tree = {}
    comm = {}
    for ln in snap.splitlines():
        parts = ln.split(None, 2)
        if len(parts) == 3:
            pid, ppid, c = parts
            tree.setdefault(ppid, []).append(pid)
            comm[pid] = c
    return tree, comm


def _subtree_has_claude(root_pid, tree, comm):
    """True if ``root_pid`` or any descendant has a `claude` comm (same walk cc-save does)."""
    stack = [root_pid]
    seen = set()
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        if comm.get(cur, "").rsplit("/", 1)[-1] == "claude":
            return True
        stack.extend(tree.get(cur, []))
    return False


def _action(home, **over):
    from riglib.plan import Action

    options = {
        "apply_mode": "import",
        "conf_path": str(home / ".tmux.conf"),
        "generated_dir": str(home / ".config" / "rig" / "tmux"),
        "resurrect": {},
        "continuum": {},
        "moshi": {},
        "cc_restore": {},
        "anti_sprawl": {"enabled": True, "session": "main"},
        "boot": {"enabled": True},
        "login_shell": {},
    }
    options.update(over)
    return Action(kind="provision_tmux", category="tmux", item="config",
                  source=home, target=home / ".tmux.conf", options=options)


def _apply_with_real_plugins(home, monkeypatch, **action_over):
    """Run the full provision WITH real plugin clones + the resurrect dir, but keep launchctl
    stubbed (a test can't load a real launch agent without polluting the host). Returns the
    ActionResult. The boot script + cc scripts + config are all real on-disk artifacts."""
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    # rig-cli#116: these tests fake HOME on purpose and assert on the LIVE (stubbed) launchctl
    # path — opt out of the sandboxed-HOME auto-skip, which would otherwise route them dry.
    monkeypatch.setattr(live_env_mod, "_home_is_sandboxed", lambda: False)
    loads: list[str] = []
    monkeypatch.setattr(service_ctl_mod, "_launchctl", lambda verb, arg: 0)
    monkeypatch.setattr(service_ctl_mod, "_launchctl_loaded", lambda label: False)
    monkeypatch.setattr(service_ctl_mod, "_launchctl_load_enable", lambda plist: loads.append(str(plist)) or 0)
    monkeypatch.setattr(tmux_mod, "_clean_stale_continuum_boot", lambda plan: False)
    # Stub the in-apply first-save: it would boot a session on the host's DEFAULT tmux server
    # (the runner's _tmux_resurrect_save calls the boot script with the unshimmed PATH). The e2e
    # drives boot + save EXPLICITLY through the private-socket shim instead, so the assertions run
    # against an isolated server. We still let the REAL _git_clone + resurrect-dir creation run.
    monkeypatch.setattr(tmux_mod, "_tmux_resurrect_save", lambda plan: 0)
    res = runner._do_provision_tmux(_action(home, **action_over), "backup")
    return res, loads


# ── the acceptance e2e ───────────────────────────────────────────────────────────────────────
