"""``rig profile`` — carry the GLOBAL config (``~/.config/rig/config.yaml``) between machines.

What it is
----------
A private git repo (conventionally ``<forge>/<login>/.rig-profile``) holding exactly one file,
``config.yaml`` — a copy of this machine's global rig config. ``rig profile init [URL]`` clones it
into a rig-owned checkout (or seeds an empty remote with the local file); ``rig profile sync``
moves changes BOTH ways; ``rig profile status`` says which side changed, without writing.
``profile.auto_sync: true`` makes ``rig apply commit`` sync first (non-fatal).

Layout (derived from :func:`riglib.config.global_config_path`, so ``$XDG_CONFIG_HOME`` is honored)::

    ~/.config/rig/config.yaml          the live global config (the "local" side)
    ~/.config/rig/profile/repo/        rig-owned git checkout; repo/config.yaml is the "remote" side
    ~/.config/rig/profile/state.json   last-synced snapshot hash — BESIDE the checkout, never in it

The three-way compare
---------------------
``L`` = local file, ``R`` = ``config.yaml`` at the checkout's HEAD after a fast-forward to the
remote (read from git, not the working tree), ``S`` = the hash recorded at the last successful
sync. Hashes are over LF-normalized bytes (Windows ``core.autocrlf`` must not read as a change).

- ``L == R``                 → in sync (the snapshot is refreshed).
- ``R`` missing              → push (seed the repo with the local file).
- ``L`` missing              → pull (adopt the remote file; a deletion is never propagated).
- ``R == S``, ``L != S``     → only local changed → copy into the repo, commit, push.
- ``L == S``, ``R != S``     → only remote changed → back up the local file
  (``config.yaml.rig-bak-<stamp>``, :func:`riglib.actions.fsutil.write_file`) and copy the repo's.
- anything else (both changed, or no snapshot yet) → CONFLICT: nothing is written; the user picks
  ``--take local`` / ``--take remote``. There is no silent merge.

Every file that crosses the boundary is validated with :func:`riglib.config.validate` first — a
broken or too-new config is never pushed nor adopted.

Invariant: after any sync — success or failure — the checkout's HEAD equals the remote branch (a
failed push rolls the local commit back), so there are never unpushed commits to reason about.

Security
--------
``env.vars`` (see ``rig env``) can hold API keys, and this file travels in plaintext. Every push
of a config carrying ``env.vars`` warns (loudly the first time). A push is REFUSED when the forge
reports the repo as public to an anonymous API request (:func:`repo_visibility`), and a config with
``env.vars`` is pushed to a forge URL ONLY when that request proves the repo private (GitHub, or a
Forgejo/Gitea host) — an unknown forge or an unreachable API refuses instead of guessing. The repo
MUST be private. Commits use the machine's git identity (the ``git:`` area provisions it); if
there is none, the push fails naming ``git.config.user.name/email``. The checkout is rig's own
clone of a one-file config repo with no PR flow, so two agent-tools global hooks are switched off
in THIS checkout only, through the dispatcher's trusted per-repo opt-out (repo-local
``hooks.skipGlobal`` — :func:`_prepare_checkout`): ``protect-main`` (no PR to open) and
``secret-scan`` (the privacy rule above guards the secrets this repo exists to carry). Every
other global hook still runs.

Stdlib-only at import time; ``yaml`` is imported lazily through :mod:`riglib.config`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

PROFILE_FILE = "config.yaml"
REPO_NAME = ".rig-profile"
DEFAULT_HOST = "git.hyperide.ai"
DEFAULT_BRANCH = "main"
STATE_VERSION = 1

_NETWORK_TIMEOUT = 180
_LOCAL_TIMEOUT = 30
_LOGIN_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_WHOAMI_RE = re.compile(r"signed in (?:to|as) ([A-Za-z0-9._-]+)@(\S+)")

TAKE_LOCAL = "local"
TAKE_REMOTE = "remote"

# Exit codes (the CLI returns these): 0 ok · 1 operational failure (git/network/refusal) ·
# 2 not set up / invalid input · 3 conflict or (status) a side changed.
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_CONFLICT = 3


class ProfileError(Exception):
    """A profile operation that cannot proceed. ``fix`` is the concrete next step."""

    def __init__(self, what: str, *, fix: str = "", code: int = EXIT_FAILED) -> None:
        super().__init__(what)
        self.what = what
        self.fix = fix
        self.code = code


@dataclass
class Outcome:
    """What an init/sync did. ``messages`` are ``(level, text)`` with level info|warn|error."""

    result: str  # in_sync | pushed | pulled | conflict | nothing
    detail: str
    code: int = EXIT_OK
    backup: Path | None = None
    messages: list[tuple[str, str]] = field(default_factory=list)


# ── paths + state ──────────────────────────────────────────────────────────────────────────
def profile_root() -> Path:
    from .config import global_config_path

    return global_config_path().parent / "profile"


def checkout_dir() -> Path:
    return profile_root() / "repo"


def state_path() -> Path:
    return profile_root() / "state.json"


def local_config_path() -> Path:
    from .config import global_config_path

    return global_config_path()


def digest(data: bytes | None) -> str | None:
    """sha256 over LF-normalized bytes (a CRLF checkout must not read as a change); None = absent."""
    if data is None:
        return None
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def read_state() -> dict[str, Any]:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(state: dict[str, Any]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes() if path.is_file() else None
    except OSError as exc:
        raise ProfileError(f"cannot read {path}: {exc}") from exc


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── git plumbing ───────────────────────────────────────────────────────────────────────────
def _git(
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = _LOCAL_TIMEOUT,
    env_extra: dict[str, str] | None = None,
    noninteractive: bool = False,
) -> subprocess.CompletedProcess:
    """Run git; never raises on a non-zero exit (callers decide). Missing git → ProfileError."""
    env = dict(os.environ)
    if noninteractive:
        env["GIT_TERMINAL_PROMPT"] = "0"
    if env_extra:
        env.update(env_extra)
    cmd = ["git"] + (["-C", str(cwd)] if cwd is not None else []) + args
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env,
        )
    except FileNotFoundError as exc:
        raise ProfileError("git is not installed", fix="install git, then retry", code=EXIT_USAGE) from exc
    except subprocess.TimeoutExpired as exc:
        raise ProfileError(f"`git {' '.join(args[:2])}` timed out after {timeout}s") from exc


def _git_ok(args: list[str], **kw: Any) -> str:
    """Run git and return stdout, raising ProfileError with git's stderr on failure."""
    res = _git(args, **kw)
    if res.returncode != 0:
        msg = (res.stderr or res.stdout).strip().splitlines()
        raise ProfileError(f"`git {' '.join(args[:2])}` failed: {msg[-1] if msg else f'exit {res.returncode}'}")
    return res.stdout


# Global git-hook fragments (agent-tools dispatcher ids) that don't apply to the profile checkout:
# ``protect-main`` blocks direct pushes to main to force a PR, and a one-file config repo that rig
# syncs has no PR to open. ``secret-scan`` would refuse the very thing this repo exists to carry —
# ``env.vars`` API keys — so it is replaced here by :func:`_push_local`'s own rule: a config with
# ``env.vars`` only ever goes to a repo the forge confirms is private.
_SKIP_GLOBAL_HOOKS = ("protect-main", "secret-scan")


def _prepare_checkout(repo: Path) -> None:
    """Idempotent repo-local settings of the profile checkout: LF line endings, and the
    dispatcher's per-repo opt-out (``hooks.skipGlobal``) for :data:`_SKIP_GLOBAL_HOOKS`.

    ``hooks.skipGlobal`` is the dispatcher's TRUSTED opt-out — it lives in this machine's git config,
    which a cloned repo cannot set — and it is scoped to this one checkout, so every other repo stays
    protected. Called on init and before every push (so checkouts made by older rigs get it too).
    """
    _git(["config", "core.autocrlf", "false"], cwd=repo)
    _git(["config", "core.eol", "lf"], cwd=repo)
    have = set(_git(["config", "--local", "--get-all", "hooks.skipGlobal"], cwd=repo).stdout.split())
    for hook_id in _SKIP_GLOBAL_HOOKS:
        if hook_id not in have:
            _git(["config", "--local", "--add", "hooks.skipGlobal", hook_id], cwd=repo)


def is_checkout(repo: Path) -> bool:
    return (repo / ".git").exists()


def origin_url(repo: Path) -> str | None:
    res = _git(["remote", "get-url", "origin"], cwd=repo)
    if res.returncode != 0:
        return None
    return res.stdout.strip() or None


def current_branch(repo: Path) -> str:
    res = _git(["symbolic-ref", "--short", "-q", "HEAD"], cwd=repo)
    return res.stdout.strip() or DEFAULT_BRANCH


def _has_commits(repo: Path) -> bool:
    return _git(["rev-parse", "--verify", "-q", "HEAD"], cwd=repo).returncode == 0


def _upstream_ref(repo: Path, branch: str) -> str | None:
    ref = f"refs/remotes/origin/{branch}"
    return ref if _git(["rev-parse", "--verify", "-q", ref], cwd=repo).returncode == 0 else None


def _remote_file(repo: Path, branch: str) -> bytes | None:
    """The REMOTE side: ``config.yaml`` on ``origin/<branch>``; None when the remote has no branch.

    Never HEAD: a local commit with no upstream (a first push that failed half-way, a deleted
    remote branch) is exactly content the remote does NOT have — reading it as the remote side
    would turn "never pushed" into a silent "in sync".
    """
    upstream = _upstream_ref(repo, branch)
    return _file_at(repo, upstream) if upstream else None


def _file_at(repo: Path, rev: str) -> bytes | None:
    """``config.yaml`` as stored at ``rev`` (raw blob bytes), or None when absent / no such rev."""
    try:
        res = subprocess.run(
            ["git", "-C", str(repo), "show", f"{rev}:{PROFILE_FILE}"],
            capture_output=True, timeout=_LOCAL_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout if res.returncode == 0 else None


def _fetch(repo: Path, *, noninteractive: bool) -> None:
    res = _git(["fetch", "--quiet", "--prune", "origin"], cwd=repo, timeout=_NETWORK_TIMEOUT,
               noninteractive=noninteractive)
    if res.returncode != 0:
        tail = (res.stderr or "").strip().splitlines()
        raise ProfileError(
            f"cannot reach the profile remote: {tail[-1] if tail else f'git fetch exit {res.returncode}'}",
            fix="check the network / your forge credentials, then retry",
        )


def _tracked_changes(repo: Path) -> bool:
    res = _git(["status", "--porcelain", "--untracked-files=no"], cwd=repo)
    return res.returncode == 0 and bool(res.stdout.strip())


def _converge_to_remote(repo: Path, branch: str, *, force: bool) -> None:
    """Make HEAD equal the remote branch. ``force`` (a ``--take``) resets; otherwise fast-forward only."""
    upstream = _upstream_ref(repo, branch)
    if upstream is None:
        return  # an empty remote (nothing to converge to) — the seed path handles it
    if force:
        _git_ok(["reset", "--quiet", "--hard", upstream], cwd=repo)
        return
    if _tracked_changes(repo):
        raise ProfileError(
            f"the profile checkout {repo} has uncommitted changes (rig owns this checkout)",
            fix="`rig profile sync --take local` or `--take remote` resets it",
        )
    if not _has_commits(repo):
        _git_ok(["reset", "--quiet", "--hard", upstream], cwd=repo)
        return
    res = _git(["merge", "--ff-only", "--quiet", upstream], cwd=repo)
    if res.returncode != 0:
        raise ProfileError(
            f"the profile checkout {repo} has commits the remote does not (rig never keeps any)",
            fix="`rig profile sync --take local` or `--take remote` resets it to the remote first",
        )


# ── validation of a file about to cross the boundary ─────────────────────────────────────
def _validated(data: bytes, what: str) -> dict[str, Any]:
    from .config import ConfigError, validate

    import yaml  # lazy (AGENTS.md: stdlib-only at import time)

    try:
        doc = yaml.safe_load(data.decode("utf-8")) or {}
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ProfileError(f"{what} is not valid YAML: {exc}", code=EXIT_USAGE) from exc
    if not isinstance(doc, dict):
        raise ProfileError(f"{what} is not a YAML mapping", code=EXIT_USAGE)
    try:
        validate(doc)
    except ConfigError as exc:
        raise ProfileError(
            f"{what} does not validate: {exc}",
            fix="fix the file (or upgrade rig if it uses newer keys), then retry",
            code=EXIT_USAGE,
        ) from exc
    return doc


def env_var_names(doc: dict[str, Any]) -> list[str]:
    env = doc.get("env")
    names = env.get("vars") if isinstance(env, dict) else None
    return sorted(str(k) for k in names) if isinstance(names, dict) else []


# ── forge probes (both are seams tests monkeypatch) ──────────────────────────────────────────
def _parse_forge_url(url: str) -> tuple[str, str, str] | None:
    """``(host, owner, repo)`` for an https / ssh / scp-style forge URL, else None (local paths)."""
    m = re.match(r"^https://(?:[^@/]+@)?([^/:]+)(?::\d+)?/([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    if not m:
        m = re.match(r"^ssh://(?:[^@/]+@)?([^/:]+)(?::\d+)?/([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    if not m:
        m = re.match(r"^[^@/\s]+@([^:/\s]+):([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    return (m.group(1), m.group(2), m.group(3)) if m else None


def _anonymous_get(api: str) -> tuple[int, Any]:
    """``(status, parsed JSON body)`` of an unauthenticated GET — ``(0, None)`` when it can't be
    answered: offline, not JSON, or REDIRECTED (a redirect is a different resource, possibly on
    another host, so it proves nothing about this one)."""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(api, headers={"Accept": "application/json", "User-Agent": "rig-cli"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 — https only, by construction
            if getattr(resp, "geturl", lambda: api)() != api:
                return 0, None
            return 200, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (OSError, ValueError):
        return 0, None


def repo_visibility(url: str) -> str | None:
    """Ask the forge ANONYMOUSLY whether the repo is visible: ``"public"``, ``"private"`` or None.

    A repo an unauthenticated API request can read is public by definition. A 404 means "not
    visible anonymously" ONLY where that API is known to exist — GitHub (``api.github.com``) and a
    host that answers Forgejo/Gitea's ``/api/v1/version``. Any other forge (GitLab, Bitbucket, …)
    404s that path for a PUBLIC repo too, so there it is ``None``, as are a local path, an unknown
    URL shape, a timeout, a redirect and every other status: "not verified private".
    """
    from urllib.parse import quote

    parsed = _parse_forge_url(url)
    if parsed is None:
        return None
    host, owner, name = parsed
    path = f"repos/{quote(owner, safe='')}/{quote(name, safe='')}"
    if host.lower() == "github.com":
        api = f"https://api.github.com/{path}"
    else:
        status, body = _anonymous_get(f"https://{host}/api/v1/version")
        if status != 200 or not isinstance(body, dict) or "version" not in body:
            return None  # not a Forgejo/Gitea API: its 404s would say nothing
        api = f"https://{host}/api/v1/{path}"
    status, body = _anonymous_get(api)
    if status == 404:
        return "private"
    if status == 200 and isinstance(body, dict) and body.get("private") is False:
        return "public"
    if status == 200 and isinstance(body, dict) and body.get("private") is True:
        return "private"
    return None


def fj_login(host: str | None = None) -> tuple[str, str | None] | None:
    """``(login, host|None)`` from ``fj whoami`` when ``fj`` is installed and says so; else None.

    Fail-closed on output we do not recognize: ``currently signed in to <login>@<host>`` or a bare
    login token are accepted; anything else means "can't tell cheaply" and the caller asks for a URL.
    """
    from .fj import find_real_fj

    # The REAL fj, not rig's `fj` wrapper (on Windows an sh script + .cmd shim a native subprocess
    # needn't go through): rig's pinned copy, else the first non-wrapper fj on PATH.
    fj = find_real_fj()
    if fj is None:
        return None
    cmd = [fj] + (["--host", host] if host else []) + ["whoami"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    return parse_whoami(res.stdout)


def parse_whoami(output: str) -> tuple[str, str | None] | None:
    """``(login, host|None)`` from ``fj whoami`` output; None for anything unrecognized."""
    text = output.strip()
    m = _WHOAMI_RE.search(text)
    if m:
        return m.group(1), m.group(2).rstrip("/.") or None
    if _LOGIN_RE.match(text):
        return text, None
    return None


def default_url(host: str | None = None) -> str | None:
    """``https://<host>/<login>/.rig-profile`` when a login is cheaply known (``fj whoami``)."""
    who = fj_login(host)
    if who is None:
        return None
    login, fj_host = who
    chosen = host or fj_host or os.environ.get("FJ_FALLBACK_HOST", "").strip() or DEFAULT_HOST
    chosen = re.sub(r"^[a-z]+://", "", chosen).rstrip("/")
    return f"https://{chosen}/{login}/{REPO_NAME}"


def create_repo_hint(url: str | None) -> str:
    parsed = _parse_forge_url(url) if url else None
    if parsed and parsed[0].lower() == "github.com":
        return f"gh repo create {REPO_NAME} --private"
    return f"fj repo create {REPO_NAME} --private"


# ── identity ───────────────────────────────────────────────────────────────────────────────
def _identity_args(repo: Path, doc: dict[str, Any]) -> list[str]:
    """``-c user.x=…`` overrides for the profile commit, or [] when git already has an identity.

    The machine's identity wins (whatever ``git config`` resolves in the checkout, or the
    GIT_AUTHOR_*/GIT_COMMITTER_* env). Missing parts fall back to the config's own
    ``git.config.user.name/email`` (declared but maybe not applied yet). Nothing at all → fail.
    """
    declared: dict[str, str] = {}
    git_block = doc.get("git")
    cfg = git_block.get("config") if isinstance(git_block, dict) else None
    if isinstance(cfg, dict):
        for key, value in cfg.items():
            if isinstance(key, str) and key.lower() in ("user.name", "user.email") and isinstance(value, str):
                declared[key.lower()] = value
    args: list[str] = []
    missing: list[str] = []
    for key, env_names in (("user.name", ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME")),
                           ("user.email", ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"))):
        have = _git(["config", "--get", key], cwd=repo).stdout.strip()
        if have or all(os.environ.get(n, "").strip() for n in env_names):
            continue
        if declared.get(key):
            args += ["-c", f"{key}={declared[key]}"]
        else:
            missing.append(key)
    if missing:
        raise ProfileError(
            f"no git identity for the profile commit ({', '.join(missing)} unset)",
            fix="declare git.config.user.name / git.config.user.email in ~/.config/rig/config.yaml "
            "and run `rig apply commit` (or `git config --global user.name …`), then retry",
            code=EXIT_USAGE,
        )
    return args


# ── the two directions ───────────────────────────────────────────────────────────────────────
Notify = Callable[[str, str], None]


def _warn_env_vars(doc: dict[str, Any], url: str, first_push: bool, notify: Notify) -> None:
    names = env_var_names(doc)
    if not names:
        return
    if first_push:
        notify("warn", f"WARNING: this config carries env.vars ({', '.join(names)}) — often API keys.")
        notify("warn", f"  Their values are about to be pushed in PLAINTEXT to {url}.")
        notify("warn", "  The profile repo MUST be private. Move secrets out of env.vars if it is not.")
    else:
        notify("warn", f"note: pushing env.vars ({', '.join(names)}) to the profile repo")


def _push_local(
    repo: Path, branch: str, url: str, local: bytes, state: dict[str, Any], *,
    noninteractive: bool, notify: Notify,
) -> Outcome:
    doc = _validated(local, f"local config {local_config_path()}")
    visibility = repo_visibility(url)
    if visibility == "public":
        raise ProfileError(
            f"refusing to push: the forge reports {url} as PUBLIC",
            fix="make the repo private on the forge (it holds your whole global config), then retry",
        )
    if visibility is None and _parse_forge_url(url) is not None:
        secrets = env_var_names(doc)
        if secrets:
            # Fail closed: env.vars are pushed in plaintext, and nothing proved this repo private.
            raise ProfileError(
                f"refusing to push env.vars ({', '.join(secrets)}) to {url}: could not verify the repo "
                "is private (rig checks through the GitHub or Forgejo API; this forge is unknown or "
                "did not answer)",
                fix="use a private repo on GitHub or a Forgejo host, retry when the forge is reachable, "
                "or move the secrets out of env.vars",
            )
        notify("info", "note: could not verify the repo is private (forge API unreachable or unknown) — make sure it is")
    _warn_env_vars(doc, url, first_push=not state.get("pushed"), notify=notify)
    identity = _identity_args(repo, doc)

    normalized = local.replace(b"\r\n", b"\n")
    _prepare_checkout(repo)  # before the commit: its pre-commit hooks read hooks.skipGlobal too
    before = _git(["rev-parse", "--verify", "-q", "HEAD"], cwd=repo).stdout.strip() or None
    (repo / PROFILE_FILE).write_bytes(normalized)
    _git_ok(["add", "--", PROFILE_FILE], cwd=repo)
    # nothing staged = HEAD already holds this content but the remote does not (a stray local
    # commit, or a remote branch deleted under us): no new commit — just push what HEAD has.
    if _git(["diff", "--cached", "--quiet"], cwd=repo).returncode != 0:
        host = socket.gethostname() or "unknown host"
        commit = _git(identity + ["commit", "--quiet", "-m", f"rig profile: {PROFILE_FILE} from {host}"], cwd=repo)
        if commit.returncode != 0:
            _rollback(repo, before)
            tail = (commit.stderr or commit.stdout).strip()
            raise ProfileError(f"the profile commit failed (a pre-commit hook?): {tail[-400:]}")
    push = _git(["push", "--quiet", "-u", "origin", f"HEAD:refs/heads/{branch}"], cwd=repo,
                timeout=_NETWORK_TIMEOUT, noninteractive=noninteractive)
    if push.returncode != 0:
        _rollback(repo, before)
        tail = (push.stderr or "").strip().splitlines()
        raise ProfileError(
            f"push to {url} failed: {tail[-1] if tail else f'exit {push.returncode}'}",
            fix="nothing was lost (the local file is untouched); fix access/network and re-run `rig profile sync`",
        )
    state.update({"repo": url, "branch": branch, "synced_sha256": digest(local), "synced_at": _now(), "pushed": True})
    write_state(state)
    return Outcome("pushed", f"pushed {local_config_path()} → {url} ({branch})")


def _rollback(repo: Path, before: str | None) -> None:
    """Undo a local profile commit so HEAD equals the remote again (the no-unpushed invariant)."""
    if before is not None:
        _git(["reset", "--quiet", "--hard", before], cwd=repo)
        return
    _git(["update-ref", "-d", "HEAD"], cwd=repo)
    _git(["rm", "--quiet", "--cached", "--ignore-unmatch", "--", PROFILE_FILE], cwd=repo)
    try:
        (repo / PROFILE_FILE).unlink()
    except OSError:
        pass


def _pull_remote(remote: bytes, state: dict[str, Any], url: str, branch: str) -> Outcome:
    from .actions import fsutil

    _validated(remote, f"remote profile config ({url})")
    target = local_config_path()
    text = remote.replace(b"\r\n", b"\n").decode("utf-8")
    try:
        out = fsutil.write_file(target, text, "backup")
    except OSError as exc:
        raise ProfileError(f"cannot write {target}: {exc}") from exc
    state.update({"repo": url, "branch": branch, "synced_sha256": digest(remote), "synced_at": _now()})
    write_state(state)
    detail = f"pulled {url} → {target}"
    if out.backup is not None:
        detail += f" (previous file backed up → {out.backup})"
    return Outcome("pulled", detail, backup=out.backup)


def classify(local: str | None, remote: str | None, snapshot: str | None) -> str:
    """The three-way decision over digests: ``in_sync`` | ``push`` | ``pull`` | ``conflict``."""
    if local == remote:
        return "in_sync"
    if remote is None:
        return "push"
    if local is None:
        return "pull"
    if snapshot == remote:
        return "push"
    if snapshot == local:
        return "pull"
    return "conflict"


def _conflict(repo: Path, snapshot: str | None) -> Outcome:
    why = "both changed since the last sync" if snapshot else "they differ and there is no sync record yet"
    return Outcome(
        "conflict",
        f"the local config and the profile repo {why} — nothing was written",
        code=EXIT_CONFLICT,
        messages=[
            ("info", f"  local:  {local_config_path()}"),
            ("info", f"  remote: {repo / PROFILE_FILE}"),
            ("info", "  resolve: merge by hand into the LOCAL file, then `rig profile sync --take local`"),
            ("info", "           (or keep the remote: `rig profile sync --take remote` — the local file is backed up)"),
        ],
    )


def sync(*, take: str | None = None, noninteractive: bool = False, notify: Notify | None = None) -> Outcome:
    """One two-way sync. Raises :class:`ProfileError`; a conflict is an Outcome (code 3), not a raise."""
    say: Notify = notify or (lambda _level, _text: None)
    repo = checkout_dir()
    if not is_checkout(repo):
        raise ProfileError(
            f"no profile checkout at {repo}", fix="run `rig profile init [URL]` first", code=EXIT_USAGE,
        )
    url = origin_url(repo) or "origin"
    state = read_state()
    if state.get("repo") != url:
        state = {"repo": url}  # a different repo than the snapshot described — the snapshot is void
    _fetch(repo, noninteractive=noninteractive)
    branch = current_branch(repo)
    _converge_to_remote(repo, branch, force=take is not None)

    local = _read_bytes(local_config_path())
    remote = _remote_file(repo, branch)
    d_local, d_remote = digest(local), digest(remote)
    snapshot = state.get("synced_sha256")

    if take == TAKE_LOCAL:
        if local is None:
            raise ProfileError(f"--take local: {local_config_path()} does not exist", code=EXIT_USAGE)
        decision = "in_sync" if d_local == d_remote else "push"
    elif take == TAKE_REMOTE:
        if remote is None:
            raise ProfileError(f"--take remote: the profile repo has no {PROFILE_FILE}", code=EXIT_USAGE)
        decision = "in_sync" if d_local == d_remote else "pull"
    else:
        decision = classify(d_local, d_remote, snapshot)

    if decision == "in_sync":
        if local is None:
            return Outcome("nothing", "no config on either side — nothing to sync")
        if snapshot != d_local or state.get("branch") != branch:
            state.update({"synced_sha256": d_local, "synced_at": _now(), "branch": branch})
            write_state(state)
        return Outcome("in_sync", f"in sync with {url}")
    if decision == "conflict":
        return _conflict(repo, snapshot)
    if decision == "push":
        assert local is not None
        return _push_local(repo, branch, url, local, state, noninteractive=noninteractive, notify=say)
    assert remote is not None
    return _pull_remote(remote, state, url, branch)


# ── status (read-only) ───────────────────────────────────────────────────────────────────────
@dataclass
class Status:
    url: str
    checkout: Path
    local: str  # unchanged | changed | missing
    remote: str  # unchanged | changed | missing
    verdict: str  # in_sync | push | pull | conflict
    synced_at: str | None


def status(*, fetch: bool = False) -> Status:
    """Which side changed since the last sync. Writes nothing (``fetch`` only updates rig's refs)."""
    repo = checkout_dir()
    if not is_checkout(repo):
        raise ProfileError(
            f"no profile checkout at {repo}", fix="run `rig profile init [URL]` first", code=EXIT_USAGE,
        )
    url = origin_url(repo) or "origin"
    if fetch:
        _fetch(repo, noninteractive=True)
    state = read_state()
    snapshot = state.get("synced_sha256") if state.get("repo") == url else None
    remote = _remote_file(repo, current_branch(repo))
    local = _read_bytes(local_config_path())
    d_local, d_remote = digest(local), digest(remote)

    def _side(d: str | None) -> str:
        if d is None:
            return "missing"
        return "unchanged" if d == snapshot else "changed"

    return Status(url, repo, _side(d_local), _side(d_remote), classify(d_local, d_remote, snapshot),
                  state.get("synced_at"))


# ── init ───────────────────────────────────────────────────────────────────────────────────────
def _read_local_doc() -> dict[str, Any]:
    from .config import ConfigError, read_yaml_file

    path = local_config_path()
    if not path.is_file():
        return {}
    try:
        return read_yaml_file(path)
    except ConfigError as exc:
        raise ProfileError(f"{path}: {exc}", code=EXIT_USAGE) from exc


def record_profile_block(url: str, auto_sync: bool | None, notify: Notify) -> bool:
    """Write ``profile: {repo, auto_sync}`` into the local global config. True when it changed it.

    Never rewrites an existing ``profile.repo`` (another machine may spell the same repo as ssh vs
    https — flipping it on every sync would ping-pong); ``auto_sync`` is written only when asked
    (absent means ON — see :func:`auto_sync_enabled`). Goes through the same serializer as ``rig config set --global``
    (so YAML comments in the file are not preserved — the same documented trade-off).
    """
    from .config import ConfigError, validate
    from .state import SetupState

    path = local_config_path()
    doc = _read_local_doc()
    if not doc:
        doc = {"version": 1}
    block = doc.get("profile")
    block = dict(block) if isinstance(block, dict) else {}
    changed = False
    if not block.get("repo"):
        block["repo"] = url
        changed = True
    elif block["repo"] != url:
        notify("info", f"note: profile.repo in {path} is {block['repo']!r}; this checkout tracks {url!r} — left as is")
    if auto_sync is not None and block.get("auto_sync") is not auto_sync:
        block["auto_sync"] = auto_sync
        changed = True
    if not changed:
        return False
    doc["profile"] = block
    try:
        validate(doc)
    except ConfigError as exc:
        raise ProfileError(f"{path} would not validate with the profile block: {exc}", code=EXIT_USAGE) from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SetupState.from_dict(doc).to_yaml(), encoding="utf-8")
    return True


def _clone(url: str, repo: Path) -> None:
    repo.parent.mkdir(parents=True, exist_ok=True)
    res = _git(
        ["clone", "--quiet", "-c", "core.autocrlf=false", "-c", "core.eol=lf", "--", url, str(repo)],
        timeout=_NETWORK_TIMEOUT,
    )
    if res.returncode != 0:
        tail = (res.stderr or "").strip().splitlines()
        raise ProfileError(
            f"cannot clone {url}: {tail[-1] if tail else f'exit {res.returncode}'}",
            fix=f"create the (PRIVATE) repo first — e.g. `{create_repo_hint(url)}` — and check access",
        )


def init(
    url: str | None = None, *, host: str | None = None, auto_sync: bool | None = None,
    notify: Notify | None = None,
) -> Outcome:
    """Clone (or seed) the profile repo, record ``profile.repo``, then sync once."""
    from .config import ConfigError, validate

    say: Notify = notify or (lambda _level, _text: None)
    doc = _read_local_doc()
    configured = doc.get("profile", {}).get("repo") if isinstance(doc.get("profile"), dict) else None
    url = url or configured or default_url(host)
    if not url:
        raise ProfileError(
            "no profile repo URL given and none could be derived (no `fj` login found)",
            fix=f"pass it: `rig profile init https://{DEFAULT_HOST}/<login>/{REPO_NAME}` "
            f"(create it first, PRIVATE: `{create_repo_hint(None)}`)",
            code=EXIT_USAGE,
        )
    try:
        validate({"version": 1, "profile": {"repo": url}})
    except ConfigError as exc:
        raise ProfileError(str(exc), code=EXIT_USAGE) from exc

    repo = checkout_dir()
    if is_checkout(repo):
        existing = origin_url(repo)
        if existing != url:
            raise ProfileError(
                f"{repo} already tracks {existing}",
                fix=f"to switch repos, delete {repo} (it only holds a copy of your config) and re-run init",
                code=EXIT_USAGE,
            )
        say("info", f"profile checkout already present ({url}) — syncing")
    else:
        if repo.exists() and any(repo.iterdir()):
            raise ProfileError(f"{repo} exists and is not a git checkout", fix="move it away, then retry",
                               code=EXIT_USAGE)
        say("info", f"cloning {url} → {repo}")
        _clone(url, repo)
    _prepare_checkout(repo)

    state = read_state()
    if state.get("repo") != url:
        write_state({"repo": url})
    remote = _remote_file(repo, current_branch(repo))
    local = _read_bytes(local_config_path())

    if remote is None:
        # empty remote (or no config.yaml yet): seed it with this machine's config
        record_profile_block(url, auto_sync, say)
        return sync(take=TAKE_LOCAL, notify=say)
    if local is None:
        adopted = sync(take=TAKE_REMOTE, notify=say)
        if record_profile_block(url, auto_sync, say):
            pushed = sync(notify=say)
            adopted.messages.append(("info", pushed.detail))
        return adopted
    if digest(local) == digest(remote):
        state = read_state()
        state.update({"repo": url, "branch": current_branch(repo), "synced_sha256": digest(local),
                      "synced_at": _now()})
        write_state(state)
        if record_profile_block(url, auto_sync, say):
            return sync(notify=say)
        return Outcome("in_sync", f"in sync with {url}")
    # both sides exist and differ: do not guess, do not touch the local file
    return sync(notify=say)


# ── auto_sync (called by `rig apply commit`) ─────────────────────────────────────────────────
def auto_sync_enabled() -> bool:
    """True when the global config names a profile repo and does not opt out with
    ``auto_sync: false`` — a machine that has a profile keeps it in sync by default."""
    from .config import ConfigError, read_yaml_file

    path = local_config_path()
    if not path.is_file():
        return False
    try:
        doc = read_yaml_file(path)
    except ConfigError:
        return False
    block = doc.get("profile")
    return isinstance(block, dict) and bool(block.get("repo")) and block.get("auto_sync", True) is not False
