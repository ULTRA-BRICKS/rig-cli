"""The bootstrap-rendered Oxlint config must be accepted by a REAL pinned Oxlint binary.

String/JSON assertions cannot catch an unknown plugin id or a rule Oxlint rejects, so each framework
config is rendered through the real bootstrap plan and linted by the pinned `oxlint`. The binary comes
from (in order) `RIG_TEST_OXLINT`, the Rig tool cache, or a fresh pinned `npm install --prefix` into a
pytest temp dir. When none is obtainable (offline, no npm) the tests SKIP with that reason locally and FAIL
under RIG_LINT_REQUIRE_REAL=1 (CI), so a missing binary can never turn this check into a silent skip.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from riglib.lint_providers import PINS
from riglib.lint_tool_cache import find_cached_tool
from tests import lint_bootstrap_helpers
from tests.lint_real_helpers import agent_tools_source, session_cache, unobtainable

sandbox = lint_bootstrap_helpers.sandbox  # the fixture, registered under its own name in this module
_OXLINT = PINS["oxlint"]
_INSTALL_TIMEOUT_S = 240


def _install_pinned(prefix: Path) -> Path | None:
    npm = shutil.which("npm")
    if npm is None:
        return None
    argv = [npm, "install", "--prefix", str(prefix), "--ignore-scripts", "--no-audit", "--no-fund", f"oxlint@{_OXLINT.version}"]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=_INSTALL_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    exe = prefix / "node_modules" / ".bin" / "oxlint"
    return exe if done.returncode == 0 and exe.is_file() else None


def oxlint_binary(scratch: Path) -> Path:
    """The pinned real oxlint (``RIG_TEST_OXLINT``, the Rig cache, or a pinned npm install into ``scratch``).

    Unobtainable => fail under RIG_LINT_REQUIRE_REAL=1, otherwise skip with the reason.
    """
    explicit = os.environ.get("RIG_TEST_OXLINT")
    found = Path(explicit) if explicit else find_cached_tool("oxlint", {**os.environ, "RIG_LINT_TOOL_CACHE": str(session_cache())}, version=_OXLINT.version)
    if found is None or not found.is_file():
        found = _install_pinned(scratch)
    if found is None:
        unobtainable(f"pinned oxlint {_OXLINT.version} is unobtainable here (no RIG_TEST_OXLINT, no cache, npm install failed offline)")
    return found


@pytest.fixture(scope="session")
def real_oxlint(tmp_path_factory) -> Path:
    return oxlint_binary(tmp_path_factory.mktemp("oxlint"))


def _rendered_config(sandbox) -> str:
    plan, _ = sandbox.plan(node=(20, 19, 0))
    [action] = [a for a in plan.install_plan.actions if a.kind == "provision_linter_config"]
    assert action.options["rel_path"] == ".oxlintrc.json"
    return str(action.options["content"])


@pytest.mark.parametrize(
    ("deps", "source"),
    [
        ({}, "src/index.ts"),
        ({"react": "18.3.1"}, "src/App.tsx"),
        ({"next": "14.2.0", "react": "18.3.1"}, "app/page.tsx"),
        ({"vue": "3.4.0"}, "src/App.vue"),
        ({"nuxt": "3.11.0"}, "app.vue"),
        ({"svelte": "5.0.0"}, "src/App.svelte"),
    ],
)
def test_real_oxlint_accepts_the_framework_config(sandbox, real_oxlint, tmp_path, deps, source):
    sandbox.install_managers("npm")
    sandbox.package_json({"dependencies": deps})
    sandbox.write(source, "export const answer = 42;\n")
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / ".oxlintrc.json").write_text(_rendered_config(sandbox), encoding="utf-8")
    (project / "src" / "ok.ts").write_text("export const answer = 42;\n", encoding="utf-8")
    done = subprocess.run(
        [str(real_oxlint), "-c", ".oxlintrc.json", "src"], cwd=project, capture_output=True, text=True, timeout=120, check=False
    )
    assert done.returncode == 0, f"oxlint rejected the generated config:\n{done.stdout}\n{done.stderr}"
    assert "nknown" not in done.stderr + done.stdout


def test_real_oxlint_rejects_an_unknown_plugin_id(real_oxlint, tmp_path):
    """Guards the guard: if this passed, the acceptance test above would prove nothing."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "ok.ts").write_text("export const answer = 42;\n", encoding="utf-8")
    (tmp_path / ".oxlintrc.json").write_text('{"plugins": ["nuxt"]}\n', encoding="utf-8")
    done = subprocess.run(
        [str(real_oxlint), "-c", ".oxlintrc.json", "src"], cwd=tmp_path, capture_output=True, text=True, timeout=120, check=False
    )
    assert done.returncode != 0


def test_a_fresh_repo_is_not_green_on_a_correctness_finding_with_the_bootstrapped_config(sandbox, real_oxlint, tmp_path):
    """Oxlint on its own defaults reports a correctness finding as a WARNING and exits 0 (a false green)."""
    from riglib.lint_bootstrap import apply_bootstrap

    sandbox.install_managers("npm")
    sandbox.package_json()
    sandbox.write("src/bad.js", "debugger;\n")
    bare = subprocess.run([str(real_oxlint), "src"], cwd=sandbox.repo, capture_output=True, text=True, timeout=120, check=False)
    assert bare.returncode == 0 and "arning" in bare.stdout, "premise: the defaults only warn"

    plan, _ = sandbox.plan(node=(20, 19, 0))
    assert not apply_bootstrap(plan, root=sandbox.repo, environ={}).failed
    done = subprocess.run([str(real_oxlint), "src"], cwd=sandbox.repo, capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode == 1, done.stdout + done.stderr


_REAL_CATALOG = Path.home() / "xp" / "agent-tools"
_BAD = "export const x = {} as unknown as string;\ndebugger;\n"
_CLEAN = "export const x: string = \"ok\";\n"


def _policy_steps_only(plan):
    from riglib.plan import InstallPlan

    wanted = [(s, a) for s, a in zip(plan.steps, plan.install_plan.actions, strict=True) if s.id.startswith("policy-")]
    return InstallPlan(actions=[a for _, a in wanted], on_conflict="skip")


def test_real_policy_hosted_in_the_cache_flags_user_code_fixes_clean_and_leaves_managed_bytes_alone(sandbox, real_oxlint):
    """Fresh default config, real catalog bundle, real Oxlint: bad code fails, corrected code passes, nothing managed moves."""
    import hashlib

    from riglib.actions import run_plan
    from riglib.catalog import Catalog

    source = agent_tools_source() or _REAL_CATALOG
    if not (source / "vendor/anti-slop/skills/install-anti-slop/assets/anti-slop").is_dir():
        unobtainable(f"no real agent-tools catalog with the anti-slop carrier at {source}")
    real_npm = shutil.which("npm")
    if real_npm is None:
        unobtainable("npm is unobtainable here")
    (sandbox.bin_dir / "npm").symlink_to(real_npm)  # the planner only sees the sandbox PATH
    sandbox.package_json({"type": "module"})
    sandbox.write("src/bad.ts", _BAD)
    plan, _ = sandbox.plan(rules={}, catalog=Catalog(source=source), node=(22, 18, 0))
    report = run_plan(_policy_steps_only(plan))
    if any(r.status == "error" and "plugins" in r.detail for r in report.results):
        unobtainable("@oxlint/plugins is unobtainable here (offline)")
    assert all(r.status in ("created", "updated", "skipped") for r in report.results), [r.detail for r in report.results]
    [directory] = sorted((Path.home() / ".cache" / "rig" / "lint-tools" / "policy").iterdir())
    policy = directory / "oxlint.policy.json"

    def lint() -> subprocess.CompletedProcess:
        return subprocess.run([str(real_oxlint), "-c", str(policy), "src"], cwd=sandbox.repo, capture_output=True, text=True, timeout=120, check=False)

    bad = lint()
    assert bad.returncode == 1 and "no-debugger" in bad.stdout and "anti-slop(no-chained-type-assertions)" in bad.stdout, bad.stdout + bad.stderr
    assert "tools/oxlint" not in bad.stdout and not (sandbox.repo / "tools").exists(), "the bundle is not the user's code"
    sandbox.write("src/bad.ts", _CLEAN)
    fixed = lint()
    assert fixed.returncode == 0, fixed.stdout + fixed.stderr

    def managed_hashes() -> dict[str, str]:
        return {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(directory.rglob("*")) if p.is_file()
                and "node_modules" not in p.parts}

    before = managed_hashes()
    again = lint()
    assert again.returncode == 0 and managed_hashes() == before, "checks never rewrite the hosted policy or bundle"
    fix = subprocess.run([str(real_oxlint), "-c", str(policy), "--fix", "src"], cwd=sandbox.repo, capture_output=True, text=True, timeout=120, check=False)
    assert fix.returncode == 0 and managed_hashes() == before, "--fix never edits the hosted bundle bytes"


def _oxfmt_binary(scratch: Path) -> Path:
    """The pinned real oxfmt (``RIG_TEST_OXFMT``, the Rig cache, or a pinned npm install into ``scratch``)."""
    pin = PINS["oxfmt"]
    explicit = os.environ.get("RIG_TEST_OXFMT")
    found = Path(explicit) if explicit else find_cached_tool("oxfmt", {**os.environ, "RIG_LINT_TOOL_CACHE": str(session_cache())}, version=pin.version)
    npm = shutil.which("npm")
    if (found is None or not found.is_file()) and npm is not None:
        done = subprocess.run(
            [npm, "install", "--prefix", str(scratch), "--ignore-scripts", "--no-audit", "--no-fund", f"oxfmt@{pin.version}"],
            capture_output=True, text=True, timeout=_INSTALL_TIMEOUT_S, check=False,
        )
        exe = scratch / "node_modules" / ".bin" / "oxfmt"
        found = exe if done.returncode == 0 and exe.is_file() else None
    if found is None or not found.is_file():
        unobtainable(f"pinned oxfmt {pin.version} is unobtainable here (no RIG_TEST_OXFMT, no cache, npm install failed offline)")
    return found


@pytest.mark.parametrize("flavor", ["ts", "json"])
def test_every_generated_config_variant_passes_the_real_oxfmt_check(tmp_path, flavor):
    """The config Rig writes into a repo must pass that repo's own formatter check: no ignore file, no exemption."""
    from riglib.lint_policy import render_oxlint_config

    oxfmt = _oxfmt_binary(tmp_path / "tool")
    out = tmp_path / "configs"
    out.mkdir()
    count = 0
    for plugins in ((), ("react",), ("react", "nextjs"), ("vue",)):
        for type_aware in (False, True):
            for rules in ({}, {"groups": {"anti-slop": False}}, {"all": True}):
                count += 1
                (out / f"c{count}.{flavor}").write_text(
                    render_oxlint_config(rules, flavor=flavor, plugins=plugins, type_aware=type_aware), encoding="utf-8"
                )
    done = subprocess.run([str(oxfmt), "--check", str(out)], capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize(
    ("deps", "jsx", "passes"),
    [
        ({"react": "18.3.1"}, "react-jsx", True),
        ({"react": "18.3.1"}, "react", False),
        ({"react": "18.3.1"}, "preserve", False),
        ({"next": "15.0.0", "react": "18.3.1"}, "preserve", True),
        ({"next": "15.0.0", "react": "18.3.1"}, "react", False),
    ],
)
def test_bootstrapped_react_policy_follows_the_tsconfig_jsx_runtime(sandbox, real_oxlint, tmp_path, deps, jsx, passes):
    sandbox.install_managers("npm")
    sandbox.package_json({"dependencies": deps})
    sandbox.write("tsconfig.json", json.dumps({"compilerOptions": {"jsx": jsx}}))
    sandbox.write("src/App.tsx", "export const A = () => <div>hi</div>;\n")
    lines = _rendered_config(sandbox).splitlines()
    config = json.loads("\n".join(ln for ln in lines if not ln.startswith("//")))
    assert ("react/react-in-jsx-scope" in config["rules"]) is passes
    config.pop("jsPlugins", None)  # the anti-slop bundle lives in the tool cache, not in this sandbox
    config["rules"] = {k: v for k, v in config["rules"].items() if not k.startswith("anti-slop/")}
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / ".oxlintrc.json").write_text(json.dumps(config), encoding="utf-8")
    (project / "src" / "App.tsx").write_text("export const A = () => <div>hi</div>;\n", encoding="utf-8")
    done = subprocess.run(
        [str(real_oxlint), "-c", ".oxlintrc.json", "src"], cwd=project, capture_output=True, text=True, timeout=120, check=False
    )
    assert (done.returncode == 0) is passes, done.stdout + done.stderr
