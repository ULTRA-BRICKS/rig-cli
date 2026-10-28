"""``rig activate`` — turn on everything this machine can run, and say plainly what it cannot.

``rig apply`` reconciles the config you wrote; ``rig init`` / ``rig setup`` write a config. A new
machine needs both at once, non-interactively: the same skills, hooks, tmux, omp and sysmon setup
as the machine the profile came from, switched on wherever THIS machine can run it. ``rig
activate`` is that command. It is preview-first like ``rig apply``: bare = a read-only preview,
``rig activate commit`` executes.

What it does, in order (commit):

1. ``rig profile sync`` — when ``profile.repo`` is configured (pulls the shared config and the
   tmux / omp / sysmon files; a conflict warns and the local files are used as they are).
2. Probes the machine (:func:`build_activation`): which harnesses are installed, whether ``tmux``,
   ``abduco``, ``git`` and a service manager (systemd ``--user`` / launchd) exist, and whether a pinned
   ``cccc`` analyzer exists for this OS/CPU (the ``lint_checks`` area: analyzer + commit hook).
3. Writes the ``enabled`` switches the config does not mention yet (a missing ``harness``,
   ``tmux``, ``harness_wrappers`` block; the first-class ``ctmux`` tool) into the GLOBAL config,
   so the decision is visible and travels with the profile. It NEVER overrides a key the config
   already has — an explicit ``enabled: false`` stays false.
4. Builds the usual plan from the global config alone (no repo layer: this is a machine-level
   command), drops the areas the probes ruled out, and runs it through the SAME
   :func:`riglib.actions.run_plan` as ``rig apply commit``. Idempotent — a second run changes nothing.

Areas the machine cannot support are listed with the reason and excluded; they never turn the
exit code non-zero. Stdlib-only; effects (config write, plan execution) are the caller's.
"""

from __future__ import annotations

import copy
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# harness kind -> (binary, config dir) used to detect an installed harness
HARNESSES: tuple[tuple[str, str, str], ...] = (
    ("omp", "omp", "~/.omp"),
    ("claude-code", "claude", "~/.claude"),
    ("codex", "codex", "~/.codex"),
    ("opencode", "opencode", "~/.config/opencode"),
)

FIRST_CLASS_TOOLS = ("ctmux",)

# the areas `rig activate --only` accepts, in table order
AREA_IDS = (
    "skills", "agent_hooks", "git_hooks", "lint_checks", "harness", "tmux", "harness_wrappers", "sysmon", "models",
    "tg_ctl", "tcc_watch", "tgctl_reaper", "spotlight", "tools", "global",
)

ACTIVATE = "activate"
UNSUPPORTED = "unsupported"
LEFT_ALONE = "left alone"


def _cccc_unsupported() -> str | None:
    """Why no pinned ``cccc`` analyzer exists for this OS/CPU, or ``None`` when one does."""
    from . import cccc_install

    try:
        cccc_install.pin_for()
    except cccc_install.UnsupportedPlatform as exc:
        return str(exc)
    return None


def _systemd_user() -> bool:
    if shutil.which("systemctl") is None:
        return False
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    uid = getattr(os, "getuid", lambda: -1)()
    return bool(runtime) or Path(f"/run/user/{uid}").is_dir()


@dataclass
class Probe:
    """What the machine offers. Every field is a seam: tests replace them, nothing here mutates."""

    platform: str = field(default_factory=lambda: sys.platform)
    which: Callable[[str], str | None] = shutil.which
    exists: Callable[[str], bool] = lambda path: Path(os.path.expanduser(path)).exists()
    systemd_user: Callable[[], bool] = _systemd_user
    cccc_unsupported: Callable[[], str | None] = _cccc_unsupported

    @property
    def darwin(self) -> bool:
        return self.platform == "darwin"

    def has_service_manager(self) -> bool:
        return self.darwin or (self.platform.startswith("linux") and self.systemd_user())

    def detected_harnesses(self) -> list[str]:
        from .problem_reporting import only_reporting_policy

        return [
            kind for kind, binary, home in HARNESSES
            if self.which(binary) or (self.exists(home) and not only_reporting_policy(kind, home))
        ]


@dataclass
class Area:
    id: str
    title: str
    status: str  # activate | unsupported | left alone
    detail: str
    categories: frozenset[str]

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "status": self.status, "detail": self.detail}


@dataclass
class Activation:
    """The outcome of probing: per-area verdicts, the config keys to write, and what to run."""

    areas: list[Area]
    config_changes: list[str]
    doc: dict[str, Any]

    @property
    def included(self) -> frozenset[str]:
        out: set[str] = set()
        for area in self.areas:
            if area.status == ACTIVATE:
                out |= area.categories
        return frozenset(out)

    @property
    def unsupported(self) -> list[Area]:
        return [a for a in self.areas if a.status == UNSUPPORTED]

    def to_dict(self) -> dict[str, Any]:
        return {
            "areas": [a.to_dict() for a in self.areas],
            "config_changes": list(self.config_changes),
            "unsupported": [a.id for a in self.unsupported],
        }


def _set_if_absent(doc: dict[str, Any], dotted: str, value: Any, changes: list[str]) -> bool:
    parts = dotted.split(".")
    node = doc
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            if part in node:
                return False
            nxt = node[part] = {}
        node = nxt
    if parts[-1] in node:
        return False
    node[parts[-1]] = value
    shown = "{}" if value == {} else str(value).lower() if isinstance(value, bool) else str(value)
    changes.append(f"{dotted}: {shown}")
    return True


def build_activation(
    global_doc: dict[str, Any], probe: Probe | None = None, only: frozenset[str] | None = None
) -> Activation:
    """Probe the machine against ``global_doc`` (the GLOBAL config as a mapping; ``{}`` when absent).

    ``only`` limits the run to those area ids (:data:`AREA_IDS`): the others are neither listed, nor
    switched on in the config, nor planned.
    """
    probe = probe or Probe()
    doc = copy.deepcopy(global_doc) if global_doc else {"version": 1}
    doc.setdefault("version", 1)
    changes: list[str] = []
    areas: list[Area] = []

    def want(area_id: str) -> bool:
        return only is None or area_id in only

    def add(area_id: str, title: str, status: str, detail: str, *categories: str) -> None:
        areas.append(Area(area_id, title, status, detail, frozenset(categories or (area_id,))))

    if want("skills"):
        add("skills", "agent skills", ACTIVATE, "installed from the agent-tools checkout")
    if want("agent_hooks"):
        add("agent_hooks", "agent hooks", ACTIVATE,
            "descriptors for every configured harness (omp: ~/.omp/agent/hooks) + the hook bridges")

    if want("git_hooks"):
        _git_hooks(probe, add)

    if want("lint_checks"):
        _lint_checks(doc, probe, add)

    if want("harness"):
        _harness(doc, probe, changes, add)
    if want("tmux"):
        _tmux(doc, probe, changes, add)
    if want("harness_wrappers"):
        _harness_wrappers(doc, probe, changes, add)

    _service_areas(probe, want, add)

    if want("tools"):
        _tools(doc, probe, changes, add)
    if want("global"):
        add("global", "env, git config, gitignore, fj, bun", ACTIVATE, "as declared in the config",
            "env", "git", "gitignore", "fj", "bun", "mode")
    return Activation(areas, changes, doc)


def _git_hooks(probe: Probe, add: Callable[..., None]) -> None:
    if probe.which("git"):
        add("git_hooks", "global git-hook dispatcher", ACTIVATE, "secret-scan and friends in every repo")
    else:
        add("git_hooks", "global git-hook dispatcher", UNSUPPORTED, "git is not installed")


def _tmux(doc: dict[str, Any], probe: Probe, changes: list[str], add: Callable[..., None]) -> None:
    if not probe.which("tmux"):
        add("tmux", "tmux config + plugins", UNSUPPORTED, "tmux is not installed")
    elif "tmux" in doc:
        add("tmux", "tmux config + plugins", ACTIVATE, "as declared in the config")
    else:
        _set_if_absent(doc, "tmux.enabled", True, changes)
        add("tmux", "tmux config + plugins", ACTIVATE, "tmux found; rig-managed config + resurrect/continuum")


def _harness_wrappers(doc: dict[str, Any], probe: Probe, changes: list[str], add: Callable[..., None]) -> None:
    title = "harness launcher wrappers"
    if not probe.which("abduco"):
        add("harness_wrappers", title, UNSUPPORTED, "abduco is not installed (brew install abduco / apt install abduco)")
    elif "harness_wrappers" in doc:
        add("harness_wrappers", title, ACTIVATE, "as declared in the config")
    else:
        _set_if_absent(doc, "harness_wrappers.enabled", True, changes)
        add("harness_wrappers", title, ACTIVATE, "abduco found; crash-supervised hosting for the harnesses")


def _lint_checks(doc: dict[str, Any], probe: Probe, add: Callable[..., None]) -> None:
    """The machine-wide ``rig lint check`` artifacts: the pinned analyzer and the pre-commit hook."""
    title = "lint check (cccc analyzer + pre-commit hook)"
    block = doc.get("lint_checks")
    if isinstance(block, dict) and block.get("enabled") is False:
        add("lint_checks", title, LEFT_ALONE, "lint_checks.enabled is false in the config — not touched",
            "lint_checks_machine")
        return
    reason = probe.cccc_unsupported()
    if reason is not None:
        add("lint_checks", title, UNSUPPORTED, reason, "lint_checks_machine")
        return
    add("lint_checks", title, ACTIVATE, "pinned, digest-verified cccc + the commit gate in every repo", "lint_checks_machine")


def _area_verdict(supported: bool, detail: str, why_not: str) -> tuple[str, str]:
    return (ACTIVATE, detail) if supported else (UNSUPPORTED, why_not)


def _service_areas(probe: Probe, want: Callable[[str], bool], add: Callable[..., None]) -> None:
    """The areas that need a service manager (systemd --user / launchd), and the macOS-only ones."""
    why = "no service manager (needs systemd --user on Linux, launchd on macOS)"
    service = probe.has_service_manager()
    for area_id, title, detail in (("sysmon", "resource monitor", "resident daemon"),
                                   ("models", "model-freshness schedule", "timer")):
        if want(area_id):
            add(area_id, title, *_area_verdict(service, detail, why))
    mac = _area_verdict(probe.darwin, "launchd agent", "macOS only (launchd / Spotlight)")
    for area_id, title in (("tg_ctl", "tg-ctl inbound daemon"), ("tcc_watch", "TCC watch"),
                           ("tgctl_reaper", "tg-ctl reaper"), ("spotlight", "Spotlight exclusions")):
        if want(area_id):
            add(area_id, title, *mac)


def _harness(doc: dict[str, Any], probe: Probe, changes: list[str], add: Callable[..., None]) -> None:
    title = "harness config (auto-mode, permissions, global instructions, omp config, bridges)"
    if "harness" in doc:
        add("harness", title, ACTIVATE, "as declared in the config", "harness", "permissions", "instructions")
        return
    found = probe.detected_harnesses()
    if not found:
        add("harness", "global agent instructions (default Claude)", ACTIVATE,
            "no coding-agent harness found; prepare default reporting instructions only (loaded when the client is installed)",
            "instructions")
        return
    block: dict[str, Any] = {"enabled": True, "kind": found[0], "auto_mode": True}
    if found[1:]:
        block["kinds"] = found[1:]
    doc["harness"] = block
    changes.append(f"harness: kind {found[0]}" + (f", kinds {', '.join(found[1:])}" if found[1:] else ""))
    add("harness", title, ACTIVATE, f"detected: {', '.join(found)}", "harness", "permissions", "instructions")


def _tools(doc: dict[str, Any], probe: Probe, changes: list[str], add: Callable[..., None]) -> None:
    tools = doc.get("tools")
    if not probe.which("git"):
        add("tools", "CLI ecosystem", UNSUPPORTED, "git is not installed (tools are cloned and installed)")
    elif tools is None:
        _set_if_absent(doc, "tools.enabled", True, changes)
        for name in FIRST_CLASS_TOOLS:
            _set_if_absent(doc, f"tools.items.{name}", {}, changes)
        add("tools", "CLI ecosystem", ACTIVATE, f"first-class tools: {', '.join(FIRST_CLASS_TOOLS)}")
    elif isinstance(tools, dict) and tools.get("enabled"):
        added = [n for n in FIRST_CLASS_TOOLS if _set_if_absent(doc, f"tools.items.{n}", {}, changes)]
        add("tools", "CLI ecosystem", ACTIVATE, "declared tools" + (f" + {', '.join(added)}" if added else ""))
    else:
        add("tools", "CLI ecosystem", LEFT_ALONE,
            "tools.enabled is not true in the config — not touched (set it to true to install ctmux and friends)")


def render(activation: Activation, *, preview: bool) -> list[str]:
    """Human-readable lines of the area table and the config changes."""
    lines: list[str] = []
    for area in activation.areas:
        mark = {ACTIVATE: "✓", UNSUPPORTED: "✗", LEFT_ALONE: "–"}[area.status]
        verb = {ACTIVATE: "will activate" if preview else "activating",
                UNSUPPORTED: "cannot activate", LEFT_ALONE: "left alone"}[area.status]
        lines.append(f"  {mark} {area.id:<17} {verb}: {area.detail}")
    if activation.config_changes:
        lines.append("")
        lines.append(("config changes (written to the global config on commit; existing keys are never "
                      "overridden):") if preview else "config changes written to the global config:")
        lines.extend(f"    + {change}" for change in activation.config_changes)
    return lines
