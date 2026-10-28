"""Environment guards shared by live-service handlers: sandboxed-HOME detection and dry-run flags."""

from __future__ import annotations

import functools
import os
from pathlib import Path


# ── the sandboxed-HOME guard for every launchd-touching action (rig-cli#116) ─────────
# Every fsutil-based action follows ``Path.home()``, so overriding ``$HOME`` LOOKS like a full
# sandbox — but ``launchctl`` acts on the per-user ``gui/<uid>`` domain, which HOME cannot
# redirect. A HOME-only-isolated ``rig apply`` (a test, an e2e leg, a manual catalog check) once
# bootstrapped the REAL ``ai.hyperide.tg-ctl`` agent from a plist under a scratch HOME (pointing at
# a nonexistent binary) and crash-looped it 23,641 times until booted out by hand. So: before ANY
# live launchctl (or systemctl --user) mutation, compare the resolved ``Path.home()`` with the uid's real
# login home; when they differ, the action behaves exactly like its ``RIG_*_DRY_RUN`` env (artifact
# written, live mutation skipped) and says so in its detail. Fail-closed: an unknown login home
# counts as sandboxed. ONE predicate feeds every launchd-backed provisioner's dry-run seam
# (schedule, tmux, tg_ctl, tcc_watch, tgctl_reaper, spotlight) via their ``_*_dry_run`` predicates,
# and ``_live_skip_reason`` renders the note.
@functools.lru_cache(maxsize=1)
def _real_login_home() -> Path | None:
    """The current uid's login home from the passwd database (resolved), or ``None`` if unknown.
    Process-stable, so cached (tests monkeypatch the NAME, which bypasses the cache)."""
    try:
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    except (ImportError, KeyError, OSError):
        return None


def _home_is_sandboxed() -> bool:
    """True when ``Path.home()`` (the HOME rig writes under) is NOT the uid's real login home —
    i.e. a HOME-override sandbox that a per-user launchd mutation would silently escape.

    ``Path.home()`` itself can fail-closed-worthy here: with ``$HOME`` unset AND no passwd entry
    for the uid (e.g. ``docker run --user <unmapped-uid>``), POSIX ``expanduser`` falls back to
    ``pwd.getpwuid`` and raises ``KeyError`` (wrapped as ``RuntimeError`` on Python >= 3.12) —
    the SAME "uid has no passwd entry" case :func:`_real_login_home` already turns into "sandboxed"
    by returning ``None``. Catch it here too so THIS predicate degrades to "sandboxed" instead of
    crashing. NOTE this does not make a bare `rig apply` crash-proof in that container shape on its
    own: several call sites (e.g. ``tg_ctl_plan_from_action``) call ``Path.home()`` directly to
    resolve artifact paths BEFORE this predicate ever runs, and would already raise there — a
    genuinely unresolvable HOME is a pre-existing, tool-wide limitation outside rig-cli#116's
    scope (mismatch detection, not HOME-resolution recovery). What this guards is narrower but
    still real: every RENDERING call site that reads ``Path.home()`` again for a detail message
    AFTER the predicate already answered (see :func:`_live_skip_reason`) must not crash even when
    a fresh ``Path.home()`` call would.
    """
    real = _real_login_home()
    if real is None:
        return True
    try:
        return Path.home().resolve() != real
    except (KeyError, OSError, RuntimeError):
        return True


def _env_flag(var: str) -> bool:
    return os.environ.get(var, "").strip().lower() in ("1", "true", "yes")


def _live_skip_reason(env_var: str, verb: str) -> str:
    """Why a live launchd/systemd mutation was skipped, for an action's detail: the env flag when
    it is set (``RIG_X_DRY_RUN — skipped <verb>``), else the sandboxed-HOME note.

    ``Path.home()`` itself can raise here in the exact state ``_home_is_sandboxed`` degrades to
    dry-run for (an unmapped-uid container: no ``$HOME``, no passwd entry) — this renderer must
    not reintroduce that crash on the reporting path. Render the raw ``$HOME`` env var instead
    when resolution fails; the detail is informational, never load-bearing.
    """
    if _env_flag(env_var):
        return f"{env_var} — skipped {verb}"
    real = _real_login_home()
    try:
        home = str(Path.home())
    except (KeyError, OSError, RuntimeError):
        home = os.environ.get("HOME", "<unset, no passwd entry>")
    return (
        f"HOME is overridden ({home} != {real if real is not None else 'unknown login home'})"
        f" — skipped live {verb} (sandboxed run)"
    )


def _schedule_dry_run() -> bool:
    """Honor RIG_SCHEDULE_DRY_RUN — write the artifact file but DON'T touch the live daemon.

    For CI / containers / smoke where a real ``launchctl load`` (a per-user daemon mutation
    HOME can't redirect) or a real ``systemctl --user enable`` is unwanted. The plist / unit files
    still land (in the HOME path), but the daemon load / timer enable is skipped. ALSO true
    automatically under a sandboxed HOME (:func:`_home_is_sandboxed`, rig-cli#116).
    """
    return _env_flag("RIG_SCHEDULE_DRY_RUN") or _home_is_sandboxed()


def _tmux_dry_run() -> bool:
    """Honor RIG_TMUX_DRY_RUN — write the tmux artifacts but DON'T run the LIVE activation.

    The live activation (clone tpm/resurrect/continuum, create ~/.tmux/resurrect, ``launchctl
    load -w`` the boot agent, take a first ``resurrect save``, clean continuum's stale macOS
    boot Login Items) is real network + daemon + ``tmux``-server access — unwanted in CI /
    containers / the unit suite. With the flag set, the on-disk artifacts still land; the live
    effects are skipped. Mirrors :func:`_schedule_dry_run`, including the automatic sandboxed-HOME
    skip (:func:`_home_is_sandboxed`, rig-cli#116).
    """
    return _env_flag("RIG_TMUX_DRY_RUN") or _home_is_sandboxed()
