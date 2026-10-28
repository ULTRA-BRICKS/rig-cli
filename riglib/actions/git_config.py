"""Global git config accessors and the ``provision_git_config`` handler."""

from __future__ import annotations

from dataclasses import dataclass, field
import shlex
import subprocess

from ..plan import Action
from .result import ActionResult


# ── git/gh shells (isolated for testability) ──────────────────────────────────────
def _git_global(key: str) -> str | None:
    try:
        res = subprocess.run(
            ["git", "config", "--global", key], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout.strip() if res.returncode == 0 and res.stdout.strip() else None


def _set_git_global(key: str, value: str) -> int:
    try:
        res = subprocess.run(
            ["git", "config", "--global", key, value], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return 1
    return res.returncode


def _git_global_get(key: str) -> str | None:
    """``git config --global --get <key>`` VERBATIM (only git's trailing newline dropped), or None.

    Unlike :func:`_git_global` it keeps leading/trailing spaces and an empty value, so the ``git:``
    area's value compare (``riglib.gitconfig.resolve_key``) never reads a stripped value as drift.
    ``None`` = unset (rc 1), no global gitconfig yet, or git missing — all "not set" to the caller.
    """
    try:
        res = subprocess.run(
            ["git", "config", "--global", "--get", key],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    out = res.stdout
    return out[:-1] if out.endswith("\n") else out


def _do_provision_git_config(action: Action, on_conflict: str) -> ActionResult:
    """Set every declared ``git.config`` key with ``git config --global <key> <value>``.

    Idempotent: a key git already holds with the same meaning (``riglib.gitconfig.resolve_key``,
    bool/int-aware) is left alone. A key set by hand to a DIFFERENT value is overwritten and its
    prior value + the exact restore command are recorded in the detail (a scalar has no file to
    back up — the same contract the dispatcher uses for ``core.hooksPath``); ``on_conflict=skip``
    leaves such a key untouched instead. Never unsets anything. A failed write (git missing, or a
    multi-valued key git refuses to overwrite) is an ``error`` naming the key.
    """
    from .. import gitconfig

    states = gitconfig.resolve(action.options)
    if not states:
        return ActionResult(action, "skipped", "git: no keys declared")
    out = _GitConfigOutcome()
    for st in states:
        _apply_git_key(st, on_conflict, out)
    detail = out.detail()
    if out.failed:
        return ActionResult(action, "error", detail)
    if out.updated:
        return ActionResult(action, "updated", detail)
    if out.created:
        return ActionResult(action, "created", detail)
    return ActionResult(action, "skipped", detail)


@dataclass
class _GitConfigOutcome:
    """Per-key results of one ``git.config`` apply, rendered into the action detail."""

    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    current_count: int = 0

    def detail(self) -> str:
        parts: list[str] = []
        if self.created:
            parts.append(f"set {', '.join(self.created)}")
        if self.updated:
            parts.append(f"updated {', '.join(self.updated)}")
        if self.kept:
            parts.append(f"left differing {', '.join(self.kept)} (on_conflict=skip)")
        if self.failed:
            parts.append(f"FAILED {', '.join(self.failed)}")
        if self.current_count:
            parts.append(f"{self.current_count} already current")
        return "git: " + "; ".join(parts + self.notes)


def _apply_git_key(st, on_conflict: str, out: _GitConfigOutcome) -> None:
    """Write one resolved key if it needs it, recording the outcome in ``out``."""
    if st.state == "ok":
        out.current_count += 1
        return
    if st.state == "modified" and on_conflict == "skip":
        out.kept.append(st.key)
        return
    rc = _set_git_global(st.key, st.desired)
    if rc != 0:
        why = "git refused (a multi-valued key? rig sets single values only)" if rc == 5 else f"exit {rc}"
        out.failed.append(f"{st.key} ({why})")
        return
    if st.state == "missing":
        out.created.append(st.key)
        return
    out.updated.append(st.key)
    prior = st.current if st.current is not None else ""
    out.notes.append(
        f"prior {st.key}={prior!r} (restore with: git config --global "
        f"{shlex.quote(st.key)} {shlex.quote(prior)})"
    )
