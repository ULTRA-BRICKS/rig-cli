"""`fix/else-after-return`: the one semantics-preserving `rig lint check --fix` transform.

Removes an `else` whose then-branch unconditionally ends in a direct return/throw. The rewrite is
driven by the REAL TypeScript AST (the target repository's own `node_modules/typescript`, run by
node or bun through the embedded helper below): never regexes, so strings, templates, regexes and
comments are untouched. The else block keeps its braces (it becomes a bare block, so lexical
scope and TDZ behavior are identical); `else if (d) {..}` becomes a following `if`; a non-block,
non-declaration else statement simply follows. Only an `if` that is a direct statement of a
Block/SourceFile qualifies (an unbraced `if` nested in if/loop/label would change the dangling-else
binding), the then-part must be a direct return/throw or a block whose LAST statement is one, and an
unbraced then-part without `;` followed by a statement that could continue it (ASI) is skipped.

Scope: edits are restricted to the sites whose NEAREST enclosing function is one of the flagged
functions (matched by the analyzer's line + name), so every other function stays byte-identical.
`targets=None` is the unrestricted whole-file transform (transform tests only).

File handling is as paranoid as the contract demands: the parent directory is reached by walking
every component with `O_DIRECTORY|O_NOFOLLOW` from a root directory fd, and the read, the temp file
and the `os.replace` all go through that one directory fd, so a directory swapped for a symlink
mid-run can never redirect a write out of the repository. Parse errors before and after abort the
fix, the bytes are re-read and compared right before the atomic replace, and mode is preserved.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass

RULE = "fix/else-after-return"
FIXABLE_EXTENSIONS = frozenset({"ts", "tsx", "mts", "cts", "js", "jsx", "mjs", "cjs"})
HELPER_TIMEOUT_SECONDS = 120

HELPER_JS = r"""
const fs = require('fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const out = (o) => { process.stdout.write(JSON.stringify(o)); };
let ts;
try { ts = require(input.tsDir); } catch (e) { out({ ok: false, reason: 'parser-missing', detail: String(e && e.message || e) }); process.exit(0); }
const ext = input.fileName.split('.').pop().toLowerCase();
const kind = { tsx: ts.ScriptKind.TSX, jsx: ts.ScriptKind.JSX, js: ts.ScriptKind.JS, mjs: ts.ScriptKind.JS, cjs: ts.ScriptKind.JS }[ext] || ts.ScriptKind.TS;
const parse = (t) => ts.createSourceFile(input.fileName, t, ts.ScriptTarget.Latest, true, kind);
const errorsOf = (sf) => (sf.parseDiagnostics || []).map((d) => ({
  line: sf.getLineAndCharacterOfPosition(d.start || 0).line + 1,
  message: ts.flattenDiagnosticMessageText(d.messageText, '\n'),
}));
const declarationKinds = new Set([
  ts.SyntaxKind.VariableStatement, ts.SyntaxKind.FunctionDeclaration, ts.SyntaxKind.ClassDeclaration,
  ts.SyntaxKind.InterfaceDeclaration, ts.SyntaxKind.TypeAliasDeclaration, ts.SyntaxKind.EnumDeclaration,
  ts.SyntaxKind.ModuleDeclaration, ts.SyntaxKind.ImportEqualsDeclaration, ts.SyntaxKind.ImportDeclaration,
  ts.SyntaxKind.ExportDeclaration, ts.SyntaxKind.ExportAssignment,
]);
const exits = (s) => ts.isReturnStatement(s) || ts.isThrowStatement(s);
const unconditional = (s) => exits(s) || (ts.isBlock(s) && s.statements.length > 0 && exits(s.statements[s.statements.length - 1]));
const hazardStart = new Set(['(', '[', '+', '-', '/', '`', '*', '<']);
const isWs = (c) => /\s/.test(c);

const isFn = (n) => ts.isFunctionDeclaration(n) || ts.isFunctionExpression(n) || ts.isArrowFunction(n)
  || ts.isMethodDeclaration(n) || ts.isConstructorDeclaration(n) || ts.isGetAccessorDeclaration(n)
  || ts.isSetAccessorDeclaration(n);
const wrapper = (n) => ts.isVariableDeclaration(n) || ts.isVariableDeclarationList(n) || ts.isVariableStatement(n)
  || ts.isPropertyAssignment(n) || ts.isPropertyDeclaration(n) || ts.isParenthesizedExpression(n)
  || ts.isExportAssignment(n);
const lineOf = (sf, pos) => sf.getLineAndCharacterOfPosition(pos).line + 1;

// Function-like nodes get a document-order ordinal; edits never add or remove one, so the ordinal is
// a stable identity across the fixpoint passes (line numbers are not: the rewrite adds lines).
function ownersOf(sf, targets) {
  const owners = new Map();
  const claims = targets.map(() => 0);
  let ordinal = 0;
  const visit = (node) => {
    if (isFn(node)) {
      const id = ordinal++;
      const lines = new Set([lineOf(sf, node.getStart(sf))]);
      if (node.name) lines.add(lineOf(sf, node.name.getStart(sf)));
      for (let p = node.parent; p && wrapper(p); p = p.parent) lines.add(lineOf(sf, p.getStart(sf)));
      const named = node.name || (node.parent && node.parent.name);
      const name = named ? named.getText(sf) : '<anonymous>'; // the analyzer's name for an unnamed function
      const hit = targets.findIndex((t) => lines.has(t.line) && name === t.name);
      if (hit >= 0) { owners.set(id, hit); claims[hit]++; }
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  // Two functions matching one (line, name) target cannot be told apart: rewrite neither.
  for (const [id, hit] of [...owners]) if (claims[hit] > 1) owners.delete(id);
  return owners;
}

function sitesOf(sf, text, owners) {
  const edits = [];
  const enclosing = [];
  let ordinal = 0;
  const visit = (node) => {
    const fn = isFn(node);
    if (fn) enclosing.push(ordinal++);
    const owner = owners === null ? 0 : owners.get(enclosing[enclosing.length - 1]);
    if (owner !== undefined && ts.isIfStatement(node) && node.elseStatement
        && (ts.isBlock(node.parent) || ts.isSourceFile(node.parent))
        && unconditional(node.thenStatement) && !declarationKinds.has(node.elseStatement.kind)) {
      const thenEnd = node.thenStatement.end;
      const elseStart = node.elseStatement.getStart(sf);
      const hazard = !ts.isBlock(node.thenStatement) && text[thenEnd - 1] !== ';' && hazardStart.has(text[elseStart]);
      const tok = node.getChildren(sf).find((c) => c.kind === ts.SyntaxKind.ElseKeyword);
      if (!hazard && tok) {
        const start = tok.getStart(sf);
        let ls = start; while (ls > thenEnd && isWs(text[ls - 1])) ls--;
        let re = tok.end; while (re < text.length && isWs(text[re])) re++;
        const before = text.slice(ls, start);
        let replacement = before;
        if (!/[\r\n]/.test(before)) {
          const ifStart = node.getStart(sf);
          const lineStart = text.lastIndexOf('\n', ifStart - 1) + 1;
          const indent = text.slice(lineStart).match(/^[ \t]*/)[0];
          const nl = lineStart >= 2 && text[lineStart - 2] === '\r' ? '\r\n'
            : lineStart >= 1 ? '\n' : (text.includes('\r\n') ? '\r\n' : '\n');
          replacement = nl + indent;
        }
        edits.push({ start: ls, end: re, text: replacement, target: owner });
      }
    }
    ts.forEachChild(node, visit);
    if (fn) enclosing.pop();
  };
  visit(sf);
  return edits;
}

const original = input.text;
const targets = input.targets;
const firstSf = parse(original);
const firstErrors = errorsOf(firstSf);
if (firstErrors.length) { out({ ok: false, reason: 'parse-error', errors: firstErrors, version: ts.version }); process.exit(0); }
const owners = targets === null ? null : ownersOf(firstSf, targets);
const perTarget = (targets || []).map(() => 0);
if (input.dry) {
  const found = sitesOf(firstSf, original, owners);
  if (targets !== null) for (const e of found) perTarget[e.target] += 1;
  out({ ok: true, version: ts.version, count: found.length, perTarget, text: null });
  process.exit(0);
}
let text = original;
let count = 0;
for (let pass = 0; pass < 500; pass++) {
  const edits = sitesOf(parse(text), text, owners);
  if (!edits.length) break;
  edits.sort((a, b) => b.start - a.start);
  for (const e of edits) { text = text.slice(0, e.start) + e.text + text.slice(e.end); if (targets !== null) perTarget[e.target] += 1; }
  count += edits.length;
  const errs = errorsOf(parse(text));
  if (errs.length) { out({ ok: false, reason: 'post-fix-parse-error', errors: errs, version: ts.version }); process.exit(0); }
}
out({ ok: true, version: ts.version, count, perTarget, text: count ? text : null });
"""


Target = tuple[int, str]  # (analyzer line, function name) of one flagged function


@dataclass(frozen=True)
class Rewrite:
    """A successful, already-written rewrite (enough state to revert it)."""

    rel: str
    original: bytes
    written: bytes
    count: int
    parser_version: str
    mode: int


@dataclass(frozen=True)
class Skipped:
    rel: str
    reason: str
    detail: str


def find_runtime() -> str | None:
    return shutil.which("node") or shutil.which("bun")


def find_typescript(root: str, rel: str) -> str | None:
    """Nearest `node_modules/typescript` walking up from the file to the repository root."""
    current = os.path.dirname(os.path.join(root, rel))
    while True:
        candidate = os.path.join(current, "node_modules", "typescript")
        if os.path.isfile(os.path.join(candidate, "package.json")):
            return candidate
        if current == root or len(current) <= len(root):
            return None
        current = os.path.dirname(current)


def run_parser(runtime: str, ts_dir: str, rel: str, text: str,
               targets: Sequence[Target] | None = None, *, dry: bool = False) -> dict:
    """Invoke the embedded helper; returns its JSON verdict (module-level seam for tests).

    Runs in a private empty directory, never the repository: bun reads `./bunfig.toml` (a
    `preload` is code execution) and node/bun resolve project-relative config from the cwd.
    `ts_dir` is absolute, so the helper needs no repository cwd; bun's auto-install is off.
    """
    wanted = None if targets is None else [{"line": line, "name": name} for line, name in targets]
    payload = json.dumps({"tsDir": ts_dir, "fileName": rel, "text": text, "targets": wanted, "dry": dry})
    flags = ["--no-install"] if os.path.basename(runtime).startswith("bun") else []
    with tempfile.TemporaryDirectory(prefix="rig-fix-parser-") as neutral:
        proc = subprocess.run([runtime, *flags, "-e", HELPER_JS], input=payload.encode("ascii"), cwd=neutral,
                              capture_output=True, timeout=HELPER_TIMEOUT_SECONDS, check=False)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()[:300]
        return {"ok": False, "reason": "parser-failed", "detail": f"exit {proc.returncode}: {detail}"}
    try:
        verdict = json.loads(proc.stdout)
    except ValueError:
        return {"ok": False, "reason": "parser-failed", "detail": "helper output is not JSON"}
    return verdict if isinstance(verdict, dict) else {"ok": False, "reason": "parser-failed",
                                                     "detail": "helper output is not an object"}


_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _CLOEXEC


@contextmanager
def _parent_dir(root: str, rel: str) -> Iterator[tuple[int, str]]:
    """(directory fd, file name) of `rel`, every component opened `O_NOFOLLOW` from the root fd.

    A symlinked component fails the open instead of being followed, and everything done through
    the returned fd stays inside the directory that was actually opened (no name re-resolution).
    """
    parts = rel.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise OSError("unsafe relative path")
    fd = os.open(root, _DIR_FLAGS)  # `root` is already canonical (git toplevel realpath); a symlink refuses
    try:
        for part in parts[:-1]:
            deeper = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = deeper
        yield fd, parts[-1]
    finally:
        os.close(fd)


def _read_at(dir_fd: int, name: str) -> tuple[bytes, int]:
    fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise OSError("not a regular non-symlink file")
        return handle.read(), stat.S_IMODE(info.st_mode)


def _temp_name() -> str:
    """Name of the sibling temp file (module-level seam: tests interleave a directory swap here)."""
    return f".rig-fix-{secrets.token_hex(8)}"


def _replace_at(dir_fd: int, name: str, data: bytes, mode: int) -> None:
    """Temp file in the same directory + `os.replace`, all relative to `dir_fd`; keeps the mode."""
    tmp = _temp_name()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _CLOEXEC, 0o600, dir_fd=dir_fd)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(tmp, dir_fd=dir_fd)
        raise


def read_file(root: str, rel: str) -> tuple[bytes, int]:
    """Bytes and permission bits of a regular non-symlink file under `root` (raises OSError)."""
    with _parent_dir(root, rel) as (dir_fd, name):
        return _read_at(dir_fd, name)


def _tracked_by_git(root: str, ts_dir: str) -> bool:
    """True when git tracks the typescript dir (or its `node_modules` entry), or git cannot say.

    A `node_modules/typescript` committed to the repository is repo-supplied code, not an install
    product; it is never loaded. Fail closed: an unanswerable question counts as tracked.
    """
    modules = os.path.dirname(ts_dir)
    paths = [os.path.relpath(p, root) for p in (ts_dir, modules)]
    try:
        proc = subprocess.run(["git", "-C", root, "ls-files", "-z", "--", *paths],
                              capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return True
    return proc.returncode != 0 or bool(proc.stdout)


def _parser_for(root: str, rel: str) -> tuple[str, str] | Skipped:
    ts_dir = find_typescript(root, rel)
    if ts_dir is None:
        return Skipped(rel, "parser-missing", "no node_modules/typescript between the file and the repository root")
    if _tracked_by_git(root, ts_dir):
        return Skipped(rel, "parser-untrusted", "node_modules/typescript is tracked by git (repo-supplied code); never loaded")
    runtime = find_runtime()
    if runtime is None:
        return Skipped(rel, "parser-missing", "neither node nor bun is available to run the TypeScript parser")
    return runtime, ts_dir


def _verdict_skip(rel: str, verdict: dict) -> Skipped | None:
    if verdict.get("ok"):
        return None
    detail = verdict.get("detail") or json.dumps(verdict.get("errors", []))[:300]
    return Skipped(rel, str(verdict.get("reason", "parser-failed")), str(detail))


def probe(root: str, rel: str, targets: Sequence[Target]) -> list[int] | None:
    """Qualifying sites per flagged function (no write), or None when nothing could be determined."""
    found = _parser_for(root, rel)
    if isinstance(found, Skipped):
        return None
    runtime, ts_dir = found
    try:
        text = read_file(root, rel)[0].decode("utf-8")
        verdict = run_parser(runtime, ts_dir, rel, text, targets, dry=True)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    counts = verdict.get("perTarget")
    if not verdict.get("ok") or not isinstance(counts, list) or len(counts) != len(targets):
        return None
    return [int(n) if isinstance(n, int) and not isinstance(n, bool) else 0 for n in counts]


def _rewrite_in(root: str, rel: str, dir_fd: int, name: str, parser: tuple[str, str],
                targets: Sequence[Target] | None) -> Rewrite | Skipped:
    runtime, ts_dir = parser
    before, mode = _read_at(dir_fd, name)
    try:
        text = before.decode("utf-8")
    except UnicodeDecodeError:
        return Skipped(rel, "not-utf8", "file is not valid UTF-8")
    try:
        verdict = run_parser(runtime, ts_dir, rel, text, targets)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Skipped(rel, "parser-failed", str(exc))
    refused = _verdict_skip(rel, verdict)
    if refused is not None:
        return refused
    new_text = verdict.get("text")
    if not isinstance(new_text, str) or not verdict.get("count"):
        return Skipped(rel, "no-fixable-pattern", "no else-after-return site qualifies")
    written = new_text.encode("utf-8")
    if _read_at(dir_fd, name)[0] != before:
        return Skipped(rel, "modified-concurrently", "file changed while it was being parsed; not written")
    try:
        _replace_at(dir_fd, name, written, mode)
    except OSError as exc:
        return Skipped(rel, "write-failed", str(exc))
    return Rewrite(rel, before, written, int(verdict["count"]), str(verdict.get("version", "")), mode)


def fix_file(root: str, rel: str, targets: Sequence[Target] | None = None) -> Rewrite | Skipped:
    """Apply the transform in place to the sites inside `targets`, or explain why nothing changed."""
    parser = _parser_for(root, rel)
    if isinstance(parser, Skipped):
        return parser
    try:
        with _parent_dir(root, rel) as (dir_fd, name):
            return _rewrite_in(root, rel, dir_fd, name, parser, targets)
    except OSError as exc:
        return Skipped(rel, "unsafe-path", str(exc))


def revert_file(root: str, rewrite: Rewrite) -> str | None:
    """Restore the original bytes; returns an error string when it could not be done safely."""
    try:
        with _parent_dir(root, rewrite.rel) as (dir_fd, name):
            if _read_at(dir_fd, name)[0] != rewrite.written:
                return "file changed after the fix; left as is"
            _replace_at(dir_fd, name, rewrite.original, rewrite.mode)
    except OSError as exc:
        return str(exc)
    return None
