"""Plan stages for GitHub repository settings: ruleset, merge, GHAS, Actions and the browser-only toggles.

Split out of :mod:`riglib.plan`.
"""

from __future__ import annotations

from .catalog import Catalog
from .config import LoadedConfig
from .github_actions import GITHUB_ACTIONS_DEFAULTS
from .github_browser import UI_ONLY_TOGGLES
from .github_ghas import GITHUB_GHAS_DEFAULTS
from .github_merge import GITHUB_MERGE_DEFAULTS
from .github_ruleset import CI_GATE_CHECK_CONTEXTS
from .github_ruleset import GITHUB_RULESET_DEFAULTS
from .github_ruleset import UNATTRIBUTED_APPROVAL_KEY
from .plan_items import _item_enabled
from .plan_mode import _autonomous_block
from .plan_model import Action
from .plan_model import InstallPlan


def _enabled_ci_check_contexts(config: LoadedConfig, catalog: Catalog) -> list[str]:
    """The required-status-check contexts for the merge-gating CI gates this repo actually has.

    ROADMAP §5 names the PR-checklist and unresolved-review-threads gates as the required checks
    rig adds to the ruleset. But requiring a check whose workflow ISN'T in the repo wedges every PR
    (GitHub waits forever for a check-run that can never report — the lockout guard in
    ``github_ruleset.CI_GATE_CHECK_CONTEXTS``). So we require a context ONLY when its CI gate is
    enabled AND being written to ``.github/workflows`` (not ``export-only``, where nothing lands).
    The returned list is in ``CI_GATE_CHECK_CONTEXTS`` order so the plan is deterministic.
    """
    ci = config.category("ci")
    if ci.get("enabled") is False:
        return []
    # WHITELIST the one target GitHub actually runs workflows from: `.github/workflows`. GitHub only
    # executes workflow files in that exact directory — `export-only` (write nowhere) OR any other
    # custom target means the check-run never appears, so requiring its context would wedge every PR
    # (the lockout footgun). A blacklist of just `export-only` would miss every other non-standard
    # target; the whitelist is the safe form.
    target = ci.get("target", config.defaults.get("ci_target", ".github/workflows"))
    if target != ".github/workflows":
        return []
    known = catalog.names("ci")
    contexts: list[str] = []
    for slot, context in CI_GATE_CHECK_CONTEXTS.items():
        if slot not in known:
            continue  # the checkout doesn't carry this gate — can't be provisioned, so don't require
        item = catalog.get("ci", slot)
        # `type_enabled=False` is DELIBERATE: a gate becomes a required check only when the config
        # actually enables it (items.<>.enabled / enable: / all:), matching the CI builder's own
        # resolution exactly — so a gate that is WRITTEN as a workflow is the same set that is
        # REQUIRED, and the two can't drift. The scaffold (riglib/state.py) enables both merge gates
        # explicitly, so a fresh repo gets both required. (A bare project-type default does not flip a
        # CI gate on here — CI items are `default_enabled=False` in the catalog — so there is no
        # "type pulls in a gate but it isn't required" gap; the gate isn't pulled in at all without
        # explicit config.)
        if item is not None and _item_enabled(ci, item, type_enabled=False):
            contexts.append(context)
    return contexts


def _build_github_ruleset(config: LoadedConfig, catalog: Catalog, plan: InstallPlan) -> None:
    """Plan the GitHub repository branch-ruleset provisioning for the repo.

    Default **ON** (like ``agents_md``): rig reconciles a branch ruleset named
    ``github.ruleset.name`` (default ``rig-managed``) on the repo's DEFAULT branch via
    ``gh api``. Opt out with ``github: { ruleset: { enabled: false } }``. The whole
    GitHub-vs-desired classification lives in ``actions/`` (it depends on the live API), so the
    plan emits ONE idempotent action carrying the resolved knobs; no carrier in agent-tools,
    and the repo root is the target the action resolves ``owner/repo`` from.

    The action itself returns ``skipped`` when the repo has no github remote — so "default ON
    when the repo has a github remote" needs no detection here; a non-github repo is a no-op.
    The resolved options merge the documented defaults with any ``ruleset`` overrides, so a
    sparse config still produces the safe default ruleset and the footgun ``update`` rule is
    never reachable.

    REQUIRED STATUS CHECKS (ROADMAP §5). When the config does NOT pin ``required_status_checks``,
    rig defaults them to the merge-gating CI gates this repo actually provisions (PR Checklist +
    review-threads, via :func:`_enabled_ci_check_contexts`) — so a PR can't merge until those gates
    are green, WITHOUT requiring a check whose workflow isn't present (which would wedge every PR).
    An explicit ``required_status_checks`` in the config wins verbatim (including ``[]`` to require
    none), so the auto-default never overrides a deliberate choice.

    UNATTRIBUTED-APPROVAL KNOB, MODE-GATED. GitHub's
    ``require_extra_approval_for_unattributed_changes`` requires one more approval than
    ``required_approving_review_count`` on a PR GitHub classifies as opened under an app identity
    not attributed to a person — GitHub's own docs name the Copilot coding agent as the documented
    trigger ("When Copilot opens a pull request that isn't attributed to a person, the ruleset
    requires one more approval than the number you configured"). Found ON (``true``) blocking
    ``gh ship``'s merge on agent-tools' ``rig-managed`` ruleset and hand-patched to ``false`` via a
    raw ``gh api ... -X PUT`` on 2026-09-07 as an emergency stopgap (rig-cli#399's investigation
    could not independently confirm from the rule-suites log or PR authorship why it fired there —
    agent-tools' recent merges are all attributed to a human login, which the documented mechanism
    would not gate on; the exact causal story is unresolved, only the observed block + the hand-
    patch are facts). This makes the fix durable regardless of the precise mechanism. rig defaults
    it to ``False`` (never required), but ONLY for a repo whose EFFECTIVE ``mode.name`` is ``autonomous``
    (:func:`_autonomous_block`, cascaded from the machine-wide ``~/.config/rig/config.yaml`` —
    ``mode`` is a GLOBAL-only category, config.py rejects it from a committed repo ``rig.yaml``
    outright — so THIS GATING IS PER-MACHINE, not per-repo: every repo `rig apply` touches from a
    machine with ``mode.name: autonomous`` gets the knob managed). A repo NOT running in autonomous
    mode gets no injected default: the knob is simply absent from ``options``, so it is never sent
    in the DESIRED body and never compared for drift — it is UNMANAGED. An unrelated knob change
    still PUTs the full ``pull_request`` rule (a full-replace update), which would otherwise reset
    an unmanaged knob to GitHub's own platform default — so ``actions/runner.py``'s
    ``_do_provision_github_ruleset`` carries the live-fetched value into the outgoing PUT
    unchanged (``github_ruleset.carry_over_unattributed_approval``), NOT a full optimistic-
    concurrency guarantee: a hand edit landing in the narrow window between that read and the PUT
    can still be overwritten — the same read-then-write risk this module already carries for every
    field it reconciles. An explicit
    ``github.ruleset.require_extra_approval_for_unattributed_changes`` in the repo's own config
    ALWAYS wins over this mode-gated default, in either direction and regardless of mode — this is
    the way to opt a single repo back in even on an autonomous-mode machine.
    """
    gh = config.data.get("github")
    if gh is None:
        gh = {}
    if not isinstance(gh, dict):
        return  # validate() already fail-closed on a non-mapping block
    ruleset = gh.get("ruleset", {})
    if not isinstance(ruleset, dict):
        return
    if ruleset.get("enabled") is False:
        return
    # `enabled` is a plan-gating meta-key, not a ruleset-body knob — strip it before merging the
    # overrides onto the canonical defaults so it never leaks into the action options. Also drop
    # explicit `null` values: a `required_reviews: null` (which validate() tolerates) would
    # otherwise overlay the `0` default with None and crash `int(None)` in build_ruleset_rules —
    # and a `null` bool knob would silently disable a guard instead of using its default. A
    # missing key already falls back to the default; an explicit null must do the same.
    overrides = {k: v for k, v in ruleset.items() if k != "enabled" and v is not None}
    options = {**GITHUB_RULESET_DEFAULTS, **overrides}
    # Default the required checks to the repo's actual merge-gating CI gates when not pinned. The
    # presence test is on the ORIGINAL ruleset mapping (an explicit `[]` is a deliberate "require
    # none" and must survive the `v is not None` filter above — which it does, so check `ruleset`).
    if "required_status_checks" not in ruleset or ruleset.get("required_status_checks") is None:
        options["required_status_checks"] = _enabled_ci_check_contexts(config, catalog)
    # Mode-gated default only — never overrides an explicit config value (setdefault, and the
    # config-driven `overrides` merge above already ran).
    if _autonomous_block(config) is not None:
        options.setdefault(UNATTRIBUTED_APPROVAL_KEY, False)
    plan.actions.append(
        Action(
            kind="provision_github_ruleset",
            category="github",
            item="ruleset",
            source=config.repo_root,
            target=config.repo_root,
            options=options,
        )
    )


def _github_subblock(config: LoadedConfig, name: str) -> dict | None:
    """Resolve the ``github.<name>`` sub-block for a plan builder, or None to SKIP it.

    Returns None when the github block is absent/non-mapping, the sub-block is non-mapping, or the
    sub-block is explicitly disabled (``enabled: false``) — the four callers each then return early.
    Otherwise returns the sub-block dict (possibly empty → all defaults). One helper so every
    github plan builder reads the block the same way, with the same default-ON + opt-out semantics
    as ``_build_github_ruleset``.
    """
    gh = config.data.get("github")
    if gh is None:
        gh = {}
    if not isinstance(gh, dict):
        return None
    block = gh.get(name, {})
    if not isinstance(block, dict):
        return None
    if block.get("enabled") is False:
        return None
    return block


def _github_options(block: dict, defaults: dict) -> dict:
    """Merge a sub-block's overrides onto ``defaults``, dropping ``enabled`` and explicit nulls.

    ``enabled`` is a plan-gating meta-key (handled by :func:`_github_subblock`), and an explicit
    ``null`` must fall back to the default (never overlay it with None — which would disable a
    secure-default guard or crash a coercion). Mirrors the ruleset builder's override discipline.
    """
    overrides = {k: v for k, v in block.items() if k != "enabled" and v is not None}
    return {**defaults, **overrides}


def _build_github_merge(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan the GitHub repo merge-button-policy provisioning (``github.merge``).

    Default **ON** (like ``github.ruleset``): rig reconciles the squash-only merge model +
    auto-delete-head-branch + allow-auto-merge via ``PATCH /repos/{o}/{r}``. Opt out with
    ``github: { merge: { enabled: false } }``. The classification lives in ``actions/`` (it depends
    on the live API); the plan emits ONE idempotent action carrying the resolved knobs. The action
    returns ``skipped`` on a repo with no github remote, so default-ON needs no detection here.
    """
    block = _github_subblock(config, "merge")
    if block is None:
        return
    plan.actions.append(
        Action(
            kind="provision_github_merge",
            category="github",
            item="merge",
            source=config.repo_root,
            target=config.repo_root,
            options=_github_options(block, GITHUB_MERGE_DEFAULTS),
        )
    )


def _build_github_ghas(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan the GitHub Advanced Security provisioning (``github.ghas``).

    Default **ON**: rig reconciles dependency graph + vuln-alerts + Dependabot security updates +
    secret-scanning (+ push protection) + CodeQL default-setup, each via the right ``gh api``
    endpoint. Opt out with ``github: { ghas: { enabled: false } }``. The action degrades loudly on a
    repo whose plan does not include a GHAS-licensed scanner (private repo without GHAS), and is a
    no-op on a repo with no github remote.
    """
    block = _github_subblock(config, "ghas")
    if block is None:
        return
    plan.actions.append(
        Action(
            kind="provision_github_ghas",
            category="github",
            item="ghas",
            source=config.repo_root,
            target=config.repo_root,
            options=_github_options(block, GITHUB_GHAS_DEFAULTS),
        )
    )


def _build_github_actions(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan the GitHub Actions permissions provisioning (``github.actions``).

    Default **ON**: rig reconciles Actions-enabled + allowed_actions and the default GITHUB_TOKEN
    scope (READ-only by default, least privilege) + whether workflows may approve PRs. Opt out with
    ``github: { actions: { enabled: false } }``. A no-op on a repo with no github remote.
    """
    block = _github_subblock(config, "actions")
    if block is None:
        return
    plan.actions.append(
        Action(
            kind="provision_github_actions",
            category="github",
            item="actions",
            source=config.repo_root,
            target=config.repo_root,
            options=_github_options(block, GITHUB_ACTIONS_DEFAULTS),
        )
    )


def _build_github_browser(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan the API-unreachable GitHub settings provisioning via agent-browser (``github.browser``).

    Default **ON** in the plan (so ``rig status`` shows it), but the action itself is gated OFF at
    apply time unless ``RIG_GH_BROWSER=1`` — driving a real browser is a heavier path than gh api,
    so it runs only when explicitly enabled. Opt out of even planning it with
    ``github: { browser: { enabled: false } }``. The toggle defaults come from the browser backend's
    ``UI_ONLY_TOGGLES`` table (one source).
    """
    block = _github_subblock(config, "browser")
    if block is None:
        return
    defaults = {knob: spec["default"] for knob, spec in UI_ONLY_TOGGLES.items()}
    plan.actions.append(
        Action(
            kind="provision_github_browser",
            category="github",
            item="browser",
            source=config.repo_root,
            target=config.repo_root,
            options=_github_options(block, defaults),
        )
    )
