"""Hook-bridge entries, targets and the Claude settings.json bridge block helpers."""

from __future__ import annotations

import math
import shlex
from pathlib import Path

from ..config import OMP_HOOK_BRIDGE_EXTENSION_NAME, OPENCODE_HOOK_BRIDGE_PLUGIN_NAME
from ..plan import Action


# ── agents-hooks/v1 → harness bridge registration ───────────────────────────────────
# A managed bridge candidate is identified by this module substring; the in-sync shape
# still requires type="command" plus the exact command we would write.
_BRIDGE_MARKER = "cc_hook_bridge"

# Minimum OUTER `timeout` (seconds) on every rig-managed claude-code bridge hook (UB-25).
# Claude Code kills a command hook at its `timeout` (600s when absent), and a timed-out
# PreToolUse hook does NOT block: its output is discarded and the tool call proceeds. So the
# outer timeout must outlast the bridge, or the harness cancels a guard before its deny lands.
# Budget: cc_hook_bridge runs every descriptor for the point sequentially, each killed at its
# own timeout_ms; the largest shipped is 960s (hatch-capable guards: the hatch helper's 900s
# tg-ctl wait + 30s process margin, and `critical` hatches are bounded by it too). 1200 =
# one full 960s descriptor + 240s for the remaining hooks, which return in milliseconds on
# the normal path. Known limit: two hatch waits requested in ONE tool call (~1860s) exceed it.
# A HIGHER operator-set value (int or float) is kept; only a missing, non-numeric, non-finite
# (NaN/inf), or lower one is raised to this floor.
HOOK_BRIDGE_TIMEOUT_S = 1200

_CODEX_BRIDGE_BEGIN = "# >>> rig managed: codex hook bridge"

_CODEX_BRIDGE_END = "# <<< rig managed: codex hook bridge"

_CODEX_HOOK_EVENTS = ("PreToolUse", "PostToolUse", "Stop")

_OPENCODE_PLUGIN_NAME = OPENCODE_HOOK_BRIDGE_PLUGIN_NAME

_LEGACY_OPENCODE_PLUGIN_NAME = "agent-tools-hook-bridge.js"

_OMP_EXTENSION_NAME = OMP_HOOK_BRIDGE_EXTENSION_NAME


def hook_bridge_entries(action: Action) -> dict[str, list[tuple[str, str]]]:
    """The (matcher, command) pairs the bridge maintains per harness hook event.

    Single source of truth shared by the install handler and drift, so both agree on what
    the harness config should contain. The command runs the dispatcher with the agent-tools
    ``lib/`` on PYTHONPATH so the bridge resolves against the same checkout whose
    ``agent-hooks/`` scripts the installed descriptors point at.

    PreToolUse is real prevention; Stop gates the turn end; PostToolUse (write tools only)
    is the feedback channel for the agent-tools ``post-write`` point. Claude Code uses its
    pipe-alternation tool matcher for writes, ``Agent|Task`` for subagent dispatch (the
    agent-tools ``pre-agent`` point), ``Skill`` for a skill invocation (the agent-tools
    ``pre-skill`` point), ``Monitor`` for a background event-stream watch (the agent-tools
    ``pre-monitor`` point — the matcher that lets ``subagent-no-monitor`` block a dispatched
    subagent from calling Monitor: only the main loop is re-invoked by a Monitor
    notification, so the subagent must use Bash ``run_in_background`` for one ordinary
    command or a foreground heartbeat loop for anything else), and ``EnterWorktree`` for
    switching into a worktree (the agent-tools
    ``pre-worktree-enter`` point — blocks entering a worktree a DIFFERENT agent created), and
    ``Workflow`` for a dynamic multi-agent script (ultracode). rig does NOT provision a bare
    ``Workflow`` permissions.allow entry itself (that stays an operator/config-layer choice,
    same as any other allow rule) — but WHEN an operator allow-lists it so ultracode is never
    prompted on spawn, that allow must not be a blanket pass for the script's contents: this
    matcher is what makes the agent-tools ``workflow-danger-gate`` (the ``pre-workflow`` point)
    actually run, denying a script whose executable JS contains a raw ``gh pr merge``,
    ``git reset --hard``, a force-push, ``sudo rm``, ``--no-verify``, ``git clean -f``, or
    ``screencapture`` while leaving comments and ultracode's own ``agent(...)`` dispatch
    untouched. Without this row registered, ``point_for_event`` maps Workflow to
    ``pre-workflow`` in agent-tools but Claude Code never calls the bridge for it, so the gate
    never fires regardless of the permissions.allow state — the same "mapped but not wired" gap
    ``pre-skill``/``pre-worktree-enter`` were in before their matcher was added (see the
    safety-property paragraph below).
    Codex uses the hook matchers exposed by its TOML contract here: ``Bash`` and
    ``apply_patch`` (no ``pre-agent``/``pre-skill``/``pre-monitor``/``pre-worktree-enter``
    mapping yet — same gap, tracked upstream in agent-tools; Codex has no Monitor-equivalent
    tool today either, and Codex/opencode have no worktree-switching tool of their own today).

    Entries are unconditional by design, not capability-probed against the bridge package at
    plan time: the plan already verifies the runnable module files exist, and drift/apply share
    this function as the desired state. This is safe against version skew in EITHER direction:
    ``cc_hook_bridge``'s ``point_for_event`` returns ``None`` for any ``(event, tool)`` pair it
    doesn't map (dispatch.py, confirmed), and its dispatcher treats that ``None`` exactly like
    "no descriptor blocked" — it emits NOTHING on stdout, so Claude Code sees no
    ``permissionDecision`` and the tool call proceeds ALLOWED, indistinguishable from no hook
    being registered at all. So a matcher registered here against an OLDER agent-tools checkout
    that predates a given point is a silent no-op, not a block, and never wedges the tool call —
    the safety property this unconditional-registration design relies on. The mirror case — an
    agent-hook installed for a point with no matcher registered yet — is likewise inert rather
    than broken: the descriptor simply never fires
    until this function is updated, exactly the state ``pre-skill``/``pre-worktree-enter`` were
    in before their matcher was added.

    ``action.options["extra_stop_hooks"]`` (codex only) is a list of already-rendered,
    already-quoted command strings appended as additional ``Stop`` entries after the bridge's
    own — a generic seam other tools' Codex Stop hooks fold into (see
    :func:`riglib.tg_ctl.codex_usage_hook_command` for the tg-cli#308 rationale and the one
    current caller). This function does not know or care what those commands do.
    """
    lib_dir = str(action.options["lib_dir"])
    py = str(action.options.get("python", "python3"))
    module = hook_bridge_module(action)
    kind = str(action.options.get("kind", "claude-code"))
    hooks_dir = action.options.get("hooks_dir")
    hook_dir_env = {
        "claude-code": "CC_HOOKS_DIR",
        "codex": "CODEX_HOOKS_DIR",
    }.get(kind)

    def cmd(event: str) -> str:
        # quote BOTH the lib path and the interpreter: a path with spaces would break the
        # hook, and an unquoted config-supplied `python` would let shell syntax be injected
        # into every hook command. `-m <module>` keeps the run command and the
        # presence-marker in lockstep (rename the module → both change together).
        env = [f"PYTHONPATH={shlex.quote(lib_dir)}"]
        if hooks_dir and hook_dir_env:
            env.append(f"{hook_dir_env}={shlex.quote(str(hooks_dir))}")
        return f"{' '.join(env)} {shlex.quote(py)} -m {module} {event}"

    if kind == "codex":
        extra_stop_hooks = action.options.get("extra_stop_hooks") or []
        stop_entries = [("", cmd("Stop"))] + [("", extra) for extra in extra_stop_hooks]
        return {
            "PreToolUse": [
                ("Bash", cmd("PreToolUse")),
                ("apply_patch", cmd("PreToolUse")),
            ],
            "PostToolUse": [
                ("apply_patch", cmd("PostToolUse")),
            ],
            "Stop": stop_entries,
        }

    return {
        "PreToolUse": [
            ("Bash", cmd("PreToolUse")),
            ("Edit|Write|MultiEdit|NotebookEdit", cmd("PreToolUse")),
            ("Agent|Task", cmd("PreToolUse")),
            ("Skill", cmd("PreToolUse")),
            ("Monitor", cmd("PreToolUse")),
            ("EnterWorktree", cmd("PreToolUse")),
            ("Workflow", cmd("PreToolUse")),
        ],
        "PostToolUse": [
            ("Edit|Write|MultiEdit|NotebookEdit", cmd("PostToolUse")),
        ],
        "Stop": [("", cmd("Stop"))],
    }


def hook_bridge_module(action: Action) -> str:
    """The bridge Python module this action registers."""
    return str(action.options.get("module") or _BRIDGE_MARKER)


def hook_bridge_format(action: Action) -> str:
    """The harness config format this action mutates."""
    return str(action.options.get("format") or "json")


def hook_bridge_settings_file(action: Action) -> Path:
    """The settings/config file a ``register_hook_bridge`` action targets."""
    target = action.target
    if target.suffix:
        return target
    fmt = hook_bridge_format(action)
    if fmt == "toml":
        return target / "config.toml"
    if fmt == "opencode-plugin":
        return target / _OPENCODE_PLUGIN_NAME
    if fmt == "omp-extension":
        return target / _OMP_EXTENSION_NAME
    return target / "settings.json"


def opencode_hook_bridge_plugin_target(action: Action) -> tuple[Path, Path]:
    """Return the opencode plugin symlink path and the bridge plugin it should target."""
    plugin_path = hook_bridge_settings_file(action)
    module = hook_bridge_module(action)
    dest = Path(str(action.options["lib_dir"])) / module / "plugin.js"
    return plugin_path, dest


def find_managed_bridge_hook(blocks, matcher: str, marker: str = _BRIDGE_MARKER) -> dict | None:
    """Return OUR managed hook dict for ``matcher`` in an event's block list, else None.

    Single source of truth for "where is the bridge entry" — shared by apply
    (upsert) and drift (compare shape + command), so both agree on what counts as the
    managed hook and never diverge. This identifies a hook by the bridge marker in its
    command; callers validate the full in-sync shape separately.
    """
    if not isinstance(blocks, list):
        return None
    for block in blocks:
        if not isinstance(block, dict) or str(block.get("matcher", "")) != matcher:
            continue
        for hk in block.get("hooks", []) or []:
            if isinstance(hk, dict) and marker in str(hk.get("command", "")):
                return hk
    return None


def _bridge_timeout_problem(hook: dict) -> str | None:
    """Why a managed hook's outer ``timeout`` is below the floor, or ``None`` when it is fine."""
    value = hook.get("timeout")
    if value is None:
        return f"outer timeout is missing (need >= {HOOK_BRIDGE_TIMEOUT_S}s)"
    # bool is an int subclass (`true` would read as 1s); NaN compares False against the floor
    # and would pass as "fine"; inf is an unbounded wait. JSON floats like 3600.0 are valid.
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return f"outer timeout is not a finite number of seconds (need >= {HOOK_BRIDGE_TIMEOUT_S}s)"
    if value < HOOK_BRIDGE_TIMEOUT_S:
        return f"outer timeout {value}s is below the {HOOK_BRIDGE_TIMEOUT_S}s floor"
    return None


def managed_bridge_hook_drift_reason(hook: dict, command: str) -> str | None:
    """Why a managed bridge hook differs from what apply writes, or ``None`` when in sync.

    Shared by apply (:func:`_upsert_bridge`) and drift so both judge the same shape: the hook
    type, the exact command, and an outer timeout at or above :data:`HOOK_BRIDGE_TIMEOUT_S`.
    """
    if hook.get("type") != "command" or str(hook.get("command", "")) != command:
        return "type/command is stale (apply will rewrite)"
    return _bridge_timeout_problem(hook)


def managed_bridge_hook_in_sync(hook: dict, command: str) -> bool:
    """True when a managed bridge hook exactly matches the shape apply writes."""
    return managed_bridge_hook_drift_reason(hook, command) is None


def _claude_bridge_block(matcher: str, command: str) -> dict:
    """A claude-code settings.json bridge block: :func:`_bridge_block` plus the outer timeout.

    Kept separate from ``_bridge_block``, which the Codex TOML renderer shares — Codex hooks
    have no such field (and ``_toml_inline`` does not render ints)."""
    block = _bridge_block(matcher, command)
    block["hooks"][0]["timeout"] = HOOK_BRIDGE_TIMEOUT_S
    return block


def _bridge_block(matcher: str, command: str) -> dict:
    """One hook block for an (matcher, command) pair (shared by the JSON and Codex TOML paths)."""
    block: dict = {"hooks": [{"type": "command", "command": command}]}
    if matcher:
        # an empty matcher (Stop) is omitted entirely → CC treats it as "match all".
        block = {"matcher": matcher, "hooks": block["hooks"]}
    return block


def _upsert_bridge(blocks: list, matcher: str, command: str, on_conflict: str, marker: str = _BRIDGE_MARKER) -> str:
    """Insert/refresh OUR managed block for (matcher, command). Returns the outcome.

    - already present with the same type+command and an outer timeout at/above the floor →
      ``"noop"`` (idempotent).
    - present with a different type/command or a missing/short timeout → rewrite in place →
      ``"changed"``, UNLESS ``on_conflict=skip`` → leave it, return ``"skipped-stale"``. The
      timeout is only ever RAISED to the floor: a higher operator value survives a rewrite.
    - absent → append a fresh managed block → ``"changed"``.
    """
    hk = find_managed_bridge_hook(blocks, matcher, marker)
    if hk is not None:
        if managed_bridge_hook_in_sync(hk, command):
            return "noop"
        if on_conflict == "skip":
            return "skipped-stale"
        hk["type"] = "command"
        hk["command"] = command
        if _bridge_timeout_problem(hk) is not None:
            hk["timeout"] = HOOK_BRIDGE_TIMEOUT_S
        return "changed"
    blocks.append(_claude_bridge_block(matcher, command))
    return "changed"
