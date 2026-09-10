#!/usr/bin/env python3
"""Public-plan transport binding for the repository-scoped mutation engine.

Some full-repository plans exceed GitHub Contents API's inline-content limit.
Fetch the exact immutable raw object, without any Authorization header. All
mutations still use the explicitly scoped API instance in apply.py.
"""
from __future__ import annotations
import hashlib,importlib.util,json,pathlib,re,urllib.request
path=pathlib.Path(__file__).with_name('apply.py')
spec=importlib.util.spec_from_file_location('grooming_apply_engine',path)
engine=importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)

def immutable_public_plan(api,repo,ref):
    if repo not in engine.ALLOWED or not re.fullmatch('[0-9a-f]{40}',ref):
        raise ValueError('Unexpected repository or mutable plan ref')
    url='https://raw.githubusercontent.com/alex-mextner/rig-cli/'+ref+'/docs/grooming/full-2026-09-10/plans/'+repo+'.json'
    with urllib.request.urlopen(url,timeout=120) as response:
        if response.url!=url:
            raise ValueError('Unexpected redirect for immutable public plan')
        raw=response.read(25_000_001)
    if len(raw)>25_000_000:raise ValueError('Plan exceeds reviewed size bound')
    rows=json.loads(raw)
    if not isinstance(rows,list) or len({r['number'] for r in rows})!=len(rows):raise ValueError('Invalid plan collection')
    for row in rows:
        if row['key']!=repo+'#'+str(row['number']):raise ValueError('Cross-repository plan row')
        if row['body'] is not None and (not row['body'].startswith(engine.MARK) or len(row['body'])>64000):raise ValueError('Invalid plan body')
    return rows,hashlib.sha256(raw).hexdigest()

engine.get_plan=immutable_public_plan
if __name__=='__main__':engine.main()
