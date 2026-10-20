"""`rig worktree verdict` / `guard` / `remove` — the user-facing entry points of the landed-removal
verdict (real git + a fake forge; no network).

`remove` must refuse EVERY data-loss verdict (dirty, unlanded, live, …) — `--force` included — and
accept a worktree wherever git knows it, not only under `.worktrees/`.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
from removal_helpers import FakeForge, Sandbox, git, no_processes

from riglib import errors, removal_verdict as rv, worktree
from riglib.cli import main


@pytest.fixture
def sb(tmp_path, monkeypatch) -> Sandbox:
    sandbox = Sandbox(tmp_path)
    monkeypatch.setattr(rv, "default_forge_lookup", FakeForge())
    return sandbox


def landed(sb: Sandbox, name: str) -> Path:
    wt = sb.worktree(name)
    sb.commit(wt, f"{name}.txt", "x\n", f"feat {name}")
    sb.land_merge_commit(name)
    sb.fetch()
    return wt


def branch_exists(sb: Sandbox, name: str) -> bool:
    return bool(git(sb.primary, "branch", "--list", name))


# ── worktree.remove (API) ────────────────────────────────────────────────────────
def test_remove_accepts_a_worktree_outside_dot_worktrees_by_path(sb):
    wt = landed(sb, "far")
    res = worktree.remove(sb.primary, str(wt), process_cwds=no_processes)
    assert res.status == "removed", res.message
    assert not wt.exists() and not branch_exists(sb, "far")


def test_remove_accepts_a_worktree_outside_dot_worktrees_by_directory_name_or_branch(sb):
    wt = landed(sb, "byname")
    res = worktree.remove(sb.primary, "byname", process_cwds=no_processes)
    assert res.status == "removed", res.message
    assert not wt.exists()
    wt2 = sb.worktree("dirname", branch="feature/by-branch")
    res2 = worktree.remove(sb.primary, "feature/by-branch", process_cwds=no_processes)
    assert res2.status == "removed", res2.message
    assert not wt2.exists() and not branch_exists(sb, "feature/by-branch")


@pytest.mark.parametrize("force", [False, True])
def test_remove_refuses_a_dirty_worktree_even_with_force(sb, force):
    wt = landed(sb, "dirty")
    (wt / "precious.txt").write_text("mine\n")
    res = worktree.remove(sb.primary, str(wt), force=force, process_cwds=no_processes)
    assert res.status == "error" and res.exit_code == errors.EXIT_REMOVAL_UNSAFE
    assert "dirty-worktree" in res.message
    assert (wt / "precious.txt").read_text() == "mine\n" and branch_exists(sb, "dirty")


@pytest.mark.parametrize("force", [False, True])
def test_remove_refuses_unlanded_commits_even_with_force(sb, force):
    wt = sb.worktree("unlanded")
    sb.commit(wt, "a.txt", "a\n", "only here")
    res = worktree.remove(sb.primary, str(wt), force=force, process_cwds=no_processes)
    assert res.status == "error" and res.exit_code == errors.EXIT_REMOVAL_UNSAFE
    assert "not-landed" in res.message
    assert wt.exists() and branch_exists(sb, "unlanded")


def test_remove_refuses_a_live_process_cwd(sb):
    wt = landed(sb, "livecwd")
    res = worktree.remove(sb.primary, str(wt), process_cwds=lambda: [wt])
    assert res.exit_code == errors.EXIT_REMOVAL_UNSAFE and "live-process" in res.message
    assert wt.exists()


def test_remove_force_unlocks_a_locked_but_safe_worktree_and_plain_remove_does_not(sb):
    wt = landed(sb, "locked")
    git(sb.primary, "worktree", "lock", str(wt))
    plain = worktree.remove(sb.primary, str(wt), process_cwds=no_processes)
    assert plain.status == "error" and plain.exit_code == errors.EXIT_CONFIG and wt.exists()
    forced = worktree.remove(sb.primary, str(wt), force=True, process_cwds=no_processes)
    assert forced.status == "removed", forced.message
    assert not wt.exists()


def test_remove_verdict_is_attached_to_the_result(sb):
    wt = sb.worktree("attached")
    sb.commit(wt, "a.txt", "a\n", "x")
    res = worktree.remove(sb.primary, str(wt), process_cwds=no_processes)
    assert res.verdict is not None and not res.verdict.safe


def test_remove_unknown_target_is_a_config_error(sb):
    res = worktree.remove(sb.primary, "ghost", process_cwds=no_processes)
    assert res.status == "error" and res.exit_code == errors.EXIT_CONFIG
    assert "not a working tree" in res.message


def test_remove_ambiguous_directory_name_is_refused_not_guessed(sb):
    first = sb.worktree("dup", branch="dup-one")
    nested = sb.wt_dir / "nested"
    nested.mkdir()
    git(sb.primary, "worktree", "add", "-q", "-b", "dup-two", str(nested / "dup"), "origin/main")
    res = worktree.remove(sb.primary, "dup", process_cwds=no_processes)
    assert res.status == "error" and "ambiguous" in res.message
    assert first.exists() and (nested / "dup").exists()


# ── CLI: verdict ─────────────────────────────────────────────────────────────────
def run_cli(argv: list[str], capsys) -> tuple[int, str]:
    code = main(argv)
    return code, capsys.readouterr().out


def test_cli_verdict_safe_exits_zero(sb, capsys):
    wt = landed(sb, "v-safe")
    code, out = run_cli(["worktree", "verdict", "-C", str(sb.primary), "--no-fetch", str(wt)], capsys)
    assert code == 0 and "SAFE" in out and "UNSAFE" not in out


def test_cli_verdict_unsafe_exits_ten_with_machine_readable_json(sb, capsys):
    wt = sb.worktree("v-unsafe")
    (wt / "x.txt").write_text("x\n")
    code, out = run_cli(["worktree", "verdict", "-C", str(sb.primary), "--no-fetch", "--json", str(wt)], capsys)
    assert code == errors.EXIT_REMOVAL_UNSAFE
    data = json.loads(out)
    assert data["safe"] is False and data["kind"] == "worktree"
    assert any(r["code"] == "dirty-worktree" for r in data["reasons"])


def test_cli_verdict_resolves_a_local_branch_and_a_remote_branch(sb, capsys):
    wt = landed(sb, "v-branch")
    sb.push(wt, "v-branch")
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    code, out = run_cli(["worktree", "verdict", "-C", str(sb.primary), "--no-fetch", "v-branch"], capsys)
    assert code == 0 and "local-branch" in out
    code, out = run_cli(["worktree", "verdict", "-C", str(sb.primary), "--no-fetch", "--remote", "origin", "--json", "v-branch"], capsys)
    assert code == 0 and json.loads(out)["kind"] == "remote-branch"


def test_cli_verdict_unknown_target_is_a_config_error(sb, capsys):
    code, out = run_cli(["worktree", "verdict", "-C", str(sb.primary), "--no-fetch", "nope"], capsys)
    assert code == errors.EXIT_CONFIG


def test_cli_verdict_fetches_by_default(sb, capsys):
    wt = sb.worktree("v-fetch")
    sb.commit(wt, "a.txt", "a\n", "feat")
    sb.land_ff("v-fetch")  # origin moved; the local origin/main is stale until the verdict refreshes it
    code, _ = run_cli(["worktree", "verdict", "-C", str(sb.primary), str(wt)], capsys)
    assert code == 0


# ── CLI: remove ──────────────────────────────────────────────────────────────────
def test_cli_remove_by_path_outside_dot_worktrees(sb, capsys):
    wt = landed(sb, "cli-far")
    code, out = run_cli(["worktree", "remove", "-C", str(sb.primary), str(wt)], capsys)
    assert code == 0, out
    assert not wt.exists() and not branch_exists(sb, "cli-far")


def test_cli_remove_refuses_unsafe_and_force_cannot_override(sb, capsys):
    wt = sb.worktree("cli-bad")
    sb.commit(wt, "a.txt", "a\n", "unmerged")
    for extra in ([], ["--force"]):
        code, out = run_cli(["worktree", "remove", "-C", str(sb.primary), *extra, str(wt)], capsys)
        assert code == errors.EXIT_REMOVAL_UNSAFE, out
    assert wt.exists() and branch_exists(sb, "cli-bad")


# ── CLI: guard ───────────────────────────────────────────────────────────────────
def guard(sb: Sandbox, command: str, monkeypatch, capsys) -> dict:
    monkeypatch.setattr(sys, "stdin", io.StringIO(command))
    code = main(["worktree", "guard", "--cwd", str(sb.primary), "--no-fetch"])
    out = capsys.readouterr().out
    assert code == 0, out
    return json.loads(out)


def test_cli_guard_emits_one_json_object_for_each_decision(sb, monkeypatch, capsys):
    assert guard(sb, "ls", monkeypatch, capsys)["decision"] == "pass"
    wt = sb.worktree("g-bad")
    sb.commit(wt, "a.txt", "a\n", "unmerged")
    bad = guard(sb, f"git worktree remove --force {wt}", monkeypatch, capsys)
    assert bad["decision"] == "deny" and bad["targets"][0]["safe"] is False and "rig worktree verdict" in bad["reason"]
    ok = landed(sb, "g-ok")
    assert guard(sb, f"git worktree remove {ok}", monkeypatch, capsys)["decision"] == "allow"


def test_cli_guard_denies_when_the_command_cannot_be_read(sb, monkeypatch, capsys):
    class Broken:
        def read(self):
            raise OSError("stdin closed")

    monkeypatch.setattr(sys, "stdin", Broken())
    code = main(["worktree", "guard", "--cwd", str(sb.primary)])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["decision"] == "deny"


def test_cli_guard_denies_on_a_bad_cwd(sb, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sys, "stdin", io.StringIO("git branch -D x"))
    code = main(["worktree", "guard", "--cwd", str(tmp_path / "missing"), "--no-fetch"])
    assert code == 0 and json.loads(capsys.readouterr().out)["decision"] == "deny"


def test_installed_entry_point_runs_end_to_end(sb, tmp_path):
    """The real `rig` executable, a real subprocess: the shape the hook actually spawns."""
    rig = Path(__file__).resolve().parents[1] / "bin" / "rig"
    wt = sb.worktree("e2e-bad")
    sb.commit(wt, "a.txt", "a\n", "unmerged")
    res = subprocess.run(
        [str(rig), "worktree", "guard", "--cwd", str(sb.primary), "--no-fetch"],
        input=f"git worktree remove --force {wt}",
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout)["decision"] == "deny"


# ── review findings ──────────────────────────────────────────────────────────────
def test_a_file_that_appears_after_the_verdict_still_blocks_the_removal(sb):
    """The verdict and the delete are not atomic — the delete re-checks right before mutating."""
    wt = landed(sb, "late-file")

    def scan_then_write_late() -> list[Path]:
        (wt / "late.txt").write_text("written after the status check\n")
        return []

    res = worktree.remove(sb.primary, str(wt), process_cwds=scan_then_write_late)
    assert res.status == "error" and res.exit_code == errors.EXIT_REMOVAL_UNSAFE
    assert (wt / "late.txt").exists() and branch_exists(sb, "late-file")


def test_a_commit_that_appears_after_the_verdict_still_blocks_the_removal(sb):
    wt = landed(sb, "late-commit")

    def scan_then_commit() -> list[Path]:
        sb.commit(wt, "late.txt", "late\n", "committed after the verdict")
        return []

    res = worktree.remove(sb.primary, str(wt), process_cwds=scan_then_commit)
    assert res.status == "error" and res.exit_code == errors.EXIT_REMOVAL_UNSAFE
    assert wt.exists() and branch_exists(sb, "late-commit")


def test_verdict_for_a_target_that_is_neither_worktree_nor_branch_exits_two(sb, capsys, tmp_path):
    stray = tmp_path / "stray"
    stray.mkdir()
    for target in (str(stray), "no-such-branch"):
        code, _ = run_cli(["worktree", "verdict", "-C", str(sb.primary), "--no-fetch", target], capsys)
        assert code == errors.EXIT_CONFIG, target


def test_verdict_missing_branch_exits_two_even_without_origin_head(sb, capsys):
    git(sb.primary, "remote", "set-head", "origin", "-d")
    code, _ = run_cli(["worktree", "verdict", "-C", str(sb.primary), "--no-fetch", "no-such-branch"], capsys)
    assert code == errors.EXIT_CONFIG


def test_a_differently_cased_path_is_not_the_same_worktree_on_a_case_sensitive_filesystem(sb, tmp_path):
    probe = tmp_path / "CaseProbe"
    probe.write_text("")
    if (tmp_path / "caseprobe").exists():
        pytest.skip("case-insensitive filesystem")
    wt = landed(sb, "Foo")
    res = worktree.remove(sb.primary, str(wt.parent / "foo"), process_cwds=no_processes)
    assert res.status == "error" and "not a working tree" in res.message
    assert wt.exists()


def test_guard_accepts_the_short_dash_c_flag(sb, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("ls"))
    code = main(["worktree", "guard", "-C", str(sb.primary), "--no-fetch"])
    assert code == 0 and json.loads(capsys.readouterr().out)["decision"] == "pass"


def test_a_branch_ref_moved_after_the_verdict_blocks_the_removal(sb):
    """`git update-ref` moves the branch (and therefore the worktree's HEAD) behind our back."""
    wt = landed(sb, "late-ref")
    moved_to = git(sb.primary, "rev-parse", "origin/main")

    def scan_then_move_ref() -> list[Path]:
        newer = git(sb.primary, "commit-tree", "-m", "newer", "-p", moved_to, f"{moved_to}^{{tree}}")
        git(sb.primary, "update-ref", "refs/heads/late-ref", newer)
        return []

    res = worktree.remove(sb.primary, str(wt), process_cwds=scan_then_move_ref)
    assert res.status == "error" and res.exit_code == errors.EXIT_REMOVAL_UNSAFE
    assert wt.exists() and branch_exists(sb, "late-ref")


def test_a_branch_name_is_not_mistaken_for_a_path_that_happens_to_exist_in_the_cwd(sb, monkeypatch, capsys):
    wt = sb.worktree("dirname-differs", branch="feature/x")
    (sb.primary / "feature" / "x").mkdir(parents=True)  # a SOURCE directory with the branch's name
    monkeypatch.chdir(sb.primary)
    code, out = run_cli(["worktree", "remove", "-C", str(sb.primary), "--no-fetch", "feature/x"], capsys)
    assert code == 0, out
    assert not wt.exists()


def test_a_relative_path_prefers_the_callers_directory_over_the_repo_root(sb, tmp_path):
    """`../x` means the shell's `../x` (cwd-relative) even when the repo-root reading also names a
    registered worktree; two registered readings never silently fall through to name matching."""
    here = sb.wt_dir / "sub" / "deeper"
    here.mkdir(parents=True)
    wt_a = sb.wt_dir / "sub" / "x"
    git(sb.primary, "worktree", "add", "-q", "-b", "x-a", str(wt_a), "main")
    wt_b = sb.root / "x"
    git(sb.primary, "worktree", "add", "-q", "-b", "x-b", str(wt_b), "main")
    path, err = worktree.resolve_target(sb.primary, "../x", cwd=here)
    assert err is None and path == wt_a


def test_a_deleted_cwd_does_not_crash_remove_or_verdict(sb, tmp_path, monkeypatch, capsys):
    """A shell left standing in a worktree that was deleted by hand still needs `rig worktree remove`."""
    gone = tmp_path / "gone-cwd"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    wt = sb.worktree("stale-dir")
    code, out = run_cli(["worktree", "verdict", "-C", str(sb.primary), "--no-fetch", "stale-dir"], capsys)
    assert code == 0, out
    code, out = run_cli(["worktree", "remove", "-C", str(sb.primary), "--no-fetch", "stale-dir"], capsys)
    assert code == 0, out
    assert not wt.exists()


def test_cli_verdict_remote_reports_safe_for_a_superseded_branch_and_names_the_archive_ref(sb, capsys):
    wt = sb.worktree("superseded")
    sb.commit(wt, "s.txt", "s\n", "feat superseded")
    sb.push(wt, "superseded")
    git(wt, "push", "-q", "origin", "HEAD:refs/heads/archive/superseded-prs")
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    code, out = run_cli(
        ["worktree", "verdict", "-C", str(sb.primary), "--no-fetch", "--remote", "origin", "--json", "superseded"], capsys
    )
    data = json.loads(out)
    assert code == 0 and data["safe"] is True and data["landed_by"] == "preserved"
    assert any("origin/archive/superseded-prs" in note for note in data["notes"])


def test_cli_verdict_remote_still_refuses_a_branch_nothing_else_holds(sb, capsys):
    wt = sb.worktree("lonely")
    sb.commit(wt, "l.txt", "l\n", "feat lonely")
    sb.push(wt, "lonely")
    code, out = run_cli(
        ["worktree", "verdict", "-C", str(sb.primary), "--no-fetch", "--remote", "origin", "--json", "lonely"], capsys
    )
    assert code == errors.EXIT_REMOVAL_UNSAFE and json.loads(out)["safe"] is False
