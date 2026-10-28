[← Back to the config schema index](../config-schema.md)

# `rig.yaml` schema — GitHub, tmux, spotlight and fj

Reference part of [config-schema.md](../config-schema.md): `github`, the machine-owned external PR review policy, `tmux` (+ `tmux.recovery`), `spotlight`, `fj`.

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

A softer, conditional variant (agent-tools#879) lists owners whose PRs with zero
GitHub-side reviews pass the gate on ship's own evidence instead of a review:

```yaml
ci:
  items:
    ship:
      external_review_evidence_owners:
        - o-maan
```

For a listed owner ship defers the external-review decision until after its
acceptance gate and passes only when CI is green (or the hatch-gated `--skip-ci`
path), the review quorum was met, the acceptance gate proved every criterion,
and there are no unresolved review threads or unaddressed PR comments; any miss
falls back to the usual Telegram hatch. An owner also opted out by
`require_external_review: false` stays fully opted out. Same machine-only,
exact-owner rules as `external_review_owners`. Set it with
`rig config set ci.items.ship.external_review_evidence_owners o-maan --global
--no-apply`.

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
| `autosave.enabled` | bool | `true` | provision an **independent** launchd saver (`StartInterval` = `continuum.save_interval` minutes) that calls resurrect `save.sh` directly — decoupled from continuum's fragile status-right hook. When on, rig emits `@continuum-save-interval '0'` so there is exactly **one** authoritative saver (no racing writers over the `last` symlink). The wrapper guards against no-server, empty servers, and a **degenerate save** (a bare boot-time `main` never clobbers a richer prior snapshot), and writes a health-state file + log line so a silent month-long death is impossible. Runs are single-flight via a `mkdir` lock that records the holder pid; a lock left by a run that died without cleanup (dead holder pid, or older than `stale_after`, including a legacy empty lock) is logged (`stale lock …: holder pid …, <reason>, age …s`), broken and re-taken once, while a live holder younger than `stale_after` is respected (`busy: lock held, skipping`). |
| `autosave.label` | str | `ai.hyperide.tmux-autosave` | the autosave launchd agent label (and plist filename stem) |
| `autosave.stale_after` | int ≥ 1 | `45` | minutes: the freshness threshold. After each run the saver logs a `WARN` in the health file when the newest snapshot is older than this; **`rig status` reads that health record and surfaces a `tmux autosave agent: STALE …` line** when the agent's last run is older than `stale_after` (i.e. the saver stopped firing) — the observability that turns a silent month-long death into a visible one within one interval. The line is advisory (it does not change the status exit code) and only appears when the agent's launchd plist is actually installed. It is also the age after which the wrapper breaks a leftover single-flight lock even if its recorded holder is still alive (a hung run). |
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

**Boot marker — a slow boot must not skip the restore (rig-cli#674).** tmux-continuum starts its
layout restore only when the tmux server is younger than `@continuum-restore-max-delay` (default
10 s) at the moment its init runs during the conf load. After a reboot the load average can
stretch that conf load past 10 s (≈16 s at load 140 on 2026-10-01), so the restore is silently
skipped and `main` stays a bare one-pane session. So the boot script writes
`<generated_dir>/.tmux-boot.marker` (`<epoch> <pid>`, via a temp name + rename) right before
`new-session -d` and removes it on every exit path it controls (normal exit, error, `HUP`/`INT`/
`TERM`; it removes only a marker whose pid is its own — a best-effort, non-atomic check: two boots
racing within microseconds can at worst cost one of them the widened window). The
generated `rig.tmux.conf` then carries, BEFORE the continuum init,
`if-shell "<marker younger than 10 minutes AND its pid is a live tmux-boot.sh>" "set -g
@continuum-restore-max-delay 600"` and, right AFTER it, `set -gu @continuum-restore-max-delay` —
so only a boot-script-started server gets the 10-minute window; tpm's re-run of the plugin and
any later `tmux source-file` on a live server use the default 10 s window and can never restore
over live windows. A marker leaked by a SIGKILLed boot script (no trap can run) names a pid that
is dead — or, if the OS reused it, no longer a `tmux-boot.sh` — so it stops counting at once,
and its age bound is 10 minutes in any case. Both lines exist only when `boot.enabled`
and `continuum.restore` are on. No config key: the window and marker name are constants in
`riglib/tmux.py` (`BOOT_RESTORE_WINDOW_MINUTES`, `BOOT_MARKER_NAME`).

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

Persistent interactive sessions are hosted by `rig harness run omp` (abduco + a crash supervisor,
see [`rig harness`](harness-and-permissions.md#rig-harness--hosted-harness-sessions-for-omp-claude-code-codex-opencode)); the
pane map and restore above relaunch a dead original with `rig harness run omp --host-id ID
--resume EXACT_FILE`, only by explicit history identity. Existing unwrapped live processes
need a one-time saved stop/resume to enter this durable lifecycle; a live legacy process is
never adopted by duplicating it.

`tg-ctl omp-live` is preferred for exact live identity; when absent, rig inspects the original
process's own TTY breadcrumb/open transcript. If no inspector is available, recovery blocks
rather than interpreting that as an empty machine. The transport requires the original-process
extension; see tg-cli's `docs/omp-original-process.md` for installation and receipt semantics.


The opt-in reproducible real-process proof is `python3 scripts/verify-omp-recovery.py
--tg-cli-root /path/to/tg-cli --omp-bin /path/to/omp --output /private/evidence-dir`.
It uses only a new private tmux socket, a no-tools OMP, and one short model acknowledgment;
it verifies socket recreation, server death/recovery, exact map identity, and SAME original
PID/instance/session after reattach. It does not stop or restart the default tmux server.


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
  # git_credentials: true      # git over HTTPS to <host> uses fj's login (rig's git-credential-fj)
  # client_id: …               # default: Forgejo's built-in `tea` OAuth app
```

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `enabled` | bool | — | block **presence** opts in; `false` opts out |
| `host` | str | — (required) | the Forgejo host; a scheme/trailing path is stripped |
| `install` | bool | `true` | manage the `fj` command: the real fj + the wrapper that adds `fj ship`; `false` leaves fj entirely to you (no wrapper, no `fj ship`) |
| `version` | str | `0.6.0` | release rig downloads (only versions with a pinned sha256) |
| `bin_dir` | str | `~/.local/bin` | where the `fj` wrapper goes — must be on PATH, ahead of any other `fj` |
| `git_credentials` | bool | `true` | git over HTTPS to the global host uses fj's login: rig's helper `<bin_dir>/git-credential-fj` as `credential.https://<host>.helper` (below); `false` leaves git's credential config to you — rig neither writes nor removes anything there |
| `client_id` | str | Forgejo `tea` app | OAuth client `fj auth login` uses |

`rig apply` (one `provision_fj` action): sets up the `fj` command (below); lists each host in fj's
`client_ids` file (`~/.config/forgejo-cli/client_ids`; `%APPDATA%\Cyborus\forgejo-cli\config\client_ids`
on Windows) with the client ID of Forgejo's **built-in public OAuth app** — every instance ships them
(`[oauth2] DEFAULT_APPLICATIONS`), so no admin registers anything; sets `FJ_FALLBACK_HOST=https://<host>`
(through the `env` area on macOS/Linux — a hand-set `env.vars.FJ_FALLBACK_HOST` wins, and
`env: {enabled: false}` turns the export off there, so set it in your shell profile yourself —, as a
user environment variable via `setx` on Windows); and makes git use fj's login (below). Lines rig
does not own in `client_ids` are kept. `rig status` reports the same steps as drift. `RIG_FJ_DRY_RUN=1`
makes apply report instead of act.

**git uses fj's login — one `fj auth login` for both.** rig writes its own git credential helper,
`<bin_dir>/git-credential-fj` (POSIX `sh`, marked `rig-managed git credential helper`; it carries the
path of fj's `keys.json` — `${XDG_DATA_HOME:-~/.local/share}/forgejo-cli/keys.json` on Linux,
`~/Library/Application Support/forgejo-cli.forgejo-cli/keys.json` on macOS,
`%APPDATA%\forgejo-cli\forgejo-cli\data\keys.json` on Windows — and nothing else machine-specific).
On `git push`/`fetch` to `https://<host>` git asks it; for an OAuth login it first runs
`fj --host <host> whoami` (fj refreshes an expired token and saves it before any API call), then
prints `username=fj` + `password=<token>` (Forgejo takes the token as the password whatever the user
name). A pasted token (`fj auth add-token`) is handed over as is; no login at all → no answer (git
falls through to its usual error) and `run: fj --host <host> auth login` on stderr; never anything
for plain `http` or another host. `store`/`erase` do nothing — fj owns the token. In the global git
config rig makes `credential.https://<host>.helper` **end with an empty value and then the helper**:
git drops every helper it collected before an empty value, so a global/system `credential.helper`
(`store`, `cache`, GCM — Git for Windows sets `manager` system-wide) can never answer first with a
stale password. The value is the helper's absolute path on macOS/Linux (works without `bin_dir` on a
daemon's PATH) and `fj` — git's short name for `git-credential-fj` on PATH — on Windows, where
Cygwin git and Git for Windows disagree on absolute path forms. Only the GLOBAL host gets it. rig
keeps no record of earlier hosts: after changing the global `fj.host`, the old host's entries stay
(harmless — the helper hands a host only fj's token for that same host); drop them with
`git config --global --unset-all credential.https://<old host>.helper`.

rig owns only its own values in that key (the helper, empty resets) and re-appends them at the end
when anything follows them. **A helper rig did not write** (e.g. a hand-made `helper = forgejo`) is
**kept** — the empty reset after it already makes git skip it — and reported as drift with the
exact `git config --global --fixed-value --unset-all …` to remove it; apply fails that step until
you do. A file at `<bin_dir>/git-credential-fj` that is not rig's is never overwritten, and git is
then not pointed at it. The Git Credential Manager settings older rigs wrote
(`credential.https://<host>.oauthClientId` / `oauthRedirectUri` / `oauthAuthorizeEndpoint` /
`oauthTokenEndpoint`) are removed by apply while they still hold rig's exact values. Their config
key `fj.git_client_id` is gone too: a config still setting it fails to load and says to delete it.
Needs git **2.30 or newer**: rig removes only its own values with `git config --fixed-value`, which
older git rejects (apply then fails that step with git's own error).

**`fj ship` — the same command as `gh ship`.** fj has no alias mechanism, so `fj` on PATH is a thin
rig wrapper, `<bin_dir>/fj` (POSIX `sh`, a constant per fj version — no machine path in it).
`fj ship …` runs the **very dispatch body of the `gh ship` alias** (one function renders both, see
[`ship_delegator`](project-tooling.md#ship_delegator)): the repo's `.claude/scripts/pr-ship.sh`, else
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
on macOS; apply reports it, only your shell profile can fix it), `git-credential-fj` (the helper
missing, stale or a foreign file in the way), `git-credential:<host>` (rig's helper entries missing
or not last, a helper rig did not write, leftover GCM settings). A PATH without `bin_dir` and
without any other fj (a daemon's minimal PATH) is not reported. Not drift, but shown next to the
area (and appended to apply's line): fj is not logged in to the global host yet, or its login there
is a pasted token — `fj --host <host> auth login` (it replaces the host's entry in place).

Logging in is then one command: `fj auth login` (browser → *Authorize*) — fj and git both use it. A
repo `rig.yaml` with its own `fj.host` adds that host to fj's `client_ids` on `rig apply` inside the
repo (a line you wrote for it yourself is kept); git's credential helper in `~/.gitconfig` is set for
the GLOBAL host only, so a cloned repo naming `github.com` or a company server cannot repoint git's
login for it. `FJ_FALLBACK_HOST` keeps pointing at the GLOBAL host (and is not set at all without
one). `fj.host` is the only key a repo (or `--config`) layer may set: every other `fj` key changes
the machine's fj install, the OAuth app fj logs in with, or the global git config, so the loader
rejects it outside `~/.config/rig/config.yaml` — a cloned repo cannot redirect your logins. fj
refreshes its OAuth token itself (and the git helper has it do so); Forgejo drops a refresh token
unused for `[oauth2] REFRESH_TOKEN_EXPIRATION_TIME` (default 730 h) — a machine idle that long runs
`fj auth login` again. Headless: `fj auth add-token` (git then uses that token too).

---
