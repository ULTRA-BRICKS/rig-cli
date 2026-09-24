"""Hermetic tests for the ``git:`` area — global git settings via ``git config --global``.

Every test that could reach a real ``git`` pins ``GIT_CONFIG_GLOBAL`` to a tmp file and sets
``GIT_CONFIG_NOSYSTEM`` (the conftest autouse fixture already isolates HOME/USERPROFILE/XDG), so
nothing here can touch the developer's real ``~/.gitconfig``. The seam tests monkeypatch
``runner._git_global_get`` / ``runner._set_git_global`` — ``gitconfig.resolve_key`` imports the
getter lazily, so one patch covers plan/apply/drift/verify.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from riglib import gitconfig
from riglib.actions import runner
from riglib.actions.runner import _do_provision_git_config
from riglib.config import ConfigError, LoadedConfig, load, validate
from riglib.plan import Action, InstallPlan, _build_git


# The genuine seams, captured at import — before the conftest autouse guard swaps them for an
# in-memory store for every test. Only the real-git tests below put them back, and only after
# pointing GIT_CONFIG_GLOBAL at a tmp file.
_REAL_GET = runner._git_global_get
_REAL_SET = runner._set_git_global


@pytest.fixture
def git_global(tmp_path, monkeypatch) -> Path:
    """A throwaway global gitconfig + the REAL git seams (never the developer's ~/.gitconfig)."""
    path = tmp_path / "sandbox.gitconfig"
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(path))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr(runner, "_git_global_get", _REAL_GET)
    monkeypatch.setattr(runner, "_set_git_global", _REAL_SET)
    return path


@pytest.fixture
def fake_git(monkeypatch):
    """An in-memory global gitconfig behind the two runner seams."""
    store: dict[str, str] = {}
    calls: list[tuple[str, str]] = []

    def _get(key: str):
        return store.get(gitconfig.canonical_key(key))

    def _set(key: str, value: str) -> int:
        calls.append((key, value))
        store[gitconfig.canonical_key(key)] = value
        return 0

    monkeypatch.setattr(runner, "_git_global_get", _get)
    monkeypatch.setattr(runner, "_set_git_global", _set)
    return store, calls


def _action(entries: dict) -> Action:
    return Action(
        kind="provision_git_config",
        category="git",
        item="config",
        source=Path("."),
        target=Path("gitconfig"),
        options={"config": gitconfig.desired_config({"config": entries})},
    )


# ── key shape ───────────────────────────────────────────────────────────────────────────
_KEY_CORPUS = {
    "user.name": True,
    "pull.rebase": True,
    "init.defaultBranch": True,
    "url.https://example.com/.insteadOf": True,
    "branch.feat/x.remote": True,
    "diff.tool-x.cmd": True,
    "a...b": True,  # subsection "." — odd but git-legal, and both checks agree
    "user": False,
    ".name": False,
    "user.": False,
    "a..b": False,  # empty subsection
    "1user.name": False,
    "user.1name": False,
    "user_x.name": False,
    "user.na_me": False,
    "user.name\n": False,
    "url.a\nb.insteadOf": False,
    "": False,
}


@pytest.mark.parametrize("key,ok", sorted(_KEY_CORPUS.items()))
def test_parse_key_and_json_pattern_agree(key, ok):
    assert (gitconfig.parse_key(key) is not None) is ok
    # the published JSON-schema propertyNames pattern must accept exactly the same keys
    assert (re.fullmatch(gitconfig.GIT_CONFIG_KEY_JSON_PATTERN, key) is not None) is ok


def test_canonical_key_lowercases_section_and_name_only():
    assert gitconfig.canonical_key("User.Name") == "user.name"
    assert gitconfig.canonical_key("URL.https://X/.insteadOf") == "url.https://X/.insteadof"


def test_values_match_uses_git_semantics():
    assert gitconfig.values_match("yes", "true", gitconfig.TYPE_BOOL)
    assert gitconfig.values_match("On", "true", gitconfig.TYPE_BOOL)
    assert gitconfig.values_match("0", "false", gitconfig.TYPE_BOOL)
    assert not gitconfig.values_match("merges", "true", gitconfig.TYPE_BOOL)
    assert gitconfig.values_match("1k", "1024", gitconfig.TYPE_INT)
    assert not gitconfig.values_match("main ", "main", gitconfig.TYPE_STRING)
    assert gitconfig.values_match("Alex Ultra", "Alex Ultra", gitconfig.TYPE_STRING)


# ── validation ──────────────────────────────────────────────────────────────────────────
def test_validate_accepts_the_documented_shape():
    validate({"version": 1, "git": {"enabled": True, "config": {
        "user.name": "Alex Ultra", "user.email": "a@example.com",
        "pull.rebase": True, "init.defaultBranch": "main", "core.compression": 9,
    }}})
    validate({"version": 1, "git": {}})
    validate({"version": 1, "git": {"config": {}}})


@pytest.mark.parametrize(
    "block,match",
    [
        ("oops", "git must be a mapping"),
        ({"bogus": 1}, "unknown git key"),
        ({"enabled": "yes"}, "git.enabled must be a bool"),
        ({"config": ["user.name"]}, "git.config must be a mapping"),
        ({"config": {"username": "x"}}, "not a valid git config key"),
        ({"config": {"user": {"name": "x"}}}, "nested mapping"),  # what `config set git.config.user.name` builds
        ({"config": {"user.name": {"first": "x"}}}, "nested mapping"),
        ({"config": {"user.name": "a\nb"}}, "single line"),
        ({"config": {"remote.o.fetch": ["a", "b"]}}, "multi-valued"),
        ({"config": {"core.compression": 1.5}}, "string, bool or int"),
        ({"config": {"user.name": None}}, "string, bool or int"),
        ({"config": {"core.hooksPath": "/x"}}, "managed by another rig block"),
        ({"config": {"Core.ExcludesFile": "/x"}}, "managed by another rig block"),
        ({"config": {"user.name": "a", "User.Name": "b"}}, "same key"),
    ],
)
def test_validate_rejects(block, match):
    with pytest.raises(ConfigError, match=match):
        validate({"version": 1, "git": block})


def test_validate_rejects_bad_key_with_schema_path():
    with pytest.raises(ConfigError) as exc:
        validate({"version": 1, "git": {"config": {"nodot": "x"}}})
    assert exc.value.schema_path == "git.config"
    assert "user.name" in exc.value.fix


# ── global-layer-only enforcement (security: a repo must not rewrite ~/.gitconfig) ─────────
def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize("block", ["git: {config: {core.sshCommand: evil}}", "profile: {repo: https://x/y}"])
def test_loader_rejects_global_only_blocks_from_repo_layer(tmp_path, block):
    repo = tmp_path / "repo"
    _write(repo / "rig.yaml", f"version: 1\n{block}\n")
    with pytest.raises(ConfigError, match="global-only config block"):
        load(repo)


def test_loader_rejects_git_block_from_explicit_config_layer(tmp_path):
    other = tmp_path / "other.yaml"
    _write(other, "version: 1\ngit: {config: {user.name: X}}\n")
    with pytest.raises(ConfigError, match="global-only"):
        load(tmp_path, explicit_config=other)


def test_loader_treats_the_global_file_passed_explicitly_as_global(tmp_path):
    """`config set --global` validates the global file as `explicit_config`; it is still GLOBAL."""
    from riglib.config import global_config_path

    gpath = global_config_path()
    _write(gpath, "version: 1\ngit: {config: {user.name: X}}\nprofile: {repo: https://h/u/.rig-profile}\n")
    loaded = load(gpath.parent, explicit_config=gpath, include_global=False)
    assert loaded.data["git"]["config"]["user.name"] == "X"


def test_loader_accepts_git_and_profile_from_global_layer(tmp_path):
    from riglib.config import global_config_path

    _write(
        global_config_path(),
        "version: 1\ngit: {config: {user.name: X}}\nprofile: {repo: https://h/u/.rig-profile}\n",
    )
    loaded = load(tmp_path / "nowhere", include_repo=False)
    assert loaded.data["git"]["config"]["user.name"] == "X"
    assert loaded.data["profile"]["repo"] == "https://h/u/.rig-profile"


# ── plan ────────────────────────────────────────────────────────────────────────────────
def test_plan_emits_one_action_only_when_keys_declared(tmp_path):
    def _plan(data):
        plan = InstallPlan()
        _build_git(LoadedConfig(data=data, repo_root=tmp_path), plan)
        return plan.actions

    assert _plan({}) == []
    assert _plan({"git": {}}) == []
    assert _plan({"git": {"config": {}}}) == []
    assert _plan({"git": {"enabled": False, "config": {"user.name": "x"}}}) == []
    acts = _plan({"git": {"config": {"user.name": "X", "pull.rebase": True, "core.compression": 9}}})
    assert len(acts) == 1
    act = acts[0]
    assert (act.kind, act.category, act.item) == ("provision_git_config", "git", "config")
    assert act.options["config"] == {
        "core.compression": {"value": "9", "type": "int"},
        "pull.rebase": {"value": "true", "type": "bool"},
        "user.name": {"value": "X", "type": "string"},
    }


def test_plan_target_honors_git_config_global(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "g.cfg"))
    assert gitconfig.global_gitconfig_path() == tmp_path / "g.cfg"


# ── apply / drift / verify through the seams ────────────────────────────────────────────
def test_apply_sets_missing_then_is_idempotent(fake_git):
    store, calls = fake_git
    act = _action({"user.name": "Alex Ultra", "pull.rebase": True})
    first = _do_provision_git_config(act, "backup")
    assert first.status == "created"
    assert store == {"user.name": "Alex Ultra", "pull.rebase": "true"}
    second = _do_provision_git_config(act, "backup")
    assert second.status == "skipped"
    assert "2 already current" in second.detail
    assert len(calls) == 2


def test_apply_overwrites_differing_value_and_records_restore(fake_git):
    store, _calls = fake_git
    store["user.email"] = "old@example.com"
    res = _do_provision_git_config(_action({"user.email": "new@example.com"}), "backup")
    assert res.status == "updated"
    assert store["user.email"] == "new@example.com"
    assert "prior user.email='old@example.com'" in res.detail
    assert "git config --global user.email old@example.com" in res.detail


def test_apply_on_conflict_skip_leaves_differing_value(fake_git):
    store, calls = fake_git
    store["user.email"] = "old@example.com"
    res = _do_provision_git_config(_action({"user.email": "new@example.com", "user.name": "N"}), "skip")
    assert store["user.email"] == "old@example.com"
    assert store["user.name"] == "N"  # a MISSING key is still set under skip (nothing replaced)
    assert "on_conflict=skip" in res.detail
    assert calls == [("user.name", "N")]


def test_apply_treats_hand_written_yes_as_current(fake_git):
    store, calls = fake_git
    store["pull.rebase"] = "yes"
    res = _do_provision_git_config(_action({"pull.rebase": True}), "backup")
    assert res.status == "skipped"
    assert calls == []


def test_apply_reports_error_when_git_refuses(monkeypatch):
    monkeypatch.setattr(runner, "_git_global_get", lambda key: None)
    monkeypatch.setattr(runner, "_set_git_global", lambda key, value: 5)
    res = _do_provision_git_config(_action({"remote.origin.fetch": "x"}), "backup")
    assert res.status == "error"
    assert "remote.origin.fetch" in res.detail and "multi-valued" in res.detail


def test_drift_reports_one_item_per_key(fake_git):
    from riglib.drift import DriftReport, _check_git_config

    store, _calls = fake_git
    store["user.name"] = "Alex Ultra"
    store["user.email"] = "old@example.com"
    report = DriftReport()
    _check_git_config(
        _action({"user.name": "Alex Ultra", "user.email": "new@example.com", "init.defaultBranch": "main"}),
        report,
    )
    by_item = {d.item: d for d in report.items}
    assert set(by_item) == {"user.email", "init.defaultBranch"}
    assert by_item["user.email"].direction == "modified"
    assert by_item["init.defaultBranch"].direction == "missing"
    assert all(d.category == "git" for d in report.items)


def test_verify_passes_only_when_every_key_reads_back(fake_git):
    from riglib.verify import _verify_git_config

    store, _calls = fake_git
    act = _action({"user.name": "A", "pull.rebase": False})
    assert [r.passed for r in _verify_git_config(act)] == [False, False]
    store["user.name"] = "A"
    store["pull.rebase"] = "false"
    assert all(r.passed for r in _verify_git_config(act))


# ── the real git binary, against a sandboxed GIT_CONFIG_GLOBAL ─────────────────────────────
pytestmark_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git_get(key: str) -> str:
    return subprocess.run(
        ["git", "config", "--global", "--get", key], capture_output=True, text=True, check=False
    ).stdout.strip()


@pytestmark_git
def test_real_git_apply_is_idempotent_and_never_unsets(git_global):
    subprocess.run(["git", "config", "--global", "alias.st", "status"], check=True)
    subprocess.run(["git", "config", "--global", "pull.rebase", "yes"], check=True)
    act = _action({
        "User.Name": "Alex Ultra",
        "pull.rebase": True,
        "url.https://example.com/.insteadOf": "ex:",
    })
    first = _do_provision_git_config(act, "backup")
    assert first.status == "created", first.detail
    assert _git_get("user.name") == "Alex Ultra"
    assert _git_get("url.https://example.com/.insteadof") == "ex:"
    assert _git_get("pull.rebase") == "yes"  # already meant true — left as the user wrote it
    assert _git_get("alias.st") == "status"  # a hand-set extra is never touched
    second = _do_provision_git_config(act, "backup")
    assert second.status == "skipped", second.detail
    # the file rig wrote is the sandboxed one
    assert "Alex Ultra" in git_global.read_text(encoding="utf-8")


@pytestmark_git
def test_real_git_drift_then_apply_converges(git_global):
    from riglib.drift import DriftReport, _check_git_config

    subprocess.run(["git", "config", "--global", "init.defaultBranch", "master"], check=True)
    act = _action({"init.defaultBranch": "main"})
    report = DriftReport()
    _check_git_config(act, report)
    assert [(d.direction, d.item) for d in report.items] == [("modified", "init.defaultBranch")]
    assert _do_provision_git_config(act, "backup").status == "updated"
    report = DriftReport()
    _check_git_config(act, report)
    assert report.items == []


@pytestmark_git
def test_cli_apply_commit_only_git_sets_keys(tmp_path, monkeypatch, fake_agent_tools, git_global, capsys):
    from riglib.cli import main
    from riglib.config import global_config_path

    _write(
        global_config_path(),
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "git:\n  config:\n    user.name: Alex Ultra\n    pull.rebase: true\n",
    )
    workdir = tmp_path / "nongit"
    workdir.mkdir()
    rc = main(["apply", "commit", "--only", "git", "-C", str(workdir)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert _git_get("user.name") == "Alex Ultra"
    assert _git_get("pull.rebase") == "true"
    rc = main(["status", "-C", str(workdir)])
    out = capsys.readouterr().out
    assert "global git settings" in out


def test_git_is_wired_into_registries():
    from riglib.action_tags import ACTION_TAGS
    from riglib.actions.runner import _HANDLERS
    from riglib.areas import AREAS
    from riglib.config import _VALID_TOP_KEYS
    from riglib.config_schema import TOP_LEVEL_KEYS
    from riglib.layers import GLOBAL, layer_for_category
    from riglib.schema import writable_layer_for_category
    from riglib.verify import _VERIFIERS

    for key in ("git", "profile"):
        assert key in _VALID_TOP_KEYS and key in TOP_LEVEL_KEYS
        assert writable_layer_for_category(key) == GLOBAL
    assert "provision_git_config" in _HANDLERS
    assert "provision_git_config" in _VERIFIERS
    assert "provision_git_config" in ACTION_TAGS
    assert any(a.key == "git" and a.layer == GLOBAL for a in AREAS)
    assert layer_for_category("git") == GLOBAL
