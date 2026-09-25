# Spec: Claude Code plugin-marketplace-sourced skill provisioning (`skills.plugins`)

Status: landed (this PR).
Origin: manual fix-up of 14 hand-symlinked `superpowers` plugin skills (haft-provisioning-part-c,
PR #590) exposed a real gap — this closes it with a declarative, self-healing mechanism.

## 1. Problem

A prior session needed the Claude Code `superpowers` marketplace plugin's skills
(`brainstorming`, `writing-plans`, `using-git-worktrees`, …) visible to every harness (Claude
Code, Codex, omp), the same way the 78 agent-tools catalog skills already are. The plugin's skill
content lives at a **version-pinned** cache path:

```
~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/skills/<name>/SKILL.md
```

tracked by Claude Code's own `~/.claude/plugins/installed_plugins.json`. The one-off fix
hand-symlinked `~/.agents/skills/<name> → .../superpowers/6.4.1/skills/<name>` for each of 14
skills (skipping a 15th, `systematic-debugging`, which collides with an unrelated pre-existing
agent-tools catalog skill of the same name). Two problems with stopping there:

1. **The symlinks are not self-healing.** The next time Claude Code auto-updates the
   `superpowers` plugin, `installed_plugins.json`'s `installPath` moves to a new version
   directory (e.g. `6.5.0`) — nothing re-points the hand-made symlinks. They dangle silently.
2. **`rig status` has no way to recognize them.** They are real dirs/symlinks on
   `~/.agents/skills` that no `rig.yaml`/config declares, so `rig status` reports all 14 (plus 3
   unrelated hand-authored skills, `h-reason`/`debate-swarm`/`moshi-best-practices`, fixed
   separately via `skills.known` — see docs/config-schema.md#provenance) as undeclared drift.

## 2. Design

### 2.1 Config surface

```yaml
skills:
  plugins:
    items:
      superpowers:
        marketplace: claude-plugins-official
```

Mirrors this repo's existing `skills.by_type`/`skills.by_stack` shape exactly: a nested block
under `skills` carrying its own `items` open map, keyed by name (here, the plugin's own name),
each item a small fixed-shape spec (`{marketplace: <str>}`). See `riglib/config_schema.py`'s
`_SKILLS_PLUGIN_ITEM_BLOCK` / `_SKILLS_BLOCK.nested["plugins"]` and
`riglib/config.py`'s `_validate_skills_plugins`.

An array-of-objects shape (`plugins: [{marketplace: ..., plugin: ...}]`) was considered and
rejected: every other "list of named things with attributes" block in this schema (`mcp.items`,
`tools.items`, `by_type.items`, `by_stack.items`) is an open map keyed by name, never an array —
matching that convention keeps one idiom across the whole schema instead of introducing a second.

### 2.2 Resolution (`riglib/plugin_skills.py`)

At every `rig apply` (inside `plan.build()`, immediately after the catalog skill loop):

1. Read `~/.claude/plugins/installed_plugins.json` (best-effort: a missing/malformed file
   resolves to "nothing installed", never a crash).
2. For each declared `skills.plugins.items.<plugin>`, look up `<plugin>@<marketplace>` in the
   file's `plugins` map. It lists one entry **per install scope** (`user` = machine-wide,
   `project` = pinned inside one repo's `.claude` dir) — prefer the `user`-scoped entry (what
   Claude Code resolves to outside that one pinned project); among remaining candidates, the most
   recently updated one wins.
3. Not found (plugin never installed on this machine, or a typo'd marketplace/plugin name) →
   record a `plan.notes` line, skip. Never a fatal `rig apply` failure — a declared plugin can be
   legitimately absent on a CI runner or a laptop without Claude Code.
4. **Confinement (security review finding).** `installed_plugins.json` is Claude Code's OWN
   file, not rig's — a corrupted write or a compromised plugin could point `installPath`
   anywhere on disk. A resolved `installPath` outside `~/.claude/plugins/` is refused (degrades
   to a `plan.notes` line, same as "not found"), never trusted as a symlink source.
5. Enumerate `<resolved-install-path>/skills/*/SKILL.md` (mirrors the catalog scanner's own "a
   skill is a dir with a SKILL.md" contract). **Confinement (security review finding):** a
   candidate whose resolved path lands outside the plugin's OWN install root is excluded, not
   followed — a marketplace archive can ship a directory symlink inside its own extracted tree
   (e.g. `skills/pwned -> /somewhere/else`); this must never become a global skill symlink
   source.
6. A skill name colliding with a real agent-tools **catalog** skill (any leaf name in
   `plan.catalog_items["skills"]`, machine-wide, not gated on this repo's stack) is skipped —
   the catalog is rig's own namespace and always wins. Recorded as a `plan.notes` line, matching
   the `systematic-debugging` precedent this feature was built to formalize.
7. A skill name colliding with a same-named skill from a DIFFERENT declared plugin is skipped
   too — without this, two `link_skill_plugin` actions would target the same
   `skills_target/<name>` with different sources, flip-flopping which one "wins" on every apply
   (never idempotent) and leaving the loser permanently `modified` in drift. The
   alphabetically-first plugin name wins (matching `resolve_plugin_skills`'s stable sort); the
   loser is recorded as a `plan.notes` line.
8. Everything else becomes a `link_skill_plugin` action: `target/<skill-name> →
   <resolved-install-path>/skills/<skill-name>` — plus the usual `link_skill_harness` fan-out
   into every configured harness's discovery dir, exactly like a catalog skill gets.

### 2.3 Execution + drift (idempotent, self-healing)

`link_skill_plugin` reuses `actions/runner.py`'s existing `_do_link_skill_harness` handler
verbatim — both maintain "idempotent symlink at `target` pointing to `source`": a correct
symlink is a no-op, a **wrong** one is re-pointed (never backed up — a symlink carries no user
data), and a **real** (non-symlink) dir/file already there is left untouched (never clobber
hand-authored content or a catalog skill). This is what makes a plugin version bump self-heal:
the next `rig apply` re-resolves `installed_plugins.json`, gets the new `installPath`, and the
existing symlink at the SAME target gets re-pointed — nothing dangles, nothing needs a manual
fix.

`riglib/drift.py`'s `detect()` treats a `link_skill_plugin` action's target exactly like a
`copy_skill` action's target: it is added to `declared_skill_dirs`, so `rig status`'s "extra"
(undeclared) skill scan skips it entirely — it never reaches the provenance classification
(`skills.known`, `.installed-by` markers, …) other undeclared items go through, because it is not
undeclared: it is directly plan-declared, the same as a catalog skill. `_check_link_skill_plugin`
flags missing/stale-symlink drift the same way `_check_skill_harness_link` does.

## 3. Scope and limitations

- **Claude Code's plugin marketplace only** — `~/.claude/plugins/installed_plugins.json`. Codex,
  Gemini, and any other agent's plugin/extension system are not read by this mechanism (none is
  known to rig at the time of writing).
- **Skills only.** Only a plugin's `skills/*` subtree is symlinked. A plugin's hooks, MCP
  servers, slash commands, or any other capability it ships are untouched — this is not a
  general third-party-plugin provisioning feature, just the one gap (skill discoverability
  outside Claude Code) that was actually hit.
- **No `skills.plugins.known` needed.** Because resolution happens inside `plan.build()` (a real
  plan action, not a post-hoc disk scan), a plugin-sourced skill is never "undeclared" in the
  first place — `skills.known` remains for skills rig genuinely has no other way to place (a
  hand-authored pack with no catalog/plugin backing, like `h-reason`).

## 4. Testing

`tests/test_skill_plugins.py` (hermetic — fake agent-tools + a fixture `installed_plugins.json`
 plus a tmp `HOME`, no dependency on the real machine's Claude Code install):

- unit coverage for `resolve_plugin_install_path` (scope preference, tie-breaking) and
  `plugin_skill_dirs` (SKILL.md-only filtering);
- config validation (accepts a well-formed block; rejects a non-string/empty `marketplace`, a
  missing `marketplace`, and an unknown `skills.plugins` key);
- (a) a fresh declaration resolves and links every skill the fixture plugin ships, and `rig
  status`-equivalent drift detection reports it converged, not undeclared;
- (b) re-applying with nothing changed is a clean idempotent no-op (`skipped`, not
  `created`/`updated` again);
- (c) a simulated plugin version bump (a second fixture `installPath`, mirroring a real
  marketplace auto-update) re-points the existing symlink at the next apply, with zero stale
  drift afterward;
- (d) a plugin skill name colliding with a real catalog skill (`naming`, from the shared
  `fake_agent_tools` fixture) is skipped — the catalog copy is provably untouched, never
  shadowed;
- (e) two DIFFERENT declared plugins shipping a same-named skill: exactly one
  `link_skill_plugin` action is emitted (alphabetically-first plugin), the loser is noted, and a
  second apply stays idempotent with zero drift — never flip-flops which plugin "wins";
- (f) security: an `installPath` resolving OUTSIDE `~/.claude/plugins/` is refused (a plan note,
  never a symlink source), and a `skills/<name>` entry that is itself a symlink escaping the
  plugin's own install root is excluded from `plugin_skill_dirs`, never followed;
- a declared-but-not-installed plugin degrades to a plan note, never a crash, including three
  malformed `installed_plugins.json` shapes (invalid JSON, a non-dict root, a non-dict
  `plugins` value) and a genuinely missing file;
- `skills.enabled: false` suppresses `link_skill_plugin` actions too, even when the plugin
  resolves fine — it is the one gate the whole block sits behind;
- two DIFFERENT, non-colliding plugins declared together resolve independently (no
  cross-contamination), each fanning out `link_skill_harness` actions (and drift coverage) into
  a configured harness skill dir exactly like a catalog skill does;
- the no-`skills.plugins`-at-all case (the overwhelming majority of repos) emits zero
  `link_skill_plugin` actions — no surprise behavior for a repo that never opted in.

## 5. This machine's real config

`~/.config/rig/config.yaml` (global) now carries:

```yaml
skills:
  known: [swiftui, h-reason, debate-swarm, moshi-best-practices]
  plugins:
    items:
      superpowers:
        marketplace: claude-plugins-official
```

`known` covers the 3 genuinely hand-authored, plugin-less skills (`h-reason`, `debate-swarm`,
`moshi-best-practices`, alongside the pre-existing `swiftui`); the 14 `superpowers` plugin skills
need no `known` entry — `skills.plugins.items.superpowers` makes them natively
plan-declared/converged instead.
