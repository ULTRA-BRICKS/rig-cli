"""Managed-block constants (global excludes, ship delegator, hook bridges, worktrees).

Separate from the validators so plan builder, runner and drift can import the shared
markers/paths without pulling in config validation (and without an import cycle).
"""

from __future__ import annotations

# NB: ``.serena/`` is deliberately NOT here (Serena state is COMMITTED shared project memory).
# ``.metadata_never_index`` (rig-cli#331): the ``spotlight:`` sweep drops this macOS Spotlight
# sentinel into dependency/build dirs — including a COMMITTED ``vendor/`` inside a git working
# tree — so without a machine-wide ignore every worktree shows an untracked file and ``gh ship``
# refuses it as dirty. A bare basename pattern matches at any depth.
GITIGNORE_DEFAULT_ENTRIES = ("**/.claude/worktrees/", ".metadata_never_index")

# The XDG default rig points ``core.excludesfile`` at when the user has NOT already set it. Git's
# documented global-ignore location is ``$XDG_CONFIG_HOME/git/ignore`` (``~/.config/git/ignore``).
# Defined here so the plan builder and runner agree on the fallback path; the runner expands ``~``
# and ``$XDG_CONFIG_HOME`` at apply time so a committed config stays portable.
GITIGNORE_DEFAULT_EXCLUDESFILE = "~/.config/git/ignore"

# The markers that fence rig's managed block in the global excludes file. Defined here (the schema
# layer, stdlib-only) so config validation can reject an entry that collides with a marker WITHOUT
# importing the actions runner (which would form a config→plan→config import cycle); the runner
# imports these from config so the two never drift.
GITIGNORE_BEGIN_MARKER = "# >>> rig-managed (do not edit) >>>"
GITIGNORE_END_MARKER = "# <<< rig-managed (do not edit) <<<"

# A fixed explanatory comment rig writes as the FIRST line INSIDE the managed block, right after the
# begin marker, so a human reading the global excludes file knows what the block is and why it is
# there. It is part of the canonical block text (rendered byte-for-byte), so it must match what is
# ALREADY on a provisioned machine for a re-apply to be a true zero-churn no-op. Do not reword
# casually — a change here makes the next apply rewrite every provisioned machine's block.
# (Reworded once with rig-cli#331, which added the sentinel entry and rewrote every block anyway.)
GITIGNORE_BLOCK_COMMENT = (
    "# Claude Code creates throwaway worktrees under each repo's .claude/worktrees/ and rig's "
    "spotlight sweep drops .metadata_never_index into dependency dirs; rig ignores both globally."
)


# ── ship_delegator ─────────────────────────────────────────────────────────────────
# rig provisions a per-repo ``.claude/scripts/pr-ship.sh`` thin delegator so the repo-keyed
# ``gh ship`` alias (``gh ship`` → ``<repo>/.claude/scripts/pr-ship.sh``) works in EVERY managed
# repo on a clean machine — not only in agent-tools, which is the only repo that historically
# carried it. The delegator execs the canonical ``ci/ship/ship.sh`` (agent-tools' real ship
# implementation): repo-local first (so agent-tools self-hosts), else via ``$AGENT_TOOLS_ROOT``
# — from the environment or from the machine-level ``$XDG_CONFIG_HOME/agent-tools/env`` file rig
# apply writes (the delegator itself is a portable constant, no baked path). The file is IGNORED in
# the repo's ``.git/info/exclude`` (a per-repo, never-committed exclude) so it does not dirty the
# worktree — ship refuses a dirty tree, so an un-ignored provisioned file would break the very
# command it enables. Defined here (the schema layer, stdlib-only) so the plan builder, runner,
# and drift reference the SAME relative path + marker; the runner imports them so the two never
# drift.
SHIP_DELEGATOR_REL_PATH = ".claude/scripts/pr-ship.sh"

# The markers that fence rig's managed entry in the per-repo ``.git/info/exclude``. Distinct text
# from the GLOBAL excludes markers so the two managed blocks never collide if a future change put
# both in one file (they don't today — global vs per-repo). Same begin/end shape the global block
# uses, reconciled by the same marker-block machinery.
SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER = "# >>> rig-managed ship delegator (do not edit) >>>"
SHIP_DELEGATOR_EXCLUDE_END_MARKER = "# <<< rig-managed ship delegator (do not edit) <<<"
SHIP_DELEGATOR_EXCLUDE_COMMENT = (
    "# rig provisions .claude/scripts/pr-ship.sh (the `gh ship` delegator); ignored so it "
    "does not dirty the worktree."
)

# rig provisions the opencode hook bridge as a repo-local symlink so it can run after project
# plugins. The symlink target is machine-local, so it must be ignored per repo rather than
# committed.
OPENCODE_HOOK_BRIDGE_EXCLUDE_BEGIN_MARKER = "# >>> rig-managed opencode hook bridge (do not edit) >>>"
OPENCODE_HOOK_BRIDGE_EXCLUDE_END_MARKER = "# <<< rig-managed opencode hook bridge (do not edit) <<<"
OPENCODE_HOOK_BRIDGE_EXCLUDE_COMMENT = (
    "# rig provisions the opencode hook bridge plugin symlink; ignored so it does not dirty "
    "the worktree."
)
OPENCODE_HOOK_BRIDGE_PLUGIN_NAME = "zz-agent-tools-hook-bridge.js"

# rig provisions the omp hook bridge as a user-level symlink (or, with a custom hooks_dir,
# a small wrapper) into omp's auto-discovered ``extensions/`` dir — parallel to the opencode
# plugin above, but ``.ts`` (omp natively loads TypeScript extension modules, and this name
# keeps the same "zz-" late-load convention the guard extension and the opencode plugin use).
OMP_HOOK_BRIDGE_EXTENSION_NAME = "zz-agent-tools-hook-bridge.ts"


# ── agent worktree convention (`rig worktree create`) ───────────────────────────────
# rig standardizes WHERE a new agent/harness worktree lands: ``<repo>/.worktrees/<name>`` — a
# single, discoverable, IN-repo location, replacing the several ad hoc conventions that had grown
# up around the ecosystem (Claude Code's own throwaway ``.claude/worktrees/`` — a separate, already
# -solved problem, see ``GITIGNORE_DEFAULT_ENTRIES`` above — plus a bare, uncoded ``.worktrees/``
# some repos and some sibling-of-repo layouts had adopted by hand). ``rig worktree create`` is the
# one place that plants a NEW worktree, so every repo it touches converges on this one path.
#
# The directory is LOCAL scratch space (a machine's working copies of in-progress branches), never
# meant to be shared or committed — so it belongs in the per-repo, never-committed
# ``.git/info/exclude``, exactly like the ship delegator above, and specifically NOT in the
# committed ``.gitignore`` (that file is shared with every clone/contributor; a worktree
# convention is a local, per-machine choice, and .gitignore is intentionally left untouched).
WORKTREES_DIR_NAME = ".worktrees"

# The markers that fence rig's managed entry in the per-repo ``.git/info/exclude``. Distinct text
# from the ship-delegator/opencode markers so the managed blocks in one exclude file never collide.
WORKTREES_EXCLUDE_BEGIN_MARKER = "# >>> rig-managed worktrees (do not edit) >>>"
WORKTREES_EXCLUDE_END_MARKER = "# <<< rig-managed worktrees (do not edit) <<<"
WORKTREES_EXCLUDE_COMMENT = (
    "# rig-created agent worktrees live under .worktrees/; ignored so they don't dirty the "
    "primary checkout."
)
