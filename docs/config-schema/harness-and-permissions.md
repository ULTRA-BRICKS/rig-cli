[← Back to the config schema index](../config-schema.md)

# `rig.yaml` schema — Harness and permissions

Reference part of [config-schema.md](../config-schema.md): `harness`, `permissions`, `harness_wrappers` and the hosted `rig harness` sessions.

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

**Global problem-reporting instructions.** Every configured harness (effective repo kinds plus
the machine's global kinds) receives rig's canonical reporting requirement by default. A
problem or blocker report must include the specific actions already attempted and observed
results, the independent remedy already started, and the next concrete step/verification
point. If work truly cannot continue, name the exact dependency and why it blocks the next
action. A bare problem description or "under observation" is insufficient.

The canonical source is `riglib/problem_reporting.py`; a manually maintained home `AGENTS.md`
is not required. Provisioning is independent of `permissions.enabled` and repo-local
`agents_md.enabled`; `harness.enabled: false` leaves the client instruction files untouched.

| Harness | Global instruction target |
| --- | --- |
| Claude Code | `~/.claude/CLAUDE.md`, plus `CLAUDE.md` in the directory selected by an explicit user-scope Claude `settings_path`, in registered user-scope `settings_paths`, and in discovered account directories; `discover_config_dirs` is honored |
| Codex | `~/.codex/AGENTS.md`, honoring `RIG_CODEX_HOME` |
| OMP | `~/.omp/agent/RULES.md`, honoring `PI_CODING_AGENT_DIR` / `PI_CONFIG_DIR` |
| OpenCode | `~/.config/opencode/AGENTS.md`, honoring `XDG_CONFIG_HOME`; when absent, preserve an existing `~/.claude/CLAUDE.md` fallback by updating that file instead |
| Pi | `~/.pi/agent/AGENTS.md`, honoring `PI_CODING_AGENT_DIR` |
| CommandCode | `~/.commandcode/AGENTS.md` |

OMP uses `RULES.md` because its task subagents drop `AGENTS.md` but retain rules, as described
in its [context-file documentation](https://github.com/can1357/oh-my-pi/blob/main/docs/context-files.md).
OpenCode's target is its documented [global rules file](https://opencode.ai/docs/rules/).
Rig honors `OPENCODE_DISABLE_CLAUDE_CODE=1` and `OPENCODE_DISABLE_CLAUDE_CODE_PROMPT=1`
when selecting the fallback; native files and broken native aliases are never silently bypassed.
Client discovery settings still apply; for example, an OMP configuration that disables the
native context provider will not load its native rules file.

Claude instruction targets are deduplicated by the instruction file, independently of settings
aliases: accounts sharing `settings.json` can still have separate `CLAUDE.md` files. Relative
settings paths and extra instruction targets inside the current repository are project scope
and are excluded from this global action. The Global config-web plan and drift panels include
the same `instructions` actions. With no client installed, `rig activate` can prepare the
default Claude instruction file without enabling or claiming an installed harness.
A directory containing only rig's managed reporting policy, or an empty directory chain left
by preparation or a failed write, is excluded from client detection. Repeating activation
does not turn that preparation into an auto-mode enrollment; an executable or real client
configuration remains installation evidence.

Preview with `rig apply info --only instructions`; apply with
`rig apply commit --only instructions`. The managed block is appended or updated without
replacing user-owned instructions. Updates honor `on_conflict`, malformed markers fail
explicitly, and a repeated apply leaves current files unchanged. Writes use a temporary file
and atomic replacement, retaining existing symlink aliases and permissions. An intervening
content edit or alias change detected before replacement is reported as a conflict; re-run
apply against the latest content. Reporting, advisory permission, and canonical repo-guide
splices also share a kernel lock for the resolved file across their complete read/update/write
transaction, including writes through different aliases. Locks live under
`$XDG_STATE_HOME/rig/instruction-locks` (default `~/.local/state/rig/instruction-locks`),
outside client directories, and release on failure or process exit. A hard-linked file
requiring an update is left intact with
an explicit error rather than silently splitting its aliases. `rig status` reports missing
or stale blocks under **global agent instructions**. Existing sessions may need to reload
their client context before consuming updated instructions.

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
that mentions "worktree add"), `EnterWorktree`
(whole-tool deny on purpose: the tool can CREATE a worktree at a model-supplied path, which
is the location-bypass `rig worktree create` exists to close — CC-only, no equivalent on
opencode/codex/omp), and Runpod creation, removed by owner order tg#25309 (rig-cli#714):
`Bash(runpodctl pod create:*)`, `Bash(runpodctl pods create:*)`,
`Bash(runpodctl serverless create:*)`, `Bash(runpodctl sls create:*)`,
`Bash(runpodctl cluster create:*)`, `Bash(runpodctl create pod:*)`,
`Bash(runpodctl create pods:*)`, `Bash(flash deploy:*)` plus the whole-tool MCP denies
`mcp__runpod__create-pod`, `mcp__runpod__create-endpoint`, `mcp__runpod__create-cluster` and
`mcp__runpod__deploy-hub-repo`. Ask (prompt, don't block): `Bash(pkill:*)`,
`Bash(killall:*)`, `Bash(git reset --hard:*)` + `Bash(git reset * --hard *)` +
`Bash(git reset * --hard)`. The full annotated list lives in `riglib/permissions.py`.

**Baked deny/ask baselines (opencode).** opencode now gets its OWN deny/ask baseline written into
the `permission.bash` object (values `"deny"`/`"ask"`), hand-written in opencode's glob dialect
(`*` = zero-or-more chars, `?` = one char, **last matching key wins**). Deny: `"gh pr merge*"`,
`"git push*--force*"`, `"git push -f*"`, `"git commit --no-verify*"`, `"sudo rm*"`,
`"screencapture*"`, `"osascript*"`, `"git worktree add*"`, `"rg*--pre *"` + `"rg*--pre=*"` + `"rg*--pre"` (value-shaped / end-anchored,
so they don't also deny the legitimate `--pretty` flag — a single substring-only `"rg*--pre*"`
entry was rejected for exactly that regression), and the same eight Runpod creation prefixes as
claude-code (`"runpodctl pod create*"` … `"flash deploy*"`). Ask: `"pkill*"`, `"killall*"`, `"git reset*--hard*"`.
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
and — rig-cli#187 — the flag-first 2-token `rg --pre`, and — rig-cli#714 — the eight Runpod creation
prefixes `runpodctl pod|pods|serverless|sls|cluster create`, `runpodctl create pod|pods` and
`flash deploy`) becomes `decision="forbidden"`. **Fidelity gap:** `prefix_rule` matches a
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
trusted repositories — wrapper indirection (a command run as the first word of a `$(…)`
substitution, copied binaries, aliases, a `sh -c "$CMD"` whose string is a variable, a script
file or another program's output piped into a shell) hides the command from argv matching; it is
not a sandbox. Literal `sh -c '…'` / `bash -lc '…'` / `eval '…'` / `env -S '…'` /
`find -exec … ;` / `watch '…'` strings and `echo|printf … | sh` / `sh <<< '…'` are re-parsed and
matched like top-level commands (rig-cli#701).

**omp guard signal safety (rig-cli#701).** 2026-10-15 an agent, pushed off `pkill` by the ask
rule, ran `kill -TERM $(pstree -p $P …)` with an empty `$P`; `pstree -p` printed the whole system
tree and SIGTERM reached `systemd --user`, ending every omp session and the desktop. The guard now
judges what a kill-class command signals — kill/pkill/killall, also behind `sudo`/`doas`/`env`/
`timeout`/`nohup`/`nice`/`setsid`/`stdbuf`/`ionice`/`chrt`/`taskset`/`strace`/`flock`/
`systemd-run`/`xargs` (path-qualified too), the nested command strings above (any depth), shell
keywords and subshells. A prefix chain deeper than 16 wrappers is denied outright
(`signal-session-infra`) rather than guessed at. These rules live in the omp guard only — the
claude-code/opencode/codex belts do not carry them yet (rig-cli#706):

- DENY `kill-pstree-fed`: a kill fed by pstree (substitution, `| xargs kill`, a variable assigned
  from pstree).
- DENY `kill-broadcast`: `kill -1`, `kill 0`, negative PIDs / `kill -- -PGID` (also through a
  variable bound to such a literal earlier in the command), `pkill -v`, and `pkill`/`killall`
  selecting by user/session/group with no pattern.
- DENY `signal-session-infra`: pkill/killall patterns naming systemd, gnome-shell/gnome-session/
  Xwayland, gdm, tmux or omp; `kill $$`/`$PPID`; a kill of `$(pgrep|pidof|ps …)` for those names;
  `systemctl exit|isolate|…` and stop/kill/restart of slices, `init.scope`, `user@*`, `session-*`,
  omp scopes, GNOME/gdm/tmux/dbus units or a bare glob; `loginctl terminate-*|kill-*`;
  `tmux kill-server` and `kill-session -a` on the default server (`-L default` included); and a
  LITERAL PID that /proc resolves to pid 1, the guard's own omp process or one of its ancestors
  (pane shell, tmux server), session infrastructure by comm, a logind session leader, or any
  process in ANOTHER omp scope.
- ASK `kill-computed-pid`: a kill whose target is computed at runtime (`$(…)`, backticks,
  `xargs kill`, a glob, or a `$VAR` without a visible `[ -n "$VAR" ] &&` / `[ -z "$VAR" ] ||` /
  `if [ -n … ]; then` check or `${VAR:?}`), and `xargs pkill|killall|systemctl stop` (patterns or
  units from stdin).
- Allowed: `kill <literal pid>` of your own processes, `kill %1`, `kill $!`, `kill -0` probes,
  `kill -l`, `systemctl --user stop run-*.scope`.

Every kill-class reason names the way out (proc://bg_N/kill, `systemctl --user stop
run-<id>.scope`, `[ -n "$P" ] && ps -p "$P" && kill "$P"`). Check a command without running it:
`bun ~/.omp/agent/extensions/rig-permissions-guard.ts '<command>'` prints `deny|ask <id>: <reason>`
or `allow` (exit 2/1/0); `… --tool <name> --input '<json>'` does the same for any other tool call
(e.g. `--tool write --input '{"path":"xd://mcp__runpod_create_pod","content":"{}"}'`), and a
malformed invocation exits 64. Literal-PID verdicts are judged from the CHECKING process's ancestry and
omp scope, so run it from the session's own bash tool to get that session's verdict. A kill run
as the first word of another command's `$(…)` stays the documented wrapper gap above.

**omp guard: Runpod is removed (rig-cli#714, owner order tg#25309).** Heavy work runs on the
owner's server through `server-run -- <cmd>`. The guard denies every Runpod creation path an agent
has; each reason reads `Runpod is removed (owner tg#25309): heavy work goes to the server:
server-run -- <cmd>`. There is no sanctioned Runpod exception left.

- DENY `runpodctl-pod-create`, `runpodctl-pods-create`, `runpodctl-serverless-create`,
  `runpodctl-sls-create`, `runpodctl-cluster-create`, `runpodctl-create-pod`,
  `runpodctl-create-pods`, `flash-deploy`: those argv prefixes, behind wrappers and nested
  `sh -c` strings too, and past runpodctl's global `-o|--output`.
- DENY `runpod-http-create`: a curl/wget/HTTPie/xh POST, PUT or PATCH (or an explicit method the
  guard cannot resolve) to `*.runpod.io` whose decoded, normalized path matches
  `^/v[12]/(pods|endpoints|clusters)(/|$|\?)` — create, update and lifecycle POSTs alike. GET reads
  and DELETE (it only stops spend) stay allowed. A request whose URL, method or body comes from a
  file the guard cannot read first — `curl -K|--config`, a `wget -i` write, a curl `{{var}}` URL
  write — is denied whatever its host (fail closed); `wget -e method=…|post_data=…` is judged like
  the matching flag.
- DENY `runpod-graphql-create`: a write to `*.runpod.io/graphql` whose body carries
  `podFindAndDeployOnDemand`, `podRentInterruptable`, `createCluster`, `deployCpuPod` or
  `saveEndpoint`, or whose body cannot be read before it runs (`@file`, stdin, a `$…`/backtick
  expansion — so a read query using GraphQL `$variables` is denied too); one of those names in the
  URL denies as well. Bodies are matched as written, with JSON `\u…` escapes decoded and
  `%XX`-decoded.
- DENY `runpod-mcp-create`: a `write` to the MCP devices `xd://mcp__runpod_create_pod`,
  `…_create_endpoint`, `…_create_cluster`, `…_deploy_hub_repo` (case, `%XX` and `-`/`_` spellings
  normalized; a tool named `mcp__runpod_create_pod` directly too). `runpod-mcp-scale-up`:
  `xd://mcp__runpod_update_endpoint` whose JSON content sets `workersMin` or `workersMax` above
  zero at any depth, or cannot be parsed; renames and scale-to-zero pass. `list_*`, `get_*`,
  `delete_*` and reading a device's docs stay allowed.
- DENY `eval-runpod-graphql-deploy` / `eval-runpod-graphql-rent`: an eval cell whose source names
  `podFindAndDeployOnDemand` / `podRentInterruptable` as a whole word, as written or with its
  `\u…`/`\x…` string escapes decoded (a name assembled at runtime, e.g. by concatenation, is not
  seen).

Known gaps, same class as the wrapper gap above: a write by an interpreter (`python -c`,
`node -e`), `rsync`, `xargs rm`, a `$VAR` path or URL host, and a `cd` to an unknowable directory
are not judged (a literal `cd` adds a directory — the old one stays, since the cd may fail); a
write POSTed to another host that redirects (`curl -L`, HTTP 307) to Runpod, and a request shaped
by a default `~/.curlrc`/`~/.wgetrc`, are not seen; `runpodctl serverless update --workers-max N`
and `runpodctl pod start` (they bill an existing endpoint or pod) are not denied; and the budget
and installed-script path rules exist on omp only — the claude-code/opencode/codex belts carry the
argv and (claude-code) MCP denies, not path-scoped Edit/Write denies.

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

## `harness_wrappers`

Makes a coding-agent harness start under `rig harness` without anyone typing `rig harness run`:
`rig apply commit` writes one small shell script per harness (`omp`, `claude`, `codex`, `opencode`)
into a directory of your choosing. Each script is just `rig harness exec <kind> -- "$@"`. An
**interactive** launch on a terminal is then hosted in an `abduco` session under rig's crash
supervisor (`rig harness host`) and registered with tg-ctl, so a crash relaunches the harness onto
the same conversation and tg-ctl can type into it from outside tmux. Install `abduco` for that
(`brew install abduco`).

**Only interactive launches are hosted.** Everything else — `-p` / `--print`, `exec`, `mcp` and
other subcommands, `--help`, a launch with no terminal — runs the real binary untouched, so
scripts, hooks and the harnesses' own headless children see no difference.

**Opt-in, GLOBAL config only.** Default `enabled: false`; an absent or empty block writes nothing
(only `signal_scope`, below, acts by default).
The block writes executables onto your PATH and names the binary they exec, so a committed repo
`rig.yaml` must choose neither: `config.load` rejects `harness_wrappers` from any non-global layer
(a repo `rig.yaml` or a `--config` file), and `rig config set harness_wrappers.… --global` writes the
global file.

```yaml
# ~/.config/rig/config.yaml
harness_wrappers:
  enabled: true
  dir: ~/.local/share/rig/bin            # default shown; absolute, ~ or $VAR
  kinds: [omp, claude, codex, opencode]  # default shown; any non-empty subset
  real_bin:                              # only for a real binary that is NOT the first match on PATH
    omp: ~/src/oh-my-pi/packages/coding-agent/dist/omp
```

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `enabled` | bool | `false` | write the wrappers (opt-in) |
| `dir` | str | `~/.local/share/rig/bin` | where the scripts land, one file per kind named after it (`<dir>/omp`). Absolute, `~…` or `$VAR…`; a relative path is rejected |
| `kinds` | array of `omp` \| `claude` \| `codex` \| `opencode` | all four | harnesses to write a wrapper for; must not be empty (to install none, set `enabled: false`) |
| `real_bin` | map kind → path | `{}` | absolute path (or `~…` / `$VAR…`) of the real binary for a kind whose real binary is not the first executable of that name on PATH — typically the omp fork checkout. A key must be one of the installed `kinds` |
| `signal_scope` | array of `omp` \| `claude` \| `codex` \| `opencode` | `[omp]` | kinds whose launch through rig runs inside a Linux Landlock signal domain (see below). Independent of `enabled`; `[]` turns it off |

**PATH ordering is yours.** rig never edits `PATH`, a shell rc file or an existing binary. A wrapper
only takes effect once `dir` comes BEFORE the directories holding the real binaries, for example
`export PATH="$HOME/.local/share/rig/bin:$PATH"` in your shell profile. Until then the wrappers sit
unused and nothing changes.

**Where the real binary comes from.** The wrapper resolves it at run time: the path recorded for the
kind in `~/.config/rig/harness-real-bin.json`, else the first executable of that name on PATH that is
not itself a rig wrapper. `real_bin` is how you pin it when that first match is wrong — `rig apply
commit` records each declared path there (the file must exist and be absolute, and must not be a rig
wrapper). A kind without a `real_bin` entry leaves any path already recorded alone.

**What `rig apply commit` does.** `plan.build` emits one `harness_wrapper` action per kind, in the
fixed order `omp`, `claude`, `codex`, `opencode` whatever order `kinds` lists them in. Each action:

- writes `<dir>/<kind>` atomically with mode `0755` when it is missing (`created`);
- rewrites it when it is a rig-written wrapper with different text, for instance after the checkout
  `rig` runs from moved (`updated`);
- records a declared `real_bin` in `harness-real-bin.json` when that differs (`updated`);
- does nothing when the text and the record already match (`unchanged`) — re-running is a no-op;
- **refuses** to touch a file or symlink at that path that rig did not write (no rig marker): the
  action fails with an error naming the path, the file is left byte-for-byte as it was, and the
  other kinds still install. `defaults.on_conflict` does not change this — `backup` would move what
  may be the real binary out of the way. Move the file aside, or point `dir` somewhere else.

`rig apply info` and `--dry-run` only print the actions; they write nothing.

**Drift.** `rig status` lists a `harness launcher wrappers` area (GLOBAL) and reports each declared
kind: a wrapper that is **missing** (`wrapper not installed`), **stale** (a rig wrapper whose text
differs), or **foreign** (a file or symlink rig did not write), and a **real binary** record that
disagrees with `real_bin` (`<kind> real_bin`). All of them point at `rig apply commit`; a foreign
file is the one case it will not fix by itself. A wrapper the config no longer declares is never
judged, and never deleted.

Idempotent: a second `rig apply commit` with the same config changes nothing.

### `signal_scope` — each agent session can signal only its own process tree (#704)

Default `[omp]`, Linux only, and independent of `enabled`: it applies to every rig launch path of a
listed kind (`rig harness exec` and the `rig harness` host, including every crash relaunch). rig
puts `riglib/signal_scope.py` as the innermost argv prefix:
`python3 -I -S <checkout>/riglib/signal_scope.py -- <real harness> <args…>`. The helper calls
`prctl(PR_SET_NO_NEW_PRIVS)` and `landlock_restrict_self` with a ruleset that handles no filesystem
or network access and only `LANDLOCK_SCOPE_SIGNAL` (Landlock ABI ≥ 6, kernel ≥ 6.12), then execs the
harness in place (same pid, the harness's own argv). Each launch gets its own domain.

**What it blocks.** The session (omp itself, its in-process `kill` builtin, eval kernels, MCP/LSP
servers, subagents, the omp broker it starts) can send a signal — including signal 0 and
`pidfd_send_signal` — only to processes in its own domain or one nested under it. Every other target
gets `EPERM` ("Operation not permitted"). `kill -1` and `kill -<pgid>` skip targets outside the
domain. A wrong pid list therefore ends at most that one session. Kernel-sent signals (tty
Ctrl-C, SIGCHLD, SIGPIPE, SIGHUP) are unaffected, and processes outside can still signal in (tmux,
the rig host, systemd). Domains nest up to 16 layers; a 17th launch (omp starting omp starting …)
still starts, inside the 16 it has.

**What it does not block.** Requests that ask a process *outside* the domain to do the killing:
`systemctl --user stop|kill|exit|isolate`, `systemd-run --user … kill`, `loginctl terminate-*`,
`tmux kill-server`, `docker kill`. Those stay the job of the permission guard (#701). This is
protection against accidents, not against an adversarial agent. It also stops one session from
killing a dev server another session started; use `systemctl --user stop <unit>` or ask that session.

**NO_NEW_PRIVS side effects.** The flag is inherited across exec, so setuid/setgid bits and file
capabilities no longer raise privilege inside the session:

| Affected | Inside a contained session | Way out |
|---|---|---|
| `sudo`, `pkexec`, `su` | refused ("no new privileges" flag is set) | run them in a tmux pane outside omp |
| AppImages (FUSE through setuid `fusermount3`) | `Cannot mount AppImage`, rc 127 | extract once (`--appimage-extract`) and exec `squashfs-root/AppRun`; ad hoc `APPIMAGE_EXTRACT_AND_RUN=1` with `TMPDIR` on disk |
| `sshfs`, any FUSE mount, `fusermount3 -u` | `Operation not permitted` | `systemd-run --user --pipe --wait --quiet -- <cmd>` |
| `newuidmap`/`newgidmap` (rootless podman) | fails | `systemd-run --user --pipe --wait --quiet -- <cmd>` |
| Chrome / agent-browser, `bwrap`, docker | work (no setuid needed) | — |

The escape hatch `systemd-run --user --pipe --wait --quiet -- <cmd>` has the user manager start
`<cmd>` as a transient service: it runs outside every domain, with `NoNewPrivs: 0`. It is for the
privilege-needing tools above (sudo, fusermount, rootless podman) only. An `EPERM` from `kill` is
the containment working: re-running that `kill` through the escape hatch is exactly the incident
this exists to stop (the permission guard's kill rules still apply to it).

**Diagnosis.** A contained process has `RIG_SIGNAL_SCOPED=1` in its environment (inherited by every
child): that is why `kill` says "Operation not permitted". When containment is unavailable (ABI < 6,
not Linux, a failing syscall) the helper prints one line,
`rig signal-scope: not contained (<detail>); starting <cmd> unrestricted`, and starts the harness
anyway — it never blocks a launch. `RIG_SIGNAL_SCOPE=0` skips it for one launch. A missing or
invalid global config falls back to the default `[omp]`.

**Known limitation: omp's cross-project broker prune.** omp's liveness probes treat `EPERM` as
"dead". A daemon broker started in one session's domain sees other projects' brokers and clients as
dead, deletes their presence files and, after 5 minutes, their daemon runtime dirs. The services keep
running and nothing is killed, but other sessions lose `proc://` control and logs for them, and
`omp ps` may show a detached daemon as exited. The fix (only `ESRCH` means dead) is upstream in
oh-my-pi.


## `rig harness` — hosted harness sessions for omp, Claude Code, Codex, opencode

```sh
rig harness run <omp|claude|codex|opencode> [--name LABEL] [--host-id NAME] [--detach] [--real-bin PATH] [harness args…]
rig harness attach NAME          # reconnect a terminal to a hosted session
rig harness list [--json]        # abduco sessions joined with their tg-ctl records
rig harness install-wrapper <harness> --target PATH [--real-bin PATH]
```

`run` replaces the current process with `abduco -f -A NAME rig harness host NAME`, so a dead
terminal never loses the agent; `--detach` creates the session without attaching and prints its
name (scripts, smoke tests). `NAME` is `<harness>-<12 hex>` (never the cwd — one directory
legitimately hosts several sessions) and stays the same across relaunches: it is the address
tg-ctl delivers to. `--name` is a human label (tg shows it as the `[window]` bracket outside
tmux); options must precede the harness's own arguments, and a bare `--` ends them (so Claude
Code's own `--name` goes after it). The socket namespace is `~/.local/state/rig/abduco`
(private, `RIG_OMP_ABDUCO_DIR` overrides it in tests).

**Process tree.** `abduco server (pid 1's child) → rig harness host NAME → harness`. The
supervisor runs INSIDE the abduco session, so the pty, the socket and any attached client
survive a relaunch, and the harness's stdio IS the abduco pty (nothing is proxied). The harness
command line travels in the `RIG_HOST_SPEC` environment variable of the session — never in
argv: tg-ctl identifies agents by argv (`…/claude …`, `…/codex`), so an argv naming the harness
would make the abduco server or the supervisor look like the agent.

**Crash handling** (`riglib/harness_host.py`). Classification happens in the supervisor — the
harness's parent — because abduco 0.6 prints `session terminated with exit status 0` for a child
killed by SIGKILL or SIGTERM alike (`WEXITSTATUS` of a signal status). On 2026-09-30 an omp host
grew to ~90 GiB, macOS memory-pressure jetsam SIGKILLed it, and the only trace was that "0".

| Harness exit | Verdict |
|---|---|
| exit 0 (`/exit`, Ctrl-D) | requested — supervision ends, record deleted |
| 129 / 130 / 143, SIGHUP / SIGINT / SIGTERM | requested stop — same |
| SIGTERM/SIGHUP to the supervisor itself | forwarded to the harness, supervision ends |
| any other non-zero status | crash — relaunch |
| any other signal (SIGKILL, SIGSEGV, SIGABRT, …) | crash — relaunch |

A crash relaunches the harness in the same session and terminal after a pause that doubles from
1 s to 30 s while runs keep dying within 60 s. Once 5 restarts happened inside 10 minutes, the
next crash ends supervision: the supervisor deletes the record and exits with the REAL status (`128 + signal`), so abduco's final
message stops lying. Every decision is printed to the terminal and appended to
`~/.local/state/rig/abduco/supervisor.log`. The relaunch only happens after the dead process was
reaped, so there is never a second writer of the conversation.

**Native resume.** The relaunch keeps the original configuration flags, drops the one-shot
session choice and the initial prompt (already in the transcript), and adds:

| Harness | Relaunch | Which conversation |
|---|---|---|
| omp | `--resume <jsonl>` | omp's terminal breadcrumb `<agent dir>/terminal-sessions/<tty>`, accepted only if written after the dead process started (follows `/new`, `/resume`); else the original command line |
| Claude Code | `--resume <id>` (`--session-id <id>` if the transcript never reached disk) | `~/.claude/sessions/<pid>.json` sampled while it lives (follows `/clear`); else the id the wrapper assigned with `--session-id` at launch |
| Codex | `resume <id>` | the only interactive rollout of this cwd under `~/.codex/sessions` touched since start (`session_meta.cwd`, not a subagent thread) |
| opencode | `--session <id>` | the only top-level session of this cwd in `opencode.db` updated since start |

Codex and opencode cannot tell two hosted sessions in the SAME directory apart, and guessing could
attach a second writer to a live sibling's conversation, so with more than one candidate they answer
unknown (rig-cli#664); omp and Claude Code are exact. With nothing known the previous command line is
reused (a fresh conversation, never someone else's).

**Registration with tg-ctl.** While supervising, the host keeps
`${TG_CTL_CONFIG_DIR:-~/.config/tg-cli}/hosts/NAME.json` (mode 0600, written atomically,
refreshed on every relaunch, deleted when supervision ends): `v`, `name`, `harness`, `label`,
`cwd`, absolute `socket`, `serverPid` (the abduco server), `harnessPid`, `role: "coordinator"`,
`startedAt`/`updatedAt`, plus `supervisorPid`, `restarts` and the resolved `session`. tg-ctl
only reads it and ignores a record whose server is dead. The harness environment gets
`RIG_SESSION=NAME` (membership, not role) and NO coordinator marker; `RIG_AGENT_ID`,
`RIG_DETACHED_AGENT`, `PI_SESSION_FILE`, `TG_AGENT` (and the retired `RIG_OMP_SESSION`) are
scrubbed, so a session started from inside a subagent's shell is not mistaken for a subagent.
`RIG_SESSION` replaces `RIG_OMP_SESSION`: tg-cli must read it (its omp-live extension) before
sessions hosted by this version are expected to keep their durable mapping.

**Wrappers.** `rig harness install-wrapper omp --target ~/.local/share/rig/bin/omp` writes a
small script that hosts interactive launches and execs the real binary untouched for everything
else (`-p/--print`, `exec`, subcommands, `--help`, no tty). The real binary is the recorded one
(`--real-bin`, kept in `~/.config/rig/harness-real-bin.json` — needed when it is not the first
match on PATH, as with the omp fork checkout) or else the first non-wrapper on PATH. An unmanaged
file or symlink at the target is never overwritten. Putting the directory before the real
binaries on PATH is the caller's job; `rig apply` provisions the scripts from `harness_wrappers`.
