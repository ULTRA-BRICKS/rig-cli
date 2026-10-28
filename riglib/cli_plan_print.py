"""Plan loading and plan/result rendering shared by init, apply, status and config set.

Context: one `plan.build` call site and one renderer so every command shows the same plan.
"""
from __future__ import annotations

from pathlib import Path

from .cli_output import _bold, _dim, _err, _ok, _warn
from .harness_mode import NA_NOTE_MARKER


# ── shared helpers ────────────────────────────────────────────────────────────────
def _load_plan(
    cwd: str,
    config: str | None,
    project_type_override: str | None = None,
    *,
    allow_repo_autodiscovery_in_non_git: bool = True,
    layer_overrides: dict[Path, dict] | None = None,
    include_repo: bool | None = None,
):
    """Load config + catalog + build a plan. Returns (plan, loaded, env)."""
    from .catalog import Catalog
    from .config import load
    from .detect import detect_environment
    from .plan import build

    env = detect_environment(Path(cwd).resolve())
    explicit = _resolve_explicit_config(env, config)
    if include_repo is None:
        include_repo = _include_repo_config(env, explicit, allow_repo_autodiscovery_in_non_git)
    loaded = load(
        env.repo_root, explicit_config=explicit, include_repo=include_repo, layer_overrides=layer_overrides
    )
    catalog = Catalog.scan(loaded.agent_tools_source)
    ptype = project_type_override or env.project_type
    plan = build(loaded, catalog, project_type=ptype, stack_preset=env.stack_preset)
    return plan, loaded, env


def _resolve_explicit_config(env, config: str | None) -> Path | None:
    """Resolve ``--config`` the same way for every command path.

    A relative ``--config`` is relative to the detected git root, so the command works the
    same from the root or any subdirectory.
    """
    if not config:
        return None
    cp = Path(config)
    return (cp if cp.is_absolute() else env.repo_root / cp).resolve()


def _include_repo_config(env, explicit: Path | None, allow_repo_autodiscovery_in_non_git: bool) -> bool:
    """Whether this command context should load the repo/config layer."""
    if env.is_git_repo:
        return True
    if explicit is not None:
        return True
    return allow_repo_autodiscovery_in_non_git


def _validate_layer_in_isolation(
    layer_path: Path, *, data_override: dict | None = None
) -> None:
    """Build a plan over ONE config file alone (no cascade), so a catalog-backed value the file
    DECLARES can't be masked by a layer that merges over it.

    A `rig config set … --global` edit is otherwise only validated by the merged cascade
    (`_load_plan`), where a repo's `rig.yaml` overriding the just-broken global key (e.g. a bad
    `agent_tools_source`, or a global catalog item ref) hides the breakage — the write persists a
    config that fails in every OTHER repo. Loading the file as the sole explicit layer with
    `include_global=False` (its own directory has no `rig.yaml` to overlay) surfaces those errors
    against the file itself. Raises ConfigError / CatalogError / PlanError on rejection.

    Scope: only what the file ITSELF declares. A global file legitimately omits
    `agent_tools_source` (the checkout is supplied per-repo or via env), so we only run the
    catalog-backed plan build when this file pins its own `agent_tools_source` — there is nothing
    global-specific to catalog-validate otherwise, and demanding an independently-resolvable
    checkout would reject a valid deferring global config. Schema (`config.load`) still runs either
    way.
    """
    from .catalog import Catalog
    from .config import load
    from .plan import build

    loaded = load(
        layer_path.parent,
        explicit_config=layer_path,
        include_global=False,
        layer_overrides=({layer_path.resolve(): data_override} if data_override is not None else None),
    )
    if loaded.agent_tools_source is None:
        return  # no own catalog coordinate to mask — schema validation already ran in load()
    catalog = Catalog.scan(loaded.agent_tools_source)
    build(loaded, catalog)


# Above this many planned actions, `_print_plan` prints a per-carrier SUMMARY rather than the
# full wall of "category/item → target" lines (a fresh `rig init`/`apply` is ~120 actions, and
# a `config set` reconcile runs the SAME full engine). `--plan` always forces the full list; a
# genuinely small plan (a scoped `--only`, a near-empty config) stays fully inline — it is not a
# wall and the per-line detail is more useful than a count.
_PLAN_INLINE_MAX = 12


# Human labels for the plan summary, keyed by Action.category. An unlisted category falls back to
# its raw name (underscores hyphenated), so a newly added carrier can't silently break the summary.
_CATEGORY_LABELS = {
    "skills": "skills",
    "agent_hooks": "agent-hooks",
    "git_hooks": "git-hooks",
    "ci": "CI gates",
    "mcp": "MCP",
    "harness": "harness",
    "permissions": "permissions",
    "instructions": "global agent instructions",
    "agents_md": "agents.md",
    "ship_delegator": "ship gate",
    "ship_env": "ship env (machine)",
    "github": "GitHub",
    "models": "models",
    "tmux": "tmux",
    "linters": "linters",
    "gitignore": "gitignore",
    "tools": "tools",
    "tg_ctl": "tg-ctl",
    "tcc_watch": "tcc-watch",
    "tgctl_reaper": "tgctl-reaper",
    "sysmon": "sysmon",
}


def _plan_summary_line(plan) -> str:
    """One-line "<n> <carrier>, …" count of the plan, most-numerous carrier first."""
    from collections import Counter

    counts = Counter(a.category for a in plan.actions)
    parts = [
        f"{n} {_CATEGORY_LABELS.get(cat, cat.replace('_', '-'))}"
        # most-numerous first, ties broken alphabetically for a stable, reproducible read.
        for cat, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return ", ".join(parts)


# A note is a free-text string; these substrings mark a REAL GAP the user should see — something
# rig DECLINED to provision that the config asked for (a bridge/schedule/permission it could not
# wire) — as opposed to informational policy/wiring/status chatter. Deliberately NARROW: broad
# words ("has no", "not written", "missing", "cannot") also appear in intentional-state notes
# (export-only CI, a harness that reads a global instruction file instead of a skills dir), so
# they would cry wolf. Kept as one keyword set (single source of truth) rather than restructuring
# every `plan.notes.append` call site into a typed severity — centralized here and covered by
# tests. If a note class needs elevating, add its stable phrase here, not a generic word.
_NOTE_ATTENTION_MARKERS = (
    "skipped",  # "hook_bridge: skipped", "permissions: skipped", "models: schedule skipped"
    "dropped",  # "allow rules dropped for harness …"
    "not provisioned",
    "not wired",
    "no allowlist to provision",
    NA_NOTE_MARKER,  # harness_mode.na_note (declared): the HARNESS has no such knob (n/a)
    # NOTE: no dedicated entry for riglib.plan.HOOK_BRIDGE_NO_SUPPORT_PHRASE — its note text is
    # always prefixed "hook_bridge: skipped", which the "skipped" marker above already matches.
    # A second, re-typed substring of that constant here would be exactly the desync risk this
    # module's own docstring warns about (a wording tweak in plan.py silently stops matching);
    # not duplicating the phrase is the fix, not importing plan.py into this stdlib-only module.
)


def _note_needs_attention(note: str) -> bool:
    """True when a plan note reports a real provisioning gap (elevated), not just information."""
    low = note.lower()
    return any(marker in low for marker in _NOTE_ATTENTION_MARKERS)


def _print_notes(notes: list[str], *, expand: bool = False) -> None:
    """Print plan notes: real-gap notes elevated to a ``⚠`` line, informational ones collapsed.

    By default the informational notes are hidden behind a one-line count (``notes: N
    informational, M need attention — --notes to expand``) so they never form a wall; the gap
    notes (e.g. the hook_bridge one) are always shown. ``expand=True`` (``--notes``) prints every
    note verbatim.
    """
    if not notes:
        return
    attention = [n for n in notes if _note_needs_attention(n)]
    info = [n for n in notes if not _note_needs_attention(n)]
    for note in attention:
        print(f"  {_warn('⚠')} {note}")
    if expand:
        for note in info:
            print(f"  {_dim('note')}: {note}")
        return
    if info:
        n, m = len(info), len(attention)
        parts = [f"{n} informational"]
        if m:
            parts.append(f"{m} need attention")
        print(_dim(f"  notes: {', '.join(parts)} — --notes to expand"))


def _print_hook_bridge_coverage_gaps(plan) -> None:
    """`rig status` call-out for a harness kind with ZERO agents-hooks bridge coverage.

    ``rig apply`` builds this note unconditionally now (rig-cli#342's silent-skip fix): every
    configured/default harness kind with no known bridge surface (currently pi/commandcode) gets
    a "hook_bridge: skipped — kind '<kind>' has no supported agents-hooks bridge yet" plan note
    on EVERY build, not only when the user explicitly opted in with ``hook_bridge.enabled:
    true``. Without this, that note was only ever visible via ``rig apply``/``rig apply info`` —
    ``rig status`` is the surface the acceptance criterion actually names, so it must say it too.
    """
    from .plan import HOOK_BRIDGE_NO_SUPPORT_PHRASE

    gaps = [n for n in plan.notes if HOOK_BRIDGE_NO_SUPPORT_PHRASE in n]
    if not gaps:
        return
    print()
    for note in gaps:
        print(f"  {_warn('⚠')} {note}")


def _print_plan(
    plan, *, full: bool = False, preview: bool = False, expand_notes: bool = False,
    preview_of: str = "rig apply",
) -> None:
    # `preview=True` marks the plan as NOT applied (the `rig init` scaffold/no-TUI paths): the
    # plan is what ``preview_of`` WOULD do, so it must never read as completed work.
    label = _dim(f"   — PREVIEW of `{preview_of}` (not applied)") if preview else ""
    print(_bold(f"\nPlan: {len(plan)} action(s)  [on_conflict={plan.on_conflict}]") + label)
    if full or len(plan.actions) <= _PLAN_INLINE_MAX:
        for a in plan.actions:
            print(f"  {_dim('•')} {a.category}/{a.item} → {a.target}")
    else:
        print(f"  {_plan_summary_line(plan)}")
        print(_dim(f"  (run with --plan to list all {len(plan.actions)} actions)"))
    _print_notes(plan.notes, expand=expand_notes)


# Result statuses that mean the action CHANGED disk (vs "skipped" = already in sync).
_CHANGED_STATUSES = ("created", "updated", "backed_up")


def _print_results(report, *, verbose: bool = False, list_hint: str | None = None) -> None:
    """Print apply results grouped by what CHANGED vs what was already in sync.

    Changed/error rows always print; already-in-sync (``skipped``) no-ops are collapsed behind a
    one-line count unless ``verbose``. ``list_hint`` (e.g. ``"-v"``) is the flag the current
    command exposes to reveal the collapsed rows — passed ONLY by front-ends that actually accept
    it (``rig apply commit``), so the hint never advertises a flag the caller lacks (``init
    --apply`` / ``config set`` reuse this renderer without a ``-v``). The trailing ``Summary:``
    line is unchanged so existing status/parity assertions keep working.
    """
    icon = {
        "created": _ok("✔"),
        "updated": _ok("✔"),
        "backed_up": _warn("↩"),
        "skipped": _dim("·"),
        "planned": _dim("•"),
        "error": _err("✗"),
    }
    in_sync = [r for r in report.results if r.status == "skipped"]
    changed = [r for r in report.results if r.status != "skipped"]
    rows = report.results if verbose else changed
    for r in rows:
        print(f"  {icon.get(r.status, '?')} {r.action.category}/{r.action.item}: {r.detail}")
    if in_sync and not verbose:
        hint = f" (run {list_hint} to list)" if list_hint else ""
        print(_dim(f"  {icon['skipped']} {len(in_sync)} already in sync{hint}"))
    summary = ", ".join(f"{k}={v}" for k, v in sorted(report.summary().items()))
    print(_bold(f"\nSummary: {summary}"))


def _run_and_print_verify(plan, applied=None) -> bool:
    """Run the post-apply verify framework over ``plan`` and print each check. Returns ok/not-ok.

    Runs only checks for provisioners actually in the plan. Skipped checks (non-macOS / dry-run
    launchd) never fail the result — only a real FALSE does. This is the prove-it-worked step:
    every provisioner that declares a verify() is confirmed to have taken effect.
    """
    from .verify import verify_plan

    report = verify_plan(plan, applied)
    if not report.results:
        return True
    icon = {"pass": _ok("✔"), "FAIL": _err("✗"), "skipped": _dim("·")}
    print(_bold("\nVerify:"))
    for r in report.results:
        print(f"  {icon.get(r.state, '?')} {r.category}/{r.item}: {r.evidence}")
    summary = ", ".join(f"{k}={v}" for k, v in sorted(report.summary().items()))
    print(_bold(f"Verify summary: {summary}"))
    if not report.ok:
        print(_err(f"verify: {len(report.failures)} check(s) FAILED — provisioned artifact did not take effect"))
    return report.ok
