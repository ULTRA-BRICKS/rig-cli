"""Plan stage for the ``mode:`` block (autonomous / supervised) and the permission-rule resolution it feeds.

Split out of :mod:`riglib.plan`.
"""

from __future__ import annotations

from typing import Any
from .config import LoadedConfig
from .plan_model import Action
from .plan_model import InstallPlan


_AUTONOMOUS_DEFAULT_ALLOW_RULES = ("Bash(dev:*)", "Bash(review:*)", "Bash(task:*)")


def _autonomous_block(config: LoadedConfig) -> dict[str, Any] | None:
    mode = config.data.get("mode")
    if not isinstance(mode, dict) or mode.get("name") != "autonomous":
        return None
    auto = mode.get("autonomous", {})
    return auto if isinstance(auto, dict) else {}


def _subblock(parent: dict[str, Any], key: str) -> dict[str, Any]:
    child = parent.get(key, {})
    return child if isinstance(child, dict) else {}


def _build_mode(config: LoadedConfig, plan: InstallPlan) -> None:
    """Surface autonomous-mode policy in the plan.

    The runtime execution lives in the agent skills/harnesses; rig's job is to make the policy
    declared, validated, and visible in dry-run/status surfaces instead of burying it in prose.
    """
    auto = _autonomous_block(config)
    if auto is None:
        return
    plan.actions.append(
        Action(
            kind="record_mode",
            category="mode",
            item="autonomous",
            source=config.repo_root,
            target=config.repo_root,
            options={"name": "autonomous"},
        )
    )
    review_fix = _subblock(auto, "review_fix")
    if review_fix.get("enabled", True) is not False:
        until = str(review_fix.get("until", "clean"))
        max_iter = int(review_fix.get("max_iterations", 5))
        plan.notes.append(f"autonomous mode: review/fix until {until} (max {max_iter} iterations)")

    quorum = _subblock(_subblock(auto, "decisions"), "review_quorum")
    if quorum.get("enabled", True) is not False:
        min_iter = int(quorum.get("min_iterations", 2))
        min_models = int(quorum.get("min_models", 3))
        plan.notes.append(
            f"autonomous mode: decisions require review quorum ({min_iter} iterations across {min_models} models)"
        )

    escalation = _subblock(auto, "escalation")
    framework = str(escalation.get("framework_skill", "decision-request-discipline"))
    plan.notes.append(f"autonomous mode: escalation framework skill '{framework}'")

    comparison = _subblock(auto, "parallel_worktree_comparison")
    if comparison.get("enabled", True) is not False and escalation.get("require_parallel_worktree_comparison", True) is not False:
        candidates = int(comparison.get("candidates", 2))
        plan.notes.append(
            f"autonomous mode: parallel worktree comparison required before escalation ({candidates} candidates)"
        )
    plan.notes.append(
        "autonomous mode: do not interrupt in-flight work for unrelated requests; "
        "dispatch independent work to parallel subagents/worktrees and keep the current task alive"
    )

    parallel = _subblock(auto, "parallelism")
    if parallel.get("limit_aware", True) is not False:
        max_agents = int(parallel.get("max_agents", 4))
        max_worktrees = int(parallel.get("max_worktrees", 4))
        reserve = int(parallel.get("reserve_slots", 1))
        plan.notes.append(
            "autonomous mode: "
            f"limit-aware parallelism max_agents={max_agents} max_worktrees={max_worktrees} "
            f"reserve_slots={reserve}"
        )

    permissions = config.data.get("permissions", {})
    devtools = _subblock(auto, "development_tools")
    if isinstance(permissions, dict) and permissions.get("enabled") is False and devtools.get("allow", list(_AUTONOMOUS_DEFAULT_ALLOW_RULES)):
        plan.notes.append(
            "autonomous mode: development tool allow rules not provisioned because permissions.enabled=false"
        )


def _autonomous_allow_rules(config: LoadedConfig, kind: str, plan: InstallPlan) -> list[str]:
    auto = _autonomous_block(config)
    if auto is None:
        return []
    devtools = _subblock(auto, "development_tools")
    raw = devtools.get("allow", list(_AUTONOMOUS_DEFAULT_ALLOW_RULES))
    if not isinstance(raw, list) or not raw:
        return []
    from .permissions import CONFIG_RULE_DIALECT_KINDS

    # The autonomous dev-tool allow rules are claude-code dialect (Bash(...) specifiers), so only
    # kinds that consume that dialect verbatim get them. opencode HAS rule containers but a
    # different glob dialect — adding a claude-shaped rule as an opencode allow key would be a bogus
    # entry that never matches, so drop it with a note (same split as _resolve_permission_rules).
    if kind not in CONFIG_RULE_DIALECT_KINDS:
        plan.notes.append(
            f"autonomous mode: development tool allow rules dropped for harness '{kind}' "
            "(claude-code dialect rules; this harness has a different command-rule syntax)"
        )
        return []
    out: list[str] = []
    seen: set[str] = set()
    for entry in raw:
        s = str(entry)
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _resolve_permission_rules(
    p: dict[str, Any], kind: str, plan: InstallPlan
) -> tuple[list[str], list[str], list[str]]:
    """Resolve the raw allow entries + the deny/ask rule lists for the permissions action.

    ``allow`` is ADDITIVE raw entries on top of the tool-derived allowlist (this is how the
    hand-grown machine allowlist is adopted as declared config); ``deny``/``ask`` REPLACE the
    baked baseline (see :mod:`riglib.permissions` — an explicit ``[]`` disables it). All three
    config lists are RAW rule strings in claude-code's dialect, so only claude-code
    (``CONFIG_RULE_DIALECT_KINDS``) consumes them verbatim:

    - A kind with NO rule containers at all (codex — its execpolicy is a separate surface) gets
      nothing here, with a plan note when the config explicitly asked.
    - A kind WITH containers but a different dialect (opencode: ``permission.bash`` glob keys) gets
      its BAKED opencode-dialect deny/ask baseline (never the claude-shaped config override, which
      would be a bogus glob key that never matches); the raw config allow/deny/ask are dropped with
      a plan note so the drop is visible in ``rig plan``, never silent.
    """
    from .permissions import CONFIG_RULE_DIALECT_KINDS, HARNESS_RULE_CONTAINERS

    if kind not in HARNESS_RULE_CONTAINERS:
        _note_rules_dropped_no_containers(p, kind, plan)
        return [], [], []
    if kind not in CONFIG_RULE_DIALECT_KINDS:
        return _baked_dialect_rules(p, kind, plan)
    return _config_dialect_rules(p, kind)


def _dropped_rule_lists(p: dict[str, Any]) -> list[str]:
    """Names of the raw ``allow``/``deny``/``ask`` lists the config explicitly set."""
    return [k for k in ("allow", "deny", "ask") if isinstance(p.get(k), list)]


def _note_rules_dropped_no_containers(p: dict[str, Any], kind: str, plan: InstallPlan) -> None:
    from .permissions import HARNESS_RULES_NA

    dropped = _dropped_rule_lists(p)
    if dropped:
        reason = HARNESS_RULES_NA.get(kind, "no verified rule dialect")
        plan.notes.append(
            f"permissions: raw {'/'.join(dropped)} entries dropped — harness '{kind}' ({reason})"
        )


def _baked_dialect_rules(
    p: dict[str, Any], kind: str, plan: InstallPlan
) -> tuple[list[str], list[str], list[str]]:
    from .permissions import resolve_rules

    # opencode: containers exist, but the config lists are claude-dialect → drop with a note,
    # and use the BAKED opencode-dialect baseline (override=None) for deny/ask.
    dropped = _dropped_rule_lists(p)
    if dropped:
        plan.notes.append(
            f"permissions: raw {'/'.join(dropped)} entries dropped — harness '{kind}' uses its "
            "baked opencode-dialect deny/ask baseline (config lists are claude-code dialect)"
        )
    return [], resolve_rules(kind, "deny", None), resolve_rules(kind, "ask", None)


def _config_dialect_rules(
    p: dict[str, Any], kind: str
) -> tuple[list[str], list[str], list[str]]:
    from .permissions import resolve_rules

    allow_cfg = p.get("allow")
    allow_rules: list[str] = []
    seen: set[str] = set()
    for entry in (allow_cfg if isinstance(allow_cfg, list) else []):
        if str(entry) not in seen:
            seen.add(str(entry))
            allow_rules.append(str(entry))
    deny = resolve_rules(kind, "deny", list(p["deny"]) if isinstance(p.get("deny"), list) else None)
    ask = resolve_rules(kind, "ask", list(p["ask"]) if isinstance(p.get("ask"), list) else None)
    return allow_rules, deny, ask
