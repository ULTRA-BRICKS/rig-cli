"""Per-run bookkeeping of omp ``config.yml`` files created by this apply (approval and web_search share one file)."""

from __future__ import annotations

from pathlib import Path


# omp config.yml paths created during THIS run_plan. Approval and web_search share
# that file; on_conflict=skip must not treat a file we just created as user content
# (rig-cli#447 first-run: the second writer would otherwise skip the missing key).
_omp_yaml_created_this_run: set[str] = set()


def _reset_omp_yaml_created() -> None:
    _omp_yaml_created_this_run.clear()


def _mark_omp_yaml_created(path: Path) -> None:
    _omp_yaml_created_this_run.add(str(path))


def _omp_yaml_skip_existing(path: Path, existed: bool, on_conflict: str) -> bool:
    """True when skip must leave this yaml alone (a user file, not our first-run create)."""
    return existed and on_conflict == "skip" and str(path) not in _omp_yaml_created_this_run
