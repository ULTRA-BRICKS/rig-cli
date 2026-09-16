# Review addendum: tg-cli/docs/specs/autolink-compound.md

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

# tg-cli — compound autolink refs: ranges & lists (item 7)

Repo: `~/.files/repos/tg-cli`. Shared logic in `features/autolink-refs/compound.ts`,
tests in `tests/autolink-compound.test.ts`. Extends both `autolink-tasks`
(`XXX-ddd` Linear codes) and `autolink-prs` (`#ddd` GitHub refs).

## North star

A single token may pack several references that share a lead:

```
HYP-100..103/110      #5-7,9
```

- **list separators** `/` and `,` → each written number is its own reference;
- **range separators** `..`, `…`, `-` → an inclusive range.

Rendering (decision 2026-06-12 — "в теле ссылки на границы, внизу — все"):

- **in the message body**: leave the text as-is, only *add* links. Every number
  that is **written** (the lead and each number after a separator — i.e. both
  range endpoints) becomes a link; range INTERIOR numbers are not in the text so
  are never linked there.
- **in the bottom reference block**: enumerate **every** number, including the
  full range expansion, each as its own link/line.

So `HYP-100..103` → body links `HYP-100` and `103`; the expandable block lists
`HYP-100, HYP-101, HYP-102, HYP-103` (each verified one).

## Grammar

```
compound := LEAD ( SEP NUMBER )*
LEAD     := [A-Z]{3}-[0-9]+     (ticket)   |   #[1-9][0-9]*   (ref)
SEP      := '..' | '…' | '-'    (range)    |   '/' | ','      (list)
```

The tail numbers are **bare digit runs** — `HYP-1/HYP-2` does NOT merge (the
second lead has letters); it stays two independent single-number groups. A single
`.` is never a separator (a sentence period after a code is not a range), only
`..`/`…` are.

## Path disambiguation (tickets only)

`autolink-tasks` skips file-path-looking codes (`HYP-1.ts:10`, `src/HYP-1/x`) to
avoid corrupting line-spec quote anchors. The `/` list separator collides with
that rule, so compound ticket-lead detection (`ticketLeads`) relaxes it:

- `/` followed by a **digit** → list separator (`HYP-1/2/3` is a ticket list);
- `/` followed by a **letter** → path (`src/HYP-1/x.ts` stays a file mention);
- a leading `/`/`\`, or `.ext`/`.decimal`, still mark a file mention; `..` stays
  a valid range.

GitHub `#refs` never had a path rule, so all separators work there unchanged.
`findCodeMatches` / `findRefMatches` (legacy single-code linkers) are untouched.

## Expansion

`expandGroup` walks the written numbers: list seps append the next number, range
seps fill the inclusive interior. Dedup is within a group; cross-token dedup +
first-appearance order is the detector's job.

**Cap:** `RANGE_STEP_CAP = 100` per range step. A fat-fingered `HYP-1..99999`
drops the interior and keeps only the two endpoints (still linked + listed) — no
100k-line block. A descending range (`HYP-7-3`) degrades to its two endpoints.

## Module API (`features/autolink-refs/compound.ts`)

- `groupsFromLeads(token, leads): CompoundGroup[]` — grow each lead's tail.
- `expandGroup(group): number[]` — all numbers (capped), in order, deduped.
- `linkifyCompound(html, findLeads, urlFor): string` — tag-safe body linkify
  (own walker; never touches `<a>`/`<pre>`/`<code>` or `://` tokens); wraps each
  resolving written span in `<a>`.

Feature wrappers:

- `detectTicketCodesExpanded` / `detectRefsExpanded` — superset of the legacy
  detectors; feed the probe + the bottom block.
- `linkifyTicketsCompound` / `linkifyRefsCompound` — body linkers; for a
  separator-free token they are byte-identical to `linkifyCodes` / `linkifyRefs`.

`applyAutolink` gains an optional `linkifyTickets` override (default = legacy
`linkifyCodes`), so existing callers are unchanged; `tg` passes the compound
linker. The bottom block already enumerates whatever the (now expanded) ticket /
ref list contains.

## Non-goals

- No new write ops; verification is still the existing linear/gh probes (interior
  numbers are probed too, so an absent one stays plain — never invented).
- Lists/ranges across whitespace (`HYP-1 / HYP-2`) are not compound — they are
  separate single tokens by the existing rules.
- No styling — purely link generation (composes with the prefix styling, item 5).
