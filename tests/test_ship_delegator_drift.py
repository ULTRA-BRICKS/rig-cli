"""`gh ship` delegator provisioning tests: drift area (see test_ship_delegator_content for overview)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from riglib.actions.runner import run_plan
from riglib.actions.ship_delegator import (
    _is_under_system_temp,
    env_file_pins_transient_root,
    resolve_ship_delegator,
    ship_delegator_content,
    ship_env_file_content,
    ship_env_file_path,
    transient_ship_root_skip_reason,
)
from riglib.config import ConfigError, validate
from riglib.drift import detect
from riglib.actions import ship_delegator as ship_delegator_mod
from tests.ship_delegator_helpers import (
    _apply,
    _canonical_ship,
    _delegator_path,
    _exclude_path,
    _git_repo,
    _plan_with_action,
    _transient_setup,
)

def test_validate_rejects_non_mapping():
    with pytest.raises(ConfigError):
        validate({"version": 1, "ship_delegator": "yes"})


def test_validate_rejects_unknown_key():
    with pytest.raises(ConfigError):
        validate({"version": 1, "ship_delegator": {"nope": True}})


def test_validate_rejects_non_bool_enabled():
    with pytest.raises(ConfigError):
        validate({"version": 1, "ship_delegator": {"enabled": "yes"}})


def test_validate_accepts_enabled_bool():
    validate({"version": 1, "ship_delegator": {"enabled": False}})


def test_validate_rejects_top_level_null_block():
    # `ship_delegator: ~` at the top level → None, which is not a mapping → ConfigError (consistent
    # with the non-mapping guard; an absent KEY is the way to take the default, not an explicit null).
    with pytest.raises(ConfigError):
        validate({"version": 1, "ship_delegator": None})


# ── drift parity ────────────────────────────────────────────────────────────────────
def test_drift_missing_delegator(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    report = detect(_plan_with_action(repo, canonical))
    items = [i for i in report.items if i.category == "ship_delegator" and i.item == "delegator"]
    assert items and items[0].direction == "missing"


def test_drift_clean_after_apply(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)
    report = detect(_plan_with_action(repo, canonical))
    assert not [i for i in report.items if i.category == "ship_delegator"]


def test_drift_modified_delegator(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)
    _delegator_path(repo).write_text("#!/usr/bin/env bash\n# tampered\n", encoding="utf-8")
    report = detect(_plan_with_action(repo, canonical))
    items = [i for i in report.items if i.category == "ship_delegator"]
    assert items and items[0].direction == "modified"


def test_drift_missing_ignore_entry(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)
    # remove the exclude entry → drift should flag the unignored delegator
    _exclude_path(repo).write_text("", encoding="utf-8")
    report = detect(_plan_with_action(repo, canonical))
    items = [i for i in report.items if i.category == "ship_delegator"]
    assert items and items[0].direction == "missing"


# ── io_error: a directory / unreadable file at the delegator path ───────────────────
def test_io_error_directory_at_delegator_path(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    # a DIRECTORY where the delegator file should be → io_error (apply errors, drift modified).
    deleg = _delegator_path(repo)
    deleg.mkdir(parents=True)
    assert resolve_ship_delegator(repo).state == "io_error"
    res = _apply(repo, canonical)
    assert res.status == "error"
    assert "not a regular file" in res.detail
    report = detect(_plan_with_action(repo, canonical))
    items = [i for i in report.items if i.category == "ship_delegator"]
    assert items and items[0].direction == "modified"


# ── exclude entry handling: unbalanced markers, multi-pair collapse, no spurious backup ─
def test_unbalanced_exclude_marker_is_not_treated_as_present(tmp_path):
    # a begin marker with NO end is malformed; _reconcile_ship_exclude refuses to touch it, so it
    # must report as drift (NOT a false "ok"), else status reads clean while the file is broken.
    from riglib.actions.ship_delegator import SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER

    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)  # provision a correct delegator first
    # now corrupt the exclude: a lone begin marker (no end)
    _exclude_path(repo).write_text(SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER + "\n", encoding="utf-8")
    r = resolve_ship_delegator(repo)
    assert r.state == "update" and not r.exclude_ok
    report = detect(_plan_with_action(repo, canonical))
    items = [i for i in report.items if i.category == "ship_delegator"]
    assert items and items[0].direction == "missing"


def test_duplicate_exclude_blocks_collapse_to_one(tmp_path):
    from riglib.actions.ship_delegator import ship_delegator_exclude_block_text

    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)
    block = ship_delegator_exclude_block_text()
    excl = _exclude_path(repo)
    # a hand-added SECOND identical block + a user line between them
    excl.write_text(f"{block}\nkeep-me.txt\n{block}\n", encoding="utf-8")
    # re-apply collapses to exactly one block, preserving the user line
    _apply(repo, canonical)
    after = excl.read_text(encoding="utf-8")
    assert after.count("# >>> rig-managed ship delegator (do not edit) >>>") == 1
    assert "keep-me.txt" in after
    # and the resolution now reads clean (balanced, single block)
    assert resolve_ship_delegator(repo).state == "ok"


def test_mixed_correct_and_wrong_duplicate_blocks_collapse_to_one(tmp_path):
    # two managed blocks where ONE is correct and the other has a wrong body, with user content
    # before/between/after. Reconcile must collapse to exactly one CANONICAL block and preserve every
    # user line verbatim (the splice cursor/newline logic is where an off-by-one would land).
    from riglib.actions.ship_delegator import (
        SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER,
        SHIP_DELEGATOR_EXCLUDE_END_MARKER,
        ship_delegator_exclude_block_text,
    )

    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)
    excl = _exclude_path(repo)
    good = ship_delegator_exclude_block_text()
    wrong = f"{SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER}\n# WRONG\n/nope\n{SHIP_DELEGATOR_EXCLUDE_END_MARKER}"
    excl.write_text(f"# head\n{good}\nmid-user-line\n{wrong}\n# tail\n", encoding="utf-8")
    _apply(repo, canonical)
    after = excl.read_text(encoding="utf-8")
    assert after.count(SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER) == 1  # collapsed to one block
    assert "/nope" not in after and "# WRONG" not in after  # the wrong block is gone
    assert good in after  # the surviving block is canonical
    for line in ("# head", "mid-user-line", "# tail"):
        assert line in after, f"user line {line!r} not preserved"
    assert resolve_ship_delegator(repo).state == "ok"


def test_exclude_write_is_atomic_no_partial_on_failure(tmp_path, monkeypatch):
    # if the atomic rename fails mid-reconcile, the pre-existing user content must survive intact
    # (no truncated/empty file) — the whole point of the temp-file + os.replace dance.
    from riglib.actions import ship_delegator as ship_delegator_mod
    import os

    repo = _git_repo(tmp_path / "repo")
    excl = _exclude_path(repo)
    excl.parent.mkdir(parents=True, exist_ok=True)
    excl.write_text("# precious user content\n*.secret\n", encoding="utf-8")
    original = excl.read_text(encoding="utf-8")

    real_replace = os.replace

    def _boom(src, dst, *a, **k):
        if str(dst) == str(excl):
            raise OSError("simulated rename failure")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", _boom)
    ok, note = ship_delegator_mod._reconcile_ship_exclude(excl)
    assert ok is False
    # the original content is untouched (no partial write clobbered it)
    assert excl.read_text(encoding="utf-8") == original
    # no leftover temp files
    assert list(excl.parent.glob("*.rig-tmp")) == []


def test_single_block_with_wrong_body_is_reconciled(tmp_path):
    # a single managed block whose BODY was hand-edited (wrong comment) must be detected as drift and
    # rewritten in place to the canonical block (the common real-world drift, distinct from dupes).
    from riglib.actions.ship_delegator import (
        SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER,
        SHIP_DELEGATOR_EXCLUDE_END_MARKER,
        ship_delegator_exclude_block_text,
    )

    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)
    excl = _exclude_path(repo)
    # tamper the block body (a wrong comment + wrong path inside correct, balanced markers)
    excl.write_text(
        f"# top\n{SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER}\n# tampered\n/wrong/path\n"
        f"{SHIP_DELEGATOR_EXCLUDE_END_MARKER}\n# bottom\n",
        encoding="utf-8",
    )
    assert not resolve_ship_delegator(repo).exclude_ok  # drift
    _apply(repo, canonical)
    after = excl.read_text(encoding="utf-8")
    assert ship_delegator_exclude_block_text() in after  # canonical block restored
    assert "/wrong/path" not in after  # tampered body gone
    assert "# top" in after and "# bottom" in after  # surrounding user content preserved
    assert resolve_ship_delegator(repo).state == "ok"


def test_unreadable_delegator_file_is_io_error(tmp_path):
    # a delegator file rig cannot READ (mode 000) → io_error (apply errors, drift modified). Skipped
    # when running as root (root bypasses the permission bit, so the read would succeed).
    if os.geteuid() == 0:
        pytest.skip("root bypasses file permissions; the unreadable-file branch can't be exercised")
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    deleg = _delegator_path(repo)
    deleg.parent.mkdir(parents=True, exist_ok=True)
    deleg.write_text("#!/usr/bin/env bash\n# x\n", encoding="utf-8")
    deleg.chmod(0o000)
    try:
        assert resolve_ship_delegator(repo).state == "io_error"
        res = _apply(repo, canonical)
        assert res.status == "error"
        report = detect(_plan_with_action(repo, canonical))
        items = [i for i in report.items if i.category == "ship_delegator"]
        assert items and items[0].direction == "modified"
    finally:
        deleg.chmod(0o644)  # so tmp cleanup can remove it


def test_no_spurious_backup_when_only_exclude_entry_is_missing(tmp_path):
    # the delegator FILE is already correct; only the exclude entry is gone. apply must NOT rewrite
    # the (correct) file and must NOT create a *.rig-bak-* backup of it — only add the exclude.
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)
    _exclude_path(repo).write_text("", encoding="utf-8")  # drop the ignore entry, keep the file
    r = resolve_ship_delegator(repo)
    assert r.state == "update" and r.file_correct and not r.exclude_ok
    res = _apply(repo, canonical)
    assert res.status == "updated" and res.backup is None
    # no backup file was created next to the delegator
    backups = list(_delegator_path(repo).parent.glob("*.rig-bak-*"))
    assert backups == []
    # the exclude entry is back; now in sync
    assert resolve_ship_delegator(repo).state == "ok"


def test_skip_conflict_still_ignores_the_left_file_and_keeps_its_mode(tmp_path):
    # on_conflict=skip leaves a hand-edited delegator AS-IS (content AND mode untouched), but the
    # rig-owned exclude is still reconciled so the left file is ignored (status won't re-flag a
    # non-ignored delegator).
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    deleg = _delegator_path(repo)
    deleg.parent.mkdir(parents=True, exist_ok=True)
    deleg.write_text("#!/usr/bin/env bash\n# hand-edited, keep me\n", encoding="utf-8")
    deleg.chmod(0o644)  # NON-executable on purpose
    res = _apply(repo, canonical, on_conflict="skip")
    assert res.status == "skipped"
    # file content untouched
    assert "hand-edited" in deleg.read_text(encoding="utf-8")
    # mode untouched — rig must NOT chmod +x a user file it declined to write
    assert deleg.stat().st_mode & 0o111 == 0
    # but the exclude entry was added regardless
    assert ".claude/scripts/pr-ship.sh" in _exclude_path(repo).read_text(encoding="utf-8")


def test_overwrite_conflict_replaces_without_backup(tmp_path):
    # on_conflict=overwrite replaces a hand-edited delegator with the canonical one, NO backup kept.
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    deleg = _delegator_path(repo)
    deleg.parent.mkdir(parents=True, exist_ok=True)
    deleg.write_text("#!/usr/bin/env bash\n# stale\n", encoding="utf-8")
    res = _apply(repo, canonical, on_conflict="overwrite")
    assert res.status == "updated" and res.backup is None
    assert deleg.read_text(encoding="utf-8") == ship_delegator_content()
    assert list(deleg.parent.glob("*.rig-bak-*")) == []
    assert resolve_ship_delegator(repo).state == "ok"


def test_apply_errors_when_exclude_unwritable(tmp_path, monkeypatch):
    # if .git/info/exclude cannot be written, the delegator is on disk but NOT ignored → the action
    # must report ERROR (not a misleading "created"), because the worktree would be dirtied.
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)


    monkeypatch.setattr(ship_delegator_mod, "_reconcile_ship_exclude", lambda p: (False, f"could not write {p}: boom"))
    res = _apply(repo, canonical)
    assert res.status == "error"
    assert "NOT git-ignored" in res.detail


def test_drift_reports_both_modified_file_and_missing_ignore(tmp_path):
    # when BOTH the file content differs AND the ignore entry is gone, status must show BOTH issues.
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)
    _delegator_path(repo).write_text("#!/usr/bin/env bash\n# tampered\n", encoding="utf-8")
    _exclude_path(repo).write_text("", encoding="utf-8")  # drop the ignore entry too
    report = detect(_plan_with_action(repo, canonical))
    items = [i for i in report.items if i.category == "ship_delegator"]
    dirs = sorted(i.direction for i in items)
    assert dirs == ["missing", "modified"], f"expected both, got {[(i.direction, i.item) for i in items]}"


def test_exclude_preserves_user_content_around_the_block(tmp_path):
    # user lines BEFORE and AFTER the managed block must survive a reconcile (splice-cursor regression).
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    excl = _exclude_path(repo)
    excl.parent.mkdir(parents=True, exist_ok=True)
    excl.write_text("# my own ignore\n*.log\n", encoding="utf-8")
    _apply(repo, canonical)
    after = excl.read_text(encoding="utf-8")
    assert "# my own ignore" in after and "*.log" in after
    assert ".claude/scripts/pr-ship.sh" in after
    # idempotent: a re-apply preserves it all and adds nothing
    _apply(repo, canonical)
    assert excl.read_text(encoding="utf-8") == after


# ── default-on validation: {} and {enabled: true} are accepted (the production defaults) ─
def test_validate_accepts_empty_block_and_enabled_true():
    validate({"version": 1, "ship_delegator": {}})
    validate({"version": 1, "ship_delegator": {"enabled": True}})
    validate({"version": 1})  # absent block → default ON


def test_validate_rejects_enabled_null():
    # YAML `enabled: ~` → None. We REJECT it (the JSON Schema declares boolean; null is invalid
    # there) — to take the default, OMIT the key rather than set it to null.
    with pytest.raises(ConfigError):
        validate({"version": 1, "ship_delegator": {"enabled": None}})


def test_misordered_exclude_markers_reported_as_drift_not_rewritten(tmp_path):
    # an end marker BEFORE a begin marker is misordered; reconcile refuses it and it reads as drift.
    from riglib.actions.ship_delegator import (
        SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER,
        SHIP_DELEGATOR_EXCLUDE_END_MARKER,
        _reconcile_ship_exclude,
    )

    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)
    excl = _exclude_path(repo)
    excl.write_text(
        f"{SHIP_DELEGATOR_EXCLUDE_END_MARKER}\n{SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER}\n", encoding="utf-8"
    )
    # not in sync (drift)
    r = resolve_ship_delegator(repo)
    assert not r.exclude_ok
    # reconcile refuses (ok=False) and leaves the file untouched
    before = excl.read_text(encoding="utf-8")
    ok, note = _reconcile_ship_exclude(excl)
    assert ok is False and "misordered" in note
    assert excl.read_text(encoding="utf-8") == before


# ── full plan round-trip via run_plan ───────────────────────────────────────────────
def test_run_plan_provisions_and_is_idempotent(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    plan = _plan_with_action(repo, canonical)
    plan.on_conflict = "backup"
    report = run_plan(plan)
    assert all(r.status != "error" for r in report.results)
    assert _delegator_path(repo).is_file()
    # second run is a no-op
    report2 = run_plan(plan)
    deleg = [r for r in report2.results if r.action.kind == "provision_ship_delegator"]
    assert deleg and deleg[0].status == "skipped"


# ── transient (temp-dir) agent-tools root must never clobber the durable machine pointer ──────
def test_transient_root_skip_reason_fires_only_for_persistent_pointer(tmp_path, monkeypatch):
    # transient root + persistent pointer → a skip reason; a durable-to-durable apply → None.
    canonical, _ = _transient_setup(tmp_path, monkeypatch)
    reason = transient_ship_root_skip_reason(canonical)
    assert reason is not None and "transient" in reason
    # a canonical OUTSIDE the (monkeypatched) temp set is durable → no skip
    durable = tmp_path / "xp" / "agent-tools" / "ci" / "ship" / "ship.sh"
    durable.parent.mkdir(parents=True, exist_ok=True)
    durable.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    assert transient_ship_root_skip_reason(durable) is None


def test_both_transient_writes_normally(tmp_path):
    # Default isolation: env file (XDG under tmp_path/.config via conftest) AND the source are both
    # under the real tempdir → self-consistent, the guard must NOT fire, the write proceeds. This is
    # the pytest/cleanroom-under-$TMPDIR path — proving the guard doesn't break ordinary applies.
    canonical = _canonical_ship(tmp_path)
    assert _is_under_system_temp(ship_env_file_path())  # conftest pins XDG under tmp
    assert transient_ship_root_skip_reason(canonical) is None
    repo = _git_repo(tmp_path / "repo")
    res = _apply(repo, canonical)
    assert res.status in ("created", "updated")
    assert ship_env_file_path().read_text(encoding="utf-8") == ship_env_file_content(canonical)


def test_transient_apply_leaves_existing_good_pointer_intact(tmp_path, monkeypatch):
    # A one-off apply from a temp checkout must NOT overwrite a good machine pointer with a doomed
    # temp path (the recurring `gh ship` breakage). apply skips the write (non-error) and the
    # existing durable pointer survives byte-for-byte.
    canonical, env_path = _transient_setup(tmp_path, monkeypatch)
    good = tmp_path / "xp" / "agent-tools" / "ci" / "ship" / "ship.sh"
    good.parent.mkdir(parents=True, exist_ok=True)
    good_content = ship_env_file_content(good)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(good_content, encoding="utf-8")
    repo = _git_repo(tmp_path / "repo")
    res = _apply(repo, canonical)
    assert res.status != "error"
    assert env_path.read_text(encoding="utf-8") == good_content  # doomed path NOT written
    # the refusal is SURFACED (not a silent success): the operator sees why the pointer was left.
    assert "transient" in res.detail


def test_transient_apply_does_not_create_a_doomed_pointer(tmp_path, monkeypatch):
    # With NO existing pointer, applying from a temp source must not CREATE one at the temp path —
    # absent is strictly better than a path that vanishes and masks the misconfig. The refusal note
    # is surfaced so a `gh ship` with no resolvable root here is visible, not silent.
    canonical, env_path = _transient_setup(tmp_path, monkeypatch)
    repo = _git_repo(tmp_path / "repo")
    res = _apply(repo, canonical)
    assert res.status != "error"
    assert not env_path.exists()
    assert "transient" in res.detail


def test_transient_drift_does_not_flag_env(tmp_path, monkeypatch):
    # status/apply parity: apply skips the transient write, so drift must NOT report ship_env drift
    # (a change apply would refuse to make) — mirrors the self-hosting skip.
    canonical, _ = _transient_setup(tmp_path, monkeypatch)
    repo = _git_repo(tmp_path / "repo")
    report = detect(_plan_with_action(repo, canonical))
    assert not [i for i in report.items if i.category == "ship_env"]


def test_transient_does_not_hide_a_broken_env_file(tmp_path, monkeypatch):
    # A GENUINELY broken machine env file (a symlink/non-file at the path) must STILL surface even
    # under a transient source — apply errors and drift flags it, not "clean". The structural check
    # runs before the transient skip in BOTH, so status/apply agree (codex finding: don't let the
    # transient skip mask a broken pointer the delegator can't source).
    canonical, env_path = _transient_setup(tmp_path, monkeypatch)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.symlink_to(tmp_path / "nowhere")  # dangling symlink = non-file
    repo = _git_repo(tmp_path / "repo")
    res = _apply(repo, canonical)
    assert res.status == "error"
    assert env_path.is_symlink()  # not silently replaced
    report = detect(_plan_with_action(repo, canonical))
    items = [i for i in report.items if i.category == "ship_env" and i.item == "env-file"]
    assert items and items[0].direction == "modified"


def test_already_poisoned_pointer_is_not_hidden_by_transient_skip(tmp_path, monkeypatch):
    # The bug's AFTERMATH: the env file ALREADY pins a vanished temp checkout. A transient-source
    # run must NOT hide that (status would say clean while `gh ship` sources the dead path and exits
    # 127). Drift keeps flagging it (a durable apply repairs it); apply skips-with-note and does NOT
    # clobber it with yet another temp path — same shape as skipped_user.
    canonical, env_path = _transient_setup(tmp_path, monkeypatch)  # current source = faketmp/agent-tools
    stale = tmp_path / "faketmp" / "old-agent-tools" / "ci" / "ship" / "ship.sh"  # a DIFFERENT temp root
    stale_content = ship_env_file_content(stale)
    assert env_file_pins_transient_root(stale_content) is True
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(stale_content, encoding="utf-8")
    # drift still flags the poisoned pointer
    repo = _git_repo(tmp_path / "repo")
    report = detect(_plan_with_action(repo, canonical))
    items = [i for i in report.items if i.category == "ship_env" and i.item == "env-file"]
    assert items and items[0].direction == "modified"
    # apply skips-with-note and leaves the stale content as-is (no clobber with a new temp path)
    res = _apply(repo, canonical)
    assert res.status != "error" and "transient" in res.detail
    assert env_path.read_text(encoding="utf-8") == stale_content
    # a DURABLE-existing pointer is NOT treated as poisoned (it is the good case we protect)
    durable = tmp_path / "xp" / "agent-tools" / "ci" / "ship" / "ship.sh"
    assert env_file_pins_transient_root(ship_env_file_content(durable)) is False


def test_env_file_pins_transient_root_detects_export_prefixed_pointer(tmp_path, monkeypatch):
    # The delegator `source`s the machine env file, so `export AGENT_TOOLS_ROOT=/tmp/...` is a
    # shell-honored assignment. The poisoned-pointer detector must recognize that form too — else
    # an EXPORTED transient pointer slips past the drift surfacing and `gh ship` sources a dead
    # temp path (exit 127) while status reports clean (codex P2 on #128).
    _transient_setup(tmp_path, monkeypatch)  # monkeypatches the temp-root set to tmp_path/faketmp
    stale = tmp_path / "faketmp" / "old-agent-tools" / "ci" / "ship" / "ship.sh"
    assert env_file_pins_transient_root(f"export AGENT_TOOLS_ROOT='{stale}'\n") is True
    assert env_file_pins_transient_root(f"export   AGENT_TOOLS_ROOT='{stale}'\n") is True
    # a durable exported pointer stays the good (non-poisoned) case
    durable = tmp_path / "xp" / "agent-tools" / "ci" / "ship" / "ship.sh"
    assert env_file_pins_transient_root(f"export AGENT_TOOLS_ROOT='{durable}'\n") is False


def test_poisoned_pointer_flagged_even_when_it_equals_the_transient_source(tmp_path, monkeypatch):
    # Edge: the existing poisoned pointer pins the SAME transient root as the current plan's source,
    # so env_current == desired. The content-equality check would say "clean" — but the pointer is
    # STILL a temp path that vanishes, so status must flag it regardless of equality.
    canonical, env_path = _transient_setup(tmp_path, monkeypatch)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(ship_env_file_content(canonical), encoding="utf-8")  # == desired, both transient
    repo = _git_repo(tmp_path / "repo")
    report = detect(_plan_with_action(repo, canonical))
    items = [i for i in report.items if i.category == "ship_env" and i.item == "env-file"]
    assert items and items[0].direction == "modified"


def test_transient_env_skip_reports_skipped_even_when_delegator_written(tmp_path, monkeypatch):
    # When the delegator file itself is freshly WRITTEN (r.state != "ok") but the machine env write
    # was skipped for a transient source, the action must report "skipped" (not "created"/"updated")
    # so a left-as-is / poisoned pointer never vanishes into a success — same as skipped_user.
    canonical, env_path = _transient_setup(tmp_path, monkeypatch)
    repo = _git_repo(tmp_path / "repo")  # no pre-existing delegator → r.state == "create"
    assert not _delegator_path(repo).exists()
    res = _apply(repo, canonical)
    assert _delegator_path(repo).is_file()  # delegator WAS written
    assert res.status == "skipped"  # but the action is skipped, not created
    assert "transient" in res.detail


def test_is_under_system_temp_survives_gettempdir_raising(tmp_path, monkeypatch):
    # In a locked-down env `tempfile.gettempdir()` can RAISE; _is_under_system_temp must not crash —
    # it falls back to the hard-coded roots. A /tmp path still reads as temp; an arbitrary one not.
    import tempfile as _tf

    def _boom():
        raise FileNotFoundError("No usable temporary directory")

    monkeypatch.setattr(_tf, "gettempdir", _boom)
    monkeypatch.delenv("TMPDIR", raising=False)
    assert _is_under_system_temp(Path("/tmp/x/agent-tools")) is True
    assert _is_under_system_temp(Path("/opt/agent-tools")) is False
