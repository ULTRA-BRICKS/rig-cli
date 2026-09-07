"""``rig report`` -- generate and publish completion reports (single-task and
ecosystem-periodic) to a static GitHub Pages "reports" site, following the
``result-report`` agent-tools skill's data/template contract
(``skills/universal/result-report/SKILL.md`` -- as of 2026-09-07 (scaffold time) an
UNMERGED agent-tools branch/PR, #440, not yet on agent-tools main; confirm it has
landed before treating this contract as stable).

Scaffold only as of 2026-09-07 -- ``rig report init|new|periodic`` are wired into the
CLI and argparse-complete, but their bodies are stubs (exit code 2, "not yet
implemented"). :mod:`riglib.report.model` and :mod:`riglib.report.render` are real: the
``ReportData`` dataclass and the ``Report.init()`` JS serializer a later implementation
builds against. See :mod:`riglib.report.command` for the full design rationale and the
per-subcommand plan.

Entry point: :func:`run`, wired from ``riglib/cli.py`` as ``cmd_report``.
"""

from __future__ import annotations

from .command import run

__all__ = ["run"]
