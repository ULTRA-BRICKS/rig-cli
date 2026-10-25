"""The PreToolUse removal guard: parse a bash command conservatively, run the landed-removal
verdict on EVERY target of `git worktree remove`, `git branch -d/-D`, `git push --delete`, and
allow only when all are SAFE. Anything it cannot parse with certainty is a DENY, never a guess.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from removal_helpers import FakeForge, Sandbox, git, keep_on, no_processes, superseded

from riglib import removal_guard as rg


@pytest.fixture
def sb(tmp_path) -> Sandbox:
    return Sandbox(tmp_path)


def decide(sb: Sandbox, command: str, cwd: Path | None = None, forge: FakeForge | None = None) -> rg.GuardDecision:
    return rg.evaluate_command(
        command,
        cwd or sb.primary,
        forge=forge or FakeForge(),
        process_cwds=no_processes,
        fetch=False,
    )


def landed_worktree(sb: Sandbox, name: str = "done") -> Path:
    wt = sb.worktree(name)
    sb.commit(wt, f"{name}.txt", "x\n", f"feat {name}")
    sb.land_merge_commit(name)
    sb.fetch()
    return wt


# ── not a removal: stay silent ───────────────────────────────────────────────────
@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "git status",
        "git branch --list",
        "git branch -r",
        "git branch -r -d origin/gone",  # deleting a remote-TRACKING ref loses nothing the remote lacks
        "git push origin main",
        "git push -u origin HEAD:refs/heads/feature",
        "git worktree list",
        "git worktree add .worktrees/x -b x",
        "git worktree prune",
        'git commit -m "docs: git worktree remove and git branch -D explained"',
        "git commit -F - <<'EOF'\ndocs\n\nrun `git branch -D x` and don't `git worktree remove`\nEOF",
        'echo "git branch -D x"',
        "gh pr create --title t --body 'then git worktree remove ../x'",
        'git status && echo "git push origin --delete x"',
        "git log --oneline | head -3",
        "echo $HOME",
        'grep -c git "$f"',
        "echo git $HOME",
        "$EDITOR $FILE",
        'git branch -m "$OLD" "$NEW"',
        'git branch --contains "$SHA"',
        "git push -od origin HEAD",
        'git -C "$WT" status',
        'git -C "$WT" log --oneline',
        "git -c x=$V commit -m msg",
        "git --git-dir=$D log",
        'source "$VIRTUAL_ENV/bin/activate"',
        '. "$HOME/.cargo/env"',
        "git branch -d --l x",
        'git branch "hotfix-$N"',
        'git branch "fix/$TICKET" origin/main',
        'git -C "$WT" branch --show-current',
        'git -C "$WT" worktree list',
        'git -C "$WT" push --dry-run origin HEAD',
        'git -C "$WT" update-ref refs/tags/v1 HEAD',
        "git update-ref -m'drop stale' refs/tags/v1 HEAD",
        'bash "$HOME/run.sh"',
        'git push --force-with-lease=main:$OLD_SHA origin main',
        "git push --push-option=ci.var=$X origin HEAD",
        "git branch --set-upstream-to=origin/$BRANCH",
        "git update-ref refs/heads/x HEAD",
        "git update-ref refs/tags/v1 HEAD",
        'git commit -m "$(git rev-parse HEAD)"',
        "git checkout -b feature/$USER",
        "git commit -m \"$(cat <<'EOF'\nfix: don't run `git branch -D x` or git worktree remove here\nEOF\n)\"",
        "gh pr create --body \"$(cat <<'EOF'\nit's about git push origin --delete x\nEOF\n)\"",
    ],
)
def test_non_removal_commands_pass(sb, command):
    d = decide(sb, command)
    assert d.decision == "pass", d.reason


# ── allow: simple forms with every target SAFE ───────────────────────────────────
def test_allows_worktree_remove_of_a_landed_clean_worktree_outside_dot_worktrees(sb):
    wt = landed_worktree(sb)
    d = decide(sb, f"git worktree remove {wt}")
    assert d.decision == "allow", d.reason
    assert d.verdicts[0].safe


@pytest.mark.parametrize("flags", ["--force", "-f", "-f -f", "--force --force"])
def test_force_flag_never_bypasses_the_verdict(sb, flags):
    wt = landed_worktree(sb, "forced")
    (wt / "dirty.txt").write_text("precious\n")
    d = decide(sb, f"git worktree remove {flags} {wt}")
    assert d.decision == "deny"
    assert "dirty" in d.reason.lower()


def test_allows_force_when_the_worktree_is_actually_safe(sb):
    wt = landed_worktree(sb, "forced-ok")
    assert decide(sb, f"git worktree remove --force {wt}").decision == "allow"


def test_allows_dash_c_form_and_relative_path(sb):
    wt = landed_worktree(sb, "rel")
    assert decide(sb, f"git -C {sb.primary} worktree remove {wt}", cwd=sb.root).decision == "allow"
    assert decide(sb, "git worktree remove ../wts/rel", cwd=sb.primary).decision == "allow"


def test_allows_branch_delete_of_a_landed_branch(sb):
    wt = landed_worktree(sb, "br")
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    for flag in ("-D", "-d", "--delete", "-df", "-fd", "--delete --force"):
        git(sb.primary, "branch", "-f", "br", "origin/main")  # recreate (landed: == origin/main)
        d = decide(sb, f"git branch {flag} br")
        assert d.decision == "allow", (flag, d.reason)


def test_redirections_are_not_arguments(sb):
    wt = landed_worktree(sb, "redir")
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    d = decide(sb, "git branch -D redir </dev/null >/dev/null 2>&1")
    assert d.decision == "allow", d.reason
    assert [v.target for v in d.verdicts] == ["redir"]


def test_allows_multiple_branch_targets_when_all_are_safe(sb):
    for name in ("m1", "m2"):
        wt = landed_worktree(sb, name)
        git(sb.primary, "worktree", "remove", "--force", str(wt))
    assert decide(sb, "git branch -D m1 m2").decision == "allow"


@pytest.mark.parametrize(
    "template",
    [
        "git push origin --delete {b}",
        "git push --delete origin {b}",
        "git push -d origin {b}",
        "git push origin :{b}",
        "git push origin --delete refs/heads/{b}",
        "git push origin :refs/heads/{b}",
    ],
)
def test_allows_remote_delete_of_a_landed_branch(sb, template):
    wt = sb.worktree("rm")
    sb.commit(wt, "a.txt", "a\n", "feat")
    sb.push(wt, "rm")
    sb.land_ff("rm")
    sb.fetch()
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    d = decide(sb, template.format(b="rm"))
    assert d.decision == "allow", d.reason


def test_allow_decisions_are_logged(sb, tmp_path, monkeypatch):
    home = tmp_path / "loghome"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    wt = landed_worktree(sb, "logged")
    decide(sb, f"git worktree remove {wt}")
    log = home / ".cache" / "rig" / "removal-guard.jsonl"
    rec = json.loads(log.read_text().splitlines()[-1])
    assert rec["decision"] == "allow" and rec["targets"][0]["target"].endswith("logged")


# ── deny: unsafe targets, with reasons and the sanctioned rescue ─────────────────
def test_denies_unlanded_worktree_with_rescue_instructions(sb):
    wt = sb.worktree("work")
    sb.commit(wt, "a.txt", "a\n", "unmerged work")
    d = decide(sb, f"git worktree remove --force {wt}")
    assert d.decision == "deny"
    assert "not-landed" in d.reason
    # no recovery pack: nothing here tells the agent to write a backup file (git's reflog is the way back)
    for backup in ("recovery", "git diff --binary", ".patch", "bundle"):
        assert backup not in d.reason
    assert "git push origin HEAD:refs/heads/" in d.reason
    assert "rig worktree verdict" in d.reason


def test_denies_unmerged_branch_delete(sb):
    wt = sb.worktree("ub")
    sb.commit(wt, "a.txt", "a\n", "unmerged")
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    d = decide(sb, "git branch -D ub")
    assert d.decision == "deny" and "not-landed" in d.reason


def test_denies_unmerged_remote_delete(sb):
    wt = sb.worktree("ur")
    sb.commit(wt, "a.txt", "a\n", "unmerged")
    sb.push(wt, "ur")
    d = decide(sb, "git push origin --delete ur")
    assert d.decision == "deny" and "not-landed" in d.reason


@pytest.mark.parametrize("flags", ["--delete", "-d", "--delete --force"])
def test_denies_delete_of_the_default_branch(sb, flags):
    assert decide(sb, f"git branch {flags} main").decision == "deny"
    assert decide(sb, "git push origin --delete main").decision == "deny"


def test_one_unsafe_target_among_several_denies_the_whole_command(sb):
    good = landed_worktree(sb, "good")
    git(sb.primary, "worktree", "remove", "--force", str(good))
    bad = sb.worktree("bad")
    sb.commit(bad, "a.txt", "a\n", "unmerged")
    git(sb.primary, "worktree", "remove", "--force", str(bad))
    d = decide(sb, "git branch -D good bad")
    assert d.decision == "deny"
    assert [v.safe for v in d.verdicts] == [True, False]


def test_denies_a_path_that_is_not_a_known_worktree(sb, tmp_path):
    stray = tmp_path / "stray"
    stray.mkdir()
    d = decide(sb, f"git worktree remove --force {stray}")
    assert d.decision == "deny" and "unknown-worktree" in d.reason


def test_denies_the_primary_checkout(sb):
    d = decide(sb, f"git worktree remove --force {sb.primary}")
    assert d.decision == "deny" and "primary-checkout" in d.reason


def test_denies_a_symlinked_path_to_a_safe_worktree(sb, tmp_path):
    wt = landed_worktree(sb, "sym")
    link = tmp_path / "alias"
    link.symlink_to(wt)
    d = decide(sb, f"git worktree remove {link}")
    assert d.decision == "deny" and "symlink" in d.reason.lower()


def test_denies_a_path_through_a_symlinked_parent(sb, tmp_path):
    wt = landed_worktree(sb, "symp")
    alias_dir = tmp_path / "aliasdir"
    alias_dir.symlink_to(wt.parent)
    d = decide(sb, f"git worktree remove {alias_dir}/symp")
    assert d.decision == "deny"


def test_denies_dotdot_that_escapes_to_an_unknown_path(sb):
    d = decide(sb, "git worktree remove ../../../../etc", cwd=sb.primary)
    assert d.decision == "deny"


# ── deny: forms that cannot be evaluated with certainty ──────────────────────────
@pytest.mark.parametrize(
    "command",
    [
        "git fetch && git branch -D x",
        "git branch -D x; git status",
        "git status || git branch -D x",
        "git branch -D x | cat",
        "git branch -D x & ",
        "git status\ngit branch -D x",
        "git branch -D x && git worktree remove /tmp/y && git push origin --delete x",
        "(git branch -D x)",
        "{ git branch -D x; }",
        "cd /tmp && git branch -D x",
        "echo $(git branch -D x)",
        'echo "$(git branch -D x)"',
        "echo `git branch -D x`",
        "bash -c 'git branch -D x'",
        'sh -c "git worktree remove /tmp/y"',
        "eval 'git branch -D x'",
        "xargs git branch -D",
        "sudo git branch -D x",
        "env FOO=1 git branch -D x",
        "FOO=1 git branch -D x",
        "time git branch -D x",
        "command git branch -D x",
        "nice git push origin --delete x",
        "find . -exec git branch -D x ;",
        "ssh host 'git branch -D x'",
        "bash <<EOF\ngit branch -D x\nEOF",
        "git branch -D $BRANCH",
        'git branch -D "$BRANCH"',
        "git branch -D ${BRANCH}",
        "git branch -D feat/*",
        "git branch -D 'feat/*'",
        "git branch -D feat?",
        "git branch -D feat[12]",
        "git branch -D {a,b}",
        "git branch -D ~/x",
        "git worktree remove ~/x",
        "git worktree remove $WT",
        "git worktree remove ../wts/*",
        "git worktree remove a b",
        "git worktree remove",
        "git --git-dir=/tmp/other/.git branch -D x",
        "git --git-dir /tmp/other/.git branch -D x",
        "git --work-tree=/tmp/o branch -D x",
        "git -c core.hooksPath=/dev/null branch -D x",
        "git -C $DIR branch -D x",
        "git push --mirror origin",
        "git push --prune origin",
        "git push origin --delete",
        "git push --delete",
        "git push origin :feat/*",
        'git branch -D "unterminated',
        "git branch -D x # && echo $(git branch -D y)",
        # redirection targets, here-strings and process substitutions still execute their contents
        'echo hi > "$(git branch -D x)"',
        "echo hi > $(git branch -D x)",
        "bash <<< 'git worktree remove ../wts/foo'",
        "bash < <(echo git branch -D x)",
        # an UNQUOTED heredoc expands $(…) in its body; a body belongs to the command that opened it
        "cat <<EOF\n$(git branch -D x)\nEOF",
        "cat <<EOF\n`git branch -D x`\nEOF",
        "bash <<EOF && true\ngit branch -D x\nEOF",
        # ${…} can carry a command substitution
        "echo ${X:-$(git branch -D x)}",
        'echo "${X:-$(git branch -D x)}"',
        # git accepts any unambiguous prefix of a long option
        "git push --mirr origin",
        "git push --pru origin",
        # expansions can assemble the removal after the lexer-free prefilter looks at it
        "git branch $'-D' x",
        "git${IFS}branch${IFS}-D${IFS}x",
        "git worktree${IFS}remove /tmp/p",
        'git branch "$FLAG" x',
        "git push origin $'--delete' x",
        # a subcommand or flag assembled from an expansion hides exactly the word the check needs
        "git $S -D x",
        "git $S origin --delete x",
        "git worktree $SUB --force /tmp/p",
        "git branch -$L x",
        "git branch --$L x",
        "git push --$L origin x",
        # wrappers and assignment prefixes get the SAME parsing as a direct git
        "sudo git $S -D x",
        "env git branch -$L x",
        "env git $S origin --delete x",
        "BR=-D git branch $BR feature",
        "env BR=-D git branch $BR feature",
        "nice git worktree $SUB /tmp/p",
        # a backtick word is as bare an expansion as $VAR
        "git branch `echo -D` x",
        'git branch "`echo -D`" x',
        # a dynamic command word does not hide the removal behind it
        "$G branch $'\\x2dD' x",
        "$G branch -$L x",
        "$G push origin --delete x",
        # unparseable AND able to assemble a removal fails closed
        'git branch $X "unterminated',
        # a non-mode flag (-q/-f/--quiet) does not state what the bare expansion means
        'git branch -q "$F" feature',
        'git branch -f "$F" feature',
        'git branch --quiet $F feature',
        "sudo git branch -f $F x",
        # deleting a branch ref by hand skips every landed check
        "git update-ref -d refs/heads/x",
        "git update-ref --delete refs/heads/x",
        "git update-ref --d refs/heads/x",
        "git update-ref -d refs/heads/x abc123",
        # more executors that run a git for you
        'git branch -v "$F" feature',
        "git branch hotfix-$N",
        'git -C /somewhere/else update-ref -d refs/heads/main',
        'git -C "$WT" update-ref -d refs/heads/feat',
        "exec $CMD",
        "env $CMD",
        "sudo $CMD",
        "git branch --sort=refname $F feature",
        "git branch --ignore-case $F feature",
        "git update-ref -m cleanup -d refs/heads/feat",
        "git update-ref -d HEAD",
        "git update-ref --stdin",
        "git update-ref --std",
        'git update-ref refs/heads/feat ""',
        "bash -c $'git branch \\x2dD x'",
        "git branch --format -r -D feature",
        "git branch --abbrev -D feature",
        "git branch feature${IFS}-D",
        'git branch "a"$F x',
        "git push origin x${IFS}--delete",
        'ssh host "$CMD"',
        'git -C "$WT" branch -D x',
        'git -C "$WT" worktree remove /tmp/p',
        "git branch --sort -refname -D feature",
        'git branch --format -m "$F" feature',
        "git branch --all $F feature",
        "git push --repo=origin :feature",
        "git push --repo origin :feature",
        "git push --repo=origin --delete feature",
        "git branch {-D,feature}",
        "git worktree {remove,/tmp/p}",
        "git push origin {--delete,feature}",
        "set -- ' '; git$1branch -D x",
        "git update-ref refs/heads/feature 0000000000000000000000000000000000000000",
        'git update-ref refs/heads/feature "$V"',
        'bash -c "$CMD"',
        'sudo bash -c "$CMD"',
        'eval "$CMD"',
        "bash -c \"git branch $'\\x2dD' feature\"",
        "flock /tmp/l git branch -D x",
        "caffeinate git push origin --delete x",
        "systemd-run --user git worktree remove /tmp/p",
    ],
)
def test_unparseable_or_compound_removal_forms_are_denied(sb, command):
    d = decide(sb, command)
    assert d.decision == "deny", (command, d.decision, d.reason)
    assert d.reason


def test_compound_with_a_comment_that_hides_nothing_still_checks_the_real_command(sb):
    wt = landed_worktree(sb, "cmt")
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    d = decide(sb, "git branch -D cmt # clean up")
    assert d.decision == "allow", d.reason


@pytest.mark.parametrize("name", ["-x", "--force", "a b", "refs/heads/main", "a..b"])
def test_option_looking_or_invalid_branch_names_never_reach_git(sb, name):
    import shlex

    d = decide(sb, f"git branch -D -- {shlex.quote(name)}")
    assert d.decision == "deny", (name, d.reason)
    assert "invalid-branch-name" in d.reason


def test_quoted_metacharacters_are_a_plain_name_not_executed(sb, tmp_path):
    marker = tmp_path / "pwned"
    d = decide(sb, f"git branch -D 'x;touch {marker}'")
    assert d.decision == "deny"
    assert not marker.exists()


def test_quoted_metacharacter_branch_that_is_landed_is_allowed_verbatim(sb):
    name = "feat$(x);`y`"
    wt = sb.worktree("mq", branch=name)
    sb.commit(wt, "a.txt", "a\n", "feat")
    sb.land_ff(name)
    sb.fetch()
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    d = decide(sb, "git branch -D 'feat$(x);`y`'")
    assert d.decision == "allow", d.reason


def test_outside_a_git_repository_a_recognized_removal_is_denied(sb, tmp_path):
    nowhere = tmp_path / "nowhere"
    nowhere.mkdir()
    d = decide(sb, "git branch -D x", cwd=nowhere)
    assert d.decision == "deny"


def test_internal_error_fails_closed(sb, monkeypatch):
    monkeypatch.setattr(rg, "_evaluate_segments", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    d = decide(sb, "git branch -D x")
    assert d.decision == "deny" and "boom" in d.reason


def test_forge_failure_is_fail_closed_when_only_the_pr_can_vouch_for_the_branch(sb):
    """The squash edited the branch's content on the way in, so no commit carries the branch's diff: only the merged
    PR (a forge fact) can say it landed. With the forge down the guard denies; with the PR it allows."""
    from removal_helpers import FakeForge, forge_down, pr

    wt = sb.worktree("ff")
    tip = sb.commit(wt, "a.txt", "a\n", "feat a")
    sb._integrator_branch("ff")
    git(sb.integrator, "merge", "-q", "--squash", "_incoming/ff")
    (sb.integrator / "a.txt").write_text("a, reworded by the maintainer while squashing\n", encoding="utf-8")
    git(sb.integrator, "commit", "-q", "-am", "squash ff")
    git(sb.integrator, "push", "-q", "origin", "main")
    merge = git(sb.integrator, "rev-parse", "HEAD")
    sb.fetch()
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    assert decide(sb, "git branch -D ff", forge=forge_down()).decision == "deny"
    assert decide(sb, "git branch -D ff", forge=FakeForge([pr("ff", head_oid=tip, merge_oid=merge)])).decision == "allow"


def test_decision_serializes_for_the_cli(sb):
    wt = sb.worktree("ser")
    sb.commit(wt, "a.txt", "a\n", "unmerged")
    d = decide(sb, f"git worktree remove {wt}")
    out = d.to_dict()
    assert out["decision"] == "deny"
    assert out["targets"][0]["kind"] == "worktree" and out["targets"][0]["safe"] is False
    json.dumps(out)


# ── property-style: hostile shell syntax never crashes the guard and never earns an allow ──
_FRAGMENTS = [
    "git", "branch", "-D", "-d", "worktree", "remove", "push", "origin", "--delete", "x", "y",
    "&&", "||", ";", "|", "&", "\n", "(", ")", "{", "}", "$(", "`", "'", '"', "\\", "$X", "*", "~",
    "<<EOF", "EOF", "#", ">", "2>&1", "bash -c", "sudo", "xargs", "--", "-C", "/tmp", ":x", " ", " ",
]


@pytest.mark.parametrize("seed", range(6))
def test_fuzzed_commands_never_raise_and_never_allow_outside_a_repo(tmp_path, seed):
    import random

    rng = random.Random(seed)
    nowhere = tmp_path / "nowhere"
    nowhere.mkdir()
    for _ in range(150):
        command = " ".join(rng.choice(_FRAGMENTS) for _ in range(rng.randint(1, 14)))
        d = rg.evaluate_command(command, nowhere, forge=FakeForge(), process_cwds=no_processes, fetch=False)
        assert d.decision in {"pass", "deny"}, (command, d)
        if not rg.needs_parse(command):
            assert d.decision == "pass", command


# ── review findings: option abbreviations, --signed, relative cwd, redirections, notes ──
def test_abbreviated_long_options_are_still_deletions(sb):
    wt = landed_worktree(sb, "abbr")
    sb.push(wt, "abbr")
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    for command in (
        "git branch --d abbr",
        "git branch --del abbr",
        "git branch --dele abbr",
        "git push origin --d abbr",
        "git push origin --dele abbr",
    ):
        d = decide(sb, command)
        assert d.decision == "allow" and d.verdicts, (command, d.reason)


def test_push_signed_does_not_swallow_the_remote(sb):
    wt = sb.worktree("signed")
    sb.commit(wt, "a.txt", "a\n", "unmerged")
    sb.push(wt, "signed")
    d = decide(sb, "git push --signed origin :signed")
    assert d.decision == "deny" and d.verdicts and not d.verdicts[0].safe


def test_a_relative_cwd_is_resolved_not_mistaken_for_a_symlink(sb, monkeypatch):
    wt = landed_worktree(sb, "relcwd")
    monkeypatch.chdir(sb.primary)
    d = rg.evaluate_command(
        "git worktree remove ../wts/relcwd", Path("."), forge=FakeForge(), process_cwds=no_processes, fetch=False
    )
    assert d.decision == "allow", d.reason
    assert wt.exists()


def test_redirections_and_quoted_heredocs_stay_data(sb):
    assert decide(sb, "git commit -F - <<'EOF'\n$(git branch -D x)\nEOF").decision == "pass"
    assert decide(sb, "cat <<'EOF' > notes.txt\ngit branch -D x\nEOF").decision == "pass"
    assert decide(sb, "echo 'git branch -D x' > notes.txt").decision == "pass"


def test_a_failed_refresh_fetch_is_visible_in_the_decision(sb):
    wt = landed_worktree(sb, "note")
    git(sb.primary, "remote", "set-url", "origin", str(sb.root / "gone.git"))
    d = rg.evaluate_command(
        f"git worktree remove {wt}", sb.primary, forge=FakeForge(), process_cwds=no_processes, fetch=True
    )
    assert d.decision == "allow" and "fetch" in d.reason.lower()


def test_a_cwd_reached_through_a_symlink_does_not_poison_relative_targets(sb, tmp_path):
    wt = landed_worktree(sb, "viasym")
    link = tmp_path / "lnk"
    link.symlink_to(sb.root)
    d = rg.evaluate_command(
        "git worktree remove ../wts/viasym", link / "primary",
        forge=FakeForge(), process_cwds=no_processes, fetch=False,
    )
    assert d.decision == "allow", d.reason
    assert wt.exists()


def test_ansi_c_unicode_escapes_are_decoded_before_the_check(sb):
    wt = landed_worktree(sb, "uni")
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    for command in ("git branch $'\\u002dD' uni", "git branch $'\\U0000002dD' uni", "git branch $'\\x2dD' uni", "git branch $'\\055D' uni"):
        d = decide(sb, command)
        assert d.decision == "allow" and d.verdicts, (command, d.reason)


def test_the_mention_prefilter_covers_every_option_abbreviation_the_parsers_accept(sb):
    """One table (rg._LONG_OPTIONS) drives the parsers; the regex must not drift from it. `--remotes`
    only SUPPRESSES a deletion, so it never needs to reach the parsers on its own."""
    shapes = {  # the command each overwrite/rm option is parsed in
        "--force": "git branch {} a b",
        "--move": "git branch {} a b",
        "--copy": "git branch {} a b",
        "--recursive": "rm {} dir",
        "--force-create": "git switch {} a",
    }
    for full, min_len in rg._LONG_OPTIONS.items():
        if full in ("--remotes", "--stdin", "--repo"):  # mode suppressor / update-ref-only / reached via `:`/--delete
            continue
        for n in range(min_len, len(full) + 1):
            prefix = full[:n]
            if full in shapes:
                assert rg.mentions_removal(shapes[full].format(prefix)), prefix
                continue
            assert rg.mentions_removal(f"git push origin {prefix} x"), prefix
            if full == "--delete":
                assert rg.mentions_removal(f"git branch {prefix} x"), prefix


def test_ansi_c_escapes_consume_one_to_four_hex_digits_like_bash(sb):
    """`$'\\u2d\\u2d'delete` is `--delete` in bash; an all-or-nothing decoder would miss it."""
    wt = landed_worktree(sb, "uni2")
    sb.push(wt, "uni2")
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    for command in (
        "git push origin $'\\u2d\\u2d'delete uni2",
        "git push origin $'\\x2d\\x2d'delete uni2",
        "git branch $'\\u2d\\x44' uni2",
    ):
        d = decide(sb, command)
        assert d.decision == "allow" and d.verdicts, (command, d.reason)


def test_locale_quoted_strings_are_literal_text(sb):
    wt = landed_worktree(sb, "locq")
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    d = decide(sb, 'git branch $"-D" locq')
    assert d.decision == "allow" and d.verdicts, d.reason


def test_the_guard_fails_closed_on_a_deleted_cwd_instead_of_raising(sb, tmp_path, monkeypatch):
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    d = rg.evaluate_command("git branch -D x", Path("."), forge=FakeForge(), process_cwds=no_processes, fetch=False)
    assert d.decision == "deny"


def test_update_ref_delete_of_a_tag_or_remote_tracking_ref_passes(sb):
    assert decide(sb, "git update-ref -d refs/tags/v1").decision == "pass"
    assert decide(sb, "git update-ref -d refs/remotes/origin/gone").decision == "pass"


def test_update_ref_that_does_not_touch_branches_passes(sb):
    assert decide(sb, "git update-ref refs/tags/v1 HEAD").decision == "pass"
    assert decide(sb, "git update-ref -m msg refs/tags/v1 HEAD").decision == "pass"
    assert decide(sb, "git update-ref -d refs/remotes/origin/gone").decision == "pass"


# ── preserved: commits that survive on another remote branch ─────────────────────
def superseded_with_archive(sb: Sandbox, name: str = "sup", archive: str = "archive/keep") -> Path:
    """A pushed branch that never lands on main, plus a second remote branch holding its commits."""
    wt, _ = superseded(sb, name)
    keep_on(sb, wt, archive)
    return wt


@pytest.mark.parametrize(
    "template",
    ["git push origin --delete {b}", "git push origin :{b}", "git branch -D {b}", "git branch -d {b}"],
)
def test_allows_deleting_a_branch_whose_commits_survive_on_another_remote_branch(sb, template):
    wt = superseded_with_archive(sb)
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    d = decide(sb, template.format(b="sup"))
    assert d.decision == "allow", d.reason
    assert d.verdicts[0].landed_by == "preserved"
    assert "origin/archive/keep" in d.reason
    assert "held by another remote branch" in d.reason


def test_allows_removing_a_clean_worktree_whose_commits_survive_on_another_remote_branch(sb):
    wt = superseded_with_archive(sb)
    d = decide(sb, f"git worktree remove {wt}")
    assert d.decision == "allow", d.reason


def test_denies_removing_a_dirty_worktree_even_when_its_commits_survive_elsewhere(sb):
    wt = superseded_with_archive(sb)
    (wt / "scratch.txt").write_text("uncommitted\n")
    d = decide(sb, f"git worktree remove --force {wt}")
    assert d.decision == "deny" and "dirty-worktree" in d.reason


@pytest.mark.parametrize(
    "both",
    [
        "git push origin --delete one two",
        "git push origin :one :two",
        "git push origin --delete refs/heads/one refs/heads/two",
        "git push origin :one :refs/heads/two",
        "git push origin --delete one refs/heads/two",
        "git push --force origin +:one :two",
    ],
)
def test_denies_deleting_the_only_two_branches_that_hold_the_commits_in_one_command(sb, both):
    wt = sb.worktree("one")
    sb.commit(wt, "one.txt", "x\n", "feat one")
    git(wt, "push", "-q", "origin", "HEAD:refs/heads/one", "HEAD:refs/heads/two")
    sb.fetch()
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    # either one alone is fine: the other still holds the commit
    assert decide(sb, "git push origin --delete one").decision == "allow"
    assert decide(sb, "git push origin --delete two").decision == "allow"
    # both in ONE command would leave nothing
    d = decide(sb, both)
    assert d.decision == "deny" and "not-landed" in d.reason
    assert not any(v.safe for v in d.verdicts)


def test_denies_deleting_the_branch_and_the_archive_that_preserves_it_together(sb):
    wt = superseded_with_archive(sb)
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    d = decide(sb, "git push origin --delete sup archive/keep")
    assert d.decision == "deny" and "not-landed" in d.reason
    assert "the same command also deletes archive/keep" in d.reason


def test_local_branch_deletes_never_remove_the_remote_holders(sb):
    """`git branch -D` touches no remote ref, so naming the archive in the same command changes nothing."""
    wt = superseded_with_archive(sb)
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    git(sb.primary, "branch", "archive/keep", "origin/archive/keep")
    d = decide(sb, "git branch -D sup archive/keep")
    assert d.decision == "allow", d.reason


@pytest.mark.parametrize(
    "command",
    [
        "git push origin :sup main:refs/heads/archive/keep",
        "git push --force origin :sup HEAD:refs/heads/archive/keep",
        "git push origin +HEAD~1:refs/heads/archive/keep :sup",
        "git push origin :sup archive/keep",
    ],
)
def test_denies_a_push_that_deletes_a_branch_and_rewrites_another_ref(sb, command):
    """The holder that vouches for `sup` could be the very ref this push moves."""
    wt = superseded_with_archive(sb)
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    d = decide(sb, command)
    assert d.decision == "deny" and "own command" in d.reason


def test_a_tag_deletion_beside_an_ordinary_push_is_not_a_branch_removal(sb):
    assert decide(sb, "git push origin :refs/tags/v1 main").decision == "pass"


def test_the_deny_text_names_the_route_that_works(sb):
    wt = sb.worktree("lone")
    sb.commit(wt, "a.txt", "a\n", "unmerged and unarchived")
    sb.push(wt, "lone")
    d = decide(sb, "git push origin --delete lone")
    assert d.decision == "deny"
    assert "push the commits to a branch that is not being deleted" in d.reason
    assert "archive/" in d.reason


def test_the_deny_text_pushes_to_the_remote_the_branch_is_deleted_from(sb):
    git(sb.primary, "remote", "add", "upstream", str(sb.origin))
    wt = sb.worktree("lone-up")
    tip = sb.commit(wt, "a.txt", "a\n", "unmerged and unarchived")
    sb.push(wt, "lone-up")
    git(sb.primary, "fetch", "-q", "upstream")
    d = decide(sb, "git push upstream --delete lone-up")
    assert d.decision == "deny"
    assert f"git push upstream {tip}:refs/heads/archive/lone-up-{tip[:8]}" in d.reason


def test_the_rescue_names_an_archive_branch_unique_to_the_commit(sb):
    """A reused worktree/branch name must never lead to a push over an earlier archive branch."""
    wt = sb.worktree("work")
    tip = sb.commit(wt, "a.txt", "a\n", "unmerged and unarchived")
    d = decide(sb, f"git worktree remove {wt}")
    assert d.decision == "deny"
    assert f"git push origin HEAD:refs/heads/archive/work-{tip[:8]}" in d.reason
    assert f"inside {wt}" in d.reason


# ── the denial explains itself (rig-cli#757) ─────────────────────────────────────
def test_the_deny_text_has_found_why_and_numbered_steps(sb):
    wt = sb.worktree("work")
    sb.commit(wt, "a.txt", "a\n", "feat: unmerged work")
    d = decide(sb, f"git worktree remove {wt}")
    assert d.decision == "deny"
    for heading in ("WHAT I FOUND", "WHY I STOPPED", "WHAT TO DO"):
        assert heading in d.reason
    assert "1 commit(s)" in d.reason and "feat: unmerged work" in d.reason and "a.txt" in d.reason
    assert "  1. " in d.reason and "  2. " in d.reason and "  3. " in d.reason
    assert "rig worktree discard" in d.reason and "--reason" in d.reason
    assert f"git -C {sb.primary} worktree remove --force {wt}" in d.reason  # what the owner can run themselves
    assert "Do NOT look for another way" in d.reason


def test_a_dirty_worktree_is_told_to_commit_not_to_ask_for_a_discard(sb):
    wt = sb.worktree("dirty")
    (wt / "scratch.txt").write_text("uncommitted\n")
    d = decide(sb, f"git worktree remove {wt}")
    assert d.decision == "deny" and "Fix that first" in d.reason and "rig worktree discard" not in d.reason


def test_an_unparseable_removal_gets_a_how_to_not_just_a_refusal(sb):
    d = decide(sb, "git branch -D a && git branch -D b")
    assert d.decision == "deny"
    assert "WHAT TO DO" in d.reason and "git -C /abs/repo branch -D <name>" in d.reason
    assert "rig worktree discard" in d.reason


def test_the_discard_command_itself_is_not_a_git_removal(sb):
    """The approval lives inside `rig worktree discard`; the guard must not try to judge (or block) it."""
    unlanded = sb.worktree("old")
    sb.commit(unlanded, "o.txt", "o\n", "feat: old")
    assert decide(sb, 'rig worktree discard old --reason "obsolete, superseded by the merged PR"').decision == "pass"


def test_a_squash_merged_branch_that_main_has_since_edited_around_is_allowed(sb):
    """The false denial that started rig-cli#757: two commits squashed under another PR, main moved on next to them."""
    sb.commit(sb.primary, "doc.md", "".join(f"line {i:02d} is a long distinctive sentence\n" for i in range(1, 15)), "docs: base")
    git(sb.primary, "push", "-q", "origin", "main")
    wt = sb.worktree("squashed")
    text = (wt / "doc.md").read_text()
    anchor = "line 05 is a long distinctive sentence\n"
    sb.commit(wt, "doc.md", text.replace(anchor, anchor + "ADDED one by the branch\n"), "feat: one")
    sb.commit(wt, "doc.md", text.replace(anchor, anchor + "ADDED one by the branch\nADDED two by the branch\n"), "feat: two")
    sb.land_squash("squashed")
    git(sb.integrator, "fetch", "-q", "origin")
    git(sb.integrator, "checkout", "-q", "-B", "main", "origin/main")
    path = sb.integrator / "doc.md"
    path.write_text(path.read_text().replace("line 06 is", "line 06 was rewritten, and is"), encoding="utf-8")
    git(sb.integrator, "commit", "-q", "-am", "docs: main edits the next line")
    git(sb.integrator, "push", "-q", "origin", "main")
    sb.fetch()
    d = decide(sb, f"git worktree remove --force {wt}")
    assert d.decision == "allow", d.reason
    assert d.verdicts[0].landed_by == "squash-merged"
