"""``provision_tools`` handler: brew/apt packages, tool checkouts and per-tool install scripts."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from ..plan import Action
from . import shared
from .result import ActionResult


# How long a single tool's install.sh may run before we give up (clone + dep install can be slow,
# but a hung script must never wedge `rig apply`). Default 300s; overridable via the env var.
_TOOL_INSTALL_TIMEOUT_DEFAULT_S = 300


def _tool_install_timeout_s() -> int:
    """The per-tool install.sh timeout, read at CALL time with a safe fallback.

    Read here, not at module top: a non-numeric ``RIG_TOOL_INSTALL_TIMEOUT_S`` must NOT crash the
    import of ``runner`` (which would wedge the WHOLE CLI, not just tools). A junk/absent value
    falls back to the 300s default; a positive int wins.
    """
    raw = os.environ.get("RIG_TOOL_INSTALL_TIMEOUT_S")
    if not raw:
        return _TOOL_INSTALL_TIMEOUT_DEFAULT_S
    try:
        val = int(raw)
    except ValueError:
        return _TOOL_INSTALL_TIMEOUT_DEFAULT_S
    return val if val > 0 else _TOOL_INSTALL_TIMEOUT_DEFAULT_S


def _do_provision_tools(action: Action, on_conflict: str) -> ActionResult:
    """Provision the personal CLI ecosystem by running each declared tool's OWN install.sh.

    For each :class:`ToolSpec` carried by the action: FRESHNESS FIRST — if the tool ships
    ``scripts/deploy.sh``, rig runs it (ff-only ``git pull``) to keep the checkout current, even when
    the tool is already installed (a non-zero deploy exit is a non-fatal warning, folded into the
    message). Then the install decision: if the tool is already installed (bin resolves AND the skill
    blurb is advertised — :func:`tools.tool_status`) it is a no-op (``skipped``); else rig runs
    ``bash <repo>/install.sh``, which does the tool's own locate→symlink→install-skill dance. SAFE:
    rig never deletes a user's existing symlink; an already-working bin (even a Homebrew one) counts
    as resolved and is left alone — only an unadvertised tool is (re)installed to wire up the skill. A
    missing repo (no install.sh on disk) is an ``error`` for that tool, not a crash.

    Returns ``skipped`` when every tool was already current, ``created`` when at least one was
    installed, ``error`` if any tool's install failed or its repo was absent. ``RIG_TOOLS_DRY_RUN``
    reports what WOULD install without running any install.sh (used by the e2e suite).
    """
    from .. import tools as toolsmod

    plan = toolsmod.plan_from_action_options(action.options)
    if not plan.specs:
        return ActionResult(action, "skipped", "tools: no tools declared")

    dry = bool(os.environ.get("RIG_TOOLS_DRY_RUN"))
    installed: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    for spec in plan.specs:
        status, detail = _provision_one_tool(spec, dry)
        if status == "installed":
            installed.append(detail)
        elif status == "skipped":
            skipped.append(detail)
        else:
            errors.append(detail)

    return _tools_result(action, installed, skipped, errors)


# Prefix of the `_ensure_apt_packages` note meaning "packages still missing, rig could not elevate".
_APT_PENDING_PREFIX = "apt: WARN"


def _provision_one_tool(spec: object, dry: bool) -> tuple[str, str]:
    """Provision one tool: ensure its Homebrew deps, keep its checkout fresh, then install if needed.

    ``spec`` is a :class:`tools.ToolSpec`. The missing-repo guard runs FIRST: a repo with no
    ``install.sh`` is not a rig-provisioned tool → ``error``, and — crucially — we run NO script
    against it (a malformed/foreign ``tools.items.*.repo`` path never gets deploy.sh executed before
    the error). BREW FORMULAE run next (:func:`_ensure_brew_formulae`): a tool whose declared
    Homebrew dependency is missing AND can't be installed (``brew`` absent, or ``brew install``
    itself failed) is FATAL for this tool's provision, same as the missing-repo guard — a wrapper
    whose runtime dependency isn't installed isn't a working tool no matter how install.sh went.
    FRESHNESS runs next: if the tool ships ``scripts/deploy.sh``, rig runs it to ff-pull the
    checkout (:func:`_maybe_deploy_tool`, non-fatal, folded into the reported message) — BEFORE the
    already-installed short-circuit, which is exactly what would otherwise hide a stale checkout.
    Then the install decision: already-installed (bin resolves + advertised) → ``skipped`` UNLESS a
    real ``brew install`` just ran for this tool (``brew_changed``), in which case it reports
    ``installed`` instead — a tool whose bin/skill were already current but whose runtime formula
    was JUST installed performed a genuine mutation and must not be folded into "unchanged" (review
    finding: `_ensure_brew_formulae` ran unconditionally before this check, so a real package
    install was previously miscounted as a no-op). Otherwise run its install.sh (or, under dry-run,
    report). Returns ``(status, detail)`` — status ∈ installed/skipped/error. A deploy failure NEVER
    changes the status: a stale-but-installed tool still reports skipped/installed per the brew
    signal above, so freshness rot surfaces as a warning, not an apply error.
    """
    from .. import tools as toolsmod

    assert isinstance(spec, toolsmod.ToolSpec)
    clone_error, clone_note = _ensure_tool_checkout(spec, dry)
    if clone_error:
        return "error", clone_error
    st = toolsmod.tool_status(spec)
    if not st.repo_present:
        if dry and clone_note:
            return "installed", _with_note(f"{spec.name} (dry-run — would run {spec.install_script})", clone_note)
        return "error", f"{spec.name}: no install.sh at {spec.install_script} (repo missing?)"
    apt_error, apt_note, apt_changed = _ensure_apt_packages(spec, dry)
    if apt_error:
        return "error", apt_error
    brew_error, brew_note, brew_changed = _ensure_brew_formulae(spec, dry)
    if brew_error:
        return "error", brew_error
    deploy_note = _maybe_deploy_tool(spec, dry)
    note = _combine_notes(clone_note, apt_note, brew_note, deploy_note)
    if st.installed:
        status = "installed" if (brew_changed or apt_changed or clone_note) else "skipped"
        return status, _with_note(f"{spec.name} already installed ({spec.managed_bin})", note)
    if dry:
        return "installed", _with_note(f"{spec.name} (dry-run — would run {spec.install_script})", note)
    return _install_and_check_tool(spec, apt_note, note)


def _last_output_line(out: str) -> str:
    stripped = out.strip()
    return stripped.splitlines()[-1] if stripped else "(no output)"


def _install_and_check_tool(spec, apt_note: str, note: str) -> tuple[str, str]:
    """Run the tool's install.sh, then its optional ``--check``; ``(status, detail)``."""
    rc, out = _run_tool_install(spec)
    pending = f" ({apt_note})" if apt_note.startswith(_APT_PENDING_PREFIX) else ""
    if rc != 0:
        return "error", f"{spec.name}: install.sh exited {rc} — {_last_output_line(out)}{pending}"
    if spec.check:
        check_rc, check_out = _run_tool_check(spec)
        if check_rc != 0:
            return "error", f"{spec.name}: install.sh --check exited {check_rc} — {_last_output_line(check_out)}{pending}"
    return "installed", _with_note(f"{spec.name} installed via {spec.install_script.name}", note)


def _maybe_deploy_tool(spec: object, dry: bool) -> str:
    """Keep a provisioned tool's checkout fresh via its own ``scripts/deploy.sh`` (opt-in, non-fatal).

    Returns ``""`` (a no-op) when the tool ships no ``scripts/deploy.sh`` or under dry-run. Otherwise
    runs the deploy hook (ff-only ``git pull``, guarded entirely by the script) and returns a short
    note to fold into the tool's reported message. A non-zero exit (offline/dirty/diverged) is
    DOWNGRADED to a warning note — never an apply abort — mirroring the offline-safe ``_git_clone``
    discipline (a failed freshness pull is 'tool stale', not 'apply broken').
    """
    from .. import tools as toolsmod

    assert isinstance(spec, toolsmod.ToolSpec)
    if dry or not spec.deploy_script.exists():
        return ""
    rc, out = _run_tool_deploy(spec)
    if rc == 0:
        return "deploy.sh: ok"
    tail = out.strip().splitlines()[-1] if out.strip() else "(no output)"
    return f"deploy.sh WARN (rc={rc}): {tail}"


# One `brew list --formula`/`brew install` invocation's budget. A DEDICATED constant (not shared
# with `_tool_install_timeout_s()`, review finding: an earlier version claimed to share it but
# `_brew_installed_snapshot`'s probe hardcoded 30s regardless of `RIG_TOOL_INSTALL_TIMEOUT_S`) — a
# probe is a cheap read, not a clone+build, so it gets its own short, honest budget; only the
# actual `brew install` mutation shares the tool-install timeout (it can be genuinely slow: cold
# cache, source builds).
_BREW_LIST_TIMEOUT_S = 30


def _brew_installed_snapshot() -> tuple[frozenset[str], str | None]:
    """ONE `brew list --formula` call enumerating every currently installed formula.

    Returns ``(installed_formula_names, brew_path)``. Shared by every tool's every declared
    formula in a single apply/status run (review finding: a PER-FORMULA `brew list --formula
    <name>` probe made `rig status` shell out to Homebrew once per declared formula — on the
    default-off `tools:` carrier's own steady-state no-op path — turning a "pure on-disk reads"
    drift check into an O(formulae) subprocess fan-out with its own 30s timeout EACH; a single
    `brew list --formula` (no name) lists every installed formula in one call, and membership is a
    Python set lookup).

    ``brew_path`` is ``None`` when ``brew`` itself isn't on PATH — callers use that (not an empty
    ``installed_formula_names``) to give the actionable "install Homebrew first" message rather
    than mistaking "brew missing" for "formula missing" (review finding: the drift message used to
    unconditionally say "apply runs `brew install <formula>`" even when apply could not possibly
    run it). When ``brew`` IS on PATH but the list itself times out or errors, this fails toward
    "nothing confirmed installed" (empty set, ``brew_path`` still set) — the same conservative
    default the old per-formula probe used: a real formula then either installs cleanly on retry or
    the actual ``brew install`` call surfaces the concrete error. Never raises.
    """
    brew = shutil.which("brew")
    if brew is None:
        return frozenset(), None
    try:
        proc = subprocess.run(
            [brew, "list", "--formula"],
            capture_output=True,
            text=True,
            timeout=_BREW_LIST_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        return frozenset(), brew
    if proc.returncode != 0:
        return frozenset(), brew
    return frozenset(line.strip() for line in (proc.stdout or "").splitlines() if line.strip()), brew


def _ensure_brew_formulae(spec: object, dry: bool) -> tuple[str, str, bool]:
    """Ensure every Homebrew formula ``spec`` declares (``brew_formulae``) is installed.

    Returns ``(error, note, changed)``. ``error`` is a short, actionable message when the tool
    CANNOT be considered provisioned without a missing formula — ``brew`` itself absent from PATH,
    or a ``brew install`` that itself failed — the caller (:func:`_provision_one_tool`)
    short-circuits on it exactly like the missing-repo guard: a wrapper whose runtime OS dependency
    isn't installed isn't a working tool no matter how ``install.sh`` went. ``note`` folds a short,
    non-fatal status into the caller's detail the same way :func:`_maybe_deploy_tool`'s note does.
    ``changed`` is True ONLY when a REAL ``brew install`` ran and succeeded — the caller uses it to
    report the tool's overall status as ``installed`` even when the tool's own bin/skill were
    already current, so a brew-only mutation is never miscounted as a no-op (review finding). A
    tool that declares no formulae is untouched: all three return empty/False immediately.

    Read-only under ``RIG_TOOLS_DRY_RUN`` — the presence probe (:func:`_brew_installed_snapshot`)
    always runs (it never mutates), but a missing formula is reported via ``note`` only ("would
    install"), never treated as fatal and never actually installed, matching the dry-run
    no-mutation contract :func:`_maybe_deploy_tool` and the install decision itself already honor.
    """
    from .. import tools as toolsmod

    assert isinstance(spec, toolsmod.ToolSpec)
    if not spec.brew_formulae:
        return "", "", False
    installed, brew = _brew_installed_snapshot()
    missing = [f for f in spec.brew_formulae if f not in installed]
    if not missing:
        return "", f"brew: {', '.join(spec.brew_formulae)} already installed", False
    if dry:
        return "", f"brew: would install {', '.join(missing)} (dry-run)", False
    if brew is None:
        return (
            f"{spec.name}: brew is not on PATH — install Homebrew, then run "
            f"`brew install {' '.join(missing)}` ({spec.name} depends on it at runtime)",
            "", False,
        )
    rc, out = _run_brew_install(brew, missing)
    if rc != 0:
        tail = out.strip().splitlines()[-1] if out.strip() else "(no output)"
        return f"{spec.name}: brew install {' '.join(missing)} exited {rc} — {tail}", "", False
    return "", f"brew: installed {', '.join(missing)}", True


def _run_brew_install(brew: str, formulae: list[str]) -> tuple[int, str]:
    """Run ``brew install <formulae...>`` for every MISSING formula in ONE call (batched: shared
    dependency resolution, one Homebrew startup, instead of one invocation per formula — review
    finding). Returns ``(returncode, combined_output)``, never raises — mirrors
    :func:`_run_tool_bash_script`'s never-raises contract so a hung/broken brew can't wedge or
    crash apply.
    """
    timeout_s = _tool_install_timeout_s()
    joined = " ".join(formulae)
    try:
        proc = subprocess.run(
            [brew, "install", *formulae],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 1, f"brew install {joined} timed out after {timeout_s}s"
    except OSError as exc:
        return 1, f"could not run brew install {joined}: {exc}"


def _ensure_apt_packages(spec: object, dry: bool) -> tuple[str, str, bool]:
    """Ensure every apt package ``spec`` declares (``apt_packages``) is installed — apt machines only.

    Returns ``(error, note, changed)`` exactly like :func:`_ensure_brew_formulae`. On a machine
    without ``apt-get`` (macOS, a brew-only box) the list is SKIPPED with a note, never an error:
    one profile carries both ``brew:`` and ``apt:`` and each OS honors its own. A missing package
    that rig cannot install (no root, no non-interactive sudo) is NOT an error: the note carries the
    exact command to run by hand and the tool's own install.sh still runs (user-level failover).
    Read-only under ``RIG_TOOLS_DRY_RUN`` (the probe runs, nothing is installed).
    """
    from .. import apt_packages
    from .. import tools as toolsmod

    assert isinstance(spec, toolsmod.ToolSpec)
    if not spec.apt_packages:
        return "", "", False
    if not apt_packages.is_apt_system():
        return "", "apt: skipped (no apt-get on this machine)", False
    installed = apt_packages.installed_snapshot()
    missing = apt_packages.missing(spec.apt_packages, installed)
    if not missing:
        return "", f"apt: {', '.join(spec.apt_packages)} already installed", False
    if dry:
        return "", f"apt: would install {', '.join(missing)} (dry-run)", False
    argv = apt_packages.install_command(missing)
    if argv is None:
        # No root and no non-interactive sudo: NOT fatal. The tool's own install.sh still runs
        # (it installs user-level and reports what it cannot do) and the exact remainder is
        # reported for the user to run by hand; `rig status` keeps flagging it until it is done.
        return "", f"{_APT_PENDING_PREFIX} run `{apt_packages.manual_command(missing)}` (no root, no sudo -n)", False
    rc, out = _run_apt_install(argv)
    if rc != 0:
        tail = out.strip().splitlines()[-1] if out.strip() else "(no output)"
        return f"{spec.name}: apt-get install {' '.join(missing)} exited {rc} — {tail}", "", False
    return "", f"apt: installed {', '.join(missing)}", True


def _run_apt_install(argv: list[str]) -> tuple[int, str]:
    """Run one ``apt-get install`` argv non-interactively under the tool-install timeout; never raises."""
    timeout_s = _tool_install_timeout_s()
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s, env=env)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 1, f"apt-get install timed out after {timeout_s}s"
    except OSError as exc:
        return 1, f"could not run apt-get install: {exc}"


def _ensure_tool_checkout(spec: object, dry: bool) -> tuple[str, str]:
    """Clone ``spec.clone_url`` into ``spec.repo`` when the checkout is absent. Returns ``(error, note)``.

    Only acts when ``repo`` does not exist at all and a clone source is known; an existing
    directory (even a broken one) is never touched — the caller's missing-install.sh guard reports
    it. Dry-run only notes what would be cloned.
    """
    from .. import tools as toolsmod

    assert isinstance(spec, toolsmod.ToolSpec)
    if spec.repo.exists() or not spec.clone_url:
        return "", ""
    if dry:
        return "", f"would clone {spec.clone_url} → {spec.repo} (dry-run)"
    spec.repo.parent.mkdir(parents=True, exist_ok=True)
    rc = shared._git_clone(spec.clone_url, spec.repo)
    if rc != 0:
        return (
            f"{spec.name}: could not clone {spec.clone_url} to {spec.repo} (git exit {rc}) — check the "
            f"network / git, or clone it by hand",
            "",
        )
    return "", f"cloned {spec.clone_url}"


def _combine_notes(*parts: str) -> str:
    """Join non-empty notes with '; ' for a single bracketed suffix via :func:`_with_note`."""
    return "; ".join(p for p in parts if p)


def _with_note(detail: str, note: str) -> str:
    """Append a bracketed ``note`` to ``detail`` (a no-op when ``note`` is empty)."""
    return f"{detail} [{note}]" if note else detail


def _run_tool_install(spec: object) -> tuple[int, str]:
    """Run ``bash <repo>/install.sh`` for one tool. Returns ``(returncode, combined_output)``.

    The tool's install.sh is self-locating (it sees its own BASH_SOURCE → uses the local clone) and
    self-idempotent (re-symlink, re-run install-skill). We just invoke it with a timeout so a hung
    script can't wedge apply; the bin dir + skill dir it writes are HOME-anchored by the script.
    """
    from .. import tools as toolsmod

    assert isinstance(spec, toolsmod.ToolSpec)
    return _run_tool_bash_script(spec.install_script, spec.repo, "install.sh")


def _run_tool_deploy(spec: object) -> tuple[int, str]:
    """Run ``bash <repo>/scripts/deploy.sh`` to keep the tool's checkout fresh (ff-only ``git pull``).

    Mirrors :func:`_run_tool_install` — same cwd, capture, and ``RIG_TOOL_INSTALL_TIMEOUT_S`` budget
    (a network fetch needs the headroom). The deploy script owns all its own safety (refuses a
    dirty/detached/diverged tree, ff-only); rig just invokes it. Returns ``(returncode,
    combined_output)`` and never raises — the caller downgrades a non-zero rc to a warning.
    """
    from .. import tools as toolsmod

    assert isinstance(spec, toolsmod.ToolSpec)
    return _run_tool_bash_script(spec.deploy_script, spec.repo, "deploy.sh")


def _run_tool_check(spec: object) -> tuple[int, str]:
    """Run ``bash <repo>/install.sh --check`` — the tool's own verdict that its install is healthy."""
    from .. import tools as toolsmod

    assert isinstance(spec, toolsmod.ToolSpec)
    return _run_tool_bash_script(spec.install_script, spec.repo, "install.sh --check", "--check")


def _run_tool_bash_script(script: Path, cwd: Path, label: str, *args: str) -> tuple[int, str]:
    """Run ``bash <script> [args…]`` in ``cwd`` under the tool-install timeout. Returns ``(rc, combined)``.

    The shared core of :func:`_run_tool_install` (``install.sh``), :func:`_run_tool_check`
    (``install.sh --check``) and :func:`_run_tool_deploy` (``scripts/deploy.sh``): identical
    invocation (bash + captured output + the ``RIG_TOOL_INSTALL_TIMEOUT_S`` budget). Never raises —
    a timeout or spawn failure is reported as a non-zero rc with a ``label``-tagged message, so a
    hung/broken script can't wedge or crash apply.
    """
    timeout_s = _tool_install_timeout_s()
    try:
        proc = subprocess.run(
            ["bash", str(script), *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 1, f"{label} timed out after {timeout_s}s"
    except OSError as exc:
        return 1, f"could not run {label}: {exc}"


def _tools_result(
    action: Action, installed: list[str], skipped: list[str], errors: list[str]
) -> ActionResult:
    """Fold per-tool outcomes into one ActionResult (error if any failed, else created/skipped)."""
    parts: list[str] = []
    if installed:
        parts.append(f"installed: {', '.join(installed)}")
    if skipped:
        parts.append(f"already current: {', '.join(skipped)}")
    if errors:
        parts.append(f"FAILED: {'; '.join(errors)}")
    detail = "tools: " + ("; ".join(parts) if parts else "nothing to do")
    if errors:
        return ActionResult(action, "error", detail)
    if installed:
        return ActionResult(action, "created", detail)
    return ActionResult(action, "skipped", detail)
