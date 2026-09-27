"""`rig omp` CLI wiring — the durable launcher + pane save/restore commands.

The process-replacing subcommands (run/attach/exec) never actually exec in these tests: the
underlying `riglib.omp_launcher` functions are monkeypatched so the CLI layer's argv/exit-code
wiring is exercised without touching a real process image.
"""

from __future__ import annotations

import json

from riglib import omp_launcher as ol
from riglib.cli import main


def test_omp_bare_prints_help(capsys):
    rc = main(["omp"])
    assert rc == 0
    assert "run" in capsys.readouterr().out


def test_omp_run_invokes_run_session(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ol,
        "run_session",
        lambda argv, session_id=None, abduco_bin="abduco": calls.append(
            (argv, session_id, abduco_bin)
        ),
    )
    rc = main(["omp", "run", "--session-id", "sid-1", "omp", "--resume", "/x.jsonl"])
    assert rc == 0
    assert calls == [(["omp", "--resume", "/x.jsonl"], "sid-1", "abduco")]


def test_omp_run_strips_leading_separator(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ol,
        "run_session",
        lambda argv, session_id=None, abduco_bin="abduco": calls.append(argv),
    )
    main(["omp", "run", "--", "omp", "acp"])
    assert calls == [["omp", "acp"]]


def test_omp_run_resume_shorthand_builds_omp_argv(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ol,
        "run_session",
        lambda argv, session_id=None, abduco_bin="abduco": calls.append(argv),
    )
    main(["omp", "run", "--resume", "/x.jsonl"])
    assert calls == [["omp", "--resume", "/x.jsonl"]]


def test_omp_run_resume_shorthand_honors_omp_bin(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ol,
        "run_session",
        lambda argv, session_id=None, abduco_bin="abduco": calls.append(argv),
    )
    main(["omp", "run", "--resume", "/x.jsonl", "--omp-bin", "/custom/omp"])
    assert calls == [["/custom/omp", "--resume", "/x.jsonl"]]


def test_omp_run_without_argv_is_config_error(capsys):
    from riglib import errors

    rc = main(["omp", "run"])
    assert rc == errors.EXIT_CONFIG
    assert "needs the OMP command" in capsys.readouterr().out


def test_omp_run_missing_abduco_binary_reports_missing_dep(monkeypatch, capsys):
    from riglib import errors

    def raise_missing(argv, session_id=None, abduco_bin="abduco"):
        raise FileNotFoundError(abduco_bin)

    monkeypatch.setattr(ol, "run_session", raise_missing)
    rc = main(["omp", "run", "omp"])
    assert rc == errors.EXIT_MISSING_DEP
    assert "abduco" in capsys.readouterr().out


def test_omp_attach_invokes_attach_session(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ol,
        "attach_session",
        lambda sid, abduco_bin="abduco": calls.append((sid, abduco_bin)),
    )
    rc = main(["omp", "attach", "sid-1"])
    assert rc == 0
    assert calls == [("sid-1", "abduco")]


def test_omp_list_json(monkeypatch, capsys):
    monkeypatch.setattr(
        ol,
        "list_sessions",
        lambda abduco_bin="abduco": (
            [{"name": "a", "attached": True, "terminated": False, "created": "x"}],
            True,
        ),
    )
    rc = main(["omp", "list", "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == [
        {"name": "a", "attached": True, "terminated": False, "created": "x"}
    ]


def test_omp_list_missing_binary_is_missing_dep(monkeypatch):
    from riglib import errors

    monkeypatch.setattr(ol, "list_sessions", lambda abduco_bin="abduco": ([], False))
    rc = main(["omp", "list"])
    assert rc == errors.EXIT_MISSING_DEP


def test_omp_save_panes_wires_map_path(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        ol,
        "save_panes",
        lambda path, tmux_bin="tmux", tg_ctl_bin="tg-ctl": (
            calls.append((path, tmux_bin)) or True
        ),
    )
    map_path = tmp_path / "m"
    rc = main(["omp", "save-panes", "--map", str(map_path)])
    assert rc == 0
    assert calls == [(map_path, "tmux")]


def test_omp_save_panes_failure_exits_nonzero(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ol, "save_panes", lambda path, tmux_bin="tmux", tg_ctl_bin="tg-ctl": False
    )
    rc = main(["omp", "save-panes", "--map", str(tmp_path / "m")])
    assert rc == 1


def test_omp_restore_panes_reports_blocked_count(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        ol,
        "restore_panes",
        lambda path, tmux_bin="tmux", tg_ctl_bin="tg-ctl", rig_bin=None: 2,
    )
    rc = main(["omp", "restore-panes", "--map", str(tmp_path / "m")])
    assert (
        rc == 0
    )  # never fails the whole run — panes are individually blocked, not the script
    assert "2 pane" in capsys.readouterr().out


def test_omp_install_wrapper(tmp_path):
    real_bin = tmp_path / "real-omp"
    real_bin.write_text("#!/bin/sh\n")
    target = tmp_path / "bin" / "omp"
    config = tmp_path / "cfg.json"
    rc = main(
        [
            "omp",
            "install-wrapper",
            "--target",
            str(target),
            "--real-bin",
            str(real_bin),
            "--config",
            str(config),
        ]
    )
    assert rc == 0
    assert target.exists()
    assert json.loads(config.read_text())["real_bin"] == str(real_bin)


def test_omp_exec_dispatches_to_exec_or_wrap(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ol,
        "exec_or_wrap",
        lambda argv, config_path=None: calls.append((argv, config_path)),
    )
    rc = main(["omp", "exec", "--", "-p", "hello"])
    assert rc == 0
    assert calls == [(["-p", "hello"], None)]


def test_omp_exec_missing_config_is_config_error(monkeypatch, capsys):
    from riglib import errors

    def raise_runtime(argv, config_path=None):
        raise RuntimeError("no real OMP binary recorded")

    monkeypatch.setattr(ol, "exec_or_wrap", raise_runtime)
    rc = main(["omp", "exec", "--", "-p", "hi"])
    assert rc == errors.EXIT_CONFIG
    assert "no real OMP binary" in capsys.readouterr().out


def test_save_panes_forwards_the_selected_live_bridge(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        ol, "save_panes", lambda path, **kwargs: calls.append(kwargs) or True
    )
    assert (
        main(
            [
                "omp",
                "save-panes",
                "--map",
                str(tmp_path / "m"),
                "--tg-ctl-bin",
                "/custom/tg-ctl",
            ]
        )
        == 0
    )
    assert calls[0]["tg_ctl_bin"] == "/custom/tg-ctl"
