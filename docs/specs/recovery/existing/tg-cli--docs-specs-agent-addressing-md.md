# Review addendum: tg-cli/docs/specs/agent-addressing.md

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
- [tg-cli#59](https://github.com/alex-mextner/tg-cli/issues/59) — /agent shows useless duplicate text rows + no buttons — make it an inline-keyboard picker with distinct labels
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

# tg-cli — /agent addressing (item 1)

Repo: `~/.files/repos/tg-cli`. Pure logic in `features/tg-ctl/agent-match.ts`,
tests in `tests/agent-match.test.ts` + `tests/ctl-agent-route.test.ts`. Daemon
wiring in `tg-ctl` (`listAgentCandidates`, `handleAgentRoute`,
`handleAgentCallback`); command + callback routing in
`features/tg-ctl/updates.ts`.

## North star

A new inbound command lets you address a **specific** agent from Telegram when
several agents run side by side:

```
/agent [<window>] <message>
```

- `<window>` is fuzzy-matched (phonetic) against tmux window names.
- A confident unique match routes `<message>` straight to that agent's pane.
- Ambiguous / no selector → inline **selection buttons**, grouped by
  tmux session, each carrying the pending message; the tap injects it.
- **No match** (selector given but nothing matched) → error reply naming the
  selector and the running agents; the message is **not** delivered.
- Bare `/agent` → an inline-keyboard **picker** (tappable buttons), one
  distinct-labelled button per addressable agent. A tap arms a one-shot route:
  the next ordinary inbound message goes to that selected agent. The confirmation
  card keeps a `Cancel` button to clear the pending route before anything is sent.

This complements the existing default routing (the per-pane registration set —
every live session that ran `tg` is registered; tg-cli#67): `/agent` is the
explicit override when the auto-picked target is not who you mean.

## Command grammar

`parseAgentCommand(text)` → `{ selector, rest, all }`:

- `selector` = the first whitespace token after `/agent` (a *possible* window),
  or `null` for bare `/agent`.
- `rest` = the message when `selector` IS a window (everything after it).
- `all` = the message when there is NO selector (everything after `/agent`).

The daemon decides which interpretation holds *after* matching, because only it
knows the live window names. Tolerates `/agent@botname` (group chats).

The same command grammar applies when `/agent ...` is the **caption** of an
inbound photo/document. Telegram delivers that as media, not as text, so
`stepUpdates` keeps the parsed agent route on the `download-media` action. After
the daemon downloads the file, it injects the pre-wrapped
`sent photo: <local-path>` / `sent file: <local-path>` receipt plus the caption
body into the selected agent. The `/agent <selector>` command line itself is not
injected. Explicit caption routing wins over default last-active auto-bind and
over reply-route metadata.

## Phonetic normalization

`phoneticKey(s)`:

1. lowercase + **Cyrillic→Latin transliteration** (so `апи`, `кодекс` match
   `api`, `codex`);
2. strip separators/spaces;
3. sound-equivalences: `ph→f`, `ck→k`, `qu→kw`, `x→ks`, hard `c→k`, `w→v`,
   `y→i`;
4. collapse doubled letters.

`matchWindows(selector, candidates)` scores each candidate against the selector
key — `0` exact · `1` prefix · `2` substring · `3+` within an edit-distance band
(`⌈maxlen/4⌉`) — and returns the tied best matches. `confident` = exactly one
best match → route without asking. The Cyrillic table makes a Russian-typed
selector usable; this is deliberately a *light* fold, not full Metaphone.

## Selection buttons

When a choice is needed (a bare `/agent`, OR an ambiguous route),
`buildAgentSelectMessage(chatId, candidates, token, preview)` builds a message
that is a SHORT prompt plus one inline button per candidate, each labelled with
`<label>`. The message text is the prompt ONLY — there is **no** per-agent text
list and **no** `▸ <session>` group header (both were redundant noise that just
duplicated the buttons; #63). The buttons carry the distinct labels; they stay
grouped by session for a stable row order. Callback data is `tga:<token>:<index>`
(index into the candidate list). `token` keys the daemon's `pendingAgent` store
(message + candidate subset + button message id + `selectOnly`), TTL 10 min. With
no `preview` (bare `/agent`) the prompt is `Pick an agent:`; with one it is
`Route to which agent?` followed by the quoted message preview.

### Distinct labels (`distinctLabels`)

`<label>` is NOT a bare `<window> · <agent>` — several `claude` panes in the same
numeric window (e.g. window "4", no window names set → all fall back to the
session name "4") would otherwise render as three identical `4 · claude` buttons,
impossible to tell apart (the reported bug). `distinctLabels(candidates)`:

- base = `<window> · <agent>`; when the window name is **bare** (empty or purely
  numeric — the session-name fallback) it leads with the **cwd project dir**
  (`pane_current_path` basename) instead → `<project> · <agent>` (e.g. `rig`,
  `3d-cli`, `hyperide`);
- any label still shared by >1 candidate gets the project appended, then — as the
  last-resort, always-unique distinguisher — the pane id (`%N`).

`AgentCandidate` therefore carries `panePath` (the pane's cwd). `dedupeCandidates`
drops any pane id listed twice (a merged/flaky snapshot guard; pane ids are
unique per server, so it never collapses two real agents).

### Tap

A tap (`tga:…`) is parsed by `parseAgentCallback` and emitted as an
`agent-callback` action; the daemon looks up the token. For a **route** picker it
injects the pending message into `candidates[index]`'s pane (re-verifying the
pane still hosts an agent), answers the callback, and edits the prompt to
`→ <label>`. For a **bare-`/agent`** (`selectOnly`) picker there is no message to
inject yet: it answers `selected`, arms a one-shot route for the next ordinary
inbound message, and edits the prompt to `selected <label> — send the next
message here` with a `Cancel` button. A successful/queued/failed delivery edits
that same prompt to its final state and removes the button.

`tga:` callbacks are routed BEFORE `tgq:` (q→buttons) in `stepUpdates`, so the
two button systems never collide.

## Discovery (daemon)

`listAgentCandidates()` runs the same `snapshot()` (panes + process tree) the
target picker uses and finds every pane hosting an agent (`findAgentInPane`). The
window name comes from the core `PANE_FORMAT` itself — `#{window_name}` is a fixed
field carried through `parsePaneList` alongside the pane id (tg-cli#75). It used to
be a **separate** `tmux list-panes -F '#{pane_id}\t#{window_name}'` call, but that
call skipped the UTF-8 locale env, so under launchd the tab-mangle blanked every
name and the picker fell back to the cwd basename; folding it into the locale-safe,
retried core snapshot fixed that. A pane with no name falls back to its session
name; a bare default (a number, a shell/launcher command, or a version string) is
treated as non-distinguishing and the label leans on the cwd project dir instead.

## Edge cases

- **No agents** → `NO_AGENT_REPLY`.
- **Single agent + a message** → injected directly, no buttons (asking would be
  silly).
- **Confident match but empty message** (`/agent feat-bot`) → reply naming the
  match and asking for the message; nothing injected.
- **Selector matches nothing** → error reply ("No agent matching X found. Running:
  …") is sent back to the user; the message is **not** delivered to any agent
  and the request ends there (no picker fallthrough).
- Injection always goes through `buildTextInjectPlan` (verify-pane → literal /
  bracketed-paste → paced Enter), so a pane that lost its agent is refused, not
  executed as a shell command.

## Non-goals / future

- The bare `/agent` selection is intentionally one-shot: it is remembered only
  until the next ordinary inbound message, `Cancel`, TTL expiry, or another
  explicit `/agent` command. (Earlier this was "lists only, no buttons"; the
  picker replaced the text list because three indistinguishable `4 · claude`
  rows were useless and there were no buttons to tap — CTO bug report.)
- No persistence of `pendingAgent` across a daemon restart (10-min in-memory TTL).
- When a bare-`/agent` button is tapped, the daemon re-runs `listAgentCandidates()`
  and verifies the handed-back selector against the FULL LIVE fleet at tap time
  (not the stale picker-time subset) — so the suggestion never points at a pane
  outside the original picker. The user then sends `/agent <selector> <message>`,
  re-resolved against the live fleet again; if the fleet changed between tap and
  send (an agent started/exited, a cwd changed) the selector may become ambiguous
  (→ the picker re-opens — safe) or, in the worst case, route to a different pane.
  That residual window is bounded by the 10-min TTL and is the same
  last-write-wins staleness the whole `/agent` flow already tolerates.
- The cwd is an EXACT-only tie-breaker, and only when no window name matched — a
  fuzzy or even prefix window-name token never silently routes via someone else's
  cwd (a short `/agent dev` won't land in a `…/development` project).
- After that exact cwd miss, a **unique project token** (hyphen/underscore/dot
  piece of length >= 4, or the full basename) may route: `/agent landing` hits
  the sole `conloca-landing` omp. A hyphenated selector with no token hit may
  then use a **unique first segment** (`/agent rig-fable` -> sole `rig-cli`).
  Ambiguous token/segment sets do not guess. Picker labels for omp append the
  project dir so those names stay visible next to the session title.
- Live end-to-end requires an inbound tap from the chat owner (the unit + step
  tests cover the logic; the button round-trip is the same mechanism as
  q→buttons, which has integration coverage).
