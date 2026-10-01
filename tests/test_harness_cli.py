"""`rig harness …` — argparse wiring, option splitting and exit codes.

The process-replacing subcommands never exec here: the `riglib.harness_launcher` functions are
monkeypatched, so the CLI layer's argv/exit-code wiring is exercised without touching a process
image. The error exits (missing abduco, bad names) go through the real launcher.
"""

from __future__ import annotations

import json
import stat

import pytest

from riglib import errors
from riglib import harness_launcher as hl
from riglib.cli import main
from riglib.harness_cli import split_run_options


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("RIG_OMP_ABDUCO_DIR", str(tmp_path / "abduco"))
    monkeypatch.setenv("RIG_CONFIG_DIR", str(tmp_path / "rigcfg"))
    monkeypatch.setenv("TG_CTL_CONFIG_DIR", str(tmp_path / "tg"))


@pytest.fixture
def run_calls(monkeypatch):
    calls = []

    def fake_run_session(kind, args, **kwargs):
        calls.append((kind, args, kwargs))
        return kwargs.get("host_id") or f"{kind}-generated"

    monkeypatch.setattr(hl, "run_session", fake_run_session)
    return calls


# ── split_run_options ────────────────────────────────────────────────────────────────────────
def test_split_run_options_takes_leading_rig_options_then_the_harness_args():
    opts, rest = split_run_options(["--name", "L", "--host-id", "X", "--resume", "F"])
    assert opts == {"label": "L", "host_id": "X"}
    assert rest == ["--resume", "F"]


def test_split_run_options_accepts_equals_form_flags_and_paths():
    opts, rest = split_run_options(["--name=L", "--detach", "--real-bin", "/bin/omp", "--abduco-bin=/x/abduco", "hi"])
    assert opts == {"label": "L", "detach": True, "real_bin": "/bin/omp", "abduco_bin": "/x/abduco"}
    assert rest == ["hi"]


def test_split_run_options_stops_at_the_first_harness_argument():
    # `--name` AFTER the harness's own args belongs to the harness (claude has its own --name).
    assert split_run_options(["--resume", "F", "--name", "L"]) == ({}, ["--resume", "F", "--name", "L"])
    assert split_run_options(["hello", "--name", "L"]) == ({}, ["hello", "--name", "L"])


def test_split_run_options_leaves_a_valueless_trailing_option_to_the_harness():
    assert split_run_options(["--name"]) == ({}, ["--name"])


def test_split_run_options_bare_separator_ends_the_options():
    assert split_run_options(["--name", "L", "--", "--name", "x"]) == ({"label": "L"}, ["--name", "x"])


# ── rig harness (bare) / argparse wiring ─────────────────────────────────────────────────────
def test_bare_harness_prints_help(capsys):
    assert main(["harness"]) == 0
    out = capsys.readouterr().out
    for verb in ("run", "attach", "list", "install-wrapper"):
        assert verb in out


def test_run_rejects_an_unknown_harness(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["harness", "run", "vim"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


# ── rig harness run ──────────────────────────────────────────────────────────────────────────
def test_run_omp_with_name_host_id_and_resume(run_calls, capsys):
    rc = main(["harness", "run", "omp", "--name", "L", "--host-id", "X", "--resume", "F"])
    assert rc == 0
    (kind, args, kwargs), = run_calls
    assert (kind, args) == ("omp", ["--resume", "F"])
    assert kwargs == {"label": "L", "host_id": "X", "detach": False, "real_bin": None, "abduco_bin": "abduco"}
    assert capsys.readouterr().out.strip() == "X"


def test_run_accepts_options_before_the_harness_name(run_calls):
    assert main(["harness", "run", "--name", "L", "--detach", "omp", "--resume", "F"]) == 0
    (_kind, args, kwargs), = run_calls
    assert args == ["--resume", "F"] and kwargs["label"] == "L" and kwargs["detach"] is True


def test_run_without_options_passes_everything_to_the_harness(run_calls):
    assert main(["harness", "run", "claude", "--model", "opus", "fix the bug"]) == 0
    (kind, args, kwargs), = run_calls
    assert kind == "claude" and args == ["--model", "opus", "fix the bug"]
    assert kwargs["label"] is None and kwargs["host_id"] is None and kwargs["detach"] is False


def test_run_separator_after_rig_options_is_dropped(run_calls):
    assert main(["harness", "run", "--name", "L", "omp", "--", "acp"]) == 0
    (_kind, args, kwargs), = run_calls
    assert args == ["acp"] and kwargs["label"] == "L"


def test_a_bare_separator_hands_the_harness_its_own_name_flag(run_calls):
    """Claude Code has its own `--name`; after `--` it reaches claude, before it it is the label."""
    assert main(["harness", "run", "claude", "--", "--name", "x"]) == 0
    assert main(["harness", "run", "--name", "L", "claude", "--", "--name", "x"]) == 0
    assert main(["harness", "run", "claude", "--name", "L", "--", "--name", "x", "fix it"]) == 0
    first, second, third = run_calls
    assert (first[1], first[2]["label"]) == (["--name", "x"], None)
    assert (second[1], second[2]["label"]) == (["--name", "x"], "L")
    assert (third[1], third[2]["label"]) == (["--name", "x", "fix it"], "L")


def test_run_forwards_the_abduco_and_real_binaries(run_calls):
    assert main(["harness", "run", "codex", "--abduco-bin", "/x/abduco", "--real-bin", "/x/codex"]) == 0
    (_kind, _args, kwargs), = run_calls
    assert kwargs["abduco_bin"] == "/x/abduco" and kwargs["real_bin"] == "/x/codex"


def test_run_missing_abduco_reports_missing_dep(tmp_path, capsys):
    fake = tmp_path / "omp"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    for extra in ([], ["--detach"]):
        rc = main(
            ["harness", "run", "omp", "--abduco-bin", str(tmp_path / "no-abduco"), "--real-bin", str(fake), *extra]
        )
        assert rc == errors.EXIT_MISSING_DEP == 127
        assert "abduco" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["../bad", "--x", "a b", "/abs"])
def test_run_with_an_invalid_host_id_is_a_clean_config_error(bad, capsys):
    rc = main(["harness", "run", "omp", f"--host-id={bad}", "--real-bin", "/opt/omp"])
    assert rc == errors.EXIT_CONFIG == 2
    assert "invalid harness host name" in capsys.readouterr().err


def test_run_reports_a_launcher_runtime_error_as_config_error(monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("no real omp executable found on PATH")

    monkeypatch.setattr(hl, "run_session", boom)
    assert main(["harness", "run", "omp"]) == errors.EXIT_CONFIG
    assert "no real omp executable" in capsys.readouterr().err


# ── attach / list ────────────────────────────────────────────────────────────────────────────
def test_attach_invokes_attach_session(monkeypatch):
    calls = []
    monkeypatch.setattr(hl, "attach_session", lambda name, abduco_bin="abduco": calls.append((name, abduco_bin)))
    assert main(["harness", "attach", "omp-x1"]) == 0
    assert calls == [("omp-x1", "abduco")]


def test_attach_with_an_invalid_name_is_a_clean_config_error(capsys):
    assert main(["harness", "attach", "../bad"]) == errors.EXIT_CONFIG
    assert "invalid harness host name" in capsys.readouterr().err


def test_attach_missing_abduco_reports_missing_dep(tmp_path, capsys):
    rc = main(["harness", "attach", "omp-x1", "--abduco-bin", str(tmp_path / "no-abduco")])
    assert rc == errors.EXIT_MISSING_DEP
    assert "abduco" in capsys.readouterr().err


def test_list_json(monkeypatch, capsys):
    rows = [{"name": "omp-a", "attached": True, "terminated": False, "created": "x", "harness": "omp"}]
    monkeypatch.setattr(hl, "list_sessions", lambda abduco_bin="abduco": (rows, True))
    assert main(["harness", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == rows


def test_list_text_marks_attached_and_terminated_sessions(monkeypatch, capsys):
    rows = [
        {"name": "omp-a", "attached": True, "terminated": False, "created": "t1", "harness": "omp", "label": "L", "cwd": "/w"},
        {"name": "claude-b", "attached": False, "terminated": True, "created": "t2"},
        {"name": "codex-c", "attached": False, "terminated": False, "created": "t3"},
    ]
    monkeypatch.setattr(hl, "list_sessions", lambda abduco_bin="abduco": (rows, True))
    assert main(["harness", "list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "* omp-a\tt1\tomp\tL\t/w"
    assert lines[1].startswith("+ claude-b\tt2")
    assert lines[2].startswith("  codex-c\tt3")


def test_list_with_no_sessions_says_so(monkeypatch, capsys):
    monkeypatch.setattr(hl, "list_sessions", lambda abduco_bin="abduco": ([], True))
    assert main(["harness", "list"]) == 0
    assert "no hosted harness sessions" in capsys.readouterr().out


def test_list_when_abduco_cannot_be_inspected_is_missing_dep(monkeypatch):
    monkeypatch.setattr(hl, "list_sessions", lambda abduco_bin="abduco": ([], False))
    assert main(["harness", "list"]) == errors.EXIT_MISSING_DEP


# ── install-wrapper / exec / host ────────────────────────────────────────────────────────────
def test_install_wrapper_writes_the_script_and_records_the_real_bin(tmp_path, capsys):
    real = tmp_path / "real-omp"
    real.write_text("#!/bin/sh\n")
    target = tmp_path / "bin" / "omp"
    rc = main(["harness", "install-wrapper", "omp", "--target", str(target), "--real-bin", str(real)])
    assert rc == 0
    assert target.read_text() == hl.wrapper_text("omp")
    assert target.stat().st_mode & stat.S_IXUSR
    assert json.loads((tmp_path / "rigcfg" / "harness-real-bin.json").read_text()) == {"omp": str(real.resolve())}
    assert "not activated" in capsys.readouterr().out


def test_install_wrapper_refuses_to_overwrite_a_foreign_file(tmp_path, capsys):
    target = tmp_path / "omp"
    target.write_text("#!/bin/sh\necho vendor\n")
    rc = main(["harness", "install-wrapper", "omp", "--target", str(target)])
    assert rc == errors.EXIT_CONFIG
    assert "unmanaged" in capsys.readouterr().err
    assert target.read_text() == "#!/bin/sh\necho vendor\n"


def test_exec_dispatches_to_exec_or_wrap_after_the_separator(monkeypatch):
    calls = []
    monkeypatch.setattr(hl, "exec_or_wrap", lambda kind, args, **kw: calls.append((kind, args)))
    assert main(["harness", "exec", "omp", "--", "-p", "hello"]) == 0
    assert calls == [("omp", ["-p", "hello"])]


def test_exec_keeps_harness_flags_that_look_like_rig_options(monkeypatch):
    calls = []
    monkeypatch.setattr(hl, "exec_or_wrap", lambda kind, args, **kw: calls.append((kind, args)))
    assert main(["harness", "exec", "claude", "--", "--name", "x"]) == 0
    assert calls == [("claude", ["--name", "x"])]


def test_exec_without_a_real_binary_is_a_config_error(monkeypatch, capsys):
    def boom(kind, args, **kw):
        raise RuntimeError("no real omp executable found on PATH (wrappers are skipped)")

    monkeypatch.setattr(hl, "exec_or_wrap", boom)
    assert main(["harness", "exec", "omp", "--", "-p", "hi"]) == errors.EXIT_CONFIG
    assert "no real omp executable" in capsys.readouterr().err


def test_host_without_a_spec_is_a_usage_error_not_a_crash(monkeypatch, capsys):
    monkeypatch.delenv("RIG_HOST_SPEC", raising=False)
    assert main(["harness", "host", "omp-x1"]) == 2
    assert "RIG_HOST_SPEC" in capsys.readouterr().err

