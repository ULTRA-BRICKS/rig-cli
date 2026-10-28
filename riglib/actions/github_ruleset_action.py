"""``provision_github_ruleset`` handler."""

from __future__ import annotations

import json

from ..github_ruleset import (
    UNATTRIBUTED_APPROVAL_KEY,
    build_ruleset_body,
    carry_over_unattributed_approval,
    find_managed_ruleset,
    normalize_ruleset,
)
from ..plan import Action
from . import github_common
from .result import ActionResult


def github_ruleset_state(action: Action) -> tuple[str, dict]:
    """Classify the on-disk-vs-desired ruleset state — the one source apply + drift share.

    Returns ``(state, info)`` where ``state`` is one of:
      - ``no_remote``  — the repo has no github origin remote: nothing to provision.
      - ``gh_error``   — listing rulesets failed (gh missing / not authed / API error).
      - ``create``     — no rig-managed ruleset exists: apply POSTs one.
      - ``update``     — a rig-managed ruleset exists but its rules/bypass/enforcement differ.
      - ``ok``         — a rig-managed ruleset exists and matches the desired body.
    ``info`` carries ``owner``/``repo``/``desired`` and, when present, the existing ruleset's
    ``id`` and ``detail`` (the error string for ``gh_error``).
    """
    owner_repo = github_common.github_owner_repo(action.target)
    desired = build_ruleset_body(action.options)
    if owner_repo is None:
        return "no_remote", {"desired": desired}
    owner, repo = owner_repo
    info: dict = {"owner": owner, "repo": repo, "desired": desired}
    rulesets, err = github_common._gh_list_rulesets(owner, repo)
    if rulesets is None:
        info["detail"] = err
        return "gh_error", info
    existing = find_managed_ruleset(rulesets, desired["name"])
    if existing is None:
        return "create", info
    rs_id = existing.get("id")
    if rs_id is None:
        # a listed ruleset with no id can't be fetched/updated — don't GET .../rulesets/None.
        info["detail"] = f"managed ruleset '{desired['name']}' has no id in the API response"
        return "gh_error", info
    info["id"] = rs_id
    # the list endpoint omits rules/bypass_actors; fetch the full ruleset to compare.
    rc, out, gerr = github_common._gh_api([f"repos/{owner}/{repo}/rulesets/{rs_id}"])
    if rc != 0:
        info["detail"] = gerr.strip() or out.strip() or f"gh api exited {rc}"
        return "gh_error", info
    try:
        current = json.loads(out)
    except ValueError:
        info["detail"] = "gh api returned non-JSON ruleset"
        return "gh_error", info
    # Carried by _do_provision_github_ruleset into the PUT body when the unattributed-approval
    # knob is unmanaged (see carry_over_unattributed_approval) — an unrelated drift must not
    # silently reset a live GitHub-side value rig never declared an opinion on.
    info["current"] = current
    # Compare with the SAME manage_unattributed_approval flag on both sides (see
    # github_ruleset.normalize_ruleset's docstring) — it is present iff the plan builder
    # mode-gated `require_extra_approval_for_unattributed_changes` into `action.options`.
    manage_unattributed = UNATTRIBUTED_APPROVAL_KEY in action.options
    if normalize_ruleset(
        current, manage_unattributed_approval=manage_unattributed
    ) == normalize_ruleset(desired, manage_unattributed_approval=manage_unattributed):
        return "ok", info
    return "update", info


def _do_provision_github_ruleset(action: Action, on_conflict: str) -> ActionResult:
    """Provision (create/update) the rig-managed GitHub branch ruleset via ``gh api``.

    Shares :func:`github_ruleset_state` with drift, so status and apply read one
    classification. No github remote → ``skipped`` (never an error). The #4136.1 auth gate runs
    FIRST (before the live read), so an unauthenticated apply notifies + waits + resumes rather than
    degrading to a plain read error. ``RIG_GH_DRY_RUN`` computes the create/update but skips both the
    gate and the POST/PUT, returning what WOULD change. A read/write failure (no admin / API error)
    surfaces as an ``error`` result with the detail.
    """
    _owner, _repo, early = github_common._github_action_preamble(action, "github-ruleset")
    if early is not None:
        return early
    state, info = github_ruleset_state(action)
    if state == "gh_error":
        return ActionResult(action, "error", f"github-ruleset: {info.get('detail', 'gh api failed')}")

    owner, repo, desired = info["owner"], info["repo"], info["desired"]
    name = desired["name"]
    if state == "ok":
        return ActionResult(action, "skipped", f"github-ruleset: '{name}' already matches on {owner}/{repo}")

    body = json.dumps(desired)
    if state == "create":
        if github_common._gh_dry_run():
            return ActionResult(action, "created", f"github-ruleset: RIG_GH_DRY_RUN — would CREATE '{name}' on {owner}/{repo} (not posted)")
        rc, out, err = github_common._gh_api(
            ["--method", "POST", f"repos/{owner}/{repo}/rulesets", "--input", "-"],
            input_text=body,
        )
        if rc != 0:
            return _ruleset_write_error(action, "create", owner, repo, err, out)
        return ActionResult(action, "created", f"github-ruleset: created '{name}' on {owner}/{repo}")

    # state == "update"
    rs_id = info.get("id")
    # Preserve an UNMANAGED unattributed-approval value across this PUT (see
    # github_ruleset.carry_over_unattributed_approval's docstring) — a PUT replaces the whole
    # pull_request rule's parameters, so a live human choice on this security-relevant setting
    # must not be silently reset to GitHub's default just because some OTHER knob drifted.
    put_body = json.dumps(carry_over_unattributed_approval(desired, info.get("current")))
    if github_common._gh_dry_run():
        return ActionResult(action, "updated", f"github-ruleset: RIG_GH_DRY_RUN — would UPDATE '{name}' (id={rs_id}) on {owner}/{repo} (not put)")
    rc, out, err = github_common._gh_api(
        ["--method", "PUT", f"repos/{owner}/{repo}/rulesets/{rs_id}", "--input", "-"],
        input_text=put_body,
    )
    if rc != 0:
        return _ruleset_write_error(action, "update", owner, repo, err, out)
    return ActionResult(action, "updated", f"github-ruleset: updated '{name}' (id={rs_id}) on {owner}/{repo}")


def _looks_like_ruleset_plan_limited(detail: str) -> bool:
    """Heuristic: does this gh error mean the repo's PLAN can't enforce a ruleset (vs. a real bug)?

    A FREE private repo can't use branch protection / rulesets — the ``POST/PUT .../rulesets``
    call returns 403 with a body that names the plan limit ("Upgrade to GitHub Team", "upgrade
    your plan", "not available for private repositories", "make this repository public"). On THAT
    we want a specific, loud "ZERO server-side enforcement" message (it is a capability limit, not
    a rig bug). But a BARE 403/422 is NOT enough — that could equally be a no-admin token or a real
    payload bug we must surface as-is. So, like :func:`_looks_like_ghas_unlicensed`, match the
    PLAN/UPGRADE WORDING only; a transient/service or auth error stays a generic error.

    The phrase list is a white-list of GitHub's KNOWN plan-limit wording and must be kept current as
    GitHub rephrases (the same maintenance burden as :func:`_looks_like_ghas_unlicensed`). A MISS is
    SAFE: an unmatched plan-limit body falls through to a generic loud ``error`` (never a silent
    no-op), so a stale list degrades gracefully rather than masking the failure.
    """
    low = detail.lower()
    plan_phrases = (
        "upgrade to github",
        "upgrade your plan",
        "upgrade this repository",
        "not available for private",
        "make this repository public",
        "make the repository public",
        "rulesets are not available",
        "branch protection is not available",
        "not available on your plan",
    )
    return any(p in low for p in plan_phrases)


def _ruleset_write_error(
    action: Action, verb: str, owner: str, repo: str, err: str, out: str
) -> ActionResult:
    """Build the LOUD error result for a failed ruleset create/update — never a silent no-op.

    When the failure wording says the repo's plan can't enforce a ruleset (free private repo, 403),
    the message is SPECIFIC and actionable. The wording differs by verb:
    - ``create``: ZERO server-side enforcement is active (no ruleset was created at all).
    - ``update``: the existing ruleset was NOT reconciled (old config still active on GitHub).
    Any other failure surfaces the raw gh detail. Either way the result is an ``error`` rig status
    reports — never a swallowed success.
    """
    detail = err.strip() or out.strip() or "gh api reported a non-zero exit with no output"
    if _looks_like_ruleset_plan_limited(detail):
        if verb == "create":
            enforcement_note = (
                "ZERO server-side enforcement is active: a merge is gated ONLY by the "
                "client-side `gh ship` preflight, which can be bypassed (raw `gh pr merge`, "
                "the web UI, --skip-ci)."
            )
        else:
            enforcement_note = (
                "the existing ruleset on GitHub was NOT reconciled to the desired state "
                "(old config remains active — check `rig status` for the current drift)."
            )
        return ActionResult(
            action,
            "error",
            f"github-ruleset: branch protection unavailable on {owner}/{repo}'s plan — GitHub "
            f"rejected the ruleset {verb} (free private repos cannot enforce rulesets). "
            f"{enforcement_note} Fix: upgrade to GitHub Team/Pro, or make the repo public. "
            f"(gh said: {detail})",
        )
    return ActionResult(action, "error", f"github-ruleset: {verb} failed: {detail}")
