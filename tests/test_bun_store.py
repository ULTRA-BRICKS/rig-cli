"""bun worktree installs through the global store: `rig worktree create` + the `rig status` check.

The cruxes (each red if broken):
- `rig worktree create` in a bun repo with `bun.enabled` runs `bun install --frozen-lockfile`
  INSIDE the new worktree, and never when the global bunfig is not provisioned (that install
  would be a full copy) or when `bun.worktree_install: false`;
- a worktree whose node_modules is a symlink to another checkout, a full copy, or full of broken
  links into `~/.bun/install/cache/links` is reported by `rig status`, while a healthy store
  install (links plus the few lifecycle-script packages bun keeps as directories) is not.
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


def _git_repo(path: Path, *, bun: bool = True) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "package.json").write_text('{"name": "x"}\n', encoding="utf-8")
    if bun:
        (path / "bun.lock").write_text("{}\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"], cwd=path, check=True
    )
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


def _fake_bun(tmp_path: Path, monkeypatch, *, exit_code: int = 0) -> Path:
    """A `bun` on PATH that logs `<cwd> <args>` and lays out a store-style node_modules."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "bun-calls.log"
    script = bindir / "bun"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$PWD $*" >> "{log}"\n'
        f"if [ {exit_code} -ne 0 ]; then echo 'error: lockfile had changes, but lockfile is frozen' >&2; exit {exit_code}; fi\n"
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
    problems = bun_store.scan_install(wt, tmp_path / "links")
    assert len(problems) == 1
    assert "symlink" in problems[0]
    assert str(main_checkout / "node_modules") in problems[0]
    assert "bun install --frozen-lockfile" in problems[0]


def test_scan_accepts_store_install_with_a_few_lifecycle_dirs(tmp_path):
    links = tmp_path / "links"
    wt = tmp_path / "wt"
    _store_layout(wt, links, linked=46, copied=2)
    assert bun_store.scan_install(wt, links) == []


def test_scan_flags_majority_copied_entries(tmp_path):
    links = tmp_path / "links"
    wt = tmp_path / "wt"
    _store_layout(wt, links, linked=1, copied=40)
    problems = bun_store.scan_install(wt, links)
    assert len(problems) == 1
    assert "40 of 41" in problems[0]
    assert "real directories" in problems[0]


def test_scan_flags_broken_links_into_the_store(tmp_path):
    links = tmp_path / "links"
    wt = tmp_path / "wt"
    _store_layout(wt, links, linked=10, copied=0, broken=3)
    problems = bun_store.scan_install(wt, links)
    assert len(problems) == 1
    assert "3 broken link(s)" in problems[0]


def test_scan_flags_unpinned_hoisted_layout_but_not_a_pinned_one(tmp_path):
    wt = tmp_path / "wt"
    for pkg in ("react", "zod", "typescript"):
        (wt / "node_modules" / pkg).mkdir(parents=True)
    problems = bun_store.scan_install(wt, tmp_path / "links")
    assert len(problems) == 1
    assert "hoisted" in problems[0]
    (wt / "bunfig.toml").write_text('[install]\nlinker = "hoisted"  # repo needs it\n', encoding="utf-8")
    assert bun_store.scan_install(wt, tmp_path / "links") == []


def test_scan_ignores_missing_or_empty_node_modules(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    assert bun_store.scan_install(wt, tmp_path / "links") == []
    (wt / "node_modules").mkdir()
    assert bun_store.scan_install(wt, tmp_path / "links") == []


def test_repo_pins_hoisted_reads_only_the_install_table(tmp_path):
    (tmp_path / "bunfig.toml").write_text('[test]\nlinker = "hoisted"\n', encoding="utf-8")
    assert not bun_store.repo_pins_hoisted(tmp_path)
    (tmp_path / "bunfig.toml").write_text('install.linker = "hoisted"\n', encoding="utf-8")
    assert bun_store.repo_pins_hoisted(tmp_path)


def test_store_links_dir_honors_bun_env(tmp_path, monkeypatch):
    monkeypatch.delenv("BUN_INSTALL_CACHE_DIR", raising=False)
    monkeypatch.setenv("BUN_INSTALL", str(tmp_path / "bunhome"))
    assert bun_store.store_links_dir() == tmp_path / "bunhome" / "install" / "cache" / "links"
    monkeypatch.setenv("BUN_INSTALL_CACHE_DIR", str(tmp_path / "cache"))
    assert bun_store.store_links_dir() == tmp_path / "cache" / "links"


# ── check_worktree_installs (the drift hook) ─────────────────────────────────


def test_check_worktree_installs_reports_only_bad_bun_worktrees(tmp_path, monkeypatch):
    monkeypatch.setenv("BUN_INSTALL_CACHE_DIR", str(tmp_path / "cache"))
    links = tmp_path / "cache" / "links"
    repo = _git_repo(tmp_path / "repo")
    (repo / "node_modules").mkdir()
    good = repo / ".worktrees" / "good"
    bad = repo / ".worktrees" / "bad"
    for wt, name in ((good, "good"), (bad, "bad")):
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", str(wt), "-b", name], check=True)
    _store_layout(good, links, linked=5, copied=0)
    os.symlink(repo / "node_modules", bad / "node_modules")
    report = DriftReport()
    bun_store.check_worktree_installs(repo, report)
    assert [(i.direction, i.category, i.item) for i in report.items] == [
        ("modified", "bun", f"worktree:{os.path.join('.worktrees', 'bad')}")
    ]
    assert report.items[0].target == bad


def test_check_worktree_installs_skips_non_bun_repos(tmp_path):
    repo = _git_repo(tmp_path / "repo", bun=False)
    main_nm = tmp_path / "elsewhere"
    main_nm.mkdir()
    os.symlink(main_nm, repo / "node_modules")
    report = DriftReport()
    bun_store.check_worktree_installs(repo, report)
    assert report.items == []


def test_status_reports_store_bypass_when_bun_enabled(tmp_path, capsys, fake_agent_tools):
    repo = _git_repo(tmp_path / "repo")
    main_nm = tmp_path / "main-checkout-node_modules"
    main_nm.mkdir()
    os.symlink(main_nm, repo / "node_modules")
    _provision_bunfig()
    _global_config(f"agent_tools_source: {fake_agent_tools}\nbun:\n  enabled: true\n")
    (repo / "rig.yaml").write_text(
        "version: 1\nskills: {enabled: false}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nci: {enabled: false}\n",
        encoding="utf-8",
    )
    rc = main(["status", "-C", str(repo)])
    out = capsys.readouterr().out
    assert "bun/worktree:(primary)" in out
    assert "node_modules is a symlink" in out
    assert rc == 3


def test_status_is_silent_about_worktrees_when_bun_disabled(tmp_path, capsys, fake_agent_tools):
    repo = _git_repo(tmp_path / "repo")
    main_nm = tmp_path / "main-checkout-node_modules"
    main_nm.mkdir()
    os.symlink(main_nm, repo / "node_modules")
    _global_config(f"agent_tools_source: {fake_agent_tools}\n")
    (repo / "rig.yaml").write_text(
        "version: 1\nskills: {enabled: false}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nci: {enabled: false}\n",
        encoding="utf-8",
    )
    main(["status", "-C", str(repo)])
    assert "bun/worktree:" not in capsys.readouterr().out


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
def test_install_refuses_a_node_modules_symlink(tmp_path, monkeypatch):
    log = _fake_bun(tmp_path, monkeypatch)
    _provision_bunfig()
    wt = tmp_path / "wt"
    wt.mkdir()
    target = tmp_path / "main" / "node_modules"
    target.mkdir(parents=True)
    os.symlink(target, wt / "node_modules")
    outcome = bun_store.install_worktree_deps(wt)
    assert outcome.status == "failed"
    assert "symlink" in outcome.message
    assert not log.exists()


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


def test_worktree_install_enabled_needs_bun_enabled():
    assert bun_store.worktree_install_enabled({"enabled": True})
    assert not bun_store.worktree_install_enabled({"enabled": True, "worktree_install": False})
    assert not bun_store.worktree_install_enabled({"worktree_install": True})
    assert not bun_store.worktree_install_enabled(None)
