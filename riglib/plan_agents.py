"""Plan stages for per-harness keepalive/budget knobs and repo-level agent files (AGENTS.md symlink,
ship delegator, ship task prefix).

Split out of :mod:`riglib.plan`.
"""

from __future__ import annotations

import re
from .catalog import Catalog
from .config import LoadedConfig
from .harness_skills import codex_user_path as _codex_user_path
from .harness_skills import omp_user_path as _omp_user_path
from .plan_harness import _enabled_harness_block
from .plan_model import Action
from .plan_model import InstallPlan
from .plan_targets import _configured_harness_kinds
from .plan_targets import _expand


def _build_omp_idle_ttl(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan the omp ``task.agentIdleTtlMs`` grace-window write for new omp processes.

    omp parks idle helpers after ``task.agentIdleTtlMs`` (product default 420000 ms);
    parked agents drop off the *default* ``hub list`` view (a parked peer stays fully
    reachable via ``hub send``/``history://``/``agent://`` regardless). rig's default,
    :data:`riglib.omp_idle_ttl.DEFAULT_OMP_IDLE_TTL_MS` (300000 ms / 5 min), is shorter
    than omp's own stock default and bounds live-session growth, unlike the old ``0``
    (never park until process exit) that let idle helpers accumulate without bound.
    Override with ``harness.idle_ttl.ms`` (``<= 0`` restores the old never-park
    behavior), or skip entirely with ``harness.idle_ttl.enabled: false``. A live omp
    process keeps the spawn-cached TTL until restart; the YAML write does not unpark
    helpers in an already-running session. Emitted whenever a configured harness kind
    is omp and the harness block is enabled.
    """
    from .omp_idle_ttl import resolve_idle_ttl_ms

    h = _enabled_harness_block(config)
    if h is None:
        return
    if "omp" not in _configured_harness_kinds(config):
        return
    desired_ms = resolve_idle_ttl_ms(h)
    if desired_ms is None:
        return
    plan.actions.append(
        Action(
            kind="provision_omp_idle_ttl",
            category="harness",
            item="omp-idle-ttl",
            source=config.repo_root,
            target=_expand(_omp_user_path("config.yml"), config.repo_root),
            options={"kind": "omp", "ms": desired_ms},
        )
    )


def _build_codex_skills_budget(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan the codex ``skills.max_context_tokens`` write for a sizeable personal skill library.

    Codex CLI truncates/omits skill descriptions from its model-visible catalog once the
    library's rendered text exceeds Codex's own (much smaller) context budget for that catalog
    (openai/codex#24299) — a real gap on any machine whose skill library grows past Codex's stock
    default. rig's default, :data:`riglib.codex_skills_budget.DEFAULT_MAX_CONTEXT_TOKENS`
    (50000), was verified (2026-09-18, InvestigateCodexSkillLoading) to roughly double visible
    description length and plateau around 40KB of rendered skill text. Override with
    ``harness.codex_skills_budget.max_context_tokens``, or skip entirely with
    ``harness.codex_skills_budget.enabled: false``. The write is ADDITIVE and never clobbers a
    value already on disk (even a lower one the user set by hand, or a higher one) — only an
    ABSENT key gets written, mirroring :func:`_build_omp_idle_ttl`. Emitted whenever a configured
    harness kind is codex and the harness block is enabled.

    Unlike the codex auto/permission-MODE write (``apply_harness``, opt-in — only fires once the
    user declares an explicit ``auto_mode``/``mode`` intent), this fires unconditionally whenever
    codex is ANY configured kind — the whole point of a machine-wide default rollout. That wider
    blast radius means a Python without a TOML parser (3.10 without the ``tomli`` backport, which
    ``pyproject.toml`` ships only in the ``test`` extra) MUST NOT turn a previously-green
    ``rig apply`` red: skip emitting the action and leave a VISIBLE plan note instead of letting
    the writer hit ``_toml_parser() is None`` and return an apply-failing ``error`` status for
    every configured-codex machine on that Python.
    """
    from .codex_skills_budget import resolve_skills_budget_tokens, toml_parser_available

    h = _enabled_harness_block(config)
    if h is None:
        return
    if "codex" not in _configured_harness_kinds(config):
        return
    desired_tokens = resolve_skills_budget_tokens(h)
    if desired_tokens is None:
        return
    if not toml_parser_available():
        plan.notes.append(
            "harness: codex skills-budget skipped — no TOML parser on this Python "
            "(3.10 needs the `tomli` backport); install tomli to let rig manage "
            "skills.max_context_tokens in ~/.codex/config.toml"
        )
        return
    plan.actions.append(
        Action(
            kind="provision_codex_skills_budget",
            category="harness",
            item="codex-skills-budget",
            source=config.repo_root,
            target=_expand(_codex_user_path("config.toml"), config.repo_root),
            options={"kind": "codex", "max_context_tokens": desired_tokens},
        )
    )


def _build_agents_symlink(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan the AGENTS.md (canonical) + CLAUDE.md (symlink) provisioning for the repo.

    Default **ON**: every repo should expose one agent-guide file under both names so every
    harness reads the same instructions, plus the marker-delimited orchestrator and
    autonomous-mode reminders spliced into that guide. Opt out with
    ``agents_md: { enabled: false }`` (or ``{ symlink: false }``). The classify-and-converge
    logic lives in ``actions/`` (it depends on what is already on disk), so the plan emits
    idempotent actions anchored at the repo root; no carrier in agent-tools.
    """
    am = config.data.get("agents_md")
    if am is None:
        am = {}
    if not isinstance(am, dict):
        return  # validate() already fail-closed on a non-mapping block
    if am.get("enabled") is False or am.get("symlink") is False:
        return
    plan.actions.append(
        Action(
            kind="provision_agents_symlink",
            category="agents_md",
            item="symlink",
            source=config.repo_root,
            target=config.repo_root,
            options={},
        )
    )
    # Symlink first so create_both writes the placeholder before these splices run.
    plan.actions.append(
        Action(
            kind="provision_agents_orchestrator",
            category="agents_md",
            item="orchestrator",
            source=config.repo_root,
            target=config.repo_root,
            options={},
        )
    )
    plan.actions.append(
        Action(
            kind="provision_agents_autonomous_mode",
            category="agents_md",
            item="autonomous-mode",
            source=config.repo_root,
            target=config.repo_root,
            options={},
        )
    )


def _build_ship_delegator(config: LoadedConfig, catalog: Catalog, plan: InstallPlan) -> None:
    """Plan the per-repo ``.claude/scripts/pr-ship.sh`` (``gh ship`` delegator) provisioning.

    Default **ON**: every managed repo should expose the delegator the global ``gh ship`` alias
    runs, so ``gh ship`` works there on a clean machine — not only in agent-tools (the only repo that
    historically carried it). Opt out with ``ship_delegator: { enabled: false }``.

    Fail-CLOSED on a checkout lacking the canonical ``ci/ship/ship.sh``: rig never provisions a
    delegator that would exec a non-existent script. The canonical path is resolved from the
    agent-tools checkout NOW and carried in the action's ``canonical_ship`` option — apply writes
    it (as the agent-tools root) into the MACHINE-level ``$XDG_CONFIG_HOME/agent-tools/env`` file,
    NOT into the delegator, whose rendered content is a portable constant (a re-apply / drift
    compare is a byte-for-byte no-op even for a repo that commits it). The classify-and-converge
    + the ``.git/info/exclude`` ignore handling live in ``actions/`` (they depend on what is on
    disk); the plan emits one idempotent action anchored at the repo root. No carrier in agent-tools.
    """
    sd = config.data.get("ship_delegator")
    if sd is None:
        sd = {}
    if not isinstance(sd, dict):
        return  # validate() already fail-closed on a non-mapping block
    delegator_enabled = sd.get("enabled") is not False
    # The `gh ship` gh alias is provisioned by ONE action, gated by EITHER path that wants it: the
    # (default-on) ship_delegator area, OR a ci `ship` item with `gh_alias: true`. `_build_ci` runs
    # BEFORE this, so its already-resolved ship action is in the plan — reading it here (rather than
    # re-deriving the ci enable logic) keeps a single source of truth AND guarantees exactly one
    # alias action (never a second, unconditional writer racing the reconciler). When ONLY the ci
    # path asks (delegator disabled), the alias is still provisioned but no delegator file is.
    ci_alias_requested = any(
        a.kind == "install_ci" and a.options.get("slot") == "ship" and a.options.get("gh_alias")
        for a in plan.actions
    )
    if not (delegator_enabled or ci_alias_requested):
        return  # nothing to do — neither path wants the delegator or the alias
    canonical = catalog.source / "ci" / "ship" / "ship.sh"
    if not canonical.is_file():
        plan.notes.append(
            "ship_delegator: skipped — no ci/ship/ship.sh in this agent-tools checkout "
            f"({catalog.source}); `gh ship` cannot delegate (update agent-tools to a version "
            "that ships the ship gate)"
        )
        return
    if delegator_enabled:
        plan.actions.append(
            Action(
                kind="provision_ship_delegator",
                category="ship_delegator",
                item="delegator",
                source=catalog.source,
                target=config.repo_root,
                options={"canonical_ship": str(canonical)},
            )
        )
    # The machine-global `gh ship` alias is the ENTRY POINT the delegator serves: `gh ship <PR>`
    # runs it, and it dispatches to `<repo>/.claude/scripts/pr-ship.sh` (or the canonical fallback).
    # It was historically HAND-SET; provision it so a clean machine has a working `gh ship` and it
    # can't silently go missing. A GLOBAL artifact (the expansion is a portable constant, so the
    # action carries no options) — its target is gh's config file, matching the drift row's target.
    from .gh_ship_alias import GH_SHIP_ALIAS_CATEGORY, gh_config_path

    # The alias's out-of-repo fallback resolves AGENT_TOOLS_ROOT from the machine-level env file,
    # which `_do_provision_ship_delegator` reconciles. In the ci-only combo (delegator DISABLED but
    # a ci `ship` item asks for the alias) NO delegator action is emitted, so the alias action must
    # own the env file itself — else `gh ship` outside a managed repo resolves nothing and exits 127
    # on a clean machine (codex P2, PR #151). Carry canonical_ship ONLY then: when the delegator is
    # emitted it reconciles the env file, so the alias stays a portable, option-less constant (no
    # double writer). The runner + drift read this option to decide whether to reconcile/check it.
    # (KNOWN GAP, pre-existing and out of this thread's scope: a SELF-HOSTING repo — agent-tools —
    # with the delegator enabled skips the env reconcile entirely, so a clean machine that only ever
    # applies agent-tools has no env file and `gh ship` outside it exits 127. The durable fix is to
    # make the machine-global alias the SOLE owner of the machine-global env file; tracked separately.)
    alias_options: dict[str, str] = {} if delegator_enabled else {"canonical_ship": str(canonical)}
    plan.actions.append(
        Action(
            kind="provision_gh_ship_alias",
            category=GH_SHIP_ALIAS_CATEGORY,
            item="alias",
            source=catalog.source,
            target=gh_config_path(),
            options=alias_options,
        )
    )


# The task.code_prefix / SHIP_TASK_CODE_PREFIX contract, encoded ONCE and shared with
# actions/runner.py's re-check before writing the line to disk (a shell-sourced file, so the
# runner is the last gate) — the JSON Schema's not_pattern (config_schema.py) is a THIRD,
# necessarily-separate encoding (regex syntax the schema generator emits), kept in sync by hand.
SHIP_TASK_CODE_PREFIX_RE = re.compile(r"[A-Z0-9]{1,40}")


def _build_ship_task_prefix(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan ``.ship-config``'s ``SHIP_TASK_CODE_PREFIX`` line from ``task.code_prefix``.

    Unset (the default) emits NOTHING — the action is only added when a prefix is actually
    configured, so a repo that never touches the ``task`` block never gets a ``.ship-config``
    conjured out of nothing. See ``_do_provision_ship_task_prefix`` (actions/runner.py) for why
    this is a merge into any existing file rather than a whole-file write, and
    ``ci/ship/ship.sh``'s header doc for why the prefix exists: review-cli's review-quorum
    store is a single GLOBAL file keyed only by the task-code string, with no per-repo scoping —
    a repo whose task-cli backend is GitHub Issues (bare ``#NNN``, no code convention of its
    own) needs a repo-unique prefix synthesized in front of the issue number, or two repos both
    deriving bare ``#346`` would silently share (and falsely satisfy) each other's count.
    """
    task = config.data.get("task")
    if task is None:
        task = {}
    if not isinstance(task, dict):
        # validate() (config.py's _validate_task) already raises ConfigError for a non-mapping
        # `task` block in the normal production path (rig apply / rig config set always call
        # validate() first) — this branch is bypass-only defense-in-depth for a caller that
        # constructs a LoadedConfig directly (see test_build_notes_non_mapping_task_block_
        # when_validate_is_bypassed). Note it anyway, for the same reason a malformed
        # code_prefix gets a note below: silence here would look identical to "no task block
        # configured" and a caller that DID bypass validate() would never learn why nothing
        # was written.
        plan.notes.append(f"task {task!r} is not a mapping — not writing .ship-config")
        return
    prefix = task.get("code_prefix")
    if prefix is None or prefix == "":
        return  # genuinely unset — no note, this is the expected default
    if not isinstance(prefix, str):
        # validate() checks block/scalar STRUCTURE, not a leaf's declared type (see the
        # not_pattern comment in config_schema.py) — a non-string value reaches here
        # undetected. Deliberately NOT coerced (e.g. an unquoted `code_prefix: 12345` parses
        # as a YAML int): the JSON schema declares `"type": "string"`, so a coercing builder
        # would silently accept a value every schema-aware editor already flags as invalid —
        # the same string/schema disagreement the config_schema.py:870 comment warns against.
        # Note it, same as a malformed string below, and tell the author how to fix it.
        plan.notes.append(
            f"task.code_prefix {prefix!r} is not a string (quote it, e.g. code_prefix: \"12345\") "
            "— not writing .ship-config"
        )
        return
    # Re-validate the SAME 1-40-uppercase-letters/digits contract ship.sh enforces (see
    # ci/ship/ship.sh's SHIP_TASK_CODE_PREFIX parsing) here too, at plan-build time — not just
    # in the JSON schema's `not_pattern`. Catching a malformed value HERE means `rig apply`
    # refuses to even attempt the write, rather than writing a value ship.sh will later read and
    # discard by ignoring the WHOLE .ship-config file (silently dropping any hand-committed
    # SHIP_LOCAL_TEST_DIR/SHIP_LOCAL_TEST_CMD override too — exactly what the merge-not-overwrite
    # design in _do_provision_ship_task_prefix exists to protect).
    if not SHIP_TASK_CODE_PREFIX_RE.fullmatch(prefix):
        plan.notes.append(
            f"task.code_prefix {prefix!r} is not 1-40 uppercase letters/digits — "
            "not writing .ship-config (fix the value, or ship.sh would ignore the whole file)"
        )
        return
    plan.actions.append(
        Action(
            kind="provision_ship_task_prefix",
            category="task",
            item="code_prefix",
            source=config.repo_root,
            target=config.repo_root,
            options={"code_prefix": prefix},
        )
    )
