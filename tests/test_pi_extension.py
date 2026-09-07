"""pi permission parity — the `permission-guard` extension provisioning.

pi ships no built-in permission system; rig delivers the deny/ask belt by installing the
`permission-guard` pi extension (discovered in agent-tools `pi-extensions/`) and writing the
rig-owned policy file the extension reads. These tests cover the full engine path: catalog
discovery → plan action → runner install (idempotent + backup) → drift, plus the registry/config
wiring that stops pi being an N/A skip.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from riglib.actions import run_plan
from riglib.catalog import Catalog
from riglib.config import ConfigError, LoadedConfig, validate
from riglib.drift import detect
from riglib.harness_skills import instruction_file_for, pi_agent_dir, pi_user_path
from riglib.permissions import (
    PI_ASK_RULES,
    PI_DENY_RULES,
    harness_permission_extension,
    pi_policy_document,
)
from riglib.plan import Action, InstallPlan, PlanError, build


# ── registry + policy document ────────────────────────────────────────────────────
def test_pi_permission_extension_is_registered():
    assert harness_permission_extension("pi") == "permission-guard"
    assert harness_permission_extension("claude-code") is None


def test_pi_policy_document_encodes_the_baseline():
    doc = pi_policy_document()
    assert doc["version"] == 1 and doc["default"] == "allow"
    ids = {r["id"] for r in doc["rules"]}
    # the deny baseline the claude-code allowlist also encodes, in flag-anywhere dialect
    assert {"gh-pr-merge", "git-force-push", "git-no-verify", "sudo-rm", "screencapture"} <= ids
    assert {"pkill", "killall", "git-reset-hard"} <= ids
    force = next(r for r in doc["rules"] if r["id"] == "git-force-push")
    assert force["action"] == "deny" and "--force" in force["flagsAny"] and "--force-with-lease" not in force["flagsAny"]


def test_pi_policy_document_overrides_replace_baseline():
    doc = pi_policy_document(deny_override=[], ask_override=[])
    assert doc["rules"] == []
    assert len(pi_policy_document()["rules"]) == len(PI_DENY_RULES) + len(PI_ASK_RULES)


# ── harness_skills: agent dir + the corrected AGENTS.md path ───────────────────────
def test_pi_agent_dir_honors_env(monkeypatch):
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    assert pi_agent_dir() == "~/.pi/agent"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", "/custom/pi")
    assert pi_agent_dir() == "/custom/pi"
    assert pi_user_path("extensions/x") == "/custom/pi/extensions/x"


def test_pi_instruction_file_is_agent_dir_not_config_pi(monkeypatch):
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    # the corrected path — pi reads ~/.pi/agent/AGENTS.md, NOT ~/.config/pi/AGENTS.md
    assert instruction_file_for("pi") == "~/.pi/agent/AGENTS.md"


# ── catalog discovery ──────────────────────────────────────────────────────────────
def test_catalog_discovers_pi_extension(fake_agent_tools):
    cat = Catalog.scan(str(fake_agent_tools))
    item = cat.get("pi_extensions", "permission-guard")
    assert item is not None
    assert item.category == "pi_extensions"
    assert (item.path / "index.ts").is_file()


def test_catalog_ignores_pi_extension_without_index_ts(fake_agent_tools):
    # a dir with no index.ts is not a discoverable pi extension
    (fake_agent_tools / "pi-extensions" / "not-an-ext").mkdir(parents=True)
    (fake_agent_tools / "pi-extensions" / "not-an-ext" / "README.md").write_text("x\n")
    cat = Catalog.scan(str(fake_agent_tools))
    assert cat.get("pi_extensions", "not-an-ext") is None


# ── plan ────────────────────────────────────────────────────────────────────────────
def _pi_cfg(repo: Path, source: Path) -> LoadedConfig:
    return LoadedConfig(
        repo_root=repo,
        data={
            "agent_tools_source": str(source),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "permissions": {"kind": "pi"},
        },
    )


def _pi_action(plan: InstallPlan) -> Action:
    return next(a for a in plan.actions if a.kind == "provision_pi_extension")


def test_plan_emits_pi_extension_action(tmp_path, fake_agent_tools, monkeypatch):
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "piroot"))
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = build(_pi_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    action = _pi_action(plan)
    assert action.target == tmp_path / "piroot" / "extensions" / "permission-guard"
    assert action.options["policy_file"] == str(tmp_path / "piroot" / "rig-permission-policy.json")
    assert action.options["extension"] == "permission-guard"
    assert action.options["policy"]["rules"]  # carries the resolved baseline
    # pi is NOT recorded as an N/A skip anymore
    assert not any("has no allowlist to provision" in n and "pi" in n for n in plan.notes)


def test_plan_instruction_policy_and_extension_share_pi_coding_agent_dir_root(
    tmp_path, fake_agent_tools, monkeypatch
):
    """The extension target
    (`provision_pi_extension`, via `_build_pi_extension`/`pi_user_path`) and the advisory
    instruction-policy target (`provision_instruction_policy`, via `_build_guard_policy`) must
    resolve under the SAME root when `PI_CODING_AGENT_DIR` is overridden — a split root means the
    advisory AGENTS.md block is written where the running pi instance never looks. Both must route
    through the dynamic resolver (`instruction_file_for`/`pi_user_path`), never the static
    `HARNESS_INSTRUCTION_POLICY[kind]` dict snapshot, which does not honor the override."""
    piroot = tmp_path / "piroot"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(piroot))
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = build(_pi_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    extension_action = _pi_action(plan)
    assert extension_action.target == piroot / "extensions" / "permission-guard"
    instr_actions = [a for a in plan.actions if a.kind == "provision_instruction_policy" and a.options["kind"] == "pi"]
    assert len(instr_actions) == 1, plan.actions
    assert instr_actions[0].target == piroot / "AGENTS.md"


def test_plan_keeps_baseline_when_deny_ask_absent_for_pi(tmp_path, fake_agent_tools, monkeypatch):
    # Pins the invariant the override logic rests on: an ABSENT deny/ask key (not an empty list)
    # must still yield the full baked baseline, never an empty policy.
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "piroot"))
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = build(_pi_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    action = _pi_action(plan)
    assert len(action.options["policy"]["rules"]) > 1  # the full baked baseline, not wiped


def test_plan_rejects_non_list_deny_for_pi_sole_target(tmp_path, fake_agent_tools, monkeypatch):
    # A scalar deny (a plausible YAML typo, e.g. `deny: "Bash(rm:*)"` instead of a list) is
    # STILL "accepted then enforced nowhere" on the sole target — it must get the SAME treatment
    # as a well-formed non-empty list (no `isinstance(val, list)` carve-out that would silently
    # downgrade a malformed override to a mere note while a well-formed one raises).
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "piroot"))
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _pi_cfg(repo, fake_agent_tools)
    cfg.data["permissions"]["deny"] = "Bash(rm:*)"
    with pytest.raises(PlanError, match="permissions.deny"):
        build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")


def test_plan_notes_non_list_deny_when_pi_is_one_of_several_targets(tmp_path, fake_agent_tools, monkeypatch):
    # Same malformed-scalar shape as above, but pi is one of SEVERAL configured targets — dropped
    # with a note like any other non-empty override in that case, not raised.
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "piroot"))
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _pi_cfg(repo, fake_agent_tools)
    del cfg.data["permissions"]["kind"]
    cfg.data["harness"] = {"enabled": True, "kind": "claude-code", "kinds": ["claude-code", "pi"]}
    cfg.data["permissions"]["deny"] = "Bash(rm:*)"
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert any("raw deny entries dropped" in n and "pi" in n for n in plan.notes)


def test_plan_notes_allowlist_keys_ignored_for_pi(tmp_path, fake_agent_tools, monkeypatch):
    # pi has no additively-mergeable command allowlist — tools/extra/disable/allow must be
    # noted as ignored, not silently no-op'd.
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "piroot"))
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _pi_cfg(repo, fake_agent_tools)
    cfg.data["permissions"]["extra"] = ["kubectl"]
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert any("ignored for harness 'pi'" in n and "extra" in n for n in plan.notes)


def test_plan_honors_empty_deny_ask_override_for_pi(tmp_path, fake_agent_tools, monkeypatch):
    # permissions.deny: [] / ask: [] REPLACE the baked baseline wholesale (disables it) — the
    # same "[] disables it" semantics the claude-code allowlist path honors.
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "piroot"))
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _pi_cfg(repo, fake_agent_tools)
    cfg.data["permissions"]["deny"] = []
    cfg.data["permissions"]["ask"] = []
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    action = _pi_action(plan)
    assert action.options["policy"]["rules"] == []


def test_plan_wipes_only_the_overridden_role_for_pi(tmp_path, fake_agent_tools, monkeypatch):
    # deny/ask overrides are independent — wiping deny must NOT also wipe ask, and vice versa.
    full_baseline_count = len(pi_policy_document()["rules"])
    ask_only_count = len(pi_policy_document(deny_override=[])["rules"])
    assert 0 < ask_only_count < full_baseline_count  # sanity: baseline has BOTH deny and ask rules

    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "piroot"))
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _pi_cfg(repo, fake_agent_tools)
    cfg.data["permissions"]["deny"] = []
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    action = _pi_action(plan)
    assert len(action.options["policy"]["rules"]) == ask_only_count  # ask rules preserved


def test_plan_provisions_pi_extension_when_permissions_kind_pinned_off_harness_kind(
    tmp_path, fake_agent_tools, monkeypatch
):
    # permissions.kind is independently settable from harness.kind — pinning permissions.kind
    # to pi while harness.kind is claude-code must still provision the pi extension.
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "piroot"))
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _pi_cfg(repo, fake_agent_tools)
    cfg.data["harness"] = {"enabled": False, "kind": "claude-code"}
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    action = _pi_action(plan)
    assert action.options["kind"] == "pi"


def test_plan_rejects_override_when_permissions_kind_pins_pi_despite_claude_code_in_harness_kinds(
    tmp_path, fake_agent_tools, monkeypatch
):
    # Discriminating case: `permissions.kind: pi` (pinned) alongside `harness.kinds: [claude-code,
    # pi]`. `_permissions_kinds` honors the EXPLICIT `permissions.kind` pin over the harness-derived
    # list — claude-code's allowlist is NOT built at all in this config (only "pi" is a permissions
    # target), so a non-empty deny/ask override truly lands nowhere and must still raise, even
    # though claude-code superficially "looks" configured via `harness.kinds`. Pins down that the
    # `kinds` passed into `_build_pi_extension` is the PERMISSIONS-resolved list, not the raw
    # harness-derived one — the two diverge exactly in this shape.
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "piroot"))
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _pi_cfg(repo, fake_agent_tools)  # permissions.kind: pi
    cfg.data["harness"] = {"enabled": True, "kind": "claude-code", "kinds": ["claude-code", "pi"]}
    cfg.data["permissions"]["deny"] = ["Bash(sudo rm:*)"]
    with pytest.raises(PlanError, match="permissions.deny"):
        build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")


def test_plan_rejects_non_empty_deny_ask_override_when_pi_is_the_sole_target(tmp_path, fake_agent_tools, monkeypatch):
    # A populated deny/ask override is in claude-code's rule-STRING dialect (Bash(x:*)), which
    # doesn't translate into pi's structured argvAll/flagsAny rule dicts. When pi is the ONLY
    # configured permissions target, silently dropping it would mean the override is accepted at
    # config time but enforced NOWHERE at all — a real misconfiguration must fail loudly (PlanError)
    # instead of a plan note nobody is guaranteed to read.
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "piroot"))
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _pi_cfg(repo, fake_agent_tools)
    cfg.data["permissions"]["deny"] = ["Bash(sudo rm:*)"]
    with pytest.raises(PlanError, match="permissions.deny"):
        build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")


def test_plan_rejects_non_empty_ask_override_when_pi_is_the_sole_target(tmp_path, fake_agent_tools, monkeypatch):
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "piroot"))
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _pi_cfg(repo, fake_agent_tools)
    cfg.data["permissions"]["ask"] = ["Bash(pkill:*)"]
    with pytest.raises(PlanError, match="permissions.ask"):
        build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")


def test_plan_rejects_and_reports_both_roles_when_deny_and_ask_both_set_on_sole_target(
    tmp_path, fake_agent_tools, monkeypatch
):
    # When BOTH deny and ask are set on a sole target, the error must name both — reporting only
    # the first would make the user fix deny, rerun, and only then discover ask is equally broken.
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "piroot"))
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _pi_cfg(repo, fake_agent_tools)
    cfg.data["permissions"]["deny"] = ["Bash(sudo rm:*)"]
    cfg.data["permissions"]["ask"] = ["Bash(pkill:*)"]
    with pytest.raises(PlanError, match=r"permissions\.deny/permissions\.ask"):
        build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")


def test_plan_drops_non_empty_deny_override_with_note_when_pi_is_one_of_several_targets(
    tmp_path, fake_agent_tools, monkeypatch
):
    # Unlike the sole-target case above: when pi is one of SEVERAL configured permissions
    # targets, the override still takes effect for the other (compatible) harnesses — dropping it
    # for pi alone with a visible note is a reasonable partial application, not a config-time
    # error (mirrors the opencode dialect-mismatch drop).
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "piroot"))
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _pi_cfg(repo, fake_agent_tools)
    del cfg.data["permissions"]["kind"]
    cfg.data["harness"] = {"enabled": True, "kind": "claude-code", "kinds": ["claude-code", "pi"]}
    cfg.data["permissions"]["deny"] = ["Bash(sudo rm:*)"]
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    action = _pi_action(plan)
    assert action.options["policy"]["rules"]  # baseline still applied, not silently emptied
    assert any("raw deny entries dropped" in n and "pi" in n for n in plan.notes)


def test_plan_rejects_non_empty_deny_override_when_pi_plus_opencode_neither_consumes_it(
    tmp_path, fake_agent_tools, monkeypatch
):
    # Regression: "pi is the only configured target" (`kinds == [kind]`) is the WRONG predicate
    # for "this override is enforced nowhere" — a `kinds=[pi, opencode]` config is EQUALLY dead,
    # since opencode also drops a claude-code-dialect override (it uses its own baked
    # opencode-dialect deny/ask baseline instead — see `_resolve_permission_rules`). The correct
    # check is "does any configured kind actually consume the claude-code rule-string dialect"
    # (only claude-code does), not "is pi literally the sole entry in `kinds`".
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "piroot"))
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _pi_cfg(repo, fake_agent_tools)
    del cfg.data["permissions"]["kind"]
    cfg.data["harness"] = {"enabled": True, "kind": "pi", "kinds": ["opencode"]}
    cfg.data["permissions"]["deny"] = ["Bash(sudo rm:*)"]
    with pytest.raises(PlanError, match="permissions.deny"):
        build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")


def test_plan_notes_when_extension_missing_from_catalog(tmp_path, fake_agent_tools, monkeypatch):
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "piroot"))
    import shutil

    shutil.rmtree(fake_agent_tools / "pi-extensions")
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = build(_pi_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert not any(a.kind == "provision_pi_extension" for a in plan.actions)
    assert any("not found in the agent-tools catalog" in n for n in plan.notes)


def test_plan_rejects_non_empty_override_even_when_extension_missing_from_catalog(
    tmp_path, fake_agent_tools, monkeypatch
):
    # Regression: the "enforced nowhere" validation must fire REGARDLESS of whether the pi
    # extension carrier is present in the catalog — an earlier version returned early on a
    # missing carrier (with just a "not found in catalog" note) before ever reaching the deny/ask
    # check, silently accepting an override that plainly can never be enforced (there is no
    # extension to write a policy file for at all).
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "piroot"))
    import shutil

    shutil.rmtree(fake_agent_tools / "pi-extensions")
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _pi_cfg(repo, fake_agent_tools)
    cfg.data["permissions"]["deny"] = ["Bash(sudo rm:*)"]
    with pytest.raises(PlanError, match="permissions.deny"):
        build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")


# ── validate ────────────────────────────────────────────────────────────────────────
def test_validate_accepts_permissions_kind_pi():
    validate({"version": 1, "permissions": {"kind": "pi"}})  # no raise — pi is provisioned now


def test_validate_still_rejects_na_kinds():
    with pytest.raises(ConfigError):
        validate({"version": 1, "permissions": {"kind": "gemini"}})


# ── runner: install + policy write, idempotent, backup ───────────────────────────────
def _build_pi_plan(tmp_path, fake_agent_tools, monkeypatch) -> InstallPlan:
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "piroot"))
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    full = build(_pi_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    # isolate: run ONLY the pi action so the test never touches other areas / real HOME
    return InstallPlan(actions=[_pi_action(full)], on_conflict=full.on_conflict)


def test_runner_installs_extension_and_writes_policy(tmp_path, fake_agent_tools, monkeypatch):
    plan = _build_pi_plan(tmp_path, fake_agent_tools, monkeypatch)
    run_plan(plan)
    piroot = tmp_path / "piroot"
    assert (piroot / "extensions" / "permission-guard" / "index.ts").is_file()
    policy_file = piroot / "rig-permission-policy.json"
    assert policy_file.is_file()
    doc = json.loads(policy_file.read_text())
    assert {r["id"] for r in doc["rules"]} >= {"git-force-push", "pkill"}


def test_runner_is_idempotent(tmp_path, fake_agent_tools, monkeypatch):
    plan = _build_pi_plan(tmp_path, fake_agent_tools, monkeypatch)
    run_plan(plan)
    report = run_plan(plan)  # second apply — everything already correct
    assert all(r.status == "skipped" for r in report.results), [r.detail for r in report.results]


def test_runner_backs_up_conflicting_policy(tmp_path, fake_agent_tools, monkeypatch):
    plan = _build_pi_plan(tmp_path, fake_agent_tools, monkeypatch)
    policy_file = tmp_path / "piroot" / "rig-permission-policy.json"
    policy_file.parent.mkdir(parents=True, exist_ok=True)
    policy_file.write_text('{"version":1,"default":"allow","rules":[{"id":"stale"}]}\n')
    report = run_plan(plan)  # on_conflict defaults to backup
    # look up by action kind, not index — robust against a future change to plan action ordering
    result = next(r for r in report.results if r.action.kind == "provision_pi_extension")
    assert result.backup is not None
    # the desired policy is now in place; the concrete backup path holds the stale content
    assert "stale" not in policy_file.read_text()
    assert "stale" in result.backup.read_text()


# ── drift ─────────────────────────────────────────────────────────────────────────
def test_runner_double_backup_surfaces_both_restore_paths(tmp_path, fake_agent_tools, monkeypatch):
    # when BOTH the extension dir and the policy file conflict, ActionResult carries one structured
    # backup slot — but neither restore path is lost: both appear in the result detail string.
    plan = _build_pi_plan(tmp_path, fake_agent_tools, monkeypatch)
    piroot = tmp_path / "piroot"
    ext_dir = piroot / "extensions" / "permission-guard"
    ext_dir.mkdir(parents=True, exist_ok=True)
    (ext_dir / "index.ts").write_text("stale extension\n")  # differs from source → will back up
    policy_file = piroot / "rig-permission-policy.json"
    policy_file.write_text('{"stale":true}\n')  # differs from desired → will back up
    report = run_plan(plan)  # on_conflict=backup
    result = report.results[0]
    assert result.status == "backed_up", result.detail
    # both backups referenced in the detail (each WriteOutcome.detail carries its own path)
    assert result.detail.count("backed up prior") == 2, result.detail


def test_drift_missing_before_apply(tmp_path, fake_agent_tools, monkeypatch):
    plan = _build_pi_plan(tmp_path, fake_agent_tools, monkeypatch)
    report = detect(plan)
    assert any(d.direction == "missing" and "not installed" in d.detail for d in report.items)
    assert any(d.direction == "missing" and "policy file not written" in d.detail for d in report.items)


def test_drift_clean_after_apply(tmp_path, fake_agent_tools, monkeypatch):
    plan = _build_pi_plan(tmp_path, fake_agent_tools, monkeypatch)
    run_plan(plan)
    report = detect(plan)
    pi_items = [d for d in report.items if d.category == "permissions"]
    assert pi_items == [], [d.detail for d in pi_items]


def test_drift_modified_when_policy_hand_edited(tmp_path, fake_agent_tools, monkeypatch):
    plan = _build_pi_plan(tmp_path, fake_agent_tools, monkeypatch)
    run_plan(plan)
    policy_file = tmp_path / "piroot" / "rig-permission-policy.json"
    policy_file.write_text('{"version":1,"default":"allow","rules":[]}\n')  # user gutted it
    report = detect(plan)
    assert any(d.direction == "modified" and "policy file differs" in d.detail for d in report.items)


def test_drift_modified_when_installed_extension_differs(tmp_path, fake_agent_tools, monkeypatch):
    plan = _build_pi_plan(tmp_path, fake_agent_tools, monkeypatch)
    run_plan(plan)
    # a stale/tampered installed extension file (differs from the agent-tools source)
    (tmp_path / "piroot" / "extensions" / "permission-guard" / "index.ts").write_text("tampered\n")
    report = detect(plan)
    assert any(d.direction == "modified" and "differs from the agent-tools source" in d.detail for d in report.items)


def test_drift_reports_modified_instead_of_crashing_on_invalid_utf8_policy(tmp_path, fake_agent_tools, monkeypatch):
    # The permission-guard extension itself fails CLOSED on an unreadable/invalid policy file
    # (falls back to its baked baseline) — drift (and therefore `rig status`/config-web) must
    # report the same broken artifact as `modified`, not crash the whole command with an
    # uncaught UnicodeDecodeError.
    plan = _build_pi_plan(tmp_path, fake_agent_tools, monkeypatch)
    run_plan(plan)
    policy_file = tmp_path / "piroot" / "rig-permission-policy.json"
    policy_file.write_bytes(b"\xff\xfe not valid utf-8")
    report = detect(plan)  # must not raise
    assert any(
        d.direction == "modified" and "policy file unreadable" in d.detail for d in report.items
    ), report.items


def test_drift_reports_modified_instead_of_crashing_on_unreadable_policy_permissions(
    tmp_path, fake_agent_tools, monkeypatch
):
    # Same fail-explicit contract for an OSError (e.g. a permission-denied policy file), not
    # just UnicodeDecodeError.
    plan = _build_pi_plan(tmp_path, fake_agent_tools, monkeypatch)
    run_plan(plan)
    policy_file = tmp_path / "piroot" / "rig-permission-policy.json"
    policy_file.chmod(0o000)
    try:
        # root bypasses file-mode read checks entirely, so chmod(0o000) would silently fail to
        # exercise the OSError path under a root-run CI container — fail loudly rather than
        # silently not testing anything.
        if os.access(policy_file, os.R_OK):
            pytest.skip("running as a user that can read a 0o000 file (root?) — cannot exercise this path")
        report = detect(plan)  # must not raise
    finally:
        policy_file.chmod(0o644)  # restore so cleanup can remove tmp_path
    assert any(
        d.direction == "modified" and "policy file unreadable" in d.detail for d in report.items
    ), report.items


def test_pi_policy_document_does_not_alias_baseline_lists():
    # mutating a returned rule's nested list must NOT corrupt the module baseline for the next call
    doc = pi_policy_document()
    force = next(r for r in doc["rules"] if r["id"] == "git-force-push")
    force["flagsAny"].append("--nuke")
    fresh = next(r for r in pi_policy_document()["rules"] if r["id"] == "git-force-push")
    assert "--nuke" not in fresh["flagsAny"]


def test_runner_write_failure_surfaces_as_error(tmp_path, fake_agent_tools, monkeypatch):
    # a policy-write failure must surface as `error`, never be masked (run_plan converts the
    # raised IO error into an error ActionResult; the handler's status precedence puts error first).
    plan = _build_pi_plan(tmp_path, fake_agent_tools, monkeypatch)
    action = plan.actions[0]
    blocker = tmp_path / "piroot" / "blocker"
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("x")  # a FILE where the policy path expects a directory parent
    action.options["policy_file"] = str(blocker / "policy.json")
    report = run_plan(InstallPlan(actions=[action], on_conflict="backup"))
    assert report.results[0].status == "error", report.results[0].detail
