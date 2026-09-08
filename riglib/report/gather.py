"""Gather git log/diff since merge-base — HyperIDE result-report.sh `gather`.

Accessed via: ``rig report`` → :func:`gather`. Stdlib + the ``git`` binary only.
``gh pr view`` is intentionally omitted from this slice: the original script
treated a missing PR as ``{}``, and a report you can open does not need it.

Assumptions: ``cwd`` is a git work tree. Merge-base candidates are
``origin/main``, ``main``, then ``master`` unless ``--base`` is given.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import (
    EXIT_INTERNAL,
    ConfigError,
    MissingDepError,
    NotARepoError,
    RigError,
)

_GIT_TIMEOUT_S = 30
_DEFAULT_BASES = ("origin/main", "main", "master")


@dataclass(frozen=True)
class Commit:
    sha: str
    message: str
    date: str


@dataclass(frozen=True)
class FileChange:
    path: str
    added: int
    deleted: int
    status: str  # new | modified | deleted


@dataclass
class Gathered:
    repo_url: str
    head_sha: str
    branch: str
    base_ref: str
    commits: list[Commit] = field(default_factory=list)
    files: list[FileChange] = field(default_factory=list)

    @property
    def insertions(self) -> int:
        return sum(f.added for f in self.files)

    @property
    def deletions(self) -> int:
        return sum(f.deleted for f in self.files)


def gather(cwd: Path, *, base: str | None = None) -> Gathered:
    """Return commits + file stats for ``base..HEAD``. Raises :class:`NotARepoError`
    / :class:`MissingDepError` rather than a bare subprocess exception so the CLI
    can render what/why/fix."""
    _require_git_repo(cwd)
    try:
        head = _git_out(cwd, "rev-parse", "HEAD")
    except _GitFailed:
        raise ConfigError(
            what=f"repository has no commits: {cwd}",
            why="`git rev-parse HEAD` failed — this work tree has no HEAD yet.",
            fix="make at least one commit, then re-run `rig report`.",
        ) from None
    branch = _git_out(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    repo_url = _https_remote(cwd)
    base_ref, merge_base = _resolve_merge_base(cwd, base)
    commits = _commits_since(cwd, merge_base)
    files = _files_since(cwd, merge_base)
    return Gathered(
        repo_url=repo_url,
        head_sha=head,
        branch=branch,
        base_ref=base_ref,
        commits=commits,
        files=files,
    )


def _require_git_repo(cwd: Path) -> None:
    try:
        out = _git_out(cwd, "rev-parse", "--is-inside-work-tree")
    except MissingDepError:
        raise
    except _GitFailed:
        raise NotARepoError(
            what=f"not a git repository: {cwd}",
            why="`rig report` gathers commits and diffs from git; a non-repo has neither.",
            fix="cd into a git checkout, or pass `-C /path/to/repo`.",
        ) from None
    if out != "true":
        raise NotARepoError(
            what=f"not a git repository: {cwd}",
            why="git rev-parse --is-inside-work-tree did not return true.",
            fix="cd into a git checkout, or pass `-C /path/to/repo`.",
        )


def _resolve_merge_base(cwd: Path, requested: str | None) -> tuple[str, str]:
    if requested:
        try:
            mb = _git_out(cwd, "merge-base", requested, "HEAD")
        except _GitFailed:
            raise ConfigError(
                what=f"unknown --base ref {requested!r}",
                why="git could not find a merge-base between that ref and HEAD.",
                fix="pass an existing ref, e.g. `--base main` or `--base origin/main`.",
            ) from None
        return requested, mb
    for name in _DEFAULT_BASES:
        try:
            mb = _git_out(cwd, "merge-base", name, "HEAD")
        except _GitFailed:
            continue
        return name, mb
    # No default base resolved (unusual: no main/master). Empty unique range.
    head = _git_out(cwd, "rev-parse", "HEAD")
    return "HEAD", head


def _commits_since(cwd: Path, merge_base: str) -> list[Commit]:
    # NUL field separators: a tab in the subject is legal and must not steal the date.
    raw = _git_out(
        cwd,
        "log",
        "-z",
        "--format=%H%x00%s%x00%ci",
        f"{merge_base}..HEAD",
        keep_nul=True,
    )
    fields = _nul_tokens(raw)
    commits: list[Commit] = []
    for i in range(0, len(fields) - 2, 3):
        sha, message, date = fields[i], fields[i + 1], fields[i + 2]
        commits.append(Commit(sha=sha, message=message, date=date))
    return commits


def _files_since(cwd: Path, merge_base: str) -> list[FileChange]:
    status_by_path = _name_status(cwd, merge_base)
    raw = _git_out(cwd, "diff", "-z", "--numstat", f"{merge_base}..HEAD", keep_nul=True)
    tokens = _nul_tokens(raw)
    files: list[FileChange] = []
    i = 0
    while i < len(tokens):
        rec = tokens[i]
        i += 1
        if rec.count("\t") < 2:
            continue
        added_s, deleted_s, path = rec.split("\t", 2)
        if path == "" and i + 1 < len(tokens):
            # -z rename: "added\tdel\t" then old path, new path
            path = tokens[i + 1]
            i += 2
        added = 0 if added_s == "-" else _as_int(added_s)
        deleted = 0 if deleted_s == "-" else _as_int(deleted_s)
        status = status_by_path.get(path, "modified")
        files.append(FileChange(path=path, added=added, deleted=deleted, status=status))
    return files


def _name_status(cwd: Path, merge_base: str) -> dict[str, str]:
    raw = _git_out(
        cwd, "diff", "-z", "--name-status", f"{merge_base}..HEAD", keep_nul=True
    )
    mapping = {
        "A": "new",
        "D": "deleted",
        "M": "modified",
        "C": "modified",
        "T": "modified",
        "R": "modified",
    }
    tokens = _nul_tokens(raw)
    out: dict[str, str] = {}
    i = 0
    while i < len(tokens):
        code = tokens[i]
        i += 1
        kind = code[:1] if code else ""
        if kind in ("R", "C"):
            if i + 1 >= len(tokens):
                break
            _old, dest = tokens[i], tokens[i + 1]
            i += 2
            out[dest] = mapping.get(kind, "modified")
        else:
            if i >= len(tokens):
                break
            path = tokens[i]
            i += 1
            out[path] = mapping.get(kind, "modified")
    return out


def _nul_tokens(raw: str) -> list[str]:
    if not raw:
        return []
    parts = raw.split("\0")
    if parts and parts[-1] == "":
        parts = parts[:-1]
    return parts


def _https_remote(cwd: Path) -> str:
    try:
        url = _git_out(cwd, "remote", "get-url", "origin")
    except _GitFailed:
        return ""
    return _to_https(url)


def _to_https(url: str) -> str:
    u = url.strip()
    if u.endswith(".git"):
        u = u[:-4]
    if u.startswith("git@github.com:"):
        u = "https://github.com/" + u[len("git@github.com:") :]
    elif u.startswith("ssh://git@github.com/"):
        u = "https://github.com/" + u[len("ssh://git@github.com/") :]
    return _strip_userinfo(u)


def _strip_userinfo(url: str) -> str:
    """Drop ``user:token@`` from an http(s) URL so a credential in ``origin``
    cannot leak into the HTML report."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    if not (parts.username or parts.password):
        return url
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def _as_int(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return 0


class _GitFailed(Exception):
    """A git invocation ran but exited non-zero — not the same as git missing."""


def _git_out(cwd: Path, *args: str, keep_nul: bool = False) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except FileNotFoundError as exc:
        raise MissingDepError(
            what="git is not installed",
            why="`rig report` reads commits and diffs through git.",
            fix="install git (e.g. `xcode-select --install` or `brew install git`), then re-run.",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RigError(
            what=f"git {' '.join(args)} timed out after {_GIT_TIMEOUT_S}s",
            why="a hung git process cannot produce a report.",
            fix="retry; if it keeps hanging, pass `--base` at a closer ref.",
            exit_code=EXIT_INTERNAL,
        ) from exc
    if proc.returncode != 0:
        raise _GitFailed(
            proc.stderr.strip() or f"git {' '.join(args)} exit {proc.returncode}"
        )
    out = proc.stdout or ""
    return out if keep_nul else out.strip()
