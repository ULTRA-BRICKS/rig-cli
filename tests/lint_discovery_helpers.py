"""Fixtures for the lint discovery tests: real throwaway git repositories, no network, no repo code executed."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from riglib.lint_discovery import ConfiguredTool, Discovery, ProjectScope, discover


def make_repo(tmp_path: Path, files: dict[str, str], name: str = "repo") -> Path:
    """Write ``files`` (relative path -> text) into a fresh ``git init`` repository and return its resolved root."""
    root = (tmp_path / name).resolve()
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def pkg(**fields: object) -> str:
    return json.dumps(fields)


def scan(tmp_path: Path, files: dict[str, str], name: str = "repo") -> Discovery:
    return discover(make_repo(tmp_path, files, name))


def scope_of(discovery: Discovery, rel: str) -> ProjectScope:
    found = discovery.scope(rel)
    assert found is not None, f"no scope {rel!r} in {[s.rel for s in discovery.scopes]}"
    return found


def tool_rows(scope: ProjectScope, tool: str) -> list[ConfiguredTool]:
    return [t for t in scope.tools if t.tool == tool]


def tool_row(scope: ProjectScope, tool: str, role: str) -> ConfiguredTool:
    rows = [t for t in scope.tools if t.tool == tool and t.role == role]
    assert rows, f"no {tool}:{role} in {[(t.tool, t.role) for t in scope.tools]}"
    return rows[0]
