"""Top-level fail-closed config validation: root shape, version, defaults, ``stack`` and the
ordered dispatch to every per-block validator.

It lives apart from the block validators so the call ORDER (first error wins) is visible in
one table. It must not import ``riglib.config`` or ``config_load`` (they import this module).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .config_checks import reject_unknown_keys
from .config_constants import VALID_CATEGORIES, VALID_ON_CONFLICT
from .config_errors import ConfigError
from .config_validate_ci import (
    validate_agent_hooks,
    validate_ci,
    validate_git_hooks,
    validate_mcp,
)
from .config_validate_dev import validate_dev, validate_scripts
from .config_validate_env import validate_bun, validate_env, validate_gitignore, validate_spotlight
from .config_validate_git import validate_fj, validate_git, validate_profile
from .config_validate_github import validate_github
from .config_validate_harness import validate_harness
from .config_validate_internal import validate_internal_dev, validate_ship_delegator
from .config_validate_linters import validate_linters
from .config_validate_project_tools import validate_ecosystem_readme, validate_project_tools
from .config_validate_mode import validate_mode
from .config_validate_models import validate_agents_md, validate_models
from .config_validate_permissions import validate_permissions
from .config_validate_skills import validate_skills
from .config_validate_sysmon import validate_sysmon
from .config_validate_task import validate_task
from .config_validate_tmux import validate_tmux
from .config_validate_tools import (
    validate_tcc_watch,
    validate_tg_ctl,
    validate_tgctl_reaper,
    validate_tools,
)
from .config_validate_tracked_files import validate_tracked_files
from .config_validate_wrappers import validate_harness_wrappers

BlockValidator = Callable[[Any], None]

# Validators that run BEFORE the shared lint-blocks layer check, in the original order.
_BLOCKS_BEFORE_LINT: tuple[tuple[str, BlockValidator], ...] = (
    ("ci", validate_ci),
    ("agent_hooks", validate_agent_hooks),
    ("mcp", validate_mcp),
    ("git_hooks", validate_git_hooks),
    ("skills", validate_skills),
    ("mode", validate_mode),
    ("harness", validate_harness),
    ("permissions", validate_permissions),
    ("scripts", validate_scripts),
    ("dev", validate_dev),
    ("models", validate_models),
    ("agents_md", validate_agents_md),
    ("github", validate_github),
    ("tmux", validate_tmux),
    ("env", validate_env),
    ("gitignore", validate_gitignore),
    ("spotlight", validate_spotlight),
    ("bun", validate_bun),
    ("fj", validate_fj),
    ("git", validate_git),
    ("profile", validate_profile),
    ("tools", validate_tools),
    ("tg_ctl", validate_tg_ctl),
    ("tcc_watch", validate_tcc_watch),
    ("tgctl_reaper", validate_tgctl_reaper),
    ("sysmon", validate_sysmon),
    ("internal_dev", validate_internal_dev),
    ("ship_delegator", validate_ship_delegator),
    ("linters", validate_linters),
)

# ...and the ones that run AFTER it.
_BLOCKS_AFTER_LINT: tuple[tuple[str, BlockValidator], ...] = (
    ("project_tools", validate_project_tools),
    ("ecosystem_readme", validate_ecosystem_readme),
    ("task", validate_task),
    ("tracked_files", validate_tracked_files),
    ("harness_wrappers", validate_harness_wrappers),
)


def validate_stack(data: dict[str, Any]) -> None:
    """Validate the top-level ``stack`` preset value's SHAPE, if present.

    A MISSING stack is NOT an error here — per-repo mandatoriness is a SOFT requirement surfaced
    as a warning by init/apply/status (see :func:`stack_requirement_warning`), not a hard
    validation failure (which would break every existing committed rig.yaml on the next apply).
    Only a PRESENT, malformed value fails: wrong shape or an l1 outside the six-enum. lang and
    framework are open vocabulary. The parse/enum logic lives in :mod:`riglib.stack` (one source).
    """
    if "stack" not in data:
        return
    value = data["stack"]
    if not isinstance(value, str):
        raise ConfigError(
            f"stack must be a string like 'l1/lang[/framework]', got {value!r}",
            schema_path="stack",
        )
    from .stack import STACK_L1, StackError, parse_stack

    try:
        parse_stack(value)
    except StackError as exc:
        raise ConfigError(
            str(exc),
            why="stack is the repo's stack preset (l1/lang[/framework]); it selects by-stack skills",
            fix=f"use l1/lang[/framework] with l1 in {list(STACK_L1)} "
            "(e.g. mobile/swift/swiftui, frontend/ts/react, backend/python)",
            schema_path="stack",
        ) from exc


def _validate_version(data: dict[str, Any]) -> None:
    version = data.get("version", 1)
    # bool is an int subclass and True == 1, so guard it explicitly — `version: true` is a typo,
    # not v1 (this also blocks `rig config set version true` from coercing past the check).
    if isinstance(version, bool) or not isinstance(version, int):
        raise ConfigError(
            f"version must be an int, got {version!r}",
            why="version is the config schema version (only 1 is supported)",
            fix="set `version: 1`",
            schema_path="version",
        )
    if version != 1:
        raise ConfigError(
            f"unsupported config version {version} (this rig supports v1)",
            fix="set `version: 1`",
            schema_path="version",
        )


def _validate_defaults(data: dict[str, Any]) -> None:
    defaults = data.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ConfigError("defaults must be a mapping", schema_path="defaults")
    reject_unknown_keys(defaults, "defaults")
    on_conflict = defaults.get("on_conflict", "backup")
    if on_conflict not in VALID_ON_CONFLICT:
        raise ConfigError(
            f"defaults.on_conflict must be one of {sorted(VALID_ON_CONFLICT)}, "
            f"got {on_conflict!r}",
            fix=f"use one of: {', '.join(sorted(VALID_ON_CONFLICT))}",
            schema_path="defaults.on_conflict",
        )


def _validate_block_shapes(data: dict[str, Any]) -> None:
    for cat in VALID_CATEGORIES:
        if cat in data and not isinstance(data[cat], dict):
            raise ConfigError(f"category '{cat}' must be a mapping", schema_path=cat)
    for project_block in ("scripts", "dev"):
        if project_block in data and not isinstance(data[project_block], dict):
            raise ConfigError(f"{project_block} must be a mapping", schema_path=project_block)


def _run_blocks(data: dict[str, Any], blocks: tuple[tuple[str, BlockValidator], ...]) -> None:
    for key, validator in blocks:
        validator(data.get(key, {}))


def validate(data: dict[str, Any]) -> None:
    """Fail-closed schema validation. Raises :class:`ConfigError` on any violation.

    The accepted key set + the schema PATH on each error come from :mod:`riglib.config_schema`
    (the SAME registry ``schema/rig.schema.json`` is generated from), so a hand-edit, a
    ``rig config set``, and the editor all agree on what is valid. The roadmap's "reject an
    unknown key or bad value with a clear error + the schema path" is implemented here.
    """
    if not isinstance(data, dict):
        raise ConfigError("config root must be a mapping", schema_path="")

    # `scope` was removed (the two layers cascade by LOCATION — a repo rig.yaml is repo-scoped,
    # the global config is global). It is tolerated (whitelisted in the gate) so existing
    # committed rig.yaml files don't break before they're cleaned up.
    reject_unknown_keys(data, "")
    _validate_version(data)
    _validate_defaults(data)
    _validate_block_shapes(data)
    validate_stack(data)
    _run_blocks(data, _BLOCKS_BEFORE_LINT)
    from . import config_lint_blocks

    config_lint_blocks.validate_layer(data, "config")
    _run_blocks(data, _BLOCKS_AFTER_LINT)
