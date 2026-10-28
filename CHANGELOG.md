# Changelog

## 0.73.1

### Fixed
- The generated Oxlint React policy also turns `react/react-in-jsx-scope` off for a Next.js scope (declared `next` dependency) whose
  tsconfig has `jsx: preserve`, because Next compiles JSX itself; a non-Next `preserve` scope and the classic runtime keep the rule,
  and an explicit `linters.rules.severity` entry stays final (rig-cli#789).
- `rig schema --check` on a non-editable install no longer reports a false "missing or out of sync": the wheel now carries the
  committed schema as package data (`riglib/rig.schema.json`, copied from the canonical `schema/rig.schema.json` by `setup.py` at build time), and the check compares it with the registry. A missing or altered packaged file still exits 2; `rig schema --write` on an
  install exits 2 and writes nothing (it only targets a rig-cli checkout) (rig-cli#793).

## 0.73.0

### Changed
- The omp guard still denies every Runpod creation path, now with one message that names the replacement:
  `Runpod is removed (owner tg#25309): heavy work goes to the server: server-run -- <cmd>` (rig-cli#786). The
  `runpod-budget-write` and `runpod-run-script-write` rules (runpod-run state protection) are gone with `runpod-run`.

## 0.72.0

### Added
- The generated Oxlint React policy turns `react/react-in-jsx-scope` off when the nearest `tsconfig.json` explicitly selects the
  automatic JSX runtime (`jsx: react-jsx` / `react-jsxdev`, JSON-only `extends` chain), so users are not pushed to add an unneeded
  `import React`; classic runtime, no evidence and any existing Oxlint config keep the rule as is.
- `rig lint` / `rig lint run`: runs every applicable check (lint, format check, typecheck, project lint/typecheck scripts, the
  complexity/file-length gate) through one provider registry, chosen from the languages, manifests, nested packages and configured
  tools in the worktree. Per-check states `passed`/`failed`/`missing-tool`/`not-applicable`/`unsupported-platform`, exit priority
  `2 > 127 > 3 > 1 > 0`, `--json` (`rig-lint-run/v1`), `--plan`, `--no-setup`, `--ci`, `--only/--skip/--scope/--timeout`, and `--fix`
  that runs only safe fixers and re-checks. Configured tools stay authoritative and complementary ones all run. See `docs/lint.md`.
- The summary reports `files_checked`, `files_uncovered` and `complete`; unsupported languages are listed. `complete: false` never
  exits 0 by itself: any source file nobody verified (no checker, a tool that could not run), or a Vue/Svelte/Nuxt scope whose
  templates no configured tool lints (`template-lint-uncovered`, listed under `coverage.template_uncovered`), exits 127 unless the run
  was narrowed on purpose with `--only`/`--skip`/`--scope`/`lint.categories`. `--plan` stays a preview (0). A linter that rejects the project's
  own configuration is exit 3 (exit 2 stays Rig's own usage/config error).
- Read-only runtime guard: every non-fix check is bracketed by a `git status` + content-digest snapshot; a check that changes the
  worktree is `failed` with reason `wrote-files` (exit 3), which also catches scripts such as `oxfmt .` and bare `ruff format`.
- A tool that could not run is no longer reported as `findings`: a rejected configuration or the exit code a tool reserves for
  usage/config errors is `config-error`, a trace from the tool is `crash`, and unresolved modules with no `node_modules` are
  `dependencies-not-installed` (`missing-tool`, with the install hint). Their files count as not checked, so
  `files_checked` never includes files a tool did not analyze. `complete` is now also false whenever any applicable check crashed,
  timed out, hit a configuration error or could not run, even if other checks cover the same files (listed in
  `coverage.incomplete_checks`); a crashed typecheck next to a passing linter used to read `complete: true`.
- The nested `rig lint check` runs through a trusted by-path launcher (`riglib/lint_check_launcher.py`), so a project's own
  `riglib.py` / `riglib/` in the linted repository is never imported in place of Rig.
- Local bare `rig lint` installs missing default tooling through the same `plan.build` + `actions.run_plan` engine as `rig apply`:
  Oxlint + Oxfmt for JS/TS (pinned, via the repo's own package manager or a Rig-owned tool cache), Ruff for Python (pinned, from the
  Rig tool cache so the application's runtime environment is never synced), sha256-verified ShellCheck/shfmt/actionlint, and
  `rustup` clippy/rustfmt components. CI never edits manifests, lockfiles or configs.
- `lint:` config block (global + repo) for the master switch, per-check timeout, categories, bootstrap and per-provider switches.
- Bootstrap coverage (honest list): installs JS/TS Oxlint+Oxfmt, Python Ruff, ShellCheck/shfmt/actionlint and Rust clippy/rustfmt; Go
  needs nothing; apt/brew/sudo, package-manager installs, ambiguous lockfiles and TypeScript major changes are never done (reported).
- `rig lint --ci` contract: no setup, no manifest/lockfile/config edits, read-only runtime guard, exit `2 > 127 > 3 > 1 > 0`; an
  incomplete run (uncovered source files) exits 127 unless deliberately narrowed.

### Changed
- Bare `rig lint` now runs the checks (it used to print help); `rig lint --help` still lists `run`, `check` and `rules`, and
  `rig lint check` / `rig lint rules` keep their behavior. `rig lint rules` shows `active` / `inactive_reason` for type-aware rules.
- Decomposition (no behavior change): oversized `riglib` modules (`cli`, `config`, `config_schema`, `drift`, `plan`, `actions`, `permissions`,
  `config_web`) and their test files were split into focused modules/helpers so the whole-file complexity/length gate passes with no
  exemptions or baselines.

## 0.71.0

### Added
- Default-on `instructions` category: rig provisions the canonical "actionable problem reports" policy (what was already tried and
  observed, the remedy already started, the next verification or the exact blocking dependency) as a marker-delimited block in the
  global guide of every effective and machine harness kind (Claude `CLAUDE.md` incl. `~/.claude-accounts/*` and explicit
  `settings_paths`, Codex `AGENTS.md`, OMP `RULES.md` so task subagents see it, and the other registered kinds; OpenCode reuses the
  Claude guide it also reads while its own `AGENTS.md` does not exist yet, unless `OPENCODE_DISABLE_CLAUDE_CODE*` is set). It no
  longer depends on a home `AGENTS.md`, permission provisioning or a repo guide. There is no separate toggle: `harness.enabled: false`
  stops rig managing it together with the rest of the harness block (blocks already written stay in place).
  `rig apply info --only instructions` previews and `rig apply commit --only instructions` applies it; `rig status` reports missing,
  stale or malformed blocks; global config-web and `rig activate` use the same actions.
- Reporting, advisory-permission and repo-guide splices share a resolved-target kernel lock (`riglib/instruction_files.py`), so
  concurrent writers keep each other's blocks. The reporting writer additionally replaces atomically, preserves user text and other
  managed blocks, and aborts on an intervening edit or a hard-linked target.
- A policy-only `rig activate` of the harness area (empty or just-prepared guide directories) is not evidence of an installed client,
  so it does not enable that harness's auto mode on the next run.

## 0.70.0

### Added
- `rig lint check PATH` reports an explicit PATH that selects no in-scope file (e.g. gitignored) as a `skipped` entry with reason
  `not-in-scope` instead of a silent empty pass.
- `rig lint check`: default-on complexity (cognitive > 15, cyclomatic > 20) and file-length (warn > 1200, error > 2400) gate backed by
  the pinned, digest-verified official `cccc` v1.8.4 analyzer, independent of Oxc/Biome/`linters.enabled`. Configurable through the
  `lint_checks` block (global → repo). See `docs/lint-check.md`.
- `rig apply commit` activates it through the reconcile engine: `install_cccc` (pinned analyzer under
  `~/.local/share/rig/cccc`), `install_lint_check_hook` (`rig lint check --staged --json` in the global git-hook dispatcher) and
  `install_lint_check_workflow` (`.github/workflows/rig-lint-check.yml`). `rig status` reports drift for all three (new
  `lint_checks_machine` (GLOBAL) and `lint_checks` (REPO) areas).
- `rig lint check --fix` is scoped and verified: it rewrites only the flagged functions, reverts any rewrite that introduces a finding,
  grows a size violation or resolves nothing, writes through pinned directory fds, rolls back on exceptions and reports
  `needs-restage` for fully staged files. `fix.requested` is true whenever `--fix` was passed; an analyzer summary that reports
  parse errors fails the check closed (exit 3).
- `lint_checks` activation hardening: the CI workflow installs rig from the explicit, validated `lint_checks.ci.rig_source`
  (`git+https://<host>/<path>@<40-hex commit sha>`) instead of the nonexistent `hyper-rig==<version>` and is not written while it is
  unset (bytes no longer churn per rig version); the pre-commit fragment is `global-hook-protected` so a tracked `.githooks-skip` or
  `GLOBAL_HOOKS_SKIP` cannot disable it; `install_cccc` installs under an alternate `HOME` too (only `RIG_CCCC_DRY_RUN=1` skips the
  download), and survives `http.client.HTTPException`; the hook is
  written only after the analyzer verified, so a failed/offline install is a loud error and never leaves a machine-wide hook that
  blocks every commit with 127.
- `lint_checks` machine-wide artifacts follow machine intent: the analyzer and the pre-commit hook are planned and drift-checked
  from the GLOBAL layer's intent, never from one repo's opt-out. A repo's `lint_checks.enabled: false` or
  `complexity.enabled: false` no longer lets `rig apply commit` there write an analyzer-less machine-wide hook (every other repo then
  exited 127) nor leaves permanent `extra … 30-lint-check still installed` drift; it only drops that repo's workflow / omits the
  analyzer from it. `install_cccc` is planned whenever the global layer or the applying repo runs complexity
  (a repo that enables the check against a global opt-out gets the analyzer its own `rig lint check` demands, but no hook; the hook
  follows the global switch only) and the hook is gated on that verified analyzer; a repo that enables complexity against a global
  complexity opt-out gets exit 127 with the fix message until its own `rig apply commit` installs the analyzer (loud, never a silent
  pass). `rig activate` has a `lint_checks` area (category `lint_checks_machine`, probed for a pinned `cccc` on this OS/CPU; an
  unsupported platform is listed with the reason, never an error), so `rig activate commit` installs the analyzer and hook instead of
  silently filtering them out. The generated CI workflow keeps its analyzer step unless the
  repo itself disabled complexity (CI never reads the global config, where complexity runs by default). The CI
  step installs the analyzer to `${XDG_DATA_HOME:-$HOME/.local/share}/rig/cccc/v1.8.4`, exactly where rig looks, and the installer
  accepts only the release tarball's top-level `cccc` member (the one the CI step extracts). Remove the machine-wide gate with
  `lint_checks: {enabled: false}` in `~/.config/rig/config.yaml`.

### Changed
- Existing configs now plan `lint_checks` actions by default (the analyzer install and the pre-commit hook; the CI workflow once
  `lint_checks.ci.rig_source` is set); opt out of the machine-wide gate with `lint_checks: {enabled: false}` in the global config
  (in a repo's `rig.yaml` it only switches that repo's check and workflow off).
- `rig lint check` hardening: a read-only or `--staged` scan (and the global commit hook) never spawns node/bun, and `--fix` never loads a
  git-tracked `node_modules/typescript`; `--fix` no longer rewrites an unnamed closure sharing a line with a differently named flagged
  function; a config error outside `lint_checks` is a stderr warning instead of exit 2 (errors inside `lint_checks` stay fatal); the
  staged snapshot directory is canonicalized (macOS `/var` -> `/private/var`).
