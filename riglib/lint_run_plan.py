"""Planning half of `rig lint run`: validate options, bootstrap (maybe), discover, plan, filter.

Context: `lint_run.execute` calls this module to turn options + a repository into the ordered list of
`PlannedCheck`s. Setup goes through `lint_bootstrap` (which plans with `plan.build` and applies with
`actions.run_plan`: the one engine, never a second executor) and runs ONLY for a bare local run:
`--plan`, `--no-setup`, CI and `lint.bootstrap.enabled: false` all stay read-only but still print
what would be installed. Nothing here executes a check.
"""
from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from . import cccc_install, lint_discovery, lint_providers, lint_run_config, lint_run_fix
from .errors import RigError
from .lint_check_scope import ScopeError, git_toplevel, restrictions
from .lint_ci_env import ci_env_truthy
from .lint_run_model import EXIT_USAGE, SCHEMA, CheckResult, RunOptions, RunReport
from .lint_run_proc import depth
from .lint_run_verdict import INCOMPLETE_PROBLEMS, static_result

if TYPE_CHECKING:
    from .lint_discovery import Discovery, DiscoveryProblem
    from .lint_providers import PlannedCheck, ToolEnv

_NODE_VERSION_TIMEOUT_S = 10


class Refusal(Exception):
    """A usage-level refusal discovered while planning (exit 2)."""


@dataclass(frozen=True)
class Setup:
    label: str
    plan: object | None
    outcome: object | None
    # Git ignore patterns the setup plan assumed (a pending `node_modules` ignore). `--plan` runs nothing, so it
    # lists its checks on the same post-ignore view; runs that execute checks keep the real on-disk view.
    assumed_ignored: tuple[str, ...] = ()


@dataclass(frozen=True)
class Planned:
    setup: Setup
    discovery: Discovery
    checks: tuple[PlannedCheck, ...]


def ci_from(opts: RunOptions, environ: Mapping[str, str]) -> bool:
    # One predicate with the bootstrap handlers, so the reported `ci=` can never disagree with a refusal.
    # An explicit `--ci` is a read-only request that no environment value (`CI=0`) can retract; the
    # environment only ever adds CI-ness.
    return bool(opts.ci) or ci_env_truthy(environ)


def usage_report(message: str, opts: RunOptions) -> RunReport:
    doc = {"schema": SCHEMA, "ok": False, "exit_code": EXIT_USAGE, "error": message,
           "fix": {"requested": opts.fix}}
    return RunReport(EXIT_USAGE, doc, f"error: {message}")


def _validate_categories(values: Sequence[str], flag: str) -> str | None:
    known = lint_run_config.CATEGORIES
    bad = [v for v in values if v not in known]
    if not bad:
        return None
    return f"{flag}: unknown categor{'ies' if len(bad) > 1 else 'y'} {', '.join(bad)} (choose from {', '.join(known)})"


def prepare(opts: RunOptions, environ: Mapping[str, str]):
    """Validate options and load config/root; returns (error message | None, cfg, root, prefixes)."""
    for values, flag in ((opts.only, "--only"), (opts.skip, "--skip")):
        problem = _validate_categories(values, flag)
        if problem:
            return problem, None, None, ()
    if opts.timeout is not None and not 1 <= opts.timeout <= 3600:
        return "--timeout must be an integer between 1 and 3600 seconds", None, None, ()
    if opts.fix and (opts.plan or ci_from(opts, environ)):
        return "--fix writes files and cannot be combined with --plan or --ci", None, None, ()
    try:
        cfg = lint_run_config.load_config(opts.cwd, opts.config)
    except (ValueError, RigError) as exc:
        return f"config error: {exc}", None, None, ()
    try:
        root = Path(git_toplevel(opts.cwd))
        prefixes = restrictions(str(root), opts.cwd, list(opts.scopes))
    except ScopeError as exc:
        return str(exc), None, None, ()
    return None, cfg, root, tuple(prefixes)


def _node_version(which: Callable[[str], str | None]) -> tuple[int, int, int] | None:
    node = which("node")
    if not node:
        return None
    try:
        proc = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=_NODE_VERSION_TIMEOUT_S,
                              check=False, stdin=subprocess.DEVNULL, shell=False)
    except (OSError, subprocess.SubprocessError):
        return None
    parts = proc.stdout.strip().lstrip("v").split(".")
    try:
        major, minor, patch = (int(p) for p in (parts + ["0", "0"])[:3])
    except ValueError:
        return None
    return major, minor, patch


def tool_env(root: Path, environ: Mapping[str, str], which: Callable[[str], str | None]) -> ToolEnv:
    system, machine = cccc_install.platform_key()  # the vocabulary rig-check's pin table uses
    return lint_providers.ToolEnv(
        root=root, which=which, system=system, machine=machine,
        node_version=_node_version(which), environ=environ, depth=depth(environ),
    )


def _in_scope(scope: str, prefixes: Sequence[str]) -> bool:
    if not prefixes or "" in prefixes:
        return True
    rel = "" if scope == "." else scope
    return any(rel == p or rel.startswith(p + "/") for p in prefixes)


def selection(opts: RunOptions, prefixes: Sequence[str]) -> Callable[[PlannedCheck], lint_providers.Unavailable | None]:
    """The execution selection as a predicate: why a check is left out, or None when it stays."""
    def excluded(check: PlannedCheck) -> lint_providers.Unavailable | None:
        if opts.only and check.category not in opts.only:
            detail = f"excluded by --only {','.join(opts.only)}"
        elif check.category in opts.skip:
            detail = f"excluded by --skip {check.category}"
        elif not _in_scope(check.scope, prefixes):
            detail = "outside the --scope selection"
        else:
            return None
        return lint_providers.Unavailable("not-applicable", "filtered", detail)
    return excluded


def _synthetic_check(check_id: str, provider: str, scope: str, tool: str) -> PlannedCheck:
    return lint_providers.PlannedCheck(
        id=check_id, provider=provider, category="lint", scope=scope, tool=tool, precedence=3,
        covers=frozenset(), argv=None, cwd=scope, unavailable=None, timeout_s=None, fix_argv=None,
        result_rule="exit-zero", resolution=None,
    )


def problem_result(problem: DiscoveryProblem) -> CheckResult:
    reason = "unreadable" if problem.kind == "unreadable" else "malformed-manifest"
    scope = os.path.dirname(problem.path) or "."
    detail = f"{problem.path}: {problem.detail}; fix the file so its tools can be discovered"
    return static_result(_synthetic_check(f"discovery@{problem.path}", "discovery", scope, "discovery"),
                         "failed", reason, detail)


def problem_rows(discovery: Discovery) -> tuple[CheckResult, ...]:
    return tuple(problem_result(p) for p in discovery.problems if p.kind in INCOMPLETE_PROBLEMS)


def disabled_result() -> CheckResult:
    return static_result(_synthetic_check("lint@.", "lint", ".", "rig"), "not-applicable", "disabled-by-config",
                         "lint.enabled is false in the rig config")


def planned_result(check: PlannedCheck, default_timeout_s: int) -> CheckResult:
    if check.argv is None:
        assert check.unavailable is not None
        return static_result(check, check.unavailable.state, check.unavailable.reason, check.unavailable.detail)
    return CheckResult(replace(check, timeout_s=check.timeout_s or default_timeout_s), "planned", "planned",
                       f"would run: {shlex.join(check.argv)}", None, None, None, "", "", False, None, None)


def _setup_label(opts: RunOptions, cfg: lint_run_config.LintConfig) -> str | None:
    """None means "may apply"; any string is the reason setup is not applied."""
    if opts.plan:
        return "skipped-plan"
    if opts.no_setup:
        return "skipped-no-setup"
    if not (cfg.enabled and cfg.bootstrap.enabled):
        return "disabled-by-config"
    return None


def _ci_label(plan, outcome) -> str:
    """Under CI only steps that write nothing in the repository (tool cache, toolchain components) run.

    A refused repo-editing step is a deliberate skip, not a failure: the check it would have enabled
    is reported `missing-tool` (exit 127), which already says the run is not a pass.
    """
    from .lint_bootstrap_model import CI_SAFE_KINDS

    safe_states = [state for step, (_, state, _) in zip(plan.steps, outcome.results) if step.kind in CI_SAFE_KINDS]
    if "failed" in safe_states:
        return "failed"
    if "applied" in safe_states:
        return "applied"
    return "skipped-ci" if any(step.kind not in CI_SAFE_KINDS for step in plan.steps) else "nothing-to-do"


def _selected_categories(opts: RunOptions, cfg: lint_run_config.LintConfig) -> lint_run_config.LintConfig:
    """``cfg`` with every category this run does not select switched off, so setup installs nothing for it.

    `--only limits` runs no linter, so no linter is installed; the bootstrap planner honors
    ``cfg.categories`` for the needs it plans. The checks keep the original config.
    """
    chosen = {c: on and (not opts.only or c in opts.only) and c not in opts.skip for c, on in cfg.categories.items()}
    return replace(cfg, categories=chosen)


def _plan_setup(opts: RunOptions, cfg, discovery: Discovery, env: ToolEnv, root: Path):
    """The lint-scoped bootstrap plan for ``discovery`` (the ONE planning path: `plan.build`)."""
    from . import lint_bootstrap, plan as plan_engine  # both import riglib.plan; needed once a repo is being linted
    from .config import LoadedConfig

    needs = lint_providers.bootstrap_needs(discovery, cfg, env)
    # The lint plan reads no config data (the resolved `LintConfig` travels in the request), so the loaded
    # config only names the repo root. The same catalog resolution `rig apply` uses, so the anti-slop policy
    # is written exactly as apply would; unavailable => an explicit `required-policy-unavailable` block.
    request = plan_engine.LintBootstrapRequest(needs, discovery, _selected_categories(opts, cfg), env)
    catalog = lint_bootstrap.resolve_policy_catalog()
    return plan_engine.build(LoadedConfig(data={}, repo_root=root), catalog, lint_bootstrap=request).lint_bootstrap


def _pending_ignores(plan) -> list[str]:
    """The git ignore patterns the plan's `git-exclude` step is about to record (empty without one)."""
    entries: list[str] = []
    for step, action in zip(plan.steps, plan.install_plan.actions, strict=True):
        if step.kind == "git-exclude":
            entries.extend(action.options.get("git_exclude", ()))
    return entries


def _bootstrap(opts: RunOptions, cfg, discovery: Discovery, env: ToolEnv, ci: bool,
               environ: Mapping[str, str], root: Path) -> Setup:
    from . import lint_bootstrap

    label = _setup_label(opts, cfg)
    plan = _plan_setup(opts, cfg, discovery, env, root)
    pending = tuple(_pending_ignores(plan))
    if pending:
        # The install creates `node_modules` AFTER the ignore for it is written, so what a local run would
        # install is decided against the repository as it will look then: dependencies already on disk (and not
        # ignored yet) must not count as project scopes that withhold the root's defaults, or the first run
        # would bootstrap less than the second one does. Every mode PRINTS that same setup (--plan, --no-setup
        # and --ci only preview it: nothing is written, and the discovery is unscoped like `plan_run`'s own).
        plan = _plan_setup(opts, cfg, lint_discovery.discover(root, assume_ignored=pending), env, root)
    if label is not None:
        return Setup(label, plan, None, pending)
    if plan.is_empty():
        return Setup("failed" if plan.setup_incomplete else "blocked" if plan.blocked else "nothing-to-do", plan, None, pending)
    # `--ci` is an explicit read-only request even when the environment does not say CI: the
    # bootstrap engine decides "CI" from the environment it is given, so hand it that fact.
    outcome = lint_bootstrap.apply_bootstrap(plan, root=root, environ={**environ, "CI": "1"} if ci else environ)
    if ci:
        return Setup(_ci_label(plan, outcome), plan, outcome, pending)
    return Setup("failed" if outcome.failed or plan.setup_incomplete else "applied", plan, outcome, pending)


def plan_run(opts: RunOptions, cfg, root: Path, prefixes: Sequence[str], environ: Mapping[str, str],
             which: Callable[[str], str | None], ci: bool) -> Planned:
    env = tool_env(root, {**environ, "CI": "1"} if ci else environ, which)  # `--ci` is CI for every planner
    discovery = lint_discovery.discover(root)
    setup = _bootstrap(opts, cfg, discovery, env, ci, environ, root)
    if setup.outcome is not None:
        discovery = lint_discovery.discover(root)  # the install changed manifests, locks and node_modules
    elif opts.plan and setup.assumed_ignored:
        # `--plan` executes nothing: its checks list describes the same post-ignore repository as its setup.
        discovery = lint_discovery.discover(root, assume_ignored=setup.assumed_ignored)
    config_path = str(root / opts.config) if opts.config else None  # absolute stays absolute under `/`
    # The selection runs INSIDE planning, before overlap resolution: a script that `--only`/`--skip`/`--scope`
    # leaves out cannot claim to cover (and demote) the direct tool check that will actually run.
    checks = lint_providers.plan_checks(discovery, cfg, env, config_path=config_path,
                                        select=selection(opts, prefixes))
    if opts.fix:
        blocked = lint_run_fix.partially_staged_under(root, {c.cwd for c in checks if c.argv is not None})
        if blocked:
            raise Refusal(f"--fix refused: partially staged files would be rewritten ({', '.join(blocked[:5])}); "
                          "stage or stash them first")
    return Planned(setup, discovery, checks)
