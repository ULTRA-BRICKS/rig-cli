# Review addendum: tg-cli/docs/specs/autolink-tasks.md

Source revision: `b640b65e7b30321ec8c14259896212f1fe78626f`.

This source document is retained with its original draft/approved/skipped status. This addendum does not approve previously skipped work. Root `rig-cli/SPEC.md` describes Evolve, not the entire rig ecosystem.

Current recovery contract: [Auxiliary CLI behavior and end-to-end user workflows](../auxiliary.md). Governing epic: https://github.com/alex-mextner/rig-cli/issues/521.

## Example and revalidation

A CLI is invoked from outside its repository with a valid input, an invalid input and an unavailable optional backend. Successful output, nonzero failure, cancellation and cleanup are separately asserted. A mock backend passing does not establish that a live account, local model or audio device was exercised.

## Source quotations

> Нужно ещё проверить, что это работает на машине, и приложить к отчёту проверяемые доказательства.

Source **Q202**; compiled-memory-attribution; MEMORY.md standing lesson: Merge is not this machine; on disk is not PATH; PDF page(s) 74. Thematic recovery requirement, not retroactive authorship of this entire specification.

<details><summary>Original wording (before editorial correction)</summary>

> надо еще проверить что это на машине работает и в отчете приложить пруфы проверяемые

</details>

> Все доказательства должны быть проверяемыми.

Source **Q028**; compiled-fragment-attribution; rig-zerobug/rig-fable session 2026-09-04..07; agent=Full175Report, ResultReport; PDF page(s) 39. Thematic recovery requirement, not retroactive authorship of this entire specification.

<details><summary>Original wording (before editorial correction)</summary>

> все пруфы должны быть проверяемы

</details>

## Linked acceptance backlog

- [tg-cli#27](https://github.com/alex-mextner/tg-cli/issues/27) — /new command — spawn a new agent session (interactive inline-button options)
- [tg-cli#29](https://github.com/alex-mextner/tg-cli/issues/29) — File-excerpt attachment — quote referenced lines for path:line refs (currently broken)
- [tg-cli#31](https://github.com/alex-mextner/tg-cli/issues/31) — CodeQL JS/TS: fix-or-justify 8 pre-existing TS findings (surfaced by #25 rig rollout)
- [tg-cli#80](https://github.com/alex-mextner/tg-cli/issues/80) — tg VERSION literal (1.15.0) has DRIFTED from package.json (1.0.0) — derive it dynamically
- [tg-cli#95](https://github.com/alex-mextner/tg-cli/issues/95) — tg --file report.html renders RAW HTML tags in PDF instead of formatted output
- [tg-cli#102](https://github.com/alex-mextner/tg-cli/issues/102) — md-pdf: convertMdToPdf lacks the network-blackhole + drops embed-resources hardening that convertHtmlToPdf got in #96
- [tg-cli#103](https://github.com/alex-mextner/tg-cli/issues/103) — pdf-render: Chrome still loads absolute-path / file:// <img> subresources into the printed PDF (both md-pdf and html-report paths)
- [tg-cli#134](https://github.com/alex-mextner/tg-cli/issues/134) — tg replies: multi-id sends (split/album) show duplicate lines in list/find
- [tg-cli#136](https://github.com/alex-mextner/tg-cli/issues/136) — tg replies history.jsonl: outbound writer has no RMW lock (unlike routes.json)
- [tg-cli#138](https://github.com/alex-mextner/tg-cli/issues/138) — --title should refuse a tg#<id> reference
- [tg-cli#140](https://github.com/alex-mextner/tg-cli/issues/140) — pre-send-photo hook: forward outgoing caption as --intent to review visual
- [tg-cli#142](https://github.com/alex-mextner/tg-cli/issues/142) — pre-send-photo: _safe_intent() silently drops an unencodable caption without warn()
- [tg-cli#160](https://github.com/alex-mextner/tg-cli/issues/160) — Fix tg voice transcript confirmation and reactions
- [tg-cli#163](https://github.com/alex-mextner/tg-cli/issues/163) — pre-send-photo: selection-highlight veto downgraded to WARN, not BLOCK
- [tg-cli#171](https://github.com/alex-mextner/tg-cli/issues/171) — Auto-continue transient API overload stops
- [tg-cli#188](https://github.com/alex-mextner/tg-cli/issues/188) — tg: hard-block stray CJK stuck mid-word into Latin/Cyrillic text
- [tg-cli#191](https://github.com/alex-mextner/tg-cli/issues/191) — tg replies: agent-scoped history (attribution + default/--agent/--all)
- [tg-cli#205](https://github.com/alex-mextner/tg-cli/issues/205) — tg: block mixed-script homoglyph/mojibake garbage tokens (почčesна)
- [tg-cli#210](https://github.com/alex-mextner/tg-cli/issues/210) — tg-cli: skip fenced code blocks when auto-converting markdown pipe tables
- [tg-cli#215](https://github.com/alex-mextner/tg-cli/issues/215) — tg: don't hard-block Cyrillic-affixed acronyms like antiDDoS-style mixed-script tech terms
- [tg-cli#216](https://github.com/alex-mextner/tg-cli/issues/216) — tg: make escalation-format section validation actually section-aware
- [tg-cli#217](https://github.com/alex-mextner/tg-cli/issues/217) — tg: require a real file:line, not just a path, in the escalation-format where-to-look field
- [tg-cli#221](https://github.com/alex-mextner/tg-cli/issues/221) — tg: harden pipe-table rendering for ragged rows and tighten the where-to-look file reference check
- [tg-cli#223](https://github.com/alex-mextner/tg-cli/issues/223) — tg: escalation-format validator structure checks have false-positive and false-negative edge cases
- [tg-cli#225](https://github.com/alex-mextner/tg-cli/issues/225) — tg: report the mixed-script guard's offending-token position against the authored input, not the rendered table
- [tg-cli#226](https://github.com/alex-mextner/tg-cli/issues/226) — tg: mixed-script guard hard-blocks common Russian dev slang like zapushil, zadeployil (Cyrillic prefix plus English verb stem plus Cyrillic suffix)
- [tg-cli#227](https://github.com/alex-mextner/tg-cli/issues/227) — tg: mixed-script guard is bypassable via invisible format characters splitting a run
- [tg-cli#228](https://github.com/alex-mextner/tg-cli/issues/228) — transmit() preflight refusals (rich-HTML / flood-cap) leak pdfTempDirs via process.exit
- [tg-cli#237](https://github.com/alex-mextner/tg-cli/issues/237) — Add offline dry-run validation for tg escalation-format gate
- [tg-cli#246](https://github.com/alex-mextner/tg-cli/issues/246) — tg --detect-model misbrands omp sessions as claude (CLAUDECODE env checked before ancestry)
- [tg-cli#248](https://github.com/alex-mextner/tg-cli/issues/248) — Review this uncommitted git diff for bugs, regressions, security issues,
- [tg-cli#249](https://github.com/alex-mextner/tg-cli/issues/249) — Review OMP model-detection ordering diff
- [tg-cli#251](https://github.com/alex-mextner/tg-cli/issues/251) — Review omp model-detection ordering diff for cross-file consistency
- [tg-cli#252](https://github.com/alex-mextner/tg-cli/issues/252) — Review OMP detection ordering for cross-file consistency
- [tg-cli#253](https://github.com/alex-mextner/tg-cli/issues/253) — Review OMP lineage-first model detection diff
- [tg-cli#254](https://github.com/alex-mextner/tg-cli/issues/254) — Review OMP model-detection order diff
- [tg-cli#255](https://github.com/alex-mextner/tg-cli/issues/255) — Read-only review of the current uncommitted tg-cli diff for bugs, regres
- [tg-cli#256](https://github.com/alex-mextner/tg-cli/issues/256) — Review OMP model detection ordering
- [tg-cli#257](https://github.com/alex-mextner/tg-cli/issues/257) — Review OMP model-detection ordering diff
- [tg-cli#258](https://github.com/alex-mextner/tg-cli/issues/258) — Review OMP model-detection ordering diff
- [tg-cli#259](https://github.com/alex-mextner/tg-cli/issues/259) — Escalation gate hasWallOfText measures RAW line length incl. HTML markup — any single-line HTML body >350 chars is deny-by-default blocked
- [tg-cli#263](https://github.com/alex-mextner/tg-cli/issues/263) — harness-event mislabels bare StopFailure notifications with stale tmux window name instead of live harness kind
- [tg-cli#270](https://github.com/alex-mextner/tg-cli/issues/270) — escalation-format: withVisualLineBreaks is markup-context-blind (breaks inside <pre>/<code>) and its block-tag set is incomplete
- [tg-cli#284](https://github.com/alex-mextner/tg-cli/issues/284) — Review last-user-target rename for cross-file consistency
- [tg-cli#314](https://github.com/alex-mextner/tg-cli/issues/314) — Review Codex rig hook dedup diff
- [tg-cli#319](https://github.com/alex-mextner/tg-cli/issues/319) — Review omp non-tmux diff
- [tg-cli#336](https://github.com/alex-mextner/tg-cli/issues/336) — Audit ACP-backed omp routing for unsafe delivery paths
- [tg-cli#353](https://github.com/alex-mextner/tg-cli/issues/353) — ACP session/prompt stays wedged forever after one failed prompt (post-#346)
- [tg-cli#357](https://github.com/alex-mextner/tg-cli/issues/357) — Delivery isolation: preserve per-target ordering without waiting for another agent's response

## Preserved specification

# tg-cli — autolink-tasks feature spec

Repo: `~/.files/repos/tg-cli`. Feature lives in `features/autolink-tasks/`, tests in
`tests/autolink.test.ts`. Built on top of the auto-attach architecture (feature flags,
`renderText` hook, post-render SendPlan transforms).

## North star

Ticket-like codes in the message text (e.g. `HYP-576`) are verified against Linear via
the `linear` CLI (schpet/linear-cli) and turned into links. The ticket title is surfaced:
on the first line for a single ticket, as a collapsed reference block for several.
ON by default, toggled like any feature: `--no-feature autolink-tasks` or
`features: { autolink-tasks: false }` in `~/.config/tg-cli/config.yaml`.

## Detection

- A ticket code is exactly **3 uppercase Latin letters, a dash, then digits**, written
  as one token: `[A-Z]{3}-[0-9]+`.
- Boundaries: the char before/after the match must not be alphanumeric — `XHYP-576`,
  `HYP-576a` are NOT codes; `(HYP-576)`, `HYP-576,`, `HYP-576.` are.
- Tokens containing `://` are skipped entirely: a pasted URL
  (`https://linear.app/glide-vc/issue/HYP-576/slug`) must never be linkified inside.
- A code embedded in a file-path-looking token is a FILE mention, not a ticket:
  `HYP-123.ts:10`, `src/HYP-124/x.ts` are skipped (this also keeps line-spec quote
  anchors contiguous — codex review finding). A trailing sentence period
  (`fixed HYP-576.`) still counts.
- Codes are deduplicated preserving first-appearance order.
- Detection runs on the decoded caption (after `\n` escapes), BEFORE extraction/render.
  The tmux `[window]` prefix is NOT scanned (metadata, not message text).

## Verification (linear CLI)

One live Linear probe per send, only when detection found codes. The probe runs
`linear api '<GraphQL>'` once, with one immediate retry using the same query if the
first attempt fails unexpectedly:

```
query { issues(filter: { or: [
  { and: [ { team: { key: { eq: "HYP" } } }, { number: { in: [576, 99999] } } ] },
  ...one and-clause per team key...
] }, first: 250) { nodes { identifier title url } } }
```

- Codes are grouped by team key; numbers with >9 digits are dropped (parseInt precision;
  no real Linear issue number is that long).
- Issues missing in Linear simply don't come back → those codes are **ignored** (stay
  plain text). This also survives the aliased-`issue(id:)` pitfall: a single missing id
  there nukes the whole response (`data: null`), the filter query does not.
- Spawn timeout 10s per attempt. A transient unexpected failure is retried once; if
  the retry also fails, the message is sent UNCHANGED with a one-line stderr warning.
  Autolink must never block or fail a send.

### CLI missing / not authenticated → hint ONCE

- Spawn fails (binary not found) → stderr hint to install:
  `brew install schpet/tap/linear` (NOT bare `brew install linear` — that's the desktop
  app cask). Shown ONCE ever, recorded in `~/.config/tg-cli/autolink-tasks.json`
  (`{"install": true}`).
- Exit ≠ 0 with `No API key configured` / `No workspaces configured` in output → stderr
  hint to run `linear auth login`. Also shown once (`{"login": true}`).
- The hint state file is best-effort: unreadable/corrupt → treated as empty; write
  failures are swallowed.
- In both cases the message is sent unchanged.

## Rendering (post-render transform, like line-spec quotes)

When ≥1 detected code resolves to a real ticket, the render is forced to HTML (same
mechanism as `hasQuotes`: plain prose gets `&<>`-escaped). The transform then runs on
`plan.textMessages[0]` — which also covers the media-caption ride, since the transmitter
promotes that same message to a caption. It runs BEFORE line-spec quote insertion so
injected `<pre>` quote bodies are never linkified.

1. **Linkify**: every occurrence of a verified code in text segments becomes
   `<a href="URL">CODE</a>`. Skipped inside existing tags, inside `<a>…</a>` text
   (no nested links), inside `<pre>`/`<code>` (Telegram forbids nested entities),
   and inside `://` tokens.
2. **Single ticket** → its title goes on the FIRST line:
   - with an `<emoji> [window]` prefix line: appended to that line — `✳️ [win] Title`;
   - without a prefix: the title becomes its own first line above the text.
   Title is HTML-escaped.
3. **Several tickets** → a collapsed reference block appended at the end:
   `<blockquote expandable>` with one `<a href>CODE</a>: title` line per ticket, in
   first-appearance order.

Edge cases:
- All prose stripped by R2/R4 → no text message → transform skipped silently.
- A code whose only mention sat inside an extracted (R4) block: linkify finds no
  occurrence (no-op), but the title line / reference block still applies — the ticket
  WAS mentioned in the original text.
- Window name containing a verified code gets linkified too (harmless, deliberate).

## Non-goals

- No write operations against Linear, ever.
- No scanning of attachments' contents.
- Lowercase codes (`hyp-576`) are not tickets.
- No second hint after the first (per kind), even across months.

## v1.2 amendment: response cache

Linear API calls are cached to avoid spawning a `linear api` subprocess on every `tg`
invocation when the same codes appear repeatedly (common in agent workflows that send
multiple progress updates per task).

### Cache location and TTL

- File: `~/.cache/tg-cli/linear-cache.json`
- TTL: **1 hour** per entry, measured from the time of the last verified lookup.
- The cache directory is created on first write if absent.

### What is cached

Both **positive** (ticket found, title + URL stored) and **negative** (ticket confirmed
absent from Linear) verdicts are cached. Negative caching is intentional: an unknown code
is not retried on every send — it stays absent for 1 hour. This prevents repeated API
calls for typos or codes from other systems.

Cache shape: `{ entries: { "<CODE>": { t: <unix ms>, info: TicketInfo | null } } }` —
`info: null` is a cached verified-absent verdict. Expired entries are pruned on write.

### Behavior

1. On each send, all detected codes are looked up in the cache.
2. Only codes with no valid (non-expired) cache entry trigger a `linear api` spawn.
3. If all codes hit the cache, no subprocess is spawned at all.
4. Fresh results from the API are merged back and written with a plain best-effort
   `writeFileSync` (no atomic rename — a torn write just parses as corrupt and the
   cache self-heals on the next probe).

### Degradation policy

Cache read/write failures (permission error, corrupt JSON, disk full) are caught and
swallowed silently. The feature degrades to the pre-cache behavior: spawn `linear api` for
all codes on that invocation. A failed write does not retry and does not block the send.
Cache errors are never surfaced to the user.
