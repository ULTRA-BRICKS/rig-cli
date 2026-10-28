"""Agent-hook install handlers plus the rig-pin helpers hook descriptors embed."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from . import fsutil
from ..hook_blurb_sync import sync_agent_hook_blurbs
from ..plan import Action
from .result import ActionResult


RIG_PIN_PLACEHOLDER = "/ABSOLUTE/PATH/TO/rig"


class RigPinError(ValueError):
    """No absolute ``rig`` launcher could be found to pin into a hook descriptor (a ``ValueError`` so the
    install action reports it; the drift check reports it too instead of reading clean)."""


def pinned_rig_path() -> str:
    """Absolute path of the ``rig`` launcher to pin into an agent-hook descriptor, replacing
    :data:`RIG_PIN_PLACEHOLDER` in the descriptor's ``args``. A hook that shells out to rig (the
    agent-tools ``block-unlanded-removal`` guard) must run THIS rig — the one that provisioned it —
    never whatever ``rig`` its own PATH resolves, which a shadowing binary or a stale install could
    answer for. The running launcher wins (``sys.argv[0]`` when it is an existing executable file
    named ``rig``, made absolute but NOT symlink-resolved, so it names the stable launcher a user
    runs); an embedding process (``python -m riglib``, a test runner) falls back to ``rig`` on PATH.
    Neither found is an error, never a bare ``rig``: a pin that is not absolute pins nothing.
    """
    argv0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if os.path.basename(argv0) == "rig" and os.path.isfile(argv0) and os.access(argv0, os.X_OK):
        return argv0
    found = shutil.which("rig")
    if found:
        return os.path.abspath(found)
    raise RigPinError(
        "cannot pin rig into the hook descriptor: this process was not started from a `rig` launcher "
        "and no `rig` is on PATH"
    )


def build_hook_descriptor(action: Action) -> tuple[dict, str]:
    """Build the descriptor dict + the script's absolute cmd for an agent-hook action.

    Shared by the install action and the drift check so both agree on what the installed
    descriptor SHOULD contain (absolute ``cmd`` per the agents-hooks/v1 contract + any
    config ``on_error`` override). Raises on a missing/unreadable source descriptor.
    """
    descriptor_name = action.options.get("descriptor") or ""
    src_descriptor = action.source / descriptor_name
    spec = json.loads(src_descriptor.read_text(encoding="utf-8"))
    cmd = str(spec.get("cmd", ""))
    if "/ABSOLUTE/PATH/TO/" in cmd:
        rel = cmd.split("/ABSOLUTE/PATH/TO/", 1)[1]
        cmd = str((Path(action.options["agent_tools_source"]) / rel).resolve())
    elif not os.path.isabs(cmd):
        cmd = str((Path(action.options["agent_tools_source"]) / cmd).resolve())
    spec["cmd"] = cmd
    args = spec.get("args")
    if isinstance(args, list) and RIG_PIN_PLACEHOLDER in args:
        pin = pinned_rig_path()
        spec["args"] = [pin if arg == RIG_PIN_PLACEHOLDER else arg for arg in args]
    on_error = action.options.get("on_error")
    if on_error:
        spec["on_error"] = on_error
    return spec, cmd


def descriptor_text(spec: dict) -> str:
    """The canonical on-disk serialization of a descriptor (single source of truth)."""
    return json.dumps(spec, indent=2) + "\n"


def _do_install_agent_hook(action: Action, on_conflict: str) -> ActionResult:
    """Copy the hook dir + write an absolute-path descriptor into the harness hook dir.

    Per the agents-hooks/v1 contract the descriptor ``cmd`` must be an ABSOLUTE path. We
    rewrite the ``/ABSOLUTE/PATH/TO/...`` placeholder to the real script path inside the
    agent-tools checkout, applying any ``on_error`` override from config.
    """
    descriptor_name = action.options.get("descriptor") or ""
    src_descriptor = action.source / descriptor_name
    if not src_descriptor.is_file():
        return ActionResult(action, "error", f"descriptor not found: {src_descriptor}")
    try:
        spec, cmd = build_hook_descriptor(action)
    except (OSError, ValueError) as exc:
        return ActionResult(action, "error", f"bad descriptor: {exc}")

    # The descriptor's cmd points at a script INSIDE the agent-tools checkout. rig consumes
    # agent-tools READ-ONLY — never chmod (or otherwise mutate) the source. agent-tools
    # ships its hook scripts executable; if one is not, surface that as an explicit warning
    # rather than silently flipping the source's bits.
    note = ""
    script = Path(cmd)
    if script.is_file() and not os.access(script, os.X_OK):
        note = f" (warning: source script not executable: {script})"

    target_descriptor = action.target / descriptor_name
    out = fsutil.write_file(target_descriptor, descriptor_text(spec), on_conflict)
    return ActionResult(action, out.status, f"hook descriptor {out.detail}{note}", out.backup)


def _do_sync_hook_blurbs(action: Action, on_conflict: str) -> ActionResult:
    """Mirror every ``agent-hooks/<name>/blurb.md`` under the agent-tools checkout into the
    SessionStart ``.blurbs`` aggregator dir (agent-tools#504).

    Delegates the actual diff+copy to :func:`riglib.hook_blurb_sync.sync_agent_hook_blurbs`,
    which is already idempotent (skips a target whose content matches) — never consults
    ``on_conflict``: a hook blurb is rig-mirrored content sourced from the hook's own
    ``blurb.md``, not user data a backup would protect.
    """
    existed_before = set(action.target.glob("*.md")) if action.target.is_dir() else set()
    changed = sync_agent_hook_blurbs(action.source, action.target)
    if not changed:
        return ActionResult(action, "skipped", "hook-blurbs: up to date")
    names = ", ".join(sorted(p.stem for p in changed))
    status = "created" if any(p not in existed_before for p in changed) else "updated"
    return ActionResult(action, status, f"hook-blurbs: synced {names}")
