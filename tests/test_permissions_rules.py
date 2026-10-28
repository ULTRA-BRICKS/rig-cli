"""Permission-allowlist tests: deny/ask rules, belts and guard rule shape."""

from __future__ import annotations

import json

from riglib.actions import run_plan
from riglib.catalog import Catalog
from riglib.drift import detect
from riglib.permissions import (
    DEFAULT_RULES,
    DEFAULT_TOOLS,
)
from riglib.plan import build

import pytest

from tests.permissions_helpers import _cfg, _perm_results, _opencode_cfg


def test_plan_emits_enterworktree_and_git_worktree_add_denies(fake_agent_tools, tmp_path):
    # default-on plan must carry both bans so `rig apply` provisions them with no config
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    plan = build(_cfg(repo, fake_agent_tools, settings), Catalog.scan(str(fake_agent_tools)),
                 project_type="unknown")
    opts = [a for a in plan.actions if a.kind == "provision_permissions"][0].options
    assert "EnterWorktree" in opts["deny_rules"]
    assert "Bash(git worktree add:*)" in opts["deny_rules"]


def test_apply_enterworktree_deny_is_idempotent(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg(repo, fake_agent_tools, settings, tools=["git"]), cat, project_type="unknown")
    first = run_plan(plan)
    assert not first.errors, [r.detail for r in first.errors]
    deny = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["deny"]
    assert "EnterWorktree" in deny
    assert "Bash(git worktree add:*)" in deny
    before = settings.read_text(encoding="utf-8")
    second = run_plan(plan)
    assert all(r.status == "skipped" for r in _perm_results(second))
    assert settings.read_text(encoding="utf-8") == before


def test_status_flags_missing_enterworktree_deny_as_drift(fake_agent_tools, tmp_path):
    # the live-machine shape when this baseline entry is absent from settings.json:
    # every other deny is present, EnterWorktree is not — `rig status` must name that one.
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    deny_without = [r for r in DEFAULT_RULES["claude-code"]["deny"] if r != "EnterWorktree"]
    settings.write_text(json.dumps({"permissions": {
        "allow": ["Bash(git:*)"],
        "deny": deny_without,
        "ask": list(DEFAULT_RULES["claude-code"]["ask"]),
    }}), encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg(repo, fake_agent_tools, settings, tools=["git"]), cat, project_type="unknown")
    missing = [d for d in detect(plan).by_direction("missing") if d.category == "permissions"]
    assert any("EnterWorktree" in d.detail and "permissions.deny" in d.detail for d in missing)
    run_plan(plan)
    assert not [d for d in detect(plan).items if d.category == "permissions"]


def test_drift_reports_user_extras_and_apply_never_deletes(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg(repo, fake_agent_tools, settings, tools=["git"]), cat, project_type="unknown")
    run_plan(plan)  # converge first
    # the user hand-adds entries beyond the rig baseline
    data = json.loads(settings.read_text(encoding="utf-8"))
    data["permissions"]["allow"] += ["Bash(kubectl:*)", "Bash(docker ps:*)"]
    data["permissions"]["deny"].append("Bash(shutdown:*)")
    settings.write_text(json.dumps(data), encoding="utf-8")
    rep = detect(plan)
    # since rig-cli#357 user extras are KNOWN ("your additions, kept"), not drift: per entry, each
    # naming its container, so status can group them into one line per container
    assert not [d for d in rep.by_direction("extra") if d.category == "permissions"]
    kept = {k.name: k.container for k in rep.known if k.category == "permissions"}
    assert kept["Bash(kubectl:*)"] == "claude-code:permissions.allow"
    assert kept["Bash(docker ps:*)"] == "claude-code:permissions.allow"
    assert kept["Bash(shutdown:*)"] == "claude-code:permissions.deny"
    # extras are report-only: nothing is missing, and a re-apply STILL never deletes them
    assert not [d for d in rep.by_direction("missing") if d.category == "permissions"]
    run_plan(plan)
    data2 = json.loads(settings.read_text(encoding="utf-8"))
    assert "Bash(kubectl:*)" in data2["permissions"]["allow"]
    assert "Bash(shutdown:*)" in data2["permissions"]["deny"]


def test_explicit_empty_deny_keeps_container_managed(fake_agent_tools, tmp_path):
    # `deny: []` disables the baseline (rig stops ADDING) but the container stays rig-MANAGED:
    # a previously-applied baseline left on disk must show as per-entry extras, not silently
    # vanish from status — and apply must still never delete it (codex finding, rig-cli#100).
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))
    run_plan(build(_cfg(repo, fake_agent_tools, settings, tools=["git"]), cat, project_type="unknown"))
    plan2 = build(_cfg(repo, fake_agent_tools, settings, tools=["git"], deny=[], ask=[]),
                  cat, project_type="unknown")
    rep = detect(plan2)
    # per-entry, all visible — as KNOWN "baseline rules your config turned off, kept" (rig-cli#357)
    deny_kept = [k for k in rep.known
                 if k.category == "permissions" and k.container == "claude-code:permissions.deny"]
    assert len(deny_kept) == len(DEFAULT_RULES["claude-code"]["deny"])
    assert not [d for d in rep.by_direction("extra") if d.category == "permissions"]
    report = run_plan(plan2)
    assert all(r.status == "skipped" for r in _perm_results(report))  # nothing to add
    data = json.loads(settings.read_text(encoding="utf-8"))
    for rule in DEFAULT_RULES["claude-code"]["deny"]:
        assert rule in data["permissions"]["deny"]  # never deleted


def test_apply_tolerates_and_drift_flags_nonstring_array_entries(fake_agent_tools, tmp_path):
    # a hand-mangled allow list with non-string junk: apply must not crash (string-only
    # membership; an object in the list would even make set() raise TypeError), junk survives,
    # and drift flags it as `modified` so status never looks clean over it (apply/status parity)
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    settings.write_text(json.dumps({"permissions": {"allow": ["Bash(git:*)", {"oops": 1}, 42]}}),
                        encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg(repo, fake_agent_tools, settings, tools=["git", "gh"]), cat, project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    allow = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert {"oops": 1} in allow and 42 in allow          # junk preserved (rig never deletes)
    assert "Bash(gh:*)" in allow                          # …and the merge still landed
    mod = [d for d in detect(plan).by_direction("modified") if d.category == "permissions"]
    assert any("non-string" in d.detail and "permissions.allow" in d.detail for d in mod)


# ── opencode deny/ask via permission.bash (object form, last-match ordering) ─────────
def test_opencode_deny_ask_written_after_user_catch_all(fake_agent_tools, tmp_path):
    """opencode is last-match-wins, so rig's deny/ask keys MUST land AFTER a user ``"*"`` catch-all
    (and after rig's own allow keys) or the catch-all would win over a deny."""
    from riglib.permissions import OPENCODE_ASK_RULES, OPENCODE_DENY_RULES

    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "opencode.json"
    settings.write_text(json.dumps({"permission": {"bash": {"*": "allow"}}}), encoding="utf-8")
    plan = build(_opencode_cfg(repo, fake_agent_tools, settings, tools=["gh"]),
                 Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert not run_plan(plan).errors
    bash = json.loads(settings.read_text(encoding="utf-8"))["permission"]["bash"]
    keys = list(bash)
    assert keys[0] == "*"                       # user catch-all preserved AND still first
    assert bash["gh *"] == "allow"
    for d in OPENCODE_DENY_RULES:
        assert bash[d] == "deny"
        assert keys.index(d) > keys.index("*")      # deny after the catch-all → last-match-wins
        assert keys.index(d) > keys.index("gh *")   # …and after rig's allow keys
    for a in OPENCODE_ASK_RULES:
        assert bash[a] == "ask"


def test_opencode_upgrade_reanchors_deny_after_a_newly_merged_allow_key(fake_agent_tools, tmp_path):
    """Regression: on a machine that already applied rig BEFORE `gh` joined the default tool
    set, `gh pr merge*: deny` was written first; `gh` joining the defaults later means `gh *:
    allow` is a NEW key merged into an object that already has that deny key. Plain
    append-missing would land `gh *` AFTER the pre-existing `gh pr merge*`, and opencode is
    last-match-wins — the later allow key would silently un-deny the merge. Assert the runner
    re-anchors rig's deny/ask keys after every apply, and that a second apply changes nothing
    (idempotent — the fix must not turn every apply into a rewrite)."""
    from riglib.permissions import OPENCODE_ASK_RULES, OPENCODE_DENY_RULES

    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "opencode.json"
    pre_gh_tools = [t for t in DEFAULT_TOOLS if t != "gh"]
    seed_bash: dict[str, str] = {f"{t} *": "allow" for t in pre_gh_tools}
    for key in OPENCODE_DENY_RULES:
        seed_bash[key] = "deny"
    for key in OPENCODE_ASK_RULES:
        seed_bash[key] = "ask"
    settings.write_text(json.dumps({"permission": {"bash": seed_bash}}), encoding="utf-8")

    plan = build(_opencode_cfg(repo, fake_agent_tools, settings),
                 Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]

    bash = json.loads(settings.read_text(encoding="utf-8"))["permission"]["bash"]
    assert bash["gh *"] == "allow"
    assert bash["gh pr merge*"] == "deny"
    keys = list(bash)
    # the load-bearing assertion: the newly-merged `gh *` allow key must NOT outrank the
    # pre-existing `gh pr merge*` deny key (last-match-wins → later position wins)
    assert keys.index("gh *") < keys.index("gh pr merge*")
    for d in OPENCODE_DENY_RULES:
        assert keys.index(d) > keys.index("gh *")
    for a in OPENCODE_ASK_RULES:
        assert keys.index(a) > keys.index("gh *")

    after_first = settings.read_text(encoding="utf-8")
    second = run_plan(plan)
    assert not second.errors, [r.detail for r in second.errors]
    assert settings.read_text(encoding="utf-8") == after_first  # idempotent: no-op re-apply


def test_opencode_apply_heals_an_already_misordered_machine(fake_agent_tools, tmp_path):
    """The SHARPER regression: a machine that opted into `gh` the documented pre-diff way
    (`permissions.extra: [gh]`, back when `gh` was not yet a default tool) already has `gh *:
    allow` sitting AFTER `gh pr merge*: deny` in `permission.bash` — last-match-wins means the
    merge deny was ALREADY silently defeated on that machine, before this PR even lands. Every
    desired entry (allow AND deny/ask) is already PRESENT there — `added` would be 0 for every
    container — so a naive `total_added == 0` skip would leave it broken forever, on every future
    apply, because "everything already present" never stops being true. The fix must compare the
    container's serialized bytes, not `added`, to catch pure reordering as a write-worthy change."""
    from riglib.permissions import OPENCODE_ASK_RULES, OPENCODE_DENY_RULES

    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "opencode.json"
    pre_gh_tools = [t for t in DEFAULT_TOOLS if t != "gh"]
    seed_bash: dict[str, str] = {f"{t} *": "allow" for t in pre_gh_tools}
    for key in OPENCODE_DENY_RULES:
        seed_bash[key] = "deny"
    for key in OPENCODE_ASK_RULES:
        seed_bash[key] = "ask"
    seed_bash["gh *"] = "allow"  # the already-broken shape: gh allow AFTER the deny keys
    settings.write_text(json.dumps({"permission": {"bash": seed_bash}}), encoding="utf-8")

    plan = build(_opencode_cfg(repo, fake_agent_tools, settings),
                 Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    assert not any(r.status == "skipped" for r in report.results if r.action.category == "permissions"), (
        "the reorder-only repair must not be silently discarded as a no-op"
    )

    bash = json.loads(settings.read_text(encoding="utf-8"))["permission"]["bash"]
    keys = list(bash)
    assert keys.index("gh *") < keys.index("gh pr merge*")  # healed
    for d in OPENCODE_DENY_RULES:
        assert keys.index(d) > keys.index("gh *")
    for a in OPENCODE_ASK_RULES:
        assert keys.index(a) > keys.index("gh *")

    after_first = settings.read_text(encoding="utf-8")
    second = run_plan(plan)
    assert not second.errors, [r.detail for r in second.errors]
    assert settings.read_text(encoding="utf-8") == after_first  # idempotent once healed


def test_opencode_misordered_deny_before_allow_is_drift_not_clean(fake_agent_tools, tmp_path):
    # rig-cli#333 review finding: on an UPGRADED opencode settings file where `gh pr merge*: deny`
    # already precedes `gh *: allow` (the shape `test_opencode_apply_heals_an_already_misordered_
    # machine` proves `run_plan` heals), `_check_permissions` only compared key membership/values —
    # not order — so `rig status` reported this insecure pre-repair state as clean. Since opencode
    # is last-match-wins, the broad allow silently wins over the merge-ban deny until an apply is
    # actually committed. Status must say `modified`, not clean, over it.
    from riglib.permissions import OPENCODE_ASK_RULES, OPENCODE_DENY_RULES

    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "opencode.json"
    pre_gh_tools = [t for t in DEFAULT_TOOLS if t != "gh"]
    seed_bash: dict[str, str] = {f"{t} *": "allow" for t in pre_gh_tools}
    for key in OPENCODE_DENY_RULES:
        seed_bash[key] = "deny"
    for key in OPENCODE_ASK_RULES:
        seed_bash[key] = "ask"
    seed_bash["gh *"] = "allow"  # every desired entry present — just in the broken order
    settings.write_text(json.dumps({"permission": {"bash": seed_bash}}), encoding="utf-8")

    plan = build(_opencode_cfg(repo, fake_agent_tools, settings),
                 Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    rep = detect(plan)
    mod = [d for d in rep.by_direction("modified") if d.category == "permissions"]
    assert mod, "a misordered opencode deny/ask key must be reported as drift, not clean"
    assert any("permission.bash" in d.detail for d in mod)

    run_plan(plan)  # apply heals it (already proven elsewhere) — status must go clean afterwards
    assert not [d for d in detect(plan).items if d.category == "permissions"]


def test_opencode_misordered_deny_before_user_catch_all_is_drift(fake_agent_tools, tmp_path):
    # the sibling shape: rig's own deny key precedes a USER key (not one of rig's own allow
    # entries) — last-match-wins means the user's catch-all still wins over the deny today.
    from riglib.permissions import OPENCODE_DENY_RULES

    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "opencode.json"
    seed_bash: dict[str, str] = {}
    for key in OPENCODE_DENY_RULES:
        seed_bash[key] = "deny"
    seed_bash["*"] = "allow"  # user's own catch-all, added AFTER rig's deny keys were written
    settings.write_text(json.dumps({"permission": {"bash": seed_bash}}), encoding="utf-8")

    plan = build(_opencode_cfg(repo, fake_agent_tools, settings, tools=[]),
                 Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    rep = detect(plan)
    mod = [d for d in rep.by_direction("modified") if d.category == "permissions"]
    assert mod, "a rig deny key preceding a user key must be reported as drift"


def test_opencode_deny_ask_drift_missing_then_converged_and_extras_once(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "opencode.json"
    settings.write_text("{}", encoding="utf-8")  # file exists but empty → per-entry missing fires
    plan = build(_opencode_cfg(repo, fake_agent_tools, settings, tools=["gh"]),
                 Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    missing = [d for d in detect(plan).by_direction("missing") if d.category == "permissions"]
    assert any("gh pr merge*" in d.detail and "permission.bash" in d.detail for d in missing)
    # apply converges; a rig-written deny key must NOT surface as an allow "extra"
    assert not run_plan(plan).errors
    assert not [d for d in detect(plan).items if d.category == "permissions"]
    # a genuine user extra in the shared object is reported EXACTLY ONCE (not once per role)
    data = json.loads(settings.read_text(encoding="utf-8"))
    data["permission"]["bash"]["frobnicate *"] = "allow"
    settings.write_text(json.dumps(data), encoding="utf-8")
    kept = [k for k in detect(plan).known if k.category == "permissions"]
    assert len(kept) == 1 and kept[0].name == "frobnicate *"
    assert not [d for d in detect(plan).by_direction("extra") if d.category == "permissions"]


def test_opencode_force_push_deny_over_blocks_force_with_lease_documented():
    # documented fidelity gap: opencode `*` has no word boundary, so the force-push deny also
    # matches --force-with-lease (which claude-code's word-boundary matcher excludes).
    from riglib.permissions import OPENCODE_DENY_RULES

    assert "git push*--force*" in OPENCODE_DENY_RULES


def test_opencode_rg_pre_deny_matches_pre_but_not_pretty():
    # rig-cli#187: BEHAVIORAL check (not just membership) via opencode's documented glob
    # dialect (`*` = zero-or-more chars, verified equivalent to shell fnmatch). The single
    # substring-only `rg*--pre*` entry was rejected in review for over-denying `--pretty` (a
    # different, legitimate, read-only flag that merely contains "--pre" as a substring) — this
    # proves the replacement value-shaped forms deny the real flag without that regression.
    import fnmatch

    from riglib.permissions import OPENCODE_DENY_RULES

    def denied(command: str) -> bool:
        return any(fnmatch.fnmatch(command, pattern) for pattern in OPENCODE_DENY_RULES)

    assert denied("rg --pre cat pattern")
    assert denied("rg pattern --pre cat")
    assert denied("rg --pre=cat pattern")
    assert denied("rg pattern --pre")  # trailing/end-anchored form, mirrors claude-code's coverage
    assert not denied("rg --pretty pattern src")  # the regression this fix must not reintroduce
    assert not denied("rg pattern src")
    # `--pre-glob` ALONE (no `--pre`) is a documented no-op EVERYWHERE, not just this belt — it
    # has no effect without `--pre`, which is caught on its own by every belt including this one.
    assert not denied("rg --pre-glob '*.md' pattern")


def test_rg_pre_deny_rules_scoped_not_over_broad():
    # rig-cli#187 fix must not touch the base `Bash(rg:*)` allow entry or over-deny plain search.
    from riglib.permissions import CLAUDE_CODE_DENY_RULES, DEFAULT_EXTERNAL_TOOLS

    assert "rg" in DEFAULT_EXTERNAL_TOOLS  # rg stays in the default allow set — only --pre denied
    assert "Bash(rg:*)" not in CLAUDE_CODE_DENY_RULES  # must never blanket-deny the whole tool
    pre_rules = [r for r in CLAUDE_CODE_DENY_RULES if r.startswith("Bash(rg ")]
    assert pre_rules, "expected at least one rg --pre deny rule"
    for rule in pre_rules:
        assert "--pre" in rule


def test_guard_rule_subcommand_flags_arity_relaxed_to_1_or_2():
    # rig-cli#187 relaxed subcommand_flags from "exactly 2 tokens" to "1 or 2" (a flat command
    # like `rg` needs only 1). Pin the boundary: 1 and 2 are valid, 0 and 3 are still rejected.

    from riglib.permissions import GuardRule

    GuardRule("ok-1token", "hint", "subcommand_flags", ("rg",), ("--pre",))
    GuardRule("ok-2token", "hint", "subcommand_flags", ("git", "push"), ("--force",))
    with pytest.raises(ValueError, match="1 or 2 tokens"):
        GuardRule("bad-0token", "hint", "subcommand_flags", (), ("--pre",))
    with pytest.raises(ValueError, match="1 or 2 tokens"):
        GuardRule("bad-3token", "hint", "subcommand_flags", ("a", "b", "c"), ("--pre",))


def test_guard_rule_flags_with_value_must_be_subset_of_flags():
    # rig-cli#187 (round 2): flags_with_value is a PER-FLAG opt-in for the joined `flag=value`
    # match, not a family-wide behavior — it must name only flags already in `flags`, and
    # only `subcommand_flags` rules may use it at all (a construction error is far cheaper
    # than a silent mismatch in a security guard).

    from riglib.permissions import GuardRule

    GuardRule("ok-subset", "hint", "subcommand_flags", ("rg",), ("--pre", "--pre-glob"),
              flags_with_value=("--pre",))
    with pytest.raises(ValueError, match="flags_with_value must be a subset of flags"):
        GuardRule("bad-not-subset", "hint", "subcommand_flags", ("rg",), ("--pre",),
                   flags_with_value=("--not-in-flags",))
    with pytest.raises(ValueError, match="argv_prefix needs tokens only"):
        GuardRule("bad-argv-prefix-with-value", "hint", "argv_prefix", ("sudo", "rm"),
                   flags_with_value=("--force",))
    with pytest.raises(ValueError, match="flag_value_prefix needs"):
        GuardRule("bad-flag-value-prefix-with-value", "hint", "flag_value_prefix", ("git",),
                   ("-c",), ("core.hooksPath=",), flags_with_value=("-c",))
