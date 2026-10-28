"""Validator for the ``github`` config block (ruleset, merge, ghas, actions, browser).

Owns the bool-knob tables and the fail-closed shape checks for the GitHub repository settings rig
provisions. It is separate from the other block validators because the block has five nested
sub-blocks with a shared per-knob pattern, so the knob tables live next to the code that walks
them. The allowed-key sets themselves come from :mod:`riglib.config_schema` through
``reject_unknown_keys`` — the single source the schema registry also reads.
"""

from __future__ import annotations

from typing import Any

from .config_checks import check_bool, check_str, check_str_list, reject_unknown_keys
from .config_errors import ConfigError

# The bool knobs of each github sub-block — every one validated as a bool (fail-closed) so a typo'd
# value (`enabled: yes`, `squash_merge: 1`) is rejected with the schema path. Listed once here so
# the validator and the schema registry never disagree on the type of a knob.
GITHUB_MERGE_BOOL_KNOBS = (
    "enabled",
    "squash_merge",
    "merge_commit",
    "rebase_merge",
    "delete_branch_on_merge",
    "allow_auto_merge",
    "allow_update_branch",
)
GITHUB_GHAS_BOOL_KNOBS = (
    "enabled",
    "vulnerability_alerts",
    "automated_security_fixes",
    "secret_scanning",
    "secret_scanning_push_protection",
    "code_scanning_default_setup",
)
GITHUB_ACTIONS_BOOL_KNOBS = ("enabled", "actions_enabled", "can_approve_pull_request_reviews")
GITHUB_BROWSER_BOOL_KNOBS = ("enabled", "discussions", "projects")
# Enum knobs of github.actions → their allowed values (fail-closed on anything else).
GITHUB_ACTIONS_ENUMS = {
    "allowed_actions": ("all", "local_only", "selected"),
    "default_workflow_permissions": ("read", "write"),
}
GITHUB_RULESET_BOOL_KNOBS = (
    "enabled",
    "require_pull_request",
    "required_conversation_resolution",
    "dismiss_stale_reviews",
    "block_force_push",
    "restrict_deletion",
    "require_linear_history",
    "require_signatures",
    "admin_bypass",
    # Mode-gated (autonomous only) — see plan._build_github_ruleset. An explicit value here always
    # wins over the mode-gated default, regardless of mode. NOT in GITHUB_RULESET_DEFAULTS: unlike
    # every other bool knob, it has no unconditional default — its default only applies when the
    # repo's effective mode is autonomous.
    "require_extra_approval_for_unattributed_changes",
)
GITHUB_RULESET_KEYS = {
    *GITHUB_RULESET_BOOL_KNOBS,
    "name",
    "required_reviews",
    "required_status_checks",
}

# Each omitted merge-model knob falls back to its default (squash ON, the other two OFF).
_MERGE_MODEL_DEFAULTS = {"squash_merge": True, "merge_commit": False, "rebase_merge": False}


def _validate_ruleset(gh: dict[str, Any]) -> None:
    ruleset = gh.get("ruleset", {})
    if not isinstance(ruleset, dict):
        raise ConfigError("github.ruleset must be a mapping", schema_path="github.ruleset")
    reject_unknown_keys(ruleset, "github.ruleset")
    for knob in GITHUB_RULESET_BOOL_KNOBS:
        check_bool(ruleset, knob, f"github.ruleset.{knob}")
    check_str(ruleset, "name", "github.ruleset.name")
    reviews = ruleset.get("required_reviews")
    # NB: bool is an int subclass in Python — reject it explicitly so `true` can't masquerade
    # as a review count.
    if reviews is not None and (isinstance(reviews, bool) or not isinstance(reviews, int) or reviews < 0):
        raise ConfigError(
            f"github.ruleset.required_reviews must be an int >= 0, got {reviews!r}",
            schema_path="github.ruleset.required_reviews",
        )
    check_str_list(ruleset, "required_status_checks", "github.ruleset.required_status_checks")


def _merge_model_enabled(merge: dict[str, Any], knob: str, default: bool) -> bool:
    # An explicit `null` resolves to the DEFAULT too — matching the plan builder, which drops `null`
    # overrides (`v is not None`) so a `squash_merge: null` is squash-ON, not off. Resolving
    # null→default here keeps the validator from rejecting a config the plan would happily run.
    value = merge.get(knob, default)
    return default if value is None else bool(value)


def _check_a_merge_model_remains(merge: dict[str, Any]) -> None:
    # Skip the check on a DISABLED block: `_build_github_merge` doesn't provision it
    # (enabled:false → no PATCH), so rejecting an all-off-but-disabled block would forbid a
    # harmless combination the plan never acts on — keep validation aligned with the plan.
    if merge.get("enabled") is False:
        return
    # GitHub rejects (HTTP 422) a repo that allows NO merge model. Catch it at config time — a
    # `rig.yaml` that leaves squash, merge-commit, AND rebase all off would otherwise fail only on
    # the live PATCH. "All off" is reachable by setting squash_merge:false alone (merge_commit and
    # rebase_merge already default off).
    if all(not _merge_model_enabled(merge, knob, default) for knob, default in _MERGE_MODEL_DEFAULTS.items()):
        raise ConfigError(
            "github.merge disables every merge model (squash_merge, merge_commit, rebase_merge "
            "all false) — GitHub requires at least one; enable one (squash_merge is the default)",
            schema_path="github.merge",
        )


def _check_actions_enums(actions: dict[str, Any]) -> None:
    for knob, allowed in GITHUB_ACTIONS_ENUMS.items():
        value = actions.get(knob)
        if value is not None and value not in allowed:
            raise ConfigError(
                f"github.actions.{knob} must be one of {', '.join(allowed)}, got {value!r}",
                schema_path=f"github.actions.{knob}",
            )


def validate_github(gh: dict[str, Any]) -> None:
    """Validate the ``github`` block — the GitHub repository ruleset rig provisions.

    rig reconciles a branch ruleset (the modern replacement for branch protection) on the
    repo's default branch via ``gh api``, named by ``ruleset.name`` (rig owns rulesets with
    that name). Default **ON** when the repo has a github remote; a repo without one is a
    no-op (the action skips, never errors). Fail-closed, consistent with every other block,
    on: a non-mapping block, an unknown ``github`` / ``github.ruleset`` key (typo guard), a
    non-bool boolean knob, a ``required_reviews`` that is not an int >= 0, and a
    ``required_status_checks`` that is not a list of strings.

    The footgun guard is structural, not a config knob: rig NEVER emits the ``update``
    ("Restrict updates") rule (it locks out every merge to a protected default branch), and
    never emits a ``required_deployments`` rule with an empty environment list — so neither is
    expressible here at all.
    """
    if not isinstance(gh, dict):
        raise ConfigError("github must be a mapping", schema_path="github")
    if not gh:
        return
    reject_unknown_keys(gh, "github")
    _validate_ruleset(gh)
    # The other github sub-blocks (merge / ghas / actions / browser) — each a mapping, each with its
    # allowed keys gated by reject_unknown_keys (sourced from the schema registry), each bool knob
    # validated as a bool, and github.actions' two enum knobs pinned to their allowed values.
    merge = validate_github_subblock(gh, "merge", GITHUB_MERGE_BOOL_KNOBS)
    if merge is not None:
        _check_a_merge_model_remains(merge)
    validate_github_subblock(gh, "ghas", GITHUB_GHAS_BOOL_KNOBS)
    actions = validate_github_subblock(gh, "actions", GITHUB_ACTIONS_BOOL_KNOBS)
    if actions is not None:
        _check_actions_enums(actions)
    validate_github_subblock(gh, "browser", GITHUB_BROWSER_BOOL_KNOBS)


def validate_github_subblock(
    gh: dict[str, Any], name: str, bool_knobs: tuple[str, ...]
) -> dict[str, Any] | None:
    """Validate one ``github.<name>`` sub-block; return it (for further checks) or None if absent.

    Fail-closed, consistent with every other block: a non-mapping sub-block, an unknown key (typo
    guard, via the schema-sourced :func:`reject_unknown_keys`), or a non-bool bool knob each raise
    a 3-part ConfigError with the schema path. A sub-block ABSENT from the github block → returns
    None (the caller skips its extra cross-knob checks — nothing was configured, defaults apply); an
    empty sub-block (``merge: {}``) is present-but-default and returns ``{}``, so its cross-knob
    checks still run on the resolved defaults.
    """
    if name not in gh:
        return None  # absent → no sub-block to validate; caller's extra checks are skipped
    block = gh.get(name, {})
    if not isinstance(block, dict):
        raise ConfigError(f"github.{name} must be a mapping", schema_path=f"github.{name}")
    reject_unknown_keys(block, f"github.{name}")
    for knob in bool_knobs:
        check_bool(block, knob, f"github.{name}.{knob}")
    return block
