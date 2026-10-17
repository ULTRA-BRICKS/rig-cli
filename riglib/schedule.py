"""Model-freshness schedule — PURE planning of the daily cron artifact.

The CTO's #3685 direction: rig should install a cron that runs the agent-tools model-currency
checker **once a day, e.g. at noon**, and on `rig init` AND `rig apply` should **check whether
the cron is installed and install it if missing**.

Cross-platform:
  - **macOS → launchd**. A ``~/Library/LaunchAgents/<label>.plist`` loaded via ``launchctl``
    is the native, supported "cron" on macOS (cron is deprecated/unmanaged there).
  - **Linux → systemd --user timer**. A ``<label>.service`` (oneshot) + ``<label>.timer``
    (``OnCalendar`` daily, ``Persistent=true``) under ``~/.config/systemd/user``. Not crontab:
    agent shells run with ``NoNewPrivs=1``, so the setgid ``crontab`` binary cannot read or write
    the spool there (rig-cli#721) — and ``rig apply`` is normally launched by an agent. A user
    timer needs no privilege, shows in ``systemctl --user list-timers`` and logs to the journal.
    Earlier rig versions wrote a sentinel-fenced crontab line; apply removes it when readable.

This module is **stdlib-only and effect-free**: it computes WHAT the artifact should be (the
plist XML / the unit files, the install paths, the platform branch). The effectful
``launchctl load`` / ``systemctl --user`` calls live in ``actions/runner.py`` (the one-engine apply
path), and drift detection diffs the desired artifact against disk in ``drift.py``. Three
consumers (plan, apply, drift) share THIS so the desired state never drifts between them.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

# The default daily run time — NOON, per the CTO. Hour/minute the launchd plist + systemd timer
# both encode. Override via rig.yaml ``models.schedule.time: "HH:MM"``.
DEFAULT_HOUR = 12
DEFAULT_MINUTE = 0

# The launchd label / sentinel identity. Reverse-DNS per Apple convention; the SAME string is
# the plist Label, the plist filename stem, and the systemd unit stem — one identity across
# platforms so install/drift/remove all key off it.
DEFAULT_LABEL = "ai.hyperide.model-freshness"

# The sentinel comment earlier rig versions fenced their crontab line with (comment line + the
# cron line). Kept only so apply can find and remove that legacy pair, never a user's line.
CRON_SENTINEL_PREFIX = "# rig-managed:"


@dataclass(frozen=True)
class SchedulePlan:
    """The desired daily-schedule state, platform-resolved. Pure data, no I/O.

    ``platform`` is "launchd" (macOS) or "systemd" (Linux/other). ``checker_cmd`` is the
    fully-resolved argv the schedule runs (``python3 .../lib/checker/model_freshness.py``).
    """

    platform: str  # "launchd" | "systemd"
    label: str
    hour: int
    minute: int
    checker_cmd: list[str]
    plist_path: Path | None = None  # launchd only
    log_path: Path | None = None  # launchd only (StandardOut/ErrorPath)
    unit_dir: Path | None = None  # systemd only (~/.config/systemd/user)

    @property
    def service_name(self) -> str:
        return f"{self.label}.service"

    @property
    def timer_name(self) -> str:
        return f"{self.label}.timer"

    @property
    def human_time(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"

    # ── launchd artifact ──────────────────────────────────────────────────────────────
    def plist_xml(self) -> str:
        """The launchd plist XML for a daily StartCalendarInterval run at hour:minute."""
        args = "".join(f"    <string>{_xml_escape(a)}</string>\n" for a in self.checker_cmd)
        log = self.log_path or (Path.home() / "Library" / "Logs" / f"{self.label}.log")
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n'
            "<dict>\n"
            "  <key>Label</key>\n"
            f"  <string>{_xml_escape(self.label)}</string>\n"
            "  <key>ProgramArguments</key>\n"
            "  <array>\n"
            f"{args}"
            "  </array>\n"
            "  <key>StartCalendarInterval</key>\n"
            "  <dict>\n"
            "    <key>Hour</key>\n"
            f"    <integer>{self.hour}</integer>\n"
            "    <key>Minute</key>\n"
            f"    <integer>{self.minute}</integer>\n"
            "  </dict>\n"
            "  <key>StandardOutPath</key>\n"
            f"  <string>{_xml_escape(str(log))}</string>\n"
            "  <key>StandardErrorPath</key>\n"
            f"  <string>{_xml_escape(str(log))}</string>\n"
            "  <key>RunAtLoad</key>\n"
            "  <false/>\n"
            "</dict>\n"
            "</plist>\n"
        )

    # ── systemd --user artifacts ─────────────────────────────────────────────────────────
    def unit_files(self) -> dict[Path, str]:
        """The two unit files (path → content): a oneshot service and its daily timer.

        The service runs the checker through a LOGIN shell (``bash -lc``): a user unit starts
        with systemd's minimal environment, while the checker needs the login PATH (``gh``,
        ``git``) and the provider keys/CLI config dirs the user's profile exports.
        ``Persistent=true`` fires a run missed while the machine was off or logged out.
        """
        if self.unit_dir is None:
            raise ValueError("unit_files() needs a systemd schedule (unit_dir unset)")
        cmd = " ".join(_sh_quote(a) for a in self.checker_cmd)
        service = (
            "[Unit]\n"
            "Description=rig: daily model-freshness check (agent-tools)\n"
            "\n"
            "[Service]\n"
            "Type=oneshot\n"
            f"ExecStart=/bin/bash -lc {_systemd_quote(cmd)}\n"
        )
        timer = (
            "[Unit]\n"
            "Description=rig: daily model-freshness check (agent-tools)\n"
            "\n"
            "[Timer]\n"
            f"OnCalendar=*-*-* {self.hour:02d}:{self.minute:02d}:00\n"
            "Persistent=true\n"
            f"Unit={self.service_name}\n"
            "\n"
            "[Install]\n"
            "WantedBy=timers.target\n"
        )
        return {self.unit_dir / self.service_name: service, self.unit_dir / self.timer_name: timer}


def systemd_user_unit_dir() -> Path:
    """Where the systemd user manager looks for user-written units:
    ``$XDG_CONFIG_HOME/systemd/user``, else ``~/.config/systemd/user``."""
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    return (Path(xdg) if xdg else Path.home() / ".config") / "systemd" / "user"


def detect_platform() -> str:
    """"launchd" on macOS, "systemd" elsewhere. Override with ``RIG_SCHEDULE_PLATFORM``
    (test seam — lets the launchd/systemd branches be exercised on either host)."""
    forced = os.environ.get("RIG_SCHEDULE_PLATFORM", "").strip().lower()
    if forced in ("launchd", "systemd"):
        return forced
    return "launchd" if sys.platform == "darwin" else "systemd"


def default_checker_path(agent_tools_source: str | Path | None) -> Path | None:
    """The model_freshness.py path inside the given agent-tools checkout (None if unknown).

    rig consumes agent-tools READ-ONLY; the schedule runs the checker FROM that checkout, so
    the command path is anchored on the resolved ``agent_tools_source``.
    """
    if not agent_tools_source:
        return None
    return Path(agent_tools_source) / "lib" / "checker" / "model_freshness.py"


def build_schedule(
    *,
    checker_path: Path,
    hour: int = DEFAULT_HOUR,
    minute: int = DEFAULT_MINUTE,
    label: str = DEFAULT_LABEL,
    platform: str | None = None,
    python: str | None = None,
) -> SchedulePlan:
    """Resolve the desired :class:`SchedulePlan` for this machine.

    ``python`` defaults to ``python3`` (resolved on PATH at run time by launchd/systemd, not
    pinned to rig's interpreter — the checker is stdlib-first and runs under any python3).
    """
    plat = platform or detect_platform()
    py = python or "python3"
    cmd = [py, str(checker_path)]
    plist_path = None
    log_path = None
    unit_dir = None
    if plat == "launchd":
        plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        log_path = Path.home() / "Library" / "Logs" / f"{label}.log"
    else:
        unit_dir = systemd_user_unit_dir()
    return SchedulePlan(
        platform=plat,
        label=label,
        hour=hour,
        minute=minute,
        checker_cmd=cmd,
        plist_path=plist_path,
        log_path=log_path,
        unit_dir=unit_dir,
    )


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _sh_quote(s: str) -> str:
    """POSIX-shell single-quote one argv token (bare when it is plainly safe)."""
    if s and all(c.isalnum() or c in "@+=:,./-_" for c in s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


def _systemd_quote(s: str) -> str:
    """Quote ONE ``ExecStart=`` argument for systemd's own parser: double quotes with ``\\`` and
    ``"`` escaped, ``%`` doubled (specifier) and ``$`` doubled (variable expansion)."""
    escaped = s.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'
