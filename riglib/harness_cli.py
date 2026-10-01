"""``rig harness …`` — argparse wiring and thin handlers for :mod:`riglib.harness_launcher`.

``rig harness run <omp|claude|codex|opencode> [--name LABEL] [args…]`` hosts a harness session in
abduco under the rig crash supervisor and registers it with tg-ctl. ``attach``/``list`` manage the
hosted sessions; ``install-wrapper``/``exec`` are the opt-in PATH wrapper; ``host`` is the
supervisor itself (what abduco runs — internal).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import errors
from .harness_kinds import KINDS

#: options ``run`` takes (before OR after the harness name), option -> run_session keyword.
_RUN_OPTIONS = {"--name": "label", "--host-id": "host_id", "--real-bin": "real_bin", "--abduco-bin": "abduco_bin"}
_RUN_FLAGS = {"--detach": "detach"}


class _KeepRawArgs(argparse.ArgumentParser):
    """argparse drops a bare ``--`` (Python 3.13+), but ``rig harness run claude -- --name x`` needs
    it to tell rig's ``--name`` from Claude Code's own. The subparser keeps the strings it got."""

    def parse_known_args(self, args=None, namespace=None):
        parsed, extras = super().parse_known_args(args, namespace)
        parsed.raw_args = list(args or [])
        return parsed, extras


def add_parser(sub: "argparse._SubParsersAction") -> None:
    hp = sub.add_parser(
        "harness",
        help="host omp/claude/codex/opencode in abduco under a crash supervisor, registered with tg-ctl",
        description="Run a coding-agent harness so it survives a dead terminal and a crash: abduco "
        "hosts the pty, a supervisor inside the session relaunches the harness onto the SAME "
        "conversation when it is killed (bounded backoff, crash-loop cap), and the session is "
        "registered for tg-ctl so Telegram messages reach it outside tmux.",
    )
    hp.set_defaults(_harness_parser=hp)
    hsub = hp.add_subparsers(dest="harness_command", metavar="<run|attach|list|...>", parser_class=_KeepRawArgs)

    rp = hsub.add_parser(
        "run",
        help="host a harness session in abduco (connect-or-create)",
        description="rig harness run <harness> [--name LABEL] [--host-id NAME] [--detach] "
        "[--real-bin PATH] [harness args…]. Replaces this process (exec) — never returns — unless "
        "--detach, which creates the session without attaching and prints its name.",
    )
    rp.add_argument("harness", choices=KINDS)
    rp.add_argument("harness_args", nargs=argparse.REMAINDER, help="[rig options] then the harness's own arguments")
    rp.add_argument("--name", dest="label", help="human label for the session (tg shows it as the [window] outside tmux)")
    rp.add_argument("--host-id", help="abduco session name (default: <harness>-<12 hex>; restore reuses the original)")
    rp.add_argument("--detach", action="store_true", help="create the session without attaching to it")
    rp.add_argument("--real-bin", help="absolute path of the real harness binary (default: recorded/PATH)")
    rp.add_argument("--abduco-bin", default="abduco")

    ap = hsub.add_parser("attach", help="attach to a hosted session")
    ap.add_argument("name")
    ap.add_argument("--abduco-bin", default="abduco")

    lp = hsub.add_parser("list", help="list hosted sessions")
    lp.add_argument("--json", action="store_true")
    lp.add_argument("--abduco-bin", default="abduco")

    iw = hsub.add_parser(
        "install-wrapper",
        help="write the opt-in PATH wrapper for one harness (does not activate it)",
        description="Write a wrapper script that hosts interactive launches of the harness and "
        "passes everything else (print mode, subcommands, --help) straight to the real binary. "
        "Never overwrites an unmanaged file or the real binary. Putting --target earlier in PATH "
        "than the real binary is the caller's job; `rig apply` does it from `harness_wrappers`.",
    )
    iw.add_argument("harness", choices=KINDS)
    iw.add_argument("--target", required=True, help="path to write the wrapper script to")
    iw.add_argument("--real-bin", help="absolute path of the real binary to record (when not the first on PATH)")

    ep = hsub.add_parser("exec", help="the installed wrapper's own body: host-or-passthrough (internal)")
    ep.add_argument("harness", choices=KINDS)
    ep.add_argument("harness_args", nargs=argparse.REMAINDER)

    sp = hsub.add_parser("host", help="the supervisor abduco runs inside a hosted session (internal)")
    sp.add_argument("name")


def strip_separator(argv: list[str]) -> list[str]:
    """A leading bare ``--`` in an ``argparse.REMAINDER`` is the conventional "end of my flags"."""
    return argv[1:] if argv and argv[0] == "--" else argv


def take_run_options(tokens: list[str]) -> tuple[dict[str, object], int]:
    """The leading ``rig harness run`` options in ``tokens`` and the index where they end. A bare
    ``--`` ends them (and is consumed); so does the first token that is not one of them."""
    opts: dict[str, object] = {}
    i = 0
    while i < len(tokens):
        arg = tokens[i]
        name, eq, inline = arg.partition("=")
        if arg == "--":
            return opts, i + 1
        if arg in _RUN_FLAGS:
            opts[_RUN_FLAGS[arg]] = True
            i += 1
        elif name in _RUN_OPTIONS and (eq or i + 1 < len(tokens)):
            opts[_RUN_OPTIONS[name]] = inline if eq else tokens[i + 1]
            i += 1 if eq else 2
        else:
            break
    return opts, i


def split_run_options(tokens: list[str]) -> tuple[dict[str, object], list[str]]:
    """``rig harness run omp --name tg --resume X`` — the leading ``rig`` options after the harness
    name, then the harness's own arguments. A bare ``--`` ends the options."""
    opts, end = take_run_options(tokens)
    return opts, tokens[end:]


def parse_run_argv(tokens: list[str]) -> tuple[str, dict[str, object], list[str]]:
    """``[rig options] HARNESS [rig options] [--] harness args…`` -> (harness, options, args)."""
    opts, i = take_run_options(tokens)
    if i >= len(tokens):
        raise ValueError("missing harness name")
    after, harness_args = split_run_options(tokens[i + 1 :])
    return tokens[i], {**opts, **after}, harness_args


def cmd_harness(args: argparse.Namespace) -> int:
    command = getattr(args, "harness_command", None)
    if not command:
        args._harness_parser.print_help()
        return 0
    handlers = {
        "run": _run,
        "attach": _attach,
        "list": _list,
        "install-wrapper": _install_wrapper,
        "exec": _exec,
        "host": _host,
    }
    return handlers[command](args)


def _launcher():
    from . import harness_launcher

    return harness_launcher


def _fail(message: str, code: int = errors.EXIT_CONFIG) -> int:
    print(f"error: {message}", file=sys.stderr)
    return code


def _run(args: argparse.Namespace) -> int:
    try:
        harness, opts, harness_args = parse_run_argv(args.raw_args)
    except ValueError as exc:
        return _fail(str(exc))
    label = opts.get("label")
    host_id = opts.get("host_id")
    detach = bool(opts.get("detach"))
    real_bin = opts.get("real_bin")
    abduco_bin = str(opts.get("abduco_bin", "abduco"))
    hl = _launcher()
    try:
        name = hl.run_session(
            harness,
            harness_args,
            label=str(label) if label else None,
            host_id=str(host_id) if host_id else None,
            detach=detach,
            real_bin=str(real_bin) if real_bin else None,
            abduco_bin=abduco_bin,
        )
    except FileNotFoundError:
        return _fail(f"'{abduco_bin}' not found — install it (e.g. `brew install abduco`)", errors.EXIT_MISSING_DEP)
    except (ValueError, RuntimeError) as exc:
        return _fail(str(exc))
    print(name)  # only reached with --detach (an attaching run exec'd away)
    return 0


def _attach(args: argparse.Namespace) -> int:
    try:
        _launcher().attach_session(args.name, abduco_bin=args.abduco_bin)
    except FileNotFoundError:
        return _fail(f"'{args.abduco_bin}' not found — install it (e.g. `brew install abduco`)", errors.EXIT_MISSING_DEP)
    except ValueError as exc:
        return _fail(str(exc))
    return 0  # unreachable on success


def _list(args: argparse.Namespace) -> int:
    sessions, ok = _launcher().list_sessions(abduco_bin=args.abduco_bin)
    if not ok:
        return _fail(f"could not list sessions via '{args.abduco_bin}' — is it installed?", errors.EXIT_MISSING_DEP)
    if args.json:
        print(json.dumps(sessions))
        return 0
    if not sessions:
        print("no hosted harness sessions")
        return 0
    for s in sessions:
        marker = "*" if s["attached"] else ("+" if s["terminated"] else " ")
        extra = "\t".join(s.get(k, "") for k in ("harness", "label", "cwd"))
        print(f"{marker} {s['name']}\t{s['created']}\t{extra}".rstrip())
    return 0


def _install_wrapper(args: argparse.Namespace) -> int:
    hl = _launcher()
    try:
        changed = hl.install_wrapper(args.harness, Path(args.target), real_bin=args.real_bin)
    except ValueError as exc:
        return _fail(str(exc))
    print(f"{args.harness} wrapper {'written to' if changed else 'already current at'} {args.target}")
    print("not activated — put this path earlier in PATH than the real binary to enable it")
    return 0


def _exec(args: argparse.Namespace) -> int:
    try:
        _launcher().exec_or_wrap(args.harness, strip_separator(args.harness_args))
    except (ValueError, RuntimeError) as exc:
        return _fail(str(exc))
    except FileNotFoundError:
        return _fail("'abduco' not found — install it (e.g. `brew install abduco`)", errors.EXIT_MISSING_DEP)
    return 0  # unreachable on success


def _host(args: argparse.Namespace) -> int:
    from .harness_host import main as host_main

    return host_main([args.name])
