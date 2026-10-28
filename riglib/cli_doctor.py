"""`rig doctor` — detect and (offer to) install required/optional dependencies.

Context: also reports Claude config dirs and the TUI deps.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from .cli_output import _bold, _dim, _dim_cmd, _err, _ok, _warn
from .cli_status import _handle_core_bare
from .cli_status_services import _print_dep_statuses


def _doctor_repo_root() -> Path:
    """The enclosing repo root so a ``rig doctor`` run from a SUBDIR still reads the repo's
    ``rig.yaml`` layer: the nearest ancestor carrying a ``.git`` (a directory for a primary
    checkout, a FILE for a linked worktree). No git subprocess — stdlib only, and the cwd itself
    outside any repo."""
    cwd = Path.cwd()
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".git").exists():
            return candidate
    return cwd


_CONFIG_DIR_ROLE_TEXT = {
    "default": "default (no CLAUDE_CONFIG_DIR)",
    "env": "CLAUDE_CONFIG_DIR (this shell)",
    "account": "claude-rotate account",
    "configured": "harness.settings_paths",
    "unmanaged-account": "claude-rotate account, discover_config_dirs: false",
}


def _print_config_dir_row(row) -> None:
    role = _CONFIG_DIR_ROLE_TEXT.get(row.role, row.role)
    if not row.exists:
        print(f"    {_warn('✗')} {row.settings}  {_dim('[' + role + '] — no settings.json')}")
    elif row.malformed:
        print(f"    {_err('✗')} {row.settings}  {_dim('[' + role + '] — malformed JSON')}")
    else:
        state = (
            f"{row.hook_events} hook events, {row.bridge_hooks} rig bridge hooks, "
            f"{row.allow_entries} allow entries, defaultMode={row.default_mode or '-'}"
        )
        mark = _ok("✔") if row.bridge_hooks else _warn("✗")
        print(f"    {mark} {row.settings}  {_dim('[' + role + '] ' + state)}")


def _print_claude_config_dirs() -> list:
    """Print the settings.json each claude config dir on this machine loads + its managed-key counts.

    Honors the effective ``harness.settings_paths`` / ``discover_config_dirs`` (leniently loaded —
    doctor never fails on a broken config elsewhere). Returns the gaps (a dir a live ``claude``
    could load that lacks the hook bridge ``~/.claude/settings.json`` carries) so ``cmd_doctor``
    folds them into its exit code; each gap names a fix that actually converges THAT dir.
    Prints nothing when only the default dir exists.
    """
    from .claude_config_dirs import (
        CONFIG_DIR_ENV,
        config_dir_gaps,
        doctor_config_dirs,
        resolve_settings_paths,
    )
    from .config import load_harness_fan_out

    repo_root = _doctor_repo_root()
    # the SAME resolver the plan uses: a relative settings_paths entry is anchored at the repo
    # root, so doctor from a subdirectory inspects the file apply actually wrote
    harness = resolve_settings_paths(load_harness_fan_out(repo_root), repo_root)
    rows = doctor_config_dirs(harness=harness)
    gaps = config_dir_gaps(rows)
    if len(rows) < 2 and not gaps:
        return []
    print(_bold(f"\n  ▸ claude config dirs — what an interactive `claude` loads ({CONFIG_DIR_ENV}):"))
    for row in rows:
        _print_config_dir_row(row)
    for gap in gaps:
        print(f"      {_err('✗')} {gap.what}")
        print(f"        {_ok('fix:')} {gap.fix}")
    return gaps


def _models_schedule_opted_out() -> bool:
    """True when the effective config says ``models.enabled: false`` — then an unwritable
    scheduler is irrelevant and doctor must not fail on it. An unloadable config is not an
    opt-out (doctor still reports the scheduler)."""
    from .config import ConfigError, load

    try:
        models = load(_doctor_repo_root()).data.get("models")
    except (ConfigError, OSError):
        return False
    return isinstance(models, dict) and models.get("enabled") is False


@dataclass(frozen=True)
class _DoctorFindings:
    """What the non-dependency checks found; decides the exit code after the dep list printed."""

    repo_corrupt: bool
    dead_targets: list
    config_dir_gaps: list
    sched_problem: str | None


def _print_probe_results(report) -> None:
    # probes run AFTER the dependency list prints: an opted-in probe is a network model
    # call (tens of seconds) and must not stall a silent screen.
    from .probes import any_probe_enabled, run_probes

    if any_probe_enabled():  # cheap gate — no model call unless opted in
        print(_dim("  running activation probe(s) (opted in)…"))
    report.probes = run_probes()
    for probe in report.probes:
        if probe.ok is None:
            print(f"  {_dim('⊘')} {probe.name}: {_dim(probe.detail)}")
        elif probe.ok:
            print(f"  {_ok('✔')} {probe.name}: {probe.detail}")
        else:
            print(f"  {_err('✗')} {probe.name}: {probe.detail}")


def _print_dead_targets(dead_targets: list) -> None:
    if not dead_targets:
        return
    print(_err(_bold(f"\n  ▸ missing targets ({len(dead_targets)}) — config points at files that are gone:")))
    for f in dead_targets:
        print(f"    {_err('✗')} {f.what}")
        print(f"      {_ok('fix:')} {f.fix}")


def _scheduler_problem_or_none() -> str | None:
    """A present binary is not a working schedule — rig must be able to WRITE to it
    (the crontab path silently failed from every NoNewPrivs agent shell, rig-cli#721)."""
    from .doctor import scheduler_problem

    if _models_schedule_opted_out():
        return None
    problem = scheduler_problem()
    if problem:
        print(_err(_bold("\n  ▸ scheduler unwritable — rig cannot install the daily model-freshness schedule:")))
        print(f"    {_err('✗')} {problem}")
    return problem


def _collect_findings(args: argparse.Namespace) -> _DoctorFindings:
    from .drift import scan_missing_targets

    # repo corruption: a working checkout with core.bare=true silently breaks every git op +
    # ship. This is the most severe class — surface it (and --fix it) ahead of any dep shortfall.
    repo_corrupt = _handle_core_bare(do_fix=args.fix)
    # missing-target: proactively flag a dead hook reference in the harness settings.json (the
    # rtk-hook case) so it's caught here, not at runtime as a generic harness error.
    dead_targets = scan_missing_targets()
    _print_dead_targets(dead_targets)
    # claude config dirs: which settings.json an interactive `claude` on THIS machine actually
    # loads (CLAUDE_CONFIG_DIR / claude-rotate account dirs) and whether the rig-managed hooks
    # are there — the rig-cli#368 class, invisible to `rig status` before the fan-out.
    config_dir_gaps = _print_claude_config_dirs()
    return _DoctorFindings(repo_corrupt, dead_targets, config_dir_gaps, _scheduler_problem_or_none())


def _corruption_verdict(args: argparse.Namespace) -> int:
    from . import errors

    # When --fix was already given but a repair failed, do NOT advise "re-run --fix" — it would
    # just fail again. The local-write manual command can also be ineffective if `true` comes
    # from a worktree/include scope (the very case fix_core_bare re-checks), so point at BOTH
    # the unwritable-config and the other-scope possibilities rather than a single command.
    if args.fix:
        print(_err("\n  the core.bare repair above FAILED — config may be unwritable, or `true` comes from a worktree/include scope; check it manually — git is broken there"))
    else:
        print(_err("\n  fix the corrupted core.bare above (or re-run `rig doctor --fix`) — git is broken there"))
    return errors.EXIT_REPO_CORRUPT


def _clean_deps_verdict(findings: _DoctorFindings) -> int:
    """Exit code once every dependency is satisfied: the remaining findings decide."""
    from . import errors

    if findings.dead_targets:
        # a dead target (but no missing dep) is still a problem worth a non-zero exit.
        print(_warn("\n  a missing target above needs attention (re-run `rig apply commit` or remove the stale entry)"))
        return errors.EXIT_MISSING_TARGET
    if findings.sched_problem:
        print(_err("\n  the scheduler above is unwritable — `rig apply` cannot install the model-freshness schedule"))
        return errors.EXIT_MISSING_TARGET
    if findings.config_dir_gaps:
        print(_err("\n  a claude config dir above lacks the rig hook bridge — those sessions run no guard (see fix: lines)"))
        return errors.EXIT_DRIFT
    print(_ok("\n  all required dependencies present"))
    return 0


def _doctor_verdict(args: argparse.Namespace, report, findings: _DoctorFindings) -> int | None:
    """The exit code when the findings alone decide it; ``None`` when dependency installation is next."""
    from . import errors

    missing_req = report.missing_required
    # an explicitly opted-into probe that FAILED must never end in the clean-bill success
    # path, and it outranks merely-OPTIONAL dep shortfalls; a missing REQUIRED dep keeps its
    # (more actionable) guidance path — the probe line is already printed above either way.
    failed_probes = [p for p in report.probes if p.ok is False]
    # repo corruption is the top-precedence failure: a broken .git makes every other git-backed
    # check unreliable. An UNFIXED corruption is non-zero regardless of the dependency picture.
    if findings.repo_corrupt:
        return _corruption_verdict(args)
    if failed_probes and not missing_req:
        print(_err("\n  an activation probe above FAILED — the claimed enforcement channel does not work"))
        return errors.EXIT_PROBE_FAILED
    if not missing_req and not (args.optional and report.missing_optional):
        return _clean_deps_verdict(findings)
    return None


def cmd_doctor(args: argparse.Namespace) -> int:
    from . import errors
    from .doctor import diagnose

    report = diagnose()
    print(_bold(f"rig doctor — {report.os.pretty}") + _dim(f"  (pkg manager: {report.os.package_manager or 'none detected'})"))
    print()
    _print_dep_statuses(report)
    _print_probe_results(report)
    findings = _collect_findings(args)
    verdict = _doctor_verdict(args, report, findings)
    if verdict is not None:
        return verdict

    if not args.yes:
        print(_warn("\n  missing dependencies above. Re-run with --yes to install them"))
        print(_dim("  (add --optional to also install optional deps)"))
        # honor the documented exit-code contract: a missing REQUIRED dep is the 127 class
        # (shell convention). A shortfall of only OPTIONAL deps (under --optional) is advisory,
        # not a hard "required tool absent", so it stays the generic non-zero.
        return errors.EXIT_MISSING_DEP if report.missing_required else 1

    return _doctor_bootstrap(report, include_optional=args.optional)


def _print_install_result(name: str, rc: int, fallback: list[str] | None) -> None:
    print(f"    {_ok('✔') if rc == 0 else _err('✗')} {name} (rc={rc})")
    # rc != 0 here on a PEP-668 interpreter is that install_cmd refusing, not a real
    # error worth a bare traceback-style "rc=1" — give the same actionable fallback
    # `_print_dep_statuses` would have shown before this bootstrap attempt ran.
    if rc != 0 and fallback:
        # this interpreter IS externally-managed, so a PEP-668 refusal is the LIKELY cause
        # of the failure above — but rc != 0 can also come from an unrelated network/
        # resolver/permission failure the marker check can't distinguish. Hedge the wording
        # accordingly rather than asserting a cause that isn't always true.
        print(f"        {_warn('install failed — if this interpreter refused it (PEP-668), try:')}")
        print(f"        {_dim('either: pipx install hyper-rig   (recommended)')}")
        print(f"            or: {_dim_cmd(fallback)}")


def _doctor_bootstrap(report, *, include_optional: bool) -> int:
    """The ``rig doctor --yes`` install leg: bootstrap the missing deps and map the outcome to the
    documented exit-code contract (a REQUIRED dep still absent is the 127 class)."""
    from . import errors
    from .doctor import bootstrap

    print(_bold("\n  installing missing dependencies..."))
    results = bootstrap(report, assume_yes=True, include_optional=include_optional)
    fallback_by_name = {st.dep.name: st.pep668_fallback for st in report.statuses if st.pep668_fallback}
    for name, rc in results:
        _print_install_result(name, rc, fallback_by_name.get(name))
    failed = {name for name, rc in results if rc != 0}
    if not failed:
        return 0
    # an install that left a REQUIRED dep absent is still the missing-dependency class;
    # a failed optional install is advisory (generic non-zero).
    req_names = {st.dep.name for st in report.statuses if st.dep.required and not st.present}
    return errors.EXIT_MISSING_DEP if failed & req_names else 1
