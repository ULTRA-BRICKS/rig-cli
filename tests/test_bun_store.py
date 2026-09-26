"""bun worktree installs: `rig worktree create` + the `rig status` check.

The cruxes (each red if broken):
- `rig worktree create` in a bun repo runs a plain `bun install --frozen-lockfile` INSIDE the new
  worktree (so the repo's own bunfig decides the layout) only when the GLOBAL config has
  `bun.enabled` — a repo rig.yaml can turn it off but never on — and never through a
  node_modules / node_modules/.bun symlink or before the global bunfig is provisioned;
- `rig status` reports links into another checkout and broken links into the store as drift, but
  a full copy / hoisted install only as an informational note (the store breaks typecheck in some
  repos), and nothing for a repo whose bunfig opts out of the store.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from riglib import bun_store, bunfig
from riglib.cli import main
from riglib.config import ConfigError, global_config_path, validate
from riglib.drift import DriftReport

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="fake bun is a POSIX shell script")


# ── helpers ──────────────────────────────────────────────────────────────────


def _commit_all(path: Path, message: str) -> None:
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", message], cwd=path, check=True)


def _git_repo(path: Path, *, bun: bool = True) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "package.json").write_text('{"name": "x"}\n', encoding="utf-8")
    if bun:
        (path / "bun.lock").write_text("{}\n", encoding="utf-8")
    _commit_all(path, "init")
    return path


def _provision_bunfig() -> Path:
    target = bunfig.bunfig_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(bunfig.managed_block() + "\n", encoding="utf-8")
    return target


def _global_config(text: str) -> None:
    path = global_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("version: 1\n" + text, encoding="utf-8")


def _fake_bun(tmp_path: Path, monkeypatch, *, exit_code: int = 0, sleep_s: int = 0) -> Path:
    """A `bun` on PATH that logs `<cwd> <args>` and lays out a store-style node_modules."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "bun-calls.log"
    script = bindir / "bun"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$PWD $*" >> "{log}"\n'
        + (f"sleep {sleep_s}\n" if sleep_s else "")
        + f"if [ {exit_code} -ne 0 ]; then echo 'error: lockfile had changes, but lockfile is frozen' >&2; exit {exit_code}; fi\n"
        "mkdir -p node_modules/.bun\n"
        "exit 0\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}")
    return log


def _store_layout(worktree: Path, links: Path, *, linked: int, copied: int, broken: int = 0) -> None:
    bun_dir = worktree / "node_modules" / ".bun"
    (bun_dir / "node_modules").mkdir(parents=True)  # bun's own hoisted-links dir: never counted
    for i in range(linked):
        store_entry = links / f"pkg{i}@1.0.0-abc"
        if i >= broken:
            store_entry.mkdir(parents=True, exist_ok=True)
        os.symlink(store_entry, bun_dir / f"pkg{i}@1.0.0")
    for i in range(copied):
        (bun_dir / f"scripted{i}@2.0.0" / "node_modules").mkdir(parents=True)


# ── scan_install ─────────────────────────────────────────────────────────────


def test_scan_flags_node_modules_symlink_to_another_checkout(tmp_path):
    main_checkout = tmp_path / "main"
    (main_checkout / "node_modules").mkdir(parents=True)
    wt = tmp_path / "wt"
    wt.mkdir()
    os.symlink(main_checkout / "node_modules", wt / "node_modules")
    scan = bun_store.scan_install(wt, tmp_path / "links")
    assert len(scan.broken) == 1
    assert "symlink" in scan.broken[0]
    assert str(main_checkout / "node_modules") in scan.broken[0]
    assert "bun install --frozen-lockfile" in scan.broken[0]


def test_scan_flags_a_linked_node_modules_bun_dir(tmp_path):
    other = tmp_path / "main" / "node_modules" / ".bun"
    other.mkdir(parents=True)
    wt = tmp_path / "wt"
    (wt / "node_modules").mkdir(parents=True)
    os.symlink(other, wt / "node_modules" / ".bun")
    scan = bun_store.scan_install(wt, tmp_path / "links")
    assert len(scan.broken) == 1
    assert os.path.join("node_modules", ".bun") in scan.broken[0]


def test_scan_flags_bun_entries_linked_into_another_checkout(tmp_path):
    links = tmp_path / "links"
    wt = tmp_path / "wt"
    _store_layout(wt, links, linked=5, copied=0)
    other_pkg = tmp_path / "main" / "node_modules" / ".bun" / "zod@3.0.0"
    other_pkg.mkdir(parents=True)
    os.symlink(other_pkg, wt / "node_modules" / ".bun" / "zod@3.0.0")
    local_pkg = wt / "packages" / "local"
    local_pkg.mkdir(parents=True)
    os.symlink(local_pkg, wt / "node_modules" / ".bun" / "local@file")  # a file: dep inside the tree
    scan = bun_store.scan_install(wt, links)
    assert len(scan.broken) == 1
    assert "1 node_modules/.bun entry link into another checkout" in scan.broken[0]
    assert "zod@3.0.0" in scan.broken[0]


def test_scan_accepts_store_install_with_a_few_lifecycle_dirs(tmp_path):
    links = tmp_path / "links"
    wt = tmp_path / "wt"
    _store_layout(wt, links, linked=46, copied=2)
    assert bun_store.scan_install(wt, links) == bun_store.InstallScan()


def test_scan_reports_majority_copied_as_off_store_not_broken(tmp_path):
    links = tmp_path / "links"
    wt = tmp_path / "wt"
    _store_layout(wt, links, linked=1, copied=40)
    scan = bun_store.scan_install(wt, links)
    assert scan.broken == ()
    assert scan.off_store == "40 of 41 packages copied"


def test_scan_flags_broken_links_into_the_store(tmp_path):
    links = tmp_path / "links"
    wt = tmp_path / "wt"
    _store_layout(wt, links, linked=10, copied=0, broken=3)
    scan = bun_store.scan_install(wt, links)
    assert len(scan.broken) == 1
    assert "3 broken link(s)" in scan.broken[0]


def test_store_prefix_needs_a_path_boundary(tmp_path):
    links = tmp_path / "cache" / "links"
    assert bun_store._points_into_store(str(links / "zod@3-abc"), links)
    assert not bun_store._points_into_store(str(tmp_path / "cache" / "links-evil" / "zod"), links)


def test_scan_hoisted_is_off_store_unless_the_repo_pins_it(tmp_path):
    wt = tmp_path / "wt"
    for pkg in ("react", "zod", "typescript"):
        (wt / "node_modules" / pkg).mkdir(parents=True)
    scan = bun_store.scan_install(wt, tmp_path / "links")
    assert scan.broken == ()
    assert scan.off_store == "hoisted, 3 package dirs"
    (wt / "bunfig.toml").write_text('[install]\nlinker = "hoisted"  # repo needs it\n', encoding="utf-8")
    assert bun_store.scan_install(wt, tmp_path / "links") == bun_store.InstallScan()


def test_scan_respects_a_repo_that_turns_the_store_off(tmp_path):
    links = tmp_path / "links"
    wt = tmp_path / "wt"
    _store_layout(wt, links, linked=0, copied=30)
    (wt / "bunfig.toml").write_text("[install]\nglobalStore = false\n", encoding="utf-8")
    assert bun_store.scan_install(wt, links) == bun_store.InstallScan()


def test_scan_ignores_missing_or_empty_node_modules(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    assert bun_store.scan_install(wt, tmp_path / "links") == bun_store.InstallScan()
    (wt / "node_modules").mkdir()
    assert bun_store.scan_install(wt, tmp_path / "links") == bun_store.InstallScan()


def test_project_install_pins_read_only_the_install_table(tmp_path):
    cfg = tmp_path / "bunfig.toml"
    cfg.write_text('[test]\nlinker = "hoisted"\nglobalStore = false\n', encoding="utf-8")
    assert bunfig.project_install_pins(tmp_path) == {}
    assert bunfig.project_store_opt_out(tmp_path) is None
    cfg.write_text('install.linker = "hoisted"\n[test]\nroot = "t"\n', encoding="utf-8")
    assert bunfig.project_install_pins(tmp_path) == {"linker": "hoisted"}
    assert "hoisted" in bunfig.project_store_opt_out(tmp_path)
    cfg.write_text('[install]\nlinker = "isolated"\nglobalStore = true\n', encoding="utf-8")
    assert bunfig.project_store_opt_out(tmp_path) is None
    cfg.write_text("[install]\nglobalStore = false # typecheck needs local @types\n", encoding="utf-8")
    assert "globalStore = false" in bunfig.project_store_opt_out(tmp_path)


def test_store_links_dir_honors_bun_env(tmp_path, monkeypatch):
    monkeypatch.delenv("BUN_INSTALL_CACHE_DIR", raising=False)
    monkeypatch.setenv("BUN_INSTALL", str(tmp_path / "bunhome"))
    assert bun_store.store_links_dir() == tmp_path / "bunhome" / "install" / "cache" / "links"
    monkeypatch.setenv("BUN_INSTALL_CACHE_DIR", str(tmp_path / "cache"))
    assert bun_store.store_links_dir() == tmp_path / "cache" / "links"


# ── check_worktree_installs (the drift hook) ─────────────────────────────────


def _repo_with_worktrees(tmp_path: Path, *names: str) -> tuple[Path, list[Path]]:
    repo = _git_repo(tmp_path / "repo")
    (repo / "node_modules").mkdir()
    trees = []
    for name in names:
        wt = repo / ".worktrees" / name
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", str(wt), "-b", name], check=True)
        trees.append(wt)
    return repo, trees


def test_check_worktree_installs_drifts_only_on_broken_installs(tmp_path, monkeypatch):
    monkeypatch.setenv("BUN_INSTALL_CACHE_DIR", str(tmp_path / "cache"))
    links = tmp_path / "cache" / "links"
    repo, (good, linked, copied) = _repo_with_worktrees(tmp_path, "good", "linked", "copied")
    _store_layout(good, links, linked=5, copied=0)
    os.symlink(repo / "node_modules", linked / "node_modules")
    _store_layout(copied, links, linked=0, copied=12)
    report = DriftReport()
    bun_store.check_worktree_installs(repo, report)
    assert [(i.direction, i.category, i.item) for i in report.items] == [
        ("extra", "bun", f"worktree:{os.path.join('.worktrees', 'linked')}")
    ]
    assert report.items[0].target == linked
    # the full copy is informational only: a repo may need a local install
    assert len(report.notes) == 1
    note = report.notes[0]
    assert note.category == "bun"
    assert "1 bun worktree install(s) not using the global store" in note.detail
    assert os.path.join(".worktrees", "copied") in note.detail


def test_check_worktree_installs_skips_non_bun_repos(tmp_path):
    repo = _git_repo(tmp_path / "repo", bun=False)
    main_nm = tmp_path / "elsewhere"
    main_nm.mkdir()
    os.symlink(main_nm, repo / "node_modules")
    report = DriftReport()
    bun_store.check_worktree_installs(repo, report)
    assert report.items == []
    assert report.notes == []


def test_repo_worktrees_survives_a_failing_git(tmp_path):
    assert bun_store.repo_worktrees(tmp_path / "not-a-repo") == []


def _status_repo(tmp_path: Path, fake_agent_tools: Path, *, bun_enabled: bool) -> Path:
    repo = _git_repo(tmp_path / "repo")
    _provision_bunfig()
    bun_block = "bun:\n  enabled: true\n" if bun_enabled else ""
    _global_config(f"agent_tools_source: {fake_agent_tools}\n{bun_block}")
    (repo / "rig.yaml").write_text(
        "version: 1\nskills: {enabled: false}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nci: {enabled: false}\n",
        encoding="utf-8",
    )
    return repo


def test_status_reports_a_node_modules_symlink_as_drift(tmp_path, capsys, fake_agent_tools):
    repo = _status_repo(tmp_path, fake_agent_tools, bun_enabled=True)
    main_nm = tmp_path / "main-checkout-node_modules"
    main_nm.mkdir()
    os.symlink(main_nm, repo / "node_modules")
    rc = main(["status", "-C", str(repo)])
    out = capsys.readouterr().out
    assert "bun/worktree:(primary)" in out
    assert "node_modules is a symlink" in out
    assert rc == 3


def test_status_shows_an_off_store_install_as_a_note_not_drift(tmp_path, capsys, fake_agent_tools):
    repo = _status_repo(tmp_path, fake_agent_tools, bun_enabled=True)
    for pkg in ("react", "zod"):
        (repo / "node_modules" / pkg).mkdir(parents=True)
    main(["status", "-C", str(repo)])
    out = capsys.readouterr().out
    assert "bun/worktree:" not in out
    assert "not using the global store" in out


def test_status_is_silent_about_worktrees_when_bun_disabled(tmp_path, capsys, fake_agent_tools):
    repo = _status_repo(tmp_path, fake_agent_tools, bun_enabled=False)
    main_nm = tmp_path / "main-checkout-node_modules"
    main_nm.mkdir()
    os.symlink(main_nm, repo / "node_modules")
    main(["status", "-C", str(repo)])
    out = capsys.readouterr().out
    assert "bun/worktree:" not in out
    assert "not using the global store" not in out


# ── install_worktree_deps ────────────────────────────────────────────────────


@posix_only
def test_install_runs_frozen_lockfile_in_the_worktree(tmp_path, monkeypatch):
    log = _fake_bun(tmp_path, monkeypatch)
    _provision_bunfig()
    wt = tmp_path / "wt"
    wt.mkdir()
    outcome = bun_store.install_worktree_deps(wt)
    assert outcome.status == "installed", outcome.message
    assert log.read_text(encoding="utf-8").split() == [str(wt.resolve()), "install", "--frozen-lockfile"]
    assert "global store" in outcome.message


@posix_only
def test_install_follows_a_repo_that_opts_out_of_the_store(tmp_path, monkeypatch):
    log = _fake_bun(tmp_path, monkeypatch)
    _provision_bunfig()
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "bunfig.toml").write_text("[install]\nglobalStore = false\n", encoding="utf-8")
    outcome = bun_store.install_worktree_deps(wt)
    assert outcome.status == "installed", outcome.message
    # no linker/store flags forced on top of the repo's own bunfig
    assert log.read_text(encoding="utf-8").split()[1:] == ["install", "--frozen-lockfile"]
    assert "as the repo configures" in outcome.message
    assert "globalStore = false" in outcome.message


@posix_only
def test_install_skips_without_the_global_store(tmp_path, monkeypatch):
    log = _fake_bun(tmp_path, monkeypatch)
    wt = tmp_path / "wt"
    wt.mkdir()
    outcome = bun_store.install_worktree_deps(wt)
    assert outcome.status == "skipped"
    assert "rig apply" in outcome.message
    assert not log.exists()


def test_install_skips_when_bun_is_missing(tmp_path, monkeypatch):
    _provision_bunfig()
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    monkeypatch.setenv("BUN_INSTALL", str(tmp_path / "no-bun"))
    wt = tmp_path / "wt"
    wt.mkdir()
    outcome = bun_store.install_worktree_deps(wt)
    assert outcome.status == "skipped"
    assert "bun is not installed" in outcome.message


@posix_only
@pytest.mark.parametrize("linked", ["node_modules", os.path.join("node_modules", ".bun")])
def test_install_refuses_a_write_through_symlink(tmp_path, monkeypatch, linked):
    log = _fake_bun(tmp_path, monkeypatch)
    _provision_bunfig()
    wt = tmp_path / "wt"
    (wt / "node_modules").mkdir(parents=True)
    if linked == "node_modules":
        (wt / "node_modules").rmdir()
    target = tmp_path / "main" / linked
    target.mkdir(parents=True)
    os.symlink(target, wt / linked)
    outcome = bun_store.install_worktree_deps(wt)
    assert outcome.status == "failed"
    assert "symlink" in outcome.message
    assert not log.exists()


def test_install_symlink_with_unprovisioned_store_is_a_skip_not_a_failure(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    (tmp_path / "main" / "node_modules").mkdir(parents=True)
    os.symlink(tmp_path / "main" / "node_modules", wt / "node_modules")
    assert bun_store.install_worktree_deps(wt).status == "skipped"


@posix_only
def test_install_failure_reports_stderr_and_the_fix(tmp_path, monkeypatch):
    _fake_bun(tmp_path, monkeypatch, exit_code=1)
    _provision_bunfig()
    wt = tmp_path / "wt"
    wt.mkdir()
    outcome = bun_store.install_worktree_deps(wt)
    assert outcome.status == "failed"
    assert "lockfile is frozen" in outcome.message
    assert "Never symlink node_modules" in outcome.message


@posix_only
def test_install_timeout_is_a_failure_with_the_command(tmp_path, monkeypatch):
    _fake_bun(tmp_path, monkeypatch, sleep_s=5)
    _provision_bunfig()
    wt = tmp_path / "wt"
    wt.mkdir()
    outcome = bun_store.install_worktree_deps(wt, timeout_s=1)
    assert outcome.status == "failed"
    assert "timed out after 1s" in outcome.message


# ── rig worktree create wiring ───────────────────────────────────────────────


@posix_only
def test_worktree_create_installs_bun_deps_when_enabled(tmp_path, monkeypatch, capsys):
    log = _fake_bun(tmp_path, monkeypatch)
    _provision_bunfig()
    _global_config("bun:\n  enabled: true\n")
    repo = _git_repo(tmp_path / "repo")
    rc = main(["worktree", "create", "feat", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == 0, out
    wt = repo / ".worktrees" / "feat"
    assert log.read_text(encoding="utf-8").split() == [str(wt.resolve()), "install", "--frozen-lockfile"]
    assert "deps installed" in out
    assert (wt / "node_modules").is_dir() and not (wt / "node_modules").is_symlink()


@posix_only
def test_worktree_create_honors_worktree_install_false(tmp_path, monkeypatch, capsys):
    log = _fake_bun(tmp_path, monkeypatch)
    _provision_bunfig()
    _global_config("bun:\n  enabled: true\n  worktree_install: false\n")
    repo = _git_repo(tmp_path / "repo")
    assert main(["worktree", "create", "feat", "-C", str(repo)]) == 0
    assert not log.exists()


@posix_only
def test_worktree_create_honors_a_repo_level_opt_out(tmp_path, monkeypatch, capsys):
    log = _fake_bun(tmp_path, monkeypatch)
    _provision_bunfig()
    _global_config("bun:\n  enabled: true\n")
    repo = _git_repo(tmp_path / "repo")
    (repo / "rig.yaml").write_text("version: 1\nbun:\n  worktree_install: false\n", encoding="utf-8")
    _commit_all(repo, "rig")
    assert main(["worktree", "create", "feat", "-C", str(repo)]) == 0
    assert not log.exists()


@posix_only
def test_a_repo_rig_yaml_cannot_turn_the_install_on(tmp_path, monkeypatch, capsys):
    log = _fake_bun(tmp_path, monkeypatch)
    _provision_bunfig()
    repo = _git_repo(tmp_path / "repo")
    (repo / "rig.yaml").write_text("version: 1\nbun:\n  enabled: true\n  worktree_install: true\n", encoding="utf-8")
    _commit_all(repo, "rig")
    assert main(["worktree", "create", "feat", "-C", str(repo)]) == 0
    assert not log.exists()
    assert "bun install --frozen-lockfile" in capsys.readouterr().out  # just the hint


@posix_only
def test_worktree_create_without_bun_enabled_only_hints(tmp_path, monkeypatch, capsys):
    log = _fake_bun(tmp_path, monkeypatch)
    repo = _git_repo(tmp_path / "repo")
    assert main(["worktree", "create", "feat", "-C", str(repo)]) == 0
    assert not log.exists()
    assert "bun install --frozen-lockfile" in capsys.readouterr().out


@posix_only
def test_worktree_create_in_non_bun_repo_does_nothing_extra(tmp_path, monkeypatch, capsys):
    log = _fake_bun(tmp_path, monkeypatch)
    _provision_bunfig()
    _global_config("bun:\n  enabled: true\n")
    repo = _git_repo(tmp_path / "repo", bun=False)
    assert main(["worktree", "create", "feat", "-C", str(repo)]) == 0
    assert not log.exists()
    assert "bun install" not in capsys.readouterr().out


def test_worktree_create_with_a_broken_config_keeps_the_worktree(tmp_path, capsys):
    _global_config("bun:\n  enabled: \"yes\"\n")
    repo = _git_repo(tmp_path / "repo")
    rc = main(["worktree", "create", "feat", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == 0
    assert (repo / ".worktrees" / "feat").is_dir()
    assert "rig config did not load" in out


@posix_only
def test_worktree_create_install_failure_exits_1_and_keeps_the_worktree(tmp_path, monkeypatch, capsys):
    _fake_bun(tmp_path, monkeypatch, exit_code=1)
    _provision_bunfig()
    _global_config("bun:\n  enabled: true\n")
    repo = _git_repo(tmp_path / "repo")
    rc = main(["worktree", "create", "feat", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == 1
    assert (repo / ".worktrees" / "feat").is_dir()
    assert "bun install --frozen-lockfile" in out


# ── config ───────────────────────────────────────────────────────────────────


def test_validate_bun_worktree_install():
    validate({"version": 1, "bun": {"enabled": True, "worktree_install": False}})
    with pytest.raises(ConfigError, match="bun.worktree_install must be a bool"):
        validate({"version": 1, "bun": {"enabled": True, "worktree_install": "no"}})


def test_install_decision_is_global_on_repo_off():
    on = {"enabled": True}
    assert bun_store.install_decision(on, on) == "install"
    assert bun_store.install_decision(on, {"enabled": True, "worktree_install": False}) == "off"
    assert bun_store.install_decision({"enabled": True, "worktree_install": False}, {"worktree_install": True}) == "off"
    assert bun_store.install_decision(None, {"enabled": True}) == "hint"
    assert bun_store.install_decision({"enabled": False}, on) == "hint"
