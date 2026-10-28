"""`rig schema --check/--write` against a REAL built wheel installed into an isolated venv, run from a foreign cwd.

The wheel packages only ``riglib*``; ``setup.py`` copies the committed ``schema/rig.schema.json`` into it at build time
as ``riglib/rig.schema.json`` (no second tracked copy). A missing or altered packaged schema must stay a non-zero drift
verdict, ``--write`` on an install must never write into site-packages, and a build without the canonical file fails.
Nothing here skips: builds use the project's own setuptools backend in-process and the install is `pip --no-index`, so no network,
no uv and no isolated build env; any failing step FAILS (a packaging regression is never a skip).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tarfile
import venv
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_IGNORE = shutil.ignore_patterns(
    ".git", ".worktrees", "build", "*.egg-info", "__pycache__", ".pytest_cache", ".venv", "node_modules", ".mypy_cache", "docs", "portal"
)
# PEP 517 hooks of the project's own backend, run by THIS interpreter: no uv, no network, no isolated build env. The
# backend (setuptools >= 70.1, a `.[test]` dependency) is imported, so a missing one FAILS the test instead of skipping.
_BUILD = "import sys; from setuptools import build_meta as b; print(getattr(b, sys.argv[1])(sys.argv[2]))"


def _run(argv, cwd):
    done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL, check=False)
    assert done.returncode == 0, f"{' '.join(argv[:4])} failed:\n{done.stdout[-1500:]}\n{done.stderr[-1500:]}"
    return done


def _build(kind, src, out):
    """Build the ``wheel``/``sdist`` of ``src`` into ``out`` with the project's own backend; the artifact path."""
    out.mkdir(parents=True, exist_ok=True)
    done = _run([sys.executable, "-c", _BUILD, f"build_{kind}", str(out)], src)
    return out / done.stdout.strip().splitlines()[-1]


def _clean_copy(base):
    """A clean copy of the tree, so a stale in-tree build/ directory can never mask a packaging gap."""
    src = base / "src"
    shutil.copytree(ROOT, src, ignore=_IGNORE)
    return src


@pytest.fixture(scope="module")
def sdist_wheel(tmp_path_factory):
    """sdist of a clean copy -> unpacked -> wheel built from the sdist (the pip-install-from-sdist path)."""
    base = tmp_path_factory.mktemp("sdist")
    archive = _build("sdist", _clean_copy(base), base / "sd")
    with tarfile.open(archive) as tar:
        tar.extractall(base / "unpacked", **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
    [unpacked] = (base / "unpacked").iterdir()
    return _build("wheel", unpacked, base / "wh")


@pytest.fixture(scope="module")
def installed(tmp_path_factory, sdist_wheel):
    base = tmp_path_factory.mktemp("wheel")
    env_dir = base / "venv"
    venv.EnvBuilder(with_pip=True, symlinks=True).create(env_dir)  # ensurepip: offline
    _run([str(env_dir / "bin" / "python"), "-m", "pip", "install", "--no-index", "--no-deps", "--disable-pip-version-check", str(sdist_wheel)], base)
    site = next((env_dir / "lib").glob("python*/site-packages"))
    foreign = base / "elsewhere"
    foreign.mkdir()
    return {"wheel": sdist_wheel, "rig": env_dir / "bin" / "rig", "site": site, "cwd": foreign}


def test_a_wheel_built_straight_from_the_tree_carries_the_committed_schema_bytes(tmp_path):
    with zipfile.ZipFile(_build("wheel", _clean_copy(tmp_path), tmp_path / "wh")) as wheel:
        assert wheel.read("riglib/rig.schema.json") == (ROOT / "schema" / "rig.schema.json").read_bytes()


def test_a_build_without_the_canonical_schema_fails_instead_of_shipping_an_unverifiable_wheel(tmp_path):
    src = _clean_copy(tmp_path)
    (src / "schema" / "rig.schema.json").unlink()
    done = subprocess.run(
        [sys.executable, "-c", _BUILD, "build_wheel", str(tmp_path / "wh")], cwd=src,
        capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL, check=False,
    )
    assert done.returncode != 0 and "cannot package the schema" in done.stdout + done.stderr
    assert not list((tmp_path / "wh").glob("*.whl"))


def _schema(inst, *flags):
    return subprocess.run(
        [str(inst["rig"]), "schema", *flags], cwd=inst["cwd"], capture_output=True, text=True, timeout=300,
        stdin=subprocess.DEVNULL, check=False,
    )


def test_the_sdist_built_wheel_carries_the_committed_schema_bytes(installed):
    with zipfile.ZipFile(installed["wheel"]) as wheel:
        packaged = wheel.read("riglib/rig.schema.json")
    assert packaged == (ROOT / "schema" / "rig.schema.json").read_bytes()


def test_installed_check_verifies_the_packaged_schema_from_a_foreign_cwd(installed):
    done = _schema(installed, "--check")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "in sync with the registry" in done.stdout
    assert str(installed["site"] / "riglib" / "rig.schema.json") in done.stdout


def test_installed_check_fails_closed_when_the_packaged_schema_is_missing(installed):
    path = installed["site"] / "riglib" / "rig.schema.json"
    original = path.read_bytes()
    path.unlink()
    try:
        done = _schema(installed, "--check")
    finally:
        path.write_bytes(original)
    assert done.returncode == 2 and "missing or out of sync" in done.stdout, done.stdout + done.stderr


@pytest.mark.parametrize("corrupt", [lambda raw: raw[: len(raw) // 2], lambda raw: raw + b"\xff\xfe", lambda raw: b""])
def test_installed_check_fails_closed_when_the_packaged_schema_is_corrupt(installed, corrupt):
    path = installed["site"] / "riglib" / "rig.schema.json"
    original = path.read_bytes()
    path.write_bytes(corrupt(original))
    try:
        done = _schema(installed, "--check")
    finally:
        path.write_bytes(original)
    assert done.returncode == 2 and "missing or out of sync" in done.stdout, done.stdout + done.stderr
    assert "Traceback" not in done.stderr


def test_installed_write_is_refused_and_leaves_site_packages_untouched(installed):
    path = installed["site"] / "riglib" / "rig.schema.json"
    before = path.read_bytes()
    done = _schema(installed, "--write")
    assert done.returncode == 2 and "no rig-cli checkout" in done.stdout, done.stdout + done.stderr
    assert path.read_bytes() == before
    assert not (installed["site"] / "schema").exists() and list(installed["cwd"].iterdir()) == []


def test_installed_bare_schema_prints_the_registry_schema(installed):
    from riglib import config_schema

    done = _schema(installed)
    assert done.returncode == 0 and done.stdout == config_schema.render_schema_json()
