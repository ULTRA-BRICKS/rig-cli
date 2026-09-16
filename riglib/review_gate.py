"""Source of truth for the ``review-gate`` git-hook composer — the installed review-cli.

The dispatcher's ``hooks/review-gate`` composer is the review-before-commit gate. It used to
be copied verbatim from agent-tools (``git-hooks/global-dispatcher/hooks/review-gate``) like
every other composer — but its REAL owner is review-cli, which ships the same gate through
``review install-commit-hook``. The two copies forked: review-cli#211 added the trivial-delta
tolerance to review-cli's template only, while the agent-tools copy rig installed on every
machine never got it, so every rig-managed machine kept running the stale gate and ``rig
status`` reported it as CLEAN (it matched the stale source it was compared against).

Single owner = review-cli. rig asks the installed review-cli for the gate body (``review
install-commit-hook --print-gate`` prints it to stdout, exit 0) and installs + drift-checks
against THAT. The agent-tools copy is only a FALLBACK template, used when:

  - ``review`` is not installed (or the seam below disables it), or
  - the installed review-cli predates ``--print-gate`` (it prints a usage error and exits
    non-zero — this is what a machine with an older review-cli sees today).

A TRANSIENT failure (the call times out or cannot be executed) is different: it says nothing
about which gate is right, so it must never move an installed gate BACKWARD to the
agent-tools copy. ``composer_sources`` reports it as "expected unknown": apply leaves an
existing gate as-is (and installs the fallback only when nothing is there), drift skips the
comparison. Every fallback is reported in the action detail + the drift item text, so a stale
gate is visible instead of silently "clean". See rig-cli#382 / review-cli#396.

``composer_sources`` is the ONE place install (``runner._install_composers``) and drift
(``drift._check_composers``) get their expected composer content from — the two paths must
not fork again.

Test seam: ``RIG_REVIEW_BIN`` — unset → ``shutil.which("review")``; set to the empty string
→ disabled (the hermetic unit-suite default, set autouse in ``tests/conftest.py``); set to a
path → that executable. Stdlib only at import time.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

REVIEW_GATE_COMPOSER = "review-gate"
FALLBACK_SUFFIX = " — using the agent-tools copy"
REVIEW_CLI_ORIGIN = "review-cli's --print-gate output"
SOURCE_ORIGIN = "source"
_PRINT_GATE_ARGS = ("install-commit-hook", "--print-gate")
# fsutil.backup_path naming: `<name>.rig-bak-<YYYYmmdd-HHMMSS>[.<n>]`
_BACKUP_RE = re.compile(r"\.rig-bak-\d{8}-\d{6}(\.\d+)?$")
# The child's cwd: the filesystem root, NOT a temp dir — `tempfile.gettempdir()` honours
# $TMPDIR, which a wrapper can point INSIDE the repo, and the output is a GLOBAL file.
_NEUTRAL_CWD = os.path.abspath(os.sep)


@dataclass(frozen=True)
class GateSource:
    """The gate body review-cli printed, or why it could not (``transient`` = keep as-is)."""

    body: str | None
    note: str
    transient: bool = False


@dataclass(frozen=True)
class ComposerSource:
    """What apply installs / drift expects for one composer file.

    ``expected`` is None only for a TRANSIENT review-cli failure on ``review-gate``: the right
    content is unknown, so an installed file is left alone and drift does not compare it.
    ``fallback`` is the agent-tools copy, installed only when nothing is on disk yet.
    """

    name: str
    expected: str | None
    fallback: str
    origin: str
    note: str | None = None


def resolve_review_bin() -> str | None:
    """The ``review`` executable to ask for the gate body, or None when unavailable.

    ``RIG_REVIEW_BIN`` overrides discovery: empty string disables the lookup entirely (the
    unit suite must never shell out to a real review-cli), a path is used verbatim.
    """
    override = os.environ.get("RIG_REVIEW_BIN")
    if override is not None:
        return override or None
    return shutil.which("review")


def _minimal_env() -> dict[str, str]:
    """Only PATH + HOME reach the child: the gate body must not depend on session state."""
    return {k: v for k, v in os.environ.items() if k in ("PATH", "HOME")}


def review_gate_body() -> GateSource:
    """The gate body from the installed review-cli, or a fallback/transient reason.

    Never raises: every failure degrades to a ``GateSource`` whose note the caller reports
    verbatim (action detail / drift item). The child gets no stdin, a minimal env and a
    neutral cwd (the output is a GLOBAL file; it must not depend on the repo rig runs in).
    """
    review_bin = resolve_review_bin()
    if review_bin is None:
        return GateSource(None, f"review not installed{FALLBACK_SUFFIX}")
    try:
        proc = subprocess.run(
            [review_bin, *_PRINT_GATE_ARGS],
            capture_output=True,
            timeout=30,
            env=_minimal_env(),
            cwd=_NEUTRAL_CWD,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return GateSource(None, f"review --print-gate timed out/failed ({exc}) — left as-is", True)
    try:  # only stdout is the gate body; stderr noise must not discard a valid gate
        body = proc.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        return GateSource(None, f"review --print-gate output unreadable ({exc}){FALLBACK_SUFFIX}")
    if proc.returncode != 0 or not body.strip() or not body.startswith("#!"):
        return GateSource(
            None, f"review does not support --print-gate (exit {proc.returncode}){FALLBACK_SUFFIX}"
        )
    return GateSource(body, f"review-gate from review-cli ({review_bin})")


def composer_sources(src_hooks: Path) -> list[ComposerSource]:
    """Expected content for every composer the source ships, in name order.

    review-cli is asked ONCE, and only when the source actually ships a ``review-gate``.
    """
    files = sorted(p for p in src_hooks.iterdir() if p.is_file())
    gate: GateSource | None = None
    if any(p.name == REVIEW_GATE_COMPOSER for p in files):
        gate = review_gate_body()
    out: list[ComposerSource] = []
    for src_file in files:
        text = src_file.read_text(encoding="utf-8")
        expected, origin, note = text, SOURCE_ORIGIN, None
        if src_file.name == REVIEW_GATE_COMPOSER and gate is not None:
            note = gate.note
            if gate.body is not None:
                expected, origin = gate.body, REVIEW_CLI_ORIGIN
            elif gate.transient:
                expected, origin = None, REVIEW_CLI_ORIGIN
        out.append(ComposerSource(src_file.name, expected, text, origin, note))
    return out


def gate_note(sources: list[ComposerSource]) -> str:
    """The one-line review-gate provenance for the action detail."""
    for src in sources:
        if src.name == REVIEW_GATE_COMPOSER:
            return src.note or "review-gate from source"
    return "no review-gate composer in source"


def unshipped_composers(composer_dir: Path, sources: list[ComposerSource]) -> list[Path]:
    """Files in the composers dir that the source no longer ships (retired hooks).

    git keeps running them (``core.hooksPath`` points at the dir), so they are reported as
    disk→config drift — never deleted. rig's own ``*.rig-bak-*`` backups are not drift.
    """
    if not composer_dir.is_dir():
        return []
    shipped = {src.name for src in sources}
    return sorted(
        p for p in composer_dir.iterdir()
        if p.is_file() and p.name not in shipped and not _BACKUP_RE.search(p.name)
    )
