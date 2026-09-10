# Full harness x account x model inventory

**Generated:** 2026-09-10T01:21–01:34 UTC (see per-section timestamps below; each source stamps its own fetch time).
**Scope:** every harness `riglib` manages — the canonical 6-kind registry in `riglib/harness_mode.py::HARNESS_MODES`/`HARNESS_MODE_NA` and `riglib/harness_skills.py::KNOWN_HARNESS_KINDS`, cross-checked against `schema/rig.schema.json`'s `harness.kind` enum: **claude-code, codex, opencode, omp, pi, commandcode**.
**Status:** this is a live snapshot, not a one-off. Re-run with the exact commands in [How to regenerate](#how-to-regenerate) any time a current picture is needed — this doc is the durable home for that, per the "full inventory, not sample" rule.

This is a *data inventory*, not a design doc: it does not change any code. See [Gaps found](#gaps-found) for the one filed follow-up.

## Contents
- [1. harness-cli: `harness limits --all --json`](#1-harness-cli-harness-limits---all---json)
- [2. omp: `omp usage --json`](#2-omp-omp-usage---json)
- [3. opencode: `opencode providers list`](#3-opencode-opencode-providers-list)
- [4. pi: `pi --list-models`](#4-pi---list-models)
- [5. review-cli: configured model/seat roster (harness-prefixed)](#5-review-cli-configured-modelseat-roster-harness-prefixed)
- [6. Coverage matrix: does anything watch this harness's accounts?](#6-coverage-matrix-does-anything-watch-this-harnesss-accounts)
- [Gaps found](#gaps-found)
- [How to regenerate](#how-to-regenerate)

---

## 1. harness-cli: `harness limits --all --json`

Fetched 2026-09-10T01:21:43Z. harness-cli's provider set is hardcoded to exactly 5 providers (`harness_cli/limits_collect.py::collect_all()`): Claude Code (multi-account), Codex, z.ai, x.ai, kimi. No `accounts`/`models` subcommand exists (`harness accounts --help` / `harness models --help` both fail — confirmed both at runtime and in `cli.py`'s `build_parser()` source).

| Provider | Account | Plan | Status | Reason | Windows |
|---|---|---|---|---|---|
| Claude Code | `default` | Max 20x | **http_error** | rate limited (HTTP 429) | — |
| Claude Code | alex.mex.ux@gmail.com | Max 20x | **missing_creds** | missing credentials — not guessed from tokens | — |
| Claude Code | mextner@gmail.com | Max 20x | **missing_creds** | missing credentials — not guessed from tokens | — |
| Claude Code | invntrm@gmail.com | Max 20x | **missing_creds** | missing credentials — not guessed from tokens | — |
| Codex | chatgpt@app.glide.vc | Pro | **ok** | — | Weekly: 100% used, resets 2026-09-15T01:22Z |
| z.ai | z.ai | — | **ok** | — | Session: 0% used; Weekly: 100% used, resets 2026-09-10T13:15Z |
| x.ai | x.ai | — | **no_endpoint** | no usage endpoint — not guessed from tokens | — |
| kimi | kimi | — | **http_error** | unauthorized (HTTP 401) | — |

Notes:
- `--current` (vs `--all`) returns only the `default` Claude Code card — confirmed identical shape from two different cwds (`/Users/ultra/xp/harness-cli` and `/Users/ultra/xp/rig-cli`); `harness limits` is fully cwd-independent (reads only `$HOME`-rooted paths).
- The z.ai/x.ai/kimi cards read credentials **only** from opencode's own auth store (`~/.local/share/opencode/auth.json`), not a harness-cli-private credential — i.e. these three rows are really "does opencode's z.ai/x.ai/kimi login still work", not a distinct account.
- **Cross-check discrepancy (informational, not filed as a bug):** harness-cli reports `missing_creds` for the 3 non-default Claude accounts (alex.mex.ux, mextner, invntrm), but §2 below (`omp usage --json`, same 3 emails) shows all 3 have valid, live, working credentials at the same time. This points to a keychain-access difference between the two collectors' runtime context (harness-cli's `providers/claude.py` prefers a macOS Keychain read that can silently no-op outside an interactive session), not that the accounts are actually dead.

## 2. omp: `omp usage --json`

Fetched 2026-09-10T01:27–01:28Z (`generatedAt`: 2026-09-10T01:28:19Z). omp tracks its own multi-provider, multi-account pool independently of harness-cli — 6 provider families, several with more than one account.

### anthropic (3 accounts)

| Account | 5h | 7d | 7d (Fable tier) |
|---|---|---|---|
| invntrm@gmail.com | ok — 0% used | **exhausted** — 100% used, resets 2026-09-13T03:00Z | warning — 90% used, 10% left |
| mextner@gmail.com | ok — 54% used | warning — 99% used, resets 2026-09-15T18:00Z | ok — 68% used |
| alex.mex.ux@gmail.com | ok — 32% used | ok — 86% used, resets 2026-09-15T22:00Z | ok — 39% used |

### xai-oauth (1 account)

| Account | SuperGrok weekly credits | Grok Build (weekly) |
|---|---|---|
| grok@app.glide.vc | **exhausted** — 100% used, resets 2026-09-13T16:53Z | **exhausted** — 100% used, resets 2026-09-13T16:53Z |

### opencode-go (1 account, plan "OpenCode Go")

| Window | Status |
|---|---|
| 5h | ok — 0% used, resets 2026-09-10T06:27Z |
| Weekly | **exhausted** — 100% used, resets 2026-09-14T00:00Z |
| Monthly | ok — 50% used, resets 2026-10-08T17:42Z |

### kimi-code (1 account: `d9krrb4us3e7hvvpf7jg`)

| Window | Status |
|---|---|
| 7d (total quota) | ok — 44% used, resets 2026-09-16T09:16Z |
| 5h | ok — 33% used, resets 2026-09-10T02:16Z |

### cursor (1 account: invntrm@gmail.com)

| Meter | Status |
|---|---|
| gpt-4 requests | 0 used (monthly, resets 2026-10-09T14:18Z) |
| Cursor Models (individual-auto) | ok — 15.25% used |
| Other Models (individual-api) | **exhausted** — $20.00 / $20.00 used |

### openai-codex (1 account: n5q9t29zh7@privaterelay.appleid.com, plan "plus")

| Window | Status |
|---|---|
| 5h (primary) | **exhausted** — 100% used, resets 2026-09-10T02:40Z |
| 7d (secondary) | ok — 16% used, resets 2026-09-16T21:40Z |

`metadata.allowed: false`, `limitReached: true` — this account is currently hard-blocked from new chat requests.

### Disabled credentials (2)

| Provider | Account | Cause | Disabled at |
|---|---|---|---|
| kimi-code | `d9u7kv136ak1c8icqbpg` (oauth) | `invalid_grant` — refresh token rejected | 2026-09-07T09:04Z |
| anthropic | bbv279dsvb@privaterelay.appleid.com (oauth) | `account_on_hold` (claude.ai/restricted) | 2026-09-09T19:34Z |

`accountsWithoutUsage`: none.

## 3. opencode: `opencode providers list`

Fetched live. 8 stored credentials in `~/.local/share/opencode/auth.json`:

| Provider | Auth type |
|---|---|
| DeepSeek | api key |
| fireworks | api key |
| commandcode | api key |
| Z.AI | api key |
| xAI | oauth |
| Kimi For Coding | api key |
| Moonshot AI | api key |
| OpenCode Go | api key |

Cross-check against `opencode models --verbose`'s catalog (`providerID` field, models actually reachable): `opencode-go` (27 models), `zai` (16), `xai` (14), `opencode` native/zen (7, no auth needed), `moonshotai` (4), `kimi-for-coding` (4), `deepseek` (3), `commandcode` (3), `fireworks` (1) — every stored credential has at least one live-reachable model, confirming all 8 are wired, not stale entries.

harness-cli's z.ai/x.ai/kimi providers (§1) only exercise 3 of these 8 credentials (Z.AI, xAI, Kimi For Coding); DeepSeek, fireworks, commandcode, and Moonshot AI have no harness-cli card at all.

## 4. pi: `pi --list-models`

`pi` is a standalone CLI (`/opt/homebrew/bin/pi`), single-provider via `--provider`/`--api-key` flags (no multi-account concept, no subcommand for accounts). Live output on this machine:

```
No models available. Use /login to log into a provider via OAuth or API key.
```

**Status: no provider logged in on this machine.** No account/model data to report.

## 5. review-cli: configured model/seat roster (harness-prefixed)

review-cli is not itself an account/credential store — it dispatches reviews through the other harnesses' own auth. Its `~/.config/review-cli/config.yaml` (live, current) `models:` priority roster is the closest thing this ecosystem has to a canonical "every model in active use, and which harness routes it" table:

| Seat (harness-prefixed) | Harness | Model | Live-pool priority | Availability (from `review --show-board`) |
|---|---|---|---|---|
| `claude:claude-opus-4-8` | claude-code (direct CLI) | claude-opus-4-8 | #1 (pool) | available |
| `codex:gpt-5.6-terra` | codex | gpt-5.6-terra | #2 (pool) | available |
| `oc:zai/glm-5.2` | opencode | glm-5.2 (via z.ai) | #3 (pool) | available |
| `claude:claude-sonnet-5` | claude-code (direct CLI) | claude-sonnet-5 | #4 (reserve) | available |
| `oc:xai/grok-4.6` | opencode | grok-4.6 (via xAI oauth) | #5 (reserve) | available |
| `omp:anthropic/claude-opus-4-8` | omp | claude-opus-4-8 (omp-hosted, multi-account pool) | #6 (reserve) | available |
| `commandcode:deepseek/deepseek-v4-pro` | commandcode | deepseek-v4-pro | #7 (reserve) | **unavailable — provider unpaid/disabled** |
| `commandcode:Qwen/Qwen3.7-Max` | commandcode | Qwen3.7-Max | #8 (reserve) | **unavailable — provider unpaid/disabled** |
| `commandcode:zai-org/GLM-5.2` | commandcode | GLM-5.2 | #9 (reserve) | **unavailable — provider unpaid/disabled** |
| `claude:claude-fable-5` | claude-code (direct CLI) | claude-fable-5 | #10 (reserve, last-resort) | available (~98-100% historical dispatch failure rate per `review stat`) |

`pool: 3` — a bare `review diff` runs seats #1–#3; reserves backfill on failure. `unpaid_providers: [commandcode, gemini]` — every `commandcode:`/`gemini` seat is skipped before any network call, which is why #7–#9 show unavailable regardless of live credential state.

### `review stat --json` (7-day call-log window, since 2026-09-03) — per-harness health

| Harness | Calls | OK | Fail | Notes |
|---|---|---|---|---|
| claude | 74 | 5 | 69 | mostly Fable paywall/exhaustion retries |
| codex | 3 | 0 | 3 | |
| omp | 217 | 161 | 56 | highest-volume, best success rate |
| opencode | 102 | 0 | 102 | 0% success in this window |
| z.ai | 58 | 0 | 58 | 0% success in this window (quota exhaustion incident, review-cli#382) |

This is review-cli's own call-outcome telemetry, not a live rate-limit reading (see review-cli#205 — that gap is tracked in review-cli's own repo, not duplicated here).

## 6. Coverage matrix: does anything watch this harness's accounts?

| Harness | `harness limits --all` | `omp usage --json` | `opencode providers list` | review-cli `--show-board`/`stat` |
|---|---|---|---|---|
| **claude-code** | yes — multi-account (4 dirs found; 1 live, 3 keychain-blind in this run, see §1 note) | yes — 3 accounts, independently live | n/a | yes (`claude:` seats) |
| **codex** | yes — 1 account | yes — 1 account (`openai-codex`) | n/a | yes (`codex:` seats) |
| **opencode** | partial — only reads its z.ai/xAI/kimi credentials, 3 of 8 | yes — `opencode-go` credential only (1 of 8) | yes — full 8-credential list, the only complete view | yes (`oc:` seats) |
| **omp** | **no** | yes — omp reporting on itself | n/a | yes (`omp:` seats, `review stat` harness breakdown) |
| **pi** | **no** | no | n/a | no |
| **commandcode** | **no** (no standalone CLI to query) | no | n/a | yes, but currently in `unpaid_providers:` (skipped, not dispatched) |

Bold **no** cells are the real gaps — see below.

## Gaps found

Filed: **[alex-mextner/harness-cli#21](https://github.com/alex-mextner/harness-cli/issues/21) — "harness limits: no coverage for omp's full provider fleet, opencode's full credential set, or pi accounts"**

Summary of what it covers (do not re-file — see the issue for full acceptance criteria):
- `harness limits --all` has zero visibility into omp's own tracked provider fleet (anthropic x3, xai-oauth, opencode-go, kimi-code, cursor, openai-codex) — of which `opencode-go` weekly and `cursor` individual-api are **already exhausted right now** and harness-cli would report neither.
- `harness limits --all` reads only 3 of opencode's 8 stored credentials (misses DeepSeek, fireworks, commandcode, Moonshot AI).
- `harness limits --all` has no card for `pi` at all.
- Related but distinct, not duplicated: `alex-mextner/harness-cli#17` (narrower — omp `usage_history` for xai/grok only) and `review-cli#205` (review-cli's own local usage-percent coverage, separate repo/gap).

## How to regenerate

Every command below is read-only. Run all of them for a fresh snapshot; none require flags beyond what's shown.

```sh
# harness-cli — per-account/provider rate-limit proximity (§1)
harness limits --all --json

# omp — omp's own multi-account/provider usage (§2)
omp usage --json

# opencode — stored credentials (§3)
opencode providers list
# opencode — full reachable model catalog per credential (cross-check)
opencode models --verbose

# pi — logged-in provider/model (§4)
pi --list-models

# review-cli — configured seat roster + live availability (§5)
review --show-board
review --list-defaults
review stat --json
review help config    # full config/model/board selection cascade reference
```

`harness limits`/`omp usage` are cwd-independent (`$HOME`-rooted only). `review` reads the invoking repo's git/opencode state for repo-scoped modes, but the discovery flags above (`--show-board`, `stat`, `--list-defaults`, `help config`) work from any directory.
