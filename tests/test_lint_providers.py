"""Provider registry for `rig lint run`: applicability, argv exactness, authority of configured tools, tool resolution.

Facts are built in memory (the discovery module has its own tests); binaries live in a real tmp repo so tool
resolution runs against the real filesystem. Nothing here executes a tool or touches the network or HOME.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from riglib import lint_providers as lp
from riglib import lint_providers_registry as registry
from riglib.lint_policy import anti_slop_required, resolve_rule_severities
from riglib.lint_providers_build import build
from tests.lint_providers_helpers import (
    JS,
    PY,
    TS,
    bin_path,
    config,
    discovery,
    install,
    make_repo,
    plan,
    pm,
    scope,
    script,
    state,
    symlink,
    tool,
    tool_env,
    ts_info,
)

FORBIDDEN = ("--type-aware", "--type-check", "--unsafe", "--unsafe-fixes", "--fix-dangerously", "--fix-suggestions")
# Rig base severities for an Oxlint scope with no config file (Oxlint's defaults only warn and exit 0).
BASE = ("-D", "correctness", "-D", "suspicious", "-D", "perf")


# ---------------------------------------------------------------- registry


def test_registry_ids_unique_sorted_and_complete():
    ids = [spec.id for spec in lp.providers()]
    assert ids == sorted(ids) and len(ids) == len(set(ids))
    assert lp.PROVIDER_IDS == frozenset(ids)
    expected = {"oxlint", "oxfmt", "eslint", "biome-lint", "biome-format", "prettier", "tsc", "ruff-check",
                "ruff-format", "flake8", "pylint", "black", "mypy", "pyright", "shellcheck", "shfmt", "actionlint",
                "cargo-clippy", "cargo-fmt", "go-vet", "gofmt", "rig-check", "script"}
    assert expected <= set(ids)
    assert lp.provider("oxlint").tool == "oxlint"
    with pytest.raises(KeyError):
        lp.provider("nope")


def test_duplicate_registration_fails(monkeypatch):
    monkeypatch.setattr(registry, "_REGISTRY", dict(registry._REGISTRY))
    monkeypatch.setattr(registry, "PROVIDER_IDS", registry.PROVIDER_IDS)
    with pytest.raises(ValueError):
        lp.register(lp.provider("oxlint"))


def test_pins_are_exact_versions():
    assert set(lp.PINS) == {"oxlint", "oxfmt", "oxlint-tsgolint", "@oxlint/plugins", "ruff"}
    for key, pin in lp.PINS.items():
        assert pin.name == key and pin.version.count(".") >= 2 and pin.version[0].isdigit()
    assert lp.PINS["ruff"].ecosystem == "pypi" and lp.PINS["oxlint"].ecosystem == "npm"


def test_plan_is_deterministic_and_sorted(tmp_path):
    root = make_repo(tmp_path)
    scopes = (scope("web", (TS,), files={"shell": ("web/a.sh",)}), scope(".", (JS,), tools=(tool("eslint", "lint", config=("eslint.config.js",)),)))
    first = lp.plan_checks(discovery(root, *scopes), config(), tool_env(root))
    second = lp.plan_checks(discovery(root, *reversed(scopes)), config(), tool_env(root))
    assert first == second
    keys = [lp.check_sort_key(c) for c in first]
    assert keys == sorted(keys)


# ---------------------------------------------------------------- argv tables


def _js_cases(root):
    b = lambda name: bin_path(root, name)
    return [
        ("oxlint@.", tool("oxlint", "lint"), (b("oxlint"), *BASE), (b("oxlint"), "--fix", *BASE)),
        ("oxfmt@.", tool("oxfmt", "format"), (b("oxfmt"), "--check"), (b("oxfmt"),)),
        ("eslint@.", tool("eslint", "lint", config=("eslint.config.js",)), (b("eslint"), "."), (b("eslint"), "--fix", ".")),
        ("biome-lint@.", tool("biome", "lint"), (b("biome"), "lint", "."), (b("biome"), "lint", "--write", ".")),
        ("biome-format@.", tool("biome", "format"), (b("biome"), "format", "."), (b("biome"), "format", "--write", ".")),
        ("prettier@.", tool("prettier", "format"), (b("prettier"), "--check", "."), (b("prettier"), "--write", ".")),
    ]


@pytest.mark.parametrize("index", range(6))
def test_js_tool_argv_tables(tmp_path, index):
    root = make_repo(tmp_path)
    check_id, cfg_tool, argv, fix = _js_cases(root)[index]
    for name in ("oxlint", "oxfmt", "eslint", "biome", "prettier"):
        install(root, f"node_modules/.bin/{name}")
    checks = plan(root, scope(".", (JS,), tools=(cfg_tool,)))
    check = checks[check_id]
    assert (check.argv, check.fix_argv, check.cwd, check.unavailable) == (argv, fix, ".", None)
    assert check.resolution is not None and check.resolution.kind == "local-bin"
    assert check.resolution.path == f"node_modules/.bin/{cfg_tool.tool}"


def test_tsc_argv_uses_scope_tsconfig_and_has_no_fix(tmp_path):
    root = make_repo(tmp_path)
    install(root, "node_modules/.bin/tsc")
    checks = plan(root, scope(".", (TS,), typescript=ts_info()))
    check = checks["tsc@."]
    assert check.argv == (bin_path(root, "tsc"), "--noEmit", "-p", "tsconfig.json")
    assert check.fix_argv is None and check.category == "typecheck"


def test_tsc_needs_declared_typescript_and_tsconfig(tmp_path):
    root = make_repo(tmp_path)
    assert "tsc@." not in plan(root, scope(".", (TS,), typescript=ts_info(spec=None)))
    assert "tsc@." not in plan(root, scope(".", (TS,), typescript=ts_info(tsconfigs=())))
    assert "tsc@." not in plan(root, scope(".", (TS,)))


def test_nested_scope_resolves_from_root_node_modules_and_uses_scope_cwd(tmp_path):
    root = make_repo(tmp_path)
    install(root, "node_modules/.bin/tsc")
    nested = scope("web/app", (TS,), typescript=ts_info(tsconfigs=("web/app/tsconfig.json",)))
    checks = plan(root, scope(".", (JS,)), nested)
    check = checks["tsc@web/app"]
    assert check.cwd == "web/app" and check.argv == (bin_path(root, "tsc"), "--noEmit", "-p", "tsconfig.json")


def test_python_tool_argv_tables(tmp_path):
    root = make_repo(tmp_path)
    for name in ("ruff", "flake8", "pylint", "black", "mypy", "pyright"):
        install(root, f".venv/bin/{name}")
    (root / "mypkg").mkdir()
    (root / "mypkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "-weird").mkdir()
    (root / "-weird" / "__init__.py").write_text("", encoding="utf-8")
    tools = tuple(tool(n, r) for n, r in (("ruff", "lint"), ("flake8", "lint"), ("pylint", "lint"), ("black", "format"),
                                           ("mypy", "typecheck"), ("pyright", "typecheck")))
    checks = plan(root, scope(".", (PY,), tools=tools))
    v = lambda name: str(root / ".venv" / "bin" / name)
    assert checks["ruff-check@."].argv == (v("ruff"), "check")
    assert checks["ruff-check@."].fix_argv == (v("ruff"), "check", "--fix")
    assert checks["flake8@."].argv == (v("flake8"),) and checks["flake8@."].fix_argv is None
    assert checks["pylint@."].argv == (v("pylint"), "./-weird", "mypkg") and checks["pylint@."].fix_argv is None
    assert checks["black@."].argv == (v("black"), "--check", ".") and checks["black@."].fix_argv == (v("black"), ".")
    assert checks["mypy@."].argv == (v("mypy"), ".") and checks["mypy@."].fix_argv is None
    assert checks["pyright@."].argv == (v("pyright"),) and checks["pyright@."].fix_argv is None
    # complementary tools stay separate: black and Ruff's formatter both run
    assert checks["ruff-format@."].argv == (v("ruff"), "format", "--check")


def test_ruff_format_argv_when_ruff_is_the_formatter(tmp_path):
    root = make_repo(tmp_path)
    install(root, ".venv/bin/ruff")
    checks = plan(root, scope(".", (PY,), tools=(tool("ruff", "lint"),)))
    ruff = str(root / ".venv" / "bin" / "ruff")
    assert checks["ruff-format@."].argv == (ruff, "format", "--check")
    assert checks["ruff-format@."].fix_argv == (ruff, "format")


def test_pylint_without_package_dirs_is_not_applicable(tmp_path):
    root = make_repo(tmp_path)
    install(root, ".venv/bin/pylint")
    checks = plan(root, scope(".", (PY,), tools=(tool("pylint", "lint"),)))
    assert state(checks["pylint@."]) == ("not-applicable", "no-matching-files")


def test_shell_checks_use_scope_relative_paths_spaces_and_dash_prefix(tmp_path):
    root = make_repo(tmp_path)
    which = {"shellcheck": "/usr/bin/shellcheck", "shfmt": "/usr/bin/shfmt"}
    files = {"shell": ("pkg/my script.sh", "pkg/-x.sh", "pkg/d/b;$(x).sh")}
    checks = plan(root, scope("pkg", (), files=files), which=which)
    sc = checks["shellcheck@pkg"]
    expected = ("--", "my script.sh", "./-x.sh", "d/b;$(x).sh")
    assert sc.argv == ("/usr/bin/shellcheck", *expected) and sc.fix_argv is None and sc.cwd == "pkg"
    sf = checks["shfmt@pkg"]
    assert sf.argv == ("/usr/bin/shfmt", "-d", *expected)
    assert sf.fix_argv == ("/usr/bin/shfmt", "-w", *expected)


def test_many_shell_files_are_chunked_into_numbered_checks(tmp_path):
    root = make_repo(tmp_path)
    names = tuple(f"scripts/{'x' * 90}{i:04d}.sh" for i in range(2000))
    checks = plan(root, scope(".", (), files={"shell": names}), which={"shellcheck": "/s/shellcheck"})
    ids = sorted(i for i in checks if i.startswith("shellcheck@"))
    assert ids[0] == "shellcheck@." and ids[1] == "shellcheck@.#2" and len(ids) > 2
    seen = [p for i in ids for p in checks[i].argv[2:]]
    assert seen == list(names)
    assert all(len(" ".join(checks[i].argv)) < 100_500 for i in ids)


def test_actionlint_argv(tmp_path):
    root = make_repo(tmp_path)
    files = {"github-workflows": (".github/workflows/ci.yml", ".github/workflows/a b.yaml")}
    checks = plan(root, scope(".", (), files=files), which={"actionlint": "/bin/actionlint"})
    assert checks["actionlint@."].argv == (
        "/bin/actionlint", "-no-color", "--", ".github/workflows/ci.yml", ".github/workflows/a b.yaml")
    assert checks["actionlint@."].fix_argv is None


def test_cargo_argv_with_and_without_lockfile(tmp_path):
    root = make_repo(tmp_path)
    which = {"cargo": "/bin/cargo"}
    checks = plan(root, scope(".", ("rust",)), which=which)
    assert checks["cargo-clippy@."].argv == (
        "/bin/cargo", "clippy", "--workspace", "--all-targets", "--", "-D", "warnings")
    (root / "Cargo.lock").write_text("", encoding="utf-8")
    checks = plan(root, scope(".", ("rust",)), which=which)
    assert checks["cargo-clippy@."].argv == (
        "/bin/cargo", "clippy", "--workspace", "--all-targets", "--locked", "--", "-D", "warnings")
    assert checks["cargo-fmt@."].argv == ("/bin/cargo", "fmt", "--all", "--", "--check")
    assert checks["cargo-fmt@."].fix_argv == ("/bin/cargo", "fmt", "--all")
    assert checks["cargo-clippy@."].fix_argv is None


def test_go_argv_and_stdout_empty_rule(tmp_path):
    root = make_repo(tmp_path)
    which = {"go": "/bin/go", "gofmt": "/bin/gofmt"}
    checks = plan(root, scope("svc", ("go",), files={"go": ("svc/main.go", "svc/-b.go")}), which=which)
    assert checks["go-vet@svc"].argv == ("/bin/go", "vet", "./...") and checks["go-vet@svc"].result_rule == "exit-zero"
    fmt = checks["gofmt@svc"]
    assert fmt.argv == ("/bin/gofmt", "-l", "--", "main.go", "./-b.go") and fmt.result_rule == "stdout-empty"
    assert fmt.fix_argv == ("/bin/gofmt", "-w", "--", "main.go", "./-b.go")


def test_rig_check_argv_fix_and_config_path(tmp_path):
    root = make_repo(tmp_path)
    check = plan(root)["rig-check@."]
    launcher = str(Path(lp.__file__).resolve().with_name("lint_check_launcher.py"))
    head = (sys.executable, launcher, "lint", "check")
    assert check.argv == (*head, "--json", "-C", str(root)) and check.category == "limits"
    assert check.fix_argv == (*head, "--fix", "--json", "-C", str(root))
    disc = discovery(root)
    with_cfg = lp.plan_checks(disc, config(), tool_env(root), config_path="/x/rig.yaml")
    assert with_cfg[0].argv[-2:] == ("--config", "/x/rig.yaml")


def test_rig_check_states(tmp_path):
    root = make_repo(tmp_path)
    assert state(plan(root, cfg=config(lint_checks_enabled=False))["rig-check@."]) == (
        "not-applicable", "disabled-by-config")
    unsupported = plan(root, machine="riscv64")["rig-check@."]
    assert state(unsupported) == ("unsupported-platform", "platform-unsupported") and unsupported.argv is None


def test_no_provider_emits_forbidden_flags(tmp_path):
    root = make_repo(tmp_path)
    for name in ("oxlint", "oxfmt", "eslint", "biome", "prettier", "tsc"):
        install(root, f"node_modules/.bin/{name}")
    tools = (tool("oxlint", "lint", options={"type_check": "true"}), tool("oxfmt", "format"), tool("biome", "lint"),
             tool("prettier", "format"), tool("eslint", "lint", config=("e.js",)))
    checks = plan(root, scope(".", (TS,), tools=tools, typescript=ts_info(major=7)))
    argvs = [argv for c in checks.values() for argv in (c.argv or (), c.fix_argv or ())]
    flags = [arg for argv in argvs for arg in argv if arg.startswith(FORBIDDEN)]
    assert flags == []


# ---------------------------------------------------------------- configured tools are authoritative


def test_configured_eslint_blocks_oxlint_default_but_not_oxfmt(tmp_path):
    root = make_repo(tmp_path)
    install(root, "node_modules/.bin/eslint")
    sc = scope(".", (TS,), tools=(tool("eslint", "lint", config=("eslint.config.js",)),))
    checks = plan(root, sc)
    assert "oxlint@." not in checks and "eslint@." in checks
    needs = lp.bootstrap_needs(discovery(root, sc), config(), tool_env(root))
    assert [(n.role, n.tool, n.provider) for n in needs] == [("format", "oxfmt", "oxfmt")]


def test_declared_eslint_without_config_does_not_suppress_the_default_and_stays_visible(tmp_path):
    root = make_repo(tmp_path)
    sc = scope(".", (JS,), tools=(tool("eslint", "lint"),))
    checks = plan(root, sc)
    assert state(checks["eslint@."]) == ("not-applicable", "no-config-file")
    assert state(checks["oxlint@."]) == ("missing-tool", "not-bootstrapped")
    roles = [n.role for n in lp.bootstrap_needs(discovery(root, sc), config(), tool_env(root))]
    assert sorted(roles) == ["format", "lint"]


def test_eslint_invoked_by_a_script_is_configured_even_without_a_config_file(tmp_path):
    root = make_repo(tmp_path)
    sc = scope(".", (JS,), tools=(tool("eslint", "lint", scripts=("lint",)),))
    assert "oxlint@." not in plan(root, sc)
    assert [n.role for n in lp.bootstrap_needs(discovery(root, sc), config(), tool_env(root))] == ["format"]


def test_configured_ruff_means_no_black_and_no_needs(tmp_path):
    root = make_repo(tmp_path)
    sc = scope(".", (PY,), tools=(tool("ruff", "lint"),))
    checks = plan(root, sc)
    assert "black@." not in checks
    assert lp.bootstrap_needs(discovery(root, sc), config(), tool_env(root)) == ()


def test_configured_black_only_needs_ruff_lint(tmp_path):
    root = make_repo(tmp_path)
    sc = scope(".", (PY,), tools=(tool("black", "format"),))
    needs = lp.bootstrap_needs(discovery(root, sc), config(), tool_env(root))
    assert [(n.role, n.provider) for n in needs] == [("lint", "ruff-check")]
    assert "ruff-format@." not in plan(root, sc)


def test_foreign_tools_are_unsupported_and_never_replaced(tmp_path):
    root = make_repo(tmp_path)
    sc = scope(".", (JS,), tools=(tool("xo", "lint"), tool("dprint", "format")))
    other = scope("py", (PY,), tools=(tool("ruff", "lint"), tool("tslint", "lint")))
    disc = discovery(root, sc, other)
    assert lp.unsupported_tools(disc) == ((".", "dprint"), (".", "xo"), ("py", "tslint"))
    assert lp.bootstrap_needs(disc, config(), tool_env(root)) == ()
    assert "oxlint@." not in plan(root, sc) and "oxfmt@." not in plan(root, sc)


def test_ancestor_configuration_covers_descendants(tmp_path):
    root = make_repo(tmp_path)
    top = scope(".", (JS,), tools=(tool("eslint", "lint", config=("eslint.config.js",)), tool("prettier", "format")))
    member = scope("packages/a", (TS,))
    checks = plan(root, top, member)
    assert not [i for i in checks if i.endswith("@packages/a") and i.split("@")[0] in {"oxlint", "oxfmt", "eslint"}]
    assert lp.bootstrap_needs(discovery(root, top, member), config(), tool_env(root)) == ()


def test_default_lands_on_topmost_scope_only(tmp_path):
    root = make_repo(tmp_path)
    scopes = (scope(".", (JS,)), scope("packages/a", (TS,)), scope("packages/b", (TS,)))
    needs = lp.bootstrap_needs(discovery(root, *scopes), config(), tool_env(root))
    assert {(n.scope, n.role) for n in needs} == {(".", "lint"), (".", "format")}
    checks = plan(root, *scopes)
    assert {i for i in checks if i.startswith(("oxlint", "oxfmt"))} == {"oxlint@.", "oxfmt@."}


def test_member_configuration_withholds_root_default_and_unconfigured_siblings_get_their_own(tmp_path):
    root = make_repo(tmp_path)
    scopes = (
        scope(".", (JS,)),
        scope("a", (TS,), tools=(tool("eslint", "lint", config=("eslint.config.js",)),)),
        scope("b", (TS,)),
    )
    needs = lp.bootstrap_needs(discovery(root, *scopes), config(), tool_env(root))
    lint_scopes = {n.scope for n in needs if n.role == "lint"}
    assert lint_scopes == {"b"}
    assert {n.scope for n in needs if n.role == "format"} == {"."}


# ---------------------------------------------------------------- bootstrap needs and missing tools


def test_empty_js_project_needs_pinned_oxlint_and_oxfmt(tmp_path):
    root = make_repo(tmp_path)
    needs = lp.bootstrap_needs(discovery(root, scope(".", (JS,), manager=pm("pnpm"))), config(), tool_env(root))
    by_role = {n.role: n for n in needs}
    assert by_role["lint"].tool == "oxlint" and by_role["format"].tool == "oxfmt"
    assert all(n.ecosystem == "js" for n in needs)
    names = {p.name: p.version for n in needs for p in n.packages}
    assert names["oxlint"] == lp.PINS["oxlint"].version and names["oxfmt"] == lp.PINS["oxfmt"].version
    assert "oxlint-tsgolint" not in names


def test_python_project_needs_ruff_for_both_roles(tmp_path):
    root = make_repo(tmp_path)
    needs = lp.bootstrap_needs(discovery(root, scope(".", (PY,))), config(), tool_env(root))
    assert {(n.role, n.provider) for n in needs} == {("lint", "ruff-check"), ("format", "ruff-format")}
    assert all(n.ecosystem == "python" and [p.name for p in n.packages] == ["ruff"] for n in needs)


def test_version_override_applies_to_pins(tmp_path):
    root = make_repo(tmp_path)
    cfg = config(bootstrap=config().bootstrap.__class__(True, True, True, {"ruff": "0.1.0"}))
    needs = lp.bootstrap_needs(discovery(root, scope(".", (PY,))), cfg, tool_env(root))
    assert {p.version for n in needs for p in n.packages} == {"0.1.0"}


@pytest.mark.parametrize(("major", "wanted"), [(7, True), (6, False), (5, False), (None, False)])
def test_tsgolint_only_with_typescript_7(tmp_path, major, wanted):
    root = make_repo(tmp_path)
    ts = ts_info(major=major) if major else None
    needs = lp.bootstrap_needs(discovery(root, scope(".", (TS,), typescript=ts)), config(), tool_env(root))
    names = {p.name for n in needs for p in n.packages}
    assert ("oxlint-tsgolint" in names) is wanted


@pytest.mark.parametrize("enabled", [True, False])
def test_plugins_package_only_when_anti_slop_required(tmp_path, enabled):
    root = make_repo(tmp_path)
    cfg = config(linters_enabled=enabled)
    needs = lp.bootstrap_needs(discovery(root, scope(".", (JS,))), cfg, tool_env(root))
    names = {p.name for n in needs for p in n.packages}
    expected = enabled and anti_slop_required(resolve_rule_severities(cfg.linters_rules))
    assert ("@oxlint/plugins" in names) is expected


def test_declared_but_uninstalled_is_missing_tool_without_new_need(tmp_path):
    root = make_repo(tmp_path)
    sc = scope(".", (JS,), tools=(tool("oxlint", "lint"), tool("oxfmt", "format")))
    checks = plan(root, sc)
    assert state(checks["oxlint@."]) == ("missing-tool", "tool-not-installed")
    assert "install" in checks["oxlint@."].unavailable.detail
    assert checks["oxlint@."].argv is None
    assert lp.bootstrap_needs(discovery(root, sc), config(), tool_env(root)) == ()


def test_default_not_yet_installed_reports_not_bootstrapped(tmp_path):
    root = make_repo(tmp_path)
    checks = plan(root, scope(".", (JS,)))
    assert state(checks["oxlint@."]) == ("missing-tool", "not-bootstrapped")
    assert checks["oxlint@."].precedence == lp.PRECEDENCE_BOOTSTRAP


def test_disabled_bootstrap_hides_default_but_still_reports_needs(tmp_path):
    root = make_repo(tmp_path)
    sc = scope(".", (JS,))
    cfg = config(bootstrap=config().bootstrap.__class__(True, False, True, {}))
    assert state(plan(root, sc, cfg=cfg)["oxlint@."]) == ("not-applicable", "disabled-by-config")
    assert len(lp.bootstrap_needs(discovery(root, sc), cfg, tool_env(root))) == 2


def test_system_tool_missing_reports_install_hint(tmp_path):
    root = make_repo(tmp_path)
    check = plan(root, scope(".", (), files={"shell": ("a.sh",)}))["shellcheck@."]
    assert state(check) == ("missing-tool", "tool-not-installed") and "PATH" in check.unavailable.detail


# ---------------------------------------------------------------- tool resolution


def test_resolution_walks_up_but_never_above_the_repo_root(tmp_path):
    root = make_repo(tmp_path)
    install(tmp_path, "node_modules/.bin/oxlint")  # above the repo: must be ignored
    sc = scope("a/b", (JS,))
    disc = discovery(root, sc)
    res = lp.resolve_tool("oxlint", sc, disc, tool_env(root))
    assert res.kind == "missing"
    install(root, "a/node_modules/.bin/oxlint")
    res = lp.resolve_tool("oxlint", sc, disc, tool_env(root))
    assert (res.kind, res.path) == ("local-bin", "a/node_modules/.bin/oxlint")
    assert res.argv_prefix == (str(root / "a" / "node_modules" / ".bin" / "oxlint"),)


def test_nearest_node_modules_wins(tmp_path):
    root = make_repo(tmp_path)
    install(root, "node_modules/.bin/eslint")
    install(root, "pkg/node_modules/.bin/eslint")
    sc = scope("pkg", (JS,))
    res = lp.resolve_tool("eslint", sc, discovery(root, sc), tool_env(root))
    assert res.path == "pkg/node_modules/.bin/eslint"


def test_venv_bin_is_found_and_windows_uses_scripts_dir(tmp_path):
    root = make_repo(tmp_path)
    install(root, ".venv/bin/ruff")
    sc = scope(".", (PY,))
    assert lp.resolve_tool("ruff", sc, discovery(root, sc), tool_env(root)).path == ".venv/bin/ruff"
    win = make_repo(tmp_path / "w")
    install(win, ".venv/Scripts/ruff.exe")
    res = lp.resolve_tool("ruff", sc, discovery(win, sc), tool_env(win, system="windows"))
    assert res.kind == "local-bin" and res.path == ".venv/Scripts/ruff.exe"


def test_manager_run_for_declared_python_tool_never_syncs_via_uv(tmp_path):
    root = make_repo(tmp_path)
    sc = scope(".", (PY,), manager=pm("uv", "python"), tools=(tool("mypy", "typecheck"),))
    disc = discovery(root, sc)
    # `uv run` syncs the whole project environment (application runtime deps) just to lint: never planned.
    assert lp.resolve_tool("mypy", sc, disc, tool_env(root, which={"uv": "/bin/uv"})).kind == "missing"
    poetry = scope(".", (PY,), manager=pm("poetry", "python"), tools=(tool("mypy", "typecheck"),))
    res = lp.resolve_tool("mypy", poetry, discovery(root, poetry), tool_env(root, which={"poetry": "/bin/poetry"}))
    assert (res.kind, res.argv_prefix) == ("manager-run", ("poetry", "run", "mypy"))
    pdm = scope(".", (PY,), manager=pm("pdm", "python"), tools=(tool("mypy", "typecheck"),))
    res = lp.resolve_tool("mypy", pdm, discovery(root, pdm), tool_env(root, which={"pdm": "/bin/pdm"}))
    assert res.argv_prefix == ("pdm", "run", "mypy")
    # manager missing from PATH or tool undeclared: missing, never a guess
    assert lp.resolve_tool("mypy", poetry, discovery(root, poetry), tool_env(root)).kind == "missing"
    undeclared = scope(".", (PY,), manager=pm("poetry", "python"))
    assert lp.resolve_tool("mypy", undeclared, discovery(root, undeclared), tool_env(root, which={"poetry": "/u"})).kind == "missing"


def test_path_is_for_system_tools_only(tmp_path):
    root = make_repo(tmp_path)
    sc = scope(".", (JS,))
    disc = discovery(root, sc)
    env = tool_env(root, which={"oxlint": "/usr/bin/oxlint", "ruff": "/usr/bin/ruff", "eslint": "/usr/bin/eslint",
                                "prettier": "/p", "biome": "/b", "oxfmt": "/o", "shellcheck": "/usr/bin/shellcheck",
                                "pnpm": "/usr/bin/pnpm"})
    for name in ("oxlint", "ruff", "eslint", "prettier", "biome", "oxfmt"):
        assert lp.resolve_tool(name, sc, disc, env).kind == "missing", name
    assert lp.resolve_tool("shellcheck", sc, disc, env).kind == "path"
    assert lp.resolve_tool("pnpm", sc, disc, env).argv_prefix == ("/usr/bin/pnpm",)


def test_repo_local_manager_binary_never_stands_in_for_path_manager(tmp_path):
    root = make_repo(tmp_path)
    install(root, "node_modules/.bin/pnpm")
    sc = scope(".", (JS,))
    assert lp.resolve_tool("pnpm", sc, discovery(root, sc), tool_env(root)).kind == "missing"


def test_bin_entry_resolving_outside_the_repo_is_accepted_and_noted(tmp_path):
    root = make_repo(tmp_path)
    outside = install(tmp_path, "store/oxlint/bin/oxlint")
    symlink(outside, root / "node_modules" / ".bin" / "oxlint")
    sc = scope(".", (JS,), tools=(tool("oxlint", "lint"),))
    res = lp.resolve_tool("oxlint", sc, discovery(root, sc), tool_env(root))
    assert (res.kind, res.path) == ("local-bin", "node_modules/.bin/oxlint")
    assert res.detail.startswith("resolved_outside_repo: ") and str(outside) in res.detail
    check = plan(root, sc)["oxlint@."]
    assert check.unavailable is None and check.argv[0] == str(root / "node_modules/.bin/oxlint")


def test_symlinked_node_modules_directory_is_an_escape(tmp_path):
    root = make_repo(tmp_path)
    install(tmp_path, "shared/node_modules/.bin/oxlint")
    symlink(tmp_path / "shared" / "node_modules", root / "node_modules")
    sc = scope(".", (JS,), tools=(tool("oxlint", "lint"),))
    assert lp.resolve_tool("oxlint", sc, discovery(root, sc), tool_env(root)).kind == "escaped"
    check = plan(root, sc)["oxlint@."]
    assert state(check) == ("missing-tool", "symlink-escape") and check.argv is None


def test_internal_symlink_is_accepted_and_non_executable_skipped(tmp_path):
    root = make_repo(tmp_path)
    real = install(root, "node_modules/oxlint/bin/oxlint")
    symlink(real, root / "node_modules" / ".bin" / "oxlint")
    sc = scope(".", (JS,))
    assert lp.resolve_tool("oxlint", sc, discovery(root, sc), tool_env(root)).kind == "local-bin"
    plain = root / "node_modules" / ".bin" / "eslint"
    plain.write_text("x", encoding="utf-8")
    plain.chmod(0o644)
    assert lp.resolve_tool("eslint", sc, discovery(root, sc), tool_env(root)).kind == "missing"


# ---------------------------------------------------------------- project scripts


def _script_check(tmp_path, sc, which=None, **kw):
    root = make_repo(tmp_path)
    return plan(root, sc, which=which or {"npm": "/bin/npm", "pnpm": "/bin/pnpm", "yarn": "/bin/yarn",
                                         "bun": "/bin/bun", "make": "/bin/make"}, **kw)


def test_package_json_script_argv_per_manager(tmp_path):
    for manager, exe in (("pnpm", "pnpm"), ("yarn-berry", "yarn"), ("yarn", "yarn"), ("npm", "npm"), ("bun", "bun")):
        sc = scope("app", (TS,), manager=pm(manager), scripts=(script("lint:ci"),))
        check = _script_check(tmp_path / manager, sc)["script:package.json:lint:ci@app"]
        assert check.argv == (f"/bin/{exe}", "run", "lint:ci") and check.cwd == "app"
        assert (check.category, check.provider, check.precedence, check.fix_argv) == ("scripts", "script", 0, None)


def test_script_without_detected_manager_defaults_to_npm(tmp_path):
    check = _script_check(tmp_path, scope(".", (JS,), scripts=(script("lint"),)))["script:package.json:lint@."]
    assert check.argv == ("/bin/npm", "run", "lint")


def test_ambiguous_manager_reports_missing_tool(tmp_path):
    sc = scope(".", (JS,), manager=pm(""), scripts=(script("lint"),))
    check = _script_check(tmp_path, sc)["script:package.json:lint@."]
    assert state(check) == ("missing-tool", "ambiguous-package-manager") and check.argv is None


def test_missing_manager_binary(tmp_path):
    sc = scope(".", (JS,), manager=pm("pnpm"), scripts=(script("lint"),))
    check = _script_check(tmp_path, sc, which={"npm": "/bin/npm"})["script:package.json:lint@."]
    assert state(check) == ("missing-tool", "tool-not-installed")


def test_makefile_target_argv(tmp_path):
    sc = scope("svc", ("go",), scripts=(script("check-types", ("typecheck",), runner="Makefile"),))
    check = _script_check(tmp_path, sc)["script:Makefile:check-types@svc"]
    assert check.argv == ("/bin/make", "--no-print-directory", "-C", ".", "--", "check-types") and check.cwd == "svc"


def test_recursion_guard_skips_every_script(tmp_path):
    sc = scope(".", (JS,), scripts=(script("lint"), script("typecheck", ("typecheck",))))
    checks = _script_check(tmp_path, sc, depth=1)
    for check_id in ("script:package.json:lint@.", "script:package.json:typecheck@."):
        assert state(checks[check_id]) == ("not-applicable", "recursion-guard")
        assert checks[check_id].argv is None


def test_self_referential_and_writer_scripts_are_never_runnable(tmp_path):
    sc = scope(".", (JS,), scripts=(script("lint", rig_lint=True), script("lint:ci", writes=True)))
    checks = _script_check(tmp_path, sc)
    assert state(checks["script:package.json:lint@."]) == ("not-applicable", "self-referential-script")
    assert state(checks["script:package.json:lint:ci@."]) == ("not-applicable", "writes-files")


@pytest.mark.parametrize("name", ["lint fix", "lint;rm", "lint$(x)", "-lint", "li`nt`", "lint\nx", "", "lint|x"])
def test_unsafe_script_names_are_dropped_and_never_interpolated(tmp_path, name):
    sc = scope(".", (JS,), scripts=(script(name), script("lint")))
    checks = _script_check(tmp_path, sc)
    assert set(checks) >= {"script:package.json:lint@."}
    script_argvs = [c.argv for c in checks.values() if c.argv and c.provider == "script"]
    assert not [argv for argv in script_argvs if any(name and name in a for a in argv)]


def test_scripts_without_roles_are_ignored(tmp_path):
    sc = scope(".", (JS,), scripts=(script("random", ()),))
    assert not [i for i in _script_check(tmp_path, sc) if i.startswith("script:")]


# ---------------------------------------------------------------- policy: config, platforms


def test_disabled_category_and_provider_become_not_applicable(tmp_path):
    from riglib.lint_run_config import ProviderOverride

    root = make_repo(tmp_path)
    install(root, "node_modules/.bin/eslint")
    install(root, "node_modules/.bin/prettier")
    sc = scope(".", (JS,), tools=(tool("eslint", "lint", config=("e",)), tool("prettier", "format")))
    cats = dict(config().categories, format=False)
    cfg = config(categories=cats, providers={"eslint": ProviderOverride(enabled=False)})
    checks = plan(root, sc, cfg=cfg)
    assert state(checks["eslint@."]) == ("not-applicable", "disabled-by-config")
    assert state(checks["prettier@."]) == ("not-applicable", "disabled-by-config")
    assert checks["eslint@."].argv is None and checks["rig-check@."].argv is not None


def test_timeout_override_is_carried_per_provider(tmp_path):
    from riglib.lint_run_config import ProviderOverride

    root = make_repo(tmp_path)
    install(root, "node_modules/.bin/eslint")
    sc = scope(".", (JS,), tools=(tool("eslint", "lint", config=("e",)),))
    checks = plan(root, sc, cfg=config(providers={"eslint": ProviderOverride(timeout_seconds=600)}))
    assert checks["eslint@."].timeout_s == 600 and checks["rig-check@."].timeout_s is None


def test_unsupported_platform_provider(tmp_path, monkeypatch):
    root = make_repo(tmp_path)
    monkeypatch.setattr(registry, "_REGISTRY", dict(registry._REGISTRY))
    monkeypatch.setattr(registry, "PROVIDER_IDS", registry.PROVIDER_IDS)
    lp.register(lp.ProviderSpec(
        "fake-mac-only", "fake", "lint", (JS,), frozenset({"plan9"}), None, (),
        lambda ctx: (build("fake-mac-only", "lint", ctx.scope, "fake", 3, frozenset(), argv=("fake",)),),
    ))
    checks = plan(root, scope(".", (JS,)), system="linux")
    assert state(checks["fake-mac-only@."]) == ("unsupported-platform", "platform-unsupported")
    assert checks["fake-mac-only@."].argv is None
    assert plan(root, scope(".", (JS,)), system="plan9")["fake-mac-only@."].unavailable is None


# ---------------------------------------------------------------- chunk_files


def test_chunk_files_bounds_order_and_dash_prefix():
    files = [f"d/{i:03d}.sh" for i in range(100)] + ["-rf"]
    chunks = lp.chunk_files(files, max_bytes=100)
    flat = [p for chunk in chunks for p in chunk]
    assert flat == [*files[:-1], "./-rf"]
    assert all(sum(len(p) + 1 for p in chunk) <= 100 for chunk in chunks) and len(chunks) > 5


def test_chunk_files_edge_cases():
    assert lp.chunk_files([]) == ()
    assert lp.chunk_files(["a"], max_bytes=100_000) == (("a",),)
    huge = "x" * 500
    assert lp.chunk_files(["a", huge, "b"], max_bytes=50) == (("a",), (huge,), ("b",))


# ---------------------------------------------------------------- real tool: a dash-named file is a file, not an option


def test_real_ruff_lints_a_file_literally_named_like_an_option(tmp_path):
    import subprocess

    from tests.lint_real_helpers import pinned_tool

    ruff = str(pinned_tool("ruff", "RIG_TEST_RUFF_BIN"))
    names = ["--config=evil.py", "-x.py", "ok.py"]
    for name in names:
        (tmp_path / name).write_text("import os\n", encoding="utf-8")
    operands = lp.chunk_files(names)[0]
    assert operands == ("./--config=evil.py", "./-x.py", "ok.py")
    done = subprocess.run([ruff, "check", "--no-cache", "--isolated", "--select", "F401", *operands],
                          cwd=tmp_path, capture_output=True, text=True, check=False)
    assert done.returncode == 1, done.stderr
    for shown in ("--config=evil.py", "-x.py", "ok.py"):
        assert shown in done.stdout
