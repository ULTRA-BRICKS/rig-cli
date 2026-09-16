# Review addendum: agent-tools/docs/specs/2026-06-15-harness-layer-redesign.md

Source revision: `5bd4dca121baf3034f9800c70c58fae2f53c3f82`.

This source document is retained with its original draft/approved/skipped status. This addendum does not approve previously skipped work. Root `rig-cli/SPEC.md` describes Evolve, not the entire rig ecosystem.

Current recovery contract: [Declarative provisioning, reproducible installation and honest drift](../provisioning.md). Governing epic: https://github.com/alex-mextner/rig-cli/issues/521.

## Example and revalidation

The operator disables a managed integration and runs reconciliation. Only owned artifacts are removed; manual files are preserved. The same declared state drives apply and status. A managed-file comment names the supported configuration command rather than merely saying do not edit.

## Source quotations

> Чтобы rig сам делал всё, что нужно.

Source **Q019**; compiled-fragment-attribution; rig-zerobug/rig-fable session 2026-09-04..07; agent=нет; PDF page(s) 44. Thematic recovery requirement, not retroactive authorship of this entire specification.

**Context fragment:** do not interpret as a standalone approval.

<details><summary>Original wording (before editorial correction)</summary>

> риг сам делал все что нужно

</details>

> В белых списках всех агентов.

Source **Q018**; compiled-fragment-attribution; rig-zerobug/rig-fable session 2026-09-04..07; agent=нет; PDF page(s) 43. Thematic recovery requirement, not retroactive authorship of this entire specification.

**Context fragment:** do not interpret as a standalone approval.

<details><summary>Original wording (before editorial correction)</summary>

> в белых списках всех агентов

</details>

## Linked acceptance backlog

- [agent-tools#15](https://github.com/alex-mextner/agent-tools/issues/15) — Slim ~/.claude/CLAUDE.md to PERSONAL-ONLY (move tool/process docs into skills)
- [agent-tools#86](https://github.com/alex-mextner/agent-tools/issues/86) — model-freshness checker: adopt shared agenttools_errors (what/why/how + structured exit codes)
- [agent-tools#89](https://github.com/alex-mextner/agent-tools/issues/89) — model-freshness checker: adopt shared agenttools_errors (structured WHAT/WHY/HOW + stable exit codes)
- [agent-tools#155](https://github.com/alex-mextner/agent-tools/issues/155) — fix(review-gate): use `command git` to bypass shell aliases/wrappers in hash computation
- [agent-tools#219](https://github.com/alex-mextner/agent-tools/issues/219) — Fix non-Claude harness hook provisioning
- [agent-tools#250](https://github.com/alex-mextner/agent-tools/issues/250) — Investigate weak non-Claude harness provisioning
- [agent-tools#251](https://github.com/alex-mextner/agent-tools/issues/251) — Investigate harness provisioning gaps
- [agent-tools#254](https://github.com/alex-mextner/agent-tools/issues/254) — Fix Codex orchestration context switching via rig provisioning
- [agent-tools#293](https://github.com/alex-mextner/agent-tools/issues/293) — Finish pi-permission-guard extension module (salvaged WIP)
- [agent-tools#296](https://github.com/alex-mextner/agent-tools/issues/296) — Remove lib/agenttools_dev — dev extracted to standalone dev-cli
- [agent-tools#325](https://github.com/alex-mextner/agent-tools/issues/325) — fix(pi): close 5 permission-guard bypasses found in review of agent-tools#294
- [agent-tools#500](https://github.com/alex-mextner/agent-tools/issues/500) — agent-tools commits bypass review-gate (secret-scan only, no review-before-commit)
- [agent-tools#502](https://github.com/alex-mextner/agent-tools/issues/502) — stop-completion-selfcheck: classify the turn instead of firing a static checklist
- [agent-tools#613](https://github.com/alex-mextner/agent-tools/issues/613) — Hooks said no; the agent still wrote files and ran review through other tools
- [agent-tools#631](https://github.com/alex-mextner/agent-tools/issues/631) — Block raw gh issue and Linear when task-cli is configured
- [agent-tools#640](https://github.com/alex-mextner/agent-tools/issues/640) — Map omp tool_call+yield to v1 stop so session-end hooks can block a premature yield
- [agent-tools#661](https://github.com/alex-mextner/agent-tools/issues/661) — Map opencode skill tool to pre-skill so skills-marker-writer fires
- [agent-tools#665](https://github.com/alex-mextner/agent-tools/issues/665) — Wire generated zsh tab-completion into each ecosystem CLI

## Preserved specification

# Harness-layer redesign — per-harness auto-mode + permission passthrough

Status: **DESIGN** (2026-06-15). Drives rig-cli harness provisioning + tg-ctl passthrough.
Supersedes the hard-coded "auto_mode → write `bypassPermissions` to committed
`.claude/settings.json`" mapping.

## Why (research findings, 2026-06-15)

1. **Claude Code `auto` mode is USER-level only.** Docs (permission-modes.md) + empirical
   (CC 2.1.177, this machine): `defaultMode: "auto"` in a repo's project/local
   `.claude/settings.json` is **silently ignored** since CC v2.1.142 (project → starts in
   `default`, exit 0, no warning). It is honored ONLY from `~/.claude/settings.json`,
   managed settings, or the `--permission-mode auto` launch flag. Other modes
   (`acceptEdits`/`plan`/`dontAsk`) ARE honored at project scope — only `auto` is stripped.
2. **`auto` > `bypassPermissions` for a real machine.** `auto` (research preview, v2.1.83+)
   auto-approves but a separate classifier model blocks actions that escalate beyond the
   request, touch unrecognized infra, or look prompt-injected. `bypassPermissions` skips
   everything (docs: "containers/VMs only"). Requires Opus 4.6+/Sonnet 4.6+; admin can
   hard-disable via `permissions.disableAutoMode`.
3. **Committed per-repo `bypassPermissions` is now actively wrong:** it's the footgun mode
   AND, because project settings outrank user settings, a committed project
   `bypassPermissions` **overrides** a safer user-level `auto`. The 6 tool repos that
   currently commit it must be migrated.
4. **Every harness differs** — see the table. rig must branch on `harness.kind` across two
   independent axes (auto-mode provisioning, permission passthrough), not hard-code CC.

## Per-harness model

Model each harness as `{ kind, auto_mode_strategy, passthrough_mechanism }`:

| harness | auto-mode strategy | where | passthrough to TG |
|---|---|---|---|
| **claude-code** | config-file value `auto` | **USER** `~/.claude/settings.json` (project ignored) | `PermissionRequest` hook → tg-ctl (already wired) |
| **codex** | config-file `approval_policy=never` + `sandbox_mode=workspace-write` | `~/.codex/config.toml` | app-server JSON-RPC approval (best seam) |
| **opencode** | config-file `permission:"allow"` (or allowlist) | committed `opencode.json` | `permission.ask` / `tool.execute.before` plugin hooks (buggy — pair with allowlist) |
| **gemini** | **launch-flag** `--yolo`/`--approval-mode=yolo` (NOT stickable via settings) + `GEMINI_CLI_TRUST_WORKSPACE=true` | launch command | `hooks.BeforeTool` (rig ships the gate) |
| **commandcode** | launch-flag `--permission-mode auto-accept`/`--yolo` (config key unconfirmed) | launch command | CC-lineage hooks (unverified) |
| **pi** | **none** — already promptless | n/a | `pi.on("tool_call")` extension (rig must ship the gate); emit a **sandbox/containment warning** instead of an auto-mode setting |

## Concrete rig-cli changes

`riglib/plan.py`:
- Replace the scalars `_HARNESS_SETTINGS` / `_HARNESS_AUTO_MODE` with a per-kind descriptor
  carrying `auto_mode_strategy` (`config-file:user` | `config-file:project` |
  `launch-flag` | `none`), the target path/scope, and the value/flag to write.
- claude-code: strategy `config-file:user`, target `~/.claude/settings.json`,
  value `defaultMode: auto`. (This is a **global/machine** action — gate on `scope`
  including global; it no longer "travels with the repo".)
- Emit a per-kind passthrough provisioning action (CC: ensure tg-ctl's `PermissionRequest`
  hook is installed; codex: app-server approval wiring; etc.).
- **Migration:** `rig apply` (and `rig status` drift) must detect + remove a stale committed
  project `.claude/settings.json` `defaultMode: bypassPermissions` it previously wrote, now
  that the value moved to user-level `auto`. Affects agent-tools, review-cli, task-cli,
  draw-cli, 3d-cli (and the rig-cli self-dogfood). Leave non-rig-managed keys untouched.

`rig.yaml`: `harness.auto_mode: true` stays the declaration; the *realization* is per-kind
(user-level for CC). Document that CC auto-mode is per-machine, set at `rig init`/`rig apply`
with global scope, not committed per repo.

## Sequencing

1. **CC user-level `auto`** + migrate the 6 repos off committed `bypassPermissions`. (Core fix.)
2. **Hook-bridge #18** (PR #20 + rig-cli #12) — guards fire as PreToolUse, complementary to
   `auto`'s classifier. Live-CC verified 2026-06-15 (raw `gh pr merge` → CC deny; benign →
   pass). #20 conflict resolved + pushed; land #20 then #12.
3. **codex + opencode** auto-mode + passthrough (first-class, config-file + real approval seams).
4. **gemini + commandcode** (launch-flag auto + best-effort hooks); **pi** (no-op + sandbox warning).

## Open / to verify

- Does CC fire the `PermissionRequest` hook in `auto` mode when the classifier blocks a tool?
  If yes, tg-ctl passthrough works today; if it surfaces differently, needs another seam.
  (tg-ctl already installs the hook; the auto-mode round-trip was never live-spiked.)
- `auto` is a research preview + model-gated — keep `bypassPermissions` (committed, +bridge)
  as the documented fallback for non-qualifying accounts / container runs.
