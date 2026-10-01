"""What the harness wrapper must know about each supported harness — pure, stdlib-only.

``rig harness run|exec|host`` treats omp, Claude Code, Codex and opencode alike except for three
things, all here:

* **Which invocations are an interactive session** (:func:`is_interactive`). Only those are
  hosted in abduco and registered with tg-ctl; ``-p``/``--print``, ``exec``, ``mcp``, ``--help``
  … pass straight through to the real binary.
* **How a dead session is relaunched onto the same conversation** (:func:`relaunch_argv`):
  ``omp --resume <jsonl>``, ``claude --resume <id>``, ``codex resume <id>``,
  ``opencode --session <id>``. The original command line keeps its configuration flags and loses
  the one-shot session choice and the initial prompt (already in the resumed transcript).
* **How a fresh launch learns its session identity** (:func:`prepare_launch`): Claude Code is
  started with a ``--session-id`` the wrapper chose, so the conversation is named before it can
  crash. The others are located after the fact by :mod:`riglib.harness_sessions`.

The argv tables below are read off each CLI's ``--help`` (claude 2.1.286, codex 0.159.2, opencode
1.18.30, omp). A flag that is NOT in a table is treated as "takes the next non-dash token as its
value" — the same rule oh-my-pi's own ``/restart`` applies — because dropping a real value would
break the relaunch while replaying a stray prompt only repeats it.
"""

from __future__ import annotations

import shlex
import uuid
from dataclasses import dataclass

KINDS = ("omp", "claude", "codex", "opencode")


@dataclass(frozen=True)
class SessionRef:
    """Identity of the conversation a harness process was in."""

    id: str | None = None
    #: the transcript path, when the harness resumes by path (omp) or the transcript is known
    #: to exist on disk (Claude Code).
    file: str | None = None
    #: where the harness actually ran, when that differs from the wrapper's cwd (a worktree).
    cwd: str | None = None
    #: the harness never persisted anything: relaunch without resuming.
    fresh: bool = False

    def to_json(self) -> dict:
        return {k: v for k, v in (("id", self.id), ("file", self.file), ("cwd", self.cwd)) if v}


@dataclass(frozen=True)
class Flags:
    """One CLI's option grammar, only as far as session rewriting needs it."""

    #: options taking exactly one value.
    value: frozenset[str] = frozenset()
    #: options whose value is optional (taken only when the next token is not another option).
    optional: frozenset[str] = frozenset()
    #: options swallowing every following non-option token.
    variadic: frozenset[str] = frozenset()
    #: options known to take nothing. ``None`` = unknown, every other option uses the heuristic.
    boolean: frozenset[str] | None = None
    #: the one-shot session choice, dropped (with its value) on relaunch.
    session: frozenset[str] = frozenset()
    #: positionals are the initial prompt/subcommand (dropped) rather than operands (kept).
    drop_positionals: bool = True

    def consumed(self, name: str, following: list[str]) -> int:
        """How many tokens after option ``name`` belong to it."""
        if name in self.variadic:
            n = 0
            while n < len(following) and not following[n].startswith("-"):
                n += 1
            return n
        if name in self.value:
            return 1 if following else 0
        if name in self.optional or self.boolean is None or name not in self.boolean:
            return 1 if following and not following[0].startswith("-") else 0
        return 0


@dataclass(frozen=True)
class _Piece:
    tokens: list[str]
    role: str  # "flag" | "positional" | "tail"
    name: str | None = None


def _pieces(args: list[str], flags: Flags) -> list[_Piece]:
    pieces: list[_Piece] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            pieces.append(_Piece(args[i:], "tail"))
            break
        if not arg.startswith("-") or arg == "-":
            pieces.append(_Piece([arg], "positional"))
            i += 1
            continue
        inline = arg.startswith("--") and "=" in arg
        name = arg.split("=", 1)[0] if arg.startswith("--") else arg
        take = 0 if inline else flags.consumed(name, args[i + 1 :])
        pieces.append(_Piece(args[i : i + 1 + take], "flag", name))
        i += 1 + take
    return pieces


def first_positional(args: list[str], flags: Flags) -> str | None:
    """The first operand that is not an option or an option's value (a subcommand or a prompt)."""
    for piece in _pieces(args, flags):
        if piece.role == "positional":
            return piece.tokens[0]
    return None


def has_flag(args: list[str], flags: Flags, *names: str) -> bool:
    """Whether any of ``names`` is used as an option (before a bare ``--``)."""
    return any(p.role == "flag" and p.name in names for p in _pieces(args, flags))


def flag_value(args: list[str], flags: Flags, *names: str) -> str | None:
    """The value of the first of ``names`` (``--n v`` or ``--n=v``), else ``None``."""
    for piece in _pieces(args, flags):
        if piece.role != "flag" or piece.name not in names:
            continue
        first = piece.tokens[0]
        if first.startswith("--") and "=" in first:
            return first.split("=", 1)[1]
        if len(piece.tokens) > 1:
            return piece.tokens[1]
    return None


def rewrite(argv: list[str], flags: Flags, append: list[str]) -> list[str]:
    """``argv`` minus its session choice (and, per ``flags``, its positionals) plus ``append``."""
    kept = [argv[0]]
    for piece in _pieces(argv[1:], flags):
        if piece.role == "tail":
            if flags.drop_positionals:
                break
            kept += piece.tokens
        elif piece.role == "positional":
            if not flags.drop_positionals:
                kept += piece.tokens
        elif piece.name not in flags.session:
            kept += piece.tokens
    return kept + append


# ── omp ──────────────────────────────────────────────────────────────────────────────────────
#: Mirrors oh-my-pi ``cli/flag-tables.ts`` ``SESSION_SOURCE_FLAGS`` — what its own ``/restart``
#: drops before appending ``--resume``. Every other omp option uses the heuristic.
_OMP = Flags(
    session=frozenset({"--resume", "-r", "--session", "--continue", "-c", "--fork", "--from-claude", "--from-codex"}),
    value=frozenset({"--resume", "-r", "--session", "--fork"}),
)

# ── Claude Code ──────────────────────────────────────────────────────────────────────────────
_CLAUDE = Flags(
    value=frozenset(
        {
            "--agent", "--agents", "--append-system-prompt", "--autocompact", "--client-data-url",
            "--debug-file", "--effort", "--environment", "--fallback-model", "--input-format",
            "--json-schema", "--max-budget-usd", "--model", "-n", "--name", "--output-format",
            "--permission-mode", "--permission-prompts", "--plugin-dir", "--plugin-url",
            "--remote-control-session-name-prefix", "--session-id", "--setting-sources",
            "--settings", "--system-prompt", "--system-prompt-snapshot",
        }
    ),  # fmt: skip
    optional=frozenset(
        {
            "--cloud", "-d", "--debug", "--from-pr", "--prompt-suggestions", "--remote-control",
            "-r", "--resume", "--teleport", "-w", "--worktree",
        }
    ),  # fmt: skip
    variadic=frozenset(
        {
            "--add-dir", "--allowedTools", "--allowed-tools", "--betas", "--disallowedTools",
            "--disallowed-tools", "--file", "--mcp-config", "--tools",
        }
    ),  # fmt: skip
    boolean=frozenset(
        {
            "--allow-dangerously-skip-permissions", "--ax-screen-reader", "--bg", "--background",
            "--bare", "--brief", "--chrome", "-c", "--continue", "--dangerously-skip-permissions",
            "--desktop", "--disable-slash-commands", "--exclude-dynamic-system-prompt-sections",
            "--fork-session", "--forward-subagent-text", "-h", "--help", "--ide",
            "--include-hook-events", "--include-partial-messages", "--no-chrome",
            "--no-session-persistence", "-p", "--print", "--replay-user-messages", "--restricted",
            "--safe-mode", "--strict-mcp-config", "--tmux", "--verbose", "-v", "--version",
        }
    ),  # fmt: skip
    session=frozenset(
        {"-r", "--resume", "-c", "--continue", "--session-id", "--fork-session", "--from-pr", "--teleport", "--cloud"}
    ),
)
_CLAUDE_SUBCOMMANDS = frozenset(
    {
        "agents", "attach", "auth", "auto-mode", "doctor", "gateway", "import", "install", "logs",
        "mcp", "plugin", "plugins", "project", "respawn", "rm", "setup-token", "stop", "kill",
        "ultrareview", "update", "upgrade",
    }
)  # fmt: skip
#: a launch that already names (or asks for) its conversation, or is not a local session at all.
_CLAUDE_OWN_SESSION = ("-r", "--resume", "-c", "--continue", "--session-id", "--from-pr", "--teleport", "--cloud")

# ── Codex ────────────────────────────────────────────────────────────────────────────────────
_CODEX = Flags(
    value=frozenset(
        {
            "-c", "--config", "--enable", "--disable", "--remote", "--remote-auth-token-env",
            "-m", "--model", "--local-provider", "-p", "--profile", "-s", "--sandbox", "-C",
            "--cd", "--add-dir", "-a", "--ask-for-approval",
        }
    ),  # fmt: skip
    variadic=frozenset({"-i", "--image"}),
    boolean=frozenset(
        {
            "--strict-config", "--oss", "--approve-for-me", "--dangerously-bypass-approvals-and-sandbox",
            "--dangerously-bypass-hook-trust", "--worktree", "--search", "--no-alt-screen",
            "--no-daemon", "-h", "--help", "-V", "--version", "--last", "--all",
            "--include-non-interactive",
        }
    ),  # fmt: skip
    session=frozenset({"--last", "--all", "--include-non-interactive"}),
)
_CODEX_SUBCOMMANDS = frozenset(
    {
        "agents", "exec", "e", "review", "login", "logout", "mcp", "plugin", "app-server",
        "remote-control", "app", "completion", "update", "doctor", "sandbox", "debug", "apply", "a",
        "queue", "archive", "delete", "migrate-rollouts", "unarchive", "cloud", "exec-server",
        "features", "help",
    }
)  # fmt: skip
#: ``codex resume``/``codex fork`` open an interactive TUI; every other subcommand does not.
_CODEX_TUI_SUBCOMMANDS = frozenset({"resume", "fork"})

# ── opencode ─────────────────────────────────────────────────────────────────────────────────
_OPENCODE = Flags(
    value=frozenset(
        {
            "--log-level", "--port", "--hostname", "--mdns-domain", "-m", "--model", "-s",
            "--session", "--prompt", "--agent", "--replay-limit",
        }
    ),  # fmt: skip
    variadic=frozenset({"--cors"}),
    boolean=frozenset(
        {
            "-h", "--help", "-v", "--version", "--print-logs", "--pure", "--mdns", "-c", "--continue",
            "--fork", "--auto", "--mini", "--no-replay",
        }
    ),  # fmt: skip
    session=frozenset({"-s", "--session", "-c", "--continue", "--fork", "--prompt"}),
    drop_positionals=False,  # `opencode [project]`: the operand is the project directory
)
_OPENCODE_SUBCOMMANDS = frozenset(
    {
        "completion", "acp", "mcp", "attach", "run", "debug", "providers", "auth", "agent", "upgrade",
        "uninstall", "serve", "web", "models", "stats", "export", "import", "github", "session",
        "plugin", "plug", "db",
    }
)  # fmt: skip

_TABLES = {"omp": _OMP, "claude": _CLAUDE, "codex": _CODEX, "opencode": _OPENCODE}

_INFO_FLAGS = ("-h", "--help", "-v", "-V", "--version")


def validate_kind(kind: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown harness {kind!r} (expected one of: {', '.join(KINDS)})")
    return kind


def is_interactive(kind: str, args: list[str]) -> bool:
    """Whether ``<kind> *args`` starts an interactive session (what gets hosted + registered).

    ``args`` excludes the executable. Everything headless or administrative — print mode,
    subcommands, help/version — is not."""
    validate_kind(kind)
    flags = _TABLES[kind]
    if kind == "omp":
        from .omp_recovery import is_interactive_omp

        return is_interactive_omp(shlex.join(["omp", *args]))
    if has_flag(args, flags, *_INFO_FLAGS):
        return False
    if kind == "claude":
        if has_flag(args, flags, "-p", "--print", "--bg", "--background"):
            return False
        return first_positional(args, flags) not in _CLAUDE_SUBCOMMANDS
    if kind == "codex":
        first = first_positional(args, flags)
        return first is None or first in _CODEX_TUI_SUBCOMMANDS or first not in _CODEX_SUBCOMMANDS
    first = first_positional(args, flags)
    return first is None or first == "pr" or first not in _OPENCODE_SUBCOMMANDS


# ── launching and relaunching ────────────────────────────────────────────────────────────────
def prepare_launch(kind: str, argv: list[str], *, new_id: str | None = None) -> tuple[list[str], SessionRef | None]:
    """The command to start for a fresh hosted session, and the conversation it will be in if
    that is already known. Only Claude Code lets the wrapper choose the id up front."""
    validate_kind(kind)
    if kind != "claude":
        return list(argv), None
    flags = _CLAUDE
    given = flag_value(argv[1:], flags, "--resume", "-r", "--session-id")
    if given:
        return list(argv), SessionRef(id=given)
    if has_flag(argv[1:], flags, *_CLAUDE_OWN_SESSION):
        return list(argv), None
    session_id = new_id or str(uuid.uuid4())
    return [*argv, "--session-id", session_id], SessionRef(id=session_id)


def relaunch_argv(kind: str, previous: list[str], ref: SessionRef | None) -> list[str]:
    """The command that continues ``ref`` after ``previous`` died. Without a usable ``ref`` the
    previous command is reused as is (it already names whatever it was resuming)."""
    validate_kind(kind)
    if ref is None:
        return list(previous)
    flags = _TABLES[kind]
    if kind == "omp":
        return rewrite(previous, flags, ["--resume", ref.file] if ref.file and not ref.fresh else [])
    if ref.fresh or not ref.id:
        return rewrite(previous, flags, [])
    if kind == "claude":
        # `--resume` needs the transcript on disk; a conversation that died before its first
        # message is started again under the same id.
        return rewrite(previous, flags, ["--resume" if ref.file else "--session-id", ref.id])
    if kind == "codex":
        return rewrite(previous, flags, ["resume", ref.id])
    return rewrite(previous, flags, ["--session", ref.id])
