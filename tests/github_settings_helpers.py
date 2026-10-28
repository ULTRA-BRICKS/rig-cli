"""Shared fixtures and action builders for the github-settings test files."""

from __future__ import annotations


import pytest

from riglib.actions import github_browser_action as github_browser_action_mod
from riglib.config import LoadedConfig
from riglib import github_auth
from riglib.github_actions import (
    GITHUB_ACTIONS_DEFAULTS,
)
from riglib.github_browser import (
    UI_ONLY_TOGGLES,
)
from riglib.github_ghas import (
    GITHUB_GHAS_DEFAULTS,
)
from riglib.github_merge import (
    GITHUB_MERGE_DEFAULTS,
)
from riglib.plan import Action
from riglib.actions import github_common as github_common_mod



# ── shared helpers ───────────────────────────────────────────────────────────────────
@pytest.fixture
def gh_repo(monkeypatch):
    """Pretend the repo has a github origin remote (no real git needed)."""
    monkeypatch.setattr(github_common_mod, "github_owner_repo", lambda repo_root: ("acme", "widget"))


@pytest.fixture(autouse=True)
def _authed(monkeypatch):
    """Default every test to an already-authed gate — the gate has its OWN tests below.

    Autouse so the mutation tests don't each have to stub it; the gate-specific tests override it.
    Also resets the process-level per-apply auth-gate dedup set so one test's timed-out kind can't
    leak into the next (the real reset happens in run_plan; tests call handlers directly).
    """
    github_auth.reset_auth_gate()
    monkeypatch.setattr(github_common_mod, "ensure_gh_auth", lambda **kw: github_auth.AuthOutcome("ok"))
    monkeypatch.setattr(github_browser_action_mod, "ensure_browser_auth", lambda **kw: github_auth.AuthOutcome("ok"))


def _action(kind, item, repo, opts) -> Action:
    return Action(kind=kind, category="github", item=item, source=repo, target=repo, options=opts)


def _merge_action(repo, **overrides):
    return _action("provision_github_merge", "merge", repo, {**GITHUB_MERGE_DEFAULTS, **overrides})


def _ghas_action(repo, **overrides):
    return _action("provision_github_ghas", "ghas", repo, {**GITHUB_GHAS_DEFAULTS, **overrides})


def _actions_action(repo, **overrides):
    return _action("provision_github_actions", "actions", repo, {**GITHUB_ACTIONS_DEFAULTS, **overrides})


def _browser_action(repo, **overrides):
    base = {k: v["default"] for k, v in UI_ONLY_TOGGLES.items()}
    return _action("provision_github_browser", "browser", repo, {**base, **overrides})


def _cfg(data, repo_root):
    return LoadedConfig(data=data, repo_root=repo_root)
