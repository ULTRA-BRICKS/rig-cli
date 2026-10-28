# Guard: deny direct Runpod creation outside runpod-run — Implementation Plan

> **Superseded by owner order tg#25309 (rig-cli#786):** Runpod is removed and `runpod-run` no longer exists. The guard still denies every Runpod creation path, but with the message `Runpod is removed (owner tg#25309): heavy work goes to the server: server-run -- <cmd>`; the `runpod-budget-write` and `runpod-run-script-write` rules and every `use runpod-run` hint below are gone. Read the rest as history.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** the generated omp guard (and the claude-code deny belt) denies every agent path that rents Runpod compute outside `runpod-run`, and every agent write to runpod-run's owner-only budget files or its installed script (rig-cli#714).

**Architecture:** rules stay declared once in `riglib/permissions.py` as `GuardRule`s and rendered into `rig-permissions-guard.ts` by `riglib/omp_guard.py` (template v3 from #709: `evaluateBash` → `ruleHits`, nested `sh -c` re-parsing, check CLI). Add three matcher families and widen the scope invariant into a matcher→allowed-scope table:
- `mcp_tool` (scope `("write",)`): a `write` tool call whose `input.path` is `xd://mcp__<server>_<tool>` with the tool in `tokens`; optional `value_prefixes` unused. A content predicate is needed only for update-endpoint (see constraints).
- `write_path` (scope `("write","edit")` for tool calls, plus a bash-side check): `write`'s `input.path`, `edit`'s hashline `[PATH#TAG]` section headers (port `_HASHLINE_HEADER` from agent-tools lib/omp_hook_bridge/dispatch.py), `apply_patch` `*** Update/Add/Delete File:` lines; paths `~`-expanded and normalized; matches a protected path glob in `tokens`.
- `http_write_url` (scope `("bash",)`): curl/wget/http/xh stage with a write method (port the method resolution of agent-tools agent-hooks/block-raw-ticket/block_raw_ticket.py: `-X`/`--request`, `-d`/`--data*`/`--json`/`-F` ⇒ POST unless `-G`, `-T` ⇒ PUT, wget `--post-*`/`--method`, httpie/xh method word or `key=value` items ⇒ POST; an unresolvable explicit method counts as a write) and a URL whose host matches `tokens` and path regex in `value_prefixes`.
The tool_call handler gains `write`/`edit`/`apply_patch` dispatch before its "not bash → allow" fast path. The check CLI gains `--tool <name> --input '<json>'` so a non-bash call can be evaluated from the shell (live proof).

**Tech Stack:** Python 3 (registry, renderer), generated TypeScript run by omp/bun, pytest + bun-executed fixtures.

**Spec:** binding brief R10 (session artifact `runpod-enforce-brief.md`), quoted: "deny agents' direct pod/endpoint/cluster creation outside runpod-run — `runpodctl (pod|serverless|cluster) create`, `runpodctl create pod(s)`, `flash deploy`, curl/wget/http POST to *.runpod.io v2 pods/endpoints/clusters or GraphQL podFindAndDeployOnDemand/podRentInterruptable, and Runpod MCP tools create-pod / create-endpoint / create-cluster / deploy-hub-repo / update-endpoint scaling-up … Also deny agent writes to ~/.config/runpod-run/budget* and edits of the installed runpod-run script. Deny message: 'use runpod-run'." Research: agent://RunpodEnforce.GuardScout/report (MCP calls arrive as `toolName: "write"`, `input.path: "xd://mcp__runpod_create_pod"`, `input.content` = JSON args; verified in saved omp session transcripts).

## Global Constraints

- Every new rule's hint starts with `use runpod-run` (e.g. `use runpod-run — direct Runpod pod creation bypasses its budget, pod cap and TTL`). Verdict `deny` for all.
- Bash argv rules (generic `argv_prefix`, matched in both views per #709): `runpodctl pod create`, `runpodctl pods create`, `runpodctl serverless create`, `runpodctl cluster create`, `runpodctl create pod`, `runpodctl create pods`, `flash deploy`. Still allowed: `runpodctl pod list`, `runpodctl user`, `runpodctl billing …`, `runpodctl doctor`, `flash --help`.
- HTTP: hosts `api.runpod.io`, `rest.runpod.io` (and any `*.runpod.io`); write method to a path matching `^/v[12]/(pods|endpoints|clusters)(/|$|\?)` (POST create, also PATCH/PUT on endpoints), or a POST to `/graphql` whose literal body contains `podFindAndDeployOnDemand`, `podRentInterruptable`, `createCluster`, `deployCpuPod`, or `saveEndpoint`; a `/graphql` POST whose body is unreadable at pre-exec time (`@file`, stdin, shell-expanded `$…`) is denied too (fail closed). GET reads stay allowed.
- MCP (tool names as they appear in xd paths, underscores): `mcp__runpod_create_pod`, `mcp__runpod_create_endpoint`, `mcp__runpod_create_cluster`, `mcp__runpod_deploy_hub_repo` always denied; `mcp__runpod_update_endpoint` denied when its JSON content sets `workersMin` > 0 or `workersMax` > 0 (at any depth), or is unparseable; allowed otherwise (renames, scale-to-zero). Read tools (`list_*`, `get_*`) and `delete_*` stay allowed.
- Protected paths (write/edit/apply_patch tool calls and bash writes): `~/.config/runpod-run/budget*`, `~/.agents/skills/runpod-offload/**`, `~/.local/bin/runpod-run`. Bash write shapes: output redirection (`>`, `>>`) to the path, `tee`, `cp`/`mv`/`install`/`ln`/`rm`/`chmod`/`truncate`/`dd of=` with the path as an operand, `sed -i`/`perl -i` on it. Reading (`cat`, `less`, `grep`) stays allowed. `rig apply` writing the installed skill is a subprocess, not a tool call, so it is unaffected.
- eval scope: `code_pattern` rules for the needles `podFindAndDeployOnDemand` and `podRentInterruptable` (GraphQL from an eval cell).
- claude-code belt: add whole-tool denies `mcp__runpod__create-pod`, `mcp__runpod__create-endpoint`, `mcp__runpod__create-cluster`, `mcp__runpod__deploy-hub-repo` and `Bash(runpodctl pod create:*)`, `Bash(runpodctl create pod:*)`, `Bash(runpodctl create pods:*)`, `Bash(runpodctl serverless create:*)`, `Bash(runpodctl cluster create:*)`, `Bash(flash deploy:*)`; opencode/codex: the same runpodctl/flash argv forms in their dialects. Every guard-only rule id is listed in the parity test's `guard_extras`.
- Bump `GUARD_TEMPLATE_VERSION` to 4 and the package version per rig-cli's release rule (skill bump-version-on-release; `riglib/_version.py`).
- Tests run one file at a time under `nice -n19 ionice -c3`; bun is required for the TS fixtures (CI installs it). Docs English-only; update the guard section of docs/README where the rule list is documented.

## Review Focus

- `runpod-run --dry-run -- pytest tests` and `runpod-run --status` must stay allowed (they contain no denied argv; runpod-run's own internal curl is not a tool call).
- `curl -s https://rest.runpod.io/v1/pods -H "Authorization: …"` (GET) allowed; the same with `-d '{…}'` denied.
- `bash -c 'runpodctl pod create --gpuType x'` denied (nested re-parse from #709).
- An `edit` whose hashline header names `~/.agents/skills/runpod-offload/runpod-run` is denied; an edit of `~/dev/agent-tools/skills/universal/runpod-offload/runpod-run` (the source in a worktree) is allowed.
- A `write` to `xd://mcp__runpod_list_pods` is allowed.

---

### Task 1: matchers, rules, handler dispatch, check CLI

**Files:**
- Modify: `riglib/permissions.py` (matcher→scope table in `GuardRule.__post_init__`, new rules in `OMP_GUARD_DENY_RULES`, claude-code/opencode/codex belt entries)
- Modify: `riglib/omp_guard.py` (TS: `mcp_tool`, `write_path`, `http_write_url` matchers; handler dispatch for write/edit/apply_patch; check CLI `--tool/--input`; template version 4)
- Modify: `riglib/_version.py`, docs listing guard rules
- Test: `tests/test_omp_guard_runpod.py` (new; bun-executed table through `evaluate…` and the real handler, same harness style as tests/test_omp_guard_signals.py), `tests/test_omp_guard.py` (parity `guard_extras`), `tests/test_permissions.py` (belt entries)

**Interfaces:**
- Produces: TS `evaluateToolCall(toolName: string, input: unknown, host?): Finding[]` (bash delegates to `evaluateBash`); check CLI `bun rig-permissions-guard.ts --tool write --input '{"path":"xd://mcp__runpod_create_pod","content":"{}"}'` → `deny runpod-mcp-create: use runpod-run …`, exit 2.

- [ ] Step 1: failing table tests covering every Global Constraints line (deny + allow rows; the Review Focus rows included).
- [ ] Step 2: run `nice -n19 ionice -c3 python3 -m pytest -q tests/test_omp_guard_runpod.py` → FAIL.
- [ ] Step 3: implement.
- [ ] Step 4: run the new file, then tests/test_omp_guard.py, tests/test_omp_guard_signals.py, tests/test_permissions.py one at a time → PASS.
- [ ] Step 5: commit `feat(guard): deny direct Runpod creation and runpod-run budget/script writes outside runpod-run (Refs #714)` (plan file included).
