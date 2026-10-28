"""Pinned, sha256-verified release binaries for the system linters ``rig lint`` can install.

Context: ShellCheck, shfmt and actionlint are standalone binaries with no package-manager route
that Rig could lock. A missing one is installed in user space from the project's official GitHub
release asset at an exact version, after the download matches the sha256 pinned below (the
``digest`` GitHub publishes per asset, read on 2026-10-27). Nothing is written to the repository
and no sudo/apt is involved; the binary lands in the Rig tool cache
(``<cache>/bin/<tool>-<version>/<tool>``, see :mod:`riglib.lint_tool_cache`). A platform with no
pinned asset has no install route: the caller reports it, never a pass.

Trust boundary: URL, sha256 and archive kind come from this table via the planner, never from
repository content. The download is https-only and size-bounded; an archive must contain exactly
one regular file whose basename is the tool, and unsafe members reject the archive. Stdlib-only at
import time.
"""

from __future__ import annotations

import hashlib
import http.client
import io
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Mapping

RELEASE_VERSIONS: Mapping[str, str] = {"shellcheck": "0.11.0", "shfmt": "3.14.1", "actionlint": "1.7.12"}
_DOWNLOAD_TIMEOUT_S = 120
_MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
Fetch = Callable[[str], bytes]


class ReleaseError(Exception):
    """A download that failed, did not match its pin, or held an unexpected archive."""


@dataclass(frozen=True)
class ReleaseAsset:
    url: str
    sha256: str
    kind: Literal["tar.gz", "raw"]


_SHELLCHECK = "https://github.com/koalaman/shellcheck/releases/download/v0.11.0/shellcheck-v0.11.0.{os}.{arch}.tar.gz"
_SHFMT = "https://github.com/mvdan/sh/releases/download/v3.14.1/shfmt_v3.14.1_{os}_{arch}"
_ACTIONLINT = "https://github.com/rhysd/actionlint/releases/download/v1.7.12/actionlint_1.7.12_{os}_{arch}.tar.gz"

# (tool, system, machine) with the `cccc_install.platform_key()` vocabulary -> asset.
_ASSETS: Mapping[tuple[str, str, str], ReleaseAsset] = {
    ("shellcheck", "linux", "x86_64"): ReleaseAsset(_SHELLCHECK.format(os="linux", arch="x86_64"), "b7af85e41cc99489dcc21d66c6d5f3685138f06d34651e6d34b42ec6d54fe6f6", "tar.gz"),
    ("shellcheck", "linux", "aarch64"): ReleaseAsset(_SHELLCHECK.format(os="linux", arch="aarch64"), "68a8133197a50beb8803f8d42f9908d1af1c5540d4bb05fdfca8c1fa47decefc", "tar.gz"),
    ("shellcheck", "darwin", "x86_64"): ReleaseAsset(_SHELLCHECK.format(os="darwin", arch="x86_64"), "c2c15e08df0e8fbc374c335b230a7ee958c313fa5714817a59aa59f1aa594f51", "tar.gz"),
    ("shellcheck", "darwin", "arm64"): ReleaseAsset(_SHELLCHECK.format(os="darwin", arch="aarch64"), "339b930feb1ea764467013cc1f72d09cd6b869ebf1013296ba9055ab2ffbd26f", "tar.gz"),
    ("shfmt", "linux", "x86_64"): ReleaseAsset(_SHFMT.format(os="linux", arch="amd64"), "76e77641faa025814b77f153b29796b8e6fa2fca03e0c76a691608b86c7ea7bf", "raw"),
    ("shfmt", "linux", "aarch64"): ReleaseAsset(_SHFMT.format(os="linux", arch="arm64"), "5f2db09dae91fca848f7adbdd014632e921a383863a2ad7e0450ad3aba0c6489", "raw"),
    ("shfmt", "darwin", "x86_64"): ReleaseAsset(_SHFMT.format(os="darwin", arch="amd64"), "d33eee0da0f92835b3562e9767a05cee7e4eaeef47daa03bfd09da17b4b590a6", "raw"),
    ("shfmt", "darwin", "arm64"): ReleaseAsset(_SHFMT.format(os="darwin", arch="arm64"), "b7c872db63553ccffc7253aba3ed7d4885a27d83f1ba567b1138c6315a5847e5", "raw"),
    ("actionlint", "linux", "x86_64"): ReleaseAsset(_ACTIONLINT.format(os="linux", arch="amd64"), "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8", "tar.gz"),
    ("actionlint", "linux", "aarch64"): ReleaseAsset(_ACTIONLINT.format(os="linux", arch="arm64"), "325e971b6ba9bfa504672e29be93c24981eeb1c07576d730e9f7c8805afff0c6", "tar.gz"),
    ("actionlint", "darwin", "x86_64"): ReleaseAsset(_ACTIONLINT.format(os="darwin", arch="amd64"), "5b44c3bc2255115c9b69e30efc0fecdf498fdb63c5d58e17084fd5f16324c644", "tar.gz"),
    ("actionlint", "darwin", "arm64"): ReleaseAsset(_ACTIONLINT.format(os="darwin", arch="arm64"), "aba9ced2dee8d27fecca3dc7feb1a7f9a52caefa1eb46f3271ea66b6e0e6953f", "tar.gz"),
}


def release_asset(tool: str, system: str, machine: str) -> ReleaseAsset | None:
    """The pinned asset for ``tool`` on this platform, or ``None`` (no install route)."""
    return _ASSETS.get((tool, system, machine))


def _download(url: str) -> bytes:
    if not url.startswith("https://"):
        raise ReleaseError(f"refusing non-https release URL {url}")
    try:
        with urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT_S) as resp:  # noqa: S310 -- https checked above
            data = resp.read(_MAX_DOWNLOAD_BYTES + 1)
    except (OSError, http.client.HTTPException) as exc:
        raise ReleaseError(f"download of {url} failed: {type(exc).__name__}: {exc}") from exc
    if len(data) > _MAX_DOWNLOAD_BYTES:
        raise ReleaseError(f"{url} is larger than {_MAX_DOWNLOAD_BYTES} bytes")
    return data


def _unpack(archive: bytes, name: str) -> bytes:
    """The bytes of the single regular file called ``name``; links, devices and traversal reject the archive."""
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            members = tar.getmembers()
            for member in members:
                if member.name.startswith("/") or ".." in Path(member.name).parts or not (member.isreg() or member.isdir()):
                    raise ReleaseError(f"archive member {member.name!r} is a link/device/traversal; archive refused")
            wanted = [m for m in members if m.isreg() and Path(m.name).name == name]
            if len(wanted) != 1:
                raise ReleaseError(f"archive must hold exactly one `{name}` file, found {len(wanted)}")
            handle = tar.extractfile(wanted[0])
            if handle is None:
                raise ReleaseError(f"archive member `{name}` is unreadable")
            return handle.read()
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise ReleaseError(f"not a readable gzip tar archive: {exc}") from exc


def fetch_verified_binary(url: str, sha256: str, kind: str, name: str, *, fetch: Fetch | None = None) -> bytes:
    """Download, verify against the pinned digest, and return the executable's bytes."""
    data = (fetch or _download)(url)
    actual = hashlib.sha256(data).hexdigest()
    if actual != sha256:
        raise ReleaseError(f"sha256 mismatch for {url}: expected {sha256}, got {actual}; refusing to install")
    binary = data if kind == "raw" else _unpack(data, name)
    if not binary:
        raise ReleaseError(f"{name} in {url} is empty")
    return binary
