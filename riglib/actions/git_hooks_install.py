"""Global git-hook dispatcher, composer, fragment and CI workflow install handlers."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import fsutil
from ..plan import Action
from ..review_gate import ComposerSource, composer_sources, gate_note, unshipped_composers
from . import git_config
from .result import ActionResult
from .shared import _chmod_x_if_changed


def _do_install_dispatcher(action: Action, on_conflict: str) -> ActionResult:
    """Install the global-hook dispatcher and wire it as global ``core.hooksPath``.

    The dispatcher has three on-disk pieces (agent-tools git-hooks/global-dispatcher):
      - ``run-global-hooks`` — the runner, placed beside the composer dir so the
        composers' ``../run-global-hooks`` reference resolves.
      - ``hooks/`` — the per-event COMPOSERS (pre-commit/commit-msg/pre-push + review-gate).
        **core.hooksPath must point HERE**, not at the runner's parent: git looks for an
        executable named after the event in core.hooksPath, and those live in hooks/.
        ``review-gate`` is sourced from the installed review-cli (``review
        install-commit-hook --print-gate``) with the agent-tools copy as fallback.
      - ``global-hooks.d/<event>/`` — the drop-in fragments the runner enumerates.

    Idempotent: every copy skips-if-identical; the ``core.hooksPath`` set checks the
    current value first and records the prior value for restore.
    """
    src = action.source  # the global-dispatcher dir in agent-tools
    roll = _DispatcherRollup()
    # The runner sits at the configured runner path; the composer dir (the real
    # core.hooksPath target) is its sibling ``hooks/`` directory.
    runner_target = Path(action.options["runner"])
    composer_target = runner_target.parent / "hooks"

    _install_runner_step(src, runner_target, on_conflict, roll)
    _install_composers_step(src, composer_target, on_conflict, roll)
    _install_fragments_step(src, action, on_conflict, roll)
    _install_retrofit_step(src, action, on_conflict, roll)
    if action.options.get("set_global_hooks_path"):
        _set_hooks_path_step(composer_target, roll)
    return ActionResult(action, roll.overall(), "; ".join(roll.notes) or "dispatcher installed", roll.backup)


class _DispatcherRollup:
    """Notes and sub-outcome statuses accumulated across the dispatcher's install steps."""

    def __init__(self) -> None:
        self.notes: list[str] = []
        self.statuses: list[str] = []
        self.backup: Path | None = None

    def record(self, label: str, out: fsutil.WriteOutcome) -> None:
        self.notes.append(f"{label} {out.status}")
        self.statuses.append(out.status)
        self.backup = self.backup or out.backup

    def overall(self) -> str:
        # roll up: error wins, else any change wins, else skipped (idempotent no-op)
        if "error" in self.statuses:
            return "error"
        if any(s in ("created", "updated", "backed_up") for s in self.statuses):
            return "backed_up" if self.backup else "created"
        return "skipped"


def _install_runner_step(src: Path, runner_target: Path, on_conflict: str, roll: _DispatcherRollup) -> None:
    # 1. the runner script
    src_runner = src / "run-global-hooks"
    if src_runner.is_file():
        out = fsutil.copy_file(src_runner, runner_target, on_conflict)
        _chmod_x_if_changed(runner_target, out)
        roll.record("runner", out)


def _install_composers_step(src: Path, composer_target: Path, on_conflict: str, roll: _DispatcherRollup) -> None:
    # 2. the composers (core.hooksPath target) — per file; review-gate comes from the
    # installed review-cli when it can print it (see _install_composers).
    src_hooks = src / "hooks"
    if src_hooks.is_dir():
        out, composer_notes = _install_composers(src_hooks, composer_target, on_conflict)
        roll.notes.append(f"composers {out.status}")
        roll.notes.extend(composer_notes)
        roll.statuses.append(out.status)
        roll.backup = roll.backup or out.backup


def _install_fragments_step(src: Path, action: Action, on_conflict: str, roll: _DispatcherRollup) -> None:
    # 3. fragments dir (global-hooks.d) — honor the per-fragment enable config.
    src_frag = src / "global-hooks.d"
    if not src_frag.is_dir():
        return
    out = _install_fragments(src_frag, action.target, action.options.get("fragments", {}), on_conflict)
    roll.record("fragments", out)
    # the runner enumerates ${GLOBAL_HOOKS_DIR:-~/.config/git/global-hooks.d}. If the
    # configured fragments dir is anything else, the runner won't see it unless that
    # env is exported — warn rather than silently install fragments that never run.
    default_dir = Path(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    ) / "git" / "global-hooks.d"
    if action.target.resolve() != default_dir.resolve():
        roll.notes.append(
            f"WARNING: fragments dir {action.target} is not the runner default "
            f"{default_dir}; export GLOBAL_HOOKS_DIR={action.target} or the runner "
            "will not enumerate them"
        )


def _install_retrofit_step(src: Path, action: Action, on_conflict: str, roll: _DispatcherRollup) -> None:
    # 4. retrofit script onto PATH-ish location (~/.local/bin)
    if not action.options.get("install_local_retrofit_script"):
        return
    src_retro = src / "install-local-hooks.sh"
    if src_retro.is_file():
        bin_dir = Path(os.path.expanduser("~/.local/bin"))
        retro_target = bin_dir / "install-local-hooks.sh"
        out = fsutil.copy_file(src_retro, retro_target, on_conflict)
        _chmod_x_if_changed(retro_target, out)
        roll.record("retrofit-script", out)


def _set_hooks_path_step(composer_target: Path, roll: _DispatcherRollup) -> None:
    # 5. set global core.hooksPath → the composer dir (record prior value)
    hooks_path = str(composer_target)
    current = git_config._git_global("core.hooksPath")
    if current == hooks_path:
        roll.notes.append("core.hooksPath already set")
        roll.statuses.append("skipped")
        return
    if current:
        roll.notes.append(f"prior core.hooksPath={current} (restore with: git config --global core.hooksPath {current})")
    rc = git_config._set_git_global("core.hooksPath", hooks_path)
    roll.notes.append(f"core.hooksPath → {hooks_path}" if rc == 0 else "core.hooksPath set FAILED")
    roll.statuses.append("created" if rc == 0 else "error")


_COMPOSER_STATUS_RANK = {"skipped": 0, "created": 1, "updated": 1, "backed_up": 2}

# fsutil says "updated" for an overwrite; the runner-level vocabulary folds it into "created".
_COMPOSER_STATUS_ALIASES = {"updated": "created"}


def _prepare_composer_dir(target: Path, on_conflict: str) -> fsutil.WriteOutcome | None:
    """Make ``target`` a directory, honouring ``on_conflict`` when a stale FILE sits there.

    Returns the outcome of clearing a stale file (``skipped`` on a conflict-skip means the
    caller must not install anything), or None when the directory is ready.
    """
    if target.exists() and not target.is_dir():
        if on_conflict == "skip":
            return fsutil.WriteOutcome("skipped", f"exists (not a directory), on_conflict=skip: {target}")
        if on_conflict == "backup":
            bak = fsutil.backup_path(target)
            shutil.move(str(target), str(bak))
            target.mkdir(parents=True)
            return fsutil.WriteOutcome("backed_up", f"backed up prior → {bak}", backup=bak)
        target.unlink()
    target.mkdir(parents=True, exist_ok=True)
    return None


def _install_one_composer(src: ComposerSource, dst: Path, on_conflict: str) -> fsutil.WriteOutcome:
    """Write one composer; a transient review-cli failure leaves an installed gate FILE alone
    (a directory or nothing at that path is not a hook — the fallback goes in per on_conflict)."""
    if src.expected is None:
        if dst.is_file():
            return fsutil.WriteOutcome("skipped", f"left as-is ({src.note}): {dst}")
        return fsutil.write_file(dst, src.fallback, on_conflict)
    return fsutil.write_file(dst, src.expected, on_conflict)


def _install_composers(
    src_hooks: Path, target: Path, on_conflict: str
) -> tuple[fsutil.WriteOutcome, list[str]]:
    """Install the per-event COMPOSERS (pre-commit/commit-msg/pre-push/review-gate) PER FILE.

    Expected content comes from ``review_gate.composer_sources`` (the same helper drift
    uses): ``review-gate`` from the installed review-cli when it answers, else the
    agent-tools copy; the provenance is the first of the returned notes (action detail).
    Files the source no longer ships are left in place (git still runs them) and named in a
    second note — drift reports them as ``extra``; rig never deletes them.

    Git ignores a non-executable hook, so the exec bit must converge even when the content is
    already identical (else a re-apply leaves the dispatcher silently disabled); only a
    CONFLICT-skip leaves a file untouched — ``_chmod_x_if_changed`` encodes exactly that.

    Returns the most-significant per-file outcome (``backed_up`` > ``created`` > ``skipped``)
    carrying the FIRST backup path, mirroring ``_install_fragments``.
    """
    sources = composer_sources(src_hooks)
    composer_notes = [gate_note(sources)]
    prepared = _prepare_composer_dir(target, on_conflict)
    if prepared is not None and prepared.status == "skipped":
        return prepared, composer_notes
    best = "skipped"
    first_backup: Path | None = prepared.backup if prepared is not None else None
    for src in sources:
        dst = target / src.name
        out = _install_one_composer(src, dst, on_conflict)
        _chmod_x_if_changed(dst, out)
        if _COMPOSER_STATUS_RANK.get(out.status, 0) > _COMPOSER_STATUS_RANK.get(best, 0):
            best = out.status
        if out.backup is not None and first_backup is None:
            first_backup = out.backup
    if prepared is not None:
        best = prepared.status
    detail = "per-file"
    if first_backup is not None:
        detail += f"; backed up → {first_backup}"
    extra = unshipped_composers(target, sources)
    if extra:
        composer_notes.append(
            "not shipped by source, left as-is: " + ", ".join(p.name for p in extra)
        )
    status = _COMPOSER_STATUS_ALIASES.get(best, best)
    return fsutil.WriteOutcome(status, detail, first_backup), composer_notes


def _install_fragments(
    src_frag: Path, target: Path, fragments_cfg: dict, on_conflict: str
) -> fsutil.WriteOutcome:
    """Install shipped ``global-hooks.d`` fragments PER FILE, honoring per-fragment config.

    Fragment files live at ``<event>/<NN-name>`` (e.g. ``pre-commit/10-secret-scan``). A
    config entry ``fragments.<name>.enabled: false`` skips every file whose name contains
    ``<name>``. The drop-in dir is a SHARED namespace — other tools and prior installs may
    have their own fragments there — so we merge file-by-file and never copy the whole tree
    (a whole-tree copy would back up / clobber unrelated drop-ins). Extras left in the dir
    are surfaced by ``rig status``, not reconciled here.

    Returns the most-significant per-file outcome (``backed_up`` > ``created`` > ``skipped``)
    and carries the FIRST backup path so the dispatcher report keeps a restore hint.
    """
    disabled = {
        name
        for name, spec in (fragments_cfg or {}).items()
        if isinstance(spec, dict) and spec.get("enabled") is False
    }
    target.mkdir(parents=True, exist_ok=True)
    best = "skipped"
    first_backup: Path | None = None
    rank = {"skipped": 0, "created": 1, "updated": 1, "backed_up": 2}
    for frag, dst in _enabled_fragments(src_frag, target, disabled):
        out = fsutil.copy_file(frag, dst, on_conflict)
        _chmod_x_if_changed(dst, out)
        if rank.get(out.status, 0) > rank.get(best, 0):
            best = out.status
        if out.backup is not None and first_backup is None:
            first_backup = out.backup
    suffix = f" ({len(disabled)} disabled)" if disabled else ""
    detail = f"per-file{suffix}"
    if first_backup is not None:
        detail += f"; backed up → {first_backup}"
    return fsutil.WriteOutcome("created" if best == "updated" else best, detail, first_backup)


def _enabled_fragments(src_frag: Path, target: Path, disabled: set[str]) -> list[tuple[Path, Path]]:
    """``(source, destination)`` for every shipped fragment file not disabled by config."""
    pairs: list[tuple[Path, Path]] = []
    for event_dir in sorted(p for p in src_frag.iterdir() if p.is_dir()):
        dst_event = target / event_dir.name
        for frag in sorted(event_dir.iterdir()):
            if frag.is_file() and not any(name in frag.name for name in disabled):
                pairs.append((frag, dst_event / frag.name))
    return pairs


def _install_ship_launcher(action: Action, on_conflict: str) -> ActionResult:
    ship = action.source / "ship.sh"
    if not ship.is_file():
        return ActionResult(action, "error", "ship.sh not found in catalog item")
    target = action.target / "ship"
    from ..ship_launcher import ship_launcher_text
    out = fsutil.write_file(target, ship_launcher_text(action.source), on_conflict)
    _chmod_x_if_changed(target, out)
    detail = f"ship {out.detail}"
    # NOTE: `gh_alias: true` on the ci ship item does NOT set the alias HERE. The `gh ship`
    # alias is provisioned by the single `provision_gh_ship_alias` action (the plan builder
    # emits it when this item requests it OR ship_delegator is on), so it honors on_conflict,
    # is idempotent, and is drift-tracked. A direct writer here would be a SECOND, unconditional
    # setter racing that reconciler (clobbering even under on_conflict=skip, rewriting an
    # already-correct alias every apply). The option is consumed at plan-build time.
    if action.options.get("gh_alias"):
        detail += "; gh ship alias via provision_gh_ship_alias"
    return ActionResult(action, out.status, detail, out.backup)


def _vendor_ci_companions(
    companions: list[tuple[Path, str]], repo_root: Path, out: fsutil.WriteOutcome, on_conflict: str
) -> str:
    """Copy companions; fold the first backup into ``out`` and return the rolled-up status.

    rank: backed_up > created > skipped, so a skipped workflow + a changed companion still
    reports a change (not a misleading 'skipped'/changed=0).
    """
    rank = {"skipped": 0, "created": 1, "updated": 1, "backed_up": 2}
    best_status = out.status
    for comp, rel in companions:
        comp_target = repo_root / rel
        c_out = fsutil.copy_file(comp, comp_target, on_conflict)
        if comp.suffix in _CI_COMPANION_SUFFIXES:
            _chmod_x_if_changed(comp_target, c_out)
        if c_out.backup is not None and out.backup is None:
            out.backup = c_out.backup
        if rank.get(c_out.status, 0) > rank.get(best_status, 0):
            best_status = "created" if c_out.status == "updated" else c_out.status
    return best_status


def _do_install_ci(action: Action, on_conflict: str) -> ActionResult:
    slot = action.options.get("slot", action.item)
    if slot == "ship":
        return _install_ship_launcher(action, on_conflict)

    # fail-closed on a requested variant the catalog doesn't ship — a variant is a config
    # decision, so silently installing a DIFFERENT workflow than asked is wrong.
    variant = action.options.get("variant")
    if variant and not (action.source / f"workflow-{variant}.yml").is_file():
        return ActionResult(action, "error", f"ci/{slot}: requested variant '{variant}' not found")
    workflow = resolve_ci_workflow(action.source, slot, variant)
    if workflow is None:
        return ActionResult(action, "error", f"no workflow file for ci/{slot}")
    target = action.target / f"{slot}.yml"
    out = fsutil.copy_file(workflow, target, on_conflict)

    # Vendor the companion helpers the workflow invokes, at their required install paths
    # (most ci/<slot>/, some fixed like pr-checklist → .github/scripts/). These are relative
    # to the CHECKOUT ROOT (passed explicitly), not derived from ci.target which may be
    # customized to e.g. .ci/workflows.
    repo_root = Path(action.options.get("repo_root") or action.target.parent.parent)
    companions = _ci_companion_files(action.source, workflow)
    best_status = _vendor_ci_companions(companions, repo_root, out, on_conflict)
    extra = f"; +{len(companions)} companion(s)" if companions else ""
    return ActionResult(action, best_status, f"workflow {out.detail}{extra}", out.backup)


# companion files to vendor alongside a workflow (helpers the workflow runs). New agent-tools
# gates are stdlib Python (`python3 ci/<slot>/<name>.py`, agent-tools#945); .sh/.mjs remain
# for the older slots until they are ported.
_CI_COMPANION_SUFFIXES = (".py", ".sh", ".mjs")

_CI_COMPANION_EXTRA = ("pull_request_template.md",)

# slots whose workflow imports a companion from a FIXED path (not ci/<slot>/). Maps the
# companion filename → its repo-relative install dir, per the agent-tools workflow's import.
_CI_COMPANION_FIXED_PATHS = {
    "pr-checklist": {
        "checklist-gate.mjs": ".github/scripts",
        "pull_request_template.md": ".github",
    },
}


def _is_python_test(p: Path) -> bool:
    """A test-shaped .py (pytest's ``test_*.py``/``*_test.py``, ``*.test.py``, ``conftest.py``).

    Contract with agent-tools: a slot's tests run there (``tests/``) and never ship; a gate
    script never has a test-shaped name. Vendoring one would also let the consumer repo's
    own pytest collect it.
    """
    if p.suffix != ".py":
        return False
    return p.name == "conftest.py" or p.name.startswith("test_") or p.stem.endswith(("_test", ".test"))


def _ci_companion_files(source: Path, workflow: Path) -> list[tuple[Path, str]]:
    """Companions a CI slot's workflow invokes + their repo-relative install path.

    Returns ``(source_file, repo_relative_install_path)`` pairs. Most companions live in
    ``ci/<slot>/`` (where the workflow runs ``python3 ci/<slot>/x.py`` or ``bash
    ci/<slot>/x.sh``), but some workflows import from a fixed path (pr-checklist →
    ``.github/scripts/``); those are mapped explicitly. Excludes the workflow yml, READMEs,
    test files (``*.test.mjs`` and test-shaped ``.py``, see ``_is_python_test``), and
    gitleaks config.
    """
    slot = source.name
    fixed = _CI_COMPANION_FIXED_PATHS.get(slot, {})
    out: list[tuple[Path, str]] = []
    for p in sorted(source.iterdir()):
        if not p.is_file() or p == workflow:
            continue
        if p.name.endswith(".test.mjs") or p.name.startswith("README") or _is_python_test(p):
            continue
        if p.suffix in _CI_COMPANION_SUFFIXES or p.name in _CI_COMPANION_EXTRA:
            install_dir = fixed.get(p.name, f"ci/{slot}")
            out.append((p, f"{install_dir}/{p.name}"))
    return out


def resolve_ci_workflow(source: Path, slot: str, variant: str | None) -> Path | None:
    """Resolve the source workflow file for a CI slot (shared by the runner and drift).

    Prefer the variant-specific workflow, else ``workflow.yml``, else a slot-named file
    (e.g. secret-scan ships ``secret-scan.yml``), else the single non-config ``*.yml``.
    """
    candidates: list[Path] = []
    if variant:
        candidates.append(source / f"workflow-{variant}.yml")
    candidates += [source / "workflow.yml", source / f"{slot}.yml"]
    workflow = next((c for c in candidates if c.is_file()), None)
    if workflow is None:
        ymls = [
            p
            for p in sorted(source.glob("*.yml")) + sorted(source.glob("*.yaml"))
            if "gitleaks" not in p.name  # config files, not workflows
        ]
        workflow = ymls[0] if len(ymls) == 1 else None
    return workflow
