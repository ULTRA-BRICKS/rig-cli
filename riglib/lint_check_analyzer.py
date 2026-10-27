"""Run the pinned `cccc` analyzer on an explicit file list and validate its output.

Why strict: upstream cccc exits 0 even when a file failed to parse and silently skips files it
does not understand, so an exit code proves nothing. rig therefore requires that the JSON names
EXACTLY the requested files (no missing, duplicate or unexpected record), carries integer metrics
on every (nested) function and reports no parse error; anything else is INCOMPLETE and fails the
check closed. Files are always passed explicitly with absolute paths (never a directory walk, so
nothing is excluded behind rig's back) after `--`-safe absolute prefixes (a name may start with
a dash), and `--no-config` keeps a stray cccc.toml from changing the thresholds or the file set.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass

from .lint_check_scope import ScopedFile, extension
from .lint_check_capture import OutputLimitError, capture

# Every extension the pinned cccc v1.8.4 routes to a front-end (README language table, each probed
# against the real binary). A missing entry is a fail-OPEN hole: a supported file would be reported
# `not-applicable` instead of measured, so tests probe the binary against this set in both
# directions. Any other file is `not-applicable` for the complexity check, not incomplete.
SUPPORTED_EXTENSIONS = frozenset({
    "c", "cc", "cjs", "cl", "clj", "cljc", "cljs", "cpp", "cs", "csx", "cts", "cxx", "dart", "el",
    "go", "h", "h++", "hh", "hpp", "hxx", "ipp", "java", "js", "jsx", "kt", "kts", "lisp", "lsp",
    "mjs", "mts", "php", "pl", "pm", "py", "pyi", "rb", "rkt", "rktd", "rktl", "rs", "sc", "scala",
    "scm", "sld", "ss", "swift", "t", "tpp", "ts", "tsx", "vb", "zig",
})
ARG_BYTES_PER_BATCH = 100_000
FILES_PER_BATCH = 1000
ANALYZER_TIMEOUT_SECONDS = 300
ANALYZER_OUTPUT_LIMIT = 64 * 1024 * 1024
ANALYZER_PATH = "<analyzer>"  # pseudo-path of an incomplete record that belongs to the analyzer run, not to a file
MAX_NESTING = 200


class AnalyzerUnavailable(Exception):
    """The analyzer is missing, unsupported on this platform or not byte-identical to the pin."""


@dataclass(frozen=True)
class FunctionMetric:
    path: tuple[str, ...]
    kind: str
    line: int
    cognitive: int
    cyclomatic: int

    @property
    def qualified(self) -> str:
        return ".".join(self.path)


@dataclass(frozen=True)
class Incomplete:
    path: str
    check: str
    reason: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "check": self.check, "reason": self.reason, "detail": self.detail}


def complexity_eligible(rel: str) -> bool:
    return extension(rel) in SUPPORTED_EXTENSIONS


class _Malformed(Exception):
    pass


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _Malformed(f"{label} is not a non-negative integer")
    return value


def _flatten(items: object, parent: tuple[str, ...], depth: int) -> list[FunctionMetric]:
    if not isinstance(items, list):
        raise _Malformed("functions is not a list")
    if depth > MAX_NESTING:
        raise _Malformed("function nesting too deep")
    flat: list[FunctionMetric] = []
    for item in items:
        if not isinstance(item, dict):
            raise _Malformed("function record is not an object")
        name, kind = item.get("name"), item.get("kind")
        if not isinstance(name, str) or not isinstance(kind, str):
            raise _Malformed("function name/kind is not a string")
        line = _integer(item.get("line"), "function line")
        if line < 1:
            raise _Malformed("function line < 1")
        path = (*parent, name)
        flat.append(FunctionMetric(path, kind, line, _integer(item.get("cognitive"), "cognitive"),
                                   _integer(item.get("cyclomatic"), "cyclomatic")))
        if "children" in item:
            flat.extend(_flatten(item["children"], path, depth + 1))
    return flat


def _record(rec: object) -> tuple[str, list[FunctionMetric], list[str]]:
    if not isinstance(rec, dict) or not isinstance(rec.get("path"), str):
        raise _Malformed("file record has no string path")
    _integer(rec.get("cognitive"), "file cognitive")
    _integer(rec.get("cyclomatic"), "file cyclomatic")
    errors = rec.get("parse_errors", [])
    if not isinstance(errors, list) or not all(isinstance(e, str) for e in errors):
        raise _Malformed("parse_errors is not a list of strings")
    return rec["path"], _flatten(rec.get("functions"), (), 0), errors


def _key(path: str, cwd: str) -> str:
    return os.path.normpath(path if os.path.isabs(path) else os.path.join(cwd, path))


def _fail_all(batch: list[ScopedFile], reason: str, detail: str) -> list[Incomplete]:
    return [Incomplete(f.rel, "complexity", reason, detail) for f in batch]


def _file_records(stdout: bytes) -> tuple[list, dict]:
    """(`files` list, summary) of a structurally sound cccc document (batch-wide defects raise)."""
    try:
        doc = json.loads(stdout)
    except ValueError as exc:
        raise _Malformed(f"output is not JSON: {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("files"), list):
        raise _Malformed("output has no files list")
    summary = doc.get("summary")
    if not isinstance(summary, dict):
        raise _Malformed("output has no summary object")
    count = _integer(summary.get("file_count"), "summary.file_count")
    if count != len(doc["files"]):
        raise _Malformed("summary.file_count does not match the files list")
    return doc["files"], summary


def _index_records(records: list, cwd: str):
    """-> (records seen per normalized path, parsed (functions, parse errors) per path)."""
    seen: dict[str, int] = {}
    parsed: dict[str, tuple[list[FunctionMetric], list[str]]] = {}
    for rec in records:
        try:
            path, functions, errors = _record(rec)
        except _Malformed as exc:
            raise _Malformed(f"malformed file record: {exc}") from exc
        key = _key(path, cwd)
        seen[key] = seen.get(key, 0) + 1
        parsed[key] = (functions, errors)
    return seen, parsed


def _reconcile(expected: dict[str, ScopedFile], seen: dict[str, int], parsed):
    """Match records to the requested files exactly -> (reports by rel path, incompletes)."""
    incompletes: list[Incomplete] = []
    for key, count in seen.items():
        if key not in expected:
            incompletes.append(Incomplete(key, "complexity", "unexpected-file",
                                          "analyzer reported a file that was not requested"))
        elif count > 1:
            incompletes.append(Incomplete(expected[key].rel, "complexity", "duplicate-file",
                                          f"analyzer reported this file {count} times"))
    reports: dict[str, list[FunctionMetric]] = {}
    for key, scoped in expected.items():
        if key not in seen:
            incompletes.append(Incomplete(scoped.rel, "complexity", "missing-from-output",
                                          "analyzer produced no record for this file"))
            continue
        if seen[key] != 1:
            continue
        functions, errors = parsed[key]
        if errors:
            incompletes.append(Incomplete(scoped.rel, "complexity", "parse-error", "; ".join(errors)[:300]))
        else:
            reports[scoped.rel] = functions
    return reports, incompletes


def _parse_error_fields(summary: dict) -> tuple[int, list[str]]:
    count, named = summary.get("parse_error_count", 0), summary.get("parse_error_files", [])
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise _Malformed("summary.parse_error_count is not a non-negative integer")
    if not isinstance(named, list) or not all(isinstance(p, str) for p in named):
        raise _Malformed("summary.parse_error_files is not a list of strings")
    return count, named


def _summary_parse_errors(summary: dict, expected: dict[str, ScopedFile], cwd: str,
                          reports: dict[str, list[FunctionMetric]], incompletes: list[Incomplete]) -> None:
    """Defense in depth: a summary that reports parse errors the file records do not carry fails closed.

    Files the summary names are turned into `parse-error` incompletes (their metrics are dropped);
    a non-zero count that no incomplete explains makes the whole batch malformed, and a count larger
    than the files explained adds one `parse-error-unexplained` incomplete naming the delta.
    """
    count, named = _parse_error_fields(summary)
    for key in (_key(p, cwd) for p in named):
        scoped = expected.get(key)
        if scoped is not None and reports.pop(scoped.rel, None) is not None:
            incompletes.append(Incomplete(scoped.rel, "complexity", "parse-error",
                                          "the analyzer summary reports a parse error for this file"))
    explained = len({i.path for i in incompletes if i.reason == "parse-error"})
    if (count or named) and not explained:
        raise _Malformed(f"summary reports {count or len(named)} parse error(s) that no file record carries")
    if count > explained:
        incompletes.append(Incomplete(ANALYZER_PATH, "complexity", "parse-error-unexplained",
                                      f"summary reports {count} parse error(s) but only {explained} file(s) carry one "
                                      f"({count - explained} unexplained)"))


def _validate(stdout: bytes, batch: list[ScopedFile], cwd: str):
    """-> (reports by rel path, incompletes). Raises _Malformed for a batch-wide defect."""
    records, summary = _file_records(stdout)
    seen, parsed = _index_records(records, cwd)
    expected = {os.path.normpath(f.path): f for f in batch}
    reports, incompletes = _reconcile(expected, seen, parsed)
    _summary_parse_errors(summary, expected, cwd, reports, incompletes)
    return reports, incompletes


def _batches(files: list[ScopedFile]) -> list[list[ScopedFile]]:
    batches: list[list[ScopedFile]] = []
    current: list[ScopedFile] = []
    used = 0
    for f in files:
        size = len(os.fsencode(f.path)) + 1
        if current and (used + size > ARG_BYTES_PER_BATCH or len(current) >= FILES_PER_BATCH):
            batches.append(current)
            current, used = [], 0
        current.append(f)
        used += size
    if current:
        batches.append(current)
    return batches


def _run_batch(binary: str, batch: list[ScopedFile], cwd: str):
    argv = [binary, "--no-config", "--no-cache", "--no-ignore", *[os.path.abspath(f.path) for f in batch]]
    try:
        proc = capture(argv, cwd=cwd, timeout=ANALYZER_TIMEOUT_SECONDS, limit=ANALYZER_OUTPUT_LIMIT)
    except OutputLimitError as exc:
        return {}, _fail_all(batch, "analyzer-output-limit", str(exc))
    except subprocess.TimeoutExpired:
        return {}, _fail_all(batch, "analyzer-timeout", f"no result within {ANALYZER_TIMEOUT_SECONDS}s")
    except OSError as exc:
        return {}, _fail_all(batch, "analyzer-failed", f"cannot execute analyzer: {exc}")
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()[:300]
        return {}, _fail_all(batch, "analyzer-failed", f"exit {proc.returncode}: {detail}")
    try:
        return _validate(proc.stdout, batch, cwd)
    except _Malformed as exc:
        return {}, _fail_all(batch, "malformed-output", str(exc))


def analyze(binary: str, files: list[ScopedFile], cwd: str):
    """Analyze `files` -> (reports by rel path, incompletes). Never walks directories."""
    reports: dict[str, list[FunctionMetric]] = {}
    incompletes: list[Incomplete] = []
    for batch in _batches(files):
        got, bad = _run_batch(binary, batch, cwd)
        reports.update(got)
        incompletes.extend(bad)
    return reports, incompletes


def analyzer_version(binary: str) -> str | None:
    try:
        proc = subprocess.run([binary, "--version"], capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    words = proc.stdout.decode("utf-8", "replace").split()
    return words[-1] if proc.returncode == 0 and words else None
