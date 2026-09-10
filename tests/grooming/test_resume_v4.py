"""Focused regressions for the continuation path. No provider credentials/API writes."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import urllib.request

import pytest

MODULE = Path(__file__).parents[2] / "tools/grooming/resume_v4.py"
spec = importlib.util.spec_from_file_location("resume_v4", MODULE)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)


def pair(number=7, original="## Actual feature\nKeep all requirements.\n"):
    prior = dict(id=700+number, number=number, title="Feature contract", body=original, state="closed")
    row = dict(number=number, key=f"rig-cli#{number}", id=prior['id'], title=prior['title'],
               expected_sha256=m.digest(original), expected_state='closed',
               expected_updated_at='2026-09-10T10:00:00Z',
               body=m.START_V4+'\n\nEditorial prefix\n\n'+m.END_V4+'\n\n'+m.SCOPE_V4+'\n\n'+original,
               record={'quote_ids':['v4-202']})
    return row, prior


@pytest.mark.parametrize('value', ['rig-cli','alex-mextner/rig-cli','ultrabricks/rig-cli'])
def test_short_and_full_names_have_same_plan_key(value):
    assert m.repository_name(value)=='rig-cli'


@pytest.mark.parametrize('value', ['','../rig-cli','a/b/rig-cli','a%2Fb/rig-cli','random-tool','/rig-cli','a/rig-cli?ref=x'])
def test_invalid_repository_name_is_refused(value):
    with pytest.raises(m.ValidationError):m.repository_name(value)


def test_transfer_uses_stable_id_not_old_owner():
    metadata={'id':1269680459,'full_name':'ultrabricks/rig-cli'}
    assert m.canonical_repository('alex-mextner/rig-cli',metadata,'1269680459')=='ultrabricks/rig-cli'


@pytest.mark.parametrize('metadata,expected', [
    ({'id':1,'full_name':'ultrabricks/rig-cli'},'2'),
    ({'id':1,'full_name':'ultrabricks/review-cli'},'1'),
    ({'id':1,'full_name':'rig-cli'},'1'),
    ({'id':1,'full_name':'ultrabricks/rig-cli'},None),
])
def test_wrong_or_unproven_repository_identity_refused(metadata,expected):
    with pytest.raises(m.ValidationError):m.canonical_repository('rig-cli',metadata,expected)


def test_unmanaged_original_is_preserved_whole():
    assert m.required_fragments('## Feature\nOriginal source.')==['## Feature\nOriginal source.']


def test_v1_appended_user_notes_are_not_dropped():
    body=m.START_V1+'\nPrefix\n'+m.SCOPE_V1+'\nFeature\n'+m.END_V1+'\n## New user clarification\nKeep this.'
    assert m.required_fragments(body)==['Feature','## New user clarification\nKeep this.']


def test_v1_consolidated_findings_survive_prefix_replacement():
    body=m.START_V1+'\nPrefix\n'+m.SALVAGE+'\nUnique criteria from duplicate\n'+m.SCOPE_V1+'\nFeature\n'+m.END_V1
    assert m.required_fragments(body)==['Feature','Unique criteria from duplicate']


def test_v4_scope_heading_not_duplicated():
    row,_=pair()
    assert m.required_fragments(row['body'])==['## Actual feature\nKeep all requirements.']


def test_quoted_marker_not_used_as_structural_boundary():
    body=m.START_V1+'\n> '+m.END_V1+'\n'+m.SCOPE_V1+'\nFeature\n'+m.END_V1
    assert m.required_fragments(body)==['Feature']


def test_fenced_marker_not_used_as_structural_boundary():
    body=m.START_V1+'\n```text\n'+m.END_V1+'\n```\n'+m.SCOPE_V1+'\nFeature\n'+m.END_V1
    assert m.required_fragments(body)==['Feature']


@pytest.mark.parametrize('body',[
    m.START_V1+'\n'+m.SCOPE_V1+'\nNo end',
    m.START_V1+'\nNo source heading\n'+m.END_V1,
    m.START_V1+'\n'+m.SCOPE_V1+'\nFeature\n'+m.END_V1+'\n'+m.END_V1,
    m.START_V4+'\nNo end',
])
def test_malformed_managed_section_is_not_guessed(body):
    with pytest.raises(m.ValidationError):m.required_fragments(body)


def test_valid_plan_retains_own_source_and_quote():
    row,prior=pair()
    assert m.validate_plan([row],'ultrabricks/rig-cli',[prior],{'v4-202'})==[row]


@pytest.mark.parametrize('change', ['identity','body_hash','state','title','key','quote','missing_body','too_long','pull_request'])
def test_plan_tampering_or_unresolvable_data_refused(change):
    row,prior=pair()
    if change=='identity':row['id']=99
    if change=='body_hash':row['expected_sha256']='f'*64
    if change=='state':row['expected_state']='open'
    if change=='title':row['title']='another title'
    if change=='key':row['key']='tg-cli#7'
    if change=='quote':row['record']['quote_ids']=['v4-missing']
    if change=='missing_body':row['body']=None
    if change=='too_long':row['body']+='x'*64000
    if change=='pull_request':prior['pull_request']={}
    with pytest.raises(m.ValidationError):m.validate_plan([row],'rig-cli',[prior],{'v4-202'})


def test_duplicate_rows_refused():
    row,prior=pair()
    with pytest.raises(m.ValidationError):m.validate_plan([row,row],'rig-cli',[prior],{'v4-202'})


def test_old_v4_compiler_note_loss_is_caught_before_writes():
    original=m.START_V1+'\nPrefix\n'+m.SCOPE_V1+'\nFeature\n'+m.END_V1+'\nNew independent user requirement'
    row,prior=pair(original=original)
    row['body']=m.START_V4+'\nPrefix\n'+m.END_V4+'\n'+m.SCOPE_V4+'\nFeature\n'
    with pytest.raises(m.ValidationError,match='loses original'):m.validate_plan([row],'rig-cli',[prior],{'v4-202'})


def test_marker_alone_is_not_success():
    row,prior=pair()
    current={**prior,'body':m.START_V4+'\nSomeone else edited this'}
    assert m.assess(row,current)=='concurrent_edit_preserved'


def test_metadata_edits_are_preserved_even_with_exact_body():
    row,prior=pair()
    current={**prior,'body':row['body'],'state':'open'}
    assert m.assess(row,current)=='metadata_changed_after_application'


class FakeAPI:
    def __init__(self,priors,denied=False,uncertain=False,after_note=False):
        self.data={p['number']:copy.deepcopy(p) for p in priors}
        self.patches=[];self.reads=[];self.denied=denied;self.uncertain=uncertain;self.after_note=after_note
    def issue(self,number):
        self.reads.append(number)
        if self.denied:raise m.AccessDenied('forbidden')
        value=copy.deepcopy(self.data[number])
        if self.after_note and self.patches:value['body']+='\nHuman note after PATCH'
        return value
    def patch_body(self,number,body):
        self.patches.append((number,body));self.data[number]['body']=body
        if self.uncertain:raise m.DeliveryUncertain('lost reply after accepted write')
        return copy.deepcopy(self.data[number])


def test_dry_run_is_read_only_and_not_reported_as_applied(tmp_path):
    row,prior=pair();api=FakeAPI([prior])
    result=m.run_plan(api,[row],apply=False,journal=tmp_path/'out.json')
    assert api.patches==[] and result['writes']==0 and result['complete'] is False
    assert result['events'][0]['result']=='ready'


def test_apply_readbacks_and_preserves_closed_state(tmp_path):
    row,prior=pair();api=FakeAPI([prior])
    result=m.run_plan(api,[row],apply=True,journal=tmp_path/'out.json')
    assert api.reads==[7,7] and result['complete'] is True
    assert api.data[7]['state']=='closed' and result['writes']==1


def test_resume_is_exactly_idempotent(tmp_path):
    row,prior=pair();api=FakeAPI([prior])
    m.run_plan(api,[row],apply=True,journal=tmp_path/'out.json')
    result=m.run_plan(api,[row],apply=True,journal=tmp_path/'out.json')
    assert len(api.patches)==1 and result['writes']==0 and result['complete'] is True


def test_current_edit_conflict_is_not_overwritten(tmp_path):
    row,prior=pair();prior['body']+='\nA later human note';api=FakeAPI([prior])
    result=m.run_plan(api,[row],apply=True,journal=tmp_path/'out.json')
    assert not api.patches and result['remaining_or_conflicted']==1


def test_authorization_failure_stops_before_next_issue(tmp_path):
    a,pa=pair(1);b,pb=pair(2);api=FakeAPI([pa,pb],denied=True)
    result=m.run_plan(api,[a,b],apply=True,journal=tmp_path/'out.json')
    assert api.reads==[1] and result['fatal']=='authorization_denied'
    assert (tmp_path/'out.json').exists() and not api.patches


def test_uncertain_write_is_not_blindly_retried_and_resume_reads_it(tmp_path):
    row,prior=pair();api=FakeAPI([prior],uncertain=True)
    result=m.run_plan(api,[row],apply=True,journal=tmp_path/'out.json')
    assert len(api.patches)==1 and result['fatal']=='delivery_uncertain' and result['pending_write']
    api.uncertain=False
    result=m.run_plan(api,[row],apply=True,journal=tmp_path/'out.json')
    assert len(api.patches)==1 and result['complete']


def test_edit_after_patch_is_not_counted_as_exact(tmp_path):
    row,prior=pair();api=FakeAPI([prior],after_note=True)
    result=m.run_plan(api,[row],apply=True,journal=tmp_path/'out.json')
    assert not result['complete'] and result['events'][0]['result']=='postwrite_conflict'


def test_write_budget_does_not_fake_total_completion(tmp_path):
    a,pa=pair(1);b,pb=pair(2);api=FakeAPI([pa,pb])
    result=m.run_plan(api,[a,b],apply=True,journal=tmp_path/'out.json',max_writes=1)
    assert len(api.patches)==1 and not result['complete'] and result['remaining_or_conflicted']==1


def test_journal_written_atomically_and_contains_no_credentials(tmp_path):
    file=tmp_path/'journal.json';m.save_journal(file,{'events':[],'writes':0})
    assert json.loads(file.read_text())=={'events':[],'writes':0}
    assert list(tmp_path.iterdir())==[file]
    assert file.stat().st_mode&0o777==0o600


@pytest.mark.parametrize('url',['http://api.github.com/repos/x/y','https://evil.example/files','https://api.github.com.evil.example/a'])
def test_cross_origin_credentials_never_forwarded(url):
    req=urllib.request.Request('https://api.github.com/repos/old/rig-cli',headers={'Authorization':'Bearer fake'})
    with pytest.raises(m.AccessDenied):m.SameOriginRedirect().redirect_request(req,None,302,'Found',{},url)


def test_same_origin_rename_redirect_is_allowed():
    req=urllib.request.Request('https://api.github.com/repos/old/rig-cli',headers={'Authorization':'Bearer fake'})
    redirected=m.SameOriginRedirect().redirect_request(req,None,301,'Moved',{},'https://api.github.com/repos/ultrabricks/rig-cli')
    assert redirected.get_header('Authorization')=='Bearer fake'


@pytest.mark.parametrize('interval',[0,-1,0.5])
def test_noncompliant_write_pacing_refused(interval):
    with pytest.raises(m.ValidationError):m.GitHubAPI('ultrabricks/rig-cli','unused',interval)


@pytest.mark.parametrize('invalid_id', [None, True, 0, -1, '707'])
def test_plan_id_must_be_a_positive_integer(invalid_id):
    row,prior=pair();row['id']=prior['id']=invalid_id
    with pytest.raises(m.ValidationError):m.validate_plan([row],'rig-cli',[prior],{'v4-202'})


@pytest.mark.parametrize('changes', [{'title':''}, {'title':None}, {'expected_state':'accepted'}])
def test_invalid_plan_metadata_is_rejected(changes):
    row,prior=pair();row.update(changes)
    prior['title']=row['title'];prior['state']=row['expected_state']
    with pytest.raises(m.ValidationError):m.validate_plan([row],'rig-cli',[prior],{'v4-202'})


def test_startup_validation_failure_has_journal(tmp_path):
    report=tmp_path/'report.json'
    code=m.main(['--repository','ultrabricks/rig-cli','--source-ref','main','--journal',str(report)])
    result=json.loads(report.read_text())
    assert code==2 and result['fatal']=='ValidationError' and result['writes']==0


def test_missing_plan_is_not_misrepresented_as_empty_completion(tmp_path,monkeypatch):
    report=tmp_path/'report.json'
    monkeypatch.setattr(m,'verify_checkout',lambda *a:None)
    def fail(*args):raise FileNotFoundError('private details')
    monkeypatch.setattr(m,'committed_bytes',fail)
    code=m.main(['--repository','ultrabricks/harness-cli','--source-ref','a'*40,'--journal',str(report)])
    result=json.loads(report.read_text())
    assert code==2 and result['fatal']=='FileNotFoundError' and not result['complete']
    assert 'private details' not in report.read_text()


def test_modified_runner_does_not_execute_as_immutable(monkeypatch):
    root=MODULE.parents[2]
    monkeypatch.setattr(m.subprocess,'check_output',lambda *a,**k:'a'*40)
    monkeypatch.setattr(m,'committed_bytes',lambda *a:b'not this executable')
    with pytest.raises(m.ValidationError,match='differs from the executable'):m.verify_checkout(root,'a'*40)


def test_startup_denied_records_no_secret_response(tmp_path,monkeypatch):
    row,prior=pair()
    monkeypatch.setattr(m,'verify_checkout',lambda *a:None)
    def data(root,ref,path):
        return json.dumps([row] if '/plans/' in path else [prior] if '/before/' in path else {'records':[{'id':'v4-202'}]}).encode()
    monkeypatch.setattr(m,'committed_bytes',data)
    monkeypatch.setenv('GH_TOKEN','fake-test-token')
    def deny(*a):raise m.AccessDenied('SENSITIVE_RESPONSE_BODY')
    monkeypatch.setattr(m.GitHubAPI,'request',deny)
    report=tmp_path/'report.json'
    code=m.main(['--repository','ultrabricks/rig-cli','--source-ref','a'*40,'--journal',str(report)])
    assert code==2 and json.loads(report.read_text())['fatal']=='AccessDenied'
    assert 'SENSITIVE' not in report.read_text() and 'fake-test-token' not in report.read_text()
