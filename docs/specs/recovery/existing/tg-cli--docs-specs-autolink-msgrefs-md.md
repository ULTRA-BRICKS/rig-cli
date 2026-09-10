# Review addendum: tg-cli/docs/specs/autolink-msgrefs.md

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

# tg-cli — autolink-msgrefs feature spec

Repo: `~/.files/repos/tg-cli`. Feature lives in `features/autolink-msgrefs/`, tests in
`tests/autolink-msgrefs.test.ts`. Sibling of `autolink-prs` (docs/specs/autolink-prs.md)
in shape, but for inbound **Telegram message** references — `tg#<id>` — not GitHub `#N`.

## North star

The inbound inject wrap renders an inbound message's id as `tg#<id>`
(`[TG from Alex tg#1234] …`, see `features/tg-ctl/inject.ts`). When an agent quotes that
id back in an outbound message ("answered tg#1234", "see tg#1234"), it means *the Telegram
message with id 1234* — NOT a GitHub issue/PR `#1234`.

The `tg#` prefix is the whole disambiguation. A bare `#1234` belongs to `autolink-prs`
(resolved against the cwd GitHub repo). Without the prefix the two namespaces collide:
`#3715` quoted from a phone would be probed against `gh` and mis-linked as PR/issue #3715.
The convention fixes that at the source — ids are *written* as `tg#<id>`, and this feature
links them.

ON by default, toggled like any feature: `--no-feature autolink-msgrefs` or
`features: { autolink-msgrefs: false }` in `~/.config/tg-cli/config.yaml`.

## Detection (`detect.ts`)

- A reference is `tg#` (case-insensitive on `tg`) immediately followed by a positive id:
  `tg#[1-9][0-9]*`. `tg#0` is not a thing — the leading digit must be `1-9`.
- The already-rendered bold-italic form `𝒕𝒈#<id>` / `𝑻𝑮#<id>` is also accepted. This
  matters in private DMs, where message refs render as styled-but-unlinked text and users
  often copy that visible token back verbatim.
- Boundaries: the char before `tg` and the char after the number must not be alphanumeric.
  `xtg#1`, `tg#1a` are NOT references; `(tg#10)`, `tg#10,`, `tg#10.` are.
- Whitespace tokens containing `://` are skipped (a pasted URL with `tg#` in its path is not
  a plain-text mention — same guard as autolink-prs).
- `detectMsgRefs(text)` → unique ids in first-appearance order.

## Linking (`render.ts`)

- `msgRefUrl(chatId, id)` builds a `t.me` deep link **only for a supergroup/channel** chat
  (`-100<rest>` → `https://t.me/c/<rest>/<id>`). A private bot DM (a positive chat id) or a
  basic group has NO public per-message URL → `null`.
- `linkifyMsgRefs(html, urlFor)` rewrites each `tg#<id>` in an already-rendered HTML body:
  - url present → `<a href="t.me/c/…">tg#<id></a>`;
  - url null → a marked-but-unlinked **bold-italic** styled reference (so it still reads as
    a deliberate reference, not stray text). The dominant case — the user's private DM — is
    the null case.
- Same tag-safe walk as the sibling features: never rewrites inside `<a>…</a>` (Telegram
  rejects nested links), `<pre>`/`<code>`, or a token containing `://`.
- When a mentioned message id exists in the local `tg replies` history, `buildMsgRefEntries`
  contributes a bottom `<blockquote expandable>` entry. The label is the same link/styled
  `tg#<id>` marker; the title is `from: head`, where `head` is a one-line collapsed excerpt
  truncated to a short prefix with `…`. The outbound message never quotes the whole referenced
  Telegram message.
- Excerpt entries are keyed from the same tag-safe walk as linkification, not from raw text:
  `tg#<id>` inside `<a>`, `<pre>`, `<code>`, or a URL token does not link and does not add an
  excerpt block.
- New history rows carry `chat_id`, and excerpt lookup filters by the current chat so the same
  bot used in two chats cannot show another chat's message text for a colliding `message_id`.
  Legacy rows without `chat_id` remain eligible for backward compatibility.
- Missing/corrupt history is best-effort: link/styled body refs still render, but the excerpt
  block entry is skipped.

## Ordering — runs BEFORE the #N PR pass

In the `tg` outbound transform, `linkifyMsgRefs` runs FIRST, before `applyAutolink`'s
ticket/`#N` linkifiers. The resulting `<a>`/styled span is then skipped by those passes
(they don't rewrite inside `<a>`), and a `tg#3715` is never offered to the gh probe. In
practice the PR detector's own boundary rule already rejects `tg#3715` (the `g` before `#`
is alphanumeric), so the two are naturally disjoint; running msgrefs first makes that
explicit and robust to any future loosening of the PR boundary rule.

## Tests (TDD, red-first)

- detect: bare `tg#<id>`; case-insensitive prefix; dedup + first-appearance order;
  already-styled `𝒕𝒈#<id>`; punctuation boundaries; leading/trailing alnum rejection;
  `tg#0` rejection; bare `#N` is NOT a msgref; URL tokens skipped; multiline; empty.
- `msgRefUrl`: supergroup → `t.me/c` link; DM/basic-group/empty → null.
- linkify: url → anchor; null url → styled bold-italic ref; tag-safety inside
  `<a>`/`<pre>`/`<code>`/URL tokens; no-ref body unchanged.
- excerpt entries: compact one-line history excerpt, ellipsis on truncation, missing ids
  skipped, duplicate ids deduped in mention order, code-point-safe emoji truncation, escaped
  excerpt text, and current-chat filtering when `chat_id` is available.
- entrypoint subprocess: real `tg` send against a fake Bot API reads
  `tg-ctl.<bot>.history.jsonl` and sends the excerpt block for both `tg#<id>` and
  `𝒕𝒈#<id>`; skipped tags do not create excerpt-only blocks.
- coexistence: `detectRefs` ignores `tg#<id>`; the PR linkify never rewrites a `tg#<id>`
  even when that number is a verified PR, while a standalone `#N` still links.
