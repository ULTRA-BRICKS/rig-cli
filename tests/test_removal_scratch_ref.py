"""Scratch refs — refs outside refs/heads, refs/tags and refs/remotes, e.g. ``refs/rigaudit/main`` (rig-cli#768).

The agent that creates such a ref must be able to remove it through the SAME verdict engine as a branch: SAFE when
the commit it names is landed or also held by a local branch that stays, otherwise the friendly explanation and the
owner-approved discard path. Real git (a tmp bare origin and clones), a fake forge, no network.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from removal_helpers import FakeForge, Sandbox, git, no_processes, pr, superseded

from riglib import errors, removal_explain, worktree
from riglib import removal_guard as rg
from riglib import removal_verdict as rv
from riglib.worktree_discard import Approval, discard

REF = "refs/rigaudit/f477"
OTHER = "refs/rigaudit/f477r"
REASON = "obsolete: audit marker of a closed investigation"


@pytest.fixture
def sb(tmp_path) -> Sandbox:
    return Sandbox(tmp_path)


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    return h


class Owner:
    def __init__(self, approve: bool, before=None) -> None:
        self.approve, self.before, self.calls = approve, before, []

    def __call__(self, justification: str, context: dict[str, str], cwd: str) -> Approval:
        self.calls.append((justification, context, cwd))
        if self.before is not None:
            self.before()
        return Approval(self.approve, "approved by the fake owner" if self.approve else "denied by the fake owner")


def only_on_ref(sb: Sandbox, ref: str = REF, name: str = "audit") -> str:
    """A commit that exists nowhere but under ``ref``: a branch is made, committed on, then deleted."""
    wt = sb.worktree(name)
    tip = sb.commit(wt, f"{name}.txt", f"{name}\n", f"audit {name}")
    git(sb.primary, "update-ref", ref, tip)
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    git(sb.primary, "branch", "-D", name)
    return tip


def verdict(sb: Sandbox, ref: str = REF, forge: FakeForge | None = None, removing: frozenset[str] = frozenset()) -> rv.Verdict:
    return rv.scratch_ref_verdict(sb.primary, ref, forge=forge or FakeForge(), removing=removing)


def codes(v: rv.Verdict) -> list[str]:
    return [r.code for r in v.reasons]


def refs(sb: Sandbox, namespace: str = "refs/rigaudit") -> list[str]:
    return git(sb.primary, "for-each-ref", "--format=%(refname)", namespace).split()


def run_discard(sb: Sandbox, targets: list[str], owner: Owner):
    return discard(
        sb.primary, targets, reason=REASON, fetch=False, forge=FakeForge(), process_cwds=no_processes, approver=owner
    )


# ── which refs are scratch refs ──────────────────────────────────────────────────
@pytest.mark.parametrize(
    "name,expected",
    [
        ("refs/rigaudit/main", True),
        ("refs/pull/12/head", True),
        ("refs/stash", True),
        ("refs/notes/commits", True),
        ("refs/heads/main", False),
        ("refs/tags/v1", False),
        ("refs/remotes/origin/main", False),
        ("main", False),
        ("rigaudit/main", False),
        ("HEAD", False),
    ],
)
def test_which_names_are_scratch_refs(name, expected):
    assert rv.is_scratch_ref(name) is expected


# ── the verdict ──────────────────────────────────────────────────────────────────
def test_a_ref_at_a_commit_already_on_the_default_branch_is_safe(sb):
    git(sb.primary, "update-ref", "refs/rigaudit/main", git(sb.primary, "rev-parse", "origin/main"))
    v = verdict(sb, "refs/rigaudit/main")
    assert v.safe and v.landed_by == "ancestor" and v.kind == "scratch-ref" and v.target == "refs/rigaudit/main"


def test_the_only_holder_of_unlanded_work_is_unsafe_and_can_be_discarded(sb):
    tip = only_on_ref(sb)
    v = verdict(sb)
    assert not v.safe and codes(v) == ["not-landed"] and v.tip == tip
    assert "held-by-ref proof" in v.reasons[0].message
    assert removal_explain.approvable(v)
    text = removal_explain.explain_denial([v])
    assert "WHAT I FOUND" in text and "WHAT TO DO" in text
    assert f"rig worktree discard -C {sb.primary} {REF} --reason" in text
    assert f"git -C {sb.primary} update-ref -d {REF}" in text  # what the owner can run themselves
    assert f"git branch archive/f477-{tip[:8]} {tip}" in text  # the keep-it route
    assert "recovery" not in text and "bundle" not in text


def test_a_local_branch_that_stays_holds_the_commit(sb):
    tip = only_on_ref(sb)
    git(sb.primary, "branch", "keep", tip)
    v = verdict(sb)
    assert v.safe and v.landed_by == rv.HELD_BY_REF and any("refs/heads/keep" in n for n in v.notes)
    assert "also held by a local branch" in v.render()
    git(sb.primary, "branch", "-D", "keep")  # the holder is gone: nothing keeps the commit any more
    assert not verdict(sb).safe


def test_a_tag_is_no_holder_because_deleting_a_tag_is_not_guarded(sb):
    tip = only_on_ref(sb)
    git(sb.primary, "tag", "tmp", tip)  # `git tag -d tmp` would pass unjudged: a tag made on the spot would launder the commit
    v = verdict(sb)
    assert not v.safe and "no local branch holds it" in v.reasons[0].message


def test_another_scratch_ref_never_holds_the_commit_but_a_branch_unless_it_goes_with_it(sb):
    tip = only_on_ref(sb)
    git(sb.primary, "update-ref", OTHER, tip)  # a scratch ref can be moved onto main unjudged: no holder
    assert not verdict(sb, REF).safe
    git(sb.primary, "branch", "keep", tip)
    assert verdict(sb, REF).landed_by == rv.HELD_BY_REF
    # deleting the branch in the same command: it may not vouch
    assert not verdict(sb, REF, removing=frozenset({"refs/heads/keep"})).safe


def test_a_branch_holding_a_descendant_commit_holds_it_too(sb):
    wt = sb.worktree("audit")
    sb.commit(wt, "a.txt", "a\n", "first")
    child = sb.commit(wt, "b.txt", "b\n", "second")
    first = git(wt, "rev-parse", "HEAD~1")
    git(sb.primary, "update-ref", REF, first)
    git(sb.primary, "update-ref", OTHER, child)
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    git(sb.primary, "branch", "-D", "audit")
    git(sb.primary, "branch", "keep", child)
    assert verdict(sb, REF).landed_by == rv.HELD_BY_REF
    assert verdict(sb, OTHER).landed_by == rv.HELD_BY_REF
    git(sb.primary, "branch", "-f", "keep", first)
    assert verdict(sb, REF).landed_by == rv.HELD_BY_REF and not verdict(sb, OTHER).safe  # older does not hold newer


@pytest.mark.parametrize(
    "holder",
    [
        "refs/stash",
        "refs/bisect/bad",
        "refs/original/refs/heads/x",
        "refs/prefetch/remotes/origin/x",  # what `git maintenance` prefetches and prunes again
        "refs/rewritten/x",
        "refs/pull/12/head",  # a mirror a fetch refspec made
        "refs/remotes/origin/ghost",  # a tracking ref the remote no longer has
    ],
)
def test_fleeting_and_remote_copies_are_never_holders(sb, holder):
    tip = only_on_ref(sb)
    git(sb.primary, "update-ref", holder, tip)
    assert not verdict(sb).safe


def test_a_symbolic_ref_to_the_target_is_not_a_holder(sb):
    only_on_ref(sb)
    git(sb.primary, "symbolic-ref", "refs/rigaudit/alias", REF)  # follows the target: it goes when the target goes
    assert not verdict(sb).safe


def test_a_symbolic_scratch_ref_is_judged_as_the_ref_it_points_at(sb):
    """`git update-ref -d alias` follows the symref and deletes the REFERENT, so the referent's work is what is at stake."""
    tip = only_on_ref(sb)
    git(sb.primary, "symbolic-ref", "refs/rigaudit/alias", REF)
    v = verdict(sb, "refs/rigaudit/alias")
    assert not v.safe and codes(v) == ["not-landed"] and v.tip == tip
    assert any("symbolic ref" in n for n in v.notes)
    git(sb.primary, "branch", "keep", tip)
    held = verdict(sb, "refs/rigaudit/alias")
    assert held.safe and held.landed_by == rv.HELD_BY_REF
    assert not verdict(sb, "refs/rigaudit/alias", removing=frozenset({"refs/heads/keep"})).safe  # the holder goes too


def test_a_symbolic_ref_to_a_branch_is_refused(sb):
    wt = sb.worktree("feat")
    sb.commit(wt, "a.txt", "a\n", "unmerged")
    git(sb.primary, "symbolic-ref", "refs/rigaudit/alias", "refs/heads/feat")
    v = verdict(sb, "refs/rigaudit/alias")
    assert not v.safe and codes(v) == ["symbolic-ref"] and not removal_explain.approvable(v)


def test_a_live_branch_of_origin_preserves_the_commit_and_a_stale_tracking_ref_does_not(sb):
    wt, tip = superseded(sb, "sup")  # pushed, never landed
    git(sb.primary, "update-ref", REF, tip)
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    git(sb.primary, "branch", "-D", "sup")
    v = verdict(sb)
    assert v.safe and v.landed_by == rv.PRESERVED and any("origin/sup" in n for n in v.notes)
    git(sb.origin, "update-ref", "-d", "refs/heads/sup")  # gone on the remote, still tracked locally
    assert not verdict(sb).safe


def test_a_squash_merged_branch_is_proven_for_its_scratch_ref(sb):
    wt = sb.worktree("feat")
    sb.commit(wt, "feat.txt", "feat\n", "feat: thing")
    tip = sb.commit(wt, "more.txt", "more\n", "feat: more")  # two commits: no single patch-id twin on main
    git(sb.primary, "update-ref", REF, tip)
    sb.land_squash("feat")
    sb.advance_main("feat.txt", "feat\nlater\n")  # main edits the same file: merging the tip is no longer a no-op
    sb.fetch()
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    git(sb.primary, "branch", "-D", "feat")
    v = verdict(sb)
    assert v.safe and v.landed_by == rv.SQUASH_MERGED


def test_a_merged_pr_found_by_head_commit_proves_a_scratch_ref(sb):
    wt = sb.worktree("feat")
    tip = sb.commit(wt, "feat.txt", "feat\n", "feat: thing")
    git(sb.primary, "update-ref", REF, tip)
    squash = sb.land_squash("feat", edit=("feat.txt", "reviewed\n"))  # not the whole-batch diff: only the PR can prove it
    sb.fetch()
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    git(sb.primary, "branch", "-D", "feat")
    assert not verdict(sb).safe
    v = verdict(sb, forge=FakeForge([pr("feat", head_oid=tip, merge_oid=squash)]))
    assert v.safe and v.landed_by == "pr-merged"


def test_the_stash_ref_is_never_judged_landed(sb):
    (sb.primary / "README.md").write_text("changed\n")
    git(sb.primary, "stash", "push")
    v = verdict(sb, "refs/stash")
    assert not v.safe and codes(v) == ["stash"] and not removal_explain.approvable(v)
    assert "EVERY stash entry" in v.reasons[0].message


def test_a_ref_that_names_a_blob_cannot_be_judged(sb):
    blob = git(sb.primary, "hash-object", "-w", "README.md")
    git(sb.primary, "update-ref", "refs/rigaudit/blob", blob)
    v = verdict(sb, "refs/rigaudit/blob")
    assert not v.safe and codes(v) == ["not-a-commit"] and not removal_explain.approvable(v)


@pytest.mark.parametrize(
    "name,code",
    [
        ("refs/rigaudit/missing", "unknown-ref"),
        ("refs/rigaudit/../x", "invalid-ref-name"),
        ("refs/heads/main", "not-a-scratch-ref"),
        ("refs/tags/v1", "not-a-scratch-ref"),
        ("refs/remotes/origin/main", "not-a-scratch-ref"),
        ("main", "not-a-scratch-ref"),
    ],
)
def test_names_that_cannot_be_judged_are_unsafe_with_a_code(sb, name, code):
    v = verdict(sb, name)
    assert not v.safe and codes(v) == [code]


def test_a_missing_default_branch_ref_fails_closed(sb):
    only_on_ref(sb)
    git(sb.primary, "update-ref", "-d", "refs/remotes/origin/HEAD")
    git(sb.primary, "update-ref", "-d", "refs/remotes/origin/main")
    git(sb.primary, "update-ref", "refs/rigaudit/f477-copy", "refs/rigaudit/f477")  # held: only the missing default ref matters
    v = verdict(sb)
    assert not v.safe and "no-default-ref" in codes(v)


# ── resolving a target: worktree, scratch ref, branch, or an explanation ─────────
def judge(sb: Sandbox, target: str, cwd: Path | None = None, remote: str | None = None):
    return worktree.judge_target(
        sb.primary, target, remote=remote, cwd=cwd, fetch=False, forge=FakeForge(), process_cwds=no_processes
    )


def test_a_full_scratch_ref_name_resolves_to_the_scratch_verdict(sb):
    only_on_ref(sb)
    v, err = judge(sb, REF)
    assert err is None and v.kind == "scratch-ref" and v.target == REF


def test_a_full_heads_name_resolves_to_the_branch_verdict(sb):
    wt = sb.worktree("feat")
    sb.commit(wt, "a.txt", "a\n", "unmerged")
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    v, err = judge(sb, "refs/heads/feat")
    assert err is None and v.kind == "local-branch" and v.target == "feat"


def test_a_tag_gets_an_explanation_and_the_command_to_run(sb):
    git(sb.primary, "tag", "v1")
    for name in ("refs/tags/v1", "v1"):
        v, err = judge(sb, name)
        assert v is None and err.not_found and err.exit_code == errors.EXIT_CONFIG
        assert "is a tag" in err.message and "git tag -d v1" in err.message and "git branch -a --contains v1" in err.message


def test_a_remote_tracking_ref_points_at_the_remote_flag(sb):
    for name in ("refs/remotes/origin/main", "origin/main"):
        v, err = judge(sb, name)
        assert v is None and "remote-tracking ref" in err.message
        assert "rig worktree verdict --remote origin main" in err.message and "rig worktree discard --remote origin main" in err.message


def test_an_unknown_full_ref_says_how_to_list_refs(sb):
    v, err = judge(sb, "refs/rigaudit/nope")
    assert v is None and "no ref 'refs/rigaudit/nope'" in err.message and "git for-each-ref" in err.message
    assert "refs/rigaudit/main" in err.message  # the example of a scratch ref name


def test_a_directory_that_is_no_registered_worktree_is_explained(sb, tmp_path):
    plain = tmp_path / "leftover"
    plain.mkdir()
    v, err = judge(sb, str(plain))
    assert v is None and "is a directory" in err.message and "git worktree prune" in err.message
    v, err = judge(sb, str(sb.integrator))  # another clone of the same remote: it has its own .git
    assert v is None and f"-C {sb.integrator}" in err.message and "another repository" in err.message


def test_an_unknown_name_lists_the_kinds_that_exist(sb):
    v, err = judge(sb, "no-such-thing")
    assert v is None and err.not_found
    for word in ("git worktree list", "git branch --list", "scratch ref", "--remote"):
        assert word in err.message


def test_no_unresolvable_target_gets_the_old_bare_error(sb):
    git(sb.primary, "tag", "v1")
    for name in ("v1", "origin/main", "refs/rigaudit/nope", "no-such-thing", str(sb.root)):
        v, err = judge(sb, name)
        assert v is None and "neither a registered worktree" not in err.message


# ── discard ──────────────────────────────────────────────────────────────────────
def test_a_safe_scratch_ref_is_deleted_without_asking_the_owner(sb):
    tip = only_on_ref(sb)
    git(sb.primary, "branch", "keep", tip)
    owner = Owner(True)
    res = run_discard(sb, [REF], owner)
    assert res.status == "discarded" and not owner.calls, res.message
    assert refs(sb) == []
    assert git(sb.primary, "rev-parse", "refs/heads/keep") == tip  # the holder is untouched


def test_an_unlanded_scratch_ref_is_deleted_only_after_the_owner_approves(sb):
    only_on_ref(sb)
    owner = Owner(True)
    res = run_discard(sb, [REF], owner)
    assert res.status == "discarded" and len(owner.calls) == 1, res.message
    assert owner.calls[0][1]["target"] == f"scratch-ref {REF}"
    assert refs(sb) == []


def test_a_denied_discard_keeps_the_scratch_ref(sb):
    tip = only_on_ref(sb)
    owner = Owner(False)
    res = run_discard(sb, [REF], owner)
    assert res.status == "refused" and res.exit_code == errors.EXIT_REMOVAL_UNSAFE
    assert git(sb.primary, "rev-parse", REF) == tip


def test_a_scratch_ref_that_moves_after_the_judgement_is_not_deleted(sb):
    only_on_ref(sb)
    moved = only_on_ref(sb, OTHER, "other")
    owner = Owner(True, before=lambda: git(sb.primary, "update-ref", REF, moved))
    res = run_discard(sb, [REF], owner)
    assert res.status == "error" and "moved after it was judged" in res.message
    assert git(sb.primary, "rev-parse", REF) == moved


def test_scratch_refs_discarded_together_cannot_vouch_for_each_other(sb):
    tip = only_on_ref(sb)
    git(sb.primary, "update-ref", OTHER, tip)
    owner = Owner(True)
    res = run_discard(sb, [REF, OTHER], owner)
    assert res.status == "discarded" and len(owner.calls) == 1, res.message
    card = owner.calls[0][1]
    assert card["t1 target"] == f"scratch-ref {REF}" and card["t2 target"] == f"scratch-ref {OTHER}"
    assert refs(sb) == []


def test_a_branch_discarded_in_the_same_command_cannot_vouch_for_a_scratch_ref(sb):
    tip = only_on_ref(sb)
    git(sb.primary, "branch", "stale", tip)
    owner = Owner(True)
    res = run_discard(sb, ["stale", REF], owner)
    assert res.status == "discarded" and len(owner.calls) == 1, res.message
    assert "t2 target" in owner.calls[0][1]  # both targets needed the owner: the branch was no holder
    assert refs(sb) == [] and not git(sb.primary, "branch", "--list", "stale")


def test_a_stash_is_refused_and_nobody_is_asked(sb):
    (sb.primary / "README.md").write_text("changed\n")
    git(sb.primary, "stash", "push")
    owner = Owner(True)
    res = run_discard(sb, ["refs/stash"], owner)
    assert res.status == "refused" and not owner.calls
    assert git(sb.primary, "stash", "list")


def test_a_symbolic_scratch_ref_is_deleted_like_its_referent(sb):
    only_on_ref(sb)
    git(sb.primary, "symbolic-ref", "refs/rigaudit/alias", REF)
    owner = Owner(False)
    res = run_discard(sb, ["refs/rigaudit/alias"], owner)
    assert res.status == "refused" and len(owner.calls) == 1  # the referent's unlanded work needs the owner
    assert git(sb.primary, "rev-parse", REF)
    git(sb.primary, "branch", "keep", git(sb.primary, "rev-parse", REF))
    safe = run_discard(sb, ["refs/rigaudit/alias"], Owner(True))
    assert safe.status == "discarded", safe.message
    assert git(sb.primary, "for-each-ref", "--format=%(refname)", REF) == ""  # git deleted the referent


def test_the_branch_of_a_worktree_discarded_in_the_same_command_cannot_vouch_for_a_scratch_ref(sb):
    """The worktree is named `w` but sits on branch `feat`: the verdicts, not the target strings, say what goes."""
    wt = sb.worktree("w", branch="feat")
    tip = sb.commit(wt, "a.txt", "a\n", "unmerged")
    git(sb.primary, "update-ref", REF, tip)
    owner = Owner(True)
    res = run_discard(sb, ["w", REF], owner)
    assert res.status == "discarded" and len(owner.calls) == 1, res.message
    assert "t2 target" in owner.calls[0][1]  # the scratch ref needed the owner too: `feat` was no holder
    assert refs(sb) == [] and not git(sb.primary, "branch", "--list", "feat") and not wt.exists()


# ── the guard ────────────────────────────────────────────────────────────────────
def decide(sb: Sandbox, command: str, cwd: Path | None = None) -> rg.GuardDecision:
    return rg.evaluate_command(command, cwd or sb.primary, forge=FakeForge(), process_cwds=no_processes, fetch=False)


def test_the_guard_allows_deleting_a_landed_scratch_ref(sb):
    git(sb.primary, "update-ref", "refs/rigaudit/main", git(sb.primary, "rev-parse", "origin/main"))
    d = decide(sb, "git update-ref -d refs/rigaudit/main")
    assert d.decision == "allow" and "ancestor" in d.reason and "scratch-ref" in d.reason


def test_the_guard_allows_deleting_a_scratch_ref_another_ref_holds(sb):
    tip = only_on_ref(sb)
    git(sb.primary, "branch", "keep", tip)
    assert decide(sb, f"git update-ref -d {REF}").decision == "allow"
    assert decide(sb, f"git update-ref -d {REF} {tip}").decision == "allow"  # with the expected old value


def test_the_guard_denies_the_only_holder_of_unlanded_work_with_the_friendly_text(sb):
    tip = only_on_ref(sb)
    d = decide(sb, f"git update-ref -d {REF}")
    assert d.decision == "deny" and "not-landed" in d.reason
    for heading in ("WHAT I FOUND", "WHY I STOPPED", "WHAT TO DO"):
        assert heading in d.reason
    assert f"rig worktree discard -C {sb.primary} {REF}" in d.reason
    assert f"git branch archive/f477-{tip[:8]}" in d.reason
    assert f"rig worktree verdict -C {sb.primary} {REF}" in d.reason
    assert git(sb.primary, "rev-parse", REF) == tip  # the guard only judges


def test_the_guard_denies_dropping_the_stash(sb):
    (sb.primary / "README.md").write_text("changed\n")
    git(sb.primary, "stash", "push")
    d = decide(sb, "git update-ref -d refs/stash")
    steps = d.reason.split("WHAT TO DO")[1].split("Do NOT")[0]
    assert d.decision == "deny" and "stash" in d.reason and "rig worktree discard" not in steps  # an approval cannot settle it


def test_the_guard_judges_a_scratch_ref_in_another_repository(sb, tmp_path):
    only_on_ref(sb)
    d = decide(sb, f"git -C {sb.primary} update-ref -d {REF}", cwd=tmp_path)
    assert d.decision == "deny" and "not-landed" in d.reason


def test_the_guard_denies_a_scratch_ref_deletion_it_cannot_read(sb):
    only_on_ref(sb)
    assert decide(sb, 'git update-ref -d "$R"').decision == "deny"
    assert decide(sb, f"git update-ref -d {REF} && echo done").decision == "deny"
    assert decide(sb, "git update-ref --stdin < /dev/null").decision == "deny"


def test_a_null_new_value_is_a_deletion_the_guard_judges(sb):
    only_on_ref(sb)
    assert decide(sb, f"git update-ref {REF} 0000000000000000000000000000000000000000").decision == "deny"


def test_moving_a_scratch_ref_is_not_a_removal(sb):
    tip = only_on_ref(sb)
    assert decide(sb, f"git update-ref {REF} {tip}").decision == "pass"


# ── the CLI ──────────────────────────────────────────────────────────────────────
def rig(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    root = Path(__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": str(root), "GIT_CONFIG_GLOBAL": os.devnull}
    return subprocess.run(
        [sys.executable, "-m", "riglib", "worktree", *args], capture_output=True, text=True, cwd=cwd, env=env, check=False
    )


def test_the_cli_verdict_reads_a_scratch_ref(sb):
    git(sb.primary, "update-ref", "refs/rigaudit/main", git(sb.primary, "rev-parse", "origin/main"))
    safe = rig("verdict", "refs/rigaudit/main", "--no-fetch", "-C", str(sb.primary), cwd=sb.primary)
    assert safe.returncode == 0 and "SAFE to remove scratch-ref refs/rigaudit/main" in safe.stdout, safe.stdout + safe.stderr
    only_on_ref(sb)
    unsafe = rig("verdict", REF, "--no-fetch", "-C", str(sb.primary), cwd=sb.primary)
    assert unsafe.returncode == errors.EXIT_REMOVAL_UNSAFE
    assert "WHAT I FOUND" in unsafe.stdout and "rig worktree discard" in unsafe.stdout


def test_the_cli_explains_a_target_it_does_not_judge(sb):
    git(sb.primary, "tag", "v1")
    res = rig("verdict", "refs/tags/v1", "--no-fetch", "-C", str(sb.primary), cwd=sb.primary)
    assert res.returncode == errors.EXIT_CONFIG and "is a tag" in res.stdout + res.stderr
    assert "neither a registered worktree" not in res.stdout + res.stderr


def test_the_cli_discard_removes_a_safe_scratch_ref(sb):
    git(sb.primary, "update-ref", "refs/rigaudit/main", git(sb.primary, "rev-parse", "origin/main"))
    res = rig("discard", "refs/rigaudit/main", "--reason", REASON, "--no-fetch", "-C", str(sb.primary), cwd=sb.primary)
    assert res.returncode == 0 and "deleted scratch ref refs/rigaudit/main" in res.stdout, res.stdout + res.stderr
    assert refs(sb) == []


def test_the_verdict_command_a_denial_prints_works_from_another_directory(sb, tmp_path):
    """agent-tools#1017: the denial named `rig worktree verdict <path>`, which only resolves from inside the repo."""
    wt = sb.worktree("work")
    sb.commit(wt, "a.txt", "a\n", "unmerged work")
    v = rv.worktree_verdict(sb.primary, wt, forge=FakeForge(), process_cwds=no_processes)
    text = removal_explain.explain_denial([v])
    command = re.search(r"`(rig worktree verdict [^`]+)`", text).group(1)
    assert f"-C {sb.primary}" in command
    res = rig(*shlex.split(command)[2:], cwd=tmp_path)
    assert res.returncode == errors.EXIT_REMOVAL_UNSAFE and "WHAT I FOUND" in res.stdout, res.stdout + res.stderr


def test_the_guard_judges_a_symbolic_scratch_ref_as_its_referent(sb):
    only_on_ref(sb)
    git(sb.primary, "symbolic-ref", "refs/rigaudit/alias", REF)
    d = decide(sb, "git update-ref -d refs/rigaudit/alias")  # git would delete REF itself
    assert d.decision == "deny" and "not-landed" in d.reason
    assert git(sb.primary, "rev-parse", REF)


def test_a_tag_made_on_the_spot_does_not_let_the_guard_allow_the_deletion(sb):
    tip = only_on_ref(sb)
    git(sb.primary, "tag", "tmp", tip)  # `git tag -d tmp` is not guarded, so a tag is no holder
    assert decide(sb, f"git update-ref -d {REF}").decision == "deny"


def test_a_missing_tag_is_not_called_a_tag_and_the_stash_name_is_case_insensitive(sb):
    v, err = judge(sb, "refs/tags/typo")
    assert v is None and "is a tag" not in err.message and "no ref 'refs/tags/typo'" in err.message
    assert rv.is_scratch_ref("refs/Heads/main") is False and rv.is_scratch_ref("refs/TAGS/x") is False
