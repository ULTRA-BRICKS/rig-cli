"""Observe real review-cli mode handlers and persisted statistics offline.

Only provider availability and the network/model leaf are replaced. CLI parsing,
selection, handlers, prompts, threads, moderator, rounds and statistics are real.
Run once per process so product module constants resolve against isolated HOME.
"""
from __future__ import annotations
import argparse, contextlib, hashlib, io, json, os, pathlib, re, sys, threading

p=argparse.ArgumentParser()
p.add_argument('--source',type=pathlib.Path,required=True)
p.add_argument('--out',type=pathlib.Path,required=True)
p.add_argument('--mode',choices=['diff','just-ask','quorum','brainstorm'],required=True)
p.add_argument('--available',type=int,default=1)
p.add_argument('--selection',choices=['default','explicit','configured'],default='default')
p.add_argument('--task-source',choices=['flag','env','absent'],default='flag')
p.add_argument('--pool',type=int)
p.add_argument('--fail-role',default='')
a=p.parse_args()
# A result may only identify the audited revision when its product files match.
source_files=sorted((a.source/'reviewlib').rglob('*.py'))
source_manifest='\n'.join(f'{f.relative_to(a.source).as_posix()} {hashlib.sha256(f.read_bytes()).hexdigest()}' for f in source_files)
expected_tree='d05bd16bbdb18d971d986ca8456646a0b6c2a5288a1b4a767f6bd430e92bd90d'
if hashlib.sha256(source_manifest.encode()).hexdigest()!=expected_tree:
    raise SystemExit('Product source differs from the audited revision; do not relabel its results.')
a.out.mkdir(parents=True,exist_ok=False)
# Make every product path private to this single observation.
home=a.out/'home';home.mkdir(exist_ok=True)
for k in list(os.environ):
    if k.startswith(('REVIEW_','CLAUDE_','ANTHROPIC_','OPENAI_','GEMINI_','RIG_','SHIP_')) or k.endswith(('API_KEY','TOKEN','SECRET')):
        del os.environ[k]
os.environ.update(HOME=str(home),XDG_CONFIG_HOME=str(home/'.config'),XDG_CACHE_HOME=str(home/'.cache'),PYTHONNOUSERSITE='1',REVIEW_FAKE_BACKEND='1',REVIEW_LOG_DIR=str(home/'logs'),REVIEW_STATS_FILE=str(home/'stats.jsonl'),GEMINI_ENV_FILE=str(home/'absent.env'))
sys.path.insert(0,str(a.source))
from reviewlib import backends, cli
from reviewlib.config import DEFAULT_MODELS, REVIEW_ROLES
from reviewlib.modes import PERSONAS
from reviewlib.modes.brainstorm import MODERATOR_PROMPT_LEADIN,SYNTHESIS_PROMPT_MARKER

# Choose named entries used by BOTH the flat defaults and reviewer boards.
# Do not mistake a bare CLI alias (e.g. codex) for availability of every model.
priority=['oc:xai/grok-4.6','gemini','claude:claude-opus-4-8','codex:gpt-6-astra']
models=priority[:a.available]
# At zero, an explicit list still describes a real requested but unavailable model.
requested=models or [list(DEFAULT_MODELS)[0]]
if a.selection=='configured':
    config=home/'.config/review-cli/config.yaml';config.parent.mkdir(parents=True,exist_ok=True)
    import yaml
    config.write_text(yaml.safe_dump({'models':requested,'brainstorm_models':requested,'moderators':requested}))

def reason(model):
    return None if model in models else 'Unavailable in isolated provider inventory fixture'
backends.backend_unavailable_reason=reason

calls=[];lock=threading.Lock();original_fake=backends.review_fake

def observed_leaf(model,prompt,diff,cwd,timeout,round_no=0,effort=None):
    # This is the product's pre-existing env-gated fake leaf, wrapped for observation.
    # No mode, prompt builder, scheduler, result predicate or stats writer is stubbed.
    if SYNTHESIS_PROMPT_MARKER in prompt:kind='synthesis'
    elif MODERATOR_PROMPT_LEADIN in prompt or prompt.startswith('You are the MODERATOR'):kind='moderator'
    else:kind='expert'
    persona=re.match(r"You are a '([^']+)'",prompt)
    named_roles=[role for role,lens in REVIEW_ROLES.items() if lens in prompt]
    with lock:
        i=len(calls)+1
        entry={'index':i,'model':model,'round':round_no,'kind':kind,'persona':persona[1] if persona else None,'review_roles_in_prompt':named_roles,'prompt_sha256':hashlib.sha256(prompt.encode()).hexdigest(),'diff_sha256':hashlib.sha256(diff.encode()).hexdigest(),'prompt_file':f'prompt-{i:03}.txt'}
        calls.append(entry);(a.out/entry['prompt_file']).write_text(prompt)
    if model not in models:
        result=backends.ReviewResult(model=model,command='fixture:unavailable',returncode=1,stdout='',stderr='Unavailable in isolated provider inventory fixture')
    elif a.fail_role and a.fail_role in named_roles:
        result=backends.ReviewResult(model=model,command='fixture:permanent-failure',returncode=1,stdout='',stderr='Synthetic permanent role failure')
    else:result=original_fake(model,prompt,diff,cwd,timeout,round_no,effort)
    entry['exit_code']=result.returncode
    return result
backends.review_fake=observed_leaf

argv=[a.mode]
if a.mode!='diff':argv+=['Assess a storage migration without losing user edits.']
if a.task_source=='flag':argv+=['--task','444']
elif a.task_source=='env':os.environ['REVIEW_TASK_CODE']='444'
if a.selection=='explicit':argv+=['-m',','.join(requested)]
if a.pool is not None:argv+=['--pool',str(a.pool)]
if a.mode=='brainstorm':argv+=['--rounds','3','--max-rounds','3']
argv+=['-C',str(a.source)]
# A pipe is a supported real input. Only diff mode receives one.
sys.stdin=io.StringIO('diff --git a/example.py b/example.py\n--- a/example.py\n+++ b/example.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n' if a.mode=='diff' else '')
stdout,stderr=io.StringIO(),io.StringIO()
with contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):
    try:rc=cli.main(argv)
    except SystemExit as e:rc=e.code
(a.out/'stdout.txt').write_text(stdout.getvalue());(a.out/'stderr.txt').write_text(stderr.getvalue())
statsfile=home/'stats.jsonl';stats=[json.loads(s) for s in statsfile.read_text().splitlines()] if statsfile.exists() else []
experts=[c for c in calls if c['kind']=='expert']
report={'source_commit':'5430f9361468bf9bd1a66a4d4be8198024f989ba','argv':argv,'task_source':a.task_source,'selection':a.selection,'configured_pool':a.pool,'available_models':models,'cli_exit':rc,'real_handlers_used':True,'real_scheduler_used':True,'real_stats_writer_used':True,'model_transport':'product built-in offline fake with an observation wrapper','provider_availability':'fixture','expert_calls':len(experts),'moderator_calls':sum(c['kind']=='moderator' for c in calls),'synthesis_calls':sum(c['kind']=='synthesis' for c in calls),'expert_personas':sorted({c['persona'] for c in experts if c['persona']}),'expert_review_roles':sorted({r for c in experts for r in c['review_roles_in_prompt']}),'calls':calls,'stats':stats}
(a.out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({k:v for k,v in report.items() if k not in ('calls','argv')},ensure_ascii=False))
