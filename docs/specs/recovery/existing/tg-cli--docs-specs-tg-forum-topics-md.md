# Review addendum: tg-cli/docs/specs/tg-forum-topics.md

Source revision: `b640b65e7b30321ec8c14259896212f1fe78626f`.

This source document is retained with its original draft/approved/skipped status. This addendum does not approve previously skipped work. Root `rig-cli/SPEC.md` describes Evolve, not the entire rig ecosystem.

Current recovery contract: [Telegram routing, durable decisions and concurrent delivery](../telegram.md). Governing epic: https://github.com/alex-mextner/rig-cli/issues/521.

## Example and revalidation

The user replies to landing while rig-fable is busy. Delivery to landing and the next Telegram update continue without waiting for rig-fable's full turn. A late Approve is retained for the same scoped action after reconnect, but a changed command, repository, commit or native session cannot reuse it.

## Source quotations

> Ответы в Telegram всё ещё не работают для OMP-сессий вне tmux. Я же сказал: нужно проработать все сценарии.

Source **Q201**; compiled-memory-attribution; MEMORY.md standing lesson: Telegram must see every live agent; every reply route must land; PDF page(s) 74. Thematic recovery requirement, not retroactive authorship of this entire specification.

<details><summary>Original wording (before editorial correction)</summary>

> и реплаи в телеге все еще не работают для omp сессий вне tmux, я ж сказал надо все сценарии проработать

</details>

> В Telegram само отправилось «/agent 44897 PING-339-LIVE-PROOF». Это явно какие-то тесты.

Source **Q233**; compiled-memory-attribution; MEMORY.md standing lesson: Telegram tests: headless Web + QR, never the real chat; PDF page(s) 87. Thematic recovery requirement, not retroactive authorship of this entire specification.

<details><summary>Original wording (before editorial correction)</summary>

> /agent 44897 PING-339-LIVE-PROOF вот такое сейчас само написалось в телегу)) это точно какие-то тесты

</details>

## Linked acceptance backlog

- [tg-cli#26](https://github.com/alex-mextner/tg-cli/issues/26) — /tasks command — session task table + Gantt + agent summary (HD inline)
- [tg-cli#28](https://github.com/alex-mextner/tg-cli/issues/28) — tg#<id> message-ref convention + autolink (before #N PR/issue detection)
- [tg-cli#30](https://github.com/alex-mextner/tg-cli/issues/30) — Decisions-as-buttons + hanging-question inject-collision (no-expiration; DEFER inbound)
- [tg-cli#52](https://github.com/alex-mextner/tg-cli/issues/52) — tg-ctl forum-topics: wire the routing half (increment 3 minus spawn) — topic→pane map + inbound route + outbound message_thread_id
- [tg-cli#53](https://github.com/alex-mextner/tg-cli/issues/53) — reply to agent-B mis-delivered to agent-A: recognized route loses to the single global registration
- [tg-cli#56](https://github.com/alex-mextner/tg-cli/issues/56) — forum-topics incr2: tg --topic <id> — agent replies thread into their own topic
- [tg-cli#57](https://github.com/alex-mextner/tg-cli/issues/57) — forwarded question must wait indefinitely for its Telegram answer, not expire to terminal
- [tg-cli#58](https://github.com/alex-mextner/tg-cli/issues/58) — unscoped question expiry silently strands its deferred backlog (no dead-letter notice)
- [tg-cli#63](https://github.com/alex-mextner/tg-cli/issues/63) — /agent picker: drop the redundant text list + ▸window header — buttons only
- [tg-cli#65](https://github.com/alex-mextner/tg-cli/issues/65) — ask-forward silently drops a question when the registration doesn't match (no log line)
- [tg-cli#67](https://github.com/alex-mextner/tg-cli/issues/67) — per-pane registration set: every live session's questions forward (kill the single-registration limit)
- [tg-cli#72](https://github.com/alex-mextner/tg-cli/issues/72) — tg --tag answer must require --reply-to; hidden terminal-only bypass, not in help
- [tg-cli#74](https://github.com/alex-mextner/tg-cli/issues/74) — tg-ctl: ambiguous/gone target dumps plain text instead of the tappable agent picker
- [tg-cli#75](https://github.com/alex-mextner/tg-cli/issues/75) — no-reply auto-bind to last-active agent + /agent picker uses tmux window names not cwd
- [tg-cli#78](https://github.com/alex-mextner/tg-cli/issues/78) — no-reply inbound binds to the last-message agent (simplify the MRU machinery); fix the regression
- [tg-cli#84](https://github.com/alex-mextner/tg-cli/issues/84) — forum-topics: spawn an agent on forum_topic_created (the /new flow trigger — spec increment 2)
- [tg-cli#86](https://github.com/alex-mextner/tg-cli/issues/86) — tg-ctl forum-topics increment-4 polish: recent-path buttons, clear model keyboard, re-spawn on dead pane, crash-orphan reconcile
- [tg-cli#88](https://github.com/alex-mextner/tg-cli/issues/88) — tg-ctl status: autostart shows 'NOT enabled' when daemon is supervised by an external launchd job
- [tg-cli#93](https://github.com/alex-mextner/tg-cli/issues/93) — tg-ctl: cleanExit unlinks the pidfile unconditionally — a departing/loser daemon deletes the live winner's pidfile (false 'not running')
- [tg-cli#97](https://github.com/alex-mextner/tg-cli/issues/97) — AskUserQuestion forwards a duplicate Telegram card; second tap reads as 'expired'
- [tg-cli#99](https://github.com/alex-mextner/tg-cli/issues/99) — tg-ctl: question-answer state must survive socket-close + daemon restart (late-deliver + lossless reconnect)
- [tg-cli#100](https://github.com/alex-mextner/tg-cli/issues/100) — tg-ctl late-delivery: verify pane IDENTITY (not just liveness) before injecting a late answer
- [tg-cli#115](https://github.com/alex-mextner/tg-cli/issues/115) — /tasks [<agent>] [<status>] bot command: task table with lifecycle status (tickets + PR/CI state)
- [tg-cli#116](https://github.com/alex-mextner/tg-cli/issues/116) — autolink-msgrefs: replace literal tg#<id> with a quoted preview of the referenced message («начало сообщения…»), hyperlinked where possible
- [tg-cli#118](https://github.com/alex-mextner/tg-cli/issues/118) — question buttons: tapped answer never reaches the agent (CC >=2.1.198 requires hookEventName); multi-question ends on an extra manual Enter
- [tg-cli#121](https://github.com/alex-mextner/tg-cli/issues/121) — tg replies --session accepts a tmux window name, not only a pane id
- [tg-cli#143](https://github.com/alex-mextner/tg-cli/issues/143) — Flaky CI: tests/ctl-topic-spawn-integration.test.ts VANISHED-dir re-spawn cases fail intermittently in full suite
- [tg-cli#149](https://github.com/alex-mextner/tg-cli/issues/149) — tg-ctl: synchronous unforgeable 'ask & wait for a real Telegram reply' primitive for external approval callers (tg#6554)
- [tg-cli#150](https://github.com/alex-mextner/tg-cli/issues/150) — Persistent codex/agent tmux panes have no task-lock — new inbound message silently drops in-flight work
- [tg-cli#152](https://github.com/alex-mextner/tg-cli/issues/152) — git-state-check banner: warn before a message lands in an occupied pane
- [tg-cli#161](https://github.com/alex-mextner/tg-cli/issues/161) — Inspect tg voice reply reaction semantics
- [tg-cli#164](https://github.com/alex-mextner/tg-cli/issues/164) — Escalation-format gate: pre-send-text hook + question tag (tg-cli core)
- [tg-cli#167](https://github.com/alex-mextner/tg-cli/issues/167) — tg-cli: print tg# message refs on send
- [tg-cli#173](https://github.com/alex-mextner/tg-cli/issues/173) — Rewrite dirty-pane routing warning
- [tg-cli#178](https://github.com/alex-mextner/tg-cli/issues/178) — Improve Telegram tasks view with status groups and filters
- [tg-cli#181](https://github.com/alex-mextner/tg-cli/issues/181) — tg-ctl: ask once before falling back from private topics
- [tg-cli#183](https://github.com/alex-mextner/tg-cli/issues/183) — tg-cli: stop dropping stale inbound messages
- [tg-cli#186](https://github.com/alex-mextner/tg-cli/issues/186) — tg-ctl /new is broken: tmux index collision, ignored inline dir, fixed arg order, and a re-ask loop on spawn failure
- [tg-cli#190](https://github.com/alex-mextner/tg-cli/issues/190) — tg-ctl: inject !shell messages verbatim for harness passthrough
- [tg-cli#194](https://github.com/alex-mextner/tg-cli/issues/194) — tg-ctl: graceful reload — no dropped channel across restart
- [tg-cli#197](https://github.com/alex-mextner/tg-cli/issues/197) — tg-ctl: graceful reload preserves queued inbound messages
- [tg-cli#201](https://github.com/alex-mextner/tg-cli/issues/201) — /tasks: a failing 'task list' renders as an empty board (log-only, not surfaced to the user)
- [tg-cli#203](https://github.com/alex-mextner/tg-cli/issues/203) — tg-ctl: self-heal Claude hooks on daemon start (question + token-limit states silently break)
- [tg-cli#208](https://github.com/alex-mextner/tg-cli/issues/208) — tg silently fragments an oversized message into dozens of separate Telegram sends instead of warning or refusing
- [tg-cli#209](https://github.com/alex-mextner/tg-cli/issues/209) — tg-cli: decode text escapes before enforcing escalation format on --tag decision/question
- [tg-cli#212](https://github.com/alex-mextner/tg-cli/issues/212) — tg-ctl: preserve a symlinked ~/.claude/settings.json during hook self-heal write
- [tg-cli#213](https://github.com/alex-mextner/tg-cli/issues/213) — tg-ctl: strengthen test coverage for the /tasks Review column and title-guard feature flag wiring
- [tg-cli#218](https://github.com/alex-mextner/tg-cli/issues/218) — tg: convert every markdown pipe table in a message, not just the first
- [tg-cli#219](https://github.com/alex-mextner/tg-cli/issues/219) — tg: use the post-conversion format when checking the long-unstructured-message warning
- [tg-cli#222](https://github.com/alex-mextner/tg-cli/issues/222) — tg: document how to compose a compliant decision or question escalation via tg --table
- [tg-cli#231](https://github.com/alex-mextner/tg-cli/issues/231) — tg-ctl: unidentifiable/overclaiming abandoned-hook text and codex-only advice shown to every harness
- [tg-cli#233](https://github.com/alex-mextner/tg-cli/issues/233) — tg-ctl: queued permission decision must never expire on a timer
- [tg-cli#235](https://github.com/alex-mextner/tg-cli/issues/235) — tg-ctl: stale abandoned question/permission entries not reaped from questions.json store
- [tg-cli#236](https://github.com/alex-mextner/tg-cli/issues/236) — tg-ctl: no relay of agent's post-completion follow-up work back to Telegram (silent open thread despite work being done)
- [tg-cli#238](https://github.com/alex-mextner/tg-cli/issues/238) — pre-send-text decision-gate hook inspects pre-substitution shell text, blindspot for $(cat file) message bodies
- [tg-cli#239](https://github.com/alex-mextner/tg-cli/issues/239) — No dry-run mode: agents testing the --tag decision|question gate leak placeholder content to the live chat
- [tg-cli#242](https://github.com/alex-mextner/tg-cli/issues/242) — tg --dry-run: fix attachment-detection ordering and add pre-autolink and rich-message validation
- [tg-cli#243](https://github.com/alex-mextner/tg-cli/issues/243) — tg-ctl: recognize omp (Oh My Pi) harness — inbound discovery + outbound branding
- [tg-cli#264](https://github.com/alex-mextner/tg-cli/issues/264) — harness-event bare StopFailure alerts lose per-pane identity when multiple panes run the same harness
- [tg-cli#267](https://github.com/alex-mextner/tg-cli/issues/267) — Late-deliver a Telegram permission tap by injecting the digit into the still-showing terminal menu
- [tg-cli#268](https://github.com/alex-mextner/tg-cli/issues/268) — tg-ctl permission pane-inject: deferred hardening (verbose deny label, state-machine extraction, other prompt shapes, deny-window guard test)
- [tg-cli#274](https://github.com/alex-mextner/tg-cli/issues/274) — tg-ctl: /agent <selector> <text> route drops the tg#<id> wrap tag
- [tg-cli#275](https://github.com/alex-mextner/tg-cli/issues/275) — tg-ctl: /agent-routed messages lose the tg#<id> wrap tag
- [tg-cli#279](https://github.com/alex-mextner/tg-cli/issues/279) — tg-ctl: the live daemon process never restarts after a merge, so shipped fixes silently sit inert for weeks
- [tg-cli#280](https://github.com/alex-mextner/tg-cli/issues/280) — tg-ctl inbound Telegram albums (media_group_id) are split into separate per-photo deliveries instead of one bundled message
- [tg-cli#283](https://github.com/alex-mextner/tg-cli/issues/283) — permission-menu.ts: multi-line Bash command matching for late-delivery pane-injection needs live-verified redesign (tg-cli#182 leg-2 follow-up)
- [tg-cli#298](https://github.com/alex-mextner/tg-cli/issues/298) — tg-ctl: isLikelySlashCommand rejects Telegram's @botname group-chat suffix, breaking verbatim passthrough in bound topics
- [tg-cli#299](https://github.com/alex-mextner/tg-cli/issues/299) — Audit lost inbound Telegram messages across all agents and close the delivery gap
- [tg-cli#300](https://github.com/alex-mextner/tg-cli/issues/300) — tg: a file path mentioned in a rich HTML message is auto-attached, which silently downgrades the send to the basic tier and fails with a misleading 'only those HTML tags are supported' error
- [tg-cli#301](https://github.com/alex-mextner/tg-cli/issues/301) — tg: drop the question tag — only the standard tags stay (answer, decision, problem, report); a question is a decision request
- [tg-cli#302](https://github.com/alex-mextner/tg-cli/issues/302) — tg: refuse to send a message containing a banned word from the shared dictionary (DICT.json next to the global CLAUDE.md), printing the replacement
- [tg-cli#304](https://github.com/alex-mextner/tg-cli/issues/304) — Empirical study of Claude Code prompts inside tmux: how to tell whether a permission or question is still pending, and wire the findings into tg-ctl
- [tg-cli#306](https://github.com/alex-mextner/tg-cli/issues/306) — tg-ctl: agents running outside tmux are invisible — show them as unreachable with the reason, and deliver to them through the Stop-hook inbox
- [tg-cli#309](https://github.com/alex-mextner/tg-cli/issues/309) — Permission pane-inject: truncated-identity prefix match and TOCTOU can approve the wrong command
- [tg-cli#316](https://github.com/alex-mextner/tg-cli/issues/316) — tg-ctl: migrate the legacy Codex hooks.json Stop entry away, detect the telemetry hook in config.toml, and recognize the launchd-owned daemon in status
- [tg-cli#317](https://github.com/alex-mextner/tg-cli/issues/317) — Approval requests report "not posted" for messages Telegram actually received
- [tg-cli#323](https://github.com/alex-mextner/tg-cli/issues/323) — tg-ctl: control live omp via ACP, not tmux send-keys
- [tg-cli#327](https://github.com/alex-mextner/tg-cli/issues/327) — pre-send-photo consumes a live Telegram hatch approval without weakening the default block
- [tg-cli#333](https://github.com/alex-mextner/tg-cli/issues/333) — Hatch permission asks from non-tmux omp are dropped not-registered
- [tg-cli#334](https://github.com/alex-mextner/tg-cli/issues/334) — tg-ctl status lies not-running while launchd pid lives
- [tg-cli#339](https://github.com/alex-mextner/tg-cli/issues/339) — Telegram replies still miss live omp sessions outside tmux
- [tg-cli#341](https://github.com/alex-mextner/tg-cli/issues/341) — tg-ctl ACP initialize times out: omp 18.1.14 speaks NDJSON, not Content-Length
- [tg-cli#343](https://github.com/alex-mextner/tg-cli/issues/343) — ACP initialize timed out: tg-ctl speaks Content-Length, omp acp speaks NDJSON
- [tg-cli#344](https://github.com/alex-mextner/tg-cli/issues/344) — tg-ctl must self-register OS autostart on first run
- [tg-cli#349](https://github.com/alex-mextner/tg-cli/issues/349) — tg-ctl drops RIG_HATCH_REQUEST_* Telegram approvals when the human taps after the requesting process exits
- [tg-cli#354](https://github.com/alex-mextner/tg-cli/issues/354) — tg-ctl: stale pane registrations survive a tmux server restart (pane_id reuse)
- [tg-cli#356](https://github.com/alex-mextner/tg-cli/issues/356) — tg hatches: list/show hatch-approval history in a durable local store
- [tg-cli#358](https://github.com/alex-mextner/tg-cli/issues/358) — Fix omp session title in outbound header and /agent picker
- [tg-cli#360](https://github.com/alex-mextner/tg-cli/issues/360) — tg-ctl: rescope omp delivery to fire-and-forget, fix offset-advance-before-persistence ordering (supersedes PR 322)
- [tg-cli#372](https://github.com/alex-mextner/tg-cli/issues/372) — tg-ctl ask: report the daemon's real posted/dropped/expired outcome instead of a generic not-posted
- [tg-cli#374](https://github.com/alex-mextner/tg-cli/issues/374) — tg-ctl: 3 generic-ask hardening findings from PR #368 review round 3 (not yet fixed)
- [tg-cli#377](https://github.com/alex-mextner/tg-cli/issues/377) — Generic interactive agent control: migrate all message and permission routes to harness-cli
- [tg-cli#379](https://github.com/alex-mextner/tg-cli/issues/379) — tg-ctl: awareness-nudge sweep repeatedly fails with 'Internal error' for stale /private/tmp omp ACP sessions
- [tg-cli#380](https://github.com/alex-mextner/tg-cli/issues/380) — [EPIC] Reliable Telegram agent control: durable delivery, generic harness API and scoped approvals

## Preserved specification

# tg-ctl — Telegram forum-topics mode (one topic = one agent)

Status: **DRAFT design** (CTO 2026-06-19 "делай"). Implemented incrementally — see
§9 Increment plan. Supersedes the flat `/agent` phonetic selector as the primary
addressing model when the bot chat is a **forum** (a supergroup with topics enabled).

## 1. Goal

Each background agent lives in its **own Telegram forum topic** (`message_thread_id`).
The topic IS the address — no phonetic disambiguation. Concretely:

- **Creating a topic spins up an agent.** A `forum_topic_created` service message starts
  a per-topic `/new` flow that asks the working **PATH** and the **MODEL**, then launches a
  fresh agent (`claude --model <model>` in `<path>`, in its own tmux window) bound to that
  topic.
- **Routing is per-topic.** An inbound message carrying `message_thread_id = T` injects into
  topic `T`'s bound pane; that agent's `tg` replies post back **into the same topic** T.
- The legacy flat-chat behaviour (`/agent`, reply-routing, `/status`, …) is unchanged for
  non-forum chats and for the forum's General topic (`message_thread_id` absent).

## 2. Why forum topics (vs the flat `/agent` picker)

The flat selector fuzzy-matches a window name out of an ambiguous pool and re-asks on every
collision. A forum topic is a **durable, unambiguous, user-visible address**: the binding is
created once (on topic creation), Telegram renders each agent as its own thread, and the user
never disambiguates. It also gives each agent an isolated transcript.

## 3. Telegram surface used

- **Forum topics** require the chat to be a **forum** (supergroup, `Topics` enabled). The bot
  must be an admin with `can_manage_topics` to create/close topics; it can always *post into*
  an existing topic with `message_thread_id`.
- **Service messages** (read-only, delivered as `message`): `forum_topic_created`
  (`{ name, icon_color?, icon_custom_emoji_id? }`), `forum_topic_edited`, `forum_topic_closed`,
  `forum_topic_reopened`. Each carries `message_thread_id` = the topic's id (== the id of the
  topic's first message).
- **Posting into a topic**: `sendMessage(..., { message_thread_id: T })`. All bot replies for a
  topic-bound agent MUST carry `message_thread_id: T` or they land in General.
- **Inbound**: a user message in topic T arrives as a normal `message` with
  `message_thread_id: T` and `is_topic_message: true`.

## 4. Lifecycle / state machine (per topic)

A topic moves through a small pure state machine (`features/tg-ctl/topics.ts`):

```
(none) --forum_topic_created--> awaiting-path
awaiting-path --user sends a path (or picks a recent-repo button)--> awaiting-model
awaiting-model --user picks a model button--> spawning --(pane up)--> bound
bound --forum_topic_closed / agent death--> closed   (re-open re-binds or re-spawns)
```

- **awaiting-path**: the bot posts (into topic T) "Which directory should this agent work in?"
  with inline buttons for recent/known repos (from the routes/registration history) + a
  free-text fallback ("or send an absolute path").
- **awaiting-model**: "Which model?" buttons — the model catalog (see §6). Default highlighted.
- **spawning**: the entrypoint runs `tmux new-window` (see §5); on success → `bound`, else an
  error is posted into the topic and the state returns to `awaiting-model` (retry).
- **bound**: `{ threadId, paneId, path, model, name }` persisted; messages route here.
- **closed**: binding retained but marked closed; a `forum_topic_reopened` or a new message
  re-attaches (re-spawn if the pane is gone).

The state machine is PURE: `(topicState, inbound|callback) -> { nextState, action? }`. The
entrypoint owns the spawn, the sendMessage, and persistence.

## 5. Spawning (the new capability)

The daemon has only ever *attached* to existing panes; topic-mode adds **spawning**. On
`spawning`:

```
tmux new-window -t <session> -n <topic-name-slug> -c <path> -P -F '#{pane_id}' \
  -- claude --model <model>
```

- `-P -F '#{pane_id}'` returns the new pane id → bound to the topic immediately (no discovery
  race). `-c <path>` sets the cwd. The agent command mirrors how a human starts the agent:
  Claude via `claude [--model …]`, Codex via `codex [--model …]`, and opencode via
  `opencode --model <provider/model>`.
- The window name is a slug of the topic name (so `/agent` still works as a fallback address).
- Guardrails: `<path>` must exist and be a directory (else re-ask in `awaiting-path`); the model
  must be in the catalog; refuse to spawn a second pane for an already-`bound` topic.

## 6. Model catalog

A small declared catalog `features/tg-ctl/models.ts` (pure data) → the `awaiting-model`
buttons and the spawn command. Each entry: `{ id, label, kind: 'claude'|'codex'|'opencode',
argv: (path) => string[], promptArgv?: (task) => string[] }`. The catalog ships Claude
tiers (opus/sonnet/haiku) + default, Codex default/GPT entries, and opencode provider/model
entries. The catalog is the single source of truth for both the buttons and the spawn argv
(no drift); flat `/new` filters model buttons by the selected harness, and a concrete model
token infers its harness. Initial task text from flat `/new` is passed as a positional prompt
after `--` for Claude/Codex and as one `--prompt=<text>` argv element for opencode; when
private-chat topics are enabled, the private-topic-backed `/new` spawn gets the same task argv
and also receives `TG_TOPIC`.

## 7. Persistence

A new state file `topics` (added to `CtlPaths`): a JSON array of `TopicBinding`
`{ threadId, paneId, path, model, name, status, ts }`, written by the entrypoint on every
transition (same pattern as `routes`). On daemon restart the bindings reload; a binding whose
pane is gone is re-spawned on the next message (or marked `closed`). Capped like routes.

## 8. Routing changes (updates.ts)

`stepUpdates` gains, BEFORE the existing flat-chat dispatch:

- `message.forum_topic_created` → `{ kind: 'topic-new', threadId, name, from }` (start the flow).
- `message.message_thread_id` present AND topic is `bound` → route the (wrapped) text to that
  topic's pane via a `{ kind: 'topic-route', threadId, injectText }` action (the entrypoint maps
  `threadId → paneId`). Replies/acks for it carry `message_thread_id`.
- `message.message_thread_id` present AND topic is `awaiting-*` → feed the path/model answer to
  the state machine (`{ kind: 'topic-answer', … }`).
- `forum_topic_closed`/`reopened` → `{ kind: 'topic-close'|'topic-reopen', threadId }`.
- No `message_thread_id` (General / non-forum) → the existing behaviour, untouched.

Outbound threading: the entrypoint threads every topic-originated reply with
`message_thread_id`. The `tg` send side learns a `--topic <T>` / `TG_TOPIC` so an agent's
replies thread correctly even though `tg` runs from the pane (the binding supplies T; the agent
need not know it — the daemon stamps the route with the threadId and `tg`'s reply path reads it,
mirroring how `routes.cwd` already flows).

## 9. Increment plan (ship small, each green + reviewed)

> Note: the items below are listed in **landing order** (1 → 3 → 2 → 4), NOT renumbered — the
> routing half (3) shipped before the spawn executor (2) because routing carries no agent-launch
> risk and is independently testable against a seeded binding. The numbers are the original plan ids.

1. **Foundation (pure core + tests)** — LANDED (2157f09): `types.ts` (thread/forum fields +
   `TopicBinding` + topic Actions), `features/tg-ctl/topics.ts` (binding store parse/append/lookup
   + the pure state machine), `features/tg-ctl/models.ts` (catalog), `stepUpdates` recognising the
   topic updates and emitting the new Actions, full unit tests. The pure layer only — the entrypoint
   did not yet consume the Actions (dead-wired behind a not-yet-existent `control.topics` flag).
3. **Per-topic routing + outbound threading (routing half)** — LANDED: the `control.topics` config
   flag (default OFF) + the `topics` state file (`CtlPaths.topics`); the poll loop passes
   `topicsEnabled`/`topicStatusOf` into `stepUpdates`; `executeAction` handles `topic-route` (map
   `threadId → paneId` from the store, re-verify the pane still hosts an agent, inject the wrapped
   text with the flat path's defer-guard — a dead/missing pane marks the binding `closed` and posts
   an error INTO the topic, never leaking to a flat agent) and `topic-close`/`topic-reopen` (persist
   the transition). All topic-originated daemon output carries `message_thread_id`. Integration test
   `tests/ctl-topics-integration.test.ts` proves: 1:1 routing unchanged (flag off AND on), topic
   routing into the bound pane, and the dead-pane no-leak + threaded error.
   **`tg --topic` (the agent's own `tg` reply threading) — LANDED (increment 2):** the OUTBOUND
   `tg` send learned a `--topic <id>` flag and a `TG_TOPIC` env fallback (the flag wins). When set,
   EVERY outbound primitive (sendMessage/sendRich/sendPhoto/sendDocument/sendMediaGroup) carries
   `message_thread_id` — NOT consumed after the first message, so a >4096 split and album items all
   stay in the topic. With neither flag nor env the wire payload is byte-identical to before (the
   1:1 path is untouched). The `TG_TOPIC` env is **ADVISORY** (an agent shell can inherit a stale
   value): a malformed value is logged and ignored (posts to General), and a well-formed but
   server-REJECTED value (closed/deleted topic, non-forum chat → `message thread not found` /
   `TOPIC_CLOSED`) makes the send retry ONCE without the thread id rather than hard-fail — so a
   daily-critical send never dies because of a stale ambient default. An EXPLICIT `--topic` stays
   strict: the agent asked for that topic, so a rejection surfaces as a real error. The DAEMON
   auto-stamping `TG_TOPIC` into the spawned window's env (so the agent need not pass `--topic`)
   LANDED in increment 4 (`new-window -e TG_TOPIC=<id>`); the advisory fallback above keeps that
   ambient default safe against a since-closed topic.
2. **Spawn + bind executor (the remaining seam)** — LANDED: the entrypoint handles
   `topic-new`/`topic-answer` plus a new `topic-model` model-button callback. `forum_topic_created`
   → an `awaiting-path` binding + a "which directory?" prompt; a path message → validated (must
   exist + be a dir) → `awaiting-model` + the model-catalog button keyboard
   (`tgm:<threadId>:<modelId>`); a model tap → `tmux new-window -c <path> -P -F '#{pane_id}' -- <argv>`
   (the catalog argv) → bind the returned pane → `bound` + a confirmation into the topic. SAFETY: every
   spawn step is exception-guarded so a bad path / missing tmux / spawn error never throws into the poll
   loop — it posts a human-readable error into the topic and leaves the binding mid-flow for a retry;
   an already-`bound` topic refuses a second spawn (a duplicate `forum_topic_created` is already filtered
   in `stepUpdates`, so a restart/dup can't double-spawn); the whole path only runs when `control.topics`
   is on (default OFF → 1:1 byte-identical). Tests: `tests/ctl-topic-spawn-integration.test.ts` (valid
   spawn, malformed path, spawn failure survives, no double-spawn, flag-off no-op) + the pure
   parser/keyboard/callback-emission units in `tests/ctl-topics.test.ts`. (The recent-repo path
   buttons and re-spawn-on-dead-pane were deferred to increment 4 — now LANDED, see below.)
4. **Lifecycle polish — LANDED.** All increment-4 items shipped together:
   - **Recent-repo path buttons** on the awaiting-path step (`tgp:<threadId>:<index>:<nonce>`): the
     prompt offers recent project cwds (from the routes store + per-pane registrations, newest-first,
     deduped to absolute existing dirs) as one-tap buttons; the chosen list + a per-prompt NONCE are
     persisted on the binding so a STALE button from a superseded prompt is rejected (never resolves
     its index against a newer choice list). Free-text remains a fallback.
   - **Model keyboard cleared on bind** via `editMessageReplyMarkup` (and on a restart-to-path) so the
     stale buttons can't be re-tapped.
   - **Re-spawn on a dead/closed pane**: a message to a topic whose bound pane died marks it `closed`
     and offers a one-tap **Re-spawn** button (`tgr:<threadId>`) that re-launches with the retained
     path + model (else restarts the /new flow when the path/model is missing or the dir vanished).
     The offer is THROTTLED (one per dead topic, `respawnOffered`), only stamped when the button send
     succeeds, and a re-spawn failure restores `closed` so the next message re-offers.
   - **DAEMON auto-stamps `TG_TOPIC`** into the spawned window's env (`new-window -e TG_TOPIC=<id>`)
     so the agent's plain `tg "reply"` threads back into the topic without `--topic`.
   - **Crash-window orphan reconcile** on startup: a binding stuck `awaiting-model` after a crash
     between `new-window` and the `bound` write is RE-BOUND to its live orphan pane (no second spawn)
     — proven by a per-spawn token stamped as a `@tg_spawn_token` WINDOW USER OPTION (queryable via
     `#{@tg_spawn_token}` in the pane format; `new-window -e` process env is NOT queryable later),
     with the recorded paneId as a fallback proof. Adoption requires same slug + same cwd + (token OR
     recorded paneId), so a same-slug/cwd STRANGER is never adopted; only `awaiting-model + path +
     model + spawnPending` bindings are candidates (a normal pre-model or failed-spawn state is not).
     A flaky/empty startup snapshot is skipped (never mass-closes live bindings); a model tap on a
     still-`spawnPending` binding re-probes (the just-in-time adoption) so a missed startup reconcile
     can't double-spawn. A `bound` binding whose pane is gone is marked `closed`.
   - **Same-batch races**: a re-spawn tap + a text message in one getUpdates batch routes the text to
     the re-bound pane (not dropped); a second same-batch message to a just-closed topic goes through
     the throttled recovery, never the old "recreate the topic" dead-end.
   Residual (accepted): a daemon crash in the sub-ms gap between `new-window` returning and the token
   stamp leaves an orphan with neither a token nor a recorded paneId — bounded by the JIT re-probe and
   astronomically unlikely; a stranded orphan is recoverable by hand.

## 10. Edge cases / decisions

- **Not a forum / no admin rights**: topic-mode silently inert; flat `/agent` stays the address.
  A `forum_topic_created` in a chat where the bot lacks `can_manage_topics` still binds on the
  *first user message* (the bot can post into an existing topic without managing it).
- **Path doesn't exist / not a dir** → re-ask, never spawn into a bad cwd.
- **Agent dies** → next topic message re-spawns (status posted into the topic).
- **Two messages during `spawning`** → queued; the second injects once `bound` (no double-spawn).
- **Staleness / sender allow-list / at-most-once offset** semantics are unchanged — topic updates
  ride the same `stepUpdates` offset discipline.
- **Security**: spawning runs `claude` in a user-chosen path — same trust boundary as the human
  starting it; the sender allow-list still gates who can drive the bot.

## 11. Known deferrals (vs the flat path) — to close in later increments

- **Reply quote-anchor (increment 3).** The flat path builds a `↩ «…»` quote anchor from
  `reply_to_message`/`quote`; `topic-route` currently wraps prose plainly. A reply to a specific
  message INSIDE a topic therefore loses the quoted context the flat mode shows. Bring the
  anchor into `topic-route` when threaded replies land for topics.
- **Daemon-level slash commands inside a topic (increment 2).** In a bound topic every slash
  command is injected verbatim into the topic's agent (so `/compact`, `/stop` reach it). That is
  intentional — the topic IS the address — but the DAEMON-level verbs (`/status`, `/agent`,
  `/new`) thus reach the agent as literal text instead of being handled by the daemon. A
  per-topic command filter (or routing `/status`/`/new` to the daemon) is an increment-2 polish.
- **Topic rename name persistence (increment 2).** `forum_topic_edited` is swallowed (no
  mis-route) but the new name is not yet persisted into `TopicBinding.name`, so the tmux-window
  slug can go stale after a rename — the entrypoint should update it.
