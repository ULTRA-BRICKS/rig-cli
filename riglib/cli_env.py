"""`rig env list|set|del` — manage global env.vars keys (plaintext; list masks values).

Context: writes through the same config layer engine as `rig config set`.
"""
from __future__ import annotations

import argparse
import json
import sys

from .cli_config import _config_target, _read_target_yaml, _reconcile_edited_layer
from .cli_output import _dim, _err, _err_block, _mask_secret


def cmd_env(args: argparse.Namespace) -> int:
    """`rig env list|set|del` — global ``env.vars`` without dumping secrets to stdout."""
    if not getattr(args, "env_command", None):
        print(
            _err("error: `rig env` needs a subcommand: list, set, or del"),
            file=sys.stderr,
        )
        print(
            _dim("  rig env list              # keys; values masked"), file=sys.stderr
        )
        print(
            _dim("  rig env set KEY [VALUE]   # omit VALUE to read stdin"),
            file=sys.stderr,
        )
        print(
            _dim("  rig env del KEY           # remove a key, then reconcile"),
            file=sys.stderr,
        )
        return 2
    if args.env_command == "list":
        return _cmd_env_list(args)
    if args.env_command == "set":
        return _cmd_env_set(args)
    return _cmd_env_del(args)


def _cmd_env_list(args: argparse.Namespace) -> int:

    from .config import ConfigError, get_path, global_config_path

    target = global_config_path()
    try:
        data = _read_target_yaml(target) or {}
    except ConfigError as exc:
        print(_err(f"error: {exc}"), file=sys.stderr)
        return 2
    try:
        vars_block = get_path(data, "env.vars")
    except ConfigError:
        vars_block = {}
    if not isinstance(vars_block, dict):
        vars_block = {}
    items = {str(k): "" if v is None else str(v) for k, v in vars_block.items()}
    shown = items if args.show else {k: _mask_secret(v) for k, v in items.items()}
    if args.json:
        print(json.dumps(shown, sort_keys=True))
    else:
        for key in sorted(shown):
            print(f"{key}={shown[key]}")
    return 0


def _env_key_or_error(key: str) -> str | None:
    from .shell_env import ENV_VAR_KEY_PATTERN

    if not ENV_VAR_KEY_PATTERN.match(key):
        print(_err(f"error: {key!r} is not a valid shell identifier"), file=sys.stderr)
        print(
            _dim(
                "  env.vars keys are written unquoted into export KEY=... in rig.env.sh"
            ),
            file=sys.stderr,
        )
        return None
    return key


def _read_env_value(args: argparse.Namespace) -> str | None:
    """String value for ``env set``, or ``None`` after printing an error.

    Prefer stdin (pipe or ``--stdin``) so the secret never lands in argv / shell history.
    A TTY with no VALUE and no ``--stdin`` is an error — do not block waiting for a paste.
    """
    import getpass

    has_value = args.value is not None
    if args.stdin and has_value:
        print(_err("error: pass VALUE or --stdin, not both"), file=sys.stderr)
        return None
    if not args.stdin and has_value:
        return args.value
    if sys.stdin.isatty() and not args.stdin:
        print(_err(f"error: missing value for {args.key}"), file=sys.stderr)
        print(
            _dim("  pass VALUE, pipe it, or `rig env set KEY --stdin`"), file=sys.stderr
        )
        return None
    if sys.stdin.isatty():
        try:
            raw = getpass.getpass(f"{args.key}: ")
        except EOFError:
            print(_err("error: missing value on stdin"), file=sys.stderr)
            return None
        return raw
    raw = sys.stdin.read()
    if raw.endswith("\n"):
        raw = raw[:-1]
    if raw.endswith("\r"):
        raw = raw[:-1]
    return raw


def _cmd_env_mutate(
    args: argparse.Namespace, *, key: str, op: str, value: str | None
) -> int:
    """Apply ``set_path`` / ``del_path`` on ``env.vars.<key>`` then the shared reconcile."""
    from .config import ConfigError, del_path, set_path, validate

    args.is_global = True
    try:
        # Global file only — env is machine-wide, so this must work from `cd ~` (no git repo).
        target, _repo_root = _config_target(args, need_repo=False)
    except Exception as exc:  # noqa: BLE001
        print(_err(f"error: {type(exc).__name__}: {exc}"), file=sys.stderr)
        return 2

    config_path = f"env.vars.{key}"
    try:
        data = _read_target_yaml(target) or {}
        data.pop("scope", None)
        if op == "set":
            set_path(data, config_path, value)
        else:
            del_path(data, config_path)
        validate(data)
    except ConfigError as exc:
        if op == "del":
            print(_err(f"error: {exc}"), file=sys.stderr)
        else:
            print(_err_block(exc))
        return 2
    except Exception as exc:  # noqa: BLE001
        print(_err(f"error: {type(exc).__name__}: {exc}"))
        return 2

    write = not bool(getattr(args, "preview", False))
    apply = write and not bool(getattr(args, "no_apply", False))
    if op == "set":
        # Never interpolate the value: CodeQL taints getpass/stdin as a password, and
        # even a last-4 mask still logs secret bytes. Confirm by key + path only.
        would = f"Would set {config_path}  → {target}"
        ok = f"set {config_path}  → {target}"
    else:
        would = f"Would del {config_path}  → {target}"
        ok = f"del {config_path}  → {target}"
    return _reconcile_edited_layer(
        cwd=args.cwd,
        target=target,
        is_global=True,
        data=data,
        write=write,
        apply=apply,
        full_plan=getattr(args, "plan", False),
        would_line=would,
        ok_line=ok,
        preview_hint="(preview only: config and disk are unchanged)",
        allow_repo_autodiscovery_in_non_git=False,
        include_repo=False,
        only_kinds=frozenset({"provision_env"}),
    )


def _cmd_env_set(args: argparse.Namespace) -> int:
    key = _env_key_or_error(args.key)
    if key is None:
        return 2
    value = _read_env_value(args)
    if value is None:
        return 2
    return _cmd_env_mutate(args, key=key, op="set", value=value)


def _cmd_env_del(args: argparse.Namespace) -> int:
    key = _env_key_or_error(args.key)
    if key is None:
        return 2
    return _cmd_env_mutate(args, key=key, op="del", value=None)
