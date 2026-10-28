"""github.browser backend, the auth gate, ruleset checks and config validation (split from test_github_settings).
"""

from __future__ import annotations
from tests.github_settings_helpers import (_authed, _browser_action, _cfg, _merge_action, gh_repo)



import pytest

from riglib.actions import runner
from riglib.actions import github_browser_action as github_browser_action_mod
from riglib.config import ConfigError, validate
from riglib import github_auth
from riglib.github_browser import (
    build_command_plan,
    desired_toggles,
    settings_url,
)
from riglib.plan import build
from riglib.actions import github_common as github_common_mod



# ════════════════════════════════════════════════════════════════════════════════════
# github.browser — agent-browser backend for API-unreachable settings
# ════════════════════════════════════════════════════════════════════════════════════
def test_browser_command_plan_is_deterministic():
    desired = desired_toggles({"discussions": True, "projects": False})
    plan = build_command_plan("acme", "widget", desired)
    assert plan[0] == ["open", settings_url("acme", "widget")]
    # one find-role step per managed toggle; the agent-browser contract is
    # `find role <role> <action> --name <label>` (name is an OPTION, never a positional).
    assert ["find", "role", "switch", "check", "--name", "Discussions"] in plan
    assert ["find", "role", "switch", "uncheck", "--name", "Projects"] in plan


def test_browser_gated_off_by_default(monkeypatch, gh_repo, tmp_path):
    monkeypatch.delenv("RIG_GH_BROWSER", raising=False)
    monkeypatch.delenv("RIG_GH_DRY_RUN", raising=False)
    called: list = []
    monkeypatch.setattr(github_browser_action_mod, "_agent_browser", lambda args: called.append(args) or (0, "", ""))
    res = runner._do_provision_github_browser(_browser_action(tmp_path), "backup")
    assert res.status == "skipped" and "RIG_GH_BROWSER" in res.detail
    assert not called  # no browser spawned


def test_browser_drives_ui_when_enabled(monkeypatch, gh_repo, tmp_path):
    monkeypatch.setenv("RIG_GH_BROWSER", "1")
    monkeypatch.delenv("RIG_GH_DRY_RUN", raising=False)
    steps: list = []
    monkeypatch.setattr(github_browser_action_mod, "_agent_browser", lambda args: steps.append(args) or (0, "", ""))
    res = runner._do_provision_github_browser(_browser_action(tmp_path), "backup")
    assert res.status == "updated"
    assert steps and steps[0][0] == "open"


def test_browser_step_failure_degrades_loud(monkeypatch, gh_repo, tmp_path):
    monkeypatch.setenv("RIG_GH_BROWSER", "1")
    monkeypatch.delenv("RIG_GH_DRY_RUN", raising=False)

    # `open` and the login probe (`get url`) succeed so the handler reaches the TOGGLE steps; a
    # toggle (`find role switch …`) then fails — the per-step degrade path this test is about. (A
    # blanket-fail stub would trip the earlier `open` failure instead, never exercising it.)
    def _toggle_fails(args):
        if args and args[0] in ("open", "get"):
            return 0, "https://github.com/acme/widget/settings", ""
        return 1, "", "could not find element"

    monkeypatch.setattr(github_browser_action_mod, "_agent_browser", _toggle_fails)
    res = runner._do_provision_github_browser(_browser_action(tmp_path), "backup")
    assert res.status == "error" and "failed" in res.detail


def test_browser_handler_errors_when_browser_auth_unavailable(monkeypatch, gh_repo, tmp_path):
    """Handler→gate integration (round-5 test gap): when ensure_browser_auth is NOT ok (agent-browser
    absent), the handler returns a loud error and never drives the UI."""
    monkeypatch.setenv("RIG_GH_BROWSER", "1")
    monkeypatch.delenv("RIG_GH_DRY_RUN", raising=False)
    monkeypatch.setattr(github_browser_action_mod, "ensure_browser_auth",
                        lambda **kw: github_auth.AuthOutcome("unavailable", detail="agent-browser not available"))
    called: list = []
    monkeypatch.setattr(github_browser_action_mod, "_agent_browser", lambda args: called.append(args) or (0, "", ""))
    res = runner._do_provision_github_browser(_browser_action(tmp_path), "backup")
    assert res.status == "error" and "agent-browser" in res.detail and not called


def test_browser_login_probe_recognizes_sso_redirect(monkeypatch):
    """guard 2b: a SAML/SSO bounce (/orgs/<org>/sso) reads as not-logged-in (round-5 #2)."""
    monkeypatch.setattr(github_browser_action_mod, "_agent_browser", lambda args: (0, "https://github.com/orgs/acme/sso?return_to=x", ""))
    assert github_browser_action_mod._browser_on_login_page("acme", "widget") is True
    # a normal org page that is NOT an sso path stays logged-in
    monkeypatch.setattr(github_browser_action_mod, "_agent_browser", lambda args: (0, "https://github.com/orgs/acme/people", ""))
    assert github_browser_action_mod._browser_on_login_page("acme", "widget") is False


def test_browser_login_probe_uses_path_segment_not_substring(monkeypatch):
    """Review-finding (round 2) #2: the login check must key on the first PATH SEGMENT, so a repo or
    owner literally named `login` (URL `.../login/...`) does NOT false-positive as 'not logged in'."""
    # logged out → GitHub redirected to the /login page (first segment is `login`)
    monkeypatch.setattr(github_browser_action_mod, "_agent_browser", lambda args: (0, "https://github.com/login?return_to=x", ""))
    assert github_browser_action_mod._browser_on_login_page("acme", "widget") is True
    # a repo named `login` on a settings page — `login` appears in the path but NOT as the first
    # segment, so this is a logged-IN settings page and must NOT be flagged.
    monkeypatch.setattr(github_browser_action_mod, "_agent_browser", lambda args: (0, "https://github.com/acme/login/settings", ""))
    assert github_browser_action_mod._browser_on_login_page("acme", "login") is False
    # an owner named `login`
    monkeypatch.setattr(github_browser_action_mod, "_agent_browser", lambda args: (0, "https://github.com/login/widget/settings", ""))
    assert github_browser_action_mod._browser_on_login_page("login", "widget") is False


def test_browser_logged_out_session_degrades_loud(monkeypatch, gh_repo, tmp_path):
    """A present-but-logged-OUT browser session (open succeeds, but URL bounced to /login) degrades
    loudly with an actionable message — never a blind click on the login page."""
    monkeypatch.setenv("RIG_GH_BROWSER", "1")
    monkeypatch.delenv("RIG_GH_DRY_RUN", raising=False)
    toggles: list = []

    def _logged_out(args):
        if args[:2] == ["get", "url"]:
            return 0, "https://github.com/login", ""  # session not logged in
        if args and args[0] == "open":
            return 0, "", ""
        toggles.append(args)
        return 0, "", ""

    monkeypatch.setattr(github_browser_action_mod, "_agent_browser", _logged_out)
    res = runner._do_provision_github_browser(_browser_action(tmp_path), "backup")
    assert res.status == "error" and "not logged into github.com" in res.detail
    assert not toggles  # no toggle was clicked on the login page


def test_browser_dry_run_enabled_previews_steps_runs_nothing(monkeypatch, gh_repo, tmp_path):
    # backend ENABLED → dry-run previews the would-drive steps but spawns nothing.
    monkeypatch.setenv("RIG_GH_DRY_RUN", "1")
    monkeypatch.setenv("RIG_GH_BROWSER", "1")
    called: list = []
    monkeypatch.setattr(github_browser_action_mod, "_agent_browser", lambda args: called.append(args) or (0, "", ""))
    res = runner._do_provision_github_browser(_browser_action(tmp_path), "backup")
    assert res.status == "updated" and "RIG_GH_DRY_RUN" in res.detail and "would drive" in res.detail and not called


def test_browser_dry_run_disabled_previews_skip_not_drive(monkeypatch, gh_repo, tmp_path):
    """Review-finding (round 3) #4: with the backend OFF (no RIG_GH_BROWSER), a real apply is
    skipped — so the dry-run preview must say 'would be SKIPPED', not 'would drive N steps' (a
    preview an apply in the same env would never honor)."""
    monkeypatch.setenv("RIG_GH_DRY_RUN", "1")
    monkeypatch.delenv("RIG_GH_BROWSER", raising=False)
    called: list = []
    monkeypatch.setattr(github_browser_action_mod, "_agent_browser", lambda args: called.append(args) or (0, "", ""))
    res = runner._do_provision_github_browser(_browser_action(tmp_path), "backup")
    assert res.status == "skipped" and "would be SKIPPED" in res.detail and not called


# ════════════════════════════════════════════════════════════════════════════════════
# the #4136.1 auth gate — ASK and WAIT, never silently fail
# ════════════════════════════════════════════════════════════════════════════════════
def test_auth_ok_immediately_when_authed(monkeypatch):
    monkeypatch.setattr(github_auth, "_gh_auth_ok", lambda: True)
    notified: list = []
    monkeypatch.setattr(github_auth, "_notify", lambda msg: notified.append(msg) or True)
    out = github_auth.ensure_gh_auth(owner="acme", repo="widget")
    assert out.ok and not notified  # no notify when already authed


def test_auth_missing_degrades_loud_without_phone_spam_at_zero_budget(monkeypatch):
    # the autonomous default (0 budget) must NOT hang AND must NOT ping the phone: there's no wait to
    # notify about, and an apply runs the gate per-action (~5×) — "loud" is the visible error result,
    # not up to 5 tg pushes. (Review round-5 #1.) So: no notify, no sleep, a loud actionable detail.
    monkeypatch.setattr(github_auth, "_gh_auth_ok", lambda: False)
    monkeypatch.setattr(github_auth.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setenv("RIG_GH_AUTH_WAIT", "0")
    notified: list = []
    monkeypatch.setattr(github_auth, "_notify", lambda msg: notified.append(msg) or True)
    slept: list = []
    monkeypatch.setattr(github_auth, "_sleep", lambda s: slept.append(s))
    out = github_auth.ensure_gh_auth(owner="acme", repo="widget")
    assert out.state == "timed_out" and not notified and not slept
    assert "gh auth login" in out.detail  # actionable: the exact command, in the RESULT (the loud signal)


def test_auth_waits_then_resumes_when_login_appears(monkeypatch):
    # with a budget, the gate polls and returns ok the moment auth appears (the "WAIT then resume").
    states = iter([False, False, True])
    monkeypatch.setattr(github_auth, "_gh_auth_ok", lambda: next(states))
    monkeypatch.setattr(github_auth.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(github_auth, "_notify", lambda msg: True)
    monkeypatch.setenv("RIG_GH_AUTH_WAIT", "100")
    monkeypatch.setenv("RIG_GH_AUTH_POLL", "1")
    # a clock that advances 1s per call but never hits the 100s deadline before auth appears.
    t = iter(range(0, 1000))
    monkeypatch.setattr(github_auth, "_now", lambda: float(next(t)))
    monkeypatch.setattr(github_auth, "_sleep", lambda s: None)
    out = github_auth.ensure_gh_auth(owner="acme", repo="widget")
    assert out.ok and out.notified


def test_auth_unavailable_when_gh_missing(monkeypatch):
    monkeypatch.setattr(github_auth, "_gh_auth_ok", lambda: False)
    monkeypatch.setattr(github_auth.shutil, "which", lambda name: None)
    out = github_auth.ensure_gh_auth(owner="acme", repo="widget")
    assert out.state == "unavailable" and "gh CLI not found" in out.detail


def test_auth_gate_dedups_notify_and_wait_across_actions_in_one_run(monkeypatch):
    """Review-finding (round 8) #2: within ONE apply, the gate runs per github.* action. If login
    never appears, only the FIRST action should notify + wait the budget; siblings short-circuit to
    an immediate timed_out (no extra tg pushes, no extra budget×N blocking)."""
    github_auth.reset_auth_gate()
    monkeypatch.setattr(github_auth, "_gh_auth_ok", lambda: False)
    monkeypatch.setattr(github_auth.shutil, "which", lambda name: "/usr/bin/gh")
    notifies: list = []
    monkeypatch.setattr(github_auth, "_notify", lambda msg: notifies.append(msg) or True)
    sleeps: list = []
    monkeypatch.setattr(github_auth, "_sleep", lambda s: sleeps.append(s))
    monkeypatch.setenv("RIG_GH_AUTH_WAIT", "3")
    monkeypatch.setenv("RIG_GH_AUTH_POLL", "1")
    t = iter(range(0, 1000))
    monkeypatch.setattr(github_auth, "_now", lambda: float(next(t)))

    first = github_auth.ensure_gh_auth(owner="acme", repo="widget")
    sleeps_after_first = len(sleeps)
    second = github_auth.ensure_gh_auth(owner="acme", repo="widget")
    third = github_auth.ensure_gh_auth(owner="acme", repo="widget")
    assert first.state == second.state == third.state == "timed_out"
    assert len(notifies) == 1  # only the FIRST action pinged the user
    assert len(sleeps) == sleeps_after_first  # the 2nd/3rd actions did NOT re-wait (no extra sleeps)


def test_browser_auth_ok_when_present(monkeypatch):
    monkeypatch.setattr(github_auth, "_browser_auth_ok", lambda: True)
    assert github_auth.ensure_browser_auth(owner="acme", repo="widget").ok


def test_browser_auth_notifies_and_times_out_when_missing(monkeypatch):
    """ensure_browser_auth mirrors the gh gate: absent + a WAIT budget → notify once, then degrade
    loud when it doesn't appear. (Notify is reserved for the positive-budget wait path — at 0 budget
    it degrades silently to the result, per round-5 #1.)"""
    notified: list = []
    monkeypatch.setattr(github_auth, "_browser_auth_ok", lambda: False)
    monkeypatch.setattr(github_auth, "_notify", lambda msg: notified.append(msg) or True)
    monkeypatch.setattr(github_auth, "_sleep", lambda s: None)
    monkeypatch.setenv("RIG_GH_AUTH_WAIT", "2")
    monkeypatch.setenv("RIG_GH_AUTH_POLL", "1")
    t = iter(range(0, 100))
    monkeypatch.setattr(github_auth, "_now", lambda: float(next(t)))
    out = github_auth.ensure_browser_auth(owner="acme", repo="widget")
    assert out.state == "timed_out" and notified and "agent-browser" in out.detail


def test_browser_auth_no_phone_spam_at_zero_budget(monkeypatch):
    """At the 0 (unattended) budget the browser gate degrades loud WITHOUT a tg ping — same anti-spam
    rule as the gh gate (round-5 #1)."""
    notified: list = []
    monkeypatch.setattr(github_auth, "_browser_auth_ok", lambda: False)
    monkeypatch.setattr(github_auth, "_notify", lambda msg: notified.append(msg) or True)
    monkeypatch.setattr(github_auth, "_sleep", lambda s: None)
    monkeypatch.setenv("RIG_GH_AUTH_WAIT", "0")
    out = github_auth.ensure_browser_auth(owner="acme", repo="widget")
    assert out.state == "timed_out" and not notified


def test_gate_blocks_before_any_api_call_when_not_authed(monkeypatch, gh_repo, tmp_path):
    """#4136.1 + review-finding #1: the gate runs BEFORE the first read, not just before the PATCH.

    The realistic no-auth case is "the very first `gh api` read fails because there's no token". If
    the gate only ran before the mutation, that read would short-circuit to a plain `gh_error` and
    the notify-and-wait gate would be dead code. So the gate must fire FIRST — proven here by
    asserting `_gh_api` is NEVER called when the gate is not-ok (the read never even happens).
    """
    monkeypatch.setattr(github_common_mod, "ensure_gh_auth",
                        lambda **kw: github_auth.AuthOutcome("timed_out", detail="run `gh auth login`"))
    calls: list = []
    monkeypatch.setattr(github_common_mod, "_gh_api", lambda args, *, input_text=None: calls.append(args) or (0, "{}", ""))
    res = runner._do_provision_github_merge(_merge_action(tmp_path), "backup")
    assert res.status == "error" and "gh auth login" in res.detail
    assert not calls  # neither the read NOR the mutation happened — the gate fired first


def test_unauthed_read_failure_triggers_the_gate_not_a_bare_error(monkeypatch, gh_repo, tmp_path):
    """End-to-end of finding #1: with a REAL not-authed probe, an apply hits the notify-and-wait
    gate (which returns its actionable detail), instead of the first read failing into a bare
    `gh_error`. We stub the auth PROBE (not the runner gate) to logged-out + a recorded notify."""
    notified: list = []
    # Restore the REAL gate (the autouse `_authed` fixture stubbed runner.ensure_gh_auth to ok) so
    # this test exercises the genuine probe→notify→wait path against a logged-out probe.
    monkeypatch.setattr(github_common_mod, "ensure_gh_auth", github_auth.ensure_gh_auth)
    monkeypatch.setattr(github_auth, "_gh_auth_ok", lambda: False)
    monkeypatch.setattr(github_auth.shutil, "which", lambda name: "/usr/bin/gh")  # gh present, just logged out
    monkeypatch.setattr(github_auth, "_notify", lambda msg: notified.append(msg) or True)
    monkeypatch.setattr(github_auth, "_sleep", lambda s: None)
    # a positive (bounded) budget so the "ask and WAIT" path runs — notify fires, then it times out.
    monkeypatch.setenv("RIG_GH_AUTH_WAIT", "2")
    monkeypatch.setenv("RIG_GH_AUTH_POLL", "1")
    t = iter(range(0, 100))
    monkeypatch.setattr(github_auth, "_now", lambda: float(next(t)))
    api_calls: list = []
    monkeypatch.setattr(github_common_mod, "_gh_api", lambda args, *, input_text=None: api_calls.append(args) or (0, "{}", ""))
    res = runner._do_provision_github_merge(_merge_action(tmp_path), "backup")
    assert res.status == "error" and "gh auth login" in res.detail
    assert notified, "the user must have been pinged (tg) about the missing login — the #4136.1 contract"
    assert not api_calls  # the gate intercepted before any gh api read/mutation


# ════════════════════════════════════════════════════════════════════════════════════
# plan wiring + config validation (the schema is the single source of truth)
# ════════════════════════════════════════════════════════════════════════════════════
def test_plan_emits_all_github_actions_default_on(fake_agent_tools, tmp_path):
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg({}, tmp_path), cat, project_type="cli")
    kinds = {a.kind for a in plan.actions}
    assert {
        "provision_github_ruleset",
        "provision_github_merge",
        "provision_github_ghas",
        "provision_github_actions",
        "provision_github_browser",
    } <= kinds


def test_plan_opt_out_per_subblock(fake_agent_tools, tmp_path):
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"github": {"ghas": {"enabled": False}, "actions": {"enabled": False}}}, tmp_path)
    plan = build(cfg, cat, project_type="cli")
    kinds = {a.kind for a in plan.actions}
    assert "provision_github_ghas" not in kinds and "provision_github_actions" not in kinds
    assert "provision_github_merge" in kinds  # the others stay on


# ════════════════════════════════════════════════════════════════════════════════════
# ruleset required status checks — ROADMAP §5: PR Checklist + review-threads as REQUIRED checks,
# derived from the merge-gating CI gates the repo actually provisions (lockout-safe).
# ════════════════════════════════════════════════════════════════════════════════════
def _ruleset_action(plan):
    return next(a for a in plan.actions if a.kind == "provision_github_ruleset")


# CI items are default-OFF in the catalog, so the auto-default only kicks in for gates the config
# opts into — exactly mirroring the scaffold (riglib/state.py), which enables both merge gates.
_BOTH_GATES = {"ci": {"items": {"pr-checklist": {"enabled": True}, "review-threads": {"enabled": True}}}}


def test_ruleset_requires_enabled_ci_gates_as_status_checks(fake_agent_tools, tmp_path):
    """Both merge-gating gates present+enabled → both contexts become required status checks."""
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg(_BOTH_GATES, tmp_path), cat, project_type="cli")
    checks = _ruleset_action(plan).options["required_status_checks"]
    # The check-run CONTEXT names (job `name:`), not the slot names — matching what GitHub reports.
    assert checks == ["PR Checklist", "review-threads"]


def test_ruleset_omits_status_check_for_a_disabled_gate(fake_agent_tools, tmp_path):
    """Lockout guard: a DISABLED merge-gating gate must NOT be required (its check never runs)."""
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"ci": {"items": {"pr-checklist": {"enabled": True}, "review-threads": {"enabled": False}}}}, tmp_path)
    plan = build(cfg, cat, project_type="cli")
    checks = _ruleset_action(plan).options["required_status_checks"]
    assert checks == ["PR Checklist"]  # review-threads dropped — requiring it would wedge every PR


def test_ruleset_no_required_checks_when_no_gate_enabled(fake_agent_tools, tmp_path):
    """No merge-gating gate enabled → no required checks (else every PR locks out)."""
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg({}, tmp_path), cat, project_type="cli")
    assert _ruleset_action(plan).options["required_status_checks"] == []


def test_ruleset_no_required_checks_when_ci_disabled(fake_agent_tools, tmp_path):
    """CI off entirely → no workflows land → no required checks (else every PR locks out)."""
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"ci": {"enabled": False, "items": {"review-threads": {"enabled": True}}}}, tmp_path)
    plan = build(cfg, cat, project_type="cli")
    assert _ruleset_action(plan).options["required_status_checks"] == []


def test_ruleset_no_required_checks_when_ci_export_only(fake_agent_tools, tmp_path):
    """export-only writes no workflow into the repo, so its checks never run → require none."""
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"ci": {"target": "export-only", "items": {"review-threads": {"enabled": True}}}}, tmp_path)
    plan = build(cfg, cat, project_type="cli")
    assert _ruleset_action(plan).options["required_status_checks"] == []


def test_ruleset_no_required_checks_for_custom_non_standard_ci_target(fake_agent_tools, tmp_path):
    """Review-finding (round 4) #1: GitHub only runs workflows from `.github/workflows`. A CUSTOM
    target dir means the check-run never appears, so requiring its context would wedge every PR. The
    auto-default must require nothing for any target other than `.github/workflows`."""
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"ci": {"target": ".ci/workflows", "items": {"review-threads": {"enabled": True}}}}, tmp_path)
    plan = build(cfg, cat, project_type="cli")
    assert _ruleset_action(plan).options["required_status_checks"] == []


def test_ci_gate_check_contexts_match_real_workflow_job_names():
    """Review-finding (round 3) #1 — the LOCKOUT guard's load-bearing fact: the context a ruleset
    requires must equal the check-run name GitHub reports, which for an Actions check is the JOB's
    `name:` (when set), NOT the workflow's top-level `name:`. A wrong context wedges every PR. Assert
    each `CI_GATE_CHECK_CONTEXTS` value equals the real agent-tools workflow's JOB name. Skips when
    the agent-tools checkout isn't beside this repo (so a thin checkout doesn't fail the suite)."""
    import re as _re
    from pathlib import Path as _Path

    from riglib.github_ruleset import CI_GATE_CHECK_CONTEXTS

    at = _Path.home() / "xp" / "agent-tools"
    if not (at / "ci").is_dir():
        pytest.skip("agent-tools checkout not present — context-vs-real-workflow guard skipped")
    for slot, context in CI_GATE_CHECK_CONTEXTS.items():
        wf = at / "ci" / slot / "workflow.yml"
        assert wf.is_file(), f"merge-gating CI slot {slot!r} has no workflow.yml in agent-tools"
        text = wf.read_text()
        # the FIRST `jobs:` entry's `name:` is the check-run context (gh reports a single-job
        # workflow's check by that job name); fall back to the job id when no job `name:`.
        job_block = text.split("jobs:", 1)[1] if "jobs:" in text else ""
        names = _re.findall(r"^\s{4}name:\s*(.+?)\s*$", job_block, _re.MULTILINE)
        job_ids = _re.findall(r"^\s{2}([A-Za-z0-9_-]+):\s*$", job_block, _re.MULTILINE)
        candidates = set(names) | set(job_ids)
        assert context in candidates, (
            f"CI_GATE_CHECK_CONTEXTS[{slot!r}]={context!r} matches no job name/id in {wf} "
            f"(found {sorted(candidates)}) — a stale context would LOCK OUT every PR"
        )


def test_required_checks_match_written_workflow_job_names_in_one_plan(fake_agent_tools, tmp_path):
    """Review-finding (round 4) #2 — the lockout invariant, tested HERMETICALLY in a single build:
    every required_status_checks context the plan emits must equal a JOB NAME in the workflow that
    the SAME plan writes (an `install_ci` action for that slot). This couples "required" to
    "written" without needing the real agent-tools checkout — a context/job-name mismatch (the exact
    lockout footgun) fails here, not silently on a live repo."""
    import re as _re

    from riglib.catalog import Catalog
    from riglib.github_ruleset import CI_GATE_CHECK_CONTEXTS

    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg(_BOTH_GATES, tmp_path), cat, project_type="cli")
    required = _ruleset_action(plan).options["required_status_checks"]
    assert required, "expected the auto-default to require the merge gates"
    context_to_slot = {ctx: slot for slot, ctx in CI_GATE_CHECK_CONTEXTS.items()}
    written_ci = {a.item: a for a in plan.actions if a.kind == "install_ci"}
    for context in required:
        slot = context_to_slot[context]
        assert slot in written_ci, f"required context {context!r} but its workflow (slot {slot!r}) is not written"
        text = (written_ci[slot].source / "workflow.yml").read_text()
        job_block = text.split("jobs:", 1)[1] if "jobs:" in text else ""
        names = _re.findall(r"^\s{4}name:\s*(.+?)\s*$", job_block, _re.MULTILINE)
        ids = _re.findall(r"^\s{2}([A-Za-z0-9_-]+):\s*$", job_block, _re.MULTILINE)
        assert context in (set(names) | set(ids)), (
            f"required context {context!r} is not a job name/id in the workflow the plan writes "
            f"for slot {slot!r} (found {sorted(set(names) | set(ids))}) — this would LOCK OUT every PR"
        )


def test_ruleset_required_checks_honor_ci_enable_delta_list(fake_agent_tools, tmp_path):
    """The `enable:` delta list also flips a gate on — its context must be required too."""
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"ci": {"enable": ["pr-checklist", "review-threads"]}}, tmp_path)
    plan = build(cfg, cat, project_type="cli")
    assert _ruleset_action(plan).options["required_status_checks"] == ["PR Checklist", "review-threads"]


def test_ruleset_explicit_required_checks_win_verbatim(fake_agent_tools, tmp_path):
    """An explicit config list is honored as-is — the CI-derived auto-default never overrides it."""
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {**_BOTH_GATES, "github": {"ruleset": {"required_status_checks": ["my-custom-check"]}}},
        tmp_path,
    )
    plan = build(cfg, cat, project_type="cli")
    assert _ruleset_action(plan).options["required_status_checks"] == ["my-custom-check"]


def test_ruleset_explicit_empty_required_checks_means_require_none(fake_agent_tools, tmp_path):
    """An explicit `[]` is a deliberate 'require no checks' and survives the auto-default."""
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {**_BOTH_GATES, "github": {"ruleset": {"required_status_checks": []}}},
        tmp_path,
    )
    plan = build(cfg, cat, project_type="cli")
    assert _ruleset_action(plan).options["required_status_checks"] == []


def test_validate_rejects_unknown_github_subblock_key(tmp_path):
    with pytest.raises(ConfigError) as e:
        validate({"github": {"merge": {"squash_merg": True}}})  # typo
    assert "github.merge" in str(e.value)


def test_validate_rejects_non_bool_ghas_knob():
    with pytest.raises(ConfigError):
        validate({"github": {"ghas": {"secret_scanning": "yes"}}})


def test_validate_rejects_bad_actions_enum():
    with pytest.raises(ConfigError) as e:
        validate({"github": {"actions": {"allowed_actions": "everything"}}})
    assert "allowed_actions" in str(e.value)


def test_validate_rejects_merge_with_no_model_enabled():
    """All three merge models off → GitHub 422; catch it at config time, not on the live PATCH."""
    with pytest.raises(ConfigError) as e:
        validate({"github": {"merge": {"squash_merge": False, "merge_commit": False, "rebase_merge": False}}})
    assert "merge model" in str(e.value) and "github.merge" in str(e.value)


def test_validate_accepts_merge_with_only_rebase_enabled():
    """A non-default single model is fine — only the all-off case is rejected."""
    validate({"github": {"merge": {"squash_merge": False, "merge_commit": False, "rebase_merge": True}}})


def test_validate_rejects_all_off_reachable_via_single_squash_false():
    """All-off is reachable by setting squash_merge:false ALONE (merge_commit/rebase default off) —
    the validator resolves the omitted knobs to their defaults, so this must be rejected too."""
    with pytest.raises(ConfigError) as e:
        validate({"github": {"merge": {"squash_merge": False}}})
    assert "merge model" in str(e.value)


def test_validate_allows_all_off_merge_when_block_is_disabled():
    """Review-finding (round 4) #3: an all-off merge block that is `enabled: false` is harmless — the
    plan skips it entirely — so validation must NOT reject it (the model check is for live blocks)."""
    validate({"github": {"merge": {"enabled": False, "squash_merge": False, "merge_commit": False, "rebase_merge": False}}})


def test_validate_rejects_non_mapping_github_subblock():
    """A scalar where a sub-block mapping is expected fails closed with the schema path."""
    with pytest.raises(ConfigError) as e:
        validate({"github": {"merge": "squash"}})
    assert "github.merge must be a mapping" in str(e.value)


def test_validate_merge_null_knob_resolves_to_default_not_all_off():
    """Review-finding (round 6) #3: an explicit `null` resolves to the DEFAULT (matching the plan,
    which drops null overrides), so `squash_merge: null` is squash-ON — NOT an all-off rejection."""
    validate({"github": {"merge": {"squash_merge": None, "merge_commit": False, "rebase_merge": False}}})


def test_validate_accepts_valid_full_github_block():
    # the scaffolded shape (all sub-blocks present, secure defaults) round-trips clean.
    from riglib.state import default_state

    validate(default_state())  # must not raise

