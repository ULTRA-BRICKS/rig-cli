"""Validation of the ``linters`` block (per-repo linter/formatter config provisioning).

Owns the fixed per-item/per-bundle key sets, the repo-containment predicate that the validator,
runner and drift all share, and the fail-closed validator. It is separate from ``config.py`` so
the containment rule and the key sets live in one importable place with the validator that
enforces them (the schema registry and plan builder reference the SAME sets).

``linters.items.<n>`` entries each describe a file rig writes and drift-compares: a ``tool``, an
optional ``role``, a repo-relative ``path`` and exactly one of literal ``content`` or an
agent-tools-relative ``source``. rig hardcodes NO specific linter.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .config_errors import ConfigError

LINTER_ITEM_KEYS = {"tool", "role", "path", "content", "source", "enabled"}
LINTER_BUNDLE_KEYS = {"source", "target", "enabled"}
# `role` is DESCRIPTIVE — it is rendered in the apply/drift label (`<role> <tool>:<item>`, see
# runner._linter_label) so a formatter and a linter read distinctly; both roles reconcile identically
# (a config file written + drift-compared). A foreign role is rejected so a typo (`role: format`)
# fails loudly rather than rendering an odd label.
VALID_LINTER_ROLES = {"linter", "formatter"}
_LINTERS_TOP_KEYS = ("enabled", "preview", "rules", "items")


def linter_path_escapes_repo(rel: str) -> bool:
    """True when a ``linters.items.<n>.path`` would write OUTSIDE the repo (reject it).

    A provisioned config file must stay inside the repo: a committed rig.yaml that could write
    anywhere on disk (``../../etc/x``, ``/etc/x``, a Windows ``C:\\...`` / ``C:/...`` drive-absolute,
    or a backslash path that escapes once interpreted on Windows) is a footgun. This is the SINGLE
    predicate the validator (fail-closed at load) AND the runner/drift (defense-in-depth against a
    hand-built or replayed Action) both call, so the containment rule lives in exactly one place.
    Conservative by design — it rejects on ANY of: a POSIX, native, OR Windows-drive absolute (so a
    config authored on one OS can't silently escape on another), a ``..`` component, a backslash, a
    ``.git`` component (a path into the git dir would let a committed rig.yaml rewrite repo metadata
    or install a hook on apply — a privilege-escalation footgun, never a real linter config location),
    or a ``.``-only path (``.``/``./.``) that names the repo ROOT and so can never name a file.
    """
    if not isinstance(rel, str) or not rel:
        return True
    if "\\" in rel:
        return True  # a Windows separator: ambiguous/unsafe across platforms — refuse it.
    # Absolute under ANY interpretation: POSIX (`/x`), the running OS, OR a Windows DRIVE — the last
    # catches `C:/tmp/x` (forward-slashed) on POSIX, where PurePosixPath sees a plain relative name
    # but Windows would treat it as drive-absolute. PureWindowsPath.drive is non-empty for `C:/...`.
    if PurePosixPath(rel).is_absolute() or os.path.isabs(rel):
        return True
    if PureWindowsPath(rel).is_absolute() or PureWindowsPath(rel).drive:
        return True
    # `..` as a path COMPONENT (not a substring — `..foo` is a legal filename). Check both the POSIX
    # split and the OS-native split so `foo/../../bar` is caught regardless of the running platform.
    parts = set(PurePosixPath(rel).parts) | set(Path(rel).parts)
    if ".." in parts:
        return True
    # `.git` as ANY component: a provisioned file inside the git dir (`.git/config`,
    # `.git/hooks/pre-commit`) would let a committed rig.yaml rewrite repo metadata or install a hook
    # on `rig apply` — a privilege-escalation footgun. A linter config NEVER legitimately lives in
    # `.git`, so reject it (case-insensitive: `.GIT` is the same dir on a case-insensitive FS).
    if any(p.lower() == ".git" for p in parts):
        return True
    # A path that is ONLY `.` components (`.`, `./`, `./.`) names the repo ROOT, never a file — it
    # has no `..`/abs so it would otherwise validate clean, yet can never converge (`repo_root / "."
    # == repo_root`, a directory → io_error forever). Reject it at load too, consistent with the
    # fail-closed-at-validation principle: never accept a "valid config that can never name a file".
    return parts <= {"."}


def _check_top_level(li: dict[str, Any]) -> None:
    """Reject unknown top keys and non-bool ``enabled``/``preview`` flags."""
    for key in li:
        if key not in _LINTERS_TOP_KEYS:
            raise ConfigError(
                f"unknown linters key {key!r} (expected one of: enabled, preview, rules, items)",
                schema_path="linters",
            )
    for flag in ("enabled", "preview"):
        if flag in li and not isinstance(li[flag], bool):
            raise ConfigError(
                f"linters.{flag} must be a bool, got {li[flag]!r}", schema_path=f"linters.{flag}"
            )


def _check_rules(li: dict[str, Any]) -> None:
    rules = li.get("rules", {})
    if not isinstance(rules, dict):
        raise ConfigError("linters.rules must be a mapping", schema_path="linters.rules")
    try:
        from .lint_policy import resolve_rule_severities

        resolve_rule_severities(rules)
    except ValueError as exc:
        raise ConfigError(str(exc), schema_path="linters.rules") from exc


def _check_bundle_path(name: Any, spec: dict[str, Any], key: str) -> None:
    value = spec.get(key)
    where = f"linters.bundles.{name}.{key}"
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{where} is required", where)
    if linter_path_escapes_repo(value):
        raise ConfigError(f"{where} must be a safe relative path", where)


def _check_bundle(name: Any, spec: Any) -> None:
    if not isinstance(name, str) or not name or not isinstance(spec, dict):
        raise ConfigError("linters.bundles entries must be named mappings", f"linters.bundles.{name}")
    unknown_bundle = sorted(set(spec) - LINTER_BUNDLE_KEYS)
    if unknown_bundle:
        raise ConfigError(
            f"unknown linters.bundles.{name} key: {unknown_bundle[0]}",
            f"linters.bundles.{name}.{unknown_bundle[0]}",
        )
    if spec.get("enabled") is not None and not isinstance(spec.get("enabled"), bool):
        raise ConfigError(
            f"linters.bundles.{name}.enabled must be a bool", f"linters.bundles.{name}.enabled"
        )
    for key in ("source", "target"):
        _check_bundle_path(name, spec, key)


def _check_bundles(li: dict[str, Any]) -> None:
    bundles = li.get("bundles", {})
    if bundles is None:
        return
    if not isinstance(bundles, dict):
        raise ConfigError("linters.bundles must be a mapping", "linters.bundles")
    for name, spec in bundles.items():
        _check_bundle(name, spec)


def _check_item_shape(path: str, spec: Any) -> None:
    """Mapping-ness, the typo guard on keys, and required non-empty ``tool``/``path``."""
    if not isinstance(spec, dict):
        raise ConfigError(f"{path} must be a mapping", schema_path=path)
    for key in spec:
        if key not in LINTER_ITEM_KEYS:
            raise ConfigError(
                f"unknown {path} key {key!r} (expected one of: {', '.join(sorted(LINTER_ITEM_KEYS))})",
                schema_path=path,
            )
    # tool/path are required. Desired bytes come from exactly one of literal `content` or a
    # canonical agent-tools-relative `source` file.
    for req in ("tool", "path"):
        val = spec.get(req)
        if not isinstance(val, str) or not val:
            raise ConfigError(
                f"{path}.{req} must be a non-empty string, got {val!r}",
                schema_path=f"{path}.{req}",
            )


def _check_item_payload(path: str, spec: dict[str, Any]) -> None:
    """Exactly one of ``content``/``source``, non-empty, and ``source`` must be a safe path."""
    content = spec.get("content")
    source = spec.get("source")
    if (content is None) == (source is None):
        raise ConfigError(
            f"{path} must set exactly one of content or source",
            fix="use content for repo-specific bytes or source for a reusable agent-tools file",
            schema_path=path,
        )
    desired = content if content is not None else source
    desired_key = "content" if content is not None else "source"
    if not isinstance(desired, str) or not desired:
        raise ConfigError(
            f"{path}.{desired_key} must be a non-empty string, got {desired!r}",
            schema_path=f"{path}.{desired_key}",
        )
    if source is not None and (source != source.strip() or linter_path_escapes_repo(source)):
        raise ConfigError(
            f"{path}.source must be a safe agent-tools-relative path, got {source!r}",
            schema_path=f"{path}.source",
        )


def _check_item_target_path(path: str, rel: str) -> None:
    # Reject leading/trailing whitespace in `path`: the runner/drift do NOT strip it (they operate
    # on the literal bytes), so ` ../escape` would validate as one filename here yet escape at
    # apply time, and ` config/x ` would validate as a quoted name but write to `config/x`. Fail
    # closed on the ambiguity — a config path is a literal filename, never whitespace-padded.
    if rel != rel.strip():
        raise ConfigError(
            f"{path}.path must not have leading/trailing whitespace, got {rel!r}",
            schema_path=f"{path}.path",
        )
    # The provisioned file MUST stay inside the repo — see `linter_path_escapes_repo` (the one
    # containment predicate the runner/drift also call, so the rule lives in a single place).
    if linter_path_escapes_repo(rel):
        raise ConfigError(
            f"{path}.path must be a repo-relative path inside the repo (no leading '/', '..', or '\\'), got {rel!r}",
            schema_path=f"{path}.path",
        )


def _check_item_role_and_enabled(path: str, spec: dict[str, Any]) -> None:
    role = spec.get("role")
    # `isinstance(role, str)` BEFORE the membership test: a non-string role (a YAML list/map, e.g.
    # `role: [linter]`) is UNHASHABLE, so `role not in <set>` would raise a raw TypeError instead
    # of a structured ConfigError. Guard the type first so every bad role fails the same clean way.
    if role is not None and (not isinstance(role, str) or role not in VALID_LINTER_ROLES):
        raise ConfigError(
            f"{path}.role must be one of {sorted(VALID_LINTER_ROLES)}, got {role!r}",
            fix=f"use one of: {', '.join(sorted(VALID_LINTER_ROLES))}",
            schema_path=f"{path}.role",
        )
    if "enabled" in spec and not isinstance(spec["enabled"], bool):
        raise ConfigError(
            f"{path}.enabled must be a bool, got {spec['enabled']!r}",
            schema_path=f"{path}.enabled",
        )


def _check_duplicate_target(
    path: str, name: Any, spec: dict[str, Any], seen_paths: dict[str, str]
) -> None:
    # Duplicate-target guard (enabled items only — a disabled item provisions nothing, so it can't
    # collide). Normalize the path (`./a` vs `a`) so `a/b` and `./a/b` are recognized as the same
    # file. Two items on one file is always a config smell; reject it rather than let apply/status
    # churn. A disabled item is skipped, so toggling one off resolves a collision without an edit.
    if spec.get("enabled") is False:
        return
    rel = spec["path"]
    norm = PurePosixPath(rel).as_posix()
    if norm in seen_paths:
        raise ConfigError(
            f"{path}.path {rel!r} is already provisioned by linters.items.{seen_paths[norm]} "
            "— two items must not target the same file",
            fix="give each item a distinct path, or disable one with `enabled: false`",
            schema_path=f"{path}.path",
        )
    seen_paths[norm] = name


def _check_items(li: dict[str, Any]) -> None:
    items = li.get("items", {})
    if not isinstance(items, dict):
        raise ConfigError("linters.items must be a mapping", schema_path="linters.items")
    # Track the target path of each ENABLED item so two items can't both provision the same file:
    # with different content `rig apply` would write one then the other and `rig status` would churn
    # forever (each apply re-flags + re-backs-up the loser). One file = one item.
    seen_paths: dict[str, str] = {}
    for name, spec in items.items():
        path = f"linters.items.{name}"
        _check_item_shape(path, spec)
        _check_item_payload(path, spec)
        _check_item_target_path(path, spec["path"])
        _check_item_role_and_enabled(path, spec)
        _check_duplicate_target(path, name, spec, seen_paths)


def validate_linters(li: dict[str, Any]) -> None:
    """Validate the ``linters`` block — rig's per-repo linter/formatter config provisioning.

    Default **ON**: every declared item is provisioned on ``rig init`` AND ``rig apply``. The block
    carries a fixed ``enabled`` flag plus an open ``items`` map keyed by an arbitrary LABEL; each
    item declares ``{ tool, role?, path, content, enabled? }`` — the tool name, an optional role
    (``linter``/``formatter``, default ``linter``, status-label only), the repo-relative file
    ``path``, and the exact file ``content``. Fail-closed, consistent with every other block, on: a
    non-mapping block, a non-bool ``enabled``, a non-mapping ``items``, a non-mapping item, an
    unknown per-item key (typo guard), a missing/empty/non-string ``tool`` / ``path`` / ``content``,
    an absolute or parent-escaping ``path`` (a provisioned file must stay inside the repo), a
    non-bool item ``enabled``, a ``role`` outside the allowed set, and TWO enabled items targeting
    the SAME normalized ``path`` (which would make apply/status churn forever).
    """
    if not isinstance(li, dict):
        raise ConfigError("linters must be a mapping", schema_path="linters")
    if not li:
        return
    _check_top_level(li)
    _check_rules(li)
    _check_bundles(li)
    _check_items(li)
