#!/usr/bin/env python3
"""Host-side driver for the real-agent ticket-gate E2E (rig-cli#756). Stdlib only.

    python3 tests/omp_ticket_e2e/run_matrix.py -n 5 --scenarios A,B,C,D,E --variants base,fixed

What it does, in order:
  1. stages a docker build context: the host's omp binary, a `git archive` of each variant's
     agent-tools and rig-cli (so `base` can be the UNMODIFIED code at a ref while `fixed` is a
     branch), the real `task` SKILL.md, and in_container/;
  2. builds ONE image;
  3. runs N trials per (variant, scenario), each in a fresh container with the agent's credential
     injected at run time (never baked into the image, never in argv);
  4. calls analyze.py on every trial's raw artefacts and writes results.json + results.md.

Credential: ANTHROPIC_OAUTH_TOKEN (or ANTHROPIC_API_KEY) from the environment, else the freshest
unexpired Anthropic OAuth access token of the host omp (~/.omp/agent/agent.db, read-only). Only
the short-lived access token is used — never the refresh token — so the host's login is not
rotated by a container.

Heavy-run guard: pass --wrap 'flock /tmp/3d-cli-heavy.lock systemd-run --user --scope -q ...'
to prefix `docker build`/`docker run` (the container itself is capped by --memory/--cpus).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import analyze  # noqa: E402
from scenarios import SCENARIOS  # noqa: E402

RIG_REPO = HERE.parent.parent
IMAGE = "rig-omp-ticket-e2e"
DEFAULT_MODEL = "anthropic/claude-haiku-4-5"
TASK_SKILL_CANDIDATES = (
    Path.home() / ".agents/skills/task/SKILL.md",
    Path.home() / "xp/task-cli/tasklib/skill/SKILL.md",
)


def run(cmd: list[str], *, wrap: list[str], timeout: int, env: dict[str, str] | None = None,
        check: bool = True) -> subprocess.CompletedProcess:
    full = [*wrap, *cmd]
    proc = subprocess.run(full, capture_output=True, text=True, timeout=timeout, env=env)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{shlex.join(cmd)} exited {proc.returncode}\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")
    return proc


def git_archive(spec: str, dest: Path) -> str:
    """`path@ref` -> extract that tree into dest; returns the resolved commit sha."""
    repo, _, ref = spec.partition("@")
    ref = ref or "HEAD"
    sha = subprocess.run(["git", "-C", repo, "rev-parse", ref], capture_output=True, text=True, check=True).stdout.strip()
    dest.mkdir(parents=True, exist_ok=True)
    arc = subprocess.Popen(["git", "-C", repo, "archive", sha], stdout=subprocess.PIPE)
    subprocess.run(["tar", "-x", "-C", str(dest)], stdin=arc.stdout, check=True)
    if arc.wait() != 0:
        raise RuntimeError(f"git archive failed for {spec}")
    return sha


def find_omp_binary(explicit: str | None) -> Path:
    for cand in (explicit, os.environ.get("OMP_BIN"), str(Path.home() / ".local/lib/omp/omp")):
        if cand and Path(cand).is_file() and os.access(cand, os.X_OK):
            return Path(cand)
    raise SystemExit("omp binary not found: pass --omp-bin or set OMP_BIN (~/.local/lib/omp/omp)")


def find_task_skill() -> Path:
    for cand in TASK_SKILL_CANDIDATES:
        if cand.is_file():
            return cand
    raise SystemExit("task SKILL.md not found (install task-cli: ~/.agents/skills/task/SKILL.md)")


def credential_env() -> dict[str, str]:
    """The agent credential as an env mapping (never printed, never put in argv)."""
    for name in ("ANTHROPIC_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
        if os.environ.get(name):
            return {name: os.environ[name]}
    db = Path.home() / ".omp/agent/agent.db"
    if not db.is_file():
        raise SystemExit("no credential: set ANTHROPIC_OAUTH_TOKEN/ANTHROPIC_API_KEY or log in with omp")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    best: dict | None = None
    for (data,) in con.execute("select data from auth_credentials where provider='anthropic' and disabled_cause is null"):
        cred = json.loads(data)
        if cred.get("expires", 0) / 1000 > time.time() + 900 and (best is None or cred["expires"] > best["expires"]):
            best = cred
    if best is None:
        raise SystemExit("no unexpired Anthropic OAuth token in the host omp store; run omp once to refresh it")
    return {"ANTHROPIC_OAUTH_TOKEN": best["access"]}


def stage_context(ctx: Path, args: argparse.Namespace, variants: list[str]) -> dict[str, str]:
    shutil.copy2(find_omp_binary(args.omp_bin), ctx / "omp")
    (ctx / "task-skill").mkdir()
    shutil.copy2(find_task_skill(), ctx / "task-skill" / "SKILL.md")
    shutil.copytree(HERE / "in_container", ctx / "in_container")
    shutil.copy2(HERE / "Dockerfile", ctx / "Dockerfile")
    shas: dict[str, str] = {}
    for variant in variants:
        shas[f"{variant}/agent-tools"] = git_archive(getattr(args, f"agent_tools_{variant}"), ctx / "variants" / variant / "agent-tools")
        shas[f"{variant}/rig-cli"] = git_archive(getattr(args, f"rig_{variant}"), ctx / "variants" / variant / "rig-cli")
    return shas


def run_trial(args: argparse.Namespace, wrap: list[str], cred: dict[str, str], out_root: Path,
              variant: str, scenario_key: str, index: int) -> dict:
    scenario = SCENARIOS[scenario_key]
    out = out_root / variant / scenario_key / f"trial-{index:02d}"
    out.mkdir(parents=True, exist_ok=True)
    out.chmod(0o777)  # the container's `tester` user (not the host uid) writes artefacts here
    name = f"rig-omp-e2e-{os.getpid()}-{variant}-{scenario_key}-{index}"
    cmd = [
        "docker", "run", "--rm", "--name", name,
        "--memory", args.memory, "--cpus", str(args.cpus),
        "-v", f"{out}:/out",
        "-e", "ANTHROPIC_OAUTH_TOKEN" if "ANTHROPIC_OAUTH_TOKEN" in cred else "ANTHROPIC_API_KEY",
        "-e", f"E2E_VARIANT={variant}", "-e", f"E2E_SCENARIO={scenario_key}",
        "-e", f"E2E_TASK_MODE={scenario.task_mode}", "-e", f"E2E_MODEL={args.model}",
        "-e", f"E2E_TIMEOUT={args.trial_timeout}", "-e", "E2E_OUT=/out",
        "-e", f"E2E_PROMPT={scenario.prompt}",
        *(["-e", "E2E_HELPER_TASK_BLOCKED=1"] if scenario.helper_task_blocked else []),
        IMAGE,
    ]
    started = time.time()
    invalid: list[str] = []
    for attempt in range(args.max_retries + 1):
        try:
            proc = run(cmd, wrap=wrap, timeout=args.trial_timeout + 240 + (args.lock_wait if wrap else 0), env={**os.environ, **cred}, check=False)
            (out / "docker.stdout").write_text(proc.stdout)
            (out / "docker.stderr").write_text(proc.stderr)
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            (out / "trial.error").write_text("docker run timed out")
        result = analyze.analyze_trial(out, scenario_key)
        if result["outcome"] not in analyze.INVALID_OUTCOMES or attempt == args.max_retries:
            break
        # The trial did not test what it was meant to (harness error, or the test hook was routed
        # around): keep its raw logs under invalid/ and take a fresh sample for this cell.
        invalid.append(result["outcome"])
        parked = out.parent / "invalid" / f"{out.name}-attempt{attempt + 1}"
        parked.parent.mkdir(exist_ok=True)
        shutil.move(str(out), str(parked))
        out.mkdir()
        out.chmod(0o777)
    result.update(variant=variant, scenario=scenario_key, trial=index, seconds=round(time.time() - started, 1),
                  invalid_attempts=invalid)
    (out / "result.json").write_text(json.dumps(result, indent=2))
    print(f"  {variant:5} {scenario_key} #{index:02d} -> {result['outcome']}", flush=True)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-n", "--trials", type=int, default=5, help="trials per (variant, scenario)")
    ap.add_argument("--scenarios", default="A,B,C,D,E")
    ap.add_argument("--variants", default="base,fixed")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="cheap omp model selector for coordinator AND subagent")
    ap.add_argument("--out", type=Path, default=None, help="results dir (default ~/.cache/rig-cli/e2e-runs/<ts>)")
    for variant in ("base", "fixed"):
        ap.add_argument(f"--agent-tools-{variant}", default=None, help="path@ref of agent-tools for this variant")
        ap.add_argument(f"--rig-{variant}", default=None, help="path@ref of rig-cli for this variant")
    ap.add_argument("--omp-bin", default=None)
    ap.add_argument("--jobs", type=int, default=1, help="concurrent containers")
    ap.add_argument("--trial-timeout", type=int, default=600)
    ap.add_argument("--memory", default="2g")
    ap.add_argument("--cpus", default="2")
    ap.add_argument("--wrap", default=os.environ.get("E2E_WRAP", ""), help="command prefix for docker build/run")
    ap.add_argument("--lock-wait", type=int, default=3600,
                    help="seconds a --wrap'd run may queue (flock) before the per-trial deadline starts to count")
    ap.add_argument("--max-retries", type=int, default=2,
                    help="re-sample a cell whose trial was INVALID (harness error / leaked test premise)")
    ap.add_argument("--no-build", action="store_true", help="reuse the existing image")
    args = ap.parse_args(argv)

    default_tools = os.environ.get("AGENT_TOOLS_REPO", str(Path.home() / "xp/agent-tools"))
    args.agent_tools_base = args.agent_tools_base or f"{default_tools}@main"
    args.agent_tools_fixed = args.agent_tools_fixed or f"{default_tools}@HEAD"
    args.rig_base = args.rig_base or f"{RIG_REPO}@HEAD"
    args.rig_fixed = args.rig_fixed or f"{RIG_REPO}@HEAD"
    wrap = shlex.split(args.wrap)
    scenarios = [s for s in args.scenarios.split(",") if s]
    variants = [v for v in args.variants.split(",") if v]
    bad_variants = [v for v in variants if v not in ("base", "fixed")]
    if bad_variants:
        raise SystemExit(f"unknown variants: {bad_variants} (have base, fixed)")
    unknown = [s for s in scenarios if s not in SCENARIOS]
    if unknown:
        raise SystemExit(f"unknown scenarios: {unknown} (have {sorted(SCENARIOS)})")

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_root = args.out or Path.home() / ".cache/rig-cli/e2e-runs" / stamp
    out_root.mkdir(parents=True, exist_ok=True)
    cred = credential_env()

    tmp_parent = Path(os.environ.get("TMPDIR") or Path.home() / ".cache/rig-cli/tmp")
    tmp_parent.mkdir(parents=True, exist_ok=True)
    shas: dict[str, str] = {}
    if not args.no_build:
        with tempfile.TemporaryDirectory(prefix="omp-ticket-e2e-", dir=tmp_parent) as tmp:
            ctx = Path(tmp)
            shas = stage_context(ctx, args, variants)
            print(f"building {IMAGE} ({json.dumps(shas)})", flush=True)
            run(["docker", "build", "-t", IMAGE, str(ctx)], wrap=wrap, timeout=1800)

    jobs = [(v, s, i) for s in scenarios for v in variants for i in range(1, args.trials + 1)]
    print(f"{len(jobs)} trials, model {args.model}, out {out_root}", flush=True)
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = [pool.submit(run_trial, args, wrap, cred, out_root, v, s, i) for v, s, i in jobs]
        results = [f.result() for f in futures]
    meta = {"model": args.model, "trials": args.trials, "shas": shas, "image": IMAGE, "started": stamp}
    (out_root / "results.json").write_text(json.dumps({"meta": meta, "results": results}, indent=2))
    table = analyze.render_tables(results, meta)
    (out_root / "results.md").write_text(table)
    print(table)
    return 0


if __name__ == "__main__":
    sys.exit(main())
