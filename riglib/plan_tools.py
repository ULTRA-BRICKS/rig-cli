"""Plan stages for the personal CLI ecosystem: project tools, ecosystem README, tool installs and the
rig-managed daemons (tg-ctl, tcc-watch, reaper, sysmon), harness wrappers, internal-dev hook.

Split out of :mod:`riglib.plan`.
"""

from __future__ import annotations

from pathlib import Path
from . import ecosystem_readme
from . import project_tools
from .config import LoadedConfig
from .config import linter_path_escapes_repo
from .paths import expand_user_path as _expand_user_path
from .plan_model import Action
from .plan_model import InstallPlan
from .plan_model import PlanError


def _build_project_tools(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan repo-local integrations for Haft, Serena, and Sverklo.

    The pure renderer in :mod:`riglib.project_tools` owns the desired carriers. The plan emits one
    action per file/operation so dry-run/status can name the exact drifting integration.
    """
    for entry in project_tools.desired_entries(config.repo_root, config.data.get("project_tools")):
        plan.actions.append(
            Action(
                kind="provision_project_tool",
                category="project_tools",
                item=entry.item,
                source=config.repo_root,
                target=config.repo_root,
                options=entry.to_options(),
            )
        )


def _build_ecosystem_readme(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan the rig-managed README ``## Ecosystem`` block (rig-cli#308), if opted in.

    Default **OFF** (opt-in, like ``tools:``/``project_tools`` in spirit): an ABSENT, empty, or
    ``enabled: false`` ``ecosystem_readme:`` block emits NO action, so a clean ``rig apply`` never
    rewrites a repo's README unprompted. A repo opts in via ``rig setup`` (the schema.py registry)
    or ``rig config set ecosystem_readme.enabled true``. Emits ONE ``apply_ecosystem_readme``
    action; the runner splices the SAME registry-rendered block (:mod:`riglib.ecosystem_readme`)
    into every target repo, so a re-apply with an unchanged registry is a no-op everywhere.
    """
    er = config.data.get("ecosystem_readme")
    if not isinstance(er, dict) or not er.get("enabled"):
        # rig never auto-deletes: a block left over from a PRIOR enabled apply still sits in the
        # target file, and its own in-block notice still claims it is "kept in sync by rig apply"
        # — false the moment the feature is off. Note it ONLY when there is evidence a block was
        # actually written (no noise on a repo that never opted in), mirroring the leftover-note
        # pattern _build_guard_policy uses for a disabled permissions posture.
        er_dict = er if isinstance(er, dict) else {}
        target_rel = str(er_dict.get("target") or "README.md")
        target = config.repo_root / target_rel
        try:
            orphaned = target.is_file() and ecosystem_readme.has_managed_block(
                target.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError):
            orphaned = False
        if orphaned:
            plan.notes.append(
                f"ecosystem_readme: disabled — a rig-managed Ecosystem block stays in {target_rel} "
                "(rig never auto-deletes) but will no longer be kept in sync; re-enable it or "
                "remove the block by hand"
            )
        return

    target_rel = str(er.get("target") or "README.md")
    if linter_path_escapes_repo(target_rel):
        raise PlanError(f"ecosystem_readme.target must stay inside the repo: {target_rel!r}")

    plan.actions.append(
        Action(
            kind="apply_ecosystem_readme",
            category="ecosystem_readme",
            item="readme",
            source=config.repo_root,  # no carrier in agent-tools; the registry is in-code
            target=config.repo_root / target_rel,
            options={"target": target_rel},
        )
    )


def _build_tools(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan the personal CLI ecosystem install (tg/review/task/draw/…), if ``tools:`` opts in.

    Default **OFF** (opt-in, unlike ``tg_ctl``/``models``): an ABSENT, empty, or ``enabled: false``
    ``tools:`` block emits NO action, so a clean ``rig init`` never clones four repos a user may not
    have and the e2e suite never shells out to a real ``install.sh``. A machine opts in by listing
    tools under ``tools.items``. This is a per-MACHINE concern (the tool ecosystem on this dev box),
    so the block lives in the GLOBAL layer (``~/.config/rig/config.yaml``).

    Emits ONE idempotent ``provision_tools`` action carrying every resolved :class:`ToolSpec` (as a
    small JSON-ish dict each). The runner runs each tool's OWN ``install.sh`` only when the tool is
    not already current; drift diffs declared-vs-on-disk. There is no agent-tools carrier — each
    tool installs FROM its own checkout, so ``source`` is just the repo root for describe().
    """
    from . import tools as toolsmod

    specs = toolsmod.resolve_tool_specs(config.data.get("tools"))
    if not specs:
        return

    plan.actions.append(
        Action(
            kind="provision_tools",
            category="tools",
            item="ecosystem",
            source=config.repo_root,  # no carrier; each tool installs from its own checkout
            target=specs[0].bin_dir,  # the managed PATH dir (display/anchor only)
            options={"specs": [toolsmod.spec_to_option(s) for s in specs]},
        )
    )


def _build_tg_ctl(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan the rig-managed tg-ctl inbound-daemon LaunchAgent, unless ``enabled: false``.

    Default **ON** (like ``agents_md``/``github``): an ABSENT or empty ``tg_ctl:`` block still
    provisions the daemon, so ``rig init`` on a clean machine sets it up with no config at all.
    Only ``enabled: false`` opts out. This is a per-MACHINE concern (one inbound Telegram
    control daemon per machine), so the block belongs in the GLOBAL layer
    (``~/.config/rig/config.yaml``) — but it cascades into the merged config the same way.

    Unlike tmux, the tg-ctl artifact paths are HOME-anchored per-machine (not repo-relative):
    the runner resolves them against ``Path.home()`` at apply time (so a committed rig.yaml stays
    portable and never anchors ~/.files/bin to a repo root). The action just carries the raw
    config knobs; the bun path is discovered at apply time.
    """
    from .tg_ctl import DEFAULT_BOOT_LABEL

    # An ABSENT key (None) defaults to provisioning (default-on). A PRESENT block (validate() has
    # already guaranteed it is a mapping) opts in unless `enabled: false`.
    t = config.data.get("tg_ctl") or {}
    if t.get("enabled") is False:
        return

    plan.actions.append(
        Action(
            kind="provision_tg_ctl",
            category="tg_ctl",
            item="boot",
            source=config.repo_root,  # no carrier; rig generates the plist
            # target is the launchd LABEL (not a filesystem path) — the runner resolves the real
            # ~/Library/LaunchAgents/<label>.plist against HOME at apply time. `label or DEFAULT`
            # (not `get(key, default)`) so a YAML `label:` with no value (None) can't become the
            # literal string "None".
            target=Path(t.get("label") or DEFAULT_BOOT_LABEL),
            options={
                "boot": t.get("boot", True),
                "label": t.get("label"),
                "bun_path": t.get("bun_path"),
                "tg_ctl_path": t.get("tg_ctl_path"),
                "config_dir": t.get("config_dir"),
            },
        )
    )


def _build_tcc_watch(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan the rig-managed tcc-watch dialog-watcher LaunchAgent, unless ``enabled: false``.

    Phase 1 (detection-only) of the defense-in-depth design behind the eval-tool TCC deny-list
    (PR #488) — see :mod:`riglib.tcc_watch`'s module docstring for the full phased design and
    :mod:`riglib.tcc_watch_daemon` for what the daemon actually does.

    Default **ON** (like ``tg_ctl``): an ABSENT or empty ``tcc_watch:`` block still provisions
    the daemon, so `rig init` on a clean macOS machine sets it up with no config at all. Only
    `enabled: false` opts out. Per-MACHINE concern (one watcher daemon per machine), so the block
    belongs in the GLOBAL layer (``~/.config/rig/config.yaml``) — but it cascades into the
    merged config the same way every other GLOBAL-only block does.
    """
    from .tcc_watch import DEFAULT_LABEL

    # An ABSENT key (None) defaults to provisioning (default-on). A PRESENT block (validate() has
    # already guaranteed it is a mapping) opts in unless `enabled: false`.
    t = config.data.get("tcc_watch") or {}
    if t.get("enabled") is False:
        return

    plan.actions.append(
        Action(
            kind="provision_tcc_watch",
            category="tcc_watch",
            item="watch",
            source=config.repo_root,  # no carrier; rig generates the plist + copies the watcher script
            # target is the launchd LABEL (not a filesystem path), like provision_tg_ctl.
            target=Path(t.get("label") or DEFAULT_LABEL),
            options={
                "label": t.get("label"),
                "config_dir": t.get("config_dir"),
                "python_bin": t.get("python_bin"),
            },
        )
    )


def _build_tgctl_reaper(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan the rig-managed tgctl-reaper periodic orphan-sweep LaunchAgent, unless
    ``enabled: false``.

    Durable fix for the 2026-09-09 OOM incident (agent-tools#697) — see
    :mod:`riglib.tgctl_reaper`'s module docstring for the full rationale and
    :mod:`riglib.tgctl_reaper_daemon` for what the sweep actually does.

    Default **ON** (like ``tg_ctl``/``tcc_watch``): an ABSENT or empty ``tgctl_reaper:`` block
    still provisions the sweep, so `rig init` on a clean macOS machine sets it up with no config
    at all. Only `enabled: false` opts out. Per-MACHINE concern (one reaper per machine), so the
    block belongs in the GLOBAL layer (``~/.config/rig/config.yaml``) — but it cascades into the
    merged config the same way every other GLOBAL-only block does.
    """
    from .tgctl_reaper import DEFAULT_INTERVAL_SECONDS, DEFAULT_LABEL

    # An ABSENT key (None) defaults to provisioning (default-on). A PRESENT block (validate() has
    # already guaranteed it is a mapping) opts in unless `enabled: false`.
    t = config.data.get("tgctl_reaper") or {}
    if t.get("enabled") is False:
        return

    plan.actions.append(
        Action(
            kind="provision_tgctl_reaper",
            category="tgctl_reaper",
            item="sweep",
            source=config.repo_root,  # no carrier; rig generates the plist + copies the sweeper script
            # target is the launchd LABEL (not a filesystem path), like provision_tcc_watch.
            target=Path(t.get("label") or DEFAULT_LABEL),
            options={
                "interval_seconds": t.get("interval_seconds") or DEFAULT_INTERVAL_SECONDS,
                "label": t.get("label"),
                "config_dir": t.get("config_dir"),
                "python_bin": t.get("python_bin"),
            },
        )
    )


def _build_sysmon(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan the rig-managed sysmon resource-monitor daemon, unless ``enabled: false``.

    Default **ON** (like ``tg_ctl``/``tcc_watch``/``tgctl_reaper``): an ABSENT or empty
    ``sysmon:`` block still provisions the monitor — it is the layer that catches the NEXT
    resource-exhaustion cause before the kernel panics (rig-cli#622; see :mod:`riglib.sysmon`).
    GLOBAL-only (one monitor per machine). The whole validated block rides in the action
    options; :func:`riglib.actions.sysmon_action.sysmon_plan_from_action` resolves it into the pure
    :class:`riglib.sysmon.SysmonPlan` that apply, drift, verify and status share. The global
    ``env.vars`` ride along so the GPU-slot cap follows ``RIG_GPU_SLOTS`` when rig exports it.
    """
    from .sysmon import DEFAULT_LABEL

    block = config.data.get("sysmon") or {}
    if block.get("enabled") is False:
        return
    env_block = config.data.get("env") if isinstance(config.data.get("env"), dict) else {}
    env_vars = env_block.get("vars") if isinstance(env_block.get("vars"), dict) else {}
    plan.actions.append(
        Action(
            kind="provision_sysmon",
            category="sysmon",
            item="monitor",
            source=config.repo_root,  # no carrier; rig renders the service + copies the package
            # target is the service LABEL (not a filesystem path), like provision_tcc_watch.
            target=Path(block.get("label") or DEFAULT_LABEL),
            options={
                "block": {k: v for k, v in block.items() if k != "enabled"},
                "gpu_env": {k: str(v) for k, v in env_vars.items() if k in ("RIG_GPU_SLOTS", "RIG_GPU_SLOT_DIR")},
            },
        )
    )


def _build_harness_wrappers(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan the opt-in per-harness PATH wrappers: ONE ``harness_wrapper`` action per kind.

    Default **OFF**: only ``harness_wrappers.enabled: true`` emits anything. GLOBAL-only — the loader
    already refused the block from any other layer. Pure: paths are expanded, never stat'ed (the
    runner and drift read the disk). Kinds come out in canonical order whatever the config order, so
    the plan is deterministic. rig never edits ``PATH``: the user puts ``dir`` before the real binaries.
    """
    from . import wrapper_provision

    settings = wrapper_provision.settings_from_config(config.data.get("harness_wrappers"))
    if not settings.enabled:
        return
    bindir = _expand_user_path(settings.dir)
    for kind in settings.kinds:
        real_bin = settings.real_bin.get(kind)
        plan.actions.append(
            Action(
                kind="harness_wrapper",
                category="harness_wrappers",
                item=kind,
                source=config.repo_root,  # no carrier; the wrapper text is generated
                target=bindir / kind,
                options={"kind": kind, "real_bin": str(_expand_user_path(real_bin)) if real_bin else None},
            )
        )


def _build_internal_dev(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan the per-repo daemon auto-reload post-commit hook, when ``auto_reload_on_commit`` is on.

    Default **OFF** (opt-in): an ABSENT/empty/``false`` block emits NOTHING. When enabled, one
    ``install_dev_reload_hook`` action carries the resolved daemon-source patterns + reload command;
    the runner writes the repo-local ``<git-dir>/hooks/post-commit`` (and, under a global
    ``core.hooksPath`` composer, a generic post-commit trampoline). REPO-owned: the enablement +
    paths live in the committed ``rig.yaml``, exactly like ``agent_hooks.worktree_only``.
    """
    from .dev_reload import DEFAULT_RELOAD_COMMAND

    d = config.data.get("internal_dev") or {}
    if not d.get("auto_reload_on_commit"):
        return

    paths = list(d.get("daemon_source_paths") or [])
    reload_command = d.get("reload_command") or DEFAULT_RELOAD_COMMAND
    plan.actions.append(
        Action(
            kind="install_dev_reload_hook",
            category="internal_dev",
            item="post-commit",
            source=config.repo_root,  # the repo whose commits trigger the reload
            # display target — the runner re-resolves the real git dir (worktree-correct) at apply.
            target=config.repo_root / ".git" / "hooks" / "post-commit",
            options={
                "daemon_source_paths": paths,
                "reload_command": reload_command,
            },
        )
    )
