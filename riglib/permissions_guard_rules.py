"""Command-granular guard rules (omp guard) — rule shape, validation and the baked deny/ask belts.

Split out of :mod:`riglib.permissions`, which re-exports every public name here so callers keep
importing from ``riglib.permissions``. The generated omp guard (:mod:`riglib.omp_guard`) renders
:data:`OMP_GUARD_DENY_RULES` / :data:`OMP_GUARD_ASK_RULES` into its TS matcher.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

# ── omp guard rules — the command-granular deny/ask baseline as STRUCTURED data ─────────────
# The single source the omp TS guard extension is GENERATED from (riglib.omp_guard renders it;
# nothing is hand-copied). Each rule carries the same INTENT as the claude-code/opencode belts
# but matches at the ARGV level (the TS hook tokenizes the full command), which is strictly more
# precise than prefix globs: ``git push --force`` matches the exact ``--force`` token anywhere in
# the stage's argv, so ``--force-with-lease`` (the safe force) never false-positives, and
# ``git commit --no-verify`` is caught in ANY flag position — the case the claude glob belt had
# to leave hook-only.
#
# Matcher families (any-of ``flags`` semantics: at least one exact token present):
#   ``argv_prefix``      — a pipeline stage whose LEADING tokens equal ``tokens`` (after
#                          stripping leading VAR=val env assignments)
#   ``subcommand_flags`` — a stage LED by ``tokens`` (``tokens[0]`` alone for a flat command
#                          like ``rg``, or ``tokens[0] tokens[1]`` for a subcommand shape like
#                          ``git push``) carrying at least one of ``flags`` as an exact argv
#                          token anywhere. ``flags_with_value`` (a SUBSET of ``flags``, added
#                          for rig-cli#187) ALSO matches those specific flags as a joined
#                          ``flag=value`` single token (e.g. ``rg --pre=cat``) via an exact
#                          ``flag + "="`` prefix check. Deliberately PER-FLAG, not family-wide:
#                          ``git-commit-no-verify`` matches ``--no-verify`` as an exact token
#                          ANYWHERE (by design, so a commit MESSAGE mentioning the flag doesn't
#                          false-positive) — a family-wide ``=`` check would newly deny a
#                          message token that merely STARTS WITH ``--no-verify=`` (e.g.
#                          ``git commit -m "--no-verify=disable it"``), silently widening that
#                          rule's false-positive surface. Only flags that actually take an
#                          ``=``-joinable value (``rg --pre``) opt in via ``flags_with_value``.
# ``hint`` is the block/confirm reason: static text, NEVER command contents (a command can
# carry secrets; reasons must not). KNOWN GAPS (documented, same class as the glob belts):
# (1) wrapper indirection (a command run as the first word of a ``$(…)`` substitution, copied
# binaries, aliases, ``xargs`` for non-kill rules, a ``sh -c "$CMD"`` whose string is a
# variable, a script FILE or another program's output piped into a shell) hides the command
# from argv matching — the guard covers model-issued tool calls in trusted repositories; it is
# not a sandbox. Literal ``sh -c '…'`` / ``bash -lc '…'`` / ``eval '…'`` / ``env -S '…'`` /
# ``find -exec … ;`` / ``watch '…'`` strings and ``echo|printf … | sh`` / ``sh <<< '…'`` ARE
# re-parsed as commands (rig-cli#701).
# (2) ANSI-C quoting (``rg $'--pre' cmd``) tokenizes to the LITERAL string
# ``$--pre``, not ``--pre`` — ordinary single/double quotes ARE caught (the shell strips them
# before argv sees the token), but this one quoting FORM is not; this is a pre-existing
# tokenizer limitation (equally true of every other ``subcommand_flags`` rule today, e.g.
# ``git push $'--force'``), not specific to ``rg-pre``. (3) specific to ``rg-pre``
# (rig-cli#187): ``RIPGREP_CONFIG_PATH=cfg rg pattern`` as ONE command IS visible in argv as a
# leading env assignment, but ``argv_prefix``/``subcommand_flags`` both match against the
# ``stripEnv``'d stage (env assignments are stripped as noise before matching), so this rule
# does not special-case it back in; a config file supplying ``--pre`` via an ALREADY-exported
# env var (set in a shell profile before the agent's command ever runs) is a true blind spot no
# argv check can see. Neither is fixed here — left as documented follow-up tickets, not silent
# gaps (see the PR description for #187).

# Signal-safety matchers (rig-cli#701). 2026-10-15 an agent, pushed off ``pkill`` by the ask
# rule, ran ``kill -TERM $(pstree -p $P …)`` with an empty ``$P``: ``pstree -p`` printed the
# whole system tree and SIGTERM reached ``systemd --user``, ending every omp session and the
# desktop. These four families judge WHAT a kill-class command signals, which no prefix/flag
# shape can express: the generated TS parses kill/pkill/killall operands and selectors, the
# systemctl/loginctl/tmux verbs, the ``$(…)``/``xargs`` sources feeding a kill, and resolves a
# literal PID against /proc (comm, session, cgroup, the guard process's own ancestry). A rule
# only NAMES its matcher (no tokens/flags); the value here is the shape prose the advisory
# instruction policy prints for it.
#   ``kill_pstree_fed``   — kill fed by pstree output (substitution, ``| xargs kill``, a variable
#                           assigned from pstree)
#   ``kill_broadcast``    — kill -1 / 0 / negative PIDs (process groups), pkill -v, user- or
#                           session-wide pkill/killall with no pattern
#   ``signal_protected``  — any signal/stop aimed at session infrastructure: systemd (--user),
#                           the logind session leader, gnome-shell/gnome-session/Xwayland, the
#                           tmux server, an omp session, slices and user@/session units
#   ``kill_computed_pid`` — kill whose target is computed at runtime: ``$(…)``, backticks,
#                           ``xargs kill``, a glob, or a ``$VAR`` with no visible non-empty check
SIGNAL_MATCHERS: dict[str, str] = {
    "kill_pstree_fed": "kill $(pstree …) · pstree … | xargs kill",
    "kill_broadcast": "kill -1|0|-PGID · pkill -v · pkill/killall -u USER with no pattern",
    "signal_protected": (
        "kill/pkill/killall/systemctl/loginctl/tmux kill-server aimed at systemd --user, "
        "session leaders, slices, gnome-shell/gnome-session/Xwayland, the tmux server or omp"
    ),
    "kill_computed_pid": "kill $(…)|`…`|unchecked $VAR · … | xargs kill",
}

# Runpod spend matchers (rig-cli#714). Agents rent Runpod compute only through ``runpod-run`` (its
# budget, pod cap and TTL); these families catch the paths around it that no argv prefix can:
#   ``http_write_url`` — a curl/wget/http/xh stage whose effective method is POST, PUT or PATCH (or
#                        an explicit method the guard cannot resolve) to a URL whose host is one of
#                        ``tokens`` or a subdomain of one, and whose decoded, normalized path
#                        matches one of the ``value_prefixes`` REGEXES. Non-empty ``flags`` are
#                        request-body NEEDLES (GraphQL operation names): the write must then carry
#                        one of them, or a body unreadable before it runs (``@file``, stdin, a
#                        shell expansion) — and a needle in the URL itself also matches. DELETE is
#                        never a match: deleting only ever stops spend.
#   ``mcp_tool``       — an MCP device call: omp routes MCP tools as a ``write`` to
#                        ``xd://mcp__<server>_<tool>``; ``tokens`` are those device names in their
#                        normalized form (lower case, ``_`` never ``-``). Non-empty ``flags`` are
#                        CONTENT keys: the call matches only when its JSON content sets one of them
#                        above zero at any depth, or cannot be parsed.
#   ``write_path``     — a write/edit/apply_patch target or a bash write (``>``/``>>``, tee, cp, mv,
#                        install, ln, rm, chmod, truncate, ``dd of=``, ``sed -i``, ``perl -i``) on a
#                        path matching one of the ``tokens`` globs (``~/``-anchored or absolute;
#                        ``*`` stays in one segment, a trailing ``/**`` also covers the directory).
#                        Reads are never matched.
# ``code_pattern`` gains one opt-in flag for the same incident class: ``flags=("word",)`` matches
# the needle as a whole word ANYWHERE in the eval source — a GraphQL operation name lives inside
# query text, never as the quoted command name the default code_pattern shape looks for.

# The tool_call surfaces each matcher judges; a matcher absent here judges bash argv only. A
# rule's ``scope`` must equal its matcher's entry, so the generated TS can dispatch on it. One
# addition the table cannot name: an mcp_tool rule also judges a tool whose NAME is the device
# (``mcp__runpod_create_pod``), for a harness that exposes MCP tools directly.
MATCHER_SCOPES: dict[str, tuple[str, ...]] = {
    "code_pattern": ("eval",),
    "mcp_tool": ("write",),
    "write_path": ("bash", "write", "edit", "apply_patch"),
}
_CODE_PATTERN_FLAGS = frozenset({"word"})


@dataclass(frozen=True)
class GuardRule:
    """One command-granular guard rule — harness-agnostic INTENT, rendered per harness."""

    id: str
    hint: str
    # "argv_prefix" | "subcommand_flags" | "flag_value_prefix" | "code_pattern" | "mcp_tool" |
    # "write_path" | "http_write_url", or a signal matcher: "kill_pstree_fed" | "kill_broadcast" |
    # "signal_protected" | "kill_computed_pid"
    matcher: str
    tokens: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    value_prefixes: tuple[str, ...] = ()
    flags_with_value: tuple[str, ...] = ()  # subset of `flags` that ALSO matches `flag=value`
    # The tool_call surfaces the rule judges — always its matcher's MATCHER_SCOPES entry
    # (("bash",) unless listed there): ("eval",) scans the eval cell's SOURCE TEXT, ("write",)
    # the MCP device a write targets, write_path every file-writing surface.
    scope: tuple[str, ...] = ("bash",)

    def __post_init__(self) -> None:
        # shape invariants — the generated TS matcher assumes them, and a silent mis-match
        # in a security guard is worse than a loud construction error.
        validator = _SHAPE_VALIDATORS.get(self.matcher)
        if validator is None and self.matcher in SIGNAL_MATCHERS:
            validator = _shape_signal
        if validator is None:
            raise ValueError(f"guard rule {self.id!r}: unknown matcher {self.matcher!r}")
        problem = validator(self)
        if problem is not None:
            raise ValueError(f"guard rule {self.id!r}: {problem}")
        expected_scope = MATCHER_SCOPES.get(self.matcher, ("bash",))
        if self.scope != expected_scope:
            raise ValueError(
                f"guard rule {self.id!r}: a {self.matcher} rule must scope to {expected_scope!r} "
                "(MATCHER_SCOPES) — the generated guard dispatches on it"
            )


# Per-matcher shape checks: each returns the problem text (the ValueError message after the
# ``guard rule 'id': `` prefix) or ``None`` when the rule is well-formed.


def _shape_argv_prefix(rule: GuardRule) -> str | None:
    if not rule.tokens or rule.flags or rule.value_prefixes or rule.flags_with_value:
        return "argv_prefix needs tokens only"
    return None


def _shape_subcommand_flags(rule: GuardRule) -> str | None:
    if len(rule.tokens) not in (1, 2) or not rule.flags or rule.value_prefixes:
        return "subcommand_flags needs 1 or 2 tokens + flags"
    if not set(rule.flags_with_value) <= set(rule.flags):
        return "flags_with_value must be a subset of flags"
    return None


def _shape_flag_value_prefix(rule: GuardRule) -> str | None:
    # tokens[0] is the program; any of ``flags`` whose FOLLOWING token starts with
    # one of ``value_prefixes`` matches (git -c core.hooksPath=/dev/null …).
    if len(rule.tokens) != 1 or not rule.flags or not rule.value_prefixes or rule.flags_with_value:
        return (
            "flag_value_prefix needs 1 token + flags + "
            "value_prefixes (no flags_with_value — that's a subcommand_flags concept)"
        )
    return None


def _shape_signal(rule: GuardRule) -> str | None:
    if rule.tokens or rule.flags or rule.value_prefixes or rule.flags_with_value:
        return (
            f"signal matcher {rule.matcher!r} takes no "
            "tokens/flags — its argv logic lives in the generated TS"
        )
    return None


def _shape_code_pattern(rule: GuardRule) -> str | None:
    if (
        len(rule.tokens) != 1
        or not set(rule.flags) <= _CODE_PATTERN_FLAGS
        or rule.value_prefixes
        or rule.flags_with_value
    ):
        return (
            "code_pattern needs exactly one token (the needle), "
            "optionally flags=('word',), and nothing else"
        )
    return None


def _is_normalized_mcp_device(token: str) -> bool:
    return token.startswith("mcp__") and token == token.lower() and "-" not in token


def _shape_mcp_tool(rule: GuardRule) -> str | None:
    if (
        not rule.tokens
        or not all(_is_normalized_mcp_device(t) for t in rule.tokens)
        or rule.value_prefixes
        or rule.flags_with_value
    ):
        return (
            "mcp_tool needs normalized device names "
            "(mcp__<server>_<tool>, lower case, '_' not '-') + optional content-key flags"
        )
    return None


def _shape_write_path(rule: GuardRule) -> str | None:
    if (
        not rule.tokens
        or any(not t.startswith(("~/", "/")) for t in rule.tokens)
        or rule.flags
        or rule.value_prefixes
        or rule.flags_with_value
    ):
        return (
            "write_path needs ~/-anchored or absolute path "
            "globs as tokens and nothing else"
        )
    return None


def _is_lower_host(token: str) -> bool:
    return bool(token) and token == token.lower() and "/" not in token and ":" not in token


def _shape_http_write_url(rule: GuardRule) -> str | None:
    if (
        not rule.tokens
        or not all(_is_lower_host(t) for t in rule.tokens)
        or not rule.value_prefixes
        or rule.flags_with_value
    ):
        return (
            "http_write_url needs lower-case host tokens + "
            "path-regex value_prefixes (+ optional body-needle flags)"
        )
    for pattern in rule.value_prefixes:
        try:
            re.compile(pattern)
        except re.error as exc:
            return f"value_prefixes entry {pattern!r} is not a regex: {exc}"
    return None


_SHAPE_VALIDATORS: dict[str, Callable[[GuardRule], str | None]] = {
    "argv_prefix": _shape_argv_prefix,
    "subcommand_flags": _shape_subcommand_flags,
    "flag_value_prefix": _shape_flag_value_prefix,
    "code_pattern": _shape_code_pattern,
    "mcp_tool": _shape_mcp_tool,
    "write_path": _shape_write_path,
    "http_write_url": _shape_http_write_url,
}


# Shared text of every signal-safety block reason (rig-cli#701): the incident, then the safe
# alternative. The 2026-10-15 agent switched to the deadlier idiom because the pkill ask told it
# what NOT to do but never what to do instead — every kill-class reason now names the way out.
_SIGNAL_INCIDENT = (
    "2026-10-15 an empty $P made `kill -TERM $(pstree -p $P …)` SIGTERM systemd --user and end "
    "every omp session plus the desktop (rig-cli#701)"
)
_SIGNAL_SAFE = (
    "stop your own job by handle — proc://bg_N/kill, `kill %1`, `kill $!`, "
    "`systemctl --user stop run-<id>.scope` — or verify one PID and kill it literally: "
    '`kill <pid>`, or `[ -n "$P" ] && ps -p "$P" && kill "$P"`'
)


def _signal_hint(what: str) -> str:
    """A kill-class block reason: what is wrong, the incident, then the safe alternative."""
    return f"{what} — {_SIGNAL_INCIDENT}. Instead, {_SIGNAL_SAFE}"


# rig-cli#714: every Runpod creation path outside runpod-run shares one reason.
_RUNPOD_CREATE_HINT = (
    "use runpod-run — creating Runpod pods, endpoints or clusters directly bypasses its budget, "
    "pod cap and TTL"
)
# (rule id, argv prefix). `pods` is runpodctl's alias of `pod`, `sls` of `serverless`; `create
# pod(s)` is the deprecated top-level form, still shipped.
_RUNPOD_CREATE_ARGV: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("runpodctl-pod-create", ("runpodctl", "pod", "create")),
    ("runpodctl-pods-create", ("runpodctl", "pods", "create")),
    ("runpodctl-serverless-create", ("runpodctl", "serverless", "create")),
    ("runpodctl-sls-create", ("runpodctl", "sls", "create")),
    ("runpodctl-cluster-create", ("runpodctl", "cluster", "create")),
    ("runpodctl-create-pod", ("runpodctl", "create", "pod")),
    ("runpodctl-create-pods", ("runpodctl", "create", "pods")),
    ("flash-deploy", ("flash", "deploy")),
)
# api.runpod.io (GraphQL + v2), rest.runpod.io (v1) and any other *.runpod.io host
_RUNPOD_API_HOSTS: tuple[str, ...] = ("runpod.io",)
# GraphQL mutations that rent compute or create/scale an endpoint
_RUNPOD_GRAPHQL_CREATE: tuple[str, ...] = (
    "podFindAndDeployOnDemand", "podRentInterruptable", "createCluster", "deployCpuPod",
    "saveEndpoint",
)


OMP_GUARD_DENY_RULES: tuple[GuardRule, ...] = (
    # raw PR merges are banned machine-wide — merges go through `gh ship` (the gated delegator)
    GuardRule("gh-pr-merge", "raw PR merges are banned — use `gh ship` (the gated delegator)",
              "argv_prefix", ("gh", "pr", "merge")),
    # force-push; `--force-with-lease` (the safe force) can never match: it is a DIFFERENT exact
    # token, so any-of ("--force", "-f") stays precise with no exclusion list needed
    GuardRule("git-push-force", "force-push is banned — the safe force is --force-with-lease",
              "subcommand_flags", ("git", "push"), ("--force", "-f")),
    # hook-bypass commits — caught in ANY flag position (the gap the claude glob belt documents)
    GuardRule("git-commit-no-verify", "hook-bypass commits are banned (--no-verify skips the gates)",
              "subcommand_flags", ("git", "commit"), ("--no-verify",)),
    # config-injection evasion: `git -c core.hooksPath=/dev/null commit` bypasses the hook
    # gates WITHOUT --no-verify, and `-c alias.x=…` rewrites command semantics — the matcher
    # treats -c as transparent for matching, so this must be its OWN deny rule
    GuardRule("git-config-injection", "git -c config overrides that disable hooks or rewrite commands are banned",
              "flag_value_prefix", ("git",), ("-c", "--config"), ("core.hooksPath=", "alias.")),
    # no legitimate agent flow removes files as root
    GuardRule("sudo-rm", "removing files as root is banned",
              "argv_prefix", ("sudo", "rm")),
    # screenshots go through Playwright/CDP; `screencapture` black-frames windows on other
    # Spaces and trips macOS Screen Recording grants (the documented hard rule)
    GuardRule("screencapture", "screenshots go through Playwright/CDP, not screencapture",
              "argv_prefix", ("screencapture",)),
    # AppleScript/System Events sent from bash triggers an unapprovable macOS TCC "bash would
    # like to access data from other apps" prompt (root-caused via a personal
    # ~/.files/bin/vscmd script sending VS Code keystrokes through `tell application "System
    # Events"`). No legitimate agent flow in this ecosystem invokes osascript directly from a
    # bash tool call — native UI automation goes through the `computer` eval helpers (AX tree +
    # native input), which never touch Apple Events, and app-specific remote control should use
    # that app's own CLI/IPC (e.g. VS Code's `code --command`) instead of simulated keystrokes.
    GuardRule("osascript", "AppleScript/System Events from bash trigger unapprovable TCC prompts — use computer.* (AX/native input) or an app's own CLI instead",
              "argv_prefix", ("osascript",)),
    # rig-cli#187: `rg --pre` runs an arbitrary preprocessor command — argv matching catches it
    # in ANY position with no false positive on `--pretty` (a DIFFERENT exact token), the gap
    # the claude-code/opencode glob belts can only approximate. `--pre-glob` is deliberately
    # NOT in `flags` here either — same reasoning as the glob belts (it's a no-op without
    # `--pre`, and every dangerous combination is already caught by `--pre` alone), kept
    # consistent across ALL belts this time (an earlier draft denied bare `--pre-glob` here as
    # "defense in depth," which review correctly called out as paying the exact false-positive
    # cost the claude-code comment argues against, with a misleading block-reason message on a
    # genuinely harmless command).
    GuardRule("rg-pre", "rg --pre runs an arbitrary preprocessor command",
              "subcommand_flags", ("rg",), ("--pre",),
              flags_with_value=("--pre",)),
    # raw `git worktree add` is banned — worktrees go through `rig worktree create`
    GuardRule("git-worktree-add", "raw git worktree add is banned — use `rig worktree create`",
              "argv_prefix", ("git", "worktree", "add")),
    # the eval tool's `computer` prelude runs on a Bun child process with full child_process
    # access — an agent hitting the sandboxed bash/read tools' "Operation not permitted" on a
    # TCC-protected folder (Downloads/Documents/Photos) can route around it by spawning
    # `osascript` from INSIDE eval code (`require("child_process").execSync("osascript …")`,
    # `Bun.spawn(["osascript", …])`), driving Finder/System Events directly — the same class of
    # unapprovable TCC "control Finder" prompt as the bash-level osascript rule, just reached
    # through the eval tool's fast-path instead (the guard's tool_call handler only inspected
    # the "bash" tool's argv before this rule, so a bash-level deny alone never saw it — traced
    # live to a conloca-landing session spawning osascript from inside `computer.run` to read
    # ~/Downloads/figma-exports). No legitimate eval cell in this ecosystem needs to shell out
    # to osascript — UI automation goes through the `computer` helpers (window/ax/press, AX
    # tree + native input), never raw AppleScript.
    GuardRule("eval-osascript-spawn",
              "raw AppleScript automation from eval bypasses the sandbox and repeatedly "
              "triggers unapprovable TCC prompts — use the `computer` helpers "
              "(window/ax/press), not a spawned osascript",
              "code_pattern", ("osascript",), scope=("eval",)),
    # `automator` (/usr/bin/automator) runs a .workflow document from the command line — the
    # SAME AppleEvent/Automation TCC surface as osascript, just a different front door (Alex's
    # follow-up on rig-cli#488: "why only osascript, aren't there other paths into TCC?"). A
    # workflow can drive System Events/Finder/other apps exactly like an AppleScript can, and
    # `execSync("automator x.workflow")` / `Bun.spawn(["automator", …])` from eval bypasses the
    # sandbox the same way the pre-#488 osascript gap did. No legitimate eval cell in this
    # ecosystem needs to shell out to automator — UI automation goes through the `computer`
    # helpers, and app-specific control uses that app's own CLI/IPC.
    GuardRule("eval-automator-spawn",
              "Automator workflow automation from eval bypasses the sandbox and repeatedly "
              "triggers unapprovable TCC prompts — use the `computer` helpers "
              "(window/ax/press), not a spawned automator",
              "code_pattern", ("automator",), scope=("eval",)),
    # `screencapture` already has a bash-level argv_prefix deny (this file, "screencapture") —
    # but that rule only inspects the "bash" tool's argv, the same gap eval-osascript-spawn
    # closed for osascript. `execSync("screencapture …")` / `Bun.spawn(["screencapture", …])`
    # from eval reaches the Bun child process directly and trips the same unapprovable Screen
    # Recording TCC prompt the bash-level rule exists to prevent — the eval fast-path was never
    # covered. Screenshots go through Playwright/CDP, not a spawned screencapture.
    GuardRule("eval-screencapture-spawn",
              "screenshots from eval bypass the sandbox and repeatedly trigger unapprovable "
              "Screen Recording TCC prompts — use Playwright/CDP or the `computer` helpers, "
              "not a spawned screencapture",
              "code_pattern", ("screencapture",), scope=("eval",)),
    # direct shell reads of the Contacts (AddressBook) data directory — a SQLite store gated by
    # its own TCC "Contacts" permission, the same protected-user-data class as the
    # Downloads-folder incident that motivated eval-osascript-spawn. A raw `fs.readFileSync`
    # inside the eval Bun process already fails with "Operation not permitted" (no Full Disk
    # Access grant on that process), but shelling out via `execSync('cp -r "~/Library/Application
    # Support/AddressBook" …')` / `Bun.spawn(["cp", "-r", "~/Library/Application Support/AddressBook",
    # …])` launders the read through whatever TCC grants the spawned binary itself holds. No
    # legitimate eval cell needs to read this directory directly.
    GuardRule("eval-addressbook-read",
              "reading the Contacts (AddressBook) data directory from eval bypasses the "
              "sandbox and accesses TCC-protected contact data directly — this data is not "
              "available to the eval tool",
              "code_pattern", ("~/Library/Application Support/AddressBook",), scope=("eval",)),
    # same class as eval-addressbook-read, for the Calendars store (TCC "Calendars" permission).
    GuardRule("eval-calendars-read",
              "reading the Calendars data directory from eval bypasses the sandbox and "
              "accesses TCC-protected calendar data directly — this data is not available to "
              "the eval tool",
              "code_pattern", ("~/Library/Calendars",), scope=("eval",)),
    # same class as eval-addressbook-read, for the Photos library (TCC "Photos" permission).
    GuardRule("eval-photos-library-read",
              "reading the Photos library from eval bypasses the sandbox and accesses "
              "TCC-protected photo data directly — this data is not available to the eval tool",
              "code_pattern", ("~/Pictures/Photos Library.photoslibrary",), scope=("eval",)),
    # rig-cli#701 — see SIGNAL_MATCHERS. pstree-fed kills are DENY, not ask: `pstree -p` with an
    # empty or stale pid lists the WHOLE system tree, so the target set is unknowable by
    # construction (the exact 2026-10-15 command). Broadcast/process-group kills and signals to
    # session infrastructure have no legitimate agent use at all.
    GuardRule("kill-pstree-fed",
              _signal_hint("pstree-fed kills are banned: `pstree -p` with an empty or wrong pid "
                           "lists the whole system tree"),
              "kill_pstree_fed"),
    GuardRule("kill-broadcast",
              _signal_hint("kill -1, kill 0, negative PIDs (process groups), pkill -v and "
                           "user-wide pkill/killall signal far more than your job"),
              "kill_broadcast"),
    GuardRule("signal-session-infra",
              _signal_hint("never signal or stop systemd --user, user@/session units or slices, "
                           "the session leader, gnome-shell/gnome-session/Xwayland, the tmux "
                           "server or an omp session (yours or another's)"),
              "signal_protected"),
    # rig-cli#714 — see the Runpod spend matchers above. runpod-run is the ONE sanctioned way an
    # agent rents Runpod compute (budget, pod cap, TTL); every other creation path is denied, and
    # so is any agent write to runpod-run's budget ledger or its installed script/skill. runpod-run
    # itself is unaffected: its own runpodctl/curl calls are subprocesses, not tool calls.
    *(
        GuardRule(rule_id, _RUNPOD_CREATE_HINT, "argv_prefix", tokens)
        for rule_id, tokens in _RUNPOD_CREATE_ARGV
    ),
    GuardRule("runpod-http-create", _RUNPOD_CREATE_HINT, "http_write_url", _RUNPOD_API_HOSTS,
              value_prefixes=(r"^/v[12]/(pods|endpoints|clusters)(/|$|\?)",)),
    GuardRule("runpod-graphql-create", _RUNPOD_CREATE_HINT, "http_write_url", _RUNPOD_API_HOSTS,
              flags=_RUNPOD_GRAPHQL_CREATE, value_prefixes=(r"^/graphql(/|$|\?)",)),
    GuardRule("runpod-mcp-create", _RUNPOD_CREATE_HINT, "mcp_tool",
              ("mcp__runpod_create_pod", "mcp__runpod_create_endpoint", "mcp__runpod_create_cluster",
               "mcp__runpod_deploy_hub_repo"),
              scope=MATCHER_SCOPES["mcp_tool"]),
    # update-endpoint also renames and scales to zero; only raising workers above zero rents
    GuardRule("runpod-mcp-scale-up",
              "use runpod-run — scaling a Runpod endpoint above zero workers directly bypasses its "
              "budget, pod cap and TTL",
              "mcp_tool", ("mcp__runpod_update_endpoint",), flags=("workersMin", "workersMax"),
              scope=MATCHER_SCOPES["mcp_tool"]),
    GuardRule("runpod-budget-write",
              "use runpod-run — its budget ledger (~/.config/runpod-run/budget*) is runpod-run's "
              "own state; agents never edit it",
              "write_path", ("~/.config/runpod-run/budget*",), scope=MATCHER_SCOPES["write_path"]),
    # the INSTALLED copy only: the agent-tools source (skills/universal/runpod-offload) stays
    # editable in a worktree, and `rig apply` installs it as a subprocess, not a tool call
    GuardRule("runpod-run-script-write",
              "use runpod-run as installed — change it in the agent-tools source "
              "(skills/universal/runpod-offload) and run `rig apply`, never the installed copy",
              "write_path", ("~/.agents/skills/runpod-offload/**", "~/.local/bin/runpod-run"),
              scope=MATCHER_SCOPES["write_path"]),
    # GraphQL from an eval cell (fetch/requests): only the two pod-renting operation names — the
    # other GraphQL create names (createCluster, saveEndpoint, …) are common identifiers in
    # unrelated code, so the eval scan would false-positive on them
    GuardRule("eval-runpod-graphql-deploy", _RUNPOD_CREATE_HINT, "code_pattern",
              ("podFindAndDeployOnDemand",), flags=("word",), scope=MATCHER_SCOPES["code_pattern"]),
    GuardRule("eval-runpod-graphql-rent", _RUNPOD_CREATE_HINT, "code_pattern",
              ("podRentInterruptable",), flags=("word",), scope=MATCHER_SCOPES["code_pattern"]),
)

# ask = sometimes-legit: confirm with the operator when a UI exists; BLOCK headless (a prompt
# nobody can answer must never auto-approve).
OMP_GUARD_ASK_RULES: tuple[GuardRule, ...] = (
    # broad pattern-kills have nuked OTHER sessions' work before (never-broad-pkill doctrine);
    # reaping one's OWN strays is legit — hence ask, not deny. The hint names the safe way out:
    # an ask that only says "no" pushed the 2026-10-15 agent to an unguarded idiom (rig-cli#701).
    GuardRule("pkill", f"broad pattern-kills have nuked other sessions' work before — {_SIGNAL_SAFE}",
              "argv_prefix", ("pkill",)),
    GuardRule("killall", f"broad pattern-kills have nuked other sessions' work before — {_SIGNAL_SAFE}",
              "argv_prefix", ("killall",)),
    # rig-cli#701: a kill whose target is computed at runtime ($(…), backticks, xargs, a glob, an
    # unchecked $VAR) may be empty or the wrong set — ask, since reaping a verified own child this
    # way is legit; a `[ -n "$P" ] &&` / `${P:?}` guard or `$!` passes without asking.
    GuardRule("kill-computed-pid",
              _signal_hint("kill target computed at runtime ($(…), backticks, xargs, an unchecked "
                           "$VAR) can be empty or the wrong processes"),
              "kill_computed_pid"),
    # `git reset --hard` has destroyed uncommitted work before
    GuardRule("git-reset-hard", "git reset --hard has destroyed uncommitted work before",
              "subcommand_flags", ("git", "reset"), ("--hard",)),
)

