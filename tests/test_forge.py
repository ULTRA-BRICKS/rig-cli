"""forge — fj install, one-click OAuth login for fj + git, FJ_FALLBACK_HOST (riglib.forge)."""

from __future__ import annotations

import hashlib
import io
import os
import tarfile
import zipfile
from pathlib import Path

import pytest

from riglib import forge
from riglib.config import ConfigError, validate


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Every path forge touches points into tmp: home, APPDATA, XDG, git's global config."""
    home = tmp_path / "home"
    home.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    monkeypatch.setattr(forge.shutil, "which", lambda name: None)
    return home


def _opts(**over):
    base = forge.forge_options_from_config({"host": "git.example.com"})
    base.update(over)
    return base


# ── config ──────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,want",
    [
        ("git.example.com", "git.example.com"),
        ("https://Git.Example.com/", "git.example.com"),
        ("http://git.example.com:3000/org/repo", "git.example.com:3000"),
    ],
)
def test_normalize_host(raw, want):
    assert forge.normalize_host(raw) == want


@pytest.mark.parametrize("raw", ["", "not a host", "-bad.example", "git.example.com:abc"])
def test_normalize_host_rejects(raw):
    with pytest.raises(ValueError):
        forge.normalize_host(raw)


def test_config_accepts_a_forge_block():
    validate({"version": 1, "forge": {"host": "git.hyperide.ai", "git_credentials": False}})


@pytest.mark.parametrize(
    "block,path",
    [
        ({"host": "no spaces allowed"}, "forge.host"),
        ({}, None),  # empty block is a no-op, not an error
        ({"enabled": True}, "forge.host"),  # present + enabled needs a host
        ({"host": "git.hyperide.ai", "fj_version": "9.9.9"}, "forge.fj_version"),
        ({"host": "git.hyperide.ai", "install_fj": "yes"}, "forge.install_fj"),
        ({"host": "git.hyperide.ai", "typo": 1}, "forge.typo"),
    ],
)
def test_config_rejects_bad_forge_blocks(block, path):
    if path is None:
        validate({"version": 1, "forge": block})
        return
    with pytest.raises(ConfigError) as exc:
        validate({"version": 1, "forge": block})
    assert path in str(exc.value.schema_path or exc.value)


def test_disabled_block_skips_host_check():
    validate({"version": 1, "forge": {"enabled": False}})


def test_options_keep_the_machine_default_host_when_a_repo_overrides():
    opts = forge.forge_options_from_config({"host": "other.example"}, fallback_host="git.hyperide.ai")
    assert opts["hosts"] == ["other.example", "git.hyperide.ai"]
    assert opts["fallback_host"] == "git.hyperide.ai"
    assert opts["fj_client_id"] == forge.FORGEJO_TEA_CLIENT_ID
    assert opts["git_client_id"] == forge.FORGEJO_GCM_CLIENT_ID


def test_implied_env_vars_posix_only():
    block = {"host": "git.hyperide.ai"}
    assert forge.implied_env_vars(block, windows=False) == {"FJ_FALLBACK_HOST": "https://git.hyperide.ai"}
    assert forge.implied_env_vars(block, windows=True) == {}
    assert forge.implied_env_vars({"host": "x.example", "enabled": False}, windows=False) == {}
    assert forge.implied_env_vars(None, windows=False) == {}


# ── fj locations ────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "system,machine,asset",
    [
        ("Linux", "x86_64", "forgejo-cli-x86_64-linux.tar.gz"),
        ("Linux", "aarch64", "forgejo-cli-aarch64-linux.tar.gz"),
        ("Windows", "AMD64", "forgejo-cli-x86_64-windows.zip"),
        ("Darwin", "arm64", None),  # Homebrew
        ("Linux", "riscv64", None),
    ],
)
def test_release_asset(system, machine, asset):
    assert forge.fj_release_asset(system, machine) == asset


def test_client_ids_path_per_os(sandbox):
    assert forge.client_ids_path("Windows") == sandbox / "AppData" / "Roaming" / "Cyborus" / "forgejo-cli" / "config" / "client_ids"
    assert forge.client_ids_path("Darwin") == sandbox / ".config" / "forgejo-cli" / "client_ids"
    assert forge.client_ids_path("Linux") == sandbox / ".config" / "forgejo-cli" / "client_ids"


def test_client_ids_text_keeps_other_hosts_and_replaces_ours():
    existing = "# mine\ncodeberg.org abc\ngit.example.com OLD\ngit.example.com DUP\n"
    text = forge.desired_client_ids_text(existing, ["git.example.com", "second.example"], "NEW")
    assert text.splitlines() == [
        "# mine",
        "codeberg.org abc",
        f"git.example.com NEW  {forge.CLIENT_IDS_MARKER}",
        f"second.example NEW  {forge.CLIENT_IDS_MARKER}",
    ]
    assert forge.desired_client_ids_text(text, ["git.example.com", "second.example"], "NEW") == text


# ── install ─────────────────────────────────────────────────────────────────────────────────


def _zip_with(name: str, payload: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, payload)
    return buf.getvalue()


def _tgz_with(name: str, payload: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name)
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


@pytest.mark.parametrize(
    "system,machine,asset,binary,pack",
    [
        ("Windows", "AMD64", "forgejo-cli-x86_64-windows.zip", "fj.exe", _zip_with),
        ("Linux", "x86_64", "forgejo-cli-x86_64-linux.tar.gz", "fj", _tgz_with),
    ],
)
def test_install_fj_verifies_and_extracts(sandbox, monkeypatch, system, machine, asset, binary, pack):
    archive = pack(binary, b"FJ-BINARY")
    monkeypatch.setitem(forge.FJ_RELEASE_SHA256, "0.6.0", {asset: hashlib.sha256(archive).hexdigest()})
    monkeypatch.setattr(forge.platform, "machine", lambda: machine)
    fetched = []
    note = forge.install_fj("0.6.0", str(sandbox / "bin"), system=system,
                            fetch=lambda url: fetched.append(url) or archive)
    assert fetched == [forge.FJ_RELEASE_URL.format(version="0.6.0", asset=asset)]
    assert (sandbox / "bin" / binary).read_bytes() == b"FJ-BINARY"
    assert "installed fj 0.6.0" in note


def test_install_fj_refuses_a_checksum_mismatch(sandbox, monkeypatch):
    monkeypatch.setattr(forge.platform, "machine", lambda: "AMD64")
    with pytest.raises(forge.ForgeError, match="sha256 mismatch"):
        forge.install_fj("0.6.0", str(sandbox / "bin"), system="Windows",
                         fetch=lambda url: _zip_with("fj.exe", b"tampered"))
    assert not (sandbox / "bin" / "fj.exe").exists()


def test_install_fj_on_macos_needs_brew(sandbox):
    with pytest.raises(forge.ForgeError, match="brew install forgejo-cli"):
        forge.install_fj("0.6.0", str(sandbox / "bin"), system="Darwin")


# ── pending / apply (the shared comparison) ────────────────────────────────────────────────


def _stub_git(monkeypatch, *, gcm: bool):
    store: dict[str, str] = {}
    monkeypatch.setattr(forge, "git_uses_gcm", lambda: gcm)
    monkeypatch.setattr(forge, "git_config_get", lambda key: store.get(key))

    def fake_run(cmd, **kw):
        assert cmd[:3] == ["git", "config", "--global"], cmd
        store[cmd[3]] = cmd[4]

        class R:
            returncode = 0
            stderr = ""

        return R()

    monkeypatch.setattr(forge.subprocess, "run", fake_run)
    return store


def test_pending_then_apply_then_current(sandbox, monkeypatch):
    store = _stub_git(monkeypatch, gcm=True)
    installed = []
    monkeypatch.setattr(forge, "install_fj", lambda v, d: installed.append((v, d)) or "installed fj")
    opts = _opts()

    todo = forge.pending_changes(opts, windows=False)
    assert {item for _, item, _, _ in todo} == {"fj", "client_ids", "git-credential:git.example.com"}

    done, errors = forge.apply_changes(opts, todo)
    assert errors == [] and installed == [("0.6.0", "~/.local/bin")]
    assert forge.client_ids_path().read_text(encoding="utf-8") == (
        f"git.example.com {forge.FORGEJO_TEA_CLIENT_ID}  {forge.CLIENT_IDS_MARKER}\n"
    )
    assert store == forge.git_credential_settings("git.example.com", forge.FORGEJO_GCM_CLIENT_ID)

    # fj now "exists" — nothing left to do
    monkeypatch.setattr(forge, "find_fj", lambda bin_dir=None: "/usr/bin/fj")
    assert forge.pending_changes(opts, windows=False) == []


def test_git_settings_skipped_without_gcm(sandbox, monkeypatch):
    _stub_git(monkeypatch, gcm=False)
    monkeypatch.setattr(forge, "find_fj", lambda bin_dir=None: "/usr/bin/fj")
    items = {item for _, item, _, _ in forge.pending_changes(_opts(), windows=False)}
    assert items == {"client_ids"}


def test_windows_sets_the_user_env_var(sandbox, monkeypatch):
    _stub_git(monkeypatch, gcm=False)
    monkeypatch.setattr(forge, "find_fj", lambda bin_dir=None: "C:/fj.exe")
    env: dict[str, str] = {}
    monkeypatch.setattr(forge, "read_windows_user_env", lambda name: env.get(name))
    monkeypatch.setattr(forge, "set_windows_user_env", lambda name, value: env.__setitem__(name, value))
    opts = _opts()
    todo = forge.pending_changes(opts, windows=True)
    assert "FJ_FALLBACK_HOST" in {item for _, item, _, _ in todo}
    forge.apply_changes(opts, todo)
    assert env == {"FJ_FALLBACK_HOST": "https://git.example.com"}


def test_failed_install_does_not_block_the_other_steps(sandbox, monkeypatch):
    _stub_git(monkeypatch, gcm=False)

    def boom(v, d):
        raise forge.ForgeError("fj: no network")

    monkeypatch.setattr(forge, "install_fj", boom)
    done, errors = forge.apply_changes(_opts(), forge.pending_changes(_opts(), windows=False))
    assert errors == ["fj: no network"]
    assert forge.client_ids_path().is_file()


# ── plan / runner / drift wiring ────────────────────────────────────────────────────────────


def _loaded(tmp_path, data):
    from riglib.config import LoadedConfig

    return LoadedConfig(data=data, repo_root=tmp_path)


def test_plan_emits_one_forge_action(sandbox, tmp_path):
    from riglib.plan import InstallPlan, _build_forge

    plan = InstallPlan(actions=[])
    _build_forge(_loaded(tmp_path, {"forge": {"host": "git.example.com"}}), plan)
    [action] = plan.actions
    assert action.kind == "provision_forge" and action.category == "forge"
    assert action.options["hosts"] == ["git.example.com"]


def test_plan_skips_absent_or_disabled_forge(sandbox, tmp_path):
    from riglib.plan import InstallPlan, _build_forge

    for data in ({}, {"forge": {"enabled": False, "host": "git.example.com"}}):
        plan = InstallPlan(actions=[])
        _build_forge(_loaded(tmp_path, data), plan)
        assert plan.actions == []


def test_env_area_exports_the_fallback_host_on_posix(sandbox, tmp_path, monkeypatch):
    from riglib.plan import InstallPlan, _build_env

    monkeypatch.setattr(forge, "implied_env_vars", lambda block: {"FJ_FALLBACK_HOST": "https://git.example.com"})
    plan = InstallPlan(actions=[])
    _build_env(_loaded(tmp_path, {"forge": {"host": "git.example.com"}}), plan)  # no env block at all
    [action] = plan.actions
    assert action.kind == "provision_env"
    assert action.options["vars"] == {"FJ_FALLBACK_HOST": "https://git.example.com"}

    plan = InstallPlan(actions=[])
    _build_env(_loaded(tmp_path, {"forge": {"host": "git.example.com"},
                                  "env": {"vars": {"FJ_FALLBACK_HOST": "https://mine.example"}}}), plan)
    assert plan.actions[0].options["vars"]["FJ_FALLBACK_HOST"] == "https://mine.example"  # hand-set wins


def test_runner_dry_run_and_drift_share_the_comparison(sandbox, tmp_path, monkeypatch):
    from riglib.actions import runner
    from riglib.drift import DriftReport, _check_forge
    from riglib.plan import InstallPlan, _build_forge

    _stub_git(monkeypatch, gcm=False)
    monkeypatch.setattr(forge, "find_fj", lambda bin_dir=None: "/usr/bin/fj")
    # the Windows user env already carries the host, so only client_ids is out of date on any OS
    monkeypatch.setattr(forge, "read_windows_user_env", lambda name: "https://git.example.com")
    plan = InstallPlan(actions=[])
    _build_forge(_loaded(tmp_path, {"forge": {"host": "git.example.com"}}), plan)
    [action] = plan.actions

    report = DriftReport()
    _check_forge(action, report)
    assert [(i.direction, i.category, i.item) for i in report.items] == [("missing", "forge", "client_ids")]

    monkeypatch.setenv("RIG_FORGE_DRY_RUN", "1")
    res = runner._do_provision_forge(action, "backup")
    assert res.status == "skipped" and "dry-run" in res.detail
    assert not forge.client_ids_path().exists()

    monkeypatch.delenv("RIG_FORGE_DRY_RUN")
    assert runner._do_provision_forge(action, "backup").status == "updated"
    assert runner._do_provision_forge(action, "backup").status == "skipped"
