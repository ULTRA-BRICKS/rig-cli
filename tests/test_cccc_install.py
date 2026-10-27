"""The pinned cccc analyzer install (riglib.cccc_install): verify-then-extract, idempotent, fail closed.

No network: ``fetch`` serves a locally built tarball whose digests are patched into the pin table.
"""

from __future__ import annotations

import hashlib
import http.client
import io
import os
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from riglib import cccc_install as ci

pytestmark = pytest.mark.usefixtures("real_cccc_inspect")

KEY = ("linux", "x86_64")
FAKE_BINARY = b"#!/bin/sh\necho fake-cccc\n"


def _tar(members: list[tuple[str, bytes | str | None, str]]) -> bytes:
    """members: (name, data, kind) with kind file|symlink|hardlink; data is the link target for links."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data, kind in members:
            info = tarfile.TarInfo(name)
            if kind == "file":
                payload = data if isinstance(data, bytes) else b""
                info.size = len(payload)
                info.mode = 0o755
                tar.addfile(info, io.BytesIO(payload))
            else:
                info.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
                info.linkname = str(data)
                tar.addfile(info)
    return buf.getvalue()


def _pin_for(tarball: bytes, binary: bytes = FAKE_BINARY) -> ci.Pin:
    return ci.Pin("x86_64-unknown-linux-musl", hashlib.sha256(tarball).hexdigest(), hashlib.sha256(binary).hexdigest())


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path


def _install(tarball: bytes, monkeypatch, **kw):
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        return tarball

    monkeypatch.setitem(ci._PINS, KEY, _pin_for(tarball, kw.get("binary", FAKE_BINARY)))
    return ci.install(fetch=fetch, key=KEY), calls


def test_pin_table_has_all_four_platforms_with_contract_digests():
    assert ci.PINNED_VERSION == "1.8.4"
    linux = ci.pin_for(("linux", "x86_64"))
    assert linux.tarball_sha256 == "64b3084e05cf2f75d4af57268a2b898526c04be7bc31d0a36708081b13a64f97"
    assert linux.binary_sha256 == "e09bc0ec6e9b8e55a57188b0137fdb38a11bba3cc115be8f6ef28df7f4a1d578"
    assert linux.url == "https://github.com/moznion/cccc/releases/download/v1.8.4/cccc-v1.8.4-x86_64-unknown-linux-musl.tar.gz"
    for key in (("linux", "aarch64"), ("darwin", "x86_64"), ("darwin", "arm64")):
        pin = ci.pin_for(key)
        assert len(pin.tarball_sha256) == 64 and len(pin.binary_sha256) == 64
        assert pin.url.startswith("https://github.com/moznion/cccc/releases/download/v1.8.4/")


def test_unsupported_platform_fails_closed(store):
    with pytest.raises(ci.UnsupportedPlatform, match="freebsd/riscv"):
        ci.install(fetch=lambda url: pytest.fail("must not download"), key=("freebsd", "riscv"))
    assert ci.inspect(("freebsd", "riscv")).state == "unsupported"


def test_install_verifies_extracts_and_is_idempotent(store, monkeypatch):
    tarball = _tar([("cccc", FAKE_BINARY, "file"), ("README.md", b"r", "file"), ("LICENSE", b"l", "file")])
    result, calls = _install(tarball, monkeypatch)
    dest = ci.managed_binary_path()
    assert result.changed and result.path == dest
    assert dest.read_bytes() == FAKE_BINARY and dest.stat().st_mode & 0o777 == 0o755
    assert len(calls) == 1 and calls[0].endswith("x86_64-unknown-linux-musl.tar.gz")
    assert ci.resolve_verified_binary(KEY) == dest
    again, calls2 = _install(tarball, monkeypatch)
    assert not again.changed and calls2 == []  # verified file present: no download at all
    assert [p.name for p in dest.parent.iterdir()] == ["cccc"]  # no temp litter


def test_only_the_exact_top_level_member_name_is_extracted(store, monkeypatch):
    """The CI workflow runs `tar -x cccc`, which matches this exact name: the installer must read the same layout."""
    from riglib import lint_checks_artifacts as arts

    assert f'"$tmp" {ci.BINARY_MEMBER}\n' in arts.render_workflow("git+https://git.example.test/o/r@" + "a" * 40, True)
    for name in ("./cccc", "bin/cccc"):
        with pytest.raises(ci.AnalyzerMismatch, match="exactly one top-level `cccc`"):
            _install(_tar([(name, FAKE_BINARY, "file")]), monkeypatch)
        assert not ci.managed_binary_path().exists()


@pytest.mark.parametrize(
    "members,why",
    [
        ([("cccc", FAKE_BINARY, "file"), ("../evil", b"x", "file")], "traversal"),
        ([("cccc", FAKE_BINARY, "file"), ("/abs/evil", b"x", "file")], "absolute"),
        ([("cccc", "/etc/passwd", "symlink")], "symlink cccc"),
        ([("cccc", FAKE_BINARY, "file"), ("LICENSE", "/etc/passwd", "symlink")], "symlink sibling"),
        ([("cccc", FAKE_BINARY, "file"), ("other", "cccc", "hardlink")], "hardlink"),
        ([("README.md", b"r", "file")], "no cccc"),
        ([("cccc", FAKE_BINARY, "file"), ("sub/cccc", FAKE_BINARY, "file")], "two cccc"),
    ],
)
def test_unsafe_archive_is_refused_and_nothing_is_installed(store, monkeypatch, members, why):
    tarball = _tar(members)
    with pytest.raises(ci.AnalyzerMismatch):
        _install(tarball, monkeypatch)
    assert not ci.managed_binary_path().exists(), why
    version_dir = ci.managed_binary_path().parent
    assert not version_dir.exists() or list(version_dir.iterdir()) == [], why  # no temp litter either


def test_tarball_digest_mismatch_fails_closed(store, monkeypatch):
    good = _tar([("cccc", FAKE_BINARY, "file")])
    monkeypatch.setitem(ci._PINS, KEY, _pin_for(good))
    tampered = _tar([("cccc", b"#!/bin/sh\necho evil\n", "file")])
    with pytest.raises(ci.AnalyzerMismatch, match="tarball sha256"):
        ci.install(fetch=lambda url: tampered, key=KEY)
    assert not ci.managed_binary_path().exists()


def test_binary_digest_mismatch_fails_closed_even_with_valid_tarball_digest(store, monkeypatch):
    tarball = _tar([("cccc", b"#!/bin/sh\necho evil\n", "file")])
    # tarball digest matches the pin, the pinned BINARY digest is for different bytes
    monkeypatch.setitem(ci._PINS, KEY, _pin_for(tarball, binary=FAKE_BINARY))
    with pytest.raises(ci.AnalyzerMismatch, match="extracted cccc sha256"):
        ci.install(fetch=lambda url: tarball, key=KEY)
    assert not ci.managed_binary_path().exists()


def test_truncated_download_fails_closed(store, monkeypatch):
    tarball = _tar([("cccc", FAKE_BINARY * 2000, "file")])
    monkeypatch.setitem(ci._PINS, KEY, _pin_for(tarball, FAKE_BINARY * 2000))
    with pytest.raises(ci.AnalyzerMismatch):
        ci.install(fetch=lambda url: tarball[: len(tarball) // 2], key=KEY)
    assert not ci.managed_binary_path().exists()


def test_download_failure_is_analyzer_missing(store, monkeypatch):
    monkeypatch.setitem(ci._PINS, KEY, _pin_for(_tar([("cccc", FAKE_BINARY, "file")])))

    def boom(url: str) -> bytes:
        raise ci.AnalyzerMissing("network down")

    with pytest.raises(ci.AnalyzerMissing):
        ci.install(fetch=boom, key=KEY)


def test_default_fetch_refuses_non_https():
    with pytest.raises(ci.AnalyzerMismatch, match="non-https"):
        ci._download("http://example.invalid/cccc.tar.gz")


@pytest.mark.parametrize(
    "exc",
    [
        http.client.IncompleteRead(b"partial", 1000),  # a short read: HTTPException, NOT an OSError
        http.client.RemoteDisconnected("server closed the connection"),
        urllib.error.URLError("network is unreachable"),
        TimeoutError("timed out"),
    ],
)
def test_default_fetch_maps_every_transport_failure_to_analyzer_missing(monkeypatch, exc):
    def boom(*_a, **_kw):
        raise exc

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(ci.AnalyzerMissing, match="download of https://example.invalid/cccc.tar.gz failed"):
        ci._download("https://example.invalid/cccc.tar.gz")


def test_resolve_fails_closed_on_missing_modified_nonexec_and_symlink(store, monkeypatch):
    tarball = _tar([("cccc", FAKE_BINARY, "file")])
    monkeypatch.setitem(ci._PINS, KEY, _pin_for(tarball))
    with pytest.raises(ci.AnalyzerMissing, match="rig apply commit"):
        ci.resolve_verified_binary(KEY)
    ci.install(fetch=lambda url: tarball, key=KEY)
    dest = ci.managed_binary_path()
    dest.write_bytes(FAKE_BINARY + b"#tampered")
    dest.chmod(0o755)
    with pytest.raises(ci.AnalyzerMismatch, match="rig apply commit"):
        ci.resolve_verified_binary(KEY)
    assert ci.inspect(KEY).state == "mismatch"
    # reinstall repairs the mismatch (replaces only with verified bytes)
    assert ci.install(fetch=lambda url: tarball, key=KEY).changed
    assert ci.inspect(KEY).state == "ok"
    dest.chmod(0o644)
    with pytest.raises(ci.AnalyzerMissing, match="not executable"):
        ci.resolve_verified_binary(KEY)
    dest.unlink()
    real = store / "elsewhere"
    real.write_bytes(FAKE_BINARY)
    real.chmod(0o755)
    os.symlink(real, dest)
    with pytest.raises(ci.AnalyzerMismatch, match="not a regular file"):
        ci.resolve_verified_binary(KEY)


def test_unreadable_binary_is_a_typed_mismatch_not_a_traceback(store, monkeypatch):
    tarball = _tar([("cccc", FAKE_BINARY, "file")])
    monkeypatch.setitem(ci._PINS, KEY, _pin_for(tarball))
    ci.install(fetch=lambda url: tarball, key=KEY)

    def unreadable(_path):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(ci, "file_sha256", unreadable)
    with pytest.raises(ci.AnalyzerMismatch, match="cannot be read"):
        ci.resolve_verified_binary(KEY)
    assert ci.inspect(KEY).state == "mismatch"


def test_other_version_dir_is_named_when_pinned_one_is_missing(store, monkeypatch):
    monkeypatch.setitem(ci._PINS, KEY, _pin_for(_tar([("cccc", FAKE_BINARY, "file")])))
    old = ci.managed_binary_path().parent.parent / "v1.0.0"
    old.mkdir(parents=True)
    state = ci.inspect(KEY)
    assert state.state == "missing" and "v1.0.0" in state.detail


def test_platform_key_normalisation(monkeypatch):
    import platform
    import sys

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(platform, "machine", lambda: "AMD64")
    assert ci.platform_key() == ("linux", "x86_64")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    assert ci.platform_key() == ("linux", "aarch64")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(platform, "machine", lambda: "aarch64")
    assert ci.platform_key() == ("darwin", "arm64")


def test_real_tarball_installs_when_provided(store):
    """The real release tarball for this platform (``RIG_TEST_CCCC_TARBALL=<path>``) installs byte-identically — real pin digests."""
    path = os.environ.get("RIG_TEST_CCCC_TARBALL")
    if not path:
        pytest.skip("set RIG_TEST_CCCC_TARBALL to this platform's release tarball to run")
    data = Path(path).read_bytes()
    result = ci.install(fetch=lambda url: data)
    assert result.changed and ci.file_sha256(result.path) == ci.pin_for().binary_sha256
