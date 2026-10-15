# Spec: agent signal containment — omp sessions run in a Landlock signal domain (L2)

Status: design (implementation tracked in [rig-cli#704](https://git.hyperide.ai/ultrabricks/rig-cli/issues/704)).
Plan: [`docs/superpowers/plans/2026-10-15-agent-signal-containment.md`](../superpowers/plans/2026-10-15-agent-signal-containment.md).

## 1. Problem

On 2026-10-15 at 20:33:48, an omp subagent ran

```bash
P=$(pgrep -f "^gh ship 247" | head -1); kill -TERM $(pstree -p $P | grep -o '([0-9]*)' | tr -d '()')
```

with `$P` empty. `pgrep` matched nothing because the job was still queued inside `flock`, so its command line started with `flock`. With no pid argument, `pstree -p` prints the whole system tree. The loop ran inside omp's in-process shell, and it sent SIGTERM to `systemd --user`. The user manager ran `exit.target` and stopped every unit under `user@1000.service`: all nine omp session scopes, the GNOME session (gdm logged out to the greeter) and sysmon. No OOM was involved.

The root problem is that any process of uid 1000 may signal any other process of uid 1000. The agent's bash tool, its python/bun eval kernels and every script they start therefore hold a loaded gun aimed at the desktop and at every other agent session. The rig guard regex ([#701](https://git.hyperide.ai/ultrabricks/rig-cli/issues/701)) can only refuse spellings it recognises. `os.kill` in python, `process.kill` in bun/node, a pid list read from a file, or the next shell idiom nobody listed will all get through.

### The ask (verbatim)

> как избежать подобных выбиваний всех процессов

("how do we avoid this kind of knocking out of all processes"). The design decisions were delegated: "сам подумай как лучше сделать" ("think it through yourself and decide how best to do it").

## 2. Layered defense

| Layer | What | Where | Status |
|---|---|---|---|
| L1 | Guard regex: ask/deny `kill $(…)`, `kill -1`, pstree-fed kills, and signals to systemd --user / session leader / gnome-shell / tmux / other omp sessions | [#701](https://git.hyperide.ai/ultrabricks/rig-cli/issues/701), `riglib/permissions.py` | in progress |
| **L2** | **Kernel-enforced signal scope: each omp session can signal only its own process tree** | **this spec, [#704](https://git.hyperide.ai/ultrabricks/rig-cli/issues/704)** | design |
| L3 | sysmon records a mass session death and can restore the sessions | [#702](https://git.hyperide.ai/ultrabricks/rig-cli/issues/702) | filed |
| L4 | Remove the false alarms that set off the "abort everything" storm: the swap-drain swapoff read as a memory fall, and swap alert flapping | [#693](https://git.hyperide.ai/ultrabricks/rig-cli/issues/693), [#699](https://git.hyperide.ai/ultrabricks/rig-cli/issues/699), ubuntu-tweaks HYP-1706 | filed / in progress |

L1 catches the common case early and explains the refusal to the agent. L2 makes the remaining cases harmless. L3 recovers from what slips past both. L4 removes the pressure that made agents start killing in the first place.

### Threat model

L2 targets **accidental** mass signals from agent tooling: a wrong pid list, an empty variable, `kill -1`, or a careless `os.kill` loop. The offending session is still inside its own domain, so the incident command still ends **that one session** (omp and its children; spike row f1: the wrapper's own bash died). What L2 removes is the reach beyond that session. It is not containment against an adversarial agent. These IPC paths ask a process **outside** the domain to do the killing, so L2 does not stop them, and they stay L1's job:

- `systemctl --user stop|kill|exit|isolate …`;
- `systemd-run --user … kill …` (this is also the escape hatch, see §6);
- `loginctl terminate-*`;
- `tmux kill-server`;
- `docker kill`.

## 3. Mechanism

Linux Landlock ABI ≥ 6 (kernel ≥ 6.12) adds *scoping*. A ruleset created with `scoped = LANDLOCK_SCOPE_SIGNAL` and **no** filesystem or network rights (`handled_access_fs = 0`, `handled_access_net = 0`) restricts nothing except signals. After `prctl(PR_SET_NO_NEW_PRIVS, 1)` and `landlock_restrict_self(fd, 0)`, the calling process and every descendant it creates live in a new Landlock domain.

- A task in the domain can send a signal (including signal 0, `pidfd_send_signal`, and `F_SETOWN`/SIGIO) only to tasks in the same domain or in domains nested under it. Every other target returns **EPERM**.
- `kill(-1, sig)` and `kill(-pgid, sig)` skip the targets outside the domain and succeed if at least one target in the domain was signalled.
- Signals the kernel sends are not affected: tty SIGINT/SIGTSTP/SIGWINCH, SIGCHLD to the parent, SIGPIPE, SIGHUP on hangup. Processes **outside** the domain can still signal into it (tmux, the rig host, systemd stopping the scope).
- Domains nest up to 16 layers. `restrict_self` at depth 17 fails with `E2BIG`, and the process stays in the 16 domains it already has.
- `NO_NEW_PRIVS` is mandatory for an unprivileged caller and is inherited across exec: setuid/setgid bits and file capabilities no longer raise privilege (§6).

Syscall numbers are 444 (`landlock_create_ruleset`) and 446 (`landlock_restrict_self`) on x86_64 and aarch64; 445 (`landlock_add_rule`) is not used. The ABI version is `landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION)`.

## 4. Spike results (home, kernel 7.0.0-15-generic, Landlock ABI 8, 2026-10-15)

A throwaway ctypes wrapper (`create_ruleset{fs=0, net=0, scoped=SIGNAL}` → NNP → `restrict_self` → `execvp`) ran each case. "Outside" means a process started by the test harness outside the wrapper.

| # | Case | Observed |
|---|---|---|
| a1 | `kill -TERM <outside pid>` (bash builtin), `/bin/kill`, python `os.kill`, bun and node `process.kill`, python `pidfd_send_signal` | All `Operation not permitted` / `EPERM`; the canary stays alive |
| a2 | Signal-0 probe of an outside pid (`kill -0`, `process.kill(pid, 0)`) | **EPERM**, not success and not ESRCH; `kill -0 1` and `kill -0 $PPID` are EPERM too |
| b1 | Kill own background job (`sleep 20 & kill $!`) / grandchild / `kill -0 $$` | rc 143 / rc 0 / rc 0 |
| b2 | Outer domain kills a child that restricted itself again (nested domain) | rc 0 |
| b3 | 17 nested wrappers | Layer 17 `restrict_self` → `E2BIG` ("Argument list too long"); 16 layers succeed |
| c1 | `systemctl --user stop <scope started outside>` from inside | rc 0, scope `inactive`, its process got SIGTERM |
| c2 | `systemd-run --user --scope` started inside: direct `kill` of its pid / `systemctl --user kill` | rc 143 / rc 0 (the scope's process is in the domain) |
| c3 | `systemd-run --user --pipe --wait grep NoNewPrivs /proc/self/status` from inside | `NoNewPrivs: 0`: the user manager spawns it **outside** the domain (escape hatch) |
| d1 | `docker ps`, `omp-steer list`, `tg-ctl omp-live status --json` | Same output inside and outside |
| d2 | `tg-ctl status` | **Wrong inside**: "not running (stale pidfile, pid 2204873)" while running; tg-ctl treats EPERM as dead ([tg-cli#748](https://git.hyperide.ai/ultrabricks/tg-cli/issues/748)) |
| e1 | `sudo -n true` | Outside: "interactive authentication is required". Inside: `sudo: The "no new privileges" flag is set, which prevents sudo from running as root.` |
| e2 | `fusermount3 -u <dir>` | Inside: `failed to unmount …: Operation not permitted` |
| e3 | `~/Applications/OpenSCAD.AppImage --version` | Outside: `OpenSCAD version 2026.10.08`. Inside: rc 127, `Cannot mount AppImage, please check your FUSE setup … fusermount3: mount failed: Operation not permitted` |
| e4 | Same AppImage with `APPIMAGE_EXTRACT_AND_RUN=1 TMPDIR=<disk dir>` | Works inside; leaves a 296 MB extraction behind |
| e5 | `bwrap --unshare-user --unshare-pid`, `google-chrome --headless=new --dump-dom` | Both work inside (the user-namespace sandbox needs no setuid) |
| e6 | `pkexec --version`, `newuidmap` usage | Both run. Privilege escalation through them cannot work under NNP [INFERENCE: not exercised; same mechanism as e1/e2]. Rootless podman (newuidmap) is not installed here |
| f1 | **The incident command** inside, behind a precondition that aborts if `kill -0 <canary>` succeeds | `pstree -p ''` listed 1302 pids; 1110 × EPERM; the plain canary and a canary in its own `systemd-run --user --scope` both survive; `systemd --user` keeps pid 2204816; only the wrapper's own bash died (it is in the domain) |
| f2 | `kill -TERM -1` inside | rc 0; only the domain's own `sleep` child died; canaries alive |
| g1 | **Real omp 18.8.1 under the wrapper** (`omp -p --tools=bash`): the bash tool runs `kill -0`/`kill -TERM` on an outside canary, then kills its own job | `$$` = the omp pid, `NoNewPrivs: 1`, both outside signals `Operation not permitted`, own job rc 143, canary alive |

## 5. Hook point

### 5.1 Why the whole omp process, not only the shell

omp's bash tool does not run `/bin/bash`. It runs an **in-process brush shell**, and `kill`, `pkill`, `pgrep`, `nohup` and `timeout` are builtins that execute inside the omp process. The evidence:

- `omp://natives-shell-pty-process.md` §"Session creation": "process builtins registered … `pgrep`, `pkill` …; `kill` comes from the default bash-mode set, where `pi-builtins`' richer implementation replaces brush's original".
- In a live session, `type kill` prints `kill is a shell builtin`, and `ps -o comm -p $$` prints `omp`.
- In the incident, the RCA shows `$$` was the omp pid.

A wrapper around the tool shell therefore cannot exist: the builtin `kill` sends the signal from omp itself. **Ruling R1: restrict the whole omp process before exec.** That domain then also covers:

- the eval kernels (python/bun children);
- MCP servers, LSP servers and the browser, all spawned by omp;
- the in-process subagents of the `task` tool;
- the omp daemon broker for named services. omp spawns it with `Bun.spawn` (`packages/coding-agent/src/launch/client.ts`, omp 18.8.1 bundle `~/.local/lib/omp/omp` line 1213628), so it inherits the domain of the session that started it.

omp's own control of its jobs keeps working, because every target is inside the domain:

- `proc://bg_N/kill`, timeouts and `Shell.abort()` signal the run's own descendants;
- `proc://<service>/kill` is an IPC request to the broker, which signals its own children;
- the TUI's Ctrl+Z `process.kill(0, "SIGSTOP")` (`input-controller.ts`) reaches omp's own process group.

### 5.2 One domain per session (Ruling R2)

Each launch calls `restrict_self` itself, so every omp session gets its own domain. A single shared domain for all sessions was rejected: inside it, the incident command would still SIGTERM every other omp session directly, and that is most of the outage. The price of separate domains: a session's signal-0 probe of another session's pid gets EPERM (§7), so code that reads EPERM as "dead" has to be fixed.

### 5.3 Where rig inserts it

The helper is a stdlib-only Python file, `riglib/signal_scope.py`, run by path:

```
python3 -I -S <checkout>/riglib/signal_scope.py -- <real harness> <args…>
```

It restricts itself and then `execvp`s the command, so omp keeps the same pid and the final argv is omp's own. The helper adds no lasting process, which matters because tg-ctl and sysmon recognise agents by argv. Ruling R3 settles three choices:

- **Argv prefix instead of `preexec_fn`:** `harness_host.py` runs a sampler thread (`harness_host.py:418-425`).
- **Argv prefix instead of an in-process restrict before `exec_fn`:** tests inject `exec_fn`, and an in-process restrict would sandbox the pytest process.
- **A file path instead of `-m riglib…`:** startup stays at a bare interpreter, with no package import.

Call sites (the innermost prefix: any cgroup or `systemd-run` wrapper goes outside it):

1. `riglib/harness_launcher.py:287`: `exec_fn(real, [real, *args], env)`, the non-interactive `rig harness exec` path.
2. `riglib/harness_host.py:357`: `subprocess.Popen(command, …)`, the supervised interactive path. The prefix is applied at spawn time only. `command` itself stays the harness argv, so relaunch/resume logic is unchanged, and the host's `registry.write(harness_pid=child.pid)` still records omp's pid because the helper execs in place.
3. The omp cgroup launcher that [#692](https://git.hyperide.ai/ultrabricks/rig-cli/issues/692) moves into rig. It becomes `systemd-run --user --scope … -- python3 -I -S …/signal_scope.py -- <real omp> "$@"`. `systemd-run --scope` execs its command (verified: the command's parent is the caller), so the order is: scope created → helper restricts → omp. On home today this launcher is the hand-written `~/.local/bin/omp`, which execs omp at lines 33, 44-50 and 53.

`HostSpec` gains `signal_scope: bool`, serialised as `"signal_scope": true` only when true; an absent key reads as false. The supervisor's restarts therefore keep the decision taken at launch.

## 6. NO_NEW_PRIVS side effects and mitigations

| Affected | Impact on home | Mitigation (Ruling R7) |
|---|---|---|
| `sudo`, `pkexec`, `su` | Agents could not use them anyway: sudo needs a password and the bash tool has no tty (e1, outside run). The user's own `!sudo …` typed into omp also stops working. | Run privileged commands in a tmux pane outside omp. The escape hatch below runs outside the domain, but sudo's password prompt still needs a terminal. |
| AppImages (FUSE via setuid `fusermount3`) | **`~/Applications/orcaslicer-cli` execs `OrcaSlicer.AppImage`**, used by the 3d toolchain, and fails with rc 127 (e3). OpenSCAD/Snapmaker AppImages fail the same way. | Extract once to disk (`OrcaSlicer.AppImage --appimage-extract`) and exec `squashfs-root/AppRun`. That costs no tmpfs, and the wrapper script keeps its interface (plan Task 4). Ad-hoc fallback: `APPIMAGE_EXTRACT_AND_RUN=1` with `TMPDIR` on disk (e4). |
| `sshfs`, any FUSE mount, `fusermount3 -u` | Fails (e2). | `systemd-run --user --pipe --wait --quiet -- <cmd>` |
| `newuidmap`/`newgidmap` (rootless podman) | Not installed here. | Same escape hatch. Docker uses its daemon socket and works (d1). |
| Chrome / agent-browser, `bwrap` | None: user-namespace sandboxes need no setuid (e5). | — |

**Escape hatch.** `systemd-run --user --pipe --wait --quiet -- <cmd>` asks the user manager, over D-Bus, to start `<cmd>` as a transient service. The user manager is outside every domain, so `<cmd>` runs with `NoNewPrivs: 0` and no Landlock domain (c3). This path already exists and needs no broker of our own. A custom IPC broker was rejected: it would only re-implement this path, and §2's threat model does not ask for one. The docs name the escape hatch next to the EPERM explanation, for privilege-needing tools (sudo, fusermount, rootless podman) only. They state that an EPERM from `kill` is the containment working, and that re-running a `kill` through the escape hatch is exactly the incident L2 exists to stop (L1's kill guard still applies to it).

**Diagnosis.** The helper exports `RIG_SIGNAL_SCOPED=1` into the contained process (and so into every child). This tells an agent, the L1 guard message or a human why `kill` returned "Operation not permitted" (Ruling R11).

## 7. Liveness probes: EPERM must mean "alive"

Inside a domain, `kill(pid, 0)` on a pid outside it returns EPERM (a2). POSIX already defines EPERM as "exists, not yours to signal", so the correct probe treats only ESRCH as dead. Probes that catch every error as "dead" now give the wrong answer across domains:

| Code | Behaviour with EPERM-as-dead | Fix owner | Gate? |
|---|---|---|---|
| tg-cli `features/tg-ctl/omp-live-runtime.mjs:30` `processExists`, used at `:250-280` | **Deletes live sessions' omp-live records and sockets.** This happens when an agent in a contained session runs `omp-steer` or `tg-ctl omp-live status/send`. Those sessions then become unsteerable, and the sysmon `alert.route: agents` delivery can no longer reach them. | [tg-cli#748](https://git.hyperide.ai/ultrabricks/tg-cli/issues/748) | **Yes** |
| tg-cli `tg-ctl:844-851` `kill0` | `tg-ctl status` says "not running (stale pidfile)" (d2) | [tg-cli#748](https://git.hyperide.ai/ultrabricks/tg-cli/issues/748) | **Yes** |
| rig `riglib/tmux.py:1541` lock-break script (`! kill -0 "$holder"` → "holder dead" → `rm -rf "$LOCK"`) | When run from inside omp, it breaks a lock that a live process outside holds | this plan, Task 3 | **Yes** |
| omp `packages/coding-agent/src/launch/presence.ts` (bundle lines 1625337-1625428: `r0o` presence check, `aLe` broker-pid check, `Ddi` prune) | A broker starting in one session's domain sees other projects' brokers and clients as dead. It deletes their presence files, then `rm -rf`s their daemon runtime dirs once those dirs are more than 5 min old. The services keep running, but other sessions lose `proc://` control and logs for them. | upstream oh-my-pi (issue filed by plan Task 5) | No: known limitation, see below |
| omp `launch/broker.ts` `UBc` (line 1680238), `collab/registry.ts` `R0l` (line 1320761), `cli/ps-data.ts` `wBu` (line 1625541) | Lock-owner check (it is also protected by a lock-file acquire), stale collab entry removal on version mismatch, and `omp ps` showing a detached daemon as exited | upstream oh-my-pi (same issue) | No |
| omp `logger.ts`, `stream.ts`, `session-storage.ts`, `isolation-ownership.ts`, `browser/orphan-registry.ts`, `gc-cli.ts`, `parent-watchdog-worker.ts`; rig `omp_inspect.py:293`, `tracked_files.py:741`, `worktree_gc.py:512` | Already treat EPERM as alive | — | — |

**Ruling R10 (rollout gate).** The default goes live only when Task 6 wires the launch paths. That task waits for four things:

- tg-cli#748 is merged and deployed;
- the rig `tmux.py` fix has landed (Task 3);
- the orcaslicer-cli AppImage path is fixed (Task 4);
- the omp upstream issue has been filed (Task 5).

The omp presence/broker defect is accepted as a known limitation until upstream fixes it. Its blast radius is lost `proc://` control of named services across projects; no process is killed and no session dies. That is far smaller than the mass kill L2 prevents, and the docs state it.

Signals *between* sessions also stop: an agent can no longer `kill` a dev server or job that another session started. That is the point of the change. For a unit, the way out is `systemctl --user stop <unit>`; otherwise, ask the owning session. Without the scope, omp's stats `port-conflict.ts` would kill another session's stats server; with it, it reports "Failed to stop …".

## 8. Helper contract (`riglib/signal_scope.py`)

- `landlock_abi() -> int`: the ABI version, or `0` when Landlock is missing or disabled, the platform is not Linux, or the syscall fails.
- `restrict_signals() -> tuple[Outcome, str]`. `Outcome` is an enum with these members:
  - `RESTRICTED`;
  - `UNSUPPORTED`: ABI < 6, or not Linux;
  - `NESTING_LIMIT`: `E2BIG` from `restrict_self`;
  - `FAILED`: any other errno; the `str` names the step and its errno text.
- `scoped_argv(argv: list[str]) -> list[str]`: `[sys.executable, "-I", "-S", <this file>, "--", *argv]`.
- `main(argv: list[str], *, restrict=restrict_signals, exec_fn=os.execvp, stderr=sys.stderr) -> int`. It parses `-- CMD [ARGS…]`; a missing command is usage error exit 2.
  - With `RIG_SIGNAL_SCOPE=0` in the environment, it execs the command unrestricted.
  - Otherwise it calls `restrict()`. On `RESTRICTED` it sets `RIG_SIGNAL_SCOPED=1`. On `NESTING_LIMIT` it says nothing, because the process is already contained. On `UNSUPPORTED` or `FAILED` it writes one line to stderr, `rig signal-scope: not contained (<detail>); starting <cmd> unrestricted`, and still execs. **The session always starts (Ruling R4, the same rule as the cgroup launcher).**
  - If the exec itself fails, it prints `rig signal-scope: cannot exec <cmd>: <err>` and returns 127.
- The file imports only the stdlib (`ctypes`, `enum`, `os`, `sys`) and never imports `riglib`.

## 9. Configuration, rollout, rollback

```yaml
# ~/.config/rig/config.yaml (GLOBAL-only, like the whole harness_wrappers block)
harness_wrappers:
  signal_scope: [omp]     # default; kinds whose launch through rig runs inside a Landlock signal domain
```

- `signal_scope` takes a list of harness kinds from `omp`, `claude`, `codex` and `opencode`; `[]` turns it off. The default is `[omp]` (**Ruling R6**: claude, codex and opencode were not measured, so they are opt-in). The key acts on **every rig launch path** (§5.3), independent of `harness_wrappers.enabled`, which only controls whether the PATH wrapper scripts get installed.
- Policy lives in `riglib/wrapper_provision.py` as `signal_scope_enabled(kind, env, *, platform=sys.platform, config_path=None) -> bool`, and the mechanism lives in `signal_scope.py`. The function returns false unless `platform == "linux"`, and false when `env["RIG_SIGNAL_SCOPE"] == "0"`. Otherwise it returns `kind in settings.signal_scope`, where the settings come from `read_yaml_file(config_path or global_config_path())`, the same runtime read as `cli.py:917-918`. A missing, unreadable or invalid file falls back to the default `[omp]`; a config problem must never block a launch or silently drop the protection.
- **Rollout.** Tasks 1–2 add the helper and the config, and nothing calls them yet. Next comes the gate: Tasks 3–5, plus tg-cli#748 deployed. Then Task 6 wires rig's launch paths and makes the default live, Task 7 switches the home launcher, and Task 8 is the live proof.
- **Rollback.** For one launch: `RIG_SIGNAL_SCOPE=0 omp …`. Permanent: `rig config set harness_wrappers.signal_scope '[]' --global`. A running session cannot leave its domain; restart it to drop the restriction.
- **Ruling R13 (home launcher).** If #692 has merged, its rig-generated launcher calls the helper. If it has not, Task 7 edits the three exec lines of the hand-written `~/.local/bin/omp` (33, 50, 53) to go through the helper, and #692 carries that prefix into its rig-managed version.

## 10. macOS and other platforms (Ruling R9)

macOS has no Landlock. `signal_scope_enabled` returns false off Linux, so the helper is never inserted and the real binary starts exactly as before. There, the defence is L1 (and L3). Linux kernels without Landlock ABI ≥ 6 still insert the helper, which warns once per launch and execs unrestricted (§8).

## 11. Tests

- `tests/test_signal_scope.py`
  - Unit tests with fake `restrict` and `exec_fn`: argv shape; kill switch; the `RIG_SIGNAL_SCOPED` marker; fail-open warning text for `UNSUPPORTED` and `FAILED`; silence for `NESTING_LIMIT`; exit 2 for usage; 127 when exec fails.
  - Kernel tests, skipped unless `landlock_abi() >= 6`:
    - a contained child gets `PermissionError` from `os.kill(<outside canary>, 0)` and from `SIGTERM`, and the canary survives;
    - a contained child kills its own child;
    - the incident pattern, `kill -TERM -1` from a contained `bash -c`, leaves the outside canary alive.
- `tests/test_harness_launcher.py` / `tests/test_harness_host.py`: the non-interactive exec and the host spawn use `scoped_argv` exactly when the spec says to; `HostSpec` round-trips `signal_scope`; the relaunch argv stays unprefixed.
- `tests/test_harness_wrappers_apply.py`: config validation for `signal_scope`, and `signal_scope_enabled` over platform, env and config (missing, `[]`, invalid).
- Live proof on home after rollout (#704 acceptance): an omp session started through the launcher runs the incident command from its bash tool; an outside canary and `systemd --user` survive.

## 12. Rulings

| # | Ruling | Why |
|---|---|---|
| R1 | Restrict the whole omp process, not the tool shell | The shell and `kill`/`pkill` are in-process builtins (§5.1) |
| R2 | One domain per session | A shared domain would still let one session kill all the others |
| R3 | Stdlib helper run by path as an argv prefix | Threads in the host rule out `preexec_fn`; injected `exec_fn` in tests rules out an in-process restrict; no package import cost |
| R4 | Fail open; `E2BIG` is silent | A session must always start; at the nesting limit the process is already contained |
| R5 | `harness_wrappers.signal_scope` (GLOBAL), `RIG_SIGNAL_SCOPE=0` per launch | Same block as the #692 cgroup option; per-launch rollback needs no apply |
| R6 | Default `[omp]` only | Only omp was measured |
| R7 | No custom privilege broker; `systemd-run --user --pipe --wait` is the escape hatch; extract AppImages | The existing IPC path is verified (c3); a broker adds attack surface for no gain under §2's model |
| R8 | Threat model: accidents, not adversaries; IPC kill vectors are L1's | Landlock does not mediate D-Bus or tmux sockets |
| R9 | macOS and Windows: not inserted | No Landlock there |
| R10 | Wiring the launch paths (the moment the default goes live) waits for tg-cli#748, the tmux.py fix, the orcaslicer-cli AppImage fix and the filed omp upstream issue; the omp broker prune is a documented known limitation | The gated items silently break steering, locks and the 3d slicer; the omp one costs only service control |
| R11 | Export `RIG_SIGNAL_SCOPED=1` | Makes EPERM explainable to agents and humans |
| R12 | Spec in `docs/specs/`, plan in `docs/superpowers/plans/` | Repo spec convention; no repo plan directory exists, so the writing-plans default applies |
| R13 | Home gets L2 through #692's launcher, or an interim edit of `~/.local/bin/omp` if #692 is not merged | The live launcher is that file today |
