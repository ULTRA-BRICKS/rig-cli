"""Forgejo access — install ``fj``, one-click OAuth login for fj AND git, a default host.

What this is
------------
The ecosystem lives on Forgejo (``git.hyperide.ai``). Out of the box its CLI ``fj``
(forgejo-cli) is unfriendly: outside a clone it needs ``--host`` on every call, and ``fj auth
login`` only does a browser login for a hard-coded list of public instances — everywhere else it
tells you to go mint a token by hand. git over HTTPS has the same problem. The ``fj:`` block
fixes all of it, per machine:

- **install fj** when it is missing — ``brew install forgejo-cli`` on macOS, the official release
  binary (sha256-pinned below) into ``~/.local/bin`` on Linux and Windows (Cygwin/Git Bash too);
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

This module is stdlib-only. The planning helpers are pure; the few effectful helpers (download +
install fj, read/write the Windows user environment) are called only by the runner
(``actions/runner.py`` ``_do_provision_fj``).
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import re
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

# Forgejo's built-in public OAuth2 applications (same IDs on every instance that keeps the default
# ``[oauth2] DEFAULT_APPLICATIONS``). Their redirect URI is loopback-any-port, which fits both fj's
# fixed ``http://127.0.0.1:26218/`` and GCM's random port.
FORGEJO_TEA_CLIENT_ID = "d57cb8c4-630c-4168-8324-ec79935e18d4"
FORGEJO_GCM_CLIENT_ID = "e90ee53c-94e2-48ac-9358-a874fb9e0662"

FALLBACK_HOST_VAR = "FJ_FALLBACK_HOST"
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
    what ``FJ_FALLBACK_HOST`` points at: the machine default, never a single repo's override.
    """
    host = normalize_host(str(block["host"]))
    fallback = normalize_host(fallback_host) if fallback_host else host
    hosts = [host] + ([fallback] if fallback != host else [])
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


def git_uses_gcm() -> bool:
    """True when git's effective credential helper is Git Credential Manager."""
    try:
        out = subprocess.run(
            ["git", "config", "--get-all", "credential.helper"],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    helpers = [h.strip() for h in out.splitlines() if h.strip()]
    return bool(helpers) and any(
        h.split()[0].rsplit("/", 1)[-1].replace("git-credential-", "") in {"manager", "manager-core", "git-credential-manager"}
        for h in helpers
    )


def git_config_get(key: str) -> str | None:
    try:
        res = subprocess.run(["git", "config", "--global", "--get", key], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return res.stdout.strip() if res.returncode == 0 else None


def find_fj(bin_dir: str | None = None) -> str | None:
    found = shutil.which("fj")
    if found:
        return found
    if bin_dir:
        candidate = Path(os.path.expanduser(bin_dir)) / ("fj.exe" if os.name == "nt" else "fj")
        if candidate.is_file():
            return str(candidate)
    return None


# ── what is out of date (shared by apply and status) ────────────────────────────────────────


def pending_changes(options: dict[str, Any], *, windows: bool | None = None) -> list[tuple[str, str, Path, str]]:
    """Every provisioning step still to do, as ``(direction, item, target, detail)`` — the ONE
    comparison both ``rig apply`` (``_do_provision_fj``) and ``rig status`` use. Read-only."""
    if windows is None:
        windows = os.name == "nt"
    todo: list[tuple[str, str, Path, str]] = []
    bin_dir = Path(os.path.expanduser(options["bin_dir"]))

    if options["install"] and not find_fj(options["bin_dir"]):
        todo.append(("missing", "fj", bin_dir, "fj is not installed (apply installs it)"))

    cid_path = client_ids_path()
    existing = cid_path.read_text(encoding="utf-8") if cid_path.is_file() else ""
    if desired_client_ids_text(existing, options["hosts"], options["client_id"]) != _normalized(existing):
        direction = "modified" if existing else "missing"
        todo.append((direction, "client_ids", cid_path,
                     f"fj can't log in to {', '.join(options['hosts'])} in the browser yet (no OAuth client ID)"))

    if windows:
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


def apply_changes(options: dict[str, Any], todo: list[tuple[str, str, Path, str]] | None = None) -> tuple[list[str], list[str]]:
    """Do every step :func:`pending_changes` lists; return ``(done, errors)`` — one line each.

    A failing step doesn't stop the others (fj missing must not block the git settings)."""
    if todo is None:
        todo = pending_changes(options)
    items = {item.split(":", 1)[0] for _, item, _, _ in todo}
    done: list[str] = []
    errors: list[str] = []
    if "fj" in items:
        try:
            done.append(install_fj(options["version"], options["bin_dir"]))
        except FjError as exc:
            errors.append(str(exc))
    if "client_ids" in items:
        path = client_ids_path()
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(desired_client_ids_text(existing, options["hosts"], options["client_id"]), encoding="utf-8")
        done.append(f"fj browser login enabled for {', '.join(options['hosts'])} ({path})")
    if FALLBACK_HOST_VAR in items:
        url = fallback_host_url(options["fallback_host"])
        try:
            set_windows_user_env(FALLBACK_HOST_VAR, url)
            done.append(f"{FALLBACK_HOST_VAR}={url} (new shells)")
        except FjError as exc:
            errors.append(str(exc))
    if "git-credential" in items:
        for host in options["hosts"]:
            for key, value in git_credential_settings(host, options["git_client_id"]).items():
                if git_config_get(key) == value:
                    continue
                res = subprocess.run(["git", "config", "--global", key, value], capture_output=True, text=True, timeout=15)
                if res.returncode != 0:
                    errors.append(f"git config --global {key} failed: {res.stderr.strip()}")
            done.append(f"git login via browser (GCM OAuth) for https://{host}")
    return done, errors


def _normalized(text: str) -> str:
    return text if not text or text.endswith("\n") else text + "\n"


# ── effectful helpers (runner only) ───────────────────────────────────────────────


def install_fj(version: str, bin_dir: str, *, system: str | None = None, fetch=None) -> str:
    """Install fj; return a one-line note. Raises :class:`FjError` with the remedy on failure.

    macOS → ``brew install forgejo-cli``. Linux/Windows → download the pinned release archive,
    verify its sha256, and extract the single ``fj``/``fj.exe`` binary into ``bin_dir``.
    """
    system = (system or platform.system()).lower()
    if system == "darwin":
        brew = shutil.which("brew")
        if not brew:
            raise FjError("fj: Homebrew is not on PATH — install it (https://brew.sh), then `brew install forgejo-cli`")
        res = subprocess.run([brew, "install", "forgejo-cli"], capture_output=True, text=True, timeout=900)
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

    dest_dir = Path(os.path.expanduser(bin_dir))
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    with tempfile.NamedTemporaryFile(dir=dest_dir, prefix=".fj-", delete=False) as tmp:
        tmp.write(binary)
    os.chmod(tmp.name, 0o755)
    os.replace(tmp.name, dest)
    return f"installed fj {version} → {dest}"


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 — fixed https URL
        return resp.read()


def read_windows_user_env(name: str) -> str | None:
    """The persisted per-user value (``HKCU\\Environment``), not this process's copy."""
    import winreg  # Windows-only; callers check os.name

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value)
    except OSError:
        return None


def set_windows_user_env(name: str, value: str) -> None:
    """Persist a per-user environment variable; ``setx`` also broadcasts the change so new shells
    (Cygwin, Git Bash, PowerShell) see it."""
    res = subprocess.run(["setx", name, value], capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        raise FjError(f"setx {name} exited {res.returncode}: {(res.stderr or res.stdout).strip()}")
