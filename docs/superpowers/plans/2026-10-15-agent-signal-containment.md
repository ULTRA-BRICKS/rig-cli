# Agent Signal Containment (L2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When rig launches omp, omp runs in its own Linux Landlock signal domain. One agent's `kill` can then reach only that session's own process tree.

**Architecture:** A stdlib-only helper, `riglib/signal_scope.py`, restricts itself (`LANDLOCK_SCOPE_SIGNAL` + `NO_NEW_PRIVS`) and execs the harness. rig puts it as the innermost argv prefix on its two launch paths, and later on the #692 cgroup launcher. A GLOBAL config key with a per-launch env kill switch decides when it is used. Rollout to home is gated on fixing liveness probes that treat EPERM as "dead".

**Tech Stack:** Python 3 stdlib (`ctypes` syscalls 444/446, `prctl`), pytest, bash.

**Spec:** `docs/specs/2026-10-15-agent-signal-containment-design.md` (ticket [#704](https://git.hyperide.ai/ultrabricks/rig-cli/issues/704))

## Global Constraints

- Landlock ABI ≥ 6 is required. The ruleset is exactly `handled_access_fs=0, handled_access_net=0, scoped=LANDLOCK_SCOPE_SIGNAL (1<<1)`.
- Syscalls: `landlock_create_ruleset=444`, `landlock_restrict_self=446`. The version query is `landlock_create_ruleset(NULL, 0, 1)`.
- The helper never prevents a launch. `UNSUPPORTED`/`FAILED` print one stderr line and exec anyway. `NESTING_LIMIT` (`E2BIG`) is silent.
- The stderr line is exactly `rig signal-scope: not contained (<detail>); starting <cmd> unrestricted`. An exec failure prints `rig signal-scope: cannot exec <cmd>: <err>` and exits 127. A usage error exits 2.
- Env: `RIG_SIGNAL_SCOPE=0` disables the helper for one launch. The helper exports `RIG_SIGNAL_SCOPED=1` when it restricted.
- Argv prefix: `[sys.executable, "-I", "-S", <abs path of riglib/signal_scope.py>, "--", *argv]`. It is always the innermost prefix.
- Config: `harness_wrappers.signal_scope` is a list drawn from `omp`, `claude`, `codex` and `opencode`. The default is `["omp"]`; `[]` turns it off. It is GLOBAL-only and acts independently of `harness_wrappers.enabled`.
- Linux only: on any other platform the helper is never inserted.
- `signal_scope.py` imports nothing from `riglib`.

## Review Focus

1. **A missing, unreadable or invalid global config at launch.** The launch must still happen, with the default `[omp]`. No traceback and no silent loss of protection. Test: `test_signal_scope_enabled_falls_back_to_default_on_bad_config` (Task 2).
2. **A crash relaunch inside the rig host.** Every restart must again be contained, and the resume argv the host builds must not carry the prefix twice. Test: `test_every_relaunch_is_contained_and_unprefixed_in_registry` (Task 6).
3. **omp starting omp** (`review`, nested subagents through the PATH wrapper) up to the 16-layer limit. Depth 17 must still start. Test: `test_nesting_limit_execs_silently` (Task 1).
4. **The helper path contains spaces, or `sys.executable` is a venv shim.** The prefix must be absolute and argv-safe (no shell). Test: `test_scoped_argv_is_absolute_and_shell_free` (Task 1).
5. **The cross-project omp broker prune** (spec §7, a known limitation). After rollout, check that `omp ps` in a second project still lists its services; if it does not, record that on #704. Check: Task 8 Step 3.

---

### Task 1: `riglib/signal_scope.py` helper

**Files:**
- Create: `riglib/signal_scope.py`
- Test: `tests/test_signal_scope.py`

**Interfaces:**
- Produces:
  - `class Outcome(enum.Enum)` with members `RESTRICTED`, `UNSUPPORTED`, `NESTING_LIMIT`, `FAILED`.
  - `landlock_abi() -> int`.
  - `restrict_signals() -> tuple[Outcome, str]`.
  - `scoped_argv(argv: list[str]) -> list[str]`.
  - `HELPER: pathlib.Path`, the resolved path of this file.
  - `main(argv: list[str], *, restrict: Callable[[], tuple[Outcome, str]] = restrict_signals, exec_fn: Callable[[str, list[str]], NoReturn] = os.execvp, stderr: TextIO = sys.stderr) -> int`.
  - `if __name__ == "__main__": sys.exit(main(sys.argv[1:]))`.

- [ ] **Step 1: Write the failing unit tests** (fakes only; these run everywhere)

```python
def test_scoped_argv_is_absolute_and_shell_free():
    argv = ss.scoped_argv(["/opt/my omp/omp", "-r", "a b"])
    assert argv == [sys.executable, "-I", "-S", str(ss.HELPER), "--", "/opt/my omp/omp", "-r", "a b"]
    assert Path(argv[3]).is_absolute()

def test_restricted_exec_marks_env(monkeypatch):
    calls = []
    rc = ss.main(["--", "omp", "-x"], restrict=lambda: (ss.Outcome.RESTRICTED, ""), exec_fn=lambda f, a: calls.append((f, a, os.environ.get("RIG_SIGNAL_SCOPED"))))
    assert calls == [("omp", ["omp", "-x"], "1")]

def test_unsupported_warns_once_and_still_execs(capsys): ...
    # restrict -> (UNSUPPORTED, "landlock ABI 0 < 6"); stderr == "rig signal-scope: not contained (landlock ABI 0 < 6); starting omp unrestricted\n"; exec called; RIG_SIGNAL_SCOPED unset

def test_failed_warns_and_still_execs(capsys): ...   # same with (FAILED, "restrict_self: Invalid argument")
def test_nesting_limit_execs_silently(capsys): ...   # stderr == ""; exec called
def test_kill_switch_skips_restrict(monkeypatch):    # RIG_SIGNAL_SCOPE=0 -> restrict never called, exec called
def test_missing_command_is_usage_error(): assert ss.main([]) == 2 and ss.main(["--"]) == 2
def test_exec_failure_returns_127(capsys): ...       # exec_fn raises FileNotFoundError -> 127, "rig signal-scope: cannot exec nope: " in stderr
def test_helper_imports_no_riglib():                 # ast-parse HELPER; no Import/ImportFrom of "riglib" or relative
```

- [ ] **Step 2: Write the failing kernel tests**, marked `pytest.mark.skipif(ss.landlock_abi() < 6, reason="needs Landlock ABI >= 6")`. Each one starts an outside canary with `subprocess.Popen(["sleep", "30"], start_new_session=True)` and kills it in `finally`.

```python
def test_contained_child_cannot_signal_outside(canary):
    r = run([*ss.scoped_argv([sys.executable, "-c", f"import os,signal\nfor s in (0, signal.SIGTERM):\n  try: os.kill({canary.pid}, s); print('sent')\n  except PermissionError: print('EPERM')"])])
    assert r.stdout.split() == ["EPERM", "EPERM"] and canary.poll() is None

def test_contained_child_kills_its_own_child(): ...      # bash -c 'sleep 20 & kill $!; wait $!; echo $?' -> "143"
def test_kill_minus_one_spares_outside(canary): ...      # bash -c 'sleep 20 & kill -TERM -1; wait' ; canary alive
def test_nested_scopes_still_start(): ...                # 17 nested scoped_argv layers around "true" -> rc 0, stderr has no "not contained"
```

- [ ] **Step 3: Run them to confirm they fail**

Run: `python3 -m pytest tests/test_signal_scope.py -q`
Expected: ERROR collecting, `ModuleNotFoundError: riglib.signal_scope`.

- [ ] **Step 4: Implement `riglib/signal_scope.py`.** Use `ctypes.CDLL(None, use_errno=True).syscall` with a `ctypes.Structure` of three `c_uint64` (`fs`, `net`, `scoped`). Steps: `prctl(38, 1, 0, 0, 0)`, then `restrict_self(fd, 0)`. Map errno `E2BIG` → `NESTING_LIMIT`. Map `ENOSYS`/`EOPNOTSUPP`, ABI < 6 or a non-Linux platform → `UNSUPPORTED`. Map any other errno → `FAILED`, with detail `"<step>: <strerror>"`. Close the ruleset fd before exec. Add a file header comment explaining why the helper exists (incident 2026-10-15) and why it is stdlib-only.

- [ ] **Step 5: Run the tests**

Run: `python3 -m pytest tests/test_signal_scope.py -q`
Expected: all pass on home (ABI 8). On an ABI < 6 host the kernel tests show `s`.

- [ ] **Step 6: Commit**

```bash
git add riglib/signal_scope.py tests/test_signal_scope.py
git commit -m "feat(harness): Landlock signal-scope helper that execs a harness in its own signal domain (Refs #704)"
```

### Task 2: `harness_wrappers.signal_scope` config and launch policy

**Files:**
- Modify: `riglib/config.py` (`_validate_harness_wrappers`, ~line 3013), `riglib/config_schema.py` (`_HARNESS_WRAPPERS_BLOCK`, ~line 1775), `riglib/schema.py` (harness_wrappers `Area`, ~line 561), `schema/rig.schema.json` (`harness_wrappers`, ~line 3066), `riglib/wrapper_provision.py` (`Settings`, `settings_from_config`), `docs/config-schema.md` (`## harness_wrappers`, ~line 4285), `README.md` (`rig harness` row, line 158)
- Test: `tests/test_harness_wrappers_apply.py`

**Interfaces:**
- Consumes: `riglib.config.read_yaml_file`, `riglib.config.global_config_path`.
- Produces:
  - `Settings.signal_scope: tuple[str, ...]`, in canonical `KINDS` order; default `("omp",)`.
  - `signal_scope_enabled(kind: str, env: Mapping[str, str], *, platform: str = sys.platform, config_path: Path | None = None) -> bool` in `riglib/wrapper_provision.py`.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("value", [[], ["omp"], ["omp", "claude"]])
def test_validate_accepts_signal_scope(value): validate({"version": 1, "harness_wrappers": {"signal_scope": value}})

@pytest.mark.parametrize("value, msg", [("omp", "harness_wrappers.signal_scope must be a list"), (["vim"], "unknown harness 'vim'")])
def test_validate_rejects_signal_scope(value, msg): ...

def test_settings_default_signal_scope_is_omp(): assert settings_from_config({}).signal_scope == ("omp",)

def test_signal_scope_enabled_matrix(tmp_path):
    cfg = tmp_path / "config.yaml"; cfg.write_text("harness_wrappers:\n  signal_scope: [omp, codex]\n")
    assert signal_scope_enabled("omp", {}, platform="linux", config_path=cfg)
    assert signal_scope_enabled("codex", {}, platform="linux", config_path=cfg)
    assert not signal_scope_enabled("claude", {}, platform="linux", config_path=cfg)
    assert not signal_scope_enabled("omp", {"RIG_SIGNAL_SCOPE": "0"}, platform="linux", config_path=cfg)
    assert not signal_scope_enabled("omp", {}, platform="darwin", config_path=cfg)

def test_signal_scope_off_with_empty_list(tmp_path): ...   # signal_scope: [] -> False for omp

@pytest.mark.parametrize("text", [None, "", ": not yaml :", "harness_wrappers: 3\n", "harness_wrappers:\n  signal_scope: omp\n"])
def test_signal_scope_enabled_falls_back_to_default_on_bad_config(tmp_path, text):
    # None = file absent; every case -> omp True, claude False, no exception
```

- [ ] **Step 2: Run them to confirm they fail**

Run: `python3 -m pytest tests/test_harness_wrappers_apply.py -q -k signal_scope`
Expected: FAIL (`unknown key 'signal_scope'` / `ImportError: signal_scope_enabled`).

- [ ] **Step 3: Implement.**
  - Validation reuses the `kinds` checks (list of strings, known kinds) but allows an empty list.
  - Schema doc for `signal_scope`: "Kinds whose launch through rig runs inside a Linux Landlock signal domain: the agent can signal only its own process tree. Linux only. Applies to every rig launch path, independent of `enabled`. `[]` turns it off; `RIG_SIGNAL_SCOPE=0` skips it for one launch."
  - `signal_scope_enabled` catches `ConfigError`/`OSError` and falls back to `Settings` defaults when the read data has the wrong shape.
  - In `docs/config-schema.md`, add a `signal_scope` subsection. It covers: what it blocks (spec §3); what it does not block (spec §2, IPC); the NO_NEW_PRIVS table (spec §6) with the `systemd-run --user --pipe --wait --quiet -- <cmd>` escape hatch; `RIG_SIGNAL_SCOPED=1`; and the omp broker known limitation (spec §7).
  - Extend the README `rig harness` row with one sentence about the signal scope.

- [ ] **Step 4: Run the tests, plus the schema sync tests**

Run: `python3 -m pytest tests/test_harness_wrappers_apply.py tests/test_config_web.py tests/test_config_web_plan.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add riglib/config.py riglib/config_schema.py riglib/schema.py schema/rig.schema.json riglib/wrapper_provision.py docs/config-schema.md README.md tests/test_harness_wrappers_apply.py
git commit -m "feat(config): harness_wrappers.signal_scope, default [omp], decides which rig launches are signal-contained (Refs #704)"
```

### Task 3: tmux lock-break treats EPERM as alive

**Files:**
- Modify: `riglib/tmux.py:1541` (the generated `break_stale_lock` shell)
- Test: the existing test module for the tmux save script (find it with `grep -l break_stale_lock tests/`)

**Interfaces:** none new.

- [ ] **Step 1: Write the failing test** `test_lock_held_by_unsignalable_pid_is_not_broken`. Write `$LOCK/pid` with the pid of a live process outside the caller's reach. Run the generated script through `scoped_argv(["bash", script])` (skip if ABI < 6). Assert the lock dir still exists and the log has no "holder dead".
- [ ] **Step 2: Run it.** Expected: FAIL; the lock is broken with "holder dead".
- [ ] **Step 3: Implement.** Replace `! kill -0 "$holder" 2>/dev/null` with a locale-independent probe that treats only a missing process as dead: `[ ! -d "/proc/$holder" ]` on Linux; on macOS keep `kill -0` but run it as `LC_ALL=C kill -0 "$holder" 2>&1 | grep -q 'No such process'`. Never grep localized diagnostics without `LC_ALL=C`. The age-based break stays as it is. Also add `test_dead_holder_is_broken_under_non_c_locale` (LANG=de_DE.UTF-8, dead pid → lock broken).
- [ ] **Step 4: Run the tmux tests.** Expected: PASS.
- [ ] **Step 5: Commit** `fix(tmux): a lock held by a pid we may not signal is alive, not stale (Refs #704)`.

### Task 4: AppImage launcher on home runs the extracted image (operator, outside the repo)

**Files:** `~/Applications/orcaslicer-cli` (hand-written, not in any repo); a new dir `~/Applications/OrcaSlicer.d/`.

- [ ] **Step 1:** `cd ~/Applications && mkdir OrcaSlicer.d && cd OrcaSlicer.d && ../OrcaSlicer.AppImage --appimage-extract >/dev/null`. Expected: `squashfs-root/AppRun` exists.
- [ ] **Step 2:** Change the last line of `orcaslicer-cli` to `exec "$HOME/Applications/OrcaSlicer.d/squashfs-root/AppRun" "$@"`.
- [ ] **Step 3:** Verify inside a scope: `python3 -I -S riglib/signal_scope.py -- ~/Applications/orcaslicer-cli --help | head -3`. Expected: OrcaSlicer usage text, not "Cannot mount AppImage".
- [ ] **Step 4:** Record on #704 (`task comment '#704' …`) that the AppImage path is fixed. Note there that the OpenSCAD and Snapmaker AppImages need the same treatment when agents use them.

### Task 5: File the omp upstream issue (operator)

- [ ] **Step 1:** Open an issue on the upstream oh-my-pi repository: "Liveness probes treat EPERM as dead: the daemon broker prunes live runtime dirs of other projects". Give the sites (`launch/presence.ts` `r0o`/`aLe`/prune, `launch/broker.ts` lock-owner check, `collab/registry.ts`, `cli/ps-data.ts`) with omp 18.8.1 bundle lines from spec §7. Include the repro: two sessions in different projects, the second started under `riglib/signal_scope.py`. State the fix: only `ESRCH` means dead.
- [ ] **Step 2:** Link it on #704 with `task comment '#704' "<url>"`.

### Task 6: Contain both rig launch paths (the default goes live)

**Precondition (Ruling R10, amended by R-M2):** [tg-cli#748](https://git.hyperide.ai/ultrabricks/tg-cli/issues/748) is merged and deployed (`tg-ctl restart --if-stale` has run), and Tasks 3–4 are done. Task 5 is not an agent action (public upstream filing): its issue text is posted on #704 for the owner; the broker-prune limitation is watched by Task 8 Step 3, and a failure there turns the default off (`signal_scope: []`) until upstream fixes it. Merging this task makes `[omp]` live for every `rig harness` launch.

**Files:**
- Modify: `riglib/harness_host.py` (`HostSpec` lines 124-153; spawn at line 357), `riglib/harness_launcher.py` (`build_spec` line 128, `run_session` line 133, `exec_or_wrap` line 287)
- Test: `tests/test_harness_launcher.py`, `tests/test_harness_host.py`

**Interfaces:**
- Consumes: `signal_scope.scoped_argv` (Task 1), `wrapper_provision.signal_scope_enabled` (Task 2).
- Produces:
  - `HostSpec.signal_scope: bool = False`. `to_env` writes `"signal_scope": true` only when true; `from_env` reads an absent key as `False`.
  - `build_spec(..., signal_scope: bool)`.
  - `harness_host.child_argv(spec: HostSpec, command: list[str]) -> list[str]`, which returns `scoped_argv(command)` when `spec.signal_scope`, else `command`.

- [ ] **Step 1: Write the failing tests**

```python
# test_harness_launcher.py
def test_noninteractive_exec_goes_through_the_helper_when_enabled(monkeypatch):
    monkeypatch.setattr(hl.wrapper_provision, "signal_scope_enabled", lambda kind, env, **kw: True)
    monkeypatch.setattr(hl, "resolve_real_bin", lambda kind, env: "/opt/omp")
    calls = []
    hl.exec_or_wrap("omp", ["-p", "hi"], env={}, interactive_tty=False, exec_fn=lambda p, a, e: calls.append((p, a)))
    assert calls == [(sys.executable, ss.scoped_argv(["/opt/omp", "-p", "hi"]))]

def test_noninteractive_exec_is_untouched_when_disabled(monkeypatch): ...   # enabled -> False: calls == [("/opt/omp", ["/opt/omp", "-p", "hi"])]

def test_run_session_records_the_decision_in_the_spec(monkeypatch): ...      # HostSpec.from_env(env[ENV_SPEC]).signal_scope is True

# test_harness_host.py
def test_spec_round_trips_signal_scope():
    spec = hh.HostSpec("omp", ["/opt/omp"], "/tmp", signal_scope=True)
    assert hh.HostSpec.from_env(spec.to_env()).signal_scope is True
    assert "signal_scope" not in hh.HostSpec("omp", ["/opt/omp"], "/tmp").to_env()

def test_child_argv_prefixes_only_when_scoped(): ...

@pytest.mark.skipif(ss.landlock_abi() < 6, reason="needs Landlock ABI >= 6")
def test_every_relaunch_is_contained_and_unprefixed_in_registry(fake, tmp_path):
    # _host(..., plan="crash,crash,exit") with a spec built with signal_scope=True:
    # every entry in _runs(tmp_path) has nnp == "1" and scoped == "1"
    # (FAKE_HARNESS records NoNewPrivs from /proc/self/status and RIG_SIGNAL_SCOPED);
    # the registry's recorded argv and every relaunch command start with the fake binary, never sys.executable
```

- [ ] **Step 2: Run them to confirm they fail**

Run: `python3 -m pytest tests/test_harness_launcher.py tests/test_harness_host.py -q -k "signal_scope or scoped or helper or relaunch_is_contained"`
Expected: FAIL (`unexpected keyword argument 'signal_scope'`).

- [ ] **Step 3: Implement.**
  - The decision is made in `run_session`, not only in `exec_or_wrap`: `run_session` computes `signal_scope_enabled(kind, env)` itself when the caller passes no explicit value, because `rig harness run` (`harness_cli.py`) calls `run_session` directly. `exec_or_wrap` computes it once and passes it down. The non-interactive branch execs `argv[0]` of `scoped_argv([real, *args])` when `scoped`. Add `test_direct_run_session_is_contained_by_default` (no explicit value → spec.signal_scope True under the default config).
  - The host spawns `child_argv(spec, command)` at line 357 and leaves `command` unprefixed.
  - Extend `FAKE_HARNESS` in `tests/test_harness_host.py` so it records `nnp` and `scoped` in its run entry.

- [ ] **Step 4: Run the harness suites**

Run: `python3 -m pytest tests/test_harness_launcher.py tests/test_harness_host.py tests/test_harness_cli.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add riglib/harness_host.py riglib/harness_launcher.py tests/test_harness_launcher.py tests/test_harness_host.py
git commit -m "feat(harness): rig harness exec/host start omp inside its own signal domain (Refs #704)"
```

### Task 7: Home launcher goes through the helper (Ruling R13)

**Precondition:** Task 6 has merged.

**Files:** if #692 has merged, its rig-generated omp launcher (whatever `harness_launcher` function emits the `systemd-run` argv). Otherwise `~/.local/bin/omp` (exec lines 33, 44-50, 53).

- [ ] **Step 1 (#692 merged):** Write a test that the generated launcher's exec argv is `systemd-run … -- <scoped_argv([real_omp])> "$@"` when `signal_scope_enabled("omp", env)`, and unprefixed when it is not. Implement, run `python3 -m pytest tests/test_harness_launcher.py -q`, commit `feat(harness): the omp cgroup launcher starts omp signal-contained (Refs #704)`.
- [ ] **Step 1 (#692 not merged):** In `~/.local/bin/omp`, replace `"$REAL_OMP"` in the three exec sites (line 33, line 50 inside the `systemd-run` exec at 44-50, and line 53) with `python3 -I -S "$HOME/dev/rig-cli/riglib/signal_scope.py" -- "$REAL_OMP"`. Add one comment line naming #704 and `RIG_SIGNAL_SCOPE=0` as the per-launch off switch. Then comment on #692 (`task comment '#692' …`) that its rig-managed launcher must keep this prefix.
- [ ] **Step 2:** `omp --version` in a new tmux pane. Expected: `omp/18.8.1` (or newer) and no "not contained" line.

### Task 8: Live proof and ticket acceptance

- [ ] **Step 1:** Start an outside canary that the user manager owns: `systemd-run --user --unit=l2-canary sleep 600`.
- [ ] **Step 2:** Run the incident in a **disposable** contained session. It dies by design, because it is in its own domain. Note the `systemd --user` pid and `pgrep -c -x omp`. Then, from a plain pane, launch through the launcher `omp -p --no-session --tools=bash --model haiku "Run exactly this with the bash tool, then print its output: env | grep RIG_SIGNAL_SCOPED; if kill -0 $(systemctl --user show -p MainPID --value l2-canary.service) 2>/dev/null; then echo NOT-CONTAINED; exit 99; fi; P=; kill -TERM \$(pstree -p \$P | grep -o '([0-9]*)' | tr -d '()') 2>/tmp/l2-proof.err"`. Expected afterwards, checked from the plain pane:
  - `grep -c 'not permitted' /tmp/l2-proof.err` is in the hundreds;
  - `systemctl --user is-active l2-canary.service` prints `active`;
  - the `systemd --user` pid is unchanged;
  - `pgrep -c -x omp` is the earlier count (the disposable session has exited);
  - every other session still answers.
- [ ] **Step 3:** Review Focus 5: in a second project, `omp ps` still lists that project's named services ten minutes after a broker started in the first. Record the result on #704.
- [ ] **Step 4:** Attach the transcripts and tick the #704 criteria with `task check '#704' <n> --proof <file>`. Stop the canary: `systemctl --user stop l2-canary.service`.
