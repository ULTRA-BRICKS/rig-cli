"""``lint_checks`` and ``lint`` block descriptors for the published JSON Schema.

Why a separate module: ``config_schema.py`` only fixes the registry order, so block descriptors
live in focused modules. ``Block``/``Leaf`` come from the leaf module ``config_schema_model``,
so there is no import cycle with ``config_schema``.
"""

from __future__ import annotations

from .config_schema_model import Block, Leaf
from .lint_checks_ci import SCHEMA_PATTERN as _RIG_SOURCE_PATTERN

_CATEGORY_NAMES = ("lint", "typecheck", "format", "limits", "scripts")
_PIN_KEYS_PATTERN = r"^(oxlint|oxfmt|oxlint-tsgolint|@oxlint/plugins|ruff)$"


def lint_checks_block() -> Block:
    return Block(
        doc=(
            "`rig lint check`: cognitive/cyclomatic complexity (pinned cccc) and file-line limits. "
            "Independent of `linters.*`; valid in the global and the repo layer (repo wins per key)."
        ),
        leaves={"enabled": Leaf("boolean", "master switch for the check and its commit-time activation", default=True)},
        nested={
            "complexity": Block(
                doc="per-function complexity limits; a value strictly above the limit fails",
                leaves={
                    "enabled": Leaf("boolean", "run the complexity check", default=True),
                    "max_cognitive": Leaf("integer", "cognitive complexity limit", default=15, minimum=1, maximum=10_000_000),
                    "max_cyclomatic": Leaf("integer", "cyclomatic complexity limit", default=20, minimum=1, maximum=10_000_000),
                },
            ),
            "file_lines": Block(
                doc="physical line-count limits per file (warning above warn_above, error above error_above)",
                leaves={
                    "enabled": Leaf("boolean", "run the file-line check", default=True),
                    "warn_above": Leaf("integer", "lines above this warn (must be <= error_above)", default=1200, minimum=1, maximum=10_000_000),
                    "error_above": Leaf("integer", "lines above this fail", default=2400, minimum=1, maximum=10_000_000),
                    "exempt_extensions": Leaf(
                        "array",
                        "lowercase extensions without a dot that are exempt from the file-line check",
                        items_type="string",
                        items_pattern="^[a-z0-9]+$",
                        default=["json"],
                    ),
                },
            ),
            "ci": Block(
                doc="where the rendered CI workflow installs rig from; the workflow is only written when this is set",
                leaves={
                    "rig_source": Leaf(
                        "string",
                        "immutable `git+https://<host>/<path>@<40-hex commit sha>` of a rig-cli that has `rig lint check`",
                        pattern=_RIG_SOURCE_PATTERN,
                    ),
                },
            ),
        },
    )


def lint_block() -> Block:
    provider = Block(
        doc="per-provider override: switch one check provider off or bound its runtime (argv is never configurable).",
        leaves={
            "enabled": Leaf("boolean", "run this provider (false => its checks report `not-applicable`)", default=True),
            "timeout_seconds": Leaf("integer", "per-check timeout for this provider", minimum=1, maximum=3600),
        },
    )
    return Block(
        doc=(
            "`rig lint` (aggregator): which applicable checks run, their timeouts, and the local bootstrap of "
            "Oxlint/Oxfmt/Ruff. Configuration only (nothing is reconciled by `rig apply`); valid in the global "
            "and the repo layer (repo wins per key). Providers' commands are never configurable."
        ),
        leaves={
            "enabled": Leaf("boolean", "master switch for `rig lint run` (not for `rig lint check` / `rig lint rules`)", default=True),
            "timeout_seconds": Leaf("integer", "default per-check timeout", default=300, minimum=1, maximum=3600),
        },
        nested={
            "categories": Block(
                doc="switch a whole check category off (its checks report `not-applicable`)",
                leaves={name: Leaf("boolean", f"run the `{name}` category", default=True) for name in _CATEGORY_NAMES},
            ),
            "bootstrap": Block(
                doc="local-only installation of missing Oxlint/Oxfmt (JS/TS) and Ruff (Python); never runs in CI",
                leaves={
                    "enabled": Leaf("boolean", "allow bare local `rig lint` to install missing tooling", default=True),
                    "javascript": Leaf("boolean", "bootstrap Oxlint + Oxfmt for JS/TS scopes without a configured tool", default=True),
                    "python": Leaf("boolean", "bootstrap Ruff for Python scopes without a configured tool", default=True),
                    "versions": Leaf(
                        "object",
                        "exact-version overrides keyed by pinned package (oxlint, oxfmt, oxlint-tsgolint, @oxlint/plugins, ruff)",
                        additional_properties_type="string",
                        property_names_pattern=_PIN_KEYS_PATTERN,
                    ),
                },
            ),
        },
        open_map="providers",
        open_map_doc="per-provider overrides keyed by provider id (eslint, ruff-check, shellcheck, ...)",
        open_map_item=provider,
    )
