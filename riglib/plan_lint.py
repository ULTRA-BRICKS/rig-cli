"""Plan stages for per-repo linter/formatter configs (``linters:``) and the CCCC lint-check gate
(``lint_checks:``).

Split out of :mod:`riglib.plan`.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any
from .catalog import Catalog
from .catalog import rule_provider_carrier
from .config import LoadedConfig
from .plan_model import Action
from .plan_model import InstallPlan
from .plan_model import PlanError


def _build_linters(config: LoadedConfig, catalog: Catalog, plan: InstallPlan) -> None:
    """Plan generic config carriers plus Rig-owned Oxc rule policy.

    Explicit ``linters.items`` remain generic and can be provisioned regardless of the active lint
    engine. Rule policy is different: Rig only emits Oxc policy/plugin actions when the repository
    is ready to execute them. A foreign/no-linter repository gets one loud blocked action containing
    a copy-ready migration prompt while the rest of ``rig apply`` can continue.
    """
    li = config.data.get("linters")
    if li is None:
        li = {}
    if not isinstance(li, dict) or li.get("enabled") is False:
        return

    _plan_linter_bundles(li, config, catalog, plan)
    item_paths = _plan_linter_items(li, config, catalog, plan)

    stack_parts = set((config.stack or "").split("/"))
    typescript_stack = bool({"ts", "typescript"} & stack_parts)
    rules_explicit = "rules" in li
    if not typescript_stack and not rules_explicit:
        return
    if "oxlint.config.ts" in item_paths:
        raise PlanError(
            "linters.items targets oxlint.config.ts while Rig rule policy also owns that file; "
            "remove the item and configure linters.rules instead"
        )
    _plan_oxc_rule_policy(li, item_paths, config, catalog, plan)


def _plan_linter_bundles(li: dict, config: LoadedConfig, catalog: Catalog, plan: InstallPlan) -> None:
    bundles = li.get("bundles", {})
    if not isinstance(bundles, dict):
        return
    for name, spec in bundles.items():
        if not isinstance(spec, dict) or spec.get("enabled") is False:
            continue
        plan.actions.append(Action(
            kind="provision_linter_bundle", category="linters", item=str(name),
            source=catalog.source, target=config.repo_root,
            options={"source_rel": str(spec["source"]), "target_rel": str(spec["target"])},
        ))


def _linter_item_content(name: Any, spec: dict, catalog: Catalog, rel_path: str) -> str | None:
    """Resolve one explicit linter item's content (source-backed gets a managed header), or None to skip."""
    from .linter_carriers import read_source_text
    from .managed_config import SOURCE_BACKED, with_managed_header

    content = spec.get("content")
    source_rel = spec.get("source")
    source_backed = isinstance(source_rel, str) and bool(source_rel)
    if source_backed:
        try:
            content = read_source_text(catalog.source, source_rel)
        except ValueError as exc:
            raise PlanError(f"linters.items.{name}.source: {exc}") from exc
    if not isinstance(content, str) or not content:
        return None
    if source_backed:
        content = with_managed_header(
            rel_path,
            content,
            source=f"agent-tools/{source_rel} + rig.yaml selection",
            management_class=SOURCE_BACKED,
        )
    return content


def _plan_linter_items(li: dict, config: LoadedConfig, catalog: Catalog, plan: InstallPlan) -> set[str]:
    items = li.get("items", {})
    item_paths: set[str] = set()
    if not isinstance(items, dict):
        return item_paths
    for name, spec in items.items():
        if not isinstance(spec, dict) or spec.get("enabled") is False:
            continue
        rel_path = spec.get("path")
        if not isinstance(rel_path, str) or not rel_path:
            continue
        content = _linter_item_content(name, spec, catalog, rel_path)
        if content is None:
            continue
        item_paths.add(PurePosixPath(rel_path).as_posix())
        plan.actions.append(
            Action(
                kind="provision_linter_config",
                category="linters",
                item=str(name),
                source=catalog.source,
                target=config.repo_root,
                options={
                    "tool": str(spec.get("tool") or ""),
                    "role": str(spec.get("role") or "linter"),
                    "rel_path": rel_path,
                    "content": content,
                },
            )
        )
    return item_paths


def _plan_oxfmt_baseline(formatter_env: Any, config: LoadedConfig, catalog: Catalog, plan: InstallPlan) -> None:
    from .managed_config import SOURCE_BACKED, with_managed_header

    if not formatter_env.ready:
        plan.actions.append(Action(
            kind="format_policy_blocked", category="linters", item="formatter",
            source=catalog.source, target=config.repo_root,
            options={"reason": formatter_env.reason, "agent_prompt": formatter_env.agent_prompt},
        ))
        return
    source_rel = "linters/oxc/.oxfmtrc.jsonc"
    source_file = catalog.source / source_rel
    if not source_file.is_file():
        raise PlanError(f"Oxfmt carrier is missing: {source_file}")
    try:
        formatter_content = source_file.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except (OSError, UnicodeDecodeError) as exc:
        raise PlanError(f"cannot read Oxfmt carrier {source_file}: {exc}") from exc
    formatter_content = with_managed_header(
        ".oxfmtrc.jsonc", formatter_content,
        source=f"agent-tools/{source_rel}", management_class=SOURCE_BACKED,
    )
    plan.actions.append(Action(
        kind="provision_linter_config", category="linters", item="rig-oxfmt-baseline",
        source=source_file, target=config.repo_root,
        options={"tool": "oxfmt", "role": "formatter", "rel_path": ".oxfmtrc.jsonc",
                 "content": formatter_content, "rig_owned": True},
    ))
    if formatter_env.foreign_formatters:
        plan.notes.append(
            "formatters: Oxfmt is available alongside " + ", ".join(formatter_env.foreign_formatters)
            + "; Rig applies the Oxfmt baseline but duplicate/conflicting format scripts should be migrated deliberately"
        )


def _plan_anti_slop_bundle(catalog: Catalog, config: LoadedConfig, plan: InstallPlan) -> None:
    try:
        source_rel, _source_root = rule_provider_carrier(catalog.source, "anti-slop")
    except Exception as exc:
        raise PlanError(str(exc)) from exc
    if not any(a.kind == "provision_linter_bundle" and a.options.get("target_rel") == "tools/oxlint/anti-slop" for a in plan.actions):
        plan.actions.append(Action(
            kind="provision_linter_bundle", category="linters", item="anti-slop",
            source=catalog.source, target=config.repo_root,
            options={"source_rel": source_rel, "target_rel": "tools/oxlint/anti-slop"},
        ))


def _plan_oxc_rule_policy(li: dict, item_paths: set[str], config: LoadedConfig, catalog: Catalog, plan: InstallPlan) -> None:
    from .lint_policy import anti_slop_required, render_oxlint_config, resolve_rule_severities, rule_policy_summary
    from .linter_environment import inspect_formatter_environment, inspect_linter_environment
    from .linter_policy_gate import policy_block_reason
    from .linter_policy_gate import type_aware_note as policy_type_aware_note

    rules_cfg = li.get("rules", {})
    if not isinstance(rules_cfg, dict):
        rules_cfg = {}
    severities = resolve_rule_severities(rules_cfg)
    summary = rule_policy_summary(rules_cfg)
    plan.notes.append(
        "linters: effective rule policy — "
        f"{summary['error']} error, {summary['warn']} warn, {summary['off']} off"
    )

    anti_slop = anti_slop_required(severities)
    env = inspect_linter_environment(config.repo_root, anti_slop=anti_slop)
    formatter_env = inspect_formatter_environment(config.repo_root)
    if ".oxfmtrc.jsonc" not in item_paths:
        _plan_oxfmt_baseline(formatter_env, config, catalog, plan)

    blocked_reason = policy_block_reason(env)
    if blocked_reason:
        plan.actions.append(
            Action(
                kind="lint_policy_blocked",
                category="linters",
                item="rules",
                source=catalog.source,
                target=config.repo_root,
                options={"reason": blocked_reason, "agent_prompt": env.agent_prompt},
            )
        )
        return

    type_aware_note = policy_type_aware_note(env)
    if type_aware_note:
        plan.notes.append(type_aware_note)

    if env.foreign_linters:
        plan.notes.append(
            "linters: Oxlint is available alongside "
            + ", ".join(env.foreign_linters)
            + "; Rig applies Oxc policy but the repository should avoid duplicate/conflicting lint scripts"
        )

    generated = render_oxlint_config(rules_cfg, type_aware=env.type_aware)
    plan.actions.append(
        Action(
            kind="provision_linter_config",
            category="linters",
            item="rig-oxlint-policy",
            source=catalog.source,
            target=config.repo_root,
            options={
                "tool": "oxlint",
                "role": "linter",
                "rel_path": "oxlint.config.ts",
                "content": generated,
                "preview_findings": li.get("preview") is not False,
                "rig_owned": True,
            },
        )
    )

    if anti_slop:
        _plan_anti_slop_bundle(catalog, config, plan)


def _build_lint_checks(config: LoadedConfig, catalog: Catalog, plan: InstallPlan) -> None:
    """Plan the ``rig lint check`` activation: pinned analyzer, pre-commit hook, CI workflow.

    Default **ON** and deliberately NOT inside :func:`_build_linters`: the check has no dependency
    on Oxc, Biome or ``linters.enabled`` — a Biome-only repo, a repo with no linter and
    ``linters.enabled: false`` all get it. The analyzer and the hook are MACHINE-wide artifacts
    (category ``lint_checks_machine``, GLOBAL layer, visible to ``rig status`` outside a repo): the
    hook follows the GLOBAL layer's switch (:func:`lint_checks_artifacts.lint_checks_intent`), never
    one repo's ``rig.yaml``, which would otherwise write a hook that blocks every OTHER repo; the
    per-machine, harmless analyzer is installed whenever any layer (global or this repo) runs
    complexity. A repo's ``lint_checks.enabled: false`` / ``complexity.enabled: false`` only reaches
    the repo's own workflow (category ``lint_checks``, REPO layer).
    """
    from . import cccc_install, lint_checks_artifacts as arts

    intent = arts.lint_checks_intent(config)
    # The CLI itself is a consumer even when automated hooks and CI are disabled.
    # `rig apply commit` must satisfy the install hint printed by a manual lint check.
    if intent.install_analyzer:
        plan.actions.append(
            Action(
                kind="install_cccc", category="lint_checks_machine", item="cccc", source=catalog.source,
                target=cccc_install.managed_binary_path(), options={"version": cccc_install.PINNED_VERSION},
            )
        )
    if intent.machine_enabled:
        # the analyzer action is appended FIRST: the hook handler runs after it and refuses to write the
        # machine-wide hook unless that analyzer verified (see riglib.actions.lint_checks)
        _plan_lint_check_hook(config, catalog, plan, needs_analyzer=intent.needs_analyzer)
    if intent.repo.enabled:
        _plan_lint_check_workflow(config, catalog, plan, with_analyzer=intent.workflow_with_analyzer)


def _lint_check_hook_blocker(disp_cfg: Any, needs_analyzer: bool) -> str | None:
    """Why the pre-commit fragment must NOT be planned (an explicit plan note), or None to plan it.

    Two blockers beyond the dispatcher/fragment switches: a platform with no pinned analyzer (the
    hook would block every commit on a machine that can never have the analyzer; irrelevant when
    complexity is disabled) and an unpinnable ``rig`` (a bare name pins nothing). A merely PENDING
    install keeps the hook in the plan, but the apply handler writes it only once the analyzer
    verified, so a failed download never leaves a newly enabled hook that blocks every commit.
    """
    from . import cccc_install, lint_checks_artifacts as arts

    if not (isinstance(disp_cfg, dict) and disp_cfg.get("enabled")):
        return (
            "pre-commit hook NOT installed — git_hooks.dispatcher.enabled is false, so nothing "
            "would run `rig lint check --staged` on commit (CI and `rig lint check` still work)"
        )
    if arts.hook_fragment_disabled(disp_cfg):
        return "pre-commit hook disabled by git_hooks.dispatcher.fragments.lint-check.enabled: false"
    if not needs_analyzer:
        return None
    try:
        cccc_install.pin_for()
    except cccc_install.UnsupportedPlatform as exc:
        return f"pre-commit hook NOT installed — it would block every commit on a machine that can never have the analyzer: {exc}"
    return None


def _plan_lint_check_hook(config: LoadedConfig, catalog: Catalog, plan: InstallPlan, *, needs_analyzer: bool) -> None:
    from . import lint_checks_artifacts as arts
    from .actions.agent_hook_install import RigPinError, pinned_rig_path

    gh = config.category("git_hooks")
    disp_cfg = gh.get("dispatcher", {}) if isinstance(gh, dict) else {}
    blocker = _lint_check_hook_blocker(disp_cfg, needs_analyzer)
    if blocker is not None:
        plan.notes.append(f"lint_checks: {blocker}")
        return
    try:
        rig_bin = pinned_rig_path()
    except RigPinError as exc:
        plan.notes.append(f"lint_checks: pre-commit hook NOT installed — {exc}")
        return
    plan.actions.append(
        Action(
            kind="install_lint_check_hook", category="lint_checks_machine", item="pre-commit-hook",
            source=catalog.source, target=arts.hook_fragment_path(arts.resolve_dispatcher_dir(disp_cfg, config.repo_root)),
            options={"rig_bin": rig_bin, "needs_analyzer": needs_analyzer},
        )
    )


def _plan_lint_check_workflow(config: LoadedConfig, catalog: Catalog, plan: InstallPlan, *, with_analyzer: bool) -> None:
    from . import lint_checks_artifacts as arts, lint_checks_ci

    ci = config.category("ci")
    ci_target = ci.get("target", config.defaults.get("ci_target", ".github/workflows"))
    if ci.get("enabled") is False or ci_target != ".github/workflows":
        plan.notes.append(
            "lint_checks: CI workflow not written — ci.enabled is false or ci.target is not `.github/workflows` "
            "(GitHub only runs workflows from there)"
        )
        return
    rig_source = lint_checks_ci.rig_source(config.data)
    if rig_source is None:
        plan.notes.append(
            "lint_checks: CI workflow not written — no immutable rig install source is configured, and a "
            "pin that does not resolve would leave the job permanently red. Set "
            f"`lint_checks.ci.{lint_checks_ci.KEY}: {lint_checks_ci.EXAMPLE}` (a full commit sha of rig-cli "
            "that has `rig lint check`) in rig.yaml or the global config"
        )
        return
    plan.actions.append(
        Action(
            kind="install_lint_check_workflow", category="lint_checks", item="ci-workflow",
            source=catalog.source, target=config.repo_root,
            options={"rel_path": arts.WORKFLOW_REL, "rig_source": rig_source, "with_analyzer": with_analyzer},
        )
    )
