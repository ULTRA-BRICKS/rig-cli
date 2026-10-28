"""The Rig anti-slop Oxlint policy during bootstrap: hosted in the tool cache, never copied into the repository.

The vendored anti-slop plugin is upstream third-party code. A fresh repo gets it as a pinned directory
of the Rig tool cache (bundle copy + `@oxlint/plugins` + rendered policy), reproducible in CI, so the
repo gains no bundle, no generated config and no manifest dependency for it. Managed artifacts that a
repo already carries (e.g. from `rig apply`) keep their provenance reporting. Shim package managers and
a hermetic catalog; nothing touches the network or the real HOME.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from riglib.catalog import Catalog
from riglib.lint_bootstrap import apply_bootstrap
from riglib.lint_discovery import discover
from riglib.lint_policy import render_oxlint_config
from riglib.lint_policy_cache import cached_policy_file
from riglib.lint_run_config import resolve
from tests.lint_bootstrap_helpers import sandbox  # noqa: F401 -- fixture import

_CARRIER = "vendor/anti-slop/skills/install-anti-slop/assets/anti-slop"


def _catalog(tmp_path: Path) -> Catalog:
    carrier = tmp_path / "agent-tools" / _CARRIER
    (carrier / "rules").mkdir(parents=True, exist_ok=True)
    for name in ("skills", "agent-hooks"):  # what `catalog.resolve_source` requires of a checkout
        (tmp_path / "agent-tools" / name).mkdir(exist_ok=True)
    (carrier / "index.ts").write_text("export default {};\n", encoding="utf-8")
    (carrier / "rules" / "r.ts").write_text("export const r = 1;\n", encoding="utf-8")
    return Catalog(source=tmp_path / "agent-tools")


def _cache_dirs() -> list[Path]:
    root = Path.home() / ".cache" / "rig" / "lint-tools" / "policy"
    return sorted(root.iterdir()) if root.is_dir() else []


def _full(sandbox, tmp_path, **kwargs):
    return sandbox.plan(rules={}, catalog=_catalog(tmp_path), **kwargs)[0]


# ── the plan ───────────────────────────────────────────────────────────────────────


def test_anti_slop_is_hosted_in_the_tool_cache_and_adds_nothing_to_the_repo(sandbox, tmp_path):
    sandbox.install_managers("npm")
    sandbox.package_json()
    plan = _full(sandbox, tmp_path)
    steps = {s.id.split("@")[0]: s for s in plan.steps}
    assert {"js-add-dev", "policy-bundle", "policy-plugins", "policy-config"} <= set(steps)
    assert [p.name for p in steps["js-add-dev"].packages] == ["oxlint", "oxfmt"], "@oxlint/plugins is never a project dependency"
    assert not [a for a in plan.install_plan.actions if a.kind in ("provision_linter_bundle", "provision_linter_config")]
    for name in ("policy-bundle", "policy-plugins", "policy-config"):
        assert steps[name].kind == "install-tool-cache" and steps[name].writes == () and steps[name].lock == "n/a"
    assert any(n.startswith("anti-slop policy hosted in the Rig tool cache") and "nothing is added to the repository" in n for n in plan.notes)
    assert not plan.blocked and not plan.policy_unavailable
    text = steps["policy-config"].content
    config = json.loads("\n".join(line for line in text.splitlines() if not line.startswith("//")))
    assert config["jsPlugins"][0]["specifier"].endswith("/anti-slop/index.ts") and config["jsPlugins"][0]["specifier"].startswith("/")
    assert "ignorePatterns" not in config, "the bundle is outside the repo: nothing to exclude"


def test_first_run_provisions_the_cache_second_run_changes_nothing(sandbox, tmp_path):
    sandbox.install_managers("npm")
    sandbox.package_json()
    plan = _full(sandbox, tmp_path)
    outcome = apply_bootstrap(plan, root=sandbox.repo, environ={})
    assert not outcome.failed, outcome
    [directory] = _cache_dirs()
    assert (directory / "anti-slop" / "index.ts").read_text() == "export default {};\n"
    assert (directory / "anti-slop" / "rules" / "r.ts").is_file() and (directory / "node_modules" / "@oxlint" / "plugins" / "package.json").is_file()
    assert (directory / "oxlint.policy.json").is_file()
    tree = sandbox.tree()
    again = _full(sandbox, tmp_path)
    assert again.is_empty() and not again.blocked
    assert sandbox.tree() == tree and "tools" not in {p.split("/")[0] for p in tree}
    shas = {p: p.read_bytes() for p in directory.rglob("*") if p.is_file()}
    assert apply_bootstrap(again, root=sandbox.repo, environ={}).results == ()
    assert {p: p.read_bytes() for p in directory.rglob("*") if p.is_file()} == shas, "managed bytes are stable"


def test_the_cache_policy_steps_run_under_ci_and_write_nothing_in_the_repo(sandbox, tmp_path):
    sandbox.install_managers("npm")
    sandbox.package_json({"devDependencies": {"oxlint": "1.90.0"}})
    sandbox.write("package-lock.json", "{}")
    plan = _full(sandbox, tmp_path)
    kinds = {s.id.split("@")[0] for s in plan.steps}
    assert {"policy-bundle", "policy-package", "policy-plugins", "policy-config"} <= kinds
    before = sandbox.tree()
    outcome = apply_bootstrap(plan, root=sandbox.repo, environ={"CI": "true"})
    states = {step_id.split("@")[0]: state for step_id, state, _ in outcome.results}
    assert states["policy-config"] == "applied" and sandbox.tree() == before


def test_the_provider_finds_the_installed_policy_and_nothing_before_it(sandbox, tmp_path, monkeypatch):
    sandbox.install_managers("npm")
    sandbox.package_json()
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(_catalog(tmp_path).source))
    cfg = resolve({"linters": {"rules": {}}})
    scope = discover(sandbox.repo).scope(".")
    assert cached_policy_file(scope, discover(sandbox.repo), cfg, {}) is None
    assert not apply_bootstrap(_full(sandbox, tmp_path), root=sandbox.repo, environ={}).failed
    found = cached_policy_file(scope, discover(sandbox.repo), cfg, {})
    assert found is not None and found.name == "oxlint.policy.json" and found.is_file()


def test_a_user_oxlint_config_means_no_cache_policy(sandbox, tmp_path, monkeypatch):
    sandbox.install_managers("npm")
    sandbox.package_json({"devDependencies": {"oxlint": "1.90.0"}})
    sandbox.write(".oxlintrc.json", '{ "rules": {} }\n')
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(_catalog(tmp_path).source))
    plan = _full(sandbox, tmp_path)
    assert not [s for s in plan.steps if s.id.startswith("policy-")] and not plan.blocked
    discovery = discover(sandbox.repo)
    assert cached_policy_file(discovery.scope("."), discovery, resolve({"linters": {"rules": {}}}), {}) is None


def test_a_changed_bundle_gets_its_own_cache_directory(sandbox, tmp_path):
    sandbox.install_managers("npm")
    sandbox.package_json()
    catalog = _catalog(tmp_path)
    first, _ = sandbox.plan(rules={}, catalog=catalog)
    (catalog.source / _CARRIER / "index.ts").write_text("export default { v: 2 };\n")
    second, _ = sandbox.plan(rules={}, catalog=catalog)
    assert {s.cache_dir for s in first.steps if s.id.startswith("policy-")}.isdisjoint({s.cache_dir for s in second.steps if s.id.startswith("policy-")})


# ── the policy cannot be hosted ────────────────────────────────────────────────────


def test_without_a_catalog_the_policy_is_an_explicit_non_green_state_and_nothing_is_written_to_the_repo(sandbox):
    sandbox.install_managers("npm")
    sandbox.package_json()
    plan, _ = sandbox.plan(rules={})
    assert [s.kind for s in plan.steps] == ["add-dev-packages"], "no config and no bundle in the repo"
    [blocked] = [b for b in plan.blocked if b.reason == "required-policy-unavailable"]
    assert blocked.tool == "oxlint" and "agent-tools catalog" in blocked.detail and plan.policy_unavailable
    assert not apply_bootstrap(plan, root=sandbox.repo, environ={}).failed
    again, _ = sandbox.plan(rules={})
    assert again.is_empty() and again.policy_unavailable, "never quietly green on a later run"
    assert not (sandbox.repo / "oxlint.config.ts").exists() and not _cache_dirs()


def test_the_policy_without_npm_is_blocked_with_a_remediation(sandbox, tmp_path):
    sandbox.package_json({"devDependencies": {"oxlint": "1.90.0"}})
    sandbox.write("package-lock.json", "{}")
    sandbox.write("node_modules/.bin/oxlint", "#!/bin/sh\n").chmod(0o755)  # installed: only the hosted policy's plugin package is missing
    plan = _full(sandbox, tmp_path)
    assert plan.policy_unavailable and "npm" in [b for b in plan.blocked if b.reason == "required-policy-unavailable"][0].detail


def test_opting_out_of_anti_slop_needs_no_catalog_and_writes_the_base_config(sandbox):
    sandbox.install_managers("npm")
    sandbox.package_json()
    plan, _ = sandbox.plan()  # NO_SLOP
    assert not plan.blocked and "write-config" in [s.kind for s in plan.steps]
    assert not [s for s in plan.steps if s.id.startswith("policy-")]


# ── source-only JavaScript (no package.json) uses the very same route ──────────────


def test_manifest_less_javascript_hosts_oxlint_and_the_policy_in_the_cache_and_is_not_blocked(sandbox, tmp_path):
    sandbox.install_managers("npm")
    sandbox.write("src/index.ts", "export const x = 1;\n")
    before = sandbox.tree()
    plan = _full(sandbox, tmp_path)
    ids = {s.id.split("@")[0] for s in plan.steps}
    assert {"js-cache-install", "policy-bundle", "policy-plugins", "policy-config"} <= ids
    assert "write-config" not in {s.kind for s in plan.steps} and not plan.blocked
    assert sandbox.tree() == before and not (sandbox.repo / "package.json").exists()


def test_manifest_less_javascript_without_a_catalog_is_non_green(sandbox):
    sandbox.install_managers("npm")
    sandbox.write("src/index.ts", "export const x = 1;\n")
    plan, _ = sandbox.plan(rules={})
    assert plan.policy_unavailable


# ── a bundle a repo already carries (e.g. from `rig apply`) is ordinary, user-visible code ──


def _bundle_in_repo(sandbox, tmp_path) -> Catalog:
    catalog = _catalog(tmp_path)
    shutil.copytree(catalog.source / _CARRIER, sandbox.repo / "tools/oxlint/anti-slop")
    sandbox.write("oxlint.config.ts", render_oxlint_config({}))
    return catalog


def test_no_generated_config_excludes_any_repo_path_from_oxlint(sandbox):
    text = render_oxlint_config({})
    assert "ignorePatterns" not in text and "tools/oxlint/anti-slop/**" not in text
    assert "ignorePatterns" not in render_oxlint_config({}, flavor="json")


def test_a_bundle_in_the_repo_is_not_special_cased_by_planning_or_the_formatter(sandbox, tmp_path):
    sandbox.install_managers("npm")
    sandbox.package_json()
    catalog = _bundle_in_repo(sandbox, tmp_path)
    plan, _ = sandbox.plan(rules={}, catalog=catalog)
    assert not [n for n in plan.notes if "managed" in n.lower()] and not plan.blocked
    from riglib.lint_providers import plan_checks

    cfg = resolve({"linters": {"rules": {}}})
    env = sandbox.env()
    fmt = [c for c in plan_checks(discover(sandbox.repo), cfg, env) if c.provider == "oxfmt"]
    assert all(a.startswith("-") or a in ("oxfmt", "/") or "/" in a for a in (fmt[0].argv or ())) and not any(
        a.startswith("!") for a in (fmt[0].argv or ())
    ), "no negation operand hides repo paths from the formatter"


def test_a_modified_file_inside_a_managed_looking_path_is_still_checked_by_real_oxlint(sandbox, tmp_path):
    """Own source under tools/oxlint/anti-slop/ is linted like any other file: the generated config hides nothing."""
    from tests.test_lint_bootstrap_oxlint import oxlint_binary

    oxlint = oxlint_binary(tmp_path)
    import subprocess

    sandbox.package_json({"type": "module"})
    sandbox.write("tools/oxlint/anti-slop/mine.ts", "debugger;\n")
    sandbox.write(".oxlintrc.json", render_oxlint_config({"groups": {"anti-slop": False}}, flavor="json"))
    done = subprocess.run([str(oxlint), "-c", ".oxlintrc.json", "tools"], cwd=sandbox.repo, capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode == 1 and "tools/oxlint/anti-slop/mine.ts" in done.stdout + done.stderr


# ── TypeScript >= 7 without oxlint-tsgolint: only the optional type-aware portion is dropped ──


def _ts7_repo(sandbox, dev: dict) -> None:
    import json as _json

    sandbox.install_managers("npm")
    sandbox.package_json({"devDependencies": {"typescript": "7.0.2", **dev}})
    sandbox.write("package-lock.json", "{}")
    sandbox.write("node_modules/typescript/package.json", _json.dumps({"name": "typescript", "version": "7.0.2"}))
    sandbox.write("tsconfig.json", "{}\n")


def test_ts7_without_tsgolint_drops_only_the_type_aware_portion_with_a_note(sandbox, tmp_path):
    _ts7_repo(sandbox, {"oxlint": "1.90.0"})
    sandbox.write("node_modules/.bin/oxlint", "#!/bin/sh\n").chmod(0o755)
    plan = _full(sandbox, tmp_path)
    text = next(s.content for s in plan.steps if s.id.startswith("policy-config"))
    assert "typeAware" not in text and "Type-info rules omitted" in text
    for core in ('"correctness": "error"', '"anti-slop/no-chained-type-assertions": "error"', '"typescript/no-non-null-assertion": "error"'):
        assert core in text, f"the core policy keeps {core}"
    assert any("type-aware linting is disabled" in n and "oxlint-tsgolint" in n for n in plan.notes)
    assert not plan.blocked and not plan.policy_unavailable


def test_ts7_with_tsgolint_being_added_renders_the_type_aware_policy_on_the_first_run(sandbox, tmp_path):
    _ts7_repo(sandbox, {})
    plan = _full(sandbox, tmp_path)
    assert "oxlint-tsgolint" in [p.name for p in next(s for s in plan.steps if s.id.startswith("js-add-dev")).packages]
    assert '"typeAware": true' in next(s.content for s in plan.steps if s.id.startswith("policy-config"))
    assert not any("type-aware linting is disabled" in n for n in plan.notes)


# ── two Oxlint config flavors in one directory ─────────────────────────────────────


def test_two_oxlint_config_flavors_in_one_directory_fail_honestly_instead_of_picking_one(sandbox):
    sandbox.install_managers("npm")
    sandbox.package_json({"devDependencies": {"oxlint": "1.90.0"}})
    sandbox.write("node_modules/.bin/oxlint", "#!/bin/sh\n").chmod(0o755)
    sandbox.write(".oxlintrc.json", "{}\n")
    sandbox.write("oxlint.config.ts", "export default {};\n")
    before = sandbox.tree()
    plan, _ = sandbox.plan()
    [blocked] = [b for b in plan.blocked if b.reason == "conflicting-config"]
    assert ".oxlintrc.json" in blocked.detail and "oxlint.config.ts" in blocked.detail and "one config per directory" in blocked.detail
    assert plan.setup_incomplete and sandbox.tree() == before, "nothing is chosen, written or deleted for the user"


def test_one_config_per_directory_is_fine_and_nested_directories_may_differ(sandbox):
    sandbox.install_managers("npm")
    sandbox.package_json({"devDependencies": {"oxlint": "1.90.0"}})
    sandbox.write("node_modules/.bin/oxlint", "#!/bin/sh\n").chmod(0o755)
    sandbox.write(".oxlintrc.json", "{}\n")
    sandbox.package_json(rel="pkg/package.json")
    sandbox.write("pkg/oxlint.config.ts", "export default {};\n")
    plan, _ = sandbox.plan()
    assert not [b for b in plan.blocked if b.reason == "conflicting-config"]


# ── the key the check looks up is the key bootstrap provisioned (also under CI) ────────────────────


def _ts7_policy_dirs(sandbox, tmp_path, environ):
    from dataclasses import replace as _replace

    _ts7_repo(sandbox, {"oxlint": "1.90.0"})
    sandbox.write("node_modules/.bin/oxlint", "#!/bin/sh\n").chmod(0o755)
    catalog = _catalog(tmp_path)
    cfg = resolve({"linters": {"rules": {}}})
    discovery = discover(sandbox.repo)
    env = _replace(sandbox.env(), environ=environ)
    from riglib.lint_bootstrap import build_bootstrap_plan
    from riglib.lint_providers import bootstrap_needs

    plan = build_bootstrap_plan(bootstrap_needs(discovery, cfg, env), discovery, cfg, env, catalog=catalog)
    assert not apply_bootstrap(plan, root=sandbox.repo, environ=environ).failed or environ.get("CI")
    return discovery, cfg, env, catalog


@pytest.mark.parametrize("environ", [{}, {"CI": "true"}])
def test_bootstrap_and_check_agree_on_the_policy_key_for_typescript_7(sandbox, tmp_path, monkeypatch, environ):
    discovery, cfg, env, catalog = _ts7_policy_dirs(sandbox, tmp_path, environ)
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(catalog.source))
    found = cached_policy_file(discovery.scope("."), discovery, cfg, env.environ)
    assert found is not None and found.is_file(), "the policy bootstrap provisioned is the one the Oxlint check finds"

