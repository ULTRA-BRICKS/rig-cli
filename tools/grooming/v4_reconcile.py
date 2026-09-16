#!/usr/bin/env python3
"""Source-grounded v4 reconciliation. Ticket text is data, never executable code.

compile: read current public issues and build an immutable, reviewable plan.
apply: one repository's scoped token, read-before-write, additive labels, read-back.
No state changes, merges, permission weakening, process termination or credential
access. Existing parent relationships are never replaced by this program.
"""
from __future__ import annotations
import argparse,collections,datetime,hashlib,importlib.util,json,os,pathlib,re,time,urllib.error,urllib.request,unicodedata
from v4_policy import ASSOCIATIONS,DEPENDENCIES,DOMAIN_QUOTES,EDITED,FINDINGS,MANDATE
BASE=pathlib.Path('docs/grooming/v4-2026-09-10')
OLD=pathlib.Path('docs/grooming/full-2026-09-10')
REPOS=['rig-cli','agent-tools','review-cli','task-cli','tg-cli','qa-cli','draw-cli','stt-cli','research-cli','dev-cli','pm-cli']
MARK='<!-- rig-grooming:v4:2026-09-10 -->'
END='<!-- /rig-grooming:v4:2026-09-10 -->'
GIT='https://github.com/alex-mextner/'
DOC=GIT+'rig-cli/blob/docs/full-grooming-2026-09-10/'
ROOT=GIT+'rig-cli/issues/521'


def sha(text):return hashlib.sha256((text or '').encode()).hexdigest()
def norm(text):return re.sub(r'\s+',' ',unicodedata.normalize('NFKC',text)).strip().casefold()
def dump(path,obj):path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n')

class API:
    def __init__(self):self.token=os.environ['GH_TOKEN'];self.last=0.;self.writes=0
    def call(self,path,method='GET',data=None):
        if not path.startswith('/repos/'):raise ValueError('Repository API only')
        for attempt in range(6):
            if method!='GET':time.sleep(max(0,float(os.environ.get('WRITE_INTERVAL','2.0'))-time.monotonic()+self.last))
            headers={'Authorization':'Bearer '+self.token,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28','User-Agent':'rig-v4-authorized-grooming'}
            req=urllib.request.Request('https://api.github.com'+path,method=method,headers=headers,data=None if data is None else json.dumps(data).encode())
            try:
                with urllib.request.urlopen(req,timeout=60) as response:raw=response.read()
                if method!='GET':self.last=time.monotonic();self.writes+=1
                return json.loads(raw) if raw else None
            except urllib.error.HTTPError as exc:
                detail=exc.read().decode(errors='replace')
                retry=exc.code in (429,500,502,503,504) or (exc.code==403 and 'rate limit' in detail.lower())
                if retry and attempt<5:
                    wait=max(10*(attempt+1),int(exc.headers.get('Retry-After','0') or 0))
                    if exc.headers.get('X-RateLimit-Remaining')=='0':wait=max(wait,int(exc.headers.get('X-RateLimit-Reset','0'))-int(time.time())+1)
                    if wait>900:raise RuntimeError('Rate limit; checkpoint retained, no bypass')
                    time.sleep(wait);continue
                raise RuntimeError(f'{method} {path}: HTTP {exc.code}: {detail[:240]}') from exc
        raise RuntimeError('Retries exhausted')
    def pages(self,path):
        out=[];page=1
        while True:
            data=self.call(path+('&' if '?' in path else '?')+'per_page=100&page='+str(page));out.extend(data)
            if len(data)<100:return out
            page+=1


def load_quotes():
    data=json.loads((BASE/'reviewed-input.json').read_text());aliases={}
    old=json.loads((OLD/'quote-registry.json').read_text())['records']
    old_by_text={norm(q['original']):q for q in old}
    for q in data['records']:
        original=q['original'];exact=old_by_text.get(norm(original));edited=original
        if exact:edited=exact['edited']
        for ident in q['source_ids']:
            if ident in EDITED:
                edited=EDITED[ident];q['editorial_status']='Reviewed editorial excerpt; full original retained';break
        if edited==original:
            # Conservative editorial correction only, outside code and URLs.
            chunks=re.split(r'(```[\s\S]*?```|`[^`\n]*`|https?://\S+)',original)
            fixes={'чтото':'что-то','какойто':'какой-то','почемуто':'почему-то','изза':'из-за','сейчса':'сейчас','несколкько':'несколько','несоклько':'несколько','ничго':'ничего','поднимишь':'поднимешь','сессиия':'сессия','восстанвои':'восстанови','полноенное':'полноценное','елси':'если'}
            for i in range(0,len(chunks),2):chunks[i]=re.sub(r'\b[А-Яа-яЁё]+\b',lambda m:fixes.get(m.group().lower(),m.group()),chunks[i])
            edited=''.join(chunks).strip()
            if re.match('[а-яё]',edited):edited=edited[0].upper()+edited[1:]
            if re.search('[А-Яа-яЁё]$',edited):edited+='.'
            q['editorial_status']='Conservative spelling/punctuation edit; no inferred missing context'
        q['edited']=edited
        for ident in q['source_ids']:aliases[ident]=q
    return data,aliases


def original_scope(body):
    if MARK in body and END in body:return body.split(END,1)[1].strip()
    heading='## Original technical scope, acceptance and delivery history'
    if heading in body:
        before,scope=body.split(heading,1)
        scope=scope.rsplit('<!-- /rig-grooming:2026-09-10:v1 -->',1)[0].strip()
        # Keep explicitly salvaged requirements, not just the duplicate's title.
        preserve='## Consolidated requirements and retained findings'
        if preserve in before:scope+='\n\n'+before.split(preserve,1)[1]
        return scope
    return body


def quote_md(q,relation):
    loc=[]
    for p in q.get('provenance',[]):
        if p.get('parent_attribution') or p.get('source')=='telegram':
            loc.append(str(p.get('source'))+' '+str(p.get('timestamp'))+'; line '+str(p.get('line_number')))
    return ('> '+q['edited'].replace('\n','\n> ')+'\n\n'+f"Источник **{q['id']}** ({q['source_kind']}); "+relation+'. '+('; '.join(loc[:2]) if loc else q['context'])+'\n\n<details><summary>Полный исходный текст до редакторской правки</summary>\n\n> '+q['original'].replace('\n','\n> ')+'\n\n</details>')


def topic_for(repo,title,scope):
    # Use the established registry unless a more specific operation is explicit.
    spec=importlib.util.spec_from_file_location('old_compiler',pathlib.Path(__file__).with_name('build.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module.classify(repo,title)


def choose_sources(repo,issue,scope,topic,records,aliases):
    found=[];kind='current-grooming-mandate-only'
    for ident,numbers in ASSOCIATIONS.get(repo,{}).items():
        if issue['number'] in numbers and ident in aliases:found.append(aliases[ident]);kind='reviewed-requirement-association'
    normalized=norm(scope)
    for q in records:
        if len(q['original'])>=45 and norm(q['original']) in normalized:
            if q not in found:found.insert(0,q)
            kind='source-text-also-present-in-original-description'
    # No generic themed quote is passed off as a recovered feature original.
    seen=set();unique=[]
    for q in found:
        if q['id'] not in seen:seen.add(q['id']);unique.append(q)
    return unique[:3],kind


def make_plan(repo,issue,records,aliases,tests,head,oldrow):
    body=issue.get('body') or '';scope=original_scope(body)
    topic=(oldrow or {}).get('topic') or topic_for(repo,issue['title'],scope)
    quotes,attribution=choose_sources(repo,issue,scope,topic,records,aliases)
    is_process=bool(re.match(r'(?i)^(?:review (?:the |current |uncommitted|staged|version|release)|audit .*?(?:diff|version|release))',issue['title']))
    key=repo+'#'+str(issue['number']);status='process-record' if is_process else ('epic' if '[EPIC]' in issue['title'].upper() else 'product-or-maintenance')
    lead=[MARK,'## Исходный запрос и результат груминга v4',f'**{key}** · {status} · [Спецификация]({DOC}docs/specs/recovery/{topic}.md) · [Ретроспектива]({DOC}docs/specs/recovery/01-retrospective-v4.md) · [Координатор]({ROOT})']
    if quotes:
        lead.append('Ниже — исходные запросы по этой проблеме с редакторской правкой. Связь проверена по содержанию; она не означает, что пользователь диктовал заголовок тикета или все детали реализации.')
        lead.extend(quote_md(q,attribution) for q in quotes)
    else:
        lead.append('**Исходная постановка именно этой исторической задачи в v4 однозначно не установлена.** Предыдущее добавление общей тематической цитаты не закрывало этот пробел. Сохраняются исходные технические требования и история; чужой текст или новый пересказ не выдаются за запрос пользователя.')
        lead.append('> '+MANDATE+'\n\nЭто текущий запрос на груминг из этой переписки, с редакторской правкой; **не исходная постановка функции**. Полный v4 source/disposition ledger: '+DOC+'docs/grooming/v4-2026-09-10/source-audit.json')
    criteria=[re.split(r'\s+—\s*forced:',t,flags=re.I)[0].strip() for _,t in re.findall(r'(?m)^\s*- \[([ xX])\] (.+)$',scope)]
    lead+=['## Проверка исходного результата',f'GitHub state при чтении: **{issue["state"]}** ({issue.get("state_reason") or "без уточнения"}). Этот проход не меняет его на основании merge, screenshot path или source status.',f'Проверяемая операция: **{issue["title"]}**.']
    if criteria:lead.append('Повторить конкретные критерии этого тикета:\n'+'\n'.join('- '+c for c in list(dict.fromkeys(criteria))[:3]))
    else:lead.append('В исходном описании нет явного checklist приёмки. Требуется уточнить проверяемый вход и ожидаемый результат из сохранённого scope; процессное ревью не превращается в новую продуктовую функцию.')
    lead.append('Пример протокола проверки: на чистой конфигурации выполнить указанную ниже операцию с обычным входом; затем повторить при недоступной зависимости и после перезапуска. Записать фактический и ожидаемый вывод, exit code/изменение состояния, ревизию и воспроизводимые шаги. Пример — план, а не утверждение об уже полученном результате.')
    if tests:
        lead.append('Изолированные тесты репозитория: '+str(tests.get('test_counts'))+' на `'+tests['source_sha']+'`. Это только соответствующий run; текущая default revision при сверке: `'+head+'`. '+('Ревизия изменилась: прежний прогон не аттестует новый HEAD. ' if tests['source_sha']!=head else '')+'Реальная macOS, аккаунты и пользовательский UI отдельно не приняты. '+tests.get('qualification',''))
    else:lead.append('Нового изолированного тестового результата для этого репозитория здесь нет. Наличие кода/закрытого PR не подставлено вместо проверки.')
    if oldrow and oldrow.get('canonical'):lead.append('Каноническая задача после сохранения уникальных требований: '+GIT+oldrow['canonical'].replace('#','/issues/'))
    if oldrow and oldrow.get('related_issues'):
        refs=[r for r in oldrow['related_issues'] if r!=key]
        if refs:lead.append('Сохранённые related references: '+', '.join('['+r+']('+GIT+r.replace('#','/issues/')+')' for r in refs[:20])+'. Related не означает blocked-by.')
    lead.append('Все 38 578 keyword-score связей из v4 остались непроверенными кандидатами, а не native dependencies. Исходная история сохранена ниже и в before-image журнале; незавершённые проверки не объявлены успешными.')
    new='\n\n'.join(lead)+'\n\n'+END+'\n\n## Сохранённая техническая постановка и история\n\n'+scope+'\n'
    record={'key':key,'repo':repo,'number':issue['number'],'title':issue['title'],'provider_state':issue['state'],'topic':topic,'classification':status,'provenance':attribution if quotes else 'original-feature-source-gap','quote_ids':[q['id'] for q in quotes],'source_sha':head,'verification_disposition':'not-live-accepted','verification_flags':([] if quotes else ['Original feature source not established'])+(['No explicit acceptance checklist'] if not criteria else []),'canonical':(oldrow or {}).get('canonical')}
    return {'number':issue['number'],'key':key,'id':issue['id'],'title':issue['title'],'expected_sha256':sha(body),'expected_state':issue['state'],'expected_updated_at':issue['updated_at'],'body':new if len(new)<63000 else None,'record':record}


def compile_corpus():
    api=API();data,aliases=load_quotes();tests=json.loads((OLD/'test-results.json').read_text());summary={'source_version':4,'compiled_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'repositories':{},'issue_mutations':0,'source_audit':data['audit'],'quote_association_policy':'Exact source-text matches or explicitly reviewed policy map; no keyword-score acceptance.'}
    for repo in REPOS:
        issues=[i for i in api.pages('/repos/alex-mextner/'+repo+'/issues?state=all&sort=created&direction=asc') if 'pull_request' not in i]
        head=api.call('/repos/alex-mextner/'+repo+'/commits/HEAD')['sha']
        oldrows={r['number']:r for r in json.loads((OLD/'issues'/(repo+'.json')).read_text())}
        plan=[make_plan(repo,i,data['records'],aliases,tests.get(repo),head,oldrows.get(i['number'])) for i in issues]
        dump(BASE/'before'/(repo+'.json'),issues);dump(BASE/'plans'/(repo+'.json'),plan);dump(BASE/'issues'/(repo+'.json'),[p['record'] for p in plan])
        summary['repositories'][repo]={'issues':len(issues),'source_revision':head,'with_requirement_quotes':sum(bool(p['record']['quote_ids']) for p in plan),'ready_bodies':sum(p['body'] is not None for p in plan),'body_size_blockers':[p['number'] for p in plan if p['body'] is None]}
        print(repo,summary['repositories'][repo],flush=True)
    summary['public_issues']=sum(r['issues'] for r in summary['repositories'].values())
    summary['source_quote_records']=len(data['records']);summary['public_prs']=935
    summary['with_requirement_quotes']=sum(r['with_requirement_quotes'] for r in summary['repositories'].values())
    dump(BASE/'quote-registry.json',{'records':data['records'],'excluded_records':[],'audit':data['audit']});dump(BASE/'corpus-summary.json',summary);dump(BASE/'test-results.json',tests)
    usage=collections.defaultdict(list)
    for p in (BASE/'issues').glob('*.json'):
        for row in json.loads(p.read_text()):
            for ident in row['quote_ids']:usage[ident].append(row['key'])
    dump(BASE/'request-coverage.json',[{'id':q['id'],'source_ids':q['source_ids'],'related_issues':usage[q['id']],'disposition':'associated with reviewed issues' if usage[q['id']] else 'retained in source registry; no issue-level association established'} for q in data['records']])


def get_public_json(ref,path):
    if not re.fullmatch('[0-9a-f]{40}',ref):raise ValueError('Immutable ref required')
    url='https://raw.githubusercontent.com/alex-mextner/rig-cli/'+ref+'/'+path
    with urllib.request.urlopen(url,timeout=90) as response:return json.load(response)


def apply_repo(repo,ref):
    full='alex-mextner/'+repo
    if repo not in REPOS+['harness-cli'] or os.environ.get('GITHUB_REPOSITORY')!=full:raise ValueError('Repository token scope mismatch')
    api=API();out=pathlib.Path('grooming-v4-results');out.mkdir(exist_ok=True)
    result={'repository':full,'plan_ref':ref,'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'events':[],'dependencies':[],'state_mutations':0}
    def event(n,status,**kw):
        result['events'].append(dict(number=n,result=status,**kw));result['counts']=dict(collections.Counter(e['result'] for e in result['events']));dump(out/'applied.json',result);print(n,status,flush=True)
    if repo=='harness-cli':
        # Source quotations are public evidence; private source/bodies never leave
        # the repository-local journal or get copied into the public portal.
        d=get_public_json(ref,str(BASE/'quote-registry.json'));aliases={k:q for q in d['records'] for k in q['source_ids']}
        current=[i for i in api.pages('/repos/'+full+'/issues?state=all') if 'pull_request' not in i]
        head=api.call('/repos/'+full+'/commits/HEAD')['sha']
        plan=[make_plan(repo,i,d['records'],aliases,None,head,None) for i in current]
        dump(out/'private-before.json',current)
    else:plan=get_public_json(ref,str(BASE/'plans'/(repo+'.json')))
    dump(out/'reviewed-plan.json',plan)
    for p in plan:
        n=p['number']
        try:
            if p['body'] is None:event(n,'body-size-gap');continue
            latest=api.call('/repos/'+full+'/issues/'+str(n));old=latest.get('body') or ''
            if sha(old)==sha(p['body']):event(n,'already-applied-readback');continue
            if MARK in old:event(n,'v4-edit-preserved');continue
            if sha(old)!=p['expected_sha256'] or latest['title']!=p['title'] or latest['state']!=p['expected_state']:
                event(n,'concurrent-change-preserved',current_updated_at=latest['updated_at']);continue
            changed=api.call('/repos/'+full+'/issues/'+str(n),'PATCH',{'body':p['body']})
            if sha(changed.get('body'))!=sha(p['body']):raise RuntimeError('Body read-back mismatch')
            event(n,'updated-readback-verified',body_sha256=sha(changed['body']),url=changed['html_url'])
        except RuntimeError as exc:event(n,'mutation-gap',error=str(exc))
    for blocked,blocking in DEPENDENCIES:
        if not blocked.startswith(repo+'#'):continue
        b_repo,b_num=blocking.split('#');number=int(blocked.split('#')[1])
        try:
            other=api.call('/repos/alex-mextner/'+b_repo+'/issues/'+b_num)
            existing=api.pages('/repos/'+full+'/issues/'+str(number)+'/dependencies/blocked_by')
            if not any(x['id']==other['id'] for x in existing):api.call('/repos/'+full+'/issues/'+str(number)+'/dependencies/blocked_by','POST',{'issue_id':other['id']})
            readback=api.pages('/repos/'+full+'/issues/'+str(number)+'/dependencies/blocked_by')
            assert any(x['id']==other['id'] for x in readback)
            result['dependencies'].append({'blocked':blocked,'blocking':blocking,'result':'native-dependency-readback-verified'})
        except (RuntimeError,AssertionError) as exc:result['dependencies'].append({'blocked':blocked,'blocking':blocking,'result':'dependency-gap','error':str(exc)})
        dump(out/'applied.json',result)
    final=[i for i in api.pages('/repos/'+full+'/issues?state=all') if 'pull_request' not in i]
    expected={p['number'] for p in plan};final_by={i['number']:i for i in final}
    result.update(finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),planned_issues=len(plan),final_issue_count=len(final),v4_marker_count=sum(MARK in (i.get('body') or '') for i in final),new_since_plan=[i['number'] for i in final if i['number'] not in expected],api_writes=api.writes)
    result['final_body_verifications']=[{'number':p['number'],'expected_body_matches':sha(final_by.get(p['number'],{}).get('body'))==sha(p['body']),'has_v4_marker':MARK in (final_by.get(p['number'],{}).get('body') or '')} for p in plan]
    dump(out/'applied.json',result)
    if any(e['result'].endswith('gap') or e['result']=='concurrent-change-preserved' for e in result['events']):raise SystemExit('Reconciliation gaps preserved in journal')

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('mode',choices=['compile','apply']);ap.add_argument('--repo');ap.add_argument('--ref');a=ap.parse_args()
    if a.mode=='compile':compile_corpus()
    else:apply_repo(a.repo,a.ref)
