"""The `rig-lint-run/v1` document and its text rendering.

Context: `lint_run` produces results, a discovery, a bootstrap plan/outcome and a `Coverage`; this
module only SHAPES them. Wording rule (acceptance): a headline never says "clean" or "passed" unless
every applicable check passed AND every source file was checked; a missing tool, a blocked setup,
partial coverage or an unsupported language reads as "not verified". The document keeps the field
names documented in docs/lint.md; adding a field is fine, renaming one is a schema change.
"""
from __future__ import annotations

import shlex
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from .lint_run_model import EXIT_FINDINGS, EXIT_INCOMPLETE, EXIT_MISSING, EXIT_OK, SCHEMA, CheckResult

if TYPE_CHECKING:
    from .lint_discovery import Discovery
    from .lint_run_config import LintConfig
    from .lint_run_coverage import Coverage

_TEXT_TAIL_LINES = 30
_UNAVAILABLE = ("missing-tool", "unsupported-platform")


def discovery_doc(discovery: Discovery, unsupported: Sequence[tuple[str, str]]) -> dict[str, object]:
    scopes = []
    for scope in discovery.scopes:
        pm = scope.package_manager
        scopes.append({
            "path": scope.rel, "languages": list(scope.languages),
            "package_manager": None if pm is None else {
                "name": pm.name, "source": pm.source, "lockfile": pm.lockfile, "root": pm.root,
                **({"ambiguous": list(pm.ambiguous)} if pm.ambiguous else {})},
            "tools": [{"tool": t.tool, "role": t.role, "declared": t.declared, "config_files": list(t.config_files)}
                      for t in scope.tools],
            "scripts": [s.name for s in scope.scripts],
        })
    return {
        "scopes": scopes,
        "unknown_languages": [{"language": u.language, "files": u.files, "examples": list(u.examples)}
                              for u in discovery.unknown],
        "unsupported_tools": [{"scope": s, "tool": t} for s, t in unsupported],
        "problems": [{"path": p.path, "kind": p.kind, "detail": p.detail} for p in discovery.problems],
        "skipped_symlinks": list(discovery.skipped_symlinks),
    }


def empty_discovery_doc() -> dict[str, object]:
    return {"scopes": [], "unknown_languages": [], "unsupported_tools": [], "problems": [], "skipped_symlinks": []}


def bootstrap_doc(plan, outcome) -> dict[str, object]:
    """`plan`/`outcome` are `lint_bootstrap.BootstrapPlan`/`BootstrapOutcome` (None when setup never planned)."""
    if plan is None:
        return {"steps": [], "blocked": [], "results": [], "notes": []}
    return {
        "steps": [{"id": s.id, "kind": s.kind, "manager": s.manager, "argv": list(s.argv), "writes": list(s.writes)}
                  for s in plan.steps],
        "blocked": [{"scope": b.scope, "tool": b.tool, "reason": b.reason, "detail": b.detail} for b in plan.blocked],
        "results": [] if outcome is None else [{"id": i, "state": s, "detail": d} for i, s, d in outcome.results],
        "notes": list(plan.notes),
    }


def _config_doc(cfg: LintConfig) -> dict[str, object]:
    return {
        "enabled": cfg.enabled, "timeout_seconds": cfg.timeout_seconds, "categories": dict(cfg.categories),
        "bootstrap": {"enabled": cfg.bootstrap.enabled, "javascript": cfg.bootstrap.javascript,
                      "python": cfg.bootstrap.python},
        "providers": {pid: {"enabled": o.enabled, **({"timeout_seconds": o.timeout_seconds}
                                                      if o.timeout_seconds is not None else {})}
                      for pid, o in sorted(cfg.providers.items())},
    }


def _summary(results: Sequence[CheckResult], duration_ms: int, cov: Coverage) -> dict[str, object]:
    def count(state: str) -> int:
        return sum(1 for r in results if r.state == state)

    not_applicable = count("not-applicable")
    return {
        "total": len(results), "applicable": len(results) - not_applicable, "passed": count("passed"),
        "failed": count("failed"), "missing_tool": count("missing-tool"), "not_applicable": not_applicable,
        "unsupported_platform": count("unsupported-platform"), "planned": count("planned"),
        "files_checked": cov.files_checked, "files_uncovered": cov.files_uncovered, "complete": cov.complete,
        "duration_ms": duration_ms,
    }


def document(*, root: Path, exit_code: int, plan: bool, ci: bool, fix: bool, setup: str, cfg: LintConfig,
             discovery: dict[str, object], bootstrap: dict[str, object], results: Sequence[CheckResult],
             cov: Coverage, duration_ms: int) -> dict[str, object]:
    return {
        "schema": SCHEMA, "ok": None if plan else exit_code == EXIT_OK, "exit_code": exit_code,
        # Top-level mirror of summary.complete: the one boolean a caller must read before trusting "ok".
        "complete": cov.complete,
        "root": str(root), "mode": {"ci": ci, "plan": plan, "fix": fix, "setup": setup},
        "config": _config_doc(cfg), "discovery": discovery, "bootstrap": bootstrap,
        "coverage": cov.as_dict(),
        "checks": [r.as_dict() for r in results], "summary": _summary(results, duration_ms, cov),
    }


# ── text ─────────────────────────────────────────────────────────────────────────

def _tail(stdout: str, stderr: str) -> list[str]:
    lines = [ln for ln in (stderr + "\n" + stdout).splitlines() if ln.strip()]
    return lines[-_TEXT_TAIL_LINES:]


def _finding_line(finding: Mapping[str, object]) -> str:
    where = f"{finding.get('path')}:{finding['line']}" if finding.get("line") else str(finding.get("path"))
    return f"{where} {finding.get('severity')} {finding.get('rule')}: {finding.get('message')}"


def _report_lines(report: Mapping[str, object]) -> list[str]:
    """Findings of an embedded ``rig-lint-check/v1`` document, most severe first, capped like any output tail."""
    found = [f for f in report.get("findings", []) if isinstance(f, Mapping)]
    ranked = sorted(found, key=lambda f: (f.get("severity") != "error", str(f.get("path")), f.get("line") or 0))
    lines = [_finding_line(f) for f in ranked[:_TEXT_TAIL_LINES]]
    if len(ranked) > _TEXT_TAIL_LINES:
        lines.append(f"... and {len(ranked) - _TEXT_TAIL_LINES} more finding(s); see `rig lint check` or --json")
    return lines


def _failure_lines(row: Mapping[str, object]) -> list[str]:
    """The evidence under a failed row: parsed findings when the check embeds a report, else the output tail."""
    report = row.get("report")
    if isinstance(report, Mapping) and isinstance(report.get("findings"), list) and report["findings"]:
        return _report_lines(report)
    return _tail(row["output"]["stdout"], row["output"]["stderr"])


def _bootstrap_lines(bootstrap: Mapping[str, object]) -> list[str]:
    out = []
    for step in bootstrap["steps"]:
        out.append(f"bootstrap step {step['id']}: {shlex.join(step['argv'])}" if step["argv"]
                   else f"bootstrap step {step['id']}")
    out.extend(f"bootstrap {res['id']}: {res['state']} ({res['detail']})" for res in bootstrap["results"])
    out.extend(f"bootstrap blocked [{b['scope']}] {b['tool']}: {b['reason']} - {b['detail']}"
               for b in bootstrap["blocked"])
    out.extend(f"bootstrap note: {note}" for note in bootstrap["notes"])
    return out


_UNCOVERED_WHY = {
    "no-provider": "Rig has no checker for it",
    "no-check": "no check was planned for it",
    "not-verified": "its check did not run, was skipped, or could not analyze it",
    "tracked-but-ignored": "tracked by git but matched by an ignore rule, so its tool skips it; un-ignore it, "
                           "untrack it, or accept it as unchecked",
}


def _uncovered_line(group: Mapping[str, object]) -> str:
    return (f"not checked: {group['language']} - {group['files']} file(s), "
            f"e.g. {', '.join(group['examples'])} ({_UNCOVERED_WHY.get(group['why'], group['why'])})")


def _text_head(doc: Mapping[str, object]) -> list[str]:
    mode, discovery = doc["mode"], doc["discovery"]
    out = [f"rig lint: {doc['root']}",
           f"mode: ci={'yes' if mode['ci'] else 'no'} plan={'yes' if mode['plan'] else 'no'} "
           f"fix={'yes' if mode['fix'] else 'no'} setup={mode['setup']}"]
    out.extend(_bootstrap_lines(doc["bootstrap"]))
    out.extend(f"discovery problem: {p['path']}: {p['kind']} ({p['detail']})" for p in discovery["problems"])
    out.extend(f"configured tool without a Rig provider (left untouched): {i['tool']} [{i['scope']}]"
               for i in discovery["unsupported_tools"])
    out.extend(_uncovered_line(group) for group in doc["coverage"]["uncovered"])
    out.extend(f"template-lint-uncovered: {gap['framework']} templates in [{gap['scope']}] are not linted "
               "(Oxlint checks the script part only; configure ESLint or the framework's tool)"
               for gap in doc["coverage"]["template_uncovered"])
    if doc["coverage"]["incomplete_checks"]:
        out.append("analysis not finished by: " + ", ".join(doc["coverage"]["incomplete_checks"])
                   + " (the result is incomplete even where other checks cover the same files)")
    return out


def _text_check(row: Mapping[str, object], width: int) -> list[str]:
    timing = f"{row['duration_ms']}ms" if row["duration_ms"] is not None else "-"
    exit_part = f" exit {row['exit_code']}" if row["exit_code"] is not None else ""
    out = [f"  {row['state']:<20} {row['id']:<{width}} {row['category']:<9} {row['reason']}{exit_part} {timing}"]
    if row["state"] != "passed":
        out.append(f"      {row['detail']}")
    if row["state"] == "failed":
        out.extend(f"      | {line}" for line in _failure_lines(row))
    fix = row["fix"]
    if fix and fix["mode"] == "unsupported":
        out.append("      --fix: no safe fix for this check; ran in check mode")
    if fix and fix["changed_files"]:
        out.append(f"      --fix changed: {', '.join(fix['changed_files'])}")
    if fix and fix["needs_restage"]:
        out.append(f"      needs restage (not restaged by rig): {', '.join(fix['needs_restage'])}")
    return out


def _languages(cov: Mapping[str, object]) -> str:
    return ", ".join(sorted({g["language"] for g in cov["uncovered"]})) or "unknown"


def _unavailable_count(summary: Mapping[str, object]) -> int:
    return summary["missing_tool"] + summary["unsupported_platform"]


def _verdict_127(doc: Mapping[str, object]) -> str:
    unavailable = _unavailable_count(doc["summary"])
    if unavailable:
        return (f"INCOMPLETE: {unavailable} check(s) could not run (tool missing or platform unsupported); "
                "their files are NOT verified")
    cov = doc["coverage"]
    if not cov["files_uncovered"] and cov["template_uncovered"]:
        return ("INCOMPLETE: templates are NOT linted (template-lint-uncovered): Oxlint reads only the script part of "
                "Vue/Svelte files; configure ESLint (or the framework's own tool) for them")
    return (f"INCOMPLETE: {cov['files_uncovered']} source file(s) in {_languages(cov)} were NOT checked "
            "(unsupported-language); the run is not verified")


def _verdict_ok(doc: Mapping[str, object]) -> str:
    cov, summary = doc["coverage"], doc["summary"]
    if not cov["complete"]:
        return (f"NOT COMPLETE: no check failed, but only {cov['files_checked']} of {cov['files_total']} "
                "source file(s) were verified (the rest have no checker, were filtered or disabled, or could not run)")
    if summary["applicable"] == 0:
        return "no source files to check: nothing was verified because there is nothing to verify"
    return f"OK: all {summary['applicable']} applicable check(s) passed; {cov['files_checked']} source file(s) checked"


def headline(doc: Mapping[str, object]) -> str:
    code, summary = doc["exit_code"], doc["summary"]
    if [row["id"] for row in doc["checks"]] == ["lint@."]:
        return "DISABLED: lint.enabled is false in the rig config; nothing was verified"
    if doc["mode"]["plan"]:
        return "PLAN ONLY: nothing was installed, run or verified"
    if code == EXIT_OK:
        return _verdict_ok(doc)
    if code == EXIT_FINDINGS:
        return f"FAILED: findings in {summary['failed']} check(s)"
    if code == EXIT_INCOMPLETE:
        return ("INCOMPLETE: analysis did not finish (timeout, crash, unreadable output or manifest, a check that "
                "wrote files, or failed setup); this is not a verified result")
    if code == EXIT_MISSING:
        return _verdict_127(doc)
    return f"FAILED (exit {code})"


def _summary_line(doc: Mapping[str, object]) -> str:
    s = doc["summary"]
    counts = [f"{s[key]} {label}" for key, label in (
        ("passed", "passed"), ("failed", "failed"), ("missing_tool", "missing-tool"),
        ("unsupported_platform", "unsupported-platform"), ("not_applicable", "not-applicable"),
        ("planned", "planned")) if s[key]]
    return (f"summary: {s['total']} checks, {s['applicable']} applicable"
            + (f": {', '.join(counts)}" if counts else "")
            + f"; files checked {s['files_checked']}, not checked {s['files_uncovered']} "
            f"(exit {doc['exit_code']}, {s['duration_ms']} ms)")


def render_text(doc: Mapping[str, object]) -> str:
    out = _text_head(doc)
    rows = doc["checks"]
    width = max((len(r["id"]) for r in rows), default=0)
    if rows:
        out.append(f"checks ({len(rows)}):")
    for row in rows:
        out.extend(_text_check(row, width))
    if doc["summary"]["applicable"] == 0:
        out.append("no applicable checks: nothing was verified")
    out.append(_summary_line(doc))
    out.append(f"result: {headline(doc)}")
    return "\n".join(out)
