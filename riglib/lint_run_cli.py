"""argparse front-end for `rig lint [run]`: Namespace -> `RunOptions`, then emit the report.

Context: `cli.py` stays thin; the run flags are declared once here and attached to BOTH the `lint`
parser and its `run` subparser. The subparser copy uses `default=argparse.SUPPRESS`: argparse
copies a subparser's defaults over the parent's parsed values, so without it
`rig lint --json run` would silently lose `--json`.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import lint_run


def _split(values: Sequence[str] | None) -> tuple[str, ...]:
    return tuple(part.strip() for raw in values or () for part in raw.split(",") if part.strip())


class _RunFix(argparse.Action):
    """`--fix` of `rig lint [run]`: also records `run_fix`.

    `rig lint check` has its own `--fix` (same attribute, `fix`), so a bare `fix=True` cannot say WHICH
    command the flag was written for. `rig lint --fix check` must not silently turn the run-level flag
    into a `check --fix` rewrite; the dispatcher refuses it because only this action sets `run_fix`.
    """

    def __call__(self, parser, namespace, values, option_string=None):
        namespace.fix = True
        namespace.run_fix = True


def add_run_arguments(parser: argparse.ArgumentParser, *, suppress_defaults: bool) -> None:
    def default(value):
        return argparse.SUPPRESS if suppress_defaults else value

    parser.add_argument("-C", "--cwd", default=default("."), help="repo root (default: cwd)")
    parser.add_argument("--config", default=default(None), help="config file (default: ./rig.yaml + global)")
    parser.add_argument("--json", action="store_true", default=default(False),
                        help="emit the rig-lint-run/v1 JSON document")
    parser.add_argument("--plan", action="store_true", default=default(False),
                        help="print what would be installed and run; execute nothing, write nothing")
    parser.add_argument("--no-setup", action="store_true", default=default(False),
                        help="never install missing tooling; report missing tools instead")
    parser.add_argument("--fix", action=_RunFix, nargs=0, default=default(False),
                        help="run each check's safe fixer (never unsafe modes), then re-check")
    parser.add_argument("--ci", action="store_true", default=default(False),
                        help="read-only run (also implied by CI / GITHUB_ACTIONS in the environment)")
    parser.add_argument("--only", action="append", metavar="CAT[,CAT]", default=default(None),
                        help="run only these categories: lint, typecheck, format, limits, scripts")
    parser.add_argument("--skip", action="append", metavar="CAT[,CAT]", default=default(None),
                        help="skip these categories")
    parser.add_argument("--scope", action="append", metavar="DIR", default=default(None),
                        help="limit to a package directory (repeatable)")
    parser.add_argument("--timeout", type=int, metavar="SEC", default=default(None),
                        help="per-check timeout in seconds, 1..3600 (default: lint.timeout_seconds)")


def options_from(args: argparse.Namespace) -> lint_run.RunOptions:
    from . import lint_run  # the executor pulls discovery/providers; keep `rig --help` light

    return lint_run.RunOptions(
        cwd=getattr(args, "cwd", None) or ".", config=getattr(args, "config", None),
        json=bool(getattr(args, "json", False)), plan=bool(getattr(args, "plan", False)),
        no_setup=bool(getattr(args, "no_setup", False)), fix=bool(getattr(args, "fix", False)),
        # `--ci` is a switch: absent means "decide from the environment", not "not CI".
        ci=True if getattr(args, "ci", False) else None,
        only=_split(getattr(args, "only", None)), skip=_split(getattr(args, "skip", None)),
        scopes=tuple(getattr(args, "scope", None) or ()), timeout=getattr(args, "timeout", None),
    )


def _printable(text: str) -> str:
    """Escape what a UTF-8 stream cannot carry (a non-UTF-8 filename arrives as lone surrogates)."""
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def _emit(report: lint_run.RunReport, as_json: bool) -> None:
    error = report.doc.get("error")
    if as_json:
        print(json.dumps(report.doc, indent=2))
    elif error is None:
        print(_printable(report.text))
    if error is not None:
        print(_printable(report.text), file=sys.stderr)


def run(args: argparse.Namespace) -> int:
    from . import lint_run

    opts = options_from(args)
    try:
        report = lint_run.execute(opts)
    except Exception as exc:  # noqa: BLE001 - a crash must read as "analysis incomplete", never as a pass
        message = f"internal error: {type(exc).__name__}: {exc}"
        print(traceback.format_exc(), file=sys.stderr)
        report = lint_run.RunReport(
            lint_run.EXIT_INCOMPLETE,
            {"schema": lint_run.SCHEMA, "ok": False, "exit_code": lint_run.EXIT_INCOMPLETE, "error": message,
             "fix": {"requested": opts.fix}},
            f"error: {message}")
    _emit(report, opts.json)
    return report.exit_code
