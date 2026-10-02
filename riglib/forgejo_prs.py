"""Forgejo pull-request listing, normalized to the ``gh pr list --json`` record shape.

Why this exists: worktree GC (:mod:`riglib.worktree_gc`) and ``rig daily``
(:mod:`riglib.daily.github`) used to call ``gh pr list`` unconditionally, which silently fails (or
lies) for a canonical Forgejo repo. ``fj`` prints human text only — there is no ``--json`` — so
this reads Forgejo's REST API (``/api/v1/repos/{owner}/{repo}/pulls``) and emits dicts with the
SAME keys the GitHub parsers already understand (``number/state/headRefName/mergedAt/closedAt/url/
isCrossRepository/title/body/labels``), so those parsers stay untouched and shared.

Invariants: only hosts rig trusts as Forgejo (``git.hyperide.ai`` by default, plus the host of
``$FORGEJO_URL``) are routed here — anything else, GitHub included, stays on ``gh``. A token
(``$FORGEJO_TOKEN``) is attached only when ``$FORGEJO_URL`` names the SAME origin (same rule as
task-cli), and a redirect off the origin is refused so it cannot leak. Anonymous reads are the
fallback, which works for public repos such as ``ultrabricks/rig-cli``. Not read: fj's own token
store — a private repo without ``$FORGEJO_TOKEN`` therefore raises :class:`ForgeLookupError`, and
the callers already degrade safely on that.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_FORGEJO_HOSTS = ("git.hyperide.ai",)
_PAGE_SIZE = 50  # Forgejo's default MAX_RESPONSE_ITEMS; asking for more is silently clamped
_TIMEOUT_S = 30

HttpGet = Callable[[str, dict[str, str], int], tuple[int, str, str]]


class ForgeLookupError(RuntimeError):
    """The Forgejo pull-request list could not be fetched or trusted."""


@dataclass(frozen=True)
class ForgejoRepo:
    origin: str  # bare https origin, e.g. https://git.hyperide.ai
    owner: str
    name: str


def _trusted_hosts() -> set[str]:
    hosts = {h.lower() for h in DEFAULT_FORGEJO_HOSTS}
    env_host = urlsplit(os.environ.get("FORGEJO_URL", "")).hostname
    if env_host:
        hosts.add(env_host.lower())
    return hosts


def _split_remote(url: str) -> tuple[str, str, str] | None:
    """``(host, owner, repo)`` from an https / ssh / scp-style remote, else None."""
    patterns = (
        r"^https://(?:[^@/]+@)?([^/:]+)(?::\d+)?/([^/]+)/([^/]+?)(?:\.git)?/?$",
        r"^ssh://(?:[^@/]+@)?([^/:]+)(?::\d+)?/([^/]+)/([^/]+?)(?:\.git)?/?$",
        r"^[^@/\s]+@([^:/\s]+):([^/]+)/([^/]+?)(?:\.git)?/?$",
    )
    for pat in patterns:
        m = re.match(pat, url.strip())
        if m:
            return m.group(1), m.group(2), m.group(3)
    return None


def forgejo_repo_from_remote(url: str) -> ForgejoRepo | None:
    """The Forgejo repo a git remote URL points at, or None (GitHub / unknown host / local path)."""
    parts = _split_remote(url)
    if parts is None or parts[0].lower() not in _trusted_hosts():
        return None
    host, owner, name = parts
    return ForgejoRepo(f"https://{host.lower()}", owner, name)


def forgejo_repo_from_spec(spec: str) -> ForgejoRepo | None:
    """``host/owner/name`` (three segments, trusted Forgejo host) -> repo; ``owner/name`` -> None.

    ``rig daily`` repo specs are GitHub ``owner/name`` slugs by default; a host prefix opts a repo
    into Forgejo without changing what any existing two-segment slug means.
    """
    segs = spec.strip().split("/")
    if len(segs) != 3 or segs[0].lower() not in _trusted_hosts():
        return None
    return ForgejoRepo(f"https://{segs[0].lower()}", segs[1], segs[2])


def normalize_pull(pull: dict) -> dict:
    """One Forgejo pull object -> the ``gh pr list --json`` shape the GitHub parsers read."""
    head = pull.get("head") or {}
    head_repo = (head.get("repo") or {}).get("full_name")
    base_repo = ((pull.get("base") or {}).get("repo") or {}).get("full_name")
    if pull.get("merged"):
        state = "MERGED"
    else:
        state = "OPEN" if pull.get("state") == "open" else "CLOSED"
    return {
        "number": pull.get("number"),
        "state": state,
        "headRefName": head.get("ref"),
        "mergedAt": pull.get("merged_at"),
        "closedAt": pull.get("closed_at"),
        "url": pull.get("html_url", ""),
        "title": pull.get("title", ""),
        "body": pull.get("body") or "",
        "labels": [{"name": lbl.get("name", "")} for lbl in (pull.get("labels") or []) if isinstance(lbl, dict)],
        # a deleted fork has no head repo: cannot be proven same-repo, so treat as cross-repo
        "isCrossRepository": not head_repo or head_repo != base_repo,
    }


def _default_http_get(url: str, headers: dict[str, str], timeout: int) -> tuple[int, str, str]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — https origin only
            return resp.status, resp.read().decode("utf-8", "replace"), resp.geturl()
    except urllib.error.HTTPError as exc:
        return exc.code, "", url
    except (OSError, ValueError) as exc:
        raise ForgeLookupError(f"Forgejo request failed: {exc}") from exc


def _same_origin(a: str, b: str) -> bool:
    """Same https origin (host plus effective port), fail closed on malformed URLs."""
    try:
        pa, pb = urlsplit(a), urlsplit(b)
        porta = pa.port or (443 if pa.scheme == "https" else None)
        portb = pb.port or (443 if pb.scheme == "https" else None)
    except ValueError:
        return False
    return (
        pa.scheme == pb.scheme == "https"
        and (pa.hostname or "").lower() == (pb.hostname or "").lower()
        and porta == portb
    )


def _forgejo_host_key(origin: str) -> str:
    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    return f"{host}:{port}" if host and port and port != 443 else host


def _fj_key_files() -> list[Path]:
    """forgejo-cli 0.6 key-store candidates for the running platform.

    Exact-host lookup only; the store aliases map is intentionally ignored so one
    Forgejo host can never donate its token to another.
    """
    home = Path(os.environ.get("HOME") or os.path.expanduser("~"))
    xdg_data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    mac = home / "Library" / "Application Support" / "forgejo-cli.forgejo-cli" / "keys.json"
    linux = xdg_data / "forgejo-cli" / "keys.json"
    appdata = os.environ.get("APPDATA")
    windows = (
        Path(appdata) / "forgejo-cli" / "forgejo-cli" / "data" / "keys.json"
        if appdata
        else None
    )
    if sys.platform == "darwin":
        order = [mac, linux, windows]
    elif sys.platform == "win32":
        order = [windows, mac, linux]
    else:
        order = [linux, mac, windows]
    return [path for path in order if path is not None]


def _fj_store_token(host_key: str) -> str | None:
    if not host_key:
        return None
    for path in _fj_key_files():
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        hosts = data.get("hosts") if isinstance(data, dict) else None
        entry = hosts.get(host_key) if isinstance(hosts, dict) else None
        token = entry.get("token") if isinstance(entry, dict) else None
        if isinstance(token, str) and token.strip():
            return token.strip()
    return None


def _refresh_fj_login(host_key: str) -> None:
    """Best-effort OAuth refresh; never prompts and never stalls a read for long."""
    fj = shutil.which("fj")
    if not fj or not host_key:
        return
    try:
        subprocess.run(
            [fj, "-H", host_key, "whoami"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _auth_headers(origin: str) -> dict[str, str]:
    headers = {"Accept": "application/json", "User-Agent": "rig-cli"}
    token = os.environ.get("FORGEJO_TOKEN")
    forge_url = os.environ.get("FORGEJO_URL", "")
    if token and token.strip() and _same_origin(forge_url, origin):
        headers["Authorization"] = f"token {token.strip()}"
        return headers

    host_key = _forgejo_host_key(origin)
    token = _fj_store_token(host_key)
    if not token:
        _refresh_fj_login(host_key)
        token = _fj_store_token(host_key)
    if token:
        headers["Authorization"] = f"token {token}"
    return headers


def _get_page(target: ForgejoRepo, state: str, page: int, http_get: HttpGet) -> list:
    url = (
        f"{target.origin}/api/v1/repos/{target.owner}/{target.name}/pulls"
        f"?state={state}&page={page}&limit={_PAGE_SIZE}"
    )
    status, body, final = http_get(url, _auth_headers(target.origin), _TIMEOUT_S)
    if urlsplit(final).netloc.lower() != urlsplit(target.origin).netloc.lower():
        raise ForgeLookupError(f"Forgejo redirected off {target.origin}; refusing to follow")
    if status != 200:
        raise ForgeLookupError(
            f"Forgejo pulls list for {target.owner}/{target.name} returned HTTP {status} "
            "(private repo? set $FORGEJO_TOKEN and $FORGEJO_URL)"
        )
    try:
        data = json.loads(body or "[]")
    except ValueError as exc:
        raise ForgeLookupError("Forgejo pulls list returned unparseable JSON") from exc
    if not isinstance(data, list):
        raise ForgeLookupError(f"Forgejo pulls list returned a non-list JSON root ({type(data).__name__})")
    return data


def fetch_pulls(target: ForgejoRepo, *, state: str, limit: int, http_get: HttpGet | None = None) -> list[dict]:
    """Up to ``limit`` pulls (``state``: open|closed|all), newest first, in ``gh --json`` shape."""
    getter = http_get or _default_http_get
    out: list[dict] = []
    page = 1
    while len(out) < limit:
        chunk = _get_page(target, state, page, getter)
        out.extend(normalize_pull(p) for p in chunk if isinstance(p, dict))
        if len(chunk) < _PAGE_SIZE:
            break
        page += 1
    return out[:limit]
