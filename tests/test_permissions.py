"""Permission-allowlist provisioning — resolve, plan, ADDITIVE merge, idempotency, drift.

The load-bearing invariant under test (the task's hard requirement): the merge into the harness
allowlist is ADDITIVE — it preserves every pre-existing entry (auto-mode, the user's accumulated
list), merges in the desired ecosystem/external tools, dedupes, and a re-apply is a true no-op.
It NEVER clobbers. These tests assert that against the claude-code array form and the opencode
object form, plus the config-driven tool list (tools/extra/disable) and N/A harnesses.
"""

from __future__ import annotations

import json
from pathlib import Path

from riglib.actions import run_plan
from riglib.catalog import Catalog
from riglib.config import LoadedConfig, validate, ConfigError, global_config_path, load
from riglib.drift import detect
from riglib.permissions import (
    DEFAULT_RULES,
    DEFAULT_TOOLS,
    HARNESS_ALLOWLIST_NA,
    HARNESS_RULE_CONTAINERS,
    desired_entries,
    harness_supported,
    resolve_rules,
    resolve_tools,
)
from riglib.plan import build

import pytest


_DESTRUCTIVE_RAW_COMMANDS = ("rm", "sudo", "dd", "kill", "killall", "pkill", "shutdown", "reboot", "mkfs")


# ── the harness module (registry + renderers) ─────────────────────────────────────
def test_default_tools_cover_ecosystem_and_external():
    for t in ("tg", "review", "draw", "3d", "rig", "task", "dev", "pm", "research"):  # our CLIs
        assert t in DEFAULT_TOOLS
    for t in ("rg", "jq", "gitleaks"):  # read-only helper tools
        assert t in DEFAULT_TOOLS
    assert "gh" in DEFAULT_TOOLS  # the one raw git-hosting tool that IS pre-allowed by default
    # Development lifecycle and process/git/external-write access routes through `dev`, not raw tools.
    for t in _DESTRUCTIVE_RAW_COMMANDS + ("lsof", "ps", "pgrep", "docker", "bun", "npm", "uv", "git"):
        assert t not in DEFAULT_TOOLS


def test_gh_pr_merge_stays_denied_on_every_belt_despite_gh_being_default():
    # `gh` joining the default allow set must never widen the merge ban — the gh-pr-merge deny
    # rule stays intact on all four belts (rig-cli task: "keeping the existing gh-pr-merge deny
    # rule intact").
    from riglib.permissions import (
        CLAUDE_CODE_DENY_RULES,
        CODEX_DENY_RULES,
        DEFAULT_GIT_HOSTING_TOOLS,
        OMP_GUARD_DENY_RULES,
        OPENCODE_DENY_RULES,
    )

    assert "Bash(gh pr merge:*)" in CLAUDE_CODE_DENY_RULES
    assert "gh pr merge*" in OPENCODE_DENY_RULES
    assert ("gh", "pr", "merge") in CODEX_DENY_RULES
    assert any(rule.id == "gh-pr-merge" for rule in OMP_GUARD_DENY_RULES)
    # `plan._resolved_permission_tools` folds `allow_gh: false` into `disable` by iterating
    # DEFAULT_GIT_HOSTING_TOOLS, not a hardcoded "gh" literal — pin the coupling loudly: a second
    # member here means `allow_gh` silently covers it too, and the flag likely needs renaming or
    # splitting into a per-tool opt-out.
    assert DEFAULT_GIT_HOSTING_TOOLS == ("gh",)


def test_allow_gh_false_opts_out_via_disable():
    # `allow_gh: false` is documented sugar over `disable: [gh]` — assert the underlying
    # `disable`-based opt-out (which plan._resolved_permission_tools folds allow_gh into) still
    # drops `gh` from the resolved/rendered allowlist while every other default tool stays.
    tools = resolve_tools(None, None, ["gh"])
    assert "gh" not in tools
    for t in ("tg", "rg", "dev"):
        assert t in tools
    entries = desired_entries("claude-code", tools)
    assert "Bash(gh:*)" not in entries


def test_default_claude_code_allowlist_includes_pm_and_research():
    # the read-only ecosystem coordinators (pm-cli, research-cli) are pre-allowed by default,
    # rendered as the claude-code prefix-glob entry — mirroring the `dev` grant.
    entries = desired_entries("claude-code", resolve_tools(None, None, None))
    assert "Bash(pm:*)" in entries
    assert "Bash(research:*)" in entries


def test_resolve_tools_replace_add_remove_and_dedup():
    # explicit `tools` REPLACES the default set
    assert resolve_tools(["git", "gh"], None, None) == ["git", "gh"]
    # extra ADDS, disable REMOVES, both against the DEFAULT set when tools is None
    out = resolve_tools(None, ["kubectl"], ["gitleaks"])
    assert "kubectl" in out and "gitleaks" not in out and "dev" in out and "git" not in out
    # dedup, first-seen order preserved
    assert resolve_tools(["git", "git", "gh"], ["gh"], None) == ["git", "gh"]


def test_claude_code_entry_shape_matches_live_settings():
    # the live ~/.claude/settings.json uses Bash(gh:*) / Bash(git:*) — match it so a re-apply dedups
    assert desired_entries("claude-code", ["gh", "git"]) == ["Bash(gh:*)", "Bash(git:*)"]


def test_opencode_entry_shape_is_glob_key():
    assert desired_entries("opencode", ["git", "gh", "dev"]) == ["git *", "gh *", "dev *"]


def test_na_harnesses_have_no_allowlist():
    assert not harness_supported("codex")
    assert not harness_supported("pi")
    assert not harness_supported("omp")
    assert harness_supported("claude-code")
    assert harness_supported("opencode")
    assert "codex" in HARNESS_ALLOWLIST_NA and "pi" in HARNESS_ALLOWLIST_NA
    assert "omp" in HARNESS_ALLOWLIST_NA
    # gemini is deprecated/removed — it is no longer a recognized allowlist harness at all.
    assert "gemini" not in HARNESS_ALLOWLIST_NA
    assert not harness_supported("gemini")


def test_config_and_permissions_kind_sets_cannot_drift():
    """config.py derives its permissions-kind sets from this module's registries, and every
    known harness must be provisionable SOMEHOW (allowlist / execpolicy / guard / advisory)
    or recorded N/A — a kind added to harness_skills without a classification falls through
    the fan-out silently."""
    from riglib.config import _NA_PERMISSIONS_KINDS, _VALID_PERMISSIONS_KINDS
    from riglib.harness_skills import KNOWN_HARNESS_KINDS
    from riglib.permissions import (
        HARNESS_ALLOWLISTS,
        HARNESS_EXECPOLICY,
        HARNESS_GUARD,
        HARNESS_INSTRUCTION_POLICY,
    )

    # pinned-kind validation: every kind with a permissions surface is accepted; the N/A
    # set is whatever remains (empty today — every known kind has a surface).
    provisionable = (
        set(HARNESS_ALLOWLISTS) | set(HARNESS_EXECPOLICY)
        | set(HARNESS_GUARD) | set(HARNESS_INSTRUCTION_POLICY)
    )
    assert _VALID_PERMISSIONS_KINDS == provisionable
    assert _NA_PERMISSIONS_KINDS == set(HARNESS_ALLOWLIST_NA) - provisionable
    # every known kind has SOME permissions surface (or an explicit allowlist N/A record).
    assert provisionable == set(KNOWN_HARNESS_KINDS)


# ── validation (fail-closed) ───────────────────────────────────────────────────────
def test_validate_rejects_unknown_key_and_bad_types():
    with pytest.raises(ConfigError):
        validate({"version": 1, "permissions": {"toolz": ["git"]}})  # typo
    with pytest.raises(ConfigError):
        validate({"version": 1, "permissions": {"tools": "git"}})  # not a list
    with pytest.raises(ConfigError):
        validate({"version": 1, "permissions": {"enabled": "yes"}})  # not a bool
    with pytest.raises(ConfigError):
        validate({"version": 1, "permissions": {"allow_gh": "no"}})  # not a bool
    # the good shape passes
    validate({"version": 1, "permissions": {"enabled": True, "tools": ["git"], "extra": ["gh"], "disable": []}})
    validate({"version": 1, "permissions": {"allow_gh": False}})


# ── plan + apply (claude-code array form) ───────────────────────────────────────────
def _cfg(repo: Path, source: Path, settings: Path, **perm) -> LoadedConfig:
    return LoadedConfig(
        data={
            "agent_tools_source": str(source),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            # harness present but no auto-mode write to the same file (use a different settings_path
            # for the harness so the two actions don't both target `settings`); here we just disable
            # the harness block entirely to isolate the permissions action.
            "permissions": {"settings_path": str(settings), **perm},
        },
        repo_root=repo,
    )


def _perm_results(report):
    return [r for r in report.results if r.action.category == "permissions"]


def test_plan_default_on_when_block_absent(fake_agent_tools, tmp_path):
    # an ABSENT permissions block still provisions (default-on) the default tool set
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "harness": {"kind": "claude-code", "settings_path": str(settings)},
        },
        repo_root=repo,
    )
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    perm_actions = [a for a in plan.actions if a.kind == "provision_permissions"]
    assert len(perm_actions) == 1
    assert perm_actions[0].options["kind"] == "claude-code"
    assert list(perm_actions[0].options["tools"]) == list(DEFAULT_TOOLS)


def test_plan_disabled_emits_no_action(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    plan = build(_cfg(repo, fake_agent_tools, settings, enabled=False),
                 Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert not [a for a in plan.actions if a.kind == "provision_permissions"]


def test_plan_allow_gh_false_drops_gh_from_default_set(fake_agent_tools, tmp_path):
    # `allow_gh: false` opts out of the `gh` default without touching the rest of the default set.
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    plan = build(_cfg(repo, fake_agent_tools, settings, allow_gh=False),
                 Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    perm_actions = [a for a in plan.actions if a.kind == "provision_permissions"]
    assert len(perm_actions) == 1
    tools = perm_actions[0].options["tools"]
    assert "gh" not in tools
    assert [t for t in DEFAULT_TOOLS if t != "gh"] == list(tools)


def test_plan_allow_gh_absent_or_true_keeps_gh(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    for perm in ({}, {"allow_gh": True}):
        plan = build(_cfg(repo, fake_agent_tools, settings, **perm),
                     Catalog.scan(str(fake_agent_tools)), project_type="unknown")
        perm_actions = [a for a in plan.actions if a.kind == "provision_permissions"]
        assert "gh" in perm_actions[0].options["tools"]


def test_plan_allow_gh_false_wins_over_extra_gh(fake_agent_tools, tmp_path):
    # `extra: [gh]` re-adding `gh` must not undo `allow_gh: false` — disable applies AFTER extra
    # in resolve_tools, and _resolved_permission_tools folds allow_gh into disable, so `gh` is
    # dropped regardless of how it entered the desired set.
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    plan = build(_cfg(repo, fake_agent_tools, settings, extra=["gh"], allow_gh=False),
                 Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    tools = [a for a in plan.actions if a.kind == "provision_permissions"][0].options["tools"]
    assert "gh" not in tools


def test_plan_allow_gh_false_wins_over_explicit_tools_gh(fake_agent_tools, tmp_path):
    # same interplay against an explicit `tools` list (which REPLACES the default set) rather
    # than `extra`.
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    plan = build(_cfg(repo, fake_agent_tools, settings, tools=["gh", "tg"], allow_gh=False),
                 Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    tools = [a for a in plan.actions if a.kind == "provision_permissions"][0].options["tools"]
    assert tools == ["tg"]


def test_apply_creates_allowlist_and_is_idempotent(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg(repo, fake_agent_tools, settings, tools=["git", "gh", "tg"]), cat, project_type="unknown")

    first = run_plan(plan)
    assert not first.errors, [r.detail for r in first.errors]
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["permissions"]["allow"] == ["Bash(git:*)", "Bash(gh:*)", "Bash(tg:*)"]
    assert any(r.status == "created" for r in _perm_results(first))

    second = run_plan(plan)  # idempotent — all entries already present → skipped
    assert all(r.status == "skipped" for r in _perm_results(second))
    assert json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"] == \
        ["Bash(git:*)", "Bash(gh:*)", "Bash(tg:*)"]


def test_apply_is_additive_and_never_clobbers_existing(fake_agent_tools, tmp_path):
    """THE invariant: existing entries (auto-mode, accumulated list) survive; ours merge in; dedup."""
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    # a pre-existing settings.json with an accumulated allow list + other keys + defaultMode
    settings.write_text(json.dumps({
        "model": "opus",
        "permissions": {
            "defaultMode": "auto",
            "allow": ["WebFetch", "Bash(docker ps:*)", "Bash(git:*)"],  # git already there → must dedup
            "deny": ["Bash(rm:*)"],
        },
    }), encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    # deny=[]/ask=[] isolates this test on the ALLOW merge (the deny/ask baseline has its own
    # dedicated tests below — with the default baseline on, `deny` would gain the baked rules)
    plan = build(_cfg(repo, fake_agent_tools, settings, tools=["git", "gh"], deny=[], ask=[]),
                 cat, project_type="unknown")
    report = run_plan(plan)
    assert not report.errors
    data = json.loads(settings.read_text(encoding="utf-8"))
    allow = data["permissions"]["allow"]
    # every pre-existing entry preserved, in order
    assert allow[:3] == ["WebFetch", "Bash(docker ps:*)", "Bash(git:*)"]
    # the missing one (gh) is appended; the already-present one (git) is NOT duplicated
    assert "Bash(gh:*)" in allow
    assert allow.count("Bash(git:*)") == 1
    # unrelated keys + defaultMode + deny all intact
    assert data["model"] == "opus"
    assert data["permissions"]["defaultMode"] == "auto"
    assert data["permissions"]["deny"] == ["Bash(rm:*)"]


def test_narrowed_default_allowlist_does_not_prune_existing_broad_entries(fake_agent_tools, tmp_path):
    """Changing rig's default desired tools must not delete grants already present on a machine."""
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    legacy = ["Bash(git:*)", "Bash(gh:*)", "Bash(uv:*)", "Bash(bun:*)"]
    settings.write_text(json.dumps({"permissions": {"allow": legacy}}), encoding="utf-8")

    plan = build(
        _cfg(repo, fake_agent_tools, settings, deny=[], ask=[]),
        Catalog.scan(str(fake_agent_tools)),
        project_type="unknown",
    )
    report = run_plan(plan)

    assert not report.errors
    allow = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert allow[:len(legacy)] == legacy
    for entry in legacy:
        assert allow.count(entry) == 1
    assert "Bash(dev:*)" in allow


def test_apply_backs_up_before_change_under_backup_policy(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    settings.write_text(json.dumps({"permissions": {"allow": ["WebFetch"]}}), encoding="utf-8")
    cfg = _cfg(repo, fake_agent_tools, settings, tools=["git"])
    cfg.data["defaults"] = {"on_conflict": "backup"}
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(cfg, cat, project_type="unknown")
    run_plan(plan)
    assert any(p.name.startswith("settings.json.rig-bak-") for p in repo.iterdir())


# ── opencode object form ────────────────────────────────────────────────────────────
def test_opencode_object_form_merge(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "opencode.json"
    # existing permission.bash object with a user deny — must NOT be downgraded
    settings.write_text(json.dumps({
        "permission": {"bash": {"rm *": "deny", "git *": "allow"}},
    }), encoding="utf-8")
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "harness": {"kind": "opencode"},  # reserved for harness write, but permissions supports it
            "permissions": {"settings_path": str(settings), "tools": ["git", "gh"]},
        },
        repo_root=repo,
    )
    # the harness block validate() rejects opencode kind — but permissions resolves the kind from
    # harness.kind. Build the config WITHOUT going through validate (LoadedConfig is pre-validated
    # in tests), so we can exercise the opencode permissions path directly.
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    perm = [a for a in plan.actions if a.kind == "provision_permissions"]
    assert perm and perm[0].options["kind"] == "opencode"
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    data = json.loads(settings.read_text(encoding="utf-8"))
    bash = data["permission"]["bash"]
    assert bash["rm *"] == "deny"          # user override preserved, never clobbered
    assert bash["git *"] == "allow"        # already present, untouched
    assert bash["gh *"] == "allow"         # added


# ── drift ─────────────────────────────────────────────────────────────────────────
def test_drift_missing_then_in_sync(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg(repo, fake_agent_tools, settings, tools=["git", "gh"]), cat, project_type="unknown")

    # nothing on disk → missing for each desired entry
    rep = detect(plan)
    miss = [d for d in rep.by_direction("missing") if d.category == "permissions"]
    assert miss, "expected missing permissions drift"

    run_plan(plan)
    rep2 = detect(plan)
    assert not [d for d in rep2.items if d.category == "permissions"]


def test_drift_modified_when_user_partially_present(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    # git present, gh absent → gh is missing drift, git is in sync
    settings.write_text(json.dumps({"permissions": {"allow": ["Bash(git:*)"]}}), encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    # deny=[]/ask=[]: keep this drift test focused on the allowlist (deny/ask drift is below)
    plan = build(_cfg(repo, fake_agent_tools, settings, tools=["git", "gh"], deny=[], ask=[]),
                 cat, project_type="unknown")
    rep = detect(plan)
    missing = [d for d in rep.by_direction("missing") if d.category == "permissions"]
    assert len(missing) == 1 and "Bash(gh:*)" in missing[0].detail


# ── opencode-specific (kind override + object-form drift) ───────────────────────────
def _opencode_cfg(repo: Path, source: Path, settings: Path, **perm) -> LoadedConfig:
    # target opencode INDEPENDENTLY via permissions.kind (harness.kind: opencode is rejected by
    # validate; permissions.kind: opencode is the supported way to provision the opencode allowlist).
    return LoadedConfig(
        data={
            "agent_tools_source": str(source),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "permissions": {"kind": "opencode", "settings_path": str(settings), **perm},
        },
        repo_root=repo,
    )


def test_opencode_default_on_apply_writes_default_set(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "opencode.json"
    plan = build(_opencode_cfg(repo, fake_agent_tools, settings),
                 Catalog.scan(str(fake_agent_tools)), project_type="unknown")

    report = run_plan(plan)

    assert not report.errors, [r.detail for r in report.errors]
    bash = json.loads(settings.read_text(encoding="utf-8"))["permission"]["bash"]
    # allow keys for every tool …
    for tool in DEFAULT_TOOLS:
        assert bash[f"{tool} *"] == "allow"
    # … plus the baked opencode-dialect deny/ask baseline folded into the SAME permission.bash
    # object (values "deny"/"ask"), emitted AFTER the allow keys (last-match-wins ordering).
    from riglib.permissions import OPENCODE_ASK_RULES, OPENCODE_DENY_RULES

    for key in OPENCODE_DENY_RULES:
        assert bash[key] == "deny"
    for key in OPENCODE_ASK_RULES:
        assert bash[key] == "ask"
    keys = list(bash)
    assert keys.index(f"{DEFAULT_TOOLS[0]} *") < keys.index(OPENCODE_DENY_RULES[0])  # allow before deny
    assert not [d for d in detect(plan).items if d.category == "permissions"]


def test_permissions_kind_override_targets_opencode(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "opencode.json"
    plan = build(_opencode_cfg(repo, fake_agent_tools, settings, tools=["git"]),
                 Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    perm = [a for a in plan.actions if a.kind == "provision_permissions"]
    assert perm and perm[0].options["kind"] == "opencode"


def test_validate_permissions_kind_na_and_unknown_rejected():
    with pytest.raises(ConfigError, match="no longer supported"):
        validate({"version": 1, "permissions": {"kind": "gemini"}})  # deprecated/removed harness
    with pytest.raises(ConfigError):
        validate({"version": 1, "permissions": {"kind": "bogus"}})   # unknown
    validate({"version": 1, "permissions": {"kind": "opencode"}})    # allowlist kind
    validate({"version": 1, "permissions": {"kind": "claude-code"}})
    validate({"version": 1, "permissions": {"kind": "omp"}})         # guard + approval policy (rig-cli#202)
    validate({"version": 1, "permissions": {"kind": "codex"}})       # execpolicy surface
    validate({"version": 1, "permissions": {"kind": "pi"}})          # advisory instruction policy
    validate({"version": 1, "permissions": {"kind": "commandcode"}}) # advisory instruction policy
    validate({"version": 1, "permissions": {"kind": None}})          # explicit unpinned/fan-out


def test_permissions_fan_out_provisions_omp_guard_and_approval(fake_agent_tools, tmp_path, monkeypatch):
    """harness.kind: omp with no permissions.kind fans out → the guard extension + approval
    policy actions (tier 1 enforcement), NOT a bare N/A skip (rig-cli#202)."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    repo = tmp_path / "repo"; repo.mkdir()
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "harness": {"kind": "omp"},
            "permissions": {"tools": ["git"]},
        },
        repo_root=repo,
    )

    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")

    assert not [a for a in plan.actions if a.kind == "provision_permissions"]
    guards = [a for a in plan.actions if a.kind == "install_harness_guard"]
    assert [a.options["kind"] for a in guards] == ["omp"]
    approvals = [a for a in plan.actions if a.kind == "provision_harness_approval"]
    assert [a.options["kind"] for a in approvals] == ["omp"]
    assert any("omp" in note and "tier 1" in note for note in plan.notes), plan.notes


def test_permissions_fan_out_mixed_supported_and_guard_kinds(fake_agent_tools, tmp_path, monkeypatch):
    """kind: claude-code + kinds: [omp] in ONE build: claude-code gets its allowlist action
    AND omp gets its guard + approval actions — neither eats the other."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    repo = tmp_path / "repo"; repo.mkdir()
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "harness": {"kind": "claude-code", "kinds": ["omp"]},
            "permissions": {"tools": ["git"]},
        },
        repo_root=repo,
    )

    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")

    perm = [a for a in plan.actions if a.kind == "provision_permissions"]
    assert [a.options["kind"] for a in perm] == ["claude-code"]
    assert [a.options["kind"] for a in plan.actions if a.kind == "install_harness_guard"] == ["omp"]
    assert [a.options["kind"] for a in plan.actions if a.kind == "provision_harness_approval"] == ["omp"]


def test_opencode_drift_object_form(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "opencode.json"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_opencode_cfg(repo, fake_agent_tools, settings, tools=["git", "gh"]), cat, project_type="unknown")
    # nothing on disk → missing
    assert [d for d in detect(plan).by_direction("missing") if d.category == "permissions"]
    # a user deny on one entry → modified (apply leaves it, status surfaces it)
    settings.write_text(json.dumps({"permission": {"bash": {"git *": "deny", "gh *": "allow"}}}), encoding="utf-8")
    rep = detect(plan)
    mod = [d for d in rep.by_direction("modified") if d.category == "permissions"]
    assert mod and "git *" in mod[0].detail
    # converge gh stays allow, git deny preserved → no MISSING for gh
    assert not [d for d in rep.by_direction("missing") if "gh *" in d.detail]


# ── malformed JSON + error paths ────────────────────────────────────────────────────
def test_malformed_json_skip_leaves_untouched(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    settings.write_text("{ this is not json", encoding="utf-8")
    cfg = _cfg(repo, fake_agent_tools, settings, tools=["git"])
    cfg.data["defaults"] = {"on_conflict": "skip"}
    report = run_plan(build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    perm = _perm_results(report)
    assert perm and perm[0].status == "skipped"
    assert settings.read_text(encoding="utf-8") == "{ this is not json"  # untouched


def test_non_array_allow_is_error(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    settings.write_text(json.dumps({"permissions": {"allow": "not-a-list"}}), encoding="utf-8")
    report = run_plan(build(_cfg(repo, fake_agent_tools, settings, tools=["git"]),
                            Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    perm = _perm_results(report)
    assert perm and perm[0].status == "error" and "not an array" in perm[0].detail


def test_non_dict_root_is_error(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    settings.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    report = run_plan(build(_cfg(repo, fake_agent_tools, settings, tools=["git"]),
                            Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    perm = _perm_results(report)
    assert perm and perm[0].status == "error" and "not a JSON object" in perm[0].detail


# ── status: updated (not created) when modifying an existing file (non-backup policy) ─
def test_status_updated_not_created_for_existing_file(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    settings.write_text(json.dumps({"permissions": {"allow": ["WebFetch"]}}), encoding="utf-8")
    cfg = _cfg(repo, fake_agent_tools, settings, tools=["git"])
    cfg.data["defaults"] = {"on_conflict": "overwrite"}  # non-backup → no backup, but file existed
    report = run_plan(build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    perm = _perm_results(report)
    assert perm and perm[0].status == "updated"  # not "created" — the file already existed


# ── disable + default-on, end to end through apply ───────────────────────────────────
def test_disable_drops_tool_from_desired_set_end_to_end(fake_agent_tools, tmp_path):
    # `disable` removes a tool from rig's DESIRED set so it is never ADDED to the allowlist.
    # Disable the ENTIRE default set (not a hand-picked subset) so this assertion can't silently
    # drift when a future PR adds another default tool — intent is explicit (nothing survives),
    # not positional.
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    plan = build(_cfg(repo, fake_agent_tools, settings, disable=list(DEFAULT_TOOLS)),
                 Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    run_plan(plan)
    allow = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert "Bash(gitleaks:*)" not in allow  # disabled → never added
    assert "Bash(dev:*)" not in allow
    assert allow == []  # every default tool was disabled — nothing survives


def test_disable_leaves_other_defaults_intact(fake_agent_tools, tmp_path):
    # A focused counterpart to the exhaustive-disable test above: disabling one tool must not
    # touch any other default tool's presence in the allowlist.
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    plan = build(_cfg(repo, fake_agent_tools, settings, disable=["gitleaks"]),
                 Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    run_plan(plan)
    allow = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert "Bash(gitleaks:*)" not in allow
    assert "Bash(pm:*)" in allow


def test_disable_dev_drops_dev_from_desired_set(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    plan = build(_cfg(repo, fake_agent_tools, settings, disable=["dev"]),
                 Catalog.scan(str(fake_agent_tools)), project_type="unknown")

    run_plan(plan)

    allow = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert allow == [f"Bash({t}:*)" for t in DEFAULT_TOOLS if t != "dev"]
    assert "Bash(dev:*)" not in allow


# ── receipt-tracked revocation of a rig-granted default (rig-cli#333 review finding) ────
# `allow_gh: false` (sugar for `disable: [gh]`) is only a REAL opt-out when rig can actually take
# back a grant it made itself. The additive merge never deletes anything on its own — these tests
# pin the sidecar-receipt mechanism that lets `allow_gh: false` revoke a `gh` entry rig itself
# granted as a default, while a `gh` entry rig never granted (hand-added, or predates this
# feature) is correctly left alone — "safely revoked without deleting unrelated user entries".
def test_allow_gh_false_revokes_a_rig_granted_default_entry(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))

    # first apply: gh joins the default set — rig grants AND receipts it as its own
    plan1 = build(_cfg(repo, fake_agent_tools, settings), cat, project_type="unknown")
    run_plan(plan1)
    allow = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert "Bash(gh:*)" in allow

    # opt out — status must show the stale grant as ACTIONABLE drift, not a summarized "kept" line
    plan2 = build(_cfg(repo, fake_agent_tools, settings, allow_gh=False), cat, project_type="unknown")
    extra = [d for d in detect(plan2).by_direction("extra") if d.category == "permissions"]
    assert any("Bash(gh:*)" in d.detail for d in extra)

    report = run_plan(plan2)
    perm = _perm_results(report)
    assert perm and perm[0].status in ("updated", "backed_up")
    allow2 = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert "Bash(gh:*)" not in allow2  # actually revoked — not merely reported as "kept"
    assert not [d for d in detect(plan2).items if d.category == "permissions"]  # clean now

    # idempotent: a further apply with the same opted-out config is a true no-op
    report2 = run_plan(plan2)
    assert _perm_results(report2)[0].status == "skipped"


def test_allow_gh_false_leaves_a_hand_added_gh_entry_alone(fake_agent_tools, tmp_path):
    # `gh` was NEVER granted by rig here (no prior apply at all) — a pre-existing hand-added entry
    # (or one that predates this receipt feature) must never be silently deleted.
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    settings.write_text(json.dumps({"permissions": {"allow": ["Bash(gh:*)"]}}), encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg(repo, fake_agent_tools, settings, allow_gh=False), cat, project_type="unknown")

    report = run_plan(plan)
    allow = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert "Bash(gh:*)" in allow  # never receipted by rig → never revoked
    assert not [d for d in detect(plan).by_direction("extra") if d.category == "permissions"]
    kept = [k for k in detect(plan).known
            if k.category == "permissions" and k.name == "Bash(gh:*)"]
    assert kept  # visible as "your own entries, kept" — not silent


def test_allow_gh_false_revokes_only_one_copy_of_a_hand_duplicated_entry(fake_agent_tools, tmp_path):
    # round-5 review finding: the receipt proves rig granted `Bash(gh:*)` ONCE, never that every
    # occurrence of an identical string in the array is rig's. A JSON array tolerates duplicates
    # even though rig's own additive merge never creates one — a user who hand-duplicated the
    # entry must keep their extra copy; only the one rig can prove it wrote is taken back.
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))

    plan1 = build(_cfg(repo, fake_agent_tools, settings), cat, project_type="unknown")
    run_plan(plan1)  # gh default-granted + receipted (exactly one copy on disk)
    data = json.loads(settings.read_text(encoding="utf-8"))
    data["permissions"]["allow"].append("Bash(gh:*)")  # the user hand-duplicates it
    settings.write_text(json.dumps(data), encoding="utf-8")

    plan2 = build(_cfg(repo, fake_agent_tools, settings, allow_gh=False), cat, project_type="unknown")
    run_plan(plan2)
    allow_after = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert allow_after.count("Bash(gh:*)") == 1, "one copy revoked, the hand-added duplicate kept"


def test_opencode_allow_gh_false_revokes_a_rig_granted_default_entry(fake_agent_tools, tmp_path):
    # the opencode mirror: same receipt mechanism, object-form container (`gh *` key).
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "opencode.json"
    cat = Catalog.scan(str(fake_agent_tools))

    plan1 = build(_opencode_cfg(repo, fake_agent_tools, settings), cat, project_type="unknown")
    run_plan(plan1)
    bash = json.loads(settings.read_text(encoding="utf-8"))["permission"]["bash"]
    assert bash.get("gh *") == "allow"

    plan2 = build(_opencode_cfg(repo, fake_agent_tools, settings, allow_gh=False), cat, project_type="unknown")
    extra = [d for d in detect(plan2).by_direction("extra") if d.category == "permissions"]
    assert any("gh *" in d.detail for d in extra)

    run_plan(plan2)
    bash2 = json.loads(settings.read_text(encoding="utf-8"))["permission"]["bash"]
    assert "gh *" not in bash2
    assert not [d for d in detect(plan2).items if d.category == "permissions"]


def test_receipt_never_revokes_a_non_git_hosting_tool_across_shared_settings(fake_agent_tools, tmp_path):
    # round-2 review finding: the settings file is MACHINE-WIDE (one ~/.claude/settings.json
    # shared by every repo rig manages), so a receipt scoped to "every tool-derived allow entry"
    # let repo A's `permissions.tools: [kubectl]` grant get revoked the moment repo B applied its
    # OWN (kubectl-less) config against the same shared file. Receipt eligibility is now hard-
    # scoped to the git-hosting default tools only (`gh`) — prove a non-gh tool survives even
    # when it looks, from a later apply's point of view, exactly like a stale grant.
    repo_a = tmp_path / "repo-a"; repo_a.mkdir()
    repo_b = tmp_path / "repo-b"; repo_b.mkdir()
    shared_settings = tmp_path / "shared-settings.json"  # simulates the one machine-wide file
    cat = Catalog.scan(str(fake_agent_tools))

    # repo A: an explicit, non-default tool list that happens to include kubectl
    plan_a = build(_cfg(repo_a, fake_agent_tools, shared_settings, tools=["git", "kubectl"]),
                    cat, project_type="unknown")
    run_plan(plan_a)
    allow_after_a = json.loads(shared_settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert "Bash(kubectl:*)" in allow_after_a

    # repo B: its OWN config against the SAME file, with a tool list that does not mention kubectl
    plan_b = build(_cfg(repo_b, fake_agent_tools, shared_settings, tools=["git"]), cat, project_type="unknown")
    assert not [d for d in detect(plan_b).by_direction("extra") if d.category == "permissions"], (
        "kubectl must never be reported as revocable drift from a repo that never granted it"
    )
    run_plan(plan_b)
    allow_after_b = json.loads(shared_settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert "Bash(kubectl:*)" in allow_after_b, "repo A's still-declared grant must survive repo B's apply"


def test_receipt_never_revokes_gh_from_an_unrelated_repos_narrower_tool_list(fake_agent_tools, tmp_path):
    # round-2 review finding (the sharper gh-specific case, not just kubectl): repo B's apply must
    # not revoke `gh` just because ITS OWN `permissions.tools` override happens to omit it — that
    # is not the same thing as repo B explicitly typing `allow_gh: false` or `disable: [gh]`. Only
    # an EXPLICIT by-name opt-out is ever a revocation trigger.
    repo_a = tmp_path / "repo-a"; repo_a.mkdir()
    repo_b = tmp_path / "repo-b"; repo_b.mkdir()
    shared_settings = tmp_path / "shared-settings.json"
    cat = Catalog.scan(str(fake_agent_tools))

    plan_a = build(_cfg(repo_a, fake_agent_tools, shared_settings), cat, project_type="unknown")  # gh default-granted + receipted
    run_plan(plan_a)
    assert "Bash(gh:*)" in json.loads(shared_settings.read_text(encoding="utf-8"))["permissions"]["allow"]

    # repo B: an explicit tool list that simply never mentions gh — NOT allow_gh:false, NOT disable
    plan_b = build(_cfg(repo_b, fake_agent_tools, shared_settings, tools=["tg"]), cat, project_type="unknown")
    assert not [d for d in detect(plan_b).by_direction("extra") if d.category == "permissions"], (
        "gh must not be reported as revocable drift from a repo that never opted out of it"
    )
    run_plan(plan_b)
    allow_after_b = json.loads(shared_settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert "Bash(gh:*)" in allow_after_b, "repo A's gh grant must survive repo B's unrelated apply"


def test_explicit_allow_wins_over_allow_gh_false_no_contradictory_revoked_report(fake_agent_tools, tmp_path):
    # round-2 review finding: a self-contradicting config — `allow_gh: false` alongside an explicit
    # `permissions.allow: ['Bash(gh:*)']` re-asserting the exact same entry — must not revoke it
    # and then silently re-add it in the SAME apply while reporting "revoked" over an entry that,
    # in fact, is still there. The raw, explicit assertion wins.
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))

    plan1 = build(_cfg(repo, fake_agent_tools, settings), cat, project_type="unknown")
    run_plan(plan1)  # gh default-granted + receipted

    plan2 = build(_cfg(repo, fake_agent_tools, settings, allow_gh=False, allow=["Bash(gh:*)"]),
                   cat, project_type="unknown")
    # round-9 review finding: the plan preview must not overpromise a delete the executor is,
    # correctly, about to skip — parity between what `rig apply info` says and what
    # `rig apply commit` actually does.
    assert not any("will be REVOKED" in note for note in plan2.notes), plan2.notes
    assert any("NOT applied" in note for note in plan2.notes), plan2.notes
    report = run_plan(plan2)
    perm = _perm_results(report)
    assert perm and "revoked" not in perm[0].detail, perm[0].detail
    allow = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert "Bash(gh:*)" in allow  # the explicit raw assertion survives
    assert not [d for d in detect(plan2).items if d.category == "permissions"]


def test_raw_allow_entry_is_never_receipted_even_when_it_renders_like_the_default(fake_agent_tools, tmp_path):
    # round-6 review finding: a raw `permissions.allow: ['Bash(gh:*)']` entry renders IDENTICALLY
    # to the git-hosting default's own string. On a FIRST apply (before rig's own default merge
    # would have added it), that made it look "newly added by the default merge" and receipted it
    # as though rig granted it as a default — contradicting the documented "raw allow entries are
    # never receipted" contract. Prove it survives an unrelated LATER opt-out untouched.
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))

    # first-ever apply: gh is default-granted AND separately, explicitly re-asserted via raw allow
    plan1 = build(_cfg(repo, fake_agent_tools, settings, allow=["Bash(gh:*)"]), cat, project_type="unknown")
    run_plan(plan1)
    assert "Bash(gh:*)" in json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]

    # the user later drops the raw declaration and opts out via allow_gh — the entry must survive:
    # it was never receipted as rig's own default grant, only ever the user's raw assertion
    plan2 = build(_cfg(repo, fake_agent_tools, settings, allow_gh=False), cat, project_type="unknown")
    assert not [d for d in detect(plan2).by_direction("extra") if d.category == "permissions"]
    run_plan(plan2)
    allow_after = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert "Bash(gh:*)" in allow_after


def test_extra_gh_is_never_receipted_even_on_a_tools_override_that_excludes_it(fake_agent_tools, tmp_path):
    # round-7 review finding: `permissions.extra: [gh]` on top of an explicit `tools:` override
    # that would NOT otherwise include `gh` is exactly as deliberate a user assertion as raw
    # `permissions.allow` — the SAME "looks newly added by the default merge" trap that made
    # `extra`-derived `gh` get wrongly receipted as rig's own default grant, revocable by an
    # unrelated LATER `allow_gh: false`.
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))

    # first-ever apply: an explicit, narrow tools list + extra: [gh] re-adds gh on top of it
    plan1 = build(_cfg(repo, fake_agent_tools, settings, tools=["git"], extra=["gh"]),
                  cat, project_type="unknown")
    run_plan(plan1)
    assert "Bash(gh:*)" in json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]

    # the user later drops `extra` and opts out via allow_gh — the entry must survive: it was
    # never receipted as rig's own default grant, only ever the user's explicit `extra` assertion
    plan2 = build(_cfg(repo, fake_agent_tools, settings, tools=["git"], allow_gh=False),
                  cat, project_type="unknown")
    assert not [d for d in detect(plan2).by_direction("extra") if d.category == "permissions"]
    run_plan(plan2)
    allow_after = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert "Bash(gh:*)" in allow_after


def test_explicit_redeclaration_clears_a_receipt_from_an_earlier_default_grant(fake_agent_tools, tmp_path):
    # round-9 review finding: `gh` is first granted as a plain default (receipted), then a LATER
    # apply explicitly re-declares it via `tools: [gh]` (never-receipted per round 8) — but that
    # earlier receipt entry survived untouched because the round-8 fix only kept NEW additions out
    # of the receipt, never cleared an EXISTING one on a later explicit re-declaration. A THIRD,
    # unrelated apply's `allow_gh: false` must not revoke an entry whose ownership already
    # transferred to the user's own explicit `tools` declaration.
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))

    plan1 = build(_cfg(repo, fake_agent_tools, settings), cat, project_type="unknown")
    run_plan(plan1)  # gh granted as a plain default, receipted

    plan2 = build(_cfg(repo, fake_agent_tools, settings, tools=["gh"]), cat, project_type="unknown")
    run_plan(plan2)  # explicit re-declaration — ownership transfers to the user, receipt must clear
    assert "Bash(gh:*)" in json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]

    plan3 = build(_cfg(repo, fake_agent_tools, settings, tools=["tg"], allow_gh=False),
                  cat, project_type="unknown")
    assert not [d for d in detect(plan3).by_direction("extra") if d.category == "permissions"]
    run_plan(plan3)
    allow_after = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert "Bash(gh:*)" in allow_after


def test_explicit_tools_gh_is_never_receipted_even_though_it_replaces_the_default_set(fake_agent_tools, tmp_path):
    # round-8 review finding: an explicit `permissions.tools: [gh]` REPLACES the default set
    # wholesale (resolve_tools' documented contract) — naming `gh` there is exactly as deliberate
    # a user assertion as `extra`/raw `allow`, the SAME "looks newly added by the default merge"
    # trap yet again, via a third config knob.
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))

    # first-ever apply: an explicit tools list that itself names gh (not the default set)
    plan1 = build(_cfg(repo, fake_agent_tools, settings, tools=["gh"]), cat, project_type="unknown")
    run_plan(plan1)
    assert "Bash(gh:*)" in json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]

    # the user later opts out via allow_gh — the entry must survive: it was never receipted as
    # rig's own default grant, only ever the user's explicit `tools` assertion
    plan2 = build(_cfg(repo, fake_agent_tools, settings, tools=["gh"], allow_gh=False),
                  cat, project_type="unknown")
    assert not [d for d in detect(plan2).by_direction("extra") if d.category == "permissions"]
    run_plan(plan2)
    allow_after = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert "Bash(gh:*)" in allow_after


def test_stale_receipted_grant_already_removed_by_hand_is_not_reported_as_drift(fake_agent_tools, tmp_path):
    # round-2 review finding: if the USER already deleted the receipted `Bash(gh:*)` entry by hand
    # before setting `allow_gh: false`, there is nothing left for apply to revoke — reporting it as
    # `extra`/"apply revokes it" would be a false non-clean status with a false remediation claim.
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))

    plan1 = build(_cfg(repo, fake_agent_tools, settings), cat, project_type="unknown")
    run_plan(plan1)
    data = json.loads(settings.read_text(encoding="utf-8"))
    data["permissions"]["allow"].remove("Bash(gh:*)")  # the user removes it by hand
    settings.write_text(json.dumps(data), encoding="utf-8")

    plan2 = build(_cfg(repo, fake_agent_tools, settings, allow_gh=False), cat, project_type="unknown")
    assert not [d for d in detect(plan2).by_direction("extra") if d.category == "permissions"], (
        "an already-hand-removed entry must not be reported as actionable revocation drift"
    )
    report = run_plan(plan2)
    # nothing to revoke → the settings file itself is untouched (apply is a true no-op on it)
    allow_after = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert "Bash(gh:*)" not in allow_after
    assert not report.errors, [r.detail for r in report.errors]


def test_receipt_not_written_when_the_settings_write_fails(fake_agent_tools, tmp_path):
    # round-2 review finding: syncing the receipt BEFORE the settings write let a failed write
    # (a read-only/unwritable target) leave a receipt claiming rig granted an entry that was never
    # actually written to disk — a later hand-added entry with that same name would then be wrongly
    # treated as rig's own and revoked on some future opt-out. Force a write failure (a directory
    # in place of the settings file) and assert the receipt stays untouched.
    from riglib.actions.runner import permissions_receipt_file

    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    settings.mkdir()  # config_file.write_text() will raise IsADirectoryError
    plan = build(_cfg(repo, fake_agent_tools, settings), Catalog.scan(str(fake_agent_tools)),
                 project_type="unknown")

    report = run_plan(plan)
    perm = _perm_results(report)
    assert perm and perm[0].status == "error"
    receipt_file = permissions_receipt_file(perm[0].action)
    assert not receipt_file.exists(), "the receipt must not record a grant the write never landed"


def test_default_on_apply_writes_default_set_and_is_in_sync(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    # absent permissions block, but pin settings_path so we don't touch ~/.claude
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "permissions": {"settings_path": str(settings)},  # default-on, default tool set
        },
        repo_root=repo,
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(cfg, cat, project_type="unknown")
    run_plan(plan)
    allow = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert allow == [f"Bash({t}:*)" for t in DEFAULT_TOOLS]
    for raw_command in _DESTRUCTIVE_RAW_COMMANDS:
        assert f"Bash({raw_command}:*)" not in allow
    assert not [d for d in detect(plan).items if d.category == "permissions"]  # in sync after apply


def test_empty_tools_list_is_noop(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    # deny=[]/ask=[] too — with every list empty the action has literally nothing to add
    report = run_plan(build(_cfg(repo, fake_agent_tools, settings, tools=[], deny=[], ask=[]),
                            Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    perm = _perm_results(report)
    assert perm and perm[0].status == "skipped"  # zero entries → nothing added


def test_empty_action_against_malformed_file_is_a_true_noop(fake_agent_tools, tmp_path):
    # rig-cli#333 review finding: with tools=[]/deny=[]/ask=[] (nothing desired at all) against a
    # MALFORMED settings file under the default `backup` policy, the action has nothing to write —
    # so it must never touch the file at all: no backup, no rewrite, original bytes untouched. The
    # old code parsed first (backing up the malformed file + resetting to {}) and only THEN noticed
    # the action was empty, leaving a stray backup behind on every single re-apply forever.
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    settings.write_text("{ broken", encoding="utf-8")
    cfg = _cfg(repo, fake_agent_tools, settings, tools=[], deny=[], ask=[])
    cfg.data["defaults"] = {"on_conflict": "backup"}
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")

    report = run_plan(plan)
    perm = _perm_results(report)
    assert perm and perm[0].status == "skipped"
    assert settings.read_text(encoding="utf-8") == "{ broken"  # untouched, not reset to {}
    assert not [p for p in repo.iterdir() if p.name.startswith("settings.json.rig-bak-")]

    # re-apply: still a true no-op, still zero backups (idempotent — not just "first apply is fine")
    report2 = run_plan(plan)
    assert _perm_results(report2)[0].status == "skipped"
    assert settings.read_text(encoding="utf-8") == "{ broken"
    assert not [p for p in repo.iterdir() if p.name.startswith("settings.json.rig-bak-")]


def test_malformed_json_backup_then_rewrites_under_backup(fake_agent_tools, tmp_path):
    # on_conflict != skip → back up the malformed file, reset to {}, write the allowlist
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    settings.write_text("{ broken", encoding="utf-8")
    cfg = _cfg(repo, fake_agent_tools, settings, tools=["git"])
    cfg.data["defaults"] = {"on_conflict": "backup"}
    report = run_plan(build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    perm = _perm_results(report)
    assert perm and perm[0].status == "backed_up" and "malformed" in perm[0].detail
    assert any(p.name.startswith("settings.json.rig-bak-") for p in repo.iterdir())
    assert json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"] == ["Bash(git:*)"]


def test_status_backed_up_when_modifying_existing_under_backup(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    settings.write_text(json.dumps({"permissions": {"allow": ["WebFetch"]}}), encoding="utf-8")
    cfg = _cfg(repo, fake_agent_tools, settings, tools=["git"])
    cfg.data["defaults"] = {"on_conflict": "backup"}
    report = run_plan(build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    perm = _perm_results(report)
    assert perm and perm[0].status == "backed_up"


def test_drift_wrong_type_container_is_modified(fake_agent_tools, tmp_path):
    # permissions.allow as a STRING → apply errors, so drift must say `modified` (not `missing`),
    # matching apply (B2/B3: status and apply agree on the shape problem).
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    settings.write_text(json.dumps({"permissions": {"allow": "nope"}}), encoding="utf-8")
    plan = build(_cfg(repo, fake_agent_tools, settings, tools=["git"]),
                 Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    rep = detect(plan)
    assert [d for d in rep.by_direction("modified") if d.category == "permissions"]
    assert not [d for d in rep.by_direction("missing") if d.category == "permissions"]


def test_drift_non_dict_root_is_modified(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    settings.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    plan = build(_cfg(repo, fake_agent_tools, settings, tools=["git"]),
                 Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    rep = detect(plan)
    mod = [d for d in rep.by_direction("modified") if d.category == "permissions"]
    assert mod and "not a JSON object" in mod[0].detail


def test_dual_target_permissions_and_harness_distinct_files(fake_agent_tools, tmp_path):
    # permissions.kind: opencode writes opencode.json; harness.kind: claude-code writes auto-mode
    # to a SEPARATE settings.json — the two land in different files in one apply.
    repo = tmp_path / "repo"; repo.mkdir()
    cc_settings = repo / "cc-settings.json"
    oc_settings = repo / "opencode.json"
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "harness": {"kind": "claude-code", "settings_path": str(cc_settings), "mode": "acceptEdits"},
            "permissions": {"kind": "opencode", "settings_path": str(oc_settings), "tools": ["git"]},
        },
        repo_root=repo,
    )
    run_plan(build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    # harness auto-mode landed in cc-settings.json; the allowlist landed in opencode.json
    assert json.loads(cc_settings.read_text())["permissions"]["defaultMode"] == "acceptEdits"
    assert json.loads(oc_settings.read_text())["permission"]["bash"]["git *"] == "allow"


def test_validate_rejects_bad_tool_name_and_non_json_settings_path():
    with pytest.raises(ConfigError):
        validate({"version": 1, "permissions": {"tools": ["git status"]}})  # space → broken entry
    with pytest.raises(ConfigError):
        validate({"version": 1, "permissions": {"extra": ["foo; rm -rf"]}})  # metachars
    with pytest.raises(ConfigError):
        validate({"version": 1, "permissions": {"tools": ["../../bin/x"]}})  # path traversal
    with pytest.raises(ConfigError):
        validate({"version": 1, "permissions": {"tools": ["-rf"]}})  # leading dash → bogus entry
    with pytest.raises(ConfigError):
        validate({"version": 1, "permissions": {"settings_path": "~/.codex/config.toml"}})  # not .json
    # a plain tool name (incl. 3d), an absolute path, + a .json path pass
    validate({"version": 1, "permissions": {"tools": ["git", "gh", "3d", "/opt/bin/tool"],
                                            "settings_path": "~/.claude/settings.json"}})


def test_opencode_idempotent(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "opencode.json"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_opencode_cfg(repo, fake_agent_tools, settings, tools=["git", "gh"]), cat, project_type="unknown")
    run_plan(plan)
    before = settings.read_text(encoding="utf-8")
    second = run_plan(plan)  # all keys already allow → skipped, byte-stable
    assert all(r.status == "skipped" for r in _perm_results(second))
    assert settings.read_text(encoding="utf-8") == before


def test_harness_kind_cascades_into_permissions_when_no_explicit_kind(fake_agent_tools, tmp_path):
    # no permissions.kind → the kind follows harness.kind (opencode here), not the claude-code default
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "opencode.json"
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "harness": {"kind": "opencode"},
            "permissions": {"settings_path": str(settings), "tools": ["git"]},  # no kind here
        },
        repo_root=repo,
    )
    perm = [a for a in build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown").actions
            if a.kind == "provision_permissions"]
    assert perm and perm[0].options["kind"] == "opencode"  # cascaded from harness.kind


def test_permissions_settings_path_with_multiple_harnesses_targets_first_supported(
    fake_agent_tools, tmp_path
):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "harness": {"kind": "claude-code", "kinds": ["opencode"]},
            "permissions": {"settings_path": str(settings), "tools": ["git"]},
        },
        repo_root=repo,
    )

    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    perm = [a for a in plan.actions if a.kind == "provision_permissions"]

    assert [a.options["kind"] for a in perm] == ["claude-code"]
    assert perm[0].target == settings
    assert any("settings_path override with multiple harnesses" in note for note in plan.notes)


def test_permissions_fan_out_to_supported_harness_kinds_by_default(
    fake_agent_tools, tmp_path, monkeypatch
):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    repo = tmp_path / "repo"; repo.mkdir()
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "harness": {"kind": "claude-code", "kinds": ["opencode", "codex"]},
            "permissions": {"tools": ["git"]},
        },
        repo_root=repo,
    )

    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    perm = [a for a in plan.actions if a.kind == "provision_permissions"]

    assert [a.options["kind"] for a in perm] == ["claude-code", "opencode"]
    assert [a.target for a in perm] == [
        home / ".claude" / "settings.json",
        home / ".config" / "opencode" / "opencode.json",
    ]
    # codex has no config allowlist → the note describes the achieved effect (execpolicy + hook
    # bridge), not a bare "skipped"; and rig plans a provision_execpolicy action for it.
    assert any(
        "harness 'codex' has no config allowlist" in note and "execpolicy" in note
        for note in plan.notes
    )
    execp = [a for a in plan.actions if a.kind == "provision_execpolicy"]
    assert [a.options["kind"] for a in execp] == ["codex"]
    assert execp[0].target == home / ".codex" / "rules" / "rig-managed.rules"


def test_permissions_explicit_null_masks_global_pin_and_fans_out(
    fake_agent_tools, tmp_path, monkeypatch
):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    repo = tmp_path / "repo"; repo.mkdir()
    gpath = global_config_path()
    gpath.parent.mkdir(parents=True, exist_ok=True)
    gpath.write_text(
        "version: 1\npermissions: {enabled: true, kind: claude-code}\n",
        encoding="utf-8",
    )
    (repo / "rig.yaml").write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\nci: {enabled: false}\n"
        "mcp: {enabled: false}\ngit_hooks: {dispatcher: {enabled: false}}\n"
        "harness: {kind: claude-code, kinds: [opencode]}\n"
        "permissions: {enabled: true, kind: null, tools: [git]}\n",
        encoding="utf-8",
    )
    cfg = load(repo)

    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    perm = [a for a in plan.actions if a.kind == "provision_permissions"]

    assert [a.options["kind"] for a in perm] == ["claude-code", "opencode"]
    assert [a.target for a in perm] == [
        home / ".claude" / "settings.json",
        home / ".config" / "opencode" / "opencode.json",
    ]


def test_disable_never_deletes_an_existing_user_entry(fake_agent_tools, tmp_path):
    # the headline safety invariant: `disable` only stops rig ADDING a tool; an entry the user
    # ALREADY has in their allowlist is NEVER removed by rig (additive-only — never clobbers).
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    settings.write_text(json.dumps({"permissions": {"allow": ["Bash(gitleaks:*)", "WebFetch"]}}), encoding="utf-8")
    plan = build(_cfg(repo, fake_agent_tools, settings, tools=["git"], disable=["gitleaks"]),
                 Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    run_plan(plan)
    allow = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert "Bash(gitleaks:*)" in allow  # user's pre-existing entry survives a disable
    assert "WebFetch" in allow
    assert "Bash(git:*)" in allow       # the desired tool was added


def test_disable_not_reported_as_missing_drift(fake_agent_tools, tmp_path):
    # symmetry: a disabled tool must not appear as `missing` drift either — apply won't add it, so
    # status must not claim it's missing (the two resolve the tool list independently).
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    plan = build(_cfg(repo, fake_agent_tools, settings, tools=["git"], disable=["git"]),
                 Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    run_plan(plan)
    rep = detect(plan)
    assert not [d for d in rep.items if d.category == "permissions" and "git" in d.detail]


def test_explicit_kind_claude_code_with_harness_opencode(fake_agent_tools, tmp_path):
    # permissions.kind wins over harness.kind: explicit claude-code beats a harness.kind: opencode
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "harness": {"kind": "opencode"},
            "permissions": {"kind": "claude-code", "settings_path": str(settings), "tools": ["git"]},
        },
        repo_root=repo,
    )
    perm = [a for a in build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown").actions
            if a.kind == "provision_permissions"]
    assert perm and perm[0].options["kind"] == "claude-code"


def test_extra_added_end_to_end(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    run_plan(build(_cfg(repo, fake_agent_tools, settings, tools=["git"], extra=["kubectl"]),
                   Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    allow = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert "Bash(git:*)" in allow and "Bash(kubectl:*)" in allow


def test_drift_non_object_intermediate_is_modified(fake_agent_tools, tmp_path):
    # `permissions` itself is a scalar → apply errors on the intermediate; drift must say modified
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    settings.write_text(json.dumps({"permissions": "TODO"}), encoding="utf-8")
    plan = build(_cfg(repo, fake_agent_tools, settings, tools=["git"]),
                 Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    rep = detect(plan)
    mod = [d for d in rep.by_direction("modified") if d.category == "permissions"]
    # ONE item, not one per container (allow/deny/ask all hit the same scalar `permissions` node;
    # apply raises exactly one error, so status must report exactly one problem)
    assert len(mod) == 1
    assert not [d for d in rep.by_direction("missing") if d.category == "permissions"]


def test_opencode_apply_errors_on_non_object_bash(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "opencode.json"
    settings.write_text(json.dumps({"permission": {"bash": ["git *"]}}), encoding="utf-8")  # list, not obj
    report = run_plan(build(_opencode_cfg(repo, fake_agent_tools, settings, tools=["git"]),
                            Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    perm = _perm_results(report)
    assert perm and perm[0].status == "error" and "not an object" in perm[0].detail


# ═══ deny / ask / raw-allow — the FULL permissions layer (rig-cli#100) ═══════════════
# CTO decision 2026-07-01: the harness permissions layer (deny, ask, AND allow) must be
# provisioned/reconciled by rig, not hand-edited. The harness evaluates deny → ask → allow
# BEFORE PreToolUse hooks and independent of the model — the OUTER belt; the argv-parsing
# agent-hooks stay the deep layer. The invariants under test: the baked baseline is scoped +
# conservative, the merge stays ADDITIVE per container, re-apply is a no-op, drift reports
# missing rig-managed entries AND user extras — and apply never deletes an extra.
def test_default_rules_are_scoped_and_conservative():
    deny = DEFAULT_RULES["claude-code"]["deny"]
    ask = DEFAULT_RULES["claude-code"]["ask"]
    # every baked Bash rule is SCOPED (Tool(specifier)) — a bare "Bash" deny would remove the
    # whole tool from the model's context. EnterWorktree is the one intentional whole-tool
    # deny: the tool itself can CREATE a worktree at a model-supplied path, which is the
    # location-bypass `rig worktree create` exists to close.
    for rule in (*deny, *ask):
        if rule == "EnterWorktree":
            continue
        assert rule.startswith("Bash(") and rule.endswith(")") and rule != "Bash(*)"
    assert "EnterWorktree" in deny
    assert "Bash(git worktree add:*)" in deny
    # prefix-only on purpose: `git * worktree add` would also match a commit message
    assert "Bash(git * worktree add:*)" not in deny
    # the CTO-decided deny baseline: raw PR merge, force push (flag-first AND later positions),
    # hook-bypass commit (flag-first only — see riglib.permissions for why), sudo rm, screencapture
    assert "Bash(gh pr merge:*)" in deny
    assert "Bash(git push --force:*)" in deny and "Bash(git push * --force *)" in deny
    assert "Bash(git push -f:*)" in deny and "Bash(git push * -f *)" in deny
    # end-anchored forms are EXPLICIT — `git push origin main --force` (flag last, nothing after)
    # must not hinge on the trailing-` *`-matches-end-of-string reading of the matcher
    assert "Bash(git push * --force)" in deny and "Bash(git push * -f)" in deny
    assert "Bash(git commit --no-verify:*)" in deny
    assert "Bash(sudo rm:*)" in deny
    assert "Bash(screencapture:*)" in deny
    assert "Bash(osascript:*)" in deny
    # rig-cli#187: `rg --pre` is arbitrary subprocess execution behind a read-only search grant.
    # rg's arg parser allows flags anywhere, so both flag-first AND later-position forms are
    # required, for BOTH the space value (`--pre CMD`) and `=` value (`--pre=CMD`) shapes — `=`
    # is a literal boundary character, not a space, so it needs its own pattern.
    assert "Bash(rg --pre:*)" in deny and "Bash(rg * --pre *)" in deny
    assert "Bash(rg --pre=*)" in deny and "Bash(rg * --pre=*)" in deny
    assert "Bash(rg * --pre)" in deny  # end-anchored: `rg pattern --pre` (flag last, no value)
    # `--pre-glob` is deliberately NOT globbed here — or anywhere, including the omp ARGV guard
    # (no effect without `--pre`, which is caught on its own; denying it would be pattern bloat
    # / a false positive for zero security value).
    assert not any("--pre-glob" in r for r in deny)
    # the safe force (`--force-with-lease`) must never be denied — the word boundary in the
    # rules above already excludes it; assert no rule names it explicitly either
    assert not any("--force-with-lease" in r for r in deny)
    # ask: sometimes-legit destructive commands prompt instead of hard-failing
    assert "Bash(pkill:*)" in ask and "Bash(killall:*)" in ask
    assert "Bash(git reset --hard:*)" in ask and "Bash(git reset * --hard *)" in ask
    assert "Bash(git reset * --hard)" in ask  # end-anchored: `git reset HEAD~1 --hard`


def test_resolve_rules_default_override_dedup_unknown():
    # None → the baked default for the kind
    assert resolve_rules("claude-code", "deny", None) == list(DEFAULT_RULES["claude-code"]["deny"])
    # an explicit list REPLACES the default wholesale (atomic, like permissions.tools)…
    assert resolve_rules("claude-code", "deny", ["Bash(foo:*)"]) == ["Bash(foo:*)"]
    # …so an explicit EMPTY list disables the baseline
    assert resolve_rules("claude-code", "ask", []) == []
    # dedup, first-seen order preserved
    assert resolve_rules("claude-code", "deny", ["Bash(a:*)", "Bash(a:*)", "Bash(b:*)"]) == \
        ["Bash(a:*)", "Bash(b:*)"]
    # opencode now has a VERIFIED rule dialect → its own baked baseline
    assert resolve_rules("opencode", "deny", None) == list(DEFAULT_RULES["opencode"]["deny"])
    assert resolve_rules("opencode", "ask", None) == list(DEFAULT_RULES["opencode"]["ask"])
    assert "opencode" in HARNESS_RULE_CONTAINERS and "claude-code" in HARNESS_RULE_CONTAINERS
    # a kind with NO rule containers at all still has none
    assert resolve_rules("codex", "deny", None) == []


def test_validate_rule_lists():
    validate({"version": 1, "permissions": {
        "allow": ["WebFetch", "Read(//tmp/x/**)", "mcp__pencil", "Bash(git push * --force *)"],
        "deny": ["Bash(sudo rm:*)"], "ask": ["Bash(pkill:*)"]}})
    with pytest.raises(ConfigError):
        validate({"version": 1, "permissions": {"deny": "Bash(x:*)"}})   # not a list
    with pytest.raises(ConfigError):
        validate({"version": 1, "permissions": {"deny": [42]}})          # not strings
    with pytest.raises(ConfigError):
        validate({"version": 1, "permissions": {"ask": ["Bash rm"]}})    # space outside specifier
    with pytest.raises(ConfigError):
        validate({"version": 1, "permissions": {"allow": ["Bash()"]}})   # empty specifier


def test_plan_carries_rule_options_defaults_and_overrides(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))
    # defaults: the baked deny/ask baseline, no raw allow entries
    plan = build(_cfg(repo, fake_agent_tools, settings), cat, project_type="unknown")
    opts = [a for a in plan.actions if a.kind == "provision_permissions"][0].options
    assert opts["deny_rules"] == list(DEFAULT_RULES["claude-code"]["deny"])
    assert opts["ask_rules"] == list(DEFAULT_RULES["claude-code"]["ask"])
    assert opts["allow_rules"] == []
    # config overrides: deny/ask REPLACE the baseline, allow ADDS raw entries
    plan2 = build(_cfg(repo, fake_agent_tools, settings,
                       deny=["Bash(shutdown:*)"], ask=[], allow=["WebFetch"]),
                  cat, project_type="unknown")
    opts2 = [a for a in plan2.actions if a.kind == "provision_permissions"][0].options
    assert opts2["deny_rules"] == ["Bash(shutdown:*)"]
    assert opts2["ask_rules"] == []
    assert opts2["allow_rules"] == ["WebFetch"]


def test_plan_opencode_uses_baked_baseline_and_drops_claude_dialect_config(fake_agent_tools, tmp_path):
    # opencode's user-facing permissions.allow/deny/ask config lists are claude-code dialect
    # (Bash(...) specifiers). rig does NOT adopt them as opencode glob keys (they would never
    # match); it drops them with a visible plan note and uses its BAKED opencode-dialect deny/ask
    # baseline instead. The raw allow config is dropped too (no claude-shaped glob key leaks in).
    from riglib.permissions import OPENCODE_ASK_RULES, OPENCODE_DENY_RULES

    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "opencode.json"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_opencode_cfg(repo, fake_agent_tools, settings, tools=["git"],
                               deny=["Bash(x:*)"], allow=["WebFetch"]), cat, project_type="unknown")
    opts = [a for a in plan.actions if a.kind == "provision_permissions"][0].options
    assert opts["allow_rules"] == []
    assert opts["deny_rules"] == list(OPENCODE_DENY_RULES)
    assert opts["ask_rules"] == list(OPENCODE_ASK_RULES)
    note = [n for n in plan.notes if "dropped" in n]
    assert note and "allow" in note[0] and "deny" in note[0] and "opencode" in note[0]
    # …and the raw WebFetch entry must NOT leak into the opencode container on apply
    report = run_plan(plan)
    assert not report.errors
    bash = json.loads(settings.read_text(encoding="utf-8"))["permission"]["bash"]
    assert "WebFetch" not in bash and "Bash(x:*)" not in bash
    assert bash["gh pr merge*"] == "deny"
    bash = json.loads(settings.read_text(encoding="utf-8"))["permission"]["bash"]
    assert "WebFetch" not in bash and "git *" in bash
    # …and with no raw lists configured there is no noise note
    plan2 = build(_opencode_cfg(repo, fake_agent_tools, settings, tools=["git"]), cat, project_type="unknown")
    assert not any("dropped" in n for n in plan2.notes)


def test_apply_writes_deny_and_ask_containers_idempotently(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg(repo, fake_agent_tools, settings, tools=["git"]), cat, project_type="unknown")
    first = run_plan(plan)
    assert not first.errors, [r.detail for r in first.errors]
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["permissions"]["deny"] == list(DEFAULT_RULES["claude-code"]["deny"])
    assert data["permissions"]["ask"] == list(DEFAULT_RULES["claude-code"]["ask"])
    second = run_plan(plan)  # every container converged → skipped, byte-stable
    assert all(r.status == "skipped" for r in _perm_results(second))
    assert json.loads(settings.read_text(encoding="utf-8")) == data


def test_apply_preserves_user_deny_and_ask_entries(fake_agent_tools, tmp_path):
    # the additive invariant extends to deny/ask: the user's own rules survive at their
    # position, rig's baseline appends after them, overlap dedups
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    settings.write_text(json.dumps({"permissions": {
        "deny": ["Bash(shutdown -h now)", "Bash(sudo rm:*)"],  # 2nd overlaps the baseline
        "ask": ["Bash(reboot:*)"],
    }}), encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    report = run_plan(build(_cfg(repo, fake_agent_tools, settings, tools=["git"]), cat, project_type="unknown"))
    assert not report.errors, [r.detail for r in report.errors]
    data = json.loads(settings.read_text(encoding="utf-8"))
    deny = data["permissions"]["deny"]
    assert deny[:2] == ["Bash(shutdown -h now)", "Bash(sudo rm:*)"]  # user's order preserved
    assert deny.count("Bash(sudo rm:*)") == 1                        # dedup — not re-appended
    for rule in DEFAULT_RULES["claude-code"]["deny"]:
        assert rule in deny
    assert data["permissions"]["ask"][0] == "Bash(reboot:*)"


def test_apply_merges_raw_allow_rules(fake_agent_tools, tmp_path):
    # permissions.allow (raw rule entries — the adopted hand-grown allowlist) merges into the
    # allow container alongside the tool-derived entries, deduped against what's already there
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    settings.write_text(json.dumps({"permissions": {"allow": ["WebFetch"]}}), encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    run_plan(build(_cfg(repo, fake_agent_tools, settings, tools=["git"],
                        allow=["WebFetch", "Read(//tmp/reports/**)", "mcp__pencil"]),
                   cat, project_type="unknown"))
    allow = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert allow[0] == "WebFetch" and allow.count("WebFetch") == 1  # dedup against existing
    assert "Bash(git:*)" in allow
    assert "Read(//tmp/reports/**)" in allow and "mcp__pencil" in allow


def test_drift_missing_deny_ask_entries_then_converged(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    settings.write_text(json.dumps({"permissions": {"allow": ["Bash(git:*)"]}}), encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg(repo, fake_agent_tools, settings, tools=["git"]), cat, project_type="unknown")
    missing = [d for d in detect(plan).by_direction("missing") if d.category == "permissions"]
    assert any("permissions.deny" in d.detail and "Bash(gh pr merge:*)" in d.detail for d in missing)
    assert any("permissions.ask" in d.detail and "Bash(pkill:*)" in d.detail for d in missing)
    run_plan(plan)  # apply converges → clean
    assert not [d for d in detect(plan).items if d.category == "permissions"]


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
    import pytest

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
    import pytest

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
