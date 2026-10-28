"""Shared fixtures for the bootstrap tests: a tmp git repo, shim package managers on a controlled PATH, a call log.

The shims log argv, edit the manifest, write the lockfile and create the binaries a real install would, so
no test touches the network, a real package manager or the real HOME.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from riglib.catalog import Catalog
from riglib.lint_bootstrap import BootstrapPlan, build_bootstrap_plan
from riglib.lint_discovery import discover
from riglib.lint_providers import PINS, ToolEnv, bootstrap_needs
from riglib.lint_run_config import resolve

OXLINT = PINS["oxlint"]
OXFMT = PINS["oxfmt"]
RUFF = PINS["ruff"]
NO_SLOP = {"groups": {"anti-slop": False}}

_SHIM = r'''#!%(python)s
import json, os, pathlib, sys

name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
if os.environ.get("SHIM_LOG"):
    with open(os.environ["SHIM_LOG"], "a") as fh:
        fh.write(json.dumps([name, *args, os.getcwd()]) + "\n")
mode = os.environ.get("SHIM_MODE", "ok")
if mode == "fail":
    print("boom: registry unreachable", file=sys.stderr)
    sys.exit(3)

def make_bin(base, pkg):
    target = pathlib.Path(base) / pkg.rsplit("/", 1)[-1]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o755)


if "--prefix" in args:  # tool-cache install: packages land in <prefix>/node_modules, nothing in the repo
    prefix = pathlib.Path(args[args.index("--prefix") + 1])
    for spec in [a for a in args if not a.startswith("-") and a.rsplit("@", 1)[0] not in ("", a)]:
        pkg = spec.rsplit("@", 1)[0]
        (prefix / "node_modules" / pkg).mkdir(parents=True, exist_ok=True)
        (prefix / "node_modules" / pkg / "package.json").write_text("{}")
        make_bin(prefix / "node_modules" / ".bin", pkg)
    sys.exit(0)

LOCKS = {"npm": "package-lock.json", "pnpm": "pnpm-lock.yaml", "yarn": "yarn.lock", "bun": "bun.lock",
         "uv": "uv.lock", "poetry": "poetry.lock", "pdm": "pdm.lock"}
cwd = pathlib.Path.cwd()
specs = [a for a in args if not a.startswith("-") and (a.rsplit("@", 1)[0] not in ("", a) or "==" in a)]
python = name in ("uv", "poetry", "pdm")


if specs and python:
    lines = []
    for spec in specs:
        pkg, ver = spec.split("==")
        ver = "9.9.9" if mode == "wrong-version" else ver
        lines.append('"%%s==%%s"' %% (pkg, ver))
        make_bin(cwd / ".venv" / "bin", pkg)
        with open(cwd / LOCKS[name], "a") as fh:
            fh.write('[[package]]\nname = "%%s"\nversion = "%%s"\n' %% (pkg, ver))
    with open(cwd / "pyproject.toml", "a") as fh:
        fh.write("\n[dependency-groups]\ndev = [%%s]\n" %% ", ".join(lines))
elif specs:
    manifest = json.loads((cwd / "package.json").read_text())
    dev = manifest.setdefault("devDependencies", {})
    for spec in specs:
        pkg, ver = spec.rsplit("@", 1)
        dev[pkg] = "9.9.9" if mode == "wrong-version" else ver
        make_bin(cwd / "node_modules" / ".bin", pkg)
    (cwd / "package.json").write_text(json.dumps(manifest, indent=2) + "\n")
    if mode != "no-lock":
        (cwd / LOCKS[name]).write_text("lock\n")
else:
    if name in LOCKS and not python and not (cwd / LOCKS[name]).exists():
        print("no lockfile", file=sys.stderr)
        sys.exit(1)
    manifest = json.loads((cwd / "package.json").read_text()) if (cwd / "package.json").exists() else {}
    for pkg in {**manifest.get("devDependencies", {}), **manifest.get("dependencies", {})}:
        make_bin(cwd / "node_modules" / ".bin", pkg)
    if python:
        make_bin(cwd / ".venv" / "bin", "ruff")
'''


# ── fixtures ───────────────────────────────────────────────────────────────────────


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """A git repo, a shim bin dir first on PATH, and a recorded call log; CI env cleared."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / ".gitignore").write_text("node_modules/\n.venv/\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.jsonl"
    for var in ("CI", "GITHUB_ACTIONS", "SHIM_MODE", "XDG_CACHE_HOME", "RIG_LINT_TOOL_CACHE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SHIM_LOG", str(log))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return Sandbox(repo, bin_dir, log)


class Sandbox:
    def __init__(self, repo: Path, bin_dir: Path, log: Path) -> None:
        self.repo, self.bin_dir, self.log = repo, bin_dir, log

    def install_managers(self, *names: str) -> None:
        for name in names:
            path = self.bin_dir / name
            path.write_text(_SHIM % {"python": sys.executable}, encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def write(self, rel: str, text: str) -> Path:
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def package_json(self, extra: Mapping[str, object] | None = None, rel: str = "package.json") -> None:
        self.write(rel, json.dumps({"name": "demo", "version": "1.0.0", **(extra or {})}, indent=2) + "\n")

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def tree(self) -> dict[str, bytes]:
        return {
            p.relative_to(self.repo).as_posix(): p.read_bytes() if p.is_file() else b"<dir>"
            for p in sorted(self.repo.rglob("*"))
            if ".git" not in p.relative_to(self.repo).parts
        }

    def env(self, node: tuple[int, int, int] | None = (22, 18, 0)) -> ToolEnv:
        return ToolEnv(
            root=self.repo,
            which=lambda name: shutil.which(name, path=str(self.bin_dir)),
            system="linux",
            machine="x86_64",
            node_version=node,
            environ={},
            depth=0,
        )

    def plan(
        self,
        *,
        rules: Mapping[str, object] | None = None,
        lint: Mapping[str, object] | None = None,
        catalog: Catalog | None = None,
        node: tuple[int, int, int] | None = (22, 18, 0),
        scopes: tuple[str, ...] = (),
    ) -> tuple[BootstrapPlan, ToolEnv]:
        data: dict[str, object] = {"linters": {"rules": dict(rules if rules is not None else NO_SLOP)}}
        if lint is not None:
            data["lint"] = dict(lint)
        cfg = resolve(data)
        discovery = discover(self.repo, scopes=scopes)
        env = self.env(node)
        needs = bootstrap_needs(discovery, cfg, env)
        return build_bootstrap_plan(needs, discovery, cfg, env, catalog=catalog), env
