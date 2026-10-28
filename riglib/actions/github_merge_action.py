"""``provision_github_merge`` handler."""

from __future__ import annotations

import json

from ..github_merge import build_merge_body, normalize_merge
from ..plan import Action
from . import github_common
from .result import ActionResult


# ── rig-managed GitHub repo MERGE-button policy (§5 github.merge) ────────────────────
# rig reconciles the repo's merge-button policy — squash-only merge model, auto-delete head branch
# on merge, allow-auto-merge — via `PATCH /repos/{owner}/{repo}` on the same `gh api` backend as the
# ruleset. The DESIRED body, managed-field set, and normalized comparison live in
# `riglib/github_merge.py` (pure); this module holds only the live-API classification and the Action
# handler. apply and drift share `github_merge_state`. CAPABILITY DEGRADE: these are repo SETTINGS,
# not a ruleset, so a PATCH never locks anyone out — a no-admin token gets HTTP 403, surfaced as a
# visible error (apply) / "could not verify" (drift), never a silent green or crash.
def github_merge_state(action: Action) -> tuple[str, dict]:
    """Classify the live-vs-desired merge policy — the one source apply + drift share.

    States: ``no_remote`` (no github origin), ``gh_error`` (read failed: gh missing / not authed /
    no admin / API error), ``update`` (live differs → apply PATCHes), ``ok`` (already converged).
    There is no ``create`` — a github repo ALWAYS has merge settings, so the only outcomes are
    converge or already-converged. ``info`` carries owner/repo/desired and, on gh_error, a detail.
    """
    owner_repo = github_common.github_owner_repo(action.target)
    desired = build_merge_body(action.options)
    if owner_repo is None:
        return "no_remote", {"desired": desired}
    owner, repo = owner_repo
    info: dict = {"owner": owner, "repo": repo, "desired": desired}
    rc, out, err = github_common._gh_api([f"repos/{owner}/{repo}"])
    if rc != 0:
        info["detail"] = err.strip() or out.strip() or f"gh api exited {rc}"
        return "gh_error", info
    try:
        repo_obj = json.loads(out)
    except ValueError:
        info["detail"] = "gh api returned non-JSON repo object"
        return "gh_error", info
    if normalize_merge(repo_obj) == normalize_merge(desired):
        return "ok", info
    return "update", info


def _do_provision_github_merge(action: Action, on_conflict: str) -> ActionResult:
    """Provision (PATCH) the repo merge-button policy via ``gh api``.

    Shares :func:`github_merge_state` with drift. No github remote → ``skipped``. The #4136.1 auth
    gate runs FIRST (before the live read), so an unauthenticated apply notifies + waits + resumes
    rather than degrading to a plain read error. ``RIG_GH_DRY_RUN`` computes the PATCH but skips both
    the gate and the mutation. A read/PATCH failure (no admin / API error) surfaces as a loud
    ``error``.
    """
    _owner, _repo, early = github_common._github_action_preamble(action, "github-merge")
    if early is not None:
        return early
    state, info = github_merge_state(action)
    if state == "gh_error":
        return ActionResult(action, "error", f"github-merge: {info.get('detail', 'gh api failed')}")
    owner, repo, desired = info["owner"], info["repo"], info["desired"]
    if state == "ok":
        return ActionResult(action, "skipped", f"github-merge: policy already matches on {owner}/{repo}")
    if github_common._gh_dry_run():
        return ActionResult(action, "updated", f"github-merge: RIG_GH_DRY_RUN — would UPDATE merge policy on {owner}/{repo} (not patched)")
    rc, out, err = github_common._gh_api(
        ["--method", "PATCH", f"repos/{owner}/{repo}", "--input", "-"],
        input_text=json.dumps(desired),
    )
    if rc != 0:
        return ActionResult(action, "error", f"github-merge: update failed: {err.strip() or out.strip()}")
    return ActionResult(action, "updated", f"github-merge: updated merge policy on {owner}/{repo}")
