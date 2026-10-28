"""`rig export`, `rig schema` and `rig config get|set` handlers.

Context: the headless single-key editor; `set` reconciles through the SAME plan + apply engine as `rig apply`.
"""
from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from .cli_output import _bold, _dim, _err, _err_block, _fmt_scalar, _ok
from .cli_plan_print import (
    _load_plan,
    _print_plan,
    _print_results,
    _validate_layer_in_isolation,
)

if TYPE_CHECKING:
    from .schema import Option


def cmd_export(args: argparse.Namespace) -> int:
    from .detect import detect_environment
    from .state import SetupState

    env = detect_environment(Path(args.cwd).resolve())
    repo_root = env.repo_root  # write/anchor at the git root, like setup/apply/status
    # resolve the output path against the repo root (relative -o is relative to -C/git root,
    # not cwd) BEFORE the collision check, so the check and the write target the same file.
    out_raw = Path(args.output)
    out = out_raw if out_raw.is_absolute() else repo_root / out_raw
    if out.exists() and not args.force:
        print(_err(f"error: {out} exists (use --force to overwrite)"))
        return 2
    # exported rig.yaml stays portable: do NOT pin an auto-detected absolute source.
    state = SetupState.default(agent_tools_source=None, project_type=env.project_type)
    written = state.write(out)
    print(_ok(f"wrote {written}"))
    print(_dim("  edit it, then: rig apply"))
    return 0


def _config_target(args: argparse.Namespace, *, need_repo: bool) -> tuple[Path, Path | None]:
    """Resolve (config_file, repo_root) for a `config get|set` invocation.

    --global → the XDG global config; otherwise the per-repo ./rig.yaml at the detected git
    root (so the command works from any subdirectory, like apply/status).

    ``need_repo`` gates the repo lookup: ``set`` always reconciles the repo afterwards (even a
    --global edit must converge the repo in front of you), so it needs the root. A ``get
    --global`` does NOT touch any repo, so it must NOT require one — ``detect_environment``
    is skipped, and ``rig config get … --global`` works outside any git repo.
    """
    from .config import global_config_path, repo_config_path
    from .detect import detect_environment

    if args.is_global and not need_repo:
        return global_config_path(), None
    env = detect_environment(Path(args.cwd).resolve())
    target = global_config_path() if args.is_global else repo_config_path(env.repo_root)
    return target, env.repo_root


def _read_target_yaml(path: Path):
    """Parse a single config file to a dict (NOT the cascade), or None if it doesn't exist.

    Delegates to the loader's single-file reader, which already wraps YAML errors and rejects
    a non-mapping top level as a fail-closed :class:`ConfigError`.
    """
    from .config import read_yaml_file

    if not path.is_file():
        return None
    return read_yaml_file(path)


def cmd_schema(args: argparse.Namespace) -> int:
    """`rig schema` — print / verify / regenerate the rig.yaml JSON Schema.

    Default: print the generated schema to stdout (pipe it into a file or an editor config).
    ``--check``: exit 2 if the committed ``schema/rig.schema.json`` is missing or stale (the same
    drift the sync test catches — usable as a CI gate). ``--write``: regenerate the committed file.
    Bare print needs no repo; --check/--write operate on rig's OWN packaged schema file.
    """
    from . import config_schema

    if args.write:
        try:
            path = config_schema.write_schema_file()
        except OSError as exc:
            # a bare pip install puts the path under a (possibly read-only) site-packages; the
            # canonical symlink install always writes fine. Fail clearly, don't crash.
            print(_err(f"error: cannot write {config_schema.schema_file_path()}: {exc}"))
            print(_dim("  the schema file lives in the rig-cli checkout (symlink install); "
                       "run from there, or redirect: rig schema > schema/rig.schema.json"))
            return 2
        print(_ok(f"wrote {path}"))
        return 0
    if args.check:
        if config_schema.schema_file_in_sync():
            print(_ok(f"{config_schema.schema_file_path()} is in sync with the registry"))
            return 0
        print(_err(f"error: {config_schema.schema_file_path()} is missing or out of sync"))
        print(_dim("  regenerate it with: rig schema --write"))
        return 2
    print(config_schema.render_schema_json(), end="")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    if not getattr(args, "config_command", None):
        print(_err("error: `rig config` needs a subcommand: get or set"))
        print(_dim("  rig config get <dot.path>           # read a key"))
        print(_dim("  rig config set <dot.path> <value>   # write a key, then reconcile"))
        return 2
    if args.config_command == "get":
        return _cmd_config_get(args)
    return _cmd_config_set(args)


def _cmd_config_get(args: argparse.Namespace) -> int:
    from .config import ConfigError, canonical_dot_path, get_path

    try:
        target, _repo_root = _config_target(args, need_repo=False)
        data = _read_target_yaml(target)
        if data is None:
            raise ConfigError(f"config file not found: {target}")
        config_path = canonical_dot_path(args.path)
        value = get_path(data, config_path)
    except ConfigError as exc:
        # diagnostics go to STDERR so `get --json | jq` keeps a clean stdout (the exit code
        # already signals failure); this matters most for the machine-readable --json path.
        print(_err(f"error: {exc}"), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        # symmetric with set: anything unexpected (e.g. environment detection failing outside a
        # git repo on a non-global get) must fail closed with a message, not a traceback.
        print(_err(f"error: {type(exc).__name__}: {exc}"), file=sys.stderr)
        return 2

    if args.json:
        import json

        # default=str so a YAML date/datetime (or any non-JSON scalar) serializes instead of
        # throwing an unhandled TypeError — get must always fail soft on output.
        print(json.dumps(value, default=str))
    elif isinstance(value, (dict, list)):
        # a subtree: dump it as YAML so the structure is readable (and re-feedable to set's
        # scalar children). Lazy yaml import keeps the no-yaml path light.
        import yaml

        print(yaml.safe_dump(value, sort_keys=False, default_flow_style=False).rstrip())
    else:
        print(_fmt_scalar(value))  # YAML/CLI casing for bool/null, str otherwise
    return 0


def _global_layer_refusal(option: "Option | None", category: str) -> str | None:
    """The reason a repo-local `config set` of this key is refused, or ``None`` when it is allowed.

    Same precedence as :attr:`riglib.schema.Option.layer` (the property the wizard and
    config-web route by): a registered option decides by ITS layer (a per-option override
    wins over its category, rig-cli#372); an unregistered key falls back to the category.
    One helper decides BOTH the gate and its wording, so they can't drift apart.
    """
    from .layers import GLOBAL
    from .schema import writable_layer_for_category

    if option is not None:
        if option.layer != GLOBAL:
            return None
        if writable_layer_for_category(category) == GLOBAL:
            return f"`{category}` is a global-only config block"
        return f"`{option.key}` is a global-only option"
    if writable_layer_for_category(category) == GLOBAL:
        return f"`{category}` is a global-only config block"
    return None


def _print_plan_error(exc: Exception) -> None:
    from .catalog import CatalogError
    from .config import ConfigError
    from .plan import PlanError

    if isinstance(exc, ConfigError):
        print(_err_block(exc))
        return
    kind = "" if isinstance(exc, (CatalogError, PlanError, OSError)) else f"{type(exc).__name__}: "
    print(_err(f"error: {kind}{exc}"))


def _plan_layer_edit(
    *,
    cwd: str,
    target: Path,
    is_global: bool,
    data: dict,
    allow_repo_autodiscovery_in_non_git: bool,
    include_repo: bool | None,
    only_kinds: frozenset[str] | None,
):
    """Build the plan against the in-memory edit; ``None`` (after printing why) when it cannot be built."""
    try:
        if is_global:
            _validate_layer_in_isolation(target, data_override=data)
        plan, _loaded, _env = _load_plan(
            cwd, config=None, layer_overrides={target.resolve(): data},
            allow_repo_autodiscovery_in_non_git=allow_repo_autodiscovery_in_non_git,
            include_repo=include_repo,
        )
        if only_kinds is not None:
            plan.actions = [a for a in plan.actions if a.kind in only_kinds]
    except Exception as exc:  # noqa: BLE001
        _print_plan_error(exc)
        return None
    return plan


def _restore_layer(target: Path, original: str | None, parent_existed: bool) -> bool:
    """Put the layer file back as it was before the rejected edit; False when that itself failed."""
    try:
        if original is None:
            target.unlink(missing_ok=True)
            if not parent_existed:
                with contextlib.suppress(OSError):
                    target.parent.rmdir()
        else:
            target.write_text(original, encoding="utf-8")
    except OSError:
        return False
    return True


def _print_write_failure(exc: Exception, target: Path, restored: bool) -> None:
    from .config import ConfigError

    if isinstance(exc, ConfigError):
        print(_err_block(exc))
    else:
        print(_err(f"error: {type(exc).__name__}: {exc}"))
    if restored:
        print(_dim(f"  (config not changed — {target} left untouched)"))
    else:
        print(_err(f"  WARNING: could not restore {target} — it may contain the rejected edit"))


def _write_layer_or_rollback(target: Path, data: dict, is_global: bool) -> bool:
    """Write the edited layer; on any failure restore the previous file and report. True when written."""
    from .state import SetupState

    original = target.read_text(encoding="utf-8") if target.is_file() else None
    parent_existed = target.parent.exists()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        state = SetupState.from_dict(data)
        if is_global:
            target.write_text(state.to_yaml(), encoding="utf-8")
        else:
            state.write(target)
    except Exception as exc:  # noqa: BLE001
        _print_write_failure(exc, target, _restore_layer(target, original, parent_existed))
        return False
    return True


def _reconcile_edited_layer(
    *,
    cwd: str,
    target: Path,
    is_global: bool,
    data: dict,
    write: bool,
    apply: bool,
    full_plan: bool,
    would_line: str,
    ok_line: str,
    preview_hint: str,
    allow_repo_autodiscovery_in_non_git: bool = True,
    include_repo: bool | None = None,
    only_kinds: frozenset[str] | None = None,
) -> int:
    """Plan (always) then optionally write + reconcile an already-validated layer edit.

    Shared by ``config set`` and ``env set|del`` so there is one YAML-write / rollback /
    run_plan path. ``write=False`` is the preview: the plan is built in memory against
    ``data`` and nothing hits disk.
    """
    from .actions import run_plan

    plan = _plan_layer_edit(
        cwd=cwd, target=target, is_global=is_global, data=data,
        allow_repo_autodiscovery_in_non_git=allow_repo_autodiscovery_in_non_git,
        include_repo=include_repo, only_kinds=only_kinds,
    )
    if plan is None:
        return 2
    if not write:
        print(_bold(would_line))
        _print_plan(plan, full=full_plan, preview=True)
        print(_dim(f"\n{preview_hint}"))
        return 0
    if not _write_layer_or_rollback(target, data, is_global):
        return 2

    print(_ok(ok_line))
    _print_plan(plan, full=full_plan)
    if not apply:
        print(_dim("\n(--no-apply: config written, nothing reconciled)"))
        return 0
    report = run_plan(plan)
    _print_results(report)
    return 1 if report.errors else 0


def _set_target(args: argparse.Namespace):
    """Resolve ``(target file, canonical dot path)`` for ``config set``; ``None`` after printing why not."""
    from .config import ConfigError, canonical_dot_path

    try:
        target, _repo_root = _config_target(args, need_repo=True)
        return target, canonical_dot_path(args.path)
    except ConfigError as exc:
        print(_err(f"error: {exc}"), file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — environment detection failing must fail soft
        print(_err(f"error: {type(exc).__name__}: {exc}"), file=sys.stderr)
    return None


def _set_refusal(args: argparse.Namespace, target: Path, config_path: str, option: "Option | None") -> bool:
    """Print the reason and return True when ``config set`` must not touch this key/file."""
    category = config_path.split(".", 1)[0]
    refusal = None if args.is_global else _global_layer_refusal(option, category)
    if refusal is not None:
        print(_err(f"error: {refusal}; use `--global`."), file=sys.stderr)
        print(_dim("  global-only settings are written to ~/.config/rig/config.yaml, never ./rig.yaml."),
              file=sys.stderr)
        return True

    # Refuse a repo-local `set` when ./rig.yaml does not exist yet: starting from {} and
    # reconciling would let built-in defaults mutate disk with no committed source of truth —
    # the same hazard `rig apply`'s no-config guard prevents. `rig init` (or `rig export`)
    # creates the file first. (--global may legitimately create the machine-wide file, so it is
    # not guarded here; its second gate still validates over the cascade.)
    if not args.is_global and not target.is_file():
        print(_err(f"error: no {target} — run `rig init` (or `rig export -o rig.yaml`) first."))
        print(_dim("  `config set` edits an existing committed config; it does not bootstrap one."))
        return True

    # `scope` is a removed key (the cascade is location-based). Refuse to SET it — the
    # recommended editor must never (re)introduce a dead setting, even though the loader still
    # tolerates it in old committed files.
    if config_path == "scope" or config_path.startswith("scope."):
        print(_err("error: `scope` is a removed setting — the cascade is by location "
                   "(global vs repo), not a flag. Nothing to set."), file=sys.stderr)
        return True
    return False


def _edited_tree(args: argparse.Namespace, target: Path, config_path: str, option: "Option | None"):
    """The whole target config with the new value set and schema-validated; ``None`` after printing why not."""
    from .config import ConfigError, coerce_scalar, set_path, validate
    from .schema import coerce as coerce_option

    try:
        data = _read_target_yaml(target) or {}
        # drop any legacy `scope` already in the file (mirrors config.load): we re-serialize the
        # whole file, so leaving it would re-emit a key the schema no longer recognizes.
        data.pop("scope", None)
        if option is not None:
            try:
                value = coerce_option(option, args.value)
            except ValueError as exc:
                raise ConfigError(str(exc), schema_path=config_path) from exc
        else:
            value = coerce_scalar(args.value)
        set_path(data, config_path, value)
        # First gate: schema validation of the WHOLE edited tree (enum/type checks). This
        # catches e.g. harness.auto_mode="yes" before anything touches disk.
        validate(data)
    except ConfigError as exc:
        print(_err_block(exc))
        return None
    except Exception as exc:  # noqa: BLE001
        # nothing has touched disk yet (read/coerce/validate only). Anything unexpected here
        # must still fail closed with a message, not a traceback.
        print(_err(f"error: {type(exc).__name__}: {exc}"))
        return None
    return data, value


def _cmd_config_set(args: argparse.Namespace) -> int:
    from .schema import option_for_key

    resolved = _set_target(args)
    if resolved is None:
        return 2
    target, config_path = resolved
    option = option_for_key(config_path)
    if _set_refusal(args, target, config_path, option):
        return 2
    edited = _edited_tree(args, target, config_path, option)
    if edited is None:
        return 2
    data, value = edited

    return _reconcile_edited_layer(
        cwd=args.cwd,
        target=target,
        is_global=args.is_global,
        data=data,
        write=bool(args.commit or args.no_apply),
        apply=bool(args.commit),
        full_plan=getattr(args, "plan", False),
        would_line=f"Would set {config_path} = {_fmt_scalar(value)}  → {target}",
        ok_line=f"set {config_path} = {_fmt_scalar(value)}  → {target}",
        preview_hint="(preview only: config and disk are unchanged; add --commit to write + reconcile)",
    )
