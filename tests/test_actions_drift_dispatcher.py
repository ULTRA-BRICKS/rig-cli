"""Dispatcher, CI, skill-link and MCP action tests."""

from __future__ import annotations

import json
import os
import re
import sys
import pytest
from riglib.actions import run_plan
from riglib.catalog import Catalog
from riglib.config import LoadedConfig
from riglib.drift import detect
from riglib.plan import Action, build
from tests.actions_drift_helpers import (
    _dispatcher_cfg,
    _full_cfg,
    _harness_skill_cfg,
    _skill_link_cfg,
)


def test_cc_and_codex_dispatchers_read_custom_hooks_dir_env(fake_agent_tools, tmp_path, monkeypatch):
    """Bridge commands and dispatcher modules must agree on custom descriptor-dir env names."""
    lib_dir = fake_agent_tools / "lib"
    monkeypatch.syspath_prepend(str(lib_dir))
    monkeypatch.setenv("CC_HOOKS_DIR", str(tmp_path / "cc-hooks"))
    monkeypatch.setenv("CODEX_HOOKS_DIR", str(tmp_path / "codex-hooks"))
    for name in (
        "cc_hook_bridge",
        "cc_hook_bridge.dispatch",
        "codex_hook_bridge",
        "codex_hook_bridge.dispatch",
    ):
        sys.modules.pop(name, None)

    from cc_hook_bridge import dispatch as cc_dispatch
    from codex_hook_bridge import dispatch as codex_dispatch

    assert cc_dispatch.hooks_dir() == tmp_path / "cc-hooks"
    assert codex_dispatch.hooks_dir() == tmp_path / "codex-hooks"


def test_apply_is_idempotent(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_full_cfg(repo, fake_agent_tools), cat, project_type="cli")

    first = run_plan(plan)
    assert not first.errors, [r.detail for r in first.errors]
    assert first.changed > 0

    second = run_plan(plan)
    assert not second.errors
    # second run changes nothing — everything skips
    assert second.changed == 0
    assert all(r.status in ("skipped",) for r in second.results), second.summary()


def test_multi_descriptor_hook_installs_all_stays_clean(fake_agent_tools, tmp_path):
    """A hook dir with TWO descriptors: both write, re-apply skips both, drift is clean (#184).

    End-to-end proof (not just planned actions): the fake ``dual-guard`` hook ships a pre-bash
    AND a pre-write descriptor. Both must land on disk, a second ``run_plan`` must skip both,
    and ``detect`` must report neither missing nor extra.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    hooks_dir = tmp_path / "hooks"
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False},
            "agent_hooks": {"all": True, "target": str(hooks_dir)},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")

    first = run_plan(plan)
    assert not first.errors, [r.detail for r in first.errors]
    assert (hooks_dir / "dual-guard.pre-bash.json").is_file()
    assert (hooks_dir / "dual-guard.pre-write.json").is_file()

    second = run_plan(plan)
    dual = [r for r in second.results if r.action.item == "dual-guard"]
    assert len(dual) == 2
    assert all(r.status == "skipped" for r in dual), [r.detail for r in dual]

    report = detect(plan, scan_hook_dirs=[hooks_dir])
    hook_drift = [i for i in report.items if i.category == "agent_hooks" and "dual-guard" in i.item]
    assert not hook_drift, [(i.direction, i.item) for i in hook_drift]


def test_dispatcher_idempotent_status(fake_agent_tools, tmp_path, monkeypatch):
    """The dispatcher action rolls sub-outcomes up: a no-op second run reports 'skipped'."""
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False},
            "agent_hooks": {"enabled": False},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "git_hooks": {
                "dispatcher": {
                    "enabled": True,
                    "dir": str(home / "ghd"),
                    "runner": str(home / "run-global-hooks"),
                    "set_global_hooks_path": False,  # don't touch global git config in tests
                    "install_local_retrofit_script": False,
                }
            },
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    first = run_plan(plan)
    disp1 = next(r for r in first.results if r.action.item == "dispatcher")
    assert disp1.status in ("created", "backed_up")
    second = run_plan(plan)
    disp2 = next(r for r in second.results if r.action.item == "dispatcher")
    assert disp2.status == "skipped", disp2.detail


def test_dispatcher_installs_composers_and_points_hookspath(fake_agent_tools, tmp_path, monkeypatch):
    """core.hooksPath must point at the composer dir (hooks/), which must be installed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    recorded = {}

    def _fake_set(key, value):
        recorded[key] = value
        return 0

    monkeypatch.setattr("riglib.actions.git_config._set_git_global", _fake_set)
    monkeypatch.setattr("riglib.actions.git_config._git_global", lambda key: None)

    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False},
            "agent_hooks": {"enabled": False},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "git_hooks": {
                "dispatcher": {
                    "enabled": True,
                    "dir": str(home / "ghd"),
                    "runner": str(home / "run-global-hooks"),
                    "set_global_hooks_path": True,
                    "install_local_retrofit_script": False,
                }
            },
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    composer = home / "hooks"
    assert (composer / "pre-commit").is_file()  # composers installed
    assert recorded["core.hooksPath"] == str(composer)  # points at composer dir, not runner parent


def test_disabled_dispatcher_still_wired_is_drift(tmp_path, monkeypatch):
    from riglib.drift import DriftReport, check_disabled_dispatcher

    composer = tmp_path / "hooks"
    composer.mkdir()
    (composer / "pre-commit").write_text("#!/bin/sh\nexec ../run-global-hooks pre-commit\n", encoding="utf-8")
    monkeypatch.setattr("riglib.drift_git_hooks._git_global", lambda k: str(composer))
    report = DriftReport()
    check_disabled_dispatcher(tmp_path, report)
    assert any(i.category == "git_hooks" and i.direction == "extra" for i in report.items)


def test_disabled_dispatcher_unrelated_hookspath_not_flagged(tmp_path, monkeypatch):
    from riglib.drift import DriftReport, check_disabled_dispatcher

    composer = tmp_path / "hooks"
    composer.mkdir()
    (composer / "pre-commit").write_text("#!/bin/sh\necho someone elses hook\n", encoding="utf-8")
    monkeypatch.setattr("riglib.drift_git_hooks._git_global", lambda k: str(composer))
    report = DriftReport()
    check_disabled_dispatcher(tmp_path, report)
    assert not report.items  # not a rig dispatcher → not flagged


def test_drift_dispatcher_composer_modified(fake_agent_tools, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    monkeypatch.setattr("riglib.actions.git_config._set_git_global", lambda k, v: 0)
    monkeypatch.setattr("riglib.actions.git_config._git_global", lambda k: None)
    monkeypatch.setattr("riglib.drift_git_hooks._git_global", lambda k: None)
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": True, "dir": str(home / "ghd"),
                                         "runner": str(home / "run-global-hooks"),
                                         "set_global_hooks_path": False,
                                         "install_local_retrofit_script": False}},
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    run_plan(plan)
    # edit a non-pre-commit composer → must be flagged
    (home / "hooks" / "commit-msg").write_text("#!/bin/sh\ntampered\n", encoding="utf-8")
    report = detect(plan)
    assert any(i.category == "git_hooks" and i.item == "commit-msg" and i.direction == "modified" for i in report.items)


def test_write_file_replaces_stale_directory(tmp_path):
    from riglib.actions import fsutil

    target = tmp_path / "codeql.yml"
    target.mkdir()  # a stale DIRECTORY where a file should be
    (target / "junk").write_text("x", encoding="utf-8")
    out = fsutil.write_file(target, "name: codeql\n", "backup")
    assert out.status == "backed_up"
    assert target.is_file()
    assert target.read_text() == "name: codeql\n"


def test_drift_dispatcher_fragment_deleted(fake_agent_tools, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    monkeypatch.setattr("riglib.actions.git_config._set_git_global", lambda k, v: 0)
    monkeypatch.setattr("riglib.actions.git_config._git_global", lambda k: None)
    monkeypatch.setattr("riglib.drift_git_hooks._git_global", lambda k: None)
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": True, "dir": str(home / "ghd"),
                                         "runner": str(home / "run-global-hooks"),
                                         "set_global_hooks_path": False,
                                         "install_local_retrofit_script": False}},
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    run_plan(plan)
    # delete an installed fragment → config→disk drift
    (home / "ghd" / "pre-commit" / "10-secret-scan").unlink()
    report = detect(plan)
    assert any(i.category == "git_hooks" and i.direction == "missing" and "secret-scan" in i.item for i in report.items)


def test_dispatcher_fragments_filtered(fake_agent_tools, tmp_path, monkeypatch):
    """A disabled fragment is not copied into global-hooks.d."""
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    monkeypatch.setattr("riglib.actions.git_config._set_git_global", lambda k, v: 0)
    monkeypatch.setattr("riglib.actions.git_config._git_global", lambda k: None)
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False},
            "agent_hooks": {"enabled": False},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "git_hooks": {
                "dispatcher": {
                    "enabled": True,
                    "dir": str(home / "ghd"),
                    "runner": str(home / "run-global-hooks"),
                    "set_global_hooks_path": False,
                    "install_local_retrofit_script": False,
                    "fragments": {"conventional-commit": {"enabled": False}},
                }
            },
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    run_plan(plan)
    ghd = home / "ghd"
    # secret-scan fragment present, conventional-commit fragment filtered out
    assert (ghd / "pre-commit" / "10-secret-scan").is_file()
    assert not (ghd / "commit-msg" / "10-conventional-commit").exists()


def test_prepush_fragment_installed_executable_and_drifts(fake_agent_tools, tmp_path, monkeypatch):
    """A PRE-PUSH event fragment (protect-main, HYP-856) is provisioned like any other
    fragment: installed per-file + executable, and a deletion is config→disk drift.

    Pins that the fragment pipeline is EVENT-GENERIC — nothing in install/drift may
    special-case pre-commit; protect-main is the first shipped pre-push fragment."""
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    monkeypatch.setattr("riglib.actions.git_config._set_git_global", lambda k, v: 0)
    monkeypatch.setattr("riglib.actions.git_config._git_global", lambda k: None)
    monkeypatch.setattr("riglib.drift_git_hooks._git_global", lambda k: None)
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_dispatcher_cfg(fake_agent_tools, repo, home), cat, project_type="unknown")
    run_plan(plan)
    frag = home / "ghd" / "pre-push" / "10-protect-main"
    assert frag.is_file()
    assert os.access(frag, os.X_OK)  # git ignores a non-executable hook
    # content-modified drift is detected too (the pipeline is event-generic for
    # BOTH directions, matching test_drift_ci_content_modified for ci items)
    frag.write_text("#!/bin/sh\ntampered\n", encoding="utf-8")
    report = detect(plan)
    assert any(
        i.category == "git_hooks" and i.direction == "modified" and "protect-main" in i.item
        for i in report.items
    )
    frag.unlink()
    report = detect(plan)
    assert any(
        i.category == "git_hooks" and i.direction == "missing" and "protect-main" in i.item
        for i in report.items
    )


def test_prepush_fragment_disabled_via_config(fake_agent_tools, tmp_path, monkeypatch):
    """`fragments.protect-main.enabled: false` (rig.yaml) opts the pre-push gate out
    without touching the other events' fragments."""
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    monkeypatch.setattr("riglib.actions.git_config._set_git_global", lambda k, v: 0)
    monkeypatch.setattr("riglib.actions.git_config._git_global", lambda k: None)
    monkeypatch.setattr("riglib.drift_git_hooks._git_global", lambda k: None)
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _dispatcher_cfg(
        fake_agent_tools, repo, home, fragments={"protect-main": {"enabled": False}}
    )
    plan = build(cfg, cat, project_type="unknown")
    run_plan(plan)
    ghd = home / "ghd"
    assert not (ghd / "pre-push" / "10-protect-main").exists()
    assert (ghd / "pre-commit" / "10-secret-scan").is_file()  # others unaffected


def test_drift_ci_content_modified(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_full_cfg(repo, fake_agent_tools), cat, project_type="cli")
    run_plan(plan)
    # tamper an installed workflow in place
    (repo / ".github/workflows/codeql.yml").write_text("name: tampered\n", encoding="utf-8")
    report = detect(plan)
    assert any(i.direction == "modified" and i.category == "ci" and i.item == "codeql" for i in report.items)


def test_drift_ci_companion_deleted(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "ci": {"target": str(repo / ".github/workflows"), "items": {"leftover-grep": {"enabled": True}}},
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    run_plan(plan)
    (repo / "ci/leftover-grep/leftover-grep.sh").unlink()
    report = detect(plan)
    assert any(i.category == "ci" and i.direction == "missing" and "leftover-grep" in i.item for i in report.items)


def test_drift_disabled_fragment_still_on_disk(fake_agent_tools, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    monkeypatch.setattr("riglib.actions.git_config._set_git_global", lambda k, v: 0)
    monkeypatch.setattr("riglib.actions.git_config._git_global", lambda k: None)
    monkeypatch.setattr("riglib.drift_git_hooks._git_global", lambda k: None)
    cat = Catalog.scan(str(fake_agent_tools))
    base = {
        "agent_tools_source": str(fake_agent_tools),
        "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
        "ci": {"enabled": False}, "mcp": {"enabled": False},
    }
    disp = {"enabled": True, "dir": str(home / "ghd"), "runner": str(home / "run-global-hooks"),
            "set_global_hooks_path": False, "install_local_retrofit_script": False}
    # install everything (fragment present)
    cfg_on = LoadedConfig(data={**base, "git_hooks": {"dispatcher": disp}}, repo_root=repo)
    run_plan(build(cfg_on, cat, project_type="unknown"))
    assert (home / "ghd" / "commit-msg" / "10-conventional-commit").is_file()
    # now the config disables conventional-commit, but the file remains → drift
    cfg_off = LoadedConfig(
        data={**base, "git_hooks": {"dispatcher": {**disp, "fragments": {"conventional-commit": {"enabled": False}}}}},
        repo_root=repo,
    )
    report = detect(build(cfg_off, cat, project_type="unknown"))
    assert any(i.category == "git_hooks" and i.direction == "extra" and "conventional-commit" in i.item for i in report.items)


def test_drift_extra_workflow_detected(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_full_cfg(repo, fake_agent_tools), cat, project_type="cli")
    run_plan(plan)
    # an undeclared CATALOG gate left on disk (leftover-grep is a slot this config does not enable)
    # is a rig orphan → drift. A non-slot workflow would be the repo's own (known, not drift —
    # see tests/test_provenance.py).
    (repo / ".github/workflows/leftover-grep.yml").write_text("name: leftover-grep\n", encoding="utf-8")
    report = detect(plan)
    extras = report.by_direction("extra")
    assert any(i.category == "ci" and i.item == "leftover-grep" for i in extras), [(i.category, i.item) for i in extras]


def test_drift_ship_content_modified(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    ship_dir = repo / "bin"
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "ci": {"items": {"ship": {"enabled": True, "install_to": str(ship_dir)}}},
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    run_plan(plan)
    (ship_dir / "ship").write_text("#!/bin/sh\necho tampered\n", encoding="utf-8")
    report = detect(plan)
    assert any(i.direction == "modified" and i.category == "ci" and i.item == "ship" for i in report.items)


def test_drift_extra_hook_descriptor_detected(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_full_cfg(repo, fake_agent_tools), cat, project_type="cli")
    run_plan(plan)
    hooks_dir = repo / "hooks-out"
    (hooks_dir / "rogue-hook.pre-bash.json").write_text("{}", encoding="utf-8")
    report = detect(plan)
    assert any(i.category == "agent_hooks" and i.direction == "extra" for i in report.items)


def test_overwrite_file_where_dir_expected(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    skills_out = repo / "skills-out"
    skills_out.mkdir(parents=True)
    # a stale FILE sits where the skill DIR should go
    (skills_out / "naming").write_text("stale file\n", encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "defaults": {"skills_target": str(skills_out), "on_conflict": "overwrite"},
            "skills": {"universal": {"enable": ["naming"], "all": False}}, "by_type": {},
            "agent_hooks": {"enabled": False}, "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    assert (skills_out / "naming").is_dir()  # the file was replaced by the dir


def test_drift_extra_mcp_detected(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_full_cfg(repo, fake_agent_tools), cat, project_type="cli")
    run_plan(plan)
    mcp_json = repo / "mcp-out" / "mcp.json"
    data = json.loads(mcp_json.read_text())
    data["mcpServers"]["rogue"] = {"command": "rogue", "args": []}
    mcp_json.write_text(json.dumps(data), encoding="utf-8")
    report = detect(plan)
    assert any(i.category == "mcp" and i.item == "rogue" for i in report.by_direction("extra"))


def test_fragments_merge_preserves_unrelated(fake_agent_tools, tmp_path, monkeypatch):
    """Installing fragments must not clobber an unrelated drop-in already present."""
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    monkeypatch.setattr("riglib.actions.git_config._set_git_global", lambda k, v: 0)
    monkeypatch.setattr("riglib.actions.git_config._git_global", lambda k: None)
    ghd = home / "ghd"
    # a user fragment that rig does not ship
    (ghd / "pre-commit").mkdir(parents=True)
    (ghd / "pre-commit" / "99-my-own").write_text("#!/bin/sh\n", encoding="utf-8")

    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": True, "dir": str(ghd),
                                         "runner": str(home / "run-global-hooks"),
                                         "set_global_hooks_path": False,
                                         "install_local_retrofit_script": False}},
        },
        repo_root=repo,
    )
    run_plan(build(cfg, cat, project_type="unknown"))
    assert (ghd / "pre-commit" / "99-my-own").is_file()  # unrelated fragment survives
    assert (ghd / "pre-commit" / "10-secret-scan").is_file()  # rig's fragment installed


def test_mcp_malformed_config_backed_up_not_discarded(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    mcp_dir = repo / "mcp-out"
    mcp_dir.mkdir()
    (mcp_dir / "mcp.json").write_text("{ this is not json", encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_full_cfg(repo, fake_agent_tools), cat, project_type="cli")
    report = run_plan(plan)  # default on_conflict=backup
    assert not report.errors, [r.detail for r in report.errors]
    # a backup of the malformed file exists
    assert any(p.name.startswith("mcp.json.rig-bak-") for p in mcp_dir.iterdir())


def test_skill_harness_link_created_and_resolves(fake_agent_tools, tmp_path):
    """Apply symlinks each installed skill into the harness dir; the link resolves to it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    harness = repo / "harness-skills"
    skills_out = repo / "skills-out"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_skill_link_cfg(repo, fake_agent_tools, harness_dir=harness, skills_target=skills_out),
                 cat, project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    link = harness / "naming"
    assert link.is_symlink(), "skill not symlinked into the harness dir"
    assert link.resolve() == (skills_out / "naming").resolve()
    assert (link / "SKILL.md").is_file()  # the link resolves to a real skill


def test_skill_harness_link_idempotent(fake_agent_tools, tmp_path):
    """A re-apply with a correct existing symlink is a no-op (skipped)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    harness = repo / "harness-skills"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_skill_link_cfg(repo, fake_agent_tools, harness_dir=harness), cat, project_type="unknown")
    run_plan(plan)
    second = run_plan(plan)
    link_results = [r for r in second.results if r.action.kind == "link_skill_harness"]
    assert link_results, "no link action emitted"
    assert all(r.status == "skipped" for r in link_results), [r.detail for r in link_results]


def test_skill_harness_link_repoints_wrong_symlink(fake_agent_tools, tmp_path):
    """A symlink pointing at the WRONG destination is re-pointed, not left stale."""
    repo = tmp_path / "repo"
    repo.mkdir()
    harness = repo / "harness-skills"
    harness.mkdir()
    skills_out = repo / "skills-out"
    bogus = repo / "somewhere-else"
    bogus.mkdir()
    (harness / "naming").symlink_to(bogus)  # stale link to the wrong place
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_skill_link_cfg(repo, fake_agent_tools, harness_dir=harness, skills_target=skills_out),
                 cat, project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    link = harness / "naming"
    assert link.resolve() == (skills_out / "naming").resolve()  # re-pointed to the real skill
    link_res = [r for r in report.results if r.action.kind == "link_skill_harness"]
    assert any(r.status == "updated" for r in link_res), [r.detail for r in link_res]


def test_skill_harness_link_leaves_real_dir_untouched(fake_agent_tools, tmp_path):
    """A REAL (non-symlink) skill dir already at the harness path is never clobbered."""
    repo = tmp_path / "repo"
    repo.mkdir()
    harness = repo / "harness-skills"
    harness.mkdir()
    # a hand-authored skill (like h-reason / debate-swarm) lives at the harness path
    real_skill = harness / "naming"
    real_skill.mkdir()
    (real_skill / "SKILL.md").write_text("hand-authored\n", encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_skill_link_cfg(repo, fake_agent_tools, harness_dir=harness), cat, project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    assert not real_skill.is_symlink()  # left as a real dir
    assert (real_skill / "SKILL.md").read_text() == "hand-authored\n"  # content preserved
    link_res = [r for r in report.results if r.action.kind == "link_skill_harness"]
    assert all(r.status == "skipped" for r in link_res), [r.detail for r in link_res]


def test_skill_harness_link_disabled_emits_no_action(fake_agent_tools, tmp_path):
    """skills.harness_link: false → no link actions planned at all."""
    repo = tmp_path / "repo"
    repo.mkdir()
    harness = repo / "harness-skills"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_skill_link_cfg(repo, fake_agent_tools, harness_dir=harness, harness_link=False),
                 cat, project_type="unknown")
    assert not [a for a in plan.actions if a.kind == "link_skill_harness"]
    run_plan(plan)
    assert not (harness / "naming").exists()  # nothing linked


def test_skill_harness_link_default_dir_under_home(fake_agent_tools, tmp_path, monkeypatch):
    """With no harness_skill_dir set, the default is ~/.claude/skills (HOME-expanded)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_skill_link_cfg(repo, fake_agent_tools, harness_dir=None), cat, project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    link = home / ".claude" / "skills" / "naming"
    assert link.is_symlink()
    assert link.resolve() == (repo / "skills-out" / "naming").resolve()


def test_skill_harness_link_no_self_link_when_target_is_harness_dir(fake_agent_tools, tmp_path):
    """If skills_target already IS the harness dir, no self-referential link is planned."""
    repo = tmp_path / "repo"
    repo.mkdir()
    shared = repo / "shared-skills"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_skill_link_cfg(repo, fake_agent_tools, harness_dir=shared, skills_target=shared),
                 cat, project_type="unknown")
    assert not [a for a in plan.actions if a.kind == "link_skill_harness"]


def test_skill_conflict_backup_is_outside_scanned_skills_dir(fake_agent_tools, tmp_path):
    """A conflicting skill install backs the prior skill up OUTSIDE skills_target.

    skills_target (``~/.agents/skills``) is auto-scanned by opencode, so a same-parent
    ``<name>.rig-bak-*/`` backup — still carrying a ``SKILL.md`` — would be re-loaded as a
    duplicate skill. The backup must relocate out of the scanned dir entirely.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    skills_out = repo / "skills-out"  # the natively-scanned skills_target
    # a DIFFERING prior skill already occupies the install path → forces an on_conflict=backup move
    prior = skills_out / "naming"
    prior.mkdir(parents=True)
    (prior / "SKILL.md").write_text("stale prior skill\n", encoding="utf-8")

    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _skill_link_cfg(repo, fake_agent_tools, harness_dir=repo / "harness-skills",
                        skills_target=skills_out),
        cat, project_type="unknown",
    )
    report = run_plan(plan)  # default on_conflict=backup
    assert not report.errors, [r.detail for r in report.errors]

    skill_res = [r for r in report.results if r.action.kind == "copy_skill"]
    backed = [r for r in skill_res if r.status == "backed_up"]
    assert backed, [r.detail for r in skill_res]
    bak = backed[0].backup
    assert bak is not None

    # 1) the backup is NOT under the scanned skills dir (the bug: it used to be a sibling there).
    assert skills_out not in bak.parents, f"backup {bak} still under scanned skills dir {skills_out}"
    # 2) and there is no *.rig-bak-* dir loitering INSIDE the scanned skills dir at all.
    assert not list(skills_out.glob("*.rig-bak-*")), \
        f"a rig-bak dir leaked into the scanned skills dir: {list(skills_out.glob('*.rig-bak-*'))}"
    # 3) the prior skill's content is preserved at the relocated backup (data not lost).
    assert (bak / "SKILL.md").read_text(encoding="utf-8") == "stale prior skill\n"
    # 4) the fresh skill is installed at the canonical path.
    assert (skills_out / "naming" / "SKILL.md").is_file()


def test_skill_backup_under_skills_target_parent_sibling(fake_agent_tools, tmp_path):
    """The relocated backup sits in a ``.rig-backups`` sibling of the skills dir (one level up)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    # nest skills_target so its parent is meaningfully distinct (mirrors ~/.agents/skills)
    skills_out = repo / "agents" / "skills"
    prior = skills_out / "naming"
    prior.mkdir(parents=True)
    (prior / "SKILL.md").write_text("stale\n", encoding="utf-8")

    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _skill_link_cfg(repo, fake_agent_tools, harness_dir=repo / "harness-skills",
                        skills_target=skills_out),
        cat, project_type="unknown",
    )
    report = run_plan(plan)
    bak = [r for r in report.results if r.status == "backed_up"][0].backup
    assert bak is not None
    # backup dir is <skills_target>/../.rig-backups (a sibling of the skills dir, not inside it)
    assert bak.parent == repo / "agents" / ".rig-backups"


@pytest.mark.parametrize(
    "kind, rel_dir",
    [("claude-code", ".claude/skills"), ("codex", ".codex/skills")],
)
def test_skills_dir_harness_links_into_its_own_dir(fake_agent_tools, tmp_path, monkeypatch, kind, rel_dir):
    """A skills-DIRECTORY harness (claude-code, codex) gets each skill symlinked into ITS dir."""
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_harness_skill_cfg(repo, fake_agent_tools, kind), cat, project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    link = home / rel_dir / "naming"
    assert link.is_symlink(), f"{kind}: skill not symlinked into {rel_dir}"
    assert link.resolve() == (repo / "skills-out" / "naming").resolve()
    assert (link / "SKILL.md").is_file()


@pytest.mark.parametrize("kind", ["claude-code", "codex"])
def test_skills_dir_harness_link_idempotent_and_drift(fake_agent_tools, tmp_path, monkeypatch, kind):
    """A skills-dir harness link is idempotent (re-apply = skipped) and drift-free once synced."""
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_harness_skill_cfg(repo, fake_agent_tools, kind), cat, project_type="unknown")
    # before apply: the missing harness link is reported as config→disk drift
    pre = detect(plan)
    assert [d for d in pre.items if "harness link" in d.item], f"{kind}: missing link not flagged"
    run_plan(plan)
    # after apply: no harness-link drift remains
    post = detect(plan)
    assert not [d for d in post.items if "harness link" in d.item], f"{kind}: drift after apply"
    # re-apply is a pure no-op for the link
    second = run_plan(plan)
    links = [r for r in second.results if r.action.kind == "link_skill_harness"]
    assert links and all(r.status == "skipped" for r in links), [r.detail for r in links]


@pytest.mark.parametrize(
    "kind, instr_marker",
    [("pi", "AGENTS.md"), ("commandcode", "AGENTS.md")],
)
def test_instruction_file_harness_emits_no_link_but_a_note(fake_agent_tools, tmp_path, monkeypatch, kind, instr_marker):
    """An INSTRUCTION-FILE harness (pi/commandcode) links no skill but records WHY.

    No skills dir exists for these kinds, so rig emits zero ``link_skill_harness`` actions (it never
    guesses a dir). The skill is still COPIED to skills_target; a plan note explains the kind reads a
    global AGENTS.md instead — so ``rig status`` isn't a silent empty area.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_harness_skill_cfg(repo, fake_agent_tools, kind), cat, project_type="unknown")
    # no harness symlink action for an instruction-file harness
    assert not [a for a in plan.actions if a.kind == "link_skill_harness"], f"{kind}: unexpected link"
    # the skill is still installed (copy_skill) — it reaches the harness via the instruction file
    assert [a for a in plan.actions if a.kind == "copy_skill"], f"{kind}: skill not even copied"
    # a note names the instruction file (so the empty link area is explained, not silent)
    notes = " ".join(plan.notes)
    assert kind in notes and instr_marker in notes, f"{kind}: no explanatory note ({plan.notes})"
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]


def test_instruction_file_harness_with_explicit_dir_does_link(fake_agent_tools, tmp_path):
    """An explicit ``harness_skill_dir`` overrides the no-link default even for pi (user opt-in).

    pi is instruction-file-only (no skills dir), so it links nothing by default; an explicit
    dir forces a real link and suppresses the instruction-file note.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    harness = repo / "pi-skills"
    cfg = _harness_skill_cfg(repo, fake_agent_tools, "pi")
    cfg.data["skills"]["harness_skill_dir"] = str(harness)
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(cfg, cat, project_type="unknown")
    # the explicit dir forces a real link (and suppresses the instruction-file note)
    assert [a for a in plan.actions if a.kind == "link_skill_harness"], "explicit dir did not link"
    assert not any("instruction file" in n for n in plan.notes), plan.notes
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    assert (harness / "naming").resolve() == (repo / "skills-out" / "naming").resolve()


@pytest.mark.parametrize("kind", ["opencode", "omp"])
def test_native_discovery_harness_links_nothing_when_target_is_native(
    fake_agent_tools, tmp_path, monkeypatch, kind
):
    """A native-discovery harness with the default target needs no harness symlink."""
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _harness_skill_cfg(repo, fake_agent_tools, kind)
    cfg.data["defaults"].pop("skills_target")
    plan = build(cfg, cat, project_type="unknown")
    # no harness symlink for a native-discovery harness (it reads skills_target directly)...
    assert not [a for a in plan.actions if a.kind == "link_skill_harness"], f"{kind}: unexpected link"
    # ...but the skill IS still copied to skills_target (which the harness auto-scans)...
    assert [a for a in plan.actions if a.kind == "copy_skill"], f"{kind}: skill not copied"
    # ...and a note explains it auto-loads natively (so the empty link area is not a silent gap).
    notes = " ".join(plan.notes)
    assert kind in notes and "natively" in notes and ".agents/skills" in notes, plan.notes
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]


@pytest.mark.parametrize("kind", ["opencode", "omp"])
def test_native_discovery_harness_custom_target_links_back_to_native_root(
    fake_agent_tools, tmp_path, monkeypatch, kind
):
    """A native-discovery harness still sees skills when skills_target is customized."""
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    cat = Catalog.scan(str(fake_agent_tools))

    plan = build(_harness_skill_cfg(repo, fake_agent_tools, kind), cat, project_type="unknown")

    links = [a for a in plan.actions if a.kind == "link_skill_harness"]
    assert links
    assert {a.target for a in links} == {home / ".agents" / "skills" / "naming"}
    notes = " ".join(plan.notes)
    assert kind in notes and "skills_target" in notes and "native discovery dir" in notes
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    link = home / ".agents" / "skills" / "naming"
    assert link.is_symlink()
    assert link.resolve() == (repo / "skills-out" / "naming").resolve()


def test_two_native_kinds_sharing_one_root_do_not_duplicate_links(
    fake_agent_tools, tmp_path, monkeypatch
):
    """opencode + omp share the SAME native root (``~/.agents/skills``). With a custom
    skills_target, both kinds want the identical link-back symlink — the plan must emit it
    ONCE per skill, not one conflicting action per kind."""
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _harness_skill_cfg(repo, fake_agent_tools, "opencode")
    cfg.data["harness"]["kinds"] = ["omp"]
    plan = build(cfg, cat, project_type="unknown")
    targets = [a.target for a in plan.actions if a.kind == "link_skill_harness"]
    assert targets, "expected link-back actions for the custom target"
    assert len(targets) == len(set(targets)), f"duplicate link targets: {targets}"
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]


@pytest.mark.parametrize("kind", ["pi", "commandcode"])
def test_auto_mode_on_na_kind_records_visible_note(fake_agent_tools, tmp_path, kind):
    """A kind with NO auto/permission-mode setting at all (pi/commandcode) emits no write — but a
    VISIBLE n/a note naming the reason, so the request is never a silent no-op. The kinds that
    used to self-skip here (codex/opencode/omp) now get a real write — see tests/test_harness_mode.py.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _harness_skill_cfg(repo, fake_agent_tools, kind)
    cfg.data["harness"]["auto_mode"] = True
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(cfg, cat, project_type="unknown")
    assert not [a for a in plan.actions if a.kind == "apply_harness"], f"{kind}: unexpected write"
    assert any("no auto/permission-mode setting" in n and kind in n for n in plan.notes), plan.notes


@pytest.mark.parametrize("kind", ["pi", "commandcode"])
def test_explicit_hook_bridge_on_non_claude_kind_notes_skip(fake_agent_tools, tmp_path, kind):
    """Explicitly enabling hook_bridge on an unsupported kind is reported skipped, not silently dropped.

    ``omp`` is no longer in this list — it now HAS a supported bridge (rig-cli#342), so
    explicitly enabling it builds a real ``register_hook_bridge`` action instead of a skip
    note; see ``test_plan_omp_hook_bridge_emitted_with_harness`` in test_catalog_plan.py.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _harness_skill_cfg(repo, fake_agent_tools, kind)
    cfg.data["harness"]["hook_bridge"] = {"enabled": True}
    cfg.data["agent_hooks"] = {"all": True}  # bridge is only relevant with hooks present
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(cfg, cat, project_type="unknown")
    assert not [a for a in plan.actions if a.kind == "register_hook_bridge"], f"{kind}: unexpected bridge"
    assert any("hook_bridge: skipped" in n and kind in n for n in plan.notes), plan.notes


@pytest.mark.parametrize("kind", ["pi", "commandcode"])
def test_hook_bridge_gap_note_fires_without_explicit_enable(fake_agent_tools, tmp_path, kind):
    """A harness kind with genuinely NO bridge surfaces the gap on every plan build — not only
    when the user thought to opt in with ``hook_bridge.enabled: true``.

    This is the rig-cli#342 silent-skip fix (rig-cli#337 gap catalog item #2): before the fix,
    ``_build_hook_bridge_for_kind`` only appended the "no supported agents-hooks bridge yet"
    note when ``bridge_cfg.get("enabled") is True`` — so a repo that never tried the bridge saw
    NO indication that ``pi``/``commandcode`` have zero agents-hooks coverage at all. The note
    must fire unconditionally, and (being a real coverage gap) must be one of the ALWAYS-SHOWN
    "attention" notes `rig apply` prints with a warning marker, not folded into the
    collapsed informational count.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _harness_skill_cfg(repo, fake_agent_tools, kind)
    cfg.data["agent_hooks"] = {"all": True}  # descriptors installed; only the bridge is missing
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(cfg, cat, project_type="unknown")
    assert not [a for a in plan.actions if a.kind == "register_hook_bridge"], f"{kind}: unexpected bridge"
    matching = [n for n in plan.notes if "hook_bridge: skipped" in n and kind in n]
    assert matching, f"{kind}: expected an unconditional hook_bridge gap note, got {plan.notes}"

    from riglib.cli_plan_print import _note_needs_attention

    assert _note_needs_attention(matching[0]), (
        f"{kind}: hook_bridge gap note must be an always-shown attention note: {matching[0]!r}"
    )


@pytest.mark.parametrize("kind", ["pi", "commandcode"])
def test_hook_bridge_gap_note_honors_explicit_opt_out(fake_agent_tools, tmp_path, kind):
    """An explicit `hook_bridge.enabled: false` still suppresses the gap note, even for a kind
    with genuinely no bridge.

    The unconditional-note fix (rig-cli#342) must not turn a documented opt-out key into dead
    code: a user who already knows pi/commandcode has no bridge and deliberately silenced it
    must not get nagged on every `rig apply`/`rig status` with no way to quiet it (a real
    regression review-cli caught when the unconditional note was ordered BEFORE the
    enabled-False check)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _harness_skill_cfg(repo, fake_agent_tools, kind)
    cfg.data["harness"]["hook_bridge"] = {"enabled": False}
    cfg.data["agent_hooks"] = {"all": True}
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(cfg, cat, project_type="unknown")
    assert not any("hook_bridge" in n and kind in n for n in plan.notes), plan.notes


def test_no_skill_discovery_note_when_skills_disabled(fake_agent_tools, tmp_path):
    """With skills.enabled: false, an instruction-file harness gets NO discovery note (nothing installed)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _harness_skill_cfg(repo, fake_agent_tools, "codex")
    cfg.data["skills"] = {"enabled": False}
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(cfg, cat, project_type="unknown")
    assert not any("instruction file" in n for n in plan.notes), plan.notes
    assert not [a for a in plan.actions if a.category == "skills"], "no skill actions when disabled"


def test_skill_harness_link_drift_missing_then_synced(fake_agent_tools, tmp_path):
    """Drift: a missing harness link is config→disk drift; after apply it is in sync."""
    repo = tmp_path / "repo"
    repo.mkdir()
    harness = repo / "harness-skills"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_skill_link_cfg(repo, fake_agent_tools, harness_dir=harness), cat, project_type="unknown")
    rep = detect(plan)
    miss = [d for d in rep.by_direction("missing")
            if d.category == "skills" and "harness link" in d.item]
    assert miss, "expected a missing harness-link drift item"
    run_plan(plan)
    rep2 = detect(plan)
    assert not [d for d in rep2.items if "harness link" in d.item]


def test_skill_harness_link_drift_wrong_dest_is_modified(fake_agent_tools, tmp_path):
    """A symlink to the wrong destination surfaces as 'modified' drift."""
    repo = tmp_path / "repo"
    repo.mkdir()
    harness = repo / "harness-skills"
    harness.mkdir()
    bogus = repo / "elsewhere"
    bogus.mkdir()
    (harness / "naming").symlink_to(bogus)
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_skill_link_cfg(repo, fake_agent_tools, harness_dir=harness), cat, project_type="unknown")
    rep = detect(plan)
    mod = [d for d in rep.by_direction("modified")
           if d.category == "skills" and "harness link" in d.item]
    assert mod, "expected a modified harness-link drift item for the wrong symlink"


def test_skill_harness_link_repoints_broken_symlink(fake_agent_tools, tmp_path):
    """A BROKEN symlink (target does not exist) is re-pointed to the real installed skill."""
    repo = tmp_path / "repo"
    repo.mkdir()
    harness = repo / "harness-skills"
    harness.mkdir()
    skills_out = repo / "skills-out"
    (harness / "naming").symlink_to(repo / "ghost")  # dangling — target never created
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_skill_link_cfg(repo, fake_agent_tools, harness_dir=harness, skills_target=skills_out),
                 cat, project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    assert (harness / "naming").resolve() == (skills_out / "naming").resolve()
    link_res = [r for r in report.results if r.action.kind == "link_skill_harness"]
    assert any(r.status == "updated" for r in link_res), [r.detail for r in link_res]


def test_skill_harness_link_errors_when_install_target_missing(fake_agent_tools, tmp_path):
    """If the installed skill the link should point at is absent, the link action errors
    (and does not leave a dangling symlink) — exercised by running the link action alone."""
    from riglib.actions.runner import _do_link_skill_harness
    from riglib.plan import Action

    repo = tmp_path / "repo"
    harness = repo / "harness-skills"
    action = Action(
        kind="link_skill_harness", category="skills", item="naming",
        source=repo / "skills-out" / "naming",  # never created
        target=harness / "naming",
    )
    res = _do_link_skill_harness(action, "backup")
    assert res.status == "error"
    assert not (harness / "naming").exists()  # no dangling link created


def test_skill_harness_link_unknown_kind_emits_no_link(fake_agent_tools, tmp_path):
    """An unknown harness.kind with no harness_skill_dir → rig does not guess a path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    # bypass config validation (it would reject an unknown kind) to exercise the plan guard:
    # the resolver must return None for a kind with no known discovery dir.
    from riglib.plan import _resolve_harness_skill_dir

    cfg = LoadedConfig(
        data={"harness": {"kind": "claude-code"}, "skills": {}},  # known kind, link on
        repo_root=repo,
    )
    assert _resolve_harness_skill_dir(cfg) is not None  # known kind resolves
    cfg_unknown = LoadedConfig(data={"skills": {}}, repo_root=repo)
    # monkeypatch the kind map to simulate a kind rig knows of but has no dir for
    import riglib.plan as planmod

    saved = dict(planmod._HARNESS_SKILL_DIRS)
    try:
        planmod._HARNESS_SKILL_DIRS.clear()  # no known dir for any kind
        assert _resolve_harness_skill_dir(cfg_unknown) is None
        plan = build(LoadedConfig(
            data={"agent_tools_source": str(fake_agent_tools),
                  "defaults": {"skills_target": str(repo / "skills-out")},
                  "skills": {"universal": {"enable": ["naming"], "all": False}, "by_type": {}},
                  "agent_hooks": {"enabled": False}, "ci": {"enabled": False},
                  "mcp": {"enabled": False}, "git_hooks": {"dispatcher": {"enabled": False}}},
            repo_root=repo), cat, project_type="unknown")
        assert not [a for a in plan.actions if a.kind == "link_skill_harness"]
    finally:
        planmod._HARNESS_SKILL_DIRS.clear()
        planmod._HARNESS_SKILL_DIRS.update(saved)


def test_skill_harness_link_follows_harness_kind(fake_agent_tools, tmp_path):
    """The skill-link discovery dir follows harness.kind when a harness block pins one."""
    from riglib.plan import _resolve_harness_skill_dir

    repo = tmp_path / "repo"
    cfg = LoadedConfig(
        data={"harness": {"kind": "claude-code", "auto_mode": True}, "skills": {}},
        repo_root=repo,
    )
    resolved = _resolve_harness_skill_dir(cfg)
    assert resolved is not None
    assert resolved.name == "skills" and ".claude" in str(resolved)


def test_skill_harness_link_real_dir_not_flagged_as_drift(fake_agent_tools, tmp_path):
    """A real dir at the harness path is NOT reported as drift (rig won't touch it)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    harness = repo / "harness-skills"
    harness.mkdir()
    real = harness / "naming"
    real.mkdir()
    (real / "SKILL.md").write_text("real\n", encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_skill_link_cfg(repo, fake_agent_tools, harness_dir=harness), cat, project_type="unknown")
    rep = detect(plan)
    assert not [d for d in rep.items if "harness link" in d.item]
