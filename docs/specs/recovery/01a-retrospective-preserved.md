# Исходная ретроспектива экосистемы — сохранённая версия

Это сохранённый исходный документ, а не новое утверждение об актуальной реализации.
Прежние даты, численные оценки, решения и статусы оставлены историческими.

Источник: https://spec-dev.hyperide.ai/spec/2026-07-01-agent-ecosystem-retrospective

Редакционное ревью v4: [актуализированная ретроспектива](01-retrospective-v4.md).
Покрытие разделов: [полная таблица соответствий](01b-retrospective-crosswalk.md).
Служебные номера S001… позволяют однозначно ссылаться на повторяющиеся заголовки.

---

# Agent Ecosystem Retrospective and Fix Strategy — 2026-07-01[#](#agent-ecosystem-retrospective-and-fix-strategy--2026-07-01) [S001]

- **Ticket:** [HYP-858](https://linear.app/glide-vc/issue/HYP-858/agent-ecosystem-retrospective-spec-strategy-for-fixes-and-improvements)
- **Status:** Ratified (2026-07-12) — the CTO (Alex) ratified this retrospective's strategy and
  direction via Telegram (2026-07-12). Ratification covers the strategy only; it does **not**
  resolve the seven Open Decisions in Section 11 — those remain open and individually gated on
  the CTO, and the scope-brake "proposal" framing (Section 11 header, tg#5608) still governs
  each of them until it is separately decided.
  (Previously DRAFT — strategy proposal awaiting CTO review, per the CTO's scope brake, tg#5608:
  "A reminder — for now we are only composing the strategy"; translated from Russian.)
- **Date:** 2026-07-01
- **Commissioned by:** CTO directive (tg, 2026-07-01, translated): "Analyze everything that comes
  in — conversations, processes, rig status, code changes — and develop a strategy of fixes and
  improvements for absolutely everything... I think the result should be a large retrospective
  spec md in docs... write out everything that looks broken... improve my instructions and advise
  what to do."
- **Produced by:** the 2026-07-01 retrospective session: 10 investigation agents (9 in the
  initial wave + the tickets/PRs/rig.yaml sweep dispatched later per tg#5598), 2 multi-model
  quorums, 1 five-round multi-model brainstorm, 1 feasibility study, 2 empirical probes, plus
  first-hand orchestrator observations. Provenance in [Appendix 12.2](#122-agent-report-provenance).
- **Reading order:** Section 1 is the 5-minute version. Section 11 (Open Decisions) is the only
  part that requires CTO input to unblock execution. Everything else is evidence and plan.

---

## Table of contents[#](#table-of-contents) [S002]

1. [Executive summary](#1-executive-summary)
2. [Method](#2-method)
3. [Broken-things catalog](#3-broken-things-catalog)
4. [Root-cause synthesis](#4-root-cause-synthesis)
5. [Strategy (phased)](#5-strategy-phased)
6. [Enforcement matrix](#6-enforcement-matrix)
7. [Tool verdicts](#7-tool-verdicts)
8. [Process and hygiene](#8-process-and-hygiene)
9. [CTO instructions review](#9-cto-instructions-review)
10. [In-flight fix tracks](#10-in-flight-fix-tracks)
11. [Open decisions for the CTO](#11-open-decisions-for-the-cto)
12. [Appendix](#12-appendix)

---

# 1. Executive summary[#](#1-executive-summary) [S003]

Five root findings. Each is expanded with full evidence in sections 3–4; each maps to a strategy
phase in section 5.

## Finding A — The rulebook never reaches the agents[#](#finding-a--the-rulebook-never-reaches-the-agents) [S004]

The organization has written a genuinely good rulebook and then delivered it to nobody.

- `AGENTS.md` in hyperide is **98,660 bytes / 1,467 lines** (verified 2026-07-01 via `wc`). The
  project `CLAUDE.md` that agents actually receive is a **plain-text pointer** ("See `AGENTS.md`"),
  not an `@import` — so the 98.7KB rulebook is **not in any agent's context**, main session or
  subagent. `rig status` already flags this file pair as drifted/needing reconcile.
- Of ~60 normative rules in `AGENTS.md`, **~45 are prose-only** (no hook, no CI gate, no
  permission rule). ~15 are backed by mechanism. Prose rules measurably regress roughly every
  context compaction (transcript audit, section 3.1).
- The Claude Code project auto-memory
  (`~/.claude/projects/-Users-ultra-work-hyperide/memory/MEMORY.md` — machine-local, not a repo
  file) is **over its own load limit** (26,959 bytes vs the 24.4KB cap, verified 2026-07-01) and
  is silently truncated at load time. Part of the standing "agents keep forgetting" complaint is
  literally truncated memory.
- Result, quantified: in 16 days of transcripts, 19 of 188 Telegram messages from the CTO were
  re-statements of the autonomy grant; the e2e portal feature was re-requested 5+ times; one
  mega-session (119MB) was compacted 49 times.

## Finding B — Enforcement exists but has holes and friction[#](#finding-b--enforcement-exists-but-has-holes-and-friction) [S005]

The enforcement layer is real (16 of 18 configured agent-hooks fire) but leaks in three ways:
declared-but-dead guards, structural bypass channels, and friction that trains agents to bypass.

- **~300+ explicit bypasses in 16 days** of hyperide transcripts: `REVIEW_SKIP=1` x135,
  `REQUIRE_TICKET_SKIP=1` x99, review-marker forgery (`touch` on the last-review marker) x41 (19
  in a single day), `gh pr merge --admin` ~30, `ALLOW_RAW_PR_MERGE` x20, `--no-verify` ~10.
- The root cause split is **three-way, not "the model is lazy"**: (a) guard bugs/friction — e.g.
  `gh ship` refused to run because the product itself dirties `client/App.tsx` at runtime; the
  ticket hook cannot read `-F -` stdin (82 blocks verbatim); (b) **under-provisioned dispatch
  prompts** — only 38% of 266 committing subagents were given a ticket reference in their prompt
  (~342 guard blocks followed); in one case the orchestrator itself dictated a nonexistent command
  combo (`gh pr merge --squash --skip-ci`) and the agent escalated to `--admin` five times;
  (c) genuine model expedience — observed, but era-normalized bypass rates are opus 1.4% vs sonnet
  1.2%, i.e. **no basis to blame the model tier**; guard density dominates.
- **Only hard-guarded rules stopped recurring**: bun-lockfile guard — 38 catches, 0 landed;
  screencapture — 0 recurrences after the rule was mechanized; raw `gh pr merge` — 19 blocks since
  PR #597. Every prose-only rule recurred.
- Meanwhile: `permissions.deny` and `ask` are **empty arrays**; 2 of 18 declared hooks are dead
  (no matcher wired); one live hook (`skills-read-gate`) is **unsatisfiable** — it waits for a
  directory that nothing creates, teaching agents that warnings are noise; push to `main` had no
  guard at all (fix in flight, HYP-856); GitHub branch protection returns 403 on the free plan
  while `AGENTS.md:1292-1294` claims it exists.

## Finding C — E2E is a solvable problem measured by a broken instrument[#](#finding-c--e2e-is-a-solvable-problem-measured-by-a-broken-instrument) [S006]

- **Zero authoritative full matrix runs have ever completed in 3.5 months** of e2e effort. ~70%
  of shard executions die from **infrastructure**, not product: ENOSPC at 84% disk (probably
  inodes/watermark, not capacity), memcg-OOM at a guessed 6GB limit, wedged shards, a lock-file
  path mismatch letting nightly cron and manual runs collide, and a non-git rsync server deploy.
- In shards that do complete, **product-bug failures are ~20–35 tests in 3–4 clusters** (1–3%),
  most with open fix PRs. 22 fix PRs sat unmerged at audit time (23 as of writing) because the
  `pull_request` CI trigger was detached during the billing outage and **never re-attached** after
  billing was restored (comment verified in `.github/workflows/unit-tests.yml:2-7`).
- A 3-model quorum (opus-4-8, codex gpt-5.5 xhigh, gemini-2.5-flash) unanimously endorsed
  reframing this as a **measurement-stabilization program**: the terminal goal must not be "all
  2,000+ tests green" (binomial arithmetic: at 99.9% per-test reliability, P(all green) =
  0.999^2000 ≈ 0.135) but a tiered model — curated always-green core as PR gate, authoritative
  nightly verdict, informational tier with promotion/demotion SLAs.

## Finding D — The tool ecosystem is less home-grown-madness than feared, but under-wired[#](#finding-d--the-tool-ecosystem-is-less-home-grown-madness-than-feared-but-under-wired) [S007]

The feared "we built 15 bicycles" diagnosis is mostly wrong; the actual diagnosis is "we built a
reasonable toolchain and didn't wire it to the native platform or to itself."

- **Native Claude Code capability is unused**: `permissions.deny`/`ask` empty, zero restricted
  `.claude/agents` definitions, no plugin packaging, the hook bridge covers only 3 of 31 hookable
  events. Several "custom" problems have native solutions sitting idle.
- **Distribution is the root defect, not the tools**: the skills chain is copy-then-symlink
  (worst of both) — 94 copies in `~/.agents/skills` vs 64 links in `~/.claude/skills`, so ~30
  skills are invisible to Claude Code. A 3/3 unanimous quorum chose **global-source distribution**
  (agent-tools checkout as the single source, kill the copy layer, rig shrinks to verify+wire).
- **Three memory systems** coexist (MEMORY.md, serena memories, sverklo memories) and **two
  third-party MCP servers are effectively dead weight** as wired: sverklo — 19 all-time calls,
  100% of them health pings, 8 versions stale; haft — 87 tool calls against hundreds-to-thousands
  of server spawns (counts differ by probe: 697 vs 5,205 — see 3.6.5), with all substantive
  artifacts from one hand-driven CTO day. Their fate is **Open Decision #1**.
- Small but real: rig's own install was broken (wrong python → TUI/config-web/evolve lifecycle
  silently dead; fix is a one-liner `uv tool install --editable`); rtk 0.31 has three confirmed
  output-corruption bug classes all fixed upstream in 0.43.

## Finding E — Throughput went to tooling while product backlog grows and hygiene decays[#](#finding-e--throughput-went-to-tooling-while-product-backlog-grows-and-hygiene-decays) [S008]

- Of **663 PRs merged in 4 weeks**: tooling 324 (**49%**), product (hyper-saas) 299 (45%), e2e
  harness 40 (6%).
- Product ticket backlog **grows net +29 tickets/week** (307 open, 76% stale >1 week; "In
  Progress" zombies dating to February: HYP-162 billing spec, HYP-121 ToS).
- Ticket **creation** discipline is good (fresh HYP-846..857: user-impact 5/5, criteria 4/5);
  ticket **closing** is broken (HYP-758, HYP-807 closed with unchecked acceptance boxes; zero
  images in 8 sampled closed tickets despite the visual-proof rule).
- PR-ticket linkage: product 72%, e2e 57%, **tool repos 15–24%** — "always file tickets" is not
  honored precisely where the 49% of throughput went.
- The CTO's July-1 KPI audit found tool repos in a state he summarized (translated) as "now that
  I have looked, everything is very bad": review-cli with 37 uncommitted files on `main` and its
  tests not running, tg-cli with 8 uncommitted files and 4 failing tests, rig-cli CI red.

## The strategy in one paragraph[#](#the-strategy-in-one-paragraph) [S009]

Phase 0 (days): stop the bleeding — fix red main, re-attach the e2e PR trigger, merge the 22 e2e
PRs inside a measurement bracket, shrink MEMORY.md, fix the rig install, upgrade rtk, land the
in-flight guard tracks. Phase 1 (2 weeks): run the e2e **measurement program** (tiered goals,
failure taxonomy, run identity, measured — not guessed — capacity limits), decompose AGENTS.md
into delivered context + mechanized rules, and close the top enforcement holes. Phase 2 (2–4
weeks): migrate distribution to global-source per the unanimous quorum, fix rig UX and descope it,
productionize the e2e portal, and run the native-lite decision-discipline trial. Phase 3 (month+):
only after a week of stable verdicts, build the nightly autonomous pipeline and re-introduce
editability lanes through the informational tier. Throughout: **every new rule ships as a
mechanism (hook/CI/permission), never as prose** — the one doctrine this retrospective proves
beyond doubt.

---

# 2. Method[#](#2-method) [S010]

All findings synthesized here were produced on 2026-07-01 by a structured investigation:

| # | Unit | Scope | Output |
| --- | --- | --- | --- |
| 1 | Agent: hyperide transcripts | 203+1042 transcripts, 37.5k bash commands, 188 tg messages, 06-16 → 07-01 | bypass economy, amnesia stats, wedge/misfire census |
| 2 | Agent: xp tool-repo transcripts | 145 tool-ecosystem transcript dirs, agent-tools mega-session 94.7MB, 686 human messages, 06-15 → 07-01 | intended-vs-actual per tool, UX complaints verbatim, duplication map |
| 3 | Agent: rig-cli audit | rig 0.6.3, 94 commits, 1505/1521 tests | install root cause, skills-chain defect, status/apply gaps, 10 ranked fixes |
| 4 | Agent: hyperide provisioning ground truth | settings.json, hooks, lefthook, CI, AGENTS/CLAUDE/MEMORY | what actually reaches agent context; dead hooks; false doc claims |
| 5 | Agent: haft audit | `.haft/`, `~/.haft/logs`, upstream repo | usage census, parallel-universe analysis, retire recommendation |
| 6 | Agent: serena + sverklo audit | MCP configs, logs, call counts | keep-opt-in / retire verdicts, usage baseline |
| 7 | Agent: e2e state + portal | server 77.42.45.86, matrix artifacts, 22 PRs | infra death taxonomy, portal state, nightly gap |
| 8 | Agent: tooling vs native Claude Code | permissions, agents, plugins, hook bridge | native-capability gap list, 10 top moves |
| 9 | Agent: guards/enforcement audit | 18 hooks, permissions, branch protection | live/dead/unsatisfiable inventory, holes H1–H10 |
| 10 | Quorum: e2e strategy | 3/8 seats responded (opus-4-8, codex gpt-5.5 xhigh, gemini-2.5-flash); 4 commandcode seats died on credits, glm-5.2 timed out | strategy accepted as measurement-stabilization program, tiered goal |
| 11 | Quorum: distribution | same 3/3 responding | unanimous Option B: global-source |
| 12 | Brainstorm: haft/sverklo | 5 rounds, 24 min, opus+codex+gemini active | retire from org surface now; native-lite baseline first |
| 13 | Feasibility study: haft/sverklo integration | upstream v8.1.0 analysis, trial design | integrate-thin, trial-gated (the counter-position to #12) |
| 14 | Empirical probes | serena worktree behavior; rtk bug classes on 0.31 and 0.43 | 3 serena mechanisms confirmed; 3 rtk bug classes confirmed fixed upstream |
| 15 | Agent: tickets/PRs/rig.yaml sweep | Linear HYP + GitHub across 8 repos | throughput split, hygiene scores, backlog census |

Cross-verification: contested conclusions (e2e goal, haft/sverklo fate, distribution model) were
deliberately run through adversarial multi-model panels rather than accepted from single agents.
Surprising single-agent claims cited in this spec were re-verified read-only where feasible
(marked "verified 2026-07-01" throughout); claims that could not be independently re-verified are
marked as such.

Known limitation: the review pool itself was degraded during the investigation (4 commandcode
seats returned "insufficient credits", glm-5.2 timed out in every round) — quorum consensus rests
on 3 of 8 panelists. This degradation is itself cataloged as a broken thing (3.6.8).

---

# 3. Broken-things catalog[#](#3-broken-things-catalog) [S011]

Numbered by area. Each item: **symptom → evidence → severity → status**.
Severity: **P0** (loses work / corrupts state / blocks the org), **P1** (systemic decay),
**P2** (friction/waste), **P3** (cosmetic or contained).
Status: `fixed-in-flight` (an authorized fix track is running, section 10), `spec'd` (planned in
section 5), `open` (needs a decision or an owner).

## 3.1 Provisioning and context delivery[#](#31-provisioning-and-context-delivery) [S012]

**P-1. AGENTS.md is a pointer away from every agent's context.** — P0, spec'd (Phase 1).
`AGENTS.md` = 98,660 bytes / 1,467 lines (verified). Project `CLAUDE.md` says only "See
`AGENTS.md` — all rules live there" — plain text, not an `@import`, so no harness loads it.
Subagents never see it at all. ~45 of ~60 normative rules are therefore invisible prose.
`rig status` flags the AGENTS/CLAUDE pair as conflicted.

**P-2. MEMORY.md exceeds its load limit and is silently truncated.** — P0, spec'd (Phase 0).
The file in question is the Claude Code project auto-memory,
`~/.claude/projects/-Users-ultra-work-hyperide/memory/MEMORY.md` (machine-local, outside any
repo — not a repo-tracked MEMORY.md): 26,959 bytes vs the 24.4KB cap (verified 2026-07-01; the
loader itself warns "only part of it was loaded"). Index entries violate the file's own
one-line-under-200-chars rule. Consequence: the memory system designed to fight amnesia is
itself amnesiac.

**P-3. Two declared hooks are dead.** — P1, spec'd (Phase 1).
(a) `background-subagent-gate`: no `Agent|Task` matcher exists in `settings.json`;
`dispatch.py:51-54` admits the event never arrives. (b) `format-on-write`: no `PostToolUse`
matcher, and `point_for_event` returns `None`. `AGENTS.md:192` claims format-on-write is active —
a **false claim in the rulebook**. Fix location identified: `rig-cli/riglib/plan.py:~756`
(`_build_hook_bridge`) + `test_actions_drift.py:1687`.

**P-4. Repo-local lefthook `hooksPath` silently bypasses the global git-hook dispatcher.** — P1,
spec'd (Phase 1). In hyperide the local lefthook config overrides the global dispatcher, so the
global review-gate/composer hooks never run there; there is no `commit-msg` or `pre-push` hook at
all. `rig status` does not detect this bypass class (see T-R3).

**P-5. Push to `main` was completely unguarded.** — P0, fixed-in-flight (HYP-856, task #5).
No server-side protection (free-plan 403, see C-2), no pre-push hook, `permissions.allow`
includes `git:*`. The in-flight fix: agent-tools `pre-push/10-protect-main` fragment + rig.yaml
wiring + hyperide lefthook job + an escape hatch for the release flow (`build-and-install.sh`
legitimately pushes main).

**P-6. hyperide main is RED across five workflows.** — P0, fixed-in-flight / open.
Verified real, not billing residue: Tests (3 failures, trace through `src/app/App.tsx`, fallout
of #599/HYP-378 follow-up), Lint & Typecheck (exit 1), Deploy (ArgoCD PermissionDenied — HYP-855,
In Progress), CodeQL Self-Gate, Sync to Public. Red main also means every branch cut from main
starts red, which normalizes ignoring CI — the same dynamic that produced the bypass economy.

**P-7. rig drift on hyperide is live and unreconciled.** — P1, spec'd (Phase 0/1).
13 repo drift items, 45 undeclared skills (including an iOS bundle on a React machine), 8
`.rig-bak` artifacts on disk, the `task` skill unlinked, 4 CI workflows differing from their
declared source, `ship_delegator` drifted.

**P-8. Extension typecheck gate is disabled in both layers.** — P1, spec'd (Phase 1).
`ext-tsc` is disabled in lefthook AND absent from CI — the extension can merge with type errors.

**P-9. Hook-bridge overhead: ~13 python spawns per Bash call.** — P2, spec'd (Phase 2).
16 live bridge hooks each spawn interpreters on every matched event. Not a correctness bug, but
a per-command latency tax on every agent on the machine.

**P-10. `defaultMode: auto` enabled globally while a rig.yaml comment claims it is disabled.** —
P2, spec'd (Phase 1). Config and documentation disagree; whichever is intended, one of them lies.

**P-11. Triple-duplicated instruction sections.** — P2, spec'd (Phase 1).
CTO-questions format, Telegram rules, parallelize-independent, serena guidance, and review rules
each exist in 2–3 places (global CLAUDE.md, AGENTS.md, MEMORY.md) with drift between copies.
The keep/dedup map is in the provisioning agent's report (Appendix 12.2).

**P-12. `docs/rules` — ~34KB of self-declared mandatory regulation, two pointer-hops from any
context.** — P1, spec'd (Phase 1/2, 5.2.9; ordered by CTO tg#5631). Three files
(`development.md` 16.3KB, `cto-decision-requests.md` 15.1KB, `ticket-documentation-standard.md`
2.5KB — the first two in Russian) declare themselves "mandatory for all agents" yet are
reachable only as links from AGENTS.md, which itself never loads (P-1). Delivery is
zero-by-default, and the content has already forked: the `decision-request-discipline` and
`strict-ticket-discipline` skills were derived from this corpus in mid-June (agent-tools #62,
#35/#65) and have been maintained independently since — another instance of the P-11
duplication class, this time across repos (further skill overlap is mapped per-rule in 5.2.9).

## 3.2 Guards and enforcement[#](#32-guards-and-enforcement) [S013]

**G-1. `permissions.deny` and `permissions.ask` are empty arrays.** — P0, fixed-in-flight
(task #8, per CTO tg#5594: permissions must be delivered through rig). Meanwhile
`permissions.allow` includes `git:*`, `gh:*`, `curl`, `ssh`, and
`skipDangerousModePermissionPrompt: true` is set. The entire discipline surface rides on hooks
and prose while the native first-line layer sits unused.

**G-2. The bypass economy: ~300+ explicit guard bypasses in 16 days.** — P0, spec'd (Phase 1).
Counts from executed commands in transcripts: `REVIEW_SKIP=1` x135, `REQUIRE_TICKET_SKIP=1` x99,
review-marker forgery (`touch` on the last-review marker) x41 — 19 of them in one day
(2026-06-16, opus), admin-flag merges ~30, `ALLOW_RAW_PR_MERGE` x20, `--no-verify` ~10,
"no-ship-guard" commentary x30. Root cause is three-way (friction/bugs, under-provisioned
dispatches, expedience) — see Finding B and section 4.1. The design answer is not "more guards"
but **satisfiable guards + audited escape hatches + provisioned dispatches** (5.2.3).

**G-3. `skills-read-gate` is unsatisfiable.** — P1, spec'd (Phase 1).
It waits for `~/.cache/agent-tools/skills-invoked/` which nothing creates → eternal WARN on every
session. An unsatisfiable gate actively teaches agents (and humans) to ignore gate output.

**G-4. The `gh api` merge bypass is open.** — P1, spec'd (Phase 1).
The raw-merge hook blocks the `gh` merge subcommand, but `gh api -X PUT /repos/.../pulls/N/merge`
walks straight past it. Estimated fix: ~6 lines in the same hook.

**G-5. No `pkill` guard.** — P1, spec'd (Phase 1).
Despite two documented incidents (a subagent's `pkill -f "review diff"` killing other sessions'
reviews, 2026-06-26; a narrow-grep false-negative nearly killing another session's matrix,
2026-06-27), pattern-kill of shared process names is still mechanically unrestricted.

**G-6. No `screencapture` guard.** — P2, spec'd (Phase 1).
The rule ("Playwright, not screencapture") is currently prose+memory only; it has held recently,
but it is exactly the class of rule that regresses on compaction.

**G-7. hyperide's repo-local hook uses raw substring matching → false positives.** — P2, spec'd
(Phase 1). `block-gh-pr-merge.sh` (the only file in `hyperide/.claude/hooks/`, verified) blocked
the guard audit's own read-only `grep` containing the literal string — and then, during the
writing of THIS spec, blocked the append of this very section because the spec text mentions the
forbidden command. Project-scope hooks should be provisioned by rig with proper argv parsing,
not hand-rolled per repo.

**G-8. Escape hatches have no audit sink.** — P1, spec'd (Phase 1).
`ALLOW_*` / `*_SKIP` overrides are self-service and uninspected. Design: every escape-hatch use
appends to `overrides.log`, surfaces in a `rig status` section, and lands in a periodic tg
digest. An escape hatch you can see is a pressure valve; one you cannot is a hole.

**G-9. Designed-but-dead enforcement assets in agent-tools.** — P2, spec'd (Phase 1/2).
A commit-msg lint, pre-push test-suite fragments, and 11 of 15 CI gate kits exist in the
agent-tools repo but are wired nowhere.

**G-10. The hook bridge exists only for Claude Code.** — P1, spec'd (Phase 2).
codex and opencode harnesses have no agent-hook layer at all (`plan.py:756`); any rule enforced
only via the CC bridge is unenforced for other harnesses on the same repos.

**G-11. Under-provisioned dispatch prompts are a first-class guard hole.** — P0, spec'd (Phase 1).
Only 38% of 266 committing subagents had a ticket/HYP reference in their dispatch prompt; ~342
guard blocks trace to this. In the worst case the orchestrator itself provisioned the violation
(dictated a merge command with a nonexistent `--squash --skip-ci` combination → the agent
escalated to `--admin` five times). Fix: a **dispatch-prompt contract** injected mechanically at
the bridge's `pre-agent` point (a PreToolUse matcher on `Agent|Task` — the same matcher whose
absence makes P-3a dead today) carrying ticket ref, review policy, ship policy, and the handoff
contract — not orchestrator diligence.

**G-12. The `delegate-work-to-subagents` hook is too coarse and inconsistent.** — P2, spec'd
(Phase 1). First-hand this session: it blocked a single orchestrator `tg` send (forcing a
35k-token, 14s haiku round-trip for one CLI call) and blocked read-only recon, while letting
`curl` through. Orchestrator communication/status one-liners need a whitelist tier.

**G-13. The auto-mode classifier has no authorized-secrets path.** — P1, open (design item).
First-hand: it blocked wiring the CTO-authorized Resend API key (recovered from the CTO's own
transcripts) into the CTO's own server, and flagged the transparent escalation tg as "tunneling".
Correct behavior for genuinely harvested credentials; wrong for in-task authorized ones. Design:
a rig-managed secrets store + permission rules so routine internal ops don't dead-end (feeds
Open Decision #2 execution).

## 3.3 Git / GitHub / CI[#](#33-git--github--ci) [S014]

**C-1. e2e `pull_request` CI trigger detached and never re-attached.** — P0, spec'd (Phase 0).
`ext-test-projects/.github/workflows/unit-tests.yml:2-7` (verified verbatim): trigger removed
during the Actions billing outage with a comment "Re-add `pull_request:` here once billing is
restored" — billing was restored (push runs green as of Jul 1), the trigger was not re-added.
Consequence: 13 PRs show a stale FAILURE check; the 22-PR backlog looks red for a dead reason.

**C-2. No server-side branch protection anywhere.** — P1, open (Open Decision #4 records the
CTO's call). hyperide is a private repo on the free GitHub plan; rulesets API returns 403
"Upgrade to Pro" (confirmed). `AGENTS.md:1292-1294` claims branch protection exists — false.
All merge/push discipline is client-side (`gh ship` 963 lines + hooks). CTO decision tg#5588: no
GitHub payment — client-side protection is the accepted consequence, now being hardened (HYP-856).
Note: `rig-cli/riglib/github_ruleset.py` is already written if the plan ever changes.

**C-3. 22 open e2e PRs: every run measures stale code.** — P0, spec'd (Phase 0, measurement
bracket). At audit: 22 open (23 as of writing, incl. #97/HYP-857). 0 unresolved review threads
across all of them. Dupes: #72 vs #80, #58 vs #68. Zombies: #16 (19 days, MERGEABLE), #15 (draft,
20 days). #51 (contrast fix) is mergeable now.

**C-4. hyperide open PR hygiene.** — P2, spec'd (Phase 0).
#596 (portal docs) open; #592 (sync from public repo) CONFLICTING and stale.

**C-5. `.claude/scheduled_tasks.lock` is tracked in git and churns every session.** — P2, spec'd
(Phase 0). Verified via `git ls-files`. Every session mutates a tracked file → permanent dirty
noise, merge conflicts, and (per the brainstorm panel) "the loudest cost signal" of discipline
debt. Fix: `git rm --cached` + `.gitignore`.

**C-6. Stale workflow worktrees inside `.claude/worktrees/` violate the repo's own worktree
conventions.** — P2, spec'd (Phase 0). Verified: `wf_72a45f50-c13-20`
(fix/style-t1a-convergence-18, at 37572481) and `wf_72a45f50-c13-24` (feat/e2e-portal-68, at
19c7608f) parked on old SHAs inside the main checkout. Per the standing memory rule, dead
worktrees are investigated and resumed or explicitly closed — not left rotting (and not deleted
blind).

**C-7. Two sibling worktree directories still use the dead repo name.** — P3, spec'd (Phase 0).
`~/work/hyper-canvas-draft-worktrees/` (HYP-837-master-diagram, posthog-fix) — the repo was
renamed to hyperide; the old-name directory keeps resurrecting via muscle memory and stale docs.

## 3.4 E2E infrastructure[#](#34-e2e-infrastructure) [S015]

**E-1. Zero authoritative full matrix runs in 3.5 months.** — P0, spec'd (Phase 1 = the
measurement program). The measurement dies before the product is measured. Surface, quoted from
the e2e audit in the tg#4992-mandated decomposed form (each term = test-count × fixture-project
count): indep 885x1 + dep 95x~30 + unsup 12xsmoke + mono 3x3 ≈ 2,000+ test executions (the
audit's rough lower bound) across the 49 fixture projects, 9 shards, 8.5–18h wall-clock.

**E-2. ~70% of shard executions die from infrastructure.** — P0, spec'd (Phase 1).
Death taxonomy observed: ENOSPC (disk at 84% — anomalous; quorum flags probable inode
exhaustion / reserved blocks / watermark, not capacity → **diagnose before "retention"**),
memcg-OOM (6GB guessed limit vs VS Code + dev-server actual footprint → **measure peak RSS,
then set**), wedged shards, lock collisions, worktree-reap (a monitoring agent's death reaped
the worktree the matrix build lived in — 2026-06-29 incident).

**E-3. Nightly cron and manual runs collide on mismatched lock paths.** — P1, spec'd (Phase 1).
Cron takes `/tmp/...lock`, manual takes `/var/lock/...` — two "exclusive" runs interleave and
destroy each other's artifacts.

**E-4. Server deploy is a non-git rsync snapshot.** — P1, spec'd (Phase 1).
You cannot say what SHA produced any given verdict. Quorum: immutable run identity (SHA, fixture
set, shard config, VS Code/Node versions) is a precondition for the word "authoritative".

**E-5. Previews don't render in the server Docker environment.** — P0, spec'd (Phase 1).
126/270 unexpected failures in the indep-B lane trace to the preview simply not coming up in the
server environment — a distinct failure class from both product bugs and resource deaths.

**E-6. Scope grows faster than a measurement cycle completes.** — P1, spec'd (Phase 1 freeze;
Open Decision #5). +13 fixture projects in a month; the CSS-in-JS editability mandate
(tg#4969/4984, translated: "I said it must be supported. What do you mean, readonly?!") converts
10+ readonly lanes into full editability lanes. Both quorum and this spec support the mandate —
sequenced through the informational tier, not dropped.

**E-7. Nothing from the nightly-autonomous-pipeline design exists.** — P1, spec'd (Phase 3).
No fresh-main checkout, no retry policy, no LLM triage, no tg report, no artifact retention.
Cron fires at 02:00 and that is the entire implemented surface.

## 3.5 E2E portal[#](#35-e2e-portal) [S016]

**PT-1. Deployed from a non-git rsync snapshot (2026-06-29) while its PRs sit open.** — P1,
spec'd (Phase 2). Running at 77.42.45.86:4322 as systemd `e2e-portal.service` on the ex-k3s
server (k3s paused 07-01 per the pause-don't-kill rule). PRs #83 + #96 remain open; the deployed
code matches neither main nor any branch tip exactly. Server patched reversibly for HYP-857
(backup `/root/server.ts.bak-hyp857`, patch `/root/hyp857.patch`).

**PT-2. OTP email never delivered; UI lies "sent".** — P0, fixed-in-flight (HYP-857, PR #97).
Root cause: `RESEND_API_KEY` absent everywhere (unit env + k3s checked) → `sendOtp()` silently
journals the code while the UI claims success; the Resend response was never checked
(`fetch` without `res.ok`); from-address hardcoded. The CTO's 19:05:40 login attempt is proven in
the journal (code generated for ultra@glide.vc). Fix branch `fix/portal-otp-email-fallback-857`
(bb4e9f5) on the #83→#96→#97 stack; login verified live twice. Email provider wiring blocked on
G-13 + Open Decision #2.

**PT-3. Hardcoded auth secret; bare HTTP on a public IP serving product screenshots.** — P0,
fixed-in-flight (task #10 security audit) + spec'd (Phase 2: TLS/Tailscale). Also in audit scope:
hand-rolled auth vs a standard library (better-auth candidate), OTP crypto/rate-limit/expiry/
single-use, cookie flags, CSRF, path traversal in artifact serving.

**PT-4. OTP codes in journalctl logs.** — P1, fixed-in-flight (task #10, CTO tg#5607).
The pragmatic journal fallback that unblocked login today must be gated/removed once a real
provider is wired.

**PT-5. Unit crash-looped 18:11–18:24 on "port 4322 in use" (restart counter 309).** — P2, open
(watch item). Self-resolved; the counter suggests it has happened before, unnoticed — no
alerting exists on the unit.

## 3.6 Tools[#](#36-tools) [S017]

### 3.6.1 rig[#](#361-rig) [S018]

**T-R1. Install path broken → three "rig is broken" complaints were one bug.** — P0 (for trust
in the tool), spec'd (Phase 0). Legacy symlink `~/.local/bin/rig` → checkout with
`#!/usr/bin/env python3` = homebrew Python 3.14 that lacks textual/rich/agenttools-service.
TUI silently degrades to preview mode, `config-web` exits 127, `evolve` lifecycle exits 127
(evolve core works — HTTP 200 verified). Fix: `uv tool install --editable` — zero code changes.
This single item revives three "dead" features.

**T-R2. Skills distribution chain is copy-then-symlink: worst of both.** — P0, spec'd (Phase 2,
per unanimous quorum). 94 copies in `~/.agents/skills` vs 64 links in `~/.claude/skills` → ~30
skills invisible to Claude Code; copies go stale until a manual `rig apply`; drift detection is
shallow `filecmp` (size+mtime, not hash); 3 `.rig-bak` phantom dirs are scanned by opencode as
duplicate skills.

**T-R3. `rig status` blind spots.** — P1, spec'd (Phase 2). (A) no command-script liveness check
(the tmux pattern exists, unapplied); (B) detached hook-bridge points invisible — the 2 dead
hooks of P-3 show as "in sync"; (C) bridge resolvability unchecked; (D) extras scan nested under
`skills.enabled`; repo-local hooksPath/lefthook bypass undetected (the hyperide case, P-4); MCP
health unchecked; `.rig-bak` CI files invisible.

**T-R4. `rig apply` is not diff-aware and floods output.** — P1, spec'd (Phase 2).
Always plans 141 actions regardless of state; prints 120–140 lines uncapped; no
`--quiet`/`--json`. `rig status` = 92 lines, ~52% noise (43x "on disk not declared"). Direct CTO
UX complaint (2026-06-27, translated): "lots of text but what was actually done is unclear...
why do anything at all if no instructions were received from the user."

**T-R5. Bootstrap cannot bootstrap.** — P1, spec'd (Phase 2). Nothing clones agent-tools; doctor
doesn't know about it → green doctor + exit-2 apply on a clean machine. `apply` without a repo
rig.yaml still mutates HOME from global config (guard hole, `cli.py:820`). `init` previews 142
actions, `apply` executes 121. A cleanroom E2E exists but is off by default and doesn't test
the real gap.

**T-R6. Code quality below the shop's own bar.** — P2, spec'd (Phase 2).
`runner.py` 4,274 lines / 23 handlers; `plan.build` 251 lines; `cmd_status` 188; 25 functions
over the CTO's 80-line rule; no ruff/mypy in rig's own CI (while `3d test` runs all three).

**T-R7. Scope creep.** — P1, Open Decision #3. Five config UIs (TUI, config-web, spec-web,
dashboard, evolve UI) plus tmux driver (1,204 lines), tg-ctl, model-cron, stats — inside a
"bootstrap" tool. 40 untracked `evolve-*.png/md` in the repo root; `rig-cli-wt` empty dir;
`rig-rollout-wave3` directory is not even rig (HyperCalendarBot).

### 3.6.2 rtk[#](#362-rtk) [S019]

**T-K1. Three output-corruption bug classes on installed 0.31 — all fixed upstream in 0.43.** —
P0 (it corrupts what agents read), spec'd (Phase 0). Confirmed by probe on both versions:
(a) `grep -c` grouped-parser mangling; (b) multi-file `cat` rewritten into an invalid rtk read;
(c) BRE→rust-regex semantic drift = silent 0-matches (rc=2) + NUL-byte files skipped. Live
re-observation this session: a `curl` JSON response replaced by a schema summary
(`{message: string[104]...}`) **even when redirected to a file** — anything an agent must parse
needs `rtk proxy` until the upgrade lands.

**T-K2. Not fixed even in 0.43:** `ps aux` COMMAND truncation at ~40 chars (poisons kill
forensics — directly undermines the verify-ownership-before-kill rule) and 1MB matched lines
flooding context. — P1, spec'd (Phase 0: ps exclusion in the hook).

**T-K3. The rtk hook is hand-written, not rig-managed.** — P1, spec'd (Phase 0).
`rtk init -g` artifact plus a manual local `rg` patch; 0.43 itself reports "Hook outdated".
Binary is third-party (brew `rtk-ai/rtk`, Apache-2.0). Plan: brew upgrade + `rtk init -g` +
`rig doctor` Dependency(rtk >= 0.43) via the "tool owns install" pattern; drop the local patch.
For context: rtk claims 867.5M tokens saved (42.7%) over 333k commands — worth keeping, worth
managing properly.

### 3.6.3 serena[#](#363-serena) [S020]

**T-S1. Worktree handling silently redirects reads/edits to the MAIN checkout.** — P0 (silent
wrong-file edits), spec'd (Phase 1). Three confirmed mechanisms: (1) two-pass root resolution
(`cli.py:62-93`): a `.serena/project.yml` at ANY ancestor beats the worktree's `.git` file
(hyperide is safe — its `.serena` is tracked so each worktree has a copy); (2) the MAIN one:
claude-code context sets `single_project: true` → project pinned to session-start cwd →
`EnterWorktree` mid-session does NOT re-point serena → it silently reads and edits the main
checkout (architecture, not bug — and it exactly explains the CTO's suspicion); (3)
`/private/tmp/.serena/project.yml` garbage (from hyperos) hijacks scratchpad cwds; many sessions
start with "No project root found". Fixes: rm the tmp garbage; keep `.serena` tracked (add to
`install-worktree-deps.sh`); start sessions IN the worktree; mechanism (1) is already fixed
upstream — see the subsection below (the earlier "file an upstream issue" plan is superseded).

**T-S2. Four stale `.serena` task-state memories.** — P3, spec'd (Phase 1 cleanup).

#### Serena: worktree defect — upstream status, alternatives evaluated, chosen path (2026-07-02)[#](#serena-worktree-defect--upstream-status-alternatives-evaluated-chosen-path-2026-07-02) [S021]

**Upstream status (verified against oraios/serena, 2026-07-02).** Mechanism (1) of T-S1 — the
two-pass root resolution — is FIXED upstream: PR oraios/serena#1550 (merged 2026-06-07) collapses
the walk into a single pass so the **nearest** project boundary wins (`.serena/project.yml` OR
`.git`, including worktree/submodule pointer files), with a regression test for the exact
worktree-under-Serena-project layout. The fix sits in main's "Unreleased" changelog and is in NO
tagged release (latest v1.5.3 = 2026-05-26; merge = 2026-06-07); the installed v1.3.1.dev0
(verified via `uv tool list` on this machine) predates it. Mechanism (2) — the claude-code context ships `single_project: true`, pinning the
project to session-start cwd, so `EnterWorktree` mid-session silently redirects reads/edits to
the main checkout — is unchanged upstream: main still ships `single_project: true` and upstream
issue #1496 (the same class: "Agent Teams worktree teammates keep Serena rooted to the primary
checkout") is open. Per T-S1 this is architecture (deliberate pinning), not a bug — but it is
the wrong architecture for CLI worktree flows, so we mitigate locally rather than wait. Mechanism (3) (stray `/private/tmp/.serena/project.yml`) is local hygiene,
already Phase 0 item 8. Related open upstream: #1455 (`.serena/cache` index is
worktree-incompatible).

**Consequence:** no upstream PR and no fork are needed for mechanism (1) — a third party landed
exactly the nearest-marker design we had drafted; the interim is a git-main pin until the next
release. The remaining upstream contribution candidate is mechanism (2) (mid-session re-root for
CLI contexts, #1496), which is mitigable locally first via a custom context (5.2.7 item 2).

**Native Claude Code LSP (first-party, shipped ~late Dec 2025, opt-in).** Claude Code now has
native LSP support: plugins carry a `.lsp.json` (official marketplace: `typescript-lsp`, backed
by `typescript-language-server`), and the harness exposes a native `LSP` tool. Documented
operations: diagnostics after each edit, goToDefinition, goToImplementation, findReferences,
hover, documentSymbol, workspaceSymbol, call hierarchy. NO rename, NO symbol-precise edit ops.
Because it is harness-native and resolves the file passed per call, it is structurally immune to
serena's pinned-root worktree class. Not yet functional on this machine (no TS language server
binary installed; only the `clangd-lsp` plugin is enabled).

| Capability | Native `LSP` tool | serena |
| --- | --- | --- |
| definition / references | yes | yes (`find_symbol`, `find_referencing_symbols`) |
| hover / type info | yes | partial |
| file / workspace symbols | yes | yes (`get_symbols_overview`) |
| call hierarchy | yes | no |
| diagnostics | yes (auto after edits) | yes (`get_diagnostics_for_file`) |
| rename across repo | no | yes (`rename_symbol`) |
| symbol-precise edits | no (line/string Edit) | yes (`replace_symbol_body`, `insert_*_symbol`) |
| worktree correctness | per-call path — immune | pinned root until T-S1 fixes land |
| docs/memory surface | none | memories subsystem (unused per doctrine) |

**Alternatives evaluated** (criterion: opt-in LSP precision with NO docs/specs/memory surface —
docs/specs belong to superpowers + haft per the CTO):

- `isaacphi/mcp-language-server` (Go, ~1.6k stars, last push 2026-03-01, self-described beta):
  pure MCP→LSP bridge — definition/references/rename/diagnostics/hover/edit. Fits the criterion
  exactly; maintenance cadence is slow.
- `ktnyt/cclsp` (TypeScript, ~660 stars, pushed 2026-02-22): same shape incl. rename and
  diagnostics; smaller community.
- `Tritlo/lsp-mcp` (~120 stars, no pushes since 2025-07): stale — drop.
- ast-grep MCP (`ast-grep/ast-grep-mcp`, local clone at `~/xp/ast-grep-mcp`, experimental):
  structural AST search/rewrite, no type-aware references or rename — a grep complement, not a
  serena replacement.
- "Native LSP + ast-grep, no extra MCP": covers the whole read side with zero third-party MCP
  dependencies; loses only rename + symbol-precise edits.

**Chosen path (proposed):** (1) upgrade serena to a git-main pin containing #1550, move to the
next tagged release when cut; (2) enable native LSP for TypeScript (official plugin +
rig-managed server binary) as the default read-side code-intel; (3) keep serena opt-in for its
unique write side (rename, symbol-body edits) behind a rig-provisioned custom context that
re-enables `activate_project` (mitigates mechanism (2)); (4) re-evaluate serena's niche after
two weeks of measured native-LSP usage — if rename/symbol-edit demand stays near zero, the niche
vanishes and Decision #6's retire path applies. Work items in 5.2.7.
**Update 2026-07-02 (tg#5670): item (3) is SUPERSEDED** — instead of keeping serena for the
write side, build the public `alex-mextner/morph-cli` CLI (see 3.6.3.2). serena drops to
retire-candidate, kept only through the two-week native-LSP measurement, then unregistered once
that CLI's Phase 1 lands.

#### 3.6.3.1 Part A — rig auto-provisions the TypeScript language server (native-LSP enablement, 2026-07-02)[#](#3631-part-a--rig-auto-provisions-the-typescript-language-server-native-lsp-enablement-2026-07-02) [S022]

**Problem.** Native Claude Code LSP (3.6.3) is opt-in and, once enabled, spawns whatever server a
plugin's `.lsp.json` declares. The official `typescript-lsp` plugin resolves its server from
**PATH** — its config is
`{"lspServers": {"typescript": {"command": "typescript-language-server", "args": ["--stdio"],
"extensionToLanguage": {".ts": "typescript", ".tsx": "typescript", ".cts": "typescript",
".mts": "typescript"}}}}`. This machine has **no** TS server binary (verified:
`which typescript-language-server` → not found; only the `clangd-lsp` plugin is enabled —
irrelevant on a bun/react box). Enabling the plugin without the binary yields a dead LSP: CC
silently gets no diagnostics/references. rig must provision the binary AND enable the plugin,
and — per the self-heal doctrine (tg#5652/tg#4919) — **auto-fix, not merely report**.

**Languages that matter here.** TS/TSX primary (the repo is bun/react). The stock `typescript-lsp`
plugin's `extensionToLanguage` maps only .ts/.tsx/.cts/.mts (the quote above) — so TSX is covered
out of the box. `typescript-language-server` itself also handles .js/.jsx, but native CC LSP only
routes files the plugin maps, so covering .js/.jsx requires **extending `extensionToLanguage`**
(a rig-managed settings step, folded into the plugin pin below) — not a second server.

**What CC actually consumes** (verified 2026-07-02): CC >= 2.0.74 exposes a native `LSP` tool
whose ops are diagnostics, go-to-definition/implementation, find-references, hover,
document/workspace symbols, call hierarchy — **no rename, no code actions, no edits** (that gap
is Part B). The plugin id in `enabledPlugins` is `typescript-lsp@claude-plugins-official`
(mirrors the existing `clangd-lsp@claude-plugins-official: true` at the user's
`settings.json:345`).

**Install mechanism.** `typescript-language-server` is a brew formula (verified: stable 5.3.0,
bottled) on macOS; on Linux it is `npm install -g typescript-language-server typescript` (no
reliable distro package). It wraps `tsserver` and prefers the project-local `typescript` when
present, else its bundled copy — so the binary alone suffices; per-repo type accuracy rides the
repo's own `typescript` dep.

**rig slot (file:line).** Two mechanisms, both with an existing home:

1. **Dependency probe + self-heal install** — `riglib/doctor.py`. Add one entry to
   `DEPENDENCIES` (`doctor.py:35`):
   `Dependency("typescript-language-server", "native CC LSP for TS/TSX", kind="npm",

required=False, pkg={"brew": "typescript-language-server"})`. Two small extensions are needed:
the current` Dependency.kind `is only` "binary" | "python" `(`doctor.py:24-31`) and`install\_command `knows only OS package managers (`detect.py:89-104`). So (a) add`kind="npm"`; (b) in` \_install\_cmd\_for `(`doctor.py:154-169`) branch on npm →`["npm", "install", "-g", "typescript-language-server", "typescript"] `by default, preferring
the brew formula on macOS when present. Data/behavior contract for` kind="npm"`: the npm
command is the default, and` pkg `carries ONLY non-npm overrides (here the macOS brew formula);`pkg `is deliberately not consulted for the npm command itself — document this so the branch
generalizes cleanly to other npm-delivered tools. The probe is` shutil.which(...) `in` diagnose`(`doctor.py:116-135`) — no new probe code. Self-heal:` bootstrap `(`doctor.py:172-193`)
already runs the install command under` assume\_yes`; the doctrine (auto-fix, don't just
report) is satisfied by including this optional dep in the standard` rig doctor `self-heal
pass — a missing LSP binary is exactly the idempotent, safe class self-heal should install,
cf. the "tool owns install" invariant in` tools.py:32-41`.

1. **Plugin enablement (NEW rig capability)** — flip `typescript-lsp` on in
   `settings.json.enabledPlugins`. rig does **not** manage `enabledPlugins` today (grep of
   `riglib/{plan,config_schema,state}.py` → 0 hits). Design: a global-layer config key (e.g.
   `harness.claude.enabledPlugins: ["typescript-lsp@claude-plugins-official"]`) written
   idempotently by the settings.json bridge (the same bridge that owns hooks/permissions), plus
   a doctor check of the installed plugin version against
   `~/.claude/plugins/**/installed_plugins.json`. This is what 5.2.7 item 3 already gestures at.

**Layer.** Per-MACHINE concern (a react dev box), so both the server Dependency and the
`enabledPlugins` pin live in the GLOBAL layer (`~/.config/rig/config.yaml`), exactly like the
`tools:` block — never a committed repo `rig.yaml` (`tools.py:43-45`).

**Net effect.** `rig doctor` on a fresh react box: detects the missing TS server → installs it
(brew/npm) → enables the `typescript-lsp` plugin → native LSP read-side comes alive with zero
manual steps. Part B supplies the write side the native `LSP` tool lacks.

#### 3.6.3.2 Part B — own rename/refactor + structural-search CLI: `alex-mextner/morph-cli` (2026-07-02)[#](#3632-part-b--own-renamerefactor--structural-search-cli-alex-mextnermorph-cli-2026-07-02) [S023]

**Directive (tg#5670, translated).** "ast-grep-mcp is probably stale. MCP is usually not
justified. Let's look at all these tools and build our own with rename/refactor, structural
search, etc. Make it public `alex-mextner/lsp-ast-tools`. Let rig provision it. Put in it what
CC has too, but without provisioning by default." This **supersedes** the "keep serena opt-in
for rename" line in the 3.6.3 chosen path and in Decision #6.

**ast-grep-mcp salvage verdict — start clean, keep only the engine.** The local clone
`~/xp/ast-grep-mcp` is the official `ast-grep/ast-grep-mcp` (NOT Alex's), **stale** (last commit
`33ef227`, 2025-06-23, ~13 months old; pyproject `sg-mcp` 0.1.0) and **read-only** — four tools
(`find_code`, `find_code_by_rule`, `dump_syntax_tree`, `test_match_code_rule`) that merely shell
out to the `ast-grep` CLI (`main.py:84-137`). No rename, no rewrite, no LSP. The MCP wrapper is
disposable (and MCP is the thing we are moving away from); the only asset is the `ast-grep`
binary itself (0.39.4 installed), which our CLI shells to exactly as the MCP did. **Verdict: do
not fork the MCP; reimplement its four read shapes as CLI subcommands over `ast-grep`, discard
the rest.**

**Capability gap analysis.** "yes/no/partial" = has the op. "GAP" = missing from BOTH serena and
native CC → the differentiators worth building.

| Operation | CC native `LSP` | serena | ast-grep | Verdict for our CLI |
| --- | --- | --- | --- | --- |
| rename-symbol (cross-file) | no | yes (`rename_symbol`) | no (no type refs) | **build** — serena-only today; lets us kill the serena dep |
| organize-imports | no | no | partial (structural) | **GAP — build** |
| remove-unused-imports | no | no | no | **GAP — build** |
| remove-unused-exports (knip-adjacent) | no | no | no | **GAP — build (P2)** |
| safe-delete (ref-checked) | no | yes (`safe_delete_symbol`) | no | **build** — enforces dead-code rule; standalone once serena retired |
| move-symbol-across-files | no | no | no | **GAP — build (P2)** |
| batch structural codemod | no | no | yes (`scan --fix`) | **wrap** ast-grep; add type-verify pass |
| type-aware find-and-replace | no | partial (rename only) | no | **GAP — build (P2)** |
| extract-function/variable | no | no | no | GAP — P3 (fiddly) |
| inline-symbol | no | no | no | GAP — P3 (fiddly) |
| change-signature | no | no | no | GAP — P3 (fiddly) |
| find-references (ranked) | partial (unranked) | yes (`find_referencing_symbols`) | no | overlap — include, opt-in; ranking is the extra |
| call-hierarchy | yes | no | no | overlap — native LSP owns it |
| definition / diagnostics | yes | yes | no | overlap — native LSP owns it |

Genuine differentiators (in NEITHER serena nor CC): **organize-imports, remove-unused-imports,
remove-unused-exports, move-symbol-across-files, type-aware find-and-replace**, plus rename +
safe-delete which today exist ONLY behind the serena dep we want to drop.

**Update (tg#5696).** The unused-export *detection* side is now covered by rig-provisioned
**knip** (5.2.8 item 1) as a CI gate; `morph prune-exports` stays as the *fix* op and should
wrap knip's analysis rather than reimplement the repo-wide export graph — which also demotes the
Phase-2 `prune-exports` effort estimate.

**Naming (tg#5678).** Repo/command must end in `-cli`, matching `tg-cli`/`review-cli`/`task-cli`/
`rig-cli` (repo = `<name>-cli`, command = `<name>`). Three candidates:

| Candidate (repo / cmd) | Rationale | Con |
| --- | --- | --- |
| `chisel-cli` / `chisel` | metaphor for precise, structural carving of code; short, memorable, no PATH collision; pairs with a sharp native core | not literally self-describing |
| **`morph-cli` / `morph`** (CHOSEN) | evokes AST morphing (and the ts-morph lineage) | couples the name to a library we are actually dropping; slightly generic |
| `refac-cli` / `refac` | maximally literal — an agent knows what it does from the name (lowest cognitive load for a Sonnet shop) | least distinctive; "refac" reads as a typo of "refactor" |

`astk-cli` was considered and dropped (awkward to pronounce/type). The retro's draft
recommendation was `chisel-cli`; **the CTO picked `morph-cli` (tg#5692, translated: "probably
morph")** — repo `alex-mextner/morph-cli`, command `morph`; the rest of this spec uses `morph`.
Collision note: a commercial "Morph" fast-apply code-edit tool exists in the same space — run a
final name-availability check before creating the public repo.

**Architecture (FIXED by tg#5670 + tg#5678 — recorded, not re-litigated).** A standalone **CLI**,
public repo `alex-mextner/morph-cli` (does not exist yet — `gh repo view` → 404; scaffolding is
follow-on task #19). Not an MCP (CTO: "MCP usually not justified"): a CLI has zero idle context
cost, works across ALL harnesses (codex/opencode have no native LSP), and is invoked over Bash
like `tg`/`review`/`task`. Per tg#5678 it is a **two-layer** tool: a **thin Python CLI shell**
(argument parsing, self-registering command modules, dynamic `--version`, structured exit codes,
compact-output formatting, token accounting, ecosystem glue) over a **native core in Rust**
(all AST/LSP work). The shell matches the fleet's dominant CLI language (`task`/`rig`/`3d`/
`research`/`draw` are all Python) and reuses their `install.sh` + `install-skill` + `rig.yaml`
conventions; the core delivers the "very fast" requirement. This shape is now codified as fleet
policy, not a one-off choice: **new CLIs are Python-only — no TypeScript** (tg#5696, 5.2.8),
with a native (Rust) core behind the Python shell where performance demands it (tg#5678).

**Engine.** Because the core is native (Rust/Go), it **cannot** use `ts-morph` (a Node/JS
library) — so the type-aware semantics come from driving **`typescript-language-server`** (the
same server Part A provisions for CC's read side) over LSP: `textDocument/rename`,
`source.organizeImports` / `source.removeUnused` code actions, `textDocument/references` (for
safe-delete's ref check), applying the returned `WorkspaceEdit`s. Structural ops embed/shell
**`ast-grep`** (itself Rust — ecosystem-aligned). So the tool is **TS/JS-family for type-aware
refactors** (100% of a bun/react shop) and **all-language for structural search**. **Rust over
Go for the core:** it aligns with `rtk` (Rust) and `ast-grep` (Rust), yields a single fast static
binary, and has mature LSP-client crates (`async-lsp`/`lsp-types`). Go would ship faster but
loses the ecosystem alignment and the ast-grep reuse; recorded as the fallback if build velocity
dominates. **`ts-morph` rejected** (was the fewest-lines path) because it needs a Node runtime,
cannot live in the native core, and adds cold-start latency — the exact friction we are removing.

**Performance (tg#5678: "very fast").** The real LSP latency is server *startup* (tsserver
indexes a large project in 1-5s), which is exactly the friction that killed MCP-tool adoption
(sverklo: 19 calls). Mitigation: the Rust core runs a **warm per-project daemon** (a persistent
LSP session reused across invocations, addressed over a unix socket); the first call pays
startup, subsequent calls are warm (single-digit ms). Structural (`ast-grep`) ops need no daemon
— instant. Boundary: the Python shell imports only stdlib (lazy-heavy-imports), execs the
`morph-core` binary or talks to its socket, and formats the JSON it returns; **all** parsing,
LSP I/O, edit application, and ast-grep integration live in Rust. A Bash-callable CLI + warm
native core removes both the context tax (vs MCP) and the latency (vs a cold LSP client).

**Command surface (proposed).**

- Type-aware (LSP core), the differentiators — rig-provisioned / foregrounded:
  `rename <file>:<symbol> <new>`, `organize-imports <path...>`, `prune-imports <path...>`,
  `prune-exports [--dry-run] <path...>`, `safe-delete <file>:<symbol>` (ref-check first; refuse
  and list referrers if used elsewhere — enforces the dead-code-investigation rule
  mechanically), `move <file>:<symbol> --to <dest>`, `codemod --rule <yaml|pattern>

[--rewrite ...]` (ast-grep rewrite + optional type-verify via the LSP core).

- Structural (ast-grep): `search <pattern|--rule yaml> [path]`, `ast/dump <file>` (CST/AST/
  pattern debug) — reimplements ast-grep-mcp's read shapes.
- Overlap-with-CC (present but NOT provisioned by default, per tg#5670): `refs

<file>:<symbol> [--ranked]`,` defs`,` callers`. Interpretation of "without provisioning by
default": the CLI **ships** these for cross-harness parity (codex/opencode have no native
LSP), but on Claude Code they duplicate the native` LSP` tool, so rig's skill blurb steers
reads to native LSP and foregrounds only the differentiator commands. One binary, all
commands; provisioning/steering favors the write side + native-LSP reads.

**Token economy (headline value, tg#5678).** For a Sonnet shop the tool's primary payoff is
tokens saved, not cleverness. The naive agent refactor reads whole files into context, edits,
then re-reads to verify — a cross-file rename touching 20 files pulls 20 full files into context.
`morph` never loads files into the model: it performs the op in the native core and emits
**compact structured output** — e.g. `renamed foo → bar: 14 edits / 6 files` plus only the
changed hunks (± few context lines), never whole files; `search`/`refs` return `file:line` + the
matched node only, capped at N results with a `--drill <id>` to expand exactly one; mutating
commands default to a summarized diff. Every command is capped-output-with-drill-down by default
(the rtk philosophy applied to refactors). Accounting is first-class and rtk-style: the core
knows which files it touched and their sizes, so it estimates `tokens_saved = naive_read_cost −
actual_output_cost` per invocation, appends to `~/.cache/morph/gain.jsonl`, and exposes
`morph gain` (cumulative saved) + `morph gain --history` (per-command) — the same surface as
`rtk gain`. Token savings is therefore measurable, not asserted.

**Ecosystem integration (tg#5678).** Named integration points:

- **rig** — provisioned via the `tools.items` block (below); runtime deps (`typescript-language-

server `from Part A,` ast-grep`) via` rig doctor`; the Rust core binary built/fetched by the
repo's own` install.sh`.

- **rtk** — `morph` is its **own** gain-tracker (independent `morph gain`), NOT coupled to
  rtk's internal ledger (rtk is third-party Apache-2.0; binding to its ledger format is fragile).
  An optional `morph gain --export-rtk` can later feed rtk's total for one unified number if the
  CTO wants it. `morph` is a peer of rtk, not an rtk-rewrite target (it is already compact by
  design); the rtk hook must **pass `morph` through unmangled** (cf. the `ps` exclusion, T-K2).
- **review** — mutating commands accept `--review` to pipe the resulting diff to `review diff`
  before the agent commits (the review-always doctrine); `morph` emits a clean, git-stageable
  diff that `review` consumes directly.
- **task** — mutating commands accept `--ticket HYP-NNN`, stamped into the op-log, feeding the
  require-ticket discipline (a refactor is a tracked change).
- **agent-hooks / skills** — `safe-delete` mechanically enforces the dead-code-investigation rule
  (refuses if refs exist outside the declaration and prints them); a PreToolUse hook can steer
  bulk symbol deletion toward `morph safe-delete`, and `organize-imports`/`prune-imports` can
  wire into the pre-commit gate. `install-skill` advertises `morph` in the harness skill list so
  agents discover it (same as `tg`/`review`/`task`).

**MVP scope + phases.**

- **Phase 1 (MVP):** `rename`, `organize-imports`, `prune-imports`, `safe-delete`, `search`,
  `ast/dump`. Highest value, simplest via the LSP core + ast-grep. Effort ~3-4 focused days incl.
  tests, `install.sh`, `rig.yaml`, CI — most of it is the task-cli scaffolding, not the logic
  (each command is a thin wrapper over one LSP-core / ast-grep call).
- **Phase 2:** `prune-exports` (repo-wide unused-export graph), `move`, `codemod` (+ type-verify),
  `refs --ranked`. Effort ~3-5 days (prune-exports is the only non-trivial one).
- **Phase 3 (only if demand shows):** `extract-*`, `inline`, `change-signature` (the fiddly
  refactors) + the overlap read commands for non-CC harnesses.

**How rig provisions it (zero new distribution code).** Add to the GLOBAL
`~/.config/rig/config.yaml` `tools.items` block simply `morph: {}` — the tool name `morph`
makes `resolve_tool_specs` default the repo to `~/xp/morph-cli` (`tools.py:144`,
`~/xp/<name>-cli`), which is exactly the `-cli` checkout, so no explicit `repo:` is needed.
`resolve_tool_specs` (`tools.py:121-147`) turns it into a `ToolSpec`; `_do_provision_tools` runs
its `install.sh` (symlink into `~/.local/bin` + `install-skill`); `tool_status`
(`tools.py:150-163`) makes it idempotent. **No rig code changes** for distribution — it drops
into the existing tools area. Runtime deps go in `rig doctor`: add `ast-grep` (brew formula) as a
`Dependency` alongside the Part A `typescript-language-server` entry; the Rust `morph-core`
binary is built or fetched by the repo's own `install.sh` (prebuilt release binary preferred over
requiring `cargo` on every box), not a machine dep.

**Tradeoff vs. keeping fixed-serena (the retired Decision #6 line).** Keeping serena for rename =
**0 build cost** but an ongoing third-party Python MCP, always-on context tax, the architectural
worktree-pinned-root class (mitigated, not gone), near-zero measured usage (~5 code-intel MCP
calls in 2 weeks vs 42.5k Bash), and slow upstream cadence. Building the CLI = **~1-2 weeks** of
net-new code we own, but it (a) kills a third-party dep, (b) works on every harness, (c) has no
worktree defect (per-invocation cwd), (d) is provisioned for free by rig's tools area, and
(e) delivers the five differentiator ops serena ALSO lacks. tg#5670 makes the call: **build.**

**Open question for the CTO (only if the build-vs-keep call is close for you).** If in practice
you only ever want **rename** and nothing else, the honest cheaper path is to keep a pinned
serena and skip the build — the two-week native-LSP measurement (Decision #6) would settle it.
But the moment organize-imports / prune-unused / prune-exports / move / safe-delete matter (all
missing from serena too), the own CLI wins decisively. Question: **is rename-only sufficient
(→ keep pinned serena), or is the broader missing set wanted (→ build morph-cli)?** tg#5670
reads as the latter; proceeding on that unless you say otherwise.

### 3.6.4 sverklo[#](#364-sverklo) [S024]

**T-V1. 19 all-time calls, 100% health pings; 8 versions stale (0.21 vs 0.29 upstream);
third-party; every advertised capability overlaps an incumbent.** — P1, Open Decision #1.
Usage baseline for contrast (hyperide, 2 weeks): Bash 42,519 / Read 13,517 / Edit 5,173 vs ~5
code-intel MCP calls total. Discoverability is NOT the explanation — the doctrine has been live
since Jun 15; only 9/204 hyperide sessions (4.4%) invoked ANY third-party tool. The feasibility
study says integrate-thin trial; the brainstorm panel says replace with a 30-line `xrg` wrapper.
Both positions in section 7 and Open Decision #1.

### 3.6.5 haft[#](#365-haft) [S025]

**T-H1. Massive spawn-to-use ratio; all substantive artifacts from one hand-driven CTO day.** —
P1, Open Decision #1. Third-party Go tool (m0n0x41d/haft), local v7.0.0 (2026-05-12) vs upstream
v8.1.0 (breaking). All substantive `.haft` activity = ONE DAY (2026-05-13: 4 problems, 5
decisions, 2 plans with a dead `repo_ref` to hyper-canvas-draft). Advisory-only: zero hooks/CI;
path policies match 0 files (boilerplate `internal/`+`desktop/` globs). Parallel universes:
`docs/specs` 56 living files vs `.haft` dead, 0 cross-references. Agents delete haft notes as
debris (commit 238988c3, 2026-07-01, a note re HYP-837). 12 of 13 `h-*` slash commands were
never typed. **Count discrepancy, flagged honestly:** the haft audit counted 697 server spawns /
87 tool calls; the feasibility probe counted 5,205 spawns / 87 calls (60:1). The two probes used
different windows/methods; the exact spawn count is unverified here, but the conclusion
(spawn-heavy, call-light, one productive day) is identical under either number.

### 3.6.6 review-cli / review pool[#](#366-review-cli--review-pool) [S026]

**T-P1. Pool seats die on third-party credits and timeouts, silently degrading quorums.** — P1,
spec'd (Phase 1: pool health surfacing). During this retrospective: 4 commandcode seats
("insufficient credits": Kimi-K2.7, deepseek-v4-pro, Qwen3.7-Max, commandcode's zai-org/GLM-5.2)

- the opencode seat `oc:zai/glm-5.2` timing out at 240s — five distinct seats (the two GLM-5.2
  entries are the same model behind two different providers, each its own seat) — in BOTH quorums
  and every brainstorm round. Consensus built on 3/8 panelists without any loud warning that the
  panel had shrunk. review-cli should fail loudly / annotate verdict
  confidence when seats die; credits monitoring belongs in `rig doctor` or a cron.

**T-P2. The review-cli repo itself failed the July-1 KPI audit.** — P1, open (owner: the
tool-repo hygiene sweep, 5.1.9). 37 uncommitted files on `main`; tests not running. Same audit:
tg-cli 8 uncommitted + 4 failing tests; rig-cli CI red. See 4.5.

### 3.6.7 tg[#](#367-tg) [S027]

**T-T1. Forum-topics feature chased for 2 weeks, activation unproven, CTO frustration on record
(tg#5468).** — P2, open. Also: tg vendors its own hooks dispatcher (duplication with the
agent-tools dispatcher). Core tool verdict remains keep/double-down — it is the single
highest-leverage communication channel.

**T-T2. Roadmap ordered 2026-07-02 (tg#5698/tg#5699), implemented in a separate track — recorded
here, tracked in section 10, NOT gated on this spec:** (a) harness session-limit/error → tg
notification with an inline auto-continue-at-reset-time button; (b) a `/tasks [agent] [status]`
command rendering a full-lifecycle table per task (tg message → acceptance criteria → CI →
review cycles); (c) a reaction lifecycle on inbound messages — an hourglass reaction when the
agent is stopped on limits, an eyes reaction on resume, a check-mark reaction once the task is
filed / the answer is given.

### 3.6.8 agent-browser[#](#368-agent-browser) [S028]

**T-B1. `agent-browser` screenshot hangs on this machine.** — P1, open (new; from the HYP-857
handoff). Accessibility snapshots worked as proof fallback. Needs a repro + upstream issue;
meanwhile screenshots via Playwright per the standing recipe. Also: brew upgrade available
0.27 → 0.31.

### 3.6.9 Ecosystem-level[#](#369-ecosystem-level) [S029]

**T-E1. Three memory systems coexist** (MEMORY.md, serena memories, sverklo memories) with no
designated system of record. — P1, spec'd (Phase 1: MEMORY.md is the system of record; serena
memories for serena's own onboarding cache only; sverklo memories OFF).

**T-E2. Duplication census.** — P2, Open Decision #3 input. Two rig-config implementations (one
discarded); `rig stats` vs `mcp-skill-usage` (#148); five bespoke web UIs in a month; tg's
vendored hooks dispatcher; research-cli (4 commits, skeleton) overlapping `review just-ask`;
claude-p (fork) displaced by `claude --print` (#77).

**T-E3. Orphaned work.** — P2, spec'd (Phase 0 sweep). `agent-tools-wt-129` has STAGED
uncommitted modifications (looks like a dep-review gate follow-up — investigate and resume, not
delete, per the dead-worktree rule); dead `review --mcp` entry in global mcp.json.

**T-E4. Dead skill pointer.** — P3, spec'd (Phase 1). The `semantic-code-search` skill points at
sverklo; `2026-06-15-thirdparty-tool-triage.md` has an unchecked completion checkbox.

### 3.6.10 Shared-lib consolidation (agent-tools/lib): built-but-unadopted[#](#3610-shared-lib-consolidation-agent-toolslib-built-but-unadopted) [S030]

**Premise inversion (corrects the loose "lib is near-empty" read).** `agent-tools/lib` is not
near-empty — it holds **14 tested Python modules** from a **June 16–18 extraction sprint** that
built the surface and then stopped. The adoption step and the distribution decision never
happened, so the sprint output is stranded: **9 of 14 are ghosts** (advertise, config, registry,
retry, help, completion, tmux\_inject, gantt, and daemon as transitive-only), **1 is broken**
(`agenttools_log` — `task` calls a `.log()` method that does not exist, so it silently falls back
to a local sink; a `# type: ignore` masked it), and **1 is single-adopter drift** (`errors` — an
EXIT-7 collision: lib `NETWORK=7` vs rig `REPO_CORRUPT=7`). Cleanly adopted: only **service,
providers, cc\_hook\_bridge**. This is "orphaned by migration" at ecosystem scale, so the dead-code
rule governs: **wire it, don't delete it.** — P1.

**T-L1. Root cause — no blessed dependency path.** `lib` is not on PyPI, so there is no sanctioned
way to depend on it, and **three competing consumption models** grew up instead: PyPI-declared
(nobody), vendor + SHA-drift-guard (research-cli only), and lazy `sys.path` (review / rig / task).
Distribution — not the code — is why adoption stalled. — P1, Open Decision #7.

**T-L2. Module × adoption census.**

| Module | State | Adopter / would-be | Note |
| --- | --- | --- | --- |
| service | adopted | rig, task | clean |
| providers | adopted | review (partial) | origin stack still lives in review — see T-L3.5 |
| cc\_hook\_bridge | adopted | task, rig | clean |
| errors | drift | rig | EXIT-7 collision (`NETWORK=7` vs `REPO_CORRUPT=7`) |
| daemon | ghost + duplicated | task-cli (reimplemented) | ~898-LOC parallel daemon written 9 days AFTER the lib module existed — T-L2.1 |
| agenttools\_log | broken | task (intended) | `.log()` missing → local sink; `# type: ignore` hid it |
| advertise | ghost | 6 tools reimplement | 447-LOC module vs ~1300 Py + 319 TS reimpl |
| config | ghost | review + others | review 6 sites ignore `$XDG_CONFIG_HOME` (bug); `deep_merge` ×3 |
| registry | ghost | trust-kernel consumers | wire contract already frozen cross-lang |
| retry | ghost | transient-classifier copies | verbatim copy, drift flagged in-code |
| help | ghost | — | no consumer |
| completion | ghost | — | no consumer |
| tmux\_inject | ghost | task-cli | should be wired |
| gantt | ghost | (TS-only consumer) | Python module for a TS consumer — see T-L4 |

**T-L2.1. The daemon duplication is the smoking gun.** task-cli duplicated `agenttools_daemon`
wholesale — a **~898-line** parallel daemon implementation written **9 days AFTER** the lib
module already existed. This is not divergent legacy code that predates the extraction; it is
direct proof that without a blessed dependency path the lib is invisible at the moment of need —
the author reimplemented instead of importing. (The two copies now embody different designs —
lib's supervisor model vs task-cli's fail-soft loop — reconciled in the work plan below.) — P1.

**T-L3. Consolidation map — top-5 extractions (leverage / cost / priority).**

1. **Dynamic `--version` resolver** (S, **P1**) — fixes task's unscoped `^version=` bug, the
   draw / research hardcoded-version regressions, and review (which has no `--version` at all).
2. **`agenttools_config` adoption + XDG** (S–M, **P1**) — review has 6 sites that ignore
   `$XDG_CONFIG_HOME` (a bug); `deep_merge` is triplicated.
3. **`agenttools_registry` trust-kernel** (M, **P1**) — the wire contract is already frozen
   cross-language; security-relevant.
4. **`advertise` install-skill** (M–L, **P2**) — ~1300 Py + 319 TS reimplement a 447-LOC
   extracted module across 6 tools; spec Phase-1 item.
5. **T-L3.5 — converge review's ~3000-LOC provider stack onto `agenttools_providers`** (L,
   **P2**) — review is the origin repo still running the very stack `lib` was carved from.

Also small (S): transient-classifier (verbatim copy, in-code drift note), `notify()` tg shell-out
(2 copies), tailscale host-discovery (byte-identical, security), and fixing `agenttools_log`.

**T-L4. Cross-language strategy — twin only the seams that have a TS consumer.** The
agent-tools `docs/specs/2026-06-15-shared-lib-architecture.md` spec *prescribed* a contract-first layout —
`lib/py` + `lib/ts` + `lib/contracts` twins with conformance vectors. The sprint shipped **flat,
Python-only**, no skeleton, so tg hand-syncs by comment instead: `autostart.ts:12` ("mirrors
agenttools\_service"), a hand-ported `install.ts`, a TS trust gate, `retry.ts`. Plan: build the
prescribed skeleton, but twin **only the ~5 seams tg actually uses**, backed by conformance
fixtures; do not mirror all 14. `gantt` is the cautionary tale — a Python module written for a
TS-only consumer, which is exactly why it is a ghost. Per the CLI-language policy (tg#5696,
5.2.8): the `lib/ts` twin exists solely for the already-existing TS consumer (tg-cli); it is not
a license for new TypeScript CLIs. — P2.

**T-L5. Gap — token-counting exists nowhere.** No shared token accounting exists anywhere in the
ecosystem; the new morph-cli (3.6.3.2) will build `agenttools_tokens` from scratch. It should
land in `lib` as the shared origin, not as a morph-private module. — P2.

**T-L6. Governance — nothing stops re-duplication today.**

- **CI grep-gate**: fail when a CLI (re)defines a lib-owned symbol; rig-provisioned so every repo
  inherits it.
- **rig drift**: extend `drift.py` with `_check_lib_adoption` to flag copies of lib-owned code.
- **Fix the READMEs that lie**: several describe adoption in the present tense as if it were done.
- **Solve distribution** (the actual unblock, Open Decision #7): pick ONE model and migrate every
  consumer onto it.

**Strategy (CTO directive, tg#5686, translated: "generalize everything reasonably possible into
lib").** Three moves, in order: (1) give `lib` a **blessed dependency path** — a declared `uv`
path/git dependency on the global checkout, or publish (Open Decision #7); (2) **adopt it in ALL
CLIs** — the T-L2 census is the checklist; (3) **delete the duplicates** only after adoption
lands (dead-code rule: wire first, then remove the copies). The directive also sets the scope
bar going in: generalization into `lib` is the *default* for any logic used by 2+ tools, not an
opportunistic exception.

**Work plan (tickets to file under HYP-858).**

- *Per-unit extraction*: version-resolver; config + XDG; registry trust-kernel; advertise
  install-skill; review provider-stack convergence; + a small batch (transient-classifier,
  `notify()`, tailscale discovery, fix `agenttools_log`).
- *Ghost decision-tickets* (dead-code rule — investigate then port-or-drop, never silent delete):
  gantt (port-or-drop, TS consumer); completion (no consumer — complete or drop); tmux\_inject
  (wire into task-cli); daemon vs task-cli daemon (reconcile the supervisor model vs the fail-soft
  loop).
- *Spec-level decision*: `errors` EXIT-7 contract reconciliation — renumber one owner and freeze
  the exit-code table as a single source (Open Decision input).
- *New origin*: `agenttools_tokens` (token-counting), origin = morph-cli, home = `lib`.

## 3.7 Process / tickets / PRs[#](#37-process--tickets--prs) [S031]

**PR-1. Ticket closing discipline is broken while creation is good.** — P1, spec'd (Phase 1).
HYP-758 and HYP-807 closed with unchecked acceptance boxes; ZERO images in 8 sampled closed
tickets (the visual-proof rule scores 2/5); broken link in HYP-851 to a nonexistent repo. Fresh
creations HYP-846..857 score user-impact 5/5, criteria 4/5 — the `task` CLI gates work at
creation and are not enforced at close.

**PR-2. PR-ticket linkage collapses exactly in the tooling repos.** — P1, spec'd (Phase 1).
Product 72%, e2e 57%, tool repos 15–24%. The "always file tickets" rule (memory, tg#4866) is
honored where hooks enforce it and ignored where they don't — one more data point for 4.1.
Fix: flip pr-title-lint from warn to block, delivered via rig.

**PR-3. Backlog demographics.** — P1, spec'd (Phase 1: Linear triage). 307 open HYP tickets, 76%
stale >1 week, net +29/week; 234 need triage; "In Progress" zombies since February (HYP-162
billing spec, HYP-121 ToS). Median time-to-merge is measured in minutes (self-ship culture), so
an open PR >24h is de-facto abandoned — 34 such in hyper-saas.

**PR-4. rig.yaml inconsistency across repos.** — P2, spec'd (Phase 2).
ext-test-projects has NO rig.yaml (the only repo without one); two generations of style coexist
(full-fat committed vs minimal+global); review/task/research-cli declare `ci.enabled: false`
while hand-made workflows sit on disk.

**PR-5. tg reporting anti-patterns persist despite a standing memory rule.** — P2, spec'd
(Phase 1). Observed in transcripts: reports without status/waiting-for/trigger, duplicate or
boring screenshots, no video, 9 English-language reports (the rule says Russian), wrong-message
answers after compaction. The format rule exists (memory, tg#5262) — it is prose, so it decays;
see 5.2.5 for the mechanization proposal.

## 3.8 Docs and memory[#](#38-docs-and-memory) [S032]

**D-1. False claims inside AGENTS.md** (the rulebook lies about its own enforcement):
format-on-write active (`AGENTS.md:192` — dead hook), branch protection enabled
(`AGENTS.md:1292-1294` — 403 on free plan), `AGENTS.md:181` claims `.claude/settings.local.json`
is "tracked in git — commit changes to it" while `.gitignore:44` ignores it and `git ls-files`
confirms it is untracked (verified 2026-07-01; found by the review pass on this spec), and
`AGENTS.md:444` itself documents `REQUIRE_TICKET_SKIP` as a how-to — the rulebook teaches its
own bypass. — P1, spec'd (Phase 1).

**D-2. MEMORY.md index bloat** (P-2) plus entries that duplicate whole topic files inline. —
P0, spec'd (Phase 0 shrink).

**D-3. Spec sprawl without an index:** 56 files in `docs/specs`; the master styles spec is
authoritative (per tg#4969) but nothing marks superseded specs. — P2, spec'd (Phase 1 doc-pass).

## 3.9 Working-diff findings (canvas-preview fork in `client/App.tsx`)[#](#39-working-diff-findings-canvas-preview-fork-in-clientapptsx) [S033]

Surfaced by the brainstorm panel, which was deliberately grounded in the live working diff.
These are P1 pre-merge gates for the current feature work, not general debt:

**W-1.** Blind `.catch(() => {})` around the dynamic preview import — swallows mount failures,
kills the agent self-correction signal. Replace with log + fallback render of `<App/>`.

**W-2.** Route detection via `pathname.includes("test-preview")` — over-broad substring
predicate; needs an exact route match plus validation of the `?component=` parameter.

**W-3.** The `@hyperide-managed` generated region has **no end delimiter** — breaks any future
manifest/lint tooling over managed regions; add START/END markers as a build-time contract.

**W-4.** `import("./__canvas_preview__")` is a **build-time** contract — on a clean checkout the
module may not exist and the build fails before any runtime `.catch` can help. Needs a typed
`CanvasPreviewModule` interface + a contract test + a dev-only entrypoint consideration.

**W-5.** `.claude/scheduled_tasks.lock` tracked (C-5) — same diff, same hygiene bar.
Plus a short `docs/decisions/canvas-preview.md` capturing the "why" (see 5.3.4 native-lite).

---

# 4. Root-cause synthesis[#](#4-root-cause-synthesis) [S034]

Five mechanisms explain nearly every item in section 3. They interlock: fixing any one of them
in isolation has failed before (the transcripts show every one of these being "fixed" by prose
at least once).

## 4.1 Rules-as-prose vs rules-as-mechanism[#](#41-rules-as-prose-vs-rules-as-mechanism) [S035]

The single strongest empirical result of this retrospective:

> **Every rule that is enforced by a hook stopped recurring. Every rule that lives as prose
> regressed, on a timescale of roughly one context compaction.**

Evidence pairs (rule → outcome):

| Rule | Form | Outcome (16-day window) |
| --- | --- | --- |
| No `bun install` on the extension | PreToolUse hook | 38 catches, **0 landed** |
| No raw PR merge | repo hook (since #597) | 19 blocks, 0 landed (but see G-4 bypass) |
| No `screencapture` | mechanized earlier | 0 recurrences |
| Review before commit | hook + `REVIEW_SKIP` hatch | 135 skips + 41 marker forgeries |
| Ticket before commit | hook + `REQUIRE_TICKET_SKIP` hatch | 99 skips |
| tg report format (status/waiting/trigger) | prose + memory | violated repeatedly (PR-5) |
| Functions ≤80 lines | prose (global CLAUDE.md) | 25 violations in rig-cli alone |
| Always file tickets in tool repos | prose | linkage 15–24% vs 72% where hooked |
| English-only repo docs | prose | held (low-frequency rule; small sample) |

The nuance that matters: **hatched hooks (with self-service `*_SKIP`) sit in between** — they
stop accidental violations but convert deliberate ones into an unaudited bypass economy (G-2,
G-8). So the doctrine has three tiers, not two: prose (decays), hatched-hook (works if the hatch
is audited), hard-hook (works). This doctrine is not new — it is already written in agent-tools
`ROADMAP.md:1692`: "prose regresses; every rule = PreToolUse hook, rig-provisioned", with
satisfiable/tiered/scoped/honest-escape criteria. The retrospective's contribution is proving it
quantitatively and finding the places where the doctrine itself is not yet applied to the
doctrine's own infrastructure (dead hooks, unsatisfiable gates, unaudited hatches).

Corollary the brainstorm panel sharpened: the strongest mechanism of all is not even a hook —
it is an **executable test + a type**. A decision encoded as a contract test cannot silently
drift; a decision encoded as prose (in AGENTS.md, MEMORY.md, or a haft note — the surface does
not matter) inherits the "refresh triggers never wired" disease. Promote decisions upward:
prose → hook → test/type, as far up as the decision allows.

## 4.2 Context economics: what actually reaches whom[#](#42-context-economics-what-actually-reaches-whom) [S036]

The instruction system was designed as if context were free and delivery were guaranteed.
Neither is true.

Actual delivery table (verified by the provisioning audit):

| Artifact | Main agent | Subagents | codex/opencode |
| --- | --- | --- | --- |
| global `~/.claude/CLAUDE.md` (user prefs) | YES | YES (claudeMd block) | NO |
| project `CLAUDE.md` (3 lines, pointer) | YES | YES | partial |
| **`AGENTS.md` (98.7KB, the actual rulebook)** | **NO** | **NO** | NO |
| `MEMORY.md` | truncated | truncated | NO |
| Skills (`~/.claude/skills`) | 64 linked | 64 linked | different set (94 copies) |
| Hook enforcement | YES | YES | **NO agent layer** |
| Dispatch prompt contract | n/a | **only if orchestrator remembers** (38%) | n/a |

Three consequences:

1. **The 98.7KB rulebook is optimized for a reader that does not exist.** No harness loads it.
   Humans don't re-read it either (it lies about its own enforcement, D-1, and nobody noticed).
2. **Subagents live and die by their dispatch prompt.** The 38%-provisioned statistic (G-11)
   means the majority of guard "violations" were structurally guaranteed before the subagent
   said a word. Provisioning is a mechanism problem (inject a contract via hook), not a
   diligence problem.
3. **Budget arithmetic**: even if AGENTS.md were imported, 98.7KB of prose in every context
   would crowd out working memory and still decay on compaction. The fix is not "import the
   whole thing" but decomposition: a small always-loaded core (~30KB ceiling), path-scoped
   rules loaded on relevance, culture moved to skills (loaded on trigger), and everything
   enforcement-shaped moved out of prose entirely (4.1).

## 4.3 Measurement before goal[#](#43-measurement-before-goal) [S037]

Three separate programs made the same mistake: declaring an outcome goal before building the
instrument that measures it.

- **E2E**: goal "all green" for 3.5 months; instrument (an authoritative run) never existed.
  Result: 22 unmerged fix PRs (nothing proves them), scope +13 fixtures against a broken meter,
  and plausible-but-wrong triage risk everywhere. (Section 5.2.1 is the corrective program.)
- **Tool adoption**: goal "agents use code intelligence"; instrument (usage telemetry per tool)
  was never read until this retrospective (19 calls / 100% pings went unnoticed for weeks).
- **Discipline**: goal "agents follow the rules"; instrument (bypass counts, hook hit rates)
  existed in raw transcripts but was never aggregated — the ~300-bypass economy was invisible
  until counted.

The pattern fix is the same in all three: define the unit of authoritative measurement first
(one nightly verdict; one usage dashboard; one overrides.log), stabilize it, and only then set
outcome goals against it. The e2e quorum formalized this as "one night = one authoritative
verdict" — the KPI this spec generalizes: **every program gets a meter before it gets a target.**

## 4.4 Incumbent-vs-new-tool niche collisions[#](#44-incumbent-vs-new-tool-niche-collisions) [S038]

Every under-used tool in section 3.6 lost a niche fight to an incumbent that was already
enforced or already familiar:

| Newcomer | Incumbent that already owned the niche | Result |
| --- | --- | --- |
| haft decisions/problems | `docs/specs` (56 files) + review brainstorm + Linear | 87 calls ever; agents delete its notes |
| haft h-reason skill | superpowers:brainstorming + review | never dispatched |
| sverklo search/memory | rg/grep + serena + MEMORY.md | 19 calls, all pings |
| sverklo review\_diff | review-cli | never used |
| research-cli | review just-ask | 4 commits, skeleton |
| claude-p | `claude --print` (native caught up) | displaced |
| rig stats | mcp-skill-usage | duplicate (#148) |

The lesson is procedural, not about any single tool: **a new tool must name the incumbent it
replaces and either delete it or lose to it.** The brainstorm panel put it as a rule: "no new
discipline surface except by REPLACING a noisier one." This becomes a standing adoption gate in
5.3.4 (native-lite decision discipline) and should be applied to every future tool proposal.

Counter-example proving the rule: tg succeeded precisely because it had no incumbent (there was
no other channel to the CTO's phone) and was wired into the workflow (hooks inject replies).

## 4.5 The July-1 KPI audit pattern: tooling exempted itself from its own rules[#](#45-the-july-1-kpi-audit-pattern-tooling-exempted-itself-from-its-own-rules) [S039]

The CTO's spot audit (2026-07-01, translated: "now that I have looked, everything is very bad")
found the tool repos violating the rules the tools exist to enforce:

- review-cli: 37 uncommitted files on `main`, tests not running — the tool that gates other
  repos' commits doesn't gate its own.
- tg-cli: 8 uncommitted files, 4 failing tests.
- rig-cli: CI red; no ruff/mypy on itself; 25 functions over the 80-line rule; 40 untracked
  artifacts in the repo root.
- Tool repos' PR-ticket linkage 15–24% vs product 72% (PR-2).
- ext-test-projects: the only repo with no rig.yaml at all (PR-4).

Mechanism: the enforcement stack (hooks, lefthook, CI gates, rig) was rolled out repo-by-repo
toward the product, and the tool repos — where 49% of the month's throughput went — were left
running on trust. Trust produced exactly what it produces. The fix is boring and structural:
tool repos get the same rig-provisioned gate set as product repos (5.1.9, 5.2.4), and `rig
status --all-repos` becomes the drift meter that would have caught this in June.

---

# 5. Strategy (phased)[#](#5-strategy-phased) [S040]

## 5.0 Mapping to the CTO's stated goal priorities[#](#50-mapping-to-the-ctos-stated-goal-priorities) [S041]

The CTO's standing priorities: (1) e2e 100% green and real stabilization; (2) the e2e portal;
(3) perfect screenshots; (4) rig tooling perfect, including evolve / spec-web / review-state-web
/ rig-tui. This strategy serves all four but re-sequences them through the measurement doctrine
(4.3):

- **"e2e 100% green"** is re-framed per the unanimous quorum: 100%-green is the standing state
  of the curated **Tier 0 core** (a real, enforced gate) plus a **Tier 1 nightly authoritative
  verdict** over everything, rather than a binary demand on 2,000+ tests whose binomial
  arithmetic guarantees perpetual red (0.999^2000 ≈ 0.135). This is a strengthening, not a
  retreat: today NOTHING is reliably green because nothing is reliably measured.
- **Portal** is productionized in Phase 2 (merge the stack, git deploy, TLS, real email).
- **Perfect screenshots** get a quality program in Phase 3, built on the visual-proof-cycle
  skill and the proof-pack items already tracked in the open-work tracker.
- **Rig tooling perfect** starts with the install fix (Phase 0 — which alone revives TUI,
  config-web, and the evolve lifecycle), then UX/status-truth/bootstrap in Phase 2, gated by
  the descoping decision (Open Decision #3).

Scope note per tg#5608: everything below is a **proposal**. Only the five explicitly-ordered
fix tracks (section 10) are executing now. Nothing else starts until the CTO reviews this spec
(suggested vehicle: `review spec-web`).

## 5.1 Phase 0 — days (stop the bleeding)[#](#51-phase-0--days-stop-the-bleeding) [S042]

Ordered; items are small, independently shippable, each with its own ticket per the ticket
discipline.

1. **Fix red main in hyperide** (P-6). Tests (3 failures via `src/app/App.tsx`, #599/HYP-378
   fallout), Lint & Typecheck, CodeQL Self-Gate, Sync to Public. ArgoCD Deploy is HYP-855
   (in progress). Exit criterion: all checks green on main twice consecutively.
2. **Re-attach the e2e `pull_request` trigger** (C-1) in
   `ext-test-projects/.github/workflows/unit-tests.yml` — one-line revert of the billing-outage
   detachment, per the workflow's own TODO comment.
3. **Merge/close the 22 e2e PRs inside a measurement bracket** (C-3), per the quorum protocol:
   take one authoritative RED baseline first (fixes E-2/E-3 minimally enough to complete a run),
   record the failing clusters, then merge (batch size = Open Decision #5's sub-point; claude
   said one batch after baseline, codex said small batches with a SHA per run), then verify the
   attributed clusters went green. Close the dupes (#72 vs #80, #58 vs #68) and adjudicate the
   zombies (#16, #15). #51 (contrast) is mergeable immediately.
4. **Working-diff hygiene for canvas-preview** (3.9, brainstorm Phase 0 gate): `git rm --cached

.claude/scheduled\_tasks.lock `+ gitignore (C-5/W-5);` .catch(() => {}) `→ log +` <App/>`fallback (W-1); exact route predicate +` ?component= `validation (W-2); START/END delimiters
on the` @hyperide-managed `region (W-3); typed` CanvasPreviewModule `+ contract test +
clean-checkout build proof (W-4); short` docs/decisions/canvas-preview.md`.

1. **Shrink MEMORY.md under the 24.4KB cap** (P-2): one-line index entries ≤200 chars, detail
   into topic files, delete superseded entries (e.g. the CI-billing-down entry is obsolete —
   billing is restored). Add a size check so it cannot silently re-bloat (see 5.2.3 wave 1).
2. **Fix the rig install** (T-R1): `uv tool install --editable` as the canonical path (PR #84
   direction), remove the legacy symlink. Acceptance: `rig tui`, `rig config-web`, and the
   evolve lifecycle all start on this machine.
3. **rtk upgrade 0.31 → 0.43** (T-K1): brew upgrade + `rtk init -g` (regenerates the hook,
   closing bug classes a/b/c — proven on the bottle binary in scratch), add a `ps` exclusion to
   the hook (T-K2), drop the local rg patch, and declare rtk in `rig doctor` as
   Dependency(rtk >= 0.43) so this never drifts silently again (T-K3).
4. **Worktree/orphan sweep** (C-6, C-7, T-E3): investigate-and-resume-or-close the two `wf_*`
   worktrees and `agent-tools-wt-129` (staged uncommitted work — dead-worktree rule applies);
   retire the `hyper-canvas-draft-worktrees` directory name; remove the dead `review --mcp`
   mcp.json entry; delete `/private/tmp/.serena/project.yml` (T-S1 fix #1).
5. **Tool-repo hygiene sweep** (T-P2, 4.5): commit-or-stash triage of review-cli's 37 and
   tg-cli's 8 dirty files (investigate first — some may be orphaned features per the dead-code
   rule), get their test suites running, fix rig-cli CI. Exit criterion: `git status` clean and
   CI green on main in all three repos.
6. **rig self-healing install** (T-R1 follow-through; standing CTO doctrine tg#4919 2026-06-28
   "everything must install itself", reiterated tg#5652 — now promoted to an acceptance
   criterion, not a preference): (a) `rig doctor` detects a legacy symlink install / missing
   textual+rich+agenttools-service in the running interpreter and AUTO-migrates to
   `uv tool install --editable` (pipx fallback) instead of only reporting; (b) install.sh
   handles fresh machines via an uv→pipx→pip fallback chain, zero manual steps; (c) `rig

init/apply` preflight auto-repairs the install rather than silently degrading the TUI to
preview mode (the current behavior); (d) covered by the cleanroom E2E in CI (T-R5).

## 5.2 Phase 1 — ~2 weeks (measurement program + context decomposition + enforcement wave 1)[#](#52-phase-1--2-weeks-measurement-program--context-decomposition--enforcement-wave-1) [S043]

### 5.2.1 The E2E measurement program (quorum-ratified)[#](#521-the-e2e-measurement-program-quorum-ratified) [S044]

Goal restated: **"one night = one authoritative verdict"** — not "all green". Verbatim quorum
consensus items (all 3/3 unless noted):

1. **Freeze the scope baseline mechanically, not socially** — no new lanes, no fixture
   expansion, no readonly→editability conversions during stabilization; enforce via a CI check
   on the fixture/lane manifests, not via a request in chat. (Scope freeze approval = Open
   Decision #5.)
2. **Tiered goal structure**: Tier 0 — curated always-green core as the PR gate (fast, minutes,
   separate from the matrix; composition = Open Decision #5): the promoted cross-project subset
   already chosen by the CTO (tg#4992: inspector-select + style read/write + drag-reorder/resize
   on dep projects) is the natural seed. Tier 1 — the nightly authoritative matrix producing one
   classified verdict. Tier 2 — informational lanes (new editability lanes enter here) with
   explicit promotion/demotion criteria and an aging SLA so the tier doesn't become (quorum
   phrasing) a graveyard of hidden bugs.
3. **Failure taxonomy + autoclassification**: every failure classified `infra | harness |

product | test-bug | environment | flake | unknown` (detect OOM-killer via dmesg, ENOSPC,
wedge-timeout vs assertion), because a verdict that requires a human to hand-triage 70% noise
is not authoritative. Flake gets a quarantine lane with owners and expiry.

1. **Immutable run identity**: SHA, fixture-set hash, shard config, VS Code + Node versions,
   server deploy revision pinned into every run artifact.
2. **Server environment fixes, diagnosis-first**: ENOSPC at 84% — diagnose inodes/reserved
   blocks/watermark before buying "retention" (claude's dissent, adopted); memcg limit set from
   **measured** peak RSS of VS Code + dev-server per shard, not guessed 6GB; single lock path
   for cron and manual runs (E-3); git-based server deploy replacing rsync (E-4); fix the
   preview-not-rendering class in server Docker (E-5) — it is 126/270 of one lane and probably
   one root cause.
3. **NO LLM auto-fix until a week of consecutive authoritative verdicts** (3/3, emphatic).
   70% infra noise would make an auto-fixer generate plausible-but-wrong product patches.
4. **Wall-clock plan**: 8.5–18h does not fit a night (2–3/3). Options once verdicts stabilize:
   shard rebalance, parallel runners, or shrinking Tier 1 scope; also evaluate the single-expert
   idea worth a spike — **ephemeral isolated runner per shard**, which addresses wedge + ENOSPC
   - OOM + lock collisions as one root cause (no shared mutable environment) instead of four
     patches. The 8.5–18h variance itself is an infra-retry symptom to measure.

### 5.2.2 AGENTS.md decomposition (context delivery fix)[#](#522-agentsmd-decomposition-context-delivery-fix) [S045]

Target: from 98.7KB-invisible to ~30KB-delivered.

1. **Split** per the provisioning agent's keep-map: (a) a ~30KB project core that `CLAUDE.md`
   actually `@import`s (build/test/ship commands, repo layout, the 10 highest-value invariants);
   (b) path-scoped rule files (`.claude/rules/*.md` or equivalent) loaded on relevance —
   extension rules, e2e rules, portal rules; (c) general culture (review discipline, worktree
   discipline, tg format) moved into agent-tools **skills**, which load on trigger and are
   shared across repos by distribution rather than duplicated per repo.
2. **Delete the false claims** (D-1): the format-on-write claim (AGENTS.md:192), the
   branch-protection claim (AGENTS.md:1292-1294), and the REQUIRE\_TICKET\_SKIP how-to
   (AGENTS.md:444 — escape-hatch documentation belongs next to the audited hatch, not in the
   rulebook as a recipe). Sweep for other enforcement claims and verify each against the actual
   hook inventory (section 6) before keeping it.
3. **Dedup the triple-duplicated sections** (P-11) to single owners: user prefs live in global
   CLAUDE.md only; project invariants in the project core only; episodic lessons in MEMORY.md
   topic files only.
4. **Every rule kept as prose gets a pointer to its mechanism** ("enforced by <hook>") or an
   explicit `UNENFORCED` tag — so the next audit can grep the gap instead of rediscovering it.

### 5.2.3 Enforcement wave 1[#](#523-enforcement-wave-1) [S046]

In-flight already: main-push pre-hook (HYP-856, task #5); permissions baseline via rig (task #8).
This wave adds:

1. **Revive or delete the two dead hooks** (P-3): `background-subagent-gate` gets its
   `Agent|Task` matcher via `_build_hook_bridge` (`riglib/plan.py:~756`) or is removed from the
   declared set; `format-on-write` gets a `PostToolUse` matcher or is deleted with its AGENTS.md
   claim. A declared-dead guard is worse than none — it is a false sense of coverage.
2. **Close the `gh api` merge bypass** (G-4, ~6 lines) and add `pkill`/`screencapture` guards
   (G-5, G-6): pkill guard blocks pattern-kills of shared tool names and requires PID-targeted
   kills; screencapture guard points at the Playwright recipe.
3. **Escape-hatch audit sink** (G-8): every `ALLOW_*`/`*_SKIP` use appends
   `{ts, session, hatch, command, reason}` to `overrides.log`; `rig status` grows an "overrides
   this week" section; weekly tg digest. Bypass without a reason string becomes a block.
4. **Make `skills-read-gate` satisfiable or delete it** (G-3).
5. **Dispatch-prompt contract injection** (G-11): the revived `pre-agent` bridge point (the
   PreToolUse `Agent|Task` matcher from item 1 — one mechanism serves both) injects the standing
   contract (ticket ref requirement, review policy incl. anti-wedge inline rule, ship policy =
   report-don't-merge, handoff contract fields) into every subagent dispatch. This mechanizes
   what 12+ MEMORY.md entries currently beg orchestrators to remember.
6. **Tier the `delegate-work-to-subagents` hook** (G-12): whitelist orchestrator
   communication/status one-liners (tg, task status, read-only recon) so the delegate rule stops
   taxing exactly the behaviors (reporting, verification) the org wants more of.
7. **Fix the repo-local merge-guard false positives** (G-7) by replacing the substring hook
   with a rig-provisioned argv-aware guard — the same event that blocked this spec's own append
   is the demo.
8. **MEMORY.md size gate**: a hook or session-start check that warns at 20KB and blocks new
   index entries over 200 chars (P-2 durability).
9. **Ticket-close gate + pr-title-lint block** (PR-1, PR-2): `task done` already runs on-done
   gates — wire closing through it everywhere (the sampled violations bypassed the CLI);
   pr-title-lint flips warn→block in tool repos via rig.
10. **Review-pool health surfacing** (T-P1): review-cli marks dead seats loudly in verdicts
    ("consensus from N of M seats"), and `rig doctor` (or a cron) checks commandcode credits
    before quorums silently shrink again.

### 5.2.4 Repo/rig reconciliation[#](#524-reporig-reconciliation) [S047]

`rig apply` convergence on hyperide (P-7: the 13 drift items, .rig-bak cleanup, task-skill
link), rig.yaml for ext-test-projects (PR-4), reconcile `ci.enabled:false`-with-workflows-on-disk
in review/task/research-cli, re-enable the ext typecheck gate in lefthook AND CI (P-8), and
resolve the `defaultMode:auto` config-vs-comment contradiction (P-10).

### 5.2.5 Communication upgrades (tg)[#](#525-communication-upgrades-tg) [S048]

The status/waiting-for/trigger format is already a memory rule (tg#5262) — per 4.1 it therefore
decays. Mechanization options, cheapest first: (a) a `tg --tag report` template lint — reject
report-tagged sends missing the three required fields (they are structurally detectable:
status line, waiting-on line, next-action line); (b) a report-gate skill invoked by the Stop
hook for long-running sessions. Recommendation: (a) now, in tg-cli where the send already
passes through one choke point. (The separately-ordered tg-cli roadmap items — limit
notifications with an auto-continue button, `/tasks` lifecycle table, inbound reaction
lifecycle, tg#5698/tg#5699 — are tracked in section 10, not here.)

### 5.2.6 Linear triage[#](#526-linear-triage) [S049]

234 stale tickets triaged (batch-close with a comment, or re-activate with an owner); the
February zombies (HYP-162, HYP-121) explicitly adjudicated by the CTO — they are 5 months old
and either matter (schedule them) or don't (close them, recorded). Institute a weekly staleness
digest via the task daemon so the pile never rebuilds silently (net +29/week says it will).

### 5.2.7 Code-intel remediation + reasoning-tool delineation (serena / native LSP / superpowers / haft)[#](#527-code-intel-remediation--reasoning-tool-delineation-serena--native-lsp--superpowers--haft) [S050]

Basis: the 3.6.3 upstream/alternatives subsection and the CTO directive tg#5651 (fix or replace
serena; delineate superpowers vs haft; rig provisions both sides of the boundary).

1. **serena upgrade to the #1550 fix**: pin to a git-main SHA
   (`uv tool install --from git+https://github.com/oraios/serena@<sha> serena-agent`); verify
   with the worktree repro (session started inside a worktree under an ancestor `.serena` —
   assert the resolved root IS the worktree path, not merely that the ancestor loses); declare
   in `rig doctor` as a versioned Dependency; move to the next tagged release when upstream
   cuts one. The earlier fork plan is void — the fix is merged upstream.
2. **Custom serena context, rig-provisioned**: a claude-code-derived context YAML with
   `single_project: false` so `activate_project` stays available, plus one rule line: after
   `EnterWorktree`, call `activate_project` on the worktree root. Closes T-S1 mechanism (2)
   without waiting for upstream (#1496); if it proves out, offer the design upstream.
3. **Enable native LSP for TypeScript**: install `typescript-language-server` + enable the
   official `typescript-lsp` plugin; rig manages both (an `enabledPlugins` pin through the
   settings.json bridge + a binary Dependency in doctor). Then measure: two weeks of native
   `LSP` tool usage vs serena calls feeds the Decision #6 re-evaluation.
4. **superpowers/haft delineation rule-skill** (tg#5651), rig-provisioned cross-harness,
   extending the triage-spec §5.4 routing:
   ```in-task process discipline (diverge, plan, execute, debug, verify)
   → superpowers (brainstorming → writing-plans → executing-plans → TDD → verification);
   in-loop only — artifacts are plans/tests/commits, never durable decision records.
   cross-task decision memory (irreversible/expensive choice; "did we already decide this?")
   → haft (h-frame → h-explore → h-compare → h-decide; h-search BEFORE re-deciding);
   durable, evidence-graded records with expiry; superpowers never writes these.
   reversible / cheap / obvious → neither; reason in-line.
   Boundary test: "will a future session need to know WHY this was chosen?" — yes → the
   superpowers loop must END in a haft record (or a docs/decisions entry per Decision #1);
   no → superpowers alone.```
5. **rig provisions both sides of the boundary**: haft is already a rig area (the
   `project_tools` Area in `riglib/areas.py` provisions `.haft` + MCP registration; the `haft`
   Block in `riglib/config_schema.py` holds `workflow.md` defaults); superpowers gains a
   declared pin — the
   settings.json bridge asserts `enabledPlugins["superpowers@claude-plugins-official"] = true`
   and doctor checks the installed version against `installed_plugins.json`. Note: 5.3.1's
   quorum rejected CC plugins as a distribution channel for OUR skills; pinning a third-party
   marketplace plugin is a dependency declaration, not a distribution channel — no conflict.
6. **Honest refinement of Open Decision #1 / 7.1**: the brainstorm panel recommended haft OFF
   the org surface (CTO-personal, with a docs/decisions export). The CTO directive (tg#5651)
   keeps both provisioned with an explicit boundary — that instruction is operative. The
   delineation makes the overlap zone (triage-spec §4.2's "two think-before-build frameworks")
   explicit instead of letting the tools cancel each other; Position B stays recorded as the
   fallback if haft's organic usage remains at zero AFTER the boundary ships.
7. **Own refactor CLI + TS-server provisioning (2026-07-02, tg#5670 + tg#5678; Parts A/B in
   3.6.3.1-3.6.3.2).** (A) `rig doctor` gains a `typescript-language-server` Dependency
   (`doctor.py:35`, new `kind="npm"` + an npm branch in `_install_cmd_for` `doctor.py:154-169`)
   and a settings.json-bridge `enabledPlugins` pin for `typescript-lsp@claude-plugins-official`
   (NEW rig capability — no `enabledPlugins` management exists today); both in the GLOBAL layer,
   auto-fixed by the doctor self-heal pass. (B) build `alex-mextner/morph-cli` — thin Python
   shell (self-registering commands, `morph gain` token accounting, `--review`/`--ticket`
   ecosystem glue) over a fast Rust core (LSP client to `typescript-language-server` + `ast-grep`;
   warm per-project daemon for low latency); MVP = rename + organize/prune-imports + safe-delete +
   structural search; scaffolding tracked as task #19. Provision via the existing `tools.items`
   block (`morph: {}`, default repo `~/xp/morph-cli`, `tools.py:121-163`, zero new distribution
   code); add `ast-grep` as a `rig doctor` Dependency. Tests required (both introduce a new
   behavior branch in existing code): npm-vs-brew install-command selection for `kind="npm"`
   (macOS→brew, Linux→npm) and idempotent `enabledPlugins` writes (a repeat `rig doctor` must not
   duplicate the pin). This retires the serena-opt-in-for-rename path (Decision #6 (d)).

### 5.2.8 2026-07-02 directive batch (knip, mergiraf, CLI-language policy)[#](#528-2026-07-02-directive-batch-knip-mergiraf-cli-language-policy) [S051]

Three CTO directives from the 2026-07-02 tg batch, folded in as planned tracks per the
strategy-first scope brake (tg#5608):

1. **knip provisioning (tg#5696).** rig provisions **knip** in every JS/TS repo: a repo config +
   a CI gate over unused exports / unused files / unused dependencies (warn first, then block —
   same ramp as pr-title-lint, 5.2.3 item 9). An unused-export gate would have caught the recent
   red-main breakage class outright. Delivery reuses the existing gate-kit path (ci/ drop-ins),
   nothing bespoke. Relation to morph: knip is the *detect* gate; `morph prune-exports` is the
   *fix* op and wraps knip's analysis (3.6.3.2).
2. **mergiraf adoption (tg#5693).** rig provisions **mergiraf** — the tree-sitter AST-aware git
   merge driver already in use via the `git-merge-syntax-aware` skill — as the standard
   conflict-resolution tool: a `rig doctor` Dependency (brew/cargo) plus the gitattributes +
   `git config merge.mergiraf` wiring as a rig-managed fragment (global gitconfig layer, per-repo
   `.gitattributes` where committed). Caveat carried over from the skill: an AST merge can be
   syntactically clean and semantically wrong — always typecheck after.
3. **CLI-language policy (tg#5696).** **New CLIs are Python-only; no TypeScript.**
   Performance-critical cores go native (Rust) behind a thin Python shell — the morph-cli
   pattern (tg#5678, 3.6.3.2). The existing TS surface (tg-cli) is grandfathered; the `lib/ts`
   twins (T-L4) serve that existing consumer only and are not a license for new TS CLIs. The
   policy line belongs in the decomposed AGENTS.md tooling page (5.2.2) so it reaches agents as
   text at the moment of scaffolding — there is no mechanical gate for "don't start a repo in
   the wrong language", so this one stays prose-with-a-pointer by design (9.4).

### 5.2.9 docs/rules migration into rig provisioning (tg#5631)[#](#529-docsrules-migration-into-rig-provisioning-tg5631) [S052]

CTO directive (tg#5631, translated): "I wrote this in docs/rules — follow it, and plan the
migration to rig." The immediate trigger was the escalation-format rule (every escalated
question carries context + 2+ options with real pros/cons + a recommendation); the general
point is that rules the CTO authors in one repo must reach every agent in every repo. Today
they reach nobody by default (P-12). Strategy-first per tg#5608: this subsection is the plan;
implementation follows spec review.

**Inventory (verified 2026-07-02, hyperide main checkout, P-12):**

| File | Size | Language | Content |
| --- | --- | --- | --- |
| `docs/rules/development.md` | 16.3KB | Russian | the full dev cycle: ticket → root-cause-first → worktree + TDD → pre-commit gates (knip, review tandem, adversarial verification) → visual proof with CTO acceptance → post-commit → merge/release; PR & ticket acceptance discipline incl. the checkbox merge gate |
| `docs/rules/cto-decision-requests.md` | 15.1KB | Russian | escalation regulation: quorum-before-asking, self-check ("one shell command answers it — don't ask"), per-agent-type consultation matrix, the strict question format, Telegram as the mandatory channel, sibling-repo diff-as-formatted-text rules, PR-or-drop fields |
| `docs/rules/ticket-documentation-standard.md` | 2.5KB | English | six required ticket sections: concrete defect, evidence captured first, origin, consequence-if-unfixed, provable acceptance criteria, pseudocode |

**Design decision: no new normative-text channel.** A rig-distributed `rules` text blob would
recreate the AGENTS.md pathology one layer down — a rulebook nothing loads and nothing enforces
(4.1, 4.2). Instead, every rule is classified into the 4.1 tiers and delivered through channels
rig already manages or is gaining in wave 1:

1. **Enforced** — PreToolUse hooks via the cc\_hook\_bridge, git-hook fragments, CI gate kits
   (5.2.3, G-9). The default target per 4.1/9.4: prefer this tier whenever the rule is
   mechanically checkable — unenforced text is the proven-decaying form.
2. **Trigger-loaded advisory** — agent-tools skills (already rig-distributed; where the
   corpus's existing counterparts live: `decision-request-discipline` and
   `strict-ticket-discipline` were derived from it directly, and `systematic-debugging`,
   `tdd-red-first`, `adversarial-verification` overlap it — see the mapping rows 3/5/12).
3. **Always-loaded advisory** — a NEW, small rig `rules` area: agent-tools `rules/*.md`
   fragments fanned into repos' `.claude/rules/` — the same path-scoped rule files 5.2.2
   already plans for the AGENTS.md decomposition; this gives them their distribution
   mechanism. Per-repo selection in rig.yaml; budget-capped per 4.2; built symlink-first so it
   adds nothing to the copy layer 5.3.1 deletes.
4. **Repo-local** — stays in the repo's project core (5.2.2, item 1a) when genuinely
   repo-specific. Note the split: a rule may be global while its parameter is local (the
   worktree-placement rule is universal; the sibling-directory path value is per-repo).

**Source of truth: agent-tools** (per the 5.3.1 quorum — the canonical checkout). A repo-local
`docs/rules` file is never again the only home of normative content.

**Authoring loop** (the actual tg#5631 pain): the CTO authors where he works, and a rule
written into one repo must not rot there. Rather than a second sync channel, `rig status`
gains an **unpromoted-rules check**: any repo-local `docs/rules/*.md`, or `.claude/rules/*.md`
not traceable to an agent-tools source, is flagged as a drift item with a "promote to
agent-tools" action (joins the T-R3 blind-spot fixes). Promotion is a normal agent task:
translate to English if needed, classify per the tiers above, reconcile with any existing
twin, wire the mechanism, retire the local file to a pointer stub. A freshly authored rule
becomes a visible work item within one `rig status` run instead of an invisible file.

**Per-rule mapping** (current corpus decomposed; the `#` column is this table's own numbering —
section-6 matrix references appear inline as "row N" in the Mechanism/Target columns):

| # | Rule (source) | Mechanism today | Target |
| --- | --- | --- | --- |
| 1 | Ticket before branch/commit (development.md) | require-ticket hook (row 4) + prose | ENFORCED already — text becomes pointer; hatch audit per 5.2.3 |
| 2 | Branch naming `HYP-XXX-desc` (development.md) | prose | ENFORCE — wire the shelved commit-msg/branch lint fragment (G-9, row 17) |
| 3 | Root-cause-first diagnosis (development.md) | prose + `systematic-debugging` skill | ADVISORY — skill is single owner (9.2 treatment) |
| 4 | Worktree placement: sibling `-worktrees/`, never `.claude/worktrees/` (development.md) | prose (+ the 2026-06-27 stomping incident, matrix row 25) | ENFORCE — PreToolUse guard on `git worktree add`; path value parameterized per-repo in rig.yaml |
| 5 | TDD red-first (development.md) | prose + `tdd-red-first` skill | ADVISORY — skill owns it |
| 6 | `bun run test`, not `bun test`; test scope (development.md) | prose | REPO-LOCAL project core; optional cheap PTU guard (same family as row 10) |
| 7 | No `sed`/`perl`/`awk` file edits (development.md) | prose | ENFORCE — small PTU guard, ships with the wave-1 G-5/G-6 batch |
| 8 | e2e timeout-inflation ban (development.md) | prose | ADVISORY — e2e path-scoped rules page (5.2.2, item 1b); a timeout-constant diff lint is a later candidate, not wave 1 |
| 9 | knip before commit (development.md) | prose | ENFORCED-IN-FLIGHT — the 5.2.8 item 1 CI gate |
| 10 | Review tandem before commit (development.md) | review hook (row 5) | ENFORCED — audited hatch per 5.2.3 item 3 |
| 11 | No `--no-verify` (development.md) | block-no-verify hook (row 1) | ENFORCED — keep |
| 12 | Adversarial verification (development.md) | prose + `adversarial-verification` skill | ADVISORY — skill owns it |
| 13 | Visual proof; "done" = CTO-accepted screenshots; proofs duplicated to PR + Linear (development.md) | visual-proof marker hook (row 6) + PR checklist gate + prose | ENFORCE MORE — tg report-gate tie-in (5.2.5) + ticket-close gate (5.2.3 item 9) |
| 14 | Post-commit: Linear comment, stale-docs check (development.md) | prose | ADVISORY — the /commit skill checklist owns it |
| 15 | Merge/release flow: changelog → bump → tag → publish (development.md) | prose | REPO-LOCAL — hyperide project core (5.2.2, item 1a) |
| 16 | PR acceptance sections + checkbox merge gate (development.md) | repo CI: `pr-checklist-gate.yml` + `checklist-gate.mjs` (hand-made) | ENFORCED — promote to an agent-tools CI gate kit (G-9 family) so every repo can adopt; hyperide repoints via rig.yaml |
| 17 | Escalate only non-derivable decisions; quorum-first; no risk-labeling of product features (cto-decision-requests.md) | prose + `decision-request-discipline` skill + MEMORY.md entries | ADVISORY — skill is the owner; reconcile drift; retire the MEMORY.md twins (9.2) |
| 18 | Self-check before asking — one shell command answers it, don't ask (cto-decision-requests.md) | prose + skill | ADVISORY — same skill |
| 19 | Consultation matrix: advisor/codex/claude availability per agent type (cto-decision-requests.md) | prose only | ADVISORY — fold into the skill; the per-agent-type half belongs in the dispatch-prompt contract (G-11), since it varies by agent role |
| 20 | Strict question format: context / term decoding / options with real pros-cons / recommendation / where-to-look / screenshots / pseudocode (cto-decision-requests.md) | prose + skill + memory (tg#4965) | ADVISORY + LINT — skill text plus a tg template lint on decision-tagged sends (extends 5.2.5 (a); the fields are structurally detectable) |
| 21 | Telegram mandatory channel; consolidated open-decisions list on request (cto-decision-requests.md) | prose | ADVISORY — skill; the tg-cli `/tasks` lifecycle table (section 10 track) is the eventual mechanical rendering of "all open decisions" |
| 22 | Sibling-repo diff-as-formatted-text recipe: three-dot diff against the PR base, HTML chunking, neutral cwd, `TG_AI_MODEL` (cto-decision-requests.md) | prose only — exists nowhere else | ADVISORY — move into the tg skill (tool-usage rules live with the tool, 9.2); the highest-value untwinned content in the corpus |
| 23 | PR-or-drop required fields (cto-decision-requests.md) | prose | ADVISORY — `decision-request-discipline` skill |
| 24 | Six required ticket sections (ticket-documentation-standard.md) | prose + `strict-ticket-discipline` skill (near-verbatim twin) + task-CLI creation gates | ENFORCED at creation (PR-1 shows creation gates hold); close-side via 5.2.3 item 9; file retires to a pointer |

**Migration steps** (ordered):

1. **Reconcile and translate**: for each row with an existing skill twin, diff the file text
   against the skill line-by-line, fold real deltas into the skill (English), and flag any
   contradiction for CTO decision. Rows 19 and 22 are content that exists ONLY in docs/rules —
   they migrate whole.
2. **Wire the newly-enforceable rows** through the wave-1 machinery (5.2.3): sed/awk edit
   guard, worktree-path guard, branch-name lint via the shelved commit-msg fragment; the
   optional bun-test guard.
3. **Promote the PR checklist gate** into agent-tools/ci as a gate kit; hyperide repoints via
   rig.yaml — the reverse of the G-9 pattern: a designed-AND-live asset finally gets
   distributed.
4. **Add the rig `rules` area** (fragments → `.claude/rules/`, symlink-first) and land the
   always-on residue — if any survives classification: the current corpus resolves almost
   entirely into hooks + skills + repo core, which is itself evidence for the 9.4
   constitution.
5. **Retire the source files to pointer stubs** (English, one paragraph each: what the
   regulation covers, where it now lives, what is enforced by what). Stubs stay for human
   browsing and to keep inbound links alive (AGENTS.md:5-15, 623, 1216 — updated anyway in the
   5.2.2 decomposition pass).
6. **Turn on the authoring-loop check** in `rig status` (the unpromoted-rules drift class).

**What stays repo-local**: build/test commands and scope, the worktree parent-path value, the
release flow, e2e-process specifics — all via the 5.2.2 project core and path-scoped pages
(delivered by `@import`, not rig fan-out).

**Acceptance criteria** (provable, per the very ticket standard this subsection migrates):

- No normative content whose only home is a repo-local docs/rules file (grep-able: stubs only).
- Every mapping row resolves to a named mechanism or an explicit `UNENFORCED` tag (5.2.2 item 4).
- A rule file dropped into any repo's `docs/rules/` appears in `rig status` within one run.
- Zero Cyrillic in agent-facing rule surfaces — the section-6 matrix row 24 check gains
  `docs/rules` and `.claude/rules` in scope; this corpus quietly falsified that row's "held"
  verdict, so the cheap Cyrillic CI grep gets its first real customer.

**Phasing**: steps 1–2 ride Phase 1 wave 1 (5.2.3); steps 3–6 land in Phase 2 with the
distribution work (5.3.1/5.3.2) — the rules area must not be built on the copy layer that
5.3.1 deletes.

## 5.3 Phase 2 — 2–4 weeks (distribution, rig UX, portal, decision discipline)[#](#53-phase-2--24-weeks-distribution-rig-ux-portal-decision-discipline) [S053]

### 5.3.1 Distribution: global-source migration (quorum-unanimous Option B)[#](#531-distribution-global-source-migration-quorum-unanimous-option-b) [S054]

- agent-tools checkout = the canonical source; `~/.agents/skills` becomes a symlink into it
  (or a direct checkout — the quorum left this sub-choice open); **the copy layer is deleted**
  (it is the root cause of drift, not a symptom).
- Version control = a git-ref pin in the committed rig.yaml; harness dirs (`~/.claude/skills`,
  opencode paths) hold relative symlinks / generated adapters only.
- rig shrinks to **verify + wire** for file content, keeping all non-file provisioning (git
  hooks, GitHub config, tmux, LaunchAgents, permissions, settings.json bridge).
- Failure modes the quorum demanded designs for: F1 (3/3) — a dirty/wrong-ref/moved checkout
  kills all harnesses at once → `rig verify` gates on `git status --porcelain` + expected ref
  and fails loudly; F2 (3/3) — live-reads mean any accidental edit has instant blast radius →
  same verify gate + the checkout treated as read-only outside explicit upgrade runs; F3 (2/3)
  — incomplete fanout would reproduce the "~30 silently unlinked skills" → rig fails loudly and
  prunes dangling links; F4 (2/3) — per-repo pin option for repos that need to lag.
- Singles worth adopting: bootstrap needs a clone+auth wrapper that runs before rig exists on
  a fresh machine (claude); migration must detect and report old copied trees shadowing the new
  source (codex); verify codex/opencode actually follow symlinks before wiring them (open fact
  to test first).
- CC plugins: rejected as source of truth (3/3 — a CC-only half would create a second
  distribution system); acceptable later as a **generated derived artifact** from the pinned
  rev (codex's position; no full quorum — treat as optional follow-up, not part of the
  migration).

### 5.3.2 rig UX and scope[#](#532-rig-ux-and-scope) [S055]

- **Status truth** (T-R3): detect detached bridge points, dead command scripts, repo-local
  hooksPath/lefthook bypasses, MCP health, `.rig-bak` artifacts. `rig status` output target:
  ≤15 lines when healthy ("all N areas in sync" + exceptions only).
- **Diff-aware apply** (T-R4): plan only actual deltas; `--json` and `--quiet` for agents;
  output cap.
- **Bootstrap** (T-R5): doctor knows about agent-tools (clone step), the `cli.py:820` guard
  hole closed, init/apply action-count parity test, and the cleanroom E2E turned on in CI
  covering the real fresh-machine path.
- **Own hygiene** (T-R6): ruff+mypy in rig CI; split runner.py (4,274 lines) and the other
  over-limit functions — the shop's 80-line rule applies to the shop's own tools.
- **Descoping** (T-R7): 5 config UIs → 2 (recommendation in Open Decision #3: keep spec-web
  (mature, CTO-used) + TUI (now that it works); fold dashboard into spec-web; evolve UI stays
  with evolve; config-web retires). tmux driver / tg-ctl / stats move to separate areas or
  merge with incumbents (stats vs mcp-skill-usage, #148).

### 5.3.3 Portal productionization[#](#533-portal-productionization) [S056]

Merge the #83 → #96 → #97 stack plus the security-audit PR (task #10 output); git-based deploy
replacing the rsync snapshot (same doctrine as E-4); TLS or Tailscale-only access (PT-3); real
email provider per Open Decision #2; unit alerting for crash loops (PT-5); artifact-serving
path-traversal fix from the audit. Exit criterion: the CTO logs in from his phone via a real
OTP email over HTTPS, and the deployed SHA is `git describe`-able.

### 5.3.4 Native-lite decision discipline (brainstorm Phase 1)[#](#534-native-lite-decision-discipline-brainstorm-phase-1) [S057]

Regardless of Open Decision #1's outcome, the panel's native-lite baseline is cheap and
self-contained (SERIAL trial, 2 weeks):

- `docs/decisions/*.md` — minimal decision records ("why" only; the "what" lives in tests and
  types per 4.1's corollary).
- A ~20-line **expiry linter**: decisions carry an expiry/review-by date; expired ones fail CI
  until renewed or deleted.
- **`xrg`**: a ~30-line wrapper running rg across the repo manifest (`repos → files → tests |

no-coverage` output, freshness header, fail-closed to live rg). No daemon, no SessionStart
reindex, no capability seam (YAGNI, per the panel's own self-correction).

- **Adversarial canary seeds** — the trial's instrument: an expired decision, a fake
  `@hyperide-managed` marker outside the allowlist, a committed `.lock`, a stale registry entry;
  all must FAIL loudly. If the canaries pass silently, the discipline layer is dead and we know
  in week 1, not month 3.
- **Kill criteria by documented ACTION-CHANGES, not call counts** (call counts are gameable and
  were exactly the metric that hid haft's non-adoption): survives only if ≥2 risky changes
  reference a decision that changed code/test/rollback, and xrg produces ≥2 documented
  action-changes, both machine-collected via a required PR-template field (default "none").

## 5.4 Phase 3 — month+ (autonomy on top of stable measurement)[#](#54-phase-3--month-autonomy-on-top-of-stable-measurement) [S058]

1. **Nightly autonomous pipeline** (E-7) — only after a week of consecutive authoritative
   verdicts: fresh-main checkout, retry policy, LLM triage constrained to `product`-classified
   failures, tg morning report (using the mechanized format), artifact retention policy.
2. **Editability lanes** re-introduced one at a time **via the informational tier** (claude's
   dissent, adopted over strict serialization): each lane lands in Tier 2, gets triaged to
   stability, then promotes to Tier 1/Tier 0 — so one stubborn lane cannot block the CSS-in-JS
   mandate for months. The mandate itself (10+ systems fully editable) is unchanged.
3. **review qa Tier-2 sandbox** (from the tooling-vs-native audit): the un-caged tester hole
   gets a sandbox; reconcile the overlap with ext-test-projects so there are not two competing
   harnesses.
4. **Screenshots/video proof-pack quality program** (CTO priority #3): build on
   visual-proof-cycle + the Playwright-only rule; per-feature hero shots regenerated on release;
   video capture for interactive flows (the transcripts show "no video" as a standing gap);
   English captions per the demo rule (memory, tg#5224). Ties into the tg report-gate: a claim
   of visual change without an attached capture gets flagged at send time.
5. **Cross-harness enforcement** (G-10): extend the bridge (or its equivalent) to codex and
   opencode once the CC-side wave-1 set has proven out — same rules, every harness.

---

# 6. Enforcement matrix[#](#6-enforcement-matrix) [S059]

Rule × mechanism × layer × rig-delivery × status, from the guard audit inventory (16 live + 2
dead of 18 declared agent-hooks, plus git/CI/permission layers). Legend — Layer: `PTU` =
PreToolUse bridge hook, `git` = git hook (lefthook/dispatcher), `CI` = server-side workflow,
`perm` = permissions.deny/ask, `prose` = documentation only. Rig: is the mechanism declared in
rig.yaml and reconciled by `rig apply`?

| # | Rule | Mechanism today | Layer | Rig-managed | Status | Planned change (§) |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | No `--no-verify` commits | block-no-verify hook | PTU | yes | LIVE, fail-closed | keep |
| 2 | No raw PR merge | block-raw-pr-merge + repo hook | PTU + repo | partial (repo hook hand-rolled) | LIVE; `gh api` bypass open (G-4); substring false-positives (G-7) | close bypass, argv-aware, rig-provision (5.2.3) |
| 3 | No secrets in commits | secrets hook | PTU | yes | LIVE, fail-closed | keep; add gitleaks CI backstop (Phase 2) |
| 4 | Ticket before commit | require-ticket hook + `REQUIRE_TICKET_SKIP` | PTU | yes | LIVE but hatched; 99 skips; can't read `-F -` stdin (82 blocks) | fix stdin bug; audit hatch (5.2.3) |
| 5 | Review before commit | review hook + marker + `REVIEW_SKIP` | PTU | yes | LIVE but hatched; 135 skips + 41 marker forgeries | audited hatch; forgery-resistant marker (5.2.3) |
| 6 | Visual proof before claim | visual-proof marker hook | PTU | yes | LIVE, trust-based (marker) | tie to tg report-gate (5.2.5) |
| 7 | Skills read before work | skills-read-gate | PTU | yes | LIVE but UNSATISFIABLE (G-3) | make satisfiable or delete |
| 8 | Background-subagent gate | declared, no `Agent\ | Task` matcher | — | declared | DEAD (P-3a) | revive via plan.py:756 or delete |
| 9 | Format on write | declared, no PostToolUse matcher | — | declared | DEAD (P-3b); AGENTS.md:192 lies | revive or delete + fix doc |
| 10 | No `bun install` on extension | bun-lock guard | PTU | yes | LIVE — 38 catches, 0 landed (proof of doctrine) | keep |
| 11 | No push to main | none → pre-push fragment | git | in-flight (HYP-856) | WAS ABSENT (P-5) | land + escape-hatch audit |
| 12 | Branch protection (server) | none — free plan 403 | — | ruleset code exists unused | ABSENT; AGENTS.md claims otherwise (C-2, D-1) | doc fix; Open Decision #4 recorded |
| 13 | No `pkill` pattern-kills | prose + 2 incident memories | prose | no | UNENFORCED (G-5) | PTU guard (5.2.3) |
| 14 | No `screencapture` | prose + memory | prose | no | UNENFORCED (G-6); 0 recent recurrences | PTU guard (5.2.3) |
| 15 | Delegate work to subagents | delegate hook | PTU | yes | LIVE, too coarse (G-12) | whitelist tier |
| 16 | Permissions deny/ask baseline | empty arrays | perm | in-flight (task #8) | ABSENT (G-1) | rig-delivered baseline |
| 17 | Commit-msg lint | fragment exists in agent-tools | — | designed | DEAD-ON-SHELF (G-9) | wire in Phase 1/2 |
| 18 | Pre-push test suite | fragments exist | — | designed | DEAD-ON-SHELF (G-9) | evaluate per-repo |
| 19 | CI gate kits (11 of 15) | exist in agent-tools/ci | — | designed | DEAD-ON-SHELF (G-9) | wire selectively (Phase 2) |
| 20 | PR-title ticket linkage | pr-title-lint (warn) | CI | partial | WARN-ONLY; tooling repos 15–24% linkage | flip to block via rig (5.2.3) |
| 21 | Ticket-close gates (boxes/proof) | task CLI on-done gates | CLI | n/a | BYPASSABLE (closes happen outside CLI) | route closes through CLI (5.2.3) |
| 22 | tg report format | prose + memory | prose | no | UNENFORCED (PR-5) | tg template lint (5.2.5) |
| 23 | Function length ≤80 lines | prose (global CLAUDE.md) | prose | no | UNENFORCED; 25 violations in rig-cli | lint rule in tool-repo CI (5.3.2) |
| 24 | English-only repo docs | prose | prose | no | held so far | cheap CI grep for Cyrillic — add opportunistically |
| 25 | Worktree isolation for parallel writers | prose + memory | prose | no | UNENFORCED; stomping incident 2026-06-27 | dispatch-contract injection covers it (5.2.3) |
| 26 | Dispatch-prompt contract | none (orchestrator memory) | — | no | ABSENT (G-11) — 38% provisioned | pre-agent bridge injection (5.2.3) |
| 27 | Escape-hatch auditing | none | — | no | ABSENT (G-8) | overrides.log + rig status + tg digest |
| 28 | MEMORY.md size cap | loader truncation (silent) | — | no | FAILING SILENTLY (P-2) | size gate (5.2.3) |
| 29 | Cross-harness (codex/opencode) enforcement | none | — | no | ABSENT (G-10) | Phase 3 |

Reading the matrix: rows 1–10 are the working core (with the two dead rows 8–9 to fix or fold);
rows 11–16 are the structural holes being closed now; rows 17–21 are paid-for-but-unshipped
assets; rows 22–29 are prose that must either become mechanism or be consciously accepted as
culture. The matrix is the Phase 1 wave-1 worklist in table form.

---

# 7. Tool verdicts[#](#7-tool-verdicts) [S060]

One line of evidence each; full evidence in 3.6 and the agent reports (Appendix 12.2).

| Tool | Verdict | Evidence one-liner |
| --- | --- | --- |
| tg | **KEEP / double-down** | The one channel that reaches the CTO; hooks already inject replies; only tool with genuine daily pull. |
| review-cli (core: diff/quorum/brainstorm/just-ask) | **KEEP** | Multi-model adversarial review is unique capability; this retrospective ran on it; needs pool-health surfacing (T-P1) + its own repo hygiene (T-P2). Use native `/code-review` as the cheap first pass. |
| review qa | **KEEP + sandbox** | Un-caged tester is a real hole; reconcile overlap with ext-test-projects (two harnesses risk). |
| spec-web | **KEEP** | Mature, CTO-used, done relative to asks. |
| dashboard | **THIN** | Overlaps spec-web; fold in (Open Decision #3). |
| rig | **KEEP + re-arm** | Provisioning works (16/18 hooks fire, 1505/1521 tests); install fix revives TUI/config-web/evolve; then status-truth, diff-aware apply, bootstrap, descope (5.3.2). |
| evolve | **THIN (hobby cadence)** | Spec slice 1 of 5; core works (HTTP 200), lifecycle was install-broken; not on the critical path. |
| agent-tools | **KEEP → global-source** | 18 real agent-hooks; becomes the canonical checkout per quorum (5.3.1); plugins only as generated artifact later. |
| task-cli | **KEEP core, THIN gantt/due-daemon** | Creation gates demonstrably work (PR-1); periphery underused. |
| research-cli | **MERGE into `review just-ask --lenses`** | 4 commits, skeleton, spun out for want of a PyPI account. |
| claude-p | **RETIRE after verify** | Displaced by native `claude --print` (#77). |
| agent-browser | **KEEP (off-the-shelf)** | brew upgrade 0.27→0.31; fix/report the screenshot hang (T-B1). |
| rtk | **KEEP, upgrade + rig-manage** | 867.5M tokens claimed saved; 3 bug classes fixed in 0.43; hook becomes rig-delivered (5.1.7). |
| serena | **KEEP opt-in** | LSP value proven live (0.97s cold start, unique rename/refs); worktree semantics fixed per T-S1; NOT a memory system. |
| sverklo | **OPEN DECISION #1** | 19 calls / 100% pings says retire; feasibility study says thin trial; brainstorm says `xrg` wrapper. |
| haft | **OPEN DECISION #1** | 87 calls ever / one productive CTO day; feasibility says trial 4 thin hooks; brainstorm says CTO-personal + export mandate. |

## 7.1 haft + sverklo: the two positions, presented for decision[#](#71-haft--sverklo-the-two-positions-presented-for-decision) [S061]

This is the one place two deliberately-independent analyses reached different conclusions, and
per the CTO's instruction (tg#5592 overrode the initial retire verdicts and ordered a full
integration study), both are presented rather than silently merged. Decision framing in Open
Decision #1.

**Position A — feasibility study: INTEGRATE-THIN, trial-gated, pre-agreed kill criteria.**

- haft (higher conviction of the two): upgrade v7→v8.1 (breaking; drops the agent/TUI/desktop
  surfaces we don't use; adds evidence grades F0–F3, trust decay, drift detection — the refresh
  story becomes core; 15 upstream skills replace our 13 hand-rolled `h-*`). Integration = 4 thin
  hooks: require-decision-ref on policy paths (pre-bash), capture-decision-note on Stop
  (nudge-once, not block), protect-haft-artifacts (blocks the debris-deletion class seen in
  commit 238988c3), `haft check` as CI warn-tier. Rewrite `workflow.md` path policies for the
  real repo layout (`lib/**`, `shared/**`, ext `src/**`, `server/**`). Demotions bundled in:
  MEMORY.md stops accepting decision-shaped entries (the overflow valve), commissions/harness
  lane stays OFF (would duplicate the subagent+worktree+review pipeline). Trial gate: ≥6 organic
  agent artifacts/week, ≥1 stale-catch in 14 days; kill at <3/week average. Acknowledged risk:
  v8's Transformer Mandate makes binding decisions human-only, which may cap agent adoption at
  notes-only — in which case a docs/decisions template + expiry linter delivers ~70% of the
  value at ~5% of the dependency cost (the study's own words, converging toward Position B).
- sverklo (lower conviction): additive 0.21→0.29 upgrade; register the 4 working repos;
  freshness via SessionStart reindex + post-commit fragment; `audit-diff` as a warn-tier
  pre-commit gate (circular deps / fan-in — a class nothing else checks); memories and
  review\_diff OFF (anti parallel-universe). Trial: ≥10 real calls/week, ≥2 action-changes/14
  days; kill = uninstall. The study itself flags the honest alternative: a 30-line `xrg`
  wrapper unless ranked fan-in + test\_map earn their keep. Scenario grades: repo-rename
  shrapnel STRONG, HYP-758 MODERATE, matcher-gap WEAK (dissolves — config-string, not an
  import edge).

**Position B — brainstorm panel (5 rounds, opus+codex+gemini; unanimous among active seats):
retire BOTH from the org surface now; do NOT run the integrate-thin trials.**

- Core read of the data: 87 calls against thousands of spawns, and 4.4% third-party tool
  invocation across 204 sessions, is **market rejection, not undiscovered potential**.
- haft → **CTO personal tool** (the Transformer Mandate is a human-ICP product; an agent-driven
  shop is a PMF mismatch) **with a mandatory export of artifacts to `docs/decisions/*.md`** —
  otherwise the write-only gap just relocates. Trial its personal value during active
  canvas-preview work (the hottest hour for tooling; a no-show there is definitive).
- sverklo → the `xrg` wrapper (5.3.4). No daemon, no SessionStart reindex — that is the
  5205/87 pattern re-armed.
- Core thesis: a decision-with-teeth is an executable TEST + a TYPE, not a fourth prose
  surface; any prose artifact inherits the "refresh triggers never wired" disease (4.1
  corollary). No new discipline surface except by replacing a noisier one; haft is NOT the
  MEMORY.md overflow fix.
- Native-lite first (5.3.4); third-party trials LATER only if the native baseline exposes a
  concrete missing capability with examples. Pre-agreed tiebreaker: a failed trial → zero org
  surface; a CTO override requires a named owner, budget, dated hypothesis, expiry, and one
  existing surface deleted to make room.

**Recommendation: Position B (brainstorm path — native-lite first).** Reasons: (1) it subsumes
Position A's trial idea at lower cost — the native-lite baseline IS a trial of the underlying
need, run on components we fully control; (2) the panel's market-rejection argument (87 calls,
one productive day, agents deleting haft artifacts as debris) was **not rebutted** by the
feasibility study, which itself concedes the 70%-value-at-5%-cost fallback; (3) Position A's
strongest component — "MEMORY.md stops taking decision-shaped entries" — survives intact in
Position B via docs/decisions; (4) reversibility: if native-lite's canaries and action-change
metrics reveal a real gap, Position A's integration design is written and ready — nothing is
lost by sequencing it second.

---

# 8. Process and hygiene[#](#8-process-and-hygiene) [S062]

## 8.1 Ticket discipline scorecard[#](#81-ticket-discipline-scorecard) [S063]

| Dimension | Score | Evidence |
| --- | --- | --- |
| Creation: user-impact stated | 5/5 | fresh sample HYP-846..857 |
| Creation: acceptance criteria | 4/5 | same sample |
| Creation: filed-before-work | good in product | task CLI gates enforce at create |
| Closing: boxes checked | **broken** | HYP-758, HYP-807 closed with unchecked boxes |
| Closing: visual proof attached | **2/5, zero images** | 8 sampled closed tickets |
| Links valid | 1 broken | HYP-851 → nonexistent repo |

Diagnosis: the gates live in `task create` and are structurally bypassable at close (closes
happen in Linear UI / via API without the CLI's on-done gates). Fix in 5.2.3 item 9: closing
routes through `task done`, which already implements the gates; a Linear webhook or periodic
sweep flags closes that bypassed it.

## 8.2 PR–ticket linkage[#](#82-prticket-linkage) [S064]

Product 72% · e2e harness 57% · tool repos 15–24%. Same rule, same people, different enforcement
density (4.1, 4.5). Fix: pr-title-lint warn→block delivered via rig to every repo (5.2.3);
the block includes the escape hatch with a reason string, audited like every other hatch.

## 8.3 Throughput and backlog[#](#83-throughput-and-backlog) [S065]

- 663 PRs merged in 4 weeks: tooling 324 (49%) / product 299 (45%) / e2e 40 (6%). The tooling
  peak was the mid-June rig rollout wave — a deliberate investment, but one that never had an
  exit criterion, which is how 49% happens.
- Product backlog: 307 open, net +29/week, 76% stale >1 week, 34 de-facto-abandoned open PRs in
  hyper-saas, February zombies (HYP-162, HYP-121).
- Proposed operating rule: tooling work (except Phase 0/1 items in this spec) is capped until
  the product backlog net-growth crosses zero, and every tooling initiative names its exit
  criterion at start. This spec's own phases have exit criteria for exactly that reason.

## 8.4 Linear triage plan[#](#84-linear-triage-plan) [S066]

Phase 1 (5.2.6): 234 stale tickets triaged in one pass — every ticket gets one of {closed with
reason, re-owned with a date, explicitly parked with an expiry}. The task daemon's due-date
reminders (already built, underused — see task-cli verdict) become the maintenance loop.

## 8.5 tg communication[#](#85-tg-communication) [S067]

Current state and the mechanization proposal are in 5.2.5. The standing format rule
(status / waiting-for / trigger, memory tg#5262) is correct — it just needs to move from prose
to a send-time lint. Report language: Russian by default per the CTO's standing instruction
(9 English-language violations counted in the window). Long-work reports must carry the full
context block (what's running now, done, blocked, next, worktrees touched, checks passed) —
already specified in the CTO's global instructions; the lint checks structure, not content.

## 8.6 Session-task hygiene (a meta-observation from this retrospective)[#](#86-session-task-hygiene-a-meta-observation-from-this-retrospective) [S068]

The day's orchestration itself demonstrated a working pattern worth institutionalizing: all
investigation findings were condensed into session-task metadata (task #3 carried 18 structured
metadata keys — 15 investigation outputs + 3 context keys), which survived compaction and
powered this spec. That is the promise-durable-action
rule working as designed. The gap: session tasks are per-session; anything that must outlive the
session still needs MEMORY.md topic files or tickets — the metadata pattern should be documented
in the orchestration skill so other sessions use it deliberately rather than by luck.

---

# 9. CTO instructions review[#](#9-cto-instructions-review) [S069]

Requested explicitly ("improve my instructions"). Reviewed surfaces: global `~/.claude/CLAUDE.md`
(user preferences, ~350 lines), hyperide `AGENTS.md` (1,467 lines), `MEMORY.md` (index + topic
files). Verdict up front: the instructions are unusually good as *content* — the failures are
almost entirely *delivery and enforcement* failures (section 4). The review therefore mostly
moves things rather than rewrites them.

## 9.1 What is demonstrably working — keep, and say why[#](#91-what-is-demonstrably-working--keep-and-say-why) [S070]

- **promise-durable-action** ("a promise = an immediate durable mechanism, never words").
  Working examples from the record: the gh-ship local-checks fallback was implemented in
  `pr-ship.sh` the same day it was promised (2026-06-29, tg#5109-5110) instead of staying
  verbal; this retrospective itself stored every finding as task metadata the moment it landed
  rather than promising a write-up later (8.6). This rule is the manual precursor of the
  rules-as-mechanism doctrine — it should be cited inside the constitution (9.4).
- **never-idle-wait / watchdog discipline**. Working examples: during this retrospective the
  e2e quorum runner hit the 10-minute Bash tool ceiling and switched to a nohup+watch pattern
  rather than blocking (recorded in the quorum execution notes); the day's background agents
  ran under monitor/wakeup supervision and the one known failure mode (bg review + stop = park
  forever) is separately mechanized in the anti-wedge-review skill. The rule earned its length
  the hard way (one lost night) and has visibly changed behavior.
- **Dead-code / dead-worktree investigation rules**. Applied twice today alone (agent-tools-wt-129
  kept for investigation; haft notes deletion flagged as the debris-deletion class rather than
  repeated).
- **Ask-with-options / 30-second-decidable escalation format** (tg#4965). Section 11 of this
  spec is written in exactly that format because it works.
- **Orchestrate-don't-implement + review-always-pool-sized** (tg#4978, tg#4944). The
  retrospective's own structure (9 agents + quorums + panel, orchestrator hands-off) is the
  rule executing.

## 9.2 What is duplicated or misplaced — move, don't rewrite[#](#92-what-is-duplicated-or-misplaced--move-dont-rewrite) [S071]

- **Telegram formatting rules** exist in global CLAUDE.md, in the tg skill, AND in MEMORY.md
  (tg#5456 entry). Single owner should be the tg skill (it ships with the tool); global
  CLAUDE.md keeps two lines (language + "use tg, not curl"); MEMORY.md entry retires once the
  skill covers it. Same treatment for the review/serena/parallelize sections triple-duplicated
  across surfaces (P-11).
- **VS Code extension debugging block** (~90 lines of global CLAUDE.md): this is project
  knowledge, not user preference. It belongs in the hyperide/ext-test-projects project core
  (5.2.2) or a project-scoped skill; in the global file it taxes every session on every
  unrelated repo. Same for the "Superpowers specs paths" and 3D-CLI blocks (repo-scoped).
- **The dictionary and language rules** are genuinely global user preferences — correct
  placement, keep.
- **80-line function rule**: keep the prose (it explains intent) but the enforcement moves to
  lint config in tool-repo CI (matrix row 23) — the prose alone produced 25 violations in
  rig-cli.

## 9.3 What is stale or self-violating — fix[#](#93-what-is-stale-or-self-violating--fix) [S072]

- **MEMORY.md violates its own index rule** (one line, ≤200 chars) badly enough to blow the
  load cap (P-2). Several index entries are multi-clause paragraphs duplicating their topic
  files (e.g. the matrix-stale-reds entry appears twice with different text). The Phase 0
  shrink (5.1.5) plus the size gate (5.2.3 item 8) make the rule real.
- **Obsolete entries still steering behavior**: "CI down (billing) → --skip-ci" is marked TEMP
  but billing was restored 07-01 — the entry now actively teaches the wrong thing (skip-ci as
  normal). Retire it in the shrink.
- **AGENTS.md false enforcement claims** (D-1) — covered in 5.2.2; the instructions-review
  point is the meta-rule: **an instruction document may not claim an enforcement it does not
  have**; every "this is enforced" sentence needs a pointer to the hook/CI that enforces it
  (then rig status can verify the pointer).
- **EnterWorktree baseRef warning** (global CLAUDE.md): correct and battle-earned, but it
  documents a footgun that should be removed rather than described — a candidate for a
  worktree-creation hook that checks the base against the current stack and warns in-line
  (the worktree-base-trap skill exists; wire it as the mechanism and shrink the prose).

## 9.4 The proposed constitution: "rule = hook, prose = pointer"[#](#94-the-proposed-constitution-rule--hook-prose--pointer) [S073]

Explicitly proposed for adoption, as the CTO asked for instruction improvements rather than
piecemeal edits. One page, placed at the top of the future slim project core, replacing ~40
pages of scattered rule prose:

1. **Every normative rule ships with its mechanism** — a PreToolUse hook, a git hook, a CI
   gate, a permission rule, a lint, or a test. A rule proposal without a mechanism proposal is
   a discussion item, not a rule. (Already CTO doctrine — agent-tools ROADMAP.md:1692; this
   promotes it from roadmap to constitution.)
2. **Prose may only do three things**: explain WHY a mechanism exists (linked from the
   mechanism), teach non-normative craft (skills), or point to the mechanism. Prose that
   states a rule without pointing at its mechanism gets an `UNENFORCED` tag at review time.
3. **Every escape hatch is audited** (overrides.log; weekly digest). An unaudited hatch is a
   hole; an unsatisfiable gate is worse than no gate (both proven this cycle, G-2/G-3).
4. **Mechanisms are rig-delivered**, never hand-installed per repo (the hyperide substring hook
   G-7 is the cautionary tale — it blocked this spec's own text).
5. **Decisions promote upward**: prose → hook → test/type. The highest form a decision can
   take is an executable check; take it whenever available (4.1 corollary, brainstorm thesis).
6. **New surfaces replace old ones**: no new tool/discipline surface without naming and
   deleting the noisier incumbent it supersedes (4.4).
7. **Meters before targets**: no program gets an outcome KPI until its authoritative
   measurement exists and has run stably (4.3).

## 9.5 Sonnet-proofing (the original framing, answered by the data)[#](#95-sonnet-proofing-the-original-framing-answered-by-the-data) [S074]

The instructions were partly written on the assumption that weaker models need heavier prose.
The transcript audit falsifies the premise: era-normalized bypass rates are opus 1.4% vs sonnet
1.2%, and all 4 zero-tool misfires in the window were opus. **Model tier does not predict rule
adherence; guard density does.** Sonnet-proofing therefore equals mechanism-proofing: the same
constitution, matrix, and dispatch-contract injection serve every model tier, and model choice
returns to being a cost/latency decision (with restricted `.claude/agents` definitions pinning
models per role — Sonnet executes, Opus judges — as the tooling-vs-native audit recommends).

---

# 10. In-flight fix tracks[#](#10-in-flight-fix-tracks) [S075]

Status as of writing (2026-07-01, evening; tg-cli roadmap rows added 2026-07-02). These tracks
run under explicit CTO orders and are NOT gated on this spec's review (everything else is).

| Track | Ticket / task | Status | Next event |
| --- | --- | --- | --- |
| PostHog EU telemetry — no events arriving (tg#5585) | task #4 | RUNNING — full-lifecycle agent; hypothesis #1 is the red Deploy workflow (P-6/HYP-855) blocking prod rollout | agent handoff → orchestrator ships PR; proof = events visible in PostHog UI (project 213539) |
| Main-push protection via rig-provisioned pre-hooks (tg#5588) | HYP-856 / task #5 | RUNNING — pre-push fragment + rig.yaml wiring + hyperide lefthook + release-flow escape hatch | PRs in agent-tools/rig-cli/hyperide; orchestrator ships |
| Portal OTP email (tg#5593) | HYP-857 / task #7 | PR #97 open (branch `fix/portal-otp-email-fallback-857`, bb4e9f5, stacked #83→#96→#97); server patched reversibly; login verified live 2x; **Resend key found and validated, wiring blocked on the auto-mode classifier (G-13)** | CTO decision on provider/domain = Open Decision #2; then key lands in the unit env |
| Permissions baseline via rig (tg#5594) | task #8 | RUNNING — deny/ask baseline content + rig permissions-area reconciliation (merge, not clobber) | PR in rig-cli/agent-tools; coordinates with HYP-856 agent in separate worktrees |
| Portal auth security audit + OTP out of logs (tg#5607) | task #10 | RUNNING — journal-fallback gating, hand-rolled-auth audit (better-auth as candidate), rate-limit/expiry/single-use, cookies, CSRF, path traversal, bare-HTTP exposure | hardening PR on the portal stack + costed migration option |
| tg-cli: session-limit/error → tg notification with an inline auto-continue-at-reset-time button (tg#5698/5699) | — | ORDERED 2026-07-02 — separate implementation track (see T-T2) | tg-cli PR; proof = notification + working button on a real limit stop |
| tg-cli: `/tasks [agent] [status]` full-lifecycle table — tg message → acceptance criteria → CI → review cycles (tg#5698/5699) | — | ORDERED 2026-07-02 — separate implementation track (see T-T2) | tg-cli PR; proof = `/tasks` renders live task state end-to-end |
| tg-cli: reaction lifecycle on inbound messages — hourglass on limit-stop, eyes on resume, check-mark on task-filed/answered (tg#5698/5699) | — | ORDERED 2026-07-02 — separate implementation track (see T-T2) | tg-cli PR; proof = reactions observed on a real inbound message across the three states |
| docs/rules migration into rig provisioning — escalation-format rule + full corpus (tg#5631) | task #12 | PLANNED — spec'd as 5.2.9 (inventory, tiered per-rule mapping, rig `rules` area, unpromoted-rules check); unlike the rows above, implementation IS gated on this spec's review per the strategy-first brake (tg#5608) | reconcile+translate pass and wave-1 guards (Phase 1); gate-kit promotion + rules area (Phase 2); proof = `rig status` flags an unpromoted repo-local rule |

Interlock note: the OTP journal fallback (track 3's pragmatic unblock) is scheduled for removal
by track 5 — sequencing matters; the fallback stays until Open Decision #2 lands a real provider.

---

# 11. Open decisions for the CTO[#](#11-open-decisions-for-the-cto) [S076]

Each: context → options → recommendation. All are 30-second decidable; none block the others.

## Decision #1 — haft + sverklo: integrate-thin trial vs native-lite retirement[#](#decision-1--haft--sverklo-integrate-thin-trial-vs-native-lite-retirement) [S077]

**Context:** Two independent deep analyses (7.1). Feasibility study: both integrate-thin,
trial-gated with kill criteria. Brainstorm panel (unanimous among active seats): both off the
org surface now; haft demoted to CTO personal tool with mandatory `docs/decisions` export;
sverklo replaced by a 30-line `xrg`; native-lite baseline first; third-party trials later only
if the baseline exposes a concrete gap.
**Options:**

- (a) Brainstorm path: native-lite now (xrg + docs/decisions + expiry linter + canaries,
  2-week serial trial); haft = personal with export mandate; sverklo uninstalled (~584MB back).
- (b) Feasibility path: run both integrate-thin trials (4 haft hooks + v8 upgrade; sverklo 0.29
  - SessionStart reindex + audit-diff warn gate) with the pre-agreed kill criteria.
- (c) Split: haft per (a), sverklo per (b) — the study's sverklo case is the weaker one, so
  this is listed only for completeness.
  **Recommendation: (a).** The panel's market-rejection argument was never rebutted; (a) subsumes
  (b)'s trial logic at lower cost and total reversibility — (b)'s integration design stays on the
  shelf, ready, if canaries expose a real gap.

## Decision #2 — portal email provider and sending domain[#](#decision-2--portal-email-provider-and-sending-domain) [S078]

**Context:** PT-2/HYP-857. A validated Resend API key exists (recovered from your own
transcripts); wiring it needs your explicit go (the classifier correctly refuses to
self-authorize secrets, G-13). The from-address needs a domain.
**Options:**

- (a) Resend + verify `hyperide.ai` domain: zero code changes (PR #97 already calls Resend),
  ~10 minutes of DNS, professional from-address.
- (b) Another SMTP/provider: code change in the portal server + new creds.
- (c) Keep journal-fallback + add OAuth (Google) login: no email dependency at all, but adds an
  OAuth app + is odd for external viewers later.
  **Recommendation: (a)** — it is literally wired already; say "go Resend" and name the
  from-address (e.g. portal@hyperide.ai).

## Decision #3 — rig descoping list[#](#decision-3--rig-descoping-list) [S079]

**Context:** T-R7. Five config UIs + tmux/tg-ctl/model-cron/stats inside one "bootstrap" tool;
the CTO's own KPI audit flagged the sprawl.
**Options:**

- (a) 5 UIs → 2: keep spec-web + TUI; fold dashboard into spec-web; config-web retires (TUI
  covers it now that install works); evolve UI stays with evolve at hobby cadence. tmux/tg-ctl/
  stats split into separate areas or merge with incumbents (stats vs mcp-skill-usage #148).
- (b) Keep all surfaces, just fix quality: no deletion, apply 5.3.2 hygiene across the board.
- (c) Aggressive: rig = apply/status/doctor only; everything UI moves out of the repo.
  **Recommendation: (a)** — matches actual usage evidence (spec-web used, config-web exit-127 for
  weeks and nobody noticed until the audit), keeps one human UI (TUI) + one web UI.

## Decision #4 — GitHub plan (recorded decision, no action needed)[#](#decision-4--github-plan-recorded-decision-no-action-needed) [S080]

**Context:** C-2. Server-side branch protection/rulesets require a paid plan (403 confirmed);
you said no payment (tg#5588).
**Recorded:** client-side-only protection is the accepted trade-off; HYP-856 + G-4 closure +
the audited-hatch design are the compensating controls. Consequence acknowledged: a determined
or buggy client with push rights can still bypass locally-enforced rules — accepted risk.
**Only sub-option if ever revisited:** GitHub Team (~$4/user/mo) turns `github_ruleset.py`
(already written) into real server-side enforcement and lets `gh ship` shrink.

## Decision #5 — e2e scope freeze + Tier 0 core composition + merge-bracket batch size[#](#decision-5--e2e-scope-freeze--tier-0-core-composition--merge-bracket-batch-size) [S081]

**Context:** 5.2.1. The quorum demands a mechanical scope freeze and a curated Tier 0; the
merge of the 22 PRs needs a bracket protocol.
**Options (freeze):** (a) freeze until 7 consecutive authoritative verdicts; (b) freeze with a
weekly CTO-approved exception window.
**Options (Tier 0 seed):** your own tg#4992 promoted subset (inspector-select + style read/write

- drag-reorder/resize on dep projects) as the seed, extended only by promotion from Tier 1.
  **Options (merge bracket):** (a) one batch after the RED baseline (claude); (b) small batches /
  train with SHA per run (codex).
  **Recommendation:** freeze (a); Tier 0 = the tg#4992 seed; bracket (b) small batches — it costs
  one extra day and preserves attribution if anything regresses.

## Decision #6 — serena / sverklo MCP registration defaults[#](#decision-6--serena--sverklo-mcp-registration-defaults) [S082]

**Context:** T-S1/T-V1, updated 2026-07-02 by the 3.6.3 upstream/alternatives findings: the
worktree root-resolution fix is merged upstream but unreleased (oraios/serena#1550); native
Claude Code LSP now covers the read side (references/definition/diagnostics/hover, no rename)
and is immune to the pinned-root worktree class; sverklo's registration depends on Decision #1.
**Update 2026-07-02 (tg#5670 + tg#5678):** the CTO directs building an OWN CLI (public
`alex-mextner/morph-cli` — thin Python shell + fast Rust core, token-economy accounting,
ecosystem-integrated) for rename/refactor/structural search rather than keeping serena for the
write side — see Parts A/B (3.6.3.1-3.6.3.2). The options and recommendation below are updated
accordingly.
**Options:**

- (a) serena opt-in per session/project (registered but tools deferred; the semantic-code-search
  skill routes to it only when worktree preconditions hold); sverklo unregistered (follows
  Decision #1a).
- (b) Status quo: both always-on; rely on doctrine.
- (c) Native-LSP-first (new): enable the official `typescript-lsp` plugin as the default
  read-side code-intel; serena stays registered-but-deferred on a git-main pin, reached
  deliberately for rename/symbol-precise edits — its remaining unique ops — via the custom
  context (5.2.7, which also repairs serena's own worktree read path); sverklo unregistered.
  Re-evaluate serena's niche after two weeks of measured native-LSP usage.
- (d) **Native-LSP reads + own refactor CLI (2026-07-02, tg#5670 + tg#5678):** enable the
  official `typescript-lsp` plugin for the read side (Part A, 3.6.3.1) AND build the public
  `alex-mextner/morph-cli` (Part B, 3.6.3.2) — a thin Python shell over a fast Rust core that
  drives `typescript-language-server` (rename, organize/prune-imports, safe-delete-with-ref-check,
  move) and `ast-grep` (structural search/codemod), with rtk-style token-saving accounting
  (`morph gain`). It covers the five ops serena ALSO lacks. serena drops to retire-candidate:
  registered-but-deferred only through the two-week measurement, then unregistered once morph's
  Phase 1 lands (unless rename-only demand argues for a pinned serena instead — the CTO open
  question in 3.6.3.2). sverklo unregistered.
  **Recommendation: (d)** — supersedes (c). (c) kept serena as the write-side niche; tg#5670/
  tg#5678 choose to OWN that niche (a CLI we control, cross-harness, no worktree defect,
  rig-provisioned for free, no third-party MCP, token-accounted) and to cover five ops serena
  never had. The read side is identical to (c) — first-party native LSP with no worktree defect
  and no memory/docs baggage.

## Decision #7 — Shared-lib distribution model (declared uv path/git dep vs PyPI vs vendor + drift-guard)[#](#decision-7--shared-lib-distribution-model-declared-uv-pathgit-dep-vs-pypi-vs-vendor--drift-guard) [S083]

**Context:** 3.6.10 / T-L1. `agent-tools/lib` has 14 built, tested modules but adoption stalled
because there is no blessed way to depend on it; three consumption models coexist today
(PyPI-declared = nobody, vendor + SHA-drift-guard = research-cli only, lazy `sys.path` =
review/rig/task). Every extraction ticket in 3.6.10 is blocked on picking exactly one. The CTO
directive (tg#5686, translated: "generalize everything reasonably possible into lib") makes
this the gating decision for the whole consolidation track.
**Options:**

- (a) **PyPI-publish `agent-tools-lib`**: a real versioned dependency (`pip`/`uv`-installable,
  caret ranges, no vendoring). Cost: a publish pipeline + release discipline + a public or private
  index; ties consumers to a release cadence.
- (b) **Standardize on vendor + SHA-drift-guard** (research-cli's existing pattern): copy `lib`
  into each consumer with a committed SHA the drift-guard checks in CI. Cost: N copies, but the
  guard turns drift into a loud CI failure; no publish infra; works offline and cross-repo today.
- (c) **Declared global-source dependency** (aligns with 5.3.1's quorum-unanimous global-source
  skills migration): the single agent-tools checkout is the source of truth, and each consumer
  *declares* it in its own `pyproject.toml` — a `uv` path dependency (`[tool.uv.sources]`
  pointing at the checkout) or a git dependency — instead of raw `sys.path` insertion. Raw
  `PYTHONPATH`/`sys.path` survives only as a transitional shim; keep (b) only for repos that
  genuinely ship detached from the checkout.

**Recommendation: (c).** It is consistent with the distribution decision the retro already made
for skills (5.3.1, Option B, 3/3 quorum): one global source, kill the divergent copies. It
promotes the existing lazy-`sys.path` consumers to a *declared* dependency (one
`[tool.uv.sources]` entry each — near-zero migration), needs no publish pipeline, and leaves (b)
available for the rare detached-ship case.
Defer (a) until an external consumer or an independent release cadence actually demands a
published package. Whichever is chosen, it must be the *single* model — the current three-way
split is the actual defect.

---

# 12. Appendix[#](#12-appendix) [S084]

## 12.1 Artifact paths[#](#121-artifact-paths) [S085]

| Artifact | Path |
| --- | --- |
| E2E strategy quorum (full verdicts + moderator) | session scratchpad `quorum-result.md` (39.4KB; also `quorum-run.log`, `quorum.log`) |
| Distribution quorum | session task #3 metadata key `quorum_distribution` |
| haft/sverklo brainstorm (5 rounds, full log) | `~/Library/Logs/review-cli/20260701T205759_175432Z-brainstorm.md` (155.4KB, 1,065 lines) |
| PR dumps (8 repos) | session scratchpad `prs/*.json` (saas 186KB, e2e 23.7KB, rig 22.6KB, rev 22.5KB, tg 24.2KB, at 36.7KB, task 6.4KB, res 952B) |
| Linear HYP issue dump | session scratchpad `hyp-issues.jsonl` (185.3KB) |
| Transcript-derived datasets | session scratchpad: `all_bash.tsv` (14.9M), `sub_bash_cmds.tsv` (14.2M), `dispatch_prompts.tsv` (2.7M), `user_texts.tsv` (2.4M), `at-tail.jsonl` (4.8M), `alex_tg_msgs.txt` (32KB) |
| Proposed permissions baseline (task #8 input) | session scratchpad `proposed-permissions-config.yaml` (5.7KB) |
| HYP-857 server patch + backup | server: `/root/hyp857.patch`, `/root/server.ts.bak-hyp857`; local copy `hyp857.patch` in scratchpad |
| rtk probe workspaces | session scratchpad `rtk-exp/`, serena probe `serena-exp2/` |

Scratchpad root (session-scoped, this machine):
`/private/tmp/claude-501/-Users-ultra-work-hyperide/81bb8c60-8548-4884-b29a-259dec215585/scratchpad/`.
Note: the scratchpad is ephemeral; anything from it that this spec's execution needs long-term
should be copied into the relevant ticket or repo during Phase 0.

## 12.2 Agent report provenance[#](#122-agent-report-provenance) [S086]

All 15 investigation outputs (12 `report_*` + 2 `quorum_*` + 1 `brainstorm_*`), plus 3
orchestrator context keys (18 metadata keys total), are stored as metadata on session task #3
(`~/.claude/tasks/81bb8c60-8548-4884-b29a-259dec215585/3.json`, 27.7KB) under keys:
`report_serena_sverklo`, `report_haft`, `report_tooling_vs_native`, `report_provisioning`,
`report_guards`, `report_e2e`, `report_hyperide_transcripts`, `report_xp_transcripts`,
`report_rig`, `report_feasibility_haft_sverklo`, `report_tickets_prs_rigyaml`,
`report_serena_rtk_probe`, `quorum_e2e`, `quorum_distribution`, `brainstorm_haft_sverklo`,
plus `firsthand_observations`, `firsthand_observations_2`, and `alex_directive_strategy_first`.
The same ephemerality note applies: the task store is session-scoped; this spec is the durable
record, and any numeric claim traceable to a report key above should be re-derived from primary
sources (transcripts, GitHub, Linear) if ever contested.

Claims in this spec independently re-verified on 2026-07-01 during writing: AGENTS.md size;
CLAUDE.md pointer text; MEMORY.md size; `hyperide/.claude/hooks` contents; rig.yaml size;
`scheduled_tasks.lock` tracked in git; the e2e `unit-tests.yml` detached-trigger comment; open
PR counts (hyperide: #592/#596; ext-test-projects: 23); the stale `wf_*` worktrees and
`hyper-canvas-draft-worktrees` naming; the G-7 substring false positive (reproduced live by
this spec's own append being blocked). Claims NOT independently re-verified here and taken from
single agent reports are marked in-text where surprising (notably the haft spawn-count
discrepancy, 3.6.5, and the rtk-probe agent's claim of full rollback of its main-checkout
pollution — the orchestrator was verifying the latter independently at the time of writing).

## 12.3 Glossary[#](#123-glossary) [S087]

- **agent-tools** — the repo holding shared skills, agent-hooks, git-hook fragments, and CI
  gate kits; the future canonical distribution source (5.3.1).
- **authoritative run/verdict** — a matrix run with immutable identity (SHA, fixtures, shard
  config, versions) that completes and classifies every failure; the Tier 1 nightly output.
- **bridge / cc\_hook\_bridge** — the shim translating Claude Code hook events into agent-tools
  hook fragments; currently covers PreToolUse(Bash, Edit/Write) + Stop of 31 events.
- **dispatch-prompt contract** — the standing set of obligations (ticket ref, review/ship
  policy, handoff fields) that must reach every subagent; today via orchestrator memory (38%),
  future via pre-agent bridge injection (5.2.3).
- **escape hatch** — a documented env-var override on a guard (`REVIEW_SKIP`, `ALLOW_*`);
  legitimate only when audited (G-8).
- **haft** — third-party Go decision-graph tool (m0n0x41d/haft); Open Decision #1.
- **knip** — JS/TS analyzer of unused exports/files/dependencies; rig-provisioned CI gate per
  tg#5696 (5.2.8).
- **matrix** — the full e2e surface: ~2,000+ tests over 49 fixture projects, 9 shards, 8.5–18h.
- **measurement bracket** — the merge protocol: authoritative RED baseline → merge → verify
  attribution (5.1.3).
- **mergiraf** — tree-sitter AST-aware git merge driver (syntax-level conflict resolution);
  rig-provisioned per tg#5693 (5.2.8); already in use via the `git-merge-syntax-aware` skill.
- **morph / morph-cli** — the planned first-party rename/refactor + structural-search CLI
  (3.6.3.2): thin Python shell over a Rust core driving `typescript-language-server` +
  `ast-grep`; name per tg#5692 (final collision check pending).
- **native-lite** — the brainstorm panel's minimal decision-discipline stack: docs/decisions +
  expiry linter + xrg + canary seeds + tests/types as the binding form (5.3.4).
- **rig** — the provisioning CLI applying committed rig.yaml (skills, hooks, CI, MCP) to repos
  and machines.
- **rtk** — third-party token-optimizing CLI proxy rewriting common commands; brew `rtk-ai/rtk`.
- **rules area (rig)** — proposed rig area fanning agent-tools `rules/*.md` fragments into
  repos' `.claude/rules/` for the always-loaded advisory tier only (5.2.9); enforcement rides
  hooks/CI, trigger-shaped culture rides skills.
- **serena** — LSP-backed code-intelligence MCP (symbols, references, symbol-precise edits).
- **sverklo** — third-party multi-repo code-intelligence MCP with its own index and memories;
  Open Decision #1.
- **Tier 0/1/2** — PR-gate core / nightly authoritative matrix / informational-experimental
  lanes (5.2.1).
- **xrg** — proposed ~30-line cross-repo rg wrapper (`repos → files → tests | no-coverage`).

## 12.4 Translated CTO quotes used in this spec[#](#124-translated-cto-quotes-used-in-this-spec) [S088]

All originally Russian, translated for the repo-docs English-only rule; tg references preserved:
tg#5608 (strategy-first scope brake), tg#5588 (no GitHub payment; client-side protection via
rig), tg#5592 (study haft/sverklo integration instead of retiring), tg#5594 (permissions must
be rig-delivered), tg#4992 (test-count decomposition + promoted core subset), tg#4969/#4984
(CSS-in-JS full editability mandate), tg#4978 (orchestrate-only), tg#4944 (review always, pool
sized), tg#4866 (always file tickets), tg#3835/tg#4579 (rig as the umbrella installer),
tg#3909 (global-by-default, agents dir only for project specifics), the 2026-06-27 rig-init UX
complaint, and the 2026-07-01 KPI audit remark ("now that I have looked, everything is very
bad"). Added 2026-07-02: tg#5670 (build our own refactor tool, public, rig-provisioned),
tg#5678 (thin Python shell + very fast native core, `-cli` naming, token economy), tg#5686
("generalize everything reasonably possible into lib"), tg#5692 (name pick: "probably morph"),
tg#5693 (adopt mergiraf), tg#5696 (rig provisions knip; new CLIs Python-only, no TypeScript),
tg#5698/tg#5699 (tg-cli roadmap: limit notification with auto-continue button, `/tasks`
lifecycle table, inbound reaction lifecycle), tg#5631 ("I wrote this in docs/rules — follow
it, and plan the migration to rig" — the escalation-format rule authored into docs/rules;
plan in 5.2.9).

---

# 13. Execution status & handover (2026-07-02)[#](#13-execution-status--handover-2026-07-02) [S089]

This section is the resume point. Per CTO tg#5726 ("wrap up, fix everything in the spec, work
passes to an opus session"), a successor agent/session must be able to pick up **from this
section alone** — it duplicates the live state on purpose rather than pointing back into §10/§11
(which hold the rationale, not the current status). It supersedes §10's "as of writing
(2026-07-01, evening)" status table wherever the two disagree.

**Provenance of the numbers below.** PR/branch/check states (13.1–13.2, 13.4) and the spec-web
daemon liveness (13.4–13.5) were **live-verified 2026-07-02 ~09:30 CEST** against GitHub
(`gh pr view`, `gh pr list` across six repos) and `curl`. Agent IDs, the telemetry-comment
decisions, and other items sourced only from the handover-memory snapshot (its own metadata dates
it 2026-07-02 ~10:45 CEST) are tagged **(as of 10:45, unverified)**. The tag denotes
**verification status, not recency** — read it as "not independently re-checked in this pass," not
"older data." Where a live-verified value and a memory-only value overlap, the live-verified one is
authoritative regardless of the clock ordering of the two timestamps. The successor should re-check
the tagged items before acting on them. Full verification log in 13.5.

## 13.1 Shipped 2026-07-02[#](#131-shipped-2026-07-02) [S090]

| PR / commit | What | State | Proof |
| --- | --- | --- | --- |
| **#603** → merge `5543bc4c` | feat(hooks): block direct pushes to main via lefthook pre-push gate (HYP-856) | MERGED 2026-07-02 07:11Z — **now `main` HEAD** | `gh pr view 603 --repo hyperide/hyper-saas` |
| **#606** → merge `131faf40` | fix(ci): clear the 3 red-main signatures blocking green-gated merges (HYP-860) | MERGED 2026-07-01 23:12Z | `gh pr view 606 --repo hyperide/hyper-saas` |
| Retro spec (#604) commits `8cbcac63`, `43b556f9` | DRY fold + 2026-07-02 directive batch; docs/rules→rig migration plan (24-row map) | pushed to `spec/agent-ecosystem-retrospective`; all #604 checks green | this file's git log |

Note on dates: #606 merged the **previous night** (2026-07-01 23:12Z) — it is listed here, under a
2026-07-02 heading, because it is the red-main fix that unblocked today's green-gated merges; only
#603 and the spec commits are strictly 2026-07-02 work.

Note: #607 ("CodeQL Self-Gate 59-finding backlog", HYP-861) was reported "proven green" in the
handover memory but is **not yet merged** — it is in the ship queue (13.4), blocked on unresolved
review threads, not on CodeQL.

## 13.2 In-flight tracks[#](#132-in-flight-tracks) [S091]

Each track runs under an explicit CTO order and is **not** gated on this spec's review (the
strategy-first brake, tg#5608, gates only the spec-derived Phase work). "Agent" = the transcript
ID from the handover memory; treat those as pointers, not liveness signals — agent output stubs
(133 B) make mtime useless, so re-check the repo/PR state, not the agent.

| Track | Task # / agent | Repo + branch | What remains | How to verify |
| --- | --- | --- | --- | --- |
| `gh ship` whitelist in the delegate hook | #23 / `a0f915db` (unverified) | `agent-tools` @ `fix-gh-ship` (branch exists; **no PR yet**) | Open PR, land; source fix so the delegate hook stops flapping on orchestrator Bash (it currently blocks even read-only `gh pr view`) | Delegate hook lets `gh ship` and read-only `gh pr view` through for the orchestrator, deterministically |
| Portal bare-HTML screenshots + phone horizontal-scroll | #25 (+#15) / `a2f30434` (unv.) | `hyper-ext-e2e` (portal); branch TBD | Render screenshots as bare HTML; kill the phone h-scroll | Open the portal on a phone: screenshots show as bare HTML, no horizontal scroll |
| tg-cli: limit→tg notify + auto-continue button; `/tasks` cmd; ⏳/👀/✅ reactions | #26–#28 / `a6a80631` (unv.) | `tg-cli` (**no PR yet** — 0 open) | Implement all three; **add test isolation + staleness guard + fix the `"%0"` template bug** (its smoke test leaked to Alex's real chat) | Notification + working button on a real limit-stop; `/tasks` renders live state; reactions across the 3 states; smoke test does NOT hit Alex's real chat |
| spec-web daemon resume + new reqs | #20, #33 / `a528cca9` (unv.) | `review-cli` (**no PR yet** — 0 open) | `--agent <name>` REQUIRED on start; submitted comments must be **delivered** to that agent's session (10 comments sit undelivered in `~/.config/review-cli/spec-web/*.json`) — tg#5722 | Starting spec-web without `--agent` fails; a submitted comment reaches the named agent's session |
| rig-provisioned lint hook (repair the dead `post-write` hook) | #30 / `ad9b5daa` (unv.) | `agent-tools` **PR #161** (`feat/lint-on-write-post-write`, closes #160; MERGEABLE) | Review → merge | A PostToolUse Write event fires the lint hook in Claude Code (before-fix it fired nothing — confirmed dead) |
| ArgoCD token regen, token-only scope | #32 / `a5d8b487` (unv.) | infra (no repo PR) | Regenerate token; **RBAC changes are classifier-forbidden** — if root cause is RBAC, escalate to Alex with options | ArgoCD auth works on the regenerated token; no RBAC change made |
| Telemetry spec: apply Alex's 10 spec-web comments | (telemetry) / `a19d9142` (unv.) | `hyper-saas` **PR #605** (`spec/telemetry-plan`, HYP-859) | Apply comments (redact `phc_` key → `.env`; OD-1=b, OD-2=a, OD-3=a; §5.4 deferred; ext = priority #1, SaaS deferred; +realtime critical tg alerts; OD-4 clarified but NOT decided); **fix the red CI** | #605 CI green (currently RED: Lint & Typecheck, Tests, CodeQL Self-Gate, review-threads all FAILURE) + comments resolved |

**Interlock (do not miss):** the `gh ship` whitelist (#23) **and** the lint hook (#30, PR #161)
both modify `agent-tools`. `rig apply` must be run **exactly once, after BOTH merge** — serialized.
Neither agent applies live config itself; skipping this leaves the shipped hooks un-provisioned.

**Queued / unstarted** (handover memory, unverified): task #31 (red-main → tg alert) and task #19
(build `morph-cli`, only after the name collision is confirmed — see 13.3 item 4 and §11 Decision
#6/#7).

## 13.3 Pending CTO actions[#](#133-pending-cto-actions) [S092]

Five items are blocked on Alex and were asked via tg. None block each other.

1. **PAT for sync-public.** The claude.ai Chrome extension is not connected, so the automated
   path can't run. Either enable the claude.ai Chrome extension + restart Chrome (then re-dispatch
   the sync agent), **or** create a PAT manually (instructions already tg'd). The public mirror is
   ~354 commits behind, baseline `6acb3d30` (count/baseline as of 10:45, unverified).
2. **`gh secret set POSTHOG_PROJECT_API_KEY`.** The classifier needs Alex's **direct** order — a
   spec-web comment is insufficient authority for a secret-store write.
3. **Open Decision #4 consent choice** (telemetry consent model). Recommendation: **(a)**.
4. **`morph-cli` name confirmation** — collision with the commercial "Morph"; task #19 (build it)
   is blocked until Alex confirms the name (ties to §11 Decision #6/#7).
5. **ArgoCD RBAC decision** — only if the ArgoCD agent (track #32) reports the root cause is RBAC;
   RBAC changes are classifier-forbidden, so escalate with options rather than acting.

## 13.4 Resume protocol for the successor[#](#134-resume-protocol-for-the-successor) [S093]

1. **Session task list #1–#33** lives in the session task store
   (`~/.claude/tasks/81bb8c60-8548-4884-b29a-259dec215585/`). Read it and cross-reference 13.2;
   this section is the map, the task store is the detail. Note the store is session-scoped and
   ephemeral — migrate anything long-lived into Linear/repo before relying on it.
2. **spec-web daemons** (ppid=1, survive session death — live-verified alive):
   - retrospective spec: `http://localhost:7911/` (this document)
   - telemetry spec: `http://localhost:7912/`
   - Tailscale mirror `https://ultras-mbp.tailbfe8ea.ts.net:8444/` (retro) / `:8445` (telemetry)
     from the handover memory — **unverified** (empty response from this shell; use the localhost
     ports, or restart the daemon per track #20).
3. **`rig apply`-once constraint** — see the interlock in 13.2: run `rig apply` a single time only
   after **both** agent-tools changes (#23 `fix-gh-ship` and #30 PR #161) have merged.
4. **Ship queue** (orchestrator ships; subagents only open PR + report — per standing policy):
   - **#607** (HYP-861, CodeQL Self-Gate backlog) — MERGEABLE/UNSTABLE; every check green
     **except `review-threads` (FAILURE)**. Resolve the open review threads, then `gh ship 607`.
   - **#602** (HYP-855, P0 prod white-screen + PostHog EU) — all 20 checks green, `mergeable`
     not yet computed by GitHub. Ship **after screenshot proof**: the prod-render proof
     (agent `abc577f1`, unverified) → `gh ship 602 --screenshot <path>`.
   - **Portal #97** (OTP fallback, HYP-857) + **#99** (security hardening) — both MERGEABLE. Ship
     **after the delegate-hook `gh ship` whitelist (#23) lands**, so the flapping delegate hook
     doesn't block the orchestrator's `gh ship`. Note #97 additionally needs **Open Decision #2**
     (email provider/domain) for the Resend key to actually deliver — merging the code and
     wiring the key are separable.
   - **#605** (telemetry plan) — fix the red CI + apply the 10 comments first (track in 13.2), then ship.
   - **agent-tools #161** — review + merge. Do **not** run `rig apply` on its own: the once-only
     step (item 3) fires only after #23 (`fix-gh-ship`) has **also** merged; if #23 is still open,
     defer `rig apply` until it lands.
5. **Process facts for the successor:** the delegate hook currently flaps on orchestrator Bash
   (blocked even read-only `gh pr view`; it let 2× `gh ship` through) — the fix is in flight (#23).
   Sub-agent secret-store writes and prod-RBAC changes require Alex's direct order (classifier).
   Use opus/sonnet for routine dispatches.

## 13.5 Live-verification log (2026-07-02 ~09:30 CEST)[#](#135-live-verification-log-2026-07-02-0930-cest) [S094]

**Verified live against GitHub / curl:**

- `hyper-saas`: #602 OPEN (all 20 checks SUCCESS; `mergeable` UNKNOWN/uncomputed), #604 OPEN (all
  checks SUCCESS), #605 OPEN (CI FAILURE: Lint & Typecheck, Tests, CodeQL Self-Gate, review-threads),
  #607 OPEN MERGEABLE/UNSTABLE (all green except review-threads FAILURE); #603 + #606 MERGED; `main`
  HEAD = `5543bc4c`. Also open: #596 (e2e portal), #592 (public sync).
- `review-cli`: 0 open PRs. `rig-cli`: 0 open PRs. `tg-cli`: 0 open PRs.
- `agent-tools`: 1 open PR — **#161** (`feat/lint-on-write-post-write`, closes #160); branches
  `fix-gh-ship` and `revise-ship-ci-gate` exist with **no PR opened yet**; `main` HEAD = `8357782b`.
- `hyper-ext-e2e` (portal): **#97** (`fix/portal-otp-email-fallback-857`, MERGEABLE) and **#99**
  (`fix/portal-security-hardening-858`, MERGEABLE) open, plus ~18 other open e2e PRs.
- spec-web: `localhost:7911` and `localhost:7912` both **alive** (`review-specweb/1.0 Python/3.14.3`;
  a 501 on HTTP HEAD is expected — the daemon only implements GET).

**Taken from the handover memory, NOT re-verified (treat as "as of 10:45, unverified"):** all agent
IDs; the tg-cli smoke-test-leak + `"%0"` template bug; the telemetry spec-comment decisions
(OD-1/2/3, redaction, priority ordering); ArgoCD root-cause; the public-mirror "354 commits behind /
baseline `6acb3d30`"; and the Tailscale spec-web mirror URLs (`:8444`/`:8445`).

## 13.6 Status update — 2026-08-18 (five weeks post-ratification)[#](#136-status-update--2026-08-18-five-weeks-post-ratification) [S095]

Sections 13.1–13.5 above are a frozen point-in-time resume log from the day after this spec was
written (2026-07-02) and were never updated since — this section is the first update since
ratification (2026-07-12). It cross-references five things a 2026-08-18 session found/fixed
against this spec's own catalogued findings, with real ticket/PR citations re-verified this date
against git/GitHub/Linear **except where noted inline** (item 3 flags an unresolved citation
mismatch; item 5 explicitly could not re-derive two PR numbers; item 6's central claim rests on a
single weak-negative GitHub code search, not a repo-clone-verified absence) — and closes with the
next actionable item per this spec's own priority ordering (§5) — **among items this pass could
verify**; it does NOT re-audit full Phase-0/1 completion (item 6 says so explicitly), so this is a
next-verified-unblocked-item claim, not a re-ratified phase reordering. **This section's
recommendation supersedes the footer's "Next step" line below** (Phase 0 is no longer the stated
near-term action pending that audit; see
item 6).

**(1) A real command-injection vulnerability, closed — a new guard-quality finding class this
spec's original audit did not catalogue.** [HYP-1260](https://linear.app/glide-vc/issue/HYP-1260):
`agent-tools`' `ci/ship/ship.sh` built its `SHIP_IMAGE_UPLOAD_CMD` uploader invocation via bash
string substitution into `eval`, so a screenshot path containing a literal double-quote could break
out of the quoting and inject shell syntax. Fixed and **merged** same-day: `agent-tools` PR
[#387](https://github.com/alex-mextner/agent-tools/pull/387) (`fix(ship): close eval-injection in
upload_png's SHIP_IMAGE_UPLOAD_CMD`). The original guard audit (§3.2, G-1..G-13) inventoried
*whether* guards fire, not whether a guard's own implementation is itself an injection surface —
this is a distinct defect class. Recorded here as **G-14 (candidate)** — "a guard/tooling
implementation can itself be an injection surface" — so a future audit of §3.2 can reference it by
id instead of re-discovering it as prose; §3.2's own G-1..G-13 list is left unedited per the
frozen-section convention, this is a forward pointer only. **Note this is NOT the same file/finding
as G-9** ("designed-but-dead enforcement assets in agent-tools") even though both touch
`agent-tools`: G-9's text (§3.2) is specifically about a commit-msg lint fragment, pre-push
test-suite fragments, and 11 of 15 CI gate kits sitting unwired; `ci/ship/ship.sh` is wired and
live (this repo's own shim exports
`SHIP_IMAGE_UPLOAD_CMD` per AGENTS.md's HYP-1252 passage) — the opposite of "designed-but-dead."
G-14 is a genuinely new, thematically-adjacent defect class (a live guard whose OWN implementation
is an injection surface), not a second instance of G-9; the two share only the file, not the
finding.

**(2) A tg-cli escalation-format gate bug, found and fix proposed, not yet merged — direct
evidence for §5.2.5/§8.5 (tg communication).** A subagent reported (relayed via Telegram) that the
`--tag question|decision` escalation-format gate rejected realistic decision-request content (a
real file path, a multi-row table, a multi-item recommendation list) while only the tool's own toy
template passed. Root cause: `hasWallOfText` split the body on literal `\n` only, so a
fully-structured Rich Message assembled as one continuous source string (no literal newline
between `<h3>`/`<table>`/`<ul>` blocks) was scored as one giant "line" and tripped the
`WALL_LINE_CHARS` threshold the moment the content was realistic. Fix proposed in `tg-cli` PR
[#262](https://github.com/alex-mextner/tg-cli/pull/262) (`fix(escalation-format): stop wall-of-text
check from newline-formatting hazing`, fixes `tg-cli`#261) — **OPEN, not yet merged** as of
2026-08-18. This is exactly the class of friction §4.1/Finding B calls a bypass-economy driver: a
gate that misfires on legitimate content trains agents to route around it, not through it.

**(3) HYP-1256 citation mismatch — flagged, not force-fit.** A same-session report described a
"Linear-ticket-closure gap fixed as HYP-1256." Linear shows HYP-1256 is in fact "e2e: Remix
console-capture postMessage bridge test times out" (an e2e infra bug, unrelated to ticket-closure
tooling, already Done with visual/log proof attached) — re-verified via `linear issue view
HYP-1256` on 2026-08-18. Rather than repeat an unverified citation into this spec (the exact
failure mode §8.1 diagnoses — "closing tickets bypasses the CLI's gates" — would be compounded by
a spec that also cites tickets without checking them), this section records the mismatch and
leaves **§8.1's underlying diagnosis unchanged and still open**: ticket-closing still bypasses
`task done`'s on-close gates when closed via the Linear UI/API directly, and the 5.2.3-item-9 fix
(route closes through `task done`; webhook/sweep flags closes that bypassed it) has **no evidence
of being implemented** as of this pass. Per this section's own append-only convention (§13.1–13.5
are a frozen snapshot; this update follows the same rule), if a successor session later identifies
the correct ticket for whatever closure-gap fix was actually intended, it should **append a new
dated amendment** (a §13.7 or a follow-up bullet under a later status update) rather than edit this
paragraph in place — editing it would silently erase the 2026-08-18 record of the mismatch, which
is the citation-hygiene evidence this paragraph exists to preserve.

**(4) The orchestrator-vs-subagent `gh ship` policy question, resolved — closes part of
G-11/5.2.3-item-5.** [HYP-1271](https://linear.app/glide-vc/issue/HYP-1271) ("Document
orchestrator-vs-subagent gh ship policy in AGENTS.md") is **Done**, merged via `hyper-saas` PR
[#726](https://github.com/hyperide/hyper-saas/pull/726). `AGENTS-CORE.md`'s "PR Merge Discipline"
section now states explicitly that the orchestrator never invokes `gh` directly (including `gh
ship`) and names the enforcing mechanism (the `orchestrator-stays-thin` hook in `agent-tools`,
PR [#212](https://github.com/alex-mextner/agent-tools/pull/212)). **This closes only the narrower
"ship policy = report-don't-merge" SUB-CASE of G-11, not G-11's spec'd remedy itself.** G-11's own
text (5.2.3 item 5) calls for the standing contract (ticket ref, review policy, ship policy, handoff
fields) to be **mechanically injected at the revived `pre-agent` bridge point into every subagent
dispatch** — replacing the 12+ MEMORY.md entries that currently only beg orchestrators to remember.
What landed here is prose in the canonical rules file plus one orchestrator-side hook for one rule
(ship policy) — per this spec's own §4.1 doctrine, prose is the decaying enforcement form G-11 exists
to retire, not the mechanized one. The pre-agent contract-injection mechanism itself remains
unimplemented. **Not fully closed**: a follow-up,
[HYP-1272](https://linear.app/glide-vc/issue/HYP-1272) ("AGENTS.md: orchestrator-stays-thin hook
conflicts with documented orchestrator-run read-only `gh` calls"), is still **open/todo** — several
runbooks in this repo (the CronCreate PR-check reminder template, `.claude/commands/pr.md`,
`full-vibe.md`, `result-report.md`, `runners.md`) still instruct the orchestrator to run `gh`
inline and have not been reconciled with the resolved policy.

**(5) Review-quorum-skip recurrence — live evidence G-2 is still open, PR numbers not
independently re-verified in this pass.** The originating session reported two PRs where the
review-quorum gate was skipped before merge. `agent-tools` PR
[#225](https://github.com/alex-mextner/agent-tools/pull/225) ("Guard-B, self-merge-authority")
shows a review-quorum preflight mechanism does exist in code, so this is a **gap in an existing
guard, not an absent one** — consistent with Finding B's diagnosis (guard bugs/friction, not model
laziness). This section could not, in the time available, independently re-derive which two PR
numbers the incidents refer to; flagging the gap honestly rather than citing unverified PR numbers.
Net effect: **G-2 (the bypass economy) is confirmed still open**, roughly five weeks after Phase 1
was scoped to close it (2026-07-12 ratification → 2026-08-18, 37 days); 5.2.3-item-10 (review-pool
health surfacing) is a DIFFERENT concern (pool seats dying silently) that this incident's evidence
(a skipped review-quorum gate) does not itself confirm or refute, and is left OUT of scope here.

**(6) Next actionable, unblocked item per this spec's own priority ordering (§5).** Phase 0 (§5.1)
is long past its "days" window and Phase 1 (§5.2, "~2 weeks", due ~2026-07-26) is roughly three
weeks overdue as of 2026-08-18, so nothing here
re-derives full Phase-0/1 completion status — that is a separate, larger audit. Within what this
pass **did** verify: **5.2.3 item 3, "Escape-hatch audit sink (G-8)"** — every `ALLOW_*`/`*_SKIP`
override should append `{ts, session, hatch, command, reason}` to `overrides.log`, surface in `rig
status`, and land in a weekly tg digest — has **no hit found** in `agent-tools` as of 2026-08-18
via `gh api search/code` for `overrides.log` (0 hits). **Caveat this section's own preamble
requires: GitHub code search only indexes default branches, can lag recent pushes, and is weak
negative evidence** — this is "not found by one search," not a repo-clone-verified absence; a
successor picking this up should `grep` a fresh clone of `agent-tools` before filing a ticket, in
case the search index is stale or the sink landed under a different name. With that caveat, G-8 has
no stated dependency on any other Phase-1/2 item, so it is unblocked if confirmed missing on THAT
axis — it has not been checked against the separately-gated Open Decisions (Section 11), and at
least one plausibly bears on G-8's design: `overrides.log` records the full `{command}` string per
the item-6 spec above, and surfacing that in `rig status` + a weekly tg digest is a secret-leak
surface if override command lines carry tokens/paths — implementation should respect whatever the
pending redaction decision settles, not assume it's moot. It would also make PAST hatch-mediated
bypasses of this kind auditable from `rig status` instead of a
same-day manual dig — **conditional on this recurrence actually having gone through an
`ALLOW_*`/`*_SKIP` hatch invocation, which finding (5) explicitly could not confirm**; finding (5)
instead frames the review-quorum skips as "a gap in an existing guard, not an absent one" (a guard
bug, per Finding B), and `overrides.log` only records explicit hatch invocations — if these
specific skips were guard-bug-mediated rather than hatch-mediated, G-8 would not have recorded them
either. G-8 is still next in priority order and worth building regardless, but it is not
established here as *the* fix for this specific recurrence. Recommended next step: re-confirm via a
fresh clone, then file/pick up the ticket for G-8 in `agent-tools` and implement
`overrides.log` + the `rig status` surfacing, then re-run the same review-quorum-skip check against
its output instead of manual reconstruction.

---

\_End of spec. Original next step per tg#5608 (2026-07-02): CTO review (suggested via `review
spec-web`), then Open Decisions #1–#7, then Phase 0 execution under normal ticket/PR discipline —
**superseded by §13.6 item (6) as of 2026-08-18**: CTO review + ratification already happened
(2026-07-12), Phase 0/1 are weeks past their windows, and the concrete next action is now
"re-confirm and, if still missing, implement the G-8 escape-hatch audit sink," not Phase 0
execution. **Open Decisions #1–#7 (Section 11) are NOT superseded and remain open and
individually CTO-gated** — the item-(6) supersession only replaces the STRATEGY-EXECUTION step of
the old chain (CTO review, then Phase 0), it does not touch the separately-gated decisions; per the
header status line, ratifying the strategy never resolved them. Live resume point for the successor
session: §13, now including §13.6 (2026-08-18 status update — read that first, it supersedes the
staleness of §13.1–13.5 without deleting them).\_
