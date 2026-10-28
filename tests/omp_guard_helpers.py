"""Shared helpers and fixtures for the `omp_guard` test modules (split from the original single module)."""
from __future__ import annotations

from pathlib import Path

from riglib.actions import run_plan
from riglib.catalog import Catalog
from riglib.config import LoadedConfig
from riglib.plan import build


def _omp_cfg(repo: Path, source: Path, **perm) -> LoadedConfig:
    """A config with harness.kind: omp so the permissions feature fans out to it."""
    data = {
        "agent_tools_source": str(source),
        "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
        "ci": {"enabled": False}, "mcp": {"enabled": False},
        "git_hooks": {"dispatcher": {"enabled": False}},
        "harness": {"enabled": False, "kind": "omp"},
    }
    if perm:
        data["permissions"] = perm
    return LoadedConfig(data=data, repo_root=repo)


def _instr_cfg(repo: Path, source: Path, kind: str) -> LoadedConfig:
    cfg = _omp_cfg(repo, source)
    cfg.data["harness"]["kind"] = kind
    return cfg


def _pin_home(monkeypatch, home: Path) -> None:
    """The instruction-file targets are XDG-aware (~/.config maps to $XDG_CONFIG_HOME), so a
    test pinning HOME must pin XDG under it too or the action lands in conftest's isolated home."""
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))


# ── install_harness_guard (apply) ────────────────────────────────────────────────────
def _guard_path(home: Path) -> Path:
    return home / ".omp" / "agent" / "extensions" / "rig-permissions-guard.ts"


def _apply_guard(fake_agent_tools, tmp_path, monkeypatch, on_conflict="backup"):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    cfg = _omp_cfg(repo, fake_agent_tools)
    cfg.data["defaults"] = {"on_conflict": on_conflict}
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    report = run_plan(plan)
    res = [r for r in report.results if r.action.kind == "install_harness_guard"][0]
    return home, res, plan


# ── provision_harness_approval (apply) ───────────────────────────────────────────────
def _config_yml(home: Path) -> Path:
    return home / ".omp" / "agent" / "config.yml"


def _apply_approval(fake_agent_tools, tmp_path, monkeypatch, on_conflict="backup"):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    cfg = _omp_cfg(repo, fake_agent_tools)
    cfg.data["defaults"] = {"on_conflict": on_conflict}
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    report = run_plan(plan)
    res = [r for r in report.results if r.action.kind == "provision_harness_approval"][0]
    return home, res, plan


# ── provision_instruction_policy (apply) ─────────────────────────────────────────────
def _apply_policy(fake_agent_tools, tmp_path, monkeypatch, kind="pi", on_conflict="backup"):
    home = tmp_path / "home"
    _pin_home(monkeypatch, home)
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    cfg = _instr_cfg(repo, fake_agent_tools, kind)
    cfg.data["defaults"] = {"on_conflict": on_conflict}
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    report = run_plan(plan)
    res = [r for r in report.results if r.action.kind == "provision_instruction_policy"][0]
    return home, res


def _policy_path(home: Path, kind: str) -> Path:
    rel = ".pi/agent/AGENTS.md" if kind == "pi" else ".commandcode/AGENTS.md"
    return home / rel


def _bun_bin() -> str | None:
    import shutil

    found = shutil.which("bun")
    if found:
        return found
    fallback = Path.home() / ".bun" / "bin" / "bun"
    return str(fallback) if fallback.is_file() else None
