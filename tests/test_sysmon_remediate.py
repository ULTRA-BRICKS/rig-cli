"""sysmon auto-remediation (rig-cli#622): the brief, the per-harness launch argv, and the gates."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import time
from pathlib import Path

import pytest

from riglib.sysmon_daemon import config, remediate
from riglib.sysmon_daemon.model import AgentHost, Attribution, Finding, Offender, to_jsonable

NOW = 1_790_000_000.0
OMP = AgentHost(pid=900, kind="omp", args="omp", cwd="/xp/app", session_file="/s/omp-a.jsonl",
                session_id="abc", pane="tmux:main:1.0")
LAUNCHER_KINDS = ("omp", "codex", "opencode")


def _incident(kind="live", severity="critical", incident_id="20260929T120000-live"):
    offender = Offender(pid=4242, comm="whisper-cli", args="whisper-cli -m ggml-large.bin", rss_kb=1_600_000,
                        start_time=5.0, attribution=Attribution(pid=4242, chain=(4242, 900, 1), host=OMP,
                                                                pane="tmux:main:1.0"))
    finding = Finding(key="gpu_models", kind="gpu", severity=severity, summary="13 GPU-model processes running",
                      metrics={"gpu_models": 13}, offenders=(offender,), healable=True)
    return {
        "id": incident_id, "kind": kind, "opened_at": NOW - 60, "updated_at": NOW, "severity": severity,
        "findings": [to_jsonable(finding)], "sample": {"load_per_cpu": 3.1, "wired_pct": 95.7},
        "sessions": [{"kind": "omp", "pid": 900, "start_time": 1.0, "cwd": "/xp/app",
                      "session_file": "/s/omp-a.jsonl", "session_id": "abc", "pane": "tmux:main:1.0",
                      "title": "Ship the monitor", "captured_at": NOW - 300}],
        "crash": None, "heal": [], "remediation": None, "restore": [], "alerts": [],
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A tmp HOME with every launcher installed and a tmp agent log dir."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("RIG_AGENT_LOG_DIR", str(tmp_path / "agent-logs"))
    for kind in LAUNCHER_KINDS:
        launcher = tmp_path / "skills" / f"rig-detached-{kind}" / f"rig-detached-{kind}"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\nexit 0\n")
        launcher.chmod(0o755)
    (tmp_path / "work").mkdir()
    cfg = config.load(None)
    cfg["auto_remediate"] = True
    cfg["remediation"].update({"agent": "omp", "skills_dir": "~/skills", "workdir": "~/work", "cooldown_minutes": 60})
    return tmp_path, cfg


class FakeRun:
    def __init__(self, returncode=0, stderr="", raises=None):
        self.returncode = returncode
        self.stderr = stderr
        self.raises = raises
        self.calls = []

    def __call__(self, cmd, **kwargs):
        assert kwargs.get("stdin") is subprocess.DEVNULL and kwargs.get("timeout")
        self.calls.append((list(cmd), kwargs))
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(cmd, self.returncode, "detached agent launched", self.stderr)


# ── gates ─────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("flag", [False, "true", 1])
def test_anything_but_an_explicit_true_is_disabled_and_writes_nothing(env, flag):
    home, cfg = env
    cfg["auto_remediate"] = flag
    run = FakeRun()
    result, state = remediate.maybe_spawn(_incident(), home / "inc", cfg, {"x": 1}, NOW, run=run)
    assert result["status"] == "disabled" and state == {"x": 1}
    assert run.calls == [] and not (home / "inc").exists()


def test_a_live_warning_is_skipped_but_a_post_crash_warning_is_not(env):
    home, cfg = env
    result, _ = remediate.maybe_spawn(_incident(severity="warning"), home / "a", cfg, {}, NOW, run=FakeRun())
    assert result["status"] == "skipped"
    result, _ = remediate.maybe_spawn(_incident(kind="post-crash", severity="warning"), home / "b", cfg, {}, NOW,
                                      run=FakeRun())
    assert result["status"] == "spawned"


def test_cooldown_blocks_a_second_spawn_until_it_expires(env):
    home, cfg = env
    run = FakeRun()
    inside = {"last_spawn_at": NOW - 59 * 60}
    result, state = remediate.maybe_spawn(_incident(), home / "inc", cfg, inside, NOW, run=run)
    assert result["status"] == "skipped" and state == inside and run.calls == []
    result, state = remediate.maybe_spawn(_incident(), home / "inc", cfg, {"last_spawn_at": NOW - 60 * 60}, NOW, run=run)
    assert result["status"] == "spawned" and state["last_spawn_at"] == NOW


def test_dry_run_returns_the_argv_runs_nothing_and_keeps_the_cooldown_free(env):
    home, cfg = env
    cfg["remediation"]["dry_run"] = True
    run = FakeRun()
    result, state = remediate.maybe_spawn(_incident(), home / "inc", cfg, {}, NOW, run=run)
    assert result["status"] == "dry_run" and run.calls == [] and state == {}
    assert result["command"][0] == str(home / "skills/rig-detached-omp/rig-detached-omp")


# ── spawn ─────────────────────────────────────────────────────────────────────────────────
def test_spawn_runs_the_launcher_with_the_brief_and_records_the_handoff(env):
    home, cfg = env
    run = FakeRun()
    inc_dir = home / "state/incidents/20260929T120000-live"
    result, state = remediate.maybe_spawn(_incident(), inc_dir, cfg, {}, NOW, run=run)
    name = "sysmon-remediate-20260929T120000-live"
    brief = inc_dir / "remediation-brief.md"
    assert result["status"] == "spawned"
    assert (result["agent"], result["name"]) == ("omp", name)
    assert result["handoff"] == str(inc_dir / "remediation-handoff.md")
    assert result["log"] == str(home / "agent-logs" / f"{name}.log")
    assert [c for c, _ in run.calls] == [[str(home / "skills/rig-detached-omp/rig-detached-omp"), name, str(brief),
                                          str(home / "work")]]
    assert brief.read_text(encoding="utf-8").startswith("# ")
    assert state["last_spawn_at"] == NOW


@pytest.mark.parametrize("fake, needle", [(FakeRun(returncode=2, stderr="brief file missing"), "brief file missing"),
                                          (FakeRun(raises=FileNotFoundError("nope")), "nope"),
                                          (FakeRun(raises=subprocess.TimeoutExpired("x", 30)), "timed out")])
def test_a_failed_launch_is_an_error_result_and_keeps_the_cooldown_free(env, fake, needle):
    home, cfg = env
    result, state = remediate.maybe_spawn(_incident(), home / "inc", cfg, {}, NOW, run=fake)
    assert result["status"] == "error" and needle in result["error"] and state == {}


@pytest.mark.parametrize("agent", ["pi", "omp-missing"])
def test_unknown_agent_or_missing_launcher_is_an_error_not_an_exception(env, agent):
    home, cfg = env
    if agent == "omp-missing":
        (home / "skills/rig-detached-omp/rig-detached-omp").unlink()
        agent = "omp"
    cfg["remediation"]["agent"] = agent
    run = FakeRun()
    result, state = remediate.maybe_spawn(_incident(), home / "inc", cfg, {}, NOW, run=run)
    assert result["status"] == "error" and result["error"] and run.calls == [] and state == {}


def test_agent_name_is_launcher_safe_whatever_the_incident_id(env):
    home, cfg = env
    result, _ = remediate.maybe_spawn(_incident(incident_id="2026-09-29 12:00/../x"), home / "inc", cfg, {}, NOW,
                                      run=FakeRun())
    assert re.fullmatch(r"sysmon-remediate-[A-Za-z0-9._-]+", result["name"])
    assert "/" not in result["name"] and " " not in result["name"]


# ── launch argv per harness ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("kind", LAUNCHER_KINDS)
def test_launcher_kinds_use_the_rig_detached_launcher_under_the_expanded_skills_dir(env, kind):
    home, cfg = env
    argv, extra = remediate.launch_command(kind, "n1", Path("/b.md"), Path("/w"), cfg)
    assert argv == [str(home / f"skills/rig-detached-{kind}/rig-detached-{kind}"), "n1", "/b.md", "/w"]
    assert extra == {}  # the launcher sets the identity markers itself


def test_claude_code_launch_detaches_a_print_run_with_the_brief_and_identity(env):
    """Run the real /bin/sh line against a fake ``claude`` to prove the argv, env and log."""
    home, cfg = env
    bin_dir = home / "bin"
    bin_dir.mkdir()
    out = home / "claude-called"
    fake = bin_dir / "claude"
    fake.write_text(f'#!/bin/sh\n{{ printf "%s\\n" "$@"; echo "ID=$RIG_AGENT_ID DETACHED=$RIG_DETACHED_AGENT"; }}'
                    f' > "{out}.tmp"\nmv "{out}.tmp" "{out}"\necho child-output\n')
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    brief = home / "brief.md"
    brief.write_text("# fix the load\nline two\n")
    argv, extra = remediate.launch_command("claude-code", "n2", brief, home / "work", cfg)
    assert extra == {"RIG_AGENT_ID": "n2", "RIG_DETACHED_AGENT": "1"}
    proc = subprocess.run(argv, env={**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin", **extra},
                          capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, proc.stderr
    deadline = time.time() + 10
    while not out.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert out.read_text().splitlines() == ["-p", "--permission-mode", "bypassPermissions", "# fix the load",
                                            "line two", "ID=n2 DETACHED=1"]
    log = home / "agent-logs" / "n2.log"
    while "child-output" not in (log.read_text() if log.exists() else "") and time.time() < deadline:
        time.sleep(0.05)
    assert "child-output" in log.read_text()


def test_claude_code_launch_fails_fast_when_claude_is_not_on_path(env):
    home, cfg = env
    brief = home / "brief.md"
    brief.write_text("# x\n")
    argv, extra = remediate.launch_command("claude-code", "n3", brief, home / "work", cfg)
    proc = subprocess.run(argv, env={**os.environ, "PATH": "/usr/bin:/bin", **extra}, capture_output=True, text=True,
                          timeout=10, stdin=subprocess.DEVNULL)
    assert proc.returncode != 0 and "claude" in proc.stderr


# ── brief ─────────────────────────────────────────────────────────────────────────────────
def test_brief_carries_the_evidence_the_restore_command_and_the_handoff(tmp_path):
    inc_dir = tmp_path / "incidents/20260929T120000-live"
    text = remediate.build_brief(_incident(), inc_dir, config.load(None))
    for needle in ("20260929T120000-live", str(inc_dir / "incident.json"), str(inc_dir / "remediation-handoff.md"),
                   "rig sysmon restore --incident 20260929T120000-live", "4242", "whisper-cli", "/s/omp-a.jsonl",
                   "tmux:main:1.0", "95.7", "tg report", "task new", "/tmp/heavy-run.lock", "lldb"):
        assert needle in text, needle
    assert "--dry-run" not in text


def test_drill_brief_dry_runs_only_the_report_and_carries_the_planted_evidence(tmp_path):
    """A drill must never message Telegram, but it restores its (throwaway) sessions for real and
    points the agent at the cause it planted, so the end-to-end fix is exercised."""
    incident = _incident(kind="simulated", incident_id="20260929T120000-simulated")
    incident["evidence"] = ["/tmp/drill/run-models.sh launches every model at once"]
    text = remediate.build_brief(incident, tmp_path, config.load(None))
    assert "rig sysmon restore --incident 20260929T120000-simulated`" in text
    assert re.search(r"tg report [^\n]*--dry-run", text)
    assert "Drill evidence: /tmp/drill/run-models.sh" in text and "do NOT file tickets" in text


def test_post_crash_brief_names_the_crash(tmp_path):
    incident = _incident(kind="post-crash")
    incident["crash"] = {"reason": "panic", "last_heartbeat": NOW - 30, "panic_file": "/Library/Logs/x.panic",
                         "panic_excerpt": "panic(cpu 3 caller 0xfffff): wired memory exhausted"}
    text = remediate.build_brief(incident, tmp_path, config.load(None))
    assert "/Library/Logs/x.panic" in text and "wired memory exhausted" in text
