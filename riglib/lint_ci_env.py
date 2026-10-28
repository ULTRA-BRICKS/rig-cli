"""The one definition of "this is a CI run" shared by ``rig lint``'s report and its mutating steps.

``CI`` / ``GITHUB_ACTIONS`` count only when they carry an explicit true word (``1``, ``true``, ``yes``,
``on``; case-insensitive, whitespace ignored) -- what real CI systems export. An unusual value
(``CI=banana``, ``CI=2``, ``CI=y``) is NOT CI, so a stray variable in a developer's shell cannot disable
local setup, and no caller may keep its own copy of this rule: the report's ``ci=`` flag, the bootstrap
planner and the mutating action handler all call this function, so they can never disagree.
Stdlib-only.
"""

from __future__ import annotations

from collections.abc import Mapping

_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_CI_VARS = ("CI", "GITHUB_ACTIONS")


def ci_env_truthy(environ: Mapping[str, str]) -> bool:
    """True when ``CI`` or ``GITHUB_ACTIONS`` carries an explicit true word."""
    return any(environ.get(name, "").strip().lower() in _TRUE_WORDS for name in _CI_VARS)
