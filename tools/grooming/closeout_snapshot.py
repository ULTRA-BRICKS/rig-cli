#!/usr/bin/env python3
"""Read-only current inventory; repository transfer is resolved, never guessed.

The historical alex-mextner/name address is a lookup alias. GitHub's canonical
full_name is recorded and used for further reads. A missing or private repository
is reported separately instead of truncating the entire ecosystem inventory.
"""
from __future__ import annotations
import collections,datetime,hashlib,json,os,pathlib,re,time,urllib.request,urllib.error
from resume_v4 import REPOSITORIES,validate_plan,assess,SameOriginRedirect,digest

OUT=pathlib.Path('closeout-results')
BASE=pathlib.Path('docs/grooming/v4-2026-09-10')
HEADERS={'Authorization':'Bearer '+os.environ['GH_TOKEN'],'Accept':'application/vnd.github+json','User-Agent':'rig-authorized-closeout','X-GitHub-Api-Version':'2022-11-28'}
OPENER=urllib.request.build_opener(SameOriginRedirect())

def api(path):
    match=re.match(r'^/repos/(alex-mextner|ultrabricks)/([^/?]+)(?:/|\?|$)',path)
    if not match or match[2] not in REPOSITORIES:raise ValueError('Outside authorized ecosystem')
    for attempt in range(4):
        try:
            with OPENER.open(urllib.request.Request('https://api.github.com'+path,headers=HEADERS),timeout=60) as r:return json.load(r)
        except urllib.error.HTTPError as e:
            limited=e.code==429 or (e.code==403 and (e.headers.get('Retry-After') or e.headers.get('X-RateLimit-Remaining')=='0'))
            if limited:
                wait=max(60,int(e.headers.get('Retry-After','0') or 0))
                if e.headers.get('X-RateLimit-Remaining')=='0':wait=max(wait,int(e.headers.get('X-RateLimit-Reset','0'))-int(time.time())+1)
                if attempt<3 and wait<=900:time.sleep(wait);continue
            elif e.code in (500,502,503,504) and attempt<3:time.sleep(5*(attempt+1));continue
            raise

def pages(path):
    result=[];page=1
    while True:
        batch=api(path+('&' if '?' in path else '?')+'per_page=100&page='+str(page))
        assert isinstance(batch,list)
        result+=batch
        if len(batch)<100:return result
        page+=1

def redact(text):
    patterns=[r'gh[pousr]_[A-Za-z0-9]{30,}',r'github_pat_[A-Za-z0-9_]{30,}',r'\b[0-9]{8,12}:AA[A-Za-z0-9_-]{30,}',r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----']
    for p in patterns:text=re.sub(p,'[REDACTED credential-shaped value]',text)
    return text

def dump(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n')

def safe_body_record(row):
    result=dict(row)
    if isinstance(row.get('body'),str):
        result['observed_body_sha256']=digest(row['body']);result['body']=redact(row['body'])
        result['body_redacted']=result['body']!=row['body']
    return result

def snapshot(repo,quote_ids):
    meta=api('/repos/alex-mextner/'+repo)
    full=meta['full_name']
    if full.split('/')[-1]!=repo or full.split('/')[0] not in ('ultrabricks','alex-mextner'):raise ValueError('Unexpected canonical repository')
    if meta.get('private'):raise ValueError('Private source requires separate review before publication')
    prefix='/repos/'+full
    head=api(prefix+'/commits/'+meta['default_branch'])['sha']
    rows=[i for i in pages(prefix+'/issues?state=all&sort=created&direction=asc') if 'pull_request' not in i]
    prs=pages(prefix+'/pulls?state=all&sort=created&direction=asc')
    comments={}
    for issue in rows:
        if issue.get('comments'):comments[str(issue['number'])]=[safe_body_record(c) for c in pages(prefix+'/issues/'+str(issue['number'])+'/comments')]
    dump(OUT/'inventory'/(repo+'.json'),{'metadata':meta,'head':head,'issues':[safe_body_record(i) for i in rows],'prs':[safe_body_record(p) for p in prs],'comments':comments})
    statuses=[];plan_path=BASE/'plans'/(repo+'.json')
    if plan_path.exists():
        plans=json.loads(plan_path.read_text());before=json.loads((BASE/'before'/(repo+'.json')).read_text())
        priors={r['number']:r for r in before};current={r['number']:r for r in rows}
        for p in plans:
            r={'number':p['number']}
            try:validate_plan([p],repo,[priors[p['number']]],quote_ids)
            except (ValueError,KeyError,TypeError) as e:r.update(result='invalid-saved-plan',reason=str(e))
            else:r['result']=assess(p,current.get(p['number'],{}))
            statuses.append(r)
        known={p['number'] for p in plans}
        for i in rows:
            if i['number'] not in known:statuses.append({'number':i['number'],'result':'new-since-plan'})
    else:statuses=[{'number':i['number'],'result':'no-previous-public-plan'} for i in rows]
    dump(OUT/'preflight'/(repo+'.json'),statuses)
    archive=OUT/'sources'/(repo+'.tar.gz');archive.parent.mkdir(exist_ok=True)
    url='https://codeload.github.com/'+full+'/tar.gz/'+head
    with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'rig-public-source-review'}),timeout=120) as r:raw=r.read(100_000_001)
    if len(raw)>100_000_000:raise ValueError('Source archive unexpectedly large')
    archive.write_bytes(raw)
    return {'id':meta['id'],'canonical_repository':full,'head':head,'issues':len(rows),'open':sum(i['state']=='open' for i in rows),'prs':len(prs),'merged_prs':sum(bool(p.get('merged_at')) for p in prs),'comments':sum(len(v) for v in comments.values()),'preflight':dict(collections.Counter(r['result'] for r in statuses)),'redacted_issues':[i['number'] for i in rows if redact(i.get('body') or '')!=(i.get('body') or '')],'source_archive_sha256':hashlib.sha256(raw).hexdigest()}

def main():
    OUT.mkdir(exist_ok=True)
    registry=json.loads((BASE/'quote-registry.json').read_text());ids={q['id'] for q in registry['records']}
    summary={'checked_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'run_id':os.environ.get('GITHUB_RUN_ID'),'repositories':{},'errors':{},'mutations':0}
    for repo in sorted(REPOSITORIES):
        try:summary['repositories'][repo]=snapshot(repo,ids)
        except urllib.error.HTTPError as e:summary['errors'][repo]={'http_status':e.code,'stage':'read-only snapshot','result':'not inventoried, not empty'}
        except (ValueError,urllib.error.URLError,TimeoutError) as e:summary['errors'][repo]={'kind':type(e).__name__,'stage':'read-only snapshot','result':'incomplete; prior artifacts retained'}
        dump(OUT/'snapshot-summary.json',summary)
        print(repo,json.dumps(summary['repositories'].get(repo,summary['errors'].get(repo))),flush=True)
    summary['finished_at']=datetime.datetime.now(datetime.timezone.utc).isoformat()
    summary['complete']=not summary['errors'] and len(summary['repositories'])==len(REPOSITORIES)
    dump(OUT/'snapshot-summary.json',summary)
    if not summary['complete']:raise SystemExit('Some repository reads need reconciliation; inspect the summary')

if __name__=='__main__':main()
