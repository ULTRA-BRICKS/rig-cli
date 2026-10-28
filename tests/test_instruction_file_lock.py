"""Concurrent rig writers must preserve every block in a shared global guide."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from riglib.actions import run_plan
from riglib.actions.agents_md import ORCHESTRATOR_BEGIN_MARKER
from riglib.actions.harness_settings import INSTRUCTION_POLICY_BEGIN_MARKER
from riglib.drift import detect
from riglib.plan import Action, InstallPlan
from riglib.problem_reporting import BEGIN_MARKER


ROOT = Path(__file__).resolve().parents[1]
CHILD = """
import json, sys
from pathlib import Path
from riglib.actions import run_plan
from riglib.plan import Action, InstallPlan
action = Action(kind=sys.argv[1], category='instructions', item='pi',
                source=Path('.'), target=Path(sys.argv[2]), options={})
print('ready', flush=True)
report = run_plan(InstallPlan(actions=[action]))
print(json.dumps({'errors': [r.detail for r in report.errors], 'changed': report.changed}), flush=True)
"""


def _action(kind, target):
    if kind == "provision_agents_orchestrator":
        target = target.parent
    return Action(kind=kind, category="instructions", item="pi", source=ROOT, target=target, options={})


def _child(kind, target):
    process = subprocess.Popen(
        [sys.executable, "-c", CHILD, kind, str(target)], cwd=ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert process.stdout.readline().strip() == "ready"
    return process


def _finish(process):
    try:
        process.wait(timeout=10)
        # Read through the same TextIOWrapper as the ready handshake; communicate()
        # can miss a result line already buffered by readline() on a fast child.
        stdout, stderr = process.stdout.read(), process.stderr.read()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    assert process.returncode == 0, stderr
    result = json.loads(stdout)
    assert not result["errors"], result
    return result


@pytest.mark.parametrize("first_kind,alias", [
    ("provision_problem_reporting", False), ("provision_instruction_policy", False),
    ("provision_problem_reporting", True), ("provision_instruction_policy", True),
    ("provision_agents_orchestrator", True),
])
def test_concurrent_guide_writers_preserve_both_blocks_and_repeat_is_unchanged(
    tmp_path, monkeypatch, first_kind, alias,
):
    from riglib import problem_reporting

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = Path.home() / ".pi/agent/AGENTS.md"
    target.parent.mkdir(parents=True)
    target.write_text("# User instructions\n")
    other = Path.home() / ".commandcode/AGENTS.md" if alias else target
    if alias:
        other.parent.mkdir()
        other.symlink_to(target)
    second_kind = "provision_instruction_policy" if first_kind == "provision_problem_reporting" else "provision_problem_reporting"
    child = None

    def contend():
        nonlocal child
        child = _child(second_kind, other)
        # The first writer has already read its snapshot. Without a shared lock the
        # second writer finishes here, and the first then overwrites its new block.
        # With the lock the child waits, then reads the first writer's complete result.
        try:
            child.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass

    if first_kind == "provision_problem_reporting":
        original_replace = problem_reporting.os.replace

        def replace(source, destination):
            if Path(destination) == target:
                contend()
            return original_replace(source, destination)

        monkeypatch.setattr(problem_reporting.os, "replace", replace)
    else:
        original_write = Path.write_text

        def write(path, *args, **kwargs):
            if path == target:
                contend()
            return original_write(path, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", write)
    first = _action(first_kind, target)
    report = run_plan(InstallPlan(actions=[first]))
    assert not report.errors
    assert child is not None
    assert _finish(child)["changed"] == 1
    content = target.read_text()
    assert content.startswith("# User instructions\n")
    assert content.count(BEGIN_MARKER) == 1
    other_marker = ORCHESTRATOR_BEGIN_MARKER if first_kind == "provision_agents_orchestrator" else INSTRUCTION_POLICY_BEGIN_MARKER
    assert content.count(other_marker) == 1
    if alias:
        assert other.is_symlink()
    together = InstallPlan(actions=[first, _action(second_kind, other)])
    assert not detect(together).items
    assert run_plan(together).changed == 0
    assert sorted(path.name for path in target.parent.iterdir()) == ["AGENTS.md"]


@pytest.mark.parametrize("first_kind", ["provision_problem_reporting", "provision_instruction_policy"])
def test_failed_guide_writer_releases_lock_for_another_process(tmp_path, monkeypatch, first_kind):
    from riglib import problem_reporting

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = Path.home() / ".pi/agent/AGENTS.md"
    target.parent.mkdir(parents=True)
    target.write_text("# User instructions\n")
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise OSError("injected write failure")

        if first_kind == "provision_problem_reporting":
            patch.setattr(problem_reporting.os, "replace", fail)
        else:
            patch.setattr(Path, "write_text", fail)
        failed = run_plan(InstallPlan(actions=[_action(first_kind, target)]))
        assert len(failed.errors) == 1
        assert "injected write failure" in failed.errors[0].detail
    assert target.read_text() == "# User instructions\n"
    second_kind = "provision_instruction_policy" if first_kind == "provision_problem_reporting" else "provision_problem_reporting"
    assert _finish(_child(second_kind, target))["changed"] == 1
    assert not run_plan(InstallPlan(actions=[_action(first_kind, target)])).errors
    content = target.read_text()
    assert content.count(BEGIN_MARKER) == 1
    assert content.count(INSTRUCTION_POLICY_BEGIN_MARKER) == 1


def test_instruction_lock_uses_central_state_and_times_out_without_blocking_other_files(tmp_path, monkeypatch):
    from riglib.instruction_files import instruction_file_lock

    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    target = Path.home() / ".claude/CLAUDE.md"
    script = """
import sys
from pathlib import Path
from riglib.instruction_files import instruction_file_lock
try:
    with instruction_file_lock(Path(sys.argv[1]), timeout=0.05):
        raise AssertionError('another process already holds this target')
except TimeoutError as exc:
    print(str(exc))
"""
    with instruction_file_lock(target):
        with instruction_file_lock(Path.home() / ".codex/AGENTS.md"):
            result = subprocess.run(
                [sys.executable, "-c", script, str(target)], cwd=ROOT,
                capture_output=True, text=True, timeout=5,
            )
    assert result.returncode == 0, result.stderr
    assert "another rig writer holds" in result.stdout
    assert "re-run apply" in result.stdout
    assert len(list((state / "rig/instruction-locks").glob("*.lock"))) == 2
    assert not target.parent.exists()  # locking must not look like an installed client
    with instruction_file_lock(target, timeout=0.05):
        pass  # the context exit released the kernel lock
