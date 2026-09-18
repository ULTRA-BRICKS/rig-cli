"""Rig's machine policy must be visible, typed, and independent of PR-local YAML."""
import json

import pytest

from riglib import config, config_schema, schema
from riglib.layers import GLOBAL


def document(required=False, owners=None):
    return {'ci': {'items': {'ship': {'require_external_review': required, 'external_review_owners': ['alex-mextner'] if owners is None else owners}}}}


@pytest.mark.parametrize('required', [False, True])
def test_boolean_machine_policy_is_valid(required):
    config.validate(document(required))


@pytest.mark.parametrize('required', ['false', 0, None, [], {}])
def test_ambiguous_boolean_is_rejected(required):
    with pytest.raises(config.ConfigError, match='require_external_review'):
        config.validate(document(required))


@pytest.mark.parametrize('owners', ['alex-mextner', ['*'], ['bad/owner'], [3], ['-invalid'], ['a' * 40]])
def test_invalid_owner_scope_rejected(owners):
    with pytest.raises(config.ConfigError, match='external_review_owners'):
        config.validate(document(owners=owners))


def test_policy_options_are_written_to_machine_layer():
    for key in ('require_external_review', 'external_review_owners'):
        option = schema.option_for_key('ci.items.ship.' + key)
        assert option is not None
        assert option.layer == GLOBAL


def test_one_machine_policy_applies_to_two_repositories(tmp_path, monkeypatch):
    global_dir = tmp_path / 'machine'
    monkeypatch.setenv('XDG_CONFIG_HOME', str(global_dir))
    path = config.global_config_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(document()))
    for name in ('calendar', 'another-project'):
        repo = tmp_path / name
        repo.mkdir()
        (repo / 'rig.yaml').write_text('version: 1\nci: {enabled: true}\n')
        loaded = config.load(repo)
        assert loaded.data['ci']['items']['ship']['require_external_review'] is False
        assert loaded.data['ci']['items']['ship']['external_review_owners'] == ['alex-mextner']


def test_pr_local_policy_is_not_machine_authority(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'no-global'))
    (tmp_path / 'rig.yaml').write_text(json.dumps(document()))
    with pytest.raises(config.ConfigError, match='global-only'):
        config.load(tmp_path)


def test_editor_schema_exposes_machine_policy_types():
    ci = config_schema.json_schema()['properties']['ci']['properties']['items']
    ship = ci['properties']['ship']['properties']
    assert ship['require_external_review']['type'] == 'boolean'
    assert ship['require_external_review']['default'] is True
    assert ship['external_review_owners']['type'] == 'array'
    assert ship['external_review_owners']['items']['type'] == 'string'


def test_legacy_boolean_ship_toggle_remains_accepted():
    config.validate({'ci': {'items': {'ship': False}}})
    node = config_schema.json_schema()['properties']['ci']['properties']['items']['properties']['ship']
    assert 'boolean' in node['type']
