"""`gh ship` delegator provisioning tests: alias area (see test_ship_delegator_content for overview)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from riglib.actions.runner import _do_provision_ship_delegator
from riglib.actions.ship_delegator import (
    resolve_ship_delegator,
    ship_delegator_content,
    ship_env_file_content,
    ship_env_file_path,
)
from riglib.drift import detect
from riglib.plan import Action, build
from riglib.actions import ship_delegator as ship_delegator_mod
from tests.ship_delegator_helpers import (
    _action,
    _alias_action,
    _apply,
    _canonical_ship,
    _delegator_path,
    _exclude_path,
    _git_repo,
    _loaded,
    _plan_with_action,
)

def test_ship_env_is_intentionally_absent_from_config_schema():
    # ship_env is a STATUS-ONLY area (like `ship` and `ship_delegator`, which are also absent
    # from the wizard/config-web registry): it has no config options of its own — the machine
    # env file is derived from the ship_delegator action's canonical_ship. Pin the intent so a
    # future registry-parity sweep doesn't "fix" it into the wizard.
    from riglib.schema import AREAS as SCHEMA_AREAS

    assert "ship_env" not in {a.category for a in SCHEMA_AREAS}


def test_apply_only_ship_env_aliases_to_the_owning_action():
    # `ship_env` is a drift/status-only category; the action that repairs it is ship_delegator
    # normally. `rig apply --only ship_env` must scope to it, never a silent no-op.
    from riglib.cli_apply import _scope_categories

    assert "ship_delegator" in _scope_categories("ship_env")
    assert _scope_categories("skills,ci") == {"skills", "ci"}


def test_only_ship_env_keeps_ci_only_alias_but_not_the_option_less_one(tmp_path):
    # ci-only combo: `apply --only ship_env` must keep the canonical_ship-carrying alias (its env
    # owner) — but NEVER the option-less alias of a normal delegator-enabled plan, which does not
    # own ship_env and could clobber a user's `gh ship` alias (regression guard, codex P1 on #151).
    from riglib.cli_apply import _action_in_only_scope, _scope_categories

    wanted = _scope_categories("ship_env")
    canonical = _canonical_ship(tmp_path)
    assert _action_in_only_scope(_alias_action(canonical), wanted), "ci-only owner must survive"
    assert not _action_in_only_scope(_alias_action(None), wanted), "option-less alias must be dropped"


def test_only_gh_ship_alias_keeps_alias_and_does_not_drag_delegator():
    from riglib.cli_apply import _action_in_only_scope, _scope_categories

    wanted = _scope_categories("gh_ship_alias")
    assert _action_in_only_scope(_alias_action(None), wanted)
    assert not _action_in_only_scope(_action(Path("/repo"), Path("/x/ci/ship/ship.sh")), wanted)


def test_ship_env_provenance_uses_declaring_layer_of_the_combo():
    # ci-only combo declared in the GLOBAL cascade — even alongside an UNRELATED repo rig.yaml:
    # ship_env / gh_ship_alias provenance must name the layer that actually declares the combo
    # (via source_for_key), not blindly the repo file (codex P2 on #151).
    from riglib.cli_status_areas import _declaring_config

    global_cfg = Path("/home/u/.config/rig/config.yaml")
    repo_cfg = Path("/repo/rig.yaml")

    class _Loaded:
        repo_path = repo_cfg
        global_path = global_cfg

        def source_for_key(self, key):  # ship_delegator declared in GLOBAL, everything else in repo
            return global_cfg if key == "ship_delegator" else repo_cfg

    assert _declaring_config("ship_env", _Loaded()) == str(global_cfg)
    assert _declaring_config("gh_ship_alias", _Loaded()) == str(global_cfg)


def test_ship_env_area_configured_in_ci_only_combo():
    # areas registry parity: with only the gh_ship_alias action present (ci-only combo — no
    # ship_delegator action), the ship_env area must still roll up as CONFIGURED via configured_by,
    # not render as "not configured".
    from riglib.areas import AREAS, area_matches_action

    area = next(a for a in AREAS if a.key == "ship_env")
    assert area_matches_action(area, "gh_ship_alias", None), "alias owner must configure ship_env"
    assert area_matches_action(area, "ship_delegator", None), "delegator owner still configures it"
    assert not area_matches_action(area, "skills", None)


def test_divergent_roots_last_apply_wins_and_other_repo_drifts(tmp_path):
    # INVARIANT: one agent-tools checkout per machine. Two repos declaring DIVERGENT roots is an
    # ambiguous setup — each apply points the ONE machine env file at its own root (last apply
    # wins) and status in the other repo honestly reports env-file drift, never silent corruption.
    repo_a = _git_repo(tmp_path / "repo-a")
    repo_b = _git_repo(tmp_path / "repo-b")
    canonical_a = _canonical_ship(tmp_path)  # tmp/agent-tools
    other = tmp_path / "other-agent-tools" / "ci" / "ship" / "ship.sh"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("#!/usr/bin/env bash\necho other\n", encoding="utf-8")
    _apply(repo_a, canonical_a)
    _apply(repo_b, other)  # last apply wins the machine pointer
    assert ship_env_file_path().read_text(encoding="utf-8") == ship_env_file_content(other)
    # repo A's plan now sees env-file drift (stale for ITS declared root) — the honest signal
    report = detect(_plan_with_action(repo_a, canonical_a))
    env_items = [i for i in report.items if i.category == "ship_env" and i.item == "env-file"]
    assert env_items and env_items[0].direction == "modified"
    # repo B's plan reads clean
    report_b = detect(_plan_with_action(repo_b, other))
    assert not [i for i in report_b.items if i.category in ("ship_delegator", "ship_env")]


def test_apply_errors_when_env_file_unwritable(tmp_path, monkeypatch):
    # if the machine env file cannot be written, the delegator cannot resolve the checkout on a
    # clean machine → the action must ERROR, not report a misleading success.

    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    monkeypatch.setattr(
        ship_delegator_mod, "_reconcile_ship_env_file",
        lambda c, oc: (False, "could not write env file: boom", "unchanged"),
    )
    res = _apply(repo, canonical)
    assert res.status == "error"
    assert "env file" in res.detail


def test_selfhosting_repo_apply_never_touches_the_env_file(tmp_path, monkeypatch):
    # a repo that CARRIES its own ci/ship/ship.sh (agent-tools self-hosts) never reads the env
    # file — the delegator's repo-local branch wins first — so apply must not reconcile it AT
    # ALL there (status skips the check for such a repo; parity cuts both ways: no rewrite, no
    # backup, no error on an unwritable $XDG_CONFIG_HOME, nothing status never flagged).
    from riglib.actions.ship_delegator import _SHIP_ENV_HEADER

    repo = _git_repo(tmp_path / "repo")
    repo_ship = repo / "ci" / "ship" / "ship.sh"
    repo_ship.parent.mkdir(parents=True, exist_ok=True)
    repo_ship.write_text("#!/usr/bin/env bash\necho local\n", encoding="utf-8")
    canonical = _canonical_ship(tmp_path)
    # a stale RIG-OWNED env file that an ordinary repo's apply would rewrite in place
    p = ship_env_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    stale = f"{_SHIP_ENV_HEADER}: machine-level pointer.\nAGENT_TOOLS_ROOT='/stale/root'\n"
    p.write_text(stale, encoding="utf-8")
    # drift agrees there is nothing to do for this repo…
    report = detect(_plan_with_action(repo, canonical))
    assert not [i for i in report.items if i.category == "ship_env"]
    # …and apply indeed does nothing to the env file (no rewrite, no .rig-bak)
    res = _apply(repo, canonical)
    assert res.status != "error"
    assert _delegator_path(repo).is_file()
    assert p.read_text(encoding="utf-8") == stale
    assert list(p.parent.glob("env.rig-bak-*")) == []
    # even an UNWRITABLE env path can't fail apply for this repo — the reconcile is never called
    monkeypatch.setattr(
        ship_delegator_mod, "_reconcile_ship_env_file",
        lambda c, oc: (_ for _ in ()).throw(AssertionError("env reconcile must not run")),
    )
    res2 = _apply(repo, canonical)
    assert res2.status == "skipped"


def test_env_file_path_mirrors_bash_reader_exactly(monkeypatch, tmp_path):
    # the python WRITER and the bash READER (`${XDG_CONFIG_HOME:-${HOME:-}/.config}`) must agree
    # expansion-for-expansion, or apply writes one file and the delegator reads another (exit
    # 127 in a sanitized env). XDG set → XDG wins; else HOME; else the same degenerate
    # `/.config` bash expands to — never os.path.expanduser's passwd-database home.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert ship_env_file_path() == tmp_path / "xdg" / "agent-tools" / "env"
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert ship_env_file_path() == tmp_path / "home" / ".config" / "agent-tools" / "env"
    monkeypatch.delenv("HOME", raising=False)
    assert ship_env_file_path() == Path("/.config/agent-tools/env")


def test_ship_env_drift_is_classified_global_layer():
    # the machine env file is a MACHINE artifact: `rig status` must render its drift under the
    # GLOBAL heading (not blame the repo), via its own GLOBAL area — whose configured-ness keys
    # off the repo-scoped ship_delegator action that writes the file (configured_by), so a
    # configured ship gate never renders the env area as a false "not configured".
    from riglib.areas import AREAS, area_matches_action, area_matches_drift
    from riglib.layers import GLOBAL, REPO, layer_for_category

    assert layer_for_category("ship_env") == GLOBAL
    assert layer_for_category("ship_delegator") == REPO
    area = next(a for a in AREAS if a.key == "ship_env")
    assert area.layer == GLOBAL
    assert area_matches_drift(area, "ship_env", "env-file", "modified")
    assert area_matches_drift(area, "ship_env", "env-file", "missing")
    # the ship_delegator ACTION marks the env area configured…
    assert area_matches_action(area, "ship_delegator", {})
    # …but the delegator's own drift stays out of the env area (and vice versa)
    assert not area_matches_drift(area, "ship_delegator", "delegator", "modified")
    deleg_area = next(a for a in AREAS if a.key == "ship_delegator")
    assert not area_matches_drift(deleg_area, "ship_env", "env-file", "modified")


def test_committed_delegator_stays_byte_clean_across_applies(tmp_path):
    # the agent-tools#151 scenario (rig-cli#108): the repo COMMITS the portable delegator. Apply
    # must byte-match it → the file is never rewritten, no .rig-bak appears, and git status for
    # .claude/scripts/ stays EMPTY across repeated applies.
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    deleg = _delegator_path(repo)
    deleg.parent.mkdir(parents=True, exist_ok=True)
    deleg.write_text(ship_delegator_content(), encoding="utf-8")
    deleg.chmod(0o755)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "commit delegator"],
        cwd=repo, check=True,
    )
    before = deleg.stat().st_mtime_ns
    _apply(repo, canonical)  # first apply may add the exclude entry + env file
    res2 = _apply(repo, canonical)
    assert res2.status == "skipped"
    assert deleg.stat().st_mtime_ns == before, "a committed, matching delegator was rewritten"
    assert list(deleg.parent.glob("*.rig-bak-*")) == []
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", ".claude/scripts/"],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    assert status.stdout.strip() == "", f"committed delegator drifted: {status.stdout!r}"


def test_drift_reports_missing_and_stale_env_file(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)
    p = ship_env_file_path()
    # stale root → modified
    p.write_text("AGENT_TOOLS_ROOT='/stale/root'\n", encoding="utf-8")
    report = detect(_plan_with_action(repo, canonical))
    items = [i for i in report.items if i.category == "ship_env"]
    assert [(i.item, i.direction) for i in items] == [("env-file", "modified")]
    assert not [i for i in report.items if i.category == "ship_delegator"]
    # absent → missing
    p.unlink()
    report = detect(_plan_with_action(repo, canonical))
    items = [i for i in report.items if i.category == "ship_env"]
    assert [(i.item, i.direction) for i in items] == [("env-file", "missing")]


# ── create / idempotency ───────────────────────────────────────────────────────────
def test_create_writes_delegator_and_ignores_it(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    assert resolve_ship_delegator(repo).state == "create"

    res = _apply(repo, canonical)
    assert res.status == "created"

    deleg = _delegator_path(repo)
    assert deleg.is_file()
    assert deleg.read_text(encoding="utf-8") == ship_delegator_content()
    # executable bit set
    assert deleg.stat().st_mode & 0o111
    # ignored in .git/info/exclude → never dirties the worktree
    excl = _exclude_path(repo).read_text(encoding="utf-8")
    assert ".claude/scripts/pr-ship.sh" in excl


def test_provisioned_file_does_not_dirty_the_worktree(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    # a committed file so the tree starts clean
    (repo / "README.md").write_text("# repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=repo,
        check=True,
    )
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    )
    # the provisioned delegator must NOT show up as untracked/modified
    assert status.stdout.strip() == "", f"worktree dirtied: {status.stdout!r}"


def test_idempotent_second_apply_skips(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    assert _apply(repo, canonical).status == "created"
    second = _apply(repo, canonical)
    assert resolve_ship_delegator(repo).state == "ok"
    assert second.status == "skipped"


def test_stale_delegator_is_updated(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    deleg = _delegator_path(repo)
    deleg.parent.mkdir(parents=True, exist_ok=True)
    deleg.write_text("#!/usr/bin/env bash\n# stale\n", encoding="utf-8")
    assert resolve_ship_delegator(repo).state == "update"
    res = _apply(repo, canonical)
    assert res.status in ("updated", "backed_up")
    assert deleg.read_text(encoding="utf-8") == ship_delegator_content()


# ── CRLF / line-ending agreement between drift and reconcile ────────────────────────
def test_crlf_exclude_file_drift_and_reconcile_agree(tmp_path):
    # a .git/info/exclude with CRLF endings must not make drift ("ok") and reconcile ("rewrite")
    # disagree: both read RAW, so once provisioned a re-apply is a no-op and status stays clean.
    from riglib.actions.ship_delegator import ship_delegator_exclude_block_text

    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    excl = _exclude_path(repo)
    excl.parent.mkdir(parents=True, exist_ok=True)
    # seed a CRLF user line, then provision
    with excl.open("w", encoding="utf-8", newline="") as fh:
        fh.write("*.log\r\n")
    _apply(repo, canonical)
    # provisioned: now in sync; a second apply is a true no-op (no rewrite churn)
    assert resolve_ship_delegator(repo).state == "ok"
    before = excl.read_bytes()
    res2 = _apply(repo, canonical)
    assert res2.status == "skipped"
    assert excl.read_bytes() == before  # byte-identical → drift & reconcile agreed
    # the rig block itself is present and the user CRLF line survived verbatim
    assert b"*.log\r\n" in excl.read_bytes()
    assert ship_delegator_exclude_block_text() in excl.read_text(encoding="utf-8")


def test_whitespace_only_exclude_content_is_preserved(tmp_path):
    # a whitespace-only exclude file must keep its blank lines (verbatim) when the block is appended.
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    excl = _exclude_path(repo)
    excl.parent.mkdir(parents=True, exist_ok=True)
    excl.write_text("\n\n\n", encoding="utf-8")
    _apply(repo, canonical)
    after = excl.read_text(encoding="utf-8")
    assert after.startswith("\n\n\n")  # pre-existing blanks preserved
    assert ".claude/scripts/pr-ship.sh" in after


# ── non-git directory: file written, exclude step skipped, drift reports the file only ─
def test_non_git_dir_writes_file_skips_exclude(tmp_path):
    plain = tmp_path / "plain"  # NOT a git repo
    plain.mkdir()
    canonical = _canonical_ship(tmp_path)
    # drift: the missing delegator file (no ignore concept without git) — plus the not-yet-written
    # machine env file, which is legitimate pre-apply drift; check the delegator item specifically.
    report = detect(_plan_with_action(plain, canonical))
    items = [i for i in report.items if i.category == "ship_delegator" and i.item == "delegator"]
    assert len(items) == 1 and items[0].direction == "missing"
    # apply writes the file; no exclude (no git), reported as a non-error
    res = _apply(plain, canonical)
    assert res.status in ("created", "updated")
    assert _delegator_path(plain).is_file()
    assert "no git repo" in res.detail
    # now in sync
    assert resolve_ship_delegator(plain).state == "ok"


def test_missing_canonical_ship_option_errors(tmp_path):
    # a malformed action with no canonical_ship must ERROR, not write a broken `canonical=` delegator.
    repo = _git_repo(tmp_path / "repo")
    action = Action(
        kind="provision_ship_delegator", category="ship_delegator", item="delegator",
        source=repo, target=repo, options={},  # no canonical_ship
    )
    res = _do_provision_ship_delegator(action, "backup")
    assert res.status == "error" and "canonical_ship" in res.detail
    assert not _delegator_path(repo).exists()


# ── git worktree: info/exclude lives in the COMMON gitdir ───────────────────────────
def test_ignore_resolves_through_git_worktree(tmp_path):
    main = _git_repo(tmp_path / "main")
    (main / "README.md").write_text("# m\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=main, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=main,
        check=True,
    )
    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-q", str(wt), "-b", "feat"], cwd=main, check=True
    )
    canonical = _canonical_ship(tmp_path)
    res = _apply(wt, canonical)
    assert res.status == "created"
    assert _delegator_path(wt).is_file()
    # NON-circular proof: git itself must HONOR the written entry — ask git whether it ignores the
    # delegator (check-ignore exits 0 + names the rule's source file only when an exclude matched).
    ci = subprocess.run(
        ["git", "-C", str(wt), "check-ignore", "-v", ".claude/scripts/pr-ship.sh"],
        capture_output=True, text=True,
    )
    assert ci.returncode == 0, f"git does not ignore the delegator in the worktree: {ci.stderr}"
    assert "info/exclude" in ci.stdout, ci.stdout
    # and the worktree stays clean (the user-facing invariant)
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=wt, capture_output=True, text=True, check=True
    )
    assert status.stdout.strip() == "", f"worktree dirtied: {status.stdout!r}"


# ── plan gating: default ON + opt-out ───────────────────────────────────────────────
def test_plan_includes_delegator_by_default(fake_agent_tools, tmp_path):
    from riglib.catalog import Catalog

    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = {"version": 1, "agent_tools_source": str(fake_agent_tools)}
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_loaded(cfg, repo), cat, project_type="cli")
    acts = [a for a in plan.actions if a.kind == "provision_ship_delegator"]
    assert len(acts) == 1
    assert acts[0].options.get("canonical_ship", "").endswith("ci/ship/ship.sh")


def test_plan_opt_out(fake_agent_tools, tmp_path):
    from riglib.catalog import Catalog

    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = {
        "version": 1,
        "agent_tools_source": str(fake_agent_tools),
        "ship_delegator": {"enabled": False},
    }
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_loaded(cfg, repo), cat, project_type="cli")
    assert not [a for a in plan.actions if a.kind == "provision_ship_delegator"]


def test_plan_skips_when_no_canonical_ship_in_checkout(fake_agent_tools, tmp_path):
    import shutil

    from riglib.catalog import Catalog

    # a checkout lacking ci/ship/ship.sh → no delegator action, with a note (fail-closed, no
    # broken delegator pointing at a non-existent canonical script). Work on a COPY of the fixture
    # tree so the unlink never mutates the shared fixture (it is function-scoped today, but a copy
    # keeps this test self-contained regardless of any future scope change).
    src = tmp_path / "at-copy"
    shutil.copytree(fake_agent_tools, src)
    (src / "ci" / "ship" / "ship.sh").unlink()
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = {"version": 1, "agent_tools_source": str(src)}
    cat = Catalog.scan(str(src))
    plan = build(_loaded(cfg, repo), cat, project_type="cli")
    assert not [a for a in plan.actions if a.kind == "provision_ship_delegator"]
    assert any("ship_delegator" in n for n in plan.notes)


# ── config validation ───────────────────────────────────────────────────────────────
