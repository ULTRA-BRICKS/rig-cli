"""The plan executor — runs each :class:`~riglib.plan.Action`, returns results.

Stdlib only. Each handler lives in an area module next to this one and implements one action kind,
returning an :class:`~riglib.actions.result.ActionResult`; this module owns the run loop and the
kind -> handler table. Non-fatal errors are collected (the runner continues); the caller decides
how to surface them.
"""

from __future__ import annotations

from typing import Callable

from . import lint_checks
from . import lint_policy_actions
from ..github_auth import reset_auth_gate
from ..logging import log_event
from ..plan import Action, InstallPlan
from .agent_hook_install import _do_install_agent_hook, _do_sync_hook_blurbs
from .agents_md import (
    _do_provision_agents_autonomous_mode,
    _do_provision_agents_orchestrator,
    _do_provision_agents_symlink,
)
from .env_action import _do_provision_env
from .fj_bunfig import _do_provision_bunfig, _do_provision_fj
from .git_config import _do_provision_git_config
from .git_hooks_install import _do_install_ci, _do_install_dispatcher
from .github_actions_action import _do_provision_github_actions
from .github_browser_action import _do_provision_github_browser
from .github_ghas_action import _do_provision_github_ghas
from .github_merge_action import _do_provision_github_merge
from .github_ruleset_action import _do_provision_github_ruleset
from .global_excludes import _do_provision_global_excludes
from .harness_approval import _do_provision_harness_approval
from .harness_extras import _do_harness_wrapper, _do_install_dev_reload_hook, _do_provision_pi_extension
from .harness_guard import _do_install_harness_guard
from .harness_mode import _do_apply_harness
from .harness_settings import (
    _do_provision_codex_skills_budget,
    _do_provision_instruction_policy,
    _do_provision_omp_idle_ttl,
    _do_provision_omp_web_search,
    _do_provision_problem_reporting,
)
from .hook_bridge_register import _do_register_hook_bridge
from .linter_config import (
    _do_provision_linter_bundle,
    _do_provision_linter_config,
    _do_provision_project_tool,
)
from .managed_blocks import _do_apply_ecosystem_readme, _do_provision_execpolicy
from .mcp import _do_register_mcp
from .omp_yaml_state import _reset_omp_yaml_created
from .permissions import _do_provision_permissions
from .result import ActionResult, ApplyReport
from .schedule import _do_provision_schedule
from .ship_delegator import _do_provision_ship_delegator
from .ship_extras import _do_provision_gh_ship_alias, _do_provision_ship_task_prefix
from .skills import _do_copy_skill, _do_link_skill_harness
from .spotlight import _do_provision_spotlight
from .sysmon_action import _do_provision_sysmon
from .tcc_watch_action import _do_provision_tcc_watch
from .tg_ctl_action import _do_provision_tg_ctl
from .tgctl_reaper_action import _do_provision_tgctl_reaper
from .tmux import _do_provision_tmux
from .tools_provision import _do_provision_tools


def run_plan(
    plan: InstallPlan,
    *,
    dry_run: bool = False,
    progress: Callable[[ActionResult], None] | None = None,
    on_start: Callable[[Action], None] | None = None,
) -> ApplyReport:
    """Execute (or dry-run) every action in the plan. Returns the collected report.

    ``on_start(action)`` fires BEFORE each action dispatches (so a caller can show a slow phase as
    in-flight — silence during a hung runner ≠ hang); ``progress(result)`` fires AFTER it
    completes. Both are optional and best-effort in caller code.
    """
    # Fresh auth-gate state per run: a new apply re-notifies + re-waits for a missing login (the user
    # may have logged in since), but WITHIN this run the per-action gate dedups (no ~5× push/wait).
    reset_auth_gate()
    _reset_omp_yaml_created()
    report = ApplyReport()
    for action in plan.actions:
        if on_start is not None and not dry_run:
            on_start(action)
        if dry_run:
            res = ActionResult(action, "planned", action.describe())
        else:
            try:
                res = _dispatch(action, plan.on_conflict)
            except Exception as exc:  # noqa: BLE001 — collect, never abort the whole run
                res = ActionResult(action, "error", f"{type(exc).__name__}: {exc}")
        report.results.append(res)
        log_event(
            "rig.action",
            kind=action.kind,
            item=f"{action.category}/{action.item}",
            status=res.status,
        )
        if progress is not None:
            progress(res)
    return report


def _dispatch(action: Action, on_conflict: str) -> ActionResult:
    handler = _HANDLERS.get(action.kind)
    if handler is None:
        return ActionResult(action, "error", f"no handler for action kind '{action.kind}'")
    return handler(action, on_conflict)


# ── handlers ────────────────────────────────────────────────────────────────────
def _do_record_mode(action: Action, on_conflict: str) -> ActionResult:
    return ActionResult(action, "skipped", f"mode/{action.item}: policy recorded")


_HANDLERS: dict[str, Callable[[Action, str], ActionResult]] = {
    "record_mode": _do_record_mode,
    "copy_skill": _do_copy_skill,
    "link_skill_harness": _do_link_skill_harness,
    # link_skill_plugin reuses the harness-link handler verbatim: both maintain an idempotent
    # symlink at `action.target` pointing to `action.source` (create / re-point / never clobber
    # a real dir) — the only difference is WHICH dir the symlink lands in (skills_target itself
    # vs a harness discovery dir), which is just a different `target` on the Action, not
    # different execution logic.
    "link_skill_plugin": _do_link_skill_harness,
    "install_agent_hook": _do_install_agent_hook,
    "sync_hook_blurbs": _do_sync_hook_blurbs,
    "install_dispatcher": _do_install_dispatcher,
    "install_cccc": lint_checks.do_install_cccc,
    "install_lint_check_hook": lint_checks.do_install_hook,
    "install_lint_check_workflow": lint_checks.do_install_workflow,
    "install_ci": _do_install_ci,
    "register_mcp": _do_register_mcp,
    "apply_harness": _do_apply_harness,
    "provision_permissions": _do_provision_permissions,
    "provision_execpolicy": _do_provision_execpolicy,
    "install_harness_guard": _do_install_harness_guard,
    "provision_harness_approval": _do_provision_harness_approval,
    "provision_omp_web_search": _do_provision_omp_web_search,
    "provision_instruction_policy": _do_provision_instruction_policy,
    "provision_problem_reporting": _do_provision_problem_reporting,
    "provision_pi_extension": _do_provision_pi_extension,
    "register_hook_bridge": _do_register_hook_bridge,
    "provision_omp_idle_ttl": _do_provision_omp_idle_ttl,
    "provision_codex_skills_budget": _do_provision_codex_skills_budget,
    "provision_schedule": _do_provision_schedule,
    "provision_agents_symlink": _do_provision_agents_symlink,
    "provision_agents_orchestrator": _do_provision_agents_orchestrator,
    "provision_agents_autonomous_mode": _do_provision_agents_autonomous_mode,
    "provision_ship_delegator": _do_provision_ship_delegator,
    "provision_ship_task_prefix": _do_provision_ship_task_prefix,
    "provision_gh_ship_alias": _do_provision_gh_ship_alias,
    "lint_policy_blocked": lint_policy_actions.do_lint_policy_blocked,
    "format_policy_blocked": lint_policy_actions.do_format_policy_blocked,
    "provision_linter_config": _do_provision_linter_config,
    "provision_linter_bundle": _do_provision_linter_bundle,
    "lint_bootstrap_step": lint_policy_actions.do_lint_bootstrap_step,
    "provision_project_tool": _do_provision_project_tool,
    "apply_ecosystem_readme": _do_apply_ecosystem_readme,
    "provision_github_ruleset": _do_provision_github_ruleset,
    "provision_github_merge": _do_provision_github_merge,
    "provision_github_ghas": _do_provision_github_ghas,
    "provision_github_actions": _do_provision_github_actions,
    "provision_github_browser": _do_provision_github_browser,
    "provision_tmux": _do_provision_tmux,
    "provision_env": _do_provision_env,
    "provision_global_excludes": _do_provision_global_excludes,
    "provision_spotlight": _do_provision_spotlight,
    "provision_tools": _do_provision_tools,
    "provision_tg_ctl": _do_provision_tg_ctl,
    "provision_tcc_watch": _do_provision_tcc_watch,
    "provision_tgctl_reaper": _do_provision_tgctl_reaper,
    "provision_sysmon": _do_provision_sysmon,
    "provision_bunfig": _do_provision_bunfig,
    "provision_fj": _do_provision_fj,
    "provision_git_config": _do_provision_git_config,
    "harness_wrapper": _do_harness_wrapper,

    "install_dev_reload_hook": _do_install_dev_reload_hook,
}
