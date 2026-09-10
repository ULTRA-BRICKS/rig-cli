# Review addendum: tg-cli/docs/specs/git-state-banner.md

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
- [tg-cli#173](https://github.com/alex-mextner/tg-cli/issues/173) — Rewrite dirty-pane routing warning
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

# tg-cli — git-state-check banner

Repo: `~/.files/repos/tg-cli`. Pure logic in `features/tg-ctl/git-state.ts`, tests in
`tests/ctl-git-state.test.ts` (pure), `tests/ctl-git-state-banner-integration.test.ts` (real
daemon + real git fixtures, tmux/ps shimmed). Config toggle in `features/tg-ctl/config.ts` /
`features/tg-ctl/types.ts`, tests in `tests/ctl-config.test.ts`. Entrypoint wiring
(`gitStateForPath`, `withGitStateBanner`) in `tg-ctl`, adjacent to `injectViaTarget`.

## Incident this fixes

A subagent working an unrelated task on HYP-915 appeared to "silently drop it" for six hours.
Root cause: `tg-ctl` routes an inbound message to a tmux **pane**, not to "the agent's current
task". A fresh message about a different topic landed in the same pane that was mid-flight on
HYP-915; the pane — human or AI, Claude or Codex — has no signal that this is a NEW thread rather
than a continuation, and just reads it as the next line of the same conversation.

Several fixes were considered (per-task session isolation, a stricter routing model, …). This
implements the harness-agnostic one ("Вариант В"): a warning banner prepended to the delivered
message when the destination pane looks mid-flight, so whoever/whatever receives it gets an
explicit signal instead of silence.

## What the check does — and what it does NOT do

`isPaneOccupiedWithWork` (git-state.ts) treats a pane's cwd as "occupied with feature work" when
**either**:
- `git status --porcelain` is non-empty (uncommitted changes), **or**
- the current branch is not `main`/`master` (a clean feature-branch checkout is still someone's
  deliberate work in progress, even with nothing staged right now).

This detects **"this pane's cwd is mid-flight"**, not **"this message is off-topic"** — those are
not the same thing. An agent that always works on a feature branch will see the banner on
**every** delivery to that pane; this is the check's expected shape, not a bug. It is a heads-up
nudge, not a hard gate — it never blocks or drops the message, only prepends operational prose
for the receiving pane. The banner text is not a machine-readable routing API.

## Banner text

```
⚠ This pane currently has uncommitted work on branch feat/foo (3 files changed). Routing check: if this message is about another project, clarify with the user before taking it here. If it clarifies an active task, route it to that active subagent. If it is a new task, start a new subagent.
```

A clean-but-non-main-branch pane gets a variant that doesn't falsely claim "uncommitted work":

```
⚠ This pane is currently on branch feat/foo (not main/master, tree clean). Routing check: if this message is about another project, clarify with the user before taking it here. If it clarifies an active task, route it to that active subagent. If it is a new task, start a new subagent.
```

`buildGitStateBanner(state)` returns `null` (no banner at all) when `state` is `null`
(undeterminable: not a git repo, `git` missing, spawn error/timeout) or the pane is clean on
`main`/`master`.

## Where it's wired — and where it's deliberately NOT

`gitStateForPath(cwd)` (in `tg-ctl`) runs `git -C <cwd> rev-parse --abbrev-ref HEAD` then
`git -C <cwd> status --porcelain`, each spawn killed after 2s (`GIT_STATE_TIMEOUT_MS`) so a stuck
`git` — lock contention, a huge repo, a hung network filesystem — can never block the daemon's
single-threaded poll loop. Any failure degrades to `null` (no banner), the same "never breaks
delivery" contract every other best-effort read in the entrypoint follows.

`withGitStateBanner(panePath, text)` composes the banner and is called **only** at the three
**silent auto-bind** deliveries inside `injectViaTarget` — the un-anchored path where a fresh
message lands on a discovered pane with nothing else choosing it for it:
- a plain non-reply text message (`inject-text` — the exact HYP-915 shape),
- a non-reply photo/document notice (`download-media`),
- a standalone (non-reply) voice transcript (`transcribe-voice`).

**Command passthrough guard (review catch on PR #153):** the flat `inject-text` action also
carries a command **verbatim, unwrapped** — not the normal wrapped-prose case — so the harness TUI
can execute it. Two shapes qualify: an unrecognized `/command` (e.g. `/compact`, `/clear`), and a
`!shell` command (a message starting with `!` — the harness `!` convention runs the rest as a
shell command in-session). Prepending the banner ahead of either would push the leading `/` or `!`
off the first character, so the harness would read the whole thing as plain prompt text instead of
a command. `withGitStateBanner` therefore skips the banner whenever `text` starts with `/` **or**
`!` — the wrapped-prose case never does (the `injectWrap` template always renders first), so this
only ever catches a real passthrough. Covered by
`tests/ctl-git-state-banner-integration.test.ts`'s dirty-pane slash and `!shell` passthrough tests.

Deliberately **excluded**, with the reasoning at each call site:
- **reply-route** — anchored to a specific prior message; on-topic by construction.
- **topic-route** (forum-topics mode) — a per-topic pane, a different targeting model entirely;
  the topic itself is the disambiguator.
- **`/new` spawns** — a brand-new pane has no prior work to protect.
- **picker taps** (`/agent`, the ambiguous-route picker) — a human already chose that exact pane.
- **a harness control-command passthrough** (e.g. a `/compact` typed mid-`/new`-flow) — a control
  verb, not new content.
- **messages deferred behind an open question** — flushed through `driveFlush`, a separate code
  path; out of scope for this pass.

`injectViaTarget`'s `buildPlan` callback was changed to receive the resolved `TargetPane` (not a
bare `paneId` string): the banner needs `panePath` (the pane's cwd), and that only exists on the
`TargetPane` the function already resolved via `discover()`/`discoverForInject()` — re-deriving it
from a bare paneId (a second `snapshot()`) would race against, and could disagree with, the pane
the message actually lands in.

## Config

`control.git_state_banner` (default **ON**), same `control:` YAML block as `topics` /
`private_topics`. Set `false` to opt out per machine if the noise (see above) isn't worth it.

## Explicitly out of scope for this pass

- The defer-while-waiting flush path (a message queued behind an open question, delivered later
  by `driveFlush`) does not get the banner.
- Forum-topics mode's per-topic bound delivery does not get the banner (different targeting
  model; a topic is itself a disambiguator that the flat 1:1 mode lacks).
- This is a nudge, not a fix for the routing model itself — it does not change WHICH pane a
  message lands on, only what the pane is told when it does.
