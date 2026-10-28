"""Apply handlers of the ``lint_checks`` activation: converge once, then skip; fail closed; honour on_conflict."""

from __future__ import annotations

import errno
import http.client
import os
import shutil
import stat
import urllib.error
import urllib.request

import pytest
from lint_checks_helpers import FAKE_BINARY, fake_tarball, lint_plan, pin_fake_release

from riglib import cccc_install
from riglib.actions.runner import run_plan

pytestmark = pytest.mark.usefixtures("real_cccc_inspect", "pinned_rig")

REAL_DOWNLOAD = cccc_install._download  # captured before any test replaces it with a fake


@pytest.fixture
def env(tmp_path, monkeypatch, fake_agent_tools):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("RIG_CCCC_DRY_RUN", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    calls = pin_fake_release(monkeypatch)
    plan = lint_plan(fake_agent_tools, tmp_path, repo)
    return plan, repo, calls


def _result(report, kind):
    return next(r for r in report.results if r.action.kind == kind)


def test_first_apply_converges_then_rerun_skips_everything(env, tmp_path):
    plan, repo, calls = env
    first = run_plan(plan)
    assert [r.status for r in first.results] == ["created", "created", "created"], [r.detail for r in first.results]
    assert cccc_install.resolve_verified_binary() == cccc_install.managed_binary_path()
    hook = tmp_path / "disp" / "pre-commit" / "30-lint-check"
    assert hook.is_file() and os.access(hook, os.X_OK)
    assert (repo / ".github" / "workflows" / "rig-lint-check.yml").is_file()
    assert len(calls) == 1
    second = run_plan(plan)
    assert [r.status for r in second.results] == ["skipped"] * 3
    assert len(calls) == 1  # verified analyzer: no second download


def test_explicit_dry_run_flag_never_downloads(env, monkeypatch):
    plan, _, calls = env
    monkeypatch.setenv("RIG_CCCC_DRY_RUN", "1")
    res = _result(run_plan(plan), "install_cccc")
    assert res.status == "skipped" and "dry-run" in res.detail and calls == []
    assert not cccc_install.managed_binary_path().exists()


def test_alternate_home_apply_installs_the_analyzer_under_that_home(tmp_path, monkeypatch, fake_agent_tools):
    """A HOME-override apply (bot worker, CI container) used to skip the install while status/lint demanded it."""
    from riglib.actions import live_env as live_env_mod

    alt = tmp_path / "alt-home"
    alt.mkdir()
    monkeypatch.setenv("HOME", str(alt))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("RIG_CCCC_DRY_RUN", raising=False)
    assert live_env_mod._home_is_sandboxed()  # this really is an alternate HOME, not the login home
    repo = tmp_path / "repo"
    repo.mkdir()
    calls = pin_fake_release(monkeypatch)
    plan = lint_plan(fake_agent_tools, tmp_path, repo)
    report = run_plan(plan)
    res = _result(report, "install_cccc")
    assert res.status == "created" and not report.errors, [r.detail for r in report.results]
    dest = alt / ".local" / "share" / "rig" / "cccc" / f"v{cccc_install.PINNED_VERSION}" / "cccc"
    assert dest.is_file() and dest.read_bytes() == FAKE_BINARY and os.access(dest, os.X_OK)
    assert cccc_install.managed_binary_path() == dest and len(calls) == 1
    assert cccc_install.inspect().state == "ok"  # status / `rig lint check` now agree with apply
    assert _result(run_plan(plan), "install_cccc").status == "skipped"


def _offline(monkeypatch, exc: BaseException) -> list[str]:
    """Route the REAL ``_download`` into a failing urlopen (no network is ever touched); returns the attempted URLs."""
    monkeypatch.setattr(cccc_install, "_download", REAL_DOWNLOAD)
    attempts: list[str] = []

    def boom(url, *_a, **_kw):
        attempts.append(url)
        raise exc

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    return attempts


@pytest.mark.parametrize(
    "exc",
    [urllib.error.URLError("network is unreachable"), http.client.IncompleteRead(b"par", 100), http.client.RemoteDisconnected("eof")],
)
def test_offline_apply_fails_loudly_and_writes_no_machine_wide_hook(env, tmp_path, monkeypatch, exc):
    plan, _, _ = env
    _offline(monkeypatch, exc)
    report = run_plan(plan)
    cccc, hook = _result(report, "install_cccc"), _result(report, "install_lint_check_hook")
    assert cccc.status == "error" and "download of" in cccc.detail and "fail closed" in cccc.detail
    assert hook.status == "error" and "NOT written" in hook.detail and "rig apply commit" in hook.detail
    assert len(report.errors) == 2  # a failed apply is never a silent pass: rig apply commit exits non-zero
    assert not (tmp_path / "disp" / "pre-commit" / "30-lint-check").exists()
    assert not cccc_install.managed_binary_path().exists()
    assert _result(report, "install_lint_check_workflow").status == "created"  # repo CI is independent of the machine


def test_offline_apply_through_the_real_urllib_stack(env, tmp_path, monkeypatch):
    """No urlopen stub: a refused proxy connection travels the genuine urllib path into the same loud failure.

    An explicit opener is installed, so neither env proxies nor an opener another test already cached decide
    where the request goes: the only reachable endpoint is the refused discard port 127.0.0.1:9.
    """
    plan, _, _ = env
    monkeypatch.setattr(cccc_install, "_download", REAL_DOWNLOAD)
    dead = "http://127.0.0.1:9"
    for var in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(var, raising=False)  # ProxyHandler's proxy_bypass reads them
    monkeypatch.setattr(urllib.request, "_opener", urllib.request.build_opener(urllib.request.ProxyHandler({"http": dead, "https": dead})))
    report = run_plan(plan)
    assert [r.status for r in report.errors] == ["error", "error"]
    detail = _result(report, "install_cccc").detail
    assert "download of https://github.com/" in detail
    assert f"[Errno {errno.ECONNREFUSED}]" in detail  # refused by the dead proxy, not an HTTP answer from the real network
    assert not (tmp_path / "disp" / "pre-commit" / "30-lint-check").exists()


def test_recovery_after_an_offline_apply_installs_both(env, tmp_path, monkeypatch):
    plan, _, _ = env
    _offline(monkeypatch, urllib.error.URLError("offline"))
    assert len(run_plan(plan).errors) == 2
    pin_fake_release(monkeypatch)  # back online
    report = run_plan(plan)
    assert not report.errors
    assert (tmp_path / "disp" / "pre-commit" / "30-lint-check").is_file()


def test_hook_is_not_written_while_the_analyzer_is_mismatched_and_kept_by_policy(env, tmp_path):
    plan, _, _ = env
    _bad_binary()
    plan.on_conflict = "skip"  # the user's own file is left untouched => the pinned analyzer is still unusable
    report = run_plan(plan)
    assert _result(report, "install_cccc").status == "skipped"
    hook = _result(report, "install_lint_check_hook")
    assert hook.status == "error" and "sha256" in hook.detail
    assert not (tmp_path / "disp" / "pre-commit" / "30-lint-check").exists()


def test_dry_run_reports_the_pending_hook_without_writing_it(env, tmp_path, monkeypatch):
    plan, _, _ = env
    monkeypatch.setenv("RIG_CCCC_DRY_RUN", "1")
    report = run_plan(plan)
    hook = _result(report, "install_lint_check_hook")
    assert hook.status == "skipped" and "dry-run" in hook.detail and not report.errors
    assert not (tmp_path / "disp").exists()


def test_global_complexity_disabled_downloads_nothing_and_still_installs_the_hook(tmp_path, monkeypatch, fake_agent_tools):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("RIG_CCCC_DRY_RUN", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    pin_fake_release(monkeypatch)
    attempts = _offline(monkeypatch, urllib.error.URLError("offline"))  # even offline: nothing needs the network
    plan = lint_plan(fake_agent_tools, tmp_path, repo, global_lint={"complexity": {"enabled": False}})
    assert "install_cccc" not in {a.kind for a in plan.actions}
    report = run_plan(plan)
    assert not report.errors and attempts == []
    assert (tmp_path / "disp" / "pre-commit" / "30-lint-check").is_file()
    assert not (tmp_path / "data").exists()


def test_repo_complexity_off_cannot_write_the_machine_hook_without_the_analyzer(tmp_path, monkeypatch, fake_agent_tools):
    """One repo opting out of complexity must not let `rig apply commit` there write a hook that 127s everywhere else."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("RIG_CCCC_DRY_RUN", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    pin_fake_release(monkeypatch)
    attempts = _offline(monkeypatch, urllib.error.URLError("offline"))
    plan = lint_plan(fake_agent_tools, tmp_path, repo, {"lint_checks": {"complexity": {"enabled": False}}})
    report = run_plan(plan)
    assert len(attempts) == 1  # the analyzer IS attempted: other repos need it
    cccc, hook = _result(report, "install_cccc"), _result(report, "install_lint_check_hook")
    assert cccc.status == "error" and hook.status == "error" and "NOT written" in hook.detail
    assert not (tmp_path / "disp" / "pre-commit" / "30-lint-check").exists()
    assert _result(report, "install_lint_check_workflow").status == "created"


def test_globally_disabled_lint_checks_plan_nothing_so_nothing_downloads(tmp_path, monkeypatch, fake_agent_tools):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    repo.mkdir()
    calls = pin_fake_release(monkeypatch)
    plan = lint_plan(fake_agent_tools, tmp_path, repo, global_lint={"enabled": False})
    assert plan.actions == []
    assert not run_plan(plan).results and calls == []


def test_digest_mismatch_is_an_error_and_installs_nothing(env, monkeypatch):
    plan, _, _ = env
    pin_fake_release(monkeypatch, tarball=fake_tarball(b"#!/bin/sh\necho evil\n"))
    monkeypatch.setattr(cccc_install, "_download", lambda url: fake_tarball(b"#!/bin/sh\necho swapped\n"))
    res = _result(run_plan(plan), "install_cccc")
    assert res.status == "error" and "fail closed" in res.detail
    assert not cccc_install.managed_binary_path().exists()


def test_unsupported_platform_is_a_loud_blocked_error(env, monkeypatch):
    plan, _, calls = env
    monkeypatch.setattr(cccc_install, "platform_key", lambda: ("plan9", "mips"))
    res = _result(run_plan(plan), "install_cccc")
    assert res.status == "error" and res.detail.startswith("BLOCKED") and "plan9/mips" in res.detail
    assert calls == []


def _bad_binary():
    dest = cccc_install.managed_binary_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"#!/bin/sh\necho tampered\n")
    dest.chmod(0o755)
    return dest


def test_mismatched_analyzer_honours_on_conflict(env):
    plan, _, _ = env
    dest = _bad_binary()
    plan.on_conflict = "skip"
    res = _result(run_plan(plan), "install_cccc")
    assert res.status == "skipped" and "left untouched" in res.detail
    assert dest.read_bytes().endswith(b"tampered\n")
    plan.on_conflict = "backup"
    res = _result(run_plan(plan), "install_cccc")
    assert res.status == "backed_up" and res.backup is not None
    assert res.backup.read_bytes().endswith(b"tampered\n") and not os.access(res.backup, os.X_OK)
    assert dest.read_bytes() == FAKE_BINARY
    _bad_binary()
    plan.on_conflict = "overwrite"
    assert _result(run_plan(plan), "install_cccc").status == "created"
    assert dest.read_bytes() == FAKE_BINARY


def test_failed_install_after_a_backup_still_reports_where_the_old_file_went(env, monkeypatch):
    """`on_conflict=backup` moved the user's file aside; an offline install must not lose track of it."""
    plan, _, _ = env
    dest = _bad_binary()
    plan.on_conflict = "backup"
    _offline(monkeypatch, urllib.error.URLError("offline"))
    res = _result(run_plan(plan), "install_cccc")
    assert res.status == "error" and "fail closed" in res.detail
    assert res.backup is not None and res.backup.read_bytes().endswith(b"tampered\n") and not os.access(res.backup, os.X_OK)
    assert str(res.backup) in res.detail  # named for the operator, not only recorded
    assert not dest.exists()  # nothing untrusted stays where rig looks for the analyzer


def test_install_oserror_after_a_backup_is_a_loud_error_with_the_backup_path(env, monkeypatch):
    """A read-only store / full disk raises a plain OSError: still an `error` result naming the backup."""
    plan, _, _ = env
    _bad_binary()
    plan.on_conflict = "backup"

    def disk_full():
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cccc_install, "install", disk_full)
    res = _result(run_plan(plan), "install_cccc")
    assert res.status == "error" and "No space left" in res.detail
    assert res.backup is not None and str(res.backup) in res.detail


def test_hook_conflict_policy_and_exec_bit(env, tmp_path):
    plan, _, _ = env
    run_plan(plan)
    hook = tmp_path / "disp" / "pre-commit" / "30-lint-check"
    hook.chmod(0o644)  # git ignores a non-exec hook: re-apply must restore the bit
    res = _result(run_plan(plan), "install_lint_check_hook")
    assert os.access(hook, os.X_OK), res
    plan.on_conflict = "skip"  # identical bytes are rig's own file, not a user conflict: the lost bit is still restored
    hook.chmod(0o644)
    assert _result(run_plan(plan), "install_lint_check_hook").status == "skipped"
    assert os.access(hook, os.X_OK)
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")  # a hand edit that would silently disable the gate
    plan.on_conflict = "skip"
    assert _result(run_plan(plan), "install_lint_check_hook").status == "skipped"
    assert hook.read_text() == "#!/bin/sh\nexit 0\n"
    plan.on_conflict = "backup"
    res = _result(run_plan(plan), "install_lint_check_hook")
    assert res.status == "backed_up" and res.backup is not None and not os.access(res.backup, os.X_OK)
    assert "lint check --staged --json" in hook.read_text()


def test_workflow_backup_and_unsafe_path_refused(env):
    plan, repo, _ = env
    run_plan(plan)
    wf = repo / ".github" / "workflows" / "rig-lint-check.yml"
    wf.write_text("name: hand-edited\n", encoding="utf-8")
    res = _result(run_plan(plan), "install_lint_check_workflow")
    assert res.status == "backed_up" and res.backup is not None and "rig-lint-check" in wf.read_text()
    outside = repo.parent / "outside"
    outside.mkdir()
    shutil.rmtree(repo / ".github" / "workflows")
    (repo / ".github" / "workflows").symlink_to(outside)
    res = _result(run_plan(plan), "install_lint_check_workflow")
    assert res.status == "error" and "symlink" in res.detail
    assert list(outside.iterdir()) == []  # nothing escaped the repo


def test_backup_of_a_symlinked_analyzer_never_touches_the_link_target(env, tmp_path):
    plan, _, _ = env
    user_cccc = tmp_path / "user-bin" / "cccc"
    user_cccc.parent.mkdir()
    user_cccc.write_bytes(b"#!/bin/sh\necho mine\n")
    user_cccc.chmod(0o755)
    dest = cccc_install.managed_binary_path()
    dest.parent.mkdir(parents=True)
    dest.symlink_to(user_cccc)
    res = _result(run_plan(plan), "install_cccc")
    assert res.status == "backed_up" and res.backup is not None and res.backup.is_symlink()
    assert res.backup.resolve() == user_cccc.resolve()  # the link itself was moved aside, still pointing at the user's file
    assert stat.S_IMODE(user_cccc.stat().st_mode) == 0o755 and user_cccc.read_bytes().endswith(b"mine\n")  # the target's mode is untouched
    assert dest.read_bytes() == FAKE_BINARY and not dest.is_symlink()


def test_backup_of_a_directory_at_the_analyzer_path_keeps_it_traversable(env):
    plan, _, _ = env
    dest = cccc_install.managed_binary_path()
    (dest / "inner").mkdir(parents=True)
    (dest / "inner" / "keep.txt").write_text("user data", encoding="utf-8")
    res = _result(run_plan(plan), "install_cccc")
    assert res.status == "backed_up" and res.backup is not None and res.backup.is_dir()
    assert stat.S_IMODE(res.backup.stat().st_mode) & 0o111  # a stripped directory could no longer be entered
    assert (res.backup / "inner" / "keep.txt").read_text(encoding="utf-8") == "user data"
    assert dest.read_bytes() == FAKE_BINARY


def test_overwrite_replaces_a_symlink_without_following_it(env, tmp_path):
    plan, _, _ = env
    user_cccc = tmp_path / "user-bin" / "cccc"
    user_cccc.parent.mkdir()
    user_cccc.write_bytes(b"mine")
    dest = cccc_install.managed_binary_path()
    dest.parent.mkdir(parents=True)
    dest.symlink_to(user_cccc)
    plan.on_conflict = "overwrite"
    assert _result(run_plan(plan), "install_cccc").status == "created"
    assert user_cccc.read_bytes() == b"mine" and dest.read_bytes() == FAKE_BINARY


@pytest.mark.parametrize("policy,status", [("skip", "skipped"), ("backup", "backed_up"), ("overwrite", "created")])
def test_wrong_non_executable_file_is_a_conflict_not_a_silent_replace(env, policy, status):
    plan, _, _ = env
    dest = cccc_install.managed_binary_path()
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"not the analyzer")
    dest.chmod(0o644)  # previously classified "missing" (X_OK checked before the hash) and overwritten without a backup
    plan.on_conflict = policy
    res = _result(run_plan(plan), "install_cccc")
    assert res.status == status, res.detail
    assert (dest.read_bytes() == b"not the analyzer") is (policy == "skip")
    assert (res.backup is not None) is (policy == "backup")


def test_pinned_bytes_without_exec_bit_are_repaired_quietly(env):
    plan, _, _ = env
    run_plan(plan)
    dest = cccc_install.managed_binary_path()
    dest.chmod(0o644)
    plan.on_conflict = "skip"
    res = _result(run_plan(plan), "install_cccc")
    assert res.status == "created" and os.access(dest, os.X_OK)
