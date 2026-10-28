"""Drift checks for single-file config artifacts: linter config, project tools, global excludes, git config, fj, harness wrappers, bunfig."""

from __future__ import annotations

from .actions.git_exclude import _find_marker_lines
from .actions.global_excludes import _resolve_excludes_target, resolve_global_excludes
from .actions.linter_config import _linter_label, resolve_linter_config
from .config import GITIGNORE_BEGIN_MARKER, linter_path_escapes_repo
from .plan import Action
from . import project_tools
from .drift_model import DriftItem, DriftNote, DriftReport


def _check_linter_config(action: Action, report: DriftReport) -> None:
    """Flag drift on ONE per-repo linter/formatter config file (the ``linters`` block).

    Switches on the SAME :func:`resolve_linter_config` ``state`` apply uses, so status and apply read
    one classification:

    - ``ok``       → no drift (file present + bytes correct).
    - ``create``   → ``missing``: the config file is absent (apply writes it).
    - ``update``   → ``modified``: the file exists but its bytes differ from config (apply rewrites
                     it, backing up a hand-edited file per ``on_conflict``).
    - ``io_error`` → ``modified``: a directory, a symlink, or an unreadable/non-UTF-8 file sits at
                     the path (or the path escapes the repo); apply errors.
    """
    # Do NOT strip rel_path — match the runner + validator (which reject whitespace-padded paths) so
    # status and apply operate on the identical literal value.
    rel_path = str(action.options.get("rel_path", ""))
    content = action.options.get("content")
    role = str(action.options.get("role") or "linter")
    tool = str(action.options.get("tool") or "")
    label = _linter_label(role, tool, str(action.item))
    # Mirror the runner's fail-closed guards (same label scheme, so status and apply name the broken
    # item identically): a malformed action, then a path that escapes the repo. Surface either as a
    # `modified` item so status flags it rather than silently passing.
    # `not content` rejects an empty string too (mirroring the runner + plan builder): the validator
    # requires non-empty content, so an empty one means a synthetic / replayed Action — flag it as
    # drift so status and apply agree it is broken rather than treating a 0-byte write as "in sync".
    if not rel_path or not isinstance(content, str) or not content:
        report.items.append(
            DriftItem("modified", "linters", label, action.target,
                      "linters action is missing rel_path/content (malformed plan)")
        )
        return
    if linter_path_escapes_repo(rel_path):
        report.items.append(
            DriftItem("modified", "linters", label, action.target,
                      f"linters path {rel_path!r} escapes the repo (apply refuses to write it)")
        )
        return
    r = resolve_linter_config(action.target, rel_path, content)
    if r.state == "ok":
        return
    if r.state == "create":
        report.items.append(
            DriftItem("missing", "linters", label, r.target_path,
                      f"{rel_path} not provisioned (apply writes it from config)")
        )
    elif r.state == "io_error":
        report.items.append(
            DriftItem("modified", "linters", label, r.target_path, r.detail)
        )
    elif r.state == "update":
        report.items.append(
            DriftItem("modified", "linters", label, r.target_path,
                      f"provisioned {rel_path} differs from the rig-managed config")
        )


def _check_lint_policy_blocked(action: Action, report: DriftReport) -> None:
    reason = str(action.options.get("reason") or "lint policy prerequisites are blocked")
    report.items.append(DriftItem("modified", "linters", action.item, action.target, reason))


def _check_format_policy_blocked(action: Action, report: DriftReport) -> None:
    reason = str(action.options.get("reason") or "formatter policy prerequisites are blocked")
    report.items.append(DriftItem("modified", "linters", action.item, action.target, reason))


def _check_linter_bundle(action: Action, report: DriftReport) -> None:
    from .linter_carriers import resolve_bundle

    r = resolve_bundle(
        action.source, action.target, str(action.options.get("source_rel") or ""), str(action.options.get("target_rel") or "")
    )
    if r.state != "ok":
        direction = "missing" if r.state == "create" else "modified"
        report.items.append(DriftItem(direction, "linters", action.item, r.target, r.detail or f"linter bundle state: {r.state}"))


def _check_project_tool(action: Action, report: DriftReport) -> None:
    """Flag drift for one repo-local project-tool carrier or registration."""
    tool = str(action.options.get("tool") or "project-tool")
    operation = str(action.options.get("operation") or "file")
    label = f"{tool}/{action.item}"
    if operation == "register" and tool == "sverklo":
        registered, detail = project_tools.sverklo_registered(action.target)
        if not registered:
            report.items.append(
                DriftItem(
                    "missing", "project_tools", label, action.target,
                    f"sverklo registry missing this repo ({detail}) — apply runs sverklo register",
                )
            )
        return
    if operation == "reindex" and tool == "sverklo":
        return  # apply-only maintenance; no cheap read-back beyond registration.

    rel_path = str(action.options.get("rel_path") or "")
    content = action.options.get("content")
    if not rel_path or not isinstance(content, str):
        report.items.append(
            DriftItem("modified", "project_tools", label, action.target,
                      "project_tools action is missing rel_path/content (malformed plan)")
        )
        return
    r = project_tools.resolve_entry(action.target, rel_path, content, operation)
    if r.state == "ok":
        return
    if r.state == "create":
        report.items.append(
            DriftItem("missing", "project_tools", label, r.target_path,
                      f"{rel_path} not provisioned (apply writes it from project_tools)")
        )
    elif r.state == "io_error":
        report.items.append(DriftItem("modified", "project_tools", label, r.target_path, r.detail))
    elif r.state == "update":
        report.items.append(
            DriftItem("modified", "project_tools", label, r.target_path,
                      f"provisioned {rel_path} differs from project_tools config")
        )


def _check_global_excludes(action: Action, report: DriftReport) -> None:
    """Flag drift in the GLOBAL git-excludes provisioning (a GLOBAL-section drift item).

    Two coupled checks, mirroring apply's two steps:
      1. ``core.excludesfile`` — if it is unset (and no override pins a file), rig WOULD set it;
         surface that as ``missing`` so ``status`` says "apply will wire core.excludesfile". The
         resolution goes through the SAME :func:`_resolve_excludes_target` apply uses, so status and
         apply agree on the target file and whether git config needs writing.
      2. The managed block — switch on the SAME :func:`resolve_global_excludes` ``state`` apply uses:

         - ``create``   → ``missing``: no excludes file or no managed block (apply adds it).
         - ``update``   → ``modified``: a managed block exists but differs, OR the file has
                          duplicated rig-managed blocks (apply collapses to one correct block).
         - ``ok``       → no block drift item (in sync).
         - ``conflict`` → ``modified``: unbalanced markers rig won't rewrite — surfaced so the
                          operator reconciles by hand (apply leaves it untouched).
         - ``io_error`` → ``modified``: the file couldn't be read. NOT silently in-sync — rig
                          couldn't even inspect it, so a green status would mask an un-provisioned
                          ignore.
    """
    entries = [str(e) for e in action.options.get("entries", [])]
    target, needs_set, set_value = _resolve_excludes_target(action)
    if needs_set:
        report.items.append(
            DriftItem(
                "missing", "gitignore", "core.excludesfile", target,
                f"global core.excludesfile is unset (apply sets it → {set_value})",
            )
        )
    r = resolve_global_excludes(target, entries)
    if r.state == "ok":
        return
    if r.state == "create":
        report.items.append(
            DriftItem("missing", "gitignore", "block", target,
                      "rig-managed global-excludes block not present (apply adds it)")
        )
    elif r.state == "update":
        # Name the entries the file lacks (e.g. `.metadata_never_index` on a machine provisioned
        # before rig-cli#331) so the operator sees WHY the block is stale, not just that it is.
        missing = f" — missing: {', '.join(r.missing_entries)}" if r.missing_entries else ""
        report.items.append(
            DriftItem("modified", "gitignore", "block", target,
                      f"rig-managed global-excludes block differs from config{missing} "
                      "(apply reconciles it)")
        )
    elif r.state in ("conflict", "io_error"):
        report.items.append(
            DriftItem("modified", "gitignore", "block", target, r.detail)
        )


def _check_fj(action: Action, report: DriftReport) -> None:
    """Flag missing Forgejo access via the SAME :func:`fj.pending_changes` apply acts on; what only
    the user can do (log in, swap a pasted token for the browser login) is a note, never drift."""
    from . import fj

    for direction, item, target, detail in fj.pending_changes(action.options):
        report.items.append(DriftItem(direction, "fj", item, target, detail))
    for host, note in fj.login_notes(action.options):
        report.notes.append(DriftNote("fj", host, note))


def _check_git_config(action: Action, report: DriftReport) -> None:
    """Flag each declared ``git.config`` key that is unset / different in the global gitconfig.

    One :class:`DriftItem` PER KEY (item = the dotted key) so ``rig status`` names exactly which
    setting drifted. Uses the SAME :func:`riglib.gitconfig.resolve` apply and verify use. Keys the
    user set by hand that the config does not declare are never reported (the config→disk rule).
    """
    from . import gitconfig

    for st in gitconfig.resolve(action.options):
        if st.state == "ok":
            continue
        direction = "missing" if st.state == "missing" else "modified"
        report.items.append(DriftItem(direction, "git", st.key, action.target, st.detail))


def _check_harness_wrapper(action: Action, report: DriftReport) -> None:
    """Flag one kind's PATH wrapper that is missing / stale / not rig's, and a real-binary record
    that disagrees with ``harness_wrappers.real_bin``.

    Reads the SAME :func:`riglib.wrapper_provision.resolve` state apply and verify use. The item is
    the harness kind; the real-binary record is its own item (``<kind> real_bin``) so ``rig status``
    names which half drifted. A real binary the config does not declare is never judged.
    """
    from . import wrapper_provision

    state = wrapper_provision.resolve(action)
    category, item = action.category, action.item
    if state.wrapper == "relative":
        report.items.append(DriftItem("modified", category, item, state.target, wrapper_provision.relative_detail(state)))
        return
    if state.wrapper == "missing":
        report.items.append(DriftItem("missing", category, item, state.target, "wrapper not installed (`rig apply commit` writes it)"))
    elif state.wrapper == "stale":
        report.items.append(
            DriftItem("modified", category, item, state.target,
                      "wrapper differs from the one this rig writes (`rig apply commit` rewrites it)")
        )
    elif state.wrapper == "foreign":
        report.items.append(
            DriftItem("modified", category, item, state.target,
                      f"{wrapper_provision.foreign_detail(state)}; `rig apply commit` will not overwrite it — move it aside first")
        )
    if not state.real_bin_current:
        report.items.append(
            DriftItem("modified", category, f"{item} real_bin", state.target, wrapper_provision.real_bin_detail(state))
        )


def _check_bunfig(action: Action, report: DriftReport) -> None:
    """Flag drift in ``~/.bunfig.toml`` using the same :func:`bunfig.resolve` apply uses."""
    from . import bunfig

    r = bunfig.resolve(action.target)
    if r.state == "ok":
        return
    if r.state == "create":
        report.items.append(
            DriftItem("missing", "bun", "bunfig", r.path,
                      "~/.bunfig.toml missing isolated linker + globalStore (apply splices it)")
        )
        return
    if r.state == "update":
        report.items.append(
            DriftItem("modified", "bun", "bunfig", r.path,
                      "managed ~/.bunfig.toml is stale (apply rewrites it)")
        )
        return
    report.items.append(
        DriftItem("modified", "bun", "bunfig", r.path, r.detail)
    )


def check_disabled_bunfig(report: DriftReport) -> None:
    """Flag a still-installed managed block when the config disables the ``bun`` category.

    apply never deletes; so a machine that previously provisioned the bunfig block keeps it in
    ``~/.bunfig.toml`` even after the config turns the category off. With the action gone from
    the plan, ``_check_bunfig`` never runs — so without this scan the leftover block would
    report as in sync (mirrors :func:`check_disabled_global_excludes`).
    """
    from . import bunfig

    target = bunfig.bunfig_path()
    if not target.is_file():
        return
    try:
        content = target.read_text(encoding="utf-8")
    except OSError:
        return
    if bunfig.BEGIN_MARKER in content:
        report.items.append(
            DriftItem(
                "extra",
                "bun",
                "bunfig",
                target,
                "bun disabled in config but the rig-managed block is still in ~/.bunfig.toml",
            )
        )


def check_disabled_global_excludes(action: Action, report: DriftReport) -> None:
    """Flag a still-installed managed block when the config disables the ``gitignore`` category.

    apply never deletes; so a machine that previously provisioned the global-excludes block keeps
    it in ``core.excludesfile`` even after the config turns the category off. With the action gone
    from the plan, ``_check_global_excludes`` never runs — so without this scan the leftover block
    would report as "in sync". Resolve the target the SAME way apply does and report a present begin
    marker as disk→config drift (mirrors :func:`check_disabled_dispatcher`). Marker detection reuses
    :func:`_find_marker_lines` (the same offset-based scanner :func:`resolve_global_excludes` uses)
    read with newline translation off, so detection never diverges from apply.
    """
    target, _needs_set, _set_value = _resolve_excludes_target(action)
    if not target.is_file():
        return
    try:
        with target.open(encoding="utf-8", newline="") as fh:
            content = fh.read()
    except OSError:
        return
    if _find_marker_lines(content, GITIGNORE_BEGIN_MARKER):
        report.items.append(
            DriftItem("extra", "gitignore", "block", target,
                      "gitignore disabled in config but the rig-managed block is still in the global excludes file")
        )
