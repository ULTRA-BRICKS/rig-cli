"""Config cascade loader + schema validation for ``rig.yaml``.

Two layers, cascaded by **location** (no scope flag):

1. **Global** — ``~/.config/rig/config.yaml`` (or ``$XDG_CONFIG_HOME/rig/config.yaml``).
   Machine-wide defaults a developer carries across repos.
2. **Per-repo** — ``rig.yaml`` at the repo root. Committed by default; it is the
   reproducible source of truth and **overrides** the global layer.

The merge is a deep dict merge: per-repo keys win, dicts merge recursively, scalars and
lists replace wholesale (a list in rig.yaml fully replaces the global list — lists are
treated as atomic decisions, not appended, to keep the result predictable).

``yaml`` is imported lazily so ``rig --help``/``doctor`` work even if PyYAML is missing.
Validation is **fail-closed**: unknown top-level keys, unknown categories, and invalid
enum values abort before anything touches disk.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .harness_skills import KNOWN_HARNESS_KINDS as _KNOWN_HARNESS_KINDS
from .permissions import (
    HARNESS_ALLOWLIST_NA,
    HARNESS_ALLOWLISTS,
    HARNESS_EXECPOLICY,
    HARNESS_GUARD,
    HARNESS_INSTRUCTION_POLICY,
)
from .project_tools import (
    HAFT_KEYS,
    HAFT_WORKFLOW_KEYS,
    HAFT_WORKFLOW_MODES,
    PROJECT_TOOLS_KEYS as PROJECT_TOOLS_KEYS,
    SERENA_KEYS,
    SVERKLO_KEYS,
)
from .shell_env import ENV_VAR_KEY_PATTERN as _ENV_VAR_KEY_PATTERN

CONFIG_FILENAME = "rig.yaml"

_VALID_TOP_KEYS = {
    "version",
    "defaults",
    "agent_tools_source",
    "stack",
    "skills",
    "agent_hooks",
    "git_hooks",
    "ci",
    "mcp",
    "mode",
    "harness",
    "permissions",
    "scripts",
    "dev",
    "models",
    "agents_md",
    "github",
    "tmux",
    "env",
    "gitignore",
    "spotlight",
    "bun",
    "tools",
    "tg_ctl",
    "tcc_watch",
    "ship_delegator",
    "linters",
    "project_tools",
    "ecosystem_readme",
    "scripts",
    "dev",
    "task",
}
_VALID_CATEGORIES = {"skills", "agent_hooks", "git_hooks", "ci", "mcp"}
_VALID_ON_CONFLICT = {"skip", "overwrite", "backup"}
_VALID_TIERS = {"block", "warn"}
_VALID_ON_ERROR = {"open", "closed"}
_VALID_TASK_BACKENDS = {"github-issues", "linear"}
_VALID_TASK_ATTACHMENT_MODES = {"link", "native"}
_VALID_LEGACY_AGENT_HOOK_TARGET_KINDS = {"claude-code", "generic"}
_MCP_ITEM_KEYS = {"enabled", "server", "command", "args", "env"}
_VALID_TMUX_APPLY = {"import", "block"}
_VALID_MODE_NAMES = {"standard", "autonomous"}
_VALID_AUTONOMOUS_UNTIL = {"clean", "budget", "manual"}
# Harness kinds rig knows a skill/instruction discovery convention for — the union of the
# skills-DIRECTORY harnesses (claude-code, codex), the NATIVE-DISCOVERY harnesses
# (opencode, omp), and the INSTRUCTION-FILE harnesses (pi, commandcode). A
# ``harness.kind`` in this set is ACCEPTED: rig can
# provision skill discovery (and, for the supported kinds, the auto-mode write / allowlist)
# for it. The single source of truth is :mod:`riglib.harness_skills`. The auto-mode WRITE is
# keyed off :mod:`riglib.harness_mode` — a kind with no such setting (pi/commandcode) gets a
# visible plan note, not a crash.
_VALID_HARNESS_KINDS = set(_KNOWN_HARNESS_KINDS)
# No kind is "reserved + rejected" any longer — every documented kind is now provisionable for
# skills. Kept as an (empty) set so the validator's reserved-kind branch stays well-defined.
_RESERVED_HARNESS_KINDS: set[str] = set()
# DEPRECATED harness kinds — removed everywhere (CTO 2026-07). Still recognized ONLY so a config
# that names one fails with a helpful "no longer supported (deprecated)" message instead of the
# generic typo error. gemini was an instruction-file harness (~/.gemini/GEMINI.md); it is gone.
_DEPRECATED_HARNESS_KINDS: dict[str, str] = {
    "gemini": "Gemini is deprecated and no longer provisioned by rig.",
}


class ConfigError(ValueError):
    """Raised on a malformed/invalid config (fail-closed before any write).

    A 3-part, renderable error (WHAT / WHY / FIX) that also names the SCHEMA PATH of the
    offending key. The roadmap (§5 "ENFORCED JSON schema") asks every rejection to say what is
    wrong, where (the dotted schema path, e.g. ``harness.auto_mode``), and how to fix it — so a
    malformed config fails LOUDLY, not silently.

    Subclass of :class:`ValueError` for backward-compat: every existing ``except ConfigError``
    (and ``except (ConfigError, …)``) keeps catching it. ``str(err)`` stays the one-line WHAT, so
    legacy ``error: {exc}`` renderers are unchanged; the richer renderer (:func:`render_config_error`)
    shows the why/fix/schema path when present.

    ``schema_path`` is the dotted path into the config tree the JSON schema indexes
    (``defaults.on_conflict``, ``github.ruleset.required_reviews``) — the SAME path an editor would
    show from ``schema/rig.schema.json``. ``why``/``fix`` carry the root cause and a concrete remedy.
    A plain ``ConfigError("msg")`` (no kwargs) still works — why/fix/schema_path default empty.
    """

    def __init__(
        self,
        what: str,
        *,
        why: str = "",
        fix: str = "",
        schema_path: str = "",
    ) -> None:
        super().__init__(what)
        self.what = what
        self.why = why
        self.fix = fix
        self.schema_path = schema_path

    def __str__(self) -> str:
        return self.what


def render_config_error(err: ConfigError, *, color: bool = True) -> str:
    """Render a :class:`ConfigError` as the consistent 3-part block (what / why / fix + path).

    Always shows the WHAT (prefixed ``error:``). Shows the SCHEMA PATH on the ``why`` line and the
    FIX line only when populated, so a terse error doesn't print empty labels. Mirrors
    :func:`riglib.errors.render` (the structured-error renderer) so config errors and catalog/plan
    errors read identically. The path is shown as ``schema/rig.schema.json#/<json-pointer>`` so the
    user can jump straight to the offending node in the published schema.
    """

    def _col(code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if color else s

    lines = [_col("31", f"error: {err.what}")]
    why = err.why
    if err.schema_path:
        from . import config_schema

        # The pointer is resolved through the registry so it stops at an open `items`/`fragments`
        # map (item names aren't schema nodes) and never dangles. A path that can't resolve at all
        # shows the dotted key alone — better than a pointer to a node that doesn't exist.
        pointer = config_schema.schema_pointer_for(err.schema_path)
        if pointer is not None:
            loc = f"schema path: {err.schema_path}  ({config_schema.SCHEMA_REL_PATH}#{pointer})"
        else:
            loc = f"schema path: {err.schema_path}"
        why = f"{why}; {loc}" if why else loc
    if why:
        lines.append(_col("2", "  why: ") + why)
    if err.fix:
        lines.append(_col("32", "  fix: ") + err.fix)
    return "\n".join(lines)


def _reject_unknown_keys(block: dict[str, Any], block_path: str) -> None:
    """Reject any key in ``block`` that the schema does not declare for ``block_path``.

    The single unknown-key gate, sourced from :mod:`riglib.config_schema` (the SAME registry the
    published JSON schema is emitted from), so the validator and ``schema/rig.schema.json`` can
    never disagree on what is a typo. Raises a 3-part :class:`ConfigError` whose schema path points
    at the FIRST offending key and whose fix lists the keys that block DOES accept. ``block_path``
    is the dotted path of the parent block (``""`` for the document root, ``github.ruleset`` for a
    nested block).

    A block carrying an OPEN ``items``/``fragments`` map (ci, mcp, agent_hooks, skills.by_type,
    git_hooks.dispatcher) keeps that map key as valid — only OTHER unknown keys are rejected.
    """
    from . import config_schema

    if block_path:
        valid = config_schema.block_child_keys(block_path)
    else:
        valid = set(config_schema.TOP_LEVEL_KEYS) | {"scope"}  # tolerate the legacy top key
    if valid is None:
        return  # not a registry-known block (defensive — caller passes a known path)
    unknown = sorted(set(block) - valid)
    if not unknown:
        return
    bad = unknown[0]
    # "top-level" at the document root, else the dotted block path ("github.ruleset").
    where = block_path or "top-level"
    key_path = f"{block_path}.{bad}" if block_path else bad
    plural = "key" if len(unknown) == 1 else "keys"
    accepted = ", ".join(sorted(valid - {"scope"}))
    raise ConfigError(
        f"unknown {where} {plural}: {', '.join(unknown)}",
        why=f"{', '.join(unknown)} {'is' if len(unknown) == 1 else 'are'} not a known "
        f"{where} key — likely a typo (validated against {config_schema.SCHEMA_REL_PATH})",
        fix=f"remove it, or use one of: {accepted}",
        schema_path=key_path,
    )


def _check_bool(block: dict[str, Any], key: str, path: str) -> None:
    """Fail-closed if ``block[key]`` is present and not a bool, naming the schema ``path``.

    A tiny shared guard so every block's bool knob (``enabled``/``all``/…) rejects a typo'd value
    with the schema path attached — the runtime and the JSON schema then agree on the type. NB:
    ``bool`` is an int subclass, so this also rejects an int posing as a bool only implicitly; use
    a dedicated check where an int and a bool must be told apart (e.g. ``required_reviews``).
    """
    value = block.get(key)
    if value is not None and not isinstance(value, bool):
        raise ConfigError(f"{path} must be a bool, got {value!r}", schema_path=path)


def _check_str(block: dict[str, Any], key: str, path: str) -> None:
    """Fail-closed if ``block[key]`` is present and not a string, naming the schema ``path``."""
    value = block.get(key)
    if value is not None and not isinstance(value, str):
        raise ConfigError(f"{path} must be a string, got {value!r}", schema_path=path)


def global_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "rig" / "config.yaml"


def repo_config_path(repo_root: Path) -> Path:
    return repo_root / CONFIG_FILENAME


# ── dot-path get/set + scalar coercion (the `rig config get|set` engine) ────────────
# A dot path indexes the YAML mapping tree: ``harness.mode`` → ``data["harness"]["mode"]``.
# Used ONLY for the targeted config edit/read commands — not for the cascade loader, which
# always merges whole layers. Kept here (next to the loader/validator) so the path syntax
# and the schema it indexes never drift into a separate module.


def split_path(dotted: str) -> list[str]:
    """Split and normalize a dot path into trimmed segments, rejecting empty segments.

    Fail-closed: a malformed path is a :class:`ConfigError`, not a silent no-op — a typo in
    ``rig config set`` must abort before it writes a key the schema can't reach.
    """
    if not dotted or not dotted.strip():
        raise ConfigError("empty config path")
    parts = [p.strip() for p in dotted.split(".")]
    if any(p == "" for p in parts):
        # empty segment ("a..b", a leading/trailing dot, or a whitespace-only segment "a. .b")
        raise ConfigError(f"invalid config path {dotted!r}: empty segment")
    return parts


def canonical_dot_path(dotted: str) -> str:
    """Return the trimmed, single-dot-joined canonical form of a config dot path."""
    return ".".join(split_path(dotted))


def get_path(data: dict[str, Any], dotted: str) -> Any:
    """Read a nested value by dot path. Raises :class:`ConfigError` if the path is absent.

    Traversal fails closed when an intermediate segment is missing OR is a non-mapping
    (``a.b`` where ``a`` is a scalar/list) — both are "the path does not exist", reported
    with the exact segment that broke so the caller can fix the typo.
    """
    node: Any = data
    walked: list[str] = []
    for seg in split_path(dotted):
        if not isinstance(node, dict) or seg not in node:
            where = ".".join(walked) or "<root>"
            raise ConfigError(f"config path {dotted!r} not found (no {seg!r} under {where})")
        node = node[seg]
        walked.append(seg)
    return node


⚠ 1 unresolved conflict detected
- ours = HEAD
- theirs = a530cf2 (feat(task): add task.code_prefix, reconciled into .ship-config)
- base = parent of a530cf2 (feat(task): add task.code_prefix, reconciled into .ship-config)
NOTICE: Inspect a block by reading `conflict://<N>` (add `/ours` / `/theirs` / `/base` to render a single side). Resolve with `write({ path: "conflict://<N>", content })`, or bulk-resolve every registered conflict with `write({ path: "conflict://*", content })`. Writes replace ONLY the marker block (markers + all sides) — never repeat the lines before/after it; they stay in place.
`content` shorthand: a line that is exactly `@ours` / `@theirs` / `@base` / `@both` expands to that recorded section. `@both` is ours-then-theirs with no separator — only for additive conflicts where each side adds something different; NEVER for competing edits of the same lines (pick a side or write the combined text). Lines that are not a token pass through verbatim, so `"// keep both\n@ours\n@theirs"` literally writes the comment, then ours, then theirs.
Per-id bulk: `write({ path: "conflict://*", content: "1: @ours\n2: @theirs\n…" })` resolves each listed id with that side in ONE call — the cheapest way through many pick-one conflicts; unlisted ids stay registered.
Resolve each block faithfully: keep one side (`@ours`/`@theirs`), or combine them when both intents apply — never invent content beyond the recorded sides, and never stack both sides of competing edits. Resolve several conflicts in a single turn by issuing multiple `write` calls at once; ids stay valid as earlier blocks are resolved.

──── #1  L76-81 ────
<<< ours
    "ecosystem_readme",
=== base
(empty)
>>> theirs
    "task",

[Showing lines 1-300 of 3034. Use :301 to continue]