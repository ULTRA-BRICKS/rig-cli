"""README ecosystem-block provisioning (rig-cli#308).

Every ecosystem repo hand-copies an "## Ecosystem" README section listing sibling tools and it
silently drifts (only copy-paste keeps it current). ``ecosystem_readme:`` lets ``rig apply``
write/update a marker-delimited section from ONE canonical, in-code registry
(:mod:`riglib.ecosystem_readme`), the same marker-splice discipline the codex execpolicy .rules
block and the global-excludes/ship-delegator blocks already use: idempotent + additive (content
outside the markers survives byte-for-byte), backup-on-conflict, and two-way drift via
``rig status``.
"""

from __future__ import annotations

from pathlib import Path

from riglib import config as configmod
from riglib.actions import run_plan
from riglib.actions.runner import (
    ECOSYSTEM_README_BEGIN_MARKER,
    ECOSYSTEM_README_END_MARKER,
)
from riglib.catalog import Catalog
from riglib.config import ConfigError, LoadedConfig
from riglib.drift import detect
from riglib.ecosystem_readme import ECOSYSTEM_TOOLS, render_ecosystem_block
from riglib.plan import build

REPO_ROOT = Path(__file__).resolve().parent.parent


def _cfg(repo: Path, source: Path, **ecosystem_readme) -> LoadedConfig:
    data = {
        "agent_tools_source": str(source),
        "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
        "ci": {"enabled": False}, "mcp": {"enabled": False},
        "git_hooks": {"dispatcher": {"enabled": False}},
        "harness": {"enabled": False},
    }
    if ecosystem_readme:
        data["ecosystem_readme"] = ecosystem_readme
    return LoadedConfig(data=data, repo_root=repo)


def _ecosystem_action(plan):
    acts = [a for a in plan.actions if a.kind == "apply_ecosystem_readme"]
    assert acts, "expected an apply_ecosystem_readme action"
    return acts[0]


# ── renderer ─────────────────────────────────────────────────────────────────────────
def test_registry_is_nonempty_and_well_formed():
    assert len(ECOSYSTEM_TOOLS) >= 5
    for tool in ECOSYSTEM_TOOLS:
        assert tool.name and tool.url.startswith("http") and tool.description


def test_render_block_has_markers_and_every_tool():
    block = render_ecosystem_block()
    assert block.startswith(ECOSYSTEM_README_BEGIN_MARKER)
    assert block.rstrip().endswith(ECOSYSTEM_README_END_MARKER)
    for tool in ECOSYSTEM_TOOLS:
        assert tool.name in block
        assert tool.url in block


def test_render_block_is_deterministic():
    assert render_ecosystem_block() == render_ecosystem_block()


# ── plan ─────────────────────────────────────────────────────────────────────────────
def test_plan_emits_no_action_when_disabled_or_absent(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    plan = build(_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert not [a for a in plan.actions if a.kind == "apply_ecosystem_readme"]

    plan2 = build(_cfg(repo, fake_agent_tools, enabled=False), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert not [a for a in plan2.actions if a.kind == "apply_ecosystem_readme"]


def test_plan_notes_an_orphaned_block_when_disabled(fake_agent_tools, tmp_path):
    # rig never auto-deletes: a block left over from a prior enabled apply must not silently keep
    # claiming (via its own in-block notice) that it is "kept in sync" once the feature is off.
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "README.md").write_text(
        f"{ECOSYSTEM_README_BEGIN_MARKER}\n## Ecosystem\n\n- x\n{ECOSYSTEM_README_END_MARKER}\n",
        encoding="utf-8",
    )
    plan = build(_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert any("ecosystem_readme" in n and "disabled" in n for n in plan.notes)


def test_plan_no_orphan_note_when_no_block_on_disk(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    plan = build(_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert not any("ecosystem_readme" in n for n in plan.notes)


def test_plan_emits_action_targeting_readme_when_enabled(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    plan = build(_cfg(repo, fake_agent_tools, enabled=True), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    act = _ecosystem_action(plan)
    assert act.target == repo / "README.md"


def test_plan_honors_custom_target(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    plan = build(_cfg(repo, fake_agent_tools, enabled=True, target="docs/README.md"),
                 Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    act = _ecosystem_action(plan)
    assert act.target == repo / "docs" / "README.md"


# ── apply (marker block) ────────────────────────────────────────────────────────────
def test_apply_creates_readme_and_is_idempotent(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    readme = repo / "README.md"
    plan = build(_cfg(repo, fake_agent_tools, enabled=True), Catalog.scan(str(fake_agent_tools)), project_type="unknown")

    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    text = readme.read_text(encoding="utf-8")
    assert ECOSYSTEM_README_BEGIN_MARKER in text and ECOSYSTEM_README_END_MARKER in text
    for tool in ECOSYSTEM_TOOLS:
        assert tool.name in text

    # re-apply is a byte-identical no-op
    report2 = run_plan(build(_cfg(repo, fake_agent_tools, enabled=True), Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    assert all(r.status == "skipped" for r in report2.results if r.action.kind == "apply_ecosystem_readme")
    assert readme.read_text(encoding="utf-8") == text


def test_apply_inserts_into_existing_readme_preserving_content(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    readme = repo / "README.md"
    readme.write_text("# my-project\n\nSome hand-written docs.\n", encoding="utf-8")
    plan = build(_cfg(repo, fake_agent_tools, enabled=True), Catalog.scan(str(fake_agent_tools)), project_type="unknown")

    assert not run_plan(plan).errors
    text = readme.read_text(encoding="utf-8")
    assert "# my-project" in text
    assert "Some hand-written docs." in text
    assert ECOSYSTEM_README_BEGIN_MARKER in text


def test_apply_backs_up_before_rewrite_under_backup_policy(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    readme = repo / "README.md"
    stale = f"{ECOSYSTEM_README_BEGIN_MARKER}\n## Ecosystem\n\n- old\n{ECOSYSTEM_README_END_MARKER}\n"
    readme.write_text(stale, encoding="utf-8")
    cfg = _cfg(repo, fake_agent_tools, enabled=True)
    cfg.data["defaults"] = {"on_conflict": "backup"}
    report = run_plan(build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    res = [r for r in report.results if r.action.kind == "apply_ecosystem_readme"][0]
    assert res.status == "backed_up"
    assert list(repo.glob("README.md.rig-bak-*"))


def test_apply_leaves_stale_block_untouched_under_skip(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    readme = repo / "README.md"
    stale = f"{ECOSYSTEM_README_BEGIN_MARKER}\n## Ecosystem\n\n- old\n{ECOSYSTEM_README_END_MARKER}\n"
    readme.write_text(stale, encoding="utf-8")
    cfg = _cfg(repo, fake_agent_tools, enabled=True)
    cfg.data["defaults"] = {"on_conflict": "skip"}
    report = run_plan(build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    res = [r for r in report.results if r.action.kind == "apply_ecosystem_readme"][0]
    assert res.status == "skipped"
    assert readme.read_text(encoding="utf-8") == stale  # untouched


# ── drift ────────────────────────────────────────────────────────────────────────────
def test_drift_missing_then_converged_then_stale(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    readme = repo / "README.md"
    plan = build(_cfg(repo, fake_agent_tools, enabled=True), Catalog.scan(str(fake_agent_tools)), project_type="unknown")

    missing = [d for d in detect(plan).by_direction("missing") if d.category == "ecosystem_readme"]
    assert any("ecosystem" in d.detail.lower() for d in missing)

    assert not run_plan(plan).errors
    assert not [d for d in detect(plan).items if d.category == "ecosystem_readme"]

    text = readme.read_text(encoding="utf-8").replace(ECOSYSTEM_TOOLS[0].name, "renamed-tool", 1)
    readme.write_text(text, encoding="utf-8")
    mod = [d for d in detect(plan).by_direction("modified") if d.category == "ecosystem_readme"]
    assert any("stale" in d.detail for d in mod)


def test_drift_unbalanced_markers_is_modified(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    readme = repo / "README.md"
    readme.write_text(f"{ECOSYSTEM_README_BEGIN_MARKER}\n## Ecosystem\n\n- x\n", encoding="utf-8")  # no end marker
    plan = build(_cfg(repo, fake_agent_tools, enabled=True), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    mod = [d for d in detect(plan).by_direction("modified") if d.category == "ecosystem_readme"]
    assert any("unbalanced" in d.detail for d in mod)


def test_apply_unbalanced_markers_is_a_hard_error(fake_agent_tools, tmp_path):
    # apply refuses to guess at an unbalanced/misordered marker pair — a HAND fix, not a rewrite.
    repo = tmp_path / "repo"; repo.mkdir()
    readme = repo / "README.md"
    readme.write_text(f"{ECOSYSTEM_README_BEGIN_MARKER}\n## Ecosystem\n\n- x\n", encoding="utf-8")  # no end marker
    plan = build(_cfg(repo, fake_agent_tools, enabled=True), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    report = run_plan(plan)
    assert report.errors, "expected apply to hard-error on unbalanced markers"
    assert "unbalanced" in report.errors[0].detail
    # untouched — apply never rewrote the malformed file
    assert readme.read_text(encoding="utf-8") == f"{ECOSYSTEM_README_BEGIN_MARKER}\n## Ecosystem\n\n- x\n"


def test_apply_into_existing_readme_is_idempotent_on_reapply(fake_agent_tools, tmp_path):
    # the insert-preserving-content path (test_apply_inserts_into_existing_readme_preserving_content
    # above) only proves the FIRST apply; this proves the second apply against that same file is
    # ALSO a no-op, not just a fresh-file re-apply (test_apply_creates_readme_and_is_idempotent).
    repo = tmp_path / "repo"; repo.mkdir()
    readme = repo / "README.md"
    readme.write_text("# my-project\n\nSome hand-written docs.\n", encoding="utf-8")
    cfg = _cfg(repo, fake_agent_tools, enabled=True)

    plan1 = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert not run_plan(plan1).errors
    text_after_first = readme.read_text(encoding="utf-8")

    plan2 = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    report2 = run_plan(plan2)
    assert all(r.status == "skipped" for r in report2.results if r.action.kind == "apply_ecosystem_readme")
    assert readme.read_text(encoding="utf-8") == text_after_first
    assert "# my-project" in text_after_first
    assert "Some hand-written docs." in text_after_first


# ── config validation ───────────────────────────────────────────────────────────────
def test_validate_rejects_unknown_key():
    try:
        configmod.validate({"ecosystem_readme": {"enabled": True, "bogus": 1}})
        assert False, "expected ConfigError"
    except ConfigError as exc:
        assert "bogus" in str(exc)


def test_validate_rejects_non_bool_enabled():
    try:
        configmod.validate({"ecosystem_readme": {"enabled": "yes"}})
        assert False, "expected ConfigError"
    except ConfigError as exc:
        assert "enabled" in str(exc)


def test_validate_rejects_target_escaping_the_repo():
    for bad_target in ("../../etc/passwd", "/etc/passwd", "docs/../../x", ".git/hooks/pre-commit"):
        try:
            configmod.validate({"ecosystem_readme": {"enabled": True, "target": bad_target}})
            assert False, f"expected ConfigError for {bad_target!r}"
        except ConfigError as exc:
            assert "ecosystem_readme.target" in str(exc)


def test_validate_accepts_valid_block_and_absent_or_empty_block():
    configmod.validate({"ecosystem_readme": {"enabled": True, "target": "docs/README.md"}})
    configmod.validate({})
    configmod.validate({"ecosystem_readme": {}})


def test_plan_raises_when_target_escapes_repo(fake_agent_tools, tmp_path):
    from riglib.plan import PlanError

    repo = tmp_path / "repo"; repo.mkdir()
    cfg = _cfg(repo, fake_agent_tools, enabled=True, target="../outside.md")
    try:
        build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
        assert False, "expected PlanError"
    except PlanError as exc:
        assert "ecosystem_readme.target" in str(exc)


# ── dogfood: rig-cli's own README stays byte-identical to the renderer ───────────────
def test_rig_cli_readme_block_matches_the_canonical_renderer():
    # rig-cli's own README.md wraps its "## Ecosystem" section in the managed markers and enables
    # ecosystem_readme in its own rig.yaml (dogfooding #308). If this test ever fails, someone
    # edited the registry OR the README by hand without keeping the other in sync — `rig apply
    # commit` in this repo would then dirty the working tree on the very next run.
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    start = readme.index(ECOSYSTEM_README_BEGIN_MARKER)
    end = readme.index(ECOSYSTEM_README_END_MARKER) + len(ECOSYSTEM_README_END_MARKER)
    assert readme[start:end] == render_ecosystem_block()
