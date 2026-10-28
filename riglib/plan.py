"""Plan builder — resolves (config + catalog) into an ordered list of Actions.

This is the headless engine ``rig apply`` and the wizard's Apply screen both call. It
turns the declarative ``rig.yaml`` decisions into concrete, idempotent install actions:

- resolves which catalog items are enabled (category ``enabled``, ``all``/``enable``/
  ``disable`` deltas, per-item ``items.<name>.enabled``, and the detected project type
  pulling in by-type skill bundles),
- resolves each item's install target (item → category → ``defaults`` → built-in),
- produces a stable, ordered ``InstallPlan`` of :class:`Action` objects.

An :class:`Action` is a small dataclass describing the change; the *execution* lives in
``actions/`` (stdlib-only). The plan never touches disk — ``--dry-run`` prints it.

:func:`build` only ORDERS the stages; each stage lives in a focused ``plan_*`` module
(:mod:`~riglib.plan_catalog`, :mod:`~riglib.plan_harness`, :mod:`~riglib.plan_github`, …). Names the
rest of the code base (and the tests) import from ``riglib.plan`` are re-exported below.
"""

from __future__ import annotations

from .catalog import Catalog
from .config import LoadedConfig
from .harness_skills import HARNESS_SKILL_DIRS as _HARNESS_SKILL_DIRS
from .plan_agents import (
    SHIP_TASK_CODE_PREFIX_RE,
    _build_agents_symlink,
    _build_codex_skills_budget,
    _build_omp_idle_ttl,
    _build_ship_delegator,
    _build_ship_task_prefix,
)
from .plan_catalog import plan_ci, plan_dispatcher, plan_mcp, plan_skills
from .plan_github import (
    _build_github_actions,
    _build_github_browser,
    _build_github_ghas,
    _build_github_merge,
    _build_github_ruleset,
)
from .plan_guard import _build_guard_policy
from .plan_global_instructions import _build_global_instructions
from .plan_harness import _build_harness, _build_harness_delegated_notes, _build_omp_web_search
from .plan_hook_bridge import _build_hook_bridge
from .plan_items import _catalog_by_leaf, _plan_agent_hooks, _plan_hook_blurbs, _validate_item_names
from .plan_lint import _build_lint_checks, _build_linters
from .plan_lint_bootstrap import LintBootstrapRequest, plan_lint_bootstrap
from .plan_mode import _build_mode
from .plan_model import Action, InstallPlan, PlanError
from .plan_permissions import (
    _build_execpolicy,
    _build_permissions,
    _explicit_git_hosting_opt_out,
    _explicit_tools_git_hosting,
    _resolved_permission_tools,
)
from .plan_system import (
    _build_bun,
    _build_env,
    _build_fj,
    _build_git,
    _build_global_excludes,
    _build_models,
    _build_spotlight,
    _build_tmux,
    env_options_from_config,
)
from .plan_targets import (
    HOOK_BRIDGE_NO_SUPPORT_PHRASE,
    _expand,
    _resolve_harness_skill_dir,
    resolve_category_target,
    resolve_category_targets,
)
from .plan_tools import (
    _build_ecosystem_readme,
    _build_harness_wrappers,
    _build_internal_dev,
    _build_project_tools,
    _build_sysmon,
    _build_tcc_watch,
    _build_tg_ctl,
    _build_tgctl_reaper,
    _build_tools,
)
from .provenance import KNOWN_CATEGORIES, known_names_from_config

# Re-exports: the stable import surface of the old monolithic ``riglib.plan`` (cli, drift, the
# action runner and the tests import these names from here, not from the ``plan_*`` modules).
__all__ = [
    "Action",
    "HOOK_BRIDGE_NO_SUPPORT_PHRASE",
    "InstallPlan",
    "LintBootstrapRequest",
    "PlanError",
    "SHIP_TASK_CODE_PREFIX_RE",
    "_HARNESS_SKILL_DIRS",
    "_build_bun",
    "_build_codex_skills_budget",
    "_build_ecosystem_readme",
    "_build_execpolicy",
    "_build_git",
    "_build_github_ruleset",
    "_build_harness",
    "_build_harness_wrappers",
    "_build_hook_bridge",
    "_build_internal_dev",
    "_build_lint_checks",
    "_build_linters",
    "_build_models",
    "_build_omp_idle_ttl",
    "_build_omp_web_search",
    "_build_spotlight",
    "_build_tcc_watch",
    "_build_tg_ctl",
    "_build_tgctl_reaper",
    "_build_tmux",
    "_build_tools",
    "_divert_tracked_files",
    "_expand",
    "_explicit_git_hosting_opt_out",
    "_explicit_tools_git_hosting",
    "_plan_hook_blurbs",
    "_resolve_harness_skill_dir",
    "_resolved_permission_tools",
    "build",
    "env_options_from_config",
    "resolve_category_target",
    "resolve_category_targets",
]


def build(
    config: LoadedConfig,
    catalog: Catalog | None,
    *,
    project_type: str = "unknown",
    stack_preset: str | None = None,
    lint_bootstrap: LintBootstrapRequest | None = None,
) -> InstallPlan:
    """Build the ordered :class:`InstallPlan` from config + catalog.

    With ``lint_bootstrap`` the build is LINT-SCOPED: it plans only the repo-local lint tooling
    bootstrap ``rig lint`` needs (``lint_bootstrap_step`` + Oxlint policy actions), through the same
    :class:`InstallPlan` / ``actions.run_plan`` machinery, and none of the apply stages. ``catalog``
    may then be ``None`` (the anti-slop policy is deferred to ``rig apply``). Without it the
    plan is the full apply plan, ``catalog`` is required, and no bootstrap action is ever emitted —
    so ``rig apply`` / ``rig init`` / config-web plans and fingerprints are unaffected by the feature.
    """
    if lint_bootstrap is not None:
        return _build_lint_bootstrap(config, catalog, lint_bootstrap)
    if catalog is None:
        raise PlanError("plan.build needs a catalog unless it is a lint-scoped bootstrap build")
    plan = InstallPlan(
        on_conflict=str(config.defaults.get("on_conflict", "backup")),
        catalog_items={c: _catalog_by_leaf(catalog, c) for c in KNOWN_CATEGORIES},
        known_names=known_names_from_config(config),
    )
    _validate_item_names(config, catalog)
    _build_catalog_stages(plan, config, catalog, project_type, stack_preset)
    _build_harness_stages(plan, config, catalog)
    _build_repo_stages(plan, config, catalog)
    _build_github_stages(plan, config, catalog)
    _build_machine_stages(plan, config)
    # ── tracked_files: hold back writes to committed files that are landed, not written ──
    _divert_tracked_files(config, plan)
    return plan


def _build_lint_bootstrap(
    config: LoadedConfig, catalog: Catalog | None, request: LintBootstrapRequest
) -> InstallPlan:
    """The lint-scoped plan: ``skip`` conflicts so a file a user wrote is never replaced."""
    if request.discovery.root.resolve() != config.repo_root.resolve():
        raise PlanError(
            f"lint bootstrap was discovered at {request.discovery.root} but the config belongs to {config.repo_root}"
        )
    plan = InstallPlan(on_conflict="skip")
    plan_lint_bootstrap(plan, request, catalog)
    return plan


def _build_catalog_stages(
    plan: InstallPlan, config: LoadedConfig, catalog: Catalog, project_type: str, stack_preset: str | None
) -> None:
    """skills (+ plugin skills), agent_hooks, git_hooks dispatcher, ci, mcp."""
    skills_target = plan_skills(plan, config, catalog, project_type, stack_preset)
    _plan_agent_hooks(plan, config, catalog)
    _plan_hook_blurbs(plan, config, catalog, skills_target)
    plan_dispatcher(plan, config, catalog)
    plan_ci(plan, config, catalog)
    plan_mcp(plan, config, catalog)


def _build_harness_stages(plan: InstallPlan, config: LoadedConfig, catalog: Catalog) -> None:
    """Harness mode, permissions, hook bridge and per-harness provisioning."""
    # ── harness (auto-mode / permission provisioning) ─────────────────────────────
    _build_harness(config, plan)
    _build_global_instructions(config, plan)

    # ── permissions (per-harness command allowlist) ───────────────────────────────
    _build_mode(config, plan)
    _build_permissions(config, catalog, plan)
    _build_execpolicy(config, plan)
    _build_guard_policy(config, plan)
    _build_harness_delegated_notes(config, plan)
    # ORDER MATTERS — both omp YAML writers target the same config.yml. Approval
    # creates the file on first-run; web_search then merges exclude. The reverse
    # made approval see an existing file and skip under on_conflict=skip (rig-cli#447).
    _build_omp_web_search(config, plan)

    # ── hook bridge (make agents-hooks/v1 descriptors FIRE in the harness) ─────────
    _build_hook_bridge(config, catalog, plan)

    # ── omp idle-TTL keepalive (new omp processes; live sessions keep spawn-cached TTL) ─
    _build_omp_idle_ttl(config, plan)
    _build_codex_skills_budget(config, plan)


def _build_repo_stages(plan: InstallPlan, config: LoadedConfig, catalog: Catalog) -> None:
    """Per-repo files: models schedule, AGENTS.md, ship, linters, lint checks, project tools, README."""
    # ── models (daily model-freshness checker schedule) ───────────────────────────
    _build_models(config, catalog, plan)

    # ── agents_md (AGENTS.md canonical + CLAUDE.md symlink) ────────────────────────
    _build_agents_symlink(config, plan)

    # ── ship_delegator (per-repo .claude/scripts/pr-ship.sh so `gh ship` works here) ─
    _build_ship_delegator(config, catalog, plan)

    # ── task.code_prefix → .ship-config's SHIP_TASK_CODE_PREFIX ────────────────────
    _build_ship_task_prefix(config, plan)

    # ── linters (per-repo linter/formatter config files) ──────────────────────────
    _build_linters(config, catalog, plan)

    # ── lint_checks (cccc complexity + file-line gate; independent of linters.*) ───
    _build_lint_checks(config, catalog, plan)

    # ── project_tools (Haft / Serena / Sverklo repo integrations) ─────────────────
    _build_project_tools(config, plan)

    # ── ecosystem_readme (README "## Ecosystem" block, rig-cli#308) ───────────────
    _build_ecosystem_readme(config, plan)


def _build_github_stages(plan: InstallPlan, config: LoadedConfig, catalog: Catalog) -> None:
    """GitHub repository settings via gh api + agent-browser."""
    # ORDER MATTERS — actions run in this build order (runner.run_plan iterates plan.actions
    # in sequence, no sort). Enable GitHub Actions BEFORE GHAS: CodeQL default-setup
    # (provisioned by _build_github_ghas) requires Actions to be enabled — GitHub rejects
    # default-setup with "Actions must be enabled for default setup" otherwise. Building
    # actions first makes a brand-new repo converge in ONE apply instead of needing a second.
    _build_github_ruleset(config, catalog, plan)
    _build_github_merge(config, plan)
    _build_github_actions(config, plan)
    _build_github_ghas(config, plan)
    _build_github_browser(config, plan)


def _build_machine_stages(plan: InstallPlan, config: LoadedConfig) -> None:
    """Machine-level settings and rig-managed daemons."""
    # ── tmux (rig-managed tmux configuration) ──────────────────────────────────────
    _build_tmux(config, plan)

    # ── env (rig-managed shell environment variables) ──────────────────────────────
    _build_env(config, plan)

    # ── gitignore (rig-managed block in the GLOBAL git excludes file) ──────────────
    _build_global_excludes(config, plan)

    # ── spotlight (macOS: keep Spotlight from indexing dependency/build dirs) ───────
    _build_spotlight(config, plan)

    # ── bun (isolated linker + global virtual store in ~/.bunfig.toml) ────────────
    _build_bun(config, plan)

    # ── fj (Forgejo: install fj, one-click OAuth login for fj + git, default host) ─
    _build_fj(config, plan)

    # ── git (global git settings via `git config --global`) ──────────────────────────
    _build_git(config, plan)

    # ── tools (the personal CLI ecosystem: tg/review/task/draw/…) ──────────────────
    _build_tools(config, plan)

    # ── tg_ctl (rig-managed tg-ctl inbound daemon LaunchAgent) ─────────────────────
    _build_tg_ctl(config, plan)

    # ── tcc_watch (rig-managed TCC dialog-watcher daemon LaunchAgent, Phase 1 detection) ──
    _build_tcc_watch(config, plan)

    # ── tgctl_reaper (rig-managed periodic orphan-sweep LaunchAgent, agent-tools#697) ──
    _build_tgctl_reaper(config, plan)

    # ── sysmon (rig-managed resource monitor + alerts + auto-heal + session restore, #622) ──
    _build_sysmon(config, plan)

    # ── harness_wrappers (opt-in PATH wrappers that host an interactive harness launch) ──
    _build_harness_wrappers(config, plan)

    # ── internal_dev (per-repo daemon auto-reload post-commit hook) ────────────────
    _build_internal_dev(config, plan)


def _divert_tracked_files(config: LoadedConfig, plan: InstallPlan) -> None:
    """Move actions that write a ``tracked_files`` path out of ``plan.actions`` into ``plan.diverted``.

    Done here, in the one plan builder, so no plan consumer writes such a file into a working
    checkout (see :mod:`riglib.tracked_files`). A no-op without the block or outside a git repo,
    and in a repo with no origin default branch to land on (a local-only repo, say): there the
    files are written into the checkout as usual, never held back with nowhere to go.
    """
    from . import tracked_files

    settings = tracked_files.settings_from_config(config.data)
    if not settings.enabled or not tracked_files.inside_work_tree(config.repo_root):
        return
    if tracked_files.default_branch(config.repo_root) is None:
        plan.notes.append("tracked_files: no origin default branch to land on here (neither origin/HEAD nor "
                          "origin/main|master; `git remote set-head origin --auto` sets origin/HEAD) — rig-owned "
                          "committed files are written into this checkout as usual")
        return
    plan.actions, plan.diverted = tracked_files.divert_actions(plan.actions, config.repo_root, settings)
    if plan.diverted:
        plan.notes.append(
            f"tracked_files: {len(plan.diverted)} action(s) writing a committed file are held back from this "
            "checkout; `rig apply commit` lands them on the default branch"
        )
