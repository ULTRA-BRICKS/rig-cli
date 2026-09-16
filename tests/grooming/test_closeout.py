"""Contract tests; no network and no real tracker writes."""
import copy,importlib.util,json,pathlib,sys
import pytest
ROOT=pathlib.Path(__file__).parents[2]
sys.path.insert(0,str(ROOT/'tools/grooming'))
import resume_v4 as core
import closeout_apply as apply
import closeout_compile as compiler


def row(n=7):
 prior={'id':100+n,'number':n,'title':'Keep source','state':'open','body':'Original requirement.'}
 plan={'id':prior['id'],'number':n,'key':f'rig-cli#{n}','title':prior['title'],'expected_state':'open','expected_sha256':core.digest(prior['body']),'body':core.START_V4+'\nCorrected introduction\n'+core.END_V4+'\n'+core.SCOPE_V4+'\nOriginal requirement.','record':{'quote_ids':[]}}
 return plan,prior

class Fake:
 def __init__(self,prior,uncertain=False,denied=False,post_edit=False):
  self.data={i['number']:copy.deepcopy(i) for i in prior};self.writes=[];self.uncertain=uncertain;self.denied=denied;self.post_edit=post_edit;self.all_reads=0
 def issue(self,n):
  if self.denied:raise core.AccessDenied('never print credentials')
  return copy.deepcopy(self.data[n])
 def patch_body(self,n,body):
  self.data[n]['body']=body;self.writes.append(n)
  if self.uncertain:raise core.DeliveryUncertain('response lost')
  return copy.deepcopy(self.data[n])
 def all_issues(self):
  self.all_reads+=1;data=copy.deepcopy(list(self.data.values()))
  if self.post_edit:data[0]['body']+='\nConcurrent edit.'
  return data

def test_bulk_receipts_not_counted_until_independent_readback(tmp_path):
 a,b=row();fake=Fake([b]);result=apply.apply_rows(fake,[a],tmp_path/'r.json',{},True)
 assert fake.writes==[7] and fake.all_reads==1 and result['verified_exact']==1 and result['complete']

def test_dry_run_never_writes_or_claims_applied(tmp_path):
 a,b=row();fake=Fake([b]);result=apply.apply_rows(fake,[a],tmp_path/'r.json',{},False)
 assert not fake.writes and not result['complete'] and result['writes']==0

def test_current_human_edit_is_retained(tmp_path):
 a,b=row();b['body']+='\nHuman note';fake=Fake([b]);result=apply.apply_rows(fake,[a],tmp_path/'r.json',{},True)
 assert not fake.writes and result['remaining_or_conflicted']==1

def test_postwrite_edit_not_claimed_exact(tmp_path):
 a,b=row();fake=Fake([b],post_edit=True);result=apply.apply_rows(fake,[a],tmp_path/'r.json',{},True)
 assert not result['complete'] and result['verified_exact']==0

def test_marker_only_does_not_prevent_factual_conflict_reporting(tmp_path):
 a,b=row();b['body']=core.START_V4+'\nDifferent work';fake=Fake([b]);result=apply.apply_rows(fake,[a],tmp_path/'r.json',{},True)
 assert result['events'][0]['result']=='concurrent_edit_preserved' and not fake.writes

def test_resume_does_not_repeat_successful_write(tmp_path):
 a,b=row();fake=Fake([b]);apply.apply_rows(fake,[a],tmp_path/'r.json',{},True)
 result=apply.apply_rows(fake,[a],tmp_path/'r.json',{},True)
 assert fake.writes==[7] and result['writes']==0 and result['complete']

def test_access_denial_stops_and_does_not_probe_another_api(tmp_path):
 a,b=row();fake=Fake([b],denied=True);result=apply.apply_rows(fake,[a],tmp_path/'r.json',{},True)
 assert result['fatal']=='authorization-denied' and fake.all_reads==0 and not fake.writes

def test_unknown_write_not_blindly_retried(tmp_path):
 a,b=row();fake=Fake([b],uncertain=True);result=apply.apply_rows(fake,[a],tmp_path/'r.json',{},True)
 assert fake.writes==[7] and result['fatal']=='delivery-uncertain'
 assert result['verified_exact']==1 and not result['complete']
 fake.uncertain=False
 result=apply.apply_rows(fake,[a],tmp_path/'r.json',{},True)
 assert fake.writes==[7] and result['complete']

def test_state_and_title_unchanged(tmp_path):
 a,b=row();b['state']='closed';a['expected_state']='closed';fake=Fake([b])
 apply.apply_rows(fake,[a],tmp_path/'r.json',{},True)
 assert fake.data[7]['state']=='closed' and fake.data[7]['title']=='Keep source'

@pytest.mark.parametrize('text', ['random #18','https://github.com/unrelated/harness-cli/issues/18','harness-cli#188','https://github.com/ultrabricks/harness-cli/pull/18'])
def test_no_fabricated_explicit_reference(text):
 assert ('harness-cli',18) not in compiler.issue_references(text,{'harness-cli'})

def test_explicit_source_links_normalize_owner_without_new_dependencies():
 text='https://github.com/alex-mextner/harness-cli/issues/18 and harness-cli#19'
 assert compiler.issue_references(text,{'harness-cli'})=={('harness-cli',18),('harness-cli',19)}

def test_shared_requirement_does_not_claim_feature_original():
 q={'id':'v4-227','edited':'Одна модель должна заполнять все роли.','source_kind':'source-attributed','context_incomplete':False}
 text=compiler.quote_block(q,'review-cli#444','shared-requirement-context')
 assert 'не доказательство происхождения' in text and 'Полный оригинал' in text

def test_manual_exceptions_are_explicit_and_not_dropped():
 assert ('harness-cli',18) in compiler.MANUAL and ('tg-cli',118) in compiler.MANUAL

def test_process_review_is_not_a_new_feature():
 assert compiler.is_process('Review uncommitted version bump',{})
 assert not compiler.is_process('Support account-aware fallback',{})
