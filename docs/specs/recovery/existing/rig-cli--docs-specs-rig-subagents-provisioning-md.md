# Review addendum: rig-cli/docs/specs/rig-subagents-provisioning.md

Source revision: `97023cfd0c1ec4c8ba5c9e6a7666e26391ecc6ad`.

This source document is retained with its original draft/approved/skipped status. This addendum does not approve previously skipped work. Root `rig-cli/SPEC.md` describes Evolve, not the entire rig ecosystem.

Current recovery contract: [Native agent identity and cross-harness guard parity](../identity.md). Governing epic: https://github.com/alex-mextner/rig-cli/issues/521.

## Example and revalidation

A main OMP session and two native task children share an OS process. The children have distinct invocation/session identity, while the main call remains the orchestrator. A path suffix, recent transcript mtime, caller-supplied agent_id or process-global flag cannot prove which child made the call.

## Source quotations

> OMP, похоже, не подчиняется хуку оркестратора.

Source **Q020**; compiled-fragment-attribution; rig-zerobug/rig-fable session 2026-09-04..07; agent=ThinNotSub; PDF page(s) 1. Thematic recovery requirement, not retroactive authorship of this entire specification.

<details><summary>Original wording (before editorial correction)</summary>

> omp похоже не подчинается хуку оркестратора

</details>

> Не применяется к сессиям внутри review-cli.

Source **Q122**; compiled-fragment-attribution; rig-zerobug/rig-fable session 2026-09-04..07; agent=ThinNotSub; PDF page(s) 13. Thematic recovery requirement, not retroactive authorship of this entire specification.

**Context fragment:** do not interpret as a standalone approval.

<details><summary>Original wording (before editorial correction)</summary>

> не примеряется к сессиям внутри review-cli

</details>

## Linked acceptance backlog

- [rig-cli#75](https://github.com/alex-mextner/rig-cli/issues/75) — docs(spec): correct false 'CC ships no built-in agents' claim in rig-subagents-provisioning spec
- [rig-cli#77](https://github.com/alex-mextner/rig-cli/issues/77) — docs: mark rig-subagents-provisioning spec as SKIPPED (not planned work)
- [rig-cli#119](https://github.com/alex-mextner/rig-cli/issues/119) — Wire Claude Code Agent Task hook bridge matcher
- [rig-cli#121](https://github.com/alex-mextner/rig-cli/issues/121) — Provision Codex hook bridge in rig apply
- [rig-cli#127](https://github.com/alex-mextner/rig-cli/issues/127) — Fix opencode hook bridge PR blockers
- [rig-cli#213](https://github.com/alex-mextner/rig-cli/issues/213) — orchestrator-stays-thin false-blocks tg calls describing git commands as prose
- [rig-cli#340](https://github.com/alex-mextner/rig-cli/issues/340) — Review Monitor hook bridge matcher diff
- [rig-cli#341](https://github.com/alex-mextner/rig-cli/issues/341) — Fold tg-ctl's Codex Stop usage-telemetry hook into rig's codex hook bridge
- [rig-cli#346](https://github.com/alex-mextner/rig-cli/issues/346) — Deduplicate opencode/omp hook-bridge register+drift logic in rig-cli
- [rig-cli#348](https://github.com/alex-mextner/rig-cli/issues/348) — Review omp hook bridge uncommitted diff
- [rig-cli#403](https://github.com/alex-mextner/rig-cli/issues/403) — Add RIG_CLEANROOM_E2E omp leg to prove harness parity for orchestrator-stays-thin
- [rig-cli#423](https://github.com/alex-mextner/rig-cli/issues/423) — Splice always-on orchestrator block into repo AGENTS.md
- [rig-cli#425](https://github.com/alex-mextner/rig-cli/issues/425) — Cleanroom: all harnesses see the same orchestrator AGENTS.md block after apply
- [rig-cli#443](https://github.com/alex-mextner/rig-cli/issues/443) — Honor orchestrator_only false when planning the AGENTS.md block
- [rig-cli#444](https://github.com/alex-mextner/rig-cli/issues/444) — Remove gh ship exception from orchestrator AGENTS.md block
- [rig-cli#445](https://github.com/alex-mextner/rig-cli/issues/445) — Provision omp task.agentIdleTtlMs=0 so yielded helpers stay on the live roster
- [rig-cli#463](https://github.com/alex-mextner/rig-cli/issues/463) — Document the subagent Monitor ban via rig (reasons + replacement tools)
- [rig-cli#464](https://github.com/alex-mextner/rig-cli/issues/464) — Live omp sessions keep parking after task.agentIdleTtlMs: 0 (spawn-time settings cache)
- [rig-cli#479](https://github.com/alex-mextner/rig-cli/issues/479) — Register a Workflow PreToolUse matcher in hook_bridge_entries so workflow-danger-gate (agent-tools#652/#671) actually fires under Claude Code
- [rig-cli#481](https://github.com/alex-mextner/rig-cli/issues/481) — rig-cli: register Workflow PreToolUse matcher for pre-workflow / workflow-danger-gate

## Preserved specification

# rig provisions sub-agents (`subagents` category)

> **STATUS: SKIPPED — NOT planned work.** The CTO decided not to adopt custom
> sub-agents (Claude Code + opencode only, not a cross-harness fit; the value is already
> covered by the Task/Agent tool + workflow swarms). rig-cli #53 (agents provisioning) was
> fully **reverted** by #55 — zero agent code remains. This document is retained **only** as
> the design record of a skipped feature — **do not implement it.** See the agents-skip
> decision (rig-cli #77).

Status: SKIPPED (design record only). Tracks ledger task #43.
Owner: rig-cli (`riglib/`). Source catalog: agent-tools.

## Goal

Let rig provision reusable **sub-agent definitions** (Claude Code `.claude/agents/*.md`)
the same way it already provisions skills and agent-hooks: a curated catalog in
agent-tools, selected by a committed/global `rig.yaml` block, applied idempotently into
the harness's agent-discovery directory, and drift-checked two ways.

Two scopes, one mechanism (rig already has both, by config *location*, not a `scope` key):

- **GLOBAL** — a machine-wide sub-agent library installed into `~/.claude/agents`,
  declared in `~/.config/rig/config.yaml`. Carried across every repo.
- **REPO-LOCAL** — project-specific task specialists committed with the repo, installed
  into `<repo>/.claude/agents`, declared in the repo's `./rig.yaml`.

v1 ships **Claude Code only**. Cross-harness mapping (opencode/gemini/copilot/codex) is
designed below but deferred — see "Cross-harness handling".

### Why `subagents`, not `agents`

The name `agents` is already taken in rig. `config_schema.py:509` registers an `agents_md`
block and `runner.py` has `_do_provision_agents_symlink` / `resolve_agents_md` — these own
the **AGENTS.md (canonical) + CLAUDE.md (symlink)** instruction-file invariant, an entirely
different concept from `.claude/agents/*.md` sub-agents. To avoid a confusing overload, the
new category, config block, scanner, handler, and registry all use the word **`subagents`**.

## What a Claude Code sub-agent is (authoritative — current CC docs, verified 2026-06-21)

A single Markdown file with YAML frontmatter; the Markdown body is the sub-agent's system
prompt.

```markdown
---
name: code-reviewer            # required. lowercase+hyphens. invocation identity (NOT the filename)
description: Reviews diffs...   # required. the orchestrator reads this to decide WHEN to delegate
tools: Read, Glob, Grep         # optional. inherits all tools if omitted
model: sonnet|opus|haiku|inherit|<full-id>   # optional. default: inherit
---
<markdown system prompt = the sub-agent's operational contract>
```

Optional frontmatter CC also accepts (rig copies the file verbatim, so these pass through
untouched): `disallowedTools`, `permissionMode`, `maxTurns`, `skills`, `mcpServers`,
`hooks`, `memory`, `background`, `effort`, `isolation`, `color`, `initialPrompt`.

**Locations + runtime precedence (highest→lowest), enforced by CC, NOT by rig:**
managed-settings `.claude/agents/` → `--agents` CLI JSON → **project `.claude/agents/`** →
**user `~/.claude/agents/`** → plugin `agents/`. Same-`name` conflict: higher location
wins. Both project and user dirs are scanned **recursively** (subfolders allowed but do not
affect identity; identity = the `name` field only). Duplicate `name` in one scope → one
silently discarded.

**Invocation:** automatic (CC matches `description`), explicit (`@agent-<name>`), or
whole-session (`claude --agent <name>`). The Agent/Task tool's `subagent_type` is the
`name`; the SubagentStart hook receives it as `agent_type`. CC **does** ship built-in
subagents (`Explore`, `Plan`, `general-purpose`, plus `claude-code-guide` and
`statusline-setup`); custom `.claude/agents/*.md` definitions **add** to that built-in set,
they are not the only agents.

**Implication for rig:** unlike skills, the install dir IS the discovery dir. There is no
"install into `~/.agents/...` then symlink into the harness dir" split for CC. One
`copy_subagent` action suffices; no `link_*_harness` second action.

## Source catalog layout (agent-tools)

Flat single-file per sub-agent, mirroring the on-disk shape CC itself ships:

```
agent-tools/subagents/<name>.md   # frontmatter + body. <name> == frontmatter `name` (validated at scan)
```

Rationale for flat (not dir-per-agent like agent-hooks): a CC sub-agent *is* one `.md` with
no companion assets. A directory buys nothing for v1 — switch to `subagents/<name>/<name>.md`
only when companion assets or per-harness source variants become real. No `by-type/`
grouping in v1 — flat universal set (`group=""`).

The `_looks_like_agent_tools` guard (`catalog.py:96`, keys on `skills/` AND `agent-hooks/`)
**stays unchanged** — adding `subagents/` does not gate source recognition (backward compat).

## Scanner (`catalog.py`)

Add `_scan_subagents()` mirroring `_scan_skills` but flat, carrier = the file. Reuse
`_read_skill_description` (`catalog.py:100`) — CC frontmatter uses the same `description:`
line, no yaml import (AGENTS.md hard rule). Register the call in `Catalog.scan()` after
`_scan_agent_hooks()` (`catalog.py:149`). Extend the category-enum comment at `catalog.py:48`
to include `subagents`. Item: `name=md.stem, category="subagents", group="",
description=_read_skill_description(md), path=md, default_enabled=True`.

**Frontmatter validation (fail-closed at scan):** require `name` and `description`. If
`name` present and `!= md.stem`, error with the file path (identity ambiguity). If
`description` missing, error (orchestrator can't delegate). Loud catalog error, not a
silent skip.

## Apply — Claude Code (the v1 deliverable)

### Target resolution (the GLOBAL-vs-LOCAL switch — no new code path)
- `_BUILTIN_TARGETS` (`plan.py:48`): add `"subagents": "~/.claude/agents"`.
- `_DEFAULTS_KEY` (`plan.py:54`): add `"subagents": "subagents_target"`.
- `_resolve_target` already does `cat.target → defaults.subagents_target →
  _BUILTIN_TARGETS["subagents"]`, then `_expand`.
- `_expand` (`plan.py:99`) is the entire global-vs-local mechanism: `~`/`$VAR`/absolute →
  absolute machine path (**GLOBAL**, default `~/.claude/agents`); a *relative* target (e.g.
  `.claude/agents`) → anchored at `repo_root` (**REPO-LOCAL**, travels with the repo). No
  separate code for the local path.

### One action per enabled item
In `plan.build()`, after the agent_hooks block (~`plan.py:417`), iterate
`catalog.by_category("subagents")`, gate by `_item_enabled(sa, item, type_enabled=False)`,
and append `Action(kind="copy_subagent", category="subagents", item=item.name,
source=item.path, target=subagents_target / item.path.name)`. `_item_enabled` (`plan.py:208`)
gives precedence for free (`items.<name>.enabled → enable/disable → all → default_enabled`).
Add `copy_subagent` to the `Action.kind` docstring (`plan.py:78`).

### Handler (`actions/runner.py`)
A single-file write (not a tree copy), reusing the `fsutil.write_file` primitive
`_do_install_agent_hook` already uses (`runner.py:303`): mkdir the target parent, then
`fsutil.write_file(action.target, action.source.read_text(), on_conflict)`. Register in
`_HANDLERS` (`runner.py:3985`): `"copy_subagent": _do_copy_subagent`. Conflict policy reuses
the shared `on_conflict` (skip/backup/overwrite).

### Idempotence
`rig apply` twice produces identical results: `write_file` re-writes only on byte difference;
no hashing state file.

## Precedence (resolved end-to-end)

**rig config (which sub-agents get written, and where):** per-item
`subagents.items.<name>.enabled` (repo `./rig.yaml` beats global config, more specific
layer) → `enable`/`disable` lists → `all:` (opt-out default true) → catalog
`default_enabled`.

**CC runtime (which `name` wins on a disk collision):** rig does NOT enforce — it writes
files; CC's own precedence applies (project `.claude/agents` > user `~/.claude/agents` >
plugin). Installing the same `name` both globally and repo-locally lets the repo-local copy
win at runtime — the intended shadowing composition.

## Drift (`drift.py`)

- `action.kind` switch (`drift.py:107`): add the `copy_subagent` arm + record declared
  `(dir, filename)`.
- `_check_copy_subagent`: **missing** if target absent; **modified** on byte diff (file
  compare, not `dirs_identical`).
- `_extras_subagents` mirroring `_extras_skills` (`drift.py:184`): flag undeclared `*.md` as
  "extra". **Caution — the target may hold hand-authored / `/agents`-created sub-agents**
  (esp. GLOBAL `~/.claude/agents`). Gate the extras scan behind category-enabled at
  `cli.py:727` (same gate skills use). When enabled, undeclared `.md` is a soft "extra" note;
  rig never deletes it (no `clean` auto-delete in v1).

## Config schema (`config_schema.py`) + CLI surface

Add `_SUBAGENTS_BLOCK` mirroring `_AGENT_HOOKS_BLOCK` (`config_schema.py:196`) — leaves
`enabled`(bool,true) / `target`(str,`~/.claude/agents`) / `all`(bool,true) / `enable`(array) /
`disable`(array), `open_map="items"` keyed by agent name. Wiring (each changes together, sync
test enforces):
- `BLOCKS` (`config_schema.py:499`): add `"subagents": _SUBAGENTS_BLOCK`.
- `_DEFAULTS_BLOCK` (`config_schema.py:146`): add `subagents_target` leaf.
- `config.py:31` `_VALID_TOP_KEYS`: add `"subagents"`.
- `config.py:51` `_VALID_CATEGORIES`: add `"subagents"`.
- `config.py` `validate()`: add `_validate_subagents(...)` mirroring `_validate_agent_hooks`
  (reject-unknown-keys; bool `enabled`/`all`; str `target`; `items` map w/ per-item bool
  `enabled`; str-array `enable`/`disable`).
- `plan.py:332` `_validate_item_names` flat-items loop: extend `for cat_name in
  ("agent_hooks", "mcp")` → `(..., "subagents")` so a typo fails closed with did-you-mean.
- `layers.py:29` `_CATEGORY_LAYER`: add `"subagents": GLOBAL`. Known wart (shared with
  skills): a relative repo-local target still classifies GLOBAL in `rig status`. Acceptable v1.
- Regenerate `schema/rig.schema.json` (`rig schema --write`).

### What a user writes
GLOBAL (`~/.config/rig/config.yaml`), opt-out all: `subagents: {enabled: true, all: true}`
→ writes every `agent-tools/subagents/*.md` into `~/.claude/agents`.
REPO-LOCAL (`./rig.yaml`): `subagents: {target: .claude/agents, all: false, items:
{code-reviewer: {enabled: true}}}` — relative target → repo-local, travels with the repo.

## Edge cases
- Malformed catalog file (missing `name`/`description`, or `name != stem`): scan-time error
  naming the file. Loud, not a silent skip.
- Shared global dir pollution: extras scan gated on category-enabled; rig never deletes.
- Repo-local conflict with a real existing `.claude/agents/<name>.md`: governed by
  `on_conflict` (skip/backup/overwrite); default does not clobber without backup.
- `name` vs filename divergence: forbidden in catalog (validated) — no upside, only a
  config/runtime mismatch.
- Duplicate `name` across global + repo-local: allowed; CC runtime precedence resolves it
  (the shadowing feature).
- No versioning / no auto-update: `rig apply` is the update trigger.

## Out of scope (v1)
Cross-harness provisioning (designed, deferred), `by-type/` agents, agent-teams, `clean`
auto-delete, secrets injection.