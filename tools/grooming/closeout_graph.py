#!/usr/bin/env python3
"""Apply the explicitly reviewed relationship map within the caller repository.

Descriptions are never interpreted as commands. Existing parents are preserved;
only missing parents are filled. Dependencies are explicit, not keyword matches.
Read every resulting relationship independently before reporting it as applied.
"""
from __future__ import annotations
import datetime,json,os,pathlib,time,urllib.request,urllib.error
from resume_v4 import SameOriginRedirect,save_journal

ROOTS={'rig-cli':522,'agent-tools':725,'review-cli':498,'harness-cli':30,'tg-cli':382,'task-cli':185,'qa-cli':13,'stt-cli':42,'research-cli':19,'dev-cli':30,'pm-cli':29}
PARENTS={
 'harness-cli':{22:30,26:30,1:26,2:30,5:26,6:30,8:26,9:26,11:26,12:26,14:30,15:30,17:26,18:26,19:30,21:26,24:26,25:30,28:22,29:22},
 'agent-tools':{742:718,743:734,744:733},
 'tg-cli':{398:380,399:380},
 'rig-cli':{538:531},
}
DEPENDENCIES={
 'review-cli':[(494,'review-cli',493),(496,'review-cli',495)],
 'agent-tools':[(705,'review-cli',472),(573,'agent-tools',712),(573,'agent-tools',673)],
 'rig-cli':[(517,'rig-cli',516),(519,'rig-cli',518)],
 'tg-cli':[(377,'harness-cli',28),(379,'harness-cli',29)],
}
JOURNAL=pathlib.Path('closeout-graph/receipt.json')

class Stop(RuntimeError):pass

class API:
 def __init__(self,full):self.full=full;self.token=os.environ['GH_TOKEN'];self.opener=urllib.request.build_opener(SameOriginRedirect());self.last=0
 def call(self,path,method='GET',data=None,optional=False):
  if path!='/graphql' and not path.startswith('/repos/ultrabricks/'):raise ValueError('Out-of-scope graph read')
  if method!='GET' and path!='/graphql':
   if not path.startswith('/repos/'+self.full+'/'):raise ValueError('Writes are scoped to caller')
   time.sleep(max(0,3+self.last-time.monotonic()))
  request=urllib.request.Request('https://api.github.com'+path,method=method,data=None if data is None else json.dumps(data).encode(),headers={'Authorization':'Bearer '+self.token,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28','User-Agent':'rig-reviewed-graph'})
  try:
   with self.opener.open(request,timeout=60) as r:raw=r.read()
   if method!='GET' and path!='/graphql':self.last=time.monotonic()
   return json.loads(raw) if raw else None
  except urllib.error.HTTPError as e:
   if optional and e.code==404:return None
   if e.code in (401,403,429):raise Stop('Authorization or rate limit: HTTP '+str(e.code)) from e
   raise RuntimeError('Graph request HTTP '+str(e.code)) from e
 def pages(self,path):
  out=[];page=1
  while True:
   part=self.call(path+('&' if '?' in path else '?')+'per_page=100&page='+str(page));out+=part
   if len(part)<100:return out
   page+=1
 def parents(self,issues):
  result={}
  for i in range(0,len(issues),40):
   query='query($ids:[ID!]!){nodes(ids:$ids){... on Issue{id number parent{id number url repository{nameWithOwner}}}}}'
   data=self.call('/graphql','POST',{'query':query,'variables':{'ids':[r['node_id'] for r in issues[i:i+40]]}})
   if data.get('errors'):raise RuntimeError('Parent query failed')
   for node in data['data']['nodes']:
    if node:result[str(node['number'])]=node.get('parent')
  return result


def main():
 full=os.environ['GITHUB_REPOSITORY'];repo=full.split('/')[-1]
 if full!='ultrabricks/'+repo or repo not in ROOTS:raise ValueError('Unexpected caller')
 api=API(full);meta=api.call('/repos/'+full)
 if str(meta['id'])!=os.environ['GITHUB_REPOSITORY_ID']:raise ValueError('Stable repository mismatch')
 report={'repository':full,'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'events':[],'issues_changed':False,'replace_parent':False,'keyword_relationships_used':0}
 def event(**data):
  report['events'].append(data);save_journal(JOURNAL,report);print(json.dumps(data),flush=True)
 issues=[i for i in api.pages('/repos/'+full+'/issues?state=all') if 'pull_request' not in i]
 original=api.parents(issues);report['parents_before']=original;save_journal(JOURNAL,report)
 desired=[(repo,child,parent) for child,parent in PARENTS.get(repo,{}).items()]
 if repo=='rig-cli':desired.extend((child_repo,n,521) for child_repo,n in ROOTS.items())
 try:
  for child_repo,number,parent_number in desired:
   if child_repo==repo and number==parent_number:raise ValueError('Self parent')
   child_path='/repos/ultrabricks/'+child_repo+'/issues/'+str(number)
   current_parent=api.call(child_path+'/parent',optional=True)
   expected='https://github.com/'+full+'/issues/'+str(parent_number)
   if current_parent:
    event(kind='parent',child=child_repo+'#'+str(number),result='already-correct' if current_parent['html_url']==expected else 'existing-parent-preserved',actual_parent=current_parent['html_url'],intended_parent=expected);continue
   child=api.call(child_path);parent=api.call('/repos/'+full+'/issues/'+str(parent_number))
   if 'pull_request' in child or 'pull_request' in parent:raise ValueError('Issue/PR identity mismatch')
   try:api.call('/repos/'+full+'/issues/'+str(parent_number)+'/sub_issues','POST',{'sub_issue_id':child['id'],'replace_parent':False})
   except RuntimeError as exc:
    if isinstance(exc,Stop):raise
    event(kind='parent',child=child_repo+'#'+str(number),result='write-not-confirmed',reason=str(exc));continue
   readback=api.call(child_path+'/parent',optional=True)
   event(kind='parent',child=child_repo+'#'+str(number),result='linked-readback-verified' if readback and readback['id']==parent['id'] else 'readback-gap',actual_parent=readback['html_url'] if readback else None)
  for blocked,other,blocking in DEPENDENCIES.get(repo,[]):
   target=api.call('/repos/ultrabricks/'+other+'/issues/'+str(blocking))
   path='/repos/'+full+'/issues/'+str(blocked)+'/dependencies/blocked_by'
   current=api.pages(path)
   if not any(i['id']==target['id'] for i in current):
    try:api.call(path,'POST',{'issue_id':target['id']})
    except RuntimeError as exc:
     if isinstance(exc,Stop):raise
     event(kind='dependency',blocked=repo+'#'+str(blocked),blocking=other+'#'+str(blocking),result='write-not-confirmed',reason=str(exc));continue
   final=api.pages(path)
   event(kind='dependency',blocked=repo+'#'+str(blocked),blocking=other+'#'+str(blocking),result='native-readback-verified' if any(i['id']==target['id'] for i in final) else 'readback-gap')
 except Stop as exc:
  report['stopped']=str(exc);save_journal(JOURNAL,report)
 # Record all actual parent links, not only intended writes. No raw issue bodies.
 if 'stopped' not in report:
  report['parents_after']=api.parents(issues)
  report['native_parent_count']=sum(bool(p) for p in report['parents_after'].values())
 report['finished_at']=datetime.datetime.now(datetime.timezone.utc).isoformat()
 report['complete']=('stopped' not in report and all(e['result'] in ('already-correct','linked-readback-verified','native-readback-verified','existing-parent-preserved') for e in report['events']))
 save_journal(JOURNAL,report)
 if not report['complete']:raise SystemExit('Relationship gaps retained in receipt')

if __name__=='__main__':main()
