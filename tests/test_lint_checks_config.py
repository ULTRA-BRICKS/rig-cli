"""`lint_checks` config block: defaults, cascade, strict validation, schema/registry, CLI surface."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from riglib import config, config_schema, lint_checks_config as lcc, schema
from riglib.cli import main
from riglib.config import ConfigError
from riglib.layers import REPO


def _w(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    r = tmp_path / "repo"
    r.mkdir()
    return r


def _global(tmp_path: Path, text: str) -> None:
    _w(tmp_path / "xdg" / "rig" / "config.yaml", text)


def _loaded(repo: Path) -> lcc.LintChecksConfig:
    return lcc.resolve(config.load(repo).data)


def test_defaults_without_any_config(repo):
    cfg = _loaded(repo)
    assert cfg == lcc.DEFAULTS
    assert (cfg.max_cognitive, cfg.max_cyclomatic, cfg.warn_above, cfg.error_above) == (15, 20, 1200, 2400)
    assert cfg.exempt_extensions == ("json",)
    assert cfg.enabled and cfg.complexity_enabled and cfg.file_lines_enabled


def test_global_only(tmp_path, repo):
    _global(tmp_path, "version: 1\nlint_checks: {complexity: {max_cognitive: 18}}\n")
    cfg = _loaded(repo)
    assert cfg.max_cognitive == 18 and cfg.max_cyclomatic == 20


def test_repo_only(repo):
    _w(repo / "rig.yaml", "version: 1\nlint_checks: {enabled: false, file_lines: {warn_above: 500}}\n")
    cfg = _loaded(repo)
    assert cfg.enabled is False and cfg.warn_above == 500 and cfg.error_above == 2400


def test_repo_overrides_global_deep_per_key(tmp_path, repo):
    _global(tmp_path, "version: 1\nlint_checks: {complexity: {max_cognitive: 18, max_cyclomatic: 30}}\n")
    _w(repo / "rig.yaml", "version: 1\nlint_checks: {complexity: {max_cognitive: 9}}\n")
    cfg = _loaded(repo)
    assert (cfg.max_cognitive, cfg.max_cyclomatic) == (9, 30)


def test_merged_warn_above_error_names_both_layers(tmp_path, repo):
    _global(tmp_path, "version: 1\nlint_checks: {file_lines: {error_above: 1000}}\n")
    _w(repo / "rig.yaml", "version: 1\nlint_checks: {file_lines: {warn_above: 1500}}\n")
    with pytest.raises(ConfigError) as exc:
        config.load(repo)
    msg = str(exc.value)
    assert "warn_above (1500, layer: repo)" in msg and "error_above (1000, layer: global)" in msg


def test_warn_above_default_error_conflict_names_default(repo):
    _w(repo / "rig.yaml", "version: 1\nlint_checks: {file_lines: {warn_above: 3000}}\n")
    with pytest.raises(ConfigError, match=r"error_above \(2400, layer: default\)"):
        config.load(repo)


def test_warn_equal_error_is_valid(repo):
    _w(repo / "rig.yaml", "version: 1\nlint_checks: {file_lines: {warn_above: 100, error_above: 100}}\n")
    assert _loaded(repo).warn_above == 100


def test_a_layer_validated_alone_is_not_judged_against_the_defaults_of_the_other_layers(tmp_path, repo):
    """`validate()` runs on one layer too (config set): warn_above 3000 is fine next to a global error_above 5000."""
    config.validate({"version": 1, "lint_checks": {"file_lines": {"warn_above": 3000}}})
    _global(tmp_path, "version: 1\nlint_checks: {file_lines: {error_above: 5000}}\n")
    _w(repo / "rig.yaml", "version: 1\nlint_checks: {file_lines: {warn_above: 3000}}\n")
    assert _loaded(repo).warn_above == 3000  # the MERGED result is still cross-checked by load()


@pytest.mark.parametrize("value", ["true", "1.5", "'15'", "0", "-3", "null", "10000001", "[1]"])
def test_invalid_threshold_values(repo, value):
    _w(repo / "rig.yaml", f"version: 1\nlint_checks: {{complexity: {{max_cognitive: {value}}}}}\n")
    with pytest.raises(ConfigError, match=r"lint_checks\.complexity\.max_cognitive.*layer: repo"):
        config.load(repo)


def test_threshold_upper_cap_is_inclusive(repo):
    _w(repo / "rig.yaml", "version: 1\nlint_checks: {complexity: {max_cyclomatic: 10000000}}\n")
    assert _loaded(repo).max_cyclomatic == 10_000_000


@pytest.mark.parametrize(
    "block",
    [
        "{enabled: null}",
        "{enabled: 1}",
        "{enabled: 'yes'}",
        "{complexity: {enabled: null}}",
        "{file_lines: {enabled: 0}}",
    ],
)
def test_flags_are_bool_only(repo, block):
    _w(repo / "rig.yaml", f"version: 1\nlint_checks: {block}\n")
    with pytest.raises(ConfigError, match="must be a bool"):
        config.load(repo)


@pytest.mark.parametrize("block", ["[1]", "true", "null", "'x'"])
def test_block_must_be_mapping(repo, block):
    _w(repo / "rig.yaml", f"version: 1\nlint_checks: {block}\n")
    with pytest.raises(ConfigError, match="lint_checks"):
        config.load(repo)


@pytest.mark.parametrize(
    ("block", "key"),
    [
        ("{bogus: 1}", "lint_checks.bogus"),
        ("{complexity: {max_cognitve: 3}}", "lint_checks.complexity.max_cognitve"),
        ("{file_lines: {warn: 3}}", "lint_checks.file_lines.warn"),
    ],
)
def test_unknown_key_at_every_level_names_key_and_allowed_set(repo, block, key):
    _w(repo / "rig.yaml", f"version: 1\nlint_checks: {block}\n")
    with pytest.raises(ConfigError) as exc:
        config.load(repo)
    assert exc.value.schema_path == key
    assert "use one of:" in exc.value.fix and "layer: repo" in str(exc.value)


def test_unknown_key_in_global_layer_names_global(tmp_path, repo):
    _global(tmp_path, "version: 1\nlint_checks: {nope: 1}\n")
    with pytest.raises(ConfigError, match="layer: global"):
        config.load(repo)


@pytest.mark.parametrize("sub", ["complexity", "file_lines"])
def test_subsection_must_be_mapping(repo, sub):
    _w(repo / "rig.yaml", f"version: 1\nlint_checks: {{{sub}: [1]}}\n")
    with pytest.raises(ConfigError, match="must be a mapping"):
        config.load(repo)


@pytest.mark.parametrize(
    ("exts", "message"),
    [
        ("[.json]", "leading dot"),
        ("[JSON]", "lowercase"),
        ("['']", "lowercase alphanumeric"),
        ("[a-b]", "lowercase alphanumeric"),
        ("[json, json]", "duplicate"),
        ("[1]", "lowercase alphanumeric"),
        ("json", "list"),
    ],
)
def test_exempt_extensions_rules(repo, exts, message):
    _w(repo / "rig.yaml", f"version: 1\nlint_checks: {{file_lines: {{exempt_extensions: {exts}}}}}\n")
    with pytest.raises(ConfigError, match="exempt_extensions") as exc:
        config.load(repo)
    assert message in str(exc.value) or message in exc.value.fix


def test_exempt_extensions_valid_and_empty(repo):
    _w(repo / "rig.yaml", "version: 1\nlint_checks: {file_lines: {exempt_extensions: [json, md5, d2]}}\n")
    assert _loaded(repo).exempt_extensions == ("json", "md5", "d2")
    _w(repo / "rig.yaml", "version: 1\nlint_checks: {file_lines: {exempt_extensions: []}}\n")
    assert _loaded(repo).exempt_extensions == ()


def test_allowed_in_both_layers_not_global_only(tmp_path, repo):
    _global(tmp_path, "version: 1\nlint_checks: {enabled: false}\n")
    _w(repo / "rig.yaml", "version: 1\nlint_checks: {enabled: true}\n")
    assert _loaded(repo).enabled is True


# ── schema / registry ────────────────────────────────────────────────────────────

def test_schema_file_in_sync_and_describes_block():
    assert config_schema.schema_file_in_sync()
    node = config_schema.json_schema()["properties"]["lint_checks"]
    cog = node["properties"]["complexity"]["properties"]["max_cognitive"]
    assert cog["minimum"] == 1 and cog["default"] == 15
    assert node["additionalProperties"] is False


def test_top_keys_in_sync_with_validator():
    assert "lint_checks" in config._VALID_TOP_KEYS
    assert "lint_checks" in config_schema.TOP_LEVEL_KEYS


def test_registry_exposes_every_option_with_hints_in_repo_layer():
    keys = {
        "lint_checks.enabled": True,
        "lint_checks.complexity.enabled": True,
        "lint_checks.complexity.max_cognitive": 15,
        "lint_checks.complexity.max_cyclomatic": 20,
        "lint_checks.file_lines.enabled": True,
        "lint_checks.file_lines.warn_above": 1200,
        "lint_checks.file_lines.error_above": 2400,
        "lint_checks.file_lines.exempt_extensions": ["json"],
    }
    by_key = {o.key: o for a in schema.AREAS for o in a.options}
    for key, default in keys.items():
        assert key in by_key, key
        assert by_key[key].default == default and by_key[key].hint
        assert by_key[key].layer == REPO  # `rig setup` writes it to the committed rig.yaml
    assert "lint_checks" not in schema.global_only_categories()


# ── config set / get ─────────────────────────────────────────────────────────────

def test_config_set_get_roundtrip_and_rollback(repo, capsys, fake_agent_tools):
    _w(repo / "rig.yaml", f"version: 1\nagent_tools_source: {fake_agent_tools}\n")
    assert main(["config", "set", "lint_checks.complexity.max_cognitive", "18", "-C", str(repo), "--no-apply"]) == 0
    assert config.load(repo).data["lint_checks"]["complexity"]["max_cognitive"] == 18
    capsys.readouterr()
    assert main(["config", "get", "lint_checks.complexity.max_cognitive", "-C", str(repo)]) == 0
    assert capsys.readouterr().out.strip() == "18"
    before = (repo / "rig.yaml").read_text()
    for bad in ("0", "true", "1.5"):
        assert main(["config", "set", "lint_checks.complexity.max_cognitive", bad, "-C", str(repo), "--no-apply"]) != 0
        assert (repo / "rig.yaml").read_text() == before
    assert main(["config", "set", "lint_checks.file_lines.warn_above", "9999", "-C", str(repo), "--no-apply"]) != 0
    assert (repo / "rig.yaml").read_text() == before


# ── lint CLI ─────────────────────────────────────────────────────────────────────

def test_lint_check_parser_attrs_match_contract(monkeypatch, tmp_path):
    seen = {}
    stub = types.ModuleType("riglib.lint_check")
    stub.run = lambda args: seen.update(vars(args)) or 7
    monkeypatch.setitem(sys.modules, "riglib.lint_check", stub)
    rc = main(["lint", "check", "a.ts", "b dir", "-C", str(tmp_path), "--config", "c.yaml", "--json", "--staged", "--fix"])
    assert rc == 7
    assert (seen["paths"], seen["cwd"], seen["config"]) == (["a.ts", "b dir"], str(tmp_path), "c.yaml")
    assert seen["json"] is True and seen["staged"] is True and seen["fix"] is True


def test_lint_check_defaults(monkeypatch):
    seen = {}
    stub = types.ModuleType("riglib.lint_check")
    stub.run = lambda args: seen.update(vars(args)) or 0
    monkeypatch.setitem(sys.modules, "riglib.lint_check", stub)
    assert main(["lint", "check"]) == 0
    assert (seen["paths"], seen["cwd"], seen["config"]) == ([], ".", None)
    assert not (seen["json"] or seen["staged"] or seen["fix"])


def test_lint_check_help_documents_flags_and_exit_codes(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["lint", "check", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for token in ("--staged", "--fix", "--json", "--config", "exit codes", "127", "rig apply commit", "3 "):
        assert token in out


def test_lint_bare_prints_help_and_rules_still_routes(capsys, repo):
    assert main(["lint"]) == 0
    out = capsys.readouterr().out
    assert "check" in out and "rules" in out
    assert main(["lint", "rules", "--json", "-C", str(repo)]) == 0
    assert capsys.readouterr().out.lstrip().startswith("[")


def test_status_layer_is_repo_and_global_editable():
    from riglib.layers import layer_for_category

    assert layer_for_category("lint_checks") == REPO
    assert "lint_checks" not in schema.global_only_categories()  # global config.yaml still accepts it
