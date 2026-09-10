"""Cross-harness clean-room parity matrix on Apple's native ``container`` CLI (rig-cli#337).

What this is
------------
The sibling ``test_cleanroom_e2e.py`` (Docker) proves ``rig init`` for a brand-new user — but
only ever with ``harness.kind: claude-code``. This module runs the SAME clean-room shape
(fresh non-root user, empty ``$HOME``, self-contained fake agent-tools checkout, the REAL CLI,
an in-container assertion script) under Apple's ``container`` CLI instead of Docker, and
parametrizes it over every supported harness kind: claude-code, codex, opencode, omp, pi, commandcode.

Per kind the in-container script asserts (#337's acceptance list, 1:1):

(a) SKILLS ARE DISCOVERABLE THE WAY THAT HARNESS EXPECTS — ``riglib/harness_skills.py``'s
    discovery families: a symlink dir for claude-code (``~/.claude/skills``) and codex
    (``~/.codex/skills``), native ``~/.agents/skills`` loading for opencode and omp (rig links
    nothing AND says so in the plan notes), and a global instruction file for pi
    (``~/.config/pi/AGENTS.md``) and commandcode (``~/.commandcode/AGENTS.md``) with a
    visible "no skill-discovery dir" note (rig-cli#461).
(b) AUTO-MODE WRITE HAPPENED **OR** IS A VISIBLE SKIP — a ``harness/<kind>`` plan action
    (claude-code / codex / opencode), or omp's mode written by the permissions approval
    action (noted as ``harness: omp auto-mode … is written by the permissions approval
    action``, rig-cli#370/#355), or an ``auto-mode write skipped`` / ``no auto/permission-mode setting`` n/a note (pi/commandcode). Silent = FAIL.
(c) AGENTS-HOOKS BRIDGE REGISTERED **OR** VISIBLY SKIPPED — a ``harness/hook-bridge`` action
    whose target file exists and names the bridge module, or a ``hook_bridge: skipped`` note.
    Silent = FAIL. omp_hook_bridge is a first-class kind (rig-cli#352 / agent-tools#556).
(d) GUARD PARITY — the REAL ``worktree-only-writes`` hook (vendored from a real agent-tools
    checkout when one is reachable on the host) is driven through each kind's OWN bridge
    dispatcher with a pre-write event: it must BLOCK on the default branch and ALLOW in a
    feature worktree, identically across kinds. A kind whose bridge is absent/unregistered
    records ``PARITY-BLOCKED`` with the reason instead of pretending.
Plus the two invariants the Docker clean-room already proves: a second ``rig apply`` is
idempotent, and ``rig status`` exits 0. The Docker orchestrator-block checker (#427/#437)
also runs for every kind so CLAUDE.md / AGENTS.md stay in lockstep.

Network isolation — the verified mechanism
------------------------------------------
Apple ``container run`` (1.0.0) has NO ``--network none``: with no ``--network`` flag a
container joins the ``default`` NAT network and HAS egress (verified live: a TCP connect to
``1.1.1.1:53`` succeeded; ``--no-dns`` and ``--cap-drop ALL`` do not change that, and root inside
the guest cannot delete the default route — ``RTNETLINK: Operation not permitted``). What DOES
isolate is a **host-only network**: ``container network create --internal <name>`` (mode
``hostOnly``) — a container attached to it cannot reach the internet OR the host gateway (both
time out). So this module creates one internal network per session, runs every kind on it, and
the in-container script's FIRST assertion is an egress probe that must FAIL — the offline
guarantee is re-proven on every run, never assumed. The one-time image BUILD still needs
apt/PyPI egress, exactly like the Docker variant.

Invariants
----------
- Opt-in (``RIG_CONTAINER_E2E=1``) + auto-skip when the ``container`` CLI/apiserver is absent.
  The default ``pytest -q`` stays hermetic.
- ONE image per session (module-scoped), kinds run strictly SERIALLY on it, image + network
  deleted in teardown. The host's free disk is checked before the build (``container`` images
  live on the boot volume; a full disk has killed sibling test runs).
- Kind is passed at RUN time (``RIG_E2E_KIND``), never baked into a per-kind image.
- ``RIG_CONTAINER_E2E_KINDS`` (comma list) narrows the matrix for a targeted run.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
import uuid
from pathlib import Path

import pytest

import test_cleanroom_e2e as cr

REPO_ROOT = Path(__file__).resolve().parent.parent

KINDS: tuple[str, ...] = ("claude-code", "codex", "opencode", "omp", "pi", "commandcode")
MIN_FREE_DISK_GB = 3.0

_E2E_OPTED_IN = os.environ.get("RIG_CONTAINER_E2E", "").strip().lower() in ("1", "true", "yes")

pytestmark = pytest.mark.skipif(
    not _E2E_OPTED_IN,
    reason="clean-room Apple-container matrix is opt-in: set RIG_CONTAINER_E2E=1",
)


def _container() -> str:
    return os.environ.get("CONTAINER", "container")


def _container_available() -> bool:
    """True when the Apple ``container`` CLI is on PATH and its apiserver answers."""
    if shutil.which(_container()) is None:
        return False
    try:
        r = subprocess.run(
            [_container(), "system", "status"], capture_output=True, text=True, timeout=30
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _selected_kinds() -> tuple[str, ...]:
    raw = os.environ.get("RIG_CONTAINER_E2E_KINDS", "").strip()
    if not raw:
        return KINDS
    chosen = tuple(k.strip() for k in raw.split(",") if k.strip())
    unknown = [k for k in chosen if k not in KINDS]
    if unknown:
        raise ValueError(f"RIG_CONTAINER_E2E_KINDS names unknown kind(s): {unknown}")
    return chosen


def _free_disk_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / (1024**3)


# ── real agent-tools pieces for the parity leg ────────────────────────────────────────────
# The fake checkout from test_cleanroom_e2e ships STUB bridges (dispatch returns 0) — enough to
# prove rig WIRES the bridge, useless for proving the guard FIRES through it. When a real
# agent-tools checkout is reachable on the host, its bridge dispatchers + the shared v1 runner +
# the real worktree-only-writes hook are vendored over the stubs so the in-container parity leg
# drives the genuine code path. Small (under 1MB total) and stdlib-only at runtime.
_REAL_LIBS = (
    "agent_hooks_v1",
    "agenttools_hatch_escalation",
    "cc_hook_bridge",
    "codex_hook_bridge",
    "opencode_hook_bridge",
    "omp_hook_bridge",
)
_PARITY_HOOK = "worktree-only-writes"


def _real_agent_tools_source() -> Path | None:
    from riglib import catalog

    try:
        return catalog.resolve_source(None)
    except Exception:  # noqa: BLE001 — no checkout is a legitimate (CI) state, not an error
        return None


def _ensure_stub_bridges(fake_root: Path) -> None:
    """The Docker fake checkout only ships ``cc_hook_bridge``. The matrix needs the other
    dispatchers present so rig actually REGISTERS each kind's bridge — a missing package is a
    visible skip, which would hide a wiring regression. Real agent-tools pieces overlay these
    stubs when a checkout is reachable."""
    lib = fake_root / "lib"
    for name in ("codex_hook_bridge", "opencode_hook_bridge", "omp_hook_bridge"):
        if (lib / name / "dispatch.py").is_file():
            continue
        cr._write(lib / name / "__init__.py", "")
        cr._write(
            lib / name / "__main__.py",
            "import sys\nfrom .dispatch import main\nraise SystemExit(main(sys.argv[1:]))\n",
        )
        cr._write(lib / name / "dispatch.py", "def main(argv=None):\n    return 0\n")
    if not (lib / "opencode_hook_bridge" / "plugin.js").is_file():
        cr._write(
            lib / "opencode_hook_bridge" / "plugin.js",
            "export const AgentToolsHookBridge = async () => ({});\n",
        )
    if not (lib / "omp_hook_bridge" / "extension.ts").is_file():
        cr._write(
            lib / "omp_hook_bridge" / "extension.ts",
            "export default function (pi) { return {}; }\n",
        )


def _vendor_real_pieces(fake_root: Path) -> dict[str, str]:
    """Overlay the real bridges/runner/hook onto the fake checkout; return a provenance map
    (piece → source path, or the reason it is missing) recorded into the image for the report."""
    provenance: dict[str, str] = {}
    real = _real_agent_tools_source()
    if real is None:
        provenance["*"] = "no real agent-tools checkout on host — parity leg runs on stubs (skipped)"
        return provenance
    strip = shutil.ignore_patterns("__pycache__", "*.pyc", "*.rig-bak-*")
    for lib in _REAL_LIBS:
        src = real / "lib" / lib
        if src.is_dir():
            shutil.copytree(src, fake_root / "lib" / lib, ignore=strip, dirs_exist_ok=True)
            provenance[lib] = str(src)
        else:
            provenance[lib] = f"missing in {real}"
    hook = real / "agent-hooks" / _PARITY_HOOK
    if hook.is_dir():
        shutil.copytree(hook, fake_root / "agent-hooks" / _PARITY_HOOK, ignore=strip, dirs_exist_ok=True)
        provenance[_PARITY_HOOK] = str(hook)
    else:
        provenance[_PARITY_HOOK] = f"missing in {real}"
    return provenance


# The per-kind clean-room rig.yaml. Same OFF list as the Docker config (no daemons/network), the
# four acceptance subjects ON, `worktree_only: true` so the parity hook is ENROLLED for the test
# repo, and NO `settings_path` (a claude-code-shaped path would be a wrong-suffix skip for the
# toml/plugin/extension bridges — each kind must land in its own default file).
_MATRIX_CONFIG = """\
version: 1
agent_tools_source: __SRC__
skills:
  enabled: true
  universal: { all: true }
  by_type:   { enable: [cli] }
agent_hooks:
  all: true
  worktree_only: true
git_hooks:
  dispatcher: { enabled: true }
ci:
  items:
    secret-scan:    { enabled: true, tier: block }
    leftover-grep:  { enabled: true, tier: block }
    review-threads: { enabled: true, tier: block }
harness:
  enabled: true
  kind: __KIND__
  auto_mode: true
models:    { enabled: false }
github:    { ruleset: { enabled: false } }
tg_ctl:    { enabled: false }
tmux:      { enabled: false }
gitignore: { enabled: false }
"""


# The parity driver: builds the kind's NATIVE pre-write event for a write into `repo`, pipes it
# through that kind's bridge dispatcher (`python3 -m <module> <event>`), and prints the verdict.
# The bridge reads descriptors from its own DEFAULT dir (the one rig provisioned) — no env
# override — so this proves rig's provisioning and the bridge's discovery agree.
_PARITY_PY = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json
    import os
    import subprocess
    import sys

    BRIDGES = {
        "claude-code": ("cc_hook_bridge", "PreToolUse"),
        "codex": ("codex_hook_bridge", "PreToolUse"),
        "opencode": ("opencode_hook_bridge", "tool.execute.before"),
        "omp": ("omp_hook_bridge", "tool_call"),
    }


    def native_event(kind, repo):
        target = os.path.join(repo, "parity.txt")
        if kind == "claude-code":
            return {"hook_event_name": "PreToolUse", "tool_name": "Write",
                    "tool_input": {"file_path": target, "content": "x"}, "cwd": repo}
        if kind == "codex":
            patch = "*** Begin Patch\\n*** Add File: parity.txt\\n+x\\n*** End Patch"
            return {"hook_event_name": "PreToolUse", "tool_name": "apply_patch",
                    "tool_input": {"command": patch}, "cwd": repo}
        if kind == "opencode":
            return {"hook": "tool.execute.before",
                    "input": {"tool": "write", "args": {"filePath": target, "content": "x"}},
                    "cwd": repo}
        if kind == "omp":
            return {"event": "tool_call", "toolName": "write",
                    "input": {"path": target, "content": "x"}, "cwd": repo}
        raise SystemExit(f"unknown kind {kind}")


    def verdict(kind, lib, repo):
        module, event = BRIDGES[kind]
        proc = subprocess.run(
            [sys.executable, "-m", module, event],
            input=json.dumps(native_event(kind, repo)), capture_output=True, text=True,
            cwd=repo, env={**os.environ, "PYTHONPATH": lib}, timeout=120,
        )
        # The shipped bridges signal a block as exit 0 + JSON on stdout (verified live), but the
        # Claude Code / Codex native protocols ALSO allow "exit 2 + reason on stderr" — honor
        # both so a bridge that switches convention reads as a block, not a crash.
        if proc.returncode == 2:
            return "block", f"exit 2: {proc.stderr.strip()[:160]}"
        if proc.returncode != 0:
            return "error", f"bridge exited {proc.returncode}: {proc.stderr.strip()[:200]}"
        out = proc.stdout.strip()
        if not out:
            return "allow", proc.stderr.strip()[:160]
        try:
            data = json.loads(out)
        except ValueError:
            return "error", f"bridge stdout is not JSON: {out[:160]}"
        if not isinstance(data, dict):
            return "error", f"bridge stdout is not an object: {out[:160]}"
        specific = data.get("hookSpecificOutput")
        blocked = data.get("decision") == "block" or (
            isinstance(specific, dict) and specific.get("permissionDecision") == "deny"
        )
        return ("block" if blocked else "allow"), out[:160]


    def main():
        kind, lib, main_repo, feature_repo = sys.argv[1:5]
        on_main, why_main = verdict(kind, lib, main_repo)
        on_feature, why_feature = verdict(kind, lib, feature_repo)
        print(f"PARITY-DETAIL: kind={kind} main={on_main} ({why_main}) feature={on_feature} ({why_feature})")
        if on_main == "block" and on_feature == "allow":
            print(f"PARITY: kind={kind} block-on-main=yes allow-on-feature=yes")
            return 0
        print(f"PARITY-FAIL: kind={kind} main={on_main} feature={on_feature}", file=sys.stderr)
        return 1


    if __name__ == "__main__":
        raise SystemExit(main())
    """
)


# The in-container assertion script. `RIG_E2E_KIND` selects the harness kind; every check
# prints a labeled OK/FAIL line, and the per-kind outcome lines (`AUTO-MODE:`, `BRIDGE:`,
# `PARITY…:`) are the machine-readable result table the pytest side collects.
_ASSERT_SCRIPT = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    set -euo pipefail

    KIND="${RIG_E2E_KIND:?RIG_E2E_KIND (harness kind) must be set}"
    SRC=/opt/agent-tools
    REPO="$HOME/project"
    OUT=/opt/cleanroom

    fail() { printf 'CLEANROOM-FAIL[%s]: %s\\n' "$KIND" "$1" >&2; exit 1; }
    pass() { printf 'CLEANROOM-OK[%s]:   %s\\n' "$KIND" "$1"; }

    # ── (0) OFFLINE: the internal network must have NO egress — proven, not assumed ──
    # several independent public endpoints incl. TCP/443, so a policy that happens to block one
    # resolver cannot masquerade as isolation; EVERY probe must fail.
    if python3 - <<'PY' 2>/dev/null
    import socket
    reached = []
    for host, port in (("1.1.1.1", 53), ("1.1.1.1", 443), ("8.8.8.8", 53), ("9.9.9.9", 443)):
        try:
            socket.create_connection((host, port), timeout=3).close()
            reached.append(f"{host}:{port}")
        except OSError:
            pass
    print("REACHED " + ", ".join(reached)) if reached else None
    raise SystemExit(0 if reached else 1)
    PY
    then
      fail "container HAS internet egress — the internal network is not isolating (see module docstring)"
    fi
    pass "offline: no egress to 1.1.1.1/8.8.8.8/9.9.9.9 (53+443) on the internal network"

    [ ! -e "$HOME/.claude/skills" ] || fail "pre-existing ~/.claude/skills on a brand-new user"
    [ ! -e "$HOME/.agents/skills" ] || fail "pre-existing ~/.agents/skills on a brand-new user"
    pass "brand-new user starts with a pristine HOME"

    git config --global user.email cleanroom@example.com
    git config --global user.name  cleanroom
    mkdir -p "$REPO"
    # `-b main` + one commit: the worktree-only hook resolves the default branch from a LOCAL
    # main/master branch when there is no origin/HEAD — an unborn branch would fail OPEN.
    git -C "$REPO" init -q -b main
    git -C "$REPO" commit -q --allow-empty -m "init"
    sed -e "s#__SRC__#$SRC#" -e "s#__KIND__#$KIND#" "$OUT/rig.yaml" > "$REPO/rig.yaml"

    rig init -C "$REPO" --config "$REPO/rig.yaml" --yes --apply >"$OUT/init.log" 2>&1 \\
      || { cat "$OUT/init.log"; fail "rig init --yes --apply exited non-zero"; }
    pass "rig init --yes --apply succeeded (kind=$KIND)"

    # the post-apply plan preview: the visibility surface for (b)/(c). --plan lists every
    # action as `category/item → target`; the ⚠ gap notes print by default, --notes adds the rest.
    rig apply info -C "$REPO" --config "$REPO/rig.yaml" --plan --notes >"$OUT/info.log" 2>&1 \\
      || { cat "$OUT/info.log"; fail "rig apply info exited non-zero"; }

    # ── (a) SKILLS DISCOVERABLE THE WAY THIS KIND EXPECTS ─────────────────────────
    [ -d "$HOME/.agents/skills" ] || fail "skills not installed into ~/.agents/skills"
    installed=$(find "$HOME/.agents/skills" -mindepth 2 -maxdepth 2 -name SKILL.md | wc -l | tr -d ' ')
    [ "$installed" -eq 5 ] || fail "expected exactly 5 installed skills, got $installed"
    INSTR=""
    case "$KIND" in
      claude-code) SKILL_DIR="$HOME/.claude/skills" ;;
      codex)       SKILL_DIR="$HOME/.codex/skills" ;;
      pi)          SKILL_DIR="" ; INSTR="$HOME/.config/pi/AGENTS.md" ;;
      commandcode) SKILL_DIR="" ; INSTR="$HOME/.commandcode/AGENTS.md" ;;
      *)           SKILL_DIR="" ;;
    esac
    if [ -n "$SKILL_DIR" ]; then
      [ -d "$SKILL_DIR" ] || fail "harness skill dir $SKILL_DIR not created"
      linked=0
      for d in "$HOME/.agents/skills"/*/; do
        name="$(basename "$d")"
        [ -L "$SKILL_DIR/$name" ] || fail "skill '$name' not symlinked into $SKILL_DIR"
        [ -f "$SKILL_DIR/$name/SKILL.md" ] || fail "link '$name' in $SKILL_DIR does not resolve"
        linked=$((linked + 1))
      done
      [ "$linked" -eq 5 ] || fail "expected exactly 5 harness-linked skills, got $linked"
      pass "skills discoverable via symlink dir ($linked links resolve in $SKILL_DIR)"
    elif [ -n "$INSTR" ]; then
      # instruction-file: no skill dir, a global AGENTS.md, and a VISIBLE note (silent no-link is a gap)
      [ ! -e "$HOME/.claude/skills" ] || fail "kind $KIND is instruction-file but rig created ~/.claude/skills"
      grep -q "has no skill-discovery dir" "$OUT/info.log" || { cat "$OUT/info.log"; fail "no instruction-file discovery note for kind $KIND"; }
      [ -f "$INSTR" ] || fail "advisory instruction file $INSTR not written"
      pass "skills: instruction-file kind (5 in ~/.agents/skills, no links, discovery note present; advisory $INSTR written)"
    else
      # native discovery: rig links NOTHING and the plan SAYS so (a silent no-link is a gap)
      [ ! -e "$HOME/.claude/skills" ] || fail "kind $KIND discovers natively but rig created ~/.claude/skills"
      grep -q "harness '$KIND' auto-loads ~/.agents/skills natively" "$OUT/info.log" \\
        || { cat "$OUT/info.log"; fail "no 'auto-loads natively' note for kind $KIND in rig apply info"; }
      pass "skills discoverable natively (5 in ~/.agents/skills, no links, note present)"
    fi

    # ── agent-hook descriptors land in THIS kind's descriptor dir ───────────────────
    case "$KIND" in
      claude-code) HOOKS_DIR="$HOME/.claude/hooks" ;;
      codex)       HOOKS_DIR="$HOME/.codex/hooks" ;;
      opencode)    HOOKS_DIR="$HOME/.config/opencode/hooks" ;;
      omp)         HOOKS_DIR="$HOME/.omp/agent/hooks" ;;
      pi|commandcode) HOOKS_DIR="" ;;
    esac
    desc=""
    if [ -n "$HOOKS_DIR" ]; then
      desc="$(find "$HOOKS_DIR" -name 'block-no-verify*.json' -print -quit 2>/dev/null || true)"
    fi
    if [ -n "$desc" ]; then
      pass "agent-hook descriptors installed in $HOOKS_DIR"
      HOOKS_NOTE="native-dir"
    else
      # where DID they go? (rig without rig-cli#352 falls back to ~/.claude/hooks for omp)
      elsewhere="$(find "$HOME" -name 'block-no-verify*.json' -print -quit 2>/dev/null || true)"
      [ -n "$elsewhere" ] || fail "block-no-verify descriptor not installed anywhere under HOME"
      HOOKS_NOTE="fallback-dir:$(dirname "$elsewhere")"
      printf 'HOOKS-DIR-FALLBACK[%s]: descriptors in %s, not %s\\n' "$KIND" "$(dirname "$elsewhere")" "$HOOKS_DIR"
    fi

    runner="$HOME/.config/git/run-global-hooks"
    [ -x "$runner" ] || fail "global git-hooks dispatcher runner not installed/executable"
    hp="$(git config --global core.hooksPath || true)"
    [ "${hp%/}" = "$HOME/.config/git/hooks" ] || fail "core.hooksPath not wired (got '$hp')"
    [ -f "$REPO/.github/workflows/secret-scan.yml" ] || fail "secret-scan CI workflow not written"
    pass "dispatcher wired + CI workflows written"

    # ── harness orchestrator parity (cc / omp / codex / opencode) ────────────────
    # Staged by the Docker clean-room (#427/#437). All four instruction views must
    # share the same always-on rig-managed block after apply, regardless of kind.
    python3 "$OUT/check_orchestrator.py" "$REPO" \\
      || fail "harness instruction files do not share the orchestrator block"
    pass "claude-code/omp/codex/opencode see the same orchestrator block"

    # ── (b) AUTO-MODE: written OR visible skip — never silent ──────────────────────
    # claude-code/codex/opencode emit harness/<kind> → <settings>. omp's mode key is
    # owned by the permissions approval action (rig-cli#370); the plan note names that.
    if grep -q "harness/$KIND → " "$OUT/info.log"; then
      target="$(grep "harness/$KIND → " "$OUT/info.log" | head -1 | sed 's/.*→ //')"
      [ -f "$target" ] || fail "harness/$KIND action targets $target but it was not written"
      if [ "$KIND" = claude-code ]; then
        python3 "$OUT/check_settings.py" auto_mode "$target" || fail "permissions.defaultMode != 'auto' in $target"
      fi
      printf 'AUTO-MODE[%s]: written (%s)\\n' "$KIND" "$target"
    elif grep -q "harness: $KIND auto-mode" "$OUT/info.log" \\
         && grep -q "permissions approval action" "$OUT/info.log"; then
      printf 'AUTO-MODE[%s]: written-via-approval\\n' "$KIND"
    elif grep -q "auto-mode write skipped" "$OUT/info.log" && grep -q "$KIND" "$OUT/info.log"; then
      printf 'AUTO-MODE[%s]: visible-skip\\n' "$KIND"
    elif grep -q "no auto/permission-mode setting" "$OUT/info.log" && grep -q "$KIND" "$OUT/info.log"; then
      printf 'AUTO-MODE[%s]: visible-skip (n/a)\\n' "$KIND"
    elif grep -q "auto-mode n/a" "$OUT/info.log" && grep -q "$KIND" "$OUT/info.log"; then
      printf 'AUTO-MODE[%s]: visible-skip (n/a)\\n' "$KIND"
    else
      cat "$OUT/info.log"; fail "auto-mode neither written nor visibly noted for kind $KIND (SILENT)"
    fi

    # ── (c) HOOK BRIDGE: registered OR visible skip — never silent ─────────────────
    BRIDGE_TARGET=""
    if grep -q "harness/hook-bridge → " "$OUT/info.log"; then
      BRIDGE_TARGET="$(grep "harness/hook-bridge → " "$OUT/info.log" | head -1 | sed 's/.*→ //')"
      [ -f "$BRIDGE_TARGET" ] || fail "hook-bridge action targets $BRIDGE_TARGET but it was not written"
      case "$KIND" in
        claude-code) python3 "$OUT/check_settings.py" hook_bridge "$BRIDGE_TARGET" "$SRC/lib" \\
                       || fail "cc_hook_bridge hooks not fully wired in $BRIDGE_TARGET" ;;
        codex)       grep -q codex_hook_bridge "$BRIDGE_TARGET" || fail "codex_hook_bridge absent from $BRIDGE_TARGET" ;;
        opencode)    grep -q opencode_hook_bridge "$BRIDGE_TARGET" || fail "opencode_hook_bridge absent from $BRIDGE_TARGET" ;;
        omp)         grep -q omp_hook_bridge "$BRIDGE_TARGET" || fail "omp_hook_bridge absent from $BRIDGE_TARGET" ;;
      esac
      printf 'BRIDGE[%s]: registered (%s)\\n' "$KIND" "$BRIDGE_TARGET"
    elif grep -q "hook_bridge: skipped" "$OUT/info.log"; then
      printf 'BRIDGE[%s]: visible-skip (%s)\\n' "$KIND" "$(grep 'hook_bridge: skipped' "$OUT/info.log" | head -1 | sed 's/^ *//')"
    else
      cat "$OUT/info.log"; fail "hook bridge neither registered nor visibly noted for kind $KIND (SILENT — #337 gap)"
    fi

    # ── idempotent re-apply + clean status ─────────────────────────────────────────
    out2="$(rig apply commit -C "$REPO" --config "$REPO/rig.yaml" 2>&1)" && apply_rc=0 || apply_rc=$?
    [ "$apply_rc" -eq 0 ] || { printf '%s\\n' "$out2" >&2; fail "second apply exited non-zero (rc=$apply_rc)"; }
    summary="$(printf '%s\\n' "$out2" | grep '^Summary:' || true)"
    [ -n "$summary" ] || { printf '%s\\n' "$out2" >&2; fail "second apply printed no Summary: line"; }
    if printf '%s\\n' "$summary" | grep -Eq '(created|updated|backed_up|error|failed)=[1-9]'; then
      printf '%s\\n' "$out2" >&2; fail "second apply was not idempotent: $summary"
    fi
    pass "rig apply is idempotent ($summary)"
    rig status -C "$REPO" --config "$REPO/rig.yaml" >"$OUT/status.log" 2>&1 \\
      || { cat "$OUT/status.log"; fail "rig status exited non-zero after apply"; }
    pass "rig status clean (exit 0)"

    # ── (d) GUARD PARITY through this kind's bridge ────────────────────────────────
    case "$KIND" in
      claude-code) MODULE=cc_hook_bridge ;; codex) MODULE=codex_hook_bridge ;;
      opencode) MODULE=opencode_hook_bridge ;; omp) MODULE=omp_hook_bridge ;;
      pi|commandcode) MODULE="" ;;
    esac
    if [ -z "$MODULE" ]; then
      printf 'PARITY-BLOCKED[%s]: instruction-file harness has no agents-hooks bridge\\n' "$KIND"
    elif [ ! -f "$OUT/parity-enabled" ]; then
      printf 'PARITY-SKIPPED[%s]: no real agent-tools checkout on the host — stub bridges only\\n' "$KIND"
    elif [ ! -f "$SRC/lib/$MODULE/dispatch.py" ] || [ ! -f "$SRC/lib/$MODULE/__main__.py" ]; then
      printf 'PARITY-BLOCKED[%s]: %s not in the agent-tools checkout (%s)\\n' "$KIND" "$MODULE" \\
        "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], "?"))' "$OUT/provenance.json" "$MODULE")"
    elif [ -z "$BRIDGE_TARGET" ]; then
      printf 'PARITY-BLOCKED[%s]: rig did not register the %s bridge\\n' "$KIND" "$MODULE"
    elif [ "$HOOKS_NOTE" != native-dir ]; then
      printf 'PARITY-BLOCKED[%s]: descriptors landed in a %s the %s dispatcher does not read\\n' "$KIND" "$HOOKS_NOTE" "$MODULE"
    else
      rig worktree create -C "$REPO" feature
      FEATURE="$REPO/.worktrees/feature"
      python3 "$OUT/parity.py" "$KIND" "$SRC/lib" "$REPO" "$FEATURE" \\
        || fail "guard parity: worktree-only-writes did not block-on-main/allow-on-feature through $MODULE"
      pass "guard parity: worktree-only-writes blocks on main + allows on a feature worktree via $MODULE"
    fi

    # ── (d, live) the SAME guard through a REAL harness session (omp / opencode) ────────
    # Only meaningful once the bridge is registered AND the descriptors sit where the bridge
    # reads them — otherwise the harness would simply not route through rig's wiring.
    case "$KIND" in
      claude-code|opencode|omp)
        if [ -z "$BRIDGE_TARGET" ] || [ "$HOOKS_NOTE" != native-dir ]; then
          printf 'LIVE-BLOCKED[%s]: rig did not register the bridge / descriptors not in the native dir\\n' "$KIND"
        else
          FEATURE="${FEATURE:-$REPO/.worktrees/feature}"
          [ -d "$FEATURE" ] || rig worktree create -C "$REPO" feature
          python3 "$OUT/live.py" "$KIND" "$REPO" "$FEATURE" \\
            || fail "live parity: a real $KIND session did not block-on-main / allow-on-feature"
        fi
        ;;
      *)
        # live.py prints this kind's labelled LIVE-BLOCKED/LIVE-SKIPPED line and its reason
        python3 "$OUT/live.py" "$KIND" "$REPO" "$REPO"
        ;;
    esac

    printf 'CLEANROOM-ALL-GREEN kind=%s\\n' "$KIND"
    """
)

_RESULT_MARKERS = ("AUTO-MODE[", "BRIDGE[", "PARITY", "HOOKS-DIR-FALLBACK[", "LIVE-")


# ── the LIVE leg: a REAL harness session against a canned local model ───────────────────────
# A synthetic bridge event proves the dispatcher + hook; it does not prove the HARNESS actually
# routes its tool call through the bridge rig registered. The live leg does: a tiny in-container
# OpenAI-compatible provider (chat/completions, SSE) answers every first turn with ONE canned
# `write` tool call and every second turn with "done"; the real harness binary runs a print-mode
# session against it in the main repo (expect: write blocked, no file) and in a feature worktree
# (expect: file written). Harness binaries are baked in only with `--build-arg RIG_E2E_LIVE=1`
# (`RIG_CONTAINER_E2E_LIVE=1` on the pytest side) — ~350MB of downloads the base matrix does not
# need. Kinds without an installable/offline-drivable harness record LIVE-SKIPPED/LIVE-BLOCKED
# with the reason, never a synthetic result dressed up as live.
_STUB_PROVIDER_PY = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    \"\"\"A canned local model provider for the live leg.

    Speaks BOTH wire formats the four harnesses use, on one port:
      * OpenAI chat/completions (streaming + non-streaming) — opencode, omp
      * Anthropic Messages, SSE — Claude Code
    Turn 1 answers with ONE tool call that writes ``STUB_TARGET_FILE``; turn 2 says "done".
    ``STUB_TOOL`` picks which tool name/arguments shape to emit.
    \"\"\"
    import json, os, sys, time, uuid
    from http.server import BaseHTTPRequestHandler, HTTPServer

    PORT = int(os.environ["STUB_PORT"])
    MODEL = "stub-1"
    TARGET = os.environ["STUB_TARGET_FILE"]
    TOOL = os.environ.get("STUB_TOOL", "write")
    # every key any of the four harnesses uses for the destination path
    ARGS = json.dumps({"path": TARGET, "filePath": TARGET, "file_path": TARGET,
                       "content": "parity\\n"})


    def _openai_second_turn(messages):
        return any(isinstance(m, dict) and (m.get("role") == "tool" or m.get("tool_calls"))
                   for m in messages)


    def _anthropic_second_turn(messages):
        for m in messages:
            content = m.get("content") if isinstance(m, dict) else None
            if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            ):
                return True
        return False


    def _openai_body(second, cid, now):
        if second:
            msg, fin = {"role": "assistant", "content": "done"}, "stop"
        else:
            msg = {"role": "assistant", "content": None, "tool_calls": [
                {"id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
                 "function": {"name": TOOL, "arguments": ARGS}}]}
            fin = "tool_calls"
        return {"id": cid, "object": "chat.completion", "created": now, "model": MODEL,
                "choices": [{"index": 0, "message": msg, "finish_reason": fin}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


    def _openai_deltas(second):
        if second:
            return [({"role": "assistant", "content": ""}, None), ({"content": "done"}, None),
                    ({}, "stop")]
        call_id = f"call_{uuid.uuid4().hex[:8]}"
        return [({"role": "assistant", "tool_calls": [
                    {"index": 0, "id": call_id, "type": "function",
                     "function": {"name": TOOL, "arguments": ""}}]}, None),
                ({"tool_calls": [{"index": 0, "function": {"arguments": ARGS}}]}, None),
                ({}, "tool_calls")]


    def _anthropic_events(second):
        \"\"\"The minimal Messages-API SSE sequence for one tool_use (or a plain text turn).\"\"\"
        mid = f"msg_{uuid.uuid4().hex[:12]}"
        start = {"type": "message_start", "message": {
            "id": mid, "type": "message", "role": "assistant", "content": [], "model": MODEL,
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1}}}
        if second:
            body = [
                {"type": "content_block_start", "index": 0,
                 "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0,
                 "delta": {"type": "text_delta", "text": "done"}},
                {"type": "content_block_stop", "index": 0},
            ]
            stop = "end_turn"
        else:
            body = [
                {"type": "content_block_start", "index": 0,
                 "content_block": {"type": "tool_use", "id": f"toolu_{uuid.uuid4().hex[:12]}",
                                   "name": TOOL, "input": {}}},
                {"type": "content_block_delta", "index": 0,
                 "delta": {"type": "input_json_delta", "partial_json": ARGS}},
                {"type": "content_block_stop", "index": 0},
            ]
            stop = "tool_use"
        return [start, *body,
                {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                 "usage": {"output_tokens": 1}},
                {"type": "message_stop"}]


    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            sys.stderr.write("stub-provider: " + (fmt % args) + "\\n")

        def _json(self, obj, status=200):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _sse(self, events, *, named):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            for ev in events:
                if named:
                    self.wfile.write(f"event: {ev['type']}\\n".encode())
                self.wfile.write(f"data: {json.dumps(ev)}\\n\\n".encode())
            if not named:
                self.wfile.write(b"data: [DONE]\\n\\n")
            self.wfile.flush()
            self.close_connection = True

        def do_GET(self):
            self._json({"object": "list",
                        "data": [{"id": MODEL, "object": "model", "owned_by": "stub"}]})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            path = self.path.split("?")[0].rstrip("/")
            messages = req.get("messages") or []
            sys.stderr.write(f"stub-provider: POST {path} msgs={len(messages)}\\n")
            if path.endswith("/count_tokens"):
                return self._json({"input_tokens": 1})
            if path.endswith("/messages"):
                return self._sse(_anthropic_events(_anthropic_second_turn(messages)), named=True)
            if not path.endswith("/chat/completions"):
                return self._json({"error": f"unsupported path {path}"}, 404)
            second = _openai_second_turn(messages)
            cid, now = f"chatcmpl-{uuid.uuid4().hex[:12]}", int(time.time())
            if not req.get("stream"):
                return self._json(_openai_body(second, cid, now))
            chunks = [{"id": cid, "object": "chat.completion.chunk", "created": now,
                       "model": MODEL,
                       "choices": [{"index": 0, "delta": d, "finish_reason": f}]}
                      for d, f in _openai_deltas(second)]
            self._sse(chunks, named=False)


    HTTPServer(("127.0.0.1", PORT), H).serve_forever()
    """
)

_LIVE_PY = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    \"\"\"Drive a REAL harness session against the stub provider; print the LIVE-* result line.

    usage: live.py <kind> <main_repo> <feature_repo>

    The contrast is always the same: the SAME canned tool call, once in the repo's primary
    checkout (on the default branch — must be BLOCKED by rig's worktree-only-writes guard,
    reaching the hook through the bridge rig registered in that harness's own config) and once
    in a feature worktree (must be ALLOWED). A kind that cannot be driven this way records a
    labelled LIVE-BLOCKED/LIVE-SKIPPED line with the reason — never a synthetic stand-in.
    \"\"\"
    import json, os, shutil, socket, subprocess, sys, time
    from pathlib import Path

    HOME = Path.home()
    BLOCK_TEXT = "worktree-only workflow forbids"
    TOOL = {"claude-code": "Write", "opencode": "write", "omp": "write"}
    NOT_LIVE = {
        "codex": "LIVE-BLOCKED: codex 0.153.4 exposes no apply_patch function tool "
                 "(feature apply_patch_freeform is removed) — edits flow through exec_command, "
                 "which codex_hook_bridge maps to pre-bash, so the pre-write worktree-only guard "
                 "cannot fire in a live codex session; covered by the synthetic bridge event only",
        "pi": "LIVE-BLOCKED: pi is an instruction-file harness with no agents-hooks bridge "
              "and no live session driver in this matrix — covered by the visible auto-mode n/a and hook_bridge skip notes",
        "commandcode": "LIVE-BLOCKED: commandcode is an instruction-file harness with no agents-hooks bridge "
              "and no live session driver in this matrix — covered by the visible auto-mode n/a and hook_bridge skip notes",
    }


    def free_port():
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port


    def merge_json(path, extra):
        data = json.loads(path.read_text()) if path.is_file() else {}
        data.update(extra)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))


    def stage_opencode_deps(repo):
        \"\"\"Copy the build-time `@opencode-ai/plugin` tree into both plugin dirs.

        opencode npm-installs that dependency at plugin-load time; offline the fetch fails and
        the plugin — rig's whole bridge — is silently skipped. See _INSTALL_OPENCODE_DEPS.\"\"\"
        src = Path(os.environ.get("RIG_E2E_OPENCODE_DEPS", "/opt/opencode-deps"))
        if not (src / "node_modules").is_dir():
            return
        for dest in (HOME / ".config" / "opencode", Path(repo) / ".opencode"):
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / "package.json", dest / "package.json")
            if not (dest / "node_modules").exists():
                shutil.copytree(src / "node_modules", dest / "node_modules", symlinks=True)


    def configure_omp(base):
        import yaml  # rig's own runtime dep, present in the image

        agent = HOME / ".omp" / "agent"
        agent.mkdir(parents=True, exist_ok=True)
        cfg = agent / "config.yml"
        data = yaml.safe_load(cfg.read_text()) if cfg.is_file() else {}
        data = data if isinstance(data, dict) else {}
        data.setdefault("setupVersion", 2)
        data.setdefault("modelRoles", {})["default"] = "stub/stub-1"
        cfg.write_text(yaml.safe_dump(data))
        (agent / "models.yml").write_text(yaml.safe_dump({"providers": {"stub": {
            "baseUrl": base, "apiKey": "STUB_KEY_LITERAL", "api": "openai-completions",
            "models": [{"id": "stub-1", "name": "Stub", "api": "openai-completions",
                        "reasoning": False, "input": ["text"], "contextWindow": 32000,
                        "maxTokens": 4000,
                        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}]}}}))


    def configure(kind, port, repos):
        base = f"http://127.0.0.1:{port}/v1"
        if kind == "omp":
            configure_omp(base)
        elif kind == "opencode":
            merge_json(HOME / ".config" / "opencode" / "opencode.json", {
                "provider": {"stub": {"npm": "@ai-sdk/openai-compatible", "name": "Stub",
                                      "options": {"baseURL": base, "apiKey": "stub"},
                                      "models": {"stub-1": {"name": "Stub 1", "tool_call": True}}}},
                "model": "stub/stub-1",
                "permission": {"edit": "allow", "bash": "allow", "external_directory": "allow"}})
            for repo in repos:
                stage_opencode_deps(repo)
        elif kind == "claude-code":
            # rig registers the CC bridge in the PROJECT settings of the repo it applied to; the
            # feature worktree is a separate working directory rig never saw, so carry the same
            # rig-written settings across — otherwise the "allow" leg would only prove the hook
            # is ABSENT there, which proves nothing about the guard.
            src = Path(repos[0]) / ".claude" / "settings.json"
            for repo in repos[1:]:
                dest = Path(repo) / ".claude" / "settings.json"
                dest.parent.mkdir(parents=True, exist_ok=True)
                if src.is_file():
                    shutil.copy2(src, dest)


    def session_env(kind, port):
        env = {**os.environ, "OMP_SKIP_SETUP": "1", "PI_NO_TITLE": "1", "PI_NO_PTY": "1"}
        if kind == "claude-code":
            env.update({
                "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
                "ANTHROPIC_API_KEY": "stub-key-not-a-real-credential",
                "ANTHROPIC_MODEL": "stub-1",
                # keep the CLI from reaching for the network it does not have
                "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1",
                "DISABLE_ERROR_REPORTING": "1", "DISABLE_BUG_COMMAND": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            })
        return env


    def session_cmd(kind, repo):
        if kind == "omp":
            return ["omp", "-p", "--mode", "json", "--no-session", "--no-skills",
                    "--model", "stub/stub-1", "--cwd", repo, "--approval-mode", "yolo",
                    "create parity.txt"]
        if kind == "opencode":
            return ["opencode", "run", "--model", "stub/stub-1", "create parity.txt"]
        return ["claude", "-p", "create parity.txt", "--model", "stub-1",
                "--permission-mode", "bypassPermissions", "--allowedTools", "Write"]


    def session(kind, repo, port):
        proc = subprocess.run(session_cmd(kind, repo), cwd=repo, env=session_env(kind, port),
                              stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=300)
        return proc.stdout + proc.stderr


    def run_leg(kind, repo, port):
        \"\"\"One session against a fresh stub bound to THIS repo's parity.txt.\"\"\"
        target = Path(repo) / "parity.txt"
        target.unlink(missing_ok=True)
        stub = subprocess.Popen(
            [sys.executable, "/opt/cleanroom/stub_provider.py"],
            env={**os.environ, "STUB_PORT": str(port), "STUB_TARGET_FILE": str(target),
                 "STUB_TOOL": TOOL[kind]},
            stderr=subprocess.DEVNULL)
        try:
            time.sleep(1)
            out = session(kind, repo, port)
        finally:
            stub.terminate()
            stub.wait(timeout=10)
        return {"written": target.is_file(), "block_text": BLOCK_TEXT in out}, out


    def binary_for(kind):
        return {"omp": "omp", "opencode": "opencode", "claude-code": "claude"}[kind]


    def main():
        kind, main_repo, feature_repo = sys.argv[1:4]
        if kind in NOT_LIVE:
            label, detail = NOT_LIVE[kind].split(":", 1)
            print(f"{label}[{kind}]:{detail}")
            return 0
        binary = binary_for(kind)
        if shutil.which(binary) is None:
            print(f"LIVE-SKIPPED[{kind}]: {binary} not installed in the image "
                  f"(build with RIG_E2E_LIVE=1)")
            return 0
        port = free_port()
        configure(kind, port, [main_repo, feature_repo])
        results, outputs = {}, {}
        for label, repo in (("main", main_repo), ("feature", feature_repo)):
            results[label], outputs[label] = run_leg(kind, repo, port)
            print(f"LIVE-DETAIL[{kind}]: {label}: {results[label]}")
        ok = (results["main"]["block_text"] and not results["main"]["written"]
              and results["feature"]["written"] and not results["feature"]["block_text"])
        if ok:
            print(f"LIVE-PARITY[{kind}]: real {binary} session — "
                  f"block-on-main=yes allow-on-feature=yes")
            return 0
        for label, out in outputs.items():  # the harness's own output is the only diagnostic
            tail = "\\n".join(out.splitlines()[-40:])
            print(f"--- {kind} session output ({label}, last 40 lines) ---\\n{tail}",
                  file=sys.stderr)
        print(f"LIVE-FAIL[{kind}]: {results}", file=sys.stderr)
        return 1


    if __name__ == "__main__":
        raise SystemExit(main())
    """
)

# Appended to the Docker clean-room's Dockerfile right before `USER newuser`: the real harness
# binaries for the live leg, only when RIG_E2E_LIVE=1 (~400MB of downloads the base matrix does
# not need). Every version is pinned; the image BUILD may use the network, the RUN never does.
# omp is the pinned GitHub release binary (same version as the dev machine's brew install, sha256
# verified); opencode is the standalone linux-arm64 binary from its npm platform package; Claude
# Code is its npm package. All arm64, like the Apple container VM.
_OMP_VERSION = "18.0.11"
_OMP_SHA256 = "e5f77cb65aa2dc777a8a5932be3b2e6a44271c8df2eb209cea6f04f212f3f010"
_OPENCODE_VERSION = "1.18.29"
_CLAUDE_CODE_VERSION = "2.1.263"
# Where the build pre-warms opencode's plugin runtime dependency; see _OPENCODE_DEPS below.
_OPENCODE_DEPS_DIR = "/opt/opencode-deps"

_INSTALL_OMP = f"""\
      curl -fsSL -o /opt/harness/bin/omp \\
        https://github.com/can1357/oh-my-pi/releases/download/v{_OMP_VERSION}/omp-linux-arm64; \\
      echo "{_OMP_SHA256}  /opt/harness/bin/omp" | sha256sum -c -; chmod +x /opt/harness/bin/omp; \\
"""

_INSTALL_OPENCODE = f"""\
      curl -fsSL -o /tmp/oc.tgz \\
        https://registry.npmjs.org/opencode-linux-arm64/-/opencode-linux-arm64-{_OPENCODE_VERSION}.tgz; \\
      mkdir -p /tmp/oc && tar -xzf /tmp/oc.tgz -C /tmp/oc; \\
      install -m 755 "$(find /tmp/oc -type f -name opencode | head -1)" /opt/harness/bin/opencode; \\
      rm -rf /tmp/oc /tmp/oc.tgz; \\
"""

# opencode resolves a plugin by first `npm install`ing `@opencode-ai/plugin` into the plugin
# directory's parent — at RUN time, from the network. Offline that fetch fails (`ECONNREFUSED
# registry.npmjs.org`) and the plugin is silently NOT loaded, so rig's bridge never fires and a
# live session writes straight through (verified live: the identical session BLOCKS when the
# container has egress). The build therefore pre-installs that dependency tree once; live.py
# copies it into both plugin dirs before the offline session. This is a harness runtime cache,
# not rig state — the clean-room's "brand-new user" assertions cover rig's own targets.
_INSTALL_OPENCODE_DEPS = f"""\
      mkdir -p {_OPENCODE_DEPS_DIR}; \\
      printf '{{"dependencies":{{"@opencode-ai/plugin":"{_OPENCODE_VERSION}"}}}}' \\
        > {_OPENCODE_DEPS_DIR}/package.json; \\
      npm install --prefix {_OPENCODE_DEPS_DIR} --no-audit --no-fund; \\
"""

_INSTALL_CLAUDE_CODE = f"""\
      npm install -g --no-audit --no-fund @anthropic-ai/claude-code@{_CLAUDE_CODE_VERSION}; \\
"""

_HARNESS_INSTALL = textwrap.dedent(
    f"""\
    ARG RIG_E2E_LIVE=0
    RUN if [ "$RIG_E2E_LIVE" = "1" ]; then set -eux; \\
          apt-get update \\
          && apt-get install -y --no-install-recommends curl libstdc++6 nodejs npm \\
          && rm -rf /var/lib/apt/lists/*; \\
          mkdir -p /opt/harness/bin; \\
    {_INSTALL_OMP}{_INSTALL_OPENCODE}{_INSTALL_OPENCODE_DEPS}{_INSTALL_CLAUDE_CODE}\
        fi
    ENV PATH=/opt/harness/bin:$PATH
    ENV RIG_E2E_OPENCODE_DEPS={_OPENCODE_DEPS_DIR}
    """
)


def _dockerfile() -> str:
    """The Docker clean-room's Dockerfile with the optional harness-install block spliced in.

    Spliced BEFORE the first ``COPY`` of build-context content, so the ~330MB harness download
    layer is cached across runs — every run re-stages the context (new rig source / scripts), and
    a layer placed after those COPYs would re-download on each rebuild."""
    marker = "COPY rig-cli /opt/rig-cli\n"
    assert marker in cr._DOCKERFILE, "test_cleanroom_e2e._DOCKERFILE lost its rig-cli COPY line"
    return cr._DOCKERFILE.replace(marker, _HARNESS_INSTALL + "\n" + marker, 1)


def _stage_build_context(ctx: Path) -> dict[str, str]:
    """The Docker clean-room's context (rig source + fake agent-tools + Dockerfile), with the
    matrix config/assert/parity overlaid and the real bridge pieces vendored when available."""
    # Dev knob: RIG_CONTAINER_E2E_RIG_SOURCE points the image at ANOTHER rig checkout (e.g. a
    # worktree carrying an unmerged bridge registration) instead of this one. Unset = this repo.
    override = os.environ.get("RIG_CONTAINER_E2E_RIG_SOURCE", "").strip()
    saved_root = cr.REPO_ROOT
    cr.REPO_ROOT = Path(override).resolve() if override else saved_root
    try:
        cr._stage_build_context(ctx)
    finally:
        cr.REPO_ROOT = saved_root
    _ensure_stub_bridges(ctx / "agent-tools")
    provenance = _vendor_real_pieces(ctx / "agent-tools")
    provenance["rig-source"] = str(cr.REPO_ROOT if not override else Path(override).resolve())
    cleanroom = ctx / "cleanroom"
    cr._write(cleanroom / "rig.yaml", _MATRIX_CONFIG)
    cr._write(cleanroom / "assert.sh", _ASSERT_SCRIPT, executable=True)
    cr._write(cleanroom / "parity.py", _PARITY_PY, executable=True)
    cr._write(cleanroom / "stub_provider.py", _STUB_PROVIDER_PY, executable=True)
    cr._write(cleanroom / "live.py", _LIVE_PY, executable=True)
    cr._write(ctx / "Dockerfile", _dockerfile())
    cr._write(cleanroom / "provenance.json", json.dumps(provenance, indent=2))
    if "*" not in provenance:
        cr._write(cleanroom / "parity-enabled", "1\n")
    return provenance


def _cleanup(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        pass


@pytest.fixture(scope="module")
def matrix_image(tmp_path_factory: pytest.TempPathFactory):
    """Build the ONE shared image + the internal (host-only) network; tear both down after."""
    if not _container_available():
        pytest.skip("Apple `container` CLI / apiserver not available")
    free = _free_disk_gb(Path.home())
    if free < MIN_FREE_DISK_GB:
        pytest.skip(f"only {free:.1f} GB free on the boot volume (need {MIN_FREE_DISK_GB}); not building")
    c = _container()
    suffix = uuid.uuid4().hex[:12]
    # The build context MUST live under $HOME: the Apple builder shim shares the home volume with
    # the build VM but NOT the tmp volume — a context under /var/folders (pytest's tmp_path) is
    # transferred as EMPTY ("transferring context: 2B") and every COPY fails "not found"
    # (verified live, container 1.0.0). RIG_CONTAINER_E2E_CTX_ROOT overrides the parent dir.
    ctx_root = Path(os.environ.get("RIG_CONTAINER_E2E_CTX_ROOT") or Path.home() / ".cache" / "rig-container-e2e")
    ctx = ctx_root / suffix
    ctx.mkdir(parents=True, exist_ok=False)
    provenance = _stage_build_context(ctx)
    tag = f"rig-cleanroom-matrix:{suffix}"
    net = f"rig-e2e-iso-{suffix}"
    try:
        live = os.environ.get("RIG_CONTAINER_E2E_LIVE", "").strip().lower() in ("1", "true", "yes")
        build = cr._run(
            [c, "build", "-t", tag, "--build-arg", f"RIG_E2E_LIVE={'1' if live else '0'}", str(ctx)],
            timeout=1800,
        )
        assert build.returncode == 0, f"container build failed:\nSTDOUT:\n{build.stdout}\nSTDERR:\n{build.stderr}"
        mk = cr._run([c, "network", "create", "--internal", net], timeout=120)
        assert mk.returncode == 0, f"container network create --internal failed:\n{mk.stdout}\n{mk.stderr}"
        # belt and braces with the in-container egress probe: the network the runs attach to
        # must be the host-only mode the CLI reports for `--internal` (verified: "hostOnly").
        insp = cr._run([c, "network", "inspect", net], timeout=60)
        assert insp.returncode == 0, f"container network inspect failed:\n{insp.stdout}\n{insp.stderr}"
        modes = [n.get("configuration", {}).get("mode") for n in json.loads(insp.stdout or "[]")]
        assert modes == ["hostOnly"], f"internal network {net} is not host-only: modes={modes}"
        yield {"tag": tag, "network": net, "provenance": provenance}
    finally:
        _cleanup([c, "image", "rm", tag])
        _cleanup([c, "network", "rm", net])
        shutil.rmtree(ctx, ignore_errors=True)


def _result_lines(stdout: str) -> list[str]:
    return [ln for ln in stdout.splitlines() if ln.startswith(_RESULT_MARKERS)]


@pytest.mark.parametrize("kind", _selected_kinds())
def test_cleanroom_matrix_kind(matrix_image: dict, kind: str) -> None:
    """Run the clean-room for ONE harness kind on the internal network; assert all-green."""
    c = _container()
    # a fixed --name so a run that outlives its timeout can still be force-removed (`--rm` only
    # fires on a normal exit; a leaked container would also pin the image + network in teardown).
    name = f"{matrix_image['network']}-{kind}"
    try:
        run = cr._run(
            [c, "run", "--rm", "--name", name, "--network", matrix_image["network"],
             "-e", f"RIG_E2E_KIND={kind}", matrix_image["tag"]],
            timeout=900,
        )
    finally:
        _cleanup([c, "rm", "-f", name])
    combined = f"STDOUT:\n{run.stdout}\nSTDERR:\n{run.stderr}"
    # the per-kind result table, visible with `-s` / `-rA` and in the failure message
    print(f"\n[{kind}] " + "\n[{0}] ".format(kind).join(_result_lines(run.stdout) or ["(no result lines)"]))
    assert run.returncode == 0, f"clean-room run failed for kind={kind} (exit {run.returncode}):\n{combined}"
    assert f"CLEANROOM-ALL-GREEN kind={kind}" in run.stdout, f"missing all-green sentinel:\n{combined}"
