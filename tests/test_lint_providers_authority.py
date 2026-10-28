"""Acceptance fixtures for `rig lint` providers (review F2-F7): nothing configured is suppressed, nothing
writes, orphans stay lintable through the pinned Rig cache, and coverage never reads as a clean pass.

Facts are built in memory; binaries are stand-ins in a tmp repo / tmp cache. No tool runs, no network.
"""
from __future__ import annotations

from riglib import lint_providers as lp
from riglib.lint_discovery_model import Discovery, UnknownLanguage
from riglib.lint_tool_cache import environment_dir
from tests.lint_providers_helpers import (
    JS,
    PY,
    TS,
    config,
    discovery,
    install,
    make_repo,
    plan,
    pm,
    runnable,
    scope,
    script,
    state,
    tool,
    tool_env,
    ts_info,
)

NPM = {"npm": "/bin/npm"}


def _js_repo(tmp_path, names=("eslint", "oxlint", "prettier", "biome")):
    root = make_repo(tmp_path)
    for name in names:
        install(root, f"node_modules/.bin/{name}")
    return root


# ---------------------------------------------------------------- F3: complementary tools stay separate


def test_eslint_and_oxlint_both_run_when_the_script_only_covers_a_subtree(tmp_path):
    root = _js_repo(tmp_path)
    tools = (tool("eslint", "lint", config=("eslint.config.js",), scripts=("lint",)),
             tool("oxlint", "lint", config=(".oxlintrc.json",)))
    sc = scope(".", (TS,), tools=tools, scripts=(script("lint", covers=("eslint",)),))  # `eslint src`: no runs
    checks = plan(root, sc, which=NPM)
    assert {"eslint@.", "oxlint@.", "script:package.json:lint@."} <= runnable(checks)


def test_only_the_exact_duplicate_of_an_unrestricted_script_is_demoted(tmp_path):
    root = _js_repo(tmp_path)
    tools = (tool("eslint", "lint", config=("eslint.config.js",)), tool("oxlint", "lint", config=(".oxlintrc.json",)))
    sc = scope(".", (TS,), tools=tools, scripts=(script("lint", covers=("eslint",), runs=("eslint:lint",)),))
    checks = plan(root, sc, which=NPM)
    assert state(checks["eslint@."]) == ("not-applicable", "covered-by:script:package.json:lint@.")
    assert checks["oxlint@."].unavailable is None


def test_black_and_ruff_format_are_both_planned(tmp_path):
    root = make_repo(tmp_path)
    for name in ("black", "ruff"):
        install(root, f".venv/bin/{name}")
    sc = scope(".", (PY,), tools=(tool("black", "format"), tool("ruff", "lint", config=("ruff.toml",))))
    checks = plan(root, sc)
    venv = str(root / ".venv" / "bin")
    assert checks["black@."].argv == (f"{venv}/black", "--check", ".")
    assert checks["ruff-format@."].argv == (f"{venv}/ruff", "format", "--check")


def test_scoped_scripts_for_different_trees_both_run(tmp_path):
    root = make_repo(tmp_path)
    sc = scope(".", (TS,), scripts=(script("lint:web"), script("lint:api")))
    checks = plan(root, sc, which=NPM)
    assert {"script:package.json:lint:web@.", "script:package.json:lint:api@."} <= runnable(checks)


def test_a_javascript_lint_script_does_not_hide_the_python_default(tmp_path):
    root = _js_repo(tmp_path)
    tools = (tool("eslint", "lint", config=("eslint.config.js",)),)
    sc = scope(".", (JS, PY), tools=tools, scripts=(script("lint", runs=("eslint:lint",)),))
    checks = plan(root, sc, which=NPM)
    assert "ruff-check@." in checks and checks["ruff-check@."].precedence == lp.PRECEDENCE_BOOTSTRAP
    needs = lp.bootstrap_needs(discovery(root, sc), config(), tool_env(root, which=NPM))
    assert {(n.ecosystem, n.role) for n in needs} >= {("python", "lint"), ("python", "format")}


# ---------------------------------------------------------------- F4: nothing runs that can write


def test_script_naming_a_writing_formatter_without_read_only_form_is_never_run(tmp_path):
    root = make_repo(tmp_path)
    sc = scope(".", (TS,), scripts=(script("format:check", ("format",), unsafe=("oxfmt",)),))
    check = plan(root, sc, which=NPM)["script:package.json:format:check@."]
    assert state(check) == ("not-applicable", "writes-unknown")
    assert check.argv is None and "oxfmt" in check.unavailable.detail


def test_default_formatter_forms_never_write(tmp_path):
    root = make_repo(tmp_path)
    for name in ("oxfmt", "prettier"):
        install(root, f"node_modules/.bin/{name}")
    install(root, ".venv/bin/ruff")
    sc = scope(".", (JS, PY))
    checks = plan(root, sc, which={"cargo": "/c", "gofmt": "/g", "shfmt": "/s"})
    check_forms = {c.id: c.argv[1:] for c in checks.values() if c.argv and c.category == "format"}
    assert check_forms["oxfmt@."] == ("--check",)
    assert check_forms["ruff-format@."] == ("format", "--check")


# ---------------------------------------------------------------- mixed Bun + uv scope: tools resolve per ecosystem


def _mixed_scope(tools):
    return scope(".", (TS, PY), manager=pm("bun"), other_managers=(pm("uv", "python"),),
                 typescript=ts_info(), tools=tools)


def test_js_tool_in_a_mixed_scope_never_goes_through_the_python_manager(tmp_path):
    root = make_repo(tmp_path)
    sc = _mixed_scope((tool("eslint", "lint", config=("eslint.config.js",)),))
    checks = plan(root, sc, which={"uv": "/bin/uv", "bun": "/bin/bun"})
    for check_id in ("tsc@.", "eslint@."):
        assert state(checks[check_id])[0] == "missing-tool" and checks[check_id].argv is None
    assert "bun install" in checks["eslint@."].unavailable.detail
    assert not [c for c in checks.values() if c.argv and "uv" in c.argv]
    assert lp.resolve_tool("tsc", sc, discovery(root, sc), tool_env(root, which={"uv": "/bin/uv"})).kind == "missing"


def test_local_node_modules_bin_wins_for_js_tools_in_a_mixed_scope(tmp_path):
    root = make_repo(tmp_path)
    tsc = install(root, "node_modules/.bin/tsc")
    checks = plan(root, _mixed_scope(()), which={"uv": "/bin/uv"})
    assert checks["tsc@."].argv[0] == str(tsc)


def test_python_tool_in_a_mixed_scope_uses_the_python_env_not_uv_run(tmp_path):
    root = make_repo(tmp_path)
    sc = _mixed_scope((tool("ruff", "lint", config=("ruff.toml",)),))
    venv_ruff = install(root, ".venv/bin/ruff")
    res = lp.resolve_tool("ruff", sc, discovery(root, sc), tool_env(root, which={"uv": "/bin/uv"}))
    assert (res.kind, res.argv_prefix) == ("local-bin", (str(venv_ruff),))


def test_venv_is_never_a_source_for_js_tools(tmp_path):
    root = make_repo(tmp_path)
    install(root, ".venv/bin/tsc")
    sc = _mixed_scope(())
    assert lp.resolve_tool("tsc", sc, discovery(root, sc), tool_env(root)).kind == "missing"


# ---------------------------------------------------------------- heavy application env: linting never syncs it


def test_fresh_uv_project_never_plans_uv_run_and_uses_the_pinned_cache(tmp_path):
    root = make_repo(tmp_path)
    environ = _cache_env(tmp_path)
    ruff = install(environment_dir(environ, "pypi", "ruff", lp.PINS["ruff"].version), "bin/ruff")
    sc = scope(".", (PY,), manager=pm("uv", "python"))  # pyproject depends on e.g. torch==9999: never installed
    checks = plan(root, sc, which={"uv": "/bin/uv"}, environ=environ)
    assert checks["ruff-check@."].argv == (str(ruff), "check")
    assert not [c for c in checks.values() if c.argv and "uv" in c.argv]


def test_uv_project_without_a_ready_ruff_is_missing_not_synced(tmp_path):
    root = make_repo(tmp_path)
    sc = scope(".", (PY,), manager=pm("uv", "python"), tools=(tool("ruff", "lint", config=("pyproject.toml",)),))
    check = plan(root, sc, which={"uv": "/bin/uv"})["ruff-check@."]
    assert state(check) == ("missing-tool", "not-bootstrapped") and check.argv is None
    assert "runtime environment" in check.unavailable.detail


def test_declared_ruff_uses_the_version_the_project_locks(tmp_path):
    root = make_repo(tmp_path)
    environ = _cache_env(tmp_path)
    (root / "uv.lock").write_text('[[package]]\nname = "ruff"\nversion = "0.9.9"\n', encoding="utf-8")
    locked = install(environment_dir(environ, "pypi", "ruff", "0.9.9"), "bin/ruff")
    sc = scope(".", (PY,), manager=pm("uv", "python"), tools=(tool("ruff", "lint", config=("ruff.toml",)),))
    res = lp.resolve_tool("ruff", sc, discovery(root, sc), tool_env(root, environ=environ))
    assert (res.kind, res.argv_prefix) == ("rig-cache", (str(locked),))


# ---------------------------------------------------------------- F7: orphans and the Rig-owned tool cache


def _cache_env(tmp_path):
    return {"RIG_LINT_TOOL_CACHE": str(tmp_path / "cache")}


def test_cached_ruff_resolves_for_python_without_a_manifest(tmp_path):
    root = make_repo(tmp_path)
    environ = _cache_env(tmp_path)
    ruff = install(environment_dir(environ, "pypi", "ruff", lp.PINS["ruff"].version), "bin/ruff")
    sc = scope(".", (PY,))
    env = tool_env(root, environ=environ)
    res = lp.resolve_tool("ruff", sc, discovery(root, sc), env)
    assert (res.kind, res.argv_prefix) == ("rig-cache", (str(ruff),))
    checks = plan(root, sc, environ=environ)
    assert checks["ruff-check@."].argv == (str(ruff), "check") and checks["ruff-format@."].unavailable is None


def test_cache_version_follows_the_configured_override(tmp_path):
    root = make_repo(tmp_path)
    environ = _cache_env(tmp_path)
    install(environment_dir(environ, "pypi", "ruff", "0.1.0"), "bin/ruff")
    sc = scope(".", (PY,))
    env = tool_env(root, environ=environ)
    assert lp.resolve_tool("ruff", sc, discovery(root, sc), env).kind == "missing"
    pins = {"ruff": lp.PinnedPackage("ruff", "0.1.0", "pypi")}
    assert lp.resolve_tool("ruff", sc, discovery(root, sc), env, pins=pins).kind == "rig-cache"


def test_declared_but_uninstalled_js_tool_is_not_masked_by_the_cache(tmp_path):
    root = make_repo(tmp_path)
    environ = _cache_env(tmp_path)
    install(environment_dir(environ, "npm", "oxlint", lp.PINS["oxlint"].version), "node_modules/.bin/oxlint")
    sc = scope(".", (JS,), tools=(tool("oxlint", "lint"),), manager=pm("npm"))
    res = lp.resolve_tool("oxlint", sc, discovery(root, sc), tool_env(root, environ=environ))
    assert res.kind == "missing"  # `npm install` is the remedy for a declared tool


def test_cached_oxlint_resolves_for_manifestless_javascript(tmp_path):
    root = make_repo(tmp_path)
    environ = _cache_env(tmp_path)
    oxlint = install(environment_dir(environ, "npm", "oxlint", lp.PINS["oxlint"].version), "node_modules/.bin/oxlint")
    sc = scope(".", (JS,))
    res = lp.resolve_tool("oxlint", sc, discovery(root, sc), tool_env(root, environ=environ))
    assert (res.kind, res.path) == ("rig-cache", str(oxlint))


def test_orphan_python_in_a_javascript_repo_gets_the_ruff_default(tmp_path):
    root = make_repo(tmp_path)
    sc = scope(".", (JS, PY), manager=pm("npm"))
    checks = plan(root, sc, which=NPM)
    assert state(checks["ruff-check@."]) == ("missing-tool", "not-bootstrapped")
    assert "lint:python:." in checks["ruff-check@."].covers and "format:python:." in checks["ruff-format@."].covers
    needs = {(n.ecosystem, n.tool) for n in lp.bootstrap_needs(discovery(root, sc), config(), tool_env(root, which=NPM))}
    assert ("python", "ruff") in needs and ("js", "oxlint") in needs


# ---------------------------------------------------------------- system tools and Rust needs


def test_missing_shell_tools_become_system_needs_unless_present_or_windows(tmp_path):
    root = make_repo(tmp_path)
    sc = scope(".", (), files={"shell": ("a.sh",), "github-workflows": (".github/workflows/ci.yml",)})
    needs = lp.bootstrap_needs(discovery(root, sc), config(), tool_env(root))
    assert {(n.ecosystem, n.tool) for n in needs} == {("system", "shellcheck"), ("system", "shfmt"), ("system", "actionlint")}
    present = {"shellcheck": "/s/shellcheck", "shfmt": "/s/shfmt", "actionlint": "/s/actionlint"}
    assert lp.bootstrap_needs(discovery(root, sc), config(), tool_env(root, which=present)) == ()
    assert lp.bootstrap_needs(discovery(root, sc), config(), tool_env(root, system="windows")) == ()


def test_rustup_need_only_when_rustup_exists_and_go_never_needs_anything(tmp_path):
    root = make_repo(tmp_path)
    rust = scope(".", ("rust",))
    assert lp.bootstrap_needs(discovery(root, rust), config(), tool_env(root)) == ()
    needs = lp.bootstrap_needs(discovery(root, rust), config(), tool_env(root, which={"rustup": "/r/rustup"}))
    assert {(n.ecosystem, n.tool) for n in needs} == {("rustup", "cargo-clippy"), ("rustup", "cargo-fmt")}
    go = scope("svc", ("go",), files={"go": ("svc/m.go",)})
    assert lp.bootstrap_needs(discovery(root, go), config(), tool_env(root)) == ()


# ---------------------------------------------------------------- F8: coverage is never a clean pass on gaps


def _disc(root, scopes, sources, unknown=()):
    base = discovery(root, *scopes)
    return Discovery(root, base.scopes, tuple(unknown), (), (), 0, True, sources)


def test_coverage_counts_checked_and_uncovered_files(tmp_path):
    root = _js_repo(tmp_path, ("oxlint", "oxfmt"))
    sc = scope(".", (TS,))
    disc = _disc(root, (sc,), {"typescript": ("a.ts", "src/b.ts"), "python": ("tool.py",)},
                 unknown=(UnknownLanguage("kotlin", 3, ("A.kt",)),))
    checks = lp.plan_checks(disc, config(), tool_env(root))
    result = lp.coverage(disc, checks)
    assert result.files_checked == 2 and result.files_uncovered == 4 and not result.complete
    assert result.uncovered == (("kotlin", 3), ("python", 1))


def test_missing_tool_is_not_coverage(tmp_path):
    root = make_repo(tmp_path)
    sc = scope(".", (TS,))
    disc = _disc(root, (sc,), {"typescript": ("a.ts",)})
    checks = lp.plan_checks(disc, config(), tool_env(root))
    assert state(next(c for c in checks if c.id == "oxlint@.")) == ("missing-tool", "not-bootstrapped")
    assert lp.coverage(disc, checks).files_checked == 0
    executed = {c.id for c in checks}
    assert lp.coverage(disc, checks, executed).files_checked == 1  # the caller decides what really ran


def test_orphan_go_without_module_is_uncovered_once(tmp_path):
    root = make_repo(tmp_path)
    disc = _disc(root, (), {"go": ("x/main.go",)}, unknown=(UnknownLanguage("go", 1, ("x/main.go",)),))
    result = lp.coverage(disc, lp.plan_checks(disc, config(), tool_env(root)))
    assert (result.files_checked, result.files_uncovered, result.uncovered) == (0, 1, (("go", 1),))


def test_empty_repository_is_complete(tmp_path):
    root = make_repo(tmp_path)
    disc = _disc(root, (), {})
    assert lp.coverage(disc, ()).complete


def test_template_frameworks_without_eslint_stay_partial(tmp_path):
    root = make_repo(tmp_path)
    vue = scope("web", (TS,), frameworks=("vue",))
    react = scope("app", (TS,), frameworks=("react",))
    linted = scope("docs", (TS,), frameworks=("svelte",), tools=(tool("eslint", "lint", config=("eslint.config.js",)),))
    disc = _disc(root, (vue, react, linted), {})
    result = lp.coverage(disc, ())
    assert result.template_uncovered == (("web", "vue"),)
    assert result.complete and not result.fully_covered


def test_provisioned_shell_tools_resolve_from_the_pinned_cache(tmp_path):
    root = make_repo(tmp_path)
    environ = _cache_env(tmp_path)
    shellcheck = install(environment_dir(environ, "bin", "shellcheck", lp.SYSTEM_TOOL_VERSIONS["shellcheck"]), "shellcheck")
    sc = scope(".", (), files={"shell": ("a.sh",)})
    check = plan(root, sc, environ=environ)["shellcheck@."]
    assert check.argv == (str(shellcheck), "--", "a.sh") and check.resolution.kind == "rig-cache"
    assert lp.bootstrap_needs(discovery(root, sc), config(), tool_env(root, environ=environ)) != ()  # shfmt still missing


# ---------------------------------------------------------------- Oxlint without a config: Rig base severities


def test_oxlint_without_any_config_file_denies_the_base_categories(tmp_path):
    root = make_repo(tmp_path)
    oxlint = install(root, "node_modules/.bin/oxlint")
    for sc in (scope(".", (JS,)), scope(".", (JS,), tools=(tool("oxlint", "lint"),))):  # default and declared-only
        check = plan(root, sc)["oxlint@."]
        base = ("-D", "correctness", "-D", "suspicious", "-D", "perf")
        assert check.argv == (str(oxlint), *base) and check.fix_argv == (str(oxlint), "--fix", *base)
        assert not [a for a in check.argv if a in ("--deny-warnings", "--type-aware", "--type-check")]


def test_an_existing_oxlint_config_is_authoritative_and_gets_no_flags(tmp_path):
    root = make_repo(tmp_path)
    oxlint = install(root, "node_modules/.bin/oxlint")
    configured = scope(".", (JS,), tools=(tool("oxlint", "lint", config=(".oxlintrc.json",)),))
    assert plan(root, configured)["oxlint@."].argv == (str(oxlint),)
    member = scope("pkg", (JS,))
    assert plan(root, configured, member)["oxlint@."].argv == (str(oxlint),)  # ancestor config governs the chain


def test_real_oxlint_exits_nonzero_on_correctness_findings_with_the_planned_argv(tmp_path):
    import subprocess

    from tests.lint_real_helpers import pinned_tool

    real = str(pinned_tool("oxlint", "RIG_TEST_OXLINT_BIN"))
    root = make_repo(tmp_path)
    (root / "a.js").write_text("debugger;\n", encoding="utf-8")
    bin_dir = root / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "oxlint").symlink_to(real)
    argv = plan(root, scope(".", (JS,)))["oxlint@."].argv
    done = subprocess.run(argv, cwd=root, capture_output=True, text=True, check=False)
    assert done.returncode == 1 and "no-debugger" in done.stdout, done.stdout + done.stderr
    bare = subprocess.run([real, "a.js"], cwd=root, capture_output=True, text=True, check=False)
    assert bare.returncode == 0  # why the flags exist: Oxlint's defaults only warn


# ---------------------------------------------------------------- Oxlint runs with the provisioned policy file (-c)


def _provisioned(monkeypatch, tmp_path, *, present=True):
    from riglib import lint_policy_cache

    policy = tmp_path / "cache" / "policy" / "oxlint.config.ts"
    policy.parent.mkdir(parents=True)
    policy.write_text("export default {}\n", encoding="utf-8")
    seen = []

    def fake(scope, discovery, cfg, environ):
        seen.append((scope.rel, dict(environ)))
        return policy if present else None

    monkeypatch.setattr(lint_policy_cache, "cached_policy_file", fake)
    return policy, seen


def test_oxlint_uses_the_provisioned_policy_file_instead_of_the_base_flags(tmp_path, monkeypatch):
    root = make_repo(tmp_path)
    oxlint = install(root, "node_modules/.bin/oxlint")
    policy, seen = _provisioned(monkeypatch, tmp_path)
    environ = {"RIG_LINT_TOOL_CACHE": str(tmp_path / "cache")}
    check = plan(root, scope(".", (JS,)), environ=environ)["oxlint@."]
    assert check.argv == (str(oxlint), "-c", str(policy))
    assert check.fix_argv == (str(oxlint), "--fix", "-c", str(policy))
    assert "-D" not in check.argv and seen and seen[0][1] == environ  # the tool env's environ reaches the cache lookup


def test_oxlint_falls_back_to_the_base_flags_when_the_policy_is_not_provisioned(tmp_path, monkeypatch):
    root = make_repo(tmp_path)
    oxlint = install(root, "node_modules/.bin/oxlint")
    _provisioned(monkeypatch, tmp_path, present=False)
    check = plan(root, scope(".", (JS,)))["oxlint@."]
    assert check.argv == (str(oxlint), "-D", "correctness", "-D", "suspicious", "-D", "perf")


def test_a_governing_config_never_gets_the_cached_policy(tmp_path, monkeypatch):
    root = make_repo(tmp_path)
    oxlint = install(root, "node_modules/.bin/oxlint")
    _, seen = _provisioned(monkeypatch, tmp_path)
    configured = scope(".", (JS,), tools=(tool("oxlint", "lint", config=(".oxlintrc.json",)),))
    assert plan(root, configured)["oxlint@."].argv == (str(oxlint),)
    assert seen == []  # `-c` would disable nested config lookup, so the lookup is not even made
