"""`rig worktree gc` removes ONLY what the landed-removal verdict calls SAFE.

gc keeps its own classifier (live / prunable / dirty / merged / closed / no-pr-stale / active) but
every removal it would perform — dry-run plan and `--yes` execution alike — goes through the SAME
verdict `rig worktree remove` and the removal guard use (:mod:`riglib.removal_verdict`). The
classes below are the ones the old gc removed although the verdict calls them UNSAFE: a `closed`
PR whose commits only live on the branch being deleted, a `prunable` worktree with unlanded
commits, a `no-pr-stale` one, a landed tree hiding a nested git repository. REAL git throughout
(`tests/removal_helpers.py`: tmp bare origin + primary clone + linked worktrees); the forge, the
PR index and the process scanner are injected fakes. Every negative asserts the worktree AND its
branch are still there and that no flag (`--yes`, `--include-stale`) changes that.
"""

from __future__ import annotations

import datetime
import shutil
import subprocess
from pathlib import Path

import pytest
from removal_helpers import FakeForge, Sandbox, forge_down, git, keep_on, no_processes, superseded

from riglib import removal_verdict as rv
from riglib import worktree_gc
from riglib.cli import main


def _no_live(path: Path) -> bool:
    return False


def _future_iso() -> str:
    return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)).isoformat()


def _lookup(**by_branch: worktree_gc.PrInfo):
    return lambda branch: by_branch.get(branch)


def _merged(number: int = 7) -> worktree_gc.PrInfo:
    return worktree_gc.PrInfo(number=number, state="MERGED", merged_at=_future_iso())


def _closed(number: int = 8) -> worktree_gc.PrInfo:
    return worktree_gc.PrInfo(number=number, state="CLOSED", closed_at=_future_iso())


@pytest.fixture
def sb(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


def _gc(sb: Sandbox, pr_lookup, *, yes: bool = True, dry_run: bool = False, include_stale: bool = False):
    return worktree_gc.run_gc(
        sb.primary,
        dry_run=dry_run,
        yes=yes,
        include_stale=include_stale,
        liveness_check=_no_live,
        pr_lookup=pr_lookup,
        forge=FakeForge([]),
        process_cwds=no_processes,
    )


def _branches(sb: Sandbox) -> set[str]:
    return set(git(sb.primary, "for-each-ref", "--format=%(refname:short)", "refs/heads").split())


def _landed_worktree(sb: Sandbox, name: str = "feat") -> Path:
    wt = sb.worktree(name)
    sb.commit(wt, f"{name}.txt", f"{name}\n", f"feat {name}")
    sb.push(wt, name)
    sb.land_ff(name)
    sb.fetch()
    return wt


# ── positive controls: SAFE still removes (worktree AND branch) ──────────────────
def test_landed_merged_worktree_is_removed_with_its_branch(sb):
    wt = _landed_worktree(sb)

    report = _gc(sb, _lookup(feat=_merged()))

    assert report.entries[0].classification == "merged"
    assert report.entries[0].removed is True
    assert not wt.exists()
    assert "feat" not in _branches(sb)


def test_closed_pr_whose_commits_were_archived_is_removed(sb):
    wt, _tip = superseded(sb, "sup")
    keep_on(sb, wt, "archive/sup-1")

    report = _gc(sb, _lookup(sup=_closed()))

    assert report.entries[0].classification == "closed"
    assert report.entries[0].removed is True
    assert not wt.exists()
    assert "sup" not in _branches(sb)


# ── UNSAFE per the verdict is never removed, whatever the flags ──────────────────
def test_closed_pr_with_commits_only_on_the_deleted_branch_is_kept(sb):
    wt, tip = superseded(sb, "sup")

    report = _gc(sb, _lookup(sup=_closed()))

    entry = report.entries[0]
    assert entry.classification == "closed"
    assert entry.removed is False
    assert entry.remove_error is None
    assert entry.skipped_reason is not None and "UNSAFE" in entry.skipped_reason
    assert "not-landed" in entry.skipped_reason
    assert wt.exists()
    assert "sup" in _branches(sb)
    assert git(wt, "rev-parse", "HEAD") == tip
    assert "kept" in worktree_gc.render_report(report)


def test_prunable_worktree_with_unlanded_commits_keeps_its_branch(sb):
    wt, tip = superseded(sb, "pr")
    shutil.rmtree(wt)  # deleted by hand; git still has it registered

    report = _gc(sb, _lookup())

    entry = report.entries[0]
    assert entry.classification == "prunable"
    assert entry.removed is False
    assert entry.skipped_reason is not None and "UNSAFE" in entry.skipped_reason
    assert "pr" in _branches(sb)
    assert git(sb.primary, "rev-parse", "refs/heads/pr") == tip
    assert str(wt) in git(sb.primary, "worktree", "list", "--porcelain")


def test_prunable_worktree_with_landed_commits_is_still_removed(sb):
    wt = _landed_worktree(sb, "gone")
    shutil.rmtree(wt)

    report = _gc(sb, _lookup())

    assert report.entries[0].classification == "prunable"
    assert report.entries[0].removed is True
    assert "gone" not in _branches(sb)


def test_no_pr_stale_with_unlanded_commits_is_kept_even_with_include_stale(sb, monkeypatch):
    wt, _tip = superseded(sb, "old")
    long_ago = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=400)
    monkeypatch.setattr(worktree_gc, "_last_activity_utc", lambda path: long_ago)

    report = _gc(sb, _lookup(), include_stale=True)

    entry = report.entries[0]
    assert entry.classification == "no-pr-stale"
    assert entry.removed is False
    assert entry.skipped_reason is not None and "UNSAFE" in entry.skipped_reason
    assert wt.exists()
    assert "old" in _branches(sb)


def test_landed_worktree_hiding_a_nested_git_repository_is_kept(sb):
    wt = sb.worktree("nest")
    sb.commit(wt, ".gitignore", "vendor/\n", "ignore vendor")
    sb.push(wt, "nest")
    sb.land_ff("nest")
    sb.fetch()
    nested = wt / "vendor" / "lib"
    nested.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(nested)], check=True)
    (nested / "work.txt").write_text("only copy of this work\n", encoding="utf-8")

    report = _gc(sb, _lookup(nest=_merged()))

    entry = report.entries[0]
    assert entry.classification == "merged"  # gc's own classifier sees a clean tree...
    assert entry.removed is False  # ...the verdict knows better
    assert entry.skipped_reason is not None and "UNSAFE" in entry.skipped_reason
    assert (nested / "work.txt").exists()
    assert "nest" in _branches(sb)


def test_a_branch_swapped_between_plan_and_removal_is_not_deleted_on_the_new_tree_verdict(sb):
    """The worktree is re-pointed at a LANDED commit (detached) after planning, so the worktree
    verdict alone would say SAFE; the planned branch still holds unlanded work and must survive."""
    wt, tip = superseded(sb, "swap")
    calls = {"n": 0}

    def liveness(path: Path) -> bool:
        calls["n"] += 1
        if calls["n"] == 2:  # the pre-removal recheck: someone checks out landed history
            git(wt, "checkout", "-q", "--detach", "origin/main")
        return False

    report = worktree_gc.run_gc(
        sb.primary,
        dry_run=False,
        yes=True,
        liveness_check=liveness,
        pr_lookup=_lookup(swap=_closed()),
        forge=FakeForge([]),
        process_cwds=no_processes,
    )

    entry = report.entries[0]
    assert entry.removed is False
    assert git(sb.primary, "rev-parse", "refs/heads/swap") == tip
    assert wt.exists()


def test_a_commit_made_after_planning_is_not_removed(sb):
    wt = _landed_worktree(sb)
    calls = {"n": 0}

    def liveness(path: Path) -> bool:
        calls["n"] += 1
        if calls["n"] == 2:  # between plan and removal: a new local-only commit
            sb.commit(wt, "late.txt", "late\n", "late work")
        return False

    report = worktree_gc.run_gc(
        sb.primary,
        dry_run=False,
        yes=True,
        liveness_check=liveness,
        pr_lookup=_lookup(feat=_merged()),
        forge=FakeForge([]),
        process_cwds=no_processes,
    )

    assert report.entries[0].removed is False
    assert (wt / "late.txt").exists()
    assert "feat" in _branches(sb)


# ── the dry-run plan never promises a removal the verdict would refuse ───────────
def test_dry_run_does_not_promise_an_unsafe_removal(sb):
    safe_wt = _landed_worktree(sb, "safe")
    unsafe_wt, _tip = superseded(sb, "unsafe")

    report = _gc(sb, _lookup(safe=_merged(), unsafe=_closed(9)), yes=False, dry_run=True)

    by_name = {e.path.name: e for e in report.entries}
    assert by_name["safe"].plan_removable is True
    assert by_name["unsafe"].plan_removable is False
    assert by_name["unsafe"].skipped_reason is not None and "UNSAFE" in by_name["unsafe"].skipped_reason
    text = worktree_gc.render_report(report)
    assert text.count("would be removed") == 1
    assert safe_wt.exists() and unsafe_wt.exists()
    assert report.total_reclaimable_bytes == by_name["safe"].size_bytes


def test_one_unsafe_entry_does_not_stop_the_safe_ones(sb):
    safe_wt = _landed_worktree(sb, "safe")
    unsafe_wt, _tip = superseded(sb, "unsafe")

    report = _gc(sb, _lookup(safe=_merged(), unsafe=_closed(9)))

    by_name = {e.path.name: e for e in report.entries}
    assert by_name["safe"].removed is True and not safe_wt.exists()
    assert by_name["unsafe"].removed is False and unsafe_wt.exists()


# ── fail closed ──────────────────────────────────────────────────────────────────
def test_no_resolvable_default_branch_means_nothing_is_removed(sb):
    wt = _landed_worktree(sb)
    git(sb.primary, "remote", "set-head", "origin", "-d")

    report = _gc(sb, _lookup(feat=_merged()))

    assert report.entries[0].removed is False
    assert report.entries[0].skipped_reason is not None and "UNSAFE" in report.entries[0].skipped_reason
    assert wt.exists()


def test_a_running_process_in_the_worktree_blocks_removal(sb):
    wt = _landed_worktree(sb)

    report = worktree_gc.run_gc(
        sb.primary,
        dry_run=False,
        yes=True,
        liveness_check=_no_live,
        pr_lookup=_lookup(feat=_merged()),
        forge=FakeForge([]),
        process_cwds=lambda: [wt / "src"],
    )

    assert report.entries[0].removed is False
    assert "live-process" in (report.entries[0].skipped_reason or "")
    assert wt.exists()


def test_forge_is_asked_exactly_once_per_run(sb):
    for name in ("a", "b", "c"):
        wt = sb.worktree(name)
        sb.commit(wt, f"{name}.txt", f"{name}\n", f"feat {name}")
        sb.push(wt, name)
    forge = FakeForge([])

    worktree_gc.run_gc(
        sb.primary,
        dry_run=False,
        yes=True,
        liveness_check=_no_live,
        pr_lookup=_lookup(a=_closed(1), b=_closed(2), c=_closed(3)),
        forge=forge,
        process_cwds=no_processes,
    )

    assert forge.calls == 1


def test_a_failing_forge_is_not_asked_again_for_every_nominee(sb):
    for name in ("a", "b", "c"):
        wt = sb.worktree(name)
        sb.commit(wt, f"{name}.txt", f"{name}\n", f"feat {name}")
        sb.push(wt, name)
    down = forge_down()

    report = worktree_gc.run_gc(
        sb.primary,
        dry_run=False,
        yes=True,
        liveness_check=_no_live,
        pr_lookup=_lookup(a=_closed(1), b=_closed(2), c=_closed(3)),
        forge=down,
        process_cwds=no_processes,
    )

    assert down.calls == 1
    assert [e.removed for e in report.entries] == [False, False, False]


def test_a_branch_that_moves_after_the_verdict_is_left_in_place(sb, monkeypatch):
    """Worktree removal can take a while on a big tree; a commit landing on the branch meanwhile was
    never judged, so `branch -D` must not delete it."""
    wt = _landed_worktree(sb)
    real = worktree_gc._RemovalGate.unsafe_reason

    def judge_then_move(self, entry):
        reason = real(self, entry)
        if reason is None:
            tree = git(sb.primary, "rev-parse", "origin/main^{tree}")
            late = git(sb.primary, "commit-tree", tree, "-p", git(sb.primary, "rev-parse", "feat"), "-m", "late")
            git(sb.primary, "update-ref", "refs/heads/feat", late)
        return reason

    monkeypatch.setattr(worktree_gc._RemovalGate, "unsafe_reason", judge_then_move)

    report = worktree_gc.run_gc(
        sb.primary,
        dry_run=False,
        yes=True,
        liveness_check=_no_live,
        pr_lookup=_lookup(feat=_merged()),
        forge=FakeForge([]),
        process_cwds=no_processes,
    )

    entry = report.entries[0]
    assert entry.remove_error is not None and "moved" in entry.remove_error
    assert "feat" in _branches(sb)
    assert not wt.exists()


# ── CLI: --yes / --include-stale are not an override ─────────────────────────────
def test_cli_yes_and_include_stale_do_not_override_the_verdict(sb, monkeypatch, capsys):
    wt, _tip = superseded(sb, "sup")
    long_ago = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=400)
    monkeypatch.setattr(worktree_gc, "_last_activity_utc", lambda path: long_ago)
    monkeypatch.setattr(worktree_gc, "_default_liveness_factory", lambda: _no_live)
    monkeypatch.setattr(worktree_gc, "make_default_pr_lookup", lambda repo_root: _lookup(sup=_closed()))
    monkeypatch.setattr(rv, "default_forge_lookup", FakeForge([]))
    monkeypatch.setattr(rv, "all_process_cwds", no_processes)

    rc = main(["worktree", "gc", "--repo", str(sb.primary), "--yes", "--include-stale"])

    out = capsys.readouterr().out
    assert rc == 0
    assert wt.exists()
    assert "sup" in _branches(sb)
    assert "UNSAFE" in out


def test_origin_default_is_refreshed_so_a_just_merged_branch_can_be_removed(sb):
    """The tracking ref is stale (the merge happened forge-side a moment ago): without the single
    per-run refresh the verdict would call a landed branch unlanded."""
    wt = sb.worktree("fresh")
    sb.commit(wt, "fresh.txt", "fresh\n", "feat fresh")
    sb.push(wt, "fresh")
    sb.land_ff("fresh")  # NO sb.fetch(): the primary's origin/main does not contain it yet

    report = _gc(sb, _lookup(fresh=_merged()))

    assert report.entries[0].removed is True
    assert not wt.exists()


def test_a_crashing_verdict_keeps_the_worktree(sb, monkeypatch):
    wt = _landed_worktree(sb)

    def boom(*args, **kwargs):
        raise RuntimeError("verdict exploded")

    monkeypatch.setattr(rv, "worktree_verdict", boom)

    report = _gc(sb, _lookup(feat=_merged()))

    assert report.entries[0].removed is False
    assert "verdict-error" in (report.entries[0].skipped_reason or "")
    assert wt.exists()
    assert "feat" in _branches(sb)


def test_a_branch_that_disappears_after_the_verdict_is_reported_not_crashed_on(sb, monkeypatch):
    wt = _landed_worktree(sb)
    real = worktree_gc._RemovalGate.unsafe_reason

    def judge_then_delete_the_branch(self, entry):
        reason = real(self, entry)
        if reason is None:
            git(sb.primary, "update-ref", "-d", "refs/heads/feat")
        return reason

    monkeypatch.setattr(worktree_gc._RemovalGate, "unsafe_reason", judge_then_delete_the_branch)

    report = worktree_gc.run_gc(
        sb.primary,
        dry_run=False,
        yes=True,
        liveness_check=_no_live,
        pr_lookup=_lookup(feat=_merged()),
        forge=FakeForge([]),
        process_cwds=no_processes,
    )

    assert report.entries[0].remove_error is not None and "left in place" in report.entries[0].remove_error
    assert not wt.exists()


def test_deleting_the_judged_branch_drops_its_config_like_branch_dash_d_does(sb):
    _landed_worktree(sb)
    git(sb.primary, "config", "branch.feat.remote", "origin")
    git(sb.primary, "config", "branch.feat.merge", "refs/heads/feat")

    report = _gc(sb, _lookup(feat=_merged()))

    assert report.entries[0].removed is True and report.entries[0].remove_error is None
    assert "feat" not in _branches(sb)
    assert git(sb.primary, "config", "--get-regexp", r"^branch\.feat\.", check=False) == ""


def test_a_branch_checked_out_in_another_worktree_after_the_verdict_is_not_deleted(sb, monkeypatch):
    """`git update-ref -d` does not refuse a checked-out branch the way `git branch -D` does."""
    wt = _landed_worktree(sb)
    other = sb.wt_dir / "other"
    real = worktree_gc._RemovalGate.unsafe_reason

    def judge_then_check_out(self, entry):
        reason = real(self, entry)
        if reason is None:
            git(wt, "checkout", "-q", "--detach")  # free the branch from this worktree...
            git(sb.primary, "worktree", "add", "-q", str(other), "feat")  # ...and check it out elsewhere
        return reason

    monkeypatch.setattr(worktree_gc._RemovalGate, "unsafe_reason", judge_then_check_out)
    report = _gc(sb, _lookup(feat=_merged()))

    assert "checked out in" in (report.entries[0].remove_error or "")
    assert "feat" in _branches(sb)
    assert other.exists()


def test_a_squash_merged_worktree_is_removed(sb):
    """Squash-merged: never an ancestor of origin/main, landed by content (the verdict's content proof)."""
    wt = sb.worktree("sq")
    sb.commit(wt, "sq.txt", "sq\n", "feat sq")
    sb.push(wt, "sq")
    sb.land_squash("sq")
    sb.fetch()

    report = _gc(sb, _lookup(sq=_merged()))

    assert report.entries[0].removed is True
    assert not wt.exists()
    assert "sq" not in _branches(sb)

