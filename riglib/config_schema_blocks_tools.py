"""Linters, ecosystem-readme, project-tools and tools blocks of the published schema registry.

Split out of ``config_schema`` to keep each module small; the registry order is assembled there.
"""

from __future__ import annotations

from .project_tools import HAFT_WORKFLOW_MODES
from .config_schema_model import Block, Leaf


_LINTERS_ITEM_BLOCK = Block(
    doc="one explicit linter/formatter config file rig writes/reconciles.",
    leaves={
        "tool": Leaf("string", "the tool name (drives status/log labels)"),
        "role": Leaf("string", "linter | formatter", enum=("linter", "formatter"), default="linter"),
        "path": Leaf("string", "repo-relative target config path"),
        "content": Leaf("string", "literal desired content; mutually exclusive with source"),
        "source": Leaf("string", "agent-tools-relative canonical source file; mutually exclusive with content"),
        "enabled": Leaf("boolean", "provision this one file", default=True),
    },
    schema_extra={"oneOf": [
        {"required": ["content"], "not": {"required": ["source"]}},
        {"required": ["source"], "not": {"required": ["content"]}},
    ]},
)

_LINTERS_BUNDLE_ITEM_BLOCK = Block(
    doc="one deterministic directory bundle copied from agent-tools",
    leaves={
        "source": Leaf("string", "agent-tools-relative source directory"),
        "target": Leaf("string", "repo-relative destination directory"),
        "enabled": Leaf("boolean", "provision this bundle", default=True),
    },
)
_LINTERS_BUNDLES_BLOCK = Block(
    doc="directory bundles keyed by label",
    additional_properties=_LINTERS_BUNDLE_ITEM_BLOCK.to_node(),
)

_LINTERS_RULES_BLOCK = Block(
    doc="Rig-owned rule selection. Built-in defaults are inherited through global config and rig.yaml; inspect the effective result with `rig lint rules`.",
    leaves={
        "all": Leaf("boolean", "enable every applicable known policy concept after provider selection/dedup; per-rule severity still wins", default=False),
        "enable": Leaf("array", "individual rules to enable without enabling all", items_type="string"),
        "disable": Leaf("array", "individual rules to disable after defaults/groups/all", items_type="string"),
        "severity": Leaf("object", "final per-rule off|warn|error overrides; highest-precedence rule-policy setting", additional_properties_type="string"),
    },
    nested={
        "groups": Block(
            doc="rule-group toggles keyed by group name; `anti-slop: true` uses audited per-rule defaults rather than forcing every rule to error",
            additional_properties={"type": "boolean"},
        ),
    },
)

_LINTERS_BLOCK = Block(
    doc="Rig-owned lint/format policy plus explicit reusable config carriers.",
    leaves={
        "enabled": Leaf("boolean", "provision lint/format policy", default=True),
        "preview": Leaf("boolean", "show before/after lint finding counts when policy changes", default=True),
    },
    nested={"rules": _LINTERS_RULES_BLOCK, "bundles": _LINTERS_BUNDLES_BLOCK},
    open_map="items",
    open_map_doc=(
        "extra config files keyed by label; each uses `{ tool, role, path, content|source, enabled }`."
    ),
    open_map_item=_LINTERS_ITEM_BLOCK,
    open_map_item_required=("tool", "path"),
)

_ECOSYSTEM_README_BLOCK = Block(
    doc=(
        "writes/updates a marker-delimited '## Ecosystem' section in a target repo's README from "
        "one canonical, in-code tool registry (riglib.ecosystem_readme), so the hand-copied "
        "sibling-tool list every repo carries stops silently drifting (rig-cli#308). Default OFF "
        "(opt-in) — enable it, then `rig apply` inserts the section (or updates a stale one)."
    ),
    leaves={
        "enabled": Leaf("boolean", "provision the README Ecosystem block (opt-in)", default=False),
        "target": Leaf("string", "repo-relative README path to reconcile", default="README.md"),
    },
)

_PROJECT_TOOLS_BLOCK = Block(
    doc="repo-local integration carriers for Haft, Serena, and Sverklo.",
    leaves={
        "enabled": Leaf("boolean", "provision repo-local project tool integrations", default=True),
    },
    nested={
        "haft": Block(
            doc="Haft FPF/spec carriers plus optional Codex MCP registration.",
            leaves={
                "enabled": Leaf("boolean", "provision Haft project carriers", default=True),
                "project_name": Leaf("string", "Haft project name; defaults to the repo directory name"),
                "project_id": Leaf("string", "stable Haft project id; generated from project_name when omitted"),
                "codex_mcp": Leaf("boolean", "merge the Haft MCP server into .codex/config.toml", default=True),
            },
            nested={
                "workflow": Block(
                    doc="Haft workflow defaults written to .haft/workflow.md.",
                    leaves={
                        "mode": Leaf("string", "Haft workflow mode", enum=HAFT_WORKFLOW_MODES, default="standard"),
                        "require_decision": Leaf("boolean", "require explicit decisions for high-impact work", default=True),
                        "require_verify": Leaf("boolean", "require verification evidence before completion", default=True),
                        "allow_autonomy": Leaf("boolean", "allow autonomous Haft execution by default", default=False),
                    },
                ),
            },
        ),
        "serena": Block(
            doc="Serena project configuration under .serena/.",
            leaves={
                "enabled": Leaf("boolean", "provision Serena project config", default=True),
                "project_name": Leaf("string", "Serena project name; defaults to the repo directory name"),
                "languages": Leaf("array", "Serena language ids; auto-detected when omitted", items_type="string"),
                "read_only": Leaf("boolean", "disable Serena editing tools for this project", default=False),
                "ignored_paths": Leaf("array", "extra Serena ignore patterns", items_type="string"),
            },
        ),
        "sverklo": Block(
            doc="Sverklo global registry/index integration for this repo.",
            leaves={
                "enabled": Leaf("boolean", "provision Sverklo integration", default=True),
                "register": Leaf("boolean", "register this repo in the global Sverklo registry", default=True),
                "reindex": Leaf("boolean", "run sverklo reindex during apply (off by default)", default=False),
            },
        ),
    },
)

_TOOLS_ITEM_BLOCK = Block(
    doc="one declared tool: the repo whose install.sh rig runs, plus optional overrides.",
    leaves={
        "enabled": Leaf("boolean", "install this tool (default true)", default=True),
        "repo": Leaf("string", "the tool's checkout dir (holds install.sh); default ~/xp/<name>-cli, "
                     "or ~/dev/<name>-cli when only that one holds an install.sh"),
        "bin_dir": Leaf("string", "override the managed PATH dir for this one tool"),
        "brew": Leaf(
            "array",
            "Homebrew formulae this tool depends on at RUNTIME (not installed by the tool's own "
            "install.sh); rig installs any missing formula via `brew install` before running "
            "install.sh, and `rig status` flags a declared-but-absent formula as drift. Fails "
            "closed with an actionable error if `brew` itself is not on PATH. Default [] — no "
            "package dependency declared, the existing behavior for every tool that omits this key.",
            items_type="string",
        ),
        "apt": Leaf(
            "array",
            "Debian/Ubuntu packages this tool depends on at RUNTIME, the apt twin of `brew`: rig "
            "runs `apt-get install` for the missing ones before install.sh, ONLY on a machine that "
            "has apt-get (skipped on macOS / brew-only boxes, so one profile serves both). Needs "
            "root or a non-interactive sudo; without either the tool is NOT aborted: install.sh "
            "still runs and the exact `sudo apt-get install -y …` to run by hand is reported. A "
            "package whose command is already on PATH counts as present. `rig status` flags a "
            "declared-but-absent package as drift. Default [] (`ctmux`: tmux, fzf).",
            items_type="string",
        ),
        "clone": Leaf(
            "string",
            "git URL rig clones into `repo` when it is absent (a fresh machine needs no manual "
            "checkout). First-class tools carry a default: `ctmux` clones "
            "https://github.com/alex-mextner/ctmux-cli to ~/xp/ctmux-cli.",
        ),
    },
)

_TOOLS_BLOCK = Block(
    doc=(
        "the personal CLI tool ecosystem (tg/review/task/draw/…) rig installs + advertises at "
        "apply, by running each tool's own install.sh. rig also keeps a provisioned checkout FRESH: "
        "if a tool repo ships scripts/deploy.sh, apply runs it (ff-only git pull) even when already "
        "installed — opt-in per tool, non-fatal. Default OFF (opt-in): list tools under items. A "
        "per-MACHINE concern — belongs in the GLOBAL layer, not a committed rig.yaml."
    ),
    leaves={
        "enabled": Leaf("boolean", "provision the listed tools (opt-in)", default=False),
        "target": Leaf("string", "managed PATH dir each tool symlinks its bin into", default="~/.local/bin"),
    },
    open_map="items",
    open_map_doc="per-tool spec keyed by command name (repo / bin_dir / enabled)",
    open_map_item=_TOOLS_ITEM_BLOCK,
)
