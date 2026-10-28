"""Core registry blocks: install-target defaults, skills, agent hooks, git hooks, CI and MCP.

Split out of ``config_schema`` to keep each module small; the registry order is assembled there.
"""

from __future__ import annotations

from .config_schema_model import Block, Leaf


_DEFAULTS_BLOCK = Block(
    doc="cross-category fallback targets + the on-conflict policy.",
    leaves={
        "skills_target": Leaf("string", "default skills install dir", default="~/.agents/skills"),
        "hooks_target": Leaf("string", "default agent-hooks dir", default="~/.claude/hooks"),
        "ci_target": Leaf("string", "default CI workflows dir", default=".github/workflows"),
        "mcp_target": Leaf("string", "default MCP config dir", default="~/.claude/mcp"),
        "on_conflict": Leaf(
            "string",
            "what apply does when a target already exists",
            enum=("skip", "overwrite", "backup"),
            default="backup",
        ),
    },
)

_SKILLS_PLUGIN_ITEM_BLOCK = Block(
    doc="one Claude Code plugin-marketplace-sourced skill declaration.",
    leaves={
        "marketplace": Leaf(
            "string",
            "the Claude Code plugin marketplace name this plugin was installed from (the "
            "`@<marketplace>` half of `installed_plugins.json`'s `<plugin>@<marketplace>` key)",
        ),
    },
)

_SKILLS_BLOCK = Block(
    doc="advisory markdown rules copied into the agent skills dir (opt-out model).",
    leaves={
        "enabled": Leaf("boolean", "install skills at all", default=True),
        "target": Leaf("string", "where SKILL.md dirs are copied", default="~/.agents/skills"),
        "all": Leaf("boolean", "enable all skills (opt-out)", default=True),
        "known": Leaf(
            "array",
            "hand-installed skill dir names on disk that rig does NOT manage but should report as known (informational), "
            "not as drift — see docs/config-schema.md#provenance",
            items_type="string",
        ),
        "harness_link": Leaf(
            "boolean",
            "also symlink each installed skill into the harness discovery dir",
            default=True,
        ),
        "harness_skill_dir": Leaf("string", "override the harness skill-discovery dir"),
    },
    nested={
        "universal": Block(
            doc="the universal skill set (opt-out via disable / opt-in via enable).",
            leaves={
                "all": Leaf("boolean", "enable all universal skills", default=True),
                "disable": Leaf("array", "universal skills to drop", items_type="string"),
                "enable": Leaf("array", "universal skills to add (with all:false)", items_type="string"),
            },
        ),
        "by_type": Block(
            doc="by-type skill bundles for the detected project type.",
            leaves={
                "enable": Leaf("array", "which by-type bundles to install whole", items_type="string"),
                "disable": Leaf("array", "by-type bundles to drop", items_type="string"),
            },
            open_map="items",
            open_map_doc="per-skill overrides keyed by `by-type/<kind>/<name>`",
        ),
        "by_stack": Block(
            doc="by-stack skills auto-selected by the repo's declared `stack` (hierarchical "
            "prefix match); tune with disable / per-item overrides.",
            leaves={
                "disable": Leaf(
                    "array",
                    "by-stack skill item names (`by-stack/<l1>/<lang>[/<fw>]/<name>`) to drop "
                    "even though the declared stack would select them",
                    items_type="string",
                ),
            },
            open_map="items",
            open_map_doc="per-skill overrides keyed by `by-stack/<l1>/<lang>[/<fw>]/<name>` "
            "(`enabled: true` force-adds an off-stack skill; `enabled: false` drops one)",
        ),
        "plugins": Block(
            doc="Claude Code plugin-marketplace-sourced skills — resolved against the live "
            "`~/.claude/plugins/installed_plugins.json` at every `rig apply` so a plugin's "
            "`skills/*` dirs stay symlinked into the skills target even across a plugin "
            "version bump. See docs/config-schema.md#claude-code-plugin-marketplace-sourced-skills-skillsplugins.",
            leaves={},
            open_map="items",
            open_map_doc="one entry per declared plugin, keyed by plugin name (`{marketplace: "
            "<marketplace-name>}`) — the plugin's own `skills/*/SKILL.md` dirs are resolved "
            "from `<plugin>@<marketplace>`'s live installPath",
            open_map_item=_SKILLS_PLUGIN_ITEM_BLOCK,
        ),
    },
)

_AGENT_HOOKS_BLOCK = Block(
    doc="programmatic guards that block before a side effect (no-verify, secrets, raw merge).",
    leaves={
        "enabled": Leaf("boolean", "install the agent-hook guards", default=True),
        "target": Leaf("string", "where hook descriptors are written", default="~/.claude/hooks"),
        "target_kind": Leaf(
            "string",
            "legacy ignored scaffold key; accepted for old configs but not used for rendering",
            enum=("claude-code", "generic"),
        ),
        "all": Leaf("boolean", "install all guard hooks", default=True),
        "known": Leaf(
            "array",
            "hook ids (or full descriptor stems) another installer wrote on disk that rig does NOT manage but should report as known (informational), "
            "not as drift — see docs/config-schema.md#provenance",
            items_type="string",
        ),
        # Two RUNTIME behaviour knobs read BY the installed hooks from this committed rig.yaml
        # (rig apply does not consume them — the hook scripts parse agent_hooks.<key> at fire
        # time). They live here so the strict validator/schema accept them per-repo.
        "worktree_only": Leaf(
            "boolean",
            "enforce the worktree-only workflow: gates THREE hooks. worktree-only-writes DENIES an "
            "Edit/Write while the checkout sits on the default branch (main/master); "
            "pin-primary-worktree DENIES a git checkout/switch that would move the repo's PRIMARY "
            "worktree onto anything but the default branch, and a git commit/cherry-pick/merge/"
            "rebase/revert/am on the primary checkout's default branch; block-devserver-primary "
            "DENIES a dev-server launch there. ON by default: every repo with a rig.yaml is "
            "enforced unless it sets worktree_only: false (a repo that legitimately works "
            "directly on main); a directory with no rig.yaml is never blocked. No "
            "self-service env bypass — each hook has its OWN hatch var: a deliberate one-off "
            "Edit/Write on main is requested via "
            "RIG_HATCH_REQUEST_WORKTREE_ONLY_WRITES=\"<justification>\"; a one-off git checkout/"
            "switch in the primary checkout via "
            "RIG_HATCH_REQUEST_PIN_PRIMARY_WORKTREE=\"<justification>\" (both: tg approval, "
            "deny-by-default; bare 1 rejected). (Alex tg#5742, tg#6462/tg#6477; default flipped agent-tools#1012.)",
            default=True,
        ),
        "orchestrator_only": Leaf(
            "boolean",
            "keep the orchestrator thin: the orchestrator-stays-thin hook warns on the first "
            "inline implementation Bash / code Edit by the main thread (delegate to a subagent), "
            "then blocks a repeat within its TTL. Read-only inspection (git status/ls/cat/grep/"
            "find, git worktree list) is never gated; tg/review/task are sanctioned orchestration, "
            "also never gated. Most gh is still delegated to a subagent — gh pr merge/create/close/"
            "edit, gh issue create/close/edit, gh release, any gh api mutation included. Two "
            "exceptions run inline: an unchained gh ship <PR#> alone on the line, and a fixed "
            "read-only allowlist (gh issue list/view, gh pr list/view/checks/diff, gh run "
            "list/view/watch, gh repo view, GET-only gh api with no -f/--field, -F/--raw-field, "
            "or --input). Opt-OUT, "
            "default ON — set false to exempt a repo that "
            "legitimately works inline (e.g. 3d-cli). No self-service env bypass; a one-off is "
            "requested via RIG_HATCH_REQUEST_ORCHESTRATOR_STAYS_THIN=\"<justification>\" (tg "
            "approval, deny-by-default; bare 1 rejected). (Alex tg#5743, tg#7103; allowlist "
            "enforced by agent-tools#764, 2026-09-18.)",
            default=True,
        ),
    },
    open_map="items",
    open_map_doc="per-hook overrides (enabled / on_error) keyed by hook name",
)

_GIT_HOOKS_BLOCK = Block(
    doc="the global-hook dispatcher: your hooks run in every repo, even ones that hijack core.hooksPath.",
    nested={
        "dispatcher": Block(
            doc="the machine-wide global-hook dispatcher.",
            leaves={
                "enabled": Leaf("boolean", "install the global-hook dispatcher", default=False),
                "dir": Leaf("string", "drop-in fragments dir", default="~/.config/git/global-hooks.d"),
                "runner": Leaf("string", "the dispatcher script", default="~/.config/git/run-global-hooks"),
                "set_global_hooks_path": Leaf(
                    "boolean", "wire it as git's global core.hooksPath", default=True
                ),
                "install_local_retrofit_script": Leaf(
                    "boolean", "put install-local-hooks.sh on PATH", default=True
                ),
            },
            open_map="fragments",
            open_map_doc="per-fragment overrides keyed by fragment name (secret-scan, …)",
        ),
    },
)

_CI_BLOCK = Block(
    doc="vendor-neutral CI gates (secret-scan, codeql, dependency-review, ship, …).",
    leaves={
        "enabled": Leaf("boolean", "install CI gate workflows", default=True),
        "target": Leaf("string", "workflows dir, or `export-only`", default=".github/workflows"),
        "all": Leaf("boolean", "install every gate (vs per-item)", default=False),
        "known": Leaf(
            "array",
            "workflow file stems this repo owns that collide with a catalog slot name on disk that rig does NOT manage but should report as known (informational), "
            "not as drift — see docs/config-schema.md#provenance",
            items_type="string",
        ),
    },
    open_map="items",
    open_map_doc="per-gate overrides (enabled / tier / variant / …) keyed by gate name",
)

_MCP_ITEM_BLOCK = Block(
    doc="one MCP server registration override.",
    leaves={
        "enabled": Leaf("boolean", "register this MCP server", default=True),
        "server": Leaf("string", "server name written under mcpServers"),
        "command": Leaf("string", "server executable, or legacy command string when args is omitted"),
        "args": Leaf("array", "argv passed to command exactly", items_type="string"),
        "env": Leaf(
            "object",
            "environment variables for this server",
            additional_properties_type="string",
        ),
    },
)

_MCP_BLOCK = Block(
    doc="MCP registrations (review, code-search) — callable from any agent.",
    leaves={
        "enabled": Leaf("boolean", "register MCP servers", default=True),
        "target": Leaf("string", "MCP config dir, file, or `export-only`", default="~/.claude/mcp"),
        "known": Leaf(
            "array",
            "MCP server names registered by something other than rig on disk that rig does NOT manage but should report as known (informational), "
            "not as drift — see docs/config-schema.md#provenance",
            items_type="string",
        ),
    },
    open_map="items",
    open_map_doc="per-server overrides (command / args / env / server / enabled) keyed by server name",
    open_map_item=_MCP_ITEM_BLOCK,
)
