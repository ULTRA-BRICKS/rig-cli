"""``provision_env`` handler: managed environment variables for harness launchers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import fsutil
from ..plan import Action
from .result import ActionResult


def env_plan_from_action(action: Action):
    """Rebuild the pure :class:`~riglib.shell_env.ShellEnvPlan` an action describes.

    Shared by the install handler and the drift check so both agree on the exact desired
    artifacts from the action's options. ``Path.home()`` is the resolved HOME at apply time (a
    test monkeypatches it to a tmp HOME). Lazy import keeps the actions package import-light.

    UNLIKE ``tmux_plan_from_action``, ``rc_path``/``generated_dir`` are REQUIRED here — a
    ``KeyError`` for a missing one is a programmer error, not a runtime condition to paper over.
    ``env`` is a brand-new action kind (unlike ``tmux``, which has genuinely pre-dated some of
    its options across real upgrades), so there is no real "action persisted by an older rig"
    scenario for a bare-default fallback to serve; ``_build_env`` (``riglib/plan.py``) is the
    ONE place that resolves the defaults (including the ``~/.config`` -> ``$XDG_CONFIG_HOME``
    special case) and it always writes both keys into ``Action.options`` — so a duplicate
    fallback default here would only ever be exercised by a caller NOT going through
    ``_build_env``, and would then risk resolving differently than the plan builder did (review
    finding, discovered via exactly that divergence). Failing loudly beats resolving quietly
    wrong.
    """
    from ..shell_env import build_shell_env

    opts = action.options
    return build_shell_env(
        repo_home=Path.home(),
        rc_path=str(opts["rc_path"]),
        generated_dir=str(opts["generated_dir"]),
        vars=dict(opts.get("vars", {}) or {}),
    )


def _do_provision_env(action: Action, on_conflict: str) -> ActionResult:
    """Generate the rig-managed shell-env-vars file and ensure it is sourced from ``rc_path``.

    What it writes:
      - ``<generated_dir>/rig.env.sh`` — the rig-owned file (wholesale rewrite; one
        ``export KEY=value`` line per configured var).
      - ``rc_path`` (default ``~/.zshenv``) — POSITION-TOLERANT: if rig's CURRENT import line
        already appears anywhere, ``rc_path`` is untouched (even a user-relocated line is left
        where they put it); otherwise it is appended at the end, dropping any STALE copy (an
        old ``generated_dir``) first. Nothing but rig's own recognizable line is ever touched —
        see :func:`riglib.shell_env.desired_rc_text` for the exact predicate, shared verbatim
        with the drift check so apply and status can never disagree.

    Both writes go through :func:`riglib.actions.fsutil.write_file` — same ``on_conflict``
    policy, same backup-on-replace, same error-to-``ActionResult`` conversion for BOTH files.
    ``rc_path`` is the user's own, possibly irreplaceable file (unlike the freely-regenerable
    ``rig.env.sh``), so it gets the SAME safety treatment, not less (review finding: an earlier
    version wrote it with a bare ``write_text`` — no backup, and only ``OSError`` was caught,
    so a non-UTF-8 ``rc_path`` raised ``UnicodeDecodeError`` — a ``ValueError`` — right through
    apply uncaught).

    Idempotent: a re-apply that finds both artifacts already current is a ``skipped`` no-op.
    """
    from ..shell_env import desired_rc_text

    plan = env_plan_from_action(action)
    state = _EnvState()

    # 1) the generated rig.env.sh (wholesale, idempotent on identical bytes).
    #
    # `render_env_file()`'s key check should never actually raise — `config.validate` already
    # rejects an invalid var key before a plan is ever built — but it is a defense-in-depth
    # invariant, not a caller contract every path is guaranteed to have gone through (review),
    # so a violation is still reported as a clean ActionResult error rather than an uncaught
    # ValueError. Same for the mkdir: a file blocking `generated_dir` (or a permissions issue)
    # must not propagate raw either.
    try:
        plan.generated_dir.mkdir(parents=True, exist_ok=True)
        rendered = plan.render_env_file()
    except (OSError, ValueError) as exc:
        return ActionResult(action, "error", f"env: failed to prepare {plan.generated_file_path}: {exc}")
    env_out = fsutil.write_file(plan.generated_file_path, rendered, on_conflict)
    if env_out.status == "error":
        return ActionResult(action, "error", f"env: {env_out.detail}")
    # `on_conflict=skip` left a HAND-EDITED (or otherwise differing) rig.env.sh untouched —
    # the SAME unresolved-conflict class as the rc_path branch below. Without surfacing it,
    # `changed` would stay False while the file genuinely differs from the desired render and a
    # run where nothing else changed would report "already current" — asserting currency that
    # isn't true while `rig status` simultaneously reports it as modified.
    state.record(
        env_out,
        f"generated {plan.generated_file_path.name}",
        f"{plan.generated_file_path.name} differs and on_conflict=skip — NOT regenerated",
    )

    # 2) ensure the import line in rc_path. The read (to compute `desired`) can fail the same
    # ways the write can — a vanished/unreadable file, or non-UTF-8 content (`UnicodeDecodeError`
    # is a `ValueError`, not an `OSError` — both are caught so neither propagates raw).
    try:
        existing = plan.rc_path.read_text(encoding="utf-8") if plan.rc_path.is_file() else ""
    except (OSError, UnicodeDecodeError) as exc:
        return ActionResult(action, "error", f"env: failed to read {plan.rc_path}: {exc}", state.backup)

    desired = desired_rc_text(existing, plan)
    if desired != existing:
        failure = _write_rc(action, plan, desired, on_conflict, state)
        if failure is not None:
            return failure

    if not state.changed:
        if state.unresolved:
            return ActionResult(action, "skipped", "env: " + "; ".join(state.unresolved), state.backup)
        return ActionResult(action, "skipped", "env: already current", state.backup)
    return ActionResult(
        action, "updated", "env: " + "; ".join(state.details + state.unresolved), state.backup
    )


@dataclass
class _EnvState:
    """Accumulated outcome of the two env artifacts written by one provision run."""

    changed: bool = False
    details: list[str] = field(default_factory=list)
    # conflict-skipped writes (on_conflict=skip left the file untouched though it needed
    # reconciling) — surfaced in the result detail but NOT counted as a change (nothing was
    # written): unresolved drift, matching tmux's `skipped_conflicts` convention.
    unresolved: list[str] = field(default_factory=list)
    backup: Path | None = None

    def record(self, out, written_detail: str, unresolved_detail: str) -> None:
        """Fold one ``fsutil.write_file`` outcome into the run state."""
        if out.backup:
            # `ActionResult.backup` is a single slot; if BOTH artifacts got backed up this run,
            # rc_path's backup (recorded last) deliberately wins — rc_path is the irreplaceable
            # file, the generated one is trivially regenerable from config. Both backups still
            # exist on disk either way; only the headline pointer is singular.
            self.backup = out.backup
        if out.status != "skipped":
            self.changed = True
            self.details.append(written_detail)
        elif not out.detail.startswith("identical"):
            self.unresolved.append(unresolved_detail)


def _write_rc(action: Action, plan, desired: str, on_conflict: str, state: _EnvState) -> ActionResult | None:
    """Write the spliced rc file; return an error result, or None when the run may continue."""
    try:
        plan.rc_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return ActionResult(
            action, "error", f"env: failed to create {plan.rc_path.parent}: {exc}", state.backup
        )
    rc_out = fsutil.write_file(plan.rc_path, desired, on_conflict)
    if rc_out.status == "error":
        return ActionResult(action, "error", f"env: {rc_out.detail}", state.backup)
    # `on_conflict=skip` left rc_path UNCHANGED even though `desired != existing` — the import
    # line was NOT added, so `rig.env.sh` exists but nothing sources it yet: the vars silently
    # never take effect. That must be surfaced explicitly, not discarded by the "already
    # current" branch of the caller.
    state.record(
        rc_out,
        f"added import line to {plan.rc_path}",
        f"import line NOT added to {plan.rc_path} (on_conflict=skip) — the "
        "generated vars are not yet sourced; re-run with backup/overwrite to wire it",
    )
    return None
