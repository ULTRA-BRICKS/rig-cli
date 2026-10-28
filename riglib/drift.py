"""Drift detection — compares the config-declared state against on-disk reality.

Drift is surfaced in **both directions** and never silently reconciled:

- **config→disk (missing)**: rig.yaml declares item X but it is absent / differs on disk.
- **disk→config (extra)**: an installed item Z exists on disk but is not declared in
  rig.yaml (orphan / hand-added).

The plan tells us what *should* be on disk; this module walks the resolved targets and
diffs them. ``rig status`` renders the result; ``rig apply`` converges the
config→disk side (it does not delete extras — extras are reported for the human to
decide, per the "surface, don't auto-reconcile" rule).

Layout: this module owns the scan orchestration (:func:`compute_drift_report`) and the per-kind
dispatch (:func:`detect`); the checkers live in ``drift_*`` area modules (``drift_model`` report
data model, ``drift_extras`` disk→config scanners, ``drift_skills`` / ``drift_ship`` /
``drift_github`` / ``drift_git_hooks`` / ``drift_config_files`` / ``drift_permissions`` /
``drift_harness`` / ``drift_hook_bridge`` / ``drift_env`` / ``drift_services`` per artifact area).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from .actions.harness_mode import harness_settings_file
from .actions.permissions_specs import permissions_settings_file
from .layers import GLOBAL, layer_for_category
from .missing_target import scan_settings_hooks
from .plan import Action, InstallPlan
from .drift_model import DriftItem, DriftNote, DriftReport, KnownItem, _on_darwin
from .drift_skills import (
    _check_agent_hook,
    _check_agents_autonomous_mode,
    _check_agents_orchestrator,
    _check_agents_symlink,
    _check_copy_skill,
    _check_link_skill_plugin,
    _check_mcp,
    _check_skill_harness_link,
    _mcp_config_file,
)
from .drift_config_files import (
    _check_bunfig,
    _check_fj,
    _check_format_policy_blocked,
    _check_git_config,
    _check_global_excludes,
    _check_harness_wrapper,
    _check_linter_bundle,
    _check_linter_config,
    _check_lint_policy_blocked,
    _check_project_tool,
    check_disabled_bunfig,
    check_disabled_global_excludes,
)
from .drift_git_hooks import (
    _check_ci,
    _check_composers,
    _check_disabled_lint_check_hook,
    _check_dispatcher,
    _check_lint_check_analyzer,
    _check_lint_check_hook,
    _check_lint_check_workflow,
    check_disabled_dispatcher,
    hook_manager_shim,
)
from .drift_harness import (
    _check_codex_skills_budget,
    _check_ecosystem_readme,
    _check_execpolicy,
    _check_harness,
    _check_harness_approval,
    _check_harness_guard,
    _check_instruction_policy,
    _check_omp_idle_ttl,
    _check_omp_web_search,
    _check_pi_extension,
    _check_problem_reporting,
)
from .drift_env import _check_env, _check_tmux, _check_tools, check_disabled_env
from .drift_ship import (
    _check_gh_ship_alias,
    _check_ship_delegator,
    _check_ship_task_prefix,
    check_ship_env_for_dropped_repo_action,
)
from .drift_github import (
    _check_github_actions,
    _check_github_ghas,
    _check_github_merge,
    _check_github_ruleset,
)
from .drift_hook_bridge import _check_hook_bridge, _check_opencode_hook_bridge
from .drift_services import (
    _check_internal_dev,
    _check_schedule,
    _check_spotlight,
    _check_sysmon,
    _check_tg_ctl,
    _check_tgctl_reaper,
)
from .drift_permissions import _check_permissions
from .drift_extras import _extras_ci, _extras_hooks, _extras_mcp, _extras_skills


# Names other modules / tests import from ``riglib.drift`` although the checkers live in the
# ``drift_*`` area modules (the model classes in ``drift_model``).
__all__ = [
    "DriftItem",
    "DriftNote",
    "DriftReport",
    "DriftScan",
    "KnownItem",
    "check_disabled_bunfig",
    "check_disabled_dispatcher",
    "check_disabled_env",
    "check_disabled_global_excludes",
    "check_ship_env_for_dropped_repo_action",
    "compute_drift_report",
    "detect",
    "harness_settings_paths",
    "hook_manager_shim",
    "is_global_action",
    "scan_missing_targets",
    "_check_composers",
    "_on_darwin",
    "_check_opencode_hook_bridge",
]


def is_global_action(action: Action) -> bool:
    """Whether an install action belongs to the GLOBAL status layer (see :mod:`riglib.layers`)."""
    return layer_for_category(action.category) == GLOBAL


def harness_settings_paths(plan: InstallPlan) -> list[Path]:
    """The claude-code settings.json file(s) THIS config provisions hooks/permissions into.

    The actions that write the harness settings file (auto-mode, the command allowlist, the
    hook-bridge) all carry the RESOLVED settings path as ``action.target`` — honoring a
    ``harness.settings_path`` / ``permissions.settings_path`` override and the harness kind
    (e.g. a non-``auto`` mode writes the repo-local ``.claude/settings.json``, not the one
    under HOME). Resolve from THOSE so the missing-target scan inspects the file rig actually
    manages, not a hardcoded ``~/.claude/settings.json``. Deduped, order-preserving.

    Scoped to **claude-code** actions: the scanner (:func:`scan_settings_hooks`) understands
    only the claude-code ``settings.json`` ``hooks`` shape. The allowlist write CAN target
    opencode (its ``~/.config/opencode/opencode.json`` has a different schema with no such
    ``hooks`` blocks), so an opencode ``provision_permissions`` action's target must NOT be
    scanned here — feeding it to the claude-hook scanner would misread an opencode file as a
    claude hooks file. We key off the harness kind every action carries in ``options['kind']``.
    """
    kinds = {"apply_harness", "provision_permissions", "register_hook_bridge"}
    paths: list[Path] = []
    seen: set[Path] = set()
    for action in plan.actions:
        if action.kind not in kinds:
            continue
        if action.options.get("kind") != "claude-code":
            continue  # only the claude-code settings.json carries the hook shape we scan
        resolved = harness_settings_file(action)
        if resolved not in seen:
            seen.add(resolved)
            paths.append(resolved)
    return paths


def scan_missing_targets(settings_paths: list[Path] | None = None) -> list:
    """Scan the harness settings.json(s) for hook commands pointing at files that are gone.

    ``settings_paths`` are the resolved harness settings files THIS config manages (from
    :func:`harness_settings_paths`). ``None`` — the ``rig doctor`` case, which loads no config —
    falls back to the claude-code default under HOME (``~/.claude/settings.json``). An EMPTY
    list is NOT the same as ``None``: it means this config provisions no harness settings file
    (e.g. harness + permissions both disabled), so there is nothing to scan — scanning the HOME
    default there would cry wolf on a file the config doesn't manage (the very false positive
    this scan exists to avoid). Findings are deduped across files by the per-finding ``what``
    line (``missing <kind>: <path>``) so the same dead target isn't reported twice.
    """
    if settings_paths is None:
        # the doctor case: the HOME default PLUS every claude-rotate account dir — the files the
        # DEFAULT fan-out manages (rig-cli#368). Filesystem-only, never the shell's
        # CLAUDE_CONFIG_DIR, so the scan is deterministic; doctor inventories the env dir separately.
        from .claude_config_dirs import managed_settings_files

        settings_paths = managed_settings_files()
    findings: list = []
    seen: set[str] = set()
    for settings in settings_paths:
        for finding in scan_settings_hooks(settings):
            if finding.what in seen:
                continue
            seen.add(finding.what)
            findings.append(finding)
    return findings


@dataclass
class DriftScan:
    """The result of :func:`compute_drift_report`: a two-way drift report plus its context."""

    report: DriftReport
    dead_targets: list
    dropped_ship_delegator: list  # list[Action] dropped from the effective plan (non-git only)
    repo_actions_dropped: int  # count of repo-scoped actions dropped (non-git only)
    plan: InstallPlan  # the EFFECTIVE plan drift was computed against (see docstring below)


def compute_drift_report(
    plan: InstallPlan, loaded, env, *, restrict_scan_categories: frozenset[str] | None = None
) -> DriftScan:
    """Scan configured target dirs + compute config<->disk drift for a resolved plan.

    Shared by ``rig status`` (:func:`riglib.cli.cmd_status`) and config-web's drift panel
    (:func:`riglib.config_web_plan.compute_scope_drift`) — the ONE place that (a) computes the
    EFFECTIVE plan (GLOBAL-layer actions only, when ``env.is_git_repo`` is False — repo-scoped
    actions have no layer to report under outside a git repo, mirroring what ``rig apply`` itself
    skips there), (b) wires :func:`detect`'s scan-dir arguments (which target dirs to scan for
    disk→config extras), (c) runs the disabled-category augmentation checks (a category the
    config turned OFF but whose prior artifacts are still on disk: dispatcher / global-excludes /
    env), and (d) runs the missing-target scan (a harness hook command pointing at a file that's
    gone). PURE — no printing, and it does NOT mutate the caller's ``plan`` — a caller does NOT
    need to (and must NOT separately) filter ``plan.actions`` first, and calling this twice on
    the SAME input ``plan`` returns identical results both times (unlike an earlier version of
    this function, which reassigned ``plan.actions`` in place: a second call on that same object
    then saw an already-filtered list and silently reported 0 dropped actions — caught in review).

    Returns a :class:`DriftScan`. ``dropped_ship_delegator`` is the repo-scoped ``ship_delegator``
    actions dropped from the EFFECTIVE plan in the non-git case (their GLOBAL ``ship_env``
    machine-env-file check still runs here even though the repo-local action was dropped) — a
    caller needing to render them (e.g. `cmd_status`'s area summary) gets them back;
    ``repo_actions_dropped`` is the total count of repo-scoped actions dropped, for a caller's own
    "N action(s) not evaluated" note. ``scan.plan`` is the plan actually scanned — a caller doing
    further rendering (area summaries, a plan preview) after this call MUST use ``scan.plan``,
    not the original ``plan`` it passed in, to see the same GLOBAL-only view drift was computed
    against. Both ``dropped_ship_delegator``/``repo_actions_dropped`` are empty/0, and
    ``scan.plan is plan``, when ``env.is_git_repo`` is True.

    ``restrict_scan_categories`` (config-web's Global tab; ``None`` for ``rig status``, which
    scans everything the cascade enables) additionally requires a category to be IN this set
    before its extras-scan dir is added — on top of the existing enabled-check. Needed because
    the Global tab's PLAN is deliberately narrowed to only the WRITABLE-global categories
    (:func:`riglib.config_web_plan.build_scope_plan`'s docstring explains why), but this
    function's scan-dir wiring is independent of the plan's own action list — without this
    restriction, the Global tab would scan ``~/.agents/skills``/hooks/MCP dirs for "extras" it
    can NEVER resolve from that tab (its plan declares zero skills/agent_hooks/mcp actions by
    design), flagging every skill a REPO tab legitimately installed as false-positive drift on a
    tab that never renders those settings (caught in review — a regression the Global-plan
    narrowing itself introduced, since before that narrowing the plan and the scan agreed).
    """
    effective_plan, dropped_ship_delegator, repo_actions_dropped = _effective_plan(plan, env)
    targets = _scan_targets(loaded, env, restrict_scan_categories)
    report = detect(
        effective_plan,
        scan_skill_dirs=targets.skill_dirs,
        scan_ci_dirs=targets.ci_dirs,
        scan_mcp_files=targets.mcp_files,
        scan_hook_dirs=targets.hook_dirs,
    )
    # non-git cwd: the repo-scoped delegator actions were dropped above (in the non-git partition),
    # but their GLOBAL ship_env (machine env file) check must still run — apply reconciles that
    # file here too.
    for action in dropped_ship_delegator:
        check_ship_env_for_dropped_repo_action(action, report)
    _check_disabled_categories(loaded, env, report, restrict_scan_categories)
    # missing-target: a hook command in the harness settings.json that points at a file gone
    # from disk (the dead-rtk-hook case) surfaces PROACTIVELY here, before it bites at runtime
    # as a generic "PreToolUse error". This is independent of config↔disk drift. Scan the
    # settings file(s) THIS config actually provisions (honoring a settings_path / harness-kind
    # override), not a hardcoded ~/.claude/settings.json.
    dead_targets = scan_missing_targets(harness_settings_paths(effective_plan))
    return DriftScan(
        report=report,
        dead_targets=dead_targets,
        dropped_ship_delegator=dropped_ship_delegator,
        repo_actions_dropped=repo_actions_dropped,
        plan=effective_plan,
    )


def _restricted_out(restrict_scan_categories: frozenset[str] | None, category: str) -> bool:
    return restrict_scan_categories is not None and category not in restrict_scan_categories


def _effective_plan(plan: InstallPlan, env) -> tuple[InstallPlan, list, int]:
    """``(effective plan, dropped ship_delegator actions, dropped repo-action count)``.

    The REPO layer only exists inside a git repository. plan.build still emits default-on repo
    actions from built-in defaults even when no repo config file was loaded; outside git those
    actions have no layer to report under, so drift detection must drop them (as must the
    summary a caller renders from the returned dropped list). Builds a NEW plan rather than
    mutating the caller's — see :func:`compute_drift_report`'s "PURE" note.

    ship_delegator carries the GLOBAL ship_env (machine env file) check, and apply does NOT drop
    repo actions outside git — it still reconciles that file. The dropped actions are returned so
    the env-file check still runs (status/apply parity for the machine-wide artifact; the
    repo-local delegator part stays dropped).
    """
    if env.is_git_repo:
        return plan, [], 0
    global_actions = []
    repo_actions = []
    for action in plan.actions:
        (global_actions if is_global_action(action) else repo_actions).append(action)
    effective_plan = InstallPlan(
        actions=global_actions, on_conflict=plan.on_conflict, notes=plan.notes,
        catalog_items=plan.catalog_items, known_names=plan.known_names,
        diverted=list(getattr(plan, "diverted", [])),  # every InstallPlan field travels along
    )
    dropped_ship_delegator = [a for a in repo_actions if a.category == "ship_delegator"]
    return effective_plan, dropped_ship_delegator, len(repo_actions)


@dataclass
class _ScanTargets:
    """Configured target locations scanned for disk→config extras even when no action targets them."""

    skill_dirs: list[Path] = field(default_factory=list)
    ci_dirs: list[Path] = field(default_factory=list)
    mcp_files: list[Path] = field(default_factory=list)
    hook_dirs: list[Path] = field(default_factory=list)


def _scan_targets(loaded, env, restrict: frozenset[str] | None) -> _ScanTargets:
    """Wire :func:`detect`'s scan-dir arguments.

    CI + MCP targets are REPO-LOCAL with clear ownership → scan even when the category is
    disabled, so a previously-applied gate/server left on disk surfaces as disk→config drift.
    Skills/agent-hooks live in SHARED global dirs (~/.agents/skills, ~/.claude/hooks) where other
    tools' entries legitimately coexist, so only scan them when the category is enabled (flagging
    every global skill as "extra" would be noise).
    """
    from .plan import resolve_category_target, resolve_category_targets

    targets = _ScanTargets()
    if _category_scanned(loaded, restrict, "skills"):
        targets.skill_dirs.extend(_present(resolve_category_target(loaded, "skills")))
    if _category_scanned(loaded, restrict, "agent_hooks"):
        targets.hook_dirs.extend(resolve_category_targets(loaded, "agent_hooks"))
    # CI + MCP are REPO-LOCAL — only scan them when this IS a repo (a non-git dir has no repo
    # layer; scanning a cwd-relative .github there would be meaningless). Gated on the SAME
    # env.is_git_repo the effective-plan partition uses, so a scope that forces a non-git
    # env (config-web's Global tab, which has no repo context even when $HOME happens to be a
    # git checkout) never scans a repo-local CI dir either.
    if env.is_git_repo and not _restricted_out(restrict, "ci"):
        targets.ci_dirs.extend(_present(resolve_category_target(loaded, "ci")))  # CI: scan unconditionally (repo-local)
    if not _restricted_out(restrict, "mcp"):
        mcp_target = resolve_category_target(loaded, "mcp")  # MCP: scan unconditionally (global)
        if mcp_target:
            targets.mcp_files.append(mcp_target if mcp_target.suffix == ".json" else mcp_target / "mcp.json")
    return targets


def _category_scanned(loaded, restrict: frozenset[str] | None, category: str) -> bool:
    """The category is not disabled in config and is inside the restricted scope (if any)."""
    return loaded.category(category).get("enabled") is not False and not _restricted_out(restrict, category)


def _present(target: Path | None) -> list[Path]:
    return [target] if target else []


def _check_disabled_categories(loaded, env, report: DriftReport, restrict: frozenset[str] | None) -> None:
    """Disabled-category augmentation checks: prior artifacts of a category config turned OFF."""
    _check_disabled_dispatcher_config(loaded, report, restrict)
    _check_disabled_gitignore_config(loaded, report, restrict)
    _check_missing_models_block(loaded, report, restrict)
    _check_disabled_env_config(loaded, report, restrict)
    _check_bun_config(loaded, env, report, restrict)


def _check_disabled_dispatcher_config(loaded, report: DriftReport, restrict: frozenset[str] | None) -> None:
    # disabled-but-installed dispatcher: config turned the dispatcher off, but a prior apply
    # may have left core.hooksPath pointing at the installed composer dir. apply won't delete
    # it, so surface it as disk→config drift. Gated on the SAME restriction as the scan-dirs
    # — the Global tab's plan can't declare a "git_hooks" action either, so it must not
    # report drift for a category it never renders/applies (caught in review, same class of bug
    # as the skills-false-positive the scan-dir restriction fixes).
    git_hooks_cfg = loaded.category("git_hooks")
    disp_cfg = git_hooks_cfg.get("dispatcher", {}) if isinstance(git_hooks_cfg, dict) else {}
    if not isinstance(disp_cfg, dict):
        return
    if not disp_cfg.get("enabled") and not _restricted_out(restrict, "git_hooks"):
        check_disabled_dispatcher(loaded.repo_root, report)
    # the fragment is a lint_checks_machine artifact (its drift item carries that category), so that is the
    # category a restricted scan must name — not git_hooks, whose dispatcher it merely rides
    if disp_cfg.get("enabled") and not _restricted_out(restrict, "lint_checks_machine"):
        _check_disabled_lint_check_hook(loaded, disp_cfg, report)


def _check_disabled_gitignore_config(loaded, report: DriftReport, restrict: frozenset[str] | None) -> None:
    # disabled-but-installed global-excludes block: config opted the gitignore category out, but a
    # prior apply may have left the rig-managed block in the global excludes file. apply won't
    # remove it, so surface it as disk→config drift (mirrors the disabled-dispatcher scan; this is
    # a GLOBAL, machine-wide artifact, not repo-local). "gitignore" IS writable-global, so this
    # one is NOT restricted out for the Global scope — it belongs there.
    gi_cfg = loaded.data.get("gitignore")
    if not (isinstance(gi_cfg, dict) and gi_cfg.get("enabled") is False) or _restricted_out(restrict, "gitignore"):
        return
    from .config import GITIGNORE_DEFAULT_EXCLUDESFILE

    gi_opts: dict[str, object] = {"xdg_default": GITIGNORE_DEFAULT_EXCLUDESFILE}
    override = gi_cfg.get("excludesfile")
    if isinstance(override, str) and override:
        gi_opts["excludesfile"] = override
    check_disabled_global_excludes(
        Action(
            kind="provision_global_excludes",
            category="gitignore",
            item="block",
            source=loaded.repo_root,
            target=loaded.repo_root,
            options=gi_opts,
        ),
        report,
    )


def _check_missing_models_block(loaded, report: DriftReport, restrict: frozenset[str] | None) -> None:
    # a host config with no ``models:`` block at all: the daily model-freshness schedule is
    # silently absent (a global config written before the scaffold default gained the block,
    # rig-cli#721). Surface it as drift naming the one command that turns it on; ``enabled:
    # false`` is an explicit opt-out and stays quiet, and a machine with no global config yet is
    # `rig init`'s job, not this check's.
    if loaded.global_path is not None and "models" not in loaded.data and not _restricted_out(restrict, "models"):
        report.items.append(DriftItem(
            "missing", "models", "model-freshness", loaded.global_path,
            "no `models:` block, so no daily model-freshness schedule — enable with: "
            "rig config set --global models.enabled true (or opt out: models.enabled false)",
        ))


def _check_disabled_env_config(loaded, report: DriftReport, restrict: frozenset[str] | None) -> None:
    # disabled-but-installed shell env vars: config opted the env category out, but a prior
    # apply may have left rig.env.sh + its live source line in rc_path. apply won't remove
    # them, so surface as disk→config drift (mirrors the disabled-global-excludes scan above;
    # also a GLOBAL, machine-wide artifact). A higher-consequence orphan than a stale tmux conf
    # — the vars keep applying to every shell, not just tmux sessions — so worth the extra scan
    # even though tmux itself has no equivalent check. "env" is status-global but NOT
    # writable-global (like skills/agent_hooks) — restricted out for the Global scope for the
    # same reason as the dispatcher check above.
    env_cfg = loaded.data.get("env")
    if not (isinstance(env_cfg, dict) and env_cfg.get("enabled") is False) or _restricted_out(restrict, "env"):
        return
    from .plan import env_options_from_config

    env_disabled_options = env_options_from_config(env_cfg, loaded.repo_root)
    check_disabled_env(
        Action(
            kind="provision_env",
            category="env",
            item="vars",
            source=loaded.repo_root,
            # matches `_build_env`'s own target (rc_path) — the scan derives everything
            # from `options` regardless, but keeping the two constructions of this action
            # kind identical avoids a needless divergence (review).
            target=Path(env_disabled_options["rc_path"]),
            options=env_disabled_options,
        ),
        report,
    )


def _check_bun_config(loaded, env, report: DriftReport, restrict: frozenset[str] | None) -> None:
    if _restricted_out(restrict, "bun"):
        return
    bun_cfg = loaded.data.get("bun")
    if isinstance(bun_cfg, dict) and bun_cfg.get("enabled") is False:
        check_disabled_bunfig(report)
    # broken bun worktree installs: with the global store on, a node_modules symlink to another
    # checkout or broken links into links/ is drift apply cannot fix (it never deletes) — surfaced
    # with the per-worktree fix; an install that merely skips the store is only a note.
    from .bun_store import check_worktree_installs, worktree_check_enabled

    if env.is_git_repo and worktree_check_enabled(loaded.global_data.get("bun"), bun_cfg):
        check_worktree_installs(env.repo_root, report, merged_bun=bun_cfg)


@dataclass
class _Declared:
    """What the plan's actions declare, keyed by the dir/file the extras scanners walk."""

    skill_dirs: dict[Path, set[str]]
    ci_dirs: dict[Path, set[str]]
    mcp: dict[Path, set[str]]
    hook_dirs: dict[Path, set[str]]


def _declare_skill(action: Action, declared: _Declared) -> None:
    declared.skill_dirs.setdefault(action.target.parent, set()).add(action.target.name)


def _declare_agent_hook(action: Action, declared: _Declared) -> None:
    descriptor = action.options.get("descriptor")
    if descriptor:
        declared.hook_dirs.setdefault(action.target, set()).add(descriptor)


def _declare_ci(action: Action, declared: _Declared) -> None:
    if action.options.get("slot") != "ship":
        declared.ci_dirs.setdefault(action.target, set()).add(f"{action.item}.yml")


def _declare_lint_check_workflow(action: Action, declared: _Declared) -> None:
    # rig-owned file in the SAME workflows dir the extras scan covers: declared, never "extra"/"known"
    wf_path = action.target / str(action.options["rel_path"])
    declared.ci_dirs.setdefault(wf_path.parent, set()).add(wf_path.name)


def _declare_mcp(action: Action, declared: _Declared) -> None:
    server_key = str(action.options.get("server") or action.item)
    declared.mcp.setdefault(_mcp_config_file(action), set()).add(server_key)


_DECLARERS = {
    "copy_skill": _declare_skill,
    "link_skill_plugin": _declare_skill,
    "install_agent_hook": _declare_agent_hook,
    "install_ci": _declare_ci,
    "install_lint_check_workflow": _declare_lint_check_workflow,
    "register_mcp": _declare_mcp,
}


def _check_handlers(self_merge_files: set[Path]) -> dict:
    """Action kind → drift checker ``(action, report)``.

    Built per call (not a module constant) so each checker is resolved through this module's
    namespace at detect time — the checkers are seams the test suite stubs on ``riglib.drift``.
    ``provision_github_browser`` has no entry: the agent-browser backend has no cheap read-back,
    status doesn't probe the UI.
    """

    def permissions(action: Action, report: DriftReport) -> None:
        # settings files an active self-merge harness action writes the ship rules into: the
        # permissions extras check suppresses those rig-owned rules ONLY for these files.
        _check_permissions(action, report, self_merge_owned=permissions_settings_file(action) in self_merge_files)

    return {
        "copy_skill": _check_copy_skill,
        "link_skill_harness": _check_skill_harness_link,
        "link_skill_plugin": _check_link_skill_plugin,
        "install_agent_hook": _check_agent_hook,
        "install_ci": _check_ci,
        "install_dispatcher": _check_dispatcher,
        "install_cccc": _check_lint_check_analyzer,
        "install_lint_check_hook": _check_lint_check_hook,
        "install_lint_check_workflow": _check_lint_check_workflow,
        "register_mcp": _check_mcp,
        "apply_harness": _check_harness,
        "provision_permissions": permissions,
        "provision_execpolicy": _check_execpolicy,
        "install_harness_guard": _check_harness_guard,
        "provision_harness_approval": _check_harness_approval,
        "provision_omp_web_search": _check_omp_web_search,
        "provision_instruction_policy": _check_instruction_policy,
        "provision_problem_reporting": _check_problem_reporting,
        "provision_pi_extension": _check_pi_extension,
        "register_hook_bridge": _check_hook_bridge,
        "provision_omp_idle_ttl": _check_omp_idle_ttl,
        "provision_codex_skills_budget": _check_codex_skills_budget,
        "provision_schedule": _check_schedule,
        "provision_agents_symlink": _check_agents_symlink,
        "provision_agents_orchestrator": _check_agents_orchestrator,
        "provision_agents_autonomous_mode": _check_agents_autonomous_mode,
        "provision_ship_delegator": _check_ship_delegator,
        "provision_gh_ship_alias": _check_gh_ship_alias,
        "lint_policy_blocked": _check_lint_policy_blocked,
        "format_policy_blocked": _check_format_policy_blocked,
        "provision_linter_config": _check_linter_config,
        "provision_linter_bundle": _check_linter_bundle,
        "provision_project_tool": _check_project_tool,
        "apply_ecosystem_readme": _check_ecosystem_readme,
        "provision_github_ruleset": _check_github_ruleset,
        "provision_github_merge": _check_github_merge,
        "provision_github_ghas": _check_github_ghas,
        "provision_github_actions": _check_github_actions,
        "provision_tmux": _check_tmux,
        "provision_env": _check_env,
        "provision_global_excludes": _check_global_excludes,
        "provision_tools": _check_tools,
        "provision_tg_ctl": _check_tg_ctl,
        "provision_tgctl_reaper": _check_tgctl_reaper,
        "provision_sysmon": _check_sysmon,
        "provision_spotlight": _check_spotlight,
        "provision_bunfig": _check_bunfig,
        "provision_fj": _check_fj,
        "provision_git_config": _check_git_config,
        "harness_wrapper": _check_harness_wrapper,
        "provision_ship_task_prefix": _check_ship_task_prefix,
        "install_dev_reload_hook": _check_internal_dev,
    }


def detect(
    plan: InstallPlan,
    *,
    scan_skill_dirs: list[Path] | None = None,
    scan_ci_dirs: list[Path] | None = None,
    scan_mcp_files: list[Path] | None = None,
    scan_hook_dirs: list[Path] | None = None,
) -> DriftReport:
    """Compute drift for a resolved plan against current disk state.

    The ``scan_*`` arguments are configured target locations that should be scanned for
    disk→config extras EVEN IF no action targets them (e.g. ``ci: {all: false}`` yields zero
    CI actions, but undeclared workflows on disk are still drift; an MCP config with no
    declared servers should still surface undeclared entries). The caller passes the
    resolved category targets so the extras scan is complete.

    ``plan.known_names`` (the ``<category>.known`` config lists) and ``plan.catalog_items`` let
    the extras scanners file an undeclared item under :attr:`DriftReport.known` (an origin rig
    can name — informational) instead of as drift. See :mod:`riglib.provenance`.
    """
    report = DriftReport()
    # getattr: callers hand in duck-typed plan stand-ins (an object with just ``actions``) — no
    # catalog / config knowledge then, the conservative reading (everything undeclared is drift).
    known = getattr(plan, "known_names", None) or {}
    catalog = getattr(plan, "catalog_items", None) or {}
    declared = _Declared(
        skill_dirs=_empty_declarations(scan_skill_dirs),
        ci_dirs=_empty_declarations(scan_ci_dirs),
        mcp=_empty_declarations(scan_mcp_files),
        hook_dirs=_empty_declarations(scan_hook_dirs),
    )
    handlers = _check_handlers(_self_merge_files(plan))
    for action in plan.actions:
        check = handlers.get(action.kind)
        if check is not None:
            check(action, report)
        declare = _DECLARERS.get(action.kind)
        if declare is not None:
            declare(action, declared)
    _extras_skills(declared.skill_dirs, report, known.get("skills", set()), catalog.get("skills"))
    _extras_ci(declared.ci_dirs, report, known.get("ci", set()), catalog.get("ci"))
    _extras_mcp(declared.mcp, report, known.get("mcp", set()))
    _extras_hooks(declared.hook_dirs, report, known.get("agent_hooks", set()), catalog.get("agent_hooks"))
    return report


def _empty_declarations(scan_targets: list[Path] | None) -> dict[Path, set[str]]:
    return {target: set() for target in (scan_targets or [])}


def _self_merge_files(plan: InstallPlan) -> set[Path]:
    """Settings files an active self-merge harness action writes the ship rules into.

    The permissions extras check suppresses those rig-owned rules as "extras" ONLY for these
    files — so a self_merge:false plan (or a permissions action targeting a DIFFERENT file) still
    reports stray ship rules as drift-extras, keeping two-way drift honest (codex review).
    """
    return {
        harness_settings_file(a)
        for a in plan.actions
        if a.kind == "apply_harness" and a.options.get("self_merge")
    }
