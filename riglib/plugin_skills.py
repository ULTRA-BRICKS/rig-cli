"""Claude Code plugin-marketplace-sourced skills (``skills.plugins`` — rig-cli#590).

Why
---
A skill a Claude Code *plugin* ships (``<marketplace>/<plugin>/skills/<name>/SKILL.md``, e.g.
the ``superpowers`` marketplace plugin's ``brainstorming``/``writing-plans``/…) lives under a
**version-pinned** cache path: ``~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/``.
Hand-symlinking ``~/.agents/skills/<name>`` straight at that path works until the plugin
auto-updates — Claude Code rewrites ``installPath`` in
``~/.claude/plugins/installed_plugins.json`` to the new version, and nothing re-points the
symlink. It dangles silently.

This module resolves a declared ``skills.plugins.items.<plugin>: {marketplace: <name>}`` entry
against the LIVE ``installed_plugins.json`` at every ``rig apply`` (the same way a catalog skill
is re-resolved from ``agent_tools_source`` on every run), so the resulting
``~/.agents/skills/<name>`` symlink self-heals across a plugin update exactly like every other
rig-managed skill converges on drift.

Scope (documented in ``docs/config-schema.md`` and ``docs/specs/``)
---------------------------------------------------------------------
Claude Code's own plugin marketplace only — ``~/.claude/plugins/installed_plugins.json``. Codex,
Gemini, and any other agent's plugin/extension system are NOT read by this module. Only a
plugin's ``skills/*`` subtree is symlinked; a plugin's hooks, MCP servers, slash commands, or any
other capability are untouched (out of scope for this feature).

``installed_plugins.json`` shape (Claude Code's own file, not rig's)
----------------------------------------------------------------------
``{"version": 2, "plugins": {"<plugin>@<marketplace>": [{"scope": "user"|"project",
"installPath": "...", "version": "...", "installedAt": "...", "lastUpdated": "..."}, ...]}}`` —
one entry per SCOPE the plugin was installed at (a plugin can be both a "user" (global) install
and separately pinned inside one project's ``.claude`` dir, so the same key can list several
entries). rig provisions machine-wide (default ``skills_target`` is ``~/.agents/skills``, not
tied to any one repo), so :func:`resolve_plugin_install_path` prefers the ``user``-scoped entry
(what Claude Code resolves to OUTSIDE that one pinned project) and, among remaining candidates,
the most recently updated one — never a stale duplicate scope over a newer one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .paths import expand_user_path

# The one file Claude Code itself writes/reads to track a plugin's live install location, per
# scope. Unexpanded here so callers/tests go through expand_user_path (HOME-respecting, the same
# expansion every other harness path in this codebase uses) rather than a hardcoded absolute path.
INSTALLED_PLUGINS_PATH = "~/.claude/plugins/installed_plugins.json"


def installed_plugins_path() -> Path:
    """The live ``installed_plugins.json`` Claude Code itself maintains, HOME-expanded."""
    return expand_user_path(INSTALLED_PLUGINS_PATH)


def _read_installed_plugins(path: Path) -> dict[str, Any]:
    """Best-effort JSON read of ``installed_plugins.json``.

    A missing file (Claude Code never installed on this machine) or malformed JSON (mid-write,
    corrupted) resolves to "nothing installed" rather than raising — a plugin genuinely absent on
    THIS machine (a CI runner, a laptop without Claude Code) is normal, expected state that
    :func:`resolve_plugin_skills` turns into a plan note, never a fatal ``rig apply`` error.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def resolve_plugin_install_path(installed: dict[str, Any], plugin: str, marketplace: str) -> Path | None:
    """The live on-disk root ``<plugin>@<marketplace>`` currently resolves to, or ``None``.

    See the module docstring for the scope-preference rule: a ``user``-scoped entry wins over a
    ``project``-scoped one when both exist; ties (multiple entries in the winning pool) break on
    the most recent ``lastUpdated`` (falling back to ``installedAt`` when absent) so a stale
    duplicate scope entry never shadows a newer one.
    """
    plugins = installed.get("plugins")
    if not isinstance(plugins, dict):
        return None
    entries = plugins.get(f"{plugin}@{marketplace}")
    if not isinstance(entries, list) or not entries:
        return None
    candidates = [
        e for e in entries
        if isinstance(e, dict) and isinstance(e.get("installPath"), str) and e["installPath"]
    ]
    if not candidates:
        return None
    user_scoped = [e for e in candidates if e.get("scope") == "user"]
    pool = user_scoped or candidates
    best = max(pool, key=lambda e: str(e.get("lastUpdated") or e.get("installedAt") or ""))
    return Path(str(best["installPath"]))


def plugin_skill_dirs(install_path: Path) -> list[Path]:
    """Every ``<install_path>/skills/<name>/`` dir carrying a ``SKILL.md``, sorted by name.

    Mirrors the catalog scanner's own "a skill is a dir with a SKILL.md" contract
    (:mod:`riglib.catalog`) — anything else under ``skills/`` (a stray file, an empty dir) is
    silently not a skill, same as the catalog scanner's own behavior.
    """
    skills_dir = install_path / "skills"
    if not skills_dir.is_dir():
        return []
    return sorted(
        (md.parent for md in skills_dir.glob("*/SKILL.md") if md.is_file()),
        key=lambda p: p.name,
    )


def resolve_plugin_skills(
    plugins_items: dict[str, Any], *, installed_plugins_file: Path | None = None,
) -> tuple[list[tuple[str, str, Path]], list[str]]:
    """Resolve every ``skills.plugins.items.<plugin>`` entry to its live skill source dirs.

    Returns ``(resolved, notes)``:

    - ``resolved`` — a ``(plugin_name, skill_name, skill_source_dir)`` triple per skill the
      plugin currently ships, sorted by plugin name then skill name for a stable plan order.
    - ``notes`` — plain-language strings (destined for ``InstallPlan.notes``, same as the
      existing skill-discovery notes) for a declared plugin NOT found in ``installed_plugins.json``
      (not installed on this machine, or a typo'd marketplace/plugin name) or one that resolves
      but currently ships zero skills. Never a hard failure: a plugin absent on one machine must
      not break ``rig apply`` there.

    ``installed_plugins_file`` overrides the live path — tests point it at a fixture file instead
    of monkeypatching ``HOME``.
    """
    path = installed_plugins_file if installed_plugins_file is not None else installed_plugins_path()
    installed = _read_installed_plugins(path)
    resolved: list[tuple[str, str, Path]] = []
    notes: list[str] = []
    # `key=str`: a malformed config could carry a non-string plugin-name key (e.g. YAML's bare
    # `123:` parses as an int) — `_validate_skills_plugins` only checks each item's VALUE shape,
    # not the key type, so a bare `sorted(plugins_items)` would raise TypeError on a mixed
    # int/str key set and crash `build()` for every repo, not just the malformed one. Sorting by
    # `str(name)` instead keeps this a plan-note skip (a non-string name simply never matches a
    # real plugin) rather than a hard crash.
    for plugin_name in sorted(plugins_items, key=str):
        spec = plugins_items[plugin_name]
        if not isinstance(spec, dict):
            continue
        marketplace = spec.get("marketplace")
        if not isinstance(marketplace, str) or not marketplace:
            continue
        install_path = resolve_plugin_install_path(installed, plugin_name, marketplace)
        if install_path is None:
            notes.append(
                f"skills.plugins.items.{plugin_name}: '{plugin_name}@{marketplace}' not found in "
                f"{path} — not installed on this machine (or a marketplace/plugin name typo); skipped"
            )
            continue
        skill_dirs = plugin_skill_dirs(install_path)
        if not skill_dirs:
            notes.append(
                f"skills.plugins.items.{plugin_name}: resolved to {install_path} but it ships no "
                "skills/*/SKILL.md"
            )
            continue
        for skill_dir in skill_dirs:
            resolved.append((plugin_name, skill_dir.name, skill_dir))
    return resolved, notes
