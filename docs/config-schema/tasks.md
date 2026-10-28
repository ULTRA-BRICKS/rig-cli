[← Back to the config schema index](../config-schema.md)

# `rig.yaml` schema — Task routing

Reference part of [config-schema.md](../config-schema.md): the `task` block: tracker, routing, and workspace task routing (UB-23).

## `task`

Two unrelated concerns share this one top-level block: **`code_prefix`**, which `rig apply`
DOES materialize to disk, and **task-cli's ticket-tracker routing** (`backend`/`team`/`project`/
`repo`/`attachment_mode`, plus nested `github:`/`linear:`), which rig only validates — it writes
nothing to disk for those keys.

### `code_prefix`

A repo-unique **task-code prefix**, consumed by `ci/ship/ship.sh`'s review-quorum gate. That
gate only recognizes `HYP-123`/`XX-123` style task codes out of the box — a repo whose task-cli
backend is GitHub Issues (bare `#NNN`, no ticket-code convention of its own) can never derive a
task code and is permanently refused by the gate. Setting `code_prefix` fixes that: `rig apply`
writes it into the repo's `.ship-config` as `SHIP_TASK_CODE_PREFIX=<PREFIX>`, and `ship.sh`
synthesizes `<PREFIX>-NNN` from a bare `#NNN` found in the branch name or PR body — **never**
the bare `#NNN` itself.

```yaml
task:
  code_prefix: RIG   # 1-40 uppercase letters/digits; unset = feature off
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `code_prefix` | string | *(unset)* | the repo-unique prefix spliced in front of a bare issue number (`<PREFIX>-NNN`); must be 1-40 uppercase letters/digits |

**Why a prefix, not the bare number.** review-cli's review-quorum store is a single **global**
file keyed only by the task-code string, with no per-repo scoping. Two different repos both
deriving bare `#346` (e.g. `agent-tools#346` and `rig-cli#346`) would silently share — and
falsely satisfy — each other's review-iteration count. The prefix is what keeps the synthesized
code collision-free per repo.

**Merge, not overwrite.** `rig apply` merges the `SHIP_TASK_CODE_PREFIX=` line into any existing
`.ship-config` content — a hand-committed `SHIP_LOCAL_TEST_DIR`/`SHIP_LOCAL_TEST_CMD` override is
never clobbered.

**Fail-closed on a malformed prefix.** `ship.sh` validates `SHIP_TASK_CODE_PREFIX` itself at
read time (1-40 uppercase letters/digits) and ignores the **whole** `.ship-config` file if it
doesn't match — same policy as an unsafe `SHIP_LOCAL_TEST_DIR`. `HYP-`/`XX-` style codes are
unaffected either way and still take precedence when present.

**One-way drift (a known limit, same as `linters` above).** Removing `code_prefix` from
`rig.yaml` does not remove the `SHIP_TASK_CODE_PREFIX=` line it previously wrote — `rig apply`
emits no action for an unset prefix, so the stale line is never revisited, and `rig status`
reports clean. Delete the line from `.ship-config` by hand if you turn the feature off.

### Ticket-tracker routing

**task-cli's per-repo ticket-tracker routing** (CTO decision #4136.4: hyperide → Linear/HYP,
every other repo → GitHub Issues by default). Unlike `code_prefix` above, rig itself **writes
nothing to disk** for these keys — task-cli reads this block **directly** at runtime
(`tasklib.config.rig_task_overlay`, in the [task-cli
repo](https://github.com/alex-mextner/task-cli)), with LOWER precedence than a repo's own
native `task.yaml`. A repo that already keeps a `task.yaml` is unaffected by anything set here.
rig only validates the key set + enum values so a typo fails fast at `rig status`/`rig apply`
time, with rig's own error format, instead of only surfacing later as a task-cli error.

```yaml
task:
  backend: linear          # github-issues (default) | linear
  team: HYP                # flat shorthand for linear.team
  attachment_mode: link    # link (never re-host a local file) | native (default; real upload)
  # equivalently, nested form (both may be combined; task-cli rejects a genuine collision):
  # linear:
  #   team: HYP
  #   project: ""           # optional Linear project id to scope create/list/search to
  #   attachment_mode: link
  # github:
  #   repo: owner/name       # or 'auto' to detect from the git remote
```

All keys below (flat and nested, scalar and container) tolerate an explicit `null` the same as
absent — rig's `_validate_task` guards every one of them with `is not None`, an early `None`
return (the three enum leaves: `backend`, `attachment_mode`, `linear.attachment_mode`), or a
null-specific `{} if x is None else x` normalization (the two nested sections; not a plain
`or {}`, which would also swallow other falsy-but-invalid values), matching task-cli's own
tolerant reads. The one exception is the top-level `task:` header itself, which stays rejected
(matches every other
block's convention).

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `backend` | str or null | `github-issues` | the tracker backend: `github-issues` or `linear` |
| `team` | str or null | — | flat shorthand for `linear.team` (e.g. `HYP`) |
| `project` | str or null | — | flat shorthand for `linear.project` |
| `repo` | str or null | — | flat shorthand for `github.repo` (`owner/name`, or `auto`) |
| `attachment_mode` | str or null | `native` (task-cli's own default) | how a screenshot/file gets attached to a Linear ticket — see below |
| `github.repo` | str or null | — | nested form of the `repo` shorthand |
| `linear.team` | str or null | — | nested form of the `team` shorthand |
| `linear.project` | str or null | — | nested form of the `project` shorthand |
| `linear.attachment_mode` | str or null | — | nested form of the `attachment_mode` shorthand |
| `github`, `linear` | object or null | — | nested sections; an explicit `null` (a bare `github:`/`linear:` header) is tolerated the same as absent |
| `projects` | array or null | — | task-cli's cross-project registry (a LIST of entries, not a mapping — see task-cli's `tasklib/projects.py`); rig only checks it's a list; an explicit `null` is tolerated the same as absent |
| `enforce`, `classify`, `session` | object or null | — | task-cli's own nested shapes (each with their own defaults); rig only checks they're mappings — task-cli's own `tasklib.config.validate` is the runtime authority on their contents; an explicit `null` is tolerated the same as absent |

**`attachment_mode` — `link` vs `native` (tg#11652).** How task-cli attaches a screenshot/file
to a Linear ticket:
- **`native`** (task-cli's own default): uploads the bytes through Linear's authenticated
  `fileUpload` + signed-URL PUT + `attachmentCreate` API sequence, so the ticket carries a real
  Linear attachment object with an inline preview.
- **`link`**: never uploads a local file — only registers an already-hosted URL (e.g. a GitHub
  PR asset, or a PR's `#screenshots` anchor) as an external-link attachment. hyperide pins this
  mode (HYP-1248): a raw pasted-image `uploads.linear.app` URL is session-scoped and 401s for a
  non-browser viewer, so hyperide's mitigation is to link out to the PR instead of embedding an
  image. A bare local path in this mode has nothing to link to and the attach fails cleanly
  (best-effort — the local ref stays visible in the ticket body regardless).

---

## `task` — Workspace task routing (UB-23)

Additions on top of the existing `task` block above (`backend`/`team`/`project`/`repo`/
`attachment_mode`, nested `github:`/`linear:`, `code_prefix`):

- **`backend: tasks`** — a third tracker backend value (alongside `github-issues` and
  `linear`): the native Tasks-app (HTTPS REST).
- **`task.tasks` block** — coordinates for the `tasks` backend, used when
  `backend: tasks`:
  - `task.tasks.url` — the Tasks-app base URL, e.g. `https://tasks-ultrabricks.vercel.app`.
    It must be a bare `https` origin — no userinfo, query, fragment or path (task-cli appends
    `/api/v1` itself and sends its bearer token there) — the same rule task-cli's tasks
    backend enforces. A rejected value is never echoed back.
  - `task.tasks.team` — the Tasks-app team/workspace key, e.g. `UB`, `HYP`.
  - `task.tasks.attachment_mode` — `native` (upload through the Tasks-app API) or `link`
    (register an already-hosted URL as-is), mirroring `linear.attachment_mode`.

All three are also registered as wizard options (`riglib/schema.py`'s `task` Area,
alongside the existing `task.code_prefix` entry) with `rig setup`/`rig config set`.

```yaml
task:
  backend: tasks
  tasks:
    url: https://tasks-ultrabricks.vercel.app
    team: UB
    attachment_mode: native
```

### Ancestor `task`-block inheritance

A folder default (e.g. `~/xp/rig.yaml`, `~/work/rig.yaml`) can declare a `task:` block
that every repo nested underneath inherits, so routing config doesn't need repeating per
repo:

```yaml
# ~/xp/rig.yaml — a folder default for every repo under ~/xp
task:
  backend: tasks
  tasks:
    url: https://tasks-ultrabricks.vercel.app
    team: UB
```

```yaml
# ~/xp/some-project/rig.yaml — inherits the backend/url/team above automatically;
# only overrides what differs for this one repo
task:
  team: HYP
```

Precedence: `global (~/.config/rig/config.yaml) < ancestor task blocks (outermost
first, nearest ancestor last) < repo rig.yaml`, merged per-KEY (not whole-block
replacement) — an ancestor overriding `team` while leaving `backend` unset still lets a
global `backend` value survive. Only the `task` key is ever read from an ancestor's
`rig.yaml`; no other section (harness, permissions, hooks, CI, mode) leaks from an
ancestor into a descendant repo. `code_prefix` is repo-unique ship metadata, not
routing — it is stripped from every ancestor's contribution before merging and is never
itself inherited.

Ancestor resolution excludes the repo itself. Inside `$HOME`, the walk goes up to and
including `$HOME` and never above it (the intended `~/xp`/`~/work` folder-default
shape). Outside `$HOME`, the walk covers every ancestor directory except the filesystem
root. A worktree under `<repo>/.worktrees/<name>` has the primary repo as one of its
ancestors, so it inherits the primary repo's `task` block the same way any other
descendant does. Each ancestor's `task` block is independently validated (the same
`_validate_task` used for the global/repo layers), so a malformed ancestor config fails
closed, naming that ancestor's file. Disable the whole mechanism with
`riglib.config.load(..., include_ancestors=False)`.

**Only trusted ancestors are read.** An ancestor `rig.yaml` — and the directory holding it —
must be owned by the current user and not group/world-writable, or it is skipped (logged at
WARNING). This is task-cli's `_trusted_ancestor` rule, so both tools read the same ancestors:
outside `$HOME` the walk passes shared directories such as `/tmp`, and a `rig.yaml` another
local user planted there must not retarget your tickets. Windows skips the check. A filesystem
that reports every file as `0777` or root-owned (WSL `drvfs` under `/mnt/c` without the
`metadata` mount option, some NFS/SMB mounts) fails it for every ancestor; the WARNING names the
file or directory, and the fix is a mount that keeps real ownership or a repo-level `task:` block.

**An ancestor alone is not a declared config.** An ancestor contributes only `task` routing,
which `rig apply` does not consume, so a directory whose only layer is an ancestor still gets
`rig apply`'s "no rig.yaml found" refusal (and config-web's), instead of applying built-in
defaults (`LoadedConfig.has_declared_config`).

**`include_repo=False` stays GLOBAL-only.** A caller that excludes the repo scope
(`load(..., include_repo=False)` — e.g. a global-config-only read) never sees ancestor
task routing leak in either: ancestors are repo-scoped folder defaults, not global
config, so the ancestor overlay is gated by `include_repo` in addition to
`include_ancestors`. The global layer itself still loads normally in this mode.

**Provenance (`LoadedConfig.layers`).** Every ancestor file that actually contributed to
the merged `task` block (i.e. whose contribution was non-empty after stripping
`code_prefix`) is listed in `LoadedConfig.layers` as `ancestor:<path>`, positioned
between the `global:<path>` and `repo:<path>` entries — matching the `global <
ancestors < repo` cascade order, outermost ancestor first. An ancestor consulted but
contributing nothing (no `rig.yaml`, no `task:` key, or a `task:` block that only set
`code_prefix`) is not listed.

### `backend: forgejo` — native Forgejo issues (UB-25)

A fourth tracker backend value: native Forgejo issues on a self-hosted instance (e.g.
`git.hyperide.ai`). This is a real issue backend, not an alias of `github-issues`. The canonical
name is exactly `forgejo`; there is no `fj` alias. Coordinates live under `task.forgejo`:

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `forgejo.url` | str or null | — | the Forgejo instance base URL, e.g. `https://git.hyperide.ai`. Must be `https://` with a host (and a numeric port, if any), no whitespace, no query/fragment, no `@` or `%` (so no embedded `user:token@` credentials, plain or percent-escaped), and must NOT end in `/api/v1` or `/`. A rejection never echoes the value, since a malformed base URL is where a pasted token tends to hide. Why the suffixes matter: task-cli derives the API base by appending `/api/v1`, so either suffix would produce a doubled path and is rejected. |
| `forgejo.repo` | str or null | — | this repo's issue store as `owner/name` (e.g. `ultrabricks/rig-cli`), or `auto` to resolve it from the repo's own git `origin` at runtime. Anything else (`owner/`, `a/b/c`, any whitespace including a trailing newline, `AUTO`) is rejected. |
| `forgejo.attachment_mode` | str or null | — | `native` (upload through the Forgejo API) or `link` (register an already-hosted URL as-is) |
| `forgejo` | object or null | — | the nested section; a bare `forgejo:` header (null) is tolerated the same as absent, `forgejo: false` is rejected |

All three leaves are also registered as wizard options (`riglib/schema.py`, `task` Area), so
`rig setup` and `rig config set` accept them. The default backend contract for a repo is:

```yaml
task:
  backend: forgejo
  forgejo:
    url: https://git.hyperide.ai
    repo: owner/name
    attachment_mode: native
```

#### Cascade contract for `task.forgejo`

**The contract, in one sentence:** an explicit `task.forgejo.repo` is inherited normally from
ancestor configs in BOTH rig and task-cli, and only `code_prefix` is stripped; `repo: auto` is the
documented workspace fallback for when each repo should resolve its own origin.

In detail: `task.forgejo` cascades exactly like every other `task` routing key, in both rig and
task-cli: `global < ancestors (outermost first, nearest wins) < repo`, merged per key. An explicit
`owner/name` in an ancestor `rig.yaml` IS inherited by every repo underneath that does not set its
own. rig and task-cli must resolve the same file to the same value, so rig neither strips, refuses,
nor warns about an inherited explicit `repo`.

Because of that, the rule is:

- A folder-default ancestor sets `backend`, `forgejo.url`, and `forgejo.repo: auto`. Each repo
  underneath then resolves its own `owner/name` from its own `origin` remote.
- An explicit `forgejo.repo: owner/name` belongs only in the repo's OWN `rig.yaml`. It overrides
  an inherited `auto`.
- An explicit `owner/name` in a folder-level ancestor therefore routes every repo beneath it
  that does not override `repo` into that one repository. That is intended when the ancestor is
  a primary checkout (its `rig.yaml` is a legitimate ancestor of its own `.worktrees/<name>`
  worktrees and carries exactly such a value); for a workspace folder, use `auto`.

Per-repo explicit example (the repo pins its own issue store):

```yaml
# ~/xp/rig-cli/rig.yaml
task:
  backend: forgejo
  forgejo:
    url: https://git.hyperide.ai
    repo: ultrabricks/rig-cli
    attachment_mode: native
```

Inheritance-fallback example (a folder default supplies the backend and instance; each repo
supplies only its own identity, or nothing at all to fall back to `auto`):

```yaml
# ~/work/rig.yaml (folder default)
task:
  backend: forgejo
  forgejo:
    url: https://git.hyperide.ai
    repo: auto
```

```yaml
# ~/work/hyperide/rig.yaml (inherits backend + url; pins its own repo)
task:
  forgejo:
    repo: hyperide/hyperide
```

A sibling repo under `~/work` with no `task:` block of its own inherits `repo: auto` and resolves
its issue store from its own `origin`.

#### Migrating a repo to `forgejo`

There is no dedicated migration command. The existing single-key editor handles it: each call
writes one nested key, validates the result, and rolls back on a validation failure. It is
idempotent (re-running with the same value changes nothing). To move a repo off a stale
`github-issues` or `linear` override:

```sh
rig config set task.backend forgejo
rig config set task.forgejo.url https://git.hyperide.ai     # omit when an ancestor already sets it
rig config set task.forgejo.repo owner/name                  # or `auto`
```

`rig config set` never removes a leftover `task.github`/`task.linear` section or a flat
`task.repo`; delete those by hand. Remove a flat `task.repo` in particular: rig documents it as
shorthand for `github.repo`, but task-cli's Forgejo branch also fans it out onto `forgejo.repo`.
For that reason, under `backend: forgejo` rig applies the same `owner/name`-or-`auto` check to a
flat `task.repo` as to `task.forgejo.repo`.
