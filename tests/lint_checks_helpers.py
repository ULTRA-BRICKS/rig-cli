"""Shared fixtures for the lint_checks runner/drift tests: a local fake cccc release, no network."""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

from riglib import cccc_install
from riglib.catalog import Catalog
from riglib.config import LoadedConfig
from riglib.plan import build

LINT_CATEGORIES = ("lint_checks", "lint_checks_machine")
FAKE_BINARY = b"#!/bin/sh\necho fake-cccc\n"
RIG_SOURCE = "git+https://git.example.test/org/rig-cli@" + "a" * 40


def fake_tarball(binary: bytes = FAKE_BINARY) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("cccc")
        info.size = len(binary)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(binary))
    return buf.getvalue()


def pin_fake_release(monkeypatch, tarball: bytes | None = None, binary: bytes = FAKE_BINARY) -> list[str]:
    """Patch this machine's pin to a local fake release and serve it from ``_download``; returns the URL log."""
    tarball = tarball if tarball is not None else fake_tarball(binary)
    key = cccc_install.platform_key()
    monkeypatch.setitem(
        cccc_install._PINS, key,
        cccc_install.Pin("fake-target", hashlib.sha256(tarball).hexdigest(), hashlib.sha256(binary).hexdigest()),
    )
    calls: list[str] = []

    def download(url: str) -> bytes:
        calls.append(url)
        return tarball

    monkeypatch.setattr(cccc_install, "_download", download)
    return calls


def _merged_lint_block(global_lint: dict, repo_lint: dict) -> dict:
    """A shallow-per-section cascade of two raw ``lint_checks`` blocks (repo wins), like the real deep merge."""
    merged = {k: (dict(v) if isinstance(v, dict) else v) for k, v in global_lint.items()}
    for key, value in repo_lint.items():
        merged[key] = {**merged.get(key, {}), **value} if isinstance(value, dict) and isinstance(merged.get(key), dict) else value
    return merged


def lint_plan(
    fake_agent_tools: Path, tmp_path: Path, repo: Path, extra: dict | None = None, global_lint: dict | None = None,
):
    """A plan holding only the lint_checks actions; the CI workflow is planned because ``RIG_SOURCE`` is configured.

    ``extra['lint_checks']`` is the REPO layer's block, ``global_lint`` the GLOBAL layer's (both land in the
    merged ``data``, only the global one in ``global_data`` — as ``config.load`` builds them).
    """
    extra = dict(extra or {})
    repo_lint = extra.pop("lint_checks", {})
    lint_block = _merged_lint_block(global_lint or {}, {"ci": {"rig_source": RIG_SOURCE}, **repo_lint})
    data = {
        "git_hooks": {"dispatcher": {"enabled": True, "dir": str(tmp_path / "disp")}},
        "lint_checks": lint_block,
        **extra,
    }
    global_data = {"lint_checks": global_lint} if global_lint else {}
    plan = build(LoadedConfig(data=data, repo_root=repo, global_data=global_data), Catalog.scan(str(fake_agent_tools)), project_type="cli")
    plan.actions = [a for a in plan.actions if a.category in LINT_CATEGORIES]
    return plan
