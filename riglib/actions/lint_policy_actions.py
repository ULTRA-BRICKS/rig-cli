"""Apply handlers for lint policy: ``lint_policy_blocked`` / ``format_policy_blocked`` readiness
failures and ``lint_bootstrap_step``, one repo-local pinned tooling install for ``rig lint``.

Trust boundary: the argv, cwd and package pins come from :mod:`riglib.lint_bootstrap`, never from
repository configuration, but this handler still re-checks everything a hand-built or replayed
action could get wrong, because it is the one place that spawns a package manager:

- refuses under CI (``CI`` / ``GITHUB_ACTIONS`` truthy) and unless the action carries
  ``allow_mutation: true`` -- a CI run must never rewrite manifests or lockfiles;
- the working directory is resolved lexically under the repository with no symlink component, and
  any existing manifest/lockfile it may rewrite must be a regular file (never written through a
  link);
- the command runs as an argv (never a shell) with stdin closed and a hard timeout;
- afterwards the manifest must really declare each pinned package and the lockfile must exist,
  otherwise the step is an ``error`` instead of a silent success.

Out-of-manifest steps (pinned tool cache installs, rustup components) carry ``cache_dir`` or
``component`` and are delegated to :mod:`riglib.lint_tool_steps`: they write nothing in the repository,
so unlike manifest-rewriting steps they are allowed under CI. Stdlib-only at import time.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..config import linter_path_escapes_repo
from ..lint_ci_env import ci_env_truthy
from .linter_config import _unsafe_path_component
from .result import ActionResult

STEP_TIMEOUT_SECONDS = 600
_NPM_DEP_BLOCKS = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")
_STDERR_TAIL = 600


# argv[0] of a bootstrap step is one of the package managers Rig's own planner emits; names and
# versions must look like registry pins so a replayed action can never smuggle an option or a URL.
_MANAGER_EXECUTABLES = frozenset({"npm", "pnpm", "yarn", "bun"})
_PACKAGE_NAME = re.compile(r"(@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*", re.IGNORECASE)
_PACKAGE_VERSION = re.compile(r"[0-9][0-9A-Za-z.+!_-]*")


def _packages(raw: object) -> list[tuple[str, str, str]] | None:
    if not isinstance(raw, list):
        return None
    out: list[tuple[str, str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        name, version, ecosystem = item.get("name"), item.get("version"), item.get("ecosystem")
        if not (isinstance(name, str) and isinstance(version, str) and ecosystem in ("npm", "pypi")):
            return None
        if not (_PACKAGE_NAME.fullmatch(name) and _PACKAGE_VERSION.fullmatch(version)):
            return None
        out.append((name, version, ecosystem))
    return out


def _declared_npm(manifest: Path) -> dict[str, str]:
    try:
        doc = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(doc, dict):
        return {}
    out: dict[str, str] = {}
    for block in _NPM_DEP_BLOCKS:
        deps = doc.get(block)
        if isinstance(deps, dict):
            out.update({str(k): str(v) for k, v in deps.items()})
    return out


def _locked_pypi(lockfile: Path, name: str, version: str) -> bool:
    """uv/poetry/pdm locks all record ``name = "<pkg>"`` followed by ``version = "<ver>"``."""
    try:
        text = lockfile.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    pattern = rf'name = "{re.escape(name)}"\s*\n\s*version = "{re.escape(version)}"'
    return re.search(pattern, text) is not None


def _satisfied(root: Path, cwd: Path, lockfile: str, pkgs: list[tuple[str, str, str]]) -> bool:
    """Every pinned package is declared at exactly its pin and the lockfile exists."""
    if not (root / lockfile).is_file():
        return False
    npm = _declared_npm(cwd / "package.json")
    for name, version, ecosystem in pkgs:
        if ecosystem == "npm":
            if npm.get(name) != version:
                return False
        elif not _locked_pypi(root / lockfile, name, version):
            return False
    return True


def _unsafe_writes(root: Path, writes: list[str]) -> str | None:
    for rel in writes:
        if linter_path_escapes_repo(rel):
            return f"{rel!r} escapes the repo"
        unsafe = _unsafe_path_component(root, rel)
        if unsafe is not None:
            return f"{unsafe[0]} is a {unsafe[1].replace('-', ' ')} (refusing to write through it)"
        target = root / rel
        if target.exists() and not target.is_file():
            return f"{target} is not a regular file"
    return None


def do_lint_policy_blocked(action, on_conflict: str):
    """Surface a non-mutating lint-policy readiness failure while allowing other Rig areas to run."""
    reason = str(action.options.get("reason") or "Oxc lint policy prerequisites are not satisfied")
    prompt = str(action.options.get("agent_prompt") or "").strip()
    detail = f"lint-policy: BLOCKED — {reason}"
    if prompt:
        detail += f"\n\nReady-to-copy migration prompt:\n{prompt}"
    return ActionResult(action, "error", detail)


def do_format_policy_blocked(action, on_conflict: str):
    """Surface formatter readiness as one failed target without aborting later actions."""
    reason = str(action.options.get("reason") or "Oxfmt formatter prerequisites are not satisfied")
    prompt = str(action.options.get("agent_prompt") or "").strip()
    detail = f"format-policy: BLOCKED — {reason}"
    if prompt:
        detail += f" Agent/employee migration prompt: {prompt}"
    return ActionResult(action, "error", detail)


@dataclass(frozen=True)
class _Step:
    """A validated ``lint_bootstrap_step`` action: everything the handler needs, already type-checked."""

    argv: list[str]
    cwd: Path
    root: Path
    lockfile: str
    writes: list[str]
    packages: list[tuple[str, str, str]]
    expect_declared: bool


def _malformed(opts: Mapping[str, object]) -> bool:
    argv, cwd_rel, lockfile, writes = opts.get("argv"), opts.get("cwd"), opts.get("lockfile"), opts.get("writes")
    argv_ok = isinstance(argv, list) and bool(argv) and all(isinstance(a, str) and a for a in argv)
    argv_ok = argv_ok and argv[0] in _MANAGER_EXECUTABLES
    writes_ok = isinstance(writes, list) and all(isinstance(w, str) for w in writes)
    return not (
        argv_ok and writes_ok and isinstance(cwd_rel, str) and isinstance(lockfile, str) and lockfile
        and _packages(opts.get("packages")) is not None
    )


def _parse_step(action) -> _Step | str:
    """The validated step, or the refusal reason (a hand-built or replayed action may be wrong anywhere)."""
    opts = action.options
    if _malformed(opts):
        return "malformed action (argv/cwd/lockfile/writes/packages)"
    cwd_rel = str(opts["cwd"])
    root = Path(action.target)
    if cwd_rel != "." and (linter_path_escapes_repo(cwd_rel) or _unsafe_path_component(root, cwd_rel) is not None):
        return f"cwd {cwd_rel!r} escapes the repo or crosses a symlink"
    cwd = root if cwd_rel == "." else root / cwd_rel
    if not cwd.is_dir():
        return f"cwd {cwd} is not a directory"
    writes = list(opts["writes"])
    unsafe = _unsafe_writes(root, writes)
    if unsafe is not None:
        return unsafe
    return _Step(
        argv=list(opts["argv"]), cwd=cwd, root=root, lockfile=str(opts["lockfile"]), writes=writes,
        packages=_packages(opts.get("packages")) or [], expect_declared=opts.get("expect_declared") is True,
    )


def _spawn(step: _Step) -> str | None:
    """Run the package manager; the failure detail, or ``None`` on a zero exit."""
    argv = step.argv
    try:
        proc = subprocess.run(
            argv, cwd=step.cwd, shell=False, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=STEP_TIMEOUT_SECONDS, check=False,
        )
    except FileNotFoundError:
        return f"{argv[0]} is not installed or not on PATH"
    except subprocess.TimeoutExpired:
        return f"{' '.join(argv)} timed out after {STEP_TIMEOUT_SECONDS}s"
    except OSError as exc:
        return f"cannot run {argv[0]}: {exc}"
    if proc.returncode == 0:
        return None
    tail = (proc.stderr or proc.stdout or "").strip()[-_STDERR_TAIL:]
    return f"{' '.join(argv)} exited {proc.returncode}: {tail}"


def _postcondition_failure(step: _Step) -> str | None:
    """Why a zero exit still is not success: the pins are undeclared or the lockfile is missing."""
    if step.expect_declared and not _satisfied(step.root, step.cwd, step.lockfile, step.packages):
        return (
            f"{step.argv[0]} succeeded but the manifest/lockfile does not declare the pinned packages "
            "or the lockfile is missing"
        )
    if not (step.root / step.lockfile).is_file():
        return f"{step.argv[0]} succeeded but lockfile {step.lockfile} does not exist"
    return None


def _refusal(action) -> str | None:
    """Why the step must not run at all (CI, or no explicit mutation grant)."""
    if ci_env_truthy(os.environ):
        return "refused under CI; bootstrap never modifies manifests or lockfiles there"
    if action.options.get("allow_mutation") is not True:
        return "refused: the action does not carry allow_mutation=true"
    return None


def do_lint_bootstrap_step(action, on_conflict: str):
    opts = action.options
    if "cache_dir" in opts or "component" in opts:
        from ..lint_tool_steps import run_tool_step

        return run_tool_step(action)
    label = f"lint-bootstrap ({opts.get('step') or action.item})"

    def error(detail: str):
        return ActionResult(action, "error", f"{label}: {detail}")

    refusal = _refusal(action)
    if refusal is not None:
        return error(refusal)
    if "git_exclude" in opts:  # repo-local ignore of the dependency dirs, written before the install that creates them
        from ..lint_tool_steps import run_git_exclude_step

        status, detail = run_git_exclude_step(action)
        return ActionResult(action, status, f"{label}: {detail}")
    step = _parse_step(action)
    if isinstance(step, str):
        return error(step)
    if step.expect_declared and _satisfied(step.root, step.cwd, step.lockfile, step.packages):
        return ActionResult(action, "skipped", f"{label}: pinned packages already declared and locked")
    failure = _spawn(step) or _postcondition_failure(step)
    if failure is not None:
        return error(failure)
    return ActionResult(action, "updated", f"{label}: ran {' '.join(step.argv)}")
