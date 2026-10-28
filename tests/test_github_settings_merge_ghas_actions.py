"""github.merge / ghas / actions repo-settings backends (split from test_github_settings).
"""

from __future__ import annotations

import json
from tests.github_settings_helpers import (_actions_action, _authed, _ghas_action, _merge_action, gh_repo)



from riglib.actions import runner
from riglib.actions import github_actions_action as github_actions_action_mod
from riglib.actions import github_ghas_action as github_ghas_action_mod
from riglib.actions import github_merge_action as github_merge_action_mod
from riglib.drift import detect
from riglib import github_auth
from riglib.github_actions import (
    GITHUB_ACTIONS_DEFAULTS,
    build_permissions_body,
    build_workflow_permissions_body,
    normalize_permissions,
)
from riglib.github_ghas import (
    GITHUB_GHAS_DEFAULTS,
    build_security_analysis_body,
    normalize_security_analysis,
)
from riglib.github_merge import (
    GITHUB_MERGE_DEFAULTS,
    MANAGED_MERGE_API_FIELDS,
    build_merge_body,
)
from riglib.actions import github_common as github_common_mod


# ════════════════════════════════════════════════════════════════════════════════════
# github.merge — squash-only merge-button policy (PATCH /repos)
# ════════════════════════════════════════════════════════════════════════════════════
def test_merge_body_is_squash_only_secure_default():
    body = build_merge_body(GITHUB_MERGE_DEFAULTS)
    assert body == {
        "allow_squash_merge": True,
        "allow_merge_commit": False,
        "allow_rebase_merge": False,
        "delete_branch_on_merge": True,
        "allow_auto_merge": True,
        "allow_update_branch": True,
    }


def test_merge_body_carries_only_managed_fields_never_repo_identity():
    # the PATCH body must never carry a name/visibility key — a guard against renaming/exposing.
    body = build_merge_body({**GITHUB_MERGE_DEFAULTS, "merge_commit": True})
    assert set(body) == set(MANAGED_MERGE_API_FIELDS)
    assert not any(k in body for k in ("name", "private", "visibility", "description"))


def test_merge_body_hard_bools():
    # a stray truthy/falsy config value is coerced to a real bool (never sends 1 / "").
    body = build_merge_body({**GITHUB_MERGE_DEFAULTS, "squash_merge": 1, "merge_commit": ""})
    assert body["allow_squash_merge"] is True and body["allow_merge_commit"] is False


def test_merge_state_update_then_ok(monkeypatch, gh_repo, tmp_path):
    # live repo currently allows merge commits → update; after we report it matches → ok.
    live = {"allow_squash_merge": True, "allow_merge_commit": True, "allow_rebase_merge": True,
            "delete_branch_on_merge": False, "allow_auto_merge": False, "allow_update_branch": False}
    monkeypatch.setattr(github_common_mod, "_gh_api", lambda args, *, input_text=None: (0, json.dumps(live), ""))
    state, _ = github_merge_action_mod.github_merge_state(_merge_action(tmp_path))
    assert state == "update"

    matching = dict(build_merge_body(GITHUB_MERGE_DEFAULTS))
    monkeypatch.setattr(github_common_mod, "_gh_api", lambda args, *, input_text=None: (0, json.dumps(matching), ""))
    state, _ = github_merge_action_mod.github_merge_state(_merge_action(tmp_path))
    assert state == "ok"


def test_merge_apply_patches_only_managed_fields(monkeypatch, gh_repo, tmp_path):
    live = {"allow_squash_merge": False}  # differs → update
    patched: dict = {}

    def fake(args, *, input_text=None):
        if "--method" in args and args[args.index("--method") + 1] == "PATCH":
            patched["body"] = json.loads(input_text)
            return 0, "{}", ""
        return 0, json.dumps(live), ""

    monkeypatch.setattr(github_common_mod, "_gh_api", fake)
    res = runner._do_provision_github_merge(_merge_action(tmp_path), "backup")
    assert res.status == "updated"
    assert patched["body"] == build_merge_body(GITHUB_MERGE_DEFAULTS)
    assert "name" not in patched["body"] and "private" not in patched["body"]


def test_merge_no_remote_is_skip_not_error(monkeypatch, tmp_path):
    monkeypatch.setattr(github_common_mod, "github_owner_repo", lambda repo_root: None)
    res = runner._do_provision_github_merge(_merge_action(tmp_path), "backup")
    assert res.status == "skipped" and "no github origin" in res.detail


def test_merge_gh_error_degrades_loud(monkeypatch, gh_repo, tmp_path):
    # a no-admin token / not-authed read → 403 → loud error, never a silent green.
    monkeypatch.setattr(github_common_mod, "_gh_api", lambda args, *, input_text=None: (1, "", "HTTP 403: Forbidden"))
    res = runner._do_provision_github_merge(_merge_action(tmp_path), "backup")
    assert res.status == "error" and "403" in res.detail


def test_merge_dry_run_makes_no_patch(monkeypatch, gh_repo, tmp_path):
    monkeypatch.setenv("RIG_GH_DRY_RUN", "1")
    live = {"allow_squash_merge": False}
    calls: list = []

    def fake(args, *, input_text=None):
        calls.append(args)
        return 0, json.dumps(live), ""

    monkeypatch.setattr(github_common_mod, "_gh_api", fake)
    res = runner._do_provision_github_merge(_merge_action(tmp_path), "backup")
    assert res.status == "updated" and "RIG_GH_DRY_RUN" in res.detail
    assert not any("--method" in c for c in calls)  # no PATCH issued


def test_merge_drift_parity(monkeypatch, gh_repo, tmp_path):
    # apply and drift switch on the SAME github_merge_state.
    live = {"allow_squash_merge": True, "allow_merge_commit": True}
    monkeypatch.setattr(github_common_mod, "_gh_api", lambda args, *, input_text=None: (0, json.dumps(live), ""))
    plan_obj = type("P", (), {"actions": [_merge_action(tmp_path)]})()
    report = detect(plan_obj)  # type: ignore[arg-type]
    merge_items = [i for i in report.items if i.item == "merge"]
    assert merge_items and merge_items[0].direction == "modified"


# ════════════════════════════════════════════════════════════════════════════════════
# github.ghas — GitHub Advanced Security
# ════════════════════════════════════════════════════════════════════════════════════
def test_ghas_security_analysis_body_secure_default():
    body = build_security_analysis_body(GITHUB_GHAS_DEFAULTS)
    # dependency_graph is NOT in the PATCH-body diff (github.com doesn't round-trip it); only the
    # secret-scanning fields are compared/PATCHed via security_and_analysis.
    assert body == {
        "secret_scanning": {"status": "enabled"},
        "secret_scanning_push_protection": {"status": "enabled"},
    }
    assert "dependency_graph" not in body


def test_ghas_body_disabled_knob_sets_disabled_status():
    body = build_security_analysis_body({**GITHUB_GHAS_DEFAULTS, "secret_scanning": False})
    assert body["secret_scanning"] == {"status": "disabled"}


def test_ghas_normalize_missing_block_reads_disabled():
    assert normalize_security_analysis({}) == {
        "secret_scanning": "disabled",
        "secret_scanning_push_protection": "disabled",
    }


def test_ghas_state_update_when_scanner_off(monkeypatch, gh_repo, tmp_path):
    live = {"security_and_analysis": {"secret_scanning": {"status": "disabled"},
                                      "dependency_graph": {"status": "enabled"},
                                      "secret_scanning_push_protection": {"status": "enabled"}}}
    monkeypatch.setattr(github_common_mod, "_gh_api", lambda args, *, input_text=None: (0, json.dumps(live), ""))
    state, _ = github_ghas_action_mod.github_ghas_state(_ghas_action(tmp_path))
    assert state == "update"


def test_ghas_apply_patches_sub_resources_and_codeql(monkeypatch, gh_repo, tmp_path):
    # everything is currently OFF, so apply must drive the security PATCH, both sub-resource PUTs,
    # and the CodeQL PATCH (read-then-act: the GETs report off, so the mutations fire).
    live = {"security_and_analysis": {"secret_scanning": {"status": "disabled"}}}
    seen: list[tuple[str, str]] = []

    def fake(args, *, input_text=None):
        if "--method" in args:
            method = args[args.index("--method") + 1]
            path = args[args.index("--method") + 2]
            seen.append((method, path))
            return 0, "{}", ""
        # GETs: repo object, the sub-resource presence reads (404 = off), CodeQL state (off).
        if args[0].endswith("/vulnerability-alerts") or args[0].endswith("/automated-security-fixes"):
            return 1, "", "HTTP 404: Not Found"  # disabled
        if args[0].endswith("/default-setup"):
            return 0, json.dumps({"state": "not-configured"}), ""
        return 0, json.dumps(live), ""

    monkeypatch.setattr(github_common_mod, "_gh_api", fake)
    res = runner._do_provision_github_ghas(_ghas_action(tmp_path), "backup")
    assert res.status == "updated"
    methods = {(m, p.split("/")[-1]) for m, p in seen}
    assert ("PATCH", "widget") in methods  # PATCH /repos/acme/widget (security_and_analysis)
    assert ("PUT", "vulnerability-alerts") in methods
    assert ("PUT", "automated-security-fixes") in methods
    assert ("PATCH", "default-setup") in methods


def test_ghas_second_apply_is_idempotent_noop(monkeypatch, gh_repo, tmp_path):
    # everything already in the desired state → state ok, NO mutations, reports skipped.
    sa = {f: {"status": s} for f, s in {f2: n["status"] for f2, n in build_security_analysis_body(GITHUB_GHAS_DEFAULTS).items()}.items()}
    live = {"security_and_analysis": sa}
    mutated: list = []

    def fake(args, *, input_text=None):
        if "--method" in args:
            mutated.append(args)
            return 0, "{}", ""
        if args[0].endswith("/vulnerability-alerts"):
            return 0, "", ""  # 204 → enabled (desired)
        if args[0].endswith("/automated-security-fixes"):
            return 0, json.dumps({"enabled": True}), ""
        if args[0].endswith("/default-setup"):
            return 0, json.dumps({"state": "configured"}), ""
        return 0, json.dumps(live), ""

    monkeypatch.setattr(github_common_mod, "_gh_api", fake)
    res = runner._do_provision_github_ghas(_ghas_action(tmp_path), "backup")
    assert res.status == "skipped" and not mutated  # a true no-op second apply


def test_ghas_private_unlicensed_degrades_loud_not_crash(monkeypatch, gh_repo, tmp_path):
    # a private repo without a GHAS plan → 403/422 on the scanners; the apply degrades LOUDLY but
    # does not crash and does not return a silent green.
    live = {"security_and_analysis": {"secret_scanning": {"status": "disabled"}}}

    def fake(args, *, input_text=None):
        if "--method" in args and args[args.index("--method") + 1] == "PATCH" and args[args.index("--method") + 2] == "repos/acme/widget":
            return 1, "", "HTTP 422: Advanced Security is not available for this repository"
        if "--method" in args:
            return 0, "{}", ""
        return 0, json.dumps(live), ""

    monkeypatch.setattr(github_common_mod, "_gh_api", fake)
    res = runner._do_provision_github_ghas(_ghas_action(tmp_path), "backup")
    assert res.status == "updated"  # the free features still applied
    assert "DEGRADED" in res.detail and "GHAS" in res.detail


def test_ghas_plan_gated_scanner_unreadable_still_applies_free_features(monkeypatch, gh_repo, tmp_path):
    """Review-finding #2: a code-scanning endpoint that can't even be READ (plan-gated 403 on a
    private repo) must NOT collapse the whole apply to an error. The free features (vuln-alerts /
    Dependabot) must still be applied, and the unreadable scanner degraded LOUDLY — not a bare error
    that strands everything (the old behavior returned `gh_error` from the classifier and bailed)."""
    live = {"security_and_analysis": {"secret_scanning": {"status": "disabled"}}}
    puts: list[str] = []

    def fake(args, *, input_text=None):
        if "--method" in args:
            path = args[args.index("--method") + 2]
            if path.endswith("/default-setup"):
                return 1, "", "HTTP 403: Advanced Security is not available for this repository"
            puts.append(path.split("/")[-1])
            return 0, "{}", ""
        if args[0].endswith("/vulnerability-alerts") or args[0].endswith("/automated-security-fixes"):
            return 1, "", "HTTP 404: Not Found"  # off → apply will enable them (free features)
        if args[0].endswith("/default-setup"):
            return 1, "", "HTTP 403: Advanced Security is not available for this repository"  # UNREADABLE
        return 0, json.dumps(live), ""

    monkeypatch.setattr(github_common_mod, "_gh_api", fake)
    res = runner._do_provision_github_ghas(_ghas_action(tmp_path), "backup")
    assert res.status == "updated"
    # the FREE features were still applied despite the plan-gated scanner being unreadable
    assert "vulnerability-alerts" in puts and "automated-security-fixes" in puts
    # and the unreadable scanner is surfaced LOUDLY, not silently dropped
    assert "DEGRADED" in res.detail and "code-scanning" in res.detail


def test_ghas_real_auth_error_is_not_swallowed_as_degrade(monkeypatch, gh_repo, tmp_path):
    # a genuine permission error (not a plan/feature limit) must surface as an error, not a degrade.
    live = {"security_and_analysis": {"secret_scanning": {"status": "disabled"}}}

    def fake(args, *, input_text=None):
        if "--method" in args and args[args.index("--method") + 1] == "PATCH" and args[args.index("--method") + 2] == "repos/acme/widget":
            return 1, "", "HTTP 403: Resource not accessible by integration"
        if "--method" in args:
            return 0, "{}", ""
        return 0, json.dumps(live), ""

    monkeypatch.setattr(github_common_mod, "_gh_api", fake)
    res = runner._do_provision_github_ghas(_ghas_action(tmp_path), "backup")
    assert res.status == "error" and "403" in res.detail


def test_ghas_sa_block_hard_error_still_applies_free_features(monkeypatch, gh_repo, tmp_path):
    """Review-finding (round 8) #1: a GENUINE security_and_analysis PATCH failure must NOT early-out
    and strand the free features — it goes to hard_errors (final status=error) but vuln-alerts /
    Dependabot are still attempted, matching the 'applied independently' design."""
    live = {"security_and_analysis": {"secret_scanning": {"status": "disabled"}}}  # SA drifts
    puts: list[str] = []

    def fake(args, *, input_text=None):
        if "--method" in args:
            method, path = args[args.index("--method") + 1], args[args.index("--method") + 2]
            if method == "PATCH" and path == "repos/acme/widget":
                return 1, "", "HTTP 403: Resource not accessible by integration"  # REAL error
            puts.append(path.split("/")[-1])
            return 0, "{}", ""
        if args[0].endswith("/vulnerability-alerts"):
            return 1, "", "HTTP 404: Not Found"  # off → apply will PUT it (a free feature)
        if args[0].endswith("/automated-security-fixes"):
            return 1, "", "HTTP 404: Not Found"
        if args[0].endswith("/default-setup"):
            return 0, json.dumps({"state": "configured"}), ""
        return 0, json.dumps(live), ""

    monkeypatch.setattr(github_common_mod, "_gh_api", fake)
    res = runner._do_provision_github_ghas(_ghas_action(tmp_path), "backup")
    assert res.status == "error" and "FAILED" in res.detail and "security_and_analysis" in res.detail
    # the free features were STILL attempted despite the SA-block hard error (no early return)
    assert "vulnerability-alerts" in puts and "automated-security-fixes" in puts


def test_ghas_real_subresource_write_error_is_hard_error_not_silent_degrade(monkeypatch, gh_repo, tmp_path):
    """Review-finding (round 3) #2: a GENUINE (non-plan-limit) failure on a sub-resource PUT/DELETE
    must surface as status=error — NOT 'updated (degraded)', which automation checking
    `status != error` would read as a false green. SA block matches; the sub-resource WRITE 403s."""
    sa = {f2: n["status"] for f2, n in build_security_analysis_body(GITHUB_GHAS_DEFAULTS).items()}
    live = {"security_and_analysis": {k: {"status": v} for k, v in sa.items()}}

    def fake(args, *, input_text=None):
        if "--method" in args:
            path = args[args.index("--method") + 2]
            if path.endswith("/vulnerability-alerts"):
                return 1, "", "HTTP 403: Resource not accessible by integration"  # REAL error, not plan
            return 0, "{}", ""
        if args[0].endswith("/vulnerability-alerts"):
            return 1, "", "HTTP 404: Not Found"  # off → apply will try to PUT it (and that 403s)
        if args[0].endswith("/automated-security-fixes"):
            return 0, json.dumps({"enabled": True}), ""
        if args[0].endswith("/default-setup"):
            return 0, json.dumps({"state": "configured"}), ""
        return 0, json.dumps(live), ""

    monkeypatch.setattr(github_common_mod, "_gh_api", fake)
    res = runner._do_provision_github_ghas(_ghas_action(tmp_path), "backup")
    assert res.status == "error" and "403" in res.detail and "FAILED" in res.detail


def test_ghas_plan_limit_classified_on_full_string_not_truncated_prefix(monkeypatch, gh_repo, tmp_path):
    """Review-finding (round 7) #2: a verbose gh prefix could push the plan-limit phrase past the
    80-char display truncation. Classification must use the FULL string, so a real plan limit still
    DEGRADES (not a hard error) even when the key phrase sits far into a long message."""
    sa = {f2: n["status"] for f2, n in build_security_analysis_body(GITHUB_GHAS_DEFAULTS).items()}
    live = {"security_and_analysis": {k: {"status": v} for k, v in sa.items()}}
    long_prefix = "HTTP 422: Validation Failed. " + ("blah " * 25)  # >100 chars before the phrase
    plan_limit = long_prefix + "GitHub Advanced Security is not available for this repository"

    def fake(args, *, input_text=None):
        if "--method" in args:
            path = args[args.index("--method") + 2]
            if path.endswith("/vulnerability-alerts"):
                return 1, "", plan_limit
            return 0, "{}", ""
        if args[0].endswith("/vulnerability-alerts"):
            return 1, "", "HTTP 404: Not Found"
        if args[0].endswith("/automated-security-fixes"):
            return 0, json.dumps({"enabled": True}), ""
        if args[0].endswith("/default-setup"):
            return 0, json.dumps({"state": "configured"}), ""
        return 0, json.dumps(live), ""

    monkeypatch.setattr(github_common_mod, "_gh_api", fake)
    res = runner._do_provision_github_ghas(_ghas_action(tmp_path), "backup")
    assert res.status == "updated" and "DEGRADED" in res.detail and "FAILED" not in res.detail


def test_ghas_actions_disabled_collision_is_hard_error_not_unlicensed_degrade(monkeypatch, gh_repo, tmp_path):
    """Review-finding (round 7) #3: 'Actions must be enabled for default setup' is a FIXABLE config
    collision (turn Actions on), not a plan limit — it must surface as a hard error, not degrade."""
    live = {"security_and_analysis": {f2: {"status": n["status"]}
                                      for f2, n in build_security_analysis_body(GITHUB_GHAS_DEFAULTS).items()}}

    def fake(args, *, input_text=None):
        if "--method" in args:
            path = args[args.index("--method") + 2]
            if path.endswith("/default-setup"):
                return 1, "", "HTTP 409: Actions must be enabled for default setup to be configured"
            return 0, "{}", ""
        if args[0].endswith("/vulnerability-alerts"):
            return 0, "", ""
        if args[0].endswith("/automated-security-fixes"):
            return 0, json.dumps({"enabled": True}), ""
        if args[0].endswith("/default-setup"):
            return 0, json.dumps({"state": "not-configured"}), ""  # drift → apply tries to configure
        return 0, json.dumps(live), ""

    monkeypatch.setattr(github_common_mod, "_gh_api", fake)
    res = runner._do_provision_github_ghas(_ghas_action(tmp_path), "backup")
    assert res.status == "error" and "FAILED" in res.detail and "code-scanning" in res.detail


def test_ghas_transient_service_error_is_hard_error_not_swallowed_as_unlicensed(monkeypatch, gh_repo, tmp_path):
    """Review-finding (round 6) #4: a transient '503 Service not available' must NOT be mis-classified
    as a GHAS plan limit (which would degrade to a false green). Only feature/plan wording degrades."""
    sa = {f2: n["status"] for f2, n in build_security_analysis_body(GITHUB_GHAS_DEFAULTS).items()}
    live = {"security_and_analysis": {k: {"status": v} for k, v in sa.items()}}

    def fake(args, *, input_text=None):
        if "--method" in args:
            path = args[args.index("--method") + 2]
            if path.endswith("/vulnerability-alerts"):
                return 1, "", "HTTP 503: Service not available, try again later"  # transient, NOT a plan limit
            return 0, "{}", ""
        if args[0].endswith("/vulnerability-alerts"):
            return 1, "", "HTTP 404: Not Found"
        if args[0].endswith("/automated-security-fixes"):
            return 0, json.dumps({"enabled": True}), ""
        if args[0].endswith("/default-setup"):
            return 0, json.dumps({"state": "configured"}), ""
        return 0, json.dumps(live), ""

    monkeypatch.setattr(github_common_mod, "_gh_api", fake)
    res = runner._do_provision_github_ghas(_ghas_action(tmp_path), "backup")
    assert res.status == "error" and "FAILED" in res.detail  # not silently degraded


def test_ghas_plan_limited_subresource_write_still_degrades_not_errors(monkeypatch, gh_repo, tmp_path):
    """The flip side: a PLAN/FEATURE limit on a sub-resource write degrades loudly but stays
    `updated` (the free features that DID apply are not thrown away)."""
    sa = {f2: n["status"] for f2, n in build_security_analysis_body(GITHUB_GHAS_DEFAULTS).items()}
    live = {"security_and_analysis": {k: {"status": v} for k, v in sa.items()}}

    def fake(args, *, input_text=None):
        if "--method" in args:
            path = args[args.index("--method") + 2]
            if path.endswith("/vulnerability-alerts"):
                return 1, "", "HTTP 422: Dependency graph is not available for this repository"
            return 0, "{}", ""
        if args[0].endswith("/vulnerability-alerts"):
            return 1, "", "HTTP 404: Not Found"
        if args[0].endswith("/automated-security-fixes"):
            return 0, json.dumps({"enabled": True}), ""
        if args[0].endswith("/default-setup"):
            return 0, json.dumps({"state": "configured"}), ""
        return 0, json.dumps(live), ""

    monkeypatch.setattr(github_common_mod, "_gh_api", fake)
    res = runner._do_provision_github_ghas(_ghas_action(tmp_path), "backup")
    assert res.status == "updated" and "DEGRADED" in res.detail and "FAILED" not in res.detail


def test_ghas_no_remote_skip(monkeypatch, tmp_path):
    monkeypatch.setattr(github_common_mod, "github_owner_repo", lambda repo_root: None)
    res = runner._do_provision_github_ghas(_ghas_action(tmp_path), "backup")
    assert res.status == "skipped"


def test_ghas_drift_could_not_verify_on_repo_read_failure(monkeypatch, gh_repo, tmp_path):
    """Review-finding (round 7) test gap: `rig status` ghas leg must surface a VISIBLE 'could not
    verify' item when the repo read fails — never a silent in-sync."""
    monkeypatch.setattr(github_common_mod, "_gh_api", lambda args, *, input_text=None: (1, "", "HTTP 403: forbidden"))
    plan_obj = type("P", (), {"actions": [_ghas_action(tmp_path)]})()
    report = detect(plan_obj)  # type: ignore[arg-type]
    ghas_items = [i for i in report.items if i.item == "ghas"]
    assert ghas_items and ghas_items[0].direction == "modified" and "could not verify" in ghas_items[0].detail


def test_merge_and_actions_drift_could_not_verify_on_read_failure(monkeypatch, gh_repo, tmp_path):
    """Review-finding (round 8) test gap: the merge + actions status legs must ALSO surface a loud
    'could not verify' on a failed read (the key guard against a false-green status under no admin)."""
    monkeypatch.setattr(github_common_mod, "_gh_api", lambda args, *, input_text=None: (1, "", "HTTP 403: forbidden"))
    for action, item in ((_merge_action(tmp_path), "merge"), (_actions_action(tmp_path), "actions")):
        plan_obj = type("P", (), {"actions": [action]})()
        report = detect(plan_obj)  # type: ignore[arg-type]
        items = [i for i in report.items if i.item == item]
        assert items and items[0].direction == "modified" and "could not verify" in items[0].detail


def test_ghas_subresource_disable_sends_delete(monkeypatch, gh_repo, tmp_path):
    """want=False for a sub-resource currently ON → DELETE (the off direction, not only enable)."""
    # SA + codeql already match defaults; only vuln-alerts is desired OFF while live is ON.
    sa = {f2: n["status"] for f2, n in build_security_analysis_body(GITHUB_GHAS_DEFAULTS).items()}
    live = {"security_and_analysis": {k: {"status": v} for k, v in sa.items()}}
    seen: list[tuple[str, str]] = []

    def fake(args, *, input_text=None):
        if "--method" in args:
            seen.append((args[args.index("--method") + 1], args[args.index("--method") + 2].split("/")[-1]))
            return 0, "{}", ""
        if args[0].endswith("/vulnerability-alerts"):
            return 0, "", ""  # 204 → enabled (but we want it OFF)
        if args[0].endswith("/automated-security-fixes"):
            return 0, json.dumps({"enabled": True}), ""
        if args[0].endswith("/default-setup"):
            return 0, json.dumps({"state": "configured"}), ""
        return 0, json.dumps(live), ""

    monkeypatch.setattr(github_common_mod, "_gh_api", fake)
    res = runner._do_provision_github_ghas(_ghas_action(tmp_path, vulnerability_alerts=False), "backup")
    assert res.status == "updated"
    assert ("DELETE", "vulnerability-alerts") in seen  # the OFF direction was driven


def test_ghas_unreadable_subresource_not_double_degraded_nor_re_mutated(monkeypatch, gh_repo, tmp_path):
    """Review-finding (round 2) #1: a sub-resource the classifier couldn't read (read-only token,
    403) must be degraded EXACTLY ONCE and NOT get a doomed PUT/DELETE that 403s a second time."""
    sa = {f2: n["status"] for f2, n in build_security_analysis_body(GITHUB_GHAS_DEFAULTS).items()}
    live = {"security_and_analysis": {k: {"status": v} for k, v in sa.items()}}
    mutations: list[str] = []

    def fake(args, *, input_text=None):
        if "--method" in args:
            mutations.append(args[args.index("--method") + 2])
            return 0, "{}", ""
        if args[0].endswith("/vulnerability-alerts"):
            return 1, "", "HTTP 403: Resource not accessible"  # unreadable (not a 404 → None)
        if args[0].endswith("/automated-security-fixes"):
            return 0, json.dumps({"enabled": True}), ""
        if args[0].endswith("/default-setup"):
            return 0, json.dumps({"state": "configured"}), ""
        return 0, json.dumps(live), ""

    monkeypatch.setattr(github_common_mod, "_gh_api", fake)
    res = runner._do_provision_github_ghas(_ghas_action(tmp_path), "backup")
    assert res.status == "updated"
    # the unreadable endpoint appears in the degrade summary EXACTLY ONCE
    assert res.detail.count("vulnerability-alerts") == 1
    # and NO mutation was attempted against it (no doomed PUT/DELETE)
    assert not any("vulnerability-alerts" in m for m in mutations)


def test_ghas_sa_block_not_re_patched_when_only_subresource_drifts(monkeypatch, gh_repo, tmp_path):
    """Review-finding (round 2) #3: when the security_and_analysis block already matches and only a
    sub-resource drifts, apply must NOT PATCH /repos (a no-op + a misleading 'converged' note)."""
    sa = {f2: n["status"] for f2, n in build_security_analysis_body(GITHUB_GHAS_DEFAULTS).items()}
    live = {"security_and_analysis": {k: {"status": v} for k, v in sa.items()}}
    repo_patched: list = []

    def fake(args, *, input_text=None):
        if "--method" in args:
            path = args[args.index("--method") + 2]
            if path == "repos/acme/widget":
                repo_patched.append(path)
            return 0, "{}", ""
        if args[0].endswith("/vulnerability-alerts"):
            return 1, "", "HTTP 404: Not Found"  # off → only THIS drifts (a free feature to enable)
        if args[0].endswith("/automated-security-fixes"):
            return 0, json.dumps({"enabled": True}), ""
        if args[0].endswith("/default-setup"):
            return 0, json.dumps({"state": "configured"}), ""
        return 0, json.dumps(live), ""

    monkeypatch.setattr(github_common_mod, "_gh_api", fake)
    res = runner._do_provision_github_ghas(_ghas_action(tmp_path), "backup")
    assert res.status == "updated"
    assert not repo_patched  # the SA block already matched — no redundant PATCH /repos
    assert "security_and_analysis converged" not in res.detail  # no misleading note


# ════════════════════════════════════════════════════════════════════════════════════
# github.actions — Actions permissions (least privilege)
# ════════════════════════════════════════════════════════════════════════════════════
def test_actions_token_is_read_only_by_default():
    wf = build_workflow_permissions_body(GITHUB_ACTIONS_DEFAULTS)
    assert wf == {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False}


def test_actions_permissions_body_omits_allowed_when_disabled():
    body = build_permissions_body({**GITHUB_ACTIONS_DEFAULTS, "actions_enabled": False})
    assert body == {"enabled": False}  # the API rejects allowed_actions with enabled=false


def test_actions_state_update_when_token_is_write(monkeypatch, gh_repo, tmp_path):
    def fake(args, *, input_text=None):
        if args[0].endswith("/workflow"):
            return 0, json.dumps({"default_workflow_permissions": "write", "can_approve_pull_request_reviews": True}), ""
        return 0, json.dumps({"enabled": True, "allowed_actions": "all"}), ""

    monkeypatch.setattr(github_common_mod, "_gh_api", fake)
    state, _ = github_actions_action_mod.github_actions_state(_actions_action(tmp_path))
    assert state == "update"


def test_actions_apply_puts_both_endpoints(monkeypatch, gh_repo, tmp_path):
    puts: list[str] = []

    def fake(args, *, input_text=None):
        if "--method" in args and args[args.index("--method") + 1] == "PUT":
            puts.append(args[args.index("--method") + 2])
            return 0, "{}", ""
        if args[0].endswith("/workflow"):
            return 0, json.dumps({"default_workflow_permissions": "write"}), ""
        return 0, json.dumps({"enabled": False}), ""

    monkeypatch.setattr(github_common_mod, "_gh_api", fake)
    res = runner._do_provision_github_actions(_actions_action(tmp_path), "backup")
    assert res.status == "updated"
    assert any(p.endswith("/actions/permissions") for p in puts)
    assert any(p.endswith("/permissions/workflow") for p in puts)


def test_actions_already_in_sync_skips(monkeypatch, gh_repo, tmp_path):
    def fake(args, *, input_text=None):
        if args[0].endswith("/workflow"):
            return 0, json.dumps(build_workflow_permissions_body(GITHUB_ACTIONS_DEFAULTS)), ""
        return 0, json.dumps(normalize_permissions(build_permissions_body(GITHUB_ACTIONS_DEFAULTS))), ""

    monkeypatch.setattr(github_common_mod, "_gh_api", fake)
    res = runner._do_provision_github_actions(_actions_action(tmp_path), "backup")
    assert res.status == "skipped"


def test_actions_disabled_skips_workflow_permissions_put(monkeypatch, gh_repo, tmp_path):
    """Review-finding (round 6) #2: `actions_enabled: false` must NOT issue the workflow-permissions
    PUT (GitHub rejects it when Actions is off) — else a legitimate 'disable Actions' config errors."""
    puts: list[str] = []

    def fake(args, *, input_text=None):
        if "--method" in args:
            puts.append(args[args.index("--method") + 2])
            return 0, "{}", ""
        if args[0].endswith("/workflow"):
            return 0, json.dumps({"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False}), ""
        return 0, json.dumps({"enabled": True, "allowed_actions": "all"}), ""  # live: enabled → drift

    monkeypatch.setattr(github_common_mod, "_gh_api", fake)
    res = runner._do_provision_github_actions(_actions_action(tmp_path, actions_enabled=False), "backup")
    assert res.status == "updated" and "disabled Actions" in res.detail
    assert any(p.endswith("/actions/permissions") for p in puts)
    assert not any(p.endswith("/permissions/workflow") for p in puts)  # the workflow PUT was skipped


def test_actions_disabled_converges_and_is_idempotent(monkeypatch, gh_repo, tmp_path):
    """Review-finding (round 7) #1: with `actions_enabled: false`, the classifier must IGNORE the
    workflow-token endpoint (apply skips its PUT) — else it reads perpetual drift and apply reports
    `updated` forever. Live Actions already disabled → state `ok`, a re-apply is a true no-op."""
    wf_reads: list = []

    def fake(args, *, input_text=None):
        if "--method" in args:
            return 0, "{}", ""
        if args[0].endswith("/permissions/workflow"):
            wf_reads.append(args[0])
            return 0, json.dumps({"default_workflow_permissions": "write", "can_approve_pull_request_reviews": True}), ""
        return 0, json.dumps({"enabled": False}), ""  # Actions already disabled (matches desired)

    monkeypatch.setattr(github_common_mod, "_gh_api", fake)
    res = runner._do_provision_github_actions(_actions_action(tmp_path, actions_enabled=False), "backup")
    assert res.status == "skipped"  # converged — NOT a phantom `updated`
    assert not wf_reads  # the workflow endpoint was never even read (mirrors the apply skip)


def test_actions_drift_parity(monkeypatch, gh_repo, tmp_path):
    """apply and drift switch on the SAME github_actions_state — a write token reads as drift."""
    def fake(args, *, input_text=None):
        if args[0].endswith("/workflow"):
            return 0, json.dumps({"default_workflow_permissions": "write", "can_approve_pull_request_reviews": True}), ""
        return 0, json.dumps({"enabled": True, "allowed_actions": "all"}), ""

    monkeypatch.setattr(github_common_mod, "_gh_api", fake)
    plan_obj = type("P", (), {"actions": [_actions_action(tmp_path)]})()
    report = detect(plan_obj)  # type: ignore[arg-type]
    actions_items = [i for i in report.items if i.item == "actions"]
    assert actions_items and actions_items[0].direction == "modified"


def test_actions_no_remote_and_gh_error(monkeypatch, tmp_path):
    """no remote → skipped; a failed read → loud error (not a silent green)."""
    monkeypatch.setattr(github_common_mod, "github_owner_repo", lambda repo_root: None)
    assert runner._do_provision_github_actions(_actions_action(tmp_path), "backup").status == "skipped"
    monkeypatch.setattr(github_common_mod, "github_owner_repo", lambda repo_root: ("acme", "widget"))
    monkeypatch.setattr(github_common_mod, "ensure_gh_auth", lambda **kw: github_auth.AuthOutcome("ok"))
    monkeypatch.setattr(github_common_mod, "_gh_api", lambda args, *, input_text=None: (1, "", "HTTP 403: forbidden"))
    res = runner._do_provision_github_actions(_actions_action(tmp_path), "backup")
    assert res.status == "error" and "403" in res.detail

