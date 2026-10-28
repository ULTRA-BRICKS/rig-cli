"""Plan stage for the harness guard policy (approval-mode, instruction policy) and the pi extension.

Split out of :mod:`riglib.plan`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from .catalog import Catalog
from .config import LoadedConfig
from .harness_mode import harness_auto_intent
from .harness_mode import mode_value_for
from .harness_mode import resolved_mode_value
from .harness_skills import instruction_file_for as _instruction_file_for
from .plan_harness import _enabled_harness_block
from .plan_harness import _permissions_kinds
from .plan_model import Action
from .plan_model import InstallPlan
from .plan_model import PlanError
from .plan_targets import _DEFAULT_HARNESS_KIND
from .plan_targets import _configured_harness_kinds
from .plan_targets import _expand


def _build_guard_policy(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan the guard-extension / approval-posture / advisory-policy provisioning (rig-cli#202).

    Part of the ``permissions`` feature (gated by the same ``permissions.enabled`` and, when
    set, ``permissions.kind`` selection). Three ATOMIC actions, one per surface — never an
    overloaded container:

    * ``install_harness_guard`` (omp) — the wholly rig-owned GENERATED TS guard extension
      (``<agent root>/extensions/<name>``), codegen'd from the single rule registry in
      :mod:`riglib.permissions` by :mod:`riglib.omp_guard`. deny blocks outright; ask
      confirm-gates with a UI, blocks headless.
    * ``provision_harness_approval`` (omp) — the declarative approval posture merged
      additively into the harness config (never a differing user value clobbered).
    * ``provision_instruction_policy`` (pi/commandcode) — the deny/ask INTENT as an
      ADVISORY managed block in the harness's global instruction file (truthfully tensed:
      not enforced at the execution layer).
    """
    p = config.data.get("permissions")
    if p is None:
        p = {}
    if not isinstance(p, dict):
        return
    if p.get("enabled") is False:
        # rig-managed permissions artifacts are left in place (rig never auto-deletes), but
        # a relaxed posture with the feature OFF must not be silent — note ONLY when there
        # is evidence something was actually provisioned (no noise on fresh machines).
        for kind in _permissions_kinds(config, p):
            if any(p_.exists() for p_ in _disabled_leftover_paths(config, kind)):
                plan.notes.append(
                    f"permissions: disabled — rig-managed guard/approval artifacts for '{kind}' "
                    "stay on disk (rig never auto-deletes); remove them by hand to fully relax"
                )
        return

    # the allowlist knobs are INERT for kinds without an allowlist surface (guard/advisory/
    # execpolicy) — say so instead of silently ignoring a user's custom rules.
    _inert_note_kinds = {"tools", "extra", "disable", "allow", "deny", "ask"}
    configured_knobs = sorted(k for k in _inert_note_kinds if p.get(k))
    for kind in _permissions_kinds(config, p):
        _plan_guard_policy_for_kind(config, plan, kind, configured_knobs)


def _disabled_leftover_paths(config: LoadedConfig, kind: str) -> list[Path]:
    """Every on-disk artifact path rig may have provisioned for ``kind``."""
    from .permissions import HARNESS_APPROVAL, guard_extension_path_for

    leftovers: list[Path] = []
    if kind in HARNESS_APPROVAL:
        leftovers.append(_expand(guard_extension_path_for(kind), config.repo_root))
        leftovers.append(
            _expand(f"{HARNESS_APPROVAL[kind].root()}/{HARNESS_APPROVAL[kind].config_name}",
                    config.repo_root)
        )
    leftovers.extend(_instruction_leftover_paths(config, kind))
    if kind == "pi":
        leftovers.extend(_pi_extension_leftover_paths(config, kind))
    return leftovers


def _instruction_leftover_paths(config: LoadedConfig, kind: str) -> list[Path]:
    from .harness_skills import LEGACY_INSTRUCTION_FILES
    from .permissions import HARNESS_INSTRUCTION_POLICY

    leftovers: list[Path] = []
    if kind in HARNESS_INSTRUCTION_POLICY:
        # route through the DYNAMIC resolver (honors PI_CODING_AGENT_DIR for pi), not the
        # static HARNESS_INSTRUCTION_POLICY[kind] snapshot — see the identical fix + note
        # at the actual provisioning site below.
        instr_target = _instruction_file_for(kind)
        if instr_target:
            leftovers.append(_expand(instr_target, config.repo_root))
        # ALSO check known-historical locations that differ from the resolved one: a
        # machine that ran an OLDER rig-cli can carry a stale managed block at a location
        # this release no longer targets. Checking only the current dynamic target would
        # miss it: the leftover note (and thus the visible "still active" warning)
        # silently disappears, even though the legacy file remains a LIVE managed block
        # pi would read again the moment nothing overrides it.
        legacy_candidates = {HARNESS_INSTRUCTION_POLICY.get(kind), LEGACY_INSTRUCTION_FILES.get(kind)}
        for legacy_target in legacy_candidates:
            if legacy_target and legacy_target != instr_target:
                leftovers.append(_expand(legacy_target, config.repo_root))
    return leftovers


def _pi_extension_leftover_paths(config: LoadedConfig, kind: str) -> list[Path]:
    # The extension + JSON policy file are pi's OTHER enforcement artifacts
    # (_build_pi_extension below) — the disabled-state check must cover them too, or
    # a machine with a live guard extension but a since-deleted/never-provisioned
    # AGENTS.md advisory block gets no leftover warning at all even though pi still
    # auto-loads and enforces the extension.
    from .harness_skills import pi_user_path as _pi_user_path
    from .permissions import harness_permission_extension

    ext_name = harness_permission_extension(kind)
    if not ext_name:
        return []
    dynamic_ext = _expand(_pi_user_path(f"extensions/{ext_name}"), config.repo_root)
    dynamic_policy = _expand(_pi_user_path("rig-permission-policy.json"), config.repo_root)
    leftovers = [dynamic_ext, dynamic_policy]
    # ALSO check the static default root (~/.pi/agent), mirroring the AGENTS.md
    # legacy-candidate treatment above: provision at the default root, THEN set
    # PI_CODING_AGENT_DIR to move pi elsewhere, and the dynamic-only check above
    # would only look at the NEW root — missing a still-live guard extension +
    # policy file left behind at the OLD (default) root.
    static_ext = _expand(f"~/.pi/agent/extensions/{ext_name}", config.repo_root)
    static_policy = _expand("~/.pi/agent/rig-permission-policy.json", config.repo_root)
    if static_ext != dynamic_ext:
        leftovers.append(static_ext)
    if static_policy != dynamic_policy:
        leftovers.append(static_policy)
    return leftovers


def _plan_guard_policy_for_kind(
    config: LoadedConfig, plan: InstallPlan, kind: str, configured_knobs: list[str]
) -> None:
    from .permissions import (
        approval_supported,
        guard_supported,
        harness_permission_extension,
        instruction_policy_supported,
    )

    guard_target = _plan_guard_action(config, plan, kind) if guard_supported(kind) else None
    # A kind with its OWN permission extension (pi) gets specific, accurate drop notes from
    # `_build_pi_extension` for these same keys (its structured rule dialect vs. these raw
    # claude-code-style strings) — this generic "no allowlist surface" note would otherwise
    # duplicate it and, for deny/ask specifically, contradict it (pi DOES have an enforcement
    # surface; the strings just don't translate into it).
    if (
        (guard_supported(kind) or instruction_policy_supported(kind))
        and not harness_permission_extension(kind)
        and configured_knobs
    ):
        plan.notes.append(
            f"permissions: {', '.join(configured_knobs)} "
            f"{'is' if len(configured_knobs) == 1 else 'are'} inert for harness '{kind}' "
            "(no allowlist surface — the rules come from the baked baseline registry)"
        )
    if approval_supported(kind):
        _plan_approval_action(config, plan, kind, guard_target)
    if instruction_policy_supported(kind):
        _plan_instruction_policy_action(config, plan, kind)


def _plan_guard_action(config: LoadedConfig, plan: InstallPlan, kind: str) -> Path:
    from .permissions import guard_extension_path_for

    guard_target = _expand(guard_extension_path_for(kind), config.repo_root)
    plan.actions.append(
        Action(
            kind="install_harness_guard",
            category="permissions",
            item=kind,
            source=config.repo_root,  # no carrier in agent-tools; anchor on the repo
            target=guard_target,
            options={"kind": kind},
        )
    )
    return guard_target


def _plan_approval_action(
    config: LoadedConfig, plan: InstallPlan, kind: str, guard_target: Path | None
) -> None:
    from .permissions import HARNESS_APPROVAL

    # The approval posture relaxes the harness to auto-approve — it may ONLY land
    # when the guard belt for the same kind is in place (the runner re-verifies the
    # guard target's content before writing; see the action handler).
    options: dict[str, Any] = {"kind": kind, "mode_value": _approval_mode_value(config, kind)}
    if guard_target is not None:
        options["guard_target"] = str(guard_target)
    plan.actions.append(
        Action(
            kind="provision_harness_approval",
            category="permissions",
            item=kind,
            source=config.repo_root,
            target=_expand(
                f"{HARNESS_APPROVAL[kind].root()}/{HARNESS_APPROVAL[kind].config_name}",
                config.repo_root,
            ),
            options=options,
        )
    )


def _plan_instruction_policy_action(config: LoadedConfig, plan: InstallPlan, kind: str) -> None:
    # DYNAMIC resolver, not the static HARNESS_INSTRUCTION_POLICY[kind] dict value: the
    # dict is a module-import-time snapshot that never re-derives, so for "pi" specifically
    # it silently ignores a PI_CODING_AGENT_DIR override at plan-build time — the SAME
    # override the pi permission-guard extension (_build_pi_extension, via pi_user_path)
    # already honors. Without this, the advisory instruction-policy block would be written
    # to the default ~/.pi/agent/AGENTS.md even when pi is actually rooted elsewhere,
    # silently invisible to the running pi instance.
    instr_target = _instruction_file_for(kind)
    if instr_target is None:
        # `instruction_policy_supported(kind)` guards this branch, so a `None` here means
        # the two registries have fallen out of sync — an internal invariant, not a user
        # config error. `assert` would vanish under `python -O`; raise explicitly so this
        # production invariant is checked unconditionally (fail-explicit on IO/state).
        raise PlanError(f"instruction_policy_supported({kind!r}) but no instruction file")
    plan.actions.append(
        Action(
            kind="provision_instruction_policy",
            category="permissions",
            item=kind,
            source=config.repo_root,
            target=_expand(instr_target, config.repo_root),
            options={"kind": kind},
        )
    )


def _approval_mode_value(config: LoadedConfig, kind: str) -> str:
    """The approval-posture value for ``kind`` — follows the harness auto intent when the harness
    block configures ``kind``; otherwise (no block, ``enabled: false``, or ``kind`` not among the
    configured harness kinds) the legacy relaxed posture (yolo, rig-cli#202 owner decision) — a
    foreign harness's ``auto_mode: false`` must not flip a kind it never declared."""
    h = _enabled_harness_block(config)
    if h is None or kind not in _configured_harness_kinds(config):
        return mode_value_for(kind, None, legacy_intent=True)
    primary = str(h.get("kind") or _DEFAULT_HARNESS_KIND)
    intent = harness_auto_intent(h, primary)
    # a primary omp's own pinned mode (``write``) is honoured verbatim, like every primary kind
    return resolved_mode_value(kind, primary, h.get("mode"), intent, legacy_intent=True)


def _reject_unenforceable_pi_override(overridden_roles: list[str], kinds: list[str]) -> None:
    """Raise when a deny/ask override can be consumed by no configured permissions target."""
    from .permissions import CONFIG_RULE_DIALECT_KINDS

    override_enforced_nowhere = not any(k in CONFIG_RULE_DIALECT_KINDS for k in kinds)
    if not (overridden_roles and override_enforced_nowhere):
        return
    # Report EVERY offending role in one error, not just the first — a config with both
    # deny and ask set would otherwise make the user fix one, rerun, and only then discover
    # the other is equally broken. One `plural` flag drives every pronoun/verb choice below
    # so they can't drift out of agreement with each other (as a "them"-vs-"it" split once did).
    plural = len(overridden_roles) > 1
    roles = "/".join(f"permissions.{r}" for r in overridden_roles)
    it_them = "them" if plural else "it"
    raise PlanError(
        f"{roles} {'are' if plural else 'is'} set, but no configured "
        f"permissions target ({', '.join(kinds)}) can consume {it_them} — pi's policy rules "
        "are argvAll/flagsAny dicts, not claude-code's Bash(...) rule strings, and none of the "
        f"other configured kinds use that dialect either, so this override would be accepted "
        f"then enforced nowhere. Remove {roles} to keep pi's baked baseline, or set {it_them} "
        f"to [] to explicitly disable {it_them} (rig has no durable way to add a custom pi "
        "rule through config yet)."
    )


def _note_pi_dropped_allowlist_keys(plan: InstallPlan, p: dict[str, Any], kind: str) -> None:
    # tools/extra/disable/allow are the config-allowlist keys — pi has no additively-mergeable
    # allowlist (see harness_permission_extension), so they're inert for this kind. Note it,
    # rather than a silent no-op, so a user setting them for pi gets visible feedback.
    dropped_allowlist_keys = [k for k in ("tools", "extra", "disable", "allow") if p.get(k) is not None]
    if dropped_allowlist_keys:
        plan.notes.append(
            f"permissions: {'/'.join(dropped_allowlist_keys)} ignored for harness '{kind}' — pi has "
            "no additively-mergeable command allowlist (only deny/ask policy rules)"
        )


def _build_pi_extension(
    config: LoadedConfig,
    catalog: Catalog,
    kind: str,
    p: dict[str, Any],
    plan: InstallPlan,
    kinds: list[str],
) -> bool:
    """Emit the pi ``permission-guard`` extension provisioning, if ``kind`` uses that path.

    Returns True when it handled ``kind`` (an action was emitted OR a note explains why it could
    not), so the caller skips the generic N/A note. pi's permissions are delivered by installing
    the extension into pi's ``extensions/`` dir and writing the rig-owned policy file it reads —
    the SAME baseline (deny gh-pr-merge / force-push / --no-verify / sudo-rm / screencapture; ask
    pkill / killall / git reset --hard) the claude-code allowlist encodes, in the extension's
    flag-anywhere dialect. Idempotent + backup-on-conflict + drift are handled in the runner.

    ``kinds`` is the FULL resolved list of harness kinds ``permissions`` targets this apply (not
    just ``kind``): a non-empty ``deny``/``ask`` override is claude-code's rule-string dialect
    (``Bash(x:*)``), which doesn't translate into pi's structured argvAll/flagsAny rule dicts, so
    it can only take effect when claude-code (the one kind in ``CONFIG_RULE_DIALECT_KINDS``) is
    ALSO among ``kinds`` — pi plus any other non-claude-code target (opencode, codex, omp,
    commandcode) is equally dead, not just pi alone. When no configured target can consume it, the
    override is a config-time :class:`PlanError`, not a silently-dropped plan note.
    """
    from .harness_skills import pi_user_path
    from .permissions import harness_permission_extension, pi_policy_document

    ext_name = harness_permission_extension(kind)
    if ext_name is None:
        return False
    # Checked BEFORE the catalog lookup below, unconditionally: a missing extension carrier must
    # not short-circuit past this validation — the override is exactly as unenforceable either way.
    overridden_roles = [r for r in ("deny", "ask") if p.get(r) not in (None, [])]
    _reject_unenforceable_pi_override(overridden_roles, kinds)
    item = catalog.get("pi_extensions", ext_name)
    if item is None:
        plan.notes.append(
            f"permissions: harness '{kind}' extension '{ext_name}' not found in the agent-tools "
            "catalog (pi-extensions/); permission enforcement NOT provisioned"
        )
        return True
    install_dir = _expand(pi_user_path(f"extensions/{ext_name}"), config.repo_root)
    policy_file = _expand(pi_user_path("rig-permission-policy.json"), config.repo_root)
    # `deny`/`ask` REPLACE the baked baseline wholesale ("[] disables it", same as the claude-code
    # allowlist path). override_enforced_nowhere would have already raised above, so reaching here
    # with a non-empty override means at least one other configured target still consumes it — pi
    # alone drops it with a note (partial application, same precedent as opencode's dialect drop
    # in _resolve_permission_rules). Neither the raise nor the note suggests hand-editing the
    # policy file as a workaround: the next `rig apply` reconciles it back to the baked/declared
    # policy regardless, so that "fix" would silently revert.
    deny_override = [] if p.get("deny") == [] else None
    ask_override = [] if p.get("ask") == [] else None
    for role in overridden_roles:
        plan.notes.append(
            f"permissions: raw {role} entries dropped for harness '{kind}' — pi's policy rules "
            "are structured (not the claude-code rule-string dialect) and rig has no durable way "
            "to add a custom pi rule through config yet"
        )
    _note_pi_dropped_allowlist_keys(plan, p, kind)
    policy = pi_policy_document(deny_override=deny_override, ask_override=ask_override)
    plan.actions.append(
        Action(
            kind="provision_pi_extension",
            category="permissions",
            item=kind,
            source=item.path,
            target=install_dir,
            options={
                "kind": kind,
                "extension": ext_name,
                "policy_file": str(policy_file),
                "policy": policy,
            },
        )
    )
    return True
