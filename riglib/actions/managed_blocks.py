"""Marker-delimited managed blocks: the splice primitive, execpolicy rules and the ecosystem README block."""

from __future__ import annotations

import shutil
from pathlib import Path

from . import fsutil
from .. import ecosystem_readme
from ..plan import Action
from .git_exclude import _find_marker_lines
from .result import ActionResult
from .shared import _should_backup


# ── codex execpolicy .rules provisioning (~/.codex/rules/rig-managed.rules) ────────────────────
# codex auto-scans ~/.codex/rules/*.rules (Starlark) at startup. rig owns a MARKER-DELIMITED block
# of prefix_rule(...) lines in a DEDICATED rig-managed.rules file: safe-command `allow` (the resolved
# tool set) + a minimal coarse `forbidden` deny (see riglib.permissions). The block is spliced with
# the SAME offset machinery the global-excludes / ship-delegator managed blocks use (a SYNC family:
# _find_marker_lines + collapse-to-one), so a re-apply is a byte-identical no-op and any lines a user
# put OUTSIDE the markers are preserved verbatim.
EXECPOLICY_BEGIN_MARKER = "# >>> rig-managed execpolicy (do not edit) >>>"

EXECPOLICY_END_MARKER = "# <<< rig-managed execpolicy (do not edit) <<<"

EXECPOLICY_BLOCK_COMMENT = (
    "# rig provisions safe-command allow + coarse deny prefix_rules here. Flag-position guards "
    "(force-push, --no-verify anywhere) live in the PreToolUse hook bridge."
)


def execpolicy_rules_file(action: Action) -> Path:
    """The .rules file a ``provision_execpolicy`` action targets (shared by apply + drift)."""
    return action.target


def execpolicy_block_text(action: Action) -> str:
    """The exact marker-delimited block rig owns for this action (no trailing newline).

    Single source of truth shared by the install handler and drift, so both agree byte-for-byte.
    The block is the begin marker, a fixed explanatory comment, one ``prefix_rule(...)`` line per
    resolved rule (allow tools then coarse deny — via :func:`riglib.permissions.execpolicy_rule_lines`),
    then the end marker.
    """
    from ..permissions import execpolicy_rule_lines

    kind = str(action.options.get("kind", "codex"))
    tools = [str(t) for t in action.options.get("tools", [])]
    lines = execpolicy_rule_lines(kind, tools)
    return "\n".join([EXECPOLICY_BEGIN_MARKER, EXECPOLICY_BLOCK_COMMENT, *lines, EXECPOLICY_END_MARKER])


def _do_provision_execpolicy(action: Action, on_conflict: str) -> ActionResult:
    """Reconcile rig's managed execpolicy block into the codex .rules file — idempotent, additive.

    Mirrors the global-excludes reconcile: append the block to a file that lacks it (creating the
    file/dirs), collapse any duplicated managed regions to ONE correct block in place, or no-op a
    correct single block. Every line OUTSIDE the markers is preserved verbatim. An unbalanced /
    misordered marker pair is a hard error rig refuses to guess at. ``on_conflict=backup`` keeps a
    timestamped copy before a rewrite; ``skip`` leaves a drifted block untouched (drift stays visible).
    """
    desired = execpolicy_block_text(action)
    path = execpolicy_rules_file(action)
    path.parent.mkdir(parents=True, exist_ok=True)

    if not path.exists():
        path.write_text(desired + "\n", encoding="utf-8")
        return ActionResult(action, "created", f"execpolicy/{action.item}: wrote {path}")
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            content = fh.read()
    except OSError as exc:
        return ActionResult(action, "error", f"execpolicy/{action.item}: cannot read {path}: {exc}")

    new_content, state, detail = _splice_managed_block(
        content, desired, EXECPOLICY_BEGIN_MARKER, EXECPOLICY_END_MARKER
    )
    if state == "conflict":
        return ActionResult(action, "error", f"execpolicy/{action.item}: {detail}")
    if state == "ok":
        return ActionResult(action, "skipped", f"execpolicy/{action.item}: already current in {path}")
    # ``create`` = the file has no managed block yet → APPEND it (additive, never touches the user's
    # own lines), exactly like the fresh-file branch above; ``on_conflict`` does NOT gate an additive
    # add (mirrors the global-excludes / ship-delegator appenders — else a user who pre-seeded the
    # file would get NO deny baseline under ``skip`` while a clean machine does). Only ``update``
    # REWRITES an existing managed block, so only that honors ``skip`` (leave the drift) / ``backup``.
    backup_note = ""
    if state == "update":
        if on_conflict == "skip":
            return ActionResult(
                action, "skipped",
                f"execpolicy/{action.item}: {path} block differs but on_conflict=skip — left unchanged (drift NOT reconciled)",
            )
        if _should_backup(on_conflict):
            bak = fsutil.backup_path(path)
            shutil.copy2(str(path), str(bak))
            backup_note = f" (backed up prior → {bak})"
    path.write_text(new_content, encoding="utf-8")
    status = "backed_up" if backup_note else "updated"
    verb = "appended block to" if state == "create" else "reconciled block in"
    return ActionResult(action, status, f"execpolicy/{action.item}: {verb} {path}{backup_note}")


def _splice_managed_block(
    content: str, desired: str, begin_marker: str, end_marker: str
) -> tuple[str, str, str]:
    """Splice ``desired`` into ``content`` between the markers — returns ``(new_content, state, detail)``.

    ``state`` is ``ok`` (already exactly one correct block — ``new_content`` is unused),
    ``create``/``update`` (``new_content`` is the reconciled file), or ``conflict`` (unbalanced /
    misordered markers — ``detail`` says why, ``new_content`` unused). Every line OUTSIDE the managed
    region survives byte-for-byte; duplicated managed regions collapse to one. SYNC family with
    :func:`resolve_global_excludes` / :func:`_reconcile_ship_exclude` (same offset machinery)."""
    begins = _find_marker_lines(content, begin_marker)
    ends = _find_marker_lines(content, end_marker)
    if len(begins) != len(ends) or (ends and not begins):
        return "", "conflict", "unbalanced rig-managed markers — reconcile by hand, then re-run"
    if not begins:
        body = content.rstrip("\n")
        sep = "\n\n" if body else ""
        return f"{body}{sep}{desired}\n", "create", ""
    pairs = _pair_marker_regions(begins, ends)
    if isinstance(pairs, str):
        return "", "conflict", pairs
    if len(pairs) == 1 and content[pairs[0][0] : pairs[0][1]] == desired:
        return "", "ok", ""
    out: list[str] = []
    cursor = 0
    for idx, (r_start, r_end) in enumerate(pairs):
        out.append(content[cursor:r_start])
        if idx == 0:
            out.append(desired)
        elif content[r_end : r_end + 1] == "\n":
            r_end += 1
        cursor = r_end
    out.append(content[cursor:])
    return "".join(out), "update", ""


def _pair_marker_regions(begins: list, ends: list) -> list[tuple[int, int]] | str:
    """Pair begin/end markers into ``(start, end)`` regions, or return the conflict detail."""
    markers = sorted([(b[0], b[1], "begin") for b in begins] + [(e[0], e[1], "end") for e in ends])
    pairs: list[tuple[int, int]] = []
    expect = "begin"
    pending = -1
    for start, line_end, kind in markers:
        if kind != expect:
            return "misordered/nested rig-managed markers — reconcile by hand, then re-run"
        if kind == "begin":
            pending = start
            expect = "end"
        else:
            pairs.append((pending, line_end))
            expect = "begin"
    return pairs


# ── README ecosystem-block provisioning (rig-cli#308) ──────────────────────────────────────────
# Writes/updates a marker-delimited "## Ecosystem" section into a target repo's README.md from the
# ONE canonical registry in riglib.ecosystem_readme. Reuses the SAME `_splice_managed_block` offset
# machinery the execpolicy / global-excludes / ship-delegator managed blocks use, so a re-apply
# with an unchanged registry is a byte-identical no-op and any content a user put OUTSIDE the
# markers (their own README prose) survives verbatim.
ECOSYSTEM_README_BEGIN_MARKER = ecosystem_readme.ECOSYSTEM_README_BEGIN_MARKER

ECOSYSTEM_README_END_MARKER = ecosystem_readme.ECOSYSTEM_README_END_MARKER


def ecosystem_readme_file(action: Action) -> Path:
    """The README a ``apply_ecosystem_readme`` action targets (shared by apply + drift)."""
    return action.target


def ecosystem_readme_block_text(action: Action) -> str:
    """The exact marker-delimited block rig owns for this action (no trailing newline).

    Single source of truth shared by the install handler and drift, so both agree byte-for-byte.
    Content is entirely registry-driven (no per-repo variation), unlike execpolicy's per-kind
    tool set.
    """
    return ecosystem_readme.render_ecosystem_block()


def _do_apply_ecosystem_readme(action: Action, on_conflict: str) -> ActionResult:
    """Reconcile rig's managed Ecosystem block into the target README.md — idempotent, additive.

    Mirrors ``_do_provision_execpolicy`` exactly: append the block to a README that lacks it
    (creating the file/dirs), collapse any duplicated managed regions to ONE correct block in
    place, or no-op a correct single block. Every line OUTSIDE the markers is preserved verbatim.
    An unbalanced/misordered marker pair is a hard error rig refuses to guess at, as is a target
    that already has a BARE (unmarked) ``## Ecosystem`` heading — the primary migration case —
    rather than silently appending a duplicate, misplaced section (see
    :func:`riglib.ecosystem_readme.has_unmarked_ecosystem_heading`). ``on_conflict=backup`` keeps
    a timestamped copy before a rewrite; ``skip`` leaves a drifted block untouched.
    """
    desired = ecosystem_readme_block_text(action)
    path = ecosystem_readme_file(action)
    path.parent.mkdir(parents=True, exist_ok=True)

    if not path.exists():
        # newline="" (matching the read below): a fresh file's content is entirely OUR OWN LF text,
        # but writing without it would still hit Path.write_text's default OS-newline translation
        # on Windows for no benefit here — keep the write side symmetric with the read side always.
        path.write_text(desired + "\n", encoding="utf-8", newline="")
        return ActionResult(action, "created", f"ecosystem_readme/{action.item}: wrote {path}")
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            content = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        # unlike execpolicy's own rig-generated .rules file, a README target is a pre-existing,
        # arbitrary user-authored file — non-UTF-8 bytes are a real, reachable case here, not a
        # rig-generated: report it as an actionable error instead of letting it crash apply.
        return ActionResult(action, "error", f"ecosystem_readme/{action.item}: cannot read {path}: {exc}")

    new_content, state, detail = _splice_managed_block(
        content, desired, ECOSYSTEM_README_BEGIN_MARKER, ECOSYSTEM_README_END_MARKER
    )
    if state == "conflict":
        return ActionResult(action, "error", f"ecosystem_readme/{action.item}: {detail}")
    if state == "create" and ecosystem_readme.has_unmarked_ecosystem_heading(content):
        # The primary migration case (rig-cli#308's own stated motivation): a target repo already
        # hand-copies an unmarked '## Ecosystem' section. Blindly appending here would leave that
        # stale section untouched (still drifting) AND bolt on a second, misplaced heading -- refuse
        # instead, same "refuse to guess" stance as an unbalanced marker pair.
        return ActionResult(
            action, "error",
            f"ecosystem_readme/{action.item}: {path} already has an unmarked '## Ecosystem' section "
            f"-- wrap it in {ECOSYSTEM_README_BEGIN_MARKER} / {ECOSYSTEM_README_END_MARKER} by hand "
            "(or remove it) before enabling this, so apply never appends a duplicate section",
        )
    if state == "ok":
        return ActionResult(action, "skipped", f"ecosystem_readme/{action.item}: already current in {path}")
    backup_note = ""
    if state == "update":
        if on_conflict == "skip":
            return ActionResult(
                action, "skipped",
                f"ecosystem_readme/{action.item}: {path} block differs but on_conflict=skip — left unchanged (drift NOT reconciled)",
            )
        if _should_backup(on_conflict):
            bak = fsutil.backup_path(path)
            shutil.copy2(str(path), str(bak))
            backup_note = f" (backed up prior → {bak})"
    # newline="" mirrors the read: unlike execpolicy's rig-generated .rules file, this splice can
    # carry a PRE-EXISTING CRLF README's content verbatim in the preserved (outside-markers) text —
    # a plain write_text() would apply Path.write_text's default OS-newline translation on top of
    # already-literal \r\n bytes, corrupting them to \r\r\n on Windows.
    path.write_text(new_content, encoding="utf-8", newline="")
    status = "backed_up" if backup_note else "updated"
    verb = "appended block to" if state == "create" else "reconciled block in"
    return ActionResult(action, status, f"ecosystem_readme/{action.item}: {verb} {path}{backup_note}")
