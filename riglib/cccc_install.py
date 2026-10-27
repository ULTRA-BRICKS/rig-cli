"""Pinned, verified install of the official ``cccc`` complexity analyzer (moznion/cccc).

``rig lint check`` (:mod:`riglib.lint_check`) measures cognitive/cyclomatic complexity with the
upstream ``cccc`` binary. A mutable ``cccc`` found on ``$PATH`` would make the gate's verdict
depend on whatever happens to be installed, so rig owns ONE binary:

- the release is pinned (:data:`PINNED_VERSION`, an immutable GitHub release) and every platform
  carries TWO digests — the release tarball and the binary extracted from it;
- :func:`install` downloads the tarball, verifies its digest BEFORE touching its contents,
  extracts only the single ``cccc`` member (a traversal, link or device member makes the whole
  archive untrusted), verifies the binary digest, and only then renames it into place — the
  analyzer is never executed before it has been verified;
- :func:`resolve_verified_binary` is the only way production code obtains an executable path: it
  re-hashes the managed file on every call, so a swapped or truncated binary fails closed.

There is deliberately no environment or flag override in production; tests inject a fake fetcher
and a patched pin table instead. Stdlib-only at import time.
"""

from __future__ import annotations

import hashlib
import http.client
import io
import os
import platform
import stat
import sys
import tarfile
import tempfile
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

PINNED_VERSION = "1.8.4"
_RELEASE_BASE = f"https://github.com/moznion/cccc/releases/download/v{PINNED_VERSION}"
_MAX_TARBALL_BYTES = 200 * 1024 * 1024
# the release tarball's layout: one top-level regular file of this exact name (CI's `tar -x` uses it too)
BINARY_MEMBER = "cccc"
_MAX_BINARY_BYTES = 256 * 1024 * 1024
_DOWNLOAD_TIMEOUT_S = 120


class CcccInstallError(Exception):
    """Base of every analyzer install/verification failure (all fail closed)."""


class AnalyzerMissing(CcccInstallError):
    """The managed analyzer is not installed (or could not be downloaded)."""


class AnalyzerMismatch(CcccInstallError):
    """A digest, file type or extraction check failed — the bytes are not the pinned release."""


class UnsupportedPlatform(CcccInstallError):
    """No pinned release exists for this OS/CPU."""


@dataclass(frozen=True)
class Pin:
    target: str
    tarball_sha256: str
    binary_sha256: str

    @property
    def url(self) -> str:
        return f"{_RELEASE_BASE}/cccc-v{PINNED_VERSION}-{self.target}.tar.gz"


# (normalized sys.platform, normalized machine) -> release digests (contract section 1).
_PINS: dict[tuple[str, str], Pin] = {
    ("linux", "x86_64"): Pin(
        "x86_64-unknown-linux-musl",
        "64b3084e05cf2f75d4af57268a2b898526c04be7bc31d0a36708081b13a64f97",
        "e09bc0ec6e9b8e55a57188b0137fdb38a11bba3cc115be8f6ef28df7f4a1d578",
    ),
    ("linux", "aarch64"): Pin(
        "aarch64-unknown-linux-musl",
        "caad978e8595db0ba07585aad6b63affdee0d28f191b620d1cfd02e51e31186b",
        "c1925f82c8b1e319568b4defbadb6da419a60734dce84a2c4bbd5885ea89c41b",
    ),
    ("darwin", "x86_64"): Pin(
        "x86_64-apple-darwin",
        "4fde0cf0eec82dc78a0587f1bfae5ecf154bfb2f3959cff9deba2b35d430c662",
        "6d7765a99d052ab11b289d71a71e52a14a9c3ba9f4f9ebd638e9267f35f86a1f",
    ),
    ("darwin", "arm64"): Pin(
        "aarch64-apple-darwin",
        "f8f015ce3ec60db09bb6823aae9dc9e6bfdb13d79ae50a1bcfd3518357deeda9",
        "6e864a71673598faac0701a0cf29fd6c4d2105863bdbc53d3b5bb4d0f1b8b992",
    ),
}

Fetch = Callable[[str], bytes]


def platform_key() -> tuple[str, str]:
    """This machine's pin-table key: ``(linux|darwin|<sys.platform>, x86_64|aarch64|arm64|…)``."""
    system = "linux" if sys.platform.startswith("linux") else sys.platform
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64", "x64": "x86_64"}.get(machine, machine)
    if system == "linux" and machine == "arm64":
        machine = "aarch64"
    if system == "darwin" and machine == "aarch64":
        machine = "arm64"
    return system, machine


def pin_for(key: tuple[str, str] | None = None) -> Pin:
    key = key or platform_key()
    pin = _PINS.get(key)
    if pin is None:
        supported = ", ".join(f"{s}/{m}" for s, m in sorted(_PINS))
        raise UnsupportedPlatform(
            f"no pinned cccc v{PINNED_VERSION} release for {key[0]}/{key[1]} (supported: {supported}); "
            "`rig lint check` cannot run here — set lint_checks.enabled: false to opt out explicitly"
        )
    return pin


def managed_binary_path() -> Path:
    """``${XDG_DATA_HOME:-~/.local/share}/rig/cccc/v<version>/cccc`` — rig's own store, NOT on PATH."""
    base = Path(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"))
    return base / "rig" / "cccc" / f"v{PINNED_VERSION}" / "cccc"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _other_versions() -> list[str]:
    store = managed_binary_path().parent.parent
    try:
        return sorted(p.name for p in store.iterdir() if p.is_dir() and p.name != f"v{PINNED_VERSION}")
    except OSError:
        return []


def resolve_verified_binary(key: tuple[str, str] | None = None) -> Path:
    """The managed analyzer, proven byte-identical to the pin, or a typed failure.

    Raises :class:`UnsupportedPlatform`, :class:`AnalyzerMissing` (absent, non-regular or not
    executable) or :class:`AnalyzerMismatch` (digest differs). Messages name ``rig apply commit``
    as the remedy because that is the reconcile that installs the pin.
    """
    pin = pin_for(key)
    path = managed_binary_path()
    try:
        info = path.lstat()
    except OSError:
        others = _other_versions()
        hint = f" (found other version dir(s) {', '.join(others)}; the pinned one is v{PINNED_VERSION})" if others else ""
        raise AnalyzerMissing(
            f"cccc v{PINNED_VERSION} is not installed at {path}{hint}; run `rig apply commit` to install the pinned analyzer"
        ) from None
    if not stat.S_ISREG(info.st_mode):
        raise AnalyzerMismatch(f"{path} is not a regular file (symlinks/directories are refused); run `rig apply commit`")
    try:
        actual = file_sha256(path)
    except OSError as exc:  # unreadable/vanished between lstat and read: same typed, fail-closed contract
        raise AnalyzerMismatch(f"{path} cannot be read ({type(exc).__name__}: {exc}); run `rig apply commit` to reinstall the pinned analyzer") from exc
    if actual != pin.binary_sha256:
        raise AnalyzerMismatch(
            f"{path} sha256 {actual} != pinned {pin.binary_sha256} (cccc v{PINNED_VERSION}); "
            "run `rig apply commit` to reinstall the pinned analyzer"
        )
    # hashed first: a wrong non-executable file is a MISMATCH (on_conflict applies), only the pinned
    # bytes without the exec bit are the benign "not executable" repair
    if not os.access(path, os.X_OK):
        raise AnalyzerMissing(f"{path} is not executable; run `rig apply commit` to reinstall the pinned analyzer")
    return path


@dataclass(frozen=True)
class InstallResult:
    path: Path
    changed: bool
    detail: str


def _download(url: str) -> bytes:
    if not url.startswith("https://"):
        raise AnalyzerMismatch(f"refusing non-https analyzer URL {url}")
    try:
        with urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT_S) as resp:  # noqa: S310 — https checked above
            data = resp.read(_MAX_TARBALL_BYTES + 1)
    except (OSError, http.client.HTTPException) as exc:  # IncompleteRead & co. are NOT OSErrors
        raise AnalyzerMissing(f"download of {url} failed: {type(exc).__name__}: {exc}") from exc
    return data


def _member_is_safe(member: tarfile.TarInfo) -> bool:
    name = member.name
    parts = Path(name).parts
    if name.startswith("/") or ".." in parts:
        return False
    return member.isreg() or member.isdir()


def _extract_binary(tarball: bytes) -> bytes:
    """The bytes of the single regular ``cccc`` member; any unsafe member rejects the archive."""
    try:
        with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
            members = tar.getmembers()
            for member in members:
                if not _member_is_safe(member):
                    raise AnalyzerMismatch(f"tarball member {member.name!r} is a link/device/traversal — archive refused")
            wanted = [m for m in members if m.isreg() and Path(m.name).name == BINARY_MEMBER]
            if len(wanted) != 1 or wanted[0].name != BINARY_MEMBER:
                raise AnalyzerMismatch(f"tarball must contain exactly one top-level `{BINARY_MEMBER}` file, found {[m.name for m in wanted]}")
            member = wanted[0]
            if member.size <= 0 or member.size > _MAX_BINARY_BYTES:
                raise AnalyzerMismatch(f"tarball `cccc` has an implausible size ({member.size} bytes)")
            fh = tar.extractfile(member)
            if fh is None:
                raise AnalyzerMismatch("tarball `cccc` is unreadable")
            data = fh.read(_MAX_BINARY_BYTES + 1)
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise AnalyzerMismatch(f"tarball is not a readable gzip tar archive: {exc}") from exc
    return data


def install(*, fetch: Fetch | None = None, key: tuple[str, str] | None = None) -> InstallResult:
    """Install the pinned analyzer; a no-op when the managed file already verifies.

    Verification order: tarball sha256 → safe single-member extraction → binary sha256 → atomic
    rename of a temp file living in the destination directory. Nothing is executed and the final
    path is never created on any failure.
    """
    pin = pin_for(key)
    dest = managed_binary_path()
    try:
        resolve_verified_binary(key)
        return InstallResult(dest, False, f"cccc v{PINNED_VERSION} already installed and verified")
    except (AnalyzerMissing, AnalyzerMismatch):
        pass  # absent or wrong bytes at the managed path: replaced below, only by verified bytes
    tarball = (fetch or _download)(pin.url)
    if len(tarball) > _MAX_TARBALL_BYTES:
        raise AnalyzerMismatch(f"download of {pin.url} exceeds {_MAX_TARBALL_BYTES} bytes")
    actual = hashlib.sha256(tarball).hexdigest()
    if actual != pin.tarball_sha256:
        raise AnalyzerMismatch(f"tarball sha256 {actual} != pinned {pin.tarball_sha256} ({pin.url}); nothing installed")
    binary = _extract_binary(tarball)
    actual = hashlib.sha256(binary).hexdigest()
    if actual != pin.binary_sha256:
        raise AnalyzerMismatch(f"extracted cccc sha256 {actual} != pinned {pin.binary_sha256}; nothing installed")
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".cccc-", dir=dest.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(binary)
        tmp.chmod(0o755)
        if dest.is_symlink() or dest.is_dir():
            raise AnalyzerMismatch(f"{dest} is a symlink/directory; remove it and re-run `rig apply commit`")
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return InstallResult(dest, True, f"installed cccc v{PINNED_VERSION} ({pin.target}) → {dest}")


@dataclass(frozen=True)
class AnalyzerState:
    """Read-only verdict for ``rig status``: ``ok`` | ``missing`` | ``mismatch`` | ``unsupported``."""

    state: str
    detail: str
    path: Path


def inspect(key: tuple[str, str] | None = None) -> AnalyzerState:
    path = managed_binary_path()
    try:
        resolve_verified_binary(key)
    except UnsupportedPlatform as exc:
        return AnalyzerState("unsupported", str(exc), path)
    except AnalyzerMissing as exc:
        return AnalyzerState("missing", str(exc), path)
    except AnalyzerMismatch as exc:
        return AnalyzerState("mismatch", str(exc), path)
    return AnalyzerState("ok", f"cccc v{PINNED_VERSION} verified", path)
