"""Exec a harness inside its own Linux Landlock signal domain (#704).

Why this exists: on 2026-10-15 one agent's bash tool ran a `kill` over a
`pstree` of the wrong pid and took down `systemd --user` and every other
session on the machine. Every agent runs as the same uid, so plain Unix
permissions allow that. `LANDLOCK_SCOPE_SIGNAL` (Landlock ABI >= 6) narrows
it: after `landlock_restrict_self`, this process and its descendants can
signal only processes inside the same (or a nested) domain, so an agent can
kill its own tree and nothing else.

Usage: `python3 -I -S signal_scope.py -- <cmd> [args...]`. The helper never
prevents a launch: when containment is unavailable it says so on stderr and
execs anyway.

Stdlib-only, and it imports nothing from riglib: it runs under `-I -S`
(isolated, no site) on the launch hot path of every agent, possibly from a
different interpreter than rig's own, so it must not depend on rig's import
path or third-party packages.
"""

from __future__ import annotations

import ctypes
import enum
import errno
import os
import pathlib
import sys
from collections.abc import Callable
from typing import NoReturn, TextIO

HELPER = pathlib.Path(__file__).resolve()

_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_SCOPE_SIGNAL = 1 << 1
_PR_SET_NO_NEW_PRIVS = 38
_MIN_ABI = 6


class Outcome(enum.Enum):
    RESTRICTED = "restricted"
    UNSUPPORTED = "unsupported"
    NESTING_LIMIT = "nesting_limit"
    FAILED = "failed"


class _RulesetAttr(ctypes.Structure):
    _fields_ = [
        ("fs", ctypes.c_uint64),
        ("net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    ]


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(None, use_errno=True)


def landlock_abi() -> int:
    """The kernel's Landlock ABI version, or 0 when Landlock is absent/disabled or not Linux."""
    if not sys.platform.startswith("linux"):
        return 0
    try:
        libc = _libc()
    except OSError:
        return 0
    abi = libc.syscall(
        _SYS_LANDLOCK_CREATE_RULESET,
        None,
        ctypes.c_size_t(0),
        ctypes.c_uint32(_LANDLOCK_CREATE_RULESET_VERSION),
    )
    return max(0, abi)


def _classify(step: str, err: int) -> tuple[Outcome, str]:
    if err == errno.E2BIG:
        return Outcome.NESTING_LIMIT, f"{step}: {os.strerror(err)}"
    if err in (errno.ENOSYS, errno.EOPNOTSUPP):
        return Outcome.UNSUPPORTED, f"{step}: {os.strerror(err)}"
    return Outcome.FAILED, f"{step}: {os.strerror(err)}"


def restrict_signals() -> tuple[Outcome, str]:
    """Put the calling process into a fresh signal-scoped Landlock domain."""
    if not sys.platform.startswith("linux"):
        return Outcome.UNSUPPORTED, f"platform {sys.platform}"
    abi = landlock_abi()
    if abi < _MIN_ABI:
        return Outcome.UNSUPPORTED, f"landlock ABI {abi} < {_MIN_ABI}"
    libc = _libc()
    attr = _RulesetAttr(0, 0, _LANDLOCK_SCOPE_SIGNAL)
    fd = libc.syscall(
        _SYS_LANDLOCK_CREATE_RULESET,
        ctypes.byref(attr),
        ctypes.c_size_t(ctypes.sizeof(attr)),
        ctypes.c_uint32(0),
    )
    if fd < 0:
        return _classify("create_ruleset", ctypes.get_errno())
    try:
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            return _classify("no_new_privs", ctypes.get_errno())
        if libc.syscall(_SYS_LANDLOCK_RESTRICT_SELF, fd, ctypes.c_uint32(0)) != 0:
            return _classify("restrict_self", ctypes.get_errno())
    finally:
        os.close(fd)
    return Outcome.RESTRICTED, ""


def scoped_argv(argv: list[str]) -> list[str]:
    """`argv` prefixed so it runs through this helper; absolute, no shell involved."""
    return [sys.executable, "-I", "-S", str(HELPER), "--", *argv]


def main(
    argv: list[str],
    *,
    restrict: Callable[[], tuple[Outcome, str]] = restrict_signals,
    exec_fn: Callable[[str, list[str]], NoReturn] = os.execvp,
    stderr: TextIO | None = None,
) -> int:
    stderr = stderr if stderr is not None else sys.stderr
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        print("usage: signal_scope.py -- <cmd> [args...]", file=stderr)
        return 2
    cmd = argv[0]
    if os.environ.get("RIG_SIGNAL_SCOPE") != "0":
        outcome, detail = restrict()
        if outcome is Outcome.RESTRICTED:
            os.environ["RIG_SIGNAL_SCOPED"] = "1"
        elif outcome is not Outcome.NESTING_LIMIT:
            print(
                f"rig signal-scope: not contained ({detail}); starting {cmd} unrestricted",
                file=stderr,
            )
        stderr.flush()
    try:
        exec_fn(cmd, argv)
    except OSError as exc:
        print(
            f"rig signal-scope: cannot exec {cmd}: {exc.strerror or exc}", file=stderr
        )
        return 127
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
