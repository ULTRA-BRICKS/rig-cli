"""tgctl-reaper boot provisioning — PURE planning of the rig-managed periodic LaunchAgent.

## What this is

The durable fix for a real 2026-09-09 incident (agent-tools#697, OOM): tg-cli's ctl-omp-acp /
ctl-omp-reply integration tests spawn throwaway `omp acp` children that, when a test run is
interrupted, get reparented to launchd (ppid == 1) and sit idle at 0% CPU consuming RSS. A
periodic sweep (`riglib/tgctl_reaper_daemon.py`'s `sweep()`) SIGKILLs them once they are
provably orphaned (see that module's SAFETY section). Until now this sweep existed ONLY as a
manually-installed LaunchAgent (`agent-tools/ops/tgctl-omp-acp-reaper/install.sh`, run by hand
per machine) — reproducible on no machine that didn't happen to have someone run it, drift-
checked on none of them, and the marker regex it hardcoded could go stale with no test catching
it. This module makes it a first-class ``rig apply`` provisioned target, exactly like
``tg_ctl``/``tcc_watch``.

## Why a THIRD launchd shape (StartInterval), not RunAtLoad+KeepAlive or StartCalendarInterval

rig already provisions two launchd shapes:
  1. **RunAtLoad + KeepAlive** persistent daemons (:mod:`riglib.tg_ctl`, :mod:`riglib.tcc_watch`)
     — always-running background services.
  2. **StartCalendarInterval** fixed-daily-time cron (:mod:`riglib.schedule`) — runs once at a
     fixed HH:MM.

Neither matches "run every N seconds, then exit" — a periodic SWEEP, not a resident daemon and
not a once-daily cron. `StartInterval <seconds>` is launchd's own primitive for exactly this
(the same shape the manually-installed plist template already used: `StartInterval` 900 =
15 min). This module is the first rig-managed use of that plist key.

## How it is reached

``plan._build_tgctl_reaper`` reads the ``tgctl_reaper:`` config block, calls
:func:`build_tgctl_reaper` to resolve a :class:`TgctlReaperPlan`, and emits a
``provision_tgctl_reaper`` action. ``runner._do_provision_tgctl_reaper`` renders the plist +
copies the sweeper script from the same plan and writes + (re)loads it — mirroring
:mod:`riglib.tcc_watch`'s shape (byte-exact plist render, backup-on-conflict, gui-domain
bootstrap/bootout, ``RIG_TGCTL_REAPER_DRY_RUN`` + the shared sandboxed-HOME guard), with
``StartInterval``/``RunAtLoad: False`` in place of ``RunAtLoad: True``/``KeepAlive: True``
(a periodic sweep has nothing to keep alive between runs).

This is a per-MACHINE concern (one reaper per machine, not a repo concern), so the
``tgctl_reaper:`` block belongs in the GLOBAL layer (``~/.config/rig/config.yaml``), exactly
like ``tg_ctl``/``tcc_watch``/``tmux`` — NOT a committed repo ``rig.yaml``.

## Label continuity with the manually-installed predecessor

:data:`DEFAULT_LABEL` is ``com.hyperide.tgctl-omp-acp-reaper`` — the EXACT label the manual
`agent-tools/ops/tgctl-omp-acp-reaper/install.sh` already uses on any machine that ran it by
hand. Reusing it means a `rig apply` on such a machine RECOGNIZES the existing plist as the
SAME service (by label) rather than installing a second, colliding LaunchAgent under a
different name. The rendered plist's exact bytes still differ from the hand-installed one (this
module points the sweeper at a rig-managed script path under ``config_dir``, not the
`~/.local/share/agent-tools/...` path `install.sh` uses) — `rig apply`'s normal
backup-on-conflict path reconciles that difference on first apply, same as any other managed
artifact.

## What THIS module does not do

It does not change ``should_reap_orphan``'s safety predicates or the marker-regex prefix
enumeration — those live in, and are tested in, :mod:`riglib.tgctl_reaper_daemon` (the ported
logic). This module is planning + rendering ONLY, pure/effect-free (see :mod:`riglib.tcc_watch`'s
module docstring for the same split rationale).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

# The EXACT label the manually-installed predecessor uses (agent-tools/ops/tgctl-omp-acp-reaper),
# reused so a re-apply on a machine that already ran that install.sh by hand recognizes the SAME
# service rather than installing a colliding second LaunchAgent under a different name.
DEFAULT_LABEL = "com.hyperide.tgctl-omp-acp-reaper"
DEFAULT_CONFIG_DIR = "~/.config/rig/tgctl-reaper"

# 15 minutes — matches the manually-installed predecessor's StartInterval.
DEFAULT_INTERVAL_SECONDS = 900

OUT_LOG_NAME = "launchd.tgctl-reaper.out.log"
ERR_LOG_NAME = "launchd.tgctl-reaper.err.log"
SWEEPER_SCRIPT_NAME = "reap.py"

# Common python3 install locations, checked when PATH resolution fails (a non-interactive
# `rig apply` env may have a bare PATH) — mirrors tcc_watch's `_PYTHON3_FALLBACK_PATHS`.
_PYTHON3_FALLBACK_PATHS = ("/usr/bin/python3", "/opt/homebrew/bin/python3", "/usr/local/bin/python3")

# The PATH the daemon runs with. `{python_dir}` is the resolved python3's own directory so the
# daemon's `ps` subprocess call (a system binary) always resolves regardless of env.
_DAEMON_PATH_TEMPLATE = "{python_dir}:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"


@dataclass(frozen=True)
class TgctlReaperPlan:
    """The desired tgctl-reaper LaunchAgent state, fully resolved. Pure data, no I/O."""

    home: Path
    enabled: bool
    label: str
    interval_seconds: int
    config_dir: Path
    python_bin: Path

    # ── resolved artifact paths ──────────────────────────────────────────────────────
    @property
    def plist_path(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{self.label}.plist"

    @property
    def out_log_path(self) -> Path:
        return self.config_dir / OUT_LOG_NAME

    @property
    def err_log_path(self) -> Path:
        return self.config_dir / ERR_LOG_NAME

    @property
    def sweeper_script_path(self) -> Path:
        return self.config_dir / SWEEPER_SCRIPT_NAME

    @property
    def daemon_path_env(self) -> str:
        return _DAEMON_PATH_TEMPLATE.format(python_dir=str(self.python_bin.parent))

    # ── the generated LaunchAgent plist ──────────────────────────────────────────────
    def render_plist(self) -> str:
        """The rig-owned ``<label>.plist`` XML — a ``StartInterval`` periodic sweep, not a
        resident ``RunAtLoad``/``KeepAlive`` daemon: the sweep runs, exits, and launchd starts it
        again ``interval_seconds`` later.

        ``sort_keys=False`` (mirrors :func:`riglib.tcc_watch.TccWatchPlan.render_plist`) so key
        order is stable across renders — a re-apply against an identical desired plist must be a
        true byte-for-byte no-op, never a spurious rewrite.
        """
        import plistlib

        payload = {
            "Label": self.label,
            "ProgramArguments": [str(self.python_bin), str(self.sweeper_script_path)],
            "WorkingDirectory": str(self.config_dir),
            "EnvironmentVariables": {
                "PATH": self.daemon_path_env,
                "HOME": str(self.home),
            },
            "StartInterval": int(self.interval_seconds),
            "RunAtLoad": False,
            "StandardOutPath": str(self.out_log_path),
            "StandardErrorPath": str(self.err_log_path),
        }
        return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False).decode("utf-8")

    def render_sweeper_script(self) -> str:
        """The exact sweeper source (``riglib/tgctl_reaper_daemon.py``), copied byte-for-byte to
        ``sweeper_script_path`` so the installed periodic job runs the SAME code this module's
        own test suite exercises directly via ``import riglib.tgctl_reaper_daemon`` — one source
        of truth for the reap logic, no drift between tested behavior and deployed script.
        """
        from . import tgctl_reaper_daemon

        return Path(tgctl_reaper_daemon.__file__).read_text(encoding="utf-8")


def _resolve_python3(home: Path) -> Path:
    """The python3 binary for the LaunchAgent: prefer PATH, else the first existing common
    install location, else the first fallback path (present-or-not — the runner surfaces a
    missing interpreter as a launch failure via launchd's own error reporting, same contract
    as tcc_watch's python3 resolution)."""
    found = shutil.which("python3")
    if found:
        return Path(found)
    for candidate in _PYTHON3_FALLBACK_PATHS:
        path = Path(candidate)
        if path.is_file():
            return path
    return Path(_PYTHON3_FALLBACK_PATHS[0])


def _expand_home(p: str | Path, home: Path) -> Path:
    """Expand a ``~``/``~/...`` path against the resolved ``home`` (never the real one)."""
    s = str(p)
    if s == "~":
        return home
    if s.startswith("~/"):
        return home / s[2:]
    return Path(s)


def build_tgctl_reaper(
    *,
    home: Path,
    enabled: bool = True,
    label: str = DEFAULT_LABEL,
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    config_dir: str | Path = DEFAULT_CONFIG_DIR,
    python_bin: str | Path | None = None,
) -> TgctlReaperPlan:
    """Resolve the desired :class:`TgctlReaperPlan` from the (already-validated) tgctl_reaper
    config block. ``home`` is the resolved HOME (the caller passes ``Path.home()`` or a test tmp
    HOME).
    """
    resolved_python = _expand_home(python_bin, home) if python_bin else _resolve_python3(home)
    return TgctlReaperPlan(
        home=home,
        enabled=bool(enabled),
        label=str(label),
        interval_seconds=int(interval_seconds),
        config_dir=_expand_home(config_dir, home),
        python_bin=resolved_python,
    )
