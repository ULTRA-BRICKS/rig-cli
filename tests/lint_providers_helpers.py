"""Builders shared by the lint provider tests: in-memory discovery facts plus a real tmp repo for tool resolution."""
from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from riglib.lint_discovery_model import (
    ConfiguredTool,
    Discovery,
    Evidence,
    PackageManager,
    ProjectScope,
    ProjectScript,
    TypeScriptInfo,
)

from riglib import lint_providers as lp
from riglib.lint_run_config import DEFAULTS, LintConfig

JS, TS, PY = "javascript", "typescript", "python"


def tool(name: str, role: str, *, declared: bool = True, config: tuple[str, ...] = (),
         options: Mapping[str, str] | None = None, field: bool = False, scripts: tuple[str, ...] = ()) -> ConfiguredTool:
    evidence = (Evidence("package.json", "field", name),) if field else ()
    return ConfiguredTool(name, role, declared, config, scripts, evidence, options or {})  # type: ignore[arg-type]


def script(name: str, roles: tuple[str, ...] = ("lint",), *, runner: str = "package.json", rig_lint: bool = False,
           writes: bool = False, covers: tuple[str, ...] = (), runs: tuple[str, ...] = (),
           unsafe: tuple[str, ...] = ()) -> ProjectScript:
    return ProjectScript(runner, name, f"run {name}", (), rig_lint, writes, roles, covers, runs, unsafe)  # type: ignore[arg-type]


def pm(name: str, ecosystem: str = "js", root: str = ".") -> PackageManager:
    return PackageManager(ecosystem, name, "lockfile", None, root)  # type: ignore[arg-type]


def ts_info(*, spec: str | None = "^5.0.0", major: int | None = 5, tsconfigs: tuple[str, ...] = ("tsconfig.json",)
            ) -> TypeScriptInfo:
    return TypeScriptInfo(spec, f"{major}.0.0" if major else None, major, tsconfigs)


def scope(rel: str = ".", languages: tuple[str, ...] = (TS,), *, tools: tuple[ConfiguredTool, ...] = (),
          scripts: tuple[ProjectScript, ...] = (), manager: PackageManager | None = None,
          typescript: TypeScriptInfo | None = None, files: Mapping[str, tuple[str, ...]] | None = None,
          frameworks: tuple[str, ...] = (), other_managers: tuple[PackageManager, ...] = ()) -> ProjectScope:
    return ProjectScope(rel, languages, manager, tools, scripts, typescript, files or {}, (), other_managers, frameworks)


def discovery(root: Path, *scopes: ProjectScope) -> Discovery:
    return Discovery(root, tuple(sorted(scopes, key=lambda s: s.rel)), (), (), (), 0, True)


def tool_env(root: Path, *, which: Mapping[str, str] | None = None, system: str = "linux", machine: str = "x86_64",
             depth: int = 0, environ: Mapping[str, str] | None = None) -> lp.ToolEnv:
    found = dict(which or {})
    return lp.ToolEnv(root, found.get, system, machine, (22, 18, 0), environ or {}, depth)


def config(**fields: object) -> LintConfig:
    return replace(DEFAULTS, **fields)  # type: ignore[arg-type]


def install(root: Path, rel: str) -> Path:
    """Create an executable stand-in binary at ``root/rel``."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def plan(root: Path, *scopes: ProjectScope, cfg: LintConfig | None = None, which: Mapping[str, str] | None = None,
         **env_kwargs: object) -> dict[str, lp.PlannedCheck]:
    disc = discovery(root, *scopes)
    checks = lp.plan_checks(disc, cfg or DEFAULTS, tool_env(root, which=which, **env_kwargs))  # type: ignore[arg-type]
    return {c.id: c for c in checks}


def runnable(checks: Mapping[str, lp.PlannedCheck]) -> set[str]:
    return {i for i, c in checks.items() if c.unavailable is None}


def state(check: lp.PlannedCheck) -> tuple[str, str] | None:
    return None if check.unavailable is None else (check.unavailable.state, check.unavailable.reason)


def bin_path(root: Path, name: str, rel: str = ".") -> str:
    base = root if rel == "." else root / rel
    return str(base / "node_modules" / ".bin" / name)


def make_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    return root


def symlink(target: Path, link: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, link)
