"""fj — fj install + the `fj ship` wrapper, one-click OAuth login for fj + git, FJ_FALLBACK_HOST
(riglib.fj)."""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from riglib import fj
from riglib import gh_ship_alias as gsa
from riglib.config import ConfigError, validate

# Resolved before any fixture rewrites PATH. On Windows `sh` is Git for Windows' MSYS sh — never
# spawn "bash" by name there: CreateProcess finds WSL's System32\bash.exe first.
SH = shutil.which("sh")
GIT = shutil.which("git")
TOOLS_PATH = os.pathsep.join(dict.fromkeys(os.path.dirname(p) for p in (SH, GIT) if p))
needs_sh = pytest.mark.skipif(SH is None or GIT is None, reason="needs a POSIX sh and git")
windows_only = pytest.mark.skipif(os.name != "nt", reason="cmd.exe / fj.cmd")


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Every path fj touches points into tmp: home, APPDATA, XDG, git's global config — and PATH is
    an empty dir, so no fj of the machine running the suite is ever seen (or run)."""
    home = tmp_path / "home"
    home.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.delenv("AGENT_TOOLS_ROOT", raising=False)
    monkeypatch.setattr(fj.shutil, "which", lambda name: None)
    return home


def _opts(**over):
    base = fj.fj_options_from_config({"host": "git.example.com"}, fallback_host="git.example.com")
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
    assert fj.normalize_host(raw) == want


@pytest.mark.parametrize("raw", ["", "not a host", "-bad.example", "git.example.com:abc"])
def test_normalize_host_rejects(raw):
    with pytest.raises(ValueError):
        fj.normalize_host(raw)


def test_config_accepts_an_fj_block():
    validate({"version": 1, "fj": {"host": "git.hyperide.ai", "git_credentials": False}})


@pytest.mark.parametrize(
    "block,path",
    [
        ({"host": "no spaces allowed"}, "fj.host"),
        ({}, None),  # empty block is a no-op, not an error
        ({"enabled": True}, "fj.host"),  # present + enabled needs a host
        ({"host": "git.hyperide.ai", "version": "9.9.9"}, "fj.version"),
        ({"host": "git.hyperide.ai", "install": "yes"}, "fj.install"),
        ({"host": "git.hyperide.ai", "typo": 1}, "fj.typo"),
    ],
)
def test_config_rejects_bad_fj_blocks(block, path):
    if path is None:
        validate({"version": 1, "fj": block})
        return
    with pytest.raises(ConfigError) as exc:
        validate({"version": 1, "fj": block})
    assert path in str(exc.value.schema_path or exc.value)


def test_disabled_block_skips_host_check():
    validate({"version": 1, "fj": {"enabled": False}})


def test_options_keep_the_machine_default_host_when_a_repo_overrides():
    opts = fj.fj_options_from_config({"host": "other.example"}, fallback_host="git.hyperide.ai")
    assert opts["hosts"] == ["other.example", "git.hyperide.ai"]
    assert opts["fallback_host"] == "git.hyperide.ai"
    assert opts["client_id"] == fj.FORGEJO_TEA_CLIENT_ID
    assert opts["git_client_id"] == fj.FORGEJO_GCM_CLIENT_ID


def test_implied_env_vars_posix_only():
    block = {"host": "git.hyperide.ai"}
    assert fj.implied_env_vars(block, windows=False) == {"FJ_FALLBACK_HOST": "https://git.hyperide.ai"}
    assert fj.implied_env_vars(block, windows=True) == {}
    assert fj.implied_env_vars({"host": "x.example", "enabled": False}, windows=False) == {}
    assert fj.implied_env_vars(None, windows=False) == {}


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
    assert fj.fj_release_asset(system, machine) == asset


def test_client_ids_path_per_os(sandbox):
    assert fj.client_ids_path("Windows") == sandbox / "AppData" / "Roaming" / "Cyborus" / "forgejo-cli" / "config" / "client_ids"
    assert fj.client_ids_path("Darwin") == sandbox / ".config" / "forgejo-cli" / "client_ids"
    assert fj.client_ids_path("Linux") == sandbox / ".config" / "forgejo-cli" / "client_ids"


def test_client_ids_text_keeps_other_hosts_and_replaces_ours():
    existing = "# mine\ncodeberg.org abc\ngit.example.com OLD\ngit.example.com DUP\n"
    text = fj.desired_client_ids_text(existing, ["git.example.com", "second.example"], "NEW")
    assert text.splitlines() == [
        "# mine",
        "codeberg.org abc",
        f"git.example.com NEW  {fj.CLIENT_IDS_MARKER}",
        f"second.example NEW  {fj.CLIENT_IDS_MARKER}",
    ]
    assert fj.desired_client_ids_text(text, ["git.example.com", "second.example"], "NEW") == text


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


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize(
    "system,machine,asset,binary,pack",
    [
        ("Windows", "AMD64", "forgejo-cli-x86_64-windows.zip", "fj.exe", _zip_with),
        ("Linux", "x86_64", "forgejo-cli-x86_64-linux.tar.gz", "fj", _tgz_with),
    ],
)
def test_install_fj_verifies_and_extracts_into_the_store(sandbox, monkeypatch, system, machine, asset, binary, pack):
    archive = pack(binary, b"FJ-BINARY")
    monkeypatch.setitem(fj.FJ_RELEASE_SHA256, "0.6.0", {asset: _sha(archive)})
    monkeypatch.setitem(fj.FJ_BINARY_SHA256, "0.6.0", {asset: _sha(b"FJ-BINARY")})
    monkeypatch.setattr(fj.platform, "machine", lambda: machine)
    fetched = []
    note = fj.install_fj("0.6.0", system=system, fetch=lambda url: fetched.append(url) or archive)
    assert fetched == [fj.FJ_RELEASE_URL.format(version="0.6.0", asset=asset)]
    dest = fj.fj_store_root(system) / "0.6.0" / binary
    assert dest == fj.fj_store_binary("0.6.0", system) and dest.read_bytes() == b"FJ-BINARY"
    assert "installed fj 0.6.0" in note
    assert not (sandbox / ".local" / "bin").exists(), "the real fj never goes onto PATH"


def test_store_root_per_os(sandbox, monkeypatch):
    assert fj.fj_store_root("Linux") == sandbox / ".local" / "share" / "rig" / "fj"
    monkeypatch.delenv("XDG_DATA_HOME")
    assert fj.fj_store_root("Darwin") == sandbox / ".local" / "share" / "rig" / "fj"
    monkeypatch.setenv("XDG_DATA_HOME", str(sandbox / "elsewhere"))
    # Windows ignores XDG_DATA_HOME: the wrapper finds the store through %USERPROFILE% from any shell
    assert fj.fj_store_root("Windows") == sandbox / ".local" / "share" / "rig" / "fj"
    assert fj.fj_store_binary("0.6.0", "CYGWIN_NT-10.0").name == "fj.exe"
    assert fj.fj_store_binary("0.6.0", "Linux").name == "fj"


def test_install_fj_refuses_a_checksum_mismatch(sandbox, monkeypatch):
    monkeypatch.setattr(fj.platform, "machine", lambda: "AMD64")
    with pytest.raises(fj.FjError, match="sha256 mismatch"):
        fj.install_fj("0.6.0", system="Windows", fetch=lambda url: _zip_with("fj.exe", b"tampered"))
    assert not fj.fj_store_binary("0.6.0", "Windows").exists()


def test_install_fj_refuses_a_binary_that_does_not_match_its_pin(sandbox, monkeypatch):
    archive = _zip_with("fj.exe", b"swapped")
    monkeypatch.setitem(fj.FJ_RELEASE_SHA256, "0.6.0", {"forgejo-cli-x86_64-windows.zip": _sha(archive)})
    monkeypatch.setattr(fj.platform, "machine", lambda: "AMD64")
    with pytest.raises(fj.FjError, match="does not match its pinned sha256"):
        fj.install_fj("0.6.0", system="Windows", fetch=lambda url: archive)
    assert not fj.fj_store_binary("0.6.0", "Windows").exists()


def test_pinned_binary_digests_cover_every_pinned_archive():
    assert set(fj.FJ_BINARY_SHA256) == set(fj.FJ_RELEASE_SHA256)
    for version, assets in fj.FJ_RELEASE_SHA256.items():
        assert set(fj.FJ_BINARY_SHA256[version]) == set(assets)


def test_install_fj_on_macos_needs_brew(sandbox):
    with pytest.raises(fj.FjError, match="brew install forgejo-cli"):
        fj.install_fj("0.6.0", system="Darwin")


# ── the wrapper: fj ship = gh ship, everything else → the real fj ────────────────────────────


def test_wrapper_carries_the_gh_ship_body_verbatim():
    wrapper = fj.fj_wrapper_sh("0.6.0")
    body = gsa.ship_dispatch_sh("fj ship")
    assert body in wrapper
    # the SAME function builds the gh alias: only the diagnostic's command name differs
    assert gsa.gh_ship_alias_expansion() == "!" + body.replace('echo "fj ship:', 'echo "gh ship:')
    assert wrapper.startswith("#!/bin/sh\n") and fj.WRAPPER_MARKER in wrapper.splitlines()[1]


def test_wrapper_is_a_portable_constant_per_version():
    for text in (fj.fj_wrapper_sh("0.6.0"), fj.fj_wrapper_cmd("0.6.0")):
        assert "/Users/" not in text and "/home/" not in text and "C:\\Users" not in text
    assert fj.fj_wrapper_sh("0.6.0") == fj.fj_wrapper_sh("0.6.0")
    assert fj.fj_wrapper_sh("0.7.0") != fj.fj_wrapper_sh("0.6.0")
    cmd = fj.fj_wrapper_cmd("0.6.0")
    assert cmd.isascii() and "\r\n" in cmd and "\n" not in cmd.replace("\r\n", "")
    assert "\r" not in fj.fj_wrapper_sh("0.6.0")


def _fake_fj(path: Path, label: str = "REAL", code: int = 7) -> Path:
    """A stand-in real fj: prints its label, argc, each argument, then stdin; exits ``code``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        f'#!/bin/sh\nprintf "{label} argc=%s\\n" "$#"\nfor a in "$@"; do printf "[%s]\\n" "$a"; done\n'
        f"cat\nexit {code}\n".encode()
    )
    path.chmod(0o755)
    return path


def _write_wrapper(bin_dir: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    wrapper = bin_dir / "fj"
    wrapper.write_bytes(fj.fj_wrapper_sh("0.6.0").encode())
    wrapper.chmod(0o755)
    return wrapper


def _env(home: Path, *path_dirs: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("AGENT_TOOLS_ROOT", "PATH")}
    env.update(
        HOME=str(home), USERPROFILE=str(home),
        XDG_CONFIG_HOME=(home / ".config").as_posix(), XDG_DATA_HOME=(home / ".local" / "share").as_posix(),
        PATH=os.pathsep.join([*(str(d) for d in path_dirs), TOOLS_PATH]),
    )
    env.update(extra)
    return env


def _run_ship(kind: str, wrapper: Path, args: list[str], cwd: Path, env: dict[str, str]):
    if kind == "gh":  # exactly how gh runs a shell alias: sh -c <expansion> -- <args>
        argv = [SH, "-c", gsa.gh_ship_alias_expansion()[1:], "--", *args]
    else:
        argv = [SH, str(wrapper), "ship", *args]
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=60)


@needs_sh
@pytest.mark.parametrize("kind", ["gh", "fj"])
def test_ship_runs_the_repo_delegator(sandbox, tmp_path, kind):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run([GIT, "init", "-q"], cwd=repo, check=True, env=_env(sandbox))
    deleg = repo / ".claude" / "scripts" / "pr-ship.sh"
    deleg.parent.mkdir(parents=True)
    deleg.write_bytes(b'#!/bin/sh\necho "DELEGATOR:$*"\n')
    deleg.chmod(0o755)
    wrapper = _write_wrapper(tmp_path / "bin")
    res = _run_ship(kind, wrapper, ["123", "--yes"], repo, _env(sandbox, tmp_path / "bin"))
    assert (res.returncode, res.stdout.strip()) == (0, "DELEGATOR:123 --yes"), res.stderr


@needs_sh
@pytest.mark.parametrize("kind", ["gh", "fj"])
def test_ship_falls_back_to_ship_sh_via_the_machine_env_file(sandbox, tmp_path, kind):
    at = tmp_path / "agent-tools"
    ship = at / "ci" / "ship" / "ship.sh"
    ship.parent.mkdir(parents=True)
    ship.write_bytes(b'#!/bin/sh\necho "CANONICAL:$* root=$AGENT_TOOLS_ROOT"\n')
    ship.chmod(0o755)
    envfile = sandbox / ".config" / "agent-tools" / "env"
    envfile.parent.mkdir(parents=True)
    envfile.write_bytes(f'AGENT_TOOLS_ROOT="{at.as_posix()}"\n'.encode())
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    wrapper = _write_wrapper(tmp_path / "bin")
    res = _run_ship(kind, wrapper, ["42"], outside, _env(sandbox, tmp_path / "bin", GIT_CEILING_DIRECTORIES=str(tmp_path)))
    assert (res.returncode, res.stdout.strip()) == (0, f"CANONICAL:42 root={at.as_posix()}"), res.stderr


@needs_sh
@pytest.mark.parametrize("kind", ["gh", "fj"])
def test_ship_exits_127_when_nothing_resolves(sandbox, tmp_path, kind):
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    wrapper = _write_wrapper(tmp_path / "bin")
    res = _run_ship(kind, wrapper, ["7"], outside, _env(sandbox, tmp_path / "bin", GIT_CEILING_DIRECTORIES=str(tmp_path)))
    assert res.returncode == 127
    assert f"{kind} ship: no ship delegator in this repo and AGENT_TOOLS_ROOT unresolved" in res.stderr
    assert res.stdout == ""


@needs_sh
def test_other_commands_reach_the_real_fj_untouched(sandbox, tmp_path):
    real = _fake_fj(fj.fj_store_binary("0.6.0"))
    wrapper = _write_wrapper(tmp_path / "bin")
    res = subprocess.run(
        [SH, str(wrapper), "pr", "ship", "--title", "a b", ""],
        input="from-stdin\n", env=_env(sandbox, tmp_path / "bin"), capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 7, res.stderr
    assert res.stdout.splitlines() == ["REAL argc=5", "[pr]", "[ship]", "[--title]", "[a b]", "[]", "from-stdin"]
    assert real.is_file()


@needs_sh
def test_without_rigs_copy_the_wrapper_runs_the_first_other_fj_on_path(sandbox, tmp_path):
    # macOS: Homebrew's fj stays where it is; a second rig wrapper on PATH is skipped too (no loop)
    wrapper = _write_wrapper(tmp_path / "bin")
    _write_wrapper(tmp_path / "old-bin")
    _fake_fj(tmp_path / "brew" / "fj", label="BREW", code=0)
    res = subprocess.run(
        [SH, str(wrapper), "whoami"], input="",
        env=_env(sandbox, tmp_path / "bin", tmp_path / "old-bin", tmp_path / "brew"),
        capture_output=True, text=True, timeout=60,
    )
    assert (res.returncode, res.stdout.splitlines()) == (0, ["BREW argc=1", "[whoami]"]), res.stderr


@needs_sh
def test_no_real_fj_anywhere_exits_127(sandbox, tmp_path):
    if any((Path(d) / n).exists() for d in TOOLS_PATH.split(os.pathsep) for n in ("fj", "fj.exe")):
        pytest.skip("this machine has an fj next to sh/git")
    wrapper = _write_wrapper(tmp_path / "bin")
    res = subprocess.run([SH, str(wrapper), "whoami"], env=_env(sandbox, tmp_path / "bin"),
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 127
    assert "fj: the real fj is not installed" in res.stderr and "run rig apply" in res.stderr


@windows_only
@needs_sh
def test_fj_cmd_passes_through_to_fj_exe_and_ships_through_sh(sandbox, tmp_path):
    # fake real fj.exe: a copy of cmd.exe (`fj /c …` runs a cmd command) — exit code, argv, stdin
    store = fj.fj_store_binary("0.6.0", "Windows")
    store.parent.mkdir(parents=True)
    shutil.copyfile(Path(os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")), store)
    for path, data in fj.fj_wrapper_files(_opts(), "Windows"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    shim = sandbox / ".local" / "bin" / "fj.cmd"
    env = _env(sandbox, shim.parent, Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32")
    run = lambda *a, **kw: subprocess.run([str(shim), *a], env=env, capture_output=True, text=True, timeout=60, **kw)  # noqa: E731
    assert run("/c", "exit 5").returncode == 5
    assert run("/c", "echo hello world").stdout.strip() == "hello world"
    assert run("/c", "findstr .", input="piped\n").stdout.strip() == "piped"

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run([GIT, "init", "-q"], cwd=repo, check=True, env=env)
    deleg = repo / ".claude" / "scripts" / "pr-ship.sh"
    deleg.parent.mkdir(parents=True)
    deleg.write_bytes(b'#!/bin/sh\necho "DELEGATOR:$*"\nexit 3\n')
    res = subprocess.run([str(shim), "ship", "12"], cwd=repo, env=env, capture_output=True, text=True, timeout=60)
    assert (res.returncode, res.stdout.strip()) == (3, "DELEGATOR:12"), res.stderr


# ── install layout per OS: migrate, write the wrapper, report what rig can't fix ─────────────


def _stub_git(monkeypatch, *, gcm: bool):
    store: dict[str, str] = {}
    monkeypatch.setattr(fj, "git_uses_gcm", lambda: gcm)
    monkeypatch.setattr(fj, "git_config_get", lambda key: store.get(key))

    def fake_run(cmd, **kw):
        assert cmd[:3] == ["git", "config", "--global"], cmd
        store[cmd[3]] = cmd[4]

        class R:
            returncode = 0
            stderr = ""

        return R()

    monkeypatch.setattr(fj.subprocess, "run", fake_run)
    return store


def _fake_install(calls: list):
    def install(version, *, system=None, fetch=None):
        calls.append(version)
        dest = fj.fj_store_binary(version, system)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"downloaded fj")
        return f"installed fj {version} → {dest}"

    return install


def _no_install(version, *, system=None, fetch=None):
    raise AssertionError("no download expected")


def _items(todo):
    return {item for _, item, _, _ in todo}


def _bin(home: Path) -> Path:
    return home / ".local" / "bin"


def _on_path(monkeypatch, *dirs: Path):
    monkeypatch.setenv("PATH", os.pathsep.join(str(d) for d in dirs))


def test_linux_pending_then_apply_then_current(sandbox, monkeypatch):
    store = _stub_git(monkeypatch, gcm=True)
    calls: list = []
    monkeypatch.setattr(fj, "install_fj", _fake_install(calls))
    _on_path(monkeypatch, _bin(sandbox))
    opts = _opts()

    todo = fj.pending_changes(opts, windows=False, system="Linux")
    assert _items(todo) == {"fj", "fj-wrapper", "client_ids", "git-credential:git.example.com"}

    done, errors = fj.apply_changes(opts, todo, system="Linux")
    assert errors == [] and calls == ["0.6.0"]
    assert (_bin(sandbox) / "fj").read_bytes() == fj.fj_wrapper_sh("0.6.0").encode()
    assert os.access(_bin(sandbox) / "fj", os.X_OK)
    assert not (_bin(sandbox) / "fj.cmd").exists(), "fj.cmd is Windows-only"
    assert fj.client_ids_path("Linux").read_text(encoding="utf-8") == (
        f"git.example.com {fj.FORGEJO_TEA_CLIENT_ID}  {fj.CLIENT_IDS_MARKER}\n"
    )
    assert store == fj.git_credential_settings("git.example.com", fj.FORGEJO_GCM_CLIENT_ID)
    assert fj.pending_changes(opts, windows=False, system="Linux") == []


def test_linux_moves_the_fj_rig_installed_into_bin_dir(sandbox, monkeypatch):
    _stub_git(monkeypatch, gcm=False)
    monkeypatch.setattr(fj, "install_fj", _no_install)
    old = b"\x7fELF the fj rig used to install into bin_dir"
    monkeypatch.setitem(fj.FJ_BINARY_SHA256, "0.6.0", {"forgejo-cli-x86_64-linux.tar.gz": _sha(old)})
    (_bin(sandbox)).mkdir(parents=True)
    (_bin(sandbox) / "fj").write_bytes(old)
    _on_path(monkeypatch, _bin(sandbox))
    opts = _opts()

    todo = fj.pending_changes(opts, windows=False, system="Linux")
    [wrapper_row] = [t for t in todo if t[1] == "fj-wrapper"]
    assert wrapper_row[0] == "modified" and "moves to" in wrapper_row[3]

    done, errors = fj.apply_changes(opts, todo, system="Linux")
    assert errors == [], errors
    assert fj.fj_store_binary("0.6.0", "Linux").read_bytes() == old
    assert fj.is_rig_wrapper(_bin(sandbox) / "fj")
    assert fj.pending_changes(opts, windows=False, system="Linux") == []


def test_a_foreign_fj_in_bin_dir_is_never_touched(sandbox, monkeypatch):
    _stub_git(monkeypatch, gcm=False)
    monkeypatch.setattr(fj, "install_fj", _fake_install([]))
    (_bin(sandbox)).mkdir(parents=True)
    mine = b"#!/bin/sh\necho my own fj\n"
    (_bin(sandbox) / "fj").write_bytes(mine)
    _on_path(monkeypatch, _bin(sandbox))
    opts = _opts()

    todo = fj.pending_changes(opts, windows=False, system="Linux")
    [row] = [t for t in todo if t[1] == "fj-wrapper"]
    assert row[0] == "modified" and "not rig's" in row[3]
    done, errors = fj.apply_changes(opts, todo, system="Linux")
    assert any("not rig's" in e for e in errors)
    assert (_bin(sandbox) / "fj").read_bytes() == mine
    assert "fj-wrapper" in _items(fj.pending_changes(opts, windows=False, system="Linux"))


def test_a_stale_wrapper_is_rewritten_and_old_versions_leave_the_store(sandbox, monkeypatch):
    _stub_git(monkeypatch, gcm=False)
    calls: list = []
    monkeypatch.setattr(fj, "install_fj", _fake_install(calls))
    _on_path(monkeypatch, _bin(sandbox))
    monkeypatch.setitem(fj.FJ_RELEASE_SHA256, "0.5.0", fj.FJ_RELEASE_SHA256["0.6.0"])
    old = _opts(version="0.5.0")
    fj.apply_changes(old, fj.pending_changes(old, windows=False, system="Linux"), system="Linux")
    assert fj.fj_store_binary("0.5.0", "Linux").is_file()

    new = _opts()
    todo = fj.pending_changes(new, windows=False, system="Linux")
    assert ("modified", "fj-wrapper") in {(d, i) for d, i, _, _ in todo}
    assert "fj" in _items(todo)
    done, errors = fj.apply_changes(new, todo, system="Linux")
    assert errors == [] and calls == ["0.5.0", "0.6.0"]
    assert fj.is_rig_wrapper(_bin(sandbox) / "fj")
    assert (_bin(sandbox) / "fj").read_bytes() == fj.fj_wrapper_sh("0.6.0").encode()
    assert not fj.fj_store_binary("0.5.0", "Linux").parent.exists()
    assert fj.pending_changes(new, windows=False, system="Linux") == []


def test_windows_moves_fj_exe_out_of_bin_dir_and_writes_both_wrappers(sandbox, monkeypatch):
    _stub_git(monkeypatch, gcm=False)
    monkeypatch.setattr(fj, "install_fj", _no_install)
    old = b"MZ the fj.exe rig used to install into bin_dir"
    monkeypatch.setitem(fj.FJ_BINARY_SHA256, "0.6.0", {"forgejo-cli-x86_64-windows.zip": _sha(old)})
    monkeypatch.setattr(fj.platform, "machine", lambda: "AMD64")
    _bin(sandbox).mkdir(parents=True)
    (_bin(sandbox) / "fj.exe").write_bytes(old)
    _on_path(monkeypatch, _bin(sandbox))
    opts = _opts()

    todo = fj.pending_changes(opts, windows=False, system="Windows")
    assert {"fj", "fj-wrapper", "fj.cmd", "fj.exe"} <= _items(todo)
    done, errors = fj.apply_changes(opts, todo, system="Windows")
    assert errors == [], errors
    assert sorted(p.name for p in _bin(sandbox).iterdir()) == ["fj", "fj.cmd"]
    assert fj.fj_store_binary("0.6.0", "Windows").read_bytes() == old
    assert (_bin(sandbox) / "fj.cmd").read_bytes() == fj.fj_wrapper_cmd("0.6.0").encode("ascii")
    assert fj.pending_changes(opts, windows=False, system="Windows") == []


def test_windows_foreign_fj_exe_beside_the_wrapper_is_reported_not_moved(sandbox, monkeypatch):
    _stub_git(monkeypatch, gcm=False)
    monkeypatch.setattr(fj, "install_fj", _fake_install([]))
    monkeypatch.setattr(fj.platform, "machine", lambda: "AMD64")
    _bin(sandbox).mkdir(parents=True)
    (_bin(sandbox) / "fj.exe").write_bytes(b"MZ somebody else's fj")
    _on_path(monkeypatch, _bin(sandbox))
    opts = _opts()
    [row] = [t for t in fj.pending_changes(opts, windows=False, system="Windows") if t[1] == "fj.exe"]
    assert "not rig's" in row[3]
    done, errors = fj.apply_changes(opts, fj.pending_changes(opts, windows=False, system="Windows"), system="Windows")
    assert any("fj.exe is not rig's" in e for e in errors)
    assert (_bin(sandbox) / "fj.exe").read_bytes() == b"MZ somebody else's fj"


def test_macos_keeps_homebrews_fj_and_only_adds_the_wrapper(sandbox, monkeypatch, tmp_path):
    _stub_git(monkeypatch, gcm=False)
    monkeypatch.setattr(fj, "install_fj", _no_install)
    brew = _fake_fj(tmp_path / "homebrew" / "bin" / "fj", label="BREW")
    _on_path(monkeypatch, _bin(sandbox), brew.parent)
    opts = _opts()

    todo = fj.pending_changes(opts, windows=False, system="Darwin")
    assert _items(todo) == {"fj-wrapper", "client_ids"}
    done, errors = fj.apply_changes(opts, todo, system="Darwin")
    assert errors == []
    assert fj.find_real_fj(system="Darwin") == str(brew), "rig's own calls skip the wrapper"
    assert fj.pending_changes(opts, windows=False, system="Darwin") == []


def test_macos_without_fj_runs_brew(sandbox, monkeypatch):
    _stub_git(monkeypatch, gcm=False)
    calls: list = []
    monkeypatch.setattr(fj, "install_fj", lambda v, *, system=None, fetch=None: calls.append(system) or "brew")
    _on_path(monkeypatch, _bin(sandbox))
    todo = fj.pending_changes(_opts(), windows=False, system="Darwin")
    [row] = [t for t in todo if t[1] == "fj"]
    assert "brew install forgejo-cli" in row[3]
    fj.apply_changes(_opts(), todo, system="Darwin")
    assert calls == ["Darwin"]


def test_an_fj_earlier_on_path_than_the_wrapper_is_drift(sandbox, monkeypatch, tmp_path):
    _stub_git(monkeypatch, gcm=False)
    monkeypatch.setattr(fj, "install_fj", _no_install)
    brew = _fake_fj(tmp_path / "homebrew" / "bin" / "fj", label="BREW")
    _on_path(monkeypatch, brew.parent, _bin(sandbox))
    opts = _opts()
    [row] = [t for t in fj.pending_changes(opts, windows=False, system="Darwin") if t[1] == "fj-path"]
    assert row[2] == brew and str(_bin(sandbox)) in row[3]
    done, errors = fj.apply_changes(opts, fj.pending_changes(opts, windows=False, system="Darwin"), system="Darwin")
    assert any("put" in e and "on PATH" in e for e in errors)
    assert fj.is_rig_wrapper(_bin(sandbox) / "fj"), "the wrapper is still written"


def test_path_shadow_ignores_a_path_without_bin_dir_and_without_fj(sandbox, monkeypatch, tmp_path):
    (tmp_path / "usr-bin").mkdir()
    _on_path(monkeypatch, tmp_path / "usr-bin")  # e.g. a launchd daemon's minimal PATH
    assert fj.path_shadow(_bin(sandbox), "Darwin") is None
    shadow = _fake_fj(tmp_path / "usr-bin" / "fj")
    assert fj.path_shadow(_bin(sandbox), "Darwin") == shadow


def test_windows_path_shadow_counts_cmd_and_exe_names(sandbox, monkeypatch, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "fj.cmd").write_bytes(b"@echo off\r\n")
    _on_path(monkeypatch, other, _bin(sandbox))
    assert fj.path_shadow(_bin(sandbox), "Windows") is not None
    _on_path(monkeypatch, _bin(sandbox), other)
    assert fj.path_shadow(_bin(sandbox), "Windows") is None


def test_install_false_leaves_the_fj_command_alone(sandbox, monkeypatch):
    _stub_git(monkeypatch, gcm=False)
    items = _items(fj.pending_changes(_opts(install=False), windows=False, system="Linux"))
    assert items == {"client_ids"}


def test_find_real_fj_prefers_rigs_copy_and_never_returns_a_wrapper(sandbox, monkeypatch, tmp_path):
    wrapper = _write_wrapper(_bin(sandbox))
    _on_path(monkeypatch, wrapper.parent)
    assert fj.find_real_fj(system="Linux") is None
    other = _fake_fj(tmp_path / "usr" / "fj")
    _on_path(monkeypatch, wrapper.parent, other.parent)
    assert fj.find_real_fj(system="Linux") == str(other)
    mine = _fake_fj(fj.fj_store_binary("0.6.0", "Linux"))
    assert fj.find_real_fj(system="Linux") == str(mine)
    assert fj.find_real_fj("0.6.0", system="Linux") == str(mine)


def test_windows_sets_the_user_env_var(sandbox, monkeypatch):
    _stub_git(monkeypatch, gcm=False)
    env: dict[str, str] = {}
    monkeypatch.setattr(fj, "read_windows_user_env", lambda name: env.get(name))
    monkeypatch.setattr(fj, "set_windows_user_env", lambda name, value: env.__setitem__(name, value))
    opts = _opts(install=False)
    todo = fj.pending_changes(opts, windows=True)
    assert "FJ_FALLBACK_HOST" in _items(todo)
    fj.apply_changes(opts, todo)
    assert env == {"FJ_FALLBACK_HOST": "https://git.example.com"}


def test_failed_install_does_not_block_the_other_steps(sandbox, monkeypatch):
    _stub_git(monkeypatch, gcm=False)
    _on_path(monkeypatch, _bin(sandbox))

    def boom(v, *, system=None, fetch=None):
        raise fj.FjError("fj: no network")

    monkeypatch.setattr(fj, "install_fj", boom)
    done, errors = fj.apply_changes(_opts(), fj.pending_changes(_opts(), windows=False, system="Linux"), system="Linux")
    assert errors == ["fj: no network"]
    assert fj.client_ids_path("Linux").is_file()
    assert fj.is_rig_wrapper(_bin(sandbox) / "fj")


# ── git credential helper detection ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "helper,gcm",
    [
        ("manager", True),
        ("manager-core", True),
        ("git-credential-manager", True),
        ('"C:/Program Files/Git/mingw64/bin/git-credential-manager.exe"', True),
        (r'"C:\Program Files\Git\mingw64\bin\git-credential-manager.exe"', True),
        (r"/mnt/c/Program\ Files/Git/mingw64/bin/git-credential-manager.exe", True),  # WSL
        ("/usr/local/share/gcm-core/git-credential-manager-core", True),
        ("C:/Git/mingw64/bin/Git-Credential-Manager.EXE", True),
        ("git-credential-manager.exe --no-ui", True),
        ("store", False),
        ("osxkeychain", False),
        ("cache --timeout=3600", False),
        ("/usr/lib/git-core/git-credential-libsecret", False),
        ("!f() { echo password=x; }; f", False),
        ("", False),
    ],
)
def test_is_gcm_helper(helper, gcm):
    assert fj.is_gcm_helper(helper) is gcm


def test_git_uses_gcm_honours_the_empty_value_reset(monkeypatch):
    outputs = {"text": ""}

    class R:
        @property
        def stdout(self):
            return outputs["text"]

    monkeypatch.setattr(fj.subprocess, "run", lambda *a, **kw: R())
    outputs["text"] = "manager\n"
    assert fj.git_uses_gcm()
    outputs["text"] = "store\n/mnt/c/Program\\ Files/Git/mingw64/bin/git-credential-manager.exe\n"
    assert fj.git_uses_gcm()
    outputs["text"] = "manager\n\nstore\n"  # an empty value clears the list before it
    assert not fj.git_uses_gcm()
    outputs["text"] = ""
    assert not fj.git_uses_gcm()


# ── plan / runner / drift wiring ────────────────────────────────────────────────────────────


def _loaded(tmp_path, data):
    from riglib.config import LoadedConfig

    return LoadedConfig(data=data, repo_root=tmp_path)


def test_plan_emits_one_fj_action(sandbox, tmp_path):
    from riglib.plan import InstallPlan, _build_fj

    plan = InstallPlan(actions=[])
    _build_fj(_loaded(tmp_path, {"fj": {"host": "git.example.com"}}), plan)
    [action] = plan.actions
    assert action.kind == "provision_fj" and action.category == "fj"
    assert action.options["hosts"] == ["git.example.com"]


def test_plan_skips_absent_or_disabled_fj(sandbox, tmp_path):
    from riglib.plan import InstallPlan, _build_fj

    for data in ({}, {"fj": {"enabled": False, "host": "git.example.com"}}):
        plan = InstallPlan(actions=[])
        _build_fj(_loaded(tmp_path, data), plan)
        assert plan.actions == []


def test_env_area_exports_the_fallback_host_on_posix(sandbox, tmp_path, monkeypatch):
    from riglib.plan import InstallPlan, _build_env

    monkeypatch.setattr(fj, "implied_env_vars", lambda block: {"FJ_FALLBACK_HOST": "https://git.example.com"})
    plan = InstallPlan(actions=[])
    _build_env(_loaded(tmp_path, {"fj": {"host": "git.example.com"}}), plan)  # no env block at all
    [action] = plan.actions
    assert action.kind == "provision_env"
    assert action.options["vars"] == {"FJ_FALLBACK_HOST": "https://git.example.com"}

    plan = InstallPlan(actions=[])
    _build_env(_loaded(tmp_path, {"fj": {"host": "git.example.com"},
                                  "env": {"vars": {"FJ_FALLBACK_HOST": "https://mine.example"}}}), plan)
    assert plan.actions[0].options["vars"]["FJ_FALLBACK_HOST"] == "https://mine.example"  # hand-set wins


def test_runner_dry_run_and_drift_share_the_comparison(sandbox, tmp_path, monkeypatch):
    from riglib.actions import runner
    from riglib.drift import DriftReport, _check_fj
    from riglib.plan import InstallPlan, _build_fj

    _stub_git(monkeypatch, gcm=False)
    # the Windows user env already carries the host, so only client_ids is out of date on any OS
    monkeypatch.setattr(fj, "read_windows_user_env", lambda name: "https://git.example.com")
    plan = InstallPlan(actions=[])
    _build_fj(_loaded(tmp_path, {"fj": {"host": "git.example.com", "install": False}}), plan)
    [action] = plan.actions

    report = DriftReport()
    _check_fj(action, report)
    assert [(i.direction, i.category, i.item) for i in report.items] == [("missing", "fj", "client_ids")]

    monkeypatch.setenv("RIG_FJ_DRY_RUN", "1")
    res = runner._do_provision_fj(action, "backup")
    assert res.status == "skipped" and "dry-run" in res.detail
    assert not fj.client_ids_path().exists()

    monkeypatch.delenv("RIG_FJ_DRY_RUN")
    assert runner._do_provision_fj(action, "backup").status == "updated"
    assert runner._do_provision_fj(action, "backup").status == "skipped"


def test_status_and_apply_agree_on_the_fj_command(sandbox, tmp_path, monkeypatch):
    """The wrapper missing is drift; apply fixes it; status is then clean (Linux layout)."""
    from riglib.actions import runner
    from riglib.drift import DriftReport, _check_fj
    from riglib.plan import InstallPlan, _build_fj

    _stub_git(monkeypatch, gcm=False)
    monkeypatch.setattr(fj.platform, "system", lambda: "Linux")
    monkeypatch.setattr(fj, "read_windows_user_env", lambda name: "https://git.example.com")
    monkeypatch.setattr(fj, "install_fj", _fake_install([]))
    monkeypatch.delenv("RIG_FJ_DRY_RUN", raising=False)
    _on_path(monkeypatch, _bin(sandbox))
    plan = InstallPlan(actions=[])
    _build_fj(_loaded(tmp_path, {"fj": {"host": "git.example.com"}}), plan)
    [action] = plan.actions

    report = DriftReport()
    _check_fj(action, report)
    assert {i.item for i in report.items} == {"fj", "fj-wrapper", "client_ids"}

    res = runner._do_provision_fj(action, "backup")
    assert res.status == "updated" and "fj ship" in res.detail, res.detail
    report = DriftReport()
    _check_fj(action, report)
    assert report.items == []
    assert runner._do_provision_fj(action, "backup").status == "skipped"


def test_wrapper_runs_fj_ship_under_git_sh_from_cygwin():
    # ship drives Windows-native python/git/jq; only Git for Windows' sh converts path args for
    # them, so under Cygwin `fj ship` re-execs itself there, handing paths over in C:/ form.
    text = fj.fj_wrapper_sh("0.6.0")
    ship = text[text.index('if [ "${1-}" = ship ]'):text.index("@SHIP@") if "@SHIP@" in text else None]
    assert "CYGWIN*)" in ship and "RIG_FJ_SHIP_MSYS" in ship
    assert '"${g%/*}/../bin/sh.exe" "${g%/*}/../../bin/sh.exe"' in ship
    assert 'AGENT_TOOLS_ROOT="$(cygpath -m "$AGENT_TOOLS_ROOT")"' in ship and "eval" not in ship
    assert 'exec "$s" "$(cygpath -w "$0")" "$@"' in ship  # `ship` stays the first argument


@pytest.mark.skipif(not (os.name == "nt" and Path(r"C:\cygwin64\bin\bash.exe").is_file()),
                    reason="needs Cygwin and Git for Windows")
def test_cygwin_fj_ship_reaches_ship_sh_under_git_sh(tmp_path):
    import subprocess

    bindir, at = tmp_path / "bin", tmp_path / "at"
    (at / "ci" / "ship").mkdir(parents=True)
    bindir.mkdir()
    wrapper = bindir / "fj"
    wrapper.write_text(fj.fj_wrapper_sh("0.6.0"), encoding="utf-8", newline="\n")
    ship = at / "ci" / "ship" / "ship.sh"
    ship.write_text('#!/bin/sh\necho "under=$(uname -s) args=$*"\n', encoding="utf-8", newline="\n")
    script = tmp_path / "run.sh"
    script.write_text(
        "chmod +x \"$(cygpath -u '{w}')\" \"$(cygpath -u '{s}')\"\n"
        "cd /tmp && AGENT_TOOLS_ROOT=\"$(cygpath -u '{a}')\" \"$(cygpath -u '{w}')\" ship 7 --dry-run\n"
        .format(w=wrapper, s=ship, a=at), encoding="utf-8", newline="\n")
    out = subprocess.run([r"C:\cygwin64\bin\bash.exe", "-l", str(script).replace("\\", "/")],
                         capture_output=True, text=True, timeout=120)
    assert "under=MINGW" in out.stdout or "under=MSYS" in out.stdout, out.stdout + out.stderr
    assert "args=7 --dry-run" in out.stdout


# ── a repo rig.yaml may only name its fj.host (security: no machine-wide auth from a clone) ──────


def _repo_with(tmp_path, text):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "rig.yaml").write_text(f"version: 1\n{text}\n", encoding="utf-8")
    return repo


@pytest.mark.parametrize("key", ["client_id: evil-app", "git_client_id: evil-gcm", "enabled: false",
                                 "install: false", "bin_dir: /tmp/evil", "version: 0.6.0",
                                 "git_credentials: false"])
def test_repo_layer_may_not_set_machine_wide_fj_keys(sandbox, tmp_path, key):
    from riglib.config import load

    repo = _repo_with(tmp_path, f"fj: {{host: git.example.com, {key}}}")
    with pytest.raises(ConfigError, match="global-only config key") as exc:
        load(repo)
    assert exc.value.schema_path == f"fj.{key.split(':')[0]}"


def _posix_env_vars(monkeypatch):
    real = fj.implied_env_vars
    monkeypatch.setattr(fj, "implied_env_vars", lambda block: real(block, windows=False))


def test_a_repo_host_never_becomes_the_machine_default(sandbox, tmp_path, monkeypatch):
    from riglib.config import global_config_path, load
    from riglib.plan import InstallPlan, _build_env, _build_fj

    gpath = global_config_path()
    gpath.parent.mkdir(parents=True, exist_ok=True)
    gpath.write_text("version: 1\nfj: {host: git.hyperide.ai}\n", encoding="utf-8")
    loaded = load(_repo_with(tmp_path, "fj: {host: forge.example}"))
    assert loaded.data["fj"]["host"] == "forge.example"  # the repo's own logins use its host

    plan = InstallPlan(actions=[])
    _build_fj(loaded, plan)
    [action] = plan.actions
    assert action.options["hosts"] == ["forge.example", "git.hyperide.ai"]
    assert action.options["fallback_host"] == "git.hyperide.ai"  # Windows: HKCU FJ_FALLBACK_HOST

    _posix_env_vars(monkeypatch)
    plan = InstallPlan(actions=[])
    _build_env(loaded, plan)
    assert plan.actions[0].options["vars"] == {"FJ_FALLBACK_HOST": "https://git.hyperide.ai"}


def test_without_a_global_host_rig_sets_no_fallback_host(sandbox, tmp_path, monkeypatch):
    from riglib.config import load
    from riglib.plan import InstallPlan, _build_env, _build_fj

    loaded = load(_repo_with(tmp_path, "fj: {host: forge.example}"))
    plan = InstallPlan(actions=[])
    _build_fj(loaded, plan)
    [action] = plan.actions
    assert action.options["fallback_host"] is None and action.options["hosts"] == ["forge.example"]
    _stub_git(monkeypatch, gcm=False)
    monkeypatch.setattr(fj, "read_windows_user_env", lambda name: None)
    assert "FJ_FALLBACK_HOST" not in _items(fj.pending_changes(action.options, windows=True))

    _posix_env_vars(monkeypatch)
    plan = InstallPlan(actions=[])
    _build_env(loaded, plan)
    assert plan.actions == []


def test_cygwin_reexec_converts_paths_without_eval():
    [sh] = [data for path, data in fj.fj_wrapper_files(_opts(), "Linux") if path.name == "fj"]
    assert b"eval" not in sh
    assert b'AGENT_TOOLS_ROOT="$(cygpath -m "$AGENT_TOOLS_ROOT")"' in sh


def test_write_atomic_leaves_no_temp_file_behind_on_failure(tmp_path, monkeypatch):
    def fail(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(fj.os, "replace", fail)
    with pytest.raises(OSError, match="disk full"):
        fj._write_atomic(tmp_path / "bin" / "fj", b"#!/bin/sh\n")
    assert list((tmp_path / "bin").iterdir()) == []
