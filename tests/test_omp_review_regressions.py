"""Final independent review: launcher boundaries and single-snapshot map parsing."""

from pathlib import Path
from unittest.mock import Mock
import pytest
from riglib import omp_launcher as ol
from riglib.omp_recovery import is_interactive_omp


def test_source_launcher_in_a_differently_named_checkout_is_detected():
    assert is_interactive_omp(
        "bun --preload /work/renamed/packages/coding-agent/scripts/omp.ts /work/renamed/packages/coding-agent/src/cli.ts --resume"
    )
    assert not is_interactive_omp("bun /work/unrelated/src/cli.ts --resume")


def test_restore_reads_map_once_even_if_replaced_during_inspection(
    tmp_path, monkeypatch
):
    file = tmp_path / "map"
    file.write_text("p:0\t/work\t/old.jsonl\told-durable\told-id\n")
    original_read = Path.read_text
    reads = []

    def read(self, *a, **kw):
        text = original_read(self, *a, **kw)
        if self == file:
            reads.append(1)
            file.write_text("p:0\t/work\t/new.jsonl\tnew-durable\tnew-id\n")
        return text

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(ol.omp_inspect, "live_omp_sessions", lambda *_: ([], True))
    monkeypatch.setattr(
        ol.omp_inspect, "list_abduco_sessions", lambda *_: (set(), True)
    )
    seen = []

    def restore(*args, **kw):
        from riglib.omp_recovery import RestorePlan

        seen.append((args, kw))
        return RestorePlan("skip", "test")

    monkeypatch.setattr(ol, "restore_one_pane", restore)
    ol.restore_panes(file, log_fn=lambda *_: None)
    assert len(reads) == 1
    assert seen[0][0][2] == "/old.jsonl" and seen[0][1]["session_id"] == "old-id"


@pytest.mark.parametrize("socket", [None, "/tmp/specific socket"])
def test_boot_works_under_stock_bash_with_and_without_socket(
    tmp_path, monkeypatch, socket
):
    import os
    import subprocess
    from riglib import tmux

    log = tmp_path / "argv"
    fake = tmp_path / "tmux"
    fake.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$ARGV_LOG"\ncase "$*" in *has-session*) exit 1;; esac\nexit 0\n'
    )
    fake.chmod(0o755)
    plan = tmux.build_tmux(repo_home=tmp_path)
    from dataclasses import replace

    plan = replace(plan, tmux_bin=str(fake))
    script = tmp_path / "boot.sh"
    script.write_text(plan.render_boot_script())
    env = dict(os.environ, ARGV_LOG=str(log))
    env.pop("RIG_TMUX_RECOVERY_SOCKET", None)
    if socket:
        env["RIG_TMUX_RECOVERY_SOCKET"] = socket
    result = subprocess.run(
        ["/bin/bash", str(script)], env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines()
    assert len(calls) == 2 and "new-session -d -s main" in calls[-1]
    assert ("-S /tmp/specific socket" in calls[-1]) == bool(socket)


def test_custom_durable_name_cannot_disappear_when_identity_is_unknown(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    target = tmp_path / "map"
    target.write_text("preserve previous map\n")
    monkeypatch.setattr(ol, "list_tmux_panes", lambda *_: [("m:1", 11, "/work")])
    monkeypatch.setattr(
        ol.omp_inspect, "capture_ps_snapshot", lambda: "11 1 /bin/abduco -a work-a\n"
    )
    monkeypatch.setattr(
        ol.omp_inspect, "find_omp_descendant", lambda *_: SimpleNamespace(pid=11)
    )
    monkeypatch.setattr(ol.omp_inspect, "pane_durable_session", lambda *_: "work-a")
    monkeypatch.setattr(ol.omp_inspect, "live_omp_sessions", lambda *_: ([], True))
    assert ol.save_panes(target) is False
    assert target.read_text() == "preserve previous map\n"


def test_terminated_abduco_session_is_not_an_alive_restore_target(monkeypatch):
    from types import SimpleNamespace
    from riglib import omp_inspect as oi

    monkeypatch.setattr(
        oi.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=0,
            stdout="Active sessions\n+\tSun Sep 27 12:00:00\tended\n*\tSun Sep 27 12:00:00\talive\n",
        ),
    )
    names, ok = oi.list_abduco_sessions()
    assert ok and names == {"alive"}


def test_pane_restore_uses_absolute_rig_binary(tmp_path, monkeypatch):
    import os
    from riglib.omp_recovery import OriginalSession

    f = tmp_path / "session.jsonl"
    f.write_text('{"type":"session","id":"sid","cwd":"/work"}\n')
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(ol, "pane_current_command", lambda *_: "zsh")
    calls = []
    monkeypatch.setattr(ol, "send_keys", lambda *args: calls.append(args) or True)
    host = OriginalSession(123, "instance", "/work", "sid", "durable")
    plan = ol.restore_one_pane(
        "main:1",
        "/work",
        str(f),
        "durable",
        live=[host],
        live_ok=True,
        durable_sockets={"durable"},
        durable_ok=True,
    )
    assert plan.action == "attach"
    import shlex

    argv = shlex.split(calls[0][1].split(" && ", 1)[1])
    assert os.path.isabs(argv[0]) and Path(argv[0]).is_file()
    assert argv[1:] == ["harness", "attach", "durable"]


def test_missing_session_file_preserves_the_last_verified_map(tmp_path, monkeypatch):
    from types import SimpleNamespace

    target = tmp_path / "map"
    target.write_text("verified previous state\n")
    monkeypatch.setattr(ol, "list_tmux_panes", lambda *_: [("main:1", 11, "/work")])
    monkeypatch.setattr(
        ol.omp_inspect, "capture_ps_snapshot", lambda: "11 1 /bin/omp\n"
    )
    host = SimpleNamespace(
        pid=11,
        session_id="new",
        session_file=str(tmp_path / "not-written.jsonl"),
        cwd="/work",
        durable_session=None,
    )
    monkeypatch.setattr(ol.omp_inspect, "live_omp_sessions", lambda *_: ([host], True))
    assert ol.save_panes(target) is False
    assert target.read_text() == "verified previous state\n"


def test_unrecorded_server_with_missing_socket_is_never_replaced(tmp_path):
    import os, shutil, signal, subprocess, tempfile, time
    from shutil import which
    from riglib import tmux_watchdog as wd

    if not which("tmux") or not which("lsof"):
        pytest.skip("requires tmux and lsof for real socket ownership proof")
    # a unix socket path is limited to ~104 bytes on macOS; pytest's tmp dirs exceed it.
    socket_dir = Path(tempfile.mkdtemp(prefix="rigwd-", dir="/tmp"))
    socket = socket_dir / "tmux.sock"
    command = ["tmux", "-S", str(socket), "-f", "/dev/null"]

    def start():
        subprocess.run(
            [*command, "new-session", "-d", "-s", "private"],
            check=True,
            stdin=subprocess.DEVNULL,
        )
        return int(
            subprocess.check_output(
                [*command, "display-message", "-p", "#{pid}"], text=True
            )
        )

    a = start()
    identity = wd.query_server(socket)
    wd.save_identity(identity, tmp_path)
    subprocess.run([*command, "kill-server"], check=True)
    time.sleep(0.15)
    b = start()
    assert a != b
    try:
        socket.unlink()
        forbidden = tmp_path / "should-not-launch"
        boot = tmp_path / "boot.sh"
        boot.write_text('#!/bin/sh\ntouch "' + str(forbidden) + '"\n')
        boot.chmod(0o755)
        result = wd.run_once(socket, home=tmp_path, boot_script=boot)
        # Host lsof may conservatively report an incomplete scan (common on
        # containerized Linux runners). Both reasons are safe: neither may
        # replace an unverified server. The exact identity_changed path is
        # pinned separately by pure tests with controlled inspection.
        assert result.action == "blocked"
        assert result.reason in {"server_identity_changed", "inspection_unavailable"}
        assert not forbidden.exists()
    finally:
        os.kill(b, signal.SIGUSR1)
        for _ in range(20):
            if socket.exists():
                break
            time.sleep(0.05)
        subprocess.run([*command, "kill-server"], capture_output=True)
        shutil.rmtree(socket_dir, ignore_errors=True)


@pytest.mark.parametrize("rc", [0, 1])
def test_socket_scan_warning_is_incomplete_even_on_success(monkeypatch, tmp_path, rc):
    from types import SimpleNamespace
    from riglib import tmux_watchdog as wd
    monkeypatch.setattr(wd.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=rc, stdout="", stderr="lsof: WARNING: partial results"))
    with pytest.raises(wd.InspectionUnavailable):
        wd.socket_owner_pids(tmp_path / "tmux.sock")


def test_socket_decode_error_is_reported_as_unavailable(monkeypatch, tmp_path):
    from riglib import tmux_watchdog as wd
    monkeypatch.setattr(wd.subprocess, "run", Mock(side_effect=UnicodeDecodeError("utf8", b"\xff", 0, 1, "bad byte")))
    with pytest.raises(wd.InspectionUnavailable):
        wd.socket_owner_pids(tmp_path / "sock")
