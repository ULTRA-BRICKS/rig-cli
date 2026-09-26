"""Make rig's stdout/stderr able to print its own output on any terminal.

rig's help, plans and status tables use non-ASCII glyphs (``→``, ``✔``, ``—``). Python encodes
stdout with the OS locale codepage whenever the stream is not a real console — on Windows that is
``cp1251``/``cp1252`` for a pipe, a redirect, or a mintty/Cygwin/Git Bash terminal (mintty talks to
native programs through a pipe). Those codepages cannot encode ``→``, so a bare ``rig --help`` died
with ``UnicodeEncodeError`` before printing a line. Re-encoding the streams as UTF-8 (with
``errors="replace"`` so an exotic terminal degrades to ``?`` instead of crashing) fixes that without
asking the user to export ``PYTHONUTF8=1``.

Stdlib-only at import time (the AGENTS.md hard rule).
"""

from __future__ import annotations

import sys


def _is_utf8(encoding: str | None) -> bool:
    return (encoding or "").lower().replace("-", "").replace("_", "") in {"utf8", "utf8sig"}


def ensure_utf8_stdio() -> None:
    """Reconfigure ``sys.stdout``/``sys.stderr`` to UTF-8 when they use another encoding.

    Idempotent and safe to call from every entry point: a stream already in UTF-8, or one that
    cannot be reconfigured (a test double, a closed stream), is left alone.
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None or _is_utf8(getattr(stream, "encoding", None)):
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
