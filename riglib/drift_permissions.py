"""Drift checks for the harness permissions allow/deny/ask lists (missing, revoked, ordering, extras)."""

from __future__ import annotations

import json
from pathlib import Path

from .actions.harness_mode import (
    SELF_MERGE_LEGACY_ALLOW,
    SELF_MERGE_PERMISSIONS_ALLOW,
    SELF_MERGE_PERMISSIONS_DENY,
)
from .actions.permissions_specs import desired_permission_specs, permissions_settings_file
from .plan import Action
from .provenance import KIND_DISABLED_BASELINE, KIND_RETIRED_DEFAULT, KIND_USER_EXTRA
from .drift_model import DriftItem, DriftReport, KnownItem


def _check_permissions(action: Action, report: DriftReport, *, self_merge_owned: bool = False) -> None:
    """Flag drift between the configured permissions layer and the harness settings file.

    ``self_merge_owned`` is True when an active self-merge harness action writes the ship rules into
    THIS same settings file — only then are those rig-owned rules suppressed from the allow extras.

    One action spans EVERY container rig manages (rig-cli#100): the allow list plus, for
    claude-code, the deny/ask rule baselines. Per container:

    missing  — the settings file is absent, the container is absent, OR a desired entry is not
               present in it. ``rig apply`` ADDS the missing entries (additive merge — never
               removing the user's own).
    modified — the settings file is malformed JSON, OR (object form) a desired entry's KEY is
               present but its value is not ``"allow"`` (the user set it to ``ask``/``deny`` — that
               is the user's call, surfaced so status isn't a silent green, but apply does NOT
               downgrade it).
    extra    — an entry in the container BEYOND the rig-managed baseline. Hand-edits to the
               permissions lists must be visible drift, not silent config rot — and rig deletes
               NONE of them, with exactly ONE exception: a ``gh``/git-hosting default allow entry
               rig itself receipted as its own grant, which an explicit ``allow_gh: false`` /
               ``disable: [gh]`` DOES revoke (:func:`_check_permission_revocation`, rig-cli#333) —
               see :func:`_report_permission_extras` for the allow-vs-deny/ask reporting shape of
               everything else.

    Shares :func:`desired_permission_specs` with the install handler so apply and status never
    diverge on what rig manages.
    """
    specs = desired_permission_specs(action)
    config_file = permissions_settings_file(action)
    if not config_file.is_file():
        report.items.append(
            DriftItem("missing", "permissions", action.item, config_file, "harness settings file not written")
        )
        return
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
    except ValueError:
        report.items.append(
            DriftItem("modified", "permissions", action.item, config_file, "harness settings file is malformed JSON")
        )
        return
    # A non-object root is a SHAPE problem apply ERRORS on — report `modified` (not a stream of
    # `missing` entries), so status and apply agree on what the operator must fix.
    if not isinstance(data, dict):
        report.items.append(
            DriftItem("modified", "permissions", action.item, config_file, "harness settings file is not a JSON object")
        )
        return
    # Two passes on purpose: apply errors out on the FIRST shape problem and fixes NOTHING in the
    # file, so when ANY container is mis-shaped the per-entry "apply adds it" claims would be false
    # — report only the shape `modified` items then (status and apply agree on what to fix first).
    resolved: list[tuple[object, object]] = []
    shape_errors: list[str] = []
    for ps in specs:
        node, err = _resolve_permission_container(data, ps)
        if err is not None:
            # dedup by detail: a non-dict `permissions` node yields the SAME error for every
            # container spec — apply raises it once, so status reports it once too
            if err not in shape_errors:
                shape_errors.append(err)
        else:
            resolved.append((ps, node))
    if shape_errors:
        for err in shape_errors:
            report.items.append(DriftItem("modified", "permissions", action.item, config_file, err))
        return
    # Per-spec missing/modified (each spec owns its own entries — no overlap even when specs share
    # a container, since a role's keys never appear in a sibling role's desired set).
    for ps, node in resolved:
        _check_permission_entries(action, ps, node, config_file, report)
    # Ordering: opencode's shared permission.bash object is last-match-wins, so a deny/ask key that
    # sits BEFORE some other key (rig's own allow, or the user's) is a live security gap even
    # though every entry is individually present/correct — the per-entry check above cannot see it.
    _check_permission_ordering(action, resolved, config_file, report)
    # A receipt-tracked default grant rig itself made but no longer desires (`allow_gh: false`
    # after `gh` joined the default set) is ACTIONABLE drift — apply revokes it — not a summarized
    # "your additions, kept" line. Compute it BEFORE the extras pass so those entries can be
    # excluded from the "kept" bucket below (no double reporting of the same entry).
    stale_granted = _check_permission_revocation(action, resolved, config_file, report)
    # Extras are reported ONCE PER CONTAINER, not per spec: opencode folds allow + deny + ask into
    # the SAME permission.bash object, so (a) a rig-written deny key must be computed against the
    # UNION of all roles' desired entries or it looks like an allow "extra", and (b) a genuine user
    # extra must not be reported once per sharing spec. For claude-code each role has its own
    # key_path, so a container has exactly one spec — the union is that spec's entries (unchanged).
    _report_container_extras(
        action, resolved, config_file, report, self_merge_owned=self_merge_owned, exclude=stale_granted,
    )


def _check_permission_revocation(
    action: Action, resolved: list[tuple[object, object]], config_file: Path, report: DriftReport,
) -> frozenset[str]:
    """Drift for a receipt-tracked default allow grant that is no longer desired AND still
    physically present (apply has something real to revoke).

    rig-cli#333 review finding: setting ``allow_gh: false`` after ``gh`` joined the default tool
    set only removed ``gh`` from the DESIRED list — the additive merge never deletes anything on
    its own, so the entry stayed authorized and was merely reported as an "extra" (kept), which
    is a real security gap for a raw GitHub-write tool. Apply now revokes an entry it can PROVE it
    granted itself (:func:`riglib.actions.permissions._revoke_stale_default_grants`, via the sidecar
    receipt written by :func:`riglib.actions.permissions._sync_permissions_receipt`) — this is the
    status-side mirror, so a stale grant reads as real, actionable drift instead of a clean/kept
    line, and apply/status can never diverge on it (the same rig-cli#333 parity requirement as the
    ordering check above).

    Round-2 review finding: "stale" used to mean "receipted but not in this apply's resolved tool
    list" — but that is ALSO exactly what an unrelated ``permissions.tools: [tg]`` override
    produces on a repo that never typed ``allow_gh``/``disable: [gh]`` at all, on the SAME
    machine-wide shared settings file a sibling repo already granted `gh` in. Mirrors the writer's
    :func:`riglib.actions.permissions._permission_revocation_context`: only the EXPLICIT opt-out signal
    (:func:`riglib.actions.permissions_receipt.explicit_git_hosting_revocation_entries`) is ever a revocation
    trigger, MINUS whatever the allow spec's own entries still want present (a raw
    ``permissions.allow`` re-asserting the exact same entry wins over the toggle).

    Round-2 review finding: a receipted entry the USER already removed by hand (present in the
    receipt, absent from the file) must NOT be reported as "apply revokes it" — apply has nothing
    left to revoke there; it would just quietly clean the stale receipt entry and return
    ``skipped``. Reporting it as `extra` drift would be a false non-clean status with a false
    remediation claim. So this checks LIVE presence in the allow container (mirroring the writer's
    own revocation-eligibility check — array: membership; object: value still equals rig's own)
    before ever reporting an entry, using the SAME parsed ``node`` the rest of ``_check_permissions``
    already resolved (never re-reading the file).

    Returns the stale-AND-present entry set so the extras pass can exclude it from the "kept"
    bucket — it is reported here instead, not twice.
    """
    from .actions.permissions_receipt import (
        explicit_git_hosting_revocation_entries,
        permissions_receipt_file,
        read_permissions_grant_receipt,
        receipt_granted_entries,
    )

    kind = str(action.options.get("kind", "claude-code"))
    receipt = read_permissions_grant_receipt(permissions_receipt_file(action))
    granted = receipt_granted_entries(receipt, kind)
    allow_spec, allow_node = next(((ps, node) for ps, node in resolved if ps.role == "allow"), (None, None))
    if allow_spec is None:
        return frozenset()
    explicit_revoke = explicit_git_hosting_revocation_entries(action)
    stale = (granted & explicit_revoke) - frozenset(allow_spec.entries)
    if not stale:
        return frozenset()
    stale_present = _stale_present_entries(stale, allow_spec, allow_node)
    for entry in sorted(stale_present):
        report.items.append(DriftItem(
            "extra", "permissions", action.item, config_file,
            f"'{entry}' is a rig-granted default no longer desired (apply revokes it)",
        ))
    return frozenset(stale_present)


def _stale_present_entries(stale: frozenset[str], allow_spec, allow_node: object) -> set[str]:
    if allow_spec.container == "array":
        present = set(_present_container_keys(allow_spec, allow_node))
        return {e for e in stale if e in present}
    node = allow_node if isinstance(allow_node, dict) else {}
    return {e for e in stale if node.get(e) == allow_spec.value}


def _check_permission_ordering(
    action: Action, resolved: list[tuple[object, object]], config_file: Path, report: DriftReport,
) -> None:
    """Flag drift when a desired deny/ask key sits BEFORE some other key in an object-form
    container (opencode's shared ``permission.bash`` — claude-code's array containers have no
    ordering concept, so this is a no-op there).

    rig-cli#333 review finding: ``run_plan`` already re-anchors a misordered deny/ask key to the
    tail on every apply (:func:`riglib.actions.permissions_specs._merge_permission_container`), but
    ``_check_permissions`` only ever compared key membership and values — never order — so on an
    upgraded/hand-grown file where ``gh pr merge*: deny`` already sits before ``gh *: allow`` (or
    before ANY other key, rig's own or the user's), ``rig status`` reported that insecure
    pre-repair state as clean. Since opencode is last-match-wins, the later key silently wins
    until an apply is actually committed.

    Runs the SAME re-anchor helper apply uses (:func:`riglib.actions.permissions_specs._reanchor_rule_keys`)
    on a throwaway ``dict`` COPY of the container — never the parsed data drift must not mutate —
    so "would apply change the key order" can never diverge between apply and status.
    """
    from .actions.permissions_specs import _reanchor_rule_keys

    by_container: dict[tuple[str, ...], list[tuple[object, object]]] = {}
    for ps, node in resolved:
        if ps.container == "object":
            by_container.setdefault(ps.key_path, []).append((ps, node))
    for key_path, pairs in by_container.items():
        node = pairs[0][1]
        if not isinstance(node, dict) or not node:
            continue
        # Compare the NET key order across every role's reanchor pass, not each pass's own
        # before/after — a deny-only pass run against a probe that ALREADY has ask keys sitting
        # after it (the fully-converged, correctly-ordered state) moves deny past them on its own,
        # which the following ask pass then undoes; that intermediate churn is not real drift, only
        # the OVERALL before-vs-after (mirroring the write-decision the runner itself makes) is.
        before_order = list(node.keys())
        probe = dict(node)
        for ps, _ in pairs:
            if ps.role in ("deny", "ask"):
                _reanchor_rule_keys(probe, ps.entries)
        if list(probe.keys()) != before_order:
            dotted = ".".join(key_path)
            report.items.append(DriftItem(
                "modified", "permissions", action.item, config_file,
                f"{dotted}: a deny/ask key precedes another key — opencode is last-match-wins "
                "(apply re-anchors rig's deny/ask keys to the end on every apply)",
            ))


def _report_container_extras(
    action: Action, resolved: list[tuple[object, object]], config_file: Path, report: DriftReport,
    *, self_merge_owned: bool = False, exclude: frozenset[str] = frozenset(),
) -> None:
    """One extras pass per container (see :func:`_check_permissions`).

    Groups the resolved (spec, node) pairs by container key_path, computes the extras (present
    entries not in the UNION of every sharing role's desired entries), and reports them under a
    single representative spec — preferring the ``allow`` role so a shared object's extras use the
    summarized allow shape rather than a per-entry deny/ask dump.

    ``self_merge_owned`` suppresses the rig-OWNED self-merge ship rules from the ``allow`` extras
    (they are written by apply_harness into this same file under an active self-merge — not user
    cruft). Only applied when a self-merge action actually owns this file; a stray ship rule under
    self_merge:false (or a different permissions target) still reports.

    ``exclude`` drops entries :func:`_check_permission_revocation` already reported as ACTIONABLE
    drift (a stale receipt-tracked default grant, ``extra``/apply-revokes-it) — those must not
    ALSO show up here as a summarized "kept" line."""
    by_container: dict[tuple[str, ...], list[tuple[object, object]]] = {}
    for ps, node in resolved:
        by_container.setdefault(ps.key_path, []).append((ps, node))
    for pairs in by_container.values():
        rep_spec, extras = _container_extras(pairs, self_merge_owned, exclude)
        _report_permission_extras(action, rep_spec, extras, config_file, report)


def _container_extras(
    pairs: list[tuple[object, object]], self_merge_owned: bool, exclude: frozenset[str],
) -> tuple[object, list]:
    specs = [ps for ps, _ in pairs]
    rep_spec = next((ps for ps in specs if ps.role == "allow"), specs[0])
    present = _present_container_keys(rep_spec, pairs[0][1])
    union: set[str] = set()
    for ps in specs:
        union.update(ps.entries)
    extras = [k for k in present if k not in union and k not in exclude]
    return rep_spec, _drop_self_merge_owned(rep_spec.role, extras, self_merge_owned)


def _present_container_keys(rep_spec, node: object) -> list:
    if rep_spec.container == "array":
        return [e for e in (node if isinstance(node, list) else []) if isinstance(e, str)]
    return list(node) if isinstance(node, dict) else []


def _drop_self_merge_owned(role: str, extras: list, self_merge_owned: bool) -> list:
    if role == "allow" and self_merge_owned:
        return [e for e in extras if e not in (*SELF_MERGE_PERMISSIONS_ALLOW, *SELF_MERGE_LEGACY_ALLOW)]
    if role == "deny" and self_merge_owned:
        return [e for e in extras if e not in SELF_MERGE_PERMISSIONS_DENY]
    return extras


def _resolve_permission_container(data: dict, ps) -> tuple[object, str | None]:
    """Walk (read-only) to ``ps``'s container → ``(node, shape_error)``.

    ``shape_error`` is the `modified` detail when an INTERMEDIATE segment exists as a non-object
    or the container itself has the wrong type — the same problems apply hard-errors on (it can't
    create a container under a scalar / merge into a wrong-typed one). An ABSENT intermediate or
    container is fine (``node=None`` — apply creates it), never a shape error.
    """
    dotted = ".".join(ps.key_path)
    node: object = data
    for seg in ps.key_path[:-1]:
        nxt = node.get(seg) if isinstance(node, dict) else None
        if nxt is not None and not isinstance(nxt, dict):
            # container-AGNOSTIC wording on purpose: a scalar `permissions` node blocks every
            # container, and naming one container per spec would emit 3 items for 1 problem —
            # the caller dedups identical details, so this reports once (matching apply's one error)
            return None, f"'{seg}' in the harness settings is not an object"
        node = nxt
        if node is None:
            return None, None
    node = node.get(ps.key_path[-1]) if isinstance(node, dict) else None
    return node, _container_shape_error(ps, node, dotted)


def _container_shape_error(ps, node: object, dotted: str) -> str | None:
    if node is None:
        return None
    if ps.container == "array" and not isinstance(node, list):
        return f"{dotted} is not an array"
    if ps.container == "object" and not isinstance(node, dict):
        return f"{dotted} is not an object"
    return None


def _check_permission_entries(action: Action, ps, node, config_file: Path, report: DriftReport) -> None:
    """Per-entry MISSING/MODIFIED drift for ONE well-shaped container (extras are reported once per
    container by :func:`_report_container_extras`, since opencode shares permission.bash across
    roles)."""
    dotted = ".".join(ps.key_path)
    if ps.container == "array":
        _check_array_entries(action, ps, node, dotted, config_file, report)
    else:
        _check_object_entries(action, ps, node, dotted, config_file, report)


def _check_array_entries(action: Action, ps, node, dotted: str, config_file: Path, report: DriftReport) -> None:
    raw_list = node if isinstance(node, list) else []
    junk = [e for e in raw_list if not isinstance(e, str)]
    if junk:
        # apply works AROUND non-string junk (string-only membership) but never removes it —
        # surface it, or status would look clean over a hand-mangled list (apply/status parity)
        report.items.append(
            DriftItem("modified", "permissions", action.item, config_file,
                      f"{dotted} contains {len(junk)} non-string entr{'y' if len(junk) == 1 else 'ies'} "
                      "(hand-edit — apply ignores them, prune by hand)")
        )
    present = {e for e in raw_list if isinstance(e, str)}
    for entry in ps.entries:
        if entry not in present:
            report.items.append(
                DriftItem("missing", "permissions", action.item, config_file,
                          f"'{entry}' not in {dotted} (apply adds it)")
            )


def _check_object_entries(action: Action, ps, node, dotted: str, config_file: Path, report: DriftReport) -> None:
    existing = node if isinstance(node, dict) else {}
    for entry in ps.entries:
        if entry not in existing:
            report.items.append(
                DriftItem("missing", "permissions", action.item, config_file,
                          f"'{entry}' not in {dotted} (apply adds it)")
            )
        elif existing.get(entry) != ps.value:
            report.items.append(
                DriftItem("modified", "permissions", action.item, config_file,
                          f"'{entry}' in {dotted} is {existing.get(entry)!r}, not {ps.value!r} "
                          "(user override — apply leaves it)")
            )


def _report_permission_extras(action: Action, ps, extras: list[str], config_file: Path, report: DriftReport) -> None:
    """Entries beyond the rig baseline — filed as KNOWN ("your additions, kept"), never deleted
    from here (rig-cli#100), and since rig-cli#357 no longer counted as drift either: rig writes
    nothing it does not count as its own, so an entry beyond the baseline is by construction the
    user's — or a rule rig itself once wrote and stopped asserting — a fact to show, not a problem
    to fix. The ONE entry class this function never even SEES is a receipted ``gh``/git-hosting
    default grant that is no longer desired — :func:`_check_permissions` reports and excludes it
    (``exclude=``) upstream, in :func:`_check_permission_revocation`, as actionable `extra` drift
    apply DOES revoke (rig-cli#333) — this function only ever renders the "kept, not touched" kind.
    Each entry names its origin:

    - a former rig default tool (:data:`riglib.permissions.RETIRED_DEFAULT_TOOLS`, rig-cli#41→#165),
    - a baseline deny/ask rule the config turned off (``deny: []`` / ``ask: []``),
    - anything else: a hand-added entry.

    Per entry, every role: the renderer groups a container's entries into one line (and caps a long
    allowlist), so the per-entry shape no longer risks drowning ``rig status``.

    Adversarial reading, on purpose: a rogue writer appending ``Bash(curl:*)`` to the allow list is
    NOT caught here — and was not before either: allow extras were one summarized count line
    (``153 entries beyond the baseline``), where one more entry is invisible. A rogue deny/ask
    entry only ever tightens the policy. The permission file's integrity is not a drift question;
    the baseline rig DOES assert (missing/modified baseline entries) stays drift above.
    """
    from .permissions import DEFAULT_RULES, RETIRED_DEFAULT_TOOLS, desired_entries, harness_supported

    if not extras:
        return
    dotted = ".".join(ps.key_path)
    # the harness KIND lives in the options; `action.item` is the display label, which a
    # claude-code fan-out target suffixes (`claude-code@account-1`) — never a rules key
    harness_kind = str(action.options.get("kind") or action.item)
    rules = DEFAULT_RULES.get(harness_kind, {})
    # Origin lookups are scoped to the container: an ARRAY container (claude-code) holds ONE role,
    # so only that role's baseline (and, for allow, the retired defaults) can explain an entry; an
    # OBJECT container (opencode's shared permission.bash) holds every role's keys, so all of them
    # can. Without the scoping a deny-list entry that merely spells an ask-baseline rule would be
    # mislabelled "baseline rule your config turned off" instead of a hand-added entry.
    roles = (ps.role,) if ps.container == "array" else tuple(rules)
    retired: set[str] = set()
    if harness_supported(harness_kind) and (ps.container != "array" or ps.role == "allow"):
        retired.update(desired_entries(harness_kind, list(RETIRED_DEFAULT_TOOLS)))
    baseline: set[str] = set()
    for role in roles:
        baseline.update(rules.get(role, ()))
    for entry in extras:
        if entry in retired:
            origin = KIND_RETIRED_DEFAULT
        elif entry in baseline:
            origin = KIND_DISABLED_BASELINE
        else:
            origin = KIND_USER_EXTRA
        report.known.append(
            KnownItem("permissions", entry, config_file, origin, entry, container=f"{harness_kind}:{dotted}")
        )
