"""`rig status` drift for the ``lint_checks`` activation: analyzer, pre-commit fragment, CI workflow."""

from __future__ import annotations

import os

import pytest
from lint_checks_helpers import RIG_SOURCE, lint_plan, pin_fake_release

from riglib import cccc_install
from riglib.actions.runner import run_plan
from riglib.drift import detect

pytestmark = pytest.mark.usefixtures("real_cccc_inspect", "pinned_rig")


@pytest.fixture
def env(tmp_path, monkeypatch, fake_agent_tools):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("RIG_CCCC_DRY_RUN", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    pin_fake_release(monkeypatch)
    return lint_plan(fake_agent_tools, tmp_path, repo), repo, tmp_path


def _items(plan):
    return {(i.direction, i.item): i for i in detect(plan).items if i.category in ("lint_checks", "lint_checks_machine")}


def test_alternate_home_apply_and_status_agree(tmp_path, monkeypatch, fake_agent_tools):
    """Apply under a HOME override installs the analyzer; status then reports clean (no unsatisfiable loop)."""
    alt = tmp_path / "alt-home"
    alt.mkdir()
    monkeypatch.setenv("HOME", str(alt))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("RIG_CCCC_DRY_RUN", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    pin_fake_release(monkeypatch)
    plan = lint_plan(fake_agent_tools, tmp_path, repo)
    assert ("missing", "cccc") in _items(plan)
    assert not run_plan(plan).errors
    assert _items(plan) == {}
    assert cccc_install.managed_binary_path().is_relative_to(alt)


def test_global_complexity_disabled_has_no_analyzer_drift_but_ci_keeps_its_analyzer_step(tmp_path, monkeypatch, fake_agent_tools):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("RIG_CCCC_DRY_RUN", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    pin_fake_release(monkeypatch)
    plan = lint_plan(fake_agent_tools, tmp_path, repo, global_lint={"complexity": {"enabled": False}})
    assert set(_items(plan)) == {("missing", "pre-commit-hook"), ("missing", "ci-workflow")}
    assert not run_plan(plan).errors
    assert _items(plan) == {}
    # CI never reads the global config: complexity runs there by default, so its workflow still installs the analyzer
    assert "cccc" in (repo / ".github" / "workflows" / "rig-lint-check.yml").read_text()
    assert not (tmp_path / "data").exists()


def test_repo_complexity_off_still_reports_the_machine_analyzer_and_converges(tmp_path, monkeypatch, fake_agent_tools):
    """The analyzer is machine-wide: one repo opting out of complexity must not make the hook analyzer-free."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("RIG_CCCC_DRY_RUN", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    pin_fake_release(monkeypatch)
    plan = lint_plan(fake_agent_tools, tmp_path, repo, {"lint_checks": {"complexity": {"enabled": False}}})
    assert set(_items(plan)) == {("missing", "cccc"), ("missing", "pre-commit-hook"), ("missing", "ci-workflow")}
    assert not run_plan(plan).errors
    assert _items(plan) == {}
    assert cccc_install.managed_binary_path().is_file()
    assert "cccc" not in (repo / ".github" / "workflows" / "rig-lint-check.yml").read_text()  # the repo's CI still skips it


def test_unapplied_machine_reports_everything_missing(env):
    plan, _, _ = env
    items = _items(plan)
    assert set(items) == {("missing", "cccc"), ("missing", "pre-commit-hook"), ("missing", "ci-workflow")}
    assert "rig apply commit" in items[("missing", "cccc")].detail


def test_applied_machine_is_in_sync(env):
    plan, _, _ = env
    run_plan(plan)
    assert _items(plan) == {}
    assert detect(plan).in_sync


def test_modified_and_wrong_version_analyzer(env):
    plan, _, _ = env
    run_plan(plan)
    dest = cccc_install.managed_binary_path()
    dest.write_bytes(b"#!/bin/sh\necho other-version\n")
    dest.chmod(0o755)
    item = _items(plan)[("modified", "cccc")]
    assert "!= pinned" in item.detail and str(dest) in item.detail
    dest.unlink()
    old = dest.parent.parent / "v1.0.0"
    old.mkdir()
    (old / "cccc").write_bytes(b"old")
    item = _items(plan)[("missing", "cccc")]
    assert "v1.0.0" in item.detail and f"v{cccc_install.PINNED_VERSION}" in item.detail


def test_unsupported_platform_is_drift_not_silence(env, monkeypatch):
    plan, _, _ = env
    monkeypatch.setattr(cccc_install, "platform_key", lambda: ("plan9", "mips"))
    item = _items(plan)[("modified", "cccc")]
    assert "no pinned cccc" in item.detail


def test_hook_missing_edited_and_non_executable(env, tmp_path):
    plan, _, _ = env
    run_plan(plan)
    hook = tmp_path / "disp" / "pre-commit" / "30-lint-check"
    hook.chmod(0o644)
    assert "not executable" in _items(plan)[("modified", "pre-commit-hook")].detail
    hook.chmod(0o755)
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    assert "differs" in _items(plan)[("modified", "pre-commit-hook")].detail
    hook.unlink()
    assert ("missing", "pre-commit-hook") in _items(plan)


def test_stale_pinned_rig_path_is_drift(env, tmp_path):
    plan, _, _ = env
    run_plan(plan)
    hook_action = next(a for a in plan.actions if a.kind == "install_lint_check_hook")
    hook_action.options["rig_bin"] = "/somewhere/else/rig"  # rig moved: the managed pin no longer matches
    assert ("modified", "pre-commit-hook") in _items(plan)
    run_plan(plan)
    assert _items(plan) == {}


def test_workflow_missing_edited_and_symlinked(env):
    plan, repo, _ = env
    run_plan(plan)
    wf = repo / ".github" / "workflows" / "rig-lint-check.yml"
    wf.write_text("name: edited\n", encoding="utf-8")
    assert "differs" in _items(plan)[("modified", "ci-workflow")].detail
    wf.unlink()
    assert ("missing", "ci-workflow") in _items(plan)
    outside = repo.parent / "elsewhere.yml"
    outside.write_text("x", encoding="utf-8")
    os.symlink(outside, wf)
    assert "symlink" in _items(plan)[("modified", "ci-workflow")].detail


def test_wrong_non_executable_analyzer_is_modified_not_missing(env):
    plan, _, _ = env
    dest = cccc_install.managed_binary_path()
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"wrong")
    dest.chmod(0o644)
    assert ("modified", "cccc") in _items(plan)


def _loaded(tmp_path, repo, data, global_data=None):
    from riglib.config import LoadedConfig

    return LoadedConfig(data=data, repo_root=repo, global_data=global_data or {})


def _machine_extras(fake_agent_tools, loaded):
    from types import SimpleNamespace

    from riglib.catalog import Catalog
    from riglib.drift import compute_drift_report
    from riglib.plan import build

    new_plan = build(loaded, Catalog.scan(str(fake_agent_tools)), project_type="cli")
    scan = compute_drift_report(new_plan, loaded, SimpleNamespace(is_git_repo=True))
    return new_plan, [i for i in scan.report.items if i.category == "lint_checks_machine" and i.direction == "extra"]


@pytest.mark.parametrize("why", ["fragment", "global lint_checks"])
def test_disabled_fragment_still_installed_is_extra_drift(env, tmp_path, fake_agent_tools, why):
    plan, repo, _ = env
    run_plan(plan)
    hook = tmp_path / "disp" / "pre-commit" / "30-lint-check"
    assert hook.is_file()
    disp = {"enabled": True, "dir": str(tmp_path / "disp")}
    data = {"git_hooks": {"dispatcher": disp}}
    global_data = {}
    if why == "fragment":
        disp["fragments"] = {"lint-check": {"enabled": False}}
    else:
        data["lint_checks"] = global_data["lint_checks"] = {"enabled": False}
    _, extras = _machine_extras(fake_agent_tools, _loaded(tmp_path, repo, data, global_data))
    assert len(extras) == 1 and "still installed (will still run)" in extras[0].detail and extras[0].target == hook


@pytest.mark.parametrize(
    "restrict,reported",
    [
        (None, True),  # `rig status`: unrestricted
        (frozenset({"lint_checks_machine"}), True),
        (frozenset({"git_hooks"}), False),  # the dispatcher is not the category the fragment's drift belongs to
        (frozenset(), False),
    ],
)
def test_the_stale_fragment_scan_is_gated_on_the_category_it_emits(env, tmp_path, fake_agent_tools, restrict, reported):
    from types import SimpleNamespace

    from riglib.catalog import Catalog
    from riglib.drift import compute_drift_report
    from riglib.plan import build

    plan, repo, _ = env
    run_plan(plan)
    disp = {"enabled": True, "dir": str(tmp_path / "disp"), "fragments": {"lint-check": {"enabled": False}}}
    loaded = _loaded(tmp_path, repo, {"git_hooks": {"dispatcher": disp}})
    new_plan = build(loaded, Catalog.scan(str(fake_agent_tools)), project_type="cli")
    scan = compute_drift_report(new_plan, loaded, SimpleNamespace(is_git_repo=True), restrict_scan_categories=restrict)
    extras = [i for i in scan.report.items if i.category == "lint_checks_machine" and i.direction == "extra"]
    assert bool(extras) is reported



def test_a_repo_level_opt_out_is_not_machine_drift_and_keeps_the_machine_actions(env, tmp_path, fake_agent_tools):
    """The hook is machine-wide and harmless where the repo opts out: `rig status` there must be able to be clean."""
    plan, repo, _ = env
    run_plan(plan)
    data = {"git_hooks": {"dispatcher": {"enabled": True, "dir": str(tmp_path / "disp")}}, "lint_checks": {"enabled": False}}
    new_plan, extras = _machine_extras(fake_agent_tools, _loaded(tmp_path, repo, data))
    assert extras == []
    kinds = {a.kind for a in new_plan.actions if a.category in ("lint_checks", "lint_checks_machine")}
    assert kinds == {"install_cccc", "install_lint_check_hook"}  # machine actions from the global layer; no workflow for this repo


def test_repo_enabled_against_a_global_opt_out_reports_the_analyzer_not_an_extra_hook(tmp_path, monkeypatch, fake_agent_tools):
    """The analyzer the repo's check demands is drift until applied; the (never written) global hook is neither missing nor extra."""
    from types import SimpleNamespace

    from riglib.drift import compute_drift_report

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("RIG_CCCC_DRY_RUN", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    pin_fake_release(monkeypatch)
    data = {
        "git_hooks": {"dispatcher": {"enabled": True, "dir": str(tmp_path / "disp")}},
        "lint_checks": {"enabled": True, "ci": {"rig_source": RIG_SOURCE}},
    }
    loaded = _loaded(tmp_path, repo, data, {"lint_checks": {"enabled": False}})
    new_plan, extras = _machine_extras(fake_agent_tools, loaded)
    assert extras == []
    assert {a.kind for a in new_plan.actions if a.category == "lint_checks_machine"} == {"install_cccc"}
    scan = compute_drift_report(new_plan, loaded, SimpleNamespace(is_git_repo=True))
    assert ("missing", "cccc") in {(i.direction, i.item) for i in scan.report.items}
    assert not run_plan(new_plan).errors
    assert cccc_install.managed_binary_path().is_file()  # the `rig apply commit` the 127 message names really installs it
    assert not (tmp_path / "disp" / "pre-commit" / "30-lint-check").exists()



def test_machine_drift_is_visible_outside_a_git_repo_but_the_workflow_is_not(env, tmp_path):
    from types import SimpleNamespace

    from riglib.drift import compute_drift_report

    plan, repo, _ = env
    loaded = _loaded(tmp_path, repo, {})
    scan = compute_drift_report(plan, loaded, SimpleNamespace(is_git_repo=False))
    cats = {(i.category, i.item) for i in scan.report.items}
    assert ("lint_checks_machine", "cccc") in cats and ("lint_checks_machine", "pre-commit-hook") in cats
    assert not any(i.item == "ci-workflow" for i in scan.report.items)
    assert scan.repo_actions_dropped == 1


def test_workflow_is_declared_so_the_ci_extras_scan_never_flags_it(env):
    plan, repo, _ = env
    run_plan(plan)
    wf_dir = repo / ".github" / "workflows"
    report = detect(plan, scan_ci_dirs=[wf_dir])
    assert not [i for i in report.items if i.item == "rig-lint-check" or i.item == "rig-lint-check.yml"]
    assert not any(k.name.startswith("rig-lint-check") for k in report.known)
