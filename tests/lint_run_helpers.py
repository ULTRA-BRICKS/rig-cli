"""Shared helpers for the `rig lint run` executor tests.

Builds `PlannedCheck`s, a scripted fake `CommandRunner` (records every spec, never starts a
process), shim executables for tools/package managers, and a tree hash for "nothing was written"
assertions. Nothing here touches the real HOME, the network or a real package manager.
"""
from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from riglib.lint_providers import PlannedCheck, ToolResolution, Unavailable
from riglib.lint_run import CommandOutcome, CommandSpec


def ensure_repo(root: Path) -> Path:
    """Make `root` a git worktree if it is not one (the read-only guard snapshots through git)."""
    if not (root / ".git").exists():
        subprocess.run(["git", "-C", str(root), "init", "-q"], check=True, capture_output=True)
    return root


def planned(*, id: str = "oxlint@.", provider: str = "oxlint", category: str = "lint", scope: str = ".",
            argv: Sequence[str] | None = ("tool", "--check"), unavailable: Unavailable | None = None,
            fix_argv: Sequence[str] | None = None, result_rule: str = "exit-zero", timeout_s: int | None = None,
            cwd: str = ".", resolution: ToolResolution | None = None,
            covers: Iterable[str] = ()) -> PlannedCheck:
    return PlannedCheck(
        id=id, provider=provider, category=category, scope=scope, tool=provider, precedence=1,
        covers=frozenset(covers), argv=tuple(argv) if argv is not None else None, cwd=cwd,
        unavailable=unavailable, timeout_s=timeout_s,
        fix_argv=tuple(fix_argv) if fix_argv is not None else None,
        result_rule=result_rule, resolution=resolution,
    )


def outcome(exit_code: int | None = 0, stdout: str = "", stderr: str = "", *, ms: int = 7,
            timed_out: bool = False, truncated: bool = False, spawn_error: str | None = None) -> CommandOutcome:
    return CommandOutcome(exit_code, stdout, stderr, ms, timed_out, truncated, spawn_error)


class FakeRunner:
    """Scripted `CommandRunner`: `script` maps an argv tuple (or a callable on the spec) to an outcome."""

    def __init__(self, script: dict[tuple[str, ...], CommandOutcome | Callable[[CommandSpec], CommandOutcome]]
                 | None = None, default: CommandOutcome | None = None) -> None:
        self.script = script or {}
        self.default = default or outcome(0)
        self.specs: list[CommandSpec] = []

    def __call__(self, spec: CommandSpec) -> CommandOutcome:
        self.specs.append(spec)
        entry = self.script.get(spec.argv, self.default)
        return entry(spec) if callable(entry) else entry

    @property
    def argvs(self) -> list[tuple[str, ...]]:
        return [s.argv for s in self.specs]


def tree_digest(root: Path) -> str:
    """Digest of every file path + content under `root` (excluding .git) to prove nothing was written."""
    sha = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if ".git" not in p.relative_to(root).parts):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            sha.update(f"L {rel} {os.readlink(path)}\0".encode())
        elif path.is_file():
            sha.update(f"F {rel}\0".encode() + path.read_bytes())
        else:
            sha.update(f"D {rel}\0".encode())
    return sha.hexdigest()


# A shared-library CPython (the hosted toolcache 3.10/3.11 builds) only starts when the loader can find libpython.
# The runner hands a check exactly the environment it was given, so a test that launches `sys.executable` or a
# python shim under a hand-built environ must carry this through (the product itself never forwards it).
LOADER_ENV: dict[str, str] = {k: os.environ[k] for k in ("LD_LIBRARY_PATH",) if k in os.environ}


def write_exe(path: Path, body: str) -> Path:
    """An executable python script (shebang = the test interpreter) standing in for a real tool."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def which_in(*dirs: Path) -> Callable[[str], str | None]:
    """A `which` limited to the given directories: the real PATH is never consulted."""
    def which(name: str) -> str | None:
        for directory in dirs:
            candidate = directory / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        return None
    return which
