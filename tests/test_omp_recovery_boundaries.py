"""Coordinator regressions for exact session identity and fail-closed supervision (#619)."""

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from riglib import omp_launcher as ol, omp_inspect as oi, tmux_watchdog as wd
from riglib.omp_recovery import OriginalSession, ServerIdentity


def test_save_two_sessions_in_same_cwd_uses_original_identity_not_mtime(
    tmp_path, monkeypatch
):
    cwd = str(tmp_path / "same")
    Path(cwd).mkdir()
    files = []
    for sid in ["first", "second"]:
        p = tmp_path / (sid + ".jsonl")
        p.write_text(json.dumps({"type": "session", "id": sid, "cwd": cwd}) + "\n")
        files.append(p)
    hosts = [
        SimpleNamespace(
            pid=11 + i,
            instance_id=str(i),
            cwd=cwd,
            session_id=sid,
            session_file=str(files[i]),
            durable_session=None,
        )
        for i, sid in enumerate(["first", "second"])
    ]
    monkeypatch.setattr(
        ol, "list_tmux_panes", lambda *_: [("m:1", 11, cwd), ("m:2", 12, cwd)]
    )
    monkeypatch.setattr(
        oi, "capture_ps_snapshot", lambda: "11 1 /bin/omp\n12 1 /bin/omp\n"
    )
    monkeypatch.setattr(oi, "live_omp_sessions", lambda *_: (hosts, True))
    target = tmp_path / "map"
    assert ol.save_panes(target)
    rows = ol.read_map(target)
    assert [r[2] for r in rows] == [str(p) for p in files]


def test_unidentified_live_omp_preserves_prior_map(tmp_path, monkeypatch):
    target = tmp_path / "map"
    target.write_text("prior-map\n")
    monkeypatch.setattr(ol, "list_tmux_panes", lambda *_: [("m:1", 11, "/work")])
    monkeypatch.setattr(oi, "capture_ps_snapshot", lambda: "11 1 /bin/omp\n")
    monkeypatch.setattr(
        oi,
        "live_omp_sessions",
        lambda *_: ([OriginalSession(11, "p", "/work", None, None)], True),
    )
    assert ol.save_panes(target) is False
    assert target.read_text() == "prior-map\n"


def test_empty_bridge_does_not_hide_live_legacy_process(monkeypatch):
    unknown = OriginalSession(11, "p", "/work", None, None)
    monkeypatch.setattr(oi, "query_tg_ctl_omp_live", lambda *_: ([], True))
    monkeypatch.setattr(oi, "capture_ps_snapshot", lambda: "11 1 /bin/omp\n")
    monkeypatch.setattr(oi, "local_live_omp_sessions", lambda _: ([unknown], True))
    hosts, ok = oi.live_omp_sessions()
    assert ok and hosts == [unknown]


def test_saved_identity_is_header_session_not_durable_name(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text(
        json.dumps({"type": "session", "id": "real-session", "cwd": "/x"}) + "\n"
    )
    assert oi.saved_session_id_for(str(p)) == "real-session"


def test_failed_cwd_inspection_of_live_pid_is_not_discarded(monkeypatch):
    monkeypatch.setattr(oi, "lsof_available", lambda: True)
    monkeypatch.setattr(oi, "process_cwd", lambda *a: None)
    rows = [oi.PsRow(os.getpid(), 1, "/bin/omp")]
    _, ok = oi.local_live_omp_sessions(rows)
    assert not ok


@pytest.mark.parametrize("sid", ["../escape", "/absolute", "--flag", "x\ny"])
def test_durable_identifiers_cannot_escape_socket_namespace(sid):
    with pytest.raises(ValueError):
        ol.build_attach_argv(sid)


def test_wrapper_refuses_unmanaged_symlink_without_touching_vendor(tmp_path):
    vendor = tmp_path / "vendor"
    vendor.write_text("VENDOR")
    target = tmp_path / "omp"
    target.symlink_to(vendor)
    with pytest.raises(ValueError):
        ol.install_wrapper(target, str(vendor), tmp_path / "config")
    assert vendor.read_text() == "VENDOR" and target.is_symlink()


def test_existing_path_is_not_proof_tmux_responds(tmp_path):
    sock = tmp_path / "socket"
    sock.write_text("not a server")
    plan = wd.run_once(sock, home=tmp_path)
    assert plan.reason != "server_healthy"


def test_saved_non_tmux_process_is_never_signalled(tmp_path, monkeypatch):
    fake = ServerIdentity(
        os.getpid(), os.getuid(), "/bin/python", "same-start", str(tmp_path / "missing")
    )
    wd.save_identity(fake, tmp_path)
    monkeypatch.setattr(wd, "probe_saved_identity", lambda *_: fake)
    signal = Mock(return_value=True)
    monkeypatch.setattr(wd, "recover_socket", signal)
    plan = wd.run_once(tmp_path / "missing", home=tmp_path)
    assert plan.action == "blocked"
    signal.assert_not_called()


def test_restore_executor_uses_recorded_header_id_and_attaches_same_owner(
    tmp_path, monkeypatch
):
    file = tmp_path / "s.jsonl"
    file.write_text(
        json.dumps({"type": "session", "id": "exact", "cwd": str(tmp_path)}) + "\n"
    )
    host = OriginalSession(31, "live", str(tmp_path), "exact", "omp-live", str(file))
    monkeypatch.setattr(ol, "_pane_is_empty_shell", lambda *a: True)
    send = Mock(return_value=True)
    monkeypatch.setattr(ol, "send_keys", send)
    result = ol.restore_one_pane(
        "test:0",
        str(tmp_path),
        str(file),
        "omp-live",
        live=[host],
        live_ok=True,
        durable_sockets={"omp-live"},
        durable_ok=True,
        session_id="exact",
    )
    assert result.action == "attach" and result.owner_pid == 31
    assert "rig omp attach omp-live" in send.call_args.args[1]


def test_unknown_owner_diagnostic_keeps_the_exact_blocking_pid():
    from riglib.omp_recovery import SavedPane, plan_restore

    result = plan_restore(
        SavedPane("x:0", "/work", "saved", "/file"),
        [OriginalSession(42, "live", "/work", None, None)],
        set(),
        inspection_ok=True,
        session_file_exists=True,
        pane_is_empty_shell=True,
    )
    assert result.action == "blocked" and result.owner_pid == 42
