"""Tests for the Spotlight-exclude provisioner (sweep + launchd re-sweep agent + plan wiring)."""

from __future__ import annotations

from pathlib import Path

from riglib import spotlight
from riglib.actions.runner import _do_provision_spotlight
from riglib.plan import Action


def _make_tree(root: Path) -> None:
    """A project tree with dependency dirs at several depths + a nested node_modules."""
    (root / "proj/node_modules/pkg").mkdir(parents=True)
    (root / "proj/dist").mkdir(parents=True)
    (root / "proj/src").mkdir(parents=True)
    (root / "proj/packages/inner/node_modules").mkdir(parents=True)
    (root / "api/target/debug").mkdir(parents=True)
    (root / "api/.venv/lib").mkdir(parents=True)
    (root / "api/app").mkdir(parents=True)


def test_iter_target_dirs_finds_and_prunes(tmp_path):
    root = tmp_path / "work"
    _make_tree(root)
    found = spotlight.iter_target_dirs((root,), frozenset(spotlight.DEFAULT_DENY))
    names = sorted(p.name for p in found)
    # matched: two node_modules (top + nested), dist, target, .venv
    assert names == [".venv", "dist", "node_modules", "node_modules", "target"]
    # pruning: nothing UNDER a matched node_modules is yielded (pkg/ is not a match anyway, but
    # the walk must not have descended — assert no path contains node_modules/pkg style descent).
    assert not any("node_modules" in str(p.parent) for p in found)


def test_perform_sweep_drops_sentinels_idempotent(tmp_path):
    root = tmp_path / "xp"
    _make_tree(root)
    deny = frozenset(spotlight.DEFAULT_DENY)
    res1 = spotlight.perform_sweep((root,), deny)
    assert res1.matched == 5
    assert len(res1.created) == 5 and len(res1.existing) == 0
    for sentinel in res1.created:
        assert sentinel.name == spotlight.SENTINEL_NAME
        assert sentinel.is_file()
    # re-sweep is a no-op: everything already covered.
    res2 = spotlight.perform_sweep((root,), deny)
    assert len(res2.created) == 0 and len(res2.existing) == 5


def test_perform_sweep_records_missing_root(tmp_path):
    present = tmp_path / "work"
    (present / "proj/node_modules").mkdir(parents=True)
    absent = tmp_path / "nope"
    res = spotlight.perform_sweep((present, absent), frozenset(spotlight.DEFAULT_DENY))
    assert present in res.roots_scanned
    assert absent in res.roots_missing


def test_resolve_deny_replace_and_extra():
    assert spotlight.resolve_deny(["a", "b"], None) == frozenset({"a", "b"})  # replace
    got = spotlight.resolve_deny(None, ["zz"])
    assert "zz" in got and "node_modules" in got  # extra adds to default
    assert spotlight.resolve_deny(None, None) == frozenset(spotlight.DEFAULT_DENY)


def test_plist_xml_has_runatload_and_logging():
    plan = spotlight.build_spotlight(
        roots=(Path("/x"),), deny=frozenset({"node_modules"}),
        sweep_cmd=("/usr/bin/python3", "-m", "riglib", "spotlight-sweep"),
    )
    xml = plan.plist_xml()
    assert "<key>RunAtLoad</key>" in xml and "<true/>" in xml
    assert "StandardOutPath" in xml and "StandardErrorPath" in xml
    assert "StartCalendarInterval" in xml
    assert "spotlight-sweep" in xml


def test_do_provision_spotlight_sweeps_and_writes_plist(tmp_path, monkeypatch):
    root = tmp_path / "work"
    _make_tree(root)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("RIG_SPOTLIGHT_DRY_RUN", "1")  # write the plist, skip live launchctl
    action = Action(
        kind="provision_spotlight", category="spotlight", item="exclude",
        source=tmp_path, target=Path("ai.hyperide.spotlight-exclude"),
        options={
            "roots": [str(root)],
            "deny": sorted(spotlight.DEFAULT_DENY),
            "label": "ai.hyperide.spotlight-exclude",
            "max_depth": 8,
            "sweep_cmd": ["/usr/bin/python3", "-m", "riglib", "spotlight-sweep"],
        },
    )
    res = _do_provision_spotlight(action, "backup")
    assert res.ok
    # sentinels dropped
    assert (root / "proj/node_modules/.metadata_never_index").is_file()
    assert (root / "api/target/.metadata_never_index").is_file()
    # plist written (non-darwin hosts skip the plist — guard the assertion)
    import sys

    if sys.platform == "darwin":
        plist = home / "Library/LaunchAgents/ai.hyperide.spotlight-exclude.plist"
        assert plist.is_file()
        assert "spotlight-sweep" in plist.read_text()


def test_plan_emits_spotlight_action_only_when_enabled(tmp_path):
    from riglib.config import LoadedConfig
    from riglib.plan import _build_spotlight, InstallPlan

    def _cfg(data):
        return LoadedConfig(data=data, repo_root=tmp_path)

    plan = InstallPlan()
    _build_spotlight(_cfg({}), plan)  # absent block → no action
    assert not plan.actions
    plan = InstallPlan()
    _build_spotlight(_cfg({"spotlight": {"enabled": False}}), plan)  # disabled → no action
    assert not plan.actions
    plan = InstallPlan()
    _build_spotlight(_cfg({"spotlight": {}}), plan)  # present-but-empty → default OFF, no action
    assert not plan.actions
    plan = InstallPlan()
    _build_spotlight(_cfg({"spotlight": {"enabled": True, "roots": ["~/w"], "extra": ["foo"]}}), plan)
    assert len(plan.actions) == 1
    act = plan.actions[0]
    assert act.kind == "provision_spotlight"
    assert "foo" in act.options["deny"]


def test_do_provision_spotlight_launchctl_load_failure_is_error(tmp_path, monkeypatch, live_launchd_home):
    import sys
    if sys.platform != "darwin":
        import pytest
        pytest.skip("launchd branch is macOS-only")
    from riglib.actions import runner

    root = tmp_path / "work"
    (root / "proj/node_modules").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("RIG_SPOTLIGHT_DRY_RUN", raising=False)  # exercise the real load path
    monkeypatch.setattr(runner, "_launchctl", lambda verb, arg: 0 if verb == "unload" else 1)
    monkeypatch.setattr(runner, "_launchctl_loaded", lambda label: False)
    action = Action(
        kind="provision_spotlight", category="spotlight", item="exclude",
        source=tmp_path, target=Path("ai.hyperide.spotlight-exclude"),
        options={"roots": [str(root)], "deny": sorted(spotlight.DEFAULT_DENY),
                 "label": "ai.hyperide.spotlight-exclude", "max_depth": 8,
                 "sweep_cmd": ["/usr/bin/python3", "-m", "riglib", "spotlight-sweep"]},
    )
    res = _do_provision_spotlight(action, "backup")
    assert res.status == "error"
    assert "launchctl load` failed" in res.detail
    # sentinel still dropped despite the load failure (the sweep ran first).
    assert (root / "proj/node_modules/.metadata_never_index").is_file()


def _spotlight_action(root: Path, label: str = "ai.hyperide.spotlight-exclude") -> Action:
    return Action(
        kind="provision_spotlight", category="spotlight", item="exclude",
        source=root, target=Path(label),
        options={
            "roots": [str(root)],
            "deny": sorted(spotlight.DEFAULT_DENY),
            "label": label,
            "max_depth": 8,
            "sweep_cmd": ["/usr/bin/python3", "-m", "riglib", "spotlight-sweep"],
        },
    )


def _apply_spotlight(root: Path, home: Path, monkeypatch) -> Action:
    """Apply the provisioner (dry-run launchd) so sentinels + plist land, and return the action."""
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("RIG_SPOTLIGHT_DRY_RUN", "1")  # write plist, skip live launchctl
    action = _spotlight_action(root)
    _do_provision_spotlight(action, "backup")
    return action


def _spotlight_darwin_applied(tmp_path, monkeypatch, platform: str = "darwin") -> tuple[Action, Path, Path]:
    """Pin the platform, build a project tree, apply (sentinels + plist), pin ``Path.home``.

    The shared preamble of the spotlight drift tests — returns (action, root, home) so each test
    only writes its own perturbation + assertion. ``platform`` pins ``sys.platform`` at BOTH apply
    and detect time (``"linux"`` exercises the non-darwin no-plist path on every host).
    """
    import sys

    monkeypatch.setattr(sys, "platform", platform)
    root = tmp_path / "work"
    _make_tree(root)
    home = tmp_path / "home"
    home.mkdir()
    action = _apply_spotlight(root, home, monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return action, root, home


def _spotlight_drift(action: Action) -> list:
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    return [d for d in detect(InstallPlan(actions=[action])).items if d.category == "spotlight"]


def test_drift_spotlight_in_sync_after_apply(tmp_path, monkeypatch):
    """Right after apply, `rig status` sees NO spotlight drift (sentinels + plist match config)."""
    action, _root, _home = _spotlight_darwin_applied(tmp_path, monkeypatch)
    assert not _spotlight_drift(action)


def test_drift_spotlight_missing_sentinel(tmp_path, monkeypatch):
    """A matched dir that LOST its sentinel is drift: it predates the sweep, so it was covered once."""
    action, root, _home = _spotlight_darwin_applied(tmp_path, monkeypatch)
    spotlight.sentinel_path(root / "proj/node_modules").unlink()
    drift = _spotlight_drift(action)
    assert any(d.direction == "missing" and spotlight.SENTINEL_NAME in d.detail for d in drift)


def test_drift_spotlight_scans_full_set_not_just_sample(tmp_path, monkeypatch):
    """Drift must catch an uncovered project even when it sorts PAST the first 20 matched dirs.

    The post-apply verify step used to spot-check only the head of the matched list; reusing that
    bound for drift would silently miss an uncovered project whenever it lands beyond the sample.
    This pins the uncovered dir at the tail of a 25-dir list so a first-N slice would skip it.
    """
    action, root, _home = _spotlight_darwin_applied(tmp_path, monkeypatch)
    covered = []
    for i in range(25):
        d = root / f"covered{i:03d}/node_modules"
        d.mkdir(parents=True)
        spotlight.sentinel_path(d).touch()  # already covered
        covered.append(d)
    missed = root / "zzz_missed/node_modules"
    missed.mkdir(parents=True)  # the sweep should have covered this one and did not
    spotlight.record_sweep(_sel(root), 1000.0, [*covered, missed])
    # Deterministic order: the uncovered dir is LAST, beyond the first-N window the old code sampled.
    monkeypatch.setattr(spotlight, "iter_target_dirs", lambda *a, **k: [*covered, missed])
    drift = _spotlight_drift(action)
    assert any(d.direction == "missing" and spotlight.SENTINEL_NAME in d.detail for d in drift)


def test_drift_spotlight_missing_plist(tmp_path, monkeypatch):
    """The launchd re-sweep agent plist vanishing is drift (new projects would stop being covered)."""
    action, _root, home = _spotlight_darwin_applied(tmp_path, monkeypatch)
    plist = home / "Library/LaunchAgents/ai.hyperide.spotlight-exclude.plist"
    assert plist.is_file()
    plist.unlink()
    drift = _spotlight_drift(action)
    assert any(d.direction == "missing" and "plist" in d.detail for d in drift)


def test_drift_spotlight_modified_plist(tmp_path, monkeypatch):
    """A hand-edited re-sweep plist (drifted schedule/argv) is surfaced as modified."""
    action, _root, home = _spotlight_darwin_applied(tmp_path, monkeypatch)
    plist = home / "Library/LaunchAgents/ai.hyperide.spotlight-exclude.plist"
    plist.write_text("<plist>tampered</plist>", encoding="utf-8")
    drift = _spotlight_drift(action)
    assert any(d.direction == "modified" for d in drift)


def test_drift_spotlight_agent_not_loaded(tmp_path, monkeypatch, live_launchd_home):
    """With a correct plist on disk but the launchd agent NOT loaded (real load path, not dry-run),
    status flags 'not loaded' — the branch every other drift test short-circuits via dry-run."""
    from riglib import drift as driftmod

    action, _root, home = _spotlight_darwin_applied(tmp_path, monkeypatch)
    plist = home / "Library/LaunchAgents/ai.hyperide.spotlight-exclude.plist"
    assert plist.is_file()  # plist matches config; only the loaded-state differs
    monkeypatch.delenv("RIG_SPOTLIGHT_DRY_RUN", raising=False)  # exercise the live loaded-probe
    # explicit here (the autouse fixture already stubs this False) — this is the one test whose
    # branch depends on the loaded-probe outcome, so pin it locally rather than rely on the fixture.
    monkeypatch.setattr(driftmod, "_launchctl_loaded", lambda label: False)
    drift = _spotlight_drift(action)
    assert any(d.direction == "missing" and "not loaded" in d.detail for d in drift)


def test_drift_spotlight_modified_and_not_loaded_both_surface(tmp_path, monkeypatch, live_launchd_home):
    """A content-drifted plist whose agent is ALSO not loaded surfaces BOTH facts — the modified
    check must not short-circuit the (independent) loaded probe."""
    from riglib import drift as driftmod

    action, _root, home = _spotlight_darwin_applied(tmp_path, monkeypatch)
    plist = home / "Library/LaunchAgents/ai.hyperide.spotlight-exclude.plist"
    plist.write_text("<plist>tampered</plist>", encoding="utf-8")  # content drift
    monkeypatch.delenv("RIG_SPOTLIGHT_DRY_RUN", raising=False)  # exercise the live loaded-probe
    monkeypatch.setattr(driftmod, "_launchctl_loaded", lambda label: False)
    drift = _spotlight_drift(action)
    assert any(d.direction == "modified" for d in drift)
    assert any(d.direction == "missing" and "not loaded" in d.detail for d in drift)


def test_drift_spotlight_skipped_on_non_darwin(tmp_path, monkeypatch):
    """On a non-macOS host the provisioner writes no plist, so status must NOT flag a phantom one."""
    action, _root, _home = _spotlight_darwin_applied(tmp_path, monkeypatch, platform="linux")
    # sentinels are present (sweep is cross-platform), and there is no launchd plist to miss.
    assert not [d for d in _spotlight_drift(action) if "plist" in d.detail]


def test_iter_target_dirs_respects_max_depth_boundary(tmp_path):
    # a matched dir exactly AT the cap is excluded; one level shallower is found — guards the
    # off-by-one in `len(here.parts) - root_depth >= max_depth`.
    root = tmp_path / "work"
    (root / "a/node_modules").mkdir(parents=True)          # depth 1 under root (a/ then match)
    (root / "a/b/c/node_modules").mkdir(parents=True)      # deeper
    deny = frozenset({"node_modules"})
    shallow = spotlight.iter_target_dirs((root,), deny, max_depth=2)
    assert (root / "a/node_modules") in shallow
    assert (root / "a/b/c/node_modules") not in shallow    # beyond the depth-2 cap → pruned
    deep = spotlight.iter_target_dirs((root,), deny, max_depth=8)
    assert (root / "a/b/c/node_modules") in deep


def test_cmd_spotlight_sweep_reads_deny_from_config(tmp_path, monkeypatch, capsys):
    # the sweep command honors a custom `deny` from the merged config (not just `roots`).
    from riglib.cli import main

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    work = tmp_path / "work"
    (work / "proj/weirddir").mkdir(parents=True)
    (work / "proj/node_modules").mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = repo / "rig.yaml"
    cfg.write_text(
        f"version: 1\nspotlight:\n  enabled: true\n  roots:\n    - {work}\n"
        "  deny:\n    - weirddir\n",
        encoding="utf-8",
    )
    rc = main(["spotlight-sweep", "-C", str(repo), "--config", str(cfg)])
    assert rc == 0
    # custom deny took effect; the default node_modules is NOT swept (deny REPLACES the default).
    assert (work / "proj/weirddir/.metadata_never_index").is_file()
    assert not (work / "proj/node_modules/.metadata_never_index").exists()


def _sel(*roots: Path, deny=None, max_depth: int = spotlight.DEFAULT_MAX_DEPTH) -> spotlight.SweepSelector:
    """The selector every test here sweeps with: the given roots, the default denylist, depth 8."""
    return spotlight.SweepSelector(
        tuple(roots), frozenset(spotlight.DEFAULT_DENY) if deny is None else frozenset(deny), max_depth
    )


# ── coverage predicate: stale (the sweep visited it) vs pending (it appeared since) ────────
# The split is decided by SET MEMBERSHIP in the last sweep's recorded dir list, so every test pins
# that list explicitly (`record_sweep`). No filesystem timestamp is involved, which is why these
# behave identically on APFS, ext4 and any CI filesystem.


def test_scan_coverage_with_no_recorded_sweep_calls_everything_stale(tmp_path):
    """A machine that never swept has no excuse for an uncovered dir — all of them are real drift."""
    root = tmp_path / "work"
    _make_tree(root)
    deny = frozenset(spotlight.DEFAULT_DENY)
    cov = spotlight.scan_coverage(_sel(root), spotlight.last_sweep(_sel(root)))
    assert cov.total == 5 and cov.covered == 0
    assert len(cov.stale) == 5 and not cov.pending


def test_scan_coverage_splits_stale_from_pending(tmp_path):
    """A dir the last sweep never saw is pending; one it visited and left uncovered is stale."""
    root = tmp_path / "work"
    _make_tree(root)
    deny = frozenset(spotlight.DEFAULT_DENY)
    spotlight.perform_sweep((root,), deny)
    fresh = root / "fresh/node_modules"
    fresh.mkdir(parents=True)

    # the recorded sweep never visited `fresh` (it did not exist yet) → pending, not a failure
    cov = spotlight.scan_coverage(_sel(root), spotlight.last_sweep(_sel(root)))
    assert cov.total == 6 and cov.covered == 5
    assert list(cov.pending) == [fresh] and not cov.stale

    # a sweep that DID visit it and left it uncovered → the sweep missed it: stale
    spotlight.record_sweep(_sel(root), 1000.0, [fresh])
    cov = spotlight.scan_coverage(_sel(root), spotlight.last_sweep(_sel(root)))
    assert list(cov.stale) == [fresh] and not cov.pending


def test_hand_deleted_sentinel_is_stale_on_every_platform(tmp_path):
    """Deleting a sentinel out of a swept dir is drift — no filesystem timestamp is consulted.

    Dating the dir instead would excuse exactly this case wherever ``st_birthtime`` is missing:
    the unlink bumps the parent dir's ``st_ctime`` past the sweep, so a timestamp-based split
    would call a hand-deleted sentinel "created since the last sweep" and pass it.
    """
    root = tmp_path / "work"
    _make_tree(root)
    deny = frozenset(spotlight.DEFAULT_DENY)
    spotlight.perform_sweep((root,), deny)
    victim = root / "proj/node_modules"
    spotlight.sentinel_path(victim).unlink()
    cov = spotlight.scan_coverage(_sel(root), spotlight.last_sweep(_sel(root)))
    assert list(cov.stale) == [victim] and not cov.pending


def test_last_sweep_ignores_a_record_made_for_other_roots(tmp_path):
    """After a config change adds a root, the old record says nothing about it — treat as never."""
    a, b = tmp_path / "a", tmp_path / "b"
    spotlight.record_sweep(_sel(a), 1000.0, [])
    assert spotlight.last_sweep(_sel(a)).at == 1000.0
    assert spotlight.last_sweep(_sel(a, b)) is None


def test_last_sweep_ignores_a_record_made_with_another_denylist_or_depth(tmp_path):
    """A `.venv` newly added to `deny` (or a deeper walk) is uncovered because NOTHING swept it.

    Review round 1 (rig-cli#375) reproduced the excuse a roots-only key gives: change `deny`,
    skip `rig apply`, and every never-swept dir files as pending → `rig status` says in sync.
    The record is keyed on the whole selector, so that state reads as "never swept" instead.
    """
    root = tmp_path / "work"
    _make_tree(root)
    spotlight.perform_sweep((root,), frozenset({"node_modules"}))
    assert spotlight.last_sweep(_sel(root, deny={"node_modules"})) is not None
    assert spotlight.last_sweep(_sel(root, deny={"node_modules", "target"})) is None
    assert spotlight.last_sweep(_sel(root, deny={"node_modules"}, max_depth=9)) is None
    # and through the predicate: the unswept `target` dirs are STALE (drift), not pending
    cov = spotlight.scan_coverage(_sel(root), spotlight.last_sweep(_sel(root)))
    assert not cov.pending and cov.covered == 2 and len(cov.stale) == 3  # the 3 non-node_modules dirs


def test_last_sweep_is_insensitive_to_root_and_deny_order(tmp_path):
    """Reordering the configured roots or denylist changes nothing about what was swept."""
    a, b = tmp_path / "a", tmp_path / "b"
    spotlight.record_sweep(_sel(a, b, deny=["x", "y"]), 1000.0, [])
    assert spotlight.last_sweep(_sel(b, a, deny=["y", "x"])).at == 1000.0


def test_last_sweep_treats_a_corrupt_record_as_no_sweep(tmp_path):
    """A truncated / hand-edited record reads as "never swept" — strict, never permissive."""
    spotlight.record_sweep(_sel(tmp_path / "a"), 1000.0, [])
    spotlight.state_path().write_text('{"at": 1000.0, "roots": [', encoding="utf-8")
    assert spotlight.last_sweep(_sel(tmp_path / "a")) is None


def test_last_sweep_rejects_a_record_with_the_wrong_json_types(tmp_path):
    """Valid JSON of the wrong shape is not a record: a string is iterable too, and iterating its
    characters would mint a nonsense `seen` set that excuses real drift as pending."""
    import json

    sel = _sel(tmp_path / "a")
    good = {"at": 1000.0, **sel.fingerprint(), "seen": [str(tmp_path / "a/node_modules")]}
    spotlight.state_path().parent.mkdir(parents=True, exist_ok=True)
    spotlight.state_path().write_text(json.dumps(good), encoding="utf-8")
    assert spotlight.last_sweep(sel) is not None
    for field, bad in (("roots", "abc"), ("seen", "xyz"), ("deny", {"a": 1}), ("max_depth", "8"),
                       ("seen", [1, 2]), ("at", "soon")):
        spotlight.state_path().write_text(json.dumps({**good, field: bad}), encoding="utf-8")
        assert spotlight.last_sweep(sel) is None, field


def test_perform_sweep_records_when_it_ran_and_what_it_visited(tmp_path):
    """The record carries the start time (for the message) and every dir the walk visited."""
    import time

    root = tmp_path / "work"
    _make_tree(root)
    before = time.time()
    result = spotlight.perform_sweep((root,), frozenset(spotlight.DEFAULT_DENY))
    record = spotlight.last_sweep(_sel(root))
    assert record is not None and before - 1 <= record.at <= time.time()
    assert len(record.seen) == result.matched == 5
    assert str(root / "proj/node_modules") in record.seen


def test_apply_marks_every_unmarked_dir_and_a_second_apply_changes_nothing(tmp_path, monkeypatch):
    """`rig apply commit` covers EVERY unmarked dir (not a sample) and reports how many it wrote."""
    import sys

    monkeypatch.setattr(sys, "platform", "darwin")
    root = tmp_path / "work"
    for i in range(25):
        (root / f"p{i:03d}/node_modules").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("RIG_SPOTLIGHT_DRY_RUN", "1")
    action = _spotlight_action(root)

    first = _do_provision_spotlight(action, "backup")
    assert first.status == "created" and "25 new" in first.detail
    assert all(spotlight.has_sentinel(root / f"p{i:03d}/node_modules") for i in range(25))

    second = _do_provision_spotlight(action, "backup")
    assert second.status == "skipped" and "0 new" in second.detail


def test_status_agrees_with_apply_right_after_it(tmp_path, monkeypatch):
    """25 unmarked dirs → apply → `rig status` sees no spotlight drift (the parity acceptance)."""
    import sys

    monkeypatch.setattr(sys, "platform", "darwin")
    root = tmp_path / "work"
    for i in range(25):
        (root / f"p{i:03d}/node_modules").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    action = _apply_spotlight(root, home, monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    assert not _spotlight_drift(action)


def test_drift_names_the_stale_count(tmp_path, monkeypatch):
    """A dir the sweep should have covered but did not is drift, and the message names how many."""
    action, root, _home = _spotlight_darwin_applied(tmp_path, monkeypatch)
    missed = root / "zzz/node_modules"
    missed.mkdir(parents=True)
    record = spotlight.last_sweep(_sel(root))
    spotlight.record_sweep(_sel(root), record.at, [*(Path(p) for p in record.seen), missed])
    drift = _spotlight_drift(action)
    assert [d.direction for d in drift] == ["missing"]
    assert "1/6" in drift[0].detail and spotlight.SENTINEL_NAME in drift[0].detail


def test_drift_reports_a_dir_the_sweep_never_saw_as_a_note_not_drift(tmp_path, monkeypatch):
    """A cache dir created since the last sweep is EXPECTED — informational, never a red status."""
    action, root, _home = _spotlight_darwin_applied(tmp_path, monkeypatch)
    (root / "fresh/node_modules").mkdir(parents=True)
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    report = detect(InstallPlan(actions=[action]))
    assert not [d for d in report.items if d.category == "spotlight"]
    notes = [n for n in report.notes if n.category == "spotlight"]
    assert len(notes) == 1 and "1 of 6" in notes[0].detail and "since the last sweep" in notes[0].detail
    # the age of the last sweep is what exposes a loaded-but-failing re-sweep agent
    assert "(just now)" in notes[0].detail


def test_describe_age_is_coarse_and_never_negative():
    assert spotlight.describe_age(1000.0, now=1000.0) == "just now"
    assert spotlight.describe_age(1000.0, now=1000.0 + 59) == "just now"
    assert spotlight.describe_age(1000.0, now=1000.0 + 61) == "1 minute ago"
    assert spotlight.describe_age(1000.0, now=1000.0 + 3 * 3600 + 5) == "3 hours ago"
    assert spotlight.describe_age(1000.0, now=1000.0 + 2 * 86400) == "2 days ago"
    assert spotlight.describe_age(2000.0, now=1000.0) == "just now"  # clock went backwards


# ── the re-sweep agent's argv must work for the install shape actually on the machine ──────
# launchd runs the job from `/` with no PYTHONPATH. On the reference machine rig is the symlink-
# shim install (`~/.local/bin/rig` → `<checkout>/bin/rig`), and `-m riglib` cannot import a
# checkout that is not on sys.path: the agent failed every one of its 9 runs (rig-cli#375).


def test_default_sweep_cmd_runs_the_checkout_shim_when_there_is_one():
    """This repo IS a checkout with `bin/rig`, so the argv re-enters it through the shim."""
    import sys

    shim = Path(spotlight.__file__).resolve().parent.parent / "bin" / "rig"
    assert shim.is_file()
    assert spotlight.default_sweep_cmd() == (sys.executable, str(shim), "spotlight-sweep")


def test_default_sweep_cmd_falls_back_to_the_module_form_for_a_package_install(tmp_path, monkeypatch):
    """riglib in site-packages has no sibling `bin/rig` → `-m riglib` is the only entry point."""
    import sys

    fake_pkg = tmp_path / "site-packages" / "riglib"
    fake_pkg.mkdir(parents=True)
    monkeypatch.setattr(spotlight, "__file__", str(fake_pkg / "spotlight.py"))
    assert spotlight.default_sweep_cmd() == (sys.executable, "-m", "riglib", "spotlight-sweep")
