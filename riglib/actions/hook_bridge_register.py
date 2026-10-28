"""Hook-bridge registration entry point: picks the Claude, codex, opencode or omp strategy."""

from __future__ import annotations

import json
import shutil

from . import fsutil
from ..plan import Action
from .hook_bridge import (
    _upsert_bridge,
    hook_bridge_entries,
    hook_bridge_format,
    hook_bridge_module,
    hook_bridge_settings_file,
)
from .hook_bridge_codex import _do_register_codex_hook_bridge
from .hook_bridge_plugins import _do_register_omp_hook_bridge, _do_register_opencode_hook_bridge
from .result import ActionResult
from .shared import _should_backup


def _load_claude_settings(action: Action, config_file, on_conflict: str) -> tuple[dict, str, ActionResult | None]:
    """Read the Claude settings JSON → (data, backup note, early result).

    A malformed file is backed up and replaced by ``{}`` unless ``on_conflict=skip``; a
    non-object document is an error."""
    data: dict = {}
    backup_note = ""
    if config_file.is_file():
        try:
            data = json.loads(config_file.read_text(encoding="utf-8"))
        except ValueError:
            if on_conflict == "skip":
                return data, backup_note, ActionResult(
                    action, "skipped",
                    f"hook_bridge/{action.item}: existing {config_file} is malformed JSON "
                    "(on_conflict=skip), left untouched",
                )
            bak = fsutil.backup_path(config_file)
            shutil.copy2(str(config_file), str(bak))
            data = {}
            backup_note = f" (backed up malformed config → {bak})"
    if not isinstance(data, dict):
        return data, backup_note, ActionResult(
            action, "error", f"hook_bridge/{action.item}: {config_file} is not a JSON object"
        )
    return data, backup_note, None


def _upsert_claude_bridge_hooks(action: Action, hooks: dict, on_conflict: str) -> tuple[int, int]:
    """Upsert every (event, matcher) bridge block into ``hooks`` → (changed, skipped_stale)."""
    changed = 0
    skipped_stale = 0
    for event, pairs in hook_bridge_entries(action).items():
        blocks = hooks.get(event)
        if not isinstance(blocks, list):
            blocks = []
        for matcher, command in pairs:
            outcome = _upsert_bridge(blocks, matcher, command, on_conflict, hook_bridge_module(action))
            if outcome == "changed":
                changed += 1
            elif outcome == "skipped-stale":
                skipped_stale += 1
        hooks[event] = blocks
    return changed, skipped_stale


def _do_register_claude_hook_bridge(action: Action, on_conflict: str) -> ActionResult:
    """Register the bridge dispatcher in a Claude-style ``settings.json``."""
    config_file = hook_bridge_settings_file(action)
    config_file.parent.mkdir(parents=True, exist_ok=True)

    data, backup_note, early = _load_claude_settings(action, config_file, on_conflict)
    if early is not None:
        return early

    hooks = data.get("hooks")
    if hooks is None:
        hooks = {}
    if not isinstance(hooks, dict):
        return ActionResult(action, "error", f"hook_bridge/{action.item}: 'hooks' is not an object in {config_file}")

    changed, skipped_stale = _upsert_claude_bridge_hooks(action, hooks, on_conflict)

    if changed == 0:
        note = "dispatcher already wired"
        if skipped_stale:
            note = f"{skipped_stale} managed hook(s) drifted but left untouched (on_conflict=skip)"
        return ActionResult(action, "skipped", f"hook_bridge/{action.item}: {note} in {config_file}")

    data["hooks"] = hooks
    if _should_backup(on_conflict) and backup_note == "" and config_file.is_file():
        bak = fsutil.backup_path(config_file)
        shutil.copy2(str(config_file), str(bak))
        backup_note = f" (backed up prior → {bak})"
    config_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    status = "backed_up" if backup_note else "created"
    return ActionResult(
        action, status,
        f"hook_bridge/{action.item}: wired {changed} dispatcher hook(s) in {config_file}{backup_note}",
    )


def _do_register_hook_bridge(action: Action, on_conflict: str) -> ActionResult:
    """Register the agents-hooks bridge dispatcher in the harness config.

    Idempotent and additive: each (event, matcher) gets OUR managed block appended only if
    an equivalent managed block (type=command and command contains the bridge module name) is
    not already there.
    Every other hook in the file — the user's rtk-rewrite, tg-ctl, etc. — is preserved
    untouched: ``hooks`` is a SHARED namespace, so we never rewrite a whole event array.

    A managed block whose shape drifted (e.g. the agent-tools lib path moved or the hook type
    is malformed) is rewritten in place — UNLESS ``on_conflict=skip``, which leaves it untouched
    (matching the file-level skip semantics; ``rig status`` still surfaces it as drift). Removing
    an unmanaged matcher's hooks, or a matcher we no longer ship, is left to ``rig status``.
    """
    fmt = hook_bridge_format(action)
    if fmt == "toml":
        return _do_register_codex_hook_bridge(action, on_conflict)
    if fmt == "opencode-plugin":
        return _do_register_opencode_hook_bridge(action, on_conflict)
    if fmt == "omp-extension":
        return _do_register_omp_hook_bridge(action, on_conflict)
    return _do_register_claude_hook_bridge(action, on_conflict)
