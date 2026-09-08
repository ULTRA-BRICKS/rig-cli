"""omp web_search exclude provisioning (rig-cli#447).

Perplexity is a default omp web_search provider whose anonymous/cookie path returns a
Sign-up wall. rig additively merges providers.webSearchExclude into omp config.yml
when omp is a configured harness kind.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from riglib.catalog import Catalog
from riglib.config import ConfigError, LoadedConfig, validate
from riglib.drift import detect
from riglib.omp_web_search import (
    DEFAULT_WEB_SEARCH_EXCLUDE,
    ensure_exclude,
    missing_exclude_entries,
    resolve_exclude,
)
from riglib.plan import Action, build


def _cfg(repo: Path, source: Path, harness: dict | None = None, **extra) -> LoadedConfig:
    data = {
        "agent_tools_source": str(source),
        "skills": {"enabled": False},
        "agent_hooks": {"enabled": False},
        "ci": {"enabled": False},
        "mcp": {"enabled": False},
        "git_hooks": {"dispatcher": {"enabled": False}},
        "harness": harness if harness is not None else {"enabled": True, "kind": "omp"},
        "permissions": {"enabled": False},
    }
    data.update(extra)
    return LoadedConfig(data=data, repo_root=repo)


def _plan(fake_agent_tools, tmp_path, monkeypatch, harness=None, **extra):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return home, build(
        _cfg(repo, fake_agent_tools, harness=harness, **extra),
        Catalog.scan(str(fake_agent_tools)),
        project_type="unknown",
    )


def _actions(plan, kind):
    return [a for a in plan.actions if a.kind == kind]


def _yml(home: Path) -> Path:
    return home / ".omp" / "agent" / "config.yml"


def _read(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


# ── resolve_exclude ─────────────────────────────────────────────────────────────
def test_resolve_exclude_defaults_to_perplexity_when_block_absent():
    assert resolve_exclude({"kind": "omp"}) == ["perplexity"]
    assert resolve_exclude(None) == []


def test_resolve_exclude_empty_list_is_opt_out():
    assert resolve_exclude({"web_search": {"exclude": []}}) == []
    assert resolve_exclude({"web_search": {"enabled": False}}) == []


def test_resolve_exclude_explicit_names_dedupe_and_drop_empties():
    assert resolve_exclude({"web_search": {"exclude": ["perplexity", "perplexity", ""]}}) == [
        "perplexity"
    ]


def test_ensure_exclude_is_additive_and_preserves_extras():
    data = {"providers": {"webSearchExclude": ["ecosia"], "other": 1}}
    added = ensure_exclude(data, ["perplexity"])
    assert added == ["perplexity"]
    assert data["providers"]["webSearchExclude"] == ["ecosia", "perplexity"]
    assert data["providers"]["other"] == 1
    assert ensure_exclude(data, ["perplexity"]) == []


def test_missing_exclude_entries_shape_and_ok():
    assert missing_exclude_entries({}, ["perplexity"])[0] == "missing"
    assert missing_exclude_entries({"providers": "x"}, ["perplexity"])[0] == "shape"
    data = {"providers": {"webSearchExclude": ["perplexity", "ecosia"]}}
    state, missing = missing_exclude_entries(data, ["perplexity"])
    assert state == "ok" and missing == []


# ── validation ──────────────────────────────────────────────────────────────────
def test_web_search_block_validates_and_rejects_bad_types():
    validate({"version": 1, "harness": {"kind": "omp", "web_search": {"exclude": ["perplexity"]}}})
    validate({"version": 1, "harness": {"kind": "omp", "web_search": {"enabled": False}}})
    try:
        validate({"version": 1, "harness": {"kind": "omp", "web_search": {"exclude": [""]}}})
    except ConfigError as exc:
        assert "web_search.exclude" in str(exc)
    else:
        raise AssertionError("empty exclude entry must fail")
    try:
        validate({"version": 1, "harness": {"kind": "omp", "web_search": {"bogus": True}}})
    except ConfigError as exc:
        assert "web_search" in str(exc)
    else:
        raise AssertionError("unknown web_search key must fail")


# ── plan ────────────────────────────────────────────────────────────────────────
def test_plan_omp_emits_default_perplexity_exclude(fake_agent_tools, tmp_path, monkeypatch):
    home, plan = _plan(fake_agent_tools, tmp_path, monkeypatch)
    acts = _actions(plan, "provision_omp_web_search")
    assert len(acts) == 1
    assert acts[0].options["exclude"] == ["perplexity"]
    assert acts[0].target == home / ".omp" / "agent" / "config.yml"
    assert acts[0].category == "harness"


def test_plan_kinds_omp_emits_even_when_primary_is_claude(fake_agent_tools, tmp_path, monkeypatch):
    _, plan = _plan(
        fake_agent_tools, tmp_path, monkeypatch,
        harness={"enabled": True, "kind": "claude-code", "kinds": ["omp"]},
    )
    assert _actions(plan, "provision_omp_web_search")


def test_plan_skips_when_omp_not_configured(fake_agent_tools, tmp_path, monkeypatch):
    _, plan = _plan(
        fake_agent_tools, tmp_path, monkeypatch,
        harness={"enabled": True, "kind": "claude-code"},
    )
    assert not _actions(plan, "provision_omp_web_search")


def test_plan_skips_when_harness_disabled(fake_agent_tools, tmp_path, monkeypatch):
    _, plan = _plan(
        fake_agent_tools, tmp_path, monkeypatch,
        harness={"enabled": False, "kind": "omp"},
    )
    assert not _actions(plan, "provision_omp_web_search")


def test_plan_skips_when_exclude_empty_or_disabled(fake_agent_tools, tmp_path, monkeypatch):
    _, plan = _plan(
        fake_agent_tools, tmp_path, monkeypatch,
        harness={"enabled": True, "kind": "omp", "web_search": {"exclude": []}},
    )
    assert not _actions(plan, "provision_omp_web_search")
    _, plan = _plan(
        fake_agent_tools, tmp_path, monkeypatch,
        harness={"enabled": True, "kind": "omp", "web_search": {"enabled": False}},
    )
    assert not _actions(plan, "provision_omp_web_search")


def test_plan_honors_pi_coding_agent_dir(fake_agent_tools, tmp_path, monkeypatch):
    home = tmp_path / "home"
    agent = tmp_path / "custom-agent"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent))
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = build(
        _cfg(repo, fake_agent_tools),
        Catalog.scan(str(fake_agent_tools)),
        project_type="unknown",
    )
    acts = _actions(plan, "provision_omp_web_search")
    assert acts[0].target == agent / "config.yml"


# ── apply ───────────────────────────────────────────────────────────────────────
def test_apply_creates_exclude_and_is_idempotent(fake_agent_tools, tmp_path, monkeypatch):
    from riglib.actions.runner import run_plan

    home, plan = _plan(fake_agent_tools, tmp_path, monkeypatch)
    report = run_plan(plan)
    res = [r for r in report.results if r.action.kind == "provision_omp_web_search"][0]
    assert res.status == "created", res.detail
    assert _read(_yml(home))["providers"]["webSearchExclude"] == ["perplexity"]
    report2 = run_plan(plan)
    res2 = [r for r in report2.results if r.action.kind == "provision_omp_web_search"][0]
    assert res2.status == "skipped", res2.detail


def test_apply_merges_additively_preserving_siblings(fake_agent_tools, tmp_path, monkeypatch):
    from riglib.actions.runner import run_plan

    home, plan = _plan(fake_agent_tools, tmp_path, monkeypatch)
    cfg = _yml(home)
    cfg.parent.mkdir(parents=True)
    cfg.write_text("modelRoles: {default: x}\nproviders:\n  webSearchExclude: [ecosia]\n  keep: 1\n", encoding="utf-8")
    res = [r for r in run_plan(plan).results if r.action.kind == "provision_omp_web_search"][0]
    assert res.status in {"updated", "backed_up"}, res.detail
    data = _read(cfg)
    assert data["providers"]["webSearchExclude"] == ["ecosia", "perplexity"]
    assert data["providers"]["keep"] == 1
    assert data["modelRoles"]["default"] == "x"


def test_apply_malformed_yaml_is_hard_error(fake_agent_tools, tmp_path, monkeypatch):
    from riglib.actions.runner import run_plan

    home, plan = _plan(fake_agent_tools, tmp_path, monkeypatch)
    cfg = _yml(home)
    cfg.parent.mkdir(parents=True)
    before = "providers: [not: valid: yaml:\n"
    cfg.write_text(before, encoding="utf-8")
    res = [r for r in run_plan(plan).results if r.action.kind == "provision_omp_web_search"][0]
    assert res.status == "error" and "malformed" in res.detail
    assert cfg.read_text(encoding="utf-8") == before


def test_apply_skip_does_not_rewrite(fake_agent_tools, tmp_path, monkeypatch):
    from riglib.actions.runner import run_plan

    home, plan = _plan(fake_agent_tools, tmp_path, monkeypatch)
    plan.on_conflict = "skip"
    cfg = _yml(home)
    cfg.parent.mkdir(parents=True)
    before = "theme: titanium\n"
    cfg.write_text(before, encoding="utf-8")
    res = [r for r in run_plan(plan).results if r.action.kind == "provision_omp_web_search"][0]
    assert res.status == "skipped"
    assert cfg.read_text(encoding="utf-8") == before


def test_apply_wrong_shape_is_skipped_not_clobbered():
    from riglib.actions.runner import _do_provision_omp_web_search

    cfg = Path("/tmp")  # placeholder replaced below
    # constructed below with tmp via Action target
    from riglib.plan import Action as A
    import tempfile
    td = Path(tempfile.mkdtemp())
    target = td / "config.yml"
    target.write_text("providers: not-a-map\n", encoding="utf-8")
    action = A(
        kind="provision_omp_web_search",
        category="harness",
        item="omp",
        source=td,
        target=target,
        options={"kind": "omp", "exclude": ["perplexity"]},
    )
    res = _do_provision_omp_web_search(action, "backup")
    assert res.status == "skipped" and "shape" in res.detail
    assert "not-a-map" in target.read_text(encoding="utf-8")


# ── drift ───────────────────────────────────────────────────────────────────────
def test_drift_missing_then_clean(fake_agent_tools, tmp_path, monkeypatch):
    from riglib.actions.runner import run_plan

    home, plan = _plan(fake_agent_tools, tmp_path, monkeypatch)
    rep = detect(plan)
    assert any(
        d.direction == "missing" and "webSearchExclude" in d.detail for d in rep.items
    ), [d.detail for d in rep.items]
    run_plan(plan)
    rep2 = detect(plan)
    assert not [d for d in rep2.items if "webSearchExclude" in d.detail], [d.detail for d in rep2.items]


def test_drift_already_present_is_not_drift(fake_agent_tools, tmp_path, monkeypatch):
    """A hand-set exclude that already contains perplexity is in-sync — extras stay, no drift."""
    home, plan = _plan(fake_agent_tools, tmp_path, monkeypatch)
    cfg = _yml(home)
    cfg.parent.mkdir(parents=True)
    cfg.write_text("providers:\n  webSearchExclude: [perplexity, ecosia]\n", encoding="utf-8")
    rep = detect(plan)
    assert not [d for d in rep.items if "webSearchExclude" in d.detail], [d.detail for d in rep.items]


assert DEFAULT_WEB_SEARCH_EXCLUDE == ("perplexity",)

def test_plan_emits_web_search_after_approval(fake_agent_tools, tmp_path, monkeypatch):
    """First-run skip is order-sensitive: approval must create the file, then web_search merges."""
    _, plan = _plan(
        fake_agent_tools, tmp_path, monkeypatch,
        harness={"enabled": True, "kind": "omp", "auto_mode": False},
        permissions={"enabled": True},
    )
    kinds = [a.kind for a in plan.actions
             if a.kind in ("provision_harness_approval", "provision_omp_web_search")]
    assert "provision_harness_approval" in kinds
    assert "provision_omp_web_search" in kinds
    assert kinds.index("provision_harness_approval") < kinds.index("provision_omp_web_search")


def test_first_run_skip_writes_approval_and_exclude(fake_agent_tools, tmp_path, monkeypatch):
    """No config.yml + on_conflict=skip must still land always-ask AND perplexity exclude.

    If web_search created the file first, approval treated it as an existing-file skip
    and never wrote tools.approvalMode (codex P1 on rig-cli#447).
    """
    from riglib.actions.runner import run_plan

    home, plan = _plan(
        fake_agent_tools, tmp_path, monkeypatch,
        harness={"enabled": True, "kind": "omp", "auto_mode": False},
        permissions={"enabled": True},
        defaults={"on_conflict": "skip"},
    )
    assert plan.on_conflict == "skip"
    assert not _yml(home).exists()
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    data = _read(_yml(home))
    assert data["tools"]["approvalMode"] == "always-ask"
    assert "perplexity" in data["providers"]["webSearchExclude"]


def test_schema_registry_exposes_web_search_keys():
    from riglib.schema import option_for_key, coerce
    enabled = option_for_key("harness.web_search.enabled")
    exclude = option_for_key("harness.web_search.exclude")
    assert enabled is not None and enabled.default is True
    assert exclude is not None and exclude.default == ["perplexity"]
    assert coerce(enabled, "false") is False
    assert coerce(exclude, "") == []

