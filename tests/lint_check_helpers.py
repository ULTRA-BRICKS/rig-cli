"""Shared helpers for the `rig lint check` engine tests.

Provides throwaway git repos, a configurable fake analyzer (a tiny script that emits canned
cccc-shaped JSON so every fail-closed coverage case can be exercised without the network), the
real pinned analyzer when its sha256 verifies, and a read-only TypeScript lookup for the fixer
tests. Nothing here touches the real HOME or installs anything.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from riglib import cccc_install, lint_check
from riglib.lint_checks_config import DEFAULTS, LintChecksConfig

# Extracted-binary sha256 of the pinned cccc v1.8.4 linux x86_64 musl release (contract section 1).
# A binary that does not hash to it is never used as "the real analyzer".
REAL_CCCC_SHA = "e09bc0ec6e9b8e55a57188b0137fdb38a11bba3cc115be8f6ef28df7f4a1d578"
# RIG_TEST_CCCC_BIN / RIG_TEST_TYPESCRIPT_DIR point at a real analyzer and a read-only TypeScript
# package (without RIG_TEST_CCCC_BIN the managed install under ~/.local/share is tried);
# RIG_LINT_REQUIRE_REAL=1 turns "not found" into a failure (CI), otherwise tests skip with the reason.


def make_cfg(**overrides) -> LintChecksConfig:
    return dataclasses.replace(DEFAULTS, **overrides)


def git(repo: Path, *args: str, input: bytes | None = None) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], input=input, capture_output=True, check=True)
    return proc.stdout.decode()


def make_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    git(repo, "config", "commit.gpgsign", "false")
    return repo


def put(repo: Path, rel: str, content: str | bytes) -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return path


def commit_all(repo: Path, message: str = "c") -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message, "--allow-empty")


def lines_source(n: int, *, final_newline: bool = True, eol: str = "\n") -> str:
    body = eol.join(f"// line {i}" for i in range(n))
    return body + (eol if final_newline and n else "")


def cognitive_source(n: int) -> str:
    return "function f(x) {\n" + "  if (x) { x = 1; }\n" * n + "}\n"


def cyclomatic_source(n: int) -> str:
    cases = "".join(f"    case {i}: break;\n" for i in range(n - 1))
    return "function g(x) {\n  switch (x) {\n" + cases + "  }\n}\n"


def _require_real() -> bool:
    return os.environ.get("RIG_LINT_REQUIRE_REAL") == "1"


def _managed_cccc() -> Path:
    """The pinned analyzer in the REAL user data dir (the test HOME is isolated, so ask the passwd db)."""
    import pwd

    return (Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local" / "share" / "rig" / "cccc"
            / f"v{cccc_install.PINNED_VERSION}" / "cccc")


def real_cccc() -> str | None:
    env = os.environ.get("RIG_TEST_CCCC_BIN")
    path = Path(env) if env else _managed_cccc()
    if path.is_file() and cccc_install.file_sha256(path) == REAL_CCCC_SHA:
        return str(path)
    return None


def require_real_cccc() -> str:
    path = real_cccc()
    if path is None:
        reason = "verified real cccc (sha256 pin) not found: set RIG_TEST_CCCC_BIN"
        if _require_real():
            pytest.fail(reason + " (RIG_LINT_REQUIRE_REAL=1)")
        pytest.skip(reason)
    return path


FAKE_SCRIPT = r'''#!{python}
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
spec = json.load(open(os.path.join(here, "fake.json")))
if sys.argv[1:] == ["--version"]:
    print(spec.get("version_out", "cccc 1.8.4")); sys.exit(0)
open(os.path.join(here, "calls.log"), "a").write(json.dumps(sys.argv[1:]) + "\n")
if spec.get("sleep"): time.sleep(spec["sleep"])
if spec.get("stderr"): sys.stderr.write(spec["stderr"])
if spec.get("raw") is not None:
    sys.stdout.write(spec["raw"]); sys.exit(spec.get("exit", 0))
files = [a for a in sys.argv[1:] if not a.startswith("--")]
records = []
for path in files:
    base = os.path.basename(path)
    if base in spec.get("omit", []):
        continue
    funcs = spec.get("functions", {{}}).get(base, [{{"name": "f", "kind": "function", "line": 1, "cognitive": 1, "cyclomatic": 1}}])
    shown = os.path.realpath(path) if spec.get("canonical") else path  # a canonicalizing analyzer
    rec = {{"path": shown, "cognitive": 0, "cyclomatic": 1, "functions": funcs}}
    rec.update(spec.get("record", {{}}).get(base, {{}}))
    records.append(rec)
    if base in spec.get("dup", []):
        records.append(dict(rec))
for extra in spec.get("extra", []):
    records.append({{"path": extra, "cognitive": 0, "cyclomatic": 1, "functions": []}})
doc = {{"files": records, "summary": {{"file_count": len(records)}}}}
doc.update(spec.get("doc", {{}}))
sys.stdout.write(json.dumps(doc)); sys.exit(spec.get("exit", 0))
'''


def fn(name: str = "f", cognitive: int = 1, cyclomatic: int = 1, line: int = 1, kind: str = "function",
       children: list | None = None) -> dict:
    out = {"name": name, "kind": kind, "line": line, "cognitive": cognitive, "cyclomatic": cyclomatic}
    if children is not None:
        out["children"] = children
    return out


class FakeAnalyzer:
    """A script analyzer whose output is driven by `spec` (see FAKE_SCRIPT)."""

    def __init__(self, directory: Path, **spec) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "cccc"
        self.spec_path = directory / "fake.json"
        self.path.write_text(FAKE_SCRIPT.format(python=sys.executable))
        self.path.chmod(0o755)
        self.configure(**spec)

    def configure(self, **spec) -> None:
        self.spec_path.write_text(json.dumps(spec))

    def calls(self) -> list[list[str]]:
        log = self.path.parent / "calls.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def use_analyzer(monkeypatch, path: str | Path) -> None:
    monkeypatch.setattr(lint_check, "resolve_analyzer", lambda: str(path))


def use_config(monkeypatch, cfg: LintChecksConfig | None = None, **overrides) -> LintChecksConfig:
    cfg = cfg or make_cfg(**overrides)
    monkeypatch.setattr(lint_check, "load_config", lambda cwd, config_path: cfg)
    return cfg


def args_for(repo: Path, *paths: str, json_out: bool = True, staged: bool = False, fix: bool = False,
             cwd: Path | None = None) -> argparse.Namespace:
    return argparse.Namespace(paths=list(paths), cwd=str(cwd or repo), config=None, json=json_out,
                              staged=staged, fix=fix)


def run_check(capsys, repo: Path, *paths: str, staged: bool = False, fix: bool = False,
              cwd: Path | None = None) -> tuple[int, dict]:
    code = lint_check.run(args_for(repo, *paths, staged=staged, fix=fix, cwd=cwd))
    out = capsys.readouterr().out
    return code, json.loads(out)


def rules(doc: dict, rule: str | None = None) -> list[dict]:
    return [f for f in doc["findings"] if rule is None or f["rule"] == rule]


def typescript_dir() -> str | None:
    env = os.environ.get("RIG_TEST_TYPESCRIPT_DIR")
    if env and os.path.isfile(os.path.join(env, "package.json")):
        return env
    return None


def require_typescript() -> str:
    found = typescript_dir()
    if found is None or shutil.which("node") is None:
        reason = "TypeScript compiler or node not found: set RIG_TEST_TYPESCRIPT_DIR"
        if _require_real():
            pytest.fail(reason + " (RIG_LINT_REQUIRE_REAL=1)")
        pytest.skip(reason)
    return found


def raw_name_dir(tmp_path: Path) -> Path | None:
    """A fresh directory on a filesystem that accepts non-UTF-8 file names, else None.

    Some CI runners mount a utf8only /tmp that refuses them with EILSEQ; the tmpfs in /dev/shm does not.
    """
    for base in (tmp_path, Path("/dev/shm")):
        if not base.is_dir():
            continue
        try:
            candidate = Path(tempfile.mkdtemp(dir=base))
        except OSError:  # unwritable or full: this base cannot host the repo either
            continue
        probe = os.fsencode(candidate) + b"/\xff"
        try:
            os.close(os.open(probe, os.O_WRONLY | os.O_CREAT, 0o600))
        except OSError:
            shutil.rmtree(candidate, ignore_errors=True)
            continue
        os.unlink(probe)
        return candidate
    return None


def require_raw_name_dir(tmp_path: Path) -> Path:
    found = raw_name_dir(tmp_path)
    if found is None:
        reason = "no writable filesystem accepts non-UTF-8 file names"
        if _require_real():
            pytest.fail(reason + " (RIG_LINT_REQUIRE_REAL=1)")
        pytest.skip(reason)
    return found


def link_typescript(repo: Path) -> None:
    """Expose the shared read-only TypeScript as the repo's node_modules/typescript."""
    modules = repo / "node_modules"
    modules.mkdir(exist_ok=True)
    link = modules / "typescript"
    if not link.exists():
        link.symlink_to(require_typescript(), target_is_directory=True)
    ignore = repo / ".gitignore"
    if not ignore.exists():
        ignore.write_text("node_modules/\n")


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def else_ifs(n: int, name: str = "f") -> str:
    """A function whose `n` single-line else-after-return sites give cognitive 2n (fixable to n)."""
    body = "".join(f"  if (x > {i}) {{ return {i}; }} else {{ x += 1; }}\n" for i in range(n))
    return f"function {name}(x) {{\n" + body + "  return x;\n}\n"
