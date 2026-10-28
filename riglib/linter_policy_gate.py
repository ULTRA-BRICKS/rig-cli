"""Readiness gate for the Rig-owned Oxlint policy (``rig apply``).

Kept out of ``plan.build`` so the planner stays a thin caller. Pure: reads only the
:class:`~riglib.linter_environment.LinterEnvironment` the planner already computed.
Stdlib-only at import time.
"""

from __future__ import annotations

from .linter_environment import LinterEnvironment

RIG_OXLINT_CONFIG = "oxlint.config.ts"
TSGOLINT = "oxlint-tsgolint"


def type_aware_note(env: LinterEnvironment) -> str:
    """The explicit note when only the type-aware portion is dropped (TypeScript >= 7 without tsgolint), else ``""``."""
    if TSGOLINT in env.missing_oxc_packages and not env.type_aware:
        return (
            "linters: type-aware linting disabled: TypeScript >= 7 but oxlint-tsgolint is not declared; "
            "type-info rules are omitted and the rest of the policy is applied"
        )
    return ""


def policy_block_reason(env: LinterEnvironment) -> str:
    """Why Rig cannot apply its Oxlint policy here, or ``""`` when it can.

    ``env.missing_oxc_packages`` already lists only what the ACTIVE policy needs
    (``@oxlint/plugins`` iff anti-slop is on). Oxlint reads one config flavor per directory, so a
    user-owned ``.oxlintrc.json(c)`` / ``oxlint.config.mts`` next to the Rig-owned
    ``oxlint.config.ts`` would be ambiguous and also blocks.
    """
    if not env.ready:
        return env.reason
    # `oxlint-tsgolint` only powers the OPTIONAL type-aware portion (TypeScript >= 7): without it the core
    # policy still applies and only the type-info rules are omitted (see `type_aware_note`).
    blocking = tuple(p for p in env.missing_oxc_packages if p != TSGOLINT)
    if blocking:
        return (
            "Rig anti-slop policy is blocked because " + ", ".join(blocking)
            + " is not declared in this repository; the generated local plugin cannot execute "
            "reliably without it."
        )
    others = [name for name in env.config_files if name != RIG_OXLINT_CONFIG]
    if others:
        return (
            "Rig rule policy is blocked because an Oxlint config in another flavor already exists ("
            + ", ".join(others)
            + f"); Rig owns {RIG_OXLINT_CONFIG}, so migrate the rules into Rig policy and remove the other file."
        )
    return ""
