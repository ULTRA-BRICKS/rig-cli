# Prior grooming specification — retained baseline

Current recovery coordinator: https://github.com/alex-mextner/rig-cli/issues/521

This earlier design is not implementation evidence. Consult the current domain contracts and live issue ledger before executing it.

# Telegram control: durable delivery and scoped approvals

Status: proposed implementation specification; documentation only.
Date: 2026-09-10.
Epic: [tg-cli#380](https://github.com/alex-mextner/tg-cli/issues/380).
Parent: [rig-cli#514](https://github.com/alex-mextner/rig-cli/issues/514).
Producer contract: [harness-cli#22](https://github.com/alex-mextner/harness-cli/issues/22).

## Outcome and ownership

An accepted Telegram message has a recoverable intent and an inspectable delivery outcome for the correct agent. One busy target does not freeze unrelated conversations. A human decision survives a disconnected requesting process without becoming authority for another action.

Tg-cli owns Telegram updates, cards, routing preferences, durable inbound/outbound state and human-decision presentation. Harness-cli owns native session discovery, control protocols, capabilities and transport lifecycle. Agent-tools owns guard policy and the original scoped approval request. Rig owns service provisioning/reconciliation.

## Verified baseline

At inspected main `74de8f5d9ee4c828ac6d5db2053f469f97f4f024`, `features/tg-ctl/omp.ts` still defines ACP types, protocol method names and OMP session discovery. The new harness one-shot nudge implementation waits on session/prompt and terminates its child; it is not a replacement for the complete persistent question/permission lifecycle. [#377](https://github.com/alex-mextner/tg-cli/issues/377) tracks that remaining migration.

Keep existing fixes credited: [#369](https://github.com/alex-mextner/tg-cli/issues/369)/PR #378 for the multi-candidate headless anchor, [#372](https://github.com/alex-mextner/tg-cli/issues/372)/PR #370 for truthful posted/dropped/expired outcomes, and [#349](https://github.com/alex-mextner/tg-cli/issues/349)/PR #376 for late hatch retention. The latter's same-hook retry description needs exact-action negative validation; this document does not claim a confirmed exploit without inspecting that implementation and its producer. [#363](https://github.com/alex-mextner/tg-cli/issues/363) is a narrower awareness-nudge delivery, not completion of generic harness integration.

## Delivery state machine

[#360](https://github.com/alex-mextner/tg-cli/issues/360) owns durable-before-ack acceptance. Proposed semantic states, whose exact schema belongs in implementation:

| State | Meaning |
| --- | --- |
| received | Update observed but not yet recoverable |
| accepted | Intent and target context committed to the durable owner |
| dispatching | Delivery attempt started with a correlated operation ID |
| delivered | Adapter supplies the agreed receipt; not inferred from writing a pipe |
| completed | Agent operation/turn completed, if that capability is available |
| failed / unknown | Typed failure or inability to determine receipt; not a silent success |

Persist the update ID, message, selected target identity/generation and operation ID before advancing the durable Telegram offset. If persistence fails, do not acknowledge the intent as recoverable. Recovery replays pending work and preserves the same idempotency identity.

At-least-once processing with idempotent acceptance is the baseline. Exactly-once model execution requires adapter support; do not advertise it from local deduplication alone. An ambiguous crash after provider receipt but before recording it must be surfaced or reconciled by the adapter's receipt contract, not concealed as certainty.

The poll loop must not wait for full model-turn completion. Accepted work belongs to an explicit durable/lifecycle owner, not an untracked fire-and-forget Promise. Normal shutdown drains or persists under that contract; forced death is covered by recovery tests.

## Concurrency and route identity

[#357](https://github.com/alex-mextner/tg-cli/issues/357) owns bounded cross-target concurrency and strict per-target ordering. Use canonical target/session generation as the ordering key. A busy or failed target cannot hold a global await across unrelated targets. Define queue saturation/backpressure and expose it; do not create unbounded parallel children to make the UI feel responsive.

[#354](https://github.com/alex-mextner/tg-cli/issues/354) covers tmux generation identity. Pane `%N` after a server restart is not automatically the previous pane's agent. [#373](https://github.com/alex-mextner/tg-cli/issues/373) covers preserving the real headless origin, including Codex, through the generic contract rather than defaulting to a Claude route.

[#379](https://github.com/alex-mextner/tg-cli/issues/379) handles stale/dead awareness-nudge targets. A recent transcript is activity evidence, not liveness. Revalidate native generation and adapter capabilities; definitively dead targets do not receive repetitive nudges. Unknown evidence is reported without inventing a new age-only liveness rule or silently discarding pending human decisions.

The existing successful last-user-target behavior from #369 must continue to work with multiple candidates. Bare `/agent` remains a picker; a valid explicit anchor routes follow-ups without asking again. Stale/ambiguous anchors must not choose a different agent silently.

## Generic interactive transport migration

[#377](https://github.com/alex-mextner/tg-cli/issues/377) depends on the reviewed CLI/IPC contract from [harness-cli#22](https://github.com/alex-mextner/harness-cli/issues/22). Compare persistent stdio and any required socket binding in the producer spec; do not copy one more ACP client into tg-cli.

Required consumer behavior: negotiate schema/capabilities, inspect/attach using canonical identity, submit messages with operation IDs, receive events, submit scoped permission decisions and detach/cancel explicitly. Unsupported capabilities produce a typed result. Tg-cli must not know ACP method names, framing, provider credential paths or OMP session-file layouts after cutover.

Migrate in parity-tested slices: generic contract fixtures, producer lifecycle, consumer adapter, then removal of old call sites/cache logic. All reply, prompt, question and permission paths must move, not only periodic nudges. Use a second or fake adapter to prove the Telegram consumer requires no protocol-specific changes.

Preserve the existing long-lived abandoned-permission retention contract, including the currently documented 12-hour behavior associated with #377, until an explicit lifecycle decision changes it. This is compatibility with an existing rule, not permission to add a new deadline. The original instruction in #360 rejects new timer/deadline/expiry workarounds for the delivery failure. Do not replace durable acceptance with a shorter wait-and-abandon path. Reaping a wedged transport must not erase pending decisions.

## Scoped human decisions

[#374](https://github.com/alex-mextner/tg-cli/issues/374) owns request validation and pending-question guard behavior. Reject blank requests and ambiguous/truncated CLI input before sending a card. A human must see the actual action being authorized, including its relevant scope. An unscoped hatch-specific exception must not bypass an existing pane-scoped pending permission guard.

The original request emitter and Telegram must agree on action identity. A safe retained decision binds a stable request/fingerprint to the exact command/argv, policy/hook, repository/worktree scope and originating session generation. Same hook name alone is insufficient to establish same action. Retry of the same still-relevant action can consume a decision once; changed command, scope or origin requires a new decision. Store the decision and consumption atomically enough to prevent concurrent double use.

Required negative cases: same hook but different command; same command in another repository; reused session identity; two concurrent retries; already consumed decision; late deny; disconnect before tap; process restart before retry. Inspect #349's implementation and agent-tools emitter first, reuse any existing exact-scope protection, and file a confirmed defect only where the negative test demonstrates a gap.

Keep these facts separate in both client output and history: not posted, posted, unanswered/expired under the existing lifecycle, explicit deny, approved, queued for a matching retry and consumed. A timeout or lost transport is not a human denial. A stored approval is not proof that the protected command executed. [agent-tools#702](https://github.com/alex-mextner/agent-tools/issues/702) must consume these distinctions.

[#356](https://github.com/alex-mextner/tg-cli/issues/356) provides correlated history; join by explicit request/operation identity, not a nearby timestamp or matching prose. Redact credentials and avoid copying private unrelated session text into audit records.

## Service lifecycle and tests

[#344](https://github.com/alex-mextner/tg-cli/issues/344) defines first-run/autostart behavior while respecting explicit disable and the shared service owner. Do not introduce a second service installer or silently re-enable a service the operator disabled.

[#375](https://github.com/alex-mextner/tg-cli/issues/375) replaces timing-sensitive concurrency/lifecycle tests with explicit barriers and deterministic fixtures. Every test owns its daemon/child processes and teardown. Cleanup validates process generation/ownership; it must not kill unrelated user processes by binary name or old PID.

Fault-injection matrix:

| Scenario | Expected invariant |
| --- | --- |
| Crash before durable accept | Offset not committed as successful acceptance |
| Crash after persist, before dispatch | Recovery finds the same pending operation |
| Duplicate update/retry | No duplicate acceptance or approval consumption |
| Target A wedged, target B healthy | B progresses; A remains inspectable |
| Two messages for one target | Stable per-target ordering |
| Client disconnect, late human tap | Scoped decision survives under existing retention |
| Changed action with same hook | No replayed authorization |
| Stale pane/PID/native generation | No silent routing to a replacement agent |
| IPC child exit with pending permission | Transport failure does not erase decision state |
| Test teardown | No orphan fixture process and no live user process touched |

## Delivery order and closure

P0: approval shape/scope and pending-question guards. P1: generic persistent control, durable acceptance, concurrency, correct identity and deterministic lifecycle tests. P2: history and autostart polish.

Closure requires producer AND consumer acceptance at named revisions, exact commands/results, durable artifacts and separate authorized live proof when required. No real Telegram cards, live daemon restart or credential actions are part of documentation verification. Narrow closed fixes remain credited; incomplete end-to-end outcomes remain open.
