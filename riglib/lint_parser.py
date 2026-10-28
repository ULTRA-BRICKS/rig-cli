"""argparse wiring and dispatch for `rig lint`: `run` (default), `check`, `rules`.

Context: `cli.py` only hooks this module in. Import-light on purpose (argparse only; every engine is
imported inside `dispatch`) because the parser is built on every `rig` invocation.

Flags the run parser shares with `check`/`rules` (-C, --config, --json, --fix) use
`default=argparse.SUPPRESS` on those subparsers: argparse copies a subparser's defaults over the
parent's parsed values, so without it `rig lint --json check` would silently lose `--json`. The
parent parser holds the real defaults, so `check`/`rules` still always see every attribute.
"""
from __future__ import annotations

import argparse
import sys

from .lint_run_cli import add_run_arguments

_EPILOG = (
    "exit codes (run):\n"
    "  0    every applicable check passed and every source file was verified (or the run was narrowed on purpose)\n"
    "  1    at least one check reported findings\n"
    "  2    usage or config error (also --fix with --plan/--ci or partially staged files)\n"
    "  3    analysis incomplete (timeout, crash, tool config error, malformed output or manifest, failed setup)\n"
    "  127  a check could not run (tool missing, platform unsupported) or source files were not verified"
)
_CHECK_EPILOG = (
    "exit codes:\n"
    "  0    no error-severity finding (warnings allowed) or check disabled\n"
    "  1    at least one error finding\n"
    "  2    usage or config error (also --fix refused with --staged / partial staging)\n"
    "  3    analysis incomplete (parse error, analyzer crash, malformed output, coverage gap)\n"
    "  127  analyzer missing or not byte-identical to the pin (run `rig apply commit`)"
)


def _add_shared(parser: argparse.ArgumentParser, *, json_help: str) -> None:
    parser.add_argument("-C", "--cwd", default=argparse.SUPPRESS, help="repo root (default: cwd)")
    parser.add_argument("--config", default=argparse.SUPPRESS, help="config file (default: ./rig.yaml + global)")
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=json_help)


def add_lint_parser(sub: "argparse._SubParsersAction") -> None:
    lp = sub.add_parser(
        "lint", help="run every applicable lint/format/type check; inspect lint policy",
        description="Run every applicable lint, format, typecheck and limit check for the repository "
        "(bare `rig lint` == `rig lint run`). `rig lint check` runs complexity/file-line limits "
        "alone; `rig lint rules` shows the effective rule policy.",
        epilog=_EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_run_arguments(lp, suppress_defaults=False)
    lsub = lp.add_subparsers(dest="lint_command", metavar="<action>")
    rup = lsub.add_parser("run", help="run every applicable check (default when no action is given)")
    add_run_arguments(rup, suppress_defaults=True)
    rp = lsub.add_parser("rules", help="show the effective lint rule policy and provider selection")
    rp.add_argument("rule", nargs="?", help="show one exact rule name")
    _add_shared(rp, json_help="emit machine-readable JSON")
    cp = lsub.add_parser(
        "check",
        help="check complexity (pinned cccc) and file-line limits",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Check cognitive/cyclomatic complexity and physical file-line limits for the files in\n"
            "scope (tracked + untracked-not-ignored). Limits come from the lint_checks config block."
        ),
        epilog=_CHECK_EPILOG,
    )
    cp.add_argument("paths", nargs="*", metavar="PATH", help="limit to these files/directories (default: whole repo)")
    _add_shared(cp, json_help="emit the rig-lint-check/v1 JSON document")
    cp.add_argument("--staged", action="store_true", help="check the staged (index) content, not the working tree")
    cp.add_argument("--fix", action="store_true", default=argparse.SUPPRESS,
                    help="apply the safe fix/else-after-return TS/JS rewrite, then re-check")


_RUN_ONLY_OPTIONS = (
    ("plan", "--plan"), ("no_setup", "--no-setup"), ("ci", "--ci"), ("only", "--only"),
    ("skip", "--skip"), ("scope", "--scope"), ("timeout", "--timeout"),
)


def _was_given(value: object) -> bool:
    # Identity against the parser defaults (False for switches, None for valued flags): truthiness would
    # let `--timeout 0` (or an empty `--only ""`) slip through as "not given".
    return value is not None and value is not False


def _misplaced_run_options(args: argparse.Namespace) -> list[str]:
    flags = [flag for attr, flag in _RUN_ONLY_OPTIONS if _was_given(getattr(args, attr, None))]
    # `--fix` written BEFORE the action is the run-level flag; `rig lint check --fix` (its own flag) is untouched.
    return (["--fix"] if getattr(args, "run_fix", False) else []) + flags


def dispatch(args: argparse.Namespace) -> int:
    action = getattr(args, "lint_command", None)
    if action in ("check", "rules"):
        misplaced = _misplaced_run_options(args)
        if misplaced:
            print(f"rig lint {action}: {', '.join(misplaced)} only applies to `rig lint run`", file=sys.stderr)
            return 2
    if action == "check":
        from .lint_check import run as check_run

        return check_run(args)
    if action == "rules":
        from .lint_rules_cli import run as rules_run

        return rules_run(args)
    from .lint_run_cli import run as run_run

    return run_run(args)
