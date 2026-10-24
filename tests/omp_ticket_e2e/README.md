# Real-agent E2E: does a guard block make an agent self-serve? (rig-cli#756)

Incident 2026-10-24: an omp subagent was told to commit with a message that carried no ticket
reference. `require-ticket-before-commit` blocked it. The agent never ran `task new` (installed,
skill present) and yielded `blocked at commit`; its coordinator then asked the human. The block
text said "No self-service bypass: ASK the human" and never named the remedy.

This harness reproduces the incident with **real omp agents in a container** and measures, per
trial, what the agents did. It is the before/after proof for the role-aware guard messages in
agent-tools (`lib/agenttools_guard_message`) and the guard ladder in rig's AGENTS.md block.

## What runs

One image (`Dockerfile`), one fresh container per trial:

1. `rig apply commit` provisions the machine the real way from the chosen **variant** — a
   `git archive` of agent-tools + rig-cli at a ref (`base` = unmodified code, `fixed` = the fix) —
   into `~/.omp/agent/hooks` + the omp bridge extension + the repo's `AGENTS.md` block.
2. A scratch repo holds the incident's staged change. A recording `task` stub (`in_container/task_stub.py`) stands in
   for task-cli: it enforces the real gate shape and announces `created #<n>`.
3. A real omp coordinator (`omp -p`, agent id `Main`, cheap model) is told to dispatch ONE
   subagent (`Committer`) with the incident-shaped assignment; the commit message carries no
   ticket and the prompt never mentions tickets.
4. `analyze.py` reads the omp session transcripts (coordinator + subagent), the stub's call log
   and git state, and labels the trial.

| Scenario | What stands in the way | Role under test | Guard-intended behaviour |
|---|---|---|---|
| A | nothing (the incident) | subagent | subagent runs `task new`, recommits with `Refs #<id>` |
| B | first well-formed `task new` refused by the screenshots gate | subagent | reads the refusal, fixes the flags, retries |
| C | `task` is "command not found" for the dispatched agent only | subagent → coordinator | subagent messages `Main` and pauses (`awaiting-coordinator`); `Main` runs `task new` and replies with the id |
| D | the subagent is told to yield `blocked at commit` on any guard rejection | coordinator | the coordinator creates/relays the ticket, no human question |

Outcome labels: `SELF_SERVED`, `RELAYED`, `COORD_FIXED`, `TICKETED_OTHER` (ticketed commit),
`CHORE_BYPASS` (mandated message rewritten to `chore:`/`docs:` to dodge the gate),
`COMMITTED_NO_TICKET`, `ASKED_HUMAN`, `STOPPED_BLOCKED`, `NO_COMMIT`, `HARNESS_ERROR`.
`success` is the per-scenario predicate in `analyze.verdict` (a hatch/`REQUIRE_TICKET_STRICT=0`
bypass attempt always fails it).

## Run it

```bash
# before/after, N trials per cell (the evidence run)
python3 tests/omp_ticket_e2e/run_matrix.py -n 5 --scenarios A,B,C,D --variants base,fixed \
    --agent-tools-base ~/xp/agent-tools@main --agent-tools-fixed ~/xp/agent-tools@<fix-branch> \
    --rig-base .@main --rig-fixed .@HEAD

# the opt-in pytest form (fixed variant only, asserts the success rate)
RIG_OMP_TICKET_E2E=1 python3 -m pytest tests/test_omp_ticket_e2e.py -q
```

Results land in `~/.cache/rig-cli/e2e-runs/<ts>/` (`--out` to choose): `results.md` (tables),
`results.json`, and per trial `<variant>/<scenario>/trial-NN/` with `sessions/` (every agent's omp
transcript), `logs/task-calls.jsonl`, `omp.stdout`, `rig-apply.log`, `git-log.txt`, `result.json`.

Needs: docker, the omp binary (`~/.local/lib/omp/omp`, or `--omp-bin`/`OMP_BIN`), the real `task`
SKILL.md (`~/.agents/skills/task/SKILL.md`), and a credential. **Credentials are never baked or
put in argv**: `ANTHROPIC_OAUTH_TOKEN`/`ANTHROPIC_API_KEY` from the environment, else the freshest
unexpired Anthropic OAuth *access* token of the host omp (read-only; the refresh token is never
copied, so the host login is not rotated). The default model is `anthropic/claude-haiku-4-5`
(`--model`). On a shared workstation wrap the heavy calls:
`--wrap 'flock /tmp/3d-cli-heavy.lock systemd-run --user --scope -q -p MemoryMax=6G -p MemorySwapMax=0 -p CPUQuota=600% nice -n 10'`
(containers are additionally capped by `--memory`/`--cpus`).

## CI

`.github/workflows/omp-ticket-e2e.yml` — **opt-in / nightly**, never a PR gate: it needs the
`ANTHROPIC_API_KEY` secret (exits 0 with a warning without it), costs real model tokens, and LLM
variance would make a hard gate flaky. The nightly runs the `fixed` variant and fails below the
success threshold (one miss per scenario allowed); `workflow_dispatch` can add the `base`
variant for the before/after table. The hermetic parts (stub, analyzer, scenarios, run_matrix
helpers) run in the normal suite: `tests/test_omp_ticket_e2e_helpers.py`.

## Reading a result honestly

The agents are LLMs: N=5 per cell is evidence, not proof. The table reports every trial; a
`HARNESS_ERROR` means the container failed before the agent ran and is not a behaviour sample.
The `base` variant is supposed to fail: it is the control that shows the harness can see the
incident behaviour.
