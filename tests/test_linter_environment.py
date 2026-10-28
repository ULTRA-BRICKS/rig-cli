import json
import subprocess

import pytest

from riglib.linter_environment import inspect_linter_environment, required_oxc_packages
from tests.lint_discovery_helpers import make_repo


def _package(repo, *, dev=None, scripts=None):
    payload = {}
    if dev is not None:
        payload["devDependencies"] = dev
    if scripts is not None:
        payload["scripts"] = scripts
    (repo / "package.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture(params=["plain-dir", "git-repo"])
def repo(request, tmp_path):
    """The same behavior must hold with and without git: the detection source differs, the answer does not."""
    if request.param == "git-repo":
        return make_repo(tmp_path, {})
    path = tmp_path / "repo"
    path.mkdir()
    return path


def _typescript(repo, declared, installed=None):
    _package(repo, dev={"oxlint": "x", "oxlint-tsgolint": "x", "typescript": declared})
    if installed is not None:
        target = repo / "node_modules" / "typescript"
        target.mkdir(parents=True)
        (target / "package.json").write_text(json.dumps({"version": installed}), encoding="utf-8")
        (repo / ".gitignore").write_text("node_modules\n", encoding="utf-8")


def test_no_linter_is_blocked_with_ready_to_copy_prompt(repo):
    _package(repo)
    env = inspect_linter_environment(repo)
    assert not env.ready
    assert not env.oxlint_present
    assert "no repository-local Oxlint dependency" in env.reason
    assert env.missing_oxc_packages == ("oxlint",)
    assert "Install compatible oxlint as repository development dependencies" in env.agent_prompt
    assert "@oxlint/plugins" not in env.agent_prompt and "oxfmt" not in env.agent_prompt
    assert "do not rely on a globally installed binary" in env.agent_prompt


def test_foreign_dependency_is_blocked_and_named(repo):
    _package(repo, dev={"eslint": "x"})
    env = inspect_linter_environment(repo)
    assert not env.ready
    assert env.foreign_linters == ("ESLint",)
    assert "uses ESLint" in env.reason


def test_foreign_config_is_detected_without_dependency(repo):
    _package(repo)
    (repo / "biome.json").write_text("{}\n", encoding="utf-8")
    env = inspect_linter_environment(repo)
    assert not env.ready
    assert "Biome" in env.foreign_linters


def test_foreign_script_is_detected_without_dependency(repo):
    _package(repo, scripts={"lint": "eslint src"})
    env = inspect_linter_environment(repo)
    assert not env.ready
    assert "ESLint" in env.foreign_linters


def test_standard_version_script_is_not_standardjs(repo):
    _package(repo, scripts={"release": "standard-version"})
    assert "StandardJS" not in inspect_linter_environment(repo).foreign_linters


def test_repo_local_oxlint_is_ready_even_when_foreign_linter_coexists(repo):
    _package(repo, dev={"oxlint": "x", "eslint": "x"})
    env = inspect_linter_environment(repo)
    assert env.ready
    assert env.oxlint_present
    assert env.foreign_linters == ("ESLint",)
    assert env.missing_oxc_packages == ()


def test_plugins_are_required_only_for_anti_slop(repo):
    _package(repo, dev={"oxlint": "x"})
    assert inspect_linter_environment(repo).missing_oxc_packages == ()
    env = inspect_linter_environment(repo, anti_slop=True)
    assert env.ready and env.missing_oxc_packages == ("@oxlint/plugins",)
    assert "@oxlint/plugins" in env.agent_prompt


def test_oxfmt_is_not_part_of_the_lint_gate(repo):
    _package(repo, dev={"oxlint": "x"})
    assert "oxfmt" not in inspect_linter_environment(repo, anti_slop=True).missing_oxc_packages


def test_full_oxc_toolchain_has_no_missing_packages(repo):
    _package(repo, dev={"oxlint": "x", "oxfmt": "x", "@oxlint/plugins": "x"})
    env = inspect_linter_environment(repo, anti_slop=True)
    assert env.ready
    assert env.missing_oxc_packages == ()


@pytest.mark.parametrize(("anti_slop", "type_aware", "expected"), [
    (False, False, ("oxlint",)),
    (True, False, ("oxlint", "@oxlint/plugins")),
    (False, True, ("oxlint", "oxlint-tsgolint")),
    (True, True, ("oxlint", "@oxlint/plugins", "oxlint-tsgolint")),
])
def test_required_oxc_packages(anti_slop, type_aware, expected):
    assert required_oxc_packages(anti_slop=anti_slop, type_aware=type_aware) == expected


@pytest.mark.parametrize("anti_slop", [False, True])
def test_prompt_names_exactly_the_required_packages(repo, anti_slop):
    _package(repo, dev={"oxlint": "x", "typescript": "^7.0.0"})
    env = inspect_linter_environment(repo, anti_slop=anti_slop)
    required = required_oxc_packages(anti_slop=anti_slop, type_aware=True)
    for name in ("oxlint", "@oxlint/plugins", "oxlint-tsgolint"):
        assert (name in env.agent_prompt) == (name in required), name
    assert env.missing_oxc_packages == tuple(p for p in required if p != "oxlint")


def test_type_aware_with_typescript_7_and_tsgolint(repo):
    _typescript(repo, "^7.0.0")
    env = inspect_linter_environment(repo)
    assert env.type_aware
    assert "TypeScript 7" in env.type_aware_reason
    assert env.missing_oxc_packages == ()


def test_type_aware_uses_the_installed_version_over_the_declared_spec(repo):
    _typescript(repo, "^7.0.0", installed="6.9.0")
    assert not inspect_linter_environment(repo).type_aware


def test_installed_typescript_7_enables_type_aware_for_a_loose_spec(tmp_path):
    repo = make_repo(tmp_path, {})
    _typescript(repo, "latest", installed="7.1.0")
    assert inspect_linter_environment(repo).type_aware


def test_typescript_5_declared_is_not_type_aware(repo):
    _typescript(repo, "~5.4.0")
    env = inspect_linter_environment(repo)
    assert not env.type_aware
    assert "TypeScript 5" in env.type_aware_reason and "7" in env.type_aware_reason
    assert env.missing_oxc_packages == ()


def test_typescript_7_without_tsgolint_asks_for_it(repo):
    _package(repo, dev={"oxlint": "x", "typescript": "^7.0.0"})
    env = inspect_linter_environment(repo)
    assert not env.type_aware
    assert "oxlint-tsgolint" in env.type_aware_reason
    assert env.missing_oxc_packages == ("oxlint-tsgolint",)


def test_tsgolint_without_typescript_is_not_type_aware(repo):
    _package(repo, dev={"oxlint": "x", "oxlint-tsgolint": "x"})
    env = inspect_linter_environment(repo)
    assert not env.type_aware
    assert "TypeScript" in env.type_aware_reason
    assert env.missing_oxc_packages == ()


def test_existing_oxlint_config_files_are_reported(repo):
    _package(repo, dev={"oxlint": "x"})
    (repo / ".oxlintrc.jsonc").write_text("{}\n", encoding="utf-8")
    (repo / "oxlint.config.ts").write_text("export default {}\n", encoding="utf-8")
    assert inspect_linter_environment(repo).config_files == (".oxlintrc.jsonc", "oxlint.config.ts")


def test_defaults_keep_old_constructor_calls_valid():
    from riglib.linter_environment import LinterEnvironment
    env = LinterEnvironment(True, True, (), (), "r", "p")
    assert (env.type_aware, env.type_aware_reason, env.config_files) == (False, "", ())


def test_nested_package_does_not_count_for_the_root_gate(tmp_path):
    repo = make_repo(tmp_path, {"package.json": "{}", "packages/a/package.json": json.dumps({"devDependencies": {"oxlint": "1"}})})
    assert not inspect_linter_environment(repo).ready


def test_git_subdirectory_falls_back_to_root_only_reading(tmp_path):
    repo = make_repo(tmp_path, {"sub/package.json": json.dumps({"devDependencies": {"oxlint": "1"}})})
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    assert inspect_linter_environment(repo / "sub").ready
