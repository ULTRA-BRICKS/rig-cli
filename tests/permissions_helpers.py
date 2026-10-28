"""Shared helpers for the permissions test modules (test_permissions_*.py)."""

from __future__ import annotations

from pathlib import Path

from riglib.config import LoadedConfig


_DESTRUCTIVE_RAW_COMMANDS = ("rm", "sudo", "dd", "kill", "killall", "pkill", "shutdown", "reboot", "mkfs")

# ── plan + apply (claude-code array form) ───────────────────────────────────────────
def _cfg(repo: Path, source: Path, settings: Path, **perm) -> LoadedConfig:
    return LoadedConfig(
        data={
            "agent_tools_source": str(source),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            # harness present but no auto-mode write to the same file (use a different settings_path
            # for the harness so the two actions don't both target `settings`); here we just disable
            # the harness block entirely to isolate the permissions action.
            "permissions": {"settings_path": str(settings), **perm},
        },
        repo_root=repo,
    )


def _perm_results(report):
    return [r for r in report.results if r.action.category == "permissions"]



# ── opencode-specific (kind override + object-form drift) ───────────────────────────
def _opencode_cfg(repo: Path, source: Path, settings: Path, **perm) -> LoadedConfig:
    # target opencode INDEPENDENTLY via permissions.kind (harness.kind: opencode is rejected by
    # validate; permissions.kind: opencode is the supported way to provision the opencode allowlist).
    return LoadedConfig(
        data={
            "agent_tools_source": str(source),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "permissions": {"kind": "opencode", "settings_path": str(settings), **perm},
        },
        repo_root=repo,
    )

# rig-cli#714: direct Runpod creation goes through runpod-run on every belt.
RUNPOD_MCP_WHOLE_TOOL_DENIES = (
    "mcp__runpod__create-pod",
    "mcp__runpod__create-endpoint",
    "mcp__runpod__create-cluster",
    "mcp__runpod__deploy-hub-repo",
)
RUNPOD_CREATE_ARGV = (
    ("runpodctl", "pod", "create"),
    ("runpodctl", "pods", "create"),
    ("runpodctl", "serverless", "create"),
    ("runpodctl", "sls", "create"),
    ("runpodctl", "cluster", "create"),
    ("runpodctl", "create", "pod"),
    ("runpodctl", "create", "pods"),
    ("flash", "deploy"),
)
