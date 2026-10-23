"""``rig profile`` moving the allow-listed files (tmux, omp config, masked mcp.json, sysmon) both ways.

Same hermetic world as :mod:`test_profile` (a local bare repo as the remote, a tmp git identity, the
forge probes stubbed) with one addition: each "machine" also has its own ``$HOME``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from test_profile import BASE_CONFIG, _git, _load, _write, world  # noqa: F401 — the shared fixture

from riglib import profile
from riglib import profile_files as pf

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0, reason="git not installed"
)

TMUX = "set -g mouse on\nset -g history-limit 100000\n"
OMP = "modelRoles:\n  default: anthropic/claude-sonnet-5-5\ntools:\n  approvalMode: yolo\n"
SYSMON = {"alert": {"cooldown_minutes": 30}, "auto_remediate": False}
BEARER = "Bearer " + "abcdefghij" + "klmnop1234"


def _mcp() -> str:
    return json.dumps({"mcpServers": {"runpod": {"type": "http", "url": "https://mcp.example.com/mcp",
                                                  "headers": {"Authorization": BEARER}}}}, indent=2)


class Box:
    """One machine: its own HOME, XDG config home and checkout."""

    def __init__(self, world, monkeypatch, tmp_path, name: str) -> None:
        self.world, self.mp, self.name = world, monkeypatch, name
        self.home = tmp_path / name / "home"
        self.home.mkdir(parents=True, exist_ok=True)

    def on(self) -> "Box":
        self.mp.setenv("HOME", str(self.home))
        self.mp.setenv("RIG_PROFILE_MACHINE", self.name)
        self.mp.delenv("PI_CODING_AGENT_DIR", raising=False)
        self.mp.delenv("PI_CONFIG_DIR", raising=False)
        self.config = self.world.machine(self.name)
        return self

    @property
    def tmux(self) -> Path:
        return self.home / ".tmux.conf"

    @property
    def omp(self) -> Path:
        return self.home / ".omp" / "agent" / "config.yml"

    @property
    def mcp(self) -> Path:
        return self.home / ".omp" / "agent" / "mcp.json"

    @property
    def sysmon(self) -> Path:
        return self.config.parent / "sysmon" / "sysmon.json"

    def fill(self, *, tmux=True, omp=True, mcp=True, sysmon=True) -> "Box":
        _write(self.config, BASE_CONFIG)
        if tmux:
            _write(self.tmux, TMUX)
        if omp:
            _write(self.omp, OMP)
        if mcp:
            _write(self.mcp, _mcp())
        if sysmon:
            _write(self.sysmon, json.dumps(SYSMON, indent=1, sort_keys=True) + "\n")
        return self


@pytest.fixture
def boxes(world, monkeypatch, tmp_path):  # noqa: F811
    return lambda name: Box(world, monkeypatch, tmp_path, name).on()


def _tracked(world) -> list[str]:
    return _git("ls-files", cwd=profile.checkout_dir()).splitlines()


def _remote_show(world, path: str) -> str:
    return subprocess.run(["git", "-C", str(world.remote), "show", f"main:{path}"], capture_output=True,
                          text=True, check=True).stdout


# ── seeding and adopting ─────────────────────────────────────────────────────────────────────
def test_init_pushes_every_present_allow_listed_file_in_its_portable_form(world, boxes):
    a = boxes("a").fill()
    a.tmux.write_text(TMUX + f"run-shell {a.home}/bin/x.sh\nsource-file '/x/rig.tmux.conf'\n", encoding="utf-8")
    out = profile.init(world.url, notify=world.notify)
    assert out.result == "pushed", out.detail
    assert _tracked(world) == [
        "config.yaml", "files/omp/config.yml", "files/omp/mcp.json", "files/sysmon.json", "files/tmux.conf",
    ]
    tmux = _remote_show(world, "files/tmux.conf")
    assert "rig.tmux.conf" not in tmux and "{{rig:home}}/bin/x.sh" in tmux and str(a.home) not in tmux
    assert json.loads(_remote_show(world, "files/omp/mcp.json"))["mcpServers"]["runpod"]["headers"] == {
        "Authorization": pf.MASK
    }
    assert BEARER not in _remote_show(world, "files/omp/mcp.json")
    assert json.loads(a.mcp.read_text())["mcpServers"]["runpod"]["headers"]["Authorization"] == BEARER
    assert world.remote_commits() == 1


def test_files_that_do_not_exist_locally_are_simply_not_synced(world, boxes):
    boxes("a").fill(tmux=False, omp=False, mcp=False, sysmon=False)
    assert profile.init(world.url, notify=world.notify).result == "pushed"
    assert _tracked(world) == ["config.yaml"]
    assert profile.sync(notify=world.notify).result == "in_sync"


def test_a_second_machine_adopts_every_file_and_keeps_its_own_tmux_tail_and_secrets(world, boxes):
    a = boxes("a").fill()
    profile.init(world.url, notify=world.notify)
    b = boxes("b")
    tail = f"source-file '{b.home}/.config/rig/tmux/rig.tmux.conf'\n"
    _write(b.tmux, "# my old conf\n" + tail)
    out = profile.init(world.url, notify=world.notify)
    # b has its own ~/.tmux.conf that differs and was never synced: a conflict, never a silent overwrite
    assert out.code == profile.EXIT_CONFLICT and "tmux" in out.detail
    assert b.tmux.read_text(encoding="utf-8") == "# my old conf\n" + tail
    assert b.omp.read_text(encoding="utf-8") == OMP
    assert b.sysmon.is_file()
    mcp = json.loads(b.mcp.read_text())["mcpServers"]["runpod"]["headers"]["Authorization"]
    assert mcp == pf.MASK  # this machine still has to fill it in
    assert any("fill in the masked value" in t for _l, t in world.messages + out.messages)

    resolved = profile.sync(take="remote", only=("tmux",), notify=world.notify)
    assert resolved.result == "pulled" and resolved.backup is not None
    assert resolved.backup.read_text(encoding="utf-8") == "# my old conf\n" + tail
    assert b.tmux.read_text(encoding="utf-8") == TMUX + tail  # the user region, then b's own import line
    assert profile.sync(notify=world.notify).result == "in_sync"
    assert a.tmux.read_text(encoding="utf-8") == TMUX


def test_a_fresh_machine_gets_everything_with_no_conflict(world, boxes):
    boxes("a").fill()
    profile.init(world.url, notify=world.notify)
    b = boxes("b")
    out = profile.init(world.url, notify=world.notify)
    assert out.code == 0, (out.detail, out.messages)
    assert b.tmux.read_text(encoding="utf-8") == TMUX
    assert yaml.safe_load(b.omp.read_text(encoding="utf-8")) == yaml.safe_load(OMP)
    assert json.loads(b.sysmon.read_text()) == SYSMON
    assert profile.sync(notify=world.notify).result == "in_sync"


# ── changes both ways ────────────────────────────────────────────────────────────────────────
def test_an_edit_pushes_one_file_and_the_other_machine_pulls_it_with_a_backup(world, boxes):
    a = boxes("a").fill()
    profile.init(world.url, notify=world.notify)
    b = boxes("b")
    profile.init(world.url, notify=world.notify)
    commits = world.remote_commits()
    a.on()
    a.tmux.write_text(TMUX + "set -g status-position top\n", encoding="utf-8")
    out = profile.sync(notify=world.notify)
    assert out.result == "pushed" and "tmux" in out.detail
    assert world.remote_commits() == commits + 1
    assert _git("show", "--stat", "--format=%s", "HEAD", cwd=world.remote).splitlines()[0].startswith(
        "rig profile: files/tmux.conf from "
    )
    b.on()
    old = b.tmux.read_text(encoding="utf-8")
    pulled = profile.sync(notify=world.notify)
    assert pulled.result == "pulled", pulled.detail
    assert "status-position top" in b.tmux.read_text(encoding="utf-8")
    backups = list(b.home.glob(".tmux.conf.rig-bak-*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == old


def test_a_pull_keeps_the_restrictive_mode_of_a_masked_mcp_file(world, boxes):
    a = boxes("a").fill()
    profile.init(world.url, notify=world.notify)
    b = boxes("b")
    profile.init(world.url, notify=world.notify)
    b.mcp.chmod(0o600)
    a.on()
    doc = json.loads(a.mcp.read_text())
    doc["mcpServers"]["extra"] = {"command": "node", "args": ["x.js"]}
    a.mcp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    assert profile.sync(notify=world.notify).result == "pushed"
    b.on()
    assert profile.sync(notify=world.notify).result == "pulled"
    assert b.mcp.stat().st_mode & 0o777 == 0o600
    assert "extra" in json.loads(b.mcp.read_text())["mcpServers"]


def test_a_conflict_in_one_file_never_blocks_the_others(world, boxes):
    a = boxes("a").fill()
    profile.init(world.url, notify=world.notify)
    b = boxes("b")
    profile.init(world.url, notify=world.notify)
    a.on()
    a.tmux.write_text(TMUX + "# from a\n", encoding="utf-8")
    a.omp.write_text(OMP + "hideThinkingBlock: true\n", encoding="utf-8")
    assert profile.sync(notify=world.notify).result == "pushed"
    b.on()
    b.tmux.write_text(TMUX + "# from b\n", encoding="utf-8")
    mine = b.tmux.read_bytes()
    out = profile.sync(notify=world.notify)
    assert out.result == "conflict" and out.code == profile.EXIT_CONFLICT
    assert "tmux" in out.detail and "omp_config" not in out.detail
    assert b.tmux.read_bytes() == mine  # untouched
    assert "hideThinkingBlock" in b.omp.read_text(encoding="utf-8")  # the other file still moved
    assert profile.sync(take="local", only=("tmux",), notify=world.notify).result == "pushed"
    assert "# from b" in _remote_show(world, "files/tmux.conf")


def test_a_noop_sync_makes_no_commit_with_files(world, boxes):
    boxes("a").fill()
    profile.init(world.url, notify=world.notify)
    commits = world.remote_commits()
    for _ in range(2):
        assert profile.sync(notify=world.notify).result == "in_sync"
    assert world.remote_commits() == commits


def test_a_file_can_be_switched_off_in_the_profile_block(world, boxes):
    a = boxes("a").fill()
    a.config.write_text(BASE_CONFIG + "profile:\n  files:\n    tmux: false\n    sysmon: false\n", encoding="utf-8")
    profile.init(world.url, notify=world.notify)
    assert _tracked(world) == ["config.yaml", "files/omp/config.yml", "files/omp/mcp.json"]


# ── secrets ──────────────────────────────────────────────────────────────────────────────────
def test_a_secret_pattern_refuses_the_whole_push_and_names_file_line_and_rule_only(world, boxes):
    a = boxes("a").fill()
    token = "gh" + "p_" + "0123456789abcdefghijklmnopqrstuvwxyz"
    a.omp.write_text(OMP + f"note: {token}\n", encoding="utf-8")
    with pytest.raises(profile.ProfileError) as exc:
        profile.init(world.url, notify=world.notify)
    message = str(exc.value)
    assert "files/omp/config.yml line 5 (github-token)" in message
    assert token not in message and token not in exc.value.fix
    assert "profile.files" in exc.value.fix
    assert world.remote_commits() == 0
    assert token in a.omp.read_text(encoding="utf-8")  # the local file is untouched


def test_secret_bearing_files_and_values_never_enter_the_checkout(world, boxes):
    a = boxes("a").fill()
    secret = "topsecret-" + "0123456789abcdef"
    env_name = "BRAVE" + "_API_KEY"
    a.config.write_text(BASE_CONFIG + f"env:\n  vars:\n    {env_name}: {secret}\n", encoding="utf-8")
    for rel in (".omp/auth-broker.token", ".omp/remote-host", ".omp/agent/agent.db", ".omp/agent/.env",
                ".omp/ssh-control/sock", ".config/rig/env/rig.env.sh"):
        target = a.home / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(secret, encoding="utf-8")
    a.omp.parent.joinpath("agent.db-wal").write_text(secret, encoding="utf-8")
    profile.init(world.url, notify=world.notify)
    profile.sync(notify=world.notify)
    checkout = profile.checkout_dir()
    assert secret not in "".join(
        p.read_text(encoding="utf-8", errors="ignore") for p in checkout.rglob("*") if p.is_file() and ".git" not in p.parts
    )
    history = _git("log", "-p", "--all", cwd=checkout)
    assert secret not in history and BEARER not in history
    assert not [t for t in _tracked(world) if "agent.db" in t or "token" in t or "ssh-control" in t]


# ── status ───────────────────────────────────────────────────────────────────────────────────
def test_status_reports_each_file_and_the_worst_verdict(world, boxes):
    a = boxes("a").fill()
    profile.init(world.url, notify=world.notify)
    st = profile.status()
    assert st.verdict == "in_sync" and [f.id for f in st.files] == ["config", "tmux", "omp_config", "omp_mcp", "sysmon"]
    a.tmux.write_text(TMUX + "# local edit\n", encoding="utf-8")
    st = profile.status()
    assert st.verdict == "push"
    assert {f.id: f.verdict for f in st.files}["tmux"] == "push"
    assert {f.id: f.verdict for f in st.files}["omp_config"] == "in_sync"


# ── machine values ───────────────────────────────────────────────────────────────────────────
def test_machine_values_are_recorded_per_host_and_never_travel_as_changes(world, boxes):
    a = boxes("a").fill()
    a.config.write_text(BASE_CONFIG + f"agent_tools_source: {a.home}/dev/agent-tools\n", encoding="utf-8")
    profile.init(world.url, notify=world.notify)
    assert "machine/a.yaml" in _tracked(world)
    record = pf.parse_record(_remote_show(world, "machine/a.yaml").encode())
    assert record["config"] == [(("agent_tools_source",), "{{rig:home}}/dev/agent-tools")]

    b = boxes("b")
    b.fill(tmux=False, omp=False, mcp=False, sysmon=False)
    b.config.write_text(
        BASE_CONFIG + f"profile:\n  repo: {world.url}\nagent_tools_source: {b.home}/xp/agent-tools\n", encoding="utf-8"
    )
    out = profile.init(world.url, notify=world.notify)
    assert out.result in ("pulled", "synced", "pushed"), out.detail
    assert _load(b.config)["agent_tools_source"] == f"{b.home}/xp/agent-tools"
    assert "machine/b.yaml" in _git("ls-tree", "-r", "--name-only", "main", cwd=world.remote).splitlines()
    assert profile.sync(notify=world.notify).result == "in_sync"

    a.on()
    a.config.write_text(a.config.read_text(encoding="utf-8").replace("/dev/agent-tools", "/work/agent-tools"),
                        encoding="utf-8")
    body_before = _remote_show(world, "config.yaml")
    assert profile.sync(notify=world.notify).result == "pushed"  # only the machine record moved
    assert _remote_show(world, "config.yaml") == body_before
    assert pf.parse_record(_remote_show(world, "machine/a.yaml").encode())["config"][0][1].endswith("/work/agent-tools")


def test_a_reinstalled_machine_gets_its_machine_values_back_from_the_overlay(world, boxes, monkeypatch):
    a = boxes("a").fill(tmux=False, omp=False, mcp=False, sysmon=False)
    a.config.write_text(BASE_CONFIG + f"agent_tools_source: {a.home}/dev/agent-tools\n", encoding="utf-8")
    profile.init(world.url, notify=world.notify)
    # same host name, empty home and empty config dir: a reinstall
    monkeypatch.setattr(profile, "checkout_dir", lambda: world.tmp / "reinstall" / "repo")
    monkeypatch.setattr(profile, "state_path", lambda: world.tmp / "reinstall" / "state.json")
    monkeypatch.setattr(profile, "local_config_path", lambda: world.tmp / "reinstall" / "config.yaml")
    from riglib import profile_files

    monkeypatch.setitem(
        profile_files._BY_ID, "config",
        profile_files.FileSpec(**{**profile_files.spec_by_id("config").__dict__,
                                  "local": lambda: world.tmp / "reinstall" / "config.yaml"}),
    )
    monkeypatch.setattr(profile_files, "FILES", tuple(profile_files._BY_ID[s.id] for s in profile_files.FILES))
    out = profile.init(world.url, notify=world.notify)
    assert out.code == 0, (out.detail, out.messages)
    restored = yaml.safe_load((world.tmp / "reinstall" / "config.yaml").read_text(encoding="utf-8"))
    assert restored["agent_tools_source"] == f"{a.home}/dev/agent-tools"


def test_an_only_run_never_replaces_the_host_record_with_a_partial_one(world, boxes):
    a = boxes("a").fill()
    a.config.write_text(BASE_CONFIG + f"agent_tools_source: {a.home}/dev/agent-tools\n", encoding="utf-8")
    a.omp.write_text(OMP + "bin: /opt/homebrew/bin/node\n", encoding="utf-8")
    profile.init(world.url, notify=world.notify)
    record = pf.parse_record(_remote_show(world, "machine/a.yaml").encode())
    assert set(record) == {"config", "omp_config"}
    a.config.write_text(a.config.read_text(encoding="utf-8") + "bun: {enabled: true}\n", encoding="utf-8")
    assert profile.sync(only=("config",), notify=world.notify).result == "pushed"
    assert set(pf.parse_record(_remote_show(world, "machine/a.yaml").encode())) == {"config", "omp_config"}
