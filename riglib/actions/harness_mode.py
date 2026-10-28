"""Harness permission-mode (auto/yolo) provisioning, including the self-merge permission carve-outs."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import fsutil
from ..harness_mode import HARNESS_MODES
from ..plan import Action
from .result import ActionResult
from .shared import _set_nested, _should_backup, approval_lookup
from .toml_edit import (
    _TOML_UNVERIFIABLE_REMEDY,
    _toml_parser,
    _toml_parses,
    read_toml_root_key,
    upsert_toml_root_key,
)


# ── harness auto-mode / permission provisioning ───────────────────────────────────
# The JSON key each supported harness uses for its permission mode. Keep this beside the
# handler so the runner and the drift check (which imports these) agree on the on-disk shape.
_HARNESS_MODE_KEY = {
    "claude-code": ("permissions", "defaultMode"),
}

# The auto-mode self-merge carve-out. Rig APPENDS this string to ``autoMode.allow`` (an additive
# merge, like the permissions/MCP merges) to clear soft blocks for the agent's own PRs. The full
# rationale + safety argument is the source of truth in docs/config-schema.md (see #self-merge).
# ``SELF_MERGE_CARVE_OUT`` is intentionally PUBLIC (no underscore): drift.py and the tests import it
# as the shared "what rig writes" constant so apply and status can never diverge.
# NOTE: the backticks inside the string are LOAD-BEARING — the classifier renders `gh ship`,
# `--admin`, etc. as code spans; do not "clean" them away.
_AUTO_MODE_SECTION = "autoMode"

_AUTO_MODE_ALLOW_KEY = "allow"

_AUTO_MODE_DEFAULTS = "$defaults"

SELF_MERGE_CARVE_OUT = (
    "Self-Merge via gh ship: The user has standing intent for the agent to merge a PR it "
    "authored in the session's starting repo via `gh ship` / `gh pr merge`. This clears the "
    "Merge Without Review and Self-Approval soft blocks for the agent's own PRs. Does NOT cover "
    "`--admin`/`--force` review bypass, merging a PR the agent isn't working on, or any exfil "
    "concern (still judged by the hard Data Exfiltration rule)."
)


def self_merge_allow_present(data: dict) -> bool:
    """True when the self-merge carve-out is already in ``autoMode.allow`` (shared with drift).

    Guards its own precondition: a non-dict ``data`` (a settings file that parsed to a JSON
    list/scalar) yields ``False`` rather than raising, so callers need no pre-check.
    """
    if not isinstance(data, dict):
        return False
    block = data.get(_AUTO_MODE_SECTION)
    if not isinstance(block, dict):
        return False
    lst = block.get(_AUTO_MODE_ALLOW_KEY)
    return isinstance(lst, list) and SELF_MERGE_CARVE_OUT in lst


def _ensure_self_merge_allow(data: dict) -> bool:
    """Additively ensure ``autoMode.allow`` carries the carve-out. Returns True if it changed.

    Never clobbers: an absent/non-list list is seeded with ``["$defaults"]`` then the carve-out
    appended; an existing list keeps every entry and gets the carve-out appended once. Sibling
    sections (``soft_deny``/``hard_deny``/``environment``) are never touched.
    """
    block = data.get(_AUTO_MODE_SECTION)
    if not isinstance(block, dict):
        block = {}
        data[_AUTO_MODE_SECTION] = block
    lst = block.get(_AUTO_MODE_ALLOW_KEY)
    if not isinstance(lst, list):
        # the one lossy edge in an otherwise purely-additive function: a malformed `allow` value
        # (e.g. a stray string) is replaced by the inheritance sentinel rather than preserved —
        # there is nothing meaningful to keep from a non-list allow list.
        lst = [_AUTO_MODE_DEFAULTS]
    if SELF_MERGE_CARVE_OUT in lst:
        return False
    block[_AUTO_MODE_ALLOW_KEY] = [*lst, SELF_MERGE_CARVE_OUT]
    return True


# The permissions.allow entries that actually unblock `gh ship` for a self-merge. The natural-language
# ``autoMode.allow`` carve-out above clears the SEMANTIC soft blocks (Merge-Without-Review /
# Self-Approval), but the top-level auto-mode Bash permission gate vetoes `gh ship` BEFORE the
# classifier ever judges the merge — and that gate is bypassed ONLY by an explicit
# ``permissions.allow`` rule. So self-merge needs BOTH: the Bash-gate allow rules here (the hard
# unblock) and the carve-out above (the soft-block clearance). The ``Bash(gh pr merge:*)`` DENY (owned
# by provision_permissions) stays intact — ship.sh runs `gh pr merge` as a child process, not a gated
# tool call, so `gh ship` remains the ONLY merge path; we whitelist that gate, not a raw merge.
# ``SELF_MERGE_PERMISSIONS_ALLOW`` is PUBLIC (no underscore): drift.py and the tests import it as the
# shared "what rig writes" constant so apply and status can never diverge.
_PERMISSIONS_SECTION = "permissions"

_PERMISSIONS_ALLOW_KEY = "allow"

_PERMISSIONS_DENY_KEY = "deny"

SELF_MERGE_PERMISSIONS_ALLOW: tuple[str, ...] = (
    "Bash(gh ship:*)",
    "Bash(fj ship:*)",
)

# `gh ship` / `fj ship` are the only ship doors, so the raw scripts are DENIED (rig-cli#635). Every
# rule uses the space-wildcard form (`Bash(cmd *)`); a leading/inner `*` combined with the trailing
# `:*` prefix syntax is rejected by Claude Code as never-matching. Each shape is listed with and
# without arguments because `Bash(x *)` does not match the bare `x`. This covers the common invocation
# shapes (bare, ./, ../, absolute, ~/, .claude/, ci/, scripts/, via bash/sh/zsh/source), not every
# directory or wrapper (`env`, `exec`, `bin/`).
_SHIP_SCRIPT_NAMES: tuple[str, ...] = ("ship.sh", "pr-ship.sh")

_SHIP_SCRIPT_PATH_PREFIXES: tuple[str, ...] = (
    "./", "../", "/", "~/", ".claude/", "ci/", "scripts/",
    "bash ", "sh ", "zsh ", "source ", ". ",
)

_SHIP_SCRIPT_INVOCATIONS: tuple[str, ...] = _SHIP_SCRIPT_NAMES + tuple(
    f"{prefix}*ship.sh" for prefix in _SHIP_SCRIPT_PATH_PREFIXES
)

SELF_MERGE_PERMISSIONS_DENY: tuple[str, ...] = tuple(
    f"Bash({prefix}{suffix})" for prefix in _SHIP_SCRIPT_INVOCATIONS for suffix in ("", " *")
)

# Older rig versions wrote these into ``permissions.allow``; the syntax never matched and Claude Code
# warns about it on every start. Apply removes exactly these two strings (exact-match migration).
SELF_MERGE_LEGACY_ALLOW: tuple[str, ...] = (
    "Bash(*/pr-ship.sh:*)",
    "Bash(*/ship.sh:*)",
)


def self_merge_permissions_present(data: dict) -> bool:
    """True when EVERY self-merge ship rule is already in ``permissions.allow`` (shared with drift).

    Guards its own precondition like :func:`self_merge_allow_present`: a non-dict ``data`` or a
    mis-shaped ``permissions``/``allow`` node yields ``False`` rather than raising.
    """
    if not isinstance(data, dict):
        return False
    block = data.get(_PERMISSIONS_SECTION)
    if not isinstance(block, dict):
        return False
    lst = block.get(_PERMISSIONS_ALLOW_KEY)
    if not isinstance(lst, list):
        return False
    deny = block.get(_PERMISSIONS_DENY_KEY)
    if not isinstance(deny, list):
        return False
    return (
        all(rule in lst for rule in SELF_MERGE_PERMISSIONS_ALLOW)
        and all(rule in deny for rule in SELF_MERGE_PERMISSIONS_DENY)
        and not any(rule in lst for rule in SELF_MERGE_LEGACY_ALLOW)
    )


def self_merge_legacy_allow_present(data: dict) -> bool:
    """True when ``permissions.allow`` still carries a never-matching legacy ship-script rule."""
    perms = data.get(_PERMISSIONS_SECTION) if isinstance(data, dict) else None
    lst = perms.get(_PERMISSIONS_ALLOW_KEY) if isinstance(perms, dict) else None
    return isinstance(lst, list) and any(rule in lst for rule in SELF_MERGE_LEGACY_ALLOW)


def self_merge_permissions_addable(data: dict) -> bool:
    """True when a self-merge apply WOULD add ship rules — mirrors :func:`_ensure_self_merge_permissions`.

    Distinguishes ABSENT (apply seeds it → addable) from MALFORMED (a non-dict ``permissions`` or a
    non-list ``allow`` → apply leaves it, ``provision_permissions`` reports the shape → NOT addable).
    Drift uses this instead of ``not self_merge_permissions_present`` so a malformed file does not
    get a second, inaccurately-worded 'ship rules absent' row on top of the shape-drift one.
    """
    if not isinstance(data, dict):
        return False
    perms = data.get(_PERMISSIONS_SECTION)
    if perms is None:
        return True  # apply seeds the section
    if not isinstance(perms, dict):
        return False  # malformed section — surfaced by the mode-key/permissions shape check
    lst = perms.get(_PERMISSIONS_ALLOW_KEY)
    deny = perms.get(_PERMISSIONS_DENY_KEY)
    if any(node is not None and not isinstance(node, list) for node in (lst, deny)):
        return False  # malformed — surfaced by provision_permissions' fail-closed shape check
    if lst is None or deny is None:
        return True  # apply seeds the list
    return (
        any(rule not in lst for rule in SELF_MERGE_PERMISSIONS_ALLOW)
        or any(rule not in deny for rule in SELF_MERGE_PERMISSIONS_DENY)
        or any(rule in lst for rule in SELF_MERGE_LEGACY_ALLOW)
    )


def _ensure_self_merge_permissions(data: dict) -> bool:
    """Ensure ``permissions`` carries the ship rules. Returns True if it changed.

    Adds the ``gh ship`` / ``fj ship`` allow rules and the raw-script deny rules, and removes the
    two never-matching legacy allow strings (:data:`SELF_MERGE_LEGACY_ALLOW`) — the only non-additive
    step, an exact-string migration.

    SELF-TARGETS the ``permissions`` section directly (creating it if absent) rather than the harness
    action's mode section — the ship rules always live under ``permissions.allow``/``deny``, and
    :func:`self_merge_permissions_present` (drift) reads exactly there, so apply and status can never
    diverge even if the mode key ever moves out of ``permissions``. Never clobbers: every existing
    allow entry is kept, only the MISSING ship rules are appended (a re-apply is a no-op).

    A MALFORMED ``permissions`` section or ``allow`` list (present but wrong type) is LEFT UNTOUCHED
    and we return ``False`` — unlike the autoMode helper we never overwrite it. ``apply_harness`` runs
    before ``provision_permissions``, whose fail-closed shape validation is the authoritative place to
    reject a mis-shaped list; silently replacing it here would destroy the user's value AND mask that
    error. Only an ABSENT section/list is created.
    """
    perms = data.get(_PERMISSIONS_SECTION)
    if perms is None:
        perms = {}
        data[_PERMISSIONS_SECTION] = perms
    elif not isinstance(perms, dict):
        return False  # malformed section — the mode-key path / provision_permissions surfaces this
    lst = perms.get(_PERMISSIONS_ALLOW_KEY)
    deny = perms.get(_PERMISSIONS_DENY_KEY)
    if _is_malformed_rule_list(lst) or _is_malformed_rule_list(deny):
        return False  # malformed — leave it for provision_permissions to fail-close on
    lst = lst if isinstance(lst, list) else []
    deny = deny if isinstance(deny, list) else []
    kept, added, denied, removed = _ship_rule_delta(lst, deny)
    if not (added or denied or removed):
        return False
    perms[_PERMISSIONS_ALLOW_KEY] = [*kept, *added]
    if denied:
        perms[_PERMISSIONS_DENY_KEY] = [*deny, *denied]
    return True


def _ship_rule_delta(lst: list, deny: list) -> tuple[list, list, list, list]:
    """``(kept, added, denied, removed)``: surviving allow entries, ship allow rules to add,
    ship deny rules to add, and legacy allow entries to drop."""
    removed = [rule for rule in lst if rule in SELF_MERGE_LEGACY_ALLOW]
    kept = [rule for rule in lst if rule not in removed]
    added = [rule for rule in SELF_MERGE_PERMISSIONS_ALLOW if rule not in kept]
    denied = [rule for rule in SELF_MERGE_PERMISSIONS_DENY if rule not in deny]
    return kept, added, denied, removed


def _is_malformed_rule_list(node: object) -> bool:
    """A rule list that is present but not a list (absent is fine — it gets seeded)."""
    return node is not None and not isinstance(node, list)


# mode_status values that mean "the auto/permission-mode key needs no write" (already correct, or
# left untouched under on_conflict=skip). The additive carve-out can still change the file.
_MODE_KEY_UNCHANGED = (None, "kept")


def _mode_key_written(status: str | None) -> bool:
    return status not in _MODE_KEY_UNCHANGED


def harness_settings_file(action: Action) -> Path:
    """The settings file an ``apply_harness`` action targets (shared with drift).

    Any suffixed target is an explicit file path; a suffixless target is a directory that
    contains the harness's default settings filename.
    """
    target = action.target
    return target if target.suffix else target / "settings.json"


def desired_harness_value(action: Action) -> tuple[tuple[str, str], str]:
    """Return ((section, key), value) the harness settings file should contain.

    Shared by the install action and drift so both agree on what auto-mode writes. Raises
    ``KeyError`` for an unsupported kind — the plan only emits supported kinds.
    """
    kind = str(action.options.get("kind", "claude-code"))
    section, key = _HARNESS_MODE_KEY[kind]
    value = str(action.options.get("mode_value", ""))
    return (section, key), value


def _load_harness_json(
    config_file: Path, action: Action, on_conflict: str
) -> tuple[dict, str] | ActionResult:
    """Load the harness settings JSON, handling absent/malformed files.

    Returns ``(data, backup_note)`` on success, or an early ``ActionResult`` when the file is
    malformed under ``on_conflict=skip`` / is not a JSON object. A malformed file under a
    non-skip policy is backed up and treated as empty (never silently discarded).
    """
    if not config_file.is_file():
        return {}, ""
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
    except ValueError:
        if on_conflict == "skip":
            return ActionResult(
                action, "skipped",
                f"harness/{action.item}: existing {config_file} is malformed JSON "
                "(on_conflict=skip), left untouched",
            )
        bak = fsutil.backup_path(config_file)
        shutil.copy2(str(config_file), str(bak))
        return {}, f" (backed up malformed config → {bak})"
    if not isinstance(data, dict):
        return ActionResult(action, "error", f"harness/{action.item}: {config_file} is not a JSON object")
    return data, ""


def _do_apply_harness(action: Action, on_conflict: str) -> ActionResult:
    """Write the harness auto/permission mode — claude-code keeps its dedicated writer (self-merge,
    project-vs-user file); every other kind goes through the generic per-format mode write."""
    if str(action.options.get("kind", "claude-code")) == "claude-code":
        return _do_apply_harness_claude_code(action, on_conflict)
    return _do_apply_harness_mode(action, on_conflict)


def desired_harness_mode(action: Action) -> tuple[tuple[str, ...], str]:
    """``(key_path, value)`` the generic mode write converges to — from the per-kind registry.

    Shared by the writer and drift so both agree on what rig manages for a non-claude kind."""
    kind = str(action.options.get("kind", "claude-code"))
    # the plan ALWAYS resolves mode_value; a missing one is a programming error, not a blank write
    return HARNESS_MODES[kind].key_path, str(action.options["mode_value"])


def harness_mode_probe(action: Action) -> tuple[str, object]:
    """READ-ONLY state of the managed mode key on disk (shared by the writer and drift).

    Returns ``(state, value)`` with state one of ``absent`` (no file), ``malformed`` (unparseable
    file), ``unverifiable`` (a TOML file rig cannot validate on this Python — no ``tomllib`` /
    ``tomli``; value names the remedy), ``shape`` (a parent is not an object — value names it),
    ``conflict`` (a value rig must not rewrite blind, e.g. a TOML inline table), ``missing`` (key
    not set) or ``present``."""
    key_path, _ = desired_harness_mode(action)
    config_file = action.target
    fmt = str(action.options.get("format", "json"))
    if fmt == "toml" and _toml_parser() is None:
        # BEFORE the existence check: a fresh config.toml is not created either (rig cannot verify
        # what it would write), and drift must say the same thing apply will — never "missing"
        # while apply then errors
        return "unverifiable", _TOML_UNVERIFIABLE_REMEDY
    if not config_file.is_file():
        return "absent", None
    try:
        text = config_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "malformed", None
    if fmt == "toml":
        if not _toml_parses(text):
            return "malformed", None
        # the registry's TOML entries are ROOT keys only (pinned by test_toml_mode_keys_are_root_keys)
        return read_toml_root_key(text, key_path[0])
    try:
        data = json.loads(text) if text.strip() else {}  # a touched, empty file is merely missing
    except ValueError:
        return "malformed", None
    if not isinstance(data, dict):
        return "shape", "<root>"
    scalar_parent = _scalar_wildcard_parent(data, key_path)
    if scalar_parent is not None:
        return "present", scalar_parent
    state, value = approval_lookup(data, key_path)
    if state == "present" and isinstance(value, (dict, list)):
        # an object/array under the managed key is not a value rig may rewrite blind — the same
        # contract the TOML path keeps for an inline table (reported, left for the user)
        return "conflict", f"{'.'.join(key_path)} = <{type(value).__name__}>"
    return state, value


def _scalar_wildcard_parent(data: dict, key_path: tuple[str, ...]) -> str | None:
    """opencode accepts ``"permission": "ask"`` as shorthand for ``{"*": "ask"}`` — for a managed
    ``<parent>.*`` key a STRING parent is that value, not a shape error. Returns it, else None."""
    if len(key_path) != 2 or key_path[1] != "*":
        return None
    parent = data.get(key_path[0])
    return parent if isinstance(parent, str) else None


def _do_apply_harness_mode(action: Action, on_conflict: str) -> ActionResult:
    """Generic mode write for a non-claude kind: converge ONE key in the harness's own file.

    Idempotent (an equal value is ``skipped``), backup-noted (a differing prior value is backed
    up under ``on_conflict=backup``, left untouched under ``skip``), never clobbering: a malformed
    file, a mis-shaped parent or a non-scalar value is reported and left for the user (drift stays
    visible). Every other key in the file is preserved — JSON is re-serialized (2-space indent, like
    the other JSON writers), TOML is edited as text (a root key upserted before the first table).
    """
    key_path, value = desired_harness_mode(action)
    config_file = action.target
    state, current = harness_mode_probe(action)
    label = f"harness/{action.item}: {'.'.join(key_path)}"
    refusal = _harness_mode_refusal(action, label, state, current, value, on_conflict)
    if refusal is not None:
        return refusal
    merged = _merge_harness_mode(action, label, state, key_path, value)
    if isinstance(merged, ActionResult):
        return merged
    backup_note = ""
    existed = state != "absent"
    # any rewrite of an EXISTING harness config is backed up under the default policy — like the
    # codex hook-bridge TOML writer (a hand-maintained file deserves a restore path even for a
    # key add), not only on a differing prior value.
    if existed and _should_backup(on_conflict):
        bak = fsutil.backup_path(config_file)
        shutil.copy2(str(config_file), str(bak))
        backup_note = f" (backed up prior → {bak})"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(merged, encoding="utf-8")
    status = "backed_up" if backup_note else ("updated" if existed else "created")
    auto = "auto-mode ON" if action.options.get("auto_mode") else "interactive"
    return ActionResult(action, status, f"{label} → '{value}' ({auto}) in {config_file}{backup_note}")


def _harness_mode_refusal(
    action: Action, label: str, state: str, current: object, value: str, on_conflict: str
) -> ActionResult | None:
    """The early error/skip result for a probe state that must not be written, else None."""
    config_file = action.target
    if state == "malformed":
        return ActionResult(action, "error", f"{label}: {config_file} is malformed — fix it by hand; rig never rewrites a config it cannot parse")
    if state == "unverifiable":
        return ActionResult(action, "error", f"{label}: {config_file} left untouched — {current}")
    if state == "shape":
        return ActionResult(action, "error", f"{label}: '{current}' is not an object in {config_file}")
    if state == "conflict":
        return ActionResult(action, "skipped", f"{label}: {current} in {config_file} is not a plain value — left untouched (drift visible)")
    if state == "present" and current == value:
        return ActionResult(action, "skipped", f"{label} already '{value}' in {config_file}")
    if state == "present" and on_conflict == "skip":
        return ActionResult(action, "skipped", f"{label} is '{current}' (on_conflict=skip), left untouched")
    return None


def _merge_harness_mode(
    action: Action, label: str, state: str, key_path: tuple[str, ...], value: str
) -> str | ActionResult:
    """The merged config text with the managed key set, or an early ``ActionResult``."""
    config_file = action.target
    fmt = str(action.options.get("format", "json"))
    if fmt == "toml":
        merged, conflict = upsert_toml_root_key(config_file.read_text(encoding="utf-8") if state != "absent" else "", key_path[0], value)
        if conflict:
            return ActionResult(action, "skipped", f"{label}: {conflict} in {config_file} — left untouched (drift visible)")
        if _toml_parses(merged) is not True:  # never leave the harness a config it cannot load
            return ActionResult(action, "error", f"{label}: merged {config_file} would not parse as TOML — left untouched")
        return merged
    if fmt == "json":
        text = config_file.read_text(encoding="utf-8") if state != "absent" else ""
        data = json.loads(text) if text.strip() else {}
        _set_nested(data, key_path, value)
        return json.dumps(data, indent=2) + "\n"
    # only the registry decides a format; a kind whose file rig does not serialize (omp's
    # yaml is owned by the approval action) must never fall through to the JSON writer
    raise ValueError(f"harness/{action.item}: no mode writer for format {fmt!r}")


def _do_apply_harness_claude_code(action: Action, on_conflict: str) -> ActionResult:
    """Merge the harness auto/permission mode + self-merge unblock into the settings JSON.

    Reconciles three managed things in the one file, all idempotent and additive-safe:
    - ``permissions.defaultMode`` (the auto/permission mode) — a DIFFERENT prior value is backed
      up before converging under ``on_conflict=backup`` (skip leaves it; overwrite replaces w/o a
      backup).
    - ``permissions.allow`` (the self-merge ship rules — ``Bash(gh ship:*)`` etc., only when
      ``options['self_merge']`` is set) — the HARD unblock that clears the auto-mode Bash permission
      gate so `gh ship` runs. APPENDED, never clobbering the user's own entries or the sibling
      ``permissions.deny`` (the ``Bash(gh pr merge:*)`` deny stays: `gh ship` is still the only path).
    - ``autoMode.allow`` (the self-merge natural-language carve-out, same ``self_merge`` gate) — the
      SOFT-block clearance (Merge-Without-Review / Self-Approval), APPENDED once.
    Every other setting in the file is preserved.
    """
    (section, key), value = desired_harness_value(action)
    config_file = harness_settings_file(action)
    config_file.parent.mkdir(parents=True, exist_ok=True)

    loaded = _load_harness_json(config_file, action, on_conflict)
    if isinstance(loaded, ActionResult):
        return loaded
    data, backup_note = loaded

    sect = data.get(section, {})
    if not isinstance(sect, dict):
        return ActionResult(action, "error", f"harness/{action.item}: '{section}' is not an object in {config_file}")
    # bind the section live so an additive ``permissions.allow`` append persists even when the
    # mode-key write below is a no-op (an unbound ``sect`` copy would drop the ship rules).
    data[section] = sect

    current = sect.get(key)
    mode_status = _mode_key_status(current, value, on_conflict)

    self_merge = bool(action.options.get("self_merge"))
    # The defaultMode this apply LEAVES on disk: the value we write, or the prior value kept under skip.
    resulting_mode = current if mode_status == "kept" else value
    # SOFT-block carve-out (autoMode.allow) + HARD Bash-gate unblock (permissions.allow ship rules), both
    # self-targeting their own section off ``data`` (not the mode ``sect``) so writer and drift readers
    # agree regardless of where the mode key lives. The carve-out is INERT without auto (the classifier
    # only runs then) → safe whenever self_merge is configured. The ship rules are ACTIVE in EVERY mode,
    # so write them ONLY when the RESULTING mode equals the declared auto value: a skip that left an
    # interactive defaultMode must not pre-approve `gh ship` (codex #159 P2). Gating on ``value`` (not a
    # literal "auto") keeps the writer symmetric with drift's ``current == value`` reader.
    carveout_added = _ensure_self_merge_allow(data) if self_merge else False
    perms_added = _ensure_self_merge_permissions(data) if (self_merge and resulting_mode == value) else False
    extra_changed = carveout_added or perms_added

    if not _mode_key_written(mode_status) and not extra_changed:
        return ActionResult(action, "skipped", _harness_noop_detail(action, section, key, value, data))

    if mode_status == "backed_up" and backup_note == "":
        bak = fsutil.backup_path(config_file)
        shutil.copy2(str(config_file), str(bak))
        backup_note = f" (backed up prior → {bak})"

    if _mode_key_written(mode_status):
        sect[key] = value

    config_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    # a carve-out/ship-rule-only write (mode key None/"kept") still changed the file → report "updated".
    status = mode_status if _mode_key_written(mode_status) else "updated"
    return ActionResult(
        action, status,
        _harness_write_detail(
            action, (section, key), value, mode_status, carveout_added, perms_added, config_file, backup_note
        ),
    )


def _mode_key_status(current: object, value: str, on_conflict: str) -> str | None:
    """What the mode key write is — None (already correct), "kept" (conflict left under skip),
    or a write status. The additive carve-out/ship-rules never trigger a backup."""
    if current == value:
        return None
    if current is not None and on_conflict == "skip":
        return "kept"
    if current is None:
        return "created"
    return "backed_up" if on_conflict == "backup" else "updated"


def _harness_noop_detail(action: Action, section: str, key: str, value: str, data: dict) -> str:
    """The 'nothing changed' detail — notes which self-merge pieces are ACTUALLY present.

    Reads live state (not just the config intent) so a malformed ``permissions.allow`` — which
    :func:`_ensure_self_merge_permissions` deliberately left untouched — is not misreported as
    'ship rules present'."""
    detail = f"harness/{action.item}: {section}.{key} already '{value}'"
    if action.options.get("self_merge"):
        present = []
        if self_merge_permissions_present(data):
            present.append("ship rules")
        if self_merge_allow_present(data):
            present.append("carve-out")
        if present:
            detail += f"; self-merge {' + '.join(present)} present"
    return detail


def _self_merge_write_note(carveout_added: bool, perms_added: bool) -> str:
    """The ``; self-merge …`` suffix describing which self-merge pieces this write added."""
    added = []
    if perms_added:
        added.append(f"{_PERMISSIONS_SECTION}.{_PERMISSIONS_ALLOW_KEY} ship rules")
    if carveout_added:
        added.append(f"{_AUTO_MODE_SECTION}.{_AUTO_MODE_ALLOW_KEY} carve-out")
    return f"; self-merge {' + '.join(added)} added" if added else ""


def _harness_write_detail(
    action: Action, mode_key: tuple[str, str], value: str,
    mode_status: str | None, carveout_added: bool, perms_added: bool, config_file: Path, backup_note: str,
) -> str:
    """Describe what the write did — the mode key and/or the additive self-merge ship rules/carve-out."""
    section, key = mode_key
    self_merge_note = _self_merge_write_note(carveout_added, perms_added)
    if not _mode_key_written(mode_status):
        left = " (mode key left, on_conflict=skip)" if mode_status == "kept" else ""
        note = self_merge_note.lstrip("; ") or "self-merge provisioning"
        return f"harness/{action.item}: {note}{left} in {config_file}{backup_note}"
    auto = "auto-mode ON" if action.options.get("auto_mode") else "interactive"
    return f"harness/{action.item}: {section}.{key} → '{value}' ({auto}){self_merge_note} in {config_file}{backup_note}"
