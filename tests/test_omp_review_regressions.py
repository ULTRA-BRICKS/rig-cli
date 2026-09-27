"""Final independent review: launcher boundaries and single-snapshot map parsing."""

import json
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


def test_wrapper_config_cannot_point_back_to_a_managed_wrapper(tmp_path):
    target = tmp_path / "wrapper"
    target.write_text(ol.wrapper_script_text())
    target.chmod(0o755)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"real_bin": str(target)}))
    execute = Mock()
    with pytest.raises(RuntimeError):
        ol.exec_or_wrap([], config_path=config, exec_fn=execute)
    execute.assert_not_called()


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


def test_installer_refuses_an_existing_native_binary_without_decoding_or_writing(
    tmp_path,
):
    target = tmp_path / "omp"
    original = b"\xcf\xfa\xed\xfe\xffNATIVE"
    target.write_bytes(original)
    vendor = tmp_path / "vendor"
    vendor.write_text("#!/bin/sh\nexit 0\n")
    with pytest.raises(ValueError, match="unmanaged"):
        ol.install_wrapper(target, str(vendor), tmp_path / "config.json")
    assert target.read_bytes() == original
    assert not (tmp_path / "config.json").exists()


@pytest.mark.parametrize("value", [None, [], 42, "text"])
def test_non_object_wrapper_config_returns_no_executable(tmp_path, value):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(value))
    assert ol.read_wrapper_real_bin(path) is None


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


def test_resume_shorthand_rejects_a_second_command(monkeypatch, capsys):
    from riglib.cli import main

    called = Mock()
    monkeypatch.setattr(ol, "run_session", called)
    assert main(["omp", "run", "--resume", "/x.jsonl", "--", "omp", "acp"]) == 2
    called.assert_not_called()
    assert "cannot combine" in capsys.readouterr().out.lower()


@pytest.mark.parametrize(
    "verb,args,method",
    [
        ("run", ["--session-id", "../bad", "omp"], "run_session"),
        ("attach", ["../bad"], "attach_session"),
    ],
)
def test_invalid_durable_id_is_a_clean_cli_error(
    monkeypatch, capsys, verb, args, method
):
    from riglib.cli import main

    monkeypatch.setattr(ol, method, Mock(side_effect=ValueError("invalid durable id")))
    assert main(["omp", verb, *args]) == 2
    assert "invalid durable id" in capsys.readouterr().out


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


def test_replacement_launch_recycles_only_terminated_abduco_sessions():
    argv = ol.build_run_argv(["omp"], "durable")
    assert "-f" in argv and "-A" in argv


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
