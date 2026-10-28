"""Harness self-merge and ship-rule tests."""

from __future__ import annotations

import json
import re
import pytest
from riglib.actions import run_plan
from riglib.actions.harness_mode import SELF_MERGE_CARVE_OUT, SELF_MERGE_PERMISSIONS_ALLOW, SELF_MERGE_PERMISSIONS_DENY
from riglib.catalog import Catalog
from riglib.drift import detect
from riglib.plan import build
from tests.actions_drift_helpers import (
    _auto_harness_cfg,
    _bash_rule_matches,
    _harness_cfg,
    _user_settings,
)


def test_self_merge_carveout_written_to_fresh_user_settings(fake_agent_tools, tmp_path):
    # auto_mode + self_merge (default ON) → a fresh ~/.claude/settings.json gets the carve-out
    # merged into autoMode.allow as ["$defaults", "<carve-out>"]; re-apply is a no-op.
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools), cat, project_type="unknown")
    run_plan(plan)

    data = json.loads(_user_settings().read_text(encoding="utf-8"))
    assert data["autoMode"]["allow"] == ["$defaults", SELF_MERGE_CARVE_OUT]
    assert data["permissions"]["defaultMode"] == "auto"
    # the HARD unblock: the ship rules land in permissions.allow (the auto-mode Bash gate bypass)
    for rule in SELF_MERGE_PERMISSIONS_ALLOW:
        assert rule in data["permissions"]["allow"], f"{rule} missing from permissions.allow"

    # idempotent: re-apply changes nothing, and the no-op detail names the self-merge provisioning
    second = run_plan(plan)
    h2 = [r for r in second.results if r.action.category == "harness"]
    assert h2 and all(r.status == "skipped" for r in h2)
    assert "self-merge ship rules + carve-out present" in h2[0].detail
    assert json.loads(_user_settings().read_text(encoding="utf-8"))["autoMode"]["allow"] == [
        "$defaults", SELF_MERGE_CARVE_OUT
    ]


def test_self_merge_preserves_existing_allow_and_other_sections(fake_agent_tools, tmp_path):
    # an existing autoMode.allow with other entries + sibling soft_deny/hard_deny sections must
    # survive: the carve-out is appended once, nothing else in autoMode is touched.
    repo = tmp_path / "repo"
    repo.mkdir()
    _user_settings().parent.mkdir(parents=True, exist_ok=True)
    _user_settings().write_text(
        json.dumps({
            "autoMode": {
                "allow": ["$defaults", "Some prior carve-out"],
                "soft_deny": ["$defaults", "keep me"],
                "hard_deny": ["$defaults"],
            },
        }),
        encoding="utf-8",
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools), cat, project_type="unknown")
    run_plan(plan)

    data = json.loads(_user_settings().read_text(encoding="utf-8"))
    assert data["autoMode"]["allow"] == ["$defaults", "Some prior carve-out", SELF_MERGE_CARVE_OUT]
    # sibling sections untouched
    assert data["autoMode"]["soft_deny"] == ["$defaults", "keep me"]
    assert data["autoMode"]["hard_deny"] == ["$defaults"]

    # idempotent second apply
    run_plan(plan)
    assert json.loads(_user_settings().read_text(encoding="utf-8"))["autoMode"]["allow"] == [
        "$defaults", "Some prior carve-out", SELF_MERGE_CARVE_OUT
    ]


def test_self_merge_seeds_defaults_when_allow_is_non_list(fake_agent_tools, tmp_path):
    # a malformed autoMode.allow (not a list) is re-seeded with $defaults, then the carve-out
    # appended — never crashes on the bad shape.
    repo = tmp_path / "repo"
    repo.mkdir()
    _user_settings().parent.mkdir(parents=True, exist_ok=True)
    _user_settings().write_text(
        json.dumps({"autoMode": {"allow": "oops-a-string", "soft_deny": ["$defaults"]}}),
        encoding="utf-8",
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools), cat, project_type="unknown")
    run_plan(plan)
    data = json.loads(_user_settings().read_text(encoding="utf-8"))
    assert data["autoMode"]["allow"] == ["$defaults", SELF_MERGE_CARVE_OUT]
    assert data["autoMode"]["soft_deny"] == ["$defaults"]


def test_self_merge_off_writes_no_carveout(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools, self_merge=False), cat, project_type="unknown")
    run_plan(plan)
    data = json.loads(_user_settings().read_text(encoding="utf-8"))
    assert "autoMode" not in data


def test_self_merge_off_never_removes_an_existing_carveout(fake_agent_tools, tmp_path):
    # the "additive only" promise: flipping self_merge:false must NOT strip a carve-out the user
    # already has on disk — rig never removes autoMode.allow entries.
    repo = tmp_path / "repo"
    repo.mkdir()
    _user_settings().parent.mkdir(parents=True, exist_ok=True)
    _user_settings().write_text(
        json.dumps({"autoMode": {"allow": ["$defaults", SELF_MERGE_CARVE_OUT]}}),
        encoding="utf-8",
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools, self_merge=False), cat, project_type="unknown")
    run_plan(plan)
    data = json.loads(_user_settings().read_text(encoding="utf-8"))
    assert data["autoMode"]["allow"] == ["$defaults", SELF_MERGE_CARVE_OUT]


def test_self_merge_inert_without_auto_mode(fake_agent_tools, tmp_path):
    # self_merge only makes sense under auto (the classifier only runs then); with an
    # interactive/acceptEdits mode the carve-out is NOT written even if self_merge:true.
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _harness_cfg(repo, fake_agent_tools, mode="acceptEdits", self_merge=True),
        cat, project_type="unknown",
    )
    run_plan(plan)
    settings = repo / ".claude" / "settings.json"
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert "autoMode" not in data


def test_self_merge_and_mode_change_in_one_apply(fake_agent_tools, tmp_path):
    # combined write: the defaultMode key changes (backed up) AND the carve-out is added in a
    # single apply — the most complex status/detail branch.
    repo = tmp_path / "repo"
    repo.mkdir()
    _user_settings().parent.mkdir(parents=True, exist_ok=True)
    _user_settings().write_text(
        json.dumps({"permissions": {"defaultMode": "default"}}), encoding="utf-8"
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools), cat, project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]

    data = json.loads(_user_settings().read_text(encoding="utf-8"))
    assert data["permissions"]["defaultMode"] == "auto"
    assert data["autoMode"]["allow"] == ["$defaults", SELF_MERGE_CARVE_OUT]
    # a differing prior mode value → backed up before converging
    assert any(p.name.startswith("settings.json.rig-bak-") for p in _user_settings().parent.iterdir())
    hres = [r for r in report.results if r.action.category == "harness"]
    assert hres and "carve-out added" in hres[0].detail
    assert "ship rules" in hres[0].detail


def test_self_merge_carveout_added_when_mode_already_correct(fake_agent_tools, tmp_path):
    # mode key already 'auto' on disk but the carve-out is absent → only the carve-out is written,
    # status "updated", detail has no on_conflict-skip suffix.
    repo = tmp_path / "repo"
    repo.mkdir()
    _user_settings().parent.mkdir(parents=True, exist_ok=True)
    _user_settings().write_text(
        json.dumps({"permissions": {"defaultMode": "auto"}}), encoding="utf-8"
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools), cat, project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]

    data = json.loads(_user_settings().read_text(encoding="utf-8"))
    assert data["permissions"]["defaultMode"] == "auto"
    assert data["autoMode"]["allow"] == ["$defaults", SELF_MERGE_CARVE_OUT]
    hres = [r for r in report.results if r.action.category == "harness"]
    assert hres and hres[0].status == "updated"
    assert "on_conflict=skip" not in hres[0].detail


def test_self_merge_carveout_added_while_mode_key_left_under_skip(fake_agent_tools, tmp_path):
    # on_conflict=skip + a differing prior defaultMode: the mode key is LEFT (skip), but the
    # additive carve-out is still appended, and the detail carries the "mode key left" note.
    repo = tmp_path / "repo"
    repo.mkdir()
    _user_settings().parent.mkdir(parents=True, exist_ok=True)
    _user_settings().write_text(
        json.dumps({"permissions": {"defaultMode": "default"}}), encoding="utf-8"
    )
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _auto_harness_cfg(repo, fake_agent_tools)
    cfg.data["defaults"] = {"on_conflict": "skip"}
    plan = build(cfg, cat, project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]

    data = json.loads(_user_settings().read_text(encoding="utf-8"))
    assert data["permissions"]["defaultMode"] == "default"  # mode key left untouched
    assert data["autoMode"]["allow"] == ["$defaults", SELF_MERGE_CARVE_OUT]  # carve-out still added
    hres = [r for r in report.results if r.action.category == "harness"]
    assert hres and "mode key left, on_conflict=skip" in hres[0].detail


def test_self_merge_drift_missing_then_synced(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools), cat, project_type="unknown")
    run_plan(plan)

    data = json.loads(_user_settings().read_text(encoding="utf-8"))
    # drop the carve-out on disk → drift
    data["autoMode"]["allow"] = ["$defaults"]
    _user_settings().write_text(json.dumps(data), encoding="utf-8")
    mod = [x for x in detect(plan).items if x.category == "harness" and "self-merge" in x.detail.lower()]
    assert mod, "expected a self-merge carve-out drift item"

    # re-apply converges
    run_plan(plan)
    assert SELF_MERGE_CARVE_OUT in json.loads(_user_settings().read_text(encoding="utf-8"))["autoMode"]["allow"]
    assert not [x for x in detect(plan).items if x.category == "harness"]


def test_self_merge_writes_ship_rules_to_permissions_allow(fake_agent_tools, tmp_path):
    # THE fix: auto_mode + self_merge (default ON) writes the ship allow rules to permissions.allow
    # — the explicit allow the auto-mode Bash classifier needs to STOP vetoing `gh ship`. This is
    # the piece the natural-language autoMode.allow carve-out alone never provided.
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools), cat, project_type="unknown")
    run_plan(plan)

    perms = json.loads(_user_settings().read_text(encoding="utf-8"))["permissions"]
    allow = perms["allow"]
    for rule in ("Bash(gh ship:*)", "Bash(fj ship:*)"):
        assert rule in allow, f"{rule} not written to permissions.allow"
    # the raw scripts are DENIED (gh ship / fj ship are the only ship doors), never allowed
    assert not [r for r in allow if "ship.sh" in r]
    for rule in SELF_MERGE_PERMISSIONS_DENY:
        assert rule in perms["deny"], f"{rule} not written to permissions.deny"
    # Claude Code rejects a rule mixing a leading/inner `*` with the trailing `:*` prefix syntax
    for rule in (*SELF_MERGE_PERMISSIONS_ALLOW, *SELF_MERGE_PERMISSIONS_DENY):
        assert not (rule.endswith(":*)") and "*" in rule.removesuffix(":*)")), f"{rule} mixes * with :*"

    # idempotent: re-apply adds nothing more
    run_plan(plan)
    allow2 = json.loads(_user_settings().read_text(encoding="utf-8"))["permissions"]["allow"]
    assert allow2.count("Bash(gh ship:*)") == 1


@pytest.mark.parametrize(
    "command",
    [
        "ship.sh", "ship.sh 12", "pr-ship.sh", "pr-ship.sh 12",
        "./ship.sh 12", "../ship.sh 12", "../scripts/ship.sh 12",
        "scripts/ship.sh", "scripts/ship.sh 12", "scripts/pr-ship.sh 12",
        ".claude/scripts/pr-ship.sh 12", "ci/ship/ship.sh 12",
        "/abs/path/ship.sh 12", "~/x/pr-ship.sh 12",
        "bash scripts/ship.sh", "bash ./ci/ship/ship.sh 3", "sh ~/x/ship.sh 3",
        "zsh scripts/ship.sh", "source scripts/ship.sh", ". ./ship.sh",
    ],
)
def test_ship_script_deny_covers_invocation_shape(command):
    # GH-635: the deny set must catch each realistic raw-script invocation.
    assert any(_bash_rule_matches(rule, command) for rule in SELF_MERGE_PERMISSIONS_DENY), (
        f"{command!r} not denied"
    )


@pytest.mark.parametrize(
    "command",
    ["gh ship", "gh ship 12", "fj ship 12", "cat scripts/ship.sh", "git add scripts/ship.sh"],
)
def test_ship_script_deny_leaves_sanctioned_doors_and_reads_alone(command):
    # deny outranks allow, so an over-broad rule here would break `gh ship` / `fj ship` themselves.
    assert not any(_bash_rule_matches(rule, command) for rule in SELF_MERGE_PERMISSIONS_DENY), (
        f"{command!r} must not be denied"
    )


def test_self_merge_preserves_permissions_allow_and_keeps_gh_pr_merge_deny(fake_agent_tools, tmp_path):
    # the ship-rule append is additive AND leaves the `Bash(gh pr merge:*)` DENY in force: `gh ship`
    # stays the only merge path (ship.sh runs gh pr merge as a child process, not a gated call).
    repo = tmp_path / "repo"
    repo.mkdir()
    _user_settings().parent.mkdir(parents=True, exist_ok=True)
    _user_settings().write_text(
        json.dumps({"permissions": {"allow": ["Bash(docker:*)"]}}), encoding="utf-8"
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools), cat, project_type="unknown")
    run_plan(plan)

    perms = json.loads(_user_settings().read_text(encoding="utf-8"))["permissions"]
    assert "Bash(docker:*)" in perms["allow"]  # the user's own entry survives
    assert "Bash(gh ship:*)" in perms["allow"]
    # the deny provisioned by provision_permissions is intact — never relaxed by the ship allow
    assert "Bash(gh pr merge:*)" in perms["deny"]


def test_self_merge_off_writes_no_ship_rules(fake_agent_tools, tmp_path):
    # self_merge:false → apply_harness adds NO ship rules (provision_permissions never carried them)
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools, self_merge=False), cat, project_type="unknown")
    run_plan(plan)
    allow = json.loads(_user_settings().read_text(encoding="utf-8"))["permissions"].get("allow", [])
    assert "Bash(gh ship:*)" not in allow


def test_self_merge_ship_rules_drift_missing_then_synced(fake_agent_tools, tmp_path):
    # drift is surfaced BOTH ways: dropping the ship rules from permissions.allow trips a harness
    # drift item; re-apply converges; and the rig-owned ship rules are NEVER miscounted as
    # permissions "extras" (provision_permissions drift filters them out).
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools), cat, project_type="unknown")
    run_plan(plan)
    # a clean apply produces no permissions "extra" drift (nor a kept-addition line) for the
    # rig-written ship rules
    _assert_no_permissions_drift(detect(plan))

    data = json.loads(_user_settings().read_text(encoding="utf-8"))
    data["permissions"]["allow"] = _without_self_merge_rules(data["permissions"]["allow"])
    _user_settings().write_text(json.dumps(data), encoding="utf-8")
    miss = [x for x in detect(plan).items if x.category == "harness" and "ship rules" in x.detail]
    assert miss, "expected a self-merge ship-rules drift item"

    run_plan(plan)
    allow = json.loads(_user_settings().read_text(encoding="utf-8"))["permissions"]["allow"]
    assert all(r in allow for r in SELF_MERGE_PERMISSIONS_ALLOW)
    assert not [x for x in detect(plan).items if x.category == "harness"]


def test_self_merge_leaves_malformed_permissions_allow_untouched(fake_agent_tools, tmp_path):
    # a pre-existing NON-list permissions.allow is a shape error provision_permissions fail-closes on
    # — apply_harness must NOT silently overwrite it with the ship-rule list (that would destroy the
    # user's value AND mask the error). The malformed value survives; no ship rules are written.
    repo = tmp_path / "repo"
    repo.mkdir()
    _user_settings().parent.mkdir(parents=True, exist_ok=True)
    _user_settings().write_text(
        json.dumps({"permissions": {"allow": "oops-a-string"}}), encoding="utf-8"
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools), cat, project_type="unknown")
    run_plan(plan)  # provision_permissions errors on the shape; apply_harness must not clobber
    allow = json.loads(_user_settings().read_text(encoding="utf-8"))["permissions"]["allow"]
    assert allow == "oops-a-string", "apply_harness clobbered a malformed permissions.allow"


def test_self_merge_noop_detail_does_not_claim_absent_ship_rules(fake_agent_tools, tmp_path):
    # mode key already correct + carve-out present + a MALFORMED permissions.allow (left untouched):
    # apply_harness skips, and the 'nothing changed' detail must NOT falsely claim ship rules present.
    repo = tmp_path / "repo"
    repo.mkdir()
    _user_settings().parent.mkdir(parents=True, exist_ok=True)
    _user_settings().write_text(
        json.dumps({
            "permissions": {"defaultMode": "auto", "allow": "oops-a-string"},
            "autoMode": {"allow": ["$defaults", SELF_MERGE_CARVE_OUT]},
        }),
        encoding="utf-8",
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools), cat, project_type="unknown")
    report = run_plan(plan)
    hres = [r for r in report.results if r.action.category == "harness"]
    assert hres and hres[0].status == "skipped"
    assert "ship rules" not in hres[0].detail, "no-op detail falsely claimed ship rules present"
    assert "carve-out present" in hres[0].detail


def test_self_merge_drift_no_false_absent_row_for_malformed_allow(fake_agent_tools, tmp_path):
    # a MALFORMED permissions.allow surfaces ONE permissions shape-drift row; the harness self-merge
    # check must NOT also emit an inaccurate "ship rules absent" row for the same file.
    repo = tmp_path / "repo"
    repo.mkdir()
    _user_settings().parent.mkdir(parents=True, exist_ok=True)
    _user_settings().write_text(
        json.dumps({
            "permissions": {"defaultMode": "auto", "allow": "oops-a-string"},
            "autoMode": {"allow": ["$defaults", SELF_MERGE_CARVE_OUT]},
        }),
        encoding="utf-8",
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools), cat, project_type="unknown")
    items = detect(plan).items
    assert not [x for x in items if x.category == "harness" and "ship rules absent" in x.detail], \
        "malformed allow must not be reported as 'ship rules absent'"
    # the shape problem is still surfaced by the permissions check
    assert [x for x in items if x.category == "permissions" and x.direction == "modified"]


def test_self_merge_settings_file_helpers_normalize_identically(fake_agent_tools, tmp_path):
    # the extras-suppression correctness hinges on harness_settings_file(harness_action) and
    # permissions_settings_file(perms_action) returning EQUAL Paths for the same user settings file
    # (set membership in detect()). Pin that invariant so a future divergence can't silently turn the
    # rig-owned ship rules into false-positive `extra` drift.
    from riglib.actions.harness_mode import harness_settings_file
    from riglib.actions.permissions_specs import permissions_settings_file

    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools), cat, project_type="unknown")
    harness_action = next(a for a in plan.actions if a.kind == "apply_harness")
    perms_action = next(a for a in plan.actions if a.kind == "provision_permissions")
    assert harness_settings_file(harness_action) == permissions_settings_file(perms_action)


def test_self_merge_off_stray_ship_rules_reported_as_extra(fake_agent_tools, tmp_path):
    # two-way drift stays honest: with self_merge:false, ship rules left on disk (e.g. from a prior
    # self_merge:true) are NOT rig-owned for this plan → reported as permissions allow-extras, never
    # silently suppressed.
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools, self_merge=False), cat, project_type="unknown")
    run_plan(plan)
    data = json.loads(_user_settings().read_text(encoding="utf-8"))
    data["permissions"]["allow"] = [*data["permissions"].get("allow", []), *SELF_MERGE_PERMISSIONS_ALLOW]
    _user_settings().write_text(json.dumps(data), encoding="utf-8")
    # since rig-cli#357 permission extras are filed as KNOWN ("your additions, kept") rather than
    # drift — but they must still SURFACE (never silently suppressed as rig-owned).
    kept = [k for k in detect(plan).known if k.category == "permissions"]
    assert {k.name for k in kept} >= set(SELF_MERGE_PERMISSIONS_ALLOW), \
        "stray ship rules under self_merge:false must surface as kept permission additions"


def test_self_merge_no_ship_rules_when_skip_leaves_non_auto_mode(fake_agent_tools, tmp_path):
    # on_conflict=skip + an existing conflicting defaultMode ('default'): the mode key is LEFT
    # interactive. The ACTIVE permissions.allow ship rules (checked in EVERY mode) must NOT be written
    # — writing them would pre-approve `gh ship` while the harness is still interactive (codex #159 P2).
    # drift must NOT flag the ship rules missing while the mode stays non-auto (the mode-key drift row
    # already signals non-convergence).
    repo = tmp_path / "repo"
    repo.mkdir()
    _user_settings().parent.mkdir(parents=True, exist_ok=True)
    _user_settings().write_text(
        json.dumps({"permissions": {"defaultMode": "default"}}), encoding="utf-8"
    )
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _auto_harness_cfg(repo, fake_agent_tools)
    cfg.data["defaults"] = {"on_conflict": "skip"}
    plan = build(cfg, cat, project_type="unknown")
    run_plan(plan)

    data = json.loads(_user_settings().read_text(encoding="utf-8"))
    assert data["permissions"]["defaultMode"] == "default"  # mode left interactive under skip
    assert "Bash(gh ship:*)" not in data["permissions"].get("allow", []), \
        "ship rules pre-approved gh ship while defaultMode stayed interactive"
    assert not [x for x in detect(plan).items if x.category == "harness" and "ship rules absent" in x.detail], \
        "drift flagged ship rules missing while the mode is non-auto"


def test_self_merge_drift_no_ship_rules_missing_when_mode_non_auto(fake_agent_tools, tmp_path):
    # a clean auto apply, then defaultMode flipped to a non-auto value on disk AND the ship rules
    # dropped: auto is no longer in effect → drift must NOT report the ship rules missing (only the
    # mode-key drift row). apply couples the ship rules to the resulting auto mode.
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools), cat, project_type="unknown")
    run_plan(plan)
    data = json.loads(_user_settings().read_text(encoding="utf-8"))
    data["permissions"]["defaultMode"] = "default"
    data["permissions"]["allow"] = [r for r in data["permissions"]["allow"] if r not in SELF_MERGE_PERMISSIONS_ALLOW]
    _user_settings().write_text(json.dumps(data), encoding="utf-8")
    items = detect(plan).items
    assert not [x for x in items if x.category == "harness" and "ship rules absent" in x.detail], \
        "drift flagged ship rules missing while defaultMode is non-auto"
    assert [x for x in items if x.category == "harness" and "defaultMode" in x.detail], \
        "the mode-key drift must still be reported"


def test_harness_auto_writes_user_settings_not_repo(fake_agent_tools, tmp_path):
    # auto_mode:true with NO settings_path → CC `auto` is written to the USER settings file
    # (~/.claude/settings.json, HOME-isolated to tmp by the autouse fixture), NOT the repo —
    # CC ignores defaultMode:auto at project scope, so committing it per-repo would be a no-op.
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    # self_merge off keeps this test focused on the mode-key write (no autoMode.allow noise).
    cfg = _auto_harness_cfg(repo, fake_agent_tools, self_merge=False)
    plan = build(cfg, cat, project_type="unknown")
    user_settings = _user_settings()

    run_plan(plan)
    assert not (repo / ".claude").exists(), "auto must NOT write into the repo's project settings"
    data = json.loads(user_settings.read_text(encoding="utf-8"))
    assert data["permissions"]["defaultMode"] == "auto"

    # idempotent
    second = run_plan(plan)
    h2 = [r for r in second.results if r.action.category == "harness"]
    assert h2 and all(r.status == "skipped" for r in h2)

    # flip the value → modified drift detected on the user settings file
    data["permissions"]["defaultMode"] = "default"
    user_settings.write_text(json.dumps(data), encoding="utf-8")
    mod = [x for x in detect(plan).by_direction("modified") if x.category == "harness"]
    assert mod and "auto" in mod[0].detail


def _without_self_merge_rules(allow: list[str]) -> list[str]:
    return [r for r in allow if r not in SELF_MERGE_PERMISSIONS_ALLOW]


def _assert_no_permissions_drift(rep) -> None:
    assert not [x for x in rep.items if x.category == "permissions" and x.direction == "extra"]
    assert not [k for k in rep.known if k.category == "permissions"]
