"""Global git excludes block resolution and handler."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import GITIGNORE_BEGIN_MARKER, GITIGNORE_BLOCK_COMMENT, GITIGNORE_END_MARKER
from ..paths import expand_user_path
from ..plan import Action
from . import git_config
from .git_exclude import _find_marker_lines
from .result import ActionResult


def global_excludes_block_text(entries: list[str]) -> str:
    """The exact marker-delimited block rig owns for ``entries`` (no trailing newline).

    Single source of truth shared by the install handler and drift, so both agree byte-for-byte
    on what the managed block SHOULD contain. The block is the begin marker, a fixed explanatory
    comment line (so a human reading the global excludes file knows what it is), one line per entry
    (in the order given), then the end marker. The comment is rendered byte-for-byte and matches the
    block already present on a provisioned machine, so a re-apply is a true zero-churn no-op.
    """
    return "\n".join([GITIGNORE_BEGIN_MARKER, GITIGNORE_BLOCK_COMMENT, *entries, GITIGNORE_END_MARKER])


@dataclass(frozen=True)
class GlobalExcludesResolution:
    """The desired global-excludes outcome — the one source apply + drift share.

    ``state`` is the single discriminator both consumers switch on:
      - ``ok``       — the managed block is already present and exactly correct: no-op.
      - ``create``   — no excludes file or no managed block: append the block (creating the file
                       if absent), preserving any existing content.
      - ``update``   — a managed block exists but its body differs, OR the file has MULTIPLE
                       rig-managed blocks (a prior non-idempotent appender): collapse the managed
                       region to ONE correct block in place, preserving every line outside the
                       markers (verbatim — CRLF and trailing blanks included).
      - ``conflict`` — the file has unbalanced markers (a begin with no end, an end before a
                       begin): rig won't guess the block's extent, so it leaves the file untouched
                       and surfaces it. ``detail`` says why.
      - ``io_error`` — the path could not be read (unreadable, or a directory sits there). Unlike a
                       marker ``conflict`` (the file is fine, the operator must reconcile) this is a
                       failure to even inspect the file: apply reports it as an ERROR, never a silent
                       skip. ``detail`` carries the OS error.

    ``desired_block`` is the canonical block text; ``new_content`` is the full desired file content
    for ``create``/``update`` (``None`` for ``ok``/``conflict``/``io_error``). ``missing_entries``
    lists the desired entries the MANAGED block(s) do not carry (``update`` only) — so drift can
    name WHY a provisioned block is stale (e.g. a machine provisioned before an entry was added).
    Only the marker-delimited region counts: a user-authored copy of the line elsewhere in the
    file does not make the block current (apply still rewrites it), so it must not hide the note.
    """

    path: Path
    state: str
    desired_block: str
    new_content: str | None = None
    detail: str = ""
    missing_entries: tuple[str, ...] = ()


def resolve_global_excludes(path: Path, entries: list[str]) -> GlobalExcludesResolution:
    """Classify the on-disk global excludes file vs the desired managed block (pure, no writes).

    Idempotent + non-destructive: rig only ever (a) appends the block to a file that lacks it
    (creating the file if absent), (b) collapses the existing managed region to ONE correct block
    in place, or (c) no-ops a correct single block. Crucially this is STRICTLY idempotent even when
    a prior tool appended the block MORE THAN ONCE: a file with several rig-managed blocks resolves
    to ``update`` and collapses to exactly one. An unbalanced marker pair is a ``conflict`` rig
    never rewrites. Every line OUTSIDE the managed region is preserved byte-for-byte: the region is
    located and spliced by raw character offset (not splitlines/rejoin), so a CRLF file, a file with
    no trailing newline, and trailing blank lines all survive untouched.
    """
    desired = global_excludes_block_text(entries)
    if not path.exists():
        return GlobalExcludesResolution(path, "create", desired, new_content=desired + "\n")
    try:
        # newline="" disables universal-newline translation so a CRLF file is read (and later
        # re-written) byte-for-byte outside the managed region — the documented verbatim guarantee.
        with path.open(encoding="utf-8", newline="") as fh:
            content = fh.read()
    except OSError as exc:
        # unreadable, or a directory at the path — a failure to inspect, not a marker conflict.
        return GlobalExcludesResolution(path, "io_error", desired, detail=f"cannot read {path}: {exc}")

    # Find each marker line by its raw [start, end_of_line] offsets so the splice preserves every
    # other byte verbatim (line ending included). A marker is a line whose stripped text equals the
    # marker constant — tolerant of trailing whitespace on the marker line itself.
    begins = _find_marker_lines(content, GITIGNORE_BEGIN_MARKER)
    ends = _find_marker_lines(content, GITIGNORE_END_MARKER)
    # An unbalanced pair (different counts, or an end with no begin) is ambiguous — rig won't guess
    # the region's extent. NOTE a balanced N-pairs (N>1) is NOT a conflict: it is a non-idempotent
    # duplicate we collapse below.
    if len(begins) != len(ends) or (ends and not begins):
        return GlobalExcludesResolution(
            path, "conflict", desired,
            detail=f"{path} has unbalanced rig-managed markers — reconcile by hand, then re-run",
        )
    if not begins:
        # no managed block: append it, keeping a single blank-line separator from prior content.
        body = content.rstrip("\n")
        sep = "\n\n" if body else ""
        new_content = f"{body}{sep}{desired}\n"
        return GlobalExcludesResolution(path, "create", desired, new_content=new_content)

    pairs = _pair_marker_regions(begins, ends)
    if pairs is None:
        return GlobalExcludesResolution(
            path, "conflict", desired,
            detail=f"{path} has misordered/nested rig-managed markers — reconcile by hand, then re-run",
        )

    # Already exactly one correct block? (the common steady state — a true no-op).
    if len(pairs) == 1 and content[pairs[0][0] : pairs[0][1]] == desired:
        return GlobalExcludesResolution(path, "ok", desired)

    new_content = _collapse_managed_regions(content, pairs, desired)
    # `pairs` holds the ORIGINAL (start, end) spans.
    managed = "".join(content[start:end] for start, end in pairs)
    return GlobalExcludesResolution(
        path, "update", desired, new_content=new_content,
        missing_entries=_missing_exclude_entries(managed, entries),
    )


def _pair_marker_regions(
    begins: list[tuple[int, int]], ends: list[tuple[int, int]],
) -> list[tuple[int, int]] | None:
    """Pair the markers by interleaving them in document order: they must strictly alternate
    begin, end, begin, end, … Anything else (an end before a begin, a begin immediately followed
    by another begin → a nested/overlapping block) is ambiguous and rig won't guess — ``None``.
    Each valid pair fences ONE managed block region ``[begin_start, end_line_end]``; the spans
    between consecutive pairs are USER content (e.g. a hand-added ignore that landed between two
    duplicated rig blocks) and MUST be preserved."""
    markers = sorted(
        [(b[0], b[1], "begin") for b in begins] + [(e[0], e[1], "end") for e in ends]
    )
    pairs: list[tuple[int, int]] = []  # (region_start, region_end) per managed block
    expect = "begin"
    pending_start = -1
    for start, line_end, kind in markers:
        if kind != expect:
            return None
        if kind == "begin":
            pending_start = start
            expect = "end"
        else:
            pairs.append((pending_start, line_end))
            expect = "begin"
    return pairs


def _collapse_managed_regions(content: str, pairs: list[tuple[int, int]], desired: str) -> str:
    """Splice OUT every managed-block region (preserving all USER content outside the markers,
    including any text BETWEEN duplicated blocks), then re-insert ONE correct block where the
    FIRST block sat. Build the result left-to-right so each non-managed span is copied verbatim."""
    out_parts: list[str] = []
    cursor = 0
    for idx, (r_start, r_end) in enumerate(pairs):
        out_parts.append(content[cursor:r_start])  # user content before this block, verbatim
        if idx == 0:
            out_parts.append(desired)  # the single canonical block replaces the first one
        elif content[r_end : r_end + 1] == "\n":
            # A removed duplicate block leaves a seam: if both the text before and after it end/
            # start with a newline we'd otherwise create a doubled blank line. Drop one leading
            # newline of the following span so collapsing N blocks doesn't accrete blank lines.
            r_end += 1
        cursor = r_end
    out_parts.append(content[cursor:])  # trailing user content, verbatim
    return "".join(out_parts)


def _missing_exclude_entries(managed: str, entries: list[str]) -> tuple[str, ...]:
    """The desired ``entries`` that the managed block text carries on NO line — the lines an
    ``update`` is going to ADD, so drift can name them (rig-cli#331). Both sides are compared
    stripped, so a padded entry written verbatim still counts as present."""
    present = {line.strip() for line in managed.splitlines()}
    return tuple(e for e in entries if e.strip() not in present)


def _resolve_excludes_target(action: Action) -> tuple[Path, bool, str | None]:
    """Resolve WHICH file holds the managed block, and whether ``core.excludesfile`` must be set.

    Honors the user's existing choice: if ``core.excludesfile`` is ALREADY set (the common case —
    e.g. ``~/.gitignore``), the block goes in THAT file and git config is left alone. If it is NOT
    set, rig points ``core.excludesfile`` at the XDG default (``~/.config/git/ignore``) and writes
    the block there — so on a clean machine ``rig init`` does everything itself. An explicit
    ``gitignore.excludesfile`` override in config forces a specific file (and rig sets
    ``core.excludesfile`` to it when git's value doesn't already match).

    Returns ``(target_path, needs_set, set_value)``:
      - ``target_path`` — the resolved, ``~``/``$XDG``-expanded file to reconcile the block in.
      - ``needs_set``   — True when ``core.excludesfile`` must be written (unset, or override
                          differs from the current value).
      - ``set_value``   — the value to write into ``core.excludesfile`` (the un-expanded, portable
                          form so git stores ``~/.config/git/ignore``, not a machine path); None
                          when ``needs_set`` is False.

    The git-config READ goes through ``_git_global`` (the same seam the dispatcher uses), so tests
    monkeypatch one function and never run real ``git config --global``.
    """
    current = git_config._git_global("core.excludesfile")
    override = action.options.get("excludesfile")
    xdg_default = action.options.get("xdg_default") or "~/.config/git/ignore"
    # git resolves core.excludesfile verbatim after its own ~/$VAR expansion; do not repo-root
    # anchor an override/xdg_default here, or rig would write a different file than git reads.
    if isinstance(override, str) and override:
        # explicit override: reconcile in this file; set core.excludesfile when git doesn't match.
        target = expand_user_path(override)
        needs_set = current != override
        return target, needs_set, (override if needs_set else None)
    if current:
        # respect the user's existing choice — manage the block in their file, touch no git config.
        return expand_user_path(current), False, None
    # unset: point git at the XDG default AND write the block there (clean-machine path).
    return expand_user_path(xdg_default), True, xdg_default


def _do_provision_global_excludes(action: Action, on_conflict: str) -> ActionResult:
    """Provision/reconcile rig's managed block in the GLOBAL git ``core.excludesfile``.

    Two coupled steps, in order:
      1. Resolve the target file from ``core.excludesfile`` (honor an existing value; set it to the
         XDG default when unset — so a clean machine is fully provisioned by ``rig init`` alone).
      2. Reconcile the marker block via the shared :func:`resolve_global_excludes` ``state`` — apply
         and drift read the same classification, so ``status`` never misreports the on-disk state.

    Idempotent: a correct single block with ``core.excludesfile`` already set is a true no-op; a
    missing block is appended (creating the file if absent); a drifted OR DUPLICATED managed region
    is collapsed IN PLACE to one correct block, preserving every other line verbatim. An unbalanced
    marker pair is a ``conflict`` rig leaves untouched (``skipped``), and an unreadable path is an
    ``error`` (never a silent skip). There is no backup: rig only ever edits its OWN fenced lines
    plus a git-config setting, so ``on_conflict`` is irrelevant here (consistent with the dispatcher
    and the surgical hook-bridge upsert).
    """
    entries = [str(e) for e in action.options.get("entries", [])]
    target, needs_set, set_value = _resolve_excludes_target(action)

    notes: list[str] = []
    cfg_status = "skipped"
    # Step 1: wire core.excludesfile when it is unset / doesn't match the override.
    if needs_set and set_value is not None:
        rc = git_config._set_git_global("core.excludesfile", set_value)
        if rc == 0:
            notes.append(f"core.excludesfile → {set_value}")
            cfg_status = "created"
        else:
            return ActionResult(action, "error", "gitignore: failed to set global core.excludesfile")

    # Step 2: reconcile the managed block in the resolved file.
    r = resolve_global_excludes(target, entries)
    if r.state == "ok":
        if cfg_status == "created":
            # rare: git config was unset but the file already had the exact block — config write is
            # itself a change, so report it (not a silent no-op).
            return ActionResult(action, "created", f"gitignore: {'; '.join(notes)} (block already correct in {target})")
        return ActionResult(action, "skipped", f"gitignore: managed block already correct in {target}")
    if r.state == "conflict":
        return ActionResult(action, "skipped", f"gitignore: {r.detail}")
    if r.state == "io_error":
        return ActionResult(action, "error", f"gitignore: {r.detail}")
    if r.new_content is None:  # defensive — create/update always carry content
        return ActionResult(action, "error", f"gitignore: unhandled state {r.state!r}")
    return _write_excludes_block(action, target, entries, r.state, r.new_content, notes)


def _write_excludes_block(
    action: Action, target: Path, entries: list[str], state: str, new_content: str, notes: list[str],
) -> ActionResult:
    """Write the computed file content and report create-vs-update."""
    existed = target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    # newline="" so the bytes we computed (which may carry the user's CRLF outside the block) are
    # written verbatim, with no platform newline translation.
    with target.open("w", encoding="utf-8", newline="") as fh:
        fh.write(new_content)
    n = len(entries)
    plural = "entry" if n == 1 else "entries"
    if state == "create":
        verb = "added block to" if existed else "created"
        block_note = f"{verb} {target} ({n} {plural})"
    else:
        block_note = f"updated managed block in {target}"
    notes.append(block_note)
    return ActionResult(action, "created" if state == "create" else "updated", f"gitignore: {'; '.join(notes)}")
