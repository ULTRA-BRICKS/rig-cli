"""Collect current-mode observations without calling live model providers.

Usage: python run_review_mode_matrix.py --source /path/to/review-cli --out ./evidence
The source must match the audited revision checked by probe_review_modes.py.
These are characterization observations, not 56 passing feature-acceptance tests.
"""
from __future__ import annotations
import argparse, concurrent.futures, json, pathlib, subprocess, sys

def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=pathlib.Path,required=True)
    parser.add_argument('--out',type=pathlib.Path,required=True)
    parser.add_argument('--jobs',type=int,default=3)
    args=parser.parse_args()
    if not 1 <= args.jobs <= 4:parser.error('--jobs must be between 1 and 4')
    source=args.source.resolve();out=args.out.resolve()
    out.mkdir(parents=True,exist_ok=False)
    script=pathlib.Path(__file__).with_name('probe_review_modes.py')
    cases=[]
    for mode in ('diff','just-ask','quorum','brainstorm'):
        for models in (0,1,2,4):
            for selection in ('default','explicit'):
                cases.append((mode,models,selection,'flag',None,''))
        for task in ('env','absent'):cases.append((mode,1,'default',task,None,''))
        for pool in (1,3,5):cases.append((mode,2,'explicit','flag',pool,''))
        cases.append((mode,1,'explicit','flag',3,'correctness'))
    def run(case):
        mode,models,selection,task,pool,failure=case
        name=f'{mode}-m{models}-{selection}-{task}-pool{pool}-fail{failure or "none"}'
        destination=out/name
        command=[sys.executable,str(script),'--source',str(source),'--out',str(destination),
                 '--mode',mode,'--available',str(models),'--selection',selection,'--task-source',task]
        if pool is not None:command+=['--pool',str(pool)]
        if failure:command+=['--fail-role',failure]
        try:process=subprocess.run(command,capture_output=True,text=True,timeout=45)
        except subprocess.TimeoutExpired:return {'name':name,'probe_error':'Observation exceeded its watchdog'}
        if process.returncode:return {'name':name,'probe_error':process.stderr[-2000:]}
        result=json.loads((destination/'report.json').read_text());result['name']=name
        return result
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        records=list(pool.map(run,cases))
    (out/'matrix.json').write_text(json.dumps(records,ensure_ascii=False,indent=2)+'\n')
    errors=sum('probe_error' in row for row in records)
    print(f'Collected {len(records)} observations; {errors} probe errors. Product outcomes are in matrix.json.')
    return 1 if errors else 0

if __name__=='__main__':raise SystemExit(main())
