#!/usr/bin/env python3
"""Static, source-linked reader. Data files are inputs, never executable code.

All pages have complete server-readable HTML. Filters progressively enhance it;
no app token, remote script, login-looking gate or private transcript is embedded.
"""
from __future__ import annotations
import argparse,hashlib,html,importlib.util,json,pathlib,posixpath,re,subprocess,textwrap,urllib.parse
from markdown_it import MarkdownIt

ROOT=pathlib.Path.cwd()
SPECS=ROOT/'docs/specs/recovery'
DATA=ROOT/'docs/grooming/closeout-2026-09-11'
OUT=ROOT/'portal/closeout'
REF=''
md=MarkdownIt('commonmark',{'html':False}).enable('table')
CSS='''
:root{font-family:ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#22302b;background:#f6f7f5;--ink:#22302b;--muted:#67736b;--line:#d9e1da;--accent:#1c6959;--soft:#e8f0eb;font-synthesis:none}*{box-sizing:border-box}body{margin:0}a{color:var(--accent);text-underline-offset:3px}a:hover{text-decoration:underline}button,input,select{font:inherit}button,a,input,select,summary{outline-offset:4px}button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{outline:3px solid #489b84}header{height:68px;display:flex;align-items:center;justify-content:space-between;padding:0 30px;border-bottom:1px solid var(--line);background:#fff;position:sticky;top:0;z-index:10;gap:15px}.brand{font-size:20px;font-weight:750;color:var(--ink);letter-spacing:-.04em;text-decoration:none}.brand span{font-weight:450;color:var(--muted)}.toplinks{display:flex;gap:22px;align-items:center;font-size:13px}.toplinks a{text-decoration:none;color:var(--muted)}.toplinks a.active{color:var(--accent);font-weight:700}.shell{display:grid;grid-template-columns:260px minmax(0,1fr);min-height:calc(100vh - 68px)}aside{padding:26px 17px;background:#fcfdfb;border-right:1px solid var(--line);height:calc(100vh - 68px);position:sticky;top:68px;overflow:auto}.navlabel,.eyebrow{font-size:10px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}.navlabel{margin:18px 10px 9px}.nav a{display:block;border-radius:7px;padding:9px 11px;text-decoration:none;color:#52635a;font-size:12px;line-height:1.4;margin:3px 0}.nav a.active{background:var(--soft);color:var(--accent);font-weight:650}.nav a:hover{background:#edf2ee}main{width:100%;max-width:1480px;margin:0 auto;padding:38px 44px 70px;min-width:0}.eyebrow{color:var(--accent)}h1{font-size:clamp(28px,3.6vw,46px);line-height:1.13;letter-spacing:-.045em;margin:12px 0 18px;overflow-wrap:anywhere}h2{font-size:24px;line-height:1.3;letter-spacing:-.025em;margin:32px 0 14px;scroll-margin-top:96px}h3{font-size:17px;line-height:1.4;margin:24px 0 10px;scroll-margin-top:96px}h4{scroll-margin-top:96px}p,li{font-size:14px;line-height:1.72}ul,ol{padding-left:23px}.intro{font-size:16px;color:var(--muted);max-width:850px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:14px}.card{display:block;border:1px solid var(--line);border-radius:11px;background:#fff;padding:21px;color:var(--ink);text-decoration:none}.card:hover{border-color:#7fa494;text-decoration:none}.card h3{margin:10px 0;font-size:17px}.card p{font-size:12px;color:var(--muted);margin:7px 0}.statgrid{display:grid;grid-template-columns:repeat(4,1fr);gap:13px;margin:28px 0}.stat{background:#fff;border:1px solid var(--line);border-radius:11px;padding:20px}.stat strong{display:block;font-size:30px;letter-spacing:-.045em}.stat span{font-size:12px;color:var(--muted)}.notice{padding:14px 18px;border:1px solid #e1d6b9;background:#fff9ec;border-radius:9px;font-size:13px;line-height:1.65;margin:21px 0}.notice.good{background:#edf5ef;border-color:#d2e1d3}.badge{display:inline-block;padding:4px 7px;font-size:10px;line-height:1.4;border-radius:5px;border:1px solid var(--line);color:var(--muted);background:#f4f6f2}.badge.warn{background:#fff3d8;border-color:#e5d5af;color:#806025}.actions{display:flex;flex-wrap:wrap;gap:9px;margin:23px 0}.button{font-size:12px;color:var(--accent);background:var(--soft);border:1px solid #c4d7cb;border-radius:7px;display:inline-block;padding:9px 13px;text-decoration:none;cursor:pointer}.button.secondary{background:#fff;border-color:var(--line);color:#53615a}.document{padding:30px 36px;background:#fff;border:1px solid var(--line);border-radius:12px;min-width:0}.document h1{font-size:32px}.document p,.document li{overflow-wrap:anywhere}.document a{overflow-wrap:anywhere}.document blockquote,.quotation{border-left:3px solid #76a391;background:#f0f6f1;padding:10px 18px;margin:20px 0}.document blockquote p{margin:5px 0}pre{overflow:auto;background:#172b25;color:#e0eee5;padding:17px;border-radius:8px;font-size:12px;line-height:1.6}code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:.87em;overflow-wrap:anywhere}.document table{border-collapse:collapse;font-size:12px;width:100%;display:block;overflow:auto;margin:19px 0}.document th,.document td{padding:10px;border:1px solid var(--line);vertical-align:top;min-width:90px}.document th{background:#f2f5f0}figure{margin:22px 0}.diagram{border:1px solid var(--line);background:#fcfefb;border-radius:9px;padding:16px;overflow:auto}.diagram svg{display:block;width:100%;height:auto;max-height:620px;min-width:440px}.diagram figcaption{font-size:11px;color:var(--muted);line-height:1.6;margin-top:12px}.document img{max-width:100%;height:auto}.document details{font-size:12px;margin:15px 0}.document summary{cursor:pointer;color:var(--accent)}.doclayout{display:grid;grid-template-columns:minmax(0,1fr) 190px;gap:22px;align-items:start}.toc{position:sticky;top:96px;font-size:11px;max-height:80vh;overflow:auto}.toc a{display:block;padding:6px 0;color:var(--muted);text-decoration:none;line-height:1.4}.toc .sub{padding-left:10px;font-size:10px}.breadcrumbs,.meta{font-size:11px;color:var(--muted);line-height:1.6;margin-bottom:14px}.filters label{min-width:0;max-width:100%}.filters{display:flex;flex-wrap:wrap;gap:9px;margin:24px 0 14px}.filters input,.filters select{padding:10px 12px;border:1px solid var(--line);border-radius:7px;background:#fff;min-width:140px;font-size:13px}.filters input{flex:1}.row{overflow-wrap:anywhere;min-width:0;padding:16px 20px;background:#fff;border:1px solid var(--line);border-radius:9px;margin:9px 0;scroll-margin-top:90px}.row h3{font-size:14px;line-height:1.5;margin:8px 0}.row .meta{margin:0}.row details{font-size:12px;margin:12px 0}.row blockquote{margin:12px 0}.count{font-size:12px;color:var(--muted);margin:12px 0}.tablewrap{overflow:auto}table.report{border-collapse:collapse;width:100%;background:#fff;font-size:12px;margin:18px 0}table.report td,table.report th{padding:12px 13px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}table.report th{background:#edf3ee}.mobile-menu{display:none}.mobile-toc{display:none}footer{border-top:1px solid var(--line);margin-top:38px;padding-top:18px;color:var(--muted);font-size:11px;line-height:1.7}#no-results{display:none}body:has([data-searchable]) #no-results{border:1px dashed var(--line);padding:25px;border-radius:9px;font-size:13px;color:var(--muted)}[hidden]{display:none!important}.skip{position:absolute;left:-9999px}.skip:focus{left:10px;top:10px;background:white;padding:12px;z-index:30}.document hr{border:0;border-top:1px solid var(--line);margin:24px 0}@media(max-width:1150px){.doclayout{display:block}.toc{display:none}.mobile-toc{display:block}.shell{grid-template-columns:220px minmax(0,1fr)}main{padding:30px 28px}.document{padding:25px}}@media(max-width:720px){header{height:62px;padding:0 16px}.toplinks{display:none}.mobile-menu{display:block}.shell{display:block}aside{display:none;position:fixed;top:62px;width:min(330px,90vw);z-index:9;height:calc(100vh - 62px);box-shadow:12px 4px 30px #14261b25}aside.open{display:block}main{padding:25px 16px 50px}.statgrid{grid-template-columns:repeat(2,1fr);gap:9px}.stat{padding:16px}.stat strong{font-size:26px}.document{padding:21px 16px}.document h1{font-size:27px}.grid{grid-template-columns:1fr}.filters{gap:7px}.filters input{width:100%;flex-basis:100%}.filters select{min-width:120px;flex:1}.actions{gap:7px}.row{padding:14px}.diagram{padding:8px}.brand{font-size:18px}h1{font-size:32px}}
'''
JS='''
(()=>{const menu=document.getElementById('menu'),aside=document.querySelector('aside');if(menu)menu.addEventListener('click',()=>{const opened=aside.classList.toggle('open');menu.setAttribute('aria-expanded',String(opened));});document.addEventListener('keydown',e=>{if(e.key==='Escape'){aside.classList.remove('open');menu?.setAttribute('aria-expanded','false')}});const input=document.getElementById('query');if(input){const rows=[...document.querySelectorAll('[data-searchable]')],selects=[...document.querySelectorAll('[data-filter]')],count=document.getElementById('count');function filter(){const q=input.value.toLocaleLowerCase().trim();let n=0;for(const row of rows){const match=(!q||row.textContent.toLocaleLowerCase().includes(q))&&selects.every(s=>!s.value||row.dataset[s.dataset.filter]===s.value);row.hidden=!match;if(match)n++;}if(count)count.textContent=n+' / '+rows.length;document.getElementById('no-results').style.display=n?'none':'block';}input.addEventListener('input',filter);for(const select of selects)select.addEventListener('change',filter);filter();}const copy=document.getElementById('copy');if(copy)copy.addEventListener('click',async()=>{try{await navigator.clipboard.writeText(document.getElementById('review-context').textContent);copy.textContent='Контекст скопирован'}catch{copy.textContent='Буфер недоступен: откройте Markdown'}});})();
'''

def esc(value):return html.escape(str(value or ''),quote=True)
def readjson(path):return json.loads(path.read_text())
def write(path,text):path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text)
def source_url(path):return 'https://github.com/ultrabricks/rig-cli/blob/'+REF+'/'+path

def load_contracts():
 spec=importlib.util.spec_from_file_location('reader_contracts',ROOT/'tools/grooming/closeout_contracts.py')
 module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
 return module.CONTRACTS


def render_dot(contract):
 lines=['digraph G {','graph [rankdir=TB,bgcolor="transparent",pad="0.3",nodesep="0.35",ranksep="0.5"];','node [shape=box,style="rounded,filled",fillcolor="#f0f5ef",color="#adbdaf",fontname="sans-serif",fontsize=12,margin="0.18,0.12"];','edge [color="#7c9585",fontname="sans-serif",fontsize=9,arrowsize=0.7];']
 for ident,label in contract['nodes']:
  wrapped='\\n'.join(textwrap.wrap(label,32,break_long_words=False))
  lines.append(ident+' [label='+json.dumps(wrapped,ensure_ascii=False).replace('\\\\n','\\n')+'];')
 for left,right,label in contract['edges']:lines.append(left+' -> '+right+' [label='+json.dumps(label,ensure_ascii=False)+'];')
 lines.append('}');return '\n'.join(lines)


def enrich():
 contracts=load_contracts();records=readjson(ROOT/'docs/grooming/v4-2026-09-10/quote-registry.json')['records']
 aliases={k:q for q in records for k in q.get('source_ids',[])}
 items={r['key']:r for r in readjson(DATA/'issues.json')}
 for topic,c in contracts.items():
  path=SPECS/(topic+'.md');original=path.read_text()
  if '## Complete linked backlog' in original:
   original=original.split('## Complete linked backlog',1)[0].strip()
  original=re.sub(r'## State-transition diagram\n\n```mermaid.*?```','',original,flags=re.S).rstrip()
  marker='## Уточнение контракта — closeout 2026-09-11'
  if marker in original:original=original.split(marker,1)[0].rstrip()
  q=aliases[c['quote']]
  addition='\n\n'+marker+'\n\n### '+c['title']+'\n\n> '+q['edited'].replace('\n','\n> ')+f"\n\nИсточник `{q['id']}`; {q['source_kind']}. Это требование к описанному механизму, не новая аттестация всех связанных issues. Полный оригинал и контекст доступны в реестре запросов.\n\n### Сценарий и объяснение\n\n"+c['example']+'\n\n### Что проверить на этой границе\n\n'+'\n'.join('- '+x for x in c['checks'])+'\n\n### Схема ответственности и переходов\n\n```dot\n'+render_dot(c)+'\n```\n\nСхема показывает требуемый контракт. Она не означает, что конкретная реализация уже прошла приёмку.\n\n### Задачи с содержательно проверенной связью\n\n'
  if c['tasks']:
   addition+='| Задача | Сохранённая область | Статус в срезе |\n|---|---|---|\n'
   for key in c['tasks']:
    r=items.get(key)
    if r:
     url='https://github.com/'+r['canonical_repository']+'/issues/'+str(r['number'])
     addition+=f"| [{key}]({url}) | {r['title'].replace('|',' / ')} | {r['provider_state']} |\n"
  else:addition+='Для вспомогательных CLI полный перечень находится в общем реестре; конкретные feature-запросы не восстановлены из одной общей цитаты.\n'
  addition+='\nПрежняя таблица «Complete linked backlog» была результатом тематической классификации и включала ложные совпадения. Она не являлась native-иерархией. Здесь оставлены содержательно связанные опорные задачи; полный реестр всех issues сохранён отдельно, с явными source gaps и фактическими parent links в журнале.\n'
  write(path,original+addition)
 write(SPECS/'02-publication-and-access.md','''# Публикация спецификаций и граница доступа

**Статус:** публичный reader; авторизация для чтения не включена.

> «Сделай все спеки доступными для ревью на spec-dev или альтернативном сайте. По возможности нужна авторизация через GitHub или почту @glide.vc».

Редакторски исправленная выдержка из текущего поручения в ChatGPT, 10 сентября 2026. Это запрос на публикацию и желаемую защиту чтения, не разрешение показывать приватные переписки или секреты.

## Что публикуется

Обновлённая ретроспектива, тематические контракты и сохранённые исходные спецификации; отдельные страницы для всех задач, 505 квалифицированных записей запросов и фактических результатов проверки. Статические страницы содержат полный текст без выполнения JavaScript. Фильтры и копирование контекста — дополнение к уже читаемому HTML.

## Найденная реализация авторизации

Репозиторий `hyperide/hyper-ext-e2e` теперь прочитан через подключённый GitHub. В `e2e/portal/README.md` и `e2e/portal/src/server.ts` описаны email OTP для `@glide.vc` через Resend и дополнительный Google OAuth. Это **не GitHub OAuth**. Исследованный source ref: `3a41ab3739bd45123e8126741e3e25139d5c0faa`; README blob `d9d2895d39ea0f9c21bb6b84a8e645e7f3763fb0`, server blob `339507fc91dd34e5dcd4772180fdb020de75aee6`.

[Документация существующего портала](https://github.com/hyperide/hyper-ext-e2e/blob/3a41ab3739bd45123e8126741e3e25139d5c0faa/e2e/portal/README.md). Приватный исходный код и его конфигурация не скопированы в публичный reader. Упоминание схемы не означает развёртывания этого сервера или наличия его ключей в другом окружении.

## Публичное чтение и отправка ревью

Этот reader читает только опубликованные данные. Кнопка ревью открывает GitHub с конкретным документом, исходной ревизией и полями замечания. GitHub авторизует отправку issue; это **не** ограничивает чтение страницы. Кнопка входа перед публичным JSON или raw GitHub URL не создаёт приватный портал.

Пример: анонимный посетитель может открыть спецификацию и Markdown. Для отправки замечания через GitHub ему нужен собственный GitHub-вход. Reader не получает его токен, не выполняет GitHub API-запись от имени посетителя и не обещает авторизацию по домену.

## Контракт действительно закрытого reader

При внедрении закрытого режима сервер проверяет сессию перед выдачей страниц с данными, API, исходников и вложений. Домен сравнивается с подтверждённым email, а не с введённой строкой. Нужны защищённый session secret, HttpOnly cookie, безопасное истечение и отзыв сессии, ограничения попыток OTP и отрицательные тесты. Отладочная печать OTP и development fallback secret в production не используются.

GitHub OAuth требует отдельно зарегистрированного приложения и согласованной политики допуска. Наличие аккаунта GitHub само по себе не означает членство в `ultrabricks` или принадлежность `@glide.vc`.

## Приёмка публикации

- Полный текст всех документов читается без JavaScript; ссылки между страницами и на Markdown работают.
- Поиск, фильтры, клавиатурная навигация и экран шириной 393 пикселя проверены отдельно.
- Диаграммы соответствуют своим исходникам, а не заменены одной общей картинкой.
- Внешний URL проверяется по HTTP, хешу контента и фактическому содержимому страницы.
- Снимок портала доказывает портал; он не доказывает исправление Telegram, guards или других продуктов.

Связанные задачи: [rig-cli#538](https://github.com/ultrabricks/rig-cli/issues/538), [review-cli#484](https://github.com/ultrabricks/review-cli/issues/484), [координатор rig-cli#521](https://github.com/ultrabricks/rig-cli/issues/521).
''')
 retro=SPECS/'01-retrospective-v4.md'
 text=retro.read_text()
 text=text.replace('Репозиторий hyperide/hyper-ext-e2e вернул 404 через подключённый GitHub; его прежняя OTP/Resend реализация не была прочитана.','При раннем проходе hyperide/hyper-ext-e2e был недоступен. После восстановления подключения прочитаны README и server.ts: подтверждены email OTP через Resend для @glide.vc и дополнительный Google OAuth, не GitHub OAuth. Детали и границы переноса приведены в [контракте публикации](./02-publication-and-access.md).')
 write(retro,text)


def safe_svg(text):
 import xml.etree.ElementTree as ET
 root=ET.fromstring(text)
 allowed={'svg','g','path','rect','circle','ellipse','line','polyline','polygon','text','tspan','defs','marker','style','title','desc','symbol','use','linearGradient','radialGradient','stop','clipPath'}
 for parent in list(root.iter()):
  for child in list(parent):
   if child.tag.rsplit('}',1)[-1] not in allowed:parent.remove(child)
  for key,value in list(parent.attrib.items()):
   k=key.rsplit('}',1)[-1].lower()
   if k.startswith('on') or (k in ('href','src') and not value.startswith('#')):del parent.attrib[key]
  if parent.tag.rsplit('}',1)[-1]=='style' and parent.text:
   if re.search(r'@import|url\((?![\s"\x27]*#)',parent.text,re.I):raise ValueError('SVG requests remote style resources')
 root.attrib.pop('width',None);root.attrib.pop('height',None);root.set('role','img')
 ET.register_namespace('','http://www.w3.org/2000/svg')
 return ET.tostring(root,encoding='unicode')


def all_diagrams():
 cache={}
 for p in (ROOT/'portal/diagrams').glob('*.mmd'):
  svg=p.with_suffix('.svg')
  if svg.exists():cache[hashlib.sha256(p.read_text().strip().encode()).hexdigest()]=safe_svg(svg.read_text())
 result={};report=[]
 for path in SPECS.rglob('*.md'):
  for m in re.finditer(r'```(dot|mermaid)\s*\n(.*?)\n```',path.read_text(),re.S):
   kind,source=m[1],m[2].strip();ident=hashlib.sha256(source.encode()).hexdigest()
   if ident not in result:
    if kind=='dot':
     svg=subprocess.run(['dot','-Tsvg'],input=source,text=True,capture_output=True,check=True,timeout=30).stdout
     result[ident]=safe_svg(svg)
    elif ident in cache:result[ident]=cache[ident]
    else:raise ValueError('Unrendered original Mermaid source: '+str(path))
    write(OUT/'diagrams'/(ident+'.svg'),result[ident]);write(OUT/'diagrams'/(ident+'.'+('dot' if kind=='dot' else 'mmd')),source+'\n')
   report.append({'document':str(path.relative_to(ROOT)),'kind':kind,'source_sha256':ident,'status':'rendered'})
 write(DATA/'reader-diagrams.json',json.dumps(report,ensure_ascii=False,indent=2)+'\n')
 return result


def slug(path):return str(path.relative_to(SPECS))[:-3].replace('/','--')

def render_markdown(text,diagrams):
 text=re.sub(r'<details>\s*<summary>(.*?)</summary>',r'\n\n**\1**\n\n',text,flags=re.S).replace('</details>','')
 result=md.render(text)
 def replacement(m):
  source=html.unescape(m[2]).strip();ident=hashlib.sha256(source.encode()).hexdigest()
  return '<figure class="diagram">'+diagrams[ident]+'<figcaption>Схема контракта из исходника '+m[1]+'. Не является результатом проверки реализации.</figcaption></figure><details><summary>Редактируемый исходник диаграммы</summary>'+m[0]+'</details>'
 return re.sub(r'<pre><code class="language-(dot|mermaid)">(.*?)</code></pre>',replacement,result,flags=re.S)


def headings(body):
 counts={};toc=[]
 def one(m):
  level,content=m[1],m[2];title=html.unescape(re.sub('<[^>]*>','',content))
  base=re.sub(r'[^\w-]+','-',title.casefold(),flags=re.U).strip('-')[:100] or 'section'
  counts[base]=counts.get(base,0)+1;ident=base+('-'+str(counts[base]) if counts[base]>1 else '')
  if level in ('2','3'):toc.append((level,ident,title))
  return '<h'+level+' id="'+ident+'">'+content+'</h'+level+'>'
 return re.sub(r'<h([1-4])>(.*?)</h\1>',one,body,flags=re.S),toc


def page(path,title,body,contracts,active=''):
 relative=posixpath.relpath('.',posixpath.dirname(path) or '.')
 prefix='' if relative=='.' else relative+'/'
 link=lambda name:prefix+name
 nav=[('index.html','Обзор'),('spec/index.html','Все спецификации'),('backlog.html','Задачи и связи'),('requests.html','Исходные запросы'),('evidence.html','Проверки и применение')]
 navhtml=''.join('<a class="'+('active' if active==n else '')+'" href="'+link(n)+'">'+esc(t)+'</a>' for n,t in nav)
 navhtml+='<div class="navlabel">Ключевые документы</div><a href="'+link('spec/01-retrospective-v4.html')+'">Актуализированная ретроспектива</a><a href="'+link('spec/00-master.html')+'">Master contract</a><a href="'+link('spec/02-publication-and-access.html')+'">Публикация и доступ</a><div class="navlabel">Границы системы</div>'
 for name,c in contracts.items():navhtml+='<a href="'+link('spec/'+name+'.html')+'">'+esc(c['title'])+'</a>'
 text='<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer"><meta name="color-scheme" content="light"><title>'+esc(title)+' · RIG specs</title><style>'+CSS+'</style></head><body><a class="skip" href="#main">К содержанию</a><header><a class="brand" href="'+link('index.html')+'">RIG <span>/ specifications</span></a><nav class="toplinks">'+''.join('<a href="'+link(n)+'">'+esc(t)+'</a>' for n,t in nav[:3])+'</nav><button class="button secondary mobile-menu" id="menu" aria-controls="sidebar" aria-expanded="false">Меню</button><a class="button secondary" href="https://github.com/ultrabricks/rig-cli/issues/521">GitHub ↗</a></header><div class="shell"><aside id="sidebar"><div class="eyebrow" style="padding:0 10px">Recovery / source v4</div><nav class="nav">'+navhtml+'</nav></aside><main id="main">'+body+'<footer>Источник документов: git <code>'+REF[:12]+'</code>. Публичное чтение; замечания отправляются через GitHub. Изолированные тесты, состояние issue и приёмка на целевой машине показаны отдельно. Полные исходные требования сохранены; неизвестное не выдано за выполненное.</footer></main></div><script>'+JS+'</script></body></html>'
 write(OUT/path,text)


def filters(options):
 return '<div class="filters"><label style="flex:1"><span class="meta">Поиск по содержимому</span><input id="query" aria-label="Поиск" placeholder="Номер, требование или термин…" style="width:100%"></label>'+''.join('<label><span class="meta">'+esc(label)+'</span><select data-filter="'+key+'" aria-label="'+esc(label)+'"><option value="">Все</option>'+''.join('<option value="'+esc(v)+'">'+esc(v)+'</option>' for v in values)+'</select></label>' for key,label,values in options)+'</div><div id="count" class="count" aria-live="polite"></div>'


def build(source_ref):
 global REF
 if not re.fullmatch('[0-9a-f]{40}',source_ref):raise ValueError('Explicit immutable source ref required')
 REF=source_ref;contracts=load_contracts();diagrams=all_diagrams()
 records=readjson(DATA/'issues.json');summary=readjson(DATA/'plan-summary.json')
 qs=readjson(ROOT/'docs/grooming/v4-2026-09-10/quote-registry.json')['records']
 evidence=readjson(DATA/'verification.json')
 receipts_path=DATA/'applied-summary.json';receipts=readjson(receipts_path) if receipts_path.exists() else None
 documents=[(p,slug(p),re.search(r'^# (.+)',p.read_text(),re.M)[1]) for p in sorted(SPECS.rglob('*.md'))]
 mapped={str(p.relative_to(ROOT)):s for p,s,_ in documents}
 for path,ident,title in documents:
  raw=path.read_text();body,toc=headings(render_markdown(raw,diagrams))
  def resolve(m):
   href=html.unescape(m[1])
   if href.startswith(('https:','http:','mailto:','#')):return m[0]
   target,_,fragment=href.partition('#')
   normalized=posixpath.normpath(str(path.parent.relative_to(ROOT))+'/'+target)
   dest=mapped.get(normalized)
   if dest:return 'href="'+esc(dest+'.html'+('#'+fragment if fragment else ''))+'"'
   return 'href="'+esc(source_url(normalized)+('#'+fragment if fragment else ''))+'"'
  body=re.sub(r'href="([^"]+)"',resolve,body)
  context='Спецификация: '+title+'\nИсточник: '+source_url(str(path.relative_to(ROOT)))+'\nРаздел и исходная цитата:\n\nОжидаемое поведение:\n\nЗамечание и воспроизведение:\n\nСвязанные задачи:\n'
  review='https://github.com/ultrabricks/rig-cli/issues/new?title='+urllib.parse.quote('[Spec review] '+title[:100])+'&body='+urllib.parse.quote(context)
  links=''.join('<a class="'+('sub' if lvl=='3' else '')+'" href="#'+esc(i)+'">'+esc(t)+'</a>' for lvl,i,t in toc)
  pre='<div class="breadcrumbs"><a href="index.html">Спецификации</a> / '+esc(ident)+'</div><div class="actions"><a class="button" href="'+esc(review)+'">Предложить правку ↗</a><a class="button secondary" href="../raw/'+esc(ident)+'.md">Markdown</a><a class="button secondary" href="'+esc(source_url(str(path.relative_to(ROOT))))+'">Источник в git ↗</a><button id="copy" class="button secondary">Копировать контекст</button></div><div class="meta">'+esc('Исторический источник с ревью' if path.parent!=SPECS or ident=='01a-retrospective-preserved' else 'Контракт / спецификация; не подтверждение реализации')+'</div><details class="mobile-toc"><summary>Оглавление</summary>'+links+'</details><div class="doclayout"><article class="document">'+body+'</article><nav class="toc" aria-label="Оглавление"><div class="eyebrow">На этой странице</div>'+links+'</nav></div><pre id="review-context" hidden>'+esc(context)+'</pre>'
  page('spec/'+ident+'.html',title,pre,contracts,'spec/index.html');write(OUT/'raw'/(ident+'.md'),raw)
 cards=''.join('<a class="card" data-searchable href="'+esc(ident)+'.html"><span class="badge">'+('Источник + ревью' if p.parent!=SPECS else 'Контракт')+'</span><h3>'+esc(title)+'</h3><p>'+esc(str(p.relative_to(ROOT)))+'</p></a>' for p,ident,title in documents)
 page('spec/index.html','Каталог спецификаций','<div class="eyebrow">Полный каталог</div><h1>Спецификации, которые можно проверить</h1><p class="intro">'+str(len(documents))+' документов. Текущие контракты, исходная ретроспектива и сохранённые исторические спецификации — без подмены их статуса утверждением «всё сделано».</p>'+filters([])+'<div class="grid">'+cards+'</div><div id="no-results">Нет совпадений.</div>',contracts,'spec/index.html')
 home='<div class="eyebrow">RIG ecosystem · review workspace</div><h1>От исходного запроса<br>к проверенному результату</h1><p class="intro">Ретроспектива, требования, задачи и доказательства всей экосистемы. Откройте конкретный контракт, найдите исходную цитату и оставьте замечание к точной версии документа.</p><div class="actions"><a class="button" href="spec/01-retrospective-v4.html">Читать обновлённую ретроспективу</a><a class="button secondary" href="spec/index.html">Все спецификации</a></div><div class="statgrid">'+''.join('<div class="stat"><strong>'+str(n)+'</strong><span>'+s+'</span></div>' for n,s in [(len(documents),'документов для ревью'),(len(records),'issues в полном срезе'),(len(qs),'записей с атрибуцией'),(len(summary['repositories']),'репозиториев')])+'</div><div class="notice"><strong>Что означают эти числа:</strong> состав реестра и опубликованных материалов, не число исправленных функций. Применение правок к issues и результаты тестов указаны отдельно. '+str(summary['without_recovered_feature_request'])+' записей не имеют однозначно восстановленной исходной постановки функции; чужие слова не подставлены вместо цитат.</div><h2>Ключевые границы системы</h2><div class="grid">'+''.join('<a class="card" href="spec/'+name+'.html"><span class="eyebrow">'+name+'</span><h3>'+esc(c['title'])+'</h3><p>Сценарий, критерии, схема и связанные задачи</p></a>' for name,c in contracts.items())+'</div><h2>Проверка и участие</h2><div class="grid"><a class="card" href="evidence.html"><h3>Что действительно запускалось</h3><p>Ревизии, падения, пропуски и фактическое применение описаний</p></a><a class="card" href="requests.html"><h3>Исходные сообщения</h3><p>Оригинал, редакторская версия, контекст и тип атрибуции</p></a><a class="card" href="spec/02-publication-and-access.html"><h3>Публичность и авторизация</h3><p>Найденная OTP-реализация и честные границы этой публикации</p></a></div>'
 page('index.html','Обзор',home,contracts,'index.html')
 backlog='<div class="eyebrow">Полный реестр</div><h1>Задачи и исходные требования</h1><p class="intro">Состояние GitHub сохранено отдельно от проверки результата. Тематический раздел — навигационная подсказка, не native parent и не доказательство зависимости.</p>'+filters([('repo','Репозиторий',sorted(summary['repositories'])),('state','GitHub state',['open','closed']),('kind','Тип',['product-or-maintenance','epic','process-record'])])
 for r in sorted(records,key=lambda r:(r['repo'],-r['number'])):
  url='https://github.com/'+r['canonical_repository']+'/issues/'+str(r['number'])
  body='<div class="row" id="'+esc(r['key'].replace('#','-'))+'" data-searchable data-repo="'+esc(r['repo'])+'" data-state="'+esc(r['provider_state'])+'" data-kind="'+esc(r['classification'])+'"><div class="meta"><a href="'+url+'">'+esc(r['key'])+' ↗</a> · '+esc(r['provider_state'])+' · '+esc(r['classification'])+'</div><h3>'+esc(r['title'])+'</h3><span class="badge warn">Индивидуальная приёмка не подтверждена этим реестром</span>'
  body+='<p class="meta">'+('Запросы: '+', '.join('<a href="requests.html#'+esc(i)+'">'+esc(i)+'</a>' for i in r['quote_ids']) if r['quote_ids'] else 'Оригинальная постановка функции не установлена; исходный scope сохранён.')+'</p>'
  if r.get('quote_relationships'):body+='<details><summary>Основание связи с запросами</summary>'+''.join('<p>'+esc(i['id'])+' · '+esc(i['kind'])+'</p>' for i in r['quote_relationships'])+'</details>'
  backlog+=body+'</div>'
 backlog+='<div id="no-results">Нет совпадений.</div>'
 page('backlog.html','Задачи',backlog,contracts,'backlog.html')
 requests='<div class="eyebrow">Происхождение требований</div><h1>Исходные запросы</h1><p class="intro">Экспорт v4: 4329 строк. В reader представлены 505 квалифицированных записей с оригиналом, редакторской версией и явной атрибуцией. Агентские сообщения и keyword-score связи не становятся требованиями человека автоматически.</p>'+filters([])
 for q in qs:
  requests+='<section class="row" id="'+esc(q['id'])+'" data-searchable><div class="meta"><strong>'+esc(q['id'])+'</strong> · '+esc(q['source_kind'])+'</div><blockquote class="quotation"><p>'+esc(q['edited']).replace('\n','<br>')+'</p></blockquote><p class="meta">'+esc(q['context'])+'</p>'+('<span class="badge warn">Контекст фрагментарный</span>' if q.get('context_incomplete') else '')+'<details><summary>Полный оригинал до редакторской правки</summary><p>'+esc(q['original']).replace('\n','<br>')+'</p></details><details><summary>Исходные ID и редакторский статус</summary><p>'+esc(', '.join(q['source_ids']))+'</p><p>'+esc(q['editorial_status'])+'</p><p>SHA-256 исходной записи: <code>'+esc(q['original_sha256'])+'</code></p></details></section>'
 requests+='<div id="no-results">Нет совпадений.</div>'
 page('requests.html','Исходные запросы',requests,contracts,'requests.html')
 check='<div class="eyebrow">Доказательства</div><h1>Что действительно проверено</h1><p class="intro">Точный commit, команда и окружение важнее статуса merged. Это результаты изолированных тестов, а не проверка установленного продукта на машине пользователя.</p><div class="notice">Не складывать повторный smoke-запуск той же suite в число уникальных тестов. Setup failure, нестабильный тест и воспроизводимый дефект продукта требуют разных выводов.</div><div class="tablewrap"><table class="report"><thead><tr><th>Репозиторий</th><th>SHA</th><th>Passed / failed / skipped</th><th>Результат</th></tr></thead><tbody>'
 for repo,item in sorted(evidence.items()):
  counts=item.get('test_counts',{})
  check+='<tr><td>'+repo+'</td><td><code>'+esc(item.get('source_sha','не записан')[:12])+'</code></td><td>'+' / '.join(str(counts.get(k,'—')) for k in ['passed','failed','skipped'])+'</td><td>'+('Команды прошли' if item.get('passed') else 'Нужен разбор / setup gap')+'</td></tr>'
 check+='</tbody></table></div><p><a href="'+source_url(str((DATA/'verification.json').relative_to(ROOT)))+'">Машиночитаемые команды и результаты ↗</a></p><h2>Применение описаний в GitHub</h2>'
 if receipts:
  check+='<div class="tablewrap"><table class="report"><tr><th>Репозиторий</th><th>Проверено точных тел</th><th>Осталось / конфликт</th><th>Состояние</th></tr>'
  for repo,item in sorted(receipts['repositories'].items()):check+='<tr><td>'+esc(repo)+'</td><td>'+str(item.get('verified_exact','—'))+'</td><td>'+str(item.get('remaining_or_conflicted','—'))+'</td><td>'+esc(item.get('status','complete' if item.get('complete') else 'не завершено'))+'</td></tr>'
  check+='</table></div><p class="meta">Время чтения журналов: '+esc(receipts.get('checked_at'))+'. Точные тела подтверждены отдельным чтением. Ручные правки и новые issues требуют самостоятельной сверки.</p>'
 else:check+='<p>Итоговый сводный журнал применения ещё не включён в эту ревизию reader. Подготовленные планы не выданы за применённые изменения.</p>'
 check+='<h2>Ограничения</h2><p>macOS LaunchAgent, реальные provider accounts, native-сессии и личный Telegram не использовались. Неисследованные случаи остаются неизвестными. Авторизация reader не включена; работа тестового backend не доказывает её наличие.</p><p><a href="https://github.com/ultrabricks/rig-cli/issues/521">Эпик восстановления ↗</a></p>'
 page('evidence.html','Проверки и применение',check,contracts,'evidence.html')
 manifest={'source_ref':REF,'documents':len(documents),'issues':len(records),'quotations':len(qs),'unique_diagrams':len(diagrams),'html_pages':len(list(OUT.rglob('*.html'))),'public_reading':True,'github_authentication':'review submission only','user_machine_acceptance':False,'source_export_version':4,'receipt_included':receipts is not None}
 write(OUT/'manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
 write(OUT/'llms.txt','# RIG specifications\n\nPublic source-linked specification review corpus. Not a production acceptance certificate.\n\n- [Catalog](spec/index.html)\n- [Retrospective](raw/01-retrospective-v4.md)\n- [Master contract](raw/00-master.md)\n- [Evidence](evidence.html)\n- [Original requests](requests.html)\n')
 print(json.dumps(manifest,indent=2))

if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('--enrich',action='store_true');parser.add_argument('--source-ref');args=parser.parse_args()
 if args.enrich:enrich()
 if args.source_ref:build(args.source_ref)
