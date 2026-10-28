"""``provision_github_browser`` handler: settings only reachable through the web UI."""

from __future__ import annotations

import os
import shutil
import subprocess
import urllib.parse

from ..github_auth import ensure_browser_auth
from ..github_browser import (
    build_command_plan as build_browser_plan,
    desired_toggles as browser_desired_toggles,
)
from ..plan import Action
from . import github_common
from .result import ActionResult


# ── rig-managed GitHub settings via agent-browser (§5 — API-unreachable toggles) ─────────
# A first-class second backend invoked INSIDE apply for settings the REST API does NOT expose. The
# settings-page URL, the per-toggle accessibility selectors, and the agent-browser command PLAN are
# pure (`riglib/github_browser.py`); this handler ensures a logged-in browser via the #4136.1 gate
# (ensure_browser_auth) and replays the plan through a single `_agent_browser` seam. CAPABILITY
# DETECTION: agent-browser absent / not logged in / the toggle not on the page → a loud "could not
# drive the UI", never a silent green or a blind click.
def _agent_browser(args: list[str]) -> tuple[int, str, str]:
    """Run ``agent-browser <args>`` — the single seam every browser-backend call funnels through.

    Tests monkeypatch THIS so no test launches a browser. A missing binary → rc 127 with a clear
    message (the handler surfaces it as a degrade, never a crash).
    """
    if not shutil.which("agent-browser"):
        return 127, "", "agent-browser not found on PATH"
    try:
        res = subprocess.run(
            ["agent-browser", *args],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", f"agent-browser failed: {exc}"
    return res.returncode, res.stdout, res.stderr


def _do_provision_github_browser(action: Action, on_conflict: str) -> ActionResult:
    """Provision API-unreachable GitHub settings by driving the settings UI with agent-browser.

    No github remote → ``skipped``. ``RIG_GH_DRY_RUN`` builds + returns the command plan but runs
    nothing. Ensures a logged-in browser via the #4136.1 gate first; on any per-step failure the
    setting degrades loudly (the UI moved / the toggle is gone / not logged in) rather than a silent
    green. ``RIG_GH_BROWSER`` gates the whole backend OFF by default — driving a real browser is a
    heavier, slower path than gh api, so it only runs when explicitly enabled (so an ordinary
    `rig apply` never spawns a browser unexpectedly); the plan is still computed for status/tests.
    """
    owner_repo = github_common.github_owner_repo(action.target)
    desired = browser_desired_toggles(action.options)
    if owner_repo is None:
        return ActionResult(action, "skipped", "github-browser: no github origin remote — nothing to provision")
    owner, repo = owner_repo
    plan = build_browser_plan(owner, repo, desired)

    # The backend-enabled gate is checked BEFORE dry-run so a preview reflects what apply would
    # ACTUALLY do: with RIG_GH_BROWSER unset, a real apply is `skipped`, so the dry-run preview must
    # say "would be skipped" too — not "would drive N steps" (which an apply in the same env never
    # would). Only when the backend IS enabled does dry-run report the would-drive preview.
    browser_enabled = os.environ.get("RIG_GH_BROWSER", "").strip().lower() in ("1", "true", "yes")
    if not browser_enabled:
        verb = "RIG_GH_DRY_RUN — would be SKIPPED" if github_common._gh_dry_run() else "disabled"
        return ActionResult(action, "skipped",
                            f"github-browser: {verb} (set RIG_GH_BROWSER=1 to drive {len(plan)} UI step(s) on {owner}/{repo})")
    if github_common._gh_dry_run():
        return ActionResult(action, "updated",
                            f"github-browser: RIG_GH_DRY_RUN — would drive {len(plan)} UI step(s) on {owner}/{repo} (not run)")

    outcome = ensure_browser_auth(owner=owner, repo=repo)
    if not outcome.ok:
        return ActionResult(action, "error", f"github-browser: {outcome.detail}")

    # The #4136.1 notify-and-wait already happened above: `ensure_browser_auth` pinged the user and
    # blocked for the browser to become available. This is the SECOND, per-page check it cannot do —
    # whether the (now-present) browser session is actually LOGGED IN to github.com. The FIRST plan
    # step is `open <settings-url>`; after navigating, probe the resulting URL. If GitHub bounced us
    # to `/login` the session is logged out, so we degrade LOUDLY (a visible error telling the user
    # to log in and re-run) rather than blind-clicking the login page. (We don't re-enter the
    # notify/poll loop here — the gate already gave the user their chance; a still-logged-out session
    # at this point is an explicit, actionable failure, not another wait.)
    open_step, toggle_steps = plan[0], plan[1:]
    rc, out, err = _agent_browser(open_step)
    if rc != 0:
        return ActionResult(action, "error",
                            f"github-browser: could not open settings on {owner}/{repo}: {(err.strip() or out.strip())[:120]}")
    if _browser_on_login_page(owner, repo):
        return ActionResult(action, "error",
                            f"github-browser: the browser session is not logged into github.com — log in and re-run "
                            f"with RIG_GH_BROWSER=1 to provision the UI-only settings on {owner}/{repo}")

    for step in toggle_steps:
        rc, out, err = _agent_browser(step)
        if rc != 0:
            return ActionResult(action, "error",
                                f"github-browser: UI step {' '.join(step)!r} failed on {owner}/{repo}: {(err.strip() or out.strip())[:120]}")
    return ActionResult(action, "updated", f"github-browser: drove {len(toggle_steps)} UI toggle(s) on {owner}/{repo}")


def _browser_on_login_page(owner: str, repo: str) -> bool:
    """True iff the current browser URL is a github.com login/SSO page (i.e. NOT logged in).

    Reads `agent-browser get url`. GitHub redirects an unauthenticated request for a repo settings
    page to ``/login`` (or ``/sessions/...`` for SSO). A read that fails is treated as "assume logged
    in" (the subsequent toggle step will surface a real failure loudly) so a transient probe error
    doesn't block an otherwise-authenticated run. Seamed via `_agent_browser` so tests drive it.

    The login signal is parsed via urllib, NOT a bare ``"/login" in url`` substring, which would
    false-positive on a repo or OWNER literally named ``login`` (``github.com/login/<repo>/settings``
    or ``github.com/<owner>/login/settings``) and abort a perfectly authenticated run. Two guards:
      1. If the URL path is exactly our ``<owner>/<repo>`` page (the settings URL we navigated to),
         it is the logged-IN destination — never a login page, even if owner or repo IS "login".
      2. Otherwise, an auth bounce lands on a known sign-in path: ``/login`` or ``/sessions/...``
         (password sign-in) OR ``/orgs/<org>/sso`` (a SAML/SSO redirect for an org-protected repo).
         We match those path shapes; an unrecognized bounce still degrades later (the toggle step
         fails to find its control), just with a generic message instead of this specific one.
    """
    rc, out, _ = _agent_browser(["get", "url"])
    if rc != 0:
        return False
    parsed = urllib.parse.urlparse(out.strip())
    segments = [s.lower() for s in parsed.path.split("/") if s]
    # guard 1: our own settings page (…/<owner>/<repo>/…) is the logged-in destination
    if len(segments) >= 2 and segments[0] == owner.lower() and segments[1] == repo.lower():
        return False
    if not segments:
        return False
    # guard 2a: password sign-in (/login, /sessions/…)
    if segments[0] in ("login", "sessions"):
        return True
    # guard 2b: SAML/SSO redirect (/orgs/<org>/sso[...])
    return len(segments) >= 3 and segments[0] == "orgs" and segments[2].startswith("sso")
