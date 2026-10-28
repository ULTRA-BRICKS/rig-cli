"""Shared helpers and fixtures for the `tmux` test modules (split from the original single module)."""
from __future__ import annotations

from pathlib import Path

from riglib import tmux


# ── pure rendering: the generated rig.tmux.conf ──────────────────────────────────────────
def _plan(**over):
    """A TmuxPlan with sensible defaults, overridable per-test."""
    return tmux.build_tmux(repo_home=Path("/home/u"), **over)


# ── plan building ────────────────────────────────────────────────────────────────────────
def _cfg(data, repo_root):
    from riglib.config import LoadedConfig

    return LoadedConfig(data=data, repo_root=repo_root)


def _build(data, repo_root, fake_agent_tools):
    from riglib.catalog import Catalog
    from riglib.plan import build

    data = {"agent_tools_source": str(fake_agent_tools), **data}
    cat = Catalog.scan(str(fake_agent_tools))
    return build(_cfg(data, repo_root), cat, project_type="unknown")


# ── install (runner) — import mode, idempotent, migration + backup ───────────────────────
def _tmux_action(home, **over):
    """A provision_tmux action whose artifacts land under a tmp HOME."""
    from riglib.plan import Action

    options = {
        "apply_mode": "import",
        "conf_path": str(home / ".tmux.conf"),
        "generated_dir": str(home / ".config" / "rig" / "tmux"),
        "resurrect": {},
        "continuum": {},
        "moshi": {},
        "cc_restore": {},
        "anti_sprawl": {},
        "boot": {"enabled": False},  # tests never touch launchd by default
    }
    options.update(over)
    return Action(
        kind="provision_tmux",
        category="tmux",
        item="config",
        source=home,
        target=home / ".tmux.conf",
        options=options,
    )


def _stage_live_tmux_state(home):
    """Create the LIVE activation state (the resurrect dir + complete plugin checkouts) a real
    apply would. Drift now checks these (codex finding), but the unit suite applies under
    RIG_TMUX_DRY_RUN (no live activation), so an in-sync drift test must stage them itself —
    otherwise drift correctly reports 'no plugins / no resurrect dir'. Uses the REAL plugin
    entrypoints from tmux.PLUGINS (NOT `<dir>.tmux` — resurrect ships `resurrect.tmux`)."""
    (home / ".tmux" / "resurrect").mkdir(parents=True, exist_ok=True)
    plugins = home / ".tmux" / "plugins"
    for name, (_repo, entry) in tmux.PLUGINS.items():
        (plugins / name).mkdir(parents=True, exist_ok=True)
        (plugins / name / entry).write_text("#!/usr/bin/env bash\n", encoding="utf-8")
