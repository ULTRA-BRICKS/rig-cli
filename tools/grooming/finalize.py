#!/usr/bin/env python3
"""Substantive recovery addenda over the lossless inventory compilation.

This stage updates documents and proposed mutations only. It does not close a
feature because tests passed or treat a missing screenshot as a product defect.
"""
from __future__ import annotations
import collections,hashlib,html,io,json,pathlib,re,runpy,urllib.request,zipfile
import xml.etree.ElementTree as ET
BASE=pathlib.Path('docs/grooming/full-2026-09-10');SPECS=pathlib.Path('docs/specs/recovery')
DOC='https://github.com/alex-mextner/rig-cli/blob/docs/full-grooming-2026-09-10/'
ROOT='https://github.com/alex-mextner/rig-cli/issues/521'
FOLLOW='https://github.com/alex-mextner/rig-cli/actions/runs/34478551966'

def dump(p,data):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')

def download(ident):
    import os
    req=urllib.request.Request('https://api.github.com/repos/alex-mextner/rig-cli/actions/artifacts/'+str(ident)+'/zip',headers={'Authorization':'Bearer '+os.environ['GH_TOKEN'],'Accept':'application/vnd.github+json'})
    with urllib.request.urlopen(req,timeout=120) as response:return zipfile.ZipFile(io.BytesIO(response.read()))

def xml_summary(raw):
    cases=[]
    for t in ET.fromstring(raw).iter('testcase'):
        failed=t.find('failure') is not None or t.find('error') is not None
        cases.append({'module':t.get('classname'),'name':t.get('name'),'status':'failed' if failed else ('skipped' if t.find('skipped') is not None else 'passed')})
    return {'counts':dict(collections.Counter(t['status'] for t in cases)),'failed':[t for t in cases if t['status']=='failed'],'skipped':[t for t in cases if t['status']=='skipped']}

def originals(body):
    marker='## Original technical scope, acceptance and delivery history'
    return body.split(marker,1)[1].rsplit('<!-- /rig-grooming:',1)[0].strip() if marker in body else body

def quotation(q,note):
    return '> '+q['edited'].replace('\n','\n> ')+'\n\nSource Q'+q['id']+'; '+q['source_kind']+'; '+q['context']+'. '+note+'\n\n<details><summary>Original wording</summary>\n\n> '+q['original'].replace('\n','\n> ')+'\n\n</details>'

def main():
    registry=json.loads((BASE/'quote-registry.json').read_text());quotes={q['id']:q for q in registry['records']}
    follow={'run_url':FOLLOW,'review-cli':{},'tg-cli':{}}
    with download(10152928687) as z:
        for name in z.namelist():
            if name.endswith('/pytest.xml'):follow['review-cli']['full']=xml_summary(z.read(name))
            if name.endswith('/effort-isolated.xml'):follow['review-cli']['effort_isolated']=xml_summary(z.read(name))
            if name.endswith('/imagemagick.txt'):follow['review-cli']['image_magick_version']=z.read(name).decode().splitlines()[0]
    with download(10152675200) as z:
        follow['tg-cli']['exit_codes']={pathlib.PurePosixPath(n).name:int(z.read(n)) for n in z.namelist() if n.endswith('.exit')}
        follow['tg-cli']['repeat_failures']={pathlib.PurePosixPath(n).name:re.findall(r'(?m)^\(fail\) (.+)',z.read(n).decode(errors='replace')) for n in z.namelist() if n.endswith('.log')}
    follow['interpretation']={
      'review-cli':'With ImageMagick 7.1.2-31 installed: 2625 passed, 1 failed, 12 skipped. The same effort/board test fails when run alone (24 passed, 1 failed). It patches backend_available but leaves another availability path dependent on installed CLIs. This is a reproducible test-isolation/contract gap, not evidence of 66 independent product regressions. Suite invocation/count differences are retained instead of silently summed.',
      'tg-cli':'The durable-question file fails once and passes twice across three isolated repeats; the repeated failure is a reconnect/callback scenario, different from the first full-suite late-delivery assertion. Watermark tests pass 27/27 with both system and venv interpreter setups after installing system Pillow. Do not declare the original watermark failure a confirmed product regression.'}
    dump(BASE/'followup-results.json',follow)
    tests=json.loads((BASE/'test-results.json').read_text())
    for repo in ('review-cli','tg-cli'):tests[repo]['followup']=follow[repo];tests[repo]['qualification']=follow['interpretation'][repo]+' Follow-up: '+FOLLOW
    dump(BASE/'test-results.json',tests)
    changes=[]
    for file in (BASE/'plans').glob('*.json'):
        plan=json.loads(file.read_text());repo=file.stem
        before={r['key']:r for r in json.loads((BASE/'before'/(repo+'.json')).read_text())}
        for row in plan:
            if not row['body']:continue
            body=row['body'];old=before[row['key']]['body'] or ''
            if repo in ('review-cli','tg-cli'):
                body=body.replace('## Worked verification example','### Dependency-correct / deterministic follow-up\n\n'+follow['interpretation'][repo]+' [Actual run]('+FOLLOW+'). No live provider was contacted.\n\n## Worked verification example',1)
            # Every issue gets its OWN original operation/criterion example before
            # the broader domain illustration. This avoids presenting a Telegram
            # delivery example as the reproduction of a QR-only helper bug.
            criteria=[]
            for _,text in re.findall(r'(?m)^\s*- \[([ xX])\] (.+)$',old):
                text=re.split(r'\s+(?:—|--)\s*(?:forced|proof):',text,flags=re.I)[0].strip()
                if text and text not in criteria:criteria.append(text)
            what=re.search(r'(?ms)^## (?:What|Summary|Problem)\s*\n(.*?)(?=^## |\Z)',old)
            context=re.sub(r'\s+',' ',what.group(1)).strip() if what else ''
            detail='### Ticket-specific check\n\n**Operation under review:** '+row['expected_title']+'.\n\n'
            if context:detail+='**Original problem context (historical statement, not a new observation):** '+context[:1100]+('… [complete text retained below]' if len(context)>1100 else '')+'\n\n'
            if criteria:
                detail+='**Expected outcomes from this ticket, not invented from its title:**\n'+'\n'.join('- '+x for x in criteria[:4])+'\n\nRe-run these checks at the recorded source revision; keep setup, actual output and result for each criterion separate. Historical checked boxes below are retained as history, not re-checked by this text.\n\n'
            else:
                detail+='**Acceptance gap:** this source record has no explicit outcome checklist. Recover a concrete operation and expected result from its technical description/comments before claiming acceptance; do not manufacture a feature requirement from a process-review title.\n\n'
            body=body.replace('## Worked verification example','## Worked verification example\n\n'+detail,1)
            related=[k for k in row['source']['related_issues'] if k!=row['key']]
            if related:
                body=body.replace('## Original technical scope, acceptance and delivery history','## Existing related work\n\n'+', '.join('['+k+'](https://github.com/alex-mextner/'+k.replace('#','/issues/')+')' for k in related[:30])+('. Complete typed relationship list is in the per-issue ledger.' if len(related)>30 else '.')+' These are observed references, not automatically asserted blocking dependencies.\n\n## Original technical scope, acceptance and delivery history',1)
            if repo=='qa-cli' and 'SHIP_' not in row['expected_title'] and row['topic']!='process':
                first=body.index('## Current evidence and next verification')
                body=body[:body.index('> ')]+'\n\n'.join(quotation(quotes[i],'Related testing mandate, not a fabricated title-level original.') for i in ('233','202'))+'\n\n'+body[first:]
                body=body.replace('The user replies to landing while rig-fable is busy. Delivery to landing and the next Telegram update continue without waiting for rig-fable\'s full turn. A late Approve is retained for the same scoped action after reconnect, but a changed command, repository, commit or native session cannot reuse it.','Run the documented qa telegram login flow against a fake browser that first shows an unrelated canvas and then a valid QR. Only the QR is captured. Restart the CLI and verify session state without opening Telegram.app or sending a production message. A separate explicitly enabled live test is required for real phone login.')
            if len(body)>64000:
                row['body']=None;row['source']['mutation_blocker']='Refined body exceeds safe size; requires manual concise rewrite preserving original.'
            else:row['body']=body
            changes.append({'key':row['key'],'criteria_recovered':len(criteria),'own_problem_context':bool(context),'body_ready':row['body'] is not None})
        dump(file,plan)
    dump(BASE/'description-review.json',changes)
    # Fix tables in generated files, not in immutable source backups.
    for file in SPECS.rglob('*.md'):
        text=file.read_text()
        while re.search(r'(?m)(^\|[^\n]*\|)\n\n(?=\|)',text):text=re.sub(r'(?m)(^\|[^\n]*\|)\n\n(?=\|)',r'\1\n',text)
        file.write_text(text)
    master='''# Rig ecosystem: восстановление работоспособности и полная приёмка

**Статус:** спецификация восстановления и проверки; не утверждение, что все функции приняты.\n**Эпик:** '''+ROOT+'''\n**Срез:** 10 сентября 2026. Все публичные issues и PR, без ограничения последними десятью днями; отдельный взгляд на изменения с 31 августа.

## Исходное поручение

'''+quotation(quotes['current-2026-09-10'],'Текущее поручение на полный груминг, не исходная постановка каждой исторической функции.')+'''

## Что именно проверено и что в источниках было неверно

Полный публичный реестр содержит 1519 issues и 935 PR в 11 репозиториях. Приватный harness-cli учитывается отдельно: его исходный код не переносится в публичный реестр. Загрузка всех описаний и комментариев доказывает полноту чтения реестра, но не работоспособность всех функций.

Приложенный экспорт запросов содержит 374 записи. Из них 117 — сгенерированные названия GitHub-задач, 10 — пересказы агента; их нельзя приписывать Алексу как дословные запросы. Ещё 18 сообщений относятся к стороннему клиентскому проекту и не публикуются здесь. В реестре цитат 229 относящихся к экосистеме записей плюс текущее поручение; фрагментарность и косвенная атрибуция обозначены.

Второе приложение — проверка ссылок на изображения в 715 задачах, а не функциональное тестирование. `fixed` может означать повторное добавление локального пути, который GitHub всё равно не показывает. `missing` может означать лишь слово screenshot в критерии будущей проверки. Эти признаки не являются основанием массово закрывать или переоткрывать продуктовые задачи.

Корневой `rig-cli/SPEC.md` посвящён **Evolve**, обозревателю эволюции проекта. Это не master-spec всей экосистемы. Его исходная область, draft-статус и условия интерактивной приёмки сохранены в отдельном документе; эта спецификация не подменяет Evolve другим продуктом.

## Границы компонентов

| Компонент | Владеет | Не должен дублировать |
|---|---|---|
| harness-cli | обнаружение харнесов/сессий, канонические ID, протоколы и фактические наблюдения health | Telegram-карточки и правила merge |
| agent-tools | переносимые guards, нормализация доверенного контекста, применение правил | второй инвентаризатор аккаунтов |
| review-cli | роли, выполнение ревью, fallback и доказательства попыток | собственный независимый парсер credential stores |
| tg-cli | маршрутизация Telegram, durable intent и жизненный цикл человеческого ответа | встроенный ACP-движок для каждого харнеса |
| task-cli | продуктовые задачи, связи, корректное описание и удалённо доступные доказательства | одноразовые todo ревью-агентов |
| rig-cli | декларативная установка, конфигурация и проверка drift | второй scheduler или service supervisor |
| qa/dev/draw/stt/research/pm | собственные пользовательские CLI-сценарии | ложные общие гарантии вместо своих integration checks |

## Пример 1: одна модель, несколько ролей — не несколько выдуманных моделей

'''+quotation(quotes['227'],'Требование применяется к каждому режиму ревью, включая деградацию пула.')+'''

Доступен один оплаченный backend. Он последовательно или в допустимом параллелизме выполняет отдельные задания correctness, security, performance и tests с соответствующими промптами. Результат содержит четыре выполненные роли и честно одну модель. `--pool 0`, preset medium/large и повторное использование модели не должны ни убирать роли, ни создавать четыре одинаковых generic prompt. Требование независимых моделей не следует выдумывать вместо запрошенного покрытия ролей.

Связанные исходные задачи: review-cli#364 (независимость измерений), #365 (presets), #433 (pool-zero), #441/#442/#444 (сохранение покрытия), #454 (fallback модели роли); agent-tools#644/#648 (потребитель кворума). Похожесть названий не означает, что все эти интеграционные случаи — дубли.

## Пример 2: неизвестный аккаунт не здоров и не исчерпан

'''+quotation(quotes['231'],'Исходное требование к лимитам; описание usage не удовлетворяет ему автоматически.')+'''

Аккаунт A использовал 100% подтверждённого окна, B — 20%, C временно недоступен для чтения. Нужно использовать B, показывать C как unknown с причиной и временем наблюдения, а не сводить все Claude-аккаунты к худшему проценту. Ошибка локальной базы или transport timeout не является доказательством исчерпания квоты. Отсутствие доказательств исчерпания не равно confirmed available.

Для `all_exhausted=true` нужен непустой полный набор реально допустимых маршрутов/аккаунтов и свежие применимые утвердительные факты по каждому. Реализация capacity producer и ship consumer проверяются вместе, с пустым пулом, устаревшими данными, чужим preset и одним рабочим альтернативным аккаунтом.

## Пример 3: доставить сообщение — не ждать завершения генерации

'''+quotation(quotes['201'],'Запрос на полный обмен с OMP вне tmux; не только успешное обнаружение в picker.')+'''

Человек отвечает `landing`, пока `rig-fable` занят. Принятие сообщения сохраняется до продвижения Telegram offset. Следующие updates и другой адресат не ждут окончания полного model turn. Сбой после сохранения восстанавливает тот же operation ID, не новый дублирующий запрос. Внутри адресата порядок сохраняется. Смена PID, pane ID или native session generation не переносит сообщение другому агенту.

Поздний Approve не теряется, но применяется ровно один раз к тому же содержимому и scope. Другой command, repository, commit или origin session требует нового решения. `accepted`, `delivered`, `completed`, `denied`, `unknown` — разные состояния. Фиксированный новый таймер не является заменой правильного владения pending operation.

## Пример 4: hosted CI не запустился — локальная проверка, а не игнорирование ошибки теста

'''+quotation(quotes['263'],'Исходный запрос на локальный путь проверки при проблемах CI.')+'''

Недоступность runner/billing инфраструктуры и отрицательный результат исполняемого теста различаются. `gh ship` запускает объявленные эквивалентные проверки локально на точном head SHA и сохраняет результат. Проверяется именно целевой `--repo`, не cwd вызывающего shell. Прошедший локальный тест не даёт права обойти другую защиту, а настоящее падение нельзя переименовать в инфраструктурный outage. Отдельно проверяется merge-time version bump при двух параллельных PR.

## Пример 5: история документа и привязки комментария

Спека изменяется дважды, причём mtime совпадает. Content hash различает наблюдённые версии; изменения между наблюдениями не обещаются восстановленными. Два одинаковых фрагмента в разных секциях дают ambiguous match, а не автоматическую новую привязку. Старый quote/revision сохраняется, переход якоря имеет причину и old/new context. Сравнение двух версий не перезаписывает текущий файл. На узком экране список комментариев и действия доступны без нативных blocking alert/confirm.

## Результаты повторных запусков

Первый изолированный прогон охватил все 11 публичных репозиториев. Для review-cli отсутствие ImageMagick было обнаружено и проверено отдельно, а не записано как 65 разных багов продукта. При ImageMagick 7.1.2-31 осталось **1 воспроизводимое падение** `test_overridden_board_flows_into_panel_jobs_once`; тот же тест падает отдельно. Его подмена одной функции availability не изолирует второй путь проверки CLI. Это требует исправления тестового контракта/изоляции и отдельной проверки реального поведения, а не заявления, что все effort overrides сломаны.

Для tg-cli файл durable question integration дал **один неуспешный и два успешных прогона**; это доказанная нестабильность теста/жизненного цикла, не доказательство постоянной потери каждого ответа. Watermark-файл прошёл 27/27 при обоих вариантах Python после установки системного Pillow. Исходное падение под PATH не названо подтверждённым продуктовым багом без такого различения.

Полные машинные результаты: [первый прогон]('''+DOC+'''docs/grooming/full-2026-09-10/test-results.json), [уточняющий прогон]('''+DOC+'''docs/grooming/full-2026-09-10/followup-results.json). macOS/LaunchAgent, реальные native события, аккаунты провайдеров и визуальная приёмка установленного продукта остаются отдельными клетками матрицы.

## Ревью старых спецификаций: обязательные поправки

**Harness redesign/shared-lib:** доверие к окружению launcher нельзя переносить на native siblings, разделяющих процесс; source/provider/account identity и временные health-наблюдения не являются одним enum. Существующие библиотеки и установщики сохраняются; новый слой не вводится только ради переименования.

**Telegram decomposition/control:** перенести все reply/topic/permission пути, а не только awareness nudge; сохранить прежние гарантии удержания решений, не удаляя regression tests. Дополнение `requestId` не доказывает scope, если command/repo/native generation не связаны с ним.

**Provider failover:** протокол, модель, роль, аккаунт и effort независимы. Fallback пробует допустимые альтернативы, но не выдаёт отсутствие результата за review success. В журнале остаются все попытки, actual route и terminal cause; cooldown без классификации причины не доказывает исчерпание квоты.

**QA:** mock/fixture/live — разные уровни доказательств. Пользовательский desktop и production Telegram не становятся тестовым fixture. Пропуск live-теста виден; отсутствие бинарника или обязательной зависимости не маскируется зелёным общим счётчиком.

**Rig provisioning/stack/linter/GitHub settings:** apply и status используют один declared state, учитывают provenance и disabled-состояния. Комментарий управляемого файла объясняет альтернативную команду. Установка на диск, PATH и версия живого процесса проверяются раздельно; symlink/TOCTOU и конкурентные config edits имеют отрицательные тесты.

**Evolve:** проверять squarified mosaic, клавиатурный drill-down/up, сохранение выбранного ghost snapshot, честные exact/range/inferred/unavailable symbol bindings и небольшой экран. Парсер Git и CLI smoke не заменяют этот UI-результат. Ранее отложенные/отклонённые направления не включаются обратно автоматически.

## Диаграмма границ ответственности

```mermaid
flowchart TD
  U[Запрос пользователя + контекст] --> T[task-cli: каноническая задача и критерии]
  T --> R[review-cli: роли, попытки, результат]
  R --> H[harness-cli: маршруты, аккаунты, native сессии]
  H --> G[agent-tools: доверенный контекст и guards]
  TG[tg-cli: сообщения и scoped решения] --> H
  TG --> G
  I[rig-cli: декларативная установка и drift] --> H
  I --> G
  I --> TG
  R --> E[Доказательства на точной ревизии]
  E --> A[Интеграционная и live-приёмка]
```

## Правила консолидации

Настоящий дубль объединяется только после переноса уникальных требований, находок и комментариев в каноническую задачу. `rig-cli#508 → #507` сохраняет OOM-расследование; `task-cli#170/#172/#173 → #169` — варианты и проверки taskref. Старое предложение `agent-tools#542` об исключении целых харнесов заменено требованиями native identity в #573; underlying defect не считается исправленным от закрытия устаревшего направления.

Одноразовые review/version-bump задания отмечаются процессными записями, но не удаляются вместе с находками. Разные producer/consumer с одним названием не становятся дублем. Существующий native parent не заменяется без отдельного решения; сохранённые связи и конфликты фиксируются в журнале.

## Условия завершения восстановления

- Для каждой задачи есть сохранённая исходная область, настоящая цитата или явный пробел происхождения, владелец и ссылка на контракт.
- Фактически применённые изменения tracker подтверждены read-back; concurrent changes не затёрты.
- Регрессия доказана конкретным входом/выходом и ревизией; неисследованные функции не объявлены работающими или сломанными.
- Спеки доступны для чтения, ревью относится к конкретному документу/разделу, а состояние доступа описано честно.
- Непроверенные live-сценарии и частичные внешние доступы остаются незавершёнными критериями, а не force-checked успехом.
'''
    (SPECS/'00-master.md').write_text(master)
    dump(BASE/'finalization.json',{'description_rows':len(changes),'ready':sum(r['body_ready'] for r in changes),'outcome_checklists_recovered':sum(r['criteria_recovered']>0 for r in changes),'master_spec':str(SPECS/'00-master.md'),'followup_run':FOLLOW,'issue_mutations':0})
    runpy.run_path('tools/grooming/render.py',run_name='__main__')

if __name__=='__main__':main()
