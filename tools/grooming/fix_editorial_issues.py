#!/usr/bin/env python3
"""Remove this grooming pass's Russian boilerplate, keeping original issue scope.

Only exact owned v4 closeout sections are candidates. No state, title, label or
relationship is changed. Hand-written corrections and unrelated issues are left
alone. Reports contain identifiers and hashes, not copied transcript bodies.
"""
from __future__ import annotations
import argparse, hashlib, json, os, pathlib, re, time, urllib.error, urllib.request

START='<!-- rig-grooming:v4:2026-09-10 -->'
END='<!-- /rig-grooming:v4:2026-09-10 -->'
OWN='<!-- rig-closeout:2026-09-11 -->'
SCOPE='## Сохранённая техническая постановка и история'
FIX='<!-- rig-editorial-correction:2026-09-11 -->'
REPOS=frozenset({'rig-cli','agent-tools','review-cli','harness-cli','tg-cli','task-cli','qa-cli','stt-cli','research-cli','dev-cli','pm-cli'})
REGISTRY='https://github.com/ultrabricks/rig-cli/blob/f707ee2b2f643a4621121187df442554aa968244/docs/grooming/v4-2026-09-10/quote-registry.json'

class ManualReview(ValueError): pass
class Stop(RuntimeError): pass

def sha(text):return hashlib.sha256((text or '').encode()).hexdigest()

def editorial_body(body):
    if not body.startswith(START+'\n') or OWN not in body.split(END,1)[0]:return None
    # Treat markers in fenced or quoted examples as content, not boundaries.
    structural=[];fence=None;offset=0
    for line in body.splitlines(keepends=True):
        value=line.rstrip('\r\n');m=re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$',value)
        if fence:
            if m and m[1][0]==fence[0] and len(m[1])>=fence[1] and not m[2].strip():fence=None
        elif m:fence=(m[1][0],len(m[1]))
        elif value in (START,END,OWN,SCOPE):structural.append((value,offset,offset+len(line)))
        offset+=len(line)
    ends=[r for r in structural if r[0]==END];heads=[r for r in structural if r[0]==SCOPE]
    if len(ends)!=1 or len(heads)!=1 or heads[0][1]<ends[0][2]:raise ManualReview('Ambiguous managed boundary')
    if body[ends[0][2]:heads[0][1]].strip():raise ManualReview('Independent text before preserved scope')
    original=body[heads[0][2]:].strip()
    if not original:raise ManualReview('Empty original issue scope')
    prefix=body[:ends[0][1]]
    quotes=[]
    for m in re.finditer(r'(?m)^((?:>[^\n]*(?:\n|$))+)(?:\n)*Источник \*\*(v4-[\w-]+)\*\*([^\n]*)',prefix):
        # Do not promote generic standing instructions to an issue's origin.
        if 'общее требование' in m[3]:continue
        quote=m[1].strip();ident=m[2]
        if (quote,ident) not in quotes:quotes.append((quote,ident))
    intro=FIX+'\n\n'
    if quotes:
        intro+='## User request\n\n'+'\n\n'.join(q+'\n\nSource: ['+i+']('+REGISTRY+').' for q,i in quotes)+'\n\n'
    revised=intro+original+'\n'
    if original not in revised:raise ManualReview('Original text not preserved')
    if len(revised)>63000:raise ManualReview('Issue body too large')
    return revised

class SameOrigin(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        from urllib.parse import urlsplit
        before,after=urlsplit(req.full_url),urlsplit(newurl)
        if (before.scheme,before.netloc)!=(after.scheme,after.netloc) or after.scheme!='https':raise Stop('Cross-origin redirect refused')
        return super().redirect_request(req,fp,code,msg,headers,newurl)

class API:
    def __init__(self,repo,token,interval=45):
        if repo not in REPOS:raise ValueError('Repository outside this correction; draw-cli excluded')
        self.full='ultrabricks/'+repo;self.token=token;self.last=0.;self.interval=interval
        self.opener=urllib.request.build_opener(SameOrigin())
    def call(self,path='',body=None):
        if path and not re.fullmatch(r'/issues(?:/[1-9]\d*)?(?:\?state=all&per_page=100&page=[1-9]\d*)?',path):raise ValueError('Unexpected endpoint')
        if body is not None:
            if not re.fullmatch(r'/issues/[1-9]\d*',path):raise ValueError('Only an issue body can be changed')
            time.sleep(max(0,self.last+self.interval-time.monotonic()));self.last=time.monotonic()
        request=urllib.request.Request('https://api.github.com/repos/'+self.full+path,
            method='PATCH' if body is not None else 'GET',
            data=json.dumps({'body':body}).encode() if body is not None else None,
            headers={'Authorization':'Bearer '+self.token,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28','Content-Type':'application/json','User-Agent':'rig-editorial-correction'})
        try:
            with self.opener.open(request,timeout=60) as response:return json.load(response)
        except urllib.error.HTTPError as exc:
            # Stop rather than bypass auth or repeat a possibly accepted mutation.
            try:message=json.loads(exc.read()).get('message','').lower()
            except Exception:message=''
            kind='rate-limited' if exc.code==429 or exc.headers.get('Retry-After') or 'rate limit'in message else 'access-denied' if exc.code in (401,403) else 'http-error'
            raise Stop(f'{kind}: HTTP {exc.code}; inspect outcome before resuming') from exc
        except (urllib.error.URLError,TimeoutError,json.JSONDecodeError) as exc:raise Stop('Transport outcome unknown; read back before resuming') from exc
    def issues(self):
        result=[];page=1
        while True:
            rows=self.call(f'/issues?state=all&per_page=100&page={page}')
            if not isinstance(rows,list):raise Stop('Invalid issue collection')
            result.extend(r for r in rows if 'pull_request'not in r)
            if len(rows)<100:return result
            page+=1

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--repository',required=True);p.add_argument('--apply',action='store_true');p.add_argument('--journal',type=pathlib.Path,default=pathlib.Path('editorial-result.json'));a=p.parse_args()
    if a.repository not in REPOS:raise SystemExit('Out of scope; draw-cli is excluded')
    if os.environ.get('GITHUB_REPOSITORY')!='ultrabricks/'+a.repository:raise SystemExit('Caller repository mismatch')
    api=API(a.repository,os.environ['GH_TOKEN'])
    meta=api.call()
    if str(meta['id'])!=os.environ.get('GITHUB_REPOSITORY_ID'):raise SystemExit('Caller repository ID mismatch')
    result={'repository':api.full,'mode':'apply' if a.apply else 'dry-run','events':[],'writes':0,'complete':False,'only_owned_v4_sections':True,'product_acceptance':False}
    def save():
        a.journal.parent.mkdir(parents=True,exist_ok=True);tmp=a.journal.with_suffix('.tmp');tmp.write_text(json.dumps(result,indent=2)+'\n');tmp.replace(a.journal)
    save()
    try:
        for issue in api.issues():
            try:proposed=editorial_body(issue.get('body')or'')
            except ManualReview as exc:result['events'].append({'number':issue['number'],'result':'manual-review','reason':str(exc)});save();continue
            if proposed is None:continue
            n=issue['number'];entry={'number':n,'before_sha256':sha(issue.get('body')),'after_sha256':sha(proposed),'result':'ready'}
            result['events'].append(entry);save()
            if a.apply:
                fresh=api.call(f'/issues/{n}')
                if fresh.get('id')!=issue['id'] or fresh.get('body')!=issue.get('body'):
                    entry['result']='concurrent-edit-preserved';save();continue
                reply=api.call(f'/issues/{n}',proposed);result['writes']+=1
                if reply.get('id')!=issue['id'] or reply.get('body')!=proposed:raise Stop('PATCH result not exact')
                actual=api.call(f'/issues/{n}')
                entry['result']='updated-and-read-back' if actual.get('body')==proposed else 'postwrite-edit-preserved'
            save();print(f"{api.full}#{n}: {entry['result']}",flush=True)
        # Independent final collection checks no owned Russian prefix was missed.
        remaining=[]
        for issue in api.issues():
            if (issue.get('body')or'').startswith(START+'\n') and OWN in (issue.get('body')or''):
                remaining.append(issue['number'])
        result['remaining_owned_v4']=remaining;result['complete']=a.apply and not remaining
    except Stop as exc:result['stop']=str(exc)
    finally:save()
    if not result['complete']:raise SystemExit('Correction incomplete or dry-run; see journal')

if __name__=='__main__':main()
