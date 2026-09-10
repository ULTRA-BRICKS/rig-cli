# Review addendum: rig-cli/docs/specs/rig-linter-bundles.md

Source revision: `97023cfd0c1ec4c8ba5c9e6a7666e26391ecc6ad`.

This source document is retained with its original draft/approved/skipped status. This addendum does not approve previously skipped work. Root `rig-cli/SPEC.md` describes Evolve, not the entire rig ecosystem.

Current recovery contract: [Declarative provisioning, reproducible installation and honest drift](../provisioning.md). Governing epic: https://github.com/alex-mextner/rig-cli/issues/521.

## Example and revalidation

The operator disables a managed integration and runs reconciliation. Only owned artifacts are removed; manual files are preserved. The same declared state drives apply and status. A managed-file comment names the supported configuration command rather than merely saying do not edit.

## Source quotations

> Чтобы rig сам делал всё, что нужно.

Source **Q019**; compiled-fragment-attribution; rig-zerobug/rig-fable session 2026-09-04..07; agent=нет; PDF page(s) 44. Thematic recovery requirement, not retroactive authorship of this entire specification.

**Context fragment:** do not interpret as a standalone approval.

<details><summary>Original wording (before editorial correction)</summary>

> риг сам делал все что нужно

</details>

> В белых списках всех агентов.

Source **Q018**; compiled-fragment-attribution; rig-zerobug/rig-fable session 2026-09-04..07; agent=нет; PDF page(s) 43. Thematic recovery requirement, not retroactive authorship of this entire specification.

**Context fragment:** do not interpret as a standalone approval.

<details><summary>Original wording (before editorial correction)</summary>

> в белых списках всех агентов

</details>

## Linked acceptance backlog

- [rig-cli#5](https://github.com/alex-mextner/rig-cli/issues/5) — Enable repo security settings at init/apply (Dependency Graph + vuln-alerts + secret-scanning)
- [rig-cli#6](https://github.com/alex-mextner/rig-cli/issues/6) — Auto-mode is LOCAL — gitignore .claude/settings.json on apply (un-commit the one in d87257a)
- [rig-cli#7](https://github.com/alex-mextner/rig-cli/issues/7) — Rollout wave-2 — bots + review-cli/rig-cli/agent-tools themselves
- [rig-cli#8](https://github.com/alex-mextner/rig-cli/issues/8) — Model-currency follow-ups — finish models.yaml manifest + cron checker (rig-provisioned)
- [rig-cli#59](https://github.com/alex-mextner/rig-cli/issues/59) — rig.yaml template ships a dead `mcp.items.review` slot → `rig status` on the repo's OWN config exits 4
- [rig-cli#63](https://github.com/alex-mextner/rig-cli/issues/63) — missing-target scan ignores configured settings_path / harness kind (hardcoded ~/.claude/settings.json)
- [rig-cli#81](https://github.com/alex-mextner/rig-cli/issues/81) — rig init: scaffold+preview by default, stop presumptuously applying with no user signal
- [rig-cli#100](https://github.com/alex-mextner/rig-cli/issues/100) — rig must own Claude Code permissions (deny/ask/allow) — outer belt, not hand-edits
- [rig-cli#105](https://github.com/alex-mextner/rig-cli/issues/105) — rig apply: keep provisioned tools fresh via their scripts/deploy.sh
- [rig-cli#117](https://github.com/alex-mextner/rig-cli/issues/117) — Add dev CLI permission and rig scripts support
- [rig-cli#118](https://github.com/alex-mextner/rig-cli/issues/118) — Provision dev CLI allowlist and configuration
- [rig-cli#130](https://github.com/alex-mextner/rig-cli/issues/130) — Add rig autonomous mode provisioning
- [rig-cli#142](https://github.com/alex-mextner/rig-cli/issues/142) — rig: provision auto-mode self-merge classifier carve-out (harness.self_merge)
- [rig-cli#145](https://github.com/alex-mextner/rig-cli/issues/145) — Run requested shell echo command
- [rig-cli#146](https://github.com/alex-mextner/rig-cli/issues/146) — Run requested shell echo command
- [rig-cli#168](https://github.com/alex-mextner/rig-cli/issues/168) — rig status: detect hand-reordered opencode permission.bash keys (deny before catch-all)
- [rig-cli#177](https://github.com/alex-mextner/rig-cli/issues/177) — fix(config): sorted(set(...)) crashes on mixed str/int mapping keys
- [rig-cli#187](https://github.com/alex-mextner/rig-cli/issues/187) — rig-cli: default rg permission allowlist grants ripgrep --pre, enabling arbitrary subprocess execution
- [rig-cli#189](https://github.com/alex-mextner/rig-cli/issues/189) — rig-cli: dev-server schema fields (ready_url, process_matchers, requires_server) are documented but not consumed by dev-cli
- [rig-cli#192](https://github.com/alex-mextner/rig-cli/issues/192) — rig-cli: pi permission-guard extension is silently dropped when settings_path triggers multi-harness fan-out narrowing
- [rig-cli#193](https://github.com/alex-mextner/rig-cli/issues/193) — rig-cli: pi permissions.deny, ask, and settings_path config values are accepted but silently discarded
- [rig-cli#195](https://github.com/alex-mextner/rig-cli/issues/195) — rig-cli: pi extension apply can lose the extension backup path when the policy write fails after it
- [rig-cli#199](https://github.com/alex-mextner/rig-cli/issues/199) — dev config schema: ready_url / process_matchers / requires_server are advertised but not consumed by dev-cli yet
- [rig-cli#200](https://github.com/alex-mextner/rig-cli/issues/200) — config: rig hand-maintains a copy of dev-cli's scripts/dev config contract — will drift again
- [rig-cli#206](https://github.com/alex-mextner/rig-cli/issues/206) — env: add rig-managed shell environment variables (COLORTERM=truecolor)
- [rig-cli#219](https://github.com/alex-mextner/rig-cli/issues/219) — tmux.focus_events.enabled missing from rig setup / rig config-web discoverability registry
- [rig-cli#227](https://github.com/alex-mextner/rig-cli/issues/227) — Stack/profile-targeted policy mutation across registered repositories
- [rig-cli#235](https://github.com/alex-mextner/rig-cli/issues/235) — First-class toolchain migration plans (ESLint/Biome/Prettier → Oxc) with semantic preservation
- [rig-cli#237](https://github.com/alex-mextner/rig-cli/issues/237) — Policy packs should bundle rationale/examples/docs alongside enforceable settings
- [rig-cli#238](https://github.com/alex-mextner/rig-cli/issues/238) — Policy source trust/pinning model for executable carriers and agent tooling
- [rig-cli#245](https://github.com/alex-mextner/rig-cli/issues/245) — Repository policy tags/selectors must be first-class config metadata, not path-name conventions
- [rig-cli#246](https://github.com/alex-mextner/rig-cli/issues/246) — Policy dry-run must include proposed config-file diff, not only resulting action plan
- [rig-cli#249](https://github.com/alex-mextner/rig-cli/issues/249) — Config provenance must explain effective value at nested-key granularity
- [rig-cli#250](https://github.com/alex-mextner/rig-cli/issues/250) — Policy compatibility checks before upgrading providers/tools
- [rig-cli#307](https://github.com/alex-mextner/rig-cli/issues/307) — rig init: interactive wizard's exit code doesn't reflect a mid-Apply failure
- [rig-cli#309](https://github.com/alex-mextner/rig-cli/issues/309) — claude-rotate: register in rig tools.items with repo override to ~/.files
- [rig-cli#311](https://github.com/alex-mextner/rig-cli/issues/311) — config-web: narrative why-content and inline illustrations per config option
- [rig-cli#320](https://github.com/alex-mextner/rig-cli/issues/320) — github_browser: discussions/projects may be misclassified as UI-only (has stable REST fields); tmux plugin clone has no concurrent-apply lock
- [rig-cli#332](https://github.com/alex-mextner/rig-cli/issues/332) — permissions: pre-allow gh by default with a config opt-out
- [rig-cli#350](https://github.com/alex-mextner/rig-cli/issues/350) — Review tg_ctl null-schema diff
- [rig-cli#355](https://github.com/alex-mextner/rig-cli/issues/355) — harness: write the auto/permission mode for codex, opencode and omp, not only claude-code
- [rig-cli#365](https://github.com/alex-mextner/rig-cli/issues/365) — Every rig command fails machine-wide: the global config declares skills.known, a key rig 0.34.1 does not have
- [rig-cli#367](https://github.com/alex-mextner/rig-cli/issues/367) — harness.require_tmux: provision claude-rotate's tmux guard (warn|wrap|off) machine-wide
- [rig-cli#371](https://github.com/alex-mextner/rig-cli/issues/371) — schema: route the machine-wide known lists (skills.known, agent_hooks.known, mcp.known) to the GLOBAL config per option, not per category
- [rig-cli#372](https://github.com/alex-mextner/rig-cli/issues/372) — schema: route the machine-wide known-lists (skills, agent_hooks, mcp) to the GLOBAL config layer via a per-option layer override
- [rig-cli#376](https://github.com/alex-mextner/rig-cli/issues/376) — config editors: unlocked read-modify-write of ~/.config/rig/config.yaml can lose a concurrent edit
- [rig-cli#377](https://github.com/alex-mextner/rig-cli/issues/377) — config-web: Global-tab edits need an agent-tools source resolvable without the repo overlay
- [rig-cli#383](https://github.com/alex-mextner/rig-cli/issues/383) — Review Claude config-dir fan-out diff
- [rig-cli#402](https://github.com/alex-mextner/rig-cli/issues/402) — Review PR171 pi provisioning diff adversarially
- [rig-cli#406](https://github.com/alex-mextner/rig-cli/issues/406) — Review permission receipt revocation diff
- [rig-cli#426](https://github.com/alex-mextner/rig-cli/issues/426) — Block raw gh issue and Linear when task-cli is configured
- [rig-cli#446](https://github.com/alex-mextner/rig-cli/issues/446) — Provision omp webSearchExclude via rig so search skips Perplexity signup wall
- [rig-cli#447](https://github.com/alex-mextner/rig-cli/issues/447) — Provision omp webSearchExclude via rig
- [rig-cli#459](https://github.com/alex-mextner/rig-cli/issues/459) — Disabled bun.enabled leaves managed bunfig block; status hides disk→config drift
- [rig-cli#460](https://github.com/alex-mextner/rig-cli/issues/460) — Unscoped rig apply refuses unknown top-level bun in global config
- [rig-cli#462](https://github.com/alex-mextner/rig-cli/issues/462) — rig apply fans Bash(rig:*) and hooks into every claude-rotate settings.json
- [rig-cli#490](https://github.com/alex-mextner/rig-cli/issues/490) — eval-tool osascript spawns bypass the bash-only permissions guard, letting TCC/Automation prompts recur
- [rig-cli#491](https://github.com/alex-mextner/rig-cli/issues/491) — Deny raw osascript spawned from the eval tool (TCC/sandbox bypass)
- [rig-cli#492](https://github.com/alex-mextner/rig-cli/issues/492) — Add rig-managed zsh tab-completion generation

## Preserved specification

# Rig reusable linter carriers

Status: implementation contract for directory `linters.bundles` plus source-file-backed `linters.items`.

## Problem

Today's `linters.items` embeds exact text in `rig.yaml`. That is fine for a one-off config but poor for organization-wide presets. It also cannot represent a vendored local plugin directory such as anti-slop.

`agent-tools` now has two canonical source forms:

- `vendor/anti-slop/skills/install-anti-slop/assets/anti-slop/` — pinned subrepo directory payload;
- `linters/oxc/` — canonical Oxlint/Oxfmt config files.

Rig should reconcile both without network downloads and without duplicating their bytes into every project YAML.

## Contract

```yaml
linters:
  enabled: true

  bundles:
    anti-slop:
      source: vendor/anti-slop/skills/install-anti-slop/assets/anti-slop
      target: tools/oxlint/anti-slop

  items:
    oxlint:
      tool: oxlint
      role: linter
      path: oxlint.config.ts
      source: linters/oxc/oxlint.config.ts

    oxfmt:
      tool: oxfmt
      role: formatter
      path: .oxfmtrc.jsonc
      source: linters/oxc/.oxfmtrc.jsonc
```

### `linters.bundles.<label>`

- `source`: required repo-relative directory under resolved `agent_tools_source`;
- `target`: required repo-relative destination directory under the target repo;
- `enabled`: optional bool, default true.

### `linters.items.<label>`

Keep today's `tool`, `role`, `path`, `enabled`. Desired bytes may come from **exactly one** of:

- `content`: existing inline exact text;
- `source`: a repo-relative regular file under resolved `agent_tools_source`.

Require one and only one of `content`/`source`. This preserves backward compatibility while allowing reusable presets.

All source paths are anchored to `agent_tools_source`. A committed project config cannot read arbitrary machine files.

## Safety and desired state

For both source files and bundles reject absolute paths, `..`, backslashes, `.git`, leading/trailing whitespace, and symlink traversal. Missing source is an error. For anti-slop specifically, an uninitialized Git subrepo therefore fails closed instead of silently creating an empty plugin.

A bundle is a deterministic regular-file tree. Symlinks, device files, sockets, and other non-regular entries are invalid. Exact means the complete tree matches, including absence of extra target files.

A source-file item reads exact bytes from its declared source and otherwise uses the existing `provision_linter_config` conflict/drift semantics.

## Conflict semantics

Both carrier forms use Rig's existing `skip | overwrite | backup` policy.

For bundles, the directory is one conflict unit: `backup` moves the complete previous target to a unique sibling and then copies the source; `overwrite` replaces the whole tree so stale files disappear; `skip` leaves drift visible.

For source-file items, existing file behavior remains unchanged. A correct re-apply is a no-op and creates no backup.

## Plan / apply / status parity

- source-file items still emit `provision_linter_config`; plan resolves their source bytes once and carries the desired content, so apply/status compare the same bytes;
- bundles emit `provision_linter_bundle` with a shared apply/drift classifier;
- disabling a carrier never surprise-deletes the old target.

## Schema

`linters` stays closed with named `items` and `bundles` maps. Bundle items are closed. Linter items add optional `source` and change the requirement from mandatory `content` to an exclusive `content XOR source` semantic validation.

The published JSON schema should express this with `oneOf` where practical, while runtime validation remains authoritative and fail-closed.

## anti-slop + Oxc consumer

```text
agent-tools/
  vendor/anti-slop/
    skills/install-anti-slop/assets/anti-slop/
  linters/oxc/
    oxlint.config.ts
    .oxfmtrc.jsonc
```

becomes:

```text
target-repo/
  tools/oxlint/anti-slop/
  oxlint.config.ts
  .oxfmtrc.jsonc
```

The standard JS/TS toolchain is Oxc: Oxlint + Oxfmt. The canonical Oxlint preset enables every anti-slop rule, type-aware mode, `typescript/no-unsafe-type-assertion`, `typescript/no-unnecessary-type-assertion`, `typescript/no-non-null-assertion`, and strict `typescript/ban-ts-comment`. `tsc --noEmit` remains a separate compiler gate.

## Non-goals

- no global binaries;
- no package-manager mutation in linter carriers;
- no network download during apply;
- no anti-slop special case in Rig core;
- no AST merge of arbitrary existing Oxlint config.

## Tests required before merge

- validation for both carriers, including `content XOR source`;
- POSIX/Windows containment and `.git` rejection;
- missing/uninitialized subrepo and nested source symlink rejection;
- source-file create/update/skip/backup and byte parity;
- bundle create, exact no-op, binary/text preservation, extra/missing/changed file drift;
- bundle skip/overwrite/backup conflict behavior;
- target leaf/parent symlink rejection;
- apply/status classifier parity;
- area/item/bundle disable gating;
- clean-room round trip under temporary HOME;
- published JSON schema sync.
