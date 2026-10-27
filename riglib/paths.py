"""Shared path expansion helpers for rig-owned user config paths."""

from __future__ import annotations

import os
from pathlib import Path


def expand_user_path(path: str) -> Path:
    """Expand ``$VAR``, ``~``, and the portable ``~/.config`` prefix.

    When ``XDG_CONFIG_HOME`` is set, ``~/.config`` and paths below it map there before
    standard environment/user expansion. Relative paths remain relative; callers that need a
    repo or cwd anchor must apply it after this expansion step.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg and (path == "~/.config" or path.startswith("~/.config/")):
        path = xdg + path[len("~/.config"):]
    return Path(os.path.expanduser(os.path.expandvars(path)))


def expand_path(path: str, repo_root: Path) -> Path:
    """:func:`expand_user_path`, then anchor a still-relative result at ``repo_root`` (resolved)."""
    p = expand_user_path(path)
    if not p.is_absolute():
        p = (repo_root / p).resolve()
    return p
