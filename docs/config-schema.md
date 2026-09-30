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
models: { ... }               # daily model-freshness checker schedule (launchd/crontab cron)
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
  link area is explained, not silent. The [`agents_md`](#agents_md) area is repo-local
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
  worktree_only: true               # enforce the worktree-only workflow in THIS repo (default off)
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
per the `agents-hooks/v1` contract.

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
| `worktree_only` | bool | `false` | **opt-IN.** Gates TWO hooks together (one feature, one flag): (1) `worktree-only-writes` (pre-write) **denies an Edit/Write while the checkout is on the repo's default branch** (main/master, detected via `origin/HEAD` / `init.defaultBranch`, never hardcoded); (2) `pin-primary-worktree` (pre-bash, agent-tools#182/#183) **denies a `git checkout`/`git switch` that would move the repo's PRIMARY worktree (never a linked `git worktree add` tree, detected via `--git-dir` vs `--git-common-dir`) onto anything but the default branch** — added because (1) alone never sees a bare `git checkout` (not an Edit/Write call) and, once on a feature branch, can't distinguish the primary checkout from a legitimate linked worktree (Alex tg#6462/tg#6477, a real incident this closed). Together: all authoring happens in a SEPARATE worktree on a feature branch; the primary checkout is for merge/pull/read-only. Default **off** so a repo that legitimately works on main (e.g. `3d-cli`) is never blocked. **Before enrolling a repo, audit its sanctioned automated flows (release scripts, `gh ship` wrappers) for a non-default `git checkout`/`switch` in the primary checkout — this guard has no special-casing for them.** No self-service env bypass — each hook has its OWN hatch var, not a shared one: a deliberate one-off Edit/Write on main is requested via `RIG_HATCH_REQUEST_WORKTREE_ONLY_WRITES="<justification>"`; a one-off `git checkout`/`switch` in the primary checkout is requested separately via `RIG_HATCH_REQUEST_PIN_PRIMARY_WORKTREE="<justification>"` (both: tg approval, deny-by-default, bare `1` rejected). (Alex tg#5742, tg#6462/tg#6477.) |
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

## `harness`

Provisions the **agent harness's auto/permission mode** as part of the reconciler. With a
`harness:` block, `rig apply` writes the harness's permission setting so **auto-mode**
(the agent runs autonomously, auto-accepting tool calls, with minimum babysitting) is part
of the reproducible config — not a manual per-machine toggle. **Recommended on by default**:
auto-mode is safe because the agent-hook guards (`block-secrets-write`, `block-no-verify`,
`enforce-timeout-on-bash`, `block-raw-process-env`, `block-raw-pr-merge`, `block-reset-hard`)
are installed in the same apply and catch the dangerous tool calls before the side effect.

```yaml
harness:
  enabled: true
  kind: claude-code            # skills-dir: claude-code | codex · native: opencode | omp · instruction-file: pi | commandcode
  # kinds: [codex]             # optional additional harnesses to provision alongside kind
  auto_mode: true              # true → auto-accept tool calls; false → interactive prompts (written per harness kind — see the table below)
  self_merge: true             # auto-mode: let the agent self-merge its OWN PRs via gh ship (permissions.allow ship rules + autoMode.allow carve-out)
  # mode: bypassPermissions    # optional: pin the exact mode value (overrides the auto_mode map)
  # settings_path: .claude/settings.json   # where to write (repo-local default; committed)
  # settings_paths: [~/other/settings.json] # EXTRA user-scope files the claude-code write also reaches
  # discover_config_dirs: true # also write every ~/.claude-accounts/account-*/settings.json (claude-rotate)
  hook_bridge:                 # wire the agents-hooks/v1 → harness dispatcher (default ON)
    enabled: true              # set false to skip wiring the dispatcher into harness config
    # python: python3          # optional: the interpreter the dispatcher runs under
  web_search:                  # omp providers.webSearchExclude (default ON when omp is configured)
    enabled: true              # set false to skip writing the exclude list
    # exclude: [perplexity]    # default; [] opts out. Additive — extras already on disk stay.
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | provision the harness setting (set `false` to leave the harness config untouched) |
| `kind` | enum | `claude-code` | which harness to provision. Skills-dir (`claude-code`, `codex`) get per-skill symlinks; native-discovery (`opencode`, `omp`) auto-loads `~/.agents/skills`; instruction-file (`pi`, `commandcode`) get their skill discovery via `AGENTS.md`. Every kind also gets its OWN auto/permission-mode key written (the per-harness table below); `pi`/`commandcode` have no such setting and get a visible n/a note |
| `kinds` | list | `[]` | additional harnesses to provision alongside `kind`. Use this when one machine runs multiple harnesses: the primary kind keeps its auto-mode/settings_path behavior, while additional kinds get skill discovery, agent-hook descriptors, supported hook bridges, supported permissions allowlists, AND their own auto-mode key (same auto intent as the primary kind). `agent_hooks.target` or a non-legacy `defaults.hooks_target` pins descriptors to one explicit target; supported bridges stay registered with a descriptor-dir override |
| `auto_mode` | bool | `false` (scaffold writes `true`) | `true` = auto-accept; maps to the harness's non-interactive permission value |
| `self_merge` | bool | `true` | auto-mode self-merge unblock. `true` adds the ship allow rules (`Bash(gh ship:*)`, `Bash(fj ship:*)`) and deny rules for the raw `ship.sh`/`pr-ship.sh` scripts to `permissions`, removes the legacy never-matching `Bash(*/pr-ship.sh:*)`/`Bash(*/ship.sh:*)` allow entries, so the auto-mode Bash gate stops vetoing `gh ship`, AND appends a `$defaults`-preserving carve-out to `autoMode.allow` clearing the Merge-Without-Review + Self-Approval soft blocks for the agent's OWN PRs. Effective only under auto-mode; inert otherwise. The `Bash(gh pr merge:*)` deny and every other classifier rule — notably the anti-exfil hard rule — stay (`gh ship` is still the only merge path) |
| `mode` | str | — | pin the exact permission value (e.g. `acceptEdits`), overriding the `auto_mode` mapping |
| `settings_path` | path | `.claude/settings.json` for Claude auto/mode writes; `~/.codex/config.toml` (or `$RIG_CODEX_HOME/config.toml`) for the Codex hook bridge; `.opencode/plugins/zz-agent-tools-hook-bridge.js` for the opencode hook bridge; `~/.omp/agent/extensions/zz-agent-tools-hook-bridge.ts` (or `$PI_CODING_AGENT_DIR`/`$PI_CONFIG_DIR`-relative) for the omp hook bridge | the settings/config file/plugin/extension to converge. If overridden, point it at the harness's native format (JSON for Claude, TOML for Codex, JS plugin for opencode, TS extension for omp). A suffixed path is treated as the file; a suffixless path is treated as a directory containing the native settings/plugin/extension filename |
| `settings_paths` | list | `[]` | EXTRA claude-code user-scope `settings.json` files the auto-mode, permissions and hook-bridge writes ALSO reach (each a `.json` path). Honored only when the primary target is `~/.claude/settings.json` — a repo-local (project-scope) file is read by every session regardless of `CLAUDE_CONFIG_DIR`, so it is never fanned out |
| `discover_config_dirs` | bool | `true` | auto-discover every `~/.claude-accounts/account-*/` directory (the `claude-rotate` multi-account launcher's per-account `CLAUDE_CONFIG_DIR`s) and treat each one's `settings.json` as a managed target alongside `~/.claude/settings.json` — same managed keys, same idempotent additive merge + backup-on-conflict, and `rig status` names WHICH file drifts (`harness/claude-code@account-2: …`). Claude Code reads ONLY `$CLAUDE_CONFIG_DIR/settings.json` when that variable is set, so without the fan-out no rig-provisioned hook runs in those sessions while status stays green (rig-cli#368). `rig doctor` lists every config dir a live `claude` could load (`~/.claude`, the shell's `CLAUDE_CONFIG_DIR`, every managed target) and exits 3 when one lacks the hook bridge `~/.claude/settings.json` carries — each gap names the fix that converges THAT dir (`rig apply commit` for a managed target; a `settings_paths` entry for an ad-hoc `CLAUDE_CONFIG_DIR`; re-enabling discovery for an opted-out account dir). Doctor reads these two keys leniently (a config broken elsewhere still yields them). Discovery never creates a directory; a `settings.json` missing inside an existing dir is created |
| `hook_bridge.enabled` | bool | `true` | wire the harness bridge dispatcher so installed agent-hooks actually fire (`cc_hook_bridge` for Claude, `codex_hook_bridge` for Codex, `opencode_hook_bridge` for opencode, `omp_hook_bridge` for omp) |
| `hook_bridge.python` | str | `python3` | the Python interpreter the dispatcher command runs under. For `kind: omp` there is no shell command to inline it into — a custom value instead forces the wrapper module (see below) and sets `OMP_HOOK_BRIDGE_PYTHON`, which `extension.ts` reads before falling back to a bare `python3` |
| `web_search.enabled` | bool | `true` | when omp is a configured harness kind, merge `providers.webSearchExclude` into `~/.omp/agent/config.yml` (or the `PI_CODING_AGENT_DIR` / `PI_CONFIG_DIR` override). `false` skips the write |
| `web_search.exclude` | list[str] | `[perplexity]` | provider ids omp `web_search` must never use, even as fallbacks. Additive merge — names already on disk stay. An explicit empty list is the opt-out. Default excludes Perplexity because its anonymous/cookie path returns a Sign-up wall without an account (rig-cli#447) |

**What gets written, per harness kind.** The auto intent is a pinned `mode:` the PRIMARY kind
knows (auto: claude-code `auto` / `bypassPermissions` / `dontAsk`, codex `auto_review`, opencode
`allow`, omp `yolo`; interactive: claude-code `default` / `acceptEdits` / `plan`, codex `user`,
opencode `ask` / `deny`, omp `always-ask` / `write`) — it is the exact value claude-code has always
written verbatim, overriding `auto_mode`, so every kind follows the same precedence; without a known
`mode:`, `auto_mode` decides — and that one intent then applies to EVERY configured kind. A `mode:`
the primary kind does not know (a claude-code value pasted onto a `kind: codex` block, or a claude-code
mode newer than rig's table) with no `auto_mode` declares NO intent for the OTHER kinds: nothing is
written for them (a claude-code primary still writes its own `mode:` verbatim; omp's approval action
keeps its legacy `yolo` posture, see the table), the plan carries an elevated note naming the primary
kind's real values plus a per-kind "not written" note, and `rig status` lists every affected kind as
not managed — never a silent tighten to interactive. The raw `mode:` string is written verbatim ONLY
for the PRIMARY kind, and only when it is one of that kind's own values (`kind: opencode, mode: deny`
stays `deny` — the exact override the `mode` row documents — never relaxed to the mapped `ask`; a
primary `claude-code` writes any `mode:` verbatim, its vocabulary can outgrow rig's table); every
ADDITIVE kind — including `kinds: [claude-code]`, which is pinned to the user-scope
`~/.claude/settings.json` like every other additive kind — gets the registry value mapped from the
intent, never the raw string (it is the primary harness's value and means nothing to another one).
With no `auto_mode` and no `mode:`, a non-claude kind is left untouched
and the plan notes it (an additive kind listed for skill discovery must not get its posture changed
behind its back). A kind with no such setting (`pi`, `commandcode`) always gets a visible n/a note —
elevated only when it is the PRIMARY kind and the config set `auto_mode`/`mode:` on it (it asked for
something rig cannot write); an additive `kinds: [pi]` listed for skill discovery is informational. omp's key is owned by the permissions approval action; whether that
action is really in the plan (permissions enabled AND omp among its kinds) decides the note — "written
by the approval action" vs an elevated "skipped". The registry is `riglib/harness_mode.py` — one entry
per kind, researched against each harness's own docs/CLI:

| Kind | File (user scope, per machine) | Key | auto | interactive |
| --- | --- | --- | --- | --- |
| `claude-code` | `~/.claude/settings.json` (`auto` only; other modes → `.claude/settings.json`) | `permissions.defaultMode` | `auto` (classifier-mediated auto-approve) | `default` |
| `codex` | `~/.codex/config.toml` (or `$RIG_CODEX_HOME/config.toml`) | `approvals_reviewer` (root key) | `auto_review` — eligible approval prompts go to Codex's reviewer subagent, the analog of claude-code's classifier `auto` | `user` (codex's default) |
| `opencode` | `~/.config/opencode/opencode.json` | `permission."*"` (the documented global default every tool inherits; opencode's `--auto` flag is runtime-only, no config key) | `allow` | `ask` (prompts for everything not covered by a per-tool rule — the rig-managed `permission.bash` allowlist still applies) |
| `omp` | `~/.omp/agent/config.yml` | `tools.approvalMode` — written by the **permissions approval action** (guard-interlocked, receipt-tracked; one owner of the key). A value rig itself installed (per the receipt) is converged when the intent changes; a user-set value is left untouched (drift visible). With `permissions.enabled: false` nothing writes it and the plan says so | `yolo` | `always-ask` (an unspecified intent keeps `yolo`, the rig-cli#202 posture) |
| `pi`, `commandcode` | — | n/a: no documented approval/auto-mode setting (their deny/ask intent is the advisory `AGENTS.md` block) — recorded in `HARNESS_MODE_NA` and printed as a visible note by `rig apply info` and a per-kind line in `rig status` | — | — |

codex: rig deliberately does NOT touch `approval_policy` / `sandbox_mode`. `approval_policy = "never"`
would auto-REJECT escalations (network, outside the workspace), so `gh`/`git push`/`tg` fail fast
under the default `workspace-write` sandbox unless the sandbox network flag is also flipped — a
sandbox change rig must not make silently. Set those by hand if you want a sandbox-less codex.

Only the one managed key is touched; every other setting in the file is preserved (codex's TOML is
edited as text — the root key is upserted before the first `[table]`, comments kept). The write is
**idempotent** (a re-apply with the same value is a no-op) and **backup-noted** (a rewritten prior
file is backed up per `defaults.on_conflict` before converging; a value rig cannot rewrite as a plain
string — a TOML inline table, a mis-shaped parent — is left untouched and reported; a codex
`config.toml` that does not parse is an error, never edited). `rig status`
reports drift if the on-disk value no longer matches the config, and prints a `harness auto-mode per
kind` block naming the key/file/value (or the n/a reason) for every configured kind.

**Self-merge unblock (`self_merge`, auto-mode only).** When `auto_mode` resolves to the
`auto` classifier mode and `self_merge` is `true` (the default), rig provisions **two** additive,
idempotent pieces in the same user-scope settings file so the agent can self-merge a PR it
authored in the session's starting repo via `gh ship`:

1. **`permissions.allow` ship rules — the HARD unblock.** rig appends `Bash(gh ship:*)` and
   `Bash(fj ship:*)`, adds `permissions.deny` rules for the raw `ship.sh` / `pr-ship.sh` scripts (`gh ship` /
   `fj ship` are the only ship doors), and removes the legacy `Bash(*/pr-ship.sh:*)` / `Bash(*/ship.sh:*)`
   allow entries older rig versions wrote (Claude Code rejects that syntax; they never matched). This is the load-bearing piece: the top-level
   auto-mode **Bash permission gate** (the classifier) vetoes `gh ship` *before* it ever judges the
   merge, and that gate is bypassed ONLY by an explicit `permissions.allow` rule. Without these,
   the natural-language carve-out below is inert — `gh ship` never runs.
2. **`autoMode.allow` carve-out — the SOFT-block clearance.** rig appends a `$defaults`-preserving
   carve-out string that clears the *Merge Without Review* and *Self-Approval* soft blocks **for the
   agent's own PRs only** — the semantic layer that judges the merge action *after* the Bash gate
   passes.

Both appends are idempotent (re-apply is a no-op), never remove the user's own entries, and never
touch the sibling `permissions.deny` / `autoMode.soft_deny`/`hard_deny`/`environment` sections. In
particular the **`Bash(gh pr merge:*)` deny stays in force** — `ship.sh` runs `gh pr merge` as a
child process (not a gated tool call), so `gh ship` remains the ONLY merge path; rig whitelists the
ship *gate*, never a raw merge. It is SAFE precisely because the other guards remain: the review-fix
loop, the local CI gate (`ship.sh`), the `block-raw-pr-merge` agent-hook for *other* PRs, and the
anti-exfil hard rule. It is **inert without auto-mode** (the classifier runs only under `auto`).
`rig status` reports drift if either the ship rules or the carve-out are missing while `self_merge`
is configured. Set `self_merge: false` to keep the gate/soft block (the agent asks before merging
its own PR). An agent cannot write these to its own live settings (that trips the Self-Modification
soft block) — run `rig apply` yourself to activate it.

**The hook bridge (`hook_bridge`).** Harnesses only run hooks declared in their own config; they
do not execute the `agents-hooks/v1` descriptors directly. Without a bridge, installed
agent-hooks are inert. So when a supported harness block is present (and `agent_hooks` is
enabled), `rig apply` registers the matching dispatcher from `agent-tools/lib`:

- `kind: claude-code` writes `cc_hook_bridge` into `settings.json`: `PreToolUse` matchers
  `Bash`, `Edit|Write|MultiEdit|NotebookEdit`, `Agent|Task`, `Skill`, `Monitor`,
  `EnterWorktree`, and `Workflow`; `PostToolUse` matcher `Edit|Write|MultiEdit|NotebookEdit`;
  and `Stop`. The `Workflow` matcher is what makes the agent-tools `workflow-danger-gate`
  (`pre-workflow`) hook actually fire. rig does not itself provision a bare `Workflow`
  `permissions.allow` entry (that stays an operator/config-layer choice); but when one is
  configured so ultracode is never prompted on spawn, this matcher is the enforcement half —
  it denies a script whose executable JS runs a raw `gh pr merge`, `git reset --hard`, a
  force-push, `sudo rm`, `--no-verify`, `git clean -f`, or `screencapture`, while leaving
  comments and ultracode's own `agent(...)` dispatch untouched.
  The `Monitor` matcher is the rig half of the **subagent Monitor ban**
  ([rig-cli#463](https://github.com/alex-mextner/rig-cli/issues/463)): Claude Code only
  fires `PreToolUse` for tools with an explicit matcher, so without this entry the
  agent-tools `subagent-no-monitor` hook never runs. A dispatched subagent must not
  call Monitor. Monitor is a fire-and-forget background watch — you start it and get
  notified per event later — and a subagent is **not** re-invoked by that notification
  (only the **main loop** is), so calling Monitor and ending the turn wedges the
  subagent forever with uncommitted work and no PR. The orchestrator's own Monitor
  use (watching a backgrounded child) is unaffected. Replacements a blocked subagent
  should use instead: (1) waiting on a single ordinary command it just started — set
  Bash `run_in_background: true`; the harness auto-resumes the subagent when that
  command exits, no Monitor needed; (2) waiting on anything else (a labeled long
  process, a condition, several things at once) — block in the **foreground** with a
  **heartbeat** loop that echoes at least every ~20s and keeps each Bash call under
  ~540s. Never Monitor from a subagent.
  **Outer timeout (UB-25).** Every managed `cc_hook_bridge` entry also carries
  `"timeout": 1200` (seconds). Claude Code kills a command hook at its `timeout` (600s when the
  field is absent), and a PreToolUse hook that times out does NOT block: its output is
  discarded and the tool call proceeds. The bridge runs every descriptor for a point in
  sequence, each capped at its own `timeout_ms`; the largest shipped is 960s (hatch-capable
  guards: the hatch helper's 900s approval wait plus a 30s margin). So without an explicit,
  larger outer timeout, the harness could cancel a guard mid-approval and fail open. 1200s
  covers one full 960s descriptor plus 240s for the remaining hooks, which return in
  milliseconds on the normal path. Known limit: two hatch waits requested in the same tool call
  (about 1860s) would exceed it. `rig apply` only ever raises the value: a missing, non-numeric,
  non-finite (NaN/inf), or lower `timeout` on a managed entry is set to 1200, while a higher
  operator-set value (int or float) is kept, and your own non-bridge hooks are never touched. `rig status` reports a missing or short
  value as drift, per file, including every fanned-out `~/.claude-accounts/account-*`
  `settings.json`.
- `kind: codex` writes `codex_hook_bridge` into `~/.codex/config.toml` (or
  `$RIG_CODEX_HOME/config.toml`, or `settings_path`) as a managed `[hooks]` TOML block:
  `PreToolUse` matchers `Bash` and `apply_patch`, `PostToolUse` matcher `apply_patch`, and
  `Stop`. A custom Codex `settings_path` must end in `.toml`.
- `kind: opencode` symlinks `opencode_hook_bridge/plugin.js` into
  `.opencode/plugins/zz-agent-tools-hook-bridge.js` (or `settings_path`) so opencode
  auto-loads it. The repo-local `zz-` path is deliberate: opencode documents global plugins as
  loading before project `.opencode/plugins`, and project plugins can mutate tool arguments, so
  the security bridge must run from the project plugin source after normal project plugins. The
  symlink is added to the repo's `.git/info/exclude` because its target is machine-local. When
  this repo-local bridge is applied, rig also removes the legacy managed global symlink at
  `~/.config/opencode/plugins/agent-tools-hook-bridge.js` if it still points at an agent-tools
  opencode bridge plugin. The plugin dispatches `tool.execute.before` for `bash`, `edit`, `write`, `apply_patch`,
  and `task`, plus `tool.execute.after` for write tools. A custom opencode `settings_path` must
  end in `.js`.
- `kind: omp` symlinks `omp_hook_bridge/extension.ts` into
  `~/.omp/agent/extensions/zz-agent-tools-hook-bridge.ts` (or `settings_path`) — omp's built-in
  extension loader auto-discovers every `.ts`/`.js` module under `extensions/`, so no separate
  registration step is needed. The DEFAULT target is a **user-level** path under the resolved
  omp agent root; like Codex's and opencode's own `settings_path`, an explicit override can
  point anywhere, including inside a repo — but unlike opencode's DEFAULT repo-local plugin
  path, rig does **not** auto-exclude an omp override that lands inside a repo from git (that
  convention is specific to opencode's own default, not a general rule), and there is no legacy
  predecessor to clean up (this bridge is new). The extension dispatches omp's
  `tool_call` (pre-execution) for `bash`/`edit`/`write`/`apply_patch`/`notebook`/`task`, and
  `tool_result` (post-execution, log-only) for the write tools — shelling into the Python
  dispatcher exactly like the opencode plugin does. A custom omp `settings_path` must end in
  `.ts`. The same apply also merges `task.agentIdleTtlMs` into `~/.omp/agent/config.yml`
  (additive; never clobbers a user-set positive TTL) with a **300000 ms (5 min) grace
  window** — shorter than omp's own stock 420000 ms/7 min product default (a deliberate
  choice, not an accidental inherit) and bounded, unlike an earlier `0` (never park until
  process exit) that let idle/zombie helper sessions accumulate without bound. New omp
  processes pick this up at Settings.init so a yielded helper stays on the *default* `hub
  list` view long enough for a follow-up before parking (a parked peer stays fully
  reachable regardless via `hub send`/`history://`/`agent://`); already-running omp
  processes keep the spawn-cached TTL until restart. Override the window with
  `harness.idle_ttl.ms` (`<= 0` restores the old never-park behavior), or skip provisioning
  entirely with `harness.idle_ttl.enabled: false`
  ([rig-cli#445](https://github.com/alex-mextner/rig-cli/issues/445),
  [rig-cli#464](https://github.com/alex-mextner/rig-cli/issues/464)).
- `kind: codex` also merges `skills.max_context_tokens` into `~/.codex/config.toml` (or
  `$RIG_CODEX_HOME/config.toml`) with a **50000-token** default — Codex CLI truncates/omits
  skill descriptions from its model-visible catalog once a personal skill library's rendered
  text exceeds Codex's own (much smaller) stock budget for that catalog
  ([openai/codex#24299](https://github.com/openai/codex/issues/24299)); 50000 was verified to
  roughly double the visible description length and plateau around 40KB of rendered skill text.
  Additive and idempotent, exactly like the omp idle-TTL write above: apply only writes the key
  when it is ABSENT — a value already on disk (lower or higher than 50000) is never clobbered,
  only reported as drift. Override with `harness.codex_skills_budget.max_context_tokens`, or
  skip entirely with `harness.codex_skills_budget.enabled: false`. Fires whenever `codex` is ANY
  configured harness kind (primary `kind` or an additive `kinds` entry), independent of whether
  an auto/permission-mode intent was ever declared for it.

Claude and Codex bridge commands run `PYTHONPATH=<agent-tools>/lib python3 -m <bridge_module>
<Event>` from the harness config; opencode and omp load their respective JS/TS module, which
shells into the Python bridge with the same `PYTHONPATH`. The merge/symlink is idempotent and
preserves unrelated config; `rig status` reports the bridge as missing/stale drift if a managed
hook is absent or points at an old checkout. Pi and CommandCode still skip the bridge with a
note — surfaced on every plan build, not only when `hook_bridge.enabled: true` is set explicitly,
since a harness with zero agents-hooks coverage is a real gap the user should always see.

When `agent_hooks.target` or a non-legacy `defaults.hooks_target` pins descriptors to one custom
directory, rig still registers the configured supported bridges and passes that descriptor dir to
the dispatcher. Claude/Codex receive the override in their managed hook command; opencode and omp
each get a small managed wrapper module (`.js`/`.ts`) that sets `OPENCODE_HOOKS_DIR` /
`OMP_HOOKS_DIR` respectively before delegating to the stock bridge. omp also forces this same
wrapper (setting `OMP_HOOK_BRIDGE_PYTHON` alongside `OMP_HOOKS_DIR`, or alone) whenever
`hook_bridge.python` is configured — a plain symlink has no shell command to inline the
interpreter into, so without the wrapper the setting would be silently ignored.

For Codex, the bridge also converges global hook feature flags that would make the managed hook
block inert: `[features] hooks = false`, top-level `features.hooks = false`, and the deprecated
top-level `codex_hooks = false` are reported as drift and rewritten to `true` when `hook_bridge`
is enabled.

Codex `pre-agent`, `pre-skill`, `pre-monitor`, and `pre-worktree-enter` are not wired yet: rig
may install `pre-agent`/`pre-skill`/`pre-monitor`/`pre-worktree-enter` descriptors into
`~/.codex/hooks` (or `$RIG_CODEX_HOME/hooks`) alongside the rest of the selected catalog, but
the Codex bridge currently dispatches only `pre-bash`, `pre-write`, `post-write`, and `stop` via
the confirmed Codex hook events above — Codex has no `Skill`-, `Monitor`-, or
`EnterWorktree`-equivalent tool dispatch mapped either. opencode has `task` tool dispatch for
`pre-agent` but no `Skill`-, `Monitor`-, or `EnterWorktree`-equivalent dispatch (`pre-skill`,
`pre-monitor`, `pre-worktree-enter`), and no stop-equivalent blocking hook is documented, so the
opencode bridge does not dispatch `stop`; use Claude Code for workflows that require
`pre-skill`, `pre-monitor`, `pre-worktree-enter`, or stop-time enforcement until opencode/Codex
expose the matching contracts.
The managed TOML block has this shape, with full commands including the resolved
`PYTHONPATH=<agent-tools>/lib`:

```toml
[hooks]
# >>> rig managed: codex hook bridge
PreToolUse = [{matcher = "Bash", hooks = [{type = "command", command = "... codex_hook_bridge PreToolUse"}]}, {matcher = "apply_patch", hooks = [{type = "command", command = "... codex_hook_bridge PreToolUse"}]}]
PostToolUse = [{matcher = "apply_patch", hooks = [{type = "command", command = "... codex_hook_bridge PostToolUse"}]}]
Stop = [{hooks = [{type = "command", command = "... codex_hook_bridge Stop"}]}]
# <<< rig managed: codex hook bridge
```

Codex TOML merge is intentionally conservative: rig refuses to add the managed bridge when the
top-level `hooks` table already uses an inline table (`hooks = { ... }`), array-of-tables
(`[[hooks]]`), dotted hook keys (`hooks.PreToolUse = ...`), nested hook tables
(`[hooks.foo]`), or unmanaged keys for events rig owns (`PreToolUse`, `PostToolUse`, `Stop`)
inside `[hooks]`. It also refuses files containing TOML triple-quote tokens (`"""` or `'''`),
because multiline strings can contain table-shaped text that this preservation merge must not
reinterpret.
Unrelated `[hooks]` keys such as `Notification` are preserved, as are `hooks` keys inside other
tables such as `[profiles.default]`.

---

## `permissions`

Reconciles the **per-harness permissions layer** (rig-cli#100): the command **ALLOWLIST** — our
ecosystem CLIs and a small read-only helper set **pre-allowed**, so the agent never stops to ask
permission for a known-safe command — plus, for claude-code, the conservative
**deny/ask rule baselines** (the outer enforcement belt: the harness evaluates
deny → ask → allow *before* PreToolUse hooks and independently of the model; the argv-parsing
agent-hook guards stay the deep layer underneath). **Default ON**: an absent/empty
`permissions:` block still provisions the default tool set AND the deny/ask baselines, so
`rig init`/`rig apply` on a clean machine gets both with no config at all. Everything is
**config-driven** — `tools` REPLACES the default set, `extra` adds, `disable` removes; `allow`
adds RAW rule entries on top of the tool-derived allowlist; `deny`/`ask` REPLACE the baked rule
baselines (an explicit `[]` disables one). The merge is **additive** — every existing entry in
every list (auto-mode, your accumulated allowlist, your own deny/ask rules) is preserved, the
desired entries are merged in, deduped; a re-apply is a no-op.

**Scope of the default set, honestly.** rig pre-allows the *tools* in the list at the
command-prefix level — for claude-code that is `Bash(<tool>:*)`, which covers every subcommand and
flag of that tool. That is why the default set is deliberately narrow: lifecycle operations go
through the standalone `dev` CLI (alex-mextner/dev-cli), and rig grants `Bash(dev:*)` as the
single safe development surface. rig does **not** grant raw process-control, package-manager, or
broad external-write tools by default: no `kill`, `lsof`, `ps`, `pgrep`, `pkill`, `docker`, `bun`,
`npm`, `uv`, or `git`. The safety boundary for those workflows belongs inside `dev`, where the CLI
can validate project ownership and intent. If you need a repo-specific raw grant, opt into it
explicitly with `tools`, `extra`, or a raw `allow` entry.

**`gh` (the GitHub CLI) is the one exception** — it IS in the default set, because everyday agent
use of it is read-heavy (`gh pr view`, `gh issue list`). Be honest about what that trades in: `gh`
is a broad write transport, not just a merge tool. `gh api -X PUT/POST ...` reaches the same REST
surface a merge would use, `gh api graphql` can run a merge mutation directly, and unrelated
commands (`gh auth token` prints the OAuth token; `gh repo delete`, `gh secret set`) are just as
reachable under a blanket `gh` grant. The baked `gh-pr-merge` deny/forbidden rule below covers ONLY
the direct, argv-prefix `gh pr merge` form on every harness belt — a global flag ahead of the
subcommand (`gh --repo o/r pr merge 5`) or the `gh api`/`gh api graphql` merge routes above are NOT
caught by any of these prefix/glob rules. What DOES catch those, on claude-code and codex (where
rig's `agent_hooks` hook-bridge feature is provisioned), is agent-tools' own argv-level
`block-raw-pr-merge` hook — the same "coarse belt here, argv-parsing deep layer in the hook bridge"
split this doc already uses for the force-push/`--no-verify` gaps below. That hook does not exist
for opencode or omp, and even where it runs it targets merges specifically — the wider `gh auth
token`/`gh repo delete`/`gh secret set` surface stays open under the default grant everywhere. Set
`allow_gh: false` (or `disable: [gh]`) to opt back out of the `gh` grant entirely, closing all of
the above at once — see `riglib/permissions.py` for the full accounting. One opencode-specific
side effect worth knowing: rig now re-anchors its own deny/ask keys to the end of `permission.bash`
on every apply (see the opencode deny/ask baseline note below), so a user's own key that
positionally overrides a baked deny by sitting after it in the file stops working on the next
apply. Use `permissions.deny: [...]` to relax a baked rule instead of relying on key position.

The allowlist file itself is per-machine. `permissions` may live in the global config for
machine-wide defaults, and repo-local `permissions:` remains accepted for compatibility with
existing committed configs.

```yaml
permissions:
  enabled: true                # provision the permissions layer (false → leave the harness config alone)
  # kind: opencode             # target one harness allowlist explicitly (default: supported harness.kind + harness.kinds)
  # tools: [tg, review, draw, 3d, rig, task, dev, pm, research, rg, jq, gitleaks, gh]  # REPLACES the default set
  # extra: [kubectl]           # ADD to the (default or explicit) set
  # disable: [gitleaks]        # drop from rig's desired set (won't ADD it; never removes a live entry)
  # allow_gh: false            # opt out of the gh grant specifically (sugar over disable: [gh])
  # allow:                     # RAW rule entries asserted present in the allow list (on TOP of tools)
  #   - WebFetch
  #   - "Read(//private/tmp/reports/**)"
  # deny: []                   # REPLACES the baked deny baseline ([] disables it; absent → baseline)
  # ask: []                    # REPLACES the baked ask baseline ([] disables it; absent → baseline)
  # settings_path: ~/.claude/settings.json   # override the per-harness settings file (rare; JSON)
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | provision the permissions layer (set `false` to leave the harness config untouched) |
| `kind` | `claude-code` \| `opencode` \| `codex` \| `omp` \| `pi` \| `commandcode` \| `null` | supported `harness.kind` plus `harness.kinds`, else `claude-code` | which harness's permissions to provision. Every known harness has a surface, so every kind is pinnable: allowlist kinds get the additive merge, omp gets the generated guard extension + approval posture (tier 1), codex the execpolicy block (tier 1), pi/commandcode the advisory instruction policy (tier 3). When absent or `null`, rig fans out to every configured harness kind |
| `tools` | str[] | the default set | the command names to pre-allow; **replaces** the default set wholesale. Naming `gh` here explicitly means THIS repo's own config never receipts it as a rig default — if THIS repo already held a receipted default grant for `gh`, that ownership clears (a later `allow_gh: false` from this same repo won't revoke it; see `allow_gh` below). On a settings file **shared** with another repo, this has no effect on a grant that OTHER repo still owns (rig-cli#549): it stays receipted under its original owner, and a later `allow_gh: false` — from any repo sharing the file — can still revoke it |
| `extra` | str[] | `[]` | command names to ADD on top of the (default or explicit) set. A `gh` added THIS way is treated the same as an explicit `tools` declaration naming `gh` — see the `tools` row above for the same same-repo-clears / other-repo-can't-erase distinction |
| `disable` | str[] | `[]` | command names to drop from rig's **desired** set, so rig won't ADD them. NB: this is additive-only for every command EXCEPT `gh` — it does NOT delete an entry already in your allowlist (rig never removes the user's entries; that stays your call), UNLESS the entry is a `gh` grant rig itself receipted as its own default: `disable: [gh]` (like `allow_gh: false`) revokes that specific one — see `allow_gh` below |
| `allow_gh` | bool | `true` | pre-allow the GitHub CLI (`gh`); `false` opts out (sugar over `disable: [gh]`, which also works) and REVOKES a `gh` entry rig itself granted as a default (tracked in a forward-only, per-repo-owned sidecar receipt, `<settings file>.<kind>.rig-permissions-grants.json` — rig-cli#333/#549; the harness `kind` is part of the name so two different harnesses independently pointed at the same shared settings file never share a receipt, and each receipted entry records WHICH repo's apply granted it, so one repo's own `tools`/`extra`/raw-`allow` declaration can only ever clear THAT SAME repo's ownership, never a different repo's — closing a cross-repo laundering gap on a shared settings file). Forward-only means: a `gh` entry that predates this feature, or one you hand-typed via `permissions.allow`/`extra`/an explicit `tools` list naming `gh`, is never receipted as THIS apply's own grant — if THIS repo already held the receipted default grant, that ownership clears (`rig status` then reports it as a kept "extra" instead); if a DIFFERENT repo sharing the settings file still owns it, that grant is untouched and stays revocable by a later `allow_gh: false` from any repo sharing the file. A `permissions.allow` entry that explicitly re-asserts `gh` wins over `allow_gh: false` (no revoke-then-re-add). The baked deny rule blocks the direct `gh pr merge` form on every harness either way — it does not cover `gh api`-based merge routes or `gh`'s other write surface, see the `gh` note above |
| `allow` | str[] | `[]` | RAW permission-rule entries (`Tool` or `Tool(specifier)`, e.g. `WebFetch`, `Bash(kubectl get:*)`, `Read(//tmp/**)`) asserted present in the allow list, on TOP of the tool-derived entries — this is how a hand-grown allowlist is adopted as declared config. claude-code only — see below |
| `deny` | str[] | the baked deny baseline | rule entries asserted present in the deny list; **replaces** the baseline wholesale (`[]` disables it). claude-code only — see below |
| `ask` | str[] | the baked ask baseline | rule entries asserted present in the ask list; **replaces** the baseline wholesale (`[]` disables it). claude-code only — see below |
| `settings_path` | path (JSON) | per-harness default | override the settings file to merge into (default: `~/.claude/settings.json` for claude-code, `~/.config/opencode/opencode.json` for opencode). A suffixed path is treated as the file; a suffixless path is treated as a directory. With multiple harnesses and no explicit `permissions.kind`, one override can only name one file, so rig targets the first supported harness and notes the skipped kinds |

**Default tool set.** Our ecosystem CLIs — `tg`, `review`, `draw`, `3d`, `rig`, `task`, `dev`,
`pm`, `research` — plus read-only helper tools: `rg`, `jq`, `gitleaks` — plus the GitHub CLI,
`gh` (the one non-read-only exception; see below). `pm` (pm-cli) and `research` (research-cli) are
read-only ecosystem coordinators — an observing/reconciling project manager and a multi-provider
research/panel CLI — that never edit code, so they share the safe profile of `review`/`task`. For
claude-code, the `dev` entry is written as `Bash(dev:*)`, which is the provisioned `dev:*`
permission surface for project-local development workflows. The `dev` implementation lives in
agent-tools; rig provisions the harness permission for it but does not implement its runtime
process-safety checks. Rig does **not** add raw process-control, package-manager, container, or
VCS commands such as `kill`, `lsof`, `ps`, `pgrep`, `pkill`, `docker`, `bun`, `npm`, `uv`, or `git`;
those safety checks belong inside `dev`.

This default is additive, not retroactive cleanup: if an older rig already added broad entries such
as `Bash(git:*)`, `Bash(uv:*)`, or `Bash(bun:*)` to a local harness settings file, `rig apply` will
not remove them. Prune those live entries manually after confirming `dev` covers the workflow, or
adopt intentional repo-specific exceptions under `permissions.allow` / `extra`.

**Multi-harness migration note.** When `permissions.kind` is absent or explicitly `null`,
permissions fan out to every configured harness kind with an additive allowlist implementation
(`harness.kind` plus `harness.kinds`). Existing configs that want single-harness behavior should
pin `permissions.kind` to that one harness, such as `claude-code` or `opencode`.

**Baked deny/ask baselines (claude-code, rig-cli#100).** Deliberately conservative and
word-boundary precise — a deny rule that false-positives on legitimate commands teaches agents
to route around the belt. Deny: `Bash(gh pr merge:*)` (merges go through `gh ship`),
`Bash(git push --force:*)` + `Bash(git push * --force *)` + `Bash(git push * --force)` +
`Bash(git push -f:*)` + `Bash(git push * -f *)` + `Bash(git push * -f)` (force pushes in
flag-first, mid AND end-anchored positions; `--force-with-lease` is NOT matched — the word
boundary excludes it), `Bash(git commit --no-verify:*)` (flag-first only: the flag-anywhere form
cannot be pattern-matched without false-positiving on commit messages that merely mention the
flag — the `block-no-verify` agent-hook remains the authoritative argv-level guard),
`Bash(sudo rm:*)`, `Bash(screencapture:*)` (screenshots go through Playwright/CDP),
`Bash(osascript:*)` (AppleScript/System Events from bash trigger unapprovable macOS TCC "access
data from other apps" prompts — same class as screencapture), and five
`Bash(rg --pre...)` forms (rig-cli#187: `rg --pre` runs an arbitrary preprocessor command
underneath the read-only `Bash(rg:*)` search grant — flag-first, later position, and both the
space/`=` value shapes; a documented fidelity gap in the later-position forms can false-positive
on a search for the literal text `--pre` — a DIFFERENT call than `--no-verify`'s, made on purpose:
see `riglib/permissions.py` for why the two gaps are handled oppositely. `--pre-glob` is
deliberately denied NOWHERE, not even by the omp ARGV guard — see `riglib/permissions.py` (it
has no effect without `--pre`, which is caught on its own; a bare `--pre-glob` deny is a false
positive with no security value). UNLIKE `--no-verify`, this belt has no `block-rg-pre`
argv-level hook backstopping it yet, so it stops accidental use, not a deliberate bypass —
`rg '--pre' cmd` (quoted) or `rg --pre<TAB>cmd` (tab, not space) match none of these five
patterns (a literal SPACE is required); a claude-code argv hook is a tracked follow-up, not yet
built). `Bash(git worktree add:*)` (raw worktree creation goes through `rig worktree create`;
the child `git worktree add` inside `rig` is not a gated tool call; prefix-only — `git -C`
/`--git-dir` forms do not match, and a mid-pattern would false-positive a commit message
that mentions "worktree add"), and `EnterWorktree`
(whole-tool deny on purpose: the tool can CREATE a worktree at a model-supplied path, which
is the location-bypass `rig worktree create` exists to close — CC-only, no equivalent on
opencode/codex/omp). Ask (prompt, don't block): `Bash(pkill:*)`,
`Bash(killall:*)`, `Bash(git reset --hard:*)` + `Bash(git reset * --hard *)` +
`Bash(git reset * --hard)`. The full annotated list lives in `riglib/permissions.py`.

**Baked deny/ask baselines (opencode).** opencode now gets its OWN deny/ask baseline written into
the `permission.bash` object (values `"deny"`/`"ask"`), hand-written in opencode's glob dialect
(`*` = zero-or-more chars, `?` = one char, **last matching key wins**). Deny: `"gh pr merge*"`,
`"git push*--force*"`, `"git push -f*"`, `"git commit --no-verify*"`, `"sudo rm*"`,
`"screencapture*"`, `"osascript*"`, `"git worktree add*"`, `"rg*--pre *"` + `"rg*--pre=*"` + `"rg*--pre"` (value-shaped / end-anchored,
so they don't also deny the legitimate `--pretty` flag — a single substring-only `"rg*--pre*"`
entry was rejected for exactly that regression). Ask: `"pkill*"`, `"killall*"`, `"git reset*--hard*"`.
rig emits these AFTER
the allow keys so they sit past any broad `"*"`/allow (last-match-wins ordering discipline). Two
deliberate **fidelity gaps**, documented: (1) opencode `*` has NO word boundary, so
`"git push*--force*"` also matches `--force-with-lease` (the safe force) that the claude-code
word-boundary matcher excludes — the precise flag-position guard stays in the opencode PreToolUse
plugin bridge (same split as claude-code); (2) the user-facing `allow`/`deny`/`ask` config lists
are claude-code dialect (`Bash(...)` specifiers), so under `kind: opencode` a configured raw list
is **dropped with a visible plan note** and the baked opencode-dialect baseline is used instead (a
claude-shaped rule written as an opencode glob key would never match). The tool-derived allowlist
(`tools`/`extra`/`disable`) works for opencode as before.

**codex execpolicy (allow + coarse deny).** codex has no config allowlist, so rig writes a
marker-delimited managed block of Starlark `prefix_rule(...)` lines into
`~/.codex/rules/rig-managed.rules` (codex auto-scans `~/.codex/rules/*.rules` at startup — no
`config.toml` reference needed). The resolved tool set becomes `decision="allow"` prefix rules; a
MINIMAL coarse deny (`gh pr merge`, `git worktree add`, `sudo rm`, `screencapture`, `osascript`,
and — rig-cli#187 — the flag-first 2-token `rg --pre`) becomes `decision="forbidden"`. **Fidelity gap:** `prefix_rule` matches a
leading-token prefix, so a coarse `["git","push"]` forbidden would over-block ALL pushes — the
coarse deny is therefore kept to unambiguous full-command (or unambiguous flag-first) bans only;
every LATER-position flag-guard (force-push, `--no-verify` anywhere, `rg --pre` in a non-leading
or joined `=` position) stays in the PreToolUse hook bridge (same split as claude-code) — but
codex has NO such bridge today, so those later-position forms are a real, tracked, pre-existing
gap here, not silently absent (see `riglib/permissions.py`, `CODEX_DENY_RULES`). The block is
idempotent, backup-noted, preserves any lines outside its markers, and `rig status` surfaces it
as `missing`/`modified` drift.

**omp guard extension + approval posture (tier 1, rig-cli#202).** omp has no per-command
allowlist (its approval is per-tool), but it auto-discovers TS extensions under
`~/.omp/agent/extensions/` whose `tool_call` handler can block a bash call before execution.
rig writes the wholly rig-owned, GENERATED `rig-permissions-guard.ts` there — codegen'd from
the single structured rule registry in `riglib/permissions.py` (`riglib/omp_guard.py`
renders it; never a hand-copied list). The guard tokenizes the full command and matches at
the argv level per pipeline stage, so it is MORE precise than the glob belts:
`git commit --no-verify` is caught in ANY flag position and `--force-with-lease` never
false-positives (exact-token matching). The deny baseline blocks outright; the ask baseline
confirm-gates via the harness UI and BLOCKS headless (a prompt nobody can answer never
auto-approves). A runtime event-shape self-check fails closed with a loud
`rig-permissions-guard.incompatible` marker if omp ever renames the `tool_call` contract.
Block reasons carry rule ids + static hints only — never command contents (secrets). The
write is atomic (temp + rename), idempotent on byte-compare, and backed up before any
replace. Alongside it, `provision_harness_approval` merges `tools.approvalMode: yolo` into
`~/.omp/agent/config.yml` ADDITIVELY (parity with claude-code's `auto_mode: true` — the
guard is the enforcement layer, the YAML is posture), tracked by a sidecar receipt
(`.rig-permissions-receipt.json`: managed keys, previous values, backup identity); a
matching value without the receipt is 'compatible unmanaged' — adopted, never clobbered —
and a differing user value is drift, never overwritten. An opt-in ACTIVATION probe
(`RIG_OMP_PROBE=1 rig doctor`) proves the block channel end-to-end with a temporary
nonce-blocking fixture extension; it stays out of the apply path (apply is
offline/declarative). Known gap, documented: the guard covers model-issued tool calls in
trusted repositories — wrapper indirection (`sh -c '…'`, command substitution `$(…)`,
subshells, copied binaries, aliases) hides the command string from argv matching; it is not
a sandbox.

**pi/commandcode advisory instruction policy (tier 3).** These instruction-file harnesses
have no execution-layer mechanism at all, so rig splices a marker-delimited ADVISORY block
(deny/ask intent, from the same rule registry) into their global `AGENTS.md` — truthfully
tensed "advisory policy — not enforced at the execution layer", terse (it rides every
turn's context). Idempotent, additive, backup-noted, drift-surfaced like the execpolicy
block.

**What gets written, per harness (keyed off `permissions.kind`, else supported `harness.kind` plus `harness.kinds`).**

- **claude-code** — `permissions.allow` in `~/.claude/settings.json` (a JSON array) gains an
  entry per tool in the form `Bash(<tool>:*)` (matching the prefix-glob form CC honors). The
  array is merged additively + deduped; every other entry survives.
- **opencode** — `permission.bash` in `~/.config/opencode/opencode.json` (a JSON object) gains a
  `"<tool> *": "allow"` entry per tool, plus the baked deny/ask baseline (`"<glob>": "deny"`/`"ask"`),
  only when the key is absent — an existing user `"deny"`/`"ask"` is never downgraded. deny/ask keys
  are emitted after the allow keys (last-match-wins ordering).
- **codex** — the command allowlist is **N/A** in `config.toml`, but the allow + coarse-deny effect
  IS delivered: rig writes a managed `prefix_rule(...)` block into `~/.codex/rules/rig-managed.rules`
  (a `provision_execpolicy` action; codex auto-scans that dir at startup). Flag-position denies stay
  in the PreToolUse hook bridge.
- **pi** — **N/A** for a per-command config allowlist (no documented mechanism that leaves the
  toolset intact), but the deny/ask EFFECT is delivered via a DIFFERENT surface: rig installs the
  `permission-guard` extension into `<pi agent root>/extensions/` and writes the rig-owned policy
  file it reads (`rig-permission-policy.json`, the SAME deny/ask baseline as the other harnesses'
  belts, in the extension's flag-anywhere/argv rule dialect). `permissions.deny`/`ask` overrides
  (claude-code's rule-string dialect) do not translate into that structured dialect, so a
  non-empty override is either dropped with a note (at least one OTHER configured permissions
  target — in practice, claude-code — can still consume it) or a config-time error (NO configured
  target can consume it — not just "pi is the sole target": `harness.kind: pi` with
  `harness.kinds: [opencode]` is equally an error, since opencode also does not consume the raw
  dialect — the override would otherwise be accepted then enforced nowhere). See
  `riglib/permissions.py`'s `HARNESS_PERMISSION_EXTENSIONS`, `CONFIG_RULE_DIALECT_KINDS`, and
  `pi_policy_document`.

The write is **idempotent** (a re-apply with the same config is a no-op) and **backup-noted** (the
prior file is backed up per `defaults.on_conflict` before converging; a file that fails to parse as
JSON is backed up before being rewritten for any policy other than `skip`, which leaves it
untouched and surfaces the drift). `rig status` reports a desired entry that is absent as `missing`
drift `rig apply` would add; a wrong-shape file (non-object root, non-array/non-object container) as
`modified` (matching the error `apply` would raise — status and apply agree on what to fix); and a
user entry BEYOND the rig-managed baseline as `extra` drift — reported, **never deleted** (allow
extras are summarized into one counted item, since a live allowlist accumulates hundreds of
hand-approved entries; deny/ask extras are named per entry). To silence an allow `extra`, adopt the
entry into `permissions.allow` in the config or prune it from the settings file by hand.

---

## `scripts`

Defines repo-level command aliases consumed by the **agent-tools** `dev` CLI:
`dev run <name>` reads this mapping and runs the selected command. rig's responsibility is
limited to validating and documenting the `rig.yaml` shape, and to provisioning the harness
permission surface for `dev:*` / `Bash(dev:*)`. The actual `dev` implementation and its process
safety checks live in agent-tools. These aliases are intentionally independent of package-manager
script systems, so Python tools, shell scripts, and repos without `package.json` can expose named
commands the same way.

```yaml
scripts:
  test: python -m pytest -q
  lint:
    cmd: ruff check .
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `scripts` | map | `{}` | repo-level commands keyed by the name passed to `dev run <name>` |
| `scripts.<name>` | string \| map | — | either a command string, or a mapping with `cmd` |
| `scripts.<name>.cmd` | string | — | command line run by `dev run <name>` |

The schema is intentionally conservative: a script entry must be either a string or exactly a
mapping with `cmd`; unknown keys inside the entry are rejected. This keeps rig's contract stable
while leaving execution, process ownership, and destructive-command checks inside `dev`.
Because the agent harness sees only `dev run <name>`, not the nested command string, `dev` is the
trust boundary for these aliases and must reject commands that are not a safe, project-owned
development workflow.

---

## `dev`

Declares repo-level metadata consumed by the **agent-tools** `dev` CLI for development server and
e2e lifecycle flows. Commands still live under top-level `scripts:`; `dev:` only names which
script is the default server/e2e command and records metadata the dev CLI can use for readiness,
owned-process checks, logs, and test artifacts. rig validates this shape, but it deliberately does
not cross-reference script names against `scripts:`; a missing script is reported by `dev` at
runtime. rig provisions `dev:*` / `Bash(dev:*)`; the actual lifecycle implementation and safe
process handling live in agent-tools (see agent-tools#208).

```yaml
scripts:
  server: npm run dev
  e2e:
    cmd: npx playwright test

dev:
  server:
    script: server
    url: http://localhost:3000
    ready_url: http://localhost:3000/health
    ports: [3000, 5173]
    process_matchers: ["vite", "npm run dev"]
    logs_root: .dev/logs/server
  e2e:
    script: e2e
    requires_server: true
    artifacts_root: test-results
    logs_root: .dev/logs/e2e
    jobs:
      smoke:
        script: e2e-smoke
        requires_server: true
        artifacts_root: test-results/smoke
        logs_root: .dev/logs/e2e-smoke
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `dev.server` | map | `{}` | development server metadata |
| `dev.server.script` | string | — | name of the top-level `scripts.<name>` entry that starts the dev server |
| `dev.server.url` | string | — | base URL the development server serves |
| `dev.server.ready_url` | string | — | URL the dev CLI can poll before running e2e |
| `dev.server.port` | int | — | a single known TCP port — dev-cli's fallback alias for `ports: [port]`, used ONLY when `ports` is absent. Declaring both `port` and `ports` on the same server is rejected (one of the two would be silently ignored at runtime); declare exactly one. |
| `dev.server.ports` | int[] | `[]` | known TCP ports the dev CLI may check for this project's server |
| `dev.server.process_matchers` | str[] | `[]` | process command substrings the dev CLI may use to identify owned server processes |
| `dev.server.logs_root` | path | — | directory where the dev CLI writes server logs |
| `dev.e2e` | map | `{}` | end-to-end test metadata |
| `dev.e2e.script` | string | — | name of the top-level `scripts.<name>` entry that runs e2e tests |
| `dev.e2e.requires_server` | bool | `true` | whether e2e expects the dev server to be running |
| `dev.e2e.artifacts_root` | path | — | directory where e2e writes artifacts such as traces, screenshots, or reports |
| `dev.e2e.logs_root` | path | — | directory where the dev CLI writes e2e logs |
| `dev.e2e.jobs` | map | `{}` | named e2e jobs keyed by job name |
| `dev.e2e.jobs.<name>.script` | string | — | name of the top-level `scripts.<name>` entry for this job |
| `dev.e2e.jobs.<name>.requires_server` | bool | `true` | whether this job expects the dev server to be running |
| `dev.e2e.jobs.<name>.artifacts_root` | path | — | directory where this job writes artifacts |
| `dev.e2e.jobs.<name>.logs_root` | path | — | directory where this job writes logs |

No raw process-control command belongs here. Do not add `kill`, `lsof`, `ps`, `pgrep`, `pkill`,
`docker`, `bun`, `npm`, `uv`, `git`, or similar commands to `permissions.tools` to support
this flow; configure scripts and route lifecycle work through `dev`, whose implementation owns the
safety boundary.

For explicit multi-project sessions, pass additional project roots through the environment rather
than committed config:

```bash
DEV_PROJECT_PATHS=/abs/app:/abs/service dev e2e run smoke
```

`DEV_PROJECT_PATHS` is a colon-separated list of additional project paths for the agent-tools
`dev` CLI to consider in the current session. It is intentionally not a `rig.yaml` key; committed
config describes this repo, while cross-repo coordination is session-scoped.

---

## `models`

Provisions a **daily cron that runs the agent-tools model-freshness checker**
(`lib/checker/model_freshness.py`), which polls provider model-list endpoints and proposes
version bumps to the model board (`agent-tools/lib/contracts/models.yaml`). Per the
provisioning rule, on **`rig init` AND `rig apply`** rig **checks whether the schedule is
installed and installs it if missing** (idempotent — a re-apply that finds it present and
current is a no-op).

```yaml
models:
  enabled: true                # provision the daily checker schedule (false → leave system cron alone)
  schedule:
    time: "12:00"              # daily run time, HH:MM 24h (default: noon)
    # label: ai.hyperide.model-freshness   # launchd Label / crontab sentinel (advanced)
  # checker_path: ~/xp/agent-tools/lib/checker/model_freshness.py   # default: resolved from agent_tools_source
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` (scaffold) | provision the daily schedule (set `false` to leave the system cron untouched) |
| `schedule.time` | `HH:MM` | `12:00` (noon) | daily run time, 24-hour; fail-closed on a malformed/out-of-range value |
| `schedule.label` | str | `ai.hyperide.model-freshness` | the launchd Label / crontab sentinel identity (one string across platforms) |
| `checker_path` | path | resolved from `agent_tools_source` | the `model_freshness.py` the schedule runs |

**Cross-platform.** The CTO asked for a "cron"; rig provisions the platform-native
equivalent:

- **macOS → launchd.** A `~/Library/LaunchAgents/<label>.plist` with a daily
  `StartCalendarInterval` (Hour/Minute), loaded via `launchctl load`. (cron is
  deprecated/unmanaged on macOS; launchd is the supported scheduler.)
- **Linux → crontab.** A single managed crontab line `MIN HOUR * * * python3 <checker>`,
  fenced by a `# rig-managed: <label>` sentinel comment so it is idempotent (re-apply finds
  it by sentinel) and removable, and so it never disturbs the user's other crontab lines.

**Idempotency (the "check whether the cron exists and install it if missing" rule).** Both `init` and `apply`
run this. A present-and-current schedule re-applies as a no-op (`skipped`); a missing or
drifted one is (re)installed. `rig status` reports the schedule explicitly (installed /
drifted / not configured) and surfaces a wrong run time or checker path as drift; `rig
doctor` flags a missing scheduler binary (`launchctl`/`crontab`).

Set `RIG_SCHEDULE_DRY_RUN=1` to write the artifact file but **skip the live daemon mutation**
(no `launchctl load`, no `crontab` write) — for CI, containers, or smoke tests where touching
the real per-user scheduler is unwanted. The same skip happens **automatically when `$HOME` is
overridden** (the resolved `Path.home()` differs from the uid's real login home): a HOME-only
sandbox cannot redirect the per-user launchd/crontab, so rig writes the artifact and reports
`HOME is overridden (<home> != <real home>) — skipped live launchctl load (sandboxed run)` instead
of mutating the real scheduler (rig-cli#116; applies to `tmux`, `tg_ctl`, `spotlight` too). The
post-apply verify skips its loaded-check and `rig status` suppresses the live loaded-probe (and, on
Linux, the crontab-line-missing check) under the same condition, so a sandboxed apply exits 0 and
stays converged on both the launchd and crontab platforms. Note `sudo rig apply` with a
preserved user `$HOME` counts as sandboxed too (uid 0's login home is not yours) — run rig as the
user whose launchd session it should manage.

---

## `agents_md`

Provisions one **canonical agent-guide file** in the repo, exposed under both conventional
names so every harness reads the same instructions: **`AGENTS.md`** is the real file
(Codex/most agents read it) and **`CLAUDE.md`** is a relative symlink to it (Claude Code
reads it). Default **ON** — on `rig init` AND `rig apply`, rig converges the repo to this
invariant; idempotent (a re-apply that finds it already correct is a no-op).

When enabled, apply also splices two marker-delimited blocks into the canonical
guide: an orchestrator block (`# >>> rig-managed orchestrator`) and an
autonomous-mode block (`# >>> rig-managed autonomous-mode`, naming
`Skill: autonomous-mode`). User body outside the markers is untouched;
re-apply is a no-op; opt-out is `agents_md.enabled: false`. Class:
marker-managed (see [managed-files.md](managed-files.md)).

```yaml
agents_md:
  enabled: true     # provision AGENTS.md (canonical) + CLAUDE.md (symlink). Default ON.
  # symlink: false  # equivalent opt-out (leave the repo's files untouched)
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | provision the canonical + symlink pair (set `false` to opt out) |
| `symlink` | bool | `true` | alias opt-out — `false` is equivalent to `enabled: false` |

**Canonical direction.** `AGENTS.md` is canonical by convention. A **real file always wins**:
a repo that already has a real `CLAUDE.md` and no real `AGENTS.md` keeps `CLAUDE.md` as the
source of truth and makes `AGENTS.md` the symlink — rig never demotes a real file to a link.
rig **writes a relative symlink** (just the filename) so the link it creates stays valid when
the repo moves.

**Safety-first.** rig only ever (a) creates into an **empty** slot, (b) collapses two
**identical real** files to a symlink, or (c) no-ops an already-correct pair. Every other
shape is a **conflict** that rig leaves completely untouched and surfaces via `rig status` —
it never clobbers a real file, a user-placed symlink, or a directory.

**On-disk cases (idempotent, never destructive):**

- **both absent** → create `AGENTS.md` (a minimal placeholder) + `CLAUDE.md` symlink.
- **one real, the other absent** → symlink the other → the real one.
- **one real, the other already the correct symlink** → no-op (in sync).
- **both real & identical content** → converge the link side (`CLAUDE.md`) to a symlink,
  honoring `on_conflict` (`backup` keeps a `CLAUDE.md.rig-bak-*` copy; `skip` leaves both real
  files). `rig status` reports the un-converged pair as drift.
- **conflict — left untouched, reported as drift:** both real with **different** content; a
  real file on one side with a **foreign symlink or a directory** on the other; **neither**
  slot a real file (a stray symlink/dir occupies one, e.g. an `AGENTS.md → CLAUDE.md` peer
  link). rig won't pick a winner or risk a symlink loop — reconcile to one real file (or a
  correct symlink) and re-apply.

`rig status` flags every state in which `rig apply` *would* act (missing pair, missing link,
un-converged identical files) plus the conflicts above; a correct canonical+symlink pair is in
sync.

---

## `ship_delegator`

Provisions the per-repo **`gh ship` delegator** — `.claude/scripts/pr-ship.sh` — **and the
machine-global `gh ship` gh alias that runs it** — so `gh ship <PR>` works in **this** repo on a
clean machine. Historically the delegator existed **only in agent-tools** (so `gh ship` failed in
every other managed repo) **and the `gh ship` alias itself was hand-set** (so on a fresh machine or
after a gh-config reset `gh ship` became "unknown command"); this is the durable fix — rig
provisions **both** everywhere. Default **ON** — on `rig init` AND `rig apply` rig writes/reconciles
the delegator and the alias; idempotent (a re-apply that finds them correct is a no-op).

```yaml
ship_delegator:
  enabled: true     # provision .claude/scripts/pr-ship.sh so `gh ship` works here. Default ON.
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | provision the delegator (set `false` to leave the repo untouched) |

**What the delegator does.** It's a thin shim that `exec`s the **canonical** generalized ship
implementation (agent-tools' `ci/ship/ship.sh`). Resolution order: a **repo-local**
`ci/ship/ship.sh` first (so agent-tools self-hosts and runs its own checked-out version), then
`$AGENT_TOOLS_ROOT` from the environment, then `$AGENT_TOOLS_ROOT` sourced from the
**machine-level env file** `${XDG_CONFIG_HOME:-~/.config}/agent-tools/env`, which `rig apply`
writes idempotently from the agent-tools checkout it applied from. The rendered delegator itself
is a **portable constant** — no machine-specific path is ever baked into it — so a repo may even
commit the file verbatim (agent-tools does) and a re-apply stays a byte-for-byte no-op. A clean
machine therefore gets a working `gh ship` in every repo with no alias hacks.

**The `gh ship` alias itself is rig-provisioned too (machine-global).** Alongside the per-repo
delegator, rig sets the `gh ship` **gh alias** (`gh alias set --clobber ship …`) to a **portable
dispatcher**: it runs `<repo>/.claude/scripts/pr-ship.sh` in a managed repo, else falls back to the
canonical `ci/ship/ship.sh` resolved via the same machine env file. The expansion is a **portable
constant** — no machine path baked in — so a re-apply is a byte-exact no-op. One alias serves every
repo, so it is a **global** artifact (shown in the **global** status section under `gh_ship_alias`)
even though the action rides along with the repo-scoped `ship_delegator`. When `gh` is not
installed, rig **self-skips** the alias (status and apply both no-op — no phantom "missing alias"
it could never set). Honors `on_conflict`: a **differing** existing alias is overwritten (the old
value noted) under the default `backup`/`overwrite`, or left as-is under `skip`. This supersedes the
older ci-`ship` `gh_alias` copy-path form — that flag now writes the same portable dispatcher, not a
`~/bin/ship` copy path, so the two never fight.

**`fj ship` is the same command.** On a machine with the [`fj`](#fj) block, `fj ship <PR>` runs the
identical dispatch (one function, `ship_dispatch_sh`, renders the gh alias body and the `fj`
wrapper's `ship` branch; only the exit-127 diagnostic names `fj ship`), so the two can never drift
apart.

**Repo-local shadowing is intentional (and a debugging footgun to know about).** Because the
delegator runs a repo-local `ci/ship/ship.sh` first, any repo that happens to carry one — a stale,
hand-edited, or old copy — will run THAT, not the canonical resolved via `AGENT_TOOLS_ROOT`, and
rig emits **no drift**
for it (rig reconciles the delegator's bytes, never the repo-local ship.sh it may exec). This is by
design for agent-tools' self-hosting; if `gh ship` "runs the wrong thing" in another repo, check for
a stray `ci/ship/ship.sh` in that repo.

**Worktree hygiene — the provisioned file does NOT dirty the tree.** `ship` refuses to merge from
a dirty worktree, so an un-ignored provisioned file would break the very command it enables. rig
adds `.claude/scripts/pr-ship.sh` to the repo's **`.git/info/exclude`** (the per-repo,
**never-committed** git exclude), worktree-aware: in a git worktree `info/exclude` is **per-worktree**
(it lives in that worktree's private gitdir, and `.git` is a file not a dir), so rig resolves the real
path via `git rev-parse --git-path info/exclude` rather than assuming `<repo>/.git/info/exclude`. The
entry is fenced with rig markers and
reconciled idempotently (duplicates collapse, every other line is preserved verbatim).

**Fail-closed.** If the agent-tools checkout has no `ci/ship/ship.sh`, rig provisions **nothing**
(a note explains why) rather than writing a delegator that would exec a non-existent script.

**Drift.** `rig status` flags the delegator as drift when the file is missing, its bytes differ
from the rig-generated delegator, or the file is present but **not** git-ignored (an un-ignored
delegator would dirty the worktree). `rig apply` reconciles. Shown in the **repo** section.
The **machine env file** is checked too, under its own `ship_env` category — shown in the
**global** section, since the file is a machine-wide artifact (`rig apply --only ship_env`
scopes to whichever action owns it: the `ship_delegator` action normally). A repo that carries its own `ci/ship/ship.sh`
never reads the env file, so both `status` and `apply` leave it entirely alone for that repo —
no check, no write, no backup (status/apply parity in both directions). The env-file check also
survives a **non-git** cwd: `status` there drops repo-scoped areas, but `apply` still reconciles
the machine env file, so the `ship_env` check keeps running (parity again — a missing/stale env
file surfaces even from `rig status` in `~`).

**ci-only combo (`ship_delegator: { enabled: false }` + `ci.items.ship.gh_alias: true`).** With the
delegator area disabled but a CI `ship` item still requesting the alias, there is no delegator
action to reconcile the machine env file — so the **`gh_ship_alias`** action owns it instead (it is
where the alias's out-of-repo fallback reads `AGENT_TOOLS_ROOT`; without it `gh ship` outside a
managed repo would exit 127). `rig apply` writes the env file, `rig status` checks it, and
`rig apply --only ship_env` includes that alias action (but never the option-less alias of a normal
delegator-enabled plan). Provenance points at the config layer that declares the combo — the repo
`rig.yaml`, or the global config when it is declared purely there.

---

### `rig config set` preview/commit contract

`rig config set <path> <value>` validates the prospective target layer and builds the resulting reconcile plan **without writing the config file**. Add `--commit` to write the already-validated change and execute that plan. `--no-apply` remains an explicit write-only compatibility mode. This mirrors `rig apply`: inspection is safe by default; mutation requires explicit intent.

## `linters`

Provisions this repo's **linter + formatter config files** — the same reconciled-area treatment
skills/hooks/CI/ship get, applied to tool config. You declare, per repo, **which** config file each
tool needs and the **exact bytes** it should hold; `rig init` / `rig apply` writes/repairs each
file, and `rig status` reports drift. rig hardcodes **no** specific linter — the tool, the path, and
the content are all per-repo config, so it provisions an `oxfmt` formatter, a `ruff` linter, an
`eslint`/`prettier` pair, or anything else equally. Default **ON**; opt out of the whole area with
`enabled: false`, or a single file with `items.<label>.enabled: false`.

```yaml
linters:
  enabled: true                 # provision the declared config files. Default ON.
  items:
    oxfmt-format:               # a free-form LABEL (any unique key)
      tool: oxfmt               # the tool name (informational; used in status/logs)
      role: formatter           # linter | formatter (default linter; status label only)
      path: .oxfmtrc.jsonc      # repo-relative file path (must stay inside the repo)
      content: |                # the EXACT file content rig writes/reconciles
        {
          "indentWidth": 2,
          "lineWidth": 100
        }
    ruff-lint:
      tool: ruff
      role: linter
      path: ruff.toml
      content: |
        line-length = 100
        [lint]
        select = ["E", "F", "I"]
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | provision the declared config files (set `false` to skip the whole area) |
| `items` | map | `{}` | the config files, keyed by a free-form label |
| `items.<label>.tool` | string | — | the tool name (required; informational — drives the status/log label) |
| `items.<label>.role` | enum | `linter` | `linter` or `formatter` — rendered in the apply/status label (`<role> <tool>:<item>`); both reconcile identically |
| `items.<label>.path` | string | — | repo-relative path of the config file (required; no leading `/`, `..`, or `\`) |
| `items.<label>.content` | string | — | the exact bytes rig writes/reconciles (required) |
| `items.<label>.enabled` | bool | `true` | provision this one file (set `false` to skip it) |

**Never-clobber.** A config file already on disk that **differs** from `content` is reconciled
honoring `defaults.on_conflict`: `backup` (the default) moves the existing file to a
`<name>.rig-bak-*` copy before writing rig's version, `skip` leaves the hand-edited file untouched,
`overwrite` replaces it with no backup. rig never silently clobbers a hand-written config. A file
whose bytes already match is a true no-op (no needless backup).

**Containment.** A `path` is rejected at validation **and** re-checked at apply/drift time if it is
absolute, escapes the repo with `..`, or contains a backslash (a Windows separator, ambiguous across
platforms) — a committed `rig.yaml` can only provision files **inside** its own repo.

**Line endings are normalized to LF.** rig writes the configured `content` with its line endings
collapsed to `\n`, and the drift compare uses a universal-newline read, so CRLF vs LF is **never**
spurious drift in either direction — a `\r\n`-on-disk file vs an `\n`-in-config file is equivalent
(left untouched), and a `\r\n`-in-config literal converges (rig writes an LF file rather than
rewriting it every run). rig does not churn a file solely over line endings. (A genuine content
difference *is* drift.)

**Symlinks are refused.** A symlink **anywhere** on the path — the final file or any parent
directory, pointing inside the repo or out — is reported as drift that `rig apply` errors on. rig
never writes **through** a link (that could clobber the link target or escape the repo). Resolve the
link to a real file/dir first. A non-directory **parent** (a regular file where a directory must be,
e.g. a file `config` with `path: config/x.toml`) is likewise an error, not a misleading "missing".

**Drift.** `rig status` flags an item as drift when its file is missing or its bytes differ from
the configured `content` (or a directory, a symlink-on-path, a non-directory parent, or an
unreadable / non-UTF-8 file sits at the path). `rig apply` reconciles. Shown in the **repo** section.

**Containment.** A `path` may not be absolute, contain `..`, a backslash, or a `.git` component (a
write into the git dir could rewrite repo metadata / install a hook) — rejected at validation and
re-checked at apply.

**One-way drift (a known limit).** Drift is **config→disk** only: rig flags a declared file that is
missing or wrong, but because a linter config lives at an arbitrary path (not a rig-owned directory
rig can enumerate), **removing** an item from `rig.yaml` does **not** flag the previously-provisioned
file as a disk→config `extra` — it is left in place (consistent with rig's "apply never deletes
on-disk extras" safety rule). Delete the stale config file by hand when you drop its item. Two-way
detection would need a managed manifest (a tracked follow-up).

---

## `task`

Two unrelated concerns share this one top-level block: **`code_prefix`**, which `rig apply`
DOES materialize to disk, and **task-cli's ticket-tracker routing** (`backend`/`team`/`project`/
`repo`/`attachment_mode`, plus nested `github:`/`linear:`), which rig only validates — it writes
nothing to disk for those keys.

### `code_prefix`

A repo-unique **task-code prefix**, consumed by `ci/ship/ship.sh`'s review-quorum gate. That
gate only recognizes `HYP-123`/`XX-123` style task codes out of the box — a repo whose task-cli
backend is GitHub Issues (bare `#NNN`, no ticket-code convention of its own) can never derive a
task code and is permanently refused by the gate. Setting `code_prefix` fixes that: `rig apply`
writes it into the repo's `.ship-config` as `SHIP_TASK_CODE_PREFIX=<PREFIX>`, and `ship.sh`
synthesizes `<PREFIX>-NNN` from a bare `#NNN` found in the branch name or PR body — **never**
the bare `#NNN` itself.

```yaml
task:
  code_prefix: RIG   # 1-40 uppercase letters/digits; unset = feature off
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `code_prefix` | string | *(unset)* | the repo-unique prefix spliced in front of a bare issue number (`<PREFIX>-NNN`); must be 1-40 uppercase letters/digits |

**Why a prefix, not the bare number.** review-cli's review-quorum store is a single **global**
file keyed only by the task-code string, with no per-repo scoping. Two different repos both
deriving bare `#346` (e.g. `agent-tools#346` and `rig-cli#346`) would silently share — and
falsely satisfy — each other's review-iteration count. The prefix is what keeps the synthesized
code collision-free per repo.

**Merge, not overwrite.** `rig apply` merges the `SHIP_TASK_CODE_PREFIX=` line into any existing
`.ship-config` content — a hand-committed `SHIP_LOCAL_TEST_DIR`/`SHIP_LOCAL_TEST_CMD` override is
never clobbered.

**Fail-closed on a malformed prefix.** `ship.sh` validates `SHIP_TASK_CODE_PREFIX` itself at
read time (1-40 uppercase letters/digits) and ignores the **whole** `.ship-config` file if it
doesn't match — same policy as an unsafe `SHIP_LOCAL_TEST_DIR`. `HYP-`/`XX-` style codes are
unaffected either way and still take precedence when present.

**One-way drift (a known limit, same as `linters` above).** Removing `code_prefix` from
`rig.yaml` does not remove the `SHIP_TASK_CODE_PREFIX=` line it previously wrote — `rig apply`
emits no action for an unset prefix, so the stale line is never revisited, and `rig status`
reports clean. Delete the line from `.ship-config` by hand if you turn the feature off.

### Ticket-tracker routing

**task-cli's per-repo ticket-tracker routing** (CTO decision #4136.4: hyperide → Linear/HYP,
every other repo → GitHub Issues by default). Unlike `code_prefix` above, rig itself **writes
nothing to disk** for these keys — task-cli reads this block **directly** at runtime
(`tasklib.config.rig_task_overlay`, in the [task-cli
repo](https://github.com/alex-mextner/task-cli)), with LOWER precedence than a repo's own
native `task.yaml`. A repo that already keeps a `task.yaml` is unaffected by anything set here.
rig only validates the key set + enum values so a typo fails fast at `rig status`/`rig apply`
time, with rig's own error format, instead of only surfacing later as a task-cli error.

```yaml
task:
  backend: linear          # github-issues (default) | linear
  team: HYP                # flat shorthand for linear.team
  attachment_mode: link    # link (never re-host a local file) | native (default; real upload)
  # equivalently, nested form (both may be combined; task-cli rejects a genuine collision):
  # linear:
  #   team: HYP
  #   project: ""           # optional Linear project id to scope create/list/search to
  #   attachment_mode: link
  # github:
  #   repo: owner/name       # or 'auto' to detect from the git remote
```

All keys below (flat and nested, scalar and container) tolerate an explicit `null` the same as
absent — rig's `_validate_task` guards every one of them with `is not None`, an early `None`
return (the three enum leaves: `backend`, `attachment_mode`, `linear.attachment_mode`), or a
null-specific `{} if x is None else x` normalization (the two nested sections; not a plain
`or {}`, which would also swallow other falsy-but-invalid values), matching task-cli's own
tolerant reads. The one exception is the top-level `task:` header itself, which stays rejected
(matches every other
block's convention).

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `backend` | str or null | `github-issues` | the tracker backend: `github-issues` or `linear` |
| `team` | str or null | — | flat shorthand for `linear.team` (e.g. `HYP`) |
| `project` | str or null | — | flat shorthand for `linear.project` |
| `repo` | str or null | — | flat shorthand for `github.repo` (`owner/name`, or `auto`) |
| `attachment_mode` | str or null | `native` (task-cli's own default) | how a screenshot/file gets attached to a Linear ticket — see below |
| `github.repo` | str or null | — | nested form of the `repo` shorthand |
| `linear.team` | str or null | — | nested form of the `team` shorthand |
| `linear.project` | str or null | — | nested form of the `project` shorthand |
| `linear.attachment_mode` | str or null | — | nested form of the `attachment_mode` shorthand |
| `github`, `linear` | object or null | — | nested sections; an explicit `null` (a bare `github:`/`linear:` header) is tolerated the same as absent |
| `projects` | array or null | — | task-cli's cross-project registry (a LIST of entries, not a mapping — see task-cli's `tasklib/projects.py`); rig only checks it's a list; an explicit `null` is tolerated the same as absent |
| `enforce`, `classify`, `session` | object or null | — | task-cli's own nested shapes (each with their own defaults); rig only checks they're mappings — task-cli's own `tasklib.config.validate` is the runtime authority on their contents; an explicit `null` is tolerated the same as absent |

**`attachment_mode` — `link` vs `native` (tg#11652).** How task-cli attaches a screenshot/file
to a Linear ticket:
- **`native`** (task-cli's own default): uploads the bytes through Linear's authenticated
  `fileUpload` + signed-URL PUT + `attachmentCreate` API sequence, so the ticket carries a real
  Linear attachment object with an inline preview.
- **`link`**: never uploads a local file — only registers an already-hosted URL (e.g. a GitHub
  PR asset, or a PR's `#screenshots` anchor) as an external-link attachment. hyperide pins this
  mode (HYP-1248): a raw pasted-image `uploads.linear.app` URL is session-scoped and 401s for a
  non-browser viewer, so hyperide's mitigation is to link out to the PR instead of embedding an
  image. A bare local path in this mode has nothing to link to and the attach fails cleanly
  (best-effort — the local ref stays visible in the ticket body regardless).

---

## `project_tools`

Provisions repo-local carriers for the project-intelligence tools used by the agent ecosystem:
Haft, Serena, and Sverklo. This is a **repo** block, distinct from the global `tools:` block:
`tools:` installs personal CLIs into a machine PATH, while `project_tools:` writes committed
project files and safe live registrations so this repository is usable by those CLIs.

```yaml
project_tools:
  enabled: true
  haft:
    enabled: true
    codex_mcp: true
    # project_name defaults to the repo directory name.
    # project_id defaults to a stable qnt_<hash> derived from project_name.
    workflow:
      mode: standard
      require_decision: true
      require_verify: true
      allow_autonomy: false
  serena:
    enabled: true
    # languages auto-detects from repo files when omitted.
    read_only: false
    ignored_paths: []
  sverklo:
    enabled: true
    register: true
    reindex: false
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | provision project-tool integrations (set `false` to skip the whole area) |
| `haft.enabled` | bool | `true` | write the `.haft/` project/spec/workflow carriers |
| `haft.project_name` | string | repo dir name | Haft project name |
| `haft.project_id` | string | `qnt_<hash>` | stable Haft project id |
| `haft.codex_mcp` | bool | `true` | merge the Haft MCP server into `.codex/config.toml` |
| `haft.workflow.mode` | enum | `standard` | `standard` or `tactical` |
| `haft.workflow.require_decision` | bool | `true` | require explicit decisions for high-impact work |
| `haft.workflow.require_verify` | bool | `true` | require verification evidence before completion |
| `haft.workflow.allow_autonomy` | bool | `false` | allow autonomous Haft execution by default |
| `serena.enabled` | bool | `true` | write `.serena/project.yml` and `.serena/.gitignore` |
| `serena.project_name` | string | repo dir name | Serena project name |
| `serena.languages` | list[str] | auto | Serena language ids; auto-detected from repo files when omitted |
| `serena.read_only` | bool | `false` | disable Serena editing tools for this project |
| `serena.ignored_paths` | list[str] | `[]` | extra Serena ignore patterns |
| `sverklo.enabled` | bool | `true` | provision Sverklo integration |
| `sverklo.register` | bool | `true` | register the repo in the global Sverklo registry during `rig apply` |
| `sverklo.reindex` | bool | `false` | run `sverklo reindex` during `rig apply` |

**Haft.** rig writes `.haft/project.yaml`, `.haft/workflow.md`, parseable placeholder spec
carriers under `.haft/specs/`, and empty tool-owned state directories via `.gitkeep` files. It
does not scan or delete tool-owned data such as `.haft/evidence` contents. When `codex_mcp` is
enabled, rig merges a managed Haft MCP section into `.codex/config.toml` with `cwd` and
`HAFT_PROJECT_ROOT` set to the absolute repo path (a relative `"."` makes `haft serve`
return `tools: null`, which Codex rejects as "Unexpected response type"); unrelated Codex
config is preserved, and an existing unmarked `[mcp_servers.haft]` table is replaced by the
managed block.

**Serena.** rig writes `.serena/project.yml` plus `.serena/.gitignore` for Serena-local cache and
machine-local overrides. `languages` is optional; when omitted, rig infers common language ids from
repo files such as `pyproject.toml`, `package.json`, `go.mod`, `Cargo.toml`, and `Package.swift`.
Serena memories remain tool-owned and are not enumerated as drift.

**Sverklo.** `register` is idempotent: apply checks `sverklo list` first and skips if the repo is
already registered. `reindex` is off by default because indexing can be slow and the registry uses
a shared database. Live Sverklo commands are gated in tests/CI by `RIG_PROJECT_TOOLS_DRY_RUN=1` or
`RIG_SVERKLO_DRY_RUN=1`.

**Drift.** `rig status` flags a declared Haft/Serena/Codex carrier as missing or modified when the
file is absent or differs from the rendered config. Sverklo registration drift is reported when
`sverklo list` does not include this repo (or the CLI is unavailable). `sverklo reindex` is
apply-only maintenance and has no cheap drift read-back.

---

## `ecosystem_readme`

Writes/updates a marker-delimited `## Ecosystem` section in this repo's README from ONE
canonical, in-code tool registry (`riglib/ecosystem_readme.py`), instead of every sibling repo
hand-copying and letting the list silently drift (rig-cli#308). This is a **repo** block, default
**OFF** (opt-in) — a fresh `rig apply` never rewrites a repo's README unprompted. Enable it via
`rig setup` (which shows/edits it like every other reconciled area) or `rig config set
ecosystem_readme.enabled true`.

```yaml
ecosystem_readme:
  enabled: true
  target: README.md  # repo-relative; defaults to README.md
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | provision the README Ecosystem block (opt-in) |
| `target` | string | `README.md` | repo-relative README path to reconcile |

**How it reconciles.** The block is delimited by `<!-- rig:ecosystem-block -->` /
`<!-- /rig:ecosystem-block -->` HTML comments (invisible in a rendered Markdown viewer) and
spliced in using the SAME marker-offset machinery the codex execpolicy `.rules` block uses: a
target missing the markers gets the block APPENDED (creating the README if it does not exist
yet); a target whose managed block differs from the canonical rendering gets it REWRITTEN
in place; a target already matching is a byte-identical no-op. Everything OUTSIDE the markers —
the rest of your README — is preserved verbatim. An unbalanced or misordered marker pair is a
hard error rig refuses to guess at (fix it by hand, then re-apply). A rewrite honors
`defaults.on_conflict` like every other managed block: `backup` (default) keeps a timestamped
copy before rewriting; `skip` leaves a stale block untouched (drift stays visible in `rig
status`) but still performs the additive append when no managed block exists yet.

**Migrating an existing hand-copied `## Ecosystem` section.** If the target already has a bare
`## Ecosystem` heading with no rig markers — the primary case this feature exists for — `rig
apply` refuses to append a second, misplaced heading rather than guess: wrap the existing section
in `<!-- rig:ecosystem-block -->` / `<!-- /rig:ecosystem-block -->` by hand (or delete it) first,
then re-apply.

**Drift.** `rig status` flags the block as missing when the target file (or a managed block
inside it) is absent, and modified when the on-disk block differs from the canonical rendering
(including an unbalanced/misordered marker pair or an unmarked pre-existing `## Ecosystem`
heading, both of which need a hand fix).

---

## `github`

Provisions a **GitHub repository branch ruleset** — the modern replacement for branch
protection — on the repo's **default branch**, declaratively and reconciled like every other
category. rig owns the ruleset named `ruleset.name` (default `rig-managed`) and converges it
via `gh api`. Default **ON**: on `rig init` AND `rig apply` rig creates the ruleset if absent,
updates it if it drifted from config, and no-ops if it already matches. A repo with **no
github remote is a no-op** (the action reports `skipped`, never an error) — so "default ON when
the repo has a github remote" needs no extra flag.

```yaml
github:
  ruleset:
    enabled: true                 # provision the ruleset (default ON; false opts out)
    name: rig-managed             # rig owns rulesets with this name
    require_pull_request: true    # pull_request rule
    required_reviews: 0           # required_approving_review_count
    required_conversation_resolution: true  # block merge while a review thread is open
    dismiss_stale_reviews: true   # drop a stale approval on a new push
    block_force_push: true        # non_fast_forward rule
    restrict_deletion: true       # deletion rule
    require_linear_history: false # required_linear_history rule
    require_signatures: false     # required_signatures rule
    # required_status_checks: omit to auto-default to the merge-gating CI gates this repo
    # provisions (PR Checklist + review-threads); set a list to pin, or [] to require none.
    admin_bypass: true            # add the repo Admin role to bypass_actors
    # require_extra_approval_for_unattributed_changes: omit — see the mode-gated paragraph below.
    # Only set it explicitly to override rig's mode-gated default in either direction.
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | provision the ruleset (set `false` to leave the repo's rulesets untouched) |
| `name` | str | `rig-managed` | the ruleset rig owns/reconciles (rig only ever touches a ruleset with this name) |
| `require_pull_request` | bool | `true` | emit the `pull_request` rule (require a PR to merge to the default branch) |
| `required_reviews` | int ≥ 0 | `0` | `required_approving_review_count` on the `pull_request` rule |
| `required_conversation_resolution` | bool | `true` | `required_review_thread_resolution` on the `pull_request` rule — the SERVER blocks any merge while any review thread is unresolved (the open-thread durable fix). Does not require a reviewer or an approval, but does block the merge until all threads are resolved. **Inert if `require_pull_request: false`** (the whole `pull_request` rule is then not emitted) |
| `dismiss_stale_reviews` | bool | `true` | `dismiss_stale_reviews_on_push` on the `pull_request` rule — dismisses any approval (including voluntary ones) when a new commit is pushed. **Inert if `require_pull_request: false`** |
| `block_force_push` | bool | `true` | emit the `non_fast_forward` rule (block force-push) |
| `restrict_deletion` | bool | `true` | emit the `deletion` rule (block deleting the branch) |
| `require_linear_history` | bool | `false` | emit the `required_linear_history` rule |
| `require_signatures` | bool | `false` | emit the `required_signatures` rule |
| `required_status_checks` | list[str] | *auto* | contexts for the `required_status_checks` rule. **When omitted**, rig defaults it (ROADMAP §5) to the merge-gating CI gates this repo actually provisions — `PR Checklist` and `review-threads` — so a PR can't merge until those are green, but ONLY for gates that are enabled and written (requiring an absent check would wedge every PR). An explicit list (including `[]` for "require none") wins verbatim. **An empty result emits no rule** (never a no-op rule) |
| `admin_bypass` | bool | `true` | add the repo Admin role (`actor_id: 5`, `RepositoryRole`, `always`) to `bypass_actors` |
| `require_extra_approval_for_unattributed_changes` | bool | *mode-gated, see below* | `require_extra_approval_for_unattributed_changes` on the `pull_request` rule — GitHub's own rule requiring one extra approval on a PR opened under an unattributed app identity (documented example: Copilot). **Has no unconditional default** — unlike every other knob above, rig only injects a default when the repo's effective `mode.name` is `autonomous`. Set explicitly to override the mode-gated default in either direction |

**`require_extra_approval_for_unattributed_changes` — mode-gated, autonomous machines only.**
GitHub's own PR rule of this name requires ONE MORE approval than `required_reviews` on a PR
GitHub classifies as opened under an app identity not attributed to a person — GitHub's own docs
name the Copilot coding agent as the documented trigger: *"When Copilot opens a pull request that
isn't attributed to a person, the ruleset requires one more approval than the number you
configured"* (docs.github.com, available-rules-for-rulesets). This was found **ON** (`true`)
blocking `gh ship`'s merge on agent-tools' `rig-managed` ruleset and hand-patched to `false` via a
raw `gh api ... -X PUT` on 2026-09-07 as an emergency stopgap — this feature makes that fix durable
and reconcilable. **The exact causal mechanism was not independently confirmed**: agent-tools'
recent merges are all attributed to a human GitHub login (`alex-mextner`), not an app/bot identity,
which the documented Copilot-specific trigger would not gate on, and the repo's rule-suites
evaluation log for the last 30 days shows zero failed pushes — so it is unclear from the evidence
gathered here exactly why the rule fired for `gh ship`. The observed block and the working
hand-patch are the facts this feature is built on; the mechanism claim above is GitHub's own
documented behavior, not a verified explanation of the specific incident. rig now reconciles the
knob declaratively: it defaults it to `false` (never required)
**only** for a repo whose effective `mode.name` is `autonomous` (the machine-wide `mode:` block —
see the `mode` section above; it cascades from `~/.config/rig/config.yaml`, since `mode` is a
GLOBAL-only category rejected from a committed per-repo `rig.yaml`). **This makes the gating
per-machine, not per-repo**: on a machine with `mode.name: autonomous` set, every rig-managed repo
`rig apply` touches gets this knob managed. A repo that is NOT in autonomous mode (or applied from
a non-autonomous machine) gets no injected default: the knob is absent from both the outgoing
desired body and the drift comparison — it is simply **unmanaged**. An unrelated ruleset knob
change still PUTs the full `pull_request` rule (GitHub's update API replaces the whole parameters
object), which would otherwise silently reset an unmanaged knob to GitHub's own platform default —
so at apply time rig re-reads the live value it already fetched for drift classification and
carries it, UNCHANGED, into the outgoing PUT (`github_ruleset.carry_over_unattributed_approval`).
This is NOT full optimistic concurrency: there is a narrow window between that read and the PUT
where a concurrent hand edit in the GitHub UI could still be overwritten by the carried-over
snapshot — the same read-then-write class of risk this module already carries for every other
field it reconciles, not one this knob introduces new. To opt a single repo back in — e.g. to keep
formal human approval required even on an autonomous machine — set
`github.ruleset.require_extra_approval_for_unattributed_changes` explicitly in that repo's own
`rig.yaml`; an explicit value always wins over the mode-gated default, in either direction and
regardless of mode.

**The footgun guard — rig never emits the `update` rule.** A hand-made ruleset with the
`update` ("Restrict updates") rule and **zero bypass actors** locks out *every* merge: each
merge is an "update" to the protected default branch, so GitHub answers `Cannot update this
protected ref` and only a repo admin using `--admin` can push past it. rig's rule assembly
**cannot emit the `update` rule** — there is no config knob and no code path that produces it.
It likewise never emits a `required_deployments` rule with an empty environment list (a no-op
smell that can also block). And when `admin_bypass` is on (the default) the repo Admin role is
a bypass actor, so an active ruleset never locks admins out of merging.

**The default ruleset rig emits:** `pull_request` (0 required reviews) + `non_fast_forward` +
`deletion`, with the Admin role in `bypass_actors`. Linear history, signatures, and required
status checks are off/empty. Targets the default branch via the `~DEFAULT_BRANCH` ref token, so
the ruleset follows a rename of the default branch.

**Reconcile + idempotency.** `github_ruleset_state` is the single classification `rig apply`
and `rig status` share (so they can never disagree): `create` (no managed ruleset → POST),
`update` (managed ruleset differs → PUT), `ok` (matches → no-op), `no_remote` (no github origin
→ no-op, no drift). The desired-vs-actual comparison normalizes both sides (sorted rules,
sorted check contexts, identity-only bypass actors, order-independent conditions, and EVERY
managed rule parameter) so a semantic match reads as in sync, not churn. The list call uses
`includes_parents=false` + `--paginate` so an inherited org ruleset is never mistaken for the
repo's, and a managed ruleset on a later page is found rather than duplicated.

**When rig can't reach GitHub (`gh` missing / not authed / API error).** `rig apply` returns an
`error` on the action (it tried to act and couldn't). `rig status` does NOT report the repo as
"in sync" — it surfaces a visible *could-not-verify* drift item, because a green "in sync" while
rig was unable to check would mask a genuinely missing/drifted ruleset. (A repo with **no github
remote** is different: that is a clean no-op, no item.) GitHub Enterprise hosts other than
`github.com` are treated as no-remote (skipped) — only `github.com` rulesets are managed today.

Set `RIG_GH_DRY_RUN=1` to compute what *would* change (the create/update is reported) but make
**no `gh` POST/PUT** — for CI, smoke, or a dry inspection where mutating a real repo is unwanted.

### `github.merge` — the repo merge-button policy (squash-only)

Reconciles the repo's **merge-button policy** via `PATCH /repos/{owner}/{repo}`: squash is the
only merge model (`merge_commit`/`rebase_merge` off → linear, one commit per PR), the head branch
is auto-deleted on merge, and auto-merge is allowed so a PR lands the instant its gate goes green.
These are repo **settings**, not a ruleset, so a mis-set value at worst disables a button — it can
never lock anyone out of merging. Default **ON**; opt out with `merge.enabled: false`.

```yaml
github:
  merge:
    enabled: true                 # provision the merge policy (default ON)
    squash_merge: true            # allow_squash_merge — the only model by default
    merge_commit: false           # allow_merge_commit
    rebase_merge: false           # allow_rebase_merge
    delete_branch_on_merge: true  # auto-delete the head branch on merge
    allow_auto_merge: true        # a PR auto-merges when its required checks pass
    allow_update_branch: true     # offer the "Update branch" button
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | provision the merge policy |
| `squash_merge` | bool | `true` | `allow_squash_merge` |
| `merge_commit` | bool | `false` | `allow_merge_commit` |
| `rebase_merge` | bool | `false` | `allow_rebase_merge` |
| `delete_branch_on_merge` | bool | `true` | auto-delete the head branch on merge |
| `allow_auto_merge` | bool | `true` | allow a PR to auto-merge when green |
| `allow_update_branch` | bool | `true` | offer the "Update branch" button |

### `github.ghas` — GitHub Advanced Security

Reconciles the repo's supply-chain and secret-leak guards: **dependency graph** + **secret
scanning** (+ push protection) via the repo's `security_and_analysis` block (`PATCH /repos`),
**vulnerability alerts** + **Dependabot security updates** via their own `PUT/DELETE`
sub-resources, and **CodeQL default-setup** via its own endpoint. Default **ON** (secure defaults).
On a **private repo whose plan does not include GHAS** the licensed scanners return 403/422 — the
action degrades **loudly** (a visible "could not enable — plan does not include GHAS" in the
result), it does NOT crash and does NOT silently report green; the free features (dep-graph,
vuln-alerts, Dependabot) are applied independently so one unlicensed scanner never masks them.

```yaml
github:
  ghas:
    enabled: true
    vulnerability_alerts: true
    automated_security_fixes: true
    secret_scanning: true
    secret_scanning_push_protection: true
    code_scanning_default_setup: true
```

(There is no `dependency_graph` knob: on github.com cloud the dependency graph is not a
separately-togglable repo setting via the API — it is always-on for public repos and is governed by
the vuln-alerts / Dependabot sub-resources rig manages, so a standalone knob would have no effect.)

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | provision GHAS settings |
| `vulnerability_alerts` | bool | `true` | the `vulnerability-alerts` sub-resource (Dependabot alerts) |
| `automated_security_fixes` | bool | `true` | Dependabot security updates (`automated-security-fixes`) |
| `secret_scanning` | bool | `true` | `security_and_analysis.secret_scanning` |
| `secret_scanning_push_protection` | bool | `true` | secret-scanning push protection |
| `code_scanning_default_setup` | bool | `true` | CodeQL default-setup (`configured`) |

### `github.actions` — GitHub Actions permissions

Reconciles the repo's Actions permissions on two endpoints: `PUT .../actions/permissions`
(whether Actions runs + which actions are allowed) and `PUT .../actions/permissions/workflow`
(the default `GITHUB_TOKEN` scope + whether workflows may approve PRs). Secure defaults: Actions
**enabled** (don't silently break CI), but the token is **READ-only** (least privilege — a
workflow that needs write declares it explicitly) and workflows may **not** approve PRs.

```yaml
github:
  actions:
    enabled: true
    actions_enabled: true
    allowed_actions: all          # all | local_only | selected
    default_workflow_permissions: read   # read | write
    can_approve_pull_request_reviews: false
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | provision Actions permissions |
| `actions_enabled` | bool | `true` | whether Actions runs at all |
| `allowed_actions` | enum | `all` | `all` / `local_only` / `selected` |
| `default_workflow_permissions` | enum | `read` | default `GITHUB_TOKEN` scope: `read` / `write` |
| `can_approve_pull_request_reviews` | bool | `false` | may a workflow approve/create PRs |

### `github.browser` — settings the REST API does NOT expose (agent-browser backend)

A first-class **second backend** for the handful of repo settings GitHub has never shipped an API
for. rig drives the GitHub **settings UI** headlessly with `agent-browser` (accessibility-role
selectors, not brittle CSS), invoked **inside** `rig apply` — not a manual "go click this" step.
It is **planned** by default (so `rig status` lists it) but **gated off at apply** unless
`RIG_GH_BROWSER=1` — driving a real browser is heavier and slower than `gh api`, so it runs only
when explicitly enabled. If the toggle is absent from the page (org policy hid it / GitHub moved
it) the action degrades loudly, never a blind click.

```yaml
github:
  browser:
    enabled: true                 # plan the backend (status lists it); apply gated by RIG_GH_BROWSER=1
    discussions: false            # the Discussions UI-only toggle
    projects: true                # the Projects UI-only toggle
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | plan the agent-browser backend (apply still needs `RIG_GH_BROWSER=1`) |
| `discussions` | bool | `false` | the Discussions UI-only toggle |
| `projects` | bool | `true` | the Projects UI-only toggle |

### The auth gate (CTO #4136.1 — ASK and WAIT, never silently fail)

Every `github.*` mutation passes a shared **auth gate** before it touches a live setting. If `gh`
is **not authenticated** for the admin scope it needs (or, for the browser backend, `agent-browser`
is unavailable), rig does **not** silently fail: it **notifies you via `tg`** with the exact
`gh auth login` command and **blocks/waits** for auth to appear, then **resumes** the apply.

- `RIG_GH_AUTH_WAIT` — max **seconds** to block waiting for login. **Unset/`0` (the default)** →
  do not block: probe once and, if still unauthenticated, degrade loudly (so an **unattended**
  `rig apply` in CI never hangs forever on a human). A positive value (e.g. `1800` = 30 min) opts
  into the interactive "ask and wait": rig notifies once, then polls until you log in or the budget
  elapses.
- `RIG_GH_AUTH_POLL` — seconds between re-probes while blocking (default `5`).
- Under `RIG_GH_DRY_RUN=1` the auth **gate is skipped** (dry-run performs no `gh` POST/PUT, so it
  never blocks on a login prompt — this is why CI and the test suite don't hang). Note the dry-run
  still performs the read-only `gh api` GET to classify live-vs-desired state, so on a repo with a
  github remote a dry-run with **no token at all** reports a read error rather than a preview; an
  offline/no-auth preview is only fully no-op on a repo with no github remote (skipped). In CI the
  test suite monkeypatches the `gh` seam, so neither the gate nor the read ever runs for real.

---

## `tmux`

rig **MANAGES tmux configuration declaratively** from this block — generating the config from
`rig.yaml` and **MIGRATING** an existing hand-written `~/.tmux.conf` instead of clobbering it.
Because rig *generates* the managed region it can **GUARANTEE plugin-init ordering** — the
root-cause fix for a stale-session-on-reboot bug (below). **Opt-in:** a `tmux:` block with
`enabled` not `false`; an absent block leaves tmux alone.

```yaml
tmux:
  enabled: true                 # provision the rig-managed tmux config (opt-in)
  apply: import                 # import (preferred) | block (sentinel-fenced fallback)
  conf_path: ~/.tmux.conf       # the file rig migrates/wires
  generated_dir: ~/.config/rig/tmux   # where rig writes rig.tmux.conf + the managed scripts
  resurrect:
    # do NOT list `claude` here while cc_restore is on — cc-restore owns the exact resume
    # (see "Why claude is NOT in the default @resurrect-processes" below).
    processes: [ssh, psql, mysql, sqlite3]
    capture_pane_contents: true
  continuum:
    restore: true               # @continuum-restore on
    save_interval: 15           # @continuum-save-interval (minutes)
    boot: true                  # NO-OP: rig's launchd agent owns boot (see boot.enabled); rig
                                # never emits @continuum-boot 'on' (it would install continuum's
                                # own untracked iTerm Tmux.Start.plist — the path rig replaces)
  moshi:
    enabled: false              # opt-in Moshi (iOS client) status-line tweaks — see below
  cc_restore:
    enabled: true               # per-window Claude Code resume by exact session id
  omp_restore:
    enabled: true               # per-window omp (oh-my-pi) resume by exact session file
  codex_restore:
    enabled: true               # per-window Codex CLI resume by exact session id
  anti_sprawl:
    enabled: true               # install an attach-or-create entry (one canonical session)
    session: main               # the canonical session name
  boot:
    enabled: true               # a launchd agent that brings tmux up after a macOS reboot
  autosave:
    enabled: true               # an INDEPENDENT launchd saver (decoupled from continuum's
                                # status-right hook); when on, continuum's own autosave is
                                # disabled (@continuum-save-interval 0) — one authoritative saver
    label: ai.hyperide.tmux-autosave
    stale_after: 45             # minutes: the saver logs a WARN + records it in the health file
                                # when the newest snapshot is older than this after a run
  login_shell:
    enabled: true               # restored panes are LOGIN shells (so ~/.zprofile/PATH is sourced)
    shell: ""                   # "" → resolve the user's $SHELL at apply; else an absolute path
  pane_titles:
    enabled: true               # pane-border-status: a compact per-pane title
    position: top                # "top" or "bottom"
    format: "#{session_name} #{window_index}:#{window_name}#{window_flags}"
    clear_status_right: true    # separately clear tmux's default clock+date status-right
  focus_events:
    enabled: true                # set -g focus-events on — report terminal focus in/out
                                  # events to programs running inside panes (some editors/
                                  # tools need this). A plain terminal-capability toggle, no
                                  # interaction with the resurrect/continuum/tpm ordering.
  clipboard:
    enabled: true                # copy-mode clipboard: pipe a copy-mode selection (keyboard
                                  # `prefix [`, `v`, `Enter`/`y`, or a mouse-drag release)
                                  # through a resolved system-clipboard command
    command: ""                  # "" → auto-detect (macOS pbcopy; Linux wl-copy/xclip/xsel);
                                  # a non-empty override is used verbatim (shell-quoted safety
                                  # checked, same class as pane_titles.format)
  copy_mode:
    mode_keys: vi                 # "vi" (default) emits `setw -g mode-keys vi`, independent of
                                  # `clipboard` above (tmux's own default is emacs, under which
                                  # `Space` pages instead of selecting and `v` is unbound);
                                  # "emacs" keeps tmux's own default table live
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | — (opt-in) | provision the rig-managed tmux config (`false`/absent → leave tmux alone) |
| `apply` | enum | `import` | apply mechanism: `import` (preferred) or `block` (managed-block fallback) |
| `conf_path` | path | `~/.tmux.conf` | the user's tmux config rig migrates and wires |
| `generated_dir` | path | `~/.config/rig/tmux` | where rig writes `rig.tmux.conf` + cc-save/cc-restore/omp-save/omp-restore/codex-save/codex-restore/tmux-attach scripts |
| `resurrect.processes` | list[str] | `[ssh, psql, mysql, sqlite3]` | `@resurrect-processes`. `claude`/`omp`/`codex` are each **omitted by default while their own `*_restore` is on** (the matching `*-restore` script owns the exact resume); rig adds a name back only when that harness's restore is off (fallback). List it explicitly to force it. |
| `resurrect.capture_pane_contents` | bool | `true` | `@resurrect-capture-pane-contents on` |
| `continuum.restore` | bool | `true` | `@continuum-restore on` |
| `continuum.save_interval` | int ≥ 1 | `15` | `@continuum-save-interval` (minutes) |
| `continuum.boot` | bool | `true` | **no-op** — rig always emits `@continuum-boot 'off'` (its own launchd agent owns boot; see `boot.enabled`). Kept for forward-compat. |
| `moshi.enabled` | bool | `false` | opt-in Moshi status-line tweaks, emitted **before** continuum init |
| `cc_restore.enabled` | bool | `true` | wire cc-save/cc-restore via resurrect post-save/post-restore hooks |
| `omp_restore.enabled` | bool | `true` | wire omp-save/omp-restore via resurrect post-save/post-restore hooks |
| `codex_restore.enabled` | bool | `true` | wire codex-save/codex-restore via resurrect post-save/post-restore hooks |
| `anti_sprawl.enabled` | bool | `true` | install the attach-or-create entry script |
| `anti_sprawl.session` | str | `main` | the one canonical session name |
| `boot.enabled` | bool | `true` | write a launchd agent (macOS) that runs the boot script after a reboot, and `launchctl load -w` it on apply |
| `boot.label` | str | `ai.hyperide.tmux-boot` | the launchd agent label (and plist filename stem) |
| `autosave.enabled` | bool | `true` | provision an **independent** launchd saver (`StartInterval` = `continuum.save_interval` minutes) that calls resurrect `save.sh` directly — decoupled from continuum's fragile status-right hook. When on, rig emits `@continuum-save-interval '0'` so there is exactly **one** authoritative saver (no racing writers over the `last` symlink). The wrapper guards against no-server, empty servers, and a **degenerate save** (a bare boot-time `main` never clobbers a richer prior snapshot), and writes a health-state file + log line so a silent month-long death is impossible. |
| `autosave.label` | str | `ai.hyperide.tmux-autosave` | the autosave launchd agent label (and plist filename stem) |
| `autosave.stale_after` | int ≥ 1 | `45` | minutes: the freshness threshold. After each run the saver logs a `WARN` in the health file when the newest snapshot is older than this; **`rig status` reads that health record and surfaces a `tmux autosave agent: STALE …` line** when the agent's last run is older than `stale_after` (i.e. the saver stopped firing) — the observability that turns a silent month-long death into a visible one within one interval. The line is advisory (it does not change the status exit code) and only appears when the agent's launchd plist is actually installed. |
| `pane_titles.enabled` | bool | `true` | provision `pane-border-status`/`pane-border-format` (a compact per-pane title). `false` emits neither, makes `position`/`format` moot, and disables `clear_status_right` too (it is nested under `enabled`, not independent of it). **Behavior change on upgrade**: with this on (the default whenever `tmux.enabled` is also on), rig now clears tmux's default clock+date `status-right` for everyone — see `clear_status_right` below for the opt-out. |
| `pane_titles.position` | enum | `top` | `pane-border-status` placement: `top` or `bottom`. An invalid stored value (e.g. from a replayed plan that bypassed validation) is defensively clamped back to `top` at render time rather than emitting a `set` tmux would reject. |
| `pane_titles.format` | str | `"#{session_name} #{window_index}:#{window_name}#{window_flags}"` | `pane-border-format` value. The default carries **no date/time token** by design. Must **not** contain `"`, `\`, `$`, `#(` (tmux's shell-exec format token — tmux runs it through the shell on every render, the actual code-execution vector), or a non-printable character other than a plain tab — those would corrupt, inject into, or execute code from the generated tmux config; rejected at validate time (and in the published JSON schema), and defensively substituted back to the default if an unsafe value ever reaches the renderer some other way. |
| `pane_titles.clear_status_right` | bool | `true` | when `pane_titles.enabled` is also true, additionally clear tmux's built-in `status-right` default — which is otherwise a clock+date (`%H:%M %d-%b-%y`), wasted space once the pane title carries the session/window context. A SEPARATE toggle from `enabled` (not independent of it): set this `false` to keep the border title while preserving an existing custom `status-right`. |
| `login_shell.enabled` | bool | `true` | set a **login-shell** `default-command` so restored panes source `~/.zprofile`/PATH (resurrect otherwise restores a non-login shell with a broken env) |
| `login_shell.shell` | str | `""` | login shell path. `""` resolves the user's `$SHELL` at apply (falling back to `/bin/zsh` then `/bin/sh`); a non-empty override **must be an absolute path** to the shell binary (a relative name or a command-with-args is rejected, so it can't silently produce a broken `default-command`) and is used verbatim. The path is **baked at generation** — NOT a tmux `${SHELL}` reference, because tmux rejects `${VAR:-default}` and would abort the whole config |
| `focus_events.enabled` | bool | `true` | `set -g focus-events` on/off — tmux reports terminal focus in/out events to programs running inside panes (needed by editors/tools that react to focus). A plain capability toggle: it carries none of the resurrect/continuum/tpm ordering constraints below (it's placed first in the generated file purely for readability, not because position is load-bearing here). Like every other modeled boolean, `false` emits an explicit `set -g focus-events off` rather than omitting the line. |
| `clipboard.enabled` | bool | `true` | provision the copy-mode clipboard key bindings: `v` (begin-selection), `y`/`Enter`/`MouseDragEnd1Pane` (copy-pipe-and-cancel through the resolved clipboard command) in the `copy-mode-vi` table, plus `Enter`/`MouseDragEnd1Pane` mirrored into the plain `copy-mode` (emacs) table too. See `copy_mode.mode_keys` below for which table is actually LIVE (an independent knob). `false` emits none of them. When no clipboard command can be resolved on this platform (no `pbcopy`/`wl-copy`/`xclip`/`xsel` on PATH and no `command` override), rig emits NO clipboard section at all — never a binding that shells out to a missing command — and surfaces a note in the `rig apply`/`rig status` detail line instead. |
| `clipboard.command` | str | `""` | explicit clipboard-write command override. `""` (default) auto-detects: macOS → `pbcopy`; Linux → `wl-copy` (only when `$WAYLAND_DISPLAY` is set AND the binary is on PATH), else `xclip -selection clipboard -in`, else `xsel --clipboard --input`, else unresolved (see `clipboard.enabled` above). A non-empty override is used verbatim and must **not** contain `"`, `\`, `$`, `#(` (a shell-exec token), or a non-printable character other than a plain tab — the same injection-safety class as `pane_titles.format` (both interpolate into a tmux double-quoted option value); rejected at validate time (and in the published JSON schema), and defensively substituted back to the platform-detected command if an unsafe value ever reaches the renderer some other way. |
| `copy_mode.mode_keys` | enum | `vi` | `setw -g mode-keys` — the copy-mode key table. tmux's OWN default is `emacs`, under which `Space` is bound to `page-down` (not `begin-selection`) and a bare `v` is unbound — so a vim-style copy-mode workflow (`prefix [` then `v`/`Space` to select, `y` to yank) silently does nothing. Rendered **unconditionally**, independent of the `clipboard` block above (Alex tg#325: disabling/losing clipboard integration must not also lose plain vi-style copy-mode navigation). An invalid stored value is defensively clamped back to `vi` at render time, same pattern as `pane_titles.position`. |

**Apply mechanism — import-preferred, managed-block fallback.**

- **`import` (preferred).** rig owns `~/.config/rig/tmux/rig.tmux.conf` (built wholesale from
  this block on every apply, idempotent) and `~/.tmux.conf` carries a **single `source-file`
  import line** appended after the user's own lines. rig rewrites only its own file and the one
  import line — every hand-written line in `~/.tmux.conf` is left untouched.
- **`block` (fallback).** rig splices the generated body between sentinel markers
  `# === rig-managed (tmux) BEGIN ===` / `# === rig-managed (tmux) END ===`, replacing **only**
  between the markers (conda-init style). Lines before and after the block are preserved.

**First apply / migration.** When the existing `~/.tmux.conf` carries rig-owned settings inline
(resurrect/continuum/tpm/Moshi), rig backs the **original** up to a UNIQUE timestamped
`~/.tmux.conf.rig-bak-<UTC>` before wiring the managed region — so every migrating apply keeps its
OWN restore point and a later apply (after a hand-edit) never loses the in-between state by
skipping a backup. The migration **neutralizes** (comments out, with a
`# rig-migrated (now in rig.tmux.conf):` marker) exactly the **rig-OWNED** init that rig now
re-emits itself in `rig.tmux.conf` (in BOTH import and block apply mode):

- the three rig-owned `@plugin` declarations — `tmux-plugins/tpm`, `…/tmux-resurrect`,
  `…/tmux-continuum`;
- every `@continuum-*` and `@resurrect-*` option (restore, boot, boot-options, save-interval,
  processes, capture-pane-contents, strategy-vim, hooks, …);
- the plugin **init** lines — `run-shell …/tmux-resurrect/resurrect.tmux`,
  `run-shell …/tmux-continuum/continuum.tmux`, and tpm's `run '…/tpm/tpm'`;
- the Moshi `status-left`/`status-right` wipe.

This is the root-cause completion for the **double-init** bug: a hand-written
`run-shell …/continuum.tmux` runs continuum-restore *before* rig's appended `source-file` sets the
login-shell `default-command`, so restored panes spawn **non-login** (`~/.zprofile` skipped); and a
live `@continuum-boot 'on'` / `@resurrect-processes '…'` fights rig's clean values. rig re-runs
these inits in the pinned order at the END of its sourced file, so the inline copies must go.

rig **owns the whole resurrect/continuum surface**, so it neutralizes *every* live `@continuum-*` /
`@resurrect-*` set directive — including options it does not itself re-emit (e.g.
`@resurrect-strategy-vim`, `@continuum-boot-options`). Those are **recoverable** from the
timestamped `.rig-bak-<UTC>` backup. To keep a value rig models, set the matching `tmux:` knob in
`rig.yaml`. An *unmodeled* option you truly need is best carried in a **separate** tmux file you
`source-file` yourself **after** rig's import (rig only neutralizes lines IN `~/.tmux.conf`/the
managed conf, never another file you source) — re-adding it as a bare line inside `~/.tmux.conf`
will be re-neutralized on the next `rig apply`, by design (rig owns that surface). The match is
anchored to a `set`/`set-option`/`set-window-option`/`setw` directive, so an
`@continuum-…`/`@resurrect-…` token appearing only inside a `status-right` **value** or a
keybinding is **not** neutralized.

**What migration NEVER touches** (no over-reach): a **third-party** `@plugin`
(`tmux-plugins/tmux-sensible`, `tmux-yank`, any non-rig plugin) — rig's tpm, run at the end of
`rig.tmux.conf`, loads it — and every **personal pref** (`set -g mouse on`, history-limit,
base-index, set-titles, `update-environment MOSHI_CLIENT`, key bindings, a real `status-right`
value, status styling that isn't continuum's, …). The migration is **idempotent**: re-applying an
already-migrated conf never double-comments and (when nothing else changed) writes nothing.
`rig status` reports drift on the **managed region only** (the generated file, the scripts, the
boot plist, the import line / managed block) — never on the user's hand-written lines.

**The root-cause ordering guarantee.** tmux-continuum's autosave timer lives in `status-right`.
A hand-written conf that ran `set -g status-right ''` (a Moshi tweak) **after**
`run-shell …/continuum.tmux` silently wiped continuum's hook → autosave died → a reboot restored
a weeks-stale session. rig's generator pins the order: plugin options → session-resume hooks (cc/omp/codex) →
**pane_titles (default-on) + the Moshi tweak (opt-in), BOTH BEFORE continuum init** → resurrect
init → **continuum init LAST** → tpm init last-of-all. So neither tweak can ever wipe continuum's
hook. `pane_titles.clear_status_right` (default-on when `pane_titles.enabled` is also on) also
clears `status-right` — not gated on `$MOSHI_CLIENT`; it's a separate feature, dropping tmux's
default clock+date rather than the Moshi gesture fix — but the same BEFORE-continuum-init
placement applies for the same reason.

**Boot + live activation (clean machine → fully working, zero manual steps).** A `rig apply`
with `boot.enabled` writes a launchd agent whose entrypoint is the generated **boot script**
(`tmux-boot.sh`), then **`launchctl load -w`**s it so it fires at login across reboots. The boot
script runs `tmux new-session -d` (NOT `tmux start-server`): a bare `start-server` starts an
**empty** server that loads neither the config nor any plugin (tmux sources the conf only on the
first session), so `@continuum-restore` never fires — `tmux ls` says "no server running" after
login. Creating a session loads `~/.tmux.conf` → the sourced `rig.tmux.conf` → continuum →
restore. The boot script is idempotent (`has-session` → exit 0), so a warm login never spawns a
duplicate. On the same apply rig also: creates `~/.tmux/resurrect` (absent → resurrect writes no
snapshot → nothing to restore); **clones** `tpm` + `tmux-resurrect` + `tmux-continuum` into
`~/.tmux/plugins` if missing (so the `@plugin` decls resolve on a clean machine); takes a first
`resurrect save` (so a reboot has something to restore); and on macOS **cleans continuum's own
stale boot** (its `osx_iterm/terminal_start_tmux.sh` Login Items + an old `Tmux.Start` launchd
agent) that would otherwise compete with rig's boot agent. Every step is idempotent and non-fatal
(an offline machine just skips the clone and retries on the next apply). Set `RIG_TMUX_DRY_RUN=1`
to write the on-disk artifacts but skip all live activation (CI / containers). A HOME-overridden
run skips the live activation automatically and surfaces it as a warning in the action detail
(`HOME is overridden … (sandboxed run)`) — see the `models` dry-run note (rig-cli#116).

**cc-restore — per-window Claude Code resume by exact session id.** rig installs two managed
scripts and wires them via `@resurrect-hook-post-save-all` / `@resurrect-hook-post-restore-all`:

- **`cc-save.sh`** — for every pane whose **process tree** contains a `claude` process, take its
  cwd, find the **newest** session id under `~/.claude/projects/<encoded-cwd>/`, and write a
  `window/pane → cwd → session_id` map. **Detection is by the process TREE, not the command
  string:** Claude Code shows up in `pane_current_command` as its VERSION (e.g. `2.1.178`), and
  the real `claude` process is a CHILD of the pane's shell — so cc-save walks the pane's
  descendants (`ps -eo pid,ppid,args`) for a process whose **executable** (argv[0]) is `claude`.
  (Filtering on `pane_current_command == claude` matched nothing → an empty map → cc never
  resumed.) **It matches the versioned install too:** cc installs as a symlink
  `~/.local/bin/claude → …/claude/versions/<version>`, so launched by the resolved path the
  process name is the *version* (`2.1.179`), not `claude`; cc-save also matches an argv[0] under
  `…/claude/versions/`. It reads the full `args` (not `comm`) so the path is visible on **both
  macOS and Linux** (Linux `comm` is the truncated basename, with no path), and keys on argv[0]
  only so a `claude` that is merely an *argument* (`vim claude.md`, `grep …/claude/versions/`)
  never false-matches. *Accepted limitations:* an install path containing a **space**, or a
  **wrapper** launch that rewrites argv[0] (`npx claude`, `node …/cli.js`), is not detected — both
  are absent from the canonical direct-exec install this targets.
  **Encoding (verified against real on-disk dirs):** the projects-dir name is the cwd with
  **every `/` and `.` replaced by `-`** (e.g. `/Users/u/.files` → `-Users-u--files`).
- **`cc-restore.sh`** — after a reboot, for each mapped window run `claude --resume <id>` —
  **only into a fresh shell pane** (never on top of a running `claude`). A stale/missing id
  falls back to `claude --continue` (most-recent session in that cwd) so a reboot is never left
  with a dead pane.

**Why `claude`/`omp`/`codex` are NOT in the default `@resurrect-processes`.** When a harness's
own `*_restore` is on, its `*-restore` script owns the resume, so rig **deliberately leaves that
harness's name OUT** of `@resurrect-processes`: if it were in the list, tmux-resurrect would
restart the pane as a *bare* process (a new/default session) **before** the `*-restore` hook
runs, and `*-restore` — which only ever resumes a *fresh shell* — would then skip that pane,
leaving the wrong session. So resurrect brings the **shell** back and `*-restore` does the exact
resume. (With e.g. `cc_restore: { enabled: false }`, `claude` *is* added to
`@resurrect-processes` as the best-effort fallback — independently for each of `omp_restore` /
`codex_restore`. You can also list a name explicitly in `resurrect.processes` to force it in.)

**Known limitation — per-cwd, not strictly per-pane.** None of the three harnesses expose their
session id per tmux pane, so each `*-save` script records the **newest** matching session for
each pane's **cwd**. Two panes of the **same** harness in the **same** directory therefore map to
the same session and `*-restore` resumes both into it. Per-window exact resume holds when each
pane is in a **distinct** cwd (the common case).

**omp-restore / codex-restore — the same exact-id resume, generalized to omp and Codex CLI.**
rig installs the same `*-save.sh` / `*-restore.sh` pair for **omp** (this harness, oh-my-pi) and
**Codex CLI**, wired through the SAME `@resurrect-hook-post-save-all` /
`@resurrect-hook-post-restore-all` hooks as cc-restore. Since each of those is a single tmux
option holding one shell command, every **enabled** harness's script is **chained** into that one
hook value (`'cc-save.sh'; 'omp-save.sh'; 'codex-save.sh'`, `eval`'d in sequence by resurrect) —
turning a harness off simply removes its command from the chain, it never clobbers the others'.

- **`omp-save.sh` / `omp-restore.sh`** — same process-tree-walk detection as cc-save (omp's own
  binary is a direct/descendant child of the pane's shell), matching argv[0] `omp` or `*/omp`
  (omp installs as a single compiled binary, so — unlike claude — there is no versioned-symlink
  quirk to also match). omp keys `~/.omp/agent/sessions/<cwd-slug>/<ISO-ts>_<uuid>.jsonl`, where
  `<cwd-slug>` is the cwd **relative to `$HOME`** with every `/` replaced by `-` (dots preserved)
  — a **different** encoding from Claude Code's full-absolute-path, dot-folding
  `~/.claude/projects/<enc>` convention. `PI_CODING_AGENT_DIR` / `PI_CONFIG_DIR` are honored if
  set in the ambient environment the resurrect hook runs under, mirroring omp's own resolution.
  The map records the **full session file path** (not a bare id) — `omp --resume` accepts "an ID
  prefix, a path, or picker" per `omp --help`, and a path sidesteps any ambiguity over whether the
  session's leading ISO-timestamp is part of its "id". After a reboot, `omp-restore.sh` runs
  `omp --resume <path>` into a fresh shell pane; a stale/missing file falls back to
  `omp --continue`.
- **`codex-save.sh` / `codex-restore.sh`** — same process-tree-walk detection, matching argv[0]
  `codex`/`*/codex` (the Codex CLI's `node` launcher `spawn()`s a native per-platform `codex`
  binary as a child; the walk finds that descendant). Unlike claude/omp, Codex's session store is
  **not cwd-keyed on disk**: rollout files live under
  `~/.codex/sessions/<local-YYYY>/<local-MM>/<local-DD>/rollout-<local-ISO-ts>-<uuid>.jsonl`, and
  the cwd is recorded only inside each file's first `session_meta` JSON line. So codex-save scans
  the last **2 days** of local-date directories (newest day/file first) reading just each
  candidate's first line until a cwd match is found — **known limitation:** a cwd whose most
  recent Codex session predates that 2-day window is missed (unbounded scanning of a
  multi-gigabyte session store on every periodic save is not an acceptable tradeoff). `RIG_CODEX_HOME`
  overrides the session-store root (ambient `CODEX_HOME` is deliberately ignored, mirroring rig's
  own Codex config-root resolution); default `~/.codex`. After a reboot, `codex-restore.sh` runs
  `codex resume <session-id>` into a fresh shell pane; a stale/missing rollout file falls back to
  `codex resume --last`.

**anti-sprawl — one canonical session.** A Moshi/iTerm reconnect that ran a bare `tmux` spawned
a **duplicate** session. rig installs `tmux-attach.sh` (attach `<session>` if it exists, else
create it). Wire it from the login shell (documented, **not** auto-wired — rig never edits the
user's shell rc): `[ -z "$TMUX" ] && exec ~/.config/rig/tmux/tmux-attach.sh`. On this machine
there is **one** canonical tmux path — the rumored second wrapper (`ln`/`.ln.conf`) does not
exist here (`/bin/ln` is coreutils; no `~/.ln.conf`), so there is nothing to reconcile against.

**boot.** rig's launchd agent is the **single** boot path; the mechanics are in the
"Boot + live activation" section above — in short: the agent's `RunAtLoad` plist runs the
generated **boot script** (`tmux-boot.sh` → `tmux new-session -d`, NOT a bare `tmux start-server`,
which would start an empty server with no conf/plugins loaded), `@continuum-restore 'on'` restores
the saved session into it, and `rig apply` **`launchctl load -w`s** the agent so it fires at login.
**rig deliberately keeps `@continuum-boot 'off'`** in the generated config: `@continuum-boot 'on'`
would make tmux-continuum install its OWN, untracked boot artifact (the iTerm-coupled
`Tmux.Start.plist` on macOS / a systemd user unit on Linux) — a second, competing boot path; rig
also **cleans** that stale artifact on macOS. So continuum handles *restore*, rig's launchd agent
handles *boot*. rig never runs `tmux source-file` against the user's **live** server (the user
reloads their config when ready); the boot agent's script is idempotent (`has-session` → exit 0,
no duplicate, no pane touched), so loading it does not disrupt an active session. The
boot-from-cold path can only be fully proven by an actual reboot.

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

## `gitignore`

Maintains a **rig-managed block** in git's **GLOBAL excludes file** (`core.excludesfile`) so
harness artifacts are ignored in **EVERY repo on the machine** — with **zero per-repo commits**
and no per-repo `rig apply`. The motivating case: Claude Code creates throwaway worktrees under
each repo's `.claude/worktrees/`; those must be gitignored everywhere, and rig owns that ignore
**globally** (one managed block, machine-wide) rather than per-repo. This is **GLOBAL config** —
it belongs in the global rig layer (`~/.config/rig/config.yaml`), wired like the git-hooks
`dispatcher` (a `git config --global` setting plus a managed file). Default **ON** — on `rig init`
AND `rig apply` rig converges the block; idempotent (a re-apply that finds it correct is a no-op).

```yaml
gitignore:
  enabled: true                 # provision the managed block (default ON; false opts out)
  entries:                      # the ignored paths inside the managed block
    - "**/.claude/worktrees/"   # default: Claude Code's throwaway worktrees (every repo)
    - ".metadata_never_index"   # default: the macOS Spotlight sentinel the `spotlight:` sweep drops
  # excludesfile: ~/.gitignore  # rare: force a specific file instead of honoring core.excludesfile
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | provision the managed block (set `false` to leave the global excludes file untouched) |
| `entries` | list[str] | `["**/.claude/worktrees/", ".metadata_never_index"]` | the paths ignored inside the managed block; an empty/absent list uses the default. Each entry must be a single non-empty line (no `\n`/`\r`/other `str.splitlines()` separator) and may not contain a rig-managed marker line — enforced by `validate()` and mirrored in the published JSON schema (`GITIGNORE_ENTRY_JSON_PATTERN`) |
| `excludesfile` | str | *(unset)* | force a specific file; by default rig honors `core.excludesfile` (or sets it — see below) |

**Target resolution (the headline behavior).** rig decides WHICH file holds the block at apply
time, honoring the user's existing choice:

- **`core.excludesfile` is already set** (e.g. `~/.gitignore`): rig manages the block **in that
  file** and leaves the git config alone — your choice is respected, the block is not moved.
- **`core.excludesfile` is unset**: rig **sets** it to the XDG default `~/.config/git/ignore`
  **and** writes the block there. So on a **clean machine** `rig init` does everything itself
  (set the git config if absent + write/reconcile the block) — no manual `git config` step.
- **`excludesfile:` override set**: rig reconciles the block in that file and points
  `core.excludesfile` at it when git's value doesn't already match.

**`.serena/` is intentionally NOT ignored.** Serena state is **committed** shared project memory
(project memories travel with the repo), so it is never in the default entries — only throwaway
harness artifacts are.

**The managed block.** rig fences its lines with explicit markers and a fixed explanatory comment,
and touches **only** what is between them — every other line in the excludes file (the user's or
another tool's) is preserved verbatim (CRLF, trailing blanks, no-final-newline all survive):

```
# >>> rig-managed (do not edit) >>>
# Claude Code creates throwaway worktrees under each repo's .claude/worktrees/ and rig's spotlight sweep drops .metadata_never_index into dependency dirs; rig ignores both globally.
**/.claude/worktrees/
.metadata_never_index
# <<< rig-managed (do not edit) <<<
```

**Caveat of the bare-basename sentinel ignore.** `.metadata_never_index` is ignored at ANY depth,
machine-wide. A repo that deliberately TRACKS a file by that name (dotfiles-style repos commit the
Apple sentinel to keep Spotlight off a directory) still has its already-tracked copies untouched
(ignores never affect tracked files), but a NEW one needs `git add -f` or a repo-local
`!.metadata_never_index` negation in that repo's `.gitignore`.

**Strict idempotency.** A re-apply is a **byte-identical no-op** when the block is already correct.
If a prior non-idempotent tool appended the block **more than once**, rig **collapses the entire
rig-managed region to one correct block** (it never duplicates, and never edits lines outside the
markers). An **unbalanced** marker pair (a begin with no end, an end before a begin) is a
`conflict` rig leaves untouched and surfaces for manual reconcile.

**Drift.** `rig status` flags the GLOBAL block as drift when it is missing, divergent, or
duplicated — **and** when `core.excludesfile` is unset and rig would set it. `rig apply`
reconciles. Shown in the **global** section of status (not the repo section).

---

## `bun`

Turns on bun's [global virtual store](https://bun.sh/docs/pm/global-store) for this
machine. The store is a **no-op under the hoisted linker**, so `rig apply` writes
**both** keys:

```toml
[install]
linker = "isolated"
globalStore = true
```

Packages then live once under `~/.bun/install/cache/links/`, and each project's
`node_modules` is a few megabytes of symlinks. A warm
`rm -rf node_modules && bun install` is symlink-only (~7× faster on bun's fixture).

This is **GLOBAL, opt-in** — `~/.config/rig/config.yaml`, **default OFF**. Isolated
linking can surface phantom deps in previously-hoisted projects, so the flag stays
off until you set `bun.enabled: true`.

Requires a bun that implements `install.globalStore` (1.4+; check `bun --version`).

**uv** already uses a global cache (`~/.cache/uv`). macOS default link-mode is
clone (APFS CoW; `nlink=1` so `du` still looks like copies).
`link-mode = hardlink` in `~/.config/uv/uv.toml` shares inodes. rig does **not**
provision `uv.toml`.

```yaml
bun:
  enabled: true                 # splice isolated linker + globalStore into ~/.bunfig.toml (default OFF)
  worktree_install: true        # `rig worktree create` runs `bun install --frozen-lockfile` in bun repos (default ON)
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | splice a managed `[install] linker = "isolated"` + `globalStore = true` block into the global bunfig |
| `worktree_install` | bool | `true` | while `enabled`, `rig worktree create` installs a bun repo's deps in the new worktree (see below) |

**What apply splices** (markers included; everything outside the markers is left
byte-for-byte):

```toml
# >>> rig-managed bun (do not edit) >>>
# bun install: isolated linker + globalStore — packages live once under ~/.bun/install/cache/links/.
# Replace via `rig config set bun.enabled true --global --commit` (off: `rig config set bun.enabled false --global --commit`).
[install]
linker = "isolated"
globalStore = true
# <<< rig-managed bun (do not edit) <<<
```

Path: `$HOME/.bunfig.toml`, or `$XDG_CONFIG_HOME/.bunfig.toml` when that env is set.
A project `bunfig.toml` still overrides the same keys.

**Hand-written bunfig.** If the file already has extra user keys **and** both
required values (`linker = "isolated"` and `globalStore = true`), apply leaves it
(no markers added). If it has no `[install]` table, apply **appends** the block.
If it already has `[install]` without those keys, that is a **conflict** (a second
`[install]` header is invalid TOML): add the keys by hand. Unbalanced markers are
also a conflict. Apply never writes on conflict.

**Drift / verify.** Missing or stale managed block → apply splices. Conflict stays.
Verify checks both `[install]` keys.

**Worktree installs.** With `enabled` and `worktree_install` on, `rig worktree create` runs a
plain `bun install --frozen-lockfile` in a new worktree whose root has `bun.lock`/`bun.lockb`, so
agents never improvise (symlinking `node_modules` to the main checkout — `bun install` then writes
through the link — or a full `npm install` copy). rig adds no linker or store flags: the repo's
own `bunfig.toml` wins over the global one, so a repo that pins `linker = "hoisted"` or sets
`globalStore = false` (the store breaks typecheck in some repos — TypeScript cannot always resolve
peer-dependency types through the real paths under `links/`) gets its own layout. If the global
bunfig is not provisioned (`rig apply` first) or bun is missing, it skips and prints the command;
a `node_modules` symlink is refused. A failed install leaves the worktree in place and exits `2`
with the command to finish it (the install is bounded at 15 minutes). The install runs the
repo's own `preinstall`/`postinstall` scripts, as any `bun install` does — so enabling bun means
trusting the repos you create worktrees for (set `worktree_install: false` if not). When the
worktree's `core.hooksPath` is outside the repo (rig's global hooks dir), the install sees it
pinned to the repo's own `.git/hooks` (via `GIT_CONFIG_COUNT`), so a `prepare: lefthook install` /
`husky` writes its shims where the global composer runs them, never over the composer. Only the
global config turns the install on — it runs the repo's install scripts, so a cloned repo's committed
`rig.yaml` must not opt a machine in — but a repo can turn it off with
`bun: {worktree_install: false}` in its `rig.yaml`.

**Worktree install check.** While the global `enabled` is on (a repo `enabled: false` turns it
off), `rig status` inside a repo scans every worktree of
that repo that has a bun lockfile. States that are wrong in every repo report as
`bun/worktree:<path>` **drift** (disk→config):

- a `node_modules` or `node_modules/.bun` that is a **symlink**, or `.bun` entries linking into
  another checkout's `node_modules` (`bun install` writes through them);
- **broken links** from `node_modules/.bun` into `~/.bun/install/cache/links/` (a wiped cache).

Installs that merely do **not use the store** are an informational note on the `bun` area (never
drift, never the exit code), because a repo may need a local install: a `node_modules/.bun` whose
entries are mostly real directories (bun keeps a few lifecycle-script packages such as `esbuild`
as directories even in a store install, so the majority is the signal), or a hoisted layout. A
repo whose `bunfig.toml` pins a linker or `globalStore = false` is not reported at all.

`rig apply` never deletes, so each item names the fix: remove that worktree's `node_modules` (or
the symlink) and run `bun install --frozen-lockfile` there. The agent-tools `package-store-guard`
hook blocks the commands that cause these states.

---

## `git`

Provisions this machine's **global git settings** — identity, pull/branch defaults, URL rewrites —
with `git config --global <key> <value>`, so a new machine gets the same git behavior from the same
config. **GLOBAL layer only**: `~/.config/rig/config.yaml`. The loader **rejects** a `git` block from
a repo `rig.yaml` (or a `--config` file) with exit `2`, because several git keys execute code
(`core.sshCommand`, `core.fsmonitor`, `credential.helper`, `!`-aliases) and a cloned repo must never
be able to rewrite your global git config through `rig apply`.

```yaml
git:
  enabled: true                  # optional — block PRESENCE opts in; only `false` opts out
  config:                        # dotted git keys → value
    user.name: Alex Ultra
    user.email: someone@example.com
    pull.rebase: true            # bools are written as true/false
    init.defaultBranch: main
    url.https://git.hyperide.ai/.insteadOf: hy:   # a subsection may hold dots and slashes
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | — (see below) | opts OUT when explicitly `false`. Like `env`, a PRESENT `git:` block with `enabled` unset opts in; an absent block is a no-op. |
| `config` | map[git key → string\|bool\|int] | `{}` | each entry is one `git config --global <key> <value>`. Bools are written `true`/`false`, ints as digits. |

**Keys** follow git's own shape: `section.name` or `section.subsection.name`. Section and name start
with a letter and hold only letters, digits and `-` (git compares them case-insensitively, so
`user.name` and `User.Name` in one block is rejected as a duplicate); the subsection is everything
between the first and last dot and may hold dots and slashes. Rejected fail-closed (exit `2`, schema
path `git.config`): a malformed key, a nested mapping instead of a dotted key (`user: {name: …}` —
write `user.name: …`), a value with a line break, a list (multi-valued keys such as
`remote.*.fetch` are out of scope), a float/null (quote it), and **`core.hooksPath` /
`core.excludesfile`** — the `git_hooks` dispatcher and the `gitignore` block own those, and two
owners would fight on every apply. `rig config set --global git.config.user.name "Alex Ultra"`
works: everything after `git.config.` is taken as ONE git key, dots and all
(`git.config.credential.https://git.example.com.helper` → key `credential.https://git.example.com.helper`).

**Apply** sets every key that is unset or differs; a key already holding a value that means the same
to git is left alone (`yes`/`on`/`1` match a desired `true`; `1k` matches `1024`). A value you set by
hand to something else is overwritten, and the result detail records the prior value with the exact
restore command (`prior user.email='old@…' (restore with: git config --global user.email old@…)`);
`defaults.on_conflict: skip` leaves such a key untouched instead. rig **never unsets** a key: keys in
`~/.gitconfig` that the config does not declare are not drift and are never touched.

**Drift / verify.** `rig status` reports one row per declared key that is missing or different
(`git/pull.rebase: global pull.rebase is 'false', config wants 'true'`); the post-apply verify reads
every key back. All three (apply, drift, verify) share one classifier (`riglib/gitconfig.py`).
`$GIT_CONFIG_GLOBAL` is honored (git's own `--global` resolution decides the file).

---

## `profile`

Where `rig profile` keeps this machine's **global config** in sync with your other machines: a
**private** git repo (conventionally `https://git.hyperide.ai/<login>/.rig-profile`) holding one file,
`config.yaml` — a copy of `~/.config/rig/config.yaml`. **GLOBAL layer only**, rejected from a repo
`rig.yaml` like `git`. Written by `rig profile init`; see the README's
[`rig profile`](../README.md#rig-profile--carry-your-global-config-between-machines) section for the
workflow.

```yaml
profile:
  repo: https://git.hyperide.ai/alex/.rig-profile   # the PRIVATE profile repo
  # auto_sync: true                                  # default: `rig apply commit` syncs first
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `repo` | string | — | the profile repo URL (https, ssh, or a local path). One line, no leading `-`. `rig profile init` records it and never rewrites an existing value (machines may spell one repo as ssh vs https). |
| `auto_sync` | bool | `true` | `rig apply commit` runs `rig profile sync` BEFORE building its plan (a pull changes the very file the plan reads). Non-fatal: a network/push failure or a conflict prints a warning and the apply continues with the local file. A pull is announced as a warning, since the plan that runs is then built from a config you did not preview. A preview (`rig apply` / `apply info`) never syncs. |

**The repo MUST be private.** The file travels in plaintext and `env.vars` (see [`env`](#env)) often
holds API keys. Every push of a config carrying `env.vars` warns (loudly the first time on each
machine, naming the keys, never the values), and a push is **refused** when the forge reports the repo
as public to an anonymous API request (Forgejo/Gitea `/api/v1/repos/<owner>/<repo>`, GitHub
`api.github.com/repos/…`; skipped with a note when that cannot be checked, e.g. offline or a local
path). Write access to the repo is write access to this machine's global rig config — and, through
the `git` block, its global git config — so guard it like your dotfiles.

**What lives where.** The checkout is `~/.config/rig/profile/repo/` (rig-owned: never edit it by
hand); the last-synced snapshot hash is `~/.config/rig/profile/state.json`, **beside** the checkout,
never committed. Both follow `$XDG_CONFIG_HOME`.

---



## `spotlight`

Keeps **macOS Spotlight** (`mds_stores`) from indexing **dependency/build dirs** — the gigabytes of
`node_modules` / `dist` / `target` / `.venv` junk that otherwise get re-scanned after every reboot,
thrashing the machine. The mechanism is the universal, per-directory opt-out macOS honors: an empty
sentinel file named **`.metadata_never_index`** in a directory tells Spotlight to skip that
directory's contents — no global Search-Privacy list, and crucially **not** `mdutil -i off` (which
would kill Spotlight wholesale; rig never does that). rig sweeps the configured dev roots dropping
the sentinel into every matched dir, and installs a **launchd re-sweep agent** (RunAtLoad + a daily
`StartCalendarInterval`, with StandardOut/ErrorPath logging) so **new** projects are covered without
any manual step. This is **GLOBAL, macOS-only, opt-in config** — it belongs in the global rig layer
(`~/.config/rig/config.yaml`) and is **default OFF** (a `spotlight:` block with `enabled: true`
turns it on). On a non-macOS host the sweep still drops sentinels but no launchd agent is written.

```yaml
spotlight:
  enabled: true                 # provision the sweep + re-sweep agent (default OFF; opt-in, macOS)
  # roots:                      # dev roots to sweep (default: ~/work, ~/xp)
  #   - ~/work
  #   - ~/xp
  # deny:                       # REPLACE the default excluded dir basenames (node_modules/dist/…)
  #   - node_modules
  # extra:                      # ADD extra excluded dir basenames on top of the default set
  #   - .cache
  # label: ai.hyperide.spotlight-exclude   # the launchd agent label
  # max_depth: 8                # walk-depth cap below each root
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | provision the sweep + launchd re-sweep agent (opt-in, macOS) |
| `roots` | list[str] | `["~/work", "~/xp"]` | dev roots swept for dependency/build dirs |
| `deny` | list[str] | *(the built-in set)* | dir **basenames** excluded; a non-empty list **REPLACES** the default set |
| `extra` | list[str] | *(none)* | dir basenames **added** to the default set |
| `label` | str | `ai.hyperide.spotlight-exclude` | the launchd agent Label / plist filename stem |
| `max_depth` | int | `8` | walk-depth cap below each root (matched dirs are pruned, never descended) |

The default `deny` set covers frontend AND backend: `node_modules`, `dist`, `build`, `.next`, `out`,
`coverage`, `.turbo`, `.nuxt`, `.svelte-kit`, `.parcel-cache`, `.cache`, `target`, `.cargo`, `.venv`,
`venv`, `__pycache__`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `vendor`, `.gradle`,
`DerivedData`. The walk **prunes** a matched dir (it never descends into a `node_modules` it just
sentinelled) and never forces a full-volume reindex — the sentinels take effect as Spotlight
re-crawls (a forced reindex would be the opposite of the goal).

**The re-sweep agent.** `rig apply` writes `~/Library/LaunchAgents/<label>.plist` and
`launchctl load`s it. The plist runs `rig spotlight-sweep` (the same sweep, headless) at login and
daily, pinned to the interpreter that ran `rig apply`: with a checkout install (`install.sh`
symlinks `~/.local/bin/rig` → `<checkout>/bin/rig`) the argv is `<python> <checkout>/bin/rig
spotlight-sweep`; with a pip/pipx install it is `<python> -m riglib spotlight-sweep`. (The
module form alone never worked for a checkout install — launchd runs from `/` with no
`PYTHONPATH`, so every daily run died with `No module named riglib`; rig-cli#375.)
`RIG_SPOTLIGHT_DRY_RUN=1` writes the plist but skips the live `launchctl load` (used by
tests/CI); a HOME-overridden run skips it automatically (rig-cli#116, see the `models` dry-run
note). `rig spotlight-sweep` is also runnable by hand to re-cover new dirs immediately.

**Missed vs not-yet-swept (rig-cli#375).** A dependency dir with no sentinel is one of two very
different things, and rig reports them differently. Every sweep records **which matched dirs it
visited** (plus when it started) in `~/.cache/rig/spotlight-last-sweep.json`, and every read-back
classifies an uncovered dir against that record:

- **stale** — the last sweep visited this dir and it still has no sentinel. The sweep genuinely
  failed it (a permission error, or the sentinel was deleted afterwards). This is **drift**, and
  it **fails** the post-apply verification.
- **pending** — the last sweep never saw this dir, so it appeared afterwards. Every `pytest` run
  mints a batch of `__pycache__` dirs; a machine that builds anything has some at any moment. This
  is **not** drift — it is reported as an informational count beside "in sync", and the next sweep
  covers it (off macOS, where rig installs no re-sweep agent, the note says to run `rig
  spotlight-sweep` instead).

If there is no usable record — never swept, unreadable or wrongly-typed file, or a record made
for a different selector (other `roots`, `deny` or `max_depth`: a `.venv` newly added to `deny`
is uncovered because nothing ever swept it, not because it appeared since) — every uncovered dir
counts as stale. The record stores each visited dir's
filesystem identity (device + inode), and that — not a file timestamp — decides: directory birth
time only exists on macOS/BSD, and elsewhere `st_ctime` is bumped by deleting a file *out of* the
directory, so a timestamp-based split would excuse the single case this feature most needs to
catch — a hand-deleted sentinel (same inode, no sentinel → stale). Identity rather than the bare
pathname because `rm -rf dist && mkdir dist` is how builds work: the new `dist` at the old path
was never swept (new inode → pending), not failed by the sweep.

**Verification.** After `rig apply`/`rig init`, the post-apply **verify** framework confirms that
**every** matched dir carries the sentinel (not a sample) AND that the launchd agent is loaded.
Only *stale* dirs fail it; *pending* ones are named in the evidence line. Full-set checking is
free: the directory walk is the entire cost and it was already being paid — classifying each dir
adds one `stat`.

**Drift.** `rig status` reports Spotlight-exclude drift through the **same** coverage predicate the
verify step uses, so the two can never contradict each other: *stale* dirs (→ `missing`, with the
count) and — on macOS — the launchd re-sweep plist being absent, hand-modified, or not loaded.
*Pending* dirs print as a dim note next to the area's state. Remediation differs by surface: `rig
spotlight-sweep` re-covers only the missing **sentinels** (it touches no launchd state); `rig
apply` reconverges **both** — it re-sweeps AND rewrites/reloads the launchd plist. The
sentinel-coverage check runs cross-platform; the launchd surface is macOS-only (no plist is written
elsewhere).

---

## `fj`

Forgejo access for this machine (GLOBAL; see `riglib/fj.py`). The ecosystem lives on Forgejo
(`git.hyperide.ai`), whose CLI `fj` otherwise needs `--host` outside a clone, can only log in
with a hand-made token, and has no `ship`. A **present** block opts in; `host` is then required.

```yaml
fj:
  host: git.hyperide.ai        # login target + FJ_FALLBACK_HOST
  # install: true              # the fj command: real fj (brew / pinned binary) + the `fj ship` wrapper
  # version: "0.6.0"
  # bin_dir: ~/.local/bin      # where the fj wrapper goes (must be on PATH, before any other fj)
  # git_credentials: true      # Git Credential Manager OAuth for https://<host>
  # client_id: …               # default: Forgejo's built-in `tea` OAuth app
  # git_client_id: …           # default: Forgejo's built-in Git Credential Manager OAuth app
```

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `enabled` | bool | — | block **presence** opts in; `false` opts out |
| `host` | str | — (required) | the Forgejo host; a scheme/trailing path is stripped |
| `install` | bool | `true` | manage the `fj` command: the real fj + the wrapper that adds `fj ship`; `false` leaves fj entirely to you (no wrapper, no `fj ship`) |
| `version` | str | `0.6.0` | release rig downloads (only versions with a pinned sha256) |
| `bin_dir` | str | `~/.local/bin` | where the `fj` wrapper goes — must be on PATH, ahead of any other `fj` |
| `git_credentials` | bool | `true` | write GCM generic-OAuth settings for `https://<host>` (the global host only) |
| `client_id` | str | Forgejo `tea` app | OAuth client `fj auth login` uses |
| `git_client_id` | str | Forgejo GCM app | OAuth client GCM uses |

`rig apply` (one `provision_fj` action): sets up the `fj` command (below); lists each host in fj's
`client_ids` file (`~/.config/forgejo-cli/client_ids`; `%APPDATA%\Cyborus\forgejo-cli\config\client_ids`
on Windows) with the client ID of Forgejo's **built-in public OAuth app** — every instance ships them
(`[oauth2] DEFAULT_APPLICATIONS`), so no admin registers anything; sets `FJ_FALLBACK_HOST=https://<host>`
(through the `env` area on macOS/Linux — a hand-set `env.vars.FJ_FALLBACK_HOST` wins, and
`env: {enabled: false}` turns the export off there, so set it in your shell profile yourself —, as a
user environment variable via `setx` on Windows); and, when git's credential helper is Git Credential
Manager, writes its `credential.https://<host>.oauth*` settings. Lines rig does not own in
`client_ids` are kept. `rig status` reports the same steps as drift. `RIG_FJ_DRY_RUN=1` makes apply
report instead of act.

**`fj ship` — the same command as `gh ship`.** fj has no alias mechanism, so `fj` on PATH is a thin
rig wrapper, `<bin_dir>/fj` (POSIX `sh`, a constant per fj version — no machine path in it).
`fj ship …` runs the **very dispatch body of the `gh ship` alias** (one function renders both, see
[`ship_delegator`](#ship_delegator)): the repo's `.claude/scripts/pr-ship.sh`, else
`$AGENT_TOOLS_ROOT/ci/ship/ship.sh` (from the environment or the machine env file
`${XDG_CONFIG_HOME:-~/.config}/agent-tools/env`), else a diagnostic and exit `127`. Every other
`fj …` execs the real fj with the arguments, stdin and exit code untouched. ship.sh picks GitHub or
Forgejo by the origin's host (its Forgejo provider: agent-tools#793), so `fj ship` and `gh ship` are
interchangeable in any repo.

Where the real fj lives:

| OS | real fj | `bin_dir` holds |
|----|---------|-----------------|
| Linux | `${XDG_DATA_HOME:-~/.local/share}/rig/fj/<version>/fj` (rig's store, not on PATH) | `fj` (wrapper) |
| macOS | Homebrew's (`brew install forgejo-cli` when no fj is on PATH); the wrapper finds it by walking PATH for an `fj` that is not a rig wrapper | `fj` (wrapper) |
| Windows (cmd, PowerShell, Cygwin, Git Bash) | `%USERPROFILE%\.local\share\rig\fj\<version>\fj.exe` — also from Cygwin, whose `$HOME` is not the profile | `fj` (wrapper, for Cygwin / Git Bash) + `fj.cmd` (cmd / PowerShell) — and **no** `fj.exe` |

Why no `fj.exe` next to the wrappers (measured on Windows 10): Cygwin and Git Bash run the
extensionless `fj` when `fj` and `fj.exe` share a PATH dir, but cmd, PowerShell and Python's
`shutil.which` pick `fj.exe` over `fj.cmd` (PATHEXT order), which would bypass `fj ship` there.
`fj.cmd ship …` runs the `sh` wrapper through the `sh` gh itself runs aliases with (`sh` on PATH,
else Git for Windows' `bin\sh.exe` next to `git`), so `fj ship` works from PowerShell as `gh ship`
does; other `fj.cmd …` calls go straight to `fj.exe`. (A batch wrapper has cmd's usual quirks:
Ctrl+C asks "Terminate batch job?".) rig's own fj calls (`fj whoami` for `rig profile`) run the
real binary directly.

**Upgrades and migration.** Changing `version` (or a rig release that bumps the default) makes the
wrapper stale: `rig apply` installs the new binary into its own store dir, rewrites the wrapper and
drops the older store dirs. A real fj that an earlier rig put into `bin_dir` (`fj` on Linux,
`fj.exe` on Windows) is recognised by its pinned sha256 and **moved** into the store — nothing else
there is ever moved or overwritten: a file that is neither rig's wrapper nor a pinned binary is
reported (`fj-wrapper` / `fj.exe`) and apply fails that step until you move it (or set
`install: false`).

**Drift** (`rig status`, the same comparison apply acts on): `fj` (real binary missing),
`fj-wrapper` / `fj.cmd` (missing, stale, a foreign file in the way), `fj.exe` (a `fj.exe` left in
`bin_dir` on Windows), `fj-path` (an `fj` in a PATH dir **before** `bin_dir` — typically Homebrew's
on macOS; apply reports it, only your shell profile can fix it). A PATH without `bin_dir` and without
any other fj (a daemon's minimal PATH) is not reported.

Logging in is then the stock commands: `fj auth login` (browser → *Authorize*) and, for git, the first
`git push` (GCM opens the browser once). A repo `rig.yaml` with its own `fj.host` adds that host to
fj's `client_ids` on `rig apply` inside the repo (a line you wrote for it yourself is kept); git's
GCM settings in `~/.gitconfig` are written for the GLOBAL host only, so a cloned repo naming
`github.com` or a company server cannot repoint git's login for it. `FJ_FALLBACK_HOST` keeps
pointing at the GLOBAL host (and is not set at all without one). `fj.host` is the only key a repo (or `--config`) layer may set: every other `fj`
key changes the machine's fj install or the OAuth app fj and git log in with, so the loader rejects
it outside `~/.config/rig/config.yaml` — a cloned repo cannot redirect your logins. fj refreshes its
OAuth token itself; Forgejo drops a refresh token unused for `[oauth2] REFRESH_TOKEN_EXPIRATION_TIME`
(default 730 h) — a machine idle that long runs `fj auth login` again. Headless: `fj auth add-token`.

---

## `tools`

rig's **primary purpose**: install + advertise the **personal CLI ecosystem** — `tg`, `review`,
`task`, `draw` (and more, declaratively) — at `rig apply`. For each declared tool, rig runs the
tool's **own `install.sh`** (which locates the repo, installs deps, symlinks the entry into the
managed PATH dir, and runs `<tool> install-skill` to advertise it into the agent harness). rig does
**not** reimplement any of that — the tool stays the single source of truth for how it installs, so
a tool changing its install is inherited for free.

**Default OFF (opt-in).** Unlike `tg_ctl`/`models`, an absent / empty / `enabled: false` block
provisions **nothing** — a machine opts in by listing tools under `items`. This is a **per-MACHINE**
concern (the tool ecosystem on this dev box), so the block belongs in the **GLOBAL** layer
(`~/.config/rig/config.yaml`), never a committed repo `rig.yaml`. Cross-platform (no launchd).

```yaml
tools:
  enabled: true              # opt-in; absent/false provisions nothing
  target: ~/.local/bin       # managed PATH dir each tool symlinks its bin into (default)
  items:
    tg:     { repo: ~/.files/repos/tg-cli }
    review: { repo: ~/xp/review-cli }
    task:   { repo: ~/xp/task-cli }
    draw:   { repo: ~/xp/draw-cli }
    dev:    { repo: ~/xp/dev-cli }
    # a bare entry defaults repo to ~/xp/<name>-cli:
    # foo: {}
    # a tool whose wrapper shells out to a Homebrew-installed binary declares that dependency —
    # rig installs the missing formula before running install.sh:
    mytool: { brew: [some-formula] }
```

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `enabled` | bool | `false` | opt-in master switch; `false`/absent = rig provisions no tools at all |
| `target` | str | `~/.local/bin` | managed PATH dir each tool's `install.sh` symlinks its entry into |
| `items.<name>` | map | — | one tool, keyed by its command name (the skill-blurb stem) |
| `items.<name>.enabled` | bool | `true` | `false` = skip this one tool |
| `items.<name>.repo` | str | `~/xp/<name>-cli` | the tool's checkout dir (holds its `install.sh`) |
| `items.<name>.bin_dir` | str | `target` | override the managed PATH dir for this one tool |
| `items.<name>.brew` | list[str] | `[]` | Homebrew formula(e) the tool depends on at **runtime** |

**Idempotent + safe.** A tool already installed — its bin resolves (the managed symlink **or**
anywhere on PATH, so a Homebrew `review` counts) **and** its skill blurb is advertised — is a
**no-op** (`skipped`); rig does **not** re-run `install.sh`. rig never deletes a user's existing
symlink. A tool that resolves but isn't advertised is (re-)installed only to wire up its skill.

**Optional Homebrew runtime dependency (`brew`).** A tool's own `install.sh` only ever installs
*itself* (locate/symlink/advertise) — it does not install packages the tool's WRAPPER shells out
to. `items.<name>.brew` (default `[]`, additive/opt-in — a tool that omits it is completely
unaffected) declares those Homebrew formula names. Before running `install.sh`, rig checks each
declared formula with `brew list --formula <formula>` and runs `brew install <formula>` for any
missing one. `brew` absent from PATH, or a `brew install` that itself fails, is a **hard error**
for that tool (not a silent skip or a crash) — a wrapper whose runtime dependency isn't installed
isn't a working tool no matter how `install.sh` went. (Forgejo's `fj` needs no `tools` entry —
the [`fj`](#fj) block installs it and its `fj ship` wrapper.)

**Drift.** `rig status` flags (in the **GLOBAL** section) a declared tool whose bin doesn't resolve
(**missing** — apply runs its `install.sh`) or that resolves but isn't advertised (**modified** —
apply re-runs `install-skill`). A declared `brew` formula Homebrew does not report as installed is
**also** flagged `missing`, independently of the bin/advertised state — one item per absent
formula — and `rig apply` runs `brew install` to reconcile it.

**Dry-run seam.** `RIG_TOOLS_DRY_RUN=1` reports what WOULD install without running any `install.sh`
or `brew install` (used by the e2e suite so tests never shell out to a real tool installer or brew).

---

## `tg_ctl`

rig provisions the **tg-ctl inbound control daemon** (tg-cli's long-poll / inject-into-tmux /
voice→text daemon, run as `tg-ctl run`) as a **macOS boot LaunchAgent** so it auto-starts at
login/boot — exactly like the tmux boot service. **Default ON** (an absent or empty `tg_ctl:`
block still provisions it, so `rig init` on a clean machine sets it up with no config). This is a
**per-MACHINE** concern (one inbound daemon per machine), so the block belongs in the **GLOBAL**
layer (`~/.config/rig/config.yaml`) — never a committed repo `rig.yaml`. **macOS-only** (launchd);
off darwin it is a no-op.

```yaml
tg_ctl:
  enabled: true                       # provision the tg-ctl LaunchAgent (default true; false = off)
  boot: true                          # write + load the boot agent (default true)
  codex_usage_hook: true              # fold tg-ctl's Codex Stop hook into rig's codex hook bridge (default true)
  # everything below is auto-discovered per-machine — override only if non-standard:
  label: ai.hyperide.tg-ctl           # launchd Label / plist filename stem (advanced)
  bun_path: ~/.bun/bin/bun            # the bun binary (default: `which bun` → ~/.bun fallback)
  tg_ctl_path: ~/.files/bin/tg-ctl    # the tg-ctl Bun script
  config_dir: ~/.config/tg-cli        # tg-cli config + launchd logs (default honors $TG_CTL_CONFIG_DIR)
```

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `enabled` | bool | `true` | `false` = **don't touch tg-ctl at all** — rig emits no action, so it neither provisions NOR cleans up NOR reports drift (a hands-off opt-out). Use `boot: false` instead to keep rig tracking a leftover plist. |
| `boot` | bool | `true` | `false` = provisioned-but-no-boot: rig writes/loads nothing, but a leftover plist (or the stale predecessor) IS still surfaced as drift so you see the orphan |
| `codex_usage_hook` | bool | `true` | whenever a codex hook bridge is being provisioned (`harness.kind: codex`, OR `codex` listed in `harness.kinds` alongside a different primary kind) and `harness.hook_bridge` is enabled, fold tg-ctl's Codex `Stop` usage-telemetry hook (`tg-ctl codex-usage-hook`) into rig's own codex hook bridge `Stop` array in `~/.codex/config.toml`, instead of tg-ctl writing its own `~/.codex/hooks.json` — avoids Codex's "loading hooks from both ..." dual-source warning (tg-cli#308). `false` = leave tg-ctl to manage its own `hooks.json` independently. No effect on a claude-code/opencode bridge (each harness kind's bridge is independent), and independent of `boot` — the fold-in runs whether or not the tg-ctl daemon itself is set to auto-start (it only needs the `tg-ctl` script + bun to be present and runnable on disk, not the daemon running). Folds in nothing (fail-closed, no error) if the resolved `tg_ctl_path` or `bun_path` does not exist/isn't executable yet. |
| `label` | str | `ai.hyperide.tg-ctl` | launchd Label / plist filename stem (one identity for install/drift/remove) |
| `bun_path` | str | discovered | the bun binary; default `which bun`, else `~/.bun/bin/bun` |
| `tg_ctl_path` | str | `~/.files/bin/tg-ctl` | the tg-ctl Bun script launchd runs (`bun <path> run`); also the binary invoked for the folded-in `codex_usage_hook` command |
| `config_dir` | str | `~/.config/tg-cli` | tg-cli config dir; the launchd out/err logs land here (honors `$TG_CTL_CONFIG_DIR`) |

> **`enabled: false` vs `boot: false`.** `enabled: false` is a complete opt-out — rig stops
> emitting the action, so a previously-installed plist is NOT cleaned up or flagged (rig is
> hands-off). If you want rig to keep watching for / flagging a leftover plist while not running
> the daemon, use `boot: false` (the action still runs; drift surfaces the orphan).

**What rig writes.** `~/Library/LaunchAgents/ai.hyperide.tg-ctl.plist` — a `RunAtLoad` +
`KeepAlive` agent that runs `bun ~/.files/bin/tg-ctl run` with a login PATH and the tg-cli config
dir's logs. The plist is **byte-exact** to a hand-created working file, so `rig apply` against an
already-correct live plist is a true **no-op** (`skipped`), never a spurious rewrite.

**(Re)load mechanism.** Unlike tmux-boot (which only writes the plist), rig **(re)loads** the agent
via `launchctl bootout`/`bootstrap` in the per-user `gui/<uid>` domain, so a clean `rig init` starts
the daemon without a reboot, and a changed plist is picked up on the next `apply`.

**Stale-predecessor teardown.** If the dead predecessor service
`~/Library/LaunchAgents/com.ultra.codex-tg-bot.plist` exists, `rig apply` **boots it out**, backs it
up (timestamped), and removes it.

**Drift.** `rig status` flags (in the **GLOBAL** section) when the agent is missing, divergent, or
written-but-not-loaded; a leftover plist when `boot: false`, or the stale predecessor, surfaces as a
disk→config **extra**. `rig apply` reconciles.

**Dry-run seam.** `RIG_TG_CTL_DRY_RUN=1` writes the managed plist into the configured (HOME-isolated)
path but skips every live/destructive mutation — the gui-domain `launchctl bootstrap`/`bootout` AND
the stale-predecessor teardown (its bootout and the on-disk backup+remove of its plist) — so
tests/smoke never touch the real launchd domain or delete the predecessor file.

**HOME override is not enough — and rig knows it (rig-cli#116).** The plist path follows `$HOME`,
but `launchctl bootstrap` loads the agent into the REAL `gui/<uid>` domain whatever `$HOME` says: a
HOME-only-isolated `rig apply` once loaded the real `ai.hyperide.tg-ctl` from a scratch-HOME plist
(pointing at a nonexistent binary) and crash-looped it until booted out by hand. So whenever the
resolved `Path.home()` differs from the uid's real login home, `rig apply` behaves exactly like
`RIG_TG_CTL_DRY_RUN` on its own — writes the plist, skips the bootstrap AND the predecessor
teardown — and reports `tg_ctl/boot: … (HOME is overridden (<home> != <real home>) — skipped live
launchctl bootstrap (sandboxed run))`. To exercise the live path on purpose, run with your real HOME.

---

## `tcc_watch`

rig provisions the **tcc-watch dialog-watcher daemon** as a **macOS boot LaunchAgent**, exactly like `tg_ctl`. It is Phase 1 (detection-only) of a defense-in-depth follow-up behind the eval-tool's TCC deny-list (`riglib/permissions.py`, PR #488).

The daemon tails macOS's own `log stream --debug` for the `com.apple.TCC` subsystem. That is Apple's own sanctioned mechanism for observing TCC activity: no private API, no `TCC.db` access. The daemon keeps a small SQLite store at `<config_dir>/tcc-watch.sqlite3`:

- every REAL permission prompt or denial, in full;
- a count of every TCC log line per hour and per message template.

It does **not** touch the live system dialog, read or write `TCC.db`, attribute an event to an agent, or send any notification. Those are Phases 2-4 of the phased ticket (see `riglib/tcc_watch.py`'s module docstring), explicitly deferred pending their own design review.

**Default ON**: an absent or empty `tcc_watch:` block still provisions it, mirroring `tg_ctl`. This is a **per-MACHINE** concern (one watcher daemon per machine), so the block belongs in the **GLOBAL** layer (`~/.config/rig/config.yaml`), never in a committed repo `rig.yaml`. **macOS-only** (launchd); off darwin it is a no-op.

```yaml
tcc_watch:
  enabled: true                          # provision the tcc-watch LaunchAgent (default true; false = off)
  # everything below is auto-discovered per-machine — override only if non-standard:
  label: ai.hyperide.tcc-watch           # launchd Label / plist filename stem (advanced)
  config_dir: ~/.config/rig/tcc-watch    # watcher script + tcc-watch.sqlite3 store + launchd logs
  python_bin: /usr/bin/python3           # the python3 binary (default: `which python3` → system fallback)
```

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `enabled` | bool | `true` | `false` = don't touch tcc-watch at all — rig emits no action |
| `label` | str | `ai.hyperide.tcc-watch` | launchd Label / plist filename stem |
| `config_dir` | str | `~/.config/rig/tcc-watch` | where the watcher script, the `tcc-watch.sqlite3` store, and the launchd out/err logs live |
| `python_bin` | str | discovered | the python3 binary; default `which python3`, else the first of `/usr/bin/python3`, `/opt/homebrew/bin/python3`, `/usr/local/bin/python3` |

**What rig writes.** rig writes `~/Library/LaunchAgents/ai.hyperide.tcc-watch.plist`, a `RunAtLoad` + `KeepAlive` agent that runs `python3 <config_dir>/watch.py <config_dir>/tcc-watch.sqlite3`. It also writes the watcher script itself, copied byte-for-byte from `riglib/tcc_watch_daemon.py`. That is the SAME module the unit suite exercises directly, so the deployed daemon and the tested logic can never drift apart.

**What gets stored (rig-cli#607).** The first cut appended every tccd line mentioning `kTCCService`/`AttributionChain` to `events.jsonl` AND echoed it to stderr: 2.1 GB plus a 2.0 GB copy in 16 days, of which 212 lines were real prompts. The store keeps three things instead:

- **`counts` + `templates`: every TCC log line, as a count.** The key is the line's os_log `formatString`, process, category, the `kTCCService*` it names, the TCC call (`function=`, plus `preflight`) and the subject app's identifier. Each template keeps its first line as an example. The rows grow with the number of distinct templates, not with line volume: a 10-minute real capture of 13,284 lines became 176 templates in 118 KB.
- **`prompts`: real prompts and denials, in full.** These are the lines whose `eventMessage` starts with one of `PROMPT_SIGNATURES`:
  - `AUTHREQ_PROMPTING:` — a consent dialog is about to show;
  - `display_prompt:` — it is on screen;
  - `CFUserNotification response:` — the user answered;
  - `Policy disallows prompt for` — denied without a dialog;
  - `Update Access Record: kTCCService` — a grant or denial was written.
- **`diagnostics`: the daemon's own diagnostics.**

**Bounded by design.**

- `counts` keeps hourly rows for 14 days, then rolls them into one row per UTC day per template, kept forever. The rows therefore grow with the templates active each day (capped, below), never with line volume.
- Past 20,000 templates, new keys fold into ONE overflow template per kind, and every key field is length-capped. The store therefore stays bounded even if something logs under `com.apple.TCC` with non-constant format strings, categories or process names.
- The store and its WAL files are owner-only (0600), and so are the config dir (0700) and launchd's logs: they hold TCC decisions, like `TCC.db` itself. Prompt text is never written to stderr.
- A store file that is not a database is moved aside, with its `-wal`/`-shm` files, as `*.unreadable-<time>` (kept) and a new store is started. A locked or failing store is retried with backoff. Either way the daemon does not crash-loop or spin.
- `prompts` keeps the newest 20,000 rows and is trimmed every 500 inserts, so a flood of prompt-shaped lines is bounded too. `diagnostics` keeps the newest 1,000.
- Counts are buffered and written at most a minute after they happen while lines keep arriving, and on exit (SIGTERM from `launchctl bootout` flushes them). Prompts are written immediately.
- stderr (`launchd.tcc-watch.err.log`) gets diagnostics only, and nothing once it is past 1 MiB. The daemon never truncates it.
- A `log stream` that keeps exiting backs off exponentially, up to 60 s.

The numbers are constants at the top of `riglib/tcc_watch_daemon.py`.

**Reading it.** `python3 <config_dir>/watch.py report [--days N] [--top N] [--json]` reads the store beside the script by default and prints:

- line totals per day;
- the share per kind (noise vs each prompt kind);
- the busiest processes;
- the busiest templates, with their example line;
- the newest prompts and denials.

In other words: what fills the TCC log, which app drives it, and which dialogs actually appeared. `report` only reads, so it answers even while the daemon is writing. Nothing else in rig reads the store.

**Upgrading from `events.jsonl`.** On its first start, the daemon imports the pre-#607 `events.jsonl` family next to the store, oldest first:

- every line is counted in its UTC day under a coarse legacy template;
- every real prompt is stored in full.

The import runs before the first `log stream` spawn, which takes minutes for a multi-GB file, so a prompt shown during that one-time upgrade window is not recorded. Each file is imported in ONE transaction. The transaction commits only if the store's totals grew by exactly the file's line count, and only then is the file removed. A retry after a crash cannot double-count, because the file's size and mtime are recorded with the import.

The old per-event mirror lines in `launchd.tcc-watch.err.log` are removed only when every one of them equals, byte for byte, the line the old daemon printed for an imported row, in order. The count verified so far is committed with each file's import, so if the first start is interrupted, a later start resumes the verification and finishes the strip. Every other line in that file is kept.

The prompt signatures are tccd's wording as captured on macOS 26.5. A release that rewords them loses nothing: the lines are still counted. But they would no longer be copied into `prompts`, so check `report` for new prompt-looking templates after a major macOS upgrade.

**(Re)load mechanism.** Like `tg_ctl`, rig **(re)loads** the agent via `launchctl bootout`/`bootstrap` in the per-user `gui/<uid>` domain. A clean `rig init` starts the daemon without a reboot, and a changed plist or script is picked up on the next `apply`.

**Dry-run seam.** `RIG_TCC_WATCH_DRY_RUN=1` writes the managed plist and watcher script into the configured (HOME-isolated) path but skips the live `launchctl bootstrap`/`bootout`. Tests and smoke runs therefore never touch the real launchd domain. The seam also engages automatically under a sandboxed HOME (rig-cli#116), as for every other launchd-backed daemon this repo provisions.

**Testing without a live TCC dialog.** `parse_observation` is a pure function over one `log stream --style ndjson` line. `TccStore` takes a path. The `run()` loop accepts an injectable `stream_factory`, `sleep`/`clock` and a `max_reconnects` bound. The unit suite replays log lines captured from a real Mac, covering prompts, denials and routine chatter. It never needs, or triggers, a real macOS permission prompt.

---

## `tgctl_reaper`

rig provisions the **tgctl-reaper periodic orphan sweep** — the durable fix for a real
2026-09-09 OOM incident (agent-tools#697): tg-cli's ctl-omp-acp / ctl-omp-reply integration
tests spawn throwaway `omp acp` children that, when a test run is interrupted, get reparented
to launchd (`ppid == 1`) and sit idle at 0% CPU consuming RSS until something reaps them. This
was previously a **manually-installed** LaunchAgent
(`agent-tools/ops/tgctl-omp-acp-reaper/install.sh`, run by hand per machine); it is now a
first-class `rig apply` provisioned target, reproducible across every machine and drift-checked
like `tg_ctl`/`tcc_watch`.

Unlike `tg_ctl`/`tcc_watch` (both `RunAtLoad` + `KeepAlive` resident daemons), this is a
**`StartInterval`** periodic sweep — launchd runs the sweeper, it exits, and launchd starts it
again `interval_seconds` later. Nothing stays resident between runs. **Default ON** (an absent
or empty `tgctl_reaper:` block still provisions it, mirroring `tg_ctl`/`tcc_watch`). This is a
**per-MACHINE** concern (one reaper per machine), so the block belongs in the **GLOBAL** layer
(`~/.config/rig/config.yaml`) — never a committed repo `rig.yaml`. **macOS-only** (launchd); off
darwin it is a no-op.

```yaml
tgctl_reaper:
  enabled: true                                   # provision the sweep (default true; false = off)
  interval_seconds: 900                            # seconds between sweeps (default 900 = 15 min)
  # everything below is auto-discovered per-machine — override only if non-standard:
  label: com.hyperide.tgctl-omp-acp-reaper         # launchd Label / plist filename stem (advanced)
  config_dir: ~/.config/rig/tgctl-reaper           # sweeper script + launchd logs
  python_bin: /usr/bin/python3                     # the python3 binary (default: `which python3` → system fallback)
```

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `enabled` | bool | `true` | `false` = don't touch the reaper at all — rig emits no action |
| `interval_seconds` | int | `900` | seconds between sweeps (launchd `StartInterval`); must be >= 1 |
| `label` | str | `com.hyperide.tgctl-omp-acp-reaper` | launchd Label / plist filename stem — **reuses the manually-installed predecessor's exact label** so a re-apply on a machine that already ran `install.sh` by hand converges onto the SAME service instead of installing a duplicate |
| `config_dir` | str | `~/.config/rig/tgctl-reaper` | where the sweeper script and the launchd out/err logs live |
| `python_bin` | str | discovered | the python3 binary; default `which python3`, else the first of `/usr/bin/python3`, `/opt/homebrew/bin/python3`, `/usr/local/bin/python3` |

**What rig writes.** `~/Library/LaunchAgents/com.hyperide.tgctl-omp-acp-reaper.plist` — a
`StartInterval` agent (`RunAtLoad: false`) that runs `python3 <config_dir>/reap.py` every
`interval_seconds` — plus the sweeper script itself, copied byte-for-byte from
`riglib/tgctl_reaper_daemon.py` (the SAME module the unit suite exercises directly), so the
deployed sweep and the tested reap logic can never drift apart.

**Safety predicates the sweep enforces** (ALL required, positive match, never absence — see
`riglib/tgctl_reaper_daemon.py`'s module docstring for the full rationale): `ppid == 1` (the
owning process is provably gone), elapsed >= 15 minutes since reparenting, `0.0` `%cpu` (fully
idle), an argv carrying a `/tgctl-<suffix>` temp-dir path token, AND an `omp acp` (token-boundary)
invocation. The temp-dir marker matches an enumerated set of tg-cli mkdtemp prefixes (kept in
sync with the predecessor script's `_TGCTL_MKDTEMP_PREFIXES`, which itself grew from the
original `tgctl-omp-(acp|reply)-` allowlist after four newer test-fixture prefixes leaked past
it undetected) — the marker alone still cannot over-match anything not already independently
proven orphaned, since every other predicate is unchanged and still required, and an
unenumerated `/tgctl-*` path (e.g. a worktree merely named `tgctl-...`) never matches at all.

**(Re)load mechanism.** Like `tg_ctl`/`tcc_watch`, rig **(re)loads** the agent via `launchctl
bootout`/`bootstrap` in the per-user `gui/<uid>` domain — a `StartInterval` agent still needs to
be LOADED for the timer to fire at all, even though nothing stays resident between runs. A clean
`rig init` starts the sweep without a reboot; a changed plist/script is picked up on the next
`apply`.

**Converging the manually-installed predecessor.** A machine that already ran
`agent-tools/ops/tgctl-omp-acp-reaper/install.sh` by hand has a plist under the SAME label but a
different script path/shape. `rig apply`'s normal backup-on-conflict path reconciles that
difference on first apply: the differing plist is backed up (timestamped), then rewritten to the
rig-rendered version.

**Dry-run seam.** `RIG_TGCTL_REAPER_DRY_RUN=1` writes the managed plist + sweeper script into the
configured (HOME-isolated) path but skips the live `launchctl bootstrap`/`bootout` mutation — so
tests/smoke never touch the real launchd domain. Also engages automatically under a sandboxed
HOME (rig-cli#116), same as every other launchd-backed daemon this repo provisions.

**Full drift/status parity with `tg_ctl`.** Unlike `tcc_watch`'s documented "known gap: no `rig
status` drift line yet", `tgctl_reaper` gets the SAME `rig status` treatment as `tg_ctl`: a
dedicated `[GLOBAL] tgctl-reaper orphan sweep: …` line reporting installed / drifted /
unsupported (off darwin), backed by a real `rig status` drift check (missing/modified on both
the plist and the sweeper script, plus a not-loaded check) — not just "apply reconciles it".

---

## `sysmon`

rig provisions the **sysmon resource monitor** (rig-cli#622), a resident daemon that catches a
resource-exhaustion incident before the kernel does. It exists because of three crashes in 48 hours
on one Mac:

- 2026-09-27: a WindowServer hang.
- 2026-09-27: a kernel panic from a tg-ctl process leak that pushed the memory compressor to its
  limit and exhausted swap.
- 2026-09-29 12:47: a WindowServer-watchdog panic at **95.7 % wired memory**. Thirteen concurrent
  `whisper-cli` runs each loaded a 1.6 GB model into Metal buffers, while
  `kern.memorystatus_vm_pressure_level` still read "normal".

There was also a near-miss the same afternoon: load ~19 on 14 CPUs, swap 3.8/5 GB, and an agent's
`lldb` raised a SecurityAgent `system.privilege.taskport.debug` password dialog that hung htop,
`sample`, `footprint` and `lsof`.

**Default ON** (an absent or empty `sysmon:` block still provisions it). It is **GLOBAL-only**: one
monitor per machine, configured in `~/.config/rig/config.yaml`. It runs as a `RunAtLoad`/`KeepAlive`
LaunchAgent on macOS and as a systemd `--user` unit on Linux.

```yaml
sysmon:
  enabled: true
  auto_remediate: false            # hand a critical incident to a coding agent (default OFF)
  sample_interval_seconds: 10      # in-process sample cadence
  census_interval_seconds: 60      # ps census cadence (also run at once on any warn)
  snapshot_interval_seconds: 300   # live omp-session snapshot cadence
  events: true                     # macOS unified-log event stream
  thresholds:                      # any subset; the rest keep their defaults
    wired_pct: { warn: 75, critical: 85 }
    reclaimable_gb: { warn: 2, critical: 1 }      # lower is worse
    data_used_pct: { warn: 90, critical: 95 }     # critical lists reclaimable candidates
  alert: { min_severity: critical, cooldown_minutes: 30, max_per_hour: 6, resolved: true, dry_run: false }
  heal: { enabled: true, notify_agent: true, throttle: true, kill_orphans: false, kill_owned: false,
          grace_seconds: 120, dry_run: false }        # every kill path is opt-in
  remediation: { agent: omp, cooldown_minutes: 60, dry_run: false }   # omp | claude-code | codex | opencode
  sessions: { restore_on_boot: false, tmux_session: main }
  gpu: { slots: 2, slot_dir: ~/.local/state/agent-tools/gpu-slots }   # default: env.vars.RIG_GPU_SLOTS / RIG_GPU_SLOT_DIR
  reclaim: { worktree_roots: [~/xp, ~/work], worktree_age_days: 7, max_candidates: 10,
             cache_dirs: [~/Library/Caches/review-cli/jobs, ~/.cache/review-cli/jobs] }   # report only
  # advanced: label (ai.hyperide.sysmon), config_dir (~/.config/rig/sysmon), python_bin (which python3)
```

| Threshold | warn | critical | Measured from |
|---|---|---|---|
| `load_per_cpu` | 1.25 | 2.0 | `getloadavg()` / CPUs |
| `swap_pct` | 80 | 95 | `vm.swapusage` (macOS grows swap in 1 GB files while disk allows — pair with `disk_free_gb`) |
| `wired_pct` | 75 | 85 | `host_statistics64` wired pages / `hw.memsize` |
| `reclaimable_gb` (lower is worse) | 2 | 1 | free + inactive + speculative + purgeable pages |
| `free_gb` (lower is worse) | 0.05 | 0.025 | free pages, vm_stat's "Pages free" (`free_count` − speculative; Linux `MemFree`) |
| `compressor_pct` | 70 | 90 | `vm.compressor_segment_pages_compressed` / `…_limit` |
| `disk_free_gb` (lower is worse) | 5 | 2 | free space on `/System/Volumes/VM` (swap growth) |
| `data_used_pct` | 90 | 95 | the data volume (`/System/Volumes/Data`, Linux `/`) as df's Capacity: Used / (Used + Available) |
| `procs_pct` | 60 | 80 | `proc_listallpids` / `kern.maxproc` |
| `storm_same_name` | 150 | 300 | processes of one user sharing one executable name |
| `proc_growth_per_min` | 200 | 500 | process-count change over the last 20–120 s |
| `display_delays_per_min` | 2 | 5 | WindowServer `Timer delayed by N s` log lines |
| `display_max_delay_s` | 1.5 | 3.0 | longest such delay in the last minute |
| `taskport_stuck_s` | 20 | 60 | age of a `sample`/`footprint`/`lldb`/`htop`/`lsof`/… while SecurityAgent is up |

`kern.memorystatus_vm_pressure_level` WARN/CRITICAL is also reported. More than `gpu.slots`
GPU-model processes (whisper, llama.cpp, mlx) is always critical. `gpu.slots` is the size of the
agent-tools#817 GPU-model slot pool; when rig's `env` block exports `RIG_GPU_SLOTS` /
`RIG_GPU_SLOT_DIR`, the monitor follows them. Watchdogd WindowServer check-in trouble is always
critical.

**Free pages** have their own line because the reclaimable sum hides the moment the kernel loses
the race. The defaults are calibrated on a 24 GB Mac with 16 KB pages: the pageout daemon keeps
free pages near `vm_page_free_target` (4000 pages, 62.5 MB) by reclaiming inactive pages, so a low
free count alone is normal load. Ten minutes of 50-ms reads at normal load had a median of 17,974
pages and 1 % under 3,635, and the 2026-09-29 panic still showed 4,610 (its signal was the ~2,000
inactive pages left, which `reclaimable_gb` catches). Staying below the target means nothing is
left to reclaim: `free_gb` warns at 0.05 GB (3,277 pages, just under `vm_page_free_min`, 3,500)
and is critical at 0.025 GB (1,638 pages, under half of `free_min` and 728 pages above
`vm_page_free_reserved`, 910, where only VM-privileged threads may still allocate). Normal load
also dips under both lines for a moment (25 of those 10,616 reads, one at 911 pages), so a line
counts only after it held for 3 consecutive samples (20 s at the default cadence).

**A full data volume** warns at 90 % and is critical at 95 % (df's Capacity; APFS volumes share
their container's free space, so the used share of the volume's own size would under-read). Swap
grows on the same disk. A critical `data_volume` alert lists **reclaimable candidates**, largest
first, capped at `reclaim.max_candidates`:

- rig worktrees (`<root>/<repo>/.worktrees/<name>` under each `reclaim.worktree_roots` dev root)
  that neither the directory nor its git `index`/`HEAD`/`logs/HEAD` changed for
  `reclaim.worktree_age_days`;
- review-cli job caches (`reclaim.cache_dirs`);
- container images no container uses, from `container system df` (Apple) and `docker system df`,
  only when the binary is on PATH.

Sizes come from `du -sk -x` and those `system df` commands, each bounded to 10 s and 30 s in all
(what the budget leaves unmeasured is counted). The scan runs at most once per alert cooldown and
is kept in the incident as `reclaimable`. **It is a report only: sysmon never deletes, moves or
prunes anything**, and the alert says so. `rig sysmon simulate --scenario disk` renders it in
dry-run against this machine.

**How it watches (event-driven where macOS publishes events).**

- **Events:** the daemon `select()`s on one `log stream --style ndjson` child. Its predicate covers
  WindowServer `Timer delayed`, watchdogd WindowServer lines and authd `system.privilege.taskport*`,
  so an event is evaluated the moment it arrives. On 2026-09-29 the display-freeze precursor started
  26 minutes before the panic.
- **Sampling:** memory, swap, load and process counts have no change events, so they are sampled
  in-process on the select timeout (ctypes `host_statistics64`/`sysctlbyname`/`proc_listallpids`),
  with no fork.
- **Census:** a `ps` census runs every `census_interval_seconds`; while a finding is at warning it
  runs every 30 s, and while one is critical on every sample. A log event triggers one too, but
  a burst of events reuses a census younger than 10 s. Each census also records every process's
  start time; the heal ladder's pid-reuse guard compares against that snapshot.
- **What the daemon never calls:** `lsof`, `sample`, `footprint`, a debugger, or anything else
  that needs `task_for_pid`. Per-process detail comes from `libproc`, which keeps working while a
  SecurityAgent prompt blocks taskgated.

**Attribution.** Every offending process is walked up its ppid chain to the owning agent host
(omp, claude, codex or opencode), that host's exact session transcript (the `.jsonl` it holds
open), and the tmux pane (`list-panes pane_pid`) or cmux surface it runs in (`cmux:<workspace>/<surface>`,
from the `CMUX_WORKSPACE_ID`/`CMUX_SURFACE_ID` cmux exports into every surface).

An **orphan** needs positive evidence: an earlier census saw the tree under an agent host (pid and
start time), and that host has died since. A host missing from one census is checked live before
it counts as dead. A tree that is merely reparented to launchd (ppid 1), such as an agent-browser
daemon, a `nohup`'d job of a live agent or rig's ship-loop, is not an orphan. The evidence lives
in the daemon's memory only, so a tree whose host died before the daemon (re)started is never an
orphan: it is throttled and alerted on, never killed.

**Alerts.** Alerts go through `tg --tag problem --subagent sysmon` with an HTML body that lists the
finding, its numbers and who owns the offenders. They are deduplicated per finding key:

- a key alerts when it first reaches `min_severity`, when it escalates, and otherwise at most once
  per cooldown;
- one message per tick carries every due finding;
- at most `max_per_hour` messages go out per hour;
- one "resolved" line is sent after a key stays clear for 5 minutes.

The dedup state lives in `state/memory.json`, so a restart never re-sends.

**Heal ladder (safe by default).**

1. **warning:** notify the owning agent. Delivery tries, in order:
   - `tg-ctl omp-live send` when that omp host is registered;
   - typing the note into the agent's tmux pane (`send-keys`) or cmux surface (`cmux send`), as one
     line followed by Enter. The target is resolved live from the host pid, and the note is typed
     only when the screen shows the agent's input box EMPTY. Typed user text, a menu, copy mode or
     an unknown screen refuse. Enter is pressed only once the box shows the whole note and nothing
     else (a note still rendering is waited for); if a human types meanwhile, or the box scrolled
     or cut the note short, Enter is withheld;
   - the harness Stop-hook inbox (`tg-ctl inbox queue`, agents outside tmux).

   The note names the offending pids, their process names and RSS, never their command lines (a
   process chooses its own argv, and the note lands in an agent's prompt). A refused note is tried
   again on the next ticks, at most 3 times per finding and agent (a new process on a reused host
   pid is a new agent). Throttles and restores run before any note is typed. When every channel
   refuses, the Telegram alert is the only signal.
2. **critical:** also throttle every unprotected offender into the OS background band
   (`taskpolicy -b` on macOS, the idle I/O class on Linux). A process that was already in the
   background band is left as it was. The throttle is **restored** when no finding blames the
   process any more: the finding cleared, the process left it, `heal.enabled` was turned off, or
   the daemon stops. A throttle that fails is retried on the next ticks (3 attempts). Every
   throttle is recorded, and written to disk right after the tick's throttle burst, so a daemon
   stopped or killed later in the tick still restores it. A failed restore is retried on the
   next 3 ticks, then kept until the process is blamed again or the daemon stops, and retried
   then. A kill that did not happen (dry-run, refused) keeps the throttle to restore.
   The ladder never renices, because an unprivileged process can never lower a nice value again.
3. **Still critical after `grace_seconds`:** SIGTERM, then SIGKILL, only where opted in:
   `kill_orphans: true` for proven orphans, `kill_owned: true` for an agent's own offenders. Both
   default to `false`.

Before every throttle, restore, signal or typed note, the pid's start time is compared with the one
the census recorded, so a reused pid is never hit; a pid whose start time is unknown is never
touched.

**Never touched:** user-interactive processes (a foreground terminal job no agent started, an
interactive shell), agent hosts, terminals/tmux/cmux, GUI apps, another user's processes, system
binaries and launchd jobs.

**Incidents.** The first critical finding opens `state/incidents/<id>/incident.json`. It holds the
findings, the sample, the live omp sessions at that moment, and the alert/heal/remediation/restore
logs. The incident closes after 5 quiet minutes.

**Auto-remediation (`auto_remediate: true`, default OFF).** On a critical incident (at most once per
`remediation.cooldown_minutes`) the daemon writes `remediation-brief.md` into the incident dir and
launches a detached coding-agent session through the rig-detached launcher family
(`~/.agents/skills/rig-detached-{omp,codex,opencode}`). claude-code has no launcher in the family,
so the same detached contract runs inline. The brief tells the agent to:

1. find and fix the root cause in its owning repo (killing processes is not a fix);
2. resume the interrupted sessions with `rig sysmon restore --incident <id>`;
3. report through `tg report`;
4. write the handoff file named in the brief.

**Session restore.** Every `snapshot_interval_seconds` the daemon records the live interactive omp
sessions in `state/sessions.json` (pid, start time, cwd, exact transcript, pane). At start it
compares the previous heartbeat with the boot session: a boot change without a clean-shutdown
marker is a crash. The crash opens a `post-crash` incident that carries that snapshot and the newest
`.panic` excerpt, and then:

- it alerts;
- it hands the incident to remediation when enabled;
- with `sessions.restore_on_boot`, it restores the sessions itself.

`rig sysmon restore [--incident ID] [--dry-run] [--tmux-socket S]` resumes each interrupted session
in a **new** tmux window, running `omp --resume <transcript> "<explanation of the incident>"`. It
refuses any session a live omp still holds open, so there is never a second writer (rig-cli#619),
and it fails closed when liveness cannot be verified.

**Commands.** `rig sysmon status` shows daemon health plus a live reading; it exits 3 when the
daemon is not running. The other commands:

- `rig sysmon sample`: one reading plus every detector, with no side effects.
- `rig sysmon test-alert`: the forced-condition smoke test. It sends ONE labelled TEST message with
  the 2026-09-27 leak, compressor and swap pattern.
- `rig sysmon simulate --scenario wired|storm|display|taskport`: runs the production evaluation path
  on synthetic findings. Alerts and heal are forced to dry-run; remediation follows the config.
- `rig sysmon incidents [--show ID]`.
- `rig sysmon restore`.

All of them forward to the same package the daemon runs.

**What rig writes.**

- `<config_dir>/sysmon_daemon/*.py`: `riglib/sysmon_daemon/` copied byte for byte.
- `<config_dir>/sysmon.json`: the block merged over the daemon defaults. Both files are generated,
  so a hand edit is drift that apply overwrites.
- `~/Library/LaunchAgents/ai.hyperide.sysmon.plist` (macOS) or
  `~/.config/systemd/user/ai.hyperide.sysmon.service` (Linux), backup-noted per `on_conflict`.

Any change reloads the service with `launchctl bootout`/`bootstrap` in `gui/<uid>`, or with
`systemctl --user daemon-reload` + `enable` + `restart`. The daemon keeps its state in files and
has no user session riding on it, so a reload is safe. `RIG_SYSMON_DRY_RUN=1`, or a sandboxed HOME
(rig-cli#116), writes the artifacts and skips the live reload.

**`rig status`.** It prints `[GLOBAL] sysmon resource monitor: running (heartbeat Ns ago)` with the
last reading, the current findings and any open incident. Drift covers:

- missing/modified package, config or service files;
- a service that is not loaded;
- a loaded service whose daemon is `stale` or `stopped`: its heartbeat is older than 3 sample
  intervals + 30 s, or it wrote a clean-shutdown marker.

A monitor that does not tick never reads as healthy.

---

## `task`

**task-cli's per-repo ticket-tracker routing** (CTO decision #4136.4: hyperide → Linear/HYP,
every other repo → GitHub Issues by default). Unlike every other block above, rig itself
**writes nothing to disk** for `task:` — task-cli reads this block **directly** at runtime
(`tasklib.config.rig_task_overlay`, in the [task-cli
repo](https://github.com/alex-mextner/task-cli)), with LOWER precedence than a repo's own
native `task.yaml`. A repo that already keeps a `task.yaml` is unaffected by anything set here.
rig only validates the key set + enum values so a typo fails fast at `rig status`/`rig apply`
time, with rig's own error format, instead of only surfacing later as a task-cli error.

```yaml
task:
  backend: linear          # github-issues (default) | linear
  team: HYP                # flat shorthand for linear.team
  attachment_mode: link    # link (never re-host a local file) | native (default; real upload)
  # equivalently, nested form (both may be combined; task-cli rejects a genuine collision):
  # linear:
  #   team: HYP
  #   project: ""           # optional Linear project id to scope create/list/search to
  #   attachment_mode: link
  # github:
  #   repo: owner/name       # or 'auto' to detect from the git remote
```

All keys below (flat and nested, scalar and container) tolerate an explicit `null` the same as
absent — rig's `_validate_task` guards every one of them with `is not None`, an early `None`
return (the three enum leaves: `backend`, `attachment_mode`, `linear.attachment_mode`), or a
null-specific `{} if x is None else x` normalization (the two nested sections; not a plain
`or {}`, which would also swallow other falsy-but-invalid values), matching task-cli's own
tolerant reads. The one exception is the top-level `task:` header itself, which stays rejected
(matches every other
block's convention).

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `backend` | str or null | `github-issues` | the tracker backend: `github-issues` or `linear` |
| `team` | str or null | — | flat shorthand for `linear.team` (e.g. `HYP`) |
| `project` | str or null | — | flat shorthand for `linear.project` |
| `repo` | str or null | — | flat shorthand for `github.repo` (`owner/name`, or `auto`) |
| `attachment_mode` | str or null | `native` (task-cli's own default) | how a screenshot/file gets attached to a Linear ticket — see below |
| `github.repo` | str or null | — | nested form of the `repo` shorthand |
| `linear.team` | str or null | — | nested form of the `team` shorthand |
| `linear.project` | str or null | — | nested form of the `project` shorthand |
| `linear.attachment_mode` | str or null | — | nested form of the `attachment_mode` shorthand |
| `github`, `linear` | object or null | — | nested sections; an explicit `null` (a bare `github:`/`linear:` header) is tolerated the same as absent |
| `projects` | array or null | — | task-cli's cross-project registry (a LIST of entries, not a mapping — see task-cli's `tasklib/projects.py`); rig only checks it's a list; an explicit `null` is tolerated the same as absent |
| `enforce`, `classify`, `session` | object or null | — | task-cli's own nested shapes (each with their own defaults); rig only checks they're mappings — task-cli's own `tasklib.config.validate` is the runtime authority on their contents; an explicit `null` is tolerated the same as absent |

**`attachment_mode` — `link` vs `native` (tg#11652).** How task-cli attaches a screenshot/file
to a Linear ticket:
- **`native`** (task-cli's own default): uploads the bytes through Linear's authenticated
  `fileUpload` + signed-URL PUT + `attachmentCreate` API sequence, so the ticket carries a real
  Linear attachment object with an inline preview.
- **`link`**: never uploads a local file — only registers an already-hosted URL (e.g. a GitHub
  PR asset, or a PR's `#screenshots` anchor) as an external-link attachment. hyperide pins this
  mode (HYP-1248): a raw pasted-image `uploads.linear.app` URL is session-scoped and 401s for a
  non-browser viewer, so hyperide's mitigation is to link out to the PR instead of embedding an
  image. A bare local path in this mode has nothing to link to and the attach fails cleanly
  (best-effort — the local ref stays visible in the ticket body regardless).

---

||||||| parent of a530cf2 (feat(task): add task.code_prefix, reconciled into .ship-config)
## `task`

**task-cli's per-repo ticket-tracker routing** (CTO decision #4136.4: hyperide → Linear/HYP,
every other repo → GitHub Issues by default). Unlike every other block above, rig itself
**writes nothing to disk** for `task:` — task-cli reads this block **directly** at runtime
(`tasklib.config.rig_task_overlay`, in the [task-cli
repo](https://github.com/alex-mextner/task-cli)), with LOWER precedence than a repo's own
native `task.yaml`. A repo that already keeps a `task.yaml` is unaffected by anything set here.
rig only validates the key set + enum values so a typo fails fast at `rig status`/`rig apply`
time, with rig's own error format, instead of only surfacing later as a task-cli error.

```yaml
task:
  backend: linear          # github-issues (default) | linear
  team: HYP                # flat shorthand for linear.team
  attachment_mode: link    # link (never re-host a local file) | native (default; real upload)
  # equivalently, nested form (both may be combined; task-cli rejects a genuine collision):
  # linear:
  #   team: HYP
  #   project: ""           # optional Linear project id to scope create/list/search to
  #   attachment_mode: link
  # github:
  #   repo: owner/name       # or 'auto' to detect from the git remote
```

All keys below (flat and nested, scalar and container) tolerate an explicit `null` the same as
absent — rig's `_validate_task` guards every one of them with `is not None`, an early `None`
return (the three enum leaves: `backend`, `attachment_mode`, `linear.attachment_mode`), or a
null-specific `{} if x is None else x` normalization (the two nested sections; not a plain
`or {}`, which would also swallow other falsy-but-invalid values), matching task-cli's own
tolerant reads. The one exception is the top-level `task:` header itself, which stays rejected
(matches every other
block's convention).

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `backend` | str or null | `github-issues` | the tracker backend: `github-issues` or `linear` |
| `team` | str or null | — | flat shorthand for `linear.team` (e.g. `HYP`) |
| `project` | str or null | — | flat shorthand for `linear.project` |
| `repo` | str or null | — | flat shorthand for `github.repo` (`owner/name`, or `auto`) |
| `attachment_mode` | str or null | `native` (task-cli's own default) | how a screenshot/file gets attached to a Linear ticket — see below |
| `github.repo` | str or null | — | nested form of the `repo` shorthand |
| `linear.team` | str or null | — | nested form of the `team` shorthand |
| `linear.project` | str or null | — | nested form of the `project` shorthand |
| `linear.attachment_mode` | str or null | — | nested form of the `attachment_mode` shorthand |
| `github`, `linear` | object or null | — | nested sections; an explicit `null` (a bare `github:`/`linear:` header) is tolerated the same as absent |
| `projects` | array or null | — | task-cli's cross-project registry (a LIST of entries, not a mapping — see task-cli's `tasklib/projects.py`); rig only checks it's a list; an explicit `null` is tolerated the same as absent |
| `enforce`, `classify`, `session` | object or null | — | task-cli's own nested shapes (each with their own defaults); rig only checks they're mappings — task-cli's own `tasklib.config.validate` is the runtime authority on their contents; an explicit `null` is tolerated the same as absent |

**`attachment_mode` — `link` vs `native` (tg#11652).** How task-cli attaches a screenshot/file
to a Linear ticket:
- **`native`** (task-cli's own default): uploads the bytes through Linear's authenticated
  `fileUpload` + signed-URL PUT + `attachmentCreate` API sequence, so the ticket carries a real
  Linear attachment object with an inline preview.
- **`link`**: never uploads a local file — only registers an already-hosted URL (e.g. a GitHub
  PR asset, or a PR's `#screenshots` anchor) as an external-link attachment. hyperide pins this
  mode (HYP-1248): a raw pasted-image `uploads.linear.app` URL is session-scoped and 401s for a
  non-browser viewer, so hyperide's mitigation is to link out to the PR instead of embedding an
  image. A bare local path in this mode has nothing to link to and the attach fails cleanly
  (best-effort — the local ref stays visible in the ticket body regardless).

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

## `tracked_files`

Some files rig owns are **committed** to the repo: `rig.yaml` itself, task-cli's `task.yaml`, the
`.ship-config` rig reconciles. Writing such a file into whichever checkout `rig apply` runs in leaves
that checkout dirty — and when the same change reaches every worktree of a repo, the identical
uncommitted edit piles up in dozens of worktrees while never reaching the default branch (a
task-routing migration once left about 200 worktrees dirty this way). This block makes `rig apply`
**land** those files once on the default branch instead. Block PRESENCE opts in; `enabled: false`
opts out. `tracked_files: {}` takes every default; a bare `tracked_files:` (null) is rejected rather
than read as either.

**Global config only.** `tracked_files` runs the ship command and pushes branches with your
credentials, and it decides which files leave the machine. A committed repo `rig.yaml` — itself a
file this block harvests — must choose neither, so `config.load` rejects the block from any
non-global layer, and `rig config set tracked_files.…` writes the global file.

```yaml
# ~/.config/rig/config.yaml
tracked_files:
  paths: [rig.yaml, task.yaml, .ship-config]   # default shown; git pathspec globs
  land: ship                 # ship (PR + gated ship in the background) | pr (PR only)
  harvest_min_worktrees: 2   # land an uncommitted edit only when this many worktrees agree on it
  normalize_worktrees: true
  normalize_min_age_minutes: 120
  ship:
    task_code: UB-3          # ticket for ship's review-quorum + acceptance gates
    task_config: ~/.config/task/tasks-ub.yaml    # optional: ship's --task-config for that ticket
    args: [--no-version-bump-ok, "config-only: rig-owned tracked files"]
    command: gh ship         # default shown
  review:
    models: omp:anthropic/claude-opus-4-8,oc:zai/glm-5.2,oc:xai/grok-4.6   # optional; review-cli's pool by default
```

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `enabled` | bool | `true` | land tracked-owned files (block presence opts in) |
| `paths` | array of str | `[rig.yaml, task.yaml, .ship-config]` | repo-relative files rig lands; must stay inside the repo. Globs `*`, `**` and `?` follow git's `:(glob)` pathspec rules: `*` stays in one directory (`*.yml` is the repo root only), `**` crosses them; classes and braces are rejected. An entry names files: one naming a directory matches nothing, and `rig status` says so |
| `land` | `ship` \| `pr` | `ship` | `ship` opens the PR and runs the gated ship in the background; `pr` opens the PR only |
| `harvest_min_worktrees` | int ≥ 1 | `2` | an uncommitted edit lands only when at least this many worktrees carry it byte-identically |
| `normalize_worktrees` | bool | `true` | check a path out again in every worktree whose uncommitted copy equals the default branch |
| `normalize_min_age_minutes` | int ≥ 0 | `120` | leave a copy alone while it changed within this many minutes |
| `ship.task_code` | str | — | the ticket ship's review-quorum and acceptance gates read (`REVIEW_TASK_CODE`); without it the PR opens but ship is not started |
| `ship.task_config` | str | — | task-cli config ship passes as `--task-config` for that ticket |
| `ship.args` | array of str | `[]` | extra `gh ship` arguments, verbatim |
| `ship.command` | str | `gh ship` | the ship command (the gated merge path) |
| `review.models` | str | review-cli's pool | seats for the review recorded on the staged landing diff |

**What rig does with it.**

1. **Divert.** The plan builder moves every action whose target is a listed file — and rig's
   `.ship-config` reconcile (the `SHIP_TASK_CODE_PREFIX` line from `task.code_prefix`) — out of
   the plan into `plan.diverted`. No plan consumer — `rig apply`, `rig config set`, `rig init`,
   config-web — writes such a file into a working checkout, and `rig status` does not report it
   as drift there; it lists it as a pending merge instead. An installer that targets a whole
   directory (the `ci` workflows under `.github/workflows`) is not held back, even when a glob
   matches the files it writes: those copies are still harvested and normalized below.
2. **Harvest.** `rig apply` scans every worktree of the repo, the primary checkout included, for an
   uncommitted edit of a listed path whose committed copy (`HEAD`) still equals
   `origin/<default>`'s — a file neither has yet counts too (a new `rig.yaml`, say). Byte-identical
   edits are grouped; the group at least
   `harvest_min_worktrees` worktrees agree on becomes the landing content. When two different edits
   both reach the threshold, that path is a **conflict** and nothing lands for it. Only regular
   files count: a deleted copy or a symlink (which could point at anything, a key included) is
   never harvested or normalized. A file rig generates from other config (`.ship-config`, from
   `task.code_prefix`) is never harvested either — ship reads it, so only rig's own reconcile
   writes it on the landing branch.
3. **Land** (`rig apply commit` only). In one non-interactive batch: fetch `origin/<default>`,
   create a locked worktree there — always `<primary checkout>/.worktrees/rig-tracked-files`,
   whichever checkout `rig apply` runs in — write the harvested content,
   rebuild the plan in that worktree (so it reads the landed `rig.yaml`) and run the held-back
   actions there, record a review of the staged diff (`review diff --staged --task <task_code>`,
   when review-cli is installed), commit, push a fresh `rig/tracked-files-<hash>-<utc>` branch, and
   open the PR (`gh pr create` for a GitHub or `github.*` Enterprise origin, `fj pr create` for any other forge). With `land: ship` and a
   `task_code`, the gated ship starts in the background (`python -m riglib.tracked_files
   ship-loop`). It retries while CI is pending or the review-dwell window is open, merges the
   default branch in (never a force-push) when the PR falls BEHIND, stops on any other refusal,
   and gives up after 8 hours. Every attempt goes to the landing log. A failure before the push
   leaves nothing behind; a leftover landing worktree from an earlier run is cleaned up first.
4. **Normalize.** Every `rig apply commit` (and the background lander right after its merge)
   checks each listed path out again in every worktree whose uncommitted copy equals
   `origin/<default>`: a modified copy is restored to `HEAD`, an untracked new file is removed. It
   skips locked worktrees, worktrees a live agent sits in (checked at that moment), and copies
   changed within `normalize_min_age_minutes`. Anything that differs from the default branch is
   left alone. A worktree whose branch predates the merge gets the change through its next merge
   of the default branch.

**Preview and dry-run.** `rig apply` (= `rig apply info`), `rig apply commit --dry-run` and
`rig status` print the pending merges under `tracked_files — pending merges to the default
branch:` and change nothing. For example:

```
tracked_files — pending merges to the default branch:
  · land rig.yaml (7 lines) from 33 worktree(s) → PR against origin/main, then gated ship
  · normalize 4 worktree(s) whose copy already equals origin/main
```

`rig apply commit --only <categories>` lands only when `tracked_files` is one of them; the held-back
actions stay out of the checkout either way. A repo with no origin default branch (a local-only
repo) has nowhere to land, so there nothing is held back: rig writes the files into the checkout
as without the block, and says so in the plan notes.

**State.** One JSON file per repo under `$XDG_STATE_HOME/rig/tracked-files/` (default
`~/.local/state/rig/tracked-files/`), keyed by the repo's shared git dir so every checkout of it
uses the same file, with the landing log and a lock file beside it. The lock (a kernel file lock, released
when its holder exits, so it never goes stale) lets one landing — or one post-merge cleanup — run per repo at a
time; a second `rig apply commit` meanwhile reports the landing in flight and exits 0. The
background lander holds a second lock (`<state>.lander`) for its whole life and records its own
pid, so even with the state file deleted by hand no new landing removes the worktree it works in.
While an earlier landing's PR may still be open — shipping, or stopped with
`ship-refused`/`timed-out`/`ship-stopped` — `rig apply commit` asks the forge (`gh pr view`,
`fj pr view`), reports that PR with its last refusal, and opens no second one. If the forge cannot
answer, the PR number was never read, or a run died between pushing its branch and opening the PR
(a checkpoint is written before the push), it also opens none and says so; a branch whose PR could
not be opened is deleted from the forge again. Once `origin/<default>`
carries the landed content (however it merged) or the PR is closed — and its background lander has exited — a new landing may start. A lander that finds a newer landing in the state file stops without touching it.
Previews and `rig status` read this state but never write it.

**Safety checks.** Every comparison uses the blob git would store (`git hash-object --path`), so
`core.autocrlf` and clean filters do not split identical copies. A copy whose index holds something
of its own (staged content that is neither `HEAD`'s nor the default branch's) is never reset, and
each copy is re-checked right before it is reset, so an edit made after the scan is kept. Every git
call drops a leaked `GIT_DIR`/`GIT_WORK_TREE`/`GIT_CONFIG_*` environment (as `rig` does for its
core.bare check), so a hook or wrapper cannot redirect a commit or push to another repo. Transport
settings (`GIT_SSH_COMMAND`, `GIT_ASKPASS`, a proxy) are yours and pass through, as for any push.
The landing and lander locks are kernel file locks (`flock`, or `msvcrt.locking` on Windows), so
neither trusts a pid.

**Never** does it force-push, merge outside the ship gate, or touch a path outside `paths`.

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


## Machine-owned external PR review policy

The same Rig YAML schema is used for repository `rig.yaml` and the machine file
`~/.config/rig/config.yaml` (`$XDG_CONFIG_HOME/rig/config.yaml`). These two policy
keys are **machine-only**, not repository-overridable merge authority:

```yaml
ci:
  items:
    ship:
      require_external_review: false
      external_review_owners:
        - alex-mextner
```

The canonical `agent-tools/ci/ship/ship.sh` reads this file on every invocation.
It checks the **actual target repository** reported by `gh`, not the current
checkout's owner. Only an exact listed owner is covered; no wildcard or empty
list disables anything. Omission, `true`, invalid YAML/types, unsafe file
permissions or an unreadable policy leave this gate enabled. The file must be
owned by the invoking user and not group/world writable; it cannot be a symlink
or reside inside the shipping checkout. Decisions are recorded in ship's audit.

This controls only the extra requirement for a GitHub-side PR review. It does
not disable local tests, CI-equivalent checks, code-review quorum, unresolved
review-thread checks, secret scanning or GitHub permissions. Existing trusted
base-branch `.ship-config` opt-outs remain compatible. An ambient
`SHIP_EXTERNAL_REVIEW=0` variable is still not policy. `rig setup` and config-web
route these keys to the Global layer; repo-local/explicit overrides are rejected.

Set the boolean with `rig config set ci.items.ship.require_external_review false
--global --no-apply` after configuring the owner list in the machine file. No
broad `rig apply` is needed for runtime consumption, and no per-PR Telegram
permission request is required once the explicit machine policy is valid.

---

## `task` — Workspace task routing (UB-23)

Additions on top of the existing `task` block above (`backend`/`team`/`project`/`repo`/
`attachment_mode`, nested `github:`/`linear:`, `code_prefix`):

- **`backend: tasks`** — a third tracker backend value (alongside `github-issues` and
  `linear`): the native Tasks-app (HTTPS REST).
- **`task.tasks` block** — coordinates for the `tasks` backend, used when
  `backend: tasks`:
  - `task.tasks.url` — the Tasks-app base URL, e.g. `https://tasks-ultrabricks.vercel.app`.
    It must be a bare `https` origin — no userinfo, query, fragment or path (task-cli appends
    `/api/v1` itself and sends its bearer token there) — the same rule task-cli's tasks
    backend enforces. A rejected value is never echoed back.
  - `task.tasks.team` — the Tasks-app team/workspace key, e.g. `UB`, `HYP`.
  - `task.tasks.attachment_mode` — `native` (upload through the Tasks-app API) or `link`
    (register an already-hosted URL as-is), mirroring `linear.attachment_mode`.

All three are also registered as wizard options (`riglib/schema.py`'s `task` Area,
alongside the existing `task.code_prefix` entry) with `rig setup`/`rig config set`.

```yaml
task:
  backend: tasks
  tasks:
    url: https://tasks-ultrabricks.vercel.app
    team: UB
    attachment_mode: native
```

### Ancestor `task`-block inheritance

A folder default (e.g. `~/xp/rig.yaml`, `~/work/rig.yaml`) can declare a `task:` block
that every repo nested underneath inherits, so routing config doesn't need repeating per
repo:

```yaml
# ~/xp/rig.yaml — a folder default for every repo under ~/xp
task:
  backend: tasks
  tasks:
    url: https://tasks-ultrabricks.vercel.app
    team: UB
```

```yaml
# ~/xp/some-project/rig.yaml — inherits the backend/url/team above automatically;
# only overrides what differs for this one repo
task:
  team: HYP
```

Precedence: `global (~/.config/rig/config.yaml) < ancestor task blocks (outermost
first, nearest ancestor last) < repo rig.yaml`, merged per-KEY (not whole-block
replacement) — an ancestor overriding `team` while leaving `backend` unset still lets a
global `backend` value survive. Only the `task` key is ever read from an ancestor's
`rig.yaml`; no other section (harness, permissions, hooks, CI, mode) leaks from an
ancestor into a descendant repo. `code_prefix` is repo-unique ship metadata, not
routing — it is stripped from every ancestor's contribution before merging and is never
itself inherited.

Ancestor resolution excludes the repo itself. Inside `$HOME`, the walk goes up to and
including `$HOME` and never above it (the intended `~/xp`/`~/work` folder-default
shape). Outside `$HOME`, the walk covers every ancestor directory except the filesystem
root. A worktree under `<repo>/.worktrees/<name>` has the primary repo as one of its
ancestors, so it inherits the primary repo's `task` block the same way any other
descendant does. Each ancestor's `task` block is independently validated (the same
`_validate_task` used for the global/repo layers), so a malformed ancestor config fails
closed, naming that ancestor's file. Disable the whole mechanism with
`riglib.config.load(..., include_ancestors=False)`.

**Only trusted ancestors are read.** An ancestor `rig.yaml` — and the directory holding it —
must be owned by the current user and not group/world-writable, or it is skipped (logged at
WARNING). This is task-cli's `_trusted_ancestor` rule, so both tools read the same ancestors:
outside `$HOME` the walk passes shared directories such as `/tmp`, and a `rig.yaml` another
local user planted there must not retarget your tickets. Windows skips the check. A filesystem
that reports every file as `0777` or root-owned (WSL `drvfs` under `/mnt/c` without the
`metadata` mount option, some NFS/SMB mounts) fails it for every ancestor; the WARNING names the
file or directory, and the fix is a mount that keeps real ownership or a repo-level `task:` block.

**An ancestor alone is not a declared config.** An ancestor contributes only `task` routing,
which `rig apply` does not consume, so a directory whose only layer is an ancestor still gets
`rig apply`'s "no rig.yaml found" refusal (and config-web's), instead of applying built-in
defaults (`LoadedConfig.has_declared_config`).

**`include_repo=False` stays GLOBAL-only.** A caller that excludes the repo scope
(`load(..., include_repo=False)` — e.g. a global-config-only read) never sees ancestor
task routing leak in either: ancestors are repo-scoped folder defaults, not global
config, so the ancestor overlay is gated by `include_repo` in addition to
`include_ancestors`. The global layer itself still loads normally in this mode.

**Provenance (`LoadedConfig.layers`).** Every ancestor file that actually contributed to
the merged `task` block (i.e. whose contribution was non-empty after stripping
`code_prefix`) is listed in `LoadedConfig.layers` as `ancestor:<path>`, positioned
between the `global:<path>` and `repo:<path>` entries — matching the `global <
ancestors < repo` cascade order, outermost ancestor first. An ancestor consulted but
contributing nothing (no `rig.yaml`, no `task:` key, or a `task:` block that only set
`code_prefix`) is not listed.

### `backend: forgejo` — native Forgejo issues (UB-25)

A fourth tracker backend value: native Forgejo issues on a self-hosted instance (e.g.
`git.hyperide.ai`). This is a real issue backend, not an alias of `github-issues`. The canonical
name is exactly `forgejo`; there is no `fj` alias. Coordinates live under `task.forgejo`:

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `forgejo.url` | str or null | — | the Forgejo instance base URL, e.g. `https://git.hyperide.ai`. Must be `https://` with a host (and a numeric port, if any), no whitespace, no query/fragment, no `@` or `%` (so no embedded `user:token@` credentials, plain or percent-escaped), and must NOT end in `/api/v1` or `/`. A rejection never echoes the value, since a malformed base URL is where a pasted token tends to hide. Why the suffixes matter: task-cli derives the API base by appending `/api/v1`, so either suffix would produce a doubled path and is rejected. |
| `forgejo.repo` | str or null | — | this repo's issue store as `owner/name` (e.g. `ultrabricks/rig-cli`), or `auto` to resolve it from the repo's own git `origin` at runtime. Anything else (`owner/`, `a/b/c`, any whitespace including a trailing newline, `AUTO`) is rejected. |
| `forgejo.attachment_mode` | str or null | — | `native` (upload through the Forgejo API) or `link` (register an already-hosted URL as-is) |
| `forgejo` | object or null | — | the nested section; a bare `forgejo:` header (null) is tolerated the same as absent, `forgejo: false` is rejected |

All three leaves are also registered as wizard options (`riglib/schema.py`, `task` Area), so
`rig setup` and `rig config set` accept them. The default backend contract for a repo is:

```yaml
task:
  backend: forgejo
  forgejo:
    url: https://git.hyperide.ai
    repo: owner/name
    attachment_mode: native
```

#### Cascade contract for `task.forgejo`

**The contract, in one sentence:** an explicit `task.forgejo.repo` is inherited normally from
ancestor configs in BOTH rig and task-cli, and only `code_prefix` is stripped; `repo: auto` is the
documented workspace fallback for when each repo should resolve its own origin.

In detail: `task.forgejo` cascades exactly like every other `task` routing key, in both rig and
task-cli: `global < ancestors (outermost first, nearest wins) < repo`, merged per key. An explicit
`owner/name` in an ancestor `rig.yaml` IS inherited by every repo underneath that does not set its
own. rig and task-cli must resolve the same file to the same value, so rig neither strips, refuses,
nor warns about an inherited explicit `repo`.

Because of that, the rule is:

- A folder-default ancestor sets `backend`, `forgejo.url`, and `forgejo.repo: auto`. Each repo
  underneath then resolves its own `owner/name` from its own `origin` remote.
- An explicit `forgejo.repo: owner/name` belongs only in the repo's OWN `rig.yaml`. It overrides
  an inherited `auto`.
- An explicit `owner/name` in a folder-level ancestor therefore routes every repo beneath it
  that does not override `repo` into that one repository. That is intended when the ancestor is
  a primary checkout (its `rig.yaml` is a legitimate ancestor of its own `.worktrees/<name>`
  worktrees and carries exactly such a value); for a workspace folder, use `auto`.

Per-repo explicit example (the repo pins its own issue store):

```yaml
# ~/xp/rig-cli/rig.yaml
task:
  backend: forgejo
  forgejo:
    url: https://git.hyperide.ai
    repo: ultrabricks/rig-cli
    attachment_mode: native
```

Inheritance-fallback example (a folder default supplies the backend and instance; each repo
supplies only its own identity, or nothing at all to fall back to `auto`):

```yaml
# ~/work/rig.yaml (folder default)
task:
  backend: forgejo
  forgejo:
    url: https://git.hyperide.ai
    repo: auto
```

```yaml
# ~/work/hyperide/rig.yaml (inherits backend + url; pins its own repo)
task:
  forgejo:
    repo: hyperide/hyperide
```

A sibling repo under `~/work` with no `task:` block of its own inherits `repo: auto` and resolves
its issue store from its own `origin`.

#### Migrating a repo to `forgejo`

There is no dedicated migration command. The existing single-key editor handles it: each call
writes one nested key, validates the result, and rolls back on a validation failure. It is
idempotent (re-running with the same value changes nothing). To move a repo off a stale
`github-issues` or `linear` override:

```sh
rig config set task.backend forgejo
rig config set task.forgejo.url https://git.hyperide.ai     # omit when an ancestor already sets it
rig config set task.forgejo.repo owner/name                  # or `auto`
```

`rig config set` never removes a leftover `task.github`/`task.linear` section or a flat
`task.repo`; delete those by hand. Remove a flat `task.repo` in particular: rig documents it as
shorthand for `github.repo`, but task-cli's Forgejo branch also fans it out onto `forgejo.repo`.
For that reason, under `backend: forgejo` rig applies the same `owner/name`-or-`auto` check to a
flat `task.repo` as to `task.forgejo.repo`.


## `tmux.recovery.enabled` — restore a lost server without cloning agents

Default `false`. With independent macOS autosave enabled, recovery runs on its configured
`tmux.continuum.save_interval` cadence (15 minutes by default), before the no-server exit.
It records only a responding server's PID, owner, executable, birth time and socket.
A missing socket belonging to that same live server is repaired with SIGUSR1; a positively
dead recorded server is recreated with the managed boot script and normal resurrect hooks.
Unknown process inspection never permits a signal or replacement. Create the private
`~/.config/rig/tmux/recovery-paused` marker to suppress automatic recovery intentionally.
A kernel lock and cooldown avoid racing recovery; bounded forensic logs and the newest
8 crash reports remain under `~/.config/rig/tmux/` for diagnosis.

OMP restoration now stores the exact header session ID and file for each pane, including
Bun-launched OMP. An unknown live owner preserves the previous map and refuses a duplicate.
No latest-file-by-cwd fallback is used: two agents can legitimately share one directory.

For persistent interactive sessions, install `abduco`, then run `rig omp run -- /absolute/omp`
or install the opt-in managed launcher using `rig omp install-wrapper --target NEW_PATH
--real-bin /absolute/vendor/omp`. An existing unmanaged file/symlink is never overwritten.
The launcher records its vendor executable and passes noninteractive OMP CLI calls through.
`rig omp list --json` lists durable terminal sessions; `rig omp attach ID` reconnects without
starting an agent. The default socket namespace is `~/.local/state/rig/abduco` (private).
`rig omp run --session-id ID --resume EXACT_FILE` restores a dead original only by explicit
history identity. Existing unwrapped live processes need a one-time saved stop/resume to
enter this durable lifecycle; a live legacy process is never adopted by duplicating it.

`tg-ctl omp-live` is preferred for exact live identity; when absent, rig inspects the original
process's own TTY breadcrumb/open transcript. If no inspector is available, recovery blocks
rather than interpreting that as an empty machine. The transport requires the original-process
extension; see tg-cli's `docs/omp-original-process.md` for installation and receipt semantics.


The opt-in reproducible real-process proof is `python3 scripts/verify-omp-recovery.py
--tg-cli-root /path/to/tg-cli --omp-bin /path/to/omp --output /private/evidence-dir`.
It uses only a new private tmux socket, a no-tools OMP, and one short model acknowledgment;
it verifies socket recreation, server death/recovery, exact map identity, and SAME original
PID/instance/session after reattach. It does not stop or restart the default tmux server.
