"""Declarative watchdog wiring must be opt-in and execute before the no-server exit."""

from pathlib import Path
from riglib.tmux import build_tmux
from riglib.cli import build_parser


def test_recovery_cli_is_explicit_and_requires_socket():
    args = build_parser().parse_args(
        ["omp", "watchdog", "--socket", "/tmp/test.sock", "--json"]
    )
    assert args.omp_command == "watchdog" and args.socket_path == "/tmp/test.sock"


def test_recovery_enabled_renders_watchdog_before_inactive_exit(tmp_path):
    plan = build_tmux(repo_home=tmp_path, recovery={"enabled": True})
    text = plan.render_autosave_script()
    assert "omp watchdog" in text
    assert text.index("omp watchdog") < text.index("no server, nothing to save")
    assert "--boot-script" in text
    assert str(plan.boot_script_path) in text


def test_recovery_is_opt_in_and_never_overrides_an_unrelated_tmux_namespace(tmp_path):
    default = build_tmux(repo_home=tmp_path)
    assert "omp watchdog" not in default.render_autosave_script()
    enabled = build_tmux(repo_home=tmp_path, recovery={"enabled": True})
    assert "RIG_TMUX_RECOVERY_SOCKET" in enabled.render_boot_script()
    assert 'set -- -S "$RIG_TMUX_RECOVERY_SOCKET"' in enabled.render_boot_script()
