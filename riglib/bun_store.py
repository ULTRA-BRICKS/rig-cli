"""bun worktree installs through the global store: the ``rig worktree create`` install and the
``rig status`` check for installs that bypass the store.

Accessed via: :func:`riglib.cli._cmd_worktree_create` → :func:`install_worktree_deps` (when
``bun.enabled`` and ``bun.worktree_install`` are on and the new tree has a ``bun.lock``);
:func:`riglib.drift.compute_drift_report` → :func:`check_worktree_installs` (when ``bun.enabled``
and ``rig status`` runs inside a git repo).

Assumptions: :mod:`riglib.bunfig` owns ``~/.bunfig.toml`` (``linker = "isolated"`` +
``globalStore = true``). Under it every ``node_modules/.bun/<pkg>@<ver>`` entry is a symlink into
``~/.bun/install/cache/links/`` and top-level ``node_modules/<pkg>`` links point into ``.bun/``,
so a worktree install is a few MB. bun still materialises some entries as real directories —
packages with lifecycle scripts (``esbuild``, ``nx``) — so a store-backed install is judged by the
MAJORITY of ``.bun`` entries, not by the absence of any directory (measured on the owner's Mac:
460 links + 20 dirs in a healthy tasks-app worktree; 0 links + 460 dirs in a store-bypassing one).

Past bugs this exists for (2026-09): nothing installed deps in a new worktree, so agents
symlinked ``node_modules`` to the main checkout (``bun install`` then wrote through the link and
corrupted it), or ran full ``npm install`` copies; ``bun pm cache rm`` left installs with broken
links into ``links/``. The agent-tools ``package-store-guard`` hook blocks the commands; this
module removes the reason to improvise and makes the leftovers visible.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

BUN_LOCKFILES = ("bun.lock", "bun.lockb")
INSTALL_CMD = ("install", "--frozen-lockfile")
INSTALL_TIMEOUT_S = 900
_GIT_TIMEOUT_S = 30
_STDERR_TAIL_LINES = 12

_HOISTED_LINE_RE = re.compile(r"""^linker\s*=\s*["']hoisted["']\s*(?:#.*)?$""")
_DOTTED_HOISTED_RE = re.compile(r"""^install\.linker\s*=\s*["']hoisted["']\s*(?:#.*)?$""")


@dataclass(frozen=True)
class InstallOutcome:
    """The result of :func:`install_worktree_deps`. ``status``: installed | skipped | failed."""

    status: str
    message: str


def is_bun_project(path: Path) -> bool:
    """True when ``path`` has a bun lockfile at its root."""
    return any((path / name).is_file() for name in BUN_LOCKFILES)


def repo_pins_hoisted(path: Path) -> bool:
    """True when the project's own ``bunfig.toml`` sets ``[install] linker = "hoisted"``."""
    from .bunfig import _in_install_matches, _iter_nonempty_lines

    cfg = path / "bunfig.toml"
    try:
        text = cfg.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    if any(_DOTTED_HOISTED_RE.match(line) for line in _iter_nonempty_lines(text)):
        return True
    return _in_install_matches(text, _HOISTED_LINE_RE)


def worktree_install_enabled(bun_cfg: object) -> bool:
    """``bun.enabled: true`` and ``bun.worktree_install`` not explicitly ``false``."""
    if not isinstance(bun_cfg, dict) or bun_cfg.get("enabled") is not True:
        return False
    return bun_cfg.get("worktree_install", True) is not False


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


def _tail(text: str) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return "\n".join(lines[-_STDERR_TAIL_LINES:])


def install_worktree_deps(worktree: Path, *, timeout_s: int = INSTALL_TIMEOUT_S) -> InstallOutcome:
    """Run ``bun install --frozen-lockfile`` in ``worktree`` — only through the global store.

    Skips (never fails the create) when the global bunfig is not provisioned — an install then
    would be a full copy, so the fix is ``rig apply`` first — or when bun is not installed.
    Refuses a ``node_modules`` symlink outright: installing through it writes into whatever
    checkout it points at.
    """
    from . import bunfig

    manual = f"`bun install --frozen-lockfile` in {worktree}"
    node_modules = worktree / "node_modules"
    if node_modules.is_symlink():
        return InstallOutcome(
            "failed",
            f"{node_modules} is a symlink to {os.readlink(node_modules)} — bun install would write "
            f"through it into that checkout. Remove the link (`rm {node_modules}`), then run {manual}",
        )
    state = bunfig.resolve()
    if state.state != "ok":
        return InstallOutcome(
            "skipped",
            f"not installing deps: {state.path} does not set the isolated linker + globalStore "
            f"({state.detail}) — run `rig apply`, then {manual}",
        )
    bun = find_bun()
    if bun is None:
        return InstallOutcome("skipped", f"not installing deps: bun is not installed — install it, then run {manual}")
    started = time.monotonic()
    try:
        res = subprocess.run(
            [bun, *INSTALL_CMD],
            cwd=worktree,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return InstallOutcome("failed", f"`bun install --frozen-lockfile` timed out after {timeout_s}s — re-run {manual}")
    except OSError as exc:
        return InstallOutcome("failed", f"could not run bun ({exc}) — run {manual}")
    if res.returncode != 0:
        detail = _tail(res.stderr) or _tail(res.stdout) or f"exit {res.returncode}"
        return InstallOutcome(
            "failed",
            f"`bun install --frozen-lockfile` failed (exit {res.returncode}) in {worktree}:\n{detail}\n"
            "Fix the cause and re-run it there (plain `bun install` only if you mean to change "
            "bun.lock). Never symlink node_modules or fall back to npm.",
        )
    took = time.monotonic() - started
    layout = "isolated linker, global store"
    if repo_pins_hoisted(worktree):
        layout = "repo bunfig.toml pins linker = \"hoisted\", so the global store does not apply"
    return InstallOutcome("installed", f"deps installed: bun install --frozen-lockfile ({layout}) in {took:.1f}s")


# ── rig status: installs that bypass the store ────────────────────────────────


def _points_into_store(link_target: str, links_dir: Path) -> bool:
    norm = link_target.replace("\\", "/")
    return norm.startswith(str(links_dir).replace("\\", "/")) or "/install/cache/links/" in norm


def scan_install(worktree: Path, links_dir: Path | None = None) -> list[str]:
    """Problems with ``worktree``'s ``node_modules`` relative to the global store (empty = fine)."""
    links = links_dir if links_dir is not None else store_links_dir()
    node_modules = worktree / "node_modules"
    if node_modules.is_symlink():
        return [
            f"node_modules is a symlink to {os.readlink(node_modules)} — `bun install` here writes "
            f"through it into that checkout; fix: `rm {node_modules}` then "
            f"`bun install --frozen-lockfile` in {worktree}"
        ]
    if not node_modules.is_dir():
        return []
    fix = f"fix: `rm -rf {node_modules}` then `bun install --frozen-lockfile` in {worktree}"
    bun_dir = node_modules / ".bun"
    if not bun_dir.is_dir():
        try:
            packages = [
                e for e in os.scandir(node_modules) if not e.name.startswith(".") and e.is_dir(follow_symlinks=False)
            ]
        except OSError:
            return []
        if packages and not repo_pins_hoisted(worktree):
            return [
                f"hoisted node_modules ({len(packages)} package dirs, no node_modules/.bun) although the "
                f"repo does not pin linker = \"hoisted\" — a full copy outside the global store; {fix}"
            ]
        return []
    linked = copied = broken = 0
    try:
        entries = list(os.scandir(bun_dir))
    except OSError:
        return []
    for entry in entries:
        if entry.name.startswith(".") or entry.name == "node_modules":
            continue
        if entry.is_symlink():
            try:
                target = os.readlink(entry.path)
            except OSError:
                continue
            if not _points_into_store(target, links):
                continue
            linked += 1
            if not os.path.exists(entry.path):
                broken += 1
        elif entry.is_dir(follow_symlinks=False):
            copied += 1
    problems: list[str] = []
    if broken:
        problems.append(
            f"{broken} broken link(s) from node_modules/.bun into {links} (store entries gone — "
            f"a wiped cache?); {fix}"
        )
    if copied > linked:
        problems.append(
            f"{copied} of {copied + linked} node_modules/.bun entries are real directories, not links "
            f"into the global store — the install bypassed it; {fix}"
        )
    return problems


def repo_worktrees(repo_root: Path) -> list[Path]:
    """Every worktree git knows for ``repo_root`` (primary first), skipping ones gone from disk."""
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if res.returncode != 0:
        return []
    out: list[Path] = []
    for line in res.stdout.splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree ") :])
            if path.is_dir():
                out.append(path)
    return out


def _label(repo_root: Path, worktree: Path) -> str:
    try:
        rel = worktree.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return str(worktree)
    return str(rel) if str(rel) != "." else "(primary)"


def check_worktree_installs(repo_root: Path, report) -> None:
    """Append a ``bun`` drift item per worktree of ``repo_root`` whose install bypasses the store."""
    from .drift import DriftItem

    links = store_links_dir()
    for worktree in repo_worktrees(repo_root):
        if not is_bun_project(worktree):
            continue
        for problem in scan_install(worktree, links):
            report.items.append(
                DriftItem("modified", "bun", f"worktree:{_label(repo_root, worktree)}", worktree, problem)
            )
