"""OMP hook bridge tests."""

from __future__ import annotations

import re
from riglib.actions import run_plan
from riglib.catalog import Catalog
from riglib.config import LoadedConfig
from riglib.drift import detect
from riglib.plan import build
from tests.actions_drift_helpers import (
    _bridge_cfg,
    _bridge_results,
    _git_init,
    _omp_wrapper_cfg,
)


def test_omp_hook_bridge_links_extension(fake_agent_tools, tmp_path):
    """Apply wires omp by symlinking the bridge extension into the extensions directory."""
    repo = tmp_path / "repo"
    repo.mkdir()
    extension = repo / "omp-agent" / "extensions" / "zz-agent-tools-hook-bridge.ts"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(
            repo,
            fake_agent_tools,
            settings_path=extension,
            kind="omp",
            hook_bridge={"enabled": True},
        ),
        cat,
        project_type="unknown",
    )
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    assert _bridge_results(report), "no register_hook_bridge action ran"
    assert extension.is_symlink()
    assert extension.resolve() == (fake_agent_tools / "lib" / "omp_hook_bridge" / "extension.ts")

    after = detect(plan)
    assert not any(i.item == "hook-bridge" for i in after.items), [i.detail for i in after.items]


def test_omp_hook_bridge_idempotent_reapply(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    extension = repo / "omp-agent" / "extensions" / "zz-agent-tools-hook-bridge.ts"
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _bridge_cfg(repo, fake_agent_tools, settings_path=extension, kind="omp",
                       hook_bridge={"enabled": True})
    plan = build(cfg, cat, project_type="unknown")
    first = run_plan(plan)
    assert not first.errors
    second_plan = build(cfg, cat, project_type="unknown")
    second = run_plan(second_plan)
    assert not second.errors
    result = _bridge_results(second)[0]
    assert result.status == "skipped"


def test_omp_hook_bridge_repoints_existing_symlink(fake_agent_tools, tmp_path):
    """A symlink pointing at a stale/wrong destination is re-pointed, not left alone."""
    repo = tmp_path / "repo"
    repo.mkdir()
    extension = repo / "omp-agent" / "extensions" / "zz-agent-tools-hook-bridge.ts"
    extension.parent.mkdir(parents=True)
    wrong_dest = tmp_path / "wrong-extension.ts"
    wrong_dest.write_text("// wrong bridge\n", encoding="utf-8")
    extension.symlink_to(wrong_dest)
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=extension, kind="omp",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )
    before = detect(plan)
    assert any(i.item == "hook-bridge" for i in before.items), [i.detail for i in before.items]

    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    result = _bridge_results(report)[0]
    # a symlink to an UNRELATED target is the user's own configuration, not a stale rig link:
    # under the default on_conflict=backup it is preserved as a restorable .rig-bak-* LINK
    # (the link itself is moved, never its target), then the managed symlink is created.
    assert result.status == "backed_up", result.detail
    assert extension.is_symlink()
    assert extension.resolve() == (fake_agent_tools / "lib" / "omp_hook_bridge" / "extension.ts")
    backups = list(extension.parent.glob("zz-agent-tools-hook-bridge.ts.rig-bak-*"))
    assert len(backups) == 1 and backups[0].is_symlink(), backups
    assert backups[0].resolve() == wrong_dest.resolve()
    assert wrong_dest.read_text(encoding="utf-8") == "// wrong bridge\n"
    assert detect(plan).in_sync


def test_omp_hook_bridge_foreign_symlink_skip_left_untouched(fake_agent_tools, tmp_path):
    """on_conflict=skip must leave a USER-created symlink (pointing at an unrelated
    extension) alone — it is foreign configuration with no rig provenance, so replacing it
    would destroy it with no restore path (PR #352 review thread / AGENTS.md backup rule)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    extension = repo / "omp-agent" / "extensions" / "zz-agent-tools-hook-bridge.ts"
    extension.parent.mkdir(parents=True)
    users_own = tmp_path / "my-extension.ts"
    users_own.write_text("// the user's own extension\n", encoding="utf-8")
    extension.symlink_to(users_own)
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=extension, kind="omp",
                    hook_bridge={"enabled": True}, on_conflict="skip"),
        cat, project_type="unknown",
    )
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    assert _bridge_results(report)[0].status == "skipped"
    assert extension.is_symlink() and extension.resolve() == users_own.resolve()
    assert not list(extension.parent.glob("*.rig-bak-*"))
    drift = detect(plan)
    assert any(i.item == "hook-bridge" and "points elsewhere" in i.detail for i in drift.items), \
        [i.detail for i in drift.items]


def test_omp_hook_bridge_prior_checkout_symlink_repointed_regardless_of_conflict(fake_agent_tools, tmp_path):
    """A symlink into a PRIOR agent-tools checkout's lib/omp_hook_bridge/extension.ts is
    demonstrably rig's own (the checkout moved) — re-pointed even under on_conflict=skip, and
    without a backup (a stale rig link carries no user data), like link_skill_harness."""
    repo = tmp_path / "repo"
    repo.mkdir()
    extension = repo / "omp-agent" / "extensions" / "zz-agent-tools-hook-bridge.ts"
    extension.parent.mkdir(parents=True)
    old_checkout = tmp_path / "old-agent-tools" / "lib" / "omp_hook_bridge" / "extension.ts"
    old_checkout.parent.mkdir(parents=True)
    old_checkout.write_text("// old bridge\n", encoding="utf-8")
    extension.symlink_to(old_checkout)
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=extension, kind="omp",
                    hook_bridge={"enabled": True}, on_conflict="skip"),
        cat, project_type="unknown",
    )
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    assert _bridge_results(report)[0].status == "updated"
    assert extension.resolve() == (fake_agent_tools / "lib" / "omp_hook_bridge" / "extension.ts")
    assert not list(extension.parent.glob("*.rig-bak-*"))
    assert detect(plan).in_sync


def test_omp_hook_bridge_wrapper_mode_foreign_symlink_skip_left_untouched(fake_agent_tools, tmp_path):
    """Same foreign-symlink rule in the WRAPPER branch (custom hook_bridge.python): a user's
    symlink at the path is not ours to replace under on_conflict=skip."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    extension = repo / ".omp-agent" / "extensions" / "zz-agent-tools-hook-bridge.ts"
    extension.parent.mkdir(parents=True)
    users_own = tmp_path / "my-extension.ts"
    users_own.write_text("// the user's own extension\n", encoding="utf-8")
    extension.symlink_to(users_own)
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _omp_wrapper_cfg(repo, fake_agent_tools, extension, python="/opt/py/bin/python3", on_conflict="skip"),
        cat, project_type="unknown",
    )
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    assert _bridge_results(report)[0].status == "skipped"
    assert extension.is_symlink() and extension.resolve() == users_own.resolve()
    assert not list(extension.parent.glob("*.rig-bak-*"))


def test_omp_hook_bridge_backs_up_existing_extension_file(fake_agent_tools, tmp_path):
    """A REAL (non-symlink) file already at the extension path is backed up, then replaced."""
    repo = tmp_path / "repo"
    repo.mkdir()
    extension = repo / "omp-agent" / "extensions" / "zz-agent-tools-hook-bridge.ts"
    extension.parent.mkdir(parents=True)
    extension.write_text("// hand-authored extension\n", encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=extension, kind="omp",
                    hook_bridge={"enabled": True}, on_conflict="backup"),
        cat,
        project_type="unknown",
    )
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    result = _bridge_results(report)[0]
    assert result.status == "backed_up"
    assert extension.is_symlink()
    assert extension.resolve() == (fake_agent_tools / "lib" / "omp_hook_bridge" / "extension.ts")
    backups = list(extension.parent.glob("zz-agent-tools-hook-bridge.ts.rig-bak-*"))
    assert backups, "expected a backup of the hand-authored extension"
    assert backups[0].read_text(encoding="utf-8") == "// hand-authored extension\n"


def test_omp_hook_bridge_skip_leaves_existing_extension_file_untouched(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    extension = repo / "omp-agent" / "extensions" / "zz-agent-tools-hook-bridge.ts"
    extension.parent.mkdir(parents=True)
    extension.write_text("// hand-authored extension\n", encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=extension, kind="omp",
                    hook_bridge={"enabled": True}, on_conflict="skip"),
        cat,
        project_type="unknown",
    )
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    result = _bridge_results(report)[0]
    assert result.status == "skipped"
    assert not extension.is_symlink()
    assert extension.read_text(encoding="utf-8") == "// hand-authored extension\n"


def test_omp_hook_bridge_drift_missing(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    extension = repo / "omp-agent" / "extensions" / "zz-agent-tools-hook-bridge.ts"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=extension, kind="omp",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )
    report = detect(plan)
    items = [i for i in report.items if i.item == "hook-bridge"]
    assert items and items[0].direction == "missing"


def test_omp_hook_bridge_custom_hook_target_writes_wrapper(fake_agent_tools, tmp_path):
    """A custom agent_hooks.target keeps the omp bridge wired to that descriptor dir.

    omp's dispatcher reads its hooks_dir from the ``OMP_HOOKS_DIR`` env var (like opencode's
    ``OPENCODE_HOOKS_DIR``) — a plain symlink cannot carry a custom directory, so an explicit
    ``agent_hooks.target`` must produce a small wrapper module instead."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    extension = repo / ".omp-agent" / "extensions" / "zz-agent-tools-hook-bridge.ts"
    custom_hooks = repo / "custom-hooks"
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False},
            "agent_hooks": {"all": True, "target": str(custom_hooks)},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "harness": {"kind": "omp", "settings_path": str(extension), "hook_bridge": {"enabled": True}},
        },
        repo_root=repo,
        repo_path=repo / "rig.yaml",
        layers=[f"repo:{repo / 'rig.yaml'}"],
    )
    plan = build(cfg, cat, project_type="unknown")

    report = run_plan(plan)

    assert not report.errors, [r.detail for r in report.errors]
    assert _bridge_results(report), "no register_hook_bridge action ran"
    assert extension.is_file()
    assert not extension.is_symlink()
    text = extension.read_text(encoding="utf-8")
    assert f'process.env.OMP_HOOKS_DIR = "{custom_hooks}"' in text
    assert "export default bridgeModule.default;" in text
    assert str((fake_agent_tools / "lib" / "omp_hook_bridge" / "extension.ts").resolve().as_uri()) in text
    assert detect(plan).in_sync


def test_omp_hook_bridge_custom_wrapper_drift_is_repaired(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    extension = repo / ".omp-agent" / "extensions" / "zz-agent-tools-hook-bridge.ts"
    custom_hooks = repo / "custom-hooks"
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False},
            "agent_hooks": {"all": True, "target": str(custom_hooks)},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "harness": {"kind": "omp", "settings_path": str(extension), "hook_bridge": {"enabled": True}},
        },
        repo_root=repo,
        repo_path=repo / "rig.yaml",
        layers=[f"repo:{repo / 'rig.yaml'}"],
    )
    plan = build(cfg, cat, project_type="unknown")
    first = run_plan(plan)
    assert not first.errors, [r.detail for r in first.errors]
    extension.write_text("// stale wrapper\n", encoding="utf-8")

    drift = detect(plan)

    assert any(
        i.item == "hook-bridge"
        and i.direction == "modified"
        and "wrapper differs from config" in i.detail
        for i in drift.items
    ), [i.detail for i in drift.items]

    second = run_plan(plan)

    assert not second.errors, [r.detail for r in second.errors]
    assert f'process.env.OMP_HOOKS_DIR = "{custom_hooks}"' in extension.read_text(encoding="utf-8")
    assert detect(plan).in_sync


def test_omp_hook_bridge_custom_python_forces_wrapper(fake_agent_tools, tmp_path):
    """A custom hook_bridge.python must reach the extension somehow — omp's extension.ts reads
    OMP_HOOK_BRIDGE_PYTHON (falling back to a bare `python3`), so unlike Claude/Codex (where the
    interpreter is inlined into the literal shell command), omp needs a wrapper to carry it —
    otherwise the config key would be silently ignored (rig-cli#342 review finding)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    extension = repo / ".omp-agent" / "extensions" / "zz-agent-tools-hook-bridge.ts"
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False},
            "agent_hooks": {"all": True},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "harness": {
                "kind": "omp",
                "settings_path": str(extension),
                "hook_bridge": {"enabled": True, "python": "/opt/py/bin/python3"},
            },
        },
        repo_root=repo,
        repo_path=repo / "rig.yaml",
        layers=[f"repo:{repo / 'rig.yaml'}"],
    )
    plan = build(cfg, cat, project_type="unknown")

    report = run_plan(plan)

    assert not report.errors, [r.detail for r in report.errors]
    assert extension.is_file()
    assert not extension.is_symlink()
    text = extension.read_text(encoding="utf-8")
    assert 'process.env.OMP_HOOK_BRIDGE_PYTHON = "/opt/py/bin/python3"' in text
    assert detect(plan).in_sync


def test_omp_hook_bridge_symlink_to_wrapper_transition_converges_under_skip(fake_agent_tools, tmp_path):
    """A rig-owned plain symlink converting to a wrapper (config gains hook_bridge.python or a
    custom agent_hooks.target) must still converge even under on_conflict=skip.

    Regression test for a review-cli finding (rig-cli#342, caught independently by two
    reviewers): the wrapper branch used to treat ANY pre-existing path — including rig's own
    prior symlink — as a conflict subject to on_conflict, so `skip` silently left the stale
    symlink in place forever (the new python/hooks_dir override never wired, drift never
    resolving). A symlink is rig's own artifact, not foreign data, and must always be safe to
    replace with the wrapper regardless of on_conflict — only a genuinely foreign real file
    should respect skip/backup.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    extension = repo / ".omp-agent" / "extensions" / "zz-agent-tools-hook-bridge.ts"
    cat = Catalog.scan(str(fake_agent_tools))

    # first apply: default config → plain symlink, no wrapper needed yet.
    base_cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False},
            "agent_hooks": {"all": True},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "harness": {"kind": "omp", "settings_path": str(extension), "hook_bridge": {"enabled": True}},
        },
        repo_root=repo,
        repo_path=repo / "rig.yaml",
        layers=[f"repo:{repo / 'rig.yaml'}"],
    )
    first_plan = build(base_cfg, cat, project_type="unknown")
    first = run_plan(first_plan)
    assert not first.errors, [r.detail for r in first.errors]
    assert extension.is_symlink()

    # second apply: config gains hook_bridge.python (forces a wrapper) with on_conflict=skip.
    wrapper_cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False},
            "agent_hooks": {"all": True},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "harness": {
                "kind": "omp",
                "settings_path": str(extension),
                "hook_bridge": {"enabled": True, "python": "/opt/py/bin/python3"},
            },
            "defaults": {"on_conflict": "skip"},
        },
        repo_root=repo,
        repo_path=repo / "rig.yaml",
        layers=[f"repo:{repo / 'rig.yaml'}"],
    )
    second_plan = build(wrapper_cfg, cat, project_type="unknown")
    second = run_plan(second_plan)

    assert not second.errors, [r.detail for r in second.errors]
    assert extension.is_file() and not extension.is_symlink(), "skip must not block the symlink→wrapper transition"
    assert 'process.env.OMP_HOOK_BRIDGE_PYTHON = "/opt/py/bin/python3"' in extension.read_text(encoding="utf-8")
    assert detect(second_plan).in_sync


def test_omp_hook_bridge_wrapper_content_change_converges_under_skip(fake_agent_tools, tmp_path):
    """Scenario A (review-cli, rig-cli#342): an already-written rig-owned WRAPPER FILE whose
    content is now stale (a different hook_bridge.python configured since) must still converge
    under on_conflict=skip — a rig-authored wrapper is rig's own artifact, not foreign data,
    exactly like a rig-owned symlink already is."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    extension = repo / ".omp-agent" / "extensions" / "zz-agent-tools-hook-bridge.ts"
    cat = Catalog.scan(str(fake_agent_tools))

    first = run_plan(build(
        _omp_wrapper_cfg(repo, fake_agent_tools, extension, python="/opt/py/bin/python3", on_conflict="backup"),
        cat, project_type="unknown",
    ))
    assert not first.errors, [r.detail for r in first.errors]
    first_text = extension.read_text(encoding="utf-8")
    assert "/opt/py/bin/python3" in first_text
    from riglib.actions.hook_bridge_wrappers import _OMP_WRAPPER_HEADER

    assert first_text.startswith(_OMP_WRAPPER_HEADER), (
        "pins the header _omp_wrapper_file_is_rig_managed matches against — if this constant's "
        "text ever changes without updating both writer and reader in lockstep, an EXISTING "
        "wrapper written before the change would stop being recognized as rig-owned, and "
        "Scenario A/B below would silently regress back to on_conflict=skip blocking forever"
    )

    second_plan = build(
        _omp_wrapper_cfg(repo, fake_agent_tools, extension, python="/opt/py/bin/python3.12", on_conflict="skip"),
        cat, project_type="unknown",
    )
    second = run_plan(second_plan)

    assert not second.errors, [r.detail for r in second.errors]
    text = extension.read_text(encoding="utf-8")
    assert 'process.env.OMP_HOOK_BRIDGE_PYTHON = "/opt/py/bin/python3.12"' in text, (
        f"skip must not block a content change to rig's OWN wrapper: {text!r}"
    )
    assert detect(second_plan).in_sync


def test_omp_hook_bridge_wrapper_to_symlink_transition_converges_under_skip(fake_agent_tools, tmp_path):
    """Scenario B (review-cli, rig-cli#342): dropping hook_bridge.python (uses_wrapper flips back
    to False) must replace a rig-owned wrapper FILE with a plain symlink even under
    on_conflict=skip."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    extension = repo / ".omp-agent" / "extensions" / "zz-agent-tools-hook-bridge.ts"
    cat = Catalog.scan(str(fake_agent_tools))

    first = run_plan(build(
        _omp_wrapper_cfg(repo, fake_agent_tools, extension, python="/opt/py/bin/python3", on_conflict="backup"),
        cat, project_type="unknown",
    ))
    assert not first.errors, [r.detail for r in first.errors]
    assert extension.is_file() and not extension.is_symlink()

    second_plan = build(
        _omp_wrapper_cfg(repo, fake_agent_tools, extension, python=None, on_conflict="skip"),
        cat, project_type="unknown",
    )
    second = run_plan(second_plan)

    assert not second.errors, [r.detail for r in second.errors]
    assert extension.is_symlink(), "skip must not block the wrapper→symlink transition"
    assert extension.resolve() == (fake_agent_tools / "lib" / "omp_hook_bridge" / "extension.ts")
    assert detect(second_plan).in_sync
