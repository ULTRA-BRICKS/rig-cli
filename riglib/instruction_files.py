"""Serialize rig's read/reconcile/write transactions on shared instruction files.

Locks follow the resolved target, so harness aliases and separate rig invocations use
one kernel lock. Keep lock files in rig state, never inside a client's discovery directory.
Do not unlink a lock file: another process may already be waiting on that same inode.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import time
from collections.abc import Callable
from pathlib import Path


def _lock_path(destination: Path) -> Path:
    xdg = os.environ.get("XDG_STATE_HOME", "")
    # The XDG spec ignores a relative value; a cwd-relative lock root would split one target's lock.
    base = Path(xdg) if os.path.isabs(xdg) else Path.home() / ".local/state"
    identity = os.path.normcase(str(destination))
    return base / "rig/instruction-locks" / (hashlib.sha256(os.fsencode(identity)).hexdigest() + ".lock")


def _nonblocking_locker() -> Callable[[int], None]:
    """Imports stay lazy and portable; an unsupported platform fails before any write."""
    try:
        import fcntl
    except ImportError:
        fcntl = None  # type: ignore[assignment]
    if fcntl is not None:
        return lambda fd: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        import msvcrt
    except ImportError:
        raise OSError("no instruction-file lock primitive on this platform; no update attempted") from None
    return lambda fd: msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)


def _acquire(fd: int, lock_once: Callable[[int], None], deadline: float, destination: Path) -> None:
    while True:
        try:
            lock_once(fd)
            return
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"another rig writer holds {destination}; re-run apply after it finishes"
                ) from exc
            time.sleep(0.025)


@contextlib.contextmanager
def instruction_file_lock(path: Path, *, timeout: float = 10.0):
    """Hold one resolved target through the caller's complete read/reconcile/write.

    Kernel locks release on process exit and on every exception. The existing
    compare-before-replace check still guards uncooperating external editors.
    """
    lock_once = _nonblocking_locker()
    destination = path.resolve()
    lock = _lock_path(destination)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + timeout
    try:
        _acquire(fd, lock_once, deadline, destination)
        if path.resolve() != destination:
            raise OSError(f"{path} changed while waiting for its instruction lock; re-run apply")
        yield
    finally:
        os.close(fd)
