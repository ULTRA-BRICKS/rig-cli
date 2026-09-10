"""The ``review-gate`` composer is sourced from the installed review-cli, agent-tools is the fallback.

rig-cli#382: the gate body forked between review-cli's template and the agent-tools copy rig
installed everywhere, and ``rig status`` reported the stale copy as clean. These tests pin
that install AND drift both use ``review install-commit-hook --print-gate`` when it works,
and degrade LOUDLY to the agent-tools copy otherwise.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from riglib import review_gate
from riglib.actions.runner import run_plan
from riglib.catalog import Catalog
from riglib.config import LoadedConfig
from riglib.drift import detect
from riglib.plan import build

FAKE_GATE = "#!/bin/sh\n# fake gate from review-cli\nexit 0\n"
FAKE_REVIEW_SCRIPT = (
    "#!/bin/sh\n"
    '[ "$1 $2" = "install-commit-hook --print-gate" ] || { echo usage >&2; exit 2; }\n'
    "printf '#!/bin/sh\\n# fake gate from review-cli\\nexit 0\\n'\n"
)


def _write_fake_review(path: Path, script: str) -> Path:
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def fake_review(tmp_path: Path) -> Path:
    """A ``review`` stand-in that only knows ``install-commit-hook --print-gate``."""
    return _write_fake_review(tmp_path / "fake-review", FAKE_REVIEW_SCRIPT)


@pytest.fixture
def dispatcher_plan(fake_agent_tools, tmp_path, monkeypatch):
    """A dispatcher-only plan against an isolated HOME (the test_actions_drift pattern)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False},
            "agent_hooks": {"enabled": False},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "git_hooks": {
                "dispatcher": {
                    "enabled": True,
                    "dir": str(home / "ghd"),
                    "runner": str(home / "run-global-hooks"),
                    "set_global_hooks_path": False,
                    "install_local_retrofit_script": False,
                }
            },
        },
        repo_root=repo,
    )
    return build(cfg, cat, project_type="unknown")


def _dispatcher_result(report):
    return next(r for r in report.results if r.action.item == "dispatcher")


def _installed(home: Path, name: str) -> str:
    return (home / "hooks" / name).read_text(encoding="utf-8")


def _source(fake_agent_tools: Path, name: str) -> str:
    return (fake_agent_tools / "git-hooks" / "global-dispatcher" / "hooks" / name).read_text(encoding="utf-8")


def _git_hooks_items(plan):
    return [i for i in detect(plan).items if i.category == "git_hooks"]


# --- the resolver seam ----------------------------------------------------------------


def test_fake_review_prints_the_gate(fake_review, monkeypatch):
    monkeypatch.setenv("RIG_REVIEW_BIN", str(fake_review))
    gate = review_gate.review_gate_body()
    assert gate.body == FAKE_GATE
    assert gate.note == f"review-gate from review-cli ({fake_review})"
    assert gate.transient is False


def test_resolve_review_bin_empty_env_disables(monkeypatch):
    monkeypatch.setenv("RIG_REVIEW_BIN", "")
    assert review_gate.resolve_review_bin() is None
    gate = review_gate.review_gate_body()
    assert gate.body is None
    assert gate.note == "review not installed — using the agent-tools copy"
    assert gate.transient is False


def test_resolve_review_bin_unset_uses_which(monkeypatch):
    monkeypatch.delenv("RIG_REVIEW_BIN")
    monkeypatch.setattr(review_gate.shutil, "which", lambda name: f"/opt/bin/{name}")
    assert review_gate.resolve_review_bin() == "/opt/bin/review"
    monkeypatch.setattr(review_gate.shutil, "which", lambda name: None)
    assert review_gate.resolve_review_bin() is None


def test_resolve_review_bin_path_wins_over_which(monkeypatch, tmp_path):
    monkeypatch.setenv("RIG_REVIEW_BIN", str(tmp_path / "review"))
    monkeypatch.setattr(review_gate.shutil, "which", lambda name: "/should/not/be/used")
    assert review_gate.resolve_review_bin() == str(tmp_path / "review")


def test_body_falls_back_when_review_predates_print_gate(monkeypatch, tmp_path):
    old = _write_fake_review(tmp_path / "old-review", "#!/bin/sh\necho 'usage: review' >&2\nexit 2\n")
    monkeypatch.setenv("RIG_REVIEW_BIN", str(old))
    gate = review_gate.review_gate_body()
    assert gate.body is None
    assert gate.note == "review does not support --print-gate (exit 2) — using the agent-tools copy"
    assert gate.transient is False


def test_body_falls_back_on_empty_stdout(monkeypatch, tmp_path):
    silent = _write_fake_review(tmp_path / "silent-review", "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("RIG_REVIEW_BIN", str(silent))
    gate = review_gate.review_gate_body()
    assert gate.body is None
    assert "does not support --print-gate (exit 0)" in gate.note
    assert gate.note.endswith("— using the agent-tools copy")


def test_body_falls_back_on_non_shebang_stdout(monkeypatch, tmp_path):
    chatty = _write_fake_review(tmp_path / "chatty-review", "#!/bin/sh\necho 'not a script'\n")
    monkeypatch.setenv("RIG_REVIEW_BIN", str(chatty))
    assert review_gate.review_gate_body().body is None


def test_body_falls_back_when_binary_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("RIG_REVIEW_BIN", str(tmp_path / "no-such-review"))
    gate = review_gate.review_gate_body()
    assert gate.body is None
    assert gate.note.startswith("review --print-gate timed out/failed (")
    assert gate.note.endswith("— left as-is")
    assert gate.transient is True


def test_body_falls_back_on_timeout(monkeypatch, tmp_path):
    slow = _write_fake_review(tmp_path / "slow-review", "#!/bin/sh\nsleep 5\n")
    monkeypatch.setenv("RIG_REVIEW_BIN", str(slow))
    real_run = review_gate.subprocess.run

    def _short_timeout(*args, **kwargs):
        kwargs["timeout"] = 0.2
        return real_run(*args, **kwargs)

    monkeypatch.setattr(review_gate.subprocess, "run", _short_timeout)
    gate = review_gate.review_gate_body()
    assert gate.body is None
    assert gate.note.startswith("review --print-gate timed out/failed (")
    assert gate.transient is True


def test_child_env_is_minimal(monkeypatch, tmp_path):
    probe = _write_fake_review(tmp_path / "probe-review", "#!/bin/sh\nprintf '#!x\\n%s' \"$RIG_LEAK\"\n")
    monkeypatch.setenv("RIG_REVIEW_BIN", str(probe))
    monkeypatch.setenv("RIG_LEAK", "leaked")
    assert review_gate.review_gate_body().body == "#!x\n"


# --- apply: install from review-cli, fallback to agent-tools ----------------------------


def test_apply_installs_review_gate_from_review_cli(dispatcher_plan, fake_agent_tools, tmp_path, monkeypatch, fake_review):
    monkeypatch.setenv("RIG_REVIEW_BIN", str(fake_review))
    home = tmp_path / "home"
    result = _dispatcher_result(run_plan(dispatcher_plan))
    assert result.status in ("created", "backed_up"), result.detail
    assert "from review-cli" in result.detail
    assert _installed(home, "review-gate") == FAKE_GATE
    assert _installed(home, "review-gate") != _source(fake_agent_tools, "review-gate")
    for other in ("pre-commit", "commit-msg", "pre-push"):
        assert _installed(home, other) == _source(fake_agent_tools, other)
        assert os.access(home / "hooks" / other, os.X_OK)
    assert os.access(home / "hooks" / "review-gate", os.X_OK)


def test_apply_is_idempotent_with_review_cli_source(dispatcher_plan, monkeypatch, fake_review):
    monkeypatch.setenv("RIG_REVIEW_BIN", str(fake_review))
    run_plan(dispatcher_plan)
    second = _dispatcher_result(run_plan(dispatcher_plan))
    assert second.status == "skipped", second.detail
    assert _git_hooks_items(dispatcher_plan) == []


def test_apply_falls_back_to_agent_tools_copy_when_review_disabled(dispatcher_plan, fake_agent_tools, tmp_path, monkeypatch):
    monkeypatch.setenv("RIG_REVIEW_BIN", "")
    home = tmp_path / "home"
    result = _dispatcher_result(run_plan(dispatcher_plan))
    assert "review not installed — using the agent-tools copy" in result.detail
    assert _installed(home, "review-gate") == _source(fake_agent_tools, "review-gate")
    assert _git_hooks_items(dispatcher_plan) == []


def test_apply_falls_back_when_review_predates_print_gate(dispatcher_plan, fake_agent_tools, tmp_path, monkeypatch):
    old = _write_fake_review(tmp_path / "old-review", "#!/bin/sh\necho usage >&2\nexit 2\n")
    monkeypatch.setenv("RIG_REVIEW_BIN", str(old))
    home = tmp_path / "home"
    result = _dispatcher_result(run_plan(dispatcher_plan))
    assert "review does not support --print-gate (exit 2) — using the agent-tools copy" in result.detail
    assert _installed(home, "review-gate") == _source(fake_agent_tools, "review-gate")


def test_apply_falls_back_on_empty_stdout(dispatcher_plan, fake_agent_tools, tmp_path, monkeypatch):
    silent = _write_fake_review(tmp_path / "silent-review", "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("RIG_REVIEW_BIN", str(silent))
    home = tmp_path / "home"
    result = _dispatcher_result(run_plan(dispatcher_plan))
    assert "using the agent-tools copy" in result.detail
    assert _installed(home, "review-gate") == _source(fake_agent_tools, "review-gate")


def test_apply_replaces_stale_agent_tools_gate_with_backup(dispatcher_plan, fake_agent_tools, tmp_path, monkeypatch, fake_review):
    """Upgrade path: a machine installed from the stale copy converges once review-cli answers."""
    monkeypatch.setenv("RIG_REVIEW_BIN", "")
    run_plan(dispatcher_plan)
    home = tmp_path / "home"
    assert _installed(home, "review-gate") == _source(fake_agent_tools, "review-gate")
    monkeypatch.setenv("RIG_REVIEW_BIN", str(fake_review))
    result = _dispatcher_result(run_plan(dispatcher_plan))
    assert result.status == "backed_up", result.detail
    assert result.backup is not None and result.backup.name.startswith("review-gate.rig-bak-")
    assert _installed(home, "review-gate") == FAKE_GATE


# --- drift: compare against the same source apply used ---------------------------------


def test_drift_flags_edited_review_gate_against_review_cli(dispatcher_plan, tmp_path, monkeypatch, fake_review):
    monkeypatch.setenv("RIG_REVIEW_BIN", str(fake_review))
    run_plan(dispatcher_plan)
    home = tmp_path / "home"
    (home / "hooks" / "review-gate").write_text("#!/bin/sh\ntampered\n", encoding="utf-8")
    items = _git_hooks_items(dispatcher_plan)
    assert [(i.direction, i.item) for i in items] == [("modified", "review-gate")]
    assert items[0].detail == "composer hook differs from review-cli's --print-gate output"
    run_plan(dispatcher_plan)
    assert _git_hooks_items(dispatcher_plan) == []


def test_drift_flags_stale_agent_tools_fork_when_review_cli_answers(dispatcher_plan, fake_agent_tools, tmp_path, monkeypatch, fake_review):
    """The whole point: a review-gate equal to the agent-tools copy is DRIFT once review-cli owns it."""
    monkeypatch.setenv("RIG_REVIEW_BIN", "")
    run_plan(dispatcher_plan)
    home = tmp_path / "home"
    assert _installed(home, "review-gate") == _source(fake_agent_tools, "review-gate")
    assert _git_hooks_items(dispatcher_plan) == []  # clean against the fallback source
    monkeypatch.setenv("RIG_REVIEW_BIN", str(fake_review))
    items = _git_hooks_items(dispatcher_plan)
    assert [(i.direction, i.item) for i in items] == [("modified", "review-gate")]
    assert "review-cli" in items[0].detail
    run_plan(dispatcher_plan)
    assert _git_hooks_items(dispatcher_plan) == []


def test_drift_other_composers_still_compared_to_source(dispatcher_plan, tmp_path, monkeypatch, fake_review):
    monkeypatch.setenv("RIG_REVIEW_BIN", str(fake_review))
    run_plan(dispatcher_plan)
    home = tmp_path / "home"
    (home / "hooks" / "pre-push").write_text("#!/bin/sh\ntampered\n", encoding="utf-8")
    items = _git_hooks_items(dispatcher_plan)
    assert [(i.direction, i.item, i.detail) for i in items] == [
        ("modified", "pre-push", "composer hook differs from source")
    ]


def test_drift_missing_review_gate_reported(dispatcher_plan, tmp_path, monkeypatch, fake_review):
    monkeypatch.setenv("RIG_REVIEW_BIN", str(fake_review))
    run_plan(dispatcher_plan)
    (tmp_path / "home" / "hooks" / "review-gate").unlink()
    items = _git_hooks_items(dispatcher_plan)
    assert [(i.direction, i.item) for i in items] == [("missing", "review-gate")]


# --- round-1 review hardening (GH-382) ------------------------------------------------


def _slow_review(tmp_path: Path, monkeypatch) -> None:
    """Point the seam at a `review` that never answers, with the timeout shortened."""
    slow = _write_fake_review(tmp_path / "slow-review", "#!/bin/sh\nsleep 5\n")
    monkeypatch.setenv("RIG_REVIEW_BIN", str(slow))
    real_run = review_gate.subprocess.run

    def _short_timeout(*args, **kwargs):
        kwargs["timeout"] = 0.2
        return real_run(*args, **kwargs)

    monkeypatch.setattr(review_gate.subprocess, "run", _short_timeout)


def test_transient_failure_leaves_installed_gate_as_is(dispatcher_plan, tmp_path, monkeypatch, fake_review):
    """A timeout says nothing about which gate is right: never regress to the agent-tools copy."""
    monkeypatch.setenv("RIG_REVIEW_BIN", str(fake_review))
    run_plan(dispatcher_plan)
    home = tmp_path / "home"
    _slow_review(tmp_path, monkeypatch)
    result = _dispatcher_result(run_plan(dispatcher_plan))
    assert result.status == "skipped", result.detail
    assert "left as-is" in result.detail
    assert _installed(home, "review-gate") == FAKE_GATE
    assert not list((home / "hooks").glob("review-gate.rig-bak-*"))
    assert _git_hooks_items(dispatcher_plan) == []  # unknown expected content is not drift


def test_transient_failure_still_installs_fallback_when_nothing_is_there(dispatcher_plan, fake_agent_tools, tmp_path, monkeypatch):
    _slow_review(tmp_path, monkeypatch)
    result = _dispatcher_result(run_plan(dispatcher_plan))
    assert result.status == "created", result.detail
    assert _installed(tmp_path / "home", "review-gate") == _source(fake_agent_tools, "review-gate")


def test_fallback_reason_reaches_the_drift_text(dispatcher_plan, tmp_path, monkeypatch):
    monkeypatch.setenv("RIG_REVIEW_BIN", "")
    run_plan(dispatcher_plan)
    (tmp_path / "home" / "hooks" / "review-gate").write_text("#!/bin/sh\ntampered\n", encoding="utf-8")
    items = _git_hooks_items(dispatcher_plan)
    assert [(i.direction, i.item) for i in items] == [("modified", "review-gate")]
    assert items[0].detail == (
        "composer hook differs from source (review not installed — using the agent-tools copy)"
    )


def test_retired_composer_is_reported_as_extra_and_never_deleted(dispatcher_plan, tmp_path, monkeypatch, fake_review):
    monkeypatch.setenv("RIG_REVIEW_BIN", str(fake_review))
    run_plan(dispatcher_plan)
    hooks = tmp_path / "home" / "hooks"
    (hooks / "post-checkout").write_text("#!/bin/sh\nretired\n", encoding="utf-8")
    (hooks / "review-gate.rig-bak-20260101-000000").write_text("old\n", encoding="utf-8")
    items = _git_hooks_items(dispatcher_plan)
    assert [(i.direction, i.item) for i in items] == [("extra", "post-checkout")]
    assert "git still runs it" in items[0].detail
    result = _dispatcher_result(run_plan(dispatcher_plan))
    assert (hooks / "post-checkout").exists()
    assert "not shipped by source, left as-is: post-checkout" in result.detail


@pytest.mark.parametrize("on_conflict", ["backup", "overwrite", "skip"])
def test_stale_file_at_composer_dir_honours_on_conflict(dispatcher_plan, tmp_path, monkeypatch, fake_review, on_conflict):
    monkeypatch.setenv("RIG_REVIEW_BIN", str(fake_review))
    hooks = tmp_path / "home" / "hooks"
    hooks.parent.mkdir(parents=True, exist_ok=True)
    hooks.write_text("not a directory\n", encoding="utf-8")
    dispatcher_plan.on_conflict = on_conflict
    result = _dispatcher_result(run_plan(dispatcher_plan))
    if on_conflict == "skip":
        # the runner + fragments still install; only the composers step is a conflict-skip
        assert "composers skipped" in result.detail
        assert hooks.is_file() and hooks.read_text(encoding="utf-8") == "not a directory\n"
        return
    assert hooks.is_dir()
    assert _installed(tmp_path / "home", "review-gate") == FAKE_GATE
    backups = list(hooks.parent.glob("hooks.rig-bak-*"))
    if on_conflict == "backup":
        assert result.status == "backed_up", result.detail
        assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == "not a directory\n"
    else:
        assert result.status == "created", result.detail
        assert backups == []


def test_review_is_not_asked_when_source_ships_no_review_gate(tmp_path, monkeypatch):
    marker = tmp_path / "asked"
    probe = _write_fake_review(
        tmp_path / "probe-review", f"#!/bin/sh\ntouch {marker}\nprintf '#!/bin/sh\\n'\n"
    )
    monkeypatch.setenv("RIG_REVIEW_BIN", str(probe))
    src_hooks = tmp_path / "hooks-src"
    src_hooks.mkdir()
    (src_hooks / "pre-commit").write_text("#!/bin/sh\n", encoding="utf-8")
    sources = review_gate.composer_sources(src_hooks)
    assert [s.name for s in sources] == ["pre-commit"]
    assert not marker.exists()
    assert review_gate.gate_note(sources) == "no review-gate composer in source"


def test_child_gets_no_stdin_and_a_neutral_cwd(tmp_path, monkeypatch):
    probe = _write_fake_review(
        tmp_path / "probe-review", "#!/bin/sh\ncat >/dev/null\nprintf '#!cwd=%s\\n' \"$PWD\"\n"
    )
    monkeypatch.setenv("RIG_REVIEW_BIN", str(probe))
    monkeypatch.chdir(tmp_path)
    gate = review_gate.review_gate_body()  # `cat` returns at once: stdin is /dev/null
    assert gate.body is not None
    assert gate.body != f"#!cwd={tmp_path}\n"
    assert gate.body.startswith("#!cwd=")


def test_undecodable_output_is_a_structural_fallback_not_a_crash(tmp_path, monkeypatch):
    bad = _write_fake_review(tmp_path / "bad-review", "#!/bin/sh\nprintf '#!\\377\\376\\n'\n")
    monkeypatch.setenv("RIG_REVIEW_BIN", str(bad))
    gate = review_gate.review_gate_body()
    assert gate.body is None
    assert gate.transient is False
    assert "unreadable" in gate.note


# --- round-2 review hardening (GH-382) ------------------------------------------------


def test_transient_failure_with_missing_gate_still_reports_missing(dispatcher_plan, tmp_path, monkeypatch, fake_review):
    monkeypatch.setenv("RIG_REVIEW_BIN", str(fake_review))
    run_plan(dispatcher_plan)
    (tmp_path / "home" / "hooks" / "review-gate").unlink()
    _slow_review(tmp_path, monkeypatch)
    items = _git_hooks_items(dispatcher_plan)
    assert [(i.direction, i.item) for i in items] == [("missing", "review-gate")]


def test_transient_failure_lists_the_gate_as_unverified_not_clean(dispatcher_plan, tmp_path, monkeypatch, fake_review):
    """Drift cannot compare an unresolvable gate, but it must SAY so instead of reporting clean."""
    from riglib.provenance import KIND_UNVERIFIED

    monkeypatch.setenv("RIG_REVIEW_BIN", str(fake_review))
    run_plan(dispatcher_plan)
    _slow_review(tmp_path, monkeypatch)
    report = detect(dispatcher_plan)
    assert [i for i in report.items if i.category == "git_hooks"] == []
    known = [k for k in report.known if k.category == "git_hooks"]
    assert [(k.item, k.kind) for k in known] == [("review-gate", KIND_UNVERIFIED)]
    assert known[0].name.startswith("review-gate: review --print-gate timed out/failed (")
    assert report.in_sync  # informational, never the drift exit code


def test_transient_failure_leaves_installed_gate_alone_under_overwrite(dispatcher_plan, tmp_path, monkeypatch, fake_review):
    monkeypatch.setenv("RIG_REVIEW_BIN", str(fake_review))
    run_plan(dispatcher_plan)
    dispatcher_plan.on_conflict = "overwrite"
    _slow_review(tmp_path, monkeypatch)
    result = _dispatcher_result(run_plan(dispatcher_plan))
    assert "left as-is" in result.detail
    assert _installed(tmp_path / "home", "review-gate") == FAKE_GATE


def test_transient_failure_repairs_a_directory_at_the_gate_path(dispatcher_plan, fake_agent_tools, tmp_path, monkeypatch, fake_review):
    """A directory is not a hook git can run: only a regular FILE is left as-is."""
    monkeypatch.setenv("RIG_REVIEW_BIN", str(fake_review))
    run_plan(dispatcher_plan)
    gate = tmp_path / "home" / "hooks" / "review-gate"
    gate.unlink()
    gate.mkdir()
    (gate / "junk").write_text("x", encoding="utf-8")
    _slow_review(tmp_path, monkeypatch)
    result = _dispatcher_result(run_plan(dispatcher_plan))
    assert result.status == "backed_up", result.detail
    assert gate.is_file() and gate.read_text(encoding="utf-8") == _source(fake_agent_tools, "review-gate")
    assert os.access(gate, os.X_OK)


def test_stderr_noise_does_not_discard_a_valid_gate(tmp_path, monkeypatch):
    noisy = _write_fake_review(
        tmp_path / "noisy-review",
        "#!/bin/sh\nprintf '\\377\\376 warning\\n' >&2\nprintf '#!/bin/sh\\n# fake gate from review-cli\\nexit 0\\n'\n",
    )
    monkeypatch.setenv("RIG_REVIEW_BIN", str(noisy))
    assert review_gate.review_gate_body().body == FAKE_GATE


def test_child_cwd_ignores_tmpdir_inside_the_repo(tmp_path, monkeypatch):
    probe = _write_fake_review(tmp_path / "probe-review", "#!/bin/sh\nprintf '#!cwd=%s\\n' \"$PWD\"\n")
    monkeypatch.setenv("RIG_REVIEW_BIN", str(probe))
    inside = tmp_path / ".tmp"
    inside.mkdir()
    monkeypatch.setenv("TMPDIR", str(inside))
    monkeypatch.chdir(tmp_path)
    body = review_gate.review_gate_body().body
    assert body is not None and str(tmp_path) not in body
    assert body == f"#!cwd={review_gate._NEUTRAL_CWD}\n"


def test_unshipped_composers_only_excludes_real_rig_backups(tmp_path):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for name in ("pre-commit", "review-gate.rig-bak-20260101-000000", "review-gate.rig-bak-20260101-000000.2",
                 "my.rig-bak-hook", "post-checkout"):
        (hooks / name).write_text("#!/bin/sh\n", encoding="utf-8")
    sources = [review_gate.ComposerSource("pre-commit", "#!/bin/sh\n", "#!/bin/sh\n", review_gate.SOURCE_ORIGIN)]
    assert [p.name for p in review_gate.unshipped_composers(hooks, sources)] == ["my.rig-bak-hook", "post-checkout"]
