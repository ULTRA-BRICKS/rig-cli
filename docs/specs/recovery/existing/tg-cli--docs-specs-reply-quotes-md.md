# Review addendum: tg-cli/docs/specs/reply-quotes.md

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
- [tg-cli#88](https://github.com/alex-mextner/tg-cli/issues/88) — tg-ctl status: autostart shows 'NOT enabled' when daemon is supervised by an external launchd job
- [tg-cli#93](https://github.com/alex-mextner/tg-cli/issues/93) — tg-ctl: cleanExit unlinks the pidfile unconditionally — a departing/loser daemon deletes the live winner's pidfile (false 'not running')
- [tg-cli#97](https://github.com/alex-mextner/tg-cli/issues/97) — AskUserQuestion forwards a duplicate Telegram card; second tap reads as 'expired'
- [tg-cli#99](https://github.com/alex-mextner/tg-cli/issues/99) — tg-ctl: question-answer state must survive socket-close + daemon restart (late-deliver + lossless reconnect)
- [tg-cli#100](https://github.com/alex-mextner/tg-cli/issues/100) — tg-ctl late-delivery: verify pane IDENTITY (not just liveness) before injecting a late answer
- [tg-cli#115](https://github.com/alex-mextner/tg-cli/issues/115) — /tasks [<agent>] [<status>] bot command: task table with lifecycle status (tickets + PR/CI state)
- [tg-cli#116](https://github.com/alex-mextner/tg-cli/issues/116) — autolink-msgrefs: replace literal tg#<id> with a quoted preview of the referenced message («начало сообщения…»), hyperlinked where possible
- [tg-cli#118](https://github.com/alex-mextner/tg-cli/issues/118) — question buttons: tapped answer never reaches the agent (CC >=2.1.198 requires hookEventName); multi-question ends on an extra manual Enter
- [tg-cli#121](https://github.com/alex-mextner/tg-cli/issues/121) — tg replies --session accepts a tmux window name, not only a pane id
- [tg-cli#130](https://github.com/alex-mextner/tg-cli/issues/130) — Reply quote-anchor should carry the original message's tg#<id>
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

# tg-cli — reply quotes forwarded into the agent (items 2, 3)

Repo: `~/.files/repos/tg-cli`. Pure logic in `features/tg-ctl/updates.ts`
(`buildReplyInject`), tests in `tests/ctl-reply-quote.test.ts`. Telegram shapes
in `features/tg-ctl/types.ts` (`reply_to_message`, `quote`).

## North star

When you **reply** (Telegram reply) to a message in the control chat — typically
an agent's own report — the agent should see *what you are answering*. The daemon
injects the reply with a one-line quote anchor:

```
↩ tg#5975 «[2026-06-12 14:30] migrated the DB…»
[TG from Alex] fix this — reply via tg
```

- **Item 2** — if you highlighted a **partial quote** (Telegram's quote
  selection, `message.quote.text`), that exact selection is the forwarded quote.
- **Item 3** — the anchor is `«[<date> <time>] <beginning of the message>…»`:
  the replied-to message's timestamp plus the start of its text and an ellipsis.
- **Original id (tg-cli#130)** — `tg#<id>` is `reply_to_message.message_id`, the
  Telegram id of the message you're answering (almost always the agent's own
  prior report). The `head` preview is truncated to 60 chars, so if the agent's
  context has compacted and the preview isn't enough to place it, it can pull
  the full original back with `tg replies agent --all-sessions --json | jq
  '.[] | select(.id == 5975)'` (or `tg replies find <snippet>`) instead of
  guessing. Renders even when the id happens to be `0` in a test fixture — the
  token is never silently dropped for a falsy-but-present id.

When both apply, the partial selection is the quoted content and it still carries
the `[date time]` + `…` framing.

## `buildReplyAnchor(m, opts)` / `buildReplyInject(m, name, opts)` (pure)

1. `original` = `reply_to_message.text` (or `.caption`), whitespace-collapsed.
2. `selected` = `m.quote.text` (the partial selection), whitespace-collapsed.
3. `body` = `selected || original` — item 2 wins when a selection exists.
4. `head` = first `60` chars of `body` + `…` (always ellipsised — item 3).
5. `when` = `opts.fmtTime(reply_to_message.date)`; defaults to a deterministic
   **UTC** `YYYY-MM-DD HH:MM`. The daemon injects a **local-time** formatter so
   the human sees Belgrade time while the pure module + tests stay deterministic.
6. `origId` = `reply_to_message.message_id`, rendered `tg#<id> ` — same
   convention as the `{id}` substitution in `wrapInbound` (tg-cli#28).
7. Result (`buildReplyAnchor`): `↩ origId«[when] head»`. `buildReplyInject`
   appends a newline + the normal `wrap(name, replyText, m.message_id)` — note
   THIS is the reply's own id (for `tg --reply-to`), distinct from `origId`
   above (the message being answered).

## Routing into the step function

In `stepUpdates`, a text message that **is a reply** and whose text is **not a
`/command`** becomes a `reply-route` action carrying the replied-to message id +
the `buildReplyInject` text; a reply whose text starts with `/` still runs the
command verbatim (so you can reply `/stop` to a message).

A **`!shell` reply is a special case** (codex #192): it also becomes a
`reply-route` (so it runs in the replied-to origin pane, not the default
last-message pane), but its `injectText` is the **raw `!…` text** — no wrap, no
quote anchor — so `!` stays at column 0 for the harness passthrough. The daemon's
history-stamping predicate (`isRoutedReply` in `tg-ctl`) mirrors this: a text
reply is stamped under its origin pane iff it is **not a `/command`** (which
includes `!shell`).

Photo/document replies first become `download-media` actions because the daemon
must fetch the file before it knows the local path to inject. The action still
carries the same `replyToMessageId` + `replyAnchor`; after download the executor
wraps `sent photo: <path>` / `sent file: <path>` plus the caption, prepends the
anchor, and hands it to the same `reply-route` handler. A screenshot reply to an
`ext` report therefore reaches `ext`, not the last active/default pane.
Exception: when the media caption itself starts with `/agent ...`, the explicit
agent selector wins over the replied-to origin. In that case the daemon routes
the media receipt as a `/agent` command and does not prepend the reply anchor.

## Reply routing: recognized origin vs LRU/MRU picker (v1.6.0)

`tg` records every outbound message id → `{paneId, cwd, ts}` into a bounded
routes map (`features/tg-ctl/routes.ts`, `tg-ctl.<botid>.routes.json`, tmux pane or `omp:<pid>`
last 300). The daemon's `handleReplyRoute`:

- **Recognized** — the replied-to id maps to a pane that STILL hosts an agent →
  inject the reply (with its anchor) straight into that origin pane. This is the
  multi-agent fan-in: replying to agent X's report reaches X, not the last
  sender.
- **Unrecognized** (id not in the map, or the pane is gone) → show the
  session-grouped **window picker** ordered by **LRU+MRU**: most-recently-messaged
  panes first, send-frequency as the tiebreaker, panes with no history last. Both
  signals are derived from the same routes map (`aggregateUsage` + `orderByLruMru`)
  — no separate usage state. A single agent skips the picker and injects directly.

The picker reuses the `/agent` selection machinery (`pendingAgent`, `tga:`
callbacks); the reply's pending message is stored `prewrapped` so the tap injects
the anchored text verbatim (no double-wrap).

### No-reply auto-bind to the CTO's own last-addressed pane (tg-cli#78)

A plain message that is **not** a reply has no anchor to recognize, so it goes
through `discoverTarget` → `pickTargetPaneFromSet`. With several live agent panes
and no registration pinning one, that picker returns `ambiguous`. Rather than
immediately asking, `discoverForInject` binds the message to the pane of the
**CTO's own last resolved inbound delivery** — persisted in
`tg-ctl.<bot>.last-user-target.json` (`features/tg-ctl/last-user-target.ts`) and
written ONLY on a confirmed delivery: an auto-bound inject, an explicit picker
tap, a named `/agent`, or a recognized reply — never on a failed inject, and
never on an agent's own outbound send. `resolveTasksScopeDir` (the `/tasks`
command's ambiguous-fleet default) shares this SAME anchor, so both surfaces
agree on "the default target".

This deliberately does **not** track "whoever spoke last" (an earlier mechanism,
`lastMessagePane` over the outbound-send routes map): an unrelated agent
proactively messaging the CTO could otherwise hijack his very next message —
the live incident this fix closes. `resolveLastUserTarget` also guards against
tmux pane-id reuse: the anchor's recorded cwd must match the live candidate's
`pane_current_path` (mirroring `routeMatchesPane`'s reply-route protection,
including rejecting an EMPTY live path, which would otherwise resolve to the
daemon's own cwd) — a stale/mismatched anchor is rejected, not trusted on
paneId alone.

Precedence is preserved: a recognized reply route still wins first; the
auto-bind only resolves an otherwise-ambiguous **non-reply**; and when there is
genuinely **no** anchor to prefer — none recorded yet, or its pane is
gone/pane-id-reused — it stays ambiguous and the button picker / ambiguous-target
reply fires (the unscoped fail-closed). The auto-bind REDUCES how often the
picker fires; it never replaces it.

Both paths honor defer-while-waiting: a reply to a pane with an open question is
queued (✍️) and flushed when the question is answered or released with no other
live question owning that pane. Successful deferred delivery flips the source
reaction to 👀 (see `docs/q-buttons-prerequisites.md`).

## Non-goals

- No multi-line `> quote` block — a single compact anchor line keeps the
  injected prompt readable (the full original is one tap away in Telegram).
- No forwarding of media in the replied-to message — text/caption only.
