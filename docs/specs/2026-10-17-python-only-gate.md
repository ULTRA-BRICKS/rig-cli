# python-only: rig-ecosystem tools stay Python, enforced by pre-commit and CI (design, 2026-10-17)

Status: **design, decisions delegated by the owner**. Tickets:
[agent-tools#945](https://git.hyperide.ai/ultrabricks/agent-tools/issues/945) (the gate),
[rig-cli#725](https://git.hyperide.ai/ultrabricks/rig-cli/issues/725) (rig vendors `.py` CI
companions), [agent-tools#953](https://git.hyperide.ai/ultrabricks/agent-tools/issues/953)
(rollout to every tool repo). Port of the largest offender:
[tg-cli#768](https://git.hyperide.ai/ultrabricks/tg-cli/issues/768).

## Understanding

### What the owner said

The repo is English-only, so these are translations; the Russian originals are in the
agent-tools ticket.

- 2026-06-14 (Telegram), answering a Bun-vs-Python research request: "Okay, we're moving the
  tools to Python." agent-tools ROADMAP.md recorded it on 06-15/16 as "the whole CLI ecosystem
  is Python-only (decided twice — don't re-ask)".
- 2026-10-17 (Telegram), after an agent had offered to keep tg-cli in TypeScript: "of course
  rewrite it. All rig-ecosystem tools must be in Python." Exemptions: only code whose host
  forces another language. How to build the enforcement was delegated.

### Why advice was not enough

The decision lived only in ROADMAP prose. After it, tg-cli gained 387 TypeScript/JavaScript
files on main, agent-tools gained a 1,383-line bash script (`runpod-run`, 2026-10-14), and on
2026-10-17 an agent offered the owner "keep tg-cli in TS" as an option. Nothing failed when
non-Python code landed, so nothing stopped it.

### Intended outcome

A commit or PR that adds or changes non-Python tool code in one of the owner's tool repos
fails on the spot and names the decision. Host-forced code passes through an explicit,
reasoned exemption. tg-cli, mid-port, is warned rather than blocked.

## Decisions

1. **Scope: a repo is a rig-ecosystem tool repo when its `origin` is in an owner namespace**
   (`git.hyperide.ai/ultrabricks`, `github.com/alex-mextner`, `github.com/ultrabricks`) **and
   its name ends in `-cli` or is listed in the registry** (agent-tools, text-lint,
   claude-accounts).
   - Rejected: "has a `rig.yaml`". Product repos (boards-app, tasks-app, hyper-saas, conloca,
     the bots) carry one too and are not tools.
   - Rejected: an opt-in marker file in each repo. A new tool that forgets the marker drifts
     silently, which is the failure being fixed. The `-cli` suffix is the ecosystem's naming
     convention, so a new `foo-cli` is covered from its first commit.
2. **What fails:** an added, changed or copied file that is
   - a non-Python language by extension (TS/JS incl. `.vue/.svelte/.astro`, Go, Rust, Ruby,
     Swift, Kotlin, Java, C/C++, Objective-C, C#, Zig, Dart, Elixir, Haskell, PowerShell, ...);
   - any file whose shebang runs a non-Python interpreter (node, bun, deno, tsx, npx/bunx,
     ruby, perl, php, lua, pwsh, awk, ...), whatever its name (`tg`, `tg-ctl`, `run.command`,
     even `tool.py`);
   - a shell script (by extension or shebang) longer than **40 lines**. Glue stays legal;
     logic does not. 40 lines covers a launcher or an `exec python3 ...` wrapper and stops a
     real program.
   Deletions and mode-only changes pass, and so does an unchanged move of a file that was already non-compliant where
   it was. A move that turns a passing file into a failing one (rename to `.ts`, out of an
   exempt directory) fails. When a merge is being resolved, only content that is new relative
   to both parents counts.
3. **Exemptions are central, per path, with a mandatory reason.** The registry (`REPOS`) is in
   the checker in agent-tools; an `Allow(glob, reason)` with an empty reason makes the checker
   refuse to run. Only host-forced code qualifies:
   - omp extensions (agent-tools `lib/omp_hook_bridge/extension.ts`; tg-cli
     `extensions/omp-live.ts` and the `omp-live-{extension,runtime}.mjs` it loads, plus their
     tests); the rig omp guard template, if it ever leaves `riglib/omp_guard.py` for a `.ts`
     file (the generated `~/.omp/agent/extensions/rig-permissions-guard.ts` is outside any repo);
   - the opencode plugin (`lib/opencode_hook_bridge/plugin.js`) and pi extensions
     (`pi-extensions/**`);
   - the oxlint JS plugin and config API (`tools/oxlint/**`, `oxlint.config.ts`);
   - browser code (review-cli dashboard/specweb assets, 3d-cli web static and Home Assistant
     cards), VS Code extension-host code (review-cli `qa/ext_runner`), the stt-cli macOS Swift
     helper and its website;
   - GNOME Shell extensions laid out as installed (`**/gnome-shell/extensions/<uuid>/**`), in
     every repo. Not "any directory with an `@`": that would also exempt `pkg@1.2/` or `@scope/`.
   Central, not per repo, so the repo being checked cannot exempt itself in the same PR that
   adds the code, and so the warn tier cannot be self-granted. Downstream, the vendored copy of
   the checker is trusted by path; an edit to it shows in the PR diff and as `rig status` drift.
4. **Tiers:** `block` everywhere; `warn` only for tg-cli, granted in the registry together with
   its port ticket (tg-cli#768), flipped to `block` when the port lands. The warn mode never
   blocks a tg-cli commit, so the port's own work is never stopped by the gate.
5. **Carriers:** one stdlib Python file, `agent-tools/ci/python-only/python_only.py`.
   - **Pre-commit:** a byte-identical twin at
     `git-hooks/global-dispatcher/global-hooks.d/pre-commit/20-python-only`, installed by
     `rig apply commit` with the global dispatcher, so it runs in every repo on every
     rig-managed machine and exits silently outside scope. A regular file, not a symlink: a
     relative link dangles after a symlink-preserving copy into `~/.config/git`. A test keeps the twins
     equal. Protected capability id `python-only`: a cloned repo's `.githooks-skip` cannot turn
     it off.
   - **CI:** slot `ci/python-only` (`ci.items.python-only` in a repo's `rig.yaml`). It runs on
     `pull_request_target` (including `edited`, so retargeting a PR re-runs it), uses the base
     branch's copy of the checker and reads the PR head as data (fetched through
     `refs/pull/<n>/head`, so fork PRs work). No PR code runs.
   - **Fail-open only before scope is known:** an unexpected error while deciding whether a repo
     is in scope lets the commit through (the fragment runs in every repo on the machine); once
     a repo is in scope, a git error fails closed.
6. **rig vendors `.py` CI companions** (rig-cli#725). Today rig copies only `.sh`/`.mjs`
   next to a workflow, so the slot would install a workflow whose script is missing. With
   #725, rig copies `.py` too, with the exec bit, and drift reports a missing or edited one
   (covered by an install-plus-drift regression test). A
   test-shaped name (`test_*.py`, `*_test.py`, `*.test.py`, `conftest.py`) is the slot's own
   test and stays in agent-tools. Contract: a gate script never has a test-shaped name (a
   vendored one would also be collected by the consumer repo's own pytest).
7. **Rollout order:** rig-cli#725 merges and is installed on the machine before the
   agent-tools change that adds the slot. Publishing the slot to the catalog is what exposes
   it: a repo with `ci.all: true` picks it up on its next `rig apply commit` with no per-repo
   PR, and an older rig would install the workflow without its script. agent-tools enables the
   slot in its own change; the other tool repos enable it one PR each under agent-tools#953.
   A rig regression test covers the `ci.all: true` case: the slot's workflow and its `.py`
   companion install together. The pre-commit fragment covers every tool repo from the first
   `rig apply commit` (bare `rig apply` only previews).
8. **Porting what already exists** is tracked per tool: agent-tools #946-#952, rig-cli#724,
   task-cli#281, review-cli#652, qa-cli#43, text-lint#8, stt-cli#146, pm-cli#41,
   research-cli#27, 3d-cli#398, dev-cli HYP-1742, router-cli HYP-1743, draw-cli HYP-1744.
   Until a file is ported, its next edit is blocked; porting it is the fix, not an exemption.
   harness-cli is already Python. claude-rotate was retired, not ported (tg-cli PR #543,
   review-cli PR #606).
9. **The lesson goes into `decision-request-discipline`:** search recorded decisions (ROADMAP,
   ADRs, AGENTS.md) before offering options, and never offer one that contradicts a recorded
   owner decision.

## Message

```
python-only: BLOCKED — non-Python source in the rig-ecosystem tool repo <name>.
  Owner decision 2026-06-14, restated 2026-10-17: every rig-ecosystem tool is written in Python
  ("Okay, we're moving the tools to Python"; "all rig-ecosystem tools must be in Python").
  - <path> — TypeScript source
  Fix: write it in Python. Shell glue up to 40 lines is fine.
  Host-forced code only (...) gets an exemption: an Allow entry with a reason in
  agent-tools ci/python-only/python_only.py (REPOS), via an agent-tools PR.
```

Exit codes: 0 pass, warn or out of scope; 1 blocked; 2 git error or invalid registry.

## Acceptance (live, on this machine, after `rig apply commit`)

- A sample `.ts` committed in a Python-only tool repo is refused, and the message cites the
  2026-06-14 decision.
- A change to an allowlisted host-forced file (agent-tools `lib/omp_hook_bridge/extension.ts`)
  passes.
- The same `.ts` in tg-cli prints the warning and the commit goes through.
- `rig apply commit` in agent-tools installs `.github/workflows/python-only.yml` and leaves
  `ci/python-only/python_only.py` identical.
- Bootstrap: the PR that first adds the workflow shows no `python-only` check, because
  `pull_request_target` runs the base branch's workflow and the base has none yet (same
  limitation as `leftover-grep.yml`). That PR is verified by the tests and the local hook. The
  first PR opened after it merges must show a `python-only` check.
