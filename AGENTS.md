# AGENTS.md — rig-cli

Rules for agents working in this repo. English only (no Cyrillic anywhere in repo docs).

## What this is

`rig` is the dev-environment umbrella driver: a standalone Python CLI that sets up a repo
from a committed `rig.yaml` by applying `agent-tools` content (skills, agent-hooks,
git-hook dispatcher, CI gates, MCP) and provisioning the agent harness's auto/permission
mode. It is a peer to `tg-cli` / `review-cli`, not part of `agent-tools` — it *consumes*
agent-tools read-only.

**`rig init` and `rig apply` are the two real commands.** `init` is first-run onboarding
(no config yet → scaffold `rig.yaml` + wire the catalog in, walking the user through it);
`apply` is the steady-state declarative reconcile (config exists → converge the disk to it).
They are distinct, NOT synonyms. Interactivity (full TUI / semi / non-interactive `--yes`) is
**orthogonal** to the command — both `init` and `apply` run in any of the three modes,
decided by TTY + config + flags. `init` is the canonical onboarding command (the front door).

**`rig apply` is PREVIEW-BY-DEFAULT — it splits into `info` and `commit`.** A bare `rig apply`
is an ALIAS for **`rig apply info`**: it builds + prints the plan (grouped, notes collapsed),
states it is a preview, and points at `rig apply commit` — it MUTATES NOTHING. **`rig apply
commit`** is the subcommand that ACTUALLY executes the plan (per-phase progress on the slow
live-activation runners, then a `✓ applied N actions (C changed, M unchanged) in Xs` completion
line). `--dry-run` still forces a preview (even under `commit`). For automation back-compat, a
bare `rig apply --yes` is read as commit intent and executes; the explicit CI form is `rig apply
commit --yes`. Both `info` and `commit` share the SAME `plan.build` — only `commit` calls
`actions.run_plan` (never fork the executor). The user must always be able to review the plan
before anything mutates. NOTE for call sites: a bare `rig apply` in a script now applies NOTHING
— scripts/tests/CI that mean to execute MUST say `rig apply commit`.

**`rig setup` is the INTERACTIVE config wizard** (NOT an alias for `init`/`apply`). In a TTY it
shows what is enabled across every reconciled area (the same areas as `rig status`), lets you
change options in the local `rig.yaml` AND the global `~/.config/rig/config.yaml` — each option
carrying an inline hint — then applies (`rig apply`) on the spot. With no TTY (piped/redirected)
it prints USAGE for `init`/`apply`/`config get|set` instead of running a half-wizard. The option
list + hints come from the in-code registry `riglib/schema.py` (the single source of truth, which
also emits a JSON schema). The wizard's schema-key engine (owning-layer routing — REPO keys →
`./rig.yaml`, GLOBAL-only keys like `gitignore`/`tg_ctl`/`tmux` → the global config) is INTERNAL
to `rig setup`; it is NOT the `config get|set` command.

**`rig config get|set <dot.path>` is the user-facing single-key editor** (the headless
counterpart `rig setup` points at), and it is a DIFFERENT surface from the wizard's schema engine.
`get <dot.path>` reads ONE nested key by dot-notation from the single target file (`./rig.yaml`,
or `--global`; NOT the cascade) — `--json` emits the raw value, a subtree prints as YAML, a
missing file/absent path exits non-zero. `set <dot.path> <value>` coerces the value conservatively
(`true`/`false`/int/float/null; leading-zero / `1e3` / underscored / Unicode-digit values stay
strings), writes it, then runs the SAME plan + apply engine as `rig apply` with full rollback if
the write or the catalog-backed plan build fails. `--global` targets the global config;
`--no-apply` writes the key and prints the plan only; a repo-local `set` refuses when `./rig.yaml`
is absent (run `rig init` first). The dot-path engine lives in `riglib/config.py`.

**`rig config-web` is the WEB front-end onto the same config + apply engine** — a fourth surface
beside the wizard, `config get|set`, and `rig apply` itself, never a parallel implementation. It
serves a local `http.server` MACHINE-WIDE console (`riglib/config_web.py`): one browser tab per
rig-managed repo (discovered read-only from the repository registry via
`riglib/config_web_scopes.py`) plus a dedicated Global tab (`~/.config/rig/config.yaml` alone, no
repo overlay). Each tab renders every area from the SAME registry (`riglib/schema.py`, via
`effective_value`) over the cascaded config, plus a DRIFT panel next to the settings
(`riglib/config_web_plan.py`'s `compute_scope_drift`, which delegates to the SAME
`compute_drift_report` helper `rig status` uses). An edit POSTs to `/edit` → `apply_edit`, which
coerces + validates fail-closed and writes through the SAME `SetupState` serializer `config set`
uses, routed to the OWNING layer (REPO → that scope's `./rig.yaml`, GLOBAL-only → the global
config); `apply_edit` itself still does NOT run `rig apply`. Reconciling is now INTERACTIVE
instead of a silent no-op: the browser offers a plan preview (`POST /api/plan`, mirroring `rig
apply info` via the exact same `plan.build()` call, each action tagged by
`riglib/action_tags.py`'s taxonomy — keyed off the real handler kinds in
`riglib/actions/runner.py`'s `_HANDLERS`, with a completeness test pinning it), lets you confirm
the whole plan or skip individual actions, then applies the SELECTED actions
(`POST /api/apply` → `riglib/config_web_plan.py`'s `ApplyJobStore`, which calls the SAME
`riglib.actions.runner.run_plan` — never a forked executor) with live per-phase progress polled
via `GET /api/apply/status` (reusing `run_plan`'s existing `on_start`/`progress` callbacks, the
same mechanism the `rig init` TUI's live Apply screen uses). A stale plan (config changed since
preview) is refused by fingerprint mismatch; only one apply job runs process-wide at a time; every
multi-repo endpoint validates its `scope` against the server's discovered-repo allowlist. Its
lifecycle (`run`/`start`/`stop`/`status`/`enable`/`disable` + launchd/systemd autostart) is
delegated to the SHARED `agenttools-service` lib — NOT hand-rolled here; the seam is
`riglib/config_web_service.py` (lazy-imports the lib so `rig --help` works without it, failing
closed with a `MissingDepError` when a lifecycle verb actually needs it). The server binds
`127.0.0.1` only and refuses cross-site (CSRF) writes on every mutating/compute-triggering POST. A
bare `rig config-web` prints help, never launches.

## Hard rules

- **Stdlib-only at import time.** Every `riglib/*` module imports only the standard library
  when loaded. Heavy/optional deps — `yaml`, `textual` — are imported lazily inside the
  function that needs them. `rig --help` and `rig doctor` must run with zero third-party
  imports. Do not add a top-level `import yaml`/`import textual`.
- **Long-running work needs factual status, not vibes.** When reporting progress on a large or
  delegated task, include the verified completed scope, the known gaps, the command/test evidence,
  the next concrete action already started, and a dated ETA. Evidence means repeatable command
  lines plus the relevant exit code, log path, screenshot path, commit id, task id, or review id.
  If you report a blocker, also record the remediation path and start the first remediation step
  or create/update the relevant task in the same turn.
- **Track long-running subprocesses as process trees, not just shell sessions.** For review,
  model, browser, server, and smoke runs that stay silent beyond their expected cadence, record the
  exec session id, inspect the live process tree (`ps`/`pgrep` with elapsed time), check the tool's
  log directory for fresh output, and report the real child-state before saying it is merely
  "still running". If children finished or timed out while the wrapper remains alive, resolve that
  wrapper state immediately: collect the final logs, stop the stale wrapper when safe, capture the
  failure mode, and start or record the next remediation step.
- **One engine, two front-ends.** `rig init` (wizard) and `rig apply` must share the same
  `plan.build` + `actions.run_plan`. Never fork the executor for the TUI. If you add a
  capability, add it to the headless engine first and let the wizard call it.
- **Harness auto-mode is provisioned through the reconciler, like every other target.** The
  `harness:` block flows config → `plan.build` (one `apply_harness` action PER configured kind —
  primary + `harness.kinds`) → `run_plan` (`actions/runner.py::_do_apply_harness`), writing only
  the managed permission key into the harness's own config, idempotent + backup-on-conflict, with
  drift surfaced by `rig status`. Recommend `auto_mode: true` by default — it is safe *because*
  the agent-hook guards (incl. `block-raw-pr-merge`, `block-reset-hard`) are installed in the same
  apply. **The per-kind mode key is one registry: `riglib/harness_mode.py`** (claude-code
  `permissions.defaultMode`; codex `approvals_reviewer` in `config.toml`; opencode `permission."*"`;
  omp `tools.approvalMode` — owned by the permissions approval action, the harness plan only notes
  it). A kind with NO such setting (pi/commandcode) is in `HARNESS_MODE_NA` with the reason and is
  surfaced as a VISIBLE note in `rig apply info` + a per-kind line in `rig status` — never a silent
  skip. Add a harness there, never a scattered key literal.
- **Claude Code config-dir discovery is one registry: `riglib/claude_config_dirs.py`.** Claude
  Code reads its user-scope `settings.json` from `$CLAUDE_CONFIG_DIR` when set; the `claude-rotate`
  launcher starts every interactive session with `CLAUDE_CONFIG_DIR=~/.claude-accounts/account-N`,
  so a write to `~/.claude/settings.json` alone leaves those sessions with ZERO rig hooks while
  `rig status` stays green (rig-cli#368). Every claude-code user-scope write (`apply_harness`,
  `provision_permissions`, the JSON `register_hook_bridge`) therefore emits ONE action PER target
  from `fan_out_settings`: the primary + `harness.settings_paths` + every discovered
  `~/.claude-accounts/account-*` dir (`harness.discover_config_dirs: false` opts out). Fan-out
  items are labelled `<kind>@account-N` so drift names WHICH file. A repo-local (project-scope)
  primary is never fanned out. `rig doctor` inventories the same targets plus the shell's
  `CLAUDE_CONFIG_DIR` and exits 3 with a per-dir fix when one lacks the hook bridge. Discovery is
  filesystem-only (deterministic plans / config-web fingerprints); the env var is doctor-only.
- **Per-harness skill/instruction discovery is one registry: `riglib/harness_skills.py`.** It
  maps `harness.kind` to one or more discovery surfaces: skills directories (claude-code →
  `~/.claude/skills`, codex → `~/.codex/skills`; rig symlinks each skill in via a
  `link_skill_harness` action), native discovery (opencode and omp auto-load `~/.agents/skills`,
  so rig records a note and links nothing when skills install to the default target), and global
  instruction files (codex/pi/commandcode → `AGENTS.md`). Codex is dual:
  `~/.codex/skills` carries skills and `~/.codex/AGENTS.md` carries global instructions. Add a
  new harness as one entry there — never scatter the path across plan/config/schema. The config
  schema ACCEPTS every kind in that registry; the runner/drift for skill links are
  harness-agnostic (they act on resolved targets).
- **`git` and `profile` are GLOBAL-layer-only, enforced at LOAD time** (`config._GLOBAL_LAYER_ONLY_BLOCKS`,
  like `mode`): a repo `rig.yaml` or `--config` layer carrying either is rejected, because
  `git.config` writes `git config --global` (code-executing keys such as `core.sshCommand`) and
  `profile` points `rig apply` at a repo whose config it pulls. Never relax this to write-routing
  only. `rig profile` (`riglib/profile.py`) syncs `~/.config/rig/config.yaml` through a PRIVATE repo
  with a three-way compare (local / repo / last-sync snapshot) and never merges silently; tests use a
  local bare repo and a tmp `GIT_CONFIG_GLOBAL` — never a real remote or the real `~/.gitconfig`.
- **What `rig profile` carries is ONE registry, `riglib/profile_files.py`.** An allow-list of portable
  files (`config.yaml`, the user region of `~/.tmux.conf`, omp `config.yml`, masked omp `mcp.json`,
  `sysmon.json`), each three-way compared on its own, plus `NEVER_SYNCED` (`env.vars` values, the omp
  auth-broker token, `agent.db`, `.env`, `remote-host`, `ssh-control`, `rig.env.sh`). A new portable file
  is one `FileSpec` there — never a special case in `profile.py`. **Secrets never travel:** `env.vars` is
  withheld on push and kept locally on pull, `mcp.json` is masked, and `scan_secrets` runs over every
  pushed byte and REFUSES the push (the finding names file/line/rule, never the value). Machine-specific
  values (registered `MachineKey`s and absolute paths outside HOME) are excluded from comparisons, win
  locally on adoption, and live per host in `machine/<host>.yaml`; HOME paths travel as `{{rig:home}}`.
  Tests build fake credentials by concatenation so no literal pattern sits in the source.
- **`rig activate` is the new-machine bootstrap, and a thin layer over the one engine.** Bare = preview
  (mutates nothing, not even the profile); `commit` syncs the profile, writes ONLY missing `enabled`
  switches into the global config (never overriding a key, a backup is kept), then runs the machine-level
  plan through the SAME `plan.build` + `actions.run_plan` as `rig apply commit`. Probing and rendering live
  in `riglib/activate.py`; an area is an id in `AREA_IDS` with the plan categories it owns — add a new
  area there, with its probe and the reason shown when the machine cannot support it. Unsupported areas
  are listed and skipped, never an error. User-scope hook dirs and bridges follow the GLOBAL config's
  harness kinds as well as the repo's (`plan._kinds_with_global`, rig-cli#743).
- **`tools.items.<name>` has `apt:` (apt machines only, skipped elsewhere) next to `brew:`, and `clone:`.**
  A first-class tool (`riglib/tools.py::KNOWN_TOOLS`, today `ctmux`) needs only `{}`: default clone URL,
  no skill blurb, verified with `install.sh --check`. apt probes live in `riglib/apt_packages.py` (one
  `dpkg-query` call shared by apply and status; a package whose command is already on PATH counts as
  present); rig never waits on a password — no root and no `sudo -n` is a WARN note naming the exact
  `sudo apt-get install -y …` line, NOT an abort: the tool's own install.sh still runs and `rig status`
  keeps flagging the package. `ctmux` defaults to `apt: [tmux, fzf]`; a tool's default `repo` is the
  first of `~/xp/<name>-cli` / `~/dev/<name>-cli` that holds an install.sh (`tools.default_repo`).
- **Harness hosting is one launcher, `rig harness` (`riglib/harness_*.py`); per-harness knowledge
  lives in ONE registry, `riglib/harness_kinds.py`.** Never add a second launcher/supervisor for
  one harness. The supervisor (`harness_host.py`) runs INSIDE the abduco session and is the ONLY
  place an exit is classified: abduco 0.6 prints `exit status 0` for a SIGKILLed child (proved on
  2026-09-30, rc-0 root cause), so a status read outside the harness's parent is meaningless.
  A harness command line travels in `RIG_HOST_SPEC`, never in abduco's or the host's argv (tg-ctl
  identifies agents by argv). The harness env gets `RIG_SESSION=<name>` (membership) and NEVER a
  positive coordinator marker; `RIG_AGENT_ID`/`RIG_DETACHED_AGENT`/`PI_SESSION_FILE`/`TG_AGENT` are
  scrubbed. The tg-ctl record (`~/.config/tg-cli/hosts/<name>.json`) is written/deleted only by the
  host. A new harness is one entry in `harness_kinds.py` (argv table, interactive rule, resume
  builder) plus one locator in `harness_sessions.py` — never a special case in the loop.
- **`rig.yaml` is committed by default.** It is the reproducible source of truth. Do not
  add an "is rig.yaml optional?" flag. Global config lives at `~/.config/rig/config.yaml`;
  per-repo `rig.yaml` overrides it; scope is by location, never a flag.
- **Drift is surfaced both ways, never silently reconciled.** `rig apply` converges
  config→disk only. `disk→config` extras are reported, never auto-deleted. An extra whose ORIGIN
  rig can name is a "known" item, not drift (`riglib/provenance.py`, rig-cli#357): a skill with an
  `.installed-by` marker / a `.blurbs/<name>.md` blurb / an ecosystem-tool name / a `skills.known`
  entry / a catalog item another repo's config selects; a hook descriptor with an `installed_by`
  key or under `agent_hooks.known`; a workflow whose stem is not a catalog CI slot (the repo's own);
  a permission entry beyond the baseline (always "kept", origin named). Known items print under
  their own dim heading and never touch the exit code. Never widen that set to hide REAL drift —
  an unknown-origin skill and an undeclared catalog gate workflow must stay drift.
- **User-visible UI work needs acceptance proof before it is reported as done.** For
  every portal/app change, keep a request-derived interaction checklist, verify the
  running app with browser automation plus at least one screenshot for visual changes,
  and report any unfinished checklist item as unfinished. A scaffold is not a result.
  If the request mentions loading or performance, ship a visible loading/progress state
  and record a concrete timing or browser proof before reporting.
- **Actions are idempotent and backup-noted.** A re-apply with the same config changes
  nothing (copies skip-if-identical, `core.hooksPath` checks current value, MCP merges are
  keyed). Anything replaced is backed up per `on_conflict` (skip|overwrite|backup) and the
  restore path recorded in the result. Fail-closed on validation; fail-explicit on IO.
- **Agent-hook `cmd` is always written absolute.** The `agents-hooks/v1` runner rejects
  relative paths; the install action rewrites the `/ABSOLUTE/PATH/TO/...` placeholder to
  the real script path in the agent-tools checkout.
- **Never mutate a LIVE running service in a way that disrupts an active session.** rig prepares
  on-disk artifacts; the user reloads their config. The `tmux` block writes `rig.tmux.conf` + the
  managed scripts + the boot script + a boot launchd plist and wires `~/.tmux.conf`, but NEVER
  runs `tmux source-file` against the user's live server (that would re-apply config under their
  feet). **The tmux LIVE ACTIVATION is the deliberate exception** (a clean machine must end up
  FULLY working with zero manual steps, CTO 2026-06-16): on `rig apply` rig also clones the
  plugins, creates `~/.tmux/resurrect`, `launchctl load -w`s the BOOT agent, takes a first
  `resurrect save`, and cleans continuum's stale boot. These are SAFE for an active session — the
  boot agent's script is idempotent (`has-session` → exit 0, never spawns a duplicate or touches
  existing panes), and a first `resurrect save` is read-only w.r.t. the live session. It mirrors
  the **stateless background daemons** exception (safe to (re)load because no live user session
  rides on them): the `models` schedule (a non-interactive cron) and the `tg_ctl` inbound daemon
  (`tg_ctl` block) both (re)load via launchd. `tg_ctl` writes the `ai.hyperide.tg-ctl.plist`
  LaunchAgent **byte-exact** to the working hand-created file (so a re-apply is a no-op `skipped`,
  never a spurious rewrite) and (re)loads it with `launchctl bootout`/`bootstrap` in the
  `gui/<uid>` domain; it also boots out + removes the dead predecessor `com.ultra.codex-tg-bot`.
  Gate the tmux activation behind `RIG_TMUX_DRY_RUN`, and `tg_ctl` behind `RIG_TG_CTL_DRY_RUN`
  (mirrors `RIG_SCHEDULE_DRY_RUN`) — which writes the managed plist but skips every
  live/destructive mutation (the `launchctl` bootstrap/bootout AND the stale-predecessor teardown:
  no bootout, no on-disk backup+remove). The unit suite + CI set these, so tests/smoke NEVER touch
  the real launchd domain or delete the predecessor file. **A HOME override alone is NOT a
  launchd sandbox — so rig guards it automatically (rig-cli#116).** Every fsutil-based action
  follows `Path.home()`, but `launchctl` acts on the real per-user `gui/<uid>` domain regardless
  of `$HOME`; a HOME-only-isolated `rig apply` once bootstrapped the REAL `ai.hyperide.tg-ctl`
  agent from a scratch-HOME plist and crash-looped it 23,641 times. So before ANY live
  launchctl/systemctl mutation the runner compares the resolved `Path.home()` with the uid's real
  login home (`riglib/actions/runner.py::_home_is_sandboxed`, one predicate feeding the four
  `_*_dry_run` seams: schedule, tmux, tg_ctl, spotlight); when they differ it behaves exactly like
  the DRY_RUN env (artifact written, live mutation skipped) and says so in the action detail
  (`HOME is overridden (<home> != <real home>) — skipped live launchctl … (sandboxed run)`). The
  post-apply **verify** and `rig status` **drift** follow the SAME predicate — verify skips the
  loaded-check and drift suppresses the live loaded-probe (schedule/tg_ctl now, as spotlight/tmux
  already did) — so a sandboxed apply exits 0 and converges instead of failing its own
  verification; a current plist is a no-op WITHOUT probing the real domain. There is no opt-in to
  force the live path from a foreign HOME (that is the incident); run with the real HOME. The env
  flags remain the explicit seam (and take precedence in the wording); a test that fakes HOME but
  asserts on the live (stubbed) path opts out with the `live_launchd_home` conftest fixture
  (`tests/conftest.py`). Migration backs up the original
  (`~/.tmux.conf.rig-bak-<UTC>`, timestamped) and never overwrites an existing backup.

## The integration seam (agent-tools)

`riglib/catalog.py` is the only module that knows the agent-tools on-disk layout. It scans
a checkout (`agent_tools_source` → `$RIG_AGENT_TOOLS_SOURCE` → default candidates) into a
flat `Item` registry. If agent-tools changes its layout, fix it *here* — nothing else
should hard-code agent-tools paths.

## Worktree lifecycle — one door, one location, one reconciler

**`rig worktree create` is the ONLY way an agent (or a human) should create a worktree in a
rig-managed repo, and `<repo>/.worktrees/<name>` is the ONLY location.** Before this, the
ecosystem had grown at least five conventions for where a worktree lands — a bare in-repo
`.worktrees/`, a sibling-of-repo `.worktrees/`, Claude Code's own `.claude/worktrees/`,
`<repo>-worktrees/`, `<repo>-wt-*` — and none of them were tracked, so nothing ever cleaned them
up (GH-329: ~70 leftover worktrees across the agent-ecosystem repos, tens of GB of duplicated
`node_modules`/`.venv`). `rig worktree create` also idempotently registers `.worktrees/` in the
repo's `.git/info/exclude` (never the committed `.gitignore`) BEFORE creating the worktree, so a
freshly created tree never dirties `git status` in the primary checkout. See
`riglib/worktree.py`'s module docstring for the full rationale, and the README's `rig worktree
create`/`remove`/`gc` rows for the user-facing contract.

- **`rig worktree create <name>`** — the single door. `--branch`/`--from` override the branch/base.
- **`rig worktree remove <path|name>`** — the inverse: removes the worktree AND its branch (`git
  worktree remove` alone leaves the branch behind) — but ONLY when the **landed-removal verdict**
  says SAFE (below); accepts a worktree anywhere git knows it; `--force` never overrides the
  verdict (it only unlocks a locked worktree).
- **`rig worktree verdict <path|name|branch> [--remote [NAME]] [--json]`** — read-only: "is deleting
  this guaranteed to lose nothing?" Exit 0 SAFE / 10 UNSAFE; UNSAFE prints the plain-language
  explanation (below), `--json` adds the file-by-file `evidence`.
- **`rig worktree discard <path|name|branch> --reason "<why>" [--remote [NAME]]`** — the ONE
  sanctioned way to drop work the proofs cannot clear: owner approval on Telegram (the agent-tools
  hatch helper, hook id `rig-worktree-discard`), batch-wise, no backup, only for NOT merged AND stale/obsolete
  work. See "Dropping obsolete work" below.
- **`rig worktree guard`** — the PreToolUse backend (bash command on stdin → one JSON
  `pass|allow|deny`) behind the agent-tools `block-unlanded-removal` hook.
- **`rig worktree gc`** — the reconciler for everything ALREADY on disk (including worktrees that
  predate this convention, wherever they physically live — `git worktree list` tracks every
  linked worktree for a repo regardless of location). Classifies each as live/prunable/dirty/
  merged/closed/no-pr-stale/active and removes only the safe classes; `rig status` reports a
  stale-worktree count using the same classifier. See `riglib/worktree_gc.py`'s module docstring.

### The landed-removal verdict — one engine, every entry point

`riglib/removal_verdict.py` is the ONLY place that decides whether deleting a worktree, a local
branch or a remote branch loses work; `rig worktree remove`, `rig worktree verdict` and the hook
backend `rig worktree guard` (`riglib/removal_guard.py`) all call it — never write a second check.
SAFE only if (fail CLOSED on any error/timeout/missing tool/ambiguity) the worktree is clean
(`status --porcelain --untracked-files=all` empty; no merge/rebase/cherry-pick/revert/bisect in
progress; no process with it as cwd; no other worktree or ignored git repo inside it; not the
primary checkout / default branch / a branch checked out elsewhere; other ignored files are NOT
protected, by rule) AND every commit unique to the branch/HEAD is LANDED on `origin/<default>` (resolved
from `origin/HEAD`) or still held by a branch that survives the removal, proven in order: ancestor of
`origin/<default>`; the MERGED PR first (found by head branch/head commit, else by the `#N` that a
default-branch commit cites together with the branch's commit subjects; its merge commit must be on
`origin/<default>`): a tip at or below the PR's recorded head is landed, a tip that descends from the head
holds NEW commits (named in the denial), a history rewritten after the merge is old when `git cherry` finds
a twin of every commit; `git merge-tree --write-tree` no-op or `git cherry` all-`-`; **`squash-merged`**
(`riglib/removal_squash.py`) — no usable PR: the commit on `origin/<default>` whose message names the
branch's commits (else any that changed the same files) carries the DIFF OF THE WHOLE BATCH
(`merge-base..tip`) — every change block of every file, a block under 20 characters only as a whole
identical block — never the latest state of the touched files; a binary file, a link, a mode change or more
than 300 files that differ is NEVER proven (`riglib/removal_content.py` only EXPLAINS an unproven branch);
uncommitted files and a stash made on the branch are the first, clearest unsafe signal and are named first;
**`preserved`** — the tip is reachable
from ANOTHER branch of the same remote (`origin` for a worktree / local branch), confirmed live with
`git ls-remote` (a stale tracking ref of a deleted or moved branch proves nothing, an unreachable remote
proves nothing). That is the route for a PR closed as superseded: push the commits to a branch that
stays (`git push origin <sha>:refs/heads/archive/<name>-<sha8>`, the name the deny text prints), then delete. The target's own branch, a
same-named or tracked (`branch.<name>.merge`) remote branch and every branch the SAME `git push` also
deletes never count (`removing`:
deleting `a` and `b` on the remote together cannot be vouched for by `a` and `b`; a push that deletes a
branch AND rewrites another ref is denied outright, since the rewritten ref could be the holder). A
local branch / worktree removal touches no remote ref, so every remote holder stays. A holder is trusted at
the moment of the check: a LATER force-push or rebase of it is not a removal and is not seen, hence the
fresh per-commit archive name; two sessions deleting each other's only holder at the same instant can both
pass. Pushing the target's own branch, stashes and the reflog never count. The forge lookup is
gc's (`worktree_gc.fetch_raw_prs`: `gh`, or the Forgejo API for a `git.hyperide.ai` origin) — never
a second fetcher; an outage only removes the PR proof. Tests use REAL git (tmp bare origin + linked
worktrees, `tests/removal_helpers.py`) and a fake forge; every data-loss shape has a negative test
and mutation-checking the engine (break a condition → a test must fail) is part of changing it.

The guard parses conservatively: ONE simple command with literal targets is judged; chains,
subshells, substitutions, wrappers (`bash -c`/`eval`/`xargs`/`sudo`/`env`), assignment prefixes,
variables/globs/braces/`~` in a target, a git/`worktree` subcommand or a flag assembled from an
expansion (`git $S -D x`, `git branch -$L x`, a bare `$FLAG` among `git branch`'s arguments),
`--git-dir`/`-c`/`--work-tree`, symlinked/unknown paths, `push --mirror/--prune` and `git update-ref -d` of a branch / `HEAD` / `--stdin` are DENIED,
never guessed; `$'-D'` is decoded, `${IFS}`/`$VAR` splitting is normalized away before the check,
and long options match by any unambiguous prefix (`--d`, `--del`). Known gap: a `git push` whose
`--delete`/`:name` arrives whole inside a variable (`git push origin "$D" x`) is not caught. Quoted/heredoc text that only mentions a removal
passes. There is NO bypass flag — `--force` does not change a verdict and no env var skips it; work
you really mean to drop goes through `rig worktree discard` (owner approval) or is kept first (`git add -A
&& git diff --binary HEAD > ~/.cache/<repo>-recovery/<wt>-<ts>.patch` plus pushing the commits to a branch that stays,
`archive/<name>`). Every allow/deny is
appended to `~/.cache/rig/removal-guard.jsonl`. `rig worktree gc` keeps its own CLASSIFIER but not
its own removal decision: every removal (dry-run plan and `--yes` execution alike) goes through the
same verdict via `worktree_gc._RemovalGate` — a nominee the verdict calls UNSAFE is reported `kept —
removal verdict UNSAFE — <reasons>`, and the branch gc deletes must still sit at the commit the verdict
judged (the worktree may have been re-pointed since planning). `--yes`/`--include-stale` widen what gc
nominates, never what the verdict accepts. The gate refreshes `origin/<default>` once per run and asks
the forge at most once per run.

**Denials explain themselves (rig-cli#757).** `riglib/removal_explain.py` is the ONE wording of every
refusal (the guard, `rig worktree remove`, `rig worktree verdict`), built from the verdict's reasons and
the `removal_content.Evidence` — never a second computation: WHAT I FOUND (the unlanded commits, a
file-by-file count of "already on main / differs where main has newer changes / only on the branch", the
missing lines quoted, and PR leads: the PRs opened from the branch and the merged PRs that changed the
same files — leads, never proof), WHY I STOPPED, and a numbered WHAT TO DO: land it (or keep the
commits on `archive/<name>`), `rig worktree discard <target> --reason "…"` for obsolete work, and the
plain `git` command the OWNER can run in their own terminal (the guard only watches agent tool calls).
The discard route is offered only when every reason is `not-landed`; a dirty tree, a running process,
the default branch or a branch checked out elsewhere get "fix that first". A command the guard cannot
parse gets `explain_unparsed`: why, plus how to rerun it as one plain command. The technical
`[not-landed] …` lines follow the friendly part so logs and tests keep the codes.

**Dropping obsolete work — `rig worktree discard` (rig-cli#757).** `riglib/worktree_discard.py`. The
target is judged by the same verdict; SAFE is simply removed. UNSAFE is refused (exit 10) unless EVERY
reason is `not-landed`. Otherwise the OWNER is asked on Telegram through
`agenttools_hatch_escalation` (hook id `rig-worktree-discard`; `RIG_HATCH_REQUEST_RIG_WORKTREE_DISCARD`
is set from `--reason`): the card carries the findings, the reason and the exact commit; deny-by-default
(no answer, no `tg-ctl`, a denial, an unparseable reply all refuse; a late approval is picked up first by
asking with the variable unset). The card is in decision-request shape (Context with file:line, Glossary, Options
table, Recommendation, Where to look) and covers ONE batch: several targets in one command are one tap; one
target that an approval cannot settle stops the batch before anybody is asked. There is NO backup (the reflog
is the way back) and this is the rare hard case — NOT merged AND stale/obsolete — never a routine confirmation.
Then it re-checks the judged tip and deletes (`remove_judged` for a worktree, `branch -D` after a tip
check, `push --force-with-lease=<ref>:<tip> --delete` for a remote branch). There is no `--force`; the
hatch helper is loaded ONLY from the fixed `catalog.DEFAULT_SOURCE_CANDIDATES` (never
`RIG_AGENT_TOOLS_SOURCE`, never the working tree) and only if its file is owned by this user or root
and not group/world-writable, so a repository, a fake `RIG_AGENT_TOOLS_SOURCE` or another account cannot
swap in an approver that says yes. Known limit (the hatch helper's own trust model, same as its `tg-ctl`
lookup): an agent running as THIS user with unconfined writes can overwrite a user-owned file of the
agent-tools checkout like any other file it owns — confining those writes is the job of the write hooks
(`worktree-only-writes`, `no-shell-file-edit`). Every attempt is
logged to `~/.cache/rig/removal-guard.jsonl`. Tests inject the approver; the loader tests run the real
path against a fake agent-tools checkout.

**Overwrites and `rm -r` (rig-cli#740).** Re-pointing a branch loses its old tip as surely as deleting
it, so the guard also judges `git branch -f|-M|-C` (and `-m`/`-c` combined with `-f`/`--force`),
`git checkout -B` and `git switch -C|--force-create`: the DESTINATION branch's current tip goes through
`removal_verdict.overwrite_verdict` — the same proofs as a deletion, except that a fast-forward (the new
start contains the old tip), a branch that does not exist yet, and being the default branch are not
reasons. A start-point from an expansion only loses the fast-forward exemption; a destination from an
expansion is denied. And `rm -r <path>`: every literal operand (an unquoted glob is expanded, `~`/`$HOME`
prefixes resolved, a symlinked spelling checked too) is compared with the EXISTING linked worktrees of the
cwd's repository, of the repository the operand sits in, and of any repository found by a bounded scan
inside it (depth 4, 5000 entries, no `node_modules`/`.git`); a worktree that IS or sits under an operand
gets the worktree verdict, and one UNSAFE one denies. Overwrites and `rm -r` are judged per command even
inside a chain (`git fetch && git checkout -B x origin/x`), but a `cd`/`pushd` anywhere in the command
makes relative paths and the cwd-resolved repository unknowable (denied; use absolute paths / `git -C
<abs>`). Operands from a variable/substitution/brace, `xargs`/`find`/`parallel`-fed `rm -r` and
`bash -c`/`eval`/`ssh` strings are denied. NOT judged (documented limits): `rm -r` of a SUB-directory of
a worktree (ordinary editing), of a primary checkout or a parent directory whose worktrees are all SAFE
(its local-only branches, stashes and untracked files are not examined), and a tree too large for the scan
cap; `find -delete`, `rsync --delete`, `mv` and interpreters are out of scope (agent-tools#966).

**The hook pins the rig it talks to (agent-tools#961).** `rig apply` rewrites the exact argument
`/ABSOLUTE/PATH/TO/rig` in an agent-hook descriptor's `args` to the absolute path of the launcher that ran
the apply (`riglib/actions/runner.py::pinned_rig_path`: `sys.argv[0]` when it is an executable file named
`rig`, else `rig` on PATH, else the install fails loudly — never a bare `rig`). The agent-tools
`block-unlanded-removal` hook reads `--rig <path>` instead of resolving `rig` through its own PATH, so a
shadowing binary cannot answer for the guard; a moved or reinstalled rig shows up as drift and a re-apply
re-pins it. A rig older than the first one that has `worktree guard` degrades the hook as documented in
its README.

**Known gap (GH-329, tracked as agent-tools#967):** the agent-tools skill
`skills/by-type/monorepo/worktree-isolation` prefers `rig worktree create` in prose but its Pattern
block still teaches a RAW `git worktree add ../.worktrees/agent-$run_id -b work/$run_id` (a
sibling-of-repo location, one of the very conventions this section replaces) and cleans up with `git
worktree remove --force`. That skill lives in the agent-tools repo, which this repo consumes read-only
(see "The integration seam" above) — rig-cli cannot fix it here.

## Harness workflow guards (worktree-only + orchestrator-only)

`rig apply` installs agent-hooks from THREE hook directories (from agent-tools, via
`agent_hooks.all`) that provision the harness workflow. Each is configured PER REPO by a boolean
in that repo's committed `rig.yaml`
(the hook scripts self-read `agent_hooks.<key>` at fire time — `rig apply` does not consume the
value, so changing enforcement needs no re-apply):

- **`agent_hooks.worktree_only`** (default **false**, opt-IN) — gates TWO hooks together (one
  feature, one flag):
  - `worktree-only-writes` (pre-write) denies an Edit/Write while the checkout is on the repo's
    default branch.
  - `pin-primary-worktree` (pre-bash, agent-tools#182/#183) denies a `git checkout`/`git switch`
    that would move the repo's PRIMARY worktree (never a linked `git worktree add` tree) onto
    anything but the default branch. Added after `worktree-only-writes` alone failed to catch a
    real incident (Alex tg#6462/tg#6477, 2026-07-04): a bare `git checkout <branch>` in the
    shared primary checkout is invisible to a pre-*write* hook, and once ON a feature branch,
    `worktree-only-writes`'s own branch check can't tell "the primary checkout" from "a linked
    worktree" — this hook is the piece that actually distinguishes them.
  Together: all authoring goes in a SEPARATE worktree on a feature branch; the PRIMARY checkout
  is for merge/pull/read-only. Enrol `hyperide` + the agent-ecosystem repos (`worktree_only:
  true`); leave it off for repos that legitimately work on main (`3d-cli`). **Caution when
  enrolling a repo**: audit any sanctioned automated flow (release scripts, `gh ship` wrappers)
  for a `git checkout`/`switch` to a non-default branch in the primary checkout FIRST — this
  guard will block it too, with no special-casing. No self-service env bypass — each hook has its
  OWN hatch var, not a shared one: a deliberate one-off Edit/Write on main is requested via
  `RIG_HATCH_REQUEST_WORKTREE_ONLY_WRITES="<justification>"`; a one-off `git checkout`/`switch` in
  the primary checkout is requested separately via
  `RIG_HATCH_REQUEST_PIN_PRIMARY_WORKTREE="<justification>"` (both: tg approval, deny-by-default,
  bare `1` rejected). (Alex tg#5742, tg#6462/tg#6477.)
- **`agent_hooks.orchestrator_only`** (default **true**, opt-OUT) — the `orchestrator-stays-thin`
  hook warns on the first implementation-shaped Bash/code-Edit by the main thread, then blocks a
  repeat within its TTL (both descriptors, `pre-bash` and `pre-write`, are provisioned — the
  catalog installs every descriptor a hook directory ships, not just the first alphabetically).
  Read-only inspection (`git status`/`ls`/`cat`/`grep`/`find`, `git worktree list`) is never
  gated; `tg`/`review`/`task` are sanctioned orchestration, also never gated. **Most `gh` is still
  delegated to a subagent, not inline orchestrator work** — `gh pr merge/create/close/edit`, `gh
  pr checkout`, `gh issue create/close/edit`, `gh release`, `gh workflow`, `gh secret`, and any
  `gh api` mutation all warn-then-block the main thread the same as any other implementation-
  shaped Bash. TWO narrow, explicit exceptions run inline, never gated: (1) a genuinely UNCHAINED
  `gh ship <PR#>` — the whole, sole command on the line (agent-tools#159/#363); (2) a FIXED
  read-only allowlist (Alex, 2026-09-18, session-authorized relaxation of the blanket gh gate) —
  `gh issue list/view`, `gh pr list/view/checks/diff`, `gh run list/view/watch`, `gh repo view`,
  and a `gh api` call carrying no `-X`/`--method` flag other than GET (absence also counts as
  GET) and no `-f`/`--field`, `-F`/`--raw-field`, or `--input` data-payload flag — granted
  per-segment like `tg`/`task`, so it chains freely with itself/other sanctioned commands, unlike
  `gh ship`'s per-LINE-only grant. A gh invocation matching neither exception still
  warn-then-blocks. Set `orchestrator_only: false` to exempt a repo entirely. No self-service env
  bypass; a one-off is requested via `RIG_HATCH_REQUEST_ORCHESTRATOR_STAYS_THIN="<justification>"`
  (tg approval, deny-by-default; bare `1` rejected). (Alex tg#5743, tg#7103; allowlist enforced by
  agent-tools#764, 2026-09-18.)

All three are complementary to the pre-push `protect-main` git-hook: that blocks a *push* to
main, these block the *authoring* / inline work that precedes it. Scope note: these are Claude
Code agent-hooks specifically — a different harness (e.g. a raw Codex/script invocation) issuing
`git checkout` is not intercepted by them; git itself has no `pre-checkout` hook to fall back on.
The primary-vs-linked-worktree distinction (`--git-dir` vs `--git-common-dir`) is the most fragile
part of `pin-primary-worktree`'s logic — its regression coverage lives in agent-tools'
`tests/test_pin_primary_worktree.py` (real temp repos + real `git worktree add` trees, not
mocks), not in this repo. See `docs/config-schema.md#agent_hooks`.

## Tests

- `python -m pytest -q` — the unit suite. Fast, hermetic; uses a fake agent-tools checkout
  (`tests/conftest.py::fake_agent_tools`) and `tmp_path` — tests never touch the real HOME
  or a real agent-tools checkout. The autouse guards `RIG_TMUX_DRY_RUN=1` /
  `_isolate_scheduler` keep the tmux live-activation + the scheduler out of the suite.
- `RIG_TMUX_E2E=1 python -m pytest -q tests/test_tmux_e2e.py` — the **opt-in** real-tmux e2e
  (the acceptance gate for the tmux reboot cycle: it drives a REAL tmux server on a private
  `-L` socket and clones the real plugins, so it needs tmux + git + network). It is OFF in the
  default `pytest` run to keep that hermetic; the tmux BFS / artifact logic it proves is ALSO
  covered hermetically by the unit suite (`test_pane_has_claude_*` etc.). Auto-skips offline.
- `RIG_CLEANROOM_E2E=1 python -m pytest -q tests/test_cleanroom_e2e.py` — the **opt-in**
  clean-room / Docker e2e: `rig init` as a BRAND-NEW user on a pristine machine. It builds a
  fresh `python:3.x-slim` container with a non-root user + an empty `$HOME` and a self-contained
  fake agent-tools checkout, then runs the REAL CLI end to end and asserts the four acceptance
  points — skills harness-discoverable (`~/.claude/skills` symlinks resolve), hooks / dispatcher /
  CI / auto-mode + the CC hook-bridge installed, idempotent re-apply, `rig status` clean. The
  container RUN is OFFLINE (`docker run --network none`); the one-time image BUILD still needs
  apt/PyPI egress. Needs a running Docker daemon; auto-skips when absent.
  Unlike `smoke.sh` (a tmp-`$HOME` on the DEV machine, which inherits the dev's installed rig /
  git config / `~/.claude` history), this proves the first-run experience for a stranger on a
  machine that has never seen rig.
- `bash tests/smoke.sh` — end-to-end: `--help`, `doctor`, a headless `init` against a
  sample config in a throwaway repo with an isolated `HOME`, idempotency, status, pytest.
  Needs a real agent-tools checkout (`RIG_AGENT_TOOLS_SOURCE`); self-skips the apply leg
  without one. The init leg sets `RIG_TMUX_DRY_RUN=1` so the tmux artifacts land without the
  live activation. Its **full-coverage leg** (`_real_catalog_full_coverage`) discovers AND
  dry-run-plans EVERY item in the REAL catalog (`all:true` across skills/agent_hooks/ci/mcp +
  dispatcher) and asserts zero unknown-item/slot errors plus that every `ci/<slot>/` on disk
  is in the plan — the rig↔catalog drift guard the synthetic `pytest` fixture can't give (a
  new slot / renamed dir rig can't resolve is unit-GREEN but live-BROKEN; "unknown ci item:
  pr-checklist" was this class). The `real-catalog-smoke` CI job (`.github/workflows/ci.yml`)
  checks out the public `alex-mextner/agent-tools` repo and runs this — best-effort, so an
  unreachable catalog SKIPS the job loudly (`::warning::`) instead of hard-failing CI. The
  opt-in pytest mirror is `RIG_SMOKE_FAST_E2E=1 … test_real_catalog_full_coverage_plans_every_item`.
- `bash tests/smoke.sh --fast` — the **pre-commit subset** (seconds, not the full ~20s run).
  Runs only the cheap REAL-catalog legs (`--help`/`--version`/`doctor`/`setup`-usage and the
  `rig status` regression legs: a clean sample exits 0, a removed slot prints the 3-part error
  + exit 4, a non-git dir doesn't nag). SKIPS the heavy `rig init --yes` apply and the full
  pytest — those stay in CI. This is the subset the repo-local pre-commit gate runs.

### The local pre-commit smoke gate (the CTO 2026-06-16 requirement)

Two same-day prod failures were unit-GREEN but smoke-BROKEN (a stale `mcp.items.review`, a
removed slot) and were caught only AFTER push, because smoke ran in CI but did not gate the
commit locally. To close the LOCAL half:

- `scripts/install-smoke-precommit.sh` — run **once per clone/worktree** to wire a
  `.git/hooks/pre-commit` shim that execs the tracked gate. Idempotent; safe to re-run. Refuses
  to mangle a symlinked pre-commit (a hook manager owns it); expands a `~`-prefixed
  `core.hooksPath`.
- `scripts/smoke-precommit-hook.sh` — the tracked gate: runs `bash tests/smoke.sh --fast` and
  blocks the commit on failure, then chains the global git-hook dispatcher. Dedup is FAIL-SAFE:
  it skips its own chain ONLY when a global `core.hooksPath` composer is active AND that
  composer's `pre-commit` actually invokes the dispatcher (a path-canonicalized match, robust to
  `~`/`$HOME`/symlink variants) — so the common composer setup never double-runs secret-scan. In
  the rare PREPEND case (the installer prepended the gate ahead of a foreign hook that ALSO runs
  the dispatcher), the gate keeps chaining and the scan may run twice — a deliberate trade: a
  duplicate read-only scan is harmless, whereas guessing wrong and DROPPING it is a security
  hole. Bypass (discouraged): `SKIP_RIG_SMOKE=1 git commit …`.
- **No agent-tools checkout? Not blocked.** `tests/smoke.sh --fast` self-skips the catalog legs
  (apply + the real-catalog `rig status` legs) and exits 0 when no `agent-tools` source is
  found — so a contributor without one still commits; the catalog regression guard simply fires
  for those who have the checkout (and always in CI). It is NOT a hard requirement to commit.
- **Global `core.hooksPath` caveat.** The installer writes `<git-dir>/hooks/pre-commit`. The
  rig global composer (`core.hooksPath = ~/.config/git/hooks`) trampolines into that file, so
  the gate fires under it. But if you set a DIFFERENT global `core.hooksPath` that does NOT call
  `$git_dir/hooks/pre-commit`, git runs only that global hook and the gate is bypassed — wire
  the gate into your global hook (or run the rig composer) in that case.
- Both are covered by `tests/test_smoke_precommit.py` (drives a real `git commit` through the
  installed hook in a HOME-isolated throwaway repo and asserts allow/block).
- CI keeps running the FULL `tests/smoke.sh` (the `--fast` gate is the local complement, not a
  replacement).
- Add a test with every behavior change. TDD red-first is the house style.

## Style

- Conventional commits.
- English-only code, comments, and docs.
- No dead code, no underscore-prefixed unused params, no `as-unknown-as` escape hatches.
- Keep `cli.py` thin (argparse + dispatch); behavior lives in the sibling modules.

# >>> rig-managed orchestrator (do not edit) >>>
# Orchestrator, not worker. Do not implement inline. Do not run `gh` except an unchained
# `gh ship <N>`, or read-only `gh issue` list/view, `gh pr` list/view/checks/diff, `gh run`
# list/view/watch, `gh repo view`, GET-only `gh api` (no -f/-F/--input). Do not `git
# worktree add` — `rig worktree create`.
# Read-only (`git status`, `ls`, `cat`, `grep`, `git worktree list`) is fine. Dispatch a subagent for the rest. Skill: delegate-work-to-subagents. Hook: orchestrator-stays-thin.
# Key reports MUST go to Telegram (`tg --tag report`): quote the user's original words, include ticket/PR URLs, name the measure already started, draft via `review just-ask`. Never "not in prod" with no next action.
# After a successful `gh ship` of a load-bearing feature, merge is not done: (1) turn it on
# in settings if it has a config knob, (2) `rig apply commit` on this machine, (3) prove it
# WORKS on this machine (command output, Telegram, deny text — SHA/on-disk is not a proof), (4) re-check the original user quote is solved.
# A helper yield is not done — do not tick the todo until the original ask is live-proven; wake parked helpers via hub send.
# <<< rig-managed orchestrator (do not edit) <<<

# >>> rig-managed autonomous-mode (do not edit) >>>
# Mode autonomous: extra-approval gate OFF. Do not invent a hold-for-human.
# Ship when gates pass. Skill: autonomous-mode (skill://autonomous-mode).
# <<< rig-managed autonomous-mode (do not edit) <<<
