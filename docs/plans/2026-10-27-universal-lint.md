# Universal `rig lint` — design + implementation plan

Ticket: rig-cli#781 (commits use `Refs #781`, never `Closes`). Branch/worktree: `universal-lint-781`.
Status of this file: the contract. Implementers code against Section 3 verbatim; a signature change needs the
`DesignOwner`/`Main` to amend this file first.

## 0. Goal and non-goals

Bare `rig lint` and `rig lint run` aggregate **every applicable check** for the repository (languages, manifests,
nested packages, platform, characteristics) through one central, extensible **provider registry**, on top of the
existing machinery (`lint_policy.py`, `linter_environment.py`, `linter_carriers.py`, `lint_rules_cli.py`,
`lint_check*.py`, `plan.py` / `actions/runner.py` / `schema.py` / `config_schema.py`). It is **not** a second
toolchain: bootstrap goes through `InstallPlan` + `Action` + `actions.run_plan`.

Unchanged and backward compatible: `rig lint check` (CCCC, `rig-lint-check/v1`, exits 0/1/2/3/127, `docs/lint-check.md`)
and `rig lint rules`. `lint_check*.py` is **not edited** by this ticket (the aggregator shells out to it, Section 3.4).

Non-goals (stated in user docs so Rig never claims exhaustive support): languages without a provider
(Kotlin, Java, Ruby, PHP, C/C++, C#, Swift, Deno projects, ...) are **reported** as `unknown_languages`, never silently
passed. No per-file exemptions, no relaxed thresholds, no `exclude` config keys (a tool's own ignore files remain the
project's business).

## 1. Decisions (resolved so implementers do not re-litigate)

| # | Decision | Why |
| --- | --- | --- |
| D1 | Five states per check: `passed`, `failed`, `missing-tool`, `not-applicable`, `unsupported-platform` (+ `planned`, only under `--plan`). A `reason` string refines each. | Never PASS for unavailable/skipped. |
| D2 | Exit priority `2 > 127 > 3 > 1 > 0`, identical to `rig lint check`. 127 = an applicable check could not run (missing-tool or unsupported-platform). 3 = analysis incomplete (timeout, crash, malformed output, malformed manifest, failed bootstrap). 1 = findings. Zero applicable checks => exit 0 with `summary.applicable == 0` and the unknown-language list printed. | Consistency with `docs/lint-check.md`. |
| D3 | **Configured tools are authoritative.** A role (`lint`, `format`, `typecheck`) in a scope that already has any configured tool (config file, config field, or declared dependency) is never bootstrapped and never replaced; Rig runs that tool (or the project script that runs it). A configured tool with no Rig provider is listed in `unsupported_tools`, still never replaced. | Ticket requirement. |
| D4 | Bootstrap defaults only for roles with **no** configured tool: JS/TS => `oxlint` (lint) + `oxfmt` (format); Python => `ruff` (lint + format). No default for `typecheck`, shell, workflows, Rust, Go (those run only when the language/characteristic is present; their binaries are system tools, never installed by Rig). | Ticket requirement. |
| D5 | Bootstrap installs **repo-local, exact-pinned, lock-updating** packages via the detected manager. Pins live in code (`lint_providers.PINS`), overridable per key by `lint.bootstrap.versions`. A PATH-global Oxlint/Ruff does **not** count as installed (CI/other devs cannot reproduce it) -- same rule `linter_environment.py` already states. | Reproducibility. |
| D6 | Bootstrap runs only for **bare local** `rig lint` (not CI, not `--no-setup`, not `--plan`, `lint.bootstrap.enabled` true). Read-only modes still compute and print what *would* be installed. Defense in depth: the runner handler itself refuses to execute when `CI`/`GITHUB_ACTIONS` is truthy or `allow_mutation` is not `true` in the action. Never creates a manifest (`package.json`, `pyproject.toml`). | Ticket: CI never modifies manifests. |
| D7 | JS/TS manager selection order: (1) `packageManager` field of the owning `package.json`; (2) exactly one lockfile (`package-lock.json`->npm, `pnpm-lock.yaml`->pnpm, `yarn.lock`->yarn (berry iff `.yarnrc.yml` or `packageManager: yarn@>=2`), `bun.lock`/`bun.lockb`->bun); (3) `pnpm-workspace.yaml`->pnpm; (4) >=2 lockfiles and no field => **ambiguous**, bootstrap blocked (`ambiguous-package-manager`); (5) no signal and `package.json` present => `npm`. The manager binary must be on PATH, else blocked (`manager-not-installed`); Rig never falls back to a different manager. Python: `uv.lock`/`[tool.uv]`->uv, `poetry.lock`/`[tool.poetry]`->poetry, `pdm.lock`/`[tool.pdm]`->pdm; anything else (`pip`, bare `requirements.txt`) has no lock => bootstrap blocked (`no-lock-capable-manager`) with a remediation text. | Reproducible locks only. |
| D8 | Project scripts: only `package.json` scripts and `Makefile` targets matching a strict **name allow-list**; run as argv (`[<pm>, "run", <name>]`, `["make", "--no-print-directory", "-C", <dir>, "--", <target>]`), never through a shell, discovered names never interpolated. Scripts whose command tokens contain `rig lint`, or any writer token (`--fix`, `--write`, `-w`, `--watch`), are `not-applicable` (`self-referential-script`, `writes-files`). Nested `rig lint` (env `RIG_LINT_DEPTH>=1`) skips every project-script check (`recursion-guard`). | Recursion + safety. |
| D9 | Overlap/dedup is by **capability key** (`<role>:<language>:<scope>`) with precedence project-script (0) > configured tool (1) > bootstrap default (2) > Rig built-in (3). The loser becomes `not-applicable` with reason `covered-by:<check id>`. This is how "no duplicate TS typecheck" is enforced (tsc vs `typecheck` script vs Oxlint `options.typeCheck`). | Ticket requirement. |
| D10 | Type-aware Oxlint is gated: `options.typeAware` and the `typescript/*` type-aware rules are emitted/active only when `oxlint-tsgolint` is declared **and** the resolved TypeScript major is >= 7. Rig never passes `--type-check` and never forces TS7 on a TS5 repo. TS5/6 repos keep `tsc --noEmit` (when configured) as their typecheck. Inactive rules are listed with a reason by `rig lint rules`. | https://oxc.rs/docs/guide/usage/linter/type-aware (TS >= 7.0 required; `--type-check` is experimental). |
| D11 | Plain detection never imports or executes repo code: JSON/JSONC/TOML are parsed with the stdlib, YAML/JS/TS configs are matched by **file name only** (plus bounded regex for the single fact `typeCheck: true`), Makefiles by a bounded regex. Only `rig lint run` (not discovery) executes project tools/scripts, exactly as CI would. | Ticket requirement. |
| D12 | `rig lint run` requires a git repository (same as `rig lint check`): enumeration reuses `lint_check_scope.worktree_scope` (tracked + untracked-not-ignored, NUL-separated, symlinks skipped with a reason). Not a repo => exit 2. | One enumerator, one symlink policy. |
| D13 | Checks run **serially in a deterministic order** (scope path, category rank, provider id), each with a bounded timeout; a failure never stops later checks. | Workstation friendliness + reproducible output. |
| D14 | Config is a new top-level block `lint` (global + project, project wins per key, same cascade as `lint_checks`). Provider commands/argv are never configurable (a committed rig.yaml must not inject commands); only on/off and timeouts. | Safety. |

### Obsolete assumptions to fix in `linter_environment.py` / `plan.py` / `lint_policy.py`

1. `@oxlint/plugins` is required for every Oxc repo. Wrong: `defineConfig` comes from `oxlint` itself; `@oxlint/plugins` is only imported by
   the vendored **anti-slop** JS plugin (`agent-tools/vendor/anti-slop/.../rules/*.ts` import `defineRule` from it). It is required **iff**
   the resolved policy needs the anti-slop plugin (`anti_slop_required`).
2. `render_oxlint_config` always writes `options: { typeAware: true }`. Needs `oxlint-tsgolint` + TypeScript >= 7 (D10).
3. `oxlint.config.ts` is the only generated flavor. Oxlint TS configs need Node >= 22.18 (or >= 24) and the Node-based package
   (https://oxc.rs/docs/guide/usage/linter/config); older Node needs `.oxlintrc.json`. A directory may hold only one flavor.
4. Detection reads the root `package.json` and root globs only (no nested packages/workspaces; `.oxlintrc.json(c)` and `oxlint.config.{ts,mts}`
   are not recognised as "already configured"). Replaced by `lint_discovery`.
5. The agent prompt tells the agent to install `oxfmt` and `@oxlint/plugins` unconditionally. Make the prompt reflect `required_oxc_packages(...)`.

## 2. Module split and single ownership

One owner per file. Owners may create throwaway local stubs of other owners' modules to develop against Section 3, but may
**not** edit files they do not own. `Main` integrates and runs the focused test files one at a time (max one pytest file per invocation).

| Owner | Files (new unless marked *edit*) | Tests (own) |
| --- | --- | --- |
| **I1 Discovery + Config + Docs** | `riglib/lint_discovery.py`; `riglib/lint_run_config.py`; `riglib/linter_environment.py` *edit*; `riglib/config.py` *edit* (`_VALID_TOP_KEYS` += `lint`, `validate()` hook); `riglib/config_schema.py` *edit* (`_LINT_BLOCK`, `lint` entry next to `lint_checks`, ~L1950); `riglib/schema.py` *edit* (Area `lint`); `schema/rig.schema.json` regenerated; `docs/lint.md` (user doc), `docs/config-schema.md` *edit* (`## lint`), `docs/lint-check.md` *edit* (one "see also" line only), `README.md` *edit* (command table row), `CHANGELOG.md` *edit* (`## Unreleased`) | `tests/test_lint_discovery.py`, `tests/test_lint_run_config.py`, `tests/test_linter_environment.py` *edit*, pinned-set edits in `tests/test_config_schema.py`, `tests/test_setup_wizard.py` (add `lint` to the expected-category set at ~L37 and to both unscaffolded/REPO allow-lists at ~L364/L384), `tests/test_config.py` only if it pins `_VALID_TOP_KEYS` |
| **I2 Providers** | `riglib/lint_providers.py` (registry, `PINS`, dataclasses, every `ProviderSpec`, tool resolution, script classification, overlap resolution, `bootstrap_needs`, `unsupported_tools`) | `tests/test_lint_providers.py` |
| **I3 Run + CLI** | `riglib/lint_run.py` (executor, aggregation, JSON, fix, exit codes, text); `riglib/lint_run_cli.py` (argparse namespace -> `RunOptions`, emit); `riglib/cli.py` *edit* (`_add_lint_parser`, `cmd_lint` only); `riglib/lint_rules_cli.py` *edit* (type-aware `active`/`inactive_reason`) | `tests/test_lint_run.py`, `tests/test_lint_run_cli.py`, `tests/test_lint_cli_and_formatter.py` *edit* only if it pins the old bare-`rig lint` help behavior |
| **I4 Bootstrap + Policy + Actions** | `riglib/lint_bootstrap.py`; `riglib/lint_policy.py` *edit*; `riglib/plan.py` *edit* (`_build_linters` L2728-2915 only: use new env fields, no behavior change for apply otherwise); `riglib/actions/runner.py` *edit* (new handler + `_HANDLERS` entry); `riglib/action_tags.py` *edit* | `tests/test_lint_bootstrap.py`, `tests/test_lint_policy.py` *edit*, `tests/test_action_tags.py` *edit* (completeness), `tests/test_linters.py` *edit* if affected |

Cross-owner touch points are **only** the Section 3 signatures. Dependency order for integration: I1 and I2 leaf modules first (I2 imports I1 dataclasses),
then I4 (imports I1+I2), then I3 (imports all). Because contracts are frozen, all four start immediately.

Style rules for every owner: stdlib-only at import time (AGENTS.md; `yaml` lazily only if unavoidable -- this feature needs none), file-header
docstring stating context/trust boundary, `from __future__ import annotations`, frozen dataclasses, no `Any` where a dataclass fits, no
shell=True anywhere, comments explain why.

## 3. Contract (exact public surface)

### 3.1 `riglib/lint_discovery.py` (I1)

```python
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping, Sequence

Role = Literal["lint", "format", "typecheck"]
EvidenceKind = Literal["manifest", "lockfile", "config", "field", "source", "script"]

# closed language/trait vocabulary used by providers
LANG_JS, LANG_TS, LANG_PY, LANG_RUST, LANG_GO, LANG_SHELL = (
    "javascript", "typescript", "python", "rust", "go", "shell")
TRAIT_GH_WORKFLOWS = "github-workflows"

@dataclass(frozen=True)
class Evidence:
    path: str                      # repo-relative POSIX, never absolute
    kind: EvidenceKind
    detail: str = ""

@dataclass(frozen=True)
class PackageManager:
    ecosystem: Literal["js", "python"]
    name: str                      # npm|pnpm|yarn|yarn-berry|bun|uv|poetry|pdm|pip ; "" when ambiguous
    source: str                    # "packageManager field"|"lockfile"|"workspace marker"|"default"|"ambiguous"
    lockfile: str | None           # repo-relative
    root: str                      # repo-relative dir owning manifest+lock (workspace root); "." for repo root
    ambiguous: tuple[str, ...] = ()  # candidate names when conflicting evidence

@dataclass(frozen=True)
class ConfiguredTool:
    tool: str                      # provider-neutral id: eslint biome prettier oxlint oxfmt tsc ruff black flake8 pylint mypy pyright ... (open vocabulary)
    role: Role
    declared: bool                 # dependency / tool table present in the manifest
    config_files: tuple[str, ...]  # repo-relative, existing, non-symlink
    scripts: tuple[str, ...]       # names of scripts whose tokens mention the tool
    evidence: tuple[Evidence, ...]
    options: Mapping[str, str]     # tiny facts only, e.g. {"type_check": "true"} (Oxlint options.typeCheck)

@dataclass(frozen=True)
class ProjectScript:
    runner: Literal["package.json", "Makefile"]
    name: str
    command: str                   # raw text, classification only, NEVER executed via shell
    tokens: tuple[str, ...]        # lowercased executable/package tokens
    invokes_rig_lint: bool
    writes_files: bool             # --fix/--write/-w/--watch token present
    roles: tuple[Role, ...]        # from the name allow-list (empty => not a lint/check script)
    covers_tools: tuple[str, ...]  # configured-tool ids found in tokens

@dataclass(frozen=True)
class TypeScriptInfo:
    declared_spec: str | None
    installed_version: str | None  # read from node_modules/typescript/package.json (JSON only)
    major: int | None
    tsconfigs: tuple[str, ...]     # repo-relative tsconfig*.json that exist in the scope dir

@dataclass(frozen=True)
class ProjectScope:
    rel: str                       # "." or POSIX relative dir
    languages: tuple[str, ...]     # sorted, closed vocabulary above
    package_manager: PackageManager | None
    tools: tuple[ConfiguredTool, ...]
    scripts: tuple[ProjectScript, ...]
    typescript: TypeScriptInfo | None
    files: Mapping[str, tuple[str, ...]]   # language/trait -> owned repo-relative files; ONLY shell, go, github-workflows
    evidence: tuple[Evidence, ...]

@dataclass(frozen=True)
class UnknownLanguage:
    language: str                  # e.g. "kotlin"
    files: int
    examples: tuple[str, ...]      # <= 3 repo-relative paths

@dataclass(frozen=True)
class DiscoveryProblem:
    path: str
    kind: Literal["malformed-manifest", "malformed-config", "unreadable", "symlink-escape"]
    detail: str

@dataclass(frozen=True)
class Discovery:
    root: Path                     # resolved git toplevel
    scopes: tuple[ProjectScope, ...]       # sorted by rel; synthetic "." scope exists iff root-level shell/workflows exist
    unknown: tuple[UnknownLanguage, ...]
    problems: tuple[DiscoveryProblem, ...]
    skipped_symlinks: tuple[str, ...]
    file_count: int
    toml_parser: bool              # False on Python 3.10 without tomli: TOML facts fall back to table-header scan
    def scope(self, rel: str) -> ProjectScope | None: ...
    def ancestors(self, rel: str) -> tuple[ProjectScope, ...]: ...   # nearest first, includes rel itself

def discover(root: str | Path, *, scopes: Sequence[str] = ()) -> Discovery:
    """Enumerate via lint_check_scope.worktree_scope (git only); raises lint_check_scope.ScopeError when not a repo.
    Never raises for malformed manifests/configs: they become DiscoveryProblem entries."""

def parse_jsonc(text: str) -> object:   # stdlib comment/trailing-comma tolerant; ValueError on failure
```

Rules (tests pin them): a scope exists per dir holding `package.json`, `pyproject.toml`, `setup.py`/`setup.cfg`,
`requirements*.txt`, `Cargo.toml` (workspace roots only; members are not scopes), `go.mod`; `PackageManager.root` is the nearest ancestor owning the lock /
workspace marker. A tool configured in an ancestor scope is **not** re-reported in descendants. `Deno` (`deno.json[c]`) => languages
recorded, no scope tools, files counted as `unknown` language `deno`. Extension -> language table for `unknown` is closed and
covers only code extensions (`.kt .java .rb .php .c .h .cpp .cs .swift .lua .ex .exs .scala .dart .zig .hs .clj .pl .sql`, ...);
docs/data (`.md .json .yaml .txt`) are never "unknown languages". Config-file name tables:

- JS: eslint `eslint.config.{js,mjs,cjs,ts,mts,cts}`, `.eslintrc{,.js,.cjs,.json,.yaml,.yml}`, field `eslintConfig`; biome `biome.json{,c}`;
  prettier `.prettierrc{,.json,.json5,.yaml,.yml,.toml,.js,.cjs,.mjs,.ts}`, `prettier.config.*`, field `prettier`; oxlint `.oxlintrc.json{,c}`,
  `oxlint.config.{ts,mts}`; oxfmt `.oxfmtrc.json{,c}`; dprint/tslint/xo/standard recorded as tools without a provider.
- Python: ruff `ruff.toml`, `.ruff.toml`, `[tool.ruff]`; black `[tool.black]`; flake8 `.flake8`, `[flake8]` in `setup.cfg`/`tox.ini`; pylint `.pylintrc`,
  `pylintrc`, `[tool.pylint]`; mypy `mypy.ini`, `.mypy.ini`, `[tool.mypy]`, `[mypy]`; pyright `pyrightconfig.json`, `[tool.pyright]`.
- Dependency declarations: `package.json` (4 dep blocks), `pyproject.toml` (`[project].dependencies`, optional-dependencies, `[dependency-groups]`,
  `[tool.poetry.*dependencies]`, `[tool.pdm.dev-dependencies]`, `[tool.uv].dev-dependencies`), `requirements*.txt` names.
- Script name allow-list (D8), exact regex, case-sensitive: `^(lint|typecheck|type-check|check-types|tsc)(:[a-z0-9-]+)?$` and
  `^(format|fmt|prettier)[:-]check$` and `^check[:-](format|fmt|lint|types)$`; names whose `:`/`-` segments include
  `fix write watch staged changed dev build test` are excluded. Makefile: same allow-list (hyphen form) on `^name:` targets, file <= 1 MiB, regex only.
- Symlinked manifests/configs are never read (`DiscoveryProblem(kind="symlink-escape")` when a symlink points outside the repo, else silently
  listed in `skipped_symlinks`). Reads use `lint_check_scope.inspect_file`-style no-follow opens.

### 3.2 `riglib/linter_environment.py` (I1) -- thin adapter over discovery

```python
@dataclass(frozen=True)
class LinterEnvironment:
    ready: bool
    oxlint_present: bool
    foreign_linters: tuple[str, ...]
    missing_oxc_packages: tuple[str, ...]   # ONLY what the active policy needs (see required_oxc_packages)
    reason: str
    agent_prompt: str
    type_aware: bool                        # oxlint-tsgolint declared AND TypeScript major >= 7
    type_aware_reason: str
    config_files: tuple[str, ...]           # existing oxlint config files (.oxlintrc.json{,c}, oxlint.config.{ts,mts})

def required_oxc_packages(*, anti_slop: bool, type_aware: bool) -> tuple[str, ...]:
    """("oxlint",) + ("@oxlint/plugins",) iff anti_slop + ("oxlint-tsgolint",) iff type_aware wanted."""
def inspect_linter_environment(repo_root: Path, *, anti_slop: bool = False) -> LinterEnvironment
def inspect_formatter_environment(repo_root: Path) -> FormatterEnvironment    # unchanged shape
```
`FormatterEnvironment` and the foreign-name labels (`"ESLint"`, `"Biome"`, ...) stay as today (existing tests pin them); detection now sources from
`discover()` at the repo root scope (still root-only semantics for `plan.py` apply, so `rig apply` output does not change except the package fixes above).

### 3.3 `riglib/lint_run_config.py` (I1)

```python
Category = Literal["lint", "typecheck", "format", "limits", "scripts"]
CATEGORIES: tuple[Category, ...]          # rank order for sorting

@dataclass(frozen=True)
class ProviderOverride:
    enabled: bool = True
    timeout_seconds: int | None = None

@dataclass(frozen=True)
class BootstrapConfig:
    enabled: bool                          # default True
    javascript: bool                       # default True
    python: bool                           # default True
    versions: Mapping[str, str]            # exact versions only, key = PINS key; default {}

@dataclass(frozen=True)
class LintConfig:
    enabled: bool                          # default True (master switch)
    timeout_seconds: int                   # default 300, 1..3600, per check
    categories: Mapping[Category, bool]    # all five keys, default True
    bootstrap: BootstrapConfig
    providers: Mapping[str, ProviderOverride]   # keys validated against lint_providers.PROVIDER_IDS
    lint_checks_enabled: bool              # resolved lint_checks.enabled (so the limits provider can say disabled-by-config)
    linters_enabled: bool                  # linters.enabled
    linters_rules: Mapping[str, object]    # cascaded linters.rules (feeds lint_policy / bootstrap)

DEFAULTS: LintConfig
def validate_block(block: object, *, layer: str) -> None        # ConfigError(schema_path=...) naming key + layer
def resolve(data: Mapping[str, object], layer_blocks: Mapping[str, object] | None = None) -> LintConfig
def load_config(cwd: str, config_path: str | None) -> LintConfig
    # same leniency as lint_check.load_config: an error in an UNRELATED block is a stderr warning, an error inside `lint` is fatal (ConfigError)
```
`validate_block` imports `PROVIDER_IDS` lazily from `lint_providers` (I2) so there is no import cycle.

### 3.4 `riglib/lint_providers.py` (I2)

```python
CheckState = Literal["passed", "failed", "missing-tool", "not-applicable", "unsupported-platform"]

@dataclass(frozen=True)
class PinnedPackage:
    name: str; version: str; ecosystem: Literal["npm", "pypi"]
PINS: Mapping[str, PinnedPackage]   # keys: oxlint oxfmt oxlint-tsgolint @oxlint/plugins ruff

@dataclass(frozen=True)
class ToolEnv:                       # injected seams; tests fake all of them
    root: Path
    which: Callable[[str], str | None]
    system: str                      # "linux" | "darwin" | "windows" | other
    machine: str                     # "x86_64" | "arm64" | ...
    node_version: tuple[int, int, int] | None
    environ: Mapping[str, str]
    depth: int                       # RIG_LINT_DEPTH

@dataclass(frozen=True)
class ToolResolution:
    kind: Literal["local-bin", "manager-run", "path", "missing", "escaped"]
    argv_prefix: tuple[str, ...]     # e.g. ("/abs/repo/node_modules/.bin/oxlint",) or ("uv","run","--frozen","--","ruff")
    path: str | None                 # display path (repo-relative for local-bin)
    detail: str = ""

@dataclass(frozen=True)
class Unavailable:
    state: Literal["missing-tool", "not-applicable", "unsupported-platform"]
    reason: str                      # machine-readable, see 3.7
    detail: str                      # one human sentence incl. remediation

@dataclass(frozen=True)
class PlannedCheck:
    id: str                          # "<provider>@<scope>" ; scripts: "script:<runner>:<name>@<scope>"
    provider: str
    category: Category
    scope: str
    tool: str
    precedence: int                  # 0 script, 1 configured, 2 bootstrap default, 3 built-in
    covers: frozenset[str]           # capability keys "<role>:<language>:<scope>"
    argv: tuple[str, ...] | None     # None iff unavailable is set
    cwd: str                         # repo-relative
    unavailable: Unavailable | None
    timeout_s: int | None            # provider override else None (runner uses config)
    fix_argv: tuple[str, ...] | None # safe fix variant; None => --fix unsupported for this check
    result_rule: Literal["exit-zero", "stdout-empty"]   # gofmt -l
    resolution: ToolResolution | None

@dataclass(frozen=True)
class PlanContext:
    discovery: Discovery; scope: ProjectScope; cfg: LintConfig; env: ToolEnv

@dataclass(frozen=True)
class BootstrapSpec:
    ecosystem: Literal["js", "python"]; role: Role; packages: tuple[str, ...]   # PINS keys

@dataclass(frozen=True)
class ProviderSpec:
    id: str; tool: str; category: Category
    languages: tuple[str, ...]
    platforms: frozenset[str] | None          # None = all; else subset of {"linux","darwin","windows"}
    bootstrap: BootstrapSpec | None
    fix_modes: tuple[str, ...]                # documentation strings, e.g. ("ruff check --fix (safe fixes only)",)
    plan: Callable[[PlanContext], tuple[PlannedCheck, ...]]

@dataclass(frozen=True)
class BootstrapNeed:
    scope: str; ecosystem: Literal["js", "python"]; role: Role
    tool: str; provider: str; packages: tuple[PinnedPackage, ...]

def register(spec: ProviderSpec) -> ProviderSpec          # ValueError on duplicate id
def providers() -> tuple[ProviderSpec, ...]               # stable order: sorted by id
def provider(provider_id: str) -> ProviderSpec            # KeyError when unknown
PROVIDER_IDS: frozenset[str]
def plan_checks(discovery: Discovery, cfg: LintConfig, env: ToolEnv) -> tuple[PlannedCheck, ...]
    # all providers x scopes; applies cfg.categories/providers (-> not-applicable disabled-by-config),
    # D8 script rules, resolve_overlaps; sorted (scope, category rank, id); deterministic
def bootstrap_needs(discovery: Discovery, cfg: LintConfig, env: ToolEnv) -> tuple[BootstrapNeed, ...]
def resolve_overlaps(checks: Sequence[PlannedCheck]) -> tuple[PlannedCheck, ...]
def resolve_tool(tool: str, scope: ProjectScope, discovery: Discovery, env: ToolEnv) -> ToolResolution
def unsupported_tools(discovery: Discovery) -> tuple[tuple[str, str], ...]   # (scope, tool) configured with no provider
def chunk_files(files: Sequence[str], *, max_bytes: int = 100_000) -> tuple[tuple[str, ...], ...]  # argv-size safe; each path prefixed "./" if it starts with "-"
```

Tool resolution (`resolve_tool`): (1) `node_modules/.bin/<tool>` walking from the scope dir **up to the repo root only** (never above); (2) `.venv/bin/<tool>`
(`Scripts\<tool>.exe` on Windows); (3) `manager-run` when the manager is uv/poetry/pdm (`uv run --frozen --`, `poetry run`, `pdm run`) and the tool is declared; (4) PATH
**only** for system tools (`shellcheck shfmt actionlint cargo go gofmt make`) and package-manager executables. Any candidate whose `realpath` leaves the repo
root => `kind="escaped"`, reported as `missing-tool` reason `symlink-escape`. A PATH-global `oxlint`/`oxfmt`/`ruff`/`eslint`/`prettier`/`biome` is **not accepted** (D5).

Provider catalog (ids, categories, argv are the contract; `<scope>` = cwd, files from `ProjectScope.files`):

| id | cat | applies when | check argv | safe `--fix` argv |
| --- | --- | --- | --- | --- |
| `oxlint` | lint | JS/TS scope; oxlint configured/declared, **or** no other lint tool (bootstrap default) | `oxlint` (config auto-discovered; Rig never adds `--type-aware`/`--type-check`) | `oxlint --fix` (never `--fix-suggestions`/`--fix-dangerously`) |
| `oxfmt` | format | same, role format | `oxfmt --check` | `oxfmt` |
| `eslint` | lint | eslint config file/field present (declared-only without config => `not-applicable` `no-config-file`) | `eslint .` | `eslint --fix .` |
| `biome-lint` / `biome-format` | lint / format | biome config or dep | `biome lint .` / `biome format .` | `biome lint --write .` / `biome format --write .` (never `--unsafe`) |
| `prettier` | format | prettier config/field/dep | `prettier --check .` | `prettier --write .` |
| `tsc` | typecheck | `typescript` declared **and** `tsconfig.json` in scope; suppressed when covered (D9) | `tsc --noEmit -p tsconfig.json` | none |
| `ruff-check` / `ruff-format` | lint / format | Python scope; ruff configured/declared **or** no other tool (bootstrap default) | `ruff check` / `ruff format --check` | `ruff check --fix` (safe fixes only, never `--unsafe-fixes`) / `ruff format` |
| `flake8`, `pylint`, `black`, `mypy`, `pyright` | lint/lint/format/typecheck/typecheck | respective config or dep | `flake8` / `pylint <rel pkg dirs>` / `black --check .` / `mypy .` / `pyright` | `black .` only; others none |
| `shellcheck` / `shfmt` | lint / format | tracked `*.sh *.bash` or shebang `sh/bash/dash/ksh` (<=128-byte no-follow read) | `shellcheck -- <files>` / `shfmt -d -- <files>` (chunked) | none / `shfmt -w -- <files>` |
| `actionlint` | lint | `.github/workflows/*.y{,a}ml` at repo root | `actionlint -no-color -- <files>` | none |
| `cargo-clippy` / `cargo-fmt` | lint / format | `Cargo.toml` workspace root | `cargo clippy --workspace --all-targets [--locked if Cargo.lock] -- -D warnings` / `cargo fmt --all -- --check` | none / `cargo fmt --all` |
| `go-vet` / `gofmt` | lint / format | `go.mod` scope | `go vet ./...` / `gofmt -l -- <tracked .go files>` (`result_rule="stdout-empty"`) | none / `gofmt -w -- <files>` |
| `rig-check` | limits | repo root, always (once) | `[sys.executable, "-m", "riglib", "lint", "check", "--json", "-C", <root>]` (+`--config <file>` when given) | `... lint check --fix --json ...` |
| `script:<runner>:<name>` | scripts | D8 allow-list match | `[<pm>, "run", <name>]` / `["make","--no-print-directory","-C",<dir>,"--",<target>]` | none (a `lint:fix`-style script is never run) |

`rig-check` states: `cccc_install.pin_for()` raising `UnsupportedPlatform` => `unsupported-platform`; exit 0 => passed; 1 => failed `findings`; 3 => failed `incomplete`;
127 => `missing-tool` `analyzer-unavailable`; 2 => failed `config-error`; `lint_checks.enabled: false` => `not-applicable` `disabled-by-config`. The parsed child
`rig-lint-check/v1` document is embedded as `check.report`. Run via subprocess so `lint_check.py` stays untouched and its contract identical.

Bootstrap defaults: `oxlint` -> `PINS["oxlint"]`; `oxfmt` -> `PINS["oxfmt"]`; `ruff-*` -> `PINS["ruff"]`. Observed latest on 2026-10-27 (registry reads): `oxlint 1.90.0`,
`oxfmt 0.75.0`, `oxlint-tsgolint 7.0.2005` (oxlint peer `>=7.0.2005`), `@oxlint/plugins 1.90.0`, `ruff 0.17.3`. Implementers re-read the registries when writing `PINS`.
`oxlint-tsgolint` is added to a bootstrap need **only** when TypeScript major >= 7 is resolved; `@oxlint/plugins` only when anti-slop is required.

### 3.5 `riglib/lint_bootstrap.py` (I4)

```python
@dataclass(frozen=True)
class BootstrapStep:
    id: str                                   # "js-add-dev@<scope>" | "js-install@<scope>" | "py-add-dev@<scope>" | "write-config@<scope>:<rel>"
    kind: Literal["add-dev-packages", "install-dependencies", "write-config"]
    scope: str; manager: str
    argv: tuple[str, ...]                     # () for write-config
    cwd: str                                  # repo-relative
    packages: tuple[PinnedPackage, ...]
    lock: Literal["lockfile", "n/a"]          # n/a only for write-config; blocked otherwise
    writes: tuple[str, ...]                   # repo-relative files the step may create/modify (manifest, lockfile, config)

@dataclass(frozen=True)
class BootstrapBlocked:
    scope: str; tool: str
    reason: Literal["ambiguous-package-manager", "manager-not-installed", "no-manifest",
                    "no-lock-capable-manager", "disabled-by-config", "read-only-mode"]
    detail: str

@dataclass(frozen=True)
class BootstrapPlan:
    steps: tuple[BootstrapStep, ...]
    blocked: tuple[BootstrapBlocked, ...]
    install_plan: "InstallPlan"               # riglib.plan.InstallPlan, on_conflict="skip"
    notes: tuple[str, ...]
    def is_empty(self) -> bool: ...           # no steps -> idempotent no-op

@dataclass(frozen=True)
class BootstrapOutcome:
    results: tuple[tuple[str, Literal["applied", "noop", "failed", "blocked"], str], ...]   # (step id, state, detail)
    failed: bool

def select_js_manager(scope: ProjectScope, which: Callable[[str], str | None]) -> PackageManager | BootstrapBlocked   # D7
def manager_argv(manager: str, packages: Sequence[PinnedPackage], *, workspace_root: bool) -> tuple[str, ...]
def build_bootstrap_plan(needs: Sequence[BootstrapNeed], discovery: Discovery, cfg: LintConfig,
                         env: ToolEnv, *, catalog: "Catalog | None" = None) -> BootstrapPlan
def apply_bootstrap(plan: BootstrapPlan, *, root: Path, environ: Mapping[str, str]) -> BootstrapOutcome
    # refuses (BootstrapOutcome failed, nothing executed) when CI/GITHUB_ACTIONS truthy; uses actions.run_plan
```

`manager_argv` (exact; `-E`/exact is mandatory, scripts disabled, no audit noise):

| manager | add-dev argv | frozen install (when declared but not installed) |
| --- | --- | --- |
| npm | `npm install --save-dev --save-exact --ignore-scripts --no-audit --no-fund <name>@<ver>...` | `npm ci --ignore-scripts` |
| pnpm | `pnpm add --save-dev --save-exact --ignore-scripts [-w] <name>@<ver>...` (`-w` iff `pnpm-workspace.yaml`) | `pnpm install --frozen-lockfile --ignore-scripts` |
| yarn (classic) | `yarn add --dev --exact --ignore-scripts [-W] <name>@<ver>...` (`-W` iff `workspaces`) | `yarn install --frozen-lockfile --ignore-scripts` |
| yarn-berry | `yarn add --dev --exact --mode=skip-build <name>@<ver>...` | `yarn install --immutable --mode=skip-build` |
| bun | `bun add --dev --exact --ignore-scripts <name>@<ver>...` | `bun install --frozen-lockfile --ignore-scripts` |
| uv | `uv add --dev --no-sync ruff==<ver>` | `uv sync --frozen` |
| poetry | `poetry add --group dev ruff==<ver>` | `poetry install --no-interaction --sync` w/ existing lock |
| pdm | `pdm add --dev ruff==<ver>` | `pdm sync` |

Idempotence: `build_bootstrap_plan` derives needs from **current disk** (package declared in manifest => no step; declared but binary unresolved => only the frozen-install step;
declared and resolvable => nothing). Second run on a bootstrapped repo => `is_empty()`; no process is spawned. Config writes: only `linters.enabled` repos, only when **no**
oxlint config file exists (any existing `.oxlintrc.json{,c}`/`oxlint.config.{ts,mts}` is user-authoritative: no action, an explicit note; `install_plan.on_conflict="skip"` is the
second guard); flavor `oxlint.config.ts` iff `node_version >= (22,18,0)` else `.oxlintrc.json` (one flavor per dir, never both). Anti-slop bundle actions (and so `@oxlint/plugins`)
are planned only when a `Catalog` resolves; otherwise a note says anti-slop policy is deferred to `rig apply`. Python bootstrap writes no config (Ruff defaults are valid). `rig apply`
behavior is unchanged by this feature (bootstrap actions are planned only by `rig lint`).

New action kind (I4, `actions/runner.py`): `lint_bootstrap_step`, `category="lint_bootstrap"`, options `{step, argv, cwd, packages, writes, allow_mutation, expect_declared}`.
Handler: refuse if CI env truthy or `allow_mutation is not True`; resolve `cwd` under the repo (no symlink components, no escape); `subprocess.run(argv, cwd=..., shell=False,
stdin=DEVNULL, timeout=600)`; afterwards verify each package is declared in the manifest at the pinned version and the lockfile exists (else `error`); idempotent pre-check returns `skipped`.
Register in `_HANDLERS` and add `"lint_bootstrap_step"` to `action_tags.ACTION_TAGS` (category `edits_file`, audience human) -- `tests/test_action_tags.py` completeness pins this.
Writing the Rig-owned policy reuses the existing `provision_linter_config` (+ `provision_linter_bundle`) actions unchanged.

### 3.6 `riglib/lint_policy.py` (I4 edits)

```python
@dataclass(frozen=True)
class RuleSpec:  # + field
    needs_type_info: bool = False             # True for rules tsgolint executes (e.g. typescript/no-unsafe-type-assertion)
def render_oxlint_config(rules_config=None, *, flavor: Literal["ts", "json"] = "ts", type_aware: bool = False) -> str
    # type_aware False: no `options.typeAware`, rules with needs_type_info omitted (and summarized in a comment)
    # flavor "json": .oxlintrc.json with the same Rig managed-header comment; jsPlugins entry identical
def rule_activity(rules_config, *, type_aware: bool) -> dict[str, tuple[bool, str]]   # name -> (active, reason) for `rig lint rules`
```
`plan.py::_build_linters` passes `type_aware=env.type_aware`, calls `inspect_linter_environment(repo_root, anti_slop=anti_slop_required(severities))`, and replaces the `@oxlint/plugins` blocked message
with the env-derived one. `rule_policy_summary` stays (counts of configured severities).

### 3.7 `riglib/lint_run.py` + `riglib/lint_run_cli.py` (I3)

```python
SCHEMA = "rig-lint-run/v1"
EXIT_OK, EXIT_FINDINGS, EXIT_USAGE, EXIT_INCOMPLETE, EXIT_MISSING = 0, 1, 2, 3, 127
INCOMPLETE_REASONS = frozenset({"timeout", "crash", "malformed-output", "output-limit", "malformed-manifest",
                                "incomplete", "bootstrap-failed", "unreadable"})

@dataclass(frozen=True)
class CommandSpec:
    argv: tuple[str, ...]; cwd: Path; timeout_s: int; env: Mapping[str, str]; max_capture_bytes: int
@dataclass(frozen=True)
class CommandOutcome:
    exit_code: int | None; stdout: str; stderr: str; duration_ms: int
    timed_out: bool; truncated: bool; spawn_error: str | None
CommandRunner = Callable[[CommandSpec], CommandOutcome]
def subprocess_runner(spec: CommandSpec) -> CommandOutcome
    # shell=False, stdin=DEVNULL, start_new_session=True, killpg on timeout, bounded capture (default 4 MiB/stream, doc excerpt 32 KiB head+tail)

@dataclass(frozen=True)
class RunOptions:
    cwd: str; config: str | None; json: bool; plan: bool; no_setup: bool; fix: bool
    ci: bool | None                           # None => auto: CI or GITHUB_ACTIONS env truthy
    only: tuple[str, ...]; skip: tuple[str, ...]; scopes: tuple[str, ...]; timeout: int | None

@dataclass(frozen=True)
class CheckResult:                            # one row of "checks"
    check: PlannedCheck; state: CheckState | Literal["planned"]; reason: str; detail: str
    exit_code: int | None; duration_ms: int | None; counts: Mapping[str, int] | None
    stdout: str; stderr: str; truncated: bool
    fix: Mapping[str, object] | None; report: Mapping[str, object] | None
    def as_dict(self) -> dict[str, object]: ...

@dataclass(frozen=True)
class RunReport:
    exit_code: int; doc: dict[str, object]; text: str

def run_checks(checks: Sequence[PlannedCheck], *, root: Path, default_timeout_s: int, fix: bool,
               environ: Mapping[str, str], runner: CommandRunner = subprocess_runner) -> tuple[CheckResult, ...]
def exit_code_for(results: Sequence[CheckResult], *, bootstrap_failed: bool, problems: Sequence[DiscoveryProblem]) -> int
def execute(opts: RunOptions, *, environ: Mapping[str, str] | None = None, runner: CommandRunner = subprocess_runner,
            which: Callable[[str], str | None] | None = None) -> RunReport
def render_text(doc: Mapping[str, object]) -> str
# lint_run_cli.py
def add_run_arguments(parser: argparse.ArgumentParser, *, suppress_defaults: bool) -> None
def run(args: argparse.Namespace) -> int      # Namespace -> RunOptions, emit text/JSON, return report.exit_code; never raises
```

Flow of `execute`: load config (`lint_run_config.load_config`) -> `discover` -> `providers.bootstrap_needs` -> bootstrap plan (always computed; applied only when
`not ci and not no_setup and not plan and cfg.bootstrap.enabled and cfg.enabled`) -> **re-discover** after an applied bootstrap -> `providers.plan_checks` -> filters
(`--only/--skip/--scope`, `lint.enabled: false` => one `not-applicable` `disabled-by-config`) -> `run_checks` (skipped under `--plan`) -> doc + exit.
Discovery `problems` of malformed/unreadable kinds add a synthetic check `discovery@<path>`: `failed`, reason `malformed-manifest`.
Child env: inherited + `RIG_LINT_DEPTH=<depth+1>`, `NO_COLOR=1`. `--fix`: run each check's `fix_argv` instead of `argv` (checks without one run in check mode with `fix.mode="unsupported"`),
then **re-run the check argv** and report the post-fix verdict; `fix.changed_files` comes from a `git status --porcelain -z` diff before/after, and files whose index
entry differs are listed in `fix.needs_restage` (never restaged). `--fix` with `--plan`/`--ci` is exit 2. `--fix` is refused (exit 2, nothing written) when any file the run
may rewrite is partially staged (`lint_check_scope.partially_staged`). Exit 2 also: not a git repo, bad `--only/--skip` category, `--scope` outside the repo, config error inside `lint`.

CLI (`cli.py`):

```
rig lint [run] [-C DIR] [--config FILE] [--json] [--plan] [--no-setup] [--fix] [--ci]
         [--only CAT[,CAT]] [--skip CAT[,CAT]] [--scope DIR]... [--timeout SEC]
rig lint check ...    # unchanged
rig lint rules ...    # unchanged (+ `active`, `inactive_reason` fields/columns for type-aware rules)
```
Bare `rig lint` (no action) now runs `run` (previously printed help, exit 0 -- the single intended behavior change; `rig lint --help` still lists `run`, `check`, `rules`).
Run options are defined once and added to both the `lint` parser and the `run` subparser; the **subparser copy uses `default=argparse.SUPPRESS`** so `rig lint --json run`,
`rig lint run --json`, `rig lint --json` and `rig lint -C dir rules` all keep their values (argparse otherwise overwrites parent values with subparser defaults). `rules` and `check`
parsers are not touched.

### 3.8 JSON document `rig-lint-run/v1`

```jsonc
{
  "schema": "rig-lint-run/v1",
  "ok": false,                       // exit_code == 0; null under --plan
  "exit_code": 1,
  "root": "/abs/repo",
  "mode": {"ci": false, "plan": false, "fix": false,
           "setup": "applied|skipped-ci|skipped-no-setup|skipped-plan|disabled-by-config|nothing-to-do|blocked|failed"},
  "config": {"enabled": true, "timeout_seconds": 300, "categories": {"lint": true, "typecheck": true, "format": true, "limits": true, "scripts": true},
             "bootstrap": {"enabled": true, "javascript": true, "python": true}, "providers": {"eslint": {"enabled": true}}},
  "discovery": {
    "scopes": [{"path": ".", "languages": ["typescript"],
                "package_manager": {"name": "pnpm", "source": "lockfile", "lockfile": "pnpm-lock.yaml", "root": "."},
                "tools": [{"tool": "eslint", "role": "lint", "declared": true, "config_files": ["eslint.config.js"]}],
                "scripts": ["lint"]}],
    "unknown_languages": [{"language": "kotlin", "files": 12, "examples": ["app/Main.kt"]}],
    "unsupported_tools": [{"scope": ".", "tool": "xo"}],
    "problems": [{"path": "web/package.json", "kind": "malformed-manifest", "detail": "..."}],
    "skipped_symlinks": []
  },
  "bootstrap": {"steps": [{"id": "js-add-dev@.", "kind": "add-dev-packages", "manager": "pnpm", "argv": ["pnpm", "add", "..."], "writes": ["package.json", "pnpm-lock.yaml"]}],
                "blocked": [{"scope": ".", "tool": "oxlint", "reason": "ambiguous-package-manager", "detail": "..."}],
                "results": [{"id": "js-add-dev@.", "state": "applied", "detail": "..."}], "notes": []},
  "checks": [{
    "id": "oxlint@.", "provider": "oxlint", "category": "lint", "scope": ".", "tool": "oxlint",
    "state": "passed|failed|missing-tool|not-applicable|unsupported-platform|planned",
    "reason": "ok|findings|timeout|crash|malformed-output|output-limit|incomplete|config-error|tool-not-installed|bootstrap-blocked|not-bootstrapped|symlink-escape|analyzer-unavailable|covered-by:<id>|disabled-by-config|filtered|recursion-guard|self-referential-script|writes-files|no-config-file|no-matching-files|platform-unsupported",
    "detail": "human sentence incl. remediation",
    "argv": ["..."], "cwd": ".", "exit_code": 1, "duration_ms": 1234, "timeout_s": 300,
    "tool": {"resolution": "local-bin", "path": "node_modules/.bin/oxlint"},
    "counts": {"errors": 3, "warnings": 1},            // null when not parseable; never invented
    "output": {"stdout": "...", "stderr": "...", "truncated": false},
    "fix": {"requested": true, "mode": "safe|unsupported", "changed_files": [], "needs_restage": []},   // null when --fix not passed
    "report": null                                      // rig-check embeds the rig-lint-check/v1 document
  }],
  "summary": {"total": 7, "applicable": 5, "passed": 3, "failed": 1, "missing_tool": 1, "not_applicable": 2, "unsupported_platform": 0, "duration_ms": 4321},
  "error": "present only on exit 2"
}
```
`applicable` = total minus `not-applicable`. On exit 2 the document is `{schema, ok:false, exit_code:2, error, fix:{requested}}` (same shape rule as `rig-lint-check/v1`). `--json` stdout is JSON only; warnings go to stderr.

### 3.9 Exit codes (consistent with `docs/lint-check.md`)

| Exit | Meaning |
| --- | --- |
| 0 | every applicable check passed (or none applicable: stated, with the unknown-language list); `--plan` always 0 unless 2 |
| 1 | at least one check `failed` with reason `findings` |
| 2 | usage/config error: not a git repo, malformed `lint` block, bad `--only/--skip/--scope/--timeout`, `--fix` with `--plan`/`--ci`/partially staged files, or the nested `rig lint check` exits 2 (reason `config-error`) |
| 3 | analysis incomplete: a check `failed` with reason in `INCOMPLETE_REASONS` (timeout, crash, malformed output, malformed manifest) or a bootstrap step failed |
| 127 | an applicable check could not run: `missing-tool` or `unsupported-platform` (the check list names the remediation; `rig lint` without `--no-setup` locally bootstraps JS/TS and Python tooling first) |

Priority `2 > 127 > 3 > 1 > 0`. `not-applicable` never changes the exit code and is never counted as a pass.

### 3.10 Config keys (I1: `lint_run_config.py`, `config_schema.py`, `schema.py`, docs)

```yaml
lint:                         # global ~/.config/rig/config.yaml and ./rig.yaml; repo wins per key (deep merge)
  enabled: true               # master switch for `rig lint run` (not for `rig lint check` / `rig lint rules`)
  timeout_seconds: 300        # per check, integer 1..3600
  categories: {lint: true, typecheck: true, format: true, limits: true, scripts: true}
  bootstrap:
    enabled: true             # may bare local `rig lint` install missing tooling
    javascript: true          # Oxlint + Oxfmt
    python: true              # Ruff
    versions: {}              # exact-version overrides keyed by PINS key, e.g. {ruff: "0.17.3"}; ^\d+\.\d+\.\d+([-+][0-9A-Za-z.-]+)?$
  providers:                  # open map keyed by provider id (validated against lint_providers.PROVIDER_IDS)
    eslint: {enabled: true, timeout_seconds: 600}
```
Unknown keys/types, unknown provider ids, non-exact versions, out-of-range timeouts are `ConfigError` naming the key and layer (message style of `lint_checks_config.py`).
`schema.py` gets an `Area("lint", ...)` with `lint.enabled`, `lint.timeout_seconds`, `lint.categories.*`, `lint.bootstrap.enabled|javascript|python` (wizard options); `areas.py` gets **no** status area (config-only; nothing reconciled -- document that in the `lint` Area blurb). `lint` is valid in both layers (not in `_GLOBAL_LAYER_ONLY_BLOCKS`, not in `_GLOBAL_ONLY_CATEGORIES`).
`schema/rig.schema.json` is regenerated from `config_schema.json_schema()` (sync test `tests/test_config_schema.py`).

## 4. Test plan (every file focused; run one pytest file per invocation)

Shared fixture style: `tmp_path` git repos (`git init -q`), fake tool shims = tiny executables in a tmp `bin/` placed first on a controlled PATH (record argv to a log, optionally edit
`package.json`/write a lockfile). No test touches the network, the real HOME, or a real npm/pnpm/uv.

| File | Required cases |
| --- | --- |
| `tests/test_lint_discovery.py` (I1) | **mixed/nested repo** (pnpm workspace with two packages + python uv project + Rust workspace with member + Go module + shell scripts + workflows); **manager selection** table (packageManager field wins over lockfile; each single lockfile; yarn classic vs berry; two lockfiles => ambiguous; pnpm-workspace marker; none => npm default; uv/poetry/pdm/pip); **existing configs preserved** detection (eslint flat, `.eslintrc.json`, `eslintConfig` field, biome.jsonc, prettier field, `[tool.ruff]`, `ruff.toml`, `.oxlintrc.jsonc`, `oxlint.config.ts`); **malformed configs** (broken package.json / pyproject / JSONC => `problems`, no exception, scope still produced); **no JS exec** (PATH shim `node`/`bun` that logs; `eslint.config.js` with side-effect text; assert shim never invoked and file never imported); `typeCheck: true` regex fact; unknown languages reported with counts, `.md/.json` excluded; script allow-list + deny (`lint:fix`, `--write`), `rig lint` self-reference flag; **paths with spaces/unicode/leading dash**; **symlink escape** (symlinked `package.json` to a file outside, symlinked dir) not read; gitignored `node_modules` excluded; non-git dir => `ScopeError`; ancestors/nearest scope helper; `toml_parser=False` fallback. |
| `tests/test_linter_environment.py` (I1, edit) | plugins required only with `anti_slop=True`; type-aware gating (TS 5 declared, TS 7 + tsgolint, tsgolint without TS7, TS missing); existing oxlint config files reported; foreign-name labels unchanged; prompt matches `required_oxc_packages`. |
| `tests/test_lint_run_config.py` (I1) | defaults; global + project cascade (repo wins, deep merge, per key); each invalid key/type/range; unknown provider id; non-exact version; leniency (unrelated-block error = warning, `lint` error = fatal); `lint_checks_enabled`/`linters_rules` plumbing; schema/setup-wizard pinned-set sync (via the edited existing tests). |
| `tests/test_lint_providers.py` (I2) | registry: unique ids, `PROVIDER_IDS` complete, deterministic order, duplicate register fails; **argv tables** (parametrized per provider, exact argv, scope cwd, no `--type-aware`/`--type-check`/`--unsafe*`); **configured tools authoritative** (eslint configured => no `oxlint` check and no bootstrap need; ruff configured => `black` absent => no extra need; foreign tool without provider => `unsupported_tools`, still no bootstrap); **overlap** (tsc vs `typecheck` script vs oxlint `typeCheck` => exactly one runs, others `covered-by:`); bootstrap needs (empty JS project => oxlint+oxfmt; Python => ruff pair; declared-but-uninstalled => no new need, check `missing-tool` `tool-not-installed`; tsgolint only with TS>=7); **tool resolution** (local-bin up-walk stops at repo root, `.venv/bin`, manager-run, PATH accepted only for system tools, PATH-global oxlint rejected, symlink escape => `escaped`); **missing tool**; **unsupported platform** (fake provider with `platforms={"plan9"}`; `rig-check` with `UnsupportedPlatform`); **recursion guard** (`depth>=1` skips scripts; `lint: rig lint check` flagged; `lint:fix`/`--write` => `writes-files`); scripts are argv with names containing spaces/`;`/`$()`/leading `-` rejected by allow-list and never interpolated; `chunk_files` bounds + `./` prefix; per-provider/category `enabled: false`; timeouts override; determinism (same input => identical tuple). |
| `tests/test_lint_bootstrap.py` (I4) | `manager_argv` exact table (all managers, workspace flags, berry vs classic); `select_js_manager` (field, single lock, ambiguous => blocked, manager missing on PATH => blocked, never falls back); **empty project** (`package.json` only): first run installs via shim (declaration + lock written), second run `is_empty()` and spawns nothing; Python uv/poetry/pdm argv, pip-only blocked with remediation; no `package.json`/`pyproject.toml` => `no-manifest`, nothing created; **existing configs preserved** (ESLint repo: no step; user `.oxlintrc.json` untouched, `on_conflict="skip"` proven by byte-compare; foreign formatter => no Oxfmt step); **read-only guarantees**: `--plan`/`--no-setup`/CI plan non-empty but `apply_bootstrap` runs nothing and bytes of manifests/locks/configs are identical (tree hash before/after); handler refuses under `CI=1` and without `allow_mutation`; handler error paths (non-zero exit => `error`, declared-version mismatch => `error`); cwd with spaces; symlinked `package.json` refused; Node boundary 22.17 vs 22.18 => json vs ts flavor; `@oxlint/plugins` only with anti-slop; handler registered in `_HANDLERS`; `ACTION_TAGS` completeness. |
| `tests/test_lint_policy.py` (I4, edit) | type-aware off by default (no `typeAware`, type-info rules omitted), on with TS7+tsgolint; json flavor valid JSONC + header; `rule_activity` reasons. |
| `tests/test_lint_run.py` (I3) | fake `CommandRunner`: **failure aggregation** (first check fails, all later checks still run, exit 1, every row has reason/exit/timing); each **state -> exit** mapping and **priority** (2>127>3>1>0 combinations); zero applicable => exit 0 + unknown languages in text/JSON; **timeout** (real `sleep` subprocess, 1 s limit => `failed` `timeout`, exit 3, process group killed); output bounding/truncation; `gofmt -l` `stdout-empty` rule; **JSON output** (schema keys/types validated by a small checker; `--json` stdout is parseable; embedded `rig-lint-check/v1` for `rig-check`); **`--fix` safe modes only** (only `fix_argv` executed, never `--unsafe*`/`--fix-dangerously`; unsupported providers run in check mode with `fix.mode=unsupported`; post-fix re-run verdict; `needs_restage` for staged files; refused with `--ci`/`--plan`/partial staging); **no writes during `--plan` and CI** (tree hash identical, runner never called for bootstrap, `setup` label correct, `CI=1` and `GITHUB_ACTIONS=true` both honored, `--ci` flag); bootstrap applied locally then re-discovery picks the new tool (shim) and the check runs; **recursion guard** (`RIG_LINT_DEPTH` incremented in child env; nested run skips scripts); **paths with spaces** (`-C "dir with space"`, scope names) and **symlink escape** (tool symlink to outside => `missing-tool symlink-escape`); `discovery` malformed manifest => synthetic failed check, exit 3; `lint.enabled: false`; `--only/--skip/--scope` filters become `not-applicable filtered`. |
| `tests/test_lint_run_cli.py` (I3) | **CLI backward compat**: `rig lint check` and `rig lint rules` argv/exit codes unchanged (smoke against fixtures already used by `tests/test_lint_check_*`/`test_lint_cli_and_formatter.py`, not re-testing CCCC); `rig lint --help` lists `run check rules`; bare `rig lint` == `rig lint run`; flag order permutations (`--json run`, `run --json`, `-C d rules`); invalid `--only` => exit 2 JSON error doc; not a git repo => 2; end-to-end tiny repo with shims on PATH producing a mixed passed/failed/missing-tool doc; `rig lint rules` shows `active`/`inactive_reason`. |

Verification commands for `Main` after integration (one file at a time, no full suite, no heavy builds):
`python3 -m pytest -q tests/test_lint_discovery.py`, then `...test_linter_environment.py`, `...test_lint_run_config.py`, `...test_lint_providers.py`, `...test_lint_policy.py`,
`...test_lint_bootstrap.py`, `...test_action_tags.py`, `...test_lint_run.py`, `...test_lint_run_cli.py`, `...test_config_schema.py`, `...test_setup_wizard.py`, plus the existing
`...test_lint_cli_and_formatter.py` and `...test_linter_carriers.py`. CI runs the full suite.

## 5. Docs and release bookkeeping (I1)

- `docs/lint.md`: what `rig lint` runs, the state model, exit table, JSON example, bootstrap rules (D4-D7), configured-tool authority, supported-provider table (generated from the Section 3.4 table), explicit "not covered" list and the sentence that Rig does not claim exhaustive support, `--fix` safe-mode table, config keys.
- `docs/config-schema.md`: new `## lint` section next to `## lint_checks`; `docs/lint-check.md`: one "see also `rig lint` aggregates this check" line, contract text untouched.
- `README.md` command table: `rig lint` row; `CHANGELOG.md`: `## Unreleased` -> `### Added` (`rig lint run`, providers, bootstrap, config block), `### Changed` (bare `rig lint` runs checks; Oxlint config no longer forces `typeAware`; `@oxlint/plugins` only with anti-slop). Version bump per the `bump-version-on-release` skill is a release step, not part of these commits.
- Comments follow `comment-hygiene` (why, not what changed); commits are atomic conventional commits ending `(Refs #781)`.

## 6. Acceptance mapping (ticket -> where it lands)

| Requirement | Owner / place |
| --- | --- |
| central extensible registry | I2 `lint_providers.register` (one `ProviderSpec` per provider; adding a provider touches this file + its test row only) |
| discover languages/manifests/configs, nested packages | I1 `lint_discovery` |
| preserve configured tools | D3; I2 `plan` per provider; I4 `build_bootstrap_plan` |
| lint / typecheck / format-check / CCCC+file-lines / scripts / shell / workflows / Rust / Go | I2 catalog (3.4) |
| recursion guard, argv-only, bounded timeout | D8; I2 script rules; I3 runner |
| distinct states, never PASS for unavailable, all checks run, aggregate nonzero, per-check reason/exit/timing, `--json` | D1, D2, D13; I3 |
| unknown languages reported | I1 `Discovery.unknown`; I3 text + JSON |
| bootstrap JS->Oxlint+Oxfmt, Python->Ruff via manager + locks through plan/actions; local-only; `--plan`/`--no-setup`/CI read-only | D4-D7; I4 |
| `--fix` safe modes only | 3.4 table; I3 |
| plain detection executes no repo JS | D11; I1 |
| config global+project, schema/help/docs coherent | 3.10; I1 (+ I3 help text) |
| no per-file exemptions / relaxed thresholds | Section 0; no `exclude`/threshold keys exist in 3.10 |
| `linter_environment.py` obsolete `@oxlint/plugins` assumptions | Section 1 list; I1 + I4 |
| Oxc type-aware: no forced TS7 / `--type-check` | D10; I2 argv table; I4 policy |

## Coordinator acceptance clarification (2026-10-27 19:49 Europe/Amsterdam)

This supplements the original user's requirement to run ALL applicable checks; it does not change the public dataclass contracts above. Independent acceptance review is being recorded in `/home/ultra/.local/state/rig-cccc-resume-20261027/universal-acceptance-review.md`.

- Deduplicate an invocation only when its actual provider AND file scope are covered. Sharing a language/category is not proof: configured Oxlint and ESLint can have complementary rules, two lint scripts can cover different directories, and a script called `lint` may not lint every language. Preserve those checks; test a mixed-language repo and two distinct configured linters.
- Source-only files must not disappear because no language manifest exists. At minimum expose the detected language and actionable missing setup; a Python script inside a JS repo is still applicable Python. The user requested automatic setup where no linters exist, so a reasonable reproducible managed tooling path should cover this instead of a silent successful scan.
- Project characteristics include framework/file formats: a frontend with React/TSX or Vue should not be treated as an empty generic TS project when bootstrapping. Use documented compatible plugins/defaults; never rewrite an existing project's own config or force a TypeScript major upgrade.
- DELIVERY prerequisite observed with the LIVE installed protected CCCC hook: `rig lint check --json riglib/cli.py riglib/config_schema.py riglib/schema.py riglib/plan.py riglib/actions/runner.py` on main a71eb0a reports 110 errors and 1 warning. File sizes: cli4434, plan4014, runner10467, config_schema2135. The hook checks whole staged files, not only added lines. Any touched legacy module must be made compliant through real decomposition/refactoring or an already-supported extension seam; do NOT disable the gate, add exemptions, raise limits, or use a parallel executor. Anticipate this before the final commit.
- Final acceptance uses the installed CLI after normal main merge, real fresh JS/TS and Python setup, preserved existing Biome plans for both bots, and exact full CI. A plan, isolated new-file tests, or only the CCCC component passing does not prove the aggregate command works.

### Independent acceptance review is available (19:53)

Read `/home/ultra/.local/state/rig-cccc-resume-20261027/universal-acceptance-review.md` before final integration. It is a bounded PLAN review, not a runtime proof. Verify findings against the current implementation and correct overstatements: Prettier/Black can run with defaults without a config, and a normal managed executable resolving into an external package store is not the same as a source-write escape. Do not blindly apply every suggested mechanism.

Load-bearing cases to pin: (a) declared ESLint with no usable config must not suppress bootstrap then become a green `not-applicable`; retain the selected tool and report its configuration failure or provide the legitimate missing setup; (b) `format:check: "oxfmt ."` actually WRITES despite lacking `--write`, so it must not be run as a read-only formatter check; bare `ruff format` and `cargo fmt` need the same treatment; (c) complementary configured tools and scoped scripts remain separate unless their actual execution is proven redundant; (d) manifest-less Python within a JS repo must remain visible and lintable, with a pinned managed tooling environment as a reasonable fallback for pip-only/no-manifest projects; (e) uncovered languages must be explicit incomplete coverage, not an unconditional clean headline. Keep trusted normal project scripts usable, and do not mistake a linter's ignored cache creation for rewriting source.

The #775 large-file commits predate activation of the real global CCCC hook; the hook was installed AFTER #775 merged. This explains why those old commits were possible and is not an exemption for #781. The existing hook is not diff-aware.

### Full-suite execution capacity (coordinator verified 2026-10-27 19:56)

Do not let shared-server root-space pressure become a stopping point. The installed `runpod-run --dry-run -- python3 -m pytest -q` successfully verified an available CPU offload route without creating a pod. Use the installed runpod-offload skill's normal live budget check, source-sync safety controls and automatic termination rather than persisting owner-specific billing details in this repository. Actual runs need the real rig CI prerequisites (tmux, lsof, abduco, Bun, pinned CCCC and TypeScript) and isolated HOME/TMPDIR, not the bare example alone. Never run the full suite on the shared workstation. Hosted Forgejo CI remains the authoritative full matrix.

Capacity helper report `/home/ultra/.local/state/rig-cccc-resume-20261027/ci-capacity-result.md` confirms 14 orphan test processes stopped, NO fixture deletions (guard refused them), and ~2.9 GB root / 5.4 GB RAM filesystem free on shared server. Do not bypass that removal guard or use its mistaken suggestion to run the full suite locally.

## Contract amendments (append only)

- I2 / `lint_providers`: `plan_checks(..., *, config_path: str | None = None)` and `PlanContext.config_path` (default `None`) so `rig-check` forwards `--config`; I3 passes `opts.config`.
- I2: `ToolEnv.system/machine` use the `cccc_install.platform_key()` vocabulary (rig-check calls `pin_for((system, machine))`).
- I2: a file-list check with more than one argv chunk is emitted as `<provider>@<scope>`, `<provider>@<scope>#2`, ... (same provider/category; runner treats each as its own check).
- I2: new `Unavailable.reason` values: `ambiguous-package-manager` (state `missing-tool`, project script with conflicting lockfiles), `no-matching-files` (pylint without package dirs). `PlannedCheck` trailing fields `timeout_s`, `fix_argv`, `result_rule`, `resolution` have defaults (`None`, `None`, `"exit-zero"`, `None`).
- I2: `bootstrap_needs` returns needs even when `lint.bootstrap.*` disables them (I4 reports `disabled-by-config`); the matching default check is then `not-applicable` `disabled-by-config`. Needs are per role and land on the topmost unconfigured scope of the ecosystem.
- I2: Oxlint `options.typeCheck` only covers the typecheck capability when TypeScript >= 7 and `oxlint-tsgolint` is a configured/declared tool of the scope chain (discovery must report it as tool `oxlint-tsgolint` or `tsgolint`).
