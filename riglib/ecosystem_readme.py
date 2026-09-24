"""The canonical "Ecosystem" README section — one registry, rendered into every repo.

Every ecosystem repo hand-copies an ``## Ecosystem`` README section listing sibling tools (see
rig-cli's own ``README.md``), and it silently drifts because there was no reconciler for it —
only copy-paste (rig-cli#308). This module owns the ONE canonical list of ecosystem tools and
renders it into the marker-delimited block :mod:`riglib.actions.runner` splices into a target
repo's README.md (``_do_apply_ecosystem_readme``, reusing the same ``_splice_managed_block``
machinery the codex execpolicy .rules block already uses).

Why the registry lives HERE, not in an agent-tools carrier
------------------------------------------------------------
Every OTHER repo-scoped block (skills, agent-hooks, CI, MCP) sources its content from a checked-out
agent-tools carrier (see ``riglib/catalog.py``); this one is a deliberate exception. agent-tools has
no tool-description registry today, and adding one would mean a cross-repo change plus dragging the
real-catalog coverage into this ticket's scope. The tradeoff this accepts: "canonical" is canonical
per INSTALLED rig VERSION, not globally — a repo running an older rig and a repo running a newer one
can disagree on what the block should say until both re-apply on a current rig. This mirrors how the
omp guard extension (:mod:`riglib.omp_guard`) is also rig-owned, generated content rather than an
agent-tools carrier. If the tool list needs to grow beyond "rig's own release cadence is fine",
promoting it to an agent-tools catalog item is the natural next step — not a rewrite of this module.

Stdlib-only at import time (the AGENTS.md hard rule).
"""

from __future__ import annotations

from dataclasses import dataclass

# The begin/end markers ``riglib.actions.runner`` splices the rendered block between. Kept HTML
# comments so they render invisibly in every Markdown viewer instead of showing up as text.
ECOSYSTEM_README_BEGIN_MARKER = "<!-- rig:ecosystem-block -->"
ECOSYSTEM_README_END_MARKER = "<!-- /rig:ecosystem-block -->"

_INTRO = (
    "Part of the [HyperIDE.ai](https://hyperide.ai) agent toolchain. Sibling tools that already "
    "solve adjacent problems — check before building your own:"
)


@dataclass(frozen=True)
class EcosystemTool:
    """One entry in the canonical ecosystem list."""

    name: str
    url: str
    description: str


# The single canonical registry. Every target repo's README gets the SAME list — there is no
# per-repo self-exclusion — so a re-apply with an unchanged registry is a no-op everywhere and the
# list stays trivially comparable across repos. Mirrors rig-cli's own README "## Ecosystem"
# section (the hand-maintained content this feature replaces): update entries HERE, never by
# hand-editing a target README's block.
ECOSYSTEM_TOOLS: tuple[EcosystemTool, ...] = (
    EcosystemTool(
        "rig-cli", "https://git.hyperide.ai/ultrabricks/rig-cli",
        "sets up a repo (and a dev machine) from a committed rig.yaml: skills, agent-hooks, "
        "git-hook dispatcher, CI gates, MCP, and the agent harness's auto/permission mode",
    ),
    EcosystemTool(
        "tg-cli", "https://git.hyperide.ai/ultrabricks/tg-cli",
        "simple Telegram CLI to send messages, photos & files, and a two-way agent bridge "
        "(reports, Q -> buttons, voice/rich)",
    ),
    EcosystemTool(
        "review-cli", "https://git.hyperide.ai/ultrabricks/review-cli",
        "multi-model read-only code review from one command: diff review, cited quorum, "
        "brainstorm, visual review, and interactive spec-review tooling",
    ),
    EcosystemTool(
        "agent-tools", "https://git.hyperide.ai/ultrabricks/agent-tools",
        "the shared catalog rig applies: portable agent skills, agent-hooks, the global "
        "git-hook dispatcher, CI gates, and MCP servers",
    ),
    EcosystemTool(
        "draw-cli", "https://git.hyperide.ai/ultrabricks/draw-cli",
        "text-to-image via Hugging Face",
    ),
    EcosystemTool(
        "3d-cli", "https://git.hyperide.ai/ultrabricks/3d-cli",
        "scriptable CLI for the full 3D FDM lifecycle: modeling, mesh repair, slicing, and "
        "print monitoring",
    ),
    EcosystemTool(
        "task-cli", "https://git.hyperide.ai/ultrabricks/task-cli",
        "enforced ticket-system CLI for agents (GitHub Issues / Linear): acceptance criteria, "
        "motivation, and user-impact gates before work starts",
    ),
    EcosystemTool(
        "dev-cli", "https://git.hyperide.ai/ultrabricks/dev-cli",
        "project-scoped dev/e2e process runner: start/list/stop dev servers and e2e jobs",
    ),
    EcosystemTool(
        "hyperide.ai", "https://hyperide.ai",
        "Figma replacement inside VS Code — edit React components directly through AST/LSP "
        "without AI hallucinations, token waste, or context-window limits",
    ),
)


_MANAGED_NOTICE = (
    "_This section is kept in sync by `rig apply` from a canonical registry — edit it in "
    "[rig-cli](https://git.hyperide.ai/ultrabricks/rig-cli)'s `riglib/ecosystem_readme.py`, not here; a "
    "hand-edit here does not survive the next apply that reconciles this block._"
)


def render_ecosystem_block() -> str:
    """Render the marker-delimited ``## Ecosystem`` section from :data:`ECOSYSTEM_TOOLS`.

    Deterministic and pure (no disk access): the same registry always renders the same text, so a
    re-apply with an unchanged registry is a byte-identical no-op everywhere. The whole section
    (heading, intro, bullet list, and the "managed by rig" notice) lives INSIDE the markers, so a
    target repo that lacks an "## Ecosystem" heading entirely still gets a complete, self-contained
    section when the block is appended — and every target repo (not just rig-cli's own README)
    carries the notice that a hand-edit here does not survive the next apply.
    """
    lines = [ECOSYSTEM_README_BEGIN_MARKER, "", "## Ecosystem", "", _INTRO, ""]
    for tool in ECOSYSTEM_TOOLS:
        lines.append(f"- **[{tool.name}]({tool.url})** — {tool.description}")
    lines.append("")
    lines.append(_MANAGED_NOTICE)
    lines.append("")
    lines.append(ECOSYSTEM_README_END_MARKER)
    return "\n".join(lines)


def has_managed_block(text: str) -> bool:
    """True if ``text`` contains the rig-managed begin marker on its own line.

    Used to detect an ORPHANED block: a repo that turned ``ecosystem_readme.enabled`` back off
    still carries a block whose own in-block notice claims it is "kept in sync by rig apply" — a
    plan note should say so rather than let that claim silently go stale (see
    ``plan._build_ecosystem_readme``).
    """
    return any(line.strip() == ECOSYSTEM_README_BEGIN_MARKER for line in text.splitlines())



# A bare (unmarked) heading text that matches the rendered section's own heading exactly, modulo
# case -- the ONLY signal available before any rig markers exist. Deliberately narrow (an exact
# heading-line match, not a substring/fuzzy check) to avoid false positives on an unrelated section
# that merely mentions the word "ecosystem".
_BARE_HEADING = "## ecosystem"


def has_unmarked_ecosystem_heading(text: str) -> bool:
    """True if ``text`` has a bare ``## Ecosystem`` heading with NO rig markers anywhere in it.

    A target repo's primary migration case -- this feature's own stated motivation -- is a repo
    that already hand-copies an ``## Ecosystem`` section (rig-cli's own README before it adopted
    the markers is the canonical example). Splicing into a marker-less file with
    :func:`has_managed_block` false always takes the ``create``/append path in
    :func:`riglib.actions.runner._splice_managed_block`: it appends the rendered block at the END
    of the file, unconditionally. Without this check that would leave the stale hand-copied
    section exactly where it was (still drifting -- the bug #308 exists to fix) AND bolt on a
    second, misplaced ``## Ecosystem`` heading, invisible to `rig status` since the *managed*
    block would then read as perfectly in sync. Callers use this to refuse instead (a HAND
    migration -- wrap the existing section in the markers, or delete it -- same "refuse to guess"
    stance as an unbalanced marker pair) rather than ever produce that duplicate silently.
    """
    if has_managed_block(text):
        return False
    return any(line.strip().lower() == _BARE_HEADING for line in text.splitlines())
