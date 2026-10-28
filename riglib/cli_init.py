"""`rig init` — first-run onboarding: resolve the init plan, persist rig.yaml, preview or apply.

Context: the front-door command; interactivity (TUI / semi / headless) is decided here.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from .cli_output import _bold, _dim, _dim_cmd, _err, _err_block, _ok, _warn
from .cli_plan_print import (
    _load_plan,
    _print_plan,
    _print_results,
    _run_and_print_verify,
)
from .cli_tui import _missing_tui_deps, _tui_importable, _tui_opted_out


def cmd_setup(args: argparse.Namespace) -> int:
    # `rig init` front door. The split:
    #   • a bare `rig init` (no --config/--yes/--apply/--dry-run) is INTERACTIVE → the TUI
    #     wizard, the user's control surface.
    #   • any explicit signal (--config / --yes / --apply / --dry-run) is HEADLESS.
    #   • a bare `rig init` with NO way to run the wizard (no TTY, or textual not installed) has
    #     NO instructions AND no wizard, so it must NOT silently scaffold+apply — it shows a
    #     non-destructive PREVIEW (writes nothing, applies nothing) and how to proceed.
    from .setup_wizard import is_interactive

    interactive = not (args.config or args.yes or args.dry_run or getattr(args, "apply", False))
    if interactive:
        # the wizard needs a TTY on BOTH ends (stdin to read, stdout to draw). A piped / CI /
        # agent run has no TTY, so a fullscreen TUI would hang — fall to the preview instead of
        # doing anything surprising (mirrors `rig setup`'s non-interactive degradation).
        if not is_interactive():
            return _setup_preview_no_tui(args, reason="no-tty")
        # explicit opt-out (`--no-tui` / RIG_NO_TUI): just the non-destructive preview, so
        # automation that sets it never gets a surprise fullscreen wizard.
        if getattr(args, "no_tui", False) or _tui_opted_out():
            return _setup_preview_no_tui(args, reason="no-tui")
        # textual ships WITH rig (a CORE dependency), so a TTY `rig init` launches the wizard
        # directly — no install step, no hint. It is missing only on a genuinely broken env; that
        # degrades to a one-line message + the non-destructive preview rather than crashing.
        if not _tui_importable():
            return _setup_preview_no_tui(args, reason="no-textual")
        # `.tui` re-exports run_wizard but imports textual ONLY inside the call, so this
        # import is cheap and stdlib-safe; the textual ImportError surfaces on invocation.
        from .tui import run_wizard

        try:
            # thread --stack through so a TTY `rig init --stack …` is honored, not silently
            # dropped at the interactive boundary (the headless path already respects it).
            return run_wizard(Path(args.cwd).resolve(), stack=getattr(args, "stack", None))
        except ImportError:
            return _setup_preview_no_tui(args, reason="no-textual")

    return _setup_headless(args, use_default=args.config is None)


def _resolve_init_plan(args: argparse.Namespace, *, use_default: bool):
    """Resolve ``(plan, loaded, env, repo_yaml, pending_write)`` for `rig init`.

    Pure resolver: it scans the catalog and builds the plan but writes NOTHING and applies
    NOTHING. Raises ConfigError/CatalogError/PlanError on a bad config (the caller renders the
    error + maps it to an exit code). ``pending_write`` records how rig.yaml WOULD be persisted
    (``("generated", SetupState)`` | ``("copy", src_path)`` | ``None``), so the headless path can
    persist it fail-closed only AFTER the plan is proven valid.

    The tuple LEADS with ``plan`` to match :func:`_load_plan` (``(plan, loaded, env)``) — both
    plan-resolvers share one convention so a caller can't mix them up.
    """
    from .catalog import Catalog
    from .config import (
        ConfigError,
        LoadedConfig,
        load,
        read_yaml_file,
        resolve_init_stack,
        validate,
    )
    from .detect import detect_environment
    from .plan import build
    from .state import SetupState

    # detect_environment resolves repo_root to the git top-level; use THAT consistently for
    # config load, rig.yaml writes, and install targets, so setup and later apply/status
    # (which also resolve to the git root) operate on the same paths and reproduce.
    env = detect_environment(Path(args.cwd).resolve())
    repo_root = env.repo_root
    repo_yaml = repo_root / "rig.yaml"
    pending_write = None
    if use_default:
        # honor a global-config-pinned agent_tools_source for the scan (the documented cascade
        # has a valid global layer), but do NOT pin it into the committed rig.yaml — that would
        # break the env/default fallback on other machines.
        global_cfg = load(repo_root, include_global=True)
        global_source = global_cfg.agent_tools_source
        Catalog.scan(global_source)  # verify an agent-tools checkout exists (fail early)
        # Stack preset: an explicit --stack wins, else the global default (cascade), else a
        # best-guess from the repo files. Written into the committed rig.yaml so the by-stack
        # skills are selected; left unset (→ soft-require warning) when nothing is known. The
        # interactive TUI seeds its state through the SAME helper, so both front-ends agree.
        stack = resolve_init_stack(
            repo_root, explicit=getattr(args, "stack", None), global_stack=global_cfg.stack
        )
        state = SetupState.default(
            agent_tools_source=None, project_type=env.project_type, stack=stack
        )
        # Build the plan from the GENERATED state, not from disk: with --dry-run there may be no
        # rig.yaml on disk yet, so loading from disk would preview an empty/stale plan instead of
        # what setup decided. Carry the global source into the in-memory config so the catalog
        # scan below resolves it.
        validate(state.data)
        data = dict(state.data)
        if global_source:
            data["agent_tools_source"] = global_source
        loaded = LoadedConfig(data=data, repo_root=repo_root)
        pending_write = ("generated", state)
    else:
        # a relative --config is relative to the -C repo root (mirrors `rig apply`).
        cp = Path(args.config)
        explicit = (cp if cp.is_absolute() else repo_root / cp).resolve()
        template_data = read_yaml_file(explicit)
        if "mode" in template_data:
            raise ConfigError(
                "mode is a global-only config block",
                why=f"{explicit} was passed to `rig init --config`, which copies the template into ./rig.yaml",
                fix="move mode to ~/.config/rig/config.yaml and remove it from the init template",
                schema_path="mode",
            )
        loaded = load(repo_root, explicit_config=explicit)
        # rig.yaml is committed-by-default: setting up from an external template must leave the
        # repo with its own rig.yaml. Copy it in unless it already IS it.
        if explicit != repo_yaml.resolve():
            pending_write = ("copy", explicit)
    catalog = Catalog.scan(loaded.agent_tools_source)
    plan = build(loaded, catalog, project_type=env.project_type, stack_preset=env.stack_preset)
    return plan, loaded, env, repo_yaml, pending_write


def _persist_rig_yaml(pending_write, repo_yaml: Path) -> None:
    """Write the committed rig.yaml from a resolved ``pending_write`` (generated | copy)."""
    kind, payload = pending_write
    if kind == "generated":
        written = payload.write(repo_yaml)
        print(_ok(f"wrote {written}"))
        return
    import shutil

    repo_yaml.parent.mkdir(parents=True, exist_ok=True)
    # never silently discard an existing committed config — back it up first.
    if repo_yaml.is_file():
        bak = repo_yaml.with_name(f"rig.yaml.rig-bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(str(repo_yaml), str(bak))
        print(_warn(f"  backed up existing rig.yaml → {bak}"))
    shutil.copyfile(payload, repo_yaml)
    print(_ok(f"wrote {repo_yaml}  (from {payload})"))


def _print_init_next_steps(
    *,
    config_exists: bool,
    reason: str,
    textual_is_too_old: bool = False,
    missing_names: list[str] | None = None,
) -> None:
    """The 'nothing was done — here is how to proceed' block for the no-TUI PREVIEW path.

    The last line is ``reason``-aware: under ``no-textual`` rig's environment is missing its
    own core dep (textual ships WITH rig), OR has a present-but-too-old one
    (``textual_is_too_old`` — computed once by the caller, ``_setup_preview_no_tui``, which
    already needs the same fact for its diagnosis line; not re-derived here, a review
    finding on the scattered-classification bug class this codebase kept re-hitting), and we
    point at the real fix — which, on a modern PEP-668 externally-managed Python, is NOT the
    same one-liner as on a managed install (see the fallback branch below); under ``no-tui``
    the user opted out (``--no-tui`` / ``RIG_NO_TUI``) so we point at dropping the flag;
    under ``no-tty`` (piped / CI / agent) the wizard can't run at all, so we point at a real
    terminal. ``missing_names`` is likewise threaded through from the caller's ALREADY-computed
    ``_missing_tui_deps()`` call rather than re-probing the environment a second time here for
    the identical fact (another instance of the same review finding).
    """
    print(_bold("\nNothing was written and nothing was applied — this is a PREVIEW."))
    print("To proceed, pick one:")
    if config_exists:
        print(f"  {_dim('•')} rig.yaml already exists here  →  run `rig apply commit` to apply it")
    else:
        print(f"  {_dim('•')} rig init --yes           write rig.yaml (config only; then `rig apply commit`)")
        print(f"  {_dim('•')} rig init --yes --apply   write rig.yaml AND apply the plan in one step")
    if reason == "no-textual":
        from .doctor import (
            break_system_packages_command_for,
            externally_managed,
            textual_upgrade_command,
        )

        # In practice `missing_names` is always non-empty here — the sole caller
        # (`_setup_preview_no_tui`'s own body, above) already applies
        # `_missing_tui_deps() or ["textual"]` before reaching this branch, and ALSO relies
        # on that same non-empty list itself (the `set(missing_names) == {"textual"}` check
        # and the diagnosis line's word-join both need it) — so it can't be removed from the
        # caller side either. The `or ["textual"]` fallback HERE is this function's OWN
        # defensive default, not a duplicate of the caller's: a review finding (Opus, round
        # 19) caught that a bare `missing = missing_names` (an earlier "de-duplication" fix,
        # round 16-18) left a latent `TypeError: 'NoneType' is not iterable` trap for any
        # FUTURE second caller that passes the documented `None` default — this function
        # must defend its own contract regardless of what today's one caller happens to
        # already guarantee. (A prior comment here also named a nonexistent `cmd_init`
        # function — round 18 — the real dispatcher is `cmd_setup`.)
        missing = missing_names or ["textual"]
        # `pipx install hyper-rig` is NOT a clean fix without this caveat, in EITHER branch
        # below that offers it as the "switch to a managed install" alternative:
        # `install.sh`'s symlink shape already occupies `~/.local/bin/rig`, and pipx
        # typically leaves an EXISTING app link alone rather than replacing it — so a bare
        # "pipx install hyper-rig" can leave the user still invoking the same broken/too-old
        # checkout afterward (a review finding, found twice: the caveat was present in only
        # one of the two branches that needed it). One string so the two copies can't drift
        # apart again.
        managed_install_line = f"  {_dim('•')} or switch to a managed install: remove ~/.local/bin/rig, then pipx install hyper-rig"
        if externally_managed():
            # Lead with the command that actually fixes THIS checkout.
            fallback = break_system_packages_command_for(missing)
            print(f"  {_dim('•')} developing on a local rig checkout? this Python refuses direct installs (PEP 668) —")
            print(f"        run: {_dim_cmd(fallback)}")
            print(managed_install_line)
        elif textual_is_too_old:
            # A review finding: this branch used to ONLY ever suggest `pipx install hyper-rig` /
            # `uv tool install hyper-rig` — fine for a genuinely ABSENT textual (a fresh managed
            # install brings a current one), but a REINSTALL of rig into a NEW managed venv
            # never touches an existing dev-checkout install's CURRENT interpreter, so a
            # too-old textual there survived every re-run with the identical "too old"
            # message — the exact remediation-loop class this whole mechanism exists to
            # close, just reopened on this one branch.
            print(f"  {_dim('•')} upgrade textual in THIS interpreter —")
            print(f"        run: {_dim_cmd(textual_upgrade_command())}")
            print(managed_install_line)
        else:
            print(f"  {_dim('•')} pipx install hyper-rig   (recommended — reinstalls rig into an isolated, managed env)")
            print(f"  {_dim('•')} or: uv tool install hyper-rig   (then re-run `rig init`)")
            # A review finding: this branch's suggestions hit the SAME stale-symlink problem
            # `managed_install_line` above exists for — a dev-checkout install
            # (install.sh's `~/.local/bin/rig` symlink) with textual genuinely absent, on a
            # non-managed interpreter, isn't actually fixed by either suggestion above
            # (pipx/uv-tool create a SEPARATE managed environment and leave the existing
            # symlink pointed at the same checkout). A direct interpreter-targeted command
            # for this specific combination is tracked separately (rig-cli#302, not yet
            # built) — until then, say so explicitly rather than silently implying the
            # suggestions above always work.
            print(f"  {_dim('•')} still broken after that? remove ~/.local/bin/rig first, then retry")
    elif reason == "no-tui":
        print(f"  {_dim('•')} drop --no-tui (and unset RIG_NO_TUI), then re-run `rig init` to choose interactively")
    else:  # no-tty: the wizard needs a real terminal; installing textual would not help here
        print(f"  {_dim('•')} run `rig init` from an interactive terminal (TTY) to choose interactively")


def _textual_too_old_not_absent(missing_names: list[str]) -> bool:
    """True when the ONLY reported dependency is a textual that is importable but below the floor.

    ``_missing_tui_deps()`` folds "present but too old" and "genuinely absent" into the SAME list
    (the remediation command is the same reinstall either way), but the DIAGNOSIS line must say
    which one it is, or a user with an old-but-installed textual goes hunting for a "missing"
    package that isn't missing at all. Only textual can be "too old" (rich has no version gate),
    and ``textual_too_old()`` alone fails closed (True) for an absent textual too, so
    ``find_spec`` must confirm it is importable first. ``set(...)``, not a list-equality check:
    robust to a future reorder of ``_missing_tui_deps()``'s iteration.
    """
    import importlib.util

    from .doctor import textual_too_old

    try:
        textual_importable = importlib.util.find_spec("textual") is not None
    except (ImportError, ValueError):
        textual_importable = False
    return set(missing_names) == {"textual"} and textual_importable and textual_too_old()


def _print_no_textual_diagnosis() -> tuple[bool, list[str]]:
    """Diagnose the missing/too-old wizard deps; returns ``(textual_is_too_old, missing_names)``.

    textual + rich are CORE dependencies (ship with rig) but their absence does not always mean a
    broken install — a local dev/symlink checkout (install.sh) on a PEP-668 externally-managed
    Python legitimately never gets them installed at all. Name whichever is actually missing
    (one or both) instead of always saying "textual".
    """
    from .doctor import externally_managed

    missing_names = _missing_tui_deps() or ["textual"]
    textual_is_too_old = _textual_too_old_not_absent(missing_names)
    if textual_is_too_old:
        missing = "textual is too old (needs upgrading)"
    else:
        missing = f"{' + '.join(missing_names)} {'are' if len(missing_names) > 1 else 'is'} missing"
    if externally_managed():
        print(_warn(f"{missing} — this Python refuses direct installs (PEP 668) — showing a preview only."))
    elif textual_is_too_old:
        # "a broken install" is the wrong diagnosis here — a pacman package or a
        # deliberately-pinned venv isn't broken, it's just below the floor.
        print(_warn(f"{missing} — showing a preview only."))
    else:
        print(_warn(f"{missing} from rig's environment (a broken install) — showing a preview only."))
    return textual_is_too_old, missing_names


def _print_preview_reason(reason: str) -> tuple[bool, list[str]]:
    """Say why the wizard cannot run; the tuple is :func:`_print_no_textual_diagnosis`'s result."""
    if reason == "no-textual":
        return _print_no_textual_diagnosis()
    if reason == "no-tui":
        print(_warn("interactive TUI disabled (--no-tui / RIG_NO_TUI) — showing a preview only."))
    else:  # no-tty
        print(_warn("no TTY — can't launch the interactive setup wizard (piped / non-interactive run)."))
    return False, []


def _setup_preview_no_tui(args: argparse.Namespace, *, reason: str) -> int:
    """`rig init` with no instructions and no way to run the wizard: a non-destructive PREVIEW.

    A bare `rig init` carries no user signal, so it must not "do a bunch of things". When the
    wizard can't run — ``reason="no-tty"`` (piped / CI / agent), ``reason="no-textual"``
    (textual is missing from rig's environment, OR present but too old to run the wizard
    safely — both degrade the same way, but the printed diagnosis line distinguishes them),
    or ``reason="no-tui"`` (the user opted out via ``--no-tui`` / ``RIG_NO_TUI``) — we WRITE
    NOTHING and APPLY NOTHING: we print what `rig apply` WOULD do and exactly how to proceed.
    """
    from .catalog import CatalogError
    from .config import ConfigError
    from .detect import detect_environment
    from .plan import PlanError

    # Computed once here and threaded through to `_print_init_next_steps` below rather than
    # re-derived there (the "absent vs too old" classification kept hitting bugs when scattered).
    textual_is_too_old, missing_names = _print_preview_reason(reason)
    config_exists = (detect_environment(Path(args.cwd).resolve()).repo_root / "rig.yaml").is_file()
    try:
        if config_exists:
            # a committed rig.yaml already exists: preview what `rig apply` WOULD do FROM it (the
            # advice below is "run rig apply"), not a default scaffold — the preview must match
            # the command we point the user at, or it misleads.
            plan, _loaded, _env = _load_plan(args.cwd, None)
        else:
            plan, _loaded, _env, _repo_yaml, _pending = _resolve_init_plan(args, use_default=True)
    except (ConfigError, CatalogError, PlanError) as exc:
        print(_err_block(exc))
        return 2
    _print_plan(plan, full=getattr(args, "plan", False), preview=True, expand_notes=getattr(args, "notes", False))
    _print_init_next_steps(
        config_exists=config_exists,
        reason=reason,
        textual_is_too_old=textual_is_too_old,
        missing_names=missing_names,
    )
    return 0


def _preview_existing_config(args: argparse.Namespace, repo_yaml: Path) -> int:
    """Preview the plan of an ALREADY-committed rig.yaml (what `rig apply` would do), writing
    nothing. Used by the dry-run and no-TUI paths when a config already exists, so the preview
    matches the command the user is pointed at (`rig apply`) instead of a default scaffold."""
    from .catalog import CatalogError
    from .config import ConfigError
    from .plan import PlanError

    try:
        plan, _loaded, _env = _load_plan(args.cwd, None)
    except (ConfigError, CatalogError, PlanError) as exc:
        print(_err_block(exc))
        return 2
    _print_plan(plan, full=getattr(args, "plan", False), preview=True, expand_notes=getattr(args, "notes", False))
    print(_dim("\n(dry-run: nothing written, nothing applied)"))
    print(_dim(f"rig.yaml already exists at {repo_yaml} — run `rig apply commit` to apply it."))
    return 0


def _existing_config_verdict(args: argparse.Namespace) -> int | None:
    """The default-scaffold path must reckon with an ALREADY-committed rig.yaml; ``None`` when there is none."""
    from .detect import detect_environment

    repo_yaml = detect_environment(Path(args.cwd).resolve()).repo_root / "rig.yaml"
    if not repo_yaml.is_file():
        return None
    if args.dry_run:
        # `rig init --dry-run` on a configured repo: preview what `rig apply` WOULD do FROM
        # the committed rig.yaml (consistent with the no-TUI preview), not the default
        # scaffold the refusing `--yes` path would never write.
        return _preview_existing_config(args, repo_yaml)
    # clobber-guard, surfaced BEFORE the catalog scan: a generated default must never
    # overwrite a customized rig.yaml, and surfacing it here keeps the clear "already
    # exists → run rig apply" message even when the agent-tools catalog is unavailable
    # (preserving the pre-refactor error precedence). The explicit --config path instead
    # BACKS UP the old rig.yaml in _persist_rig_yaml — a template install is opt-in.
    print(_err(f"error: {repo_yaml} already exists."))
    print(_dim("  run `rig apply commit` to apply it, or delete it to regenerate a default."))
    return 2


def _apply_init_plan(plan, args: argparse.Namespace) -> int:
    """--apply: show the plan (NOT a preview — it IS being applied), then run it and report what
    was DONE. Printing the plan first mirrors `rig apply` and makes `--plan` meaningful here too."""
    from .actions import run_plan

    _print_plan(plan, full=getattr(args, "plan", False), expand_notes=getattr(args, "notes", False))
    print(_bold(f"\nApplying {len(plan)} action(s)  [on_conflict={plan.on_conflict}]…"))
    report = run_plan(plan)
    _print_results(report)
    verify_ok = _run_and_print_verify(plan, report.results)
    return 1 if (report.errors or not verify_ok) else 0


def _print_scaffold_outcome(args: argparse.Namespace, *, wrote_config: bool) -> None:
    if args.dry_run:
        print(_dim("\n(dry-run: nothing written, nothing applied)"))
    elif wrote_config:
        print(_bold("\nDone — rig.yaml scaffolded (config only; NOTHING applied yet)."))
        print(_dim("Next: run `rig apply commit` to apply the plan above (or re-run init with --apply)."))
    else:
        # --config pointed at the repo's existing rig.yaml: nothing was written, nothing applied.
        print(_bold("\nNothing written (rig.yaml already in place) and NOTHING applied."))
        print(_dim("Next: run `rig apply commit` to apply the plan above (or re-run init with --apply)."))


def _setup_headless(args: argparse.Namespace, *, use_default: bool) -> int:
    from .catalog import CatalogError
    from .config import ConfigError
    from .plan import PlanError

    if use_default:
        verdict = _existing_config_verdict(args)
        if verdict is not None:
            return verdict

    try:
        plan, _loaded, _env, repo_yaml, pending_write = _resolve_init_plan(
            args, use_default=use_default
        )
    except (ConfigError, CatalogError, PlanError) as exc:
        print(_err_block(exc))
        return 2

    # plan is valid → persist rig.yaml (the committed source of truth, never optional) unless dry-run.
    # pending_write is None only when --config already points AT the repo's own rig.yaml (nothing to
    # write) — track whether a write actually happened so the message below never claims a phantom one.
    wrote_config = bool(pending_write) and not args.dry_run
    if wrote_config:
        _persist_rig_yaml(pending_write, repo_yaml)

    if getattr(args, "apply", False) and not args.dry_run:
        return _apply_init_plan(plan, args)

    # default: the plan is a PREVIEW of `rig apply` — init only scaffolds the config.
    _print_plan(plan, full=getattr(args, "plan", False), preview=True, expand_notes=getattr(args, "notes", False))
    _print_scaffold_outcome(args, wrote_config=wrote_config)
    return 0
