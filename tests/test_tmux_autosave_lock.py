"""The generated tmux autosave wrapper's single-flight lock, exercised by RUNNING the script (#627).

Each test renders ``render_autosave_script`` with a fake tmux binary and a fake resurrect
``save.sh`` under a tmp HOME, plants a fixture lock directory, and runs the real script under
bash: a lock left by a dead holder or older than STALE_SECS must be broken and the save must go
on; a live, young holder must still be respected; a normal run must leave no lock behind.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from riglib import tmux

# Run the script by path, as launchd does: its `#!/bin/bash` shebang is bash 3.2 on macOS.
pytestmark = pytest.mark.skipif(not os.access("/bin/bash", os.X_OK), reason="/bin/bash not available")

STALE_MIN = 45
STALE_SECS = STALE_MIN * 60

_FAKE_TMUX = """#!/bin/bash
# fake tmux: a live server with two real sessions and two panes; no @resurrect-dir override.
case "$1" in
  has-session) exit 0 ;;
  show-option) exit 0 ;;
  list-panes) printf 'p1\\np2\\n' ;;
  list-sessions) printf 'main\\nwork\\n' ;;
esac
exit 0
"""

_FAKE_SAVE = """#!/bin/bash
d="$XDG_DATA_HOME/tmux/resurrect"
mkdir -p "$d"
printf 'pane\\tmain\\t0\\npane\\twork\\t0\\n' > "$d/tmux_resurrect_20260930T120000.txt"
"""


@pytest.fixture
def rig(tmp_path: Path):
    home = tmp_path / "home"
    gen = tmp_path / "gen"
    gen.mkdir(parents=True)
    fake_tmux = tmp_path / "bin" / "tmux"
    fake_tmux.parent.mkdir()
    fake_tmux.write_text(_FAKE_TMUX, encoding="utf-8")
    fake_tmux.chmod(0o755)
    save = home / ".tmux" / "plugins" / "tmux-resurrect" / "scripts" / "save.sh"
    save.parent.mkdir(parents=True)
    save.write_text(_FAKE_SAVE, encoding="utf-8")
    save.chmod(0o755)
    plan = tmux.build_tmux(repo_home=home, generated_dir=gen, autosave={"stale_after": STALE_MIN})
    plan = dataclasses.replace(plan, tmux_bin=str(fake_tmux))
    script = gen / "tmux-autosave.sh"
    script.write_text(plan.render_autosave_script(), encoding="utf-8")
    script.chmod(0o755)
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "LANG": "en_US.UTF-8",
    }

    def run() -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(script)], env=env, capture_output=True, text=True, timeout=30)

    def health() -> dict:
        return json.loads(plan.autosave_health_path.read_text(encoding="utf-8"))

    lock = gen / ".tmux-autosave.lock"
    return SimpleNamespace(run=run, health=health, lock=lock, home=home, env=env)


def _dead_pid() -> int:
    child = subprocess.Popen(["true"])
    child.wait()  # spawned and reaped: its pid names no live process
    return child.pid


def _backdate(path: Path, secs: int) -> None:
    t = time.time() - secs
    os.utime(path, (t, t))


def _break_line(stdout: str) -> str:
    lines = [ln for ln in stdout.splitlines() if "stale lock" in ln]
    assert len(lines) == 1, stdout
    return lines[0]


def test_lock_with_dead_holder_is_broken_and_the_save_runs(rig):
    pid = _dead_pid()
    rig.lock.mkdir()
    (rig.lock / "pid").write_text(f"{pid}\n", encoding="utf-8")

    proc = rig.run()

    assert proc.returncode == 0, proc.stdout + proc.stderr
    line = _break_line(proc.stdout)
    assert str(rig.lock) in line and f"holder pid {pid}" in line and "holder dead" in line
    assert "age " in line
    assert "saved tmux_resurrect_20260930T120000.txt" in proc.stdout
    assert rig.health()["result"] == "ok"
    assert not rig.lock.exists()


def test_legacy_empty_lock_older_than_stale_secs_is_broken(rig):
    rig.lock.mkdir()  # the 2026-09-29 shape: an empty lock dir, no pid file
    _backdate(rig.lock, STALE_SECS + 600)

    proc = rig.run()

    assert proc.returncode == 0, proc.stdout + proc.stderr
    line = _break_line(proc.stdout)
    assert str(rig.lock) in line and "holder pid none" in line and f"older than {STALE_SECS}s" in line
    age = int(line.split("age ")[1].split("s")[0])
    assert age >= STALE_SECS + 600
    assert "saved tmux_resurrect_20260930T120000.txt" in proc.stdout
    assert rig.health()["result"] == "ok"
    assert not rig.lock.exists()


def test_lock_older_than_stale_secs_is_broken_even_with_a_live_holder(rig):
    holder = subprocess.Popen(["sleep", "60"])  # a hung run that outlived STALE_SECS
    try:
        rig.lock.mkdir()
        (rig.lock / "pid").write_text(f"{holder.pid}\n", encoding="utf-8")
        _backdate(rig.lock, STALE_SECS + 1)

        proc = rig.run()
    finally:
        holder.kill()
        holder.wait()

    assert proc.returncode == 0, proc.stdout + proc.stderr
    line = _break_line(proc.stdout)
    assert f"holder pid {holder.pid}" in line and f"older than {STALE_SECS}s" in line
    assert "saved tmux_resurrect_20260930T120000.txt" in proc.stdout
    assert not rig.lock.exists()


def test_live_young_holder_is_respected(rig):
    holder = subprocess.Popen(["sleep", "60"])
    try:
        rig.lock.mkdir()
        (rig.lock / "pid").write_text(f"{holder.pid}\n", encoding="utf-8")
        before = rig.lock.stat().st_mtime

        proc = rig.run()

        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "busy: lock held, skipping" in proc.stdout
        assert "stale lock" not in proc.stdout and "saved" not in proc.stdout
        assert rig.health()["result"] == "busy"
        assert (rig.lock / "pid").read_text(encoding="utf-8").strip() == str(holder.pid)
        assert rig.lock.stat().st_mtime == before
        assert not Path(f"{rig.lock}.break").exists()
    finally:
        holder.kill()
        holder.wait()


def test_young_empty_lock_is_respected(rig):
    # a holder between its mkdir and its pid write: no pid yet, but far younger than STALE_SECS.
    rig.lock.mkdir()

    proc = rig.run()

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "busy: lock held, skipping" in proc.stdout
    assert rig.lock.is_dir()


def test_normal_run_leaves_no_lock_behind(rig):
    proc = rig.run()

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "saved tmux_resurrect_20260930T120000.txt" in proc.stdout
    assert "stale lock" not in proc.stdout
    assert not rig.lock.exists()
    assert not Path(f"{rig.lock}.break").exists()


def test_holder_records_its_pid_inside_the_lock(rig, tmp_path):
    # save.sh runs while the lock is held: snapshot the lock's pid file from inside the run.
    spy = rig.home / ".tmux" / "plugins" / "tmux-resurrect" / "scripts" / "save.sh"
    spy.write_text(
        _FAKE_SAVE + f'cat {rig.lock}/pid > {tmp_path}/seen-pid; echo "$PPID" > {tmp_path}/runner-pid\n',
        encoding="utf-8",
    )

    proc = rig.run()

    assert proc.returncode == 0, proc.stdout + proc.stderr
    seen = (tmp_path / "seen-pid").read_text(encoding="utf-8").strip()
    assert seen == (tmp_path / "runner-pid").read_text(encoding="utf-8").strip()
    assert not rig.lock.exists()


def test_leaked_breaker_mutex_does_not_block_breaking_forever(rig):
    # a breaker SIGKILLed mid-break leaked `<lock>.break`; with a stale lock behind it the next run
    # must still reclaim the mutex, break the lock and save — never skip forever again.
    breaker = Path(f"{rig.lock}.break")
    breaker.mkdir()
    _backdate(breaker, 120)
    rig.lock.mkdir()
    (rig.lock / "pid").write_text(f"{_dead_pid()}\n", encoding="utf-8")

    proc = rig.run()

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "holder dead" in _break_line(proc.stdout)
    assert "saved tmux_resurrect_20260930T120000.txt" in proc.stdout
    assert not rig.lock.exists() and not breaker.exists()


def test_breaker_in_progress_is_respected(rig):
    # a fresh `<lock>.break` is another run breaking the lock right now: stand back, touch nothing.
    breaker = Path(f"{rig.lock}.break")
    breaker.mkdir()
    rig.lock.mkdir()
    (rig.lock / "pid").write_text(f"{_dead_pid()}\n", encoding="utf-8")

    proc = rig.run()

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "busy: lock held, skipping" in proc.stdout and "stale lock" not in proc.stdout
    assert rig.lock.is_dir() and breaker.is_dir()


def test_stat_leaking_text_to_stdout_cannot_crash_the_run(rig, tmp_path):
    # GNU `stat -f %m FILE` prints FILE's filesystem block to STDOUT and exits non-zero; if that text
    # reached `$(( ))` under `set -u` the run would die with `File: unbound variable`. A `stat` that
    # leaks for both forms must degrade to "age unknown", never crash the lock or the snapshot check.
    fake_bin = tmp_path / "leaky-bin"
    fake_bin.mkdir()
    leaky = fake_bin / "stat"
    leaky.write_text('#!/bin/bash\nprintf \'  File: "%s"\\n\' "${@: -1}"\nexit 1\n', encoding="utf-8")
    leaky.chmod(0o755)
    rig.env["PATH"] = f"{fake_bin}:{rig.env['PATH']}"
    rig.lock.mkdir()  # an empty lock whose age can't be read: treated as stale, broken

    proc = rig.run()

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "unbound variable" not in proc.stderr
    assert "holder pid none" in _break_line(proc.stdout)
    assert "saved tmux_resurrect_20260930T120000.txt" in proc.stdout
