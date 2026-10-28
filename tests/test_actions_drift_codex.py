"""Codex hook bridge tests."""

from __future__ import annotations

import json
import pytest
from riglib.actions import run_plan
from riglib.catalog import Catalog
from riglib.drift import detect
from riglib.plan import build
from tests.actions_drift_helpers import (
    _bridge_cfg,
    _bridge_results,
    _read_toml,
)


def test_codex_hook_bridge_enables_disabled_features_hooks(fake_agent_tools, tmp_path):
    """A wired Codex bridge must not report synced while [features].hooks disables hooks."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        '[features]\n'
        'hooks = false # user disabled hooks\n\n'
        '[profiles.default]\n'
        'model = "gpt-5"\n',
        encoding="utf-8",
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )

    before = detect(plan)
    assert any(
        i.item == "hook-bridge"
        and i.direction == "modified"
        and "features.hooks" in i.detail
        and "apply will enable" in i.detail
        for i in before.items
    ), [i.detail for i in before.items]

    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    text = settings.read_text(encoding="utf-8")
    assert "hooks = true # user disabled hooks" in text
    data = _read_toml(settings)
    assert data["features"]["hooks"] is True
    assert data["profiles"]["default"]["model"] == "gpt-5"
    assert "codex_hook_bridge PreToolUse" in data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert not any(i.item == "hook-bridge" for i in detect(plan).items), \
        [i.detail for i in detect(plan).items]


def test_codex_hook_bridge_enables_deprecated_codex_hooks_flag(fake_agent_tools, tmp_path):
    """Deprecated top-level codex_hooks=false also disables hooks and must be converged."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    settings.write_text('codex_hooks = false\nmodel = "gpt-5"\n', encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )

    before = detect(plan)
    assert any(
        i.item == "hook-bridge"
        and i.direction == "modified"
        and "codex_hooks" in i.detail
        for i in before.items
    ), [i.detail for i in before.items]

    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    data = _read_toml(settings)
    assert data["codex_hooks"] is True
    assert data["model"] == "gpt-5"
    assert "codex_hook_bridge Stop" in data["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert not any(i.item == "hook-bridge" for i in detect(plan).items), \
        [i.detail for i in detect(plan).items]


def test_codex_hook_bridge_enables_top_level_features_hooks(fake_agent_tools, tmp_path):
    """Top-level dotted features.hooks=false is equivalent to [features].hooks=false."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    settings.write_text('features.hooks = false\nmodel = "gpt-5"\n', encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )

    before = detect(plan)
    assert any(
        i.item == "hook-bridge"
        and i.direction == "modified"
        and "features.hooks" in i.detail
        for i in before.items
    ), [i.detail for i in before.items]

    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    data = _read_toml(settings)
    assert data["features"]["hooks"] is True
    assert data["model"] == "gpt-5"
    assert "codex_hook_bridge Stop" in data["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert not any(i.item == "hook-bridge" for i in detect(plan).items), \
        [i.detail for i in detect(plan).items]


def test_codex_hook_bridge_disabled_hooks_with_toml_conflict_reports_conflict(fake_agent_tools, tmp_path):
    """If apply cannot merge TOML, status must not promise the disabled flag will be enabled."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    original = '[features]\nhooks = false\nnotes = """\n[hooks]\nPreToolUse = []\n"""\n'
    settings.write_text(original, encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )

    drift = detect(plan)
    assert any(
        i.item == "hook-bridge"
        and i.direction == "modified"
        and "multiline strings" in i.detail
        for i in drift.items
    ), [i.detail for i in drift.items]
    assert not any("apply will enable" in i.detail for i in drift.items)

    report = run_plan(plan)
    res = _bridge_results(report)
    assert res and res[0].status == "error", [r.detail for r in res]
    assert settings.read_text(encoding="utf-8") == original


def test_codex_hook_bridge_status_flags_disabled_hooks_after_bridge_written(fake_agent_tools, tmp_path):
    """A present managed block is still drift if Codex globally disables hooks."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )
    first = run_plan(plan)
    assert not first.errors, [r.detail for r in first.errors]
    settings.write_text(
        settings.read_text(encoding="utf-8") + "\n[features]\nhooks = false\n",
        encoding="utf-8",
    )

    drift = detect(plan)
    assert any(
        i.item == "hook-bridge"
        and i.direction == "modified"
        and "features.hooks" in i.detail
        for i in drift.items
    ), [i.detail for i in drift.items]

    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    assert _read_toml(settings)["features"]["hooks"] is True
    assert not any(i.item == "hook-bridge" for i in detect(plan).items), \
        [i.detail for i in detect(plan).items]


def test_codex_hook_bridge_preserves_existing_hooks_table(fake_agent_tools, tmp_path):
    """A user's unrelated Codex hook entries survive the managed bridge insertion."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        '[hooks]\n'
        'Notification = [{matcher = "idle", hooks = [{type = "command", command = "notify-send idle"}]}]\n\n'
        '[profiles.default]\n'
        'model = "gpt-5"\n',
        encoding="utf-8",
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]

    data = _read_toml(settings)
    assert data["hooks"]["Notification"][0]["hooks"][0]["command"] == "notify-send idle"
    assert "codex_hook_bridge PreToolUse" in data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    after = detect(plan)
    assert not any(i.item == "hook-bridge" for i in after.items), [i.detail for i in after.items]
    text = settings.read_text(encoding="utf-8")

    second = run_plan(plan)
    res = _bridge_results(second)
    assert res and all(r.status == "skipped" for r in res), [r.detail for r in res]
    assert settings.read_text(encoding="utf-8") == text


def test_codex_hook_bridge_existing_hooks_table_multiline_values_idempotent(fake_agent_tools, tmp_path):
    """Multiline arrays inside [hooks] are values, not table boundaries."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        '[hooks]\n'
        'Notification = [\n'
        '  [1, 2],\n'
        '  ["#"],\n'
        ']\n\n'
        '[profiles.default]\n'
        'model = "gpt-5"\n',
        encoding="utf-8",
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )

    first = run_plan(plan)
    assert not first.errors, [r.detail for r in first.errors]
    text = settings.read_text(encoding="utf-8")
    assert text.index("codex hook bridge") < text.index("[profiles.default]")
    data = _read_toml(settings)
    assert data["hooks"]["Notification"] == [[1, 2], ["#"]]
    assert data["profiles"]["default"]["model"] == "gpt-5"
    assert "codex_hook_bridge PreToolUse" in data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]

    second = run_plan(plan)
    res = _bridge_results(second)
    assert res and all(r.status == "skipped" for r in res), [r.detail for r in res]
    assert settings.read_text(encoding="utf-8") == text


def test_codex_hook_bridge_idempotent_reapply(fake_agent_tools, tmp_path):
    """A second Codex apply is a no-op and does not duplicate the managed TOML block."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )
    first = run_plan(plan)
    assert not first.errors, [r.detail for r in first.errors]
    before = settings.read_text(encoding="utf-8")

    second = run_plan(plan)
    res = _bridge_results(second)
    assert res and all(r.status == "skipped" for r in res), [r.detail for r in res]
    assert settings.read_text(encoding="utf-8") == before
    assert before.count("# >>> rig managed: codex hook bridge") == 1
    data = _read_toml(settings)
    assert "codex_hook_bridge PreToolUse" in data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]


def test_codex_hook_bridge_and_haft_mcp_second_apply_idempotent(fake_agent_tools, tmp_path):
    """Codex hook bridge and Haft MCP can co-manage .codex/config.toml without block churn."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    cfg = _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                      hook_bridge={"enabled": True})
    cfg.data["project_tools"] = {
        "enabled": True,
        "haft": {"enabled": True},
        "serena": {"enabled": False},
        "sverklo": {"enabled": False},
    }
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(cfg, cat, project_type="unknown")

    first = run_plan(plan)
    assert not first.errors, [r.detail for r in first.errors]
    text = settings.read_text(encoding="utf-8")
    assert "rig managed: codex hook bridge" in text
    assert "rig managed: haft mcp" in text

    second = run_plan(plan)
    assert not second.errors, [r.detail for r in second.errors]
    assert settings.read_text(encoding="utf-8") == text
    assert not any(i.target == settings for i in detect(plan).items), [i.detail for i in detect(plan).items]


def test_codex_hook_bridge_conflicts_with_existing_hook_event(fake_agent_tools, tmp_path):
    """An unmanaged Codex event key is not clobbered by the managed bridge block."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    original = '[hooks]\nPreToolUse = []\n'
    settings.write_text(original, encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)
    res = _bridge_results(report)
    assert res and res[0].status == "error", [r.detail for r in res]
    assert "unmanaged hooks.PreToolUse already exists" in res[0].detail
    assert settings.read_text(encoding="utf-8") == original
    drift = detect(plan)
    assert any(
        i.item == "hook-bridge"
        and i.direction == "modified"
        and "will not overwrite unmanaged Codex hooks TOML" in i.detail
        for i in drift.items
    ), \
        [i.detail for i in drift.items]


def test_codex_hook_bridge_inline_hooks_conflict_stays_valid(fake_agent_tools, tmp_path):
    """Inline hooks tables cannot be merged safely, so apply leaves them untouched."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    original = 'model = "gpt-5"\nhooks = { Notification = [] }\n'
    settings.write_text(original, encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)
    res = _bridge_results(report)
    assert res and res[0].status == "error", [r.detail for r in res]
    assert "unmanaged hooks inline table already exists" in res[0].detail
    assert settings.read_text(encoding="utf-8") == original
    assert "rig managed: codex hook bridge" not in settings.read_text(encoding="utf-8")
    assert _read_toml(settings)["hooks"]["Notification"] == []


def test_codex_hook_bridge_dotted_hooks_conflict_stays_valid(fake_agent_tools, tmp_path):
    """Top-level dotted hooks keys cannot be combined safely with a managed [hooks] block."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    original = "hooks.Notification = []\n"
    settings.write_text(original, encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)
    res = _bridge_results(report)
    assert res and res[0].status == "error", [r.detail for r in res]
    assert "unmanaged hooks.Notification uses dotted hooks TOML" in res[0].detail
    assert settings.read_text(encoding="utf-8") == original
    assert _read_toml(settings)["hooks"]["Notification"] == []


def test_codex_hook_bridge_nested_hooks_table_conflict_stays_valid(fake_agent_tools, tmp_path):
    """Nested [hooks.*] tables are user-owned hooks TOML, so the bridge fails closed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    original = '[hooks.foo]\ncommand = "legacy"\n'
    settings.write_text(original, encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)
    res = _bridge_results(report)
    assert res and res[0].status == "error", [r.detail for r in res]
    assert "unmanaged [hooks.foo] table already exists" in res[0].detail
    assert settings.read_text(encoding="utf-8") == original
    assert _read_toml(settings)["hooks"]["foo"]["command"] == "legacy"


@pytest.mark.parametrize(
    ("original", "assert_state"),
    [
        (
            '[hooks.state]\n',
            lambda data: data["hooks"]["state"] == {},
        ),
        (
            '[hooks.state."/Users/ultra/.codex/hooks.json:stop:0:0"]\ntrusted_hash = "sha256:abc"\n',
            lambda data: data["hooks"]["state"]["/Users/ultra/.codex/hooks.json:stop:0:0"]["trusted_hash"]
            == "sha256:abc",
        ),
        (
            'hooks.state."/Users/ultra/.codex/hooks.json:stop:0:0".trusted_hash = "sha256:abc"\n',
            lambda data: data["hooks"]["state"]["/Users/ultra/.codex/hooks.json:stop:0:0"]["trusted_hash"]
            == "sha256:abc",
        ),
        (
            'hooks.state."/Users/ultra/.codex/hooks.json:stop:0:0".trusted_hash = "sha256:abc"\n'
            "[profile.default]\nmodel = \"gpt-5\"\n",
            lambda data: (
                data["hooks"]["state"]["/Users/ultra/.codex/hooks.json:stop:0:0"]["trusted_hash"]
                == "sha256:abc"
                and data["profile"]["default"]["model"] == "gpt-5"
            ),
        ),
    ],
)
def test_codex_hook_bridge_preserves_hooks_state_table(fake_agent_tools, tmp_path, original, assert_state):
    """Codex hook trust metadata under [hooks.state] is not an event hook and must be preserved."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    settings.write_text(original, encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)
    res = _bridge_results(report)
    assert res and res[0].status in {"created", "updated", "backed_up"}, [r.detail for r in res]
    data = _read_toml(settings)
    assert_state(data)
    assert "PreToolUse" in data["hooks"]
    assert "PostToolUse" in data["hooks"]
    assert "Stop" in data["hooks"]
    assert not any(i.target == settings for i in detect(plan).items), [i.detail for i in detect(plan).items]


def test_codex_hook_bridge_migrates_existing_managed_block_when_hooks_state_is_dotted(
    fake_agent_tools, tmp_path
):
    """A later Codex dotted hooks.state key must not conflict with an older managed [hooks] block."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )
    first = run_plan(plan)
    assert not first.errors, [r.detail for r in first.errors]
    managed_table_block = settings.read_text(encoding="utf-8")
    assert "[hooks]" in managed_table_block

    state_key = 'hooks.state."/Users/ultra/.codex/hooks.json:stop:0:0".trusted_hash = "sha256:abc"\n'
    settings.write_text(state_key + "\n" + managed_table_block, encoding="utf-8")

    before_migration = detect(plan)
    assert any(
        i.item == "hook-bridge"
        and i.direction == "modified"
        and "stale" in i.detail
        for i in before_migration.items
    ), [i.detail for i in before_migration.items]

    second = run_plan(plan)

    assert not second.errors, [r.detail for r in second.errors]
    text = settings.read_text(encoding="utf-8")
    data = _read_toml(settings)
    assert data["hooks"]["state"]["/Users/ultra/.codex/hooks.json:stop:0:0"]["trusted_hash"] == "sha256:abc"
    assert "hooks.PreToolUse" in text
    assert "[hooks]" not in text
    assert not any(i.target == settings for i in detect(plan).items), [i.detail for i in detect(plan).items]


def test_codex_hook_bridge_allows_multiline_hooks_state_dotted_key(fake_agent_tools, tmp_path):
    """Multiline Codex trust metadata under hooks.state is not an event-hook conflict."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        'hooks.state."/Users/ultra/.codex/hooks.json:stop:0:0".trusted_hashes = [\n'
        '  "sha256:abc",\n'
        ']\n',
        encoding="utf-8",
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)

    assert not report.errors, [r.detail for r in report.errors]
    data = _read_toml(settings)
    state = data["hooks"]["state"]["/Users/ultra/.codex/hooks.json:stop:0:0"]
    assert state["trusted_hashes"] == ["sha256:abc"]
    assert "PreToolUse" in data["hooks"]
    assert not any(i.target == settings for i in detect(plan).items), [i.detail for i in detect(plan).items]


def test_codex_hook_bridge_allows_hooks_state_continuation_with_equals(fake_agent_tools, tmp_path):
    """Continuation lines with '=' inside hooks.state metadata must stay inside the ignored value."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        'hooks.state."/Users/ultra/.codex/hooks.json:stop:0:0".trusted_hashes = [\n'
        '  "sha256:abc=def",\n'
        ']\n',
        encoding="utf-8",
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)

    assert not report.errors, [r.detail for r in report.errors]
    data = _read_toml(settings)
    state = data["hooks"]["state"]["/Users/ultra/.codex/hooks.json:stop:0:0"]
    assert state["trusted_hashes"] == ["sha256:abc=def"]
    assert "PreToolUse" in data["hooks"]
    assert not any(i.target == settings for i in detect(plan).items), [i.detail for i in detect(plan).items]


def test_codex_hook_bridge_drift_flags_dotted_block_moved_below_table(fake_agent_tools, tmp_path):
    """Dotted managed keys must stay before TOML tables or they become table-relative."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        'hooks.state."/Users/ultra/.codex/hooks.json:stop:0:0".trusted_hash = "sha256:abc"\n\n'
        "[profile.default]\n"
        'model = "gpt-5"\n',
        encoding="utf-8",
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )
    first = run_plan(plan)
    assert not first.errors, [r.detail for r in first.errors]

    text = settings.read_text(encoding="utf-8")
    begin = "# >>> rig managed: codex hook bridge"
    end_marker = "# <<< rig managed: codex hook bridge"
    start = text.index(begin)
    end = text.index(end_marker, start) + len(end_marker)
    if end < len(text) and text[end] == "\n":
        end += 1
    block = text[start:end]
    settings.write_text((text[:start] + text[end:]).rstrip() + "\n\n" + block, encoding="utf-8")

    before_repair = detect(plan)

    assert any(
        i.item == "hook-bridge"
        and i.direction == "modified"
        and "stale" in i.detail
        for i in before_repair.items
    ), [i.detail for i in before_repair.items]

    second = run_plan(plan)

    assert not second.errors, [r.detail for r in second.errors]
    repaired = settings.read_text(encoding="utf-8")
    assert repaired.index(begin) < repaired.index("[profile.default]")
    assert not any(i.target == settings for i in detect(plan).items), [i.detail for i in detect(plan).items]


def test_codex_hook_bridge_quoted_event_key_conflicts(fake_agent_tools, tmp_path):
    """Quoted TOML keys are semantically equivalent to bare event keys."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    original = '[hooks]\n"PreToolUse" = []\n'
    settings.write_text(original, encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)
    res = _bridge_results(report)
    assert res and res[0].status == "error", [r.detail for r in res]
    assert "unmanaged hooks.PreToolUse already exists" in res[0].detail
    assert settings.read_text(encoding="utf-8") == original


def test_codex_hook_bridge_quoted_hooks_table_is_preserved(fake_agent_tools, tmp_path):
    """A quoted ["hooks"] table is still the hooks table; merge into it instead of duplicating it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    settings.write_text('["hooks"]\nNotification = []\n', encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    data = _read_toml(settings)
    assert data["hooks"]["Notification"] == []
    assert "codex_hook_bridge PreToolUse" in data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]


def test_codex_hook_bridge_marker_text_inside_string_is_ignored(fake_agent_tools, tmp_path):
    """Managed markers only count on standalone lines, not inside user TOML strings."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        'note = "# >>> rig managed: codex hook bridge # <<< rig managed: codex hook bridge"\n',
        encoding="utf-8",
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    data = _read_toml(settings)
    assert data["note"].startswith("# >>> rig managed")
    assert "codex_hook_bridge Stop" in data["hooks"]["Stop"][0]["hooks"][0]["command"]


def test_codex_hook_bridge_array_hooks_conflict_stays_valid(fake_agent_tools, tmp_path):
    """Array-of-tables hooks TOML is not mistaken for a normal [hooks] table."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    original = '[[hooks]]\nname = "legacy"\n'
    settings.write_text(original, encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)
    res = _bridge_results(report)
    assert res and res[0].status == "error", [r.detail for r in res]
    assert "unmanaged [[hooks]] array-of-tables already exists" in res[0].detail
    assert settings.read_text(encoding="utf-8") == original
    assert _read_toml(settings)["hooks"][0]["name"] == "legacy"


def test_codex_hook_bridge_multiline_string_conflict_stays_valid(fake_agent_tools, tmp_path):
    """Multiline strings can contain table-looking text, so the merge fails closed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    original = 'notes = """\n[hooks]\nPreToolUse = []\n"""\n'
    settings.write_text(original, encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)
    res = _bridge_results(report)
    assert res and res[0].status == "error", [r.detail for r in res]
    assert "TOML multiline strings are unsupported" in res[0].detail
    assert settings.read_text(encoding="utf-8") == original
    drift = detect(plan)
    assert any(
        i.item == "hook-bridge" and i.direction == "modified" and "multiline strings" in i.detail
        for i in drift.items
    ), [i.detail for i in drift.items]


def test_codex_hook_bridge_allows_profile_local_hooks_key(fake_agent_tools, tmp_path):
    """A hooks key inside another TOML table is unrelated to Codex's top-level hooks table."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    settings.write_text('[profiles.default]\nhooks = "profile-local"\n', encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    data = _read_toml(settings)
    assert data["profiles"]["default"]["hooks"] == "profile-local"
    assert "codex_hook_bridge PreToolUse" in data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]


def test_codex_hook_bridge_skip_leaves_stale_managed_block_untouched(fake_agent_tools, tmp_path):
    """on_conflict=skip leaves a stale managed Codex block untouched, matching JSON bridge parity."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )
    first = run_plan(plan)
    assert not first.errors, [r.detail for r in first.errors]
    stale = settings.read_text(encoding="utf-8").replace(str(fake_agent_tools / "lib"), "/old/lib")
    assert "/old/lib" in stale
    settings.write_text(stale, encoding="utf-8")

    cfg = _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                      hook_bridge={"enabled": True})
    cfg.data["defaults"] = {"on_conflict": "skip"}
    skip_plan = build(cfg, cat, project_type="unknown")
    report = run_plan(skip_plan)
    res = _bridge_results(report)
    assert res and all(r.status == "skipped" for r in res), [r.detail for r in res]
    assert settings.read_text(encoding="utf-8") == stale


def test_codex_hook_bridge_backup_rewrites_stale_managed_block(fake_agent_tools, tmp_path):
    """Default backup policy preserves a stale managed block before converging it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )
    first = run_plan(plan)
    assert not first.errors, [r.detail for r in first.errors]
    stale = settings.read_text(encoding="utf-8").replace(str(fake_agent_tools / "lib"), "/old/lib")
    assert "/old/lib" in stale
    settings.write_text(stale, encoding="utf-8")

    report = run_plan(plan)
    res = _bridge_results(report)
    assert res and all(r.status == "backed_up" for r in res), [r.detail for r in res]
    assert "/old/lib" not in settings.read_text(encoding="utf-8")
    backups = sorted(settings.parent.glob("config.toml.rig-bak-*"))
    assert backups
    assert backups[-1].read_text(encoding="utf-8") == stale
    assert not any(i.item == "hook-bridge" for i in detect(plan).items), \
        [i.detail for i in detect(plan).items]
