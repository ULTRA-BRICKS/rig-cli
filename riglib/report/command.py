"""``rig report`` orchestration — wired from ``riglib/cli.py`` as ``cmd_report``.

Smallest slice of HyperIDE /result-report that produces a page a human can
open: gather git log/diff since merge-base, write self-contained HTML, print
the path. Does not clone hyperide.github.io, does not ``git push``, does not
post PR/Linear links.

Accessed via: ``rig report [TASK_ID] [-C DIR] [--title T] [--out FILE] [--base REF]``.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from ..errors import ConfigError
from .gather import Gathered, gather
from .html import render_html, render_text

_UNSAFE_TASK_ID = re.compile(r"[\\/\x00]")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "task_id",
        nargs="?",
        help="ticket/task id shown in the report (e.g. HYP-1234 or rig-cli#429). "
        "Default: the current branch name with '/' replaced by '-'.",
    )
    parser.add_argument("-C", "--cwd", default=".", help="repo root (default: cwd)")
    parser.add_argument(
        "--title", help="report title (default: first commit subject, else task id)"
    )
    parser.add_argument(
        "--out",
        metavar="PATH",
        help="HTML file to write (default: ~/.cache/rig/reports/<task-id>/index.html)",
    )
    parser.add_argument(
        "--base",
        metavar="REF",
        help="merge-base ref (default: origin/main, then main, then master)",
    )


def run(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd).expanduser().resolve()
    gathered = gather(cwd, base=args.base)
    task_id = validate_task_id(args.task_id or _default_task_id(gathered.branch))
    title = (args.title or "").strip() or _title_from(gathered, task_id)
    out = _out_path(args.out, task_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        render_html(task_id=task_id, title=title, gathered=gathered), encoding="utf-8"
    )
    print(render_text(task_id=task_id, title=title, gathered=gathered), end="")
    print(f"Wrote {out}")
    print(f"Open {out.resolve().as_uri()}")
    return 0


def validate_task_id(task_id: str) -> str:
    """Reject empty / dot-segment / path-shaped ids so a ticket cannot escape
    the output directory (HyperIDE result-report.sh `valid_ticket` — same class
    of bug: `HYP-1345/../..` used to resolve to the reports-repo root)."""
    s = task_id.strip()
    if not s:
        raise ConfigError(
            what="task id is empty",
            why="the report filename and heading need a non-empty id.",
            fix="pass a ticket id, e.g. `rig report HYP-1234`.",
        )
    if s in (".", "..") or _UNSAFE_TASK_ID.search(s):
        raise ConfigError(
            what=f"invalid task id {task_id!r}",
            why="a path-shaped id (`/`, `\\\\`, `..`) could write the HTML outside the reports directory.",
            fix="use a ticket id like HYP-1234 or rig-cli#429 — no slashes or `..`.",
        )
    return s


def _default_task_id(branch: str) -> str:
    return branch.replace("/", "-") or "HEAD"


def _title_from(gathered: Gathered, task_id: str) -> str:
    if gathered.commits:
        return gathered.commits[0].message
    return task_id


def _out_path(explicit: str | None, task_id: str) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    fs_name = task_id.replace("#", "-")
    return Path.home() / ".cache" / "rig" / "reports" / fs_name / "index.html"
