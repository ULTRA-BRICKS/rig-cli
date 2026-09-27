"""`rig lint code` — run the repository's own Oxlint policy, anti-slop rules included, on demand.

The policy itself is provisioned by `rig apply` from the `linters:` block (``oxlint.config.ts``
plus the vendored ``tools/oxlint/anti-slop`` plugin); `lint-on-write` and the agent-tools
Lefthook gate (``git-hooks/lefthook.yml``) run the same config per file and per commit. This
command runs it over the whole tree or the given paths with the Lefthook gate's invocation:
``--type-aware`` and the same binary order (repo-local ``node_modules/.bin/oxlint``, then the
package manager, never a global install).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from .detect import WhichFn as Which
from .detect import git_toplevel
from .errors import MissingDepError, MissingTargetError

CONFIG_FILES = ("oxlint.config.ts", ".oxlintrc.json")
ANTI_SLOP_SPECIFIER = "tools/oxlint/anti-slop"
OXLINT_TIMEOUT_S = 900
# Oxlint prints this line only after it actually linted; without it the run died on config.
_FINISHED = "Finished in"

Runner = Callable[[list[str], Path], subprocess.CompletedProcess]
TIMED_OUT = 124  # the shell convention text_check.default_runner uses too
CANNOT_RUN = 126


def resolve_oxlint(repo_root: Path, which: Which = shutil.which) -> list[str] | None:
    local = repo_root / "node_modules" / ".bin" / "oxlint"
    if local.is_file() and os.access(local, os.X_OK):
        return [str(local)]
    if (repo_root / "pnpm-lock.yaml").is_file() and which("pnpm"):
        return ["pnpm", "exec", "oxlint"]
    if (repo_root / "yarn.lock").is_file() and which("yarn"):
        return ["yarn", "oxlint"]
    if which("npx"):
        return ["npx", "--no-install", "oxlint"]
    return None


def build_command(repo_root: Path, paths: list[str], which: Which = shutil.which) -> tuple[list[str], Path, bool]:
    """Return (command, config file, anti-slop enabled) or raise a structured error."""
    config = next((repo_root / name for name in CONFIG_FILES if (repo_root / name).is_file()), None)
    if config is None:
        raise MissingTargetError(
            what=f"no Oxlint config in {repo_root}",
            why="rig lint code runs the repository's own Oxlint policy; rig apply generates it from the linters block",
            fix="enable linters.rules.groups.anti-slop: true (and the oxlint item) in rig.yaml, then run rig apply commit",
        )
    cmd = resolve_oxlint(repo_root, which)
    if cmd is None:
        raise MissingDepError(
            what=f"oxlint is not runnable in {repo_root}",
            why="no node_modules/.bin/oxlint and no package manager to run it",
            fix="add oxlint and @oxlint/plugins as devDependencies and install them",
        )
    try:
        anti_slop = ANTI_SLOP_SPECIFIER in config.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise MissingTargetError(what=f"cannot read {config}", why=str(exc), fix="check the file's permissions and encoding") from exc
    return cmd + ["--type-aware"] + (paths or ["."]), config, anti_slop


def _oxlint(cmd: list[str], repo_root: Path) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd, cwd=repo_root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=OXLINT_TIMEOUT_S
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, TIMED_OUT, "", f"oxlint timed out after {OXLINT_TIMEOUT_S}s\n")
    except OSError as exc:  # a broken shim, argument list too long
        return subprocess.CompletedProcess(cmd, CANNOT_RUN, "", f"could not run {cmd[0]}: {exc}\n")


def lint(repo_root: Path, cmd: list[str], runner: Runner = _oxlint) -> tuple[subprocess.CompletedProcess, bool]:
    """Run Oxlint; retry once without nested-config discovery when a nested carrier config broke it.

    Nested ``oxlint.config.ts`` carrier files (agent-tools ``linters/oxc/``) are not project
    configs; Oxlint rejects them before linting anything (the rig-cli#433 case lint_preview
    handles the same way). A timeout is not retried: a second run would hang just as long.
    Returns (result, whether the returned result is the retry).
    """
    proc = runner(cmd, repo_root)
    if proc.returncode not in (0, TIMED_OUT, CANNOT_RUN) and _FINISHED not in (proc.stdout or ""):
        at = cmd.index("--type-aware") + 1 if "--type-aware" in cmd else len(cmd)
        retry = runner([*cmd[:at], "--disable-nested-config", *cmd[at:]], repo_root)
        if _FINISHED in (retry.stdout or ""):
            return retry, True
    return proc, False


def _repo_root(cwd: Path) -> Path:
    return git_toplevel(cwd) or cwd


def repo_paths(invocation: Path, repo_root: Path, paths: list[str]) -> list[str]:
    """PATHs as the user meant them (relative to where rig ran), rewritten for Oxlint's cwd, the
    repo root. With no PATH, a run from a subdirectory lints that subdirectory."""
    targets = [(invocation / p).resolve() for p in paths] or [invocation]
    out = []
    for target in targets:
        try:
            rel = str(target.relative_to(repo_root))
        except ValueError:
            out.append(str(target))  # outside the repo: pass it through as an absolute path
            continue
        # A file named like an option (`--fix`) must reach Oxlint as a path, not as a flag.
        out.append(f".{os.sep}{rel}" if rel.startswith("-") else rel)
    return out


def run(args) -> int:
    invocation = Path(args.cwd).resolve()
    repo_root = _repo_root(invocation)
    cmd, config, anti_slop = build_command(repo_root, repo_paths(invocation, repo_root, list(args.paths)))
    print(f"rig lint code: {' '.join(cmd)}  (config: {config.name}, anti-slop rules: {'on' if anti_slop else 'off'})", flush=True)
    if not anti_slop:
        print("note: anti-slop rules are off here; enable linters.rules.groups.anti-slop in rig.yaml and run rig apply commit", flush=True)
    proc, retried = lint(repo_root, cmd)
    if retried:
        print("note: a nested Oxlint config was rejected; ran again with --disable-nested-config", flush=True)
    print(proc.stdout, end="", flush=True)
    if proc.stderr:
        print(proc.stderr, end="")
    return proc.returncode
