"""``provision_github_actions`` handler."""

from __future__ import annotations

import json

from ..github_actions import (
    build_permissions_body,
    build_workflow_permissions_body,
    normalize_permissions,
    normalize_workflow_permissions,
)
from ..plan import Action
from . import github_common
from .result import ActionResult


# ── rig-managed GitHub Actions permissions (§5 github.actions) ───────────────────────────
# rig reconciles the repo's Actions permissions on two endpoints: `PUT .../actions/permissions`
# (Actions enabled + allowed_actions) and `PUT .../actions/permissions/workflow` (the default
# GITHUB_TOKEN scope + whether workflows may approve PRs). Secure defaults: Actions enabled, but the
# token is READ-only and workflows may NOT approve PRs (least privilege). The DESIRED bodies live in
# `riglib/github_actions.py` (pure). Settings, not a ruleset — a mis-set value at worst restricts a
# workflow token, never locks a human out.
def github_actions_state(action: Action) -> tuple[str, dict]:
    """Classify the live-vs-desired Actions permissions — the one source apply + drift share.

    States: ``no_remote``, ``gh_error``, ``update`` (either endpoint differs), ``ok``. ``info``
    carries owner/repo and the two desired bodies (and on gh_error a detail). Both endpoints are
    read so a difference on EITHER reads as drift.
    """
    owner_repo = github_common.github_owner_repo(action.target)
    perms = build_permissions_body(action.options)
    wf = build_workflow_permissions_body(action.options)
    if owner_repo is None:
        return "no_remote", {"perms": perms, "wf": wf}
    owner, repo = owner_repo
    info: dict = {"owner": owner, "repo": repo, "perms": perms, "wf": wf}
    rc, out, err = github_common._gh_api([f"repos/{owner}/{repo}/actions/permissions"])
    if rc != 0:
        info["detail"] = err.strip() or out.strip() or f"gh api exited {rc}"
        return "gh_error", info
    try:
        live_perms = json.loads(out)
    except ValueError:
        info["detail"] = "gh api returned non-JSON actions permissions"
        return "gh_error", info
    # When the config DISABLES Actions, the apply handler deliberately skips the workflow-permissions
    # PUT (GitHub rejects it with Actions off). The classifier MUST mirror that: don't read or compare
    # the workflow-token endpoint either — else its (now-irrelevant) live value would read as
    # perpetual drift, making apply report `updated` every run and `rig status` show `modified`
    # forever, never converging. With Actions disabled, only the permissions endpoint is in scope.
    if not perms.get("enabled", True):
        in_sync = normalize_permissions(live_perms) == normalize_permissions(perms)
        return ("ok", info) if in_sync else ("update", info)
    rc, out, err = github_common._gh_api([f"repos/{owner}/{repo}/actions/permissions/workflow"])
    if rc != 0:
        info["detail"] = err.strip() or out.strip() or f"gh api exited {rc}"
        return "gh_error", info
    try:
        live_wf = json.loads(out)
    except ValueError:
        info["detail"] = "gh api returned non-JSON workflow permissions"
        return "gh_error", info
    in_sync = (
        normalize_permissions(live_perms) == normalize_permissions(perms)
        and normalize_workflow_permissions(live_wf) == normalize_workflow_permissions(wf)
    )
    return ("ok", info) if in_sync else ("update", info)


def _do_provision_github_actions(action: Action, on_conflict: str) -> ActionResult:
    """Provision the repo Actions permissions via ``gh api`` (two PUTs).

    No github remote → ``skipped``. The #4136.1 auth gate runs FIRST (before the live read), so an
    unauthenticated apply notifies + waits + resumes. ``RIG_GH_DRY_RUN`` computes the PUTs but skips
    both the gate and the mutation. A failure (no admin / API error) surfaces as a loud error.
    """
    _owner, _repo, early = github_common._github_action_preamble(action, "github-actions")
    if early is not None:
        return early
    state, info = github_actions_state(action)
    if state == "gh_error":
        return ActionResult(action, "error", f"github-actions: {info.get('detail', 'gh api failed')}")
    owner, repo, perms, wf = info["owner"], info["repo"], info["perms"], info["wf"]
    if state == "ok":
        return ActionResult(action, "skipped", f"github-actions: permissions already match on {owner}/{repo}")
    if github_common._gh_dry_run():
        return ActionResult(action, "updated", f"github-actions: RIG_GH_DRY_RUN — would UPDATE permissions on {owner}/{repo} (not put)")
    rc, out, err = github_common._gh_api(
        ["--method", "PUT", f"repos/{owner}/{repo}/actions/permissions", "--input", "-"],
        input_text=json.dumps(perms),
    )
    if rc != 0:
        return ActionResult(action, "error", f"github-actions: permissions update failed: {err.strip() or out.strip()}")
    # The default-workflow-token PUT only applies when Actions is ENABLED. When the config disables
    # Actions (`actions_enabled: false`), GitHub rejects a workflow-permissions PUT (there are no
    # workflows to scope), so issuing it would turn a legitimate "disable Actions" config into an
    # error. Skip it — disabling Actions is the whole change, and the token scope is moot.
    if not perms.get("enabled", True):
        return ActionResult(action, "updated", f"github-actions: disabled Actions on {owner}/{repo}")
    rc, out, err = github_common._gh_api(
        ["--method", "PUT", f"repos/{owner}/{repo}/actions/permissions/workflow", "--input", "-"],
        input_text=json.dumps(wf),
    )
    if rc != 0:
        return ActionResult(action, "error", f"github-actions: workflow permissions update failed: {err.strip() or out.strip()}")
    return ActionResult(action, "updated", f"github-actions: updated permissions on {owner}/{repo}")
