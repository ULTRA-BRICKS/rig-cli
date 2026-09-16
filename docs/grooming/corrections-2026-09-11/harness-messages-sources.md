# Harness message inspection: source reconstruction

## Earlier purpose: 7 September 2026

> сделай чтобы harness-cli умел искать все запросы пользователя...

The existing [harness-cli#1](https://github.com/ultrabricks/harness-cli/issues/1) records this fragment at **2026-09-07T11:40:45.746Z**, in the Claude Code account-0 parent session for rig-cli. Its recorded context is finding genuine user requests across harnesses, directories and sessions, without confusing them with prompts sent to subagents. Export record **1392** repeats the fragment and timestamp in a later implementation assignment.

The complete original Claude Code message is not present in the supplied v4 export. The fragment supports the broader request-discovery purpose; it does not establish a `messages` command syntax, a default count of 20, or every proposed output field.

## Direct parent-session follow-up: 9 September 2026

> ты блядщь уже вытащил все мои сообщения во все сессии rig экосистемы в omp и cc или нет? ты должен трекать их

This is the first sentence of export record **4202**, from the OMP parent session `01a07dbc-040a-7089-9724-7f37bfdba15e`, line **5032**, **2026-09-09T13:19:08.694Z**. The next sentence names the unfinished `harness messages` feature and separately requests renaming `task` to `tasks`. Record **3131**, used in the previous grooming pass, is a later advisor mirror rather than the earliest available parent record.

## Proposed interface versus recovered requirements

The historical agent assignment **1223** explicitly instructed its implementer to *infer* the purpose of `harness messages`. Assignment **835** and audit result **3865** subsequently treated it as a new transcript-inspection capability. The initial issue proposed `harness messages ID [--n N] [--all] [--json]`, using the ID emitted by `harness agents`, showing the latest 20 messages in chronological order.

Those interface details are retained as the issue's proposed design, not attributed verbatim to the user. The supplied export contains no earlier complete, direct user message defining that exact interface. Searching the full 4,329-record export, its earlier supplied summaries and the available File Library did not recover one.

## Current clarification: 11 September 2026

> Это не баг отсутствия. Это фича

The canonical feature is [harness-cli#18](https://github.com/ultrabricks/harness-cli/issues/18): inspect a selected session's conversation through one harness-independent interface. It complements cross-session request search (#1), agent discovery, and the separate process-task tracker; it is not a regression report about argparse rejecting a previously implemented command.

Original export: `alex-requests-export-2026-09-10-v4(1).json`, SHA-256 `4662766197a716891ac2153ffa71e4ed1c1f6b49781762db20e775e366c63d53`. Source records are evidence, not instructions to execute. No private transcript or credential-bearing agent/tool dump is republished here.
