# Rig-managed files

Rig distinguishes ownership at file/block granularity instead of treating every generated-looking file the same.

| Class | What Rig owns | Where to make durable changes | Direct edits |
| --- | --- | --- | --- |
| `generated` | the entire target file | global Rig config / `rig.yaml` / generator policy | diagnostic only; the next reconcile may replace them |
| `source-backed` | the entire materialized target copied from a canonical carrier | the named agent-tools source or Rig selection | do not edit the target; change the source and reconcile |
| `marker-managed` | only the region between Rig BEGIN/END markers | Rig config/source for the managed block | content outside the markers is user-owned and safe to edit |

Comment-capable generated targets carry this contract in a syntax-valid header, for example:

```ts
// Managed by Rig. Class: generated. Source of truth: global Rig config + rig.yaml.
```

or:

```yaml
# Managed by Rig. Class: source-backed. Canonical source: agent-tools/linters/ruff.toml.
```

`rig status`/drift logic must use the same ownership boundary as apply: a marker-managed file is not compared as if Rig owned unrelated user text outside its block.

## Strict JSON

Rig does **not** inject fake metadata properties into arbitrary strict JSON. Unknown properties are legal in JSON syntax but many consumers validate a closed application schema, so a made-up `_rig`, `$comment`, or similar key can turn an otherwise valid tool configuration into an invalid one.

The preferred order is:

1. use JSONC/JSON5/YAML/TOML when the consumer officially supports a comment-capable format;
2. use a documented extension field such as `$comment` only when that consumer/schema explicitly permits it;
3. otherwise keep ownership metadata out-of-band in Rig state/status and identify the target there.

This is deliberately capability-driven rather than extension-driven: Rig may add in-band metadata only when it can prove the target tool accepts that metadata without changing semantics. A future per-tool carrier capability can opt strict JSON formats into a known-safe metadata field; the generic JSON writer remains conservative.

## Files carried by `rig profile`

`rig profile` adds a fourth, cross-machine class on top of the three above. Every file it syncs is on
the allow-list in `riglib/profile_files.py` and is `portable`; everything else rig or omp keeps is
`secret-bearing` and never leaves the machine.

| Class | Files | Rule |
| --- | --- | --- |
| `portable` | `~/.config/rig/config.yaml`, the user region of `~/.tmux.conf`, omp `config.yml`, omp `mcp.json` (masked), `sysmon.json` | synced three-way per file; machine values and rig's own managed region stay local |
| `secret-bearing` | `env.vars` values, `~/.omp/auth-broker.token`, omp `agent.db` / `.env`, `~/.omp/remote-host`, `~/.omp/ssh-control`, `~/.config/rig/env/rig.env.sh` | NEVER synced; a secret-looking value in a payload refuses the push |

`~/.tmux.conf` is split by `tmux.split_user_and_managed`: the sentinel block (block mode) and the
`source-file …/rig.tmux.conf` import line (import mode) are marker-managed and stay on the machine;
the rest is the user's and travels.

## Files owned by `lint_checks`

`lint_checks` (default on, see [lint-check.md](lint-check.md)) writes two rig-rendered files and one binary. All three
are reconciled by `rig apply` and compared by `rig status` against the same renderer. The hook and the binary are
machine-wide: they follow the GLOBAL layer's `lint_checks` intent (never one repo's `rig.yaml`); only the workflow is per repo.

| Class | Target | Rule |
| --- | --- | --- |
| `generated` | `<git_hooks.dispatcher.dir>/pre-commit/30-lint-check` (default `~/.config/git/global-hooks.d`) | whole file, executable; pins the absolute `rig` path; carries `# global-hook-protected: true` (a tracked `.githooks-skip` / `GLOBAL_HOOKS_SKIP` cannot disable it); written only after the analyzer verified; a hand edit is backed up (`on_conflict: backup`) and restored |
| `generated` | `.github/workflows/rig-lint-check.yml` | whole file (per repo, from the merged config; not written when that repo disables `lint_checks`), carries the managed header; installs rig from the configured immutable `lint_checks.ci.rig_source` commit and the pinned analyzer (digests verified, placed where `rig` looks) unless that repo itself disables complexity (CI never reads the global config); **not written while `lint_checks.ci.rig_source` is unset**; bytes never depend on the rig version |
| `source-backed` (upstream release) | `${XDG_DATA_HOME:-~/.local/share}/rig/cccc/v<version>/cccc` | byte-identical to the pinned release (sha256 checked on every use); extracted from the tarball's single top-level `cccc` member; never on `PATH`; a mismatching file is backed up without exec bits, never trusted |
