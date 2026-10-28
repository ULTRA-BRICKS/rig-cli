"""``provision_permissions`` handler: merge, revoke stale grants, and sync the grant receipt."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import fsutil
from ..plan import Action
from .permissions_receipt import (
    desired_default_allow_entries,
    explicit_git_hosting_revocation_entries,
    permissions_receipt_file,
    read_permissions_grant_receipt,
    receipt_granted_owners,
)
from .permissions_specs import (
    PermissionSpec,
    _merge_permission_container,
    desired_permission_specs,
    permissions_settings_file,
)
from .result import ActionResult
from .shared import _should_backup


def _read_only_container(data: dict, key_path: tuple[str, ...]) -> tuple[dict | None, str | None]:
    """Read-only walk to the PARENT of a container — never creates missing intermediates (unlike
    :func:`_container_at`, which the writer uses and which scaffolds empty dicts as a side effect).
    Returns ``(None, None)`` when any segment along the way is absent or not a dict, so a caller
    can tell "nothing here to revoke from" without ever mutating ``data``."""
    cur = data
    for seg in key_path[:-1]:
        if not isinstance(cur, dict):
            return None, None
        cur = cur.get(seg)
        if cur is None:
            return None, None
    if not isinstance(cur, dict):
        return None, None
    return cur, key_path[-1]


def _revoke_stale_default_grants(
    data: dict, allow_spec: PermissionSpec, to_revoke: frozenset[str],
) -> list[str]:
    """Remove every entry in ``to_revoke`` that is STILL PRESENT with rig's own value in the allow
    container — never scaffolding a missing container (nothing to revoke from one that isn't
    there), never touching an entry the user has since repurposed (object form: a differing value
    means it is no longer rig's to take back). Returns the entries actually removed.

    Array form: removes at most ONE occurrence per revoked entry string (review finding, rig-cli#333
    round 5) — the receipt proves rig granted the entry ONCE, never that every occurrence of an
    identical string is rig's. A hand-duplicated array entry (``["Bash(gh:*)", ..., "Bash(gh:*)"]``,
    a JSON array tolerates duplicates even though rig's own additive merge never creates one) keeps
    its extra hand-added copy — only the one rig can prove it wrote is taken back.
    """
    if not to_revoke:
        return []
    parent, leaf = _read_only_container(data, allow_spec.key_path)
    if parent is None:
        return []
    existing = parent.get(leaf)
    if allow_spec.container == "array":
        if not isinstance(existing, list):
            return []
        removed, kept = _split_revoked_array(existing, to_revoke)
        if removed:
            parent[leaf] = kept
        return removed
    if not isinstance(existing, dict):
        return []
    removed = [e for e in existing if e in to_revoke and existing.get(e) == allow_spec.value]
    for entry in removed:
        del existing[entry]
    return removed


def _split_revoked_array(existing: list, to_revoke: frozenset[str]) -> tuple[list[str], list]:
    """Partition ``existing`` into (removed, kept), removing at most one occurrence per entry."""
    removed: list[str] = []
    kept: list = []
    removed_once: set[str] = set()
    for entry in existing:
        if isinstance(entry, str) and entry in to_revoke and entry not in removed_once:
            removed.append(entry)
            removed_once.add(entry)
        else:
            kept.append(entry)
    return removed, kept


def _default_allow_presence(data: dict, allow_spec: PermissionSpec) -> frozenset[str]:
    """Which allow-container KEYS are currently present (any value) — used only to tell which
    ``desired_default_allow_entries`` were ABSENT before this apply's merge (and are therefore
    newly granted by rig just now, hence receipt-eligible). Key membership only, matching what
    :func:`_merge_permission_container` itself treats as "already there" (it never touches an
    object-form entry whose value differs — see the comment on that branch)."""
    node, leaf = _read_only_container(data, allow_spec.key_path)
    existing = node.get(leaf) if node is not None else None
    if allow_spec.container == "array":
        return frozenset(e for e in existing if isinstance(e, str)) if isinstance(existing, list) else frozenset()
    return frozenset(existing) if isinstance(existing, dict) else frozenset()


@dataclass(frozen=True)
class _PermissionRevocationContext:
    """Everything :func:`_do_provision_permissions` needs to decide what (if anything) to revoke —
    computed once, read-only, before the settings file is even opened."""

    kind: str
    config_file: Path
    allow_spec: PermissionSpec
    receipt_file: Path
    desired_default_allow: frozenset[str]
    prior_granted: frozenset[str]
    to_revoke: frozenset[str]
    never_receipt: frozenset[str]
    owner: str
    prior_owners: dict[str, str]


def _permission_revocation_context(action: Action, specs: list[PermissionSpec]) -> _PermissionRevocationContext:
    """Resolve the receipt/revocation bookkeeping for one apply.

    ``to_revoke`` = entries the receipt says rig granted, that THIS apply explicitly opts out of
    (:func:`explicit_git_hosting_revocation_entries` — never mere absence from a resolved tool
    list, see that function's docstring), MINUS whatever the allow spec's own entries still want
    present. That last subtraction is a review finding (rig-cli#333 round 2): a self-contradicting
    config — ``allow_gh: false`` alongside an explicit ``permissions.allow: ['Bash(gh:*)']`` (or
    an ``extra: [gh]`` that ends up back in ``allow_spec.entries`` some other way) — must not
    revoke-then-immediately-re-add the same entry in one apply and report "revoked" over an entry
    that is, in fact, still there; an explicit raw assertion wins over the default-toggle-driven
    revocation.

    ``never_receipt`` is carried through to :func:`_sync_permissions_receipt` so it can exclude
    these entries from what gets receipted, however they happen to render — every USER-explicit
    way of asking for an entry, as opposed to rig's own config-driven default:

    - ``action.options["allow_rules"]`` — raw ``permissions.allow`` entries (review finding,
      rig-cli#333 round 6): ``permissions.allow: ['Bash(gh:*)']`` renders identically to the
      git-hosting default and, on a first apply before rig's own default merge would have added
      it, was ALSO "absent before" — but it is the user's explicit assertion, documented as never
      receipted.
    - ``action.options["extra_tools"]`` rendered through :func:`riglib.permissions.desired_entries`,
      restricted to :data:`riglib.permissions.DEFAULT_GIT_HOSTING_TOOLS` (review finding, rig-cli#333
      round 7): ``permissions.extra: [gh]`` on top of a ``tools:`` override that would NOT otherwise
      include ``gh`` is exactly as deliberate a user ask as raw ``allow`` — the SAME "looks newly
      added by the default merge" trap, just via a different config knob.
    - ``action.options["explicit_tools_git_hosting"]`` rendered the same way (review finding,
      rig-cli#333 round 8): an explicit ``permissions.tools: [gh]`` REPLACES the whole default set
      — naming ``gh`` there is exactly as deliberate a user ask as ``extra``/raw ``allow``, the
      SAME trap again via yet another config knob (see :func:`riglib.plan._explicit_tools_git_hosting`).
    """
    from ..permissions import DEFAULT_GIT_HOSTING_TOOLS, desired_entries

    kind = str(action.options.get("kind", "claude-code"))
    allow_spec = next((ps for ps in specs if ps.role == "allow"), None)
    if allow_spec is None:
        raise RuntimeError(
            f"provision_permissions for kind={kind!r} produced no allow-role PermissionSpec — "
            "desired_permission_specs() should always emit one for a supported harness kind"
        )
    receipt_file = permissions_receipt_file(action)
    receipt_data = read_permissions_grant_receipt(receipt_file)
    prior_owners = receipt_granted_owners(receipt_data, kind)
    prior_granted = frozenset(prior_owners)
    explicit_revoke = explicit_git_hosting_revocation_entries(action)
    to_revoke = (prior_granted & explicit_revoke) - frozenset(allow_spec.entries)
    never_receipt_tools = [
        str(t)
        for key in ("extra_tools", "explicit_tools_git_hosting")
        for t in action.options.get(key, [])
        if str(t) in DEFAULT_GIT_HOSTING_TOOLS
    ]
    never_receipt = frozenset(str(r) for r in action.options.get("allow_rules", [])) | frozenset(
        desired_entries(kind, never_receipt_tools)
    )
    return _PermissionRevocationContext(
        kind=kind,
        config_file=permissions_settings_file(action),
        allow_spec=allow_spec,
        receipt_file=receipt_file,
        desired_default_allow=desired_default_allow_entries(action),
        prior_granted=prior_granted,
        to_revoke=to_revoke,
        never_receipt=never_receipt,
        owner=str(action.source),
        prior_owners=prior_owners,
    )


def _merge_all_permission_specs(
    action: Action, specs: list[PermissionSpec], data: dict, total_desired: int, config_file: Path,
) -> tuple[list[str], int] | ActionResult:
    """Run every spec's merge — skipped entirely when NOTHING across every spec is desired
    (`total_desired == 0` can still hold with a non-empty deny/ask baseline alongside a disabled
    tool set; only the fully-empty case skips, matching :func:`_merge_permission_container`'s own
    scaffolding-avoidance comment). Returns ``(added_per_container, total_added)``, or an
    ``ActionResult`` error on a container shape conflict.
    """
    added_per_container: list[str] = []
    total_added = 0
    if total_desired == 0:
        return added_per_container, total_added
    for ps in specs:
        try:
            n = _merge_permission_container(data, ps)
        except ValueError as exc:
            return ActionResult(action, "error", f"permissions/{action.item}: {exc} in {config_file}")
        if n:
            added_per_container.append(f"{n} to {'.'.join(ps.key_path)}")
            total_added += n
    return added_per_container, total_added


def _describe_permission_changes(added_per_container: list[str], total_added: int, revoked: list[str]) -> list[str]:
    """The human-readable change list for the ActionResult detail (a fresh list — the caller's is
    left untouched)."""
    described = list(added_per_container)
    if total_added == 0 and not revoked:
        # Nothing NEW and nothing REVOKED, but the container changed shape anyway — the deny/ask
        # re-anchor moved an already-present key. Still a write: the caller only reaches here when
        # `after != before` already proved it.
        described.append("re-anchored deny/ask key order")
    if revoked:
        described.append(f"revoked {', '.join(sorted(revoked))} (no longer desired)")
    return described


def _maybe_backup_permissions_file(config_file: Path, on_conflict: str) -> str:
    """Back up the existing file under ``on_conflict=backup`` — "" if it didn't exist or the
    policy doesn't call for it (the malformed-file backup, a separate always-unless-skip case,
    is taken by the caller before this ever runs — see :func:`_do_provision_permissions`)."""
    if _should_backup(on_conflict) and config_file.is_file():
        bak = fsutil.backup_path(config_file)
        shutil.copy2(str(config_file), str(bak))
        return f" (backed up prior → {bak})"
    return ""


def _do_provision_permissions(action: Action, on_conflict: str) -> ActionResult:
    """Merge the per-harness permissions layer into the harness settings JSON — ADDITIVE, with
    receipt-tracked revocation of rig's own default git-hosting grant.

    One action reconciles EVERY container the plan resolved (rig-cli#100): the ``allow`` list
    (tool-derived entries + adopted raw entries) and, for claude-code, the ``deny``/``ask`` rule
    baselines. Every existing entry in every container is PRESERVED, the desired entries are
    MERGED IN, deduped, and a re-apply with the same config is a true no-op — rig only ever ADDS
    what is missing (see :func:`_merge_permission_container`). The ONE exception is a
    receipt-tracked default ``gh`` grant an EXPLICIT opt-out no longer wants
    (:func:`_permission_revocation_context`): rig itself put that entry there, so rig itself takes
    it back (:func:`_revoke_stale_default_grants`) — never an entry the receipt does not name.

    Backup-noted under ``on_conflict=backup`` when the file changes; ``skip`` leaves a malformed
    file untouched; a non-dict settings root or a non-array/object container is a hard error.
    """
    try:
        specs = desired_permission_specs(action)
    except ValueError as exc:
        return ActionResult(action, "error", f"permissions/{action.item}: {exc}")
    ctx = _permission_revocation_context(action, specs)

    # Handle the fully-empty, nothing-to-revoke-either action BEFORE touching the file at all
    # (rig-cli#333 review finding): with every list empty AND no stale rig-owned grant to take
    # back, there is nothing to do — no read, no parse, no backup. The OLD ordering parsed first
    # (which, against a MALFORMED file under `on_conflict=backup`, already copied it to a unique
    # backup and reset `data` to `{}`) and only afterwards noticed there was nothing to merge — the
    # empty-action skip fired AFTER the destructive side effect, so a malformed file got a fresh
    # spurious backup on every single re-apply forever. Bailing out here, before `config_file` is
    # even opened, keeps a truly empty action idempotent regardless of what is on disk.
    total_desired = sum(len(ps.entries) for ps in specs)
    if total_desired == 0 and not ctx.to_revoke:
        return ActionResult(
            action, "skipped",
            f"permissions/{action.item}: no desired entries to provision (empty tool + rule sets)",
        )

    ctx.config_file.parent.mkdir(parents=True, exist_ok=True)
    data, was_malformed, existed, early = _load_permissions_settings(action, ctx.config_file, on_conflict)
    if early is not None:
        return early

    # Snapshot BEFORE any mutation so the write decision is driven by whether `data` actually
    # CHANGED — not by "were any entries added" (the deny/ask re-anchor can reorder with zero new
    # entries) and not by "was anything revoked" (a revoke can find nothing left to remove).
    before = json.dumps(data, indent=2) + "\n"
    present_before = _default_allow_presence(data, ctx.allow_spec)
    revoked = _revoke_stale_default_grants(data, ctx.allow_spec, ctx.to_revoke)

    merge_result = _merge_all_permission_specs(action, specs, data, total_desired, ctx.config_file)
    if isinstance(merge_result, ActionResult):
        return merge_result
    added_per_container, total_added = merge_result

    after = json.dumps(data, indent=2) + "\n"
    if after == before:
        # Nothing to WRITE, but the receipt can still be out of date (e.g. a stale grant that is
        # no longer even present has nothing left to revoke, yet the receipt should stop naming
        # it). Safe to sync here unconditionally: no write to `config_file` happens in this
        # branch, so there is no failure window between "receipt says rig owns it" and "the file
        # actually reflects that".
        _sync_permissions_receipt(ctx, data, present_before, revoked)
        return ActionResult(
            action, "skipped",
            f"permissions/{action.item}: all {total_desired} entries already in {ctx.config_file}",
        )

    added_per_container = _describe_permission_changes(added_per_container, total_added, revoked)
    # The malformed-file backup is taken HERE — deferred all the way to the point a write is
    # actually about to happen — never eagerly inside `_load_permissions_settings` (review
    # finding, rig-cli#333 round 11): the original malformed bytes are still sitting untouched at
    # `ctx.config_file` (this function never deletes/replaces them before this point), so backing
    # up now is equivalent to backing up at parse time WHENEVER a write follows, but skips the
    # backup entirely on the "parsed fine as {}, nothing ended up changing" branches above (the
    # `total_desired == 0 and not ctx.to_revoke` bail-out, and the `after == before` no-op below a
    # revoke-only action against a malformed file that resets to `{}` and then finds nothing left
    # to revoke) — closing the SAME "spurious backup for an action that ends up writing nothing"
    # class the fully-empty-action guard above (finding #3) closes, just reached via the
    # revoke-only path that guard doesn't cover on its own.
    #
    # NEVER gated on `_should_backup` (review finding, rig-cli#333 round 13): `_should_backup` is
    # the WELL-FORMED-file predicate (`on_conflict == "backup"` only — "overwrite" deliberately
    # skips that ceremony for a file rig can already parse and safely re-merge into). A MALFORMED
    # file is different: rig is about to blow away content it never parsed and can't merge into, a
    # real, one-way information loss regardless of the well-formed-file policy — the round-11
    # refactor accidentally reused `_should_backup` here and silently dropped the always-back-up-
    # malformed-unless-skip guarantee for `on_conflict: overwrite`. No explicit `on_conflict !=
    # "skip"` check needed here: `was_malformed` can only be True at this point if
    # `_load_permissions_settings` did NOT already return its `on_conflict == "skip"` early result
    # above, so that case is already excluded.
    #
    # This exact spot has regressed three times across this branch's own history (a fresh
    # reviewer catching a subtly-wrong backup condition each time) — the guard below is a fail-
    # LOUD local check (an explicit exception, not `assert`, so it still fires under `-O`/
    # `PYTHONOPTIMIZE`) for the cross-function invariant the comment above only states in prose
    # (review finding, rig-cli#333 round 15), so a future edit to `_load_permissions_settings`'s
    # early-return logic that silently breaks the coupling trips a test immediately here, not a
    # quiet re-introduction of "malformed file backed up even under on_conflict=skip".
    if was_malformed:
        if on_conflict == "skip":
            raise RuntimeError(
                "was_malformed implies on_conflict != 'skip' — _load_permissions_settings should "
                "have short-circuited before this point"
            )
        bak = fsutil.backup_path(ctx.config_file)
        shutil.copy2(str(ctx.config_file), str(bak))
        backup_note = f" (backed up malformed config → {bak})"
    else:
        backup_note = _maybe_backup_permissions_file(ctx.config_file, on_conflict)
    # NEVER pass sort_keys=True here: opencode is last-match-wins, so its deny/ask keys in
    # permission.bash MUST serialize AFTER the allow keys / any user catch-all. Sorting (or otherwise
    # rebuilding the object) would silently reorder deny before allow and turn every deny into a
    # no-op — with no test failure unless the order assertion in test_opencode_deny_ask_written_
    # after_user_catch_all stays. Insertion order (the additive merge appends missing keys) is the
    # invariant; json.dumps preserves dict order, so leave it be. Reuse `after` (already the exact
    # bytes this same serialization would produce) rather than dumping a third time.
    ctx.config_file.write_text(after, encoding="utf-8")
    # Sync the receipt ONLY after the settings write above actually succeeded (review finding,
    # rig-cli#333 round 2): syncing it BEFORE the write let a failed write (e.g. a read-only file)
    # leave a receipt claiming rig granted an entry that was never actually written — a later
    # hand-added entry with the same name would then be wrongly treated as rig's own and revoked
    # on some future opt-out. `run_plan` turns a `write_text` exception into an "error"
    # ActionResult (never a crash), and this ordering guarantees the receipt is untouched when that
    # happens — it reflects reality, not intent.
    #
    # Residual gap (round-3 review, accepted, not fixed here): the INVERSE double-fault — the
    # settings write above succeeds (gh really is granted) but THIS receipt write then fails (its
    # own directory unwritable) — leaves gh granted with no receipt. A later `allow_gh: false` then
    # can't prove ownership and does not revoke it. This fails OPEN for a security control (gh
    # stays allowed), never fails DANGEROUS (nothing is ever wrongly deleted) — and it fails
    # VISIBLY: `rig status` still shows the un-receipted `Bash(gh:*)`/`gh *` as a kept "extra" (not
    # silently clean), so an operator auditing `gh` access sees it. Recovering automatically would
    # mean adopting a present-but-unreceipted entry into the receipt on some later apply — exactly
    # the pre-existing-grant adoption forward-only tracking deliberately rejects (see this file's
    # receipt module comment) — so this is a deliberate trade, not an oversight: a rare double
    # fault (two independent writes to two different files, one succeeding and one not) degrades
    # to "fix it by hand" rather than reopening the adoption hole for a common case.
    _sync_permissions_receipt(ctx, data, present_before, revoked)
    # "backed_up" when we preserved a prior copy; else "updated" if the file pre-existed (we merged
    # entries into it), or "created" if we wrote it fresh. Don't report "created" for a file we
    # actually modified under a non-backup policy (the review's misleading-status finding).
    status = "backed_up" if backup_note else ("updated" if existed else "created")
    return ActionResult(
        action, status,
        f"permissions/{action.item}: added {', '.join(added_per_container)} in {ctx.config_file}{backup_note}",
    )


def _load_permissions_settings(
    action: Action, config_file: Path, on_conflict: str,
) -> tuple[dict, bool, bool, ActionResult | None]:
    """Parse ``config_file`` for :func:`_do_provision_permissions`.

    Returns ``(data, was_malformed, existed, early_result)`` — when ``early_result`` is set the
    caller must return it immediately (a malformed file under ``on_conflict=skip``, or a non-dict
    root); ``data``/``was_malformed``/``existed`` are meaningless in that case.

    Deliberately does NOT back up a malformed file itself (review finding, rig-cli#333 round 11):
    it only reports ``was_malformed=True`` and resets ``data`` to ``{}`` for parsing purposes,
    leaving the original bytes on disk untouched. The caller takes the actual backup copy only
    once it knows a write is really about to happen — see the note at that call site.
    """
    data: dict = {}
    was_malformed = False
    existed = config_file.is_file()
    if existed:
        try:
            data = json.loads(config_file.read_text(encoding="utf-8"))
        except ValueError:
            if on_conflict == "skip":
                return {}, False, existed, ActionResult(
                    action, "skipped",
                    f"permissions/{action.item}: existing {config_file} is malformed JSON "
                    "(on_conflict=skip), left untouched",
                )
            was_malformed = True
            data = {}
    if not isinstance(data, dict):
        return {}, False, existed, ActionResult(
            action, "error", f"permissions/{action.item}: {config_file} is not a JSON object",
        )
    return data, was_malformed, existed, None


def _sync_permissions_receipt(
    ctx: _PermissionRevocationContext, data: dict, present_before: frozenset[str],
    revoked: list[str],
) -> None:
    """Write the sidecar receipt so it reflects what rig owns AFTER this apply.

    ``granted`` = (previously receipted entries whose ownership THIS apply doesn't just take back
    via revocation or clear away via a same-owner explicit redeclaration, and that are STILL
    ACTUALLY PRESENT after this apply) ∪ (entries THIS apply's own desired set just added, i.e.
    were ABSENT before this run's merge) — each entry paired with the repo identity
    (``str(action.source)``) that owns it (:func:`receipt_granted_owners`). Deliberately based on
    POST-MERGE PRESENCE (:func:`_default_allow_presence` on ``data``, which already reflects the
    revoke + merge this same call made), never on ``ctx.desired_default_allow`` alone (review
    finding, rig-cli#333 round 2): the settings file is MACHINE-WIDE, so an UNRELATED sibling
    repo's apply — one that never mentions ``gh``/``allow_gh`` at all, just a narrower
    ``permissions.tools`` — must not reset the receipt to "not granted" merely because ITS OWN
    resolved tool list happens to omit ``gh``; the entry is still there, untouched, so it stays
    receipted under its ORIGINAL owner. An entry that happens to match a rendering but was
    ALREADY present before this run and was NEVER receipted (a hand add, or a grant that predates
    this feature) is deliberately excluded from both sides — forward-only tracking, so only a
    grant rig can prove it made is ever revocable. Writes nothing when there is nothing to receipt
    (avoids a stray file on a machine that has never used the git-hosting default at all).

    ``newly_added`` also excludes ``ctx.never_receipt`` (review findings, rig-cli#333 rounds 6-7):
    an entry the user explicitly asked for by name — raw ``permissions.allow`` or
    ``permissions.extra`` — renders identically to the git-hosting default and, on a first apply
    before rig's own default merge would have added it, was ALSO "absent before" — but it is the
    user's explicit, config-owned assertion (documented as never receipted), not something rig can
    claim credit for granting.

    A PRIOR receipted entry only ever has its ownership CLEARED via ``ctx.never_receipt`` when
    THIS apply's own identity (``ctx.owner``) already matches who the receipt credits with it
    (rig-cli#549 review finding — see the ``transferable_prior`` comment below for the full
    incident this closes: on a machine-wide shared settings file, a DIFFERENT repo's own explicit
    declaration must never silently launder away this repo's receipted ownership). This keeps
    round-9's own scenario intact: the SAME repo redeclaring an entry it already owns (``tools:
    [gh]`` after an earlier plain-default grant) still transfers ownership to that explicit
    declaration, in every apply where the redeclaration is present.

    ``revoked`` (the entries :func:`_revoke_stale_default_grants` actually removed THIS apply) is
    also subtracted, unconditionally (review finding, rig-cli#333 round 13): array-form revocation
    removes AT MOST ONE occurrence per string, so a hand-DUPLICATED entry (rig's receipted copy
    plus an identical user-added copy) still shows up in ``present_after`` — the physical copy
    left behind is the user's, not rig's, but ``present_after`` can't tell the copies apart by
    string alone. Without this subtraction the receipt kept claiming the entry as granted after
    rig's own copy was already taken back, so a SECOND identical apply would revoke the user's
    surviving copy too — an idempotency violation (a re-apply of the same config must be a no-op)
    and a silent violation of the "only one rig-proven copy is ever revoked" contract on the second
    pass. Once rig has taken its copy back, it never again claims ownership of whatever remains.
    """
    present_after = _default_allow_presence(data, ctx.allow_spec)
    newly_added = (ctx.desired_default_allow - present_before) - ctx.never_receipt
    # Owner-scoped clearing (rig-cli#549): an explicit declaration only clears a PRIOR receipted
    # entry's ownership when THIS apply's identity already matches who the receipt credits with
    # it — never merely because this apply's OWN `never_receipt` happens to name the same
    # rendered string. The settings file is machine-wide: an unrelated sibling repo naming `gh`
    # via its own `extra`/`tools`/raw `allow` (even as a total no-op merge, since `gh` was already
    # present) must not silently launder away a DIFFERENT repo's rig-owned default grant — that
    # repo's later `allow_gh: false` must still find and revoke it. Round-9's own same-repo
    # redeclare-then-later-opt-out scenario is unaffected: there `ctx.owner` IS the receipt's
    # recorded owner for the entry, so the transfer still happens exactly as before. The actual
    # REVOKE decision (`to_revoke`, driven by an explicit by-name opt-out) stays UNSCOPED by
    # owner — computed before this function ever runs — so ANY repo's opt-out can still trigger
    # the real revocation of a grant owned by any other repo.
    transferable_prior = frozenset(e for e in ctx.never_receipt if ctx.prior_owners.get(e) == ctx.owner)
    kept_prior = ctx.prior_granted - transferable_prior
    entries = (kept_prior | newly_added) - frozenset(revoked)
    entries &= present_after
    if not entries and not ctx.receipt_file.exists():
        return
    owners = {e: ctx.prior_owners[e] for e in kept_prior}
    # Known residual gap (rig-cli#549 review finding, accepted, not fixed here): `newly_added`
    # unconditionally stamps `ctx.owner` — the CURRENT applying repo — onto every entry in it.
    # An entry can be in `ctx.prior_granted` (owned by repo A) yet ALSO in `newly_added` if it was
    # physically absent from the settings file just before this merge (e.g. hand-removed) and this
    # apply's own default merge re-adds it: ownership silently transfers A -> whichever repo's
    # apply happened to re-add it, with no by-name declaration involved. This mirrors the
    # pre-existing "forward-only tracking" trade-off this receipt has always made (an entry that
    # was present-but-unreceipted before a run is never adopted into the receipt either) — telling
    # "the original owner's grant, hand-removed and now restored" apart from "a genuinely new
    # grant that happens to render identically" is not possible from presence alone. Impact is
    # bounded: the actual REVOKE trigger (`to_revoke`, above) stays unscoped by owner regardless of
    # who this stamps as owner, so a real opt-out is never blocked by it either way.
    owners.update({e: ctx.owner for e in newly_added})
    granted = {e: owners[e] for e in sorted(entries)}
    content = json.dumps({"template": 2, "kind": ctx.kind, "granted": granted}, indent=2) + "\n"
    if ctx.receipt_file.is_file() and ctx.receipt_file.read_text(encoding="utf-8") == content:
        return  # already current — a true no-op re-apply must not even touch the receipt's mtime
    ctx.receipt_file.write_text(content, encoding="utf-8")
    ctx.receipt_file.chmod(0o600)
