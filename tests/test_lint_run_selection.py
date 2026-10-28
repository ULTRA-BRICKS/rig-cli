"""The execution selection (`--only` / `--skip` / `--scope`) is applied BEFORE overlap resolution.

A project script `lint: biome check .` covers the direct Biome checks in a default run. When the user
selects categories that leave the script out, a script that will not run must not keep claiming that
coverage: the direct Biome checks have to execute. (Seen on a real project: `--only lint,format,limits`
filtered the script and still demoted both Biome checks as `covered-by` the filtered script, so no Biome
check ran at all.)
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from riglib import lint_run as lr
from tests.lint_check_helpers import commit_all, make_repo, put
from tests.lint_run_helpers import FakeRunner, outcome, which_in, write_exe

RIG_CHECK_DOC = json.dumps({"schema": "rig-lint-check/v1", "ok": True, "summary": {"errors": 0, "warnings": 0}})


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    for name in ("CI", "GITHUB_ACTIONS", "RIG_LINT_DEPTH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RIG_LINT_TOOL_CACHE", str(tmp_path / "toolcache"))


def runner() -> FakeRunner:
    def answer(spec):
        if len(spec.argv) > 1 and spec.argv[1].endswith("lint_check_launcher.py"):
            return outcome(0, RIG_CHECK_DOC)
        return outcome(0)
    return FakeRunner(default=answer)  # type: ignore[arg-type]


@pytest.fixture
def biome_repo(tmp_path) -> Path:
    repo = make_repo(tmp_path)
    put(repo, "package.json", json.dumps({"scripts": {"lint": "biome check ."},
                                          "devDependencies": {"@biomejs/biome": "1.9.0"}}))
    put(repo, "biome.json", "{}\n")
    put(repo, "src/a.ts", "export const a = 1;\n")
    put(repo, ".gitignore", "node_modules\n")
    write_exe(repo / "node_modules" / ".bin" / "biome", "import sys; sys.exit(0)\n")
    commit_all(repo)
    return repo


def go(repo: Path, tmp_path: Path, **over):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    write_exe(bindir / "npm", "import sys; sys.exit(0)\n")
    base = dict(cwd=str(repo), config=None, json=True, plan=False, no_setup=True, fix=False, ci=None,
                only=(), skip=(), scopes=(), timeout=None)
    run = runner()
    environ = {"PATH": str(bindir), "HOME": os.environ["HOME"], "RIG_LINT_TOOL_CACHE": os.environ["RIG_LINT_TOOL_CACHE"]}
    report = lr.execute(lr.RunOptions(**{**base, **over}), environ=environ, runner=run, which=which_in(bindir))
    return report, run, {c["id"]: c for c in report.doc["checks"]}


SCRIPT = "script:package.json:lint@."


def test_default_run_keeps_the_script_and_demotes_only_the_identical_direct_checks(biome_repo, tmp_path):
    report, _, rows = go(biome_repo, tmp_path)
    assert rows[SCRIPT]["state"] == "passed"
    for biome in ("biome-lint@.", "biome-format@."):
        assert (rows[biome]["state"], rows[biome]["reason"]) == ("not-applicable", f"covered-by:{SCRIPT}")
    assert report.exit_code == 0


@pytest.mark.parametrize("over,runs,skipped", [
    ({"only": ("lint", "format", "limits")}, ("biome-lint@.", "biome-format@."), (SCRIPT,)),
    ({"only": ("lint",)}, ("biome-lint@.",), (SCRIPT, "biome-format@.")),
    ({"only": ("format",)}, ("biome-format@.",), (SCRIPT, "biome-lint@.")),
    ({"skip": ("scripts",)}, ("biome-lint@.", "biome-format@."), (SCRIPT,)),
])
def test_a_filtered_script_never_claims_coverage_so_direct_biome_checks_run(biome_repo, tmp_path, over, runs, skipped):
    report, run, rows = go(biome_repo, tmp_path, **over)
    for check_id in runs:
        assert (rows[check_id]["state"], rows[check_id]["reason"]) == ("passed", "ok"), (check_id, rows[check_id])
        assert rows[check_id]["argv"], "the direct check really executed"
    for check_id in skipped:
        assert (rows[check_id]["state"], rows[check_id]["reason"]) == ("not-applicable", "filtered")
    assert not any(r["reason"].startswith("covered-by:") for r in rows.values())
    executed = [argv for argv in run.argvs if argv[0].endswith("/biome")]
    assert len(executed) == len(runs)


def test_scope_selection_applies_before_overlaps_too(tmp_path):
    repo = make_repo(tmp_path)
    for pkg in ("web", "api"):
        put(repo, f"{pkg}/package.json", json.dumps({"scripts": {"lint": "biome check ."},
                                                     "devDependencies": {"@biomejs/biome": "1.9.0"}}))
        put(repo, f"{pkg}/biome.json", "{}\n")
        put(repo, f"{pkg}/src/a.ts", "export const a = 1;\n")
        write_exe(repo / pkg / "node_modules" / ".bin" / "biome", "import sys; sys.exit(0)\n")
    put(repo, ".gitignore", "node_modules\n")
    commit_all(repo)
    report, _, rows = go(repo, tmp_path, scopes=("web",))
    assert rows["script:package.json:lint@web"]["state"] == "passed"
    assert rows["script:package.json:lint@api"]["reason"] == "filtered"
    assert rows["biome-lint@api"]["reason"] == "filtered", "outside the scope: filtered, not covered-by a filtered script"


def test_filtered_script_rows_are_still_reported_not_dropped(biome_repo, tmp_path):
    _, _, rows = go(biome_repo, tmp_path, only=("lint",))
    assert SCRIPT in rows and rows[SCRIPT]["detail"].startswith("excluded by --only")


def test_selection_predicate_reports_why(biome_repo):
    from riglib.lint_providers import PlannedCheck, Unavailable
    from riglib.lint_run_plan import selection
    from tests.lint_run_helpers import planned

    opts = lr.RunOptions(cwd=".", config=None, json=True, plan=False, no_setup=True, fix=False, ci=None,
                         only=("lint",), skip=(), scopes=(), timeout=None)
    keep: PlannedCheck = planned(category="lint")
    drop: PlannedCheck = planned(category="format")
    excluded = selection(opts, ())
    assert excluded(keep) is None
    verdict = excluded(drop)
    assert isinstance(verdict, Unavailable) and (verdict.state, verdict.reason) == ("not-applicable", "filtered")
