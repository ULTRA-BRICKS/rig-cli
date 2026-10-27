"""`rig lint check`: complexity (pinned cccc) + file-line limits + safe TS/JS fixer.

One engine behind every front-end (CLI, activation hook, CI). Fail closed throughout: a missing or
mismatched analyzer (127), a config error (2), or ANY coverage gap in the analysis (3) can never
read as a pass. Exit priority 2 > 127 > 3 > 1 > 0. A disabled check reports `disabled`, never
`pass`. This module orchestrates; scope enumeration lives in `lint_check_scope`, analyzer
invocation/validation in `lint_check_analyzer`, the else-after-return rewrite in `lint_fix_else`.
`lint_check*` modules are the engine; `lint_checks_*` are the config/activation siblings.

`--staged` inspects index content (snapshot), so it cannot be combined with `--fix`; `--fix`
also refuses files with partially staged changes (exit 2, nothing written), and never restages: a
rewritten file whose index content differs from HEAD is reported as `needs-restage`.

`--fix` is scoped to the FLAGGED functions and verified: after the analyzer re-run a rewrite is
reverted when its file gained any finding (labelled `introduced-by-fix`), an existing size
violation grew, a function's metrics drifted, or nothing improved. An exception mid-fix rolls
every unverified rewrite back.

Trust boundary: only an explicit `--fix` loads the target repository's `node_modules/typescript`
(executing project code). A plain or `--staged` scan never does, so it reports `fixable: false`,
meaning "not confirmed fixable"; `--fix` still discovers and applies every eligible site.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import lint_check_analyzer as analyzer_mod
from . import lint_fix_else
from .errors import RigError
from .lint_check_analyzer import ANALYZER_PATH, AnalyzerUnavailable, FunctionMetric, Incomplete
from .lint_check_scope import (UNCHECKED, FileFacts, Scope, ScopedFile, ScopeError, build_scope, extension,
                               inspect_file, partially_staged, staged_paths)

SCHEMA = "rig-lint-check/v1"
RULE_COGNITIVE = "complexity/cognitive"
RULE_CYCLOMATIC = "complexity/cyclomatic"
RULE_LINES = "file-lines/max-lines"
NEEDS_RESTAGE = "needs-restage"
INSTALL_HINT = "run `rig apply commit` to install the pinned analyzer"

EXIT_OK, EXIT_FINDINGS, EXIT_USAGE, EXIT_INCOMPLETE, EXIT_ANALYZER = 0, 1, 2, 3, 127


def resolve_analyzer() -> str:
    """Path of the verified analyzer (module-level seam: tests inject fakes)."""
    from . import cccc_install

    try:
        return str(cccc_install.resolve_verified_binary())
    except (cccc_install.AnalyzerMissing, cccc_install.AnalyzerMismatch,
            cccc_install.UnsupportedPlatform) as exc:
        raise AnalyzerUnavailable(str(exc)) from exc


def _layer_files(env, explicit: Path | None) -> list[tuple[str, Path]]:
    """(label, path) of the config files `load` would read: global, then repo rig.yaml or --config."""
    from .config import global_config_path, repo_config_path

    layers = [("global", global_config_path())] if global_config_path().is_file() else []
    if explicit is not None:
        layers.append(("config", explicit))
    elif env.is_git_repo and repo_config_path(env.repo_root).is_file():
        layers.append(("repo", repo_config_path(env.repo_root)))
    return layers


def _lint_block_only(layers: list[tuple[str, Path]]):
    """Resolve `lint_checks` from the raw layers, validating only that block (other blocks ignored)."""
    from .config import _deep_merge, read_yaml_file
    from .lint_checks_config import resolve, validate_block

    merged: dict = {}
    blocks: dict = {}
    for label, path in layers:
        data = read_yaml_file(path)
        if "lint_checks" in data:
            validate_block(data["lint_checks"], layer=label)
            blocks[label] = data["lint_checks"]
            merged = _deep_merge(merged, {"lint_checks": data["lint_checks"]})
    return resolve(merged, blocks)


def load_config(cwd: str, config_path: str | None):
    """Cascaded `lint_checks` block (defaults -> global -> repo rig.yaml), validated.

    The machine-wide commit hook runs this on every commit, so an error in an UNRELATED block (a key
    a newer rig added, another block's typo) must not block commits: when the full config fails to
    load but the `lint_checks` block itself validates, the error is a stderr warning and the block is
    resolved from the raw layers. An error inside `lint_checks` (or an unreadable file) stays fatal.
    """
    from .config import ConfigError, load
    from .detect import detect_environment
    from .lint_checks_config import resolve

    env = detect_environment(Path(cwd).resolve())
    explicit = None
    if config_path:
        cp = Path(config_path)
        explicit = (cp if cp.is_absolute() else env.repo_root / cp).resolve()
    try:
        loaded = load(env.repo_root, explicit_config=explicit,
                      include_repo=env.is_git_repo or explicit is not None)
        return resolve(loaded.data)
    except ConfigError as exc:
        cfg = _lint_block_only(_layer_files(env, explicit))
        print(f"warning: ignoring a config error outside lint_checks: {exc}", file=sys.stderr)
        return cfg


@dataclass
class Result:
    findings: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    incomplete: list[Incomplete] = field(default_factory=list)
    reports: dict[str, list[FunctionMetric]] = field(default_factory=dict)
    lines: dict[str, int] = field(default_factory=dict)
    considered: int = 0
    complexity_files: int = 0
    lines_files: int = 0
    targets: dict[str, list[lint_fix_else.Target]] = field(default_factory=dict)


def _finding(rule: str, severity: str, path: str, *, line: int | None, function: str | None,
             value: int, limit: int, fixable: bool, message: str) -> dict:
    return {"rule": rule, "severity": severity, "path": path, "line": line, "function": function,
            "value": value, "limit": limit, "fixable": fixable, "message": message}


def _complexity_findings(rel: str, functions: list[FunctionMetric], cfg,
                         fixable: set[tuple[tuple[str, ...], int]]) -> list[dict]:
    found: list[dict] = []
    for fn in functions:
        if fn.cognitive > cfg.max_cognitive:
            found.append(_finding(
                RULE_COGNITIVE, "error", rel, line=fn.line, function=fn.qualified,
                value=fn.cognitive, limit=cfg.max_cognitive, fixable=(fn.path, fn.line) in fixable,
                message=f"cognitive complexity {fn.cognitive} exceeds {cfg.max_cognitive}"))
        if fn.cyclomatic > cfg.max_cyclomatic:
            found.append(_finding(
                RULE_CYCLOMATIC, "error", rel, line=fn.line, function=fn.qualified,
                value=fn.cyclomatic, limit=cfg.max_cyclomatic, fixable=False,
                message=f"cyclomatic complexity {fn.cyclomatic} exceeds {cfg.max_cyclomatic}"))
    return found


def _line_limit(lines: int, cfg) -> tuple[str, int] | None:
    """(severity, limit) a file of `lines` lines breaches, or None when it is within limits."""
    if lines > cfg.error_above:
        return "error", cfg.error_above
    if lines > cfg.warn_above:
        return "warning", cfg.warn_above
    return None


def _check_lines(res: Result, rel: str, lines: int, cfg) -> None:
    res.lines_files += 1
    res.lines[rel] = lines
    breach = _line_limit(lines, cfg)
    if breach:
        severity, limit = breach
        res.findings.append(_finding(
            RULE_LINES, severity, rel, line=None, function=None, value=lines, limit=limit,
            fixable=False, message=f"{lines} lines exceeds the {severity} limit {limit}"))


def _wanted(rel: str, cfg) -> tuple[bool, bool]:
    """(file-lines applies, complexity applies) for a path under the effective config."""
    exempt = extension(rel) in set(cfg.exempt_extensions)
    return (cfg.file_lines_enabled and not exempt,
            cfg.complexity_enabled and analyzer_mod.complexity_eligible(rel))


def _needed(cfg) -> Callable[[str], bool]:
    return lambda rel: any(_wanted(rel, cfg))


def _record_skip(res: Result, scoped: ScopedFile, cfg) -> None:
    if scoped.skip != UNCHECKED:
        res.skipped.append({"path": scoped.rel, "check": "all", "reason": scoped.skip})
    elif cfg.file_lines_enabled:  # nothing else applies, so the only reason is the exemption
        res.skipped.append({"path": scoped.rel, "check": "file_lines", "reason": "exempt-extension"})


def _read_facts(scoped: ScopedFile, res: Result, wanted: tuple[bool, bool]) -> FileFacts | None:
    problem = scoped.incomplete
    if problem is None:
        try:
            return inspect_file(scoped.path)
        except OSError as exc:
            problem = f"cannot read: {exc.strerror or exc}"
    for check, applies in zip(("file_lines", "complexity"), wanted, strict=True):
        if applies:
            res.incomplete.append(Incomplete(scoped.rel, check, "unreadable", problem))
    return None


def _checkable(scoped: ScopedFile, res: Result, cfg, wanted: tuple[bool, bool]) -> FileFacts | None:
    """Facts of a file that can be checked; skips and unreadable files are recorded, not raised."""
    if scoped.skip:
        _record_skip(res, scoped, cfg)
        return None
    facts = _read_facts(scoped, res, wanted)
    if facts is not None and facts.binary:
        res.skipped.append({"path": scoped.rel, "check": "all", "reason": "binary"})
        return None
    return facts


def _inspect(scope: Scope, cfg) -> tuple[Result, list[ScopedFile]]:
    """Per-file facts: skips, unreadable files, file-line findings. -> (result, complexity files)."""
    res = Result()
    eligible: list[ScopedFile] = []
    for scoped in scope.files:
        wanted = _wanted(scoped.rel, cfg)
        facts = _checkable(scoped, res, cfg, wanted)
        if facts is None:
            continue
        res.considered += 1
        if cfg.file_lines_enabled and not wanted[0]:
            res.skipped.append({"path": scoped.rel, "check": "file_lines", "reason": "exempt-extension"})
        if wanted[0]:
            _check_lines(res, scoped.rel, facts.lines, cfg)
        if wanted[1]:
            eligible.append(scoped)
    return res, eligible


def _flagged_targets(rel: str, functions: list[FunctionMetric], cfg) -> list[lint_fix_else.Target]:
    """(line, name) of every over-limit function a `fix/else-after-return` rewrite may touch."""
    if extension(rel) not in lint_fix_else.FIXABLE_EXTENSIONS:
        return []
    return [(f.line, f.path[-1]) for f in functions if f.cognitive > cfg.max_cognitive]


def _fixable_functions(scope: Scope, rel: str, functions: list[FunctionMetric],
                       targets: list[lint_fix_else.Target],
                       trust_parser: bool) -> set[tuple[tuple[str, ...], int]]:
    """Functions with at least one qualifying site.

    Answering needs the target repository's own TypeScript module, i.e. executing project code, so
    only an explicit `--fix` (trust_parser) asks; an ordinary or staged scan never does and reports
    every finding as not (confirmed) fixable.
    """
    if not trust_parser or not targets or scope.snapshot is not None:
        return set()
    counts = lint_fix_else.probe(scope.root, rel, targets)
    if counts is None:
        return set()
    if len(counts) != len(targets):
        return set()
    occurrences = Counter((f.line, f.path[-1]) for f in functions)
    available = {target for target, count in zip(targets, counts)
                 if count and occurrences[target] == 1}
    return {(f.path, f.line) for f in functions if (f.line, f.path[-1]) in available}


def evaluate(scope: Scope, cfg, binary: str | None, *, trust_parser: bool = False) -> Result:
    res, eligible = _inspect(scope, cfg)
    res.complexity_files = len(eligible)
    if binary is not None and eligible:
        reports, bad = analyzer_mod.analyze(binary, eligible, scope.root)
        res.reports = reports
        res.incomplete.extend(bad)
        for rel in sorted(reports):
            targets = _flagged_targets(rel, reports[rel], cfg)
            if targets:
                res.targets[rel] = targets
            res.findings.extend(_complexity_findings(
                rel, reports[rel], cfg,
                _fixable_functions(scope, rel, reports[rel], targets, trust_parser)))
    return res


def _status(cfg_enabled: bool, applicable: int, res: Result, check: str, failing_rules: set[str]) -> str:
    if not cfg_enabled:
        return "disabled"
    if any(i.check == check for i in res.incomplete):
        return "incomplete"
    if any(f["rule"] in failing_rules and f["severity"] == "error" for f in res.findings):
        return "fail"
    return "pass" if applicable else "not-applicable"


def _exit_code(res: Result) -> int:
    if res.incomplete:
        return EXIT_INCOMPLETE
    if any(f["severity"] == "error" for f in res.findings):
        return EXIT_FINDINGS
    return EXIT_OK


def _analyzer_doc(path: str | None, *, verified: bool) -> dict:
    from .cccc_install import file_sha256

    if path is None:
        return {"name": "cccc", "version": None, "path": None, "sha256": None, "verified": False}
    return {"name": "cccc", "version": analyzer_mod.analyzer_version(path), "path": path,
            "sha256": file_sha256(Path(path)), "verified": verified}


def _config_doc(cfg) -> dict:
    return {
        "enabled": cfg.enabled,
        "complexity": {"enabled": cfg.complexity_enabled, "max_cognitive": cfg.max_cognitive,
                       "max_cyclomatic": cfg.max_cyclomatic},
        "file_lines": {"enabled": cfg.file_lines_enabled, "warn_above": cfg.warn_above,
                       "error_above": cfg.error_above,
                       "exempt_extensions": list(cfg.exempt_extensions)},
    }


def _finding_keys(findings: list[dict]) -> list[tuple]:
    """Stable identity (rule, path, function, nth duplicate): line/value shifts keep it."""
    counts: dict[tuple, int] = {}
    keys: list[tuple] = []
    for f in findings:
        base = (f["rule"], f["path"], f["function"])
        counts[base] = counts.get(base, 0) + 1
        keys.append((*base, counts[base]))
    return keys


def _metrics_drift(before: list[FunctionMetric], after: list[FunctionMetric] | None) -> str | None:
    """Only a lower-or-equal cognitive value on the same function set is an acceptable change."""
    if after is None:
        return "analysis of the rewritten file is unavailable"
    if [(f.path, f.kind) for f in before] != [(f.path, f.kind) for f in after]:
        return "function set or names changed"
    for old, new in zip(before, after, strict=True):
        if old.cyclomatic != new.cyclomatic:
            return f"cyclomatic complexity of {old.qualified} changed"
        if new.cognitive > old.cognitive:
            return f"cognitive complexity of {old.qualified} increased"
    return None


def _lines_drift(cfg, before: int | None, after: int | None) -> str | None:
    """A rewrite may add lines (else -> bare block); it must not push a file into a worse band,
    nor grow a file that already breaches a limit.

    `before` is None when the file-line check does not apply to this file (disabled or exempt).
    """
    if before is None:
        return None
    if after is None:
        return "line count of the rewritten file is unavailable"
    if _line_limit(before, cfg) != _line_limit(after, cfg):
        return f"file-lines severity changed ({before} -> {after} lines)"
    if _line_limit(before, cfg) is not None and after > before:
        return f"file-lines violation grew ({before} -> {after} lines)"
    return None


def _introduced(baseline: Result, final: Result, rel: str) -> str | None:
    """Any finding (of any rule) in the rewritten file that the baseline did not have."""
    known = set(_finding_keys(baseline.findings))
    for key, finding in zip(_finding_keys(final.findings), final.findings, strict=True):
        if finding["path"] == rel and key not in known:
            return f"introduced-by-fix: new {finding['rule']} {finding['severity']} ({finding['value']})"
    return None


def _improved(before: list[FunctionMetric], after: list[FunctionMetric]) -> bool:
    return any(new.cognitive < old.cognitive for old, new in zip(before, after, strict=True))


def _verify_rewrite(cfg, baseline: Result, final: Result, rel: str) -> str | None:
    """Why a rewrite must be reverted (None = keep): worse, drifted, or no effect at all."""
    failure = (_introduced(baseline, final, rel)
               or _metrics_drift(baseline.reports[rel], final.reports.get(rel))
               or _lines_drift(cfg, baseline.lines.get(rel), final.lines.get(rel)))
    if failure is None and not _improved(baseline.reports[rel], final.reports[rel]):
        return "no-effect: no flagged function got less complex"
    return failure


def _rewrite_candidates(root: str, plan: dict[str, list[lint_fix_else.Target]],
                        rewrites: list[lint_fix_else.Rewrite]) -> dict[str, str]:
    """Apply the scoped fixer file by file -> per-path reason for each skip.

    Successful rewrites are appended to `rewrites` as they happen so a mid-loop exception can
    still roll them back.
    """
    reasons: dict[str, str] = {}
    for rel, targets in plan.items():
        outcome = lint_fix_else.fix_file(root, rel, targets)
        if isinstance(outcome, lint_fix_else.Skipped):
            reasons[rel] = f"{outcome.reason}: {outcome.detail}" if outcome.detail else outcome.reason
        else:
            rewrites.append(outcome)
    return reasons


def _revert_unsafe(scope: Scope, cfg, baseline: Result, final: Result,
                   rewrites: list[lint_fix_else.Rewrite], reasons: dict[str, str]) -> list[dict]:
    """Undo every rewrite whose re-analysis is not an improvement; mutates `rewrites`/`reasons`."""
    reverted: list[dict] = []
    for rw in list(rewrites):
        failure = _verify_rewrite(cfg, baseline, final, rw.rel)
        if failure is None:
            continue
        problem = lint_fix_else.revert_file(scope.root, rw)
        rewrites.remove(rw)
        reasons[rw.rel] = f"reverted: {failure}"
        reverted.append({"path": rw.rel, "rule": lint_fix_else.RULE, "reason": failure,
                         "restored": problem is None, **({"detail": problem} if problem else {})})
    return reverted


def _rollback(root: str, rewrites: list[lint_fix_else.Rewrite]) -> None:
    """Best-effort restore of rewrites nobody verified (the run is dying on an exception)."""
    for rw in rewrites:
        problem = lint_fix_else.revert_file(root, rw)
        if problem:
            print(_printable(f"warning: could not roll back {rw.rel}: {problem}"), file=sys.stderr)


def _resolved_by_path(baseline: Result, final: Result) -> dict[str, list[dict]]:
    """Baseline findings absent from the re-run: the only ones that may be called resolved."""
    still = set(_finding_keys(final.findings))
    resolved: dict[str, list[dict]] = {}
    for key, finding in zip(_finding_keys(baseline.findings), baseline.findings, strict=True):
        if key not in still:
            resolved.setdefault(finding["path"], []).append(
                {"rule": finding["rule"], "function": finding["function"]})
    return resolved


def _unresolved(final: Result, rewritten: set[str], reasons: dict[str, str]) -> list[dict]:
    return [{"path": f["path"], "rule": f["rule"], "function": f["function"], "severity": f["severity"],
             "value": f["value"],
             "reason": reasons.get(f["path"]) or ("remaining-after-fix" if f["path"] in rewritten else "no-safe-fix")}
            for f in final.findings]


def _empty_fix(requested: bool) -> dict:
    return {"requested": requested, "applied": [], "reverted": [], "unresolved": [], "notes": [],
            "analyzer_rerun": False}


def _restage_notes(root: str, rewrites: list[lint_fix_else.Rewrite]) -> list[dict]:
    """A rewritten file whose index content differs from HEAD still has the pre-fix bytes staged."""
    staged = staged_paths(root)
    return [{"path": rw.rel, "state": NEEDS_RESTAGE,
             "message": "the index still holds the pre-fix content; stage the file yourself (rig never runs git add)"}
            for rw in rewrites if rw.rel in staged]


def _settle(scope: Scope, cfg, binary: str, baseline: Result, rewrites: list[lint_fix_else.Rewrite],
            reasons: dict[str, str]) -> tuple[Result, list[dict]]:
    """Re-run the analyzer on the rewritten tree and revert what is not a verified improvement.

    A revert that itself fails makes the run INCOMPLETE (exit 3): the file is then in a state
    nobody verified.
    """
    final = evaluate(scope, cfg, binary, trust_parser=True)
    reverted = _revert_unsafe(scope, cfg, baseline, final, rewrites, reasons)
    if reverted:
        final = evaluate(scope, cfg, binary, trust_parser=True)
        final.incomplete.extend(Incomplete(r["path"], "fix", "revert-failed", r["detail"])
                                for r in reverted if not r["restored"])
    return final, reverted


def _apply_fix(scope: Scope, cfg, binary: str, baseline: Result):
    """-> (final result, fix document, resolved count). Keeps only verified rewrites."""
    broken = {i.path for i in baseline.incomplete}
    plan = {rel: targets for rel, targets in sorted(baseline.targets.items()) if rel not in broken}
    mixed = sorted(set(plan) & partially_staged(scope.root))
    if mixed:
        raise ScopeError("--fix refused: partially staged changes in " + ", ".join(mixed))
    rewrites: list[lint_fix_else.Rewrite] = []
    final, reverted = baseline, []
    try:
        reasons = _rewrite_candidates(scope.root, plan, rewrites)
        if rewrites:
            final, reverted = _settle(scope, cfg, binary, baseline, rewrites, reasons)
    except BaseException:
        _rollback(scope.root, rewrites)
        raise
    resolved = _resolved_by_path(baseline, final)
    applied = [{"path": rw.rel, "rule": lint_fix_else.RULE, "sites": rw.count,
                "parser": f"typescript {rw.parser_version}", "resolved": resolved.get(rw.rel, [])}
               for rw in rewrites]
    doc = {**_empty_fix(True), "applied": applied, "reverted": reverted,
           "unresolved": _unresolved(final, {rw.rel for rw in rewrites}, reasons),
           "notes": _restage_notes(scope.root, rewrites), "analyzer_rerun": bool(rewrites)}
    return final, doc, sum(len(a["resolved"]) for a in applied)


def _document(cfg, res: Result, *, binary: str | None, exit_code: int, staged: bool,
              fix: dict | None, fixed: int, error: str | None = None, master_off: bool = False) -> dict:
    lines_status = _status(cfg.file_lines_enabled, res.lines_files, res, "file_lines", {RULE_LINES})
    complexity_status = _status(cfg.complexity_enabled, res.complexity_files, res, "complexity",
                                {RULE_COGNITIVE, RULE_CYCLOMATIC})
    if master_off:
        lines_status = complexity_status = "disabled"
    doc = {
        "schema": SCHEMA,
        "ok": exit_code == EXIT_OK,
        "exit_code": exit_code,
        "staged": staged,
        "analyzer": (_analyzer_doc(binary, verified=binary is not None)
                     if cfg.complexity_enabled and not master_off else None),
        "config": _config_doc(cfg),
        "checks": {"complexity": {"status": complexity_status}, "file_lines": {"status": lines_status}},
        "findings": res.findings,
        "skipped": res.skipped,
        "incomplete": [i.as_dict() for i in res.incomplete],
        "fix": fix or _empty_fix(False),
        "summary": {
            "files_considered": res.considered,
            "errors": sum(1 for f in res.findings if f["severity"] == "error"),
            "warnings": sum(1 for f in res.findings if f["severity"] == "warning"),
            "incomplete": len(res.incomplete),
            "fixed": fixed,
        },
    }
    if error is not None:
        doc["error"] = error
    return doc


def _text(doc: dict) -> str:
    out: list[str] = []
    for f in doc["findings"]:
        where = f["path"] + (f":{f['line']}" if f["line"] else "")
        what = f" {f['function']}:" if f["function"] else ""
        out.append(f"{where}: {f['severity']} {f['rule']}{what} {f['message']}")
    for i in doc["incomplete"]:
        out.append(f"{i['path']}: incomplete {i['check']} ({i['reason']}) {i['detail']}")
    for a in doc["fix"]["applied"]:
        out.append(f"{a['path']}: fixed {a['rule']} x{a['sites']} ({len(a['resolved'])} finding(s) resolved)")
    for r in doc["fix"]["reverted"]:
        out.append(f"{r['path']}: reverted {r['rule']} ({r['reason']})")
    for n in doc["fix"]["notes"]:
        out.append(f"{n['path']}: {n['state']} ({n['message']})")
    s, c = doc["summary"], doc["checks"]
    out.append(f"lint check: {s['errors']} error(s), {s['warnings']} warning(s), {s['incomplete']} incomplete, "
               f"{s['fixed']} fixed; complexity={c['complexity']['status']} "
               f"file_lines={c['file_lines']['status']} (exit {doc['exit_code']})")
    return "\n".join(out)


def _printable(text: str) -> str:
    """Escape what a UTF-8 stream cannot carry (a non-UTF-8 filename arrives as lone surrogates)."""
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def _emit(doc: dict, as_json: bool) -> None:
    # JSON stays ASCII (ensure_ascii) so a surrogate-escaped filename is still a valid document.
    print(json.dumps(doc, indent=2) if as_json else _printable(_text(doc)))


def _fail(args, message: str, code: int) -> int:
    """Usage/config failure: exit 2 with a machine-readable document under --json."""
    if getattr(args, "json", False):
        print(json.dumps({"schema": SCHEMA, "ok": False, "exit_code": code, "error": message,
                          "fix": _empty_fix(bool(getattr(args, "fix", False)))}, indent=2))
    print(_printable(f"error: {message}"), file=sys.stderr)
    return code


def run(args) -> int:
    cwd = getattr(args, "cwd", None) or "."
    staged, fix = bool(getattr(args, "staged", False)), bool(getattr(args, "fix", False))
    if staged and fix:
        return _fail(args, "--fix cannot be combined with --staged: staged content is checked from "
                           "the index and a fix must never restage files", EXIT_USAGE)
    try:
        cfg = load_config(cwd, getattr(args, "config", None))
    except (ValueError, RigError) as exc:
        return _fail(args, f"config error: {exc}", EXIT_USAGE)
    if not cfg.enabled:
        _emit(_document(cfg, Result(), binary=None, exit_code=EXIT_OK, staged=staged,
                        fix=_empty_fix(fix), fixed=0, master_off=True), args.json)
        return EXIT_OK
    if fix and not cfg.complexity_enabled:
        return _fail(args, "--fix needs the complexity check enabled (lint_checks.complexity.enabled)",
                     EXIT_USAGE)
    try:
        scope = build_scope(cwd, list(getattr(args, "paths", None) or []), staged=staged,
                            needed=_needed(cfg))
    except ScopeError as exc:
        return _fail(args, str(exc), EXIT_USAGE)
    try:
        return _run_scoped(args, cfg, scope, staged=staged, fix=fix)
    except ScopeError as exc:
        return _fail(args, str(exc), EXIT_USAGE)
    finally:
        scope.close()


def _run_scoped(args, cfg, scope: Scope, *, staged: bool, fix: bool) -> int:
    binary: str | None = None
    if cfg.complexity_enabled:
        try:
            binary = resolve_analyzer()
        except AnalyzerUnavailable as exc:
            hint = "" if INSTALL_HINT in str(exc) else f"; {INSTALL_HINT}"
            message = f"analyzer unavailable: {exc}{hint}"
            res = evaluate(scope, cfg, None)
            res.incomplete.append(Incomplete(ANALYZER_PATH, "complexity", "analyzer-unavailable", str(exc)))
            doc = _document(cfg, res, binary=None, exit_code=EXIT_ANALYZER, staged=staged,
                            fix=_empty_fix(fix), fixed=0, error=message)
            _emit(doc, args.json)
            print(_printable(f"error: {message}"), file=sys.stderr)
            return EXIT_ANALYZER
    res = evaluate(scope, cfg, binary, trust_parser=fix)
    fix_doc, fixed = _empty_fix(fix), 0
    if fix:
        res, fix_doc, fixed = _apply_fix(scope, cfg, binary, res)
    code = _exit_code(res)
    _emit(_document(cfg, res, binary=binary, exit_code=code, staged=staged, fix=fix_doc, fixed=fixed),
          args.json)
    return code
