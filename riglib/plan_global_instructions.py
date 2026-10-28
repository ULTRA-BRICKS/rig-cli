"""Default common reporting guidance for every machine/repo harness, independent of repo guides.

Context: planned right after the harness stage; the canonical text and per-kind targets live in
`problem_reporting` / `harness_skills`. Split out of the plan assembler so it stays small.
"""
from __future__ import annotations

from pathlib import Path

from .config import LoadedConfig
from .plan_model import Action, InstallPlan
from .plan_targets import _kinds_with_global


def _build_global_instructions(config: LoadedConfig, plan: InstallPlan) -> None:
    """Default common guidance for every machine/repo harness, independent of repo guides."""
    from .claude_config_dirs import instruction_settings_paths
    from .harness_skills import global_instruction_targets

    harness = config.data.get("harness") or {}
    if not isinstance(harness, dict) or harness.get("enabled") is False:
        return
    harness = _with_machine_settings_paths(config, harness, instruction_settings_paths)
    seen: dict[Path, str] = {}
    for kind in _kinds_with_global(config):
        targets = global_instruction_targets(kind, harness, config.repo_root)
        if not targets:
            plan.notes.append(f"instructions/{kind}: no known global instruction file; reporting policy cannot be provisioned")
        for item, target in targets:
            _plan_target(plan, seen, item, target)


def _with_machine_settings_paths(config: LoadedConfig, harness: dict, instruction_settings_paths) -> dict:
    global_harness = config.global_data.get("harness") or {}
    if not isinstance(global_harness, dict):
        return harness
    # Keep explicit machine primary/account dirs when a repo overrides its harness.
    paths = [path for block in (global_harness, harness) for path in instruction_settings_paths(block)]
    return {**global_harness, **harness, "settings_paths": paths}


def _plan_target(plan: InstallPlan, seen: dict[Path, str], item: str, target: Path) -> None:
    try:
        identity = target.resolve()
    except (OSError, RuntimeError):
        # Keep an unreadable/looping target in the plan: the common policy resolver
        # reports it as drift and an apply error instead of aborting unrelated targets.
        identity = target.absolute()
    if identity in seen:
        plan.notes.append(f"instructions/{item}: shares {target} with {seen[identity]} (one policy write)")
        return
    seen[identity] = item
    plan.actions.append(Action(
        kind="provision_problem_reporting", category="instructions", item=item,
        source=Path(__file__).with_name("problem_reporting.py"), target=target,
        options={},
    ))
