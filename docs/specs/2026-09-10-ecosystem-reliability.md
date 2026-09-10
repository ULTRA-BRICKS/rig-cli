# Rig ecosystem reliability and grooming baseline

Status: proposed implementation specifications and an evidence-based backlog map; no runtime changes.
Date: 2026-09-10.
Owning epic: [rig-cli#514](https://github.com/alex-mextner/rig-cli/issues/514).

## Scope and method

The user requested grooming of the rig-cli ecosystem, specifically agent-tools, review-cli, harness-cli and tg-cli, including rewriting/reopening prematurely completed work, linked epics and specifications committed to git.

This pass reviewed the recent cross-repository issue backlog, selected older dependencies, issue discussions, current source and selected PR metadata. It is not a claim that every historical issue was exhaustively audited. No full repository test suite, live provider account, operator machine or daemon was exercised during this grooming. Previous test/live reports are attributed as previous evidence, not as checks performed now.

An issue was reopened when the original user-visible outcome remained broader than its closing evidence. A narrower genuinely delivered fix stays credited. Missing durable evidence alone is not proof that code is absent; uncertainty becomes an explicit acceptance blocker rather than an invented defect.

## Epic and specification map

| Owner | Epic | Specification |
| --- | --- | --- |
| rig-cli | [#514 ecosystem coordination](https://github.com/alex-mextner/rig-cli/issues/514) | This file |
| harness-cli | [#22 universal control](https://github.com/alex-mextner/harness-cli/issues/22) | [Harness contracts](https://github.com/alex-mextner/harness-cli/blob/docs/ecosystem-grooming-2026-09-10/docs/specs/2026-09-10-harness-contracts.md#control-and-ipc) |
| harness-cli | [#26 complete inventory/health](https://github.com/alex-mextner/harness-cli/issues/26) | [Harness inventory](https://github.com/alex-mextner/harness-cli/blob/docs/ecosystem-grooming-2026-09-10/docs/specs/2026-09-10-harness-contracts.md#inventory-and-health) |
| agent-tools | [#718 trustworthy guards](https://github.com/alex-mextner/agent-tools/issues/718) | [Guard contracts](https://github.com/alex-mextner/agent-tools/blob/docs/ecosystem-grooming-2026-09-10/docs/specs/2026-09-10-guard-contracts.md) |
| review-cli | [#492 reliable review runtime](https://github.com/alex-mextner/review-cli/issues/492) | [Review runtime](https://github.com/alex-mextner/review-cli/blob/docs/ecosystem-grooming-2026-09-10/docs/specs/2026-09-10-review-contracts.md#review-runtime) |
| review-cli | [#487 interactive entity browser](https://github.com/alex-mextner/review-cli/issues/487) | [Entity browser](https://github.com/alex-mextner/review-cli/blob/docs/ecosystem-grooming-2026-09-10/docs/specs/2026-09-10-review-contracts.md#entity-browser) |
| review-cli | [#484 versioned spec review](https://github.com/alex-mextner/review-cli/issues/484) | [Versioned spec review](https://github.com/alex-mextner/review-cli/blob/docs/ecosystem-grooming-2026-09-10/docs/specs/2026-09-10-review-contracts.md#versioned-spec-review) |
| tg-cli | [#380 reliable Telegram control](https://github.com/alex-mextner/tg-cli/issues/380) | [Telegram contract](https://github.com/alex-mextner/tg-cli/blob/docs/ecosystem-grooming-2026-09-10/docs/specs/2026-09-10-telegram-control.md) |
| rig-cli | [#510 TCC defense in depth](https://github.com/alex-mextner/rig-cli/issues/510) | TCC section below |

These branch URLs point to committed documentation. They do not imply merge or implementation. Keep branch links valid until references are migrated to accepted main revisions; do not remove the documentation branches first.

## Ownership and integration boundaries

Harness-cli owns protocol adapters, source/account discovery and factual observations. Agent-tools owns portable guard contracts and bridge normalization. Review-cli owns board/role execution and evidence. Tg-cli owns Telegram interaction and recoverable message/decision delivery. Rig-cli installs, reconciles and reports their declared state through its existing plan/runner/status mechanisms.

Do not introduce a second scheduler, service supervisor, provider credential parser or ACP engine to solve one cross-repository integration gap. A producer-only PR cannot close a producer-and-consumer outcome. Consumer behavior must be tested against the versioned public contract, including unsupported and unknown states.

## Dependency and priority plan

P0: protected-operation coverage and exact approval scope. P1: native identity, truthful inventory/capacity, durable message delivery and runtime resource safety. P2: interactive entity/spec-review surfaces and maintenance. These are relative grooming priorities, not invented release dates, staffing commitments or effort estimates.

Key dependency paths:

- Harness IDs/capabilities and persistent control (#22) enable tg-cli#377. Inventory #26 provides factual source/account coverage to review-cli#495 and capacity consumers.
- Agent-tools#573 is completed by native OMP #712 and Codex #673; those identities also support TCC attribution without new discovery heuristics.
- Review-cli#472 produces the capacity proof that agent-tools#705 consumes. Unknown, stale or incomplete evidence cannot authorize an exception.
- Review-cli#476 remains design-only until that capacity crisis is resolved AND Alex explicitly approves implementation. A grooming/specification PR is not that approval.
- Review-cli#493 revision history precedes #494 persisted anchor-change auditing. #495 shared entities precedes #496 TUI; a mocked contract can support development before live source coverage is complete.
- TCC event precision #516 precedes reliable auto-response #517; attribution #518 precedes target-specific notification #519. Operational rollout #515 is independent acceptance, not implied by source merge.

## TCC defense in depth

### Baseline and reopening

[PR #511](https://github.com/alex-mextner/rig-cli/pull/511), merge `e23ac09ed9852f7707c783452c392945eeb38268`, delivered the Phase-1 observer/provisioning source. The inspected `riglib/tcc_watch.py` explicitly states that response, attribution, notification and dedicated status/drift reporting remain unfinished.

The old #510 checklist checked live managed rollout while its explanation said rollout was deferred. It also treated descriptions of deferred phases as completed follow-up tasks. The issue is reopened as an epic with actual children; the implemented observer remains credited.

A [prior live-script report](https://github.com/alex-mextner/rig-cli/issues/510#issuecomment-5614088510) recorded 162 TCC events in six seconds. It explicitly says the broad marker set includes routine DeveloperTool policy checks and that log rotation is absent. This supports a real observer smoke test, not a claim that 162 visible permission dialogs occurred or that the managed launch service was accepted.

### Child contracts

| Task | Result and critical negative case |
| --- | --- |
| [#515 rollout/status](https://github.com/alex-mextner/rig-cli/issues/515) | Existing rig areas/drift report installed/running/degraded/unknown; a loaded but nonfunctional observer is not healthy |
| [#516 precision/storage](https://github.com/alex-mextner/rig-cli/issues/516) | Distinguish routine policy checks, prompt evidence, actual outcome and unknown; bound/rotate storage without corrupting readers |
| [#517 deny-only response](https://github.com/alex-mextner/rig-cli/issues/517) | Separately reviewed identity-checked response; no action on an ambiguous or unrelated window |
| [#518 attribution](https://github.com/alex-mextner/rig-cli/issues/518) | Proven process/session generation; reused PID or concurrent native siblings do not create false attribution |
| [#519 notification](https://github.com/alex-mextner/rig-cli/issues/519) | Durable deduplicated explanation of the actual outcome, never a claimed denial from observation alone |
| [#512 prevention](https://github.com/alex-mextner/rig-cli/issues/512) | Additional preventive eval-path coverage; explicitly not proof of a complete execution sandbox |

### Response decision and safe fallback

Compare supported Accessibility-based observation/deny interaction with observe-and-notify fallback. The response implementation must define verified window/process/service identity, supported OS behavior, localization, simultaneous/disappearing dialogs, missing prerequisites, audit and an operator-controlled disable path. Record explicit design approval before any implementation that interacts with a real consent UI.

No grant/allow action exists in the proposed response surface. Unknown identity yields no click. Do not edit TCC databases, grant permissions, bypass the platform's authorization boundaries or use this watcher as a general privileged UI automation endpoint. A protected prompt's mere appearance does not prove which agent caused it; attribution may remain unknown.

### Integration proof

Replay redacted fixtures establishes parsing/classification. Isolated plan/status tests establish managed artifact behavior. An authorized actual observer/service run establishes rollout. A separately authorized response acceptance establishes that a specific dialog was actually denied. These are distinct evidence levels. Do not trigger a real prompt merely to collect documentation proof. Pending notification delivery uses generic harness/TG contracts and must survive restart without sending duplicate incident storms.

## Runtime safety

### Memory pressure — #507

[Issue #507](https://github.com/alex-mextner/rig-cli/issues/507) originally requested diagnosis and remediation. The earlier investigation reported swap pressure, September 8 background-process jetsam events, a September 9 shutdown stall/forced reboot and a large OMP fleet. It concluded that MEMORY.md broadcast discipline plus a 10-agent rule already prevents recurrence, without locating enforcement or demonstrating a bounded concurrent-launch test.

Reopening preserves that forensic work while separating observations, causal inference and proven prevention. Identify supported admission/fan-out paths and their existing owners first. A shared limit must account atomically for concurrent launchers and children, expose queue/refusal, release reservations on terminal state and recover from crashed owners without double admission. Avoid broadcast-triggered amplification rather than asking every idle agent to acknowledge fleet management.

The previously mentioned 10-agent cap is a policy input to validate, not proof of an implemented mechanism or a universally correct machine limit. Specify the resource/admission policy with measurements and residual risks. Implement at the actual owning launch/orchestration boundary; rig provisions declared configuration rather than becoming a second scheduler.

Acceptance includes isolated simultaneous-launch, stale-reservation, restart and fan-out tests plus authorized bounded before/after responsiveness/resource observations. Do not stress the user's production machine until failure. No unscoped killall/pkill, forced reset, blind OS ceiling increase or claim that written discipline guarantees prevention.

### PTY and recovery — #495 / #496

[Issue #495](https://github.com/alex-mextner/rig-cli/issues/495) contains a PTY-exhaustion hypothesis and an already-delivered status advisory from PR #485. Its requested live counts/holders remain a separate investigation. Measure the actual platform limit, current holders and growth/release over time before claiming a leak or a universal OS-setting fix. Any privileged limit change requires platform-specific verification and operator authorization; a guessed fixed value and persistence file are not acceptance criteria by themselves.

[Issue #496](https://github.com/alex-mextner/rig-cli/issues/496) tracks restore integration gaps beyond the narrower OMP/Codex helpers delivered in #494/PR #487. Verify exact session/cwd identity, disabled-handler cleanup, ambiguous simultaneous harnesses and independent private-tmux scenarios. Never select an arbitrary newest session from another cwd as successful recovery.

## Outcome-based closure policy

Every groomed implementation task should state the observable outcome, motivation/impact, owning parent, committed specification, dependencies, delivered baseline, remaining acceptance and evidence. Preserve important original requirements instead of rewriting them around whatever the last PR happened to implement.

| Evidence status | What may be claimed |
| --- | --- |
| Source exists on an unmerged branch | Implemented on that branch only |
| PR merged | That diff is delivered to the named branch/revision |
| Prior CI/test report | Those reported tests at that revision; not a new run |
| Fixture integration passes | The simulated producer/consumer contract is covered |
| Authorized live acceptance recorded | The stated real environment/outcome was verified |
| Deferred, unavailable or force-checked | Work remains open or needs an explicit scope decision |

A local screenshot path is not a durable artifact. A checked box saying live proof was impossible does not convert the proof into success. Zero candidates is not all exhausted; unknown is not healthy; a stored approval is not executed work; an accepted intent is not a completed turn.

### Reopening and retained completion ledger

- Reopened harness-cli#22: one-shot nudge exists, full CLI/IPC ownership and consumer migration do not.
- Reopened rig-cli#510: Phase-1 observation exists, the complete protection/attribution/notification outcome does not.
- Reopened rig-cli#507: investigation exists, claimed prevention needs enforcement and validation.
- Corrected already-open agent-tools#573: status:done and checked native/live criteria contradicted admitted gaps.
- Corrected already-open review-cli#484: open PR and contradictory version-history acceptance; created #493/#494.
- Preserved review-cli#487's delivered Phase 0 while creating #495/#496 for the actual interactive outcome.
- Retained narrower completion evidence such as tg-cli#369/#372, review-cli#474, rig-cli#501 and the shipped guard/restore fixes; a broader epic does not invalidate their useful contributions.

## Documentation delivery

The five repository specs are committed on `docs/ecosystem-grooming-2026-09-10` and submitted as separate documentation PRs. They do not auto-close implementation epics. GitHub issues carry reciprocal parent/child links and explicit dependency text; these Markdown relationships should not be mistaken for a native Sub-issues API mutation. After acceptance, migrate links deliberately to the accepted main revisions and retain historical evidence.
