"""Drift checks for the ship delegator, its machine env file / task prefix, and the ``gh ship`` alias."""

from __future__ import annotations

import os
from pathlib import Path

from .actions.ship_delegator import (
    env_file_pins_transient_root,
    repo_self_hosts_ship,
    resolve_ship_delegator,
    ship_env_file_content,
    ship_env_file_path,
    transient_ship_root_skip_reason,
)
from .plan import Action
from .drift_model import DriftItem, DriftReport


def _nearest_existing_ancestor(path: Path) -> Path | None:
    """The deepest ancestor of ``path`` (inclusive) that exists on disk (lexists), or ``None``.

    Used to diagnose a blocked ``mkdir(parents=True)``: if the returned ancestor is not a
    directory, creating ``path`` errors there. ``lexists`` so a dangling symlink counts as the
    blocker it really is (mkdir errors on it too).
    """
    for candidate in (path, *path.parents):
        if os.path.lexists(candidate):
            return candidate
    return None


def check_ship_env_for_dropped_repo_action(action: Action, report: DriftReport) -> None:
    """The ``ship_env`` half of the ship-delegator drift check, for a NON-GIT status run.

    ``rig status`` in a non-git dir drops repo-scoped actions before drift detection (they have
    no repo layer to report under) — but ``rig apply`` does NOT drop them, and it still
    reconciles the MACHINE-level env file there (the delegator write skips only the git
    ``info/exclude`` part). Dropping the whole action would therefore hide GLOBAL ship_env drift
    apply would repair — status could look clean in ``~`` while every portable delegator on the
    machine exits 127. This runs just the env-file check for such a dropped action, keeping
    status/apply parity for the machine-wide artifact.

    A non-git target is never self-hosting (``repo_self_hosts_ship`` needs a git toplevel), so
    no self-hosting skip applies here. A malformed action (no ``canonical_ship``) fails CLOSED,
    mirroring the in-git guard: apply errors on it (it cannot render the env file), so status
    must flag it too — but emitted under ``ship_env`` (GLOBAL, renderable outside git), since
    the repo-categorised ``ship_delegator`` item has no repo layer to render under here.
    """
    raw_canonical = str(action.options.get("canonical_ship", "")).strip()
    if not raw_canonical:
        report.items.append(
            DriftItem("modified", "ship_env", "env-file", ship_env_file_path(),
                      "ship_delegator action has no canonical_ship (malformed plan; apply "
                      "errors on it and cannot render the machine env file)")
        )
        return
    _check_ship_env_file(Path(raw_canonical), report)


def _check_ship_env_file(canonical: Path, report: DriftReport) -> None:
    """Flag drift on the MACHINE-level ``agent-tools/env`` file (category ``ship_env``).

    Parity with the runner's three outcomes: a NON-FILE at the path or an unreadable file makes
    apply ERROR (not rewrite), so report those as ``modified`` with the real failure — never a
    misleading "missing (apply rewrites it)". Only a genuinely absent file is ``missing``.
    ``is_symlink() or lexists-and-not-file`` matches ``_reconcile_ship_env_file`` exactly: a
    dangling symlink, a dir, or ANY symlink is a non-file apply refuses to replace. A NON-DIRECTORY
    on the PARENT chain (e.g. ``~/.config/agent-tools`` exists as a file) is the same class: the
    env path itself does not exist, but apply's ``mkdir(parents=True)`` errors on it — so it must
    be ``modified`` with the real failure, never "missing (apply rewrites it)".
    """
    # Order mirrors the runner's `_reconcile_ship_env_file` EXACTLY so status/apply never disagree.
    # Apply errors on an EXISTING non-file/symlink or an unreadable file BEFORE it would skip a
    # transient source, so those checks run first here too (a genuinely broken machine env file
    # surfaces even when THIS status run's source checkout is throwaway). The transient-source skip
    # then matches apply's skip (which happens before the content write / the absent-path mkdir), so
    # the parent-blocker + desired-content checks — which apply reaches only when it actually WRITES
    # (never under a transient source) — run AFTER it.
    env_path = ship_env_file_path()
    if env_path.is_symlink() or (os.path.lexists(env_path) and not env_path.is_file()):
        report.items.append(
            DriftItem("modified", "ship_env", "env-file", env_path,
                      "a non-file sits at the machine env file path (apply errors on it)")
        )
        return
    try:
        # UnicodeDecodeError is a ValueError, not an OSError — a non-UTF-8 byte in the file must
        # yield a drift item (apply errors on it the same way), never crash detect() itself.
        env_current = env_path.read_text(encoding="utf-8") if env_path.is_file() else None
    except (OSError, UnicodeDecodeError) as exc:
        report.items.append(
            DriftItem("modified", "ship_env", "env-file", env_path,
                      f"machine env file unreadable (apply errors on it): {exc}")
        )
        return
    # Transient (temp-dir) source + persistent pointer: apply SKIPS the write, so status must NOT
    # flag drift apply would refuse to make. Placed after the structural/read checks, before the
    # absent-path + content checks — the exact split apply uses. BUT never hide an ALREADY-poisoned
    # pointer: if the existing env file itself pins a transient root (the vanished-tmp bug state),
    # flag it REGARDLESS of whether it equals the current (also-transient) desired content — apply
    # can't repair it from a throwaway source either, so it surfaces as drift until a durable apply
    # runs (same shape as the skipped_user case). Return unconditionally afterwards to mirror apply's
    # skip (a durable-good or absent existing pointer is left clean).
    if transient_ship_root_skip_reason(canonical) is not None:
        if env_file_pins_transient_root(env_current):
            report.items.append(
                DriftItem("modified", "ship_env", "env-file", env_path,
                          "machine env file pins a TRANSIENT (temp-dir) AGENT_TOOLS_ROOT that vanishes "
                          "on cleanup; apply cannot repair it from a transient source — re-run "
                          "`rig apply` from a durable agent-tools checkout (e.g. ~/xp/agent-tools)")
            )
        return
    if not os.path.lexists(env_path):
        blocker = _nearest_existing_ancestor(env_path.parent)
        # is_dir() follows symlinks, matching mkdir(parents=True, exist_ok=True): a symlink
        # RESOLVING to a dir is traversable (no error); a plain file or dangling link blocks.
        if blocker is not None and not blocker.is_dir():
            report.items.append(
                DriftItem("modified", "ship_env", "env-file", env_path,
                          f"a non-directory blocks the env file's parent path at {blocker} "
                          "(apply errors on it)")
            )
            return
    if env_current != ship_env_file_content(canonical):
        report.items.append(
            DriftItem("missing" if env_current is None else "modified",
                      "ship_env", "env-file", env_path,
                      "machine env file with AGENT_TOOLS_ROOT absent/stale (apply rewrites it; "
                      "a delegator with no $AGENT_TOOLS_ROOT and no repo-local ship.sh exits 127)")
        )


def _check_ship_task_prefix(action: Action, report: DriftReport) -> None:
    """Flag drift on ``.ship-config``'s ``SHIP_TASK_CODE_PREFIX=`` line.

    Recomputes the expected line from ``action.options["code_prefix"]`` — the SAME value
    :func:`_do_provision_ship_task_prefix` (actions/runner.py) merges in — so status and apply
    read one classification, per the repo's "drift is surfaced both ways, never silently
    reconciled" rule: a hand-edited or deleted line must not report clean.
    """
    prefix = str(action.options.get("code_prefix", "")).strip()
    if not prefix:
        return  # nothing to provision (mirrors the runner's own skip on an unset prefix)
    path = action.target / ".ship-config"
    expected = f"SHIP_TASK_CODE_PREFIX={prefix}"
    if not path.is_file():
        report.items.append(DriftItem("missing", "task", "code_prefix", path, f"{path} does not exist"))
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        report.items.append(DriftItem("modified", "task", "code_prefix", path, f"could not read {path}: {exc}"))
        return
    found = [line for line in lines if line.startswith("SHIP_TASK_CODE_PREFIX=")]
    if not found:
        report.items.append(DriftItem("missing", "task", "code_prefix", path, "SHIP_TASK_CODE_PREFIX= line is absent"))
    elif found != [expected]:
        # Anything OTHER than exactly one line matching `expected` is drift — not just a wrong
        # value. `_do_provision_ship_task_prefix` (runner.py) collapses a pre-existing DUPLICATE
        # managed line to one on the next apply, so `found == [expected, "SHIP_TASK_CODE_PREFIX=
        # OLD"]` (the correct line present ALONGSIDE a stale extra one) must not report clean —
        # `expected in found` alone would miss that apply is still about to rewrite the file.
        detail = (
            f"SHIP_TASK_CODE_PREFIX line is {found[0]!r}, expected {expected!r}"
            if len(found) == 1
            else f"found {len(found)} SHIP_TASK_CODE_PREFIX= lines {found!r}, expected exactly one {expected!r}"
        )
        report.items.append(DriftItem("modified", "task", "code_prefix", path, detail))


def _check_ship_delegator(action: Action, report: DriftReport) -> None:
    """Flag drift on the per-repo ``.claude/scripts/pr-ship.sh`` (``gh ship`` delegator).

    Switches on the SAME :func:`resolve_ship_delegator` ``state`` apply uses, so status and apply
    read one classification:

    - ``ok``       → no drift (file correct + git-ignored).
    - ``create``   → ``missing``: the delegator file is absent (apply writes it + ignores it).
    - ``update``   → ``modified`` when the FILE differs, else ``missing`` when the file is correct
                     but the ``.git/info/exclude`` entry is gone (an un-ignored delegator would
                     dirty the worktree → ship refuses). ``apply`` reconciles either.
    - ``io_error`` → ``modified``: a directory/unreadable sits at the delegator path; apply errors.

    Also byte-compares the MACHINE-level env file (``$XDG_CONFIG_HOME/agent-tools/env`` — where the
    agent-tools root lives now that the delegator itself is a portable constant): a missing/stale
    env file is drift apply repairs, and without it a provisioned delegator (absent an explicit
    ``$AGENT_TOOLS_ROOT`` and a repo-local ship.sh) exits 127. Its items are emitted under the
    ``ship_env`` category — classified GLOBAL in :mod:`riglib.layers`, because the file is a
    machine-wide artifact, not this repo's — so ``rig status`` renders it under the machine layer
    instead of blaming the repo. (The check runs from the repo-scoped ship_delegator action — for
    ANY non-self-hosting target, git or plain dir, mirroring apply, which reconciles the env file
    for those targets too; only a self-hosting repo skips it, see below.)
    """
    # Mirror the runner's fail-closed guard: a malformed action with no canonical_ship must NOT
    # pass silently — apply errors on it (it cannot render the machine env file), so status must
    # flag it too or the two would disagree. Surface it as a modified item.
    raw_canonical = str(action.options.get("canonical_ship", "")).strip()
    if not raw_canonical:
        report.items.append(
            DriftItem("modified", "ship_delegator", "delegator", action.target,
                      "ship_delegator action has no canonical_ship (malformed plan)")
        )
        return
    canonical = Path(raw_canonical)
    # A SELF-HOSTING repo (carries its own ci/ship/ship.sh — agent-tools) never reads the env
    # file: the delegator's repo-local branch wins first, and apply skips the env reconcile for
    # it. Skip the env check for such a repo so status can't flag drift apply would never repair
    # (status/apply parity) — every other target, including a non-git dir carrying a stray
    # ci/ship/ship.sh (whose runtime git probe fails, so the env file IS needed), still gets the
    # full check. `repo_self_hosts_ship` is the ONE shared predicate apply uses too.
    if not repo_self_hosts_ship(action.target):
        _check_ship_env_file(canonical, report)
    r = resolve_ship_delegator(action.target)
    if r.state == "ok":
        return
    if r.state == "create":
        report.items.append(
            DriftItem("missing", "ship_delegator", "delegator", r.delegator_path,
                      "pr-ship.sh not provisioned (apply writes it + ignores it in .git/info/exclude)")
        )
    elif r.state == "io_error":
        report.items.append(
            DriftItem("modified", "ship_delegator", "delegator", r.delegator_path, r.detail)
        )
    elif r.state == "update":
        # The two degradations are INDEPENDENT and both must surface (apply fixes both, but status
        # must show the full picture): (a) the FILE differs from the rig-generated delegator, and
        # (b) the .git/info/exclude entry is missing (an un-ignored delegator dirties the worktree →
        # ship refuses). The resolver already classified the file (``file_correct``), so trust it
        # rather than re-reading (avoids a redundant, racy TOCTOU read).
        if not r.file_correct:
            report.items.append(
                DriftItem("modified", "ship_delegator", "delegator", r.delegator_path,
                          "provisioned pr-ship.sh differs from the rig-generated delegator")
            )
        if not r.exclude_ok:
            report.items.append(
                DriftItem("missing", "ship_delegator", "ignore", r.exclude_path or r.delegator_path,
                          "pr-ship.sh present but NOT git-ignored (apply adds it to .git/info/exclude "
                          "so it can't dirty the worktree)")
            )


def _check_gh_ship_alias(action: Action, report: DriftReport) -> None:
    """Flag drift on the machine-global ``gh ship`` alias (gh config, ``aliases.ship``).

    Switches on the SAME :func:`resolve_gh_ship_alias` state apply uses, so status and apply read
    one classification:

    - ``ok``/``no_gh`` → no drift (either already correct, or gh is absent and neither side can
      manage the alias — reporting a "missing" alias rig could never set would break status/apply
      parity).
    - ``create``       → ``missing``: gh has no ``ship`` alias (apply sets it).
    - ``update``       → ``modified``: a ``ship`` alias exists but differs from the rig-managed
      expansion (apply overwrites it unless ``on_conflict=skip``).

    The alias is a machine-wide artifact (``gh_ship_alias`` is classified GLOBAL in
    :mod:`riglib.layers`), so ``rig status`` renders it under the machine layer, not this repo's.

    ci-only combo (``ship_delegator`` disabled, ``ci.items.ship.gh_alias`` on): the plan builder
    carries ``canonical_ship`` on this action because no ``provision_ship_delegator`` action owns
    the machine env file — so this check must also byte-compare that file (apply reconciles it here,
    :func:`riglib.actions.runner._do_provision_gh_ship_alias`, so status must flag its drift or the
    two disagree). No self-hosting skip: the machine-global alias needs the env file regardless of
    the current repo. When the delegator owns the env file this option is absent — its own drift
    check covers the file, so we do NOT double-report it here.
    """
    from .gh_ship_alias import GH_SHIP_ALIAS_CATEGORY, gh_config_path, resolve_gh_ship_alias

    raw_canonical = str(action.options.get("canonical_ship", "")).strip()
    if raw_canonical:
        _check_ship_env_file(Path(raw_canonical), report)

    r = resolve_gh_ship_alias()
    # ok → correct; no_gh → gh absent (unmanageable); unknown → gh config unreadable (rig refuses
    # to clobber). All three are apply no-ops, so status reports no drift (status/apply parity).
    if r.state in ("ok", "no_gh", "unknown"):
        return
    target = gh_config_path()
    if r.state == "create":
        report.items.append(
            DriftItem("missing", GH_SHIP_ALIAS_CATEGORY, "alias", target,
                      "`gh ship` alias not set (apply sets it so `gh ship <PR>` reaches the ship gate)")
        )
    elif r.state == "update":
        report.items.append(
            DriftItem("modified", GH_SHIP_ALIAS_CATEGORY, "alias", target,
                      "`gh ship` alias differs from the rig-managed expansion (apply reconciles it)")
        )
