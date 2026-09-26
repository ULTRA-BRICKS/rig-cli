"""Forgejo access — install ``fj``, one-click OAuth login for fj AND git, a default host.

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
- **one-click login for git** — the same trick for Git Credential Manager (the credential helper
  Git for Windows ships): rig writes GCM's generic-OAuth settings for ``https://<host>``, so the
  first ``git push`` opens the browser once and GCM keeps the token;
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
install fj, write the wrappers, read/write the Windows user environment) are called only by the
runner (``actions/runner.py`` ``_do_provision_fj``).
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import re
import shlex
import shutil
import subprocess
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

# Forgejo's built-in public OAuth2 applications (same IDs on every instance that keeps the default
# ``[oauth2] DEFAULT_APPLICATIONS``). Their redirect URI is loopback-any-port, which fits both fj's
# fixed ``http://127.0.0.1:26218/`` and GCM's random port.
FORGEJO_TEA_CLIENT_ID = "d57cb8c4-630c-4168-8324-ec79935e18d4"
FORGEJO_GCM_CLIENT_ID = "e90ee53c-94e2-48ac-9358-a874fb9e0662"

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

    ``hosts`` is every host fj/git should be able to log in to (the block's ``host`` first, then
    ``fallback_host`` — the GLOBAL layer's host — when a repo overrides it). ``fallback_host`` is
    what ``FJ_FALLBACK_HOST`` points at: the machine default, never a single repo's override — so
    with no global host (``None``) rig sets no ``FJ_FALLBACK_HOST`` at all.
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
        "git_client_id": str(block.get("git_client_id", FORGEJO_GCM_CLIENT_ID)),
    }


def fallback_host_url(host: str) -> str:
    return f"https://{host}"


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


def fj_store_root(system: str | None = None) -> Path:
    """rig's own dir for the real fj it downloads (one subdir per version) — NOT on PATH.

    ``${XDG_DATA_HOME:-~/.local/share}/rig/fj``; on Windows always
    ``%USERPROFILE%\\.local\\share\\rig\\fj`` (rig runs as a native Windows program, and the wrapper
    resolves the same dir from Cygwin / Git Bash through ``$USERPROFILE``, whatever ``$HOME`` is).
    """
    if fj_platform(system) == "windows":
        base = Path(os.path.expanduser("~")) / ".local" / "share"
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


def is_rig_wrapper(path: str | Path) -> bool:
    """True when ``path`` is one of rig's fj wrappers (the marker sits in its first lines)."""
    try:
        with open(path, "rb") as fh:
            return WRAPPER_MARKER.encode() in fh.read(512)
    except OSError:
        return False


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


def classify_bin_file(path: Path, desired: bytes) -> str:
    """What sits at a wrapper path: ``missing`` / ``ok`` / ``stale`` (an older rig wrapper) /
    ``rig-binary`` (a real fj rig installed there itself) / ``foreign`` (anything else — rig leaves
    it alone: a symlink, a directory, the user's own fj or script)."""
    if not os.path.lexists(path):
        return "missing"
    if path.is_symlink() or not path.is_file():
        return "foreign"
    if is_rig_wrapper(path):
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
    if system == "windows":
        appdata = os.environ.get("APPDATA") or str(home / "AppData" / "Roaming")
        return Path(appdata) / "Cyborus" / "forgejo-cli" / "config" / "client_ids"
    if system == "darwin":
        return home / ".config" / "forgejo-cli" / "client_ids"
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(home / ".config")
    return Path(xdg) / "forgejo-cli" / "client_ids"


def desired_client_ids_text(existing: str, hosts: list[str], client_id: str) -> str:
    """``existing`` with exactly one ``<host> <client_id>`` line per host (rig-marked); every other
    line — other hosts, comments — is kept as is."""
    wanted = {h: f"{h} {client_id}  {CLIENT_IDS_MARKER}" for h in hosts}
    out: list[str] = []
    seen: set[str] = set()
    for line in existing.splitlines():
        first = line.split("#", 1)[0].split()
        host = first[0].lower() if first else None
        if host in wanted:
            if host not in seen:
                out.append(wanted[host])
                seen.add(host)
            continue
        out.append(line)
    out.extend(wanted[h] for h in hosts if h not in seen)
    return "\n".join(out) + "\n"


def git_credential_settings(host: str, client_id: str) -> dict[str, str]:
    """GCM generic-OAuth settings for ``https://<host>`` (Forgejo's built-in GCM app)."""
    section = f"credential.https://{host}"
    return {
        f"{section}.oauthClientId": client_id,
        f"{section}.oauthRedirectUri": "http://127.0.0.1",
        f"{section}.oauthAuthorizeEndpoint": "/login/oauth/authorize",
        f"{section}.oauthTokenEndpoint": "/login/oauth/access_token",
    }


_GCM_HELPER_NAMES = frozenset({"manager", "manager-core"})  # after dropping "git-credential-"


def is_gcm_helper(value: str) -> bool:
    """True when one ``credential.helper`` value runs Git Credential Manager.

    Parsed the way git runs it — through the shell — so quotes and backslash-escaped spaces
    (``/mnt/c/Program\\ Files/Git/mingw64/bin/git-credential-manager.exe``, the WSL setup) are one
    word; then the program's basename, case-folded, without ``.exe`` and ``git-credential-``:
    ``manager`` / ``manager-core`` / ``git-credential-manager[-core][.exe]`` by name or path.
    """
    text = value.strip().removeprefix("!").strip()
    if not text:
        return False
    try:
        words = shlex.split(text, posix=True)
    except ValueError:  # unbalanced quotes — take the first whitespace word
        words = text.split()
    if not words:
        return False
    name = re.split(r"[\\/]", words[0])[-1].lower()
    name = name.removesuffix(".exe").removeprefix("git-credential-")
    return name in _GCM_HELPER_NAMES


def git_uses_gcm() -> bool:
    """True when git's effective credential helper list includes Git Credential Manager.

    An empty ``credential.helper`` value resets the list (git's rule), so only helpers after the
    last empty one count."""
    try:
        out = subprocess.run(
            ["git", "config", "--get-all", "credential.helper"],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    helpers: list[str] = []
    for line in out.splitlines():
        if line.strip():
            helpers.append(line)
        else:
            helpers.clear()
    return any(is_gcm_helper(h) for h in helpers)


def git_config_get(key: str) -> str | None:
    try:
        res = subprocess.run(["git", "config", "--global", "--get", key], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return res.stdout.strip() if res.returncode == 0 else None


# ── what is out of date (shared by apply and status) ────────────────────────────────────────

# pending_changes items that belong to the `fj` COMMAND (real binary + wrappers + PATH), reconciled
# together by `_apply_fj_command`.
COMMAND_ITEMS = frozenset({"fj", "fj-wrapper", "fj.cmd", "fj.exe", "fj-path"})


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
    if desired_client_ids_text(existing, options["hosts"], options["client_id"]) != _normalized(existing):
        direction = "modified" if existing else "missing"
        todo.append((direction, "client_ids", cid_path,
                     f"fj can't log in to {', '.join(options['hosts'])} in the browser yet (no OAuth client ID)"))

    if windows and options["fallback_host"]:
        want = fallback_host_url(options["fallback_host"])
        have = read_windows_user_env(FALLBACK_HOST_VAR)
        if have != want:
            todo.append(("missing" if have is None else "modified", FALLBACK_HOST_VAR,
                         Path("HKCU/Environment"), f"{FALLBACK_HOST_VAR}={have or '<unset>'} (want {want})"))

    if options["git_credentials"] and git_uses_gcm():
        for host in options["hosts"]:
            for key, value in git_credential_settings(host, options["git_client_id"]).items():
                if git_config_get(key) != value:
                    todo.append(("missing", f"git-credential:{host}", Path("~/.gitconfig"),
                                 f"git config --global {key} is not {value}"))
                    break
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
            path.write_text(desired_client_ids_text(existing, options["hosts"], options["client_id"]), encoding="utf-8")
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
    if "git-credential" in items:
        for host in options["hosts"]:
            wrote = False
            for key, value in git_credential_settings(host, options["git_client_id"]).items():
                if git_config_get(key) == value:
                    continue
                try:
                    res = subprocess.run(["git", "config", "--global", key, value], capture_output=True, text=True, timeout=15)
                except (OSError, subprocess.SubprocessError) as exc:
                    errors.append(f"git config --global {key} failed: {exc}")
                    continue
                if res.returncode != 0:
                    errors.append(f"git config --global {key} failed: {res.stderr.strip()}")
                else:
                    wrote = True
            if wrote:
                done.append(f"git login via browser (GCM OAuth) for https://{host}")
    return done, errors


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
