"""Disk→config "extras" scanners: skills / CI workflows / MCP servers / agent hooks on disk that config does not declare."""

from __future__ import annotations

import json
from pathlib import Path

from .provenance import (
    hook_provenance,
    mcp_provenance,
    skill_provenance,
    undeclared_hook_detail,
    undeclared_skill_detail,
    workflow_provenance,
)
from .drift_model import DriftItem, DriftReport, KnownItem


def _extras_hooks(
    declared_hook_dirs: dict[Path, set[str]], report: DriftReport, known: set[str], catalog: dict[str, tuple[Path, ...]] | None,
) -> None:
    """Flag agent-hook descriptors on disk (``*.json``) not declared in config.

    Dispatcher *fragments* are intentionally NOT flagged: ``global-hooks.d`` is a shared
    drop-in namespace where other tools' fragments legitimately coexist, so undeclared
    fragments there are expected, not drift. A descriptor another installer marked, an
    agent-tools ops-installer hook, or one listed under ``agent_hooks.known`` is filed as known.
    """
    for hook_dir, declared in declared_hook_dirs.items():
        if not hook_dir.is_dir():
            continue
        for entry in sorted(hook_dir.glob("*.json")):
            if entry.name in declared:
                continue
            prov = hook_provenance(entry, known=known, catalog=catalog)
            if prov is not None:
                report.known.append(KnownItem("agent_hooks", entry.stem, entry, prov.kind, entry.stem, prov.by))
            else:
                report.items.append(
                    DriftItem("extra", "agent_hooks", entry.stem, entry, undeclared_hook_detail(entry, catalog))
                )


def _skill_entries(skills_dir: Path, declared: set[str]) -> list[Path]:
    """Undeclared dirs under ``skills_dir`` that look like skills (have a SKILL.md)."""
    if not skills_dir.is_dir():
        return []
    return [
        entry
        for entry in sorted(skills_dir.iterdir())
        if entry.is_dir()
        and not entry.name.startswith(".")
        and entry.name not in declared
        and (entry / "SKILL.md").is_file()  # only flag things that look like skills
    ]


def _extras_skills(
    declared_skill_dirs: dict[Path, set[str]], report: DriftReport, known: set[str], catalog: dict[str, tuple[Path, ...]] | None,
) -> None:
    """Flag skill dirs on disk not declared in config; a skill whose origin rig can name (an
    installer marker / blurb, the ecosystem allowlist, ``skills.known``, a catalog item another
    repo's config selects) is filed as known."""
    for skills_dir, declared in declared_skill_dirs.items():
        for entry in _skill_entries(skills_dir, declared):
            prov = skill_provenance(entry, skills_dir, known=known, catalog=catalog)
            if prov is not None:
                report.known.append(KnownItem("skills", entry.name, entry, prov.kind, entry.name, prov.by))
            else:
                report.items.append(
                    DriftItem("extra", "skills", entry.name, entry, undeclared_skill_detail(entry.name, catalog))
                )


def _workflow_entries(wf_dir: Path, declared: set[str]) -> list[Path]:
    """Undeclared ``.yml``/``.yaml`` regular files under ``wf_dir``."""
    if not wf_dir.is_dir():
        return []
    return [
        entry
        for entry in sorted(wf_dir.iterdir())
        if entry.suffix in (".yml", ".yaml") and entry.is_file() and entry.name not in declared
    ]


def _extras_ci(
    declared_ci_dirs: dict[Path, set[str]], report: DriftReport, known: set[str], catalog: dict[str, tuple[Path, ...]] | None,
) -> None:
    """Flag workflow files not declared in config. Only a workflow named after a catalog CI slot
    can be a rig orphan; any other stem is the repository's own workflow (known). Without catalog
    knowledge (a hand-built plan) every undeclared workflow stays drift."""
    for wf_dir, declared in declared_ci_dirs.items():
        for entry in _workflow_entries(wf_dir, declared):
            prov = workflow_provenance(entry.stem, known=known, catalog=catalog)
            if prov is not None:
                report.known.append(KnownItem("ci", entry.stem, entry, prov.kind, entry.name))
            else:
                report.items.append(
                    DriftItem("extra", "ci", entry.stem, entry, "workflow on disk but not declared in config")
                )


def _mcp_server_names(cf: Path) -> list[str]:
    """Sorted server names in an MCP config file; empty when unreadable or malformed."""
    if not cf.is_file():
        return []
    try:
        data = json.loads(cf.read_text(encoding="utf-8"))
    except ValueError:
        return []
    servers = data.get("mcpServers", {}) if isinstance(data, dict) else {}
    if not isinstance(servers, dict):
        return []
    return sorted(servers)


def _extras_mcp(declared_mcp: dict[Path, set[str]], report: DriftReport, known: set[str]) -> None:
    for cf, declared in declared_mcp.items():
        for name in _mcp_server_names(cf):
            if name in declared:
                continue
            prov = mcp_provenance(name, known=known)
            if prov is not None:
                report.known.append(KnownItem("mcp", name, cf, prov.kind, name))
            else:
                report.items.append(
                    DriftItem("extra", "mcp", name, cf, "MCP entry registered but not declared in config")
                )
