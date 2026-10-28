"""Plan stage for harness mode provisioning (auto / permission mode per configured harness kind), the omp
web-search toggle and the delegated-harness notes.

Split out of :mod:`riglib.plan`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from .claude_config_dirs import SETTINGS_FILENAME
from .claude_config_dirs import SettingsTarget
from .claude_config_dirs import fan_out_item
from .claude_config_dirs import fan_out_settings
from .claude_config_dirs import resolve_settings_paths
from .config import LoadedConfig
from .harness_mode import HARNESS_MODES
from .harness_mode import WRITER_APPROVAL
from .harness_mode import delegated_note
from .harness_mode import harness_auto_intent
from .harness_mode import mode_is_known
from .harness_mode import na_note
from .harness_mode import resolved_mode_value
from .harness_mode import undeclared_note
from .harness_mode import unknown_mode_kind_note
from .harness_mode import unknown_mode_note
from .plan_model import Action
from .plan_model import InstallPlan
from .plan_targets import _DEFAULT_HARNESS_KIND
from .plan_targets import _configured_harness_kinds
from .plan_targets import _expand


# claude-code's PROJECT settings file: a NON-auto mode on the PRIMARY kind is written here
# (committed, travels with the repo). `auto` is special: Claude Code IGNORES
# `permissions.defaultMode: auto` from project/local settings (v2.1.142+) and honors it ONLY
# from the user's machine settings — so auto-mode is provisioned per-MACHINE, not per-repo. The
# user-scope file and the auto/interactive VALUES come from the one registry
# (``riglib.harness_mode.HARNESS_MODES["claude-code"]``): `auto` (research preview) auto-approves
# WITH a safety classifier — preferred over `bypassPermissions` (which skips every check;
# container/VM only). `default` restores prompts. Pin `harness.mode: bypassPermissions` to opt
# back into full bypass (non-qualifying account / container) — that value IS committable at
# project scope.
_HARNESS_SETTINGS = {
    "claude-code": ".claude/settings.json",
}


def _build_harness(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan the auto/permission-mode write for EVERY configured harness kind.

    No harness block → no action (rig leaves the harness config untouched). With a block, the
    primary ``kind`` AND each additive ``kinds`` entry is routed through the per-kind registry
    (:mod:`riglib.harness_mode`): claude-code keeps its dedicated writer (project-vs-user settings
    file, self-merge); codex/opencode get a generic ``apply_harness`` write of their own key; omp's
    key is owned by the permissions approval action (a note points at it); pi/commandcode have no
    such setting and get a VISIBLE n/a note. The plan stays pure; the merge happens in ``actions/``.
    """
    h = _enabled_harness_block(config)
    if h is None:
        return
    primary = str(h.get("kind", _DEFAULT_HARNESS_KIND))
    mode = h.get("mode")
    # Resolved ONCE here and handed to every per-kind builder — a builder re-deriving it from the
    # raw block would disagree with this one in the unknown-mode case ("not declared" while
    # ``mode:`` IS set) and `rig status` would print the contradiction.
    intent = harness_auto_intent(h, primary)
    # A `mode:` the primary kind does not know (and no auto_mode to decide): no other kind can
    # follow it — an unknown string must not become a silent tighten to interactive (or a silent
    # nothing) for the kind set. A claude-code PRIMARY still writes its own `mode:` verbatim (the
    # user owns that string, and claude-code's vocabulary can outgrow the registry).
    unknown_mode = intent is None and bool(mode) and not mode_is_known(primary, mode)
    kinds = _configured_harness_kinds(config)
    if unknown_mode and any(k in HARNESS_MODES and not (k == primary == "claude-code") for k in kinds):
        plan.notes.append(unknown_mode_note(primary, mode))
    for kind in kinds:
        _build_harness_for_kind(config, plan, h, kind, primary=primary, mode=mode, intent=intent, unknown_mode=unknown_mode)


def _build_harness_for_kind(
    config: LoadedConfig,
    plan: InstallPlan,
    h: dict[str, Any],
    kind: str,
    *,
    primary: str,
    mode: Any,
    intent: Any,
    unknown_mode: bool,
) -> None:
    """Route one configured harness kind to its builder (or note)."""
    spec = HARNESS_MODES.get(kind)
    if spec is None:
        # elevated only when the N/A kind is the PRIMARY and something was asked of it; an
        # additive `kinds: [pi]` next to `auto_mode: true` is the documented skills-only
        # listing and must not raise an alarm on every apply
        asked = kind == primary and (h.get("auto_mode") is not None or bool(mode))
        plan.notes.append(na_note(kind, declared=asked))
    elif spec.writer == WRITER_APPROVAL:
        return  # noted by _build_harness_delegated_notes once the approval builder has run
    elif kind == primary == "claude-code":
        _build_harness_claude_code(config, plan, h, kind)
    elif unknown_mode:
        # the primary (non-claude) kind and every additive kind alike: nothing is written
        plan.notes.append(unknown_mode_kind_note(kind, primary, mode))
    elif kind == "claude-code":
        block = _claude_code_block(h, kind, primary, intent)
        if block is None:
            plan.notes.append(undeclared_note(kind))
        else:
            _build_harness_claude_code(config, plan, block, kind)
    else:
        _build_harness_mode_for_kind(config, plan, kind, intent, primary=primary, mode=mode)


def _enabled_harness_block(config: LoadedConfig) -> dict[str, Any] | None:
    """The ``harness:`` block when present and not ``enabled: false``, else ``None``."""
    h = config.data.get("harness")
    if not isinstance(h, dict) or not h or h.get("enabled") is False:
        return None
    return h


def _build_omp_web_search(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan the omp ``providers.webSearchExclude`` merge when omp is a configured kind.

    Default ON for an enabled harness that lists omp: exclude ``perplexity`` so web_search
    never falls through to the Sign-up wall (rig-cli#447). ``web_search.enabled: false``
    or ``exclude: []`` emits nothing. Extra names already on disk are never removed.
    """
    from .harness_skills import omp_user_path
    from .omp_web_search import resolve_exclude

    h = _enabled_harness_block(config)
    if h is None:
        return
    if "omp" not in _configured_harness_kinds(config):
        return
    exclude = resolve_exclude(h)
    if not exclude:
        return
    plan.actions.append(
        Action(
            kind="provision_omp_web_search",
            category="harness",
            item="omp",
            source=config.repo_root,
            target=_expand(omp_user_path("config.yml"), config.repo_root),
            options={"kind": "omp", "exclude": exclude},
        )
    )


def _build_harness_delegated_notes(config: LoadedConfig, plan: InstallPlan) -> None:
    """Note, for every configured kind whose mode key is owned by the permissions approval action,
    whether the plan ACTUALLY carries that action — read off ``plan.actions`` after
    ``_build_guard_policy`` ran, never re-derived from its gate (a re-prediction that drifts from
    the real gate would claim "written by the approval action" while nothing writes it, and the
    kind would fall off ``rig status`` — the silent gap rig-cli#355 exists to close)."""
    if _enabled_harness_block(config) is None:
        return
    for kind in _configured_harness_kinds(config):
        spec = HARNESS_MODES.get(kind)
        if spec is None or spec.writer != WRITER_APPROVAL:
            continue
        written = any(
            a.kind == "provision_harness_approval" and str(a.options.get("kind", "")) == kind
            for a in plan.actions
        )
        plan.notes.append(delegated_note(kind, written=written))


def _claude_code_block(h: dict[str, Any], kind: str, primary: str, intent: bool | None) -> dict[str, Any] | None:
    """The harness block the claude-code writer sees — or ``None`` when nothing should be written.

    As the PRIMARY kind it is the block itself (``mode:`` / ``settings_path`` are claude-code
    values there; an undeclared intent keeps the legacy ``default`` write). As an ADDITIVE kind
    (``kind: opencode, kinds: [claude-code]``) the block's ``mode:``/``settings_path`` belong to the
    primary harness and must not leak: claude-code then gets only the inferred boolean intent
    (plus ``self_merge``), and an UNDECLARED intent means no write at all — the same rule as every
    other additive kind (a skills-only listing must not change the posture behind the user's back).
    The write is pinned to the USER-scope file either way: auto-mode is a per-machine posture for
    every additive kind, so an interactive intent must not land in the committed project settings.
    """
    if kind == primary:
        return h
    if intent is None:
        return None
    reduced: dict[str, Any] = {"auto_mode": intent, "settings_path": HARNESS_MODES[kind].settings_path()}
    if "self_merge" in h:
        reduced["self_merge"] = h["self_merge"]
    return reduced


def _build_harness_mode_for_kind(
    config: LoadedConfig, plan: InstallPlan, kind: str, intent: bool | None, *, primary: str, mode: object,
) -> None:
    """One generic ``apply_harness`` write of ``kind``'s own mode key (codex/opencode).

    The value follows the harness auto ``intent`` resolved by :func:`_build_harness` (``auto_mode``,
    else inferred from a pinned ``mode:`` via the primary kind's ``auto_values``) — except that the
    PRIMARY kind's own known ``mode:`` is written verbatim (``kind: opencode, mode: deny`` must stay
    ``deny``, not relax to ``ask``; see :func:`resolved_mode_value`). No declared intent → no write
    (an informational note instead). The target is always the kind's user-scope file from the registry.
    """
    spec = HARNESS_MODES[kind]
    if intent is None:
        # no auto_mode / mode declared: a skills-only additive kind (`kinds: [opencode]`) must not
        # have its posture tightened to the interactive value behind its back — write nothing, say so.
        plan.notes.append(undeclared_note(kind))
        return
    # An ADDITIVE kind never gets the raw ``mode:`` string: it is the primary harness's value
    # (claude-code's ``acceptEdits``, opencode's ``deny``) and would be an invalid setting here — it
    # only feeds the intent inference above. The primary's own known value is the documented override.
    mode_value = resolved_mode_value(kind, primary, mode, intent)
    # ALWAYS the kind's user-scope file: auto-mode is a per-MACHINE posture (the same reason the
    # claude-code ``auto`` write is user-scope only). ``harness.settings_path`` is NOT consulted here —
    # it is the hook-bridge / claude settings override (for opencode it names a .js PLUGIN), never
    # the mode file.
    settings_path = spec.settings_path()
    plan.actions.append(
        Action(
            kind="apply_harness",
            category="harness",
            item=kind,
            source=config.repo_root,  # no carrier in agent-tools; anchor on the repo
            target=_expand(str(settings_path), config.repo_root),
            options={
                "kind": kind,
                "auto_mode": bool(intent),
                "mode_value": mode_value,
                "format": spec.format,
            },
        )
    )


def _build_harness_claude_code(config: LoadedConfig, plan: InstallPlan, h: dict[str, Any], kind: str) -> None:
    """The claude-code writer: ``permissions.defaultMode`` + the self-merge unblock."""
    spec = HARNESS_MODES[kind]
    auto_mode = bool(h.get("auto_mode", False))
    # an explicit `mode:` override wins over the auto_mode → mode mapping (lets a config pin
    # e.g. `acceptEdits` instead of full bypass while staying non-interactive for edits).
    mode_value = h.get("mode") or spec.values[auto_mode]
    # The self-merge classifier carve-out (default ON) is EFFECTIVE only under `auto` — the
    # auto-mode classifier is the only mode that runs the soft-block rules, and its config lives
    # in the user-scope settings (same file the `auto` mode write targets). It is safe precisely
    # because every other guard stays: the review-fix loop, the local CI gate via ship.sh, the
    # agent-hooks (incl. block-raw-pr-merge for OTHER PRs), and the anti-exfil hard_deny.
    self_merge = bool(h.get("self_merge", True)) and mode_value == "auto"
    # `auto` is honored only from the user's machine settings (CC strips it from project/local
    # scope); every other mode writes to the repo's project settings. Explicit settings_path wins.
    if h.get("settings_path"):
        settings_path = h["settings_path"]
    elif mode_value == "auto":
        settings_path = spec.settings_path()
    else:
        settings_path = _HARNESS_SETTINGS[kind]
    # One action PER settings file a live `claude` on this machine may load: the user-scope
    # write fans out to every claude-rotate config dir (rig-cli#368) — see claude_config_dirs.
    for target in _claude_settings_targets(config, kind, _expand(str(settings_path), config.repo_root)):
        plan.actions.append(
            Action(
                kind="apply_harness",
                category="harness",
                item=fan_out_item(kind, target),
                source=config.repo_root,  # no carrier in agent-tools; anchor on the repo
                target=target.path,
                options={
                    "kind": kind,
                    "auto_mode": auto_mode,
                    "mode_value": str(mode_value),
                    "self_merge": self_merge,
                },
            )
        )


def _claude_settings_targets(config: LoadedConfig, kind: str, primary: Path) -> list[SettingsTarget]:
    """The settings files a claude-code user-scope write must reach (primary + account dirs).

    Every other harness kind (and any non-user-scope claude-code target) resolves to the
    primary alone — the fan-out registry (:mod:`riglib.claude_config_dirs`) decides.
    """
    if kind != "claude-code":
        return [SettingsTarget(primary, None)]
    if not primary.suffix:
        # the documented directory form (`settings_path: ~/.claude`) — the writers append the
        # native filename at write time; resolve it HERE too, so the user-scope check sees the
        # same file the writer converges instead of silently skipping the fan-out
        primary = primary / SETTINGS_FILENAME
    h = config.data.get("harness")
    # explicit extra files go through the SAME resolver as settings_path (``$VAR`` + ``~`` +
    # repo-root relative) — shared with `rig doctor`, so a relative entry never reaches a
    # writer (or the doctor inventory) as a cwd-dependent path
    harness = resolve_settings_paths(h if isinstance(h, dict) else {}, config.repo_root)
    return fan_out_settings(primary, harness)


def _permissions_kinds(config: LoadedConfig, p: dict[str, Any]) -> list[str]:
    """Harness kinds whose command allowlist should be provisioned, preserving config order."""
    # Callers pass the cascaded permissions block. Missing or explicit-null kind means
    # "unpinned" here; a repo-local null has already masked any global pin before this point.
    if p.get("kind"):
        return [str(p["kind"])]
    return _configured_harness_kinds(config)
