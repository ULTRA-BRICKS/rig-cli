"""Plain-language denials for the removal guard — WHAT was found, WHY it blocks, WHAT TO DO.

One wording for every entry point that refuses a removal (the PreToolUse guard, ``rig worktree remove``,
``rig worktree verdict``): an agent that reads only the refusal must be able to tell obsolete work from
unique work, know which commands are sanctioned, and never feel the need to look for a way around the
guard. The facts come from :class:`riglib.removal_verdict.Verdict` (reasons + the file-by-file
:class:`riglib.removal_content.Evidence`); nothing is re-computed here.

The sanctioned ways forward it names:

1. KEEP the work — land it (a merged PR), or keep the commits on a branch that stays; for junk files, do the
   work (a ``.gitignore`` rule) and the check passes;
2. DROP work that is NOT merged and is stale/obsolete — ``rig worktree discard <target>... --reason "<why>"``:
   the owner approves on Telegram (one tap for a batch), then the targets are deleted
   (:mod:`riglib.worktree_discard`);
3. the owner may always run the plain ``git`` command in their own terminal — this guard only watches agent
   tool calls.

The discard route is offered only when EVERY reason is ``not-landed``: a dirty tree, a stash, a running process,
the default branch or a branch checked out elsewhere are not questions an approval can settle.
"""

from __future__ import annotations

import shlex
from typing import Sequence

from . import removal_content as content
from . import removal_verdict as rv

NOT_LANDED = "not-landed"
_FILE_ROWS = 12
_SUPERSEDED_ROWS = 6

# What each reason code means for a person, and the one thing to do about it.
_FIX = {
    "dirty-worktree": "commit what matters (git keeps uncommitted files nowhere else); for junk (build output, caches, "
    "editor files) add a .gitignore rule - then the check passes",
    "stash": "look at it (`git stash show -p <stash@{n}>`), commit what matters on a branch, then `git stash drop`",
    "status-failed": "retry; git could not report the worktree's status",
    "operation-in-progress": "finish or abort the merge/rebase/cherry-pick/revert/bisect first",
    "live-process": "stop the process that is running inside that directory first (or `cd` out of it)",
    "live-process-unknown": "retry; the process list could not be read",
    "branch-checked-out": "switch the other worktree to a different branch first",
    "primary-checkout": "never remove the primary checkout; remove a linked worktree instead",
    "default-branch": "the default branch is never deleted or re-pointed",
    "contains-worktree": "remove the worktree that lives inside it first",
    "nested-repository": "move the git repository that lives inside it out first",
    "unknown-branch": "check the name (`git branch -a`, `git worktree list`)",
    "unknown-worktree": "check the path (`git worktree list`)",
    "invalid-branch-name": "use a plain branch name",
    "invalid-remote-name": "use the remote's NAME (`git remote -v`), not its URL",
    "remote-lookup-failed": "retry when the remote is reachable",
    "no-default-ref": "run `git fetch origin` (and `git remote set-head origin -a`) so the default branch can be resolved",
    "verdict-error": "retry; the check itself failed (fail closed)",
}


def _quote(text: str) -> str:
    return shlex.quote(text)


def _repo_arg(verdict: rv.Verdict) -> str:
    return _quote(verdict.repo) if verdict.repo else "<repo>"


def discard_command(verdict: rv.Verdict) -> str:
    """The exact ``rig worktree discard`` invocation for this target (the reason is the caller's to write)."""
    repo = _repo_arg(verdict)
    if verdict.kind == "remote-branch":
        remote, _, branch = verdict.target.partition("/")
        return f'rig worktree discard -C {repo} --remote {_quote(remote)} {_quote(branch)} --reason "<why this work is obsolete>"'
    return f'rig worktree discard -C {repo} {_quote(verdict.target)} --reason "<why this work is obsolete>"'


def owner_command(verdict: rv.Verdict) -> str:
    """The plain git command the owner can run in their OWN terminal (the guard only watches agents)."""
    repo = _repo_arg(verdict)
    if verdict.kind == "remote-branch":
        remote, _, branch = verdict.target.partition("/")
        return f"git -C {repo} push {_quote(remote)} --delete {_quote(branch)}"
    if verdict.kind == "worktree":
        return f"git -C {repo} worktree remove --force {_quote(verdict.target)}"
    return f"git -C {repo} branch -D {_quote(verdict.target)}"


def approvable(verdict: rv.Verdict) -> bool:
    """Only work the proofs could not place is a question an owner's approval can settle."""
    return bool(verdict.reasons) and all(r.code == NOT_LANDED for r in verdict.reasons)


def _commit_lines(evidence: content.Evidence) -> list[str]:
    lines = [f"      {c.sha}  {c.subject}" for c in evidence.commits]
    if evidence.commit_total > len(evidence.commits):
        lines.append(f"      ... and {evidence.commit_total - len(evidence.commits)} more")
    return lines


def _file_lines(evidence: content.Evidence) -> list[str]:
    same = evidence.count(content.IDENTICAL) + evidence.count(content.LANDED)
    sup = [f for f in evidence.files if f.status == content.SUPERSEDED]
    uniq = [f for f in evidence.files if f.status == content.UNIQUE]
    lines = [
        f"  - I compared the {len(evidence.files)} file(s) those commits change with {evidence.default_ref}:",
        f"      already on {evidence.default_ref}, same content ........ {same}",
        f"      differs, and {evidence.default_ref} changed the file since ... {len(sup)}",
        f"      exists only on this branch (or never reached it) ...... {len(uniq)}",
    ]
    for label, rows in (("only here", uniq), ("differs", sup)):
        for finding in rows[: _FILE_ROWS if label == "only here" else _SUPERSEDED_ROWS]:
            why = f" - {finding.note}" if finding.note else ""
            lines.append(f"        [{label}] {finding.path}{why}")
            lines.extend(f"            {line}" for line in finding.missing)
        if len(rows) > (_FILE_ROWS if label == "only here" else _SUPERSEDED_ROWS):
            hidden = len(rows) - (_FILE_ROWS if label == "only here" else _SUPERSEDED_ROWS)
            lines.append(f"        ... and {hidden} more [{label}] file(s)")
    return lines


def _reading(evidence: content.Evidence) -> str:
    ref = evidence.default_ref
    if evidence.classification == "unique-work":
        only = [f.path for f in evidence.files if f.status == content.UNIQUE]
        shown = ", ".join(only[:4]) + (f" and {len(only) - 4} more" if len(only) > 4 else "")
        return (
            f"  - My reading: THIS BRANCH HAS WORK THAT IS NOT ON {ref} ({shown}). Deleting it would lose that work."
        )
    return (
        f"  - My reading: LOOKS SUPERSEDED. Everything else is already on {ref}; the lines above differ only in "
        f"files where {ref} has newer changes of its own. Probably obsolete - but only a person can confirm that."
    )


def _found(verdict: rv.Verdict) -> list[str]:
    lines: list[str] = []
    tip = f" (tip {verdict.tip[:8]})" if verdict.tip else ""
    # uncommitted files and stashes are the clearest signal: they come first, the landing question last
    for reason in sorted(verdict.reasons, key=lambda r: r.code == NOT_LANDED):
        if reason.code != NOT_LANDED:
            lines.append(f"  - {reason.message}")
            continue
        evidence = verdict.evidence
        if evidence is None:
            lines.append(f"  - The work{tip} is not proven to be on the default branch: {reason.message}")
            continue
        total = evidence.commit_total
        lines.append(f"  - It carries {total} commit(s){tip} that are not on {evidence.default_ref}:")
        lines.extend(_commit_lines(evidence))
        lines.extend(_file_lines(evidence))
        lines.append(_reading(evidence))
        lines.extend(f"  - Lead: {lead}" for lead in evidence.related)
    return lines


def _why(verdict: rv.Verdict) -> str:
    if approvable(verdict):
        return (
            "  Deleting a branch or worktree cannot be undone. I say yes only when I can PROVE the work is already "
            "on the default branch: its merged pull request covers every commit, or one commit there carries the "
            "diff of the whole branch, or another remote branch keeps it. Here none of that held."
        )
    return (
        "  Each point above is a case where deleting could destroy something that exists nowhere else. "
        "`--force` and environment variables do not change that."
    )


def _steps(verdict: rv.Verdict) -> list[str]:
    if not approvable(verdict):
        fixes = [_FIX[r.code] for r in verdict.reasons if r.code in _FIX]
        steps = [f"Fix that first: {fix}." for fix in dict.fromkeys(fixes)] or [
            "Fix what the points above name, then run the same command again."
        ]
        steps.append(f"Look at the details any time: `rig worktree verdict {_quote(verdict.target)}`.")
        return steps
    repo_name = verdict.repo.rstrip("/").rsplit("/", 1)[-1] if verdict.repo else "<repo>"
    keep = rv.rescue_hint(verdict.kind, verdict.target, repo_name, verdict.tip)
    return [
        "The work MATTERS (or you are not sure): land it. Open a pull request from it, get it merged, then run the "
        f"same command again. To keep the commits without merging: {keep}.",
        "The work is NOT merged AND it is stale or obsolete (for example superseded by a merged PR), and you can "
        "explain why: ask the owner with\n"
        f"       {discard_command(verdict)}\n"
        "     Name SEVERAL such targets in ONE command and the owner approves them all with one tap. The owner "
        "gets a Telegram card with the findings above; write --reason as the decision they have to make: what the "
        "work was, what replaced it. This is for the rare hard case, not a routine confirmation. Nobody can skip "
        "this step - not with --force, an environment variable or another command.",
        "The OWNER can always delete it from their own terminal (this guard only watches agent tool calls):\n"
        f"       {owner_command(verdict)}",
        f"To re-read the details any time: `rig worktree verdict {_quote(verdict.target)}`.",
    ]


def explain_denial(verdicts: Sequence[rv.Verdict], tail: str = "") -> str:
    """The full refusal text for every UNSAFE verdict among ``verdicts`` (SAFE ones are skipped). ``tail`` is
    appended verbatim (forge/refresh notes). The technical per-reason lines (``[not-landed] ...``) follow the
    friendly part so logs and tests keep the machine-readable codes."""
    unsafe = [v for v in verdicts if not v.safe]
    verb = "overwritten" if any(v.overwrite for v in unsafe) else "deleted"
    out = [f"removal guard: STOPPED - nothing was {verb}. This is the safety check that protects unmerged work."]
    for verdict in unsafe:
        out += ["", f"=== {verdict.kind} {verdict.target} ===", "WHAT I FOUND", *_found(verdict)]
        out += ["", "WHY I STOPPED", _why(verdict), "", "WHAT TO DO"]
        out += [f"  {number}. {step}" for number, step in enumerate(_steps(verdict), start=1)]
        out += [
            "  Do NOT look for another way to delete it (`gh api` DELETE on refs, `git update-ref -d`, editing "
            ".git, `sh -c`, `--no-verify`): those are exactly what this guard exists to stop, and they are not needed."
        ]
    out += ["", "Technical details:", *(v.render() for v in unsafe)]
    text = "\n".join(out)
    return f"{text}{tail}" if tail else text


def explain_unparsed(detail: str) -> str:
    """The refusal for a command the guard cannot judge with certainty (a chain, an expansion, a wrapper, a
    symlinked path, an internal error): nothing is guessed, and the way forward is one plain command per target."""
    return "\n".join(
        [
            "removal guard: STOPPED - nothing was run. I could not tell for sure what this command would delete.",
            "",
            "WHAT I FOUND",
            f"  - {detail}",
            "",
            "WHY I STOPPED",
            "  The guard judges ONE plain command with literal names. Anything built from variables, chains, `cd`, "
            "wrappers (`sh -c`, `xargs`, `eval`) or symlinks could delete something I cannot see, so I never guess.",
            "",
            "WHAT TO DO",
            "  1. Run each deletion as its own simple command with literal names and absolute paths, for example",
            "       git -C /abs/repo branch -D <name>",
            "       git -C /abs/repo push origin --delete <name>",
            "       git -C /abs/repo worktree remove <path>",
            "     Every target is then checked on its own: it must be clean and landed on the default branch.",
            "  2. If a target holds work that is not landed, that command is stopped with a full explanation and your "
            "options, including `rig worktree discard` (it needs the owner's approval).",
            "  3. Look at any target first: `rig worktree verdict <path|branch>`.",
            "  Do NOT look for another way to delete it (`gh api` DELETE on refs, `git update-ref -d`, editing .git, "
            "`sh -c`, `--no-verify`): those are exactly what this guard exists to stop.",
        ]
    )
