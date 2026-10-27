# Changelog

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
