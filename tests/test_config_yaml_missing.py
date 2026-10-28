"""Reading rig config without PyYAML is an actionable missing-dependency error, not a traceback.

``yaml`` is imported lazily so ``rig --help``/``doctor`` run without it; every command that does read
a config must then fail with the structured what/why/fix block and exit 127 (the missing-dependency
class), never a raw ``ModuleNotFoundError``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from riglib import config_yaml
from riglib.config_harness_fanout import load_harness_fan_out
from riglib.errors import EXIT_MISSING_DEP, MissingDepError

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def no_yaml(monkeypatch):
    """``import yaml`` raises ImportError (a ``None`` entry in sys.modules blocks the import)."""
    monkeypatch.setitem(sys.modules, "yaml", None)


def test_read_yaml_file_raises_the_missing_dependency_error(no_yaml, tmp_path):
    cfg = tmp_path / "rig.yaml"
    cfg.write_text("version: 1\n")
    with pytest.raises(MissingDepError) as info:
        config_yaml.read_yaml_file(cfg)
    err = info.value
    assert err.exit_code == EXIT_MISSING_DEP == 127
    assert "PyYAML" in err.what
    assert "pip install pyyaml" in err.fix and "reinstall rig" in err.fix


def test_doctor_fan_out_still_falls_back_to_the_stdlib_reader(no_yaml, tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "rig.yaml").write_text("harness:\n  discover_config_dirs: false\n")
    assert load_harness_fan_out(repo) == {"discover_config_dirs": False}


def test_cli_without_pyyaml_prints_the_block_and_exits_127(tmp_path):
    # A real interpreter where `import yaml` fails: a shadowing yaml.py that raises ImportError.
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "yaml.py").write_text("raise ImportError('No module named yaml')\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "rig.yaml").write_text("version: 1\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    env = {**os.environ, "PYTHONPATH": str(shadow), "HOME": str(tmp_path / "home"), "XDG_CONFIG_HOME": str(tmp_path / "xdg")}
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "bin" / "rig"), "status"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 127, proc.stdout + proc.stderr
    output = proc.stdout + proc.stderr
    assert "Traceback" not in output
    assert "PyYAML is required" in output
    assert "pip install pyyaml" in output
