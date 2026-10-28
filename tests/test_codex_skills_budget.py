"""Codex skills-context budget — rig writes skills.max_context_tokens into ~/.codex/config.toml.

Codex CLI truncates/omits skill descriptions from its model-visible catalog once a personal
skill library's rendered text exceeds Codex's own (much smaller) context budget for that
catalog (openai/codex#24299) — a real gap on any machine whose skill library grows past Codex's
stock default (observed ~5440-8000 chars on a 71-skill library). rig's default is 50000: verified
(2026-09-18, InvestigateCodexSkillLoading) to roughly double visible description length and
plateau around 40KB. harness.codex_skills_budget.max_context_tokens overrides the default;
harness.codex_skills_budget.enabled: false skips provisioning entirely. Mirrors the idle-TTL
convention (rig-cli#445/#464): additive, never clobbers a value already on disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from riglib.actions import run_plan
from riglib.catalog import Catalog
from riglib.config import ConfigError, LoadedConfig, validate
from riglib.drift import detect
from riglib.plan import build


def _cfg(repo: Path, source: Path, *, kind: str = "codex", kinds: list[str] | None = None, enabled: bool = True) -> LoadedConfig:
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


@pytest.fixture
def codex_env(tmp_path, monkeypatch):
    """A fresh RIG_CODEX_HOME + repo dir, matching every test's identical preamble."""
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("RIG_CODEX_HOME", str(codex_home))
    repo = tmp_path / "repo"
    repo.mkdir()
    return codex_home, repo


def _actions(plan, kind: str):
    return [a for a in plan.actions if a.kind == kind]


def _read_toml(path: Path) -> dict:
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
        import tomli as tomllib  # type: ignore[no-redef]

    return tomllib.loads(path.read_text(encoding="utf-8"))


def _budget_result(plan):
    results = [r for r in run_plan(plan).results if r.action.kind == "provision_codex_skills_budget"]
    assert len(results) == 1, results
    return results[0]


def test_plan_emits_skills_budget_for_codex(fake_agent_tools, codex_env):
    codex_home, repo = codex_env
    plan = build(_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    acts = _actions(plan, "provision_codex_skills_budget")
    assert len(acts) == 1, plan.actions
    assert acts[0].target == codex_home / "config.toml"
    assert acts[0].category == "harness"
    assert acts[0].options["kind"] == "codex"
    assert acts[0].options["max_context_tokens"] == 50000


def test_plan_skips_when_harness_disabled(fake_agent_tools, codex_env):
    _codex_home, repo = codex_env
    plan = build(
        _cfg(repo, fake_agent_tools, enabled=False),
        Catalog.scan(str(fake_agent_tools)),
        project_type="unknown",
    )
    assert not _actions(plan, "provision_codex_skills_budget")


def test_plan_skips_when_kind_is_not_codex(fake_agent_tools, codex_env):
    _codex_home, repo = codex_env
    plan = build(
        _cfg(repo, fake_agent_tools, kind="claude-code"),
        Catalog.scan(str(fake_agent_tools)),
        project_type="unknown",
    )
    assert not _actions(plan, "provision_codex_skills_budget")


def test_plan_emits_when_codex_is_in_kinds(fake_agent_tools, codex_env):
    codex_home, repo = codex_env
    plan = build(
        _cfg(repo, fake_agent_tools, kind="claude-code", kinds=["omp", "codex"]),
        Catalog.scan(str(fake_agent_tools)),
        project_type="unknown",
    )
    acts = _actions(plan, "provision_codex_skills_budget")
    assert len(acts) == 1
    assert acts[0].target == codex_home / "config.toml"


def test_plan_skips_with_visible_note_when_no_toml_parser(fake_agent_tools, codex_env, monkeypatch):
    """A Python without tomllib/tomli (3.10 without the tomli backport, which pyproject.toml
    ships only in the `test` extra) must NOT emit an action that hard-fails `rig apply` for
    every machine with codex configured — the plan skips with a visible note instead
    (rig-cli, review finding: default-on rollout widened the blast radius vs the opt-in
    mode writer, which only errors when an explicit auto_mode/mode intent is declared)."""
    import riglib.codex_skills_budget as csb

    monkeypatch.setattr(csb, "toml_parser_available", lambda: False)
    _codex_home, repo = codex_env
    plan = build(_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert not _actions(plan, "provision_codex_skills_budget")
    assert any("no TOML parser" in n for n in plan.notes), plan.notes


def test_apply_writes_default_budget_preserving_siblings(fake_agent_tools, codex_env):
    codex_home, repo = codex_env
    codex_home.mkdir(parents=True)
    cfg_toml = codex_home / "config.toml"
    cfg_toml.write_text('model = "gpt-5"\n\n[mcp_servers.haft]\ncommand = "haft"\n', encoding="utf-8")
    plan = build(_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    res = _budget_result(plan)
    assert res.status == "backed_up", res.detail
    data = _read_toml(cfg_toml)
    assert data["skills"]["max_context_tokens"] == 50000
    assert data["model"] == "gpt-5"
    assert data["mcp_servers"]["haft"]["command"] == "haft"


def test_apply_creates_file_when_absent(fake_agent_tools, codex_env):
    codex_home, repo = codex_env
    plan = build(_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    res = _budget_result(plan)
    assert res.status == "created", res.detail
    cfg_toml = codex_home / "config.toml"
    assert _read_toml(cfg_toml)["skills"]["max_context_tokens"] == 50000


def test_apply_inserts_into_an_existing_mid_file_skills_table(fake_agent_tools, codex_env):
    """The bounds-found branch of _insert_toml_table_key — an existing [skills] table sitting
    between two other tables — is exercised (not just the append-a-new-table branch)."""
    codex_home, repo = codex_env
    codex_home.mkdir(parents=True)
    cfg_toml = codex_home / "config.toml"
    existing = 'model = "gpt-5"\n\n[skills]\nother_setting = true\n\n[mcp_servers.haft]\ncommand = "haft"\n'
    cfg_toml.write_text(existing, encoding="utf-8")
    plan = build(_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    res = _budget_result(plan)
    assert res.status == "backed_up", res.detail
    data = _read_toml(cfg_toml)
    assert data["skills"] == {"other_setting": True, "max_context_tokens": 50000}
    assert data["mcp_servers"]["haft"]["command"] == "haft"


def test_apply_is_idempotent(fake_agent_tools, codex_env):
    codex_home, repo = codex_env
    plan = build(_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    first = _budget_result(plan)
    assert first.status == "created", first.detail
    cfg_toml = codex_home / "config.toml"
    text = cfg_toml.read_text(encoding="utf-8")
    second = _budget_result(plan)
    assert second.status == "skipped", second.detail
    assert cfg_toml.read_text(encoding="utf-8") == text


def test_never_clobbers_a_lower_user_value(fake_agent_tools, codex_env):
    """The manual InvestigateCodexSkillLoading-style edit reconciliation case in reverse: a
    smaller value already on disk (the machine's OWN choice) must survive apply untouched."""
    codex_home, repo = codex_env
    codex_home.mkdir(parents=True)
    cfg_toml = codex_home / "config.toml"
    cfg_toml.write_text("[skills]\nmax_context_tokens = 8000\n", encoding="utf-8")
    plan = build(_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    res = _budget_result(plan)
    assert res.status == "skipped", res.detail
    assert _read_toml(cfg_toml)["skills"]["max_context_tokens"] == 8000
    drift = detect(plan)
    assert any(
        i.direction == "modified" and "max_context_tokens" in i.detail
        for i in drift.items
    ), drift.items


def test_never_clobbers_a_higher_user_value(fake_agent_tools, codex_env):
    """A user-set value HIGHER than the resolved default (their own deliberate choice) must
    also survive apply untouched — 'never clobber a higher value' is the explicit contract."""
    codex_home, repo = codex_env
    codex_home.mkdir(parents=True)
    cfg_toml = codex_home / "config.toml"
    cfg_toml.write_text("[skills]\nmax_context_tokens = 200000\n", encoding="utf-8")
    plan = build(_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    res = _budget_result(plan)
    assert res.status == "skipped", res.detail
    assert _read_toml(cfg_toml)["skills"]["max_context_tokens"] == 200000


def test_reconciles_the_manual_investigate_codex_skill_loading_edit(fake_agent_tools, codex_env):
    """A pre-existing config.toml whose [skills].max_context_tokens ALREADY equals the resolved
    default (the manual edit this feature reconciles) reports skipped, not created — it becomes
    managed history, drift stays clean, and the byte content is left completely untouched."""
    codex_home, repo = codex_env
    codex_home.mkdir(parents=True)
    cfg_toml = codex_home / "config.toml"
    existing = 'model = "gpt-5"\n\n[skills]\nmax_context_tokens = 50000\n'
    cfg_toml.write_text(existing, encoding="utf-8")
    plan = build(_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    res = _budget_result(plan)
    assert res.status == "skipped", res.detail
    assert cfg_toml.read_text(encoding="utf-8") == existing
    assert not any(i.item == "codex-skills-budget" for i in detect(plan).items)


def test_drift_missing_then_clean(fake_agent_tools, codex_env):
    _codex_home, repo = codex_env
    plan = build(_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    before = detect(plan)
    assert any(
        i.direction == "missing" and i.item == "codex-skills-budget"
        for i in before.items
    ), before.items
    run_plan(plan)
    after = detect(plan)
    assert not any(i.item == "codex-skills-budget" for i in after.items), after.items


def test_skip_still_writes_missing_key(fake_agent_tools, codex_env):
    """A sibling may have created config.toml in this apply; skip must not freeze a missing key."""
    codex_home, repo = codex_env
    codex_home.mkdir(parents=True)
    cfg_toml = codex_home / "config.toml"
    cfg_toml.write_text('approvals_reviewer = "user"\n', encoding="utf-8")
    cfg = _cfg(repo, fake_agent_tools)
    cfg.data["defaults"] = {"on_conflict": "skip"}
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    res = _budget_result(plan)
    assert res.status == "updated", res.detail
    data = _read_toml(cfg_toml)
    assert data["skills"]["max_context_tokens"] == 50000
    assert data["approvals_reviewer"] == "user"


def test_config_overrides_max_context_tokens(fake_agent_tools, codex_env):
    codex_home, repo = codex_env
    cfg = _cfg(repo, fake_agent_tools)
    cfg.data["harness"]["codex_skills_budget"] = {"max_context_tokens": 90000}
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    acts = _actions(plan, "provision_codex_skills_budget")
    assert len(acts) == 1, plan.actions
    assert acts[0].options["max_context_tokens"] == 90000
    run_plan(plan)
    cfg_toml = codex_home / "config.toml"
    assert _read_toml(cfg_toml)["skills"]["max_context_tokens"] == 90000


def test_skills_budget_disabled_via_config_skips_action(fake_agent_tools, codex_env):
    _codex_home, repo = codex_env
    cfg = _cfg(repo, fake_agent_tools)
    cfg.data["harness"]["codex_skills_budget"] = {"enabled": False}
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert not _actions(plan, "provision_codex_skills_budget")


def test_malformed_max_context_tokens_shape_skips_provisioning(fake_agent_tools, codex_env):
    """A max_context_tokens value rig cannot interpret (e.g. a string, or <= 0) skips
    provisioning entirely, the same leniency resolve_idle_ttl_ms applies to a bad ms."""
    _codex_home, repo = codex_env
    cfg = _cfg(repo, fake_agent_tools)
    cfg.data["harness"]["codex_skills_budget"] = {"max_context_tokens": "lots"}
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert not _actions(plan, "provision_codex_skills_budget")


def test_malformed_toml_errors_without_rewriting(fake_agent_tools, codex_env):
    codex_home, repo = codex_env
    codex_home.mkdir(parents=True)
    cfg_toml = codex_home / "config.toml"
    cfg_toml.write_text("this is [ not valid toml\n", encoding="utf-8")
    plan = build(_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    res = _budget_result(plan)
    assert res.status == "error", res.detail
    assert cfg_toml.read_text(encoding="utf-8") == "this is [ not valid toml\n"


def test_skills_as_a_string_is_a_shape_error(fake_agent_tools, codex_env):
    """`skills = "x"` (a scalar, not a table) is an unambiguous shape conflict — errors, and the
    file is left byte-for-byte untouched (never rewritten blind)."""
    codex_home, repo = codex_env
    codex_home.mkdir(parents=True)
    cfg_toml = codex_home / "config.toml"
    existing = 'skills = "x"\n'
    cfg_toml.write_text(existing, encoding="utf-8")
    plan = build(_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    res = _budget_result(plan)
    assert res.status == "error", res.detail
    assert "not a table" in res.detail
    assert cfg_toml.read_text(encoding="utf-8") == existing


def test_skills_via_multiline_string_lookalike_is_skipped_untouched(fake_agent_tools, codex_env):
    """A `[skills]`-looking line trapped inside a TOML multiline string must never be treated as
    a real table header — text-level insertion there would corrupt the string. Reported as a
    permanent, remediation-bearing skip (never an apply-failing error, never a silent rewrite),
    and drift must classify it the SAME way — not a permanent 'missing (apply sets N)' that apply
    will never actually resolve (review finding: apply and drift disagreeing on actionability)."""
    codex_home, repo = codex_env
    codex_home.mkdir(parents=True)
    cfg_toml = codex_home / "config.toml"
    existing = (
        'instructions = """\n'
        "Example:\n"
        "[skills]\n"
        "enabled = true\n"
        "[hooks]\n"
        '"""\n'
        'model = "gpt-5"\n'
    )
    cfg_toml.write_text(existing, encoding="utf-8")
    plan = build(_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    res = _budget_result(plan)
    assert res.status == "skipped", res.detail
    assert "multiline string" in res.detail
    assert cfg_toml.read_text(encoding="utf-8") == existing
    drift = detect(plan)
    items = [i for i in drift.items if i.item == "codex-skills-budget"]
    assert items and items[0].direction == "modified" and "multiline string" in items[0].detail, items


def test_multiline_lookalikes_do_not_false_positive(fake_agent_tools, codex_env):
    """A `\"\"\"`/`'''` substring inside a comment, or balanced on one line inside an ordinary
    string, is NOT a genuine multiline string — the write must proceed normally rather than
    refusing on a false diagnosis (review finding: a raw whole-file substring check over-refuses)."""
    codex_home, repo = codex_env
    codex_home.mkdir(parents=True)
    cfg_toml = codex_home / "config.toml"
    existing = (
        '# examples below use """ for block quotes\n'
        "tip = 'print(\"\"\"hello\"\"\")'\n"
        'x = """single-line, balanced"""\n'
    )
    cfg_toml.write_text(existing, encoding="utf-8")
    plan = build(_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    res = _budget_result(plan)
    assert res.status in {"updated", "backed_up"}, res.detail
    assert _read_toml(cfg_toml)["skills"]["max_context_tokens"] == 50000


@pytest.mark.parametrize("existing", ['skills.enabled = true\n', 'skills = { enabled = true }\n'])
def test_skills_via_dotted_or_inline_table_is_skipped_untouched(fake_agent_tools, codex_env, existing):
    """`skills` already defined via a dotted root key or an inline table (no literal [skills]
    header) cannot be safely appended into by the text-level editor — a second [skills] header
    would collide as a duplicate TOML table. Reported as a permanent, remediation-bearing skip,
    and drift must classify it the SAME way — not a permanent 'missing (apply sets N)' that apply
    will never actually resolve (review finding: apply and drift disagreeing on actionability)."""
    codex_home, repo = codex_env
    codex_home.mkdir(parents=True)
    cfg_toml = codex_home / "config.toml"
    cfg_toml.write_text(existing, encoding="utf-8")
    plan = build(_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    res = _budget_result(plan)
    assert res.status == "skipped", res.detail
    assert "dotted keys or an inline table" in res.detail
    assert cfg_toml.read_text(encoding="utf-8") == existing
    drift = detect(plan)
    items = [i for i in drift.items if i.item == "codex-skills-budget"]
    assert items and items[0].direction == "modified" and "dotted keys or an inline table" in items[0].detail, items


def test_skills_as_array_of_tables_is_a_shape_error(fake_agent_tools, codex_env):
    """`[[skills]]` (an array of tables, not a table) is a shape conflict distinct from the
    scalar case — errors without rewriting, rather than appending a colliding `[skills]` header."""
    codex_home, repo = codex_env
    codex_home.mkdir(parents=True)
    cfg_toml = codex_home / "config.toml"
    existing = "[[skills]]\nname = \"one\"\n"
    cfg_toml.write_text(existing, encoding="utf-8")
    plan = build(_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    res = _budget_result(plan)
    assert res.status == "error", res.detail
    assert "not a table" in res.detail
    assert cfg_toml.read_text(encoding="utf-8") == existing


# ── validation + schema registry ─────────────────────────────────────────────────────────────
def test_codex_skills_budget_block_validates_good_shapes():
    validate({"version": 1, "harness": {"kind": "codex", "codex_skills_budget": {"max_context_tokens": 90000}}})
    validate({"version": 1, "harness": {"kind": "codex", "codex_skills_budget": {"enabled": False}}})


@pytest.mark.parametrize("bad", ["lots", 0, -5, 1.9, float("inf"), float("-inf"), float("nan"), True])
def test_codex_skills_budget_rejects_bad_max_context_tokens(bad):
    with pytest.raises(ConfigError, match=r"harness\.codex_skills_budget\.max_context_tokens"):
        validate({"version": 1, "harness": {"kind": "codex", "codex_skills_budget": {"max_context_tokens": bad}}})


def test_codex_skills_budget_rejects_bad_enabled():
    with pytest.raises(ConfigError, match=r"harness\.codex_skills_budget\.enabled must be a bool"):
        validate({"version": 1, "harness": {"kind": "codex", "codex_skills_budget": {"enabled": "yes"}}})


def test_codex_skills_budget_rejects_unknown_key():
    with pytest.raises(ConfigError, match=r"harness\.codex_skills_budget"):
        validate({"version": 1, "harness": {"kind": "codex", "codex_skills_budget": {"bogus": True}}})


def test_resolve_skills_budget_tokens_never_raises_on_non_finite_or_bool():
    """Defense in depth for callers that bypass validate() (unit tests construct LoadedConfig
    directly): a non-positive-int value must skip provisioning, never crash."""
    from riglib.codex_skills_budget import resolve_skills_budget_tokens

    for bad in (float("inf"), float("-inf"), float("nan"), 1.9, True, 0, -5):
        assert resolve_skills_budget_tokens({"codex_skills_budget": {"max_context_tokens": bad}}) is None
    assert resolve_skills_budget_tokens({"codex_skills_budget": {"max_context_tokens": 90000}}) == 90000


def test_schema_registry_exposes_codex_skills_budget_keys():
    from riglib.codex_skills_budget import DEFAULT_MAX_CONTEXT_TOKENS
    from riglib.schema import coerce, option_for_key

    enabled = option_for_key("harness.codex_skills_budget.enabled")
    tokens = option_for_key("harness.codex_skills_budget.max_context_tokens")
    assert enabled is not None and enabled.default is True
    assert tokens is not None and tokens.default == 50000 == DEFAULT_MAX_CONTEXT_TOKENS
    assert coerce(enabled, "false") is False
    assert coerce(tokens, "90000") == 90000


def test_json_schema_has_codex_skills_budget_node_in_sync_with_registry():
    from riglib import config_schema

    assert config_schema.schema_file_in_sync(), "schema/rig.schema.json must match the registry"
    node = config_schema.json_schema()["properties"]["harness"]["properties"]["codex_skills_budget"]
    assert node["properties"]["max_context_tokens"]["default"] == 50000
    assert node["additionalProperties"] is False


def test_toml_parser_available_agrees_with_the_runner_probe():
    """Two independent 'can this Python parse TOML' checks (one in the owning module, for the
    plan builder; one in the runner, for the writer/drift) must never diverge — a review finding
    noted duplicating the check is only safe if it is pinned equal."""
    from riglib.actions.toml_edit import _toml_parser
    from riglib.codex_skills_budget import toml_parser_available

    assert toml_parser_available() == (_toml_parser() is not None)
