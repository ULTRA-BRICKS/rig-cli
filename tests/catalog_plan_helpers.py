"""Shared builders for the catalog/plan test files."""

from __future__ import annotations

from pathlib import Path

from riglib.config import LoadedConfig


def _cfg(data: dict, repo_root: Path) -> LoadedConfig:
    return LoadedConfig(data=data, repo_root=repo_root)


def _harness_action(plan):
    return next((a for a in plan.actions if a.category == "harness"), None)


def _bridge_action(plan):
    return next((a for a in plan.actions if a.kind == "register_hook_bridge"), None)


def _pinned_repo_cfg(tmp_path, global_harness, **extra):
    return LoadedConfig(
        data={
            "skills": {"enabled": False},
            "agent_hooks": {"all": True},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "harness": {"kind": "claude-code"},
            **extra,
        },
        repo_root=tmp_path,
        global_data={"harness": global_harness},
    )
