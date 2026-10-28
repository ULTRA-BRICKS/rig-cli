"""Dependency directories a bootstrap installs are ignored (locally) before the install, never scanned as code.

Real git repos and the real CCCC analyzer; package managers are shims. A fresh npm repo with no
`.gitignore` would otherwise see every installed package as an untracked source tree. The entry goes into
`.git/info/exclude` through the existing marker-block reconciler: other lines are preserved, the
repository's own `.gitignore` is never touched, plan / CI never write it, and a git-TRACKED file under an
ignored directory is still checked.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from riglib import cccc_install, lint_ignore
from riglib.actions import run_plan
from riglib.lint_bootstrap import apply_bootstrap
from riglib.lint_check_scope import worktree_scope
from riglib.lint_discovery import discover
from riglib.plan import Action, InstallPlan
from tests.lint_bootstrap_helpers import sandbox  # noqa: F401 -- fixture import
from tests.lint_real_helpers import unobtainable

_COMPLEX = "export function f(x: number): number {\n" + "".join(f"  if (x === {i}) {{ return {i}; }}\n" for i in range(30)) + "  return -1;\n}\n"


def _exclude(repo: Path) -> Path:
    return repo / ".git" / "info" / "exclude"


def _exclude_text(repo: Path) -> str:
    return _exclude(repo).read_text(encoding="utf-8") if _exclude(repo).is_file() else ""


def _fresh_npm_repo(sandbox, *, gitignore: str | None = None):
    sandbox.install_managers("npm")
    (sandbox.repo / ".gitignore").unlink()  # the fixture seeds one; this repo has none
    if gitignore is not None:
        sandbox.write(".gitignore", gitignore)
    sandbox.package_json()
    sandbox.write("src/index.ts", "export const x = 1;\n")


def _install_dependency(repo: Path) -> None:
    """What the real `npm install` leaves behind: a dependency with its own package.json and sources."""
    (repo / "node_modules" / "dep").mkdir(parents=True, exist_ok=True)
    (repo / "node_modules" / "dep" / "package.json").write_text('{"name": "dep", "version": "1.0.0"}\n', encoding="utf-8")
    (repo / "node_modules" / "dep" / "index.ts").write_text(_COMPLEX, encoding="utf-8")


def _analyzer_data_home() -> Path | None:
    """XDG data home holding the real, verified CCCC analyzer (resolved before any test rewires HOME), or ``None``."""
    binary = cccc_install.managed_binary_path()
    return binary.parents[3] if binary.is_file() else None


_REAL_DATA_HOME = _analyzer_data_home()


def _cccc(repo: Path) -> dict:
    """The real CCCC analyzer against a sandbox HOME: only the analyzer binary is shared, never a user config."""
    if _REAL_DATA_HOME is None:
        unobtainable("the pinned CCCC analyzer is not installed on this machine (run `rig lint check` once to install it)")
    env = {**os.environ, "XDG_DATA_HOME": str(_REAL_DATA_HOME)}  # HOME stays the sandbox's: no global rig.yaml leaks in
    done = subprocess.run([sys.executable, "-m", "riglib", "lint", "check", "--json", "-C", str(repo)], capture_output=True, text=True,
                          check=False, env=env, cwd=Path(__file__).resolve().parent.parent)
    assert "{" in done.stdout, f"rig lint check produced no JSON document: {done.stdout[:200]!r} {done.stderr[:300]!r}"
    return json.loads(done.stdout[done.stdout.index("{"):])


def _ignore_action(repo: Path, entries: list[str], *, grant: bool = True) -> Action:
    return Action("lint_bootstrap_step", "lint_bootstrap", "x", repo, repo, {"step": "x", "git_exclude": entries, "allow_mutation": grant})


def _run_one(action: Action):
    return run_plan(InstallPlan(actions=[action], on_conflict="skip")).results[0]


# ── the plan ───────────────────────────────────────────────────────────────────────


def test_the_ignore_is_the_first_step_and_planning_writes_nothing(sandbox):
    _fresh_npm_repo(sandbox)
    before = (sandbox.tree(), _exclude_text(sandbox.repo))
    plan, _ = sandbox.plan()
    assert plan.steps[0].kind == "git-exclude" and plan.steps[0].id == "git-exclude@lint-dependencies"
    assert plan.install_plan.actions[0].options["git_exclude"] == ["/node_modules/"]
    assert plan.steps[1].kind == "add-dev-packages", "the install comes after the ignore"
    assert any("ignore /node_modules/ in .git/info/exclude" in n and "not committed" in n for n in plan.notes)
    assert (sandbox.tree(), _exclude_text(sandbox.repo)) == before and sandbox.calls() == []


def test_a_nested_app_gets_its_own_anchored_entry(sandbox):
    sandbox.install_managers("bun")
    (sandbox.repo / ".gitignore").unlink()
    sandbox.package_json({"devDependencies": {"typescript": "5.4.0"}}, rel="miniapp/package.json")
    files = {"eslint.config.js": "export default [];\n", ".prettierrc": "{}\n", "tsconfig.json": "{}\n", "bun.lock": "lock\n",
             "src/App.tsx": "export const x = 1;\n"}
    for rel, text in files.items():
        sandbox.write(f"miniapp/{rel}", text)
    plan, _ = sandbox.plan()
    assert plan.install_plan.actions[0].options["git_exclude"] == ["/miniapp/node_modules/"]


# ── apply: ordering, effect, idempotence ───────────────────────────────────────────


def test_fresh_repo_apply_ignores_node_modules_so_the_first_scan_has_no_dependency_findings(sandbox):
    _fresh_npm_repo(sandbox)
    _install_dependency(sandbox.repo)  # control: before the ignore, the dependency is a scope of its own
    assert "node_modules/dep" in {s.rel for s in discover(sandbox.repo).scopes}
    control = _cccc(sandbox.repo)
    assert any(f["path"].startswith("node_modules/dep/") for f in control["findings"]), "premise: CCCC counts the dependency"

    shutil.rmtree(sandbox.repo / "node_modules")
    plan, _ = sandbox.plan()
    outcome = apply_bootstrap(plan, root=sandbox.repo, environ={})
    assert not outcome.failed and outcome.results[0][0] == "git-exclude@lint-dependencies" and outcome.results[0][1] == "applied"
    assert lint_ignore.BEGIN_MARKER in _exclude_text(sandbox.repo) and "/node_modules/" in _exclude_text(sandbox.repo)

    _install_dependency(sandbox.repo)
    assert {s.rel for s in discover(sandbox.repo).scopes} == {"."}, "no dependency scopes after the install"
    after = _cccc(sandbox.repo)
    assert not any(f["path"].startswith("node_modules/") for f in after["findings"]) and after["ok"] is True


def test_the_ignore_exists_when_the_install_runs(sandbox, monkeypatch):
    """Order guarantee: the first post-install scan is cheap and correct because the entry precedes the install."""
    _fresh_npm_repo(sandbox)
    plan, _ = sandbox.plan()
    seen: list[str] = []
    real_run = run_plan

    def spying(install_plan):
        if install_plan.actions[0].options.get("argv"):  # the package-manager step: the ignore must already be on disk
            seen.append(_exclude_text(sandbox.repo))
        return real_run(install_plan)

    monkeypatch.setattr("riglib.lint_bootstrap.run_plan", spying)
    assert not apply_bootstrap(plan, root=sandbox.repo, environ={}).failed
    assert seen and all("/node_modules/" in text for text in seen)


def test_a_second_run_changes_nothing(sandbox):
    _fresh_npm_repo(sandbox)
    plan, _ = sandbox.plan()
    assert not apply_bootstrap(plan, root=sandbox.repo, environ={}).failed
    exclude, tree = _exclude_text(sandbox.repo), sandbox.tree()
    again, _ = sandbox.plan()
    assert again.is_empty() and not [s for s in again.steps if s.kind == "git-exclude"]
    assert apply_bootstrap(again, root=sandbox.repo, environ={}).results == ()
    assert _exclude_text(sandbox.repo) == exclude and sandbox.tree() == tree
    # a repeated identical action is itself a no-op, not a duplicate block
    assert _run_one(plan.install_plan.actions[0]).status == "skipped"
    assert _exclude_text(sandbox.repo).count(lint_ignore.BEGIN_MARKER) == 1


def test_other_exclude_lines_survive_and_entries_accumulate(sandbox):
    _fresh_npm_repo(sandbox)
    _exclude(sandbox.repo).parent.mkdir(parents=True, exist_ok=True)
    _exclude(sandbox.repo).write_text("# mine\n*.secret\n!keep.secret\n", encoding="utf-8")
    assert _run_one(_ignore_action(sandbox.repo, ["/node_modules/"])).status == "updated"
    assert _run_one(_ignore_action(sandbox.repo, ["/miniapp/node_modules/"])).status == "updated"
    text = _exclude_text(sandbox.repo)
    assert text.startswith("# mine\n*.secret\n!keep.secret\n"), "user lines are preserved verbatim"
    assert lint_ignore.existing_entries(text) == ["/miniapp/node_modules/", "/node_modules/"]
    assert text.count(lint_ignore.BEGIN_MARKER) == 1


# ── existing ignore rules are respected ────────────────────────────────────────────


@pytest.mark.parametrize("gitignore", ["node_modules/\n", "/node_modules\n", "node_modules/*\n!node_modules/keep/\n", "**/node_modules\n"])
def test_an_existing_gitignore_rule_means_no_step_and_no_write(sandbox, gitignore):
    _fresh_npm_repo(sandbox, gitignore=gitignore)
    before = (sandbox.repo / ".gitignore").read_bytes()
    plan, _ = sandbox.plan()
    assert not [s for s in plan.steps if s.kind == "git-exclude"]
    assert not apply_bootstrap(plan, root=sandbox.repo, environ={}).failed
    assert (sandbox.repo / ".gitignore").read_bytes() == before and lint_ignore.BEGIN_MARKER not in _exclude_text(sandbox.repo)


def test_a_gitignore_without_a_node_modules_rule_is_never_edited(sandbox):
    original = "dist/\n!dist/keep.txt\n# my notes\n"
    _fresh_npm_repo(sandbox, gitignore=original)
    plan, _ = sandbox.plan()
    assert not apply_bootstrap(plan, root=sandbox.repo, environ={}).failed
    assert (sandbox.repo / ".gitignore").read_text() == original, "negations and comments are untouched"
    assert "/node_modules/" in _exclude_text(sandbox.repo)


# ── tracked wins ───────────────────────────────────────────────────────────────────


def test_a_tracked_file_under_an_ignored_directory_is_still_checked(sandbox):
    _fresh_npm_repo(sandbox)
    sandbox.write("node_modules/vendored/own.ts", "export const own = 1;\n")
    subprocess.run(["git", "add", "-f", "node_modules/vendored/own.ts"], cwd=sandbox.repo, check=True)
    plan, _ = sandbox.plan()
    assert not apply_bootstrap(plan, root=sandbox.repo, environ={}).failed
    _install_dependency(sandbox.repo)
    scope = worktree_scope(str(sandbox.repo), [], lambda rel: True)
    checked = {f.rel for f in scope.files if not f.skip}
    assert "node_modules/vendored/own.ts" in checked, "tracked wins over the ignore"
    assert not any(rel.startswith("node_modules/dep/") for rel in checked), "untracked dependencies are out of scope"


# ── never written outside a local, setup-allowed run ───────────────────────────────


def test_ci_refuses_the_ignore_step_and_writes_nothing(sandbox):
    _fresh_npm_repo(sandbox)
    plan, _ = sandbox.plan()
    before = (sandbox.tree(), _exclude_text(sandbox.repo))
    outcome = apply_bootstrap(plan, root=sandbox.repo, environ={"CI": "true"})
    assert outcome.results[0] == ("git-exclude@lint-dependencies", "blocked", "refused: CI never bootstraps")
    assert (sandbox.tree(), _exclude_text(sandbox.repo)) == before


def test_the_handler_itself_refuses_under_ci_and_without_a_grant(sandbox, monkeypatch):
    _fresh_npm_repo(sandbox)
    monkeypatch.setenv("CI", "1")
    assert _run_one(_ignore_action(sandbox.repo, ["/node_modules/"])).status == "error"
    monkeypatch.delenv("CI")
    assert _run_one(_ignore_action(sandbox.repo, ["/node_modules/"], grant=False)).status == "error"
    assert lint_ignore.BEGIN_MARKER not in _exclude_text(sandbox.repo)


@pytest.mark.parametrize("entry", ["*", "/", "/src/", "/../node_modules/", "!/node_modules/", "/a/*/node_modules/", "/node_modules/\n/src/"])
def test_the_handler_only_accepts_anchored_node_modules_entries(sandbox, entry):
    _fresh_npm_repo(sandbox)
    result = _run_one(_ignore_action(sandbox.repo, [entry]))
    assert result.status == "error" and "malformed git_exclude" in result.detail
    assert lint_ignore.BEGIN_MARKER not in _exclude_text(sandbox.repo)


# ── the plan is computed as the repository will look AFTER the ignore (one-run convergence) ─────────


def _dependency_with_own_tools(repo: Path) -> None:
    """node_modules/typescript declaring its own eslint/dprint, as the real package does: a descendant that
    'configures' lint and format roles and so withheld the root's bootstrap default."""
    (repo / "node_modules" / "typescript").mkdir(parents=True, exist_ok=True)
    (repo / "node_modules" / "typescript" / "package.json").write_text(
        json.dumps({"name": "typescript", "version": "5.4.5", "devDependencies": {"eslint": "8", "dprint": "0.45"}}), encoding="utf-8")
    (repo / "node_modules" / "typescript" / "index.ts").write_text("export const t = 1;\n", encoding="utf-8")


def _planned_tools(plan) -> list[str]:
    return sorted(p.name for s in plan.steps if s.kind == "add-dev-packages" for p in s.packages)


def test_discovery_can_assume_a_pending_ignore_for_untracked_files_only(sandbox):
    _fresh_npm_repo(sandbox)
    _dependency_with_own_tools(sandbox.repo)
    sandbox.write("node_modules/vendored/own.ts", "export const own = 1;\n")
    subprocess.run(["git", "add", "-f", "node_modules/vendored/own.ts"], cwd=sandbox.repo, check=True)
    plain = discover(sandbox.repo)
    assumed = discover(sandbox.repo, assume_ignored=["/node_modules/"])
    assert "node_modules/typescript" in {s.rel for s in plain.scopes}
    assert "node_modules/typescript" not in {s.rel for s in assumed.scopes}
    assert "node_modules/vendored/own.ts" in assumed.enumerated_files, "tracked files are never hidden"


def test_a_repo_without_gitignore_plans_what_the_same_repo_with_one_plans(sandbox):
    """P_noign vs P_ign: pre-installed dependency scopes must not change which defaults are bootstrapped."""
    from dataclasses import replace

    from riglib.lint_bootstrap import build_bootstrap_plan
    from riglib.lint_providers import bootstrap_needs
    from riglib.lint_run_config import resolve

    _fresh_npm_repo(sandbox)
    _dependency_with_own_tools(sandbox.repo)
    cfg = resolve({"linters": {"rules": {"groups": {"anti-slop": False}}}})
    env = sandbox.env()

    def plan_for(**kwargs):
        discovery = discover(sandbox.repo, **kwargs)
        return build_bootstrap_plan(bootstrap_needs(discovery, cfg, replace(env)), discovery, cfg, env)

    naive = plan_for()
    assert _planned_tools(naive) == ["oxlint"], "premise: the dependency scope withholds oxfmt (the reported defect)"
    corrected = plan_for(assume_ignored=["/node_modules/"])
    (sandbox.repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    with_gitignore = plan_for()
    assert _planned_tools(corrected) == _planned_tools(with_gitignore) == ["oxfmt", "oxlint"]


def test_the_first_bare_run_plans_oxlint_and_oxfmt_without_a_gitignore(sandbox, tmp_path):
    from riglib import lint_run_plan
    from riglib.lint_run_config import resolve
    from riglib.lint_run_model import RunOptions

    _fresh_npm_repo(sandbox)
    _dependency_with_own_tools(sandbox.repo)
    opts = RunOptions(cwd=str(sandbox.repo), config=None, json=False, plan=False, no_setup=False, fix=False, ci=None,
                      only=(), skip=("limits",), scopes=(), timeout=None)
    planned = lint_run_plan.plan_run(opts, resolve({"linters": {"rules": {"groups": {"anti-slop": False}}}}), sandbox.repo, (),
                                     {"PATH": os.environ["PATH"]}, lambda name: __import__("shutil").which(name, path=str(sandbox.bin_dir)), False)
    packages = sorted(p.name for s in planned.setup.plan.steps if s.kind == "add-dev-packages" for p in s.packages)
    assert packages == ["oxfmt", "oxlint"], "converges in ONE run: no second run is needed to get the formatter"
    assert {s.rel for s in planned.discovery.scopes} == {"."}, "after the ignore the dependency is out of scope"


# ── the post-ignore view is used only by a run that really writes the ignore ───────────────────────


def _run_plan(sandbox, **over):
    from riglib import lint_run_plan
    from riglib.lint_run_config import resolve
    from riglib.lint_run_model import RunOptions

    base = dict(cwd=str(sandbox.repo), config=None, json=False, plan=False, no_setup=False, fix=False, ci=None,
                only=(), skip=("limits",), scopes=(), timeout=None)
    ci = bool(over.get("ci"))
    opts = RunOptions(**{**base, **over})
    cfg = resolve({"linters": {"rules": {"groups": {"anti-slop": False}}}})
    return lint_run_plan.plan_run(opts, cfg, sandbox.repo, (), {"PATH": os.environ["PATH"]},
                                  lambda name: shutil.which(name, path=str(sandbox.bin_dir)), ci)


def _add_dev(planned) -> list[str]:
    return sorted(p.name for s in planned.setup.plan.steps if s.kind == "add-dev-packages" for p in s.packages)


def _check_ids(planned) -> set[str]:
    return {c.id for c in planned.checks}


def _same_repo_with_gitignore(sandbox):
    (sandbox.repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")


@pytest.mark.parametrize("over", [{"plan": True}, {"no_setup": True}, {"ci": True}])
def test_every_read_only_view_previews_the_same_setup_a_gitignored_repo_gets(sandbox, over):
    """P_noign vs P_ign: the printed setup (what a local run WOULD install) is accurate in --plan/--no-setup/--ci too."""
    _fresh_npm_repo(sandbox)
    _dependency_with_own_tools(sandbox.repo)
    before = (sandbox.tree(), _exclude_text(sandbox.repo))
    preview = _run_plan(sandbox, **over)
    assert _add_dev(preview) == ["oxfmt", "oxlint"]
    assert (sandbox.tree(), _exclude_text(sandbox.repo)) == before and sandbox.calls() == [], "previews write and spawn nothing"
    _same_repo_with_gitignore(sandbox)
    assert _add_dev(_run_plan(sandbox, **over)) == _add_dev(preview)


def test_plan_mode_lists_the_checks_of_the_post_ignore_repository_like_a_gitignored_one(sandbox):
    """`--plan` runs nothing, so its checks list is computed on the same view as its setup (planned, not yet runnable)."""
    _fresh_npm_repo(sandbox)
    _dependency_with_own_tools(sandbox.repo)
    before = (sandbox.tree(), _exclude_text(sandbox.repo))
    preview = _run_plan(sandbox, plan=True)
    assert {s.rel for s in preview.discovery.scopes} == {"."}, "the dependency is not a project scope in the preview"
    assert "oxfmt@." in _check_ids(preview) and not any(c.startswith("eslint@node_modules") for c in _check_ids(preview))
    assert {c.id: c.unavailable.state for c in preview.checks if c.unavailable is not None and c.id.startswith("oxfmt")} == {
        "oxfmt@.": "missing-tool"}, "tools that cannot run yet stay honestly missing-tool"
    assert (sandbox.tree(), _exclude_text(sandbox.repo)) == before
    _same_repo_with_gitignore(sandbox)
    assert _check_ids(_run_plan(sandbox, plan=True)) == _check_ids(preview)


@pytest.mark.parametrize("over", [{"no_setup": True}, {"ci": True}])
def test_runs_that_execute_checks_without_setup_still_check_the_real_disk_state(sandbox, over):
    """`--no-setup` / `--ci` RUN checks: they see the repository as it is (the ignore is never written there)."""
    _fresh_npm_repo(sandbox)
    _dependency_with_own_tools(sandbox.repo)
    planned = _run_plan(sandbox, **over)
    assert "node_modules/typescript" in {s.rel for s in planned.discovery.scopes}


def test_pending_ignores_reads_only_git_exclude_steps_and_tolerates_a_missing_key():
    from riglib.lint_bootstrap_model import BootstrapPlan, BootstrapStep
    from riglib.lint_run_plan import _pending_ignores

    def step(kind):
        return BootstrapStep(f"{kind}@x", kind, ".", "m", (), ".", (), "n/a", ())

    def action(options):
        return Action("lint_bootstrap_step", "lint_bootstrap", "x", Path("."), Path("."), options)

    plan = BootstrapPlan(
        (step("git-exclude"), step("add-dev-packages"), step("git-exclude")), (),
        InstallPlan(actions=[action({"git_exclude": ["/node_modules/"]}), action({"git_exclude": ["ignored"]}), action({})], on_conflict="skip"),
        (),
    )
    assert _pending_ignores(plan) == ["/node_modules/"]
    assert _pending_ignores(BootstrapPlan((), (), InstallPlan(on_conflict="skip"), ())) == []
