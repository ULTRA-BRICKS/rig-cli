"""`rig env set|list|del` — manage global `env.vars` keys without dumping secrets.

Covers the user-facing surface on top of `config set --global env.vars.<KEY>`: values stay
strings (no scalar coercion), stdout masks secrets, stdin supplies a value so it never lands
in argv, and `del` uses `config.del_path`. Reconcile is mocked at `run_plan` so tests never
touch a real shell rc.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
import yaml

from riglib import config
from riglib.cli import main


def _w(p: Path, s: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(s, encoding="utf-8")


def _repo_yaml(fake_agent_tools: Path) -> str:
    return (
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nci: {enabled: false}\n"
    )


@pytest.fixture
def _mock_apply(monkeypatch):
    """Same seam `_cmd_config_set` uses: `from .actions import run_plan`."""
    import riglib.actions as actions_pkg
    from riglib.actions.runner import ApplyReport

    calls: list = []

    def _fake_run_plan(plan, **_kwargs):
        calls.append(plan)
        return ApplyReport(results=[])

    monkeypatch.setattr(actions_pkg, "run_plan", _fake_run_plan)
    return calls


def _global_repo(tmp_path: Path, fake_agent_tools: Path, monkeypatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    # env set/del load only the global layer (no non-git repo autodiscovery), so the
    # catalog pin must live here — not only in ./rig.yaml.
    _w(
        config.global_config_path(),
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n",
    )
    repo = tmp_path / "repo"
    _w(repo / "rig.yaml", _repo_yaml(fake_agent_tools))
    return repo



def test_env_set_writes_string_and_masks_stdout(
    tmp_path, capsys, fake_agent_tools, monkeypatch, _mock_apply
):
    repo = _global_repo(tmp_path, fake_agent_tools, monkeypatch)
    value = "YOUR_KEY_HERE"

    rc = main(["env", "set", "EXAMPLE_VAR", value, "-C", str(repo)])

    assert rc == 0
    gdata = yaml.safe_load(config.global_config_path().read_text(encoding="utf-8"))
    assert gdata["env"]["vars"]["EXAMPLE_VAR"] == value
    out = capsys.readouterr().out
    assert value not in out
    assert "EXAMPLE_VAR" in out
    assert "HERE" in out
    assert len(_mock_apply) == 1


def test_env_set_true_stays_a_string(
    tmp_path, fake_agent_tools, monkeypatch, _mock_apply
):
    repo = _global_repo(tmp_path, fake_agent_tools, monkeypatch)
    rc = main(["env", "set", "COLORTERM", "true", "-C", str(repo)])
    assert rc == 0
    gdata = yaml.safe_load(config.global_config_path().read_text(encoding="utf-8"))
    assert gdata["env"]["vars"]["COLORTERM"] == "true"


def test_env_set_preview_writes_nothing(
    tmp_path, capsys, fake_agent_tools, monkeypatch, _mock_apply
):
    repo = _global_repo(tmp_path, fake_agent_tools, monkeypatch)
    gpath = config.global_config_path()
    before = gpath.read_text(encoding="utf-8")
    rc = main(
        ["env", "set", "EXAMPLE_VAR", "YOUR_KEY_HERE", "-C", str(repo), "--preview"]
    )
    assert rc == 0
    assert gpath.read_text(encoding="utf-8") == before
    assert _mock_apply == []
    out = capsys.readouterr().out
    assert "YOUR_KEY_HERE" not in out
    assert "preview" in out.lower()



def test_env_set_reads_value_from_stdin(
    tmp_path, capsys, fake_agent_tools, monkeypatch, _mock_apply
):
    repo = _global_repo(tmp_path, fake_agent_tools, monkeypatch)
    monkeypatch.setattr("sys.stdin", io.StringIO("piped-from-stdin\n"))
    rc = main(["env", "set", "EXAMPLE_VAR", "-C", str(repo)])
    assert rc == 0
    gdata = yaml.safe_load(config.global_config_path().read_text(encoding="utf-8"))
    assert gdata["env"]["vars"]["EXAMPLE_VAR"] == "piped-from-stdin"
    assert "piped-from-stdin" not in capsys.readouterr().out


def test_env_set_tty_without_value_errors(
    tmp_path, capsys, fake_agent_tools, monkeypatch, _mock_apply
):
    repo = _global_repo(tmp_path, fake_agent_tools, monkeypatch)
    gpath = config.global_config_path()
    before = gpath.read_text(encoding="utf-8")

    class _Tty:
        def isatty(self) -> bool:
            return True

        def read(self, *args, **kwargs) -> str:  # noqa: ARG002
            raise AssertionError("must not read a TTY for the value")

    monkeypatch.setattr("sys.stdin", _Tty())
    rc = main(["env", "set", "EXAMPLE_VAR", "-C", str(repo)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "missing value" in err.lower() or "value" in err.lower()
    assert gpath.read_text(encoding="utf-8") == before
    assert _mock_apply == []



def test_env_set_rejects_invalid_key(
    tmp_path, capsys, fake_agent_tools, monkeypatch, _mock_apply
):
    repo = _global_repo(tmp_path, fake_agent_tools, monkeypatch)
    gpath = config.global_config_path()
    before = gpath.read_text(encoding="utf-8")
    rc = main(["env", "set", "FOO-BAR", "x", "-C", str(repo)])
    assert rc == 2
    assert "FOO-BAR" in capsys.readouterr().err
    assert gpath.read_text(encoding="utf-8") == before
    assert _mock_apply == []



def test_env_list_masks_and_json(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    _w(
        config.global_config_path(),
        "version: 1\nenv:\n  vars:\n    EXAMPLE_VAR: YOUR_KEY_HERE\n    SHORT: ab\n",
    )
    plain = tmp_path / "plain"
    plain.mkdir()

    rc = main(["env", "list", "-C", str(plain)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "YOUR_KEY_HERE" not in out
    assert "EXAMPLE_VAR=" in out
    assert "HERE" in out
    assert "SHORT=" in out
    assert "ab" not in out.split("SHORT=", 1)[1].splitlines()[0]

    rc = main(["env", "list", "--json", "-C", str(plain)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["EXAMPLE_VAR"] != "YOUR_KEY_HERE"
    assert payload["EXAMPLE_VAR"].endswith("HERE")
    assert payload["SHORT"] != "ab"


def test_env_list_show_prints_full_values(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    _w(
        config.global_config_path(),
        "version: 1\nenv:\n  vars:\n    EXAMPLE_VAR: YOUR_KEY_HERE\n",
    )
    rc = main(["env", "list", "--show", "-C", str(tmp_path)])
    assert rc == 0
    assert "YOUR_KEY_HERE" in capsys.readouterr().out


def test_env_list_empty_when_no_vars(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    rc = main(["env", "list", "--json", "-C", str(tmp_path)])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {}


def test_env_del_removes_key(
    tmp_path, capsys, fake_agent_tools, monkeypatch, _mock_apply
):
    repo = _global_repo(tmp_path, fake_agent_tools, monkeypatch)
    _w(
        config.global_config_path(),
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "env:\n  vars:\n    EXAMPLE_VAR: YOUR_KEY_HERE\n    KEEP: keep-me\n",
    )

    rc = main(["env", "del", "EXAMPLE_VAR", "-C", str(repo)])
    assert rc == 0
    gdata = yaml.safe_load(config.global_config_path().read_text(encoding="utf-8"))
    assert "EXAMPLE_VAR" not in gdata["env"]["vars"]
    assert gdata["env"]["vars"]["KEEP"] == "keep-me"
    out = capsys.readouterr().out
    assert "YOUR_KEY_HERE" not in out
    assert len(_mock_apply) == 1


def test_env_del_missing_is_fail_closed(
    tmp_path, capsys, fake_agent_tools, monkeypatch, _mock_apply
):
    repo = _global_repo(tmp_path, fake_agent_tools, monkeypatch)
    original = "version: 1\nenv:\n  vars:\n    KEEP: keep-me\n"
    gpath = config.global_config_path()
    _w(gpath, original)
    rc = main(["env", "del", "EXAMPLE_VAR", "-C", str(repo)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not found" in err.lower() or "EXAMPLE_VAR" in err
    assert gpath.read_text(encoding="utf-8") == original
    assert _mock_apply == []


def test_env_set_works_outside_a_git_repo(
    tmp_path, capsys, fake_agent_tools, monkeypatch, _mock_apply
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    _w(
        config.global_config_path(),
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n",
    )
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    rc = main(["env", "set", "EXAMPLE_VAR", "YOUR_KEY_HERE", "-C", str(plain)])
    assert rc == 0
    gdata = yaml.safe_load(config.global_config_path().read_text(encoding="utf-8"))
    assert gdata["env"]["vars"]["EXAMPLE_VAR"] == "YOUR_KEY_HERE"
    assert "YOUR_KEY_HERE" not in capsys.readouterr().out
    assert len(_mock_apply) == 1



def test_env_help_lists_subcommands(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["env", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "set" in out and "list" in out and "del" in out
