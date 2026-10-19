"""Real-git sandbox for the removal-verdict / removal-guard tests.

A tmp BARE origin, a primary clone, linked worktrees, plus a second "integrator" clone that plays
the forge: it lands a branch on origin's default branch the way a PR merge would (fast-forward,
merge commit, squash, or rebase) WITHOUT touching the primary clone — exactly like a forge-side
merge, so the primary's ``origin/main`` is stale until it fetches. The forge itself (PR lookup)
is a plain fake list of :class:`PrRecord`; no network anywhere.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from riglib.removal_verdict import ForgeUnavailable, PrRecord


def _env() -> dict[str, str]:
    """Built per call (never at import): the autouse HOME-isolation fixture runs AFTER import, and
    a machine-global ``core.hooksPath`` (protect-main on push) must never reach a sandbox."""
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }


def git(cwd: Path, *args: str, check: bool = True) -> str:
    res = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, env=_env(), check=False
    )
    if check and res.returncode != 0:
        raise AssertionError(f"git {args} failed in {cwd}: {res.stderr}{res.stdout}")
    return res.stdout.strip()


class Sandbox:
    """origin.git (bare) + primary clone + integrator clone, default branch ``main``."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.origin = root / "origin.git"
        self.primary = root / "primary"
        self.integrator = root / "integrator"
        self.wt_dir = root / "wts"
        self.wt_dir.mkdir()
        subprocess.run(
            ["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True, env=_env()
        )
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.primary)], check=True, env=_env())
        self.commit(self.primary, "README.md", "# repo\n", "init")
        git(self.primary, "branch", "-M", "main")
        git(self.primary, "push", "-q", "-u", "origin", "main")
        git(self.primary, "remote", "set-head", "origin", "-a")
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.integrator)], check=True, env=_env())

    # ── commits ────────────────────────────────────────────────────────────────
    @staticmethod
    def commit(path: Path, name: str, content: str, msg: str) -> str:
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(content, encoding="utf-8")
        git(path, "add", "--", name)
        git(path, "commit", "-q", "-m", msg)
        return git(path, "rev-parse", "HEAD")

    def worktree(self, name: str, branch: str | None = None, base: str = "main", detach: bool = False) -> Path:
        wt = self.wt_dir / name
        if detach:
            git(self.primary, "worktree", "add", "-q", "--detach", str(wt), base)
        else:
            git(self.primary, "worktree", "add", "-q", "-b", branch or name, str(wt), base)
        return wt

    def push(self, path: Path, branch: str) -> None:
        git(path, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")

    def fetch(self) -> None:
        git(self.primary, "fetch", "-q", "origin")

    # ── forge-side landings (run in the integrator clone, push to origin/main) ──
    def _integrator_branch(self, branch: str) -> None:
        git(self.integrator, "fetch", "-q", "origin")
        git(self.integrator, "checkout", "-q", "-B", "main", "origin/main")
        git(self.integrator, "fetch", "-q", str(self.primary), f"refs/heads/{branch}:refs/heads/_incoming/{branch}")

    def land_ff(self, branch: str) -> str:
        self._integrator_branch(branch)
        git(self.integrator, "merge", "-q", "--ff-only", f"_incoming/{branch}")
        git(self.integrator, "push", "-q", "origin", "main")
        return git(self.integrator, "rev-parse", "HEAD")

    def land_merge_commit(self, branch: str) -> str:
        self._integrator_branch(branch)
        git(self.integrator, "merge", "-q", "--no-ff", "-m", f"Merge {branch}", f"_incoming/{branch}")
        git(self.integrator, "push", "-q", "origin", "main")
        return git(self.integrator, "rev-parse", "HEAD")

    def land_squash(self, branch: str) -> str:
        self._integrator_branch(branch)
        git(self.integrator, "merge", "-q", "--squash", f"_incoming/{branch}")
        git(self.integrator, "commit", "-q", "-m", f"squash {branch}")
        git(self.integrator, "push", "-q", "origin", "main")
        return git(self.integrator, "rev-parse", "HEAD")

    def land_rebase(self, branch: str) -> str:
        """Cherry-pick every branch-only commit onto main (what a rebase-merge produces)."""
        self._integrator_branch(branch)
        commits = git(
            self.integrator, "rev-list", "--reverse", "--no-merges", f"origin/main..{'_incoming/' + branch}"
        ).split()
        for sha in commits:
            git(self.integrator, "cherry-pick", "-x", sha)
        git(self.integrator, "push", "-q", "origin", "main")
        return git(self.integrator, "rev-parse", "HEAD")

    def advance_main(self, name: str = "other.txt", content: str = "x\n") -> str:
        """An unrelated commit lands on origin/main (forge-side)."""
        git(self.integrator, "fetch", "-q", "origin")
        git(self.integrator, "checkout", "-q", "-B", "main", "origin/main")
        sha = self.commit(self.integrator, name, content, f"main: {name}")
        git(self.integrator, "push", "-q", "origin", "main")
        return sha


def pr(
    branch: str,
    *,
    state: str = "MERGED",
    head_oid: str | None,
    merge_oid: str | None,
    base: str = "main",
    number: int = 1,
) -> PrRecord:
    return PrRecord(
        number=number,
        state=state,
        head_ref=branch,
        head_oid=head_oid,
        base_ref=base,
        merge_commit_oid=merge_oid,
        url=f"https://forge.example/pr/{number}",
    )


class FakeForge:
    """Stands in for the forge PR lookup; records how often it was consulted."""

    def __init__(self, records: list[PrRecord] | Exception | None = None) -> None:
        self.records = records if records is not None else []
        self.calls = 0

    def __call__(self, repo_root: Path) -> list[PrRecord]:
        self.calls += 1
        if isinstance(self.records, Exception):
            raise self.records
        return list(self.records)


def forge_down() -> FakeForge:
    return FakeForge(ForgeUnavailable("forge unreachable (test)"))


def no_processes() -> list[Path]:
    return []
