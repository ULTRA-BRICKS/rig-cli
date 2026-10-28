"""Plan stage for per-harness command allowlists (``permissions:``) and the Codex execpolicy rules.

Split out of :mod:`riglib.plan`.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from .catalog import Catalog
from .claude_config_dirs import fan_out_item
from .config import LoadedConfig
from .harness_skills import codex_user_path as _codex_user_path
from .plan_guard import _build_pi_extension
from .plan_harness import _claude_settings_targets
from .plan_harness import _permissions_kinds
from .plan_mode import _autonomous_allow_rules
from .plan_mode import _resolve_permission_rules
from .plan_model import Action
from .plan_model import InstallPlan
from .plan_model import PlanError
from .plan_targets import _expand


def _resolved_permission_tools(p: dict) -> list[str]:
    """The effective ``permissions`` tool list, folding ``allow_gh: false`` into ``disable``.

    ``allow_gh`` is sugar over ``permissions.disable: [gh]`` (which already worked before ``gh``
    joined the default set) — a single, discoverable opt-out for the one default addition that is
    a raw git-hosting tool, without inventing a second delta mechanism alongside
    ``tools``/``extra``/``disable``. Folds in :data:`riglib.permissions.DEFAULT_GIT_HOSTING_TOOLS`
    (today just ``("gh",)``) rather than a hardcoded literal, so the two never drift; if that tuple
    ever grows, ``allow_gh`` covers every member of it, not just ``gh`` — rename the flag if a
    second member ever needs its OWN independent opt-out. Shared by both permissions call sites
    (the allowlist builder and the codex execpolicy builder) so the two surfaces can never resolve
    ``gh`` differently.
    """
    from .permissions import DEFAULT_GIT_HOSTING_TOOLS, resolve_tools

    tools_cfg = p.get("tools")
    disable = list(p.get("disable", []) or []) if isinstance(p.get("disable"), list) else []
    if p.get("allow_gh") is False:
        for tool in DEFAULT_GIT_HOSTING_TOOLS:
            if tool not in disable:
                disable.append(tool)
    return resolve_tools(
        list(tools_cfg) if isinstance(tools_cfg, list) else None,
        list(p.get("extra", []) or []) if isinstance(p.get("extra"), list) else [],
        disable,
    )


def _explicit_git_hosting_opt_out(p: dict) -> list[str]:
    """Git-hosting default tools (``gh``) the user EXPLICITLY, deliberately opted out of —
    ``allow_gh: false`` or naming the tool directly in ``permissions.disable`` — as opposed to a
    tool merely being ABSENT from the resolved set for an incidental reason (an explicit
    ``permissions.tools`` list that REPLACES the whole default set and simply never mentioned it).

    Review finding (rig-cli#333, round 2): the runner's receipt-tracked revocation used to trigger
    off ``gh`` being absent from :func:`_resolved_permission_tools`'s OUTPUT — but that list is
    also what a totally unrelated ``permissions.tools: [tg]`` override produces, on the SAME
    machine-wide shared settings file another repo already granted `gh` in. A repo that never
    typed ``allow_gh``/``disable: [gh]`` at all would then revoke a sibling repo's `gh` grant as a
    pure side effect of picking its own narrower tool list — never the user's actual intent. This
    is the ONE explicit signal the runner is allowed to revoke on: an unambiguous, by-name
    exclusion, never an incidental one. Carried on the ``provision_permissions``/execpolicy action
    as ``revoke_git_hosting`` so the runner stays config-pure (reads resolved options, never raw
    config) while still getting a signal distinct from mere tool-list absence.
    """
    from .permissions import DEFAULT_GIT_HOSTING_TOOLS

    disable = set(p.get("disable", []) or []) if isinstance(p.get("disable"), list) else set()
    if p.get("allow_gh") is False:
        disable |= set(DEFAULT_GIT_HOSTING_TOOLS)
    return [t for t in DEFAULT_GIT_HOSTING_TOOLS if t in disable]


def _explicit_tools_git_hosting(p: dict) -> list[str]:
    """Git-hosting default tools (``gh``) named in an EXPLICIT ``permissions.tools`` override.

    Review finding (rig-cli#333, round 8): an explicit ``permissions.tools: [gh]`` REPLACES the
    whole default set (:func:`riglib.permissions.resolve_tools`'s documented "an explicit tools
    REPLACES the default set" contract) — naming ``gh`` there is exactly as deliberate a user
    assertion as ``permissions.extra: [gh]`` or a raw ``permissions.allow`` entry, both of which
    are already documented as never receipted (rounds 6/7). But the runner's receipt writer only
    ever saw the resolved ``tools`` list and could not tell "gh is here because the user typed an
    explicit tools list naming it" apart from "gh is here because it's part of the untouched
    default set" — so a first apply with ``tools: [gh]`` got wrongly receipted as rig's own
    default grant, and a later, unrelated ``allow_gh: false``/``disable: [gh]`` would delete it.
    Only fires when ``permissions.tools`` is actually set (``None`` means "use the default set",
    which IS the default grant and stays receiptable).
    """
    from .permissions import DEFAULT_GIT_HOSTING_TOOLS

    tools_cfg = p.get("tools")
    if not isinstance(tools_cfg, list):
        return []
    return [t for t in DEFAULT_GIT_HOSTING_TOOLS if t in tools_cfg]


def _build_permissions(config: LoadedConfig, catalog: Catalog, plan: InstallPlan) -> None:
    """Plan the per-harness permissions provisioning (allow + deny/ask), unless ``enabled: false``.

    Default **ON** (like ``agents_md``/``github``/``tg_ctl``): an ABSENT or empty ``permissions:``
    block still provisions the allowlist with the default tool set AND the conservative deny/ask
    rule baselines (rig-cli#100 — the outer belt), so ``rig init`` on a clean machine gets both
    with no config at all.

    Everything is CONFIG-DRIVEN — ``permissions.tools`` (a list) REPLACES the default set;
    ``permissions.extra`` adds; ``permissions.disable`` removes; ``permissions.allow_gh: false``
    is sugar that folds ``gh`` into ``disable`` (see :func:`_resolved_permission_tools`);
    ``permissions.allow`` adds RAW rule entries on top of the tool-derived allowlist;
    ``permissions.deny``/``ask`` REPLACE the baked rule baselines. The action carries the RESOLVED
    lists (so the runner stays config-pure)
    and is keyed off ``harness.kind`` (exactly like the auto-mode write), targeting the SAME
    per-harness user-scope settings file. A harness whose kind has no additively-mergeable
    allowlist (codex, pi — see :mod:`riglib.permissions`) emits NO action and is recorded
    N/A; a note explains why so ``rig status`` isn't silently empty.
    """
    p = config.data.get("permissions")
    if p is None:
        p = {}
    if not isinstance(p, dict):
        return  # validate() already fail-closed on a non-mapping block
    if p.get("enabled") is False:
        return

    from .permissions import harness_supported

    tools = _resolved_permission_tools(p)
    kinds = _permissions_kinds(config, p)
    if p.get("settings_path") and len(kinds) > 1:
        kinds = _narrow_kinds_for_settings_path(kinds, plan)

    for kind in kinds:
        if not harness_supported(kind):
            _note_non_allowlist_harness(config, catalog, kind, p, plan, kinds)
            continue
        _plan_allowlist_actions(config, kind, p, tools, plan)


def _narrow_kinds_for_settings_path(kinds: list[str], plan: InstallPlan) -> list[str]:
    """Narrow ``kinds`` to ONE allowlist harness when ``settings_path`` names a single file."""
    from .permissions import harness_supported

    # settings_path picks ONE settings file, which is only meaningful for kinds that write a
    # config-array allowlist to it (HARNESS_ALLOWLISTS). A kind with an INDEPENDENT
    # provisioning mechanism (pi's extension, codex's execpolicy, omp's guard) never reads
    # settings_path at all, so it must never be dropped from `kinds` by this narrowing — doing
    # so used to silently skip pi's entire permission-guard extension whenever settings_path
    # was combined with e.g. `kinds: [claude-code, pi]`.
    allowlist_kinds = [kind for kind in kinds if harness_supported(kind)]
    if len(allowlist_kinds) > 1:
        keep = allowlist_kinds[0]
        skipped = [kind for kind in allowlist_kinds if kind != keep]
        kinds = [kind for kind in kinds if kind not in skipped]
        plan.notes.append(
            "permissions: settings_path override with multiple harnesses targets the first "
            f"supported harness only ({keep}); skipped {', '.join(skipped)}"
        )
    return kinds


def _note_non_allowlist_harness(
    config: LoadedConfig,
    catalog: Catalog,
    kind: str,
    p: dict,
    plan: InstallPlan,
    kinds: list[str],
) -> None:
    """Record how a kind WITHOUT a config-array allowlist gets its permissions (or why not)."""
    from .permissions import (
        HARNESS_ALLOWLIST_NA,
        HARNESS_PERMISSION_TIERS,
        TIER_LABELS,
        execpolicy_supported,
        guard_supported,
        instruction_policy_supported,
    )

    # No config-array allowlist to merge — but every known harness has an enforcement
    # TIER and the effect is delivered by another surface, so describe what IS
    # achieved (and its tier) rather than a bare "skipped". codex: execpolicy .rules +
    # hook bridge. omp: the generated guard extension + approval posture (planned
    # separately below). pi gets its permission belt via a provisioned EXTENSION
    # instead of a config allowlist — no longer a silent N/A skip. commandcode (and
    # any kind with neither mechanism): the advisory instruction policy, or N/A.
    tier = HARNESS_PERMISSION_TIERS.get(kind)
    tier_note = f" (tier {tier}: {TIER_LABELS[tier]})" if tier else ""
    if execpolicy_supported(kind):
        plan.notes.append(
            f"permissions: harness '{kind}' has no config allowlist{tier_note} — "
            "safe-command allow + coarse deny are provisioned via the execpolicy .rules "
            "block, and flag-position denies via the PreToolUse hook bridge"
        )
    elif guard_supported(kind):
        plan.notes.append(
            f"permissions: harness '{kind}' has no config allowlist{tier_note} — "
            "deny/ask are enforced by the generated guard extension (tool_call block) "
            "plus the declarative approval posture"
        )
    elif _build_pi_extension(config, catalog, kind, p, plan, kinds):
        # pi's REAL enforcement surface (the provisioned permission-guard extension) takes
        # precedence over the generic advisory instruction-policy branch below — checked
        # BEFORE instruction_policy_supported so pi never falls through to the weaker
        # advisory-only note while its own extension mechanism is available. _build_pi_extension
        # itself returns True (having already appended a note) even when the extension carrier
        # is missing from the catalog, so this is never silently skipped either way.
        return
    elif instruction_policy_supported(kind):
        plan.notes.append(
            f"permissions: harness '{kind}' has no enforcement mechanism{tier_note} — "
            "the deny/ask intent is provisioned as an advisory instruction-file policy "
            "block (not enforced at the execution layer)"
        )
    else:
        reason = HARNESS_ALLOWLIST_NA.get(kind, "no command-allowlist mechanism")
        plan.notes.append(
            f"permissions: skipped — harness '{kind}' has no allowlist to provision ({reason})"
        )


def _note_git_hosting_revoke(
    kind: str,
    revoke_git_hosting: list[str],
    allow_rules: list[str],
    settings_path: object,
    plan: InstallPlan,
) -> None:
    """Preview note for a git-hosting opt-out that may DELETE a live entry (rig-cli#333)."""
    # This action can now DELETE a live entry, not just add one (rig-cli#333) — the first
    # delete-capable behavior `provision_permissions` has ever had. The plan preview
    # otherwise renders an identical `permissions/{kind} -> {target}` line for a delete as
    # for a plain add, so make the revoke intent visible BEFORE `rig apply commit` mutates
    # anything (the "review before mutate" guarantee this repo's docs commit to).
    #
    # Preview/execution parity (review finding, rig-cli#333 round 9): the runner's own
    # self-contradiction guard (`_permission_revocation_context`) never revokes an entry a
    # raw `permissions.allow` rule reasserts — so if THIS SAME config also carries such a
    # rule for the git-hosting tool being opted out of, the note below must not overpromise
    # a delete that `rig apply commit` will, correctly, skip.
    from .permissions import desired_entries

    reasserted = frozenset(desired_entries(kind, revoke_git_hosting)) & frozenset(
        str(r) for r in allow_rules
    )
    if reasserted:
        plan.notes.append(
            f"permissions: {', '.join(revoke_git_hosting)} opted out for '{kind}', but a "
            f"raw `permissions.allow` entry explicitly re-asserts it — the opt-out is "
            f"NOT applied for that entry (an explicit raw assertion wins over the "
            f"default-toggle-driven revocation; see `allow_gh` in docs/config-schema.md)"
        )
    else:
        plan.notes.append(
            f"permissions: {', '.join(revoke_git_hosting)} opted out for '{kind}' — a "
            f"rig-granted default grant, if the sidecar receipt names one, will be REVOKED "
            f"from {settings_path} (a hand-added or pre-existing grant is left untouched)"
        )


def _permission_action_options(
    kind: str,
    p: dict,
    tools: list[str],
    rules: tuple[list[str], list[str], list[str]],
    revoke_git_hosting: list[str],
) -> dict:
    """Options dict carried by every ``provision_permissions`` action for ``kind``."""
    allow_rules, deny_rules, ask_rules = rules
    return {
        "kind": kind,
        "tools": tools,
        "allow_rules": allow_rules,
        "deny_rules": deny_rules,
        "ask_rules": ask_rules,
        "revoke_git_hosting": revoke_git_hosting,
        # `permissions.extra` (raw, pre-fold) — review finding, rig-cli#333 round 7:
        # `extra: [gh]` on top of an explicit `tools:` that would otherwise NOT include
        # `gh` is exactly as deliberate a user assertion as `permissions.allow`, and is
        # documented as never receipted; but the runner only sees the ALREADY-RESOLVED
        # `tools` list (rig-cli#100's "stay config-pure" invariant), which can't tell
        # "default-derived" from "extra-derived" gh apart. Carrying the raw list lets
        # the receipt writer exclude it without the runner ever reading raw config.
        "extra_tools": list(p.get("extra", []) or []) if isinstance(p.get("extra"), list) else [],
        # rig-cli#333 round 8: an explicit `permissions.tools: [gh]` override is just
        # as deliberate a user assertion as `extra`/raw `allow` — see
        # `_explicit_tools_git_hosting`'s docstring for the incident.
        "explicit_tools_git_hosting": _explicit_tools_git_hosting(p),
    }


def _plan_allowlist_actions(
    config: LoadedConfig, kind: str, p: dict, tools: list[str], plan: InstallPlan
) -> None:
    """Emit the ``provision_permissions`` action(s) for one allowlist-capable harness kind."""
    from .permissions import HARNESS_ALLOWLISTS

    allow_rules, deny_rules, ask_rules = _resolve_permission_rules(p, kind, plan)
    for entry in _autonomous_allow_rules(config, kind, plan):
        if entry not in allow_rules:
            allow_rules.append(entry)
    spec = HARNESS_ALLOWLISTS[kind]
    # An explicit settings_path wins only for the single targeted harness case above; otherwise
    # each harness uses its own documented per-machine settings file.
    settings_path = p.get("settings_path") or spec.settings_path
    primary = _expand(str(settings_path), config.repo_root)
    revoke_git_hosting = _explicit_git_hosting_opt_out(p)
    if revoke_git_hosting:
        _note_git_hosting_revoke(kind, revoke_git_hosting, allow_rules, settings_path, plan)
    options = _permission_action_options(
        kind, p, tools, (allow_rules, deny_rules, ask_rules), revoke_git_hosting
    )
    # one action per settings file a live `claude` may load (claude-rotate dirs, rig-cli#368)
    for target in _claude_settings_targets(config, kind, primary):
        plan.actions.append(
            Action(
                kind="provision_permissions",
                category="permissions",
                item=fan_out_item(kind, target),
                source=config.repo_root,  # no carrier in agent-tools; anchor on the repo
                target=target.path,
                options=dict(options),
            )
        )


def _build_execpolicy(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan the codex execpolicy .rules provisioning (allow + coarse deny), unless disabled.

    Part of the ``permissions`` feature (gated by the same ``permissions.enabled`` and, when set,
    ``permissions.kind`` selection). For each configured harness kind that supports an execpolicy
    surface (codex — see :data:`riglib.permissions.HARNESS_EXECPOLICY`), emit ONE
    ``provision_execpolicy`` action carrying the SAME resolved tool set the allowlist uses. codex
    has no config-array allowlist, so this is how its safe-command allow + coarse deny is delivered;
    flag-position denies stay in the PreToolUse hook bridge. A kind with no execpolicy surface emits
    nothing here (the allowlist builder already noted it).
    """
    from .permissions import HARNESS_EXECPOLICY, execpolicy_supported

    p = config.data.get("permissions")
    if p is None:
        p = {}
    if not isinstance(p, dict):
        return
    if p.get("enabled") is False:
        return

    tools = _resolved_permission_tools(p)
    for kind in _permissions_kinds(config, p):
        if not execpolicy_supported(kind):
            continue
        spec = HARNESS_EXECPOLICY[kind]
        # codex's rules root honors RIG_CODEX_HOME (same resolver as its hooks/skills/config
        # targets); other kinds fall back to the generic ~-expansion of the declared path. The
        # RIG_CODEX_HOME-aware root is joined with the SAME suffix declared in spec.rules_path
        # (relative to codex's default ~/.codex root) so the two never drift.
        rules_path = spec.rules_path
        if kind == "codex":
            codex_prefix = "~/.codex/"
            if not rules_path.startswith(codex_prefix):
                raise PlanError(
                    f"codex execpolicy rules_path must live under {codex_prefix!r}: {rules_path!r}"
                )
            suffix = rules_path[len(codex_prefix) :]
            # The suffix is a hardcoded spec constant today (never user input), but this is the
            # one seam where a future spec/config change could smuggle a `..`/absolute component
            # and escape RIG_CODEX_HOME (Codex review of #169). PurePosixPath normalizes `.`/`//`
            # but leaves `..` and a leading `/` visible for us to reject explicitly.
            suffix_path = PurePosixPath(suffix)
            if not suffix.strip() or suffix_path.is_absolute() or ".." in suffix_path.parts:
                raise PlanError(
                    f"codex execpolicy rules_path suffix must not escape RIG_CODEX_HOME: {suffix!r}"
                )
            rules_path = _codex_user_path(suffix)
        plan.actions.append(
            Action(
                kind="provision_execpolicy",
                category="permissions",
                item=kind,
                source=config.repo_root,  # no carrier in agent-tools; anchor on the repo
                target=_expand(rules_path, config.repo_root),
                options={"kind": kind, "tools": tools},
            )
        )
