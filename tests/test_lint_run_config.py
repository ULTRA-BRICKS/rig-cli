"""`lint` config block (the `rig lint run` aggregator): defaults, cascade, strict validation, leniency."""

from __future__ import annotations

import re
import sys
import types
from pathlib import Path

import pytest

from riglib import config, config_schema, lint_run_config as lrc, schema
from riglib.config import ConfigError
from tests.lint_check_helpers import make_repo, put

PROVIDER_IDS = frozenset({"eslint", "oxlint", "ruff-check", "rig-check"})
PIN_KEYS = ("oxlint", "oxfmt", "oxlint-tsgolint", "@oxlint/plugins", "ruff")


@pytest.fixture(autouse=True)
def _provider_registry(monkeypatch):
    """The block validates ids/pins against the provider registry; a fixed stand-in keeps these
    tests independent of which providers the catalog happens to contain."""
    stub = types.ModuleType("riglib.lint_providers")
    stub.PROVIDER_IDS = PROVIDER_IDS
    stub.PINS = {key: object() for key in PIN_KEYS}
    monkeypatch.setitem(sys.modules, "riglib.lint_providers", stub)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return make_repo(tmp_path)


def _global(tmp_path: Path, text: str) -> None:
    put(tmp_path / "xdg" / "rig", "config.yaml", text)


def _repo_yaml(repo: Path, text: str) -> None:
    put(repo, "rig.yaml", "version: 1\n" + text)


def _loaded(repo: Path) -> lrc.LintConfig:
    return lrc.resolve(config.load(repo).data)


def test_defaults_without_any_config(repo):
    cfg = _loaded(repo)
    assert cfg == lrc.DEFAULTS
    assert cfg.enabled and cfg.timeout_seconds == 300
    assert dict(cfg.categories) == {"lint": True, "typecheck": True, "format": True, "limits": True, "scripts": True}
    assert (cfg.bootstrap.enabled, cfg.bootstrap.javascript, cfg.bootstrap.python) == (True, True, True)
    assert dict(cfg.bootstrap.versions) == {} and dict(cfg.providers) == {}
    assert cfg.lint_checks_enabled and cfg.linters_enabled and dict(cfg.linters_rules) == {}


def test_categories_constant_is_the_rank_order():
    assert lrc.CATEGORIES == ("lint", "typecheck", "format", "limits", "scripts")


def test_global_only(tmp_path, repo):
    _global(tmp_path, "version: 1\nlint: {timeout_seconds: 60, categories: {format: false}}\n")
    cfg = _loaded(repo)
    assert cfg.timeout_seconds == 60 and cfg.categories["format"] is False and cfg.categories["lint"] is True


def test_repo_wins_per_key_with_deep_merge(tmp_path, repo):
    _global(tmp_path, "version: 1\nlint: {timeout_seconds: 60, bootstrap: {python: false, versions: {ruff: 0.1.0}}}\n")
    _repo_yaml(repo, "lint: {timeout_seconds: 90, bootstrap: {javascript: false}, providers: {eslint: {timeout_seconds: 600}}}\n")
    cfg = _loaded(repo)
    assert cfg.timeout_seconds == 90
    assert (cfg.bootstrap.javascript, cfg.bootstrap.python, cfg.bootstrap.enabled) == (False, False, True)
    assert cfg.providers["eslint"] == lrc.ProviderOverride(enabled=True, timeout_seconds=600)


def test_provider_override_defaults_and_disable(repo):
    _repo_yaml(repo, "lint: {providers: {oxlint: {enabled: false}}}\n")
    assert _loaded(repo).providers["oxlint"] == lrc.ProviderOverride(enabled=False, timeout_seconds=None)


def test_exact_versions_are_kept(repo):
    _repo_yaml(repo, "lint: {bootstrap: {versions: {ruff: '0.17.3', oxlint: 1.90.0-rc.1}}}\n")
    assert dict(_loaded(repo).bootstrap.versions) == {"ruff": "0.17.3", "oxlint": "1.90.0-rc.1"}


def test_lint_checks_and_linters_are_plumbed(repo):
    _repo_yaml(repo, "lint_checks: {enabled: false}\nlinters: {enabled: false, rules: {no-console: warn}}\n")
    cfg = _loaded(repo)
    assert cfg.lint_checks_enabled is False and cfg.linters_enabled is False
    assert dict(cfg.linters_rules) == {"no-console": "warn"}


@pytest.mark.parametrize(
    ("block", "needle"),
    [
        ("{bogus: 1}", "lint.bogus"),
        ("{enabled: yes-please}", "lint.enabled"),
        ("{enabled: 1}", "lint.enabled"),
        ("{timeout_seconds: 0}", "lint.timeout_seconds"),
        ("{timeout_seconds: 3601}", "lint.timeout_seconds"),
        ("{timeout_seconds: true}", "lint.timeout_seconds"),
        ("{timeout_seconds: '30'}", "lint.timeout_seconds"),
        ("{timeout_seconds: 1.5}", "lint.timeout_seconds"),
        ("{categories: {tests: true}}", "lint.categories.tests"),
        ("{categories: {lint: 1}}", "lint.categories.lint"),
        ("{categories: [lint]}", "lint.categories"),
        ("{bootstrap: {sudo: true}}", "lint.bootstrap.sudo"),
        ("{bootstrap: {python: 'no'}}", "lint.bootstrap.python"),
        ("{bootstrap: {versions: {ruff: '^0.17.0'}}}", "lint.bootstrap.versions.ruff"),
        ("{bootstrap: {versions: {ruff: latest}}}", "lint.bootstrap.versions.ruff"),
        ("{bootstrap: {versions: {ruff: '0.17'}}}", "lint.bootstrap.versions.ruff"),
        ("{bootstrap: {versions: {ruff: 17}}}", "lint.bootstrap.versions.ruff"),
        ("{bootstrap: {versions: {eslint: 9.0.0}}}", "lint.bootstrap.versions.eslint"),
        ("{providers: {nope: {enabled: true}}}", "lint.providers.nope"),
        ("{providers: {eslint: {argv: [rm]}}}", "lint.providers.eslint.argv"),
        ("{providers: {eslint: {enabled: 2}}}", "lint.providers.eslint.enabled"),
        ("{providers: {eslint: {timeout_seconds: 0}}}", "lint.providers.eslint.timeout_seconds"),
        ("{providers: {eslint: on}}", "lint.providers.eslint"),
        ("{providers: [eslint]}", "lint.providers"),
    ],
)
def test_invalid_values_name_the_key_and_layer(repo, block, needle):
    _repo_yaml(repo, f"lint: {block}\n")
    with pytest.raises(ConfigError) as exc:
        config.load(repo)
    assert exc.value.schema_path == needle and "layer: repo" in str(exc.value)


@pytest.mark.parametrize("block", ["[]", "just-text", "null", "3"])
def test_block_must_be_a_mapping(repo, block):
    _repo_yaml(repo, f"lint: {block}\n")
    with pytest.raises(ConfigError, match=r"lint (must be a mapping|is empty)"):
        config.load(repo)


def test_a_bad_global_layer_is_named_global(tmp_path, repo):
    _global(tmp_path, "version: 1\nlint: {timeout_seconds: 0}\n")
    with pytest.raises(ConfigError, match=r"layer: global"):
        config.load(repo)


def test_boundaries_are_inclusive(repo):
    _repo_yaml(repo, "lint: {timeout_seconds: 3600, providers: {eslint: {timeout_seconds: 1}}}\n")
    cfg = _loaded(repo)
    assert cfg.timeout_seconds == 3600 and cfg.providers["eslint"].timeout_seconds == 1


def test_validate_runs_on_a_single_layer_too():
    config.validate({"version": 1, "lint": {"timeout_seconds": 10}})
    with pytest.raises(ConfigError, match=r"lint\.timeout_seconds"):
        config.validate({"version": 1, "lint": {"timeout_seconds": 10_000}})


def test_lint_is_a_valid_top_level_key_in_both_validator_and_schema():
    assert "lint" in config._VALID_TOP_KEYS and "lint" in config_schema.TOP_LEVEL_KEYS


def test_schema_accepts_and_rejects_the_same_shapes():
    node = config_schema.json_schema()["properties"]["lint"]
    assert node["additionalProperties"] is False
    assert node["properties"]["timeout_seconds"]["minimum"] == 1 and node["properties"]["timeout_seconds"]["maximum"] == 3600
    assert set(node["properties"]["categories"]["properties"]) == set(lrc.CATEGORIES)
    names = node["properties"]["bootstrap"]["properties"]["versions"]["propertyNames"]["pattern"]
    assert all(re.search(names, key) for key in PIN_KEYS) and not re.search(names, "eslint")
    item = node["properties"]["providers"]["additionalProperties"]
    assert set(item["properties"]) == {"enabled", "timeout_seconds"} and item["additionalProperties"] is False


def test_the_wizard_area_lists_only_keys_the_validator_accepts():
    area = next(a for a in schema.AREAS if a.category == "lint")
    keys = {o.key for o in area.options}
    assert {"lint.enabled", "lint.timeout_seconds", "lint.bootstrap.enabled", "lint.bootstrap.javascript",
            "lint.bootstrap.python"} <= keys
    assert {f"lint.categories.{name}" for name in lrc.CATEGORIES} <= keys
    assert schema.writable_layer_for_category("lint") == schema.REPO


# ── load_config: the leniency contract shared with `rig lint check` ────────────────


def test_load_config_cascades_global_and_repo(tmp_path, repo):
    _global(tmp_path, "version: 1\nlint: {timeout_seconds: 60, categories: {lint: false}}\n")
    _repo_yaml(repo, "lint: {timeout_seconds: 90}\n")
    cfg = lrc.load_config(str(repo), None)
    assert cfg.timeout_seconds == 90 and cfg.categories["lint"] is False


def test_load_config_defaults_without_any_file(repo):
    assert lrc.load_config(str(repo), None) == lrc.DEFAULTS


def test_load_config_honours_an_explicit_config_file(tmp_path, repo):
    put(tmp_path, "other.yaml", "version: 1\nlint: {timeout_seconds: 42}\n")
    assert lrc.load_config(str(repo), str(tmp_path / "other.yaml")).timeout_seconds == 42


def test_an_unrelated_block_error_is_a_warning(repo, capsys):
    _repo_yaml(repo, "future_block: {x: 1}\nlint: {timeout_seconds: 7}\nlint_checks: {enabled: false}\n")
    cfg = lrc.load_config(str(repo), None)
    assert cfg.timeout_seconds == 7 and cfg.lint_checks_enabled is False
    err = capsys.readouterr().err
    assert err.startswith("warning: ignoring a config error outside lint:") and "future_block" in err


def test_lenient_path_still_cascades_layers(tmp_path, repo, capsys):
    _global(tmp_path, "version: 1\nlint: {timeout_seconds: 60, bootstrap: {python: false}}\n")
    _repo_yaml(repo, "future_block: 1\nlint: {timeout_seconds: 7}\n")
    cfg = lrc.load_config(str(repo), None)
    assert cfg.timeout_seconds == 7 and cfg.bootstrap.python is False
    assert "future_block" in capsys.readouterr().err


@pytest.mark.parametrize("lint_block", [
    "lint: {timeout_seconds: 0}",
    "lint: {typo: true}",
    "lint: {providers: {nope: {}}}",
    "lint: {bootstrap: {versions: {ruff: latest}}}",
])
def test_an_error_inside_lint_stays_fatal_even_with_an_unrelated_error(repo, lint_block):
    _repo_yaml(repo, f"future_block: 1\n{lint_block}\n")
    with pytest.raises(ConfigError):
        lrc.load_config(str(repo), None)


def test_an_error_inside_lint_is_fatal_without_other_errors(repo):
    _repo_yaml(repo, "lint: {timeout_seconds: 0}\n")
    with pytest.raises(ConfigError, match=r"lint\.timeout_seconds"):
        lrc.load_config(str(repo), None)


def test_unparsable_yaml_stays_fatal(repo):
    _repo_yaml(repo, "lint: [unterminated\n")
    with pytest.raises(ConfigError):
        lrc.load_config(str(repo), None)


def test_resolved_mappings_are_read_only(repo):
    cfg = _loaded(repo)
    with pytest.raises(TypeError):
        cfg.categories["lint"] = False  # type: ignore[index]
