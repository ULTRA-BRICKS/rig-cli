"""The landed-removal verdict: "is deleting this worktree / local branch / remote branch guaranteed
to lose nothing?" — SAFE only when the target is clean AND every commit unique to it is LANDED on
origin/<default> (ancestor / merged-PR-with-matching-head / content no-op) or still held by another
live remote branch (preserved), proven, never assumed.

Real git throughout (tmp bare origin, clones, linked worktrees); the forge is a fake PR list. Every
NEGATIVE case below is a data-loss scenario that must stay UNSAFE.
"""

from __future__ import annotations

import random
import subprocess
import sys
import time
from pathlib import Path

import pytest
from removal_helpers import FakeForge, Sandbox, forge_down, git, keep_on, no_processes, pr, superseded

from riglib import removal_verdict as rv


@pytest.fixture
def sb(tmp_path) -> Sandbox:
    return Sandbox(tmp_path)


def wt_verdict(sb: Sandbox, wt: Path, forge=None, **kw) -> rv.Verdict:
    return rv.worktree_verdict(
        sb.primary, wt, forge=forge or FakeForge(), process_cwds=kw.pop("process_cwds", no_processes), **kw
    )


def codes(v: rv.Verdict) -> set[str]:
    return {r.code for r in v.reasons}


# ── POSITIVE: landed + clean ─────────────────────────────────────────────────────
def test_fresh_worktree_at_default_tip_is_safe(sb):
    wt = sb.worktree("fresh")
    v = wt_verdict(sb, wt)
    assert v.safe, v.render()
    assert v.landed_by == "ancestor"


def test_branch_merged_by_fast_forward_is_safe(sb):
    wt = sb.worktree("ff")
    sb.commit(wt, "a.txt", "a\n", "feat a")
    sb.land_ff("ff")
    sb.fetch()
    v = wt_verdict(sb, wt)
    assert v.safe, v.render()
    assert v.landed_by == "ancestor"


def test_branch_merged_by_merge_commit_is_safe(sb):
    wt = sb.worktree("mc")
    sb.commit(wt, "a.txt", "a\n", "feat a")
    sb.land_merge_commit("mc")
    sb.fetch()
    assert wt_verdict(sb, wt).safe


def test_squash_merged_pr_with_tip_equal_to_pr_head_is_safe(sb):
    wt = sb.worktree("sq")
    tip = sb.commit(wt, "a.txt", "a\n", "feat a")
    sb.commit(wt, "b.txt", "b\n", "feat b")
    tip = git(wt, "rev-parse", "HEAD")
    squash = sb.land_squash("sq")
    sb.advance_main()  # main moved on: content proof would still hold, but the PR proof must win
    sb.fetch()
    forge = FakeForge([pr("sq", head_oid=tip, merge_oid=squash)])
    v = wt_verdict(sb, wt, forge)
    assert v.safe, v.render()
    assert v.landed_by == "pr-merged"
    assert forge.calls == 1


def test_squash_merged_pr_with_tip_behind_pr_head_is_safe(sb):
    """The local tip may be an ANCESTOR of the PR's recorded head (the PR got more commits)."""
    wt = sb.worktree("sq2")
    sb.commit(wt, "a.txt", "a\n", "feat a")
    old_tip = git(wt, "rev-parse", "HEAD")
    sb.commit(wt, "b.txt", "b\n", "feat b")
    pr_head = git(wt, "rev-parse", "HEAD")
    squash = sb.land_squash("sq2")
    sb.advance_main("a.txt", "changed later\n")  # content proof fails; only the PR proof applies
    sb.fetch()
    git(wt, "reset", "-q", "--hard", old_tip)
    v = wt_verdict(sb, wt, FakeForge([pr("sq2", head_oid=pr_head, merge_oid=squash)]))
    assert v.safe, v.render()
    assert v.landed_by == "pr-merged"


def test_rebase_merged_is_safe_via_cherry_even_when_main_moved_on(sb):
    wt = sb.worktree("rb")
    sb.commit(wt, "a.txt", "a\n", "feat a")
    sb.commit(wt, "b.txt", "b\n", "feat b")
    sb.land_rebase("rb")
    sb.advance_main("a.txt", "a changed upstream\n")
    sb.fetch()
    v = wt_verdict(sb, wt)  # no PR at all
    assert v.safe, v.render()
    assert v.landed_by in {"cherry", "content-noop"}


def test_no_pr_but_merge_tree_is_a_noop_is_safe(sb):
    wt = sb.worktree("noop")
    sb.commit(wt, "a.txt", "same\n", "feat a on branch")
    # the SAME content lands on main through an unrelated commit (no patch-id match needed)
    sb.advance_main("a.txt", "same\n")
    sb.fetch()
    v = wt_verdict(sb, wt)
    assert v.safe, v.render()
    assert v.landed_by == "content-noop"


def test_clean_detached_head_at_a_landed_commit_is_safe(sb):
    wt = sb.worktree("det", detach=True)
    v = wt_verdict(sb, wt)
    assert v.safe, v.render()


def test_ignored_only_files_do_not_block(sb):
    (sb.primary / ".gitignore").write_text("*.log\n")
    git(sb.primary, "add", ".gitignore")
    git(sb.primary, "commit", "-q", "-m", "ignore logs")
    git(sb.primary, "push", "-q", "origin", "main")
    git(sb.primary, "fetch", "-q")
    wt = sb.worktree("ign")
    (wt / "debug.log").write_text("noise\n")
    v = wt_verdict(sb, wt)
    assert v.safe, v.render()


def test_local_branch_landed_is_safe(sb):
    wt = sb.worktree("lb")
    sb.commit(wt, "a.txt", "a\n", "feat")
    sb.land_ff("lb")
    sb.fetch()
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    v = rv.branch_verdict(sb.primary, "lb", forge=FakeForge())
    assert v.safe, v.render()


def test_remote_branch_landed_is_safe(sb):
    wt = sb.worktree("rbr")
    sb.commit(wt, "a.txt", "a\n", "feat")
    sb.push(wt, "rbr")
    sb.land_ff("rbr")
    sb.fetch()
    v = rv.remote_branch_verdict(sb.primary, "origin", "rbr", forge=FakeForge())
    assert v.safe, v.render()


# ── NEGATIVE: dirty worktree ─────────────────────────────────────────────────────
def test_modified_tracked_file_is_unsafe(sb):
    wt = sb.worktree("m")
    (wt / "README.md").write_text("edited\n")
    v = wt_verdict(sb, wt)
    assert not v.safe and "dirty-worktree" in codes(v)


def test_staged_file_is_unsafe(sb):
    wt = sb.worktree("s")
    (wt / "new.txt").write_text("n\n")
    git(wt, "add", "new.txt")
    v = wt_verdict(sb, wt)
    assert not v.safe and "dirty-worktree" in codes(v)


def test_untracked_file_is_unsafe(sb):
    wt = sb.worktree("u")
    (wt / "notes.md").write_text("n\n")
    v = wt_verdict(sb, wt)
    assert not v.safe and "dirty-worktree" in codes(v)


def test_untracked_file_in_new_directory_is_unsafe_even_if_status_hides_untracked(sb):
    """`status.showUntrackedFiles=no` in the repo config must not blind the check."""
    git(sb.primary, "config", "status.showUntrackedFiles", "no")
    wt = sb.worktree("u2")
    (wt / "deep" / "er").mkdir(parents=True)
    (wt / "deep" / "er" / "x.txt").write_text("x\n")
    v = wt_verdict(sb, wt)
    assert not v.safe and "dirty-worktree" in codes(v)


def test_dirty_submodule_style_change_is_unsafe_when_status_fails(sb, monkeypatch):
    wt = sb.worktree("sf")
    real_run = subprocess.run

    def broken(cmd, *a, **kw):
        if "status" in cmd:
            raise subprocess.TimeoutExpired(cmd, 1)
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(rv.subprocess, "run", broken)
    v = wt_verdict(sb, wt)
    assert not v.safe and "status-failed" in codes(v)


# ── NEGATIVE: unlanded commits ───────────────────────────────────────────────────
def test_unpushed_unmerged_commit_is_unsafe(sb):
    wt = sb.worktree("un")
    sb.commit(wt, "a.txt", "a\n", "unpushed work")
    v = wt_verdict(sb, wt)
    assert not v.safe and "not-landed" in codes(v)


def test_pushed_but_unmerged_commit_is_still_unsafe(sb):
    """Pushing is irrelevant to safety — the branch ref is what deletion removes."""
    wt = sb.worktree("pushed")
    sb.commit(wt, "a.txt", "a\n", "pushed work")
    sb.push(wt, "pushed")
    sb.fetch()
    v = wt_verdict(sb, wt)
    assert not v.safe and "not-landed" in codes(v)


def test_squash_merged_but_extra_local_commit_after_pr_head_is_unsafe(sb):
    wt = sb.worktree("extra")
    sb.commit(wt, "a.txt", "a\n", "feat a")
    pr_head = git(wt, "rev-parse", "HEAD")
    squash = sb.land_squash("extra")
    sb.advance_main("a.txt", "diverged\n")
    sb.fetch()
    sb.commit(wt, "late.txt", "late\n", "commit AFTER the PR head")
    v = wt_verdict(sb, wt, FakeForge([pr("extra", head_oid=pr_head, merge_oid=squash)]))
    assert not v.safe and "not-landed" in codes(v)


def test_pr_closed_not_merged_is_unsafe(sb):
    wt = sb.worktree("closed")
    tip = sb.commit(wt, "a.txt", "a\n", "feat")
    v = wt_verdict(sb, wt, FakeForge([pr("closed", state="CLOSED", head_oid=tip, merge_oid=None)]))
    assert not v.safe and "not-landed" in codes(v)


def test_pr_open_is_unsafe(sb):
    wt = sb.worktree("open")
    tip = sb.commit(wt, "a.txt", "a\n", "feat")
    v = wt_verdict(sb, wt, FakeForge([pr("open", state="OPEN", head_oid=tip, merge_oid=None)]))
    assert not v.safe


def test_pr_merged_but_merge_commit_absent_from_origin_default_is_unsafe(sb):
    wt = sb.worktree("absent")
    tip = sb.commit(wt, "a.txt", "a\n", "feat")
    squash = sb.land_squash("absent")  # lands on the server, but origin/main is NOT fetched
    v = wt_verdict(sb, wt, FakeForge([pr("absent", head_oid=tip, merge_oid=squash)]))
    assert not v.safe and "not-landed" in codes(v)
    # after a fetch the same record proves it
    sb.fetch()
    assert wt_verdict(sb, wt, FakeForge([pr("absent", head_oid=tip, merge_oid=squash)])).safe


def test_pr_merged_with_a_fabricated_merge_oid_is_unsafe(sb):
    wt = sb.worktree("fab")
    tip = sb.commit(wt, "a.txt", "a\n", "feat")
    v = wt_verdict(sb, wt, FakeForge([pr("fab", head_oid=tip, merge_oid="0" * 40)]))
    assert not v.safe


def test_pr_merged_into_a_non_default_base_is_unsafe(sb):
    wt = sb.worktree("base")
    sb.commit(wt, "a.txt", "a\n", "feat a")
    tip = sb.commit(wt, "b.txt", "b\n", "feat b")  # 2 commits: a squash has no patch-id twin
    squash = sb.land_squash("base")
    sb.advance_main("a.txt", "diverged\n")  # content proof cannot apply either
    sb.fetch()
    v = wt_verdict(sb, wt, FakeForge([pr("base", head_oid=tip, merge_oid=squash, base="release-1")]))
    assert not v.safe


def test_pr_record_without_head_oid_is_not_trusted(sb):
    wt = sb.worktree("nohead")
    sb.commit(wt, "a.txt", "a\n", "feat a")
    sb.commit(wt, "b.txt", "b\n", "feat b")
    squash = sb.land_squash("nohead")
    sb.advance_main("a.txt", "diverged\n")
    sb.fetch()
    v = wt_verdict(sb, wt, FakeForge([pr("nohead", head_oid=None, merge_oid=squash)]))
    assert not v.safe


def test_forge_error_with_content_not_on_default_is_unsafe(sb):
    wt = sb.worktree("fe")
    sb.commit(wt, "a.txt", "a\n", "feat")
    v = wt_verdict(sb, wt, forge_down())
    assert not v.safe
    assert "not-landed" in codes(v)
    assert any("forge" in n.lower() for n in v.notes)


def test_forge_error_is_irrelevant_when_the_tip_is_an_ancestor(sb):
    wt = sb.worktree("fe2")
    sb.commit(wt, "a.txt", "a\n", "feat")
    sb.land_ff("fe2")
    sb.fetch()
    forge = forge_down()
    v = wt_verdict(sb, wt, forge)
    assert v.safe and v.landed_by == "ancestor"
    assert forge.calls == 0  # proofs are tried in order; the cheap local one wins


def test_no_pr_and_content_not_on_default_is_unsafe(sb):
    wt = sb.worktree("nopr")
    sb.commit(wt, "a.txt", "a\n", "feat")
    sb.advance_main("zzz.txt", "other\n")
    sb.fetch()
    v = wt_verdict(sb, wt)
    assert not v.safe and "not-landed" in codes(v)


def test_detached_head_with_unique_commit_is_unsafe(sb):
    wt = sb.worktree("detu", detach=True)
    sb.commit(wt, "a.txt", "a\n", "only in the reflog")
    v = wt_verdict(sb, wt)
    assert not v.safe and "not-landed" in codes(v)


def test_stash_is_never_relied_upon(sb):
    wt = sb.worktree("stash")
    (wt / "README.md").write_text("stashed edit\n")
    git(wt, "stash", "push", "-q")
    sb.commit(wt, "a.txt", "a\n", "unmerged")
    v = wt_verdict(sb, wt)
    assert not v.safe


# ── NEGATIVE: worktree state ─────────────────────────────────────────────────────
def test_merge_in_progress_is_unsafe_even_with_a_clean_status(sb):
    wt = sb.worktree("mip")
    other = sb.worktree("mip-other")
    sb.commit(other, "o.txt", "o\n", "other")
    git(wt, "merge", "-q", "-s", "ours", "--no-commit", "mip-other")
    assert git(wt, "status", "--porcelain") == ""  # sanity: this is the "clean but mid-merge" state
    v = wt_verdict(sb, wt)
    assert not v.safe and "operation-in-progress" in codes(v)


@pytest.mark.parametrize(
    "marker", ["CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG", "rebase-merge", "rebase-apply", "sequencer"]
)
def test_other_in_progress_operations_are_unsafe(sb, marker):
    wt = sb.worktree(f"op-{marker.lower().replace('_', '-')}")
    gitdir = Path(git(wt, "rev-parse", "--absolute-git-dir"))
    target = gitdir / marker
    if marker in {"rebase-merge", "rebase-apply", "sequencer"}:
        target.mkdir()
    else:
        target.write_text("0" * 40 + "\n")
    v = wt_verdict(sb, wt)
    assert not v.safe and "operation-in-progress" in codes(v)


def test_branch_checked_out_in_another_worktree_is_unsafe(sb):
    wt = sb.worktree("elsewhere")
    v = rv.branch_verdict(sb.primary, "elsewhere", forge=FakeForge())
    assert not v.safe and "branch-checked-out" in codes(v)
    assert wt.exists()


def test_target_is_default_branch_is_unsafe(sb):
    v = rv.branch_verdict(sb.primary, "main", forge=FakeForge())
    assert not v.safe and "default-branch" in codes(v)


def test_remote_default_branch_is_unsafe(sb):
    v = rv.remote_branch_verdict(sb.primary, "origin", "main", forge=FakeForge())
    assert not v.safe and "default-branch" in codes(v)


def test_primary_checkout_is_unsafe(sb):
    v = wt_verdict(sb, sb.primary)
    assert not v.safe and "primary-checkout" in codes(v)


def test_worktree_on_the_default_branch_name_is_unsafe(sb):
    wt = sb.worktree("detmain", detach=True)
    git(wt, "checkout", "-q", "--ignore-other-worktrees", "-B", "main")
    v = wt_verdict(sb, wt)
    assert not v.safe


def test_not_a_registered_worktree_is_unsafe(sb, tmp_path):
    stray = tmp_path / "stray"
    stray.mkdir()
    v = wt_verdict(sb, stray)
    assert not v.safe and "unknown-worktree" in codes(v)


def test_live_process_with_worktree_as_cwd_is_unsafe_with_the_real_scanner(sb):
    wt = sb.worktree("live")
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], cwd=wt)
    try:
        time.sleep(0.2)
        v = rv.worktree_verdict(sb.primary, wt, forge=FakeForge())  # default process scanner
        assert not v.safe and "live-process" in codes(v)
    finally:
        proc.kill()
        proc.wait()


def test_live_process_in_a_subdirectory_counts(sb):
    wt = sb.worktree("livesub")
    (wt / "sub").mkdir()
    v = wt_verdict(sb, wt, process_cwds=lambda: [wt / "sub"])
    assert not v.safe and "live-process" in codes(v)


def test_untrusted_process_scan_fails_closed(sb):
    wt = sb.worktree("scan")
    v = wt_verdict(sb, wt, process_cwds=lambda: None)
    assert not v.safe and "live-process-unknown" in codes(v)


# ── NEGATIVE: remote branch ──────────────────────────────────────────────────────
def test_remote_tip_ahead_of_what_landed_is_unsafe(sb):
    wt = sb.worktree("ahead")
    sb.commit(wt, "a.txt", "a\n", "feat")
    sb.push(wt, "ahead")
    sb.land_ff("ahead")
    sb.fetch()
    # somebody pushes MORE to the remote branch after it landed; our local tracking ref is stale
    git(sb.integrator, "fetch", "-q", "origin")
    git(sb.integrator, "checkout", "-q", "-B", "ahead", "origin/ahead")
    sb.commit(sb.integrator, "more.txt", "more\n", "late push")
    git(sb.integrator, "push", "-q", "origin", "ahead")
    v = rv.remote_branch_verdict(sb.primary, "origin", "ahead", forge=FakeForge())
    assert not v.safe and "not-landed" in codes(v)


def test_remote_branch_that_does_not_exist_is_unsafe(sb):
    v = rv.remote_branch_verdict(sb.primary, "origin", "ghost", forge=FakeForge())
    assert not v.safe


def test_remote_lookup_failure_is_unsafe(sb):
    git(sb.primary, "remote", "set-url", "origin", str(sb.root / "does-not-exist.git"))
    v = rv.remote_branch_verdict(sb.primary, "origin", "whatever", forge=FakeForge())
    assert not v.safe


# ── NEGATIVE: default-ref resolution ─────────────────────────────────────────────
def test_missing_origin_default_ref_is_unsafe(sb):
    wt = sb.worktree("noref")
    git(sb.primary, "update-ref", "-d", "refs/remotes/origin/main")
    v = wt_verdict(sb, wt)
    assert not v.safe and "no-default-ref" in codes(v)


def test_unresolvable_origin_head_is_unsafe(sb):
    wt = sb.worktree("nohead2")
    git(sb.primary, "remote", "set-head", "origin", "-d")
    v = wt_verdict(sb, wt)
    assert not v.safe and "no-default-ref" in codes(v)


def test_stale_origin_default_does_not_know_the_landing_so_is_unsafe(sb):
    wt = sb.worktree("stale")
    sb.commit(wt, "a.txt", "a\n", "feat")
    sb.land_ff("stale")  # origin moved, local origin/main is stale (no fetch)
    v = wt_verdict(sb, wt)
    assert not v.safe and "not-landed" in codes(v)


def test_fetch_option_refreshes_a_stale_default_ref(sb):
    wt = sb.worktree("refresh")
    sb.commit(wt, "a.txt", "a\n", "feat")
    sb.land_ff("refresh")
    v = wt_verdict(sb, wt, fetch=True)
    assert v.safe, v.render()


def test_fetch_failure_is_not_fatal_but_is_recorded(sb):
    wt = sb.worktree("fetchfail")
    git(sb.primary, "remote", "set-url", "origin", str(sb.root / "gone.git"))
    v = wt_verdict(sb, wt, fetch=True)
    # tip == main tip is provably landed against the refs we have; the stale-ness is only noted
    assert v.safe
    assert any("fetch" in n.lower() for n in v.notes)


# ── branch names ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", ["-D", "--force", "a b", "x\ny", "a..b", "a~1", "", "refs/heads/main", "@{-1}"])
def test_invalid_or_option_looking_branch_names_are_unsafe(sb, name):
    v = rv.branch_verdict(sb.primary, name, forge=FakeForge())
    assert not v.safe and "invalid-branch-name" in codes(v)


def test_branch_with_shell_metacharacters_is_judged_on_its_content_not_executed(sb, tmp_path):
    marker = tmp_path / "pwned"
    name = f"feat;touch {marker}".replace(" ", "_")  # legal ref name; the ';' is just a character
    wt = sb.worktree("meta", branch=name)
    sb.commit(wt, "a.txt", "a\n", "unmerged")
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    v = rv.branch_verdict(sb.primary, name, forge=FakeForge())
    assert not v.safe and "not-landed" in codes(v)
    assert not marker.exists()


def test_branch_with_metacharacters_that_is_landed_is_safe(sb):
    name = "feat$(x);`y`"
    wt = sb.worktree("meta2", branch=name)
    sb.commit(wt, "a.txt", "a\n", "feat")
    sb.land_ff(name)
    sb.fetch()
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    assert rv.branch_verdict(sb.primary, name, forge=FakeForge()).safe


def test_nonexistent_local_branch_is_unsafe(sb):
    v = rv.branch_verdict(sb.primary, "nope", forge=FakeForge())
    assert not v.safe and "unknown-branch" in codes(v)


# ── machine-readable shape ───────────────────────────────────────────────────────
def test_verdict_serializes_with_stable_machine_codes(sb):
    wt = sb.worktree("ser")
    (wt / "x").write_text("x")
    v = wt_verdict(sb, wt)
    d = v.to_dict()
    assert d["safe"] is False and d["kind"] == "worktree" and d["verdict"] == "UNSAFE"
    assert d["reasons"][0]["code"] == "dirty-worktree"
    assert isinstance(d["reasons"][0]["message"], str) and d["reasons"][0]["message"]
    assert "UNSAFE" in v.render()


# ── property-style: a dirty or unlanded state is NEVER safe ──────────────────────
@pytest.mark.parametrize("seed", range(24))
def test_random_states_are_never_safe_unless_clean_and_landed(tmp_path, seed):
    rng = random.Random(seed)
    sb = Sandbox(tmp_path)
    wt = sb.worktree("prop")
    n_commits = rng.randint(0, 3)
    for i in range(n_commits):
        sb.commit(wt, f"f{i}.txt", f"{seed}-{i}\n", f"c{i}")
    if n_commits and rng.random() < 0.5:
        sb.push(wt, "prop")
    landing = rng.choice(["none", "ff", "squash", "rebase"]) if n_commits else "none"
    pr_head = git(wt, "rev-parse", "HEAD")
    merge_oid = None
    if landing == "ff":
        merge_oid = sb.land_ff("prop")
    elif landing == "squash":
        merge_oid = sb.land_squash("prop")
    elif landing == "rebase":
        merge_oid = sb.land_rebase("prop")
    if rng.random() < 0.4:
        sb.advance_main(f"m{seed}.txt", "m\n")
    fetched = rng.random() < 0.7
    if fetched:
        sb.fetch()
    extra_commit = rng.random() < 0.35
    if extra_commit:
        sb.commit(wt, "late.txt", "late\n", "after landing")
    dirt = rng.choice(["clean", "clean", "modify", "stage", "untracked"])
    if dirt == "modify":
        (wt / "README.md").write_text("dirty\n")
    elif dirt == "stage":
        (wt / "staged.txt").write_text("s\n")
        git(wt, "add", "staged.txt")
    elif dirt == "untracked":
        (wt / "untracked.txt").write_text("u\n")

    records = [pr("prop", head_oid=pr_head, merge_oid=merge_oid)] if landing == "squash" else []
    v = wt_verdict(sb, wt, FakeForge(records))

    tip = git(wt, "rev-parse", "HEAD")
    on_main = subprocess.run(
        ["git", "-C", str(sb.primary), "merge-base", "--is-ancestor", tip, "refs/remotes/origin/main"]
    ).returncode == 0
    squash_proven = landing == "squash" and fetched and not extra_commit and (
        subprocess.run(
            ["git", "-C", str(sb.primary), "merge-base", "--is-ancestor", merge_oid, "refs/remotes/origin/main"]
        ).returncode == 0
    )
    # Independent oracle of "clean and provably landed" (content proofs are covered by their own
    # tests; a state where only a content proof could succeed must not be flagged by this oracle).
    expect_possibly_safe = dirt == "clean" and (on_main or squash_proven or (landing == "rebase" and fetched))
    if v.safe:
        assert expect_possibly_safe, f"seed {seed}: SAFE for dirt={dirt} landing={landing} fetched={fetched} extra={extra_commit}\n{v.render()}"
    if dirt != "clean":
        assert not v.safe
    if extra_commit and landing in {"none", "squash"} and not on_main:
        assert not v.safe


# ── review findings: things `git status` cannot see, and merge commits `git cherry` skips ──
def _ignore_worktrees_dir(sb: Sandbox) -> None:
    """What `rig worktree create` does: `.worktrees/` is ignored in EVERY worktree (common exclude)."""
    exclude = Path(git(sb.primary, "rev-parse", "--git-path", "info/exclude"))
    exclude = exclude if exclude.is_absolute() else sb.primary / exclude
    exclude.write_text(exclude.read_text() + "\n.worktrees/\n")


def landed_outer(sb: Sandbox) -> Path:
    wt = sb.worktree("outer")
    sb.commit(wt, "o.txt", "o\n", "feat outer")
    sb.land_merge_commit("outer")
    sb.fetch()
    return wt


def test_a_worktree_nested_inside_the_target_blocks_removal(sb):
    """`.worktrees/` is ignored, so the target looks clean while holding ANOTHER worktree's files."""
    _ignore_worktrees_dir(sb)
    outer = landed_outer(sb)
    inner = outer / ".worktrees" / "inner"
    git(sb.primary, "worktree", "add", "-q", "-b", "inner", str(inner), "main")
    (inner / "precious.txt").write_text("uncommitted inner work\n")
    assert git(outer, "status", "--porcelain") == ""  # sanity: the outer tree really looks clean
    v = wt_verdict(sb, outer)
    assert not v.safe and "contains-worktree" in codes(v)


def test_an_ignored_nested_repository_blocks_removal(sb):
    wt = sb.worktree("nested")
    (wt / ".gitignore").write_text("vendor/\n")
    git(wt, "add", ".gitignore")
    git(wt, "commit", "-q", "-m", "ignore vendor")
    sb.land_merge_commit("nested")
    sb.fetch()
    (wt / "vendor" / "lib").mkdir(parents=True)
    git(wt / "vendor" / "lib", "init", "-q")
    assert git(wt, "status", "--porcelain") == ""
    v = wt_verdict(sb, wt)
    assert not v.safe and "nested-repository" in codes(v)


def test_git_cherry_cannot_vouch_for_a_branch_that_contains_a_merge_commit(sb):
    """`git cherry` skips merge commits, so an evil merge (extra content only in the merge) would
    read as 'all patches upstream' — it must fall through to the stricter proofs and fail."""
    wt = sb.worktree("evil")
    sb.commit(wt, "a.txt", "a\n", "c1")
    sb.advance_main("z.txt", "z\n")
    sb.fetch()
    git(wt, "merge", "-q", "--no-commit", "--no-ff", "origin/main")
    (wt / "evil.txt").write_text("only in the merge commit\n")
    git(wt, "add", "evil.txt")
    git(wt, "commit", "-q", "-m", "evil merge")
    sb.land_rebase("evil")  # c1's patch lands upstream; the merge's evil.txt does not
    sb.fetch()
    v = wt_verdict(sb, wt)
    assert not v.safe and "not-landed" in codes(v)


def test_verdict_carries_the_tip_it_judged(sb):
    wt = sb.worktree("tip")
    v = wt_verdict(sb, wt)
    assert v.tip == git(wt, "rev-parse", "HEAD") and v.to_dict()["tip"] == v.tip


def test_nested_repository_scan_that_runs_out_of_budget_is_unsafe_not_clean(sb, monkeypatch):
    """An unfinished scan is 'unknown', and unknown is UNSAFE — never 'no nested repository'."""
    wt = sb.worktree("budget")
    (wt / ".gitignore").write_text("out/\n")
    git(wt, "add", ".gitignore")
    git(wt, "commit", "-q", "-m", "ignore out")
    sb.land_merge_commit("budget")
    sb.fetch()
    (wt / "out").mkdir()
    for i in range(20):
        (wt / "out" / f"f{i}.bin").write_text("x")
    monkeypatch.setattr(rv, "_NESTED_SCAN_BUDGET", 5)
    v = wt_verdict(sb, wt)
    assert not v.safe and "nested-repository-unknown" in codes(v)


def test_nested_repository_scan_skips_reinstallable_dependency_trees(sb):
    wt = sb.worktree("deps")
    (wt / ".gitignore").write_text("node_modules/\n")
    git(wt, "add", ".gitignore")
    git(wt, "commit", "-q", "-m", "ignore deps")
    sb.land_merge_commit("deps")
    sb.fetch()
    (wt / "node_modules" / "pkg").mkdir(parents=True)
    git(wt / "node_modules" / "pkg", "init", "-q")
    assert wt_verdict(sb, wt).safe


def test_lsof_failure_with_no_output_is_an_untrusted_scan(monkeypatch):
    import subprocess as sp

    monkeypatch.setattr(rv.subprocess, "run", lambda *a, **k: sp.CompletedProcess(a[0], 1, stdout=b"", stderr=b"lsof: error"))
    assert rv._lsof_cwds(1000) is None


def test_lsof_output_is_parsed_into_cwds(monkeypatch):
    import subprocess as sp

    out = b"p1\nn/tmp/a\np2\nn/tmp/b c\n"
    monkeypatch.setattr(rv.subprocess, "run", lambda *a, **k: sp.CompletedProcess(a[0], 0, stdout=out, stderr=b""))
    assert rv._lsof_cwds(1000) == [Path("/tmp/a"), Path("/tmp/b c")]


def _land_ignore(sb: Sandbox, name: str, pattern: str, extra: dict[str, str] | None = None) -> Path:
    wt = sb.worktree(name)
    (wt / ".gitignore").write_text(pattern + "\n")
    for rel, content in (extra or {}).items():
        (wt / rel).write_text(content)
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", f"ignore {pattern}")
    sb.land_merge_commit(name)
    sb.fetch()
    return wt


def test_target_is_only_skipped_beside_a_build_manifest(sb):
    plain = _land_ignore(sb, "t-plain", "target/")
    (plain / "target" / "important").mkdir(parents=True)
    git(plain / "target" / "important", "init", "-q")
    v = wt_verdict(sb, plain)
    assert not v.safe and "nested-repository" in codes(v)

    cargo = _land_ignore(sb, "t-cargo", "target/", {"Cargo.toml": "[package]\nname='x'\n"})
    (cargo / "target" / "debug").mkdir(parents=True)
    git(cargo / "target" / "debug", "init", "-q")
    assert wt_verdict(sb, cargo).safe


def test_a_nested_repository_deeper_than_the_scan_is_unknown_not_clean(sb):
    wt = _land_ignore(sb, "deep", "deep/")
    deepest = wt / "deep"
    for part in "abcdefghij":
        deepest = deepest / part
    deepest.mkdir(parents=True)
    git(deepest, "init", "-q")
    v = wt_verdict(sb, wt)
    assert not v.safe and "nested-repository-unknown" in codes(v)


def test_a_nested_repository_within_the_scan_depth_is_found(sb):
    wt = _land_ignore(sb, "shallow", "deep/")
    inner = wt / "deep" / "a" / "b" / "c" / "d" / "e"
    inner.mkdir(parents=True)
    git(inner, "init", "-q")
    v = wt_verdict(sb, wt)
    assert not v.safe and "nested-repository" in codes(v)


def test_a_directory_with_exactly_the_scan_budget_of_entries_is_complete(sb, monkeypatch):
    wt = _land_ignore(sb, "exact", "out/")
    (wt / "out").mkdir()
    for i in range(5):
        (wt / "out" / f"f{i}").write_text("x")
    monkeypatch.setattr(rv, "_NESTED_SCAN_BUDGET", 6)
    assert wt_verdict(sb, wt).safe


def test_unknown_scan_reason_names_the_cause_and_the_way_out(sb, monkeypatch):
    wt = _land_ignore(sb, "why", "out/")
    (wt / "out").mkdir()
    for i in range(20):
        (wt / "out" / f"f{i}").write_text("x")
    monkeypatch.setattr(rv, "_NESTED_SCAN_BUDGET", 5)
    reason = next(r for r in wt_verdict(sb, wt).reasons if r.code == "nested-repository-unknown")
    assert "out/" in reason.message and "too many entries" in reason.message
    assert "delete or move" in reason.message


def test_unknown_scan_reason_for_a_too_deep_tree_says_so(sb):
    wt = _land_ignore(sb, "whydeep", "deep/")
    deepest = wt / "deep"
    for part in "abcdefghij":
        deepest = deepest / part
    deepest.mkdir(parents=True)
    reason = next(r for r in wt_verdict(sb, wt).reasons if r.code == "nested-repository-unknown")
    assert "deeper than" in reason.message


def test_the_scan_budget_boundary_is_exact(sb, monkeypatch):
    wt = _land_ignore(sb, "bound", "out/")
    (wt / "out").mkdir()
    for i in range(5):
        (wt / "out" / f"f{i}").write_text("x")
    monkeypatch.setattr(rv, "_NESTED_SCAN_BUDGET", 5)
    assert wt_verdict(sb, wt).safe  # exactly as many entries as budget
    monkeypatch.setattr(rv, "_NESTED_SCAN_BUDGET", 4)
    assert "nested-repository-unknown" in codes(wt_verdict(sb, wt))


def test_build_and_dist_are_scanned_like_any_other_ignored_directory(sb):
    wt = _land_ignore(sb, "bd", "build/\ndist/")
    for name in ("build", "dist"):
        (wt / name / "vendored").mkdir(parents=True)
        git(wt / name / "vendored", "init", "-q")
    v = wt_verdict(sb, wt)
    assert not v.safe and "nested-repository" in codes(v)


def test_tool_cache_directories_are_skipped_by_the_nested_scan(sb):
    wt = _land_ignore(sb, "caches", ".mypy_cache/\n.next/\n.tox/")
    for name in (".mypy_cache", ".next", ".tox"):
        (wt / name / "deep").mkdir(parents=True)
        git(wt / name / "deep", "init", "-q")  # would be found if scanned; reinstallable caches are skipped
    assert wt_verdict(sb, wt).safe


def test_an_empty_proc_scan_is_untrusted_not_clean(monkeypatch, tmp_path):
    """The scanner's own process always has a cwd; an empty answer means the scan failed."""
    fake_proc = tmp_path / "proc"
    fake_proc.mkdir()
    real_path = rv.Path
    monkeypatch.setattr(rv, "Path", lambda *a: fake_proc if a == ("/proc",) else real_path(*a))
    assert rv._proc_cwds(1000) is None


# ── preserved: the commits survive on another remote branch ──────────────────────
def closed_pr(branch: str, tip: str) -> FakeForge:
    return FakeForge([pr(branch, state="CLOSED", head_oid=tip, merge_oid=None)])


def test_remote_branch_whose_commits_survive_on_an_archive_branch_is_safe_and_names_it(sb):
    wt, tip = superseded(sb)
    keep_on(sb, wt)
    v = rv.remote_branch_verdict(sb.primary, "origin", "sup", forge=closed_pr("sup", tip))
    assert v.safe, v.render()
    assert v.landed_by == "preserved"
    assert "origin/archive/keep" in v.render()


def test_local_branch_whose_commits_survive_on_an_archive_branch_is_safe(sb):
    wt, tip = superseded(sb)
    keep_on(sb, wt)
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    v = rv.branch_verdict(sb.primary, "sup", forge=closed_pr("sup", tip))
    assert v.safe, v.render()
    assert v.landed_by == "preserved"


def test_clean_worktree_whose_commits_survive_on_an_archive_branch_is_safe(sb):
    wt, tip = superseded(sb)
    keep_on(sb, wt)
    v = wt_verdict(sb, wt, closed_pr("sup", tip))
    assert v.safe, v.render()
    assert v.landed_by == "preserved"


def test_detached_worktree_whose_commits_survive_on_a_remote_branch_is_safe(sb):
    wt = sb.worktree("det", detach=True)
    sb.commit(wt, "d.txt", "d\n", "detached work")
    keep_on(sb, wt, "followup")
    assert wt_verdict(sb, wt).landed_by == "preserved"


def test_an_octopus_archive_commit_preserves_every_parent_tip(sb):
    one, _ = superseded(sb, "one")
    two, _ = superseded(sb, "two")
    arch = sb.worktree("arch")
    git(arch, "merge", "-q", "--no-edit", "one", "two")
    git(arch, "push", "-q", "origin", "HEAD:refs/heads/archive/octopus")
    sb.fetch()
    for name in ("one", "two"):
        v = rv.remote_branch_verdict(sb.primary, "origin", name, forge=FakeForge())
        assert v.safe and v.landed_by == "preserved", v.render()


def test_a_dirty_worktree_stays_unsafe_even_when_its_commits_are_preserved(sb):
    wt, tip = superseded(sb)
    keep_on(sb, wt)
    (wt / "scratch.txt").write_text("uncommitted\n")
    v = wt_verdict(sb, wt, closed_pr("sup", tip))
    assert not v.safe and "dirty-worktree" in codes(v)


def test_the_branch_itself_on_the_remote_does_not_preserve_it(sb):
    wt, tip = superseded(sb)
    v = rv.remote_branch_verdict(sb.primary, "origin", "sup", forge=closed_pr("sup", tip))
    assert not v.safe and "not-landed" in codes(v)


def test_a_branch_the_same_push_deletes_does_not_preserve_it(sb):
    wt, tip = superseded(sb)
    keep_on(sb, wt)
    v = rv.remote_branch_verdict(
        sb.primary, "origin", "sup", forge=FakeForge(), removing=frozenset({"sup", "archive/keep"})
    )
    assert not v.safe and "not-landed" in codes(v)


def test_remote_holders_stay_when_a_local_branch_is_deleted_whatever_its_name(sb):
    """`git branch -D` touches no remote ref: a local branch named like the holder changes nothing."""
    wt, tip = superseded(sb)
    keep_on(sb, wt)
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    git(sb.primary, "branch", "archive/keep", "origin/archive/keep")
    assert rv.branch_verdict(sb.primary, "sup", forge=FakeForge()).landed_by == "preserved"
    assert rv.branch_verdict(sb.primary, "archive/keep", forge=FakeForge()).landed_by == "preserved"


def test_a_holder_on_another_remote_preserves_a_branch_of_that_remote(sb):
    wt, tip = superseded(sb)
    git(sb.primary, "remote", "add", "upstream", str(sb.origin))
    git(wt, "push", "-q", "origin", "HEAD:refs/heads/archive/keep")
    git(sb.primary, "fetch", "-q", "upstream")
    v = rv.remote_branch_verdict(sb.primary, "upstream", "sup", forge=FakeForge())
    assert v.safe and v.landed_by == "preserved" and any("upstream/archive/keep" in n for n in v.notes)
    assert "git push upstream upstream/sup:refs/heads/archive/sup-<ts>" in rv.rescue_hint("remote-branch", "upstream/sup")
    # the judged tip is pushed by sha (the tracking ref can be stale or absent) to a name unique per commit
    assert f"git push upstream {tip}:refs/heads/archive/sup-{tip[:8]}" in rv.rescue_hint(
        "remote-branch", "upstream/sup", tip=tip
    )


def test_a_local_branch_pushed_only_under_its_own_name_is_still_unsafe(sb):
    """`git branch -D sup` with the only other copy being `origin/sup` is the plain 'pushed is irrelevant' case."""
    wt, tip = superseded(sb)
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    v = rv.branch_verdict(sb.primary, "sup", forge=FakeForge())
    assert not v.safe and "not-landed" in codes(v) and v.tip == tip


def test_a_remote_alias_of_the_target_is_not_a_second_branch(sb):
    """A symbolic `refs/heads/alias -> refs/heads/sup` on a plain git server vanishes with `sup`."""
    wt, tip = superseded(sb)
    git(sb.origin, "symbolic-ref", "refs/heads/alias", "refs/heads/sup")
    sb.fetch()
    assert git(sb.primary, "rev-parse", "--verify", "-q", "refs/remotes/origin/alias") == tip
    v = rv.remote_branch_verdict(sb.primary, "origin", "sup", forge=FakeForge())
    assert not v.safe and "not-landed" in codes(v)


def test_a_local_branch_sharing_the_name_of_a_remote_target_does_not_hide_its_holder(sb):
    """Local `sup` tracks `origin/feat/foo`; that is irrelevant when deleting `origin/sup`: feat/foo stays."""
    wt, tip = superseded(sb)
    git(wt, "push", "-q", "-u", "origin", "HEAD:refs/heads/feat/foo")
    sb.fetch()
    assert git(wt, "config", "--get", "branch.sup.merge") == "refs/heads/feat/foo"
    v = rv.remote_branch_verdict(sb.primary, "origin", "sup", forge=FakeForge())
    assert v.safe and v.landed_by == "preserved" and any("origin/feat/foo" in n for n in v.notes)


def test_a_preserved_verdict_does_not_claim_the_commits_are_on_the_default_branch(sb):
    wt, tip = superseded(sb)
    keep_on(sb, wt)
    v = rv.remote_branch_verdict(sb.primary, "origin", "sup", forge=FakeForge())
    assert v.safe and "default branch" not in v.render()


def test_the_remote_branch_a_local_branch_tracks_is_its_own_copy_not_a_holder(sb):
    """`git push -u origin HEAD:feat/foo` puts the branch's own copy under another name: still 'pushing
    is irrelevant' — only a SECOND remote branch counts."""
    wt = sb.worktree("wt-foo")
    sb.commit(wt, "f.txt", "f\n", "feat foo")
    git(wt, "push", "-q", "-u", "origin", "HEAD:refs/heads/feat/foo")
    sb.fetch()
    assert git(wt, "config", "--get", "branch.wt-foo.merge") == "refs/heads/feat/foo"
    worktree = wt_verdict(sb, wt)
    assert not worktree.safe and "not-landed" in codes(worktree)
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    local = rv.branch_verdict(sb.primary, "wt-foo", forge=FakeForge())
    assert not local.safe and "not-landed" in codes(local)
    git(sb.primary, "push", "-q", "origin", "wt-foo:refs/heads/archive/foo")
    sb.fetch()
    assert rv.branch_verdict(sb.primary, "wt-foo", forge=FakeForge()).landed_by == "preserved"


def test_a_holder_that_moved_forward_is_trusted_only_after_a_fetch(sb):
    """The tracking ref no longer matches the remote, so it proves nothing until `git fetch` catches up."""
    wt, tip = superseded(sb)
    keep_on(sb, wt)
    git(sb.integrator, "fetch", "-q", "origin")
    git(sb.integrator, "checkout", "-q", "-B", "moved", "origin/archive/keep")
    sb.commit(sb.integrator, "m.txt", "m\n", "archive moved forward")
    git(sb.integrator, "push", "-q", "origin", "moved:refs/heads/archive/keep")
    stale = rv.remote_branch_verdict(sb.primary, "origin", "sup", forge=FakeForge())
    assert not stale.safe and "git fetch origin --prune" in stale.render()
    sb.fetch()
    assert rv.remote_branch_verdict(sb.primary, "origin", "sup", forge=FakeForge()).landed_by == "preserved"


def test_a_remote_branch_that_does_not_contain_the_commits_does_not_preserve_them(sb):
    wt, tip = superseded(sb)
    other = sb.worktree("other")
    sb.commit(other, "o.txt", "o\n", "unrelated work")
    keep_on(sb, other, "archive/other")
    v = rv.remote_branch_verdict(sb.primary, "origin", "sup", forge=closed_pr("sup", tip))
    assert not v.safe and "not-landed" in codes(v)


def test_a_stale_tracking_ref_of_a_deleted_remote_branch_does_not_preserve_commits(sb):
    wt, tip = superseded(sb)
    keep_on(sb, wt)
    git(sb.origin, "update-ref", "-d", "refs/heads/archive/keep")  # gone on the remote, still tracked locally
    assert git(sb.primary, "rev-parse", "--verify", "-q", "refs/remotes/origin/archive/keep") == tip
    remote = rv.remote_branch_verdict(sb.primary, "origin", "sup", forge=FakeForge())
    assert not remote.safe and "not-landed" in codes(remote)
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    local = rv.branch_verdict(sb.primary, "sup", forge=FakeForge())
    assert not local.safe and "not-landed" in codes(local)


def test_a_tracking_ref_that_the_remote_has_moved_elsewhere_does_not_preserve_commits(sb):
    wt, tip = superseded(sb)
    keep_on(sb, wt)
    git(sb.integrator, "fetch", "-q", "origin")
    git(sb.integrator, "checkout", "-q", "-B", "rewritten", "origin/main")
    sb.commit(sb.integrator, "r.txt", "r\n", "archive force-pushed to something else")
    git(sb.integrator, "push", "-q", "--force", "origin", "rewritten:refs/heads/archive/keep")
    v = rv.remote_branch_verdict(sb.primary, "origin", "sup", forge=FakeForge())
    assert not v.safe and "not-landed" in codes(v)


def test_an_unreachable_remote_cannot_vouch_for_a_local_branch(sb):
    wt, tip = superseded(sb)
    keep_on(sb, wt)
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    git(sb.primary, "remote", "set-url", "origin", str(sb.root / "does-not-exist.git"))
    v = rv.branch_verdict(sb.primary, "sup", forge=FakeForge())
    assert not v.safe and "not-landed" in codes(v)
