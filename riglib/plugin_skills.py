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


def _plugins_root() -> Path:
    """The Claude Code plugin store root every genuine ``installPath`` lives under
    (``~/.claude/plugins/``), HOME-expanded — the trust boundary an ``installPath`` value read
    from ``installed_plugins.json`` must stay inside of. See :func:`resolve_plugin_skills`."""
    return expand_user_path("~/.claude/plugins")


def _confined_to(path: Path, root: Path) -> bool:
    """True when ``path`` resolves to somewhere inside ``root`` (symlinks followed on both sides).

    Security guard (rig-cli#590 review finding): ``installed_plugins.json`` is Claude Code's own
    file, not rig's, and a plugin's own extracted tree can ship a directory symlink. Without this,
    a corrupted/compromised ``installPath`` (or a ``skills/<name>`` entry that is itself a symlink
    escaping the plugin's install root) would become a global skill symlink source planted into
    every harness's discovery dir — the confused-deputy pattern :func:`riglib.config.
    linter_path_escapes_repo` guards against for a different (repo-relative) path shape. An
    unresolvable path (a broken symlink, a permissions error) is treated as NOT confined — fail
    closed, never silently trust an ambiguous path.
    """
    try:
        return path.resolve(strict=False).is_relative_to(root.resolve(strict=False))
    except OSError:
        return False


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

    Confinement (security review finding, rig-cli#590): a candidate whose resolved path lands
    OUTSIDE ``install_path`` is excluded, not followed. A marketplace plugin archive can ship a
    directory symlink (``skills/pwned -> /somewhere/else``) inside its own extracted tree —
    without this check that would plant a global skill symlink pointing outside the plugin's own
    install root the moment :mod:`riglib.plan` links it into ``skills_target``.
    """
    skills_dir = install_path / "skills"
    if not skills_dir.is_dir():
        return []
    out = [
        md.parent for md in skills_dir.glob("*/SKILL.md")
        if md.is_file() and _confined_to(md.parent, install_path)
    ]
    return sorted(out, key=lambda p: p.name)


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
        if not _confined_to(install_path, _plugins_root()):
            # Security review finding (rig-cli#590): `installed_plugins.json` is Claude Code's
            # own file, not rig's — a corrupted write or a compromised plugin could point
            # `installPath` anywhere on disk. Never let a path outside the plugin store become
            # a global skill symlink source; degrade to a plan note, same as "not found".
            notes.append(
                f"skills.plugins.items.{plugin_name}: installPath {install_path} is outside "
                f"{_plugins_root()} — refusing to symlink from an untrusted/corrupted "
                "installed_plugins.json entry; skipped"
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
