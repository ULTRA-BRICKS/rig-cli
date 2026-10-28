"""Bootstrap planning/applying for bare ``rig lint`` (rig-cli#781, design D4-D7).

Real tmp git repos, real discovery and provider needs; the package managers are tiny shims on a
controlled PATH (they log argv, edit the manifest, write the lockfile and create the binaries a real
install would), so nothing here touches the network, a real package manager or the real HOME.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from riglib.actions import run_plan
from riglib.actions.runner import _HANDLERS
from riglib.action_tags import ACTION_TAGS
from riglib.catalog import Catalog
from riglib.lint_bootstrap import (
    BootstrapBlocked,
    BootstrapPlan,
    apply_bootstrap,
    build_bootstrap_plan,
    manager_argv,
    select_js_manager,
)
from riglib.lint_discovery import PackageManager, ProjectScope, discover
from riglib.lint_providers import BootstrapNeed, PinnedPackage, bootstrap_needs
from riglib.lint_run_config import resolve
from riglib.plan import Action, InstallPlan
from tests.lint_bootstrap_helpers import NO_SLOP, OXFMT, OXLINT, PINS, RUFF, sandbox  # noqa: F401 -- fixture import


def _catalog(tmp_path: Path) -> Catalog:
    carrier = tmp_path / "agent-tools" / "vendor/anti-slop/skills/install-anti-slop/assets/anti-slop"
    carrier.mkdir(parents=True, exist_ok=True)
    (carrier / "index.ts").write_text("export default {};\n", encoding="utf-8")
    return Catalog(source=tmp_path / "agent-tools")


def _step(plan: BootstrapPlan, kind: str):
    found = [s for s in plan.steps if s.kind == kind]
    assert len(found) == 1, [s.id for s in plan.steps]
    return found[0]


# ── manager_argv: the exact table ──────────────────────────────────────────────────

_A = PinnedPackage("oxlint", "1.90.0", "npm")
_B = PinnedPackage("oxfmt", "0.75.0", "npm")
_R = PinnedPackage("ruff", "0.17.3", "pypi")


@pytest.mark.parametrize(
    ("manager", "workspace", "packages", "expected"),
    [
        ("npm", False, (_A, _B), ("npm", "install", "--save-dev", "--save-exact", "--ignore-scripts", "--no-audit", "--no-fund", "oxlint@1.90.0", "oxfmt@0.75.0")),
        ("npm", True, (_A,), ("npm", "install", "--save-dev", "--save-exact", "--ignore-scripts", "--no-audit", "--no-fund", "oxlint@1.90.0")),
        ("pnpm", False, (_A, _B), ("pnpm", "add", "--save-dev", "--save-exact", "--ignore-scripts", "oxlint@1.90.0", "oxfmt@0.75.0")),
        ("pnpm", True, (_A,), ("pnpm", "add", "--save-dev", "--save-exact", "--ignore-scripts", "-w", "oxlint@1.90.0")),
        ("yarn", False, (_A,), ("yarn", "add", "--dev", "--exact", "--ignore-scripts", "oxlint@1.90.0")),
        ("yarn", True, (_A,), ("yarn", "add", "--dev", "--exact", "--ignore-scripts", "-W", "oxlint@1.90.0")),
        ("yarn-berry", True, (_A,), ("yarn", "add", "--dev", "--exact", "--mode=skip-build", "oxlint@1.90.0")),
        ("bun", False, (_A, _B), ("bun", "add", "--dev", "--exact", "--ignore-scripts", "oxlint@1.90.0", "oxfmt@0.75.0")),
    ],
)
def test_manager_argv_table(manager, workspace, packages, expected):
    assert manager_argv(manager, packages, workspace_root=workspace) == expected


def test_manager_argv_rejects_unknown_manager():
    with pytest.raises(ValueError, match="pip"):
        manager_argv("pip", (_R,), workspace_root=False)


def test_every_js_add_command_disables_lifecycle_scripts_and_pins_exactly():
    for manager in ("npm", "pnpm", "yarn", "yarn-berry", "bun"):
        argv = manager_argv(manager, (_A,), workspace_root=False)
        assert any(a in ("--ignore-scripts", "--mode=skip-build") for a in argv), manager
        assert any(a in ("--save-exact", "--exact") for a in argv), manager
        assert "oxlint@1.90.0" in argv and not any("^" in a or "~" in a for a in argv)


# ── select_js_manager ──────────────────────────────────────────────────────────────


def _scope(pm: PackageManager | None) -> ProjectScope:
    return ProjectScope(
        rel=".", languages=("javascript",), package_manager=pm, tools=(), scripts=(), typescript=None, files={}, evidence=()
    )


def _pm(name: str, *, source: str = "lockfile", ambiguous: tuple[str, ...] = ()) -> PackageManager:
    return PackageManager("js", name, source, None, ".", ambiguous)


def test_select_js_manager_accepts_the_evidenced_manager_when_installed():
    pm = _pm("pnpm")
    assert select_js_manager(_scope(pm), lambda n: f"/bin/{n}") == pm


def test_select_js_manager_berry_needs_the_yarn_binary():
    assert select_js_manager(_scope(_pm("yarn-berry")), lambda n: "/bin/yarn" if n == "yarn" else None).name == "yarn-berry"


def test_select_js_manager_without_package_json_is_no_manifest():
    blocked = select_js_manager(_scope(None), lambda n: "/bin/x")
    assert isinstance(blocked, BootstrapBlocked) and blocked.reason == "no-manifest"


def test_select_js_manager_ambiguous_is_blocked_and_names_the_candidates():
    blocked = select_js_manager(_scope(_pm("", source="ambiguous", ambiguous=("npm", "pnpm"))), lambda n: "/bin/x")
    assert isinstance(blocked, BootstrapBlocked) and blocked.reason == "ambiguous-package-manager"
    assert "npm" in blocked.detail and "pnpm" in blocked.detail and "packageManager" in blocked.detail


def test_select_js_manager_never_falls_back_to_another_manager():
    only_npm = lambda n: "/bin/npm" if n == "npm" else None  # noqa: E731
    blocked = select_js_manager(_scope(_pm("pnpm")), only_npm)
    assert isinstance(blocked, BootstrapBlocked) and blocked.reason == "manager-not-installed"
    assert "pnpm" in blocked.detail


# ── manager selection through real discovery (the D7 matrix) ───────────────────────


@pytest.mark.parametrize(
    ("files", "field", "manager", "argv0"),
    [
        ({}, "pnpm@9.1.0", "pnpm", "pnpm"),
        ({"package-lock.json": "{}"}, None, "npm", "npm"),
        ({"pnpm-lock.yaml": "lockfileVersion: 9\n"}, None, "pnpm", "pnpm"),
        ({"yarn.lock": "# yarn lockfile v1\n"}, None, "yarn", "yarn"),
        ({"yarn.lock": "__metadata:\n", ".yarnrc.yml": "nodeLinker: node-modules\n"}, None, "yarn-berry", "yarn"),
        ({"bun.lock": "{}"}, None, "bun", "bun"),
        ({"pnpm-workspace.yaml": "packages: []\n"}, None, "pnpm", "pnpm"),
        ({}, None, "npm", "npm"),
        ({"package-lock.json": "{}", "yarn.lock": ""}, "pnpm@9.1.0", "pnpm", "pnpm"),
    ],
)
def test_manager_selection_matrix(sandbox, files, field, manager, argv0):
    sandbox.install_managers("npm", "pnpm", "yarn", "bun")
    sandbox.package_json({"packageManager": field} if field else None)
    for rel, text in files.items():
        sandbox.write(rel, text)
    plan, _ = sandbox.plan()
    step = _step(plan, "add-dev-packages")
    assert (step.manager, step.argv[0]) == (manager, argv0)
    assert not plan.blocked


def test_two_lockfiles_without_a_package_manager_field_are_ambiguous(sandbox):
    sandbox.install_managers("npm", "pnpm")
    sandbox.package_json()
    sandbox.write("package-lock.json", "{}")
    sandbox.write("pnpm-lock.yaml", "x: 1\n")
    before = sandbox.tree()
    plan, _ = sandbox.plan()
    assert plan.is_empty()
    assert {b.reason for b in plan.blocked} == {"ambiguous-package-manager"}
    assert {b.tool for b in plan.blocked} == {"oxlint", "oxfmt"}
    assert sandbox.tree() == before


def test_missing_manager_on_path_blocks_and_does_not_substitute_npm(sandbox):
    sandbox.install_managers("npm")  # pnpm is NOT installed
    sandbox.package_json({"packageManager": "pnpm@9.1.0"})
    plan, _ = sandbox.plan()
    assert plan.is_empty()
    assert {b.reason for b in plan.blocked} == {"manager-not-installed"}
    assert "pnpm" in plan.blocked[0].detail
    assert sandbox.calls() == []


def test_pnpm_workspace_root_gets_the_workspace_flag(sandbox):
    sandbox.install_managers("pnpm")
    sandbox.package_json({"packageManager": "pnpm@9.1.0"})
    sandbox.write("pnpm-workspace.yaml", "packages:\n  - 'pkgs/*'\n")
    plan, _ = sandbox.plan()
    assert "-w" in _step(plan, "add-dev-packages").argv


def test_workspace_member_installs_at_the_workspace_root(sandbox):
    sandbox.install_managers("pnpm")
    sandbox.package_json({"packageManager": "pnpm@9.1.0"})
    sandbox.write("pnpm-workspace.yaml", "packages:\n  - 'pkgs/*'\n")
    sandbox.package_json(rel="pkgs/a/package.json")
    plan, _ = sandbox.plan()
    step = _step(plan, "add-dev-packages")
    assert step.cwd == "." and step.writes == ("package.json", "pnpm-lock.yaml")


# ── empty JS project: first run installs, second run is a no-op ─────────────────────


def test_empty_js_project_bootstraps_oxlint_and_oxfmt_then_is_idempotent(sandbox):
    sandbox.install_managers("npm")
    sandbox.package_json()
    plan, _ = sandbox.plan()
    add = _step(plan, "add-dev-packages")
    assert add.id == "js-add-dev@." and add.manager == "npm" and add.lock == "lockfile"
    assert add.argv == manager_argv("npm", (OXLINT, OXFMT), workspace_root=False)
    assert add.writes == ("package.json", "package-lock.json")
    assert [p.name for p in add.packages] == ["oxlint", "oxfmt"]
    config = _step(plan, "write-config")
    assert config.id == "write-config@.:oxlint.config.ts" and config.argv == () and config.lock == "n/a"
    assert plan.install_plan.on_conflict == "skip"
    assert [a.kind for a in plan.install_plan.actions] == ["lint_bootstrap_step", "provision_linter_config"]
    assert sandbox.calls() == [], "planning never spawns a process"

    outcome = apply_bootstrap(plan, root=sandbox.repo, environ={})
    assert not outcome.failed, outcome
    assert [state for _, state, _ in outcome.results] == ["applied", "applied"]
    manifest = json.loads((sandbox.repo / "package.json").read_text())
    assert manifest["devDependencies"] == {"oxlint": OXLINT.version, "oxfmt": OXFMT.version}
    assert (sandbox.repo / "package-lock.json").is_file()
    assert (sandbox.repo / "oxlint.config.ts").read_text().startswith("// Managed by Rig.")
    assert len(sandbox.calls()) == 1

    after_first = sandbox.tree()
    second, _ = sandbox.plan()
    assert second.is_empty() and not second.blocked
    assert second.install_plan.actions == []
    assert apply_bootstrap(second, root=sandbox.repo, environ={}).results == ()
    assert len(sandbox.calls()) == 1, "re-run spawned nothing"
    assert sandbox.tree() == after_first


def test_version_override_replaces_the_pin_for_that_key_only(sandbox):
    sandbox.install_managers("npm")
    sandbox.package_json()
    plan, _ = sandbox.plan(lint={"bootstrap": {"versions": {"oxlint": "1.2.3"}}})
    argv = _step(plan, "add-dev-packages").argv
    assert "oxlint@1.2.3" in argv and f"oxfmt@{OXFMT.version}" in argv


def test_declared_but_not_installed_tool_gets_only_the_frozen_install(sandbox):
    sandbox.install_managers("npm")
    sandbox.package_json({"devDependencies": {"oxlint": "1.90.0", "oxfmt": "0.75.0"}})
    sandbox.write("package-lock.json", "{}")
    sandbox.write(".oxlintrc.json", "{}\n")
    plan, _ = sandbox.plan()
    step = _step(plan, "install-dependencies")
    assert step.id == "js-install@." and step.argv == ("npm", "ci", "--ignore-scripts") and step.packages == ()
    assert [s.kind for s in plan.steps] == ["install-dependencies"]
    outcome = apply_bootstrap(plan, root=sandbox.repo, environ={})
    assert not outcome.failed
    assert plan.install_plan.actions[0].options["expect_declared"] is False
    assert (sandbox.repo / "node_modules/.bin/oxlint").exists()
    assert sandbox.plan()[0].is_empty()


def test_declared_tool_without_a_lockfile_is_blocked_with_a_remediation(sandbox):
    sandbox.install_managers("npm")
    sandbox.package_json({"devDependencies": {"oxlint": "1.90.0", "oxfmt": "0.75.0"}})
    plan, _ = sandbox.plan()
    assert plan.is_empty()
    assert {b.reason for b in plan.blocked} == {"no-lock-capable-manager"}
    assert "lockfile" in plan.blocked[0].detail


def test_manifest_less_javascript_uses_the_tool_cache_and_creates_no_manifest(sandbox):
    sandbox.install_managers("npm")
    sandbox.write("src/index.ts", "export const x = 1;\n")
    before = sandbox.tree()
    cfg = resolve({"linters": {"rules": NO_SLOP}})
    discovery = discover(sandbox.repo)
    needs = (BootstrapNeed(".", "js", "lint", "oxlint", "oxlint", (OXLINT,)),)
    plan = build_bootstrap_plan(needs, discovery, cfg, sandbox.env())
    assert [s.id for s in plan.steps] == [f"js-cache-install@oxlint-{OXLINT.version}", "write-config@.:.oxlintrc.json"]
    assert plan.steps[0].writes == () and plan.steps[1].kind == "write-config"
    assert not plan.blocked, "the anti-slop group is off in this policy, so nothing required is missing"
    assert sandbox.tree() == before and not (sandbox.repo / "package.json").exists()


def test_malformed_package_json_is_blocked_not_overwritten(sandbox):
    sandbox.install_managers("npm")
    sandbox.write("package.json", "{ not json")
    plan, _ = sandbox.plan()
    assert plan.is_empty()
    assert {b.reason for b in plan.blocked} <= {"no-manifest"}
    assert (sandbox.repo / "package.json").read_text() == "{ not json"


# ── configured tools are authoritative: existing configs preserved ─────────────────


def test_eslint_repo_gets_no_oxlint_step(sandbox):
    sandbox.install_managers("npm")
    sandbox.package_json({"devDependencies": {"eslint": "9.0.0"}})
    sandbox.write("eslint.config.js", "export default [];\n")
    sandbox.write("package-lock.json", "{}")
    plan, _ = sandbox.plan()
    assert "oxlint" not in [p.name for s in plan.steps for p in s.packages]
    assert all(b.tool != "oxlint" for b in plan.blocked)


def test_foreign_formatter_gets_no_oxfmt_step(sandbox):
    sandbox.install_managers("npm")
    sandbox.package_json({"devDependencies": {"prettier": "3.0.0"}})
    sandbox.write(".prettierrc", "{}\n")
    sandbox.write("package-lock.json", "{}")
    plan, _ = sandbox.plan()
    names = [p.name for s in plan.steps for p in s.packages]
    assert "oxfmt" not in names and "oxlint" in names


def test_user_oxlint_config_is_never_replaced(sandbox):
    sandbox.install_managers("npm")
    sandbox.package_json()
    user = sandbox.write(".oxlintrc.json", '{ "rules": { "no-debugger": "error" } }\n')
    original = user.read_bytes()
    plan, _ = sandbox.plan()
    assert [s.kind for s in plan.steps] == ["add-dev-packages"]
    assert [p.name for p in plan.steps[0].packages] == ["oxfmt"], "a configured Oxlint is never re-added or reconfigured"
    assert not apply_bootstrap(plan, root=sandbox.repo, environ={}).failed
    assert user.read_bytes() == original and not (sandbox.repo / "oxlint.config.ts").exists()


def test_on_conflict_skip_protects_a_config_that_appears_after_planning(sandbox):
    sandbox.install_managers("npm")
    sandbox.package_json()
    plan, _ = sandbox.plan()
    raced = sandbox.write("oxlint.config.ts", "export default { mine: true };\n")
    original = raced.read_bytes()
    outcome = apply_bootstrap(plan, root=sandbox.repo, environ={})
    assert not outcome.failed
    assert outcome.results[-1][1] == "noop"
    assert raced.read_bytes() == original
    assert not list(sandbox.repo.glob("*.rig-bak*"))


def test_users_oxlint_dependency_without_config_is_not_reconfigured(sandbox):
    sandbox.install_managers("npm")
    sandbox.package_json({"devDependencies": {"oxlint": "1.0.0"}})
    sandbox.write("package-lock.json", "{}")
    sandbox.write("node_modules/.bin/oxlint", "#!/bin/sh\n")
    plan, _ = sandbox.plan()
    assert all(s.kind != "write-config" for s in plan.steps)


# ── config flavor and policy gating ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("node", "rel"),
    [((22, 17, 9), ".oxlintrc.json"), ((22, 18, 0), "oxlint.config.ts"), ((24, 0, 0), "oxlint.config.ts"), (None, ".oxlintrc.json")],
)
def test_config_flavor_follows_the_node_boundary(sandbox, node, rel):
    sandbox.install_managers("npm")
    sandbox.package_json()
    plan, _ = sandbox.plan(node=node)
    config = _step(plan, "write-config")
    assert config.writes == (rel,)
    action = plan.install_plan.actions[-1]
    assert action.options["rel_path"] == rel
    assert ("defineConfig" in action.options["content"]) == (rel == "oxlint.config.ts")
    assert [s.writes for s in plan.steps if s.kind == "write-config"] == [(rel,)], "one flavor per directory"


def test_type_aware_config_only_with_tsgolint_and_typescript_7(sandbox):
    sandbox.install_managers("npm")
    sandbox.package_json({"devDependencies": {"typescript": "7.0.2"}})
    sandbox.write("node_modules/typescript/package.json", json.dumps({"name": "typescript", "version": "7.0.2"}))
    sandbox.write("tsconfig.json", "{}\n")
    plan, _ = sandbox.plan()
    names = [p.name for p in _step(plan, "add-dev-packages").packages]
    assert "oxlint-tsgolint" in names
    assert "options: { typeAware: true }" in plan.install_plan.actions[-1].options["content"]


def test_typescript_5_never_gets_tsgolint_or_type_aware(sandbox):
    sandbox.install_managers("npm")
    sandbox.package_json({"devDependencies": {"typescript": "5.9.3"}})
    sandbox.write("node_modules/typescript/package.json", json.dumps({"name": "typescript", "version": "5.9.3"}))
    sandbox.write("tsconfig.json", "{}\n")
    plan, _ = sandbox.plan()
    assert "oxlint-tsgolint" not in [p.name for p in _step(plan, "add-dev-packages").packages]
    content = plan.install_plan.actions[-1].options["content"]
    assert "typeAware" not in content and "type-check" not in content


def test_linters_disabled_installs_tools_but_writes_no_config(sandbox):
    sandbox.install_managers("npm")
    sandbox.package_json()
    cfg = resolve({"linters": {"enabled": False}})
    discovery = discover(sandbox.repo)
    needs = bootstrap_needs(discovery, cfg, sandbox.env())
    plan = build_bootstrap_plan(needs, discovery, cfg, sandbox.env())
    assert [s.kind for s in plan.steps] == ["add-dev-packages"]


# ── Python ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("marker", [{"uv.lock": "version = 1\n"}, {"poetry.lock": ""}, {"pdm.lock": ""}])
def test_python_project_uses_the_tool_cache_and_never_touches_pyproject_or_the_lock(sandbox, marker):
    sandbox.install_managers("uv", "poetry", "pdm")  # present, and must never be asked to add or sync anything
    sandbox.write("pyproject.toml", '[project]\nname = "demo"\nversion = "0.1.0"\n')
    for rel, text in marker.items():
        sandbox.write(rel, text)
    sandbox.write("app.py", "x = 1\n")
    before = sandbox.tree()
    plan, _ = sandbox.plan()
    slug = f"ruff-{RUFF.version}"
    assert [s.id for s in plan.steps] == [f"py-cache-venv@{slug}", f"py-cache-install@{slug}"]
    assert all(s.kind == "install-tool-cache" and s.writes == () and s.lock == "n/a" for s in plan.steps)
    assert not plan.blocked and sandbox.calls() == [] and sandbox.tree() == before


def test_uv_project_does_not_need_uv_on_path_to_get_ruff(sandbox, monkeypatch):
    monkeypatch.setattr("riglib.lint_bootstrap_cache.python_has_module", lambda python, module: True)
    sandbox.write("pyproject.toml", '[project]\nname = "demo"\nversion = "0"\n')
    sandbox.write("uv.lock", "version = 1\n")
    sandbox.write("app.py", "x = 1\n")
    plan, _ = sandbox.plan()
    assert [s.kind for s in plan.steps] == ["install-tool-cache"] * 2 and not plan.blocked


def test_pip_only_python_project_uses_the_tool_cache_not_a_manifest(sandbox):
    sandbox.install_managers("uv")
    sandbox.write("requirements.txt", "requests==2.0\n")
    sandbox.write("app.py", "x = 1\n")
    before = sandbox.tree()
    plan, _ = sandbox.plan()
    assert not plan.blocked
    assert {s.kind for s in plan.steps} == {"install-tool-cache"}
    assert all(s.writes == () and s.lock == "n/a" for s in plan.steps)
    assert sandbox.tree() == before and not (sandbox.repo / "pyproject.toml").exists()


def test_configured_python_linters_are_not_replaced(sandbox):
    sandbox.install_managers("uv")
    sandbox.write("pyproject.toml", '[project]\nname = "demo"\nversion = "0"\n\n[tool.black]\nline-length = 100\n')
    sandbox.write(".flake8", "[flake8]\nmax-line-length = 100\n")
    sandbox.write("uv.lock", "version = 1\n")
    sandbox.write("app.py", "x = 1\n")
    plan, _ = sandbox.plan()
    assert plan.is_empty() and not plan.blocked


# ── config switches ────────────────────────────────────────────────────────────────


def test_bootstrap_disabled_blocks_every_need(sandbox):
    sandbox.install_managers("npm")
    sandbox.package_json()
    plan, _ = sandbox.plan(lint={"bootstrap": {"enabled": False}})
    assert plan.is_empty() and plan.blocked
    assert {b.reason for b in plan.blocked} == {"disabled-by-config"}


def test_javascript_switch_blocks_only_javascript(sandbox):
    sandbox.install_managers("npm", "uv")
    sandbox.package_json()
    sandbox.write("pyproject.toml", '[project]\nname = "d"\nversion = "0"\n')
    sandbox.write("uv.lock", "version = 1\n")
    sandbox.write("app.py", "x = 1\n")
    plan, _ = sandbox.plan(lint={"bootstrap": {"javascript": False}})
    assert {s.kind for s in plan.steps} == {"install-tool-cache"} and "py-cache-venv@ruff-0.17.3" in [s.id for s in plan.steps]
    assert {b.reason for b in plan.blocked} == {"disabled-by-config"}
    assert {b.tool for b in plan.blocked} == {"oxlint", "oxfmt"}


def test_python_switch_blocks_only_python(sandbox):
    sandbox.install_managers("npm")
    sandbox.package_json()
    sandbox.write("scripts/tool.py", "x = 1\n")
    plan, _ = sandbox.plan(lint={"bootstrap": {"python": False}})
    assert [s.kind for s in plan.steps] == ["add-dev-packages", "write-config"]
    assert {b.tool for b in plan.blocked} == {"ruff"} and {b.reason for b in plan.blocked} == {"disabled-by-config"}


# ── read-only guarantees ───────────────────────────────────────────────────────────


def test_planning_is_read_only_and_spawns_nothing(sandbox):
    sandbox.install_managers("npm")
    sandbox.package_json()
    before = sandbox.tree()
    plan, _ = sandbox.plan()
    assert not plan.is_empty()
    assert sandbox.tree() == before and sandbox.calls() == []


@pytest.mark.parametrize("var", ["CI", "GITHUB_ACTIONS"])
def test_apply_refuses_under_ci_and_changes_nothing(sandbox, monkeypatch, var):
    sandbox.install_managers("npm")
    sandbox.package_json()
    plan, _ = sandbox.plan()
    before = sandbox.tree()
    outcome = apply_bootstrap(plan, root=sandbox.repo, environ={var: "true"})
    assert outcome.failed
    assert {state for _, state, _ in outcome.results} == {"blocked"}
    assert sandbox.tree() == before and sandbox.calls() == []


def test_handler_itself_refuses_under_ci(sandbox, monkeypatch):
    sandbox.install_managers("npm")
    sandbox.package_json()
    plan, _ = sandbox.plan()
    before = sandbox.tree()
    monkeypatch.setenv("CI", "1")
    report = run_plan(InstallPlan(actions=[plan.install_plan.actions[0]], on_conflict="skip"))
    assert report.results[0].status == "error" and "CI" in report.results[0].detail
    assert sandbox.tree() == before and sandbox.calls() == []


def test_handler_refuses_without_allow_mutation(sandbox):
    sandbox.install_managers("npm")
    sandbox.package_json()
    plan, _ = sandbox.plan()
    action = plan.install_plan.actions[0]
    action.options["allow_mutation"] = False
    before = sandbox.tree()
    result = run_plan(InstallPlan(actions=[action], on_conflict="skip")).results[0]
    assert result.status == "error" and "allow_mutation" in result.detail
    assert sandbox.tree() == before and sandbox.calls() == []


def test_falsy_ci_values_do_not_block(sandbox, monkeypatch):
    sandbox.install_managers("npm")
    sandbox.package_json()
    plan, _ = sandbox.plan()
    monkeypatch.setenv("CI", "false")
    assert not apply_bootstrap(plan, root=sandbox.repo, environ={"CI": "false"}).failed


# ── handler error paths ────────────────────────────────────────────────────────────


def _first_action(sandbox) -> Action:
    plan, _ = sandbox.plan()
    return plan.install_plan.actions[0]


def _run(action: Action):
    return run_plan(InstallPlan(actions=[action], on_conflict="skip")).results[0]


def test_nonzero_exit_is_an_error_and_stops_the_config_write(sandbox, monkeypatch):
    sandbox.install_managers("npm")
    sandbox.package_json()
    plan, _ = sandbox.plan()
    monkeypatch.setenv("SHIM_MODE", "fail")
    outcome = apply_bootstrap(plan, root=sandbox.repo, environ={})
    assert outcome.failed
    assert [state for _, state, _ in outcome.results] == ["failed", "blocked"]
    assert "exited 3" in outcome.results[0][2] and "registry unreachable" in outcome.results[0][2]
    assert not (sandbox.repo / "oxlint.config.ts").exists()


def test_declared_version_mismatch_is_an_error(sandbox, monkeypatch):
    sandbox.install_managers("npm")
    sandbox.package_json()
    action = _first_action(sandbox)
    monkeypatch.setenv("SHIM_MODE", "wrong-version")
    result = _run(action)
    assert result.status == "error" and "does not declare the pinned packages" in result.detail


def test_missing_lockfile_after_success_is_an_error(sandbox, monkeypatch):
    sandbox.install_managers("npm")
    sandbox.package_json()
    action = _first_action(sandbox)
    monkeypatch.setenv("SHIM_MODE", "no-lock")
    assert _run(action).status == "error"


def test_precheck_skips_when_already_declared_and_locked(sandbox):
    sandbox.install_managers("npm")
    sandbox.package_json()
    action = _first_action(sandbox)
    assert _run(action).status == "updated"
    assert _run(action).status == "skipped"
    assert len(sandbox.calls()) == 1


def test_missing_manager_binary_at_run_time_is_an_error(sandbox, monkeypatch):
    sandbox.install_managers("npm")
    sandbox.package_json()
    action = _first_action(sandbox)
    (sandbox.bin_dir / "npm").unlink()
    monkeypatch.setenv("PATH", str(sandbox.bin_dir))  # no other npm on the machine may stand in
    result = _run(action)
    assert result.status == "error" and "npm" in result.detail


def test_cwd_with_spaces_works(sandbox):
    sandbox.install_managers("npm")
    sandbox.package_json()
    sandbox.package_json(rel="my pkgs/web app/package.json")
    sandbox.write("my pkgs/web app/package-lock.json", "{}")
    sandbox.write("my pkgs/web app/index.ts", "export {};\n")
    plan, _ = sandbox.plan(scopes=("my pkgs/web app",))
    step = next(s for s in plan.steps if s.kind == "add-dev-packages" and s.cwd == "my pkgs/web app")
    outcome = apply_bootstrap(plan, root=sandbox.repo, environ={})
    assert not outcome.failed, outcome
    manifest = json.loads((sandbox.repo / "my pkgs/web app/package.json").read_text())
    assert manifest["devDependencies"]["oxlint"] == OXLINT.version
    assert sandbox.calls()[0][-1] == str(sandbox.repo / "my pkgs/web app") and step.writes[0] == "my pkgs/web app/package.json"


def test_symlinked_package_json_is_refused(sandbox, tmp_path):
    sandbox.install_managers("npm")
    sandbox.package_json()
    action = _first_action(sandbox)
    outside = tmp_path / "outside.json"
    outside.write_text('{"name": "outside"}\n', encoding="utf-8")
    (sandbox.repo / "package.json").unlink()
    (sandbox.repo / "package.json").symlink_to(outside)
    result = _run(action)
    assert result.status == "error" and "symlink" in result.detail
    assert outside.read_text() == '{"name": "outside"}\n' and sandbox.calls() == []


def test_symlinked_cwd_component_is_refused(sandbox, tmp_path):
    sandbox.install_managers("npm")
    sandbox.package_json()
    action = _first_action(sandbox)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (sandbox.repo / "pkg").symlink_to(outside)
    action.options["cwd"] = "pkg"
    result = _run(action)
    assert result.status == "error" and "symlink" in result.detail and sandbox.calls() == []


@pytest.mark.parametrize("cwd", ["../outside", "/etc"])
def test_escaping_cwd_is_refused(sandbox, cwd):
    sandbox.install_managers("npm")
    sandbox.package_json()
    action = _first_action(sandbox)
    action.options["cwd"] = cwd
    result = _run(action)
    assert result.status == "error" and sandbox.calls() == []


@pytest.mark.parametrize("tamper", ["argv0", "name", "version"])
def test_replayed_action_with_a_foreign_executable_or_option_like_pin_is_refused(sandbox, tamper):
    sandbox.install_managers("npm")
    sandbox.package_json()
    action = _first_action(sandbox)
    if tamper == "argv0":
        action.options["argv"] = ["sh", *action.options["argv"][1:]]
    else:
        pkg = dict(action.options["packages"][0])
        pkg[tamper] = "--registry=http://evil" if tamper == "name" else "-g"
        action.options["packages"] = [pkg]
    result = _run(action)
    assert result.status == "error" and sandbox.calls() == []


def test_malformed_action_is_an_error(sandbox):
    action = Action("lint_bootstrap_step", "lint_bootstrap", "x", sandbox.repo, sandbox.repo, {"allow_mutation": True})
    assert _run(action).status == "error"


def test_apply_refuses_a_plan_built_for_another_root(sandbox, tmp_path):
    sandbox.install_managers("npm")
    sandbox.package_json()
    plan, _ = sandbox.plan()
    other = tmp_path / "other"
    other.mkdir()
    outcome = apply_bootstrap(plan, root=other, environ={})
    assert outcome.failed and outcome.results[0][1] == "failed" and sandbox.calls() == []


# ── wiring ─────────────────────────────────────────────────────────────────────────


def test_handler_is_registered_and_tagged():
    assert "lint_bootstrap_step" in _HANDLERS
    tag = ACTION_TAGS["lint_bootstrap_step"]
    assert (tag.category, tag.audience) == ("edits_file", "human")


def test_json_policy_config_written_for_old_node_is_valid_json_with_header(sandbox):
    sandbox.install_managers("npm")
    sandbox.package_json()
    plan, _ = sandbox.plan(node=(20, 19, 0))
    assert not apply_bootstrap(plan, root=sandbox.repo, environ={}).failed
    text = (sandbox.repo / ".oxlintrc.json").read_text()
    assert text.startswith("// Managed by Rig.")
    body = "\n".join(line for line in text.splitlines() if not line.startswith("//"))
    assert json.loads(body)["categories"]["correctness"] == "error"
    assert not (sandbox.repo / "oxlint.config.ts").exists()


# ── framework-aware Oxlint config ──────────────────────────────────────────────────


def _written_config(plan: BootstrapPlan) -> dict:
    [action] = [a for a in plan.install_plan.actions if a.kind == "provision_linter_config"]
    text = action.options["content"]
    return json.loads("\n".join(line for line in text.splitlines() if not line.startswith("//")))


@pytest.mark.parametrize(
    ("deps", "source", "expected"),
    [
        ({"react": "18.3.1"}, "src/App.tsx", ["react"]),
        ({"next": "14.2.0", "react": "18.3.1"}, "app/page.tsx", ["react", "nextjs"]),
        ({"vue": "3.4.0"}, "src/App.vue", ["vue"]),
        ({"nuxt": "3.11.0"}, "app.vue", ["vue"]),
    ],
)
def test_detected_frameworks_get_only_their_documented_oxlint_plugins(sandbox, deps, source, expected):
    sandbox.install_managers("npm")
    sandbox.package_json({"dependencies": deps})
    sandbox.write(source, "export const x = 1;\n")
    plan, _ = sandbox.plan(node=(20, 19, 0))
    plugins = _written_config(plan)["plugins"]
    assert plugins == ["eslint", "typescript", "unicorn", "oxc", *expected]
    assert "options" not in _written_config(plan), "never type-aware without TypeScript 7 + tsgolint"


def test_framework_files_alone_are_enough_to_detect_the_framework(sandbox):
    sandbox.install_managers("npm")
    sandbox.package_json()
    sandbox.write("src/Button.jsx", "export const Button = () => <button />;\n")
    plan, _ = sandbox.plan(node=(20, 19, 0))
    assert "react" in _written_config(plan)["plugins"]


def test_plain_typescript_gets_no_framework_plugins(sandbox):
    sandbox.install_managers("npm")
    sandbox.package_json({"devDependencies": {"typescript": "5.4.0"}})
    sandbox.write("src/index.ts", "export const x = 1;\n")
    plan, _ = sandbox.plan(node=(20, 19, 0))
    assert "plugins" not in _written_config(plan)


def test_svelte_has_no_builtin_plugin_so_the_defaults_stay_and_a_note_says_so(sandbox):
    sandbox.install_managers("npm")
    sandbox.package_json({"dependencies": {"svelte": "5.0.0"}})
    sandbox.write("src/App.svelte", "<script>let x = 1;</script>\n")
    plan, _ = sandbox.plan(node=(20, 19, 0))
    assert "plugins" not in _written_config(plan)
    assert any("svelte" in note for note in plan.notes)


def test_existing_oxlint_config_is_never_rewritten_for_a_framework(sandbox):
    sandbox.install_managers("npm")
    sandbox.package_json({"dependencies": {"react": "18.3.1"}})
    sandbox.write("src/App.tsx", "export const x = 1;\n")
    original = '{ "rules": {} }\n'
    sandbox.write(".oxlintrc.json", original)
    plan, _ = sandbox.plan()
    assert not [a for a in plan.install_plan.actions if a.kind == "provision_linter_config"]
    assert (sandbox.repo / ".oxlintrc.json").read_text() == original


@pytest.mark.parametrize(("dep", "source", "partial"), [("vue", "src/App.vue", True), ("nuxt", "app.vue", True), ("react", "src/App.tsx", False)])
def test_template_frameworks_are_reported_as_partial_oxlint_coverage(sandbox, dep, source, partial):
    sandbox.install_managers("npm")
    sandbox.package_json({"dependencies": {dep: "1.0.0"}})
    sandbox.write(source, "export const x = 1;\n")
    plan, _ = sandbox.plan()
    assert any("template-lint-uncovered" in note for note in plan.notes) is partial


# ── declared-but-uninstalled dependencies of a scope (fresh checkout) ──────────────

_APP_DEPS = {"dependencies": {"react": "18.3.1", "recharts": "2.12.0"}, "devDependencies": {"typescript": "5.4.0"}}
# Lint (ESLint) and format (Prettier) are both configured, so the ONLY thing missing is node_modules.
_CONFIGURED = {
    "eslint.config.js": "export default [];\n", ".prettierrc": "{}\n", "tsconfig.json": "{}\n",
    "src/App.tsx": "export const x = 1;\n",
}


def _miniapp(sandbox, *, lock: str | None = "bun.lock", extra: dict | None = None) -> None:
    """A nested app (Finance miniapp shape): deps declared, node_modules absent, lint/typecheck configured."""
    sandbox.package_json({**_APP_DEPS, **(extra or {})}, rel="miniapp/package.json")
    for rel, text in _CONFIGURED.items():
        sandbox.write(f"miniapp/{rel}", text)
    if lock:
        sandbox.write(f"miniapp/{lock}", "lock\n")


def test_nested_app_with_missing_node_modules_gets_the_frozen_install_then_is_idempotent(sandbox):
    sandbox.install_managers("bun")
    _miniapp(sandbox)
    before_manifest = (sandbox.repo / "miniapp/package.json").read_bytes()
    plan, _ = sandbox.plan()
    step = _step(plan, "install-dependencies")
    assert step.id == "js-install@miniapp" and step.cwd == "miniapp" and step.packages == ()
    assert step.argv == ("bun", "install", "--frozen-lockfile", "--ignore-scripts")
    assert plan.install_plan.actions[0].options["expect_declared"] is False
    assert sandbox.calls() == [], "planning spawns nothing"

    outcome = apply_bootstrap(plan, root=sandbox.repo, environ={})
    assert not outcome.failed and [s for _, s, _ in outcome.results] == ["applied"]
    assert (sandbox.repo / "miniapp/node_modules/.bin/react").exists()
    assert (sandbox.repo / "miniapp/package.json").read_bytes() == before_manifest
    assert (sandbox.repo / "miniapp/bun.lock").read_text() == "lock\n", "the lockfile is never rewritten"

    calls = len(sandbox.calls())
    again, _ = sandbox.plan()
    assert again.is_empty() and not again.blocked
    assert apply_bootstrap(again, root=sandbox.repo, environ={}).results == ()
    assert len(sandbox.calls()) == calls, "second run spawns nothing"


def test_missing_dependencies_without_a_lockfile_are_a_named_blocked_state(sandbox):
    sandbox.install_managers("npm")
    _miniapp(sandbox, lock=None)
    plan, _ = sandbox.plan()
    assert plan.is_empty()
    [blocked] = [b for b in plan.blocked if b.scope == "miniapp"]
    assert blocked.reason == "no-lock-capable-manager" and "lockfile" in blocked.detail and "install" in blocked.detail
    assert not (sandbox.repo / "miniapp/node_modules").exists() and not list((sandbox.repo / "miniapp").glob("*lock*"))


def test_missing_dependencies_with_two_lockfiles_are_blocked_as_ambiguous(sandbox):
    sandbox.install_managers("npm", "bun")
    _miniapp(sandbox)
    sandbox.write("miniapp/package-lock.json", "{}")
    plan, _ = sandbox.plan()
    assert plan.is_empty() and "ambiguous-package-manager" in {b.reason for b in plan.blocked}


def test_missing_dependencies_with_the_manager_absent_are_blocked_not_swapped(sandbox):
    sandbox.install_managers("npm")
    _miniapp(sandbox)
    plan, _ = sandbox.plan()
    assert plan.is_empty() and "manager-not-installed" in {b.reason for b in plan.blocked}
    assert sandbox.calls() == []


def test_installed_dependencies_and_declaration_free_apps_need_no_restore(sandbox):
    sandbox.install_managers("bun")
    _miniapp(sandbox)
    sandbox.write("miniapp/node_modules/.bin/tsc", "#!/bin/sh\n")
    assert not [s for s in sandbox.plan()[0].steps if s.kind == "install-dependencies"]


def test_the_dependency_restore_is_refused_under_ci_and_never_runs(sandbox):
    sandbox.install_managers("bun")
    _miniapp(sandbox)
    plan, _ = sandbox.plan()
    before = sandbox.tree()
    outcome = apply_bootstrap(plan, root=sandbox.repo, environ={"CI": "true"})
    assert outcome.failed and {s for _, s, _ in outcome.results} == {"blocked"}
    assert sandbox.tree() == before and sandbox.calls() == []


def test_a_mixed_bun_and_uv_scope_installs_js_with_bun_and_never_runs_uv(sandbox):
    sandbox.install_managers("bun", "uv")
    _miniapp(sandbox)
    sandbox.write("miniapp/pyproject.toml", '[project]\nname = "m"\nversion = "0"\ndependencies = ["torch==9999"]\n')
    sandbox.write("miniapp/uv.lock", "version = 1\n")
    sandbox.write("miniapp/server.py", "import os\n")
    plan, _ = sandbox.plan()
    js = [s for s in plan.steps if s.kind != "install-tool-cache"]
    assert js and {s.manager for s in js} == {"bun"}
    assert all(s.argv[0] != "uv" for s in plan.steps)
    # Only the JS step is applied here: the Python tool-cache steps would run a real interpreter and pip.
    js_actions = [a for s, a in zip(plan.steps, plan.install_plan.actions, strict=True) if s.kind != "install-tool-cache"]
    assert not apply_bootstrap(replace(plan, steps=tuple(js), install_plan=InstallPlan(actions=js_actions, on_conflict="skip")),
                               root=sandbox.repo, environ={}).failed
    assert [c[0] for c in sandbox.calls()] == ["bun"]


@pytest.mark.parametrize("value", ["TRUE", " 1 ", "yes", "on"])
def test_explicit_true_ci_value_is_ci_for_apply_and_the_handler(sandbox, monkeypatch, value):
    """The single CI rule is an explicit true word (lint_ci_env); the planner must use the same predicate."""
    from riglib.actions.lint_policy_actions import ci_env_truthy

    assert ci_env_truthy({"CI": value})
    sandbox.install_managers("npm")
    sandbox.package_json()
    plan, _ = sandbox.plan()
    before = sandbox.tree()
    assert apply_bootstrap(plan, root=sandbox.repo, environ={"CI": value}).failed
    monkeypatch.setenv("CI", value)
    assert run_plan(InstallPlan(actions=[plan.install_plan.actions[0]], on_conflict="skip")).results[0].status == "error"
    assert sandbox.tree() == before and sandbox.calls() == []


# ── the dependency restore follows ANY selected check that needs the dependencies ──────────────────


@pytest.mark.parametrize("only", [("lint",), ("typecheck",), ("format",), ("scripts",)])
def test_the_dependency_restore_follows_any_selected_check(sandbox, only):
    sandbox.install_managers("bun")
    _miniapp(sandbox)
    plan, _ = sandbox.plan(lint={"categories": {c: c in only for c in ("lint", "typecheck", "format", "limits", "scripts")}})
    assert [s.id for s in plan.steps if s.kind == "install-dependencies"] == ["js-install@miniapp"]


def test_the_dependency_restore_is_skipped_when_only_limits_is_selected(sandbox):
    sandbox.install_managers("bun")
    _miniapp(sandbox)
    plan, _ = sandbox.plan(lint={"categories": {"lint": False, "typecheck": False, "format": False, "limits": True, "scripts": False}})
    assert not plan.steps and not plan.blocked

