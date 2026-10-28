"""Permission-allowlist provisioning — the per-harness command allowlist single source of truth.

What this is
------------
rig provisions each agent harness's permission ALLOWLIST so our ecosystem CLIs (``tg``,
``review``, ``draw``, ``3d``, ``rig``, ``task``, ``dev``, ``pm``, ``research``) and read-only
helper tools (``rg``, ``jq``, ``gitleaks``) are pre-allowed — the agent
never stops to ask permission for a known-safe command. The tool list is CONFIG-DRIVEN
(declared in ``rig.yaml`` / the global config under the ``permissions`` block) with a sensible
default set ON; this module is the registry that backs it and the renderer that turns one tool
name into the exact allowlist ENTRY each harness honors.

Why a module and not inline strings
------------------------------------
Each harness expresses "auto-allow command ``foo`` and its subcommands" in a DIFFERENT shape:

- **claude-code** — ``~/.claude/settings.json`` JSON, ``permissions.allow`` is a JSON ARRAY of
  strings; the entry is ``"Bash(foo:*)"`` (the proven prefix-glob form CC honors).
- **opencode** — ``~/.config/opencode/opencode.json`` JSON, ``permission.bash`` (singular
  ``permission``) is an OBJECT whose KEYS are command globs and whose VALUES are
  ``"allow"``/``"ask"``/``"deny"``; the entry is ``"foo *": "allow"``.
- **codex** — the config.toml allowlist is N/A (no per-command array to merge), but the allow +
  coarse-deny EFFECT is delivered via Starlark ``execpolicy`` ``.rules`` files
  (``prefix_rule(pattern=[...], decision="allow"|"forbidden")``) — a SEPARATE surface rig now
  provisions through :data:`HARNESS_EXECPOLICY` (the ``provision_execpolicy`` action), not this
  additive-array allowlist. Recorded N/A *here* (the allowlist registry); provisioned there.
- **pi** — N/A. No documented per-command auto-approve allowlist that leaves the toolset intact;
  recorded N/A rather than write a setting that could break the harness.
- **omp** — no per-command allowlist (approval is per-TOOL: ``tools.approval.<tool>``
  with ``allow|deny|prompt`` in ``~/.omp/agent/config.yml``), but command-granular deny/ask IS
  deliverable: omp auto-discovers TS extensions whose ``tool_call`` handler can block a bash
  call before execution. rig provisions that surface — a GENERATED guard extension
  (:data:`HARNESS_GUARD`, the ``install_harness_guard`` action) carrying the deny/ask
  baseline, plus the declarative approval posture (:data:`HARNESS_APPROVAL`). Recorded N/A
  *here* (the allowlist registry); provisioned there (rig-cli#202).
- **commandcode** — N/A. No documented per-command auto-approve allowlist; recorded N/A
  rather than write a setting that could break the harness. Its deny/ask INTENT ships as an
  advisory AGENTS.md block (:data:`HARNESS_INSTRUCTION_POLICY`).

Keeping the per-harness shape behind :data:`HARNESS_ALLOWLISTS` means the plan/runner/drift code
keys off ``harness.kind`` exactly like the existing skill/hook provisioning, and a new harness is
one table entry plus its renderer — never scattered string literals.

Stdlib-only (the repo import rule): no yaml/json here; callers serialize.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Callable, Literal, TypedDict

from .harness_skills import HARNESS_INSTRUCTION_FILES, omp_agent_root

# Re-exported (``as`` aliases mark them public): the guard rules live in permissions_guard_rules.
from .permissions_guard_rules import (
    _RUNPOD_CREATE_ARGV,
    MATCHER_SCOPES as MATCHER_SCOPES,
    OMP_GUARD_ASK_RULES as OMP_GUARD_ASK_RULES,
    OMP_GUARD_DENY_RULES as OMP_GUARD_DENY_RULES,
    SIGNAL_MATCHERS as SIGNAL_MATCHERS,
    GuardRule as GuardRule,
)

# ── the default tool list — our ecosystem CLIs + read-only helper tools + gh ────────────────
# CONFIG-DRIVEN: this is the DEFAULT set rig pre-allows; a config ``permissions.tools`` replaces
# it wholesale, and ``permissions.extra`` / ``permissions.disable`` apply deltas on top. The grant
# is at the command-PREFIX level (``Bash(<tool>:*)`` covers a tool's subcommands/flags), so raw
# process-control / package-manager tools stay OUT of the default set. Development lifecycle work
# routes through ``dev``, whose implementation validates process/project ownership. ``gh`` is the
# one git-hosting tool that IS in the default set — see the note above :data:`DEFAULT_GIT_HOSTING_TOOLS`.
#
# Our ecosystem CLIs (tg/review/draw/3d/rig/task/dev/pm/research) and the external tools we lean
# on. ``task`` is alex-mextner/task-cli (the binary is ``task``). ``dev`` is the agent-tools
# project-local development command surface: rig provisions the permission entry, while the dev
# helper's own implementation/provenance stays in agent-tools. ``pm`` (pm-cli) and ``research``
# (research-cli) are read-only ecosystem coordinators — a project-manager observer/reconciler and
# a multi-provider research/panel CLI; both observe and never edit code, matching the safe
# read-only profile of ``review``/``task``. ``rg`` is ripgrep's binary name. Raw ``git``, ``uv``,
# ``bun``, ``npm``, ``docker``, ``kill``, ``lsof``, ``ps`` and ``pgrep`` are deliberately absent
# by default — those safety checks belong inside ``dev``.
#
# ``gh`` (the GitHub CLI) IS pre-allowed by default, unlike the other raw git-hosting/process
# tools above — everyday agent use of it is read-heavy (``gh pr view``, ``gh issue list``). Be
# honest about the residual risk this trades in, though: ``gh`` is a BROAD write transport, not
# just a merge tool — ``gh api -X PUT/POST ...`` reaches the same REST surface a browser session
# would (including the PR-merge endpoint), ``gh api graphql`` can run a ``mergePullRequest``/
# ``enablePullRequestAutoMerge`` mutation, and unrelated commands like ``gh auth token`` (prints
# the OAuth token) or ``gh repo delete``/``gh secret set`` are just as reachable under a blanket
# ``gh:*`` grant. The baked ``gh-pr-merge`` deny/forbidden rule on every belt below
# (:data:`CLAUDE_CODE_DENY_RULES`, :data:`OPENCODE_DENY_RULES`, :data:`OMP_GUARD_DENY_RULES`,
# :data:`CODEX_DENY_RULES`) covers ONLY the direct, argv-prefix ``gh pr merge`` form — a global
# flag ahead of the subcommand (``gh --repo o/r pr merge 5``) or the ``gh api``/``gh api graphql``
# merge routes above are NOT caught by any of these prefix/glob rules, on ANY harness; that gap is
# real, not hypothetical (verified in review). What DOES close it, on claude-code and codex (where
# rig's separate ``agent_hooks``/hook-bridge feature is enabled — see ``riglib.plan``'s
# ``codex_hook_bridge`` wiring), is agent-tools' own argv-level ``block-raw-pr-merge`` hook: it
# tokenizes the full command (not just a prefix), skips ``gh``'s global flags, and recognizes both
# the REST and GraphQL merge routes — the same "coarse belt here, argv-parsing deep layer in the
# hook bridge" split already documented for force-push/``--no-verify`` above. That hook does NOT
# exist for opencode or omp today, and even where it runs it targets MERGES specifically, not the
# wider ``gh auth token``/``gh repo delete``/``gh secret set`` write surface — those stay open
# under the default ``gh`` grant on every harness. Where the deny rule DOES apply (the direct
# ``gh pr merge`` form), it still wins over the broader ``gh`` allow: claude-code evaluates
# deny → ask → allow (first match wins, deny checked first); opencode is last-match-wins and rig
# both emits deny/ask entries after the allow keys AND re-anchors them there on every subsequent
# apply (see ``riglib.actions.permissions_specs._merge_permission_container``, added because a machine that
# had already applied the OLD opencode baseline — deny key present, no ``gh`` allow key yet — would
# otherwise get the new ``gh *`` allow key appended AFTER it, silently un-denying the merge);
# codex execpolicy is most-restrictive-wins (already proven for the analogous ``rg``/``rg --pre``
# overlap, see ``tests/test_execpolicy.py``). A project or machine that wants ``gh`` off entirely —
# closing all of the above at once — can set ``permissions.allow_gh: false`` (sugar over
# ``permissions.disable: [gh]``, which already worked) or list it in ``permissions.disable``
# directly.
DEFAULT_ECOSYSTEM_TOOLS: tuple[str, ...] = (
    "tg", "review", "draw", "3d", "rig", "task", "dev", "pm", "research",
)
DEFAULT_EXTERNAL_TOOLS: tuple[str, ...] = ("rg", "jq", "gitleaks")  # read-only helper tools
DEFAULT_GIT_HOSTING_TOOLS: tuple[str, ...] = ("gh",)  # NOT read-only — see the deny-belt note above
DEFAULT_TOOLS: tuple[str, ...] = (
    DEFAULT_ECOSYSTEM_TOOLS + DEFAULT_EXTERNAL_TOOLS + DEFAULT_GIT_HOSTING_TOOLS
)
# Tools rig USED to pre-allow by default (rig-cli#41), deliberately stopped (rig-cli#165, the
# "raw process-control / package-manager / git-hosting tools are absent" policy above), and never
# reinstated — EXCEPT ``gh``, which rig-cli#332 put back into :data:`DEFAULT_GIT_HOSTING_TOOLS`
# above (see the docstring note there). rig never removes an allow entry, so a machine provisioned
# while ``gh``/``git``/``uv``/``bun`` were all still retired carries their rendered entries —
# `rig status` names them as former rig defaults (kept), not as user drift (rig-cli#357). ``git``,
# ``uv`` and ``bun`` stay OUT of the desired set; this tuple exists only to name their origin.
RETIRED_DEFAULT_TOOLS: tuple[str, ...] = ("git", "uv", "bun")


def _render_claude_code(tool: str) -> str:
    """The claude-code ``permissions.allow`` entry that pre-allows command ``tool`` + its args.

    ``Bash(foo:*)`` is the prefix-glob form Claude Code honors for "any invocation of ``foo``"
    (the colon-``*`` is the documented trailing wildcard; it matches ``foo``, ``foo sub``,
    ``foo --flag x``). This MUST match the existing accumulated entries' shape (``Bash(gh:*)``,
    ``Bash(git:*)`` are already in the live settings) so a re-apply is a true dedup no-op.
    """
    return f"Bash({tool}:*)"


def _render_opencode(tool: str) -> str:
    """The opencode ``permission.bash`` KEY that pre-allows command ``tool`` + its args.

    opencode keys ``permission.bash`` by a command GLOB; ``"foo *"`` matches ``foo`` with any
    args. The VALUE is the literal ``"allow"`` (supplied by the merge code). The space form is
    opencode's documented pattern syntax (no colon form).
    """
    return f"{tool} *"


def _render_codex_rule(tool: str) -> str:
    """The codex execpolicy ``prefix_rule`` line that pre-allows command ``tool`` + its args.

    codex auto-scans ``~/.codex/rules/*.rules`` (Starlark) at startup; a ``prefix_rule`` whose
    ``pattern`` is the single leading token ``[<tool>]`` and ``decision="allow"`` matches any
    invocation that STARTS with ``tool`` (``tool``, ``tool sub``, ``tool --flag x``). This is
    coarse by design — a leading-token prefix cannot target a specific dangerous flag, so the
    precise flag-position guards stay in the PreToolUse hook bridge (same split as claude-code).
    """
    toks = ", ".join(f'"{t}"' for t in tool.split())
    return f'prefix_rule(pattern=[{toks}], decision="allow", justification="rig-managed")'


def _render_codex_deny(pattern: tuple[str, ...]) -> str:
    """A codex execpolicy ``forbidden`` ``prefix_rule`` for the multi-token command ``pattern``.

    ``decision="forbidden"`` is the most-restrictive verdict (``forbidden > prompt > allow``). The
    pattern is a token list, so ``("gh", "pr", "merge")`` blocks any command whose first three
    tokens are ``gh pr merge`` — but NOT ``gh pr list``. Kept coarse + minimal on purpose (see
    :data:`CODEX_DENY_RULES`).
    """
    toks = ", ".join(f'"{t}"' for t in pattern)
    return f'prefix_rule(pattern=[{toks}], decision="forbidden", justification="rig-managed")'


@dataclass(frozen=True)
class HarnessAllowlist:
    """How ONE harness expresses its command allowlist — the shape the runner/drift merge into.

    ``settings_path`` is the per-machine (user-scope) config file; ``key_path`` is the dotted
    path to the allowlist container within it; ``container`` is ``"array"`` (a JSON list of entry
    strings, claude-code) or ``"object"`` (a JSON object keyed by entry string → ``value``,
    opencode). ``render`` turns a tool name into the per-harness entry string; ``value`` is the
    object-form value (``"allow"``) and is ignored for the array form.
    """

    kind: str
    settings_path: str
    key_path: tuple[str, ...]
    container: str  # "array" | "object"
    render: Callable[[str], str]
    value: str | None = None


# The harness kinds rig can provision an allowlist for. claude-code is the primary, proven one
# (its ``permissions.allow`` array is exactly what the live ~/.claude/settings.json already uses);
# opencode's ``permission.bash`` object is the second. codex + pi have NO additively-
# mergeable per-command allowlist (see the module docstring) and are absent here → recorded N/A by
# :func:`harness_supported` / the harness matrix, never written.
HARNESS_ALLOWLISTS: dict[str, HarnessAllowlist] = {
    "claude-code": HarnessAllowlist(
        kind="claude-code",
        settings_path="~/.claude/settings.json",
        key_path=("permissions", "allow"),
        container="array",
        render=_render_claude_code,
    ),
    "opencode": HarnessAllowlist(
        kind="opencode",
        settings_path="~/.config/opencode/opencode.json",
        key_path=("permission", "bash"),
        container="object",
        render=_render_opencode,
        value="allow",
    ),
}

# Harness kinds that have NO additively-mergeable per-command allowlist concept → N/A in the
# matrix. Recorded explicitly (with the reason) so ``rig`` can report "N/A" rather than silently
# doing nothing or, worse, writing a setting that breaks the harness.
HARNESS_ALLOWLIST_NA: dict[str, str] = {
    "codex": (
        "no per-command allowlist in config.toml — command execution is gated by "
        "approval_policy/sandbox_mode (coarse) and Starlark execpolicy .rules files, a separate "
        "mechanism rig does not additively merge"
    ),
    "pi": (
        "no config command-allowlist — but pi permissions ARE provisioned via the "
        "`permission-guard` extension (deny/ask enforced on the bash tool); see "
        "HARNESS_PERMISSION_EXTENSIONS / harness_permission_extension()"
    ),
    "omp": (
        "approval is per-tool (tools.approval.<tool> in ~/.omp/agent/config.yml), not a "
        "per-command allowlist — no command-token list to merge"
    ),
    "commandcode": "no documented command-allowlist mechanism",
}


# ── pi: permissions provisioned via an EXTENSION, not a config allowlist ──────────────────────
# pi ships no built-in permission system (it runs bash with the user's own privileges). Its
# extension API CAN intercept the bash tool pre-execution, so rig provisions the
# ``permission-guard`` extension (in agent-tools ``pi-extensions/``) + a rig-owned policy file it
# reads. That delivers the SAME deny-dangerous / ask-risky effect the other harnesses get — so pi
# is NO LONGER an N/A skip; it is handled on a dedicated path (:func:`harness_permission_extension`).
HARNESS_PERMISSION_EXTENSIONS: dict[str, str] = {
    "pi": "permission-guard",
}


class _PiPolicyRuleRequired(TypedDict):
    """The fields present on EVERY baked rule (verified: all 8 entries across ``PI_DENY_RULES``/
    ``PI_ASK_RULES`` carry every one of these) — split out so :class:`PiPolicyRule` can require
    them while still leaving the match-criteria fields optional (a plain ``total=False`` TypedDict
    would let a dict missing any of them type-check, defeating the point of documenting the shape
    at all)."""

    id: str
    action: Literal["deny", "ask"]
    command: str
    reason: str


class PiPolicyRule(_PiPolicyRuleRequired, total=False):
    """One rule in the pi ``permission-guard`` extension's policy JSON (``rig-permission-policy.json``).

    Mirrors the extension's own rule shape (agent-tools ``pi-extensions/permission-guard/policy.ts``):
    ``id``/``action``/``command``/``reason`` are always present on a baked rule (required, via
    :class:`_PiPolicyRuleRequired`); ``argvAll`` (every listed token must appear, in any position)
    and ``flagsAny`` (at least one listed flag must appear) are the only genuinely OPTIONAL match
    criteria — a rule may carry either, both, or neither (matching on ``command`` alone).
    """

    argvAll: list[str]
    flagsAny: list[str]


# The pi policy dialect is argv-intent / flag-ANYWHERE (what the extension's matcher enforces), NOT
# claude-code's prefix globs — so it can catch e.g. `git commit -m "..." --no-verify` that prefix
# rules miss. SYNC: keep in step with the extension's baked DEFAULT_POLICY in agent-tools
# ``pi-extensions/permission-guard/policy.ts`` (rig writes these rules into the policy file the
# extension reads; the extension embeds the same set as its fail-closed fallback).
PI_DENY_RULES: tuple[PiPolicyRule, ...] = (
    {
        "id": "gh-pr-merge",
        "action": "deny",
        "command": "gh",
        "argvAll": ["pr", "merge"],
        "reason": "raw `gh pr merge` is banned — PR merges go through the gated `gh ship` delegator",
    },
    {
        "id": "git-force-push",
        "action": "deny",
        "command": "git",
        "argvAll": ["push"],
        "flagsAny": ["--force", "-f"],
        "reason": "force-push rewrites shared history; use --force-with-lease if you truly must",
    },
    {
        "id": "git-no-verify",
        "action": "deny",
        "command": "git",
        "flagsAny": ["--no-verify"],
        "reason": "--no-verify bypasses the commit/push guard hooks; run the hooks",
    },
    {
        "id": "sudo-rm",
        "action": "deny",
        "command": "sudo",
        "argvAll": ["rm"],
        "reason": "no legitimate agent flow removes files as root",
    },
    {
        "id": "screencapture",
        "action": "deny",
        "command": "screencapture",
        "reason": "screenshots go through Playwright/CDP; screencapture black-frames off-Space windows",
    },
)
PI_ASK_RULES: tuple[PiPolicyRule, ...] = (
    {
        "id": "pkill",
        "action": "ask",
        "command": "pkill",
        "reason": "broad pattern-kills have nuked other sessions' work — confirm the target",
    },
    {
        "id": "killall",
        "action": "ask",
        "command": "killall",
        "reason": "broad pattern-kills have nuked other sessions' work — confirm the target",
    },
    {
        "id": "git-reset-hard",
        "action": "ask",
        "command": "git",
        "argvAll": ["reset"],
        "flagsAny": ["--hard"],
        "reason": "git reset --hard destroys uncommitted work — confirm first",
    },
)


def harness_permission_extension(kind: str) -> str | None:
    """The pi-extension name rig provisions to deliver permissions for ``kind``, or ``None``.

    Non-``None`` means the harness gets its permission belt via an installed extension + a
    rig-written policy file (pi today), NOT the config-allowlist merge (claude-code/opencode).
    """
    return HARNESS_PERMISSION_EXTENSIONS.get(kind)


def pi_policy_document(
    deny_override: list[PiPolicyRule] | None = None,
    ask_override: list[PiPolicyRule] | None = None,
) -> dict[str, object]:
    """The policy JSON rig writes for the pi ``permission-guard`` extension to read.

    ``deny_override`` / ``ask_override`` REPLACE the baked baseline wholesale (lists are atomic
    decisions, mirroring ``permissions.tools``/``deny``); ``None`` selects the baseline. deny rules
    precede ask rules, but the extension takes the STRONGEST decision across matches regardless of
    order, so a deny always wins over an ask on the same command.
    """
    # deepcopy so the returned document owns its nested argvAll/flagsAny lists — a shallow dict(r)
    # would alias the module-level PI_DENY_RULES/PI_ASK_RULES lists, and any in-place mutation of the
    # returned rules would corrupt the baseline for every subsequent call in the process.
    deny = list(deny_override) if deny_override is not None else copy.deepcopy(list(PI_DENY_RULES))
    ask = list(ask_override) if ask_override is not None else copy.deepcopy(list(PI_ASK_RULES))
    return {"version": 1, "default": "allow", "rules": [*deny, *ask]}


def harness_supported(kind: str) -> bool:
    """True when rig can provision an allowlist for ``kind`` (else N/A — see HARNESS_ALLOWLIST_NA)."""
    return kind in HARNESS_ALLOWLISTS


@dataclass(frozen=True)
class HarnessGuard:
    """How ONE harness receives a GENERATED command-granular guard (omp TS extension).

    ``extension_name`` is the wholly rig-owned file written under the harness's
    auto-discovered extensions dir (``<agent root>/extensions/``); ``root`` resolves that
    agent root (unexpanded), so the plan never hard-codes a harness's env contract. The
    content is codegen'd from :data:`OMP_GUARD_DENY_RULES` / :data:`OMP_GUARD_ASK_RULES` by
    :mod:`riglib.omp_guard` — one registry, one renderer, no hand-copied lists.
    """

    kind: str
    extension_name: str
    root: Callable[[], str]


HARNESS_GUARD: dict[str, HarnessGuard] = {
    "omp": HarnessGuard(kind="omp", extension_name="rig-permissions-guard.ts", root=omp_agent_root),
}


def guard_supported(kind: str) -> bool:
    """True when rig can provision a generated guard extension for ``kind`` (omp today)."""
    return kind in HARNESS_GUARD


def guard_extension_path_for(kind: str) -> str:
    """The ONE canonical (unexpanded) guard extension path for ``kind`` — plan, the runner's
    interlock fallback, and drift all derive from this so they can never resolve differently."""
    spec = HARNESS_GUARD[kind]
    return f"{spec.root()}/extensions/{spec.extension_name}"


@dataclass(frozen=True)
class HarnessApproval:
    """The declarative approval posture rig merges into a harness's config file.

    ``config_name`` is relative to the harness's agent root (``root`` resolves it,
    unexpanded); ``keys`` are ``(dotted_key_path, desired_scalar)`` pairs merged ADDITIVELY
    (set only when absent — a user's differing value is drift, never clobbered; a matching
    value without rig's receipt is 'compatible unmanaged', adopted not rewritten).
    """

    kind: str
    config_name: str
    keys: tuple[tuple[tuple[str, ...], str], ...]
    root: Callable[[], str]


# omp: approvalMode made EXPLICIT so the posture is declarative and drift-checkable. Parity
# with claude-code is auto_mode:true + the guard belt — NOT a prompt-per-bash floor (owner
# decision, rig-cli#202): the guard extension is the enforcement layer; the YAML is posture.
# The ``yolo`` here is the LEGACY default only: the plan overrides it per the harness auto intent
# (``riglib.harness_mode`` — ``harness.auto_mode: false`` → ``always-ask``), carried on the action as
# ``options["mode_value"]``. This action is the ONE owner of ``tools.approvalMode`` (rig-cli#355).
HARNESS_APPROVAL: dict[str, HarnessApproval] = {
    "omp": HarnessApproval(
        kind="omp",
        config_name="config.yml",
        keys=((("tools", "approvalMode"), "yolo"),),
        root=omp_agent_root,
    ),
}


def approval_supported(kind: str) -> bool:
    """True when rig can provision a declarative approval posture for ``kind`` (omp today)."""
    return kind in HARNESS_APPROVAL


# Instruction-file harnesses get the deny/ask INTENT as an ADVISORY managed block in their
# global instruction file — worded as advisory, never claimed as execution-layer enforcement.
# Membership is EXPLICIT (NOT "every harness with an instruction file"): adding an entry to
# HARNESS_INSTRUCTION_FILES must never silently start splicing policy into a tier-1 harness's
# file. Paths are sourced from the harness_skills registry (one owner of the file locations).
HARNESS_INSTRUCTION_POLICY: dict[str, str] = {
    "pi": HARNESS_INSTRUCTION_FILES["pi"],
    "commandcode": HARNESS_INSTRUCTION_FILES["commandcode"],
}


def instruction_policy_supported(kind: str) -> bool:
    """True when rig can provision an advisory instruction policy for ``kind`` (pi/commandcode)."""
    return kind in HARNESS_INSTRUCTION_POLICY


# ── deny / ask baselines — the OUTER enforcement belt (rig-cli#100) ──────────────────────────
# CTO decision 2026-07-01: the harness permissions layer — deny, ask, AND allow — must be
# provisioned/reconciled by rig, not hand-edited. Claude Code evaluates permission rules
# deny → ask → allow (first match wins) BEFORE PreToolUse hooks and independently of the model,
# and a user-scope deny cannot be overridden by a project-level allow — that makes these lists
# the OUTER belt; the argv-parsing agent-hooks (block-no-verify, block-raw-pr-merge, …) stay the
# deep layer underneath (they parse flags anywhere in argv, which prefix patterns cannot).
#
# The baseline is deliberately CONSERVATIVE and word-boundary precise: a deny rule that
# false-positives on legitimate commands teaches agents to route around the belt — worse than no
# rule. Verified matcher semantics (code.claude.com/docs/en/permissions, fetched 2026-07-01):
#   - ``Bash(x:*)`` — ``:*`` is the trailing word-boundary wildcard, equal to ``Bash(x *)``:
#     matches ``x`` and ``x <args>`` but never ``x2`` (boundary = space or end-of-string).
#   - a mid-pattern ``*`` matches ANY char sequence including spaces; literal `` --flag `` around
#     it keeps the boundary (``git push * --force *`` matches ``git push origin main --force``
#     but NOT ``git push --force-with-lease …`` — ``-with-lease`` breaks the boundary).
#   - compound commands are matched per subcommand (``a && b`` evaluates both independently).
#
# WHAT STAYS HOOK-ONLY (and why): ``git commit --no-verify`` with the flag in a LATER position
# (``git commit -m "…" --no-verify``, the common shape) cannot be pattern-matched safely — the
# only pattern that would catch it (``Bash(git commit *--no-verify*)``) also matches a commit
# MESSAGE that merely mentions the flag (this ecosystem writes such messages), a guaranteed
# false positive. The flag-first prefix rule below is the safe subset; the ``block-no-verify``
# agent-hook (argv-level) remains the authoritative guard. The same applies to wrapper bypasses
# in general (``sh -c '…'``, env-runner wrappers): prefix rules anchor at the command start, so
# the hooks stay the deep layer — permissions and hooks are complementary, not redundant.
CLAUDE_CODE_DENY_RULES: tuple[str, ...] = (
    # raw PR merges are banned machine-wide — merges go through `gh ship` (the gated delegator)
    "Bash(gh pr merge:*)",
    # force pushes: flag-first, mid-position AND end-anchored forms; `--force-with-lease` (the
    # safe force) is deliberately NOT matched — the word boundary after `--force` / `-f` excludes
    # it. The end-anchored forms (`… * --force`) are listed EXPLICITLY even though the docs say a
    # trailing ` *` also matches end-of-string — the common `git push origin main --force` must
    # not hinge on that one reading of the matcher (review finding, rig-cli#100).
    "Bash(git push --force:*)",
    "Bash(git push * --force *)",
    "Bash(git push * --force)",
    "Bash(git push -f:*)",
    "Bash(git push * -f *)",
    "Bash(git push * -f)",
    # hook-bypass commits — flag-first prefix only (see the module note above for the gap)
    "Bash(git commit --no-verify:*)",
    # no legitimate agent flow removes files as root
    "Bash(sudo rm:*)",
    # screenshots go through Playwright/CDP; `screencapture` black-frames windows on other
    # Spaces and trips macOS Screen Recording grants (the documented hard rule)
    "Bash(screencapture:*)",
    # AppleScript/System Events sent from bash triggers an unapprovable macOS TCC "bash would
    # like to access data from other apps" prompt — same class as screencapture above. No
    # legitimate agent flow invokes osascript directly from bash; native UI automation goes
    # through the harness's own AX/native-input helpers instead.
    "Bash(osascript:*)",
    # `rg --pre <CMD>` / `--pre=<CMD>` runs CMD as an arbitrary preprocessor on every matched
    # file — the default `Bash(rg:*)` allow grant is meant for read-only search, not arbitrary
    # subprocess execution (rig-cli#187). rg's arg parser allows flags in any position (unlike
    # git's subcommand-first shape), so both value forms (space, `=`) are covered flag-first AND
    # in a later position; `=` is a literal boundary character (not a space), so it needs its own
    # pattern — `Bash(rg --pre:*)` alone does not match `--pre=CMD` (see the module note above on
    # the `:*` word-boundary semantics). `--pre-glob` is DELIBERATELY NOT globbed here (or
    # anywhere, including the omp ARGV guard below): per rg's docs it has no effect unless
    # `--pre` is also set, and `--pre` is caught on its own by every belt, so denying bare
    # `--pre-glob` anywhere would only add pattern bloat + false-positive surface for zero
    # security gain.
    #
    # DIFFERENT CALL than `--no-verify` above, on purpose, not "the same gap accepted twice": for
    # `--no-verify` the flag-anywhere form was REJECTED because a false positive there is a commit
    # MESSAGE merely mentioning the flag (noise, no security value in blocking it). Here a false
    # positive is a literal-text SEARCH for `--pre` (`rg -e --pre .`) — rare, and the asymmetry
    # (missing a real preprocessor-exec vector vs. over-denying a rare literal search) favors
    # keeping the later-position form. UNLIKE `--no-verify`, though, `rg-pre` has no
    # claude-code-specific argv-precise hook backstopping this glob belt yet (`--no-verify`'s is
    # `block-no-verify` in agent-tools) — the omp guard's ARGV matcher ("rg-pre") only covers the
    # omp harness, not claude-code, and is itself not entirely free of this false-positive class
    # (it doesn't honor `--` end-of-options, so `rg -- --pre .` is still denied there too, a
    # pre-existing tokenizer gap shared with every other `subcommand_flags` rule, e.g.
    # `git reset -- --hard`, not specific to `rg-pre`). An analogous claude-code `block-rg-pre`
    # agent-hook is a real, tracked follow-up (agent-tools side), not attempted in this fix.
    "Bash(rg --pre:*)",
    "Bash(rg * --pre *)",
    "Bash(rg * --pre)",
    "Bash(rg --pre=*)",
    "Bash(rg * --pre=*)",
    # raw `git worktree add` is banned — worktrees go through `rig worktree create` (same
    # pattern as `gh pr merge` vs `gh ship`). The child `git worktree add` inside `rig` is
    # not a gated tool call, so the sanctioned path still works. Prefix form only:
    # `git -C <repo> worktree add` (and `--git-dir` / `--work-tree`) does not match — the
    # same prefix-anchor gap as `gh pr merge`. Unlike that case there is no argv-precise
    # claude-code hook backstopping this belt yet. A mid-pattern
    # `Bash(git * worktree add:*)` would also match a commit message that merely mentions
    # "worktree add", so it stays prefix-only on purpose.
    "Bash(git worktree add:*)",
    # EnterWorktree is a WHOLE-TOOL deny on purpose. The tool's own description says it
    # "Creates an isolated worktree (via git or configured hooks) and switches the session
    # into it" — a model-supplied path can land outside <repo>/.worktrees/ (GH-329). A
    # specifier-shaped glob cannot express "only the canonical path"; denying the tool
    # removes the prompt AND the location-bypass. CC-only (no EnterWorktree equivalent
    # on opencode/codex/omp).
    "EnterWorktree",
    # rig-cli#714: Runpod compute is rented only through runpod-run. The argv forms mirror the
    # omp guard's argv rules (prefix-only: `runpodctl -o json pod create` and the guard's
    # HTTP/GraphQL/write-path rules stay omp-only). The Runpod MCP create/deploy tools are
    # WHOLE-TOOL denies (Claude Code names MCP tools mcp__<server>__<tool>, Runpod's tool names
    # are hyphenated) — none of them has a legitimate agent use outside runpod-run.
    *(f"Bash({' '.join(tokens)}:*)" for _, tokens in _RUNPOD_CREATE_ARGV),
    "mcp__runpod__create-pod",
    "mcp__runpod__create-endpoint",
    "mcp__runpod__create-cluster",
    "mcp__runpod__deploy-hub-repo",
)

# ask = sometimes-legit: force a prompt (tg-ctl relays it to the operator's phone), don't block.
CLAUDE_CODE_ASK_RULES: tuple[str, ...] = (
    # broad pattern-kills have nuked OTHER sessions' work before (never-broad-pkill doctrine);
    # reaping one's OWN strays is legit — hence ask, not deny
    "Bash(pkill:*)",
    "Bash(killall:*)",
    # `git reset --hard` has destroyed uncommitted work before; flag-first, mid + end-anchored
    "Bash(git reset --hard:*)",
    "Bash(git reset * --hard *)",
    "Bash(git reset * --hard)",
)

# ── opencode deny / ask baselines — the OUTER belt in opencode's permission.bash glob dialect ─
# opencode's ``permission.bash`` glob dialect (VERIFIED 2026-07): ``*`` = zero-or-more chars,
# ``?`` = one char, LAST matching key wins. These mirror the claude-code baselines' INTENT but are
# hand-written in that dialect — they are CONSTANTS (a deny/ask rule targets a dangerous invocation,
# not a per-tool render). The VALUE ("deny"/"ask") comes from the rule container spec below.
#
# FIDELITY GAP (deliberate, documented): opencode ``*`` has NO word boundary, so ``git push*--force*``
# ALSO matches ``--force-with-lease`` (the SAFE force) that the claude-code word-boundary matcher
# EXCLUDES. Over-blocking the safe force here is acceptable — the precise flag-position guard lives
# in the opencode PreToolUse plugin bridge (same split as claude-code: glob/prefix rules are the
# coarse belt, the argv-parsing hook is the deep layer). The flag-first ``git commit --no-verify*``
# form is safe (flag leads, so it can't false-positive on a commit MESSAGE that mentions the flag).
OPENCODE_DENY_RULES: tuple[str, ...] = (
    "gh pr merge*",
    "git push*--force*",
    "git push -f*",
    "git commit --no-verify*",
    "sudo rm*",
    "screencapture*",
    # AppleScript/System Events sent from bash triggers an unapprovable macOS TCC prompt — same
    # class as screencapture above.
    "osascript*",
    # raw `git worktree add` is banned — worktrees go through `rig worktree create`
    "git worktree add*",
    # rig-cli#187: `rg --pre` runs an arbitrary preprocessor command. A single `rg*--pre*` entry
    # was REJECTED (review finding): opencode's `*` has no word boundary, so it would also deny
    # the legitimate, read-only `--pretty` flag (`--pretty` contains the substring `--pre`).
    # `--pre-glob` is deliberately NOT globbed here (or anywhere) — see the claude-code belt's
    # comment (no effect without `--pre`, which is caught on its own everywhere).
    # A trailing bare-end form (`rg*--pre`, `--pre` as the literal last token, no value) mirrors
    # claude-code's `Bash(rg * --pre)` — `--pretty` never ends a command at exactly `--pre`, so
    # this stays safe (`"rg --pretty".endswith("--pre")` is false).
    "rg*--pre *",
    "rg*--pre=*",
    "rg*--pre",
    # rig-cli#714: direct Runpod creation goes through runpod-run (same argv forms as claude-code)
    *(" ".join(tokens) + "*" for _, tokens in _RUNPOD_CREATE_ARGV),
)
OPENCODE_ASK_RULES: tuple[str, ...] = (
    "pkill*",
    "killall*",
    "git reset*--hard*",
)

# The baked rule baseline per harness kind. claude-code (Bash(...) specifiers, vendor-doc-verified)
# and opencode (permission.bash globs, verified dialect). Other kinds are absent (empty).
DEFAULT_RULES: dict[str, dict[str, tuple[str, ...]]] = {
    "claude-code": {"deny": CLAUDE_CODE_DENY_RULES, "ask": CLAUDE_CODE_ASK_RULES},
    "opencode": {"deny": OPENCODE_DENY_RULES, "ask": OPENCODE_ASK_RULES},
}


@dataclass(frozen=True)
class RuleContainer:
    """WHERE a harness holds one deny/ask rule list, and its container shape.

    ``key_path`` is the dotted path in the settings file; ``container`` is ``"array"`` (claude-code
    — deny/ask are separate JSON lists) or ``"object"`` (opencode — deny/ask share the SAME
    ``permission.bash`` object with the allow list, keyed by glob → ``value``). ``value`` is the
    object-form per-entry value (``"deny"``/``"ask"``) and is ``None`` for the array form.
    """

    key_path: tuple[str, ...]
    container: str  # "array" | "object"
    value: str | None = None


# Where each rule list lives per harness kind. claude-code writes two dedicated arrays; opencode
# folds deny/ask INTO the same ``permission.bash`` object as allow (values "deny"/"ask") — so the
# runner emits them AFTER the allow keys, keeping rig's denies past any broad ``*``/allow (opencode
# is last-match-wins).
HARNESS_RULE_CONTAINERS: dict[str, dict[str, RuleContainer]] = {
    "claude-code": {
        "deny": RuleContainer(("permissions", "deny"), "array", None),
        "ask": RuleContainer(("permissions", "ask"), "array", None),
    },
    "opencode": {
        "deny": RuleContainer(("permission", "bash"), "object", "deny"),
        "ask": RuleContainer(("permission", "bash"), "object", "ask"),
    },
}
# Kinds with NO verified deny/ask rule dialect at all (recorded so a config that asked for rules
# gets a visible plan note rather than a silent drop). Empty now that opencode is verified.
HARNESS_RULES_NA: dict[str, str] = {}

# Kinds whose config-level ``permissions.allow/deny/ask`` lists are consumed VERBATIM. Those
# user-facing lists are documented as claude-code dialect (``Bash(...)`` specifiers), so only
# claude-code adopts them. opencode HAS rule containers but uses its BAKED opencode-dialect
# baseline: an explicit config override there would be a claude-shaped string written as an
# opencode glob key (a rule that never matches), so it is dropped with a plan note instead.
CONFIG_RULE_DIALECT_KINDS: frozenset[str] = frozenset({"claude-code"})


def resolve_rules(kind: str, role: str, override: list[str] | None) -> list[str]:
    """The effective ``role`` (``deny``/``ask``) rule list for harness ``kind``.

    ``override`` (the config's ``permissions.deny``/``permissions.ask``) REPLACES the baked
    default wholesale — lists are atomic decisions, mirroring ``permissions.tools`` — so an
    explicit ``[]`` disables the baseline. ``None`` (absent key) selects the default. Deduped,
    first-seen order, so the merged container stays stable across re-applies. A kind without
    rule containers has no defaults (and the plan drops a configured override with a note).
    """
    base = list(override) if override is not None else list(DEFAULT_RULES.get(kind, {}).get(role, ()))
    out: list[str] = []
    seen: set[str] = set()
    for rule in base:
        if rule not in seen:
            seen.add(rule)
            out.append(rule)
    return out


def resolve_tools(
    tools: list[str] | None,
    extra: list[str] | None,
    disable: list[str] | None,
) -> list[str]:
    """Resolve the effective tool list: (``tools`` or the default set) + ``extra`` − ``disable``.

    Deterministic + de-duplicated, preserving first-seen order so the rendered allowlist is stable
    across re-applies (no churn from set ordering). An explicit ``tools`` REPLACES the default set
    (lists are atomic decisions, mirroring the config cascade); ``extra`` adds; ``disable`` drops a
    tool from rig's DESIRED set so rig won't add it — it does NOT remove an entry already in the
    user's on-disk allowlist (the merge is additive-only; rig never deletes the user's entries).
    """
    base = list(tools) if tools is not None else list(DEFAULT_TOOLS)
    base += list(extra or [])
    removed = set(disable or [])
    out: list[str] = []
    seen: set[str] = set()
    for t in base:
        if t in removed or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def desired_entries(kind: str, tools: list[str]) -> list[str]:
    """The per-harness allowlist entry strings for ``tools``, in tool order (deduped).

    Raises ``KeyError`` for an unsupported kind — callers gate on :func:`harness_supported` first
    (the plan only emits supported kinds), so this is a defensive guard.
    """
    spec = HARNESS_ALLOWLISTS[kind]
    out: list[str] = []
    seen: set[str] = set()
    for t in tools:
        entry = spec.render(t)
        if entry in seen:
            continue
        seen.add(entry)
        out.append(entry)
    return out


# ── codex execpolicy — allow + coarse deny via ~/.codex/rules/*.rules (rig-cli MVP) ──────────
# codex has NO per-command allowlist in config.toml, but it auto-scans ``~/.codex/rules/*.rules``
# (Starlark) at startup with NO config.toml reference needed. rig writes a MARKER-DELIMITED managed
# block of ``prefix_rule(...)`` lines: the same resolved tool set as the allowlist (decision=allow)
# plus a MINIMAL coarse deny (decision=forbidden). This is the codex counterpart of the claude-code
# permissions.allow/deny belt — keyed off ``harness.kind`` via :data:`HARNESS_EXECPOLICY` so the
# plan/runner/drift never scatter the path.
#
# FIDELITY GAP (deliberate): ``prefix_rule`` matches a LEADING-token prefix, so a coarse
# ``("git", "push")`` forbidden would over-block ALL pushes. The deny set is therefore kept to
# unambiguous full-command bans only; every LATER-position flag-guard (force-push, --no-verify
# anywhere, and — as of rig-cli#187 — ``rg --pre`` in a non-leading position, or the joined
# ``--pre=CMD`` form) stays in the PreToolUse hook bridge (same split as claude-code). codex has
# NO such bridge today (unlike omp's generated guard, :data:`HARNESS_GUARD` has no codex entry),
# so codex is left with the SAME pre-existing exposure it already has for later-position
# force-push/--no-verify — not a regression this fix introduces, but real and tracked (see the PR
# description for #187), not silently absent. ``rg --pre`` in the FLAG-FIRST, space-separated
# position IS an unambiguous 2-token leading prefix, though (``rg --pre`` can never legitimately
# lead a read-only search), so it gets the same coarse treatment as the other full-command bans —
# a cheap partial mitigation, not full coverage. UNLIKE the other three entries, this one
# OVERLAPS an existing allow prefix (``rg`` is in the default allow set) — verified against the
# real ``codex execpolicy check`` CLI (not assumed) that the more-specific forbidden rule wins
# over the broader allow (see ``tests/test_execpolicy.py``,
# ``test_generated_block_passes_codex_execpolicy_check``): this is real coverage, not dead code
# shadowed by the allow rule.
CODEX_DENY_RULES: tuple[tuple[str, ...], ...] = (
    ("gh", "pr", "merge"),
    ("git", "worktree", "add"),
    ("sudo", "rm"),
    ("screencapture",),
    ("osascript",),
    ("rg", "--pre"),
    # rig-cli#714: unambiguous full-command bans — Runpod compute goes through runpod-run
    *(tokens for _, tokens in _RUNPOD_CREATE_ARGV),
)


@dataclass(frozen=True)
class HarnessExecpolicy:
    """How ONE harness expresses a startup-scanned command policy file (codex execpolicy .rules).

    ``rules_path`` is the per-machine file rig writes its managed block into; ``render_allow`` turns
    one tool name into an ``allow`` ``prefix_rule`` line; ``deny_rules`` is the coarse ``forbidden``
    baseline (token-list patterns).
    """

    kind: str
    rules_path: str
    render_allow: Callable[[str], str]
    deny_rules: tuple[tuple[str, ...], ...]


HARNESS_EXECPOLICY: dict[str, HarnessExecpolicy] = {
    "codex": HarnessExecpolicy(
        kind="codex",
        rules_path="~/.codex/rules/rig-managed.rules",
        render_allow=_render_codex_rule,
        deny_rules=CODEX_DENY_RULES,
    ),
}


def execpolicy_supported(kind: str) -> bool:
    """True when rig can provision an execpolicy .rules block for ``kind`` (codex today)."""
    return kind in HARNESS_EXECPOLICY


def execpolicy_rule_lines(kind: str, tools: list[str]) -> list[str]:
    """The managed ``prefix_rule(...)`` lines for ``kind``: allow(tools) then the coarse deny set.

    Deduped in tool order; the forbidden lines follow (execpolicy is most-restrictive-wins, so
    order does not change the verdict, but keeping deny last mirrors the opencode/claude ordering
    discipline and reads clearly). Raises ``KeyError`` for an unsupported kind (callers gate on
    :func:`execpolicy_supported`).
    """
    spec = HARNESS_EXECPOLICY[kind]
    lines: list[str] = []
    seen: set[str] = set()
    for t in tools:
        line = spec.render_allow(t)
        if line not in seen:
            seen.add(line)
            lines.append(line)
    lines.extend(_render_codex_deny(p) for p in spec.deny_rules)
    return lines


# ── enforcement tiers — the honest per-harness permissions vocabulary (rig-cli#202) ──────────
# Replaces the flat N/A in the permissions matrix: EVERY known harness has a tier, and "N/A"
# is only ever a CELL (one mechanism a harness lacks), never a harness verdict.
#   1 = command-granular ENFORCED (deny/ask belt executed below the model)
#   2 = tool-granular (approval per tool, no command granularity) — reserved; no harness today
#   3 = ADVISORY (intent recorded in the instruction file; the agent self-enforces)
def _derive_permission_tiers() -> dict[str, int]:
    """The tier of every known kind, DERIVED from registry membership (never hand-maintained —
    a kind listed tier 1 without a real enforcement surface would be a silent lie):
    allowlist / execpolicy / guard / permission-extension surfaces → 1; approval posture
    WITHOUT a guard → 2; advisory instruction policy → 3."""
    tiers: dict[str, int] = {}
    for kind in (*HARNESS_ALLOWLISTS, *HARNESS_EXECPOLICY, *HARNESS_GUARD):
        tiers[kind] = 1
    for kind in HARNESS_APPROVAL:
        tiers.setdefault(kind, 2)  # tool-granular only when no command-granular guard
    for kind in HARNESS_INSTRUCTION_POLICY:
        tiers[kind] = 3
    for kind in HARNESS_PERMISSION_EXTENSIONS:
        # A provisioned extension (pi's permission-guard) IS command-granular enforcement, not
        # advisory — overrides the tier-3 default a kind also gets from HARNESS_INSTRUCTION_POLICY
        # (pi carries both: the extension enforces, the AGENTS.md block is a belt-and-suspenders
        # advisory note; the extension is the real mechanism and must win the tier).
        tiers[kind] = 1
    return tiers


HARNESS_PERMISSION_TIERS: dict[str, int] = _derive_permission_tiers()

TIER_LABELS: dict[int, str] = {
    1: "command-granular enforced",
    2: "tool-granular",
    3: "advisory",
}
