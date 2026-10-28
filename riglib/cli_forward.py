"""Thin `rig` subcommands that forward to a sibling engine (sysmon, stats, daily, usage, report, evolve, lint …).

Context: these handlers hold no behavior of their own; each lazy-imports its engine so `rig --help` stays light.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from .cli_output import _dim, _err_block
from .cli_plan_print import _resolve_explicit_config


def cmd_sysmon(args: argparse.Namespace) -> int:
    """``rig sysmon …`` — a thin forward to :func:`riglib.sysmon_daemon.cli.main` (the SAME code
    the installed daemon runs), pointing ``--config`` at the installed ``sysmon.json``."""
    from .config import global_config_path, read_yaml_file
    from .sysmon import CONFIG_NAME, DEFAULT_CONFIG_DIR
    from .sysmon_daemon import cli as sysmon_cli

    argv = list(args.sysmon_args or [])
    if not argv:
        sysmon_cli.build_parser().print_help()
        return 0
    if "--config" not in argv and argv[0] not in ("-h", "--help"):
        gpath = global_config_path()
        block = (read_yaml_file(gpath).get("sysmon") if gpath.is_file() else None) or {}
        config_dir = str(block.get("config_dir") or DEFAULT_CONFIG_DIR)
        argv[1:1] = ["--config", str(Path(config_dir).expanduser() / CONFIG_NAME)]
    return sysmon_cli.main(argv)


# ── commands ──────────────────────────────────────────────────────────────────────
def cmd_spotlight_sweep(args: argparse.Namespace) -> int:
    """Run the Spotlight-exclude sweep once (the launchd re-sweep agent's command).

    Reads the merged ``spotlight:`` config, resolves roots + denylist, and drops the sentinel into
    every matched dependency/build dir. Idempotent; touches no launchd state. Exit 0 on success.
    Deliberately NOT gated by ``RIG_SPOTLIGHT_DRY_RUN`` (unlike the apply-time provisioner): this
    command IS the sweep — a dry-run of it would be a no-op.
    """
    from . import spotlight
    from .config import ConfigError, load

    try:
        env_root, explicit = _sweep_config_context(args)
        loaded = load(env_root, explicit_config=explicit, include_repo=True)
    except ConfigError as exc:
        print(_err_block(exc))
        return 2
    s = loaded.data.get("spotlight")
    if not isinstance(s, dict):
        s = {}
    # Honor the opt-out/default-off contract: if the config is absent or spotlight is disabled,
    # this is a NO-OP (the persistent launchd agent keeps invoking us after a config removal — it
    # must not keep writing sentinels once the user turned the feature off). Same guard as
    # plan._build_spotlight: an explicit `enabled: true` is required to sweep.
    if not s.get("enabled"):
        print("spotlight-sweep: disabled in config (spotlight.enabled is not true) — nothing to do")
        return 0
    roots = spotlight.resolve_roots(s.get("roots"))
    deny = spotlight.resolve_deny(s.get("deny"), s.get("extra"))
    max_depth = int(s.get("max_depth", spotlight.DEFAULT_MAX_DEPTH))
    result = spotlight.perform_sweep(roots, deny, max_depth)
    print(f"spotlight-sweep: {result.summary()}")
    for missing in result.roots_missing:
        print(_dim(f"  (root not present: {missing})"))
    return 0


def _sweep_config_context(args: argparse.Namespace) -> tuple[Path, Path | None]:
    """Resolve (repo_root, explicit_config) for the sweep, reusing the shared config-context helpers."""
    from .detect import detect_environment

    env = detect_environment(Path(args.cwd).resolve())
    explicit = _resolve_explicit_config(env, getattr(args, "config", None))
    return env.repo_root, explicit


def cmd_install_skill(args: argparse.Namespace) -> int:
    from .install import install_skill

    return install_skill()


def cmd_config_web(args: argparse.Namespace) -> int:
    """`rig config-web …` — view/edit the reconciled rig config in a local web UI.

    A thin pass-through to :func:`riglib.config_web_service.dispatch_cli`, which routes the verb
    (run/start/stop/status/enable/disable + the internal `_serve`) through the SHARED
    agenttools-service manager. A bare `rig config-web` prints HELP and never launches a server.
    The service module is imported lazily so a missing agenttools-service lib only bites when a
    lifecycle verb is actually invoked (it raises a structured MissingDepError then), not at
    `rig --help` time.
    """
    from . import config_web_service

    return config_web_service.dispatch_cli(args)


def cmd_evolve(args: argparse.Namespace) -> int:
    """Dispatch `rig evolve` lifecycle/internal serve verbs."""
    from .evolve import service as evolve_service

    return evolve_service.dispatch_cli(args)


def cmd_lint(args: argparse.Namespace) -> int:
    from .lint_parser import dispatch

    return dispatch(args)


def cmd_stats(args: argparse.Namespace) -> int:
    # `rig stats` with no action → print the stats subparser's own help (mirrors top-level
    # `rig` behaviour; no hand-retyped usage to drift from the real flags).
    if getattr(args, "stats_action", None) != "show":
        parser = getattr(args, "_stats_parser", None)
        if parser is not None:
            parser.print_help()
        return 0
    from .stats import run as stats_run

    return stats_run(args)


def cmd_daily(args: argparse.Namespace) -> int:
    if args.action == "install-skill":
        from .daily.skill import install_skill as daily_install_skill

        return daily_install_skill()
    from .daily.command import run as daily_run

    return daily_run(args)


def cmd_usage(args: argparse.Namespace) -> int:
    from .usage import run as usage_run

    return usage_run(args)


def cmd_report(args: argparse.Namespace) -> int:
    from .report import run as report_run

    return report_run(args)
