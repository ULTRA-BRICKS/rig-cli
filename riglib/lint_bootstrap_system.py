"""Plan the installs of system linters: pinned release binaries and Rust toolchain components.

Context: ShellCheck, shfmt and actionlint have no lockable package-manager route, so a missing one
is installed from its sha256-pinned official release into the Rig tool cache
(:mod:`riglib.lint_release_tools`); an unsupported platform has no legitimate route and is reported
``unsupported-platform`` (the check stays ``missing-tool``, never a pass). Clippy and rustfmt are
rustup components: ``rustup component add clippy rustfmt`` for the toolchain that applies in the
project directory, never a toolchain install or a Rust version change. Go needs nothing: ``go vet``
and ``gofmt`` ship with the toolchain, which Rig never installs or switches. Planning is pure; the
handler (:mod:`riglib.lint_tool_steps`) does the work. Stdlib-only at import time.
"""

from __future__ import annotations

from pathlib import Path

from .lint_bootstrap_cache import CacheSteps, step_action
from .lint_bootstrap_model import BootstrapBlocked, BootstrapStep
from .lint_providers import BootstrapNeed, ToolEnv
from .lint_release_tools import RELEASE_VERSIONS, release_asset
from .lint_tool_cache import environment_dir, tool_executable
from .lint_tool_steps import RUSTUP_ARGV
from .plan import Action

_NONE = CacheSteps((), (), ())


def _fresh(step: BootstrapStep, action: Action, seen: set[str]) -> CacheSteps:
    if step.id in seen:
        return _NONE
    seen.add(step.id)
    return CacheSteps((step,), (action,), ())


def _release_install(need: BootstrapNeed, env: ToolEnv, root: Path, seen: set[str]) -> CacheSteps:
    tool = need.tool
    asset = release_asset(tool, env.system, env.machine)
    if asset is None or tool not in RELEASE_VERSIONS:
        blocked = BootstrapBlocked(
            need.scope, tool, "unsupported-platform",
            f"no pinned {tool} release for {env.system}/{env.machine}; install {tool} yourself and put it on PATH, then re-run `rig lint`",
        )
        return CacheSteps((), (), (blocked,))
    version = RELEASE_VERSIONS[tool]
    directory = environment_dir(env.environ, "bin", tool, version)
    exe = tool_executable(directory, "bin", tool, windows=env.system == "windows")
    if exe.is_file():
        return _NONE
    step = BootstrapStep(
        f"bin-cache-install@{tool}-{version}", "install-tool-cache", need.scope, "release", (), ".", (), "n/a", (),
        cache_dir=str(directory), creates=str(exe), release=(asset.url, asset.sha256, asset.kind),
    )
    return _fresh(step, step_action(step, root), seen)


def _rustup_components(need: BootstrapNeed, env: ToolEnv, root: Path, seen: set[str]) -> CacheSteps:
    if env.which("rustup") is None:
        blocked = BootstrapBlocked(
            need.scope, need.tool, "manager-not-installed",
            "clippy/rustfmt are rustup components but rustup is not on PATH; install Rust via rustup "
            "(Rig never installs a toolchain) or add the components yourself, then re-run `rig lint`",
        )
        return CacheSteps((), (), (blocked,))
    step = BootstrapStep(
        f"rustup-components@{need.scope}", "install-toolchain-component", need.scope, "rustup", RUSTUP_ARGV,
        need.scope, (), "n/a", (),
    )
    action = Action(
        kind="lint_bootstrap_step", category="lint_bootstrap", item=step.id, source=root, target=root,
        options={
            "step": step.id, "argv": list(RUSTUP_ARGV), "cwd": need.scope, "component": ["clippy", "rustfmt"],
            "allow_mutation": True,
        },
    )
    return _fresh(step, action, seen)


def plan_system_install(need: BootstrapNeed, env: ToolEnv, root: Path, seen: set[str]) -> CacheSteps:
    """Steps (or a block) for a ``system`` / ``rustup`` need; ``seen`` dedupes across scopes."""
    if need.ecosystem == "rustup":
        return _rustup_components(need, env, root, seen)
    return _release_install(need, env, root, seen)
