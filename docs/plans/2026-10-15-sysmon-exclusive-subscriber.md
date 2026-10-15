# sysmon: exclusive subscriber + handler session — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** With `sysmon.alert.route: subscriber`, alerts reach only the tg-ctl exclusive holder of source `sysmon`. When there is no holder, sysmon raises one interactive omp handler in tmux, and it retires that handler when things go quiet. sysmon itself writes to Telegram only when there is no holder and the handler could not be raised.

**Architecture:** `alert.send` gains the `subscriber` route, which calls `tg-ctl alert send --to-holder` and maps its exit codes. A new module, `riglib/sysmon_daemon/handler.py`, holds the pure spawn/fallback/startup/retire decisions, the argv builders, the brief, and thin tmux/tg-ctl I/O wrappers. `Monitor.evaluate` calls `handler.tick` after the heal ladder. Config, validation, schema, wizard and docs follow the repo's existing sysmon pattern. `route: agents` and its broadcast code are removed.

**Tech Stack:** Python 3.10+ stdlib (`riglib` imports stdlib only), pytest.

**Spec:** `docs/specs/2026-10-15-sysmon-exclusive-subscriber-design.md`. Ticket: rig-cli#700. Depends on tg-cli#747 (plan: tg-cli `docs/plans/2026-10-15-alert-exclusive-holder.md`), which must be merged and deployed first.

## Global Constraints

- **Start only after** rig-cli#699/#685 are merged; rebase onto them. Do not edit `alert.decide`, `detect.py` or the swap detector.
- Work in `rig worktree create sysmon-subscriber-700`. Tests: `python3 -m pytest -q tests/test_sysmon_daemon.py tests/test_sysmon.py tests/test_sysmon_handler.py`. The machine is memory-constrained: run no broader suite locally unless the repo's CI requires it (CI runs it).
- `route` values: `telegram` (default) and `subscriber`. `agents` is rejected with `sysmon.alert.route: agents was renamed to subscriber (rig-cli#700)`.
- `route: subscriber` together with `auto_remediate: true` → `ConfigError` `sysmon.auto_remediate cannot be on with alert.route subscriber: the handler session replaces it (rig-cli#700)`.
- `handler` defaults: `tmux_session: null` (→ `sessions.tmux_session`), `window: "sysmon-handler"`, `workdir: "~"`, `warn_persist_minutes: 15`, `spawn_cooldown_minutes: 60`, `startup_timeout_minutes: 5`, `idle_exit_minutes: 30`, `dry_run: false`.
- tg-ctl exit codes consumed: `0` ok, `1` error, `3` vacant or conflict, `5` holder suspended or delivery failed.
- Liveness reads (`tg-ctl alert holder --source sysmon --json` and one `tg-ctl omp-live status --json`) happen at most once per `census_interval_seconds` and are cached in `state["holder"]` (with the handler's `busy`); retire and fallback decisions use the cache.
- Nothing new may raise out of `Monitor.tick`: every subprocess goes through a never-raising helper (the `alert._run` pattern).
- Handler kickoff command (exact): `tg-ctl alert subscribe --exclusive --source sysmon --ttl 0`.
- Telegram fallback title for a failed start (exact): `sysmon handler did not start`.
- Telegram fallback is CRITICAL-only (spec ruling 17): a `warning` alert never goes to Telegram from any branch (old tg-ctl, unknown holder, failed spawn, cooldown); its record is `outcome: undelivered`, `reason: warning-no-telegram` and its keys are queued as pending. `sysmon handler did not start` is posted only when the finding that triggered the spawn was CRITICAL (`state["spawn_severity"]`).

## Review Focus

1. A tg-ctl older than tg-cli#747 rejects `--to-holder` (exit 1, `unknown flag`), so a CRITICAL alert must go to Telegram and not disappear; a WARNING is recorded `warning-no-telegram` and queued. Test in Task 2/4.
2. `tg-ctl alert holder` fails or prints unreadable JSON: no spawn (a handler could not subscribe anyway), and due CRITICAL problem alerts take the Telegram fallback (WARNINGs are queued). Test in Task 3.
3. The configured tmux session does not exist (`has-session` exit 1): spawn uses `new-session -d -s <S> -n sysmon-handler`. Test in Task 3.
4. The user closes the handler window by hand: no respawn before the cooldown ends, and none afterwards unless the trigger still holds. Test in Task 3.
5. The holder is the user's own session (not the spawned window): retire never touches it, even when it has been idle for hours. Test in Task 3.

---

### Task 1: Config — `route: subscriber`, `handler` block, `agents` removed

**Files:**
- Modify: `riglib/sysmon_daemon/config.py` (`DEFAULTS["alert"]["route"]` comment; new `DEFAULTS["handler"]`)
- Modify: `riglib/config.py` (`_validate_sysmon`, `_SYSMON_INTS`, `_SYSMON_BOOLS`, `_SYSMON_STRS`, route check; `"handler"` added to the per-section mapping loop)
- Modify: `riglib/sysmon.py` (`runtime_config`: merge `handler` like the other sections)
- Modify: `riglib/config_schema.py` (`alert.route` enum `("telegram", "subscriber")`; new `handler` Block)
- Modify: `riglib/schema.py` (wizard `_opt` for `sysmon.alert.route` choices; new `_opt("sysmon.handler.tmux_session", KIND_STR, None, …)`)
- Modify: `schema/rig.schema.json` (regenerate: `python3 bin/rig schema --write`)
- Modify: `docs/config-schema.md` (sysmon example block; replace the `alert.route: agents` paragraph with the subscriber/handler description)
- Test: `tests/test_sysmon.py`

**Interfaces:**
- Produces: `cfg["alert"]["route"] in ("telegram", "subscriber")`; `cfg["handler"]` with the Global Constraints keys; `handler.tmux_session` resolved by `handler.tmux_session(cfg) -> str` (Task 3).

- [ ] **Step 1: Write the failing tests**:
  - `test_alert_route_subscriber_and_handler_render_into_sysmon_json`: block `{"alert": {"route": "subscriber"}, "handler": {"tmux_session": 0, "idle_exit_minutes": 45}}` validates (an integer session name, as `rig config set` coerces `0`, is accepted); rendered `rt["alert"]["route"] == "subscriber"`, `rt["handler"]["tmux_session"] == "0"` (always a string in `sysmon.json`), `rt["handler"]["spawn_cooldown_minutes"] == 60`.
  - `test_alert_route_agents_is_rejected_with_the_new_name`: `ConfigError` whose message contains `renamed to subscriber`.
  - `test_subscriber_route_rejects_auto_remediate`: `{"auto_remediate": True, "alert": {"route": "subscriber"}}` → `ConfigError`.
  - `test_handler_keys_are_validated`: each of `{"warn_persist_minutes": 0}`, `{"spawn_cooldown_minutes": "x"}`, `{"dry_run": 1}`, `{"tmux_session": " "}`, `{"tmux_session": -1}`, `{"tmux_session": True}`, `{"bogus": 1}` → `ConfigError`.
  - Replace the rig-cli#696 test `test_alert_route_validates_and_renders_into_sysmon_json` (it asserted `agents`).
- [ ] **Step 2: Run** `python3 -m pytest -q tests/test_sysmon.py` — Expected: FAIL.
- [ ] **Step 3: Implement** the config, validation, schema leaves and wizard option; regenerate the JSON schema; update the docs paragraph.
- [ ] **Step 4: Run** `python3 -m pytest -q tests/test_sysmon.py` and `python3 bin/rig schema --check`. Expected: PASS, and `in sync`.
- [ ] **Step 5: Commit** `feat(sysmon): alert.route subscriber and the handler config block; route agents removed (Refs #700)`.

### Task 2: `alert.send` — subscriber route, broadcast removed

**Files:**
- Modify: `riglib/sysmon_daemon/alert.py` (remove `_to_agents`, `agent_text`, `AGENT_ACTION`, `AGENT_SEND_*`, `_is_pid`, `ThreadPoolExecutor` import; `ROUTES = ("telegram", "subscriber")`; add `_to_holder`; make `_to_telegram` public as `send_telegram`; module docstring)
- Test: `tests/test_sysmon_daemon.py` (delete the `alert.route: agents` tests; add the ones below)

**Interfaces:**
- Produces:
  - `holder_argv(tg_ctl: str, title: str, body: str, severity: str, key: str) -> List[str]` = `tg_ctl_argv(...) + ["--to-holder"]`.
  - `send(title, body, cfg, *, severity, key, run=subprocess.run) -> Dict[str, Any]`. With route `subscriber` the record has `via: "holder"` and `outcome` ∈ `sent` (exit 0) | `deferred` (exit 5) | `undelivered` with `vacant: True` (exit 3) | `error` (any other exit, or tg-ctl missing). `deferred` and `undelivered` records carry `detail` (tg-ctl stdout/stderr, ≤ 300 chars). No fallback happens inside `send`: the fallback decision belongs to the daemon (Task 4).
  - `send_telegram(title, body, cfg, *, severity, key, run=subprocess.run) -> Dict[str, Any]`: today's `_to_telegram` path, unchanged.

- [ ] **Step 1: Write the failing tests**:
  - `test_route_subscriber_delivers_to_the_holder_only`: the fake runner returns exit 0 with `OK holder s-1 pid 11`. Exactly one call, `argv[-1] == "--to-holder"`, `alert.flag_value(argv, "--source") == "sysmon"`. Record: `via == "holder"`, `outcome == "sent"`. No argv with `tg` or without `--to-holder`.
  - `test_route_subscriber_maps_exit_codes` (parametrized `(3, "undelivered", True)`, `(5, "deferred", None)`, `(1, "error", None)`): record `outcome` as listed; `rec.get("vacant")` as listed; exactly one call.
  - `test_route_subscriber_old_tg_ctl_is_an_error_not_silence`: exit 1 with stderr `tg-ctl alert: unknown flag: --to-holder` gives `outcome == "error"` (the daemon's fallback then applies, Task 4).
  - `test_route_subscriber_dry_run_runs_nothing`: `dry_run=True` gives `outcome == "dry_run"` and `rec["argv"][-1] == "--to-holder"`.
  - `test_send_telegram_is_the_tg_ctl_alert_path`: same assertions as the existing `test_send_goes_through_tg_ctl_alert_with_severity_key_and_html_body`, but called through `send_telegram`.
- [ ] **Step 2: Run** `python3 -m pytest -q tests/test_sysmon_daemon.py -k "route or send_telegram"`. Expected: FAIL.
- [ ] **Step 3: Implement** in `alert.py`.
- [ ] **Step 4: Run** `python3 -m pytest -q tests/test_sysmon_daemon.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(sysmon): route subscriber sends to the tg-ctl exclusive holder; the all-session broadcast is removed (Refs #700)`.

### Task 3: `handler.py` — decisions, argv, brief, I/O wrappers

**Files:**
- Create: `riglib/sysmon_daemon/handler.py`
- Test: `tests/test_sysmon_handler.py`

**Interfaces:**
- Consumes: `cfg["handler"]`, `cfg["sessions"]["tmux_bin"|"omp_bin"|"tmux_session"]`, `Finding` (severity/key), `config.expand`.
- Produces:
  - `@dataclass(frozen=True) class Holder: state: str  # "bound"|"suspended"|"vacant"|"unknown"; session_id: Optional[str]; pid: Optional[int]; busy: Optional[bool]`
  - `tmux_session(cfg) -> str`
  - `update_first_seen(findings: Sequence[Finding], state: dict, now: float) -> dict`: keeps `first_seen[key]` for keys present at WARNING or above and drops the rest.
  - `spawn_due(findings, state, now, cfg, *, holder: Holder, window_exists: bool, post_crash: bool) -> Tuple[bool, str]`. True only when `holder.state == "vacant"`, the window does not exist, `now - last_spawn_at ≥ cooldown`, and (any CRITICAL, or any WARNING with `now - first_seen[key] ≥ warn_persist_minutes*60`, or `post_crash`). The reason string names the rule that blocked or fired.
  - `telegram_fallback_due(state, now, cfg, *, holder: Holder, window_exists: bool) -> bool`. True when the holder is `unknown` (a broken channel, spec ruling 14), or when it is `vacant` and at least one of these holds: the last spawn failed; the cooldown is active and the window does not exist; the window has existed longer than `startup_timeout_minutes` with no holder.
  - `find_handler_endpoint(pane_pid: int, endpoints: Sequence[dict], is_descendant: Callable[[int, int], bool]) -> Optional[dict]`: the omp-live endpoint whose pid is `pane_pid` or a descendant of it; the caller stores its `sessionId`/`pid` as `state["handler_session_id"]`/`state["handler_pid"]` (whether or not it holds the slot).
  - `startup_failed(state, now, cfg, *, holder: Holder) -> bool`: True only when `now - last_spawn_at ≥ startup_timeout_minutes*60` and the holder is still `vacant`. Another session holding (`bound`/`suspended`) is success.
  - Pending: `state["pending"] = {"keys": {finding_key: first_deferred_at}, "crash": {"title", "body", "at"} | None}`. `queue_pending(state, finding_keys: Sequence[str], now, *, crash: Optional[dict] = None) -> dict` records each individual finding key (at most 20 keys, oldest evicted) and, for a post-crash alert, the full crash title/body. `due_pending(state, findings, now, cfg) -> Tuple[List[Finding], Optional[dict], dict]` returns the CURRENT findings at WARNING or above whose keys are pending (the caller re-renders them with `alert.render` and sends one fresh batch, so cleared findings and stale offender lists are never replayed) and the crash item (replayed verbatim, since the crash finding does not recur); the returned state drops only keys that vanished or are older than `alert.disk_cooldown_minutes` (the longest alert window, so a deferred disk alert is not lost to its own 6 h dedup; the presence filter keeps anything stale out). `ack_pending(state, finding_keys: Sequence[str], *, crash: bool) -> dict` removes keys (and the crash item) after a replay came back `sent`, or after the Telegram fallback for it succeeded; a replay that is deferred or fails stays pending. `info` alerts are never queued.
  - `retire_due(findings, state, now, cfg, *, holder: Holder, handler_busy: Optional[bool]) -> bool`. Requires all of: `state["handler_pid"]` set (sysmon spawned it and it registered); no finding at WARNING or above for `idle_exit_minutes`; `now - last_delivery_at ≥ idle_exit_minutes*60`; and `handler_busy is False`. `handler_busy` is the omp-live `busy` of `handler_pid`, whether or not it still holds the slot (after a handoff it is still checked). `None` (unknown) never retires.
  - `spawn_argv(cfg, brief: Path, kickoff: str, *, session_exists: bool) -> List[str]`: `tmux new-window -d -P -F '#{window_id} #{pane_pid}' -t =<S>: -n <window> -c <workdir>`, or `new-session -d -P -F '#{window_id} #{pane_pid}' -s <S> -n <window> -c <workdir>`, followed by `<abs omp> --auto-approve --append-system-prompt <brief> <kickoff>`. `spawn` parses the printed `@<n> <pid>` into `state["window_id"]` and `state["pane_pid"]`.
  - `retire_argvs(cfg, window_id: str, handler_pid: int, handler_session_id: str, holds: bool) -> List[List[str]]`: `[tg-ctl alert release --source sysmon --session <handler_session_id>]` (only when `holds`), then `[tg-ctl omp-live send <handler_pid> --session <handler_session_id> --request-id sysmon-handler-retire-<ms> -- "[sysmon] Nothing to handle for 30 minutes: this handler window is being closed."]`, then `[tmux kill-window -t <window_id>]`. `omp_busy` and every retire step first confirm that omp-live lists `handler_pid` with `handler_session_id` (a reused pid is not the handler: record `skipped`, clear the state). `retire` runs the kill only after `tmux display-message -p -t <window_id> '#{pane_pid}'` prints the stored `pane_pid`; otherwise it records `skipped: window <id> is not the one sysmon spawned` and only clears the state. Retire never kills by window name and never by a value that can be empty.
  - `build_brief(cfg) -> str`: sections 1–9 of spec *Handler brief*, in order, with the exact commands from the spec.
  - `kickoff_text(alert_text: str) -> str`: `"[sysmon] You are the sysmon handler. First run: tg-ctl alert subscribe --exclusive --source sysmon --ttl 0\n" + alert_text`.
  - I/O (never raise; use a `_call(argv, run) -> Tuple[bool, str]` like `heal._call`): `read_holder(cfg, run) -> Holder` (state `"unknown"` on an error, a non-JSON reply, or an exit other than 0/3; `busy` from `tg-ctl omp-live status --json` for the holder pid), `omp_busy(pid, session_id, run) -> Optional[bool]` (`None` unless omp-live lists exactly that pid WITH that session id), `window_pane_pid(cfg, run) -> Optional[int]`, `session_exists(cfg, run) -> bool`, `spawn(cfg, state, now, alert_text, run) -> Tuple[dict, dict]` (writes `<state_dir>/handler/brief.md`, runs `spawn_argv` unless `dry_run`, returns `(record, new_state)`), and `retire(cfg, state, holder, run) -> dict` (uses `state["handler_pid"]` and `state["handler_session_id"]`).
  - `tick(findings, state, now, cfg, run, *, post_crash: bool, alert_text: str) -> Tuple[dict, List[dict]]`: the one entry point Task 4 calls; it re-reads the holder only when `now - last_vacancy_check_at ≥ census_interval_seconds` (else reuses `state["holder"]`), then runs update_first_seen → startup check → spawn_due/spawn → retire_due/retire. Returns the new state and records for `_log("handler", …)`.

- [ ] **Step 1: Write the failing tests** (pure functions take an injected clock and fake runners):
  - `test_spawn_needs_vacancy_and_a_trigger`: a CRITICAL finding with `holder=vacant` gives True. The same finding with `bound`, `suspended` or `unknown` gives False.
  - `test_warning_must_persist_15_minutes`: a WARNING first seen at t gives False at t+14:59 and True at t+15:00. The key clearing and returning resets the clock.
  - `test_spawn_cooldown_and_window_block`: `last_spawn_at = now - 59 min` gives False and `- 60 min` gives True. `window_exists=True` gives False (Review Focus 4: a window the user closed is not respawned inside the cooldown).
  - `test_post_crash_triggers_spawn`.
  - `test_fallback_only_when_the_handler_cannot_help`: holder `unknown` on a fresh state gives True. Failed last spawn gives True. Cooldown active with no window gives True. Window younger than 5 min gives False. Window older than 5 min with no holder gives True. Holder bound gives False. Holder suspended gives False.
  - `test_startup_failed_only_when_still_vacant`: holder vacant at +4:59 gives False, at +5:00 gives True. Another session bound at +5:00 gives False. `find_handler_endpoint` picks the endpoint whose pid (101) is a child of pane 100.
  - `test_pending_replays_only_current_findings`: keys `load` and `swap` pending; the current findings hold only `swap` at WARNING, so `due_pending` returns `[swap finding]` and the returned state still holds `swap` but not `load`. `ack_pending(state, ["swap"], crash=False)` then empties it. With no current findings it returns `[]` and clears both. A 21st key evicts the oldest. A key older than the cooldown is dropped. A crash item is returned even with no current findings and stays until `ack_pending(..., crash=True)`.
  - `test_retire_only_the_spawned_idle_handler` (Review Focus 5): no `handler_pid` in state (the holder is the user's own session) gives False. All conditions met with `handler_busy=False` gives True. `handler_busy=True` gives False, and so does `None`. A finding at WARNING gives False. After a handoff (holder is another session), the idle handler is still retired, and `retire_argvs(..., holds=False)` contains no `release`. With a wrapper (pane 100, omp 101 registered as `handler_pid`), release and send target 101 and the kill targets the stored `window_id`.
  - `test_retire_skips_a_window_that_is_not_the_spawned_one`: `display-message` for the stored `window_id` prints a different pane pid (or fails). No `kill-window` argv runs, the record says `skipped`, and the state is cleared. Likewise when omp-live lists `handler_pid` with a different session id (a reused pid): no release, no send, no kill.
  - `test_spawn_argv_new_window_and_new_session` (Review Focus 3): exact argv for `session_exists` True and False, with `-t =0:`, `-n sysmon-handler`, `--auto-approve`, `--append-system-prompt`.
  - `test_brief_has_every_section_and_the_never_list`: `build_brief` contains `tg-ctl alert subscribe --exclusive --source sysmon --ttl 0`, `docker builder prune -f`, `memguard swap-drain`, `tg --tag decision`, `tg --tag problem`, `tg --tag report`, `swapoff`, `[ -n "$P" ] && ps -p "$P"`, and `Never`.
  - `test_read_holder_unknown_on_errors` (Review Focus 2): exit 1, exit 0 with `not json`, or an `OSError` each give `Holder.state == "unknown"`.
- [ ] **Step 2: Run** `python3 -m pytest -q tests/test_sysmon_handler.py`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** `handler.py` with a file header comment that states why it exists (rig-cli#700).
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `feat(sysmon): handler session decisions, spawn/retire argv and brief (Refs #700)`.

### Task 4: Daemon wiring — fallback, handler tick, records

**Files:**
- Modify: `riglib/sysmon_daemon/daemon.py`. Add `Monitor._deliver(title, body, *, severity, key, finding_keys: Sequence[str], crash: bool = False) -> Dict[str, Any]`, the only alert delivery call site. It runs `alert.send`. For a problem alert with `route: subscriber`, two cases add a Telegram send: if the outcome is `error`, or `undelivered` while `handler.telegram_fallback_due(...)` is true, it also runs `alert.send_telegram` and marks the record `fallback: "telegram"`. Every other `deferred` or `undelivered` problem alert is queued with `handler.queue_pending(state, finding_keys, now, crash={"title","body","at"} if crash else None)`, so a handler spawned later in the same tick receives it once it holds the slot. After `handler.tick`, whenever the holder is bound, the daemon takes `handler.due_pending(...)`, re-renders the returned findings with `alert.render` and sends that batch (and the crash item verbatim) through `_deliver` itself, so a replay that is deferred again or fails keeps its keys and gets the same fallback; `handler.ack_pending` runs when the replay's holder outcome is `sent` or its Telegram fallback was `sent` (so a broken holder channel posts each pending alert to Telegram once, not every tick). `evaluate` (problem and resolved alerts) and `check_crash` call `_deliver` instead of `alert.send`. `check_crash` also passes `post_crash=True` into the next `handler.tick`. Call `handler.tick` after the heal ladder. The remediation call stays as is, because the validator forbids `auto_remediate` with `subscriber`. The daemon also picks up a spawn request file `<state_dir>/handler/spawn-request` (written by Task 5): if it exists and no window exists, it spawns, ignoring the trigger and the cooldown, then deletes the file.
  - Severity gate (spec ruling 17): both Telegram branches apply only when `severity == "critical"`. For a `warning` the record gets `reason: "warning-no-telegram"` and the keys are queued as pending; no `send_telegram` call.
- Modify: `riglib/sysmon_daemon/model.py` only if a shared type is needed (prefer none)
- Test: `tests/test_sysmon_daemon.py` (use the existing `_monitor`/`_Run` helpers; `_Run` gets scripted exit codes per argv prefix)

**Interfaces:**
- Consumes: `alert.send`, `alert.send_telegram`, `handler.tick`, `handler.telegram_fallback_due`, `handler.read_holder`, `handler.window_pane_pid`.
- Produces: `memory["handler"]` state. `alerts` log records that took the fallback carry `fallback: "telegram"`. A `recent_handler` log exists. The open incident records `handler`.

- [ ] **Step 1: Write the failing tests**:
  - `test_subscriber_route_bound_holder_gets_the_alert_and_no_telegram`: `holder` returns bound and `send --to-holder` exits 0. Over one hot tick, no `tg-ctl alert send` call lacks `--to-holder`, and no tmux call is made.
  - `test_subscriber_route_vacant_critical_spawns_once`: `holder` exits 3 and the tmux `list-panes` fails (no window). One tick gives exactly one `new-window` call and `memory["handler"]["last_spawn_at"] == now`. A second tick with the window present gives no new spawn.
  - `test_subscriber_route_vacant_fresh_warning_neither_spawns_nor_posts`: the first tick on a WARNING makes no tmux call and no Telegram post. The alert record has `outcome == "undelivered"` and its finding key is queued as pending.
  - `test_vacant_critical_is_queued_and_reaches_the_spawned_handler`: tick 1: `--to-holder` exits 3, no window, so the alert is queued and the same tick spawns. Tick 2 (+2 min): the holder is bound and the finding is still present, so exactly one `--to-holder` replay with that key.
  - `test_deferred_alert_is_redelivered_when_the_holder_returns`: tick 1 `--to-holder` exits 5, so the record is `deferred` and queued. Tick 2: holder bound, but the replay exits 5 again, so the key is still queued. Tick 3: the replay exits 0, so there is exactly one successful `--to-holder` send with the key and the queue is empty. No Telegram send in any tick.
  - `test_failed_start_posts_to_telegram_once`: spawn on a CRITICAL at t, no holder at t+5 min. Exactly one `tg-ctl alert send` without `--to-holder`, with `--title` containing `sysmon handler did not start`. Later due CRITICAL alerts take the fallback.
  - `test_failed_start_after_warning_spawn_never_posts`: spawn triggered by a 15-minute WARNING, no holder at t+5 min: no Telegram call; the record notes the failed start; a later due WARNING is `undelivered`, `reason == "warning-no-telegram"`.
  - `test_resolved_notice_never_falls_back`: `--to-holder` exits 3 on an `info` send, so no Telegram call.
  - `test_old_tg_ctl_falls_back_to_telegram`: a CRITICAL `--to-holder` exits 1 with `unknown flag`, so one Telegram send follows; the same with a WARNING gives no Telegram send and `reason == "warning-no-telegram"`.
  - `test_tick_never_raises_when_tmux_and_tg_ctl_are_missing`: `run` raises `FileNotFoundError` for every argv, and `tick` returns.
  - `test_post_crash_alert_takes_the_fallback_on_an_old_tg_ctl`: `check_crash` with `--to-holder` exiting 1 makes exactly one Telegram send.
  - `test_spawn_request_file_is_honoured_once`: request file present and no window: one `new-window`, and the file is deleted. Request file present and the window exists: no spawn, and the file is deleted with a `refused: window exists` record.
  - `test_simulation_never_spawns_or_calls_tg_ctl`: `daemon.simulate(cfg, [critical finding])` with `route: subscriber` makes no `new-window`/`new-session`/`kill-window` call and no `tg-ctl` call (the existing census may still list panes). `simulate` forces `handler.dry_run` like it forces `alert.dry_run`; a dry-run `handler.tick` runs no subprocess at all, treats the holder as `vacant` and records `would_spawn`.
- [ ] **Step 2: Run** `python3 -m pytest -q tests/test_sysmon_daemon.py -k subscriber`. Expected: FAIL.
- [ ] **Step 3: Implement** the wiring in `evaluate` (and `check_crash`).
- [ ] **Step 4: Run** `python3 -m pytest -q tests/test_sysmon_daemon.py tests/test_sysmon_handler.py tests/test_sysmon.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(sysmon): route subscriber spawns and retires the handler session; Telegram only when it cannot (Refs #700)`.

### Task 5: CLI `rig sysmon handler status|spawn`

**Files:**
- Modify: `riglib/sysmon_daemon/cli.py` (new `handler` subparser with `status` and `spawn`, built with `add(...)` like the others)
- Modify: `README.md` (`rig sysmon` section) and `docs/config-schema.md` (commands line)
- Test: `tests/test_sysmon_daemon.py`

**Interfaces:**
- Consumes: `handler.read_holder`, `handler.window_pane_pid`, the `memory.json` `handler` state (read only).
- Produces: `status` prints `holder: <state> <session> pid <pid>`, `window: <S>:<window> <window_id> pane <pid>|absent`, and `last spawn: <iso>|never <ok|failed>`, then exits 0. `spawn` never spawns by itself, because the daemon owns the handler state. It refuses with exit 3 and `handler window exists` when the window is present. Otherwise it writes `<state_dir>/handler/spawn-request`, waits up to `max(30, 2 * sample_interval_seconds + 10)` s for `window_pane_pid` to appear, prints `spawned: <S>:<window> pane <pid>` and exits 0. If the window has not appeared, it prints `requested; the daemon did not spawn within <N> s (is ai.hyperide.sysmon running?)` and exits 1.

- [ ] **Step 1: Write the failing tests**:
  - `test_cli_handler_status_prints_holder_window_and_last_spawn`.
  - `test_cli_handler_spawn_refuses_when_the_window_exists`.
  - `test_cli_handler_spawn_writes_a_request_and_reports_the_window`: the fake `window_pane_pid` is absent, then present. The request file is written, the output is `spawned: …`, exit 0.
  - `test_cli_handler_spawn_times_out_with_exit_1`: the window never appears (inject a zero wait), so exit 1 and the `is ai.hyperide.sysmon running?` hint.
- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement** both commands.
- [ ] **Step 4: Run** `python3 -m pytest -q tests/test_sysmon_daemon.py tests/test_sysmon_handler.py tests/test_sysmon.py`. Expected: PASS.
- [ ] **Step 5:** run `flock /tmp/disk-session-review.lock review diff --staged --task 700` (at most one review at a time on this machine), fix the findings, then commit `feat(sysmon): rig sysmon handler status|spawn (Refs #700)`.

### Task 6: Ship and live acceptance on *home*

- [ ] **Step 1:** push, open a PR (`Refs #700`), wait for CI green, squash-merge, `git pull` in the main checkout, `rig worktree remove sysmon-subscriber-700`.
- [ ] **Step 2:** `rig config set --global sysmon.alert.route subscriber --no-apply`, then `rig config set --global sysmon.handler.tmux_session 0 --no-apply`, then `rig apply commit --only sysmon --yes`. Expected: `verify pass`, and `systemctl --user is-active ai.hyperide.sysmon` prints `active`.
- [ ] **Step 3:** `rig sysmon handler spawn`. Expected: a window `0:sysmon-handler` appears, and within 2 min `tg-ctl alert holder --source sysmon --json` shows its pid as holder.
- [ ] **Step 4:** `rig sysmon test-alert`. Expected: record `via: holder, outcome: sent`. The handler window shows the `[alert sysmon …]` note. No new Telegram message appears in `tg-ctl` history.
- [ ] **Step 5:** from another omp session, run `tg-ctl alert subscribe --exclusive --source sysmon`. Expected: exit 3, naming the handler and printing the `handoff-request` command.
- [ ] **Step 6:** `tg-ctl alert release --source sysmon --pid <handler pid>`, then `tmux kill-window -t <window_id>` with the id printed by `rig sysmon handler status`, then `rig sysmon test-alert`. Expected: record `via: holder, outcome: undelivered, vacant: true`; no window is spawned (test-alert never spawns) and nothing reaches Telegram. The fallback branches are covered by the Task 4 tests.
- [ ] **Step 7:** `task check '#700' …` with proofs. Close rig-cli#698 as superseded (`task change '#698'` with the reason, then `task status '#698' done`). Run `task done '#700'`.

## Review amendments (binding; override the tasks above where they differ)

From the plan review (rig-cli-700). Each item names the task whose tests it extends.

1. **Spawn bookkeeping** (Task 3): `spawn` records `last_spawn_at`, `window_created_at`, `spawn_severity` and `last_spawn_ok: None`. The startup check sets `last_spawn_ok` to `True`/`False` once; `startup_failed` is False when `last_spawn_at` is unset. `telegram_fallback_due`'s "last spawn failed" reads `last_spawn_ok is False`, never recomputes it. Tests: never-spawned state gives `startup_failed` False and no fallback; the vacant-CRITICAL tests in Task 4 also assert no Telegram send.
2. **Failed-start notice** (Task 3/4): `tick` returns `(state, records, notices)`; a notice is `{title, body, severity}`. The startup check emits `sysmon handler did not start` once (guarded by `start_failure_notified`, reset at the next spawn) and only when `spawn_severity == "critical"`. The daemon sends notices with `alert.send_telegram`; handler.py never sends to Telegram itself.
3. **Foreign window** (Task 3): a `sysmon-handler` window that sysmon has no state for (or that survived a skipped retire) is never killed; its age comes from `tmux display-message -p -t <id> '#{window_created}'`, so the fallback rule "window older than `startup_timeout_minutes` with no holder" applies to it. Test `test_foreign_handler_window_with_no_state`.
4. **`--to-holder` placement** (Task 2): `holder_argv` inserts `--to-holder` before the `--` that precedes the body. The tests assert flag membership before `--` and an intact body, not `argv[-1]`.
5. **One holder cache** (Task 3/4): a single accessor `handler.holder(state, now, cfg, run)` reads `tg-ctl alert holder` at most once per census interval and is shared by `_deliver` and `tick`. A `--to-holder` outcome updates the cache: exit 0 → `bound` (so a successful delivery can never be followed by a spawn in the same tick), exit 3 → `vacant`, exit 5 → `suspended`. Test: cached `vacant`, delivery exits 0, no spawn.
6. **Delivery timestamp** (Task 4): `_deliver` sets `state["last_delivery_at"] = now` on every holder `sent`, including replays.
7. **Post-crash flag** (Task 4): `check_crash` sets `state["post_crash_pending"] = True`; `tick` treats it as a trigger and clears it after a spawn or when the holder is not vacant.
8. **Descendant check** (Task 3): the production `is_descendant(child, ancestor)` walks `/proc/<pid>/stat` PPid at most 16 levels and returns False on any read error. Tested against a real child process (`subprocess.Popen(["sleep", "30"])`, terminated in teardown by its own handle).
9. **Clock** (Task 3): `retire_argvs` takes `now_ms`.
10. **Pending TTL** (Task 3): pending keys expire after the longest of the configured alert cooldowns (computed), not a named single key.
11. **Record vocabulary** (Task 2/4): `undelivered` records carry `reason` ∈ `no subscriber`, `handler starting`, `warning-no-telegram`; `deferred` records carry `reason: holder suspended`. Task 4 consumes `handler.spawn` as well.
12. **One runner** (Task 3): handler.py reuses `alert._run` (renamed `run_safe` if it is imported across modules); no third copy.
13. **Accepted behaviour**: a holder alive but failing deliveries for 10 minutes is vacant by tg-ctl's rule; with the handler window present no second handler is spawned and a CRITICAL takes the Telegram fallback. That is intended: the channel is broken.
