"""`rig profile` handlers and the auto-sync of the active profile.

Context: profile switches reuse the same plan + apply engine.
"""
from __future__ import annotations

import argparse
import sys

from .cli_output import _dim, _err, _ok, _warn


def _profile_notify(level: str, text: str) -> None:
    """Render one live message from :mod:`riglib.profile` (warnings to stderr, loud)."""
    if level == "warn":
        print(_warn(text), file=sys.stderr, flush=True)
    elif level == "error":
        print(_err(text), file=sys.stderr, flush=True)
    else:
        print(_dim(text), flush=True)


def _print_profile_outcome(outcome) -> int:
    style = {"pushed": _ok, "pulled": _ok, "synced": _ok, "in_sync": _ok, "nothing": _dim}.get(outcome.result, _err)
    prefix = "profile: CONFLICT — " if outcome.result == "conflict" else "profile: "
    print(style(prefix + outcome.detail))
    for level, text in outcome.messages:
        _profile_notify(level, text)
    return outcome.code


def _print_profile_error(exc) -> int:
    print(_err(f"error: {exc.what}"), file=sys.stderr)
    if exc.fix:
        print(_ok("  fix: ") + exc.fix, file=sys.stderr)
    return exc.code


def cmd_profile(args: argparse.Namespace) -> int:
    """`rig profile init|sync|status` — thin dispatch; the engine is :mod:`riglib.profile`."""
    from . import profile
    from .profile_files import NEVER_SYNCED

    command = getattr(args, "profile_command", None)
    if not command:
        print(_err("error: `rig profile` needs a subcommand: init, sync, or status"), file=sys.stderr)
        print(_dim("  rig profile init [URL]         # clone (or seed) the private profile repo"), file=sys.stderr)
        print(_dim("  rig profile sync [--take …]    # push/pull config.yaml + tmux/omp/sysmon files"), file=sys.stderr)
        print(_dim("  rig profile status [--fetch]   # which side changed (writes nothing)"), file=sys.stderr)
        return 2
    try:
        if command == "init":
            return _print_profile_outcome(
                profile.init(args.url, host=args.host, auto_sync=args.auto_sync, notify=_profile_notify)
            )
        if command == "sync":
            return _print_profile_outcome(
                profile.sync(take=args.take, only=tuple(args.only) if args.only else None, notify=_profile_notify)
            )
        st = profile.status(fetch=args.fetch)
    except profile.ProfileError as exc:
        return _print_profile_error(exc)
    verdicts = {
        "in_sync": _ok("in sync"),
        "push": _warn("local changed — `rig profile sync` pushes it"),
        "pull": _warn("remote changed — `rig profile sync` pulls it (local file backed up)"),
        "both": _warn("some files changed locally, others remotely — `rig profile sync` moves each way"),
        "conflict": _err("CONFLICT — both changed; resolve with `rig profile sync --take local|remote [--only ID]`"),
    }
    print(f"profile: {st.url}")
    print(_dim(f"  checkout: {st.checkout}"))
    for item in st.files:
        print(f"  {item.id:<11} {item.path}  local {item.local}, remote {item.remote} → {item.verdict}")
    print(f"  last sync: {st.synced_at or 'never'}")
    print(f"  state: {verdicts[st.verdict]}")
    if not args.fetch:
        print(_dim("  (compared against the last-fetched remote; --fetch to refresh)"))
    print(_dim("  never synced (secrets): " + ", ".join(entry.name for entry in NEVER_SYNCED)))
    return 0 if st.verdict == "in_sync" else profile.EXIT_CONFLICT


def _apply_auto_sync_profile(*, force: bool = False) -> None:
    """`profile.auto_sync: true` → sync the global config before `rig apply commit` builds its plan.

    Non-fatal by contract: any failure (network, push refused, conflict, not initialized) prints a
    warning and the apply continues with the LOCAL file as it is on disk. ``force`` (``rig activate
    commit``) syncs whenever ``profile.repo`` is configured, even with ``auto_sync: false``.
    """
    from . import profile

    if not (profile.configured() if force else profile.auto_sync_enabled()):
        return
    try:
        outcome = profile.sync(noninteractive=True, notify=_profile_notify)
    except profile.ProfileError as exc:
        print(_warn(f"profile auto_sync skipped: {exc.what} — applying the local config"), file=sys.stderr)
        if exc.fix:
            print(_dim(f"  fix: {exc.fix}"), file=sys.stderr)
        return
    except Exception as exc:  # noqa: BLE001 — auto-sync must never break apply
        print(_warn(f"profile auto_sync failed ({type(exc).__name__}: {exc}) — applying the local config"),
              file=sys.stderr)
        return
    if outcome.result == "conflict":
        print(_warn(f"profile auto_sync: {outcome.detail} — applying the local config"), file=sys.stderr)
        print(_dim("  resolve with `rig profile sync --take local|remote`"), file=sys.stderr)
        return
    if outcome.result in ("pulled", "synced"):
        # the plan about to run is built from a config the user has not previewed — say so loudly
        print(_warn(f"profile auto_sync: {outcome.detail}"))
        print(_warn("  the plan below is built from the PULLED config (preview: `rig apply`)"))
        return
    print(_dim(f"profile auto_sync: {outcome.detail}"))
