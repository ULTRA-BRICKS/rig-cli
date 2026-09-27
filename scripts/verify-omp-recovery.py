#!/usr/bin/env python3
"""Opt-in real OMP end-to-end proof on a private tmux socket; needs abduco and a model account."""

from pathlib import Path
import os, sys, subprocess, tempfile, json, time, shlex, signal
import argparse, shutil

parser = argparse.ArgumentParser(
    description="Verify original OMP survives private tmux socket loss and server death"
)
parser.add_argument("--tg-cli-root", type=Path, required=True)
parser.add_argument("--omp-bin", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
options = parser.parse_args()
RIG = Path(__file__).resolve().parents[1]
TG = options.tg_cli_root.resolve()
OMP = options.omp_bin.resolve()
BUN = shutil.which("bun")
if not BUN or not OMP.is_file() or not (TG / "tg-ctl").is_file():
    parser.error("valid bun/OMP/tg-cli are required")
sys.path.insert(0, str(RIG))
from riglib import omp_launcher as ol, tmux_watchdog as wd

out = options.output.resolve()
out.mkdir(exist_ok=True)
root = Path(tempfile.mkdtemp(prefix="rig619-", dir="/tmp"))
root.chmod(0o700)
socket = root / "tmux.sock"
bin = root / "bin"
bin.mkdir()
sessions = root / "sessions"
sessions.mkdir()
real_tmux = shutil.which("tmux")
if not real_tmux:
    parser.error("tmux is required")
for name, body in {
    "tmux": f'exec {real_tmux} -S {shlex.quote(str(socket))} "$@"',
    "rig": f'exec {shlex.quote(sys.executable)} {shlex.quote(str(RIG / "bin/rig"))} "$@"',
    "tg-ctl": f'exec {shlex.quote(BUN)} {shlex.quote(str(TG / "tg-ctl"))} "$@"',
}.items():
    p = bin / name
    p.write_text("#!/bin/sh\n" + body + "\n")
    p.chmod(0o700)
os.environ.update(
    PATH=str(bin) + ":" + os.environ["PATH"],
    TG_CTL_CONFIG_DIR=str(root / "tg"),
    RIG_OMP_ABDUCO_DIR=str(root / "a"),
)
os.environ.pop("TMUX", None)


def run(args, **kw):
    return subprocess.run(
        args, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=15, **kw
    )


def check(args):
    r = run(args)
    if r.returncode:
        raise RuntimeError(f"{args}: {r.stderr[-1500:]}")
    return r.stdout.strip()


args = [
    "rig",
    "omp",
    "run",
    "--session-id",
    "omp-reconnect-proof",
    "--",
    str(OMP),
    "--session-dir",
    str(sessions),
    "--no-tools",
    "--no-skills",
    "--no-extensions",
    "-e",
    str(TG / "extensions/omp-live.ts"),
    "--no-title",
    "--model",
    "anthropic/claude-sonnet-5",
    "--thinking",
    "low",
]
check(
    [
        real_tmux,
        "-S",
        str(socket),
        "-f",
        "/dev/null",
        "new-session",
        "-d",
        "-s",
        "proof",
        "-x",
        "130",
        "-y",
        "38",
        "cd " + shlex.quote(str(root)) + " && exec " + shlex.join(args),
    ]
)
(out / "root.txt").write_text(str(root))
host = None
for _ in range(30):
    rows = json.loads(check(["tg-ctl", "omp-live", "status", "--json"]))
    if rows:
        host = rows[0]
        break
    time.sleep(0.3)
if not host:
    raise RuntimeError("original bridge did not publish")
assert host["durableSession"] == "omp-reconnect-proof", host
pid = host["pid"]
server = wd.query_server(socket, real_tmux)
assert server
mapfile = root / "panes.map"
assert ol.save_panes(mapfile), "exact save failed"
print("SAVED", mapfile.read_text(), flush=True)
wd.save_identity(server, root)
socket.unlink()
repaired = wd.run_once(socket, home=root, tmux_bin=real_tmux, cooldown_seconds=0)
assert repaired.action == "restore_socket", repaired
assert wd.query_server(socket, real_tmux) == server
print("LOST_SOCKET_RECOVERED_SAME_SERVER", server.pid, flush=True)
assert wd.query_server(socket, real_tmux) == server and socket.parent == root
os.kill(server.pid, signal.SIGKILL)
for _ in range(20):
    if run([real_tmux, "-S", str(socket), "has-session"]).returncode:
        break
    time.sleep(0.1)
os.kill(pid, 0)
boot = root / "boot.sh"
boot.write_text(
    "#!/bin/sh\nexec "
    + shlex.join(
        [
            real_tmux,
            "-S",
            str(socket),
            "-f",
            "/dev/null",
            "new-session",
            "-d",
            "-s",
            "proof",
            "-x",
            "130",
            "-y",
            "38",
            "/bin/sh",
        ]
    )
    + "\n"
)
boot.chmod(0o700)
restarted = wd.run_once(
    socket, home=root, tmux_bin=real_tmux, boot_script=boot, cooldown_seconds=0
)
assert restarted.action == "start", restarted
messages = []
blocked = ol.restore_panes(
    mapfile, log_fn=lambda text: (messages.append(text), print(text, flush=True))
)
assert blocked == 0 and any(": attach" in m for m in messages), messages
time.sleep(0.8)
after = json.loads(check(["tg-ctl", "omp-live", "status", "--json"]))[0]
assert (after["pid"], after["instanceId"], after["sessionId"]) == (
    host["pid"],
    host["instanceId"],
    host["sessionId"],
)
reply = json.loads(
    check(
        [
            "tg-ctl",
            "omp-live",
            "send",
            str(pid),
            "--session",
            host["sessionId"],
            "--request-id",
            "after-tmux-crash",
            "Reply exactly DURABLE_619_ACK. Do not use tools or change anything.",
        ]
    )
)
assert reply["pid"] == pid and reply["accepted"]
fresh = wd.query_server(socket, real_tmux)
result = {
    "root": str(root),
    "original_before": host,
    "original_after": after,
    "server_before": server.pid,
    "server_after": fresh.pid,
    "socket_repair": repaired.action,
    "server_recovery": restarted.action,
    "restore_action": "attach",
    "receipt": reply,
    "map": mapfile.read_text(),
    "same_pid": True,
    "same_instance": True,
    "same_session": True,
    "default_tmux_untouched": True,
}
(out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
print("PROOF_SUCCESS", json.dumps(result, ensure_ascii=False), flush=True)

# Stop only the fixture this invocation created; production/default tmux is never addressed.
command = run(["ps", "-p", str(pid), "-o", "command="]).stdout
if str(sessions) in command and "--no-tools" in command:
    os.kill(pid, signal.SIGTERM)
run([real_tmux, "-S", str(socket), "kill-server"])
