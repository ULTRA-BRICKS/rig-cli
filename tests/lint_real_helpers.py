"""Helpers for the REAL-binary `rig lint run` integration tests (tests/test_lint_real_tools.py).

`rig lint run` is started as a subprocess (so no conftest monkeypatch can stand in for the product), against a
tmp git repository, with a scrubbed environment: no `CI`/`GITHUB_ACTIONS` (a bare *local* run bootstraps), a
throwaway HOME, and the Rig tool cache pointed at a directory shared by the whole session.

A real tool that cannot be obtained is a FAILURE when `RIG_LINT_REQUIRE_REAL=1` (CI) and a skip with an explicit
reason otherwise; nothing here ever skips silently.
"""
from __future__ import annotations

import functools
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from riglib.lint_providers import PINS
from riglib.lint_tool_cache import find_cached_tool
from tests.lint_check_helpers import commit_all, make_repo, put

ROOT = Path(__file__).resolve().parent.parent
RUN_TIMEOUT_S = 900
_SCRUBBED = ("CI", "GITHUB_ACTIONS", "RIG_LINT_DEPTH", "RIG_LINT_CI")


def require_real() -> bool:
    return os.environ.get("RIG_LINT_REQUIRE_REAL") == "1"


def unobtainable(reason: str) -> None:
    """A real binary is missing: fail under RIG_LINT_REQUIRE_REAL=1, otherwise skip saying why."""
    if require_real():
        pytest.fail(f"{reason} (RIG_LINT_REQUIRE_REAL=1)")
    pytest.skip(reason)


def _account_home() -> Path:
    """The real user home: the test HOME is isolated, so default catalog candidates are looked up via the passwd db."""
    import pwd

    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def agent_tools_source() -> Path | None:
    """The agent-tools checkout a bare `rig lint run` needs for the anti-slop policy, or `None`.

    `RIG_TEST_AGENT_TOOLS_SOURCE` (CI: only the real-tool lint tests see it) > `RIG_AGENT_TOOLS_SOURCE`, then the
    same default candidates `rig apply` tries (under the real home). The value is handed to the `rig lint run`
    subprocess as `RIG_AGENT_TOOLS_SOURCE`.
    """
    from riglib.catalog import DEFAULT_SOURCE_CANDIDATES

    explicit = os.environ.get("RIG_TEST_AGENT_TOOLS_SOURCE") or os.environ.get("RIG_AGENT_TOOLS_SOURCE")
    candidates = [Path(explicit).expanduser()] if explicit else [
        _account_home() / cand.removeprefix("~/") for cand in DEFAULT_SOURCE_CANDIDATES
    ]
    for path in candidates:
        if (path / "skills").is_dir() and (path / "agent-hooks").is_dir():
            return path.resolve()
    return None


def require_agent_tools_source() -> Path:
    """The catalog, or `unobtainable`: fail under RIG_LINT_REQUIRE_REAL=1, skip with the reason locally."""
    found = agent_tools_source()
    if found is None:
        unobtainable("no agent-tools checkout for the anti-slop policy (set RIG_AGENT_TOOLS_SOURCE or clone agent-tools)")
    return found


def lint_env(home: Path, cache: Path, *, catalog: Path | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED}
    home.mkdir(parents=True, exist_ok=True)
    env.update(
        HOME=str(home),
        RIG_LINT_TOOL_CACHE=str(cache),
        npm_config_cache=str(cache / "npm-cache"),
        PYTHONPATH=os.pathsep.join(filter(None, [str(ROOT), env.get("PYTHONPATH", "")])),
    )
    if catalog is not None:
        env["RIG_AGENT_TOOLS_SOURCE"] = str(catalog)
    return env


def run_lint(repo: Path, env: Mapping[str, str], *args: str) -> dict:
    """`rig lint run --json -C repo ...` for real; returns the parsed document."""
    argv = [sys.executable, "-m", "riglib", "lint", "run", "-C", str(repo), "--json", *args]
    done = subprocess.run(argv, capture_output=True, text=True, env=dict(env), timeout=RUN_TIMEOUT_S, check=False)
    try:
        doc = json.loads(done.stdout)
    except json.JSONDecodeError:
        pytest.fail(f"rig lint run printed no JSON document (exit {done.returncode}):\n{done.stderr[-2000:]}")
    assert doc["exit_code"] == done.returncode, (doc["exit_code"], done.returncode)
    return doc


def states(doc: dict) -> dict[str, str]:
    """check id -> state, for the repo's root scope (`tool@.`)."""
    return {c["id"]: c["state"] for c in doc["checks"]}


def bootstrap_failures(doc: dict) -> list[str]:
    boot = doc["bootstrap"]
    bad = [r["detail"] for r in boot["results"] if r["state"] not in ("applied", "nothing-to-do")]
    return bad + [f"{b['tool']}: {b['reason']}: {b['detail']}" for b in boot["blocked"]]


def settle(doc: dict, *expected: str) -> dict:
    """Setup must have really installed the tools; an environment gap is `unobtainable`, never a pass.

    `expected` are the check ids that must exist and have actually run (neither `missing-tool` nor
    `unsupported-platform`), so a green exit can never come from a check that quietly did not run.
    """
    failures = bootstrap_failures(doc)
    if failures:
        unobtainable("pinned tool bootstrap did not complete: " + "; ".join(failures))
    got = states(doc)
    for check_id in expected:
        state = got.get(check_id)
        if state in ("missing-tool", "unsupported-platform"):
            unobtainable(f"{check_id} could not start ({state})")
        assert state in ("passed", "failed"), (check_id, state, sorted(got))
    return doc


def tree(repo: Path) -> dict[str, bytes]:
    """Every repo file except `.git`, dependency trees and tool caches, with its bytes."""
    skip = {".git", "node_modules", ".ruff_cache", "__pycache__"}
    return {
        str(p.relative_to(repo)): p.read_bytes()
        for p in sorted(repo.rglob("*"))
        if p.is_file() and not skip.intersection(p.relative_to(repo).parts)
    }


def build_repo(tmp_path: Path, name: str, files: Mapping[str, str], *, executable: Sequence[str] = ()) -> Path:
    repo = make_repo(tmp_path, name)
    put(repo, ".gitignore", "node_modules/\n.venv/\n__pycache__/\n")
    for rel, content in files.items():
        put(repo, rel, content)
    for rel in executable:
        (repo / rel).chmod(0o755)
    commit_all(repo)
    return repo


# A manifest-less file per tool: the one shape whose bootstrap lands in the Rig tool cache (not in the repo).
_CACHE_SEEDS: Mapping[str, tuple[str, str]] = {"oxlint": ("seed.js", "export {};\n"), "ruff": ("seed.py", "x = 1\n")}


@functools.cache
def session_cache() -> Path:
    """The tool cache shared by every real-tool test of the session (CI points RIG_TEST_LINT_TOOL_CACHE at it).

    Deliberately NOT `RIG_LINT_TOOL_CACHE`: that variable is the product's own cache switch, and a suite-wide value
    would leak the warm real binaries into the hermetic tests that assert a tool is missing.
    """
    explicit = os.environ.get("RIG_TEST_LINT_TOOL_CACHE")
    return Path(explicit) if explicit else Path(tempfile.mkdtemp(prefix="rig-lint-tools-"))


@functools.cache
def pinned_tool(tool: str, override_env: str | None = None) -> Path:
    """The pinned binary of a Rig default tool: `override_env`, else the tool cache, else Rig's own bootstrap.

    The bootstrap is a real bare `rig lint run` over a throwaway seed repo, so the binary is exactly what a
    user's run installs. Unobtainable (offline, no npm/venv) is `unobtainable`: a failure under
    RIG_LINT_REQUIRE_REAL=1, a skip with the reason otherwise.
    """
    explicit = os.environ.get(override_env, "") if override_env else ""
    if explicit:
        if os.access(explicit, os.X_OK):
            return Path(explicit)
        unobtainable(f"{override_env}={explicit} is not an executable")
    environ = {**os.environ, "RIG_LINT_TOOL_CACHE": str(session_cache())}
    found = find_cached_tool(tool, environ, version=PINS[tool].version)
    if found is None:
        seed, text = _CACHE_SEEDS[tool]
        with tempfile.TemporaryDirectory(prefix="rig-lint-seed-") as scratch:
            repo = build_repo(Path(scratch), "seed", {seed: text})
            seed_env = lint_env(Path(scratch) / "home", session_cache(), catalog=agent_tools_source())
            run_lint(repo, seed_env, "--only", "lint")
        found = find_cached_tool(tool, environ, version=PINS[tool].version)
    if found is None:
        unobtainable(f"pinned {tool} {PINS[tool].version} is unobtainable (not in the Rig tool cache, bootstrap failed)")
    return found
