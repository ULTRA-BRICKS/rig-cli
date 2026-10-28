"""Terminal output helpers shared by every `rig` command: color, error blocks, scalar rendering.

Context: split out of `cli.py` so each command module imports one small leaf instead of the
entry point. Honors `NO_COLOR` and a non-TTY stdout (no color dependency).
"""
from __future__ import annotations

import os as _os
import shlex
import sys

from .layers import GLOBAL as _GLOBAL
from .layers import REPO as _REPO

_USE_COLOR = sys.stdout.isatty() and not _os.environ.get("NO_COLOR")


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _USE_COLOR else s


def _ok(s: str) -> str:
    return _c("32", s)


def _warn(s: str) -> str:
    return _c("33", s)


def _err(s: str) -> str:
    return _c("31", s)


def _dim(s: str) -> str:
    return _c("2", s)


def _dim_cmd(cmd: list[str]) -> str:
    """Dim-render a shell command for copy-paste display.

    ``shlex.join`` (never a bare ``' '.join``): a too-old-textual install spec can carry
    ``textual>=0.66`` — an unquoted ``>`` in copy-pasted shell text is a redirection
    operator, not a version comparator, and would silently strip the very constraint the
    command exists to enforce (plus create a stray file named ``=0.66``). One helper for
    every command-display site so this policy can't drift out of sync at any one of them.
    """
    return _dim(shlex.join(cmd))


def _bold(s: str) -> str:
    return _c("1", s)


def _err_block(exc: Exception) -> str:
    """Render an exception as the standard error block — 3-part for a config schema error.

    A :class:`riglib.config.ConfigError` carries WHAT / WHY / FIX + the SCHEMA PATH (roadmap §5):
    render the full block so a malformed config fails LOUDLY and points the user at the offending
    node in ``schema/rig.schema.json``. Any other exception (catalog/plan/OS) keeps the existing
    one-line ``error: <msg>`` shape. Used by every ``except`` site so config rejections read the
    same whether they come from ``apply``, ``config set``, ``status``, or ``init``.
    """
    from .config import ConfigError as _ConfigError
    from .config import render_config_error

    if isinstance(exc, _ConfigError):
        return render_config_error(exc, color=_USE_COLOR)
    return _err(f"error: {exc}")


def _fmt_scalar(value: object) -> str:
    """Render a coerced scalar in YAML/CLI casing (true/false/null), not Python's repr.

    Shared by `config get` (the printed value) and `config set` (the confirmation line) so a
    bool a user typed as ``false`` never echoes back as Python's ``False``.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return str(value)


def _mask_secret(value: str) -> str:
    """Render an env.vars value for stdout: last 4 chars, or four stars if shorter.

    env.vars is plaintext in config.yaml and rig.env.sh; list/set confirmation must
    not reprint the secret. ``--show`` is the explicit escape.
    """
    if len(value) <= 4:
        return "****"
    return "***" + value[-4:]


# The GLOBAL/REPO layer headings, shared by the area summary and the per-item drift dump so the
# two renderers can never disagree on the wording.
_LAYER_HEADERS = {
    _GLOBAL: "GLOBAL — machine-wide (from ~/.config/rig/config.yaml)",
    _REPO: "REPO — this repository (from ./rig.yaml)",
}
