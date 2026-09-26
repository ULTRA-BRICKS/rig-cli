"""Global git settings — the ``git:`` area (``git config --global <key> <value>``).

Accessed via: ``rig apply`` → :func:`riglib.plan._build_git` emits ONE ``provision_git_config``
action carrying every declared key → :func:`riglib.actions.runner._do_provision_git_config` sets
each key that is missing or differs. ``rig status`` (drift) and the post-apply verify read the SAME
:func:`resolve_key` classification, so the three can never disagree.

Config shape (GLOBAL layer only — ``~/.config/rig/config.yaml``)::

    git:
      enabled: true              # optional: block PRESENCE opts in; only `false` opts out
      config:
        user.name: Alex Ultra
        user.email: someone@example.com
        pull.rebase: true        # bools are written as true/false
        init.defaultBranch: main

Why GLOBAL-only, enforced at load time: several git keys execute code (``core.sshCommand``,
``core.fsmonitor``, ``credential.helper``, ``!``-aliases). A committed repo ``rig.yaml`` is a
possibly-adversarial source, so the loader rejects a ``git`` block from any non-global layer
(:func:`riglib.config.load`) — cloning a repo and running ``rig apply`` must never rewrite the
machine's global git config.

Never unsets: a key the user set by hand that the config does not declare is left alone (drift is
config→disk only for this area; disk→config extras in ``~/.gitconfig`` are not rig's business).
``core.hooksPath`` / ``core.excludesfile`` are refused by the validator — the ``git_hooks``
dispatcher and the ``gitignore`` block own them, and two owners would fight on every apply.

Key shape (mirrors git's own rules): ``section.name`` or ``section.subsection.name``. Section and
name start with a letter and hold only letters, digits and ``-`` (git compares them
case-insensitively); the subsection is everything between the FIRST and LAST dot, may itself hold
dots and slashes (``url.https://example.com/.insteadOf``), and is case-sensitive. Values are
strings without line breaks, bools, or ints. Multi-valued keys (``remote.*.fetch``) are out of
scope: ``git config --global k v`` refuses to overwrite several values, and apply reports that.

Comparison is by git's own semantics, not bytes: a desired ``true`` matches a hand-written
``yes``/``on``/``1``; a desired int matches ``1k`` == 1024. Strings compare exactly.

Stdlib-only; the git subprocess seams live in :mod:`riglib.actions.runner` (``_git_global_get`` /
``_set_git_global``) and are imported LAZILY inside :func:`resolve_key`, so one monkeypatch of the
runner module covers plan/apply/drift/verify alike.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# section / name: a letter, then letters, digits, '-' (git's `iskeychar` rule for these parts).
_KEY_PART_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*$")
# The published JSON-schema mirror of :func:`parse_key` (``propertyNames`` of ``git.config``).
# A parity test runs a corpus through both, so an editor and `rig apply` agree on every key.
GIT_CONFIG_KEY_JSON_PATTERN = r"^[A-Za-z][A-Za-z0-9-]*(\.[^\r\n\x00]+)?\.[A-Za-z][A-Za-z0-9-]*$"
_FORBIDDEN_CHARS = ("\r", "\n", "\x00")

# Keys another rig area owns — declaring them under `git.config` would make two reconcilers fight
# on every apply. Canonical (lowercased section/name) form → the owning block + why.
RESERVED_KEYS: dict[str, str] = {
    "core.hookspath": "the git_hooks dispatcher owns core.hooksPath (git_hooks.dispatcher.set_global_hooks_path)",
    "core.excludesfile": "the gitignore block owns core.excludesfile (gitignore.excludesfile)",
}

TYPE_BOOL = "bool"
TYPE_INT = "int"
TYPE_STRING = "string"


def parse_key(key: object) -> tuple[str, str | None, str] | None:
    """Split a dotted git key into ``(section, subsection|None, name)``, or ``None`` if invalid.

    Split on the FIRST dot (section) and the LAST dot (name); the middle is the subsection, which
    may itself contain dots. Fail-closed on anything git would reject or that cannot round-trip a
    single ``git config`` line (a line break / NUL anywhere).
    """
    if not isinstance(key, str) or "." not in key:
        return None
    if any(ch in key for ch in _FORBIDDEN_CHARS):
        return None
    first = key.index(".")
    last = key.rindex(".")
    section = key[:first]
    name = key[last + 1:]
    subsection = key[first + 1:last] if last > first else None
    if not _KEY_PART_RE.match(section) or not _KEY_PART_RE.match(name):
        return None
    if subsection is not None and subsection == "":
        return None
    return section, subsection, name


def canonical_key(key: str) -> str:
    """git's identity for a key: section + name lowercased, subsection kept verbatim."""
    parsed = parse_key(key)
    if parsed is None:
        raise ValueError(f"not a valid git config key: {key!r}")
    section, subsection, name = parsed
    middle = f".{subsection}" if subsection is not None else ""
    return f"{section.lower()}{middle}.{name.lower()}"


def value_type(value: Any) -> str:
    """The comparison type of a validated config value (bool before int: bool is an int subclass)."""
    if isinstance(value, bool):
        return TYPE_BOOL
    if isinstance(value, int):
        return TYPE_INT
    return TYPE_STRING


def render_value(value: Any) -> str:
    """The exact string handed to ``git config --global <key> <value>``."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def global_gitconfig_path() -> Path:
    """The file ``git config --global`` writes — for DISPLAY (plan target / drift rows) only.

    ``$GIT_CONFIG_GLOBAL`` wins; else ``~/.gitconfig``, unless only the XDG file exists (git then
    writes ``$XDG_CONFIG_HOME/git/config``). rig never opens this path itself — every read/write
    goes through ``git config --global`` so git's own resolution is the one that counts.
    """
    override = os.environ.get("GIT_CONFIG_GLOBAL", "").strip()
    if override:
        return Path(override)
    home_file = Path.home() / ".gitconfig"
    xdg_base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    xdg_file = (Path(xdg_base) if xdg_base else Path.home() / ".config") / "git" / "config"
    if not home_file.exists() and xdg_file.exists():
        return xdg_file
    return home_file


def desired_config(block: Any) -> dict[str, dict[str, str]]:
    """``{key: {"value": <git string>, "type": bool|int|string}}`` for a validated ``git`` block.

    ``{}`` when the block is absent, not a mapping, explicitly ``enabled: false``, or declares no
    keys. Keys keep the user's spelling (git normalizes case itself); sorted for a stable plan.
    """
    if not isinstance(block, dict) or block.get("enabled") is False:
        return {}
    cfg = block.get("config")
    if not isinstance(cfg, dict):
        return {}
    return {
        str(key): {"value": render_value(value), "type": value_type(value)}
        for key, value in sorted(cfg.items(), key=lambda kv: str(kv[0]))
    }


# ── git's value semantics (so a hand-written `yes` is not drift against a desired `true`) ──────
_TRUE_WORDS = {"true", "yes", "on"}
_FALSE_WORDS = {"false", "no", "off", ""}
_INT_SUFFIX = {"k": 1024, "m": 1024**2, "g": 1024**3}


def _parse_git_int(raw: str) -> int | None:
    text = raw.strip()
    if not text:
        return None
    mult = 1
    if text[-1].lower() in _INT_SUFFIX:
        mult = _INT_SUFFIX[text[-1].lower()]
        text = text[:-1]
    try:
        return int(text, 10) * mult
    except ValueError:
        return None


def _parse_git_bool(raw: str) -> bool | None:
    """git's ``git_parse_maybe_bool`` + its integer fallback (nonzero → true); ``None`` = not a bool."""
    word = raw.strip().lower()
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    number = _parse_git_int(word)
    if number is None:
        return None
    return number != 0


def values_match(current: str, desired: str, type_: str) -> bool:
    """Does git's ``current`` value already mean ``desired`` (compared as ``type_``)?"""
    if type_ == TYPE_BOOL:
        want = _parse_git_bool(desired)
        return want is not None and _parse_git_bool(current) == want
    if type_ == TYPE_INT:
        want_int = _parse_git_int(desired)
        return want_int is not None and _parse_git_int(current) == want_int
    return current == desired


@dataclass(frozen=True)
class KeyState:
    """One declared key vs the global gitconfig. ``state`` is the apply/drift/verify switch."""

    key: str
    state: str  # ok | missing | modified
    desired: str
    current: str | None
    detail: str


def resolve_key(key: str, desired: str, type_: str) -> KeyState:
    """Classify ONE declared key against ``git config --global --get``. Never writes.

    - ``ok``: git already holds a value that means ``desired`` (by git's bool/int semantics).
    - ``missing``: the key is unset in the global gitconfig.
    - ``modified``: set, but to something else (``current`` names it, for the restore hint).
    """
    from .actions.runner import _git_global_get

    current = _git_global_get(key)
    if current is None:
        return KeyState(key, "missing", desired, None, f"{key} is not set (apply sets it to {desired!r})")
    if values_match(current, desired, type_):
        return KeyState(key, "ok", desired, current, f"{key} = {current!r}")
    return KeyState(
        key, "modified", desired, current,
        f"global {key} is {current!r}, config wants {desired!r} (apply sets it)",
    )


def resolve(options: dict[str, Any]) -> list[KeyState]:
    """Classify every key an action's ``options["config"]`` declares, in key order."""
    entries = options.get("config") or {}
    return [
        resolve_key(key, str(spec.get("value", "")), str(spec.get("type", TYPE_STRING)))
        for key, spec in entries.items()
    ]
