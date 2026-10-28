"""Harness approval-policy provisioning with its restore-point receipt."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import fsutil
from ..harness_mode import HARNESS_MODES
from ..plan import Action
from .harness_guard import _guard_in_place
from .omp_yaml_state import _mark_omp_yaml_created, _omp_yaml_skip_existing
from .result import ActionResult
from .shared import _set_nested, _should_backup, approval_lookup


# ── declarative approval posture provisioning (omp ~/.omp/agent/config.yml) ─────────────────
# Additive key merge with a sidecar RECEIPT: YAML survives no comment, so the receipt records
# what rig manages, the PREVIOUS values (the deprovision/restore story), the backup identity,
# and the template version. A matching value WITHOUT a receipt is 'compatible unmanaged' —
# first-class, never clobbered; apply ADOPTS it (writes only the receipt). A DIFFERING user
# value is drift and is never overwritten.
APPROVAL_RECEIPT_NAME = ".rig-permissions-receipt.json"


def approval_config_file(action: Action) -> Path:
    """The config file a ``provision_harness_approval`` action targets (shared with drift)."""
    return action.target


def approval_receipt_file(action: Action) -> Path:
    return approval_config_file(action).parent / APPROVAL_RECEIPT_NAME


def approval_desired_keys(action: Action) -> tuple[tuple[tuple[str, ...], str], ...]:
    from ..permissions import HARNESS_APPROVAL

    kind = str(action.options.get("kind", ""))
    try:
        keys = HARNESS_APPROVAL[kind].keys
    except KeyError:
        raise ValueError(f"no approval-posture mechanism for harness kind {kind!r}") from None
    # The plan resolves the posture value from the harness auto intent (riglib.harness_mode); a
    # hand-built action without it keeps the registry's legacy value (yolo).
    mode_value = action.options.get("mode_value")
    if mode_value is None:
        return keys
    # ONE mode key per approval entry — the override below applies the single resolved value
    if len(keys) != 1:
        raise ValueError(f"approval posture for {kind!r} manages {len(keys)} keys; mode_value expects exactly one")
    return ((keys[0][0], str(mode_value)),)


def read_approval_receipt(receipt_file: Path) -> dict:
    """rig's approval receipt as written (``{}`` when absent, unreadable or malformed — every
    differing value then counts as the user's). Shared by the writer and drift."""
    try:
        loaded = json.loads(receipt_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def receipt_managed_entries(receipt: dict) -> dict[str, dict]:
    """``{dotted key: {previous, installed, ...}}`` — the per-key entries of a receipt."""
    managed = receipt.get("managed")
    if not isinstance(managed, dict):
        return {}
    return {k: v for k, v in managed.items() if isinstance(v, dict)}


def receipt_installed_values(receipt: dict) -> dict[str, object]:
    """``{dotted key: installed value}`` from rig's approval receipt — the values rig itself wrote.

    A managed key still holding the value rig installed is rig-OWNED: when the config's intent
    changes (omp ``auto_mode: true`` → ``false``: yolo → always-ask) apply converges it, instead of
    mistaking rig's own prior write for a user-set value and leaving the harness relaxed forever."""
    return {k: v.get("installed") for k, v in receipt_managed_entries(receipt).items()}


def approval_posture_relaxes(action: Action) -> bool:
    """True when any desired approval value is NOT the kind's interactive value — anything short of
    ``always-ask`` (omp ``yolo``, but also the intermediate ``write``, which auto-approves edits)
    is a relaxed posture the guard interlock exists for. Only the pure tightening write is exempt:
    it must never be blocked by a missing guard belt (that would leave the user stuck in yolo).
    Fails closed for a kind the registry does not describe."""
    kind = str(action.options.get("kind", ""))
    spec = HARNESS_MODES.get(kind)
    if spec is None:
        return True
    return any(scalar != spec.values[False] for _, scalar in approval_desired_keys(action))


def _restore_point(prior_receipt: dict, backup_identity: str | None) -> str | None:
    """The receipt's file-level restore point across writes: the ORIGINAL pre-rig backup once one
    is recorded; a recorded ``null`` stays ``null`` when rig CREATED the file (there was nothing to
    restore); but an ADOPTED config (rig never rewrote it, so no backup was taken then) takes the
    first backup made now — that IS the user's original file. A receipt without managed entries
    (absent, or malformed by hand) is no provenance at all: this run's backup is recorded."""
    entries = receipt_managed_entries(prior_receipt)
    if not entries:
        return backup_identity
    if prior_receipt.get("backup"):
        return str(prior_receipt["backup"])
    adopted = any(entry.get("adopted") for entry in entries.values())
    return backup_identity if adopted else None


def _receipt_entries(missing, keys, prior: dict[str, dict]) -> dict[str, dict]:
    """The receipt's ``managed`` map for this write. A converged rig-owned key keeps the ORIGINAL
    pre-rig ``previous`` from the prior receipt (a follow-up deprovision must restore what the user
    had — an absent key, not rig's own intermediate write); keys that already matched are adopted."""
    managed: dict[str, dict] = {}
    for key_path, scalar, previous in missing:
        dotted = ".".join(key_path)
        if previous is not None and dotted in prior:  # rig-owned convergence, not a first write
            previous = prior[dotted].get("previous")
        managed[dotted] = {"previous": previous, "installed": scalar}
    # keys that already matched (unmanaged) are ADOPTED into the same receipt — the managed
    # map must cover every managed key, not just the ones this run changed.
    for key_path, scalar in keys:
        dotted = ".".join(key_path)
        if dotted not in managed:
            managed[dotted] = {"previous": scalar, "installed": scalar, "adopted": True}
    return managed


def _do_provision_harness_approval(action: Action, on_conflict: str) -> ActionResult:
    """Merge the approval posture into the harness config — ADDITIVE, receipt-tracked.

    Sets each managed key when absent — or when it still holds the value rig itself installed
    per the receipt (rig-owned: a changed intent converges it); preserves every sibling key. Writes the sidecar
    receipt (managed paths, previous values, backup identity) — the data a FOLLOW-UP
    deprovision flow restores from (no deprovision action exists yet; the receipt's
    existence also drives the adopt-vs-current idempotency). Never clobbers a differing
    user value (skipped + drift); adopts a compatible unmanaged one. A MALFORMED config is
    a hard error (fix by hand) — rig refuses to rewrite a file it cannot parse, exactly
    like the unbalanced-markers discipline. NOTE: the YAML is re-serialized (comments in
    it are not preserved) — the receipt is the provenance channel.
    """
    try:
        keys = approval_desired_keys(action)
    except ValueError as exc:
        return ActionResult(action, "error", f"approval/{action.item}: {exc}")
    config_file = approval_config_file(action)

    # interlocked ONLY when relaxing: a tightening write (always-ask) is the safe direction and
    # must not be refused because the guard belt is missing or drifted
    guard_reason = _guard_in_place(action) if approval_posture_relaxes(action) else None
    if guard_reason is not None:
        return ActionResult(
            action, "error",
            f"approval/{action.item}: {guard_reason} — refusing to relax the approval "
            "posture without the guard belt in place; apply the guard action first",
        )

    existed = config_file.is_file()
    data, load_error = _load_approval_config(action, config_file, existed)
    if load_error is not None:
        return load_error

    receipt_file = approval_receipt_file(action)
    prior_receipt = read_approval_receipt(receipt_file)
    missing, conflicts = _classify_approval_keys(data, keys, receipt_installed_values(prior_receipt))

    if conflicts:
        return ActionResult(
            action, "skipped",
            f"approval/{action.item}: user-set value differs, left untouched (drift visible): "
            + "; ".join(conflicts),
        )

    if not missing:
        if receipt_file.exists():
            return ActionResult(action, "skipped", f"approval/{action.item}: already current in {config_file}")
        return _adopt_unmanaged_approval(action, keys, config_file, receipt_file)

    # Re-serializing an EXISTING yaml destroys its comments/formatting — a conflict-class
    # mutation, not a splice (unlike the execpolicy append, which preserves the rest of the
    # file byte-for-byte). Under ``skip`` that means: leave the file alone and keep the drift.
    # Exception: a sibling writer created this file earlier in THIS run — there is no user
    # content yet, so merging the missing key is first-run completion, not a skip conflict.
    if _omp_yaml_skip_existing(config_file, existed, on_conflict):
        return ActionResult(
            action, "skipped",
            f"approval/{action.item}: {config_file} would be rewritten (yaml comments are not "
            "preserved) but on_conflict=skip — left unchanged (drift NOT reconciled)",
        )
    return _write_approval_merge(
        action, data, keys, missing, config_file, existed, on_conflict, receipt_file, prior_receipt,
    )


def _load_approval_config(action: Action, config_file: Path, existed: bool) -> tuple[dict, ActionResult | None]:
    """Parse the existing harness config into a mapping; a malformed one is a hard error result."""
    import yaml  # lazy, like the rest of the config stack

    if not existed:
        return {}, None
    try:
        loaded = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return {}, ActionResult(
            action, "error",
            f"approval/{action.item}: {config_file} is malformed YAML ({exc.__class__.__name__}) — "
            "fix it by hand; rig never rewrites a config it cannot parse",
        )
    if loaded is not None and not isinstance(loaded, dict):
        return {}, ActionResult(
            action, "error",
            f"approval/{action.item}: {config_file} is not a YAML mapping — fix it by hand",
        )
    return loaded or {}, None


def _classify_approval_keys(
    data: dict, keys, rig_installed: dict[str, str],
) -> tuple[list[tuple[tuple[str, ...], str, str | None]], list[str]]:
    """Split desired keys into ``missing`` (key_path, desired, previous) and ``conflicts``.

    ``previous`` is None for an absent key, else the rig-installed value being converged (the
    config's intent changed, e.g. auto_mode true → false)."""
    missing: list[tuple[tuple[str, ...], str, str | None]] = []
    conflicts: list[str] = []
    for key_path, scalar in keys:
        state, value = approval_lookup(data, key_path)
        dotted = ".".join(key_path)
        if state == "shape":
            conflicts.append(f"{dotted} parent '{value}' is not an object")
        elif state == "missing":
            missing.append((key_path, scalar, None))
        elif value != scalar and value is not None and rig_installed.get(dotted) == value:
            # rig-owned: the key still holds what rig installed (an explicit null on disk is
            # never rig's — it is the user's, a conflict like any other differing value)
            missing.append((key_path, scalar, str(value)))
        elif value != scalar:
            conflicts.append(f"{dotted} is {value!r} (rig wants {scalar!r})")
    return missing, conflicts


def _adopt_unmanaged_approval(action: Action, keys, config_file: Path, receipt_file: Path) -> ActionResult:
    """'compatible unmanaged': the values already match but rig never recorded them — adopt by
    writing ONLY the receipt (the config file is never rewritten). Same schema as the merge
    path (backup: null — nothing was rewritten, nothing to restore)."""
    receipt = {
        "template": 1,
        "kind": str(action.options.get("kind", "")),
        "managed": {
            ".".join(kp): {"previous": scalar, "installed": scalar, "adopted": True}
            for kp, scalar in keys
        },
        "backup": None,
    }
    receipt_file.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    receipt_file.chmod(0o600)
    return ActionResult(
        action, "updated",
        f"approval/{action.item}: adopted compatible unmanaged value(s) in {config_file} "
        f"(receipt → {receipt_file}; config untouched)",
    )


def _write_approval_merge(
    action: Action, data: dict, keys, missing, config_file: Path, existed: bool,
    on_conflict: str, receipt_file: Path, prior_receipt: dict,
) -> ActionResult:
    """Merge the missing keys into the config, then write the sidecar receipt."""
    import yaml  # lazy, like the rest of the config stack

    config_file.parent.mkdir(parents=True, exist_ok=True)
    backup_note = ""
    backup_identity = None
    if existed and _should_backup(on_conflict):
        bak = fsutil.backup_path(config_file)
        shutil.copy2(str(config_file), str(bak))
        backup_identity = str(bak)
        backup_note = f" (backed up prior → {bak})"
    for key_path, scalar, _previous in missing:
        _set_nested(data, key_path, scalar)  # an absent or explicit-null parent is created
    managed = _receipt_entries(missing, keys, receipt_managed_entries(prior_receipt))
    config_file.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    if not existed:
        _mark_omp_yaml_created(config_file)
    receipt = {
        "template": 1,
        "kind": str(action.options.get("kind", "")),
        "managed": managed,
        # the ORIGINAL pre-rig backup stays the restore point across a convergence; a backup of
        # rig's OWN prior write is named in the action detail only (see _restore_point)
        "backup": _restore_point(prior_receipt, backup_identity),
    }
    receipt_file.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    receipt_file.chmod(0o600)
    status = "backed_up" if backup_note else ("updated" if existed else "created")
    added = ", ".join(".".join(kp) for kp, _, _ in missing)
    return ActionResult(
        action, status,
        f"approval/{action.item}: set {added} in {config_file}{backup_note} "
        "(posture relaxed — enforcement is unverified until the activation probe passes: "
        "RIG_OMP_PROBE=1 rig doctor)",
    )
