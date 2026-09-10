# Review addendum: tg-cli/docs/specs/unicode-prefix-styling.md

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

# tg-cli — Unicode prefix styling (item 5)

Repo: `~/.files/repos/tg-cli`. Code in `features/prefix-style/style.ts`, tests in
`tests/prefix-style.test.ts`. Wired into the `tg` outbound prefix (`buildPrefix`,
`renderText`) and into `applyAutolink` (`features/autolink-tasks/render.ts`).

## North star

The message prefix `<emoji> [window] <task title>` gets typographic styling:

- the **tmux window name** inside `[]` → **Mathematical Sans-Serif Bold**
  (`[𝗮𝗽𝗶-𝗯𝗼𝘁]`) — "math but no serifs" (decision 2026-06-12);
- the **single-ticket task title** appended after `]` (the autolink-tasks
  first-line title) → **Mathematical Bold Italic** (`𝑭𝒊𝒙 𝒂𝒖𝒕𝒐𝒍𝒊𝒏𝒌`), a
  serif italic (decision 2026-06-14: was Bold Script `𝓕𝓲𝔁`, switched because the
  calligraphic script read as too gaudy; Bold Italic is the restrained weight).

The brackets themselves stay unstyled; the emoji is untouched (branded custom
emoji as before). `buildPrefix` joins the prefix to the message body with a
**space, not a newline**, so the body's first line — the single-ticket task
title, or the message prose when there is no ticket — sits on the SAME line right
after `[window]` (decision 2026-06-14: was dropping to line 2). For a single
ticket the title is inserted immediately after `[window] ` via
`applyAutolink`'s `prefixBoundary`, ahead of any same-line prose.

## Why a fallback is mandatory

The Mathematical Alphanumeric Symbols blocks only cover ASCII **A–Z / a–z** (and,
for sans-serif, digits). They contain **no Cyrillic**, no accented Latin, no
Greek. A window/title that carries such a letter cannot be styled with Unicode at
all, so it falls back to a real HTML tag:

- window name with a non-Latin letter → `<b>name</b>` (escaped);
- task title with a non-Latin letter → `<i>title</i>` (escaped).

Digits and punctuation never trigger the fallback — they are left verbatim inside
an otherwise-styled token (Bold Italic has no digit glyphs, so `Fix v2` keeps its
plain `2`). The fallback trigger is precisely "contains a Unicode letter that is
not ASCII `A–Z/a–z`".

## Module API (`features/prefix-style/style.ts`)

- `toSansBold(s): string | null` — map ASCII letters+digits to Sans-Serif Bold;
  `null` when `s` has a foreign letter.
- `toBoldItalic(s): string | null` — map ASCII letters to Bold Italic (no
  digits); `null` on a foreign letter.
- `styleWindowName(name): { html, plain, tag }` — `html` is escaped (and may be
  `<b>name</b>`), `plain` is the unicode-only form, `tag` is true when `html`
  carries a real tag (forces HTML mode).
- `styleTaskTitle(raw): string` — final HTML for the title (unicode bold-italic
  escaped, or `<i>…</i>`). Used as the `styleTitle` hook of `applyAutolink`.

## Render wiring (`tg`)

`buildPrefix()` now returns `{ html, plain, present, forceHtml }`:

- `html` — the finished prefix HTML: branded emoji → `<tg-emoji>`, window →
  styled (escaped). `plain` — the unicode-only form for the non-HTML path.
- `forceHtml` — true when a custom-emoji tag or the `<b>` window fallback is
  present, so the whole message must be sent as HTML.
- `present` — replaces the old `prefix.text.length > 0` signal.

`renderText` decides `userHtml` from the **caption alone** (the emoji tag and
styled `[window]` are ours, not user-authored HTML), renders+escapes the caption
independently, then prepends the trusted `prefix.html` (or `prefix.plain`). This
is the key change that lets a `<b>` window fallback coexist with an escaped
plain-text caption — the previous single binary escape flag could not.

`applyAutolink` takes an optional `styleTitle` (default `escapeHtml`, so existing
callers are unchanged); `tg` passes `styleTaskTitle`. The script title only
applies in the single-ticket "title on the first line" branch — the multi-ticket
block keeps plain escaped titles.

## Non-goals

- No styling of the message body or the emoji.
- No Cyrillic "pseudo-bold" via combining marks — `<b>`/`<i>` is the honest
  fallback.
- Multi-ticket reference-block titles are not script-styled (only the single
  first-line title is the "task name after []").

## Caveats

- Math-styled text is poor for screen readers and copy-paste — an explicit
  aesthetic trade chosen by the user.
- Telegram renders these code points fine; the entity offsets in `tg` are UTF-16
  based, and the styled window carries no custom-emoji entity, so surrogate-pair
  widths are irrelevant to offset math.
