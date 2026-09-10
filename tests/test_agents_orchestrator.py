"""Repo AGENTS.md managed blocks — orchestrator + autonomous-mode.

Both are spliced into the canonical guide (never created as their own files) so the
session-start agent sees them before long skills load. Apply and drift share the
same splice resolver; the symlink action still owns file creation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from riglib.config import LoadedConfig
from riglib.drift import detect
from riglib.plan import Action, InstallPlan, build
from riglib.actions.runner import run_plan


ORCH_BEGIN = "# >>> rig-managed orchestrator (do not edit) >>>"
ORCH_END = "# <<< rig-managed orchestrator (do not edit) <<<"
AUTO_BEGIN = "# >>> rig-managed autonomous-mode (do not edit) >>>"
AUTO_END = "# <<< rig-managed autonomous-mode (do not edit) <<<"

USER_BODY = (
    "# USER-BODY-TOKEN\n"
    "## Pricing figures come from the business\n"
    "## Visual fidelity checks against Figma\n"
)

BLOCK_KINDS = (
    "provision_agents_orchestrator",
    "provision_agents_autonomous_mode",
)


def _cfg(data: dict, repo: Path) -> LoadedConfig:
    return LoadedConfig(data={"version": 1, **data}, repo_root=repo)


def _action(repo: Path, kind: str, item: str) -> Action:
    return Action(
        kind=kind,
        category="agents_md",
        item=item,
        source=repo,
        target=repo,
        options={},
    )


def _orch_action(repo: Path) -> Action:
    return _action(repo, "provision_agents_orchestrator", "orchestrator")


def _auto_action(repo: Path) -> Action:
    return _action(repo, "provision_agents_autonomous_mode", "autonomous-mode")


def _blocks_plan(repo: Path, on_conflict: str = "backup") -> InstallPlan:
    plan = InstallPlan(on_conflict=on_conflict)
    plan.actions.append(_orch_action(repo))
    plan.actions.append(_auto_action(repo))
    return plan


def _apply_blocks(repo: Path, on_conflict: str = "backup"):
    report = run_plan(_blocks_plan(repo, on_conflict=on_conflict))
    return report.results


def _apply_kind(repo: Path, action: Action, on_conflict: str = "backup"):
    plan = InstallPlan(on_conflict=on_conflict)
    plan.actions.append(action)
    return run_plan(plan).results[0]


def _full_plan(fake_agent_tools: Path, repo: Path) -> InstallPlan:
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    return build(_cfg({"skills": {"enabled": False}}, repo), cat, project_type="cli")


def _managed_block(text: str, begin: str, end: str) -> str:
    start = text.index(begin)
    stop = text.index(end) + len(end)
    return text[start:stop]


def _assert_gh_delegation(text: str) -> None:
    assert "gh" in text
    for token in ("list", "view", "pr", "api", "run"):
        assert token in text, token
    assert "gh ship" in text
    assert "tg --tag report" in text
    assert "quote the user's original words" in text
    assert "review just-ask" in text
    assert "not in prod" in text
    assert "rig apply commit" in text
    assert "merge is not done" in text
    assert "SHA/on-disk is not a proof" in text
    assert "yield is not done" in text
    assert "live-proven" in text


def _assert_both_blocks(text: str) -> None:
    assert ORCH_BEGIN in text and ORCH_END in text
    assert AUTO_BEGIN in text and AUTO_END in text
    _assert_gh_delegation(text)
    assert "Skill: autonomous-mode" in text


def test_apply_inserts_orchestrator_block_preserving_user_body(tmp_path):
    (tmp_path / "AGENTS.md").write_text(USER_BODY, encoding="utf-8")
    results = _apply_blocks(tmp_path)
    assert all(r.status != "error" for r in results), [r.detail for r in results]
    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "# USER-BODY-TOKEN" in text
    assert "## Pricing figures come from the business" in text
    assert "## Visual fidelity checks against Figma" in text
    _assert_both_blocks(text)


def test_reapply_is_skipped(tmp_path):
    (tmp_path / "AGENTS.md").write_text(USER_BODY, encoding="utf-8")
    first = _apply_blocks(tmp_path)
    assert all(r.status != "error" for r in first), [r.detail for r in first]
    second = _apply_blocks(tmp_path)
    assert all(r.status == "skipped" for r in second), [r.detail for r in second]


def test_does_not_clobber_long_unique_body(tmp_path):
    lines = [f"USER-LINE-{i:03d}-UNIQUE-PROSE\n" for i in range(100)]
    (tmp_path / "AGENTS.md").write_text("".join(lines), encoding="utf-8")
    results = _apply_blocks(tmp_path)
    assert all(r.status != "error" for r in results), [r.detail for r in results]
    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    outside = text
    outside = outside.replace(_managed_block(outside, ORCH_BEGIN, ORCH_END), "")
    outside = outside.replace(_managed_block(outside, AUTO_BEGIN, AUTO_END), "")
    for line in lines:
        assert line in outside


def test_drift_missing_then_clean_then_stale_rewritten(tmp_path):
    (tmp_path / "AGENTS.md").write_text(USER_BODY, encoding="utf-8")
    plan = _blocks_plan(tmp_path)
    before = detect(plan)
    missing_items = {
        i.item
        for i in before.items
        if i.category == "agents_md" and i.direction == "missing"
    }
    assert "orchestrator" in missing_items
    assert "autonomous-mode" in missing_items

    _apply_blocks(tmp_path)
    clean = detect(plan)
    assert not any(
        i.category == "agents_md" and i.item in {"orchestrator", "autonomous-mode"}
        for i in clean.items
    ), [i.detail for i in clean.items if i.category == "agents_md"]

    path = tmp_path / "AGENTS.md"
    stale = path.read_text(encoding="utf-8").replace("Orchestrator", "STALE-ORCH", 1)
    assert "STALE-ORCH" in stale
    path.write_text(stale, encoding="utf-8")
    drifted = detect(plan)
    assert any(
        i.category == "agents_md" and i.item == "orchestrator" and i.direction == "modified"
        for i in drifted.items
    ), [(i.direction, i.item, i.detail) for i in drifted.items]

    rewritten = _apply_blocks(tmp_path)
    assert all(r.status != "error" for r in rewritten), [r.detail for r in rewritten]
    text = path.read_text(encoding="utf-8")
    assert "STALE-ORCH" not in text
    _assert_both_blocks(text)
    assert not any(
        i.category == "agents_md" and i.item in {"orchestrator", "autonomous-mode"}
        for i in detect(plan).items
    )


@pytest.mark.parametrize("block", [{"enabled": False}, {"symlink": False}])
def test_plan_opt_out_omits_symlink_and_orchestrator(fake_agent_tools, tmp_path, block):
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _cfg({"agents_md": block, "skills": {"enabled": False}}, tmp_path),
        cat,
        project_type="cli",
    )
    kinds = {a.kind for a in plan.actions}
    assert "provision_agents_symlink" not in kinds
    assert "provision_agents_orchestrator" not in kinds
    assert "provision_agents_autonomous_mode" not in kinds


def test_default_on_plan_places_orchestrator_after_symlink(fake_agent_tools, tmp_path):
    plan = _full_plan(fake_agent_tools, tmp_path)
    kinds = [
        a.kind
        for a in plan.actions
        if a.kind in {"provision_agents_symlink", *BLOCK_KINDS}
    ]
    assert kinds == [
        "provision_agents_symlink",
        "provision_agents_orchestrator",
        "provision_agents_autonomous_mode",
    ]
    orch = next(a for a in plan.actions if a.kind == "provision_agents_orchestrator")
    assert orch.category == "agents_md" and orch.item == "orchestrator"
    assert orch.source == tmp_path and orch.target == tmp_path
    auto = next(a for a in plan.actions if a.kind == "provision_agents_autonomous_mode")
    assert auto.category == "agents_md" and auto.item == "autonomous-mode"
    assert auto.source == tmp_path and auto.target == tmp_path


def test_orchestrator_alone_skips_when_no_file(tmp_path):
    res = _apply_kind(tmp_path, _orch_action(tmp_path))
    assert res.status == "skipped", res.detail
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / "CLAUDE.md").exists()


def test_autonomous_alone_skips_when_no_file(tmp_path):
    res = _apply_kind(tmp_path, _auto_action(tmp_path))
    assert res.status == "skipped", res.detail
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / "CLAUDE.md").exists()


def test_full_plan_on_empty_repo_creates_then_splices(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = _full_plan(fake_agent_tools, repo)
    report = run_plan(plan)
    splices = [r for r in report.results if r.action.kind in BLOCK_KINDS]
    assert {r.action.kind for r in splices} == set(BLOCK_KINDS)
    assert all(r.status != "error" for r in splices), [r.detail for r in splices]
    text = (repo / "AGENTS.md").read_text(encoding="utf-8")
    _assert_both_blocks(text)
    assert (repo / "CLAUDE.md").is_symlink()


def test_divergent_real_pair_is_not_spliced(tmp_path):
    """Both-real-different is a symlink conflict; splices must not widen the divergence."""
    agents = tmp_path / "AGENTS.md"
    claude = tmp_path / "CLAUDE.md"
    agents.write_text("# AGENTS-ONLY-TOKEN\n", encoding="utf-8")
    claude.write_text("# CLAUDE-ONLY-TOKEN\n", encoding="utf-8")
    results = _apply_blocks(tmp_path)
    assert all(r.status == "skipped" for r in results), [r.detail for r in results]
    assert agents.read_text(encoding="utf-8") == "# AGENTS-ONLY-TOKEN\n"
    assert claude.read_text(encoding="utf-8") == "# CLAUDE-ONLY-TOKEN\n"


def test_foreign_agents_symlink_is_not_spliced(tmp_path):
    """A foreign AGENTS.md link is a symlink-action conflict; splices must not follow it."""
    guide = tmp_path / "docs" / "guide.md"
    guide.parent.mkdir()
    guide.write_text("# FOREIGN-GUIDE-TOKEN\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").symlink_to("docs/guide.md")
    results = _apply_blocks(tmp_path)
    assert all(r.status == "skipped" for r in results), [r.detail for r in results]
    assert (tmp_path / "AGENTS.md").is_symlink()
    assert str((tmp_path / "AGENTS.md").readlink()) == "docs/guide.md"
    assert guide.read_text(encoding="utf-8") == "# FOREIGN-GUIDE-TOKEN\n"
    assert not (tmp_path / "CLAUDE.md").exists()


def test_writes_through_claude_md_canonical(tmp_path):
    claude = tmp_path / "CLAUDE.md"
    claude.write_text(USER_BODY, encoding="utf-8")
    (tmp_path / "AGENTS.md").symlink_to("CLAUDE.md")
    results = _apply_blocks(tmp_path)
    assert all(r.status != "error" for r in results), [r.detail for r in results]
    assert (tmp_path / "AGENTS.md").is_symlink()
    text = claude.read_text(encoding="utf-8")
    assert "# USER-BODY-TOKEN" in text
    _assert_both_blocks(text)


def test_unbalanced_markers_error_leaves_file_untouched(tmp_path):
    (tmp_path / "AGENTS.md").write_text(USER_BODY, encoding="utf-8")
    first = _apply_blocks(tmp_path)
    assert all(r.status != "error" for r in first), [r.detail for r in first]
    path = tmp_path / "AGENTS.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace(ORCH_END + "\n", "").replace(ORCH_END, ""), encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    res = _apply_kind(tmp_path, _orch_action(tmp_path))
    assert res.status == "error", res.detail
    assert path.read_text(encoding="utf-8") == before


def test_block_is_at_most_12_lines(tmp_path):
    (tmp_path / "AGENTS.md").write_text(USER_BODY, encoding="utf-8")
    results = _apply_blocks(tmp_path)
    assert all(r.status != "error" for r in results), [r.detail for r in results]
    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    orch = _managed_block(text, ORCH_BEGIN, ORCH_END)
    auto = _managed_block(text, AUTO_BEGIN, AUTO_END)
    assert len(orch.splitlines()) <= 12
    assert len(auto.splitlines()) <= 12
