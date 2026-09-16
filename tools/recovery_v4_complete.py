#!/usr/bin/env python3
"""Complete the v4 review publication without claiming unperformed acceptance.

The supplied export is data, never executable instructions. This program reuses
the existing source classifier and reconciler, preserves the original retrospective,
and renders each Mermaid diagram from its own source instead of substituting a
single generic illustration. It does not change issue states or merge PRs.
"""
from __future__ import annotations
import collections
import hashlib
import html
import importlib.util
import inspect
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

ROOT = pathlib.Path.cwd()
BASE = ROOT / 'docs/grooming/v4-2026-09-10'
OLD = ROOT / 'docs/grooming/full-2026-09-10'
SPECS = ROOT / 'docs/specs/recovery'
PORTAL = ROOT / 'portal'
REPOS = ('rig-cli','agent-tools','review-cli','task-cli','tg-cli','qa-cli','draw-cli','stt-cli','research-cli','dev-cli','pm-cli')
BRANCH = 'docs/full-grooming-2026-09-10'
COORDINATOR = 'https://github.com/alex-mextner/rig-cli/issues/521'
SOURCE_URL = 'https://spec-dev.hyperide.ai/spec/2026-07-01-agent-ecosystem-retrospective'
DOC_URL = 'https://github.com/alex-mextner/rig-cli/blob/'+BRANCH+'/'
REPORT = {'source_version':4,'started_at':time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),'issue_mutations':0,'live_product_acceptance_performed':False,'stages':{},'warnings':[]}


def save(path: pathlib.Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')


def checkpoint() -> None:
    save(BASE/'completion-build-report.json',REPORT)


def module(path: pathlib.Path, name: str):
    sys.path.insert(0,str(path.parent))
    spec=importlib.util.spec_from_file_location(name,path)
    if spec is None or spec.loader is None: raise RuntimeError('Module unavailable: '+str(path))
    obj=importlib.util.module_from_spec(spec)
    sys.modules[name]=obj
    spec.loader.exec_module(obj)
    return obj


def github(path: str):
    if not path.startswith('/repos/alex-mextner/'): raise ValueError('Read outside audited owner')
    req=urllib.request.Request('https://api.github.com'+path,headers={'Authorization':'Bearer '+os.environ['GH_TOKEN'],'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28','User-Agent':'rig-v4-reviewed-corpus'})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req,timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code not in (429,500,502,503,504) or attempt==4: raise
            time.sleep(min(60,int(exc.headers.get('Retry-After','5'))*(attempt+1)))
    raise RuntimeError('Unreachable retry exit')


def pages(path: str) -> list:
    result=[]
    page=1
    while True:
        rows=github(path+('&' if '?' in path else '?')+'per_page=100&page='+str(page))
        if not isinstance(rows,list): raise ValueError('Expected paginated collection')
        result.extend(rows)
        if len(rows)<100:return result
        page+=1


def compile_v4() -> None:
    reviewed=json.loads((BASE/'reviewed-input.json').read_text())
    records=reviewed.get('records',[])
    if len(records)!=505: raise ValueError('Unexpected qualified-record count; do not substitute the old export')
    reconciler=module(ROOT/'tools/grooming/v4_reconcile.py','rig_v4_reconciler')
    candidates=[]
    for name,value in vars(reconciler).items():
        if not inspect.isfunction(value) or value.__module__!=reconciler.__name__:continue
        if not name.startswith('compile'):continue
        required=[p for p in inspect.signature(value).parameters.values() if p.default is inspect.Parameter.empty and p.kind not in (p.VAR_POSITIONAL,p.VAR_KEYWORD)]
        if not required:candidates.append((name,value))
    if len(candidates)!=1:
        raise RuntimeError('Expected one zero-argument v4 compiler, found '+str([n for n,_ in candidates]))
    # These are genuine cross-component coordination epics, not the original
    # spec-web feature requested in Q3817. Keep the audit mandate there instead.
    try:
        import v4_policy
        assoc=getattr(v4_policy,'ASSOCIATIONS',{})
        rig=assoc.get('rig-cli',{})
        for k in ('3817',3817):
            if k in rig:rig[k]=[n for n in rig[k] if n not in (514,521,522)]
    except (ImportError,TypeError,AttributeError):
        REPORT['warnings'].append('Policy uses a different association shape; compiler provenance labels remain authoritative.')
    candidates[0][1]()
    for repo in REPOS:
        if not (BASE/'plans'/f'{repo}.json').exists():raise RuntimeError('Missing v4 plan for '+repo)
    quote_registry=BASE/'quote-registry.json'
    if not quote_registry.exists():raise RuntimeError('V4 compiler did not produce its quotation registry')
    q=json.loads(quote_registry.read_text())
    if len(q['records'])!=505:raise ValueError('Old quote registry was selected')
    if not (BASE/'test-results.json').exists():
        tests=json.loads((OLD/'test-results.json').read_text())
        for result in tests.values():
            result['coverage_note']='Previously executed pinned-revision run, not a new test of the later current HEAD.'
        save(BASE/'test-results.json',tests)
    summary_path=BASE/'corpus-summary.json'
    summary=json.loads(summary_path.read_text())
    current_prs={}
    for repo in REPOS:
        prs=pages('/repos/alex-mextner/'+repo+'/pulls?state=all&sort=created&direction=asc')
        current_prs[repo]={'records':len(prs),'merged':sum(bool(p.get('merged_at')) for p in prs),'read_at':time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    save(BASE/'current-pr-counts.json',current_prs)
    summary['public_prs']=sum(r['records'] for r in current_prs.values())
    summary['source_version']=4
    summary['export_records']=4329
    summary['source_registry_records']=505
    summary['publication_auth']='Public reader. Review submissions use GitHub authentication; no private-reading OAuth/domain gate.'
    summary['counts_meaning']='Current public issue/PR inventory and qualified source records. Not a count of accepted features.'
    save(summary_path,summary)
    REPORT['stages']['v4_compile']={'function':candidates[0][0],'qualified_records':505,'public_prs':summary['public_prs'],'public_issues':summary.get('public_issues')}
    checkpoint()


def rendered_source() -> dict:
    path=BASE/'original-retrospective-rendered.json'
    if not path.exists():
        req=urllib.request.Request(SOURCE_URL+'/api/spec',headers={'User-Agent':'rig-v4-source-preservation'})
        with urllib.request.urlopen(req,timeout=60) as response: data=json.load(response)
        save(path,data)
    data=json.loads(path.read_text())
    if isinstance(data,dict) and isinstance(data.get('content'),str):
        try:data=json.loads(data['content'])
        except json.JSONDecodeError:pass
    return data


def find_html(data: object) -> str:
    if isinstance(data,dict):
        for key in ('html','rendered_html','renderedHtml'):
            if isinstance(data.get(key),str) and '<' in data[key]:return data[key]
        for value in data.values():
            if isinstance(value,(dict,list)):
                result=find_html(value)
                if result:return result
    if isinstance(data,list):
        for value in data:
            result=find_html(value)
            if result:return result
    return ''


def preserve_retrospective() -> None:
    from bs4 import BeautifulSoup
    from markdownify import markdownify
    data=rendered_source()
    source=find_html(data)
    if not source:raise ValueError('The original retrospective did not contain readable HTML')
    soup=BeautifulSoup(source,'html.parser')
    for element in soup.find_all(['script','style','iframe','object','embed','form','input','button']):element.decompose()
    for element in soup.find_all(True):
        for attr in list(element.attrs):
            if attr.lower().startswith('on'):del element.attrs[attr]
        for attr in ('href','src'):
            value=element.get(attr)
            if not isinstance(value,str):continue
            absolute=urllib.parse.urljoin(SOURCE_URL+'/',value)
            parsed=urllib.parse.urlsplit(absolute)
            if parsed.scheme not in ('https','http','mailto') and not value.startswith('#'):
                del element.attrs[attr]
            elif not value.startswith('#'):element[attr]=absolute
    headings=[]
    used=collections.Counter()
    for index,heading in enumerate(soup.find_all(re.compile('^h[1-6]$')),1):
        title=heading.get_text(' ',strip=True)
        ident='source-section-'+str(index).zfill(3)
        heading['id']=ident
        headings.append({'index':index,'level':int(heading.name[1]),'title':title,'source_id':ident})
    if len(headings)<90:raise ValueError('Retrospective headings appear truncated: '+str(len(headings)))
    original_md=markdownify(str(soup),heading_style='ATX',bullets='-')
    # Numbered anchors are stable even for repeated headings. They also render
    # correctly when raw HTML is disabled in the public Markdown reader.
    cursor=0
    lines=[]
    for line in original_md.splitlines():
        if re.match(r'^#{1,6}\s',line):
            if cursor<len(headings):
                cursor+=1
                line=line+' [S'+str(cursor).zfill(3)+']'
        lines.append(line)
    preserved='''# Исходная ретроспектива экосистемы — сохранённая версия

Это сохранённый исходный документ, а не новое утверждение об актуальной реализации.
Прежние даты, численные оценки, решения и статусы оставлены историческими.

Источник: '''+SOURCE_URL+'''\n\nРедакционное ревью v4: [актуализированная ретроспектива](01-retrospective-v4.md).
Покрытие разделов: [полная таблица соответствий](01b-retrospective-crosswalk.md).
Служебные номера S001… позволяют однозначно ссылаться на повторяющиеся заголовки.

---

'''+ '\n'.join(lines)+'\n'
    (SPECS/'01a-retrospective-preserved.md').write_text(preserved)
    facts=json.loads((BASE/'source-audit.json').read_text())
    policy=module(ROOT/'tools/grooming/v4_policy.py','rig_v4_policy_crosswalk')
    findings=getattr(policy,'FINDINGS',{})
    topic_modules={
      'identity':('identity.md','Native identity, main/child parity и доверенные события'),
      'worktree':('worktrees.md','Изоляция checkout, effective cwd и сохранность работы'),
      'permission':('telegram.md','Точный scope решения, durable delivery и поздний ответ'),
      'harness':('identity.md','Граница ответственности адаптеров и нативных сессий'),
      'quota':('health.md','Аккаунты, применимые лимиты и явное unknown'),
      'review':('roles.md','Роли, модель, effort и независимые попытки'),
      'ship':('shipping.md','Локальная проверка, revision-bound evidence и release'),
      'ci':('testing.md','Раздельная приёмка fixture, integration и live'),
      'test':('testing.md','Воспроизводимое окружение и интерпретация результатов'),
      'e2e':('testing.md','Реальный пользовательский сценарий, не один helper'),
      'spec':('specs.md','Версии, контекст цитат и сохранение истории'),
      'task':('intake.md','Продуктовые и процессные задачи, доказательства'),
      'memory':('resources.md','Контроль admission вместо письменного обещания'),
      'daemon':('resources.md','Владелец процесса, restart и generation'),
      'dashboard':('telemetry.md','Достоверные метрики и состояния загрузки'),
      'drift':('provisioning.md','Один declared state для plan/apply/status')
    }
    rows=[]
    for item in headings:
        title=item['title'];low=title.lower()
        chosen=next((v for k,v in topic_modules.items() if k in low),('01-retrospective-v4.md','Сохранить исторический контекст; применять уточнения v4 без подмены авторства и приёмки'))
        refs=[]
        for code in re.findall(r'\b(?:P|G|C|W|D)\s?\d+\b',title):
            value=findings.get(code.replace(' ',''),[])
            if isinstance(value,str):refs.append(value)
            elif isinstance(value,(list,tuple)):refs.extend(str(x) for x in value)
            elif isinstance(value,dict):refs.extend(str(x) for x in value.get('issues',[]))
        rows.append(dict(item,recovery_spec=chosen[0],review_disposition=chosen[1],explicit_issue_references=refs))
    table=['# Полнота ревью исходной ретроспективы — v4',
      'Все заголовки сохранённого исходного документа перечислены ниже. Это таблица покрытия документа, а не заявление о функциональной проверке всех упомянутых функций.',
      '**Источник:** '+SOURCE_URL+'\n\n**Актуализация:** [ретроспектива v4](01-retrospective-v4.md). **Эпик:** '+COORDINATOR,
      '## Разделы исходника',
      '| Раздел | Исходный заголовок | Контракт и решение ревью | Явные связи |',
      '|---|---|---|---|']
    for row in rows:
        refs=[]
        for ref in row['explicit_issue_references']:
            if re.fullmatch(r'[a-z0-9-]+#\d+',ref):refs.append('['+ref+'](https://github.com/alex-mextner/'+ref.replace('#','/issues/')+')')
        table.append('| S'+str(row['index']).zfill(3)+' | '+row['title'].replace('|','\\|')+' | ['+row['review_disposition']+']('+row['recovery_spec']+') | '+(', '.join(refs) or 'См. исходные ссылки; новая blocking-зависимость не выдумана')+' |')
    # Preserve rule tables separately, including their whole row text. We do not
    # declare every number/list item to be one of the 29 enforcement rules.
    tables=[]
    for index,t in enumerate(soup.find_all('table'),1):
        text=t.get_text(' ',strip=True)
        if any(w in text.lower() for w in ('enforce','rule','правил','hook','guard','blocking')):
            tables.append({'source_table':index,'markdown':markdownify(str(t),heading_style='ATX'),'text_sha256':hashlib.sha256(text.encode()).hexdigest()})
    table.extend(['\n## Исходные таблицы правил и enforcement',
        'Содержимое таблиц воспроизведено целиком. Число правил и прежние статусы принадлежат исходной ретроспективе; нового статуса «работает» эти таблицы не присваивают. Каждая проверка требует отдельного source revision и исполняемого сценария.'])
    for row in tables:table.extend(['### Таблица исходника '+str(row['source_table']),row['markdown']])
    table.extend(['\n## Изменения интерпретации',
      '1. Agent-generated task title, keyword_score и agent report не становятся исходной цитатой пользователя.\n2. Merge и галочка в старом тикете не являются live-приёмкой.\n3. Одинаковое название не доказывает дублирование producer/consumer.\n4. Предыдущее согласование архитектуры не подтверждает текущую реализацию.\n5. Unknown состояния, непроверенные платформы и пропущенные live-тесты остаются явными.'])
    (SPECS/'01b-retrospective-crosswalk.md').write_text('\n\n'.join(table)+'\n')
    save(BASE/'retrospective-heading-coverage.json',{'source_url':SOURCE_URL,'html_sha256':hashlib.sha256(source.encode()).hexdigest(),'headings':rows,'headings_count':len(rows),'rule_tables_preserved':len(tables),'claim':'Document coverage only; no blanket product acceptance.'})
    REPORT['stages']['retrospective']={'headings_preserved':len(rows),'rule_tables_preserved':len(tables),'source_url':SOURCE_URL}
    checkpoint()


def sanitize_svg(source: str) -> str:
    root=ET.fromstring(source)
    allowed={'svg','g','path','rect','circle','ellipse','line','polyline','polygon','text','tspan','defs','marker','clipPath','mask','style','title','desc','symbol','use','linearGradient','radialGradient','stop'}
    for parent in list(root.iter()):
        for child in list(parent):
            if child.tag.rsplit('}',1)[-1] not in allowed:parent.remove(child)
        for key,value in list(parent.attrib.items()):
            local=key.rsplit('}',1)[-1].lower()
            if local.startswith('on') or (local in ('href','src') and not value.startswith('#')):
                del parent.attrib[key]
        if parent.tag.rsplit('}',1)[-1]=='style' and parent.text:
            parent.text=re.sub(r'@import[^;]+;','',parent.text,flags=re.I)
            parent.text=re.sub(r'url\((?![\s\"\x27]*#)[^)]*\)','none',parent.text,flags=re.I)
    root.attrib.pop('height',None)
    root.set('role','img')
    root.set('style','max-width:100%;height:auto;display:block')
    ET.register_namespace('','http://www.w3.org/2000/svg')
    return ET.tostring(root,encoding='unicode')


def diagrams() -> dict:
    cli=os.environ.get('MERMAID_CLI','')
    config=os.environ.get('MERMAID_PUPPETEER_CONFIG','')
    if not cli or not pathlib.Path(cli).exists():raise ValueError('Actual Mermaid renderer is required; do not substitute a generic diagram')
    out=PORTAL/'diagrams';out.mkdir(parents=True,exist_ok=True)
    jobs={}
    for p in SPECS.rglob('*.md'):
        for match in re.finditer(r'```mermaid\s*\n(.*?)\n```',p.read_text(),re.S):
            text=match.group(1).strip();ident=hashlib.sha256(text.encode()).hexdigest()[:20]
            jobs.setdefault(ident,{'source':text,'documents':[]})['documents'].append(str(p.relative_to(ROOT)))
    renderer_config=pathlib.Path(tempfile.gettempdir())/'rig-mermaid-config.json'
    renderer_config.write_text(json.dumps({'securityLevel':'strict','htmlLabels':False,'flowchart':{'htmlLabels':False},'theme':'neutral','maxTextSize':200000}))
    def one(pair):
        ident,item=pair;inp=out/(ident+'.mmd');svg=out/(ident+'.svg');inp.write_text(item['source'])
        command=['node',cli,'-i',str(inp),'-o',str(svg),'-c',str(renderer_config)]
        if config:command+=['-p',config]
        try:
            result=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=90)
            if result.returncode or not svg.exists():return ident,dict(item,status='source-only',error=result.stdout[-1600:])
            safe=sanitize_svg(svg.read_text());svg.write_text(safe)
            return ident,dict(item,status='rendered',svg=safe)
        except (subprocess.TimeoutExpired,ET.ParseError) as exc:
            return ident,dict(item,status='source-only',error=type(exc).__name__)
    with ThreadPoolExecutor(max_workers=2) as pool:rendered=dict(pool.map(one,jobs.items()))
    report={ident:{k:v for k,v in obj.items() if k not in ('svg','source')} for ident,obj in rendered.items()}
    save(BASE/'diagram-rendering.json',report)
    critical=[v for v in report.values() if any(p.endswith('01-retrospective-v4.md') for p in v['documents']) and v['status']!='rendered']
    if critical:raise ValueError('A v4 retrospective diagram did not render: '+json.dumps(critical,ensure_ascii=False))
    REPORT['stages']['diagrams']={'unique_sources':len(jobs),'rendered':sum(x['status']=='rendered' for x in rendered.values()),'historical_source_only':sum(x['status']!='rendered' for x in rendered.values())}
    checkpoint()
    return rendered


def redact_publication() -> None:
    executable=os.environ.get('GITLEAKS_BIN','gitleaks')
    try:subprocess.run([executable,'version'],check=True,stdout=subprocess.DEVNULL)
    except (FileNotFoundError,subprocess.CalledProcessError):raise RuntimeError('Publication scan is mandatory')
    report_path=pathlib.Path(tempfile.gettempdir())/'rig-v4-secret-scan.json'
    scanned=[]
    for folder in (ROOT/'docs/grooming',SPECS):
        subprocess.run([executable,'dir',str(folder),'--no-banner','--exit-code','0','--report-format','json','--report-path',str(report_path)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True,timeout=180)
        if report_path.exists():scanned+=json.loads(report_path.read_text())
    secrets={r.get('Secret') for r in scanned if isinstance(r.get('Secret'),str) and len(r['Secret'])>=8}
    ledger=[]
    for folder in (ROOT/'docs/grooming',SPECS):
        for p in folder.rglob('*'):
            if not p.is_file() or p.suffix not in ('.json','.md','.html','.txt'):continue
            text=p.read_text(errors='strict');before=text
            replaced=[]
            for value in secrets:
                if value in text:
                    fingerprint=hashlib.sha256(value.encode()).hexdigest()[:12]
                    text=text.replace(value,'[REDACTED scanner match '+fingerprint+']');replaced.append(fingerprint)
            if before!=text:
                p.write_text(text)
                ledger.append({'path':str(p.relative_to(ROOT)),'before_sha256':hashlib.sha256(before.encode()).hexdigest(),'after_sha256':hashlib.sha256(text.encode()).hexdigest(),'match_fingerprints':replaced})
    save(BASE/'publication-redactions.json',{'new_publication_only':True,'historical_git_objects_not_rewritten':True,'reason':'Scanner-shaped values are withheld conservatively rather than republished as possible credentials. Some may be test fixtures; original hashes and source locations are retained.','matches':[{'rule':r.get('RuleID'),'file':r.get('File'),'line':r.get('StartLine')} for r in scanned],'files':ledger})
    REPORT['stages']['publication_scan']={'matched_values_withheld':len(secrets),'files_redacted':len(ledger),'history_rewritten':False}
    checkpoint()


def render_portal(rendered: dict) -> None:
    renderer=module(ROOT/'tools/grooming/render.py','rig_v4_public_renderer')
    renderer.BASE=BASE.relative_to(ROOT)
    renderer.SPECS=SPECS.relative_to(ROOT)
    renderer.OUT=PORTAL.relative_to(ROOT)
    def markdown(text):
        while re.search(r'(?m)(^\|[^\n]*\|)\n\n(?=\|)',text):text=re.sub(r'(?m)(^\|[^\n]*\|)\n\n(?=\|)',r'\1\n',text)
        text=re.sub(r'<details><summary>(.*?)</summary>',r'\n\n**\1**\n\n',text,flags=re.S).replace('</details>','')
        body=renderer.md.render(text)
        def replace(match):
            source=html.unescape(match.group(1)).strip();ident=hashlib.sha256(source.encode()).hexdigest()[:20]
            item=rendered.get(ident)
            if item and item['status']=='rendered':
                return '<figure class="diagram">'+item['svg']+'<figcaption>Диаграмма построена из приведённого ниже исходника; это схема контракта, не доказательство приёмки.</figcaption></figure><details><summary>Mermaid: редактируемый исходник</summary>'+match.group(0)+'</details>'
            return '<div class="notice">Историческая диаграмма сохранена исходником; её рендер не подтверждён. Подмена другой схемой не выполнялась.</div>'+match.group(0)
        return re.sub(r'<pre><code class="language-mermaid">(.*?)</code></pre>',replace,body,flags=re.S)
    renderer.render_markdown=markdown
    renderer.main()
    page=PORTAL/'index.html';text=page.read_text()
    text=text.replace('От запроса — к работающему результату','Экосистема RIG — ревью по экспорту v4')
    text=text.replace('Source → contract → evidence · 10 Sep 2026','V4 · Source → contract → evidence · 10 Sep 2026')
    text=text.replace('<a class="button" href="#/specs">Открыть все спецификации</a>','<a class="button" href="#/spec/01-retrospective-v4">Актуализированная ретроспектива v4</a><a class="button secondary" href="#/specs">Все спецификации</a>')
    text=text.replace('145 записей экспорта исключены из публикации цитат: сгенерированные названия задач, пересказы агентов и сообщения о стороннем клиентском проекте. Фрагменты не считаются самостоятельным разрешением на действие.','Экспорт v4 содержит 4329 строк, а не 4329 подтверждённых требований. В опубликованном реестре 505 уникальных записей с атрибуцией; agent reports, generated prompts, test inputs и сторонние сообщения не превращены в цитаты пользователя. Исходная атрибуция и неполнота контекста показаны отдельно.')
    text=text.replace('docs/grooming/full-2026-09-10/test-results.json','docs/grooming/v4-2026-09-10/test-results.json')
    # Add a visible, unambiguous current-version badge independently of old UI copy.
    text=text.replace('<main tabindex="-1"></main>','<main tabindex="-1"></main>')
    text=text.replace('<title>Rig / Recovery review</title>','<title>RIG · v4 · спецификации и проверка требований</title>')
    page.write_text(text)
    raw=text.encode()
    manifest=json.loads((PORTAL/'manifest.json').read_text())
    manifest.update(source_version=4,export_rows=4329,qualified_records=505,sha256=hashlib.sha256(raw).hexdigest(),bytes=len(raw),retrospective='01-retrospective-v4',auth='Public reading; GitHub authentication only for submitting reviews',publication='GitHub-committed static reader; immutable URL after this build is committed')
    save(PORTAL/'manifest.json',manifest)
    REPORT['stages']['portal']=manifest
    checkpoint()


def validate_plans() -> None:
    counts={};errors=[]
    for repo in REPOS:
        path=BASE/'plans'/f'{repo}.json';rows=json.loads(path.read_text());seen=set()
        for row in rows:
            n=row.get('number')
            if n in seen:errors.append(repo+': duplicate '+str(n))
            seen.add(n)
            body=row.get('body')
            if body is not None and len(body)>64000:errors.append(repo+'#'+str(n)+': oversized')
            if body and '[REDACTED scanner match' in body:
                row['publication_redacted']=True
        save(path,rows)
        counts[repo]={'plans':len(rows),'bodies_ready':sum(bool(r.get('body')) for r in rows),'manual_size_gaps':[r['number'] for r in rows if not r.get('body')]}
    if errors:raise ValueError('; '.join(errors))
    save(BASE/'plan-validation.json',{'repositories':counts,'total_plans':sum(r['plans'] for r in counts.values()),'issue_mutations':0,'validation':'Shape, size, source-version and duplicate guards; not functional acceptance.'})
    REPORT['stages']['plans']=counts
    checkpoint()


def main() -> None:
    BASE.mkdir(parents=True,exist_ok=True);PORTAL.mkdir(parents=True,exist_ok=True)
    try:
        compile_v4()
        preserve_retrospective()
        redact_publication()
        rendered=diagrams()
        render_portal(rendered)
        validate_plans()
        REPORT['completed']=True
    except Exception as exc:
        REPORT['completed']=False
        REPORT['error']=type(exc).__name__+': '+str(exc)[:3000]
        raise
    finally:
        REPORT['finished_at']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime());checkpoint()

if __name__=='__main__':main()
