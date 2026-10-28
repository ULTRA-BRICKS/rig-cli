# `rig.yaml` — declarative config schema

`rig.yaml` is the committed, reproducible source of truth for a repo's setup. `rig apply`
reads it, computes the diff vs disk, and converges. `rig init` writes it.

**Cascade (by location, no scope flag):**

1. **Global** — `~/.config/rig/config.yaml` (`$XDG_CONFIG_HOME/rig/config.yaml`).
2. **Per-repo** — `./rig.yaml` (overrides global; committed by default).

Dicts merge recursively (per-repo wins); lists and scalars replace wholesale. The result
is validated **fail-closed** before any write: unknown top-level keys, unknown categories,
bad enum values, and unknown item names abort.

**Round-trip invariant:** `init` → `rig.yaml` → `apply --config` produces the same plan.
`riglib/state.py` (`SetupState`) is the single serializer; `riglib/config.py` the loader.

## Reference parts

This file holds the overview, the JSON Schema, resolution rules, [`defaults`](#defaults), [`skills`](#skills),
`agent_hooks`, `git_hooks`, `ci`, `mcp`, [Provenance](#provenance), `mode`, [`env`](#env), [`internal_dev`](#internal_dev),
the `rig setup` / `rig config` editing contract and validation. Every other block is documented in a part:

| Part | Blocks |
| --- | --- |
| [harness-and-permissions.md](config-schema/harness-and-permissions.md) | `harness`, `permissions`, `harness_wrappers`, `rig harness` hosted sessions |
| [project-tooling.md](config-schema/project-tooling.md) | `scripts`, `dev`, `models`, `agents_md`, `ship_delegator`, `lint_checks`, `lint`, `linters`, `project_tools`, `ecosystem_readme`, `gitignore`, `bun`, `git`, `profile` |
| [tasks.md](config-schema/tasks.md) | `task` (tracker, routing, workspace task routing) |
| [github-and-tmux.md](config-schema/github-and-tmux.md) | `github`, external PR review policy, `tmux`, `tmux.recovery`, `spotlight`, `fj` |
| [machine-services.md](config-schema/machine-services.md) | `tools`, `tg_ctl`, `tcc_watch`, `tgctl_reaper`, `sysmon`, `tracked_files` |

### Moved sections

Links written before the split (`config-schema.md#tmux` and friends) still land here; each entry points at where the section now lives.

- <a id="fj"></a>[`fj`](config-schema/github-and-tmux.md#fj)
- <a id="tmux"></a>[`tmux`](config-schema/github-and-tmux.md#tmux)
- <a id="ship_delegator"></a>[`ship_delegator`](config-schema/project-tooling.md#ship_delegator)
- <a id="git"></a>[`git`](config-schema/project-tooling.md#git)
- <a id="models"></a>[`models`](config-schema/project-tooling.md#models)
- <a id="agents_md"></a>[`agents_md`](config-schema/project-tooling.md#agents_md)
- <a id="tracked_files"></a>[`tracked_files`](config-schema/machine-services.md#tracked_files)
- <a id="sysmon"></a>[`sysmon`](config-schema/machine-services.md#sysmon)
- <a id="signal_scope--each-agent-session-can-signal-only-its-own-process-tree-704"></a>[`signal_scope`](config-schema/harness-and-permissions.md#signal_scope--each-agent-session-can-signal-only-its-own-process-tree-704)
- <a id="rig-harness--hosted-harness-sessions-for-omp-claude-code-codex-opencode"></a>[`rig harness`](config-schema/harness-and-permissions.md#rig-harness--hosted-harness-sessions-for-omp-claude-code-codex-opencode)

## The JSON Schema (enforced + editor-facing)

This document is the human-readable reference; the machine-readable schema is
**`schema/rig.schema.json`** — a Draft-07 JSON Schema **generated from one in-code registry**
(`riglib/config_schema.py`) and committed. Both layers ride the same source:

- **Editors** read the committed file via the `# yaml-language-server: $schema=schema/rig.schema.json`
  modeline at the top of every `rig.yaml` (and the global config) → live key completion and
  unknown-key / bad-value squiggles, the *same* rules `rig apply` enforces.
- **`rig apply` / `rig config set` / `rig status` / `rig init`** validate against the registry
  (`riglib/config.py`, which mirrors the schema). A rejection is a **3-part error** — *what* is
  wrong, *why* (with the **schema path**, e.g. `harness.auto_mode`, and a pointer into
  `schema/rig.schema.json`), and *how to fix it* — and exits `2`.
- **`rig schema`** prints the schema; **`rig schema --check`** fails if the committed file drifted
  from the registry (a CI-usable gate); **`rig schema --write`** regenerates it. A test
  (`tests/test_config_schema.py`) keeps the file, the registry, and `config.validate`'s key set in
  lockstep, so the three never disagree.

**Strict by default — an unknown key is rejected, not ignored.** Fixed rig-owned blocks are closed
(`additionalProperties: false`): a typo'd key (`aut_mode`, `enabld`) fails loudly with the schema
path, rather than silently having no effect. The open maps are top-level `scripts:`, the
catalog-keyed `items:` (under `skills.by_type`, `agent_hooks`, `ci`, `mcp`), `fragments:` (under
`git_hooks.dispatcher`), and the schema-shaped `dev.e2e.jobs:` map. A bad catalog item *name* is caught later as a
catalog/unknown-item error (exit `4`), not a schema typo.

## CLI Exit Codes

These stable exit classes are also printed by `rig --help`:

| Code | Meaning |
| --- | --- |
| `0` | success |
| `1` | internal error: an unexpected failure or bug |
| `2` | invalid config: malformed value, type, or unknown key |
| `3` | drift: `rig status` found config/disk drift |
| `4` | unknown item: config names a catalog item that does not exist or was removed |
| `5` | missing target: config references a path or binary that is gone on disk |
| `6` | not a git repository: a repo-scoped command was run outside a repo |
| `7` | repo corrupt: a working checkout's git config is broken |
| `127` | missing dependency: a required external tool is not installed |

`8` is retired and never reused: it was `rig codex update`'s failure code, and that updater
moved to harness-cli as `harness codex update`.

## Top-level shape

```yaml
version: 1                      # schema version (int, required; only 1 supported)

defaults:                       # cross-category fallback targets/policy
  skills_target: ~/.agents/skills
  hooks_target: ~/.claude/hooks
  ci_target: .github/workflows
  mcp_target: ~/.claude/mcp
  on_conflict: skip | overwrite | backup   # what apply does when a target exists (default: backup)

agent_tools_source: ~/xp/agent-tools   # the agent-tools checkout to apply FROM (default: auto-detect)

scripts: { ... }              # repo-level named commands consumed by the standalone `dev` CLI
dev: { ... }                  # repo-level dev server/e2e lifecycle metadata consumed by `dev`
skills: { ... }
agent_hooks: { ... }
git_hooks: { ... }
ci: { ... }
mcp: { ... }
mode: { ... }                 # GLOBAL operating mode; autonomous review/fix/quorum/escalation policy
harness: { ... }              # agent harness auto/permission provisioning (auto-mode)
permissions: { ... }          # per-harness permissions layer (allowlist + deny/ask baselines), default ON
models: { ... }               # daily model-freshness checker schedule (launchd / systemd --user timer)
agents_md: { ... }            # AGENTS.md (canonical) + CLAUDE.md (symlink), default ON
github: { ... }               # repo settings: ruleset/merge/ghas/actions via gh api + browser via agent-browser, default ON
tmux: { ... }                 # rig-managed tmux config (generate + migrate ~/.tmux.conf), opt-in
gitignore: { ... }            # rig-managed block in the GLOBAL git excludesfile (ignores **/.claude/worktrees/ + the Spotlight sentinel in EVERY repo), default ON
git: { ... }                  # GLOBAL git settings via `git config --global` (identity, pull/branch defaults); GLOBAL layer ONLY
profile: { ... }              # `rig profile`: the PRIVATE repo this global config syncs through; GLOBAL layer ONLY
tg_ctl: { ... }               # tg-ctl inbound daemon as a macOS boot LaunchAgent, default ON (macOS-only)
tcc_watch: { ... }            # TCC dialog-watcher daemon (Phase 1 detection-only), a macOS boot LaunchAgent, default ON (macOS-only)
tgctl_reaper: { ... }         # tgctl-reaper periodic orphan-sweep LaunchAgent (StartInterval), default ON (macOS-only)
sysmon: { ... }               # sysmon resource monitor + tg alerts + auto-heal + session restore (LaunchAgent / systemd --user), default ON
```

If `agent_tools_source` is omitted, rig resolves it from `$RIG_AGENT_TOOLS_SOURCE`, then
the default candidates (`~/xp/agent-tools`, `~/work/agent-tools`, `~/agent-tools`).

## `scripts`

Project-local named commands consumed by the `dev` CLI and portable hooks. `rig` validates that
this top-level key is a mapping of string names to either a non-empty command string or a
`{cmd: "<non-empty string>"}` mapping (no other keys); command semantics are owned by the dev
helper that executes the script.

```yaml
scripts:
  test: uv run --with pytest pytest tests/
  server: pnpm run dev
  e2e: pnpm exec playwright test
```

## `dev`

Project-local dev/e2e lifecycle metadata consumed by the `dev` CLI. `rig` validates that this
top-level key is a mapping, then preserves it; fields such as `server`, `e2e`, `jobs`,
`logs_root`, and `artifacts_root` are interpreted by the dev helper, not by `rig apply`.

```yaml
dev:
  server:
    script: server
    ports: [5173]
  e2e:
    script: e2e
```

## Resolution rules (so the file stays terse)

- A category sets `enabled: false` to disable the whole category in one line.
- A category may set `all: true` and list exceptions in `disable: [...]`, **or** `all:
  false` and list only what is enabled in `enable: [...]`. Opt-out and opt-in are both
  expressible.
- Per-item overrides live under `items:` keyed by item name; absent items inherit the
  `all`/`enable`/`disable` decision.
- Targets resolve item → category `target` → `defaults.<x>_target` → built-in default.

---

## `defaults`

Cross-category fallback targets + the on-conflict policy. A category that does not set its own
`target` falls back to the matching `defaults.<x>_target`; `on_conflict` governs what `apply` does
when a target file/dir already exists.

```yaml
defaults:
  skills_target: ~/.agents/skills
  hooks_target: ~/.claude/hooks
  ci_target: .github/workflows
  mcp_target: ~/.claude/mcp
  on_conflict: backup            # skip | overwrite | backup
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `skills_target` | path | `~/.agents/skills` | default skills install dir |
| `hooks_target` | path | `~/.claude/hooks` | default agent-hooks dir; with `harness.kind: codex`, the effective default is `~/.codex/hooks` (or `$RIG_CODEX_HOME/hooks`) unless a custom hook descriptor target is configured |
| `ci_target` | path | `.github/workflows` | default CI workflows dir |
| `mcp_target` | path | `~/.claude/mcp` | default MCP config dir |
| `on_conflict` | `skip`/`overwrite`/`backup` | `backup` | what `apply` does when a target already exists |

`on_conflict` protects **user-authored** content. Files that carry a **rig provenance header** —
the ship delegator (`.claude/scripts/pr-ship.sh`) and the machine env file
(`agent-tools/env`) — are rig's own prior output: apply rewrites them **in place, no backup,
regardless of `on_conflict`** (like the managed marker blocks). Anything at those paths
*without* the header is treated as user content and gets the full policy. Corollary: lines you
hand-append **below** a rig header are lost on the next apply — the header marks the whole file
as rig's; put machine customization in `AGENT_TOOLS_ROOT` (env var, always wins) instead.

---

## `skills`

```yaml
skills:
  enabled: true
  target: ~/.agents/skills          # or ~/.claude/skills | ./.agents/skills | custom
  harness_link: true                # symlink each skill into the harness discovery dir
  harness_skill_dir: ~/.claude/skills   # override the per-harness default discovery dir
  universal:
    all: true                       # all universal skills (opt-out model)
    disable: [push-regularly]       # ...except these
  by_type:
    enable: [backend, cli]          # install these by-type bundles wholesale
    items:
      by-type/bot/russian-pluralization: { enabled: false }   # fine-grained override
  by_stack:
    disable: [by-stack/mobile/swift/swift-concurrency]        # drop a selected skill
    items:
      by-stack/frontend/ts/react/vercel-react-patterns: { enabled: true }   # force one in
  plugins:
    items:
      superpowers: { marketplace: claude-plugins-official }   # resolve a Claude Code plugin's skills
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | install skills at all |
| `target` | path | `~/.agents/skills` | where SKILL.md dirs are copied |
| `harness_link` | bool | `true` | also symlink each installed skill into the harness's skill-discovery dir |
| `harness_skill_dir` | path | per-harness default | where the harness discovers skills (claude-code: `~/.claude/skills`; codex: `~/.codex/skills`, or `$RIG_CODEX_HOME/skills`). An explicit value pins skill links to that one directory, overriding `harness.kinds` skill-link fan-out, and forces a directory link even for a native-discovery or instruction-file harness |
| `universal.all` | bool | `true` | enable all universal skills (opt-out) |
| `universal.disable` / `universal.enable` | list[str] | `[]` | deltas on `all` |
| `by_type.enable` | list[str] | the detected project type | which `by-type/<kind>` bundles to install whole |
| `by_type.items.<by-type/kind/name>.enabled` | bool | inherited | per-skill override |
| `by_stack.disable` | list[str] | `[]` | drop a `by-stack/<l1>/<lang>[/<fw>]/<name>` item that would otherwise be selected |
| `by_stack.items.<by-stack/l1/lang[/fw]/name>.enabled` | bool | inherited | force a skill in/out regardless of stack match |
| `plugins.items.<plugin-name>.marketplace` | str | — (required) | resolve `<plugin-name>`'s `skills/*/SKILL.md` dirs against the live Claude Code `~/.claude/plugins/installed_plugins.json` and symlink each into `target` — see [Claude Code plugin-marketplace-sourced skills](#claude-code-plugin-marketplace-sourced-skills-skillsplugins) |
| `known` | list[str] | `[]` | skill dir names on disk that rig does NOT manage (hand-installed packs) — `rig status` reports them as known, not as drift. GLOBAL-layer (the skills dir is machine-wide): the wizard / config-web / `config set --global` write it to `~/.config/rig/config.yaml`. See [Provenance](#provenance). A plugin-sourced skill declared under `plugins.items` does NOT need a `known` entry too — it is natively plan-declared, never undeclared drift. |

If `by_type.enable` is empty and the detected project type is known, that type's bundle is
auto-pulled.

`by_stack` items are selected by the repo's stack preset — the top-level `stack` config key
(`l1/lang[/framework]`) — a `by-stack/<l1>/<lang>[/<fw>]/<name>` skill is pulled in when its
stack path is a prefix of the declared one (hierarchical: `mobile/swift/swiftui` also inherits
`mobile/swift`-level skills — a MORE specific declared stack inherits its LESS specific
ancestors, not the other way round). When no `stack` is declared, rig falls back to the same
cheap signature-file guess `rig init` uses to seed one (`detect.detect_stack_preset`), mirroring
the `by_type` project-type auto-pull above (both fallbacks are silent — neither emits a plan
note when the guess fires). An
explicit `stack:` config value always wins over the guess; `by_stack.disable` / per-item
overrides apply the same either way.

### Harness skill discovery (why `harness_link`)

The agent harness lists/loads Skill-tool skills from its **own** location, not from `target`,
and harnesses split into three families by *how* they discover skills:

- **skills-directory harnesses** enumerate a directory of skill folders. rig makes an
  installed skill discoverable by symlinking `<skill_dir>/<skill> → <target>/<skill>`:
  - **claude-code** → `~/.claude/skills`
  - **codex** → `~/.codex/skills` (or `$RIG_CODEX_HOME/skills`; Codex CLI's native skills dir; codex does **not** read
    `~/.agents/skills`, so rig must link here or codex sees no skills). codex is *also* an
    instruction-file harness (`~/.codex/AGENTS.md`, or `$RIG_CODEX_HOME/AGENTS.md`) — the two
    are complementary.
    Ambient `CODEX_HOME` is ignored by design; use `RIG_CODEX_HOME` to point rig at a non-default
    persistent Codex user config root.
- **native-discovery harnesses** auto-load the default `target` (`~/.agents/skills`) directly,
  so a copied skill is already visible and rig links **nothing**. `rig status` reports
  *discovers natively* instead of a pointless symlink:
  - **opencode** → auto-loads `~/.agents/skills` (and `~/.claude/skills`) natively since ≥1.16
  - **omp** → its built-in `agents` discovery provider auto-loads `~/.agents/skills/<name>/SKILL.md` natively
- **instruction-file harnesses** have **no** per-skill directory; their guidance comes from a
  single global instruction file, not from a symlink. rig links **nothing** for these (it never
  invents a directory) and `rig status` reports the kind as *N/A — uses `<file>`* so the empty
  link area is explained, not silent. The [`agents_md`](config-schema/project-tooling.md#agents_md) area is repo-local
  AGENTS.md/CLAUDE.md provisioning, not a writer for these global files:
  - **pi** → `~/.pi/agent/AGENTS.md` (or `$PI_CODING_AGENT_DIR/AGENTS.md` when set)
  - **commandcode** → `~/.commandcode/AGENTS.md`

A skill copied into `~/.agents/skills` (the default `target`) is invisible to a skills-dir
harness until it is also present in the discovery dir. With `harness_link: true` (the
default), `rig apply` maintains an idempotent symlink for every enabled skill on a skills-dir
harness:

- an existing **correct** symlink is a no-op;
- a symlink to the **wrong** destination is re-pointed;
- a **real** (non-symlink) dir/file already at the path is **left untouched** — some skills
  are hand-authored real dirs (e.g. `h-reason`, `debate-swarm`), and rig must not clobber
  them. `rig status` reports a missing/wrong link as drift; a real dir is not flagged.

The discovery dir is keyed by the harness `kind` (defaulting to claude-code, or following
`harness.kind` when a `harness:` block pins one). Set `harness_link: false` to opt out, or
`harness_skill_dir` to point at a non-default location — an explicit `harness_skill_dir`
forces a directory link **even for an instruction-file harness** (you pointed at a real dir
on purpose).

The harness symlink is the **one action that does not consult `on_conflict`**: a wrong
symlink is always re-pointed (a symlink carries no user data to back up), and a real dir is
always left alone (no policy ever clobbers hand-authored content). `on_conflict` governs file
and directory *content*, which a discovery symlink has neither of.


### Claude Code plugin-marketplace-sourced skills (`skills.plugins`)

A Claude Code **plugin** (installed from a marketplace like `claude-plugins-official`) can ship
its own `skills/*/SKILL.md` dirs — e.g. the `superpowers` plugin's `brainstorming`,
`writing-plans`, `using-git-worktrees`, … These live under a **version-pinned** cache path,
`~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/skills/<name>/`, tracked by Claude
Code's own `~/.claude/plugins/installed_plugins.json`. A plugin auto-update changes that
`<version>` segment; a hand-made symlink into `~/.agents/skills` would dangle the moment it does.

`skills.plugins.items.<plugin-name>: { marketplace: <marketplace-name> }` fixes this declaratively:
at every `rig apply`, rig resolves `<plugin-name>@<marketplace-name>` against the LIVE
`installed_plugins.json`, enumerates that install's `skills/*/SKILL.md` dirs, and maintains an
idempotent symlink `target/<skill-name> → <resolved-install-path>/skills/<skill-name>` for each —
the same create/re-point/never-clobber-a-real-dir contract `harness_link` uses (see above), just
landing in `target` (the skills install dir) instead of a harness discovery dir. A version bump
re-points every symlink at the next `rig apply`; nothing dangles.

```yaml
skills:
  plugins:
    items:
      superpowers:
        marketplace: claude-plugins-official
```

Resolution rules:

- **Not installed on this machine** (the plugin was never installed, or the marketplace/plugin
  name is typo'd) — `rig apply` records a `plan.notes` line and skips it; this is NOT a fatal
  error (a declared plugin can be legitimately absent on a CI runner or a laptop without Claude
  Code).
- **Multiple install scopes** (Claude Code lists one entry per scope a plugin was installed at —
  a machine-wide `user` install, and/or one pinned per-project `project` install) — the `user`
  scope wins (what Claude Code resolves to outside that one pinned project); among remaining
  candidates, the most recently updated entry wins.
- **Name collision with a real agent-tools catalog skill** — the catalog skill always wins. rig
  never silently shadows its own namespace with a plugin-sourced symlink; the collision is
  recorded as a `plan.notes` line instead (mirrors the `systematic-debugging` precedent: the
  `superpowers` plugin ships a skill of that name, which collides with an existing, unrelated,
  already-catalogued agent-tools skill of the same name).
- **Name collision between two DIFFERENT declared plugins** — without a guard, two
  `link_skill_plugin` actions targeting the same `skills_target/<name>` with different sources
  would flip-flop which one "wins" on every `rig apply` (never idempotent) and the loser would
  read as permanent `modified` drift. The alphabetically-first plugin name wins (matching
  `resolve_plugin_skills`'s stable sort order); the loser is recorded as a `plan.notes` line,
  never silently applied.
- **`installPath` outside the plugin store, or an in-tree symlink escaping a plugin's own
  install root** — `installed_plugins.json` is Claude Code's OWN file, not rig's; a corrupted
  write (or a compromised plugin, or a marketplace archive shipping a `skills/<name>` directory
  symlink) could otherwise plant a global skill symlink pointing outside the trusted plugin
  store. Both are refused (never followed) and recorded as a `plan.notes` line, same as "not
  installed" — this never becomes a hard `rig apply` failure, but it also never becomes a link.

A plugin-sourced skill is a genuine **plan-declared** action (`link_skill_plugin`), exactly like
a catalog skill's `copy_skill` — `rig status` recognizes it as managed/converged natively. It
does **not** need a `skills.known` entry (that mechanism is for skills rig has no other way to
place — see [Provenance](#provenance)).

**Scope**: Claude Code's own plugin marketplace only (`~/.claude/plugins/installed_plugins.json`).
Codex, Gemini, and any other agent's plugin/extension system are not read by this mechanism.
**Limitation**: only a plugin's `skills/*` subtree is symlinked — a plugin's hooks, MCP servers,
slash commands, or any other capability are untouched (out of scope; rig has no general
third-party-plugin provisioning feature).

---

## `agent_hooks`

```yaml
agent_hooks:
  enabled: true
  target: ~/.claude/hooks
  all: true
  worktree_only: true               # enforce the worktree-only workflow in THIS repo (default ON; false opts out)
  orchestrator_only: true           # keep the orchestrator thin in THIS repo (default on)
  items:
    block-no-verify:     { enabled: true,  on_error: closed }
    block-reset-hard:    { enabled: true,  on_error: closed }
    enforce-timeout-on-bash: { enabled: true, on_error: open }
```

| Per-item key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | install this hook |
| `on_error` | `open`/`closed` | descriptor's value | fail policy (security = closed) |

| Block key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `known` | list[str] | `[]` | hook ids (or full descriptor stems) another installer wrote into the hooks dir — reported as known, not drift. GLOBAL-layer (the hooks dir is machine-wide). See [Provenance](#provenance). |

The install action always writes an **absolute** `cmd` (rewriting the
`/ABSOLUTE/PATH/TO/...` placeholder to the script's real path in the agent-tools checkout),
per the `agents-hooks/v1` contract. A descriptor whose `args` carries the exact element
`/ABSOLUTE/PATH/TO/rig` (the agent-tools `block-unlanded-removal` hook does) gets it rewritten to the
absolute path of the `rig` launcher that ran the apply (`sys.argv[0]` when it is an executable file
named `rig`, else `rig` on PATH; neither → the install fails with an error naming the pin), so a hook
that shells out to rig never resolves it through its own PATH. A moved or reinstalled launcher is
reported by `rig status` as a modified descriptor and re-pinned by the next `rig apply`.

When `harness.kind: codex` is active, rig defaults the descriptor target to `~/.codex/hooks`
(or `$RIG_CODEX_HOME/hooks`) unless `agent_hooks.target` / `defaults.hooks_target` is set to a
non-default custom path. Claude behavior is unchanged: `claude-code` still defaults to
`~/.claude/hooks`.

`agent_hooks.target_kind` is a legacy scaffold key accepted for old configs (`claude-code` or
`generic`) but ignored. Remove it when editing the file; hook targets now come from
`agent_hooks.target`, `defaults.hooks_target`, and `harness.kind` / `harness.kinds`.

### Per-repo workflow knobs — `worktree_only` / `orchestrator_only`

Two booleans that configure the **runtime behaviour** of three installed hooks per repo
(`worktree-only-writes`, `pin-primary-worktree`, `orchestrator-stays-thin`). They
are read by the hook scripts from **this committed `rig.yaml`** at fire time — `rig apply`
does **not** consume them (nothing to install; the hooks self-read the knob). They live in the
`agent_hooks` block so the strict validator accepts them.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `worktree_only` | bool | `true` | **ON by default** wherever a `rig.yaml` exists (agent-tools#1012); `worktree_only: false` opts a repo that legitimately works on main out, and a directory with no `rig.yaml` is never blocked. Gates THREE hooks together (one feature, one flag): (3) `block-devserver-primary` denies a dev-server launch on the primary checkout's default branch; the history side of (2) additionally **denies `git commit`/`cherry-pick`/`merge` (except `--ff-only`)/`rebase`/`revert`/`am` on the default branch of the PRIMARY checkout**, with the exact `rig worktree create <feature> --from origin/<default> -C <root>` fix in the message. (1) `worktree-only-writes` (pre-write) **denies an Edit/Write while the checkout is on the repo's default branch** (main/master, detected via `origin/HEAD` / `init.defaultBranch`, never hardcoded); (2) `pin-primary-worktree` (pre-bash, agent-tools#182/#183) **denies a `git checkout`/`git switch` that would move the repo's PRIMARY worktree (never a linked `git worktree add` tree, detected via `--git-dir` vs `--git-common-dir`) onto anything but the default branch** — added because (1) alone never sees a bare `git checkout` (not an Edit/Write call) and, once on a feature branch, can't distinguish the primary checkout from a legitimate linked worktree (Alex tg#6462/tg#6477, a real incident this closed). Together: all authoring happens in a SEPARATE worktree on a feature branch; the primary checkout is for merge/pull/read-only. **Before relying on the default, audit a repo's sanctioned automated flows (release scripts, `gh ship` wrappers) for a non-default `git checkout`/`switch` in the primary checkout — this guard has no special-casing for them.** No self-service env bypass — each hook has its OWN hatch var, not a shared one: a deliberate one-off Edit/Write on main is requested via `RIG_HATCH_REQUEST_WORKTREE_ONLY_WRITES="<justification>"`; a one-off `git checkout`/`switch` in the primary checkout is requested separately via `RIG_HATCH_REQUEST_PIN_PRIMARY_WORKTREE="<justification>"` (both: tg approval, deny-by-default, bare `1` rejected). (Alex tg#5742, tg#6462/tg#6477.) |
| `orchestrator_only` | bool | `true` | **opt-OUT.** By DESIGN, when `true` (default), the `orchestrator-stays-thin` hook warns on the first implementation-shaped Bash/code-Edit by the main thread (delegate to a subagent), then blocks a repeat within its TTL — both descriptors, `pre-bash` and `pre-write`, are provisioned (the catalog installs every descriptor a hook directory ships, not just the first alphabetically). Read-only inspection (`git status`/`ls`/`cat`/`grep`/`find`, `git worktree list`) is never gated; `tg`/`review`/`task` are sanctioned orchestration, also never gated. **Most `gh` is still delegated to a subagent, not inline orchestrator work** — `gh pr merge/create/close/edit`, `gh pr checkout`, `gh issue create/close/edit`, `gh release`, `gh workflow`, `gh secret`, and any `gh api` mutation all warn-then-block the main thread the same as any other implementation-shaped Bash. TWO narrow, explicit exceptions run inline, never gated: (1) a genuinely UNCHAINED `gh ship <PR#>` — the whole, sole command on the line (agent-tools#159/#363); (2) a FIXED read-only allowlist (Alex, 2026-09-18, session-authorized relaxation of the blanket gh gate) — `gh issue list/view`, `gh pr list/view/checks/diff`, `gh run list/view/watch`, `gh repo view`, and a `gh api` call carrying no `-X`/`--method` flag other than GET (absence also counts as GET) and no `-f`/`--field`, `-F`/`--raw-field`, or `--input` data-payload flag — granted per-segment like `tg`/`task`, unlike `gh ship`'s per-LINE-only grant. A gh invocation matching neither exception still warn-then-blocks. Set `false` to exempt a repo that works inline (e.g. `3d-cli`). Default **on** = no behaviour change for a repo that omits the key. No self-service env bypass; a one-off is requested via `RIG_HATCH_REQUEST_ORCHESTRATOR_STAYS_THIN="<justification>"` (tg approval, deny-by-default; bare `1` rejected). (Alex tg#5743, tg#7103; allowlist enforced by agent-tools#764, 2026-09-18.) |

Both take effect only where the corresponding hook is installed (`agent_hooks.all: true`, or
the item enabled). They are **runtime** signals, not provisioning — a fresh clone reads the
committed value directly; no re-`apply` is needed to change enforcement.

**Harness scope**: these are Claude Code agent-hooks. A different harness invoking `git
checkout` directly (a raw script, a different agent CLI) is not intercepted — git itself has
no `pre-checkout` hook to fall back on.

---

## `git_hooks`

v0.1 ships the **global dispatcher** (the headline feature — your hooks run in every repo,
even ones that hijack `core.hooksPath`). Per-repo hook templates are deferred to v0.2.

```yaml
git_hooks:
  dispatcher:
    enabled: true
    dir: ~/.config/git/global-hooks.d         # drop-in fragments dir
    runner: ~/.config/git/run-global-hooks     # the dispatcher script
    set_global_hooks_path: true                # set global core.hooksPath (records prior value)
    install_local_retrofit_script: true        # put install-local-hooks.sh on ~/.local/bin
    fragments:
      secret-scan:         { enabled: true }
      conventional-commit: { enabled: false }
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `dispatcher.enabled` | bool | `false` | install the global-hook dispatcher |
| `dispatcher.dir` | path | `~/.config/git/global-hooks.d` | fragments dir |
| `dispatcher.runner` | path | `~/.config/git/run-global-hooks` | the runner |
| `dispatcher.set_global_hooks_path` | bool | `true` | wire it as global `core.hooksPath` (backs up the prior value into the action note) |
| `dispatcher.install_local_retrofit_script` | bool | `true` | put `install-local-hooks.sh` on PATH |

---

## `ci`

```yaml
ci:
  enabled: true
  target: .github/workflows        # or `export-only` (record choices, write no files)
  all: false
  items:
    secret-scan:       { enabled: true,  tier: block }
    codeql:            { enabled: true,  tier: block, variant: selfgate }
    dependency-review: { enabled: true,  tier: block }
    leftover-grep:     { enabled: true,  tier: block }
    review-threads:    { enabled: true,  tier: block }
    ship:
      enabled: true
      install_to: ~/bin              # ship is a client command, not a workflow
      gh_alias: true                 # set the `gh ship` alias to the portable dispatcher
```

| Per-item key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | per-item | install this gate |
| `tier` | `block`/`warn` | `block` | enforcement strength (recorded; the workflow itself encodes it) |
| `variant` | str | — | e.g. codeql `selfgate` selects `workflow-selfgate.yml` |
| `install_to` / `gh_alias` | path/bool | — | ship-specific (it is a client command). `gh_alias: true` sets the `gh ship` alias to the **portable dispatcher** (same body the `ship_delegator` area writes — see below), NOT a `~/bin/ship` copy path, so the two provisioning paths never conflict. |

| Block key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `known` | list[str] | `[]` | workflow file stems this repo owns that happen to collide with a catalog slot name (a home-grown `tests.yml`) — reported as known, not drift. A workflow whose stem is NOT a catalog slot is always the repo's own and needs no declaration. See [Provenance](#provenance). |

`target: export-only` records the choices without writing files (an agent applies later or
a non-GitHub CI uses each slot's `*.sh`).

---

## `mcp`

```yaml
mcp:
  enabled: true
  target: ~/.claude/mcp            # or ./.mcp.json | export-only
  items:
    code-search:
      enabled: true
      server: serena               # a code-search server (serena/sverklo) — see mcp/README.md
      command: ""                  # no command → nothing to register (reported, not an error)
      args: []                     # optional: when present, command is not shell-split
      env: {}                      # optional environment variables for this server
```

> `review` is **not** an MCP item — it is a CLI + skill, and its MCP slot was removed in
> agent-tools #32. A config that still declares `mcp.items.review` is rejected as a removed
> slot (`rig status` exits 4); remove it.

The install action merges an MCP entry **idempotently by server name** into
`<target>/mcp.json` (or the target file if it ends in `.json`) and never overwrites an
existing differing entry unless `defaults.on_conflict: overwrite`.

| Per-item key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | per-item | register this server |
| `server` | str | item name | key written under `mcpServers` |
| `command` | str | `""` | executable/launch command; empty skips registration even when `args`/`env` are set |
| `args` | list[str] | shell-split from `command` | when present, use `command` exactly and this argv exactly |
| `env` | map[str,str] | omitted | non-empty environment map written into the MCP server entry |

| Block key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `known` | list[str] | `[]` | MCP server names registered by something other than rig — reported as known, not drift. GLOBAL-layer (the MCP config is machine-wide). See [Provenance](#provenance). |

Compatibility: if `args` is omitted, rig keeps the legacy behavior and parses `command` as a
shell-like string, so `node server.js --foo` becomes `{"command":"node","args":["server.js","--foo"]}`.
If `args` is present, `command` is the executable value exactly, allowing paths such as
`/opt/my tools/run` without quoting.
Do not put arguments or shell syntax into `command` when `args` is present:
use `command: "node"` with `args: ["server.js", "--foo"]`, not
`command: "node server.js"` with `args: ["--foo"]`.

---

## Provenance

`rig status` scans the **shared** target locations — `~/.agents/skills`, `~/.claude/hooks`,
`.github/workflows`, the MCP registry — for items on disk that the config does not declare. Those
places legitimately hold things rig never wrote, so rig first tries to **name the origin** of each
undeclared item; only an item it cannot place is reported as drift. Placed items print under a dim
"known, not managed by rig" heading (and permission entries beyond the rig baseline under "your
additions, kept"); they never count as drift, never change the exit code, and are never deleted.

A `skills.plugins.items.<name>`-declared skill is NOT part of this scan at all — it is a genuine
plan-declared action (`link_skill_plugin`, the plugin-sourced sibling of `copy_skill`), so it
never reaches the "undeclared item" classification below in the first place. See
[Claude Code plugin-marketplace-sourced skills](#claude-code-plugin-marketplace-sourced-skills-skillsplugins).

### The marker contract (for tool installers)

An installer that writes into a rig-scanned location records itself so rig can place its files:

| What it writes | The marker |
| --- | --- |
| a skill dir `<skills>/<name>/` | a one-line file `<skills>/<name>/.installed-by` naming the tool (`tg`, `review`, …) |
| an agent-hook descriptor `<hooks>/<id>.<point>.json` | a top-level `"installed_by": "<tool>"` key in the descriptor JSON (the `agents-hooks/v1` runner ignores keys it does not know) |

`rig install-skill` / `rig daily install-skill` write the skill marker themselves. Until every
ecosystem installer does, rig also honours the SessionStart blurb `<skills>/.blurbs/<name>.md` that
`<tool> install-skill` already writes, plus a shipped allowlist of the ecosystem CLIs' own skill
names (`riglib/provenance.py`). Skills installed with the third-party `skills` CLI (`npx skills
add <owner>/<repo>`) need no marker either: rig reads the lockfile that CLI keeps next to the
skills dir (`~/.agents/.skill-lock.json` for the default target) and names the source repo.

### How an undeclared item is classified

| Item | Known (informational) when … | Otherwise |
| --- | --- | --- |
| skill named after a **catalog** item | `skills.known` (your own pack under that name) → byte-identical to the catalog source (the skills dir is machine-wide, the plan per-repo, so another repo's by-type/by-stack apply legitimately put it there). Catalog names are rig's namespace: no installer marker can vouch for one | drift: a stale copy from an older catalog, or foreign content under a catalog name |
| any other skill | `.installed-by` marker → `.blurbs/<name>.md` → the `skills` CLI lockfile (`<skills>/../.skill-lock.json`, written by `npx skills add`) → shipped ecosystem allowlist → `skills.known` | drift: unknown origin — declare it under `skills.known` or remove it |
| agent-hook descriptor with a **catalog** hook id | `agent_hooks.known` → its `cmd` runs a script inside that catalog hook's directory (rig-installed for another repo) | drift: a catalog id running something else, whatever `installed_by` claims |
| any other agent-hook descriptor | `installed_by` key → shipped agent-tools ops-installer list → `agent_hooks.known` (by id or full `<id>.<point>` stem) | drift: unknown origin |
| CI workflow | `ci.known` → a stem that is NOT a catalog CI slot (the repo's own workflow) | drift: a catalog gate on disk that this config does not enable (a rig orphan) |
| MCP server | `mcp.known` | drift |
| permission entry beyond the baseline | "kept": a former rig default (`git`/`uv`/`bun`, rig-cli#41 → #165 — `gh` was reinstated by rig-cli#332, see `allow_gh` below), a baseline deny/ask rule the config turned off, or a hand-added entry — rig removes none of these. The ONE exception: a `gh` grant rig itself receipted as its own default (never a hand-added or pre-existing one) is drift, and an explicit `allow_gh: false`/`disable: [gh]` revokes it — see `allow_gh` below | — (or `extra`, for the one revocable exception) |

Real drift keeps its signal: an unknown-origin skill, an undeclared catalog gate workflow, or a
hand-mangled permission container still exits with the drift code.

### Where a `known` list lives

The `<category>.known` lists cascade like every other list in the config: a repo `rig.yaml` list
**replaces** the global one (it is not merged). So the list for a machine-wide location —
`skills.known`, `agent_hooks.known`, `mcp.known` — lives in the global `~/.config/rig/config.yaml`;
a repo-level `skills.known` would drop the global entries for that repo and resurface them as
drift. `ci.known` names files in the repo's own `.github/workflows`, so it belongs in that repo's
`rig.yaml`.

Every editing surface routes these three by that rule (rig-cli#372): the option registry
(`riglib/schema.py`) marks them GLOBAL-layer inside their otherwise repo-writable areas, so
`rig setup` and the config-web write them to the global config (the config-web Global tab shows
exactly these three under skills / agent-hooks / MCP), and `rig config set skills.known …` without
`--global` is refused the same way a global-only block is. A list that is ALREADY committed in a
repo `rig.yaml` keeps shadowing the global one until you remove it from the repo file by hand —
rig never migrates or deletes config. That replace-semantics is also the escape hatch for a repo
whose target is NOT the machine-wide default (a repo-local `mcp.target: .agent/mcp.json`, say):
commit a repo-level `mcp.known` by hand in that repo's `rig.yaml`, and the global list no longer
applies there — so a server the global list vouches for is still drift in that repo unless the
repo list names it too.

A marker is a claim, not a grant: it changes what `rig status` prints **and its exit code** (a
marker-bearing item is no longer drift, so a rogue skill that writes its own `.installed-by` is
not caught by the status exit code — the accepted trade for a directory rig does not manage). It
never changes what rig writes or removes (nothing, for a known item), and a catalog-named dir is
never vouched for by a marker.

---

## `mode`

Machine-wide operating policy. This block belongs in the global config
(`~/.config/rig/config.yaml`), not in a committed repo `rig.yaml`.

```yaml
mode:
  name: autonomous             # standard | autonomous
  autonomous:
    review_fix:
      enabled: true
      max_iterations: 5
      until: clean             # clean | budget | manual
    decisions:
      review_quorum:
        enabled: true
        min_iterations: 2
        min_models: 3
    escalation:
      framework_skill: decision-request-discipline
      require_parallel_worktree_comparison: true
    parallel_worktree_comparison:
      enabled: true
      candidates: 2
    development_tools:
      allow:
        - Bash(dev:*)
        - Bash(review:*)
        - Bash(task:*)
    parallelism:
      max_agents: 4
      max_worktrees: 4
      reserve_slots: 1
      limit_aware: true
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `name` | enum | `standard` | `standard` keeps normal rig behavior; `autonomous` activates the policy notes and permission-flow additions below |
| `autonomous.review_fix.enabled` | bool | `true` | enable review/fix iteration policy |
| `autonomous.review_fix.max_iterations` | int >= 1 | `5` | maximum review/fix iterations before stopping or escalating |
| `autonomous.review_fix.until` | enum | `clean` | stop condition: clean state, budget exhaustion, or manual stop |
| `autonomous.decisions.review_quorum.enabled` | bool | `true` | require a multi-model review quorum before treating a decision as settled |
| `autonomous.decisions.review_quorum.min_iterations` | int >= 1 | `2` | minimum recorded review iterations for a quorum |
| `autonomous.decisions.review_quorum.min_models` | int >= 2 | `3` | minimum distinct models for a quorum |
| `autonomous.escalation.framework_skill` | str | `decision-request-discipline` | named behavioral skill agents follow for escalation messages: context, options, recommendation, and explicit ask |
| `autonomous.escalation.require_parallel_worktree_comparison` | bool | `true` | do not escalate an implementation choice until independent worktree candidates have been compared |
| `autonomous.parallel_worktree_comparison.enabled` | bool | `true` | enable independent candidate worktrees before escalation |
| `autonomous.parallel_worktree_comparison.candidates` | int >= 2 | `2` | number of candidate implementations to compare |
| `autonomous.development_tools.allow` | list[str] | `Bash(dev:*)`, `Bash(review:*)`, `Bash(task:*)` | raw permission allow rules added to supported harness permissions while autonomous mode is active |
| `autonomous.parallelism.max_agents` | int >= 1 | `4` | maximum concurrent agents |
| `autonomous.parallelism.max_worktrees` | int >= 1 | `4` | maximum concurrent worktrees |
| `autonomous.parallelism.reserve_slots` | int >= 0 | `1` | slots held back for urgent/manual work |
| `autonomous.parallelism.limit_aware` | bool | `true` | agents should respect model, harness, and machine limits when dispatching parallel work |

`mode.name: autonomous` does not create a second executor. It provisions policy through
existing rig surfaces: `rig apply --dry-run`/`rig config set --global ... --no-apply` show the
review/fix, quorum, escalation, parallel-comparison, and limit-aware parallelism policy as plan
notes; `autonomous.development_tools.allow` feeds the existing additive `permissions.allow`
merge for harnesses with a verified rule dialect. Today these raw `Bash(...)` rules are applied
only to Claude Code's verified permission-rule dialect; unsupported harnesses get a plan note and
the rules are skipped rather than translated guessily. The configured `framework_skill` is a
named behavioral contract for agents, not a callable interface invoked by `rig`: ask only after
the review quorum and parallel worktree comparison cannot settle the decision, and include
context, options, a recommendation, and the specific decision requested.

---

## `env`

Provisions **rig-managed shell environment variables** — a var like `COLORTERM` that must be
visible to **every** shell invocation on the machine, not just an interactive login shell: a
mosh/SSH non-interactive command shell (`ssh host 'some-command'`), a cron/launchd job, or
`zsh -l -c '...'`. This is **GLOBAL config** (the shell startup file rig sources into is
HOME-anchored, not repo-relative) — it belongs in `~/.config/rig/config.yaml`.

Mirrors `tmux`'s "own a generated file, splice one import line" shape — rig owns
`<generated_dir>/rig.env.sh` (wholesale rewrite each apply, one `export KEY=value` per `vars`
entry, sorted by key) and ensures **one** `source '<generated file>'` line is present in
`rc_path` — every other line in that file is left untouched. Unlike `tmux` there is no dual
import/block mode and no neutralization of unrelated inline content: a plain exported var has
no equivalent of tmux's `@plugin`/`@continuum-*` declarations that need detecting and
superseding, so the splice is a simple idempotent "ensure this one line is present" append.

**Position-tolerant — deliberately NOT end-anchored like `tmux`.** `tmux`'s import line is
always re-appended at the very END on every apply, dropping any existing copy first, because
tmux's ordering guarantee genuinely depends on position (continuum's `run-shell` init must be
LAST). A plain exported var has no such hazard, so `env` takes the opposite default: if rig's
CURRENT import line already appears **anywhere** in `rc_path` — including above the user's own
exports, e.g. so *their* values win — that line's POSITION is left exactly where the user put
it, and every OTHER line is untouched. This is NOT unconditionally "byte-for-byte untouched",
though: a coexisting STALE line (an old `generated_dir`, left over from before a config change)
is always dropped even when the current line is already present and correctly placed elsewhere
— position-tolerance for the CURRENT line must not become an excuse to leave an orphaned old
`rig.env.sh` silently sourced forever. Only when the current line is truly absent everywhere
(first apply, or only that stale copy existed) does `env` append the current line once, at the
end. `rig status` shares this exact predicate, so it can never disagree with what `rig apply`
would do.

**No teardown on `enabled: false`.** Like every other rig-managed artifact ("`rig apply` NEVER
deletes on-disk extras"), turning `env` off stops rig from RECONCILING `rig.env.sh` and the
import line, but does not remove them — the vars keep applying to every shell until you delete
`<generated_dir>/rig.env.sh` and the `source` line yourself. `rig status` DOES surface this one
specifically — unlike `tmux`, which has no equivalent — checking BOTH halves independently (a
leftover `rig.env.sh`, and separately a leftover `source` line in `rc_path`, since either can
survive without the other: a hand-deleted `rig.env.sh` with the source line still present makes
every shell invocation on the machine print a "no such file or directory" error at startup).
**This coverage fires ONLY for an explicit `env: { enabled: false }`** — REMOVING the `env:`
block entirely (the more natural way to "turn a feature off", and the one actually described
above as "the real leave-shell-env-vars-alone case") runs no scan at all, so the same
still-active artifacts would report as in sync in that case. Only `gitignore`/`git_hooks`
get an equivalent "still installed but now disabled" check elsewhere in rig; extending any of
them to also cover full block-removal is a known, tracked gap, not implemented here.

**Why `~/.zshenv` by default, not `~/.zshrc`/`~/.zprofile`.** zsh's startup order makes
`~/.zshenv` the only file sourced **unconditionally** in every mode — login or not, interactive
or not (`~/.zshrc` is interactive-only; `~/.zprofile`/`~/.zlogin` are login-only). A var that
must reach `zsh -l -c '...'` (a login but **non-interactive** shell — `-c` means it never
becomes interactive, so `.zshrc` is never sourced) has to live in `.zshenv` or `.zprofile`;
`.zshenv` is the more universal of the two (it also reaches plain non-login non-interactive
invocations). Same reasoning as `tmux.login_shell` treating `~/.zprofile` as the login-env
source of truth, one file more universal.

A PRESENT `env:` block opts in (mirrors `tmux`: even an explicit empty mapping `env: {}` is
accepted, yielding a harmless header-only generated file); only an ABSENT key or
`enabled: false` is a no-op.

```yaml
env:
  enabled: true                    # provision the rig-managed shell env vars (opt-in)
  rc_path: ~/.zshenv                # the shell startup file rig sources the generated file from
  generated_dir: ~/.config/rig/env  # where rig writes rig.env.sh
  vars:
    COLORTERM: truecolor            # exported KEY=value pairs
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | — (see below) | opts OUT when explicitly `false`. There is no fixed default for this key alone — a PRESENT `env:` block with `enabled` unset already opts IN (mirrors `tmux`); it is the BLOCK's presence, not this key's own default, that decides. An entirely ABSENT `env:` block is the real "leave shell env vars alone" case. |
| `rc_path` | path | `~/.zshenv` | the shell startup file rig sources the generated env file from |
| `generated_dir` | path | `~/.config/rig/env` | where rig writes `rig.env.sh` |
| `vars` | map[str,str] | `{}` | exported `KEY=value` pairs. Keys must be valid POSIX shell identifiers (`[A-Za-z_][A-Za-z0-9_]*` — they are interpolated UNQUOTED into `export {key}=...`, a file sourced by every shell invocation on the machine, so an invalid key is rejected at validate time rather than reaching that file). Values are shell-quoted at render time, safe for spaces/quotes/`$`/backticks/anything. |

Idempotent: a re-apply that finds both the generated file and the import line already current is
a `skipped` no-op. `rig status` flags drift on the generated file's content and the import line's
presence — never on any other line in `rc_path` (that file is otherwise entirely the user's own).

### `rig env set|list|del`

The dedicated surface for `env.vars` — a thin wrapper over `rig config set --global env.vars.<KEY>`,
not a second YAML-edit path. Always global. Values are stored as **strings** (no `true`/`12`
coercion: `COLORTERM=true` must remain the string `true`).

**Plaintext.** `~/.config/rig/config.yaml` and the generated `rig.env.sh` (sourced by every zsh)
hold the values in the clear. `rig env list` masks them (`***` + last 4 characters; four stars
if shorter). `--show` (and `--json --show`) prints the full value. Prefer `rig env set KEY` with
the value on stdin so it never lands in shell history:

```bash
pbpaste | rig env set BRAVE_API_KEY
rig env list
rig env del BRAVE_API_KEY
```

`set`/`del` write + reconcile by default (`--preview` is dry-run; `--no-apply` writes the YAML
only). Reconciliation loads only the global layer, so a repo `rig.yaml` `env:` block cannot
override the value just written. `del` of a missing key is fail-closed. Deleting the last var
leaves `env.vars: {}`.


---

## `internal_dev`

For internal development of the rig-ecosystem tools themselves (rig, tg-cli, review-cli, …): when a
repo is developed IN PLACE — the checked-out files ARE the running binary (a live symlink) or a
long-running daemon reads them — a code change only takes effect after the daemon is restarted. This
block wires that restart to the commit: an opt-in `post-commit` git hook that, when a commit touches
the configured daemon-source paths, runs a graceful reload command (`tg-ctl restart` by default).
This is a per-REPO, **committed** concern (the enablement + source paths travel with the repo's
`rig.yaml`, exactly like `agent_hooks.worktree_only`) — **not** the global config. **Default OFF**
(an absent or empty `internal_dev:` block installs nothing).

```yaml
internal_dev:
  auto_reload_on_commit: true                       # opt-in; default false
  daemon_source_paths: ["src/daemon/*", "bin/tg-ctl"] # POSIX `case` globs, repo-relative
  reload_command: "tg-ctl restart"                   # default shown; any shell command
```

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `auto_reload_on_commit` | bool | `false` | install a `post-commit` hook that reloads the daemon when a commit touches a daemon-source path |
| `daemon_source_paths` | array of str | `[]` | repo-relative shell-glob patterns (POSIX `case`, a `*` spans `/`) whose change in a commit triggers the reload |
| `reload_command` | str | `tg-ctl restart` | the graceful reload command the hook runs on a match |

**What rig writes.** The repo-local `<git-dir>/hooks/post-commit` (worktree-correct — resolved via
`git rev-parse --git-common-dir`, not assumed to be `<repo>/.git`; a linked worktree's hooks live in
the repo's COMMON dir, not its private per-worktree admin dir). When a global `core.hooksPath`
composer shadows repo-local hooks (the rig global-hook dispatcher sets one machine-wide), rig also
writes a generic `post-commit` COMPOSER trampoline into that composer dir — the agent-tools
dispatcher ships no `post-commit` of its own, so without the trampoline a composer-shadowed
repo-local hook would never fire.

**The reload never blocks a commit.** `post-commit` runs after the commit is recorded; a failed
reload is reported and swallowed — a broken daemon must never wedge a developer's commit.

**Idempotent, marker-keyed, backup-on-conflict** — like every other rig-managed artifact
(`on_conflict` applies to both the hook and the composer).

**Drift.** `rig status` flags a missing or modified `post-commit` hook under the `internal_dev`
category (REPO layer).

**Dry-run seam.** `RIG_DEV_RELOAD_DRY_RUN=1` (mirrors `RIG_TG_CTL_DRY_RUN` / `RIG_TMUX_DRY_RUN`):
the hook reads it at FIRE time and skips the real reload; the runner reads it at APPLY time and
skips writing the machine-global composer (the one live/global mutation) — so tests/CI never fire a
real reload nor touch the real global hooks dir.

---

## Editing this config — `rig setup` (wizard) and `rig config get|set`

`rig setup` is the **interactive configuration wizard**. In a terminal it (1) SHOWS what is
enabled across every reconciled area above — read from the cascaded config (global + repo),
absent keys shown at their documented default — (2) lets you CHANGE any option, with an inline
HINT (the why/how) next to each, and (3) APPLIES (`rig apply`) on the spot. Run from a non-TTY
(piped / redirected) it prints USAGE for `init`/`apply`/`config get|set` rather than a
half-wizard you can't answer.

The wizard's option list + hints come from the in-code registry `riglib/schema.py` (the curated
toggle subset). The **complete** schema — every block and key — lives in `riglib/config_schema.py`
and is what `schema/rig.schema.json` is generated from; `schema.json_schema()` delegates to it, so
there is one emitter and the schema is never maintained in parallel (see "The JSON Schema" above).

**Which file a change is written to.** Each option is routed to its **owning layer**:

- **REPO** options (`skills`, `agent_hooks`, `git_hooks`, `ci`, `mcp`, `harness`, `scripts`,
  `dev`, `models`, `github`, `agents_md`, `linters`, `project_tools`, `ecosystem_readme`) are
  written to the repo's `./rig.yaml` — the values the default scaffold commits.
- **GLOBAL-only** options (`gitignore`, `tg_ctl`, `tcc_watch`, `tgctl_reaper`, `tmux`, `mode`) are machine-wide blocks the
  scaffold never writes into a committed repo file; the wizard writes them to
  `~/.config/rig/config.yaml` only — it never lands a global-only block in a committed repo
  `rig.yaml`.

`rig config get|set <dot.path>` is the **headless counterpart** (and what non-interactive
`rig setup` points at). It is a dot-path editor: it reads/edits ONE nested key by dot-notation
into the tree above (`harness.auto_mode`, `ci.items.secret-scan.tier`, `defaults.on_conflict`),
then **reconciles** (runs the same plan + apply engine as `rig apply`). `--global` targets
`~/.config/rig/config.yaml` (XDG-aware) instead of `./rig.yaml`.

```bash
rig config get harness.auto_mode                 # read one key (from ./rig.yaml, NOT the cascade)
rig config get harness.auto_mode --json          # machine-readable (JSON value)
rig config get harness                            # a subtree prints as YAML
rig config get defaults.on_conflict --global     # read the global config instead

rig config set harness.auto_mode false           # write, then RECONCILE (rig apply engine)
rig config set ci.items.secret-scan.tier warn    # creates intermediate keys as needed
rig config set defaults.on_conflict overwrite --global   # edit the global config
rig config set mode.name autonomous --global     # global-only blocks require --global
rig config set harness.auto_mode false --no-apply        # write only, print the plan, skip apply
```

- **`get`** reads the single target file (NOT the cascade): `./rig.yaml`, or the global file
  with `--global`. A missing file or absent path exits non-zero (fail-closed). `--json` emits
  the raw JSON value; a mapping/list subtree prints as YAML; bools print as `true`/`false`.
  Errors go to stderr, so `rig config get k --json | jq` keeps a clean stdout even when the key
  is missing.
- **`set`** coerces the value conservatively — `true`/`false` → bool, a plain integer → int,
  `1.5` → float, `null`/`none`/`~` → null, everything else (including `09`, `1e3`, `nan`, `inf`,
  `1_000`, and Unicode digits) stays a string. Quote-wrap to force a literal string
  (`rig config set k '"true"'` stores the string `"true"`). It creates intermediate mappings as
  needed, then guards the write with two **pre-apply** gates: the schema (`config.validate` —
  enums/types) and the catalog-backed plan build (`rig apply`'s engine — an unknown item a
  category references, a bad `agent_tools_source`, or any otherwise-unbuildable config). If
  **either** gate rejects the edit, the target file is rolled back to its prior contents and the
  command exits non-zero.
- Nullable options can be set to explicit `null` with `null`, `none`, `~`, or an empty value.
  For example, `rig config set permissions.kind null` writes `permissions.kind: null` in the
  owning layer, restoring default fan-out for that repo and masking any global-layer
  `permissions.kind` pin.
- A nested edit never clobbers an explicit null parent. If a file contains `permissions: null`,
  `rig config set permissions.kind null` fails with a "not a mapping" error; make the parent a
  mapping first.
- Dot paths cannot contain empty segments, including whitespace-only segments: `harness..kind`
  and `harness. .kind` are rejected. Whitespace around non-empty segments is trimmed.
- A value that **starts with `-`** (and is not a negative number) needs the `--` separator so
  argparse doesn't read it as a flag: `rig config set k -- -weird`.
- **A key that contains a dot** goes in quotes inside the path: `rig config set
  'mcp.items."my.server".command' npx` (quote the whole path for the shell). Past `git.config`
  no quotes are needed — git keys are always dotted, so the rest of the path is one key
  (`git.config.user.name`).
- **A repo-local `set` requires an existing `./rig.yaml`.** It edits a committed config; it does
  not bootstrap one — run `rig init` (or `rig export -o rig.yaml`) first, so built-in defaults
  never reconcile onto disk without a committed source of truth (the same guard `rig apply`
  has). `--global` may create the machine-wide `~/.config/rig/config.yaml` if it is absent.
- **`set` rewrites the whole file** through rig's serializer, so it normalizes formatting and
  **drops comments** — the value is the source of truth, not the surrounding YAML prose. It is
  also **repo-scoped**: even a `--global` edit resolves and reconciles the current repo, so run
  it from inside one (use `--no-apply` to skip the reconcile, not the repo resolution). Setting
  the removed `scope` key is refused (the cascade is by location, not a flag).

## Validation

`apply`/`status`/`init` validate before touching disk and **fail closed**. **Every block is
strict**: an unknown FIXED key in *any* block — `defaults`, `skills`, `agent_hooks`, `git_hooks`
(+ `dispatcher`), `ci`, `mcp`, `harness` (+ `hook_bridge` / `web_search`), `permissions`, `models` (+ `schedule`),
`agents_md`, `scripts`, `dev` (+ `server` / `e2e`), `github` (+ `ruleset` / `merge` / `ghas` / `actions` / `browser`), `tmux` (+ every
sub-block), `gitignore`, `git`, `profile`, `tg_ctl`, `tcc_watch`, `tgctl_reaper`, `project_tools` (+ `haft` / `workflow` / `serena` / `sverklo`),
`ecosystem_readme` — is rejected with the schema path of the offender, not silently ignored. (The open maps are
top-level `scripts:`, the catalog-keyed `items:` / `fragments:` maps, plus `dev.e2e.jobs:` — see
"Strict by default" above.) Bad-value rejections
include: unsupported `version`, invalid `on_conflict` / ci `tier` / agent-hook `on_error`, an
unknown or reserved `harness.kind`, a non-bool `harness.auto_mode`, a non-mapping
`harness.hook_bridge` / non-bool `hook_bridge.enabled` / non-string `hook_bridge.python`, a
non-mapping `harness.web_search` / non-bool `web_search.enabled` / non-list-of-nonempty-strings `web_search.exclude`, a
non-bool `git_hooks.dispatcher` bool knob, a malformed/out-of-range `models.schedule.time`, a
bad `scripts.<name>` entry (not a string or `{cmd: string}`), a
bad `dev.server`/`dev.e2e` entry, a
non-bool `agents_md.enabled`/`symlink`, a non-bool `github.ruleset` boolean knob, a
`github.ruleset.required_reviews` that is not an int ≥ 0, a `github.ruleset.required_status_checks`
that is not a list of strings, a bad `tmux.apply` enum, a `tmux.resurrect.processes` that is not a
list of strings, a `tmux.continuum.save_interval` that is not an int ≥ 1, a non-bool `tmux` bool
knob, a non-string `gitignore.excludesfile` / a `gitignore.entries` that is not a list of strings,
that contains a rig-managed marker line, or that has a blank or multi-line entry, a non-bool
`tg_ctl.enabled`/`boot`/`codex_usage_hook`, a non-string
`tg_ctl.label`/`bun_path`/`tg_ctl_path`/`config_dir`, a bad
`project_tools.haft.workflow.mode`, a non-list/string item in `project_tools.serena.languages` or
`project_tools.serena.ignored_paths`, a malformed / reserved / duplicate `git.config` key or a
non-scalar / multi-line value, a `profile.repo` that is not a one-line URL, a non-bool
`profile.auto_sync`, a **`git` or `profile` block in any layer but the global config** (they are
GLOBAL-only by load-time enforcement, like `mode`), and an `agent_tools_source` that is not an agent-tools
checkout. Every rejection is the **3-part error** (what / why + schema path / fix) and
exits `2`. `--dry-run` prints the resolved plan and exits 0 without writing.

`rig config set` is **fail-closed** with full rollback: a malformed/non-mapping existing target
file, a non-mapping intermediate on the dot path, or a schema-rejected resulting doc
(`config.validate`) is reported **before any write**; a write IO error or a catalog-backed plan
failure (the second gate — an unknown item, a bad `agent_tools_source`) rolls the file back to
its prior contents (a freshly-created file, and any dir created for it, are removed). Because every
block now enforces its key set, a `set` against a config that already carries a typo'd key
surfaces it (the whole edited tree is validated) — the same strictness a hand-edited `rig.yaml`
gets on `apply`. After it passes, `set` reconciles (the same plan + apply engine as `rig apply`);
`--no-apply` writes the key and prints the plan without converging.


### `linters` — Rig-owned lint policy

Rig is the policy source for generated linter configuration. The effective configuration is resolved from built-in defaults, then global Rig configuration, then repository `rig.yaml`.

```yaml
linters:
  enabled: true
  preview: true
  rules:
    all: false
    groups:
      typescript-core: true
      anti-slop: true
    enable: []
    disable: []
    severity:
      anti-slop/no-reflect-get: warn
  items:
    oxfmt:
      tool: oxfmt
      role: formatter
      path: .oxfmtrc.jsonc
      source: linters/oxc/.oxfmtrc.jsonc
```

`rules.all: true` enables every known applicable rule. `enable` and `disable` apply individual overrides; `severity` is the final per-rule `off` / `warn` / `error` authority. `groups` toggles named rule families. `preview` controls the best-effort before/after finding-count summary shown when Rig changes the generated Oxlint policy.

An explicit `linters.items.<name>` sets exactly one of `content` or `source`: `content` is repository-specific literal bytes, while `source` names a reusable file inside the configured agent-tools catalog.
