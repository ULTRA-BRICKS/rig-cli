"""Validator for the ``tmux`` config block (rig-managed tmux config provisioning).

Separate from ``config.py`` because the block has ~15 nested sub-blocks; each gets its own small
checker so the validator stays within the complexity gate. Errors here intentionally carry no
``schema_path`` for nested knobs — that is the established wire contract of this block.
"""

from __future__ import annotations

from typing import Any

from .config_checks import reject_unknown_keys
from .config_constants import VALID_TMUX_APPLY
from .config_errors import ConfigError

# The nested sub-blocks the tmux block accepts, each with its own allowed keys. Listed once so
# the validator rejects a typo in any of them (fail-closed, consistent with every other block).
_TMUX_TOP_KEYS = {
    "enabled",
    "apply",
    "conf_path",
    "generated_dir",
    "resurrect",
    "continuum",
    "moshi",
    "cc_restore",
    "omp_restore",
    "codex_restore",
    "anti_sprawl",
    "boot",
    "login_shell",
    "autosave",
    "recovery",
    "pane_titles",
    "focus_events",
    "clipboard",
    "copy_mode",
}
_TMUX_SUBKEYS = {
    "resurrect": {"processes", "capture_pane_contents"},
    "continuum": {"restore", "boot", "save_interval"},
    "moshi": {"enabled"},
    "cc_restore": {"enabled"},
    "omp_restore": {"enabled"},
    "codex_restore": {"enabled"},
    "anti_sprawl": {"enabled", "session"},
    "boot": {"enabled", "label"},
    "login_shell": {"enabled", "shell"},
    "autosave": {"enabled", "label", "stale_after"},
    "recovery": {"enabled"},
    "pane_titles": {"enabled", "position", "format", "clear_status_right"},
    "focus_events": {"enabled"},
    "clipboard": {"enabled", "command"},
    "copy_mode": {"mode_keys"},
}

# Sub-blocks whose ``enabled`` knob is checked, in the order the original validator checked them.
_ENABLED_SUBBLOCKS = (
    "moshi",
    "cc_restore",
    "omp_restore",
    "codex_restore",
    "anti_sprawl",
    "boot",
    "login_shell",
    "autosave",
    "recovery",
    "pane_titles",
    "focus_events",
    "clipboard",
)


def _sub(t: dict[str, Any], name: str) -> dict[str, Any]:
    """The sub-block ``name`` if it is a mapping, else ``{}`` (non-mappings are rejected earlier)."""
    block = t.get(name, {})
    return block if isinstance(block, dict) else {}


def _opt_bool(block: dict[str, Any], key: str, path: str) -> None:
    value = block.get(key)
    if value is not None and not isinstance(value, bool):
        raise ConfigError(f"{path} must be a bool, got {value!r}")


def _opt_str(block: dict[str, Any], key: str, path: str) -> None:
    value = block.get(key)
    if value is not None and not isinstance(value, str):
        raise ConfigError(f"{path} must be a string, got {value!r}")


def _opt_int_min1(block: dict[str, Any], key: str, path: str) -> None:
    value = block.get(key)
    # bool is an int subclass — reject it so `true` can't pose as a count.
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
        raise ConfigError(f"{path} must be an int >= 1, got {value!r}")


def _opt_enum(block: dict[str, Any], key: str, path: str, allowed: Any) -> None:
    value = block.get(key)
    if value is not None and (not isinstance(value, str) or value not in allowed):
        raise ConfigError(
            f"{path} must be one of {sorted(allowed)}, got {value!r}",
            fix=f"use one of: {', '.join(sorted(allowed))}",
            schema_path=path,
        )


def _check_top_scalars(t: dict[str, Any]) -> None:
    enabled = t.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise ConfigError(f"tmux.enabled must be a bool, got {enabled!r}", schema_path="tmux.enabled")
    # `apply` PRESENT must be a valid enum string. A present null would `str(None)` into a bogus
    # "None" apply_mode, and an unhashable value (list/dict) would raise a raw TypeError from the
    # `in` check — so require a string in the enum. Only an ABSENT key falls back to the default.
    if "apply" in t and (not isinstance(t["apply"], str) or t["apply"] not in VALID_TMUX_APPLY):
        raise ConfigError(
            f"tmux.apply must be one of {sorted(VALID_TMUX_APPLY)}, got {t['apply']!r}",
            fix=f"use one of: {', '.join(sorted(VALID_TMUX_APPLY))}",
            schema_path="tmux.apply",
        )
    for pathkey in ("conf_path", "generated_dir"):
        # A key PRESENT with no value (YAML `conf_path:` → None) must fail closed: it can't fall
        # back to the default the way a truly-absent key does (the plan would `str(None)` it into
        # a literal "None" path). Only an absent key is allowed; a present non-string is rejected.
        if pathkey in t and not isinstance(t[pathkey], str):
            raise ConfigError(
                f"tmux.{pathkey} must be a string, got {t[pathkey]!r}",
                schema_path=f"tmux.{pathkey}",
            )


def _check_subblock_shapes(t: dict[str, Any]) -> None:
    for sub in _TMUX_SUBKEYS:
        block = t.get(sub)
        if block is None:
            continue
        if not isinstance(block, dict):
            raise ConfigError(f"tmux.{sub} must be a mapping, got {block!r}", schema_path=f"tmux.{sub}")
        reject_unknown_keys(block, f"tmux.{sub}")


def _check_resurrect(res: dict[str, Any]) -> None:
    procs = res.get("processes")
    if procs is not None and (not isinstance(procs, list) or not all(isinstance(p, str) for p in procs)):
        raise ConfigError(f"tmux.resurrect.processes must be a list of strings, got {procs!r}")
    _opt_bool(res, "capture_pane_contents", "tmux.resurrect.capture_pane_contents")


def _check_continuum(cont: dict[str, Any]) -> None:
    for boolkey in ("restore", "boot"):
        _opt_bool(cont, boolkey, f"tmux.continuum.{boolkey}")
    _opt_int_min1(cont, "save_interval", "tmux.continuum.save_interval")


def _check_enabled_flags(t: dict[str, Any]) -> None:
    for sub in _ENABLED_SUBBLOCKS:
        _opt_bool(_sub(t, sub), "enabled", f"tmux.{sub}.enabled")


def _check_clipboard(clipboard: dict[str, Any]) -> None:
    # Same double-quoted-tmux-value injection hazard as `pane_titles.format` (the command is
    # interpolated into `copy-pipe-and-cancel "<command>"`) -- reuse the SAME check
    # (`_clipboard_command_is_safe` delegates to `_pane_titles_format_is_safe`) rather than
    # re-deriving the character class here.
    from .tmux import _clipboard_command_is_safe

    command = clipboard.get("command")
    if command is None:
        return
    if not isinstance(command, str):
        raise ConfigError(f"tmux.clipboard.command must be a string, got {command!r}")
    if command and not _clipboard_command_is_safe(command):
        raise ConfigError(
            r"""tmux.clipboard.command must not contain '"', '\', '$', '#(' (a """
            "shell-exec token), or a non-printable character other than a plain tab "
            "(they can corrupt, inject into, or execute code from the generated tmux "
            f"config), got {command!r}",
            fix=(
                "drop the offending character/sequence, or leave `command` unset to use "
                "the platform-detected clipboard command"
            ),
            schema_path="tmux.clipboard.command",
        )


def _check_copy_mode(copy_mode: dict[str, Any]) -> None:
    # "vi" (default) emits `setw -g mode-keys vi`, making copy-mode-vi (and its v/y bindings,
    # including the clipboard bindings above) the LIVE table -- tmux's own default is
    # `emacs`, under which copy-mode-vi never activates. Independent of `tmux.clipboard`.
    from .tmux import VALID_MODE_KEYS

    _opt_enum(copy_mode, "mode_keys", "tmux.copy_mode.mode_keys", VALID_MODE_KEYS)


def _check_pane_titles(pane_titles: dict[str, Any]) -> None:
    # Imported here: tmux.py has no internal riglib imports (no cycle risk); it is the ONE source
    # for both the valid-position set and the format-safety check, shared with
    # tmux.build_tmux's own defense-in-depth clamp so the two can never drift.
    from .tmux import VALID_PANE_TITLES_POSITIONS, _pane_titles_format_is_safe

    _opt_enum(pane_titles, "position", "tmux.pane_titles.position", VALID_PANE_TITLES_POSITIONS)
    fmt = pane_titles.get("format")
    if fmt is not None:
        if not isinstance(fmt, str):
            raise ConfigError(f"tmux.pane_titles.format must be a string, got {fmt!r}")
        if not _pane_titles_format_is_safe(fmt):
            raise ConfigError(
                r"""tmux.pane_titles.format must not contain '"', '\', '$', '#(' (a """
                "shell-exec token), or a non-printable character other than a plain tab "
                "(they can corrupt, inject into, or execute code from the generated tmux "
                f"config), got {fmt!r}",
                fix=(
                    "drop the offending character/sequence — tmux format variables use "
                    r"""'#{...}', never '$' or '#('"""
                ),
                schema_path="tmux.pane_titles.format",
            )
    _opt_bool(pane_titles, "clear_status_right", "tmux.pane_titles.clear_status_right")


def _check_autosave(autosave: dict[str, Any]) -> None:
    _opt_str(autosave, "label", "tmux.autosave.label")
    _opt_int_min1(autosave, "stale_after", "tmux.autosave.stale_after")


def _check_login_shell(login: dict[str, Any]) -> None:
    shell = login.get("shell")
    if shell is None:
        return
    if not isinstance(shell, str):
        raise ConfigError(f"tmux.login_shell.shell must be a string, got {shell!r}")
    # Empty string → "resolve $SHELL at apply". A NON-empty override must be an ABSOLUTE path to
    # the shell BINARY ONLY — no relative name, and NO arguments (whitespace). rig appends ` -l`
    # itself; a value like `/bin/zsh -l` would render `'/bin/zsh -l' -l`, making tmux try to exec
    # a binary literally named "/bin/zsh -l".
    if shell and (not shell.startswith("/") or any(c.isspace() for c in shell)):
        raise ConfigError(
            "tmux.login_shell.shell must be an absolute path to the shell BINARY with no "
            f"arguments (rig adds `-l`), or empty to use $SHELL — got {shell!r}"
        )


def validate_tmux(t: dict[str, Any]) -> None:
    """Validate the ``tmux`` block — rig-managed tmux configuration provisioning.

    rig GENERATES a tmux config from this block (guaranteeing plugin-init ordering so the
    Moshi status-right tweak can't wipe continuum's autosave hook) and MIGRATES an existing
    hand-written ``~/.tmux.conf`` into an import (or a sentinel-fenced managed block). An
    EMPTY/absent block means "leave tmux alone". Fail-closed, consistent with every other
    block, on: a non-mapping block, an unknown top-level/nested key (typo guard), a non-bool
    bool knob, a bad ``apply`` enum, a non-list-of-strings ``resurrect.processes``, and a
    ``continuum.save_interval`` that is not an int >= 1.
    """
    if not isinstance(t, dict):
        raise ConfigError("tmux must be a mapping", schema_path="tmux")
    if not t:
        return
    reject_unknown_keys(t, "tmux")
    _check_top_scalars(t)
    _check_subblock_shapes(t)
    _check_resurrect(_sub(t, "resurrect"))
    _check_continuum(_sub(t, "continuum"))
    _check_enabled_flags(t)
    _check_clipboard(_sub(t, "clipboard"))
    _check_copy_mode(_sub(t, "copy_mode"))
    _check_pane_titles(_sub(t, "pane_titles"))
    _check_autosave(_sub(t, "autosave"))
    _opt_str(_sub(t, "anti_sprawl"), "session", "tmux.anti_sprawl.session")
    _opt_str(_sub(t, "boot"), "label", "tmux.boot.label")
    _check_login_shell(_sub(t, "login_shell"))
