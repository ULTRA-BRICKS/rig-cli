"""Permission-allowlist tests: resolve, plan and apply (claude-code and opencode)."""

from __future__ import annotations

import json

from riglib.actions import run_plan
from riglib.catalog import Catalog
from riglib.config import LoadedConfig, validate, ConfigError
from riglib.drift import detect
from riglib.permissions import (
    DEFAULT_TOOLS,
    HARNESS_ALLOWLIST_NA,
    desired_entries,
    harness_supported,
    resolve_tools,
)
from riglib.plan import build

import pytest

from tests.permissions_helpers import _DESTRUCTIVE_RAW_COMMANDS, _cfg, _perm_results, _opencode_cfg



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
    # round-15 review finding: this exact malformed-file-backup-vs-on_conflict condition has
    # regressed three times across this branch's history — pin the `skip` side explicitly, not
    # just "the original bytes are still there" (which a backup copy elsewhere wouldn't disturb).
    assert not [p for p in repo.iterdir() if p.name.startswith("settings.json.rig-bak-")]


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

    # round-13 review finding: a SECOND identical apply (idempotent re-apply of the same config)
    # must not treat the user's surviving duplicate as rig's own and revoke it too. Before the
    # fix, the receipt kept claiming the entry as granted after rig's own copy was already taken
    # back (presence is boolean, doesn't distinguish WHICH physical copy remains) — so a re-apply
    # would delete the user's last remaining copy, violating both idempotency and the
    # "only one rig-proven copy is ever revoked" contract.
    plan3 = build(_cfg(repo, fake_agent_tools, settings, allow_gh=False), cat, project_type="unknown")
    run_plan(plan3)
    allow_after_reapply = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert allow_after_reapply.count("Bash(gh:*)") == 1, "re-apply must not delete the user's copy"


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


def test_opencode_allow_gh_false_never_revokes_an_entry_the_user_repurposed_to_a_stricter_value(
    fake_agent_tools, tmp_path,
):
    # round-14 review finding: the object-form revoke/drift value guard (`existing.get(entry) ==
    # allow_spec.value` in `_revoke_stale_default_grants`, mirrored in
    # `_check_permission_revocation`) refuses to touch an entry the user has hand-tightened to a
    # DIFFERENT value than rig's own `"allow"` — but nothing pinned it. A regression here would
    # delete a user's tightening, which is strictly worse than the gap this PR set out to close.
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "opencode.json"
    cat = Catalog.scan(str(fake_agent_tools))

    plan1 = build(_opencode_cfg(repo, fake_agent_tools, settings), cat, project_type="unknown")
    run_plan(plan1)  # gh *: allow — granted + receipted
    data = json.loads(settings.read_text(encoding="utf-8"))
    data["permission"]["bash"]["gh *"] = "ask"  # the user hand-tightens it
    settings.write_text(json.dumps(data), encoding="utf-8")

    plan2 = build(_opencode_cfg(repo, fake_agent_tools, settings, allow_gh=False), cat, project_type="unknown")
    assert not [d for d in detect(plan2).by_direction("extra") if d.category == "permissions"]
    run_plan(plan2)
    bash_after = json.loads(settings.read_text(encoding="utf-8"))["permission"]["bash"]
    assert bash_after.get("gh *") == "ask"  # the user's tightening survives untouched
