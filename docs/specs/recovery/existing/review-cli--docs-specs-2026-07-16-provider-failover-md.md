# Review addendum: review-cli/docs/specs/2026-07-16-provider-failover.md

Source revision: `c3779b4a2592961f4dcc2d9a98da65ef0e954442`.

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

- [review-cli#26](https://github.com/alex-mextner/review-cli/issues/26) — Propagate canonical ecosystem one-liner to other repos' ecosystem tables
- [review-cli#40](https://github.com/alex-mextner/review-cli/issues/40) — Harden _ensure_opencode_readonly_agent: validate/refuse a pre-existing permissive global read-only-reviewer agent
- [review-cli#88](https://github.com/alex-mextner/review-cli/issues/88) — review-cli: add OpenRouter backend
- [review-cli#94](https://github.com/alex-mextner/review-cli/issues/94) — Add per-provider opencode auth probe in backend_available()
- [review-cli#96](https://github.com/alex-mextner/review-cli/issues/96) — Replace review diff --visual with visual subcommand
- [review-cli#97](https://github.com/alex-mextner/review-cli/issues/97) — Refresh review hook hint and harden visual fallback
- [review-cli#107](https://github.com/alex-mextner/review-cli/issues/107) — deploy.sh: fetch --prune so a deleted/renamed upstream branch is detected
- [review-cli#109](https://github.com/alex-mextner/review-cli/issues/109) — visual: gate Russian-language selection/highlight claims, not just English
- [review-cli#128](https://github.com/alex-mextner/review-cli/issues/128) — Fix review-cli model override and unpaid provider handling
- [review-cli#149](https://github.com/alex-mextner/review-cli/issues/149) — review-cli: prevent stuck model seats from wedging review gates
- [review-cli#153](https://github.com/alex-mextner/review-cli/issues/153) — review diff: per-model reviewer (opencode/glm) wedges indefinitely at ~0% CPU — needs a per-backend watchdog kill
- [review-cli#155](https://github.com/alex-mextner/review-cli/issues/155) — review-cli: comprehensive provider failover + opus routing
- [review-cli#161](https://github.com/alex-mextner/review-cli/issues/161) — review-cli: close 3 P2 provider-failover coverage gaps from PR 157
- [review-cli#170](https://github.com/alex-mextner/review-cli/issues/170) — review-cli: flat review path can pass and checkpoint on rc=0-but-unusable output (pre-existing)
- [review-cli#173](https://github.com/alex-mextner/review-cli/issues/173) — review-cli: consolidate the duplicated provider-failover liveness predicate and state machine
- [review-cli#174](https://github.com/alex-mextner/review-cli/issues/174) — Add omp (Oh My Pi) agentic backend — omp:provider/model seats
- [review-cli#175](https://github.com/alex-mextner/review-cli/issues/175) — Review omp backend cross-file consistency
- [review-cli#176](https://github.com/alex-mextner/review-cli/issues/176) — Review omp backend diff for cross-file consistency
- [review-cli#177](https://github.com/alex-mextner/review-cli/issues/177) — Review omp backend diff for cross-file consistency
- [review-cli#209](https://github.com/alex-mextner/review-cli/issues/209) — review-cli#208 delta-tolerance: stale baseline survives commits + gate-twin divergence (review follow-ups)
- [review-cli#210](https://github.com/alex-mextner/review-cli/issues/210) — Trivial-delta gate (#208): stale companion desync, SHA-256 gitlink width, mktemp portability
- [review-cli#214](https://github.com/alex-mextner/review-cli/issues/214) — Diff-identity gate: --require-identity flag + grace-period cutoff for unverifiable iterations
- [review-cli#215](https://github.com/alex-mextner/review-cli/issues/215) — Diff-identity: defer repo_id/diff_files computation to post-dispatch, bundle into one struct
- [review-cli#216](https://github.com/alex-mextner/review-cli/issues/216) — review-gate pre-commit hook mismatches _write_review_stamp's hash when diff.noprefix=true
- [review-cli#217](https://github.com/alex-mextner/review-cli/issues/217) — Diff-identity gate: ubiquitous-file overlap loophole + local-to-remote repo_id divergence
- [review-cli#237](https://github.com/alex-mextner/review-cli/issues/237) — Graduated mid-call wrap-up nudges at 10/20/30min, hard cutoff at 40min
- [review-cli#250](https://github.com/alex-mextner/review-cli/issues/250) — review-cli: clean-verdict evidence requirement is a heuristic warning, not an enforced gate
- [review-cli#253](https://github.com/alex-mextner/review-cli/issues/253) — brainstorm: auto-grounding on cwd git diff has no relevance check or provenance disclosure
- [review-cli#261](https://github.com/alex-mextner/review-cli/issues/261) — refutation_verdict: document the decoration-separated NO residual, pin run_moderator signature, cover the unusable-moderator skip branch
- [review-cli#271](https://github.com/alex-mextner/review-cli/issues/271) — review-cli: _agentic() doesn't case-normalize the opencode: prefix before deriving the internal seat key
- [review-cli#296](https://github.com/alex-mextner/review-cli/issues/296) — Quality follow-up: PR #279's REVIEW_CLI_ACTIVE-clearing code could be consolidated
- [review-cli#316](https://github.com/alex-mextner/review-cli/issues/316) — Remove gemini entirely from review-cli — dead provider still sits in code defaults (MODERATOR_CANDIDATES, VISUAL_MODELS, DEFAULT_MODELS)
- [review-cli#350](https://github.com/alex-mextner/review-cli/issues/350) — review diff: say why a passing review did not satisfy the commit gate
- [review-cli#358](https://github.com/alex-mextner/review-cli/issues/358) — Review commit-gate notice diff
- [review-cli#360](https://github.com/alex-mextner/review-cli/issues/360) — review diff wedges at zero CPU before any reviewer starts, on large diffs
- [review-cli#381](https://github.com/alex-mextner/review-cli/issues/381) — Close delta-tolerance hook consistency and bypass gaps
- [review-cli#390](https://github.com/alex-mextner/review-cli/issues/390) — review diff: say WHY there is no diff (clean working tree) and how to review a committed branch
- [review-cli#396](https://github.com/alex-mextner/review-cli/issues/396) — Commit gate: single review-gate template (trivial-delta, merge skip) and review --version
- [review-cli#399](https://github.com/alex-mextner/review-cli/issues/399) — codex review seat can run commands outside its read-only sandbox (created and closed rig-cli#384 during a review)
- [review-cli#400](https://github.com/alex-mextner/review-cli/issues/400) — Fix valid TOML version parser escaping project scope
- [review-cli#476](https://github.com/alex-mextner/review-cli/issues/476) — Adapter architecture: factual health, progress-aware liveness and fallback-first execution [design-gated]

## Preserved specification

# review-cli provider-failover — consolidated CTO requirements

Source: Alex's Telegram messages (2026-07-15/16), consolidated after he noted it had
been requested several times and kept regressing (opus→opencode infra-fail blocked a
review quorum). tg#8607, 8614, 8619, 8658, 8664, 8674, 8681, 8719, 8879.

## The model↔provider model

1. **NO 1-to-1 aliases.** A model maps to an ORDERED LIST of providers, tried in order.
   `opus` is not "one backend" — it's a model that several providers can serve.
2. **A "provider" = harness + service**, counted together as one unit. Examples:
   `oc:zai/glm-5.2` = opencode-harness + zai-service; a direct `zai:glm-5.2` = direct-zai;
   `claude:...`; `codex`; other `oc:*`. The SAME model (glm-5.2, opus, …) is reachable via
   MULTIPLE (harness+service) providers — list them all as alternatives per model.
3. **opus defaults to the `claude` provider, NOT `oc`/opencode.** The current bug: `-m opus`
   routes to `opencode -m opus`, whose backend infra-fails → the model is unusable and the
   quorum can't form. opus's first/default provider must be `claude` (claude:claude-opus-4-8).

## Dynamic failover (the core)

4. **Live, mid-review, in-flight switchover.** When a model's current provider fails DURING
   a review call — OR that model is (temporarily or fully) unavailable in that provider — OR
   the WHOLE provider/service is down — switch that model to its NEXT provider and the review
   CONTINUES SEAMLESSLY (the seat does not abort, the board does not degrade). If a whole
   provider/service outage hits, EVERY model that was using it reroutes to its alternates.
5. **Cache the last-working provider per model** (persist, e.g. `~/.cache/review-cli/`); try
   it first next time; rotate on failure.

## Retry + skip (the cascade)

6. **Universal retry-with-backoff for TRANSIENT errors — provider-agnostic, one central
   classifier, NOT ad-hoc per provider.** Before failing over, retry the same provider a few
   times with exponential backoff on: network timeouts (z.ai read-timeouts), DNS failures,
   5xx, connection resets, 429. Permanent errors (400 insufficient-credits, 401/403 auth) do
   NOT retry — skip/fail fast.
   Full cascade: **transient → retry-with-backoff (same provider) → FAILOVER (next provider) →
   skip if unpaid → targeted per-provider error when all exhausted.**
7. **Skip dead/unpaid providers up-front** (never dispatched, never failed-over into):
   `commandcode` (insufficient credits), `gemini` (deprecated + went paid; host no longer
   resolves — fully disabled, not just failed-over).

## Foolproofing (pool/model selection UX)

8. When explicit `-m` narrows the set and some are down and the pool can't converge, and when
   `--pool N` > number of live models:
   - Do NOT fail opaquely — PROPOSE not overriding: use the default models (nothing specified)
     or a preset. For EACH proposal SHOW the models that would run, annotated per-model
     LIVE/DEAD + reason (e.g. "commandcode: insufficient credits (400)", "opus-via-oc: backend
     infra error"). Show ONLY proposals that fit the requested pool size.
   - If even the default board can't converge → FAIL with a TARGETED error enumerating
     per-provider what's wrong (which need credit top-up, which are down/auth, etc.).

## Implementation constraints

- Develop BRANCH-based; never break the live installed `review` (concurrent agents run
  quorums with it). Swap in only when its own tests pass green. (WIP already exists on branch
  `feat/foolproof-pool-selection` from an earlier agent that hit its weekly limit.)
- Config-file changes (pool size, unpaid_providers) are safe to apply live; CODE changes are
  branch-gated.
- Tests: provider-A-fails→falls-over-to-B (mid-review, review completes); opus defaults to
  claude; whole-service outage reroutes all its models; transient retries-then-succeeds;
  permanent (400/401) no-retry; last-working cached & tried first; foolproofing proposals show
  per-model live/dead+reason.
