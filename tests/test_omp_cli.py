"""`rig omp` CLI wiring — the tmux pane save/restore executors.

Hosting sessions is `rig harness` (tests/test_harness_cli.py); `rig omp` keeps only
save-panes / restore-panes / watchdog.
"""

from __future__ import annotations

from riglib import omp_launcher as ol
from riglib.cli import main


def test_omp_bare_prints_help(capsys):
    rc = main(["omp"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "save-panes" in out and "restore-panes" in out


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
