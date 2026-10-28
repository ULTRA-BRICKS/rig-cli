"""Install-target resolution for the plan builders: per-category targets, per-harness hook and skill
directories, and the notes that explain why a harness has no skill-link dir.

Split out of :mod:`riglib.plan`; every function here is pure (config in, paths/notes out).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from .config import LoadedConfig
from .harness_skills import codex_user_path as _codex_user_path
from .harness_skills import instruction_file_for as _instruction_file_for
from .harness_skills import native_skills_dir_for as _native_skills_dir_for
from .harness_skills import omp_user_path as _omp_user_path
from .harness_skills import skill_dir_for as _skill_dir_for
from .paths import expand_path as _expand_path


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


# The exact substring `rig apply`'s note-attention classifier (riglib.cli_plan_print._NOTE_ATTENTION_MARKERS)
# and `rig status`'s dedicated coverage-gap call-out (riglib.cli_plan_print._print_hook_bridge_coverage_gaps)
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


def _expand(path_str: str, repo_root: Path) -> Path:
    # Per-machine expansion at apply time keeps a committed rig.yaml portable.
    #  - a portable ``~/.config/...`` prefix maps to ``$XDG_CONFIG_HOME`` when set, so rig
    #    installs where XDG-aware tools (the dispatcher runner) actually look;
    #  - ``$VAR``/``${VAR}`` and ``~`` are expanded;
    #  - a relative remainder is anchored at the repo root.
    return _expand_path(path_str, repo_root)


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


def _kinds_in_block(h: dict[str, Any], default_primary: str | None) -> list[str]:
    """Primary + additive kinds of ONE ``harness`` block, in order, dropping non-strings.

    Reads a block that may not have passed validation (the global layer under a repo override is
    validated only as part of the merged block), so ``kinds`` is trusted only when it is a list.
    """
    extra = h.get("kinds")
    out: list[str] = []
    for kind in [h.get("kind") or default_primary, *(extra if isinstance(extra, list) else [])]:
        if isinstance(kind, str) and kind and kind not in out:
            out.append(kind)
    return out


def _global_harness_kinds(config: LoadedConfig) -> list[str]:
    """Primary + additive harness kinds the GLOBAL config declares (``[]`` when it has no harness block).

    A global block that names no ``kind`` still has the default primary (``claude-code``), exactly
    like the effective config's own resolution.

    The user-scope descriptor dirs (``~/.claude/hooks``, ``~/.omp/agent/hooks`` …) and their hook
    bridges belong to the harnesses installed on the MACHINE: a repo ``rig.yaml`` pinning another
    ``harness.kind`` must not make ``apply`` stop maintaining what a machine harness keeps
    reading (rig-cli#743).
    """
    h = config.global_data.get("harness")
    if not isinstance(h, dict) or not h or h.get("enabled") is False:
        return []
    return _kinds_in_block(h, _DEFAULT_HARNESS_KIND)


def _kinds_with_global(config: LoadedConfig) -> list[str]:
    """The effective config's kinds first, then any kind only the global config declares.

    A repo that switches its harness off (``harness.enabled: false``) opts out of all of it, the
    machine kinds included.
    """
    out = list(_configured_harness_kinds(config))
    effective = config.data.get("harness")
    if isinstance(effective, dict) and effective.get("enabled") is False:
        return out
    out.extend(kind for kind in _global_harness_kinds(config) if kind not in out)
    return out


def _resolve_agent_hooks_targets(config: LoadedConfig) -> list[Path]:
    """Resolve every agent-hook descriptor dir rig should maintain.

    ``agent_hooks.target`` (or a non-legacy ``defaults.hooks_target``) is an explicit single
    target and keeps the historical behavior. Otherwise rig installs descriptors into every
    configured harness kind that has a known descriptor directory — the effective config's kinds
    first, then any kind only the global config declares — so a Claude-primary machine can also
    keep Codex hooks live via ``harness.kinds: [codex]``, and an omp machine keeps
    ``~/.omp/agent/hooks`` current from inside a repo that pins ``harness.kind: claude-code``.
    """
    if _has_custom_hooks_target(config) or _has_custom_hooks_default(config):
        return [_resolve_custom_agent_hooks_target(config)]

    out: list[Path] = []
    seen: set[Path] = set()
    for kind in _kinds_with_global(config):
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
    return _kinds_with_global(config)


def _same_dir(a: Path, b: Path) -> bool:
    """True when two paths denote the same directory, resolving symlinks/`..` where possible.

    Falls back to a lexical compare if ``resolve()`` raises (e.g. a path under a non-existent
    parent on a platform that resolves strictly) — never raises.
    """
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a == b


def _harness_kind_for_skills(config: LoadedConfig) -> str:
    """The harness kind whose skill-discovery dir rig links into.

    Follows the ``harness.kind`` if a harness block pins one (so the skill links target the
    same harness the auto-mode write does); else the built-in default (claude-code).
    """
    h = config.data.get("harness")
    if isinstance(h, dict) and h.get("kind"):
        return str(h["kind"])
    return _DEFAULT_HARNESS_KIND


def _configured_harness_kinds(config: LoadedConfig) -> list[str]:
    """Primary harness kind plus any additive ``harness.kinds`` entries, preserving order."""
    h = config.data.get("harness")
    if not isinstance(h, dict) or not h:
        return [_DEFAULT_HARNESS_KIND]
    return _kinds_in_block(h, _DEFAULT_HARNESS_KIND) or [_DEFAULT_HARNESS_KIND]


def _resolve_harness_skill_dir(config: LoadedConfig) -> Path | None:
    """Resolve the harness skill-discovery dir to symlink installed skills into.

    Returns ``None`` when ``skills.harness_link`` is disabled or the harness kind has no
    known discovery dir — either an INSTRUCTION-FILE harness (pi/commandcode,
    which surface skills via AGENTS.md, not a symlinked dir) or an unknown kind. We
    never guess a path. An explicit ``skills.harness_skill_dir`` overrides the per-harness
    default (and forces the link even for an instruction-file harness — the user pointed at a
    real dir on purpose).
    """
    sk = config.category("skills")
    if sk.get("harness_link") is False:
        return None
    raw = sk.get("harness_skill_dir")
    if not raw:
        kind = _harness_kind_for_skills(config)
        raw = _skill_dir_for(kind)
        if not raw:
            return None
    return _expand(str(raw), config.repo_root)


def _resolve_harness_skill_dirs(config: LoadedConfig) -> list[tuple[str, Path]]:
    """Resolve every harness skill-discovery dir that should receive installed-skill links."""
    sk = config.category("skills")
    if sk.get("harness_link") is False:
        return []
    raw = sk.get("harness_skill_dir")
    if raw:
        return [(_harness_kind_for_skills(config), _expand(str(raw), config.repo_root))]

    out: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    skills_target = _resolve_target(config, "skills")
    for kind in _configured_harness_kinds(config):
        raw_dir = _skill_dir_for(kind)
        if raw_dir is None:
            native = _native_skills_dir_for(kind)
            if native is None:
                continue
            native_path = _expand(str(native), config.repo_root)
            if _same_dir(skills_target, native_path):
                continue
            path = native_path
        else:
            path = _expand(str(raw_dir), config.repo_root)
        if path in seen:
            continue
        seen.add(path)
        out.append((kind, path))
    return out


def _skill_discovery_notes(config: LoadedConfig) -> list[str]:
    """A status note explaining why no harness skill-link is emitted for an instruction-file
    harness, or ``None`` when one is (skills-dir harness) or linking is disabled / overridden.

    Keeps ``rig status`` honest: a pi/commandcode config that links no skills isn't a
    silent gap — the note says the kind reads a global AGENTS.md instead, so the skill
    content reaches it through the ``agents_md`` area, not a per-skill symlink.
    """
    sk = config.category("skills")
    if sk.get("enabled") is False:
        return []  # no skills installed at all → nothing to say about their discovery
    if sk.get("harness_link") is False or sk.get("harness_skill_dir"):
        return []  # linking off, or an explicit dir override forces a real link → no note
    notes: list[str] = []
    skills_target = _resolve_target(config, "skills")
    for kind in _configured_harness_kinds(config):
        if _skill_dir_for(kind) is not None:
            continue  # skills-dir harness → a link IS emitted, no note needed
        native = _native_skills_dir_for(kind)
        if native is not None:
            native_path = _expand(str(native), config.repo_root)
            if _same_dir(skills_target, native_path):
                notes.append(
                    f"skills: harness '{kind}' auto-loads {native} natively — no per-skill symlink "
                    "needed (skills install to the default skills_target, which it already scans)"
                )
            else:
                notes.append(
                    f"skills: harness '{kind}' auto-loads {native} natively, but skills_target is "
                    f"{skills_target}; rig links installed skills into the native discovery dir"
                )
            continue
        instr = _instruction_file_for(kind)
        if instr is None:
            continue  # unknown kind → handled by validation; nothing to say here
        notes.append(
            f"skills: harness '{kind}' has no skill-discovery dir — it reads a global "
            f"instruction file ({instr}); rig records that path for status instead of inventing "
            "a per-skill symlink (set skills.harness_skill_dir to force a directory link)"
        )
    return notes


def resolve_category_target(config: LoadedConfig, category: str) -> Path | None:
    """Public: the resolved install dir for a category (None when ``export-only``).

    Used by ``rig status`` to scan a category's configured target for disk→config extras
    even when no action lands there.
    """
    raw = config.category(category).get("target")
    if raw == "export-only":
        return None
    if category == "agent_hooks":
        return _resolve_agent_hooks_target(config)
    return _resolve_target(config, category)


def resolve_category_targets(config: LoadedConfig, category: str) -> list[Path]:
    """Public: all resolved install dirs for categories that can fan out by harness."""
    if category == "agent_hooks":
        return list(_resolve_agent_hooks_targets(config))
    target = resolve_category_target(config, category)
    return [target] if target is not None else []
