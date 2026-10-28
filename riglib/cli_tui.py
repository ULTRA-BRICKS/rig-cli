"""Predicates deciding whether the interactive wizard (textual + rich) can and may run.

Context: `rig init`/`rig setup` degrade to a preview when the TUI deps are missing or opted out;
the checks are spec lookups so the heavy imports never load just to ask.
"""
from __future__ import annotations

import os as _os


def _missing_tui_deps() -> list[str]:
    """Which of the wizard's two CORE deps (textual, rich) need (re)installing, by name.

    ``_tui_importable()`` fails when EITHER is missing OR ``textual`` is too old — naming the
    wrong one in a user-facing message (e.g. always saying "textual" when only `rich` is
    absent) gives contradictory guidance, since the install command built alongside it
    correctly targets whatever actually needs fixing. A too-old ``textual`` is reported here
    as "textual" too (not a separate case): the fix is the same reinstall either way, and
    every caller's messaging already treats this list as "run the install command below" —
    see ``doctor.textual_too_old()`` for the version check itself (single source of truth,
    shared with `rig doctor`'s own report). Order matches ``DEPENDENCIES`` in doctor.py for a
    stable message.
    """
    import importlib.util

    from .doctor import textual_too_old

    names = []
    for mod in ("textual", "rich"):
        try:
            present = importlib.util.find_spec(mod) is not None
        except (ImportError, ValueError):
            present = False
        if not present or (mod == "textual" and textual_too_old()):
            names.append(mod)
    return names


def _tui_importable() -> bool:
    """True when the wizard's deps are importable AND new enough to actually run the wizard.

    ``textual`` + ``rich`` are CORE runtime dependencies (pyproject ``[project].dependencies``),
    so every canonical install (`pipx install hyper-rig`, `uv tool install hyper-rig`, `pip install
    rig-cli`) brings them and this is normally True. It can be False on a genuinely broken
    environment (a partial / corrupt install) OR on a present-but-too-old ``textual`` (a distro
    package, a stale pre-floor-bump venv, see ``doctor.textual_too_old()``); either way `rig
    init` degrades to a one-line message + a non-destructive preview instead of launching a
    wizard that crashes mid-Apply. Pure spec lookup (no import side effects) so the check
    never drags textual into the process when it is absent. ``find_spec`` itself can raise
    (``ModuleNotFoundError`` on a missing parent package, ``ValueError`` on a half-written
    ``__spec__``), so it is caught: this predicate must never throw.
    """
    import importlib.util

    from .doctor import textual_too_old

    try:
        if any(importlib.util.find_spec(m) is None for m in ("textual", "rich")):
            return False
    except (ImportError, ValueError):
        return False
    return not textual_too_old()


def _tui_opted_out() -> bool:
    """The interactive TUI is suppressed via the ``RIG_NO_TUI`` env (any truthy value).

    The ``--no-tui`` flag is checked separately at the call site (it lives on ``args``); this is
    the env half, so automation can disable the wizard without passing a flag.
    """
    val = _os.environ.get("RIG_NO_TUI", "").strip().lower()
    return val not in ("", "0", "false", "no", "off")
