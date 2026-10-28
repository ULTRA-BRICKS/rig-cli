"""Plan stages for machine-level settings: global git excludes, models schedule, tmux, env, spotlight, bun,
Forgejo and global git config.

Split out of :mod:`riglib.plan`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import sys
from .catalog import Catalog
from .config import GITIGNORE_DEFAULT_ENTRIES
from .config import GITIGNORE_DEFAULT_EXCLUDESFILE
from .config import LoadedConfig
from .plan_model import Action
from .plan_model import InstallPlan
from .plan_targets import _expand


def _build_global_excludes(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan the rig-managed block in the GLOBAL git excludes file (``core.excludesfile``).

    This is GLOBAL (machine-wide) config, wired like the git-hooks ``dispatcher``: rig owns ONE
    marker-delimited block in git's global ``core.excludesfile`` so harness artifacts — chiefly
    Claude Code's throwaway ``**/.claude/worktrees/`` — are ignored in EVERY repo on the machine,
    with zero per-repo commits and no per-repo ``rig apply``. Opt out with
    ``gitignore: { enabled: false }``. Default **ON** (like the dispatcher), so on a clean machine
    ``rig init``/``rig apply`` provisions it without any per-repo config.

    Target resolution is deferred to apply time (it depends on whether ``core.excludesfile`` is
    already set on this machine — a thing the plan cannot read purely), so the plan emits ONE
    idempotent action carrying the resolved ``entries`` and the XDG fallback path; the runner
    reads ``core.excludesfile`` and EITHER reconciles the block in the user's existing excludes
    file OR sets ``core.excludesfile`` to the XDG default and writes the block there. The
    placeholder ``target`` is the XDG default for display; the runner re-resolves it. No carrier
    in agent-tools.

    The ignored ``entries`` are configurable with a sensible default (``GITIGNORE_DEFAULT_ENTRIES``);
    an empty/absent list uses that default. The ``excludesfile`` override (rare) forces a specific
    file rather than honoring ``core.excludesfile``.
    """
    gi = config.data.get("gitignore")
    if gi is None:
        gi = {}
    if not isinstance(gi, dict):
        return  # validate() already fail-closed on a non-mapping block
    if gi.get("enabled") is False:
        return
    raw_entries = gi.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        entries = list(GITIGNORE_DEFAULT_ENTRIES)
    else:
        entries = [str(e) for e in raw_entries]
    override = gi.get("excludesfile")
    options: dict[str, Any] = {
        "entries": entries,
        "xdg_default": GITIGNORE_DEFAULT_EXCLUDESFILE,
    }
    if isinstance(override, str) and override:
        options["excludesfile"] = override
    plan.actions.append(
        Action(
            kind="provision_global_excludes",
            category="gitignore",
            item="block",
            source=config.repo_root,
            # Placeholder for display only — the runner re-resolves the real target from
            # core.excludesfile (or the XDG default) at apply time. Expanded for portability.
            target=_expand(GITIGNORE_DEFAULT_EXCLUDESFILE, config.repo_root),
            options=options,
        )
    )


def _build_models(config: LoadedConfig, catalog: Catalog, plan: InstallPlan) -> None:
    """Plan the daily model-freshness checker schedule, if a ``models`` block enables it.

    The CTO's #3685 direction: on ``rig init`` AND ``rig apply``, check whether the daily
    checker cron is installed and install it if missing. The action carries the
    platform-resolved desired schedule (launchd plist on macOS / systemd --user timer on Linux); the
    install-if-missing + idempotency live in ``actions/runner.py``, so the plan stays pure.

    No ``models`` block (or ``enabled: false``) → no action (rig leaves the system cron
    alone). The checker command is anchored on the resolved ``agent_tools_source`` (rig runs
    agent-tools content read-only from the checkout) unless ``checker_path`` pins one.
    """
    from .config import _DEFAULT_SCHEDULE_TIME, parse_hhmm
    from .schedule import DEFAULT_LABEL, build_schedule, default_checker_path

    m = config.data.get("models")
    if not isinstance(m, dict) or not m:
        return
    if m.get("enabled") is False:
        return

    schedule_cfg = m.get("schedule", {}) if isinstance(m.get("schedule"), dict) else {}
    hour, minute = parse_hhmm(schedule_cfg.get("time", _DEFAULT_SCHEDULE_TIME))
    label = str(schedule_cfg.get("label") or DEFAULT_LABEL)

    # checker path: an explicit ``checker_path`` wins; else the model_freshness.py inside the
    # resolved agent-tools checkout. If neither resolves, plan a note (not a crash) — the
    # schedule can't run a checker that doesn't exist.
    checker_raw = m.get("checker_path")
    if checker_raw:
        checker_path = _expand(str(checker_raw), config.repo_root)
    else:
        checker_path = default_checker_path(catalog.source)
    if checker_path is None:
        plan.notes.append(
            "models: schedule skipped — no checker_path and agent_tools_source did not resolve "
            "a checker (set models.checker_path or agent_tools_source)"
        )
        return
    # Don't provision a schedule that runs a checker that isn't on disk — that would install a
    # cron firing a missing script daily. Skip with a note so the operator fixes the path.
    if not checker_path.is_file():
        plan.notes.append(
            f"models: schedule skipped — checker not found at {checker_path} "
            "(set models.checker_path, or point agent_tools_source at a checkout that ships "
            "lib/checker/model_freshness.py)"
        )
        return

    sched = build_schedule(checker_path=checker_path, hour=hour, minute=minute, label=label)
    # the action target is the artifact path the install/drift code keys off: the plist on
    # macOS, or the timer unit on Linux.
    target = sched.plist_path if sched.platform == "launchd" else sched.unit_dir / sched.timer_name
    plan.actions.append(
        Action(
            kind="provision_schedule",
            category="models",
            item="model-freshness",
            source=config.repo_root,  # no carrier; anchor on the repo
            target=target,  # type: ignore[arg-type]
            options={
                "platform": sched.platform,
                "label": label,
                "hour": hour,
                "minute": minute,
                "checker_path": str(checker_path),
            },
        )
    )


def _build_tmux(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan the rig-managed tmux configuration provisioning, if a ``tmux`` block enables it.

    NOT default-on: tmux config is opt-in (a ``tmux:`` block with ``enabled`` not false). The
    whole generate-and-migrate logic depends on what is already on disk (an existing hand-
    written ``~/.tmux.conf``), so the plan emits ONE idempotent ``provision_tmux`` action
    carrying the validated config sub-blocks; the runner resolves the :class:`TmuxPlan` and
    writes the artifacts. No carrier in agent-tools — rig generates the config itself.

    The conf path is resolved at apply time (HOME-relative ``~/.tmux.conf`` stays portable in a
    committed rig.yaml), so the action target is the conf path expanded against the repo root.
    """
    t = config.data.get("tmux")
    # A PRESENT `tmux:` block opts in (the docs: a block with `enabled` not false provisions) —
    # including an explicit empty mapping `tmux: {}` (which yields all the safe defaults). Only an
    # ABSENT key (None) or `enabled: false` is a no-op. `t == {}` must NOT be treated as absent.
    if t is None or not isinstance(t, dict):
        return
    if t.get("enabled") is False:
        return

    # Resolve conf_path / generated_dir HERE against the repo root, so a relative path is
    # anchored to the `-C` repo (never the process CWD) and ~/ stays home-anchored. The runner
    # gets ABSOLUTE paths and never re-resolves against CWD. (Mirrors how every other action's
    # target is plan-resolved.)
    conf_path = _expand(str(t.get("conf_path", "~/.tmux.conf")), config.repo_root)
    generated_dir = _expand(str(t.get("generated_dir", "~/.config/rig/tmux")), config.repo_root)

    plan.actions.append(
        Action(
            kind="provision_tmux",
            category="tmux",
            item="config",
            source=config.repo_root,  # no carrier; rig generates the config
            target=conf_path,
            options=_tmux_options(t, conf_path, generated_dir),
        )
    )


def _tmux_options(t: dict[str, Any], conf_path: Path, generated_dir: Path) -> dict[str, Any]:
    from .tmux import DEFAULT_APPLY_MODE

    return {
        "apply_mode": str(t.get("apply", DEFAULT_APPLY_MODE)),
        "conf_path": str(conf_path),
        "generated_dir": str(generated_dir),
        "resurrect": dict(t.get("resurrect", {}) or {}),
        "continuum": dict(t.get("continuum", {}) or {}),
        "moshi": dict(t.get("moshi", {}) or {}),
        "cc_restore": dict(t.get("cc_restore", {}) or {}),
        "omp_restore": dict(t.get("omp_restore", {}) or {}),
        "codex_restore": dict(t.get("codex_restore", {}) or {}),
        "anti_sprawl": dict(t.get("anti_sprawl", {}) or {}),
        "boot": dict(t.get("boot", {}) or {}),
        "login_shell": _tmux_login_shell(t),
        "autosave": _tmux_autosave(t),
        "recovery": dict(t.get("recovery", {}) or {}),
        "pane_titles": dict(t.get("pane_titles", {}) or {}),
        "focus_events": dict(t.get("focus_events", {}) or {}),
        "clipboard": dict(t.get("clipboard", {}) or {}),
        "copy_mode": dict(t.get("copy_mode", {}) or {}),
    }


def _tmux_login_shell(t: dict[str, Any]) -> dict[str, Any]:
    # RESOLVE the login shell to a CONCRETE path ONCE here (plan time), not per render. An empty
    # `login_shell.shell` means "use the user's $SHELL"; resolving it at render would make
    # rig.tmux.conf depend on $SHELL/FS at the moment of EACH render — so `rig apply` (one $SHELL)
    # and `rig status` (launchd/cron/CI, different/empty $SHELL) would render DIFFERENT
    # default-command lines → permanent flapping drift apply "fixes" every run (review Medium). By
    # baking the path into the action options, render/drift are deterministic and idempotent.
    login_shell = dict(t.get("login_shell", {}) or {})
    if login_shell.get("enabled", True) is not False and not login_shell.get("shell"):
        from .tmux import resolve_login_shell

        login_shell["shell"] = resolve_login_shell()
    return login_shell


def _tmux_autosave(t: dict[str, Any]) -> dict[str, Any]:
    # The independent autosave agent is macOS-only (a launchd LaunchAgent). On a non-darwin host
    # we must NOT let the generated rig.tmux.conf disable continuum's own periodic save
    # (@continuum-save-interval 0) — no independent runner replaces it there, so the user would be
    # left with NO autosave at all. Force autosave off off-darwin; continuum keeps saving (codex
    # P1). apply and status run on the SAME machine, so the gated value is consistent (no drift).
    autosave = dict(t.get("autosave", {}) or {})
    if sys.platform != "darwin":
        autosave["enabled"] = False
    return autosave


def env_options_from_config(e: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    """Resolve an ``env:`` config block into the exact ``options`` dict a ``provision_env``
    ``Action`` carries (``rc_path``/``generated_dir`` expanded + baked absolute, ``vars``
    stringified). The ONE place this resolution happens — ``_build_env`` below AND the
    disabled-env status scan (``cli.py``, which needs the same resolved paths to know WHERE to
    look for a leftover ``rig.env.sh`` when the config has since turned the block off) both call
    this instead of each re-deriving the same defaults/``_expand`` calls independently, which
    would risk silently diverging if this resolution ever changes (review finding).
    """
    from .shell_env import DEFAULT_GENERATED_DIR, DEFAULT_RC_PATH

    rc_path = _expand(str(e.get("rc_path", DEFAULT_RC_PATH)), repo_root)
    generated_dir = _expand(str(e.get("generated_dir", DEFAULT_GENERATED_DIR)), repo_root)
    vars_block = dict(e.get("vars", {}) or {})
    return {
        "rc_path": str(rc_path),
        "generated_dir": str(generated_dir),
        "vars": {str(k): str(v) for k, v in vars_block.items()},
    }


def _build_env(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan the rig-managed shell-environment-variables provisioning, if an ``env`` block enables it.

    Mirrors ``_build_tmux``'s opt-in shape exactly: a PRESENT ``env:`` block opts in (including an
    explicit empty mapping ``env: {}``, which yields a harmless header-only generated file); only an
    ABSENT key or ``enabled: false`` is a no-op. This is a per-MACHINE concern (the shell startup
    file rig sources into is HOME-anchored, not repo-relative), so the block lives in the GLOBAL
    layer (``~/.config/rig/config.yaml``) — but it cascades into the merged config the same way.

    ``rc_path``/``generated_dir`` are resolved by :func:`env_options_from_config` against the
    repo root (mirrors every other action's target resolution — never re-resolved against CWD by
    the runner) so a relative path stays anchored to the ``-C`` repo and ``~/`` stays home-anchored.
    """
    from . import fj

    e = config.data.get("env")
    # The GLOBAL block only: a repo rig.yaml's fj.host must never become the machine-wide default.
    implied = fj.implied_env_vars(_global_fj_block(config))
    if (e is None or not isinstance(e, dict)) and not implied:
        return
    e = e if isinstance(e, dict) else {}
    if e.get("enabled") is False:
        return

    options = env_options_from_config(e, config.repo_root)
    for key, value in implied.items():  # a hand-set env.vars value wins
        options["vars"].setdefault(key, value)
    plan.actions.append(
        Action(
            kind="provision_env",
            category="env",
            item="vars",
            source=config.repo_root,  # no carrier; rig generates the file itself
            target=Path(options["rc_path"]),
            options=options,
        )
    )


def _build_spotlight(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan the macOS Spotlight-exclude provisioning, if a ``spotlight`` block enables it.

    Default **OFF** (opt-in, macOS-specific — like ``tmux``/``tools``): an ABSENT, empty, or
    ``enabled: false`` block emits NO action, so ``rig apply`` on Linux / a non-opted machine
    never sweeps the filesystem. Alex enables it in the GLOBAL config. It is a per-MACHINE concern
    (the dev roots on this box), so the block lives in the GLOBAL layer.

    Emits ONE idempotent ``provision_spotlight`` action carrying the resolved roots + denylist +
    launchd knobs. The runner drops ``.metadata_never_index`` into every matched dir and writes +
    loads the periodic re-sweep LaunchAgent. The sweep argv is resolved HERE (the current
    interpreter + ``-m riglib``) so the committed plan is explicit about what launchd runs.
    """
    from . import spotlight

    s = config.data.get("spotlight")
    if s is None or not isinstance(s, dict):
        return
    # Default OFF (opt-in): unlike tmux (present-block = opt-in), spotlight requires an EXPLICIT
    # `enabled: true`. An absent OR empty `spotlight: {}` block provisions nothing — matching the
    # schema default (`enabled: false`) and the docs, so a stray empty block never sweeps the FS.
    if not s.get("enabled"):
        return

    roots = spotlight.resolve_roots(s.get("roots"))
    deny = spotlight.resolve_deny(s.get("deny"), s.get("extra"))
    label = str(s.get("label") or spotlight.DEFAULT_BOOT_LABEL)
    max_depth = int(s.get("max_depth", spotlight.DEFAULT_MAX_DEPTH))
    plan.actions.append(
        Action(
            kind="provision_spotlight",
            category="spotlight",
            item="exclude",
            source=config.repo_root,  # no carrier in agent-tools; rig owns the sweep
            target=Path(label),  # the launchd label — runner resolves the real plist under HOME
            options={
                "roots": [str(r) for r in roots],
                "deny": sorted(deny),
                "label": label,
                "max_depth": max_depth,
                "sweep_cmd": list(spotlight.default_sweep_cmd()),
            },
        )
    )


def _global_fj_block(config: LoadedConfig) -> dict | None:
    """The ``fj`` block of the GLOBAL layer alone — machine-wide fj settings, never a repo
    rig.yaml's override (a repo layer may only name its own ``fj.host``)."""
    block = config.global_data.get("fj")
    return block if isinstance(block, dict) else None


def _global_fj_host(config: LoadedConfig) -> str | None:
    """``fj.host`` from the GLOBAL config file alone — the machine default FJ_FALLBACK_HOST points
    at, even when the repo rig.yaml overrides ``fj.host`` for its own logins. ``None`` when the
    global config names no host: then rig sets no FJ_FALLBACK_HOST."""
    block = _global_fj_block(config)
    if block is None or block.get("enabled") is False:
        return None
    host = block.get("host")
    return str(host) if host else None


def _build_fj(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan Forgejo access, if a ``fj`` block enables it (block PRESENCE opts in).

    One ``provision_fj`` action: install fj when missing, list every login host in fj's
    ``client_ids`` (Forgejo's built-in public OAuth app), set ``FJ_FALLBACK_HOST`` on Windows (on
    macOS/Linux ``_build_env`` exports it) and point git's credential helper for the global host at
    fj's login (rig's ``git-credential-fj``). See
    :mod:`riglib.fj`.
    """
    from . import fj

    block = config.data.get("fj")
    if not isinstance(block, dict) or not block or block.get("enabled") is False:
        return
    options = fj.fj_options_from_config(block, fallback_host=_global_fj_host(config))
    plan.actions.append(
        Action(
            kind="provision_fj",
            category="fj",
            item=options["hosts"][0],
            source=config.repo_root,  # no carrier; rig derives everything from the block
            target=fj.client_ids_path(),
            options=options,
        )
    )


def _build_bun(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan the global bunfig isolated-linker + globalStore splice.

    Default **OFF** (opt-in): an absent, empty, or ``enabled: false`` block emits NO action.
    GLOBAL-only. Writes both ``linker = "isolated"`` and ``globalStore = true`` because
    bun's global store is a no-op under the hoisted linker.
    """
    from . import bunfig

    block = config.data.get("bun")
    if block is None or not isinstance(block, dict):
        return
    if not block.get("enabled"):
        return
    target = bunfig.bunfig_path()
    plan.actions.append(
        Action(
            kind="provision_bunfig",
            category="bun",
            item="bunfig",
            source=config.repo_root,
            target=target,
        )
    )


def _build_git(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan the global git settings (``git.config`` → ``git config --global``).

    A PRESENT block opts in (like ``env``); an absent block, ``enabled: false``, or a block that
    declares no keys emits NO action. ONE action carries every key (drift still reports per key).
    GLOBAL-only — the loader already refused a ``git`` block from any other layer.
    """
    from . import gitconfig

    entries = gitconfig.desired_config(config.data.get("git"))
    if not entries:
        return
    plan.actions.append(
        Action(
            kind="provision_git_config",
            category="git",
            item="config",
            source=config.repo_root,
            target=gitconfig.global_gitconfig_path(),
            options={"config": entries},
        )
    )
