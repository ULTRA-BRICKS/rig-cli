# Review addendum: tg-cli/docs/specs/whatsapp-transport.md

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

# tg-cli — WhatsApp companion transport (item 6, implementation spec)

Repo: `~/.files/repos/tg-cli`. Promotes the research spike
(`docs/research/2026-06-11-whatsapp-transport.md`) into an implementation plan.
**Status: spec only — not built.** It cannot be implemented or tested without the
user's Meta Business assets (phone number, Cloud API token, an always-on webhook
host), so this documents the design and the explicit trade-offs to decide before
writing code.

## Decision recap (from research)

- **Official Cloud API via Graph API directly** (no BSP, no Baileys — Baileys'
  interactive buttons are server-side-deprecated as of mid-2026, a dead end for
  Q→buttons).
- **Add only on explicit opt-in.** WhatsApp is NOT a drop-in for Telegram: no
  custom inline emoji, a 24-hour session window that gates dynamic buttons, and a
  webhook-only inbound path. It is a *companion* surface, never the default.

## North star

A `--transport whatsapp` (and `control.transport: whatsapp`) that reuses the
`tg` outbound voice and the `tg-ctl` inbound loop, swapping only the wire format.
Telegram remains the default and the reference implementation.

## Architecture fit

Introduce a small **Transport interface** both surfaces target, so the bulk of
`tg`/`tg-ctl` (SendPlan, auto-attach, autolink, inject, discovery) is unchanged:

```
interface ChatTransport {
  sendText(text, opts): Promise<{ messageId }>
  sendMedia(kind, items, caption): Promise<...>
  sendButtons(prompt, buttons): Promise<{ messageId }>   // q→buttons / /agent
  receive(): AsyncIterable<InboundEvent>                 // poll (TG) | webhook (WA)
  ack/edit/react(...)                                     // best-effort, no-op on WA where absent
}
```

- Telegram transport = today's `botPost`/`getUpdates` code behind the interface.
- WhatsApp transport = Graph API calls + a webhook receiver (below).

The render layer must branch on transport capability, NOT assume Telegram HTML.

## Outbound (Graph API)

- `POST https://graph.facebook.com/v21.0/{phone-id}/messages` with a Bearer
  token. Text, image (≤5 MB), document (≤100 MB) with captions.
- **Formatting shim**: WhatsApp uses `*bold*`, `_italic_`, `~strike~`,
  ` ```mono``` `, `> quote` — NOT HTML and NOT Telegram entities. A
  `renderForWhatsApp(plan)` converts the SendPlan's HTML/markup to WA markup and
  **drops**: custom emoji (→ fixed per-agent Unicode emoji + `*AgentName*`),
  inline hyperlink text (→ plain URLs; autolink emits bare URLs instead of
  `<a>`), `<tg-emoji>`, `<blockquote expandable>` (→ a `>`-quoted block, not
  collapsible). The item-5 Unicode prefix styling carries over verbatim (it is
  plain code points), EXCEPT the `<b>`/`<i>` Cyrillic fallback → `*…*`/`_…_`.

## Inbound (webhook, not polling)

- WhatsApp has **no long-poll**. A persistent HTTPS endpoint echoes
  `hub.challenge` on GET verify and 200s POSTs within ~5 s.
- **Cloudflare Tunnel** (free named tunnel) is the recommended always-on path; a
  `cloudflared` process under launchd (macOS) / systemd (Linux). `tg-ctl` gains a
  `whatsapp-webhook` mode (a tiny `Bun.serve`) that the tunnel fronts; it shares
  the singleton lock so it can't double-run.
- The singleton/registration/inject machinery is unchanged — only the *source* of
  inbound events differs (webhook push vs getUpdates pull).

## Q→buttons & /agent buttons under the 24h window

- **Inside the 24h user-initiated window**: free-form interactive reply buttons,
  fully dynamic labels, **max 3 buttons × 20 chars**. Covers the common case.
  `/agent` selection with >3 candidates → fall back to a numbered text list
  ("reply 1/2/3"), since WA caps at 3 buttons.
- **Outside the window**: one pre-approved generic Utility template `"{{1}}"`
  with static `Option 1/2/3` buttons; the dynamic question + option meanings go
  in the body variable. Or drop buttons and parse a numbered reply. A daily
  `/ping` keeps the window open so dynamic buttons work most of the time.

## Config

- `~/.config/tg-cli/.env`: `WA_PHONE_ID`, `WA_TOKEN`, `WA_RECIPIENT`,
  `WA_VERIFY_TOKEN` (webhook), `WA_WEBHOOK_PORT`.
- `control.transport: whatsapp` selects it; `auto` stays Telegram-first.

## Cost & limits

~$0.004/Utility msg (US base), ~$12/mo at heavy use; service messages inside an
open window are free. 80 msg/s default rate (irrelevant at personal scale).
Unverified business = 250 business-initiated conversations/24 h (never hit).

## Build order (when greenlit)

1. Extract the `ChatTransport` interface; move the Telegram code behind it (pure
   refactor, fully test-covered first).
2. `renderForWhatsApp` SendPlan→WA-markup converter (pure, unit-tested).
3. Graph API outbound transport (text/media) behind a fake for tests.
4. `cloudflared` webhook receiver + verify handshake.
5. In-window reply buttons; out-of-window generic template + numbered fallback.
6. One real send/receive smoke against a live WA number (manual, like Telegram).

## Explicit trade-offs to accept

No branded inline emoji; out-of-window buttons need static-label templates +
numbered indirection; an always-on webhook tunnel; per-message cost; one-time
Meta Business setup. If those are unacceptable, **do not add WhatsApp** — Telegram
already covers the outbound-reporting moat with zero infra.
