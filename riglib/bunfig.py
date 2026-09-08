"""``~/.bunfig.toml`` — bun's isolated linker + global virtual store, as a spliced managed block.

Accessed via: ``rig apply`` → :func:`riglib.plan._build_bun` emits one
``provision_bunfig`` action → :func:`riglib.actions.runner._do_provision_bunfig`
splices the block. ``rig status`` / verify read the same :func:`resolve` state.

Assumptions: bun package-manager commands read ``$HOME/.bunfig.toml``, or
``$XDG_CONFIG_HOME/.bunfig.toml`` when that env is set (bun's documented global
paths). A project's own ``bunfig.toml`` overrides the same keys.

The managed block sets both ``linker = "isolated"`` and ``globalStore = true``.
Bun's global store is a no-op under the hoisted linker
(https://bun.sh/docs/pm/global-store), so both keys are required for worktrees
to share one package tree. Opt-in (``bun.enabled``, default OFF, GLOBAL-only)
because isolated linking can surface phantom deps in previously-hoisted projects.

uv already uses a global cache (``~/.cache/uv``). macOS default link-mode is
clone (APFS CoW; ``nlink=1`` so ``du`` still looks like copies).
``link-mode = hardlink`` in ``~/.config/uv/uv.toml`` shares inodes. Out of
scope to provision uv.toml.

Past bugs: none in-tree yet. Unbalanced markers are a ``conflict`` and apply
leaves the file untouched (same rule as the global-excludes splice).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

BUNFIG_NAME = ".bunfig.toml"
BEGIN_MARKER = "# >>> rig-managed bun (do not edit) >>>"
END_MARKER = "# <<< rig-managed bun (do not edit) <<<"
BLOCK_COMMENT = (
    "# bun install: isolated linker + globalStore — packages live once under "
    "~/.bun/install/cache/links/."
)

_STORE_LINE_RE = re.compile(r"^globalStore\s*=\s*true\s*(?:#.*)?$")
_LINKER_LINE_RE = re.compile(r"^linker\s*=\s*[\"\']isolated[\"\']\s*(?:#.*)?$")
_TABLE_RE = re.compile(r"^\[([^\]]+)\]\s*(?:#.*)?$")


def bunfig_path() -> Path:
    """bun's global bunfig: ``$XDG_CONFIG_HOME/.bunfig.toml`` if set, else ``~/.bunfig.toml``."""
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return Path(xdg) / BUNFIG_NAME
    return Path.home() / BUNFIG_NAME


def managed_block() -> str:
    """The exact marker-bounded region rig owns, including the markers."""
    return "\n".join(
        [
            BEGIN_MARKER,
            BLOCK_COMMENT,
            "[install]",
            'linker = "isolated"',
            "globalStore = true",
            END_MARKER,
        ]
    )


def has_install_table(text: str) -> bool:
    """True when the file already has a top-level ``[install]`` table (not a subtable)."""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        table = _TABLE_RE.match(line)
        if table and table.group(1) == "install":
            return True
    return False


def _in_install_matches(text: str, line_re: re.Pattern[str]) -> bool:
    in_install = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        table = _TABLE_RE.match(line)
        if table:
            in_install = table.group(1) == "install"
            continue
        if in_install and line_re.match(line):
            return True
    return False


def has_install_global_store(text: str) -> bool:
    """True when ``[install]`` (not a subtable) contains ``globalStore = true``.

    Commented-out lines and ``globalStore`` under any other table do not count.
    """
    return _in_install_matches(text, _STORE_LINE_RE)


def has_install_isolated_linker(text: str) -> bool:
    """True when ``[install]`` (not a subtable) contains ``linker = "isolated"``."""
    return _in_install_matches(text, _LINKER_LINE_RE)


def has_required_install_keys(text: str) -> bool:
    """True when ``[install]`` has both ``linker = "isolated"`` and ``globalStore = true``."""
    return has_install_isolated_linker(text) and has_install_global_store(text)


@dataclass(frozen=True)
class ResolveResult:
    """One inspect of the global bunfig. ``state`` is the apply/drift switch."""

    state: str  # create | ok | update | conflict
    path: Path
    detail: str
    new_content: str = ""


def resolve(path: Path | None = None) -> ResolveResult:
    """Classify the bunfig at ``path`` (default :func:`bunfig_path`). Never writes.

    - ``create``: missing, or present with no ``[install]`` table and no managed block —
      apply writes/appends :func:`managed_block`.
    - ``ok``: already has ``[install] linker = "isolated"`` and ``globalStore = true``.
    - ``update``: a managed block exists but differs — apply splices it back.
    - ``conflict``: unbalanced markers, a non-file, or a hand-written ``[install]``
      table missing either required key (a second ``[install]`` header would be invalid TOML).
    """
    from .actions.runner import _splice_managed_block

    target = path if path is not None else bunfig_path()
    desired = managed_block()
    if not target.exists():
        return ResolveResult("create", target, f"missing {target}", new_content=desired + "\n")
    if not target.is_file():
        return ResolveResult("conflict", target, f"{target} exists but is not a file")
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return ResolveResult("conflict", target, f"cannot read {target}: {exc}")

    new, state, _splice_detail = _splice_managed_block(
        text, desired, BEGIN_MARKER, END_MARKER
    )
    if state == "ok":
        return ResolveResult("ok", target, f"managed block current: {target}")
    if state == "conflict":
        return ResolveResult(
            "conflict",
            target,
            f"unbalanced bun managed-block markers in {target} — reconcile by hand, then re-apply",
        )
    # Handwritten (no markers → splice-create) with both keys: leave it, no ownership.
    # splice-update must still rewrite a stale/tampered managed block.
    if state == "create" and has_required_install_keys(text):
        return ResolveResult("ok", target, f"isolated linker + globalStore already set: {target}")
    # Only on splice-create: a user [install] we would APPEND a second header to.
    # splice-update means OUR managed block already contains [install]; scanning the
    # whole file would false-conflict on that table and skip rewriting a stale block.
    if state == "create" and has_install_table(text):
        return ResolveResult(
            "conflict",
            target,
            f"{target} already has [install] without linker = \"isolated\" and "
            "globalStore = true — add those keys under your existing [install] by hand "
            "(a second [install] header would be invalid TOML)",
        )
    return ResolveResult(
        state,
        target,
        f"managed bunfig block will be {state}d in {target}",
        new_content=new if new.endswith("\n") else new + "\n",
    )
