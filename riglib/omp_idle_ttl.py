"""omp ``task.agentIdleTtlMs`` provisioning — the one owner of the default.

Accessed via: ``rig apply`` when ``harness`` is enabled and ``omp`` is a configured
kind → :func:`riglib.plan._build_omp_idle_ttl` emits ``provision_omp_idle_ttl``
→ the runner additively merges the grace-window value into
``~/.omp/agent/config.yml`` (honoring ``PI_CODING_AGENT_DIR`` / ``PI_CONFIG_DIR``).
Drift reads the same resolved value so apply and status cannot disagree.

Assumptions: omp parks an idle helper after ``task.agentIdleTtlMs`` (product default
420000 ms / 7 min — ``Settings.init``), and a parked agent drops off the *default*
``hub list`` view. A parked peer stays fully reachable regardless — `hub send` to a
known id revives it, and `history://<id>`/`agent://<id>` stay readable — parking only
trims what `hub list` shows by default. That makes a bounded grace window strictly
better than the old ``0`` (never park until process exit): ``0`` kept every idle
helper resident forever, which is exactly what accumulates unbounded zombie/idle
sessions. ``DEFAULT_OMP_IDLE_TTL_MS`` is deliberately shorter than omp's own stock
420000 ms so the choice reads as intentional, not an accidental inherit, while still
covering "read the yield report, send one follow-up" before a helper parks. An
explicit ``harness.idle_ttl.enabled: false`` skips provisioning entirely, and
``harness.idle_ttl.ms: 0`` still opts back into the old never-park behavior.

Past bugs: rig-cli#445/#464 originally wrote ``0`` unconditionally, which never parked
a yielded helper — a contributing cause of the unbounded idle/zombie-session growth
tracked alongside the zombie-reaper race investigation.
"""
from __future__ import annotations

from typing import Any

# Shorter than omp's own stock 420000ms/7min default (a deliberate, documented rig
# choice, not an accidental inherit); long enough to cover "read the yield report,
# send one follow-up" via `hub send` (a parked peer is still reachable by id through
# `hub send`/`history://`/`agent://` — parking only trims the default `hub list`
# view); and it bounds live-session growth instead of `0`'s never-park-until-exit
# lifetime, which is exactly what accumulates unbounded zombie/idle sessions.
DEFAULT_OMP_IDLE_TTL_MS = 300_000

# Single source of truth for the dotted YAML path; riglib.actions.runner
# re-exports this as OMP_IDLE_TTL_KEY (drift.py imports it from there).
IDLE_TTL_KEY: tuple[str, str] = ("task", "agentIdleTtlMs")


def resolve_idle_ttl_ms(harness: dict[str, Any] | None) -> int | None:
    """The idle-TTL value (ms) this harness block wants written, or ``None`` to skip.

    ``None`` means "emit no action" — either ``harness.idle_ttl.enabled: false``, or
    an ``idle_ttl.ms`` value in a shape rig does not understand (leniency mirroring
    :func:`riglib.omp_web_search.resolve_exclude`'s handling of a non-list ``exclude``).
    ``ms`` must be a plain ``int`` (never ``bool``, and never ``float`` — a stray
    ``.inf``/``.nan`` would otherwise reach ``int()`` and raise instead of failing
    closed to "skip"; every other numeric rig.yaml leaf is integer-only too).
    """
    if not isinstance(harness, dict):
        return DEFAULT_OMP_IDLE_TTL_MS
    block = harness.get("idle_ttl")
    if block is None:
        return DEFAULT_OMP_IDLE_TTL_MS
    if not isinstance(block, dict) or block.get("enabled") is False:
        return None
    if "ms" not in block:
        return DEFAULT_OMP_IDLE_TTL_MS
    raw = block.get("ms")
    if not isinstance(raw, int) or isinstance(raw, bool):
        return None
    return raw
