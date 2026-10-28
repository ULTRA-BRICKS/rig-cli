"""``plan.build(..., lint_bootstrap=...)`` — the lint-scoped build ``rig lint`` bootstraps through.

The bootstrap goes through the SAME ``plan.build`` + ``actions.run_plan`` as ``rig apply`` (no second
executor), but only when a :class:`LintBootstrapRequest` is passed. Real tmp repos and real discovery;
package managers are never spawned (planning is pure), ``which`` is a fake PATH lookup.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from riglib import plan as planmod
from riglib.catalog import Catalog
from riglib.config import LoadedConfig
from riglib.lint_discovery import discover
from riglib.lint_providers import ToolEnv, bootstrap_needs
from riglib.lint_run_config import resolve
from riglib.plan import InstallPlan, LintBootstrapRequest, PlanError

NO_SLOP = {"groups": {"anti-slop": False}}


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return repo


def _request(repo: Path, lint: dict[str, object] | None = None, managers: tuple[str, ...] = ("npm",)) -> LintBootstrapRequest:
    data: dict[str, object] = {"linters": {"rules": NO_SLOP}}
    if lint is not None:
        data["lint"] = lint
    cfg = resolve(data)
    discovery = discover(repo)
    env = ToolEnv(
        root=repo,
        which=lambda name: f"/fake/bin/{name}" if name in managers else None,
        system="linux",
        machine="x86_64",
        node_version=(22, 18, 0),
        environ={},
        depth=0,
    )
    return LintBootstrapRequest(tuple(bootstrap_needs(discovery, cfg, env)), discovery, cfg, env)


def _tree(repo: Path) -> dict[str, bytes]:
    return {
        p.relative_to(repo).as_posix(): p.read_bytes() if p.is_file() else b"<dir>"
        for p in sorted(repo.rglob("*"))
        if ".git" not in p.relative_to(repo).parts
    }


def _js_repo(tmp_path: Path) -> Path:
    return _repo(tmp_path, {"package.json": json.dumps({"name": "demo", "version": "1.0.0"}), "src/a.ts": "export {};\n"})


def test_lint_scoped_build_emits_bootstrap_steps_and_nothing_else(tmp_path):
    repo = _js_repo(tmp_path)
    plan = planmod.build(LoadedConfig(data={}, repo_root=repo), None, lint_bootstrap=_request(repo))
    kinds = {a.kind for a in plan.actions}
    assert "lint_bootstrap_step" in kinds
    assert kinds <= {"lint_bootstrap_step", "provision_linter_config", "provision_linter_bundle"}
    assert plan.on_conflict == "skip"
    assert plan.lint_bootstrap is not None and plan.lint_bootstrap.steps
    steps = [a for a in plan.actions if a.kind == "lint_bootstrap_step"]
    assert {p["name"] for a in steps for p in a.options.get("packages", ())} >= {"oxlint", "oxfmt"}


def test_planning_writes_nothing_to_disk(tmp_path):
    repo = _js_repo(tmp_path)
    request = _request(repo)
    before = _tree(repo)
    planmod.build(LoadedConfig(data={}, repo_root=repo), None, lint_bootstrap=request)
    assert _tree(repo) == before


def test_lint_scoped_build_is_deterministic(tmp_path):
    repo = _js_repo(tmp_path)
    config = LoadedConfig(data={}, repo_root=repo)
    first = planmod.build(config, None, lint_bootstrap=_request(repo))
    second = planmod.build(config, None, lint_bootstrap=_request(repo))
    assert [(a.kind, a.item, a.options) for a in first.actions] == [(a.kind, a.item, a.options) for a in second.actions]
    assert first.notes == second.notes


def test_bootstrap_disabled_by_config_plans_no_action_but_reports_blocked(tmp_path):
    repo = _js_repo(tmp_path)
    request = _request(repo, lint={"bootstrap": {"enabled": False}})
    plan = planmod.build(LoadedConfig(data={}, repo_root=repo), None, lint_bootstrap=request)
    assert plan.actions == []
    assert plan.lint_bootstrap is not None
    assert plan.lint_bootstrap.blocked and {b.reason for b in plan.lint_bootstrap.blocked} == {"disabled-by-config"}


def test_configured_linters_are_never_bootstrapped(tmp_path):
    repo = _repo(tmp_path, {
        "package.json": json.dumps({"name": "demo", "devDependencies": {"@biomejs/biome": "1.9.4"}}),
        "biome.json": "{}",
        "src/a.ts": "export {};\n",
    })
    plan = planmod.build(LoadedConfig(data={}, repo_root=repo), None, lint_bootstrap=_request(repo))
    assert [a for a in plan.actions if a.kind == "lint_bootstrap_step"] == []


def test_apply_build_never_emits_bootstrap_actions(tmp_path):
    repo = _js_repo(tmp_path)
    catalog = Catalog(source=tmp_path / "agent-tools")
    plan = planmod.build(LoadedConfig(data={}, repo_root=repo), catalog)
    assert plan.lint_bootstrap is None
    assert not [a for a in plan.actions if a.category == "lint_bootstrap"]


def test_apply_build_requires_a_catalog(tmp_path):
    repo = _js_repo(tmp_path)
    with pytest.raises(PlanError, match="catalog"):
        planmod.build(LoadedConfig(data={}, repo_root=repo), None)


def test_lint_scoped_build_refuses_a_request_for_another_repo(tmp_path):
    repo = _js_repo(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(PlanError, match="discovered at"):
        planmod.build(LoadedConfig(data={}, repo_root=other), None, lint_bootstrap=_request(repo))


def test_reexported_surface_matches_the_pre_split_module():
    for name in (
        "Action", "InstallPlan", "PlanError", "build", "resolve_category_target", "resolve_category_targets",
        "env_options_from_config", "HOOK_BRIDGE_NO_SUPPORT_PHRASE", "SHIP_TASK_CODE_PREFIX_RE", "_expand",
        "_HARNESS_SKILL_DIRS", "_resolve_harness_skill_dir", "_resolved_permission_tools", "_build_linters",
        "_divert_tracked_files", "_build_tmux", "_build_hook_bridge", "_plan_hook_blurbs",
    ):
        assert hasattr(planmod, name), name
    assert isinstance(planmod.InstallPlan(), InstallPlan)
