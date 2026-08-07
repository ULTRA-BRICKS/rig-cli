"""Plan builder — resolves (config + catalog) into an ordered list of Actions.

This is the headless engine ``rig apply`` and the wizard's Apply screen both call. It
turns the declarative ``rig.yaml`` decisions into concrete, idempotent install actions:

- resolves which catalog items are enabled (category ``enabled``, ``all``/``enable``/
  ``disable`` deltas, per-item ``items.<name>.enabled``, and the detected project type
  pulling in by-type skill bundles),
- resolves each item's install target (item → category → ``defaults`` → built-in),
- produces a stable, ordered ``InstallPlan`` of :class:`Action` objects.

An :class:`Action` is a small dataclass describing the change; the *execution* lives in
``actions/`` (stdlib-only). The plan never touches disk — ``--dry-run`` prints it.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .catalog import Catalog, Item, rule_provider_carrier
from .claude_config_dirs import (
    SETTINGS_FILENAME,
    SettingsTarget,
    fan_out_item,
    fan_out_settings,
    resolve_settings_paths,
)
from .config import (
    GITIGNORE_DEFAULT_ENTRIES,
    GITIGNORE_DEFAULT_EXCLUDESFILE,
    LoadedConfig,
    OMP_HOOK_BRIDGE_EXTENSION_NAME,
    OPENCODE_HOOK_BRIDGE_PLUGIN_NAME,
    linter_path_escapes_repo,
)
from .github_actions import GITHUB_ACTIONS_DEFAULTS
from .github_browser import UI_ONLY_TOGGLES
from .github_ghas import GITHUB_GHAS_DEFAULTS
from .github_merge import GITHUB_MERGE_DEFAULTS
from .github_ruleset import CI_GATE_CHECK_CONTEXTS, GITHUB_RULESET_DEFAULTS, UNATTRIBUTED_APPROVAL_KEY
from .harness_skills import HARNESS_SKILL_DIRS as _HARNESS_SKILL_DIRS
from .harness_skills import codex_user_path as _codex_user_path
from .harness_skills import instruction_file_for as _instruction_file_for
from .harness_skills import native_skills_dir_for as _native_skills_dir_for
from .harness_skills import omp_user_path as _omp_user_path
from .harness_skills import skill_dir_for as _skill_dir_for
from .harness_mode import (
    HARNESS_MODES,
    WRITER_APPROVAL,
    delegated_note,
    harness_auto_intent,
    mode_is_known,
    mode_value_for,
    resolved_mode_value,
    na_note,
    undeclared_note,
    unknown_mode_kind_note,
    unknown_mode_note,
)
from .paths import expand_user_path as _expand_user_path
from .provenance import KNOWN_CATEGORIES, known_names_from_config
from . import ecosystem_readme
from . import project_tools


class PlanError(ValueError):
    """Raised when the plan cannot be built (fail-closed before any write).

    Notably: an unknown item name in the config. The schema promises fail-closed
    validation for unknown items; catching it here (where the catalog is available)
    keeps ``apply`` from silently exiting 0 having installed nothing.
    """


# Built-in default targets per category (used when config + defaults don't pin one).
_BUILTIN_TARGETS = {
    "skills": "~/.agents/skills",
    "agent_hooks": "~/.claude/hooks",
    "ci": ".github/workflows",
    "mcp": "~/.claude/mcp",
}
_HARNESS_AGENT_HOOK_TARGETS = {
    "claude-code": "~/.claude/hooks",
    "codex": "~/.codex/hooks",
    "opencode": "~/.config/opencode/hooks",
    # omp deliberately has NO entry here: unlike codex (whose literal baseline below is a real,
    # separate consumer — the RIG_CODEX_HOME divergence check), omp has no divergence-detection
    # block that reads a static baseline, so a dict entry would be a second, unenforced source
    # of truth that could silently drift from omp_user_path("hooks")'s own resolution. The
    # dynamic special-case in _agent_hooks_target_for_kind below is the ONLY source.
}
_DEFAULTS_KEY = {
    "skills": "skills_target",
    "agent_hooks": "hooks_target",
    "ci": "ci_target",
    "mcp": "mcp_target",
}


# Per-harness skill/instruction discovery lives in ONE registry, :mod:`riglib.harness_skills`.
# ``_HARNESS_SKILL_DIRS`` (imported above) is the skills-DIRECTORY map: a skill copied into
# ``skills_target`` (default ``~/.agents/skills``) is invisible to a skills-dir harness unless it
# is also present in that harness's discovery dir (claude-code → ``~/.claude/skills``; codex is
# resolved dynamically from the rig-owned ``RIG_CODEX_HOME`` override), so rig maintains an idempotent symlink per enabled
# skill into the harness dir for the configured kind. Native-discovery harnesses such as opencode
# scan the default
# ``skills_target`` directly; when the user customizes ``skills_target``, rig links back into the
# native root so the harness still sees the installed skills. INSTRUCTION-FILE harnesses
# (pi/commandcode) have no skills dir — they surface guidance via a global
# AGENTS.md (the ``agents_md`` area), so the plural skill-dir resolver emits no link
# for them and records a status note.
# (The module-level alias name is preserved so ``plan._HARNESS_SKILL_DIRS`` keeps resolving.)
_DEFAULT_HARNESS_KIND = "claude-code"
_AUTONOMOUS_DEFAULT_ALLOW_RULES = ("Bash(dev:*)", "Bash(review:*)", "Bash(task:*)")

# The exact substring `rig apply`'s note-attention classifier (riglib.cli._NOTE_ATTENTION_MARKERS)
# and `rig status`'s dedicated coverage-gap call-out (riglib.cli._print_hook_bridge_coverage_gaps)
# both match against — ONE literal, so a future wording tweak here can't silently desync the two
# consumers (they'd import this constant instead of re-typing the phrase).
HOOK_BRIDGE_NO_SUPPORT_PHRASE = "has no supported agents-hooks bridge yet"


def _agent_hooks_target_for_kind(kind: str) -> str | None:
    """The unexpanded descriptor dir for a harness kind."""
    if kind == "codex":
        return _codex_user_path("hooks")
    if kind == "omp":
        # dynamic like codex: honors PI_CODING_AGENT_DIR (full override) / PI_CONFIG_DIR
        # (renames the .omp config-root dirname) via the one omp_agent_root() resolver.
        return _omp_user_path("hooks")
    return _HARNESS_AGENT_HOOK_TARGETS.get(kind)


@dataclass
class Action:
    """A single planned install step. ``kind`` selects the runner in ``actions/``."""

    kind: str  # copy_skill | link_skill_harness | install_agent_hook | install_dispatcher | install_ci | register_mcp | apply_harness | provision_permissions | provision_execpolicy | install_harness_guard | provision_harness_approval | provision_instruction_policy | register_hook_bridge | provision_omp_idle_ttl | provision_schedule | provision_agents_symlink | provision_agents_orchestrator | provision_agents_autonomous_mode | provision_project_tool | apply_ecosystem_readme | provision_github_ruleset | provision_github_merge | provision_github_ghas | provision_github_actions | provision_github_browser | provision_tmux | provision_global_excludes | provision_spotlight | provision_ship_task_prefix
    category: str
    item: str
    source: Path  # carrier path in the agent-tools checkout
    target: Path  # resolved install destination (expanded, absolute where applicable)
    options: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        # A hook item can emit several actions into one target dir (one per descriptor);
        # append the descriptor name so plan/dry-run lines stay distinguishable.
        descriptor = self.options.get("descriptor")
        dest = self.target / descriptor if descriptor else self.target
        return f"{self.category}/{self.item} → {dest}"


@dataclass
class InstallPlan:
    actions: list[Action] = field(default_factory=list)
    on_conflict: str = "backup"
    notes: list[str] = field(default_factory=list)
    # Every catalog item per extras-scanned category (skills / agent_hooks / ci / mcp), keyed by
    # the on-disk leaf name (`by-type/cli/lazy-imports` → `lazy-imports`) → EVERY source path
    # carrying that leaf (two namespaces may share one — `by-type/cli/x` and `by-stack/…/x` — and
    # the on-disk dir is named by the leaf alone), so drift can tell "rig could have written this"
    # (a catalog orphan, or a rig-installed item another repo's config selects — verified against
    # the source content) from "rig has no opinion on this" (a repo's own workflow → known).
    # ``None`` = no catalog knowledge (a hand-built plan): the scanners then treat every
    # undeclared item as drift, the conservative reading. Filled by :func:`build`. See
    # :mod:`riglib.provenance`.
    catalog_items: dict[str, dict[str, tuple[Path, ...]]] | None = None
    # The `<category>.known` config lists (see :func:`riglib.provenance.known_names_from_config`),
    # the user's side of the same classification. Carried on the plan so `detect` has ONE input
    # channel for provenance (catalog + config), not a kwarg only some callers remember to pass.
    known_names: dict[str, set[str]] | None = None

    def __len__(self) -> int:
        return len(self.actions)


def _expand(path_str: str, repo_root: Path) -> Path:
    # Per-machine expansion at apply time keeps a committed rig.yaml portable.
    #  - a portable ``~/.config/...`` prefix maps to ``$XDG_CONFIG_HOME`` when set, so rig
    #    installs where XDG-aware tools (the dispatcher runner) actually look;
    #  - ``$VAR``/``${VAR}`` and ``~`` are expanded;
    #  - a relative remainder is anchored at the repo root.
    p = _expand_user_path(path_str)
    if not p.is_absolute():
        p = (repo_root / p).resolve()
    return p


def _resolve_target(config: LoadedConfig, category: str) -> Path:
    cat = config.category(category)
    target = cat.get("target")
    if not target:
        target = config.defaults.get(_DEFAULTS_KEY[category])
    if not target:
        target = _BUILTIN_TARGETS[category]
    return _expand(str(target), config.repo_root)


def _resolve_primary_agent_hooks_target(config: LoadedConfig) -> Path:
    """Resolve the agent-hooks descriptor dir, defaulting to the active harness when needed."""
    ah = config.category("agent_hooks")
    target = ah.get("target")
    kind = _harness_kind_for_skills(config)
    harness_default = _agent_hooks_target_for_kind(kind)
    target_is_legacy = not target or _is_legacy_hooks_target(config, str(target))
    if harness_default and target_is_legacy and not _has_custom_hooks_default(config):
        return _expand(harness_default, config.repo_root)
    return _resolve_target(config, "agent_hooks")


def _resolve_agent_hooks_target(config: LoadedConfig) -> Path:
    """Resolve the primary agent-hooks descriptor dir."""
    return _resolve_agent_hooks_targets(config)[0]


def _is_legacy_hooks_target(config: LoadedConfig, raw: str) -> bool:
    """True when ``raw`` is equivalent to the historical Claude hooks default."""
    return _same_dir(
        _expand(raw, config.repo_root),
        _expand(_BUILTIN_TARGETS["agent_hooks"], config.repo_root),
    )


def _has_custom_hooks_default(config: LoadedConfig) -> bool:
    """True when defaults.hooks_target is present and not equivalent to the legacy default."""
    defaults_target = config.defaults.get(_DEFAULTS_KEY["agent_hooks"])
    if not defaults_target:
        return False
    return not _same_dir(
        _expand(str(defaults_target), config.repo_root),
        _expand(_BUILTIN_TARGETS["agent_hooks"], config.repo_root),
    )


def _has_custom_hooks_target(config: LoadedConfig) -> bool:
    """True when agent_hooks.target is present and not equivalent to the legacy default."""
    target = config.category("agent_hooks").get("target")
    return bool(target and not _is_legacy_hooks_target(config, str(target)))


def _resolve_custom_agent_hooks_target(config: LoadedConfig) -> Path:
    """Resolve the single custom descriptor target, ignoring scaffolded legacy targets."""
    if _has_custom_hooks_target(config):
        return _resolve_target(config, "agent_hooks")
    defaults_target = config.defaults.get(_DEFAULTS_KEY["agent_hooks"])
    if defaults_target and _has_custom_hooks_default(config):
        return _expand(str(defaults_target), config.repo_root)
    return _resolve_target(config, "agent_hooks")


def _resolve_agent_hooks_targets(config: LoadedConfig) -> list[Path]:
    """Resolve every agent-hook descriptor dir rig should maintain.

    ``agent_hooks.target`` (or a non-legacy ``defaults.hooks_target``) is an explicit single
    target and keeps the historical behavior. Otherwise rig installs descriptors into every
    configured harness kind that has a known descriptor directory, so a Claude-primary machine
    can also keep Codex hooks live via ``harness.kinds: [codex]``.
    """
    if _has_custom_hooks_target(config) or _has_custom_hooks_default(config):
        return [_resolve_custom_agent_hooks_target(config)]

    out: list[Path] = []
    seen: set[Path] = set()
    for kind in _configured_harness_kinds(config):
        raw = _agent_hooks_target_for_kind(kind)
        if raw is None:
            continue
        target_path = _expand(raw, config.repo_root)
        if target_path in seen:
            continue
        seen.add(target_path)
        out.append(target_path)
    if out:
        return out
    return [_resolve_primary_agent_hooks_target(config)]


def _additional_hook_targets_enabled(config: LoadedConfig) -> bool:
    """True when hook descriptor targets are allowed to fan out to additional harness kinds."""
    return not _has_custom_hooks_target(config) and not _has_custom_hooks_default(config)


def _hook_bridge_kinds(config: LoadedConfig) -> list[str]:
    """Harness kinds whose bridge can see installed descriptors."""
    return _configured_harness_kinds(config)


def _same_dir(a: Path, b: Path) -> bool:
    """True when two paths denote the same directory, resolving symlinks/`..` where possible.
⚠ 1 unresolved conflict detected
- ours = HEAD
- theirs = a530cf2 (feat(task): add task.code_prefix, reconciled into .ship-config)
- base = parent of a530cf2 (feat(task): add task.code_prefix, reconciled into .ship-config)
NOTICE: Inspect a block by reading `conflict://<N>` (add `/ours` / `/theirs` / `/base` to render a single side). Resolve with `write({ path: "conflict://<N>", content })`, or bulk-resolve every registered conflict with `write({ path: "conflict://*", content })`. Writes replace ONLY the marker block (markers + all sides) — never repeat the lines before/after it; they stay in place.
`content` shorthand: a line that is exactly `@ours` / `@theirs` / `@base` / `@both` expands to that recorded section. `@both` is ours-then-theirs with no separator — only for additive conflicts where each side adds something different; NEVER for competing edits of the same lines (pick a side or write the combined text). Lines that are not a token pass through verbatim, so `"// keep both\n@ours\n@theirs"` literally writes the comment, then ours, then theirs.
Per-id bulk: `write({ path: "conflict://*", content: "1: @ours\n2: @theirs\n…" })` resolves each listed id with that side in ONE call — the cheapest way through many pick-one conflicts; unlisted ids stay registered.
Resolve each block faithfully: keep one side (`@ours`/`@theirs`), or combine them when both intents apply — never invent content beyond the recorded sides, and never stack both sides of competing edits. Resolve several conflicts in a single turn by issuing multiple `write` calls at once; ids stay valid as earlier blocks are resolved.

──── #2  L143-149 ────
<<< ours
    kind: str  # copy_skill | link_skill_harness | install_agent_hook | install_dispatcher | install_ci | register_mcp | apply_harness | provision_permissions | provision_execpolicy | install_harness_guard | provision_harness_approval | provision_instruction_policy | register_hook_bridge | provision_omp_idle_ttl | provision_schedule | provision_agents_symlink | provision_agents_orchestrator | provision_agents_autonomous_mode | provision_project_tool | apply_ecosystem_readme | provision_github_ruleset | provision_github_merge | provision_github_ghas | provision_github_actions | provision_github_browser | provision_tmux | provision_global_excludes | provision_spotlight
=== base
    kind: str  # copy_skill | link_skill_harness | install_agent_hook | install_dispatcher | install_ci | register_mcp | apply_harness | provision_permissions | provision_execpolicy | install_harness_guard | provision_harness_approval | provision_instruction_policy | register_hook_bridge | provision_schedule | provision_agents_symlink | provision_project_tool | provision_github_ruleset | provision_github_merge | provision_github_ghas | provision_github_actions | provision_github_browser | provision_tmux | provision_global_excludes | provision_spotlight
>>> theirs
    kind: str  # copy_skill | link_skill_harness | install_agent_hook | install_dispatcher | install_ci | register_mcp | apply_harness | provision_permissions | provision_execpolicy | install_harness_guard | provision_harness_approval | provision_instruction_policy | register_hook_bridge | provision_schedule | provision_agents_symlink | provision_project_tool | provision_github_ruleset | provision_github_merge | provision_github_ghas | provision_github_actions | provision_github_browser | provision_tmux | provision_global_excludes | provision_spotlight | provision_ship_task_prefix

[Showing lines 1-300 of 3136. Use :301 to continue]