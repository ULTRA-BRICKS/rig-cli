"""Shared vocabulary of `rig lint run`: exit codes, check states, options, result rows, report.

Context: the executor (`lint_run`), the classifier (`lint_run_verdict`), the coverage rule
(`lint_run_coverage`) and the renderer (`lint_run_report`) all speak these types; keeping them in a
leaf module lets each of those import it without importing the orchestrator (no cycles). Pure data,
stdlib only.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .lint_providers import PlannedCheck

SCHEMA = "rig-lint-run/v1"
EXIT_OK, EXIT_FINDINGS, EXIT_USAGE, EXIT_INCOMPLETE, EXIT_MISSING = 0, 1, 2, 3, 127
# `wrote-files`: a command that was supposed to only read changed the worktree, so its verdict cannot
# be trusted and the worktree is no longer what the caller committed.
INCOMPLETE_REASONS = frozenset({"timeout", "crash", "malformed-output", "output-limit", "malformed-manifest",
                                "incomplete", "bootstrap-failed", "unreadable", "wrote-files"})

State = Literal["passed", "failed", "missing-tool", "not-applicable", "unsupported-platform", "planned"]


@dataclass(frozen=True)
class Verdict:
    """What one finished command means (classification result, before it becomes a `CheckResult`)."""

    state: State
    reason: str
    detail: str
    counts: Mapping[str, int] | None = None
    report: Mapping[str, object] | None = None


@dataclass(frozen=True)
class RunOptions:
    cwd: str
    config: str | None
    json: bool
    plan: bool
    no_setup: bool
    fix: bool
    ci: bool | None
    only: tuple[str, ...]
    skip: tuple[str, ...]
    scopes: tuple[str, ...]
    timeout: int | None


@dataclass(frozen=True)
class CheckResult:
    check: PlannedCheck
    state: State
    reason: str
    detail: str
    exit_code: int | None
    duration_ms: int | None
    counts: Mapping[str, int] | None
    stdout: str
    stderr: str
    truncated: bool
    fix: Mapping[str, object] | None
    report: Mapping[str, object] | None

    def as_dict(self) -> dict[str, object]:
        check = self.check
        resolution = check.resolution
        return {
            "id": check.id, "provider": check.provider, "category": check.category, "scope": check.scope,
            "tool": check.tool, "state": self.state, "reason": self.reason, "detail": self.detail,
            "argv": list(check.argv) if check.argv is not None else None, "cwd": check.cwd,
            "exit_code": self.exit_code, "duration_ms": self.duration_ms, "timeout_s": check.timeout_s,
            "resolution": None if resolution is None else {
                "kind": resolution.kind, "path": resolution.path, "detail": resolution.detail},
            "counts": dict(self.counts) if self.counts is not None else None,
            "output": {"stdout": self.stdout, "stderr": self.stderr, "truncated": self.truncated},
            "fix": dict(self.fix) if self.fix is not None else None,
            "report": dict(self.report) if self.report is not None else None,
        }


@dataclass(frozen=True)
class RunReport:
    exit_code: int
    doc: dict[str, object]
    text: str
