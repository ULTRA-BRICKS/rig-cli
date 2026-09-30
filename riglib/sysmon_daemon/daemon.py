"""The monitor loop: events on arrival, samples on the select timeout, one evaluation path.

``Monitor.run`` blocks in ``select()`` on the ``log stream`` child (:mod:`.events`); the
timeout is the time to the next in-process sample (:func:`.probe.sample`). A ``ps`` census
(:func:`.probe.census`) runs every ``census_interval_seconds`` — every 30 s while a finding is at
warning, on every sample while one is critical, and when a log event lands (at most once per
``EVENT_CENSUS_MIN_GAP_S``: a display-freeze burst must not fork a census per log line). Every
census also snapshots each process's start time (the pid-reuse guard) and feeds the
:class:`~.attrib.Lineage` that proves orphans. Every reading goes through
:meth:`Monitor.evaluate`, which is also what ``simulate`` drives, so a simulated incident
exercises exactly the production path:

1. incident lifecycle — the first critical finding opens an incident and snapshots the live
   agent sessions into it; the incident closes after ``CLOSE_AFTER_S`` without a critical one;
2. alerts — :func:`.alert.decide` (dedup + budget) then one ``tg`` message;
3. heal — :func:`.heal.plan` / :func:`.heal.execute` / :func:`.heal.record` (notify → throttle →
   opt-in kills; throttles are restored when the pressure clears and when the monitor stops);
4. remediation — :func:`.remediate.maybe_spawn` once per incident when ``auto_remediate``;
5. bookkeeping — the live-session snapshot, ``status.json`` heartbeat, ``memory.json``.

At start the previous heartbeat decides whether the machine crashed since the last run
(:func:`.sessions.detect_crash`); a crash opens a ``post-crash`` incident carrying the session
snapshot taken before it, alerts, and hands it to remediation / ``restore_on_boot``.
"""

from __future__ import annotations

import os
import select
import signal
import subprocess
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence

from . import __version__, alert, attrib, config, detect, events, heal, probe, remediate, sessions, store
from .model import SEVERITY_CRITICAL, SEVERITY_WARNING, Finding, Offender, Proc, to_jsonable

#: an incident closes after this long without any critical finding.
CLOSE_AFTER_S = 300.0
#: an event-triggered census reuses one younger than this.
EVENT_CENSUS_MIN_GAP_S = 10.0


class StopRequested(Exception):
    """Raised from the SIGTERM/SIGINT handler to leave ``select()`` at once."""


class Monitor:
    def __init__(
        self,
        cfg: Dict[str, Any],
        *,
        clock: Callable[[], float] = time.time,
        sampler: Optional[Callable[..., probe.Sample]] = None,
        censor: Optional[Callable[[], Optional[List[Proc]]]] = None,
        stream: Optional[events.LogStream] = None,
        persist: bool = True,
        run: Callable[..., Any] = subprocess.run,
        start_time_of: Callable[[int], Optional[float]] = probe.proc_start_time,
        heal_ops: Optional[heal.HealOps] = None,
    ) -> None:
        self.cfg = cfg
        self.state_dir = config.state_dir(cfg)
        self.clock = clock
        self.sampler = sampler or probe.sample
        self.censor = censor or probe.census
        self.persist = persist
        self.run_cmd = run
        self.stream = stream
        self.start_time_of = start_time_of
        self.heal_ops = heal_ops
        self.lineage = attrib.Lineage(start_time_of)
        self.memory: Dict[str, Any] = store.read_json(self.state_dir / store.MEMORY, {}) or {}
        self.history: Deque[probe.Sample] = deque(maxlen=90)
        self.window = events.EventWindow()
        self.sample: Optional[probe.Sample] = None
        self.procs: Optional[List[Proc]] = None
        self.attributor: Optional[attrib.Attributor] = None
        self.census_at = 0.0
        self.snapshot_at = 0.0
        self.findings: List[Finding] = []
        self.started_at = clock()
        self.incident: Optional[Dict[str, Any]] = None
        open_id = self.memory.get("open_incident")
        if open_id:
            self.incident = store.load_incident(self.state_dir, str(open_id))

    # ── readings ──────────────────────────────────────────────────────────────────────────
    def take_census(self, now: float) -> None:
        procs = self.censor()
        self.census_at = now
        if procs is None:
            return
        s = self.cfg["sessions"]
        self.procs = procs
        self.attributor = attrib.Attributor(
            procs,
            panes=probe.tmux_panes(str(s.get("tmux_bin") or "tmux"), run=self.run_cmd),
            launchd_pids=probe.launchd_job_pids(run=self.run_cmd),
            sessions_root=config.expand(str(s["sessions_dir"])),
            starts={p.pid: self.start_time_of(p.pid) for p in procs},
            lineage=self.lineage,
        )
        self.lineage.observe(self.attributor)

    def _offender(self, proc: Proc) -> Offender:
        assert self.attributor is not None
        return Offender(
            pid=proc.pid, comm=proc.comm, args=proc.args[:300], rss_kb=proc.rss_kb,
            start_time=self.attributor.starts.get(proc.pid),
            attribution=self.attributor.attribute(proc.pid),
        )

    def orphan_tree(self, pid: int) -> List[int]:
        """The ORPHANED tree ``pid`` belongs to (its reparented root + every descendant), or
        just ``[pid]`` when the tree is not an orphan.

        Every tree on macOS ends at pid 1, including a GUI app launchd started (cmux, a
        terminal) with every shell, agent and test run below it. Widening to such a root once
        turned "3 whisper processes over the cap" into every process under cmux; only a root
        :class:`~.attrib.Attributor` proves orphaned (seen under an agent host that died since,
        not a launchd job, not protected) may widen the blame."""
        assert self.attributor is not None
        root = self.attributor.orphan_root(pid)
        if root is None:
            return [pid]
        children: Dict[int, List[int]] = {}
        for p in self.attributor.by_pid.values():
            children.setdefault(p.ppid, []).append(p.pid)
        out, todo = [], [root]
        while todo:
            cur = todo.pop()
            out.append(cur)
            todo.extend(children.get(cur, []))
        return out

    def census_findings(self) -> List[Finding]:
        if self.procs is None or self.attributor is None:
            return []
        g = self.cfg["gpu"]
        slot_dir = config.expand(str(g["slot_dir"]))
        found = detect.census_findings(
            self.procs,
            self.cfg["thresholds"],
            my_uid=os.getuid(),
            gpu_patterns=list(g["patterns"]),
            gpu_slots=int(g["slots"]),
            gpu_slots_held=probe.gpu_slots_held(slot_dir, int(g["slots"])),
            offender=self._offender,
            tree_of=self.orphan_tree,
        )
        return found

    def with_context(self, findings: List[Finding]) -> List[Finding]:
        """Attach the top resident processes to memory findings (who holds the RAM)."""
        if self.procs is None or self.attributor is None:
            return findings
        top: Optional[tuple] = None
        out = []
        for f in findings:
            if f.kind in ("memory", "swap") and not f.offenders:
                if top is None:
                    top = tuple(detect.top_rss([p for p in self.procs if p.uid == os.getuid()], self._offender))
                f = Finding(f.key, f.kind, f.severity, f.summary, f.metrics, top, False)
            out.append(f)
        return out

    def tick(self, now: float) -> List[Finding]:
        """One sampling round: sample, census when due, evaluate."""
        s = self.sampler(now)
        found = detect.sample_findings(s, self.cfg["thresholds"], list(self.history))
        self.history.append(s)
        self.sample = s
        ev = detect.event_findings(self.window.recent(now), self.cfg["thresholds"], now)
        # The ps census follows the severity: every tick while something is critical, every 30 s
        # on a warning, else every census_interval_seconds.
        worst = detect.worst(found + ev)
        gap = {SEVERITY_CRITICAL: 0.0, SEVERITY_WARNING: 30.0}.get(worst, float(self.cfg["census_interval_seconds"]))
        if self.procs is None or now - self.census_at >= gap:
            self.take_census(now)
        findings = self.with_context(found + self.census_findings() + ev)
        self.evaluate(findings, now)
        return findings

    def on_events(self, now: float) -> None:
        """New log events arrived: re-evaluate with the latest sample and a census at most
        ``EVENT_CENSUS_MIN_GAP_S`` old (a burst of log lines reuses the census it just took)."""
        ev = detect.event_findings(self.window.recent(now), self.cfg["thresholds"], now)
        if not ev or self.sample is None:
            return
        if now - self.census_at >= EVENT_CENSUS_MIN_GAP_S:
            self.take_census(now)
        found = detect.sample_findings(self.sample, self.cfg["thresholds"], list(self.history)[:-1])
        self.evaluate(self.with_context(found + self.census_findings() + ev), now)

    # ── the one evaluation path ───────────────────────────────────────────────────────────
    def evaluate(self, findings: List[Finding], now: float, *, kind: str = "live") -> None:
        self.findings = findings
        critical = [f for f in findings if f.severity == SEVERITY_CRITICAL]
        if critical and self.incident is None:
            self._open_incident(now, kind)
        if self.incident is not None:
            self._update_incident(findings, now)

        due, resolved = [], []
        if self.cfg["alert"].get("enabled", True):
            due, resolved, self.memory["alert"] = alert.decide(findings, self.memory.get("alert") or {}, self.cfg, now)
        if due:
            title, body = alert.render(
                due, incident_id=self.incident["id"] if self.incident else None,
                extra_lines=self._memory_line(),
            )
            rec = alert.send(title, body, self.cfg, run=self.run_cmd)
            self._log("alerts", rec)
        if resolved:
            title, body = alert.render_resolved(resolved)
            self._log("alerts", alert.send(title, body, self.cfg, tag="report", run=self.run_cmd))

        steps, self.memory["heal"] = heal.plan(findings, self.memory.get("heal") or {}, self.cfg, now)
        if steps:
            # The mitigation first: throttles and restores are quick (macOS: syscalls, no fork),
            # done in one burst and written to disk right after, so a daemon SIGKILLed while it
            # waits on a note or a kill grace leaves no throttle without its restore record. One
            # write per tick, not per offender: a 300-process storm must not be 300 writes.
            fast = [s for s in steps if s.action in ("throttle", "restore")]
            heal.execute(fast, self.cfg, self.heal_ops, on_result=self._fold_heal)
            if fast:
                self._save_memory()
            slow = [s for s in steps if s.action not in ("throttle", "restore")]
            heal.execute(slow, self.cfg, self.heal_ops, on_result=self._fold_heal)

        if self.incident is not None and critical and not self.incident.get("remediation"):
            result, self.memory["remediation"] = remediate.maybe_spawn(
                self.incident, store.incident_dir(self.state_dir, self.incident["id"]), self.cfg,
                self.memory.get("remediation") or {}, now, run=self.run_cmd,
            )
            if result.get("status") not in ("disabled",):
                self.incident["remediation"] = result
                self._save_incident()

        if self.incident is not None and not critical:
            if now - float(self.incident.get("last_critical_at") or now) >= CLOSE_AFTER_S:
                self.incident["closed_at"] = now
                self._save_incident()
                self.incident = None
        self.memory["open_incident"] = self.incident["id"] if self.incident else None

        if self.attributor is not None and now - self.snapshot_at >= float(self.cfg["snapshot_interval_seconds"]):
            if self.persist:
                sessions.write_snapshot(self.state_dir, sessions.snapshot(self.attributor.hosts(), now))
            self.snapshot_at = now
        self.write_status(now)

    def _memory_line(self) -> List[str]:
        s = self.sample
        if s is None:
            return []
        gb = probe.GIB
        return [
            f"now: load {s.load1:.1f}/{s.ncpu} CPU, wired {(s.wired or 0) / gb:.1f}/{s.mem_total / gb:.1f} GB, "
            f"reclaimable {(s.reclaimable or 0) / gb:.1f} GB, swap {s.swap_used / gb:.1f}/{s.swap_total / gb:.1f} GB, "
            f"{s.proc_total} procs"
        ]

    # ── incidents ─────────────────────────────────────────────────────────────────────────
    def _open_incident(self, now: float, kind: str, **extra: Any) -> None:
        hosts = self.attributor.hosts() if self.attributor is not None else []
        records = sessions.snapshot(hosts, now) if hosts else sessions.read_snapshot(self.state_dir)
        self.incident = {
            "id": store.new_incident_id(now, kind),
            "kind": kind,
            "opened_at": now,
            "updated_at": now,
            "severity": SEVERITY_CRITICAL,
            "findings": [],
            "sample": self.sample.as_dict() if self.sample else None,
            "sessions": to_jsonable(records),
            "crash": None,
            "heal": [],
            "remediation": None,
            "restore": [],
            "alerts": [],
        }
        self.incident.update(extra)
        self._save_incident()

    def _update_incident(self, findings: List[Finding], now: float) -> None:
        assert self.incident is not None
        known = {f["key"]: f for f in self.incident.get("findings") or []}
        for f in findings:
            known[f.key] = to_jsonable(f)
        self.incident["findings"] = list(known.values())
        self.incident["updated_at"] = now
        if any(f.severity == SEVERITY_CRITICAL for f in findings):
            self.incident["last_critical_at"] = now
        if self.sample is not None:
            self.incident["sample"] = self.sample.as_dict()
        self._save_incident()

    def _save_incident(self) -> None:
        if self.incident is not None:
            store.save_incident(self.state_dir, self.incident)

    def _log(self, key: str, record: Dict[str, Any]) -> None:
        if self.incident is not None:
            self.incident.setdefault(key, []).append(record)
            self._save_incident()
        else:
            log = self.memory.setdefault(f"recent_{key}", [])
            log.append(record)
            del log[:-50]

    # ── crash recovery at start ───────────────────────────────────────────────────────────
    def check_crash(self, now: float) -> Optional[Dict[str, Any]]:
        prev = store.read_json(self.state_dir / store.STATUS, {}) or {}
        s = self.sampler(now)
        self.sample = s
        panic_dirs = [config.expand(str(p)) for p in self.cfg["sessions"]["panic_dirs"]]
        crash = sessions.detect_crash(prev.get("heartbeat"), s.boot_id, panic_dirs)
        if crash is None:
            return None
        finding = Finding(
            key="crash", kind="crash", severity=SEVERITY_CRITICAL,
            summary=(f"the machine went down without a clean shutdown ({crash.get('reason')}); "
                     f"last heartbeat {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(crash.get('last_heartbeat') or 0)))}"),
            metrics={"panic_file": crash.get("panic_file")},
        )
        self.incident = None
        self._open_incident(now, "post-crash", crash=crash)
        assert self.incident is not None
        self.incident["sessions"] = to_jsonable(sessions.read_snapshot(self.state_dir))
        self._update_incident([finding], now)
        n = len(self.incident["sessions"])
        extra = [f"{n} agent session(s) were live before the crash — "
                 f"rig sysmon restore --incident {self.incident['id']}"]
        if crash.get("panic_excerpt"):
            extra.append(str(crash["panic_excerpt"])[:300])
        if self.cfg["alert"].get("enabled", True):
            title, body = alert.render([finding], incident_id=self.incident["id"], extra_lines=extra)
            self._log("alerts", alert.send(title, body, self.cfg, run=self.run_cmd))
        result, self.memory["remediation"] = remediate.maybe_spawn(
            self.incident, store.incident_dir(self.state_dir, self.incident["id"]), self.cfg,
            self.memory.get("remediation") or {}, now, run=self.run_cmd,
        )
        self.incident["remediation"] = result
        self._save_incident()
        closed = self.incident
        closed["closed_at"] = now
        self._save_incident()
        self.incident = None
        if self.cfg["sessions"].get("restore_on_boot") and result.get("status") != "spawned":
            # restore() records its own steps in incident.json; re-read it afterwards.
            sessions.restore(self.state_dir, closed["id"], self.cfg, run=self.run_cmd)
            closed = store.load_incident(self.state_dir, closed["id"]) or closed
        return closed

    # ── status ────────────────────────────────────────────────────────────────────────────
    def write_status(self, now: float, *, clean_shutdown: bool = False) -> None:
        if not self.persist:
            return
        hosts = self.attributor.hosts() if self.attributor is not None else []
        status = {
            "heartbeat": {
                "pid": os.getpid(),
                "version": __version__,
                "started_at": self.started_at,
                "last_heartbeat": now,
                "boot_id": self.sample.boot_id if self.sample else None,
                "clean_shutdown": clean_shutdown,
                "sample_interval_seconds": self.cfg["sample_interval_seconds"],
            },
            "sample": self.sample.as_dict() if self.sample else None,
            "findings": to_jsonable(self.findings),
            "census_at": self.census_at,
            "proc_count": len(self.procs) if self.procs is not None else None,
            "agents": [{"pid": h.pid, "kind": h.kind, "pane": h.pane, "cwd": h.cwd,
                        "session_id": h.session_id, "interactive": h.interactive} for h in hosts],
            "open_incident": self.incident["id"] if self.incident else None,
            "log_stream": "running" if self.stream is not None and self.stream.fileno() is not None
            else ("off" if self.stream is None else "restarting"),
            "log_stream_restarts": self.stream.restarts if self.stream is not None else 0,
            "events_recent": len(self.window.recent(now)),
        }
        store.write_json(self.state_dir / store.STATUS, status)
        self._save_memory()

    # ── the loop ──────────────────────────────────────────────────────────────────────────
    def run(self, max_ticks: Optional[int] = None) -> int:
        def _stop(signum, frame):  # noqa: ARG001 — signal handler signature
            raise StopRequested()

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        interval = float(self.cfg["sample_interval_seconds"])
        ticks = 0
        try:
            self.check_crash(self.clock())
            next_sample = self.clock()
            while max_ticks is None or ticks < max_ticks:
                if self.stream is not None:
                    self.stream.ensure()
                timeout = max(0.0, next_sample - self.clock())
                fd = self.stream.fileno() if self.stream is not None else None
                if fd is not None:
                    readable, _, _ = select.select([fd], [], [], timeout)
                    if readable:
                        got = False
                        for line in self.stream.read_lines():
                            ev = events.parse_event(line, self.clock())
                            if ev is not None:
                                self.window.add(ev)
                                got = True
                        if got:
                            self.on_events(self.clock())
                elif timeout:
                    time.sleep(timeout)
                if self.clock() >= next_sample:
                    self.tick(self.clock())
                    ticks += 1
                    next_sample = self.clock() + interval
        except StopRequested:
            pass
        finally:
            if self.stream is not None:
                self.stream.stop()
            self.release_throttles()
            self.write_status(self.clock(), clean_shutdown=True)
        return 0

    def release_throttles(self) -> None:
        """A stopped (disabled, uninstalled, reloaded) monitor must not leave processes in the
        background band; the next start throttles again if the pressure is still there."""
        steps, self.memory["heal"] = heal.release(self.memory.get("heal") or {})
        if steps:
            heal.execute(steps, self.cfg, self.heal_ops, on_result=self._fold_heal)

    def _fold_heal(self, result: Dict[str, Any]) -> None:
        """Each heal outcome lands in memory the moment it happens: a stop signal in the middle of
        a batch must still find every throttle already applied (``release_throttles``)."""
        self._log("heal", result)
        self.memory["heal"] = heal.record(self.memory["heal"], [result])

    def _save_memory(self) -> None:
        if self.persist:
            store.write_json(self.state_dir / store.MEMORY, self.memory)


def build(cfg: Dict[str, Any], *, with_events: bool = True) -> Monitor:
    stream = events.LogStream() if with_events and cfg["events"].get("enabled") and probe.IS_DARWIN else None
    return Monitor(cfg, stream=stream)


def simulate(
    cfg: Dict[str, Any],
    findings: Sequence[Finding],
    now: Optional[float] = None,
    *,
    crash: Optional[Dict[str, Any]] = None,
    sessions_under: Optional[str] = None,
    evidence: Sequence[str] = (),
) -> Dict[str, Any]:
    """Run synthetic findings through the production evaluation path as a ``simulated`` incident.

    Alerts and heal are forced to dry-run (a simulation must never message Telegram or signal a
    real process); remediation follows the configuration, so ``auto_remediate: true`` launches
    a real detached session against the simulated incident. Nothing is written to the live
    daemon's ``status.json``/``memory.json``.

    ``crash`` simulates the post-crash path: the incident carries the crash record and the
    sessions from the LAST SNAPSHOT (``sessions.json``, what a real reboot would have), instead of
    the live ones. ``sessions_under`` keeps only sessions whose cwd is under that directory, so a
    drill can target a throwaway session and never another agent's. ``evidence`` lines (where the
    drill planted its cause) ride in the incident and the remediation brief.
    """
    import copy

    sim_cfg = copy.deepcopy(cfg)
    sim_cfg["alert"]["dry_run"] = True
    sim_cfg["heal"]["dry_run"] = True
    now = time.time() if now is None else now
    mon = Monitor(sim_cfg, persist=False)
    mon.memory = {}
    mon.incident = None
    mon.sample = mon.sampler(now)
    mon.take_census(now)
    mon._open_incident(now, "simulated", crash=crash)
    assert mon.incident is not None
    records = mon.incident["sessions"]
    if crash is not None:
        records = to_jsonable(sessions.read_snapshot(mon.state_dir))
    if sessions_under:
        root = os.path.realpath(sessions_under).rstrip("/") + "/"
        records = [r for r in records if (os.path.realpath(r.get("cwd") or "/nonexistent") + "/").startswith(root)]
    mon.incident["sessions"] = records
    mon.incident["evidence"] = list(evidence)
    mon._save_incident()
    mon.evaluate(list(findings), now, kind="simulated")
    return mon.incident
