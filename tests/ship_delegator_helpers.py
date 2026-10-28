"""Shared helpers for the ship_delegator test modules."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from riglib.actions import ship_delegator as ship_delegator_mod
from riglib.actions.runner import _do_provision_ship_delegator
from riglib.actions.ship_delegator import ship_env_file_content, ship_env_file_path
from riglib.config import validate
from riglib.plan import Action, InstallPlan


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return path


def _canonical_ship(tmp_path: Path) -> Path:
    src = tmp_path / "agent-tools" / "ci" / "ship" / "ship.sh"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("#!/usr/bin/env bash\necho ship\n", encoding="utf-8")
    return src


def _action(repo: Path, canonical: Path) -> Action:
    return Action(
        kind="provision_ship_delegator",
        category="ship_delegator",
        item="delegator",
        source=repo,
        target=repo,
        options={"canonical_ship": str(canonical)},
    )


def _apply(repo: Path, canonical: Path, on_conflict: str = "backup"):
    return _do_provision_ship_delegator(_action(repo, canonical), on_conflict)


def _plan_with_action(repo: Path, canonical: Path) -> InstallPlan:
    plan = InstallPlan()
    plan.actions.append(_action(repo, canonical))
    return plan


def _delegator_path(repo: Path) -> Path:
    return repo / ".claude" / "scripts" / "pr-ship.sh"


def _exclude_path(repo: Path) -> Path:
    return repo / ".git" / "info" / "exclude"


def _loaded(cfg: dict, repo: Path):
    from riglib.config import LoadedConfig

    validate(cfg)
    return LoadedConfig(data=cfg, repo_root=repo)


def _write_env_file(canonical: Path) -> Path:
    """Write the machine env file for ``canonical`` (under the suite-isolated XDG_CONFIG_HOME)."""
    p = ship_env_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(ship_env_file_content(canonical), encoding="utf-8")
    return p


def _script_env(**extra: str) -> dict:
    """A subprocess env with no inherited AGENT_TOOLS_ROOT (the delegator prefers the env var)."""
    env = dict(os.environ)
    env.pop("AGENT_TOOLS_ROOT", None)
    env.update(extra)
    return env


def _dropped_action_env_items(action, item="env-file"):
    """ship_env drift items for a repo action dropped by non-git status (``item=""`` = any item)."""
    from riglib.drift import DriftReport, check_ship_env_for_dropped_repo_action

    report = DriftReport()
    check_ship_env_for_dropped_repo_action(action, report)
    return [i for i in report.items if i.category == "ship_env" and (not item or i.item == item)]



def _alias_action(canonical: Path | None) -> Action:
    from riglib.gh_ship_alias import GH_SHIP_ALIAS_CATEGORY

    opts = {"canonical_ship": str(canonical)} if canonical is not None else {}
    return Action(
        kind="provision_gh_ship_alias", category=GH_SHIP_ALIAS_CATEGORY, item="alias",
        source=Path("/src"), target=Path("/gh/config.yml"), options=opts,
    )



def _transient_setup(tmp_path: Path, monkeypatch):
    """Wire a TRANSIENT agent-tools checkout + a PERSISTENT env-file location.

    Everything a test creates lives under the real system tempdir (pytest tmp_path), so we can't
    get a genuinely-non-temp path on disk. Instead we monkeypatch the temp-root set to a synthetic
    dir that contains ONLY the fake agent-tools checkout — so the checkout reads as "transient" and
    the env file (under a sibling XDG dir) reads as "persistent", exactly the real-machine leak.
    Returns ``(transient_canonical, env_path)``.
    """
    faketmp = tmp_path / "faketmp"
    canonical = faketmp / "agent-tools" / "ci" / "ship" / "ship.sh"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text("#!/usr/bin/env bash\necho ship\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "persistent-cfg"))
    monkeypatch.setattr(ship_delegator_mod, "_system_temp_roots", lambda: [faketmp.resolve()])
    return canonical, ship_env_file_path()
