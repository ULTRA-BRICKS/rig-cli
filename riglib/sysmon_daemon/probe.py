"""Read the machine's state without forking where the OS allows it, and without task_for_pid.

Two cadences (see the package docstring):

- :func:`sample` — in-process syscalls only (``host_statistics64``, ``sysctlbyname``,
  ``proc_listallpids``, ``statvfs``; ``/proc`` on Linux). Cheap enough to run every few
  seconds; never spawns a process.
- :func:`census` — one ``ps`` snapshot of every process (two calls: the fixed columns and the
  argv), taken on a slower cadence or when a sample crosses a warn line.

Per-process details (cwd, open files, start time) come from ``libproc`` (``proc_pidinfo`` /
``proc_pidfdinfo``) on macOS and ``/proc`` on Linux. Both work for the caller's own processes
without ``task_for_pid``, so they keep working while a SecurityAgent debug prompt blocks
taskgated (the 2026-09-29 incident: htop, ``sample``, ``footprint`` and ``lsof -p`` all hung).
This module never calls ``lsof``, ``sample``, ``footprint``, ``vmmap`` or a debugger.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import functools
import os
import re
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .model import LaunchdJob, Proc

IS_DARWIN = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

GIB = 1024 ** 3


@dataclass(frozen=True)
class Sample:
    """One in-process reading. ``None`` means the platform does not expose the value."""

    taken_at: float
    ncpu: int
    load1: float
    load5: float
    load15: float
    mem_total: int
    wired: Optional[int]
    free: Optional[int]
    inactive: Optional[int]
    speculative: Optional[int]
    purgeable: Optional[int]
    reclaimable: Optional[int]  # free + inactive + speculative + purgeable (MemAvailable on Linux)
    compressor_pages: Optional[int]
    compressor_limit_pages: Optional[int]
    swap_total: int
    swap_used: int
    pressure_level: Optional[int]  # kern.memorystatus_vm_pressure_level: 1 normal, 2 warn, 4 critical
    proc_total: int
    proc_max: Optional[int]
    disk_free: Optional[int]  # bytes free on the volume that holds swap
    boot_id: Optional[str]

    @property
    def load_per_cpu(self) -> float:
        return self.load1 / max(1, self.ncpu)

    @property
    def wired_pct(self) -> Optional[float]:
        if self.wired is None or not self.mem_total:
            return None
        return 100.0 * self.wired / self.mem_total

    @property
    def swap_pct(self) -> Optional[float]:
        if not self.swap_total:
            return None
        return 100.0 * self.swap_used / self.swap_total

    @property
    def compressor_pct(self) -> Optional[float]:
        if not self.compressor_pages or not self.compressor_limit_pages:
            return None if self.compressor_limit_pages is None else 0.0
        return 100.0 * self.compressor_pages / self.compressor_limit_pages

    @property
    def procs_pct(self) -> Optional[float]:
        if not self.proc_max:
            return None
        return 100.0 * self.proc_total / self.proc_max

    def as_dict(self) -> Dict[str, object]:
        out: Dict[str, object] = {k: getattr(self, k) for k in self.__dataclass_fields__}
        for derived in ("load_per_cpu", "wired_pct", "swap_pct", "compressor_pct", "procs_pct"):
            out[derived] = getattr(self, derived)
        return out


# ── macOS: libSystem / libproc via ctypes ─────────────────────────────────────────────────
class _VMStatistics64(ctypes.Structure):
    """``vm_statistics64_data_t`` (mach/vm_statistics.h)."""

    _fields_ = (
        [(n, ctypes.c_uint32) for n in ("free_count", "active_count", "inactive_count", "wire_count")]
        + [
            (n, ctypes.c_uint64)
            for n in (
                "zero_fill_count", "reactivations", "pageins", "pageouts", "faults",
                "cow_faults", "lookups", "hits", "purges",
            )
        ]
        + [(n, ctypes.c_uint32) for n in ("purgeable_count", "speculative_count")]
        + [(n, ctypes.c_uint64) for n in ("decompressions", "compressions", "swapins", "swapouts")]
        + [
            (n, ctypes.c_uint32)
            for n in ("compressor_page_count", "throttled_count", "external_page_count", "internal_page_count")
        ]
        + [("total_uncompressed_pages_in_compressor", ctypes.c_uint64)]
    )


_HOST_VM_INFO64 = 4
_PROC_PIDLISTFDS = 1
_PROC_PIDFDVNODEPATHINFO = 2
_PROC_PIDTBSDINFO = 3
_PROC_PIDTASKINFO = 4
_PROC_PIDVNODEPATHINFO = 9
_PROX_FDTYPE_VNODE = 1
_VNODE_INFO_SIZE = 152  # struct vnode_info
_MAXPATHLEN = 1024
_PROC_FILEINFO_SIZE = 24  # struct proc_fileinfo
_BSDINFO_SIZE = 136  # struct proc_bsdinfo
_BSDINFO_PPID_OFFSET = 16  # pbi_ppid (after pbi_flags, pbi_status, pbi_xstatus, pbi_pid)
_BSDINFO_START_OFFSET = 120  # pbi_start_tvsec, pbi_start_tvusec (uint64 each)
_TASKINFO_SIZE = 96  # struct proc_taskinfo
_TASKINFO_PRIORITY_OFFSET = 92  # pti_priority, the task's base scheduling priority
#: MAXPRI_THROTTLE: the base priority of a task in the DARWIN_BG band. A nice-20 task sits at 11,
#: so a base priority at or below this is the background band, never a mere renice.
_DARWIN_BG_BASE_PRIORITY = 4
_CTL_KERN = 1
_KERN_PROCARGS2 = 49

_libc = None
_libproc = None


def _darwin_libs():
    global _libc, _libproc
    if _libc is None:
        _libc = ctypes.CDLL(None, use_errno=True)
        _libc.mach_host_self.restype = ctypes.c_uint32
        _libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    return _libc, _libproc


def _sysctl_raw(name: str) -> Optional[bytes]:
    libc, _ = _darwin_libs()
    size = ctypes.c_size_t(0)
    key = name.encode()
    if libc.sysctlbyname(key, None, ctypes.byref(size), None, 0) != 0 or size.value == 0:
        return None
    buf = ctypes.create_string_buffer(size.value)
    if libc.sysctlbyname(key, buf, ctypes.byref(size), None, 0) != 0:
        return None
    return buf.raw[: size.value]


def _sysctl_int(name: str) -> Optional[int]:
    raw = _sysctl_raw(name)
    if raw is None or len(raw) not in (4, 8):
        return None
    return int.from_bytes(raw, sys.byteorder, signed=False)


def _sysctl_str(name: str) -> Optional[str]:
    raw = _sysctl_raw(name)
    return raw.split(b"\0", 1)[0].decode("utf-8", "replace") if raw else None


def _darwin_sample(now: float) -> Sample:
    libc, libproc = _darwin_libs()
    stats = _VMStatistics64()
    count = ctypes.c_uint32(ctypes.sizeof(_VMStatistics64) // 4)
    kr = libc.host_statistics64(libc.mach_host_self(), _HOST_VM_INFO64, ctypes.byref(stats), ctypes.byref(count))
    page = _sysctl_int("vm.pagesize") or 16384
    vm_ok = kr == 0
    swap_total = swap_used = 0
    raw = _sysctl_raw("vm.swapusage")
    if raw and len(raw) >= 24:
        swap_total, _avail, swap_used = struct.unpack_from("<QQQ", raw)
    n = libproc.proc_listallpids(None, 0)
    proc_total = 0
    if n > 0:
        buf = (ctypes.c_int * (n + 256))()
        proc_total = max(0, libproc.proc_listallpids(buf, ctypes.sizeof(buf)))
    load1, load5, load15 = os.getloadavg()
    try:
        disk = os.statvfs("/System/Volumes/VM")
        disk_free: Optional[int] = disk.f_bavail * disk.f_frsize
    except OSError:
        disk_free = None

    def pages(v: int) -> Optional[int]:
        return v * page if vm_ok else None

    reclaimable = None
    if vm_ok:
        reclaimable = page * (
            stats.free_count + stats.inactive_count + stats.speculative_count + stats.purgeable_count
        )
    return Sample(
        taken_at=now,
        ncpu=os.cpu_count() or 1,
        load1=load1,
        load5=load5,
        load15=load15,
        mem_total=_sysctl_int("hw.memsize") or 0,
        wired=pages(stats.wire_count),
        free=pages(stats.free_count),
        inactive=pages(stats.inactive_count),
        speculative=pages(stats.speculative_count),
        purgeable=pages(stats.purgeable_count),
        reclaimable=reclaimable,
        compressor_pages=_sysctl_int("vm.compressor_segment_pages_compressed"),
        compressor_limit_pages=_sysctl_int("vm.compressor_segment_pages_compressed_limit"),
        swap_total=int(swap_total),
        swap_used=int(swap_used),
        pressure_level=_sysctl_int("kern.memorystatus_vm_pressure_level"),
        proc_total=proc_total,
        proc_max=_sysctl_int("kern.maxproc"),
        disk_free=disk_free,
        boot_id=_sysctl_str("kern.bootsessionuuid"),
    )


# ── Linux: /proc ──────────────────────────────────────────────────────────────────────────
def _meminfo(text: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for line in text.splitlines():
        m = re.match(r"^(\w+(?:\(\w+\))?):\s+(\d+)(?:\s+kB)?", line)
        if m:
            out[m.group(1)] = int(m.group(2)) * 1024
    return out


def _read(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def _linux_sample(now: float) -> Sample:
    mem = _meminfo(_read("/proc/meminfo") or "")
    load1, load5, load15 = os.getloadavg()
    try:
        proc_total = sum(1 for d in os.listdir("/proc") if d.isdigit())
    except OSError:
        proc_total = 0
    pid_max = (_read("/proc/sys/kernel/pid_max") or "").strip()
    try:
        disk = os.statvfs("/")
        disk_free: Optional[int] = disk.f_bavail * disk.f_frsize
    except OSError:
        disk_free = None
    swap_total = mem.get("SwapTotal", 0)
    return Sample(
        taken_at=now,
        ncpu=os.cpu_count() or 1,
        load1=load1,
        load5=load5,
        load15=load15,
        mem_total=mem.get("MemTotal", 0),
        wired=mem.get("Unevictable"),
        free=mem.get("MemFree"),
        inactive=mem.get("Inactive"),
        speculative=None,
        purgeable=None,
        reclaimable=mem.get("MemAvailable"),
        compressor_pages=None,
        compressor_limit_pages=None,
        swap_total=swap_total,
        swap_used=swap_total - mem.get("SwapFree", 0),
        pressure_level=None,
        proc_total=proc_total,
        proc_max=int(pid_max) if pid_max.isdigit() else None,
        disk_free=disk_free,
        boot_id=(_read("/proc/sys/kernel/random/boot_id") or "").strip() or None,
    )


def sample(now: Optional[float] = None) -> Sample:
    """One in-process reading of the machine (no subprocess)."""
    now = time.time() if now is None else now
    if IS_DARWIN:
        return _darwin_sample(now)
    return _linux_sample(now)


# ── ps census ─────────────────────────────────────────────────────────────────────────────
_PS_FIELDS = "pid=,ppid=,pgid=,uid=,tty=,stat=,etime=,rss=,%cpu=,comm="
_ROW = re.compile(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\d+)\s+([\d.,]+)\s+(.*)$")
_ARGS_ROW = re.compile(r"^\s*(\d+)\s(.*)$")


def parse_etime(etime: str) -> Optional[float]:
    """``ps -o etime`` (``[[dd-]hh:]mm:ss``) → seconds."""
    days = 0
    if "-" in etime:
        d, etime = etime.split("-", 1)
        if not d.isdigit():
            return None
        days = int(d)
    parts = etime.split(":")
    if not 1 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        return None
    total = 0
    for p in parts:
        total = total * 60 + int(p)
    return days * 86400.0 + total


def parse_census(fixed: str, args: str) -> List[Proc]:
    """Join the two ``ps`` outputs by pid. Malformed rows are skipped, never raised."""
    argv: Dict[int, str] = {}
    for line in args.splitlines():
        m = _ARGS_ROW.match(line)
        if m:
            argv[int(m.group(1))] = m.group(2).strip()
    procs: List[Proc] = []
    for line in fixed.splitlines():
        m = _ROW.match(line)
        if not m:
            continue
        etime = parse_etime(m.group(7))
        if etime is None:
            continue
        pid = int(m.group(1))
        comm = os.path.basename(m.group(10).strip()) or m.group(10).strip()
        procs.append(
            Proc(
                pid=pid,
                ppid=int(m.group(2)),
                pgid=int(m.group(3)),
                uid=int(m.group(4)),
                tty=m.group(5),
                stat=m.group(6),
                etime_s=etime,
                rss_kb=int(m.group(8)),
                cpu_pct=float(m.group(9).replace(",", ".")),
                comm=comm,
                args=argv.get(pid, comm),
            )
        )
    return procs


Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def _run(cmd: List[str], run: Runner, timeout: float) -> Optional[str]:
    try:
        proc = run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def census(run: Runner = subprocess.run, timeout: float = 10.0) -> Optional[List[Proc]]:
    """Every process on the machine, or ``None`` when ``ps`` failed (callers fail closed)."""
    fixed = _run(["ps", "-A", "-ww", "-o", _PS_FIELDS], run, timeout)
    argv = _run(["ps", "-A", "-ww", "-o", "pid=,args="], run, timeout)
    if fixed is None or argv is None:
        return None
    return parse_census(fixed, argv)


# ── per-process details (libproc / /proc; never task_for_pid) ─────────────────────────────
def proc_start_time(pid: int) -> Optional[float]:
    """Process start as epoch seconds, or ``None`` when the pid is gone/unreadable."""
    if IS_DARWIN:
        _, libproc = _darwin_libs()
        buf = ctypes.create_string_buffer(_BSDINFO_SIZE)
        if libproc.proc_pidinfo(pid, _PROC_PIDTBSDINFO, 0, buf, _BSDINFO_SIZE) != _BSDINFO_SIZE:
            return None
        sec, usec = struct.unpack_from("<QQ", buf.raw, _BSDINFO_START_OFFSET)
        return sec + usec / 1e6
    fields = _linux_stat_fields(pid)
    btime = _linux_btime()
    if fields is None or btime is None or len(fields) < 20:
        return None
    return btime + int(fields[19]) / os.sysconf("SC_CLK_TCK")


_btime: Optional[int] = None


def _linux_btime() -> Optional[int]:
    """Boot time (epoch seconds) from ``/proc/stat``; constant for the process lifetime."""
    global _btime
    if _btime is None:
        _btime = next(
            (int(line.split()[1]) for line in (_read("/proc/stat") or "").splitlines() if line.startswith("btime ")),
            None,
        )
    return _btime


def _linux_stat_fields(pid: int) -> Optional[List[str]]:
    """``/proc/<pid>/stat`` fields after the ``(comm)`` field (``fields[0]`` is the state)."""
    stat = _read(f"/proc/{pid}/stat")
    return stat.rsplit(")", 1)[-1].split() if stat else None


def proc_ppid(pid: int) -> Optional[int]:
    """The live parent pid, or ``None`` when the pid is gone/unreadable."""
    if IS_DARWIN:
        _, libproc = _darwin_libs()
        buf = ctypes.create_string_buffer(_BSDINFO_SIZE)
        if libproc.proc_pidinfo(pid, _PROC_PIDTBSDINFO, 0, buf, _BSDINFO_SIZE) != _BSDINFO_SIZE:
            return None
        return struct.unpack_from("<I", buf.raw, _BSDINFO_PPID_OFFSET)[0]
    fields = _linux_stat_fields(pid)
    return int(fields[1]) if fields and len(fields) > 1 else None


def proc_is_background(pid: int) -> Optional[bool]:
    """Whether ``pid`` runs in the macOS DARWIN_BG band (``taskpolicy -b``); ``None`` when unknown.

    ``getpriority(PRIO_DARWIN_PROCESS, pid)`` always answers 0 for another process, so this reads
    the task's base priority from ``proc_pidinfo(PROC_PIDTASKINFO)`` instead (no task port)."""
    if not IS_DARWIN:
        return None
    _, libproc = _darwin_libs()
    buf = ctypes.create_string_buffer(_TASKINFO_SIZE)
    if libproc.proc_pidinfo(pid, _PROC_PIDTASKINFO, 0, buf, _TASKINFO_SIZE) != _TASKINFO_SIZE:
        return None
    return struct.unpack_from("<i", buf.raw, _TASKINFO_PRIORITY_OFFSET)[0] <= _DARWIN_BG_BASE_PRIORITY


@functools.lru_cache(maxsize=1)
def _procargs_buffer() -> "ctypes.Array[ctypes.c_char]":
    """One ``kern.argmax``-sized KERN_PROCARGS2 buffer (fixed at boot), reused by every read:
    the daemon is single-threaded, and a fresh 1 MiB buffer per process read is pure churn."""
    return ctypes.create_string_buffer(_sysctl_int("kern.argmax") or 1 << 20)


def procargs2_environ(raw: bytes) -> Optional[List[bytes]]:
    """The ``KEY=value`` strings of a ``KERN_PROCARGS2`` payload: ``int argc``, the exec path,
    NUL padding, ``argc`` argv strings, then the environment. ``None`` when malformed."""
    if len(raw) < 4:
        return None
    argc = struct.unpack_from("<i", raw, 0)[0]
    if argc < 0:
        return None
    parts = raw[4:].split(b"\0")
    i = 1
    while i < len(parts) and not parts[i]:
        i += 1
    return parts[i + argc:]


def proc_environ(pid: int) -> Optional[Dict[str, str]]:
    """The environment ``pid`` was started with, or ``None`` when unreadable (another user's
    process, gone). macOS: ``sysctl(KERN_PROCARGS2)``, what ``ps -E`` reads; Linux: ``/proc``."""
    if IS_DARWIN:
        libc, _ = _darwin_libs()
        buf = _procargs_buffer()
        size = ctypes.c_size_t(len(buf))
        mib = (ctypes.c_int * 3)(_CTL_KERN, _KERN_PROCARGS2, pid)
        if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
            return None
        strings = procargs2_environ(ctypes.string_at(buf, size.value))
        if strings is None:
            return None
    else:
        try:
            with open(f"/proc/{pid}/environ", "rb") as fh:
                strings = fh.read().split(b"\0")
        except OSError:
            return None
    env: Dict[str, str] = {}
    for item in strings:
        key, sep, value = item.decode("utf-8", "replace").partition("=")
        if sep and key:
            env[key] = value
    return env


def proc_cwd(pid: int) -> Optional[str]:
    if IS_DARWIN:
        _, libproc = _darwin_libs()
        size = 2 * (_VNODE_INFO_SIZE + _MAXPATHLEN)
        buf = ctypes.create_string_buffer(size)
        if libproc.proc_pidinfo(pid, _PROC_PIDVNODEPATHINFO, 0, buf, size) <= 0:
            return None
        path = buf.raw[_VNODE_INFO_SIZE : _VNODE_INFO_SIZE + _MAXPATHLEN].split(b"\0", 1)[0]
        return path.decode("utf-8", "replace") or None
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


class _FdInfo(ctypes.Structure):
    _fields_ = [("proc_fd", ctypes.c_int32), ("proc_fdtype", ctypes.c_uint32)]


def proc_open_paths(pid: int) -> Optional[List[str]]:
    """Paths of the regular files/vnodes ``pid`` holds open, or ``None`` when unreadable."""
    if IS_DARWIN:
        _, libproc = _darwin_libs()
        need = libproc.proc_pidinfo(pid, _PROC_PIDLISTFDS, 0, None, 0)
        if need <= 0:
            return None
        fds = (_FdInfo * (need // ctypes.sizeof(_FdInfo) + 16))()
        got = libproc.proc_pidinfo(pid, _PROC_PIDLISTFDS, 0, fds, ctypes.sizeof(fds))
        if got <= 0:
            return None
        size = _PROC_FILEINFO_SIZE + _VNODE_INFO_SIZE + _MAXPATHLEN
        info = ctypes.create_string_buffer(size)
        paths: List[str] = []
        for i in range(got // ctypes.sizeof(_FdInfo)):
            if fds[i].proc_fdtype != _PROX_FDTYPE_VNODE:
                continue
            if libproc.proc_pidfdinfo(pid, fds[i].proc_fd, _PROC_PIDFDVNODEPATHINFO, info, size) <= 0:
                continue
            start = _PROC_FILEINFO_SIZE + _VNODE_INFO_SIZE
            raw = info.raw[start : start + _MAXPATHLEN].split(b"\0", 1)[0]
            if raw:
                paths.append(raw.decode("utf-8", "replace"))
        return paths
    try:
        entries = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return None
    paths = []
    for entry in entries:
        try:
            target = os.readlink(f"/proc/{pid}/fd/{entry}")
        except OSError:
            continue
        if target.startswith("/"):
            paths.append(target)
    return paths


# ── panes, launchd jobs, GPU slots ────────────────────────────────────────────────────────
def tmux_panes(
    tmux_bin: str = "tmux", socket: Optional[str] = None, run: Runner = subprocess.run
) -> Dict[int, str]:
    """``pane_pid → "tmux:<session>:<window>.<pane>"`` for every pane (empty when no server)."""
    cmd = [tmux_bin] + (["-L", socket] if socket else []) + [
        "list-panes", "-a", "-F", "#{pane_pid}\t#{session_name}:#{window_index}.#{pane_index}",
    ]
    out = _run(cmd, run, 3.0) or ""
    panes: Dict[int, str] = {}
    for line in out.splitlines():
        pid, _, target = line.partition("\t")
        if pid.isdigit() and target:
            panes[int(pid)] = f"tmux:{target}"
    return panes


def parse_launchctl_list(text: str) -> Dict[str, LaunchdJob]:
    """``label → LaunchdJob`` from ``launchctl list`` (``PID<TAB>Status<TAB>Label``, ``-`` = none)."""
    jobs: Dict[str, LaunchdJob] = {}
    for line in text.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 3 or not parts[2].strip():
            continue
        pid, status, label = (p.strip() for p in parts[:3])
        jobs[label] = LaunchdJob(label=label, pid=int(pid) if pid.isdigit() else None,
                                 status=int(status) if re.fullmatch(r"-?\d+", status) else None)
    return jobs


def parse_launchctl_print(job: LaunchdJob, text: str) -> LaunchdJob:
    """``job`` with the ``launchctl print`` fields filled in. Only TOP-level keys count (one tab
    deep): nested blocks such as event triggers repeat ``state`` for something else."""
    fields: Dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^\t([a-z][a-z ]*?) = (.*)$", line)
        if m and m.group(1) not in fields:
            fields[m.group(1)] = m.group(2).strip()

    def number(key: str) -> Optional[int]:
        m = re.match(r"^-?\d+", fields.get(key, ""))
        return int(m.group(0)) if m else None

    pid = number("pid")
    return replace(job, state=fields.get("state"), pid=pid if pid is not None else job.pid, runs=number("runs"),
                   last_exit_code=number("last exit code"), run_interval_s=number("run interval"))


def launchd_jobs(run: Runner = subprocess.run) -> Optional[Dict[str, LaunchdJob]]:
    """Every job launchd runs in the caller's domain (macOS; empty elsewhere), ``None`` when unknown."""
    if not IS_DARWIN:
        return {}
    out = _run(["launchctl", "list"], run, 5.0)
    return None if out is None else parse_launchctl_list(out)


def launchd_job_detail(job: LaunchdJob, run: Runner = subprocess.run) -> LaunchdJob:
    """``job`` with state, runs, last exit code and run interval from ``launchctl print``
    (unchanged when that fails: the job was unloaded since ``list``)."""
    out = _run(["launchctl", "print", f"gui/{os.getuid()}/{job.label}"], run, 5.0)
    return job if out is None else parse_launchctl_print(job, out)


def gpu_slots_held(slot_dir: Path, slots: int) -> Optional[int]:
    """How many agent-tools#817 GPU-model slots are held right now.

    Probes each ``slot-<i>.lock`` with a non-blocking ``flock`` and releases at once (the same
    probe the heavy-op memory gate uses); a missing file is a free slot. ``None`` when the
    directory is unreadable.
    """
    if not slot_dir.is_dir():
        return 0
    held = 0
    for i in range(max(0, slots)):
        path = slot_dir / f"slot-{i}.lock"
        try:
            fd = os.open(str(path), os.O_RDONLY)
        except FileNotFoundError:
            continue
        except OSError:
            return None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                held += 1
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    return held
