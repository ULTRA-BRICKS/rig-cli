"""argparse builders for `rig worktree …` and `rig omp`.

Context: the worktree verbs carry many flags, so they live apart from the other subcommand builders.
"""
from __future__ import annotations

import argparse


def add_worktree_parser(sub: "argparse._SubParsersAction") -> None:
    """`rig worktree create`/`remove`/`gc` — the standardized ``.worktrees/<name>`` agent-worktree
    lifecycle. Each subcommand's own argparse registration is split into its own helper below —
    the table of help/description/epilog text for three subcommands made this one long enough to
    warrant it."""
    smp = sub.add_parser(
        "sysmon",
        help="the resource monitor: status | sample | test-alert | simulate | incidents | restore | handler",
        description="Forward to the sysmon daemon's CLI (rig-cli#622) with --config defaulting to the "
        "installed sysmon.json (sysmon.config_dir in the global config). "
        "`rig sysmon <command> --help` shows each command's flags.",
    )
    smp.add_argument("sysmon_args", nargs=argparse.REMAINDER, help="command and its flags")

    wp = sub.add_parser(
        "worktree",
        help="create/remove/verdict/discard/guard/gc a standardized agent worktree under .worktrees/",
    )
    wp.set_defaults(_worktree_parser=wp)
    wsub = wp.add_subparsers(dest="worktree_command", metavar="<create|remove|verdict|discard|guard|gc>")
    _add_worktree_create_subparser(wsub)
    _add_worktree_remove_subparser(wsub)
    _add_worktree_verdict_subparser(wsub)
    _add_worktree_discard_subparser(wsub)
    _add_worktree_guard_subparser(wsub)
    _add_worktree_gc_subparser(wsub)


def _add_worktree_create_subparser(wsub: "argparse._SubParsersAction") -> None:
    wc = wsub.add_parser(
        "create",
        help="create a linked worktree at .worktrees/<name> and register it in .git/info/exclude",
        description="Idempotently ensure .worktrees/ is ignored in the repo's .git/info/exclude "
        "— never the committed .gitignore — THEN create a linked git worktree at the "
        "standardized <repo>/.worktrees/<name> path (replacing the several ad hoc conventions "
        "this ecosystem had grown), so a new worktree never dirties `git status`. In a bun repo "
        "(bun.lock) with the global bun.enabled, it then runs `bun install --frozen-lockfile` there, "
        "as the repo's own bunfig.toml configures it (bun.worktree_install: false turns that off).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="exit codes:\n"
        "  0    success\n"
        "  1    the exclude reconcile failed, or git worktree add timed out\n"
        "  2    invalid name/branch/ref, target already exists, or git rejected the request\n"
        "       (branch collision, bad ref, dirty checkout, …); or the worktree WAS created but\n"
        "       its bun install failed (the message says how to finish it)\n"
        "  6    not a git repository\n"
        "  127  git is not installed",
    )
    wc.add_argument("name", help="worktree directory name under .worktrees/ (also the default branch name)")
    wc.add_argument("-C", "--cwd", default=".", help="repo root (default: cwd)")
    wc.add_argument("--branch", help="branch to create for the worktree (default: <name>)")
    wc.add_argument(
        "--from", dest="base_ref", metavar="REF",
        help="base ref/commit to branch from (default: git's own default, the current HEAD)",
    )


def _add_worktree_remove_subparser(wsub: "argparse._SubParsersAction") -> None:
    wr = wsub.add_parser(
        "remove",
        help="remove a landed, clean linked worktree (anywhere git knows it) and delete its branch",
        description="The inverse of `rig worktree create`, but ONLY when removal provably loses "
        "nothing: it first runs the landed-removal verdict (see `rig worktree verdict`) — the tree "
        "must be clean and every commit on it landed on origin/<default> (ancestor, merged PR "
        "with the same head, a content no-op, every change already on origin/<default> after a squash or "
        "rebase merge, or held by another remote branch that stays) — and refuses with exit 10 otherwise. `--force` "
        "never overrides that verdict; it only lets git unlock a LOCKED worktree. The target is "
        "a worktree path anywhere git knows it, or a name (.worktrees/<name>, else a unique "
        "branch / directory name). Then runs `git worktree remove --force` and `git branch -D` "
        "the branch it had checked out — `git worktree remove` alone leaves the branch behind, so "
        "a bare retry of `create` with the same name fails with 'a branch already exists'. Also "
        "recovers a STALE registration (the worktree directory was deleted by hand instead of via "
        "git). Requires git >= 2.36 (uses `worktree list --porcelain -z`).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="exit codes:\n"
        "  0    removed\n"
        "  1    git worktree list/remove/branch -D timed out, or an unexpected runtime error\n"
        "       ran into rig itself (not a missing git — that's 127)\n"
        "  2    <target> is not (or ambiguously) a registered worktree, .worktrees/ is a symlink\n"
        "       escaping the repo, or git rejected the request (locked worktree, branch\n"
        "       checked out elsewhere, git < 2.36, …)\n"
        "  6    not a git repository\n"
        "  10   UNSAFE verdict: removing it could lose work (nothing was removed)\n"
        "  127  git is not installed",
    )
    wr.add_argument("target", help="worktree path (anywhere git knows it) or name (.worktrees/<name>, branch, or directory name)")
    wr.add_argument("-C", "--cwd", default=".", help="repo root (default: cwd)")
    wr.add_argument(
        "--force", action="store_true",
        help="also unlock a LOCKED worktree (git's `--force --force`); NEVER overrides an UNSAFE verdict",
    )
    wr.add_argument(
        "--no-fetch", action="store_true",
        help="do not refresh origin/<default> before judging (default: best-effort `git fetch`)",
    )


def _add_worktree_verdict_subparser(wsub: "argparse._SubParsersAction") -> None:
    wv = wsub.add_parser(
        "verdict",
        help="read-only: is removing this worktree / local branch / remote branch / scratch ref guaranteed to lose nothing?",
        description="Print the landed-removal verdict (SAFE / UNSAFE with reasons) for a worktree, a "
        "local branch, a scratch ref (a full name outside refs/heads, refs/tags and refs/remotes, e.g. "
        "refs/rigaudit/main: SAFE when its commit is landed or also held by a local branch that "
        "stays), or — with --remote — a remote branch. SAFE only when the worktree is clean "
        "(staged, unstaged and untracked all count; no merge/rebase/cherry-pick in progress; no "
        "process with it as cwd; not the primary checkout or default branch) AND every commit "
        "unique to it is LANDED on origin/<default> or still held by a branch that survives the "
        "removal, proven in order: (1) tip is an ancestor of "
        "origin/<default>; (2) the MERGED PR (by head branch, head commit, or the #N a squash commit on "
        "origin/<default> cites): a tip at or below its head is landed, a tip with commits made AFTER the PR "
        "head holds new work, a history rewritten after the merge is old when `git cherry` finds a twin of every "
        "commit; (3) `git merge-tree` shows merging would be a no-op, or `git cherry` shows only `-` lines; (4) "
        "squash-merged: one commit on origin/<default> (its message names the branch's commits, else it changed "
        "the same files) whose DIFF carries the diff of the whole batch of the branch's commits (a binary file, "
        "a mode change or a link that differs never counts); (5) the "
        "tip is reachable from ANOTHER branch of the same remote (e.g. an archive/* branch), "
        "confirmed live with `git ls-remote` — the verdict names it; the target's own branch never "
        "counts. Pushing the target's own branch is irrelevant; the reflog is never relied on, and a stash made on the branch blocks removal. Every error, timeout, missing "
        "tool or ambiguity is UNSAFE. The same engine gates `rig worktree remove` and the "
        "PreToolUse removal guard.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="exit codes:\n"
        "  0    SAFE\n"
        "  2    <target> is not (or ambiguously) a worktree, branch or scratch ref of this repo (the message says what it is)\n"
        "  6    not a git repository\n"
        "  10   UNSAFE\n"
        "  127  git is not installed",
    )
    wv.add_argument("target", help="worktree path or name, a branch name, or a full scratch ref name")
    wv.add_argument("-C", "--cwd", default=".", help="repo root (default: cwd)")
    wv.add_argument(
        "--remote", nargs="?", const="origin", default=None, metavar="NAME",
        help="judge deleting branch <target> on this remote (default remote: origin)",
    )
    wv.add_argument("--json", action="store_true", help="machine-readable output")
    wv.add_argument(
        "--no-fetch", action="store_true",
        help="do not refresh origin/<default> before judging (default: best-effort `git fetch`)",
    )


def _add_worktree_discard_subparser(wsub: "argparse._SubParsersAction") -> None:
    wd = wsub.add_parser(
        "discard",
        help="drop work the landing proofs cannot clear — only with the owner's Telegram approval",
        description="The sanctioned way to delete worktrees / local branches / scratch refs / remote branches whose work is NOT "
        "merged and is stale or obsolete (for example a PR closed as superseded) - the rare hard case, not a "
        "routine confirmation. Each target is judged by the same verdict as `rig worktree verdict`; a SAFE one "
        "is simply removed. An UNSAFE one is refused unless every reason is `not-landed` (uncommitted files, a "
        "stash, a running process, the default branch are never approvable - fix those first). Otherwise the "
        "OWNER is asked on Telegram ONCE for the whole batch (name several targets to get one tap; agent-tools "
        "hatch hook id rig-worktree-discard, variable RIG_HATCH_REQUEST_RIG_WORKTREE_DISCARD, set from "
        "--reason) with a decision card: the findings per target, your reason and the exact commits; "
        "deny-by-default (no answer, no tg-ctl, a denial all refuse). On approval each tip is re-checked and "
        "the target deleted (a remote branch with a lease on the judged tip); there is no backup - git's reflog "
        "is the way back. There is no --force and no environment variable that skips the approval. Write "
        "--reason as the decision the owner must make: what the work was and what replaced it. Run it again "
        "after a late tap - a late approval is picked up.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="exit codes:\n"
        "  0    deleted (approved, or already proven landed)\n"
        "  1    a deletion failed after the approval (see the message)\n"
        "  2    <target> is not (or ambiguously) a worktree, branch or scratch ref, or --reason is missing/too short\n"
        "  6    not a git repository\n"
        "  10   refused: not approvable, or the owner denied / did not answer (nothing was deleted)\n"
        "  127  git is not installed",
    )
    wd.add_argument(
        "targets", nargs="+", metavar="target",
        help="worktree path or name, a branch name, or a full scratch ref name (with --remote: the remote branch); several = ONE approval",
    )
    wd.add_argument("--reason", required=True, help="why the owner should approve dropping it (>= 8 characters)")
    wd.add_argument("-C", "--cwd", default=".", help="repo root (default: cwd)")
    wd.add_argument(
        "--remote", nargs="?", const="origin", default=None, metavar="NAME",
        help="the target is a branch on this remote (default remote: origin)",
    )
    wd.add_argument(
        "--no-fetch", action="store_true",
        help="do not refresh origin/<default> before judging (default: best-effort `git fetch`)",
    )


def _add_worktree_guard_subparser(wsub: "argparse._SubParsersAction") -> None:
    wg = wsub.add_parser(
        "guard",
        help="PreToolUse hook backend: read a bash command on stdin, print one JSON allow/deny/pass decision",
        description="Reads a bash command from stdin and prints ONE JSON object "
        '{"decision": "pass"|"allow"|"deny", "reason": …, "targets": […]}. Recognizes `git worktree '
        "remove`, `git branch -d/-D`, `git push <remote> --delete <name>` / `:<name>`, and the "
        "overwrites `git branch -f/-M/-C`, `git checkout -B`, `git switch -C` and `rm -r` of a linked "
        "worktree (or a directory holding one); every "
        "target must be SAFE per `rig worktree verdict` for `allow`; anything it cannot parse with "
        "certainty (chains, subshells, variables, globs, wrappers, --git-dir …) is `deny`; not a "
        "removal is `pass`. Always exits 0 once a decision is printed — the hook treats any other "
        "outcome as deny (fail closed).",
    )
    wg.add_argument("-C", "--cwd", default=".", help="directory the command runs in (default: cwd)")
    wg.add_argument(
        "--no-fetch", action="store_true",
        help="do not refresh origin/<default> before judging (default: best-effort `git fetch`)",
    )


def _add_worktree_gc_subparser(wsub: "argparse._SubParsersAction") -> None:
    wg = wsub.add_parser(
        "gc",
        help="classify every worktree of a repo and remove the safe classes (merged/closed PR, "
        "pruned); report the rest",
        description="List every worktree `git` knows about for a repo (any physical location — "
        "not just the standardized .worktrees/<name>), classify each as live/prunable/dirty/"
        "merged/closed/no-pr-stale/active, and remove only merged/closed/prunable ones (plus "
        "no-pr-stale with --include-stale) — and only when the landed-removal verdict (`rig worktree "
        "verdict`) says SAFE: clean, and every commit landed on origin/<default> or held by another "
        "remote branch that stays; --yes/--include-stale never override it. Report-only unless "
        "--yes; --dry-run always forces a report even with --yes, mirroring `rig apply --dry-run`.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="exit codes:\n"
        "  0    success (report printed; some repos may have had nothing to do)\n"
        "  1    git worktree list timed out or hit an unexpected runtime error\n"
        "  2    a repo path was rejected, git worktree list failed, or a planned removal\n"
        "       failed outright (the `git worktree remove`/`branch -D` call itself, or the\n"
        "       pre-removal recheck) — see the report for which entry and why\n"
        "  6    the given/discovered repo is not a git repository\n"
        "  127  git is not installed",
    )
    wg.add_argument(
        "--repo", default=None,
        help="repo path to gc (default: every rig-managed repo in the repository registry)",
    )
    wg.add_argument("--dry-run", action="store_true", help="force a report even if --yes is also passed")
    wg.add_argument(
        "--yes", action="store_true",
        help="actually remove merged/closed/prunable worktrees (bare gc alone only reports)",
    )
    wg.add_argument(
        "--older-than-days", type=int, default=14,
        help="age threshold in days for the no-pr-stale classification (default: 14)",
    )
    wg.add_argument(
        "--include-stale", action="store_true",
        help="also remove no-pr-stale worktrees when --yes is set (never removed on its own)",
    )


def add_omp_parser(sub: "argparse._SubParsersAction") -> None:
    """`rig omp save-panes/restore-panes/watchdog` — the tmux executors the omp-save/omp-restore
    scripts shell out to. Launching, attaching and supervising harness sessions is
    `rig harness` (riglib/harness_cli.py). See riglib/omp_launcher.py's module docstring."""
    op = sub.add_parser("omp", help="tmux pane save/restore + server recovery for omp sessions (internal)")
    op.set_defaults(_omp_parser=op)
    osub = op.add_subparsers(dest="omp_command", metavar="<save-panes|restore-panes|watchdog>")

    sp = osub.add_parser(
        "save-panes", help="record every tmux pane's live OMP session into the map file (internal)"
    )
    sp.add_argument("--map", required=True, dest="map_path")
    sp.add_argument("--tmux-bin", default="tmux")
    sp.add_argument("--tg-ctl-bin", default="tg-ctl")

    rsp = osub.add_parser(
        "restore-panes", help="restore mapped tmux panes' OMP sessions (internal)"
    )
    rsp.add_argument("--map", required=True, dest="map_path")
    rsp.add_argument("--tmux-bin", default="tmux")
    rsp.add_argument("--tg-ctl-bin", default="tg-ctl")
    rsp.add_argument("--rig-bin", default=None)

    wp = osub.add_parser("watchdog", help="identity-checked tmux server/socket recovery (internal)")
    wp.add_argument("--socket", required=True, dest="socket_path")
    wp.add_argument("--boot-script")
    wp.add_argument("--tmux-bin", default="tmux")
    wp.add_argument("--json", action="store_true")
    wp.add_argument("--paused", action="store_true")


def omp_launcher():
    from . import omp_launcher

    return omp_launcher
