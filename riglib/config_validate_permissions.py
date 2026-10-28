"""Validator for the ``permissions`` config block (per-harness command allowlist).

Separate from the other block validators because it owns the permission-kind sets derived from
:mod:`riglib.permissions`, so the validator and the plan's fan-out cannot drift.
"""

from __future__ import annotations

import re
from typing import Any

from .config_checks import check_bool, reject_unknown_keys
from .config_constants import DEPRECATED_HARNESS_KINDS
from .config_errors import ConfigError
from .permissions import (
    HARNESS_ALLOWLIST_NA,
    HARNESS_ALLOWLISTS,
    HARNESS_EXECPOLICY,
    HARNESS_GUARD,
    HARNESS_INSTRUCTION_POLICY,
)

# The keys the permissions block accepts. Listed once so the validator rejects a typo (fail-closed,
# consistent with every other block).
PERMISSIONS_KEYS = {"enabled", "kind", "tools", "extra", "disable", "allow_gh", "settings_path",
                    "allow", "deny", "ask"}
# Harness kinds the permissions capability provisions when PINNED — any kind with a real
# permissions SURFACE: the allowlist kinds, the execpolicy kind (codex), the guard kind (omp),
# and the advisory instruction-policy kinds (pi/commandcode). Derived from
# :mod:`riglib.permissions` (the single source of truth) so the validator and the plan's
# fan-out can never drift. A kind with NO surface at all lands in the (currently empty) N/A
# set — rejected with a clear message rather than silently writing nothing. NOTE: `pi` also
# carries a STRONGER, real-enforcement surface beyond the advisory instruction-policy block
# already counted here — the `permission-guard` extension + rig-written policy file (see
# `HARNESS_PERMISSION_EXTENSIONS` / `harness_permission_extension()` in `riglib.permissions`) —
# but that does not change `pi`'s membership in this set, since it was already valid via
# `HARNESS_INSTRUCTION_POLICY` before the extension existed.
VALID_PERMISSIONS_KINDS = (
    set(HARNESS_ALLOWLISTS) | set(HARNESS_EXECPOLICY)
    | set(HARNESS_GUARD) | set(HARNESS_INSTRUCTION_POLICY)
)
NA_PERMISSIONS_KINDS = set(HARNESS_ALLOWLIST_NA) - VALID_PERMISSIONS_KINDS
# A pre-allowed tool is a single command token: it must START with an alphanumeric or ``/`` (an
# absolute path) — never a dash (a leading-dash entry like ``-rf`` / ``--flag`` would render a
# nonsensical/surprising allowlist entry) — and otherwise contain only letters, digits, and the
# chars that legitimately appear in a command name or path (``.``/``_``/``-``/``/``). No spaces, no
# shell metachars; ``..`` is rejected separately (path traversal).
PERMISSION_TOOL_RE = re.compile(r"^[A-Za-z0-9/][A-Za-z0-9._/-]*$")
# A RAW permission-rule entry (``allow``/``deny``/``ask``) is ``Tool`` or ``Tool(specifier)`` in
# the harness's own syntax: ``WebFetch``, ``mcp__pencil``, ``Bash(git push * --force *)``,
# ``Read(//tmp/**)``. Shape-checked only — the tool-name part is a bare token (glob ``*`` allowed:
# deny rules accept tool-name globs like ``mcp__*``), the specifier is any non-empty
# parenthesized string. Semantics belong to the harness; rig never interprets the pattern.
# Multi-line entries are rejected as typos; keep this aligned with the JSON Schema pattern.
PERMISSION_RULE_RE = re.compile(r"^(?!.*[\r\n])[A-Za-z0-9_*.-]+(\(.+\))?\Z")


def _validate_kind(kind: Any) -> None:
    """Check ``permissions.kind``: a string naming a harness with a mergeable allowlist."""
    if kind is None:
        return
    if not isinstance(kind, str):
        raise ConfigError(f"permissions.kind must be a string, got {kind!r}", schema_path="permissions.kind")
    if kind in DEPRECATED_HARNESS_KINDS:
        raise ConfigError(
            f"permissions.kind '{kind}' is no longer supported (deprecated). "
            f"{DEPRECATED_HARNESS_KINDS[kind]}",
            fix=f"remove permissions.kind or use one of: {', '.join(sorted(VALID_PERMISSIONS_KINDS))}",
            schema_path="permissions.kind",
        )
    if kind in NA_PERMISSIONS_KINDS:
        raise ConfigError(
            f"permissions.kind '{kind}' has no additively-mergeable command allowlist "
            f"(supported: {sorted(VALID_PERMISSIONS_KINDS)}). Remove permissions.kind or use a "
            f"supported harness.",
            fix=f"use one of: {', '.join(sorted(VALID_PERMISSIONS_KINDS))}, or remove permissions.kind",
            schema_path="permissions.kind",
        )
    if kind not in VALID_PERMISSIONS_KINDS:
        raise ConfigError(
            f"permissions.kind must be one of {sorted(VALID_PERMISSIONS_KINDS)}, got {kind!r}",
            fix=f"use one of: {', '.join(sorted(VALID_PERMISSIONS_KINDS))}",
            schema_path="permissions.kind",
        )


def _validate_tool_lists(p: dict[str, Any]) -> None:
    """Check ``tools``/``extra``/``disable``: lists of plain command tokens."""
    for listkey in ("tools", "extra", "disable"):
        value = p.get(listkey)
        if value is None:
            continue
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(
                f"permissions.{listkey} must be a list of strings, got {value!r}",
                schema_path=f"permissions.{listkey}",
            )
        # A tool name becomes a Bash(<name>:*) / "<name> *" allowlist ENTRY — a space or shell
        # metachar would render a broken/surprising entry (Bash(git status:*), Bash(:*)). Restrict
        # to a plain command token so the rendered entry always means what the author intends.
        for v in value:
            if not PERMISSION_TOOL_RE.match(v) or ".." in v:
                raise ConfigError(
                    f"permissions.{listkey} entry {v!r} is not a plain command name or "
                    "absolute path (allowed: letters, digits, '.', '_', '-', '/'; no spaces, "
                    "metachars, or '..')",
                    schema_path=f"permissions.{listkey}",
                )


def _validate_settings_path(settings_path: Any) -> None:
    """Check ``permissions.settings_path``: a string ending in ``.json``."""
    if settings_path is None:
        return
    if not isinstance(settings_path, str):
        raise ConfigError(
            f"permissions.settings_path must be a string, got {settings_path!r}",
            schema_path="permissions.settings_path",
        )
    # both supported harnesses store the allowlist in a JSON file; a non-.json override would be
    # silently nested as <path>/settings.json by the runner. Require .json so the write lands
    # exactly where the user pointed.
    if not settings_path.endswith(".json"):
        raise ConfigError(
            f"permissions.settings_path must end in .json (the harness allowlist is a JSON file), "
            f"got {settings_path!r}",
            schema_path="permissions.settings_path",
        )


def validate_permission_rule_lists(p: dict[str, Any]) -> None:
    """Validate ``allow``/``deny``/``ask`` — RAW harness permission-rule entries (rig-cli#100).

    Unlike ``tools`` (bare command names rig RENDERS into entries), these are full rule strings
    in the harness's own syntax: ``allow`` adds raw entries on top of the tool-derived allowlist
    (the adopted hand-grown machine allowlist), ``deny``/``ask`` REPLACE the baked baseline in
    :mod:`riglib.permissions`. Shape-checked only — ``Tool`` or ``Tool(specifier)`` with a
    non-empty specifier — never semantics (the harness owns matcher semantics). Rejects the
    classic typo shapes fail-closed: a non-list, non-string items, whitespace outside the
    parens (``Bash rm``), an empty ``Tool()``, newlines.
    """
    for listkey in ("allow", "deny", "ask"):
        value = p.get(listkey)
        if value is None:
            continue
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(
                f"permissions.{listkey} must be a list of strings, got {value!r}",
                schema_path=f"permissions.{listkey}",
            )
        for v in value:
            if not PERMISSION_RULE_RE.match(v):
                raise ConfigError(
                    f"permissions.{listkey} entry {v!r} is not a permission rule "
                    "(expected Tool or Tool(specifier), e.g. WebFetch or Bash(git status:*))",
                    schema_path=f"permissions.{listkey}",
                )


def validate_permissions(p: dict[str, Any]) -> None:
    """Validate the ``permissions`` block — the per-harness command allowlist rig provisions.

    rig pre-allows our ecosystem CLIs (tg/review/draw/3d/rig/task/dev/pm/research) plus read-only
    helper tools (rg/jq/gitleaks) plus gh (the GitHub CLI — not read-only, see
    ``riglib.permissions``'s module docstring for the residual write-surface it trades in) in the
    harness's permission allowlist so the agent never stops to ask for a known-safe command.
    Default **ON**: an EMPTY/absent block still provisions the DEFAULT tool set (a present block
    with ``enabled`` not false opts in). The list is config-driven — ``tools`` (a list of command
    names) REPLACES the default set, ``extra`` adds, ``disable`` removes, ``allow_gh: false`` is
    sugar for ``disable: [gh]``. Fail-closed, consistent with every other block, on: a non-mapping
    block, an unknown key (typo guard), a non-bool ``enabled``/``allow_gh``, a non-string-list
    ``tools``/``extra``/``disable``, and a non-string ``settings_path``.
    """
    if not isinstance(p, dict):
        raise ConfigError("permissions must be a mapping", schema_path="permissions")
    if not p:
        return
    reject_unknown_keys(p, "permissions")
    check_bool(p, "enabled", "permissions.enabled")
    check_bool(p, "allow_gh", "permissions.allow_gh")
    _validate_kind(p.get("kind"))
    _validate_tool_lists(p)
    validate_permission_rule_lists(p)
    _validate_settings_path(p.get("settings_path"))
