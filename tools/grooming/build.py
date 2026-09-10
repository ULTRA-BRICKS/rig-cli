#!/usr/bin/env python3
"""Deterministic, read-only compiler for the explicitly authorized recovery audit.

Ticket prose is DATA. This program never executes commands copied from issues,
never treats a merge/test count as live acceptance, and never mutates issues.
The separate apply program uses reviewed plans and optimistic concurrency.
"""
from __future__ import annotations
import collections, hashlib, html, io, json, os, pathlib, re, tarfile, urllib.request, zipfile
import xml.etree.ElementTree as ET

BASE = pathlib.Path('docs/grooming/full-2026-09-10')
SPECS = pathlib.Path('docs/specs/recovery')
PORTAL = pathlib.Path('portal')
CACHE = pathlib.Path('.grooming-cache')
OWNER = 'alex-mextner'
BRANCH = 'docs/full-grooming-2026-09-10'
GIT = 'https://github.com/' + OWNER + '/'
DOC = GIT + 'rig-cli/blob/' + BRANCH + '/'
ROOT = GIT + 'rig-cli/issues/521'
RUN = GIT + 'rig-cli/actions/runs/34476344921'
ARTIFACTS = {'rig-cli':10151776129,'agent-tools':10151763018,'review-cli':10152031014,'task-cli':10151666633,'tg-cli':10151908993,'qa-cli':10151780251,'draw-cli':10151797202,'stt-cli':10151800546,'research-cli':10151812191,'dev-cli':10151818184,'pm-cli':10151851783}
# Explicit decisions, not title-similarity auto-closure. Unique acceptance and the
# complete before-image are retained in the canonical record before any close.
DUPLICATES = {'rig-cli#508':'rig-cli#507', 'task-cli#170':'task-cli#169', 'task-cli#172':'task-cli#169', 'task-cli#173':'task-cli#169'}
SUPERSEDED = {'agent-tools#542':'agent-tools#573'}

TOPICS = {
 'intake': {'title':'Task intake, canonical references and acceptance evidence','quote_ids':['222','214','215','238'],'pattern':r'task.?code|task.?ref|ticket|acceptance|screenshot|proof|task (?:gate|attach|classify|current|link|burn|gantt)','example':'A review of an uncommitted diff is process work, not a new product feature. A concrete bug found during that review is linked to its canonical product issue, original request, exact diff and reproducible failure. A local /tmp/proof.png path is retained as a historical reference, not presented as a remotely viewable attachment.','contract':'Preserve original requirements; distinguish product work, process records, verification gaps and duplicates. Validate repository-aware issue identity before writes. Preserve complete Markdown, including embedded headings. Uploaded evidence must resolve from another machine and identify the tested revision.'},
 'worktrees': {'title':'Worktree isolation and preservation of unfinished work','quote_ids':['216','043','049','082'],'pattern':r'worktree|checkout|primary|git reset|reset-hard|foreign.*branch','example':'Two agents work on the same repository in different .worktrees directories. One edits a relative src/app.py after its execution kernel restarts. The edit must still address its own checkout, not main or the other agent. A file outside any Git repository is a separate case, not automatically a forbidden main-checkout write.','contract':'One canonical worktree creation path; trusted per-operation target resolution; clear alternatives in refusal messages. Inspect dirty branches and unique commits before consolidation. Do not delete, reset or force-push another agent\'s work to make a dashboard green.'},
 'identity': {'title':'Native agent identity and cross-harness guard parity','quote_ids':['020','122','159','193'],'pattern':r'orchestrator|subagent|hook bridge|hook-bridge|pre-agent|pre.?tool|spawn_agent|agentId|agentKind|Monitor|no-long-inline','example':'A main OMP session and two native task children share an OS process. The children have distinct invocation/session identity, while the main call remains the orchestrator. A path suffix, recent transcript mtime, caller-supplied agent_id or process-global flag cannot prove which child made the call.','contract':'Normalize trusted native and launcher contexts through one carrier contract. Preserve main/child/grandchild/sibling isolation. Missing metadata is explicit unsupported/unknown, not a global harness exemption. Keep read-only diagnostic commands available according to the latest user instructions.'},
 'shipping': {'title':'Shipping policy, local CI parity and release consistency','quote_ids':['261','263','266','103'],'pattern':r'gh ship|ship\.sh|SHIP_|version.bump|release.*version|external.review|ruleset|review.thread|no-verify|pr.merge','example':'Hosted CI cannot start because its infrastructure is unavailable. The declared equivalent checks run locally at the exact PR head revision and their results are recorded. A genuine failed test is not reclassified as an infrastructure outage. Two parallel PRs must not both reuse a stale version bump from an unrelated checkout.','contract':'Separate hosted-CI availability from test correctness. Bind local verification, review roles, release metadata and approved exceptions to the target repository and revision. Do not disable unrelated protection gates or infer a human decision from missing data.'},
 'roles': {'title':'Independent review roles, models, routes and presets','quote_ids':['227','058','170','168'],'pattern':r'board|quorum|pool|role|effort|preset|fallback.model|MiMo|mimo|Grok|Fable|Astra|Terra|model.*compose','example':'Only one paid model is available. It performs four separate reviews with security, performance, correctness and test-design prompts. Four roles are covered, but the report still states one underlying model; it does not invent four independent models or disable the role requirement.','contract':'Model, provider, account, harness, effort and review role are distinct dimensions. Preserve requested roles through fallback, all supported modes and --pool 0. Record actual executed roles/routes, not planned seats or duplicate generic prompts.'},
 'health': {'title':'Account-specific quota, billing and recovery signals','quote_ids':['231','255','257','262','265'],'pattern':r'quota|usage|limit|billing|cooldown|capacity|account|credential|authenticated','example':'Claude account A has a confirmed 100% usage window, B has 20%, and C cannot be read. Prefer B. A does not exhaust B, C is not healthy, and a local database lock is not evidence that the entire provider fleet exhausted its quota. Show observation times and API-reported reset windows separately from token usage.','contract':'Carry source, time, scope, coverage and unknown states. Separate quota, authentication, entitlement, transport and storage failures. Only a nonempty fully covered candidate set with applicable fresh affirmative evidence supports all_exhausted. Detect recovered routes without permanently trusting stale cooldowns.'},
 'execution': {'title':'Progress-aware execution, fallback and cancellation','quote_ids':['025','239','255','265'],'pattern':r'hang|stall|silence|heartbeat|timeout|stdin|retry|reaper|detach|job|cancel','example':'A remote reviewer emits output, then stops. Another remains silent but supplies a trustworthy progress signal. Neither low local CPU nor silence alone identifies quota exhaustion. Preserve partial output, try compatible alternatives where policy permits, and record cancellation/terminal cause without orphaning the owned child.','contract':'Distinguish message acceptance, transport receipt, progress and completed work. Use one execution state machine. Do not solve lost delivery by inserting arbitrary sleeps; explicit user-requested schedules and bounded test-run budgets are different concepts.'},
 'telegram': {'title':'Telegram routing, durable decisions and concurrent delivery','quote_ids':['201','233','236','239'],'pattern':r'telegram|tg-ctl|tgctl|reply|pane|topic|ask |question|approval|hatch|callback|agent picker|message','example':'The user replies to landing while rig-fable is busy. Delivery to landing and the next Telegram update continue without waiting for rig-fable\'s full turn. A late Approve is retained for the same scoped action after reconnect, but a changed command, repository, commit or native session cannot reuse it.','contract':'Persist intent before acknowledging updates; preserve per-target ordering and bounded cross-target concurrency. Route by native generation, not reused pane/PID. Keep posted, dropped, unanswered, denied, approved, consumed and actually executed outcomes distinct. Test against a mock/test chat, never the operator\'s real conversation.'},
 'resources': {'title':'Fleet admission, memory safety and service lifecycle','quote_ids':['128','132','135','138','238'],'pattern':r'memory|OOM|swap|fleet|parking|idleTtl|zombie|orphan|service|launchd|daemon|restore|resurrect|PTY|spotlight','example':'Two launchers simultaneously request the final available fleet slot. Exactly one reservation succeeds; the other gets a visible queue/refusal. A crashed owner releases its reservation using generation-aware evidence. A service reporting stopped is checked against its actual owned process, not only a missing plist.','contract':'Enforce resource policy at the actual admission/lifecycle boundary, not only in MEMORY.md. Bound caches and logs; verify recovery and disabled-state behavior. Reused PIDs and untracked legacy services cannot justify killing arbitrary processes or declaring a machine safe.'},
 'telemetry': {'title':'Truthful telemetry, scalable dashboards and historical accounting','quote_ids':['064','069','071','230'],'pattern':r'stats|dashboard|metrics|logs?|cache|timeline|feedback|cost|adoption','example':'A dashboard reads a synthetic 154,000-record corpus. Its initial cold-cache state says loading, not zero usage; later source updates do not rewrite earlier account/role attribution. A review invocation is not counted once as a CLI call and again as unrelated agent-tools adoption.','contract':'Use stable run/attempt/account identifiers, explicit classification and bounded indexed reads. Preserve every retry/skip/failure record and distinguish missing data. Migrate archives with count/content verification and rollback before retiring old storage.'},
 'specs': {'title':'Versioned specification review and discoverable documentation','quote_ids':['114','028','214','215'],'pattern':r'spec.?web|specification|spec review|anchor|evolve|README|ROADMAP|documentation|docs:|research|Haft','example':'A document changes twice with the same mtime and repeats the same quoted phrase in two sections. Content hashes distinguish captured revisions. A comment keeps its original quote and revision; a fuzzy successor is shown as suggested or ambiguous, never silently confirmed. Compare chosen revisions without replacing the live file.','contract':'Separate captured history from unobserved edits. Persist anchor transitions and preserve comments on restart/concurrent writes. Publish source-linked examples and editable diagrams. Historical/draft/skipped specifications retain their status instead of becoming implementation claims.'},
 'provisioning': {'title':'Declarative provisioning, reproducible installation and honest drift','quote_ids':['019','018','221','143'],'pattern':r'apply|provision|install|config|schema|stack|linter|MCP|permission|tool|settings|shell|zsh|completion','example':'The operator disables a managed integration and runs reconciliation. Only owned artifacts are removed; manual files are preserved. The same declared state drives apply and status. A managed-file comment names the supported configuration command rather than merely saying do not edit.','contract':'One source of truth for schema, planning, writing and drift. Installation on disk is distinct from PATH resolution and from the running process revision. Unsupported platform/configuration states are explicit, and existing disabled choices are not silently re-enabled.'},
 'testing': {'title':'Hermetic integration and real-platform acceptance','quote_ids':['005','152','177','202','233'],'pattern':r'test|clean.?room|container|parity|QA|smoke|CI|e2e','example':'A Linux fixture proves hook dispatch for a mocked OMP event. That is not evidence that a real OMP child on macOS emits the fixture, or that an installed LaunchAgent works. Record those as separate matrix cells and use a real supported test environment only for the corresponding acceptance.','contract':'Pin source revisions, isolate HOME/accounts/network side effects, report actual runner/dependency versions and preserve failed/skipped cases. Green unit tests do not certify unexecuted live/UI flows. Reproduce flaky ordering with barriers and observable completion, not larger sleeps.'},
 'auxiliary': {'title':'Auxiliary CLI behavior and end-to-end user workflows','quote_ids':['202','028'],'pattern':r'.','example':'A CLI is invoked from outside its repository with a valid input, an invalid input and an unavailable optional backend. Successful output, nonzero failure, cancellation and cleanup are separately asserted. A mock backend passing does not establish that a live account, local model or audio device was exercised.','contract':'Preserve each repository\'s actual product boundary, documented arguments and error semantics. Do not translate a setup/review process record into a new feature or assume paid/live resources from a unit fixture.'},
 'process': {'title':'Legacy process records and salvageable review findings','quote_ids':['222','238'],'pattern':r'(?!)','example':'A ticket says Review uncommitted version bump. Its review findings and linked commit/PR are retained. Any actionable product bug is linked to a canonical issue before retiring the process record. A cancelled review task is not counted as a newly delivered product feature.','contract':'Separate ephemeral agent work from the product tracker. Never discard comments, unique acceptance, branches or evidence when consolidating. Similar titles across repositories may be separate integration slices and are not automatic duplicates.'}
}


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n')


def artifact(number, target):
    if target.exists() and any(target.iterdir()): return
    target.mkdir(parents=True, exist_ok=True)
    req=urllib.request.Request('https://api.github.com/repos/alex-mextner/rig-cli/actions/artifacts/'+str(number)+'/zip',headers={'Authorization':'Bearer '+os.environ['GH_TOKEN'],'Accept':'application/vnd.github+json'})
    with urllib.request.urlopen(req,timeout=120) as response: raw=response.read()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for name in archive.namelist():
            if name.startswith('/') or '..' in pathlib.PurePosixPath(name).parts: raise ValueError('Unsafe artifact member')
        archive.extractall(target)


def classify(repo, title):
    if re.match(r'(?i)^(?:Review (?:the )?(?:current )?(?:uncommitted|staged|version|release|\d)|Audit .* (?:diff|change)|Assess .* (?:diff|bump))',title): return 'process'
    # Specific product paths beat general words such as test/config/log.
    order=['worktrees','identity','shipping','roles','health','telegram','resources','telemetry','specs','intake','execution','testing','provisioning','auxiliary']
    if repo=='task-cli': order=['worktrees','shipping','intake','specs','testing','provisioning','process','auxiliary']
    if re.search(r'(?i)spec.?web|anchor|evolve',title): return 'specs'
    if re.search(r'(?i)hatch|approval|ask |question',title) and repo in ('agent-tools','tg-cli'): return 'telegram'
    if re.search(r'(?i)hang|stall|silence|heartbeat|timeout|retry|detach',title): return 'execution'
    for name in order:
        if re.search(TOPICS[name]['pattern'],title,re.I): return name
    return 'auxiliary'


def test_results(repo):
    path=CACHE/'tests'/repo; artifact(ARTIFACTS[repo],path)
    summary=json.loads((path/'summary.json').read_text())
    cases=[]
    if (path/'pytest.xml').exists():
        root=ET.parse(path/'pytest.xml').getroot()
        for el in root.iter('testcase'):
            failure=el.find('failure'); error=el.find('error'); skip=el.find('skipped')
            status='failed' if failure is not None or error is not None else ('skipped' if skip is not None else 'passed')
            cases.append({'module':el.get('classname',''),'name':el.get('name',''),'status':status,'message':(failure if failure is not None else error).get('message','') if failure is not None or error is not None else ''})
        summary['test_counts']=dict(collections.Counter(c['status'] for c in cases))
    else:
        text=(path/'command-1.log').read_text(errors='replace')
        summary['test_counts']={key:int(re.findall(r'(?m)^\s*(\d+) '+word+r'\s*$',text)[-1]) for key,word in [('passed','pass'),('failed','fail'),('skipped','skip')]}
    summary['test_cases']=cases
    summary['run_url']=RUN
    summary['interpretation']='Isolated repository-suite evidence only; no live-machine/provider acceptance.'
    if repo=='review-cli': summary['qualification']='First pytest run omitted ImageMagick 7: visual failures require the dependency-correct follow-up. Declared smoke runner passed with its visual suite skipped. One effort/board test also failed; do not count all 66 as product bugs.'
    if repo=='tg-cli': summary['qualification']='Two failing assertions: durable late-answer callback ordering and a watermark warning under a rewritten Python PATH. Repeat/cause analysis is separate from the full-suite result.'
    return summary


def source_index(repo, live):
    result={}
    with tarfile.open(live/(repo+'-source.tar.gz')) as archive:
        for member in archive:
            if not member.isfile() or member.size>3000000: continue
            path='/'.join(pathlib.PurePosixPath(member.name).parts[1:])
            if pathlib.PurePosixPath(path).suffix not in ('.py','.ts','.js','.sh','.md','.toml','.json','.yaml','.yml'): continue
            f=archive.extractfile(member)
            if f: result[path]=f.read().decode('utf-8',errors='replace')
    return result


def code_paths(text, files):
    names=re.findall(r'(?<![\w/])(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_./-]+\.(?:py|ts|js|sh|md|toml|json|yaml|yml)',text)
    names+=re.findall(r'`([A-Za-z0-9_.-]+\.(?:py|ts|js|sh|md|toml|json|yaml|yml))`',text)
    valid=sorted({p.rstrip('.,') for p in names if p.rstrip('.,') in files})
    absent=sorted({p.rstrip('.,') for p in names if p.rstrip('.,') not in files and not p.startswith(('github.com/','api.github.com/','Users/','home/','tmp/'))})
    return valid,absent


def quote_md(q, association):
    text=q['edited'].replace('\n','\n> ')
    origin=q['context']; pages=', '.join(map(str,q.get('pages',[])))
    return ('> '+text+'\n\n'+f"Source **Q{q['id']}**; {q['source_kind']}; {origin}"+(f'; PDF page(s) {pages}' if pages else '')+'. '+association+('\n\n**Context fragment:** do not interpret as a standalone approval.' if q.get('context_incomplete') else '')+'\n\n<details><summary>Original wording (before editorial correction)</summary>\n\n> '+q['original'].replace('\n','\n> ')+'\n\n</details>')


def inline_quotes(body):
    found=[]
    for match in re.finditer(r'(?m)(?:^>[^\n]*(?:\n|$))+',body):
        text=re.sub(r'(?m)^>\s?','',match.group()).strip()
        before=body[max(0,match.start()-400):match.start()]
        if re.search('[А-Яа-яЁё]',text) and re.search(r'Alex|Алекс|verbatim|user|CTO|исходн|цитат',before,re.I):
            found.append(text)
    return list(dict.fromkeys(found))


def issue_links(text, repo, all_issues, all_prs):
    linked=[]; invalid=[]; prkeys=set()
    for owner,name,kind,num in re.findall(r'https://github.com/([\w.-]+)/([\w.-]+)/(issues|pull)/(\d+)',text):
        key=name+'#'+num
        if owner!=OWNER: continue
        if kind=='pull':
            if key in all_prs: prkeys.add(key)
            else: invalid.append({'reference':key,'kind':'pull','reason':'Not present in complete public snapshot; may be private/moved/new.'})
        elif key in all_issues: linked.append(key)
        elif key in all_prs: invalid.append({'reference':key,'kind':'issue','reason':'The number identifies a pull request in this snapshot, not an issue.'})
    for name,num in re.findall(r'\b([a-z][a-z0-9-]*-cli|agent-tools)#(\d+)\b',text):
        key=name+'#'+num
        if key in all_issues: linked.append(key)
        elif key in all_prs: prkeys.add(key)
    for num in re.findall(r'(?<![\w/#])#(\d+)\b',text):
        key=repo+'#'+num
        if key in all_issues: linked.append(key)
        elif key in all_prs: prkeys.add(key)
    return sorted(set(linked)), sorted(prkeys), invalid


def main():
    for path in (BASE,SPECS,PORTAL,CACHE): path.mkdir(parents=True,exist_ok=True)
    live=CACHE/'inventory';artifact(10151381611,live)
    inventory=json.loads((live/'summary.json').read_text())
    registry=json.loads((BASE/'quote-registry.json').read_text()); quotes={q['id']:q for q in registry['records']}
    all_issues={};all_prs={};comments=collections.defaultdict(list);sources={};tests={}
    for repo in ARTIFACTS:
        for issue in json.loads((live/(repo+'.json')).read_text()):
            issue['repo']=repo;issue['key']=repo+'#'+str(issue['number']);all_issues[issue['key']]=issue
        for pr in json.loads((live/(repo+'-prs.json')).read_text()): all_prs[repo+'#'+str(pr['number'])]=pr
        for comment in json.loads((live/(repo+'-comments.json')).read_text()):
            number=comment['issue_url'].rsplit('/',1)[-1];comments[repo+'#'+number].append(comment)
        sources[repo]=source_index(repo,live);tests[repo]=test_results(repo)
    assert len(all_issues)==1519, 'Unexpected inventory: require explicit reconciliation, not silent truncation'
    direct=collections.defaultdict(list)
    for q in quotes.values():
        for ref in q.get('claimed_task_refs',[]):
            if ref in all_issues: direct[ref].append(q)
    catalog=[];plans=collections.defaultdict(list);quote_usage=collections.defaultdict(list)
    for key,issue in sorted(all_issues.items(),key=lambda item:(item[1]['repo'],item[1]['number'])):
        repo=issue['repo'];original=issue.get('body') or '';title=issue['title'];topic=classify(repo,title);sha=inventory['repositories'][repo]['source_sha']
        combined=original+'\n'+'\n'.join(c['body'] or '' for c in comments[key])
        valid_paths,absent_paths=code_paths(combined,sources[repo]);linked,prkeys,invalid=issue_links(combined,repo,all_issues,all_prs)
        associated=direct.get(key,[])
        provenance='explicit-export-association' if associated else 'topic-associated-request'
        if not associated:
            associated=[quotes[i] for i in TOPICS[topic]['quote_ids'] if i in quotes and not quotes[i].get('context_incomplete')][:2]
        raw_inline=inline_quotes(original)
        if not direct.get(key) and not raw_inline: provenance='original-feature-source-gap; standing/topic instruction available'
        # Short fragments retain every explicit association in the complete registry;
        # the issue front section prefers a complete thought over an isolated "go".
        selected=sorted(associated,key=lambda q:(q.get('context_incomplete',False),int(q['id']) if q['id'].isdigit() else 9999))[:3]
        for q in associated: quote_usage[q['id']].append(key)
        source_tests=[]
        for path in valid_paths:
            if not path.startswith('tests/') or not path.endswith('.py'): continue
            module=path[:-3].replace('/','.')
            cases=[c for c in tests[repo]['test_cases'] if c['module']==module or c['module'].startswith(module+'.')]
            source_tests.append({'path':path,'counts':dict(collections.Counter(c['status'] for c in cases)),'qualification':'Only named tests collected by this run; no inference of live acceptance.'})
        checks=re.findall(r'(?m)^\s*- \[([ xX])\] (.*)$',original)
        flags=[]
        if re.search(r'(?i)\bforced:|force.checked',original):flags.append('Historical forced acceptance must be re-evidenced; not automatically accepted by this pass.')
        if re.search(r'(?i)(?:!\[[^\]]*\]\((?:/tmp/|/Users/|file:)|proof.*?/tmp/)',original):flags.append('Historical local-path proof cannot be verified remotely; do not call the feature broken solely for this reason.')
        if issue['state']=='closed' and any(c[0]==' ' for c in checks):flags.append('Closed with unchecked criteria: preserve closure history and explicitly reverify the outstanding outcome.')
        if not checks and topic!='process':flags.append('No machine-readable acceptance checklist found; add outcome-specific cases, not a fabricated completed checklist.')
        if absent_paths:flags.append('Some referenced paths are absent from the inspected tree; these may be historical/renamed, not automatically missing implementation.')
        if invalid:flags.append('Some references are unresolved or issue/PR-ambiguous; see the typed reference ledger.')
        canonical=DUPLICATES.get(key) or SUPERSEDED.get(key)
        state_change='duplicate' if key in DUPLICATES else ('not_planned' if key in SUPERSEDED else None)
        preservation=[other for other,target in {**DUPLICATES,**SUPERSEDED}.items() if target==key]
        record={'key':key,'repo':repo,'number':issue['number'],'id':issue['id'],'node_id':issue['node_id'],'title':title,'provider_state':issue['state'],'state_reason':issue.get('state_reason'),'topic':topic,'classification':'process-record' if topic=='process' else ('epic' if '[EPIC]' in title.upper() else 'product-or-maintenance'),'provenance':provenance,'quote_ids':[q['id'] for q in associated],'existing_source_attributed_quotes':raw_inline,'source_sha':sha,'existing_paths':valid_paths,'unresolved_historical_paths':absent_paths,'named_test_evidence':source_tests,'related_issues':linked,'linked_prs':[{'key':p,'state':all_prs[p]['state'],'merged_at':all_prs[p].get('merged_at'),'merge_commit_sha':all_prs[p].get('merge_commit_sha')} for p in prkeys],'reference_gaps':invalid,'verification_flags':flags,'canonical':canonical,'preserves':preservation,'live_acceptance':'not-performed','verification_disposition':'historical-closure-not-reaccepted' if issue['state']=='closed' else 'open-outcome-requires-acceptance','original_body_sha256':hashlib.sha256(original.encode()).hexdigest(),'snapshot_updated_at':issue['updated_at']}
        spec=DOC+'docs/specs/recovery/'+topic+'.md'
        lead=['<!-- rig-grooming:2026-09-10:v1 -->', '## Request provenance and intended outcome',f'**Scope:** {TOPICS[topic]["title"]}. **Record:** {record["classification"]}.',f'**Recovery epic:** {ROOT} · **Specification:** {spec} · **Evidence ledger:** {DOC}docs/grooming/full-2026-09-10/issues/{repo}.json']
        if provenance.startswith('original-feature-source-gap'):
            lead.append('**Original feature-request gap:** no unambiguous original for this particular historical ticket was recovered from the supplied export or its source-attributed blockquotes. The quotation below is a related standing instruction, not a claim that Alex dictated this ticket title or every implementation detail. The complete original technical description is retained below.')
        if raw_inline:lead.append('The earlier description also contains source-attributed quotations; they remain unchanged in the retained record. Their presence is not independent transcript verification.')
        for q in selected:
            association='The export explicitly associates this request with this issue; that association is retained, not newly invented.' if q in direct.get(key,[]) else 'Editorial topic association/standing requirement; not proof of the original creation context of this issue.'
            lead.append(quote_md(q,association))
        if not selected:lead.append(quote_md(quotes['current-grooming-mandate'],'Current audit mandate only; original feature request unavailable.'))
        lead+=['## Current evidence and next verification',f'Inspected source: [{repo}@{sha[:12]}]({GIT}{repo}/tree/{sha}). [Fresh isolated suite]({RUN}): '+json.dumps(tests[repo]['test_counts'],ensure_ascii=False)+'. **This does not certify a live account, UI workflow, installed binary or macOS service.**']
        if tests[repo].get('qualification'):lead.append(tests[repo]['qualification'])
        if source_tests:lead.append('Named test-file results: '+'; '.join(f'`{r["path"]}` {r["counts"] or "not collected by this runner"}' for r in source_tests)+'.')
        if prkeys:lead.append('Linked implementation records: '+'; '.join(f'[{p}]({GIT}{p.replace("#","/pull/")}) — '+('merged '+str(all_prs[p]['merged_at']) if all_prs[p].get('merged_at') else all_prs[p]['state']+', not merged') for p in prkeys)+'.')
        for flag in flags:lead.append('- '+flag)
        lead+=['## Worked verification example',f'**Illustrative scenario for this concern (not an observed reproduction):** {TOPICS[topic]["example"]}', 'Apply the scenario to the exact operation, code paths and acceptance criteria in the retained description. Record the input, actual result, expected result, source revision and a durable artifact. A failure must be tied to this outcome before reopening; a passing unrelated suite must not close it.']
        if canonical:lead+=['## Consolidation decision',f'Canonical outcome: [{canonical}]({GIT}{canonical.replace("#","/issues/")}). '+('The older entire-harness exemption direction is superseded by the explicit native-identity requirement; the underlying portability defect remains tracked.' if key in SUPERSEDED else 'This is the same outcome, not an additional delivered feature. The complete prior description, criteria and comments are preserved in the canonical audit before retiring this duplicate.')]
        if preservation:
            lead+=['## Consolidated requirements and retained findings']
            for other in preservation:
                lead.append(f'<details><summary>Preserved {other}: original scope and all source comments</summary>\n\n'+(all_issues[other].get('body') or '(No original body)')+'\n\n'+'\n\n'.join('Source comment '+c['html_url']+'\n\n'+(c['body'] or '') for c in comments[other])+'\n\n</details>')
        lead+=['## Original technical scope, acceptance and delivery history',original or '_The original issue had no description. The source gap is not filled with invented implementation requirements._','<!-- /rig-grooming:2026-09-10:v1 -->']
        body='\n\n'.join(lead).strip()+'\n'
        if len(body)>64000:
            record['mutation_blocker']='Expanded body exceeds safe GitHub size. Original retained; manual concise rewrite required.'
            body=None
        plan={'number':issue['number'],'key':key,'node_id':issue['node_id'],'expected_body_sha256':record['original_body_sha256'],'expected_updated_at':issue['updated_at'],'expected_title':title,'expected_state':issue['state'],'body':body,'topic':topic,'canonical':canonical,'state_change':state_change,'preserves':preservation,'labels_add':['grooming:2026-09-10',('record:process' if topic=='process' else 'verification:required')]+(['source:gap'] if provenance.startswith('original-feature-source-gap') else []),'source':record}
        catalog.append(record);plans[repo].append(plan)
    for repo,rows in plans.items():
        write_json(BASE/'plans'/(repo+'.json'),rows)
        write_json(BASE/'issues'/(repo+'.json'),[r for r in catalog if r['repo']==repo])
        write_json(BASE/'before'/(repo+'.json'),[{'key':i['key'],'title':i['title'],'body':i.get('body'),'state':i['state'],'updated_at':i['updated_at'],'comments':comments[i['key']]} for i in all_issues.values() if i['repo']==repo])
    # Every original supplied record has a visible disposition, including fragments
    # and excluded agent summaries. Unmapped does not become completed or discarded.
    write_json(BASE/'request-coverage.json',{'source_pdf_sha256':registry['source_sha256'],'records':[{'id':q['id'],'source_kind':q['source_kind'],'context_incomplete':q.get('context_incomplete',False),'associated_issues':sorted(set(quote_usage[q['id']])),'disposition':'associated' if quote_usage[q['id']] else 'retained; no unambiguous issue association established'} for q in quotes.values()], 'excluded_records':registry['excluded_records']})
    write_json(BASE/'test-results.json',{repo:{k:v for k,v in result.items() if k!='test_cases'} for repo,result in tests.items()})
    write_json(BASE/'test-cases.json',{repo:result['test_cases'] for repo,result in tests.items()})
    write_json(BASE/'pull-requests.json',[dict(key=key,**{k:v for k,v in pr.items() if k not in ('head','base')}) for key,pr in all_prs.items()])
    summary={'scope':'all public issues, all states and available history; private harness-cli separate','public_issues':len(catalog),'public_prs':len(all_prs),'source_registry_records':len(quotes),'excluded_export_records':len(registry['excluded_records']),'original_feature_source_gaps':sum(r['provenance'].startswith('original-feature-source-gap') for r in catalog),'process_records':sum(r['topic']=='process' for r in catalog),'planned_duplicates':DUPLICATES,'superseded_direction':SUPERSEDED,'body_size_blockers':[r['key'] for r in catalog if r.get('mutation_blocker')],'issue_mutations':0,'by_topic':dict(collections.Counter(r['topic'] for r in catalog)),'by_repo':inventory['repositories'],'runtime_acceptance':'isolated suites only; platform/live-provider flows explicitly unverified'}
    write_json(BASE/'corpus-summary.json',summary)
    for name,topic in TOPICS.items():
        rows=[r for r in catalog if r['topic']==name]
        text=['# '+topic['title'],'Status: recovery specification / proposed contract; not a claim of completed implementation.',f'Owner and acceptance coordinator: {ROOT}','## Original requests and editorial provenance']
        for ident in topic['quote_ids']:
            if ident in quotes:text.append(quote_md(quotes[ident],'Cross-ticket thematic requirement; check context before applying to a narrower issue.'))
        text+=['## Boundary and intended behavior',topic['contract'],'## Realistic worked example',topic['example'],'## Acceptance procedure','1. Reproduce the exact operation in an isolated checkout at a named revision.\n2. Capture actual and expected behavior; distinguish a product failure from a missing dependency or unsupported platform.\n3. Exercise the corresponding producer/consumer boundary, failure/restart case and a negative case.\n4. Attach durable output and separately record any real-machine/provider/UI acceptance.\n5. Close only the accepted outcome; a merged slice does not close a larger parent.','## State-transition diagram','```mermaid\nflowchart LR\n  Q[Source request + context] --> T[Canonical task + scope]\n  T --> C[Implementation at exact revision]\n  C --> U[Isolated regression check]\n  U --> I[Producer / consumer integration]\n  I --> L[Required real-environment acceptance]\n  L --> A[Accepted outcome]\n  U -->|failure or unknown| T\n  I -->|gap| T\n```','## Complete linked backlog']
        text+=['| Ticket | Scope | Provider state | Verification |','|---|---|---|---|']
        for r in rows:text.append(f'| [{r["key"]}]({GIT}{r["key"].replace("#","/issues/")}) | '+r['title'].replace('|','\\|')+f' | {r["provider_state"]} | {r["verification_disposition"]} |')
        (SPECS/(name+'.md')).write_text('\n\n'.join(text)+'\n')
    # Preserve all existing public specs as explicitly historical source documents;
    # recovery review/addenda and current task mappings are separate, never silent rewrites.
    spec_catalog=[]
    for repo,files in sources.items():
        for path,content in files.items():
            if not path.endswith('.md') or not ('docs/specs/' in path or path in ('SPEC.md','SPEK.md','MASTER_SPEC.md')):continue
            topic=classify(repo,path+' '+content.split('\n',1)[0]); slug=repo+'--'+re.sub(r'[^a-zA-Z0-9-]+','-',path).strip('-')
            dest=SPECS/'existing'/(slug+'.md');dest.parent.mkdir(parents=True,exist_ok=True)
            relevant=[r for r in catalog if r['repo']==repo and (path in r['existing_paths'] or r['topic']==topic)]
            header=f'# Review addendum: {repo}/{path}\n\nSource revision: `{inventory["repositories"][repo]["source_sha"]}`.\n\nThis source document is retained with its original draft/approved/skipped status. This addendum does not approve previously skipped work. Root `rig-cli/SPEC.md` describes Evolve, not the entire rig ecosystem.\n\nCurrent recovery contract: [{TOPICS[topic]["title"]}](../{topic}.md). Governing epic: {ROOT}.\n\n## Example and revalidation\n\n{TOPICS[topic]["example"]}\n\n## Source quotations\n\n'
            header+='\n\n'.join(quote_md(quotes[i],'Thematic recovery requirement, not retroactive authorship of this entire specification.') for i in TOPICS[topic]['quote_ids'][:2] if i in quotes)
            header+='\n\n## Linked acceptance backlog\n\n'+'\n'.join('- ['+r['key']+']('+GIT+r['key'].replace('#','/issues/')+') — '+r['title'] for r in relevant)+'\n\n## Preserved specification\n\n'+content
            dest.write_text(header)
            spec_catalog.append({'id':slug,'repo':repo,'source_path':path,'source_sha':inventory['repositories'][repo]['source_sha'],'review_path':str(dest),'topic':topic,'status':'source retained; recovery addendum, not blanket reapproval'})
    # Include the previously committed public grooming specifications as well.
    for p in live.glob('2026-09-10-*.md'):
        dest=SPECS/'previous'/p.name;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_text('# Prior grooming specification — retained baseline\n\nCurrent recovery coordinator: '+ROOT+'\n\nThis earlier design is not implementation evidence. Consult the current domain contracts and live issue ledger before executing it.\n\n'+p.read_text())
        spec_catalog.append({'id':'previous-'+p.stem,'review_path':str(dest),'status':'previous proposed specification; see current recovery contracts'})
    write_json(BASE/'spec-catalog.json',spec_catalog)
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    # Portal generation is a separate pure renderer, easy to inspect and test.
    renderer=pathlib.Path('tools/grooming/render.py')
    if renderer.exists():
        import runpy
        runpy.run_path(str(renderer),run_name='__main__')

if __name__=='__main__':main()
