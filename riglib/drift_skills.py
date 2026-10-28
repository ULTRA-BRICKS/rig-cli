"""Drift checks for skills, AGENTS.md artifacts, MCP registrations and agent-hook descriptors."""

from __future__ import annotations

import json
from pathlib import Path

from .actions import fsutil
from .actions.agent_hook_install import RigPinError, build_hook_descriptor, descriptor_text
from .actions.agents_md import resolve_agents_md
from .actions.managed_blocks import _splice_managed_block
from .actions.mcp import desired_mcp_server_entry
from .actions.skills import skill_harness_link_target
from .plan import Action
from .drift_model import DriftItem, DriftReport


def _check_copy_skill(action: Action, report: DriftReport) -> None:
    if not action.target.exists():
        report.items.append(
            DriftItem("missing", "skills", action.item, action.target, "declared, not on disk")
        )
    elif not fsutil.dirs_identical(action.source, action.target):
        report.items.append(
            DriftItem("modified", "skills", action.item, action.target, "on disk differs from source")
        )


def _check_skill_symlink(
    action: Action, report: DriftReport, *, item_label: str, missing_detail: str, modified_detail_prefix: str,
) -> None:
    """Shared missing/modified/real-dir-left-alone check for an idempotent skill symlink.

    Both :func:`_check_skill_harness_link` (a harness-discovery symlink) and
    :func:`_check_link_skill_plugin` (a plugin-sourced skill's symlink into the skills target)
    maintain the exact same idempotent-symlink contract the runner's shared
    ``_do_link_skill_harness`` handler executes for both action kinds (see
    ``actions/runner.py``'s ``_HANDLERS`` mapping) — only the item label and message wording
    differ per caller, so the check itself lives in exactly one place (code-quality review
    finding, rig-cli#590; mirrors the execution side's own single-handler precedent).

    missing  — no symlink (and no real dir) at the target path: apply creates it.
    modified — a symlink pointing at the WRONG destination: apply re-points it.
    A REAL (non-symlink) dir/file at the path is NOT flagged — it's legitimately not rig's to
    manage (a hand-authored skill, or — for the plugin case — the build()-time catalog-collision
    check's runtime safety net), so reporting it as drift (which apply ignores anyway) would be
    misleading noise.
    """
    link_path, dest = skill_harness_link_target(action)
    if link_path.is_symlink():
        from .actions.skills import _same_link_dest

        try:
            current = link_path.readlink()
        except OSError:
            return
        if not _same_link_dest(link_path, current, dest):
            report.items.append(
                DriftItem("modified", "skills", item_label, link_path,
                          f"{modified_detail_prefix}, expected → {dest}")
            )
        return
    if link_path.exists():
        return
    report.items.append(DriftItem("missing", "skills", item_label, link_path, missing_detail))


def _check_skill_harness_link(action: Action, report: DriftReport) -> None:
    """Flag drift on a skill's harness-discovery symlink — see :func:`_check_skill_symlink`."""
    _check_skill_symlink(
        action, report,
        item_label=f"{action.item} (harness link)",
        missing_detail="skill not symlinked into harness dir (harness won't list it)",
        modified_detail_prefix="harness symlink points elsewhere",
    )


def _check_link_skill_plugin(action: Action, report: DriftReport) -> None:
    """Flag drift on a plugin-sourced skill's symlink into the skills target — see
    :func:`_check_skill_symlink`. This symlink lands at ``skills_target/<name>`` itself (the
    plugin IS the skill's install location, there is no separate copy step), not a harness
    discovery dir."""
    _check_skill_symlink(
        action, report,
        item_label=action.item,
        missing_detail="plugin skill not symlinked into the skills target",
        modified_detail_prefix="plugin skill symlink points elsewhere",
    )


def _check_agents_symlink(action: Action, report: DriftReport) -> None:
    """Flag drift on the AGENTS.md (canonical) + CLAUDE.md (symlink) invariant.

    Switches on the SAME :func:`resolve_agents_md` ``state`` apply uses, so status and apply read
    one classification. Any non-``ok`` state is drift: the desired end-state is one real
    canonical + a correct symlink, and a repo that isn't there is out of sync. As everywhere
    else in rig, ``on_conflict`` governs only whether ``apply`` *reconciles* the drift, not
    whether ``status`` *reports* it — so two identical real files (``converge``) read as drift
    even though an ``on_conflict: skip`` apply would decline to collapse them (same as a
    skip'd modified skill staying visible as drift).
    """
    r = resolve_agents_md(action.target)
    if r.state == "ok":
        return
    if r.state == "create_both":
        report.items.append(
            DriftItem("missing", "agents_md", "symlink", r.canonical_path,
                      "AGENTS.md/CLAUDE.md not present (apply creates canonical + symlink)")
        )
    elif r.state == "create_link":
        report.items.append(
            DriftItem("missing", "agents_md", "symlink", r.link,
                      f"{r.link.name} missing (apply symlinks it → {r.canonical})")
        )
    elif r.state == "converge":
        report.items.append(
            DriftItem("modified", "agents_md", "symlink", r.link,
                      "AGENTS.md and CLAUDE.md are identical real files "
                      "(apply converges to a symlink under on_conflict=backup/overwrite)")
        )
    elif r.state == "conflict":
        report.items.append(
            DriftItem("modified", "agents_md", "symlink", r.link, r.detail)
        )


def _check_agents_md_splice(
    action: Action,
    report: DriftReport,
    *,
    desired: str,
    begin: str,
    end: str,
    missing_file: str,
    missing_block: str,
    conflict: str,
    stale: str,
) -> None:
    """missing/modified mirror the instruction-policy checker (same splice resolver)."""
    from .actions.agents_md import _agents_md_splice_target

    path = _agents_md_splice_target(action.target)
    if path is None or not path.is_file():
        report.items.append(
            DriftItem("missing", "agents_md", action.item, action.target / "AGENTS.md", missing_file)
        )
        return
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            content = fh.read()
    except OSError as exc:
        report.items.append(
            DriftItem("modified", "agents_md", action.item, path, f"cannot read {path}: {exc}")
        )
        return
    _, state, _detail = _splice_managed_block(content, desired, begin, end)
    if state == "ok":
        return
    if state == "create":
        report.items.append(
            DriftItem("missing", "agents_md", action.item, path, missing_block)
        )
    elif state == "conflict":
        report.items.append(
            DriftItem("modified", "agents_md", action.item, path, conflict)
        )
    else:  # update
        report.items.append(
            DriftItem("modified", "agents_md", action.item, path, stale)
        )


def _check_agents_orchestrator(action: Action, report: DriftReport) -> None:
    """Flag drift on the rig-managed orchestrator block in the repo AGENTS.md."""
    from .actions.agents_md import ORCHESTRATOR_BEGIN_MARKER, ORCHESTRATOR_END_MARKER, orchestrator_block_text

    _check_agents_md_splice(
        action, report,
        desired=orchestrator_block_text(),
        begin=ORCHESTRATOR_BEGIN_MARKER,
        end=ORCHESTRATOR_END_MARKER,
        missing_file="AGENTS.md not present (orchestrator splice skips until the symlink action creates it)",
        missing_block="no rig-managed orchestrator block (apply adds it)",
        conflict="unbalanced/misordered rig-managed orchestrator markers — reconcile by hand, then re-run",
        stale="rig-managed orchestrator block is stale (apply rewrites it)",
    )


def _check_agents_autonomous_mode(action: Action, report: DriftReport) -> None:
    """Flag drift on the rig-managed autonomous-mode block in the repo AGENTS.md."""
    from .actions.agents_md import AUTONOMOUS_BEGIN_MARKER, AUTONOMOUS_END_MARKER, autonomous_mode_block_text

    _check_agents_md_splice(
        action, report,
        desired=autonomous_mode_block_text(),
        begin=AUTONOMOUS_BEGIN_MARKER,
        end=AUTONOMOUS_END_MARKER,
        missing_file="AGENTS.md not present (autonomous-mode splice skips until the symlink action creates it)",
        missing_block="no rig-managed autonomous-mode block (apply adds it)",
        conflict="unbalanced/misordered rig-managed autonomous-mode markers — reconcile by hand, then re-run",
        stale="rig-managed autonomous-mode block is stale (apply rewrites it)",
    )


def _mcp_config_file(action: Action) -> Path:
    target = action.target
    return target if target.suffix == ".json" else target / "mcp.json"


def _check_mcp(action: Action, report: DriftReport) -> None:
    command = str(action.options.get("command", "")).strip()
    if not command:
        return
    server_key = str(action.options.get("server") or action.item)
    config_file = _mcp_config_file(action)
    desired = desired_mcp_server_entry(action.options)
    existing = None
    if config_file.is_file():
        try:
            data = json.loads(config_file.read_text(encoding="utf-8"))
            servers = data.get("mcpServers", {}) if isinstance(data, dict) else {}
            existing = servers.get(server_key) if isinstance(servers, dict) else None
        except ValueError:
            existing = None
    if existing is None:
        report.items.append(
            DriftItem("missing", "mcp", server_key, config_file, "MCP entry not registered")
        )
    elif existing != desired:
        report.items.append(
            DriftItem("modified", "mcp", server_key, config_file, "MCP entry differs from config (command/args/env)")
        )


def _check_agent_hook(action: Action, report: DriftReport) -> None:
    descriptor = action.target / (action.options.get("descriptor") or "")
    if not descriptor.exists():
        report.items.append(
            DriftItem("missing", "agent_hooks", action.item, descriptor, "descriptor not installed")
        )
        return
    # content comparison: an edited cmd/on_error (or a config on_error change) is drift —
    # apply would replace it. Build the expected descriptor the same way the install does.
    try:
        spec, _ = build_hook_descriptor(action)
        expected = descriptor_text(spec)
    except RigPinError as exc:
        report.items.append(
            DriftItem("modified", "agent_hooks", action.item, descriptor, f"cannot verify the rig pin: {exc}")
        )
        return
    except (OSError, ValueError):
        return  # unreadable source — the install action surfaces this, not drift
    if descriptor.read_text(encoding="utf-8") != expected:
        report.items.append(
            DriftItem("modified", "agent_hooks", action.item, descriptor, "descriptor on disk differs from config")
        )
