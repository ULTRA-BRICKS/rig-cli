"""The ``github`` block (ruleset, merge, GHAS, actions, browser) of the published schema registry.

Split out of ``config_schema`` to keep each module small; the registry order is assembled there.
"""

from __future__ import annotations

from .config_schema_model import Block, Leaf


_GITHUB_RULESET_BLOCK = Block(
    doc="a branch ruleset on the default branch (the modern branch-protection replacement).",
    leaves={
        "enabled": Leaf("boolean", "provision the ruleset", default=True),
        "name": Leaf("string", "the ruleset rig owns/reconciles", default="rig-managed"),
        "require_pull_request": Leaf("boolean", "require a PR to merge", default=True),
        "required_reviews": Leaf("integer", "required_approving_review_count", default=0, minimum=0),
        "required_conversation_resolution": Leaf(
            "boolean",
            "require every review thread resolved before merge (required_review_thread_resolution)",
            default=True,
        ),
        "dismiss_stale_reviews": Leaf(
            "boolean", "dismiss stale approvals on a new push (dismiss_stale_reviews_on_push)", default=True
        ),
        "block_force_push": Leaf("boolean", "emit the non_fast_forward rule", default=True),
        "restrict_deletion": Leaf("boolean", "emit the deletion rule", default=True),
        "require_linear_history": Leaf("boolean", "emit the required_linear_history rule", default=False),
        "require_signatures": Leaf("boolean", "emit the required_signatures rule", default=False),
        "required_status_checks": Leaf(
            "array", "contexts to require; empty emits no rule", items_type="string"
        ),
        "admin_bypass": Leaf("boolean", "add the repo Admin role to bypass_actors", default=True),
        # NO unconditional `default` here (unlike every other leaf above): this knob is MODE-GATED,
        # not unconditionally desired. rig defaults it to false only for a repo whose effective
        # `mode.name` is `autonomous` (plan._build_github_ruleset); a non-autonomous repo's current
        # GitHub-side value is left unmanaged. GitHub's `require_extra_approval_for_unattributed_
        # changes` requires one more approval than `required_reviews` on a PR GitHub classifies as
        # opened under an app identity not attributed to a person (GitHub's docs name the Copilot
        # coding agent as the documented trigger). It was found ON blocking `gh ship`'s merge on
        # agent-tools' ruleset and hand-patched off on 2026-09-07 — this makes that fix durable and
        # reconcilable (rig-cli#399 could not independently confirm the exact causal mechanism from
        # the rule-suites log; the observed block and hand-patch are the facts this is built on). An
        # explicit value here always overrides the mode-gated default, in either direction.
        "require_extra_approval_for_unattributed_changes": Leaf(
            "boolean",
            "pull_request rule sub-param: GitHub's own extra-approval-for-unattributed-changes "
            "rule (documented for PRs opened under an app identity, e.g. Copilot). Mode-gated to "
            "false for autonomous-mode repos; otherwise unmanaged (not compared for drift, and "
            "preserved verbatim if an unrelated ruleset knob triggers a PUT).",
        ),
    },
)

_GITHUB_MERGE_BLOCK = Block(
    doc="the repo merge-button policy (squash-only, auto-delete head branch, allow-auto-merge) via PATCH /repos.",
    leaves={
        "enabled": Leaf("boolean", "provision the merge policy", default=True),
        "squash_merge": Leaf("boolean", "allow_squash_merge (the only merge model by default)", default=True),
        "merge_commit": Leaf("boolean", "allow_merge_commit", default=False),
        "rebase_merge": Leaf("boolean", "allow_rebase_merge", default=False),
        "delete_branch_on_merge": Leaf("boolean", "auto-delete the head branch on merge", default=True),
        "allow_auto_merge": Leaf("boolean", "allow a PR to auto-merge when its gate is green", default=True),
        "allow_update_branch": Leaf("boolean", "offer the 'Update branch' button", default=True),
    },
)

_GITHUB_GHAS_BLOCK = Block(
    doc="GitHub Advanced Security: dependency graph + vuln-alerts + Dependabot + secret-scanning + CodeQL.",
    leaves={
        "enabled": Leaf("boolean", "provision GHAS settings", default=True),
        "vulnerability_alerts": Leaf("boolean", "the vulnerability-alerts sub-resource (Dependabot alerts)", default=True),
        "automated_security_fixes": Leaf("boolean", "Dependabot security updates (automated-security-fixes)", default=True),
        "secret_scanning": Leaf("boolean", "security_and_analysis.secret_scanning", default=True),
        "secret_scanning_push_protection": Leaf("boolean", "secret-scanning push protection", default=True),
        "code_scanning_default_setup": Leaf("boolean", "CodeQL default-setup (configured)", default=True),
    },
)

_GITHUB_ACTIONS_BLOCK = Block(
    doc="GitHub Actions permissions: enabled + allowed_actions + the default GITHUB_TOKEN scope.",
    leaves={
        "enabled": Leaf("boolean", "provision Actions permissions", default=True),
        "actions_enabled": Leaf("boolean", "whether Actions runs at all", default=True),
        "allowed_actions": Leaf("string", "which actions are allowed", enum=("all", "local_only", "selected"), default="all"),
        "default_workflow_permissions": Leaf("string", "default GITHUB_TOKEN scope", enum=("read", "write"), default="read"),
        "can_approve_pull_request_reviews": Leaf("boolean", "may a workflow approve/create PRs", default=False),
    },
)

_GITHUB_BROWSER_BLOCK = Block(
    doc="settings the REST API does NOT expose, driven via agent-browser (gated off at apply unless RIG_GH_BROWSER=1).",
    leaves={
        "enabled": Leaf("boolean", "plan the agent-browser backend (status lists it)", default=True),
        "discussions": Leaf("boolean", "the Discussions UI-only toggle", default=False),
        "projects": Leaf("boolean", "the Projects UI-only toggle", default=True),
    },
)

_GITHUB_BLOCK = Block(
    doc="GitHub repository settings rig reconciles via gh api + agent-browser.",
    nested={
        "ruleset": _GITHUB_RULESET_BLOCK,
        "merge": _GITHUB_MERGE_BLOCK,
        "ghas": _GITHUB_GHAS_BLOCK,
        "actions": _GITHUB_ACTIONS_BLOCK,
        "browser": _GITHUB_BROWSER_BLOCK,
    },
)
