# `rig lint` — run every applicable check

```
rig lint [run] [-C DIR] [--config FILE] [--json] [--plan] [--no-setup] [--fix] [--ci]
         [--only CAT[,CAT]] [--skip CAT[,CAT]] [--scope DIR]... [--timeout SEC]
rig lint check   …   # complexity + file-length gate only, see lint-check.md
rig lint rules   …   # effective lint-rule policy
```

A bare `rig lint` is `rig lint run`. It looks at the git worktree (tracked and untracked-not-ignored files), works out which
languages, manifests, nested packages and configured tools are present, and runs **every check that applies**: lint, format check,
typecheck, the project's own lint/typecheck scripts, and the complexity/file-length gate (`rig lint check`). One failing check never
stops the others. `rig lint check` and `rig lint rules` are unchanged and stay backward compatible.

Rig does **not** claim to cover every language. See [Not covered](#not-covered).

## What counts as a check, and who decides

- **Configured tools are authoritative.** A tool the project already configures (ESLint, Biome, Prettier, Ruff, Black, flake8,
  pylint, mypy, pyright, `tsc`, Oxlint, Oxfmt, …) is run as configured and is never replaced, reconfigured or "improved". A tool with
  no Rig provider (for example `xo`, `dprint`) is listed under `unsupported_tools`; it is still never replaced.
- **Complementary tools stay separate.** ESLint **and** Oxlint, Black **and** Ruff-format, flake8 **and** Ruff, `lint:web` **and**
  `lint:api` all run. Two checks are merged only when they are provably the same: the same tool, scope and effective argv, or a
  script that names the tool and has no path restriction. A script called `lint` does not cover every language in a mixed repo.
- **Defaults only where nothing is configured.** A role (lint, format) in a scope with no configured tool gets Rig's default:
  Oxlint + Oxfmt for JS/TS, Ruff for Python. A tool that is *declared* but has no usable configuration (ESLint in `package.json`
  with no config file) is still the selected tool: it runs and reports its own configuration failure. It is never silently turned
  into a green "not applicable".
- **No speculative checks.** Providers live in one registry (`riglib/lint_providers*.py`); a check exists because a provider
  claims the file type or manifest, nothing else.

## States and exit codes

Every check row has a `state` and a machine-readable `reason`:

| state | meaning | counts as pass? |
| --- | --- | --- |
| `passed` | the check ran and found nothing | yes |
| `failed` | it ran and reported findings, or could not finish (reason says which) | no |
| `missing-tool` | an applicable check could not be started: the tool is not installed or not resolvable | **no** |
| `unsupported-platform` | the tool has no build for this OS/CPU | **no** |
| `not-applicable` | deliberately not run (filtered, disabled, covered by an identical check, recursion guard, …) | no; never changes the exit code |
| `planned` | only under `--plan`: what would run | no |

Failure reasons: `findings` (exit 1); `timeout`, `crash`, `malformed-output`, `output-limit`, `incomplete`,
`malformed-manifest`, `unreadable`, `bootstrap-failed`, `wrote-files`, and a linter's `config-error` (exit 3; only the nested
`rig lint check` rejecting its own configuration is exit 2).

A non-zero tool exit is only `findings` when it is not a tool failure. A tool that could not run is reported separately and its files
count as **not checked**: `config-error` (the tool rejected its configuration, or exited with the code it reserves for
usage/configuration/internal errors, such as exit 2 for ESLint, Prettier and Ruff; the project's configuration is the problem, not
Rig's usage, so the run is exit 3), `crash` (an uncaught exception or
panic trace; exit 3) and `dependencies-not-installed` (state `missing-tool`: modules cannot be resolved and no `node_modules`
exists, so install the project's dependencies; exit 127; Rig never installs application dependencies). Output Rig does not
recognise stays `findings`, never "ok".

| exit | meaning |
| --- | --- |
| 0 | every applicable check passed, nothing could not run, and every source file was verified (or the run was narrowed on purpose). `--plan` always exits 0 unless 2 |
| 1 | at least one check failed with `findings` |
| 2 | Rig's own usage/config error: not a git repo, malformed `lint` block, bad `--only/--skip/--scope/--timeout`, `--fix` with `--plan`/`--ci` or with partially staged files, or the nested `rig lint check` rejected its configuration |
| 3 | analysis incomplete: timeout, crash, unreadable output, malformed manifest, a check that wrote files, a linter that rejected the PROJECT's own configuration (`config-error`), or a failed setup step |
| 127 | an applicable check could not run (`missing-tool`, `unsupported-platform`, `dependencies-not-installed`), **or** source files were not verified (`unsupported-language`, see Coverage) |

Priority is `2 > 127 > 3 > 1 > 0`, identical to `rig lint check`.

### Coverage: "nothing failed" is not "everything checked"

The summary always says how many source files were verified. A file is **checked** only when an executed lint/format/typecheck check
for its language reached a verdict about it (`passed`, or `failed` with findings). The report has `summary.files_checked`,
`summary.files_uncovered` and a `complete` flag (also at the top level). `complete` is the strict conjunction of three things:
(1) **every applicable check finished its analysis**: a check that crashed, timed out, hit a configuration error, wrote files, or
could not run (`missing-tool`, `unsupported-platform`) makes it false even when other checks cover the same files, and is listed in
`coverage.incomplete_checks` (`failed` with `findings` and `passed` are finished analysis; `not-applicable` and `planned` rows were never
meant to analyze); (2) every source file was checked (no checker for its language, a tool that could not run, or a category that was
left out each leave files unchecked); (3) no framework template goes unlinted. A file covered only by a check that did not finish is
not counted as checked. The text output then says "NOT COMPLETE" or "INCOMPLETE" and never "clean".

**`complete: false` never exits 0 on its own.** In a default run, when nothing failed but something applicable was not verified,
the exit code is **127**: a Kotlin or Ruby file next to a passing Python check (`unsupported-language`), a Kotlin-only repo, a file
whose check was disabled, or a Vue/Svelte/Nuxt scope whose templates no configured tool lints (`template-lint-uncovered`, listed
under `coverage.template_uncovered`: Oxlint reads only the script part, so a passing Oxlint does not cover the templates unless a
configured ESLint does). Unverified source ranks with a missing tool (127), above incomplete analysis (3) and findings (1), so a failing or timed-out check cannot outvote it; those checks still appear in the report with their own reason. `--plan` is a preview and
exits 0.

The one exception is a run you narrowed on purpose: `--only`/`--skip` that leave out a verifying category (`lint`, `format`,
`typecheck`, `scripts`), `--scope`, or a category turned off in the `lint.categories` config. It returns the selected checks' verdict
with `complete: false`, the omissions stay listed in `coverage`, and the text says "NOT COMPLETE". A repo with no source files
exits 0 and is `complete`.

Files the repository itself ignores are not missing coverage: discovery only sees tracked and untracked-not-ignored files, so a
`.gitignore`d `vendor/` or `node_modules/` tree is never listed. A tool's own ignore file (Biome/ESLint ignores) decides what that
tool skips; Rig does not second-guess it. A vendored tree that is tracked in git and not ignored is reported like any other source.
A file git tracks although an ignore rule matches it (`git add -f vendor/x.ts`) is still checked where the tool analyzes explicit file operands (Oxlint, Oxfmt, Ruff and Black get a `<check>#tracked-ignored` check naming exactly those files, never the ignored untracked tree); where it cannot (Prettier skips ignored operands, and a project script cannot be shown to reach it), the file is listed as uncovered with the reason `tracked-but-ignored` (un-ignore it, untrack it, or accept it), so `complete` is false and the exit is 127, never a green count.

### The read-only guard

A check must not change your files. Rig snapshots `git status` plus content digests of every dirty file before and after **each**
non-`--fix` check. If anything changed (a script that runs `oxfmt .`, a bare `ruff format`, `cargo fmt`, `gofmt -w`, a Makefile
recipe, a wrapper) the check is `failed` with reason `wrote-files`, exit 3, and the detail names the files. Rig does not revert or
restage them. Default formatter forms are the no-write ones (`--check`, `-l`, `-d`). Files matched by `.gitignore` (caches,
`node_modules`) are not covered by the guard.

## Modes

| flag | effect |
| --- | --- |
| `--json` | print only the `rig-lint-run/v1` document on stdout (warnings go to stderr) |
| `--plan` | print what would be installed and run; install nothing, run nothing, write nothing |
| `--no-setup` | never install tooling; missing tools are reported as `missing-tool` |
| `--ci` | read-only run for the repository (also implied by `CI`/`GITHUB_ACTIONS`): no manifest, lockfile or config is ever edited |
| `--fix` | run each check's **safe** fixer, then re-run the check and report the post-fix verdict (below) |
| `--only` / `--skip` | categories: `lint`, `typecheck`, `format`, `limits`, `scripts` |
| `--scope DIR` | limit to a package directory (repeatable) |
| `--timeout SEC` | per-check timeout, 1..3600 (default `lint.timeout_seconds`, 300) |

These flags belong to `rig lint [run]`. Written before `check` or `rules` (`rig lint --fix check`, `rig lint --plan --fix check`,
`rig lint --ci --fix check`) they are refused with exit 2 and nothing is written; `--fix` after `check` (`rig lint check --fix`) is
`check`'s own flag and keeps its documented meaning.

The selection (`--only`, `--skip`, `--scope`, `lint.categories`) is applied **before** overlaps are resolved and before setup needs
are computed. A check the selection leaves out is `not-applicable` (`filtered`) and claims nothing, so a project script such as
`lint: biome check .` that you filtered out can never keep the direct Biome checks "covered-by" it: with `--only lint` or
`--only format` the direct Biome check really runs, while a default run still lets the script cover them. Categories you did not
select also install and configure nothing (`--only limits` installs no Oxlint/Oxfmt and writes no config).

Execution is deterministic and serial (scope path, category, provider id). Commands run as argv lists, never through a shell, with
stdin closed, bounded output capture, a timeout that kills the whole process group, and `RIG_LINT_DEPTH` incremented so a project
script that itself calls `rig lint` is not run again (recursion guard: a script whose command contains `rig lint` is
`not-applicable`, and nested runs skip every project-script check). Plain discovery never executes repository code: JSON/TOML are
parsed, JS/TS/YAML configs are matched by file name only.

The nested `rig lint check` (the `rig-check` row) is started through a trusted by-path launcher (`riglib/lint_check_launcher.py`),
not `python -m riglib`: the linted repository is its working directory, and a project that ships its own `riglib.py` or `riglib/`
must never be imported in place of this Rig.

## Setup: what `rig lint` installs for you

Only a bare **local** run installs anything (not `--plan`, `--no-setup`, `lint.bootstrap.enabled: false`). Setup goes through the
same `plan.build` + `actions.run_plan` engine as `rig apply`, and a second run on an already set-up repo installs nothing.
Nothing is installed for a role that already has a configured tool, a PATH-global tool never counts as installed (CI and other
developers could not reproduce it), and Rig never creates `package.json` or `pyproject.toml`.

| need | route | reproducibility |
| --- | --- | --- |
| JS/TS with no lint/format tool: Oxlint `1.90.0` + Oxfmt `0.75.0` | the package manager the repo's own evidence selects (`packageManager` field, then the single lockfile, then `pnpm-workspace.yaml`, then npm); exact pin, `--ignore-scripts`, lockfile updated; plus a Rig-owned Oxlint config only when no Oxlint config exists | pinned + lockfile |
| JS/TS without a manifest (loose `.js` files) | Rig tool cache: `npm install --prefix <cache> --ignore-scripts` | pinned, nothing written in the repo |
| Python with no lint/format tool: Ruff `0.17.3` (any project shape: uv, poetry, pdm, pip-only `requirements.txt`, or no manifest at all such as `tool.py` or scripts inside a JS repo) | Rig tool cache: private venv, `pip install --only-binary :all: ruff==<pin>`. Ruff never goes through `uv run`/`poetry run`, because that would sync the application's whole runtime environment (heavy or unobtainable dependencies); an already materialized `.venv` that has the tool is used directly | pinned, nothing written in the repo |
| shell (`*.sh`, shebang scripts): ShellCheck `0.11.0`, shfmt `3.14.1`; GitHub workflows: actionlint `1.7.12` | sha256-verified release binaries into the Rig tool cache (linux/macOS, x86_64/arm64) | sha256-pinned |
| Rust: `clippy`, `rustfmt` | `rustup component add` for the toolchain that already applies to the project; the Rust version is never changed | follows the project toolchain |
| Go: `go vet`, `gofmt` | nothing to install, they ship with the Go toolchain | follows the project toolchain |

What setup will **not** do: apt/brew/sudo installs, installing a package manager, choosing a manager when two lockfiles
disagree (`ambiguous-package-manager`: reported, nothing installed), forcing a TypeScript major, or adding `--type-check`.
Type-aware Oxlint is on only when `oxlint-tsgolint` is declared **and** TypeScript is 7 or newer. A detected framework makes the
generated Oxlint config enable only built-in Oxlint plugins that exist for it (React → `react`, Next → `nextjs` + `react`,
Vue → `vue`, script only); frameworks without an Oxlint plugin (Nuxt, Svelte, Angular, Astro, …) get no plugin, and an existing
Oxlint config is never touched.

The generated React policy adapts to the JSX runtime. When the nearest `tsconfig.json` (a relative `extends` chain is followed, at
most 8 deep, read as JSON/JSONC only and never executed; malformed means no evidence) sets `compilerOptions.jsx` to `react-jsx` or
`react-jsxdev`, the Rig-rendered policy turns `react/react-in-jsx-scope` off, so no unneeded `import React` is required. The classic
runtime (`jsx: react`) or no evidence keeps the rule on, an existing Oxlint config is never altered, and
`linters.rules.severity: {react/react-in-jsx-scope: error}` still wins. Other React rules such as `react/jsx-key` stay on.
(Oxlint 1.90 has no `react/jsx-uses-react` rule, so only `react-in-jsx-scope` is adjusted.) The rendered policy is part of the
policy cache key, so editing the tsconfig re-renders it.

Under CI (or `--ci`), only steps that write nothing inside the repository run: tool-cache installs and toolchain components. A step
that would edit a manifest, lockfile or config is refused and reported `blocked`; the check it would have enabled is `missing-tool`
(exit 127), never a pass. A platform with no pinned asset, a missing package manager, or no `rustup` is reported explicitly
(`unsupported-platform`/`missing-tool`/blocked) with the remediation; it is never skipped silently.

## Running in CI

The supported consumer invocation is one command, run from the repository root (or with `-C DIR`):

```
rig lint --ci            # add --json for the machine-readable rig-lint-run/v1 document
```

`--ci` is also implied when `CI` or `GITHUB_ACTIONS` is set, but pass it explicitly so a local reproduction behaves the same.
It is read-only for the repository: nothing is committed, no manifest, lockfile or config is created or edited, and `--fix` is
refused (exit 2). The only thing it may write is the **Rig tool cache**
(`$RIG_LINT_TOOL_CACHE`, else `$XDG_CACHE_HOME/rig/lint-tools`, else `~/.cache/rig/lint-tools`).

Required setup, in this order, before `rig lint --ci`:

1. **Install `rig`** (a `git+https` pip install of this repository, or the pinned release you use elsewhere) and a `git` checkout
   of the repository to lint; Rig only looks at files git tracks or can see (tracked + untracked-not-ignored).
2. **Install the project's own dependencies, frozen, yourself**: `npm ci` / `pnpm install --frozen-lockfile` /
   `yarn install --immutable` / `bun install --frozen-lockfile`, and for Python only what a project-configured tool needs
   (`uv sync --frozen`, `poetry install`, `pip install -r requirements.txt`). Rig never installs application dependencies; a
   scope whose modules cannot be resolved is `dependencies-not-installed` (exit 127).
3. **Declare the JS/TS linters in the repository** (`oxlint@1.90.0` and `oxfmt@0.75.0` as exact devDependencies, or your own
   ESLint/Biome/Prettier). A JS/TS scope that has a manifest but no lint/format tool would need Rig to edit `package.json` and
   the lockfile; under `--ci` that step is `blocked`, the check is `missing-tool`, and the run exits 127 instead of passing.
   Commit that change once from a local `rig lint` run.
   A repository that already configures its own linters (for example **Biome**) never acquires Oxlint, Oxfmt or the anti-slop
   policy just because Rig runs: a configured tool is authoritative, nothing is bootstrapped for that role, and no catalog is needed.
4. **Give Rig the agent-tools catalog when the Rig Oxlint default applies.** The anti-slop Oxlint policy is *required* policy and is
   taken from the agent-tools catalog, the same checkout `rig apply` reads. Rig resolves it from `agent_tools_source`, then
   `$RIG_AGENT_TOOLS_SOURCE`, then `~/xp/agent-tools`, `~/work/agent-tools`, `~/agent-tools`. Where a run would generate that policy
   (a repository on the Rig Oxlint default, or one whose generated `.oxlintrc` must be reconciled), fetch `agent-tools` outside
   the repository under test (`git clone --depth 1 <agent-tools url> "$RUNNER_TEMP/agent-tools"`; `actions/checkout` can only
   write inside the workspace, so a `path:` there must be git-ignored) and export `RIG_AGENT_TOOLS_SOURCE=<that path>` for the
   `rig lint --ci` step. Rig never disables the policy or runs with a weaker one silently: without a catalog setup is `failed`
   (`required-policy-unavailable`, reason `bootstrap-failed`, exit 3) with the remediation. Rig does not edit the consumer's
   workflow: add the fetch step and the variable to the consumer's CI by hand or through its own `rig apply` reconciliation.
5. **Optionally cache the Rig tool cache** between runs (key it on the Rig version). Without a cache, `--ci` downloads the pins
   on every run: Ruff `0.17.3` (private venv, needs `python3 -m venv` and PyPI), ShellCheck `0.11.0`, shfmt `3.14.1` and
   actionlint `1.7.12` (sha256-verified GitHub release assets), and Oxlint/Oxfmt for manifest-less JS (`npm`). Network access to
   those hosts is required on a cold cache; `--no-setup` never downloads and reports every missing tool instead.
6. A Rust project needs `rustup` with the toolchain it already uses (components only); Go needs the Go toolchain.

Exit codes are the ones in [States and exit codes](#states-and-exit-codes), and CI should treat **every non-zero code as a
failed job**: `0` all applicable checks passed and every source file was verified, `1` findings, `2` Rig usage/config error,
`3` analysis incomplete (timeout, crash, a check that wrote files, a setup step that failed), `127` a tool could not run or
source files were not verified. `--ci` never turns a missing tool or an unverified language into a pass.

Rig's own CI proves this path with real binaries: `tests/test_lint_real_tools.py` runs a bare `rig lint run` (and `--ci`) over
tmp JS/React, Python (with an unresolvable dependency), shell/workflow and mixed repositories with the pinned tools. CI checks out
the real agent-tools catalog (like the real-catalog smoke job) and the tests hand it to Rig as `RIG_AGENT_TOOLS_SOURCE`. CI sets
`RIG_LINT_REQUIRE_REAL=1`, so a tool or catalog that cannot be obtained fails those tests instead of skipping them.

## Providers

| id | category | applies when | check command | `--fix` (safe only) |
| --- | --- | --- | --- | --- |
| `oxlint` | lint | JS/TS, Oxlint configured, or no other lint tool | `oxlint` | `oxlint --fix` |
| `oxfmt` | format | JS/TS, Oxfmt configured, or no other formatter | `oxfmt --check` | `oxfmt` |
| `eslint` | lint | ESLint declared or configured | `eslint .` | `eslint --fix .` |
| `biome-lint` / `biome-format` | lint / format | Biome declared or configured | `biome lint .` / `biome format .` | `--write`, never `--unsafe` |
| `prettier` | format | Prettier declared or configured | `prettier --check .` | `prettier --write .` |
| `tsc` | typecheck | `typescript` declared and a `tsconfig.json` | `tsc --noEmit -p tsconfig.json` | none |
| `ruff-check` / `ruff-format` | lint / format | Python, Ruff configured, or no other tool | `ruff check` / `ruff format --check` | `ruff check --fix` (no `--unsafe-fixes`) / `ruff format` |
| `flake8`, `pylint`, `black`, `mypy`, `pyright` | lint, lint, format, typecheck, typecheck | configured or declared | `flake8` / `pylint <pkgs>` / `black --check .` / `mypy .` / `pyright` | `black .` only |
| `shellcheck` / `shfmt` | lint / format | tracked shell files | `shellcheck -- <files>` / `shfmt -d -- <files>` | none / `shfmt -w` |
| `actionlint` | lint | `.github/workflows/*.y{,a}ml` | `actionlint -no-color -- <files>` | none |
| `cargo-clippy` / `cargo-fmt` | lint / format | `Cargo.toml` workspace root | `cargo clippy … -D warnings` / `cargo fmt --all -- --check` | none / `cargo fmt --all` |
| `go-vet` / `gofmt` | lint / format | `go.mod` | `go vet ./...` / `gofmt -l` (any output is a finding) | none / `gofmt -w` |
| `rig-check` | limits | always, once | `rig lint check --json` (embedded in the row as `report`) | `rig lint check --fix` |
| `script:<runner>:<name>` | scripts | `package.json` / `Makefile` lint, typecheck or `*:check` scripts | `<pm> run <name>` / `make -C <dir> <target>` | none |

`--fix` runs only the safe fixer above (never `--unsafe`, `--fix-dangerously`, `--fix-suggestions`), then re-runs the check;
the verdict is the post-fix state. A check without a safe fixer runs in check mode and says so. `--fix` is refused (exit 2, nothing
written) with `--plan`/`--ci` or when a file it may rewrite is partially staged. Changed files are listed under `fix.changed_files`;
files whose index entry differs are listed under `fix.needs_restage` and are never restaged by Rig.

Project scripts must match a name allow-list (`lint`, `typecheck`, `type-check`, `check-types`, `tsc` with an optional `:suffix`;
`format:check`, `fmt:check`; `check:lint`, `check:types`, …); names containing `fix`, `write`, `watch`, `dev`, `build`, `test` are
never run. A script that names a formatter without a read-only form (`oxfmt .`) is `not-applicable` (`writes-unknown`) and the
runtime guard above still protects anything that slips through.

## JSON document (`--json`, schema `rig-lint-run/v1`)

```json
{
  "schema": "rig-lint-run/v1",
  "ok": false,
  "exit_code": 127,
  "complete": false,
  "root": "/abs/repo",
  "mode": {"ci": false, "plan": false, "fix": false, "setup": "blocked"},
  "config": {"enabled": true, "timeout_seconds": 300, "categories": {"lint": true, "typecheck": true, "format": true, "limits": true, "scripts": true},
             "bootstrap": {"enabled": true, "javascript": true, "python": true}, "providers": {}},
  "discovery": {
    "scopes": [{"path": ".", "languages": ["typescript"],
                "package_manager": {"name": "pnpm", "source": "lockfile", "lockfile": "pnpm-lock.yaml", "root": "."},
                "tools": [{"tool": "eslint", "role": "lint", "declared": true, "config_files": ["eslint.config.js"]}],
                "scripts": ["lint"]}],
    "unknown_languages": [{"language": "kotlin", "files": 12, "examples": ["app/Main.kt"]}],
    "unsupported_tools": [{"scope": ".", "tool": "xo"}],
    "problems": [], "skipped_symlinks": []
  },
  "bootstrap": {"steps": [], "blocked": [{"scope": ".", "tool": "oxlint", "reason": "ambiguous-package-manager", "detail": "..."}],
                "results": [], "notes": []},
  "coverage": {"files_total": 40, "files_checked": 28, "files_uncovered": 12, "complete": false,
               "uncovered": [{"language": "kotlin", "files": 12, "examples": ["app/Main.kt"], "why": "no-provider"}],
               "template_uncovered": [], "incomplete_checks": ["eslint@."]},
  "checks": [{
    "id": "eslint@.", "provider": "eslint", "category": "lint", "scope": ".", "tool": "eslint",
    "state": "missing-tool", "reason": "tool-not-installed", "detail": "eslint is declared but not installed; run your package manager's install",
    "argv": null, "cwd": ".", "exit_code": null, "duration_ms": null, "timeout_s": null,
    "resolution": {"kind": "missing", "path": null, "detail": "eslint is not in the repository toolchain"},
    "counts": null, "output": {"stdout": "", "stderr": "", "truncated": false}, "fix": null, "report": null
  }],
  "summary": {"total": 7, "applicable": 5, "passed": 3, "failed": 1, "missing_tool": 1, "not_applicable": 2, "unsupported_platform": 0,
              "planned": 0, "files_checked": 28, "files_uncovered": 12, "complete": false, "duration_ms": 4321}
}
```

`mode.setup` is one of `applied`, `nothing-to-do`, `blocked`, `failed`, `skipped-ci`, `skipped-no-setup`, `skipped-plan`,
`disabled-by-config`. `ok` is `exit_code == 0` (`null` under `--plan`). Read `complete` before trusting `ok`. On exit 2 the
document is `{schema, ok: false, exit_code: 2, error, fix: {requested}}`. `counts` is `null` unless the tool's output was parsed;
it is never invented. Check rows from `rig-check` embed the whole `rig-lint-check/v1` document as `report`.

## Configuration

A `lint:` block in `~/.config/rig/config.yaml` and/or `rig.yaml` (the repo wins per key). Only switches and timeouts are
configurable; provider commands never are (a committed `rig.yaml` cannot inject commands). Full key reference:
[config-schema.md](config-schema.md).

```yaml
lint:
  enabled: true              # master switch for `rig lint run` (not for `rig lint check` / `rig lint rules`)
  timeout_seconds: 300       # per check, 1..3600
  categories: {lint: true, typecheck: true, format: true, limits: true, scripts: true}
  bootstrap:
    enabled: true            # may a bare local `rig lint` install tooling
    javascript: true
    python: true
    versions: {}             # exact-version overrides keyed by pinned package, e.g. {ruff: "0.17.3"}
  providers:
    eslint: {enabled: true, timeout_seconds: 600}
```

There are no `exclude` keys and no per-file exemptions: a tool's own ignore files remain the project's business, and the complexity
and file-length thresholds belong to `lint_checks:`.

## Not covered

Rig reports these instead of passing them: languages without a provider (Kotlin, Java, Scala, Ruby, PHP, C/C++, C#, Swift,
Objective-C, Dart, Lua, Elixir, Haskell, Clojure, Perl, Zig, SQL, …), Deno projects, and orphan Go/Rust files without `go.mod` /
`Cargo.toml`. They appear under `unknown_languages` and `coverage.uncovered`, make `complete` false, and, when nothing else
applies, exit 127. Also not covered: formatting of Markdown/YAML/TOML/CSS/HTML beyond what a configured Prettier or Biome does,
Dockerfile/Terraform linting, Python type checking unless mypy/pyright is configured, and TypeScript type checking unless `tsc` is
configured (Rig never adds a type checker). Template linting for Vue, Svelte, Nuxt and similar frameworks is not provided by
Oxlint (the Vue plugin lints `<script>` only): keep ESLint or the framework's own tool configured. When Rig generates an Oxlint
config for such a project it says so in a `template-lint-uncovered` setup note instead of claiming a full check. Rig never
upgrades a project's TypeScript, Rust or Go toolchain.
