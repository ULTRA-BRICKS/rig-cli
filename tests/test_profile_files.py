"""``riglib.profile_files`` — what travels with ``rig profile``, how, and what never does.

Pure transformations: no git, no disk beyond ``tmp_path``. Two "machines" differ by ``$HOME``.
Secret-shaped fixtures are assembled at run time so no literal credential pattern sits in the source.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from riglib import profile_files as pf
from riglib.tmux import BLOCK_BEGIN, BLOCK_END

# fake credentials, split so the file itself never matches a secret scanner
FAKE_GH = "gh" + "p_" + "0123456789abcdefghijklmnopqrstuvwxyz"
FAKE_SK = "s" + "k-" + "ant0123456789abcdefghijkl"
FAKE_AWS = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
FAKE_JWT = ".".join(["ey" + "JhbGciOiJIUzI1NiJ9", "ey" + "JzdWIiOiIxMjM0NTY3ODkwIn0", "dBjftJeZ4CVPmB92K27uhbUJU1p1r"])
FAKE_PEM = "-----BEGIN " + "OPENSSH PRIVATE" + " KEY-----"
FAKE_VALUE_A = "a1b2c3d4e5" + "f6g7h8i9j0"
FAKE_VALUE_B = "abc123def4" + "56ghi789"
FAKE_VALUE_C = "abcdef0123" + "456789zz"
FAKE_VALUE_D = "abcdefghij" + "klmnop1234"
FAKE_VALUE_E = "keepme1234" + "567890abc"
FAKE_VALUE_F = "mine012345" + "6789abcdef"
FAKE_PW = "hunter2hun" + "ter2"
KEY_VAR = "OPENAI" + "_API_KEY"


def _kv(key: str, value: str) -> str:
    return f"{key}: {value}"


def _spec(file_id: str) -> pf.FileSpec:
    return pf.spec_by_id(file_id)


@pytest.fixture
def on_machine(monkeypatch, tmp_path):
    def go(name: str) -> Path:
        home = tmp_path / name
        home.mkdir(exist_ok=True)
        monkeypatch.setenv("HOME", str(home))
        return home

    return go


# ── the registry ─────────────────────────────────────────────────────────────────────────────
def test_the_allow_list_is_exactly_the_declared_portable_files():
    assert pf.file_ids() == ("config", "tmux", "omp_config", "omp_mcp", "sysmon")
    assert {spec.kind for spec in pf.FILES} == {pf.KIND_PORTABLE}
    assert len({spec.repo_path for spec in pf.FILES}) == len(pf.FILES)


def test_secret_bearing_things_are_registered_and_never_on_the_allow_list(on_machine):
    on_machine("a")
    names = {entry.name for entry in pf.NEVER_SYNCED}
    assert {"env.vars", "auth-broker.token", "agent.db", "remote-host", "ssh-control"} <= names
    travelling = {spec.local().name for spec in pf.FILES}
    for entry in (e for e in pf.NEVER_SYNCED if e.name != "env.vars"):
        assert Path(entry.where.split()[0]).name not in travelling, entry


def test_local_paths_follow_home_and_the_omp_agent_dir_override(on_machine, monkeypatch):
    home = on_machine("a")
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.delenv("PI_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert _spec("tmux").local() == home / ".tmux.conf"
    assert _spec("omp_config").local() == home / ".omp" / "agent" / "config.yml"
    assert _spec("sysmon").local() == home / ".config" / "rig" / "sysmon" / "sysmon.json"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(home / "alt-agent"))
    assert _spec("omp_mcp").local() == home / "alt-agent" / "mcp.json"


def test_machine_name_is_sanitised_and_overridable(monkeypatch):
    monkeypatch.setenv("RIG_PROFILE_MACHINE", "my box/1")
    assert pf.machine_name() == "my-box-1"
    monkeypatch.setenv("RIG_PROFILE_MACHINE", "")
    assert pf.machine_name()


# ── tmux: only the user's region travels ─────────────────────────────────────────────────────
USER_TMUX = "set -g mouse on\nset -g history-limit 100000\n"


def test_tmux_block_mode_keeps_the_managed_block_on_the_machine(on_machine):
    on_machine("a")
    local = f"{USER_TMUX}{BLOCK_BEGIN}\nrun-shell /abs/continuum.tmux\n{BLOCK_END}\n".encode()
    assert pf.prepare(_spec("tmux"), local).data == USER_TMUX.encode()


def test_tmux_import_mode_drops_rig_source_lines_of_any_generated_dir(on_machine):
    on_machine("a")
    local = (
        "set -g mouse on\nsource-file '/old/dir/rig.tmux.conf'\n"
        "bind r source-file ~/.tmux.conf\nsource-file -q /x/rig.tmux.conf\n"
    ).encode()
    assert pf.prepare(_spec("tmux"), local).data == b"set -g mouse on\nbind r source-file ~/.tmux.conf\n"


def test_tmux_adopt_keeps_this_machines_managed_tail_last(on_machine):
    on_machine("a")
    tail = "source-file '/home/b/.config/rig/tmux/rig.tmux.conf'\n"
    local = ("# local only\n" + tail).encode()
    out = pf.adopt(_spec("tmux"), USER_TMUX.encode(), local, []).data.decode()
    assert out == USER_TMUX + tail
    assert pf.adopt(_spec("tmux"), USER_TMUX.encode(), None, []).data.decode() == USER_TMUX


def test_tmux_home_paths_travel_between_machines_with_different_homes(on_machine):
    a = on_machine("a")
    portable = pf.prepare(_spec("tmux"), f"run-shell {a}/bin/x.sh\nset -g @k {a}x\n".encode()).data
    assert b"{{rig:home}}/bin/x.sh" in portable
    assert f"{a}x".encode() in portable  # not followed by a slash: not a path of the home dir
    b = on_machine("b")
    assert pf.adopt(_spec("tmux"), portable, None, []).data.decode().startswith(f"run-shell {b}/bin/x.sh")


# ── yaml: env.vars withheld, machine values, home tokens ─────────────────────────────────────
CONFIG = """\
version: 1
agent_tools_source: {home}/dev/agent-tools
env:
  vars:
    {var}: placeholder
tools:
  enabled: true
  items:
    ctmux: {{}}
    qa: {{repo: {home}/xp/qa-cli}}
"""


def _cfg(home: Path) -> bytes:
    return CONFIG.format(home=home, var=KEY_VAR).encode()


def test_env_vars_never_enter_the_portable_config(on_machine):
    a = on_machine("a")
    prepared = pf.prepare(_spec("config"), _cfg(a))
    doc = yaml.safe_load(prepared.data)
    assert "env" not in doc
    assert KEY_VAR.encode() not in prepared.data and b"placeholder" not in prepared.data
    assert any(KEY_VAR in text for _lvl, text in prepared.notes)


def test_an_env_block_with_other_keys_survives_without_its_vars(on_machine):
    a = on_machine("a")
    doc = yaml.safe_load(_cfg(a)) | {"env": {"enabled": True, "vars": {"K": "placeholder"}}}
    out = yaml.safe_load(pf.prepare(_spec("config"), yaml.safe_dump(doc).encode()).data)
    assert out["env"] == {"enabled": True}


def test_a_config_with_nothing_to_transform_is_stored_byte_for_byte(on_machine):
    on_machine("a")
    raw = b"# my comment\nversion: 1\ngit:\n  config:\n    pull.rebase: true\n"
    assert pf.prepare(_spec("config"), raw).data == raw


def test_home_paths_and_machine_values_are_portable_and_recorded(on_machine):
    a = on_machine("a")
    prepared = pf.prepare(_spec("config"), _cfg(a))
    text = prepared.data.decode()
    assert str(a) not in text and "{{rig:home}}/dev/agent-tools" in text
    assert {path for path, _ in prepared.machine} == {
        ("agent_tools_source",), ("tools", "items", "qa", "repo"),
    }


def test_two_machines_with_different_machine_values_compare_equal(on_machine):
    a = on_machine("a")
    da = pf.view_digest(_spec("config"), pf.prepare(_spec("config"), _cfg(a)).data)
    b = on_machine("b")
    other = _cfg(b).replace(b"/dev/agent-tools", b"/xp/agent-tools")
    db = pf.view_digest(_spec("config"), pf.prepare(_spec("config"), other).data)
    assert da == db
    changed = other.replace(b"enabled: true", b"enabled: false")
    assert pf.view_digest(_spec("config"), pf.prepare(_spec("config"), changed).data) != db


def test_adopt_local_machine_values_win_and_env_vars_are_kept(on_machine):
    a = on_machine("a")
    remote = pf.prepare(_spec("config"), _cfg(a)).data
    b = on_machine("b")
    local = (
        f"version: 1\nagent_tools_source: {b}/xp/agent-tools\n"
        f"env:\n  vars:\n    KEPT_VAR: {FAKE_VALUE_E}\n"
        f"tools:\n  items:\n    qa: {{repo: {b}/work/qa}}\n"
    ).encode()
    doc = yaml.safe_load(pf.adopt(_spec("config"), remote, local, []).data)
    assert doc["agent_tools_source"] == f"{b}/xp/agent-tools"
    assert doc["env"] == {"vars": {"KEPT_VAR": FAKE_VALUE_E}}
    assert doc["tools"]["items"]["qa"]["repo"] == f"{b}/work/qa"
    assert doc["tools"]["items"]["ctmux"] == {}


def test_adopt_on_a_new_machine_drops_optional_machine_keys_but_keeps_required_ones(on_machine):
    a = on_machine("a")
    remote = pf.prepare(_spec("config"), _cfg(a)).data
    b = on_machine("b")
    doc = yaml.safe_load(pf.adopt(_spec("config"), remote, None, []).data)
    assert "agent_tools_source" not in doc  # optional: this machine resolves its own checkout
    assert doc["tools"]["items"]["qa"]["repo"] == f"{b}/xp/qa-cli"  # required: the default, re-homed
    assert "env" not in doc


def test_adopt_restores_values_from_the_machine_overlay(on_machine):
    a = on_machine("a")
    remote = pf.prepare(_spec("config"), _cfg(a)).data
    on_machine("b")
    record = [(("agent_tools_source",), "{{rig:home}}/mine/agent-tools")]
    doc = yaml.safe_load(pf.adopt(_spec("config"), remote, None, record).data)
    assert doc["agent_tools_source"].endswith("/mine/agent-tools") and "{{" not in doc["agent_tools_source"]


def test_a_per_machine_tool_toggle_is_a_machine_value(on_machine):
    a = on_machine("a")
    base = _cfg(a)
    off = base.replace(b"ctmux: {}", b"ctmux: {enabled: false}")
    spec = _spec("config")
    assert pf.view_digest(spec, pf.prepare(spec, off).data) == pf.view_digest(spec, pf.prepare(spec, base).data)
    remote = pf.prepare(spec, base).data
    doc = yaml.safe_load(pf.adopt(spec, remote, off, []).data)
    assert doc["tools"]["items"]["ctmux"] == {"enabled": False}


def test_foreign_absolute_paths_are_machine_values_in_every_structured_file(on_machine):
    on_machine("a")
    raw = b"tools:\n  approvalMode: yolo\nbin: /opt/homebrew/bin/node\nlist: [/usr/local/x, plain]\nnote: /\n"
    prepared = pf.prepare(_spec("omp_config"), raw)
    assert {path for path, _ in prepared.machine} == {("bin",), ("list",)}


def test_invalid_yaml_and_json_are_refused_with_a_fix(on_machine):
    on_machine("a")
    with pytest.raises(pf.PayloadError, match="not valid YAML"):
        pf.prepare(_spec("omp_config"), b"a: [unclosed\n")
    with pytest.raises(pf.PayloadError, match="not valid JSON"):
        pf.prepare(_spec("omp_mcp"), b"{nope")
    with pytest.raises(pf.PayloadError, match="not a mapping"):
        pf.prepare(_spec("sysmon"), b"[1]")


# ── mcp.json: masked on the way out, restored on the way in ──────────────────────────────────
MCP = {
    "$schema": "https://example.com/schema.json",
    "mcpServers": {
        "runpod": {"type": "http", "url": "https://mcp.example.com/mcp",
                   "headers": {"Authorization": f"Bearer {FAKE_VALUE_D}"}},
        "local": {
            "command": "node", "args": ["srv.js", "--api-key", FAKE_SK, "--port=80",
                                        f"--token={FAKE_VALUE_C}", "--verbose"],
            "env": {"NODE_ENV": "production", "GH_TOKEN": FAKE_GH, "REF": "${GH_TOKEN}"},
        },
        "u": {"url": f"https://user:{FAKE_PW}@host.example.com/x?token={FAKE_VALUE_C}&ok=1"},
    },
}


def test_mcp_secrets_are_masked_and_references_and_plain_values_are_kept(on_machine):
    on_machine("a")
    out = json.loads(pf.prepare(_spec("omp_mcp"), json.dumps(MCP).encode()).data)["mcpServers"]
    assert out["runpod"]["headers"]["Authorization"] == pf.MASK
    assert out["local"]["env"] == {"NODE_ENV": "production", "GH_TOKEN": pf.MASK, "REF": "${GH_TOKEN}"}
    assert out["local"]["args"] == ["srv.js", "--api-key", pf.MASK, "--port=80", "--token=" + pf.MASK, "--verbose"]
    assert out["u"]["url"] == f"https://user:{pf.MASK}@host.example.com/x?token={pf.MASK}&ok=1"
    assert out["runpod"]["url"] == "https://mcp.example.com/mcp"


def test_a_masked_mcp_file_passes_the_secret_scan(on_machine):
    on_machine("a")
    data = pf.prepare(_spec("omp_mcp"), json.dumps(MCP).encode()).data.decode()
    assert pf.scan_secrets("mcp.json", data) == []
    assert pf.scan_secrets("raw", json.dumps(MCP, indent=2))  # the unmasked original would be refused


def test_adopting_a_masked_mcp_restores_local_values_and_reports_the_rest(on_machine):
    on_machine("a")
    remote = pf.prepare(_spec("omp_mcp"), json.dumps(MCP).encode()).data
    mine = f"Bearer {FAKE_VALUE_F}"
    local = {"mcpServers": {"runpod": {"headers": {"Authorization": mine}}}}
    out = pf.adopt(_spec("omp_mcp"), remote, json.dumps(local).encode(), [])
    doc = json.loads(out.data)["mcpServers"]
    assert doc["runpod"]["headers"]["Authorization"] == mine
    assert doc["local"]["env"]["GH_TOKEN"] == pf.MASK
    warn = " ".join(text for level, text in out.notes if level == "warn")
    assert "/mcpServers/local/env/GH_TOKEN" in warn and "Authorization" not in warn


def test_an_unchanged_masked_file_compares_equal_to_the_local_original(on_machine):
    on_machine("a")
    spec = _spec("omp_mcp")
    local = json.dumps(MCP, indent=4).encode()
    remote = pf.prepare(spec, local).data
    assert pf.view_digest(spec, pf.prepare(spec, local).data) == pf.view_digest(spec, remote)


# ── the secret scan ──────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,rule", [
    (FAKE_PEM, "private-key"),
    ("x " + FAKE_GH, "github-token"),
    ("k = " + FAKE_SK, "api-key"),
    (FAKE_AWS, "api-key"),
    (FAKE_JWT, "jwt"),
    (f"Authorization: Bearer {FAKE_VALUE_D}", "bearer-token"),
    (f"repo: https://me:{FAKE_PW}@git.example.com/x", "url-credentials"),
    (_kv("api_key", FAKE_VALUE_A), "secret-assignment"),
    (_kv('"password"', f'"{FAKE_VALUE_B}"'), "secret-assignment"),
])
def test_scan_finds_secrets_and_reports_rule_and_line_only(text, rule):
    (finding,) = pf.scan_secrets("f.yml", "ok: 1\n" + text + "\n")
    assert (finding.file, finding.line, finding.rule) == ("f.yml", 2, rule)
    assert text not in repr(finding)


@pytest.mark.parametrize("text", [
    _kv("token_env", "GITHUB_TOKEN_ENV_NAME"),
    _kv("token", "per-session-rotate-strategy"),
    _kv("api_key", "${" + KEY_VAR + "}"),
    _kv("Authorization", pf.MASK),
    _kv("secret_file", "~/.config/rig/secret0123456789.txt"),
    "set -g mouse on",
    "bearer_style: none",
    "url: https://example.com/path?x=1",
])
def test_scan_leaves_ordinary_config_alone(text):
    assert pf.scan_secrets("f", text) == []


# ── the overlay record ───────────────────────────────────────────────────────────────────────
def test_record_round_trips_and_is_empty_without_machine_values():
    assert pf.record_data("m", {"config": [], "tmux": []}) is None
    values = {"config": [(("agent_tools_source",), "{{rig:home}}/x"), (("tools", "items", "a", "repo"), "/opt/a")]}
    data = pf.record_data("home", values)
    assert pf.parse_record(data) == values
    assert pf.parse_record(None) == {} and pf.parse_record(b"::: not yaml [") == {}
    assert pf.record_repo_path("home") == "machine/home.yaml"
