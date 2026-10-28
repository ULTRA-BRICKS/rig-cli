"""Ship task-prefix and ``gh`` ship-alias handlers."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import fsutil
from . import ship_delegator
from ..plan import Action, SHIP_TASK_CODE_PREFIX_RE
from .result import ActionResult


def _merge_task_prefix_line(existing: str, new_line: str) -> str:
    """Rewrite the first managed line in place (or append it) and drop any later duplicates."""
    out_lines: list[str] = []
    replaced = False
    for line in existing.splitlines():
        if not line.startswith("SHIP_TASK_CODE_PREFIX="):
            out_lines.append(line)
        elif not replaced:
            # Collapse to exactly ONE managed line — a pre-existing hand-committed duplicate
            # (or one left over from an older rig version) must not accumulate; only the FIRST
            # match is rewritten in place, every subsequent match is dropped rather than kept
            # as a second copy shell sourcing would silently let win/lose depending on order.
            out_lines.append(new_line)
            replaced = True
    if not replaced:
        out_lines.append(new_line)
    return "\n".join(out_lines) + "\n"


def _backup_before_rewrite(path: Path) -> Path:
    """Back up the whole file first, same as every other conflict-backed action.

    Called when ANY line other than the exact desired one — a differing value OR a stale duplicate
    alongside the correct line — is being rewritten/dropped. Checking only the first managed line
    would miss a CORRECT first line followed by a stale duplicate, silently dropping the duplicate
    with no backup even though drift flags that exact state as "modified".
    """
    backup = fsutil.backup_path(path)
    shutil.copy2(str(path), str(backup))
    return backup


def _read_ship_config(path: Path) -> str:
    """Current ``.ship-config`` text, or empty when the file does not exist."""
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _task_prefix_refusal(action: Action, prefix: str) -> ActionResult | None:
    """A skip/error result when ``prefix`` must not be written, else ``None``."""
    if not prefix:
        return ActionResult(action, "skipped", "ship-task-prefix: task.code_prefix not set, .ship-config untouched")
    # Defense-in-depth: plan.py's builder already refuses a malformed prefix before this action
    # is ever created, but re-check the SAME shared pattern here too — this runner is the last
    # gate before the value lands in a file ship.sh sources as shell, and a future caller could
    # construct the action directly, bypassing the plan builder's check entirely.
    if not SHIP_TASK_CODE_PREFIX_RE.fullmatch(prefix):
        return ActionResult(action, "error", f"ship-task-prefix: {prefix!r} is not 1-40 uppercase letters/digits (malformed action)")
    return None


# ── .ship-config's SHIP_TASK_CODE_PREFIX line (from rig.yaml's task.code_prefix) ──────
def _do_provision_ship_task_prefix(action: Action, on_conflict: str) -> ActionResult:
    """Upsert the ``SHIP_TASK_CODE_PREFIX=<prefix>`` line into ``<repo>/.ship-config``.

    Merges into any EXISTING ``.ship-config`` content (``SHIP_LOCAL_TEST_DIR``/
    ``SHIP_LOCAL_TEST_CMD``, comments, ...) rather than replacing the file — this is the only
    rig-managed key in a file whose other two keys are hand-committed per the header doc in
    ``ci/ship/ship.sh``, so a whole-file overwrite would silently drop a human's local-test
    override.

    ``on_conflict`` semantics are KEYED, like :func:`_do_register_mcp`'s ``mcpServers.<name>``
    merge — the conflict is "the managed LINE exists with a different value", never "the FILE
    exists". A file with unrelated hand-committed content (or no managed line at all) is not a
    conflict: the line is simply added, under EVERY ``on_conflict`` policy, exactly as a new
    MCP server entry is added to an existing config. Routing this through
    ``fsutil.write_file``'s whole-file comparison (as an earlier version of this function did)
    would make ``on_conflict=skip`` silently refuse to EVER provision the prefix into any repo
    that already has a hand-committed ``.ship-config`` — the precise repo shape the merge
    design exists to support — leaving ``rig status`` reporting ``missing`` forever with no way
    for ``rig apply`` to converge it.
    """
    prefix = str(action.options.get("code_prefix", "")).strip()
    refusal = _task_prefix_refusal(action, prefix)
    if refusal is not None:
        return refusal
    path = action.target / ".ship-config"
    try:
        existing = _read_ship_config(path)
    except OSError as exc:
        return ActionResult(action, "error", f"ship-task-prefix: could not read {path}: {exc}")
    new_line = f"SHIP_TASK_CODE_PREFIX={prefix}"
    current = [line for line in existing.splitlines() if line.startswith("SHIP_TASK_CODE_PREFIX=")]
    if current == [new_line]:
        return ActionResult(action, "skipped", f"ship-task-prefix: identical: {path}")
    rewrites_other_line = any(c != new_line for c in current)
    if rewrites_other_line and on_conflict == "skip":
        # The MANAGED LINE itself conflicts (not just "the file exists") — this is the one
        # real conflict case skip should honor.
        return ActionResult(
            action, "skipped", f"ship-task-prefix: SHIP_TASK_CODE_PREFIX= line exists (on_conflict=skip), left untouched: {path}"
        )
    new_content = _merge_task_prefix_line(existing, new_line)
    backup = _backup_before_rewrite(path) if rewrites_other_line and on_conflict == "backup" else None
    detail_suffix = f" (backed up prior → {backup})" if backup is not None else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_content, encoding="utf-8")
    status = "backed_up" if backup is not None else ("created" if not existing else "updated")
    return ActionResult(action, status, f"ship-task-prefix: wrote → {path}{detail_suffix}", backup)


# ── machine-global `gh ship` alias (the entry point the delegator serves) ──────────
def _do_provision_gh_ship_alias(action: Action, on_conflict: str) -> ActionResult:
    """Provision/reconcile the machine-global ``gh ship`` alias (idempotent, drift-parity).

    apply and drift switch on the SAME :func:`resolve_gh_ship_alias` state so status never
    misreports. gh-missing self-skips (status/apply parity): a machine without ``gh`` is a clean
    no-op, not a "missing alias" rig could never repair. The live ``gh alias set`` is guarded by
    ``RIG_GH_ALIAS_DRY_RUN`` (the unit suite + CI set it) so no test touches the real gh config.

    Conflict policy (the alias is gh-owned config, so the "backup" is recording the prior value in
    the result detail, not a ``.rig-bak`` file): ``create`` always sets it; a DIFFERING existing
    alias is left as-is under ``on_conflict=skip`` (with the current value noted) and overwritten
    otherwise, the old value recorded. A ``gh alias set`` failure is a soft, noted error, never a
    hard apply abort — gh being absent/erroring is not a rig failure.

    ci-only combo (``ship_delegator`` disabled but ``ci.items.ship.gh_alias`` on): the plan builder
    carries ``canonical_ship`` on this action because NO ``provision_ship_delegator`` action is in
    the plan to reconcile the machine-level env file — the one place the alias's out-of-repo
    fallback reads ``AGENT_TOOLS_ROOT``. Provision it here in that combo so ``gh ship`` doesn't exit
    127 on a clean machine (mirrors the delegator's own env reconcile — same
    :func:`_reconcile_ship_env_file`, same transient-source guard). No self-hosting skip: the alias
    is machine-global, so out-of-repo invocations need the env file even from a self-hosting repo's
    cwd. When the delegator IS enabled it owns the env file and this option is absent (no double
    writer). A failed env write is a hard error — the alias would otherwise resolve to nothing.
    """
    raw_canonical = str(action.options.get("canonical_ship", "")).strip()
    env_status, env_note = "unchanged", ""
    if raw_canonical:
        env_ok, env_note, env_status = ship_delegator._reconcile_ship_env_file(Path(raw_canonical), on_conflict)
        if not env_ok:
            return ActionResult(
                action, "error",
                f"gh-ship-alias: {env_note} — the `gh ship` fallback cannot resolve the agent-tools "
                "checkout without it (gh ship would exit 127 unless AGENT_TOOLS_ROOT is set)",
            )
    alias_result, alias_in_sync = _reconcile_gh_alias(action, on_conflict)
    return _merge_env_into_alias_result(alias_result, env_status, env_note, alias_in_sync)


def _merge_env_into_alias_result(
    result: ActionResult, env_status: str, env_note: str, alias_in_sync: bool
) -> ActionResult:
    """Fold a ci-only-combo env-file reconcile outcome into the alias-set result.

    Mirrors the delegator's partial-outcome contract so the two never disagree:

    - ``unchanged`` env → the alias result stands untouched (a true no-op env reconcile adds nothing).
    - alias ``error`` → still surface the env note in the detail (the env file may have been written
      before the alias failed), but keep the hard ``error`` status.
    - ``skipped_user`` / ``skipped_transient`` env → the machine file was intentionally LEFT AS-IS
      (a user file under ``on_conflict=skip``, or a transient source refused): the reconcile is
      INCOMPLETE, so the action is NOT clean-green even when the alias itself was set. Report
      ``skipped`` (with the note) so ``rig status`` keeps surfacing the env drift.
    - ``written`` env → a real change: bump a would-be ``skipped`` to ``updated`` ONLY when the alias
      is genuinely in sync (``alias_in_sync`` — the skip was "already correct"). A skip that hides
      REMAINING alias drift (a user alias left under ``on_conflict=skip``, gh absent/unreadable, or a
      dry-run "would create") stays ``skipped`` — bumping it to ``updated`` would falsely claim full
      reconciliation while ``rig status`` immediately re-reports the alias drift. A ``created`` /
      ``updated`` alias already reflects a change and keeps its status.
    """
    if env_status == "unchanged":
        return result
    detail = f"{result.detail}; {env_note}"
    if result.status == "error":
        return ActionResult(result.action, "error", detail, result.backup)
    if env_status in ("skipped_user", "skipped_transient"):
        return ActionResult(result.action, "skipped", detail, result.backup)
    # env_status == "written": a real machine-file change.
    status = "updated" if (result.status == "skipped" and alias_in_sync) else result.status
    return ActionResult(result.action, status, detail, result.backup)


def _reconcile_gh_alias(action: Action, on_conflict: str) -> tuple[ActionResult, bool]:
    """Reconcile ONLY the ``gh ship`` alias (no env file). See :func:`_do_provision_gh_ship_alias`.

    Returns ``(result, alias_in_sync)``. ``alias_in_sync`` is True ONLY when the alias already
    equals the desired expansion (a clean no-op) — the single case where a ``skipped`` alias carries
    no remaining drift. Every other ``skipped`` (a conflicting user alias left as-is, gh absent, an
    unreadable config, a dry-run) leaves drift or is unmanageable, so it is NOT in sync; the caller
    uses this to decide whether a companion env write may promote the action to ``updated``.
    """
    from ..gh_ship_alias import resolve_gh_ship_alias, set_gh_ship_alias

    r = resolve_gh_ship_alias()
    if r.state == "no_gh":
        return ActionResult(
            action, "skipped",
            "gh-ship-alias: `gh` CLI not found — skipped (install gh, then re-run rig apply)",
        ), False
    if r.state == "unknown":
        return ActionResult(
            action, "skipped",
            "gh-ship-alias: gh config present but unreadable/malformed — left as-is (rig won't clobber it)",
        ), False
    if r.state == "ok":
        return ActionResult(action, "skipped", "gh-ship-alias: `gh ship` already set"), True
    if r.state == "update" and on_conflict == "skip":
        return ActionResult(
            action, "skipped",
            f"gh-ship-alias: `gh ship` differs but left as-is (on_conflict=skip; current: {r.current})",
        ), False
    if os.environ.get("RIG_GH_ALIAS_DRY_RUN"):
        verb = "would create" if r.state == "create" else "would update"
        return ActionResult(action, "skipped", f"gh-ship-alias: {verb} `gh ship` (dry-run)"), False
    rc = set_gh_ship_alias()
    if rc != 0:
        return ActionResult(action, "error", "gh-ship-alias: `gh alias set` failed (rc != 0)"), False
    if r.state == "update":
        return ActionResult(action, "updated", f"gh-ship-alias: `gh ship` updated (was: {r.current})"), True
    return ActionResult(action, "created", "gh-ship-alias: `gh ship` set"), True
