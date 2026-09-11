"""Regression evidence for review-cli#444, not a green acceptance certificate.

Source under test: review-cli 5430f9361468bf9bd1a66a4d4be8198024f989ba.
Run from a checkout of that revision with an isolated HOME:
    PYTHONPATH=. python -m pytest /path/to/default_panel_roles.py -q

Actual CLI dispatch is exercised. Availability and the final mode handler are
fixtures; no provider account, paid model or personal Telegram is contacted.
Observed on 2026-09-11: all three cases fail at the final role assertion:
    models=['oc:xai/grok-4.6'], roles=[]; required role count=3; CLI exit=0.
The assertions intentionally describe the expected contract and remain red on
that source revision. Do not remove them or mark the product accepted because a
repository-level suite had passed. This evidence is not a live-machine test.
"""
from dataclasses import replace
import io
from pathlib import Path

import pytest
from reviewlib import backends, cli
from reviewlib.config import SHIP_MIN_ROLES
from reviewlib.modes import registry


@pytest.mark.parametrize("mode", ["just-ask", "quorum", "brainstorm"])
def test_default_recorded_panel_pads_one_available_model(mode, monkeypatch, tmp_path):
    captured = {}

    def handler(ctx):
        captured.update(
            models=list(ctx.models),
            roles=list(ctx.extra.get("task_roles") or []),
        )
        return 0

    monkeypatch.setattr(
        registry,
        "MODES",
        tuple(
            replace(item, handler=handler) if item.subcommand == mode else item
            for item in registry.MODES
        ),
    )
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(
        backends, "backend_available", lambda model: model == "oc:xai/grok-4.6"
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.delenv("REVIEW_TASK_CODE", raising=False)
    monkeypatch.delenv("REVIEW_FAKE_BACKEND", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GEMINI_ENV_FILE", str(tmp_path / "missing.env"))

    code = cli.main([
        mode, "test question", "--task", "444", "-C", str(Path.cwd()),
    ])
    assert code == 0, (code, captured)
    assert captured["models"], captured
    assert set(captured["models"]) == {"oc:xai/grok-4.6"}, captured
    assert len({role for role in captured["roles"] if role}) >= SHIP_MIN_ROLES, captured
