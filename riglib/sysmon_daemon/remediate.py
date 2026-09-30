"""Auto-remediation for the sysmon daemon (rig-cli#622): launch one agent to fix the root cause.

The heal ladder only relieves pressure; the thing that spawned 13 model loads or orphaned a
tree of bash children is still there and will do it again. When ``sysmon.auto_remediate`` is
on (default OFF: it starts an unattended agent with auto-approved tools), a critical or
post-crash incident gets one detached agent with a brief built from the incident evidence and
one mission: find the root cause, fix it in the owning repo, resume the interrupted sessions,
report, and leave a handoff file next to ``incident.json``.

The agent starts through the same rig-detached-<kind> launchers every other detached subagent
uses, so the hook bridge classifies it as a dispatched subagent (``RIG_AGENT_ID``). claude-code
has no such launcher; its command is built inline to the same contract (see
:func:`launch_command`). A cooldown in the persisted state keeps a flapping machine from
starting an agent per incident. Nothing here raises: every failure is an ``error`` result the
daemon records in the incident.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import config
from .model import AGENT_KINDS, SEVERITY_CRITICAL

BRIEF_FILE = "remediation-brief.md"
HANDOFF_FILE = "remediation-handoff.md"
#: Harnesses with a provisioned ``<skills_dir>/rig-detached-<kind>/rig-detached-<kind>`` launcher.
LAUNCHER_KINDS = ("omp", "codex", "opencode")
#: The launchers return as soon as the child is detached; anything longer is a hang.
LAUNCH_TIMEOUT_S = 30.0
#: Offenders listed per finding in the brief; incident.json has the rest.
MAX_OFFENDERS = 20
FORBIDDEN_TOOLS = "lldb, sample, footprint, dtrace, spindump, htop or lsof"

Runner = Callable[..., "subprocess.CompletedProcess[str]"]

# claude-code's detached launch, mirroring rig-detached-omp: the brief is read before the cd,
# the log is private and appended, stdin is /dev/null, and the child gets its own session
# (os.setsid via python3: macOS has no setsid(1)) so nothing that tree- or group-kills the
# caller takes the agent with it. It fails fast, before detaching, when claude is missing.
# Positional args: $1 log dir, $2 workdir, $3 brief file, $4 agent name, $5 python3.
_CLAUDE_SH = r"""set -e
command -v claude >/dev/null 2>&1 || { echo "claude not found on PATH" >&2; exit 127; }
umask 077
mkdir -p "$1"
log="$1/$4.log"
prompt=$(cat -- "$3")
cd -- "$2"
echo "--- $(date -u '+%Y-%m-%dT%H:%M:%SZ') launching detached agent '$4' in '$2'" >>"$log"
nohup "$5" -c 'import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' \
    claude -p --permission-mode bypassPermissions "$prompt" </dev/null >>"$log" 2>&1 &
echo "detached agent '$4' launched (pid $!, log $log)"
"""


def agent_name(incident_id: str) -> str:
    """``sysmon-remediate-<id>`` restricted to what the launchers accept as ``RIG_AGENT_ID``."""
    return "sysmon-remediate-" + (re.sub(r"[^A-Za-z0-9._-]+", "-", incident_id).strip("-") or "incident")


def log_dir() -> Path:
    """Where detached agents log; the launchers honor the same variable."""
    return Path(os.environ.get("RIG_AGENT_LOG_DIR") or "/tmp/agent-logs")


def launch_command(agent: str, name: str, brief: Path, workdir: Path, cfg: Dict[str, Any]) -> Tuple[List[str], Dict[str, str]]:
    """``(argv, extra_env)`` that starts the remediation agent detached and returns at once.

    omp/codex/opencode: the provisioned ``rig-detached-<kind>`` launcher, which sets the
    identity markers itself. claude-code: ``/bin/sh -c`` running :data:`_CLAUDE_SH` — a shell
    line rather than ``Popen(start_new_session=True)`` so the launch goes through the same
    injectable runner as every other command (tests and dry runs see one argv) and behaves like
    the launchers (nohup, own session, log append, immediate return with a launch line).

    Raises ``ValueError`` for an unknown agent kind or a launcher that is not installed.
    """
    if agent in LAUNCHER_KINDS:
        skills_dir = config.expand(str(cfg["remediation"]["skills_dir"]))
        launcher = skills_dir / f"rig-detached-{agent}" / f"rig-detached-{agent}"
        if not (launcher.is_file() and os.access(launcher, os.X_OK)):
            raise ValueError(f"launcher not installed or not executable: {launcher} (rig apply provisions it)")
        return [str(launcher), name, str(brief), str(workdir)], {}
    if agent == "claude-code":
        argv = ["/bin/sh", "-c", _CLAUDE_SH, "sysmon-remediate", str(log_dir()), str(workdir), str(brief), name,
                sys.executable]
        return argv, {"RIG_AGENT_ID": name, "RIG_DETACHED_AGENT": "1"}
    raise ValueError(f"unknown remediation agent {agent!r} (one of {', '.join(AGENT_KINDS)})")


def maybe_spawn(
    incident: Dict[str, Any],
    incident_dir: Path,
    cfg: Dict[str, Any],
    state: Dict[str, Any],
    now: float,
    run: Runner = subprocess.run,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """``(result, new_state)``; ``result["status"]`` is ``disabled`` | ``skipped`` | ``dry_run``
    | ``error`` | ``spawned``. Only a spawn starts the cooldown (``state["last_spawn_at"]``)."""
    if cfg.get("auto_remediate") is not True:
        return {"status": "disabled"}, state
    r = cfg["remediation"]
    if incident.get("severity") != SEVERITY_CRITICAL and incident.get("kind") != "post-crash":
        return {"status": "skipped", "reason": f"{incident.get('severity')} {incident.get('kind')} incident"}, state
    last = state.get("last_spawn_at")
    cooldown_s = float(r["cooldown_minutes"]) * 60
    if last is not None and now - float(last) < cooldown_s:
        return {"status": "skipped", "reason": f"cooldown: an agent was launched {int(now - float(last))}s ago"}, state

    agent = str(r["agent"])
    name = agent_name(str(incident["id"]))
    brief = incident_dir / BRIEF_FILE
    result: Dict[str, Any] = {"agent": agent, "name": name, "brief": str(brief),
                              "handoff": str(incident_dir / HANDOFF_FILE), "log": str(log_dir() / f"{name}.log"),
                              "at": now}
    try:
        argv, extra_env = launch_command(agent, name, brief, config.expand(str(r["workdir"])), cfg)
        incident_dir.mkdir(parents=True, exist_ok=True)
        brief.write_text(build_brief(incident, incident_dir, cfg), encoding="utf-8")
    except (ValueError, OSError) as e:
        return dict(result, status="error", error=str(e)), state
    result["command"] = argv
    if r["dry_run"]:
        return dict(result, status="dry_run"), state
    try:
        proc = run(argv, capture_output=True, text=True, timeout=LAUNCH_TIMEOUT_S, stdin=subprocess.DEVNULL,
                   env={**os.environ, **extra_env})
    except (OSError, subprocess.SubprocessError) as e:
        return dict(result, status="error", error=str(e)), state
    if proc.returncode != 0:
        why = (proc.stderr or "").strip() or f"exit {proc.returncode}"
        return dict(result, status="error", error=why), state
    return (dict(result, status="spawned", detail=(proc.stdout or "").strip()),
            dict(state, last_spawn_at=now, last_incident=incident["id"]))


# ── the brief ─────────────────────────────────────────────────────────────────────────────
def _ts(epoch: Any) -> str:
    if not isinstance(epoch, (int, float)):
        return "unknown"
    return time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(epoch))


def _owner(attribution: Dict[str, Any]) -> str:
    host = attribution.get("host")
    parts = []
    if isinstance(host, dict):
        where = [f"pane {host.get('pane') or attribution.get('pane') or 'none'}"]
        if host.get("session_file"):
            where.append(f"session `{host['session_file']}`")
        if host.get("cwd"):
            where.append(f"cwd `{host['cwd']}`")
        parts.append(f"owned by {host.get('kind')} pid {host.get('pid')} ({', '.join(where)})")
    elif attribution.get("pane"):
        parts.append(f"pane {attribution['pane']}")
    if attribution.get("orphan"):
        parts.append(f"ORPHAN (its agent host {attribution.get('dead_owner') or '?'} died)")
    if attribution.get("protected_reason"):
        parts.append(f"protected: {attribution['protected_reason']}")
    return "; ".join(parts) or "no agent host"


def _findings_md(findings: List[Dict[str, Any]]) -> List[str]:
    out = ["### Findings"]
    for f in findings:
        out.append(f"- **{f.get('severity')} {f.get('kind')}** (`{f.get('key')}`): {f.get('summary')}")
        metrics = ", ".join(f"{k}={v}" for k, v in sorted((f.get("metrics") or {}).items()) if v is not None)
        if metrics:
            out.append(f"  - metrics: {metrics}")
        offenders = f.get("offenders") or []
        for o in offenders[:MAX_OFFENDERS]:
            out.append(f"  - pid {o.get('pid')} `{o.get('comm')}` {int(o.get('rss_kb') or 0) / 1048576:.1f} GB RSS, "
                       f"started {_ts(o.get('start_time'))}: `{str(o.get('args') or '')[:200]}` — "
                       f"{_owner(o.get('attribution') or {})}")
        if len(offenders) > MAX_OFFENDERS:
            out.append(f"  - … and {len(offenders) - MAX_OFFENDERS} more (see incident.json)")
    if len(out) == 1:
        out.append("- none recorded")
    return out


def _sample_md(sample: Optional[Dict[str, Any]]) -> List[str]:
    out = ["### Machine at incident time"]
    for key, value in sorted((sample or {}).items()):
        if isinstance(value, float):
            out.append(f"- {key}: {round(value, 2)}")
        elif isinstance(value, (int, str)) and not isinstance(value, bool):
            out.append(f"- {key}: {value}")
    if len(out) == 1:
        out.append("- no sample recorded")
    return out


def _sessions_md(sessions: List[Dict[str, Any]]) -> List[str]:
    out = ["### Agent sessions live at incident time"]
    for s in sessions:
        title = f" \"{s['title']}\"" if s.get("title") else ""
        out.append(f"- {s.get('kind')} pid {s.get('pid')}{title} in `{s.get('cwd')}`, pane {s.get('pane') or 'none'}: "
                   f"transcript `{s.get('session_file')}`")
    if len(out) == 1:
        out.append("- none recorded")
    return out


def _crash_md(crash: Optional[Dict[str, Any]]) -> List[str]:
    if not crash:
        return []
    out = ["### Crash",
           f"- the machine went down without a clean shutdown: {crash.get('reason')}; "
           f"last heartbeat {_ts(crash.get('last_heartbeat'))}"]
    if crash.get("panic_file"):
        out.append(f"- panic report: `{crash['panic_file']}`")
    if crash.get("panic_excerpt"):
        out += ["- excerpt:", "```", str(crash["panic_excerpt"])[:2000], "```"]
    return out


def build_brief(incident: Dict[str, Any], incident_dir: Path, cfg: Dict[str, Any]) -> str:
    """The markdown mission the remediation agent starts with (also its only context)."""
    incident_id = str(incident["id"])
    simulated = incident.get("kind") == "simulated"
    dry = " --dry-run" if simulated else ""  # a drill never messages Telegram
    handoff = incident_dir / HANDOFF_FILE
    # <config_dir>/state/incidents/<id> → <config_dir>
    config_dir = incident_dir.parent.parent.parent
    gpu = cfg["gpu"]
    lines = [
        f"# sysmon remediation: incident {incident_id}",
        "",
        f"rig sysmon (rig-cli#622) launched you automatically: this machine hit a **{incident.get('severity')}** "
        f"resource incident (kind `{incident.get('kind')}`). On 2026-09-29 the same class of overload "
        "(runaway agent processes, 13 concurrent model loads wiring GPU memory, orphaned subagent shells) "
        "kernel-panicked it. You run unattended: nobody approves your steps, so the hard rules below are absolute.",
    ]
    if simulated:
        lines += ["", "This is a SIMULATED incident (a drill). Its cause is planted on disk (see the drill evidence): "
                      "find it and fix it for real in place (commit in its own git repo), resume the listed sessions "
                      "for real, but do NOT file tickets or PRs in real trackers, and run `tg report` with `--dry-run`."]
    lines += [
        "",
        "## Evidence",
        f"- Incident record: `{incident_dir / 'incident.json'}` (findings, offenders, sample, sessions, heal steps)",
        f"- Opened {_ts(incident.get('opened_at'))}, last updated {_ts(incident.get('updated_at'))}",
        f"- Now: `rig sysmon status`; history: `rig sysmon incidents`, `rig sysmon incidents --show {incident_id}`",
        "- Processes: `ps -A -ww -o pid,ppid,etime,rss,%cpu,stat,tty,comm,args`",
    ]
    lines += [f"- Drill evidence: {e}" for e in incident.get("evidence") or []]
    lines += [""]
    lines += _findings_md([f for f in incident.get("findings") or [] if isinstance(f, dict)])
    lines += [""] + _sample_md(incident.get("sample"))
    lines += [""] + _sessions_md([s for s in incident.get("sessions") or [] if isinstance(s, dict)])
    crash = _crash_md(incident.get("crash"))
    if crash:
        lines += [""] + crash
    lines += [
        "",
        "## Mission",
        "1. Find the ROOT cause from the evidence above: the incident record, `rig sysmon status`, "
        "`rig sysmon incidents`, `ps`, and the named transcripts (what made that agent start this work, "
        "and why nothing serialized it). Killing processes is not a fix.",
        "2. Fix it where it lives: file a ticket with `task new` in the owning repo and open a PR, or correct "
        "the config that allowed it. A still-running orphan may be stopped with plain `kill <pid>` "
        "(SIGTERM first); never attach a debugger to it.",
        f"3. Resume the interrupted agent sessions: `rig sysmon restore --incident {incident_id}` "
        "(it skips sessions that are live again). If `rig` itself is unavailable, the installed daemon "
        f"package does the same: `cd {config_dir} && python3 -m sysmon_daemon restore --config "
        f"{config_dir / 'sysmon.json'} --incident {incident_id}`.",
        "4. Report: `tg report --title \"...\" --ticket <ticket-url> --impact \"...\" --verify-cmd \"...\" "
        f"--verify-result \"...\"{dry}`.",
        f"5. Before you exit, write the handoff file `{handoff}`: root cause, the fix (ticket/PR URLs), what you "
        "stopped, the restore result, and whether the report was sent.",
        "",
        "## Hard rules",
        f"- Never run {FORBIDDEN_TOOLS}: on 2026-09-29 they hung behind a SecurityAgent task_for_pid dialog "
        "on the overloaded machine.",
        "- Run heavy commands (builds, test suites, model loads) only under the machine-wide lock: loop "
        "`mkdir /tmp/heavy-run.lock` until it succeeds (sleep 5 s between tries), write your pid to "
        "`/tmp/heavy-run.lock/pid`, run, and `rm -rf /tmp/heavy-run.lock` in a trap on EXIT/INT/TERM; the lock "
        "is stale only when the pid in that file is dead.",
        f"- Load at most one model at a time, through the GPU slot pool ({gpu['slots']} slots, `{gpu['slot_dir']}`).",
        "- Never kill a user-interactive process: foreground terminal jobs, terminals, tmux, interactive agent sessions.",
        "- No VMs or containers.",
        "",
    ]
    return "\n".join(lines)
