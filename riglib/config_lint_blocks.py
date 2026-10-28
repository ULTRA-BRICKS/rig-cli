"""Config-load hooks for the two lint blocks (``lint_checks`` and ``lint``).

Why a separate module: ``config.py`` is far past the repo's own size/complexity limits, so new block
logic must not grow it. ``config.load`` / ``config.validate`` call these three functions and nothing
else; the per-block rules live in ``lint_checks_config`` and ``lint_run_config``.
"""

from __future__ import annotations

from typing import Any, MutableMapping


def validate_layer(data: dict[str, Any], label: str, layers: MutableMapping[str, Any] | None = None) -> None:
    """Validate the lint blocks of ONE config layer; ``layers`` collects the raw ``lint_checks`` block per layer."""
    if "lint_checks" in data:
        from . import lint_checks_config

        lint_checks_config.validate_block(data["lint_checks"], layer=label)
        if layers is not None:
            layers[label] = data["lint_checks"]
    if "lint" in data:
        from . import lint_run_config

        lint_run_config.validate_block(data["lint"], layer=label)


def resolve_merged(merged: dict[str, Any], lint_checks_layers: dict[str, Any]) -> None:
    """Cross-key validation of the CASCADED lint blocks (errors name the layers behind a merged value)."""
    if "lint_checks" in merged:
        from . import lint_checks_config

        lint_checks_config.resolve(merged, lint_checks_layers)
    if "lint" in merged:
        from . import lint_run_config

        lint_run_config.resolve(merged)
