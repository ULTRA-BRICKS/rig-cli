"""Permission-allowlist tests: additive merge, receipts, revocation and malformed files."""

from __future__ import annotations

import json

from riglib.actions import run_plan
from riglib.catalog import Catalog
from riglib.config import LoadedConfig, validate, ConfigError
from riglib.drift import detect
from riglib.permissions import (
    DEFAULT_TOOLS,
)
from riglib.plan import build

import pytest

from tests.permissions_helpers import _DESTRUCTIVE_RAW_COMMANDS, _cfg, _perm_results, _opencode_cfg


def test_adversarial_receipt_cannot_authorize_revoking_a_non_git_hosting_entry(fake_agent_tools, tmp_path):
    # round-14 review finding: `receipt_granted_entries` clamps to `_valid_git_hosting_entries`
    # as defense against a hand-edited/forged receipt — but nothing planted one and proved the
    # clamp actually holds. `explicit_git_hosting_revocation_entries` structurally can only ever
    # target git-hosting tools (rendered from `DEFAULT_GIT_HOSTING_TOOLS`), so a forged claim
    # about an unrelated tool can't reach `to_revoke` via that route either — this test proves the
    # SECOND, independent layer: even a receipt that claims a non-git-hosting entry as "granted"
    # never lets it become `prior_granted`, so it's never eligible for revocation at all.
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))

    plan1 = build(_cfg(repo, fake_agent_tools, settings), cat, project_type="unknown")
    run_plan(plan1)  # creates the real receipt file at settings.json.claude-code.rig-permissions-grants.json
    receipt = settings.parent / "settings.json.claude-code.rig-permissions-grants.json"
    assert receipt.is_file()
    # forge the receipt: the legitimate gh grant PLUS an adversarial non-git-hosting claim
    receipt.write_text(
        json.dumps({"template": 1, "kind": "claude-code", "granted": ["Bash(gh:*)", "Bash(kubectl:*)"]}),
        encoding="utf-8",
    )
    data = json.loads(settings.read_text(encoding="utf-8"))
    data["permissions"]["allow"].append("Bash(kubectl:*)")  # present on disk too, as if hand-added
    settings.write_text(json.dumps(data), encoding="utf-8")

    plan2 = build(_cfg(repo, fake_agent_tools, settings, allow_gh=False), cat, project_type="unknown")
    run_plan(plan2)
    allow_after = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert "Bash(gh:*)" not in allow_after  # the legitimate claim was read and acted on
    assert "Bash(kubectl:*)" in allow_after  # the forged claim was clamped out, never actionable


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


def test_allow_gh_false_still_revokes_despite_a_sibling_repos_explicit_extra_declaration(fake_agent_tools, tmp_path):
    # rig-cli#549: repo A default-grants+receipts gh on a SHARED settings file. Repo B (a
    # different repo, no allow_gh:false) applies against the SAME file with its own explicit
    # `extra: [gh]` — a no-op merge (gh is already present) that must NOT silently launder gh's
    # receipted ownership away from repo A. A later allow_gh:false (from repo A OR an unrelated
    # repo C) against the shared file must still find and revoke the grant.
    repo_a = tmp_path / "repo-a"; repo_a.mkdir()
    repo_b = tmp_path / "repo-b"; repo_b.mkdir()
    repo_c = tmp_path / "repo-c"; repo_c.mkdir()
    shared_settings = tmp_path / "shared-settings.json"
    cat = Catalog.scan(str(fake_agent_tools))

    plan_a = build(_cfg(repo_a, fake_agent_tools, shared_settings), cat, project_type="unknown")
    run_plan(plan_a)  # gh default-granted + receipted, owned by repo A
    assert "Bash(gh:*)" in json.loads(shared_settings.read_text(encoding="utf-8"))["permissions"]["allow"]

    # repo B: unrelated repo, explicitly names gh via `extra` — no allow_gh:false, no revoke intent
    plan_b = build(_cfg(repo_b, fake_agent_tools, shared_settings, tools=["git"], extra=["gh"]),
                    cat, project_type="unknown")
    run_plan(plan_b)  # a no-op merge (gh already present) — must not strip repo A's receipt

    # repo C: unrelated repo, later explicitly opts out via allow_gh:false
    plan_c = build(_cfg(repo_c, fake_agent_tools, shared_settings, allow_gh=False),
                    cat, project_type="unknown")
    extra = [d for d in detect(plan_c).by_direction("extra") if d.category == "permissions"]
    assert any("Bash(gh:*)" in d.detail for d in extra), (
        "repo A's gh grant must still be reported as revocable drift after repo B's unrelated "
        f"explicit `extra` apply — receipt ownership must not have been laundered away: {extra}"
    )
    run_plan(plan_c)
    allow_after = json.loads(shared_settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert "Bash(gh:*)" not in allow_after, (
        "allow_gh:false must still revoke repo A's default grant even though an unrelated "
        "sibling repo (B) separately, explicitly named gh via `extra` in between"
    )


def test_legacy_shaped_receipt_stays_revocable_even_after_the_same_repo_explicitly_redeclares_it(
    fake_agent_tools, tmp_path,
):
    # rig-cli#549 (review finding, accepted trade-off — see `_receipt_grant_owners`'s "Known
    # residual gap" docstring): a receipt shipped in the LEGACY flat-list shape (template 1, no
    # ownership at all — real machines have these) can never prove which repo originally earned
    # the grant, so it maps to the `""` "owner unknown" sentinel — which can never match any real
    # `str(action.source)`. That makes the entry fail-safe (never silently launder-able), but it
    # also means the round-9 "an explicit same-repo redeclaration clears an entry from tracking"
    # contract (proved for a FRESH, owned receipt by
    # `test_explicit_redeclaration_clears_a_receipt_from_an_earlier_default_grant`) does not apply
    # to a pre-existing legacy grant: it stays revocable by a later `allow_gh: false`, even the
    # SAME repo's own, once that repo has explicitly redeclared the tool by name. Pinned here so
    # it is a documented, deliberate choice, not silent drift.
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    settings.write_text(json.dumps({"permissions": {"allow": ["Bash(gh:*)"]}}), encoding="utf-8")
    receipt = repo / "settings.json.claude-code.rig-permissions-grants.json"
    receipt.write_text(
        json.dumps({"template": 1, "kind": "claude-code", "granted": ["Bash(gh:*)"]}),
        encoding="utf-8",
    )
    cat = Catalog.scan(str(fake_agent_tools))

    # same repo explicitly redeclares gh via `tools` — a never-receipted, deliberate user assertion
    plan1 = build(_cfg(repo, fake_agent_tools, settings, tools=["gh"]), cat, project_type="unknown")
    run_plan(plan1)
    assert "Bash(gh:*)" in json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]

    # the same repo later opts out — with a FRESH (owned) receipt this would leave the explicit
    # `tools` declaration untouched (round-9); with a legacy (unowned) receipt it is still revoked
    plan2 = build(_cfg(repo, fake_agent_tools, settings, tools=["tg"], allow_gh=False),
                  cat, project_type="unknown")
    run_plan(plan2)
    allow_after = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert "Bash(gh:*)" not in allow_after


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


def test_receipt_never_collides_across_harness_kinds_sharing_one_settings_path(fake_agent_tools, tmp_path):
    # round-11 review finding: the round-2 fix separated receipts by settings-FILE identity, but
    # two DIFFERENT harness kinds each configured (independently, in two separate repos) with an
    # explicit `permissions.settings_path` pointing at the exact same file were NOT separated —
    # the receipt filename carried no `kind`, so the second kind's apply overwrote the first
    # kind's receipt wholesale, and the kind-match guard in `receipt_granted_entries` then treated
    # the first kind's real grant as unreceipted (fails open: the entry survives forever, an
    # opt-out silently can't revoke it — never fails dangerous).
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "shared.json"  # both kinds independently point here
    cat = Catalog.scan(str(fake_agent_tools))

    cc_plan1 = build(_cfg(repo, fake_agent_tools, settings), cat, project_type="unknown")
    run_plan(cc_plan1)  # claude-code grants + receipts gh under kind=claude-code
    assert "Bash(gh:*)" in json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]

    oc_plan1 = build(_opencode_cfg(repo, fake_agent_tools, settings), cat, project_type="unknown")
    run_plan(oc_plan1)  # a SEPARATE opencode apply syncs its OWN receipt against the same file
    assert json.loads(settings.read_text(encoding="utf-8"))["permission"]["bash"].get("gh *") == "allow"

    # the claude-code grant must still be revocable — its receipt must not have been clobbered by
    # the opencode apply's own receipt sync against the same shared file
    cc_plan2 = build(_cfg(repo, fake_agent_tools, settings, allow_gh=False), cat, project_type="unknown")
    extra = [d for d in detect(cc_plan2).by_direction("extra") if d.category == "permissions"]
    assert any("Bash(gh:*)" in d.detail for d in extra), extra
    run_plan(cc_plan2)
    allow_after = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert "Bash(gh:*)" not in allow_after


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
    from riglib.actions.permissions_receipt import permissions_receipt_file

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


def test_revoke_only_action_against_malformed_file_is_also_a_true_noop(fake_agent_tools, tmp_path):
    # round-11 review finding: the fully-empty-action guard (finding #3 / the test above) only
    # covers `total_desired == 0 AND nothing to revoke`. When there IS a stale receipted grant to
    # revoke (so that guard's `not ctx.to_revoke` half is False) but the settings file on disk is
    # malformed, the OLD code still parsed first — backing up the malformed file and resetting to
    # `{}` — before discovering the revoke target isn't even present in a `{}` document and the
    # whole action resolves to a no-op. One stray backup per re-apply (self-healing, since the
    # receipt clears to empty after this run) but still the exact "don't touch a malformed file
    # when nothing ends up written" invariant finding #3 was meant to establish.
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))

    run_plan(build(_cfg(repo, fake_agent_tools, settings), cat, project_type="unknown"))
    assert "Bash(gh:*)" in json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]

    settings.write_text("{ broken", encoding="utf-8")  # corrupted out-of-band
    cfg = _cfg(repo, fake_agent_tools, settings, tools=[], deny=[], ask=[], allow_gh=False)
    cfg.data["defaults"] = {"on_conflict": "backup"}
    plan = build(cfg, cat, project_type="unknown")

    report = run_plan(plan)
    perm = _perm_results(report)
    assert perm and perm[0].status == "skipped", perm[0].detail if perm else perm
    assert settings.read_text(encoding="utf-8") == "{ broken"  # untouched, not reset to {}
    assert not [p for p in repo.iterdir() if p.name.startswith("settings.json.rig-bak-")]


def test_disable_gh_revokes_a_rig_granted_default_entry_same_as_allow_gh_false(fake_agent_tools, tmp_path):
    # round-11 review finding: `disable: [gh]` is documented as an equal revocation trigger to
    # `allow_gh: false` (both fold into `_explicit_git_hosting_opt_out`), but every revocation
    # test used `allow_gh=False` — nothing pinned that `disable: [gh]` alone actually populates
    # the revoke signal and deletes a receipted grant.
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))

    plan1 = build(_cfg(repo, fake_agent_tools, settings), cat, project_type="unknown")
    run_plan(plan1)  # gh default-granted + receipted

    plan2 = build(_cfg(repo, fake_agent_tools, settings, disable=["gh"]), cat, project_type="unknown")
    extra = [d for d in detect(plan2).by_direction("extra") if d.category == "permissions"]
    assert any("Bash(gh:*)" in d.detail for d in extra), extra
    run_plan(plan2)
    allow_after = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert "Bash(gh:*)" not in allow_after


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


def test_malformed_json_still_backed_up_under_on_conflict_overwrite(fake_agent_tools, tmp_path):
    # round-13 review finding: the round-11 refactor deferred the malformed-file backup to write
    # time but accidentally gated it on `_should_backup` (the WELL-FORMED-file predicate,
    # `on_conflict == "backup"` only) instead of the original, policy-independent
    # `on_conflict != "skip"` — silently dropping the backup for `on_conflict: overwrite` and
    # resetting the malformed file to `{}` with zero recovery copy, contradicting the function's
    # own "never a blind overwrite of the user's data" docstring line.
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    settings.write_text("{ broken", encoding="utf-8")
    cfg = _cfg(repo, fake_agent_tools, settings, tools=["git"])
    cfg.data["defaults"] = {"on_conflict": "overwrite"}
    report = run_plan(build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    perm = _perm_results(report)
    assert perm
    # NB: don't assert bare "malformed" in perm[0].detail here — pytest's tmp_path embeds this
    # test's own NAME in the temp dir it hands back (e.g. .../test_malformed_json_still_back0/...),
    # so that substring is present in the path portion of `detail` regardless of whether a backup
    # actually happened, silently defeating the check either way. Assert the specific backup-note
    # phrase instead, plus (the real signal) that a backup file actually landed on disk.
    assert "(backed up malformed config" in perm[0].detail
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
