"""sysmon auto-remediation (rig-cli#622): the brief, the per-harness launch argv, and the gates."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from riglib.sysmon_daemon import config, remediate
from riglib.sysmon_daemon.model import AGENT_KINDS, AgentHost, Attribution, Finding, Offender, to_jsonable

NOW = 1_790_000_000.0
OMP = AgentHost(pid=900, kind="omp", args="omp", cwd="/xp/app", session_file="/s/omp-a.jsonl",
                session_id="abc", pane="tmux:main:1.0")


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
    for kind in AGENT_KINDS:
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


def test_an_unknown_agent_is_an_error_not_an_exception(env):
    home, cfg = env
    cfg["remediation"]["agent"] = "pi"
    run = FakeRun()
    result, state = remediate.maybe_spawn(_incident(), home / "inc", cfg, {}, NOW, run=run)
    assert result["status"] == "error" and "pi" in result["error"] and run.calls == [] and state == {}


@pytest.mark.parametrize("breakage", ["missing", "not-executable"])
@pytest.mark.parametrize("kind", AGENT_KINDS)
def test_a_missing_or_non_executable_launcher_is_an_error_for_every_kind(env, kind, breakage):
    """No kind has a fallback launch path: without its provisioned launcher the incident records
    an error naming the path `rig apply` provisions, nothing runs, and the cooldown stays free."""
    home, cfg = env
    launcher = home / f"skills/rig-detached-{kind}/rig-detached-{kind}"
    if breakage == "missing":
        launcher.unlink()
    else:
        launcher.chmod(0o644)
    cfg["remediation"]["agent"] = kind
    run = FakeRun()
    result, state = remediate.maybe_spawn(_incident(), home / "inc", cfg, {}, NOW, run=run)
    assert result["status"] == "error" and str(launcher) in result["error"]
    assert run.calls == [] and state == {}


def test_agent_name_is_launcher_safe_whatever_the_incident_id(env):
    home, cfg = env
    result, _ = remediate.maybe_spawn(_incident(incident_id="2026-09-29 12:00/../x"), home / "inc", cfg, {}, NOW,
                                      run=FakeRun())
    assert re.fullmatch(r"sysmon-remediate-[A-Za-z0-9._-]+", result["name"])
    assert "/" not in result["name"] and " " not in result["name"]


# ── launch argv per harness ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("kind", AGENT_KINDS)
def test_every_agent_kind_launches_through_its_rig_detached_launcher(env, kind):
    """omp, claude-code, codex and opencode all start through the provisioned
    ``<skills_dir>/rig-detached-<kind>/rig-detached-<kind>``, which sets the identity markers
    itself — the configured agent reaches the runner as exactly that argv."""
    home, cfg = env
    assert remediate.launch_command(kind, "n1", Path("/b.md"), Path("/w"), cfg) == [
        str(home / f"skills/rig-detached-{kind}/rig-detached-{kind}"), "n1", "/b.md", "/w"]
    cfg["remediation"]["agent"] = kind
    run = FakeRun()
    inc_dir = home / "inc"
    result, _ = remediate.maybe_spawn(_incident(), inc_dir, cfg, {}, NOW, run=run)
    assert result["status"] == "spawned"
    assert [c for c, _ in run.calls] == [[str(home / f"skills/rig-detached-{kind}/rig-detached-{kind}"),
                                          result["name"], str(inc_dir / "remediation-brief.md"),
                                          str(home / "work")]]


# ── brief ─────────────────────────────────────────────────────────────────────────────────
def test_brief_carries_the_evidence_the_restore_command_and_the_handoff(tmp_path):
    inc_dir = tmp_path / "incidents/20260929T120000-live"
    text = remediate.build_brief(_incident(), inc_dir, config.load(None))
    for needle in ("20260929T120000-live", str(inc_dir / "incident.json"), str(inc_dir / "remediation-handoff.md"),
                   "rig sysmon restore --incident 20260929T120000-live", "4242", "whisper-cli", "/s/omp-a.jsonl",
                   "tmux:main:1.0", "95.7", "tg report", "task new", "/tmp/heavy-run.lock", "lldb"):
        assert needle in text, needle
    assert "--dry-run" not in text


def test_drill_brief_dry_runs_the_report_and_drafts_the_ticket_into_the_handoff(tmp_path):
    """A drill must never message Telegram or file a real ticket, but it restores its (throwaway)
    sessions for real and points the agent at the cause it planted, so the recovery is exercised."""
    incident = _incident(kind="simulated", incident_id="20260929T120000-simulated")
    incident["evidence"] = ["/tmp/drill/run-models.sh launches every model at once"]
    text = remediate.build_brief(incident, tmp_path, config.load(None))
    assert "rig sysmon restore --incident 20260929T120000-simulated`" in text
    assert re.search(r"tg report [^\n]*--dry-run", text)
    assert "Drill evidence: /tmp/drill/run-models.sh" in text and "do NOT file tickets" in text
    assert "write the ticket you would file into the handoff file" in text


def test_post_crash_brief_names_the_crash(tmp_path):
    incident = _incident(kind="post-crash")
    incident["crash"] = {"reason": "panic", "last_heartbeat": NOW - 30, "panic_file": "/Library/Logs/x.panic",
                         "panic_excerpt": "panic(cpu 3 caller 0xfffff): wired memory exhausted"}
    text = remediate.build_brief(incident, tmp_path, config.load(None))
    assert "/Library/Logs/x.panic" in text and "wired memory exhausted" in text


# The owner's scope (rig-cli#642, decided 2026-09-29): remediation is operational recovery only.
# The root cause in code is ticketed; the unattended agent never ships a code change itself.
SHIPPING_WORDS = re.compile(r"\bPRs?\b|pull request|\bcommit|git push|\bmerge", re.IGNORECASE)


def _brief(kind):
    incident = _incident(kind=kind, incident_id=f"20260929T120000-{kind}")
    if kind == "post-crash":
        incident["crash"] = {"reason": "panic", "last_heartbeat": NOW - 30, "panic_file": "/Library/Logs/x.panic",
                             "panic_excerpt": "panic: watchdog"}
    if kind == "simulated":
        incident["evidence"] = ["/tmp/drill/run-models.sh launches every model at once"]
    # A fixed incident dir: a pytest tmp path would leak the test's own name into the text.
    return remediate.build_brief(incident, Path(f"/sysmon/state/incidents/20260929T120000-{kind}"), config.load(None))


@pytest.mark.parametrize("kind", ["live", "simulated", "post-crash"])
def test_brief_never_asks_for_a_pull_request_or_a_commit(kind):
    text = _brief(kind)
    found = SHIPPING_WORDS.search(text)
    assert found is None, f"the {kind} brief mentions {found.group(0)!r}: {text[max(0, found.start() - 120):found.end() + 60]!r}"


@pytest.mark.parametrize("kind", ["live", "simulated", "post-crash"])
def test_brief_mission_is_operational_recovery_then_one_ticket(kind):
    text = _brief(kind)
    mission = text.split("## Mission", 1)[1].split("## Hard rules", 1)[0]
    for needle in (
        "kill -TERM <pid>",                               # stop leaked processes, SIGTERM first
        "launchctl kickstart -k gui/$(id -u)/<label>",    # restart a hung labelled daemon cleanly
        f"rig sysmon restore --incident 20260929T120000-{kind}",
        "task find",                                      # update an existing ticket before creating one
        "task comment",
        "task new",
        "tg report",
    ):
        assert needle in mission, needle
    assert "do not edit, fix or revert any source code" in mission
    assert "Fix it where it lives" not in text and "fix it for real" not in text
