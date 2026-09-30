"""Attribute a process to the agent that owns it, and decide whether it may ever be touched.

Walks the ppid chain from the census (no extra syscalls) to the nearest agent host (omp,
claude, codex, opencode) and the nearest tmux pane (``tmux list-panes`` pane_pid) or cmux
surface (the ``CMUX_WORKSPACE_ID``/``CMUX_SURFACE_ID`` cmux exports into every surface). The
host's cwd and its exact session transcript come from :mod:`.probe`'s libproc helpers — the
transcript is the ``.jsonl`` the host process holds OPEN, never "newest file in the cwd" (two
agents in one directory stay distinct).

:func:`protected_reason` is the one predicate the heal ladder consults before any signal.
Anything user-interactive — a foreground terminal job no agent started, an agent host itself,
a shell, a terminal or multiplexer, a GUI app — is never throttled or killed; neither is a
process of another user, a system binary, or a launchd job.

An **orphan** needs positive evidence (:class:`Lineage`): an earlier census saw the tree under
an agent host, and that host has died since. A tree that is merely reparented to launchd — an
agent-browser daemon, a ``nohup``'d job of a live agent, rig's ship-loop — is not an orphan.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from .model import NO_TTY, AgentHost, Attribution, Proc, same_start
from . import probe

#: Terminals, multiplexers and session plumbing: never touched.
PROTECTED_COMMS = frozenset({
    "launchd", "systemd", "init", "kernel_task", "WindowServer", "loginwindow", "Finder", "Dock",
    "SystemUIServer", "SecurityAgent", "authd",
    "login", "sshd", "sshd-session", "ssh", "ssh-agent", "mosh-server", "mosh-client",
    "tmux", "tmux: server", "tmux: client", "screen", "abduco", "dtach", "zellij",
    "cmux", "cmux-cua", "Terminal", "iTerm2", "ghostty", "kitty", "alacritty", "Alacritty",
    "wezterm", "wezterm-gui", "Warp", "stable",
})

#: Shells are protected while they are someone's interactive shell (they have a terminal, or
#: they are a pane's process). A terminal-less shell whose owner died — the orphaned
#: ``bash -c 'while …; do stt …; done'`` loop that kept relaunching whisper-cli for 30 minutes
#: after the omp host crashed on 2026-09-29 — is exactly what the heal ladder must be able to stop.
SHELL_COMMS = frozenset({
    "sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "nu", "-zsh", "-bash", "-sh", "-fish",
})

_SYSTEM_PREFIXES = ("/System/", "/usr/libexec/", "/usr/sbin/", "/sbin/", "/usr/lib/systemd/", "/lib/systemd/")

_OMP_BIN = re.compile(r"(?:^|/)omp$")
_OMP_SOURCE = re.compile(r"/(?:scripts/omp\.ts|src/cli\.ts)(?:\s|$)")
_CLAUDE_SESSION = re.compile(r"/projects/[^/]+/[0-9a-f-]{36}\.jsonl$")
_CODEX_SESSION = re.compile(r"/sessions/\d{4}/\d{2}/\d{2}/rollout-[^/]+\.jsonl$")


def _argv(proc: Proc) -> List[str]:
    return proc.args.split()


def classify_host(proc: Proc) -> Optional[Tuple[str, bool]]:
    """``(kind, interactive)`` when ``proc`` is an agent harness host, else ``None``.

    ``interactive`` is False for one-shot runs (``omp -p``, ``claude -p``, ``codex exec``,
    ``opencode run``) — still hosts for attribution, never candidates for session restore.
    """
    argv = _argv(proc)
    if not argv:
        return None
    first = os.path.basename(argv[0])
    rest = argv[1:]
    if "__omp_worker" in proc.args:
        return None
    if _OMP_BIN.search(argv[0]) or proc.comm == "omp" or (
        first in ("bun", "node") and "oh-my-pi" in proc.args and _OMP_SOURCE.search(proc.args)
    ):
        one_shot = any(a in ("-p", "--print", "acp") or a.startswith("--mode=") for a in rest)
        return ("omp", not one_shot)
    if first == "claude" or proc.comm == "claude":
        return ("claude-code", not any(a in ("-p", "--print") for a in rest))
    if first == "codex" or proc.comm == "codex" or re.search(r"/codex(?:\.js)?$", argv[0]):
        return ("codex", not (rest[:1] == ["exec"] or rest[:1] == ["e"]))
    if first == "opencode" or proc.comm == "opencode":
        return ("opencode", rest[:1] != ["run"])
    return None


def chain_of(pid: int, by_pid: Dict[int, Proc], limit: int = 128) -> Tuple[int, ...]:
    """``pid, ppid, …`` up to pid 1 (included) or the first pid the census does not know."""
    out: List[int] = []
    cur: Optional[int] = pid
    while cur is not None and cur not in out and len(out) < limit:
        out.append(cur)
        proc = by_pid.get(cur)
        if proc is None or cur <= 1:
            break
        cur = proc.ppid
    return tuple(out)


def session_file_for(kind: str, paths: Iterable[str], sessions_root: Path) -> Optional[str]:
    """The host's own transcript among the files it holds open.

    omp keeps its main session ``sessions/<cwd-slug>/<ts>_<id>.jsonl`` open next to its
    subagents' ``sessions/<cwd-slug>/<ts>_<id>/<Name>.jsonl``; only the top-level one is the
    host's session. Claude Code and Codex transcripts are matched by their layout.
    """
    root = str(sessions_root).rstrip("/") + "/"
    for path in paths:
        if not path.endswith(".jsonl"):
            continue
        if kind == "omp" and path.startswith(root) and path[len(root):].count("/") == 1:
            return path
        if kind == "claude-code" and _CLAUDE_SESSION.search(path):
            return path
        if kind == "codex" and _CODEX_SESSION.search(path):
            return path
    return None


def session_id_from_file(kind: str, path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    stem = Path(path).stem
    if kind == "omp" and "_" in stem:
        return stem.split("_", 1)[1]
    if kind == "codex":
        m = re.search(r"([0-9a-f]{8}-[0-9a-f-]{27})$", stem)
        return m.group(1) if m else stem
    return stem


class Lineage:
    """Which agent host each process was seen under, remembered across censuses: the positive
    evidence an orphan needs.

    ``observe`` records every process of the census that has a live agent host above it (by pid
    AND start time, so a reused pid never inherits a record) and keeps the record of a process
    that has lost its host for as long as that very process lives. :meth:`dead_owner` then says
    whether the recorded host is gone. Kept in memory only: after a monitor restart nothing is an
    orphan until evidence is seen again, which errs towards never killing.
    """

    def __init__(self, start_time_of: Callable[[int], Optional[float]] = probe.proc_start_time) -> None:
        # pid → (its start time, host pid, host start time, host kind)
        self._seen: Dict[int, Tuple[float, int, float, str]] = {}
        self._start_time_of = start_time_of

    def observe(self, at: "Attributor") -> None:
        seen: Dict[int, Tuple[float, int, float, str]] = {}
        for pid in at.by_pid:
            start = at.starts.get(pid)
            if start is None:
                continue
            owner = at.host_above(pid)
            if owner is not None:
                host_start = at.starts.get(owner[0])
                if host_start is not None:
                    seen[pid] = (start, owner[0], host_start, owner[1])
                continue
            old = self._seen.get(pid)
            if old is not None and same_start(old[0], start):
                seen[pid] = old
        self._seen = seen

    def dead_owner(self, at: "Attributor", pid: int) -> Optional[str]:
        """``"<kind> pid <n>"`` when this very process was seen under an agent host that is gone
        now (its pid free, or held by a process with another start time). A host missing from the
        census is read live first: one census that missed a row must not orphan a live agent."""
        old = self._seen.get(pid)
        if old is None or not same_start(old[0], at.starts.get(pid)):
            return None
        _, host_pid, host_start, kind = old
        if host_pid in at.by_pid:
            now = at.starts.get(host_pid)
            if now is None or same_start(now, host_start):
                return None  # the host is alive, or its death cannot be proven
        elif same_start(self._start_time_of(host_pid), host_start):
            return None  # alive: the census just missed it
        return f"{kind} pid {host_pid}"


class Attributor:
    """Attribution over one census. Host details (cwd, open transcript) are resolved once per
    host and cached, so attributing hundreds of storm processes costs one libproc walk.

    ``starts`` are the processes' start times read when the census was taken: the pid-reuse
    guard compares against THAT process, not whatever holds the pid when a stale census is
    evaluated again. Without ``lineage`` nothing is ever an orphan."""

    def __init__(
        self,
        procs: Iterable[Proc],
        *,
        panes: Optional[Dict[int, str]] = None,
        launchd_pids: Optional[Set[int]] = None,
        my_uid: Optional[int] = None,
        sessions_root: Optional[Path] = None,
        starts: Optional[Dict[int, Optional[float]]] = None,
        lineage: Optional[Lineage] = None,
        cwd_of: Callable[[int], Optional[str]] = probe.proc_cwd,
        open_paths_of: Callable[[int], Optional[List[str]]] = probe.proc_open_paths,
        env_of: Callable[[int], Optional[Dict[str, str]]] = probe.proc_environ,
    ) -> None:
        self.by_pid: Dict[int, Proc] = {p.pid: p for p in procs}
        self.panes = panes or {}
        self.launchd_pids = launchd_pids
        self.my_uid = os.getuid() if my_uid is None else my_uid
        self.sessions_root = sessions_root or Path.home() / ".omp" / "agent" / "sessions"
        self.starts: Dict[int, Optional[float]] = dict(starts or {})
        self.lineage = lineage
        self._cwd_of = cwd_of
        self._open_paths_of = open_paths_of
        self._env_of = env_of
        self._hosts: Dict[int, AgentHost] = {}
        self._kinds: Dict[int, Optional[Tuple[str, bool]]] = {}
        self._surfaces: Dict[int, Optional[str]] = {}
        self._attributions: Dict[int, Attribution] = {}

    # ── agent hosts ───────────────────────────────────────────────────────────────────────
    def _kind(self, pid: int) -> Optional[Tuple[str, bool]]:
        if pid not in self._kinds:
            proc = self.by_pid.get(pid)
            self._kinds[pid] = classify_host(proc) if proc is not None else None
        return self._kinds[pid]

    def host(self, pid: int) -> Optional[AgentHost]:
        """The :class:`AgentHost` for ``pid`` when it is one (cached)."""
        if pid in self._hosts:
            return self._hosts[pid]
        kind = self._kind(pid)
        if kind is None:
            return None
        proc = self.by_pid[pid]
        paths = self._open_paths_of(pid) or []
        session_file = session_file_for(kind[0], paths, self.sessions_root)
        host = AgentHost(
            pid=pid,
            kind=kind[0],
            args=proc.args,
            cwd=self._cwd_of(pid),
            session_file=session_file,
            session_id=session_id_from_file(kind[0], session_file),
            pane=self.pane_of(chain_of(pid, self.by_pid)),
            interactive=kind[1],
            start_time=self.starts.get(pid),
        )
        self._hosts[pid] = host
        return host

    def hosts(self) -> List[AgentHost]:
        """Every agent host in the census."""
        return [h for h in (self.host(pid) for pid in self.by_pid if self._kind(pid)) if h]

    def host_above(self, pid: int) -> Optional[Tuple[int, str]]:
        """``(pid, kind)`` of the nearest agent host above ``pid`` (classification only, no libproc)."""
        for ancestor in chain_of(pid, self.by_pid)[1:]:
            kind = self._kind(ancestor)
            if kind is not None:
                return ancestor, kind[0]
        return None

    # ── panes ─────────────────────────────────────────────────────────────────────────────
    def pane_of(self, chain: Tuple[int, ...]) -> Optional[str]:
        """The tmux pane or cmux surface the chain runs in (tmux first: a tmux server started
        from a cmux surface passes that surface's ids on to every pane)."""
        for pid in chain:
            if pid in self.panes:
                return self.panes[pid]
        for i, pid in enumerate(chain):
            proc = self.by_pid.get(pid)
            if proc is not None and proc.comm == "cmux":
                # Top-down: every process of one surface shares its ids, so a 300-process storm
                # reads the environment of its few shared ancestors, not of every member.
                return next((s for s in (self._surface(p) for p in reversed(chain[:i])) if s), None) or "cmux"
        return None

    def _surface(self, pid: int) -> Optional[str]:
        """``cmux:<workspace-id>/<surface-id>`` from ``pid``'s environment (the ``login`` under
        cmux and its login shell have none: login resets it, so the walk goes on below)."""
        if pid not in self._surfaces:
            env = self._env_of(pid) or {}
            workspace, surface = env.get("CMUX_WORKSPACE_ID"), env.get("CMUX_SURFACE_ID")
            self._surfaces[pid] = f"cmux:{workspace}/{surface}" if workspace and surface else None
        return self._surfaces[pid]

    # ── one process ───────────────────────────────────────────────────────────────────────
    def protected_reason(self, proc: Proc, host: Optional[AgentHost]) -> Optional[str]:
        if proc.pid <= 1:
            return "pid 0/1"
        if proc.uid != self.my_uid:
            return "another user's process"
        if proc.pid == os.getpid():
            return "the monitor itself"
        if classify_host(proc) is not None:
            return "agent host (user-interactive)"
        if proc.comm in PROTECTED_COMMS or proc.comm.startswith("tmux"):
            return "terminal/multiplexer/session plumbing"
        if proc.comm in SHELL_COMMS and (proc.tty not in NO_TTY or proc.pid in self.panes):
            return "shell/interactive shell"
        if ".app/Contents/" in proc.args:
            return "GUI app"
        if proc.args.startswith(_SYSTEM_PREFIXES):
            return "system binary"
        if proc.foreground and host is None:
            return "user-interactive (foreground terminal job)"
        if self.launchd_pids is not None and proc.pid in self.launchd_pids:
            return "launchd job"
        return None

    def attribute(self, pid: int) -> Attribution:
        if pid in self._attributions:
            return self._attributions[pid]
        chain = chain_of(pid, self.by_pid)
        host = None
        for ancestor in chain[1:]:
            host = self.host(ancestor)
            if host is not None:
                break
        proc = self.by_pid.get(pid)
        reason = self.protected_reason(proc, host) if proc is not None else "not in the census"
        interactive = bool(reason) and (
            reason.startswith(("agent host", "shell", "terminal", "GUI", "user-interactive"))
        )
        root = self._orphan_root(chain) if host is None else None
        attribution = Attribution(
            pid=pid,
            chain=chain,
            host=host,
            pane=self.pane_of(chain),
            orphan=root is not None,
            dead_owner=root[1] if root is not None else None,
            interactive=interactive,
            protected_reason=reason,
        )
        self._attributions[pid] = attribution
        return attribution

    def orphan_root(self, pid: int) -> Optional[int]:
        """The root of the orphaned tree ``pid`` belongs to, or ``None`` when it is no orphan."""
        if not self.attribute(pid).orphan:
            return None
        root = self._orphan_root(chain_of(pid, self.by_pid))
        return root[0] if root is not None else None

    def _orphan_root(self, chain: Tuple[int, ...]) -> Optional[Tuple[int, str]]:
        """``(root pid, dead owner)``: the highest process of the chain that was seen under an
        agent host which has died since, unless that root is something never to widen to (a
        launchd job, a terminal, a GUI app, a system binary, another user's process). Unknown
        launchd jobs (``launchctl`` failed) → never an orphan."""
        if self.lineage is None or self.launchd_pids is None:
            return None
        found: Optional[Tuple[int, str]] = None
        for pid in chain:
            if pid <= 1:
                break
            owner = self.lineage.dead_owner(self, pid)
            if owner is not None:
                found = (pid, owner)
        if found is None:
            return None
        root = self.by_pid.get(found[0])
        if root is None or root.pid in self.launchd_pids or root.uid != self.my_uid:
            return None
        if root.comm in PROTECTED_COMMS or ".app/Contents/" in root.args or root.args.startswith(_SYSTEM_PREFIXES):
            return None
        return found
