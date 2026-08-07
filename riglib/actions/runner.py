"""The plan executor — runs each :class:`~riglib.plan.Action`, returns results.

Stdlib only. Each ``_do_<kind>`` handler implements one action kind and returns an
:class:`ActionResult`. Non-fatal errors are collected (the runner continues); the caller
decides how to surface them.
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..config import (
    GITIGNORE_BEGIN_MARKER,
    GITIGNORE_BLOCK_COMMENT,
    GITIGNORE_END_MARKER,
    OMP_HOOK_BRIDGE_EXTENSION_NAME,
    OPENCODE_HOOK_BRIDGE_EXCLUDE_BEGIN_MARKER,
    OPENCODE_HOOK_BRIDGE_EXCLUDE_COMMENT,
    OPENCODE_HOOK_BRIDGE_EXCLUDE_END_MARKER,
    OPENCODE_HOOK_BRIDGE_PLUGIN_NAME,
    SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER,
    SHIP_DELEGATOR_EXCLUDE_COMMENT,
    SHIP_DELEGATOR_EXCLUDE_END_MARKER,
    SHIP_DELEGATOR_REL_PATH,
    WORKTREES_DIR_NAME,
    WORKTREES_EXCLUDE_BEGIN_MARKER,
    WORKTREES_EXCLUDE_COMMENT,
    WORKTREES_EXCLUDE_END_MARKER,
    linter_path_escapes_repo,
)
from ..github_actions import (
    build_permissions_body,
    build_workflow_permissions_body,
    normalize_permissions,
    normalize_workflow_permissions,
)
from ..github_auth import ensure_browser_auth, ensure_gh_auth, reset_auth_gate
from ..github_browser import build_command_plan as build_browser_plan
from ..github_browser import desired_toggles as browser_desired_toggles
from ..github_ghas import (
    SUBRESOURCE_KNOBS,
    build_security_analysis_body,
    desired_code_scanning,
    desired_subresource,
    normalize_security_analysis,
)
from ..github_merge import build_merge_body, normalize_merge
from ..harness_mode import HARNESS_MODES
from ..github_ruleset import (
    UNATTRIBUTED_APPROVAL_KEY,
    build_ruleset_body,
    carry_over_unattributed_approval,
    find_managed_ruleset,
    normalize_ruleset,
    parse_github_remote,
)
from ..logging import log_event
from ..paths import expand_user_path
from ..plan import SHIP_TASK_CODE_PREFIX_RE, Action, InstallPlan
from .. import ecosystem_readme
from .. import project_tools
from . import fsutil


@dataclass
class ActionResult:
    action: Action
    status: str  # created | updated | skipped | backed_up | error
    detail: str
    backup: Path | None = None

    @property
    def ok(self) -> bool:
        return self.status != "error"


@dataclass
class ApplyReport:
    results: list[ActionResult] = field(default_factory=list)

    @property
    def errors(self) -> list[ActionResult]:
        return [r for r in self.results if r.status == "error"]

    @property
    def changed(self) -> int:
        return sum(1 for r in self.results if r.status in ("created", "updated", "backed_up"))

    def summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.results:
            out[r.status] = out.get(r.status, 0) + 1
        return out


# omp config.yml paths created during THIS run_plan. Approval and web_search share
# that file; on_conflict=skip must not treat a file we just created as user content
# (rig-cli#447 first-run: the second writer would otherwise skip the missing key).
_omp_yaml_created_this_run: set[str] = set()


def _reset_omp_yaml_created() -> None:
    _omp_yaml_created_this_run.clear()


def _mark_omp_yaml_created(path: Path) -> None:
    _omp_yaml_created_this_run.add(str(path))


def _omp_yaml_skip_existing(path: Path, existed: bool, on_conflict: str) -> bool:
    """True when skip must leave this yaml alone (a user file, not our first-run create)."""
    return existed and on_conflict == "skip" and str(path) not in _omp_yaml_created_this_run


def run_plan(
    plan: InstallPlan,
    *,
    dry_run: bool = False,
    progress: Callable[[ActionResult], None] | None = None,
    on_start: Callable[[Action], None] | None = None,
) -> ApplyReport:
    """Execute (or dry-run) every action in the plan. Returns the collected report.

    ``on_start(action)`` fires BEFORE each action dispatches (so a caller can show a slow phase as
    in-flight — silence during a hung runner ≠ hang); ``progress(result)`` fires AFTER it
    completes. Both are optional and best-effort in caller code.
    """
    # Fresh auth-gate state per run: a new apply re-notifies + re-waits for a missing login (the user
    # may have logged in since), but WITHIN this run the per-action gate dedups (no ~5× push/wait).
    reset_auth_gate()
    _reset_omp_yaml_created()
    report = ApplyReport()
    for action in plan.actions:
        if on_start is not None and not dry_run:
            on_start(action)
        if dry_run:
            res = ActionResult(action, "planned", action.describe())
        else:
            try:
                res = _dispatch(action, plan.on_conflict)
            except Exception as exc:  # noqa: BLE001 — collect, never abort the whole run
                res = ActionResult(action, "error", f"{type(exc).__name__}: {exc}")
        report.results.append(res)
        log_event(
            "rig.action",
            kind=action.kind,
            item=f"{action.category}/{action.item}",
            status=res.status,
        )
        if progress is not None:
            progress(res)
    return report


def _dispatch(action: Action, on_conflict: str) -> ActionResult:
    handler = _HANDLERS.get(action.kind)
    if handler is None:
        return ActionResult(action, "error", f"no handler for action kind '{action.kind}'")
    return handler(action, on_conflict)


# ── handlers ────────────────────────────────────────────────────────────────────
def _do_record_mode(action: Action, on_conflict: str) -> ActionResult:
    return ActionResult(action, "skipped", f"mode/{action.item}: policy recorded")


def _skill_backup_dir(skill_target: Path) -> Path:
    """Where a conflict-backup of an installed skill goes — a sibling ``.rig-backups/`` OUTSIDE
    the scanned skills dir.

    ``skill_target`` is ``<skills_target>/<name>`` (e.g. ``~/.agents/skills/naming``), so its
    parent is the natively-scanned skills dir. A same-parent ``<name>.rig-bak-*/`` backup still
    holds a ``SKILL.md`` and opencode (which auto-scans ``~/.agents/skills``) re-discovers it as
    a duplicate skill (rig-cli#57). Putting the backup one level up — next to the skills dir,
    not inside it — keeps the restore point without it ever being scanned as a skill.
    """
    return skill_target.parent.parent / ".rig-backups"


def _do_copy_skill(action: Action, on_conflict: str) -> ActionResult:
    out = fsutil.copy_tree(
        action.source, action.target, on_conflict, backup_dir=_skill_backup_dir(action.target)
    )
    return ActionResult(action, out.status, out.detail, out.backup)


def skill_harness_link_target(action: Action) -> tuple[Path, Path]:
    """The (symlink_path, desired_destination) a ``link_skill_harness`` action maintains.

    The symlink lives at ``action.target`` inside the harness skill dir; it should resolve to
    ``action.source`` (the installed skill in skills_target). Shared with drift so the install
    and the drift check agree on what "correct" means. The destination is the absolute
    installed-skill path — an absolute target keeps the link valid regardless of the relative
    distance between the two dirs.
    """
    return action.target, action.source.resolve()


def _do_link_skill_harness(action: Action, on_conflict: str) -> ActionResult:
    """Maintain an idempotent symlink making an installed skill discoverable by the harness.

    The agent harness lists/loads Skill-tool skills from its own dir (claude-code:
    ``~/.claude/skills``), NOT from ``skills_target`` (``~/.agents/skills``). So for every
    enabled skill rig symlinks ``<harness_skill_dir>/<skill> -> <skills_target>/<skill>``.

    - already a symlink to the correct destination → no-op (``skipped``).
    - a symlink to a WRONG destination → re-point it (``updated``); never honors on_conflict
      backup for a stale symlink (a symlink carries no user data to preserve).
    - a real (non-symlink) directory/file already there → LEAVE IT (``skipped`` with a
      warning). Some harness skills are real dirs (h-reason, debate-swarm,
      moshi-best-practices); clobbering them would destroy real content. on_conflict=overwrite
      does NOT override this — a real dir at the harness path is never rig's to replace.
    """
    link_path, dest = skill_harness_link_target(action)
    if not dest.exists():
        # the installed skill the link should point at isn't on disk — the copy_skill action
        # surfaces that failure; don't create a dangling link on top of it.
        return ActionResult(action, "error", f"skill-link/{action.item}: install target missing: {dest}")
    if _link_targets_itself(link_path, dest):
        # link would point the installed skill at itself — never clobber the real dir/file
        # with a self-referential symlink (see _link_targets_itself).
        return ActionResult(
            action, "skipped",
            f"skill-link/{action.item}: source == target ({dest}), skipping to avoid self-symlink",
        )
    link_path.parent.mkdir(parents=True, exist_ok=True)

    if link_path.is_symlink():
        try:
            current = link_path.readlink()
        except OSError as exc:
            return ActionResult(action, "error", f"skill-link/{action.item}: cannot read symlink {link_path}: {exc}")
        if _same_link_dest(link_path, current, dest):
            return ActionResult(action, "skipped", f"skill-link/{action.item}: already linked → {dest}")
        # stale/wrong symlink — re-point it (a symlink holds no user data; no backup needed).
        link_path.unlink()
        link_path.symlink_to(dest)
        return ActionResult(action, "updated", f"skill-link/{action.item}: re-pointed → {dest}")

    if link_path.exists():
        # a REAL dir/file already occupies the harness path (not a rig symlink). Never clobber
        # it — it may be a hand-authored skill (h-reason, debate-swarm). Leave it, warn.
        kind = "directory" if link_path.is_dir() else "file"
        return ActionResult(
            action, "skipped",
            f"skill-link/{action.item}: a real {kind} already exists at {link_path} "
            f"(not a rig symlink) — left untouched; skill may shadow the installed one",
        )

    link_path.symlink_to(dest)
    return ActionResult(action, "created", f"skill-link/{action.item}: linked {link_path} → {dest}")


def _link_targets_itself(link_path: Path, dest: Path) -> bool:
    """True when a symlink ``link_path -> dest`` would point the file at its OWN location.

    Normalizes both to an absolute path with the PARENT resolved (collapsing any symlinked
    ancestor dirs) but the leaf name kept verbatim — we must NOT ``resolve()`` ``link_path``
    itself: it may already BE the self-referential symlink we are guarding against (whose
    ``resolve()`` raises ``OSError: Too many levels of symbolic links``). When both normalize
    to the same path, creating the link would replace the real source file with a symlink to
    itself — the 2026-07-15 opencode-bridge corruption. Callers must skip in that case.
    """
    try:
        a = link_path.parent.resolve(strict=False) / link_path.name
        b = dest.parent.resolve(strict=False) / dest.name
    except OSError:
        # cannot prove the link is NOT self-referential (e.g. an ancestor symlink loop or a
        # permissions error on resolve) — fail SAFE: treat it as self and let the caller skip,
        # never fall through to the clobber-and-symlink path on an unresolvable parent.
        return True
    return a == b


def _same_link_dest(link_path: Path, current: Path, dest: Path) -> bool:
    """True when ``current`` (the symlink's stored target) resolves to the desired ``dest``.

    Handles both an absolute stored target and a relative one (resolved against the link's
    parent), so a correct link written either way is recognized as a no-op.
    """
    if current.is_absolute():
⚠ 1 unresolved conflict detected
- ours = HEAD
- theirs = a530cf2 (feat(task): add task.code_prefix, reconciled into .ship-config)
- base = parent of a530cf2 (feat(task): add task.code_prefix, reconciled into .ship-config)
NOTICE: Inspect a block by reading `conflict://<N>` (add `/ours` / `/theirs` / `/base` to render a single side). Resolve with `write({ path: "conflict://<N>", content })`, or bulk-resolve every registered conflict with `write({ path: "conflict://*", content })`. Writes replace ONLY the marker block (markers + all sides) — never repeat the lines before/after it; they stay in place.
`content` shorthand: a line that is exactly `@ours` / `@theirs` / `@base` / `@both` expands to that recorded section. `@both` is ours-then-theirs with no separator — only for additive conflicts where each side adds something different; NEVER for competing edits of the same lines (pick a side or write the combined text). Lines that are not a token pass through verbatim, so `"// keep both\n@ours\n@theirs"` literally writes the comment, then ours, then theirs.
Per-id bulk: `write({ path: "conflict://*", content: "1: @ours\n2: @theirs\n…" })` resolves each listed id with that side in ONE call — the cheapest way through many pick-one conflicts; unlisted ids stay registered.
Resolve each block faithfully: keep one side (`@ours`/`@theirs`), or combine them when both intents apply — never invent content beyond the recorded sides, and never stack both sides of competing edits. Resolve several conflicts in a single turn by issuing multiple `write` calls at once; ids stay valid as earlier blocks are resolved.

──── #4  L72-79 ────
<<< ours
from ..plan import Action, InstallPlan
from .. import ecosystem_readme
=== base
from ..plan import Action, InstallPlan
>>> theirs
from ..plan import SHIP_TASK_CODE_PREFIX_RE, Action, InstallPlan

[Showing lines 1-300 of 8471. Use :301 to continue]