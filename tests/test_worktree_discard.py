"""``rig worktree discard`` — the owner-approved way to drop work the landing proofs cannot clear.

Real git (tmp bare origin, primary clone, worktrees). The owner's approval is a fake approver, except in the
loader tests, which run the REAL approval path against a fake agent-tools checkout so the trust rules (fixed
location, ownership, no environment override) are exercised without Telegram.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from removal_helpers import FakeForge, Sandbox, git, no_processes

from riglib import catalog, errors, worktree_discard
from riglib.worktree_discard import Approval, discard

REASON = "obsolete: superseded by the merged PR that shipped the same feature"


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
    """Stands in for the owner's Telegram decision and records what the card carried."""

    def __init__(self, approve: bool, before=None) -> None:
        self.approve, self.before, self.calls = approve, before, []

    def __call__(self, justification: str, context: dict[str, str], cwd: str) -> Approval:
        self.calls.append((justification, context, cwd))
        if self.before is not None:
            self.before()
        return Approval(self.approve, "approved by the fake owner" if self.approve else "denied by the fake owner")


def run(sb: Sandbox, target: str, owner: Owner, *, reason: str = REASON, remote: str | None = None):
    return discard(
        sb.primary, [target], reason=reason, remote=remote, fetch=False, forge=FakeForge(), process_cwds=no_processes,
        approver=owner,
    )


def unlanded_branch(sb: Sandbox, name: str = "old") -> str:
    wt = sb.worktree(name)
    tip = sb.commit(wt, f"{name}.txt", f"{name} work\n", f"feat: {name}")
    sb.push(wt, name)
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    sb.fetch()
    return tip


def heads(sb: Sandbox) -> str:
    return git(sb.primary, "ls-remote", "--heads", "origin")


# ── a landed target needs no approval ────────────────────────────────────────────
def test_a_landed_branch_is_deleted_without_asking_anyone(sb):
    wt = sb.worktree("done")
    sb.commit(wt, "done.txt", "x\n", "feat: done")
    sb.land_merge_commit("done")
    sb.fetch()
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    owner = Owner(True)
    res = run(sb, "done", owner)
    assert res.status == "discarded" and not owner.calls
    assert "refs/heads/done" not in git(sb.primary, "branch", "--list", "done")


# ── the approved path ────────────────────────────────────────────────────────────
def test_an_approved_local_branch_is_deleted(sb, home):
    unlanded_branch(sb)
    owner = Owner(True)
    res = run(sb, "old", owner)
    assert res.status == "discarded" and res.exit_code == 0, res.message
    assert git(sb.primary, "branch", "--list", "old") == ""


def test_the_card_carries_the_findings_the_reason_and_the_exact_commit(sb, home):
    tip = unlanded_branch(sb)
    owner = Owner(True)
    run(sb, "old", owner)
    justification, context, cwd = owner.calls[0]
    assert justification == REASON and cwd == str(sb.primary)
    assert context["tip"] == tip and "branch old" in context["target"]
    assert "old.txt" in context["files"] and "UNIQUE" in context["reading"]
    assert "old" in context["commits"]


def test_the_card_has_the_decision_request_sections(sb, home):
    unlanded_branch(sb)
    owner = Owner(True)
    run(sb, "old", owner)
    context = owner.calls[0][1]
    assert sorted(context)[:5] == ["1 Context", "2 Glossary", "3 Options", "4 Recommendation", "5 Where to look"]
    assert re.search(r"worktree_discard\.py:\d+", context["1 Context"])
    assert "| Option | Pros | Cons |" in context["3 Options"]


def big_branch(sb: Sandbox, name: str) -> None:
    wt = sb.worktree(name)
    for i in range(30):
        subject = f"feat: {name} change number {i} with a rather long subject line"
        sb.commit(wt, f"{'deep/' * 6}module_with_a_long_name_{i}.txt", f"{name} {i}\n", subject)  # per-name: no shared tip
    sb.push(wt, name)
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    sb.fetch()


@pytest.mark.parametrize("names", [["big"], ["big-a", "big-b", "big-c"]])
def test_a_big_branch_still_gets_a_card_that_fits_telegram(sb, home, names):
    for name in names:
        big_branch(sb, name)
    owner = Owner(True)
    res = discard(
        sb.primary, names, reason=REASON, fetch=False, forge=FakeForge(), process_cwds=no_processes, approver=owner
    )
    context = owner.calls[0][1]
    assert res.status == "discarded", res.message
    assert sum(len(k) + len(v) for k, v in context.items()) <= 3300
    assert any(v.endswith(" ...") for k, v in context.items() if k.endswith("files"))
    assert all(name in context["target" if len(names) == 1 else f"t{n} target"] for n, name in enumerate(names, 1))


def test_an_approved_remote_branch_is_deleted_with_a_lease_on_the_judged_tip(sb, home):
    unlanded_branch(sb, "remote-old")
    git(sb.primary, "branch", "-D", "remote-old")
    owner = Owner(True)
    res = run(sb, "remote-old", owner, remote="origin")
    assert res.status == "discarded", res.message
    assert "remote-old" not in heads(sb)


def test_an_approved_worktree_and_its_branch_are_removed(sb, home):
    wt = sb.worktree("wip")
    sb.commit(wt, "wip.txt", "wip\n", "feat: wip")
    res = run(sb, str(wt), Owner(True))
    assert res.status == "discarded", res.message
    assert not wt.exists() and git(sb.primary, "branch", "--list", "wip") == ""


# ── denial, and everything an approval can never settle ──────────────────────────
def test_a_denial_deletes_nothing_and_exits_10(sb, home):
    unlanded_branch(sb)
    owner = Owner(False)
    res = run(sb, "old", owner)
    assert res.status == "refused" and res.exit_code == errors.EXIT_REMOVAL_UNSAFE
    assert "old" in git(sb.primary, "branch", "--list", "old") and "old" in heads(sb)
    assert "denied by the fake owner" in res.message


def test_a_dirty_worktree_is_refused_without_asking_the_owner(sb, home):
    wt = sb.worktree("dirty")
    (wt / "scratch.txt").write_text("uncommitted\n")
    owner = Owner(True)
    res = run(sb, str(wt), owner)
    assert res.exit_code == errors.EXIT_REMOVAL_UNSAFE and not owner.calls and wt.exists()


def test_the_default_branch_is_never_discardable(sb, home):
    owner = Owner(True)
    res = run(sb, "main", owner, remote="origin")
    assert res.exit_code == errors.EXIT_REMOVAL_UNSAFE and not owner.calls
    assert "main" in heads(sb)


@pytest.mark.parametrize("reason", ["", "   ", "short"])
def test_a_missing_or_stub_reason_never_reaches_the_owner(sb, home, reason):
    unlanded_branch(sb)
    owner = Owner(True)
    res = run(sb, "old", owner, reason=reason)
    assert res.status == "error" and res.exit_code == errors.EXIT_CONFIG and not owner.calls
    assert git(sb.primary, "branch", "--list", "old")


def test_an_unknown_target_is_a_config_error(sb, home):
    res = run(sb, "no-such-thing", Owner(True))
    assert res.exit_code == errors.EXIT_CONFIG


def test_a_branch_that_moves_while_the_owner_decides_is_not_deleted(sb, home):
    unlanded_branch(sb)
    wt = sb.worktree("tmp-mover", branch="mover", base="old")

    def move() -> None:  # new work lands on the branch after it was judged
        git(sb.primary, "branch", "-f", "old", sb.commit(wt, "late.txt", "late\n", "feat: late work"))

    res = run(sb, "old", Owner(True, before=move))
    assert res.status == "error" and "moved" in res.message
    assert git(sb.primary, "branch", "--list", "old")


def test_a_remote_branch_that_moves_while_the_owner_decides_is_not_deleted(sb, home):
    unlanded_branch(sb, "rem")
    git(sb.primary, "branch", "-D", "rem")
    other = sb.worktree("pusher", base="origin/rem")

    def move() -> None:
        sb.commit(other, "late.txt", "late\n", "feat: late work")
        git(other, "push", "-q", "origin", "HEAD:refs/heads/rem")

    res = run(sb, "rem", Owner(True, before=move), remote="origin")
    assert res.status == "error"
    assert "refs/heads/rem" in heads(sb), "the lease must stop the delete of a moved branch"


def test_one_approval_covers_a_whole_batch_and_one_unapprovable_target_stops_it(sb):
    unlanded_branch(sb, "stale-a")
    unlanded_branch(sb, "stale-b")
    owner = Owner(True)
    res = discard(
        sb.primary, ["stale-a", "stale-b"], reason=REASON, fetch=False, forge=FakeForge(), process_cwds=no_processes,
        approver=owner,
    )
    assert res.status == "discarded" and len(owner.calls) == 1, res.message
    assert git(sb.primary, "branch", "--list", "stale-a", "stale-b") == ""
    context = owner.calls[0][1]
    assert "stale-a" in context["t1 target"] and "stale-b" in context["t2 target"]
    assert context["command"] == "rig worktree discard stale-a stale-b"
    # a dirty worktree in the batch: nobody is asked, nothing is deleted
    unlanded_branch(sb, "stale-c")
    wt = sb.worktree("dirty-batch")
    (wt / "scratch.txt").write_text("uncommitted\n")
    owner = Owner(True)
    res = discard(
        sb.primary, ["stale-c", str(wt)], reason=REASON, fetch=False, forge=FakeForge(), process_cwds=no_processes,
        approver=owner,
    )
    assert res.exit_code == errors.EXIT_REMOVAL_UNSAFE and not owner.calls
    assert git(sb.primary, "branch", "--list", "stale-c") and wt.exists()


def test_nothing_is_backed_up_and_nothing_is_left_in_the_cache(sb, home):
    unlanded_branch(sb)
    run(sb, "old", Owner(True))
    assert not list((home / ".cache").glob("*-recovery"))


def test_every_attempt_is_written_to_the_guard_log(sb, home):
    unlanded_branch(sb)
    run(sb, "old", Owner(False))
    run(sb, "old", Owner(True))
    lines = [json.loads(line) for line in (home / ".cache" / "rig" / "removal-guard.jsonl").read_text().splitlines()]
    assert [rec["decision"] for rec in lines] == ["discard-denied", "discard-approved"]
    assert lines[1]["reason"] == REASON


def test_a_failed_deletion_of_a_landed_branch_is_logged_as_an_error_not_as_safe(sb, home, monkeypatch):
    wt = sb.worktree("done")
    sb.commit(wt, "done.txt", "x\n", "feat: done")
    sb.land_merge_commit("done")
    sb.fetch()
    git(sb.primary, "worktree", "remove", "--force", str(wt))
    monkeypatch.setattr(worktree_discard, "_delete", lambda *a, **k: (False, "boom"))
    res = run(sb, "done", Owner(True))
    assert res.exit_code != 0
    lines = [json.loads(line) for line in (home / ".cache" / "rig" / "removal-guard.jsonl").read_text().splitlines()]
    assert [rec["decision"] for rec in lines] == ["discard-error"]


# ── the REAL approval path: where the helper may come from ───────────────────────
FAKE_HATCH = textwrap.dedent(
    """
    import json, os
    from types import SimpleNamespace

    def hatch_env_var(hook_id):
        return "RIG_HATCH_REQUEST_" + hook_id.upper().replace("-", "_")

    def request_hatch_approval(hook_id, context, *, cwd, env=None, critical=False):
        var = hatch_env_var(hook_id)
        raw = (env or {}).get(var)
        with open(os.environ["FAKE_HATCH_LOG"], "a") as fh:
            fh.write(json.dumps({"hook": hook_id, "var": var, "raw": raw, "cwd": cwd, "ctx": context, "critical": bool(critical)}) + "\\n")
        if raw is None:
            return SimpleNamespace(approved=False, reason="not requested", requested=False)
        return SimpleNamespace(approved=%s, reason="fake owner says " + ("yes" if %s else "no"), requested=True)
    """
)


def fake_tools(root: Path, approve: bool, mode: int = 0o644) -> Path:
    lib = root / "lib" / "agenttools_hatch_escalation"
    lib.mkdir(parents=True)
    (root / "skills").mkdir()
    (root / "agent-hooks").mkdir()
    init = lib / "__init__.py"
    init.write_text(FAKE_HATCH % (approve, approve))
    init.chmod(mode)
    return root


@pytest.fixture
def hatch_log(tmp_path, monkeypatch) -> Path:
    log = tmp_path / "hatch.log"
    monkeypatch.setenv("FAKE_HATCH_LOG", str(log))
    return log


def test_the_real_approver_sets_the_hatch_variable_from_the_reason(tmp_path, monkeypatch, hatch_log):
    tools = fake_tools(tmp_path / "tools", approve=True)
    monkeypatch.setattr(catalog, "DEFAULT_SOURCE_CANDIDATES", (str(tools),))
    approval = worktree_discard.request_owner_approval(REASON, {"tip": "abc"}, str(tmp_path))
    assert approval.approved
    records = [json.loads(line) for line in hatch_log.read_text().splitlines()]
    assert records[0]["raw"] is None, "a late approval is looked for first, with the variable unset"
    assert records[-1]["var"] == "RIG_HATCH_REQUEST_RIG_WORKTREE_DISCARD" and records[-1]["raw"] == REASON
    assert records[-1]["ctx"] == {"tip": "abc"}


def test_the_real_approver_refuses_when_the_owner_says_no(tmp_path, monkeypatch, hatch_log):
    monkeypatch.setattr(catalog, "DEFAULT_SOURCE_CANDIDATES", (str(fake_tools(tmp_path / "tools", approve=False)),))
    assert not worktree_discard.request_owner_approval(REASON, {}, str(tmp_path)).approved


def test_the_real_approver_refuses_when_no_helper_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "DEFAULT_SOURCE_CANDIDATES", (str(tmp_path / "nowhere"),))
    approval = worktree_discard.request_owner_approval(REASON, {}, str(tmp_path))
    assert not approval.approved and "hatch helper was not found" in approval.detail


def test_a_helper_anyone_can_write_is_not_trusted(tmp_path, monkeypatch, hatch_log):
    tools = fake_tools(tmp_path / "tools", approve=True, mode=0o666)
    monkeypatch.setattr(catalog, "DEFAULT_SOURCE_CANDIDATES", (str(tools),))
    assert not worktree_discard.request_owner_approval(REASON, {}, str(tmp_path)).approved
    assert not hatch_log.exists()


def test_an_environment_override_cannot_swap_in_an_approver_that_says_yes(tmp_path, monkeypatch, hatch_log):
    evil = fake_tools(tmp_path / "evil", approve=True)
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(evil))
    monkeypatch.setattr(catalog, "DEFAULT_SOURCE_CANDIDATES", (str(tmp_path / "real-but-absent"),))
    assert not worktree_discard.request_owner_approval(REASON, {}, str(tmp_path)).approved
    assert not hatch_log.exists()


def test_a_helper_that_crashes_is_a_refusal(tmp_path, monkeypatch):
    tools = fake_tools(tmp_path / "tools", approve=True)
    (tools / "lib" / "agenttools_hatch_escalation" / "__init__.py").write_text("raise RuntimeError('broken helper')\n")
    monkeypatch.setattr(catalog, "DEFAULT_SOURCE_CANDIDATES", (str(tools),))
    approval = worktree_discard.request_owner_approval(REASON, {}, str(tmp_path))
    assert not approval.approved and "broken helper" in approval.detail


# ── the CLI ──────────────────────────────────────────────────────────────────────
def rig(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    root = Path(__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": str(root), "GIT_CONFIG_GLOBAL": os.devnull}
    return subprocess.run(
        [sys.executable, "-m", "riglib", "worktree", *args], capture_output=True, text=True, cwd=cwd, env=env, check=False
    )


def test_the_cli_requires_a_reason(sb):
    unlanded_branch(sb)
    res = rig("discard", "old", "-C", str(sb.primary), cwd=sb.primary)
    assert res.returncode == 2 and "--reason" in res.stderr
    assert git(sb.primary, "branch", "--list", "old")


def test_the_cli_refuses_when_the_approval_is_unavailable(sb, tmp_path):
    unlanded_branch(sb)
    res = rig("discard", "old", "--reason", REASON, "--no-fetch", "-C", str(sb.primary), cwd=sb.primary)
    assert res.returncode == errors.EXIT_REMOVAL_UNSAFE, res.stdout + res.stderr
    assert git(sb.primary, "branch", "--list", "old")


def test_the_verdict_command_prints_the_friendly_explanation(sb):
    unlanded_branch(sb)
    res = rig("verdict", "old", "--no-fetch", "-C", str(sb.primary), cwd=sb.primary)
    assert res.returncode == errors.EXIT_REMOVAL_UNSAFE
    assert "WHAT I FOUND" in res.stdout and "rig worktree discard" in res.stdout
    safe = rig("verdict", "main", "--no-fetch", "--remote", "-C", str(sb.primary), cwd=sb.primary)
    assert safe.returncode == errors.EXIT_REMOVAL_UNSAFE  # the default branch is never removable


def test_the_judged_tip_is_the_remote_tip_not_a_stale_tracking_ref(sb, home):
    """The remote moved after the last fetch: the verdict (and so the lease) uses the live tip."""
    unlanded_branch(sb, "live")
    git(sb.primary, "branch", "-D", "live")
    other = sb.worktree("mover2", base="origin/live")
    live_tip = sb.commit(other, "more.txt", "more\n", "feat: more")
    git(other, "push", "-q", "origin", "HEAD:refs/heads/live")  # the primary's origin/live still shows the OLD tip
    owner = Owner(True)
    res = run(sb, "live", owner, remote="origin")
    assert res.status == "discarded", res.message
    assert owner.calls[0][1]["tip"] == live_tip
    assert "live" not in heads(sb)


def test_no_trace_of_a_recovery_pack_is_left_in_code_docs_or_help(sb):
    """The owner dropped the recovery pack (rig-cli#757): no bundle, no patch file under the cache dir, in any
    wording an agent could read - every riglib module, the README, AGENTS.md and the docs, or --help."""
    root = Path(__file__).resolve().parent.parent
    files = [root / "README.md", root / "AGENTS.md", *sorted((root / "riglib").glob("*.py")), *sorted((root / "docs").glob("*.md"))]
    assert len(files) > 20
    texts = {str(path.relative_to(root)): path.read_text(encoding="utf-8") for path in files}
    for sub in ("remove", "verdict", "discard", "guard"):
        texts[f"rig worktree {sub} --help"] = rig(sub, "--help", cwd=sb.primary).stdout
    for name, text in texts.items():
        for trace in ("git bundle", "-recovery/", "diff --binary", "recovery pack"):
            assert trace not in text, f"{name} still mentions {trace!r}"


def test_the_real_ask_is_critical_so_a_phone_user_is_not_cut_off_at_115_seconds(tmp_path, monkeypatch, hatch_log):
    """The late-approval probe (variable unset) is a plain ask; the real ask is `critical`, so neither this process
    nor the tg-ctl daemon drops it after ~115 s and a late tap lands on a live request."""
    tools = fake_tools(tmp_path / "tools", approve=True)
    monkeypatch.setattr(catalog, "DEFAULT_SOURCE_CANDIDATES", (str(tools),))
    assert worktree_discard.request_owner_approval(REASON, {"tip": "abc"}, str(tmp_path)).approved
    calls = [json.loads(line) for line in hatch_log.read_text().splitlines()]
    assert [(c["raw"] is None, c["critical"]) for c in calls] == [(True, False), (False, True)]


def test_a_hatch_helper_without_the_critical_parameter_still_gets_a_bounded_ask(tmp_path, monkeypatch, hatch_log):
    tools = fake_tools(tmp_path / "tools", approve=True)
    init = tools / "lib" / "agenttools_hatch_escalation" / "__init__.py"
    init.write_text(init.read_text().replace("env=None, critical=False):", "env=None):").replace("bool(critical)", "False"))
    monkeypatch.setattr(catalog, "DEFAULT_SOURCE_CANDIDATES", (str(tools),))
    assert worktree_discard.request_owner_approval(REASON, {"tip": "abc"}, str(tmp_path)).approved
