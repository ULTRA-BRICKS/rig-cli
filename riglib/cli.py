"""rig CLI — argparse + subcommand dispatch only.

The thin entry point (``[project.scripts] rig = "riglib.cli:main"`` and the target of the
``bin/rig`` shim). It owns argument parsing and dispatch; all behavior lives in the sibling
modules. Heavy/optional imports (textual TUI, yaml) are done lazily inside the handler that
needs them so ``rig --help`` and ``rig doctor`` stay fast and dependency-light.

Subcommands:

    rig init     first-run onboarding — scaffold rig.yaml + wire the catalog in (the front door)
    rig apply    PREVIEW the reconcile (bare = `apply info`); `apply commit` executes it
    rig setup    interactive config wizard (no TTY → usage for init/apply/config)
    rig config   get/set ONE config key by dot path; set previews by default, --commit reconciles
    rig env      set/list/del global env.vars keys (plaintext; list masks values)
    rig status   detect + report drift in BOTH directions (config↔disk)
    rig doctor   detect + (offer to) install required/optional dependencies
    rig export   serialize default/current config to rig.yaml without a TUI
    rig stats    tool-adoption analytics over agent-harness session logs (sub: `show`)
    rig daily    "what shipped" report over merged PRs (paste into Slack)
    rig usage    Claude token/cost usage across accounts (hypothetical cost at list price)
    rig evolve   local project evolution portal (git histogram + code treemap)
    rig worktree create/remove/gc — the standardized .worktrees/<name> agent-worktree lifecycle
    rig report   HTML completion report from git history since merge-base (HyperIDE /result-report)
"""

from __future__ import annotations

import argparse
from pathlib import Path

from . import __version__, harness_cli
from .cli_apply import cmd_activate, cmd_apply
from .cli_config import cmd_config, cmd_export, cmd_schema
from .cli_doctor import cmd_doctor
from .cli_env import cmd_env
from .cli_forward import (
    cmd_config_web,
    cmd_daily,
    cmd_evolve,
    cmd_install_skill,
    cmd_lint,
    cmd_report,
    cmd_spotlight_sweep,
    cmd_stats,
    cmd_sysmon,
    cmd_usage,
)
from .cli_init import cmd_setup
from .cli_omp import cmd_omp
from .cli_output import _c
from .cli_parser import (
    add_config_parser,
    add_daily_parser,
    add_env_parser,
    add_profile_parser,
    add_report_parser,
    add_stats_parser,
    add_usage_parser,
)
from .cli_parser_worktree import add_omp_parser, add_worktree_parser
from .cli_profile import cmd_profile
from .cli_status import cmd_status
from .cli_worktree import cmd_worktree
from .lint_parser import add_lint_parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rig",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="rig — the dev-environment umbrella driver. Set up a repo from a "
        "committed rig.yaml by applying agent-tools content (skills, hooks, CI gates, MCP).",
        # Exit codes are a PUBLIC CONTRACT (scripts/CI branch on them). Documented here per the
        # structured-exit-codes skill; the constants live in riglib/errors.py.
        epilog=(
            "exit codes:\n"
            "  0    success\n"
            "  1    internal error (an unexpected failure / bug)\n"
            "  2    invalid config (a malformed value, type, or unknown key)\n"
            "  3    drift (rig status found config↔disk drift)\n"
            "  4    unknown item (config names a catalog item that doesn't exist / was removed)\n"
            "  5    missing target (config references a path/binary that's gone on disk)\n"
            "  6    not a git repository (a repo-scoped command run outside a repo)\n"
            "  7    repo corrupt (a working checkout's git config is broken, e.g. core.bare=true)\n"
            "  127  missing dependency (a required external tool isn't installed)\n"
            "\n"
            "  precedence (doctor): `rig doctor` exits 7 (repo corrupt) ahead of any other class —\n"
            "  a broken .git (e.g. core.bare=true on a working checkout) makes every git-backed\n"
            "  check unreliable, so it is fixed first. With deps + targets clean, a claude config\n"
            "  dir (CLAUDE_CONFIG_DIR / ~/.claude-accounts/account-*) that lacks the rig-managed\n"
            "  hooks ~/.claude/settings.json carries exits 3 (drift) — those sessions run no guard.\n"
            "  precedence (status): when `rig status` finds BOTH a missing target and config↔disk\n"
            "  drift, it prints both but exits 5 (missing-target outranks drift — the dead\n"
            "  reference fails at runtime, so it's the more urgent class).\n"
        ),
    )
    p.add_argument("--version", action="version", version=f"rig {__version__}")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    def _add_setup_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("-C", "--cwd", default=".", help="repo root to operate on (default: cwd)")
        parser.add_argument("--config", help="apply this config file headlessly (non-interactive)")
        parser.add_argument("--yes", action="store_true", help="non-interactive; assume yes")
        # The repo's stack preset (l1/lang[/framework]); overrides auto-detection. Written into the
        # scaffolded rig.yaml and used to select the by-stack skill set.
        parser.add_argument(
            "--stack",
            help="the repo stack preset l1/lang[/framework] (e.g. mobile/swift/swiftui, "
            "frontend/ts/react, backend/python); default: auto-detect from the repo",
        )
        # NOTE: there is intentionally NO --no-write-config flag. rig.yaml is the committed
        # source of truth and is NOT optional (AGENTS.md). Use --dry-run for a no-write preview.
        parser.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
        # init only SCAFFOLDS rig.yaml + PREVIEWS the plan; applying is a deliberate second step
        # (`rig apply commit`). --apply opts into the one-shot "scaffold AND apply now".
        parser.add_argument(
            "--apply", action="store_true",
            help="also apply the plan now (default: init only scaffolds rig.yaml; run `rig apply commit`)",
        )
        parser.add_argument(
            "--plan", action="store_true",
            help="list every planned action (default: a per-carrier summary for a large plan)",
        )
        # --notes mirrors `rig apply --notes`: expand the collapsed informational notes so the
        # collapse hint the shared plan renderer prints ("--notes to expand") is valid here too.
        parser.add_argument(
            "--notes", action="store_true",
            help="expand every informational note (default: collapse them behind a one-line count)",
        )
        # a bare interactive `rig init` launches the TUI wizard (textual ships WITH rig); --no-tui
        # (or RIG_NO_TUI=1) opts out of the wizard, keeping the non-destructive preview path.
        parser.add_argument(
            "--no-tui", action="store_true",
            help="don't launch the interactive TUI; show a preview only",
        )

    # `init` is the canonical first-run onboarding command (the front door). init/apply are
    # the two real commands; interactivity (TUI/semi/--yes) is orthogonal to both. init
    # SCAFFOLDS + PREVIEWS by default — `rig apply` (or `rig init --apply`) is what applies.
    ip = sub.add_parser("init", help="first-run onboarding: scaffold rig.yaml + preview the plan (front door; `rig apply` applies)")
    _add_setup_args(ip)

    ap = sub.add_parser(
        "apply",
        help="preview the reconcile (bare = `apply info`); `apply commit` executes it",
        description="Reconcile disk to rig.yaml (idempotent). PREVIEW-BY-DEFAULT: a bare "
        "`rig apply` is an alias for `rig apply info` — it builds + prints the plan and MUTATES "
        "NOTHING. Run `rig apply commit` to actually execute it. apply only ADDS/UPDATES the "
        "items your config declares — it NEVER deletes on-disk extras. Items present on disk "
        "but not declared in any layer (e.g. a hand-added skill) are reported by `rig status` "
        "and left untouched; apply will not nuke them.",
    )
    # info | commit as a positional (NOT argparse sub-subparsers) so every flag stays on the one
    # apply parser and works regardless of position — `rig apply commit -C x` and `rig apply -C x`
    # both parse cleanly, with no per-subparser namespace to keep in sync.
    ap.add_argument(
        "mode", nargs="?", choices=("info", "commit"), default=None,
        help="info: preview the plan, write nothing (default). commit: execute the plan.",
    )
    ap.add_argument("-C", "--cwd", default=".", help="repo root (default: cwd)")
    ap.add_argument("--config", help="config file to apply (default: ./rig.yaml + global)")
    ap.add_argument("--dry-run", action="store_true", help="force preview even under `commit`")
    ap.add_argument("--only", help="comma-separated categories to scope (e.g. skills,ci)")
    ap.add_argument(
        "--yes", action="store_true",
        help="non-interactive commit intent: a bare `apply --yes` executes (automation back-compat)",
    )
    ap.add_argument(
        "-v", "--verbose", action="store_true",
        help="show already-in-sync (no-op) actions, which are collapsed behind a count by default",
    )
    ap.add_argument(
        "--notes", action="store_true",
        help="expand every informational note (default: collapse them behind a one-line count)",
    )
    ap.add_argument(
        "--plan", action="store_true",
        help="list every planned action (default: a per-carrier summary for a large plan)",
    )

    acp = sub.add_parser(
        "activate",
        help="turn on everything this machine can run (preview by default; `activate commit` executes)",
        description="One command for a new or reinstalled machine: sync the profile (shared config + "
        "tmux/omp/sysmon files), probe what this machine supports, switch on the opt-in areas the "
        "config does not mention yet (harness, tmux, harness wrappers, the first-class ctmux tool), "
        "then apply the machine-level plan — skills, agent hooks (omp included), git hooks, tmux, "
        "omp config, harness wrappers, sysmon, tools. PREVIEW-BY-DEFAULT like `rig apply`: a bare "
        "`rig activate` mutates nothing; `rig activate commit` executes. Idempotent. Areas the "
        "machine cannot support are listed with the reason and skipped; they never fail the run. "
        "Existing config keys are never overridden.",
    )
    acp.add_argument(
        "mode", nargs="?", choices=("info", "commit"), default=None,
        help="info: preview, write nothing (default). commit: sync the profile, write the enable "
        "switches, apply the plan.",
    )
    acp.add_argument("--dry-run", action="store_true", help="force preview even under `commit`")
    acp.add_argument(
        "--only", metavar="AREAS",
        help="comma-separated areas to limit the run to (default: all): "
        "skills, agent_hooks, git_hooks, harness, tmux, harness_wrappers, sysmon, models, tg_ctl, "
        "tcc_watch, tgctl_reaper, spotlight, tools, global",
    )
    acp.add_argument("--json", action="store_true", help="preview as JSON (areas, config changes); not with commit")
    acp.add_argument("-v", "--verbose", action="store_true", help="show already-in-sync (no-op) actions")
    acp.add_argument("--notes", action="store_true", help="expand every informational note")
    acp.add_argument("--plan", action="store_true", help="list every planned action")

    st = sub.add_parser(
        "status",
        help="report drift between config and disk, grouped by layer and managed area",
        description="Report drift between config and disk. In a git repo, status shows GLOBAL "
        "machine-wide areas and the REPO layer from ./rig.yaml; outside a git repo, it ignores "
        "auto-discovered ./rig.yaml, reports only GLOBAL areas, and marks the repo layer / "
        "rig.yaml as N/A.",
    )
    st.add_argument("-C", "--cwd", default=".", help="repo root (default: cwd)")
    st.add_argument("--config", help="config file (default: ./rig.yaml + global)")

    dp = sub.add_parser("doctor", help="detect + (offer to) install dependencies")
    dp.add_argument("--yes", action="store_true", help="install missing deps non-interactively")
    dp.add_argument("--optional", action="store_true", help="also install optional deps")
    dp.add_argument(
        "--fix",
        action="store_true",
        help="auto-repair detected repo corruption (e.g. reset a wrong core.bare=true)",
    )

    ep = sub.add_parser("export", help="write a rig.yaml from default/current config")
    ep.add_argument("-C", "--cwd", default=".", help="repo root (default: cwd)")
    ep.add_argument("-o", "--output", default="rig.yaml", help="output path (default: rig.yaml)")
    ep.add_argument("--force", action="store_true", help="overwrite an existing file")

    scp = sub.add_parser(
        "schema",
        help="print the rig.yaml JSON Schema (or --check / --write that it matches on disk)",
        description="Emit the JSON Schema for rig.yaml + the global config (generated from the "
        "single in-code registry). Editors point at the committed schema/rig.schema.json for "
        "completion/validation; `--check` fails if the committed file is stale, `--write` "
        "regenerates it.",
    )
    scp.add_argument("--check", action="store_true",
                     help="exit non-zero if schema/rig.schema.json is missing or out of sync")
    scp.add_argument("--write", action="store_true",
                     help="(re)generate schema/rig.schema.json from the in-code registry")

    sub.add_parser("install-skill", help="register the rig agent skill with harnesses")

    # `spotlight-sweep` runs the Spotlight-exclude sweep once (the command the launchd re-sweep
    # agent invokes). It reads the merged `spotlight:` config and drops .metadata_never_index into
    # dependency/build dirs under the configured roots. Idempotent; no launchd mutation.
    ss = sub.add_parser(
        "spotlight-sweep",
        help="drop .metadata_never_index into dependency/build dirs under the configured roots (macOS Spotlight-exclude)",
    )
    ss.add_argument("-C", "--cwd", default=".", help="repo root (default: cwd)")
    ss.add_argument("--config", help="config file (default: ./rig.yaml + global)")

    # `setup` IS the interactive wizard (not an alias for init/apply): in a TTY it shows what is
    # enabled across every reconciled area, edits the local rig.yaml AND the global config, then
    # applies; with no TTY it prints USAGE for init/apply/config (it never runs a half-wizard).
    sp = sub.add_parser(
        "setup",
        help="interactive config wizard (no TTY → prints usage for init/apply/config)",
        description="Interactive configuration wizard. In a terminal: show what is enabled across "
        "all reconciled areas, change options in the local rig.yaml AND the global "
        "~/.config/rig/config.yaml (each option carries an inline hint), then apply. "
        "Non-interactive (piped/no TTY): print usage for init/apply/config get|set.",
    )
    sp.add_argument("-C", "--cwd", default=".", help="repo root to operate on (default: cwd)")

    add_config_parser(sub)
    add_env_parser(sub)
    add_profile_parser(sub)

    # `config-web` — a local web UI over the SAME config engine as the wizard / `config set`.
    # Its lifecycle (run/start/stop/status/enable/disable + OS autostart) is delegated to the
    # SHARED agenttools-service manager; the wiring lives in config_web_service.register (NOT
    # here) so this module carries no copy of the service machinery. Lazy-imported so a missing
    # agenttools-service lib can't break `rig --help` / the rest of the CLI.
    from . import config_web_service

    config_web_service.register(sub)

    # `evolve` — local project evolution portal. Lifecycle is the same shared
    # agenttools-service manager pattern as config-web; registration stays lazy so `rig --help`
    # does not import optional service machinery.
    from .evolve import service as evolve_service

    evolve_service.register(sub)

    add_lint_parser(sub)
    add_stats_parser(sub)
    add_daily_parser(sub)
    add_usage_parser(sub)
    add_worktree_parser(sub)
    add_omp_parser(sub)
    harness_cli.add_parser(sub)
    add_report_parser(sub)

    return p


def main(argv: list[str] | None = None) -> int:
    from .stdio import ensure_utf8_stdio

    # bin/rig and `python -m riglib` land here without passing through fleet_entrypoint.
    ensure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0

    handlers = {
        "init": cmd_setup,  # init = the canonical onboarding command (the front door)
        "apply": cmd_apply,
        "activate": cmd_activate,
        "status": cmd_status,
        "doctor": cmd_doctor,
        "export": cmd_export,
        "config": cmd_config,
        "env": cmd_env,
        "profile": cmd_profile,
        "schema": cmd_schema,
        "install-skill": cmd_install_skill,
        "spotlight-sweep": cmd_spotlight_sweep,
        "setup": cmd_setup_wizard,  # setup = the interactive config wizard (distinct from init)
        "config-web": cmd_config_web,  # web UI over the config engine; lifecycle via agenttools-service
        "evolve": cmd_evolve,  # project evolution portal; lifecycle via agenttools-service
        "lint": cmd_lint,
        "stats": cmd_stats,
        "daily": cmd_daily,
        "usage": cmd_usage,
        "worktree": cmd_worktree,
        "sysmon": cmd_sysmon,
        "omp": cmd_omp,
        "harness": harness_cli.cmd_harness,
        "report": cmd_report,
    }
    # The single top-level error handler (error-system v2): any structured RigError a command
    # raises is rendered as the consistent what/why/fix block + its stable per-class exit code.
    # A non-RigError (a real bug) propagates so its traceback is visible.
    from . import errors

    return errors.guard(lambda: handlers[args.command](args))


def cmd_setup_wizard(args: argparse.Namespace) -> int:
    """`rig setup` — the interactive config wizard, or the non-interactive USAGE pointer.

    In a TTY: open the wizard (show state across all reconciled areas → change options in the
    local rig.yaml AND the global config → apply). With no TTY (piped/redirected): print usage
    for init/apply/config and exit 0 — never a half-wizard the user can't answer.
    """
    from . import setup_wizard

    if not setup_wizard.is_interactive():
        return setup_wizard.print_non_interactive_usage()

    from .detect import detect_environment

    repo_root = detect_environment(Path(args.cwd).resolve()).repo_root

    def _apply(root: Path) -> int:
        # reuse the SAME engine as `rig apply` (one executor, never forked for the wizard).
        # Build the namespace through the REAL `apply` subparser so it always carries every
        # attribute cmd_apply reads — no hand-kept field list that can drift from the parser.
        # `commit` is the EXECUTE subcommand — the wizard applies, so it must NOT preview.
        apply_args = build_parser().parse_args(["apply", "commit", "-C", str(root)])
        return cmd_apply(apply_args)

    # color hook so the wizard's rendered state matches the rest of the CLI's NO_COLOR handling.
    return setup_wizard.run_setup(repo_root, apply_fn=_apply, color=_c)


if __name__ == "__main__":
    raise SystemExit(main())
