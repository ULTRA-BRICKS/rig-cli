"""omp idle-TTL keepalive — rig writes a task.agentIdleTtlMs grace window for new omp processes.

After yield, omp adopts the helper and parks it after task.agentIdleTtlMs (product
default 420000 ms). Parked agents drop off the *default* hub list view (a parked peer
stays fully reachable via hub send/history://.../agent://). rig's default is 300000 ms
(5 min): shorter than omp's own stock default, and bounded instead of the old 0's
never-park-until-exit lifetime that let idle helpers accumulate without bound.
harness.idle_ttl.ms overrides the default; harness.idle_ttl.ms: 0 restores the old
never-park behavior; harness.idle_ttl.enabled: false skips provisioning entirely.
Refs https://github.com/alex-mextner/rig-cli/issues/445
Refs https://github.com/alex-mextner/rig-cli/issues/464
"""

from __future__ import annotations

from pathlib import Path

from riglib.actions import run_plan
from riglib.catalog import Catalog
from riglib.config import ConfigError, LoadedConfig, validate
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
    assert acts[0].options["ms"] == 300000


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


def test_apply_writes_default_grace_window_preserving_siblings(fake_agent_tools, tmp_path, monkeypatch):
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
    assert data["task"]["agentIdleTtlMs"] == 300000
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
    assert _read_yml(cfg_yml)["task"]["agentIdleTtlMs"] == 300000
    assert _read_yml(cfg_yml)["tools"]["approvalMode"] == "yolo"


def test_config_overrides_idle_ttl_ms(fake_agent_tools, tmp_path, monkeypatch):
    """harness.idle_ttl.ms overrides the 300000 default, plumbed through to the write."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _cfg(repo, fake_agent_tools)
    cfg.data["harness"]["idle_ttl"] = {"ms": 600000}
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    acts = _actions(plan, "provision_omp_idle_ttl")
    assert len(acts) == 1, plan.actions
    assert acts[0].options["ms"] == 600000
    run_plan(plan)
    cfg_yml = home / ".omp" / "agent" / "config.yml"
    assert _read_yml(cfg_yml)["task"]["agentIdleTtlMs"] == 600000


def test_idle_ttl_disabled_via_config_skips_action(fake_agent_tools, tmp_path, monkeypatch):
    """harness.idle_ttl.enabled: false skips provisioning entirely (no action emitted)."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _cfg(repo, fake_agent_tools)
    cfg.data["harness"]["idle_ttl"] = {"enabled": False}
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert not _actions(plan, "provision_omp_idle_ttl")


def test_config_ms_zero_restores_never_park(fake_agent_tools, tmp_path, monkeypatch):
    """harness.idle_ttl.ms: 0 still opts back into the old never-park behavior end to end."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _cfg(repo, fake_agent_tools)
    cfg.data["harness"]["idle_ttl"] = {"ms": 0}
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    run_plan(plan)
    cfg_yml = home / ".omp" / "agent" / "config.yml"
    assert _read_yml(cfg_yml)["task"]["agentIdleTtlMs"] == 0


def test_legacy_negative_ttl_is_not_reported_as_drift_against_new_default(fake_agent_tools, tmp_path, monkeypatch):
    """A pre-existing non-positive TTL (old never-park convention) is a standing choice
    apply always preserves -- drift must agree it is clean, never a permanent mismatch
    against the (now positive) resolved default it can never auto-fix."""
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
    drift = detect(plan)
    assert not any(i.item == "omp-idle-ttl" for i in drift.items), drift.items


def test_malformed_ms_shape_skips_provisioning(fake_agent_tools, tmp_path, monkeypatch):
    """An idle_ttl.ms value rig cannot interpret (e.g. a string) skips provisioning
    entirely, the same leniency resolve_exclude applies to a non-list exclude."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _cfg(repo, fake_agent_tools)
    cfg.data["harness"]["idle_ttl"] = {"ms": "soon"}
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert not _actions(plan, "provision_omp_idle_ttl")


# ── validation + schema registry (rig-cli#555 shipped harness.idle_ttl unvalidated: real
# users setting it in rig.yaml hit "unknown harness key: idle_ttl" at validate() time, and
# the hand-edited schema/rig.schema.json drifted from the config_schema.py registry that
# `_reject_unknown_keys` sources from — closing both gaps here) ────────────────────────────
def test_idle_ttl_block_validates_and_rejects_bad_types():
    validate({"version": 1, "harness": {"kind": "omp", "idle_ttl": {"ms": 240000}}})
    validate({"version": 1, "harness": {"kind": "omp", "idle_ttl": {"enabled": False}}})
    validate({"version": 1, "harness": {"kind": "omp", "idle_ttl": {"ms": 0}}})
    try:
        validate({"version": 1, "harness": {"kind": "omp", "idle_ttl": {"ms": "soon"}}})
    except ConfigError as exc:
        assert "idle_ttl.ms" in str(exc)
    else:
        raise AssertionError("non-numeric ms must fail")
    try:
        validate({"version": 1, "harness": {"kind": "omp", "idle_ttl": {"enabled": "yes"}}})
    except ConfigError as exc:
        assert "idle_ttl.enabled" in str(exc)
    else:
        raise AssertionError("non-bool enabled must fail")
    try:
        validate({"version": 1, "harness": {"kind": "omp", "idle_ttl": {"bogus": True}}})
    except ConfigError as exc:
        assert "idle_ttl" in str(exc)
    else:
        raise AssertionError("unknown idle_ttl key must fail")
    # ms must be a plain int — a stray float (including non-finite .inf/.nan, which
    # `int()` cannot convert) or bool must fail closed at validate(), not reach the
    # runtime resolver and raise OverflowError/ValueError out of `rig apply`.
    for bad_ms in (1.9, float("inf"), float("-inf"), float("nan"), True):
        try:
            validate({"version": 1, "harness": {"kind": "omp", "idle_ttl": {"ms": bad_ms}}})
        except ConfigError as exc:
            assert "idle_ttl.ms" in str(exc)
        else:
            raise AssertionError(f"ms={bad_ms!r} must fail")


def test_resolve_idle_ttl_ms_never_raises_on_non_finite_or_bool():
    """Defense in depth for callers that bypass validate() (unit tests construct
    LoadedConfig directly): a non-int ms must skip provisioning, never crash on
    int(float('inf'))/int(float('nan'))."""
    from riglib.omp_idle_ttl import resolve_idle_ttl_ms

    for bad_ms in (float("inf"), float("-inf"), float("nan"), 1.9, True):
        assert resolve_idle_ttl_ms({"idle_ttl": {"ms": bad_ms}}) is None
    assert resolve_idle_ttl_ms({"idle_ttl": {"ms": 240000}}) == 240000


def test_schema_registry_exposes_idle_ttl_keys():
    from riglib.schema import coerce, option_for_key

    enabled = option_for_key("harness.idle_ttl.enabled")
    ms = option_for_key("harness.idle_ttl.ms")
    assert enabled is not None and enabled.default is True
    assert ms is not None and ms.default == 300000
    assert coerce(enabled, "false") is False
    assert coerce(ms, "180000") == 180000


def test_json_schema_has_idle_ttl_node_in_sync_with_registry():
    from riglib import config_schema

    assert config_schema.schema_file_in_sync(), "schema/rig.schema.json must match the registry"
    node = config_schema.json_schema()["properties"]["harness"]["properties"]["idle_ttl"]
    assert node["properties"]["ms"]["default"] == 300000
    assert node["additionalProperties"] is False


