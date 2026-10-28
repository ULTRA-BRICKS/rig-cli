"""Plan ``install-tool-cache`` bootstrap steps: pinned default tools outside any manifest.

Context: pip-only projects, loose scripts and JS files without a ``package.json`` have no
lock-capable manager to record a dependency in, and Rig never creates a manifest. Their default
tool (Ruff, Oxlint, Oxfmt) is instead installed at its exact pin into a private venv / npm prefix
under the user cache (:mod:`riglib.lint_tool_cache`). The steps are ordinary ``lint_bootstrap_step``
actions, so they run through ``actions.run_plan`` like every other bootstrap step, write nothing in
the repository, and are therefore also allowed in CI. Planning is pure: it only stats the cache.
Stdlib-only at import time.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from .lint_bootstrap_model import BootstrapBlocked, BootstrapStep
from .lint_discovery_model import Discovery, ProjectScope
from .lint_providers import BootstrapNeed, PinnedPackage, ToolEnv, resolve_tool
from .lint_run_config import LintConfig
from .lint_tool_cache import environment_dir, python_has_module, tool_executable, venv_python
from .plan import Action

# "manager-run" is deliberately not usable: poetry/pdm `run` may sync the project environment, and linting must
# never install (or change) the application's runtime dependencies.
USABLE_KINDS = ("local-bin", "path")


@dataclass(frozen=True)
class CacheSteps:
    steps: tuple[BootstrapStep, ...]
    actions: tuple[Action, ...]
    blocked: tuple[BootstrapBlocked, ...]


def step_action(step: BootstrapStep, root: Path) -> Action:
    """The ``lint_bootstrap_step`` action for a tool-cache step (argv install or pinned release download)."""
    options: dict[str, object] = {
        "step": step.id, "cache_dir": step.cache_dir, "creates": step.creates, "allow_mutation": True,
    }
    if step.release:
        options["release"] = list(step.release)
    elif step.copy:
        options["copy_tree"] = list(step.copy)
    elif step.content:
        options["write_content"] = step.content
    else:
        options["argv"] = list(step.argv)
    return Action(kind="lint_bootstrap_step", category="lint_bootstrap", item=step.id, source=root, target=root, options=options)


def _step(step_id: str, need: BootstrapNeed, manager: str, argv: tuple[str, ...], pkg: PinnedPackage,
          directory: Path, creates: Path) -> BootstrapStep:
    return BootstrapStep(
        step_id, "install-tool-cache", need.scope, manager, argv, ".", (pkg,), "n/a", (),
        cache_dir=str(directory), creates=str(creates),
    )


def _venv_steps(
    need: BootstrapNeed, pkg: PinnedPackage, env: ToolEnv, directory: Path, python: Path, creator: str
) -> list[BootstrapStep] | None:
    """Steps that create (or repair) the cache venv and install ``pkg``, or ``None`` when no route exists.

    Route by probe: an interpreter with ``ensurepip`` uses ``venv`` + ``pip``; otherwise ``uv`` (when on
    PATH) creates the venv and installs without a project or a sync. Neither route mutates the system
    Python. A half-created venv (directory without ``bin/python``, or Python without pip) is repaired in
    place, so a failed earlier run is retried, never trusted.
    """
    slug = f"{pkg.name}-{pkg.version}"
    exe = tool_executable(directory, "pypi", pkg.name, windows=env.system == "windows")
    has_venv = python.is_file()
    pip_ok = python_has_module(python, "pip") if has_venv else python_has_module(creator, "ensurepip")
    uv = env.which("uv")
    steps: list[BootstrapStep] = []
    if pip_ok:
        if not has_venv:
            steps.append(_step(f"py-cache-venv@{slug}", need, "pip", (creator, "-m", "venv", str(directory)), pkg, directory, python))
        # Wheels only: an sdist would execute the package's build backend on this machine.
        install = (str(python), "-m", "pip", "install", "--only-binary", ":all:", "--disable-pip-version-check",
                   "--no-input", f"{pkg.name}=={pkg.version}")
        steps.append(_step(f"py-cache-install@{slug}", need, "pip", install, pkg, directory, exe))
        return steps
    if uv is None:
        return None
    if not has_venv:
        venv = (uv, "venv", "--allow-existing", "--python", creator, str(directory))
        steps.append(_step(f"py-cache-venv@{slug}", need, "uv", venv, pkg, directory, python))
    install = (uv, "pip", "install", "--python", str(python), "--only-binary", ":all:", f"{pkg.name}=={pkg.version}")
    steps.append(_step(f"py-cache-install@{slug}", need, "uv", install, pkg, directory, exe))
    return steps


def _no_python_route(need: BootstrapNeed, creator: str) -> BootstrapBlocked:
    return BootstrapBlocked(
        need.scope, need.tool, "manager-not-installed",
        f"{creator} cannot create a pip-enabled virtual environment (ensurepip is missing) and `uv` is not on PATH; "
        f"install `uv` (https://docs.astral.sh/uv/) or your distribution's python3-venv/python3-pip package, then re-run "
        f"`rig lint` (Rig never runs sudo/apt or changes the system Python)",
    )


def _python_steps(need: BootstrapNeed, pkg: PinnedPackage, env: ToolEnv) -> list[BootstrapStep] | BootstrapBlocked:
    windows = env.system == "windows"
    directory = environment_dir(env.environ, "pypi", pkg.name, pkg.version)
    if tool_executable(directory, "pypi", pkg.name, windows=windows).is_file():
        return []
    creator = env.which("python3") or sys.executable
    steps = _venv_steps(need, pkg, env, directory, venv_python(directory, windows=windows), creator)
    return _no_python_route(need, creator) if steps is None else steps


def _npm_steps(need: BootstrapNeed, pkg: PinnedPackage, env: ToolEnv) -> list[BootstrapStep]:
    directory = environment_dir(env.environ, "npm", pkg.name, pkg.version)
    exe = tool_executable(directory, "npm", pkg.name, windows=env.system == "windows")
    if exe.exists():
        return []
    argv = ("npm", "install", "--prefix", str(directory), "--ignore-scripts", "--no-audit", "--no-fund",
            f"{pkg.name}@{pkg.version}")
    return [_step(f"js-cache-install@{pkg.name}-{pkg.version}", need, "npm", argv, pkg, directory, exe)]


def plan_cache_install(
    need: BootstrapNeed, scope: ProjectScope, discovery: Discovery, cfg: LintConfig, env: ToolEnv, pkg: PinnedPackage,
    seen: set[str],
) -> CacheSteps:
    """Steps that put ``pkg`` into the Rig tool cache for a scope with no lock-capable manager.

    ``seen`` dedupes across scopes (several scopes share one cached tool). A tool the repository
    already resolves (project venv, local ``node_modules``) needs nothing.
    """
    usable = resolve_tool(need.tool, scope, discovery, env)
    if usable.kind in USABLE_KINDS:
        return CacheSteps((), (), ())
    steps = _python_steps(need, pkg, env) if need.ecosystem == "python" else _npm_steps(need, pkg, env)
    if isinstance(steps, BootstrapBlocked):
        return CacheSteps((), (), (steps,))
    if steps and need.ecosystem == "js" and env.which("npm") is None:
        # Only an install needs npm: a tool that is already cached runs without it.
        blocked = BootstrapBlocked(
            need.scope, need.tool, "manager-not-installed",
            f"no package.json owns {need.scope}, so {need.tool} would be cached with npm, which is not on PATH; "
            "install Node.js/npm and re-run `rig lint`",
        )
        return CacheSteps((), (), (blocked,))
    fresh = [s for s in steps if s.id not in seen]
    seen.update(s.id for s in fresh)
    return CacheSteps(tuple(fresh), tuple(step_action(s, discovery.root) for s in fresh), ())
