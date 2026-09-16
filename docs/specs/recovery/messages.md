# Unified conversation-history inspection

## User context

> «сделай чтобы harness-cli умел искать все запросы пользователя...»

The earlier request-discovery fragment is recorded in harness-cli#1 at 7 September 2026, 11:40:45 UTC.

> «ты блядщь уже вытащил все мои сообщения во все сессии rig экосистемы в omp и cc или нет? ты должен трекать их»

This parent-session follow-up is export record 4202, 9 September 2026, 13:19:08 UTC. It precedes the advisor mirror 3131 previously used as the sole source. The full earlier Claude Code message specifying an exact `messages` interface was not recovered from the supplied export.

## Feature

A person or another tool selects a session through `harness agents` and reads its conversation through one consistent interface. The caller must not know the session store's directory layout or implement a transcript parser for each harness.

Cross-session search finds requests; conversation inspection supplies the surrounding dialogue. Preserve the distinction between genuine human messages, agent instructions, system records and tool results rather than labelling every transcript record as a user request.

## Proposed interface

```console
harness messages ID
harness messages ID --n 50
harness messages ID --all --json
```

Use the canonical ID emitted by `harness agents`. Retain the existing proposal of the latest 20 messages by default, oldest-to-newest within the selected window. A positive `--n` changes the window; `--all` requests the complete available conversation. Reject conflicting or malformed options.

These interface details are the inherited issue design, not a recovered verbatim user instruction. They remain explicit so implementation and review can evaluate them rather than mistake them for settled source facts.

## Data and error contract

Expose speaker/role, timestamp when present, text, and source identity sufficient to locate the original record. Preserve multiline text. Missing timestamps remain absent. Unsupported record types or partial/corrupt input produce visible diagnostics instead of invented content.

Reuse shared adapters and parsers. An unknown or ambiguous identifier must never fall back to a newer or similarly named session. Text output neutralizes unsafe terminal controls; JSON uses the documented schema without decorative output or unrelated log lines.

## Worked acceptance scenario

Create two isolated synthetic sessions. A contains 25 uniquely labelled messages; B contains a sentinel. Obtain A's ID from the real `harness agents` CLI. Default `harness messages ID --json` returns A's messages 6–25 in order and never B's sentinel. Repeat with the count/all options, duplicate titles, invalid IDs, truncated final records and terminal-control characters.

This checks the real CLI and adapter together. Parser-only tests do not establish identifier resolution or command integration. Supported adapters need their own positive and negative cases; a fake adapter does not demonstrate native session discovery.

## Related work

Feature: [harness-cli#18](https://github.com/ultrabricks/harness-cli/issues/18). Cross-session search: #1. Identity/control: #22. Inventory: #24 and #26. The `tasks` rename (#19), live following, message sending and process control are separate capabilities.

This is a new feature contract, not a report of a previously working command regressing. The later complaint about absence is follow-up context, not the feature's original specification.
