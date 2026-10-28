"""Rig-owned, version-pinned tool cache for ``rig lint``: layout and lookup.

Context: a repo without a lock-capable package manager (pip-only ``requirements.txt``, loose
``tool.py`` scripts, ``.js`` files with no ``package.json``) cannot take a manifest-and-lockfile
bootstrap, because Rig never creates a manifest. Those projects still get a reproducible default
tool: Ruff in a private venv (``pip install --only-binary :all: ruff==<pin>``) or Oxlint/Oxfmt in a
private npm prefix (``--ignore-scripts``), and the system linters (ShellCheck, shfmt, actionlint)
as sha256-verified release binaries, all under the user cache directory. Nothing is written into the
repository, so these installs are allowed in CI too, and the exact pin is the reproducibility
guarantee. ``lint_providers.resolve_tool`` finds the result as ``ToolResolution(kind="rig-cache")``;
the installing handler lives in :mod:`riglib.lint_tool_steps`. Stdlib-only at import time.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

CACHE_ENV = "RIG_LINT_TOOL_CACHE"
# Rig default tools and the system linters installed from pinned release assets ("bin").
_TOOL_ECOSYSTEM: Mapping[str, str] = {
    "oxlint": "npm", "oxfmt": "npm", "ruff": "pypi", "shellcheck": "bin", "shfmt": "bin", "actionlint": "bin",
}
_SUBDIR: Mapping[str, str] = {"pypi": "py", "npm": "npm", "bin": "bin"}
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def cache_root(environ: Mapping[str, str]) -> Path:
    """``$RIG_LINT_TOOL_CACHE`` > ``$XDG_CACHE_HOME/rig/lint-tools`` > ``~/.cache/rig/lint-tools``."""
    explicit = environ.get(CACHE_ENV, "").strip()
    if explicit:
        return Path(explicit).expanduser()
    xdg = environ.get("XDG_CACHE_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path(environ.get("HOME", "").strip() or Path.home()) / ".cache"
    return base / "rig" / "lint-tools"


def _slug(name: str, version: str) -> str:
    return _UNSAFE_NAME.sub("-", f"{name.lstrip('@')}-{version}")


def environment_dir(environ: Mapping[str, str], ecosystem: str, name: str, version: str) -> Path:
    """The private venv (pypi), npm prefix (npm) or release-binary directory (bin) holding exactly ``name`` at ``version``."""
    return cache_root(environ) / _SUBDIR[ecosystem] / _slug(name, version)


def venv_python(directory: Path, *, windows: bool = False) -> Path:
    return directory / ("Scripts/python.exe" if windows else "bin/python")


def tool_executable(directory: Path, ecosystem: str, tool: str, *, windows: bool = False) -> Path:
    """Where the tool's executable lands inside an environment directory."""
    if ecosystem == "pypi":
        return directory / (f"Scripts/{tool}.exe" if windows else f"bin/{tool}")
    if ecosystem == "bin":
        return directory / (f"{tool}.exe" if windows else tool)
    return directory / "node_modules" / ".bin" / (f"{tool}.cmd" if windows else tool)


def find_cached_tool(
    tool: str, environ: Mapping[str, str], *, version: str | None = None, windows: bool = False
) -> Path | None:
    """The cached executable of a Rig default tool, or ``None``.

    ``version`` selects the exact pin; without it the most recently installed version wins.
    """
    ecosystem = _TOOL_ECOSYSTEM.get(tool)
    if ecosystem is None:
        return None
    base = cache_root(environ) / _SUBDIR[ecosystem]
    if version is not None:
        candidates = [base / _slug(tool, version)]
    elif base.is_dir():
        candidates = sorted(base.glob(f"{_slug(tool, '')}*"), key=_mtime, reverse=True)
    else:
        candidates = []
    for directory in candidates:
        exe = tool_executable(directory, ecosystem, tool, windows=windows)
        if exe.is_file() and os.access(exe, os.X_OK):
            return exe
    return None


def python_has_module(python: str | Path, module: str, *, timeout: int = 20) -> bool:
    """Whether ``python`` can import ``module``; the cheap probe that picks the venv/pip route.

    A distro Python often ships ``venv`` without ``ensurepip``/``pip`` (Debian/Ubuntu split them into
    ``python3-venv``/``python3-pip``), so a plain ``python -m venv`` + ``pip`` plan cannot be assumed.
    Any spawn failure counts as "no": the planner then falls through to the next route or a block.
    """
    try:
        done = subprocess.run(
            [str(python), "-c", f"import {module}"], stdin=subprocess.DEVNULL, capture_output=True,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
