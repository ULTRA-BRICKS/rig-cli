#!/usr/bin/env python3
"""ONE real-agent trial inside a pristine container (rig-cli#756).

Provisions the machine the real way (`rig apply commit` from the chosen variant's rig-cli +
agent-tools), lays down a scratch git repo with staged changes, then runs a real omp coordinator
that is told to dispatch a subagent to commit them — the exact incident shape — and saves every
raw artefact to $E2E_OUT for analyze.py.

Inputs (env, set by run_matrix.py):
  E2E_VARIANT   base | fixed      which /opt/variants/<v> tree to provision from
  E2E_TASK_MODE ok | screenshot-gate | backend-down | missing   (task_stub.py behaviour)
  E2E_HELPER_TASK_BLOCKED=1       `task` is "command not found" for dispatched agents only (C)
  E2E_MODEL     omp model selector (a cheap model)
  E2E_PROMPT    the coordinator prompt (the trial's only user message)
  E2E_TIMEOUT   wall-clock seconds for the omp run (default 600)
  ANTHROPIC_OAUTH_TOKEN / ANTHROPIC_API_KEY   the agent's credential, injected at run time
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path(os.environ.get("HOME", "/home/tester"))
OUT = Path(os.environ.get("E2E_OUT", "/out"))
VARIANT = os.environ["E2E_VARIANT"]
SCENARIO = os.environ.get("E2E_SCENARIO", "?")
MODEL = os.environ.get("E2E_MODEL", "anthropic/claude-haiku-4-5")
TASK_MODE = os.environ.get("E2E_TASK_MODE", "ok")
TIMEOUT = int(os.environ.get("E2E_TIMEOUT", "600"))
VARDIR = Path("/opt/variants") / VARIANT
REPO = HOME / "work" / "wall-plugs"

SCAD_SEED = """// Spiral drywall plug (parametric)
bore_d = 2.5;
module spiral_body() {
    cylinder(h = 50, r1 = 7, r2 = 0.5);
}
spiral_body();
"""
SCAD_EDIT = """// Spiral drywall plug (parametric)
bore_d = 2.5;
module spiral_body() {
    // tip top radius > bore radius so the bore does not shorten the tip
    cylinder(h = 50, r1 = 7, r2 = bore_d / 2 + 0.8);
}
spiral_body();
"""
HELPER_HOOK = {
    "id": "e2e-helper-task-unavailable", "point": "pre-bash",
    "cmd": "/opt/e2e/helper_task_unavailable.py", "priority": 5, "timeout_ms": 10000,
    "on_error": "open", "description": "ticket-gate E2E: task unavailable to helpers",
}


def log(msg: str) -> None:
    line = f"[trial {VARIANT}/{SCENARIO}] {msg}"
    print(line, file=sys.stderr, flush=True)
    with (OUT / "trial.log").open("a") as fh:
        fh.write(line + "\n")


def fail(msg: str, code: int = 3) -> int:
    (OUT / "trial.error").write_text(msg + "\n")
    log(msg)
    return code


def git(*args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=check)
    return (proc.stdout + proc.stderr).strip()


def provision_machine() -> None:
    bindir = HOME / ".local" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    if TASK_MODE != "missing":
        (bindir / "task").unlink(missing_ok=True)
        (bindir / "task").symlink_to("/opt/e2e/task_stub.py")
    os.environ["PATH"] = f"{bindir}:{os.environ['PATH']}"
    skill = HOME / ".agents" / "skills" / "task"
    skill.mkdir(parents=True, exist_ok=True)
    shutil.copy2("/opt/task-skill/SKILL.md", skill / "SKILL.md")


def make_repo() -> str:
    (REPO / "projects" / "wall-plugs").mkdir(parents=True)
    git("init", "-q", "-b", "main")
    git("config", "user.email", "tester@example.com")
    git("config", "user.name", "tester")
    scad = REPO / "projects" / "wall-plugs" / "wall_plug.scad"
    scad.write_text(SCAD_SEED)
    git("add", "-A")
    git("commit", "-q", "-m", "chore: seed wall-plugs")
    head = git("rev-parse", "HEAD")
    scad.write_text(SCAD_EDIT)
    git("add", "projects/wall-plugs/wall_plug.scad")
    (OUT / "initial_head").write_text(head + "\n")
    return head


def write_omp_config() -> None:
    # Written BEFORE `rig apply`, which edits this file in place (approval mode, idle TTL, ...).
    cfg = HOME / ".omp" / "agent" / "config.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        f"modelRoles:\n  default: {MODEL}\ndefaultThinkingLevel: off\n"
        "tools:\n  approvalMode: yolo\ntask:\n  agentIdleTtlMs: 600000\n")


def rig_apply() -> int:
    shutil.copy2("/opt/e2e/rig.yaml", REPO / "rig.yaml")
    env = {**os.environ, "RIG_AGENT_TOOLS_SOURCE": str(VARDIR / "agent-tools"),
           "OMP_HOOK_BRIDGE_PYTHON": "/opt/venv/bin/python3"}
    # `rig apply commit` writes ~/.omp/agent/hooks/*.json, the omp bridge extension symlink and
    # (agents_md) the orchestrator block into $REPO/AGENTS.md — exactly what a user machine gets.
    with (OUT / "rig-apply.log").open("w") as fh:
        proc = subprocess.run(
            [sys.executable, str(VARDIR / "rig-cli" / "bin" / "rig"), "apply", "commit",
             "-C", str(REPO), "--config", str(REPO / "rig.yaml"), "--yes"],
            stdout=fh, stderr=subprocess.STDOUT, env=env)
    if proc.returncode != 0:
        tail = "\n".join((OUT / "rig-apply.log").read_text().splitlines()[-40:])
        print(tail, file=sys.stderr)
        return fail("rig apply failed")
    hooks = HOME / ".omp" / "agent" / "hooks"
    (OUT / "provisioned.txt").write_text(
        "\n".join(sorted(p.name for p in hooks.glob("*")) + sorted(
            p.name for p in (HOME / ".omp" / "agent" / "extensions").glob("*"))) + "\n")
    if not (hooks / "require-ticket-before-commit.pre-bash.json").exists():
        return fail("require-ticket hook not provisioned")
    if os.environ.get("E2E_HELPER_TASK_BLOCKED") == "1":
        # Test-only agent-hook: dispatched agents (trusted args.agent_id) cannot run `task`; the
        # coordinator can. Stands in for "task missing / backend down for this helper".
        (hooks / "e2e-helper-task-unavailable.pre-bash.json").write_text(json.dumps(HELPER_HOOK))
    # rig's own files are not part of the change under test.
    git("reset", "-q", "--", "AGENTS.md", "CLAUDE.md", "rig.yaml", check=False)
    (OUT / "git-status-before.txt").write_text(git("status", "--short") + "\n")
    return 0


def run_agent() -> None:
    log(f"omp run: model={MODEL} timeout={TIMEOUT}s")
    try:
        with (OUT / "omp.stdout").open("w") as so, (OUT / "omp.stderr").open("w") as se:
            proc = subprocess.run(
                ["omp", "--model", MODEL, "--thinking", "off", "-p", os.environ["E2E_PROMPT"]],
                cwd=REPO, stdout=so, stderr=se, timeout=TIMEOUT)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        code = 124
    (OUT / "omp.exit").write_text(f"{code}\n")


def harvest() -> None:
    (OUT / "git-log.txt").write_text(git("log", "--format=%H%n%B%n----", check=False) + "\n")
    (OUT / "final_head").write_text(git("rev-parse", "HEAD", check=False) + "\n")
    (OUT / "git-status-after.txt").write_text(git("status", "--short", check=False) + "\n")
    for src, dst in ((HOME / ".omp" / "agent" / "sessions", OUT / "sessions"),
                     (HOME / ".omp" / "logs", OUT / "omp-logs")):
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
    if (REPO / "AGENTS.md").is_file():
        shutil.copy2(REPO / "AGENTS.md", OUT / "AGENTS.md")
    log(f"done (omp exit {(OUT / 'omp.exit').read_text().strip()})")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    os.environ["E2E_LOG_DIR"] = str(OUT / "logs")
    (OUT / "logs").mkdir(exist_ok=True)
    provision_machine()
    make_repo()
    write_omp_config()
    code = rig_apply()
    if code:
        return code
    run_agent()
    harvest()
    return 0


if __name__ == "__main__":
    sys.exit(main())
