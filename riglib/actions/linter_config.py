"""Linter/formatter config provisioning and per-repo project-tool handlers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import fsutil
from .. import project_tools
from ..config import linter_path_escapes_repo
from ..plan import Action
from .result import ActionResult


# ── linter / formatter config files (the `linters` block) ──────────────────────────
# rig provisions per-repo linter+formatter config files (CTO decision #4136.2). Each `linters.items`
# entry names a tool + a repo-relative path + the exact file content; apply writes/reconciles it,
# drift byte-compares it. UNLIKE the ship delegator, a linter config is a COMMITTED file (not
# git-ignored), so there is no .git/info/exclude leg — just an idempotent file write that honors
# on_conflict (a hand-edited config is backed up before overwrite, never clobbered silently). apply
# and drift share `resolve_linter_config` so status never misreports what apply would do.
def _linter_label(role: str, tool: str, item: str) -> str:
    """The human label for a linter item in apply/drift output: ``<role> <tool>:<item>``.

    Renders the ``role`` (``linter``/``formatter``) so the per-item config knob is REFLECTED in
    output (not a recorded-but-unused field): a formatter and a linter that happen to share a tool
    name read distinctly. apply and drift both call this so they name the same item identically.
    Degrades gracefully: a missing tool drops the ``:tool`` half; role defaults to ``linter``.
    """
    base = f"{tool}:{item}" if tool else item
    return f"{role or 'linter'} {base}"


@dataclass(frozen=True)
class LinterConfigResolution:
    """The desired linter-config-file outcome for one item — the source apply + drift share.

    ``state`` is the single discriminator both consumers switch on:
      - ``ok``       — the file is present and its bytes already equal ``content``: a no-op.
      - ``create``   — the file is absent: write it.
      - ``update``   — the file exists but its bytes differ: rewrite it (honoring ``on_conflict``).
      - ``io_error`` — a directory, a symlink, or an unreadable/non-UTF-8 file sits at the path:
                       apply reports an ERROR, never a silent skip. ``detail`` carries why.

    ``target_path`` is the resolved absolute file path; ``content`` the desired bytes.
    """

    target_path: Path
    content: str
    state: str
    detail: str = ""


def _unsafe_path_component(repo_root: Path, rel_path: str) -> tuple[Path, str] | None:
    """The first LEXICAL component of ``repo_root/rel_path`` that rig must not write through.

    Returns ``(component, reason)`` for the first offending ancestor/leaf, or ``None`` when the whole
    chain is real directories (plus a leaf that may or may not exist) and a write stays safely
    contained. Two classes of offender, both checked WITHOUT resolving the path first (so an in-repo
    symlink is caught too, not just one escaping the repo):

    - **a symlink** at ANY component (a parent dir OR the final leaf). ``configs -> /outside`` +
      ``path: configs/ruff.toml`` has a clean rel_path and a non-existent leaf, yet ``write_file``
      would ``mkdir``/write THROUGH ``configs`` and escape the repo; ``.oxfmtrc.jsonc -> shared.json``
      (a link INSIDE the repo) would be rewritten THROUGH, clobbering the link target. rig refuses
      every symlink — the docstringed "symlinks are refused" rule — so the user resolves it first.
    - **a non-directory PARENT** (a regular file / something else where a directory must be). With a
      regular file at ``config`` and ``path: config/ruff.toml`` the leaf "doesn't exist", so a naive
      classify says ``create``; apply then hits ``write_file``'s ``mkdir(parents=True)`` and raises a
      bare ``FileExistsError``. Classify it as ``io_error`` up front so status and apply agree.

    Walks ``repo_root / part1 / part2 / …`` lexically (``repo_root`` itself is NOT inspected — a repo
    that legitimately sits under a symlink, e.g. macOS ``/tmp`` → ``/private/tmp``, must not trip).
    """
    parts = Path(rel_path).parts
    cur = repo_root
    for i, part in enumerate(parts):
        cur = cur / part
        if cur.is_symlink():
            return cur, "symlink"
        is_last = i == len(parts) - 1
        # a NON-final component that exists but is not a directory can't be descended into.
        if not is_last and cur.exists() and not cur.is_dir():
            return cur, "not-a-directory"
    return None


def resolve_linter_config(repo_root: Path, rel_path: str, content: str) -> LinterConfigResolution:
    """Classify the on-disk linter-config state vs desired (pure, no writes).

    Reads the file at ``repo_root/rel_path`` and returns the one ``state`` apply and drift both
    switch on, so ``status`` never misreports. Idempotent: an absent file is ``create``; a correct
    file is ``ok``; a differing file is ``update``. Both sides are compared LF-normalized — the
    desired ``content`` has its line endings collapsed to LF here, and the on-disk read uses
    universal-newline translation — so CRLF-vs-LF is never spurious drift in EITHER direction (a CRLF
    file on disk OR a CRLF ``content:`` literal); the returned ``content`` is the LF form apply writes.
    ``io_error`` covers every case where rig must NOT blindly write:

    - an ABSOLUTE or ``..``-escaping ``rel_path`` (this is public API; it self-guards rather than
      trust callers, so a direct caller can never read/write OUTSIDE the repo),
    - a SYMLINK at the path OR anywhere in its parent chain (in-repo or escaping) — rig refuses to
      follow it (writing through a link could clobber the target or escape the repo); resolve it first,
    - a non-directory PARENT component (a file where a dir must be — apply could not mkdir there),
    - a directory at the path,
    - an unreadable file (``OSError``),
    - a non-UTF-8 / binary file (``UnicodeDecodeError`` — a subclass of ``ValueError``, NOT
      ``OSError``; catching only ``OSError`` would let it crash ``apply``/``rig status`` instead of
      classifying as drift-needing-attention).
    """
    target = repo_root / rel_path
    # Normalize the DESIRED content's line endings to LF up front, and carry the normalized form in
    # the resolution so apply writes exactly what we compare. The on-disk read below uses
    # universal-newline translation (read_text → LF), so a CRLF `content:` literal (common when
    # pasted on Windows) would otherwise NEVER equal the normalized read: write CRLF → read LF →
    # compare to CRLF `content` → perpetual `update` (apply rewrites every run, status always drifts).
    # rig-managed config files are LF-only; this makes the feature converge regardless of how the
    # config string was authored.
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    # Self-protecting containment: the runner/drift already pre-check `linter_path_escapes_repo`, but
    # this is PUBLIC API (no `_` prefix, imported across modules), so guard here too rather than trust
    # every caller. An absolute or `..`-escaping `rel_path` makes `repo_root / rel_path` point OUTSIDE
    # the repo (`Path("/repo") / "/etc/x" == Path("/etc/x")`); refuse it before any stat/read so a
    # direct caller can never read or write through an escaping path.
    if linter_path_escapes_repo(rel_path):
        return LinterConfigResolution(target, content, "io_error", detail=f"{rel_path!r} escapes the repo (rig won't read/write through it)")
    # Component containment (defends against a symlinked OR file ancestor, and an in-repo symlink leaf)
    # — checked BEFORE exists()/read so status and apply agree and no write follows an unsafe path.
    unsafe = _unsafe_path_component(repo_root, rel_path)
    if unsafe is not None:
        comp, reason = unsafe
        detail = (
            f"{comp} is a symlink (rig won't write through it)" if reason == "symlink"
            else f"{comp} is not a directory (rig can't create {target.name} under it)"
        )
        return LinterConfigResolution(target, content, "io_error", detail=detail)
    if not target.exists():
        return LinterConfigResolution(target, content, "create")
    if not target.is_file():
        return LinterConfigResolution(target, content, "io_error", detail=f"{target} is not a regular file")
    try:
        on_disk = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return LinterConfigResolution(target, content, "io_error", detail=f"cannot read {target}: {exc}")
    if on_disk == content:
        return LinterConfigResolution(target, content, "ok")
    return LinterConfigResolution(target, content, "update")


def _do_provision_linter_config(action: Action, on_conflict: str) -> ActionResult:
    """Provision/reconcile one Rig-managed linter/formatter config file."""
    rel_path = str(action.options.get("rel_path", ""))
    content = action.options.get("content")
    tool = str(action.options.get("tool") or "")
    role = str(action.options.get("role") or "linter")
    label = _linter_label(role, tool, str(action.item))
    if not rel_path or not isinstance(content, str) or not content:
        return ActionResult(action, "error", f"linter-config ({label}): malformed action (missing rel_path/content)")
    if linter_path_escapes_repo(rel_path):
        return ActionResult(action, "error", f"linter-config ({label}): path {rel_path!r} escapes the repo (refusing to write)")

    r = resolve_linter_config(action.target, rel_path, content)
    if r.state == "io_error":
        return ActionResult(action, "error", f"linter-config ({label}): {r.detail}")
    if r.state == "ok":
        return ActionResult(action, "skipped", f"linter-config ({label}): {r.target_path.name} already correct")

    preview = ""
    if action.options.get("preview_findings") and tool == "oxlint":
        try:
            from ..lint_preview import preview_oxlint_policy
            impact = preview_oxlint_policy(action.target, r.content, rel_path)
            preview = f"; {impact.render()}"
        except Exception as exc:
            preview = f"; lint finding preview skipped: {type(exc).__name__}: {exc}"

    out = fsutil.write_file(r.target_path, r.content, on_conflict)
    return ActionResult(action, out.status, f"linter-config ({label}): {out.detail}{preview}", out.backup)


def _do_provision_project_tool(action: Action, on_conflict: str) -> ActionResult:
    """Provision one repo-local project-tool carrier or live operation.

    File entries are ordinary repo files with the same conflict policy as every other rig-managed
    config. The Haft Codex MCP entry is a managed TOML section, so an existing ``.codex/config.toml``
    keeps unrelated user config. Sverklo live operations are idempotent and dry-run gated.
    """
    tool = str(action.options.get("tool") or "project-tool")
    operation = str(action.options.get("operation") or "file")
    if operation in {"register", "reindex"} and tool == "sverklo":
        status, detail = project_tools.run_sverklo(action.target, operation)
        return ActionResult(action, status, detail)

    rel_path = str(action.options.get("rel_path") or "")
    content = action.options.get("content")
    label = f"{tool}/{action.item}"
    if not rel_path or not isinstance(content, str):
        return ActionResult(action, "error", f"project-tool ({label}): malformed action (missing rel_path/content)")
    r = project_tools.resolve_entry(action.target, rel_path, content, operation)
    if r.state == "io_error":
        return ActionResult(action, "error", f"project-tool ({label}): {r.detail}")
    if r.state == "ok":
        return ActionResult(action, "skipped", f"project-tool ({label}): {rel_path} already correct")
    out = fsutil.write_file(r.target_path, r.content, on_conflict)
    return ActionResult(action, out.status, f"project-tool ({label}): {out.detail}", out.backup)


def _do_provision_linter_bundle(action: Action, on_conflict: str) -> ActionResult:
    from ..linter_carriers import apply_bundle
    out = apply_bundle(action.source, action.target, str(action.options.get("source_rel") or ""), str(action.options.get("target_rel") or ""), on_conflict)
    return ActionResult(action, out.status, out.detail, out.backup)
