"""tcc-watch boot provisioning — PURE planning of the rig-managed TCC dialog-watcher LaunchAgent.

## What this is

Defense-in-depth follow-up to the eval-tool TCC deny-list (``riglib/permissions.py``, rig-cli
PR #488). That deny-list PREVENTS an agent's eval-tool subprocess from touching the
osascript/System-Events surface that can trigger a macOS system permission dialog
(Photos/Contacts/Calendar/Screen-Recording/Full-Disk-Access/Automation/etc). Alex's follow-up
(quoted verbatim in the phased ticket this module implements) is that prevention alone is not a
permanent fix for "the fish" — a SECOND, independent layer is needed: something that watches for
the dialog itself, in case it appears anyway (a path the deny-list didn't anticipate, a
non-eval-tool subprocess, a already-granted-then-revoked edge case, etc), and reacts.

The full phased design is 4 phases; THIS module (plus ``riglib/tcc_watch_daemon.py``) is
**Phase 1 only — detection/observability**:

  1. **Detection** (THIS): tail macOS's ``log stream`` for the ``com.apple.TCC`` subsystem and
     durably log every permission-request-shaped event (timestamp + best-effort process
     attribution) to ``~/.config/rig/tcc-watch/events.jsonl``. Pure observation — no live-dialog
     interaction, no TCC.db access.
  2. **Auto-response** (deferred, ticketed, NOT implemented here): use the macOS Accessibility
     API, from this SAME privileged daemon, to locate the actual system consent-alert window and
     click "Don't Allow" — fail-closed default (always deny). Needs its own design review before
     landing: driving Accessibility against a live system UI element is a materially higher-risk
     action than reading a log stream, and a wrong AXUIElement match could interact with the
     wrong window.
  3. **Attribution** (deferred, ticketed): correlate the Phase-1 event's timestamp + requesting
     PID/bundle-id against the machine's own agent-process tree (reusing the ancestor-walk
     patterns already in this codebase, e.g. tg-ctl's own process-tree walk / rig-cli's harness
     listing — NOT a new discovery mechanism) to name which agent session's eval-tool/subprocess
     was active at that moment.
  4. **Notification** (deferred, ticketed): send the identified agent a hub/tg message
     explaining the denial and citing the specific access that was blocked.

## Why a separate privileged daemon, not the eval-tool sandbox

This is NOT the eval-tool's ``osascript``/System-Events surface (that surface stays banned for
the eval tool regardless — this module changes nothing about PR #488's deny-list). ``tcc-watch``
is a rig-provisioned, ALWAYS-ON, machine-wide LaunchAgent — started once at login, independent of
and outliving any individual agent session — exactly like ``tg-ctl`` (:mod:`riglib.tg_ctl`) and
the model-freshness ``schedule`` (:mod:`riglib.schedule`). It never runs inside, or is invoked
by, any agent's own eval-tool subprocess. The distinction that matters for the "is this
reintroducing the risk the deny-list closed?" question is WHO controls the process (a
rig/human-provisioned daemon vs. an agent-directed subprocess), not which OS surface it touches.
See :mod:`riglib.tcc_watch_daemon`'s module docstring for the detection mechanism's own
Apple-sanctioned-tooling grounding.

## How it is reached

``plan._build_tcc_watch`` reads the ``tcc_watch:`` config block, calls :func:`build_tcc_watch`
to resolve a :class:`TccWatchPlan`, and emits a ``provision_tcc_watch`` action.
``runner._do_provision_tcc_watch`` renders the plist + copies the watcher script from the same
plan and writes + (re)loads it — mirroring :mod:`riglib.tg_ctl`'s shape exactly (byte-exact
plist render, backup-on-conflict, gui-domain bootstrap/bootout, ``RIG_TCC_WATCH_DRY_RUN`` +
the shared sandboxed-HOME guard).

This is a per-MACHINE concern (one watcher daemon per machine), so the ``tcc_watch:`` block
belongs in the GLOBAL layer (``~/.config/rig/config.yaml``), like ``tg_ctl``/``tmux`` — NOT a
committed repo ``rig.yaml``.

## Known gap: no ``rig status`` drift line yet

Unlike ``tg_ctl``, this module does NOT (yet) register a :mod:`riglib.drift` check or a
:mod:`riglib.areas` entry — ``rig status`` will not show a dedicated "tcc-watch" GLOBAL line or
flag a deleted/corrupted plist as drift. `rig apply` still installs/repairs it correctly (the
install path re-renders + rewrites on every apply regardless of drift detection); only the
STANDALONE "is it currently drifted, without running apply" visibility is missing. Deliberately
left out of Phase 1's initial cut rather than shipping a status line that would claim "in sync"
with no real check behind it (worse than no line at all) — tracked as an explicit near-term
follow-up in the phased ticket, same severity tier as the drift/status parity `tg_ctl` already has.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LABEL = "ai.hyperide.tcc-watch"
DEFAULT_CONFIG_DIR = "~/.config/rig/tcc-watch"

OUT_LOG_NAME = "launchd.tcc-watch.out.log"
ERR_LOG_NAME = "launchd.tcc-watch.err.log"
EVENTS_LOG_NAME = "events.jsonl"
WATCHER_SCRIPT_NAME = "watch.py"

# Common python3 install locations, checked when PATH resolution fails (a non-interactive
# `rig apply` env may have a bare PATH) — mirrors tg_ctl's `_BUN_FALLBACK_PATHS`.
_PYTHON3_FALLBACK_PATHS = ("/usr/bin/python3", "/opt/homebrew/bin/python3", "/usr/local/bin/python3")

# The PATH the daemon runs with. `{python_dir}` is the resolved python3's own directory so the
# daemon's `log` subprocess call (a system binary) always resolves regardless of env.
_DAEMON_PATH_TEMPLATE = "{python_dir}:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"


@dataclass(frozen=True)
class TccWatchPlan:
    """The desired tcc-watch LaunchAgent state, fully resolved. Pure data, no I/O."""

    home: Path
    enabled: bool
    label: str
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
    def events_log_path(self) -> Path:
        return self.config_dir / EVENTS_LOG_NAME

    @property
    def watcher_script_path(self) -> Path:
        return self.config_dir / WATCHER_SCRIPT_NAME

    @property
    def daemon_path_env(self) -> str:
        return _DAEMON_PATH_TEMPLATE.format(python_dir=str(self.python_bin.parent))

    # ── the generated LaunchAgent plist ──────────────────────────────────────────────
    def render_plist(self) -> str:
        """The rig-owned ``ai.hyperide.tcc-watch.plist`` XML.

        ``sort_keys=False`` (mirrors :func:`riglib.tg_ctl.TgCtlPlan.render_plist`) so key order
        is stable across renders — a re-apply against an identical desired plist must be a true
        byte-for-byte no-op, never a spurious rewrite.
        """
        import plistlib

        payload = {
            "Label": self.label,
            "ProgramArguments": [str(self.python_bin), str(self.watcher_script_path), str(self.events_log_path)],
            "WorkingDirectory": str(self.config_dir),
            "EnvironmentVariables": {
                "PATH": self.daemon_path_env,
                "HOME": str(self.home),
            },
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 10,
            "StandardOutPath": str(self.out_log_path),
            "StandardErrorPath": str(self.err_log_path),
        }
        return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False).decode("utf-8")

    def render_watcher_script(self) -> str:
        """The exact watcher source (``riglib/tcc_watch_daemon.py``), copied byte-for-byte to
        ``watcher_script_path`` so the installed daemon runs the SAME code this module's own
        test suite exercises directly via ``import riglib.tcc_watch_daemon`` — one source of
        truth for the detection logic, no drift between tested behavior and deployed script.
        """
        from . import tcc_watch_daemon

        return Path(tcc_watch_daemon.__file__).read_text(encoding="utf-8")


def _resolve_python3(home: Path) -> Path:
    """The python3 binary for the LaunchAgent: prefer PATH, else the first existing common
    install location, else the first fallback path (present-or-not — the runner surfaces a
    missing interpreter as a launch failure via launchd's own error reporting, same contract
    as tg_ctl's bun resolution)."""
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


def build_tcc_watch(
    *,
    home: Path,
    enabled: bool = True,
    label: str = DEFAULT_LABEL,
    config_dir: str | Path = DEFAULT_CONFIG_DIR,
    python_bin: str | Path | None = None,
) -> TccWatchPlan:
    """Resolve the desired :class:`TccWatchPlan` from the (already-validated) tcc_watch config
    block. ``home`` is the resolved HOME (the caller passes ``Path.home()`` or a test tmp HOME).
    """
    resolved_python = _expand_home(python_bin, home) if python_bin else _resolve_python3(home)
    return TccWatchPlan(
        home=home,
        enabled=bool(enabled),
        label=str(label),
        config_dir=_expand_home(config_dir, home),
        python_bin=resolved_python,
    )
