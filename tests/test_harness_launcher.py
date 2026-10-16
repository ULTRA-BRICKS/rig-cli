"""`riglib.harness_launcher` — hosting a harness in abduco, and the opt-in PATH wrapper.

The process-replacing paths (``run_session`` / ``attach_session`` / ``exec_or_wrap``) are driven
through an injected ``exec_fn`` for argv/env assembly. The integration class at the bottom uses a
REAL abduco on a private socket directory (``RIG_OMP_ABDUCO_DIR``) with throwaway session names and
a fake harness executable — never a shared or live abduco instance.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from shutil import which
from types import SimpleNamespace

import pytest

from riglib import harness_launcher as hl
from riglib import omp_inspect
from riglib import signal_scope as ss
from riglib.harness_host import ENV_SPEC, HostSpec
from riglib.harness_sessions import omp_agent_root

_REAL_SIGNAL_SCOPE_ENABLED = hl.wrapper_provision.signal_scope_enabled


@pytest.fixture(autouse=True)
def _no_signal_scope(monkeypatch):
    # The default config contains omp on Linux; the launch-shape tests below assert the bare
    # harness argv, so they run with the scope decided off. The signal-scope tests opt back in.
    monkeypatch.setattr(hl.wrapper_provision, "signal_scope_enabled", lambda kind, env, **kw: False)

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Never let a test create state under the real home: abduco sockets, the real-bin config and
    tg-ctl's hosts directory all live under tmp_path."""
    monkeypatch.setenv("RIG_OMP_ABDUCO_DIR", str(tmp_path / "abduco"))
    monkeypatch.setenv("RIG_CONFIG_DIR", str(tmp_path / "rigcfg"))
    monkeypatch.setenv("TG_CTL_CONFIG_DIR", str(tmp_path / "tg"))


def _script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


# ── argv / env assembly ──────────────────────────────────────────────────────────────────────
def test_run_argv_is_connect_or_create_around_the_host_command():
    argv = hl.build_run_argv("omp-x1", abduco_bin="abduco")
    assert argv[:4] == ["abduco", "-f", "-A", "omp-x1"]
    assert argv[-3:] == ["harness", "host", "omp-x1"]
    assert argv[4:] == [*hl.host_command("omp-x1")]


def test_detached_run_argv_creates_without_attaching():
    assert hl.build_run_argv("omp-x1", detach=True)[:4] == ["abduco", "-f", "-n", "omp-x1"]


def test_harness_command_line_never_appears_in_the_abduco_argv(tmp_path):
    """tg-ctl identifies agents by argv (`claude …`, `…/omp`); an abduco server whose argv carried
    the harness would be mistaken for the agent. The harness travels in RIG_HOST_SPEC instead."""
    calls = []
    hl.run_session(
        "claude",
        ["--model", "opus", "hello world"],
        real_bin="/opt/claude/bin/claude",
        host_id="claude-x1",
        exec_fn=lambda path, argv, env: calls.append((path, argv, env)),
    )
    _path, argv, _env = calls[0]
    joined = " ".join(argv)
    for leaked in ("claude-code", "/opt/claude/bin/claude", "opus", "hello world"):
        assert leaked not in joined
    assert argv[-3:] == ["harness", "host", "claude-x1"]


@pytest.mark.parametrize("name", ["../escape", "/absolute", "--flag", "x\ny", "", "a b"])
def test_session_names_cannot_escape_the_socket_namespace_or_become_flags(name):
    with pytest.raises(ValueError):
        hl.build_run_argv(name)
    with pytest.raises(ValueError):
        hl.build_attach_argv(name)


def test_attach_argv():
    assert hl.build_attach_argv("omp-x1") == ["abduco", "-a", "omp-x1"]
    assert hl.build_attach_argv("omp-x1", abduco_bin="/opt/abduco") == ["/opt/abduco", "-a", "omp-x1"]


def test_run_env_points_abduco_at_the_private_socket_dir_and_carries_the_spec(tmp_path):
    spec = HostSpec("omp", ["/bin/omp", "--resume", "/s.jsonl"], "/work", "lbl")
    env = hl.run_env(spec, base_env={"PATH": "/bin"})
    assert env["PATH"] == "/bin"
    assert env["ABDUCO_SOCKET_DIR"] == str(tmp_path / "abduco")
    assert HostSpec.from_env(env[ENV_SPEC]) == spec


def test_run_env_without_a_spec_adds_no_spec_and_no_legacy_marker():
    env = hl.run_env(base_env={"PATH": "/bin"})
    assert ENV_SPEC not in env and "RIG_OMP_SESSION" not in env
    assert "ABDUCO_SOCKET_DIR" in env


# ── run_session ──────────────────────────────────────────────────────────────────────────────
def test_run_session_generates_a_kind_prefixed_name_and_hands_spec_to_abduco(tmp_path):
    calls = []
    name = hl.run_session(
        "omp",
        ["--resume", "/x.jsonl"],
        label="work",
        real_bin="/opt/omp",
        cwd="/proj",
        exec_fn=lambda path, argv, env: calls.append((path, argv, env)),
    )
    assert name.startswith("omp-") and len(name) == len("omp-") + 12
    (path, argv, env), = calls
    assert path == "abduco"
    assert argv[:4] == ["abduco", "-f", "-A", name]
    assert HostSpec.from_env(env[ENV_SPEC]) == HostSpec("omp", ["/opt/omp", "--resume", "/x.jsonl"], "/proj", "work")
    assert env["ABDUCO_SOCKET_DIR"] == str(tmp_path / "abduco")


def test_run_session_honors_an_explicit_host_id():
    calls = []
    name = hl.run_session(
        "omp", [], host_id="omp-fixed", real_bin="/opt/omp", exec_fn=lambda p, a, e: calls.append(a)
    )
    assert name == "omp-fixed" and calls[0][3] == "omp-fixed"


def test_run_session_defaults_cwd_to_the_callers(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    calls = []
    hl.run_session("omp", [], real_bin="/opt/omp", exec_fn=lambda p, a, e: calls.append(e))
    assert HostSpec.from_env(calls[0][ENV_SPEC]).cwd == os.getcwd()


def test_claude_launch_is_given_a_session_id_up_front():
    calls = []
    hl.run_session("claude", [], real_bin="/opt/claude", exec_fn=lambda p, a, e: calls.append(e))
    spec = HostSpec.from_env(calls[0][ENV_SPEC])
    assert spec.session and spec.session.id
    assert spec.argv[-2:] == ["--session-id", spec.session.id]


@pytest.mark.parametrize("bad", ["../bad", "--flag", "a/b"])
def test_run_session_rejects_an_invalid_host_id_before_touching_abduco(bad):
    called = []
    with pytest.raises(ValueError):
        hl.run_session("omp", [], host_id=bad, real_bin="/opt/omp", exec_fn=lambda *a: called.append(a))
    assert called == []


@pytest.mark.parametrize("name", ["mysession", "claude-0123456789ab", "omp"])
def test_run_session_requires_the_host_name_to_start_with_its_harness(name):
    """Pane recognition reads the harness from the name's prefix; a bare name would make the pane
    silently unsaveable."""
    with pytest.raises(ValueError, match="must start with 'omp-'"):
        hl.run_session("omp", [], real_bin="/opt/omp", host_id=name, exec_fn=lambda *_: None)


def test_the_wrapper_appends_fallback_dirs_so_the_callers_path_order_wins():
    path_line = next(l for l in hl.wrapper_text("claude").splitlines() if l.startswith("export PATH="))
    assert path_line.startswith('export PATH="${PATH:-}:')


def test_run_session_rejects_an_unknown_harness():
    with pytest.raises(ValueError):
        hl.run_session("vim", [], real_bin="/bin/vim", exec_fn=lambda *a: None)


def test_run_session_refuses_a_real_bin_that_is_the_rig_wrapper(tmp_path):
    wrapper = tmp_path / "omp"
    wrapper.write_text(hl.wrapper_text("omp"))
    wrapper.chmod(0o755)
    called = []
    with pytest.raises(RuntimeError, match="wrapper"):
        hl.run_session("omp", [], real_bin=str(wrapper), exec_fn=lambda *a: called.append(a))
    assert called == []


def test_run_session_creates_the_socket_dir_private(tmp_path):
    hl.run_session("omp", [], real_bin="/opt/omp", exec_fn=lambda *a: None)
    assert (tmp_path / "abduco").stat().st_mode & 0o777 == 0o700


def test_detached_run_creates_with_n_and_returns_the_name_without_exec():
    seen = []

    def run_fn(argv, **kwargs):
        seen.append((argv, kwargs))
        return SimpleNamespace(returncode=0)

    name = hl.run_session(
        "codex",
        [],
        host_id="codex-d1",
        real_bin="/opt/codex",
        detach=True,
        run_fn=run_fn,
        exec_fn=lambda *a: pytest.fail("a detached run must not exec"),
    )
    assert name == "codex-d1"
    (argv, kwargs), = seen
    assert argv[:4] == ["abduco", "-f", "-n", "codex-d1"]
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert ENV_SPEC in kwargs["env"]


def test_detached_run_reports_an_abduco_failure():
    with pytest.raises(RuntimeError, match="abduco could not create session omp-d2 \\(exit 3\\)"):
        hl.run_session(
            "omp",
            [],
            host_id="omp-d2",
            real_bin="/opt/omp",
            detach=True,
            run_fn=lambda argv, **kw: SimpleNamespace(returncode=3),
        )


def test_missing_abduco_surfaces_as_file_not_found():
    with pytest.raises(FileNotFoundError):
        hl.run_session("omp", [], real_bin="/opt/omp", abduco_bin="/no/such/abduco", detach=True)


# ── attach ───────────────────────────────────────────────────────────────────────────────────
def test_attach_session_execs_abduco_attach_on_the_private_socket_dir(tmp_path):
    calls = []
    hl.attach_session("omp-x1", exec_fn=lambda p, a, e: calls.append((p, a, e)))
    (path, argv, env), = calls
    assert path == "abduco" and argv == ["abduco", "-a", "omp-x1"]
    assert env["ABDUCO_SOCKET_DIR"] == str(tmp_path / "abduco")


def test_attach_session_rejects_an_invalid_name_without_exec():
    with pytest.raises(ValueError):
        hl.attach_session("../bad", exec_fn=lambda *a: pytest.fail("must not exec"))


# ── list ─────────────────────────────────────────────────────────────────────────────────────
def test_list_sessions_joins_abduco_sessions_with_their_tg_ctl_records(tmp_path):
    log = tmp_path / "abduco.log"
    fake = _script(
        tmp_path / "abduco",
        f'echo "$ABDUCO_SOCKET_DIR $*" >> {shlex.quote(str(log))}\n'
        "printf 'Active sessions\\n'\n"
        "printf '*\\tSun Sep 27 12:00:00 2026\\tomp-aaa\\n'\n"
        "printf '+\\tSun Sep 27 12:01:00 2026\\tclaude-bbb\\n'\n"
        "printf ' \\tSun Sep 27 12:02:00 2026\\tunregistered\\n'\n",
    )
    hosts = tmp_path / "tg" / "hosts"
    hosts.mkdir(parents=True)
    (hosts / "omp-aaa.json").write_text(json.dumps({"harness": "omp", "label": "work", "cwd": "/proj", "socket": "/s"}))
    (hosts / "claude-bbb.json").write_text("{not json")

    sessions, ok = hl.list_sessions(abduco_bin=str(fake))

    assert ok
    by_name = {s["name"]: s for s in sessions}
    assert by_name["omp-aaa"] == {
        "name": "omp-aaa",
        "attached": True,
        "terminated": False,
        "created": "Sun Sep 27 12:00:00 2026",
        "harness": "omp",
        "label": "work",
        "cwd": "/proj",
    }
    assert by_name["claude-bbb"]["terminated"] is True and "harness" not in by_name["claude-bbb"]
    assert "harness" not in by_name["unregistered"]
    assert log.read_text().strip() == f"{tmp_path / 'abduco'} -l"


def test_list_sessions_reports_inspection_failure_not_an_empty_list(tmp_path):
    failing = _script(tmp_path / "abduco", "exit 1\n")
    assert hl.list_sessions(abduco_bin=str(failing)) == ([], False)
    assert hl.list_sessions(abduco_bin=str(tmp_path / "missing")) == ([], False)


# ── which binary is the real one ─────────────────────────────────────────────────────────────
def _wrapper_file(path: Path, kind: str = "omp") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(hl.wrapper_text(kind))
    path.chmod(0o755)
    return path


def test_real_bin_config_path_follows_rig_config_dir_override(tmp_path):
    assert hl.real_bin_config_path({"RIG_CONFIG_DIR": str(tmp_path)}) == tmp_path / "harness-real-bin.json"
    assert hl.real_bin_config_path({}).parts[-3:] == (".config", "rig", "harness-real-bin.json")


def test_resolve_real_bin_skips_managed_wrappers_on_path(tmp_path):
    _wrapper_file(tmp_path / "wrap" / "omp")
    real = _script(tmp_path / "real" / "omp", "exit 0\n")
    env = {"PATH": f"{tmp_path / 'wrap'}{os.pathsep}{tmp_path / 'real'}"}
    assert hl.resolve_real_bin("omp", env) == str(real)


def test_resolve_real_bin_prefers_the_recorded_path_over_path_search(tmp_path):
    on_path = _script(tmp_path / "bin" / "omp", "exit 0\n")
    recorded = _script(tmp_path / "fork" / "omp", "exit 0\n")
    env = {"PATH": str(on_path.parent), "RIG_CONFIG_DIR": str(tmp_path / "cfg")}
    hl.install_wrapper("omp", tmp_path / "wrapdir" / "omp", real_bin=str(recorded), env=env)
    assert hl.resolve_real_bin("omp", env) == str(recorded.resolve())


def test_resolve_real_bin_refuses_a_recorded_wrapper(tmp_path):
    wrapper = _wrapper_file(tmp_path / "wrapper")
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "harness-real-bin.json").write_text(json.dumps({"omp": str(wrapper)}))
    with pytest.raises(RuntimeError, match="wrapper"):
        hl.resolve_real_bin("omp", {"RIG_CONFIG_DIR": str(cfg), "PATH": ""})


def test_resolve_real_bin_fails_when_only_wrappers_exist(tmp_path):
    _wrapper_file(tmp_path / "wrap" / "claude", "claude")
    with pytest.raises(RuntimeError, match="no real claude"):
        hl.resolve_real_bin("claude", {"PATH": str(tmp_path / "wrap")})


def test_resolve_real_bin_ignores_non_executable_files(tmp_path):
    plain = tmp_path / "bin" / "codex"
    plain.parent.mkdir()
    plain.write_text("not executable")
    with pytest.raises(RuntimeError):
        hl.resolve_real_bin("codex", {"PATH": str(plain.parent)})


@pytest.mark.parametrize("value", [None, [], 42, "text"])
def test_a_non_object_real_bin_config_records_nothing(tmp_path, value):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(value))
    assert hl.read_real_bins(path) == {}


def test_real_bin_config_keeps_only_known_harness_entries(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"omp": "/a/omp", "vim": "/a/vim", "claude": 7, "codex": ""}))
    assert hl.read_real_bins(path) == {"omp": "/a/omp"}
    assert hl.read_real_bins(tmp_path / "missing.json") == {}


# ── the wrapper ──────────────────────────────────────────────────────────────────────────────
def test_wrapper_text_is_a_marked_bash_script_that_execs_harness_exec(tmp_path):
    text = hl.wrapper_text("claude")
    assert text.startswith("#!/usr/bin/env bash\n")
    assert hl.WRAPPER_MARKER in text
    assert text.rstrip().splitlines()[-1].endswith(" harness exec claude -- \"$@\"")
    script = tmp_path / "w"
    script.write_text(text)
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0
    with pytest.raises(ValueError):
        hl.wrapper_text("vim")


def test_wrapper_state_distinguishes_missing_current_stale_and_foreign(tmp_path):
    target = tmp_path / "omp"
    assert hl.wrapper_state("omp", target) == "missing"
    target.write_text(hl.wrapper_text("omp"))
    assert hl.wrapper_state("omp", target) == "current"
    target.write_text(hl.wrapper_text("omp") + "# drifted\n")
    assert hl.wrapper_state("omp", target) == "stale"
    target.write_bytes(b"\xcf\xfa\xed\xfe native")
    assert hl.wrapper_state("omp", target) == "foreign"
    # a wrapper written for another harness is ours but not current for this one.
    target.write_text(hl.wrapper_text("claude"))
    assert hl.wrapper_state("omp", target) == "stale"
    link = tmp_path / "link"
    link.symlink_to(target)
    assert hl.wrapper_state("omp", link) == "foreign"


def test_install_wrapper_writes_the_script_and_records_the_real_bin(tmp_path):
    real = _script(tmp_path / "real-omp", "echo real\n")
    original = real.read_text()
    target = tmp_path / "bin" / "omp"
    env = {"RIG_CONFIG_DIR": str(tmp_path / "cfg")}

    assert hl.install_wrapper("omp", target, real_bin=str(real), env=env) is True

    assert target.read_text() == hl.wrapper_text("omp")
    assert target.stat().st_mode & 0o777 == 0o755
    config = tmp_path / "cfg" / "harness-real-bin.json"
    assert json.loads(config.read_text()) == {"omp": str(real.resolve())}
    assert config.stat().st_mode & 0o777 == 0o600
    assert real.read_text() == original  # the vendor binary itself is untouched


def test_install_wrapper_is_idempotent_and_repairs_a_stale_wrapper(tmp_path):
    real = _script(tmp_path / "real", "exit 0\n")
    target = tmp_path / "omp"
    env = {"RIG_CONFIG_DIR": str(tmp_path / "cfg")}
    assert hl.install_wrapper("omp", target, real_bin=str(real), env=env) is True
    assert hl.install_wrapper("omp", target, real_bin=str(real), env=env) is False
    target.write_text(hl.wrapper_text("omp") + "# stale\n")
    assert hl.install_wrapper("omp", target, real_bin=str(real), env=env) is True
    assert target.read_text() == hl.wrapper_text("omp")


def test_the_superseded_omp_wrapper_is_skipped_on_path_and_replaced_not_refused(tmp_path):
    """The cutover: `rig omp install-wrapper` scripts (marker `durable OMP wrapper`) call the deleted
    `rig omp exec`. They must never be taken for the real omp, and install_wrapper must rewrite them."""
    old = tmp_path / "old" / "omp"
    old.parent.mkdir()
    old.write_text("#!/usr/bin/env bash\n# rig-managed: durable OMP wrapper — installed by `rig omp install-wrapper`.\nexec rig omp exec -- \"$@\"\n")
    old.chmod(0o755)
    real = _script(tmp_path / "real" / "omp", "exit 0\n")
    assert hl.resolve_real_bin("omp", {"PATH": f"{old.parent}{os.pathsep}{real.parent}"}) == str(real)
    assert hl.wrapper_state("omp", old) == "stale"
    env = {"RIG_CONFIG_DIR": str(tmp_path / "cfg")}
    assert hl.install_wrapper("omp", old, real_bin=str(real), env=env) is True
    assert old.read_text() == hl.wrapper_text("omp")


def test_install_wrapper_records_each_harness_independently(tmp_path):
    omp, claude = _script(tmp_path / "o", ""), _script(tmp_path / "c", "")
    env = {"RIG_CONFIG_DIR": str(tmp_path / "cfg")}
    hl.install_wrapper("omp", tmp_path / "bin" / "omp", real_bin=str(omp), env=env)
    hl.install_wrapper("claude", tmp_path / "bin" / "claude", real_bin=str(claude), env=env)
    assert hl.read_real_bins(tmp_path / "cfg" / "harness-real-bin.json") == {
        "omp": str(omp.resolve()),
        "claude": str(claude.resolve()),
    }


def test_install_wrapper_without_a_real_bin_writes_only_the_script(tmp_path):
    target = tmp_path / "bin" / "codex"
    assert hl.install_wrapper("codex", target, env={"RIG_CONFIG_DIR": str(tmp_path / "cfg")}) is True
    assert target.read_text() == hl.wrapper_text("codex")
    assert not (tmp_path / "cfg").exists()


def test_install_wrapper_refuses_an_existing_native_binary_without_decoding_or_writing(tmp_path):
    target = tmp_path / "omp"
    original = b"\xcf\xfa\xed\xfe\xffNATIVE"
    target.write_bytes(original)
    vendor = _script(tmp_path / "vendor", "exit 0\n")
    env = {"RIG_CONFIG_DIR": str(tmp_path / "cfg")}
    with pytest.raises(ValueError, match="unmanaged"):
        hl.install_wrapper("omp", target, real_bin=str(vendor), env=env)
    assert target.read_bytes() == original
    assert not (tmp_path / "cfg").exists()


def test_install_wrapper_refuses_an_unmanaged_symlink_without_touching_vendor(tmp_path):
    vendor = tmp_path / "vendor"
    vendor.write_text("VENDOR")
    target = tmp_path / "omp"
    target.symlink_to(vendor)
    with pytest.raises(ValueError):
        hl.install_wrapper("omp", target, real_bin=str(vendor), env={"RIG_CONFIG_DIR": str(tmp_path / "cfg")})
    assert vendor.read_text() == "VENDOR" and target.is_symlink()


def test_install_wrapper_rejects_a_bad_real_bin_before_writing_anything(tmp_path):
    target = tmp_path / "omp"
    env = {"RIG_CONFIG_DIR": str(tmp_path / "cfg")}
    wrapper = _wrapper_file(tmp_path / "other-wrapper")
    for bad in ("relative/omp", str(tmp_path / "missing"), str(target), str(wrapper)):
        if bad == str(target):
            target.write_text("")  # exists, but is the wrapper's own path
        with pytest.raises(ValueError):
            hl.install_wrapper("omp", target, real_bin=bad, env=env)
        target.unlink(missing_ok=True)
    assert not (tmp_path / "cfg").exists()


def test_installed_wrapper_passes_a_headless_call_straight_to_the_real_binary(tmp_path):
    """End to end: the written script → `rig harness exec` → the recorded real binary, unhosted."""
    real = _script(tmp_path / "real-omp", 'echo "REAL:$*"\n')
    target = tmp_path / "bin" / "omp"
    env = dict(os.environ, RIG_CONFIG_DIR=str(tmp_path / "cfg"))
    hl.install_wrapper("omp", target, real_bin=str(real), env=env)
    done = subprocess.run(
        [str(target), "-p", "hello"], env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == "REAL:-p hello\n"


# ── exec_or_wrap ─────────────────────────────────────────────────────────────────────────────
def _recorded_env(tmp_path: Path, kind: str = "omp", real: str | None = None) -> dict[str, str]:
    real_path = real or str(_script(tmp_path / "real" / kind, "exit 0\n"))
    env = {"RIG_CONFIG_DIR": str(tmp_path / "cfg"), "PATH": "/usr/bin:/bin"}
    hl.install_wrapper(kind, tmp_path / "wrapbin" / kind, real_bin=real_path, env=env)
    return env


@pytest.mark.parametrize(
    "kind,args",
    [("omp", ["-p", "hello"]), ("claude", ["-p", "hi"]), ("codex", ["exec", "fix it"]), ("opencode", ["run", "x"])],
)
def test_exec_or_wrap_passes_headless_invocations_through_unhosted(tmp_path, kind, args):
    env = _recorded_env(tmp_path, kind)
    real = hl.resolve_real_bin(kind, env)
    calls = []
    hl.exec_or_wrap(kind, args, env=env, interactive_tty=True, exec_fn=lambda p, a, e: calls.append((p, a, e)))
    assert calls == [(real, [real, *args], env)]


def test_exec_or_wrap_passes_an_interactive_launch_through_when_there_is_no_tty(tmp_path):
    env = _recorded_env(tmp_path)
    real = hl.resolve_real_bin("omp", env)
    calls = []
    hl.exec_or_wrap("omp", ["--resume", "/x.jsonl"], env=env, interactive_tty=False, exec_fn=lambda p, a, e: calls.append((p, a)))
    assert calls == [(real, [real, "--resume", "/x.jsonl"])]


def test_exec_or_wrap_hosts_an_interactive_launch_in_abduco(tmp_path):
    env = _recorded_env(tmp_path)
    real = hl.resolve_real_bin("omp", env)
    calls = []
    hl.exec_or_wrap(
        "omp", ["--resume", "/x.jsonl"], env=env, interactive_tty=True, exec_fn=lambda p, a, e: calls.append((p, a, e))
    )
    (path, argv, run_env), = calls
    assert path == "abduco" and argv[:3] == ["abduco", "-f", "-A"] and argv[3].startswith("omp-")
    assert real not in argv
    spec = HostSpec.from_env(run_env[ENV_SPEC])
    assert spec.argv == [real, "--resume", "/x.jsonl"] and spec.harness == "omp"


def test_exec_or_wrap_hosts_a_bare_claude_launch_with_an_assigned_session(tmp_path):
    env = _recorded_env(tmp_path, "claude")
    calls = []
    hl.exec_or_wrap("claude", [], env=env, interactive_tty=True, exec_fn=lambda p, a, e: calls.append(e))
    spec = HostSpec.from_env(calls[0][ENV_SPEC])
    assert spec.harness == "claude" and spec.session and spec.session.id


def test_exec_or_wrap_refuses_a_recorded_real_bin_that_points_back_at_a_wrapper(tmp_path):
    wrapper = _wrapper_file(tmp_path / "wrapper")
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg" / "harness-real-bin.json").write_text(json.dumps({"omp": str(wrapper)}))
    execute = []
    with pytest.raises(RuntimeError):
        hl.exec_or_wrap(
            "omp",
            [],
            env={"RIG_CONFIG_DIR": str(tmp_path / "cfg"), "PATH": ""},
            interactive_tty=True,
            exec_fn=lambda *a: execute.append(a),
        )
    assert execute == []


def test_exec_or_wrap_raises_without_any_real_binary(tmp_path):
    with pytest.raises(RuntimeError):
        hl.exec_or_wrap(
            "omp", ["--resume", "/x"], env={"RIG_CONFIG_DIR": str(tmp_path / "none"), "PATH": ""}, interactive_tty=True
        )


# ── omp's agent dir (where its terminal-session breadcrumbs live) ────────────────────────────
def test_default_omp_agent_root():
    home = Path("/home/u")
    assert omp_agent_root({}, home) == home / ".omp" / "agent"


def test_pi_coding_agent_dir_is_a_full_override():
    assert omp_agent_root({"PI_CODING_AGENT_DIR": "/custom/root"}, Path("/home/u")) == Path("/custom/root")


def test_pi_config_dir_renames_dot_omp():
    home = Path("/home/u")
    assert omp_agent_root({"PI_CONFIG_DIR": "~/.myomp"}, home) == home / ".myomp" / "agent"


def test_a_relative_agent_dir_is_relative_to_home():
    home = Path("/home/u")
    assert omp_agent_root({"PI_CODING_AGENT_DIR": "state/omp"}, home) == home / "state" / "omp"
    assert omp_agent_root({"PI_CODING_AGENT_DIR": "~"}, home) == home


# ── real abduco ──────────────────────────────────────────────────────────────────────────────
def _wait_for(predicate, what: str, timeout: float = 15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    pytest.fail(f"timed out waiting for {what}")


@pytest.mark.skipif(which("abduco") is None, reason="abduco not installed")
class TestRealAbduco:
    """A real abduco server on a private socket dir, hosting a fake harness under the real
    `rig harness host` supervisor. Session names are throwaway; nothing shared is touched."""

    @pytest.fixture(autouse=True)
    def private_abduco(self, monkeypatch, tmp_path):
        # a unix socket path is limited to ~104 bytes on macOS — pytest's tmp dirs blow it.
        short = Path(tempfile.mkdtemp(prefix="rhl-", dir="/tmp"))
        monkeypatch.setenv("RIG_OMP_ABDUCO_DIR", str(short))
        self.abduco_dir = short
        self.hosts = tmp_path / "tg" / "hosts"
        self.state = tmp_path / "state"
        self.state.mkdir()
        self.extra_pids: list[int] = []
        yield
        pids = list(self.extra_pids)
        for record in self.hosts.glob("*.json"):
            try:
                data = json.loads(record.read_text())
            except (OSError, ValueError):
                continue
            pids += [data.get(k) for k in ("serverPid", "supervisorPid", "harnessPid") if isinstance(data.get(k), int)]
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        subprocess.run(["rm", "-rf", str(short)], check=False)

    def _rig(self, *argv: str, **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "riglib", "harness", *argv],
            cwd=REPO,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
            **kwargs,
        )

    def _pids(self, path: Path) -> list[int]:
        return [int(x) for x in path.read_text().split()] if path.exists() else []

    def test_attach_reuses_the_same_harness_process(self, monkeypatch):
        pidfile = self.state / "pids"
        fake = _script(self.state / "fake-omp", f'echo $$ >> {shlex.quote(str(pidfile))}\nexec sleep 60\n')

        done = self._rig("run", "omp", "--detach", "--host-id", "omp-it-attach", "--name", "it", "--real-bin", str(fake))
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip() == "omp-it-attach"
        first = _wait_for(lambda: self._pids(pidfile), "the fake harness to start")
        self.extra_pids += first

        sessions, ok = hl.list_sessions()
        assert ok and "omp-it-attach" in {s["name"] for s in sessions}
        listed = self._rig("list", "--json")
        assert listed.returncode == 0, listed.stderr
        assert "omp-it-attach" in {s["name"] for s in json.loads(listed.stdout)}

        attacher = subprocess.Popen(
            [sys.executable, "-m", "riglib", "harness", "attach", "omp-it-attach"],
            cwd=REPO,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(0.5)
            assert self._pids(pidfile) == first, "attaching must not relaunch the harness"
        finally:
            attacher.terminate()
            attacher.wait()

    def test_the_session_is_registered_for_tg_ctl_with_the_label(self):
        fake = _script(self.state / "fake-omp", "exec sleep 60\n")
        name = hl.run_session("omp", [], host_id="omp-it-reg", label="proof", real_bin=str(fake), cwd=str(self.state), detach=True)
        record = _wait_for(
            lambda: (self.hosts / f"{name}.json").exists() and json.loads((self.hosts / f"{name}.json").read_text()),
            "the tg-ctl registry record",
        )
        assert record["harness"] == "omp" and record["label"] == "proof" and record["cwd"] == str(self.state)
        assert record["role"] == "coordinator"
        listed = {s["name"]: s for s in _wait_for(lambda: hl.list_sessions()[0], "abduco listing")}
        assert listed[name]["label"] == "proof"

    def test_a_restore_decision_attaches_to_the_live_hosted_session(self):
        """A saved pane whose hosted session is still alive must ATTACH, never resume/relaunch."""
        from riglib.omp_recovery import OriginalSession, SavedPane, plan_restore

        pidfile = self.state / "pids"
        fake = _script(self.state / "fake-omp", f'echo $$ >> {shlex.quote(str(pidfile))}\nexec sleep 60\n')
        hl.run_session("omp", [], host_id="omp-it-restore", real_bin=str(fake), detach=True)
        pid = _wait_for(lambda: self._pids(pidfile), "the fake harness to start")[0]
        self.extra_pids.append(pid)

        durable_sockets, ok = omp_inspect.list_abduco_sessions()
        assert ok
        plan = plan_restore(
            SavedPane("addr", "/proj", "omp-it-restore", str(pidfile), "omp-it-restore"),
            [OriginalSession(pid, "instance-x", "/proj", "omp-it-restore", "omp-it-restore")],
            durable_sockets,
            inspection_ok=True,
            session_file_exists=True,
            pane_is_empty_shell=True,
        )
        assert plan.action == "attach" and plan.argv[-1] == "omp-it-restore"

    def test_a_killed_omp_is_relaunched_inside_the_same_session_on_the_same_conversation(self):
        """The 2026-09-30 incident: jetsam SIGKILLed a hosted OMP and abduco reported "exit status
        0". The supervisor inside the session must relaunch it in the SAME abduco session,
        resuming the transcript OMP's own terminal breadcrumb names."""
        state = self.state / "runs.json"
        transcript = self.state / "live.jsonl"
        transcript.write_text('{"type":"session","id":"s1"}\n')
        fake = self.state / "fake_omp.py"
        fake.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys, time\n"
            "from pathlib import Path\n"
            "state = Path(os.environ['FAKE_OMP_STATE'])\n"
            "runs = json.loads(state.read_text()) if state.exists() else []\n"
            "runs.append({'pid': os.getpid(), 'ppid': os.getppid(), 'argv': sys.argv[1:]})\n"
            "state.write_text(json.dumps(runs))\n"
            "tty = os.ttyname(0)[len('/dev/'):].replace('/', '-')\n"
            "crumb = Path(os.environ['PI_CODING_AGENT_DIR']) / 'terminal-sessions' / tty\n"
            "crumb.parent.mkdir(parents=True, exist_ok=True)\n"
            "crumb.write_text(os.getcwd() + '\\n' + os.environ['FAKE_OMP_SESSION'] + '\\n')\n"
            "time.sleep(60)\n"
        )
        fake.chmod(0o755)
        os.environ.update(
            FAKE_OMP_STATE=str(state),
            FAKE_OMP_SESSION=str(transcript),
            PI_CODING_AGENT_DIR=str(self.state / "agent"),
        )
        try:
            name = hl.run_session(
                "omp",
                ["--resume", "/orig.jsonl"],
                host_id="omp-it-super",
                real_bin=str(fake),
                cwd=str(self.state),
                detach=True,
            )

            def runs(count: int) -> list:
                def got():
                    data = json.loads(state.read_text()) if state.exists() else []
                    return data if len(data) >= count else None

                return _wait_for(got, f"fake omp run {count}")

            first = runs(1)[0]
            self.extra_pids += [first["pid"], first["ppid"]]
            assert first["argv"] == ["--resume", "/orig.jsonl"]
            os.kill(first["pid"], signal.SIGKILL)
            second = runs(2)[1]
            self.extra_pids.append(second["pid"])

            assert second["argv"] == ["--resume", str(transcript)]
            assert second["ppid"] == first["ppid"], "relaunched by the same supervisor"
            sessions, ok = hl.list_sessions()
            assert ok and name in {s["name"] for s in sessions if not s["terminated"]}
        finally:
            for key in ("FAKE_OMP_STATE", "FAKE_OMP_SESSION", "PI_CODING_AGENT_DIR"):
                os.environ.pop(key, None)


# ── signal scope (#704): rig launches start the harness in its own Landlock signal domain ─────
def test_noninteractive_exec_goes_through_the_helper_when_enabled(monkeypatch):
    monkeypatch.setattr(hl.wrapper_provision, "signal_scope_enabled", lambda kind, env, **kw: True)
    monkeypatch.setattr(hl, "resolve_real_bin", lambda kind, env: "/opt/omp")
    calls = []
    hl.exec_or_wrap("omp", ["-p", "hi"], env={}, interactive_tty=False, exec_fn=lambda p, a, e: calls.append((p, a)))
    assert calls == [(sys.executable, ss.scoped_argv(["/opt/omp", "-p", "hi"]))]


def test_noninteractive_exec_is_untouched_when_disabled(monkeypatch):
    monkeypatch.setattr(hl.wrapper_provision, "signal_scope_enabled", lambda kind, env, **kw: False)
    monkeypatch.setattr(hl, "resolve_real_bin", lambda kind, env: "/opt/omp")
    calls = []
    hl.exec_or_wrap("omp", ["-p", "hi"], env={}, interactive_tty=False, exec_fn=lambda p, a, e: calls.append((p, a)))
    assert calls == [("/opt/omp", ["/opt/omp", "-p", "hi"])]


def test_run_session_records_the_decision_in_the_spec(monkeypatch):
    decided = []

    def enabled(kind, env, **kw):
        decided.append(kind)
        return True

    monkeypatch.setattr(hl.wrapper_provision, "signal_scope_enabled", enabled)
    monkeypatch.setattr(hl, "resolve_real_bin", lambda kind, env: "/opt/omp")
    calls = []
    hl.exec_or_wrap("omp", [], env={}, interactive_tty=True, exec_fn=lambda p, a, e: calls.append(e))
    spec = HostSpec.from_env(calls[0][ENV_SPEC])
    assert spec.signal_scope is True
    assert spec.argv == ["/opt/omp"]  # the prefix is applied at spawn time, never stored
    assert decided == ["omp"]  # decided once, in exec_or_wrap, and passed down


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="signal scope is Linux-only")
def test_direct_run_session_is_contained_by_default(monkeypatch):
    # `rig harness run` calls run_session directly: with no explicit value it decides itself,
    # from the real (default) config.
    monkeypatch.setattr(hl.wrapper_provision, "signal_scope_enabled", _REAL_SIGNAL_SCOPE_ENABLED)
    monkeypatch.delenv("RIG_SIGNAL_SCOPE", raising=False)
    calls = []
    hl.run_session("omp", [], real_bin="/opt/omp", exec_fn=lambda p, a, e: calls.append(e))
    assert HostSpec.from_env(calls[0][ENV_SPEC]).signal_scope is True
    calls.clear()
    hl.run_session("omp", [], real_bin="/opt/omp", signal_scope=False, exec_fn=lambda p, a, e: calls.append(e))
    assert HostSpec.from_env(calls[0][ENV_SPEC]).signal_scope is False
