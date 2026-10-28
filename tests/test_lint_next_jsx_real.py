"""Next.js / non-Next React under a ``jsx: preserve`` tsconfig with the REAL Oxlint (rig-cli#789)."""

from __future__ import annotations

import json
import subprocess

import pytest

from tests.lint_check_helpers import commit_all, make_repo, put
from tests.lint_real_helpers import build_repo, lint_env, require_agent_tools_source, run_lint, session_cache, unobtainable

NEXT_DEPS = {"next": "14.2.0", "react": "18.3.1"}
REACT_DEPS = {"react": "18.3.1"}
TSX = "export const A = () => <div>hi</div>;\n"
PRESERVE = '{"compilerOptions": {"jsx": "preserve"}}\n'


def package(name, deps, *, oxlint=False):
    doc = {"name": name, "version": "1.0.0", "private": True, "dependencies": deps}
    if oxlint:
        doc["devDependencies"] = {"oxlint": "1.90.0"}
    return json.dumps(doc) + "\n"


@pytest.fixture
def env(tmp_path):
    return lint_env(tmp_path / "home", session_cache(), catalog=require_agent_tools_source())


def _findings(doc):
    return json.dumps([c.get("output") for c in doc["checks"]])


def _state(doc, check_id):
    [check] = [c for c in doc["checks"] if c["id"] == check_id]
    return check


def test_a_next_scope_with_preserve_is_not_flagged(tmp_path, env):
    repo = build_repo(
        tmp_path, "next",
        {"package.json": package("web", NEXT_DEPS), "tsconfig.json": PRESERVE, "src/A.tsx": TSX},
    )
    doc = run_lint(repo, env, "--only", "lint")
    assert doc["exit_code"] == 0, _findings(doc)


def test_each_package_of_a_monorepo_gets_its_own_verdict_next_passes_and_classic_is_flagged(tmp_path, env):
    repo = make_repo(tmp_path, "mono")
    put(repo, ".gitignore", "node_modules/\n")
    put(repo, "package.json", package("root", {}))
    for name, deps, source in (("web", NEXT_DEPS, "A.tsx"), ("classic", REACT_DEPS, "B.tsx")):
        put(repo, f"apps/{name}/package.json", package(name, deps, oxlint=True))
        put(repo, f"apps/{name}/tsconfig.json", PRESERVE)
        put(repo, f"apps/{name}/src/{source}", TSX)
        locked = subprocess.run(
            ["npm", "install", "--package-lock-only", "--ignore-scripts", "--no-audit", "--no-fund"],
            cwd=repo / "apps" / name, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL, check=False,
        )
        if locked.returncode != 0:
            unobtainable(f"npm could not lock apps/{name}: {locked.stderr[-300:]}")
    commit_all(repo)
    doc = run_lint(repo, env, "--only", "lint")
    web, classic = _state(doc, "oxlint@apps/web"), _state(doc, "oxlint@apps/classic")
    assert web["state"] == "passed", _findings(doc)
    assert classic["state"] == "failed" and "react-in-jsx-scope" in classic["output"]["stdout"], _findings(doc)
    assert "B.tsx" in classic["output"]["stdout"] and doc["exit_code"] == 1


def test_a_nested_next_app_never_hides_a_missing_react_import_in_its_non_next_sibling(tmp_path, env):
    # One root config governs the whole subtree: the Next evidence must come from the scope itself, never from a
    # nested package, or the shared `jsx: preserve` would silence the classic-runtime sibling too.
    repo = build_repo(
        tmp_path, "shared",
        {
            "package.json": package("root", REACT_DEPS),
            "tsconfig.json": PRESERVE,
            "apps/web/package.json": package("web", NEXT_DEPS),
            "apps/web/src/A.tsx": TSX,
            "apps/classic/package.json": package("classic", REACT_DEPS),
            "apps/classic/src/B.tsx": TSX,
        },
    )
    doc = run_lint(repo, env, "--only", "lint")
    text = _findings(doc)
    assert doc["exit_code"] == 1 and "react-in-jsx-scope" in text and "B.tsx" in text, text
