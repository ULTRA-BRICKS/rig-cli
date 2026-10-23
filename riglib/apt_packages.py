"""apt-get probes shared by ``rig apply`` (the install handler) and ``rig status`` (drift).

The Debian/Ubuntu twin of the Homebrew snapshot in :mod:`riglib.actions.runner`: ONE
``dpkg-query`` call lists every installed package, so a tool declaring ``apt: [tmux, git]`` costs
one subprocess however many packages it names, and apply and status can never disagree.

Stdlib-only; every function is a bounded probe that never raises.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

_PROBE_TIMEOUT_S = 30


def is_apt_system() -> bool:
    """True on a Linux box that can install with ``apt-get`` and read the package database."""
    return (
        sys.platform.startswith("linux")
        and shutil.which("apt-get") is not None
        and shutil.which("dpkg-query") is not None
    )


def installed_snapshot() -> frozenset[str]:
    """Names of every fully installed package (``dpkg-query`` state ``ii``); empty on any failure.

    The failure default is "nothing confirmed installed": a package then either installs cleanly
    or the real ``apt-get install`` surfaces the concrete error.
    """
    dpkg = shutil.which("dpkg-query")
    if dpkg is None:
        return frozenset()
    try:
        proc = subprocess.run(
            [dpkg, "-W", "-f", "${db:Status-Abbrev}\t${Package}\n"],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        return frozenset()
    if proc.returncode != 0:
        return frozenset()
    names: set[str] = set()
    for line in (proc.stdout or "").splitlines():
        status, _, name = line.partition("\t")
        if status.strip() == "ii" and name:
            names.add(name.strip())
    return frozenset(names)


def _on_path(command: str) -> bool:
    return shutil.which(command) is not None


def missing(packages: tuple[str, ...] | list[str], have: frozenset[str]) -> list[str]:
    """The declared ``packages`` that are neither installed per dpkg nor already runnable.

    A package whose same-named command is on ``PATH`` (``tmux``, ``fzf`` from Homebrew/Linuxbrew
    or a manual build) counts as present: rig must not demand ``sudo apt install`` for a tool the
    user already has. Shared by apply and status so the two can never disagree.
    """
    return [p for p in packages if p not in have and not _on_path(p)]


def privilege_prefix() -> list[str] | None:
    """The argv prefix that runs apt as root without a prompt, or None when there is no way.

    ``[]`` as root, ``["sudo", "-n"]`` when a non-interactive sudo works, None otherwise (rig never
    blocks on a password prompt).
    """
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return []
    sudo = shutil.which("sudo")
    if sudo is None:
        return None
    try:
        probe = subprocess.run([sudo, "-n", "true"], capture_output=True, timeout=_PROBE_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return [sudo, "-n"] if probe.returncode == 0 else None


def install_command(packages: list[str]) -> list[str] | None:
    """The full ``apt-get install`` argv for ``packages``, or None without root / sudo -n."""
    prefix = privilege_prefix()
    if prefix is None:
        return None
    return [*prefix, "apt-get", "install", "-y", *packages]


def manual_command(packages: list[str]) -> str:
    """What to type by hand when rig cannot elevate."""
    return f"sudo apt-get install -y {' '.join(packages)}"
