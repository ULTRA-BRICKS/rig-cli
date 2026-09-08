"""omp idle-TTL keepalive — rig writes task.agentIdleTtlMs: 0 for new omp processes.

After yield, omp adopts the helper and parks it after task.agentIdleTtlMs (default
420000 ms). Parked agents drop off the live hub roster. <= 0 disables parking at
Settings.init; a live omp process keeps the spawn-cached TTL until restart.
Refs https://github.com/alex-mextner/rig-cli/issues/445
Refs https://github.com/alex-mextner/rig-cli/issues/464
"""

from __future__ import annotations

from pathlib import Path

from riglib.actions import run_plan
from riglib.catalog import Catalog
from riglib.config import LoadedConfig
from riglib.drift import detect
from riglib.plan import build


def _cfg(repo: Path, source: Path, *, kind: str = "omp", kinds: list[str] | None = None, enabled: bool = True) -> LoadedConfig:
    harness: dict = {"enabled": enabled, "kind": kind}
    if kinds:
        harness["kinds"] = kinds
    return LoadedConfig(
        data={
            "agent_tools_source": str(source),
            "skills": {"enabled": False},
            "agent_hooks": {"enabled": False},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "harness": harness,
        },
        repo_root=repo,
    )


def _actions(plan, kind: str):
    return [a for a in plan.actions if a.kind == kind]


def _read_yml(path: Path) -> dict:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def test_plan_emits_idle_ttl_for_omp(fake_agent_tools, tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = build(_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    acts = _actions(plan, "provision_omp_idle_ttl")
    assert len(acts) == 1, plan.actions
    assert acts[0].target == home / ".omp" / "agent" / "config.yml"
    assert acts[0].category == "harness"
    assert acts[0].options["kind"] == "omp"


def test_plan_skips_when_harness_disabled(fake_agent_tools, tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = build(
        _cfg(repo, fake_agent_tools, enabled=False),
        Catalog.scan(str(fake_agent_tools)),
        project_type="unknown",
    )
    assert not _actions(plan, "provision_omp_idle_ttl")


def test_plan_skips_when_kind_is_not_omp(fake_agent_tools, tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = build(
        _cfg(repo, fake_agent_tools, kind="claude-code"),
        Catalog.scan(str(fake_agent_tools)),
        project_type="unknown",
    )
    assert not _actions(plan, "provision_omp_idle_ttl")


def test_plan_emits_when_omp_is_in_kinds(fake_agent_tools, tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = build(
        _cfg(repo, fake_agent_tools, kind="claude-code", kinds=["codex", "omp"]),
        Catalog.scan(str(fake_agent_tools)),
        project_type="unknown",
    )
    acts = _actions(plan, "provision_omp_idle_ttl")
    assert len(acts) == 1
    assert acts[0].target == home / ".omp" / "agent" / "config.yml"


def test_apply_writes_zero_preserving_siblings(fake_agent_tools, tmp_path, monkeypatch):
    home = tmp_path / "home"
    cfg_yml = home / ".omp" / "agent" / "config.yml"
    cfg_yml.parent.mkdir(parents=True)
    cfg_yml.write_text("theme:\n  dark: titanium\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = build(_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    report = run_plan(plan)
    res = [r for r in report.results if r.action.kind == "provision_omp_idle_ttl"][0]
    assert res.status in {"created", "updated", "backed_up"}, res.detail
    data = _read_yml(cfg_yml)
    assert data["task"]["agentIdleTtlMs"] == 0
    assert data["theme"]["dark"] == "titanium"


def test_apply_is_idempotent(fake_agent_tools, tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = build(_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    first = [r for r in run_plan(plan).results if r.action.kind == "provision_omp_idle_ttl"][0]
    assert first.status in {"created", "updated", "backed_up"}, first.detail
    cfg_yml = home / ".omp" / "agent" / "config.yml"
    text = cfg_yml.read_text(encoding="utf-8")
    second = [r for r in run_plan(plan).results if r.action.kind == "provision_omp_idle_ttl"][0]
    assert second.status == "skipped", second.detail
    assert cfg_yml.read_text(encoding="utf-8") == text


def test_apply_detail_names_live_session_spawn_cache(fake_agent_tools, tmp_path, monkeypatch):
    """Apply detail must name the spawn-cached TTL so a YAML write is not read as live."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = build(_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    write = [r for r in run_plan(plan).results if r.action.kind == "provision_omp_idle_ttl"][0]
    assert "spawn-cached" in write.detail, write.detail
    skip = [r for r in run_plan(plan).results if r.action.kind == "provision_omp_idle_ttl"][0]
    assert skip.status == "skipped", skip.detail
    assert "spawn-cached" in skip.detail, skip.detail


def test_never_clobbers_positive_user_ttl(fake_agent_tools, tmp_path, monkeypatch):
    home = tmp_path / "home"
    cfg_yml = home / ".omp" / "agent" / "config.yml"
    cfg_yml.parent.mkdir(parents=True)
    cfg_yml.write_text("task:\n  agentIdleTtlMs: 420000\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = build(_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    res = [r for r in run_plan(plan).results if r.action.kind == "provision_omp_idle_ttl"][0]
    assert res.status == "skipped", res.detail
    assert _read_yml(cfg_yml)["task"]["agentIdleTtlMs"] == 420000
    drift = detect(plan)
    assert any(
        i.direction == "modified" and "agentIdleTtlMs" in i.detail
        for i in drift.items
    ), drift.items


def test_negative_ttl_is_already_disabled(fake_agent_tools, tmp_path, monkeypatch):
    home = tmp_path / "home"
    cfg_yml = home / ".omp" / "agent" / "config.yml"
    cfg_yml.parent.mkdir(parents=True)
    cfg_yml.write_text("task:\n  agentIdleTtlMs: -1\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = build(_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    res = [r for r in run_plan(plan).results if r.action.kind == "provision_omp_idle_ttl"][0]
    assert res.status == "skipped", res.detail
    assert _read_yml(cfg_yml)["task"]["agentIdleTtlMs"] == -1


def test_drift_missing_then_clean(fake_agent_tools, tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = build(_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    before = detect(plan)
    assert any(
        i.direction == "missing" and i.item == "omp-idle-ttl"
        for i in before.items
    ), before.items
    run_plan(plan)
    after = detect(plan)
    assert not any(i.item == "omp-idle-ttl" for i in after.items), after.items


def test_skip_still_writes_missing_key(fake_agent_tools, tmp_path, monkeypatch):
    """A sibling may have created config.yml in this apply; skip must not freeze missing TTL."""
    home = tmp_path / "home"
    cfg_yml = home / ".omp" / "agent" / "config.yml"
    cfg_yml.parent.mkdir(parents=True)
    cfg_yml.write_text("tools:\n  approvalMode: yolo\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _cfg(repo, fake_agent_tools)
    cfg.data["defaults"] = {"on_conflict": "skip"}
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    res = [r for r in run_plan(plan).results if r.action.kind == "provision_omp_idle_ttl"][0]
    assert res.status in {"updated", "created", "backed_up"}, res.detail
    assert _read_yml(cfg_yml)["task"]["agentIdleTtlMs"] == 0
    assert _read_yml(cfg_yml)["tools"]["approvalMode"] == "yolo"
