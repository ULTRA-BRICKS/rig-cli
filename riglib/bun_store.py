"""bun worktree installs: the ``rig worktree create`` install and the ``rig status`` check for
worktree installs that are broken or bypass the global store.

Accessed via: :func:`riglib.cli._cmd_worktree_create` → :func:`install_decision` +
:func:`install_worktree_deps` (the GLOBAL ``bun.enabled`` + ``bun.worktree_install`` turn it on;
a repo rig.yaml can only turn it off); :func:`riglib.drift.compute_drift_report` →
:func:`check_worktree_installs` (when ``bun.enabled`` and ``rig status`` runs inside a git repo).

Assumptions: :mod:`riglib.bunfig` owns ``~/.bunfig.toml`` (``linker = "isolated"`` +
``globalStore = true``). Under it every ``node_modules/.bun/<pkg>@<ver>`` entry is a symlink into
``~/.bun/install/cache/links/`` and top-level ``node_modules/<pkg>`` links point into ``.bun/``,
so a worktree install is a few MB. bun still materialises some entries as real directories —
packages with lifecycle scripts (``esbuild``, ``nx``) — so a store-backed install is judged by the
MAJORITY of ``.bun`` entries, not by the absence of any directory (measured on the owner's Mac:
460 links + 20 dirs in a healthy tasks-app worktree; 0 links + 460 dirs in a store-bypassing one).

The store is NOT safe for every repo: in summary-bot and one private repo a same-version reinstall
through the store broke typecheck (TypeScript could not resolve peer-dependency types such as
``@types/react`` / ``bun-types`` through the real paths under ``links/``). So rig never forces it:
the install is a plain ``bun install --frozen-lockfile`` that honours the repo's own
``bunfig.toml`` (a pinned ``linker`` or ``globalStore = false`` wins over the global file), and a
non-store layout is only an informational ``rig status`` note, never drift. Only states that are
wrong in every repo are drift: ``node_modules`` (or ``node_modules/.bun``, or ``.bun`` entries)
linked into another checkout — ``bun install`` writes through them — and broken links into
``links/`` (the install no longer resolves).

Past bugs this exists for (2026-09): nothing installed deps in a new worktree, so agents
symlinked ``node_modules`` to the main checkout (``bun install`` then wrote through the link and
corrupted it), or ran full ``npm install`` copies; ``bun pm cache rm`` left installs with broken
links into ``links/``. The agent-tools ``package-store-guard`` hook blocks the commands; this
module removes the reason to improvise and makes the leftovers visible.
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeGuard

if TYPE_CHECKING:
    from .drift import DriftReport

from .bunfig import project_store_opt_out

BUN_LOCKFILES = ("bun.lock", "bun.lockb")
INSTALL_CMD = ("install", "--frozen-lockfile")
INSTALL_TIMEOUT_S = 900
_GIT_TIMEOUT_S = 30
_OUTPUT_TAIL_LINES = 12
_NOTE_LABELS_SHOWN = 4

InstallStatus = Literal["installed", "skipped", "failed"]
Decision = Literal["install", "off", "hint"]


@dataclass(frozen=True)
class InstallOutcome:
    """The result of :func:`install_worktree_deps`."""

    status: InstallStatus
    message: str


def is_bun_project(path: Path) -> bool:
    """True when ``path`` has a bun lockfile at its root."""
    return any((path / name).is_file() for name in BUN_LOCKFILES)


def install_decision(global_bun: Mapping[str, Any] | None, merged_bun: Mapping[str, Any] | None) -> Decision:
    """Whether ``rig worktree create`` installs deps.

    Only the GLOBAL layer can turn the install on (it runs the repo's install scripts, so a
    cloned repo's committed rig.yaml must not be able to opt a machine in); a repo rig.yaml may
    still turn it off with ``bun.worktree_install: false``. ``"hint"``: bun is not enabled on this
    machine, so only print the manual command.
    """
    if not _global_bun_enabled(global_bun):
        return "hint"
    if global_bun.get("worktree_install", True) is False:
        return "off"
    if isinstance(merged_bun, Mapping) and (
        merged_bun.get("worktree_install") is False or merged_bun.get("enabled") is False
    ):
        return "off"
    return "install"


def resolve_install_decision(repo_root: Path) -> Decision:
    """:func:`install_decision` over the real config layers (one load): the GLOBAL layer's own
    data alone decides "on", the merged view can only turn it off. Raises ``ConfigError``."""
    from .config import load

    loaded = load(repo_root, include_repo=True)
    return install_decision(loaded.global_data.get("bun"), loaded.data.get("bun"))


def worktree_check_enabled(global_bun: Mapping[str, Any] | None, merged_bun: Mapping[str, Any] | None) -> bool:
    """`rig status` scans bun worktrees under the same layer rule as the install: the GLOBAL
    ``bun.enabled`` turns it on (a cloned repo's rig.yaml must not make every clone's status red),
    a repo ``bun.enabled: false`` turns it off."""
    if not _global_bun_enabled(global_bun):
        return False
    return not (isinstance(merged_bun, Mapping) and merged_bun.get("enabled") is False)


def _global_bun_enabled(global_bun: Mapping[str, Any] | None) -> TypeGuard[Mapping[str, Any]]:
    """The one "on" switch for both the install and the status scan: the GLOBAL bun.enabled."""
    return isinstance(global_bun, Mapping) and global_bun.get("enabled") is True


def _q(path: Path | str) -> str:
    return shlex.quote(str(path))


def manual_command(worktree: Path) -> str:
    """The command to run by hand, paste-safe (a worktree path may hold spaces or `;`)."""
    return f"`cd {_q(worktree)} && bun install --frozen-lockfile`"


def _bun_install_root() -> Path:
    raw = os.environ.get("BUN_INSTALL", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".bun"


def store_links_dir() -> Path:
    """Where bun's global store keeps package trees: ``<cache>/links``."""
    raw = os.environ.get("BUN_INSTALL_CACHE_DIR", "").strip()
    cache = Path(raw).expanduser() if raw else _bun_install_root() / "install" / "cache"
    return cache / "links"


def find_bun() -> str | None:
    """``bun`` on PATH, else bun's own install location (``$BUN_INSTALL/bin`` / ``~/.bun/bin``)."""
    found = shutil.which("bun")
    if found:
        return found
    for name in ("bun", "bun.exe"):
        candidate = _bun_install_root() / "bin" / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _readlink(path: Path | str) -> str:
    try:
        return os.readlink(path)
    except OSError:
        return "?"


def _last_lines(text: str) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return "\n".join(lines[-_OUTPUT_TAIL_LINES:])


def write_through_message(link: Path, manual: str) -> str:
    """The one explanation for a node_modules / node_modules/.bun symlink, with the paste-safe fix."""
    return (
        f"{link} is a symlink to {_readlink(link)} — bun install would write through it into that "
        f"checkout. Remove the link (`rm {_q(link)}`), then run {manual}"
    )


def write_through_link(worktree: Path) -> Path | None:
    """``node_modules`` or ``node_modules/.bun`` when it is a symlink (installs write through it)."""
    node_modules = worktree / "node_modules"
    for candidate in (node_modules, node_modules / ".bun"):
        if candidate.is_symlink():
            return candidate
    return None


def _file_tail(fh, limit: int = 64 * 1024) -> str:
    fh.seek(0, os.SEEK_END)
    size = fh.tell()
    fh.seek(max(0, size - limit))
    return fh.read().decode("utf-8", errors="replace")


def _run_bounded(cmd: list[str], cwd: Path, timeout_s: int) -> tuple[int, str, str]:
    """Run ``cmd`` in its own process group with output to temp files (a flooding install script
    cannot exhaust rig's memory, and a lifecycle child holding the output cannot keep the wait
    open). On a timeout, Ctrl-C or any exception while waiting, the WHOLE group is killed, so no
    install script keeps writing node_modules or the store after rig gave up. Re-raises
    ``TimeoutExpired``; returns (exit code, stdout tail, stderr tail)."""
    posix = os.name == "posix"
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=out, stderr=err, stdin=subprocess.DEVNULL, start_new_session=posix)
        try:
            proc.wait(timeout=timeout_s)
        finally:
            if proc.poll() is None:
                try:
                    if posix:
                        os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        proc.kill()
                except OSError:
                    pass  # the group is already gone; the wait below reaps the child
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
        return proc.returncode, _file_tail(out), _file_tail(err)


def install_worktree_deps(worktree: Path, *, timeout_s: int = INSTALL_TIMEOUT_S) -> InstallOutcome:
    """Run ``bun install --frozen-lockfile`` in ``worktree`` exactly as the repo configures it.

    No linker/store flags: the repo's own ``bunfig.toml`` overrides the global one, so a repo that
    pins ``linker``/``globalStore = false`` (because the store breaks its typecheck) gets its own
    layout. Skips (never fails the create) when the global bunfig is not provisioned yet — run
    ``rig apply`` first — or bun is missing. Refuses a ``node_modules`` / ``node_modules/.bun``
    symlink: installing through it writes into whatever checkout it points at.
    """
    from . import bunfig

    manual = manual_command(worktree)
    # refuse a write-through link FIRST: every other outcome prints `bun install` for the user
    link = write_through_link(worktree)
    if link is not None:
        return InstallOutcome("failed", write_through_message(link, manual))
    state = bunfig.resolve()
    if state.state != "ok":
        if state.state == "conflict":  # apply never writes on conflict: the fix is by hand
            return InstallOutcome("skipped", f"not installing deps: {state.detail}; then run {manual}")
        problem = {
            "create": f"{state.path} does not set the isolated linker + globalStore yet",
            "update": f"the rig-managed block in {state.path} is stale",
        }.get(state.state, state.detail)
        return InstallOutcome("skipped", f"not installing deps: {problem} — run `rig apply`, then {manual}")
    bun = find_bun()
    if bun is None:
        return InstallOutcome("skipped", f"not installing deps: bun is not installed — install it, then run {manual}")
    started = time.monotonic()
    try:
        returncode, stdout, stderr = _run_bounded([bun, *INSTALL_CMD], worktree, timeout_s)
    except subprocess.TimeoutExpired:
        return InstallOutcome("failed", f"`bun install --frozen-lockfile` timed out after {timeout_s}s — re-run {manual}")
    except KeyboardInterrupt:
        # the install's process group is already dead (_run_bounded's finally)
        return InstallOutcome("failed", f"`bun install --frozen-lockfile` interrupted — re-run {manual}")
    except OSError as exc:
        return InstallOutcome("failed", f"could not run bun ({exc}) — run {manual}")
    if returncode != 0:
        detail = _last_lines(stderr) or _last_lines(stdout) or f"exit {returncode}"
        return InstallOutcome(
            "failed",
            f"`bun install --frozen-lockfile` failed (exit {returncode}) in {worktree}:\n{detail}\n"
            "Fix the cause and re-run it there (plain `bun install` only if you mean to change "
            "bun.lock). Never symlink node_modules or fall back to npm.",
        )
    took = time.monotonic() - started
    opt_out = project_store_opt_out(worktree)
    layout = f"as the repo configures: {opt_out}, so not through the global store" if opt_out else "isolated linker, global store"
    return InstallOutcome("installed", f"deps installed: bun install --frozen-lockfile ({layout}) in {took:.1f}s")


# ── rig status: broken or store-bypassing installs ────────────────────────────


@dataclass(frozen=True)
class InstallScan:
    """What :func:`scan_install` found. ``broken``: wrong in every repo (drift). ``off_store``:
    the install does not use the global store — informational, since a repo may need that."""

    broken: tuple[str, ...] = ()
    off_store: str | None = None


def _points_into_store(entry_path: str, link_target: str, links_dir: Path) -> bool:
    """Whether a ``.bun`` entry's link target lies inside the store's ``links/`` dir. A relative
    target is resolved from the entry's own directory, and both sides through realpath (macOS
    ``/var`` → ``/private/var``), so neither a custom cache dir nor ``links-evil`` fools it."""
    target = os.path.join(os.path.dirname(entry_path), link_target)
    real_target = os.path.realpath(target)
    for base in {os.path.normpath(str(links_dir)), os.path.realpath(links_dir)}:
        if real_target == base or real_target.startswith(base.rstrip(os.sep) + os.sep):
            return True
    return False

def _links_into_other_checkout(entry_path: str, worktree: Path) -> bool:
    """A ``.bun`` entry symlink whose real target is some OTHER tree's ``node_modules``."""
    real = os.path.realpath(entry_path)
    try:
        Path(real).relative_to(worktree.resolve())
        return False
    except ValueError:
        return "/node_modules/" in real.replace("\\", "/")


def scan_install(worktree: Path, links_dir: Path | None = None) -> InstallScan:
    """Classify ``worktree``'s ``node_modules`` against the global store."""
    links = links_dir if links_dir is not None else store_links_dir()
    node_modules = worktree / "node_modules"
    link = write_through_link(worktree)
    if link is not None:
        return InstallScan(broken=(write_through_message(link, manual_command(worktree)),))
    if not node_modules.is_dir():
        return InstallScan()
    opted_out = project_store_opt_out(worktree) is not None
    bun_dir = node_modules / ".bun"
    if not bun_dir.is_dir():
        if opted_out:
            return InstallScan()
        try:
            packages = [
                e for e in os.scandir(node_modules) if not e.name.startswith(".") and e.is_dir(follow_symlinks=False)
            ]
        except OSError:
            return InstallScan()
        return InstallScan(off_store=f"hoisted, {len(packages)} package dirs") if packages else InstallScan()
    linked = copied = broken = 0
    foreign: list[str] = []
    try:
        entries = list(os.scandir(bun_dir))
    except OSError:
        return InstallScan()
    for entry in entries:
        if entry.name.startswith(".") or entry.name == "node_modules":
            continue
        if entry.is_symlink():
            target = _readlink(entry.path)
            if _points_into_store(entry.path, target, links):
                linked += 1
                if not os.path.exists(entry.path):
                    broken += 1
            elif not os.path.exists(entry.path):
                broken += 1  # dangling into a moved or wiped older store
            elif _links_into_other_checkout(entry.path, worktree):
                foreign.append(entry.name)
        elif entry.is_dir(follow_symlinks=False):
            copied += 1
    problems: list[str] = []
    fix = f"fix: `rm -rf {_q(node_modules)}`, then {manual_command(worktree)}"
    if foreign:
        problems.append(
            f"{len(foreign)} node_modules/.bun entr{'y' if len(foreign) == 1 else 'ies'} link into another "
            f"checkout's node_modules (e.g. {foreign[0]}) — `bun install` writes through them; {fix}"
        )
    if broken:
        problems.append(
            f"{broken} broken link(s) in node_modules/.bun (store entries under {links} or an older "
            f"store are gone — a wiped cache?); {fix}"
        )
    off_store = f"{copied} of {copied + linked} packages copied" if copied > linked and not opted_out else None
    return InstallScan(broken=tuple(problems), off_store=off_store)


def repo_worktrees(repo_root: Path) -> list[Path] | None:
    """Every worktree git knows for ``repo_root`` (primary first), skipping ones gone from disk;
    None when the listing fails. Uses :mod:`riglib.worktree_gc`'s shared `-z` parser, so a path
    with a newline or a non-UTF-8 byte cannot hide a worktree."""
    from .worktree_gc import WorktreeGcError, _parse_worktree_record, _run_worktree_list_porcelain

    try:
        records = _run_worktree_list_porcelain(repo_root, timeout=_GIT_TIMEOUT_S)
    except WorktreeGcError:
        return None
    out: list[Path] = []
    for record in records:
        info = _parse_worktree_record(record)
        if info is not None and info.path.is_dir():
            out.append(info.path)
    return out


def _label(repo_root: Path, worktree: Path) -> str:
    try:
        rel = worktree.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return str(worktree)
    return str(rel) if str(rel) != "." else "(primary)"


def check_worktree_installs(repo_root: Path, report: DriftReport, *, merged_bun: Mapping[str, Any] | None = None) -> None:
    """Drift for broken bun worktree installs of ``repo_root``; one note for off-store ones.

    Drift items are ``extra`` (disk→config): an on-disk state no config declares, which apply
    never deletes — the item names the per-worktree fix instead.
    """
    from .drift import DriftItem, DriftNote

    links = store_links_dir()
    off_store: list[str] = []
    worktrees = repo_worktrees(repo_root)
    if worktrees is None:
        report.notes.append(DriftNote("bun", "worktrees", "bun worktree installs not checked: `git worktree list` failed"))
        return
    for worktree in worktrees:
        if not is_bun_project(worktree):
            continue
        scan = scan_install(worktree, links)
        label = _label(repo_root, worktree)
        for problem in scan.broken:
            report.items.append(DriftItem("extra", "bun", f"worktree:{label}", worktree, problem))
        if scan.off_store:
            off_store.append(f"{label} ({scan.off_store})")
    # a repo that opted out of the worktree install (it installs its own way) gets no note;
    # its broken states stay drift above
    opted_out = isinstance(merged_bun, Mapping) and merged_bun.get("worktree_install") is False
    if off_store and not opted_out:
        shown = ", ".join(off_store[:_NOTE_LABELS_SHOWN])
        if len(off_store) > _NOTE_LABELS_SHOWN:
            shown += f" … and {len(off_store) - _NOTE_LABELS_SHOWN} more"
        report.notes.append(
            DriftNote(
                "bun",
                "worktrees",
                f"{len(off_store)} bun worktree install(s) not using the global store: {shown} — "
                "informational; if the repo can use the store, `rm -rf node_modules && bun install "
                "--frozen-lockfile` there, else pin linker/globalStore in its bunfig.toml",
            )
        )
