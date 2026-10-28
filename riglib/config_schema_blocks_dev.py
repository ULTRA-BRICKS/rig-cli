"""Scripts, dev, models and agents_md blocks of the published schema registry.

Split out of ``config_schema`` to keep each module small; the registry order is assembled there.
"""

from __future__ import annotations

from .config_schema_model import Block, Leaf


_SCRIPTS_BLOCK = Block(
    doc="repo-level commands consumed by the standalone `dev run <name>` CLI (alex-mextner/dev-cli).",
    additional_properties={
        "anyOf": [
            {"type": "string", "pattern": r"\S"},
            {
                "type": "object",
                "properties": {
                    "cmd": {
                        "type": "string",
                        "pattern": r"\S",
                        "description": "command line run by `dev run <name>`",
                    },
                },
                "required": ["cmd"],
                "additionalProperties": False,
            },
        ],
    },
)

_DEV_E2E_JOB_BLOCK = Block(
    doc="one named e2e job consumed by the standalone `dev` CLI (alex-mextner/dev-cli).",
    leaves={
        "script": Leaf("string", "name of the top-level scripts entry that runs this e2e job"),
        "requires_server": Leaf("boolean", "whether this e2e job expects the dev server", default=True),
        "artifacts_root": Leaf("string", "directory where this e2e job writes artifacts"),
        "logs_root": Leaf("string", "directory where this e2e job writes logs"),
    },
)

_DEV_BLOCK = Block(
    doc="repo-level development server and e2e metadata consumed by the standalone `dev` CLI "
    "(alex-mextner/dev-cli, provisioned like any other ecosystem tool via the `tools:` block).",
    nested={
        "server": Block(
            doc="development server metadata for `dev` lifecycle commands.",
            leaves={
                "script": Leaf("string", "name of the top-level scripts entry that starts the dev server"),
                "url": Leaf("string", "base URL the development server serves"),
                "ready_url": Leaf("string", "URL the dev CLI can poll before running e2e"),
                "port": Leaf(
                    "integer",
                    "a single known TCP port the dev CLI may check (fallback alias for `ports: "
                    "[port]` — dev-cli reads `ports` first, then falls back to this)",
                    minimum=1,
                    maximum=65535,
                ),
                "ports": Leaf(
                    "array",
                    "known TCP ports the dev CLI may check",
                    items_type="integer",
                    minimum=1,
                    maximum=65535,
                ),
                "process_matchers": Leaf(
                    "array",
                    "process command substrings the dev CLI may use to identify owned server processes",
                    items_type="string",
                ),
                "logs_root": Leaf("string", "directory where the dev CLI writes server logs"),
            },
        ),
        "e2e": Block(
            doc="end-to-end test metadata for `dev` lifecycle commands.",
            leaves={
                "script": Leaf("string", "name of the top-level scripts entry that runs e2e tests"),
                "requires_server": Leaf("boolean", "whether e2e expects the dev server to be running", default=True),
                "artifacts_root": Leaf("string", "directory where e2e writes artifacts"),
                "logs_root": Leaf("string", "directory where the dev CLI writes e2e logs"),
            },
            open_map="jobs",
            open_map_doc="named e2e jobs keyed by job name",
            open_map_item=_DEV_E2E_JOB_BLOCK,
        ),
    },
)

_MODELS_BLOCK = Block(
    doc="a daily cron that runs the model-freshness checker and proposes version bumps.",
    leaves={
        "enabled": Leaf("boolean", "provision the daily checker schedule", default=True),
        "checker_path": Leaf("string", "the model_freshness.py the schedule runs"),
    },
    nested={
        "schedule": Block(
            doc="when the daily checker runs.",
            leaves={
                "time": Leaf("string", "daily run time, HH:MM 24-hour", default="12:00"),
                "label": Leaf("string", "the launchd Label / systemd unit name"),
            },
        ),
    },
)

_AGENTS_MD_BLOCK = Block(
    doc="one canonical AGENTS.md + a CLAUDE.md symlink, so every harness reads the same instructions.",
    leaves={
        "enabled": Leaf("boolean", "provision the canonical + symlink pair", default=True),
        "symlink": Leaf("boolean", "alias opt-out — false equals enabled:false", default=True),
    },
)
