"""The canonical, COMPLETE JSON Schema for ``rig.yaml`` + the global config — one source.

What this is
------------
A declarative description of EVERY ``rig.yaml`` / ``~/.config/rig/config.yaml`` block: each
top-level key, its fixed sub-keys, their types/enums/defaults, and whether the block is *closed*
(an unknown key is rejected — ``additionalProperties: false``) or carries one named open map such
as ``items:``, ``fragments:``, or ``jobs:``. :func:`json_schema` emits a Draft-07 document from it;
:func:`schema_pointer_for` answers "where is this dotted key in the generated schema?" for error
messages.

Why a SEPARATE module from ``config.py`` and ``schema.py``
---------------------------------------------------------
- ``config.py`` is the runtime VALIDATOR (deep semantic checks: marker collisions, HH:MM ranges,
  catalog cross-refs). It stays the enforcement engine and now consults THIS module for the
  unknown-key check + the schema path on each error, so the two can never disagree on the key set.
- ``schema.py`` is the WIZARD option registry (a flat list of toggles + hints). It exposes only a
  curated subset (what a wizard can prompt for); this module is the EXHAUSTIVE shape (every key an
  editor should complete/validate). ``schema.json_schema()`` delegates here so there is ONE emitter.

The published artifact is ``schema/rig.schema.json`` (committed, referenced by editors). A sync
test (``tests/test_config_schema.py``) asserts the file equals :func:`json_schema` output, so the
generated schema, the on-disk file editors read, and ``config.validate``'s key set never drift.

Stdlib-only at import time (the AGENTS.md hard rule): no yaml/jsonschema here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config_schema_lint import lint_block, lint_checks_block
from .config_schema_model import Block, Leaf
from .config_schema_patterns import (
    GITIGNORE_ENTRY_JSON_PATTERN,
    PERMISSION_RULE_JSON_PATTERN,
    TMUX_PANE_TITLES_FORMAT_UNSAFE_PATTERN,
)
from .config_schema_blocks_core import (
    _DEFAULTS_BLOCK,
    _SKILLS_BLOCK,
    _AGENT_HOOKS_BLOCK,
    _GIT_HOOKS_BLOCK,
    _CI_BLOCK,
    _MCP_BLOCK,
)
from .config_schema_blocks_harness import (
    _HARNESS_BLOCK,
    _PERMISSIONS_BLOCK,
    _MODE_BLOCK,
)
from .config_schema_blocks_dev import (
    _SCRIPTS_BLOCK,
    _DEV_BLOCK,
    _MODELS_BLOCK,
    _AGENTS_MD_BLOCK,
)
from .config_schema_blocks_github import (
    _GITHUB_BLOCK,
)
from .config_schema_blocks_machine import (
    _TMUX_BLOCK,
    _ENV_BLOCK,
    _GITIGNORE_BLOCK,
    _SPOTLIGHT_BLOCK,
    _FJ_BLOCK,
    _BUN_BLOCK,
    _GIT_BLOCK,
    _PROFILE_BLOCK,
    _SHIP_DELEGATOR_BLOCK,
)
from .config_schema_blocks_tools import (
    _LINTERS_BLOCK,
    _ECOSYSTEM_README_BLOCK,
    _PROJECT_TOOLS_BLOCK,
    _TOOLS_BLOCK,
)
from .config_schema_blocks_task import (
    _TASK_BLOCK,
    _TG_CTL_BLOCK,
    _TCC_WATCH_BLOCK,
    _TGCTL_REAPER_BLOCK,
)
from .config_schema_blocks_sysmon import (
    _SYSMON_BLOCK,
)
from .config_schema_blocks_repo import (
    _INTERNAL_DEV_BLOCK,
    _TOP_LEAVES,
    _TRACKED_FILES_BLOCK,
    _HARNESS_WRAPPERS_BLOCK,
)

# ── the published-schema identity (referenced by editors via `$schema`/`$id`) ─────────
SCHEMA_DIALECT = "http://json-schema.org/draft-07/schema#"
SCHEMA_ID = "https://git.hyperide.ai/ultrabricks/rig-cli/raw/branch/main/schema/rig.schema.json"
# The relative path the generator writes to and the validator cites in errors. One constant so
# the CLI, the docs, and the sync test name the same file.
SCHEMA_REL_PATH = "schema/rig.schema.json"
# The same file as package data inside the wheel (see ``schema_file_path``).
PACKAGED_SCHEMA_NAME = "rig.schema.json"

# ── the registry — every rig.yaml block, mirroring config.validate's accepted key set ──
# Kept in lockstep with riglib/config.py's validators by tests/test_config_schema.py (which
# round-trips a config through both). Defaults mirror docs/config-schema.md. The block data lives
# in the ``config_schema_blocks_*`` modules; this module only fixes the published ORDER.
BLOCKS: dict[str, Block] = {
    "defaults": _DEFAULTS_BLOCK,
    "scripts": _SCRIPTS_BLOCK,
    "dev": _DEV_BLOCK,
    "skills": _SKILLS_BLOCK,
    "agent_hooks": _AGENT_HOOKS_BLOCK,
    "git_hooks": _GIT_HOOKS_BLOCK,
    "ci": _CI_BLOCK,
    "mcp": _MCP_BLOCK,
    "mode": _MODE_BLOCK,
    "harness": _HARNESS_BLOCK,
    "permissions": _PERMISSIONS_BLOCK,
    "models": _MODELS_BLOCK,
    "agents_md": _AGENTS_MD_BLOCK,
    "github": _GITHUB_BLOCK,
    "tmux": _TMUX_BLOCK,
    "env": _ENV_BLOCK,
    "gitignore": _GITIGNORE_BLOCK,
    "spotlight": _SPOTLIGHT_BLOCK,
    "bun": _BUN_BLOCK,
    "fj": _FJ_BLOCK,
    "git": _GIT_BLOCK,
    "profile": _PROFILE_BLOCK,
    "tools": _TOOLS_BLOCK,
    "tg_ctl": _TG_CTL_BLOCK,
    "tcc_watch": _TCC_WATCH_BLOCK,
    "tgctl_reaper": _TGCTL_REAPER_BLOCK,
    "sysmon": _SYSMON_BLOCK,
    "internal_dev": _INTERNAL_DEV_BLOCK,
    "ship_delegator": _SHIP_DELEGATOR_BLOCK,
    "linters": _LINTERS_BLOCK,
    "lint_checks": lint_checks_block(),
    "lint": lint_block(),
    "project_tools": _PROJECT_TOOLS_BLOCK,
    "ecosystem_readme": _ECOSYSTEM_README_BLOCK,
    "task": _TASK_BLOCK,
    "tracked_files": _TRACKED_FILES_BLOCK,
    "harness_wrappers": _HARNESS_WRAPPERS_BLOCK,
}

# Every valid TOP-LEVEL key (scalars + blocks). Mirrors config._VALID_TOP_KEYS; the sync test
# asserts the two are identical so the schema and the validator can never disagree.
TOP_LEVEL_KEYS: set[str] = set(_TOP_LEAVES) | set(BLOCKS)


def json_schema() -> dict[str, Any]:
    """Emit the COMPLETE Draft-07 JSON Schema for rig.yaml + the global config (one source).

    Strict by construction: every block is ``additionalProperties: false`` except where it carries
    an open map such as ``items``/``fragments``/``jobs``, so an unknown FIXED key is a schema
    violation an editor flags. ``scope`` is whitelisted at the top level only (a tolerated legacy
    key the loader drops). This is what ``schema/rig.schema.json`` is generated from.
    """
    props: dict[str, Any] = {name: leaf.to_node() for name, leaf in _TOP_LEAVES.items()}
    for name, block in BLOCKS.items():
        props[name] = block.to_node()
    # Named ship-policy keys coexist with the open per-gate catalog map.
    props["ci"]["properties"]["items"]["properties"] = {
        "ship": {
            "type": ["object", "boolean"],
            "properties": {
                "require_external_review": {
                    "type": "boolean", "default": True, "x-rig-global-only": True,
                    "description": "Machine-only GitHub-side external-review policy; other ship gates are unchanged.",
                },
                "external_review_owners": {
                    "type": "array", "default": [], "x-rig-global-only": True,
                    "items": {"type": "string", "pattern": "^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$"},
                    "description": "Exact owner logins covered by a false require_external_review; no wildcards.",
                },
                "external_review_evidence_owners": {
                    "type": "array", "default": [], "x-rig-global-only": True,
                    "items": {"type": "string", "pattern": "^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$"},
                    "description": "Exact owner logins whose zero-review PRs pass the external-review gate on "
                    "evidence (green CI, review quorum, proven acceptance, no open threads/comments); no wildcards.",
                },
            },
        },
    }
    props["mode"]["x-rig-global-only"] = True
    # `scope` is a removed legacy key the loader still TOLERATES (drops it) — whitelist it so a
    # not-yet-cleaned committed file doesn't trip an editor, but it is intentionally undocumented.
    props["scope"] = {"type": "string", "description": "removed legacy key (tolerated, ignored)", "deprecated": True}
    return {
        "$schema": SCHEMA_DIALECT,
        "$id": SCHEMA_ID,
        "title": "rig.yaml — rig-cli declarative config",
        "description": "The committed source of truth for a repo's setup (and ~/.config/rig/config.yaml). "
        "See docs/config-schema.md for the human-readable reference.",
        "type": "object",
        "additionalProperties": False,
        "properties": props,
    }


def schema_pointer_for(dotted: str) -> str | None:
    """A JSON pointer into ``schema/rig.schema.json`` for ``dotted``, or ``None`` if it can't resolve.

    The path is walked through the block registry, prefixing ``/properties/`` between resolvable
    nodes. Walking STOPS at an open map — segments past it (item names like ``secret-scan`` or
    ``smoke``) are NOT schema nodes, so the pointer would dangle. We return a
    pointer to the open-map node itself in that case (the deepest node that actually exists), which
    is honest: the editor jumps to ``…/items``, not a non-existent ``…/items/secret-scan``. An
    unknown top block → ``None`` (the caller then shows the dotted path without a dangling pointer).
    """
    parts = dotted.split(".")
    top = parts[0]
    if top in _TOP_LEAVES:
        return f"/properties/{top}"  # a top-level scalar leaf
    block = BLOCKS.get(top)
    if block is None:
        return None
    pointer = [top]
    for part in parts[1:]:
        if part in block.leaves:
            pointer.append(part)
            return "/properties/" + "/properties/".join(pointer)  # a leaf — done
        if block.open_map and part == block.open_map:
            pointer.append(part)
            return "/properties/" + "/properties/".join(pointer)  # the open map node itself
        if part in block.nested:
            pointer.append(part)
            block = block.nested[part]
            continue
        if block.open_map:
            # we're already inside an open map (a prior segment was the map key) — the deepest
            # resolvable node is the map; stop here rather than emit item-name segments.
            pointer.append(block.open_map)
            return "/properties/" + "/properties/".join(pointer)
        # an unknown fixed segment (the typo itself): point at the PARENT block, which exists.
        return "/properties/" + "/properties/".join(pointer) if len(pointer) > 1 else f"/properties/{top}"
    return "/properties/" + "/properties/".join(pointer)


def block_child_keys(block_path: str) -> set[str] | None:
    """The valid child keys of a block addressed by dotted path (``github.ruleset`` → its keys).

    Returns ``None`` when the path does not name a known block (so the caller can distinguish
    "unknown block" from "known block, here are its keys"). Used by the validator to build the
    unknown-key error's "expected one of …" hint from the SAME registry the schema is emitted from.
    """
    if not block_path:
        return None
    parts = block_path.split(".")
    top = parts[0]
    block = BLOCKS.get(top)
    if block is None:
        return None
    for part in parts[1:]:
        if block.open_map and part == block.open_map and block.open_map_item is not None:
            block = block.open_map_item
            continue
        block = block.nested.get(part)
        if block is None:
            return None
    return block.child_keys()


def is_source_checkout() -> bool:
    """True iff rig runs from a rig-cli checkout (rig's own ``pyproject.toml`` beside ``riglib/``), not an installed wheel."""
    try:
        text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return 'name = "hyper-rig"' in text


def schema_file_path() -> Path:
    """Absolute path of the schema file ``rig schema --check`` verifies.

    A rig-cli checkout (``pyproject.toml`` beside ``riglib/``; the canonical install is a SYMLINK to it)
    uses the committed ``schema/rig.schema.json``. An installed wheel has no ``schema/`` directory; it
    carries the SAME bytes as package data ``riglib/rig.schema.json`` (copied from the committed file by
    ``setup.py`` at build time), so the check works wherever rig is installed.
    A missing or altered file is drift: ``schema_file_in_sync`` is False. Runtime ``config.validate`` is
    registry-based and never reads it.
    """
    root = Path(__file__).resolve().parent.parent
    if is_source_checkout():
        return root / SCHEMA_REL_PATH
    return Path(__file__).resolve().parent / PACKAGED_SCHEMA_NAME


def render_schema_json() -> str:
    """The canonical on-disk serialization of the schema: 2-space indent, trailing newline.

    One renderer so :func:`write_schema_file`, ``rig schema``, and the sync test all produce/compare
    byte-identical text (a mismatch is what the sync test catches). ``sort_keys=False`` keeps the
    blocks in registry order (readable), ``ensure_ascii=False`` keeps the unicode ellipses intact.
    """
    return json.dumps(json_schema(), indent=2, sort_keys=False, ensure_ascii=False) + "\n"


def write_schema_file() -> Path:
    """(Re)generate the committed ``schema/rig.schema.json`` from the registry. Returns the written path.

    Only a rig-cli checkout has a committed file; an installed wheel refuses (never writes into site-packages).
    """
    if not is_source_checkout():
        raise OSError("this install has no rig-cli checkout; run `rig schema --write` from a checkout")
    path = schema_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_schema_json(), encoding="utf-8")
    return path


def schema_file_in_sync() -> bool:
    """True iff the committed schema file exists and equals the freshly generated text.

    The drift guard: a registry change that forgets to regenerate the file (or a hand-edit to the
    file) makes this False, so ``rig schema --check`` and the sync test fail loudly.
    """
    try:
        # CRLF from a core.autocrlf checkout is not drift; any other byte difference (incl. invalid UTF-8) is.
        return schema_file_path().read_bytes().replace(b"\r\n", b"\n") == render_schema_json().encode("utf-8")
    except OSError:
        return False


__all__ = [
    "SCHEMA_DIALECT",
    "SCHEMA_ID",
    "SCHEMA_REL_PATH",
    "PACKAGED_SCHEMA_NAME",
    "is_source_checkout",
    "Leaf",
    "Block",
    "GITIGNORE_ENTRY_JSON_PATTERN",
    "PERMISSION_RULE_JSON_PATTERN",
    "TMUX_PANE_TITLES_FORMAT_UNSAFE_PATTERN",
    "BLOCKS",
    "TOP_LEVEL_KEYS",
    "json_schema",
    "block_child_keys",
    "schema_pointer_for",
    "schema_file_path",
    "render_schema_json",
    "write_schema_file",
    "schema_file_in_sync",
]
