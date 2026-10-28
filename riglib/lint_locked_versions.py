"""The exact version a Python project already locks for a tool, read without a TOML parser or a sync.

Context: a project that declares Ruff keeps ITS Ruff. When that tool's executable is not in the
project's own ``.venv`` (a fresh checkout), ``rig lint`` must not sync the application's dependency
environment just to lint (a runtime dependency may be heavy, native or unreachable). It installs
the same version into the Rig tool cache instead, so the version comes from the project's lockfile
(``uv.lock`` / ``poetry.lock`` / ``pdm.lock``) or an exact ``name==x.y.z`` requirements pin. Lock
files all spell a package as consecutive ``name = "..."`` / ``version = "..."`` lines inside a
``[[package]]`` table, so a bounded line scan is enough on every supported Python (``tomllib`` is
3.11+). Reads never follow symlinks. Stdlib-only at import time.
"""

from __future__ import annotations

import re
from pathlib import Path

_LOCKFILES = ("uv.lock", "poetry.lock", "pdm.lock")
_MAX_BYTES = 16 * 1024 * 1024
_SEPARATORS = re.compile(r"[-_.]+")


def _normalize(name: str) -> str:
    """PEP 503 name normalization (``Ruff_Lint`` == ``ruff-lint``)."""
    return _SEPARATORS.sub("-", name).lower()


def _read(path: Path) -> str | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_BYTES:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _from_lock(text: str, wanted: str) -> str | None:
    """Version of the ``[[package]]`` whose name is ``wanted``; the table order is name then version."""
    pending = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("name = "):
            pending = _normalize(stripped[len("name = "):].strip().strip('"')) == wanted
        elif pending and stripped.startswith("version = "):
            return stripped[len("version = "):].strip().strip('"') or None
    return None


def _from_requirements(text: str, wanted: str) -> str | None:
    pattern = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([0-9][A-Za-z0-9._!+-]*)\s*(?:[;#].*)?$")
    for line in text.splitlines():
        match = pattern.match(line)
        if match and _normalize(match.group(1)) == wanted:
            return match.group(2)
    return None


def locked_version(directory: Path, name: str) -> str | None:
    """The exact locked/pinned version of ``name`` in ``directory``'s lockfile or requirements, else ``None``."""
    wanted = _normalize(name)
    for lock in _LOCKFILES:
        text = _read(directory / lock)
        version = _from_lock(text, wanted) if text is not None else None
        if version is not None:
            return version
    for requirements in sorted(directory.glob("requirements*.txt")):
        text = _read(requirements)
        version = _from_requirements(text, wanted) if text is not None else None
        if version is not None:
            return version
    return None
