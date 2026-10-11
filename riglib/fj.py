"""Forgejo access — install ``fj``, one-click OAuth login shared by fj AND git, a default host.

What this is
------------
The ecosystem lives on Forgejo (``git.hyperide.ai``). Out of the box its CLI ``fj``
(forgejo-cli) is unfriendly: outside a clone it needs ``--host`` on every call, and ``fj auth
login`` only does a browser login for a hard-coded list of public instances — everywhere else it
tells you to go mint a token by hand. git over HTTPS has the same problem. The ``fj:`` block
fixes all of it, per machine:

- **install fj** — ``brew install forgejo-cli`` on macOS (when no fj is on PATH); on Linux and
  Windows the official release binary (sha256-pinned below) into rig's own dir
  ``<data>/rig/fj/<version>/`` (:func:`fj_store_root` — deliberately NOT on PATH);
- **``fj ship`` = ``gh ship``** — fj has no alias mechanism, so ``fj`` on PATH is a thin rig
  wrapper (``<bin_dir>/fj``, POSIX ``sh``; plus ``fj.cmd`` for cmd/PowerShell on Windows):
  ``fj ship …`` runs the very dispatch body of the ``gh ship`` alias
  (:func:`riglib.gh_ship_alias.ship_dispatch_sh`), every other ``fj …`` execs the real fj with the
  arguments untouched. A real fj rig itself put in ``bin_dir`` earlier (recognised by its pinned
  sha256) is moved into the store; anything else there is never touched;
- **one-click login for fj** — every Forgejo instance ships built-in public OAuth2 apps
  (``[oauth2] DEFAULT_APPLICATIONS``: tea, git-credential-oauth, Git Credential Manager). rig lists
  the ``tea`` app's client ID for the host in fj's ``client_ids`` file, so a plain
  ``fj auth login`` opens the browser, you press *Authorize*, done — no admin, no app to register.
  fj stores the OAuth token and refreshes it itself;
- **the same login for git** — rig writes its own git credential helper ``<bin_dir>/git-credential-fj``
  and points ``credential.https://<host>.helper`` at it (after an empty value, so no other helper —
  store, cache, GCM — can answer first with a stale password). On ``git push``/``fetch`` it has fj
  refresh the OAuth token and hands it to git, so one ``fj auth login`` serves fj and git;
- **no more ``--host``** — ``FJ_FALLBACK_HOST=https://<host>`` for every shell (via the ``env``
  area on macOS/Linux, a user environment variable on Windows).

Where the real fj lives, per OS
-------------------------------
- **Linux**: ``${XDG_DATA_HOME:-~/.local/share}/rig/fj/<version>/fj``; the wrapper is
  ``<bin_dir>/fj`` (``~/.local/bin`` by default).
- **macOS**: Homebrew's fj stays where brew put it; the wrapper in ``bin_dir`` finds it by walking
  PATH for an ``fj`` that is not a rig wrapper. ``bin_dir`` must come before Homebrew on PATH —
  ``rig status`` reports it when ``fj`` resolves to anything else first (``fj-path``).
- **Windows** (native, Cygwin, Git Bash/MSYS): ``%USERPROFILE%\\.local\\share\\rig\\fj\\<version>\\fj.exe``.
  ``bin_dir`` holds the ``sh`` wrapper ``fj`` (no extension) for Cygwin / Git Bash and ``fj.cmd``
  for cmd / PowerShell — and NO ``fj.exe``: measured on Windows 10, Cygwin 3.x bash/dash and Git
  Bash both run the extensionless ``fj`` when ``fj`` and ``fj.exe`` share a PATH dir, but cmd,
  PowerShell and Python's ``shutil.which`` all take ``fj.exe`` over ``fj.cmd`` (PATHEXT order), so
  an ``fj.exe`` next to the wrapper would bypass ``fj ship`` there. (MSYS also opens ``fj.exe`` when
  a shell writes to ``fj`` while only ``fj.exe`` exists — one more reason to keep them apart.)

This module is stdlib-only. The planning helpers are pure; the few effectful helpers (download +
install fj, write the wrappers and the git credential helper, ``git config --global``, read/write
the Windows user environment) are called only by the runner (``actions/runner.py``
``_do_provision_fj``).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

DEFAULT_FJ_VERSION = "0.6.0"
FJ_RELEASE_URL = (
    "https://codeberg.org/forgejo-contrib/forgejo-cli/releases/download/v{version}/{asset}"
)
# sha256 of the official release archives (fetched over TLS from codeberg.org on 2026-09-24).
# An unpinned version is refused rather than installed unverified.
FJ_RELEASE_SHA256: dict[str, dict[str, str]] = {
    "0.6.0": {
        "forgejo-cli-x86_64-linux.tar.gz": "ea559da5449b6dd7e0dd9f7ea51c906b575696f782edd606b52adcf773602742",
        "forgejo-cli-aarch64-linux.tar.gz": "f427e0f94e2bf6f4b44bff17020706915bccfe7abb7ad9b9cbb74cc1aa2dd7b5",
        "forgejo-cli-x86_64-windows.zip": "9ec20aff62da33afe5a46b6fd63c572dc371d8434b576c35e1fc5204c6652643",
    },
}
# sha256 of the ``fj`` / ``fj.exe`` INSIDE each pinned archive above (extracted from the verified
# archives on 2026-09-25). Install checks the extracted binary against it, and it is how rig
# recognises a binary it installed itself into ``bin_dir`` before the wrapper existed — only such
# a file is ever moved; anything else there belongs to the user and is left alone.
FJ_BINARY_SHA256: dict[str, dict[str, str]] = {
    "0.6.0": {
        "forgejo-cli-x86_64-linux.tar.gz": "9e88e080e44ea87d77794bcd31b677497db8bde23e240b9c97800162938e3f66",
        "forgejo-cli-aarch64-linux.tar.gz": "6c3e2387b96612426e7a34b2a0b89e4e1022fbf53bdb1ac448dd8da6b5858b75",
        "forgejo-cli-x86_64-windows.zip": "5ac7a88728ddf0ab12a7277172a817fe14bf5e0bf7ea62470b728608228b74fb",
    },
}

# First-lines marker of both wrappers rig writes (``<bin_dir>/fj`` and, on Windows, ``fj.cmd``).
# The wrapper skips any PATH ``fj`` carrying it (never execs itself or a second rig wrapper), and
# rig treats a marked file as its own (rewrites a stale one) — an unmarked file is never clobbered.
WRAPPER_MARKER = "rig-managed fj wrapper"

# Forgejo's built-in public OAuth2 ``tea`` application (same ID on every instance that keeps the
# default ``[oauth2] DEFAULT_APPLICATIONS``). Its redirect URI is loopback-any-port, which fits fj's
# fixed ``http://127.0.0.1:26218/``.
FORGEJO_TEA_CLIENT_ID = "d57cb8c4-630c-4168-8324-ec79935e18d4"

FALLBACK_HOST_VAR = "FJ_FALLBACK_HOST"
# Safe inside a client_ids line: fj 0.6.0 drops everything after `#` before splitting host and ID
# (`parse_client_info_file` in forgejo-cli's src/auth.rs).
CLIENT_IDS_MARKER = "# rig-managed"
DEFAULT_BIN_DIR = "~/.local/bin"

_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::\d{1,5})?$")


class FjError(Exception):
    """A fj provisioning step failed in a way the user must act on."""


# ── config ──────────────────────────────────────────────────────────────────────────────────


def normalize_host(raw: str) -> str:
    """``https://git.example.com/`` / ``git.example.com`` → ``git.example.com`` (port kept)."""
    host = raw.strip()
    host = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", host)
    host = host.split("/", 1)[0]
    if not _HOST_RE.match(host):
        raise ValueError(f"not a host name: {raw!r}")
    return host.lower()


def is_valid_host(raw: Any) -> bool:
    if not isinstance(raw, str):
        return False
    try:
        normalize_host(raw)
    except ValueError:
        return False
    return True


def fj_options_from_config(block: dict[str, Any], *, fallback_host: str | None = None) -> dict[str, Any]:
    """Resolve a validated ``fj:`` block into the options a ``provision_fj`` action carries.

    ``hosts`` is every host fj should be able to log in to (the block's ``host`` first, then
    ``fallback_host`` — the GLOBAL layer's host — when a repo overrides it). ``fallback_host`` is
    what ``FJ_FALLBACK_HOST`` points at: the machine default, never a single repo's override — so
    with no global host (``None``) rig sets no ``FJ_FALLBACK_HOST`` at all. It is also the only
    host rig changes machine-wide auth for (see :func:`trusted_hosts`).
    """
    host = normalize_host(str(block["host"]))
    fallback = normalize_host(fallback_host) if fallback_host else None
    hosts = [host] + ([fallback] if fallback and fallback != host else [])
    return {
        "hosts": hosts,
        "fallback_host": fallback,
        "install": bool(block.get("install", True)),
        "version": str(block.get("version", DEFAULT_FJ_VERSION)),
        "bin_dir": str(block.get("bin_dir", DEFAULT_BIN_DIR)),
        "git_credentials": bool(block.get("git_credentials", True)),
        "client_id": str(block.get("client_id", FORGEJO_TEA_CLIENT_ID)),
    }


def fallback_host_url(host: str) -> str:
    return f"https://{host}"


def trusted_hosts(options: dict[str, Any]) -> list[str]:
    """The hosts rig may change machine-wide auth for: the GLOBAL layer's ``fj.host`` only.

    A repo's rig.yaml may name its own forge, and fj can then log in to it; but a cloned repo must
    not point git's global credential settings for a host of ITS choosing (``github.com``, a
    company server) at Forgejo's OAuth app, nor replace a ``client_ids`` line someone wrote for it.
    """
    fallback = options.get("fallback_host")
    return [h for h in options["hosts"] if h == fallback]


def implied_env_vars(block: Any, *, windows: bool | None = None) -> dict[str, str]:
    """``FJ_FALLBACK_HOST`` for the ``env`` area to export — POSIX only (Windows uses the user
    environment, see :func:`set_windows_user_env`). Empty when the block is absent/disabled."""
    if windows is None:
        windows = os.name == "nt"
    if windows or not isinstance(block, dict) or block.get("enabled") is False or not block.get("host"):
        return {}
    return {FALLBACK_HOST_VAR: fallback_host_url(normalize_host(str(block["host"])))}


# ── fj: where things live ───────────────────────────────────────────────────────────────────


def fj_release_asset(system: str | None = None, machine: str | None = None) -> str | None:
    """The release archive for this platform, or ``None`` where rig can't install one (macOS uses
    Homebrew instead)."""
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    arch = {"amd64": "x86_64", "x86_64": "x86_64", "x64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}.get(machine)
    if system == "linux" and arch in {"x86_64", "aarch64"}:
        return f"forgejo-cli-{arch}-linux.tar.gz"
    if (system == "windows" or system.startswith(("cygwin", "msys", "mingw"))) and arch == "x86_64":
        return "forgejo-cli-x86_64-windows.zip"
    return None


def fj_platform(system: str | None = None) -> str:
    """``windows`` (native, Cygwin, MSYS/Git Bash), ``darwin``, ``linux`` — else the lowercased name."""
    system = (system or platform.system()).lower()
    if system == "windows" or system.startswith(("cygwin", "msys", "mingw")):
        return "windows"
    return system


def fj_binary_name(system: str | None = None) -> str:
    return "fj.exe" if fj_platform(system) == "windows" else "fj"


def _windows_env_dir(name: str) -> Path | None:
    """A Windows directory from the environment (``USERPROFILE``, ``APPDATA``) as THIS Python opens
    it: as is on a native Windows Python, through ``cygpath -u`` on a Cygwin / MSYS Python (whose
    ``~`` is ``$HOME`` — ``/home/<user>`` — not the profile the native fj.exe and the wrapper use).
    ``None`` when unset or unconvertible."""
    value = os.environ.get(name)
    if not value:
        return None
    if os.name == "nt":
        return Path(value)
    if sys.platform.startswith(("cygwin", "msys")):
        try:
            out = subprocess.run(
                ["cygpath", "-u", value], capture_output=True, text=True, timeout=15, check=True
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
        return Path(out) if out else None
    return None


def _windows_profile() -> Path:
    """``%USERPROFILE%`` (native Windows Python: what ``~`` already is)."""
    if os.name == "nt":
        return Path(os.path.expanduser("~"))
    return _windows_env_dir("USERPROFILE") or Path(os.path.expanduser("~"))


def fj_store_root(system: str | None = None) -> Path:
    """rig's own dir for the real fj it downloads (one subdir per version) — NOT on PATH.

    ``${XDG_DATA_HOME:-~/.local/share}/rig/fj``; on Windows always
    ``%USERPROFILE%\\.local\\share\\rig\\fj`` — whether rig runs as a native Windows program or
    under a Cygwin / MSYS Python — the dir the wrapper resolves from Cygwin / Git Bash through
    ``$USERPROFILE``, whatever ``$HOME`` is.
    """
    if fj_platform(system) == "windows":
        base = _windows_profile() / ".local" / "share"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"))
    return base / "rig" / "fj"


def fj_store_binary(version: str, system: str | None = None) -> Path:
    return fj_store_root(system) / version / fj_binary_name(system)


# The wrapper is POSIX sh (dash / bash / Cygwin / Git Bash / macOS sh) and a CONSTANT per version:
# no machine path is baked in — the store dir and the ship fallback are resolved at run time, like
# the gh alias. `@SHIP@` is the gh alias body verbatim (only its diagnostic says `fj ship`).
_WRAPPER_SH = """\
#!/bin/sh
# @MARKER@ (fj @VERSION@). `rig apply` rewrites this file: change rig's `fj:` config, not it.
# `fj ship ...` is `gh ship ...`: the same dispatch as the gh alias (riglib/gh_ship_alias.py).
# Every other `fj ...` execs the real fj with its arguments, stdin and exit code untouched.
if [ "${1-}" = ship ]; then
  # Cygwin: ship drives Windows-native python/git/jq, and only Git for Windows' sh (MSYS) converts
  # POSIX path arguments for them — so run `fj ship` under it, as gh runs `gh ship` there anyway.
  case "$(uname -s 2>/dev/null)" in
    CYGWIN*)
      if [ -z "${RIG_FJ_SHIP_MSYS-}" ]; then
        g="$(command -v git 2>/dev/null)"
        for s in "${g%/*}/../bin/sh.exe" "${g%/*}/../../bin/sh.exe"; do
          if [ -n "$g" ] && [ -f "$s" ]; then
            RIG_FJ_SHIP_MSYS=1; export RIG_FJ_SHIP_MSYS
            # Cygwin-form paths (/cygdrive/...) mean nothing to MSYS: hand them over as C:/...
            if [ -n "${AGENT_TOOLS_ROOT-}" ]; then AGENT_TOOLS_ROOT="$(cygpath -m "$AGENT_TOOLS_ROOT")"; export AGENT_TOOLS_ROOT; fi
            if [ -n "${XDG_CONFIG_HOME-}" ]; then XDG_CONFIG_HOME="$(cygpath -m "$XDG_CONFIG_HOME")"; export XDG_CONFIG_HOME; fi
            exec "$s" "$(cygpath -w "$0")" "$@"
          fi
        done
        echo "fj ship: Git for Windows' sh.exe not found next to git; install Git for Windows." >&2
        exit 127
      fi ;;
  esac
  shift
  @SHIP@
fi
# The real fj: rig's pinned copy (<data>/rig/fj/@VERSION@, not on PATH), else the first fj on PATH
# that is not a rig wrapper (Homebrew's on macOS).
case "$(uname -s 2>/dev/null)" in
  CYGWIN*|MINGW*|MSYS*) b="$(cygpath -u "${USERPROFILE:-}" 2>/dev/null)"; b="${b:-$HOME}/.local/share" ;;
  *) b="${XDG_DATA_HOME:-$HOME/.local/share}" ;;
esac
for f in "$b/rig/fj/@VERSION@/fj.exe" "$b/rig/fj/@VERSION@/fj"; do
  if [ -f "$f" ] && [ -x "$f" ]; then exec "$f" "$@"; fi
done
set -f; IFS=:
for d in $PATH; do
  for f in "${d:-.}/fj.exe" "${d:-.}/fj"; do
    [ -f "$f" ] && [ -x "$f" ] || continue
    if head -n 3 "$f" 2>/dev/null | grep -q '@MARKER@'; then continue; fi
    exec "$f" "$@"
  done
done
echo "fj: the real fj is not installed ($b/rig/fj/@VERSION@ or PATH); run rig apply." >&2
exit 127
"""

# cmd / PowerShell entry point (Windows only). ASCII only, CRLF on disk. `fj ship` runs the POSIX
# wrapper beside it with the sh gh itself runs aliases with (gh's findsh: `sh` on PATH, else Git
# for Windows' `bin\\sh.exe` next to `git`), so `fj ship` is `gh ship` in these shells too.
_WRAPPER_CMD = r"""@echo off
rem @MARKER@ (fj @VERSION@). `rig apply` rewrites this file: change rig's `fj:` config, not it.
rem `fj ship ...` runs the sh wrapper next to this file (same dispatch as `gh ship`) through the sh
rem gh uses for aliases; every other `fj ...` runs the real fj.exe with its arguments untouched.
setlocal
if /i "%~1"=="ship" goto ship
set "RIG_FJ=%USERPROFILE%\.local\share\rig\fj\@VERSION@\fj.exe"
if exist "%RIG_FJ%" goto run
set "RIG_FJ="
for /f "delims=" %%F in ('where fj.exe 2^>nul') do if not defined RIG_FJ set "RIG_FJ=%%F"
if defined RIG_FJ goto run
>&2 echo fj: the real fj is not installed (%USERPROFILE%\.local\share\rig\fj\@VERSION@ or PATH); run rig apply.
exit /b 127
:run
"%RIG_FJ%" %*
exit /b %ERRORLEVEL%
:ship
set "RIG_SH="
for /f "delims=" %%S in ('where sh.exe 2^>nul') do if not defined RIG_SH set "RIG_SH=%%S"
if defined RIG_SH goto ship_run
for /f "delims=" %%G in ('where git.exe 2^>nul') do if not defined RIG_SH if exist "%%~dpG..\bin\sh.exe" set "RIG_SH=%%~dpG..\bin\sh.exe"
for /f "delims=" %%G in ('where git.exe 2^>nul') do if not defined RIG_SH if exist "%%~dpG..\apps\git\current\bin\sh.exe" set "RIG_SH=%%~dpG..\apps\git\current\bin\sh.exe"
if defined RIG_SH goto ship_run
>&2 echo fj ship: no POSIX sh found - install Git for Windows, or run fj ship from Git Bash / Cygwin.
exit /b 127
:ship_run
"%RIG_SH%" "%~dp0fj" %*
exit /b %ERRORLEVEL%
"""


def fj_wrapper_sh(version: str) -> str:
    """``<bin_dir>/fj`` — ``fj ship`` = the ``gh ship`` alias body, everything else → the real fj."""
    from .gh_ship_alias import ship_dispatch_sh

    return (
        _WRAPPER_SH.replace("@MARKER@", WRAPPER_MARKER)
        .replace("@VERSION@", version)
        .replace("@SHIP@", ship_dispatch_sh("fj ship"))
    )


def fj_wrapper_cmd(version: str) -> str:
    """``<bin_dir>/fj.cmd`` (Windows): cmd/PowerShell's ``fj`` — CRLF line endings."""
    return _WRAPPER_CMD.replace("@MARKER@", WRAPPER_MARKER).replace("@VERSION@", version).replace("\n", "\r\n")


def fj_wrapper_files(options: dict[str, Any], system: str | None = None) -> list[tuple[Path, bytes]]:
    """Every wrapper file rig owns in ``bin_dir`` with its desired bytes (``fj``; + ``fj.cmd`` on Windows)."""
    bin_dir = Path(os.path.expanduser(options["bin_dir"]))
    files = [(bin_dir / "fj", fj_wrapper_sh(options["version"]).encode("utf-8"))]
    if fj_platform(system) == "windows":
        files.append((bin_dir / "fj.cmd", fj_wrapper_cmd(options["version"]).encode("ascii")))
    return files


def _has_marker(path: str | Path, marker: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return marker.encode() in fh.read(512)
    except OSError:
        return False


def is_rig_wrapper(path: str | Path) -> bool:
    """True when ``path`` is one of rig's fj wrappers (the marker sits in its first lines)."""
    return _has_marker(path, WRAPPER_MARKER)


def pinned_binary(path: Path) -> tuple[str, str] | None:
    """``(version, asset)`` when ``path`` is byte-for-byte a fj binary rig installs, else ``None``."""
    sha = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                sha.update(chunk)
    except OSError:
        return None
    digest = sha.hexdigest()
    for version, assets in FJ_BINARY_SHA256.items():
        for asset, pinned in assets.items():
            if digest == pinned:
                return version, asset
    return None


def classify_bin_file(path: Path, desired: bytes, marker: str = WRAPPER_MARKER) -> str:
    """What sits at a rig-owned ``bin_dir`` path (an fj wrapper, or the git credential helper with
    its own ``marker``): ``missing`` / ``ok`` / ``stale`` (an older rig file) / ``rig-binary`` (a
    real fj rig installed there itself) / ``foreign`` (anything else — rig leaves it alone: a
    symlink, a directory, the user's own fj or script)."""
    if not os.path.lexists(path):
        return "missing"
    if path.is_symlink() or not path.is_file():
        return "foreign"
    if _has_marker(path, marker):
        try:
            return "ok" if path.read_bytes() == desired else "stale"
        except OSError:
            return "foreign"
    return "rig-binary" if pinned_binary(path) else "foreign"


def _path_dirs() -> list[str]:
    return [d for d in os.environ.get("PATH", "").split(os.pathsep) if d]


def _same_dir(a: str | Path, b: str | Path) -> bool:
    def norm(p: str | Path) -> str:
        return os.path.normcase(os.path.realpath(os.path.expanduser(str(p))))

    return norm(a) == norm(b)


def path_shadow(bin_dir: str | Path, system: str | None = None) -> Path | None:
    """An ``fj`` that PATH reaches BEFORE ``bin_dir`` (so ``fj`` is not rig's wrapper), else ``None``.

    Mirrors the shells' lookup: the first PATH dir holding an executable ``fj`` wins; on Windows
    any of ``fj`` / ``fj.exe`` (bash) or the PATHEXT names (cmd, PowerShell) counts. Nothing is
    reported when no ``fj`` precedes ``bin_dir`` — including a PATH without ``bin_dir`` and without
    any other fj (a daemon's minimal PATH), where there is nothing ``fj`` could wrongly resolve to.
    """
    windows = fj_platform(system) == "windows"
    names = ("fj", "fj.exe", "fj.com", "fj.bat", "fj.cmd") if windows else ("fj",)
    for d in _path_dirs():
        if _same_dir(d, bin_dir):
            return None
        for name in names:
            candidate = Path(d) / name
            if candidate.is_file() and (windows or os.access(candidate, os.X_OK)):
                return candidate
    return None


def _path_real_fj(system: str | None = None) -> str | None:
    """The first real fj on PATH — never a rig wrapper (on Windows only ``fj.exe`` is runnable)."""
    names = ("fj.exe",) if fj_platform(system) == "windows" else ("fj",)
    for d in _path_dirs():
        for name in names:
            candidate = Path(d) / name
            if candidate.is_file() and os.access(candidate, os.X_OK) and not is_rig_wrapper(candidate):
                return str(candidate)
    return None


def _version_key(name: str) -> tuple:
    return tuple(int(p) if p.isdigit() else -1 for p in re.split(r"[.-]", name))


def find_real_fj(version: str | None = None, *, system: str | None = None) -> str | None:
    """A runnable REAL fj for rig's own calls (``fj whoami``) — never the wrapper.

    rig's pinned copy first (``version``'s, or the newest in the store), then the first non-wrapper
    fj on PATH (Homebrew's on macOS)."""
    root = fj_store_root(system)
    name = fj_binary_name(system)
    if version:
        candidates = [root / version / name]
    elif root.is_dir():
        candidates = sorted(root.glob(f"*/{name}"), key=lambda p: _version_key(p.parent.name), reverse=True)
    else:
        candidates = []
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return _path_real_fj(system)


def client_ids_path(system: str | None = None) -> Path:
    """Where fj (>= 0.6.0) looks up OAuth client IDs for a host.

    Mirrors ``get_client_info_for`` in forgejo-cli's ``src/auth.rs``: macOS reads
    ``~/.config/forgejo-cli/client_ids``; elsewhere ``ProjectDirs("", "Cyborus", "forgejo-cli")``'s
    config dir — ``$XDG_CONFIG_HOME/forgejo-cli`` on Linux, ``%APPDATA%\\Cyborus\\forgejo-cli\\config``
    on Windows.
    """
    system = (system or platform.system()).lower()
    home = Path(os.path.expanduser("~"))
    if fj_platform(system) == "windows":  # the native fj.exe, also when rig runs on Cygwin Python
        appdata = _windows_env_dir("APPDATA") or _windows_profile() / "AppData" / "Roaming"
        return appdata / "Cyborus" / "forgejo-cli" / "config" / "client_ids"
    if system == "darwin":
        return home / ".config" / "forgejo-cli" / "client_ids"
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(home / ".config")
    return Path(xdg) / "forgejo-cli" / "client_ids"


def keys_json_path(system: str | None = None) -> Path:
    """Where fj (>= 0.6.0) keeps its logins: ``keys.json``, per host an ``Application`` token (``fj
    auth add-token``) or an ``OAuth`` token + refresh token + expiry (``fj auth login``).

    Mirrors ``KeyInfo::load`` in forgejo-cli's ``src/keys.rs`` — ``ProjectDirs("", "forgejo-cli",
    "forgejo-cli")``'s data dir: ``${XDG_DATA_HOME:-~/.local/share}/forgejo-cli`` on Linux,
    ``~/Library/Application Support/forgejo-cli.forgejo-cli`` on macOS,
    ``%APPDATA%\\forgejo-cli\\forgejo-cli\\data`` on Windows (the native fj.exe, also when rig runs
    on Cygwin Python).
    """
    system = (system or platform.system()).lower()
    home = Path(os.path.expanduser("~"))
    if fj_platform(system) == "windows":
        appdata = _windows_env_dir("APPDATA") or _windows_profile() / "AppData" / "Roaming"
        return appdata / "forgejo-cli" / "forgejo-cli" / "data" / "keys.json"
    if system == "darwin":
        return home / "Library" / "Application Support" / "forgejo-cli.forgejo-cli" / "keys.json"
    data = os.environ.get("XDG_DATA_HOME") or str(home / ".local" / "share")
    return Path(data) / "forgejo-cli" / "keys.json"


def fj_login_type(host: str, system: str | None = None) -> str | None:
    """How fj is logged in to ``host`` — ``OAuth`` (``fj auth login``), ``Application`` (a pasted
    token) — or ``None`` when it is not. Reads ``keys.json``; the token itself never leaves it."""
    try:
        data = json.loads(keys_json_path(system).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    hosts = data.get("hosts") if isinstance(data, dict) else None
    entry = hosts.get(host) if isinstance(hosts, dict) else None
    if not isinstance(entry, dict) or not isinstance(entry.get("token"), str) or not entry["token"]:
        return None
    return str(entry.get("type"))


def desired_client_ids_text(
    existing: str, hosts: list[str], client_id: str, *, keep: frozenset[str] | set[str] = frozenset()
) -> str:
    """``existing`` with exactly one ``<host> <client_id>`` line per host (rig-marked); every other
    line — other hosts, comments — is kept as is. For a host in ``keep`` (one only a repo named)
    a line rig did not write is left alone: rig only adds a missing one or refreshes its own."""
    wanted = {h: f"{h} {client_id}  {CLIENT_IDS_MARKER}" for h in hosts}
    out: list[str] = []
    seen: set[str] = set()
    for line in existing.splitlines():
        first = line.split("#", 1)[0].split()
        host = first[0].lower() if first else None
        if host in wanted:
            if host not in seen:
                theirs = host in keep and CLIENT_IDS_MARKER not in line
                out.append(line if theirs else wanted[host])
                seen.add(host)
            continue
        out.append(line)
    out.extend(wanted[h] for h in hosts if h not in seen)
    return "\n".join(out) + "\n"


# ── git: fj's login for git over HTTPS ──────────────────────────────────────────────────────

GIT_HELPER_NAME = "git-credential-fj"
# First-lines marker of rig's git credential helper (``<bin_dir>/git-credential-fj``): a marked
# file is rig's (a stale one is rewritten), an unmarked one is never clobbered.
GIT_HELPER_MARKER = "rig-managed git credential helper"
GITCONFIG = Path("~/.gitconfig")

# POSIX sh, so every git runs it the same way: Linux/macOS git, Cygwin git, and Git for Windows
# (which runs an extensionless `#!/bin/sh` script through its own sh). keys.json is read with awk —
# present in all of them — relying on the fixed line layout serde_json's pretty printer gives the
# file (fj writes it only with `to_vec_pretty`). `@KEYS@` is the keys.json path, shell-quoted.
_GIT_HELPER_SH = """\
#!/bin/sh
# @MARKER@ (fj). `rig apply` rewrites this file: change rig's `fj:` config, not it.
# git runs it for https://<fj host> (credential.https://<host>.helper). `get` answers with the token
# `fj auth login` stored, refreshed through fj first; `store`/`erase` do nothing (fj owns the token).
if [ "${1-}" != get ]; then cat >/dev/null; exit 0; fi
protocol= host=
while IFS= read -r line; do
  [ -n "$line" ] || break
  case $line in
    protocol=*) protocol=${line#protocol=} ;;
    host=*) host=${line#host=} ;;
  esac
done
# Never hand a token to plain http, and only look up a plain host[:port].
[ "$protocol" = https ] || exit 0
host=$(printf '%s' "$host" | tr 'A-Z' 'a-z')
case $host in ''|*[!a-z0-9.:-]*) exit 0 ;; esac
keys=@KEYS@
# One string field of keys.json's hosts.<host> entry. A value with a JSON escape (a backslash) is
# never printed half-decoded: awk exits 3 instead (fj's tokens are hex / base64url, so it doesn't).
field() {
  [ -f "$keys" ] || return 0
  awk -v h="$host" -v k="$1" '
    $0 == "  \\"hosts\\": {" { inh = 1; next }
    inh && /^  [^ ]/ { exit }
    inh && $0 == "    \\"" h "\\": {" { ine = 1; next }
    ine && /^    [^ ]/ { exit }
    ine && index($0, "      \\"" k "\\": \\"") == 1 {
      v = substr($0, length(k) + 12); sub(/",?$/, "", v)
      if (index(v, "\\\\")) exit 3
      print v; exit
    }
  ' "$keys"
}
if [ "$(field type)" = OAuth ]; then
  # fj refreshes an expired OAuth token (and saves it) before any API call; whoami is the cheapest.
  # Run from / so fj takes --host, not the remotes of the repo git runs in.
  d=${0%/*}; f=fj
  if [ "$d" != "$0" ] && [ -x "$d/fj" ]; then f=$d/fj; fi
  if ! (cd / && "$f" --host "$host" whoami) </dev/null >/dev/null 2>&1; then
    echo "git-credential-fj: fj could not refresh its login for $host; if git is refused, run: fj --host $host auth login" >&2
  fi
fi
if ! token=$(field token); then
  echo "git-credential-fj: can't read fj's token for $host from $keys (escaped characters); run: fj --host $host auth login" >&2
  exit 0
fi
if [ -z "$token" ]; then
  echo "git-credential-fj: fj is not logged in to $host; run: fj --host $host auth login" >&2
  exit 0
fi
# Forgejo takes the token as the password whatever the user name is (services/auth/basic.go).
printf 'username=fj\\npassword=%s\\n' "$token"
"""

# What rig wrote for Git Credential Manager before its own helper replaced it. Apply removes each
# key only while it still holds exactly rig's value.
_LEGACY_GCM_SETTINGS = {
    # Forgejo's built-in public GCM OAuth app: a public client ID, not a secret.
    "oauthClientId": "e90ee53c-94e2-48ac-9358-a874fb9e0662",  # gitleaks:allow
    "oauthRedirectUri": "http://127.0.0.1",
    "oauthAuthorizeEndpoint": "/login/oauth/authorize",
    "oauthTokenEndpoint": "/login/oauth/access_token",
}


def _shell_path(path: Path) -> str:
    """``path`` the way the helper's sh opens it. On Windows that sh is Cygwin's or Git for
    Windows' (MSYS), which disagree on ``/cygdrive/c`` vs ``/c`` — both open the mixed ``C:/…``."""
    if os.name == "nt":
        return path.as_posix()
    if sys.platform.startswith(("cygwin", "msys")):
        try:
            out = subprocess.run(
                ["cygpath", "-m", str(path)], capture_output=True, text=True, timeout=15, check=True
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return str(path)
        return out or str(path)
    return str(path)


def git_helper_path(options: dict[str, Any]) -> Path:
    return Path(os.path.expanduser(options["bin_dir"])) / GIT_HELPER_NAME


def git_helper_sh(keys_path: str) -> str:
    """rig's git credential helper, reading fj's logins from ``keys_path``."""
    return _GIT_HELPER_SH.replace("@MARKER@", GIT_HELPER_MARKER).replace("@KEYS@", shlex.quote(keys_path))


def git_helper_bytes(system: str | None = None) -> bytes:
    return git_helper_sh(_shell_path(keys_json_path(system))).encode("utf-8")


def git_helper_value(options: dict[str, Any], system: str | None = None) -> str:
    """The ``credential.https://<host>.helper`` value that runs rig's helper.

    macOS/Linux: its absolute path (a relative ``bin_dir`` made absolute — git takes a relative value
    for a helper NAME), so git finds it without ``bin_dir`` on PATH (cron, launchd, an IDE's git).
    Windows: ``fj`` — git's short name for ``git-credential-fj`` on PATH — because Cygwin git and Git
    for Windows don't agree on what an absolute path looks like."""
    if fj_platform(system) == "windows":
        return "fj"
    return shlex.quote(os.path.abspath(git_helper_path(options)))


def git_credential_key(host: str) -> str:
    return f"credential.https://{host}.helper"


def _git_global(*args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(["git", "config", "--global", *args], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None


def git_global_values(key: str) -> list[str] | None:
    """Every value of ``key`` in git's global config, in file order (``[]`` when unset); ``None``
    when git can't be run."""
    res = _git_global("--null", "--get-all", key)
    if res is None or res.returncode not in (0, 1):
        return None
    return res.stdout.split("\0")[:-1] if res.returncode == 0 else []


def _helper_entries_current(values: list[str], want: str) -> bool:
    """git asks rig's helper first: the key's values END with an empty one (git drops every helper
    collected before it — system, global ``credential.helper``, GCM, store) and then rig's."""
    return values[-2:] == ["", want] and values.count(want) == 1


def _foreign_helpers(values: list[str], want: str) -> list[str]:
    return [v for v in values if v and v != want]


def _foreign_helper_detail(key: str, value: str) -> str:
    return (f"`{key} = {value}` is not rig's — rig's empty reset after it makes git skip it; remove it: "
            f"git config --global --fixed-value --unset-all {shlex.quote(key)} {shlex.quote(value)}")


def _legacy_gcm_keys(host: str) -> list[tuple[str, str]]:
    keys = [(f"credential.https://{host}.{name}", value) for name, value in _LEGACY_GCM_SETTINGS.items()]
    return [(key, value) for key, value in keys if git_global_values(key) == [value]]


def login_notes(options: dict[str, Any], system: str | None = None) -> list[tuple[str, str]]:
    """What the user still has to do to log in, as ``(host, note)`` (never drift — only they can):
    no fj login for the global host yet, or a pasted token (``fj auth add-token``) instead of the
    browser login. ``fj auth login`` overwrites the host's entry in place (forgejo-cli
    ``src/auth.rs``)."""
    notes: list[tuple[str, str]] = []
    shared = "; git over HTTPS uses the same login" if options["git_credentials"] else ""
    for host in trusted_hosts(options):
        kind = fj_login_type(host, system)
        login = f"fj --host {host} auth login"
        if kind is None:
            notes.append((host, f"fj is not logged in to {host} — run `{login}` (browser → Authorize){shared}"))
        elif kind != "OAuth":
            notes.append((host, f"fj's login for {host} is a pasted {kind} token, not the browser login — "
                                f"`{login}` replaces it (browser → Authorize)"))
    return notes


# ── what is out of date (shared by apply and status) ────────────────────────────────────────

# pending_changes items that belong to the `fj` COMMAND (real binary + wrappers + PATH), reconciled
# together by `_apply_fj_command`.
COMMAND_ITEMS = frozenset({"fj", "fj-wrapper", "fj.cmd", "fj.exe", "fj-path"})
# pending_changes items of git's use of fj's login, reconciled together by `_apply_git_credentials`
# (the per-host item is `git-credential:<host>`).
GIT_ITEMS = frozenset({GIT_HELPER_NAME, "git-credential"})


def _command_changes(options: dict[str, Any], system: str | None) -> list[tuple[str, str, Path, str]]:
    """The ``fj`` command's own steps: the real fj, the wrapper(s), no stray ``fj.exe`` beside them
    (Windows), and ``fj`` on PATH resolving to the wrapper."""
    todo: list[tuple[str, str, Path, str]] = []
    plat = fj_platform(system)
    version = options["version"]
    bin_dir = Path(os.path.expanduser(options["bin_dir"]))
    store_root = fj_store_root(system)

    if fj_release_asset(system) is not None:
        store = fj_store_binary(version, system)
        if not store.is_file():
            todo.append(("missing", "fj", store, f"fj {version} is not installed (apply downloads it into {store.parent})"))
    elif not _path_real_fj(system):
        how = "brew install forgejo-cli" if plat == "darwin" else "cargo install forgejo-cli"
        todo.append(("missing", "fj", bin_dir, f"fj is not installed (`{how}`)"))

    for path, desired in fj_wrapper_files(options, system):
        item = "fj-wrapper" if path.name == "fj" else path.name
        state = classify_bin_file(path, desired)
        if state == "missing":
            todo.append(("missing", item, path, "`fj ship` is not set up yet (apply writes the fj wrapper)"))
        elif state == "stale":
            todo.append(("modified", item, path, "the fj wrapper is out of date (apply rewrites it)"))
        elif state == "rig-binary":
            todo.append(("modified", item, path,
                         f"the fj rig installed here moves to {store_root} and the fj wrapper takes its place"))
        elif state == "foreign":
            todo.append(("modified", item, path,
                         f"{path} is not rig's — rig leaves it; move it away (or set fj.install: false) "
                         "so the fj wrapper can go there"))

    if plat == "windows":
        exe = bin_dir / "fj.exe"
        if os.path.lexists(exe):
            if not exe.is_symlink() and exe.is_file() and pinned_binary(exe):
                detail = f"rig's old fj.exe here runs instead of fj.cmd in cmd/PowerShell (apply moves it to {store_root})"
            else:
                detail = f"{exe} is not rig's and runs instead of the fj wrapper in cmd/PowerShell — move it out of {bin_dir}"
            todo.append(("modified", "fj.exe", exe, detail))

    shadow = path_shadow(bin_dir, system)
    if shadow is not None:
        todo.append(("modified", "fj-path", shadow,
                     f"`fj` resolves to {shadow}, not rig's wrapper — put {bin_dir} before {shadow.parent} on PATH"))
    return todo


def _git_credential_changes(options: dict[str, Any], system: str | None) -> list[tuple[str, str, Path, str]]:
    """git over HTTPS to the global host uses fj's login: rig's helper in ``bin_dir`` is current,
    ``credential.https://<host>.helper`` ends with an empty reset + the helper, no helper rig does
    not own sits in that key, and no Git Credential Manager setting an older rig wrote is left."""
    todo: list[tuple[str, str, Path, str]] = []
    path = git_helper_path(options)
    state = classify_bin_file(path, git_helper_bytes(system), GIT_HELPER_MARKER)
    if state == "missing":
        todo.append(("missing", GIT_HELPER_NAME, path, "git can't use fj's login yet (apply writes rig's git credential helper)"))
    elif state == "stale":
        todo.append(("modified", GIT_HELPER_NAME, path, "rig's git credential helper is out of date (apply rewrites it)"))
    elif state != "ok":
        todo.append(("modified", GIT_HELPER_NAME, path,
                     f"{path} is not rig's — rig leaves it; move it away so rig's git credential helper can go there"))
    want = git_helper_value(options, system)
    for host in trusted_hosts(options):
        item = f"git-credential:{host}"
        key = git_credential_key(host)
        values = git_global_values(key)
        if values is None:
            todo.append(("missing", item, GITCONFIG,
                         f"`git config --global` can't be read (git not on PATH, or the global config "
                         f"does not parse) — rig sets {key} with it"))
            continue
        if not _helper_entries_current(values, want):
            todo.append(("modified" if want in values else "missing", item, GITCONFIG,
                         f"git does not use fj's login for https://{host} yet (apply ends {key} with an "
                         f"empty reset + {want})"))
        for value in _foreign_helpers(values, want):
            todo.append(("modified", item, GITCONFIG, _foreign_helper_detail(key, value)))
        if _legacy_gcm_keys(host):
            todo.append(("modified", item, GITCONFIG,
                         f"Git Credential Manager OAuth settings an older rig wrote for https://{host} are "
                         "still in the global git config (apply removes them)"))
    return todo


def pending_changes(
    options: dict[str, Any], *, windows: bool | None = None, system: str | None = None
) -> list[tuple[str, str, Path, str]]:
    """Every provisioning step still to do, as ``(direction, item, target, detail)`` — the ONE
    comparison both ``rig apply`` (``_do_provision_fj``) and ``rig status`` use. Read-only.

    ``system`` picks the fj layout (default: this OS); ``windows`` whether ``FJ_FALLBACK_HOST`` is a
    Windows user environment variable (default: running on Windows)."""
    if windows is None:
        windows = os.name == "nt"
    todo: list[tuple[str, str, Path, str]] = []

    if options["install"]:
        todo.extend(_command_changes(options, system))

    cid_path = client_ids_path(system)
    existing = cid_path.read_text(encoding="utf-8") if cid_path.is_file() else ""
    if _client_ids_text(existing, options) != _normalized(existing):
        direction = "modified" if existing else "missing"
        todo.append((direction, "client_ids", cid_path,
                     f"fj can't log in to {', '.join(options['hosts'])} in the browser yet (no OAuth client ID)"))

    if windows and options["fallback_host"]:
        want = fallback_host_url(options["fallback_host"])
        have = read_windows_user_env(FALLBACK_HOST_VAR)
        if have != want:
            todo.append(("missing" if have is None else "modified", FALLBACK_HOST_VAR,
                         Path("HKCU/Environment"), f"{FALLBACK_HOST_VAR}={have or '<unset>'} (want {want})"))

    if options["git_credentials"] and trusted_hosts(options):
        todo.extend(_git_credential_changes(options, system))
    return todo


def apply_changes(
    options: dict[str, Any],
    todo: list[tuple[str, str, Path, str]] | None = None,
    *,
    system: str | None = None,
) -> tuple[list[str], list[str]]:
    """Do every step :func:`pending_changes` lists; return ``(done, errors)`` — one line each.

    A failing step doesn't stop the others (fj missing must not block the git settings)."""
    if todo is None:
        todo = pending_changes(options, system=system)
    items = {item.split(":", 1)[0] for _, item, _, _ in todo}
    done: list[str] = []
    errors: list[str] = []
    if items & COMMAND_ITEMS:
        cmd_done, cmd_errors = _apply_fj_command(options, system)
        done.extend(cmd_done)
        errors.extend(cmd_errors)
    if "client_ids" in items:
        path = client_ids_path(system)
        try:
            existing = path.read_text(encoding="utf-8") if path.is_file() else ""
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_client_ids_text(existing, options), encoding="utf-8")
            done.append(f"fj browser login enabled for {', '.join(options['hosts'])} ({path})")
        except OSError as exc:
            errors.append(f"fj: could not write {path}: {exc}")
    if FALLBACK_HOST_VAR in items:
        url = fallback_host_url(options["fallback_host"])
        try:
            set_windows_user_env(FALLBACK_HOST_VAR, url)
            done.append(f"{FALLBACK_HOST_VAR}={url} (new shells)")
        except FjError as exc:
            errors.append(str(exc))
    if items & GIT_ITEMS:
        git_done, git_errors = _apply_git_credentials(options, system)
        done.extend(git_done)
        errors.extend(git_errors)
    return done, errors


def _client_ids_text(existing: str, options: dict[str, Any]) -> str:
    keep = set(options["hosts"]) - set(trusted_hosts(options))
    return desired_client_ids_text(existing, options["hosts"], options["client_id"], keep=keep)


def _normalized(text: str) -> str:
    return text if not text or text.endswith("\n") else text + "\n"


# ── effectful helpers (runner only) ───────────────────────────────────────────────


def _apply_fj_command(options: dict[str, Any], system: str | None = None) -> tuple[list[str], list[str]]:
    """Reconcile the ``fj`` command, re-checking each step on disk:

    1. move a real fj rig itself installed into ``bin_dir`` (sha256-pinned) into the store;
    2. install the real fj when it is missing (store download / brew);
    3. write / refresh the wrapper(s) — never over a file that is not rig's;
    4. drop other versions from rig's store once the wanted one is in place;
    5. report what rig can't fix: a foreign ``fj.exe`` beside the wrapper (Windows), a PATH that
       reaches another ``fj`` first.
    """
    done: list[str] = []
    errors: list[str] = []
    plat = fj_platform(system)
    version = options["version"]
    bin_dir = Path(os.path.expanduser(options["bin_dir"]))
    store_root = fj_store_root(system)

    strays = [bin_dir / "fj"] + ([bin_dir / "fj.exe"] if plat == "windows" else [])
    for stray in strays:
        if stray.is_symlink() or not stray.is_file() or is_rig_wrapper(stray):
            continue
        found = pinned_binary(stray)
        if found is None:
            continue
        dest = store_root / found[0] / ("fj.exe" if found[1].endswith(".zip") else "fj")
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(stray, dest)
            except OSError:  # another volume: copy, then drop the original (rig installed it)
                shutil.copy2(stray, dest)
                os.unlink(stray)
            done.append(f"moved the fj rig installed at {stray} → {dest}")
        except OSError as exc:
            errors.append(f"fj: could not move {stray} to {dest}: {exc}")

    if fj_release_asset(system) is not None:
        if not fj_store_binary(version, system).is_file():
            try:
                done.append(install_fj(version, system=system))
            except FjError as exc:
                errors.append(str(exc))
    elif not _path_real_fj(system):
        try:
            done.append(install_fj(version, system=system))
        except FjError as exc:
            errors.append(str(exc))

    for path, desired in fj_wrapper_files(options, system):
        state = classify_bin_file(path, desired)
        if state in ("missing", "stale"):
            try:
                _write_atomic(path, desired)
                verb = "wrote" if state == "missing" else "refreshed"
                done.append(f"{verb} the fj wrapper {path} (`fj ship` = `gh ship`)")
            except OSError as exc:
                errors.append(f"fj: could not write {path}: {exc}")
        elif state != "ok":
            errors.append(f"fj: {path} is not rig's — move it away (or set fj.install: false) "
                          "so rig can put the fj wrapper there")

    if fj_store_binary(version, system).is_file():
        _prune_store(version, system)

    if plat == "windows" and os.path.lexists(bin_dir / "fj.exe"):
        errors.append(f"fj: {bin_dir / 'fj.exe'} is not rig's and runs instead of the fj wrapper in "
                      f"cmd/PowerShell — move it out of {bin_dir}")

    shadow = path_shadow(bin_dir, system)
    if shadow is not None:
        errors.append(f"fj: `fj` resolves to {shadow}, not rig's wrapper — put {bin_dir} before "
                      f"{shadow.parent} on PATH")
    return done, errors


def _apply_git_credentials(options: dict[str, Any], system: str | None = None) -> tuple[list[str], list[str]]:
    """Point git at fj's login for the global host, re-checking each step on disk:

    1. write / refresh rig's helper in ``bin_dir`` — never over a file that is not rig's (and then
       git is not pointed at it either);
    2. make ``credential.https://<host>.helper`` end with an empty value + rig's helper: rig's own
       values (the helper, empty resets) are dropped and appended again; any other value is kept
       where it is and reported — the empty reset after it is what makes git skip it;
    3. remove the Git Credential Manager OAuth settings an older rig wrote (only rig's exact values).
    """
    done: list[str] = []
    errors: list[str] = []
    path = git_helper_path(options)
    desired = git_helper_bytes(system)
    state = classify_bin_file(path, desired, GIT_HELPER_MARKER)
    if state in ("missing", "stale"):
        try:
            _write_atomic(path, desired)
        except OSError as exc:
            return done, [f"fj: could not write {path}: {exc}"]
        done.append(f"{'wrote' if state == 'missing' else 'refreshed'} rig's git credential helper {path}")
    elif state != "ok":
        return done, [f"fj: {path} is not rig's — move it away so rig can put its git credential helper there"]

    want = git_helper_value(options, system)
    for host in trusted_hosts(options):
        key = git_credential_key(host)
        values = git_global_values(key)
        if values is None:
            errors.append(f"fj: `git config --global` can't be read (git not on PATH, or the global "
                          f"config does not parse) — could not set {key}")
            continue
        if not _helper_entries_current(values, want):
            steps = [("--fixed-value", "--unset-all", key, v) for v in dict.fromkeys([want, ""]) if v in values]
            steps += [("--add", key, ""), ("--add", key, want)]
            failed = _run_git_steps(steps)
            if failed:
                errors.append(failed)
            else:
                done.append(f"git over https://{host} uses fj's login (rig's helper ends {key})")
        errors.extend(f"fj: {_foreign_helper_detail(key, value)}" for value in _foreign_helpers(values, want))
        legacy = _legacy_gcm_keys(host)
        failed = _run_git_steps([("--fixed-value", "--unset", k, v) for k, v in legacy])
        if failed:
            errors.append(failed)
        elif legacy:
            done.append(f"removed the Git Credential Manager OAuth settings an older rig wrote for https://{host}")
    return done, errors


def _run_git_steps(steps: list[tuple[str, ...]]) -> str | None:
    """Run ``git config --global <step>`` in order; the first failure as an error line, else ``None``."""
    for step in steps:
        res = _git_global(*step)
        if res is None or res.returncode != 0:
            why = "git is not on PATH" if res is None else res.stderr.strip()
            return f"fj: git config --global {' '.join(shlex.quote(a) for a in step)} failed: {why}"
    return None


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(dir=path.parent, prefix=".fj-", delete=False)
    try:
        with tmp:
            tmp.write(data)
        os.chmod(tmp.name, 0o755)
        os.replace(tmp.name, path)
    except BaseException:  # never leave a half-written .fj-* file behind
        Path(tmp.name).unlink(missing_ok=True)
        raise


def _prune_store(keep: str, system: str | None = None) -> None:
    """Best-effort: drop other versions from rig's store — only dirs holding nothing but a fj
    binary (a Windows fj.exe that is still running stays until the next apply)."""
    root = fj_store_root(system)
    if not root.is_dir():
        return
    for child in root.iterdir():
        if child.name == keep or child.is_symlink() or not child.is_dir():
            continue
        try:
            if all(e.name in ("fj", "fj.exe") or e.name.startswith(".fj-") for e in child.iterdir()):
                shutil.rmtree(child)
        except OSError:
            pass


def install_fj(version: str, *, system: str | None = None, fetch=None) -> str:
    """Install the real fj; return a one-line note. Raises :class:`FjError` with the remedy on failure.

    macOS → ``brew install forgejo-cli``. Linux/Windows → download the pinned release archive,
    verify its sha256 (and the binary's), and extract the single ``fj``/``fj.exe`` into rig's store
    ``<data>/rig/fj/<version>/`` (:func:`fj_store_root`) — never onto PATH, where ``fj`` is the wrapper.
    """
    system = (system or platform.system()).lower()
    if system == "darwin":
        brew = shutil.which("brew")
        if not brew:
            raise FjError("fj: Homebrew is not on PATH — install it (https://brew.sh), then `brew install forgejo-cli`")
        try:
            res = subprocess.run([brew, "install", "forgejo-cli"], capture_output=True, text=True, timeout=900)
        except (OSError, subprocess.SubprocessError) as exc:
            raise FjError(f"fj: `brew install forgejo-cli` failed: {exc}") from exc
        if res.returncode != 0:
            raise FjError(f"fj: `brew install forgejo-cli` exited {res.returncode}: {res.stderr.strip()[-300:]}")
        return "installed fj via `brew install forgejo-cli`"

    asset = fj_release_asset(system)
    if asset is None:
        raise FjError(f"fj: no release binary for {system}/{platform.machine()} — `cargo install forgejo-cli`")
    pinned = FJ_RELEASE_SHA256.get(version, {}).get(asset)
    if not pinned:
        raise FjError(f"fj: version {version} has no pinned sha256 in rig — use fj.version: {DEFAULT_FJ_VERSION}")
    url = FJ_RELEASE_URL.format(version=version, asset=asset)
    data = (fetch or _fetch)(url)
    digest = hashlib.sha256(data).hexdigest()
    if digest != pinned:
        raise FjError(f"fj: sha256 mismatch for {url} (got {digest}, want {pinned}) — not installed")

    name = "fj.exe" if asset.endswith(".zip") else "fj"
    if asset.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            binary = zf.read(name)
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            member = tf.extractfile(name)
            if member is None:
                raise FjError(f"fj: {asset} has no `{name}` entry")
            binary = member.read()
    want_binary = FJ_BINARY_SHA256.get(version, {}).get(asset)
    if want_binary and hashlib.sha256(binary).hexdigest() != want_binary:
        raise FjError(f"fj: the `{name}` inside {asset} does not match its pinned sha256 — not installed")

    dest = fj_store_root(system) / version / name
    _write_atomic(dest, binary)
    return f"installed fj {version} → {dest}"


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 — fixed https URL
        return resp.read()


def read_windows_user_env(name: str) -> str | None:
    """The persisted per-user value (``HKCU\\Environment``), not this process's copy; ``None`` when
    it is unset or there is no registry to ask (not Windows)."""
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value)
    except OSError:
        return None


def set_windows_user_env(name: str, value: str) -> None:
    """Persist a per-user environment variable; ``setx`` also broadcasts the change so new shells
    (Cygwin, Git Bash, PowerShell) see it."""
    try:
        res = subprocess.run(["setx", name, value], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise FjError(f"setx {name} failed: {exc}") from exc
    if res.returncode != 0:
        raise FjError(f"setx {name} exited {res.returncode}: {(res.stderr or res.stdout).strip()}")
