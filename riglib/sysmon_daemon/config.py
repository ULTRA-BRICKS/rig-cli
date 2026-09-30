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

#: ``{metric: {"warn": x, "critical": y}}``. For ``reclaimable_gb``/``free_gb``/``disk_free_gb``
#: LOWER is worse; for every other metric HIGHER is worse (see ``detect.LOWER_IS_WORSE``).
DEFAULT_THRESHOLDS: Dict[str, Dict[str, float]] = {
    "load_per_cpu": {"warn": 1.25, "critical": 2.0},
    "swap_pct": {"warn": 80.0, "critical": 95.0},
    "wired_pct": {"warn": 75.0, "critical": 85.0},
    "reclaimable_gb": {"warn": 2.0, "critical": 1.0},
    # Free pages (vm_stat "Pages free"), calibrated on the 24 GB / 16 KB-page Mac (2026-09-30).
    # The pageout daemon keeps them near vm_page_free_target (4000 pages = 62.5 MB) by stealing
    # inactive pages, so a low free count alone is normal load: 10 minutes of 50-ms reads had a
    # median of 17,974 pages and 1% under 3,635, and the 2026-09-29 panic still showed 4,610 (its
    # signal was the ~2,000 inactive pages left, which reclaimable_gb catches). Staying under the
    # target means nothing is left to steal: warn at 0.05 GB (3,277 pages, just under
    # vm_page_free_min = 3,500), critical at 0.025 GB (1,638 pages, under half of free_min and 728
    # above vm_page_free_reserved = 910, where only VM-privileged threads may allocate). Momentary
    # dips under both are normal too, so a line must hold for detect.FREE_SUSTAIN_SAMPLES samples.
    # Linux's MemFree has the same shape (kswapd refills it).
    "free_gb": {"warn": 0.05, "critical": 0.025},
    "compressor_pct": {"warn": 70.0, "critical": 90.0},
    "disk_free_gb": {"warn": 5.0, "critical": 2.0},
    # The data volume, as df's Capacity: Used / (Used + Available).
    "data_used_pct": {"warn": 90.0, "critical": 95.0},
    "procs_pct": {"warn": 60.0, "critical": 80.0},
    "storm_same_name": {"warn": 150, "critical": 300},
    "proc_growth_per_min": {"warn": 200, "critical": 500},
    "display_delays_per_min": {"warn": 2, "critical": 5},
    "display_max_delay_s": {"warn": 1.5, "critical": 3.0},
    "taskport_stuck_s": {"warn": 20, "critical": 60},
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
        "disk_cooldown_minutes": 360,
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
    # What every disk alert lists as reclaimable (report only — see reclaim.py).
    "reclaim": {
        # dev roots holding rig-managed repos (rig's own default: spotlight.DEFAULT_ROOTS); the
        # scan lists <root>/<repo>/.worktrees/<name> checkouts idle for worktree_age_days
        "worktree_roots": ["~/xp", "~/work"],
        "worktree_age_days": 7,
        # review-cli's job records (reviewlib.jobs.jobs_dir: macOS, then Linux)
        "cache_dirs": ["~/Library/Caches/review-cli/jobs", "~/.cache/review-cli/jobs"],
        "max_candidates": 10,
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
