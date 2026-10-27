# `rig lint check` — complexity and file-length gate

Default **on**, for every repo, with no Oxc, Biome or `linters.enabled` involved. It answers two questions:

1. Does any function exceed the cognitive (> 15) or cyclomatic (> 20) complexity limit? A value **equal** to the limit passes.
2. Is any text file too long? ≤ 1200 physical lines is fine, 1201–2400 is a **warning**, > 2400 is an **error**. Only `.json` is exempt by default; lockfiles such as `uv.lock` are *not* exempt and are never modified.

Complexity is measured by the official [`moznion/cccc`](https://github.com/moznion/cccc) analyzer, pinned to an immutable release
(`v1.8.4`) and verified by sha256 on every use. Design and rejected alternatives: [specs/2026-10-26-cccc-lint-check.md](specs/2026-10-26-cccc-lint-check.md).

## Configuration

```yaml
lint_checks:
  enabled: true            # master switch: activation (hook + CI) and `rig lint check`
  complexity: {enabled: true, max_cognitive: 15, max_cyclomatic: 20}
  file_lines: {enabled: true, warn_above: 1200, error_above: 2400, exempt_extensions: [json]}
```

Cascade: built-in defaults → `~/.config/rig/config.yaml` → `./rig.yaml`; the repo layer wins. Unknown keys, wrong types and
`warn_above > error_above` inside `lint_checks` are config errors (exit 2) that name the key and the layer. An error in any OTHER
block (a key a newer rig added, another block's typo) is not fatal here, because the commit hook runs this on every commit: it is
printed to stderr as `warning: ignoring a config error outside lint_checks: ...` and `lint_checks` is resolved from the raw
layers (an unreadable/unparseable file still exits 2). `enabled: false` makes the check report
`disabled` (exit 0, never "pass") wherever the merged config says so. It removes the machine-wide activation (analyzer + hook) only
from the **global** layer: a repo's own `enabled: false` / `complexity.enabled: false` affects that repo's check and its CI workflow,
never the machine-wide artifacts (see Activation). Full key reference:
[config-schema.md](config-schema.md).

The generated CI workflow sees only the built-in defaults plus the repository's `rig.yaml` (no global config), so thresholds
that CI must enforce have to be set in the repo's `rig.yaml`; a global override only affects local runs and the commit hook.

## Running it

```
rig lint check [PATH ...] [-C DIR] [--config FILE] [--json] [--staged] [--fix]
```

| Exit | Meaning |
| --- | --- |
| 0 | no error-severity finding (warnings are allowed and always listed) |
| 1 | at least one error finding |
| 2 | usage or config error (also `--fix` combined with `--staged`, or on a partially staged file: refused, nothing written) |
| 3 | analysis **incomplete**: parse error, analyzer crash/timeout, malformed output, unreadable file, a requested file missing from (or duplicated/unexpected in) the analyzer's answer |
| 127 | analyzer missing or not byte-identical to the pin — run `rig apply commit` (the document lists an `analyzer-unavailable` incomplete record under the pseudo-path `<analyzer>`) |

`--json` prints one `rig-lint-check/v1` document (`ok`, `exit_code`, `analyzer`, effective `config`, per-check `status`,
`findings`, `skipped`, `incomplete`, `fix`, `summary`). `--staged` checks the **index** content (what would be committed), not the
working tree; `rig lint rules` is unchanged. The *policy* (thresholds, `enabled`) is always read from the global config and the
working-tree `rig.yaml`, never from the index: edit-and-commit of `rig.yaml` is the supported way to change a repo's limits, and an
uncommitted `lint_checks` edit applies to the next `--staged` run too.

The staged snapshot has a 64 MiB aggregate Git-output budget, including object headers. Exceeding it
reports an incomplete check (exit 3), never a pass; limit the selected paths for a manual inspection
or reduce the staged batch. Exempt JSON blobs are not requested and do not consume this budget.
Analyzer stdout and stderr also have a combined 64 MiB budget per batch. Excess output stops the
analyzer and reports an incomplete check; it cannot silently turn an oversized result into a pass.

## Activation (`rig apply commit`)

Three actions, all idempotent. `install_cccc` and `install_lint_check_hook` are machine-wide (status area `lint_checks_machine`, GLOBAL, visible outside a repo); the workflow is the repo's (area `lint_checks`, REPO). Because the analyzer and the hook serve **every** repo on the machine, the **hook** is decided by the global layer (built-in defaults + `~/.config/rig/config.yaml`) alone, never by the current repo opting out — otherwise one repo's opt-out would write a hook (or leave `extra` drift) that every other repo then trips over. The per-machine, harmless **analyzer** is installed whenever any layer will run complexity: the global layer (`enabled` and `complexity.enabled`) or the repo being applied — a manual `rig lint check` is also a consumer, so disabling the dispatcher and CI does not prevent analyzer installation. Turning off both global and project complexity stops installation; an already installed analyzer is retained as a harmless cached tool, not silently deleted or reported as unwanted drift. So a repo that enables the check against a global opt-out gets the analyzer from its own `rig apply commit` (the fix its exit-127 message names) but no hook; and the hook, wherever the global switch has it on, is gated on that verified analyzer. A repo that enables `complexity` against a global complexity opt-out, before it has applied, gets exit 127 with the fix message (loud, never a silent pass). A repo's `lint_checks.enabled: false` removes only its own workflow and creates no drift; a repo's own `complexity.enabled: false` only makes its CI workflow skip the analyzer download. CI never reads the global config, so a complexity opt-out that exists only in the global layer does not drop the workflow's analyzer step.

| Action | Writes | Notes |
| --- | --- | --- |
| `install_cccc` | `~/.local/share/rig/cccc/v1.8.4/cccc` (under whatever `HOME`/`XDG_DATA_HOME` rig runs with) | tarball sha256 → safe extraction of the single top-level `cccc` member (the layout of the official release tarball; the CI step extracts that same member) → binary sha256 → atomic rename. A wrong file is backed up de-executable (`on_conflict: backup`), never trusted. Unsupported OS/CPU is a loud `BLOCKED` error; a download failure (offline, truncated, reset) is a loud `error` (`rig apply commit` exits non-zero). Planned when the global layer or the applying repo runs complexity, whatever the global hook switch says; not planned when neither does (the file-line check needs no analyzer). It installs under an alternate `HOME` too (bot workers, CI containers) so `rig status` and `rig lint check` find it; only an explicit `RIG_CCCC_DRY_RUN=1` reports `dry-run` instead of downloading. |
| `install_lint_check_hook` | `<dispatcher dir>/pre-commit/30-lint-check` | runs `rig lint check --staged --json` through the existing global git-hook dispatcher with the absolute `rig` path pinned at apply time; any non-zero exit (or a missing `rig`) blocks the commit. Needs `git_hooks.dispatcher.enabled`; it is not planned (explicit plan note) when the platform has no pinned analyzer (only matters while the global layer or the applying repo runs complexity) or no absolute `rig` can be pinned. **Ordered after `install_cccc` and written only while the analyzer verifies**: if the install failed the hook is NOT written and the action is an `error` (never a newly enabled machine-wide hook that blocks every commit with 127 from a failed install); with `RIG_CCCC_DRY_RUN=1` it is reported as skipped. The fragment carries `# global-hook-protected: true`: a tracked `.githooks-skip` line or the `GLOBAL_HOOKS_SKIP` env var (both forgeable by a cloned repo) cannot disable it; only the operator's own `git config hooks.skipGlobal lint-check` (or `fragments.lint-check.enabled: false`) opts a repo out, and a repo's `lint_checks.enabled: false` makes the hook report `disabled` and pass there. A fragment left installed after the **global** layer (or the fragment switch) turned it off is reported as drift. |
| `install_lint_check_workflow` | `.github/workflows/rig-lint-check.yml` | whole-repo `rig lint check --json`; SHA-pinned `actions/checkout`, read-only token, `pull_request` (never `pull_request_target`), the analyzer verified against both digests and installed to `${XDG_DATA_HOME:-$HOME/.local/share}/rig/cccc/v1.8.4` — the exact lookup rig itself uses (omitted only when the repo itself disables complexity; CI sees built-in defaults plus the repo's `rig.yaml`, never the global config), rig installed with `pip install "<lint_checks.ci.rig_source>"`. **Only written when `lint_checks.ci.rig_source` is set** to an immutable `git+https://<host>/<path>@<40-hex commit sha>` of a rig-cli that contains `rig lint check` (otherwise a plan note says so: no installable release exists, and an unresolvable pin would leave the job red). The bytes depend on that setting only, never on the rig version. Not written when the repo's merged `lint_checks.enabled` is false, `ci.enabled: false` or `ci.target` is not `.github/workflows`. |

`rig status` reports a missing/mismatched/wrong-version analyzer, and a missing, edited or non-executable hook or workflow, as drift.

## `--fix`

Only one rewrite exists: `fix/else-after-return` (details and safety rules in the design doc). Everything else stays a diagnostic
that needs a real refactor. After a write the real analyzer is re-run and the final JSON/exit code reflect the post-fix state.

- **Scoped to the flagged functions.** Only sites whose nearest enclosing function is an over-limit function (matched by the
  analyzer's line and name) are rewritten; every other function, including a closure nested inside a flagged one, stays
  byte-identical (an unnamed closure on the same line is never matched by another function's name). A finding is `fixable` only
  when such a site exists. Finding the sites needs the repository's own TypeScript module, i.e. running project code, so only an
  explicit `--fix` does it: a plain or `--staged` scan (including the global commit hook) never spawns node/bun and reports
  `fixable: false` ("not confirmed fixable"), while `--fix` still discovers and rewrites every eligible site. Even under `--fix`
  a `node_modules/typescript` that git tracks (a committed copy is repo-supplied code, not an install product) or that git cannot
  vouch for is never loaded: the file is skipped with reason `parser-untrusted`.
- **Never worse.** After the re-run a rewrite is reverted (`fix.reverted[]`) when its file gained any finding of any rule
  (`introduced-by-fix`, e.g. an `else` -> bare block adds a line and crosses a file-lines limit), an existing size violation grew,
  a function's metrics drifted, or no function got less complex (`no-effect`). A partial reduction inside a flagged function that is
  still over the limit is kept but never listed as resolved. A revert that fails makes the run incomplete (exit 3); an exception
  or Ctrl-C mid-fix rolls every unverified rewrite back.
- **Attribution limit.** Findings are matched before/after by (rule, path, function name, nth duplicate); two same-named functions in one
  file can have a fix credited to the wrong one in `fix.applied[].resolved` (the metrics check and revert logic are unaffected).
- **Cost.** `--fix` spawns one node/bun process per flagged file (twice: once to discover, once to rewrite) and re-runs the analyzer
  over the whole scope after writing (again after a revert), so its cost grows with the number of flagged files; this is a known limit.
- **Writes are pinned.** Every path component is opened `O_NOFOLLOW` from a root directory fd and the temp file and `os.replace`
  go through that fd, so a directory swapped for a symlink mid-run cannot redirect a write out of the repository. The repository
  root is opened `O_NOFOLLOW` too.
- **Known limit.** A file is compared with what the parser saw and then renamed over, so a process that rewrites the same file in
  that window loses its edit (inherent to rename-over writers).
- **Index is never touched.** A rewritten file whose index content differs from HEAD is listed in `fix.notes[]` with state
  `needs-restage` (plus a text line): the staged copy still has the pre-fix bytes, stage the file yourself. Partially staged files
  are refused (exit 2).
- `fix.requested` is `true` whenever `--fix` was passed, including on exit 2, 127 and a disabled check.
