"""``rig profile`` — two-way sync of the GLOBAL config through a git repo.

Hermetic: the "remote" is a local bare repo under ``tmp_path``; two "machines" are two
``$XDG_CONFIG_HOME`` dirs (the global config, the profile checkout and its state file all derive
from it). ``GIT_CONFIG_GLOBAL`` points at a tmp file carrying a test identity and
``GIT_CONFIG_NOSYSTEM`` shuts out the host's system gitconfig (Git for Windows' autocrlf).
The forge probes (``repo_visibility``, ``fj_login``) are stubbed — no network, ever.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from riglib import profile
from riglib.cli import main
from riglib.config import global_config_path

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

BASE_CONFIG = "version: 1\ngit:\n  config:\n    pull.rebase: true\n"


def _git(*args: str, cwd: Path | None = None) -> str:
    cmd = ["git"] + (["-C", str(cwd)] if cwd else []) + list(args)
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()


class World:
    """A bare remote + helpers to act as machine A or B."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp = tmp_path
        self.mp = monkeypatch
        self.remote = tmp_path / "remote.git"
        _git("init", "--quiet", "--bare", "--initial-branch=main", str(self.remote))
        self.url = str(self.remote)
        self.messages: list[tuple[str, str]] = []

    def machine(self, name: str) -> Path:
        cfg_home = self.tmp / name / ".config"
        cfg_home.mkdir(parents=True, exist_ok=True)
        self.mp.setenv("XDG_CONFIG_HOME", str(cfg_home))
        return global_config_path()

    def notify(self, level: str, text: str) -> None:
        self.messages.append((level, text))

    def warns(self) -> str:
        return "\n".join(t for lvl, t in self.messages if lvl == "warn")

    def remote_file(self) -> str | None:
        res = subprocess.run(
            ["git", "-C", str(self.remote), "show", "main:config.yaml"], capture_output=True, text=True
        )
        return res.stdout if res.returncode == 0 else None

    def remote_commits(self) -> int:
        res = subprocess.run(["git", "-C", str(self.remote), "rev-list", "--count", "main"],
                             capture_output=True, text=True)
        return int(res.stdout.strip()) if res.returncode == 0 else 0

    def push_remote_edit(self, text: str) -> None:
        """Another machine (a plain clone) changes config.yaml on the remote."""
        other = self.tmp / "other-clone"
        if other.exists():
            _git("pull", "--quiet", cwd=other)
        else:
            _git("clone", "--quiet", self.url, str(other))
        (other / "config.yaml").write_text(text, encoding="utf-8", newline="\n")
        _git("add", "config.yaml", cwd=other)
        _git("commit", "--quiet", "-m", "edit elsewhere", cwd=other)
        _git("push", "--quiet", "origin", "HEAD:main", cwd=other)


@pytest.fixture
def world(tmp_path, monkeypatch) -> World:
    gitcfg = tmp_path / "test.gitconfig"
    gitcfg.write_text("[user]\n\tname = Test User\n\temail = test@example.com\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitcfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(profile, "repo_visibility", lambda url: None)
    monkeypatch.setattr(profile, "fj_login", lambda host=None: None)
    return World(tmp_path, monkeypatch)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ── init ─────────────────────────────────────────────────────────────────────────────────────
def test_init_seeds_an_empty_remote_and_records_the_repo(world):
    local = world.machine("a")
    _write(local, BASE_CONFIG)
    out = profile.init(world.url, notify=world.notify)
    assert out.result == "pushed", out.detail
    assert out.code == 0
    doc = _load(local)
    assert doc["profile"] == {"repo": world.url, "auto_sync": False}
    assert doc["git"]["config"]["pull.rebase"] is True
    assert yaml.safe_load(world.remote_file()) == doc
    # the snapshot lives BESIDE the checkout, never inside the repo
    assert profile.state_path().is_file()
    assert profile.state_path().parent == profile.checkout_dir().parent
    assert _git("ls-files", cwd=profile.checkout_dir()).splitlines() == ["config.yaml"]
    assert profile.read_state()["synced_sha256"] == profile.digest(local.read_bytes())


def test_init_second_machine_adopts_the_remote(world):
    a = world.machine("a")
    _write(a, BASE_CONFIG)
    profile.init(world.url, notify=world.notify)
    b = world.machine("b")
    assert not b.exists()
    out = profile.init(world.url, notify=world.notify)
    assert out.result == "pulled", out.detail
    assert _load(b) == _load(a)
    assert profile.sync(notify=world.notify).result == "in_sync"


def test_init_without_url_requires_one_when_fj_is_unknown(world):
    world.machine("a")
    with pytest.raises(profile.ProfileError) as exc:
        profile.init(None, notify=world.notify)
    assert exc.value.code == profile.EXIT_USAGE
    assert "fj repo create .rig-profile --private" in exc.value.fix


def test_init_uses_profile_repo_from_config_when_no_url(world):
    local = world.machine("a")
    _write(local, BASE_CONFIG + f"profile:\n  repo: {world.url}\n")
    assert profile.init(None, notify=world.notify).result == "pushed"


def test_init_with_a_differing_local_config_is_a_conflict_and_touches_nothing(world):
    a = world.machine("a")
    _write(a, BASE_CONFIG)
    profile.init(world.url, notify=world.notify)
    b = world.machine("b")
    _write(b, "version: 1\nbun: {enabled: true}\n")
    before = b.read_bytes()
    out = profile.init(world.url, notify=world.notify)
    assert out.result == "conflict"
    assert out.code == profile.EXIT_CONFLICT
    assert b.read_bytes() == before
    text = "\n".join(t for _l, t in out.messages)
    assert str(b) in text and "--take local" in text and "--take remote" in text


def test_init_refuses_a_checkout_of_another_repo(world, tmp_path):
    local = world.machine("a")
    _write(local, BASE_CONFIG)
    profile.init(world.url, notify=world.notify)
    other = tmp_path / "other.git"
    _git("init", "--quiet", "--bare", str(other))
    with pytest.raises(profile.ProfileError, match="already tracks"):
        profile.init(str(other), notify=world.notify)


def test_init_records_auto_sync_when_asked(world):
    local = world.machine("a")
    _write(local, BASE_CONFIG)
    profile.init(world.url, auto_sync=True, notify=world.notify)
    assert _load(local)["profile"]["auto_sync"] is True
    assert profile.auto_sync_enabled()


def test_clone_failure_points_at_creating_the_repo(world, tmp_path):
    world.machine("a")
    with pytest.raises(profile.ProfileError) as exc:
        profile.init(str(tmp_path / "does-not-exist.git"), notify=world.notify)
    assert "--private" in exc.value.fix


# ── sync: the three-way table ────────────────────────────────────────────────────────────────
def _two_machines(world) -> tuple[Path, Path]:
    a = world.machine("a")
    _write(a, BASE_CONFIG)
    profile.init(world.url, notify=world.notify)
    b = world.machine("b")
    profile.init(world.url, notify=world.notify)
    return a, b


def test_local_change_pushes_and_the_other_machine_pulls_with_a_backup(world):
    a, b = _two_machines(world)
    world.machine("a")
    doc = _load(a)
    doc["git"]["config"]["init.defaultBranch"] = "main"
    a.write_text(yaml.safe_dump(doc), encoding="utf-8")
    assert profile.sync(notify=world.notify).result == "pushed"

    world.machine("b")
    old_b = b.read_text(encoding="utf-8")
    out = profile.sync(notify=world.notify)
    assert out.result == "pulled", out.detail
    assert _load(b) == doc
    assert out.backup is not None and out.backup.name.startswith("config.yaml.rig-bak-")
    assert out.backup.parent == b.parent
    assert out.backup.read_text(encoding="utf-8") == old_b


def test_noop_sync_makes_no_commit(world):
    world.machine("a")
    _write(global_config_path(), BASE_CONFIG)
    profile.init(world.url, notify=world.notify)
    commits = world.remote_commits()
    assert profile.sync(notify=world.notify).result == "in_sync"
    assert profile.sync(notify=world.notify).result == "in_sync"
    assert world.remote_commits() == commits


def test_crlf_local_file_is_not_a_change(world):
    local = world.machine("a")
    _write(local, BASE_CONFIG)
    profile.init(world.url, notify=world.notify)
    local.write_bytes(local.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert profile.sync(notify=world.notify).result == "in_sync"


def test_both_changed_is_refused_then_take_remote_resolves(world):
    a, b = _two_machines(world)
    world.machine("a")
    a.write_text(a.read_text(encoding="utf-8") + "bun: {enabled: true}\n", encoding="utf-8")
    assert profile.sync(notify=world.notify).result == "pushed"
    world.machine("b")
    b.write_text(b.read_text(encoding="utf-8") + "spotlight: {enabled: false}\n", encoding="utf-8")
    mine = b.read_bytes()
    out = profile.sync(notify=world.notify)
    assert out.result == "conflict" and out.code == 3
    assert b.read_bytes() == mine  # nothing written
    resolved = profile.sync(take="remote", notify=world.notify)
    assert resolved.result == "pulled"
    assert resolved.backup is not None and resolved.backup.read_bytes() == mine
    assert _load(b) == _load(a)
    assert profile.sync(notify=world.notify).result == "in_sync"


def test_take_local_overwrites_the_remote(world):
    a, b = _two_machines(world)
    world.push_remote_edit("version: 1\nbun: {enabled: true}\n")
    world.machine("b")
    b.write_text(b.read_text(encoding="utf-8") + "spotlight: {enabled: false}\n", encoding="utf-8")
    assert profile.sync(notify=world.notify).result == "conflict"
    assert profile.sync(take="local", notify=world.notify).result == "pushed"
    assert yaml.safe_load(world.remote_file()) == _load(b)


def test_remote_only_change_pulls(world):
    _a, b = _two_machines(world)
    world.push_remote_edit("version: 1\nbun: {enabled: true}\n")
    world.machine("b")
    assert profile.sync(notify=world.notify).result == "pulled"
    assert _load(b) == {"version": 1, "bun": {"enabled": True}}


def test_invalid_remote_config_is_never_adopted(world):
    _a, b = _two_machines(world)
    world.push_remote_edit("version: 1\nnot_a_rig_key: true\n")
    world.machine("b")
    before = b.read_bytes()
    with pytest.raises(profile.ProfileError, match="does not validate"):
        profile.sync(notify=world.notify)
    assert b.read_bytes() == before


def test_invalid_local_config_is_never_pushed(world):
    local = world.machine("a")
    _write(local, BASE_CONFIG)
    profile.init(world.url, notify=world.notify)
    commits = world.remote_commits()
    local.write_text("version: 1\ngit: {config: {nodot: x}}\n", encoding="utf-8")
    with pytest.raises(profile.ProfileError, match="does not validate"):
        profile.sync(notify=world.notify)
    assert world.remote_commits() == commits


def test_sync_before_init_is_a_usage_error(world):
    world.machine("a")
    with pytest.raises(profile.ProfileError) as exc:
        profile.sync(notify=world.notify)
    assert exc.value.code == profile.EXIT_USAGE
    assert "rig profile init" in exc.value.fix


# ── identity, secrets, visibility, push failure ─────────────────────────────────────────────
def test_no_git_identity_fails_naming_the_git_block(world, tmp_path, monkeypatch):
    empty = tmp_path / "empty.gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    local = world.machine("a")
    _write(local, "version: 1\n")
    with pytest.raises(profile.ProfileError) as exc:
        profile.init(world.url, notify=world.notify)
    assert exc.value.code == profile.EXIT_USAGE
    assert "git.config.user.name" in exc.value.fix
    assert world.remote_commits() == 0
    # rolled back: the checkout is still unborn and clean
    assert subprocess.run(["git", "-C", str(profile.checkout_dir()), "rev-parse", "-q", "--verify", "HEAD"],
                          capture_output=True).returncode != 0


def test_identity_falls_back_to_the_declared_git_block(world, tmp_path, monkeypatch):
    empty = tmp_path / "empty.gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    local = world.machine("a")
    _write(local, "version: 1\ngit:\n  config:\n    user.name: Alex Ultra\n    user.email: a@example.com\n")
    assert profile.init(world.url, notify=world.notify).result == "pushed"
    author = _git("log", "-1", "--format=%an <%ae>", "main", cwd=world.remote)
    assert author == "Alex Ultra <a@example.com>"


def test_env_vars_warn_loudly_on_first_push_then_remind(world):
    local = world.machine("a")
    _write(local, BASE_CONFIG + "env:\n  vars:\n    BRAVE_API_KEY: sk-not-a-real-key\n")
    profile.init(world.url, notify=world.notify)
    first = world.warns()
    assert "BRAVE_API_KEY" in first and "PLAINTEXT" in first and "MUST be private" in first
    assert "sk-not-a-real-key" not in first
    world.messages.clear()
    local.write_text(local.read_text(encoding="utf-8") + "bun: {enabled: true}\n", encoding="utf-8")
    assert profile.sync(notify=world.notify).result == "pushed"
    again = world.warns()
    assert "BRAVE_API_KEY" in again and "PLAINTEXT" not in again


def test_push_to_a_public_repo_is_refused(world, monkeypatch):
    monkeypatch.setattr(profile, "repo_visibility", lambda url: "public")
    local = world.machine("a")
    _write(local, BASE_CONFIG)
    with pytest.raises(profile.ProfileError, match="PUBLIC"):
        profile.init(world.url, notify=world.notify)
    assert world.remote_commits() == 0


def test_rejected_push_rolls_back_and_keeps_the_snapshot(world):
    local = world.machine("a")
    _write(local, BASE_CONFIG)
    profile.init(world.url, notify=world.notify)
    snapshot = profile.read_state()["synced_sha256"]
    hook = world.remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'rejected by test' >&2\nexit 1\n", encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    local.write_text(local.read_text(encoding="utf-8") + "bun: {enabled: true}\n", encoding="utf-8")
    with pytest.raises(profile.ProfileError, match="push to"):
        profile.sync(notify=world.notify)
    repo = profile.checkout_dir()
    assert _git("rev-parse", "HEAD", cwd=repo) == _git("rev-parse", "origin/main", cwd=repo)
    assert profile.read_state()["synced_sha256"] == snapshot
    hook.unlink()
    assert profile.sync(notify=world.notify).result == "pushed"


# ── status ────────────────────────────────────────────────────────────────────────────────────
def test_status_names_the_changed_side_without_writing(world):
    _a, b = _two_machines(world)
    world.machine("b")
    st = profile.status()
    assert (st.local, st.remote, st.verdict) == ("unchanged", "unchanged", "in_sync")
    b.write_text(b.read_text(encoding="utf-8") + "bun: {enabled: true}\n", encoding="utf-8")
    assert profile.status().verdict == "push"
    world.push_remote_edit("version: 1\n")
    world.machine("b")
    assert profile.status().remote == "unchanged"  # not fetched yet
    st = profile.status(fetch=True)
    assert (st.local, st.remote, st.verdict) == ("changed", "changed", "conflict")
    assert b.read_text(encoding="utf-8").endswith("bun: {enabled: true}\n")


# ── pure helpers ──────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "local,remote,snap,want",
    [
        ("x", "x", None, "in_sync"),
        ("x", None, None, "push"),
        (None, "x", "y", "pull"),
        ("l", "s", "s", "push"),
        ("s", "r", "s", "pull"),
        ("l", "r", "s", "conflict"),
        ("l", "r", None, "conflict"),
    ],
)
def test_classify(local, remote, snap, want):
    assert profile.classify(local, remote, snap) == want


def test_parse_whoami_is_fail_closed():
    assert profile.parse_whoami("currently signed in to alex@git.hyperide.ai\n") == ("alex", "git.hyperide.ai")
    assert profile.parse_whoami("alex\n") == ("alex", None)
    assert profile.parse_whoami("not logged in") is None
    assert profile.parse_whoami("") is None


def test_default_url_from_fj_login(monkeypatch):
    monkeypatch.delenv("FJ_FALLBACK_HOST", raising=False)
    monkeypatch.setattr(profile, "fj_login", lambda host=None: ("alex", None))
    assert profile.default_url() == "https://git.hyperide.ai/alex/.rig-profile"
    monkeypatch.setenv("FJ_FALLBACK_HOST", "https://forge.example/")
    assert profile.default_url() == "https://forge.example/alex/.rig-profile"
    monkeypatch.setattr(profile, "fj_login", lambda host=None: ("alex", "git.other"))
    assert profile.default_url() == "https://git.other/alex/.rig-profile"
    assert profile.default_url(host="h.example") == "https://h.example/alex/.rig-profile"
    monkeypatch.setattr(profile, "fj_login", lambda host=None: None)
    assert profile.default_url() is None


@pytest.mark.parametrize(
    "url,parsed",
    [
        ("https://git.hyperide.ai/alex/.rig-profile", ("git.hyperide.ai", "alex", ".rig-profile")),
        ("https://github.com/alex/.rig-profile.git", ("github.com", "alex", ".rig-profile")),
        ("git@github.com:alex/.rig-profile.git", ("github.com", "alex", ".rig-profile")),
        ("ssh://git@git.hyperide.ai:2222/alex/.rig-profile.git", ("git.hyperide.ai", "alex", ".rig-profile")),
        ("C:\\tmp\\remote.git", None),
        ("/tmp/remote.git", None),
    ],
)
def test_parse_forge_url(url, parsed):
    assert profile._parse_forge_url(url) == parsed


def test_repo_visibility_reads_the_anonymous_api(monkeypatch):
    import io
    import urllib.error
    import urllib.request

    seen: list[str] = []

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _open(body=None, error=None):
        def _urlopen(req, timeout=None):
            seen.append(req.full_url)
            if error is not None:
                raise error
            return _Resp(body)
        return _urlopen

    monkeypatch.setattr(urllib.request, "urlopen", _open(b'{"private": false}'))
    assert profile.repo_visibility("https://git.hyperide.ai/alex/.rig-profile") == "public"
    assert seen[-1] == "https://git.hyperide.ai/api/v1/repos/alex/.rig-profile"
    monkeypatch.setattr(urllib.request, "urlopen", _open(b'{"private": false}'))
    assert profile.repo_visibility("git@github.com:alex/.rig-profile.git") == "public"
    assert seen[-1] == "https://api.github.com/repos/alex/.rig-profile"
    err404 = urllib.error.HTTPError("u", 404, "nf", {}, None)
    monkeypatch.setattr(urllib.request, "urlopen", _open(error=err404))
    assert profile.repo_visibility("https://git.hyperide.ai/alex/.rig-profile") == "private"
    monkeypatch.setattr(urllib.request, "urlopen", _open(error=urllib.error.URLError("offline")))
    assert profile.repo_visibility("https://git.hyperide.ai/alex/.rig-profile") is None
    before = len(seen)
    assert profile.repo_visibility("C:\\tmp\\remote.git") is None
    assert len(seen) == before  # a local path never reaches the network


def test_record_profile_block_never_rewrites_an_existing_repo(world):
    local = world.machine("a")
    _write(local, "version: 1\nprofile:\n  repo: git@h:u/.rig-profile.git\n  auto_sync: true\n")
    assert profile.record_profile_block("https://h/u/.rig-profile", None, world.notify) is False
    assert _load(local)["profile"] == {"repo": "git@h:u/.rig-profile.git", "auto_sync": True}
    assert any("left as is" in t for _l, t in world.messages)


# ── CLI surface + `rig apply commit` auto_sync ─────────────────────────────────────────────────
def test_cli_profile_usage_and_uninitialized_status(world, capsys):
    world.machine("a")
    assert main(["profile"]) == 2
    assert main(["profile", "status"]) == 2
    assert "rig profile init" in capsys.readouterr().err


def test_cli_init_sync_status_round_trip(world, capsys):
    local = world.machine("a")
    _write(local, BASE_CONFIG)
    assert main(["profile", "init", world.url]) == 0
    assert "pushed" in capsys.readouterr().out
    assert main(["profile", "sync"]) == 0
    assert "in sync" in capsys.readouterr().out
    assert main(["profile", "status"]) == 0
    local.write_text(local.read_text(encoding="utf-8") + "bun: {enabled: true}\n", encoding="utf-8")
    assert main(["profile", "status"]) == 3
    assert "local changed" in capsys.readouterr().out


def _auto_sync_machine(world, fake_agent_tools) -> Path:
    local = world.machine("a")
    _write(local, f"version: 1\nagent_tools_source: {fake_agent_tools.as_posix()}\n" + BASE_CONFIG.split("\n", 1)[1])
    assert profile.init(world.url, auto_sync=True, notify=world.notify).result == "pushed"
    return local


def test_apply_commit_auto_syncs_before_building_the_plan(world, fake_agent_tools, tmp_path, capsys):
    local = _auto_sync_machine(world, fake_agent_tools)
    remote_doc = _load(local)
    remote_doc["git"]["config"]["init.defaultBranch"] = "trunk"
    world.push_remote_edit(yaml.safe_dump(remote_doc))
    world.machine("a")
    workdir = tmp_path / "nongit"
    workdir.mkdir()
    rc = main(["apply", "commit", "--only", "git", "-C", str(workdir)])
    captured = capsys.readouterr()
    assert rc == 0, captured.out + captured.err
    assert "profile auto_sync: pulled" in captured.out
    assert _load(local)["git"]["config"]["init.defaultBranch"] == "trunk"
    # the plan was built from the PULLED file: the new key is in the applied git action
    assert "init.defaultBranch" in captured.out


def test_apply_info_never_syncs(world, fake_agent_tools, tmp_path, capsys):
    local = _auto_sync_machine(world, fake_agent_tools)
    before = local.read_bytes()
    world.push_remote_edit("version: 1\n")
    world.machine("a")
    workdir = tmp_path / "nongit"
    workdir.mkdir()
    assert main(["apply", "--only", "git", "-C", str(workdir)]) == 0
    out = capsys.readouterr().out
    assert "profile.auto_sync is on" in out
    assert local.read_bytes() == before


def test_apply_commit_auto_sync_failure_is_non_fatal(world, fake_agent_tools, tmp_path, capsys):
    local = _auto_sync_machine(world, fake_agent_tools)
    before = local.read_bytes()
    world.remote.rename(tmp_path / "gone.git")  # the remote is unreachable now
    workdir = tmp_path / "nongit"
    workdir.mkdir()
    rc = main(["apply", "commit", "--only", "git", "-C", str(workdir)])
    captured = capsys.readouterr()
    assert rc == 0, captured.out + captured.err
    assert "profile auto_sync skipped" in captured.err
    assert local.read_bytes() == before
