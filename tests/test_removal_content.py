"""The landing proofs after a squash/rebase merge — where commits, patch-ids and PR heads all differ — and the
denial's evidence when nothing proves the work landed.

Real git throughout (tmp bare origin, a primary clone, linked worktrees, a forge-side integrator clone); the
forge is a fake list. Every negative test pins a way landed-looking work could still be lost.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from removal_helpers import FakeForge, Sandbox, git, no_processes, pr

from riglib import removal_content as content
from riglib import removal_explain
from riglib import removal_verdict as rv

BASE = "\n".join(f"line {i:02d} of the document, long enough to be distinctive" for i in range(1, 21)) + "\n"


@pytest.fixture
def sb(tmp_path) -> Sandbox:
    box = Sandbox(tmp_path)
    box.commit(box.primary, "doc.md", BASE, "docs: base document")
    git(box.primary, "push", "-q", "origin", "main")
    return box


def judge(sb: Sandbox, branch: str, forge: FakeForge | None = None) -> rv.Verdict:
    """The LOCAL-branch verdict (the branch's own worktree, if any, is removed first so it is not 'checked out')."""
    checked_out = sb.wt_dir / branch
    if checked_out.exists():
        git(sb.primary, "worktree", "remove", "--force", str(checked_out))
    sb.fetch()
    return rv.branch_verdict(sb.primary, branch, forge=forge or FakeForge())


def squashed_branch(
    sb: Sandbox, name: str = "feat", edit=None, extra_files: dict[str, str] | None = None, message: str | None = None
) -> Path:
    """A worktree branch with TWO commits that insert a block into doc.md (plus optional new files), squash-merged
    on the forge: the squash is one commit whose patch-id matches neither, so only the content can vouch for it."""
    wt = sb.worktree(name)
    anchor = "line 05 of the document, long enough to be distinctive\n"
    one = "INSERTED one: the branch's new paragraph about squash merges\n"
    two = "INSERTED two: and a second sentence that proves the point\n"
    sb.commit(wt, "doc.md", BASE.replace(anchor, anchor + one), f"feat: {name} starts the paragraph")
    sb.commit(wt, "doc.md", BASE.replace(anchor, anchor + one + two), f"feat: {name} finishes the paragraph")
    for path, body in (extra_files or {}).items():
        sb.commit(wt, path, body, f"feat: {name} adds {path}")
    if message is None:
        sb.land_squash(name)
    else:
        squash_with_message(sb, name, message)
    if edit is not None:
        edit()
    sb.fetch()
    return wt


def main_edits_next_to_the_insertion(sb: Sandbox) -> None:
    """Main moves on in the very lines around the branch's insertion — a plain merge now conflicts."""
    git(sb.integrator, "fetch", "-q", "origin")
    git(sb.integrator, "checkout", "-q", "-B", "main", "origin/main")
    path = sb.integrator / "doc.md"
    path.write_text(
        path.read_text().replace("line 06 of the document", "line 06 REWRITTEN by someone else on main"), encoding="utf-8"
    )
    git(sb.integrator, "commit", "-q", "-am", "docs: main rewrites the next line")
    git(sb.integrator, "push", "-q", "origin", "main")


# ── the positive case ────────────────────────────────────────────────────────────
def test_a_squash_merged_branch_is_squash_merged_even_after_main_edits_the_same_lines(sb):
    squashed_branch(sb, edit=lambda: main_edits_next_to_the_insertion(sb))
    v = judge(sb, "feat")
    assert v.safe, v.render()
    assert v.landed_by == rv.SQUASH_MERGED
    assert v.evidence is None and any("whole branch is in commit" in n for n in v.notes)


def test_the_plain_merge_proof_really_cannot_see_this_case(sb):
    """Guards the guard: if git ever merged this cleanly, the test above would stop exercising the new proof."""
    squashed_branch(sb, edit=lambda: main_edits_next_to_the_insertion(sb))
    sb.fetch()
    default, _ = rv._resolve_default(sb.primary)
    tip = git(sb.primary, "rev-parse", "refs/heads/feat")
    assert rv._prove_content(sb.primary, tip, default)[0] is None


def test_a_squash_merged_new_file_and_edit_are_landed(sb):
    squashed_branch(sb, extra_files={"notes/new.txt": "a brand new file with some words in it\n"})
    v = judge(sb, "feat")
    assert v.safe and v.landed_by in (rv.SQUASH_MERGED, "content-noop"), v.render()


def test_content_landed_also_clears_a_worktree_and_a_remote_branch(sb):
    wt = squashed_branch(sb, edit=lambda: main_edits_next_to_the_insertion(sb))
    sb.push(wt, "feat")
    assert rv.worktree_verdict(sb.primary, wt, forge=FakeForge(), process_cwds=no_processes).safe
    assert rv.remote_branch_verdict(sb.primary, "origin", "feat", forge=FakeForge()).safe


# ── what is NOT proven ───────────────────────────────────────────────────────────
def test_a_file_that_exists_only_on_the_branch_is_unique_work(sb):
    wt = sb.worktree("solo")
    sb.commit(wt, "only-here.py", "print('only on the branch')\n", "feat: a file main never gets")
    v = judge(sb, "solo")
    assert not v.safe and v.evidence is not None
    finding = next(f for f in v.evidence.files if f.path == "only-here.py")
    assert finding.status == content.UNIQUE
    assert v.evidence.classification == "unique-work"


def test_main_rewording_the_branchs_line_is_superseded_not_proven(sb):
    """The branch's inserted block is not on main (main reworded it) — probably obsolete, but never proven."""
    wt = sb.worktree("reworded")
    sb.commit(
        wt, "doc.md",
        BASE.replace("line 05", "line 05 EDITED BY THE BRANCH with a distinctive tail"), "docs: branch edit",
    )
    git(sb.integrator, "fetch", "-q", "origin")
    git(sb.integrator, "checkout", "-q", "-B", "main", "origin/main")
    (sb.integrator / "doc.md").write_text(BASE.replace("line 05", "line 05 EDITED BY MAIN differently"), encoding="utf-8")
    git(sb.integrator, "commit", "-q", "-am", "docs: main edits the same line another way")
    git(sb.integrator, "push", "-q", "origin", "main")
    v = judge(sb, "reworded")
    assert not v.safe and v.evidence is not None
    assert v.evidence.classification == content.SUPERSEDED
    finding = next(f for f in v.evidence.files if f.path == "doc.md")
    assert finding.status == content.SUPERSEDED
    assert any("EDITED BY THE BRANCH" in line for line in finding.missing)


def test_a_line_the_branch_deleted_but_main_still_has_is_not_proven(sb):
    wt = sb.worktree("deleter")
    sb.commit(wt, "doc.md", BASE.replace("line 10 of the document, long enough to be distinctive\n", ""), "docs: drop a line")
    v = judge(sb, "deleter")
    assert not v.safe and v.evidence is not None
    finding = next(f for f in v.evidence.files if f.path == "doc.md")
    assert finding.status == content.UNIQUE and any(line.startswith("- line 10") for line in finding.missing)


def test_a_binary_file_that_differs_is_never_proven(sb):
    sb.commit(sb.primary, "blob.bin", "base\0binary", "chore: binary")
    git(sb.primary, "push", "-q", "origin", "main")
    wt = sb.worktree("bin")
    (wt / "blob.bin").write_bytes(b"branch\0version")
    git(wt, "commit", "-q", "-am", "feat: change the binary")
    git(sb.integrator, "fetch", "-q", "origin")
    git(sb.integrator, "checkout", "-q", "-B", "main", "origin/main")
    (sb.integrator / "blob.bin").write_bytes(b"main\0version")
    git(sb.integrator, "commit", "-q", "-am", "chore: main changes the binary")
    git(sb.integrator, "push", "-q", "origin", "main")
    v = judge(sb, "bin")
    assert not v.safe and v.evidence is not None
    assert "binary file differs" in next(f for f in v.evidence.files if f.path == "blob.bin").note


def test_a_trivial_added_line_is_not_vouched_for_by_the_same_text_elsewhere(sb):
    """`end` appears on main already; the branch's `end` is a different place — it must not count as landed."""
    base = "def a():\n    pass\n\ndef b():\n    pass\n\nend\n"
    sb.commit(sb.primary, "code.txt", base, "chore: code")
    git(sb.primary, "push", "-q", "origin", "main")
    wt = sb.worktree("trivial")
    sb.commit(wt, "code.txt", base.replace("def a():\n    pass\n", "def a():\n    pass\nend\n"), "fix: close a")
    v = judge(sb, "trivial")
    assert not v.safe, "a one-word line must not be proven by the same word somewhere else"


def test_a_trivial_added_line_with_its_neighbours_intact_is_landed(sb):
    """The same one-word line IS accepted when the lines around it came across with it (a squash of two commits)."""
    base = "def a():\n    pass\n\ndef b():\n    pass\n"
    sb.commit(sb.primary, "code.txt", base, "chore: code")
    git(sb.primary, "push", "-q", "origin", "main")
    wt = sb.worktree("framed")
    first = base.replace("def a():\n    pass\n", "def a():\n    pass\nend\n")
    sb.commit(wt, "code.txt", first, "fix: close a")
    sb.commit(wt, "code.txt", first + "\ndef c():\n    return 'a distinctive third function'\n", "feat: add c")
    sb.land_squash("framed")
    git(sb.integrator, "fetch", "-q", "origin")
    git(sb.integrator, "checkout", "-q", "-B", "main", "origin/main")
    (sb.integrator / "code.txt").write_text(
        (sb.integrator / "code.txt").read_text().replace("def b():\n    pass", "def b():\n    return 'b moved on'"),
        encoding="utf-8",
    )
    git(sb.integrator, "commit", "-q", "-am", "feat: main edits b, right below the inserted line")
    git(sb.integrator, "push", "-q", "origin", "main")
    v = judge(sb, "framed")
    assert v.safe and v.landed_by == rv.SQUASH_MERGED, v.render()


FUNCS = "def f():\n    first = compute_something_long()\n    return\n\ndef g():\n    return\n"


def test_a_deleted_short_line_that_main_still_has_in_place_is_not_landed(sb):
    """Deleting a bare `return` is real work; `return` exists elsewhere, so only its neighbours can tell."""
    sb.commit(sb.primary, "funcs.txt", FUNCS, "chore: funcs")
    git(sb.primary, "push", "-q", "origin", "main")
    wt = sb.worktree("deleter2")
    sb.commit(wt, "funcs.txt", FUNCS.replace("    first = compute_something_long()\n    return\n", "    first = compute_something_long()\n"), "fix: f no longer returns early")
    v = judge(sb, "deleter2")
    assert not v.safe and v.evidence is not None
    finding = next(f for f in v.evidence.files if f.path == "funcs.txt")
    assert any(line.startswith("-") and line[1:].strip() == "return" for line in finding.missing)


def test_a_deleted_short_line_that_main_has_deleted_too_is_landed(sb):
    sb.commit(sb.primary, "funcs.txt", FUNCS, "chore: funcs")
    git(sb.primary, "push", "-q", "origin", "main")
    wt = sb.worktree("deleter3")
    trimmed = FUNCS.replace("    first = compute_something_long()\n    return\n", "    first = compute_something_long()\n")
    sb.commit(wt, "funcs.txt", trimmed, "fix: f no longer returns early")
    sb.commit(wt, "extra.txt", "a second commit so the squash patch differs\n", "feat: extra")
    sb.land_squash("deleter3")
    sb.fetch()
    v = judge(sb, "deleter3")
    assert v.safe, v.render()


def test_an_exec_bit_main_lacks_is_not_proven(sb):
    wt = sb.worktree("mode")
    (wt / "doc.md").chmod(0o755)
    git(wt, "commit", "-q", "-am", "chore: make it executable")
    v = judge(sb, "mode")
    assert not v.safe and v.evidence is not None
    assert "file mode" in next(f for f in v.evidence.files if f.path == "doc.md").note


def test_too_many_files_fail_closed(sb, monkeypatch):
    monkeypatch.setattr(content, "MAX_FILES", 1)
    wt = sb.worktree("wide")
    sb.commit(wt, "a.txt", "a file with words in it\n", "feat: a")
    sb.commit(wt, "b.txt", "another file with words\n", "feat: b")
    v = judge(sb, "wide")
    assert not v.safe and "could not run" in v.render()


def test_work_added_after_the_squash_merge_is_not_covered_by_it(sb):
    """The branch was squash-merged, then got one more file: the merged part is landed, the new file is not."""
    wt = squashed_branch(sb, name="late")
    sb.commit(wt, "after-merge.txt", "written after the PR was merged\n", "feat: one more thing")
    v = judge(sb, "late")
    assert not v.safe and v.evidence is not None
    by_path = {f.path: f.status for f in v.evidence.files}
    assert by_path["after-merge.txt"] == content.UNIQUE
    assert by_path["doc.md"] in (content.LANDED, content.IDENTICAL)


# ── the explanation uses the same facts ──────────────────────────────────────────
def test_the_denial_says_what_was_found_why_and_what_to_do(sb):
    wt = sb.worktree("old")
    sb.commit(wt, "doc.md", BASE.replace("line 05", "line 05 OLD VERSION of the sentence"), "docs: old wording")
    sb.commit(wt, "extra.txt", "a file only the branch has\n", "feat: extra")
    sb.push(wt, "old")
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    v = judge(sb, "old")
    text = removal_explain.explain_denial([v])
    for part in ("WHAT I FOUND", "WHY I STOPPED", "WHAT TO DO", "2 commit(s)", "extra.txt", "only here"):
        assert part in text, part
    assert "1. " in text and "2. " in text and "3. " in text
    assert "rig worktree discard" in text and "--reason" in text
    assert f"git -C {sb.primary} branch -D old" in text
    assert "[not-landed]" in text  # the machine-readable lines stay for logs


def test_the_denial_names_pr_leads_but_they_are_never_a_proof(sb):
    wt = sb.worktree("lead")
    tip = sb.commit(wt, "doc.md", BASE.replace("line 05", "line 05 A DIFFERENT VERSION entirely"), "docs: v1")
    other = sb.worktree("v2")
    merged = sb.commit(other, "doc.md", BASE.replace("line 05", "line 05 THE VERSION THAT SHIPPED"), "docs: v2")
    squash = sb.land_squash("v2")
    forge = FakeForge(
        [
            pr("lead", state="CLOSED", head_oid=tip, merge_oid=None, number=7),
            pr("v2", state="MERGED", head_oid=merged, merge_oid=squash, number=9),
        ]
    )
    v = judge(sb, "lead", forge)
    assert not v.safe and v.evidence is not None
    leads = " | ".join(v.evidence.related)
    assert "PR #7 (closed) was opened from this branch" in leads
    assert "merged PR #9 changed 1 of the 1 file(s)" in leads


def test_a_dirty_worktree_is_never_offered_the_discard_route(sb):
    wt = sb.worktree("dirty")
    (wt / "scratch.txt").write_text("uncommitted\n")
    v = rv.worktree_verdict(sb.primary, wt, forge=FakeForge(), process_cwds=no_processes)
    assert not v.safe and not removal_explain.approvable(v)
    text = removal_explain.explain_denial([v])
    assert "rig worktree discard" not in text and "Fix that first" in text


# ── the redesigned proof order: PR first, then the whole batch against one squash commit ─────────────
def squash_with_message(sb: Sandbox, branch: str, message: str) -> str:
    """Squash-merge ``branch`` on the forge with a GitHub-style description listing the commits."""
    sb._integrator_branch(branch)
    git(sb.integrator, "merge", "-q", "--squash", f"_incoming/{branch}")
    git(sb.integrator, "commit", "-q", "-m", message)
    git(sb.integrator, "push", "-q", "origin", "main")
    return git(sb.integrator, "rev-parse", "HEAD")


def two_commit_branch(sb: Sandbox, name: str) -> tuple[Path, str, str]:
    wt = sb.worktree(name)
    first = sb.commit(wt, f"{name}-a.txt", "first file of the branch with words\n", f"feat: {name} adds a long enough subject one")
    second = sb.commit(wt, f"{name}-b.txt", "second file of the branch with words\n", f"feat: {name} adds a long enough subject two")
    return wt, first, second


def test_a_squash_commit_whose_message_names_the_branchs_commits_is_found_by_the_message(sb):
    squashed_branch(
        sb, name="named", edit=lambda: main_edits_next_to_the_insertion(sb),
        message="feat: named (#31)\n\n* feat: named starts the paragraph\n* feat: named finishes the paragraph",
    )
    v = judge(sb, "named")
    assert v.safe and v.landed_by == rv.SQUASH_MERGED, v.render()
    assert any("its message names the branch's commits" in n for n in v.notes)


def test_the_batch_must_be_in_the_squash_commit_whole_not_just_in_part(sb):
    """The squash carried only the first of the branch's two commits: the second one's work is unlanded."""
    wt, first, _second = two_commit_branch(sb, "half")
    git(sb.primary, "branch", "half-a", first)
    sb.land_squash("half-a")
    v = judge(sb, "half")
    assert not v.safe and v.evidence is not None
    assert next(f for f in v.evidence.files if f.path == "half-b.txt").status == content.UNIQUE


def test_a_merged_pr_covers_exactly_its_head_and_names_the_commits_made_after_it(sb):
    wt, _first, second = two_commit_branch(sb, "later")
    merge = sb.land_squash("later")
    sb.commit(wt, "later-c.txt", "written after the PR was merged\n", "feat: a third thing after the merge")
    sb.fetch()
    forge = FakeForge([pr("later", head_oid=second, merge_oid=merge, number=12)])
    v = judge(sb, "later", forge)
    assert not v.safe and v.evidence is not None
    assert any("after it" in lead and "a third thing after the merge" in lead for lead in v.evidence.related)
    assert "AFTER it" in v.render()
    # the same PR with nothing added after it proves the branch landed
    git(sb.primary, "branch", "-f", "later", second)
    assert judge(sb, "later", forge).landed_by == "pr-merged"


def test_history_rewritten_after_the_merge_is_old_and_a_new_commit_in_it_is_not(sb):
    wt, _first, second = two_commit_branch(sb, "rebased")
    merge = sb.land_squash("rebased")
    git(wt, "commit", "-q", "--amend", "-m", "feat: rebased adds a long enough subject two (reworded)")
    sb.fetch()
    forge = FakeForge([pr("rebased", head_oid=second, merge_oid=merge, number=13)])
    v = judge(sb, "rebased", forge)
    assert v.safe and v.landed_by == "pr-merged", v.render()
    assert any("rewritten" in n for n in v.notes)
    wt = sb.worktree("rebased-again", base="rebased")
    sb.commit(wt, "novel.txt", "work the PR never had\n", "feat: new work on the rewritten branch")
    forge = FakeForge([pr("rebased-again", head_oid=second, merge_oid=merge, number=13)])
    v = judge(sb, "rebased-again", forge)
    assert not v.safe and any("never had" in lead for lead in v.evidence.related)


def test_a_pr_under_another_branch_name_is_found_by_the_number_a_squash_commit_cites(sb):
    squashed_branch(
        sb, name="renamed", edit=lambda: main_edits_next_to_the_insertion(sb),
        message="feat: renamed (#7)\n\n* feat: renamed starts the paragraph\n* feat: renamed finishes the paragraph",
    )
    first = git(sb.primary, "rev-list", "--reverse", "origin/main..renamed").split()[0]
    merge = git(sb.primary, "log", "-1", "--format=%H", "--grep=renamed (#7)", "origin/main")
    forge = FakeForge([pr("some-other-name", head_oid=first, merge_oid=merge, number=7)])
    v = judge(sb, "renamed", forge)
    # found through the cited number; the squash proof then clears the branch whatever the PR's recorded head says
    assert v.safe and any("number a squash commit on the default branch cites" in n for n in v.notes)


def test_a_stash_made_on_the_branch_blocks_even_a_landed_branch(sb):
    wt = sb.worktree("stashed")
    (wt / "doc.md").write_text("changed but not committed\n")
    git(wt, "stash", "push", "-q")
    v = rv.worktree_verdict(sb.primary, wt, forge=FakeForge(), process_cwds=no_processes)
    assert not v.safe and any(r.code == "stash" for r in v.reasons)
    assert not removal_explain.approvable(v)
    assert "git stash drop" in removal_explain.explain_denial([v])


def test_uncommitted_files_are_named_before_the_landing_question(sb):
    wt = sb.worktree("order")
    sb.commit(wt, "x.txt", "unlanded committed work with words\n", "feat: unlanded")
    (wt / "scratch.txt").write_text("uncommitted\n")
    v = rv.worktree_verdict(sb.primary, wt, forge=FakeForge(), process_cwds=no_processes)
    text = removal_explain.explain_denial([v])
    assert text.index("uncommitted path") < text.index("It carries")
