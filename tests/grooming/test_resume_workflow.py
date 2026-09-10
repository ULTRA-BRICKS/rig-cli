"""Validate the workflow integration contract without using Actions."""
from pathlib import Path
import re
import yaml

FILE=Path(__file__).parents[2]/'.github/workflows/v4-resume-reviewed.yml'

def workflow():
    return yaml.load(FILE.read_text(),Loader=yaml.BaseLoader)

def test_no_implicit_push_or_timer_trigger():
    assert set(workflow()['on'])=={'workflow_dispatch','workflow_call'}

def test_dry_run_default_on_both_entry_points():
    for mode in workflow()['on'].values():
        assert mode['inputs']['apply']['default']=='false'
        assert mode['inputs']['source_ref']['required']=='true'

def test_actions_are_immutable_refs():
    for step in workflow()['jobs']['resume']['steps']:
        if 'uses' in step:assert re.search(r'@[a-f0-9]{40}$',step['uses'])

def test_code_and_data_are_same_explicit_commit():
    checkout=next(s for s in workflow()['jobs']['resume']['steps'] if 'checkout@' in s.get('uses',''))
    assert checkout['with']['ref']=='${{ inputs.source_ref }}'
    assert checkout['with']['repository']=='ultrabricks/rig-cli'
    assert checkout['with']['persist-credentials']=='false'

def test_mutator_receives_full_repo_and_explicit_apply_not_python_introspection():
    step=next(s for s in workflow()['jobs']['resume']['steps'] if 'GH_TOKEN' in s.get('env',{}))
    assert '--repository "$GITHUB_REPOSITORY"' in step['run']
    assert 'args+=(--apply)' in step['run']
    assert 'invoke_apply' not in step['run'] and 'publication.json' not in step['run']

def test_no_permission_changes_or_contents_write():
    assert workflow()['permissions']=={'contents':'read','issues':'write'}

def test_actual_journal_only_is_uploaded_even_on_failure():
    artifact=next(s for s in workflow()['jobs']['resume']['steps'] if 'upload-artifact@' in s.get('uses',''))
    assert artifact['if']=='always()'
    assert artifact['with']['path']=='grooming-v4-results/resume.json'

def test_caller_input_not_directly_interpolated_into_shell():
    for s in workflow()['jobs']['resume']['steps']:
        if 'run' in s:assert '${{ inputs.' not in s['run']
