"""Spotlight-exclude provisioning — PURE planning + the shared filesystem sweep.

What this is
------------
macOS Spotlight (``mds_stores``) reindexes everything under the user's home after a reboot,
including the gigabytes of dependency/build junk in every dev project (``node_modules``,
``dist``, ``target``, ``.venv`` …). The universal, per-directory opt-out on macOS is the
sentinel file ``.metadata_never_index``: an (empty) file with that exact name in a directory
tells Spotlight to skip that directory's contents — no global Search-Privacy list, no
``mdutil -i off`` (which would kill Spotlight wholesale). rig drops the sentinel into every
dependency/build dir under the configured dev roots, and provisions a launchd periodic job that
re-runs the sweep so NEW projects are covered without manual action.

How it is reached
-----------------
- ``plan._build_spotlight`` reads the ``spotlight:`` config block and emits ONE
  ``provision_spotlight`` action.
- ``runner._do_provision_spotlight`` runs :func:`perform_sweep` (drop sentinels) and writes +
  loads the launchd sweep agent (``RIG_SPOTLIGHT_DRY_RUN`` skips the live ``launchctl`` load).
- ``rig spotlight-sweep`` (the launchd job's command) calls the SAME :func:`perform_sweep`.
- ``verify._verify_spotlight`` and ``drift._check_spotlight`` both read back through the SAME
  :func:`scan_coverage` predicate, so ``rig apply``'s verify line and ``rig status`` can never
  contradict each other (rig-cli#375: verify sampled 20 dirs and passed while status scanned all
  2500 and reported 65 missing).

Design notes
------------
- Unlike the tmux/schedule pure modules (which render config artifacts written elsewhere), the
  filesystem SWEEP is this feature's core operation and has THREE effectful consumers (apply, the
  ``spotlight-sweep`` subcommand, and re-sweeps). Keeping :func:`perform_sweep` here is the single
  source of truth for the walk + drop logic, so the three can never disagree. The plist rendering
  and config resolution ARE pure. Import-time is stdlib-only (the AGENTS.md hard rule).
- The walk PRUNES matched dirs (never descends into a ``node_modules`` it just sentinelled — that
  is the whole point, and descending would be slow), and caps depth so a pathological tree can't
  wedge the sweep.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# The exact sentinel filename macOS Spotlight honors to skip a directory's contents.
SENTINEL_NAME = ".metadata_never_index"

# The dev roots swept by default. Configurable via ``spotlight.roots``; HOME-relative so a
# committed/global config stays portable.
DEFAULT_ROOTS: tuple[str, ...] = ("~/work", "~/xp")

# The dependency/build dir BASENAMES excluded by default — frontend AND backend. Config-driven:
# ``spotlight.deny`` REPLACES this set, ``spotlight.extra`` ADDS to it. Grouped by ecosystem for
# readability; membership is a flat basename match.
DEFAULT_DENY: tuple[str, ...] = (
    # frontend / node
    "node_modules", "dist", "build", ".next", "out", "coverage",
    ".turbo", ".nuxt", ".svelte-kit", ".parcel-cache", ".cache",
    # rust
    "target", ".cargo",
    # python
    ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    # go / php
    "vendor",
    # jvm / xcode
    ".gradle", "DerivedData",
)

# The launchd label / plist filename stem (reverse-DNS, Apple convention). One identity for the
# plist Label + filename so install/verify/remove all key off it. Distinct from the tmux-boot and
# model-freshness labels so the three agents never collide.
DEFAULT_BOOT_LABEL = "ai.hyperide.spotlight-exclude"

# The daily re-sweep time (launchd StartCalendarInterval). Early morning, off-peak.
DEFAULT_HOUR = 4
DEFAULT_MINUTE = 30

# A safety cap on the walk depth (levels below each root). Dependency dirs sit at or near a
# project root; matched dirs are pruned, so the remaining traversal is source trees — the cap only
# guards a pathological deep tree from wedging the sweep.
DEFAULT_MAX_DEPTH = 8

# Where the sweep records that it ran: the epoch second it STARTED plus the roots it covered.
# HOME-relative (rig's existing cache dir), so a HOME-isolated test never touches the real record.
# This timestamp is what lets every read-back consumer tell the two very different uncovered dirs
# apart — see :class:`Coverage`.
STATE_REL = ".cache/rig/spotlight-last-sweep.json"


@dataclass(frozen=True)
class SweepResult:
    """The outcome of one sweep. Pure data returned by :func:`perform_sweep`."""

    created: list[Path] = field(default_factory=list)  # sentinels newly written
    existing: list[Path] = field(default_factory=list)  # dirs that already had the sentinel
    roots_scanned: list[Path] = field(default_factory=list)  # roots that existed and were walked
    roots_missing: list[Path] = field(default_factory=list)  # configured roots absent on disk

    @property
    def matched(self) -> int:
        """Total dependency/build dirs matched (whether newly sentinelled or already covered)."""
        return len(self.created) + len(self.existing)

    def summary(self) -> str:
        n = len(self.roots_scanned)
        return (
            f"{len(self.created)} new, {len(self.existing)} already covered "
            f"across {n} {'root' if n == 1 else 'roots'}"
        )


@dataclass(frozen=True)
class Coverage:
    """How well the matched dirs are covered RIGHT NOW — the ONE predicate behind all three
    surfaces (the sweep's own report, the post-apply verify step, and ``rig status`` drift).

    The split is the whole point. A dependency/build dir with no sentinel is one of two very
    different things, and calling both "drift" made ``rig apply`` and ``rig status`` contradict
    each other on a busy machine (rig-cli#375):

    ``stale``    the last sweep VISITED this dir and it still has no sentinel — the sweep
                 genuinely failed to cover it (a permission error, or the sentinel was deleted
                 afterwards). This is real drift and fails verification.
    ``pending``  the last sweep never saw this dir, so it appeared afterwards — every `pytest` run
                 mints ``__pycache__`` dirs. Expected, not a failure: reported as an informational
                 note, and the next sweep covers it.

    "Visited" is read from the recorded dir identities of the last sweep (:class:`SweepRecord`),
    never inferred from a filesystem timestamp. With no usable record at all every uncovered dir is
    ``stale`` — a machine that never swept has no excuse. Invariant: ``pending`` is non-empty
    ONLY when a record was consulted (a dir can be "newer than the sweep" only relative to one).
    """

    covered: int
    stale: tuple[Path, ...]
    pending: tuple[Path, ...]

    @property
    def total(self) -> int:
        return self.covered + len(self.stale) + len(self.pending)


@dataclass(frozen=True)
class SpotlightPlan:
    """The desired Spotlight-exclude state, resolved. Pure data; no I/O to construct."""

    roots: tuple[Path, ...]
    deny: frozenset[str]
    sweep_cmd: tuple[str, ...]
    label: str
    hour: int
    minute: int
    max_depth: int
    plist_path: Path | None = None  # launchd only (macOS)
    log_path: Path | None = None  # launchd StandardOut/ErrorPath

    @property
    def human_time(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"

    def plist_xml(self) -> str:
        """The launchd plist XML: RunAtLoad + a daily StartCalendarInterval re-sweep, with
        StandardOut/ErrorPath logging from the START (the tmux-boot plist lacked logging and that
        cost a debugging session — this one ships it)."""
        import plistlib

        log = str(self.log_path or (Path.home() / "Library" / "Logs" / f"{self.label}.log"))
        payload = {
            "Label": self.label,
            "ProgramArguments": list(self.sweep_cmd),
            "RunAtLoad": True,
            "StartCalendarInterval": {"Hour": self.hour, "Minute": self.minute},
            "StandardOutPath": log,
            "StandardErrorPath": log,
        }
        return plistlib.dumps(payload, fmt=plistlib.FMT_XML).decode("utf-8")


def resolve_roots(roots_cfg: object) -> tuple[Path, ...]:
    """Expand the configured dev roots to absolute paths (default ``~/work``, ``~/xp``)."""
    raw = roots_cfg if isinstance(roots_cfg, list) and roots_cfg else list(DEFAULT_ROOTS)
    out: list[Path] = []
    seen: set[Path] = set()
    for entry in raw:
        p = Path(os.path.expanduser(str(entry)))
        if p not in seen:
            seen.add(p)
            out.append(p)
    return tuple(out)


def resolve_deny(deny_cfg: object, extra_cfg: object) -> frozenset[str]:
    """Resolve the excluded dir basenames: ``deny`` REPLACES the default set, ``extra`` ADDS."""
    if isinstance(deny_cfg, list) and deny_cfg:
        base = {str(x) for x in deny_cfg}
    else:
        base = set(DEFAULT_DENY)
    if isinstance(extra_cfg, list):
        base |= {str(x) for x in extra_cfg}
    return frozenset(base)


def default_sweep_cmd() -> tuple[str, ...]:
    """The argv the launchd job runs to re-sweep, pinned to the current interpreter.

    Mirrors :mod:`riglib.schedule` (which pins ``python3 <checker>``) so the job is independent
    of PATH/console-script resolution at boot time. Two install shapes exist and the argv has to
    work for both, because launchd runs it from ``/`` with no ``PYTHONPATH``:

    - the symlink-shim install (``install.sh`` links ``~/.local/bin/rig`` → ``<checkout>/bin/rig``,
      which puts the checkout on ``sys.path`` itself) → run THAT shim. ``-m riglib`` cannot see a
      checkout that is not on the interpreter's path, and it never could: on the reference machine
      the daily agent failed all 9 of its runs with ``No module named riglib`` (rig-cli#375), so an
      uncovered dir the sweep in ``rig apply`` never saw stayed uncovered forever.
    - a pip/pipx install (``riglib`` in site-packages, no sibling ``bin/rig``) → ``-m riglib``.

    The shim is located from THIS module's file, so the job re-enters whichever checkout produced
    the plan — the same one ``rig apply`` ran from.
    """
    shim = Path(__file__).resolve().parent.parent / "bin" / "rig"
    if shim.is_file():
        return (sys.executable, str(shim), "spotlight-sweep")
    return (sys.executable, "-m", "riglib", "spotlight-sweep")


def build_spotlight(
    *,
    roots: tuple[Path, ...],
    deny: frozenset[str],
    sweep_cmd: tuple[str, ...],
    label: str = DEFAULT_BOOT_LABEL,
    hour: int = DEFAULT_HOUR,
    minute: int = DEFAULT_MINUTE,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> SpotlightPlan:
    """Resolve the desired :class:`SpotlightPlan` for this machine (launchd on macOS)."""
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    log_path = Path.home() / "Library" / "Logs" / f"{label}.log"
    return SpotlightPlan(
        roots=roots,
        deny=deny,
        sweep_cmd=sweep_cmd,
        label=label,
        hour=hour,
        minute=minute,
        max_depth=max_depth,
        plist_path=plist_path,
        log_path=log_path,
    )


def sweep_args_from_options(options: dict) -> tuple[tuple[Path, ...], frozenset[str], int]:
    """Extract (roots, deny, max_depth) from an action's ``options`` bag.

    ONE decoder for the ``provision_spotlight`` action shape (``roots``: list[str], ``deny``:
    sorted list[str], ``max_depth``: int), shared by the runner (apply) and the verifier so the
    two can never drift on how they read the same action.
    """
    roots = tuple(Path(p) for p in options.get("roots", ()))
    deny = frozenset(options.get("deny", ()))
    max_depth = int(options.get("max_depth", DEFAULT_MAX_DEPTH))
    return roots, deny, max_depth


def sentinel_path(directory: Path) -> Path:
    """The sentinel file path for a directory."""
    return directory / SENTINEL_NAME


def has_sentinel(directory: Path) -> bool:
    """True when the directory already carries the Spotlight-skip sentinel file."""
    return sentinel_path(directory).is_file()


def iter_target_dirs(
    roots: tuple[Path, ...], deny: frozenset[str], max_depth: int = DEFAULT_MAX_DEPTH
) -> "list[Path]":
    """Return every dependency/build dir (basename in ``deny``) under the roots, read-only.

    PRUNES matched dirs (does not descend into a matched ``node_modules``) and caps traversal at
    ``max_depth`` levels below each root. Shared by the sweep, verify sampling, and dry-run
    reporting so all three see the SAME set. Never writes.
    """
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        root_depth = len(root.parts)
        for dirpath, dirnames, _files in os.walk(root):
            here = Path(dirpath)
            if len(here.parts) - root_depth >= max_depth:
                dirnames[:] = []
                continue
            matched = [d for d in dirnames if d in deny]
            for name in matched:
                found.append(here / name)
            # prune matched dirs (+ symlinked dirs, which os.walk would follow into cycles only
            # with followlinks=True — off by default — but pruning keeps the walk on real trees).
            dirnames[:] = [d for d in dirnames if d not in deny]
    return found


def state_path() -> Path:
    """Where the last-sweep record lives (``~/.cache/rig/spotlight-last-sweep.json``)."""
    return Path.home() / STATE_REL


@dataclass(frozen=True)
class SweepSelector:
    """WHAT a sweep matches: the roots it walks, the dir names it marks, and how deep it goes.

    The last-sweep record is keyed on the whole selector, not on the roots alone. Any of the
    three changing in config without a `rig apply` means the recorded sweep says nothing about
    the dirs the new selector matches — a `.venv` newly added to `deny` is "uncovered" because
    nothing ever swept it, not because it appeared since — so those dirs must count as real
    drift until a sweep actually runs (review round 1 on rig-cli#375 reproduced the excuse:
    change `deny`, skip apply, watch `rig status` file the unswept dirs as pending/in-sync).
    """

    roots: tuple[Path, ...]
    deny: frozenset[str]
    max_depth: int = DEFAULT_MAX_DEPTH

    def fingerprint(self) -> dict:
        """The order-insensitive identity the record is keyed on (reordering config changes
        nothing about what was swept)."""
        return {
            "roots": sorted(str(r) for r in self.roots),
            "deny": sorted(self.deny),
            "max_depth": int(self.max_depth),
        }


@dataclass(frozen=True)
class SweepRecord:
    """What the last sweep saw: when it started, and the matched dirs it actually visited.

    ``seen`` is the evidence that decides stale-vs-pending (see :class:`Coverage`): each visited
    dir's path mapped to its filesystem IDENTITY (``st_dev``, ``st_ino``). Recording the visited
    set rather than a timestamp is deliberate: dating a directory would mean trusting a filesystem
    birth time, which only macOS/BSD really have — everywhere else ``st_ctime`` is *inode-change*
    time, and deleting the sentinel out of a directory bumps its parent's ctime, so the one case
    this feature most needs to catch (a hand-deleted sentinel) would date as "created after the
    sweep" and be excused. Identity, not the bare pathname, because ``rm -rf dist && mkdir dist``
    is how builds work: the NEW ``dist`` at the old path was never swept — it is pending, not a
    sweep failure — and a fresh inode is what tells the two apart (PR #387 review). The same
    inode with no sentinel is the hand-deleted case: stale. (A filesystem that hands the freed
    inode straight back to the next ``mkdir`` — ext4 can — cannot tell the two apart and files
    the rebuilt dir as stale, which is no worse than the pathname-only behaviour; APFS, the
    Spotlight target, never reuses inode numbers.)
    """

    at: float
    seen: dict[str, tuple[int, int]]


def dir_identity(directory: Path) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` of a directory, or ``None`` when it cannot be stat'ed (vanished)."""
    try:
        st = os.stat(directory)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def identities_of(dirs: "list[Path]") -> dict[str, tuple[int, int]]:
    """The identity map for dirs that exist right now (a vanished dir is simply left out)."""
    out: dict[str, tuple[int, int]] = {}
    for d in dirs:
        ident = dir_identity(d)
        if ident is not None:
            out[str(d)] = ident
    return out


def record_sweep(
    selector: SweepSelector, started_at: float, seen: dict[str, tuple[int, int]]
) -> None:
    """Record a completed sweep: its selector, start time and every matched dir it visited, each
    with the identity it had AT THE VISIT (:func:`identities_of` / :func:`dir_identity`) —
    never re-stat'ed here, because a dir rebuilt while the rest of the tree was still being
    walked would otherwise be recorded under its NEW inode and read back as stale. Best-effort.

    Written atomically (temp file + ``os.replace``) because the launchd re-sweep job and an
    interactive ``rig status`` can touch this file at the same moment; a half-written file would
    read as "no sweep" and turn every uncovered dir into drift.

    A write failure is swallowed — losing the record only makes the next read strict (everything
    uncovered counts as stale), which is the safe direction to fail in.
    """
    path = state_path()
    payload = json.dumps(
        {
            "at": float(started_at),
            **selector.fingerprint(),
            "seen": {p: list(ident) for p, ident in seen.items()},
        }
    )
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def describe_age(at: float, now: float | None = None) -> str:
    """``at`` (epoch seconds) as a coarse human age: "just now" / "3 hours ago" / "2 days ago".

    Printed beside the pending-dir note so a re-sweep agent that silently stopped running is
    visible from ``rig status`` — verify checks that the agent is *loaded*, not that it ran.
    """
    seconds = max(0.0, (time.time() if now is None else now) - at)
    for unit, size in (("day", 86400.0), ("hour", 3600.0), ("minute", 60.0)):
        count = int(seconds // size)
        if count >= 1:
            return f"{count} {unit}{'s' if count != 1 else ''} ago"
    return "just now"


def _str_list(value: object) -> list[str]:
    """``value`` as a list of strings, or ``ValueError`` — a JSON string is iterable too, and
    iterating its characters would mint a nonsense record instead of rejecting it."""
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError("expected a list of strings")
    return value


def _seen_map(value: object) -> dict[str, tuple[int, int]]:
    """``value`` as ``{path: (dev, ino)}``, or ``ValueError`` on any other shape."""
    if not isinstance(value, dict):
        raise ValueError("expected an object")
    out: dict[str, tuple[int, int]] = {}
    for path, ident in value.items():
        if (
            not isinstance(path, str) or not isinstance(ident, list) or len(ident) != 2
            or not all(type(n) is int for n in ident)
        ):
            raise ValueError("expected {path: [dev, ino]}")
        out[path] = (ident[0], ident[1])
    return out


def last_sweep(selector: SweepSelector) -> SweepRecord | None:
    """The last sweep over EXACTLY this selector, or ``None`` when there is no usable record.

    The whole selector must match (see :class:`SweepSelector`): a record made for other roots,
    another denylist or another depth says nothing about the dirs this one matches, so reusing
    it would excuse dirs nothing ever visited. An unreadable, truncated, wrongly-typed or
    hand-edited record also reads as ``None`` — strict, never permissive.
    """
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        stored = {
            "roots": sorted(_str_list(data["roots"])),
            "deny": sorted(_str_list(data["deny"])),
            "max_depth": data["max_depth"],
        }
        if not isinstance(stored["max_depth"], int) or stored != selector.fingerprint():
            return None
        return SweepRecord(at=float(data["at"]), seen=_seen_map(data["seen"]))
    except (OSError, ValueError, TypeError, KeyError):
        return None


def scan_coverage(selector: SweepSelector, record: SweepRecord | None = None) -> Coverage:
    """Classify every matched dir as covered / stale-uncovered / pending-uncovered. Read-only.

    ``record`` is the last sweep (:func:`last_sweep`, for the SAME selector); ``None`` means no
    sweep is on record and every uncovered dir is stale. The walk (:func:`iter_target_dirs`) dominates the cost — the
    per-dir sentinel check is one extra ``stat`` each — so classifying the FULL set costs
    essentially the same as sampling it, which is why no consumer has any reason to sample.
    """
    covered = 0
    stale: list[Path] = []
    pending: list[Path] = []
    for directory in iter_target_dirs(selector.roots, selector.deny, selector.max_depth):
        if has_sentinel(directory):
            covered += 1
            continue
        identity = dir_identity(directory)
        if identity is None:
            continue  # vanished between the walk and this stat — nothing here to classify
        if record is not None and record.seen.get(str(directory)) != identity:
            # never visited, or visited and RECREATED since (`rm -rf dist && mkdir dist`): the
            # dir standing here now was never swept — pending. Same inode, no sentinel → stale.
            pending.append(directory)
        else:
            stale.append(directory)
    return Coverage(covered=covered, stale=tuple(stale), pending=tuple(pending))


def perform_sweep(
    roots: tuple[Path, ...], deny: frozenset[str], max_depth: int = DEFAULT_MAX_DEPTH
) -> SweepResult:
    """Drop the ``.metadata_never_index`` sentinel into every matched dir. Idempotent.

    Effectful (writes empty sentinel files). Returns a :class:`SweepResult` with per-dir outcomes.
    A dir that already has the sentinel is left untouched and counted as ``existing``. Records the
    run (:func:`record_sweep`) — including every dir it visited — so the read-back consumers can
    tell a dir this sweep missed from one that appeared after it.
    """
    started_at = time.time()
    created: list[Path] = []
    existing: list[Path] = []
    scanned: list[Path] = []
    missing: list[Path] = []
    for root in roots:
        (scanned if root.is_dir() else missing).append(root)
    visited: dict[str, tuple[int, int]] = {}
    for directory in iter_target_dirs(roots, deny, max_depth):
        # identity taken NOW, at the visit — a rebuild later in the walk must not be recorded
        identity = dir_identity(directory)
        if identity is None:
            continue  # vanished since the walk listed it
        visited[str(directory)] = identity
        sentinel = sentinel_path(directory)
        if sentinel.is_file():
            existing.append(sentinel)
            continue
        try:
            sentinel.touch()
            created.append(sentinel)
        except OSError:
            # A permission/IO error on one dir must not abort the whole sweep; skip it. The dir was
            # still VISITED, so it lands in the record and the next scan files it as STALE — the
            # failure surfaces as drift instead of disappearing.
            continue
    record_sweep(SweepSelector(roots, deny, max_depth), started_at, visited)
    return SweepResult(created=created, existing=existing, roots_scanned=scanned, roots_missing=missing)
