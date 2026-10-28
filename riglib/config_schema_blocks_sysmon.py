"""The ``sysmon`` resource-monitor block of the published schema registry.

Split out of ``config_schema`` to keep each module small; the registry order is assembled there.
"""

from __future__ import annotations

from .config_schema_model import Block, Leaf


def _sysmon_threshold(doc: str) -> Block:
    return Block(
        doc=doc,
        leaves={
            "warn": Leaf("number", "warning line"),
            "critical": Leaf("number", "critical line"),
        },
    )


_SYSMON_BLOCK = Block(
    doc="the sysmon resource monitor (rig-cli#622): a per-machine resident daemon (LaunchAgent on "
    "macOS, systemd --user unit on Linux) that watches load, swap, wired/compressed memory, process "
    "storms, GPU-model processes, stuck task_for_pid tools and WindowServer freeze precursors; alerts "
    "via tg; heals safely (notify -> throttle -> stop orphans); can launch a remediation agent; and "
    "restores the omp sessions an incident interrupted.",
    leaves={
        "enabled": Leaf("boolean", "provision the sysmon daemon", default=True),
        "label": Leaf("string", "launchd Label / systemd unit stem", default="ai.hyperide.sysmon"),
        "config_dir": Leaf("string", "package, sysmon.json, state and logs dir", default="~/.config/rig/sysmon"),
        "python_bin": Leaf("string", "the python3 the daemon runs under"),
        "sample_interval_seconds": Leaf("integer", "seconds between in-process samples", default=10, minimum=1),
        "census_interval_seconds": Leaf("integer", "seconds between ps censuses", default=60, minimum=5),
        "snapshot_interval_seconds": Leaf("integer", "seconds between live-session snapshots", default=300,
                                          minimum=30),
        "hysteresis_pct": Leaf("number", "a raised finding clears only once its value is this percent of its warn "
                               "line past it (swap warn 80 clears under 76); 0 turns it off", default=5.0,
                               minimum=0, maximum=50),
        "events": Leaf("boolean", "stream WindowServer/watchdogd/authd log events (macOS)", default=True),
        "auto_remediate": Leaf("boolean", "launch a detached coding-agent session on a critical incident",
                               default=False),
    },
    nested={
        "thresholds": Block(
            doc="warn/critical lines per metric (reclaimable_gb, free_gb, disk_free_gb and "
                "swap_gate_available_pct: lower is worse)",
            nested={
                name: _sysmon_threshold(doc)
                for name, doc in (
                    ("load_per_cpu", "1-minute load average per CPU"),
                    ("swap_pct", "swap used, percent; a finding only under memory pressure (the swap_gate_* "
                                 "lines), its severity the lower of the two"),
                    ("swap_gate_psi_full_avg60", "swap gate: Linux PSI memory 'full' avg60, percent of the "
                                                 "last minute every task stalled on memory (macOS: memorystatus)"),
                    ("swap_gate_available_pct", "swap gate: MemAvailable (macOS reclaimable), percent of RAM"),
                    ("wired_pct", "wired memory, percent of RAM"),
                    ("reclaimable_gb", "free+inactive+speculative+purgeable GB"),
                    ("free_gb", "free memory pages GB (vm_stat 'Pages free'; Linux MemFree)"),
                    ("compressor_pct", "compressor pages, percent of its limit"),
                    ("disk_free_gb", "free GB on the swap volume"),
                    ("data_used_pct", "data volume used percent, df Capacity (a critical one lists "
                                      "reclaimable candidates, never deletes)"),
                    ("procs_pct", "processes, percent of kern.maxproc"),
                    ("storm_same_name", "processes (every user) with one executable name"),
                    ("storm_pct", "processes with one executable name, percent of kern.maxproc"),
                    ("coalition_procs", "processes in one launchd coalition (one job's whole fan-out)"),
                    ("coalition_pct", "processes in one launchd coalition, percent of kern.maxproc"),
                    ("proc_growth_per_min", "process-count growth per minute"),
                    ("display_delays_per_min", "WindowServer 'Timer delayed' lines per minute"),
                    ("display_max_delay_s", "longest WindowServer timer delay in the last minute"),
                    ("taskport_stuck_s", "age of a task_for_pid tool stuck behind SecurityAgent"),
                    ("daemon_overrun_x", "a watched launchd interval job's current run, in multiples of its interval"),
                    ("daemon_crashloop_runs", "unscheduled relaunches of a watched launchd job within 10 minutes"),
                )
            },
        ),
        "alert": Block(
            doc="alerts: Telegram via tg-cli, or the tg-ctl exclusive holder of source sysmon",
            leaves={
                "enabled": Leaf("boolean", "send alerts", default=True),
                "min_severity": Leaf("string", "lowest severity that alerts", enum=("warning", "critical"),
                                     default="critical"),
                "route": Leaf("string", "where alerts go: `telegram` (tg-ctl alert send) or `subscriber` (only the "
                              "tg-ctl exclusive holder of source sysmon; with no holder sysmon raises a handler "
                              "session, and only a CRITICAL alert no handler can take reaches Telegram)",
                              enum=("telegram", "subscriber"), default="telegram"),
                "cooldown_minutes": Leaf("integer", "re-alert an unchanged finding after this long", default=30,
                                         minimum=1),
                "disk_cooldown_minutes": Leaf("integer", "re-alert a disk finding at the same level after this long",
                                              default=360, minimum=1),
                "swap_cooldown_minutes": Leaf("integer", "re-alert an unchanged swap finding after this long",
                                              default=120, minimum=1),
                "max_per_hour": Leaf("integer", "messages per rolling hour", default=6, minimum=1),
                "resolved": Leaf("boolean", "send one line when an alerted finding clears (route `subscriber`: "
                                 "to the holder only, never Telegram)", default=True),
                "dry_run": Leaf("boolean", "record alerts without sending", default=False),
                "tg_ctl_bin": Leaf("string", "the alert sender: `tg-ctl alert send` (tg-cli#636)", default="tg-ctl"),
                "tg_bin": Leaf("string", "the `tg` fallback sender for a tg-ctl without `alert`", default="tg"),
            },
        ),
        "heal": Block(
            doc="the auto-heal ladder (never touches user-interactive processes)",
            leaves={
                "enabled": Leaf("boolean", "run the ladder", default=True),
                "notify_agent": Leaf("boolean", "message the offending agent", default=True),
                "throttle": Leaf("boolean", "move offenders to the background band on critical (restored when "
                                 "the pressure clears)", default=True),
                "kill_orphans": Leaf("boolean", "stop proven-orphan offender trees (their agent host died) after "
                                     "the grace period", default=False),
                "kill_owned": Leaf("boolean", "also stop agent-owned offenders after the grace period",
                                   default=False),
                "grace_seconds": Leaf("integer", "critical must persist this long before a stop", default=120,
                                      minimum=0),
                "dry_run": Leaf("boolean", "record steps without executing them", default=False),
                "restart_daemons": Leaf("boolean", "restart a hung or crash-looping watched launchd job with "
                                        "`launchctl kickstart -k` after the grace period", default=True),
                "restart_labels": Leaf("array", "launchd label patterns (fnmatch) watched for hangs/crash loops "
                                       "and restartable; never the monitor itself or an application.* GUI job",
                                       items_type="string", default=["ai.hyperide.*", "com.hyperide.*"]),
                "restart_cooldown_minutes": Leaf("integer", "at most one restart per label per this many minutes",
                                                 default=30, minimum=1),
                "max_restarts_per_day": Leaf("integer", "restarts per label per 24 h before sysmon stops and "
                                             "alerts for a human", default=3, minimum=1),
            },
        ),
        "remediation": Block(
            doc="the auto-remediation agent session (only when auto_remediate is true)",
            leaves={
                "agent": Leaf("string", "harness to launch", enum=("omp", "claude-code", "codex", "opencode"),
                              default="omp"),
                "cooldown_minutes": Leaf("integer", "at most one launch per this many minutes", default=60,
                                         minimum=1),
                "dry_run": Leaf("boolean", "build the brief and command without launching", default=False),
            },
        ),
        "handler": Block(
            doc="the handler session alert.route `subscriber` raises when nobody holds the sysmon alerts "
                "(rig-cli#700): an interactive omp in a tmux window that subscribes exclusively",
            leaves={
                "tmux_session": Leaf(("string", "integer"), "tmux session of the handler window (default: "
                                     "sessions.tmux_session)", minimum=0),
                "window": Leaf("string", "tmux window name", default="sysmon-handler"),
                "workdir": Leaf("string", "the handler's working directory", default="~"),
                "warn_persist_minutes": Leaf("integer", "a WARNING raises a handler after persisting this long",
                                             default=15, minimum=1),
                "spawn_cooldown_minutes": Leaf("integer", "at most one handler spawn per this many minutes",
                                               default=60, minimum=1),
                "startup_timeout_minutes": Leaf("integer", "a handler not holding the alerts this long after "
                                                "its spawn has failed to start", default=5, minimum=1),
                "idle_exit_minutes": Leaf("integer", "close the spawned handler after this long with no "
                                          "finding, no alert and an idle session", default=30, minimum=1),
                "dry_run": Leaf("boolean", "record the spawn without running tmux", default=False),
            },
        ),
        "sessions": Block(
            doc="session snapshot and restore",
            leaves={
                "restore_on_boot": Leaf("boolean", "resume interrupted omp sessions after a crash reboot",
                                        default=False),
                "tmux_session": Leaf("string", "tmux session that receives restored windows", default="main"),
            },
        ),
        "gpu": Block(
            doc="the agent-tools#817 GPU-model slot pool the monitor checks",
            leaves={
                "slots": Leaf("integer", "slot count (default: env.vars.RIG_GPU_SLOTS, else 2)", minimum=1),
                "slot_dir": Leaf("string", "lock dir (default: env.vars.RIG_GPU_SLOT_DIR, else the pool default)"),
            },
        ),
        "reclaim": Block(
            doc="what a critical data_used_pct alert lists as reclaimable (REPORT ONLY: sysmon never deletes)",
            leaves={
                "worktree_roots": Leaf("array", "dev roots whose <repo>/.worktrees/<name> checkouts are listed "
                                       "when idle (default ~/xp, ~/work)", items_type="string"),
                "worktree_age_days": Leaf("integer", "a worktree is idle after this many days without a change",
                                          default=7, minimum=1),
                "cache_dirs": Leaf("array", "cache dirs listed with their size (default: review-cli's job dirs)",
                                   items_type="string"),
                "max_candidates": Leaf("integer", "largest candidates listed", default=10, minimum=1),
            },
        ),
    },
)
