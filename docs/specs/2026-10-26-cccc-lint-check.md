# `rig lint check`: cccc complexity + file-length gate (design, 2026-10-26)

Status: implemented. Ticket: [rig-cli#772](https://git.hyperide.ai/ultrabricks/rig-cli/issues/772). User-facing guide: [../lint-check.md](../lint-check.md).
This document records the reconcile/activation side; the CLI engine and `--fix` are described in the same ticket.

## Understanding

Owner asks (2026-10-26): a default-on check for cognitive/cyclomatic complexity (official `cccc`) and file length, thresholds
configurable global → repo, a safe `--fix`, active in every repo through rig's own reconciler — not through Oxc or Biome.
Coordinator-chosen defaults: cognitive 15, cyclomatic 20, warning above 1200 lines, error above 2400; only JSON exempt.

## Decisions

- **Own pinned binary, not `$PATH`.** `riglib/cccc_install.py` pins `moznion/cccc` v1.8.4 (an immutable release) with two sha256
  digests per platform (tarball and extracted binary; linux x86_64/aarch64 musl, macOS x86_64/arm64). The binary lives in
  `${XDG_DATA_HOME:-~/.local/share}/rig/cccc/v1.8.4/cccc` and is re-hashed on every use; there is no env or flag override.
  The analyzer is verified by path and then executed by path, so a process of the same user that replaces the user-owned managed
  binary between verification and exec is out of scope (it could equally edit rig itself).
- **Verify before extract, extract before trust, never execute early.** tarball digest → only the single regular top-level `cccc`
  member (the real v1.8.4 tarball lists exactly `cccc`, `README.md`, `LICENSE`; `riglib.cccc_install.BINARY_MEMBER` is the one name
  the extractor and the CI step's `tar -x` share; traversal, absolute, symlink, hardlink or device members reject the whole archive) → binary digest → temp file in the
  destination directory → `chmod 0755` → `os.replace`. Every failure leaves the final path untouched (tested: tampered tarball,
  tampered binary, traversal, symlink, hardlink, missing/duplicate member, truncated download, unsupported platform).
- **Independent of linters.** `plan._build_lint_checks` has its own call site next to (not inside) `_build_linters`, so a Biome-only
  repo, a repo with no linter and `linters.enabled: false` all get the gate. `lint_checks.enabled: false` in the GLOBAL layer removes it
  from the machine; in a repo it only switches that repo's check/workflow off.
- **Activation reuses existing mechanisms, no new daemon.**
  - *Machine:* `install_cccc` (handler in `riglib/actions/lint_checks.py`, registered in the runner's `_HANDLERS`, tagged in
    `action_tags`, drift in `drift._check_lint_check_analyzer`).
  - *Commit time:* the global git-hook dispatcher (`run-global-hooks`) runs every executable file in
    `global-hooks.d/pre-commit/`. rig writes one more fragment there (`30-lint-check`, capability id `lint-check`), so existing
    dedup/skip/`GLOBAL_HOOKS_DIR` behaviour applies unchanged. The fragment pins the absolute `rig` path at plan time (same
    approach as the tmux/omp scripts) and fails closed.
  - *CI:* a rig-rendered `.github/workflows/rig-lint-check.yml`, reconciled with the same safe-path classifier as linter configs
    (`resolve_linter_config`: symlinked ancestors and escapes are refused).
- **One renderer.** `riglib/lint_checks_artifacts.py` renders the fragment and workflow; plan, apply and drift all call it, so
  `rig status` compares exactly what `rig apply` writes.
- **Fail closed, loudly.** Unsupported platform → `BLOCKED` error action and drift (never silent). Dispatcher disabled, fragment
  disabled, `ci.enabled: false` or a non-standard `ci.target` → an explicit plan note naming the reason.
- **No network in tests.** Only an explicit `RIG_CCCC_DRY_RUN=1` skips the download (reported as `dry-run`). A sandboxed/alternate
  `HOME` does NOT: the analyzer lands under that HOME, where `rig status` and `rig lint check` look for it (skipping it there made
  apply "converge" while the gate stayed unsatisfiable). The suite stubs `cccc_install.inspect` as verified and exports
  `RIG_CCCC_DRY_RUN=1` (`tests/conftest.py`, `tests/smoke.sh`); the `test_lint_checks_*` modules opt back into the real verification
  with the `real_cccc_inspect` fixture and clear the flag.

## Alternatives rejected

| Alternative | Why not |
| --- | --- |
| `--fix` = trim trailing whitespace / EOF newline | Does not change complexity or line count meaningfully and gives a false sense of "fixed". The owner chose a syntax-aware, semantics-preserving rewrite (`fix/else-after-return`) instead. |
| `--fix` rewrites every else-after-return site in a flagged file | It edits functions nobody flagged and may resolve nothing. Edits are restricted to the flagged functions' sites (analyzer line + name; nested closures excluded), a finding is `fixable` only when a qualifying site exists, and any rewrite that adds a finding, grows a size violation or does not reduce a flagged function is reverted after the analyzer re-run. |
| Write by path name after a name-based symlink check | A concurrent directory-to-symlink swap between check and write redirects the write out of the repo. Components are opened `O_NOFOLLOW` from a root fd and temp file + `os.replace` use that directory fd. |
| Silently leave the index stale after `--fix` | A plain `git commit` would commit the pre-fix bytes while the report says compliant. A rewritten path with staged content gets a `needs-restage` note (rig never runs `git add`). |
| Probe fixability (load the repo's `node_modules/typescript`) on every scan | A read-only check, and the machine-wide commit hook, would execute repo-supplied JavaScript. Only an explicit `--fix` runs the parser, never against a `node_modules/typescript` git tracks (or cannot vouch for); a plain scan reports `fixable: false`. Known limit: the compare-then-rename write has an inherent small race window against another writer of the same file. |
| `cargo install cccc` / build from source at apply time | Needs a Rust toolchain on every machine, takes minutes, is not reproducible byte-for-byte and cannot be verified against a published digest. Prebuilt release + two pinned digests is verifiable and fast. |
| Use `cccc` from `$PATH` / a `RIG_CCCC_BIN` override | The verdict would depend on whatever is installed; an override is also a trivial way to turn the gate off in a hook. |
| Add the hook/CI as agent-tools catalog items | Cross-repo change with its own release cycle; the rig-owned route needs none and can later be replaced (see follow-ups). |
| Gate the check behind Oxc readiness | Defeats "every repo"; Biome-only and unlinted repos are the ones that most need it. |
| Silent skip when the analyzer is missing | A gate that passes when its tool is absent is no gate. Exit 127 + drift + hook blocking instead. |
| Blanket excludes / baselines to get repos green | Explicitly out of scope: thresholds stay, repos are refactored for real. |

## Self-application impact (rig-cli itself)

Measured 2026-10-26 on this branch's worktree with the verified analyzer (`/tmp/cccc-finance/bin/cccc`, sha256
`e09bc0ec…f4a1d578`, `cccc --no-config --min 16`) over the 722 files returned by `git ls-files --cached --others --exclude-standard`
(396 of them are analyzable source files), and `wc -l`-style physical line counts for everything except `.json`. No excludes or
baselines were added; this is what the gate will report on rig-cli once it lands.

- Files **> 2400 lines (error): 10** — `riglib/actions/runner.py` (10467), `tests/test_actions_drift.py` (5433), `docs/config-schema.md`
  (4787), `tests/test_tmux.py` (4664), `riglib/cli.py` (4434), `riglib/config.py` (4305), `riglib/drift.py` (4005),
  `riglib/plan.py` (3966), `docs/specs/recovery/01a-retrospective-preserved.md` (2606), `riglib/tmux.py` (2455).
- Files **1201–2400 lines (warning): 24** (e.g. `tests/test_permissions.py` 2187, `riglib/config_schema.py` 2121, `riglib/worktree_gc.py` 1804,
  and the CSV fixture `tests/fixtures/sysmon_swap_home_2026-10-15.csv` 1271 — no exemption exists for it).
- Functions **over a limit: 348** (all with cognitive > 15; 109 of them also cyclomatic > 20) in **102 files**; the heaviest files are
  `riglib/actions/runner.py` (46), `riglib/config.py` (26), `riglib/drift.py` (21), `riglib/removal_guard.py` (14), `riglib/cli.py` (13), `riglib/plan.py` (12).
- 0 parse errors.
- **This change's own functions (re-measured with the same pinned cccc, `--min 1`)**: every function in the new files
  (`riglib/cccc_install.py`, `riglib/lint_checks_artifacts.py`, `riglib/actions/lint_checks.py` and the four `tests/test_lint_checks_*` /
  `test_cccc_install` modules plus `tests/lint_checks_helpers.py`; 110 functions, max cognitive 12) is within the limits, and so are the
  functions added to `plan.py` (`_build_lint_checks` 1/2, `_lint_check_hook_blocker` 6/7, `_plan_lint_check_hook` 5/5,
  `_plan_lint_check_workflow` 2/3) and `drift.py` (`_check_lint_check_analyzer` 2/3, `_check_lint_check_hook` 4/5,
  `_check_lint_check_workflow` 5/4, `_check_disabled_lint_check_hook` 7/7; cognitive/cyclomatic). **Not true of two pre-existing
  functions this change extends**: `drift.detect` (cognitive 87, cyclomatic 77) gained one `elif` branch and
  `drift.compute_drift_report` (48/43) gained one `if`; both were already over the limits and stay over until the follow-up split.

Consequence: `rig lint check` on rig-cli exits **1** today and the CI workflow/hook would block rig-cli's own commits until the
offenders are split. That cleanup is intentionally NOT part of this change (no blanket excludes); see the follow-up tickets in the
progress file. Because the runner/plan/drift modules are already past 2400 lines, new `lint_checks` logic lives in new small modules
(`cccc_install.py`, `lint_checks_artifacts.py`, `actions/lint_checks.py`); the large files only gained dispatch lines and small helpers.

## Layering of the status areas

The analyzer and the pre-commit fragment are machine-wide, so their actions use category `lint_checks_machine` (GLOBAL layer,
reported by `rig status` even outside a git repo, and reconciled by machine-level applies). **They follow machine intent, not the
repo that happens to run the apply:** plan and drift share one derivation (`lint_checks_artifacts.lint_checks_intent`) that takes
machine intent from the global layer (`LoadedConfig.global_data`: built-in defaults + `~/.config/rig/config.yaml`) and adds only
whether the applying repo runs complexity. Two rules. **Hook:** planned iff the global layer has `enabled` on (global
`enabled: false` ⇒ no hook, and a leftover fragment is `extra` drift); a repo's own opt-out never removes or re-plans it. **Analyzer:**
planned iff the global layer (`enabled` and `complexity.enabled`) OR the applying repo (`enabled` and `complexity.enabled`) runs
complexity; it is per machine and harmless, so a repo that enables the check against a global opt-out gets the analyzer its own
`rig lint check` demands from the `rig apply commit` that the exit-127 message names, but no hook. The hook is gated on a verified
analyzer in exactly the cases the analyzer is planned. This is not a guarantee for every repo on the machine: a repo that enables
complexity against a global complexity opt-out gets exit 127 with the fix message until its own apply installs the analyzer (loud,
never a silent pass). A repo's `lint_checks.enabled: false` / `complexity.enabled: false` cannot make `rig apply commit` there write
an analyzer-less machine-wide hook while the global layer runs complexity (the bug this replaces), and never leaves permanent `extra`
drift; it only drops that repo's workflow / omits the analyzer from it. CI reads built-in defaults plus the repo layer, never the
global config, so the workflow omits the analyzer step only when the repo itself disabled complexity (merged complexity off while
the global layer's own is on); a global-only opt-out keeps the step, otherwise CI, where complexity runs by default, would exit 127.
The workflow is the repo's file: category `lint_checks` (REPO
layer, dropped outside a git repo), planned from the merged config. The hook is not planned (an explicit plan note instead) when the
platform has no pinned analyzer (only while the hook is gated on the analyzer, i.e. a layer runs complexity) or no absolute `rig` can be pinned (`runner.pinned_rig_path`),
so no machine-wide commit block can come from an analyzer that can never exist. While an install is merely pending the hook stays in
the plan, but its handler runs AFTER `install_cccc` and writes the file only when the analyzer verifies (a failed download is a loud
`error`, the hook stays unwritten; `RIG_CCCC_DRY_RUN=1` reports it skipped). A `30-lint-check` fragment left installed after
`fragments.lint-check.enabled: false` (or a GLOBAL `lint_checks.enabled: false`)
is reported as `extra … still installed (will still run)`, like a disabled catalog fragment. `tracked_files` diverts the workflow like
any other tracked-owned repo file (the hook lives outside any repo and is never diverted). The CI step installs the analyzer to
`${XDG_DATA_HOME:-$HOME/.local/share}/rig/cccc/v1.8.4`, the same expression `cccc_install.managed_binary_path` uses (an empty
`XDG_DATA_HOME` counts as unset in both), so the runner's `rig lint check` finds it whatever the runner exports.

## Blockers / follow-ups

- **agent-tools (read-only here):** a catalog `global-hooks.d/pre-commit/30-lint-check` fragment and a `ci/lint-check` slot would let
  the catalog own these files. Until then rig owns them; if agent-tools ships a fragment with the same name the dispatcher install
  would back up and replace it — patch plan: ship it under the same name and delete `riglib/lint_checks_artifacts.py::render_hook_fragment`.
- **How CI installs rig (decided).** `hyper-rig==<version>` does not exist on PyPI for rig's own version (only an old `0.53.0` is
  published), and rig-cli publishes no release tags, so there is no immutable install reference rig can pin by itself: the commit that
  carries this feature is not on the forge until it merges. The workflow therefore installs from the explicit, validated
  `lint_checks.ci.rig_source` (`git+https://<host>/<path>@<40-hex commit sha>` only, so a branch/tag/PyPI range can never be the CI
  gate's rig) and is **not rendered while it is unset** (plan note). Bytes depend on that setting alone, so a rig upgrade does not
  churn committed workflows. The forge repo is public (anonymous `git ls-remote` and `pip install git+https://…@<sha>` verified), so
  no credentials are needed on the runner. After the feature merges, set `lint_checks.ci.rig_source` (global or per repo) to the
  merged commit sha.
- The hook/workflow install only linux x86_64 on CI runners (`ubuntu-latest`); other runner OS/arch need a different pin row.
