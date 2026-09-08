"""``rig report`` — HyperIDE /result-report, as a rig command.

Accessed via: ``rig report [TASK_ID]`` (wired from ``riglib/cli.py`` as
``cmd_report``). Gathers git log/diff since the merge-base with main (the
mechanical half of HyperIDE's ``.claude/scripts/result-report.sh gather``)
and writes a self-contained HTML file a human can open.

Assumptions: the cwd (or ``-C``) is a git work tree; ``git`` is on PATH.
Does not publish to GitHub Pages and does not need ``components.js``.
"""

from __future__ import annotations

from .command import run

__all__ = ["run"]
