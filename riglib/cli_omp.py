"""`rig omp` command handlers (launcher, watchdog, pane save/restore).

Context: glue over the omp launcher; the parser lives in `cli_parser_worktree`.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .cli_output import _warn
from .cli_parser_worktree import omp_launcher


def cmd_omp(args: argparse.Namespace) -> int:
    if not getattr(args, "omp_command", None):
        args._omp_parser.print_help()
        return 0
    handlers = {
        "save-panes": _cmd_omp_save_panes,
        "restore-panes": _cmd_omp_restore_panes,
        "watchdog": _cmd_omp_watchdog,
    }
    handler = handlers.get(args.omp_command)
    if handler is None:
        args._omp_parser.print_help()
        return 2
    return handler(args)


def _cmd_omp_watchdog(args: argparse.Namespace) -> int:
    if sys.platform == "win32":
        print("tmux watchdog requires POSIX", file=sys.stderr)
        return 2
    from dataclasses import asdict

    from .tmux_watchdog import run_once
    result = run_once(
        Path(args.socket_path),
        boot_script=Path(args.boot_script) if args.boot_script else None,
        tmux_bin=args.tmux_bin, paused=args.paused,
    )
    if args.json:
        print(json.dumps(asdict(result)))
    elif result.action != "none":
        print(f"tmux recovery: {result.action}: {result.reason}")
    return 1 if result.action == "blocked" else 0


def _cmd_omp_save_panes(args: argparse.Namespace) -> int:
    ol = omp_launcher()
    ok = ol.save_panes(Path(args.map_path), tmux_bin=args.tmux_bin, tg_ctl_bin=args.tg_ctl_bin)
    return 0 if ok else 1


def _cmd_omp_restore_panes(args: argparse.Namespace) -> int:
    ol = omp_launcher()
    blocked = ol.restore_panes(Path(args.map_path), tmux_bin=args.tmux_bin, tg_ctl_bin=args.tg_ctl_bin, rig_bin=args.rig_bin)
    if blocked:
        print(_warn(f"omp-restore: {blocked} pane(s) left untouched (see stderr) — never duplicated"))
    return 0
