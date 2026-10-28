"""Bootstrap steps that install outside the repository: the Rig tool cache, pinned release binaries, rustup components.

Covers the pip-only / manifest-less / orphan-script routes (no manifest, no lockfile, CI-safe), the
sha256-pinned system linters and the rustup clippy/rustfmt step. The interpreter, npm, cargo and rustup
are shims on a controlled PATH and the release download is a stub, so nothing touches the network, a
real toolchain or the real HOME.
"""

from __future__ import annotations

import hashlib
import io
import stat
import subprocess
import sys
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest

from riglib import lint_release_tools as release
from riglib.actions import run_plan
from riglib.lint_bootstrap import apply_bootstrap, build_bootstrap_plan
from riglib.lint_discovery import discover
from riglib.lint_locked_versions import locked_version
from riglib.lint_providers import BootstrapNeed, bootstrap_needs, plan_checks
from riglib.lint_release_tools import RELEASE_VERSIONS, ReleaseAsset
from riglib.lint_run_config import resolve
from riglib.plan import Action, InstallPlan
from tests.lint_bootstrap_helpers import NO_SLOP, OXFMT, OXLINT, RUFF, sandbox  # noqa: F401 -- fixture import

_FAKE_PYTHON = f'''#!{sys.executable}
import json, os, pathlib, shutil, stat, sys

me = pathlib.Path(sys.argv[0])
args = sys.argv[1:]
if args[:1] == ["-c"]:  # capability probe (`import ensurepip` / `import pip`): answered, never logged
    missing = os.environ.get("SHIM_MISSING_MODULES", "").split(",")
    sys.exit(1 if args[1].split()[-1] in missing else 0)
with open(os.environ["SHIM_LOG"], "a") as fh:
    fh.write(json.dumps([me.name, *args]) + "\\n")
if args[:2] == ["-m", "venv"]:
    target = pathlib.Path(args[2]) / "bin"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy(me, target / "python")
elif args[:2] == ["-m", "pip"]:
    exe = me.parent / args[-1].split("==")[0]
    exe.write_text("#!/bin/sh\\nexit 0\\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
'''

_FAKE_UV = f'''#!{sys.executable}
import json, os, pathlib, shutil, stat, sys

args = sys.argv[1:]
with open(os.environ["SHIM_LOG"], "a") as fh:
    fh.write(json.dumps(["uv", *args]) + "\\n")
if args[:1] == ["venv"]:
    target = pathlib.Path(args[-1]) / "bin"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy(args[args.index("--python") + 1], target / "python")
elif args[:2] == ["pip", "install"]:
    python = pathlib.Path(args[args.index("--python") + 1])
    exe = python.parent / args[-1].split("==")[0]
    exe.write_text("#!/bin/sh\\nexit 0\\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
else:
    sys.exit(64)
'''

_FAKE_NPM = f'''#!{sys.executable}
import json, os, pathlib, stat, sys

args = sys.argv[1:]
with open(os.environ["SHIM_LOG"], "a") as fh:
    fh.write(json.dumps(["npm", *args]) + "\\n")
prefix = pathlib.Path(args[args.index("--prefix") + 1])
for spec in [a for a in args if not a.startswith("-") and "@" in a]:
    exe = prefix / "node_modules" / ".bin" / spec.rsplit("@", 1)[0]
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\\nexit 0\\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
'''

_FAKE_CARGO = f'''#!{sys.executable}
import json, os, pathlib, sys

if os.environ.get("RUSTUP_AUTO_INSTALL") != "0":  # a real rustup proxy would download the repo-pinned toolchain here
    print("error: rustup would auto-install a toolchain", file=sys.stderr)
    sys.exit(97)
with open(os.environ["SHIM_LOG"], "a") as fh:
    fh.write(json.dumps(["cargo", *sys.argv[1:], os.getcwd()]) + "\\n")
sys.exit(0 if (pathlib.Path(os.environ["SHIM_STATE"]) / "components").exists() else 1)
'''

_FAKE_RUSTUP = f'''#!{sys.executable}
import json, os, pathlib, sys

if os.environ.get("RUSTUP_AUTO_INSTALL") != "0":
    print("error: rustup would auto-install a toolchain", file=sys.stderr)
    sys.exit(97)
with open(os.environ["SHIM_LOG"], "a") as fh:
    fh.write(json.dumps(["rustup", *sys.argv[1:], os.getcwd()]) + "\\n")
(pathlib.Path(os.environ["SHIM_STATE"]) / "components").write_text("clippy rustfmt")
'''

_BINARY = b"#!/bin/sh\nexit 0\n"
_SHELLCHECK_URL = "https://example.invalid/shellcheck.tar.gz"
_SHFMT_URL = "https://example.invalid/shfmt"


def _shim(sandbox, name: str, source: str) -> None:
    path = sandbox.bin_dir / name
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _cache() -> Path:
    return Path.home() / ".cache" / "rig" / "lint-tools"


def _tar(member: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo(member)
        info.size, info.mode = len(_BINARY), 0o755
        tar.addfile(info, io.BytesIO(_BINARY))
    return buffer.getvalue()


@pytest.fixture
def python_cache(sandbox):
    _shim(sandbox, "python3", _FAKE_PYTHON)
    return sandbox


def _pip_only(sandbox) -> None:
    sandbox.write("requirements.txt", "requests==2.0\n")
    sandbox.write("app.py", "x = 1\n")


# ── Python without a lock-capable manager: pinned venv in the Rig cache ─────────────


def test_pip_only_repo_installs_ruff_into_the_cache_then_is_idempotent(python_cache):
    _pip_only(python_cache)
    before = python_cache.tree()
    plan, _ = python_cache.plan()
    slug = f"ruff-{RUFF.version}"
    assert [s.id for s in plan.steps] == [f"py-cache-venv@{slug}", f"py-cache-install@{slug}"]
    pip = plan.steps[1].argv
    assert pip[1:] == ("-m", "pip", "install", "--only-binary", ":all:", "--disable-pip-version-check", "--no-input", f"ruff=={RUFF.version}")
    assert python_cache.calls() == [] and python_cache.tree() == before, "planning spawns and writes nothing"

    outcome = apply_bootstrap(plan, root=python_cache.repo, environ={})
    assert not outcome.failed and [state for _, state, _ in outcome.results] == ["applied", "applied"]
    assert (_cache() / "py" / slug / "bin" / "ruff").is_file()
    assert python_cache.tree() == before, "no manifest, lockfile or config was written into the repo"

    again, _ = python_cache.plan()
    calls = len(python_cache.calls())
    assert again.is_empty() and apply_bootstrap(again, root=python_cache.repo, environ={}).results == ()
    assert len(python_cache.calls()) == calls, "a bootstrapped cache spawns nothing"


def test_cache_steps_run_under_ci(python_cache):
    _pip_only(python_cache)
    plan, _ = python_cache.plan()
    outcome = apply_bootstrap(plan, root=python_cache.repo, environ={"CI": "true"})
    assert not outcome.failed and {state for _, state, _ in outcome.results} == {"applied"}


def test_ci_refuses_manifest_steps_but_still_installs_the_cache_tool(python_cache):
    python_cache.install_managers("npm")
    python_cache.package_json()
    python_cache.write("scripts/tool.py", "import os\n")
    plan, _ = python_cache.plan()
    kinds = {s.kind for s in plan.steps}
    assert {"add-dev-packages", "install-tool-cache"} <= kinds
    before = python_cache.tree()
    outcome = apply_bootstrap(plan, root=python_cache.repo, environ={"CI": "true"})
    states = {step_id.split("@")[0]: state for step_id, state, _ in outcome.results}
    assert states["js-add-dev"] == "blocked" and states["py-cache-install"] == "applied"
    assert outcome.failed, "a refused manifest step is still reported as not bootstrapped"
    assert python_cache.tree() == before


def test_version_override_selects_its_own_cache_slot(python_cache):
    _pip_only(python_cache)
    plan, _ = python_cache.plan(lint={"bootstrap": {"versions": {"ruff": "0.17.2"}}})
    assert [s.id for s in plan.steps][0] == "py-cache-venv@ruff-0.17.2"
    assert plan.steps[0].cache_dir.endswith("py/ruff-0.17.2")


def test_a_failed_pip_install_leaves_the_failure_visible_and_is_retried(python_cache):
    _pip_only(python_cache)
    plan, _ = python_cache.plan()
    broken = _FAKE_PYTHON.replace('exe = me.parent / args[-1].split("==")[0]', "sys.exit(7)")
    _shim(python_cache, "python3", broken)
    outcome = apply_bootstrap(plan, root=python_cache.repo, environ={})
    assert outcome.failed and [s for _, s, _ in outcome.results] == ["applied", "failed"]
    retry, _ = python_cache.plan()
    assert [s.id for s in retry.steps] == [f"py-cache-install@ruff-{RUFF.version}"], "venv is kept, only pip reruns"


# ── interpreter without ensurepip (Debian/Ubuntu python3 without python3-venv/pip) ──


@pytest.fixture
def no_ensurepip(python_cache, monkeypatch):
    monkeypatch.setenv("SHIM_MISSING_MODULES", "ensurepip,pip")
    _pip_only(python_cache)
    return python_cache


def test_without_ensurepip_uv_creates_the_venv_and_installs_without_a_project(no_ensurepip):
    sandbox = no_ensurepip
    _shim(sandbox, "uv", _FAKE_UV)
    before = sandbox.tree()
    plan, _ = sandbox.plan()
    slug = f"ruff-{RUFF.version}"
    venv_dir = str(_cache() / "py" / slug)
    assert [s.id for s in plan.steps] == [f"py-cache-venv@{slug}", f"py-cache-install@{slug}"]
    venv, install = (s.argv for s in plan.steps)
    assert venv[1:] == ("venv", "--allow-existing", "--python", str(sandbox.bin_dir / "python3"), venv_dir)
    assert install[1:] == ("pip", "install", "--python", f"{venv_dir}/bin/python", "--only-binary", ":all:", f"ruff=={RUFF.version}")
    assert not {"sync", "run", "add", "--project"} & set(venv + install), "no project, no sync"
    assert sandbox.calls() == [] and sandbox.tree() == before, "planning only probes; nothing is spawned for real"

    outcome = apply_bootstrap(plan, root=sandbox.repo, environ={"CI": "1"})
    assert not outcome.failed and [s for _, s, _ in outcome.results] == ["applied", "applied"]
    assert (_cache() / "py" / slug / "bin" / "ruff").is_file()
    done = len(sandbox.calls())
    assert [c[0] for c in sandbox.calls()] == ["uv", "uv"] and sandbox.plan()[0].is_empty()
    assert len(sandbox.calls()) == done, "second run spawns nothing"
    assert sandbox.tree() == before


def test_without_ensurepip_and_without_uv_the_state_is_blocked_with_a_remediation(no_ensurepip):
    sandbox = no_ensurepip
    before = sandbox.tree()
    plan, _ = sandbox.plan()
    assert plan.is_empty()
    [blocked] = plan.blocked
    assert (blocked.reason, blocked.tool) == ("manager-not-installed", "ruff")
    for hint in ("ensurepip", "uv", "python3-venv", "sudo/apt"):
        assert hint in blocked.detail
    assert sandbox.calls() == [] and sandbox.tree() == before
    assert not (_cache() / "py").exists()


def test_a_half_created_venv_directory_is_repaired_idempotently(python_cache):
    sandbox = python_cache
    _pip_only(sandbox)
    half = _cache() / "py" / f"ruff-{RUFF.version}"
    (half / "lib").mkdir(parents=True)  # crashed earlier run: directory exists, no bin/python, no ruff
    plan, _ = sandbox.plan()
    assert [s.id for s in plan.steps] == [f"py-cache-venv@ruff-{RUFF.version}", f"py-cache-install@ruff-{RUFF.version}"]
    assert not apply_bootstrap(plan, root=sandbox.repo, environ={}).failed
    assert (half / "bin" / "ruff").is_file() and (half / "lib").is_dir()
    assert sandbox.plan()[0].is_empty()


def test_a_venv_without_pip_is_repaired_through_uv_not_trusted(no_ensurepip):
    sandbox = no_ensurepip
    _shim(sandbox, "uv", _FAKE_UV)
    venv = _cache() / "py" / f"ruff-{RUFF.version}" / "bin"
    venv.mkdir(parents=True)
    python = venv / "python"
    python.write_text(_FAKE_PYTHON, encoding="utf-8")
    python.chmod(0o755)  # created earlier with --without-pip: python exists, pip does not
    plan, _ = sandbox.plan()
    assert [s.id for s in plan.steps] == [f"py-cache-install@ruff-{RUFF.version}"] and plan.steps[0].manager == "uv"


def test_with_ensurepip_the_pip_route_is_kept_even_when_uv_exists(python_cache):
    _shim(python_cache, "uv", _FAKE_UV)
    _pip_only(python_cache)
    plan, _ = python_cache.plan()
    assert {s.manager for s in plan.steps} == {"pip"}


# ── a heavy / unreachable application runtime must never be installed just to lint ──

_FAILING_MANAGER = f'''#!{sys.executable}
import json, os, sys

with open(os.environ["SHIM_LOG"], "a") as fh:
    fh.write(json.dumps([os.path.basename(sys.argv[0]), *sys.argv[1:]]) + "\\n")
print("error: no solution found: torch==9999 is not available", file=sys.stderr)
sys.exit(9)
'''
_HEAVY_PYPROJECT = '[project]\nname = "hcb"\nversion = "0.1.0"\ndependencies = ["torch==9999", "ntgcalls"]\n'


def _heavy_project(sandbox, *, lock: str = "uv.lock") -> None:
    for manager in ("uv", "poetry", "pdm"):
        _shim(sandbox, manager, _FAILING_MANAGER)
    sandbox.write("pyproject.toml", _HEAVY_PYPROJECT)
    sandbox.write(lock, "version = 1\n")
    sandbox.write("hcb/app.py", "import os\n")


@pytest.mark.parametrize("lock", ["uv.lock", "poetry.lock", "pdm.lock"])
def test_ruff_is_provisioned_and_run_without_syncing_the_application(python_cache, lock):
    sandbox = python_cache
    _heavy_project(sandbox, lock=lock)
    before = sandbox.tree()
    plan, env = sandbox.plan()
    managers = {"uv", "poetry", "pdm"}
    assert plan.steps and not plan.blocked
    for step in plan.steps:
        assert step.kind == "install-tool-cache"
        assert Path(step.argv[0]).name not in managers and not {"sync", "add", "run"} & set(step.argv)
    assert not apply_bootstrap(plan, root=sandbox.repo, environ={}).failed

    cfg = resolve({"linters": {"rules": NO_SLOP}})
    discovery = discover(sandbox.repo)
    [ruff] = [c for c in plan_checks(discovery, cfg, env) if c.provider == "ruff-check"]
    assert ruff.argv is not None and Path(ruff.argv[0]).parent == _cache() / "py" / f"ruff-{RUFF.version}" / "bin"
    assert not managers & set(ruff.argv) and not {"sync", "run"} & set(ruff.argv)
    done = subprocess.run(list(ruff.argv), cwd=sandbox.repo, capture_output=True, text=True, check=False)
    assert done.returncode == 0
    assert [c[0] for c in sandbox.calls() if c[0] in managers] == [], "no manager was ever invoked"
    assert sandbox.tree() == before, "pyproject.toml, the lock and .venv are untouched"


def test_a_declared_ruff_keeps_the_projects_locked_version_in_the_cache(python_cache):
    sandbox = python_cache
    _heavy_project(sandbox)
    sandbox.write("pyproject.toml", _HEAVY_PYPROJECT + '\n[dependency-groups]\ndev = ["ruff==0.6.9"]\n')
    sandbox.write("uv.lock", '[[package]]\nname = "torch"\nversion = "9999"\n\n[[package]]\nname = "ruff"\nversion = "0.6.9"\n')
    plan, _ = sandbox.plan()
    assert [s.id for s in plan.steps] == ["py-cache-venv@ruff-0.6.9", "py-cache-install@ruff-0.6.9"]
    assert plan.steps[1].argv[-1] == "ruff==0.6.9"
    assert not apply_bootstrap(plan, root=sandbox.repo, environ={"CI": "true"}).failed
    assert sandbox.plan()[0].is_empty()


def test_a_ruff_already_in_the_project_venv_is_used_directly_and_nothing_is_installed(python_cache):
    sandbox = python_cache
    _heavy_project(sandbox)
    sandbox.write("pyproject.toml", _HEAVY_PYPROJECT + '\n[dependency-groups]\ndev = ["ruff==0.6.9"]\n')
    exe = sandbox.write(".venv/bin/ruff", "#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    plan, _ = sandbox.plan()
    assert plan.is_empty() and not plan.blocked


def test_locked_version_reads_each_lock_flavour_without_a_toml_parser(tmp_path):
    (tmp_path / "poetry.lock").write_text('[[package]]\nname = "Ruff"\nversion = "0.5.0"\n', encoding="utf-8")
    assert locked_version(tmp_path, "ruff") == "0.5.0"
    (tmp_path / "poetry.lock").unlink()
    (tmp_path / "requirements-dev.txt").write_text("black==24.1\nRuff == 0.4.2 ; python_version > '3.8'\n", encoding="utf-8")
    assert locked_version(tmp_path, "ruff") == "0.4.2"
    assert locked_version(tmp_path, "mypy") is None


def test_locked_version_ignores_a_symlinked_lockfile(tmp_path):
    target = tmp_path / "elsewhere.lock"
    target.write_text('[[package]]\nname = "ruff"\nversion = "0.1.0"\n', encoding="utf-8")
    project = tmp_path / "p"
    project.mkdir()
    (project / "uv.lock").symlink_to(target)
    assert locked_version(project, "ruff") is None


# ── JavaScript without a package.json: npm --prefix cache ───────────────────────────


def _loose_ts(sandbox) -> None:
    sandbox.write("src/index.ts", "export const x = 1;\n")


def test_manifest_less_javascript_installs_oxlint_and_oxfmt_into_npm_prefixes(sandbox):
    _shim(sandbox, "npm", _FAKE_NPM)
    _loose_ts(sandbox)
    before = sandbox.tree()
    plan, _ = sandbox.plan()
    assert [s.id for s in plan.steps] == [
        f"js-cache-install@oxlint-{OXLINT.version}", f"js-cache-install@oxfmt-{OXFMT.version}", "write-config@.:.oxlintrc.json",
    ]
    argv = plan.steps[0].argv
    assert argv[:2] == ("npm", "install") and "--ignore-scripts" in argv and argv[-1] == f"oxlint@{OXLINT.version}"
    assert argv[argv.index("--prefix") + 1] == plan.steps[0].cache_dir
    ci = apply_bootstrap(plan, root=sandbox.repo, environ={"CI": "1"})
    states = {step_id: state for step_id, state, _ in ci.results}
    assert states["write-config@.:.oxlintrc.json"] == "blocked" and ci.failed, "CI never writes a repo file"
    assert (_cache() / "npm" / f"oxlint-{OXLINT.version}" / "node_modules" / ".bin" / "oxlint").is_file()
    assert sandbox.tree() == before

    local, _ = sandbox.plan()
    assert [s.id for s in local.steps] == ["write-config@.:.oxlintrc.json"], "the cached tools are not reinstalled"
    assert not apply_bootstrap(local, root=sandbox.repo, environ={}).failed
    written = {path for path in sandbox.tree() if path not in before}
    assert written == {".oxlintrc.json"} and not (sandbox.repo / "package.json").exists()
    assert sandbox.plan()[0].is_empty()


def test_manifest_less_javascript_without_npm_is_blocked_with_a_remediation(sandbox):
    _loose_ts(sandbox)
    plan, _ = sandbox.plan()
    assert plan.is_empty() and {b.reason for b in plan.blocked} == {"manager-not-installed"}
    assert "npm" in plan.blocked[0].detail, "no tool, so no config is planned either"


def test_cache_handler_refuses_a_directory_outside_the_cache_root(sandbox, tmp_path):
    outside = tmp_path / "elsewhere"
    action = Action(
        "lint_bootstrap_step", "lint_bootstrap", "x", sandbox.repo, sandbox.repo,
        {"step": "x", "argv": ["true"], "cache_dir": str(outside), "creates": str(outside / "t"), "allow_mutation": True},
    )
    result = run_plan(InstallPlan(actions=[action], on_conflict="skip")).results[0]
    assert result.status == "error" and "outside the Rig tool cache" in result.detail
    assert not outside.exists()


# ── system linters: sha256-pinned release assets ────────────────────────────────────


@pytest.fixture
def releases(monkeypatch):
    """Pin stub assets for linux/x86_64 and serve them from memory."""
    tarball = _tar("shellcheck-v0.11.0/shellcheck")
    assets = {
        ("shellcheck", "linux", "x86_64"): ReleaseAsset(_SHELLCHECK_URL, hashlib.sha256(tarball).hexdigest(), "tar.gz"),
        ("shfmt", "linux", "x86_64"): ReleaseAsset(_SHFMT_URL, hashlib.sha256(_BINARY).hexdigest(), "raw"),
    }
    for key, asset in assets.items():
        monkeypatch.setitem(release._ASSETS, key, asset)
    served = {_SHELLCHECK_URL: tarball, _SHFMT_URL: _BINARY}
    monkeypatch.setattr(release, "_download", served.__getitem__)
    return served


def _shell_repo(sandbox) -> None:
    sandbox.write("scripts/run.sh", "#!/bin/sh\necho hi\n")


def test_shell_repo_installs_verified_shellcheck_and_shfmt_then_is_idempotent(sandbox, releases):
    _shell_repo(sandbox)
    before = sandbox.tree()
    plan, _ = sandbox.plan()
    steps = {s.id: s for s in plan.steps}
    assert set(steps) == {f"bin-cache-install@shellcheck-{RELEASE_VERSIONS['shellcheck']}", f"bin-cache-install@shfmt-{RELEASE_VERSIONS['shfmt']}"}
    assert all(s.kind == "install-tool-cache" and s.writes == () for s in steps.values())
    outcome = apply_bootstrap(plan, root=sandbox.repo, environ={"CI": "true"})
    assert not outcome.failed, outcome
    for tool in ("shellcheck", "shfmt"):
        exe = _cache() / "bin" / f"{tool}-{RELEASE_VERSIONS[tool]}" / tool
        assert exe.read_bytes() == _BINARY and exe.stat().st_mode & stat.S_IXUSR
    assert sandbox.tree() == before
    assert sandbox.plan()[0].is_empty()


def test_a_checksum_mismatch_installs_nothing(sandbox, releases):
    _shell_repo(sandbox)
    releases[_SHFMT_URL] = b"tampered"
    plan, _ = sandbox.plan()
    outcome = apply_bootstrap(plan, root=sandbox.repo, environ={})
    failed = [(i, d) for i, s, d in outcome.results if s == "failed"]
    assert outcome.failed and failed and "sha256 mismatch" in failed[0][1]
    assert not (_cache() / "bin" / f"shfmt-{RELEASE_VERSIONS['shfmt']}" / "shfmt").exists()


def test_a_tool_already_on_path_needs_no_install(sandbox, releases):
    _shell_repo(sandbox)
    for name in ("shellcheck", "shfmt"):
        _shim(sandbox, name, "#!/bin/sh\nexit 0\n")
    assert sandbox.plan()[0].is_empty()


def test_an_unsupported_platform_has_no_install_route_and_says_so(sandbox):
    _shell_repo(sandbox)
    cfg = resolve({"linters": {"rules": NO_SLOP}})
    discovery = discover(sandbox.repo)
    env = replace(sandbox.env(), system="freebsd", machine="x86_64")
    plan = build_bootstrap_plan(bootstrap_needs(discovery, cfg, env), discovery, cfg, env)
    assert plan.is_empty()
    assert {b.reason for b in plan.blocked} == {"unsupported-platform"}
    assert "put it on PATH" in plan.blocked[0].detail


def test_release_assets_are_pinned_https_with_a_digest_for_every_supported_platform():
    for tool in RELEASE_VERSIONS:
        for platform in (("linux", "x86_64"), ("linux", "aarch64"), ("darwin", "x86_64"), ("darwin", "arm64")):
            asset = release.release_asset(tool, *platform)
            assert asset is not None and asset.url.startswith("https://") and len(asset.sha256) == 64
            assert RELEASE_VERSIONS[tool] in asset.url
    assert release.release_asset("shellcheck", "windows", "x86_64") is None


def test_an_archive_with_a_traversal_member_is_refused():
    payload = _tar("../escape")
    sha = hashlib.sha256(payload).hexdigest()
    with pytest.raises(release.ReleaseError, match="traversal"):
        release.fetch_verified_binary("https://x", sha, "tar.gz", "shellcheck", fetch=lambda _url: payload)


# ── Rust: clippy/rustfmt components of the current toolchain; Go: nothing to install ─


@pytest.fixture
def rust(sandbox, tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("SHIM_STATE", str(state))
    _shim(sandbox, "cargo", _FAKE_CARGO)
    sandbox.write("Cargo.toml", '[package]\nname = "demo"\nversion = "0.1.0"\n')
    sandbox.write("src/main.rs", "fn main() {}\n")
    return sandbox, state


def test_rust_repo_gets_a_component_add_for_the_current_toolchain(rust):
    sandbox, state = rust
    _shim(sandbox, "rustup", _FAKE_RUSTUP)
    plan, _ = sandbox.plan()
    [step] = [s for s in plan.steps if s.kind == "install-toolchain-component"]
    assert step.id == "rustup-components@." and step.argv == ("rustup", "component", "add", "clippy", "rustfmt")
    assert "toolchain" not in step.argv and step.writes == ()
    outcome = apply_bootstrap(plan, root=sandbox.repo, environ={"CI": "true"})
    assert not outcome.failed and [s for _, s, _ in outcome.results] == ["applied"]
    assert (state / "components").exists()
    again = apply_bootstrap(plan, root=sandbox.repo, environ={})
    assert [s for _, s, _ in again.results] == ["noop"], "present components make the step a no-op"
    assert [c[0] for c in sandbox.calls()].count("rustup") == 1


def test_rustup_step_without_rustup_is_blocked(rust):
    sandbox, _ = rust
    discovery = discover(sandbox.repo)
    cfg = resolve({})
    need = BootstrapNeed(".", "rustup", "lint", "cargo-clippy", "cargo-clippy", ())  # type: ignore[arg-type]
    plan = build_bootstrap_plan((need,), discovery, cfg, sandbox.env())
    assert plan.is_empty() and {b.reason for b in plan.blocked} == {"manager-not-installed"}
    assert "rustup" in plan.blocked[0].detail and "toolchain" in plan.blocked[0].detail


def test_rustup_handler_only_runs_the_fixed_component_command(rust):
    sandbox, _ = rust
    action = Action(
        "lint_bootstrap_step", "lint_bootstrap", "x", sandbox.repo, sandbox.repo,
        {"step": "x", "argv": ["rustup", "toolchain", "install", "nightly"], "cwd": ".", "component": ["clippy"], "allow_mutation": True},
    )
    result = run_plan(InstallPlan(actions=[action], on_conflict="skip")).results[0]
    assert result.status == "error" and "only `rustup component add" in result.detail
    assert [c[0] for c in sandbox.calls()] == []


def test_go_repo_never_gets_an_install_step(sandbox):
    sandbox.write("go.mod", "module example.com/demo\n\ngo 1.22\n")
    sandbox.write("main.go", "package main\n\nfunc main() {}\n")
    plan, _ = sandbox.plan()
    assert plan.is_empty() and not plan.blocked


def test_a_repo_pinned_toolchain_that_is_not_installed_is_reported_never_downloaded(rust):
    """rustup refuses (auto-install is off): the step errors with the remediation and the probes never trigger a download."""
    sandbox, _ = rust
    _shim(sandbox, "rustup", _FAKE_RUSTUP.replace('(pathlib.Path(os.environ["SHIM_STATE"]) / "components").write_text("clippy rustfmt")',
                                                    'print("error: toolchain \'1.2.3\' is not installed", file=sys.stderr); sys.exit(1)'))
    sandbox.write("rust-toolchain.toml", '[toolchain]\nchannel = "1.2.3"\n')
    plan, _ = sandbox.plan()
    outcome = apply_bootstrap(plan, root=sandbox.repo, environ={})
    [(step_id, state, detail)] = [r for r in outcome.results if r[0].startswith("rustup-components")]
    assert state == "failed" and "not installed" in detail and "rustup toolchain install" in detail
    assert [c for c in sandbox.calls() if c[0] == "rustup"], "the rustup step really ran"
    assert not [c for c in sandbox.calls() if c[:3] == ["rustup", "toolchain", "install"]], "no toolchain download was attempted"


# ── review follow-ups: a cached JS tool needs no npm; manager-run is never "usable" ─────────────────


def test_a_cached_js_tool_needs_no_npm_on_path(sandbox):
    _shim(sandbox, "npm", _FAKE_NPM)
    _loose_ts(sandbox)
    apply_bootstrap(sandbox.plan()[0], root=sandbox.repo, environ={"CI": "1"})  # fills the cache (the config step is refused)
    (sandbox.bin_dir / "npm").unlink()
    plan, _ = sandbox.plan()
    assert not [b for b in plan.blocked if b.reason == "manager-not-installed"], "the cached oxlint/oxfmt run without npm"
    assert not [s for s in plan.steps if s.id.startswith("js-cache-install")]


def test_a_declared_ruff_that_only_resolves_through_poetry_run_goes_to_the_cache(python_cache):
    """`poetry run` may sync the project environment; the declared version is cached instead, never trusted as usable."""
    sandbox = python_cache
    _shim(sandbox, "poetry", _FAILING_MANAGER)
    sandbox.write("pyproject.toml", '[project]\nname = "p"\nversion = "0"\n\n[tool.poetry.group.dev.dependencies]\nruff = "0.6.9"\n')
    sandbox.write("poetry.lock", '[[package]]\nname = "ruff"\nversion = "0.6.9"\n')
    sandbox.write("a.py", "import os\n")
    plan, _ = sandbox.plan()
    assert [s.id for s in plan.steps] == ["py-cache-venv@ruff-0.6.9", "py-cache-install@ruff-0.6.9"]
    assert not apply_bootstrap(plan, root=sandbox.repo, environ={"CI": "1"}).failed
    assert [c[0] for c in sandbox.calls() if c[0] == "poetry"] == []



# ── a Ruff CONFIG alone (ruff.toml, no pyproject/requirements) still gets the pinned tool ───────────


@pytest.mark.parametrize("config", [("ruff.toml", "line-length = 100\n"), (".ruff.toml", "line-length = 100\n"),
                                    ("pyproject.toml", "[tool.ruff]\nline-length = 100\n")])
def test_a_ruff_config_without_a_manifest_provisions_the_pinned_ruff_in_the_cache(python_cache, config):
    sandbox = python_cache
    name, text = config
    sandbox.write(name, text)
    sandbox.write("m.py", "import os\n")
    before = sandbox.tree()
    plan, _ = sandbox.plan()
    slug = f"ruff-{RUFF.version}"
    assert [s.id for s in plan.steps] == [f"py-cache-venv@{slug}", f"py-cache-install@{slug}"]
    assert not plan.blocked and sandbox.tree() == before, "planning writes nothing and no manifest is created"
    assert not apply_bootstrap(plan, root=sandbox.repo, environ={"CI": "1"}).failed
    assert sandbox.plan()[0].is_empty()
    assert (_cache() / "py" / slug / "bin" / "ruff").is_file()


def test_a_ruff_config_keeps_the_version_the_project_locks(python_cache):
    sandbox = python_cache
    sandbox.write("ruff.toml", "line-length = 100\n")
    sandbox.write("requirements-dev.txt", "ruff==0.6.9\n")
    sandbox.write("m.py", "import os\n")
    plan, _ = sandbox.plan()
    assert [s.id for s in plan.steps] == ["py-cache-venv@ruff-0.6.9", "py-cache-install@ruff-0.6.9"]


def test_python_source_under_a_js_repo_with_a_ruff_config_gets_ruff_from_the_cache(python_cache):
    sandbox = python_cache
    sandbox.install_managers("npm")
    sandbox.package_json()
    sandbox.write("scripts/ruff.toml", "line-length = 100\n")
    sandbox.write("scripts/tool.py", "import os\n")
    plan, _ = sandbox.plan()
    assert f"py-cache-install@ruff-{RUFF.version}" in [s.id for s in plan.steps]
    assert "js-add-dev@." in [s.id for s in plan.steps], "the JS defaults are unaffected"
