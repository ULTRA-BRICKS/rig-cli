"""`rm -rf <path>` of a linked worktree (or of a directory that contains one) is a removal.

`git worktree remove` is judged by the landed-removal verdict, but deleting the very same directory
with `rm -rf` bypasses it and takes uncommitted files and the only checkout of a detached commit with
it. The guard resolves every literal target of a RECURSIVE `rm`, finds the linked worktrees of the
repositories it touches (the cwd's, and the one the target sits in) that the target is or contains,
and applies the worktree verdict to each: any UNSAFE one denies. Everything it cannot resolve (a
variable, a substitution, a brace, a target fed by `xargs`/`find`, a relative path after a `cd`) is
denied rather than guessed; a plain directory, a sub-directory of a worktree and a non-recursive `rm`
are not worktree removals and pass. REAL git (tests/removal_helpers.py).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from removal_helpers import FakeForge, Sandbox, git, no_processes, superseded

from riglib import removal_guard as rg


@pytest.fixture
def sb(tmp_path) -> Sandbox:
    return Sandbox(tmp_path)


def decide(sb: Sandbox, command: str, cwd: Path | None = None, forge: FakeForge | None = None, procs=no_processes):
    return rg.evaluate_command(command, cwd or sb.primary, forge=forge or FakeForge(), process_cwds=procs, fetch=False)


def landed_worktree(sb: Sandbox, name: str = "done") -> Path:
    wt = sb.worktree(name)
    sb.commit(wt, f"{name}.txt", "x\n", f"feat {name}")
    sb.land_merge_commit(name)
    sb.fetch()
    return wt


RM_FORMS = [
    "rm -rf {p}",
    "rm -r {p}",
    "rm -fr {p}",
    "rm -Rf {p}",
    "rm -rfv {p}",
    "rm --recursive {p}",
    "rm --recursive --force {p}",
    "rm --rec -f {p}",
    "rm --r {p}",
    "rm -f -r {p}",
    "rm -f {p} -r",
    "rm -- {p}" + "  -r",  # `--` ends options: -r is a FILE here, so this one is NOT recursive (see below)
]


@pytest.mark.parametrize("form", RM_FORMS[:-1])
def test_rm_recursive_of_an_unsafe_worktree_is_denied(sb, form):
    wt, tip = superseded(sb, "sup")
    d = decide(sb, form.format(p=wt))
    assert d.decision == "deny", d.reason
    assert "UNSAFE" in d.reason and str(wt) in d.reason
    assert wt.exists() and git(wt, "rev-parse", "HEAD") == tip  # the guard only judges


def test_dashes_after_double_dash_are_file_names_not_flags(sb):
    wt, _ = superseded(sb, "sup")
    assert decide(sb, f"rm -f -- {wt} -r").decision == "pass"


def test_rm_of_a_landed_clean_worktree_is_allowed(sb):
    wt = landed_worktree(sb)
    d = decide(sb, f"rm -rf {wt}")
    assert d.decision == "allow", d.reason
    assert [v.kind for v in d.verdicts] == ["worktree"]


def test_rm_of_a_dirty_landed_worktree_is_denied(sb):
    wt = landed_worktree(sb, "dirty")
    (wt / "precious.txt").write_text("only copy\n")
    d = decide(sb, f"rm -rf {wt}")
    assert d.decision == "deny" and "dirty" in d.reason.lower()


def test_rm_of_a_worktree_with_a_running_process_is_denied(sb):
    wt = landed_worktree(sb, "busy")
    d = decide(sb, f"rm -rf {wt}", procs=lambda: [wt])
    assert d.decision == "deny" and "live-process" in d.reason


def test_a_worktree_holding_the_only_copy_of_a_detached_commit_is_denied(sb):
    wt = sb.worktree("det", detach=True)
    sb.commit(wt, "d.txt", "d\n", "detached work")
    assert decide(sb, f"rm -rf {wt}").decision == "deny"


@pytest.mark.parametrize("suffix", ["", "/", "/."])
def test_trailing_slash_and_dot_do_not_hide_the_target(sb, suffix):
    wt, _ = superseded(sb, "sup")
    assert decide(sb, f"rm -rf {wt}{suffix}").decision == "deny"


def test_relative_targets_resolve_against_the_cwd(sb):
    superseded(sb, "sup")
    assert decide(sb, "rm -rf wts/sup", cwd=sb.root).decision == "deny"
    assert decide(sb, "rm -rf ../wts/sup", cwd=sb.primary).decision == "deny"
    assert decide(sb, "rm -rf ./sup", cwd=sb.wt_dir).decision == "deny"
    assert decide(sb, "rm -rf wts/../wts/sup", cwd=sb.root).decision == "deny"


def test_a_directory_that_contains_an_unsafe_worktree_is_denied(sb):
    landed_worktree(sb, "ok")
    superseded(sb, "sup")
    assert decide(sb, f"rm -rf {sb.wt_dir}").decision == "deny"
    assert decide(sb, f"rm -rf {sb.root}").decision == "deny"
    assert decide(sb, "rm -rf wts", cwd=sb.root).decision == "deny"
    assert decide(sb, "rm -rf ..", cwd=sb.wt_dir).decision == "deny"


def test_a_directory_whose_worktrees_are_all_safe_is_allowed(sb):
    landed_worktree(sb, "one")
    landed_worktree(sb, "two")
    d = decide(sb, f"rm -rf {sb.wt_dir}")
    assert d.decision == "allow" and len(d.verdicts) == 2


def test_each_target_is_judged_and_one_unsafe_target_denies_all(sb):
    ok = landed_worktree(sb, "ok")
    bad, _ = superseded(sb, "sup")
    assert decide(sb, f"rm -rf {ok} {bad}").decision == "deny"
    assert decide(sb, f"rm -rf {bad} {ok}").decision == "deny"


def test_globs_are_expanded_like_the_shell_does(sb):
    landed_worktree(sb, "ok")
    superseded(sb, "sup")
    assert decide(sb, "rm -rf wts/*", cwd=sb.root).decision == "deny"
    assert decide(sb, "rm -rf wts/s*", cwd=sb.root).decision == "deny"
    assert decide(sb, "rm -rf wts/su?", cwd=sb.root).decision == "deny"
    assert decide(sb, "rm -rf wts/[s]up", cwd=sb.root).decision == "deny"
    assert decide(sb, "rm -rf wts/o*", cwd=sb.root).decision == "allow"
    assert decide(sb, "rm -rf wts/nothing-matches-*", cwd=sb.root).decision == "pass"


def test_a_quoted_glob_character_is_a_literal_file_name_but_a_mixed_word_still_expands(sb):
    superseded(sb, "sup")
    assert decide(sb, 'rm -rf "wts/s*"', cwd=sb.root).decision == "pass"  # the shell does not expand it
    assert decide(sb, 'rm -rf "wts"/s*', cwd=sb.root).decision == "deny"
    assert decide(sb, "rm -rf wts/s\\*", cwd=sb.root).decision == "pass"  # an escaped star is literal too


def test_home_and_tilde_prefixes_are_resolved(sb, monkeypatch):
    superseded(sb, "sup")
    landed_worktree(sb, "ok")
    monkeypatch.setenv("HOME", str(sb.root))
    for target in ("~/wts/sup", "$HOME/wts/sup", "${HOME}/wts/sup", '"$HOME/wts/sup"', '"$HOME"/wts/sup', "~/wts/su*"):
        assert decide(sb, f"rm -rf {target}", cwd=Path("/")).decision == "deny", target
    for target in ("~/wts/ok", "$HOME/wts/ok", "${HOME}/wts/ok", '"$HOME/wts/ok"', "~/wts/o*"):
        assert decide(sb, f"rm -rf {target}", cwd=Path("/")).decision == "allow", target  # resolved, judged, SAFE
    assert decide(sb, "rm -rf ~/nothing-here", cwd=Path("/")).decision == "pass"


@pytest.mark.parametrize(
    "target",
    ['"$WT"', "$WT", "${WT}/x", '"$(pwd)"', "`pwd`", "{a,b}", "~other/x", '"$HOME/$X"', "~/$X", '"${X:-/tmp}"'],
)
def test_a_target_the_guard_cannot_resolve_is_denied(sb, target):
    d = decide(sb, f"rm -rf {target}")
    assert d.decision == "deny" and "rm" in d.reason, d.reason


def test_a_loop_over_worktrees_is_denied(sb):
    superseded(sb, "sup")
    d = decide(sb, "for d in wts/*; do rm -rf \"$d\"; done", cwd=sb.root)
    assert d.decision == "deny"


@pytest.mark.parametrize(
    "command",
    [
        "ls | xargs rm -rf",
        "find . -name x -print0 | xargs -0 rm -rf",
        "find . -name x -exec rm -rf {{}} +",
        "find . -exec rm -r {{}} \\;",
        "parallel rm -rf ::: a b",
    ],
)
def test_targets_fed_from_elsewhere_are_denied(sb, command):
    assert decide(sb, command).decision == "deny"


@pytest.mark.parametrize(
    "template",
    [
        "sudo rm -rf {p}",
        "sudo -u me rm -rf {p}",
        "command rm -rf {p}",
        "env FOO=1 rm -rf {p}",
        "nice -n 5 rm -rf {p}",
        "time rm -rf {p}",
        "nohup rm -rf {p}",
        "exec rm -rf {p}",
        "timeout 5 rm -rf {p}",
        "/bin/rm -rf {p}",
        "\\rm -rf {p}",
        "'rm' -rf {p}",
        "if true; then rm -rf {p}; fi",
        "true && rm -rf {p}",
        "! rm -rf {p}",
    ],
)
def test_wrappers_and_chains_do_not_hide_an_unsafe_rm(sb, template):
    wt, _ = superseded(sb, "sup")
    assert decide(sb, template.format(p=wt)).decision == "deny", template


@pytest.mark.parametrize(
    "template",
    ["bash -c 'rm -rf {p}'", 'sh -c "rm -rf {p}"', "eval 'rm -rf {p}'", "eval rm -rf {p}", "ssh host rm -rf {p}"],
)
def test_executors_running_an_rm_string_are_denied(sb, template):
    wt, _ = superseded(sb, "sup")
    assert decide(sb, template.format(p=wt)).decision == "deny"


def test_an_rm_in_a_command_substitution_is_denied(sb):
    wt, _ = superseded(sb, "sup")
    assert decide(sb, f"echo $(rm -rf {wt})").decision == "deny"


def test_a_cd_earlier_in_the_chain_makes_relative_targets_unknowable(sb):
    superseded(sb, "sup")
    assert decide(sb, f"cd {sb.wt_dir} && rm -rf sup").decision == "deny"
    assert decide(sb, f"cd {sb.wt_dir}; rm -rf ./sup").decision == "deny"
    assert decide(sb, f"pushd {sb.wt_dir} && rm -rf sup && popd").decision == "deny"
    assert decide(sb, f"(cd {sb.wt_dir} && rm -rf sup)").decision == "deny"
    d = decide(sb, f"cd {sb.wt_dir} && rm -rf build")
    assert d.decision == "deny" and "cd" in d.reason


def test_absolute_targets_after_a_cd_are_still_judged(sb):
    wt, _ = superseded(sb, "sup")
    assert decide(sb, f"cd /tmp && rm -rf {wt}").decision == "deny"
    ok = landed_worktree(sb, "ok")
    assert decide(sb, f"cd /tmp && rm -rf {ok}").decision == "allow"
    assert decide(sb, "cd /tmp && rm -rf /tmp/nothing-of-ours").decision == "pass"


def test_a_symlink_to_a_worktree_does_not_hide_it(sb, tmp_path):
    wt, _ = superseded(sb, "sup")
    link = tmp_path / "link-to-sup"
    link.symlink_to(wt)
    assert decide(sb, f"rm -rf {link}/").decision == "deny"
    assert decide(sb, f"rm -rf {link}").decision == "deny"
    parent_link = tmp_path / "link-to-wts"
    parent_link.symlink_to(sb.wt_dir)
    assert decide(sb, f"rm -rf {parent_link}/sup").decision == "deny"


def test_worktrees_of_the_repository_the_target_sits_in_are_found_from_any_cwd(sb, tmp_path):
    wt, _ = superseded(sb, "sup")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert decide(sb, f"rm -rf {wt}", cwd=elsewhere).decision == "deny"
    assert decide(sb, f"rm -rf {sb.wt_dir}", cwd=elsewhere).decision == "deny"


def test_a_worktree_of_another_repository_than_the_cwds_is_found(sb, tmp_path):
    (tmp_path / "other").mkdir()
    other = Sandbox(tmp_path / "other")
    wt, _ = superseded(other, "sup")
    assert decide(sb, f"rm -rf {wt}").decision == "deny"  # cwd is sb's primary, the target is other's worktree


# ── things that are NOT worktree removals ────────────────────────────────────────
@pytest.mark.parametrize(
    "command",
    [
        "rm -rf node_modules",
        "rm -rf build dist .cache",
        "rm -f file.txt",
        "rm file.txt",
        "rm -i x",
        "rm -rf",
        "rmdir {wt}",
        "rm -d {wt}",
        "rm -r {wt}/src",
        "rm -rf {wt}/node_modules {wt}/dist",
        "rm -rf {wt}/.git-notes",
        "rm -rf {primary}/untracked-dir",
        "rm -rf {root}/unrelated",
        "docker run --rm -it image sh",
        "docker run --rm image rm -rf /x",
        "git commit -m 'never rm -rf the worktree'",
        'echo "rm -rf {wt}"',
        "grep -r pattern {wt}",
        "ls -R {wt}",
        "git rm -r --cached {wt}/dir",
        "git rm -rf dir",
        "cp -r a b",
        "form -rf {wt}",
        "arm -rf {wt}",
        "perform -r {wt}",
        "rm -rf {wt}/../unrelated-sibling",
        "npm rm -r foo",
        "git commit -F - <<'EOF'\nrm -rf {wt}\nEOF",
    ],
)
def test_other_commands_pass(sb, command):
    wt, _ = superseded(sb, "sup")
    forge = FakeForge()
    d = decide(sb, command.format(wt=wt, primary=sb.primary, root=sb.root), forge=forge)
    assert d.decision == "pass", d.reason
    assert forge.calls == 0


def test_the_primary_checkout_itself_is_not_a_linked_worktree(sb):
    assert decide(sb, f"rm -rf {sb.primary}/some/dir").decision == "pass"


def test_a_missing_worktree_directory_has_nothing_to_lose(sb):
    wt, _ = superseded(sb, "sup")
    import shutil

    shutil.rmtree(wt)
    assert decide(sb, f"rm -rf {wt}").decision == "pass"
    assert decide(sb, f"rm -rf {sb.wt_dir}").decision == "pass"


def test_outside_any_repository_nothing_is_consulted(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    forge = FakeForge()
    d = rg.evaluate_command(f"rm -rf {plain}/x", plain, forge=forge, process_cwds=no_processes, fetch=False)
    assert d.decision == "pass" and forge.calls == 0


def test_the_verdict_is_the_one_rig_worktree_remove_uses(sb):
    """No second check: an rm and `git worktree remove` of the same tree agree, for every state."""
    ok = landed_worktree(sb, "ok")
    bad, _ = superseded(sb, "sup")
    for wt in (ok, bad):
        rm = decide(sb, f"rm -rf {wt}")
        remove = decide(sb, f"git worktree remove {wt}")
        assert (rm.decision, [v.safe for v in rm.verdicts]) == (remove.decision, [v.safe for v in remove.verdicts])


def test_fetch_refreshes_the_default_ref_only_when_a_worktree_is_actually_affected(sb, monkeypatch):
    calls: list[Path] = []
    monkeypatch.setattr(rg.rv, "refresh_default_ref", lambda repo: calls.append(repo) or [])
    superseded(sb, "sup")
    rg.evaluate_command("rm -rf build", sb.primary, forge=FakeForge(), process_cwds=no_processes, fetch=True)
    assert calls == []
    rg.evaluate_command(f"rm -rf {sb.wt_dir}/sup", sb.primary, forge=FakeForge(), process_cwds=no_processes, fetch=True)
    assert calls == [sb.primary]


def test_a_repository_git_cannot_list_fails_closed(sb, monkeypatch):
    wt, _ = superseded(sb, "sup")

    def broken(path, **kwargs):
        raise rg.worktree_gc.WorktreeGcError("git exploded")

    monkeypatch.setattr(rg.worktree_gc, "worktree_records", broken)
    d = decide(sb, f"rm -rf {wt}")
    assert d.decision == "deny"
    assert os.path.exists(wt)


def test_a_worktree_a_few_directories_down_is_found_from_outside_any_repository(sb, tmp_path):
    holder = tmp_path / "holder"
    deep = holder / "a" / "b" / "c" / "wt"
    git(sb.primary, "worktree", "add", "-q", "-b", "deepwt", str(deep), "main")
    sb.commit(deep, "d.txt", "d\n", "unlanded work")
    assert decide(sb, f"rm -rf {holder}", cwd=tmp_path).decision == "deny"
    assert decide(sb, f"rm -rf {holder}/a/b", cwd=tmp_path).decision == "deny"


def test_the_scan_for_nested_repositories_is_bounded(sb, tmp_path, monkeypatch):
    """A target so big that the entry cap is hit is only partly scanned: a documented limit."""
    holder = tmp_path / "holder"
    wt = holder / "wt"
    git(sb.primary, "worktree", "add", "-q", "-b", "capwt", str(wt), "main")
    sb.commit(wt, "d.txt", "d\n", "unlanded work")
    cwd = tmp_path  # not a repository: the worktree can only be found by scanning
    assert decide(sb, f"rm -rf {holder}", cwd=cwd).decision == "deny"
    monkeypatch.setattr(rg, "_SCAN_ENTRY_LIMIT", 0)
    assert decide(sb, f"rm -rf {holder}", cwd=cwd).decision == "pass"


def test_each_repository_is_refreshed_once_however_many_of_its_worktrees_are_affected(sb, monkeypatch):
    calls: list[Path] = []
    monkeypatch.setattr(rg.rv, "refresh_default_ref", lambda repo: calls.append(repo) or [])
    superseded(sb, "one")
    superseded(sb, "two")
    rg.evaluate_command(f"rm -rf {sb.wt_dir}", sb.primary, forge=FakeForge(), process_cwds=no_processes, fetch=True)
    assert calls == [sb.primary]


def test_the_primary_checkout_is_not_examined_so_a_directory_of_safe_worktrees_and_a_clone_is_allowed(sb):
    """Documented limit: only LINKED worktrees are judged. `rm -rf` of a directory that also holds a
    primary checkout is not refused for the primary's own local-only branches/stashes."""
    landed_worktree(sb, "ok")
    d = decide(sb, f"rm -rf {sb.root}")
    assert d.decision == "allow" and [v.target for v in d.verdicts] == [str(sb.wt_dir / "ok")]


def test_dot_dot_after_a_symlink_is_resolved_the_way_the_kernel_does(sb, tmp_path):
    """`link/..` is the PARENT OF THE LINK TARGET, not the directory holding the link."""
    holder = tmp_path / "holder"
    (holder / "deep" / "a").mkdir(parents=True)
    wt = holder / "deep" / "sup"
    git(sb.primary, "worktree", "add", "-q", "-b", "supwt", str(wt), "main")
    sb.commit(wt, "d.txt", "d\n", "unlanded work")
    link = tmp_path / "link"
    link.symlink_to(holder / "deep" / "a")
    assert not (tmp_path / "sup").exists()  # the LEXICAL reading of `link/../sup` names nothing
    assert decide(sb, f"rm -rf {link}/../sup", cwd=tmp_path).decision == "deny"
    assert decide(sb, "rm -rf link/../sup", cwd=tmp_path).decision == "deny"


def test_a_glob_matching_a_symlink_to_an_unsafe_worktree_is_denied(sb, tmp_path):
    wt, _ = superseded(sb, "sup")
    holder = tmp_path / "links"
    holder.mkdir()
    (holder / "wt-link").symlink_to(wt)
    assert decide(sb, "rm -rf links/wt-*", cwd=tmp_path).decision == "deny"
    assert decide(sb, f"rm -rf {holder}/wt-?ink", cwd=tmp_path).decision == "deny"

