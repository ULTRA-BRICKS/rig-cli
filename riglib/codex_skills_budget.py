"""Codex ``[skills] max_context_tokens`` provisioning — the one owner of the default.

Accessed via: ``rig apply`` when ``harness`` is enabled and ``codex`` is a configured kind →
:func:`riglib.plan._build_codex_skills_budget` emits ``provision_codex_skills_budget`` → the
runner additively merges ``max_context_tokens`` into the ``[skills]`` table of
``~/.codex/config.toml`` (honoring ``RIG_CODEX_HOME``). Drift reads the same resolved value so
apply and status cannot disagree.

Assumptions: Codex CLI truncates/omits skill descriptions from its model-visible catalog once a
personal skill library's rendered text exceeds its own (much smaller) context budget for that
catalog — a real, upstream-acknowledged gap (openai/codex#24299) that bites any machine whose
``~/.agents/skills`` (or ``~/.codex/skills``) library grows past Codex's stock default (observed
~5440-8000 chars on a 71-skill library). Setting ``[skills].max_context_tokens`` in
``~/.codex/config.toml`` raises that budget; ``DEFAULT_MAX_CONTEXT_TOKENS`` (50000) was verified
on 2026-09-18 (InvestigateCodexSkillLoading) to roughly double the visible description length
before plateauing around 40KB of rendered skill text — comfortably above any realistic personal
skill library while still bounded (not "unlimited", which would let a future library regress this
again unnoticed). An explicit ``harness.codex_skills_budget.enabled: false`` skips provisioning
entirely. The write is ADDITIVE and never clobbers a value already on disk (even a lower one the
user set by hand, or a HIGHER one) — only an ABSENT key gets written, the exact convention
:mod:`riglib.omp_idle_ttl` established for ``task.agentIdleTtlMs``: apply is a one-shot "set the
default if nobody has an opinion yet", not a policy enforcer that fights the user's own edits.
``rig status``/drift still surfaces ANY on-disk value that differs from the resolved default, so a
value changed out from under a previously-managed key stays visible, apply just never auto-fixes
it (informational, not enforced — see :func:`riglib.drift._check_codex_skills_budget`).

Past bugs: none yet — this is the FIRST rig-managed instance of this key. A manual
``~/.codex/config.toml`` edit made while investigating rig-cli's own skill-loading
(InvestigateCodexSkillLoading, 2026-09-18 session) on the machine this default was authored on
already set ``[skills].max_context_tokens = 50000`` by hand; this module reconciles that into
managed history rather than leaving it a permanent unmanaged snowflake — apply on that machine
finds the key already present with the desired value and reports ``skipped`` (not "created"),
and drift reports it clean.
"""
from __future__ import annotations

from typing import Any

# Codex CLI's own default budget for the model-visible skills catalog is small enough that a
# sizeable personal skill library truncates/omits descriptions (openai/codex#24299). Verified on
# this machine (2026-09-18, InvestigateCodexSkillLoading): raising it to 50000 roughly doubles
# visible description length and plateaus around 40KB of rendered skill text — comfortably above
# any realistic personal library, without going "unlimited".
DEFAULT_MAX_CONTEXT_TOKENS = 50_000

# Single source of truth for the dotted TOML key path; riglib.actions.runner re-exports this as
# CODEX_SKILLS_BUDGET_KEY (drift.py imports it from there).
SKILLS_BUDGET_KEY: tuple[str, str] = ("skills", "max_context_tokens")


def toml_parser_available() -> bool:
    """True when this Python can parse TOML — stdlib ``tomllib`` (3.11+) or the ``tomli``
    backport (3.10, declared in the ``test`` extra only — a bare ``pip install rig-cli`` on 3.10
    has neither). Mirrors :func:`riglib.actions.runner._toml_parser`'s exact fallback so the plan
    builder and the writer never disagree about whether this machine can manage the key; pinned
    equal by ``tests/test_codex_skills_budget.py``. Duplicated rather than imported from
    ``riglib.actions.runner`` because THAT module already imports THIS one at module scope
    (``from ..codex_skills_budget import DEFAULT_MAX_CONTEXT_TOKENS, SKILLS_BUDGET_KEY`` in
    ``actions/runner.py``) — a reverse module-scope import here would be a direct two-module
    cycle. The plan builder's own import of this module stays function-scoped (deferred to call
    time, after both modules are fully loaded), the existing convention every other plan builder
    in ``riglib.plan`` already uses for its concern-specific module.
    """
    try:
        import tomllib  # noqa: F401
    except ImportError:
        try:
            import tomli  # noqa: F401
        except ImportError:
            return False
    return True


def resolve_skills_budget_tokens(harness: dict[str, Any] | None) -> int | None:
    """The ``max_context_tokens`` value this harness block wants written, or ``None`` to skip.

    ``None`` means "emit no action" — either ``harness.codex_skills_budget.enabled: false``, or a
    ``max_context_tokens`` value in a shape rig does not understand (leniency mirroring
    :func:`riglib.omp_idle_ttl.resolve_idle_ttl_ms`'s handling of a non-int ``ms``). The value
    must be a plain positive ``int`` (never ``bool``, and never a stray ``float`` — including
    non-finite ``.inf``/``.nan`` that would otherwise reach ``int()`` and raise instead of failing
    closed to "skip"; ``config.validate`` rejects the same shapes at load time, this is defense in
    depth for a caller that bypasses it, e.g. a unit test building ``LoadedConfig`` directly).
    """
    if not isinstance(harness, dict):
        return DEFAULT_MAX_CONTEXT_TOKENS
    block = harness.get("codex_skills_budget")
    if block is None:
        return DEFAULT_MAX_CONTEXT_TOKENS
    if not isinstance(block, dict) or block.get("enabled") is False:
        return None
    if "max_context_tokens" not in block:
        return DEFAULT_MAX_CONTEXT_TOKENS
    raw = block.get("max_context_tokens")
    if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
        return None
    return raw
