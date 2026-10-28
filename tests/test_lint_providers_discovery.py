"""Real repos through `discover` + `plan_checks`: the acceptance fixtures of review F2-F8 end to end.

No tool runs; stand-in binaries live in the tmp repo. The point is that what discovery reports and what the
providers plan agree: nothing configured is hidden, nothing that can write is run, no source file is silently
ignored.
"""
from __future__ import annotations


from riglib import lint_providers as lp
from riglib.lint_discovery import discover
from tests.lint_discovery_helpers import make_repo as git_repo
from tests.lint_discovery_helpers import pkg
from tests.lint_providers_helpers import config, install, make_repo, plan, scope, state, symlink, tool, tool_env

LOCK = "# lock\n"
IGNORE = {".gitignore": "node_modules/\n.venv/\n"}


def _plan(root, **env_kwargs):
    disc = discover(root)
    env = tool_env(root, **env_kwargs)
    checks = {c.id: c for c in lp.plan_checks(disc, config(), env)}
    return disc, env, checks


def test_declared_eslint_without_config_still_gets_a_lint_check(tmp_path):
    root = git_repo(tmp_path, {
        **IGNORE, "package.json": pkg(devDependencies={"eslint": "9.0.0"}), "package-lock.json": LOCK,
        "src/a.ts": "export const a = 1\n"})
    disc, env, checks = _plan(root, which={"npm": "/bin/npm"})
    assert state(checks["eslint@."]) == ("not-applicable", "no-config-file")
    assert state(checks["oxlint@."]) == ("missing-tool", "not-bootstrapped")  # exit 127, never a green pass
    assert {n.role for n in lp.bootstrap_needs(disc, config(), env)} == {"lint", "format"}


def test_lone_python_file_is_visible_lintable_and_needs_ruff(tmp_path):
    root = git_repo(tmp_path, {"tool.py": "import os\n"})
    disc, env, checks = _plan(root)
    assert disc.source_files["python"] == ("tool.py",)
    assert state(checks["ruff-check@."]) == ("missing-tool", "not-bootstrapped")
    assert {(n.ecosystem, n.tool) for n in lp.bootstrap_needs(disc, config(), env)} == {("python", "ruff")}
    assert not lp.coverage(disc, tuple(checks.values())).complete


def test_python_script_in_a_javascript_repo_stays_lintable(tmp_path):
    root = git_repo(tmp_path, {
        **IGNORE, "package.json": pkg(name="x"), "package-lock.json": LOCK, "src/a.js": "1\n",
        "scripts/gen.py": "import os\n"})
    disc, env, checks = _plan(root, which={"npm": "/bin/npm"})
    assert "python" in disc.scope(".").languages
    assert "lint:python:." in checks["ruff-check@."].covers
    assert ("python", "ruff") in {(n.ecosystem, n.tool) for n in lp.bootstrap_needs(disc, config(), env)}


def test_requirements_only_project_plans_ruff_through_the_cache_route(tmp_path):
    root = git_repo(tmp_path, {"requirements.txt": "requests==2.0\n", "app.py": "x = 1\n"})
    disc, env, checks = _plan(root)
    assert disc.scope(".").package_manager.name == "pip"
    assert state(checks["ruff-check@."]) == ("missing-tool", "not-bootstrapped")
    assert [n.ecosystem for n in lp.bootstrap_needs(disc, config(), env) if n.tool == "ruff"] == ["python", "python"]


def test_kotlin_only_repository_is_never_complete(tmp_path):
    root = git_repo(tmp_path, {"Main.kt": "fun main() {}\n"})
    disc, env, checks = _plan(root)
    result = lp.coverage(disc, tuple(checks.values()))
    assert (result.files_checked, result.files_uncovered, result.uncovered) == (0, 1, (("kotlin", 1),))
    assert not result.complete


def test_root_eslint_config_is_reported_once_for_a_monorepo(tmp_path):
    root = git_repo(tmp_path, {
        **IGNORE, "package.json": pkg(devDependencies={"eslint": "9"}), "package-lock.json": LOCK,
        "eslint.config.js": "export default []\n",
        "packages/a/package.json": pkg(devDependencies={"eslint": "9"}), "packages/a/x.ts": "1\n",
        "packages/b/package.json": pkg(devDependencies={"eslint": "9"}), "packages/b/y.ts": "1\n"})
    install(root, "node_modules/.bin/eslint")
    disc, env, checks = _plan(root)
    eslint = [c for c in checks.values() if c.provider == "eslint"]
    assert [(c.id, c.cwd) for c in eslint] == [("eslint@.", ".")]


def test_pnpm_store_outside_the_repo_does_not_cause_a_false_missing_tool(tmp_path):
    root = git_repo(tmp_path, {
        **IGNORE, "package.json": pkg(devDependencies={"oxlint": "1.90.0"}), "pnpm-lock.yaml": LOCK, "a.ts": "1\n"})
    store = install(tmp_path, "store/oxlint/bin/oxlint")
    symlink(store, root / "node_modules" / ".bin" / "oxlint")
    disc, env, checks = _plan(root)
    assert checks["oxlint@."].unavailable is None and checks["oxlint@."].resolution.kind == "local-bin"
    assert "resolved_outside_repo" in checks["oxlint@."].resolution.detail


def test_oxfmt_format_check_script_is_never_run(tmp_path):
    root = git_repo(tmp_path, {
        **IGNORE, "package.json": pkg(scripts={"format:check": "oxfmt ."}), "package-lock.json": LOCK, "a.ts": "1\n"})
    disc, env, checks = _plan(root, which={"npm": "/bin/npm"})
    script_check = checks["script:package.json:format:check@."]
    assert state(script_check) == ("not-applicable", "writes-unknown") and script_check.argv is None


def test_lint_script_for_javascript_does_not_hide_python_in_a_mixed_repo(tmp_path):
    root = git_repo(tmp_path, {
        **IGNORE, "package.json": pkg(scripts={"lint": "eslint ."}, devDependencies={"eslint": "9"}),
        "package-lock.json": LOCK, "eslint.config.js": "export default []\n", "a.js": "1\n",
        "pyproject.toml": "[project]\nname='x'\n", "m.py": "x = 1\n"})
    install(root, "node_modules/.bin/eslint")
    disc, env, checks = _plan(root, which={"npm": "/bin/npm"})
    assert state(checks["eslint@."]) == ("not-applicable", "covered-by:script:package.json:lint@.")
    assert "ruff-check@." in checks and checks["ruff-check@."].precedence == lp.PRECEDENCE_BOOTSTRAP


# ---------------------------------------------------------------- git-ignored trees are never recursed into


def _biome_repo(tmp_path, ignore):
    files = {
        ".gitignore": ignore, "package.json": pkg(devDependencies={"@biomejs/biome": "2.0.0"}),
        "package-lock.json": LOCK, "biome.json": "{}\n", "src/a.ts": "1\n", "src/b.json": "{}\n", "README.md": "x\n",
        ".worktrees/x/biome.json": "{}\n", ".worktrees/x/src/c.ts": "1\n", "node_modules/p/i.js": "1\n",
    }
    root = git_repo(tmp_path, files)
    install(root, "node_modules/.bin/biome")
    return root


def test_biome_gets_enumerated_files_when_an_ignored_tree_has_nested_roots(tmp_path):
    root = _biome_repo(tmp_path, ".worktrees/\nnode_modules/\n")
    disc, env, checks = _plan(root)
    for check_id, lead in (("biome-lint@.", "lint"), ("biome-format@.", "format")):
        argv = checks[check_id].argv
        assert argv[1] == lead and "." not in argv
        assert set(argv[2:]) == {"biome.json", "package.json", "package-lock.json", "src/a.ts", "src/b.json"}
        assert not [a for a in argv[2:] if ".worktrees" in a or "node_modules" in a or a.endswith(".md")]
    assert checks["biome-lint@."].fix_argv[1:3] == ("lint", "--write")


def test_dot_is_kept_when_only_tool_default_ignored_dirs_exist(tmp_path):
    root = git_repo(tmp_path, {
        ".gitignore": "node_modules/\n", "package.json": pkg(devDependencies={"@biomejs/biome": "2"}),
        "package-lock.json": LOCK, "biome.json": "{}\n", "src/a.ts": "1\n", "node_modules/p/i.js": "1\n"})
    install(root, "node_modules/.bin/biome")
    disc, env, checks = _plan(root)
    assert disc.ignored_dirs == ("node_modules",)
    assert checks["biome-lint@."].argv[1:] == ("lint", ".")


def test_eslint_and_mypy_use_enumerated_files_too(tmp_path):
    root = git_repo(tmp_path, {
        ".gitignore": "dist/\n", "package.json": pkg(devDependencies={"eslint": "9"}), "package-lock.json": LOCK,
        "eslint.config.js": "export default []\n", "src/a.js": "1\n", "dist/bundle.js": "1\n",
        "pyproject.toml": "[tool.mypy]\nstrict = true\n", "py/m.py": "x = 1\n", "dist/gen.py": "x = 1\n"})
    install(root, "node_modules/.bin/eslint")
    install(root, ".venv/bin/mypy")
    disc, env, checks = _plan(root)
    assert checks["eslint@."].argv[1:] == ("eslint.config.js", "src/a.js")
    assert checks["mypy@."].argv[1:] == ("py/m.py",)


def test_ignored_tree_without_tracked_files_of_the_kind_is_not_applicable_not_a_pass(tmp_path):
    root = git_repo(tmp_path, {
        ".gitignore": "dist/\n", "setup.cfg": "[flake8]\nmax-line-length = 100\n", "dist/x.py": "x = 1\n"})
    install(root, ".venv/bin/flake8")
    disc, env, checks = _plan(root)
    assert state(checks["flake8@."]) == ("not-applicable", "no-matching-files")


def test_dependency_install_hint_names_the_scopes_manager(tmp_path):
    root = git_repo(tmp_path, {
        "package.json": pkg(workspaces=["app"]), "pnpm-lock.yaml": LOCK, "pnpm-workspace.yaml": "packages: ['app']\n",
        "app/package.json": pkg(name="app"), "app/a.ts": "1\n"})
    disc = discover(root)
    assert "`pnpm install` in app" in lp.dependency_install_hint(disc, "app")
    assert "`pnpm install`, then" in lp.dependency_install_hint(disc, ".")
    assert "install the project's dependencies" in lp.dependency_install_hint(disc, "no/such/scope")


# ---------------------------------------------------------------- file operands never act as options

HOSTILE = ("--config=evil.js", "-x.ts", "dir with space/a b.ts", "line\nbreak.ts", "dir/-ok.ts")


def _hostile_repo(tmp_path):
    files = {".gitignore": "dist/\n", "package.json": pkg(devDependencies={"@biomejs/biome": "2", "eslint": "9"}),
             "package-lock.json": LOCK, "biome.json": "{}\n", "eslint.config.js": "export default []\n",
             "dist/x.ts": "1\n", **{name: "1\n" for name in HOSTILE}}
    root = git_repo(tmp_path, files)
    install(root, "node_modules/.bin/biome")
    install(root, "node_modules/.bin/eslint")
    return root


def test_dash_leading_git_file_names_are_neutralised_in_every_file_list_argv(tmp_path):
    root = _hostile_repo(tmp_path)
    disc, env, checks = _plan(root)
    for check_id in ("biome-lint@.", "biome-format@.", "eslint@."):
        argv = checks[check_id].argv
        operands = [a for a in argv[1:] if a not in ("lint", "format")]
        assert "./--config=evil.js" in operands and "./-x.ts" in operands
        assert "dir/-ok.ts" in operands and "dir with space/a b.ts" in operands and "line\nbreak.ts" in operands
        assert not [a for a in operands if a.startswith("-")]
        assert "--config=evil.js" not in operands and "-x.ts" not in operands


def test_shell_go_and_fix_argv_neutralise_dash_names_too(tmp_path):
    root = make_repo(tmp_path)
    which = {"shellcheck": "/s", "shfmt": "/f", "gofmt": "/g"}
    sh = scope(".", (), files={"shell": ("--help.sh", "-n.sh", "a b.sh"), "go": ("--version.go",)})
    checks = plan(root, scope(".", ("go",), files=sh.files), which=which)
    assert checks["shellcheck@."].argv == ("/s", "--", "./--help.sh", "./-n.sh", "a b.sh")
    assert checks["shfmt@."].fix_argv == ("/f", "-w", "--", "./--help.sh", "./-n.sh", "a b.sh")
    assert checks["gofmt@."].argv == ("/g", "-l", "--", "./--version.go")


def test_pylint_package_dirs_starting_with_dash_are_neutralised(tmp_path):
    root = make_repo(tmp_path)
    (root / "--rcfile=evil").mkdir()
    (root / "--rcfile=evil" / "__init__.py").write_text("", encoding="utf-8")
    install(root, ".venv/bin/pylint")
    argv = plan(root, scope(".", ("python",), tools=(tool("pylint", "lint"),)))["pylint@."].argv
    assert argv[1:] == ("./--rcfile=evil",)


# ---------------------------------------------------------------- Oxlint carrier packages and the formatter argv


def _oxc_repo(tmp_path):
    root = git_repo(tmp_path, {
        **IGNORE, "package.json": pkg(devDependencies={"oxlint": "1.90.0", "oxfmt": "0.75.0", "@oxlint/plugins": "1.90.0"}),
        "package-lock.json": LOCK, "oxlint.config.ts": "export default {}\n",
        "tools/oxlint/anti-slop/rules/a.ts": "export const a = 1\n", "src/a.ts": "1\n"})
    install(root, "node_modules/.bin/oxlint")
    install(root, "node_modules/.bin/oxfmt")
    return root


def test_oxlint_carrier_packages_are_never_reported_as_unsupported_tools(tmp_path):
    disc, env, checks = _plan(_oxc_repo(tmp_path))
    assert lp.unsupported_tools(disc) == ()


def test_the_formatter_argv_hides_no_repo_path_even_for_a_managed_looking_directory(tmp_path):
    """A `// Managed by Rig.` header or a tools/oxlint/anti-slop directory earns no exclusion: it is ordinary repo code."""
    root = _oxc_repo(tmp_path)
    (root / "oxlint.config.ts").write_text("// Managed by Rig.\nexport default {}\n", encoding="utf-8")
    disc, env, checks = _plan(root)
    assert checks["oxfmt@."].argv[1:] == ("--check",) and checks["oxfmt@."].fix_argv[1:] == ()


# ---------------------------------------------------------------- coverage and tool operands share one file set


def _committed_repo_with_new_files(tmp_path):
    import subprocess

    root = git_repo(tmp_path, {
        ".gitignore": "dist/\nnode_modules/\n", "package.json": pkg(devDependencies={"eslint": "9"}), "package-lock.json": LOCK,
        "eslint.config.js": "export default []\n", "tracked.js": "1\n", "dist/bundle.js": "1\n"})
    for args in (["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    (root / "new_untracked.js").write_text("1\n", encoding="utf-8")  # created after the commit, never `git add`ed
    (root / "sub").mkdir()
    (root / "sub" / "new2.js").write_text("1\n", encoding="utf-8")
    install(root, "node_modules/.bin/eslint")
    return root


def test_a_file_counted_as_checked_is_a_file_the_tool_was_given_and_vice_versa(tmp_path):
    root = _committed_repo_with_new_files(tmp_path)
    disc, env, checks = _plan(root)
    given = set(checks["eslint@."].argv[1:])
    counted = set(disc.source_files["javascript"])
    assert {"new_untracked.js", "sub/new2.js", "tracked.js"} <= given  # untracked-not-ignored files are passed
    assert given == counted, "tool operands and the coverage inventory must be the same file set"
    assert "dist/bundle.js" not in given | counted  # an ignored tree is in neither
    result = lp.coverage(disc, tuple(checks.values()))
    assert result.files_checked == len(counted) and result.files_uncovered == 0
