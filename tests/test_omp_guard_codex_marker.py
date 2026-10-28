"""omp permissions provisioning — guard extension codegen, approval policy merge, (part 2 of 2).

instruction-file advisory policy, plan fan-out, two-way drift, and the activation probe.

Mirrors the execpolicy/permissions discipline: idempotent + additive, backup-on-conflict,
never clobbers the user's own values ('compatible unmanaged' is adopted, never rewritten),
and a re-apply is a true no-op. The omp guard TS is GENERATED from the single rule registry
in riglib.permissions — never a hand-copied second list (rig-cli#202).
"""
from __future__ import annotations

import pytest

from riglib.catalog import Catalog
from riglib.drift import detect
from riglib.omp_guard import (
    INCOMPATIBLE_MARKER_NAME,
    render_guard_ts,
)
from riglib.permissions import OMP_GUARD_ASK_RULES, OMP_GUARD_DENY_RULES
from riglib.plan import build
from tests.omp_guard_helpers import (
    _apply_approval,
    _apply_guard,
    _apply_policy,
    _bun_bin,
    _config_yml,
    _guard_path,
    _instr_cfg,
    _omp_cfg,
    _pin_home,
    _policy_path,
)


# ── pinned codex, marker lifecycle, registry-derived interlock ─────────────────────
def test_plan_pinned_codex_provisions_execpolicy(fake_agent_tools, tmp_path, monkeypatch):
    """permissions.kind: codex (newly pinnable) must emit the execpolicy action — a
    validated no-op would be the worst outcome (review finding)."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    cfg = _instr_cfg(repo, fake_agent_tools, "claude-code")
    cfg.data["permissions"] = {"kind": "codex", "tools": ["git"]}
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    acts = [a for a in plan.actions if a.kind == "provision_execpolicy"]
    assert [a.options["kind"] for a in acts] == ["codex"]


def test_apply_clears_stale_incompatible_marker(fake_agent_tools, tmp_path, monkeypatch):
    """rig apply IS the recovery path the drift message points at: a byte-current guard
    with a stale fail-closed marker gets the marker cleared (no live session needed)."""
    home, _, _ = _apply_guard(fake_agent_tools, tmp_path, monkeypatch)
    marker = _guard_path(home).parent / INCOMPATIBLE_MARKER_NAME
    marker.write_text("stale\n", encoding="utf-8")
    _, res, _ = _apply_guard(fake_agent_tools, tmp_path, monkeypatch)
    assert not marker.exists()
    assert "cleared" in res.detail
    # and drift goes clean immediately
    repo = tmp_path / "repo"
    cfg = _omp_cfg(repo, fake_agent_tools)
    rep = detect(build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    assert not [d for d in rep.items if "incompatible" in d.detail]


def test_interlock_derives_guard_from_registry_without_option(tmp_path, monkeypatch):
    """An approval action with NO guard_target option is STILL interlocked (registry-derived
    path) — the interlock must not depend on the plan's option wiring (fail closed)."""
    from riglib.actions.runner import _do_provision_harness_approval
    from riglib.plan import Action

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    cfg = home / ".omp" / "agent" / "config.yml"
    act = Action(kind="provision_harness_approval", category="permissions", item="omp",
                 source=tmp_path, target=cfg, options={"kind": "omp"})
    res = _do_provision_harness_approval(act, "backup")
    assert res.status == "error" and "refusing to relax" in res.detail


def test_probe_fixture_ts_parses_under_bun(tmp_path):
    """The probe fixture is generated TS — a syntax error in it makes omp silently skip the
    fixture and the probe report a false 'channel broken'. Parse-check it (this exact bug
    class shipped once)."""
    import subprocess

    from riglib.omp_probe import _fixture_ts

    bun = _bun_bin()
    if not bun:
        pytest.skip("bun runtime not available")
    f = tmp_path / "fixture.ts"
    f.write_text(_fixture_ts("deadbeef1234", tmp_path), encoding="utf-8")
    proc = subprocess.run([bun, "-e", f'import("{f}").then(() => process.stdout.write("parse ok"))'],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0 and "parse ok" in proc.stdout, proc.stderr


# ── parity, lookup edges, inert knobs, disabled end-state, marker_dir quoting ──────
def test_guard_rules_cover_the_same_intents_as_the_glob_belts():
    """The GuardRule registry and the claude-code/opencode glob baselines are two renderings
    of ONE policy — a rule added to one belt and forgotten in the other must fail loudly."""
    from riglib.permissions import (
        CLAUDE_CODE_ASK_RULES,
        CLAUDE_CODE_DENY_RULES,
        OPENCODE_ASK_RULES,
        OPENCODE_DENY_RULES,
    )

    # intent id → a substring at least one glob per belt must carry
    deny_intents = {
        "gh-pr-merge": "gh pr merge",
        "git-push-force": "--force",
        "git-commit-no-verify": "--no-verify",
        "sudo-rm": "sudo rm",
        "screencapture": "screencapture",
        "osascript": "osascript",
        "rg-pre": "--pre",
        "git-worktree-add": "git worktree add",
        # rig-cli#714: direct Runpod creation (argv forms; the belts carry the same prefixes)
        "runpodctl-pod-create": "runpodctl pod create",
        "runpodctl-pods-create": "runpodctl pods create",
        "runpodctl-serverless-create": "runpodctl serverless create",
        "runpodctl-sls-create": "runpodctl sls create",
        "runpodctl-cluster-create": "runpodctl cluster create",
        "runpodctl-create-pod": "runpodctl create pod",
        "runpodctl-create-pods": "runpodctl create pods",
        "flash-deploy": "flash deploy",
    }
    ask_intents = {"pkill": "pkill", "killall": "killall", "git-reset-hard": "--hard"}
    # belt intents are the shared subset; argv-precision EXTRAS the glob dialects cannot
    # express are listed explicitly (adding one still requires touching this map).
    guard_extras = {
        "git-config-injection",
        "eval-osascript-spawn",
        "eval-automator-spawn",
        "eval-screencapture-spawn",
        "eval-addressbook-read",
        "eval-calendars-read",
        "eval-photos-library-read",
        # rig-cli#701: judging WHAT a kill signals (operands, /proc lookups) is argv-only
        "kill-pstree-fed",
        "kill-broadcast",
        "signal-session-infra",
        # rig-cli#714: raw HTTP/GraphQL writes, MCP device writes, write targets and eval GraphQL
        # are surfaces the glob belts cannot see (claude-code denies the MCP tools whole-tool)
        "runpod-http-create",
        "runpod-graphql-create",
        "runpod-mcp-create",
        "runpod-mcp-scale-up",
        "eval-runpod-graphql-deploy",
        "eval-runpod-graphql-rent",
    }
    guard_ask_extras = {"kill-computed-pid"}
    assert {r.id for r in OMP_GUARD_DENY_RULES} == set(deny_intents) | guard_extras
    assert {r.id for r in OMP_GUARD_ASK_RULES} == set(ask_intents) | guard_ask_extras
    for marker in (*deny_intents.values(), *ask_intents.values()):
        assert any(marker in g for g in (*CLAUDE_CODE_DENY_RULES, *CLAUDE_CODE_ASK_RULES)), marker
        assert any(marker in g for g in (*OPENCODE_DENY_RULES, *OPENCODE_ASK_RULES)), marker


def test_approval_lookup_three_segment_null_intermediate():
    from riglib.actions.shared import approval_lookup

    assert approval_lookup({"a": None}, ("a", "b", "c")) == ("missing", None)
    assert approval_lookup({"a": {"b": None}}, ("a", "b", "c")) == ("missing", None)
    assert approval_lookup({"a": "nope"}, ("a", "b", "c")) == ("shape", "a")
    assert approval_lookup({"a": {"b": {"c": "x"}}}, ("a", "b", "c")) == ("present", "x")


def test_plan_notes_inert_allowlist_knobs_for_guard_kinds(fake_agent_tools, tmp_path, monkeypatch):
    """A user pinning omp with a custom deny list gets the baseline AND a visible note —
    never a silent no-op."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    cfg = _omp_cfg(repo, fake_agent_tools, kind="omp", deny=["Bash(docker:*)"])
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert any("inert" in n and "omp" in n and "deny" in n for n in plan.notes), plan.notes


def test_plan_notes_leftover_artifacts_when_permissions_disabled(fake_agent_tools, tmp_path, monkeypatch):
    """permissions.enabled: false leaves rig's relaxed artifacts on disk (rig never
    auto-deletes) — that must be a visible note, not a silent persistent yolo — but ONLY
    when something was actually provisioned (no noise on fresh machines)."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    cfg = _omp_cfg(repo, fake_agent_tools, enabled=False)
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert not any("never auto-deletes" in n for n in plan.notes), plan.notes
    # seed a provisioned guard → the note fires
    guard = _guard_path(home)
    guard.parent.mkdir(parents=True, exist_ok=True)
    guard.write_text("// rig guard\n", encoding="utf-8")
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert any("disabled" in n and "never auto-deletes" in n for n in plan.notes), plan.notes


def test_plan_notes_legacy_pi_instruction_leftover_under_override(fake_agent_tools, tmp_path, monkeypatch):
    """A machine that ran an OLDER rig-cli — before the pi
    instruction-policy target was resolved dynamically — could carry a stale managed block at
    the static default path (~/.pi/agent/AGENTS.md) even while PI_CODING_AGENT_DIR now points
    pi somewhere else. Disabling permissions must still surface that legacy leftover, not just
    the current override-resolved target (which correctly has nothing at it)."""
    home = tmp_path / "home"
    _pin_home(monkeypatch, home)
    custom_root = tmp_path / "custom-pi"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(custom_root))
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    cfg = _instr_cfg(repo, fake_agent_tools, "pi")
    cfg.data["permissions"] = {"enabled": False}
    # nothing at either location yet — no noise
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert not any("never auto-deletes" in n for n in plan.notes), plan.notes
    # seed ONLY the legacy static path (simulating a pre-dynamic-resolver rig-cli's leftover) —
    # the current override target (custom_root/AGENTS.md) stays absent.
    legacy = home / ".pi" / "agent" / "AGENTS.md"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("<!-- rig:permissions-policy -->\nold advisory block\n", encoding="utf-8")
    assert not (custom_root / "AGENTS.md").exists()
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert any("disabled" in n and "pi" in n and "never auto-deletes" in n for n in plan.notes), plan.notes


def test_plan_notes_true_historical_pi_instruction_leftover(fake_agent_tools, tmp_path, monkeypatch):
    """The TRUE pre-rig-cli#171 default was
    ~/.config/pi/AGENTS.md (the wrong path HARNESS_INSTRUCTION_FILES["pi"] held before that fix)
    — not ~/.pi/agent/AGENTS.md, which is already the CURRENT (fixed) default and so is only a
    meaningful 'legacy' candidate when an override is set (covered by the sibling test above).
    A machine that ran a pre-#171 rig-cli, then upgrades and disables permissions with NO
    override at all, must still get the leftover warning for the actual old wrong path."""
    home = tmp_path / "home"
    _pin_home(monkeypatch, home)
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    cfg = _instr_cfg(repo, fake_agent_tools, "pi")
    cfg.data["permissions"] = {"enabled": False}
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert not any("never auto-deletes" in n for n in plan.notes), plan.notes
    # seed ONLY the true historical wrong-path location — the current (fixed) default stays absent.
    true_legacy = home / ".config" / "pi" / "AGENTS.md"
    true_legacy.parent.mkdir(parents=True, exist_ok=True)
    true_legacy.write_text("<!-- rig:permissions-policy -->\nold advisory block\n", encoding="utf-8")
    assert not (home / ".pi" / "agent" / "AGENTS.md").exists()
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert any("disabled" in n and "pi" in n and "never auto-deletes" in n for n in plan.notes), plan.notes


def test_plan_notes_pi_extension_leftover_when_instruction_file_absent(fake_agent_tools, tmp_path, monkeypatch):
    """pi's OTHER enforcement artifact — the installed
    permission-guard extension + its JSON policy file — must ALSO be checked when permissions
    are disabled, not just the advisory AGENTS.md block. Without this, a live guard extension
    with a since-deleted/never-provisioned AGENTS.md gets no leftover warning at all, even
    though pi still auto-loads and enforces the extension."""
    home = tmp_path / "home"
    _pin_home(monkeypatch, home)
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    cfg = _instr_cfg(repo, fake_agent_tools, "pi")
    cfg.data["permissions"] = {"enabled": False}
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert not any("never auto-deletes" in n for n in plan.notes), plan.notes
    # seed ONLY the extension dir — no AGENTS.md anywhere.
    ext_dir = home / ".pi" / "agent" / "extensions" / "permission-guard"
    ext_dir.mkdir(parents=True, exist_ok=True)
    (ext_dir / "index.ts").write_text("export default function () {}\n", encoding="utf-8")
    assert not (home / ".pi" / "agent" / "AGENTS.md").exists()
    assert not (home / ".config" / "pi" / "AGENTS.md").exists()
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert any("disabled" in n and "pi" in n and "never auto-deletes" in n for n in plan.notes), plan.notes


def test_plan_notes_pi_extension_leftover_at_default_root_under_override(fake_agent_tools, tmp_path, monkeypatch):
    # Regression: a machine that provisioned pi at the DEFAULT root (~/.pi/agent), then later set
    # PI_CODING_AGENT_DIR to move pi elsewhere, must still get a leftover warning for the
    # still-live guard extension left behind at the OLD (default) root — checking only the
    # dynamic (override-resolved) root would miss it, mirroring the AGENTS.md legacy-candidate
    # fix above but for the extension + policy file artifacts.
    home = tmp_path / "home"
    _pin_home(monkeypatch, home)
    custom_root = tmp_path / "custom-pi"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(custom_root))
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    cfg = _instr_cfg(repo, fake_agent_tools, "pi")
    cfg.data["permissions"] = {"enabled": False}
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert not any("never auto-deletes" in n for n in plan.notes), plan.notes
    # seed ONLY the stale extension at the DEFAULT root — nothing at the custom (current) root.
    ext_dir = home / ".pi" / "agent" / "extensions" / "permission-guard"
    ext_dir.mkdir(parents=True, exist_ok=True)
    (ext_dir / "index.ts").write_text("export default function () {}\n", encoding="utf-8")
    assert not (custom_root / "extensions" / "permission-guard").exists()
    assert not (custom_root / "rig-permission-policy.json").exists()
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert any("disabled" in n and "pi" in n and "never auto-deletes" in n for n in plan.notes), plan.notes


def test_plan_notes_pi_policy_file_leftover_at_default_root_under_override(fake_agent_tools, tmp_path, monkeypatch):
    # Sibling of the extension-only case above: seeds ONLY the stale policy file at the default
    # root (no extension dir anywhere) — exercises the independent `static_policy != dynamic_policy`
    # branch as its own code path, distinct from the extension-dir branch above. NOTE: like its
    # sibling, this hand-seeds the file at the SAME literal path the check looks for (matching
    # this file's existing `test_plan_notes_legacy_pi_instruction_leftover_under_override`
    # convention) — it does not run real provisioning first, so it does NOT independently confirm
    # that `~/.pi/agent` is where provisioning actually writes by default; only that the check
    # looks in the right place given that assumption. That assumption is pinned separately by
    # `test_pi_agent_dir_honors_env` (asserts the unset-env default) in tests/test_pi_extension.py.
    home = tmp_path / "home"
    _pin_home(monkeypatch, home)
    custom_root = tmp_path / "custom-pi"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(custom_root))
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    cfg = _instr_cfg(repo, fake_agent_tools, "pi")
    cfg.data["permissions"] = {"enabled": False}
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert not any("never auto-deletes" in n for n in plan.notes), plan.notes
    # seed ONLY the stale policy file at the DEFAULT root — no extension dir anywhere, nothing at
    # the custom (current) root.
    default_root = home / ".pi" / "agent"
    default_root.mkdir(parents=True, exist_ok=True)
    (default_root / "rig-permission-policy.json").write_text('{"version":1,"default":"allow","rules":[]}\n')
    assert not (default_root / "extensions" / "permission-guard").exists()
    assert not (custom_root / "extensions" / "permission-guard").exists()
    assert not (custom_root / "rig-permission-policy.json").exists()
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert any("disabled" in n and "pi" in n and "never auto-deletes" in n for n in plan.notes), plan.notes


def test_render_guard_ts_bakes_marker_dir_safely():
    ts = render_guard_ts(marker_dir='/tmp/weird "quoted" \\ dir')
    assert 'BAKED_DIR = "/tmp/weird \\"quoted\\" \\\\ dir";' in ts
    # and the plain render has an empty baked dir (runtime import.meta.dir fallback)
    assert 'BAKED_DIR = "";' in render_guard_ts()


def test_drift_escalates_relaxed_posture_with_missing_guard(fake_agent_tools, tmp_path, monkeypatch):
    """The most dangerous combined state — yolo provisioned, guard belt gone — must render
    as an escalated item, not a routine missing file."""
    home, _, _ = _apply_guard(fake_agent_tools, tmp_path, monkeypatch)
    _, res, _ = _apply_approval(fake_agent_tools, tmp_path, monkeypatch)
    assert res.status in {"created", "skipped"}, res.detail
    _guard_path(home).unlink()  # guard deleted AFTER apply; yolo persists
    repo = tmp_path / "repo"
    cfg = _omp_cfg(repo, fake_agent_tools)
    rep = detect(build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    assert any("enforcement is OFF" in d.detail for d in rep.items), [d.detail for d in rep.items]


def test_approval_skip_policy_never_rewrites_existing_yaml(fake_agent_tools, tmp_path, monkeypatch):
    """on_conflict=skip + an existing (valid) config missing the managed key: the file is
    NOT re-serialized (yaml comments would be destroyed) — skipped, byte-identical, drift
    stays visible. Fresh files are still created under skip."""
    home, _, _ = _apply_guard(fake_agent_tools, tmp_path, monkeypatch)
    cfg_yml = _config_yml(home)
    original = "# my hand comment\ntools:\n  approvalMode: always-ask\n"
    cfg_yml.parent.mkdir(parents=True, exist_ok=True)
    cfg_yml.write_text(original, encoding="utf-8")
    _, res, _ = _apply_approval(fake_agent_tools, tmp_path, monkeypatch, on_conflict="skip")
    # a DIFFERING value is the never-clobber path (already covered); the rewrite path needs
    # a config that LACKS the key but is otherwise valid — use a fresh sibling key only:
    cfg_yml.write_text("# my hand comment\ntheme:\n  dark: titanium\n", encoding="utf-8")
    before = cfg_yml.read_text(encoding="utf-8")
    _, res2, _ = _apply_approval(fake_agent_tools, tmp_path, monkeypatch, on_conflict="skip")
    assert res2.status == "skipped", res2.detail
    assert "comments are not preserved" in res2.detail
    assert cfg_yml.read_text(encoding="utf-8") == before


def test_instruction_policy_writes_through_symlinked_target(fake_agent_tools, tmp_path, monkeypatch):
    """If the global AGENTS.md is a symlink (user aliased it to a file elsewhere), the
    splice writes THROUGH it — that file IS what the harness reads; drift follows the same
    link and reads clean. Pinned deliberately: rig never unlinks the user's alias."""
    home = tmp_path / "home"
    real = tmp_path / "elsewhere" / "pi-guide.md"
    real.parent.mkdir(parents=True)
    link = _policy_path(home, "pi")
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    _, res = _apply_policy(fake_agent_tools, tmp_path, monkeypatch)
    assert res.status == "created", res.detail
    assert "advisory" in real.read_text(encoding="utf-8")
    assert link.is_symlink()  # the alias itself is preserved


def test_cmd_doctor_failed_probe_yields_to_missing_required_dep(tmp_path, monkeypatch, capsys):
    """Precedence pinned: a missing REQUIRED dep keeps its (more actionable) guidance path
    even when an activation probe also failed."""
    import argparse

    import riglib.doctor
    import riglib.probes
    from riglib import cli_doctor, errors
    from riglib.doctor import DoctorReport, diagnose
    from riglib.omp_probe import PROBE_NAME
    from riglib.probes import ProbeResult

    args = argparse.Namespace(yes=False, optional=False, fix=False)
    monkeypatch.setattr(cli_doctor, "_handle_core_bare", lambda do_fix: False)
    from riglib import drift as _drift

    monkeypatch.setattr(_drift, "scan_missing_targets", lambda settings_paths=None: [])
    real = diagnose()
    report = DoctorReport(os=real.os, statuses=real.statuses)
    # force a required dep missing
    for st in report.statuses:
        if st.dep.required:
            st.present = False
            st.install_cmd = ["echo", "install"]
            break
    monkeypatch.setattr(riglib.doctor, "diagnose", lambda: report)
    monkeypatch.setenv("RIG_OMP_PROBE", "1")
    monkeypatch.setattr(riglib.probes, "run_probes", lambda: [ProbeResult(PROBE_NAME, False, "BROKEN")])
    rc = cli_doctor.cmd_doctor(args)
    assert rc == errors.EXIT_MISSING_DEP


def test_guard_provenance_corrupt_header_returns_none():
    from riglib.omp_guard import (
        GUARD_TEMPLATE_VERSION,
        guard_provenance,
        render_guard_ts,
    )

    ts = render_guard_ts()
    corrupted = ts.replace(f'"template": {GUARD_TEMPLATE_VERSION}', '"template": {', 1)
    assert guard_provenance(corrupted) is None
    assert guard_provenance("// no header at all\n") is None


def test_drift_reports_shape_conflict_for_non_dict_parent(fake_agent_tools, tmp_path, monkeypatch):
    """A managed key's parent existing as a non-dict (tools: "yolo") is modified drift,
    matching apply's conflict-skip — never a false 'missing (apply sets)' promise."""
    home = tmp_path / "home"
    _apply_guard(fake_agent_tools, tmp_path, monkeypatch)
    cfg_yml = _config_yml(home)
    cfg_yml.parent.mkdir(parents=True, exist_ok=True)
    cfg_yml.write_text('tools: "yolo"\n', encoding="utf-8")
    _pin_home(monkeypatch, home)
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    cfg = _omp_cfg(repo, fake_agent_tools)
    rep = detect(build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    assert any(d.direction == "modified" and "not an object" in d.detail for d in rep.items), \
        [d.detail for d in rep.items]


def test_install_guard_reports_template_upgrade_origin(fake_agent_tools, tmp_path, monkeypatch):
    """A rig-generated file from an OLDER template is named as such in the apply detail
    (the provenance header's purpose), not lumped with a foreign file."""
    from riglib.omp_guard import GUARD_TEMPLATE_VERSION

    old_version = GUARD_TEMPLATE_VERSION - 1
    home, _, _ = _apply_guard(fake_agent_tools, tmp_path, monkeypatch)
    path = _guard_path(home)
    old = path.read_text(encoding="utf-8").replace(
        f'"template": {GUARD_TEMPLATE_VERSION}', f'"template": {old_version}', 1
    )
    path.write_text(old, encoding="utf-8")
    _, res, _ = _apply_guard(fake_agent_tools, tmp_path, monkeypatch)
    assert f"was rig template v{old_version}" in res.detail, res.detail


def test_install_guard_unreadable_file_is_an_error(fake_agent_tools, tmp_path, monkeypatch):
    home, _, _ = _apply_guard(fake_agent_tools, tmp_path, monkeypatch)
    path = _guard_path(home)
    path.write_text("// drifted\n", encoding="utf-8")
    path.chmod(0o000)
    try:
        _, res, _ = _apply_guard(fake_agent_tools, tmp_path, monkeypatch)
        assert res.status == "error" and "cannot read" in res.detail
    finally:
        path.chmod(0o644)


def test_drift_instruction_policy_unbalanced_markers(fake_agent_tools, tmp_path, monkeypatch):
    """Unbalanced managed markers are modified DRIFT (fix by hand), mirroring the apply-side
    hard error — the two surfaces never disagree on what the operator must do."""
    home, _ = _apply_policy(fake_agent_tools, tmp_path, monkeypatch)
    path = _policy_path(home, "pi")
    text = path.read_text(encoding="utf-8")
    end_line = [ln for ln in text.splitlines() if "rig-managed instruction policy" in ln][-1]
    path.write_text(text.replace(end_line + "\n", ""), encoding="utf-8")
    repo = tmp_path / "repo"
    cfg = _instr_cfg(repo, fake_agent_tools, "pi")
    rep = detect(build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    assert any(d.direction == "modified" and "markers" in d.detail for d in rep.items), \
        [d.detail for d in rep.items]
