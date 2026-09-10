"""Sync each ``agent-hooks/<name>/blurb.md`` in the agent-tools checkout into the
SessionStart ``.blurbs`` aggregator dir (agent-tools#504).

Background: the SessionStart aggregator hook (installed by every per-tool CLI installer —
``dev``, ``tg``, ``review``, ``draw``, ``research``, ``stt``, ``task``) cats every file under
``~/.agents/skills/.blurbs/*.md`` into a new Claude Code session, so those 7 tools are
proactively known upfront. The agent-tools "umbrella" hooks (``orchestrator-stays-thin``,
``background-subagent-gate``, ...) had NO equivalent: an agent only learns those policies by
tripping the reactive PreToolUse block mid-task. This module closes that gap the same way the
per-tool installers already do it — copy a hook's own ``blurb.md`` (when it ships one) into the
aggregator dir, idempotently.

A hook opts in by adding a ``blurb.md`` next to its ``README.md``; a hook with no ``blurb.md``
is silently skipped (most hooks — e.g. narrow, low-traffic gates — have no reason to announce
themselves upfront in every session, so opt-in keeps the aggregator from bloating).

Reached via :func:`riglib.plan._plan_hook_blurbs` (emits one ``sync_hook_blurbs`` Action per
``rig apply``, gated on ``agent_hooks.enabled``) → :func:`riglib.actions.runner._do_sync_hook_blurbs`
(the effectful caller that invokes :func:`sync_agent_hook_blurbs` below).
"""

from __future__ import annotations

from pathlib import Path


def sync_agent_hook_blurbs(agent_tools_root: Path, blurbs_target: Path) -> list[Path]:
    """Copy each ``agent-hooks/<name>/blurb.md`` under ``agent_tools_root`` into
    ``blurbs_target/<name>.md``, creating ``blurbs_target`` if needed.

    Idempotent: a target whose content already matches the source is left untouched (no
    write, no mtime churn) — mirrors ``_write_if_changed`` in the per-tool CLI installers.
    Returns the list of target paths actually created or updated, sorted by hook name, for
    logging/testing. A hook directory with no ``blurb.md`` is skipped, not an error.
    """
    hooks_dir = agent_tools_root / "agent-hooks"
    if not hooks_dir.is_dir():
        return []

    changed: list[Path] = []
    for hook_dir in sorted(p for p in hooks_dir.iterdir() if p.is_dir()):
        blurb_src = hook_dir / "blurb.md"
        if not blurb_src.is_file():
            continue
        content = blurb_src.read_text(encoding="utf-8")
        target = blurbs_target / f"{hook_dir.name}.md"
        if target.is_file() and target.read_text(encoding="utf-8") == content:
            continue
        blurbs_target.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        changed.append(target)
    return changed
