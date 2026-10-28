[← Back to the config schema index](../config-schema.md)

# `rig.yaml` schema — Machine services

Reference part of [config-schema.md](../config-schema.md): `tools`, `tg_ctl`, `tcc_watch`, `tgctl_reaper`, `sysmon`, `tracked_files`.

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
    # the same on Debian/Ubuntu; each OS honors its own list and skips the other:
    other:  { brew: [tmux], apt: [tmux] }
    # ctmux (wrapper over cmux with better tmux support) is first-class: rig clones it from
    # https://github.com/alex-mextner/ctmux-cli to ~/xp/ctmux-cli by itself, on macOS and Ubuntu:
    ctmux:  {}
```

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `enabled` | bool | `false` | opt-in master switch; `false`/absent = rig provisions no tools at all |
| `target` | str | `~/.local/bin` | managed PATH dir each tool's `install.sh` symlinks its entry into |
| `items.<name>` | map | — | one tool, keyed by its command name (the skill-blurb stem) |
| `items.<name>.enabled` | bool | `true` | `false` = skip this one tool |
| `items.<name>.repo` | str | `~/xp/<name>-cli`, else `~/dev/<name>-cli` if that holds the `install.sh` | the tool's checkout dir (holds its `install.sh`) |
| `items.<name>.bin_dir` | str | `target` | override the managed PATH dir for this one tool |
| `items.<name>.brew` | list[str] | `[]` | Homebrew formula(e) the tool depends on at **runtime** |
| `items.<name>.apt` | list[str] | `[]` (`ctmux`: `[tmux, fzf]`) | Debian/Ubuntu package(s) the tool depends on at **runtime**; installed only on a machine that has `apt-get` |
| `items.<name>.clone` | str | — (`ctmux`: its GitHub URL) | git URL cloned into `repo` when the checkout is absent |

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
the [`fj`](github-and-tmux.md#fj) block installs it and its `fj ship` wrapper.)

**Optional apt runtime dependency (`apt`).** The Debian/Ubuntu twin of `brew`, for the same reason.
Before `install.sh`, on a machine that has `apt-get` and `dpkg-query`, rig lists the installed
packages with ONE `dpkg-query` call and runs ONE `apt-get install -y <missing…>` (as root, or
through `sudo -n`; rig never waits on a password prompt). On macOS or any machine without `apt-get`
the list is **skipped with a note**, not an error, so one profile can carry `brew:` and `apt:` side
by side. A missing package that rig cannot install (no root, no non-interactive sudo) is **not** an
error: the tool's line carries `apt: WARN run \`sudo apt-get install -y …\``, `install.sh` still
runs (a failure there repeats the line), and `rig status` keeps flagging the package. A package whose
same-named command is already on `PATH` (Homebrew's `fzf`, say) counts as present. `ctmux` defaults to
`apt: [tmux, fzf]`; an explicit `apt:` (even `[]`) replaces that default.

**Clone source (`clone`) and first-class tools.** A tool whose `repo` directory does not exist yet
is cloned (`git clone --depth 1`) from `items.<name>.clone` before anything else runs. An existing
directory is never cloned over. `ctmux` is first-class: `ctmux: {}` is enough — its default clone
URL is `https://github.com/alex-mextner/ctmux-cli`, its repo `~/xp/ctmux-cli`, it ships no skill
blurb (a resolving `~/.local/bin/ctmux` counts as installed), and after `install.sh` rig runs
`bash <repo>/install.sh --check` and fails the tool if that exits non-zero. The OS gate lives in
ctmux's own `install.sh` (macOS and Linux), because rig has no per-OS filter on tool items.

**Drift.** `rig status` flags (in the **GLOBAL** section) a declared tool whose bin doesn't resolve
(**missing** — apply runs its `install.sh`) or that resolves but isn't advertised (**modified** —
apply re-runs `install-skill`). A declared `brew` formula Homebrew does not report as installed is
**also** flagged `missing`, independently of the bin/advertised state — one item per absent
formula — and `rig apply` runs `brew install` to reconcile it. On an apt machine a declared `apt`
package dpkg does not report installed is flagged the same way (`<tool>:<package>`).

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
  hysteresis_pct: 5                # a raised finding clears only this % of its warn line past it
  thresholds:                      # any subset; the rest keep their defaults
    wired_pct: { warn: 75, critical: 85 }
    reclaimable_gb: { warn: 2, critical: 1 }      # lower is worse
    data_used_pct: { warn: 90, critical: 95 }     # every disk alert lists reclaimable candidates
  alert: { min_severity: critical, cooldown_minutes: 30, disk_cooldown_minutes: 360, swap_cooldown_minutes: 120,
           max_per_hour: 6,
           resolved: true, dry_run: false,              # tg_bin / tg_ctl_bin: the senders (default PATH)
           route: telegram }                            # telegram | subscriber (the tg-ctl exclusive holder)
  handler: { tmux_session: main, window: sysmon-handler, workdir: "~", warn_persist_minutes: 15,
             spawn_cooldown_minutes: 60, startup_timeout_minutes: 5, idle_exit_minutes: 30,
             dry_run: false }        # route subscriber only; tmux_session default: sessions.tmux_session
  heal: { enabled: true, notify_agent: true, throttle: true, kill_orphans: false, kill_owned: false,
          grace_seconds: 120, dry_run: false,        # every kill path is opt-in
          restart_daemons: true, restart_labels: ["ai.hyperide.*", "com.hyperide.*"],
          restart_cooldown_minutes: 30, max_restarts_per_day: 3 }   # hung/crash-looping launchd jobs
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
| `swap_pct` | 80 | 95 | `vm.swapusage` / Linux `SwapTotal − SwapFree` (macOS grows swap in 1 GB files while disk allows — pair with `disk_free_gb`); a finding only under memory pressure, see below |
| `swap_gate_psi_full_avg60` | 5 | 20 | Linux PSI `/proc/pressure/memory` `full avg60`: percent of the last minute every non-idle task stalled on memory (macOS: the memorystatus level instead) |
| `swap_gate_available_pct` (lower is worse) | 20 | 10 | `MemAvailable` (macOS: reclaimable) as a percent of RAM |
| `wired_pct` | 75 | 85 | `host_statistics64` wired pages / `hw.memsize` |
| `reclaimable_gb` (lower is worse) | 2 | 1 | free + inactive + speculative + purgeable pages |
| `free_gb` (lower is worse) | 0.05 | 0.025 | free pages, vm_stat's "Pages free" (`free_count` − speculative; Linux `MemFree`) |
| `compressor_pct` | 70 | 90 | `vm.compressor_segment_pages_compressed` / `…_limit` |
| `disk_free_gb` (lower is worse) | 5 | 2 | free space on `/System/Volumes/VM` (swap growth) |
| `data_used_pct` | 90 | 95 | the data volume (`/System/Volumes/Data`, Linux `/`) as df's Capacity: Used / (Used + Available) |
| `procs_pct` | 60 | 80 | `proc_listallpids` / `kern.maxproc` |
| `storm_same_name` | 150 | 300 | processes sharing one executable name, every user counted (another user's stay untouched by heal) |
| `storm_pct` | 4 | 8 | the same storm as a percent of `kern.maxproc` (the worse of the two lines wins) |
| `coalition_procs` | 400 | 600 | processes in one launchd coalition — one job's whole fan-out, whatever the names or users (`proc_pidinfo(PROC_PIDCOALITIONINFO)`; Linux: the cgroup's systemd unit) |
| `coalition_pct` | 8 | 12 | the same coalition as a percent of `kern.maxproc` (the worse of the two lines wins) |
| `proc_growth_per_min` | 200 | 500 | process-count change over the last 20–120 s |
| `display_delays_per_min` | 2 | 5 | WindowServer `Timer delayed by N s` log lines |
| `display_max_delay_s` | 1.5 | 3.0 | longest such delay in the last minute |
| `taskport_stuck_s` | 20 | 60 | age of a `sample`/`footprint`/`lldb`/`htop`/`lsof`/… while SecurityAgent is up |
| `daemon_overrun_x` | 2 | 3 | a watched launchd interval job's current run age / its `StartInterval` (`launchctl print` + census age) |
| `daemon_crashloop_runs` | 3 | 6 | relaunches of a watched launchd job within 10 min beyond its schedule, last exit failed |

`kern.memorystatus_vm_pressure_level` WARN/CRITICAL is also reported. More than `gpu.slots`
GPU-model processes (whisper, llama.cpp, mlx) is always critical. `gpu.slots` is the size of the
agent-tools#817 GPU-model slot pool; when rig's `env` block exports `RIG_GPU_SLOTS` /
`RIG_GPU_SLOT_DIR`, the monitor follows them. Watchdogd WindowServer check-in trouble is always
critical.

**A full swap is an incident only under memory pressure** (rig-cli#685). The kernel leaves the
cold pages of a long-gone process in swap while RAM is free: on home on 2026-10-11 swap sat at
90 % with 24 GB available and PSI 0, and on 2026-10-15 20:19–20:27 at 100 % with MemAvailable
60–64 % and PSI ~0, and each time sysmon kept paging with nothing to act on. So `swap_pct` alone
raises nothing: the swap finding's severity is the LOWER of `swap_pct`'s level and the pressure
level, the worse of the two gates — PSI memory `full avg60` against `swap_gate_psi_full_avg60`
(Linux; on macOS `kern.memorystatus_vm_pressure_level` WARN/CRITICAL stands in) and available
memory against `swap_gate_available_pct`. A full swap under a 5 % stall or with under 20 % of RAM
available is a warning; under a 20 % stall or with under 10 % available it is critical. Without
PSI (an old kernel, a container) the available-memory gate decides alone; with no pressure signal
at all `swap_pct` decides as before. The alert names the readings (`under memory pressure: PSI
full avg60 7.5%, available memory 3.0 GB (8%)`).

**A swapoff in progress is no incident either.** Mid-swapoff the kernel reports SwapTotal = what is
left on the device and SwapFree 0, so swap reads 100 % full while it empties (home, 2026-10-15
21:28: memguard's hourly `swap-drain` posted "swap 9.0 GB of 9.0 GB used (100%)" with 37.6 GB
reclaimable). There is no swap finding while SwapTotal is below its size in the recent samples
(the last 15 minutes), or while `/run/memguard/swap-drain.json` describes a live drain — `state:
draining`, its `pid` alive, `updated` at most 30 s ago (the rig-cli#693 contract; a stale or
dead-pid file is ignored, so a crashed drain never hides swap).

**Hysteresis** (rig-cli#699). A finding raised from the previous reading clears only once its
value is `hysteresis_pct` (default 5) percent of its warn line past that line — swap warn 80
clears under 76, available memory warn 20 clears over 21 — so a value hugging a line cannot flap
the finding on and off. `0` turns it off.

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
grows on the same disk. Every disk alert (`data_volume` or the swap volume's `disk`, any level) lists
**reclaimable candidates**, largest first, capped at `reclaim.max_candidates`:

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
- **Coalitions (rig-cli#644):** each census also reads every process's launchd resource coalition
  (`proc_pidinfo(PROC_PIDCOALITIONINFO)`, no task port; on Linux the systemd unit of its cgroup).
  launchd gives every job — a daemon, an app — its own coalition, and whatever the job spawns
  stays in it however it reparents, so a coalition is one job's whole fan-out. A coalition is named
  by the launchd label whose job process (`launchctl list`) lives in it, else by its oldest member
  (`no live launchd job, leader tmux pid 3209`). One over `coalition_procs` / `coalition_pct` is a
  healable finding: its unprotected members are throttled like any storm, and when its label is a
  restartable daemon (`heal.restart_labels`) the restart rung restarts that job; a GUI app or an
  unlabelled coalition is never restarted. `rig sysmon sample` (and `status.json`) list the five
  largest coalitions. The 2026-09-27 crash had 684 processes in one coalition.
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

**Alerts.** Alerts go through `tg-ctl alert send --source sysmon --severity <worst> --key <finding
keys>` (tg-cli#636) with an HTML body that lists the finding, its numbers and who owns the
offenders: tg-ctl posts it to Telegram (`--tag problem`; a resolved notice `--tag report`) with a
footer naming every omp session that subscribed to alerts (`tg-ctl alert subscribe`), and delivers
it with its Telegram message id into those sessions so the agent can follow up on the message. An
older tg-ctl without that subcommand, or none on PATH, gets the previous `tg --tag problem
--subagent sysmon` send; the `alerts` log record carries `via` and `message_id`. They are
deduplicated per finding key:

- a key alerts when it first reaches `min_severity`, when it escalates, and otherwise at most once
  per `cooldown_minutes` (a swap finding: once per `swap_cooldown_minutes`, default 2 hours). The
  last send per key and level survives the key clearing, so a finding that clears and comes back
  inside its window is not new — on 2026-10-15 a swap value hugging its line re-paged every 5–20
  minutes as "new" (rig-cli#699);
- a disk finding (`data_volume`, `disk`) alerts at most once per `disk_cooldown_minutes` (default
  6 hours) per key and severity level: a full disk stays full until someone cleans up, so it must
  not page every cooldown. The window survives the finding clearing and coming back, so a volume
  hovering at a threshold does not re-page either; a level not sent within the window, up or
  down, still alerts (with the default `min_severity: critical`, one page per 6 hours);
- one message per tick carries every due finding;
- at most `max_per_hour` messages go out per hour;
- one "resolved" line is sent after a key stays clear for 5 minutes (with `route: subscriber` it
  reaches the holder only, never Telegram).

The dedup state lives in `state/memory.json`, so a restart never re-sends.

**`alert.route: subscriber` (rig-cli#700)** gives the machine's resource alerts to exactly one agent
session: the exclusive holder of tg-ctl source `sysmon` (tg-cli#747). A session takes the slot with
`tg-ctl alert subscribe --exclusive --source sysmon`; a second session asking is refused with the
holder named and the `tg-ctl alert handoff-request` command to ask for it. sysmon sends every alert
with `tg-ctl alert send … --to-holder`, which reaches the holder and nothing else (exit `0` sent, `3`
vacant, `5` holder suspended or delivery failed). An alert that was not delivered waits in a pending
list (at most 20 finding keys, dropped after `alert.disk_cooldown_minutes`) and is re-rendered from
the CURRENT findings and sent again once a holder is bound, so a cleared finding is never replayed.

When nobody holds the slot, sysmon raises a **handler session**: an interactive `omp --auto-approve`
in tmux window `<handler.tmux_session>:sysmon-handler` (the session is created when it does not
exist), whose first command subscribes it exclusively and whose system prompt (written to
`state/handler/brief.md`) limits it to safe, reversible actions, asking on Telegram (`tg --tag
decision`) before anything else. A spawn needs a CRITICAL finding, a WARNING present for
`warn_persist_minutes`, or a crash reboot; at most one spawn per `spawn_cooldown_minutes`, never while
the window exists, and never while the holder state is unknown (a `tg-ctl alert holder` error: a
handler could not subscribe either). A handler that holds nothing `startup_timeout_minutes` after its
spawn has failed to start; its window is left for inspection. sysmon closes the window it spawned (release,
a final note, `tmux kill-window` by the stored window id after checking its pane pid) once there has
been no finding, no delivered alert and an idle handler for `idle_exit_minutes`; it never touches a
holder session it did not spawn.

sysmon itself writes to Telegram only for a **CRITICAL** alert, and only when the handler cannot take
it: the holder channel is broken (tg-ctl missing, failing, or too old for `--to-holder`), or the slot
is vacant and the last spawn failed, the spawn cooldown is active with no window, or the window has
held nothing past `startup_timeout_minutes`. A CRITICAL failed start also posts `sysmon handler did
not start` once. A WARNING never reaches Telegram: its `alerts` record says `outcome: undelivered,
reason: warning-no-telegram` and it waits as pending. Records carry `via: holder`, `outcome`
(`sent`, `deferred`, `undelivered`, `error`), `reason`, and `fallback: telegram` when the Telegram
leg ran. `route: subscriber` cannot be combined with `auto_remediate: true` (the handler replaces it);
the former `route: agents` broadcast is removed and rejected with a pointer to `subscriber`. `rig
sysmon test-alert` follows the same route and never spawns.

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
4. **A hung or crash-looping launchd daemon, still critical after `grace_seconds`:** a clean
   restart, `launchctl kickstart -k gui/<uid>/<label>` (launchd stops the running instance and
   starts a fresh one). The daemon watches every job of its `gui/<uid>` domain whose label matches
   `heal.restart_labels` (fnmatch; default `ai.hyperide.*`, `com.hyperide.*`) through `launchctl
   print`: an interval job whose current run is `daemon_overrun_x` times its `StartInterval` old is
   **hung** (launchd starts no new run while one is going — the 2026-09-29 tmux-autosave run hung
   ~55 min), and a job relaunched `daemon_crashloop_runs` times within 10 minutes beyond its
   schedule with a failing last exit is **crash-looping**. Guards:
   - only a label launchd itself reported is ever restarted; a process the monitor cannot attribute
     to a launchd job never gets this rung;
   - never the monitor's own label (`sysmon.label`) and never an `application.*` GUI job, whatever
     `restart_labels` says;
   - at most once per label per `restart_cooldown_minutes` (default 30) and `max_restarts_per_day`
     times per 24 h (default 3). A job still failing after its budget raises a critical
     `restart_budget:<label>` finding (a human is needed) instead of another restart;
   - `restart_daemons: false` keeps the detection and alerts but never restarts; `dry_run` records
     "would run launchctl kickstart -k …" and keeps a separate cooldown/budget book.

Before every throttle, restore, signal or typed note, the pid's start time is compared with the one
the census recorded, so a reused pid is never hit; a pid whose start time is unknown is never
touched.

**Never touched:** user-interactive processes (a foreground terminal job no agent started, an
interactive shell), agent hosts, terminals/tmux/cmux, GUI apps, another user's processes, system
binaries and launchd jobs (a launchd job is only ever restarted through launchd, by rung 4).

**Incidents.** The first critical finding opens `state/incidents/<id>/incident.json`. It holds the
findings, the sample, the live omp sessions at that moment, and the alert/heal/remediation/restore
logs. The incident closes after 5 quiet minutes.

**Auto-remediation (`auto_remediate: true`, default OFF).** On a critical incident (at most once per
`remediation.cooldown_minutes`) the daemon writes `remediation-brief.md` into the incident dir and
launches a detached coding-agent session through the configured `remediation.agent`'s launcher from
the rig-detached family, `~/.agents/skills/rig-detached-<kind>/rig-detached-<kind>` for every kind
(omp, claude-code, codex, opencode); a missing or non-executable launcher is an `error` result in
the incident, never a fallback launch. Remediation is **operational recovery only** (the owner's
scope, rig-cli#642): the agent never edits, fixes or reverts code. The brief tells it to:

1. find the root cause from the incident evidence;
2. recover the machine: stop leaked processes (SIGTERM first) and restart a hung or crash-looping
   labelled daemon once with `launchctl kickstart -k gui/<uid>/<label>`;
3. resume the interrupted sessions with `rig sysmon restore --incident <id>`;
4. record the root cause in ONE ticket in the owning repo (`task find` first, then `task comment`
   on an open ticket or `task new` with the evidence), and stop there;
5. report through `tg report`;
6. write the handoff file named in the brief (a drill writes its ticket draft there instead of
   filing it).

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
- `rig sysmon handler status`: `holder: <state> <session> pid <pid>`, `window: <session>:sysmon-handler
  <window id> pane <pid>` (or `absent`), `last spawn: <time> ok|failed|starting` (or `never`).
- `rig sysmon handler spawn`: asks the daemon to raise the handler session now (it writes
  `state/handler/spawn-request`, which the daemon consumes on its next tick, ignoring the trigger and
  the cooldown), then waits up to `max(30, 2 × sample_interval_seconds + 10)` seconds and prints
  `spawned: <session>:sysmon-handler pane <pid>` once the daemon recorded that window as its spawn.
  Exit 4 without `alert.route: subscriber`, 3 when the window already exists, 1 when the daemon did
  not spawn in time (the request stays for the daemon's next run).

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
