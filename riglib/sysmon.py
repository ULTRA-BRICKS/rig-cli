"""sysmon provisioning — PURE planning of the rig-managed resource-monitor daemon (rig-cli#622).

## Why

Three crashes in 48 hours on the same Mac (2026-09-27 WindowServer hang; 2026-09-27 kernel
panic from a tg-ctl process leak that pushed the compressor to its limit and exhausted swap;
2026-09-29 12:47 WindowServer-watchdog panic at 95.7% WIRED memory from 13 concurrent
``whisper-cli`` GPU model loads while the memorystatus level still read "normal") and a
fourth near-miss the same afternoon (load ~19 on 14 CPUs, swap 3.8/5 GB, an agent's ``lldb``
raising a SecurityAgent ``system.privilege.taskport.debug`` dialog that hung htop, ``sample``,
``footprint`` and ``lsof``). A root-cause fix closes one leak; nothing caught the NEXT cause of
the same failure mode before the kernel did. This daemon is that layer: it watches the machine,
alerts through tg-cli, heals safely, can hand an incident to a coding agent, and resumes the
agent sessions an incident interrupted. The daemon itself is :mod:`riglib.sysmon_daemon`.

## Shape (mirrors ``tcc_watch`` / ``tgctl_reaper``)

- a GLOBAL-only, default-ON ``sysmon:`` block → ONE ``provision_sysmon`` action;
- this module renders the artifacts (pure): the LaunchAgent plist (macOS) or the systemd
  ``--user`` unit (Linux), the runtime ``sysmon.json``, and the package files copied
  byte-for-byte from ``riglib/sysmon_daemon/`` to ``<config_dir>/sysmon_daemon/``;
- ``actions/runner.py::_do_provision_sysmon`` writes them and (re)loads the service,
  ``drift.py::_check_sysmon`` diffs them, ``verify.py`` checks the service is loaded, and
  ``rig status`` prints the daemon's live health from its heartbeat.

The service runs ``python3 -m sysmon_daemon run --config <config_dir>/sysmon.json`` with
``config_dir`` as the working directory, so the package imports from the copy rig manages.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

DEFAULT_LABEL = "ai.hyperide.sysmon"
DEFAULT_CONFIG_DIR = "~/.config/rig/sysmon"
PACKAGE_NAME = "sysmon_daemon"
CONFIG_NAME = "sysmon.json"
STATE_DIR_NAME = "state"
OUT_LOG_NAME = "launchd.sysmon.out.log"
ERR_LOG_NAME = "launchd.sysmon.err.log"

_PYTHON3_FALLBACK_PATHS = ("/usr/bin/python3", "/opt/homebrew/bin/python3", "/usr/local/bin/python3")

# The daemon shells out to tg / tg-ctl / tmux / omp / the rig-detached launchers, which live in
# the user's tool dirs — a launchd/systemd job gets a bare PATH otherwise.
_DAEMON_PATH_TEMPLATE = (
    "{python_dir}:{home}/.local/bin:{home}/.bun/bin:{home}/bin:/opt/homebrew/bin:/usr/local/bin:"
    "/usr/bin:/bin:/usr/sbin:/sbin"
)


@dataclass(frozen=True)
class SysmonPlan:
    """The desired sysmon service state, fully resolved. Pure data, no I/O."""

    home: Path
    label: str
    config_dir: Path
    python_bin: Path
    runtime: Dict[str, Any] = field(default_factory=dict)

    # ── resolved artifact paths ──────────────────────────────────────────────────────
    @property
    def plist_path(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{self.label}.plist"

    @property
    def unit_name(self) -> str:
        return f"{self.label}.service"

    @property
    def unit_path(self) -> Path:
        return self.home / ".config" / "systemd" / "user" / self.unit_name

    @property
    def config_path(self) -> Path:
        return self.config_dir / CONFIG_NAME

    @property
    def state_dir(self) -> Path:
        return self.config_dir / STATE_DIR_NAME

    @property
    def package_dir(self) -> Path:
        return self.config_dir / PACKAGE_NAME

    @property
    def out_log_path(self) -> Path:
        return self.config_dir / OUT_LOG_NAME

    @property
    def err_log_path(self) -> Path:
        return self.config_dir / ERR_LOG_NAME

    @property
    def daemon_path_env(self) -> str:
        return _DAEMON_PATH_TEMPLATE.format(python_dir=str(self.python_bin.parent), home=str(self.home))

    @property
    def program_arguments(self) -> list:
        return [str(self.python_bin), "-m", PACKAGE_NAME, "run", "--config", str(self.config_path)]

    # ── renders ──────────────────────────────────────────────────────────────────────
    def render_plist(self) -> str:
        """The rig-owned LaunchAgent: resident (``RunAtLoad`` + ``KeepAlive``), restarted by
        launchd if it ever exits. ``sort_keys=False`` keeps a re-apply a byte-for-byte no-op."""
        import plistlib

        payload = {
            "Label": self.label,
            "ProgramArguments": self.program_arguments,
            "WorkingDirectory": str(self.config_dir),
            "EnvironmentVariables": {
                "PATH": self.daemon_path_env,
                "HOME": str(self.home),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 10,
            "ProcessType": "Background",
            "StandardOutPath": str(self.out_log_path),
            "StandardErrorPath": str(self.err_log_path),
        }
        return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False).decode("utf-8")

    def render_unit(self) -> str:
        """The systemd ``--user`` unit (Linux) — same command, restarted on exit."""
        args = " ".join(self.program_arguments)
        return (
            "# rig-managed (rig-cli#622) — edit ~/.config/rig/config.yaml `sysmon:` and run `rig apply commit`.\n"
            "[Unit]\n"
            "Description=rig sysmon resource monitor\n"
            "\n"
            "[Service]\n"
            f"WorkingDirectory={self.config_dir}\n"
            f"Environment=PATH={self.daemon_path_env}\n"
            "Environment=PYTHONDONTWRITEBYTECODE=1\n"
            f"ExecStart={args}\n"
            "Restart=always\n"
            "RestartSec=10\n"
            "Nice=5\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )

    def render_config(self) -> str:
        """``sysmon.json`` — the runtime config the daemon reads (sorted, stable bytes)."""
        return json.dumps(self.runtime, indent=1, sort_keys=True) + "\n"

    def render_package(self) -> Dict[str, str]:
        """``{file name: source}`` for every module of ``riglib/sysmon_daemon`` — copied
        byte-for-byte, so the installed daemon runs exactly the code the unit suite tests."""
        from . import sysmon_daemon

        pkg = Path(sysmon_daemon.__file__).parent
        return {p.name: p.read_text(encoding="utf-8") for p in sorted(pkg.glob("*.py"))}


def _resolve_python3() -> Path:
    found = shutil.which("python3")
    if found:
        return Path(found)
    for candidate in _PYTHON3_FALLBACK_PATHS:
        path = Path(candidate)
        if path.is_file():
            return path
    return Path(_PYTHON3_FALLBACK_PATHS[0])


def _expand_home(p: str, home: Path) -> Path:
    if p == "~":
        return home
    if p.startswith("~/"):
        return home / p[2:]
    return Path(p)


def _merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def runtime_config(block: Dict[str, Any], *, config_dir: Path, gpu_env: Dict[str, str]) -> Dict[str, Any]:
    """The daemon's ``sysmon.json`` from the (validated) YAML block over the daemon defaults.

    ``gpu_env`` is the global ``env.vars`` block: when it sets ``RIG_GPU_SLOTS`` /
    ``RIG_GPU_SLOT_DIR`` (the agent-tools#817 pool's size and lock dir, exported machine-wide by
    rig's ``env`` area), the monitor's cap follows it — one source of truth for the pool size.
    """
    from .sysmon_daemon.config import DEFAULTS

    rt = _merge(DEFAULTS, {})
    for key in ("sample_interval_seconds", "census_interval_seconds", "snapshot_interval_seconds", "auto_remediate"):
        if key in block:
            rt[key] = block[key]
    if "events" in block:
        rt["events"] = {"enabled": bool(block["events"])}
    for section in ("thresholds", "alert", "heal", "remediation", "sessions", "gpu"):
        if isinstance(block.get(section), dict):
            rt[section] = _merge(rt[section], block[section])
    gpu = dict(rt["gpu"])
    user_gpu = block.get("gpu") or {}
    if "slots" not in user_gpu and str(gpu_env.get("RIG_GPU_SLOTS", "")).isdigit():
        gpu["slots"] = int(gpu_env["RIG_GPU_SLOTS"])
    if "slot_dir" not in user_gpu and gpu_env.get("RIG_GPU_SLOT_DIR"):
        gpu["slot_dir"] = str(gpu_env["RIG_GPU_SLOT_DIR"])
    rt["gpu"] = gpu
    rt["state_dir"] = str(config_dir / STATE_DIR_NAME)
    return rt


def build_sysmon(
    *,
    home: Path,
    block: Dict[str, Any],
    gpu_env: Dict[str, str],
) -> SysmonPlan:
    """Resolve the desired :class:`SysmonPlan` from the validated ``sysmon`` block."""
    config_dir = _expand_home(str(block.get("config_dir") or DEFAULT_CONFIG_DIR), home)
    python_bin = _expand_home(str(block["python_bin"]), home) if block.get("python_bin") else _resolve_python3()
    return SysmonPlan(
        home=home,
        label=str(block.get("label") or DEFAULT_LABEL),
        config_dir=config_dir,
        python_bin=python_bin,
        runtime=runtime_config(block, config_dir=config_dir, gpu_env=gpu_env),
    )
