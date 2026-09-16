#!/usr/bin/env python3
"""Apply the user's explicit all-issue grooming mandate, to ONE authorized repo.

Inputs are immutable reviewed JSON, never commands from tickets. Existing bodies
are read before writing; concurrent body/title/state changes are not overwritten.
The only closure decisions are explicit canonical mappings in the reviewed plan.
No merge, branch deletion, permission change or implementation acceptance occurs.
"""
from __future__ import annotations
import argparse,base64,collections,datetime,hashlib,json,os,pathlib,re,time,urllib.error,urllib.parse,urllib.request
ALLOWED={'rig-cli','agent-tools','review-cli','task-cli','tg-cli','qa-cli','draw-cli','stt-cli','research-cli','dev-cli','pm-cli'}
MARK='<!-- rig-grooming:2026-09-10:v1 -->'
PARENT_MARK='<!-- rig-grooming-parent:2026-09-10:'
CENTRAL='alex-mextner/rig-cli'
ROOT='https://github.com/alex-mextner/rig-cli/issues/521'
DOC='https://github.com/alex-mextner/rig-cli/blob/docs/full-grooming-2026-09-10/'
TOPICS={'intake':'Task intake and acceptance evidence','worktrees':'Worktree isolation and preservation','identity':'Native identity and guard parity','shipping':'Shipping, local CI and releases','roles':'Review roles and independent routes','health':'Account health and quota','execution':'Execution, progress and cleanup','telegram':'Telegram routing and decisions','resources':'Resource admission and services','telemetry':'Telemetry and dashboards','specs':'Specifications and documentation','provisioning':'Provisioning and drift','testing':'Testing and platform acceptance','auxiliary':'Auxiliary product workflows','process':'Historical process records'}
QUOTE='Выполни полный груминг всех задач: консолидацию, связи, исправление и улучшение описаний. Добавь мои исходные запросы цитатами с исправлением стилистики, орфографии и пунктуации. Всё влитое и сделанное нужно перепроверить.'
# Preserve the already established product workstreams; the new hierarchy is an
# acceptance-recovery index, not a replacement implementation epic for each bug.
EXISTING={
 'rig-cli':{510:[515,516,517,518,519,512]},
 'agent-tools':{718:[573,692,693,713,702,705,707,701],573:[712,673]},
 'review-cli':{492:[472,479,460,486,476,483,480,469,468,467,465],484:[493,494],487:[495,496]},
 'tg-cli':{380:[377,360,357,379,373,374,354,375,356,344]},
}

class API:
    def __init__(self, token):self.token=token;self.writes=0;self.reads=0;self.last_write=0.
    def call(self,path,method='GET',data=None):
        if not path.startswith('/repos/') and path!='/graphql':raise ValueError('Unexpected API family')
        encoded=None if data is None else json.dumps(data,ensure_ascii=False).encode()
        for attempt in range(6):
            if method not in ('GET','HEAD') and path!='/graphql':
                time.sleep(max(0,1.1-(time.monotonic()-self.last_write)))
            req=urllib.request.Request('https://api.github.com'+path,data=encoded,method=method,headers={'Authorization':'Bearer '+self.token,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28','User-Agent':'rig-user-authorized-grooming'})
            try:
                with urllib.request.urlopen(req,timeout=60) as response:
                    raw=response.read();obj=json.loads(raw) if raw else None
                if method=='GET' or path=='/graphql':self.reads+=1
                else:self.writes+=1;self.last_write=time.monotonic()
                return obj
            except urllib.error.HTTPError as exc:
                detail=exc.read().decode(errors='replace')
                if exc.code in (403,429,500,502,503,504) and attempt<5:
                    wait=int(exc.headers.get('Retry-After','0') or 0)
                    if exc.code==403 and not wait and 'rate limit' not in detail.lower():raise RuntimeError(f'{method} {path}: {exc.code} {detail[:350]}')
                    if not wait and exc.headers.get('X-RateLimit-Remaining')=='0':wait=max(1,int(exc.headers.get('X-RateLimit-Reset','0'))-int(time.time()))
                    wait=max(wait,5*(attempt+1))
                    if wait>900:raise RuntimeError(f'Rate limit requires {wait}s; checkpoint retained, no bypass')
                    print('API retry',exc.code,'after',wait,'seconds',flush=True);time.sleep(wait);continue
                raise RuntimeError(f'{method} {path}: {exc.code} {detail[:500]}') from exc
        raise RuntimeError('API retries exhausted')
    def pages(self,path):
        rows=[];p=1
        while True:
            batch=self.call(path+('&' if '?' in path else '?')+'per_page=100&page='+str(p));rows+=batch
            if len(batch)<100:return rows
            p+=1


def h(s):return hashlib.sha256((s or '').encode()).hexdigest()

def get_plan(api,repo,ref):
    if not re.fullmatch('[0-9a-f]{40}',ref):raise ValueError('Plan must be pinned to an immutable commit')
    obj=api.call('/repos/'+CENTRAL+'/contents/docs/grooming/full-2026-09-10/plans/'+repo+'.json?ref='+ref)
    raw=base64.b64decode(obj['content']);rows=json.loads(raw)
    if len({r['number'] for r in rows})!=len(rows):raise ValueError('Duplicate plan numbers')
    for r in rows:
        if r['key']!=repo+'#'+str(r['number']):raise ValueError('Cross-repository plan entry')
        if r['body'] is not None and (not r['body'].startswith(MARK) or len(r['body'])>64000):raise ValueError('Invalid plan body')
    return rows,h(raw.decode())


def parents(api,ids):
    result={}
    for i in range(0,len(ids),40):
        batch=ids[i:i+40]
        obj=api.call('/graphql','POST',{'query':'query($ids:[ID!]!){nodes(ids:$ids){... on Issue{id number parent{id number url}}}}','variables':{'ids':batch}})
        if obj.get('errors'):raise RuntimeError('Parent read failed: '+str(obj['errors'])[:400])
        for node in obj['data']['nodes']:
            if node:result[node['id']]=node.get('parent')
    return result


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--repository',required=True);parser.add_argument('--plan-ref',required=True);parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    full=args.repository;owner,repo=full.split('/')
    if owner!='alex-mextner' or repo not in ALLOWED or os.environ.get('GITHUB_REPOSITORY',full)!=full:raise SystemExit('Repository scope mismatch')
    api=API(os.environ['GH_TOKEN']);plan,digest=get_plan(api,repo,args.plan_ref)
    local=pathlib.Path('grooming-results');local.mkdir(exist_ok=True)
    result={'repository':full,'plan_ref':args.plan_ref,'plan_sha256':digest,'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'mode':'apply' if args.apply else 'dry-run','events':[],'parents':{},'newly_discovered':[]}
    def checkpoint():
        result['counts']=dict(collections.Counter(e['result'] for e in result['events']));result['api_reads']=api.reads;result['api_writes']=api.writes
        (local/'applied.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    def event(number,status,**extra):
        result['events'].append(dict(number=number,result=status,**extra));checkpoint();print(number,status,flush=True)
    raw=api.pages('/repos/'+full+'/issues?state=all&sort=created&direction=asc')
    current={r['number']:r for r in raw if 'pull_request' not in r}
    planned={r['number'] for r in plan}
    result['newly_discovered']=[{'number':n,'title':r['title'],'state':r['state'],'url':r['html_url']} for n,r in current.items() if n not in planned]
    if not args.apply:checkpoint();print('Dry run:',len(plan),'planned issues');return
    # Additive labels only. Existing status, owner and unrelated labels are retained.
    have_labels={r['name'] for r in api.pages('/repos/'+full+'/labels?')}
    definitions={'grooming:2026-09-10':('c6ded4','Source-grounded recovery audit; not functional acceptance'),'verification:required':('e9d8ae','Outcome-specific verification remains separate from merge'),'record:process':('d9dce2','Agent process/review record, not a product feature'),'source:gap':('e8c4ad','Original feature request not unambiguously recovered'),'epic:recovery':('9cc5b7','Recovery acceptance hierarchy, not a delivered feature')}
    for name,(color,description) in definitions.items():
        if name not in have_labels:api.call('/repos/'+full+'/labels','POST',{'name':name,'color':color,'description':description})
    def parent_issue(marker,title,body):
        matches=[r for r in current.values() if marker in (r.get('body') or '')]
        if len(matches)>1:raise RuntimeError('Ambiguous recovery parent marker')
        if matches:return matches[0]
        made=api.call('/repos/'+full+'/issues','POST',{'title':title,'body':marker+'\n\n'+body,'labels':['epic:recovery','grooming:2026-09-10']})
        current[made['number']]=made;event(made['number'],'parent-created',url=made['html_url']);return made
    root_body='## User request — editorial quotation\n\n> '+QUOTE+'\n\nSource: current ChatGPT request, 2026-09-10. This is the recovery mandate, not the original feature request for every historical ticket. Original wording and linked source quotations are retained in the central registry.\n\n## Outcome\nEvery issue in '+full+' has an explicit, source-grounded disposition, preserved technical scope, specification links and a verification status. Existing product epics remain owners of implementation; this epic indexes recovery/acceptance, not duplicate development.\n\nCoordinator: '+ROOT+'\nSpecification catalog: '+DOC+'docs/specs/recovery/\nInventory and evidence: '+DOC+'docs/grooming/full-2026-09-10/issues/'+repo+'.json\n\n## Acceptance\n- [ ] All original outcomes are verified or explicitly rescoped by the owner; a merge or a copied screenshot path is not acceptance.\n- [ ] Native parent links and preserved prior relationships are reconciled.\n- [ ] Source gaps and unsupported/live-platform checks remain visible.\n- [ ] Duplicate consolidation preserves every unique finding before closure.\n\n## Example\nAn issue closed after a helper-level unit test remains historically closed, but the recovery record does not claim that its installed CLI or multi-agent integration passed. Verify that specific user workflow at a named revision before marking recovery complete.\n'
    root=parent_issue(PARENT_MARK+repo+':root -->','[EPIC] '+repo+': complete backlog recovery and verified acceptance',root_body)
    result['root']=root['html_url']
    parent_map=parents(api,[r['node_id'] for n,r in current.items() if n in planned])
    intended={};known_children={child:parent for parent,children in EXISTING.get(repo,{}).items() for child in children if child in current and parent in current}
    existing_epics={r['number'] for r in plan if '[EPIC]' in r['expected_title'].upper()}
    unowned=collections.defaultdict(list)
    for r in plan:
        n=r['number'];old_parent=parent_map.get(r['node_id'])
        if old_parent:
            result['parents'][str(n)]={'status':'existing-preserved','url':old_parent['url']};continue
        if n in known_children:intended[n]=current[known_children[n]]
        elif n in existing_epics:intended[n]=root
        else:unowned[r['topic']].append(r)
    # Small repositories need one readable parent, not a forest of tiny epics.
    if len(plan)<=80:
        for rows in unowned.values():
            for r in rows:intended[r['number']]=root
    else:
        for topic,rows in unowned.items():
            for start in range(0,len(rows),85):
                chunk=rows[start:start+85];part=start//85+1;name=TOPICS[topic]+(' — part '+str(part) if len(rows)>85 else '')
                body='## User request — editorial quotation\n\n> '+QUOTE+'\n\nSource: current recovery mandate, 2026-09-10. Topic-specific original/edited quotations and source limitations are in the linked specification and each child.\n\n## Scope\n'+name+' in '+full+'. This is an acceptance-recovery group; it does not duplicate existing implementation tasks.\n\nParent: '+root['html_url']+'\nSpecification: '+DOC+'docs/specs/recovery/'+topic+'.md\n\n## Recovery criteria\n- [ ] Each child has its original scope, source/quotation provenance and an outcome-specific test record.\n- [ ] Unknown live behavior is not marked complete from an unrelated green suite.\n- [ ] Distinct regressions and salvageable work survive consolidation.\n\n## Children\n'+'\n'.join('- [ ] '+GITLINK(full,r['number'])+' — '+r['expected_title'] for r in chunk)
                group=parent_issue(PARENT_MARK+repo+':'+topic+':'+str(part)+' -->','[EPIC] '+repo+' recovery: '+name,body)
                for r in chunk:intended[r['number']]=group
                # group is new and has no inherited parent; add without replacement.
                try:api.call('/repos/'+full+'/issues/'+str(root['number'])+'/sub_issues','POST',{'sub_issue_id':group['id'],'replace_parent':False})
                except RuntimeError as exc:event(group['number'],'parent-link-gap',error=str(exc))
    # Canonical preservation happens before any duplicate is closed.
    ordered=sorted(plan,key=lambda r:(not bool(r['preserves']),r['number']))
    successful=set()
    for r in ordered:
        n=r['number']
        try:
            if r['body'] is None:event(n,'body-size-blocker');continue
            latest=api.call('/repos/'+full+'/issues/'+str(n))
            body=r['body'];target=intended.get(n)
            if target:body=body.replace('## Current evidence and next verification','**Recovery parent:** '+target['html_url']+'\n\n## Current evidence and next verification',1)
            if h(latest.get('body'))==h(body):successful.add(n);event(n,'already-applied',body_sha256=h(body));continue
            if MARK in (latest.get('body') or ''):
                # Idempotent resume: don't stack another audit section or overwrite
                # edits made after the first application.
                successful.add(n);event(n,'existing-grooming-preserved',body_sha256=h(latest['body']));continue
            if h(latest.get('body'))!=r['expected_body_sha256'] or latest['title']!=r['expected_title'] or latest['state']!=r['expected_state']:
                event(n,'concurrent-change-skipped',current_updated_at=latest['updated_at'],current_body_sha256=h(latest.get('body')));continue
            labels=list(dict.fromkeys([x['name'] for x in latest['labels']]+r['labels_add']))
            updated=api.call('/repos/'+full+'/issues/'+str(n),'PATCH',{'body':body,'labels':labels})
            if h(updated.get('body'))!=h(body):raise RuntimeError('Server returned a different body; stop claiming success')
            successful.add(n);event(n,'body-updated-readback-verified',body_sha256=h(body),url=updated['html_url'])
        except RuntimeError as exc:event(n,'mutation-error',error=str(exc))
    for r in plan:
        n=r['number'];target=intended.get(n)
        if n not in successful or target is None:continue
        try:
            api.call('/repos/'+full+'/issues/'+str(target['number'])+'/sub_issues','POST',{'sub_issue_id':current[n]['id'],'replace_parent':False})
            result['parents'][str(n)]={'status':'linked','url':target['html_url']};checkpoint()
        except RuntimeError as exc:
            result['parents'][str(n)]={'status':'preserved-or-link-gap','intended':target['html_url'],'error':str(exc)};checkpoint()
    for r in plan:
        if not r['state_change'] or r['number'] not in successful:continue
        target_number=int(r['canonical'].split('#')[1])
        if target_number not in successful:event(r['number'],'consolidation-blocked-canonical-not-preserved');continue
        try:
            latest=api.call('/repos/'+full+'/issues/'+str(r['number']))
            if latest['state']=='closed':event(r['number'],'consolidation-already-closed');continue
            # State is not "completed": retirement of a duplicate/superseded
            # direction does not claim that the canonical defect is fixed.
            changed=api.call('/repos/'+full+'/issues/'+str(r['number']),'PATCH',{'state':'closed','state_reason':'not_planned'})
            assert changed['state']=='closed'
            event(r['number'],'consolidated-with-preserved-evidence',canonical=r['canonical'],state_reason=changed.get('state_reason'))
        except RuntimeError as exc:event(r['number'],'consolidation-error',error=str(exc))
    # Re-read all native parent objects in batched queries, not just successful POSTs.
    final_parents=parents(api,[r['node_id'] for r in plan if r['number'] in successful])
    for r in plan:
        if r['number'] in successful:
            node=final_parents.get(r['node_id'])
            result['parents'].setdefault(str(r['number']),{})['verified_parent']=node['url'] if node else None
    result['finished_at']=datetime.datetime.now(datetime.timezone.utc).isoformat();result['planned_issues']=len(plan);result['processed_bodies']=len(successful);result['native_parents_verified']=sum(bool(v.get('verified_parent')) for v in result['parents'].values());checkpoint()
    # The workflow uploads the full journal even if some items were skipped.
    failures=[e for e in result['events'] if e['result'] in ('mutation-error','body-size-blocker','concurrent-change-skipped','consolidation-error','consolidation-blocked-canonical-not-preserved')]
    print(json.dumps({k:v for k,v in result.items() if k not in ('events','parents','newly_discovered')},ensure_ascii=False,indent=2))
    if failures:raise SystemExit('Some items need reconciliation; inspect the journal')

def GITLINK(full,number):return 'https://github.com/'+full+'/issues/'+str(number)
if __name__=='__main__':main()
