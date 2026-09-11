"""Read observed execution records through the real review task --check command.

Usage: python check_review_mode_evidence.py --source /path/to/review-cli --matrix ./evidence --out ./checks
No mode handler or evidence reader is replaced. The matrix contains synthetic
model responses, not live-provider acceptance.
"""
from __future__ import annotations
import argparse, concurrent.futures, json, os, pathlib, re, subprocess, sys

def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=pathlib.Path,required=True)
    parser.add_argument('--matrix',type=pathlib.Path,required=True)
    parser.add_argument('--out',type=pathlib.Path,required=True)
    args=parser.parse_args();source=args.source.resolve();matrix=args.matrix.resolve();out=args.out.resolve()
    out.mkdir(parents=True,exist_ok=False)
    records=json.loads((matrix/'matrix.json').read_text())
    def run(row):
        name=row.get('name','')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+',name) or 'probe_error'in row:
            return {'case':name,'error':'Invalid or unsuccessful observation'}
        home=matrix/name/'home'
        env={'HOME':str(home),'PATH':os.defpath,'PYTHONPATH':str(source),'PYTHONNOUSERSITE':'1',
             'LANG':'C.UTF-8','XDG_CONFIG_HOME':str(home/'.config'),'XDG_CACHE_HOME':str(home/'.cache'),
             'REVIEW_STATS_FILE':str(home/'stats.jsonl'),'REVIEW_LOG_DIR':str(home/'logs')}
        command=[sys.executable,str(source/'bin/review'),'task','444','--check','--min-iter','1',
                 '--min-roles','3','--json','-C',str(source)]
        try:process=subprocess.run(command,cwd=source,env=env,text=True,capture_output=True,timeout=45)
        except subprocess.TimeoutExpired:return {'case':name,'error':'Evidence reader exceeded its watchdog'}
        path=out/name;path.mkdir()
        (path/'stdout.txt').write_text(process.stdout);(path/'stderr.txt').write_text(process.stderr)
        try:details=json.loads(process.stdout)
        except ValueError:return {'case':name,'error':'Reader did not produce valid JSON','exit_code':process.returncode}
        return {'case':name,'argv':command,'exit_code':process.returncode,'check':details}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(run,records))
    (out/'results.json').write_text(json.dumps(results,indent=2)+'\n')
    errors=sum('error'in row for row in results)
    print(f'Collected {len(results)} evidence-reader results; {errors} experiment errors.')
    return 1 if errors else 0

if __name__=='__main__':raise SystemExit(main())
