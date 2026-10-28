"""Module-level constants shared by the config loader and the per-block validators.

Why a separate module: the loader, ``validate`` and every ``config_validate_*`` module read the same
allow-lists (valid top-level keys, enums, global-only blocks); one definition keeps them from drifting.
``config.py`` re-exports each name under its historical underscore-prefixed spelling.
"""

from __future__ import annotations

import re

from .harness_skills import KNOWN_HARNESS_KINDS as _KNOWN_HARNESS_KINDS

CONFIG_FILENAME = "rig.yaml"

# The machine-only `ci.items.ship` external-review policy (consumed by agent-tools' gh ship): one
# list for the global-only load guard and the validator, so a new key cannot reach only one.
SHIP_POLICY_OWNER_KEYS = ("external_review_owners", "external_review_evidence_owners")
SHIP_POLICY_KEYS = ("require_external_review", *SHIP_POLICY_OWNER_KEYS)
GITHUB_OWNER_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")

# Blocks that may ONLY come from the GLOBAL layer (~/.config/rig/config.yaml). Unlike the
# write-routing-only global blocks (bun/spotlight/…, `schema._GLOBAL_ONLY_CATEGORIES`), these are
# REJECTED at load time from any other layer: `git.config` can set code-executing keys
# (core.sshCommand, core.fsmonitor, credential.helper, `!` aliases) machine-wide, and `profile`
# points `rig apply` at a repo whose config it pulls — a committed (possibly adversarial) repo
# rig.yaml must be able to do neither. Name → one-line reason for the error's `why`.
GLOBAL_LAYER_ONLY_BLOCKS: dict[str, str] = {
    "git": "git.config writes the machine's GLOBAL git config (`git config --global`)",
    "profile": "profile points rig at the repo your GLOBAL config is synced with",
    "tracked_files": (
        "tracked_files runs the ship command and pushes branches with your credentials, and picks "
        "which files leave the machine — a committed repo rig.yaml (itself a harvested file) must "
        "not choose either"
    ),
    "harness_wrappers": (
        "harness_wrappers writes executables into a directory you put on PATH and names the binary "
        "they exec — a committed repo rig.yaml must choose neither"
    ),
}

# `fj` is repo-writable, but only its `host` (the forge this repo's logins use). Every other key is
# machine-wide: `client_id` picks the OAuth app fj hands its tokens to for EVERY listed host, and
# `enabled`/`install`/`version`/`bin_dir`/`git_credentials` change the machine's fj install and
# global git config — a committed repo rig.yaml must set none of them.
REPO_LAYER_FJ_KEYS = frozenset({"host"})

VALID_TOP_KEYS = {
    "version",
    "defaults",
    "agent_tools_source",
    "stack",
    "skills",
    "agent_hooks",
    "git_hooks",
    "ci",
    "mcp",
    "mode",
    "harness",
    "permissions",
    "scripts",
    "dev",
    "models",
    "agents_md",
    "github",
    "tmux",
    "env",
    "gitignore",
    "spotlight",
    "bun",
    "fj",
    "git",
    "profile",
    "tools",
    "tg_ctl",
    "tcc_watch",
    "tgctl_reaper",
    "sysmon",
    "internal_dev",
    "ship_delegator",
    "linters",
    "lint_checks",
    "lint",
    "project_tools",
    "ecosystem_readme",
    "scripts",
    "dev",
    "task",
    "tracked_files",
    "harness_wrappers",
}
VALID_CATEGORIES = {"skills", "agent_hooks", "git_hooks", "ci", "mcp"}
VALID_ON_CONFLICT = {"skip", "overwrite", "backup"}
VALID_TIERS = {"block", "warn"}
VALID_ON_ERROR = {"open", "closed"}
VALID_TASK_BACKENDS = {"github-issues", "linear", "tasks", "forgejo"}
VALID_TASK_ATTACHMENT_MODES = {"link", "native"}
FORGEJO_REPO_AUTO = "auto"  # resolve task.forgejo.repo from the repo's own git remote at runtime
FORGEJO_REPO_RE = re.compile(r"\A[^/\s]+/[^/\s]+\Z")  # owner/name; \A..\Z so no call style admits a suffix
FORGEJO_URL_FIX = "use a bare https base URL, e.g. https://git.hyperide.ai"
VALID_LEGACY_AGENT_HOOK_TARGET_KINDS = {"claude-code", "generic"}
MCP_ITEM_KEYS = {"enabled", "server", "command", "args", "env"}
VALID_TMUX_APPLY = {"import", "block"}
VALID_MODE_NAMES = {"standard", "autonomous"}
VALID_AUTONOMOUS_UNTIL = {"clean", "budget", "manual"}
# Harness kinds rig knows a skill/instruction discovery convention for — the union of the
# skills-DIRECTORY harnesses (claude-code, codex), the NATIVE-DISCOVERY harnesses
# (opencode, omp), and the INSTRUCTION-FILE harnesses (pi, commandcode). A
# ``harness.kind`` in this set is ACCEPTED: rig can
# provision skill discovery (and, for the supported kinds, the auto-mode write / allowlist)
# for it. The single source of truth is :mod:`riglib.harness_skills`. The auto-mode WRITE is
# keyed off :mod:`riglib.harness_mode` — a kind with no such setting (pi/commandcode) gets a
# visible plan note, not a crash.
VALID_HARNESS_KINDS = set(_KNOWN_HARNESS_KINDS)
# No kind is "reserved + rejected" any longer — every documented kind is now provisionable for
# skills. Kept as an (empty) set so the validator's reserved-kind branch stays well-defined.
RESERVED_HARNESS_KINDS: set[str] = set()
# DEPRECATED harness kinds — removed everywhere (CTO 2026-07). Still recognized ONLY so a config
# that names one fails with a helpful "no longer supported (deprecated)" message instead of the
# generic typo error. gemini was an instruction-file harness (~/.gemini/GEMINI.md); it is gone.
DEPRECATED_HARNESS_KINDS: dict[str, str] = {
    "gemini": "Gemini is deprecated and no longer provisioned by rig.",
}
