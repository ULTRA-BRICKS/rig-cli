"""Drift checks for the GitHub repo-settings actions (ruleset / merge / GHAS / Actions)."""

from __future__ import annotations

from .actions.github_actions_action import github_actions_state
from .actions.github_ghas_action import github_ghas_state
from .actions.github_merge_action import github_merge_state
from .actions.github_ruleset_action import github_ruleset_state
from .github_ruleset import DEFAULT_RULESET_NAME
from .plan import Action
from .drift_model import DriftItem, DriftReport


def _check_github_ruleset(action: Action, report: DriftReport) -> None:
    """Flag drift between the configured GitHub branch ruleset and the live repo.

    Switches on the SAME :func:`github_ruleset_state` apply uses (one classification, shared via
    the runner), so status and apply can never disagree on what "in sync" means:

    - ``create``   → ``missing``: no rig-managed ruleset on the repo (apply POSTs one).
    - ``update``   → ``modified``: a rig-managed ruleset exists but its rules/bypass/enforcement
                     differ from config (apply PUTs the desired body).
    - ``ok``       → no drift item (in sync).
    - ``no_remote``→ no drift item (a repo with no github origin has nothing to reconcile).
    - ``gh_error`` → a VISIBLE "could not verify" item (not silent in-sync): rig genuinely
                     couldn't reach the ruleset (gh missing / not authed / API error), so it must
                     NOT report the repo as in sync — that would mask a real missing/drifted
                     ruleset behind a green status. It is not a ``missing``/``modified`` (we
                     don't know the on-repo state), but it surfaces so the operator sees rig
                     couldn't check (and `rig apply` would error on the same state).
    """
    state, info = github_ruleset_state(action)
    desired = info.get("desired", {})
    name = desired.get("name", DEFAULT_RULESET_NAME)
    # owner/repo are guaranteed present for the create/update/gh_error states (no_remote never
    # reaches the branches below), so index directly — a future contract break surfaces as a
    # real KeyError instead of a misleading "None/None" in the message.
    if state == "create":
        report.items.append(
            DriftItem("missing", "github", "ruleset", action.target,
                      f"no rig-managed ruleset '{name}' on {info['owner']}/{info['repo']} (apply creates it)")
        )
    elif state == "update":
        report.items.append(
            DriftItem("modified", "github", "ruleset", action.target,
                      f"ruleset '{name}' on {info['owner']}/{info['repo']} differs from config (apply converges it)")
        )
    elif state == "gh_error":
        report.items.append(
            DriftItem("modified", "github", "ruleset", action.target,
                      f"could not verify ruleset '{name}' on {info.get('owner')}/{info.get('repo')} "
                      f"({info.get('detail', 'gh api failed')}) — status unknown, not confirmed in sync")
        )


def _check_github_merge(action: Action, report: DriftReport) -> None:
    """Flag drift between the configured GitHub merge-button policy and the live repo.

    Switches on the SAME :func:`github_merge_state` apply uses (one classification): ``update`` →
    ``modified`` (live differs, apply PATCHes); ``ok``/``no_remote`` → no item; ``gh_error`` → a
    VISIBLE "could not verify" item (never a silent in-sync — that would mask real drift behind a
    green status; gh missing / not authed / no admin / API error).
    """
    state, info = github_merge_state(action)
    if state == "update":
        report.items.append(
            DriftItem("modified", "github", "merge", action.target,
                      f"merge policy on {info['owner']}/{info['repo']} differs from config (apply converges it)")
        )
    elif state == "gh_error":
        report.items.append(
            DriftItem("modified", "github", "merge", action.target,
                      f"could not verify merge policy on {info.get('owner')}/{info.get('repo')} "
                      f"({info.get('detail', 'gh api failed')}) — status unknown, not confirmed in sync")
        )


def _check_github_ghas(action: Action, report: DriftReport) -> None:
    """Flag drift between the configured GHAS settings and the live repo.

    Switches on the SAME :func:`github_ghas_state` apply uses: ``update`` → ``modified``;
    ``ok``/``no_remote`` → no item; ``gh_error`` (the repo read itself failed) → a VISIBLE "could
    not verify" item. The signal covers the ``security_and_analysis`` block, the vuln-alerts /
    Dependabot sub-resources, and code-scanning. A scanner that couldn't be READ (plan-gated on a
    private repo) is reported in the same ``modified`` item (it forces ``update``) with an explicit
    "could not verify" suffix, so status is honest that one scanner's state is unknown — rather than
    masking it behind a green status (the old behavior turned this into a whole-block gh_error that
    also blocked applying the free features).
    """
    state, info = github_ghas_state(action)
    if state == "update":
        detail = f"security settings on {info['owner']}/{info['repo']} differ from config (apply converges them)"
        unverifiable = info.get("unverifiable") or []
        if unverifiable:
            detail += " — could not verify: " + "; ".join(unverifiable)
        report.items.append(DriftItem("modified", "github", "ghas", action.target, detail))
    elif state == "gh_error":
        report.items.append(
            DriftItem("modified", "github", "ghas", action.target,
                      f"could not verify security settings on {info.get('owner')}/{info.get('repo')} "
                      f"({info.get('detail', 'gh api failed')}) — status unknown, not confirmed in sync")
        )


def _check_github_actions(action: Action, report: DriftReport) -> None:
    """Flag drift between the configured GitHub Actions permissions and the live repo.

    Switches on the SAME :func:`github_actions_state` apply uses: ``update`` → ``modified``;
    ``ok``/``no_remote`` → no item; ``gh_error`` → a VISIBLE "could not verify" item.
    """
    state, info = github_actions_state(action)
    if state == "update":
        report.items.append(
            DriftItem("modified", "github", "actions", action.target,
                      f"Actions permissions on {info['owner']}/{info['repo']} differ from config (apply converges them)")
        )
    elif state == "gh_error":
        report.items.append(
            DriftItem("modified", "github", "actions", action.target,
                      f"could not verify Actions permissions on {info.get('owner')}/{info.get('repo')} "
                      f"({info.get('detail', 'gh api failed')}) — status unknown, not confirmed in sync")
        )
