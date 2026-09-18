"""tgctl-reaper sweep logic (ported from agent-tools/ops/tgctl-omp-acp-reaper, kept in sync
with its enumerated `_TGCTL_MKDTEMP_PREFIXES` marker) and the daemon's ps-parsing/sweep loop.

The marker regression this file exists to pin: the ORIGINAL agent-tools script's marker
(`r"/tgctl-omp-(?:acp|reply)-[A-Za-z0-9]+"`) is an enumerated allowlist that missed four newer
tg-cli test-fixture prefixes (`tgctl-omp-anchor-`, `tgctl-hatch-proactive-`,
`tgctl-headless-omp-`, `tgctl-ff-omp-`) and let them leak past the reaper undetected. The fixed
regex must match every known prefix while REJECTING any other `/tgctl-*` path (an earlier
revision of the daemon widened the marker to match the whole `/tgctl-` namespace unconditionally,
which a security review found would SIGKILL any idle, reparented `omp acp` session under an
unrelated path merely containing `/tgctl-`, e.g. this feature's own worktree). The other safety
predicates (ppid == 1, age, 0% CPU, the `omp acp` token match) stay exactly as strict as before —
the marker is necessary, never sufficient.
"""

from __future__ import annotations

import pytest

from riglib import tgctl_reaper_daemon as daemon

REPARENTED_MIN_AGE_S = daemon.REPARENTED_MIN_AGE_S


def _proc(args: str, *, pid=200, ppid=1, age_s=REPARENTED_MIN_AGE_S + 1, cpu_pct=0.0) -> daemon.ProcessInfo:
    return daemon.ProcessInfo(pid=pid, ppid=ppid, age_s=age_s, cpu_pct=cpu_pct, args=args)


# ── the enumerated marker: every known prefix matches, nothing else does ──────────────────
_KNOWN_LEAKED_PREFIXES = (
    "tgctl-omp-acp-abc123",  # the original enumerated suffix
    "tgctl-omp-reply-abc123",  # the original enumerated suffix
    "tgctl-omp-anchor-abc123",  # leaked prefix #1
    "tgctl-hatch-proactive-abc123",  # leaked prefix #2 (doesn't share the tgctl-omp- stem)
    "tgctl-hatch-proactive-live-abc123",  # leaked prefix #2's hyphenated `-live-` variant
    "tgctl-headless-omp-abc123",  # leaked prefix #3 (doesn't share the tgctl-omp- stem)
    "tgctl-ff-omp-abc123",  # leaked prefix #4 (doesn't share the tgctl-omp- stem)
)


@pytest.mark.parametrize("suffix", _KNOWN_LEAKED_PREFIXES)
def test_marker_matches_every_known_leaked_prefix(suffix):
    path = f"/private/var/folders/xx/T/{suffix}/config"
    assert daemon.has_tgctl_temp_dir_marker(f"/usr/bin/omp acp --config-dir {path}")


def test_marker_rejects_a_path_with_no_tgctl_segment():
    assert not daemon.has_tgctl_temp_dir_marker("/usr/bin/omp acp --config-dir /tmp/other-xyz")


def test_marker_rejects_an_unenumerated_tgctl_path():
    """The regression this file exists to pin: `/tgctl-<anything>` must NOT match unless
    `<anything>` starts with one of the enumerated `_TGCTL_MKDTEMP_PREFIXES`. A worktree, repo
    checkout, or any other legitimate path that happens to start with `tgctl-` (e.g. this
    feature's own `.worktrees/tgctl-reaper-provisioning`) must never be mistaken for a tg-cli
    mkdtemp fixture."""
    assert not daemon.has_tgctl_temp_dir_marker(
        "/usr/bin/omp acp --cwd /Users/ultra/xp/rig-cli/.worktrees/tgctl-reaper-provisioning"
    )


# ── the marker alone cannot over-match: the other predicates still gate ────────────────────
def test_unrelated_legitimate_process_under_an_enumerated_tgctl_path_is_never_reaped():
    """A hypothetical LEGITIMATE, non-orphaned, non-`omp acp` process using one of the
    enumerated `/tgctl-<prefix>-*` paths is still never touched — is_omp_acp_argv (and every
    other predicate) is unchanged and still required. The marker is necessary but not
    sufficient.
    """
    proc = _proc("/usr/bin/some-other-tool --workdir /tmp/tgctl-omp-acp-xyz")
    assert daemon.has_tgctl_temp_dir_marker(proc.args)  # the marker DOES match the path
    assert not daemon.should_reap_orphan(proc)  # but it is not an `omp acp` invocation → never reaped


def test_an_idle_reparented_omp_acp_process_under_an_unenumerated_tgctl_path_is_never_reaped():
    """The full end-to-end regression: even with EVERY other predicate satisfied (ppid == 1,
    aged past the grace period, 0% CPU, a real `omp acp` invocation), a path that merely starts
    with `/tgctl-` but does not match an enumerated mkdtemp prefix must never be reaped. This is
    the exact shape of a legitimate long-running `omp acp` session in a worktree like
    `.worktrees/tgctl-reaper-provisioning` — never a throwaway test fixture.
    """
    proc = _proc(
        "/opt/homebrew/bin/omp acp --cwd /Users/ultra/xp/rig-cli/.worktrees/tgctl-reaper-provisioning"
    )
    assert not daemon.should_reap_orphan(proc)


def test_a_live_non_orphaned_tgctl_omp_acp_process_is_never_reaped():
    """Same marker, same `omp acp` argv, but ppid != 1 (still owned by a live tg-ctl daemon) —
    must never be reaped regardless of the marker."""
    proc = _proc("/usr/bin/omp acp --config-dir /tmp/tgctl-omp-anchor-xyz", ppid=4242)
    assert not daemon.should_reap_orphan(proc)


def test_a_too_young_orphan_is_never_reaped():
    proc = _proc("/usr/bin/omp acp --config-dir /tmp/tgctl-ff-omp-xyz", age_s=REPARENTED_MIN_AGE_S - 1)
    assert not daemon.should_reap_orphan(proc)


def test_a_busy_orphan_is_never_reaped():
    proc = _proc("/usr/bin/omp acp --config-dir /tmp/tgctl-headless-omp-xyz", cpu_pct=0.4)
    assert not daemon.should_reap_orphan(proc)


# ── the positive case: every predicate satisfied ────────────────────────────────────────────
@pytest.mark.parametrize("suffix", _KNOWN_LEAKED_PREFIXES)
def test_a_provably_orphaned_fixture_is_reaped_for_every_known_prefix(suffix):
    proc = _proc(f"/usr/bin/omp acp --config-dir /tmp/{suffix}/config")
    assert daemon.should_reap_orphan(proc)


def test_is_omp_acp_argv_rejects_bare_omp_and_omp_print():
    assert not daemon.is_omp_acp_argv("/usr/bin/omp --print")
    assert not daemon.is_omp_acp_argv("/usr/bin/omp")
    assert daemon.is_omp_acp_argv("/usr/bin/omp acp --config-dir x")


# ── ps parsing + the sweep loop, fully injected (no real subprocess) ───────────────────────
def test_parse_bsd_elapsed_time_variants():
    assert daemon._parse_bsd_elapsed_time("05:30") == 5 * 60 + 30
    assert daemon._parse_bsd_elapsed_time("01:05:30") == 3600 + 5 * 60 + 30
    assert daemon._parse_bsd_elapsed_time("2-01:05:30") == 2 * 86400 + 3600 + 5 * 60 + 30
    assert daemon._parse_bsd_elapsed_time("") is None
    assert daemon._parse_bsd_elapsed_time("garbage") is None


def _fake_list_processes(procs):
    return lambda: procs


def test_sweep_kills_the_orphan_and_reports_it():
    proc = _proc("/usr/bin/omp acp --config-dir /tmp/tgctl-omp-acp-xyz")
    killed = []
    report = daemon.sweep(
        list_processes=_fake_list_processes([proc]),
        reread_args=lambda pid: proc.args,
        kill=lambda pid: killed.append(pid),
    )
    assert killed == [proc.pid]
    assert report.reaped and report.reaped[0].pid == proc.pid
    assert report.killed_pids == [proc.pid]


def test_sweep_dry_run_never_kills():
    proc = _proc("/usr/bin/omp acp --config-dir /tmp/tgctl-omp-reply-xyz")
    killed = []
    report = daemon.sweep(
        list_processes=_fake_list_processes([proc]),
        kill=lambda pid: killed.append(pid),
        dry_run=True,
    )
    assert not killed
    assert report.dry_run is True
    assert report.reaped and report.reaped[0].pid == proc.pid


def test_sweep_skips_a_helper_still_within_grace():
    proc = _proc("/usr/bin/omp acp --config-dir /tmp/tgctl-omp-anchor-xyz", age_s=60)
    report = daemon.sweep(list_processes=_fake_list_processes([proc]))
    assert not report.reaped
    assert report.skipped and report.skipped[0].pid == proc.pid


def test_sweep_reverifies_argv_before_killing_a_race_survivor():
    """A candidate that changed argv between the initial `ps` scan and the kill decision (pid
    reuse, or the process legitimately re-exec'd) must be re-verified, not killed on stale data.
    """
    proc = _proc("/usr/bin/omp acp --config-dir /tmp/tgctl-omp-acp-xyz")
    killed = []
    report = daemon.sweep(
        list_processes=_fake_list_processes([proc]),
        reread_args=lambda pid: "/usr/bin/some-unrelated-process",
        kill=lambda pid: killed.append(pid),
    )
    assert not killed
    assert not report.reaped


def test_main_dry_run_json_reports_zero_exit(capsys):
    rc = daemon.main(["--dry-run", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"dry_run": true' in out
