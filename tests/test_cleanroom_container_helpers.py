"""Hermetic unit tests for the Apple-container clean-room matrix (no container required).

``test_cleanroom_container_e2e.py`` is opt-in (``RIG_CONTAINER_E2E=1``) and skip-only without
Apple's ``container`` CLI, so its embedded assert/parity/live scripts would otherwise never
compile in the default suite. These tests import that module (the skip mark only gates ITS
items) and pin: kinds include the three #337 subjects, the matrix config parses, and every
embedded script compiles / is valid bash.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

import test_cleanroom_container_e2e as mx


def test_kinds_include_the_337_subjects() -> None:
    assert "codex" in mx.KINDS
    assert "opencode" in mx.KINDS
    assert "omp" in mx.KINDS
    assert "claude-code" in mx.KINDS


def test_selected_kinds_default_and_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RIG_CONTAINER_E2E_KINDS", raising=False)
    assert mx._selected_kinds() == mx.KINDS
    monkeypatch.setenv("RIG_CONTAINER_E2E_KINDS", "codex, omp ,opencode")
    assert mx._selected_kinds() == ("codex", "omp", "opencode")
    monkeypatch.setenv("RIG_CONTAINER_E2E_KINDS", "codex,nope")
    with pytest.raises(ValueError, match="unknown kind"):
        mx._selected_kinds()


def test_matrix_config_is_valid_yaml_with_kind_and_src_tokens() -> None:
    data = yaml.safe_load(mx._MATRIX_CONFIG.replace("__SRC__", "/opt/agent-tools").replace("__KIND__", "omp"))
    assert data["harness"]["kind"] == "omp"
    assert data["harness"]["auto_mode"] is True
    assert data["agent_hooks"]["worktree_only"] is True
    assert mx._MATRIX_CONFIG.count("__SRC__") == 1
    assert mx._MATRIX_CONFIG.count("__KIND__") == 1


def test_embedded_python_scripts_compile() -> None:
    compile(textwrap.dedent(mx._PARITY_PY), "parity.py", "exec")
    compile(textwrap.dedent(mx._STUB_PROVIDER_PY), "stub_provider.py", "exec")
    compile(textwrap.dedent(mx._LIVE_PY), "live.py", "exec")


def test_assert_script_is_valid_bash() -> None:
    r = subprocess.run(
        ["bash", "-n"],
        input=textwrap.dedent(mx._ASSERT_SCRIPT),
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"assert.sh has a bash syntax error:\n{r.stderr}"


def test_assert_script_creates_feature_worktree_via_rig() -> None:
    script = textwrap.dedent(mx._ASSERT_SCRIPT)
    assert 'rig worktree create -C "$REPO" feature' in script
    assert script.count('rig worktree create -C "$REPO" feature') == 2
    assert "$REPO/.worktrees/feature" in script
    assert "$HOME/project-feature" not in script
    assert 'git -C "$REPO" worktree add' not in script
    assert "git worktree add" not in script


def test_dockerfile_splices_before_rig_copy() -> None:
    df = mx._dockerfile()
    assert "COPY rig-cli /opt/rig-cli" in df
    assert "ARG RIG_E2E_LIVE" in df
    assert df.index("ARG RIG_E2E_LIVE") < df.index("COPY rig-cli /opt/rig-cli")


def test_ensure_stub_bridges_adds_missing_packages(tmp_path: Path) -> None:
    root = tmp_path / "agent-tools"
    (root / "lib" / "cc_hook_bridge").mkdir(parents=True)
    mx._ensure_stub_bridges(root)
    for name, extra in (
        ("codex_hook_bridge", None),
        ("opencode_hook_bridge", "plugin.js"),
        ("omp_hook_bridge", "extension.ts"),
    ):
        assert (root / "lib" / name / "dispatch.py").is_file()
        assert (root / "lib" / name / "__main__.py").is_file()
        if extra:
            assert (root / "lib" / name / extra).is_file()
