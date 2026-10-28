[← Back to the config schema index](../config-schema.md)

# `rig.yaml` schema — Project tooling

Reference part of [config-schema.md](../config-schema.md): `scripts`/`dev`, `models`, `agents_md`, `ship_delegator`, `lint_checks`, `lint`, `linters`, `project_tools`, `ecosystem_readme`, `gitignore`, `bun`, `git`, `profile`.

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
    # label: ai.hyperide.model-freshness   # launchd Label / systemd unit name (advanced)
  # checker_path: ~/xp/agent-tools/lib/checker/model_freshness.py   # default: resolved from agent_tools_source
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` (scaffold) | provision the daily schedule (set `false` to leave the system cron untouched) |
| `schedule.time` | `HH:MM` | `12:00` (noon) | daily run time, 24-hour; fail-closed on a malformed/out-of-range value |
| `schedule.label` | str | `ai.hyperide.model-freshness` | the launchd Label / systemd unit name (one string across platforms) |
| `checker_path` | path | resolved from `agent_tools_source` | the `model_freshness.py` the schedule runs |

**Cross-platform.** The CTO asked for a "cron"; rig provisions the platform-native
equivalent:

- **macOS → launchd.** A `~/Library/LaunchAgents/<label>.plist` with a daily
  `StartCalendarInterval` (Hour/Minute), loaded via `launchctl load`. (cron is
  deprecated/unmanaged on macOS; launchd is the supported scheduler.)
- **Linux → systemd --user timer.** `~/.config/systemd/user/<label>.service` (oneshot; runs
  `python3 <checker>` through `bash -lc` so the login PATH and provider env apply) plus
  `<label>.timer` (`OnCalendar=*-*-* HH:MM:00`, `Persistent=true`), enabled with `systemctl
  --user enable --now`. Visible in `systemctl --user list-timers`; output lands in the journal
  (`journalctl --user -u <label>.service`). Not crontab: agent shells run with `NoNewPrivs=1`,
  which stops the setgid `crontab` binary from reading or writing the spool, so an agent-run
  `rig apply` could never install or verify it (rig-cli#721). A crontab line left by an older
  rig (`# rig-managed: <label>` sentinel) is removed on the next apply when the crontab is
  readable.

**Idempotency (the "check whether the cron exists and install it if missing" rule).** Both `init` and `apply`
run this. A present-and-current schedule re-applies as a no-op (`skipped`); a missing or
drifted one is (re)installed. `rig status` reports the schedule explicitly (installed /
drifted / not configured) and surfaces a wrong run time or checker path as drift. A global
config with NO `models:` block (written before the scaffold default gained it) is drift too,
naming the enabling command `rig config set --global models.enabled true`; an explicit
`enabled: false` stays quiet. `rig doctor` flags a missing scheduler binary
(`launchctl`/`systemctl`) and a scheduler rig cannot write (no reachable systemd user manager,
or an unwritable `~/.config/systemd/user` / `~/Library/LaunchAgents`).

Set `RIG_SCHEDULE_DRY_RUN=1` to write the artifact file but **skip the live daemon mutation**
(no `launchctl load`, no `systemctl --user enable`) — for CI, containers, or smoke tests where touching
the real per-user scheduler is unwanted. The same skip happens **automatically when `$HOME` is
overridden** (the resolved `Path.home()` differs from the uid's real login home): a HOME-only
sandbox cannot redirect the per-user launchd/systemd manager, so rig writes the artifact and reports
`HOME is overridden (<home> != <real home>) — skipped live launchctl load (sandboxed run)` instead
of mutating the real scheduler (rig-cli#116; applies to `tmux`, `tg_ctl`, `spotlight` too). The
post-apply verify skips its loaded-check and `rig status` suppresses the live loaded-probe (and, on
Linux, the timer-active check) under the same condition, so a sandboxed apply exits 0 and
stays converged on both the launchd and systemd platforms. Note `sudo rig apply` with a
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
marker-managed (see [managed-files.md](../managed-files.md)).

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

**`fj ship` is the same command.** On a machine with the [`fj`](github-and-tmux.md#fj) block, `fj ship <PR>` runs the
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

## `lint_checks`

Knobs for `rig lint check`: per-function cognitive/cyclomatic complexity (official `cccc`, pinned and
sha256-verified) and physical file-line limits. **Default ON**, independent of `linters.*` (no Oxc or
Biome needed). Valid in **both** the global config and a repo `rig.yaml`; the repo value wins per key.
Every layer is validated alone and the merged result is validated again; unknown keys, wrong types
(bool/float/string/null where an integer is required), values outside `1..10000000`, or a merged
`warn_above > error_above` fail closed, naming the key and the layer.

```yaml
lint_checks:
  enabled: true              # master switch; false => status "disabled" there. In the GLOBAL layer it also removes the machine-wide analyzer + hook
  complexity:
    enabled: true
    max_cognitive: 15        # integer >= 1; a value strictly above the limit is an error
    max_cyclomatic: 20       # integer >= 1
  file_lines:
    enabled: true
    warn_above: 1200         # more lines than this => warning (must be <= error_above)
    error_above: 2400        # more lines than this => error
    exempt_extensions: [json]  # lowercase, no leading dot; lockfiles other than .json are NOT exempt
  ci:
    rig_source: git+https://git.hyperide.ai/ultrabricks/rig-cli@<40-hex commit sha>  # see below; unset => no CI workflow
```

Try it: `rig config set lint_checks.complexity.max_cognitive 18`, then `rig lint check --json`
(exit 0 ok/warnings only, 1 error finding, 2 usage/config error, 3 analysis incomplete,
127 analyzer missing or not matching the pin).

The generated CI workflow sees only the built-in defaults plus the repo's `rig.yaml` (no global
config), so thresholds CI must enforce belong in the repo's `rig.yaml`; a value set only in
`~/.config/rig/config.yaml` reaches local runs and the commit hook, never CI.

`lint_checks.ci.rig_source` is where the generated `.github/workflows/rig-lint-check.yml` installs `rig`
from (`pip install "<rig_source>"` in a fresh venv). It must be an **immutable** reference — exactly
`git+https://<host>/<path>@<full 40-hex commit sha>` (a branch, tag, PyPI spec or bare URL is rejected,
naming the key and the layer) — of a rig-cli commit that already contains `rig lint check`. It is
**unset by default and the workflow is then not written** (the plan prints a note): rig has no
published, installable release that carries the check (`hyper-rig==<version>` does not resolve on
PyPI for the running dev version), and an unresolvable pin would leave the CI job permanently red.
The workflow bytes depend on this setting alone — never on the rig version that rendered them — so a
rig upgrade does not churn every repo. With the repo's own `complexity.enabled: false` the workflow skips the `cccc`
download; a complexity opt-out that exists only in the global config keeps it, because CI never reads that file. The step installs
to `${XDG_DATA_HOME:-$HOME/.local/share}/rig/cccc/v<version>`, the same lookup `rig` uses.

**Machine-wide vs per-repo.** The analyzer (`install_cccc`) and the pre-commit hook are one per machine and
serve every repo. The **hook** is decided by the **global** layer (built-in defaults + `~/.config/rig/config.yaml`),
never by one repo turning the check off. The per-machine, harmless **analyzer** is installed whenever the global
layer or the repo being applied runs complexity (`enabled` and `complexity.enabled`), so a repo that enables the
check against a global opt-out gets the analyzer its own `rig lint check` needs (but no hook). A repo's
`lint_checks.enabled: false` or `complexity.enabled: false` only changes `rig lint check` in that repo and that
repo's generated workflow: it neither removes the machine-wide artifacts nor creates `extra` drift for the
installed hook. The hook is gated on a verified analyzer; a repo that enables complexity against a global
complexity opt-out gets exit 127 with the `rig apply commit` fix message until its own apply installs the analyzer
(loud, never a silent pass). To remove the machine-wide hook, disable `lint_checks` (or the
`git_hooks.dispatcher.fragments.lint-check` fragment) in the global config.


## `lint`

Knobs for `rig lint` (the aggregator that runs every applicable check: lint, typecheck, format, limits,
scripts): which checks run, their timeouts, and the local bootstrap of Oxlint/Oxfmt/Ruff. **Default ON.**
Valid in **both** the global config and a repo `rig.yaml`; the repo value wins per key (dicts merge
recursively). Every layer is validated alone and the merged result is validated again; unknown keys,
wrong types, unknown provider ids, non-exact versions and timeouts outside `1..3600` fail closed, naming
the key and the layer.

```yaml
lint:
  enabled: true              # master switch for `rig lint run` (NOT for `rig lint check` / `rig lint rules`)
  timeout_seconds: 300       # per-check timeout, integer 1..3600
  categories:                # false => that category's checks report `not-applicable`
    lint: true
    typecheck: true
    format: true
    limits: true
    scripts: true
  bootstrap:
    enabled: true            # may a bare local `rig lint` install missing tooling
    javascript: true         # Oxlint + Oxfmt for JS/TS scopes without a configured tool
    python: true             # Ruff for Python scopes without a configured tool
    versions: {}             # exact-version overrides, e.g. {ruff: "0.17.3"}
  providers:                 # open map keyed by provider id (eslint, ruff-check, shellcheck, ...)
    eslint: {enabled: true, timeout_seconds: 600}
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `lint.enabled` | bool | `true` | master switch for `rig lint run`. `rig lint check` and `rig lint rules` are unaffected |
| `lint.timeout_seconds` | int `1..3600` | `300` | default per-check timeout |
| `lint.categories.{lint,typecheck,format,limits,scripts}` | bool | `true` | `false` switches a whole category off; its checks report `not-applicable` |
| `lint.bootstrap.enabled` | bool | `true` | allow bare local `rig lint` to install missing tooling |
| `lint.bootstrap.javascript` | bool | `true` | bootstrap Oxlint + Oxfmt for JS/TS scopes that have no configured tool |
| `lint.bootstrap.python` | bool | `true` | bootstrap Ruff for Python scopes that have no configured tool |
| `lint.bootstrap.versions` | map | `{}` | exact-version overrides (`1.2.3`, optional `-pre`/`+build` suffix; no ranges) keyed by `oxlint`, `oxfmt`, `oxlint-tsgolint`, `@oxlint/plugins` or `ruff`; any other key is rejected |
| `lint.providers.<id>.enabled` | bool | `true` | `false` switches one check provider off (its checks report `not-applicable`) |
| `lint.providers.<id>.timeout_seconds` | int `1..3600` | `lint.timeout_seconds` | per-check timeout for that provider |

**Configuration only.** `lint` is not a reconciled area: `rig apply` writes and removes nothing for it and
`rig status` has no row for it. A provider's command and argv are **never** configurable — a provider can
only be switched off or given a different timeout. Tools the repo already configures stay authoritative;
bootstrap only fills in a missing JS/TS or Python tool, and **never runs in CI, with `--plan`, or with
`--no-setup`** (those stay read-only).

`lint_checks` thresholds (cognitive/cyclomatic complexity, file-line limits) are unchanged and independent:
the `limits` category runs `rig lint check` with whatever `lint_checks` resolves to. What each check does:
[lint-check.md](../lint-check.md).

Try it: `rig config set lint.timeout_seconds 600`, then `rig lint run --json`.

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

Where `rig profile` keeps this machine's **global config** and portable files in sync with your
other machines: a **private** git repo (conventionally `https://git.hyperide.ai/<login>/.rig-profile`)
holding `config.yaml` (a copy of `~/.config/rig/config.yaml`) plus an allow-list of other files.
**GLOBAL layer only**, rejected from a repo `rig.yaml` like `git`. Written by `rig profile init`; see
the README's [`rig profile`](../../README.md#rig-profile--carry-your-global-config-between-machines)
section for the workflow.

```yaml
profile:
  repo: https://git.hyperide.ai/alex/.rig-profile   # the PRIVATE profile repo
  # auto_sync: true                                  # default: `rig apply commit` syncs first
  # files: {tmux: false}                             # stop syncing one file
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `repo` | string | — | the profile repo URL (https, ssh, or a local path). One line, no leading `-`. `rig profile init` records it and never rewrites an existing value (machines may spell one repo as ssh vs https). |
| `auto_sync` | bool | `true` | `rig apply commit` runs `rig profile sync` BEFORE building its plan (a pull changes the very file the plan reads). Non-fatal: a network/push failure or a conflict prints a warning and the apply continues with the local file. A pull is announced as a warning, since the plan that runs is then built from a config you did not preview. A preview (`rig apply` / `apply info`) never syncs. (`rig activate commit` syncs whenever `repo` is set.) |
| `files` | map id → bool | all `true` | per-file switch for what travels besides `config.yaml`: `tmux`, `omp_config`, `omp_mcp`, `sysmon`. `false` stops syncing that file on every machine that shares the profile. `config` always syncs. |

**The payload is an allow-list** (`riglib/profile_files.py`, one registry): `config.yaml`,
`~/.tmux.conf` (user region), omp `config.yml`, omp `mcp.json` (masked), `sysmon.json`. Each file has
a kind: `portable` (synced) or `secret-bearing` (never synced — `env.vars` values, the omp
auth-broker token, `agent.db`, `.env`, `remote-host`, `ssh-control`, `rig.env.sh`).

**Secrets never travel.** `env.vars` is withheld from the pushed config and kept locally on a pull;
`mcp.json` is pushed with secret-looking values masked as `<rig:masked>` (a pull restores each from
the local file and names the ones still missing); every pushed byte is scanned for secret patterns
and a hit **refuses the push**, naming file, line and rule but never the value. A push is also
**refused** when the forge reports the repo as public to an anonymous API request (Forgejo/Gitea
`/api/v1/repos/<owner>/<repo>`, GitHub `api.github.com/repos/…`; skipped with a note when that cannot
be checked, e.g. offline or a local path). Write access to the repo is write access to this machine's
global rig config — and, through the `git` block, its global git config — so guard it like your
dotfiles.

**Machine values.** Home paths are stored as `{{rig:home}}/…`. `agent_tools_source`,
`harness.settings_paths`, `harness_wrappers.real_bin`, `sysmon.python_bin`,
`sysmon.reclaim.worktree_roots`, `tools.items.*.repo`, `tools.items.*.enabled` and any absolute path
outside home are *machine values*: left out of the comparison, always local-first when a file is
adopted, recorded per host in `machine/<host>.yaml`. An optional key nobody on this machine set is
dropped on adoption; `tools.items.*.repo` (required) keeps the profile's value re-homed.

**What lives where.** The checkout is `~/.config/rig/profile/repo/` (rig-owned: never edit it by
hand); the last-synced snapshot hashes are `~/.config/rig/profile/state.json`, **beside** the
checkout, never committed. Both follow `$XDG_CONFIG_HOME`.

---
