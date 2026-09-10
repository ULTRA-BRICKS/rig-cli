#!/usr/bin/env python3
"""Repository-local delivery of the verified v4 issue plan.

The source code and plan come from the exact commit whose public reader was
verified. Each repository's own token performs its writes. No force update,
blanket reopening, changed-title overwrite, or approval is performed here.
"""
from __future__ import annotations
import argparse
import collections
import hashlib
import importlib.util
import inspect
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

REPOS={'rig-cli','agent-tools','review-cli','task-cli','tg-cli','qa-cli','draw-cli','stt-cli','research-cli','dev-cli','pm-cli'}
BRANCH='docs/full-grooming-2026-09-10'
RAW='https://raw.githubusercontent.com/alex-mextner/rig-cli/'
OUT=pathlib.Path('grooming-v4-results')
LOG={'version':4,'events':[],'started_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())}
LAST_WRITE=0.


def checkpoint():
    OUT.mkdir(exist_ok=True)
    LOG['event_counts']=dict(collections.Counter(e['result'] for e in LOG['events']))
    (OUT/'readback.json').write_text(json.dumps(LOG,ensure_ascii=False,indent=2)+'\n')


def read_public(path):
    request=urllib.request.Request(path,headers={'User-Agent':'rig-v4-reviewed-plan','Cache-Control':'no-cache'})
    with urllib.request.urlopen(request,timeout=120) as response:
        if not response.url.startswith('https://raw.githubusercontent.com/alex-mextner/rig-cli/'):
            raise ValueError('Unexpected public-source origin')
        raw=response.read(32_000_001)
    if len(raw)>32_000_000:raise ValueError('Public object exceeds the reviewed size bound')
    return raw


def api(path,method='GET',data=None):
    global LAST_WRITE
    if not (path.startswith('/repos/alex-mextner/') or path=='/graphql'):raise ValueError('Unexpected API target')
    for attempt in range(5):
        if method!='GET' and path!='/graphql':time.sleep(max(0,2-(time.monotonic()-LAST_WRITE)))
        req=urllib.request.Request('https://api.github.com'+path,method=method,data=None if data is None else json.dumps(data,ensure_ascii=False).encode(),headers={'Authorization':'Bearer '+os.environ['GH_TOKEN'],'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28','User-Agent':'rig-v4-preserve-parent'})
        try:
            with urllib.request.urlopen(req,timeout=60) as response:
                raw=response.read()
            if method!='GET' and path!='/graphql':LAST_WRITE=time.monotonic()
            return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            detail=exc.read().decode(errors='replace')
            if exc.code in (429,500,502,503,504) or exc.code==403 and ('rate limit' in detail.lower() or exc.headers.get('Retry-After')):
                wait=max(5,int(exc.headers.get('Retry-After','0') or 0))
                if exc.headers.get('X-RateLimit-Remaining')=='0':wait=max(wait,int(exc.headers.get('X-RateLimit-Reset','0'))-int(time.time())+1)
                if attempt<4 and wait<900:time.sleep(wait);continue
            raise RuntimeError(f'{method} {path}: HTTP {exc.code}; {detail[:400]}') from exc
    raise RuntimeError('Retry budget exhausted')


def collection(path):
    out=[];page=1
    while True:
        batch=api(path+('&' if '?' in path else '?')+'per_page=100&page='+str(page))
        if not isinstance(batch,list):raise ValueError('Expected list')
        out+=batch
        if len(batch)<100:return out
        page+=1


def load_module(path,name):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);sys.modules[name]=module
    spec.loader.exec_module(module)
    return module


def native_parents(issues):
    result={}
    for start in range(0,len(issues),40):
        ids=[i['node_id'] for i in issues[start:start+40]]
        data=api('/graphql','POST',{'query':'query($ids:[ID!]!){nodes(ids:$ids){... on Issue{id number parent{id number url}}}}','variables':{'ids':ids}})
        if data.get('errors'):raise RuntimeError('Native-parent read failed: '+str(data['errors'])[:400])
        for item in data['data']['nodes']:
            if item:result[item['id']]=item.get('parent')
    return result


def invoke_apply(engine,full,ref):
    options=[]
    for name,fn in vars(engine).items():
        if not inspect.isfunction(fn) or fn.__module__!=engine.__name__ or not name.startswith('apply'):continue
        sig=inspect.signature(fn);values={};supported=True
        for p in sig.parameters.values():
            if p.name in ('repo','repository','repository_full_name','full','full_name'):values[p.name]=full
            elif p.name in ('ref','plan_ref','source_ref','sha','commit'):values[p.name]=ref
            elif p.name in ('apply','do_apply','write'):values[p.name]=True
            elif p.default is not inspect.Parameter.empty:continue
            elif p.kind in (p.VAR_POSITIONAL,p.VAR_KEYWORD):continue
            else:supported=False
        if supported:options.append((name,fn,values))
    if len(options)!=1:raise RuntimeError('Expected one reviewed apply function with repository/ref inputs: '+str([n for n,_,_ in options]))
    LOG['engine_function']=options[0][0];checkpoint()
    try:options[0][1](**options[0][2])
    except SystemExit as exc:
        if exc.code not in (None,0):LOG['events'].append({'result':'engine-reconciliation-needed','detail':str(exc.code)})
    except Exception as exc:
        LOG['events'].append({'result':'engine-error','detail':type(exc).__name__+': '+str(exc)[:2000]})
    checkpoint()


def main():
    full=os.environ.get('GITHUB_REPOSITORY','');parts=full.split('/')
    if len(parts)!=2 or parts[0]!='alex-mextner' or parts[1] not in REPOS:raise ValueError('Repository not in public recovery scope')
    repo=parts[1];LOG['repository']=full;checkpoint()
    publication_url=RAW+'refs/heads/'+BRANCH+'/docs/grooming/v4-2026-09-10/publication.json'
    publication=None
    for attempt in range(60):
        try:
            candidate=json.loads(read_public(publication_url))
            if candidate.get('version')==4 and candidate.get('same_content_verified') and candidate.get('external_browser_verified'):
                publication=candidate;break
        except (urllib.error.HTTPError,urllib.error.URLError,json.JSONDecodeError):pass
        time.sleep(20)
    if not publication:raise RuntimeError('No verified v4 publication; refusing to substitute the older plan')
    ref=publication['source_sha']
    if not re.fullmatch('[0-9a-f]{40}',ref):raise ValueError('Plan ref is not immutable')
    LOG['plan_ref']=ref;LOG['publication_url']=publication['url'];checkpoint()
    engine_dir=pathlib.Path('.v4-engine');engine_dir.mkdir(exist_ok=True)
    for name in ('v4_reconcile.py','v4_policy.py','build.py'):
        raw=read_public(RAW+ref+'/tools/grooming/'+name)
        (engine_dir/name).write_bytes(raw)
    sys.path.insert(0,str(engine_dir.resolve()))
    engine=load_module(engine_dir/'v4_reconcile.py','rig_reviewed_v4_apply')
    plan=json.loads(read_public(RAW+ref+'/docs/grooming/v4-2026-09-10/plans/'+repo+'.json'))
    if not isinstance(plan,list):raise ValueError('Invalid plan collection')
    LOG['planned_issues']=len(plan)
    invoke_apply(engine,full,ref)
    current=[i for i in collection('/repos/'+full+'/issues?state=all&sort=created&direction=asc') if 'pull_request' not in i]
    marker=getattr(engine,'MARK','')
    if not isinstance(marker,str) or 'v4' not in marker.lower():
        marker='<!-- rig-grooming:v4'
    planned_numbers={i['number'] for i in plan}
    bodies={i['number']:i for i in current}
    exact=[];marker_only=[];missing=[]
    for row in plan:
        n=row['number'];body=(bodies.get(n) or {}).get('body') or ''
        expected=row.get('body') or ''
        if expected and hashlib.sha256(body.encode()).digest()==hashlib.sha256(expected.encode()).digest():exact.append(n)
        elif marker in body:marker_only.append(n)
        else:missing.append(n)
    LOG['body_readback']={'exact_v4_plan':len(exact),'v4_marker_with_later_edits':len(marker_only),'not_v4':missing}
    # Only fill a missing recovery parent. Existing implementation ownership and
    # parents from another project/repository are preserved as they are.
    parent_map=native_parents(current)
    root_marker='<!-- rig-grooming-parent:2026-09-10:'+repo+':root -->'
    roots=[i for i in current if root_marker in (i.get('body') or '')]
    if len(roots)==1:
        root=roots[0];LOG['recovery_root']=root['html_url']
        for issue in current:
            if issue['number']==root['number'] or repo=='rig-cli' and issue['number']==521:continue
            if parent_map.get(issue['node_id']):continue
            if issue['number'] not in exact and issue['number'] not in marker_only:continue
            try:
                api('/repos/'+full+'/issues/'+str(root['number'])+'/sub_issues','POST',{'sub_issue_id':issue['id'],'replace_parent':False})
                LOG['events'].append({'number':issue['number'],'result':'missing-native-parent-linked','parent':root['html_url']})
            except RuntimeError as exc:LOG['events'].append({'number':issue['number'],'result':'native-parent-preserved-or-unavailable','detail':str(exc)})
            checkpoint()
    else:
        LOG['events'].append({'result':'ambiguous-or-missing-recovery-root','count':len(roots)})
    final_map=native_parents(current)
    LOG['native_parents_verified']=sum(bool(final_map.get(i['node_id'])) for i in current if i['number'] in planned_numbers)
    LOG['new_since_plan']=[{'number':i['number'],'title':i['title'],'state':i['state'],'url':i['html_url']} for i in current if i['number'] not in planned_numbers]
    LOG['completed_all_planned_bodies']=not missing
    LOG['finished_at']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime());checkpoint()
    if missing or any(e['result']=='engine-error' for e in LOG['events']):raise SystemExit('Some issues require read-back reconciliation; see the committed journal')

if __name__=='__main__':
    try:main()
    finally:checkpoint()
