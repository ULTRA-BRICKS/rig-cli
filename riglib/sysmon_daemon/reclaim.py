"""Reclaimable-space candidates for a full data or swap volume (rig-cli#645).

REPORT ONLY. Whenever a disk finding alerts (``data_volume`` or ``disk``, any level; at most once
per ``alert.disk_cooldown_minutes`` per level), the monitor lists what could be removed and
how big it is, in the Telegram alert and the incident record, and a human decides. Nothing here
removes, moves, prunes or truncates anything: it lists directories, stats them and runs read-only
size queries (``du -sk -x``, ``container system df``, ``docker system df``).

Three sources:

- rig worktrees idle for ``reclaim.worktree_age_days``: ``<root>/<repo>/.worktrees/<name>`` (the
  ``rig worktree create`` layout) under each ``reclaim.worktree_roots`` dev root. Idle means
  neither the directory nor its git admin files (``index``, ``HEAD``, ``logs/HEAD``; every add,
  commit and checkout moves one of them) changed for that long.
- review-cli job caches (``reclaim.cache_dirs``): job records review-cli itself calls disposable.
- container images, only when the runtime's binary is on PATH: Apple ``container system df``
  and ``docker system df``, sized as what the images no container uses take.

Every command is bounded (``COMMAND_TIMEOUT_S`` each, ``SCAN_BUDGET_S`` in all). What the budget
leaves unmeasured is counted, never guessed. The daemon scans at most once per alert cooldown and
reuses the report until then (``daemon.Monitor._reclaim_lines``), never every tick.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import config
from .probe import GIB

# The scan runs on the monitor's own loop, so its budget is a sampling blind spot: this Mac's
# full scan (41 idle worktrees, the review-cli cache, `container system df`) took ~2 s on
# 2026-09-30, and 30 s caps the worst case well inside the minutes a memory crisis builds over.
COMMAND_TIMEOUT_S = 10.0
SCAN_BUDGET_S = 30.0
DAY = 86400.0
MIB = 1024 ** 2
NOTE = "REPORT ONLY — sysmon deleted nothing; remove what you no longer need yourself"

Runner = Callable[..., "subprocess.CompletedProcess[str]"]
Which = Callable[[str], Optional[str]]

#: git admin files of a linked worktree that every add/commit/checkout touches.
_GIT_ACTIVITY_FILES = ("index", "HEAD", "logs/HEAD")
#: ``docker system df`` sizes: go-units HumanSize, decimal (``1.2GB`` = 1.2e9 bytes).
_DOCKER_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(B|kB|KB|MB|GB|TB|PB)\b")
_DOCKER_UNITS = {"B": 1, "kB": 1e3, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12, "PB": 1e15}


def fmt_size(n: int) -> str:
    return f"{n / GIB:.1f} GB" if n >= GIB else f"{n / MIB:.0f} MB"


def _last_activity(worktree: Path) -> float:
    """Newest mtime of the worktree directory and its git admin files."""
    times = [worktree.stat().st_mtime]
    try:
        pointer = (worktree / ".git").read_text(encoding="utf-8", errors="replace")
    except OSError:
        pointer = ""
    if pointer.startswith("gitdir:"):
        admin = Path(pointer[len("gitdir:"):].strip())
        if not admin.is_absolute():
            admin = worktree / admin
        for name in _GIT_ACTIVITY_FILES:
            try:
                times.append((admin / name).stat().st_mtime)
            except OSError:
                pass
    return max(times)


def idle_worktrees(roots: Sequence[str], age_days: float, now: float) -> List[Tuple[str, float]]:
    """``(path, idle_seconds)`` of every ``<root>/<repo>/.worktrees/<name>`` directory idle for at
    least ``age_days``, oldest first (the budget measures the most likely abandoned first)."""
    out: List[Tuple[str, float]] = []
    for root in roots:
        try:
            repos = [e for e in os.scandir(config.expand(str(root))) if e.is_dir(follow_symlinks=False)]
        except OSError:
            continue
        for repo in repos:
            try:
                entries = list(os.scandir(os.path.join(repo.path, ".worktrees")))
            except OSError:
                continue
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                try:
                    idle = now - _last_activity(Path(entry.path))
                except OSError:
                    continue
                if idle >= age_days * DAY:
                    out.append((entry.path, idle))
    out.sort(key=lambda item: -item[1])
    return out


def _output(argv: List[str], run: Runner, timeout: float) -> Optional[str]:
    try:
        proc = run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout or ""


def du_bytes(path: str, run: Runner, timeout: float) -> Optional[int]:
    """``du -sk -x`` of ``path`` (one filesystem) in bytes, or ``None`` when it failed or ran out
    of time. A total printed despite unreadable subdirectories (exit 1) still counts."""
    out = _output(["du", "-sk", "-x", path], run, timeout)
    head = (out or "").split(None, 1)
    return int(head[0]) * 1024 if head and head[0].isdigit() else None


def _apple_images(binary: str, run: Runner, timeout: float) -> Optional[Dict[str, Any]]:
    try:
        images = json.loads(_output([binary, "system", "df", "--format", "json"], run, timeout) or "")["images"]
        unused, total = int(images["reclaimable"]), int(images["sizeInBytes"])
        count, active = int(images["total"]), int(images["active"])
    except (ValueError, TypeError, KeyError):
        return None
    if unused <= 0:
        return None
    return {"kind": "container-images", "path": "container", "size_bytes": unused,
            "detail": f"unused Apple container images ({count} images, {active} in use, {fmt_size(total)} in all)"}


def _docker_bytes(text: Any) -> Optional[int]:
    m = _DOCKER_SIZE.match(str(text or ""))
    return round(float(m.group(1)) * _DOCKER_UNITS[m.group(2)]) if m else None


def _docker_images(binary: str, run: Runner, timeout: float) -> Optional[Dict[str, Any]]:
    for line in (_output([binary, "system", "df", "--format", "{{json .}}"], run, timeout) or "").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("Type") != "Images":
            continue
        unused = _docker_bytes(row.get("Reclaimable"))
        if not unused:
            return None
        return {"kind": "docker-images", "path": "docker", "size_bytes": unused,
                "detail": f"unused Docker images ({row.get('TotalCount')} images, {row.get('Active')} in use, "
                          f"{row.get('Size')} in all)"}
    return None


def scan(
    cfg: Dict[str, Any],
    now: float,
    *,
    run: Runner = subprocess.run,
    which: Which = shutil.which,
    clock: Callable[[], float] = time.monotonic,
) -> Dict[str, Any]:
    """The report: the ``reclaim.max_candidates`` largest candidates (largest first), how many
    more there were and how big they are, and how many the time budget left unmeasured."""
    r = cfg["reclaim"]
    deadline = clock() + SCAN_BUDGET_S

    def time_left() -> float:
        return min(COMMAND_TIMEOUT_S, deadline - clock())

    found: List[Dict[str, Any]] = []
    for name, sizer in (("container", _apple_images), ("docker", _docker_images)):
        binary, budget = which(name), time_left()
        if binary and budget > 0:
            candidate = sizer(binary, run, budget)
            if candidate:
                found.append(candidate)
    todo = [("review-cache", str(p), "review-cli job cache")
            for p in (config.expand(str(d)) for d in r.get("cache_dirs") or ()) if p.is_dir()]
    todo += [("worktree", path, f"idle {idle / DAY:.0f} d")
             for path, idle in idle_worktrees(r.get("worktree_roots") or (), float(r["worktree_age_days"]), now)]
    unmeasured = 0
    for kind, path, detail in todo:
        budget = time_left()
        size = du_bytes(path, run, budget) if budget > 0 else None
        if size is None:
            unmeasured += 1
        else:
            found.append({"kind": kind, "path": path, "size_bytes": size, "detail": detail})
    found.sort(key=lambda c: -c["size_bytes"])
    cap = int(r["max_candidates"])
    rest = found[cap:]
    return {
        "at": now,
        "candidates": found[:cap],
        "more": len(rest),
        "more_bytes": sum(c["size_bytes"] for c in rest),
        "unmeasured": unmeasured,
        "note": NOTE,
    }


def _short(path: str) -> str:
    home = os.environ.get("HOME") or ""
    return "~" + path[len(home):] if home and path.startswith(home + os.sep) else path


def lines(report: Dict[str, Any], volume: Optional[str]) -> List[str]:
    """The report as plain alert lines (``alert.render`` escapes them)."""
    out = [f"reclaimable on {volume or 'the data volume'} ({report.get('note') or NOTE}):"]
    for c in report.get("candidates") or []:
        size = fmt_size(int(c["size_bytes"]))
        if c["kind"] == "worktree":
            out.append(f"  • {size} worktree {_short(c['path'])} ({c['detail']})")
        elif c["kind"] == "review-cache":
            out.append(f"  • {size} {c['detail']} {_short(c['path'])}")
        else:
            out.append(f"  • {size} {c['detail']}")
    if not report.get("candidates"):
        out.append("  (no candidates found)")
    if report.get("more"):
        out.append(f"  … {report['more']} more, {fmt_size(int(report.get('more_bytes') or 0))} in all")
    if report.get("unmeasured"):
        out.append(f"  {report['unmeasured']} more not measured (du failed or the {SCAN_BUDGET_S:.0f} s budget ran out)")
    return out
