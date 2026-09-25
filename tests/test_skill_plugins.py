"""``skills.plugins`` — Claude Code plugin-marketplace-sourced skills (rig-cli#590).

A plugin's ``skills/*/SKILL.md`` dirs live under a version-pinned Claude Code plugin cache path
(``~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/``) tracked by
``~/.claude/plugins/installed_plugins.json``. These tests pin the resolve-and-symlink contract
end to end through ``build`` / ``run_plan`` / ``detect`` (hermetic — fake agent-tools + fake
``installed_plugins.json`` + tmp HOME):

- a fresh declaration resolves and links (module-level fixture data + ``resolve_plugin_skills``
  unit coverage lives in this file too, near the top);
- re-applying is idempotent (a no-op ``skipped`` on the second run);
- a simulated plugin version bump (a changed ``installPath`` in the fixture) re-points the
  existing symlink instead of leaving it dangling;
- a plugin skill name colliding with a real agent-tools catalog skill is skipped, never
  silently overriding the catalog copy.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from riglib.actions.runner import run_plan
from riglib.catalog import Catalog
from riglib.config import ConfigError, LoadedConfig, validate
from riglib.drift import detect
from riglib.plan import build
from riglib.plugin_skills import (
    plugin_skill_dirs,
    resolve_plugin_install_path,
    resolve_plugin_skills,
)


def _cfg(data: dict, repo_root: Path) -> LoadedConfig:
    return LoadedConfig(data={"version": 1, **data}, repo_root=repo_root)


def _write_plugin_skill(root: Path, name: str, body: str = "") -> Path:
    d = root / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\n---\n# {name}\n{body}", encoding="utf-8")
    return d


def _write_installed_plugins(path: Path, plugin: str, marketplace: str, install_path: Path, *, scope: str = "user") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    f"{plugin}@{marketplace}": [
                        {
                            "scope": scope,
                            "installPath": str(install_path),
                            "version": install_path.name,
                            "installedAt": "2026-01-01T00:00:00.000Z",
                            "lastUpdated": "2026-01-01T00:00:00.000Z",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )


def _skill_actions(plan, kind: str) -> list:
    return [a for a in plan.actions if a.kind == kind]


# ── unit-level: resolve_plugin_install_path / plugin_skill_dirs ────────────────────────────


def test_resolve_plugin_install_path_prefers_user_scope_over_project():
    installed = {
        "plugins": {
            "superpowers@claude-plugins-official": [
                {"scope": "project", "installPath": "/proj/path", "lastUpdated": "2026-09-01T00:00:00Z"},
                {"scope": "user", "installPath": "/user/path", "lastUpdated": "2026-01-01T00:00:00Z"},
            ]
        }
    }
    path = resolve_plugin_install_path(installed, "superpowers", "claude-plugins-official")
    assert path == Path("/user/path")


def test_resolve_plugin_install_path_breaks_ties_on_most_recent_within_pool():
    installed = {
        "plugins": {
            "superpowers@claude-plugins-official": [
                {"scope": "user", "installPath": "/old", "lastUpdated": "2026-01-01T00:00:00Z"},
                {"scope": "user", "installPath": "/new", "lastUpdated": "2026-06-01T00:00:00Z"},
            ]
        }
    }
    path = resolve_plugin_install_path(installed, "superpowers", "claude-plugins-official")
    assert path == Path("/new")


def test_resolve_plugin_install_path_none_for_unknown_key():
    assert resolve_plugin_install_path({"plugins": {}}, "superpowers", "claude-plugins-official") is None
    assert resolve_plugin_install_path({}, "superpowers", "claude-plugins-official") is None


def test_plugin_skill_dirs_lists_only_dirs_carrying_skill_md(tmp_path):
    root = tmp_path / "plugin-root"
    _write_plugin_skill(root, "brainstorming")
    _write_plugin_skill(root, "writing-plans")
    (root / "skills" / "not-a-skill").mkdir(parents=True)  # no SKILL.md → not a skill
    (root / "skills" / "stray-file.txt").write_text("x", encoding="utf-8")
    dirs = plugin_skill_dirs(root)
    assert [d.name for d in dirs] == ["brainstorming", "writing-plans"]


def test_plugin_skill_dirs_empty_when_no_skills_subdir(tmp_path):
    assert plugin_skill_dirs(tmp_path / "nonexistent") == []


# ── config validation ───────────────────────────────────────────────────────────────────────


def test_config_validate_accepts_a_well_formed_plugins_block():
    validate({"version": 1, "skills": {"plugins": {"items": {"superpowers": {"marketplace": "claude-plugins-official"}}}}})


@pytest.mark.parametrize(
    "items",
    [
        {"superpowers": {"marketplace": 1}},  # not a string
        {"superpowers": {"marketplace": ""}},  # empty string
        {"superpowers": {}},  # missing marketplace
        {"superpowers": "not-a-mapping"},
    ],
)
def test_config_validate_rejects_malformed_plugin_item(items):
    with pytest.raises(ConfigError, match="skills.plugins.items.superpowers"):
        validate({"version": 1, "skills": {"plugins": {"items": items}}})


def test_config_validate_rejects_unknown_key_in_plugins_block():
    with pytest.raises(ConfigError, match="skills.plugins"):
        validate({"version": 1, "skills": {"plugins": {"bogus": 1}}})


# ── end-to-end: build / run_plan / detect ───────────────────────────────────────────────────


def _plugin_cfg(repo: Path, source: Path, install_paths_file: Path) -> LoadedConfig:
    return _cfg(
        {
            "agent_tools_source": str(source),
            "skills": {
                "universal": {"all": True},
                "plugins": {"items": {"superpowers": {"marketplace": "claude-plugins-official"}}},
            },
            "defaults": {"skills_target": str(repo / "skills-out")},
            "agent_hooks": {"enabled": False},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "harness": {"enabled": False, "kind": "claude-code"},
            "permissions": {"enabled": False},
        },
        repo,
    )


@pytest.fixture
def _plugin_env(fake_agent_tools, tmp_path, monkeypatch):
    """A hermetic HOME with a v1 fake plugin install + its installed_plugins.json fixture."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    plugin_root = home / ".claude" / "plugins" / "cache" / "claude-plugins-official" / "superpowers" / "6.4.1"
    _write_plugin_skill(plugin_root, "brainstorming")
    _write_plugin_skill(plugin_root, "writing-plans")
    installed_plugins = home / ".claude" / "plugins" / "installed_plugins.json"
    _write_installed_plugins(installed_plugins, "superpowers", "claude-plugins-official", plugin_root)
    cat = Catalog.scan(str(fake_agent_tools))
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _plugin_cfg(repo, fake_agent_tools, installed_plugins)
    return {
        "home": home, "plugin_root": plugin_root, "installed_plugins": installed_plugins,
        "cat": cat, "repo": repo, "cfg": cfg,
    }


def test_fresh_plugin_declaration_resolves_and_links(_plugin_env):
    """(a) A fresh skills.plugins.items declaration resolves against the fixture
    installed_plugins.json and links every one of the plugin's skills."""
    cfg, cat, repo = _plugin_env["cfg"], _plugin_env["cat"], _plugin_env["repo"]
    plan = build(cfg, cat, project_type="unknown")
    actions = _skill_actions(plan, "link_skill_plugin")
    assert {a.item for a in actions} == {"superpowers/brainstorming", "superpowers/writing-plans"}
    for a in actions:
        assert a.target == repo / "skills-out" / a.target.name
        assert a.source == _plugin_env["plugin_root"] / "skills" / a.target.name

    report = run_plan(plan)
    assert not report.errors, report.errors

    link = repo / "skills-out" / "brainstorming"
    assert link.is_symlink()
    assert link.resolve() == (_plugin_env["plugin_root"] / "skills" / "brainstorming").resolve()

    # rig status: no undeclared/extra skill drift for the plugin-sourced names — they are
    # natively recognized as plan-declared, no skills.known entry needed.
    report_drift = detect(plan, scan_skill_dirs=[repo / "skills-out"])
    extras = [i.item for i in report_drift.by_direction("extra") if i.category == "skills"]
    assert "brainstorming" not in extras
    assert "writing-plans" not in extras


def test_reapplying_is_idempotent(_plugin_env):
    """(b) A second build+apply with nothing changed is a clean no-op (every plugin action
    reports 'skipped', never 're-created')."""
    cfg, cat = _plugin_env["cfg"], _plugin_env["cat"]
    plan = build(cfg, cat, project_type="unknown")
    first = run_plan(plan)
    assert not first.errors

    plan2 = build(cfg, cat, project_type="unknown")
    second = run_plan(plan2)
    plugin_results = [r for r in second.results if r.action.kind == "link_skill_plugin"]
    assert plugin_results
    assert all(r.status == "skipped" for r in plugin_results), [r.detail for r in plugin_results]


def test_plugin_version_bump_repoints_the_existing_symlink(_plugin_env):
    """(c) installed_plugins.json's installPath moving to a new version dir (the exact shape of
    a real plugin auto-update) re-points the existing skills_target symlink at the NEXT apply
    instead of leaving it dangling at the old version path."""
    cfg, cat, repo = _plugin_env["cfg"], _plugin_env["cat"], _plugin_env["repo"]
    plan = build(cfg, cat, project_type="unknown")
    run_plan(plan)
    link = repo / "skills-out" / "brainstorming"
    assert link.resolve() == (_plugin_env["plugin_root"] / "skills" / "brainstorming").resolve()

    # simulate the plugin marketplace auto-updating to a new version
    new_root = _plugin_env["home"] / ".claude" / "plugins" / "cache" / "claude-plugins-official" / "superpowers" / "6.5.0"
    _write_plugin_skill(new_root, "brainstorming", body="updated content\n")
    _write_plugin_skill(new_root, "writing-plans")
    _write_installed_plugins(_plugin_env["installed_plugins"], "superpowers", "claude-plugins-official", new_root)

    plan2 = build(cfg, cat, project_type="unknown")
    report2 = run_plan(plan2)
    bs = [r for r in report2.results if r.action.kind == "link_skill_plugin" and r.action.item.endswith("brainstorming")]
    assert bs and bs[0].status == "updated", bs

    assert link.is_symlink()
    assert link.resolve() == (new_root / "skills" / "brainstorming").resolve()
    assert "updated content" in link.joinpath("SKILL.md").read_text(encoding="utf-8")

    # no dangling/stale drift after the re-point
    report_drift = detect(plan2, scan_skill_dirs=[repo / "skills-out"])
    stale = [i for i in (report_drift.by_direction("missing") + report_drift.by_direction("modified")) if i.category == "skills"]
    assert not stale, stale


def test_plugin_skill_name_colliding_with_catalog_skill_is_skipped(fake_agent_tools, tmp_path, monkeypatch):
    """(d) A plugin skill sharing a name with a real agent-tools catalog skill (the
    ``systematic-debugging`` precedent) is skipped — the catalog copy is never shadowed or
    overwritten by a plugin-sourced symlink."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    plugin_root = home / ".claude" / "plugins" / "cache" / "claude-plugins-official" / "superpowers" / "6.4.1"
    # "naming" is a real universal catalog skill in fake_agent_tools — collide with it.
    _write_plugin_skill(plugin_root, "naming", body="plugin version — must NOT win\n")
    _write_plugin_skill(plugin_root, "brainstorming")
    installed_plugins = home / ".claude" / "plugins" / "installed_plugins.json"
    _write_installed_plugins(installed_plugins, "superpowers", "claude-plugins-official", plugin_root)

    cat = Catalog.scan(str(fake_agent_tools))
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _plugin_cfg(repo, fake_agent_tools, installed_plugins)
    plan = build(cfg, cat, project_type="unknown")

    plugin_items = {a.item for a in _skill_actions(plan, "link_skill_plugin")}
    assert plugin_items == {"superpowers/brainstorming"}  # "naming" excluded
    assert any(
        "naming" in n and "collides with an existing catalog skill" in n for n in plan.notes
    ), plan.notes

    report = run_plan(plan)
    assert not report.errors, report.errors

    naming = repo / "skills-out" / "naming"
    assert not naming.is_symlink()  # the real catalog copy, untouched
    assert "plugin version" not in naming.joinpath("SKILL.md").read_text(encoding="utf-8")


def test_two_plugins_shipping_a_same_named_skill_only_one_wins(fake_agent_tools, tmp_path, monkeypatch):
    """A cross-plugin collision (two DIFFERENT declared plugins ship a skill of the same name)
    must not emit two link_skill_plugin actions at the same target with different sources —
    that would flip-flop which one 'wins' on every apply (never idempotent) and leave the loser
    permanently 'modified' in drift. The alphabetically-first plugin name wins; the loser is
    recorded in plan.notes, never silently applied."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    root_a = home / ".claude" / "plugins" / "cache" / "mp" / "a-plugin" / "1.0.0"
    root_b = home / ".claude" / "plugins" / "cache" / "mp" / "b-plugin" / "1.0.0"
    _write_plugin_skill(root_a, "shared-skill", body="from a-plugin\n")
    _write_plugin_skill(root_b, "shared-skill", body="from b-plugin\n")
    installed_plugins = home / ".claude" / "plugins" / "installed_plugins.json"
    installed_plugins.parent.mkdir(parents=True, exist_ok=True)
    installed_plugins.write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "a-plugin@mp": [{"scope": "user", "installPath": str(root_a), "lastUpdated": "2026-01-01T00:00:00Z"}],
                    "b-plugin@mp": [{"scope": "user", "installPath": str(root_b), "lastUpdated": "2026-01-01T00:00:00Z"}],
                },
            }
        ),
        encoding="utf-8",
    )
    cat = Catalog.scan(str(fake_agent_tools))
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _cfg(
        {
            "agent_tools_source": str(fake_agent_tools),
            "skills": {
                "universal": {"all": False},
                "plugins": {"items": {"a-plugin": {"marketplace": "mp"}, "b-plugin": {"marketplace": "mp"}}},
            },
            "defaults": {"skills_target": str(repo / "skills-out")},
            "agent_hooks": {"enabled": False}, "ci": {"enabled": False}, "mcp": {"enabled": False},
            "harness": {"enabled": False, "kind": "claude-code"}, "permissions": {"enabled": False},
        },
        repo,
    )

    plan = build(cfg, cat, project_type="unknown")
    plugin_actions = _skill_actions(plan, "link_skill_plugin")
    # exactly ONE action for "shared-skill" — never two competing for the same target
    assert [a.item for a in plugin_actions] == ["a-plugin/shared-skill"]  # "a" sorts before "b"
    assert any(
        "shared-skill" in n and "b-plugin" in n and "collides with the same-named skill" in n
        for n in plan.notes
    ), plan.notes

    report = run_plan(plan)
    assert not report.errors, report.errors
    link = repo / "skills-out" / "shared-skill"
    assert link.resolve() == (root_a / "skills" / "shared-skill").resolve()

    # idempotent: a second apply never flips to b-plugin or reports drift
    plan2 = build(cfg, cat, project_type="unknown")
    report2 = run_plan(plan2)
    results = [r for r in report2.results if r.action.kind == "link_skill_plugin"]
    assert results and all(r.status == "skipped" for r in results), [r.detail for r in results]
    report_drift = detect(plan2, scan_skill_dirs=[repo / "skills-out"])
    stale = [i for i in (report_drift.by_direction("missing") + report_drift.by_direction("modified")) if i.category == "skills"]
    assert not stale, stale


def test_plugin_not_installed_on_this_machine_is_a_note_not_a_crash(fake_agent_tools, tmp_path, monkeypatch):
    """A declared plugin absent from installed_plugins.json (never installed on this machine, or
    a typo'd marketplace/plugin name) degrades to a plan note — never a hard `rig apply` failure."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    # installed_plugins.json exists but lists nothing for "superpowers"
    installed_plugins = home / ".claude" / "plugins" / "installed_plugins.json"
    installed_plugins.parent.mkdir(parents=True)
    installed_plugins.write_text(json.dumps({"version": 2, "plugins": {}}), encoding="utf-8")

    cat = Catalog.scan(str(fake_agent_tools))
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _plugin_cfg(repo, fake_agent_tools, installed_plugins)
    plan = build(cfg, cat, project_type="unknown")

    assert not _skill_actions(plan, "link_skill_plugin")
    assert any("not found in" in n and "superpowers" in n for n in plan.notes), plan.notes
    report = run_plan(plan)
    assert not report.errors


def test_no_plugins_declared_is_a_true_no_op(fake_agent_tools, tmp_path, monkeypatch):
    """The common case (no skills.plugins block at all) emits zero link_skill_plugin actions and
    reads no installed_plugins.json — never a surprise action for a repo that never opted in."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    cat = Catalog.scan(str(fake_agent_tools))
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _cfg(
        {
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"universal": {"all": True}},
            "defaults": {"skills_target": str(repo / "skills-out")},
            "agent_hooks": {"enabled": False}, "ci": {"enabled": False}, "mcp": {"enabled": False},
            "harness": {"enabled": False}, "permissions": {"enabled": False},
        },
        repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    assert not _skill_actions(plan, "link_skill_plugin")


def test_resolve_plugin_skills_sorted_and_stable(tmp_path):
    """Unit-level: resolve_plugin_skills sorts by plugin name for a stable plan order, and its
    notes name the exact declared plugin/marketplace that failed to resolve."""
    installed_file = tmp_path / "installed_plugins.json"
    root_b = tmp_path / "plugin-b-root"
    _write_plugin_skill(root_b, "zzz")
    _write_plugin_skill(root_b, "aaa")
    _write_installed_plugins(installed_file, "b-plugin", "mp", root_b)
    resolved, notes = resolve_plugin_skills(
        {
            "b-plugin": {"marketplace": "mp"},
            "a-plugin": {"marketplace": "mp"},  # not in installed_plugins.json → note
        },
        installed_plugins_file=installed_file,
    )
    assert [r[0] for r in resolved] == ["b-plugin", "b-plugin"]  # sorted by plugin name
    assert [r[1] for r in resolved] == ["aaa", "zzz"]  # skill dirs sorted by name within a plugin
    assert any("a-plugin" in n for n in notes)
