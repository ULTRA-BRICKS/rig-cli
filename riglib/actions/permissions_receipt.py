"""Grant receipt sidecar helpers: which allow entries Rig itself granted, so it can revoke only those."""

from __future__ import annotations

import json
from pathlib import Path

from ..plan import Action
from .permissions_specs import permissions_settings_file


# ── receipt-tracked revocation of a rig-GRANTED default allow entry (rig-cli#333 review finding) ──
# The additive merge above never deletes anything — by design (rig-cli#100), a permission entry
# beyond the baseline is always the USER's (or a former rig default, or a baseline the config
# turned off), never something to silently remove. `allow_gh: false` breaks that assumption: it
# is an explicit, config-level REQUEST to revoke a specific default grant, and leaving the entry
# in place (merely reporting it as "kept") is a genuine security gap for a raw GitHub-write tool.
#
# The fix is NOT "rig deletes any allow entry it no longer desires" (that would also delete a
# user's own hand-typed `permissions.allow`/`extra` entries the moment they're removed from
# config, which rig-cli#100 explicitly rules out). It is narrower: a sidecar JSON receipt records
# exactly which ALLOW entries rig itself granted as part of the config-driven DEFAULT tool set
# (never `allow_rules`/raw config entries, which stay config-owned) — mirroring the omp approval
# action's `.rig-permissions-receipt.json` sidecar (:func:`approval_receipt_file` above), the
# established "rig's own receipt, not a marker inside the harness's file" pattern in this repo.
# Only an entry the receipt names is ever removed; anything else beyond the desired set — a hand
# add, or a grant that predates this feature and was therefore never receipted — is left alone and
# reported as a kept "extra", exactly as before. Forward-only tracking fully closes the stated gap
# for `gh`: it joins the default set in this SAME change, so no machine can have a rig-default
# ``Bash(gh:*)``/``gh *`` without a receipt from the moment this ships.
#
# Known residual gap (round-4 review, accepted, not fixed here): two concurrent `rig apply`
# invocations against the SAME shared settings file (e.g. two repos applied back-to-back on one
# machine, overlapping in time) can race — no action in this file locks the settings file or the
# receipt, so a revoking apply and a granting apply interleaved in just the wrong order could each
# read a stale pre-write snapshot and one write could clobber the other's intent. Spelled out for
# the revocation direction specifically (round-8 review): an opt-out apply (A) and an unrelated
# apply from a sibling repo sharing the same file (B, adding some OTHER tool) racing in the wrong
# order can have B's write — built from a snapshot taken before A's revoke — land LAST and silently
# restore the just-revoked `gh` grant, with no error and no visible signal that the opt-out was
# undone. This is a PRE-EXISTING characteristic of every additive-merge action here (nothing in
# this file has ever locked a settings file; a plain add already races the same way), not something
# the revocation feature introduces — `rig apply` is a single-machine, effectively-serial operator
# command, not a concurrent service. Locking the whole family would be a separate, much larger
# change; out of scope for this fix.
GRANT_RECEIPT_SUFFIX = ".rig-permissions-grants.json"


def permissions_receipt_file(action: Action) -> Path:
    """The sidecar receipt a ``provision_permissions`` action reads/writes (shared with drift).

    Named after the settings FILE itself (``<config_file>.rig-permissions-grants.json``), never a
    fixed name in its parent directory. Review finding (rig-cli#333, round 2): a fixed per-directory
    name let two DIFFERENTLY NAMED settings files sharing one directory (``permissions.settings_path``
    lets a repo point anywhere) share one receipt too — repo A's grant on ``~/.claude/a.json`` could
    then authorize revoking a hand-added, unrelated entry from ``~/.claude/b.json``. Suffixing the
    settings file's own name makes receipt identity 1:1 with settings-file identity; two different
    filenames in the same directory can never collide.

    Known residual gap (round-3 review, accepted, not fixed here): the identity is the RAW target
    path, never ``.resolve()``d — matching every OTHER action target in this file
    (:func:`riglib.plan._expand` only resolves a RELATIVE path; an absolute one, the common case
    for a per-machine settings path, is used as-is). Two `settings_path` values that are really the
    same file via a symlink/hard-link would get two receipts and could, in principle, disagree on
    ownership. This is a pre-existing, codebase-wide characteristic of path identity here (backups
    and drift comparisons share it), not something specific to the receipt — fixing it only for the
    receipt would make it the one canonicalized path in an otherwise-unresolved file.

    Known residual gap (round-6 review, accepted, not fixed here): identity by PATH says nothing
    about CONTENT continuity — if the settings file at this exact path is wholesale REPLACED by
    something outside rig's own apply (not a hand-edit of the existing file, a full swap) while
    this receipt is left in place untouched, and the replacement happens to independently contain
    a string that renders identically to the git-hosting default, a later revoke would delete that
    coincidentally-identical entry, believing it is the original grant. Detecting "this file was
    swapped, not edited" would need a content fingerprint the receipt tracks across every apply —
    a real design (and one a naive version of would itself break the "hand edits keep working"
    goal, since a LEGITIMATE hand edit changes the file's content too, indistinguishable from a
    swap by a bytes-changed test alone). Requires a genuinely unusual operational sequence (an
    external process replacing the settings file while never touching its adjacent, rig-owned
    receipt); left as a known, documented limitation rather than adding fingerprint machinery for
    it here.

    The SAME root cause (entry ownership is decided by rendered-string match against a receipt
    that outlives any single apply, never by a per-grant fingerprint) also covers a narrower and
    more plausible variant flagged in round 8: the user hand-removes just the rig-granted `gh`
    entry, then — in a separate hand edit, no `rig apply` in between — hand-re-adds the identical
    string back, without ever touching the receipt (only `rig apply` itself syncs it). The next
    apply's revocation logic cannot distinguish that from the original, still-live grant and would
    delete the user's re-added entry on a later `allow_gh: false`. This is the same accepted
    trade-off as the wholesale-replacement case above, reached by a more ordinary path (a targeted
    hand edit, not a full file swap) — fixing it needs the same per-grant fingerprint machinery,
    judged out of proportion to this change's scope; not fixed here.

    The filename also carries the harness ``kind`` (review finding, rig-cli#333 round 11): the
    round-2 fix separated receipts by settings-FILE identity, but two DIFFERENT harness kinds can
    still be configured to point their OWN, independent ``permissions.settings_path`` at the exact
    SAME file (the "multiple kinds + one explicit override" case where the plan itself picks only
    one kind and skips the rest is a different, single-repo scenario — this is two SEPARATE repos,
    each configuring only one kind, that happen to target one shared path). Without ``kind`` in the
    name, a later apply for the second kind would overwrite the first kind's receipt wholesale
    (:func:`_sync_permissions_receipt` always replaces the file's content), and ``receipt_granted_
    entries``'s kind-match guard would then see the WRONG kind and treat the first kind's real
    grant as unreceipted — the same fail-open (never fail-dangerous) shape as every gap above.

    No migration is needed for the filename rename itself: this entire receipt mechanism —
    including the pre-round-11 filename it replaces — is new IN THIS SAME, still-unmerged PR
    (rig-cli#333). It has never shipped, so no real machine can hold a receipt in the old
    (kind-less) shape to migrate away from; the round-11 diff simply lands the final filename
    before this ever reaches `main`.
    """
    settings_file = permissions_settings_file(action)
    kind = str(action.options.get("kind", "claude-code"))
    return settings_file.parent / f"{settings_file.name}.{kind}{GRANT_RECEIPT_SUFFIX}"


def read_permissions_grant_receipt(receipt_file: Path) -> dict:
    """rig's default-grant receipt as written (``{}`` when absent, unreadable or malformed — every
    entry then counts as not-rig-owned, so nothing is ever revoked from an unreadable receipt)."""
    try:
        loaded = json.loads(receipt_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _valid_git_hosting_entries(kind: str) -> frozenset[str]:
    """Every entry a receipt could LEGITIMATELY name for ``kind`` — the rendering of every member
    of :data:`riglib.permissions.DEFAULT_GIT_HOSTING_TOOLS`, regardless of what is desired THIS
    apply. The fixed universe :func:`receipt_granted_entries` clamps against."""
    from ..permissions import DEFAULT_GIT_HOSTING_TOOLS, desired_entries

    return frozenset(desired_entries(kind, list(DEFAULT_GIT_HOSTING_TOOLS)))


def _receipt_grant_owners(receipt: dict, kind: str) -> dict[str, str]:
    """The single, shared shape-dispatch + clamp :func:`receipt_granted_entries` and
    :func:`receipt_granted_owners` both build on (review finding, rig-cli#549): the two public
    functions must clamp IDENTICALLY — that agreement is a security invariant (an entry
    ``receipt_granted_entries`` reports as revocable must always be exactly one
    ``receipt_granted_owners`` also reports an owner for) — so the ``kind``-match,
    :func:`_valid_git_hosting_entries` clamp, and legacy-list-vs-current-dict shape dispatch live
    in exactly one place, never duplicated across the two callers.

    Accepts either shape ``granted`` has ever been written in: the legacy flat list (template 1 —
    real shipped receipts exist in this shape, predating any ownership tracking) or the current
    ``{entry: owner}`` map (template 2, rig-cli#549). A legacy list entry, or a dict entry whose
    owner value is missing/not a string (a hand-edited/adversarial receipt), maps to the ``""``
    sentinel — "owner unknown". ``""`` can never equal a real ``str(action.source)`` (a repo root
    is never the empty string), so an unknown-owner entry is fail-safe: never TRANSFERABLE via
    ``never_receipt`` by any apply (no apply can prove it already owned it), only ever removable
    by an explicit by-name revoke (:func:`explicit_git_hosting_revocation_entries`), which stays
    unscoped by owner.

    Known residual gap (rig-cli#549, accepted, not fixed here): every entry that predates this
    change ships as ``""``-owned and STAYS ``""``-owned forever, even once re-written to the
    template-2 shape — a kept prior entry's owner is carried forward verbatim
    (:func:`_sync_permissions_receipt`'s ``owners = {e: ctx.prior_owners[e] for e in kept_prior}``),
    so ``""`` never becomes anything else once recorded. This means the round-9 "an explicit same-repo
    redeclaration always clears a receipted entry from tracking" contract (rig-cli#333) does not
    apply to a pre-existing (legacy-shaped) grant: it stays revocable by ANY later `allow_gh:
    false`, even the same repo's own, once that repo has explicitly redeclared the tool by name.
    This is the unavoidable cost of closing rig-cli#549 for a receipt shape that was never
    ownership-aware in the first place — there is no way to tell, from a bare entry name alone,
    which repo originally earned a legacy grant, so treating it as "belongs to nobody" (revocable,
    never silently launder-able) is the only fail-safe choice. It self-heals going forward: once
    an entry is genuinely re-granted as a fresh default by some apply (``newly_added``), it is
    stamped with that apply's real owner and behaves normally from then on.
    """
    if receipt.get("kind") != kind:
        return {}
    granted = receipt.get("granted")
    valid = _valid_git_hosting_entries(kind)
    if isinstance(granted, dict):
        return _owners_from_dict(granted, valid)
    if isinstance(granted, list):
        return {e: "" for e in granted if isinstance(e, str) and e in valid}
    return {}


def _owners_from_dict(granted: dict, valid: frozenset[str]) -> dict[str, str]:
    """``{entry: owner}`` clamped to ``valid``; a non-string owner maps to the unknown-owner ``""``."""
    return {e: (o if isinstance(o, str) else "") for e, o in granted.items() if isinstance(e, str) and e in valid}


def receipt_granted_entries(receipt: dict, kind: str) -> frozenset[str]:
    """The allow entries the receipt says rig itself granted as DEFAULTS for this exact ``kind``.

    Scoped to a matching ``kind`` on purpose — a receipt is never trusted across a kind mismatch
    (a hand-edited or foreign receipt), fail-safe to "nothing is rig-owned". Also CLAMPED to
    :func:`_valid_git_hosting_entries` (review finding, rig-cli#333 round 2): a syntactically valid
    but corrupted/adversarial/hand-edited receipt (``{"granted": ["Bash(kubectl:*)"]}``) must never
    be able to authorize revoking anything outside the tiny, fixed git-hosting-default universe —
    defense in depth on top of the write path only ever recording valid entries in the first place.

    Accepts either shape ``granted`` has ever been written in — see :func:`_receipt_grant_owners`,
    which this and :func:`receipt_granted_owners` both build on so the two can never disagree on
    which entries are recognized. This function only ever reports entry NAMES, never ownership.
    """
    return frozenset(_receipt_grant_owners(receipt, kind))


def receipt_granted_owners(receipt: dict, kind: str) -> dict[str, str]:
    """The ``{entry: owner}`` map for every entry :func:`receipt_granted_entries` reports for
    ``kind`` — the repo identity (``str(action.source)``) the receipt credits with each grant, or
    ``""`` ("unknown") for a legacy-shaped or malformed entry. See :func:`_receipt_grant_owners`
    for the full shape/clamp/fail-safe contract this delegates to.
    """
    return _receipt_grant_owners(receipt, kind)


def desired_default_allow_entries(action: Action) -> frozenset[str]:
    """The git-hosting-default ALLOW entries this apply's resolved tool set actually wants —
    i.e. the rendering of whichever :data:`riglib.permissions.DEFAULT_GIT_HOSTING_TOOLS` members
    are in ``action.options["tools"]``. Used only for the POSITIVE/grant side of the receipt (did
    THIS apply just add the entry) — never for deciding what to revoke (see
    :func:`explicit_git_hosting_revocation_entries` for that; round-2 review finding: an entry
    merely being absent from one repo's resolved ``tools`` is not the same as an explicit opt-out,
    see that function's docstring for the full incident).
    """
    from ..permissions import DEFAULT_GIT_HOSTING_TOOLS, desired_entries

    kind = str(action.options.get("kind", "claude-code"))
    tools = [str(t) for t in action.options.get("tools", []) if str(t) in DEFAULT_GIT_HOSTING_TOOLS]
    return frozenset(desired_entries(kind, tools))


def explicit_git_hosting_revocation_entries(action: Action) -> frozenset[str]:
    """The allow entries this apply is EXPLICITLY, deliberately opting out of — the receipt's only
    valid revocation trigger.

    Review finding (rig-cli#333, round 2): revocation used to trigger off ``gh`` being absent from
    ``desired_default_allow_entries`` — but that is also exactly what an UNRELATED
    ``permissions.tools: [tg]`` override produces on a repo that never mentioned ``gh``/``allow_gh``
    at all. On the machine-wide shared settings file, that repo's apply would then revoke a sibling
    repo's `gh` grant as a pure side effect of picking its own narrower tool list. The plan resolves
    the ACTUAL by-name opt-out (:func:`riglib.plan._explicit_git_hosting_opt_out` — ``allow_gh:
    false`` or ``gh`` named directly in ``permissions.disable``) into ``action.options
    ["revoke_git_hosting"]``; this is the one signal the runner is ever allowed to revoke on.
    """
    from ..permissions import desired_entries

    kind = str(action.options.get("kind", "claude-code"))
    tools = [str(t) for t in action.options.get("revoke_git_hosting", [])]
    return frozenset(desired_entries(kind, tools))
