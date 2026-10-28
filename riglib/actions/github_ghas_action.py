"""``provision_github_ghas`` handler: GitHub Advanced Security toggles."""

from __future__ import annotations

from dataclasses import dataclass, field
import json

from ..github_ghas import (
    SUBRESOURCE_KNOBS,
    build_security_analysis_body,
    desired_code_scanning,
    desired_subresource,
    normalize_security_analysis,
)
from ..plan import Action
from . import github_common
from .result import ActionResult


# ── rig-managed GitHub Advanced Security (§5 github.ghas) ────────────────────────────────
# rig reconciles the repo's GHAS toggles — dependency graph + secret-scanning (+ push protection)
# via `security_and_analysis` on `PATCH /repos/{o}/{r}`, vuln-alerts + Dependabot security updates
# via their own `PUT/DELETE` sub-resources, and CodeQL default-setup via its own endpoint. The
# DESIRED knobs/bodies live in `riglib/github_ghas.py` (pure). CAPABILITY DEGRADE: code/secret
# scanning on a PRIVATE repo needs a GHAS-licensed plan; the API returns 403/422 there — that is a
# loud "could not enable (plan does not include GHAS)", NOT a crash and NOT a silent green. Free
# features (dep-graph / vuln-alerts / Dependabot) degrade independently so one unlicensed scanner
# never masks a successfully-toggled free feature.
def _gh_subresource_enabled(owner: str, repo: str, endpoint: str) -> bool | None:
    """Read whether a GHAS sub-resource (vuln-alerts / Dependabot fixes) is ON. None = couldn't tell.

    ``vulnerability-alerts`` is a presence endpoint: GET returns 204 when enabled, 404 when not (gh
    reports the 404 as a non-zero rc). ``automated-security-fixes`` GET returns a JSON object with an
    ``enabled`` field. We treat a clean 204/enabled as True and a 404 as False; any OTHER error
    (auth/permission/network) → None so the caller can tell "off" apart from "couldn't check" and
    never reports a confident in-sync behind a failed read.

    The "off" signal is keyed on the HTTP STATUS (``HTTP 404``), NOT a loose substring like
    "disabled" or "not found": gh prints the status as ``(HTTP 404)`` / ``HTTP 404:`` for the
    not-enabled case, whereas a 403/422 validation body could legitimately contain the word
    "disabled" and must NOT be misread as "the resource is off" (that would mask a real
    auth/permission failure as a confident False). So we match the 404 status token only; anything
    else with a non-zero rc is an honest "unknown" (None).
    """
    rc, out, err = github_common._gh_api([f"repos/{owner}/{repo}/{endpoint}"])
    if rc == 0:
        if not out.strip():
            return True  # 204 No Content (vulnerability-alerts enabled)
        try:
            obj = json.loads(out)
        except ValueError:
            return True  # a 2xx with a non-JSON body still means the resource is present/enabled
        return bool(obj.get("enabled", True)) if isinstance(obj, dict) else True
    blob = (err + out).lower()
    # Match the 404 STATUS token (gh: "(HTTP 404)" / "HTTP 404:"), not a bare "404" anywhere or the
    # word "disabled" — so a 422 body that happens to contain "disabled" isn't misread as "off".
    if "http 404" in blob or "status: 404" in blob:
        return False
    return None  # a real error (auth/permission/network) — unknown, not "off"


def _gh_code_scanning_state(owner: str, repo: str) -> str | None:
    """Read CodeQL default-setup's state (``configured``/``not-configured``), or None if unreadable.

    The endpoint is GHAS-plan-gated; on a repo whose plan doesn't include it the GET errors. We
    return the state on a clean read and None on any error — the caller treats None as "could not
    verify" (a loud unknown), never as a confident in-sync.
    """
    rc, out, err = github_common._gh_api([f"repos/{owner}/{repo}/code-scanning/default-setup"])
    if rc != 0:
        return None
    try:
        return str(json.loads(out).get("state", ""))
    except (ValueError, AttributeError):
        return None


def github_ghas_state(action: Action) -> tuple[str, dict]:
    """Classify the live-vs-desired GHAS settings — the one source apply + drift share.

    States: ``no_remote``, ``gh_error`` (the REPO read itself failed — no auth / repo gone, so
    nothing can be classified), ``update`` (a readable setting differs OR an individual scanner is
    unverifiable), ``ok`` (every managed setting was readable AND matches). The signal covers the
    repo-object ``security_and_analysis`` block, the vuln-alerts / Dependabot sub-resources, AND
    CodeQL default-setup — each via its own GET.

    A SINGLE unreadable SCANNER endpoint (a GHAS-plan-gated 403/422 on a private repo without the
    GHAS license) does NOT collapse the whole classification to ``gh_error`` — that would make rig
    refuse to apply the FREE features (dep-graph / vuln-alerts / Dependabot) just because one
    licensed scanner is unavailable, the exact opposite of the documented "free features applied
    independently" design. Instead each unverifiable scanner is recorded in ``info["unverifiable"]``
    and forces ``update`` (so a green status never masks it, and apply runs to degrade it loudly
    while still applying everything else). Only a failed REPO read — where we can't classify
    ANYTHING — is ``gh_error``. ``info`` carries owner/repo/desired, ``unverifiable`` (a list of
    "endpoint (detail)" notes), and on gh_error a ``detail``.
    """
    owner_repo = github_common.github_owner_repo(action.target)
    desired_sa = build_security_analysis_body(action.options)
    if owner_repo is None:
        return "no_remote", {"desired": desired_sa}
    owner, repo = owner_repo
    info: dict = {"owner": owner, "repo": repo, "desired": desired_sa, "unverifiable": []}
    rc, out, err = github_common._gh_api([f"repos/{owner}/{repo}"])
    if rc != 0:
        info["detail"] = err.strip() or out.strip() or f"gh api exited {rc}"
        return "gh_error", info
    try:
        repo_obj = json.loads(out)
    except ValueError:
        info["detail"] = "gh api returned non-JSON repo object"
        return "gh_error", info
    desired_norm = {f: n["status"] for f, n in desired_sa.items()}
    # Record the security_and_analysis-block drift SEPARATELY so apply can gate the repo PATCH on it
    # (a PATCH when only a sub-resource / CodeQL drifted is a no-op that also prints a misleading
    # "security_and_analysis converged"). The aggregate `drifted` still drives the overall state.
    sa_drifted = normalize_security_analysis(repo_obj) != desired_norm
    info["sa_drifted"] = sa_drifted
    drifted = sa_drifted
    # the sub-resources: an UNREADABLE one is recorded as unverifiable (forces `update` so status
    # surfaces it and apply degrades it loudly) — NOT a whole-block gh_error that would also block
    # the free features. Distinct from "read OK but off", which is plain drift.
    for knob in SUBRESOURCE_KNOBS:
        endpoint = "vulnerability-alerts" if knob == "vulnerability_alerts" else "automated-security-fixes"
        live = _gh_subresource_enabled(owner, repo, endpoint)
        if live is None:
            info["unverifiable"].append(f"{endpoint} (could not read — plan-gated / no admin / API error)")
            drifted = True
        elif live != desired_subresource(action.options, knob):
            drifted = True
    # CodeQL default-setup: unreadable on a plan that gates it is unverifiable (degraded), not a
    # whole-block gh_error — same reasoning as the sub-resources above.
    want_codeql = "configured" if desired_code_scanning(action.options) else "not-configured"
    live_codeql = _gh_code_scanning_state(owner, repo)
    if live_codeql is None:
        info["unverifiable"].append("code-scanning default-setup (could not read — plan-gated / no admin / API error)")
        drifted = True
    elif live_codeql != want_codeql:
        drifted = True
    return ("update", info) if drifted else ("ok", info)


def _do_provision_github_ghas(action: Action, on_conflict: str) -> ActionResult:
    """Provision the repo GHAS settings via ``gh api`` (PATCH + sub-resource PUT/DELETEs).

    No github remote → ``skipped``. The #4136.1 auth gate runs FIRST (before the live read), so an
    unauthenticated apply notifies + waits + resumes. ``RIG_GH_DRY_RUN`` computes what WOULD change
    but mutates nothing. A GHAS-licensed scanner unavailable on the repo's plan degrades to a loud
    note in the result detail and does NOT block the free features (dep-graph / vuln-alerts /
    Dependabot), never a crash.
    """
    _owner, _repo, early = github_common._github_action_preamble(action, "github-ghas")
    if early is not None:
        return early
    state, info = github_ghas_state(action)
    if state == "gh_error":
        return ActionResult(action, "error", f"github-ghas: {info.get('detail', 'gh api failed')}")
    owner, repo, desired_sa = info["owner"], info["repo"], info["desired"]

    if github_common._gh_dry_run():
        verb = "already matches" if state == "ok" else "would UPDATE"
        return ActionResult(action, "skipped" if state == "ok" else "updated",
                            f"github-ghas: RIG_GH_DRY_RUN — {verb} security settings on {owner}/{repo} (not mutated)")

    writes = _GhasWrites(degraded=list(info.get("unverifiable", [])))
    unverifiable: list[str] = info.get("unverifiable", [])
    # 1) security_and_analysis block (dep-graph + secret-scanning + push protection) via repo PATCH.
    # Gate on the SA-block's OWN drift (not the aggregate `state == update`): when only a sub-resource
    # or CodeQL drifted, the SA block already matches, so PATCHing it is a no-op that would also print
    # a misleading "security_and_analysis converged". `sa_drifted` is computed by the classifier.
    if info.get("sa_drifted"):
        _patch_security_analysis(owner, repo, desired_sa, writes)
    _reconcile_subresources(action, owner, repo, unverifiable, writes)
    _reconcile_code_scanning(action, owner, repo, unverifiable, writes)

    summary = "; ".join(writes.notes) or "GHAS settings reconciled"
    if writes.degraded:
        summary += " — DEGRADED: " + "; ".join(writes.degraded)
    # A genuine (non-plan-limit) failure on ANY sub-resource / CodeQL write is a HARD error — never
    # masked as "updated (degraded)", which automation checking `status != error` would read green.
    if writes.hard_errors:
        summary += " — FAILED: " + "; ".join(writes.hard_errors)
        return ActionResult(action, "error", f"github-ghas: {summary} on {owner}/{repo}")
    status = "skipped" if (state == "ok" and not writes.notes and not writes.degraded) else "updated"
    return ActionResult(action, status, f"github-ghas: {summary} on {owner}/{repo}")


@dataclass
class _GhasWrites:
    """Outcome accumulators for one apply.

    ``degraded`` is seeded up front with scanners that couldn't even be read (plan-gated on a
    private repo) — apply still runs the free features; these surface as DEGRADED. ``hard_errors``
    holds GENUINE (non-plan-limit) write failures: they make the whole action an ERROR at the end,
    so a real auth/permission failure is never a silent green."""

    notes: list[str] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    hard_errors: list[str] = field(default_factory=list)

    def record_failure(self, full: str, label: str) -> None:
        """A plan/feature limit is an acceptable DEGRADE; anything else is a HARD error that must
        NOT be masked as "updated (degraded)". Classify on the FULL string (callers truncate only
        the displayed ``label``) so a verbose gh prefix can't push the plan-limit phrase out."""
        (self.degraded if _looks_like_ghas_unlicensed(full) else self.hard_errors).append(label)


def _patch_security_analysis(owner: str, repo: str, desired_sa: dict, writes: _GhasWrites) -> None:
    rc, out, err = github_common._gh_api(
        ["--method", "PATCH", f"repos/{owner}/{repo}", "--input", "-"],
        input_text=json.dumps({"security_and_analysis": desired_sa}),
    )
    if rc == 0:
        writes.notes.append("security_and_analysis converged")
        return
    detail = err.strip() or out.strip()
    # A private repo without a GHAS-licensed plan → 403/422 on the scanners. Degrade loudly
    # rather than failing the whole apply. A GENUINE failure goes into hard_errors (final
    # status = error) but does NOT early-return — so the FREE features (vuln-alerts /
    # Dependabot / CodeQL) are still attempted, matching the "applied independently" design.
    if _looks_like_ghas_unlicensed(detail):
        writes.degraded.append(f"secret-scanning/dep-graph (plan does not include GHAS: {detail[:80]})")
    else:
        writes.hard_errors.append(f"security_and_analysis ({detail[:80]})")


def _reconcile_subresources(
    action: Action, owner: str, repo: str, unverifiable: list[str], writes: _GhasWrites,
) -> None:
    """2) vuln-alerts + Dependabot security updates — their own PUT/DELETE sub-resources. Read the
    live state first and skip the mutation when it already matches, so a re-apply is a true no-op
    (reports `skipped`, not a phantom `updated`) instead of blindly PUTting every run.

    When the classifier ALREADY found this endpoint unreadable (a read-only / non-admin token gets
    403 on the sub-resource while the repo GET itself passed), it is in ``unverifiable`` and thus
    already seeded into ``degraded``. Skip the mutation then — it would 403 the same way and
    append a SECOND, redundant degrade line for the same endpoint."""
    for knob in SUBRESOURCE_KNOBS:
        endpoint = "vulnerability-alerts" if knob == "vulnerability_alerts" else "automated-security-fixes"
        if any(u.startswith(endpoint) for u in unverifiable):
            continue  # unreadable per the classifier — already degraded; a write would just 403 again
        _sync_subresource(owner, repo, endpoint, desired_subresource(action.options, knob), writes)


def _sync_subresource(owner: str, repo: str, endpoint: str, want: bool, writes: _GhasWrites) -> None:
    if _gh_subresource_enabled(owner, repo, endpoint) == want:
        return  # already in the desired state — no mutation, no churn
    method = "PUT" if want else "DELETE"
    rc, out, err = github_common._gh_api(["--method", method, f"repos/{owner}/{repo}/{endpoint}"])
    if rc != 0:
        full = err.strip() or out.strip()
        writes.record_failure(full, f"{endpoint} ({full[:80]})")
    else:
        writes.notes.append(f"{endpoint} {'enabled' if want else 'disabled'}")


def _reconcile_code_scanning(
    action: Action, owner: str, repo: str, unverifiable: list[str], writes: _GhasWrites,
) -> None:
    """3) CodeQL default-setup — its own endpoint with a `{state: configured|not-configured}` body.
    Both directions are reconciled: `false` sends `not-configured` so a user can turn CodeQL OFF
    again, not just on. Read the current state first and PATCH only on a real difference, so a
    converged repo stays a no-op rather than reporting `updated` every run.

    When the classifier ALREADY found this endpoint unreadable (plan-gated on a private repo), it
    is in ``unverifiable`` and thus already in ``degraded`` — skip the doomed best-effort PATCH
    (it would 403 the same way and append a SECOND, redundant degrade line)."""
    if any("code-scanning" in u for u in unverifiable):
        return
    codeql_state = "configured" if desired_code_scanning(action.options) else "not-configured"
    if _gh_code_scanning_state(owner, repo) == codeql_state:
        return
    rc, out, err = github_common._gh_api(
        ["--method", "PATCH", f"repos/{owner}/{repo}/code-scanning/default-setup", "--input", "-"],
        input_text=json.dumps({"state": codeql_state}),
    )
    if rc != 0:
        full = err.strip() or out.strip()
        # CodeQL default-setup needs Actions enabled + GHAS — a 403 from a non-admin token must
        # surface as a hard error, not be swallowed as a silent green.
        writes.record_failure(full, f"code-scanning default-setup ({full[:120]})")
    else:
        writes.notes.append(f"code-scanning default-setup {codeql_state}")


def _looks_like_ghas_unlicensed(detail: str) -> bool:
    """Heuristic: does this gh error mean the repo's plan does not include GHAS (vs. a real failure)?

    A private repo without GitHub Advanced Security gets a 403/422 whose body mentions the missing
    feature/plan. We degrade loudly on THAT (it's a capability limit, not a rig bug), but a genuine
    auth/permission error — or a generic 422 validation/payload bug — must still surface as an
    error. So we match the PLAN/FEATURE WORDING only; a bare status code (403 / 422) is NOT enough,
    because that could equally be a no-admin token or a real request bug we want to see, not swallow.

    The phrases are deliberately SPECIFIC to a plan/feature limit. A bare ``"not available"`` is NOT
    matched on its own (a transient ``503 Service not available`` or a network ``… not available``
    would be mis-swallowed as a capability limit); we require it paired with a feature/plan word
    (``not available for``, ``feature is not available``) so only the real GHAS-not-on-this-plan body
    degrades, and a transient/service error stays a hard error.
    """
    low = detail.lower()
    # Phrases that unambiguously mean "this repo's PLAN doesn't include the feature". Deliberately
    # NOT "must be enabled for" — CodeQL on a repo with Actions OFF fails "Actions must be enabled
    # for default setup", which is a FIXABLE config collision (turn Actions on), not a plan limit, and
    # must surface as a real error, not a degrade.
    plain = ("advanced security", "not included in", "upgrade your plan", "ghas")
    if any(s in low for s in plain):
        return True
    # "not available" only counts when it's about a feature/repo plan limit, not a service outage
    return ("not available for" in low) or ("feature is not available" in low) or ("not available on" in low)
