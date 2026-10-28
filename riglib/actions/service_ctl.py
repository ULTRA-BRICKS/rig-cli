"""Thin launchctl/systemctl wrappers, kept as one seam so tests and drift stub a single place."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _launchctl(verb: str, arg: str) -> int:
    try:
        res = subprocess.run(["launchctl", verb, arg], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return 1
    return res.returncode


def _launchctl_loaded(label: str) -> bool:
    try:
        res = subprocess.run(["launchctl", "list", label], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0


def _launchctl_load_enable(plist: Path) -> int:
    """``launchctl load -w <plist>`` — load the agent AND enable it across reboots (``-w``).

    Separate from :func:`_launchctl` because ``-w`` is a FLAG that must be its own argv token
    (``["launchctl", "load", "-w", <plist>]``), not folded into the verb. Used to actually FIRE
    the tmux boot agent at login — rig previously wrote the plist but never loaded it (DEFECT 1).
    """
    try:
        res = subprocess.run(
            ["launchctl", "load", "-w", str(plist)], capture_output=True, text=True, timeout=20
        )
    except (OSError, subprocess.SubprocessError):
        return 1
    return res.returncode


# ── gui-domain launchctl (modern bootstrap/bootout) ─────────────────────────────────
# The model-freshness schedule uses the legacy ``launchctl load/unload`` (``_launchctl_loaded``
# above); the tg-ctl inbound daemon uses the MODERN per-user ``gui/<uid>`` domain verbs
# (``bootstrap``/``bootout``), which is what macOS recommends and what loads a fresh agent without
# a reboot. Kept separate so the two services don't share a verb set.
def _gui_domain() -> str:
    """The per-user GUI launchd domain target ``gui/<uid>`` for the current user."""
    return f"gui/{os.getuid()}"


def _launchctl_gui(verb: str, plist_path: str) -> int:
    """``launchctl <verb> gui/<uid> <plist>`` — bootstrap (load) / bootout (unload) an agent in
    the per-user GUI domain. Returns the rc; a non-zero rc from ``bootout`` of an unloaded agent
    is harmless (the caller bootstraps after). One shell for both verbs (DRY)."""
    try:
        res = subprocess.run(
            ["launchctl", verb, _gui_domain(), plist_path],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return 1
    return res.returncode


# Thin, self-documenting wrappers so call sites read as bootout/bootstrap (and tests can spy on
# each verb independently).
def _launchctl_bootout(plist_path: str) -> int:
    return _launchctl_gui("bootout", plist_path)


def _launchctl_bootstrap(plist_path: str) -> int:
    return _launchctl_gui("bootstrap", plist_path)


def _launchctl_gui_loaded(label: str) -> bool:
    """True when ``label`` is loaded in the per-user GUI domain (``launchctl print gui/<uid>/
    <label>`` returns 0). Used by the install no-op check + drift so a written-but-not-loaded
    agent is still flagged."""
    try:
        res = subprocess.run(
            ["launchctl", "print", f"{_gui_domain()}/{label}"],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0


def _systemctl_user(*args: str) -> int:
    try:
        res = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return 1
    return res.returncode


def _systemctl_user_output(*args: str) -> str | None:
    """stdout (stripped) of a ``systemctl --user`` query, or ``None`` when it failed."""
    try:
        res = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout.strip() if res.returncode == 0 else None
