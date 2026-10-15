# sysmon: one exclusive subscriber, a handler session when nobody is subscribed (design, 2026-10-15)

Status: **design, decisions delegated by the owner**. Tickets: rig-cli#700, tg-cli#747.
Plans: [rig-cli](../plans/2026-10-15-sysmon-exclusive-subscriber.md),
[tg-cli](https://git.hyperide.ai/ultrabricks/tg-cli/src/branch/main/docs/plans/2026-10-15-alert-exclusive-holder.md).
Builds on rig-cli#697 (`alert.route: agents`), lands after rig-cli#699/#685 (swap flapping and
pressure gating, in progress elsewhere), supersedes rig-cli#698.

## Understanding

### What the owner said

The repo is English-only, so these are line-by-line translations; the Russian originals are
quoted verbatim in rig-cli#700.

- tg#22094, 2026-10-15:
  "Telegram should get a message if an agent cannot solve the problem and needs help,
  or if significant changes were made to solve the problem.
  There must also be a mechanism for an agent to register and receive these notifications; only
  one agent can be subscribed at a time; if another wants to subscribe it must be told that
  there is another one,
  with the ability to ask it to hand over the subscription.
  If no agent is subscribed, an OMP session must be raised to handle system-resource problems."
- Reply to the design questions (tg#22096): "try to find the solution yourself" — every open
  decision is delegated. The orchestrating session ruled on the main forks (recorded in
  *Decisions*); the rest are this document's and are listed there as such.

### Intended outcome

Exactly one agent session owns the machine's resource alerts. Other sessions are not
interrupted by them. When no session owns them, sysmon raises a dedicated omp session that does.
The human's Telegram hears only from that agent, and only when it needs help or changed
something significant.

### Constraints

- No second delivery mechanism: sessions are reached through `tg-ctl omp-live` (what
  mem-sentinel, `omp-steer` and rig-cli#697 use) and identified by the omp **session id**
  (persists across `omp --resume`), as tg-cli#636 subscriptions already are.
- sysmon must stay up: no new failure may raise out of the sampling loop (rig-cli#697 review).
- Under memory pressure each extra process counts: no new daemon; liveness costs at most two
  `tg-ctl` calls (`alert holder`, `omp-live status`) per census interval, cached between.
- 2026-10-15 ~20:33: seven omp sessions died during a `swapoff`. The handler never runs
  `swapoff`/`swapon` itself.
- rig-cli sysmon swap alerting (#699/#685) is being changed by another agent; this work does not
  touch `alert.decide` or the detectors.

### Success criteria

1. With `route: subscriber` and a live holder, a due alert reaches the holder and nothing else.
2. A second session asking to subscribe learns who holds the slot and how to ask for it.
3. With no holder, a critical finding or a warning that persists 15 minutes raises one handler
   session within one census interval (60 s); no second one while it exists, at most one per hour.
4. sysmon itself writes to Telegram only for a CRITICAL alert, and only when there is no holder
   and the handler could not be raised (failed, or on cooldown), or the holder channel is broken.
   A WARNING never reaches Telegram (ruling 17).

## Decisions

Rulings from the orchestrating session (owner-delegated):

- **D1**: the exclusive slot lives in tg-ctl's alert subscription store (tg-cli), not in sysmon.
- **1a**: the handler is a visible, interactive omp in tmux window `sysmon-handler`, and it holds
  the subscription.
- **2a**: the holder decides a handoff request; holder silent for 10 minutes, or dead → the
  requester may take the slot.
- **3a**: spawn only on CRITICAL, or WARNING persisting ≥ 15 minutes; max 1 spawn per hour; no
  spawn while a handler window exists. The handler runs `--auto-approve` with a narrow brief
  (*Handler brief*).
- **Telegram**: only the agent posts (`problem` when it needs help, `report` after significant
  changes); sysmon posts only a CRITICAL alert, and only when there is no subscriber AND the
  spawn failed or is on cooldown (or the holder channel is broken).

Rulings made in this document (each is restated where it applies):

1. Exclusivity is **per source** (`sysmon` today); one holder per source.
2. The holder record sits in the existing `alert-subscriptions.json` as a new `holders` list; the
   non-exclusive subscriptions are untouched and holder-only sends ignore them.
3. `alert.route: agents` (rig-cli#697) is removed, not kept beside `subscriber` (clean cutover).
   The rig default stays `telegram` for machines without this tg-ctl.
4. `route: subscriber` with `auto_remediate: true` is a config error: both would spawn an agent
   for the same incident.
5. Spawn is decided every tick from the current findings, not from the alert-due list, so a
   warning that persists keeps qualifying even when its alert is in cooldown.
6. The handler subscribes itself; sysmon never writes the holder record.
7. sysmon, not the handler, ends the handler (release + `tmux kill-window`), and only the window
   it spawned.
8. The handler's tmux session is `sysmon.handler.tmux_session`, defaulting to
   `sysmon.sessions.tmux_session` (`main`). On *home* the attached session is `0`; rollout sets
   `handler.tmux_session: "0"` there so the window is visible.
9. The brief is generated by rig-cli (like `remediate.py`'s), not shipped as an agent-tools
   skill: one repo owns both the trigger and the instructions.
10. "Swap reset" means `memguard swap-drain` (gated: RAM must be able to take the pages back),
    and only where memguard is installed; anything else is a `decision` request.
11. Liveness: holder vacant after 10 minutes without a live endpoint, or 10 minutes of failed
    deliveries; refreshed by sysmon once per census interval (60 s).
12. A handler that has not registered as holder 5 minutes after the spawn counts as a failed
    spawn (Telegram fallback, once, only if the triggering finding is CRITICAL); its window is
    left for inspection.
13. Exit codes: `0` ok, `1` error, `3` slot conflict or vacant, `5` holder suspended or delivery
    failed.
14. A broken holder channel (tg-ctl error, missing, or too old for `--to-holder`) is not
    "no subscriber": sysmon does not spawn, and CRITICAL problem alerts go to Telegram, so a
    critical alert is never silenced by a failure of the channel itself.
15. `rig sysmon handler spawn` only requests a spawn; the daemon is the single writer of handler
    state.
16. A deferred problem alert is not lost to `alert.decide`'s cooldown (which records the send
    before delivery is attempted): it waits in a pending list and is redelivered once a holder
    is bound.
17. Telegram fallback (rulings 12, 14, 16) applies to CRITICAL-severity alerts only. A WARNING
    alert is never sent to Telegram: it goes to the holder, else is queued as pending or dropped,
    and the alert record carries the reason (`undelivered`, `reason: warning-no-telegram` plus the
    holder state).

## Components

### tg-cli: exclusive holder (tg-cli#747)

Store (`features/tg-ctl/alert-subscriptions.ts`), same file and lock as tg-cli#636:

```
AlertHolder {
  source: string            // e.g. "sysmon"
  sessionId: string         // omp session id
  title, pid, cwd           // last seen, refreshed from the live endpoint
  since: number             // ms epoch, when this session got the slot
  lastSeenLiveAt: number    // last time a live endpoint or a delivery proved it alive
  failingSince: number|null // first failed delivery since the last success
  expiresAt: number|null    // --ttl; null = none
  request: { sessionId, title, pid, at, reason } | null
}
```

State of a holder at time `now` (pure function, endpoints injected), first match wins:

1. **vacant**: no record, `expiresAt` passed, or `failingSince` is 10 minutes old or older;
2. **bound**: a live omp-live endpoint has its session id;
3. **suspended**: `now - lastSeenLiveAt < 10 min` (a resume is in progress);
4. **vacant** otherwise.

Every alert command that reads a **bound** holder under the lock refreshes `lastSeenLiveAt`,
`pid`, `title`, `cwd`; a successful delivery also clears `failingSince`.

CLI (the caller's session resolves as in tg-cli#636: `--pid`, `--session`, else the nearest live
omp-live ancestor):

| Command | Effect | Exit |
|---|---|---|
| `alert subscribe --exclusive --source S [--ttl 12h]` | claim if vacant, or refresh if the caller already holds it | 0; 3 if held by another: prints holder title, pid, session id, since, and the `handoff-request` command |
| `alert handoff-request --source S [--reason R]` | record a request, deliver a note to the holder with the grant/deny commands and the 10-min deadline | 0; 3 if vacant (subscribe instead), if the caller holds it, or if another session's request is younger than 10 min |
| `alert handoff --source S --to <session>` | holder only: move the slot to a live target, clear the request, tell the new holder | 0; 1 if the caller is not the holder or the target is not live |
| `alert handoff --source S --deny [--reason R]` | holder only: clear the request, tell the requester | 0; 1 as above |
| `alert take --source S` | caller takes the slot if vacant, or if its own request is ≥ 10 min old and unanswered; tells the previous holder if live | 0; 3 otherwise |
| `alert release --source S [--pid N \| --session ID]` | the holder (or the named session) gives up the slot | 0; 1 if it does not hold it |
| `alert holder --source S [--json]` | show holder, state, request; refresh liveness | 0 bound or suspended; 3 vacant |
| `alert send … --to-holder` | deliver `[alert S · severity · key] text` into the holder only; **never posts to Telegram**; throttle as today (key `holder:` + source/key/severity), but a failed or suspended delivery releases its claim so a retry delivers | 0 delivered; 3 vacant; 5 suspended or delivery failed (a failure sets `failingSince` if unset) |

Notes delivered by handoff commands go through the same omp-live send as alerts. Holder-only
sends have no Telegram message id; the text carries source, severity and key instead.

### rig-cli: `route: subscriber` (rig-cli#700)

`sysmon.alert.route`: `telegram` (default) | `subscriber`. `agents` and `alert._to_agents` are
removed; the validator rejects `agents` with "renamed to `subscriber` (rig-cli#700)".

On a due problem alert with `route: subscriber`, `alert.send` runs
`tg-ctl alert send --source sysmon … --to-holder`:

- `0` → `outcome: sent, via: holder`.
- `5` → `outcome: deferred` (holder resuming or briefly unreachable). The alert's finding keys
  join `memory["handler"]["pending"]` (at most 20 keys, dropped after `alert.disk_cooldown_minutes`;
  a post-crash alert is kept whole, since the crash finding does not recur). Each tick that sees a
  bound holder re-renders the pending keys that are still present at WARNING or above into one
  fresh alert and sends it `--to-holder` (plus any crash alert verbatim); keys that cleared are
  dropped, so nothing stale is replayed. Resolved notices are never queued.
- `3` → vacant; the record is `outcome: undelivered, reason: no subscriber`, and:
  - the handler window exists and was spawned < 5 min ago → `deferred: handler starting`
    (queued as pending, as for `5`);
  - the last spawn failed, or the spawn cooldown is active with no window, or the window exists
    > 5 min with no holder → for a CRITICAL alert, **Telegram fallback**: the ordinary
    `tg-ctl alert send` (rig-cli#683 path); for a WARNING alert, `undelivered, reason:
    warning-no-telegram` (ruling 17), its keys queued as pending;
  - otherwise → queued as pending, as for `5`; the spawn check (which runs later in the same
    tick) decides whether a handler is raised, and the handler receives the queued alert once it
    holds the slot.
- any other exit (an error, tg-ctl missing, or a tg-ctl older than tg-cli#747 rejecting
  `--to-holder`) → `outcome: error`; a CRITICAL alert takes the Telegram fallback (a broken
  channel must never silence a critical alert), a WARNING is recorded `undelivered, reason:
  warning-no-telegram` and queued as pending.

Resolved notices (`info`) go `--to-holder` only; on 3 or 5 they are dropped, never Telegram.
`rig sysmon test-alert` uses the same path and never spawns. `rig sysmon simulate` forces
`handler.dry_run`: it records `would_spawn` and runs no tmux or tg-ctl command.

### rig-cli: handler lifecycle (`riglib/sysmon_daemon/handler.py`, new)

Persisted state `memory["handler"]`: `{last_spawn_at, last_spawn_ok, window, pane_pid,
handler_session_id, handler_pid, holder, last_delivery_at, last_vacancy_check_at,
first_seen: {key: epoch}}`.

**Spawn check**, every tick, only with `route: subscriber`, after alerts and the heal ladder:

- trigger: any CRITICAL finding, or a WARNING whose key has been present continuously for
  `warn_persist_minutes` (15; `first_seen` is cleared when the key is absent), or a post-crash
  incident;
- and the holder is vacant (`tg-ctl alert holder --source sysmon` exit 3, at most once per census
  interval; suspended does not count as vacant). If that call errors or prints unreadable JSON
  the holder is **unknown**: no spawn (a handler could not subscribe either), and due CRITICAL
  problem alerts take the Telegram fallback (WARNINGs are queued, ruling 17);
- and no window `=<tmux_session>:sysmon-handler` exists;
- and `now - last_spawn_at ≥ spawn_cooldown_minutes` (60).

**Spawn** (one tmux call, argv built like `sessions._tmux_argv`):
`tmux new-window -d -P -F '#{window_id} #{pane_pid}' -t =<S>: -n sysmon-handler -c <workdir>` (or
`new-session -d -P -F … -s <S>` when the session does not exist) `<abs omp> --auto-approve
--append-system-prompt <brief> <kickoff>`. sysmon stores the printed window id and pane pid.
`brief` = `<state_dir>/handler/brief.md`, rewritten at each spawn. `kickoff` = one line: the
`tg-ctl alert subscribe --exclusive --source sysmon --ttl 0` command, then the current alert text.
The open incident, if any, records `handler: {spawned_at, window, command}`; every spawn is also
logged to `recent_handler` in `memory.json`.

**Startup check**: 5 minutes after a spawn, sysmon looks up the window's omp: the omp-live
endpoint whose pid is the window's pane pid or a descendant of it (a harness wrapper may sit
between tmux and omp), and records it as `handler_session_id`/`handler_pid`; every later command
targets `handler_pid`, never the pane pid. The spawn has **failed** only if the holder is still
vacant at that point: if the triggering finding was CRITICAL, one Telegram alert `sysmon handler
did not start` (problem); otherwise only the record. The fallback rule above applies until the
next spawn. If another session claimed the slot first
(the handler's subscribe exited 3), that is a success, not a failure: the idle handler is
retired by the normal rule.

**Retire**: the window sysmon spawned is closed when all hold for 30 minutes
(`idle_exit_minutes`): no finding at WARNING or above, no alert delivered to the holder, and
omp-live reports `handler_pid` with `handler_session_id` `busy: false` (unknown busy, or a pid now
carrying another session, never retires). Steps: `tg-ctl alert release --source sysmon --session
<handler_session_id>` (only if it still holds), a final note via
omp-live, `tmux kill-window -t <window_id>` — only after `tmux display-message -p -t <window_id>
'#{pane_pid}'` prints the stored pane pid; a mismatch (window renamed, closed, or replaced)
records a skip and clears the state instead. Nothing is ever killed by window name or by a value
that can be empty. A handler that handed the slot off is retired by the same rule. sysmon never
touches any other window or a holder it did not spawn.

Config block `sysmon.handler`: `tmux_session` (default: `sessions.tmux_session`; a string, or a
non-negative integer such as `0`, as `rig config set` coerces it; rendered as a string), `workdir`
(`~`), `warn_persist_minutes` (15), `spawn_cooldown_minutes` (60), `startup_timeout_minutes`
(5), `idle_exit_minutes` (30), `dry_run` (false: record the argv, run nothing). Only omp is
supported (the handler must have an omp-live endpoint). All keys are validated, in the schema
registry, the JSON schema, the setup wizard (`route` and `handler.tmux_session`) and
`docs/config-schema.md`.

`rig sysmon handler status` prints holder (via tg-ctl), window, last spawn and its outcome.
`rig sysmon handler spawn` asks the daemon to spawn one now: it writes
`<state_dir>/handler/spawn-request`, which the daemon consumes on its next tick (it ignores the
trigger and cooldown, honours the window check), and waits up to
`max(30, 2 × sample_interval_seconds + 10)` s for the window. The
daemon is the only writer of the handler state. This is also the live acceptance path.

### Handler brief

Generated text, sections in this order:

1. **Mission**: you own this machine's resource alerts until you are released; keep agent
   sessions working; never damage the user's work.
2. **First command**: `tg-ctl alert subscribe --exclusive --source sysmon --ttl 0`. If it exits
   3, another agent took the slot first: report nothing, stop.
3. **Context commands**: `rig sysmon sample`, `rig sysmon incidents --show <id>`,
   `omp-steer list`.
4. **Do immediately** (no data loss possible): stop or kill processes attributed `ORPHAN`; stop
   your own processes; `docker builder prune -f`; ask an owning session to stop heavy work with
   `omp-steer <pid> '<text>'`; where `memguard` is installed and active, `memguard swap-drain`
   (it refuses when RAM cannot take the pages back).
5. **Ask first** with `tg --tag decision` and wait: anything that deletes or moves user data
   (worktrees, caches other than the docker build cache, files), killing or stopping a live
   session's non-orphan processes, restarting a user application or service, reboot or logout,
   changing configuration or thresholds, `swapoff`/`swapon` by any means other than
   `memguard swap-drain`.
6. **Telegram**: `tg --tag problem` when a CRITICAL persists 30 minutes after your actions or you
   cannot act without a decision; `tg --tag report` after any significant change: a process you
   killed that you did not own, `docker builder prune`, `memguard swap-drain`, an approved
   decision carried out, a ticket filed. Nothing else goes to Telegram (no "all clear", no
   progress).
7. **Handoff**: when asked for the slot, grant it (`tg-ctl alert handoff --source sysmon --to
   <id>`) unless an action of yours is mid-way; then finish that action and grant.
8. **Owners already notified**: the session owning an offender gets sysmon's heal note (or
   memguard's, rig-cli#694); do not repeat it more than once per finding.
9. **Never**: change code, commit, run test suites or builds; kill by `pgrep`/`pstree`/command
   substitution of a value that can be empty (stop only a PID just verified with
   `[ -n "$P" ] && ps -p "$P"`, or a systemd scope by name); treat the scheduled
   `swap-drain` dip in MemAvailable as a leak.

## Interactions

- **memguard / rig-cli#694** (ADR `ubuntu-tweaks docs/specs/2026-10-15-memory-pressure-split-design.md`,
  *One steer channel per machine*): memguard owns the memory **steer** to the offending session;
  this design owns who **handles the machine**. They do not overlap: with memguard active, sysmon's
  heal note is muted (#694) and the handler still receives sysmon's alerts. The handler's
  swap action is memguard's gated `swap-drain`, never its own.
- **mem-sentinel** `MACHINE CRITICAL` broadcast (`omp-steer all`) is unchanged; it is retired with
  memguard per the ADR.
- **rig-cli#699/#685**: they change `alert.decide` and the swap detector; this design consumes
  `decide`'s output and only changes delivery. Implementation starts after they merge. With one
  recipient, a flap costs one session's turns instead of ~9; resolved notes reach only the holder.
- **rig-cli#698** (partial broadcast double-posts): the broadcast is removed; #698 is closed as
  superseded when rig-cli#700 lands.
- **`auto_remediate`** (headless one-shot): rejected together with `route: subscriber` (ruling 4).
- **heal ladder**: unchanged.

## Testing

- tg-cli: pure tests for the holder state machine (bound/suspended/vacant at the 10-minute edges,
  TTL, request age, refresh), argv parsing of every new command, store round-trip with and
  without `holders`; lifecycle integration test with a fake omp-live registry: subscribe →
  refused second subscribe → handoff-request → deny → request → take after 10 min → send
  `--to-holder` exit codes 0/3/5 and no `tg` spawn.
- rig-cli: `alert.send` with `route: subscriber` against a fake runner for exits 0/1/3/5; the
  daemon's fallback, pending redelivery and post-crash path; `handler` pure decisions (trigger,
  cooldown, window exists, startup timeout, retire, window-id check) with an injected clock;
  argv of spawn and retire; config validation (`agents` rejected, `subscriber` +
  `auto_remediate` rejected, every `handler` key); schema in sync.
- Live acceptance on *home*: `rig sysmon handler spawn` raises the window, the handler
  subscribes (`tg-ctl alert holder --source sysmon` shows it), `rig sysmon test-alert` reaches
  only it, a second session's `subscribe --exclusive` is refused with the holder named; with the
  holder released and the window closed, `rig sysmon test-alert` records `undelivered, vacant`
  and nothing reaches Telegram; `systemctl --user is-active ai.hyperide.sysmon` = active. The
  Telegram fallback branches are proven by the unit tests, not by paging the owner.

## Migration and rollout

1. tg-cli#747 merged and deployed (`scripts/deploy.sh`). Nothing changes until something
   subscribes exclusively or sends `--to-holder`.
2. rig-cli#700 merged after #699/#685.
3. On *home*: `rig config set --global sysmon.alert.route subscriber`, `rig config set --global
   sysmon.handler.tmux_session 0`, `rig apply commit --only sysmon`. The existing non-exclusive
   subscription ("🕹️ AGENTS") no longer receives sysmon alerts; that session subscribes
   exclusively if it wants them.
4. Close rig-cli#698 as superseded.
