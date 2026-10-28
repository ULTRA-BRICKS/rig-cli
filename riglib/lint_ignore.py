"""Keep the dependency directories ``rig lint`` installs out of the repository scan.

Context: a bootstrap or dependency-restore step runs the project's package manager, which creates
``node_modules/``. In a repo with no ignore rule for it, every installed package becomes an untracked
source tree: discovery (which is git-based -- tracked plus untracked-not-ignored) then treats the
dependencies as scopes and the checks run inside them. The legitimate toolchain configuration is the
one every JS project has: ignore ``node_modules/``. Rig records it in the repository's
``.git/info/exclude`` through the SAME marker-block reconciler as the other Rig-owned per-repo
entries (:mod:`riglib.actions.git_exclude`): local, never committed, append-only for every other line,
and idempotent. Nothing about the project's own files changes.

Rules: an entry is added only for a directory this run's install step creates and that git does not
already ignore (so an existing ``.gitignore``, with its negations, is never touched or duplicated);
the entry is written BEFORE the install step so the first scan after the install is already clean; a
git-TRACKED file under an ignored directory is still checked (tracked always wins in the scope
enumeration); plan / ``--no-setup`` / CI never write it. Stdlib-only at import time.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Sequence

BEGIN_MARKER = "# >>> rig managed: lint dependencies (rig lint)"
END_MARKER = "# <<< rig managed: lint dependencies (rig lint)"
_COMMENT = "# Package-manager dependency directories installed by `rig lint`; not part of the project's own code."
_GIT_TIMEOUT_S = 10
_PROBE = ".rig-ignore-probe"


def entry_for(directory: str) -> str:
    """The anchored git pattern for ``<directory>/node_modules`` (``directory`` repo-relative, ``"."`` = root)."""
    return "/node_modules/" if directory == "." else f"/{directory.strip('/')}/node_modules/"


def valid_entry(entry: str) -> bool:
    """Only ``/<path>/node_modules/`` patterns: the handler refuses anything else, whatever the action says."""
    return (
        entry.startswith("/") and entry.endswith("/node_modules/") and "\n" not in entry and "\r" not in entry
        and ".." not in entry.split("/") and "!" not in entry and "*" not in entry
    )


def block_text(entries: Sequence[str]) -> str:
    return "\n".join([BEGIN_MARKER, _COMMENT, *entries, END_MARKER])


def existing_entries(content: str) -> list[str]:
    """The entries of the Rig-owned block already in an exclude file (empty without a block)."""
    lines = content.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == BEGIN_MARKER)
        stop = next(i for i, line in enumerate(lines) if i > start and line.strip() == END_MARKER)
    except StopIteration:
        return []
    return [line.strip() for line in lines[start + 1:stop] if line.strip() and not line.startswith("#")]


def merged_entries(content: str, wanted: Sequence[str]) -> list[str]:
    """Existing block entries plus ``wanted``, deduplicated, in a stable order (a repo accumulates, never loses)."""
    return sorted({*existing_entries(content), *wanted})


def is_ignored(root: Path, directory: str) -> bool:
    """Whether git already ignores ``<directory>/node_modules`` (any ignore source, negations honored)."""
    probe = f"node_modules/{_PROBE}" if directory == "." else f"{directory}/node_modules/{_PROBE}"
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "-q", "--", probe],
            capture_output=True, timeout=_GIT_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return True  # no git to ask: writing an exclude would be a guess, so claim nothing is missing
    return done.returncode == 0


def pending_entries(root: Path, directories: Sequence[str]) -> list[str]:
    """Entries for the install directories git does not ignore yet, deduplicated in order."""
    out: list[str] = []
    for directory in directories:
        entry = entry_for(directory)
        if entry not in out and not is_ignored(root, directory):
            out.append(entry)
    return out
