"""Tests for riglib.harness_host — the crash supervisor that runs INSIDE an abduco session.

Pure pieces (exit classification, backoff/crash-loop cap, spec, env scrub, registry record) are
tested directly. The loop runs REAL short-lived child processes (a scripted fake harness that
SIGKILLs itself, exits cleanly, …) so the signal semantics are the kernel's, not a mock's, and one
test drives the whole thing through a real abduco session: 2026-09-30's omp was SIGKILLed by
macOS memory-pressure jetsam and abduco reported "exit status 0"."""

from __future__ import annotations

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

from riglib import harness_host as hh
from riglib import harness_kinds as hk
from riglib import harness_sessions as hs
from riglib.harness_kinds import SessionRef

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def short_dir():
    """A directory short enough for AF_UNIX sockets (macOS caps them near 104 bytes; pytest's
    tmp_path is deeper)."""
    path = Path(tempfile.mkdtemp(prefix="rig-", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


# ── exit classification ──────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("returncode", "crashed", "exit_code"),
    [
        (0, False, 0),  # /exit, Ctrl-D: the user asked for it
        (130, False, 130),  # omp's own SIGINT handler (128 + 2)
        (143, False, 143),  # omp's own SIGTERM handler — a deliberate stop
        (129, False, 129),  # omp's own SIGHUP handler — the terminal went away
        (-signal.SIGTERM, False, 143),
        (-signal.SIGHUP, False, 129),
        (-signal.SIGINT, False, 130),
        (1, True, 1),  # fatal uncaught error
        (-signal.SIGKILL, True, 137),  # jetsam / OOM killer / kill -9 — the 2026-09-30 incident
        (-signal.SIGSEGV, True, 139),
        (-signal.SIGABRT, True, 134),
    ],
)
def test_classify_exit(returncode, crashed, exit_code):
    verdict = hh.classify_exit(returncode)
    assert verdict.crashed is crashed
    assert verdict.exit_code == exit_code


def test_signal_death_is_described_by_name():
    assert "SIGKILL" in hh.classify_exit(-signal.SIGKILL).description


# ── restart policy ───────────────────────────────────────────────────────────────────────────
def test_quick_crashes_back_off_exponentially_up_to_the_cap():
    policy = hh.RestartPolicy(max_restarts=10, window=600, base_delay=1, max_delay=5, stable_after=60)
    assert [policy.next_delay(now=float(i), uptime=0.5) for i in range(5)] == [1, 2, 4, 5, 5]


def test_a_stable_run_resets_the_backoff():
    policy = hh.RestartPolicy(max_restarts=10, window=600, base_delay=1, max_delay=30, stable_after=60)
    assert policy.next_delay(now=0, uptime=1) == 1
    assert policy.next_delay(now=1, uptime=1) == 2
    assert policy.next_delay(now=500, uptime=400) == 1


def test_crash_loop_gives_up_after_max_restarts_within_the_window():
    policy = hh.RestartPolicy(max_restarts=3, window=100, base_delay=0, max_delay=0, stable_after=60)
    assert [policy.next_delay(now=t, uptime=1) for t in (0, 10, 20)] == [0, 0, 0]
    assert policy.next_delay(now=30, uptime=1) is None


def test_crashes_older_than_the_window_do_not_count_toward_the_cap():
    policy = hh.RestartPolicy(max_restarts=2, window=100, base_delay=0, max_delay=0, stable_after=60)
    assert policy.next_delay(now=0, uptime=1) == 0
    assert policy.next_delay(now=10, uptime=1) == 0
    assert policy.next_delay(now=200, uptime=1) == 0


# ── the spec that travels in the environment, never in argv ──────────────────────────────────
def test_spec_round_trips_through_the_environment():
    spec = hh.HostSpec("claude", ["/c/claude", "--model", "m"], "/work", "tg-cli", SessionRef(id="s1", cwd="/w2"))
    assert hh.HostSpec.from_env(spec.to_env()) == spec


@pytest.mark.parametrize(
    "text",
    [None, "", "not json", "{}", '{"v":2,"harness":"omp","argv":["x"],"cwd":"/"}',
     '{"v":1,"harness":"pi","argv":["x"],"cwd":"/"}', '{"v":1,"harness":"omp","argv":[],"cwd":"/"}'],
)  # fmt: skip
def test_invalid_spec_is_refused(text):
    with pytest.raises(ValueError, match=hh.ENV_SPEC):
        hh.HostSpec.from_env(text)


def test_harness_env_carries_membership_only_and_scrubs_subagent_markers():
    base = {
        "PATH": "/bin",
        "RIG_AGENT_ID": "SomeSubagent",
        "RIG_DETACHED_AGENT": "1",
        "PI_SESSION_FILE": "/s/nested/agent.jsonl",
        "TG_AGENT": "worker",
        "RIG_OMP_SESSION": "omp-stale",
        hh.ENV_SPEC: "{}",
    }
    env = hh.host_env(base, "claude-0123456789ab")
    assert env == {"PATH": "/bin", "RIG_SESSION": "claude-0123456789ab"}
    assert not any("COORDINATOR" in k or k == "AGENT_ROLE" for k in env)


def test_host_command_names_the_session_but_never_a_harness():
    argv = hh.host_command("claude-0123456789ab")
    assert argv[-3:] == ["harness", "host", "claude-0123456789ab"]
    assert hh.parse_host_command(argv) == ("claude-0123456789ab", "claude")
    assert hh.parse_host_command(["/o/omp", "--resume", "x"]) is None
    assert hh.parse_host_command([*argv[:-1], "pi-0123456789ab"]) is None  # unknown harness kind
    assert hh.parse_host_command([*argv, "extra"]) is None


@pytest.mark.parametrize("name", ["", "../x", "a b", "-lead", "x" * 62])
def test_host_names_are_validated(name):
    with pytest.raises(ValueError):
        hh.validate_name(name)


# ── the tg-ctl registration record ───────────────────────────────────────────────────────────
def _registry(tmp_path: Path, spec: hh.HostSpec | None = None) -> hh.Registry:
    spec = spec or hh.HostSpec("claude", ["/c/claude"], "/work/app", "tg-cli")
    socket = tmp_path / "sock" / "claude-0123456789ab@host"
    return hh.Registry(tmp_path / "tgcfg" / "hosts", "claude-0123456789ab", spec, socket, 4321)


def test_record_matches_the_tg_ctl_contract(tmp_path):
    registry = _registry(tmp_path)
    registry.write(harness_pid=777, session=SessionRef(id="sid", file="/t/sid.jsonl"), restarts=2)
    path = tmp_path / "tgcfg" / "hosts" / "claude-0123456789ab.json"
    record = json.loads(path.read_text())
    assert record["v"] == 1
    assert record["name"] == "claude-0123456789ab"
    assert record["harness"] == "claude"
    assert record["label"] == "tg-cli"
    assert record["cwd"] == "/work/app"
    assert record["role"] == "coordinator"
    assert record["serverPid"] == 4321
    assert record["harnessPid"] == 777
    assert Path(record["socket"]).is_absolute()
    assert record["session"] == {"id": "sid", "file": "/t/sid.jsonl"}
    assert record["restarts"] == 2
    assert record["startedAt"].endswith("Z") and record["updatedAt"].endswith("Z")
    assert (path.stat().st_mode & 0o777) == 0o600
    assert (path.parent.stat().st_mode & 0o777) == 0o700
    assert [p.name for p in path.parent.iterdir()] == [path.name]  # no temp file left behind


def test_record_is_refreshed_in_place_and_deleted(tmp_path):
    registry = _registry(tmp_path)
    registry.write(harness_pid=1, session=None, restarts=0)
    started = json.loads(registry.path.read_text())["startedAt"]
    registry.write(harness_pid=2, session=None, restarts=1)
    record = json.loads(registry.path.read_text())
    assert (record["harnessPid"], record["restarts"], record["startedAt"]) == (2, 1, started)
    registry.delete()
    registry.delete()  # idempotent
    assert not registry.path.exists()


def test_label_is_optional(tmp_path):
    registry = _registry(tmp_path, hh.HostSpec("codex", ["/x/codex"], "/w"))
    registry.write(harness_pid=1, session=None, restarts=0)
    assert "label" not in json.loads(registry.path.read_text())


def test_no_record_when_not_inside_an_abduco_session(tmp_path):
    spec = hh.HostSpec("omp", ["/o/omp"], "/w")
    sockdir = tmp_path / "abduco" / "abduco" / "u"
    sockdir.mkdir(parents=True)
    env = {"ABDUCO_SOCKET_DIR": str(tmp_path / "abduco"), "TG_CTL_CONFIG_DIR": str(tmp_path / "tg")}
    # no socket for that name: a hand-run `rig harness host` has nothing tg-ctl could connect to
    assert hh.Registry.open("omp-0123456789ab", spec, env, server_pid=10, is_abduco=lambda _pid: True) is None


def test_record_needs_an_abduco_server_as_parent_and_an_absolute_socket(tmp_path, short_dir):
    import socket as socketlib

    spec = hh.HostSpec("omp", ["/o/omp"], "/w")
    root = short_dir / "ab"
    sockdir = root / "abduco" / "u"
    sockdir.mkdir(parents=True)
    sock = socketlib.socket(socketlib.AF_UNIX)
    sock.bind(str(sockdir / "omp-0123456789ab@h"))
    try:
        env = {"ABDUCO_SOCKET_DIR": str(root), "TG_CTL_CONFIG_DIR": str(tmp_path / "tg")}
        assert hh.Registry.open("omp-0123456789ab", spec, env, server_pid=10, is_abduco=lambda _p: False) is None
        registry = hh.Registry.open("omp-0123456789ab", spec, env, server_pid=10, is_abduco=lambda _p: True)
        assert registry is not None
        assert registry.path == tmp_path / "tg" / "hosts" / "omp-0123456789ab.json"
        registry.write(harness_pid=None, session=None, restarts=0)
        record = json.loads(registry.path.read_text())
        assert record["serverPid"] == 10 and record["socket"] == str(sockdir / "omp-0123456789ab@h")
        assert "harnessPid" not in record
    finally:
        sock.close()


# ── the loop, against real child processes ───────────────────────────────────────────────────
FAKE_HARNESS = textwrap.dedent(
    """
    import json, os, signal, sys, time
    from pathlib import Path

    state = Path(os.environ["FAKE_STATE"])
    runs = json.loads(state.read_text()) if state.exists() else []
    keys = ("RIG_SESSION", "RIG_AGENT_ID", "RIG_HOST_SPEC", "TG_AGENT", "PI_SESSION_FILE", "RIG_DETACHED_AGENT")
    runs.append({"pid": os.getpid(), "argv": sys.argv[1:], "cwd": os.getcwd(), "env": {k: os.environ.get(k) for k in keys}})
    state.write_text(json.dumps(runs))
    for path, text in json.loads(os.environ.get("FAKE_FILES", "[]")):
        path = path.replace("{pid}", str(os.getpid()))
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text.replace("{pid}", str(os.getpid())))
    plan = os.environ["FAKE_PLAN"].split(",")
    action = plan[min(len(runs), len(plan)) - 1]
    time.sleep(float(os.environ.get("FAKE_DELAY", "0")))
    if action == "kill9":
        os.kill(os.getpid(), signal.SIGKILL)
    if action == "sleep":
        time.sleep(60)
    sys.exit(int(action))
    """
)

# The fake harness is run by `sys.executable` through its shebang under the supervisor's explicit
# env. A shared-libpython interpreter (CI's setup-python build) finds libpython only through the
# loader path, so that path is the one ambient variable the test env must carry.
LOADER_ENV = {k: os.environ[k] for k in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH") if k in os.environ}


@pytest.fixture
def fake(tmp_path):
    script = tmp_path / "fake_harness.py"
    # An executable of its own, like the real binaries: argv[0] IS the binary.
    script.write_text(f"#!{sys.executable}\n{FAKE_HARNESS}")
    script.chmod(0o755)
    (tmp_path / "work").mkdir()
    return script


def _runs(tmp_path: Path) -> list[dict]:
    state = tmp_path / "runs.json"
    return json.loads(state.read_text()) if state.exists() else []


def _host(fake_script, tmp_path, plan, *, kind="omp", args=(), session=None, registry=None, policy=None, files=(), delay="0", **kw):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        **LOADER_ENV,
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "FAKE_STATE": str(tmp_path / "runs.json"),
        "FAKE_PLAN": plan,
        "FAKE_FILES": json.dumps(list(files)),
        "FAKE_DELAY": delay,
    }
    spec = hh.HostSpec(kind, [str(fake_script), *args], str(tmp_path / "work"), "lbl", session)
    return hh.supervise(
        spec,
        name=f"{kind}-0123456789ab",
        env=env,
        registry=registry,
        policy=policy or hh.RestartPolicy(base_delay=0, max_delay=0),
        log_path=tmp_path / "supervisor.log",
        home=home,
        poll_interval=0.05,
        **kw,
    )


def _omp_breadcrumb(tmp_path: Path, terminal: str, session: Path) -> None:
    crumb = tmp_path / "home" / ".omp" / "agent" / "terminal-sessions" / terminal
    crumb.parent.mkdir(parents=True, exist_ok=True)
    crumb.write_text(f"{session.parent}\n{session.name}\n")
    future = time.time() + 3600
    os.utime(crumb, (future, future))


def test_killed_harness_is_relaunched_resuming_the_session_it_was_in(fake, tmp_path):
    session = tmp_path / "live.jsonl"
    session.write_text('{"type":"session","id":"s1"}\n')
    _omp_breadcrumb(tmp_path, "ttys-test", session)
    rc = _host(fake, tmp_path, "kill9,0", args=["--resume", "/orig.jsonl"], terminal="ttys-test")
    assert rc == 0
    assert [r["argv"] for r in _runs(tmp_path)] == [["--resume", "/orig.jsonl"], ["--resume", str(session)]]
    log = (tmp_path / "supervisor.log").read_text()
    assert "SIGKILL" in log and str(session) in log


def test_jetsam_sigkill_is_revived_but_a_clean_exit_is_not(fake, tmp_path):
    """The 2026-09-30 incident and its mirror image: abduco reports BOTH as "exit status 0"; only
    the supervisor, the harness's parent, can tell them apart."""
    assert _host(fake, tmp_path, "kill9,0") == 0
    assert len(_runs(tmp_path)) == 2  # SIGKILL -> relaunched
    (tmp_path / "runs.json").unlink()
    assert _host(fake, tmp_path, "0,0") == 0
    assert len(_runs(tmp_path)) == 1  # exit 0 (/exit) -> stays dead


def test_without_a_known_session_the_previous_command_is_reused(fake, tmp_path):
    assert _host(fake, tmp_path, "1,0", args=["--resume", "/orig.jsonl"]) == 0
    assert [r["argv"] for r in _runs(tmp_path)] == [["--resume", "/orig.jsonl"]] * 2


def test_crash_loop_stops_reports_the_real_signal_and_unregisters(fake, tmp_path):
    registry = _registry(tmp_path)
    policy = hh.RestartPolicy(max_restarts=2, base_delay=0, max_delay=0)
    rc = _host(fake, tmp_path, "kill9", policy=policy, registry=registry)
    assert rc == 128 + signal.SIGKILL
    assert len(_runs(tmp_path)) == 3
    assert "not restarting" in (tmp_path / "supervisor.log").read_text()
    assert not registry.path.exists()


def test_requested_exit_unregisters_and_is_not_relaunched(fake, tmp_path):
    registry = _registry(tmp_path)
    assert _host(fake, tmp_path, "130,0", registry=registry) == 130
    assert len(_runs(tmp_path)) == 1
    assert not registry.path.exists()


class _RecordingRegistry(hh.Registry):
    seen: list[dict]

    def write(self, **kwargs):
        super().write(**kwargs)
        self.seen.append(json.loads(self.path.read_text()))


def test_registry_follows_every_relaunch(fake, tmp_path):
    spec = hh.HostSpec("claude", ["/c/claude"], "/w")
    registry = _RecordingRegistry(tmp_path / "tg" / "hosts", "claude-0123456789ab", spec, tmp_path / "s", 9)
    registry.seen = []
    assert _host(fake, tmp_path, "kill9,kill9,0", registry=registry) == 0
    runs = _runs(tmp_path)
    assert [(r["harnessPid"], r["restarts"]) for r in registry.seen] == [
        (runs[0]["pid"], 0),
        (runs[1]["pid"], 1),
        (runs[2]["pid"], 2),
    ]
    assert not registry.path.exists()


def test_the_harness_sees_membership_only(fake, tmp_path):
    leaky = {
        **LOADER_ENV,
        "PATH": os.environ["PATH"], "FAKE_STATE": str(tmp_path / "runs.json"), "FAKE_PLAN": "0",
        "RIG_AGENT_ID": "Sub", "TG_AGENT": "w", "PI_SESSION_FILE": "/n/a.jsonl", "RIG_DETACHED_AGENT": "1", hh.ENV_SPEC: "{}",
    }  # fmt: skip
    spec = hh.HostSpec("omp", [str(fake)], str(tmp_path / "work"))
    assert hh.supervise(spec, name="omp-0123456789ab", env=leaky, log_path=None) == 0
    assert _runs(tmp_path)[0]["env"] == {
        "RIG_SESSION": "omp-0123456789ab", "RIG_AGENT_ID": None, "RIG_HOST_SPEC": None,
        "TG_AGENT": None, "PI_SESSION_FILE": None, "RIG_DETACHED_AGENT": None,
    }  # fmt: skip


def test_harness_runs_in_the_recorded_cwd(fake, tmp_path):
    assert _host(fake, tmp_path, "0") == 0
    assert _runs(tmp_path)[0]["cwd"] == str((tmp_path / "work").resolve())


def test_a_missing_binary_is_reported_not_raised(tmp_path):
    spec = hh.HostSpec("omp", [str(tmp_path / "nope")], str(tmp_path))
    assert hh.supervise(spec, name="omp-0123456789ab", env={"PATH": "/bin"}, log_path=tmp_path / "log") == 127
    assert "cannot start" in (tmp_path / "log").read_text()


# per-harness: the relaunch command is the harness's own native resume ─────────────────────────
def test_claude_is_relaunched_with_resume_of_its_assigned_conversation(fake, tmp_path):
    cwd = str(tmp_path / "work")
    argv, ref = hk.prepare_launch("claude", [str(fake), "--model", "opus"], new_id="sid-assigned")
    transcript = hs.claude_transcript({}, tmp_path / "home", cwd, "sid-assigned")
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")
    rc = _host(fake, tmp_path, "kill9,0", kind="claude", args=argv[1:], session=ref)
    assert rc == 0
    assert [r["argv"] for r in _runs(tmp_path)] == [
        ["--model", "opus", "--session-id", "sid-assigned"],
        ["--model", "opus", "--resume", "sid-assigned"],
    ]


def test_claude_follows_the_session_id_the_live_process_published(fake, tmp_path):
    cwd = str(tmp_path / "work")
    home = tmp_path / "home"
    transcript = hs.claude_transcript({}, home, cwd, "after-clear")
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")
    published = json.dumps({"sessionId": "after-clear", "cwd": cwd})
    sessions = str(home / ".claude" / "sessions" / "{pid}.json")
    rc = _host(
        fake, tmp_path, "kill9,0", kind="claude", args=["--session-id", "assigned"], session=SessionRef(id="assigned"),
        files=[[sessions, published]], delay="0.5",
    )  # fmt: skip
    assert rc == 0
    assert _runs(tmp_path)[1]["argv"] == ["--resume", "after-clear"]


def test_claude_that_died_before_writing_anything_restarts_under_the_same_id(fake, tmp_path):
    rc = _host(fake, tmp_path, "kill9,0", kind="claude", args=["--session-id", "sid"], session=SessionRef(id="sid"))
    assert rc == 0
    assert [r["argv"] for r in _runs(tmp_path)] == [["--session-id", "sid"]] * 2


def test_codex_is_relaunched_with_resume_of_its_rollout(fake, tmp_path):
    cwd = str(tmp_path / "work")
    now = time.localtime()
    rollout = (
        tmp_path / "home" / ".codex" / "sessions" / f"{now.tm_year:04d}" / f"{now.tm_mon:02d}" / f"{now.tm_mday:02d}" / "rollout-a-uuid-1.jsonl"
    )  # fmt: skip
    rollout.parent.mkdir(parents=True)
    rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": "uuid-1", "cwd": cwd}}) + "\n")
    future = time.time() + 3600
    os.utime(rollout, (future, future))
    rc = _host(fake, tmp_path, "kill9,0", kind="codex", args=["-m", "gpt-5", "fix it"])
    assert rc == 0
    assert [r["argv"] for r in _runs(tmp_path)] == [["-m", "gpt-5", "fix it"], ["-m", "gpt-5", "resume", "uuid-1"]]


def test_opencode_is_relaunched_with_session_of_its_database_row(fake, tmp_path):
    cwd = str((tmp_path / "work").resolve())
    db = tmp_path / "home" / ".local" / "share" / "opencode" / "opencode.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE session (id text, directory text, parent_id text, time_updated integer, time_archived integer)")
    conn.execute("INSERT INTO session VALUES ('ses_x', ?, NULL, ?, NULL)", (cwd, int((time.time() + 3600) * 1000)))
    conn.commit()
    conn.close()
    rc = _host(fake, tmp_path, "kill9,0", kind="opencode", args=["--continue"])
    assert rc == 0
    assert [r["argv"] for r in _runs(tmp_path)] == [["--continue"], ["--session", "ses_x"]]


# ── SIGTERM to the host itself ───────────────────────────────────────────────────────────────
def test_terminating_the_host_stops_the_child_without_a_restart(fake, tmp_path):
    env = {
        **os.environ,
        "FAKE_STATE": str(tmp_path / "runs.json"),
        "FAKE_PLAN": "sleep",
        "RIG_OMP_ABDUCO_DIR": str(tmp_path / "abduco"),
        "TG_CTL_CONFIG_DIR": str(tmp_path / "tg"),
        hh.ENV_SPEC: hh.HostSpec("omp", [str(fake)], str(tmp_path / "work")).to_env(),
    }
    host = subprocess.Popen(
        [sys.executable, "-m", "riglib", "harness", "host", "omp-0123456789ab"],
        cwd=REPO_ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 10
        while not _runs(tmp_path) and time.time() < deadline:
            time.sleep(0.05)
        child = _runs(tmp_path)[0]["pid"]
        host.send_signal(signal.SIGTERM)
        assert host.wait(timeout=10) == 128 + signal.SIGTERM
    finally:
        if host.poll() is None:
            host.kill()
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)
    assert len(_runs(tmp_path)) == 1


def test_host_refuses_to_run_without_its_spec(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != hh.ENV_SPEC}
    done = subprocess.run(
        [sys.executable, "-m", "riglib", "harness", "host", "omp-0123456789ab"],
        cwd=REPO_ROOT, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
    )  # fmt: skip
    assert done.returncode == 2 and hh.ENV_SPEC in done.stderr


# ── the whole thing inside a real abduco session ─────────────────────────────────────────────
pytestmark_abduco = pytest.mark.skipif(shutil.which("abduco") is None, reason="needs abduco")


@pytestmark_abduco
def test_killed_harness_returns_in_the_same_abduco_session_and_a_requested_exit_stays_dead(fake, tmp_path, short_dir):
    """kill -9 the harness of a hosted session: the SAME abduco server / host keep running and a
    new harness appears with the same argv; then SIGTERM it: the session ends, abduco reports the
    REAL status (143, not the 0 it would print for a killed child) and the tg-ctl record is gone."""
    socket_root = short_dir / "abduco"
    name = f"omp-t{os.getpid():08x}"
    env = {
        **os.environ,
        "ABDUCO_SOCKET_DIR": str(socket_root),
        "RIG_OMP_ABDUCO_DIR": str(socket_root),
        "TG_CTL_CONFIG_DIR": str(tmp_path / "tg"),
        "FAKE_STATE": str(tmp_path / "runs.json"),
        "FAKE_PLAN": "sleep",
        hh.ENV_SPEC: hh.HostSpec("omp", [str(fake), "--resume", "/orig.jsonl"], str(tmp_path / "work"), "smoke").to_env(),
    }
    socket_root.mkdir()
    created = subprocess.run(["abduco", "-n", name, *hh.host_command(name)], env=env, stdin=subprocess.DEVNULL)
    assert created.returncode == 0
    record = tmp_path / "tg" / "hosts" / f"{name}.json"

    def wait_for(predicate, what):
        deadline = time.time() + 15
        while time.time() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        pytest.fail(f"timed out waiting for {what}")

    try:
        first = wait_for(lambda: _runs(tmp_path) or None, "the first harness")[0]
        wait_for(record.exists, "the tg-ctl record")
        server = json.loads(record.read_text())["serverPid"]
        assert subprocess.run(["ps", "-o", "comm=", "-p", str(server)], capture_output=True, text=True).stdout.strip().endswith("abduco")

        os.kill(first["pid"], signal.SIGKILL)  # what macOS jetsam did to omp
        second = wait_for(lambda: len(_runs(tmp_path)) == 2 and _runs(tmp_path)[1], "the relaunch")
        assert second["argv"] == first["argv"] and second["pid"] != first["pid"]
        assert second["env"]["RIG_SESSION"] == name
        wait_for(lambda: json.loads(record.read_text()).get("harnessPid") == second["pid"], "the refreshed record")
        assert json.loads(record.read_text())["serverPid"] == server  # same abduco session

        os.kill(second["pid"], signal.SIGTERM)  # a deliberate stop
        wait_for(lambda: not record.exists(), "the record to be deleted")
        time.sleep(0.3)
        assert len(_runs(tmp_path)) == 2  # not relaunched
        status = subprocess.run(["abduco", "-a", name], env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True)
        assert "exit status 143" in status.stderr
    finally:
        victims = [run["pid"] for run in _runs(tmp_path)]
        if record.exists():
            victims.append(json.loads(record.read_text())["serverPid"])
        for pid in victims:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
