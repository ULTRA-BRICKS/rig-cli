# Review addendum: tg-cli/docs/specs/autolink-prs.md

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

# tg-cli — autolink-prs feature spec

Repo: `~/.files/repos/tg-cli`. Feature lives in `features/autolink-prs/`, tests in
`tests/autolink-prs.test.ts`. Sibling of `autolink-tasks` (docs/specs/autolink-tasks.md):
the same detection → verify → cache → render pipeline, but for GitHub `#N` references
instead of Linear ticket codes. The two features compose — a single message can carry
Linear tickets, GitHub issues, and GitHub PRs at once.

## North star

GitHub-style references in the message text (e.g. `#260`) are verified against the
GitHub repo the send was launched from (via `gh`) and turned into links. The reference
title is surfaced in a collapsed reference block at the END of the message. Crucially,
GitHub `issueOrPullRequest(number:)` resolves a `#N` to EITHER an Issue or a Pull Request:

- **Issues** are merged INTO the `autolink-tasks` ticket reference block (issues and
  tickets are "work items" — they read the same way: `#260 — Title`).
- **Pull requests** ALWAYS get their OWN separate collapsed block, placed AFTER the
  tickets/issues block, even for a single PR (`PRs:` header, with a state annotation
  like `(merged)` / `(draft)`).

ON by default, toggled like any feature: `--no-feature autolink-prs` or
`features: { autolink-prs: false }` in `~/.config/tg-cli/config.yaml`.

## Detection

- A reference is a `#` immediately followed by digits, written as one token: `#[1-9][0-9]*`.
  `#0` is not a thing on GitHub — the leading digit must be `1-9`.
- Boundaries: the char before/after the match must not be alphanumeric — `x#1`, `#1a` are
  NOT references; `(#260)`, `#260,`, `#260.` are. (Mirrors the ticket boundary rules.)
- Tokens containing `://` are skipped entirely: a pasted GitHub URL
  (`https://github.com/owner/repo/pull/260`) must never be linkified inside.
- References are deduplicated preserving first-appearance order (by number).
- Detection runs on the decoded caption (after `\n` escapes), BEFORE extraction/render.
  The tmux `[window]` prefix is NOT scanned (metadata, not message text).

## Verification (gh CLI)

Two `gh` spawns per send, only when detection found references, all via an injected
`Runner` (tests pass a fake; the tg entrypoint passes a `Bun.spawnSync` wrapper run from
the send's cwd):

1. **Repo identity**: `gh repo view --json nameWithOwner -q .nameWithOwner` — the
   `owner/name` the cwd resolves to. Run first; if it fails there is no repo to query.
2. **Batch resolve**: ONE `gh api graphql -f query='...'` spawn resolving every detected
   number at once via aliased fields:

```
query {
  repository(owner: "OWNER", name: "NAME") {
    n260: issueOrPullRequest(number: 260) {
      __typename
      ... on Issue { number title url state }
      ... on PullRequest { number title url state isDraft }
    }
    n42: issueOrPullRequest(number: 42) { ... }
  }
}
```

- `issueOrPullRequest(number:)` errors per-field for a missing number — GraphQL returns a
  partial `data` object (the missing alias is `null`) PLUS a top-level `errors` array.
  This is handled gracefully: present aliases resolve, `null`/errored aliases are negative
  verdicts (the `#N` stays plain text). A whole-response failure (`data: null`) is an error.
- Each resolved ref carries its `__typename` (`Issue` | `PullRequest`), title, url, state,
  and — for PRs — `isDraft`.
- Spawn timeout 10s; on timeout/any unexpected failure the message is sent UNCHANGED with a
  one-line stderr warning. Autolink must never block or fail a send.

### Degradation (mirrors autolink-tasks linear.ts)

Every failure mode is a distinct status and keeps the send going as plain text:

- **no-cli** — `gh` binary not found (spawn returns null). One-time stderr hint to install:
  `brew install gh`.
- **no-auth** — `gh` output mentions authentication (`gh auth login` / `authentication`
  markers). One-time stderr hint to run `gh auth login`.
- **no-repo** — `gh repo view` fails because the cwd is not a git repo or has no GitHub
  remote. Silent (no hint — not actionable, and a non-GitHub cwd is normal); the send
  proceeds plain.
- **error** — anything else (network, malformed response): one-line stderr warning.

The one-time hints REUSE the existing `autolink-tasks` hint-state mechanism
(`features/autolink-tasks/state.ts`, file `~/.config/tg-cli/autolink-tasks.json`). The
`HintKind` union is extended with `'gh-install'` and `'gh-login'`; the state-file parser
stays backward-compatible (old `{install,login}` files still parse).

## Caching (mirrors autolink-tasks cache.ts)

- File: `~/.cache/tg-cli/gh-cache.json` (separate from `linear-cache.json`).
- TTL: **1 hour** per entry.
- Both **positive** (resolved ref stored) and **negative** (confirmed-absent) verdicts are
  cached — a missing `#N` is not re-probed every send.
- **CRITICAL — cache key includes the repo**: the key is `<owner>/<repo>#<number>`. The
  same `#260` means a different thing in a different repo, so a bare `#260` key would be a
  correctness bug. The repo identity from step 1 is folded into every key.
- Cache shape: `{ entries: { "<owner>/<repo>#<N>": { t: <unix ms>, info: GhRef | null } } }`.
  Expired entries are pruned on write. Read/write failures degrade silently to a live probe.

## Rendering (post-render transform)

When ≥1 detected reference resolves, the render is forced to HTML (same mechanism as
`hasTickets`/`hasQuotes`). The transform runs on `plan.textMessages[0]` BEFORE line-spec
quote insertion. Both plain-text and `--format html` sends are supported identically — the
forced-HTML body is already escaped by the time the transform runs, exactly like
autolink-tasks.

1. **Linkify**: every occurrence of a verified `#N` in text segments becomes
   `<a href="URL">#N</a>` — Issues and PRs alike. Uses the same segment-safety walk as
   autolink-tasks (no linkify inside `<a>`, `<pre>`/`<code>`, tag attributes, or `://`
   tokens). The autolink-tasks `linkifyCodes` segment walker is reused via a shared
   linkify over a generic ref table.
2. **Issues → tickets block**: resolved issues are appended to the SAME collapsed
   `<blockquote expandable>` that autolink-tasks builds for its tickets, formatted
   `<a href>#260</a> — Title`. Tickets come first (first-appearance order), then issues
   (first-appearance order). If there is exactly one ticket and zero issues, the
   single-ticket "title on the first line" behavior of autolink-tasks is unchanged; a
   present issue forces the block form (issues never go on the first line).
3. **PRs → own block**: resolved PRs ALWAYS get a separate collapsed
   `<blockquote expandable>` AFTER the tickets/issues block, even for a single PR:
   `PRs:` on the first line, then one `<a href>#N</a> — Title (state)` line per PR in
   first-appearance order. State annotation: `(merged)` / `(closed)` / `(draft)` for an
   open draft / `(open)`.

Both the issue→tickets merge and the PR block are produced by extending the autolink-tasks
`applyAutolink` contract so the two features compose in one pass over the body.

## Non-goals

- No write operations against GitHub, ever (read-only `gh api graphql` / `gh repo view`).
- No scanning of attachments' contents.
- `#0` and non-numeric `#x` are not references.
- No cross-repo references — `#N` is always resolved against the send's cwd repo only.
- No second hint after the first (per kind), even across months.
