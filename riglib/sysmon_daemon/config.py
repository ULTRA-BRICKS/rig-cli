"""sysmon runtime configuration: the ``sysmon.json`` rig renders, merged over these defaults.

rig owns the file (``riglib.sysmon.SysmonPlan.render_config``); the daemon only reads it.
Every key has a default here, so a partial or missing file still yields a working monitor —
unknown keys are ignored (rig validates the YAML block fail-closed before it ever renders).
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

#: ``{metric: {"warn": x, "critical": y}}``. For ``reclaimable_gb``/``disk_free_gb`` LOWER is
#: worse; for every other metric HIGHER is worse (see ``detect.LOWER_IS_WORSE``).
DEFAULT_THRESHOLDS: Dict[str, Dict[str, float]] = {
    "load_per_cpu": {"warn": 1.25, "critical": 2.0},
    "swap_pct": {"warn": 80.0, "critical": 95.0},
    "wired_pct": {"warn": 75.0, "critical": 85.0},
    "reclaimable_gb": {"warn": 2.0, "critical": 1.0},
    "compressor_pct": {"warn": 70.0, "critical": 90.0},
    "disk_free_gb": {"warn": 5.0, "critical": 2.0},
    "procs_pct": {"warn": 60.0, "critical": 80.0},
    # same-name storms count EVERY user's processes; the percent is of kern.maxproc (rig-cli#644)
    "storm_same_name": {"warn": 150, "critical": 300},
    "storm_pct": {"warn": 4.0, "critical": 8.0},
    # one launchd coalition's fan-out (2026-09-27: 684 in one coalition; cmux at rest holds ~100)
    "coalition_procs": {"warn": 400, "critical": 600},
    "coalition_pct": {"warn": 8.0, "critical": 12.0},
    "proc_growth_per_min": {"warn": 200, "critical": 500},
    "display_delays_per_min": {"warn": 2, "critical": 5},
    "display_max_delay_s": {"warn": 1.5, "critical": 3.0},
    "taskport_stuck_s": {"warn": 20, "critical": 60},
    # launchd daemons (rig-cli#643): a run this many times its StartInterval old is hung ...
    "daemon_overrun_x": {"warn": 2, "critical": 3},
    # ... and this many unscheduled relaunches within 10 minutes (last exit failed) is a crash loop.
    "daemon_crashloop_runs": {"warn": 3, "critical": 6},
}

#: Executable basenames that load a large ggml/gguf/mlx model into GPU (wired) memory.
DEFAULT_GPU_PATTERNS = (
    "whisper-cli",
    "whisper-server",
    "whisper-stream",
    "main-whisper",
    "llama-cli",
    "llama-server",
    "llama-run",
    "llama-mtmd-cli",
    "mlx_lm.generate",
    "mlx_lm.server",
    "mlx_whisper",
    "ollama_llama_server",
)

DEFAULTS: Dict[str, Any] = {
    "version": 1,
    "label": "ai.hyperide.sysmon",  # the monitor's own launchd label: never restarted by itself
    "state_dir": "~/.config/rig/sysmon/state",
    "sample_interval_seconds": 10,
    "census_interval_seconds": 60,
    "snapshot_interval_seconds": 300,
    "thresholds": DEFAULT_THRESHOLDS,
    "events": {"enabled": True},
    "alert": {
        "enabled": True,
        "tg_bin": "tg",
        "cooldown_minutes": 30,
        "max_per_hour": 6,
        "resolved": True,
        "min_severity": "critical",
        "dry_run": False,
    },
    "heal": {
        "enabled": True,
        "notify_agent": True,
        "throttle": True,
        "kill_orphans": False,  # every kill path is opt-in
        "kill_owned": False,
        "grace_seconds": 120,
        "dry_run": False,
        "tg_ctl_bin": "tg-ctl",
        # the restart rung (rig-cli#643): `launchctl kickstart -k` of a hung or crash-looping job,
        # only for a label matching these patterns (never the monitor itself or an application.* job)
        "restart_daemons": True,
        "restart_labels": ["ai.hyperide.*", "com.hyperide.*"],
        "restart_cooldown_minutes": 30,
        "max_restarts_per_day": 3,
    },
    "auto_remediate": False,
    "remediation": {
        "agent": "omp",
        "cooldown_minutes": 60,
        "skills_dir": "~/.agents/skills",
        "workdir": "~",
        "dry_run": False,
    },
    "sessions": {
        "sessions_dir": "~/.omp/agent/sessions",
        "omp_bin": "omp",
        "tmux_bin": "tmux",
        "tmux_session": "main",
        "restore_on_boot": False,
        "panic_dirs": [
            "/Library/Logs/DiagnosticReports",
            "/Library/Logs/DiagnosticReports/Retired",
        ],
    },
    "gpu": {
        "slots": 2,
        "slot_dir": "~/.local/state/agent-tools/gpu-slots",
        "patterns": list(DEFAULT_GPU_PATTERNS),
    },
}


def _merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def expand(path: str, home: Optional[Path] = None) -> Path:
    """``~``-expand against ``home`` (default ``$HOME``)."""
    home = home or Path(os.environ.get("HOME") or Path.home())
    if path == "~":
        return home
    if path.startswith("~/"):
        return home / path[2:]
    return Path(path)


def load(path: Optional[Path]) -> Dict[str, Any]:
    """Defaults merged with the JSON file at ``path`` (absent file → pure defaults)."""
    data: Dict[str, Any] = {}
    if path is not None and Path(path).is_file():
        loaded = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    return _merge(DEFAULTS, data)


def state_dir(cfg: Dict[str, Any]) -> Path:
    return expand(str(cfg["state_dir"]))
