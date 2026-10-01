"""Pure, fail-closed recovery decisions; IO and installation are deliberately separate.

No function here signals a PID, invokes a shell, mutates a checkout or rewrites
an OMP transcript. Executors must recheck identity immediately before acting.
"""

from dataclasses import dataclass
from pathlib import PurePosixPath
import shlex
from typing import Literal, Sequence


@dataclass(frozen=True)
class OriginalSession:
    pid: int
    instance_id: str
    cwd: str
    session_id: str | None
    durable_session: str | None
    session_file: str | None = None


@dataclass(frozen=True)
class SavedPane:
    pane: str
    cwd: str
    session_id: str
    session_file: str
    durable_session: str | None = None


@dataclass(frozen=True)
class RestorePlan:
    action: Literal["attach", "resume", "skip", "blocked"]
    reason: str
    argv: tuple[str, ...] = ()
    owner_pid: int | None = None


@dataclass(frozen=True)
class ServerIdentity:
    pid: int
    uid: int
    executable: str
    started_at: str
    socket_path: str


@dataclass(frozen=True)
class ServerPlan:
    action: Literal["none", "blocked", "restore_socket", "start"]
    reason: str
    pid: int | None = None


# A bare command is an interactive launch, not every command of the omp CLI.
_COMMANDS = frozenset(
    {
        "acp",
        "agents",
        "auth-broker",
        "collab",
        "compress",
        "config",
        "dry-balance",
        "gallery",
        "gc",
        "images",
        "install",
        "join",
        "models",
        "plugin",
        "ps",
        "read",
        "render",
        "say",
        "search",
        "setup",
        "share",
        "shell",
        "ssh",
        "stats",
        "tiny-models",
        "token",
        "ttsr",
        "help",
        "version",
    }
)

# Match the package entrypoint, independent of the containing checkout's name.
_SOURCE_ENTRYPOINTS = (
    "/packages/coding-agent/src/cli.ts",
    "/packages/coding-agent/scripts/../src/cli.ts",
)


def is_interactive_omp(command: str) -> bool:
    """Recognize actual compiled/source OMP launchers, never arbitrary Bun/CLI text."""
    try:
        args = shlex.split(command)
    except ValueError:
        return False
    if not args:
        return False
    base = PurePosixPath(args[0]).name
    if base == "omp":
        tail = args[1:]
    elif base in {"bun", "node"}:
        # The exact coding-agent source entrypoint matters: a generic cli.ts is unrelated.
        indices = [
            i for i, a in enumerate(args[1:], 1) if a.endswith(_SOURCE_ENTRYPOINTS)
        ]
        if len(indices) != 1:
            return False
        tail = args[indices[0] + 1 :]
    else:
        return False
    if any(a.startswith("__omp_worker_") for a in tail):
        return False
    if tail and tail[0] in _COMMANDS:
        return False
    for i, arg in enumerate(tail):
        if arg in {"-p", "--print", "--help", "-h", "--version", "-V"}:
            return False
        if arg.startswith("--mode=") and arg.split("=", 1)[1] != "text":
            return False
        if arg == "--mode" and (i + 1 == len(tail) or tail[i + 1] != "text"):
            return False
    return True


def plan_restore(
    saved: SavedPane,
    live: Sequence[OriginalSession],
    durable_sockets: set[str],
    *,
    inspection_ok: bool,
    session_file_exists: bool,
    pane_is_empty_shell: bool,
) -> RestorePlan:
    """Select exact-session attach/resume; never select the newest file for a cwd."""
    if not pane_is_empty_shell:
        return RestorePlan("skip", "pane_is_busy")
    if not inspection_ok:
        return RestorePlan("blocked", "inspection_unavailable")
    if not saved.session_id or not PurePosixPath(saved.session_file).is_absolute():
        return RestorePlan("blocked", "invalid_saved_identity")
    exact = [s for s in live if s.session_id == saved.session_id]
    if len(exact) > 1:
        return RestorePlan("blocked", "multiple_live_owners")
    if exact:
        original = exact[0]
        if original.cwd != saved.cwd:
            return RestorePlan(
                "blocked", "session_cwd_mismatch", owner_pid=original.pid
            )
        durable = original.durable_session
        if durable and durable in durable_sockets:
            return RestorePlan(
                "attach",
                "original_still_alive",
                ("rig", "harness", "attach", durable),
                original.pid,
            )
        return RestorePlan(
            "blocked", "live_original_not_attachable", owner_pid=original.pid
        )
    # An unknown process in this workspace may be precisely the saved session.
    unknown = [s for s in live if s.cwd == saved.cwd and not s.session_id]
    if unknown:
        return RestorePlan(
            "blocked",
            "unidentified_live_owner",
            owner_pid=unknown[0].pid if len(unknown) == 1 else None,
        )
    if saved.durable_session and saved.durable_session in durable_sockets:
        if any(
            s.durable_session == saved.durable_session
            and s.session_id != saved.session_id
            for s in live
        ):
            return RestorePlan("blocked", "durable_session_identity_mismatch")
        # Existing durable terminal is safe to attach without launching a new worker.
        return RestorePlan(
            "attach",
            "durable_terminal_still_alive",
            ("rig", "harness", "attach", saved.durable_session),
        )
    if not session_file_exists:
        return RestorePlan("blocked", "saved_session_missing")
    return RestorePlan(
        "resume",
        "original_not_running",
        ("rig", "harness", "run", "omp", "--resume", saved.session_file),
    )


def plan_server_recovery(
    saved: ServerIdentity | None,
    current: ServerIdentity | None,
    *,
    socket_responding: bool,
    inspection_ok: bool,
    paused: bool,
    cooldown_active: bool,
) -> ServerPlan:
    """Missing socket != dead server. Recreate a verified live server's socket first."""
    if paused:
        return ServerPlan("none", "recovery_paused")
    if socket_responding:
        return ServerPlan("none", "server_healthy")
    if cooldown_active:
        return ServerPlan("none", "cooldown")
    if not inspection_ok:
        return ServerPlan("blocked", "inspection_unavailable")
    if current is not None:
        if saved is not None and current == saved:
            return ServerPlan(
                "restore_socket", "verified_live_server_socket_missing", current.pid
            )
        return ServerPlan("blocked", "server_identity_changed")
    return ServerPlan("start", "server_confirmed_absent")
