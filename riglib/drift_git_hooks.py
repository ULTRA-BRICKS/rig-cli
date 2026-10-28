"""Drift checks for CI workflows, the git-hook dispatcher / composers, and the lint-check hook + workflow."""

from __future__ import annotations

import os
from pathlib import Path

from .actions.git_config import _git_global
from .actions.git_hooks_install import _ci_companion_files, resolve_ci_workflow
from .actions.linter_config import resolve_linter_config
from .plan import Action
from .provenance import KIND_UNVERIFIED
from .review_gate import SOURCE_ORIGIN, composer_sources, unshipped_composers
from .drift_model import DriftItem, DriftReport, KnownItem


def _check_ship_slot(action: Action, report: DriftReport) -> None:
    ship = action.target / "ship"
    if not ship.exists():
        report.items.append(
            DriftItem("missing", "ci", "ship", ship, "ship not installed")
        )
        return
    src_ship = action.source / "ship.sh"
    from .ship_launcher import ship_launcher_text
    if src_ship.is_file() and ship.read_text(encoding="utf-8") != ship_launcher_text(action.source):
        report.items.append(
            DriftItem("modified", "ci", "ship", ship, "installed ship differs from source")
        )


def _check_ci_companions(action: Action, slot: str, src_wf: Path, report: DriftReport) -> None:
    # vendored companion scripts — a deleted/edited one breaks the gate that apply would
    # recreate, so it is config→disk drift too. Companions install at their required paths
    # relative to the checkout root (passed explicitly, independent of ci.target).
    repo_root = Path(action.options.get("repo_root") or action.target.parent.parent)
    for comp, rel in _ci_companion_files(action.source, src_wf):
        dst = repo_root / rel
        if not dst.exists():
            report.items.append(
                DriftItem("missing", "ci", f"{slot}:{comp.name}", dst, "CI companion script not installed")
            )
        elif dst.read_text(encoding="utf-8") != comp.read_text(encoding="utf-8"):
            report.items.append(
                DriftItem("modified", "ci", f"{slot}:{comp.name}", dst, "CI companion differs from source")
            )


def _check_ci(action: Action, report: DriftReport) -> None:
    if action.options.get("slot") == "ship":
        _check_ship_slot(action, report)
        return
    wf = action.target / f"{action.item}.yml"
    if not wf.is_file():
        # absent, or a stale directory/non-file where the workflow should be — both are
        # drift apply would resolve (file-vs-dir is a recoverable conflict on apply).
        report.items.append(
            DriftItem("missing", "ci", action.item, wf,
                      "workflow not written" if not wf.exists() else "target is not a regular file")
        )
        return
    # content comparison: a workflow edited in place (e.g. a job disabled) is drift even
    # though the file still exists — apply would replace it.
    slot = action.options.get("slot", action.item)
    src_wf = resolve_ci_workflow(action.source, slot, action.options.get("variant"))
    if src_wf is None or not src_wf.is_file():
        return
    if wf.read_text(encoding="utf-8") != src_wf.read_text(encoding="utf-8"):
        report.items.append(
            DriftItem("modified", "ci", action.item, wf, "on disk differs from source workflow")
        )
    _check_ci_companions(action, slot, src_wf, report)


def check_disabled_dispatcher(repo_root: Path, report: DriftReport) -> None:
    """Flag a still-installed global dispatcher when the config disables it.

    apply never deletes; so a repo that previously installed the dispatcher keeps git's
    global ``core.hooksPath`` pointing at the composer dir even after the config turns it
    off. Detect that (the global core.hooksPath dir contains a rig composer that references
    ``run-global-hooks``) and report it as disk→config drift.
    """
    current = _git_global("core.hooksPath")
    if not current:
        return
    pre_commit = Path(current) / "pre-commit"
    if pre_commit.is_file():
        try:
            body = pre_commit.read_text(encoding="utf-8")
        except OSError:
            return
        if "run-global-hooks" in body:
            report.items.append(
                DriftItem(
                    "extra", "git_hooks", "dispatcher", Path(current),
                    "dispatcher disabled in config but still wired as global core.hooksPath",
                )
            )


# Strings only the managers' GENERATED shims carry (rig's own composers mention lefthook/husky
# in comments, so bare names are not a signature).
# Structural markers each manager writes into its global-dir shim. husky: `husky.sh` / `.husky/_`
# are the v4-v8 shims (v9 sets a repo-local core.hooksPath and never writes a global shim) and
# `HUSKY=0` is their skip switch — keep them even though a v9-only machine never shows them.
_HOOK_MANAGER_SIGNATURES = (
    ("lefthook", ("LEFTHOOK_VERBOSE", "call_lefthook")),
    ("husky", ("husky.sh", "HUSKY=0", ".husky/_")),
    ("pre-commit", ("pre-commit.com", "INSTALL_PYTHON=")),
    ("simple-git-hooks", ("SKIP_SIMPLE_GIT_HOOKS",)),
)


def hook_manager_shim(text: str) -> str | None:
    """The hook manager whose generated shim ``text`` is (lefthook, husky, pre-commit, …), or None.

    A composer is rig's own script; a shim is what `lefthook install` & co. write into whatever
    core.hooksPath resolves to — the GLOBAL one when rig's dispatcher is wired (a postinstall
    `lefthook install` replaced the pre-commit composer this way on 2026-09-26).
    """
    head = text[:2048]
    if "Composing global" in head:  # rig's own composer, just an older/edited copy
        return None
    for name, needles in _HOOK_MANAGER_SIGNATURES:
        if any(n in head for n in needles):
            return name
    return None


def _check_composers(src_hooks: Path, composer: Path, report: DriftReport) -> None:
    """Compare every shipped composer against what apply would install.

    Expected content comes from ``review_gate.composer_sources`` — the SAME helper apply
    uses — so ``review-gate`` is compared against the installed review-cli's ``--print-gate``
    output when available, else the agent-tools copy: a review-gate that still equals a
    stale agent-tools copy is reported as drift instead of "clean" (rig-cli#382). The detail
    names the source compared against and any fallback reason. A transient review-cli
    failure leaves the expected content unknown — that file is not compared but is listed
    as an informational ``unverified`` known item (never silently clean). Composers on
    disk the source no longer ships are ``extra`` (git still runs them; never deleted).
    """
    sources = composer_sources(src_hooks)
    for src in sources:
        dst = composer / src.name
        if not dst.exists():
            report.items.append(DriftItem("missing", "git_hooks", src.name, dst, "composer hook not installed"))
        elif src.expected is None:
            report.known.append(KnownItem("git_hooks", src.name, dst, KIND_UNVERIFIED, f"{src.name}: {src.note}"))
        elif (installed := dst.read_text(encoding="utf-8", errors="replace")) != src.expected:
            manager = hook_manager_shim(installed)
            if manager:
                detail = (
                    f"REPLACED by a {manager} shim — `{manager} install` wrote into the GLOBAL "
                    f"core.hooksPath, so rig's {src.name} composer (secret scan, review gate) no longer "
                    "runs in any repo; `rig apply` restores it (the old one may be in "
                    f"{src.name}.old). Never run a hook manager's install with a global core.hooksPath."
                )
            else:
                detail = f"composer hook differs from {src.origin}"
                if src.note and src.origin == SOURCE_ORIGIN:
                    detail += f" ({src.note})"
            report.items.append(DriftItem("modified", "git_hooks", src.name, dst, detail))
    for leftover in unshipped_composers(composer, sources):
        report.items.append(
            DriftItem(
                "extra", "git_hooks", leftover.name, leftover,
                "composer hook on disk but not shipped by source (git still runs it)",
            )
        )


def _check_runner(runner: Path, src_runner: Path, report: DriftReport) -> None:
    if not runner.exists():
        report.items.append(
            DriftItem("missing", "git_hooks", "dispatcher", runner, "runner not installed")
        )
    elif src_runner.is_file() and runner.read_text(encoding="utf-8") != src_runner.read_text(encoding="utf-8"):
        report.items.append(
            DriftItem("modified", "git_hooks", "run-global-hooks", runner, "runner differs from source")
        )


def _check_hooks_path(composer: Path, report: DriftReport) -> None:
    current = _git_global("core.hooksPath")
    if current != str(composer):
        report.items.append(
            DriftItem(
                "modified", "git_hooks", "dispatcher", composer,
                f"global core.hooksPath is {current or 'unset'}, expected {composer}",
            )
        )


def _check_fragment(frag: Path, dst: Path, disabled: set[str], report: DriftReport) -> None:
    if any(name in frag.name for name in disabled):
        # explicitly disabled — but install doesn't delete; a leftover copy
        # still runs in every repo, so surface it as disk→config drift.
        if dst.exists():
            report.items.append(
                DriftItem("extra", "git_hooks", frag.name, dst,
                          "fragment disabled in config but still installed (will still run)")
            )
        return
    if not dst.exists():
        report.items.append(
            DriftItem("missing", "git_hooks", frag.name, dst, "dispatcher fragment not installed")
        )
    elif dst.read_text(encoding="utf-8") != frag.read_text(encoding="utf-8"):
        report.items.append(
            DriftItem("modified", "git_hooks", frag.name, dst, "dispatcher fragment differs from source")
        )


def _disabled_fragment_names(action: Action) -> set[str]:
    return {
        name
        for name, spec in (action.options.get("fragments", {}) or {}).items()
        if isinstance(spec, dict) and spec.get("enabled") is False
    }


def _check_fragments(action: Action, report: DriftReport) -> None:
    # each enabled, shipped fragment must be present + match the source on disk — a deleted
    # or edited fragment is config→disk drift that apply would recreate.
    src_frag = action.source / "global-hooks.d"
    disabled = _disabled_fragment_names(action)
    if not src_frag.is_dir():
        return
    for event_dir in sorted(p for p in src_frag.iterdir() if p.is_dir()):
        for frag in sorted(event_dir.iterdir()):
            if frag.is_file():
                _check_fragment(frag, action.target / event_dir.name / frag.name, disabled, report)


def _check_dispatcher(action: Action, report: DriftReport) -> None:
    runner = Path(action.options["runner"])
    _check_runner(runner, action.source / "run-global-hooks", report)
    # the composer dir is the real core.hooksPath target — compare EVERY shipped composer
    # (pre-commit/commit-msg/pre-push/review-gate): a missing or edited one means that hook
    # event stops invoking the dispatcher even though apply would recreate it.
    composer = runner.parent / "hooks"
    src_hooks = action.source / "hooks"
    if src_hooks.is_dir():
        _check_composers(src_hooks, composer, report)
    # if the config wired core.hooksPath, verify git still points at the composer dir —
    # files can exist while git no longer invokes them (someone re-set core.hooksPath).
    if action.options.get("set_global_hooks_path"):
        _check_hooks_path(composer, report)
    _check_fragments(action, report)


def _check_disabled_lint_check_hook(loaded, disp_cfg: dict, report: DriftReport) -> None:
    """A ``30-lint-check`` fragment still installed after the MACHINE config switched it off.

    apply never deletes a dropped fragment and the dispatcher runs every executable file in the dir,
    so the stale fragment keeps blocking commits — same ``extra`` shape as a disabled catalog fragment.
    Only the global layer (or the fragment switch) counts: a repo's own ``lint_checks.enabled: false``
    leaves the machine-wide hook correctly installed (it exits 0 "disabled" there), so it is not drift.
    """
    from . import lint_checks_artifacts as arts

    fragment_off = arts.hook_fragment_disabled(disp_cfg)
    if not (fragment_off or not arts.lint_checks_intent(loaded).machine_enabled):
        return
    dst = arts.hook_fragment_path(arts.resolve_dispatcher_dir(disp_cfg, loaded.repo_root))
    if dst.exists():
        why = "fragment disabled in config" if fragment_off else "lint_checks.enabled is false (global config)"
        report.items.append(DriftItem(
            "extra", "lint_checks_machine", arts.HOOK_NAME, dst, f"{why} but still installed (will still run)",
        ))


def _check_lint_check_analyzer(action: Action, report: DriftReport) -> None:
    """Flag a missing / byte-mismatched / wrong-version / unsupported-platform cccc analyzer.

    One verdict, :func:`riglib.cccc_install.inspect`, shared with ``rig lint check`` itself, so
    status says exactly what the gate will do (fail closed with exit 127).
    """
    from . import cccc_install

    state = cccc_install.inspect()
    if state.state == "ok":
        return
    direction = "missing" if state.state == "missing" else "modified"
    report.items.append(DriftItem(direction, "lint_checks_machine", "cccc", state.path, state.detail))


def _check_lint_check_hook(action: Action, report: DriftReport) -> None:
    """Flag a missing, edited or non-executable pre-commit fragment (git ignores a non-exec hook)."""
    from .lint_checks_artifacts import render_hook_fragment

    path = action.target
    label = "pre-commit-hook"
    if not path.is_file():
        report.items.append(DriftItem("missing", "lint_checks_machine", label, path, "lint-check pre-commit fragment not installed"))
        return
    try:
        current = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        report.items.append(DriftItem("modified", "lint_checks_machine", label, path, f"cannot read fragment: {exc}"))
        return
    if current != render_hook_fragment(str(action.options["rig_bin"])):
        report.items.append(DriftItem("modified", "lint_checks_machine", label, path, "lint-check fragment differs from the rig-rendered one"))
    elif not os.access(path, os.X_OK):
        report.items.append(DriftItem("modified", "lint_checks_machine", label, path, "lint-check fragment is not executable (the dispatcher skips it)"))


def _check_lint_check_workflow(action: Action, report: DriftReport) -> None:
    """Flag a missing / edited / unsafe-path ``rig-lint-check.yml`` (same classifier apply uses)."""
    from .lint_checks_artifacts import render_workflow

    rel_path = str(action.options["rel_path"])
    content = render_workflow(str(action.options["rig_source"]), bool(action.options["with_analyzer"]))
    r = resolve_linter_config(action.target, rel_path, content)
    if r.state == "ok":
        return
    if r.state == "create":
        report.items.append(DriftItem("missing", "lint_checks", "ci-workflow", r.target_path, f"{rel_path} not written (apply writes it)"))
    else:
        detail = r.detail if r.state == "io_error" else f"{rel_path} differs from the rig-rendered workflow"
        report.items.append(DriftItem("modified", "lint_checks", "ci-workflow", r.target_path, detail))
