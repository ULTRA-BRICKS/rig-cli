"""``rig worktree discard`` — the sanctioned way to drop work that is NOT merged and is stale or obsolete.

The landed-removal verdict (:mod:`riglib.removal_verdict`) refuses to delete a worktree or branch whose work
it cannot place on the default branch. Most of the time that is right and the fix is to land the work (or to
handle the uncommitted files). In the rare hard case — NOT merged AND stale/obsolete, for example a PR closed
as superseded or a slice of the wrong model — the only party who can say so is the owner. This command is the
path for that decision, without any bypass; it is not a routine confirmation:

1. every target is judged with the SAME verdict; a SAFE target is simply removed (nothing to approve);
2. an UNSAFE one is refused outright unless EVERY reason is ``not-landed`` — a dirty tree, a stash, a running
   process, the default branch or a branch checked out elsewhere are not questions an approval can settle, and
   one such target stops the whole batch before anybody is asked;
3. the owner is asked ONCE on Telegram through the shared hatch helper of agent-tools
   (``agenttools_hatch_escalation``, hook id ``rig-worktree-discard``, env var
   ``RIG_HATCH_REQUEST_RIG_WORKTREE_DISCARD`` set here from ``--reason``), for all the targets together —
   one tap approves or denies the batch. The card is in decision-request shape (Context with file:line,
   Glossary, Options table, Recommendation, Where to look) and lists, per target, the commits, the files that
   exist only on the branch, the last commit's date and the exact tip. Deny-by-default — an unavailable
   ``tg-ctl``, a timeout, a denial and an unparseable reply all refuse;
4. every tip judged is re-checked, then the target is deleted: a worktree and its branch
   (:func:`riglib.worktree.remove_judged`), a local branch (``git branch -D``), a scratch ref such as
   ``refs/rigaudit/main`` (``git update-ref -d <ref> <oid>`` — it refuses if the ref moved), or a remote branch
   (``git push --force-with-lease=<ref>:<tip> <remote> --delete <branch>`` — it refuses if the branch moved).
   There is NO backup: git's reflog is the way back.

``--force`` does not exist here and no environment variable skips the approval: the helper is loaded ONLY
from the machine's fixed agent-tools checkout (never from ``RIG_AGENT_TOOLS_SOURCE`` or the working tree)
and only when the file is owned by this user or root and writable by nobody else, so a REPOSITORY (a
checkout, a fake ``RIG_AGENT_TOOLS_SOURCE``, another account) cannot substitute an approver that always says
yes. Every attempt is appended to ``~/.cache/rig/removal-guard.jsonl``.

Known limit (the hatch helper's own trust model, agent-tools ``agenttools_hatch_escalation``): the
boundary is repository content and other accounts. An agent running as THIS user whose writes are not
confined to its worktree can overwrite a user-owned file in the agent-tools checkout like any other file
it owns; confining those writes is the job of the write hooks (``worktree-only-writes``,
``no-shell-file-edit``), not of this loader — the same limit ``tg-ctl`` lookup documents.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Callable, Sequence

from . import catalog, errors, removal_content as content, removal_explain, removal_verdict as rv, worktree

HOOK_ID = "rig-worktree-discard"
MIN_REASON_CHARS = 8
_HATCH_MODULE = "agenttools_hatch_escalation"
_GIT_TIMEOUT_S = 60
_PUSH_TIMEOUT_S = 300
_SHOWN_FILES = 12
# Telegram refuses a message over 4096 characters, and a refused card is a silent "no" to the agent. The card's
# keys + values are fitted to this budget (the rest of the message is the hatch's own header and markup).
_CARD_BUDGET = 3300
_ELLIPSIS = " ..."
_CARD_ELASTIC = ("commits", "files", "leads", "findings")
_CARD_FLOOR = 100  # three targets of floor-sized evidence still leave room under Telegram's 4096


@dataclass(frozen=True)
class Approval:
    approved: bool
    detail: str


# (justification, card context, cwd) -> the owner's decision
Approver = Callable[[str, "dict[str, str]", str], Approval]


@dataclass(frozen=True)
class DiscardResult:
    """``status``: ``discarded`` | ``refused`` (the verdict or the owner said no) | ``error``."""

    status: str
    message: str
    exit_code: int
    verdicts: tuple[rv.Verdict, ...] = ()


# ── the owner's approval ─────────────────────────────────────────────────────────
def _trusted_hatch_file() -> Path | None:
    """The hatch helper of the machine's fixed agent-tools checkout, or ``None``. Environment overrides and the
    working tree are NOT consulted, and the file must be owned by this user or root and not group/world-writable."""
    for candidate in catalog.DEFAULT_SOURCE_CANDIDATES:
        path = Path(os.path.expanduser(candidate)).resolve() / "lib" / _HATCH_MODULE / "__init__.py"
        try:
            info = path.stat()
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_uid in (os.getuid(), 0) and not info.st_mode & 0o022:
            return path
    return None


def _load_hatch() -> ModuleType:
    path = _trusted_hatch_file()
    if path is None:
        raise RuntimeError(
            "the agent-tools hatch helper was not found in a trusted place "
            f"({', '.join(catalog.DEFAULT_SOURCE_CANDIDATES)}: lib/{_HATCH_MODULE})"
        )
    spec = importlib.util.spec_from_file_location(_HATCH_MODULE, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_HATCH_MODULE] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(_HATCH_MODULE, None)
        raise
    return module


def request_owner_approval(justification: str, context: dict[str, str], cwd: str) -> Approval:
    """Ask the owner on Telegram. A late approval (tapped after an earlier ask expired) is picked up first by
    asking with the hatch variable UNSET; only then is a fresh card sent. Every failure is a refusal."""
    try:
        hatch = _load_hatch()
        env_var = hatch.hatch_env_var(HOOK_ID)
        ambient = {k: v for k, v in os.environ.items() if k != env_var}
        late = hatch.request_hatch_approval(HOOK_ID, context, cwd=cwd, env=ambient)
        if late.approved:
            return Approval(True, late.reason)
        # critical=True: no client-side or daemon-side ~115 s deadline (agent-tools#754, tg-cli#403), so the card stays
        # a LIVE request for a phone user; the late-approval probe above stays a plain bounded ask.
        # An older helper (before agent-tools#754) has no such parameter: it keeps the bounded ask.
        extra = {"critical": True} if "critical" in inspect.signature(hatch.request_hatch_approval).parameters else {}
        result = hatch.request_hatch_approval(HOOK_ID, context, cwd=cwd, env={**ambient, env_var: justification}, **extra)
    except Exception as exc:  # noqa: BLE001 — an approval that cannot be obtained is a refusal
        return Approval(False, f"the owner's approval could not be requested: {exc}")
    return Approval(bool(result.approved), str(result.reason))


def _decision_sections(verdicts: Sequence[rv.Verdict]) -> dict[str, str]:
    """The decision-request shape the owner's channel requires (Context with file:line, Glossary, Options table,
    Recommendation, Where to look). The numeric prefixes keep the hatch card's alphabetical key order."""
    where = f"{Path(__file__).name}:{approval_context.__code__.co_firstlineno} (approval_context), " + (
        f"{Path(rv.__file__).name}:{rv._prove_landed.__code__.co_firstlineno} (the landing proofs)"
    )
    names = ", ".join(f"{v.kind} {v.target}" for v in verdicts)
    many = len(verdicts) > 1
    return {
        "1 Context": (
            f"{where}. rig refuses to delete {names} because its work is NOT merged: no merged PR covers it, no "
            "commit on the default branch carries its diff, and no other remote branch keeps it. The agent asks to "
            f"drop {'them' if many else 'it'} as stale/obsolete anyway. See the per-target lines below."
        ),
        "2 Glossary": (
            "SUPERSEDED = the default branch changed the same files since, so the branch's version is probably "
            "old; UNIQUE = the file exists only on the branch; tip = the exact commit judged (the delete is "
            "refused if it moved); there is no backup - git's reflog is the only way back."
        ),
        "3 Options": (
            "| Option | Pros | Cons |\n| --- | --- | --- |\n"
            f"| Approve | {'all targets are' if many else 'the target is'} gone in one tap; the clutter ends | "
            "the commits leave every branch (reflog only) |\n"
            "| Deny | nothing changes | the stale ref and its clutter stay |"
        ),
        "4 Recommendation": (
            "Approve only if the justification above says the work is obsolete (what it was and what replaced it) "
            "and the per-target lines show nothing you still want; otherwise deny."
        ),
        "5 Where to look": where,
    }


def approval_context(verdicts: Sequence[rv.Verdict]) -> dict[str, str]:
    """What the Telegram card shows the owner for ONE batch: the decision sections plus the facts of every target
    (prefixed ``t1``, ``t2``… when there are several), fitted to what Telegram accepts."""
    context = _decision_sections(verdicts)
    if len(verdicts) == 1:
        context.update(_approval_facts(verdicts[0]))
        return _fit_card(context)
    context["operation"] = "discard work that is NOT merged and is stale/obsolete: ONE decision for the whole batch"
    context["command"] = "rig worktree discard " + " ".join(v.target for v in verdicts)
    for index, verdict in enumerate(verdicts, start=1):
        facts = _approval_facts(verdict)
        facts.pop("operation", None)
        facts.pop("command", None)
        context.update({f"t{index} {key}": value for key, value in facts.items()})
    return _fit_card(context)


def _fit_card(context: dict[str, str]) -> dict[str, str]:
    """Shorten the longest evidence field until the card fits ``_CARD_BUDGET``. Deterministic (a late approval is
    matched to the same operation by a hash of the card), and only the lists of commits/files/leads shrink: the
    target, tip, reason and decision sections are never cut."""
    elastic = [key for key in context if key.rsplit(" ", 1)[-1] in _CARD_ELASTIC]
    while sum(len(k) + len(v) for k, v in context.items()) > _CARD_BUDGET:
        key = max(elastic, key=lambda k: len(context[k]), default=None)
        if key is None or len(context[key]) <= _CARD_FLOOR:
            break
        keep = max(_CARD_FLOOR, len(context[key]) * 2 // 3)
        context[key] = context[key][: keep - len(_ELLIPSIS)].rstrip() + _ELLIPSIS
    return context


def _last_commit_date(verdict: rv.Verdict) -> str:
    """The day of the commit judged, so the owner can see how stale it is."""
    try:
        res = rv.run_git(Path(verdict.repo or "."), "log", "-1", "--format=%cs", verdict.tip or "HEAD")
    except rv.GitFailure:
        return "unknown"
    return res.stdout.decode().strip() or "unknown"


def _approval_facts(verdict: rv.Verdict) -> dict[str, str]:
    """The per-target facts. Deterministic for one target at one tip (a late approval is matched
    to the same operation by a hash of it), so no timestamps or generated paths."""
    context = {
        "operation": "discard work that is NOT merged and is stale/obsolete (no backup is made)",
        "target": f"{verdict.kind} {verdict.target}",
        "tip": verdict.tip or "unknown",
        "last commit": _last_commit_date(verdict),
        "repo": verdict.repo or "unknown",
        "command": f"rig worktree discard {verdict.target}",
    }
    evidence = verdict.evidence
    if evidence is None:
        context["findings"] = " | ".join(r.message for r in verdict.reasons)[:900]
        return context
    context["reading"] = {
        "unique-work": "HAS UNIQUE WORK - files that exist only on this branch",
        "superseded": "looks SUPERSEDED - the default branch has newer changes in the files that differ",
    }.get(evidence.classification, evidence.classification)
    context["commits"] = f"{evidence.commit_total}: " + "; ".join(f"{c.sha} {c.subject}" for c in evidence.commits)
    pending = [f for f in evidence.files if f.status in (content.SUPERSEDED, content.UNIQUE)]
    same = len(evidence.files) - len(pending)
    rows = [f"{f.status}: {f.path}" + (f" ({f.note})" if f.note else "") for f in pending[:_SHOWN_FILES]]
    if len(pending) > _SHOWN_FILES:
        rows.append(f"... and {len(pending) - _SHOWN_FILES} more")
    context["files"] = f"{same} of {len(evidence.files)} already on {evidence.default_ref}; not there: " + (
        "; ".join(rows) or "none"
    )
    if evidence.related:
        context["leads"] = "; ".join(evidence.related)
    return context


# ── git ──────────────────────────────────────────────────────────────────────────
def _git(repo_root: Path, *args: str, stdin: bytes | None = None, timeout: int = _GIT_TIMEOUT_S) -> subprocess.CompletedProcess[bytes]:
    try:
        return rv.run_git(repo_root, *args, timeout=timeout, stdin=stdin)
    except rv.GitFailure as exc:
        raise RuntimeError(str(exc)) from exc


# ── deletion ─────────────────────────────────────────────────────────────────────
def _delete(repo_root: Path, verdict: rv.Verdict, *, remote: str | None) -> tuple[bool, str]:
    """Delete exactly what ``verdict`` judged, refusing if its tip moved since."""
    try:
        if verdict.kind == "worktree":
            res = worktree.remove_judged(repo_root, Path(verdict.target), verdict)
            return res.exit_code == 0, res.message
        if verdict.kind == "local-branch":
            current = _git(repo_root, "rev-parse", "--verify", "-q", f"refs/heads/{verdict.target}^{{commit}}")
            if current.returncode != 0 or current.stdout.decode().strip() != verdict.tip:
                return False, f"branch {verdict.target} moved after it was judged; nothing was deleted - run the command again"
            deleted = _git(repo_root, "branch", "-D", verdict.target)
            if deleted.returncode != 0:
                return False, f"git branch -D failed: {deleted.stderr.decode('utf-8', 'replace').strip()}"
            return True, f"deleted local branch {verdict.target}"
        if verdict.kind == "scratch-ref":
            shown = _git(repo_root, "show-ref", "--verify", verdict.target)
            oid = shown.stdout.decode().partition(" ")[0].strip()
            peeled = _git(repo_root, "rev-parse", "--verify", "-q", f"{oid}^{{commit}}") if oid else None
            if peeled is None or peeled.returncode != 0 or peeled.stdout.decode().strip() != verdict.tip:
                return False, f"{verdict.target} moved after it was judged; nothing was deleted - run the command again"
            deleted = _git(repo_root, "update-ref", "-d", verdict.target, oid)
            if deleted.returncode != 0:
                return False, f"git update-ref -d failed: {deleted.stderr.decode('utf-8', 'replace').strip()}"
            return True, f"deleted scratch ref {verdict.target}"
        name = verdict.target.partition("/")[2]
        pushed = _git(
            repo_root, "push", f"--force-with-lease=refs/heads/{name}:{verdict.tip}", remote or "origin", "--delete", name,
            timeout=_PUSH_TIMEOUT_S,
        )
        if pushed.returncode != 0:
            return False, (
                "the remote refused or the branch moved (nothing was deleted): "
                + pushed.stderr.decode("utf-8", "replace").strip()[:300]
            )
        return True, f"deleted {verdict.target} on the remote"
    except (RuntimeError, OSError) as exc:
        return False, f"could not delete {verdict.target}: {exc}"


def _audit(decision: str, verdict: rv.Verdict | None, target: str, reason: str, detail: str) -> None:
    """One JSON line per attempt in the guard's log. Best-effort: logging never changes the outcome."""
    try:
        path = Path.home() / ".cache" / "rig" / "removal-guard.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "decision": decision,
            "target": target,
            "tip": verdict.tip if verdict else None,
            "reason": reason[:500],
            "detail": detail[:500],
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass


def discard(
    repo_root: Path,
    targets: Sequence[str],
    *,
    reason: str,
    remote: str | None = None,
    cwd: Path | None = None,
    fetch: bool = True,
    forge: rv.ForgeLookup | None = None,
    process_cwds: rv.ProcessCwds = rv.all_process_cwds,
    approver: Approver = request_owner_approval,
) -> DiscardResult:
    """Judge every target; remove the SAFE ones; for the rest (``not-landed`` only) ask the owner ONCE for the
    whole batch and, on approval, delete them. Nothing is deleted unless every target can be (a refused or
    unapprovable one stops the batch before the owner is asked). See the module docstring for the contract."""
    if not targets:
        return DiscardResult("error", "name at least one worktree, branch or scratch ref to discard", errors.EXIT_CONFIG)

    def judge(target: str, removing: frozenset[str]) -> tuple[rv.Verdict | None, DiscardResult | None]:
        verdict, error = worktree.judge_target(
            repo_root, target, remote=remote, cwd=cwd, fetch=fetch, forge=forge, process_cwds=process_cwds, removing=removing
        )
        if verdict is None:
            return None, DiscardResult(
                "error", error.message if error else f"cannot resolve {target!r}", error.exit_code if error else errors.EXIT_CONFIG
            )
        return verdict, None

    # Nothing this ONE command deletes may vouch for a scratch ref it also deletes: the other scratch refs, then
    # (once the targets are resolved) the local branches and the branches of the worktrees.
    removing = frozenset(t for t in targets if rv.is_scratch_ref(t))
    verdicts: list[rv.Verdict] = []
    for target in targets:
        verdict, failed = judge(target, removing)
        if verdict is None:
            return failed
        verdicts.append(verdict)
    doomed = removing | {f"refs/heads/{v.target}" for v in verdicts if v.kind == "local-branch"}
    for v in verdicts:
        branch = worktree.checked_out_branch(repo_root, Path(v.target)) if v.kind == "worktree" else None
        if branch:
            doomed |= {f"refs/heads/{branch}"}
    if doomed != removing:
        for index, v in enumerate(verdicts):
            if v.kind == "scratch-ref":
                again, failed = judge(targets[index], doomed)
                if again is None:
                    return failed
                verdicts[index] = again
    unsafe = [v for v in verdicts if not v.safe]
    labels = ", ".join(f"{v.kind} {v.target}" for v in verdicts)
    stuck = [v for v in unsafe if not removal_explain.approvable(v)]
    if stuck:
        for v in stuck:
            _audit("discard-refused", v, f"{v.kind} {v.target}", reason, "not approvable: " + "; ".join(r.code for r in v.reasons))
        return DiscardResult(
            "refused",
            "Nothing was deleted: " + ", ".join(f"{v.kind} {v.target}" for v in stuck) + " cannot be settled by an approval.\n"
            + removal_explain.explain_denial(stuck),
            errors.EXIT_REMOVAL_UNSAFE,
            tuple(verdicts),
        )
    if unsafe:
        if len(reason.strip()) < MIN_REASON_CHARS:
            return DiscardResult(
                "error",
                f"--reason must say WHY the owner should approve dropping {labels} (at least {MIN_REASON_CHARS} "
                "characters): what the work was and what replaced it.",
                errors.EXIT_CONFIG,
                tuple(verdicts),
            )
        approval = approver(reason.strip(), approval_context(unsafe), str(repo_root))
        if not approval.approved:
            for v in unsafe:
                _audit("discard-denied", v, f"{v.kind} {v.target}", reason, approval.detail)
            return DiscardResult(
                "refused",
                f"The owner did not approve dropping {', '.join(f'{v.kind} {v.target}' for v in unsafe)}, so nothing "
                f"was deleted ({approval.detail}).\n"
                "If the owner denied it, nothing more happens. If the answer is still pending (the card was sent and has not "
                "been tapped), it stays valid: after the tap run the SAME command again and it goes through. "
                "If the work matters, land it instead (see `rig worktree verdict`).",
                errors.EXIT_REMOVAL_UNSAFE,
                tuple(verdicts),
            )
    lines: list[str] = []
    failed = False
    for v in verdicts:
        ok, message = _delete(repo_root, v, remote=remote)
        how = "already proven landed (" + str(v.landed_by) + ")" if v.safe else "approved by the owner"
        _audit(("discard-safe" if v.safe else "discard-approved") if ok else "discard-error", v, f"{v.kind} {v.target}", reason, message)
        lines.append(f"{'deleted' if ok else 'NOT deleted'} {v.kind} {v.target} ({how}): {message}")
        failed = failed or not ok
    return DiscardResult(
        "error" if failed else "discarded", "\n".join(lines), errors.EXIT_INTERNAL if failed else 0, tuple(verdicts)
    )
