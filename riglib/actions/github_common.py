"""GitHub API seam shared by the ruleset/merge/GHAS/actions/browser handlers."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from ..github_auth import ensure_gh_auth
from ..github_ruleset import parse_github_remote
from ..plan import Action
from .result import ActionResult


# ── GitHub repository ruleset (gh api) ─────────────────────────────────────────────
# rig reconciles a branch ruleset on the repo's DEFAULT branch — the modern replacement for
# branch protection — declaratively, the same way every other category is reconciled. The
# DESIRED body, the rule assembly, the normalized desired-vs-actual comparison, and the
# merge-lockout guard (rig never emits the `update` rule) all live in `riglib/github_ruleset.py`
# (pure, no Action dependency, so plan/state import them without a cycle); this module holds
# only the `gh` subprocess seams, the live-API classification, and the Action handler. apply and
# drift share `github_ruleset_state` + the pure builders, so they can never disagree on
# "in sync". All `gh` invocations go through `_gh_api` so tests can monkeypatch one seam; the
# RIG_GH_DRY_RUN env seam (mirrors RIG_SCHEDULE_DRY_RUN) computes what WOULD change without any
# POST/PUT, so CI and tests never mutate a real repo or hit the network.
def github_owner_repo(repo_root: Path) -> tuple[str, str] | None:
    """Resolve ``(owner, repo)`` from the repo's ``origin`` remote, or None if not on github.

    Shells out to ``git -C <repo_root> remote get-url origin`` and parses an SSH or HTTPS
    github.com URL via :func:`github_ruleset.parse_github_remote`. A repo with no remote, a
    non-github remote, or no git at all → None (the caller treats that as "nothing to
    provision", never an error). Isolated so tests can monkeypatch it without a real git remote.
    """
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    return parse_github_remote(res.stdout.strip())


def _gh_api(args: list[str], *, input_text: str | None = None) -> tuple[int, str, str]:
    """Run ``gh api <args>`` and return ``(returncode, stdout, stderr)``.

    The single seam every ruleset call funnels through — tests monkeypatch THIS, so no test
    ever spawns ``gh`` or touches the network. A missing ``gh`` binary returns a non-zero rc
    with a clear message (the caller surfaces it as a skipped/error result, never a crash).
    """
    if not shutil.which("gh"):
        return 127, "", "gh CLI not found on PATH"
    try:
        res = subprocess.run(
            ["gh", "api", *args],
            input=input_text,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", f"gh api failed: {exc}"
    return res.returncode, res.stdout, res.stderr


def _gh_dry_run() -> bool:
    """Honor RIG_GH_DRY_RUN — compute what WOULD change but make no POST/PUT to GitHub.

    Mirrors RIG_SCHEDULE_DRY_RUN: GET requests (read-only) still run so drift/apply can see
    the current rulesets, but the mutating create/update is skipped. CI and the test suite set
    this (or monkeypatch ``_gh_api`` wholesale) so a real repo is never mutated.
    """
    return os.environ.get("RIG_GH_DRY_RUN", "").strip().lower() in ("1", "true", "yes")


def _gh_list_rulesets(owner: str, repo: str) -> tuple[list | None, str]:
    """GET ALL the repo's OWN rulesets (paginated). Returns ``(list_or_None, error_detail)``.

    ``includes_parents=false`` excludes inherited org/enterprise rulesets, so the list holds
    only this repo's rulesets — `find_managed_ruleset` then never picks up a same-named parent.
    ``--paginate`` + ``per_page=100`` is LOAD-BEARING for idempotency: the endpoint returns only
    30 rulesets per page by default, so a repo with >30 rulesets where the managed one sits on a
    later page would otherwise look absent → apply would POST a DUPLICATE ``rig-managed`` ruleset
    on every run. With ``--paginate`` gh concatenates every page's JSON array into one.
    """
    rc, out, err = _gh_api(
        [f"repos/{owner}/{repo}/rulesets?includes_parents=false&per_page=100", "--paginate"]
    )
    if rc != 0:
        return None, (err.strip() or out.strip() or f"gh api exited {rc}")
    try:
        data = json.loads(out)
    except ValueError:
        return None, "gh api returned non-JSON ruleset list"
    return (data if isinstance(data, list) else []), ""


# ── the #4136.1 auth gate — shared by every github.* gh-api mutation ─────────────────────
# CTO #4136.1: rig must NOT silently fail when `gh` isn't authenticated for the admin scope it
# needs. Every github.* provisioner calls `_require_gh_auth` BEFORE its first mutation: if `gh` is
# already authed it returns None (proceed); otherwise it has ALREADY notified the user (via tg) and
# waited up to the RIG_GH_AUTH_WAIT budget, and returns a LOUD error ActionResult the handler
# returns as-is. Under RIG_GH_DRY_RUN the gate is skipped entirely (dry-run mutates nothing, so it
# needs no auth) — that keeps the test suite and CI from ever blocking on a login prompt.
def _require_gh_auth(action: Action, owner: str, repo: str, label: str) -> ActionResult | None:
    """Block on the #4136.1 auth gate; return None to proceed, or a loud error ActionResult.

    Skipped under RIG_GH_DRY_RUN (no mutation → no auth needed), so dry-run + tests never touch the
    gate's notify/poll path. On a non-ok outcome the user has already been pinged and rig has waited
    the configured budget; the returned ActionResult surfaces the exact `gh auth login` command so
    the failure is actionable, never a silent green.

    CALLED BEFORE THE FIRST READ. The #4136.1 contract is "not-authed → notify + WAIT → resume", and
    the very FIRST thing every github.* provisioner does is a live `gh api` read inside its
    `*_state` classifier — a read that itself FAILS without a token. So the gate must run BEFORE that
    read (not just before the mutation), otherwise an unauthenticated apply degrades to a plain
    `gh_error` and the notify-and-wait path is dead code on the exact case it exists for. Running it
    first means an unauthenticated `rig apply` pings the user and blocks for login, then the read +
    mutation proceed once authed.
    """
    if _gh_dry_run():
        return None
    outcome = ensure_gh_auth(owner=owner, repo=repo)
    if outcome.ok:
        return None
    return ActionResult(action, "error", f"{label}: {outcome.detail}")


def _github_action_preamble(
    action: Action, label: str
) -> tuple[str, str, ActionResult | None]:
    """Resolve ``(owner, repo)`` and clear the #4136.1 auth gate BEFORE any live read.

    Returns ``(owner, repo, early)``. When ``early`` is not None the caller returns it immediately:
      - ``skipped`` — the repo has no github origin remote (nothing to provision).
      - an auth ``error`` — gh is not authenticated and did not become so within the wait budget
        (the user was already pinged via tg). Under RIG_GH_DRY_RUN the gate is skipped, so dry-run
        never blocks and ``early`` is None whenever a remote exists.
    On success ``early`` is None and the caller proceeds to read live state (now that gh is authed)
    and mutate. Shared by every gh-api provisioner so the gate fires on the no-token case for all of
    them, not just the ones whose first call happened to be the mutation.
    """
    owner_repo = github_owner_repo(action.target)
    if owner_repo is None:
        return "", "", ActionResult(action, "skipped", f"{label}: no github origin remote — nothing to provision")
    owner, repo = owner_repo
    gate = _require_gh_auth(action, owner, repo, label)
    if gate is not None:
        return owner, repo, gate
    return owner, repo, None
