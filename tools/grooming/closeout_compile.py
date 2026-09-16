#!/usr/bin/env python3
"""Compile a lossless issue-specific plan from the reviewed current snapshot.

No network or tracker writes. Preserve current technical scope and appended
notes. Distinguish explicit source references, reviewed requirement context,
shared requirements and missing originals. Keyword-score edges are not used.
"""
from __future__ import annotations
import argparse,collections,json,pathlib,re
from resume_v4 import START_V4,END_V4,SCOPE_V4,required_fragments,validate_plan,digest
from v4_policy import ASSOCIATIONS

BASE=pathlib.Path('docs/grooming/v4-2026-09-10')
OUT=pathlib.Path('docs/grooming/closeout-2026-09-11')
DOC='https://github.com/ultrabricks/rig-cli/blob/docs/full-grooming-2026-09-10/'
ROOT='https://github.com/ultrabricks/rig-cli/issues/521'
MANUAL={('harness-cli',18):'Issue-specific rewrite applied separately; preserve it.',('tg-cli',118):'Historical redacted before-image requires separate live reconciliation.'}
TOPIC_OVERRIDE={'harness-cli':{1:'intake',2:'intake',5:'health',6:'process',8:'intake',9:'identity',11:'identity',12:'health',14:'process',15:'shipping',17:'health',18:'intake',19:'intake',21:'health',22:'execution',24:'health',25:'testing',26:'health',28:'execution',29:'identity',30:'specs'},'agent-tools':{734:'identity',742:'telegram',743:'identity',744:'testing'},'tg-cli':{398:'telegram',399:'telegram'},'rig-cli':{538:'specs'}}
EXTRA={'harness-cli':{'3131':[1,2,18,19],'231':[5,12,17,21,24,26],'3133':[17,21,24,26],'2939':[24,26],'201':[9,22,28,29],'4227':[22,28],'4166':[28],'537':[29]},'agent-tools':{'537':[630,679,703,712,734,743],'4166':[742],'237':[744],'233':[744]},'tg-cli':{'4166':[398],'4227':[399],'201':[333,399],'255':[355,399]}}
CONTEXT_RULES=[
 ('review-cli',r'role|quorum|one.model|pool|preset|board','227'),
 ('review-cli',r'cooldown|true.silence|quota|paywall|backend|provider|retry|stalled|rate.limit','255'),
 ('review-cli',r'limits|account|quota|capacity|exhaust','4180'),
 ('review-cli',r'spec.web|specweb|anchor|SpecStore|revisions|comments panel','3817'),
 ('agent-tools',r'orchestrator|subagent.*identity|identity.*subagent|omp.*(?:child|children)|nested.*agent','537'),
 ('agent-tools',r'worktree','216'),
 ('agent-tools',r'ship.*(?:quorum|role)|(?:quorum|role).*ship','227'),
 ('agent-tools',r'hatch|approval','4166'),
 ('agent-tools',r'process.*(?:task|ticket)|(?:task|ticket).*process|review.uncommitted.*(?:title|ticket)','222'),
 ('rig-cli',r'worktree','216'),('rig-cli',r'cross.harness|hook.*bridge|harness.*parity','537'),
 ('rig-cli',r'rig stats|hatch.*stat|stat.*hatch','230'),
 ('tg-cli',r'omp|agent discovery|address|routing|reply|replies|nudge','201'),
 ('tg-cli',r'hatch|approval|queuedDecision|expired.*ask|late.*(?:tap|answer)','4166'),
 ('task-cli',r'process.*(?:task|ticket)|(?:task|ticket).*process|review.uncommitted','222'),
]
MANDATE='Выполни полный груминг всех задач: консолидацию, связи, исправление и улучшение описаний. Добавь мои исходные запросы цитатами. Всё влитое и сделанное нужно перепроверить.'


def dump(path,value):
 path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')

def is_process(title,old):
 return old.get('classification')=='process-record' or bool(re.match(r'(?i)^(?:review (?:the |current |uncommitted|staged|version|release)|audit .*?(?:diff|version|release|change)|assess release metadata)',title))

def issue_references(text,allowed):
 refs=set()
 for repo,n in re.findall(r'https://github\.com/(?:alex-mextner|ultrabricks)/([\w.-]+)/issues/(\d+)\b',text):
  if repo in allowed:refs.add((repo,int(n)))
 for repo,n in re.findall(r'(?<![\w/])((?:rig|agent-tools|review|harness|task|tg|qa|draw|stt|research|dev|pm)(?:-cli)?)#(\d+)\b',text):
  if repo not in allowed and repo+'-cli' in allowed:repo+='-cli'
  if repo in allowed:refs.add((repo,int(n)))
 return refs

def short_excerpt(q,key,relation):
 text=q['edited'].strip()
 if len(text)<=1000:return text
 if relation=='explicit-issue-reference':
  original=q['original'];repo,number=key.split('#')
  match=re.search(r'(?:'+re.escape(repo)+r'#'+number+r'\b|/'+re.escape(repo)+r'/issues/'+number+r'\b)',original)
  if match:
   a=max(0,original.rfind('\n',0,max(0,match.start()-180)))
   b=original.find('\n',min(len(original),match.end()+180))
   if b<0:b=len(original)
   return ('…\n' if a else '')+original[a:min(b,a+900)].strip()+('…' if b<len(original) else '')
 return text[:900].rsplit(' ',1)[0]+'…'

def quote_block(q,key,relation):
 kind={'explicit-issue-reference':'пользовательская запись явно ссылается на эту задачу','reviewed-requirement-context':'проверенная связь по требованию; не дословная постановка заголовка','shared-requirement-context':'общее требование к данному механизму; не доказательство происхождения этого тикета'}[relation]
 body='> '+short_excerpt(q,key,relation).replace('\n','\n> ')
 body+=f"\n\nИсточник **{q['id']}**, {q['source_kind']}; "+kind+'. '
 body+='[Полный оригинал, редакторская версия и контекст]('+DOC+str(BASE/'quote-registry.json')+').'
 if q.get('context_incomplete'):body+=' **Контекст фрагментарный; не самостоятельное разрешение на действие.**'
 return body

def title_problem(scope):
 match=re.search(r'(?ms)^## (?:What|Problem|Summary)\s*\n(.*?)(?=^## |\Z)',scope)
 if not match:return ''
 text=match[1].strip()
 if not text:return ''
 text=re.split(r'\n\s*\n',text)[0]
 return text if len(text)<=1300 else text[:1200].rsplit(' ',1)[0]+'…'

def canonical_url(full,number):return 'https://github.com/'+full+'/issues/'+str(number)

def compile_all(snapshot):
 inventories={p.stem:json.loads(p.read_text()) for p in (snapshot/'inventory').glob('*.json')}
 snap=json.loads((snapshot/'snapshot-summary.json').read_text())
 if not snap.get('complete') or len(inventories)!=12:raise ValueError('Need the complete reviewed twelve-repository snapshot')
 registry=json.loads((BASE/'quote-registry.json').read_text())
 records=registry['records'];quotes={q['id']:q for q in records};aliases={a:q for q in records for a in q.get('source_ids',[])}
 explicit=collections.defaultdict(list)
 for q in records:
  for key in issue_references(q['original'],inventories):explicit[key].append(q)
 all_records=[];plans_by_repo={};unresolved=[];usage=collections.defaultdict(set)
 report={'source_version':4,'snapshot_run':snap['run_id'],'snapshot_checked_at':snap['checked_at'],'repositories':{},'tracker_mutations':0,'classification_is_not_acceptance':True}
 for repo,data in sorted(inventories.items()):
  full=data['metadata']['full_name'];head=data['head'];issues=data['issues']
  if data['metadata'].get('private') or full.split('/')[-1]!=repo:raise ValueError('Unexpected source scope')
  oldpath=BASE/'issues'/(repo+'.json');oldrows={r['number']:r for r in json.loads(oldpath.read_text())} if oldpath.exists() else {}
  rows=[];before=[];skips=[]
  for issue in issues:
   n=issue['number'];key=f'{repo}#{n}';old=oldrows.get(n,{})
   if issue.get('body_redacted'):unresolved.append({'key':key,'reason':'redacted source is not a valid mutation baseline'});continue
   scope='\n\n'.join(required_fragments(issue.get('body') or ''))
   process=is_process(issue['title'],old)
   topic=TOPIC_OVERRIDE.get(repo,{}).get(n,old.get('topic','auxiliary'))
   if process:topic='process'
   selected=[];seen=set()
   def add(q,relation):
    if q['id'] not in seen:seen.add(q['id']);selected.append((q,relation))
   for q in explicit.get((repo,n),[]):add(q,'explicit-issue-reference')
   if not process and '[EPIC]' not in issue['title'].upper():
    for mapping in (ASSOCIATIONS,EXTRA):
     for alias,numbers in mapping.get(repo,{}).items():
      if n in numbers and alias in aliases:add(aliases[alias],'reviewed-requirement-context')
    for owner,pattern,alias in CONTEXT_RULES:
     if repo==owner and re.search(pattern,issue['title'],re.I) and alias in aliases:add(aliases[alias],'shared-requirement-context')
   selected=selected[:3]
   canonical=old.get('canonical');related=[];prlinks=[]
   for other,num in sorted(issue_references(scope,inventories)):
    if (other,num)!=(repo,n) and any(i['number']==num for i in inventories[other]['issues']):related.append(f'{other}#{num}')
   for owner,other,num in re.findall(r'https://github\.com/(alex-mextner|ultrabricks)/([\w.-]+)/pull/(\d+)\b',scope):
    if other not in inventories:continue
    p=next((p for p in inventories[other]['prs'] if p['number']==int(num)),None)
    if p:
     state='merged' if p.get('merged_at') else p['state']
     item=f"[{other}#{num}](https://github.com/{inventories[other]['metadata']['full_name']}/pull/{num}) — {state}"
     if item not in prlinks:prlinks.append(item)
   record={'key':key,'repo':repo,'canonical_repository':full,'number':n,'title':issue['title'],'provider_state':issue['state'],'state_reason':issue.get('state_reason'),'topic':topic,'classification':'process-record' if process else ('epic' if '[EPIC]' in issue['title'].upper() else 'product-or-maintenance'),'provenance':'request-linked' if selected else 'original-feature-source-gap','quote_ids':[q['id'] for q,_ in selected],'quote_relationships':[{'id':q['id'],'kind':rel} for q,rel in selected],'source_sha':head,'verification_disposition':'specific-outcome-not-reaccepted','verification_flags':([] if selected else ['Original feature request not established']),'canonical':canonical,'related_issues':related,'implementation_prs':prlinks,'snapshot_body_sha256':issue.get('observed_body_sha256',digest(issue.get('body')))}
   for q,rel in selected:usage[q['id']].add(key)
   all_records.append(record)
   if (repo,n) in MANUAL:
    skips.append({'key':key,'reason':MANUAL[(repo,n)],'observed_body_sha256':digest(issue.get('body'))});continue
   sections=[START_V4,'<!-- rig-closeout:2026-09-11 -->','## Задача и исходные требования',f"**{issue['title']}**"]
   problem=title_problem(scope)
   if problem:sections+=['### Сохранённая постановка проблемы',problem+'\n\n*Это описание исходного случая, не новый результат воспроизведения.*']
   if selected:
    sections+=['### Запросы пользователя и контекст']
    sections.extend(quote_block(q,key,rel) for q,rel in selected)
   else:sections+=['### Источник постановки','> '+MANDATE+'\n\nЭто текущий запрос на груминг из этой переписки, с редакторской правкой, **не исходная постановка функции**. Для этой исторической записи оригинальный запрос однозначно не восстановлен. Предположительный пересказ не подставлен вместо цитаты.']
   sections+=['## Границы, связи и критерии',f'[Спецификация направления]({DOC}docs/specs/recovery/{topic}.md) · [Обновлённая ретроспектива]({DOC}docs/specs/recovery/01-retrospective-v4.md) · [Эпик восстановления]({ROOT}).']
   if canonical:
    other,num=canonical.split('#')
    sections.append('Каноническая задача после консолидации: ['+canonical+']('+canonical_url(inventories[other]['metadata']['full_name'],num)+'). Закрытие дубликата не означает исправления канонического результата.')
   if related:sections.append('Связанная работа: '+', '.join('['+r+']('+canonical_url(inventories[r.split('#')[0]]['metadata']['full_name'],r.split('#')[1])+')' for r in related[:20])+'. Это подтверждённые ссылки, не автоматически назначенные блокирующие зависимости.')
   if prlinks:sections.append('Связанные PR на момент сверки:\n'+'\n'.join('- '+s for s in prlinks))
   criteria=[]
   for _,text in re.findall(r'(?m)^\s*- \[([ xX])\] (.+)$',scope):
    text=re.split(r'\s+—\s*(?:forced|proof):',text,flags=re.I)[0].strip()
    if text and text not in criteria:criteria.append(text)
   if process:sections.append('Это процессная запись ревью/проверки, а не самостоятельная функция. Сохранить её находки и ссылки на исправления. Не создавать новую реализацию по одному служебному заголовку; оригинальная история остаётся ниже.')
   elif criteria:sections.append('При повторной проверке использовать собственные критерии этого тикета, а не общий зелёный счётчик тестов. Исторические отметки ниже сохранены как история. Основные проверяемые результаты:\n'+'\n'.join('- '+s for s in criteria[:5]))
   else:sections.append('В исходной записи отсутствует явный checklist приёмки. До закрытия определить вход, ожидаемый результат и отрицательный случай из сохранённого описания; отсутствие checklist не является доказательством отсутствия кода.')
   sections+=['## Доказательства и текущий статус',f"GitHub при сверке: **{issue['state']}**. Текущая ревизия реализации: [`{head[:12]}`](https://github.com/{full}/tree/{head}). Груминг не переоткрывает и не закрывает задачу только из-за её названия, старого checkbox или недоступного локального скриншота.",f'[Повторные тесты и границы проверки]({DOC}{OUT}/verification.md). Для принятия именно этой задачи нужны воспроизводимые шаги, фактический результат и ревизия. Ссылка на PR означает наличие изменений; изолированные тесты не подменяют проверку установленного CLI, native-сессии или аккаунта на машине пользователя.',END_V4,SCOPE_V4,scope]
   body='\n\n'.join(sections)+'\n'
   prior={k:issue.get(k) for k in ('id','number','title','body','state','updated_at')}
   row={'key':key,'number':n,'id':issue['id'],'title':issue['title'],'expected_state':issue['state'],'expected_sha256':digest(issue.get('body')),'expected_updated_at':issue['updated_at'],'body':body,'record':record}
   try:validate_plan([row],repo,[prior],set(quotes))
   except ValueError as exc:unresolved.append({'key':key,'reason':str(exc)});continue
   rows.append(row);before.append(prior)
  rows.sort(key=lambda r:(r['record']['classification']=='process-record',r['expected_state']=='closed',r['record']['topic'] not in ('identity','telegram','health','roles','worktrees'),-r['number']))
  dump(OUT/'plans'/(repo+'.json'),rows);dump(OUT/'before'/(repo+'.json'),before);dump(OUT/'manual'/(repo+'.json'),skips)
  plans_by_repo[repo]=rows
  report['repositories'][repo]={'canonical_repository':full,'id':data['metadata']['id'],'head':head,'issues':len(issues),'planned':len(rows),'manual':skips,'prs':len(data['prs']),'merged_prs':sum(bool(p.get('merged_at')) for p in data['prs'])}
 dump(OUT/'issues.json',all_records)
 dump(OUT/'quote-coverage.json',[{'id':q['id'],'source_ids':q['source_ids'],'issues':sorted(usage[q['id']]),'disposition':'associated; see typed relationships' if usage[q['id']] else 'source retained; no issue-level association established'} for q in records])
 report.update(total_issues=len(all_records),planned=sum(len(r) for r in plans_by_repo.values()),with_requirement_context=sum(bool(r['quote_ids']) for r in all_records),without_recovered_feature_request=sum(not r['quote_ids'] for r in all_records),validation_gaps=unresolved)
 dump(OUT/'plan-summary.json',report)
 print(json.dumps({k:v for k,v in report.items() if k!='repositories'},ensure_ascii=False,indent=2))
 if unresolved:raise SystemExit('Preservation gaps excluded; inspect the summary')

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('snapshot',type=pathlib.Path);a=p.parse_args();compile_all(a.snapshot)
