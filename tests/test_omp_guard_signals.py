"""Signal-safety rules of the generated omp guard (rig-cli#701), EXECUTED under bun.

2026-10-15 an omp subagent ran ``P=$(pgrep -f "^gh ship 247" | head -1); kill -TERM $(pstree
-p $P | grep -o '([0-9]*)' | tr -d '()')``. ``$P`` was empty, ``pstree -p`` with no pid printed
the whole system tree, and SIGTERM reached ``systemd --user`` — every omp session and the
desktop died. The guard only asked before ``pkill``/``killall``, and that ask is what pushed the
agent to the unguarded idiom.

These tests run the GENERATED TS (the enforcement layer itself, not a Python re-implementation)
against one table of commands. Literal-PID cases resolve against a FAKED ``/proc`` tree (comm,
stat, cgroup) passed through the guard's exported ``evaluateBash`` host seam, so the target-class
checks (systemd --user, session leader, gnome-shell, tmux server, another omp scope) are proven
without touching the real process table.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import NamedTuple

import pytest

from riglib.omp_guard import render_guard_ts
from riglib.permissions import OMP_GUARD_ASK_RULES, OMP_GUARD_DENY_RULES, SIGNAL_MATCHERS
from tests.test_omp_guard import _bun_bin

INCIDENT = (
    "P=$(pgrep -f \"^gh ship 247\" | head -1); "
    "kill -TERM $(pstree -p $P | grep -o '([0-9]*)' | tr -d '()')"
)

SIGNAL_RULE_IDS = ("kill-broadcast", "kill-pstree-fed", "signal-session-infra", "kill-computed-pid")

# The faked process table. SELF (3000) is the omp process running the guard; it lives in its own
# omp scope, under a tmux pane shell, under the tmux server — the real shape on this machine.
SELF_PID = 3000
USER = "/user.slice/user-1000.slice"
MGR = f"{USER}/user@1000.service"


class FakeProc(NamedTuple):
    comm: str
    ppid: int
    sid: int  # session id
    cgroup: str  # cgroup v2 path


FAKE_PROCS: dict[int, FakeProc] = {
    1: FakeProc("systemd", 0, 1, "/init.scope"),
    2176: FakeProc("systemd", 1, 2176, f"{MGR}/init.scope"),
    2300: FakeProc("gdm-wayland-ses", 1, 2300, f"{USER}/session-2.scope"),
    2400: FakeProc("gnome-shell", 2176, 2400, f"{MGR}/session.slice/org.gnome.Shell@wayland.service"),
    2410: FakeProc("Xwayland", 2400, 2400, f"{MGR}/session.slice/org.gnome.Shell@wayland.service"),
    2420: FakeProc("gnome-session-b", 2176, 2420, f"{MGR}/session.slice/gnome-session-manager@ubuntu.service"),
    2500: FakeProc("tmux: server", 1, 2500, f"{USER}/session-2.scope"),
    2600: FakeProc("zsh", 2500, 2600, f"{USER}/session-2.scope"),
    2700: FakeProc("zsh", 2500, 2700, f"{USER}/session-2.scope"),
    SELF_PID: FakeProc("omp", 2600, 2600, f"{MGR}/omp.slice/omp-3000-11.scope"),
    3100: FakeProc("pytest", SELF_PID, 2600, f"{MGR}/omp.slice/omp-3000-11.scope"),
    3200: FakeProc("sleep", 3100, 3200, f"{MGR}/omp.slice/omp-3000-11.scope"),  # setsid'ed own child
    4000: FakeProc("omp", 2700, 2700, f"{MGR}/omp.slice/omp-4000-22.scope"),
    4100: FakeProc("python3", 4000, 2700, f"{MGR}/omp.slice/omp-4000-22.scope"),
    5000: FakeProc("omp", 2700, 2700, f"{USER}/session-2.scope"),  # an omp started outside any scope
    6000: FakeProc("sleep", 2176, 6000, f"{MGR}/app.slice/run-p6000-i1.scope"),  # a run-*.scope job
}

ALLOW = "allow"


def _nest_sh_c(command: str, depth: int) -> str:
    """``command`` wrapped in ``depth`` layers of ``sh -c '…'`` (shell-quoted per layer)."""
    for _ in range(depth):
        command = "sh -c " + shlex.quote(command)
    return command


# (name, command, expected verdict) — expected is "allow" or "<deny|ask>:<rule id>".
CASES: list[tuple[str, str, str]] = [
    # ── the incident ────────────────────────────────────────────────────────────────────
    ("incident 2026-10-15 verbatim", INCIDENT, "deny:kill-pstree-fed"),
    ("pstree substitution backticks", "kill `pstree -p $P | grep -o '[0-9]*'`", "deny:kill-pstree-fed"),
    ("pstree piped into xargs kill", "pstree -p $P | grep -o '([0-9]*)' | tr -d '()' | xargs kill", "deny:kill-pstree-fed"),
    ("pstree piped into xargs -r kill -9", "pstree -p 123 | grep -oP '\\d+' | xargs -r kill -9", "deny:kill-pstree-fed"),
    ("pstree-tainted variable", "PIDS=$(pstree -p $P | grep -o '[0-9]*'); [ -n \"$PIDS\" ] && kill $PIDS", "deny:kill-pstree-fed"),
    # review: an inner `;` splits the substitution into stages — the taint must follow it
    ("pstree after ; inside assignment substitution",
     "P=$(true; pstree -p \"$P\" | grep -o \"[0-9]*\" | tr \"\\n\" \" \" ); [ -n \"$P\" ] && kill $P", "deny:kill-pstree-fed"),
    ("pstree after ; inside kill substitution", "kill -TERM $(true; pstree -p $P | grep -o '[0-9]*')", "deny:kill-pstree-fed"),
    ("for loop over pstree", "for p in $(pstree -p $P | grep -o '[0-9]*'); do kill $p; done", "deny:kill-pstree-fed"),
    ("decoy substitution before pstree", "kill $(true) $(pstree -p $P | grep -o '[0-9]*')", "deny:kill-pstree-fed"),
    ("export-assigned pstree", "export P=$(pstree -p $X | grep -o '[0-9]*'); kill $P", "deny:kill-pstree-fed"),
    ("quoted pstree substitution", "kill \"$(pstree -p $P)\"", "deny:kill-pstree-fed"),
    # ── broadcast / process-group kills ─────────────────────────────────────────────────
    ("kill -1 bare", "kill -1", "deny:kill-broadcast"),
    ("kill -TERM -1", "kill -TERM -1", "deny:kill-broadcast"),
    ("kill -9 -1", "kill -9 -1", "deny:kill-broadcast"),
    ("kill -- -1", "kill -- -1", "deny:kill-broadcast"),
    ("kill -s KILL -1", "kill -s KILL -1", "deny:kill-broadcast"),
    ("kill negative pgid", "kill -TERM -4242", "deny:kill-broadcast"),
    ("kill -- -PGID", "kill -- -4242", "deny:kill-broadcast"),
    ("kill -- -$PGID", "kill -- -$PGID", "deny:kill-broadcast"),
    ("kill 0 own group", "kill 0", "deny:kill-broadcast"),
    ("kill -9 0", "kill -9 0", "deny:kill-broadcast"),
    ("/bin/kill -1 path form", "/bin/kill -9 -1", "deny:kill-broadcast"),
    ("sudo kill -1", "sudo kill -9 -1", "deny:kill-broadcast"),
    ("systemd-run wrapped kill -1", "systemd-run --user --pipe --wait kill -9 -1", "deny:kill-broadcast"),
    ("flock wrapped kill -1", "flock /tmp/heavy.lock kill -TERM -1", "deny:kill-broadcast"),
    ("doas -u root kill -1", "doas -u root kill -9 -1", "deny:kill-broadcast"),
    # review: path-qualified wrappers and command-string wrappers still reach the kill
    ("/usr/bin/env wrapped kill -1", "/usr/bin/env kill -1", "deny:kill-broadcast"),
    ("/usr/bin/timeout wrapped kill -1", "/usr/bin/timeout 1 kill -1", "deny:kill-broadcast"),
    ("env -S command string", "env -S \"kill -TERM -1\"", "deny:kill-broadcast"),
    ("env -S joined command string", "env '-Skill -TERM -1'", "deny:kill-broadcast"),
    ("sh -c command string", "sh -c 'kill -9 -1'", "deny:kill-broadcast"),
    ("sudo bash -lc command string", "sudo bash -lc \"kill -TERM -1\"", "deny:kill-broadcast"),
    ("timeout sh -c command string", "timeout 1 sh -c \"kill -TERM -1\"", "deny:kill-broadcast"),
    ("bash -o pipefail -c incident", "bash -o pipefail -c '" + INCIDENT.replace("'", "\"") + "'", "deny:kill-pstree-fed"),
    ("eval command string", "eval 'kill -9 -1'", "deny:kill-broadcast"),
    ("sh -c computed kill asks", "sh -c 'kill $(pgrep -f worker)'", "ask:kill-computed-pid"),
    # review round 3: option terminators, long env options, deep nesting, more runners
    ("sh -c -- command string", "sh -c -- 'kill -9 -1'", "deny:kill-broadcast"),
    ("bash -c -e command string (options after -c)", "bash -c -e 'kill -9 -1'", "deny:kill-broadcast"),
    ("env --ignore-environment sh -c", "env --ignore-environment sh -c 'kill -9 -1'", "deny:kill-broadcast"),
    ("five nested sh -c", _nest_sh_c("kill -9 -1", 5), "deny:kill-broadcast"),
    ("16 nested eval strings", "eval " * 16 + "kill -9 -1", "deny:kill-broadcast"),
    ("17 nested eval strings fail closed", "eval " * 17 + "kill -9 -1", "deny:signal-session-infra"),
    ("17 nested eval strings fail closed on a harmless command", "eval " * 17 + "true", "deny:signal-session-infra"),
    ("setsid kill -1", "setsid kill -9 -1", "deny:kill-broadcast"),
    ("setsid -f kill -1", "setsid -f kill -9 -1", "deny:kill-broadcast"),
    ("stdbuf kill -1", "stdbuf -oL kill -1", "deny:kill-broadcast"),
    ("taskset kill -1", "taskset -c 0 kill -9 -1", "deny:kill-broadcast"),
    ("chrt kill -1", "chrt -f 10 kill -9 -1", "deny:kill-broadcast"),
    ("ionice kill -1", "ionice -c 3 kill -9 -1", "deny:kill-broadcast"),
    # review round 5: attached wrapper option values, deep prefix chains
    ("nice -n10 attached value", "nice -n10 kill -9 -1", "deny:kill-broadcast"),
    ("timeout -sTERM attached value", "timeout -sTERM 1 kill -9 -1", "deny:kill-broadcast"),
    ("sudo -uroot attached value", "sudo -uroot kill -9 -1", "deny:kill-broadcast"),
    ("doas -uroot attached value", "doas -uroot kill -9 -1", "deny:kill-broadcast"),
    ("16 nested sudo prefixes", "sudo " * 16 + "kill -9 -1", "deny:kill-broadcast"),
    ("17 nested sudo prefixes fail closed", "sudo " * 17 + "kill -9 -1", "deny:signal-session-infra"),
    # review round 4: literal-bound variables, option terminators, scripts fed to a shell
    ("guarded variable bound to -1", "P=-1; [ -n \"$P\" ] && kill -- \"$P\"", "deny:kill-broadcast"),
    ("guarded variable bound to 0", "P=0; [ -n \"$P\" ] && kill \"$P\"", "deny:kill-broadcast"),
    ("env -- kill -- -1", "env -- kill -- -1", "deny:kill-broadcast"),
    ("echo piped into sh", "echo 'kill -9 -1' | sh", "deny:kill-broadcast"),
    ("printf piped into bash -s", "printf 'kill -9 -1\\n' | bash -s", "deny:kill-broadcast"),
    ("here-string into bash", "bash <<< 'kill -9 -1'", "deny:kill-broadcast"),
    ("heredoc body line", "bash <<EOF\nkill -9 -1\nEOF", "deny:kill-broadcast"),
    ("find -exec kill", "find /tmp -name '*.pid' -exec kill -9 -1 \\;", "deny:kill-broadcast"),
    ("xargs sh -c", "pgrep -f worker | xargs sh -c 'kill -9 -1'", "deny:kill-broadcast"),
    ("watch command string", "watch -n 5 'kill -9 -1'", "deny:kill-broadcast"),
    ("pkill -v inverse", "pkill -v pytest", "deny:kill-broadcast"),
    ("pkill -u user no pattern", "pkill -u ultra", "deny:kill-broadcast"),
    ("pkill -s 0 no pattern", "pkill -s 0", "deny:kill-broadcast"),
    ("killall -u user no names", "killall -u ultra", "deny:kill-broadcast"),
    # ── session infrastructure by name / unit ──────────────────────────────────────────
    ("pkill systemd", "pkill systemd", "deny:signal-session-infra"),
    ("pkill -f systemd --user", "pkill -f 'systemd --user'", "deny:signal-session-infra"),
    ("killall gnome-shell", "killall gnome-shell", "deny:signal-session-infra"),
    ("pkill gdm", "pkill gdm", "deny:signal-session-infra"),
    ("killall gdm", "killall gdm", "deny:signal-session-infra"),
    ("pkill -f gnome-session-manager", "pkill -f gnome-session-manager", "deny:signal-session-infra"),
    ("pkill gnome-session-binary", "pkill -f gnome-session-binary", "deny:signal-session-infra"),
    ("pkill Xwayland", "pkill -9 Xwayland", "deny:signal-session-infra"),
    ("killall tmux", "killall tmux", "deny:signal-session-infra"),
    ("pkill -x omp", "pkill -x omp", "deny:signal-session-infra"),
    ("pkill -f ^omp", "pkill -f '^omp'", "deny:signal-session-infra"),
    ("kill $(pgrep omp)", "kill $(pgrep -x omp)", "deny:signal-session-infra"),
    ("kill $(pidof gnome-shell)", "kill -TERM $(pidof gnome-shell)", "deny:signal-session-infra"),
    ("decoy substitution before pgrep omp", "kill $(true) $(pgrep -x omp)", "deny:signal-session-infra"),
    ("literal-bound variable to systemd --user", "P=2176; kill $P", "deny:signal-session-infra"),
    ("declare-assigned pgrep omp", "declare P=$(pgrep -x omp); kill $P", "deny:signal-session-infra"),
    ("quoted substitution pgrep omp", "kill \"$(pgrep -x omp)\"", "deny:signal-session-infra"),
    ("echo omp | xargs pkill", "echo omp | xargs pkill -x", "deny:signal-session-infra"),
    ("xargs systemctl stop user@", "echo user@1000.service | xargs systemctl --user stop", "deny:signal-session-infra"),
    ("kill $$ own omp", "kill $$", "deny:signal-session-infra"),
    ("kill $PPID", "kill -TERM $PPID", "deny:signal-session-infra"),
    ("tmux kill-server default socket", "tmux kill-server", "deny:signal-session-infra"),
    ("tmux -L default kill-server (the user's server)", "tmux -L default kill-server", "deny:signal-session-infra"),
    ("tmux -S .../default kill-server", "tmux -S /tmp/tmux-1000/default kill-server", "deny:signal-session-infra"),
    ("tmux kill-session -a (all other sessions)", "tmux kill-session -a -t scratch", "deny:signal-session-infra"),
    ("systemctl --user stop user@", "systemctl --user stop user@1000.service", "deny:signal-session-infra"),
    ("systemctl stop user@ system", "systemctl stop user@1000.service", "deny:signal-session-infra"),
    ("systemctl --user exit", "systemctl --user exit", "deny:signal-session-infra"),
    ("systemctl --user start exit.target", "systemctl --user start exit.target", "deny:signal-session-infra"),
    ("systemctl --user isolate", "systemctl --user isolate default.target", "deny:signal-session-infra"),
    ("systemctl --user kill init.scope", "systemctl --user kill --signal=TERM init.scope", "deny:signal-session-infra"),
    ("systemctl --user stop omp.slice", "systemctl --user stop omp.slice", "deny:signal-session-infra"),
    ("systemctl --user stop other omp scope", "systemctl --user stop omp-4000-22.scope", "deny:signal-session-infra"),
    ("systemctl --user stop omp-* glob", "systemctl --user stop 'omp-*'", "deny:signal-session-infra"),
    ("systemctl --user kill gnome shell unit", "systemctl --user kill org.gnome.Shell@wayland.service", "deny:signal-session-infra"),
    ("systemctl stop session scope", "systemctl stop session-2.scope", "deny:signal-session-infra"),
    ("systemctl --user stop bare glob", "systemctl --user stop '*'", "deny:signal-session-infra"),
    ("loginctl terminate-session", "loginctl terminate-session 2", "deny:signal-session-infra"),
    ("loginctl kill-user", "loginctl kill-user ultra", "deny:signal-session-infra"),
    # ── literal PIDs resolved against the faked /proc ──────────────────────────────────
    ("literal pid 1", "kill 1", "deny:signal-session-infra"),
    ("literal systemd --user", "kill -TERM 2176", "deny:signal-session-infra"),
    ("literal session leader", "kill 2300", "deny:signal-session-infra"),
    ("literal gnome-shell", "kill -9 2400", "deny:signal-session-infra"),
    ("literal Xwayland", "kill 2410", "deny:signal-session-infra"),
    ("literal gnome-session", "kill 2420", "deny:signal-session-infra"),
    ("literal tmux server", "kill 2500", "deny:signal-session-infra"),
    ("literal own pane shell (ancestor)", "kill -HUP 2600", "deny:signal-session-infra"),
    ("literal own omp (self)", "kill 3000", "deny:signal-session-infra"),
    ("literal other omp", "kill 4000", "deny:signal-session-infra"),
    ("literal process in other omp scope", "kill -TERM 4100", "deny:signal-session-infra"),
    ("literal unscoped omp", "kill 5000", "deny:signal-session-infra"),
    ("one bad literal among good", "kill 3100 2176", "deny:signal-session-infra"),
    # ── runtime-computed targets: ask (headless = blocked) ─────────────────────────────
    ("kill $(pgrep -f worker)", "kill $(pgrep -f worker)", "ask:kill-computed-pid"),
    ("kill backticks pgrep", "kill `pgrep -f worker`", "ask:kill-computed-pid"),
    ("kill $(cat pidfile)", "kill -TERM $(cat /tmp/worker.pid)", "ask:kill-computed-pid"),
    ("kill quoted substitution", "kill \"$(pgrep -f worker)\"", "ask:kill-computed-pid"),
    ("kill $VAR unchecked", "P=$(pgrep -f worker); kill $P", "ask:kill-computed-pid"),
    ("kill ${VAR} unchecked", "kill -9 ${PID}", "ask:kill-computed-pid"),
    ("kill \"$VAR\" quoted unchecked", "kill \"$P\"", "ask:kill-computed-pid"),
    ("kill guarded with || (inverted)", "[ -n \"$P\" ] || kill $P", "ask:kill-computed-pid"),
    ("kill guard on another variable", "[ -n \"$Q\" ] && kill $P", "ask:kill-computed-pid"),
    ("kill in else branch", "if [ -n \"$P\" ]; then echo ok; else kill $P; fi", "ask:kill-computed-pid"),
    ("kill guard broken by ||", "[ -n \"$P\" ] && false || kill $P", "ask:kill-computed-pid"),
    ("for loop over pgrep", "for p in $(pgrep -f worker); do kill $p; done", "ask:kill-computed-pid"),
    ("xargs kill from pgrep", "pgrep -f worker | xargs kill", "ask:kill-computed-pid"),
    ("xargs kill from ps", "ps aux | grep worker | awk '{print $2}' | xargs kill -9", "ask:kill-computed-pid"),
    ("xargs kill from file", "cat /tmp/pids | xargs -r kill", "ask:kill-computed-pid"),
    ("kill glob operand", "kill /tmp/*.pid", "ask:kill-computed-pid"),
    ("then-wrapped unchecked kill", "if true; then kill $P; fi", "ask:kill-computed-pid"),
    ("subshell unchecked kill", "(kill $P)", "ask:kill-computed-pid"),
    ("command prefix unchecked kill", "command kill -TERM $P", "ask:kill-computed-pid"),
    ("xargs pkill pattern from stdin", "printf 'worker' | xargs pkill -f", "ask:kill-computed-pid"),
    ("xargs systemctl stop units from stdin", "cat units.txt | xargs systemctl --user stop", "ask:kill-computed-pid"),
    # ── legit forms stay allowed ────────────────────────────────────────────────────────
    ("kill literal pid", "kill 12345", ALLOW),
    ("kill -TERM literal pid", "kill -TERM 12345", ALLOW),
    ("kill own child in own scope", "kill -TERM 3100", ALLOW),
    ("kill own setsid child in own scope", "kill 3200", ALLOW),
    ("kill run-scope job by literal pid", "kill 6000", ALLOW),
    ("kill -HUP literal (signal 1 + pid)", "kill -1 12345", ALLOW),
    ("kill job spec", "kill %1", ALLOW),
    ("kill -9 job spec", "kill -9 %2", ALLOW),
    ("kill last bg job", "sleep 100 & kill $!", ALLOW),
    ("kill -0 probe var", "kill -0 $pid", ALLOW),
    ("kill -0 probe substitution", "kill -0 $(cat /tmp/worker.pid)", ALLOW),
    ("kill -s 0 probe", "kill -s 0 $P", ALLOW),
    ("kill -0 $$ probe", "kill -0 $$", ALLOW),
    ("kill -l list", "kill -l", ALLOW),
    ("kill -l 15", "kill -l 15", ALLOW),
    ("guarded var kill (the documented safe form)", "[ -n \"$P\" ] && ps -p \"$P\" && kill \"$P\"", ALLOW),
    ("guarded [[ -n ]]", "[[ -n $P ]] && kill -TERM $P", ALLOW),
    ("guarded test -n", "test -n \"$P\" && kill $P", ALLOW),
    ("guarded -z ||", "[ -z \"$P\" ] || kill \"$P\"", ALLOW),
    ("guarded if/then", "if [ -n \"$P\" ]; then kill \"$P\"; fi", ALLOW),
    ("guarded ${P:?}", "kill -TERM \"${P:?no pid}\"", ALLOW),
    ("systemctl --user stop run scope glob", "systemctl --user stop run-*.scope", ALLOW),
    ("systemctl --user stop one run scope", "systemctl --user stop run-p6000-i1.scope", ALLOW),
    ("systemctl --user kill own run scope", "systemctl --user kill run-p6000-i1.scope", ALLOW),
    ("systemctl --user status omp.slice (read-only)", "systemctl --user status omp.slice", ALLOW),
    ("systemctl --user restart a service", "systemctl --user restart ai.hyperide.sysmon.service", ALLOW),
    ("tmux kill-server private socket", "tmux -L rigtest kill-server", ALLOW),
    ("tmux kill-session", "tmux kill-session -t scratch", ALLOW),
    ("pgrep omp read-only", "pgrep -a omp", ALLOW),
    ("pstree read-only", "pstree -p 3000", ALLOW),
    ("echo mentions kill -1", "echo 'never run kill -1'", ALLOW),
    ("kill 12345 with stderr redirect", "kill 12345 2>/dev/null", ALLOW),
    ("timeout -s KILL wrapper", "timeout -s KILL 5 sleep 10", ALLOW),
    ("flock wrapped literal kill", "flock -w 5 /tmp/heavy.lock kill 12345", ALLOW),
    ("systemd-run scope wrapped literal kill", "systemd-run --user --scope -p MemoryMax=1G kill 12345", ALLOW),
    ("sh -c literal kill", "sh -c 'kill 12345'", ALLOW),
    ("bash script without -c", "bash ./scripts/stop.sh", ALLOW),
    ("many literal pids", "kill " + " ".join(str(p) for p in range(9001, 9101)), ALLOW),
    ("setsid detached job", "setsid sleep 100", ALLOW),
    ("stdbuf unrelated command", "stdbuf -oL make test", ALLOW),
    ("literal-bound variable kill", "P=12345; kill $P", ALLOW),
    ("echo piped into sh harmless", "echo 'ls -la' | sh", ALLOW),
    ("find -delete", "find . -name '*.pyc' -delete", ALLOW),
    ("find -exec rm", "find /tmp/x -name '*.log' -exec rm {} +", ALLOW),
    ("pkill -P literal parent stays ask (not deny)", "pkill -P 3100", "ask:pkill"),
    ("pkill pattern stays ask", "pkill -f 'pytest tests/test_x.py'", "ask:pkill"),
    ("killall worker stays ask", "killall worker", "ask:killall"),
    ("/usr/bin/pkill stays ask", "/usr/bin/pkill worker", "ask:pkill"),
    ("sudo /usr/bin/pkill stays ask", "sudo /usr/bin/pkill worker", "ask:pkill"),
    ("/bin/killall stays ask", "/bin/killall worker", "ask:killall"),
]

_RUNNER = r'''
import { readFileSync } from "node:fs";
import { evaluateBash } from "./guard.ts";
const [procRoot, selfPid, casesPath] = process.argv.slice(2);
const cases: [string, string][] = JSON.parse(readFileSync(casesPath, "utf8"));
const out: Record<string, string> = {};
for (const [name, command] of cases) {
  const findings = evaluateBash(command, { procRoot, selfPid: Number(selfPid) });
  const deny = findings.find((f) => f.kind === "deny");
  const hit = deny ?? findings[0];
  out[name] = hit ? `${hit.kind}:${hit.rule.id}` : "allow";
}
process.stdout.write(JSON.stringify(out) + "\n");
'''

# The real tool_call handler (default export), fed the incident command: the block reason an
# agent actually sees must carry the incident and the safe alternatives.
_HANDLER_RUNNER = r'''
import factory from "./guard.ts";
const handlers: Record<string, any> = {};
factory({ setLabel: () => {}, on: (ev: string, h: any) => { handlers[ev] = h; }, getAllTools: () => [] });
const command = process.argv[2];
const headless = await handlers["tool_call"]({ toolName: "bash", input: { command } }, { hasUI: false });
const confirmed = await handlers["tool_call"]({ toolName: "bash", input: { command } },
  { hasUI: true, ui: { confirm: async () => true } });
process.stdout.write(JSON.stringify({ headless, confirmed }) + "\n");
'''


def _bun() -> str | None:
    found = _bun_bin()
    if found:
        return found
    if os.environ.get("CI"):
        # CI installs bun on purpose: a silent skip there would leave the enforcement layer
        # untested on every PR (the tmux-gating lesson, INCIDENT 2026-06-17 follow-up a).
        pytest.fail("bun is required in CI to execute the generated guard")
    return None


def _write_fake_proc(root: Path) -> None:
    for pid, proc in FAKE_PROCS.items():
        d = root / str(pid)
        d.mkdir(parents=True)
        (d / "comm").write_text(proc.comm + "\n", encoding="utf-8")
        # field layout of /proc/<pid>/stat: pid (comm) state ppid pgrp session …
        (d / "stat").write_text(
            f"{pid} ({proc.comm}) S {proc.ppid} {pid} {proc.sid} 0 -1 4194560\n", encoding="utf-8"
        )
        (d / "cgroup").write_text(f"0::{proc.cgroup}\n", encoding="utf-8")


@pytest.fixture(scope="module")
def verdicts(tmp_path_factory) -> dict[str, str]:
    bun = _bun()
    if not bun:
        pytest.skip("bun runtime not available")
    work = tmp_path_factory.mktemp("guard-signals")
    proc_root = work / "proc"
    _write_fake_proc(proc_root)
    (work / "guard.ts").write_text(render_guard_ts(), encoding="utf-8")
    (work / "run.mts").write_text(_RUNNER, encoding="utf-8")
    (work / "cases.json").write_text(json.dumps([[n, c] for n, c, _ in CASES]), encoding="utf-8")
    proc = subprocess.run(
        [bun, "run.mts", str(proc_root), str(SELF_PID), str(work / "cases.json")],
        cwd=work, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_case_names_are_unique():
    names = [n for n, _, _ in CASES]
    assert len(names) == len(set(names))


@pytest.mark.parametrize(("name", "command", "expected"), CASES, ids=[c[0] for c in CASES])
def test_signal_rule_verdict(verdicts, name, command, expected):
    assert verdicts[name] == expected, f"{command!r}: expected {expected}, got {verdicts[name]}"


def test_every_signal_matcher_has_a_dispatch_branch_in_the_generated_guard():
    """SIGNAL_MATCHERS (Python) names the matchers; the TS dispatches on them. A matcher added to
    the registry without a branch would fail closed (block everything) — catch it here first."""
    ts = render_guard_ts()
    assert f"const SIGNAL_MATCHERS = new Set({json.dumps(list(SIGNAL_MATCHERS))});" in ts
    for matcher in SIGNAL_MATCHERS:
        assert f'if (rule.matcher === "{matcher}") return ' in ts, matcher


def test_signal_rules_are_registered_with_their_severity():
    deny = {r.id for r in OMP_GUARD_DENY_RULES}
    ask = {r.id for r in OMP_GUARD_ASK_RULES}
    assert {"kill-broadcast", "kill-pstree-fed", "signal-session-infra"} <= deny
    assert "kill-computed-pid" in ask


@pytest.mark.parametrize("rule_id", SIGNAL_RULE_IDS)
def test_signal_rule_hint_names_the_safe_alternative_and_the_incident(rule_id):
    rule = next(r for r in (*OMP_GUARD_DENY_RULES, *OMP_GUARD_ASK_RULES) if r.id == rule_id)
    assert "proc://bg_N/kill" in rule.hint
    assert "systemctl --user stop run-" in rule.hint
    assert '[ -n "$P" ] && ps -p "$P" && kill "$P"' in rule.hint
    assert "2026-10-15" in rule.hint
    assert "rig-cli#701" in rule.hint


def test_incident_is_blocked_by_the_real_handler_even_when_an_operator_would_confirm(tmp_path):
    bun = _bun()
    if not bun:
        pytest.skip("bun runtime not available")
    (tmp_path / "guard.ts").write_text(render_guard_ts(), encoding="utf-8")
    (tmp_path / "run.mts").write_text(_HANDLER_RUNNER, encoding="utf-8")
    proc = subprocess.run([bun, "run.mts", INCIDENT], cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    res = json.loads(proc.stdout.strip().splitlines()[-1])
    for verdict in (res["headless"], res["confirmed"]):
        assert verdict["block"] is True
        assert verdict["reason"].startswith("rig guard deny kill-pstree-fed: ")
        assert "proc://bg_N/kill" in verdict["reason"]


def test_check_entrypoint_cli(tmp_path):
    """``bun rig-permissions-guard.ts '<command>'`` is the guard's check entrypoint: it prints
    the verdict and exits 2 (deny) / 1 (ask) / 0 (allow)."""
    bun = _bun()
    if not bun:
        pytest.skip("bun runtime not available")
    guard = tmp_path / "rig-permissions-guard.ts"
    guard.write_text(render_guard_ts(), encoding="utf-8")

    def check(command: str) -> subprocess.CompletedProcess:
        return subprocess.run([bun, str(guard), command], cwd=tmp_path, capture_output=True, text=True, timeout=120)

    deny = check(INCIDENT)
    assert deny.returncode == 2, deny.stdout + deny.stderr
    assert deny.stdout.startswith("deny kill-pstree-fed: ")
    ask = check("pgrep -f worker | xargs kill")
    assert ask.returncode == 1, ask.stdout + ask.stderr
    assert ask.stdout.startswith("ask kill-computed-pid: ")
    allow = check("kill %1")
    assert allow.returncode == 0, allow.stdout + allow.stderr
    assert allow.stdout.strip() == "allow"
