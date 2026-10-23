"""``tools.items.<name>.apt`` / ``.clone`` and the first-class ``ctmux`` tool.

Hermetic like :mod:`test_tools`: tmp dirs only, a fake ``install.sh``, ``git clone`` and every
apt/dpkg/sudo probe replaced by a stub — no test installs a package, clones a repo or touches the
host's PATH dir.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from riglib import apt_packages, tools
from riglib import drift as driftmod
from riglib.actions import runner
from riglib.config import ConfigError, validate
from riglib.plan import Action

_REAL_PROVISION = runner._do_provision_tools


@pytest.fixture(autouse=True)
def _real_tools(monkeypatch):
    monkeypatch.setattr(runner, "_do_provision_tools", _REAL_PROVISION)
    monkeypatch.setitem(runner._HANDLERS, "provision_tools", _REAL_PROVISION)
    monkeypatch.delenv("RIG_TOOLS_DRY_RUN", raising=False)


# ── config validation ─────────────────────────────────────────────────────────────────────
def test_apt_and_clone_accepted():
    validate({"version": 1, "tools": {"enabled": True, "items": {
        "ctmux": {"apt": ["tmux"], "brew": ["tmux"], "clone": "https://github.com/alex-mextner/ctmux-cli"},
    }}})


@pytest.mark.parametrize("bad", ["tmux", 3, [1], {"a": 1}])
def test_apt_must_be_a_string_list(bad):
    with pytest.raises(ConfigError, match=r"tools\.items\.t\.apt"):
        validate({"version": 1, "tools": {"items": {"t": {"apt": bad}}}})


@pytest.mark.parametrize("bad", [5, ["x"], "", "-oProxyCommand=evil", "https://x/y\nz"])
def test_clone_must_be_a_one_line_non_flag_url(bad):
    with pytest.raises(ConfigError, match=r"tools\.items\.t\.clone"):
        validate({"version": 1, "tools": {"items": {"t": {"clone": bad}}}})


def test_unknown_item_key_is_still_rejected():
    with pytest.raises(ConfigError, match="unknown tools.items.t key"):
        validate({"version": 1, "tools": {"items": {"t": {"pip": ["x"]}}}})


# ── spec resolution ───────────────────────────────────────────────────────────────────────
def test_apt_packages_resolve_and_drop_flag_shaped_names():
    specs = tools.resolve_tool_specs(
        {"enabled": True, "items": {"t": {"repo": "/r/t", "apt": ["tmux", "--yes", "git"]}}}
    )
    assert specs[0].apt_packages == ("tmux", "git")


def test_ctmux_is_a_first_class_tool_with_its_own_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    (spec,) = tools.resolve_tool_specs({"enabled": True, "items": {"ctmux": {}}})
    assert spec.name == "ctmux"
    assert spec.repo == tmp_path / "xp" / "ctmux-cli"
    assert spec.clone_url == "https://github.com/alex-mextner/ctmux-cli"
    assert spec.advertise is False and spec.check is True
    assert spec.managed_bin == tmp_path / ".local" / "bin" / "ctmux"


def test_an_explicit_clone_overrides_the_known_default_and_plain_tools_have_none():
    specs = {s.name: s for s in tools.resolve_tool_specs({"enabled": True, "items": {
        "ctmux": {"clone": "git@example.com:me/ctmux-fork.git"},
        "tg": {},
    }})}
    assert specs["ctmux"].clone_url == "git@example.com:me/ctmux-fork.git"
    assert specs["tg"].clone_url is None and specs["tg"].advertise is True and specs["tg"].check is False


def test_ctmux_declares_its_apt_dependencies_by_default_and_an_explicit_list_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    specs = {s.name: s for s in tools.resolve_tool_specs({"enabled": True, "items": {
        "ctmux": {}, "tg": {},
    }})}
    assert specs["ctmux"].apt_packages == ("tmux", "fzf") and specs["tg"].apt_packages == ()
    (own,) = tools.resolve_tool_specs({"enabled": True, "items": {"ctmux": {"apt": ["tmux"]}}})
    assert own.apt_packages == ("tmux",)
    (none,) = tools.resolve_tool_specs({"enabled": True, "items": {"ctmux": {"apt": []}}})
    assert none.apt_packages == ()
    (blank,) = tools.resolve_tool_specs({"enabled": True, "items": {"ctmux": {"apt": None}}})
    assert blank.apt_packages == ("tmux", "fzf")  # a bare `apt:` key is a typo, not an opt-out


def test_the_default_checkout_is_found_under_dev_when_xp_has_none(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    block = {"enabled": True, "items": {"ctmux": {}}}
    assert tools.resolve_tool_specs(block)[0].repo == tmp_path / "xp" / "ctmux-cli"  # nothing yet: clone target
    (tmp_path / "dev" / "ctmux-cli").mkdir(parents=True)
    (tmp_path / "dev" / "ctmux-cli" / "install.sh").write_text("")
    assert tools.resolve_tool_specs(block)[0].repo == tmp_path / "dev" / "ctmux-cli"
    (tmp_path / "xp" / "ctmux-cli").mkdir(parents=True)
    (tmp_path / "xp" / "ctmux-cli" / "install.sh").write_text("")
    assert tools.resolve_tool_specs(block)[0].repo == tmp_path / "xp" / "ctmux-cli"  # ~/xp wins when both exist
    (own,) = tools.resolve_tool_specs({"enabled": True, "items": {"ctmux": {"repo": "~/elsewhere"}}})
    assert own.repo == tmp_path / "elsewhere"


def test_a_provisioned_ctmux_is_its_bin_on_path_plus_the_blurb_when_advertised(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "xp" / "ctmux-cli"
    (repo / "linux").mkdir(parents=True)
    (repo / "install.sh").write_text("")
    (repo / "linux" / "ctmux").write_text("")
    (tmp_path / ".local" / "bin").mkdir(parents=True)
    (tmp_path / ".local" / "bin" / "ctmux").symlink_to(repo / "linux" / "ctmux")
    advertising = replace(tools.resolve_tool_specs({"enabled": True, "items": {"ctmux": {}}})[0], advertise=True)
    assert tools.tool_status(advertising).installed is False  # bin yes, blurb not yet
    blurb = tmp_path / ".agents" / "skills" / ".blurbs" / "ctmux.md"
    blurb.parent.mkdir(parents=True)
    blurb.write_text("ctmux")
    assert tools.tool_status(advertising).installed is True


def test_spec_options_round_trip_the_new_fields():
    spec = tools.ToolSpec(
        name="ctmux", repo=Path("/r/c"), bin_dir=Path("/b"), apt_packages=("tmux",),
        clone_url="https://x/y", advertise=False, check=True,
    )
    back = tools.plan_from_action_options({"specs": [tools.spec_to_option(spec)]}).specs[0]
    assert back == spec


def test_an_unadvertised_tool_is_installed_once_its_bin_resolves(tmp_path):
    repo = tmp_path / "ctmux-cli"
    repo.mkdir()
    (repo / "install.sh").write_text("#!/bin/sh\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    spec = tools.ToolSpec(name="ctmux", repo=repo, bin_dir=bin_dir, advertise=False)
    assert tools.tool_status(spec).installed is False
    (bin_dir / "ctmux").symlink_to(repo / "install.sh")
    assert tools.tool_status(spec).installed is True
    advertising = tools.ToolSpec(name="ctmux", repo=repo, bin_dir=bin_dir)
    assert tools.tool_status(advertising).installed is False  # still wants its skill blurb


# ── apt handler ───────────────────────────────────────────────────────────────────────────
def _apt_spec(tmp_path, *packages):
    return tools.ToolSpec(name="ctmux", repo=tmp_path / "ctmux-cli", bin_dir=tmp_path / "bin",
                          apt_packages=tuple(packages))


def _ensure(spec):
    return runner._ensure_apt_packages(spec, dry=False)


def _fake_apt(monkeypatch, *, installed=(), command=("sudo", "-n", "apt-get", "install", "-y"), apt=True):
    monkeypatch.setattr(apt_packages, "_on_path", lambda name: False)
    monkeypatch.setattr(apt_packages, "is_apt_system", lambda: apt)
    monkeypatch.setattr(apt_packages, "installed_snapshot", lambda: frozenset(installed))
    monkeypatch.setattr(
        apt_packages, "install_command",
        lambda pkgs: None if command is None else [*command, *pkgs],
    )
    ran: list[list[str]] = []
    monkeypatch.setattr(runner, "_run_apt_install", lambda argv: ran.append(argv) or (0, ""))
    return ran


def test_apt_noop_when_none_declared(tmp_path, monkeypatch):
    ran = _fake_apt(monkeypatch)
    monkeypatch.setattr(apt_packages, "is_apt_system", lambda: pytest.fail("must not probe"))
    assert runner._ensure_apt_packages(_apt_spec(tmp_path), dry=False) == ("", "", False)
    assert ran == []


def test_apt_list_is_skipped_on_a_machine_without_apt(tmp_path, monkeypatch):
    ran = _fake_apt(monkeypatch, apt=False)
    error, note, changed = runner._ensure_apt_packages(_apt_spec(tmp_path, "tmux"), dry=False)
    assert error == "" and changed is False and "skipped" in note
    assert ran == []


def test_apt_already_installed_installs_nothing(tmp_path, monkeypatch):
    ran = _fake_apt(monkeypatch, installed={"tmux"})
    error, note, changed = runner._ensure_apt_packages(_apt_spec(tmp_path, "tmux"), dry=False)
    assert (error, changed) == ("", False) and "already installed" in note
    assert ran == []


def test_apt_installs_only_the_missing_packages_in_one_call(tmp_path, monkeypatch):
    ran = _fake_apt(monkeypatch, installed={"git"})
    error, note, changed = runner._ensure_apt_packages(_apt_spec(tmp_path, "tmux", "git", "curl"), dry=False)
    assert error == "" and changed is True and "tmux, curl" in note
    assert ran == [["sudo", "-n", "apt-get", "install", "-y", "tmux", "curl"]]


def test_apt_without_privilege_reports_the_remainder_and_does_not_fail(tmp_path, monkeypatch):
    ran = _fake_apt(monkeypatch, command=None)
    error, note, changed = _ensure(_apt_spec(tmp_path, "tmux", "fzf"))
    assert error == "" and changed is False
    assert note.startswith("apt: WARN") and "`sudo apt-get install -y tmux fzf`" in note
    assert ran == []


def test_a_package_whose_command_is_on_path_is_not_missing(tmp_path, monkeypatch):
    ran = _fake_apt(monkeypatch, installed=(), command=None)
    monkeypatch.setattr(apt_packages, "_on_path", lambda name: name == "fzf")
    error, note, changed = _ensure(_apt_spec(tmp_path, "fzf", "tmux"))
    assert (error, changed) == ("", False) and "`sudo apt-get install -y tmux`" in note
    assert ran == []


def test_apt_install_failure_is_an_error_with_the_tail(tmp_path, monkeypatch):
    _fake_apt(monkeypatch)
    monkeypatch.setattr(runner, "_run_apt_install", lambda argv: (100, "E: unable to locate package tmux\n"))
    error, _note, changed = runner._ensure_apt_packages(_apt_spec(tmp_path, "tmux"), dry=False)
    assert changed is False and "exited 100" in error and "unable to locate package" in error


def test_apt_dry_run_only_probes(tmp_path, monkeypatch):
    ran = _fake_apt(monkeypatch)
    error, note, changed = runner._ensure_apt_packages(_apt_spec(tmp_path, "tmux"), dry=True)
    assert (error, changed) == ("", False) and "would install tmux" in note
    assert ran == []


# ── clone + install + check, through the real handler ─────────────────────────────────────
def _fake_clone(monkeypatch, *, check_exit=0, rc=0):
    """Replace ``git clone`` with a function that materialises a repo whose install.sh links the bin."""
    calls: list[tuple[str, Path]] = []

    def clone(url: str, dest: Path) -> int:
        calls.append((url, dest))
        if rc != 0:
            return rc
        dest.mkdir(parents=True)
        (dest / "install.sh").write_text(
            "#!/bin/bash\n"
            'if [ "$1" = "--check" ]; then exit ' + str(check_exit) + "; fi\n"
            'mkdir -p "$(dirname "$0")/../bin" && ln -sf "$0" "$(dirname "$0")/../bin/ctmux"\n'
        )
        return 0

    monkeypatch.setattr(runner, "_git_clone", clone)
    return calls


def _run(spec):
    action = Action(
        kind="provision_tools", category="tools", item="ecosystem", source=spec.repo, target=spec.bin_dir,
        options={"specs": [tools.spec_to_option(spec)]},
    )
    return runner._do_provision_tools(action, "backup")


def _ctmux(tmp_path, **kw):
    return tools.ToolSpec(
        name="ctmux", repo=tmp_path / "xp" / "ctmux-cli", bin_dir=tmp_path / "xp" / "bin",
        clone_url="https://github.com/alex-mextner/ctmux-cli", advertise=False, check=True, **kw,
    )


def test_a_missing_repo_is_cloned_installed_and_checked_then_reapply_is_a_noop(tmp_path, monkeypatch):
    calls = _fake_clone(monkeypatch)
    spec = _ctmux(tmp_path)  # the fake install.sh links into <repo>/../bin, which is spec.bin_dir
    first = _run(spec)
    assert first.status == "created", first.detail
    assert calls == [("https://github.com/alex-mextner/ctmux-cli", spec.repo)]
    assert "cloned" in first.detail
    second = _run(spec)
    assert second.status == "skipped", second.detail
    assert len(calls) == 1


def test_a_failing_install_check_fails_the_tool(tmp_path, monkeypatch):
    _fake_clone(monkeypatch, check_exit=3)
    res = _run(_ctmux(tmp_path))
    assert res.status == "error"
    assert "--check exited 3" in res.detail


def test_a_failed_clone_is_an_error_naming_the_url(tmp_path, monkeypatch):
    _fake_clone(monkeypatch, rc=128)
    res = _run(_ctmux(tmp_path))
    assert res.status == "error"
    assert "could not clone https://github.com/alex-mextner/ctmux-cli" in res.detail


def test_an_existing_directory_is_never_cloned_over(tmp_path, monkeypatch):
    calls = _fake_clone(monkeypatch)
    spec = _ctmux(tmp_path)
    spec.repo.mkdir(parents=True)  # exists, but has no install.sh
    res = _run(spec)
    assert res.status == "error" and "no install.sh" in res.detail
    assert calls == []


def test_dry_run_clones_nothing_and_says_so(tmp_path, monkeypatch):
    calls = _fake_clone(monkeypatch)
    monkeypatch.setenv("RIG_TOOLS_DRY_RUN", "1")
    spec = _ctmux(tmp_path)
    res = _run(spec)
    assert res.status == "created", res.detail
    assert "would clone https://github.com/alex-mextner/ctmux-cli" in res.detail
    assert calls == [] and not spec.repo.exists()


def test_apt_runs_before_install_and_a_missing_sudo_does_not_stop_the_tool(tmp_path, monkeypatch):
    _fake_clone(monkeypatch)
    _fake_apt(monkeypatch, command=None)
    spec = _ctmux(tmp_path, apt_packages=("tmux", "fzf"))
    res = _run(spec)
    assert res.status == "created", res.detail
    assert "sudo apt-get install -y tmux fzf" in res.detail and "apt: WARN" in res.detail
    assert (spec.bin_dir / "ctmux").is_symlink()  # install.sh ran despite the pending apt packages
    again = _run(spec)  # idempotent: nothing new is installed, the remainder is still reported
    assert again.status == "skipped", again.detail
    assert "sudo apt-get install -y tmux fzf" in again.detail


def test_a_failing_install_without_sudo_still_names_the_apt_remainder(tmp_path, monkeypatch):
    _fake_clone(monkeypatch, check_exit=1)
    _fake_apt(monkeypatch, command=None)
    res = _run(_ctmux(tmp_path, apt_packages=("tmux",)))
    assert res.status == "error"
    assert "--check exited 1" in res.detail and "sudo apt-get install -y tmux" in res.detail


def test_a_second_apply_with_everything_present_is_a_noop(tmp_path, monkeypatch):
    calls = _fake_clone(monkeypatch)
    ran = _fake_apt(monkeypatch, installed={"tmux", "fzf"})
    spec = _ctmux(tmp_path, apt_packages=("tmux", "fzf"))
    assert _run(spec).status == "created"
    assert _run(spec).status == "skipped"
    assert len(calls) == 1 and ran == []


# ── drift ─────────────────────────────────────────────────────────────────────────────────
def _tools_action(spec):
    return Action(kind="provision_tools", category="tools", item="ecosystem", source=spec.repo,
                  target=spec.bin_dir, options={"specs": [tools.spec_to_option(spec)]})


def test_status_flags_a_missing_apt_package_only_on_apt_machines(tmp_path, monkeypatch):
    spec = _apt_spec(tmp_path, "tmux")
    spec.repo.mkdir()
    (spec.repo / "install.sh").write_text("")
    spec.bin_dir.mkdir()
    (spec.bin_dir / "ctmux").symlink_to(spec.repo / "install.sh")
    spec = replace(spec, advertise=False)

    def items():
        report = driftmod.DriftReport()
        driftmod._check_tools(_tools_action(spec), report)
        return [(i.direction, i.item) for i in report.items]

    monkeypatch.setattr(apt_packages, "is_apt_system", lambda: False)
    assert items() == []
    monkeypatch.setattr(apt_packages, "is_apt_system", lambda: True)
    monkeypatch.setattr(apt_packages, "installed_snapshot", lambda: frozenset())
    monkeypatch.setattr(apt_packages, "privilege_prefix", lambda: None)
    monkeypatch.setattr(apt_packages, "_on_path", lambda name: False)
    assert items() == [("missing", "ctmux:tmux")]
    monkeypatch.setattr(apt_packages, "_on_path", lambda name: True)
    assert items() == []  # a command already on PATH satisfies the package
    monkeypatch.setattr(apt_packages, "_on_path", lambda name: False)
    monkeypatch.setattr(apt_packages, "installed_snapshot", lambda: frozenset({"tmux"}))
    assert items() == []


# ── riglib.apt_packages ───────────────────────────────────────────────────────────────────
def test_installed_snapshot_keeps_only_fully_installed_packages(monkeypatch):
    monkeypatch.setattr(apt_packages.shutil, "which", lambda name: "/usr/bin/" + name)
    out = "ii \ttmux\nrc \told\nii \tgit\nun \tghost\n"
    monkeypatch.setattr(
        apt_packages.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out, stderr=""),
    )
    assert apt_packages.installed_snapshot() == frozenset({"tmux", "git"})


def test_installed_snapshot_is_empty_when_dpkg_fails(monkeypatch):
    monkeypatch.setattr(apt_packages.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(
        apt_packages.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 2, stdout="ii \ttmux\n", stderr="boom"),
    )
    assert apt_packages.installed_snapshot() == frozenset()


def test_privilege_prefix_root_sudo_or_none(monkeypatch):
    monkeypatch.setattr(apt_packages.os, "geteuid", lambda: 0)
    assert apt_packages.privilege_prefix() == []
    monkeypatch.setattr(apt_packages.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(apt_packages.shutil, "which", lambda name: None)
    assert apt_packages.privilege_prefix() is None
    monkeypatch.setattr(apt_packages.shutil, "which", lambda name: "/usr/bin/sudo")
    monkeypatch.setattr(
        apt_packages.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, b"", b"")
    )
    assert apt_packages.privilege_prefix() is None
    monkeypatch.setattr(
        apt_packages.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, b"", b"")
    )
    assert apt_packages.privilege_prefix() == ["/usr/bin/sudo", "-n"]
    assert apt_packages.install_command(["tmux"]) == ["/usr/bin/sudo", "-n", "apt-get", "install", "-y", "tmux"]
