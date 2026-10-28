"""Permission spec resolution and container merging for ``provision_permissions``."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..plan import Action


# ── permission-allowlist provisioning (claude-code permissions.allow / opencode permission.bash) ──
def permissions_settings_file(action: Action) -> Path:
    """The settings file a ``provision_permissions`` action targets (shared with drift).

    Mirrors :func:`harness_settings_file`: any suffixed target is the file itself, else a
    directory holding the per-harness settings filename. The plan resolves an absolute path;
    this just normalizes the file-vs-dir target.
    """
    target = action.target
    return target if target.suffix else target / "settings.json"


@dataclass(frozen=True)
class PermissionSpec:
    """One desired permissions container: which list it is, where it lives, what must be in it.

    ``role`` is ``"allow"``/``"deny"``/``"ask"`` (drift keys its extras-reporting style off it);
    ``container`` is ``"array"`` (claude-code) or ``"object"`` (opencode ``permission.bash``);
    ``value`` is the object-form value per entry (``"allow"``), ``None`` for arrays.
    """

    role: str
    key_path: tuple[str, ...]
    container: str  # "array" | "object"
    entries: tuple[str, ...]
    value: str | None


def desired_permission_specs(action: Action) -> list[PermissionSpec]:
    """Every permissions container this action reconciles, in a fixed order (allow, deny, ask).

    allow = the tool-derived allowlist entries + the config's raw ``allow_rules`` (the adopted
    hand-grown baseline), deduped. deny/ask = the plan-resolved rule lists, present only for
    harness kinds with VERIFIED rule containers (claude-code — see
    ``riglib.permissions.HARNESS_RULE_CONTAINERS``). Shared by the install handler and drift so
    apply and status can never diverge on what rig manages. Reads everything off the action
    options (the plan resolved them), so the runner stays pure of config.
    """
    from ..permissions import HARNESS_ALLOWLISTS, HARNESS_RULE_CONTAINERS, desired_entries

    kind = str(action.options.get("kind", "claude-code"))
    try:
        spec = HARNESS_ALLOWLISTS[kind]
    except KeyError:
        # The plan only emits supported kinds (it gates on harness_supported), so this is a
        # defensive guard — a stale/hand-built action with an N/A kind gets a clean error, not a
        # raw KeyError traceback out of the runner/drift.
        raise ValueError(f"no allowlist mechanism for harness kind {kind!r}") from None
    tools = [str(t) for t in action.options.get("tools", [])]
    # defensive copy — we append the raw allow_rules below, and mutating whatever
    # desired_entries returns would be fragile if it ever starts sharing/caching its list
    allow_entries = list(desired_entries(kind, tools))
    for raw in action.options.get("allow_rules", []) or []:
        if str(raw) not in allow_entries:
            allow_entries.append(str(raw))
    specs = [PermissionSpec("allow", spec.key_path, spec.container, tuple(allow_entries), spec.value)]
    containers = HARNESS_RULE_CONTAINERS.get(kind, {})
    # emit the deny/ask specs whenever the kind HAS the container — even with EMPTY entries
    # (config `deny: []`): rig still manages the container, so a previously-applied baseline
    # left on disk shows up as per-entry extras instead of silently vanishing from status
    # (codex review finding, rig-cli#100). The container SHAPE is per-harness (claude-code: two
    # dedicated arrays; opencode: the shared permission.bash object, values "deny"/"ask") — read it
    # off the RuleContainer so the merge writes the right shape. Allow is emitted FIRST so opencode's
    # denies land AFTER the allow keys in the object (last-match-wins ordering discipline).
    for role in ("deny", "ask"):
        rc = containers.get(role)
        if rc is None:
            continue
        rules = [str(r) for r in action.options.get(f"{role}_rules", []) or []]
        specs.append(PermissionSpec(role, rc.key_path, rc.container, tuple(rules), rc.value))
    return specs


def _container_at(data: dict, key_path: tuple[str, ...]) -> tuple[dict, str]:
    """Walk ``data`` to the PARENT of the allowlist container, creating intermediate dicts.

    Returns ``(parent_dict, leaf_key)`` so the caller can read/set ``parent[leaf_key]`` — the
    container itself (array/object) is created lazily by the caller so its shape is explicit.
    Raises ``ValueError`` if an intermediate exists as a NON-dict (we never clobber the user's
    differently-typed value silently — that surfaces as an action error).
    """
    cur = data
    for seg in key_path[:-1]:
        nxt = cur.get(seg)
        if nxt is None:
            nxt = {}
            cur[seg] = nxt
        elif not isinstance(nxt, dict):
            raise ValueError(f"'{'.'.join(key_path)}' parent '{seg}' is not an object")
        cur = nxt
    return cur, key_path[-1]


def _merge_permission_container(data: dict, ps: PermissionSpec) -> int:
    """Merge ONE desired container into ``data`` (ADDITIVE) — returns how many entries were added.

    - array form: append each missing entry, order-stable — the user's entries stay first.
    - object form: set each missing entry KEY → ``ps.value`` only when the key is absent (never
      downgrade a user's ``"deny"``/``"ask"`` override on an allow entry — that is the user's
      call, not rig's; drift surfaces it as ``modified`` instead). For deny/ask roles, rig's OWN
      desired entries are then re-anchored to the end of the object on every call (not just when
      newly added) — opencode is last-match-wins, so a deny/ask key merged in an EARLIER apply
      must never end up before an allow key merged in a LATER one.

    Raises ``ValueError`` on a shape mismatch (non-dict parent, non-array/non-object container);
    the caller turns that into an action error — never a blind overwrite of the user's data.
    """
    parent, leaf = _container_at(data, ps.key_path)
    dotted = ".".join(ps.key_path)
    existing = parent.get(leaf)
    if ps.container == "array":
        existing, added = _merge_array_entries(existing, ps, dotted)
    else:  # object form — entry KEY → value, only when the key is absent
        existing, added = _merge_object_entries(existing, ps, dotted)
    parent[leaf] = existing
    return added


def _merge_array_entries(existing: object, ps: PermissionSpec, dotted: str) -> tuple[list, int]:
    """Append each missing entry to the array container, order-stable; returns (array, added)."""
    if existing is None:
        existing = []
    if not isinstance(existing, list):
        raise ValueError(f"'{dotted}' is not an array")
    # membership over the STRING entries only — a hand-edited list can carry non-string
    # junk (an object would even make set(existing) raise TypeError). Junk is preserved
    # (rig never deletes) and drift reports it as `modified`; apply just works around it.
    present = {e for e in existing if isinstance(e, str)}
    added = 0
    for entry in ps.entries:
        if entry not in present:
            existing.append(entry)
            present.add(entry)
            added += 1
    return existing, added


def _merge_object_entries(existing: object, ps: PermissionSpec, dotted: str) -> tuple[dict, int]:
    """Set each missing entry KEY → ``ps.value`` in the object container; returns (object, added)."""
    if existing is None:
        existing = {}
    if not isinstance(existing, dict):
        raise ValueError(f"'{dotted}' is not an object")
    added = 0
    for entry in ps.entries:
        if entry not in existing:
            existing[entry] = ps.value
            added += 1
    if ps.role in ("deny", "ask"):
        # opencode is last-match-wins, and dict insertion order only grows at the tail — a
        # deny/ask key MERGED IN A PRIOR APPLY can already sit BEFORE an allow key that is
        # only newly merged in THIS apply (the exact shape rig-cli's gh default hit: `gh
        # pr merge*: deny` provisioned before `gh` joined the default tool set, `gh *:
        # allow` merged afterward → landed AFTER the existing deny key → later-key-wins
        # silently un-denied the merge — same shape for any machine that opted into `gh`
        # earlier via `permissions.extra: [gh]`, before it joined the default set). Re-anchor
        # every one of rig's OWN desired deny/ask entries (``ps.entries`` — never a user's
        # unrelated key) to the END of the container on EVERY apply, not just when freshly
        # added, so this invariant holds on a fresh write, on an upgrade, AND on a machine
        # that is ALREADY in the broken state (every desired key present, just misordered —
        # ``added`` alone would be 0 there too). This is pure reordering with no value
        # change, so the CALLER (``_do_provision_permissions``) is what decides whether it is
        # write-worthy — it compares the container's serialized bytes before/after the merge
        # loop rather than gating on ``added``, specifically so this repair is never silently
        # discarded by an "already added everything" skip. NB: this also means a user's own
        # key that sits AFTER one of rig's deny/ask entries — the only way to positionally
        # override a baked deny in opencode's last-match-wins dialect — gets re-anchored past
        # on the next apply too, defeating that override; the sanctioned way to relax a baked
        # rule is `permissions.deny: [...]` (replaces the baseline), not key position.
        _reanchor_rule_keys(existing, ps.entries)
    return existing, added


def _reanchor_rule_keys(container: dict, entries: tuple[str, ...]) -> bool:
    """Move every one of ``entries`` that is PRESENT in ``container`` to the tail, in ``entries``
    order — returns whether the key ORDER actually changed.

    The one place that knows what "healed" means for opencode's last-match-wins ``permission.bash``
    object. Shared by the writer (:func:`_merge_permission_container`, above) and by drift
    (:func:`riglib.drift._check_permission_ordering`, which runs it on a throwaway ``dict`` copy to
    detect the PRE-repair state without ever mutating the parsed file) — rig-cli#333 review finding:
    ``run_plan`` already healed a misordered deny/ask key, but ``_check_permissions`` only compared
    key membership and values, so ``rig status`` reported the insecure pre-repair state as clean.
    Sharing this exact function means apply and drift can never diverge on the definition of "in
    order" again.
    """
    before = list(container.keys())
    for entry in entries:
        if entry in container:
            container[entry] = container.pop(entry)
    return list(container.keys()) != before
