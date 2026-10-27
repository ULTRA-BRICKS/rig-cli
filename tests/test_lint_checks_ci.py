"""``lint_checks.ci.rig_source``: the only accepted CI install source is an immutable git commit URL."""

from __future__ import annotations

import re

import pytest
from lint_checks_helpers import RIG_SOURCE

from riglib import config, config_schema, lint_checks_ci, lint_checks_config, schema

GOOD = [
    RIG_SOURCE,
    "git+https://git.hyperide.ai/ultrabricks/rig-cli@" + "0123456789abcdef" * 2 + "01234567",
    "git+https://git.hyperide.ai:8443/ultrabricks/rig-cli.git@" + "f" * 40,
]
BAD = [
    "git+https://git.example.test/org/rig-cli@main",
    "git+https://git.example.test/org/rig-cli@" + "a" * 64,  # sha256 object ids are not accepted
    "git+https://git.example.test/org/rig-cli@" + "a" * 40 + "\n",  # `$` would accept a trailing newline
    "git+https://git.example.test/org/rig-cli@" + "a" * 40 + "#subdirectory=x",
    "git+ssh://git.example.test/org/rig-cli@" + "a" * 40,
    "hyper-rig==0.69.0",
    "https://git.example.test/org/rig-cli@" + "a" * 40,
    "git+https://git.example.test/../rig-cli@" + "a" * 40,  # a `..` segment escapes the repo path
]


@pytest.mark.parametrize("value", GOOD)
def test_immutable_commit_urls_are_accepted_and_resolved(value):
    lint_checks_config.validate_block({"ci": {"rig_source": value}}, layer="repo")
    assert lint_checks_ci.rig_source({"lint_checks": {"ci": {"rig_source": value}}}) == value
    lint_checks_config.resolve({"lint_checks": {"ci": {"rig_source": value}}})  # does not disturb the other knobs


@pytest.mark.parametrize("value", BAD)
def test_everything_else_is_rejected_naming_key_and_layer(value):
    with pytest.raises(config.ConfigError) as err:
        lint_checks_config.validate_block({"ci": {"rig_source": value}}, layer="global")
    text = str(err.value)
    assert "lint_checks.ci.rig_source" in text and "layer: global" in text


def test_unknown_ci_key_and_non_mapping_are_rejected():
    with pytest.raises(config.ConfigError, match=r"unknown lint_checks\.ci key: branch .*layer: repo"):
        lint_checks_config.validate_block({"ci": {"branch": "main"}}, layer="repo")
    with pytest.raises(config.ConfigError, match=r"lint_checks\.ci must be a mapping"):
        lint_checks_config.validate_block({"ci": "git+https://x/y@" + "a" * 40}, layer="repo")


def test_unset_means_no_source():
    assert lint_checks_ci.rig_source({}) is None
    assert lint_checks_ci.rig_source({"lint_checks": {"enabled": True}}) is None
    assert lint_checks_ci.rig_source({"lint_checks": {"ci": {}}}) is None


def test_the_json_schema_pattern_accepts_and_rejects_the_same_shapes():
    node = config_schema.json_schema()["properties"]["lint_checks"]["properties"]["ci"]["properties"]["rig_source"]
    assert node["type"] == "string" and node["pattern"] == lint_checks_ci.SCHEMA_PATTERN
    rx = re.compile(node["pattern"])
    assert all(rx.search(v) for v in GOOD)
    assert not any(rx.search(v) for v in BAD if not v.endswith("\n"))  # JSON-schema `$` semantics differ only on a trailing LF


def test_the_key_is_in_the_wizard_registry():
    keys = {o.key for area in schema.AREAS for o in area.options}
    assert "lint_checks.ci.rig_source" in keys
