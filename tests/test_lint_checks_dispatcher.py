"""The rendered pre-commit fragment against the REAL global dispatcher (``run-global-hooks``).

The fragment must carry ``# global-hook-protected: true``: for a protected id the dispatcher ignores a
TRACKED ``.githooks-skip`` line and the ``GLOBAL_HOOKS_SKIP`` env var (both forgeable by a cloned repo or
its lefthook ``env:`` block); only the operator's own ``git config hooks.skipGlobal`` opts out.
Everything runs in a scratch repo with a throwaway HOME / ``GIT_CONFIG_GLOBAL`` and a fake ``rig`` that
always fails; the real global git config is never read or written.
"""

from __future__ import annotations

import os
import pwd
import re
import subprocess
from pathlib import Path

import pytest

from riglib import lint_checks_artifacts as arts


def _real_dispatcher() -> Path | None:
    candidates = [os.environ.get("RIG_RUN_GLOBAL_HOOKS", "")]
    candidates.append(str(Path(pwd.getpwuid(os.getuid()).pw_dir) / ".config" / "git" / "run-global-hooks"))
    source = os.environ.get("RIG_AGENT_TOOLS_SOURCE", "")
    if source:
        candidates.append(str(Path(source) / "git-hooks" / "global-dispatcher" / "run-global-hooks"))
    for cand in candidates:
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return Path(cand)
    return None


DISPATCHER = _real_dispatcher()
# The `real_catalog` test-name infix is load-bearing: CI's real-catalog job (RIG_AGENT_TOOLS_SOURCE set)
# selects `-k real_catalog` and FAILS on any skip there, so these behavioral tests cannot silently vanish.
needs_dispatcher = pytest.mark.skipif(DISPATCHER is None, reason="no run-global-hooks dispatcher on this machine")


def test_fragment_header_marks_the_gate_protected_in_the_exact_form_the_dispatcher_greps():
    lines = arts.render_hook_fragment("/opt/rig/bin/rig").splitlines()
    assert lines[0] == "#!/bin/sh"
    assert "# global-hook-id: lint-check" in lines
    # run-global-hooks: sed -n 's/^#[[:space:]]*global-hook-protected:[[:space:]]*//p' | head -n1  =>  1|true|yes|on
    values = [m.group(1) for ln in lines if (m := re.match(r"^#\s*global-hook-protected:\s*(.*)$", ln))]
    assert values[:1] == ["true"]


def _scratch(tmp_path: Path, fragment: str) -> tuple[Path, dict[str, str]]:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[user]\n\tname = t\n\temail = t@example.test\n", encoding="utf-8")
    rig = tmp_path / "bin" / "rig"
    rig.parent.mkdir()
    rig.write_text("#!/bin/sh\necho fake-rig-finding >&2\nexit 1\n", encoding="utf-8")
    rig.chmod(0o755)
    hooks = tmp_path / "hooks.d" / "pre-commit"
    hooks.mkdir(parents=True)
    frag = hooks / arts.HOOK_NAME
    frag.write_text(fragment.replace("/opt/rig/bin/rig", str(rig)), encoding="utf-8")
    frag.chmod(0o755)
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "PATH": os.environ["PATH"], "HOME": str(home), "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1", "GLOBAL_HOOKS_DIR": str(tmp_path / "hooks.d"),
    }
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
    return repo, env


def _dispatch(repo: Path, env: dict[str, str], **extra: str) -> subprocess.CompletedProcess[str]:
    assert DISPATCHER is not None
    return subprocess.run(
        [str(DISPATCHER), "pre-commit"], cwd=repo, env={**env, **extra}, capture_output=True, text=True, check=False
    )


@needs_dispatcher
def test_real_catalog_dispatcher_tracked_skip_file_or_env_var_cannot_disable_the_protected_gate(tmp_path):
    repo, env = _scratch(tmp_path, arts.render_hook_fragment("/opt/rig/bin/rig"))
    base = _dispatch(repo, env)
    assert base.returncode == 1 and "fake-rig-finding" in base.stderr  # the fake rig's finding blocks the commit

    (repo / ".githooks-skip").write_text("# committed by anyone\nlint-check\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", ".githooks-skip"], check=True, env=env)  # tracked, as a clone would carry it
    via_file = _dispatch(repo, env)
    assert via_file.returncode == 1 and "BLOCKED by 30-lint-check" in via_file.stderr

    via_env = _dispatch(repo, env, GLOBAL_HOOKS_SKIP="lint-check")
    assert via_env.returncode == 1 and "BLOCKED by 30-lint-check" in via_env.stderr


@needs_dispatcher
def test_real_catalog_dispatcher_only_the_operators_own_git_config_opts_a_repo_out(tmp_path):
    repo, env = _scratch(tmp_path, arts.render_hook_fragment("/opt/rig/bin/rig"))
    assert _dispatch(repo, env).returncode == 1
    subprocess.run(["git", "-C", str(repo), "config", "--local", "hooks.skipGlobal", arts.HOOK_ID], check=True, env=env)
    skipped = _dispatch(repo, env)
    assert skipped.returncode == 0 and "fake-rig-finding" not in skipped.stderr


@needs_dispatcher
def test_real_catalog_dispatcher_control_an_unprotected_copy_is_skippable_by_the_tracked_file(tmp_path):
    """Proves the two tests above discriminate: without the header the very same line DOES disable the gate."""
    unprotected = arts.render_hook_fragment("/opt/rig/bin/rig").replace("# global-hook-protected: true\n", "")
    assert "global-hook-protected" not in unprotected
    repo, env = _scratch(tmp_path, unprotected)
    skip_file = repo / ".githooks-skip"
    skip_file.write_text("lint-check\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", ".githooks-skip"], check=True, env=env)
    assert _dispatch(repo, env).returncode == 0
    skip_file.unlink()  # the env path needs its own negative control: nothing but the variable may skip here
    assert _dispatch(repo, env).returncode == 1
    assert _dispatch(repo, env, GLOBAL_HOOKS_SKIP="lint-check").returncode == 0
