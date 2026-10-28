"""Shared builders for the action-drift test files."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from riglib.actions import run_plan
from riglib.catalog import Catalog
from riglib.config import LoadedConfig
from riglib.plan import Action, InstallPlan, build


def _user_settings() -> Path:
    # computed per call, NOT a module constant: the autouse fixture monkeypatches HOME per test,
    # so a constant captured at import time would point at the real ~/.claude and break isolation.
    return Path(os.path.expanduser("~/.claude/settings.json"))


def _full_cfg(repo_root: Path, source: Path) -> LoadedConfig:
    return LoadedConfig(
        data={
            "agent_tools_source": str(source),
            "defaults": {
                "skills_target": str(repo_root / "skills-out"),
                "hooks_target": str(repo_root / "hooks-out"),
                "ci_target": str(repo_root / ".github/workflows"),
                "mcp_target": str(repo_root / "mcp-out"),
                "on_conflict": "backup",
            },
            "skills": {
                "universal": {"all": True},
                "by_type": {"enable": ["cli"]},
                # keep the harness skill-link target inside the repo so tests stay hermetic
                # (the real default is ~/.claude/skills); dedicated link tests below isolate HOME.
                "harness_skill_dir": str(repo_root / "harness-skills"),
            },
            "agent_hooks": {"all": True},
            "ci": {"items": {"codeql": {"enabled": True, "variant": "selfgate"}, "secret-scan": {"enabled": True}}},
            "mcp": {"items": {"fake-mcp": {"enabled": True, "command": "fake-mcp --serve"}}},
        },
        repo_root=repo_root,
    )


def _dispatcher_cfg(fake_agent_tools, repo, home, fragments=None):
    """A minimal LoadedConfig with ONLY the dispatcher enabled (shared by the
    pre-push fragment tests below)."""
    disp = {
        "enabled": True,
        "dir": str(home / "ghd"),
        "runner": str(home / "run-global-hooks"),
        "set_global_hooks_path": False,
        "install_local_retrofit_script": False,
    }
    if fragments is not None:
        disp["fragments"] = fragments
    return LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": disp},
        },
        repo_root=repo,
    )


def _skill_link_cfg(repo: Path, source: Path, *, harness_dir: Path | None,
                    skills_target: Path | None = None, harness_link=None) -> LoadedConfig:
    skills: dict = {"universal": {"enable": ["naming"], "all": False}, "by_type": {}}
    if harness_dir is not None:
        skills["harness_skill_dir"] = str(harness_dir)
    if harness_link is not None:
        skills["harness_link"] = harness_link
    return LoadedConfig(
        data={
            "agent_tools_source": str(source),
            "defaults": {"skills_target": str(skills_target or repo / "skills-out"), "on_conflict": "backup"},
            "skills": skills,
            "agent_hooks": {"enabled": False}, "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
        },
        repo_root=repo,
    )


def _harness_skill_cfg(repo: Path, source: Path, kind: str) -> LoadedConfig:
    """A config that pins ``harness.kind`` and lets the PER-HARNESS default discovery dir resolve.

    No ``harness_skill_dir`` override: the skill-link dir (or the lack of one, for an
    instruction-file harness) comes purely from the harness kind — exactly what these tests
    exercise. HOME must be isolated by the caller (the default dirs are HOME-relative).
    """
    return LoadedConfig(
        data={
            "agent_tools_source": str(source),
            "defaults": {"skills_target": str(repo / "skills-out"), "on_conflict": "backup"},
            "skills": {"universal": {"enable": ["naming"], "all": False}, "by_type": {}},
            "harness": {"kind": kind},
            "agent_hooks": {"enabled": False}, "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            # default-on areas that would otherwise try to reach HOME / a github remote — off, to
            # keep these unit tests hermetic and focused on the skill-discovery surface.
            "agents_md": {"enabled": False}, "github": {"ruleset": {"enabled": False},
            "merge": {"enabled": False}, "ghas": {"enabled": False}, "actions": {"enabled": False},
            "browser": {"enabled": False}}, "tg_ctl": {"enabled": False},
            "gitignore": {"enabled": False}, "ship_delegator": {"enabled": False},
            "permissions": {"enabled": False},
        },
        repo_root=repo,
    )


def _harness_cfg(repo: Path, source: Path, **harness) -> LoadedConfig:
    return LoadedConfig(
        data={
            "agent_tools_source": str(source),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "harness": {"kind": "claude-code", "settings_path": str(repo / ".claude/settings.json"), **harness},
        },
        repo_root=repo,
    )


def _auto_harness_cfg(repo: Path, source: Path, **harness) -> LoadedConfig:
    """Like ``_harness_cfg`` but auto_mode → USER-scope write (no repo settings_path).

    Reuses the shared baseline so the two helpers can't drift; drops the ``settings_path`` that
    ``_harness_cfg`` forces in (auto writes to ~/.claude, not the repo) and turns ship_delegator
    off so it never creates repo/.claude and muddies the user-scope assertions.
    """
    cfg = _harness_cfg(repo, source, auto_mode=True, **harness)
    cfg.data["harness"].pop("settings_path", None)
    cfg.data["ship_delegator"] = {"enabled": False}
    return cfg


def _bash_rule_matches(rule: str, command: str) -> bool:
    # Only what the deny set relies on: `*` matches any characters (including `/` and spaces).
    inner = rule.removeprefix("Bash(").removesuffix(")")
    pattern = ".*".join(re.escape(part) for part in inner.split("*"))
    return re.fullmatch(pattern, command) is not None


def _bridge_cfg(repo_root: Path, source: Path, *, settings_path: Path,
                hook_bridge: dict | None = None,
                kind: str = "claude-code",
                on_conflict: str | None = None) -> LoadedConfig:
    harness: dict = {"kind": kind, "auto_mode": True,
                     "settings_path": str(settings_path)}
    if hook_bridge is not None:
        harness["hook_bridge"] = hook_bridge
    data = {
        "agent_tools_source": str(source),
        "skills": {"enabled": False},
        "agent_hooks": {"all": True},
        "ci": {"enabled": False},
        "mcp": {"enabled": False},
        "harness": harness,
    }
    if on_conflict is not None:
        data["defaults"] = {"on_conflict": on_conflict}
    return LoadedConfig(
        data=data,
        repo_root=repo_root,
    )


def _bridge_results(report):
    return [r for r in report.results if r.action.kind == "register_hook_bridge"]


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, timeout=10)


def _read_toml(path: Path) -> dict:
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
        import tomli as tomllib  # type: ignore[no-redef]

    return tomllib.loads(path.read_text(encoding="utf-8"))


def _codex_bridge_action(lib_dir: Path, *, extra_stop_hooks=None) -> Action:
    options = {"kind": "codex", "lib_dir": str(lib_dir), "module": "codex_hook_bridge"}
    if extra_stop_hooks is not None:
        options["extra_stop_hooks"] = extra_stop_hooks
    return Action(
        kind="register_hook_bridge",
        category="harness",
        item="hook-bridge",
        source=lib_dir,
        target=lib_dir / "config.toml",
        options=options,
    )


def _plant_file(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _plant_tg_ctl_script(bin_path: Path) -> None:
    """Create a stand-in tg-ctl script file so the fold-in's fail-closed existence check passes."""
    _plant_file(bin_path, "#!/usr/bin/env bun\n")


def _plant_bun_binary(bin_path: Path) -> None:
    """Create a stand-in, EXECUTABLE bun binary (the fold-in's fail-closed check requires the
    execute bit, not just existence) — NEVER a real absolute system path like
    ``/opt/homebrew/bin/bun``, which a test must not read or write."""
    _plant_file(bin_path, "#!/bin/sh\n")
    bin_path.chmod(0o755)


def _codex_bridge_plan(
    fake_agent_tools: Path, tmp_path: Path, *, tg_ctl: dict | None = None,
    kind: str = "codex", install_tg_ctl_script: bool = True, install_bun: bool = True,
) -> tuple[InstallPlan, Path, Path]:
    """Build a hook-bridge plan for the fold-in tests: a fresh repo + (by default) a planted
    tg-ctl script AND a planted bun binary at the default HOME paths, with an optional
    `tg_ctl:` config override that already points `bun_path` at the planted binary.

    ``install_tg_ctl_script``/``install_bun`` are independent flags (both default True) — e.g.
    a custom `tg_ctl_path` test plants its own script elsewhere but still needs a real bun
    planted at the default path.

    Returns ``(plan, settings_path, bun_path)`` so callers that need to inspect/apply the
    written file, or override `tg_ctl` while still using the planted bun path, can do so
    without re-deriving these paths.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    home = Path(os.path.expanduser("~"))
    bun_path = home / "bun-bin" / "bun"
    if install_tg_ctl_script:
        _plant_tg_ctl_script(home / ".files" / "bin" / "tg-ctl")
    if install_bun:
        _plant_bun_binary(bun_path)
    rel = Path(".codex") / "config.toml" if kind == "codex" else Path(".claude") / "settings.json"
    settings = repo / rel
    cfg = _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind=kind,
                       hook_bridge={"enabled": True})
    cfg.data["tg_ctl"] = {"bun_path": str(bun_path), **(tg_ctl or {})}
    cat = Catalog.scan(str(fake_agent_tools))
    return build(cfg, cat, project_type="unknown"), settings, bun_path


def _codex_bridge_action_from(plan: InstallPlan):
    return next(a for a in plan.actions if a.kind == "register_hook_bridge")


def _omp_wrapper_cfg(repo: Path, source: Path, extension: Path, *, python: str | None, on_conflict: str) -> LoadedConfig:
    hook_bridge: dict = {"enabled": True}
    if python is not None:
        hook_bridge["python"] = python
    return LoadedConfig(
        data={
            "agent_tools_source": str(source),
            "skills": {"enabled": False},
            "agent_hooks": {"all": True},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "harness": {"kind": "omp", "settings_path": str(extension), "hook_bridge": hook_bridge},
            "defaults": {"on_conflict": on_conflict},
        },
        repo_root=repo,
        repo_path=repo / "rig.yaml",
        layers=[f"repo:{repo / 'rig.yaml'}"],
    )


def _applied_bridge_settings(fake_agent_tools, tmp_path) -> tuple:
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".claude" / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_bridge_cfg(repo, fake_agent_tools, settings_path=settings), cat, project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    return plan, settings


def _managed_bridge_hooks(data: dict) -> list[dict]:
    return [
        hk
        for blocks in data["hooks"].values()
        for block in blocks
        for hk in block["hooks"]
        if "cc_hook_bridge" in str(hk.get("command", ""))
    ]


def _rewrite_managed_timeouts(settings: Path, value) -> None:
    """Set (or, for the sentinel ``...``, delete) the timeout on every managed bridge hook."""
    data = json.loads(settings.read_text())
    for hk in _managed_bridge_hooks(data):
        if value is ...:
            hk.pop("timeout", None)
        else:
            hk["timeout"] = value
    settings.write_text(json.dumps(data, indent=2) + "\n")


def _matcher_blocks(data: dict, event: str, matcher: str) -> list[dict]:
    return [b for b in data["hooks"][event] if b.get("matcher") == matcher]


def _matcher_commands(data: dict, event: str, matcher: str) -> list[str]:
    return [hk["command"] for b in _matcher_blocks(data, event, matcher) for hk in b["hooks"]]


def _without_matcher(data: dict, matcher: str) -> list[dict]:
    return [b for b in data["hooks"]["PreToolUse"] if b.get("matcher") != matcher]
