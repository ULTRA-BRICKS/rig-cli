"""Validation of the ``project_tools`` and ``ecosystem_readme`` blocks.

Both blocks write committed repo-local carriers (Haft/Serena/Sverklo registrations, the README
``## Ecosystem`` section), so they share one module and the repo-containment predicate from the
linters validator. Separate from ``config.py`` to keep that file under the lint size gate.
"""

from __future__ import annotations

from typing import Any

from .config_checks import check_bool, check_str, reject_unknown_keys, validate_string_list
from .config_errors import ConfigError
from .config_validate_linters import linter_path_escapes_repo
from .project_tools import (
    HAFT_KEYS,
    HAFT_WORKFLOW_KEYS,
    HAFT_WORKFLOW_MODES,
    SERENA_KEYS,
    SVERKLO_KEYS,
)


def _optional_tool_block(pt: dict[str, Any], name: str, allowed: Any) -> dict[str, Any] | None:
    """Return ``project_tools.<name>`` after mapping + unknown-key checks (None when absent/null)."""
    block = pt.get(name, {})
    if block is None:
        return None
    path = f"project_tools.{name}"
    if not isinstance(block, dict):
        raise ConfigError(f"{path} must be a mapping", schema_path=path)
    _reject_unlisted(block, path, allowed)
    return block


def _reject_unlisted(block: dict[str, Any], path: str, allowed: Any) -> None:
    for key in block:
        if key not in allowed:
            raise ConfigError(
                f"unknown {path} key {key!r} (expected one of: {', '.join(sorted(allowed))})",
                schema_path=path,
            )


def _validate_haft_workflow(haft: dict[str, Any]) -> None:
    workflow = haft.get("workflow", {})
    if workflow is None:
        return
    if not isinstance(workflow, dict):
        raise ConfigError(
            "project_tools.haft.workflow must be a mapping",
            schema_path="project_tools.haft.workflow",
        )
    _reject_unlisted(workflow, "project_tools.haft.workflow", HAFT_WORKFLOW_KEYS)
    mode = workflow.get("mode")
    if mode is not None and mode not in HAFT_WORKFLOW_MODES:
        raise ConfigError(
            f"project_tools.haft.workflow.mode must be one of {list(HAFT_WORKFLOW_MODES)}, got {mode!r}",
            fix=f"use one of: {', '.join(HAFT_WORKFLOW_MODES)}",
            schema_path="project_tools.haft.workflow.mode",
        )
    for key in ("require_decision", "require_verify", "allow_autonomy"):
        check_bool(workflow, key, f"project_tools.haft.workflow.{key}")


def _validate_haft(pt: dict[str, Any]) -> None:
    haft = _optional_tool_block(pt, "haft", HAFT_KEYS)
    if haft is None:
        return
    check_bool(haft, "enabled", "project_tools.haft.enabled")
    check_bool(haft, "codex_mcp", "project_tools.haft.codex_mcp")
    check_str(haft, "project_name", "project_tools.haft.project_name")
    check_str(haft, "project_id", "project_tools.haft.project_id")
    _validate_haft_workflow(haft)


def _validate_serena(pt: dict[str, Any]) -> None:
    serena = _optional_tool_block(pt, "serena", SERENA_KEYS)
    if serena is None:
        return
    check_bool(serena, "enabled", "project_tools.serena.enabled")
    check_bool(serena, "read_only", "project_tools.serena.read_only")
    check_str(serena, "project_name", "project_tools.serena.project_name")
    for key in ("languages", "ignored_paths"):
        if key in serena:
            validate_string_list(serena[key], f"project_tools.serena.{key}")


def _validate_sverklo(pt: dict[str, Any]) -> None:
    sverklo = _optional_tool_block(pt, "sverklo", SVERKLO_KEYS)
    if sverklo is None:
        return
    for key in ("enabled", "register", "reindex"):
        check_bool(sverklo, key, f"project_tools.sverklo.{key}")


def validate_project_tools(pt: dict[str, Any]) -> None:
    """Validate repo-local project-tool provisioning (Haft, Serena, Sverklo).

    This is distinct from the GLOBAL ``tools`` block: ``tools`` installs personal command-line
    programs, while ``project_tools`` writes committed repo carriers and safe live registrations so
    the project is usable by those tools. The block is default-off unless present in a scaffolded
    ``rig.yaml``. Fail closed on unknown keys and bad scalar/list types.
    """
    if not isinstance(pt, dict):
        raise ConfigError("project_tools must be a mapping", schema_path="project_tools")
    if not pt:
        return
    reject_unknown_keys(pt, "project_tools")
    check_bool(pt, "enabled", "project_tools.enabled")
    _validate_haft(pt)
    _validate_serena(pt)
    _validate_sverklo(pt)


def validate_ecosystem_readme(er: dict[str, Any]) -> None:
    """Validate the rig-managed README ``## Ecosystem`` block (rig-cli#308).

    Default-off (opt-in): an absent or empty block is valid and provisions nothing. ``target``
    (default ``README.md``) must be a relative path that stays inside the repo — the SAME
    containment guard (:func:`linter_path_escapes_repo`) the ``linters.items`` block uses for
    its own repo-relative writes.
    """
    if not isinstance(er, dict):
        raise ConfigError("ecosystem_readme must be a mapping", schema_path="ecosystem_readme")
    if not er:
        return
    reject_unknown_keys(er, "ecosystem_readme")
    check_bool(er, "enabled", "ecosystem_readme.enabled")
    check_str(er, "target", "ecosystem_readme.target")
    target = er.get("target")
    if isinstance(target, str) and target and linter_path_escapes_repo(target):
        raise ConfigError(
            f"ecosystem_readme.target must be a relative path inside the repo, got {target!r}",
            schema_path="ecosystem_readme.target",
        )
