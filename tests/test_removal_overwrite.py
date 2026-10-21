"""Overwriting a branch loses its old tip exactly like deleting it does.

`git branch -f/-M/-C`, `git checkout -B` and `git switch -C` re-point an existing branch; the
commits only that branch held survive nowhere but the reflog. :func:`overwrite_verdict` asks the
landed-removal question about the branch's CURRENT tip (landed on `origin/<default>`, or held by
another remote branch that stays) with two differences from a deletion: a fast-forward (the old tip
is an ancestor of the new start) loses nothing, and a branch that does not exist yet has nothing to
lose. The guard maps every overwrite shape onto it. REAL git (tests/removal_helpers.py); the forge is
a fake; every negative asserts the command is DENIED and every positive that it is not.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from removal_helpers import FakeForge, Sandbox, git, keep_on, no_processes

from riglib import removal_guard as rg
from riglib import removal_verdict as rv


@pytest.fixture
def sb(tmp_path) -> Sandbox:
    return Sandbox(tmp_path)


def decide(sb: Sandbox, command: str, cwd: Path | None = None) -> rg.GuardDecision:
    return rg.evaluate_command(command, cwd or sb.primary, forge=FakeForge(), process_cwds=no_processes, fetch=False)


def unlanded_branch(sb: Sandbox, name: str = "feat") -> str:
    """A local branch with a commit that exists nowhere else (not even pushed); no worktree holds it."""
    wt = sb.worktree(name)
    tip = sb.commit(wt, f"{name}.txt", f"{name}\n", f"work {name}")
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    return tip


def landed_branch(sb: Sandbox, name: str = "done") -> str:
    wt = sb.worktree(name)
    tip = sb.commit(wt, f"{name}.txt", f"{name}\n", f"feat {name}")
    sb.push(wt, name)
    sb.land_ff(name)
    sb.fetch()
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    return tip


def verdict(sb: Sandbox, branch: str, start: str = "", forge: FakeForge | None = None) -> rv.Verdict:
    return rv.overwrite_verdict(sb.primary, branch, start=start, forge=forge or FakeForge(), fetch=False)


# ── the verdict ──────────────────────────────────────────────────────────────────
def test_overwriting_an_unlanded_branch_is_unsafe(sb):
    tip = unlanded_branch(sb)
    v = verdict(sb, "feat", "origin/main")
    assert not v.safe and v.tip == tip
    assert [r.code for r in v.reasons] == ["not-landed"]
    assert "overwrite" in v.render()


def test_overwriting_a_landed_branch_is_safe(sb):
    landed_branch(sb)
    unlanded_branch(sb, "elsewhere")  # a start that does NOT contain the old tip: only landing can vouch
    v = verdict(sb, "done", "elsewhere")
    assert v.safe and v.landed_by == "ancestor"


def test_a_branch_that_does_not_exist_yet_has_nothing_to_lose_even_without_a_default_ref(sb):
    git(sb.primary, "remote", "set-head", "origin", "-d")
    v = verdict(sb, "brand-new", "origin/main")
    assert v.safe and v.landed_by == "new-branch"


def test_a_fast_forward_loses_nothing(sb):
    tip = unlanded_branch(sb)
    git(sb.primary, "branch", "ahead", tip)
    wt = sb.worktree("ahead-wt", branch="ahead-wt", base="ahead")
    sb.commit(wt, "more.txt", "more\n", "more")
    v = verdict(sb, "feat", "ahead-wt")
    assert v.safe and v.landed_by == "fast-forward"


def test_resetting_a_branch_to_itself_is_a_no_op(sb):
    unlanded_branch(sb)
    assert verdict(sb, "feat", "feat").safe


def test_a_rewind_is_not_a_fast_forward(sb):
    tip = unlanded_branch(sb)
    git(sb.primary, "branch", "child", tip)
    wt = sb.worktree("child-wt", branch="child-wt", base="child")
    sb.commit(wt, "more.txt", "more\n", "more")
    git(sb.primary, "branch", "-f", "feat", "child-wt")  # feat is now the CHILD
    v = verdict(sb, "feat", "origin/main")  # back to main: drops both commits
    assert not v.safe


def test_a_diverging_start_is_not_a_fast_forward(sb):
    unlanded_branch(sb, "feat")
    unlanded_branch(sb, "other")
    assert not verdict(sb, "feat", "other").safe


def test_a_start_that_does_not_resolve_falls_back_to_the_landing_proofs(sb):
    unlanded_branch(sb)
    assert not verdict(sb, "feat", "no-such-ref").safe
    landed_branch(sb)
    assert verdict(sb, "done", "no-such-ref").safe  # landed on its own, whatever the start is


def test_an_option_looking_start_is_never_handed_to_git_as_an_option(sb):
    unlanded_branch(sb)
    assert not verdict(sb, "feat", "--output=/tmp/pwned").safe


def test_the_branchs_own_remote_copy_is_not_a_second_copy(sb):
    wt = sb.worktree("pushed")
    sb.commit(wt, "p.txt", "p\n", "pushed work")
    sb.push(wt, "pushed")
    sb.fetch()
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    assert not verdict(sb, "pushed", "origin/main").safe


def test_commits_archived_on_another_remote_branch_count_as_preserved(sb):
    wt = sb.worktree("arch")
    sb.commit(wt, "a.txt", "a\n", "archived work")
    keep_on(sb, wt, "archive/arch-1")
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    v = verdict(sb, "arch", "origin/main")
    assert v.safe and v.landed_by == rv.PRESERVED


def test_the_default_branch_may_be_overwritten_when_nothing_on_it_is_lost(sb):
    sb.advance_main()
    sb.fetch()
    git(sb.primary, "merge", "-q", "--ff-only", "origin/main")
    assert verdict(sb, "main", "origin/main").safe


def test_local_commits_on_the_default_branch_are_not_thrown_away(sb):
    sb.commit(sb.primary, "local.txt", "local\n", "local only on main")
    assert not verdict(sb, "main", "origin/main").safe


@pytest.mark.parametrize("name", ["", "-x", "a b", "a..b", "refs/heads/x", "x.lock"])
def test_invalid_branch_names_are_unsafe(sb, name):
    v = verdict(sb, name, "origin/main")
    assert not v.safe and v.reasons[0].code == "invalid-branch-name"


def test_no_default_ref_makes_an_existing_unlanded_branch_unsafe(sb):
    unlanded_branch(sb)
    git(sb.primary, "remote", "set-head", "origin", "-d")
    assert not verdict(sb, "feat", "origin/main").safe


# ── the guard: every overwrite shape ─────────────────────────────────────────────
DENIED_SHAPES = [
    "git branch -f feat origin/main",
    "git branch --force feat origin/main",
    "git branch --forc feat origin/main",
    "git branch -f feat",  # start = HEAD (main)
    "git branch feat origin/main -f",
    "git branch -fq feat origin/main",
    "git branch -f -- feat origin/main",
    "git branch -f --track feat origin/main",
    "git checkout -B feat origin/main",
    "git checkout -B feat",
    "git checkout -Bfeat origin/main",
    "git checkout -fB feat origin/main",
    "git checkout -q -B feat origin/main",
    "git checkout -B feat origin/main --",
    "git switch -C feat origin/main",
    "git switch -C feat",
    "git switch --force-create feat origin/main",
    "git switch --force-create=feat origin/main",
    "git switch --force-c feat origin/main",
    "git switch -qC feat origin/main",
    "git -C {primary} checkout -B feat origin/main",
    "git -C {primary} branch -f feat origin/main",
    "git --no-pager branch -f feat origin/main",
    "git update-ref refs/heads/feat origin/main",
    "git update-ref -m reset refs/heads/feat origin/main",
    "git update-ref --no-deref refs/heads/feat origin/main",
    "git update-ref refs/heads/feat origin/main $(git rev-parse feat)",
    "git update-ref refs/heads/feat HEAD",
    "git -C {primary} update-ref refs/heads/feat origin/main",
]


@pytest.mark.parametrize("command", DENIED_SHAPES)
def test_overwriting_an_unlanded_branch_is_denied(sb, command):
    tip = unlanded_branch(sb)
    d = decide(sb, command.format(primary=sb.primary), cwd=sb.root if "{primary}" in command else None)
    assert d.decision == "deny", d.reason
    assert "overwrite" in d.reason and "feat" in d.reason
    assert git(sb.primary, "rev-parse", "refs/heads/feat") == tip  # the guard only judges


MOVE_COPY_DENIED = [
    "git branch -M feat2 feat",
    "git branch -m -f feat2 feat",
    "git branch -mf feat2 feat",
    "git branch --move --force feat2 feat",
    "git branch --mov -f feat2 feat",
    "git branch -C feat2 feat",
    "git branch -c -f feat2 feat",
    "git branch -cf feat2 feat",
    "git branch --copy --force feat2 feat",
    "git branch --cop -f feat2 feat",
]


@pytest.mark.parametrize("command", MOVE_COPY_DENIED)
def test_move_and_copy_over_an_unlanded_branch_are_denied(sb, command):
    unlanded_branch(sb, "feat")
    unlanded_branch(sb, "feat2")
    d = decide(sb, command)
    assert d.decision == "deny", d.reason
    assert [v.target for v in d.verdicts] == ["feat"]  # the DESTINATION is what gets overwritten


def test_renaming_the_current_branch_over_an_unlanded_one_is_denied(sb):
    unlanded_branch(sb, "feat")
    d = decide(sb, "git branch -M feat")  # main -> feat
    assert d.decision == "deny" and [v.target for v in d.verdicts] == ["feat"]


@pytest.mark.parametrize(
    "command",
    [
        "git branch -f done origin/main",
        "git checkout -B done origin/main",
        "git switch -C done origin/main",
        "git branch -M other done",
        "git branch -C other done",
    ],
)
def test_overwriting_a_landed_branch_is_allowed(sb, command):
    landed_branch(sb, "done")
    unlanded_branch(sb, "other")  # `other` is the SOURCE of the move/copy: its commits survive under the new name
    d = decide(sb, command)
    # moving `other` over `done` re-points `done` at unlanded work but loses only `done`'s landed tip
    assert d.decision == "allow", d.reason


@pytest.mark.parametrize(
    "command",
    [
        "git checkout -B brand-new origin/main",
        "git switch -C brand-new origin/main",
        "git branch -f brand-new origin/main",
        "git branch -M main brand-new",
        "git branch -C main brand-new",
    ],
)
def test_creating_a_new_branch_this_way_is_not_an_overwrite(sb, command):
    assert decide(sb, command).decision == "pass"


def test_a_fast_forward_reset_is_allowed(sb):
    tip = unlanded_branch(sb)
    git(sb.primary, "branch", "ahead", tip)
    wt = sb.worktree("ahead-wt", branch="ahead-wt", base="ahead")
    sb.commit(wt, "more.txt", "more\n", "more")
    for command in ("git branch -f feat ahead-wt", "git checkout -B feat ahead-wt", "git switch -C feat ahead-wt"):
        d = decide(sb, command)
        assert d.decision == "allow", (command, d.reason)
        assert d.verdicts[0].landed_by == "fast-forward"


@pytest.mark.parametrize(
    "command",
    [
        "git branch -m feat renamed",
        "git branch --move feat renamed",
        "git branch -c feat copied",
        "git branch --copy feat copied",
        "git checkout -b made origin/main",
        "git switch -c made origin/main",
        "git branch made origin/main",
        "git branch --list -f",
        "git branch -r -f",
        "git branch -u origin/main feat",
        "git branch --set-upstream-to=origin/main feat",
        "git branch -f --contains HEAD",
        "git branch -rf feat",
        "git branch -lf feat",
        "git branch -u origin/main -f feat",
        "git branch -mc -f feat2 feat",  # git refuses two modes at once
        "git branch -M feat2 feat feat",  # wrong operand count: git refuses
        "git branch -dM feat2 feat",  # a delete and a move at once: git refuses
        "git branch -f --merged main feat",  # --merged is a LIST mode
        "git branch --copy --contains HEAD -f feat feat2",
        "git branch -f",
        "git checkout feat",
        "git switch feat",
        "git checkout -b B origin/main",  # a branch literally named B is not the -B flag
        "git checkout main -- B",
        "git switch main",
    ],
)
def test_non_overwriting_forms_pass(sb, command):
    unlanded_branch(sb)
    assert decide(sb, command).decision == "pass"


def test_force_cannot_be_used_to_skip_the_verdict_by_stacking_flags(sb):
    unlanded_branch(sb)
    assert decide(sb, "git branch -f -f feat origin/main").decision == "deny"
    assert decide(sb, "git checkout -f -B feat origin/main").decision == "deny"
    assert decide(sb, "git checkout --force -B feat origin/main").decision == "deny"


@pytest.mark.parametrize(
    "command",
    [
        'git checkout -B "$B" origin/main',
        'git switch -C "$B" origin/main',
        'git branch -f "$B" origin/main',
        "git branch -M $A $B",
        "git checkout -B feat* origin/main",
        "git branch -M feat?",
        "git checkout -B {a,b} origin/main",
    ],
)
def test_an_unknowable_destination_is_denied(sb, command):
    unlanded_branch(sb)
    assert decide(sb, command).decision == "deny"


def test_an_unknowable_start_does_not_hide_an_unlanded_tip(sb):
    unlanded_branch(sb)
    assert decide(sb, 'git checkout -B feat "$REF"').decision == "deny"
    landed_branch(sb)
    assert decide(sb, 'git checkout -B done "$REF"').decision == "allow"


def test_a_chain_of_overwrites_is_judged_per_command(sb):
    unlanded_branch(sb)
    assert decide(sb, "git fetch && git checkout -B feat origin/main").decision == "deny"
    assert decide(sb, "git fetch && git checkout -B fresh origin/main && git status").decision == "pass"
    landed_branch(sb)
    assert decide(sb, "git fetch; git checkout -B done origin/main").decision == "allow"


def test_a_cd_in_the_chain_makes_the_repository_unknowable(sb):
    unlanded_branch(sb)
    assert decide(sb, f"cd {sb.primary} && git checkout -B feat origin/main", cwd=sb.root).decision == "deny"
    assert decide(sb, f"pushd {sb.primary}; git branch -f feat origin/main", cwd=sb.root).decision == "deny"
    assert decide(sb, f"(cd {sb.primary} && git switch -C feat origin/main)", cwd=sb.root).decision == "deny"


def test_a_chain_with_a_deletion_stays_denied_as_before(sb):
    unlanded_branch(sb)
    unlanded_branch(sb, "gone")
    d = decide(sb, "git branch -D gone && git checkout -B fresh origin/main")
    assert d.decision == "deny" and "chained" in d.reason


def test_a_message_that_merely_mentions_an_overwrite_passes(sb):
    unlanded_branch(sb)
    assert decide(sb, 'git commit -m "never run git checkout -B feat or git branch -f feat"').decision == "pass"
    assert decide(sb, "echo 'git switch -C feat'").decision == "pass"
    assert decide(sb, "git commit -F - <<'EOF'\ngit branch -M a b\nEOF").decision == "pass"


def test_wrappers_cannot_hide_an_overwrite(sb):
    unlanded_branch(sb)
    for command in (
        "sudo git checkout -B feat origin/main",
        "env X=1 git branch -f feat origin/main",
        "bash -c 'git checkout -B feat origin/main'",
        "eval 'git switch -C feat origin/main'",
        "eval git checkout -B feat origin/main",
        "ssh host git branch -f feat origin/main",
    ):
        assert decide(sb, command).decision == "deny", command


def test_a_checked_out_branch_in_the_current_worktree_is_judged_by_its_tip(sb):
    wt = sb.worktree("here")
    sb.commit(wt, "h.txt", "h\n", "work on here")
    d = rg.evaluate_command(
        "git checkout -B here origin/main", wt, forge=FakeForge(), process_cwds=no_processes, fetch=False
    )
    assert d.decision == "deny"
    assert git(wt, "rev-parse", "--abbrev-ref", "HEAD") == "here"


def descended_primary(sb: Sandbox) -> None:
    """The primary checkout sits on a branch that CONTAINS `feat`'s unlanded tip (HEAD is a descendant)."""
    unlanded_branch(sb, "feat")
    git(sb.primary, "checkout", "-q", "-b", "descend", "feat")
    sb.commit(sb.primary, "more.txt", "more\n", "more on descend")


@pytest.mark.parametrize(
    "command",
    [
        "git branch -f feat",  # start defaults to HEAD
        "git checkout -B feat",
        "git switch -C feat",
        "git checkout -B feat -- more.txt",  # after `--` come paths, never a start-point
        "git branch -M feat",  # renames the CURRENT branch onto feat
        "git branch -M descend feat",  # the source branch is the start
        "git branch -C descend feat",
    ],
)
def test_the_default_and_implied_starts_are_what_git_uses(sb, command):
    descended_primary(sb)
    d = decide(sb, command)
    assert d.decision == "allow" and d.verdicts[0].landed_by == "fast-forward", (command, d.reason)


def test_a_flag_assembled_from_an_expansion_is_denied(sb):
    unlanded_branch(sb)
    assert decide(sb, 'git checkout -"$F" feat origin/main').decision == "deny"
    assert decide(sb, "git switch -$F feat origin/main").decision == "deny"


def test_an_absolute_dash_c_repository_survives_a_cd(sb):
    landed_branch(sb, "done")
    assert decide(sb, f"cd /tmp && git -C {sb.primary} checkout -B done origin/main", cwd=sb.root).decision == "allow"
    assert decide(sb, f"cd /tmp && git -C {sb.primary.name} checkout -B done origin/main", cwd=sb.root).decision == "deny"


def test_a_source_branch_other_than_head_is_the_start_of_a_move_or_copy(sb):
    tip = unlanded_branch(sb, "feat")
    git(sb.primary, "branch", "ahead", tip)
    wt = sb.worktree("ahead-wt", branch="ahead-wt", base="ahead")
    sb.commit(wt, "more.txt", "more\n", "more")
    for command in ("git branch -M ahead-wt feat", "git branch -C ahead-wt feat", "git branch -m -f ahead-wt feat"):
        d = decide(sb, command)  # HEAD is main here: only the SOURCE branch contains feat's tip
        assert d.decision == "allow" and d.verdicts[0].landed_by == "fast-forward", (command, d.reason)


def test_a_branch_ref_that_does_not_point_at_a_commit_is_unsafe(sb):
    blob = git(sb.primary, "rev-parse", "HEAD:README.md")  # a blob, written straight into the ref store
    (sb.primary / ".git" / "refs" / "heads" / "weird").write_text(blob + "\n")
    v = verdict(sb, "weird", "origin/main")
    assert not v.safe and v.reasons[0].code == "unknown-branch"


def test_update_ref_overwriting_a_branch_is_judged_like_branch_force(sb):
    tip = unlanded_branch(sb)
    git(sb.primary, "branch", "ahead", tip)
    wt = sb.worktree("ahead-wt", branch="ahead-wt", base="ahead")
    sb.commit(wt, "more.txt", "more\n", "more")
    assert decide(sb, "git update-ref refs/heads/feat ahead-wt").decision == "allow"  # a fast-forward
    assert decide(sb, "git update-ref refs/heads/brand-new origin/main").decision == "pass"
    landed_branch(sb, "done")
    assert decide(sb, "git update-ref refs/heads/done origin/main").decision == "allow"
    assert decide(sb, "git update-ref refs/tags/v1 HEAD").decision == "pass"
    assert decide(sb, "git update-ref refs/remotes/origin/feat HEAD").decision == "pass"
    assert decide(sb, 'git update-ref refs/heads/feat "$NEW"').decision == "deny"  # unknowable start, unlanded tip
    assert decide(sb, 'git update-ref "refs/heads/$B" origin/main').decision == "deny"

