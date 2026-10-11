"""``python3 -m sysmon_daemon <command>`` — also reached as ``rig sysmon <command>``.

- ``run``         the daemon loop (what the LaunchAgent execs)
- ``status``      daemon health from the heartbeat + a LIVE in-process reading (exit 3 when
                  the daemon is not running or its heartbeat is stale)
- ``sample``      one live reading plus every detector, without alerting or healing
- ``test-alert``  forced-condition smoke test: the 2026-09-27 process-leak + compressor-limit +
                  low-swap pattern, labelled TEST, through the real render + ``tg`` send path
- ``simulate``    synthetic findings through the production evaluation path as a ``simulated``
                  incident (alerts/heal forced to dry-run; remediation follows the config)
- ``incidents``   list incidents, or ``--show ID``
- ``restore``     resume the omp sessions an incident interrupted (see :mod:`.sessions`)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from typing import Any, Dict, List, Optional

from . import alert, config, daemon, detect, probe, sessions, store
from .model import SEVERITY_CRITICAL, Finding, to_jsonable

DEFAULT_CONFIG = "~/.config/rig/sysmon/sysmon.json"
GIB = probe.GIB


def _cfg(path: Optional[str]) -> Dict[str, Any]:
    return config.load(config.expand(path or DEFAULT_CONFIG))


def health(cfg: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    """``{"state": running|stale|stopped|never, "age_s", "heartbeat", "status"}`` from status.json."""
    now = time.time() if now is None else now
    status = store.read_json(config.state_dir(cfg) / store.STATUS)
    if not isinstance(status, dict) or not isinstance(status.get("heartbeat"), dict):
        return {"state": "never", "age_s": None, "heartbeat": None, "status": None}
    hb = status["heartbeat"]
    age = now - float(hb.get("last_heartbeat") or 0)
    limit = max(30.0, 3 * float(cfg["sample_interval_seconds"])) + 30.0
    if hb.get("clean_shutdown"):
        state = "stopped"
    elif age > limit:
        state = "stale"
    else:
        state = "running"
    return {"state": state, "age_s": age, "heartbeat": hb, "status": status}


def format_sample(s: Dict[str, Any]) -> str:
    def gb(key: str) -> str:
        v = s.get(key)
        return "n/a" if v is None else f"{v / GIB:.1f} GB"

    def mb(key: str) -> str:
        v = s.get(key)
        return "n/a" if v is None else f"{v / 1024 ** 2:.0f} MB"

    def kb(key: str) -> str:
        v = s.get(key)
        return "n/a" if v is None else f"{v // 1024} KB"

    pressure = {1: "normal", 2: "WARN", 4: "CRITICAL"}.get(s.get("pressure_level"), "n/a")
    free = mb("free")
    if s.get("free_pages") is not None:
        free += f" = {s['free_pages']} pages of {s.get('page_size')} bytes (vm_stat \"Pages free\")"
    used_pct = s.get("data_used_pct")
    parts = [
        f"load {s['load1']:.2f} on {s['ncpu']} CPUs ({s['load_per_cpu']:.2f}/CPU)",
        f"wired {gb('wired')} of {gb('mem_total')} ({(s.get('wired_pct') or 0):.1f}%)",
        f"reclaimable {gb('reclaimable')}",
        f"free {free}",
        f"swap {gb('swap_used')} of {gb('swap_total')} ({(s.get('swap_pct') or 0):.1f}%)",
        f"compressor {(s.get('compressor_pct') or 0):.1f}% of limit",
        f"disk free {gb('disk_free')} (swap volume)",
        f"data volume {s.get('data_volume') or 'n/a'} "
        f"{'n/a' if used_pct is None else f'{used_pct:.1f}%'} full: "
        f"used {kb('data_used')}, available {kb('data_free')} (df -k Used/Available)",
        f"procs {s['proc_total']}/{s.get('proc_max') or '?'}",
        f"memory pressure {pressure}",
    ]
    return "\n".join(f"  {p}" for p in parts)


def _print_findings(findings: List[Dict[str, Any]]) -> None:
    if not findings:
        print("  none")
    for f in findings:
        print(f"  {f['severity'].upper():8} {f['key']}: {f['summary']}")
        owners: Dict[str, int] = {}
        for o in f.get("offenders") or []:
            host = (o.get("attribution") or {}).get("host")
            if host:
                label = f"{host['kind']} pid {host['pid']} {host.get('pane') or ''}".strip()
            elif (o.get("attribution") or {}).get("orphan"):
                label = "orphan"
            else:
                label = "unattributed"
            owners[label] = owners.get(label, 0) + 1
        for label, n in list(owners.items())[:5]:
            print(f"           {n} proc ← {label}")


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _cfg(args.config)
    h = health(cfg)
    live = probe.sample()
    if args.json:
        print(json.dumps({"health": {k: v for k, v in h.items() if k != "status"}, "live": live.as_dict(),
                          "daemon": h["status"]}, indent=1, default=str))
        return 0 if h["state"] == "running" else 3
    hb = h["heartbeat"] or {}
    if h["state"] == "never":
        print(f"sysmon: never ran (no heartbeat in {config.state_dir(cfg)})")
    else:
        st = h["status"] or {}
        print(f"sysmon: {h['state']} (pid {hb.get('pid')}, heartbeat {h['age_s']:.0f}s ago, "
              f"log stream {st.get('log_stream')}, {len(st.get('agents') or [])} agent hosts, "
              f"open incident {st.get('open_incident') or 'none'})")
    print("live now:")
    print(format_sample(live.as_dict()))
    if h["status"]:
        print("daemon findings (last tick):")
        _print_findings(h["status"].get("findings") or [])
    return 0 if h["state"] == "running" else 3


def cmd_sample(args: argparse.Namespace) -> int:
    cfg = _cfg(args.config)
    cfg["alert"]["dry_run"] = True
    mon = daemon.Monitor(cfg, persist=False)
    now = time.time()
    s = probe.sample(now)
    mon.sample = s
    mon.take_census(now)
    findings = mon.with_context(
        detect.sample_findings(s, cfg["thresholds"]) + mon.census_findings()
    )
    coalitions = mon.top_coalitions(5)
    if args.json:
        print(json.dumps({"sample": s.as_dict(), "coalitions": coalitions, "findings": to_jsonable(findings)},
                         indent=1, default=str))
        return 0
    print(format_sample(s.as_dict()))
    print("largest launchd coalitions:")
    for c in coalitions:
        share = f" ({c['pct']:.1f}% of the limit)" if c["pct"] is not None else ""
        print(f"  {c['procs']:5d} procs{share}  {c['label'] or '(no live launchd job)'}  leader {c['leader']}")
    print("findings:")
    _print_findings(to_jsonable(findings))
    return 0


def _test_findings() -> List[Finding]:
    """The 2026-09-27 pattern: a tg-ctl process leak, the compressor at its limit, swap exhausted."""
    return [
        Finding("storm:tg-ctl", "storm", SEVERITY_CRITICAL, "process storm: 2400 × tg-ctl (TEST data)",
                {"count": 2400}),
        Finding("compressor", "memory", SEVERITY_CRITICAL,
                "memory compressor at 97% of its page limit (TEST data)", {"compressor_pct": 97}),
        Finding("swap", "swap", SEVERITY_CRITICAL, "swap 5.9 GB of 6.0 GB used (98%) (TEST data)",
                {"swap_pct": 98}),
    ]


def cmd_test_alert(args: argparse.Namespace) -> int:
    cfg = _cfg(args.config)
    if args.dry_run:
        cfg["alert"]["dry_run"] = True
    findings = _test_findings()
    title, body = alert.render(findings, incident_id=None, test=True)
    rec = alert.send(title, body, cfg, severity=alert.worst_severity(findings), key=f"test-{uuid.uuid4().hex[:12]}")
    print(json.dumps(rec, indent=1, default=str))
    return 0 if rec.get("outcome") in ("sent", "dry_run") else 1


SCENARIOS = {
    "wired": [
        Finding("wired", "memory", SEVERITY_CRITICAL,
                "wired memory 24.6 GB of 25.8 GB (95.7%) (SIMULATED, 2026-09-29 12:47 panic values)",
                {"wired_pct": 95.7}),
        Finding("gpu_models", "gpu", SEVERITY_CRITICAL,
                "13 GPU-model processes running, cap is 2 (SIMULATED)", {"gpu_models": 13, "gpu_slots": 2}),
    ],
    "storm": [
        Finding("storm:whisper-cli", "storm", SEVERITY_CRITICAL, "process storm: 300 × whisper-cli (SIMULATED)",
                {"count": 300}),
    ],
    "display": [
        Finding("display_freeze", "display", SEVERITY_CRITICAL,
                "WindowServer timers delayed 9× in the last minute (longest 8.81s) (SIMULATED)",
                {"display_delays_per_min": 9, "display_max_delay_s": 8.81}),
    ],
    "taskport": [
        Finding("taskport_blocked", "taskport", SEVERITY_CRITICAL,
                "SecurityAgent prompt is up and 32 task_for_pid tool(s) (sample) hang behind it (SIMULATED)",
                {"stuck": 32}),
    ],
    # the post-crash path: sessions come from the last snapshot, as after a real reboot
    "crash": [
        Finding("crash", "crash", SEVERITY_CRITICAL,
                "the machine went down without a clean shutdown (SIMULATED drill)", {}),
    ],
    # a full data volume: the alert lists reclaimable candidates (report only, nothing deleted);
    # the scan runs for real (read-only) against this machine's worktrees, caches and images
    "disk": [
        Finding("data_volume", "disk", SEVERITY_CRITICAL,
                "data volume /System/Volumes/Data 96.0% full (19.4 GB free) (SIMULATED, 2026-09-30 df values)",
                {"data_used_pct": 96.0, "volume": "/System/Volumes/Data", "data_free_gb": 19.4}),
    ],
}


def cmd_simulate(args: argparse.Namespace) -> int:
    cfg = _cfg(args.config)
    crash = None
    if args.scenario == "crash":
        crash = {"reason": "simulated", "last_heartbeat": time.time(), "panic_file": None,
                 "panic_excerpt": "SIMULATED: no real panic", "boot_id_before": None, "boot_id_now": None}
    incident = daemon.simulate(cfg, SCENARIOS[args.scenario], crash=crash, sessions_under=args.sessions_under,
                               evidence=args.evidence or ())
    if args.json:
        print(json.dumps(incident, indent=1, default=str))
    else:
        rem = incident.get("remediation") or {}
        print(f"simulated incident {incident['id']}: {', '.join(f['key'] for f in incident['findings'])}")
        print(f"  sessions captured: {len(incident.get('sessions') or [])}")
        print(f"  alerts (dry-run): {len(incident.get('alerts') or [])}; heal steps (dry-run): {len(incident.get('heal') or [])}")
        rec = incident.get("reclaimable")
        if rec:
            print(f"  reclaimable candidates: {len(rec.get('candidates') or [])} listed, {rec.get('more', 0)} more, "
                  f"{rec.get('unmeasured', 0)} unmeasured — {rec.get('note')}")
        print(f"  remediation: {rem.get('status', 'disabled')} {rem.get('name', '')}")
        print(f"  {store.incident_dir(config.state_dir(cfg), incident['id']) / store.INCIDENT_FILE}")
    return 0


def cmd_incidents(args: argparse.Namespace) -> int:
    cfg = _cfg(args.config)
    sd = config.state_dir(cfg)
    if args.show:
        inc = store.load_incident(sd, args.show)
        if inc is None:
            print(f"no incident {args.show!r} in {sd}", file=sys.stderr)
            return 2
        print(json.dumps(inc, indent=1, default=str))
        return 0
    items = store.list_incidents(sd)
    if args.json:
        print(json.dumps(items, indent=1, default=str))
        return 0
    if not items:
        print("no incidents")
    for inc in items:
        opened = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(inc.get("opened_at") or 0)))
        state = "closed" if inc.get("closed_at") else "OPEN"
        keys = ", ".join(f["key"] for f in inc.get("findings") or [])
        rem = (inc.get("remediation") or {}).get("status", "-")
        print(f"{inc['id']}  {inc.get('kind'):10} {state:6} {opened}  sessions={len(inc.get('sessions') or [])} "
              f"remediation={rem}  {keys}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _cfg(args.config)
    mon = daemon.build(cfg, with_events=not args.no_events)
    return mon.run(max_ticks=args.max_ticks)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sysmon", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("--config", help=f"sysmon.json (default {DEFAULT_CONFIG})")
        return sp

    r = add("run", "run the monitor loop (the LaunchAgent's command)")
    r.add_argument("--no-events", action="store_true", help="do not start the log stream")
    r.add_argument("--max-ticks", type=int, default=None, help=argparse.SUPPRESS)
    r.set_defaults(func=cmd_run)
    s = add("status", "daemon health + a live reading")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)
    s = add("sample", "one live reading and every detector, no side effects")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_sample)
    t = add("test-alert", "send ONE labelled TEST alert through the real tg path")
    t.add_argument("--dry-run", action="store_true", help="render and print, do not send")
    t.set_defaults(func=cmd_test_alert)
    m = add("simulate", "run synthetic findings through the production path as a simulated incident")
    m.add_argument("--scenario", choices=sorted(SCENARIOS), required=True)
    m.add_argument("--sessions-under", help="keep only sessions whose cwd is under this dir (drills)")
    m.add_argument("--evidence", action="append", help="where the drill planted its cause (repeatable)")
    m.add_argument("--json", action="store_true")
    m.set_defaults(func=cmd_simulate)
    i = add("incidents", "list incidents or show one")
    i.add_argument("--show", metavar="ID")
    i.add_argument("--json", action="store_true")
    i.set_defaults(func=cmd_incidents)
    sub.add_parser("restore", help="resume the omp sessions an incident interrupted", add_help=False)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["restore"]:
        return sessions.restore_main(argv[1:])
    args = build_parser().parse_args(argv)
    return int(args.func(args))
