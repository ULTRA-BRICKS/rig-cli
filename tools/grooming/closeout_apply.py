#!/usr/bin/env python3
"""Apply body-only closeout edits to the calling repository, then read them back.

The scope/quote validator is shared with the 75-tested resume path. A final
paginated read avoids spending three requests per issue under Actions' per-repo
API allowance. A PATCH receipt is provisional until that independent read.
Existing title, state, labels, assignees and native relationships are not changed.
"""
from __future__ import annotations
import argparse,collections,datetime,json,os,pathlib,re,urllib.request
import resume_v4 as core

DATA='docs/grooming/closeout-2026-09-11'
QUOTE_DATA='docs/grooming/v4-2026-09-10/quote-registry.json'

class Client(core.GitHubAPI):
    def all_issues(self):
        out=[];page=1
        while True:
            url=f'https://api.github.com/repos/{self.full}/issues?state=all&sort=created&direction=asc&per_page=100&page={page}'
            request=urllib.request.Request(url,headers={'Authorization':'Bearer '+self.token,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28','User-Agent':'rig-closeout-readback'})
            with self.opener.open(request,timeout=90) as response:batch=json.load(response)
            if not isinstance(batch,list):raise core.ValidationError('Invalid issue collection')
            out.extend(i for i in batch if 'pull_request' not in i)
            if len(batch)<100:return out
            page+=1


def apply_rows(client,rows,journal,context,apply=False):
    result={'schema_version':1,'source_version':4,'context':context,'mode':'apply' if apply else 'dry-run','started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'events':[],'writes':0,'complete':False,'fatal':None,'product_acceptance':False}
    def save():
        result['counts']=dict(collections.Counter(e['result'] for e in result['events']))
        core.save_journal(journal,result)
    save()
    for row in rows:
        event={'key':row['key'],'number':row['number']}
        try:
            current=client.issue(row['number'])
            disposition=core.assess(row,current)
            event.update(result=disposition,observed_before_sha256=core.digest(current.get('body')))
            if disposition=='ready' and apply:
                result['pending_write']={'key':row['key'],'intended_sha256':core.digest(row['body'])};save()
                response=client.patch_body(row['number'],row['body']);result['writes']+=1
                if response.get('id')!=row['id'] or core.digest(response.get('body'))!=core.digest(row['body']):
                    raise core.DeliveryUncertain('PATCH reply differs; do not blindly repeat')
                event['result']='patch-received-awaiting-readback';result.pop('pending_write',None)
            result['events'].append(event);save()
            print(row['key'],event['result'],flush=True)
        except (core.AccessDenied,core.DeliveryUncertain) as exc:
            event['result']='authorization-denied' if isinstance(exc,core.AccessDenied) else 'delivery-uncertain'
            result['fatal']=event['result'];result['events'].append(event);save();break
    # An authorization failure is never probed through another endpoint/token.
    if result['fatal']!='authorization-denied':
        try:
            current=client.all_issues();by_number={i['number']:i for i in current}
            final=[]
            for row in rows:
                item=by_number.get(row['number'],{})
                status=core.assess(row,item)
                final.append({'key':row['key'],'number':row['number'],'result':status,'actual_body_sha256':core.digest(item.get('body'))})
            result['final_readback']=final
            result['verified_exact']=sum(i['result']=='already_exact' for i in final)
            result['new_since_plan']=[i['number'] for i in current if i['number'] not in {r['number'] for r in rows}]
            result['complete']=result['verified_exact']==len(rows) and result['fatal'] is None
        except Exception as exc:
            # Record the error class, never credential-bearing request details.
            result['readback_error']=type(exc).__name__;result['complete']=False
    result['planned']=len(rows)
    result['remaining_or_conflicted']=len(rows)-result.get('verified_exact',0)
    result['finished_at']=datetime.datetime.now(datetime.timezone.utc).isoformat();save()
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-ref',required=True)
    p.add_argument('--source-root',type=pathlib.Path,default=pathlib.Path.cwd())
    p.add_argument('--apply',action='store_true')
    p.add_argument('--journal',type=pathlib.Path,default=pathlib.Path('closeout-applied/receipt.json'))
    a=p.parse_args()
    starter={'complete':False,'phase':'preflight','source_ref':a.source_ref,'events':[],'writes':0}
    core.save_journal(a.journal,starter)
    try:
        if not re.fullmatch('[0-9a-f]{40}',a.source_ref):raise core.ValidationError('Immutable source ref required')
        # Verify both this entry point and the shared validator against git.
        actual=core.subprocess.check_output(['git','-C',str(a.source_root),'rev-parse','HEAD'],text=True).strip()
        if actual!=a.source_ref:raise core.ValidationError('Wrong code/plan revision')
        for module in (pathlib.Path(__file__),pathlib.Path(core.__file__)):
            relative=module.resolve().relative_to(a.source_root.resolve()).as_posix()
            if module.read_bytes()!=core.committed_bytes(a.source_root,a.source_ref,relative):raise core.ValidationError('Executable differs from reviewed commit')
        full=os.environ['GITHUB_REPOSITORY'];repo=core.repository_name(full)
        load=lambda path:json.loads(core.committed_bytes(a.source_root,a.source_ref,path))
        report=load(DATA+'/plan-summary.json');identity=report['repositories'][repo]
        if identity['canonical_repository']!=full or str(identity['id'])!=os.environ.get('GITHUB_REPOSITORY_ID'):raise core.ValidationError('Caller differs from reviewed repository ID')
        rows=load(DATA+'/plans/'+repo+'.json');before=load(DATA+'/before/'+repo+'.json');registry=load(QUOTE_DATA)
        core.validate_plan(rows,repo,before,{q['id'] for q in registry['records']})
        token=os.environ.get('GH_TOKEN')
        if not token:raise core.AccessDenied('No repository token')
        client=Client(full,token,interval=8.0)
        full=core.canonical_repository(full,client.request(),os.environ['GITHUB_REPOSITORY_ID']);client.full=full
    except Exception as exc:
        starter['fatal']=type(exc).__name__
        if isinstance(exc,core.ValidationError):starter['reason']=str(exc)
        core.save_journal(a.journal,starter);raise SystemExit('Preflight failed; see receipt')
    result=apply_rows(client,rows,a.journal,{'repository':full,'repository_id':identity['id'],'source_ref':a.source_ref,'snapshot_run':report['snapshot_run'],'mutations':'body only','manual_exclusions':identity['manual']},a.apply)
    print(json.dumps({k:v for k,v in result.items() if k not in ('events','final_readback')},ensure_ascii=False,indent=2))
    if not result['complete']:raise SystemExit('Partial or dry-run result; inspect receipt')

if __name__=='__main__':main()
