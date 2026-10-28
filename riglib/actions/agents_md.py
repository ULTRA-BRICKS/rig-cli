"""AGENTS.md/CLAUDE.md symlink resolution and the orchestrator/autonomous-mode managed blocks."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from . import fsutil
from ..plan import Action
from .managed_blocks import _splice_managed_block
from .result import ActionResult
from .shared import _should_backup


# ── AGENTS.md / CLAUDE.md canonical + symlink ─────────────────────────────────────
# One file is the real source of truth; the other is a relative symlink to it, so every
# agent harness (Claude Code reads CLAUDE.md; Codex/others read AGENTS.md) sees identical
# guidance. apply and drift BOTH go through `resolve_agents_md` so they can never disagree
# on canonical direction or on what counts as in-sync.
_AGENTS_MD_PLACEHOLDER = (
    "# Agent guide\n\n"
    "Repository instructions for coding agents (Claude Code, Codex, etc.). This is the\n"
    "canonical file; the other agent-guide filename is a symlink to it, so every agent\n"
    "reads the same guide.\n\n"
    "Replace the rest of this placeholder with the conventions, commands, and guardrails\n"
    "for this repo. Keep the guardrail below — it applies to every repo.\n\n"
    "## Writing reports for people (Telegram, PR bodies, spec summaries)\n\n"
    "These are read by a human, not another agent, so optimize for being understood:\n\n"
    "- Never invent abbreviations or compress terms into fragments. Write the full term.\n"
    "- Prefer fewer points explained in full sentences over more points compressed into\n"
    "  jargon. A short list of clear sentences beats a long list of cryptic stubs.\n"
    "- Expand every non-obvious term at first use (name it in full, then abbreviate if you\n"
    "  must reuse it).\n"
    "- When a message exceeds the channel's length limit, cut secondary content — drop whole\n"
    "  points — rather than compressing the wording of what remains.\n"
)


def agents_md_paths(repo_root: Path) -> tuple[Path, Path]:
    """The (AGENTS.md, CLAUDE.md) pair at a repo root."""
    return repo_root / "AGENTS.md", repo_root / "CLAUDE.md"


def _is_real_file(p: Path) -> bool:
    """True for a regular file that is NOT a symlink (a real source of truth, not a link)."""
    return p.is_file() and not p.is_symlink()


def _is_broken_symlink(p: Path) -> bool:
    """True for a dangling symlink — a link whose target no longer resolves to anything.

    The spec's "broken link": a pair rig provisioned (real canonical + symlink) where the
    canonical was later deleted, leaving the link dangling. rig surfaces this by NAME (a broken
    symlink, with how to recover) rather than as a generic "not a real file", but it never
    auto-recreates the missing canonical — an empty placeholder would silently mask the loss of
    the (possibly curated) content that was deleted, turning a visible, git-recoverable failure
    into an invisible one. ``status`` flags it; the human restores the canonical or removes the
    link.
    """
    return p.is_symlink() and not p.exists()


def symlink_points_to(link: Path, canonical_name: str) -> bool:
    """True when ``link`` is a symlink already resolving to ``canonical_name`` (same dir).

    Accepts either a relative stored target (just the filename) or an absolute one that
    resolves to the same path, so a correct link written either way is a no-op. Shared by the
    install action and the drift check so both agree on "already correct".
    """
    if not link.is_symlink():
        return False
    current = link.readlink()
    if str(current) == canonical_name:
        return True
    try:
        return (link.parent / current).resolve() == (link.parent / canonical_name).resolve()
    except (OSError, RuntimeError):
        # OSError: a broken/unreadable path. RuntimeError: pathlib raises it for a symlink loop
        # (e.g. AGENTS.md → AGENTS.md). Either way the link does not resolve to the canonical.
        return False


@dataclass(frozen=True)
class AgentsMdResolution:
    """The desired AGENTS.md/CLAUDE.md outcome for a repo — the one source apply + drift share.

    ``state`` is the single discriminator both consumers switch on, so they can never disagree
    about what is in sync:

    - ``ok``          — already a real canonical + a correct symlink: no-op.
    - ``create_both`` — both slots empty: write ``canonical`` placeholder + symlink the other.
    - ``create_link`` — one real canonical, the other empty: symlink the other → canonical.
    - ``converge``    — both real & identical: collapse ``link`` to a symlink (honors on_conflict).
    - ``conflict``    — anything ambiguous/unsafe (both real & different, a real file at one slot
                        with a foreign symlink/dir at the other, peer-link loops, neither-real
                        with a stray symlink/dir): rig NEVER mutates these; ``detail`` says why.

    ``canonical`` is the real source-of-truth filename; ``link`` is the slot that should be a
    symlink → canonical (for ``converge`` that is ``CLAUDE.md``).
    """

    agents: Path
    claude: Path
    state: str
    canonical: str
    canonical_path: Path
    link: Path
    detail: str = ""


def _both_real_resolution(agents: Path, claude: Path) -> AgentsMdResolution:
    """Both slots are real files: identical → ``converge``, anything else → ``conflict``."""
    try:
        identical = agents.read_bytes() == claude.read_bytes()
    except OSError as exc:
        return AgentsMdResolution(agents, claude, "conflict", "AGENTS.md", agents, claude,
                                  f"cannot read AGENTS.md/CLAUDE.md to compare: {exc}")
    if identical:
        return AgentsMdResolution(agents, claude, "converge", "AGENTS.md", agents, claude)  # link = CLAUDE.md
    return AgentsMdResolution(
        agents, claude, "conflict", "AGENTS.md", agents, claude,
        "AGENTS.md and CLAUDE.md are both real files with different content "
        "— reconcile into one (keep the canonical), then re-run",
    )


def _one_real_resolution(agents: Path, claude: Path, agents_real: bool) -> AgentsMdResolution:
    """Exactly one slot is a real file (the canonical); classify the other slot (the link)."""
    if agents_real:
        canonical, canonical_path, link = "AGENTS.md", agents, claude
    else:
        canonical, canonical_path, link = "CLAUDE.md", claude, agents

    def _r(state: str, detail: str = "") -> AgentsMdResolution:
        return AgentsMdResolution(agents, claude, state, canonical, canonical_path, link, detail)

    if link.is_symlink():
        if symlink_points_to(link, canonical):
            return _r("ok")
        return _r("conflict",
                  f"{link.name} is a symlink to something other than {canonical} "
                  "— left untouched; remove it to let rig manage the pair")
    if link.exists():  # not real (canonical is the only real file) and not a symlink → a dir
        return _r("conflict", f"{link.name} exists but is not a regular file — left untouched")
    return _r("create_link")


def _deleted_canonical_detail(agents: Path, claude: Path) -> str | None:
    """Detail for the spec's headline "broken link", or ``None`` when the shape does not match.

    EXACTLY one slot is a rig-shaped dangling link — a symlink whose target is the OTHER managed
    name, and that name is now missing — while the other slot is empty. This is precisely "rig
    provisioned the pair, then the real canonical was deleted". Name it with recovery guidance, but
    NEVER recreate the canonical: an empty placeholder would silently mask the loss of
    possibly-curated content (a visible, git-recoverable failure turned invisible). The narrow shape
    deliberately EXCLUDES a peer-link loop (the other slot is also a symlink, not empty → both
    "broken") and a foreign/competing occupant in the other slot, which would make the "canonical
    was deleted" narrative wrong; those fall through to the generic dangling-link report.
    """
    for link, other, canonical in ((claude, agents, "AGENTS.md"), (agents, claude, "CLAUDE.md")):
        if (
            _is_broken_symlink(link)
            and symlink_points_to(link, canonical)
            and not (other.is_symlink() or other.exists())
        ):
            return (f"{link.name} → {canonical} (missing) is a broken symlink — its "
                    f"canonical target {canonical} does not exist (deleted out from "
                    "under a provisioned pair, or never created). Restore it from "
                    "version control (e.g. git checkout) or remove the dangling link, "
                    "then re-run; rig will not recreate it, to avoid masking lost content")
    return None


def _dangling_links_detail(agents: Path, claude: Path) -> str | None:
    """Detail naming any dangling link in a "neither real" pair (foreign target, peer loop, ...).

    Says what's wrong WITHOUT the (here-inaccurate) "canonical deleted" narrative or git-restore
    advice. ``None`` when no slot dangles.
    """
    dangling = [p.name for p in (agents, claude) if _is_broken_symlink(p)]
    if not dangling:
        return None
    joined = " and ".join(dangling)
    # plural/singular; the advice deliberately does NOT presume the target is absent — for a
    # peer loop (A→B, B→A) each "target" exists as the other link, so "restore the target"
    # would be incoherent. The universal fix is one real file plus a link to it.
    subject = (
        "are broken/dangling symlinks (their targets do not resolve)"
        if len(dangling) > 1 else "is a broken/dangling symlink (its target does not resolve)"
    )
    return (f"{joined} {subject} — reconcile the pair to one real file plus a symlink "
            "to it (remove the dangling link(s); create or restore the canonical), "
            "then re-run")


def _neither_real_resolution(agents: Path, claude: Path) -> AgentsMdResolution:
    """Neither slot is a real file: empty → ``create_both``, otherwise a named ``conflict``."""
    def _r(state: str, detail: str = "") -> AgentsMdResolution:
        return AgentsMdResolution(agents, claude, state, "AGENTS.md", agents, claude, detail)

    agents_present = agents.is_symlink() or agents.exists()
    claude_present = claude.is_symlink() or claude.exists()
    if not agents_present and not claude_present:
        return _r("create_both")
    detail = _deleted_canonical_detail(agents, claude) or _dangling_links_detail(agents, claude)
    if detail is None:
        detail = ("AGENTS.md/CLAUDE.md present but neither is a real file (a symlink or "
                  "directory occupies a slot) — reconcile to one real file, then re-run")
    return _r("conflict", detail)


def resolve_agents_md(repo_root: Path) -> AgentsMdResolution:
    """Classify the on-disk AGENTS.md/CLAUDE.md state into one desired ``state`` (pure, no writes).

    Safety-first: rig only ever (a) creates into an EMPTY slot, (b) collapses two identical
    REAL files to a symlink, or (c) no-ops a correct pair. Every other shape — a foreign
    symlink, a directory, divergent real files, a peer-link loop — is a ``conflict`` rig leaves
    untouched and surfaces, so it can never clobber a real file or a user-placed symlink.
    """
    agents, claude = agents_md_paths(repo_root)
    agents_real = _is_real_file(agents)
    claude_real = _is_real_file(claude)
    if agents_real and claude_real:
        return _both_real_resolution(agents, claude)
    if agents_real or claude_real:
        return _one_real_resolution(agents, claude, agents_real)
    return _neither_real_resolution(agents, claude)


def _do_provision_agents_symlink(action: Action, on_conflict: str) -> ActionResult:
    """Provision the AGENTS.md (canonical, real) + CLAUDE.md (symlink) invariant in a repo.

    Switches on the shared :func:`resolve_agents_md` ``state`` — apply and drift read the same
    classification, so ``status`` never misreports the on-disk state. Only ``create_*`` and
    ``converge`` mutate disk (``converge`` only when ``on_conflict`` is not ``skip``); ``ok``
    and ``conflict`` never touch a file. As elsewhere in rig, ``on_conflict`` decides whether
    apply *reconciles* a reported drift, not whether status reports it.
    """
    r = resolve_agents_md(action.target)

    if r.state == "ok":
        return ActionResult(action, "skipped", f"agents-md: {r.link.name} already links → {r.canonical}")

    if r.state == "conflict":
        return ActionResult(action, "skipped", f"agents-md: {r.detail}")

    if r.state == "create_link":
        r.link.symlink_to(r.canonical)
        return ActionResult(action, "created", f"agents-md: {r.link.name} → {r.canonical}")

    if r.state == "create_both":
        r.canonical_path.write_text(_AGENTS_MD_PLACEHOLDER, encoding="utf-8")
        r.link.symlink_to(r.canonical)
        return ActionResult(
            action, "created",
            f"agents-md: created {r.canonical_path.name} (canonical) + {r.link.name} → {r.canonical}",
        )

    if r.state == "converge":
        if on_conflict == "skip":
            return ActionResult(
                action, "skipped",
                "agents-md: AGENTS.md and CLAUDE.md are identical real files; on_conflict=skip "
                "— left as two real files (set on_conflict=backup/overwrite to converge to a symlink)",
            )
        backup = None
        if on_conflict == "backup":
            backup = fsutil.backup_path(r.link)
            shutil.copy2(r.link, backup)
        r.link.unlink()
        r.link.symlink_to(r.canonical)
        suffix = f"; backed up → {backup.name}" if backup else ""
        # match the handler status contract: a replacement that kept a backup is "backed_up"
        # (CLI summaries/icons key off it), a clean overwrite is "updated".
        return ActionResult(
            action, "backed_up" if backup else "updated",
            f"agents-md: converged {r.link.name} → {r.canonical} (identical content{suffix})",
            backup,
        )

    return ActionResult(action, "error", f"agents-md: unhandled state {r.state!r}")


# ── AGENTS.md marker splices (orchestrator + autonomous-mode) ─────────────────────
# Visible at session start so the agent does not have to auto-load a long skill first.
# File creation stays with provision_agents_symlink: a splice-only apply on an empty
# repo must skip rather than plant a stub guide.

ORCHESTRATOR_BEGIN_MARKER = "# >>> rig-managed orchestrator (do not edit) >>>"

ORCHESTRATOR_END_MARKER = "# <<< rig-managed orchestrator (do not edit) <<<"

AUTONOMOUS_BEGIN_MARKER = "# >>> rig-managed autonomous-mode (do not edit) >>>"

AUTONOMOUS_END_MARKER = "# <<< rig-managed autonomous-mode (do not edit) <<<"


def orchestrator_block_text() -> str:
    """The marker-delimited orchestrator reminder (shared by apply + drift)."""
    return "\n".join([
        ORCHESTRATOR_BEGIN_MARKER,
        "# Orchestrator, not worker. Do not implement inline. Do not run `gh` except an unchained",
        "# `gh ship <N>`, or read-only `gh issue` list/view, `gh pr` list/view/checks/diff, `gh run`",
        "# list/view/watch, `gh repo view`, GET-only `gh api` (no -f/-F/--input). Do not `git",
        "# worktree add` — `rig worktree create`.",
        "# Read-only (`git status`, `ls`, `cat`, `grep`, `git worktree list`) is fine. Dispatch a subagent for the rest. Skill: delegate-work-to-subagents. Hook: orchestrator-stays-thin.",
        "# Key reports MUST go to Telegram (`tg --tag report`): quote the user's original words, include ticket/PR URLs, name the measure already started, draft via `review just-ask`. Never \"not in prod\" with no next action.",
        "# After a successful `gh ship` of a load-bearing feature, merge is not done: (1) turn it on",
        "# in settings if it has a config knob, (2) `rig apply commit` on this machine, (3) prove it",
        "# WORKS on this machine (command output, Telegram, deny text — SHA/on-disk is not a proof), (4) re-check the original user quote is solved.",
        "# A helper yield is not done — do not tick the todo until the original ask is live-proven; wake parked helpers via hub send.",
        ORCHESTRATOR_END_MARKER,
    ])


def autonomous_mode_block_text() -> str:
    """The marker-delimited autonomous-mode reminder (shared by apply + drift)."""
    return "\n".join([
        AUTONOMOUS_BEGIN_MARKER,
        "# Mode autonomous: extra-approval gate OFF. Do not invent a hold-for-human.",
        "# Ship when gates pass. Skill: autonomous-mode (skill://autonomous-mode).",
        AUTONOMOUS_END_MARKER,
    ])


def _agents_md_splice_target(root: Path) -> Path | None:
    """The real canonical guide, or None when there isn't one to own.

    Only a regular (non-symlink) canonical is spliced. A CLAUDE.md-canonical pair
    writes the real CLAUDE.md; the AGENTS.md symlink still reads it. A foreign
    AGENTS.md symlink is a conflict the symlink action leaves untouched — following
    it here would rewrite a file rig does not manage.
    """
    r = resolve_agents_md(root)
    if r.state == "conflict":
        return None
    if _is_real_file(r.canonical_path):
        return r.canonical_path
    return None


def _splice_agents_md_block(
    action: Action,
    on_conflict: str,
    *,
    desired: str,
    begin: str,
    end: str,
    label: str,
) -> ActionResult:
    """Idempotent marker splice into the canonical guide — never creates the file."""
    from ..instruction_files import instruction_file_lock

    path = _agents_md_splice_target(action.target)
    if path is None:
        return ActionResult(
            action, "skipped",
            f"{label}: no AGENTS.md/CLAUDE.md yet (symlink action owns creation)",
        )
    with instruction_file_lock(path):
        return _splice_agents_md_block_locked(
            action, on_conflict, path=path, desired=desired, begin=begin, end=end, label=label,
        )


def _splice_agents_md_block_locked(
    action: Action, on_conflict: str, *, path: Path, desired: str, begin: str, end: str, label: str,
) -> ActionResult:
    """A native global guide may alias this canonical repo/home file; share its lock."""
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            content = fh.read()
    except OSError as exc:
        return ActionResult(action, "error", f"{label}: cannot read {path}: {exc}")
    new_content, state, detail = _splice_managed_block(content, desired, begin, end)
    if state == "conflict":
        return ActionResult(action, "error", f"{label}: {detail}")
    if state == "ok":
        return ActionResult(action, "skipped", f"{label}: already current in {path}")
    backup_note = ""
    if state == "update":
        if on_conflict == "skip":
            return ActionResult(
                action, "skipped",
                f"{label}: {path} block differs but on_conflict=skip — left unchanged "
                "(drift NOT reconciled)",
            )
        if _should_backup(on_conflict):
            bak = fsutil.backup_path(path)
            shutil.copy2(str(path), str(bak))
            backup_note = f" (backed up prior → {bak})"
    path.write_text(new_content, encoding="utf-8")
    status = "backed_up" if backup_note else "updated"
    verb = "appended block to" if state == "create" else "reconciled block in"
    return ActionResult(action, status, f"{label}: {verb} {path}{backup_note}")


def _do_provision_agents_orchestrator(action: Action, on_conflict: str) -> ActionResult:
    return _splice_agents_md_block(
        action, on_conflict,
        desired=orchestrator_block_text(),
        begin=ORCHESTRATOR_BEGIN_MARKER,
        end=ORCHESTRATOR_END_MARKER,
        label="orchestrator",
    )


def _do_provision_agents_autonomous_mode(action: Action, on_conflict: str) -> ActionResult:
    return _splice_agents_md_block(
        action, on_conflict,
        desired=autonomous_mode_block_text(),
        begin=AUTONOMOUS_BEGIN_MARKER,
        end=AUTONOMOUS_END_MARKER,
        label="autonomous-mode",
    )
