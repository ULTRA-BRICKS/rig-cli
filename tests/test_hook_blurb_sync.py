"""Tests for riglib.hook_blurb_sync (agent-tools#504)."""

from __future__ import annotations

from pathlib import Path

from riglib.hook_blurb_sync import sync_agent_hook_blurbs


def _make_hook(agent_tools_root: Path, name: str, blurb: str | None) -> None:
    hook_dir = agent_tools_root / "agent-hooks" / name
    hook_dir.mkdir(parents=True, exist_ok=True)
    (hook_dir / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    if blurb is not None:
        (hook_dir / "blurb.md").write_text(blurb, encoding="utf-8")


def test_copies_blurb_for_hook_that_has_one(tmp_path: Path) -> None:
    root = tmp_path / "agent-tools"
    _make_hook(root, "orchestrator-stays-thin", "- policy summary\n")
    target = tmp_path / "blurbs"

    changed = sync_agent_hook_blurbs(root, target)

    written = target / "orchestrator-stays-thin.md"
    assert changed == [written]
    assert written.read_text(encoding="utf-8") == "- policy summary\n"


def test_skips_hook_with_no_blurb(tmp_path: Path) -> None:
    root = tmp_path / "agent-tools"
    _make_hook(root, "pkill-guard", blurb=None)
    target = tmp_path / "blurbs"

    changed = sync_agent_hook_blurbs(root, target)

    assert changed == []
    assert not target.exists()


def test_idempotent_no_rewrite_when_content_unchanged(tmp_path: Path) -> None:
    root = tmp_path / "agent-tools"
    _make_hook(root, "background-subagent-gate", "- dispatch in the background\n")
    target = tmp_path / "blurbs"

    first = sync_agent_hook_blurbs(root, target)
    assert len(first) == 1
    written = first[0]
    mtime_after_first = written.stat().st_mtime_ns

    second = sync_agent_hook_blurbs(root, target)

    assert second == []
    assert written.stat().st_mtime_ns == mtime_after_first


def test_rewrites_when_source_content_changes(tmp_path: Path) -> None:
    root = tmp_path / "agent-tools"
    hook_dir = root / "agent-hooks" / "orchestrator-stays-thin"
    _make_hook(root, "orchestrator-stays-thin", "- v1\n")
    target = tmp_path / "blurbs"

    sync_agent_hook_blurbs(root, target)
    (hook_dir / "blurb.md").write_text("- v2\n", encoding="utf-8")
    changed = sync_agent_hook_blurbs(root, target)

    written = target / "orchestrator-stays-thin.md"
    assert changed == [written]
    assert written.read_text(encoding="utf-8") == "- v2\n"


def test_multiple_hooks_sorted_and_mixed_blurb_presence(tmp_path: Path) -> None:
    root = tmp_path / "agent-tools"
    _make_hook(root, "orchestrator-stays-thin", "- a\n")
    _make_hook(root, "background-subagent-gate", "- b\n")
    _make_hook(root, "pkill-guard", blurb=None)
    target = tmp_path / "blurbs"

    changed = sync_agent_hook_blurbs(root, target)

    assert sorted(p.name for p in changed) == [
        "background-subagent-gate.md",
        "orchestrator-stays-thin.md",
    ]
    assert not (target / "pkill-guard.md").exists()


def test_missing_agent_hooks_dir_returns_empty(tmp_path: Path) -> None:
    root = tmp_path / "agent-tools-without-hooks-dir"
    root.mkdir()
    target = tmp_path / "blurbs"

    assert sync_agent_hook_blurbs(root, target) == []
    assert not target.exists()


def test_plan_emits_sync_hook_blurbs_action(fake_agent_tools, tmp_path, monkeypatch):
    from riglib.catalog import Catalog
    from riglib.config import LoadedConfig
    from riglib.plan import build

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "skills": {"enabled": False},
            "agent_hooks": {"all": True},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "harness": {"kind": "claude-code"},
        },
        repo_root=tmp_path,
    )
    plan = build(cfg, cat, project_type="unknown")
    blurb_actions = [a for a in plan.actions if a.kind == "sync_hook_blurbs"]
    assert len(blurb_actions) == 1
    action = blurb_actions[0]
    assert action.source == cat.source
    assert action.target == tmp_path / "home" / ".agents" / "skills" / ".blurbs"


def test_plan_skips_sync_hook_blurbs_when_agent_hooks_disabled(fake_agent_tools, tmp_path, monkeypatch):
    from riglib.catalog import Catalog
    from riglib.config import LoadedConfig
    from riglib.plan import build

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "skills": {"enabled": False},
            "agent_hooks": {"enabled": False},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
        },
        repo_root=tmp_path,
    )
    plan = build(cfg, cat, project_type="unknown")
    assert not [a for a in plan.actions if a.kind == "sync_hook_blurbs"]


def test_do_sync_hook_blurbs_copies_then_is_idempotent(tmp_path):
    from riglib.actions import runner
    from riglib.plan import Action

    root = tmp_path / "agent-tools"
    hook_dir = root / "agent-hooks" / "orchestrator-stays-thin"
    hook_dir.mkdir(parents=True)
    (hook_dir / "blurb.md").write_text("- policy summary\n", encoding="utf-8")
    target = tmp_path / "blurbs"
    action = Action(kind="sync_hook_blurbs", category="agent_hooks", item="blurbs", source=root, target=target)

    first = runner._do_sync_hook_blurbs(action, "backup")
    assert first.status == "created", first.detail
    written = target / "orchestrator-stays-thin.md"
    assert written.read_text(encoding="utf-8") == "- policy summary\n"

    second = runner._do_sync_hook_blurbs(action, "backup")
    assert second.status == "skipped", second.detail
