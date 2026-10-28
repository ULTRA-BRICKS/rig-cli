"""Permission-allowlist tests: multi-harness fan-out, config-driven tools and drift."""

from __future__ import annotations

import json

from riglib.actions import run_plan
from riglib.catalog import Catalog
from riglib.config import LoadedConfig, validate, ConfigError, global_config_path, load
from riglib.drift import detect
from riglib.permissions import (
    DEFAULT_RULES,
    HARNESS_RULE_CONTAINERS,
    resolve_rules,
)
from riglib.plan import build

import pytest

from tests.permissions_helpers import _cfg, _perm_results, _opencode_cfg, RUNPOD_MCP_WHOLE_TOOL_DENIES, RUNPOD_CREATE_ARGV


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
    settings.write_text(json.dumps({"permissions": "scalar"}), encoding="utf-8")
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
    # whole tool from the model's context. The whole-tool denies are intentional: EnterWorktree
    # can CREATE a worktree at a model-supplied path (the location-bypass `rig worktree create`
    # exists to close), and the Runpod MCP create/deploy tools rent compute outside runpod-run.
    whole_tool = {"EnterWorktree", *RUNPOD_MCP_WHOLE_TOOL_DENIES}
    for rule in (*deny, *ask):
        if rule in whole_tool:
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




def test_belts_deny_direct_runpod_creation():
    from riglib.permissions import (
        CLAUDE_CODE_DENY_RULES,
        CODEX_DENY_RULES,
        OMP_GUARD_DENY_RULES,
        OPENCODE_DENY_RULES,
    )

    # the list above is the omp guard's whole runpodctl/flash argv family: a new creation form
    # added to the guard must be added here, and so reach every belt
    guard_argv = {r.tokens for r in OMP_GUARD_DENY_RULES
                  if r.matcher == "argv_prefix" and r.tokens[0] in ("runpodctl", "flash")}
    assert guard_argv == set(RUNPOD_CREATE_ARGV)
    for tool in RUNPOD_MCP_WHOLE_TOOL_DENIES:
        assert tool in CLAUDE_CODE_DENY_RULES, tool
    for argv in RUNPOD_CREATE_ARGV:
        assert f"Bash({' '.join(argv)}:*)" in CLAUDE_CODE_DENY_RULES, argv
        assert " ".join(argv) + "*" in OPENCODE_DENY_RULES, argv
        assert argv in CODEX_DENY_RULES, argv


def test_belts_keep_runpod_reads_and_teardown_open():
    from riglib.permissions import CLAUDE_CODE_DENY_RULES, OPENCODE_DENY_RULES

    # reads and teardown stay open on every belt
    for allowed in ("runpodctl pod list", "runpodctl user", "runpodctl billing", "runpodctl doctor",
                    "runpodctl pod delete", "flash --help"):
        assert not any(allowed.startswith(r[len("Bash("):-len(":*)")]) for r in CLAUDE_CODE_DENY_RULES
                       if r.startswith("Bash(") and r.endswith(":*)")), allowed
        assert not any(allowed.startswith(r[:-1]) for r in OPENCODE_DENY_RULES if r.endswith("*")), allowed


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
