"""Harness-launch, permissions and mode blocks of the published schema registry.

Split out of ``config_schema`` to keep each module small; the registry order is assembled there.
"""

from __future__ import annotations

from .config_schema_model import Block, Leaf
from .config_schema_patterns import PERMISSION_RULE_JSON_PATTERN
from .config_schema_patterns import _HARNESS_KIND_ENUM


_HARNESS_BLOCK = Block(
    doc="the agent harness's auto/permission mode — provisioned, not hand-toggled.",
    leaves={
        "enabled": Leaf("boolean", "provision the harness setting", default=True),
        "kind": Leaf(
            "string",
            "which harness to provision (skills-dir: claude-code/codex; native-discovery: opencode/omp; "
            "instruction-file: pi/commandcode; codex is also instruction-file via AGENTS.md). "
            "Every kind gets its own auto/permission-mode key written (claude-code permissions.defaultMode, "
            "codex approvals_reviewer, opencode permission.*, omp tools.approvalMode via the permissions "
            "approval action); pi/commandcode have no such setting and get a visible n/a note.",
            enum=_HARNESS_KIND_ENUM,
            default="claude-code",
        ),
        "kinds": Leaf(
            "array",
            "additional harnesses to provision alongside harness.kind. Use this when one machine "
            "runs multiple harnesses: the primary kind keeps its auto-mode/settings_path behavior, "
            "while additional kinds get skill discovery, agent-hook descriptors, any supported "
            "hook bridge, and their own auto-mode key (same auto intent). If agent_hooks.target pins descriptors to one explicit target, supported "
            "bridges are registered with a descriptor-dir override.",
            items_type="string",
            items_enum=_HARNESS_KIND_ENUM,
        ),
        "auto_mode": Leaf(
            "boolean",
            "true → auto-accept tool calls; false → interactive — written per harness kind. A pinned "
            "mode: the primary kind knows overrides it (mode: auto means auto for every configured kind).",
            default=True,
        ),
        "self_merge": Leaf(
            "boolean",
            "true → add the ship allow rules (Bash(gh ship:*), Bash(fj ship:*)) and deny rules for the raw "
            "ship.sh/pr-ship.sh scripts to permissions (and drop the legacy Bash(*/pr-ship.sh:*) / "
            "Bash(*/ship.sh:*) allow entries) so the auto-mode Bash gate stops vetoing gh ship, PLUS the "
            "natural-language carve-out to autoMode.allow (clears the Merge-Without-Review + "
            "Self-Approval soft blocks for the agent's OWN PRs); auto-mode only; the Bash(gh pr merge:*) "
            "deny and every other classifier rule (incl. the anti-exfil hard rule) stay",
            default=True,
        ),
        "mode": Leaf("string", "pin the exact permission value (overrides auto_mode map)"),
        "settings_path": Leaf(
            "string",
            "override the per-harness settings/config/plugin path; absent means rig uses the harness-specific default",
        ),
        "settings_paths": Leaf(
            "array",
            "EXTRA claude-code user-scope settings.json files the auto-mode/permissions/hook-bridge "
            "writes also reach (each a .json path); only honored when the primary target is "
            "~/.claude/settings.json",
            items_type="string",
        ),
        "discover_config_dirs": Leaf(
            "boolean",
            "auto-discover every ~/.claude-accounts/account-*/ (claude-rotate) config dir and treat its "
            "settings.json as a managed target alongside ~/.claude/settings.json — a `claude` started "
            "with CLAUDE_CONFIG_DIR reads ONLY that dir's settings, so without this no rig hook runs there",
            default=True,
        ),
    },
    nested={
        "hook_bridge": Block(
            doc="wire the agents-hooks/v1 dispatcher into the supported harness config.",
            leaves={
                "enabled": Leaf("boolean", "wire the supported harness hook-bridge dispatcher", default=True),
                "python": Leaf("string", "the interpreter the dispatcher runs under", default="python3"),
            },
        ),
        "web_search": Block(
            doc="omp web_search provider exclude list, reconciled into ~/.omp/agent/config.yml.",
            leaves={
                "enabled": Leaf(
                    "boolean",
                    "provision providers.webSearchExclude when omp is a configured harness kind",
                    default=True,
                ),
                "exclude": Leaf(
                    "array",
                    "provider ids web_search must never use, even as fallbacks; default [perplexity]",
                    items_type="string",
                ),
            },
        ),
        "idle_ttl": Block(
            doc="omp task.agentIdleTtlMs grace window, reconciled into ~/.omp/agent/config.yml.",
            leaves={
                "enabled": Leaf(
                    "boolean",
                    "provision task.agentIdleTtlMs when omp is a configured harness kind",
                    default=True,
                ),
                "ms": Leaf(
                    "integer",
                    "idle-TTL grace window in ms before a yielded omp helper parks off hub list; "
                    "default 300000 (5 min); <= 0 restores the old never-park behavior",
                    default=300000,
                ),
            },
        ),
        "codex_skills_budget": Block(
            doc="codex [skills].max_context_tokens budget, reconciled into ~/.codex/config.toml "
                "(or $RIG_CODEX_HOME/config.toml).",
            leaves={
                "enabled": Leaf(
                    "boolean",
                    "provision skills.max_context_tokens when codex is a configured harness kind",
                    default=True,
                ),
                "max_context_tokens": Leaf(
                    "integer",
                    "Codex's context budget (tokens) for rendering the model-visible skills "
                    "catalog; default 50000 raises Codex's own smaller stock default so a "
                    "sizeable personal skill library is not silently truncated/omitted "
                    "(openai/codex#24299). Only WRITTEN when the key is absent -- apply never "
                    "clobbers a value already on disk",
                    default=50000,
                ),
            },
        ),
    },
)

_PERMISSIONS_BLOCK = Block(
    doc="reconcile the harness permissions layer: the command allowlist + the deny/ask rule baselines.",
    leaves={
        "enabled": Leaf("boolean", "provision the harness permissions layer", default=True),
        "kind": Leaf(
            ("string", "null"),
            "which harness's permissions to provision; null/absent means fan out to supported "
            "harness.kind plus harness.kinds",
            enum=("claude-code", "opencode", "codex", "omp", "pi", "commandcode", None),
        ),
        "tools": Leaf("array", "command names to pre-allow (replaces the default set)", items_type="string"),
        "extra": Leaf("array", "command names to ADD on top of the set", items_type="string"),
        "disable": Leaf("array", "command names to drop from rig's desired set", items_type="string"),
        "allow_gh": Leaf(
            "boolean",
            "pre-allow the GitHub CLI (gh); false opts out (sugar over disable: [gh]) — "
            "see the gh note in docs/config-schema/harness-and-permissions.md for the residual risk this trades in",
            default=True,
        ),
        "allow": Leaf(
            "array",
            "raw permission-rule entries asserted present in the allow list, on TOP of the tool-derived ones",
            items_type="string",
            items_pattern=PERMISSION_RULE_JSON_PATTERN,
        ),
        "deny": Leaf(
            "array",
            "permission-rule entries asserted present in the deny list (REPLACES the baked baseline)",
            items_type="string",
            items_pattern=PERMISSION_RULE_JSON_PATTERN,
        ),
        "ask": Leaf(
            "array",
            "permission-rule entries asserted present in the ask list (REPLACES the baked baseline)",
            items_type="string",
            items_pattern=PERMISSION_RULE_JSON_PATTERN,
        ),
        "settings_path": Leaf("string", "override the settings file (.json)"),
    },
)

_MODE_BLOCK = Block(
    doc="machine-wide operating mode; `autonomous` provisions review/fix, quorum, escalation, and parallelism policy.",
    leaves={
        "name": Leaf(
            "string",
            "operating mode for agent sessions",
            enum=("standard", "autonomous"),
            default="standard",
        ),
    },
    nested={
        "autonomous": Block(
            doc="policy knobs active when mode.name is autonomous.",
            nested={
                "review_fix": Block(
                    doc="review/fix loop policy.",
                    leaves={
                        "enabled": Leaf("boolean", "run review/fix iterations", default=True),
                        "max_iterations": Leaf(
                            "integer",
                            "maximum review/fix iterations before escalation or budget stop",
                            default=5,
                            minimum=1,
                        ),
                        "until": Leaf(
                            "string",
                            "loop stop condition",
                            enum=("clean", "budget", "manual"),
                            default="clean",
                        ),
                    },
                ),
                "decisions": Block(
                    doc="decision-making policy before asking the user.",
                    nested={
                        "review_quorum": Block(
                            doc="multi-model quorum required for decisions.",
                            leaves={
                                "enabled": Leaf("boolean", "require review quorum for decisions", default=True),
                                "min_iterations": Leaf(
                                    "integer",
                                    "minimum recorded review iterations",
                                    default=2,
                                    minimum=1,
                                ),
                                "min_models": Leaf(
                                    "integer",
                                    "minimum distinct models in the quorum",
                                    default=3,
                                    minimum=2,
                                ),
                            },
                        ),
                    },
                ),
                "escalation": Block(
                    doc="when and how an agent may escalate to the user.",
                    leaves={
                        "framework_skill": Leaf(
                            "string",
                            "skill that defines escalation message behavior",
                            default="decision-request-discipline",
                        ),
                        "require_parallel_worktree_comparison": Leaf(
                            "boolean",
                            "compare independent worktree attempts before escalation",
                            default=True,
                        ),
                    },
                ),
                "parallel_worktree_comparison": Block(
                    doc="parallel worktree comparison before escalation.",
                    leaves={
                        "enabled": Leaf("boolean", "run independent worktree comparisons", default=True),
                        "candidates": Leaf(
                            "integer",
                            "number of independent candidate implementations to compare",
                            default=2,
                            minimum=2,
                        ),
                    },
                ),
                "development_tools": Block(
                    doc="extra allowlisted development-tool permission flows for autonomous mode.",
                    leaves={
                        "allow": Leaf(
                            "array",
                            "raw permission allow rules to add while in autonomous mode",
                            default=["Bash(dev:*)", "Bash(review:*)", "Bash(task:*)"],
                            items_type="string",
                            items_pattern=PERMISSION_RULE_JSON_PATTERN,
                        ),
                    },
                ),
                "parallelism": Block(
                    doc="limit-aware parallelism caps.",
                    leaves={
                        "max_agents": Leaf("integer", "maximum concurrent agents", default=4, minimum=1),
                        "max_worktrees": Leaf("integer", "maximum concurrent worktrees", default=4, minimum=1),
                        "reserve_slots": Leaf("integer", "slots held back for urgent work", default=1, minimum=0),
                        "limit_aware": Leaf("boolean", "respect harness/model/rate limits when dispatching", default=True),
                    },
                ),
            },
        ),
    },
)
