"""Ancestor ``task``-block inheritance (UB-23).

``riglib.config.load()`` merges the ``task`` block (routing keys only, never
``code_prefix``) from every eligible ancestor directory's ``rig.yaml``, so a folder
default at e.g. ``~/xp/rig.yaml`` reaches every repo nested underneath without each repo
repeating the same routing config. Precedence is ``global < ancestors (outermost first,
nearest wins) < repo``.

Two correctness/provenance requirements on top of the base mechanism:

- ``load(..., include_repo=False)`` must stay GLOBAL-ONLY — a caller that explicitly
  excludes the repo scope must not have ancestor (folder-default) task routing leak in
  either; ancestors are repo-scoped folder defaults, not global config.
- ``LoadedConfig.layers`` must list every contributing ancestor source file, in cascade
  order (global, then ancestors outermost-to-nearest, then repo).

The autouse ``_isolate_home`` fixture (tests/conftest.py) points ``$HOME`` at
``tmp_path / "isolated-home"`` and ``$XDG_CONFIG_HOME`` at
``tmp_path / "isolated-home" / ".config"``. Ancestor-walk tests build their repo trees
INSIDE that same isolated home (via the ``home`` fixture below) so
``os.path.expanduser("~")`` in the code under test matches what the test constructs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from riglib import config
from riglib.config import ConfigError


def _w(p: Path, s: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(s, encoding="utf-8")


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """The same isolated-home directory the autouse HOME-isolation fixture already set."""
    return tmp_path / "isolated-home"


def test_nearest_ancestor_wins_over_farther_ancestor(home: Path):
    _w(home / "outer" / "rig.yaml", "version: 1\ntask:\n  team: OUTER\n")
    _w(home / "outer" / "inner" / "rig.yaml", "version: 1\ntask:\n  team: INNER\n")
    repo = home / "outer" / "inner" / "repo"
    repo.mkdir(parents=True)

    loaded = config.load(repo)

    assert loaded.data["task"] == {"team": "INNER"}


def test_repo_overrides_all_ancestors(home: Path):
    _w(home / "outer" / "rig.yaml", "version: 1\ntask:\n  team: OUTER\n")
    _w(home / "outer" / "inner" / "rig.yaml", "version: 1\ntask:\n  team: INNER\n")
    repo = home / "outer" / "inner" / "repo"
    _w(repo / "rig.yaml", "version: 1\ntask:\n  team: REPO\n")

    loaded = config.load(repo)

    assert loaded.data["task"] == {"team": "REPO"}


def test_ancestor_overrides_global_per_key_not_whole_block(home: Path, monkeypatch):
    # Proves per-KEY deep-merge precedence: the ancestor sets `team` (overriding global's
    # team) but leaves `backend` unset, so global's `backend` value survives untouched.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    _w(config.global_config_path(), "version: 1\ntask:\n  team: GLOBAL\n  backend: linear\n")
    _w(home / "outer" / "rig.yaml", "version: 1\ntask:\n  team: ANCESTOR\n")
    repo = home / "outer" / "repo"
    repo.mkdir(parents=True)

    loaded = config.load(repo)

    assert loaded.data["task"]["team"] == "ANCESTOR"  # ancestor overrides global
    assert loaded.data["task"]["backend"] == "linear"  # unset by ancestor, global survives


def test_sibling_repos_under_different_ancestor_trees_are_isolated(home: Path):
    _w(home / "xp" / "rig.yaml", "version: 1\ntask:\n  team: XP-TEAM\n")
    _w(home / "work" / "rig.yaml", "version: 1\ntask:\n  team: WORK-TEAM\n")
    repo_a = home / "xp" / "repo-a"
    repo_a.mkdir(parents=True)
    repo_b = home / "work" / "repo-b"
    repo_b.mkdir(parents=True)

    loaded_a = config.load(repo_a)
    loaded_b = config.load(repo_b)

    assert loaded_a.data["task"] == {"team": "XP-TEAM"}
    assert loaded_b.data["task"] == {"team": "WORK-TEAM"}


def test_worktree_inherits_primary_repos_task_block(home: Path):
    primary = home / "proj"
    _w(primary / "rig.yaml", "version: 1\ntask:\n  team: MAIN\n")
    worktree_child = primary / ".worktrees" / "child"
    worktree_child.mkdir(parents=True)

    loaded = config.load(worktree_child)

    assert loaded.data["task"] == {"team": "MAIN"}


def test_malformed_ancestor_task_block_fails_closed_naming_the_file(home: Path):
    ancestor_path = home / "outer" / "rig.yaml"
    _w(ancestor_path, "version: 1\ntask:\n  backend: not-a-real-backend\n")
    repo = home / "outer" / "repo"
    repo.mkdir(parents=True)

    with pytest.raises(ConfigError) as excinfo:
        config.load(repo)

    assert str(ancestor_path.resolve()) in str(excinfo.value)


def test_code_prefix_is_never_inherited_from_an_ancestor(home: Path):
    _w(home / "outer" / "rig.yaml", "version: 1\ntask:\n  code_prefix: ABC\n  team: X\n")
    repo = home / "outer" / "repo"
    _w(repo / "rig.yaml", "version: 1\ntask:\n  code_prefix: REPO-OWN\n")

    loaded = config.load(repo)

    assert loaded.data["task"]["team"] == "X"
    # the repo's OWN code_prefix survives (only ancestor contributions strip it)
    assert loaded.data["task"]["code_prefix"] == "REPO-OWN"


def test_ancestor_above_home_boundary_is_not_consulted(tmp_path: Path, home: Path):
    # An rig.yaml one level ABOVE the isolated $HOME must never be walked — the ancestor
    # walk inside $HOME stops AT $HOME, never above it.
    _w(tmp_path / "rig.yaml", "version: 1\ntask:\n  team: ABOVE_HOME\n")
    repo = home / "proj"
    repo.mkdir(parents=True)

    loaded = config.load(repo)

    assert "task" not in loaded.data or loaded.data["task"] == {}


def test_outside_home_walk_reaches_a_few_levels_up(tmp_path: Path, monkeypatch):
    # Point HOME somewhere that does NOT contain the test repo tree at all, so the repo is
    # "outside home" and the walk covers every ancestor except the filesystem root.
    other_home = tmp_path / "other-home"
    other_home.mkdir()
    monkeypatch.setenv("HOME", str(other_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(other_home / ".config"))

    outer = tmp_path / "outside"
    _w(outer / "rig.yaml", "version: 1\ntask:\n  team: OUTSIDE_TEAM\n")
    repo = outer / "a" / "b" / "repo"
    repo.mkdir(parents=True)

    loaded = config.load(repo)

    assert loaded.data["task"] == {"team": "OUTSIDE_TEAM"}


def test_include_ancestors_false_disables_the_whole_mechanism(home: Path):
    _w(home / "outer" / "rig.yaml", "version: 1\ntask:\n  team: OUTER\n")
    _w(home / "outer" / "inner" / "rig.yaml", "version: 1\ntask:\n  team: INNER\n")
    repo = home / "outer" / "inner" / "repo"
    repo.mkdir(parents=True)

    loaded = config.load(repo, include_ancestors=False)

    assert "task" not in loaded.data or loaded.data["task"] == {}


def test_zero_config_repo_is_unchanged(home: Path):
    repo = home / "solo" / "repo"
    repo.mkdir(parents=True)

    loaded = config.load(repo)

    assert "task" not in loaded.data or loaded.data["task"] == {}


# ── gap: include_repo=False must stay GLOBAL-ONLY (no ancestor leak) ────────────────────


def test_include_repo_false_does_not_inherit_ancestor_task_routing(home: Path):
    # A caller that explicitly asks for the GLOBAL layer only (include_repo=False) must not
    # see a folder-default ancestor's `task` block leak in — ancestors are repo-scoped
    # folder defaults, not global config. Regression for a real bug: include_ancestors
    # defaulted to True independently of include_repo, so a GLOBAL-only load still walked
    # and merged ancestor task routing.
    _w(home / "outer" / "rig.yaml", "version: 1\ntask:\n  team: ANCESTOR\n")
    repo = home / "outer" / "repo"
    repo.mkdir(parents=True)

    loaded = config.load(repo, include_repo=False)

    assert "task" not in loaded.data or loaded.data["task"] == {}


def test_include_repo_false_still_sees_global_task_block(home: Path, monkeypatch):
    # The GLOBAL-only exclusion is specific to ancestors — the global layer itself must
    # still load normally under include_repo=False.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    _w(config.global_config_path(), "version: 1\ntask:\n  team: GLOBAL\n")
    _w(home / "outer" / "rig.yaml", "version: 1\ntask:\n  team: ANCESTOR\n")
    repo = home / "outer" / "repo"
    repo.mkdir(parents=True)

    loaded = config.load(repo, include_repo=False)

    assert loaded.data["task"] == {"team": "GLOBAL"}


def test_include_repo_true_still_inherits_ancestors_unaffected_by_the_fix(home: Path):
    # The include_repo=False fix must not regress the normal include_repo=True path.
    _w(home / "outer" / "rig.yaml", "version: 1\ntask:\n  team: ANCESTOR\n")
    repo = home / "outer" / "repo"
    repo.mkdir(parents=True)

    loaded = config.load(repo, include_repo=True)

    assert loaded.data["task"] == {"team": "ANCESTOR"}


# ── gap: LoadedConfig.layers must list every contributing ancestor file ─────────────────


def test_layers_lists_outer_and_nearest_ancestor_in_cascade_order(home: Path):
    outer_path = home / "outer" / "rig.yaml"
    inner_path = home / "outer" / "inner" / "rig.yaml"
    # distinct, non-colliding keys so BOTH ancestors genuinely contribute to the merge
    # (a contribution that becomes empty after code_prefix stripping is never a real
    # contributor and must not be listed).
    _w(outer_path, "version: 1\ntask:\n  team: OUTER\n")
    _w(inner_path, "version: 1\ntask:\n  project: INNER-PROJ\n")
    repo = home / "outer" / "inner" / "repo"
    repo.mkdir(parents=True)

    loaded = config.load(repo)

    outer_entry = f"ancestor:{outer_path.resolve()}"
    inner_entry = f"ancestor:{inner_path.resolve()}"
    assert outer_entry in loaded.layers
    assert inner_entry in loaded.layers
    assert loaded.layers.index(outer_entry) < loaded.layers.index(inner_entry)


def test_layers_ancestor_entries_sit_between_global_and_repo(home: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    _w(config.global_config_path(), "version: 1\ntask:\n  backend: linear\n")
    ancestor_path = home / "outer" / "rig.yaml"
    _w(ancestor_path, "version: 1\ntask:\n  team: ANCESTOR\n")
    repo = home / "outer" / "repo"
    _w(repo / "rig.yaml", "version: 1\ntask:\n  project: REPO-PROJ\n")

    loaded = config.load(repo)

    ancestor_entry = f"ancestor:{ancestor_path.resolve()}"
    global_idx = next(i for i, layer in enumerate(loaded.layers) if layer.startswith("global:"))
    repo_idx = next(i for i, layer in enumerate(loaded.layers) if layer.startswith("repo:"))
    ancestor_idx = loaded.layers.index(ancestor_entry)
    assert global_idx < ancestor_idx < repo_idx


def test_layers_omits_ancestors_when_include_ancestors_false(home: Path):
    ancestor_path = home / "outer" / "rig.yaml"
    _w(ancestor_path, "version: 1\ntask:\n  team: ANCESTOR\n")
    repo = home / "outer" / "repo"
    repo.mkdir(parents=True)

    loaded = config.load(repo, include_ancestors=False)

    assert not any(layer.startswith("ancestor:") for layer in loaded.layers)


def test_layers_omits_ancestors_when_include_repo_false(home: Path):
    # mirrors the ancestor-leak fix above: no ancestor should be reported as a layer either
    # when the caller excluded the repo scope entirely.
    ancestor_path = home / "outer" / "rig.yaml"
    _w(ancestor_path, "version: 1\ntask:\n  team: ANCESTOR\n")
    repo = home / "outer" / "repo"
    repo.mkdir(parents=True)

    loaded = config.load(repo, include_repo=False)

    assert not any(layer.startswith("ancestor:") for layer in loaded.layers)


# ── narrow direct unit coverage for the ancestor-dir resolver ───────────────────────────


def test_ancestor_dirs_inside_home_stop_at_home(home: Path):
    from riglib.config import _task_inheritance_ancestor_dirs

    repo = home / "outer" / "inner" / "repo"
    repo.mkdir(parents=True)

    dirs = _task_inheritance_ancestor_dirs(repo)

    assert dirs == [home.resolve(), (home / "outer").resolve(), (home / "outer" / "inner").resolve()]


def test_ancestor_dirs_repo_root_itself_excluded(home: Path):
    from riglib.config import _task_inheritance_ancestor_dirs

    repo = home / "proj"
    repo.mkdir(parents=True)

    dirs = _task_inheritance_ancestor_dirs(repo)

    assert repo.resolve() not in dirs


def test_ancestor_dirs_outside_home_excludes_filesystem_root(tmp_path: Path, monkeypatch):
    from riglib.config import _task_inheritance_ancestor_dirs

    other_home = tmp_path / "other-home"
    other_home.mkdir()
    monkeypatch.setenv("HOME", str(other_home))

    repo = tmp_path / "outside" / "a" / "b" / "repo"
    repo.mkdir(parents=True)

    dirs = _task_inheritance_ancestor_dirs(repo)

    assert Path("/") not in dirs
    assert (tmp_path / "outside" / "a" / "b").resolve() in dirs
    assert (tmp_path / "outside").resolve() in dirs
