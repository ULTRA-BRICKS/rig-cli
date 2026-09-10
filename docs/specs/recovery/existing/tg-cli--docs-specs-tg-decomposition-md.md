# Review addendum: tg-cli/docs/specs/tg-decomposition.md

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

# tg-cli — `tg` decomposition (Stage 4)

Repo: `~/.files/repos/tg-cli`. Decompose the 1728-line `tg` entrypoint into
`features/<feature>/` modules with a thin `tg`, **zero behaviour change**, mirroring
the review-cli `reviewlib` decomposition (Stage 0) and the
`docs/specs/2026-06-...` repo norm of splitting large files. Companion to the
visual-verification effort's architecture spec §8 ("decompose `bin/review` and
`tg` into a `features/` layout with a thin entrypoint").

## Why

`tg` is 1728 lines: arg parsing, emoji/branding maps, model detection, the prefix
builder, the render pipeline, autolink orchestration, the Telegram transport, and
the `main()` flow all inline. The repo already keeps pure logic in
`features/<feature>/` (auto-attach, autolink-*, tg-ctl, prefix-style,
autolink-refs); the entrypoint just hasn't followed. The decomposition pulls the
inline logic into modules so `tg` becomes thin wiring + the `import.meta.main`
guard — easier to read, test, and reconcile with parallel work (the
`feat-tg-photo-visual-hook` Stage 3 photo hook re-applies its small pre-send call
onto the decomposed `main`).

## Invariants

- **Zero behaviour change.** Every one of the 579 `bun test` tests stays green
  after each stage; a real send still produces identical bytes. No feature
  toggles, no reordering of the send pipeline.
- **Pure modules, injected I/O** (the repo's `features/` contract): spawns,
  fetch, file reads are function parameters so tests pass fakes. The entrypoint
  keeps the real I/O.
- **`tg` stays a single `#!/usr/bin/env bun` entrypoint** (no build step) — it
  imports from `features/`, it does not become a package install target. The
  `~/.files/bin/tg` symlink rule is unchanged.

## Target layout

Extract into `features/`, by concern (lowest-coupling first):

| Module | From `tg` | Surface |
|---|---|---|
| `features/cli/args.ts` | arg parsing §231-560 | `parseArgs`, `isImagePath`, `hasRealExtension`, `expandHome`, `resolveExistingFile`, `Format`/`ParseResult` types |
| `features/cli/version.ts` | §77-133 | `versionOutput`, `gitShortHash`, `latestChangelogSection` |
| `features/branding/emoji.ts` | §565-663 | `EMBEDDABLE_EMOJI_MAP`, `UNICODE_EMOJI_MAP`, `MODEL_EMOJI_MAP`, `EmojiEntity`, `ParsedText`, `parseEmojiHelpers`, `convertEntitiesToHtml` |
| `features/branding/detect.ts` | §664-720, main §841 | `detectAgentViaAncestry`, `isProcessRunning`, `extractBaseModel`, `detectAiModel`, `detectAiEmoji` |
| `features/render/html.ts` | main render §1281-1456 | `escapeHtml`, `detectHtmlTags`, `parseModeFor`, `renderText` builder |
| `features/render/prefix.ts` | main §1042-1093 | `buildPrefix` (depends on branding + prefix-style) |
| `features/transport/telegram.ts` | main §1602-1700 | the Transport (sendMessage/sendPhoto/sendDocument/sendMediaGroup), `checkResponse`, `recordRoute` |

`tg` keeps: env load, the `main()` flow that wires the above + the autolink
orchestration (which is already thin over `features/autolink-*`) + the tg-ctl
auto-start.

## State threading

`main()`'s inline closures capture locals (`BOT_TOKEN`, `CHAT_ID`, `API`, `home`,
`MODEL`, `AI_EMOJI`, `args`, feature flags). Extracted module functions take these
as explicit parameters (or a small `Ctx` record) instead of closing over them —
the same move reviewlib made. Pure helpers (args, emoji maps, detect, version)
capture nothing and extract first (Stage 0); the stateful render/transport
closures extract after (Stage 1+).

## Status

- **Stage 0a — DONE** (`features/branding/emoji.ts`): the 3 emoji maps,
  `EmojiEntity`/`ParsedText`, `extractBaseModel`. `tg` 1728 → 1622.
- **Stage 0b — DONE** (`features/branding/detect.ts`): `isProcessRunning`,
  `detectAgentViaAncestry`. `tg` 1622 → 1589.
- **Stage 0c — DONE** (`features/cli/args.ts`): `parseArgs` + path helpers
  (`isImagePath`/`hasRealExtension`/`expandHome`/`resolveExistingFile`, now
  exported) + `Item`/`ParseResult`/`Format`/`ItemLineSpec`. 5 test files now
  import `parseArgs` from `../features/cli/args`. `tg` 1589 → 1255.
- **Stage 0d — DONE** (`features/cli/version.ts`): `VERSION` +
  `gitShortHash`/`latestChangelogSection`/`versionOutput`; the entrypoint
  re-exports `VERSION` for back-compat. `tg` 1255 → 1188.
- **Stage 1 — DONE** (`features/render/html.ts` + `features/render/prefix.ts`):
  pure `escapeHtml`/`detectHtmlTags`/`parseModeFor`/`convertEntitiesToHtml`/
  `parseEmojiHelpers` (the emoji transforms read the shared, main-mutated
  singleton maps by reference); `buildPrefix({ aiEmoji, model, tmuxWindow })`.
  `tg` 1188 → 1061.
- **Stage 2 — DONE** (`features/transport/telegram.ts`):
  `createTelegramTransport({ api, chatId, recordRoute })` factory returning the
  transmitter `Transport`; owns `checkResponse`/`blobFor`/`recordRouteFromResult`.
  `main()` keeps `recordRoute` (daemon ROUTES_PATH/TMUX_PANE bookkeeping) and
  injects it. `tg` 1061 → 926.
- **Stage 3 — IN PROGRESS**: focused unit tests for the extracted pure modules.

All 579 `bun test` pass after every stage (zero behaviour change). The
decomposition took `tg` from 1728 → 926 lines across 5 new feature modules
(`cli/args`, `cli/version`, `render/html`, `render/prefix`, `transport/telegram`).

## Staging (each stage: extract → `bun test` 579 green → commit)

- **Stage 0** — pure top-level helpers: `features/cli/args.ts`,
  `features/cli/version.ts`, `features/branding/emoji.ts` (done),
  `features/branding/detect.ts` (done). No `main` state captured → mechanical
  move + import. Biggest, safest win (~500 lines out of `tg`).
- **Stage 1 — done** — render: `features/render/html.ts` +
  `features/render/prefix.ts` (branding/flags state threaded as params).
- **Stage 2 — done** — transport: `features/transport/telegram.ts` (token/chat/API
  + the routes recorder threaded via a `createTelegramTransport` factory).
- **Stage 3 — in progress** — `tg` is now thin wiring; add focused unit tests for
  the newly extracted pure modules (args, version, html, prefix) that were
  previously only covered end-to-end.

## Non-goals

- No behaviour or output change; no new features; no dependency added.
- Not touching `tg-ctl` (already thin over `features/tg-ctl/`).
- The `feat-tg-photo-visual-hook` photo-hook (Stage 3 of the visual effort) is a
  separate branch; it re-applies its one `stat`-guarded pre-send call onto the
  decomposed `main` after this lands (small, vs. the heavy collision a parallel
  decomposition would have caused — which is why that agent deferred Stage 4).
