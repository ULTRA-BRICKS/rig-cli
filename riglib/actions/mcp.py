"""MCP server registration handler and the server-entry helpers it shares with drift checks."""

from __future__ import annotations

import json
import shlex
import shutil
from pathlib import Path

from . import fsutil
from ..plan import Action
from .result import ActionResult


def parse_mcp_command(command: str) -> dict:
    """Split an MCP launch command into ``{command, args}`` with shell-aware quoting.

    Shared by the install action and drift detection so both interpret quoted args and
    spaces-in-paths identically. ``shlex.split`` handles quotes; an unparsable string falls
    back to a naive split rather than raising (the entry is still recorded).
    """
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    if not parts:
        return {"command": command, "args": []}
    return {"command": parts[0], "args": parts[1:]}


def desired_mcp_server_entry(options: dict) -> dict:
    """Build the desired ``mcpServers.<name>`` entry from an action's options.

    Back-compat: without an explicit ``args`` key, ``command`` is parsed as the legacy shell-like
    command string. With ``args`` present, ``command`` is the executable value exactly and ``args``
    is the argv list exactly.
    """
    command = str(options.get("command", ""))
    if "args" in options:
        args = options.get("args", [])
        entry = {"command": command, "args": list(args) if isinstance(args, list) else []}
    else:
        entry = parse_mcp_command(command)
    env = options.get("env")
    if isinstance(env, dict) and env:
        entry["env"] = dict(env)
    return entry


def _do_register_mcp(action: Action, on_conflict: str) -> ActionResult:
    """Merge an MCP server entry into the harness MCP config, idempotent by name.

    The harness config is a JSON file (``<target>/mcp.json`` or the target if it's a
    .json file). We merge under ``mcpServers.<name>`` and never overwrite an existing
    differing entry unless ``on_conflict=overwrite``.
    """
    command = str(action.options.get("command", "")).strip()
    # the registration KEY is the configured server name (falls back to the item name).
    server_key = str(action.options.get("server") or action.item)
    if not command:
        return ActionResult(action, "skipped", f"mcp/{action.item}: no command set, nothing to register")

    target = action.target
    config_file = target if target.suffix == ".json" else target / "mcp.json"
    config_file.parent.mkdir(parents=True, exist_ok=True)

    loaded = _load_mcp_config(action, config_file, on_conflict)
    if isinstance(loaded, ActionResult):
        return loaded
    data, backup_note = loaded
    if not isinstance(data, dict):
        return ActionResult(action, "error", f"mcp/{action.item}: {config_file} is not a JSON object")
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        return ActionResult(action, "error", f"mcp/{action.item}: mcpServers is not an object in {config_file}")
    entry = desired_mcp_server_entry(action.options)

    status = "created"
    if server_key in servers:
        unchanged = _existing_entry_result(action, server_key, servers[server_key] == entry, on_conflict)
        if unchanged is not None:
            return unchanged
        status = "backed_up" if on_conflict == "backup" else "updated"
        if status == "backed_up" and backup_note == "":
            backup_note = _backup_whole_config(config_file)
    servers[server_key] = entry
    config_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return ActionResult(action, status, f"mcp/{server_key} registered → {config_file}{backup_note}")


def _existing_entry_result(
    action: Action, server_key: str, identical: bool, on_conflict: str
) -> ActionResult | None:
    """The skip result when the server is already registered (or must be left alone), else None."""
    if identical:
        return ActionResult(action, "skipped", f"mcp/{server_key}: already registered")
    if on_conflict == "skip":
        return ActionResult(
            action, "skipped",
            f"mcp/{server_key}: entry exists (on_conflict=skip), left untouched",
        )
    return None


def _backup_whole_config(config_file: Path) -> str:
    """Back up the whole config before converging a differing entry, so apply can reconcile
    MCP drift under the default policy (not only under overwrite)."""
    bak = fsutil.backup_path(config_file)
    shutil.copy2(str(config_file), str(bak))
    return f" (backed up prior → {bak})"



def _load_mcp_config(action: Action, config_file: Path, on_conflict: str) -> tuple[object, str] | ActionResult:
    """Read the existing MCP config as ``(data, backup_note)``, or an early ``ActionResult``."""
    if not config_file.is_file():
        return {}, ""
    try:
        return json.loads(config_file.read_text(encoding="utf-8")), ""
    except ValueError:
        # a malformed existing config must NOT be silently discarded — that would
        # erase the user's MCP setup. Per on_conflict: skip leaves it, others back it
        # up; never overwrite blind.
        if on_conflict == "skip":
            return ActionResult(
                action, "skipped",
                f"mcp/{action.item}: existing {config_file} is malformed JSON (on_conflict=skip), left untouched",
            )
        bak = fsutil.backup_path(config_file)
        shutil.copy2(str(config_file), str(bak))
        return {}, f" (backed up malformed config → {bak})"

