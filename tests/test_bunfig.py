"""Hermetic tests for the bunfig global-store provisioner (rig-cli#416)."""

from __future__ import annotations

from pathlib import Path

import pytest

from riglib import bunfig
from riglib.actions.runner import _do_provision_bunfig
from riglib.config import ConfigError, LoadedConfig, validate
from riglib.plan import Action, InstallPlan, _build_bun


def _action(path: Path) -> Action:
    return Action(
        kind="provision_bunfig",
        category="bun",
        item="bunfig",
        source=path.parent,
        target=path,
    )


def test_managed_block_sets_isolated_linker_and_global_store():
    text = bunfig.managed_block()
    assert bunfig.BEGIN_MARKER in text
    assert bunfig.END_MARKER in text
    assert 'linker = "isolated"' in text
    assert "globalStore = true" in text
    assert bunfig.has_required_install_keys(text)
    # bun is GLOBAL-only; `config set` previews unless --commit writes + reconciles.
    assert "rig config set bun.enabled true --global --commit" in text
    assert "rig config set bun.enabled false --global --commit" in text
    assert "Hand edits inside this block are replaced on the next `rig apply`" in text


def test_has_install_global_store_ignores_other_tables_and_comments():
    assert not bunfig.has_install_global_store("# globalStore = true\n")
    assert not bunfig.has_install_global_store(
        "[install.scopes.\"https://example.invalid/\"]\nglobalStore = true\n"
    )
    assert bunfig.has_install_global_store(
        "[run]\nbun = true\n\n[install]\nexact = true\nglobalStore = true\n"
    )


def test_has_install_isolated_linker_ignores_other_tables_and_comments():
    assert not bunfig.has_install_isolated_linker('# linker = "isolated"\n')
    assert not bunfig.has_install_isolated_linker(
        "[install.scopes.\"https://example.invalid/\"]\nlinker = \"isolated\"\n"
    )
    assert bunfig.has_install_isolated_linker(
        "[run]\nbun = true\n\n[install]\nexact = true\nlinker = \"isolated\"\n"
    )


def test_has_required_install_keys_needs_both():
    both = "[install]\nexact = true\nlinker = \"isolated\"\nglobalStore = true\n"
    assert bunfig.has_required_install_keys(both)
    assert not bunfig.has_required_install_keys("[install]\nglobalStore = true\n")
    assert not bunfig.has_required_install_keys("[install]\nlinker = \"isolated\"\n")


def test_resolve_missing_is_create(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    r = bunfig.resolve()
    assert r.state == "create"
    assert r.path == tmp_path / ".bunfig.toml"
    assert bunfig.has_required_install_keys(r.new_content)


def test_resolve_honors_xdg_config_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    xdg = tmp_path / "xdg"
    home.mkdir()
    xdg.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    r = bunfig.resolve()
    assert r.path == xdg / ".bunfig.toml"


def test_resolve_identical_managed_is_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    p = tmp_path / ".bunfig.toml"
    p.write_text(bunfig.managed_block() + "\n", encoding="utf-8")
    assert bunfig.resolve().state == "ok"


def test_resolve_handwritten_with_both_keys_is_ok_without_taking_ownership(tmp_path):
    p = tmp_path / "custom.toml"
    p.write_text(
        "[install]\nexact = true\nlinker = \"isolated\"\nglobalStore = true\n",
        encoding="utf-8",
    )
    r = bunfig.resolve(p)
    assert r.state == "ok"
    assert r.new_content == ""



def test_resolve_install_table_with_inline_comment_is_still_conflict(tmp_path):
    p = tmp_path / "hand.toml"
    original = "[install] # package manager defaults\nexact = true\n"
    p.write_text(original, encoding="utf-8")
    r = bunfig.resolve(p)
    assert r.state == "conflict"
    assert "invalid TOML" in r.detail
    assert p.read_text(encoding="utf-8") == original


def test_has_install_table_accepts_inline_comment():
    assert bunfig.has_install_table("[install] # comment\nexact = true\n")
    assert not bunfig.has_install_table("# [install]\nexact = true\n")


def test_resolve_existing_install_table_without_globalstore_is_conflict(tmp_path):
    p = tmp_path / "hand.toml"
    original = "[install]\nexact = true\nregistry = \"https://example.invalid\"\n"
    p.write_text(original, encoding="utf-8")
    r = bunfig.resolve(p)
    assert r.state == "conflict"
    assert "invalid TOML" in r.detail
    assert "execpolicy" not in r.detail
    assert p.read_text(encoding="utf-8") == original


def test_resolve_existing_install_table_without_linker_is_conflict(tmp_path):
    p = tmp_path / "hand.toml"
    original = "[install]\nexact = true\nglobalStore = true\n"
    p.write_text(original, encoding="utf-8")
    r = bunfig.resolve(p)
    assert r.state == "conflict"
    assert "invalid TOML" in r.detail
    assert p.read_text(encoding="utf-8") == original


def test_resolve_handwritten_without_install_table_appends_block(tmp_path):
    p = tmp_path / "hand.toml"
    p.write_text("[test]\ncoverage = false\n", encoding="utf-8")
    r = bunfig.resolve(p)
    assert r.state == "create"
    assert "[test]\ncoverage = false\n" in r.new_content
    assert bunfig.BEGIN_MARKER in r.new_content
    assert bunfig.has_required_install_keys(r.new_content)



def test_resolve_handwritten_keys_with_inline_comments_is_ok(tmp_path):
    p = tmp_path / "custom.toml"
    p.write_text(
        "[install]\nexact = true\nlinker = \"isolated\" # share\nglobalStore = true # store\n",
        encoding="utf-8",
    )
    r = bunfig.resolve(p)
    assert r.state == "ok"
    assert r.new_content == ""


def test_resolve_stale_managed_block_with_extra_key_is_update(tmp_path):
    p = tmp_path / "stale.toml"
    p.write_text(
        bunfig.BEGIN_MARKER
        + "\n[install]\nlinker = \"isolated\"\nglobalStore = true\nregistry = \"https://example.invalid\"\n"
        + bunfig.END_MARKER
        + "\n",
        encoding="utf-8",
    )
    r = bunfig.resolve(p)
    assert r.state == "update"
    assert "registry" not in r.new_content
    assert bunfig.has_required_install_keys(r.new_content)


def test_resolve_non_utf8_is_conflict(tmp_path):
    p = tmp_path / "bin.toml"
    p.write_bytes(b"[install]\nlinker = \"isolated\"\nglobalStore = true\n\xff")
    r = bunfig.resolve(p)
    assert r.state == "conflict"
    assert "cannot read" in r.detail


def test_resolve_stale_managed_block_is_update(tmp_path):
    p = tmp_path / "stale.toml"
    p.write_text(
        bunfig.BEGIN_MARKER + "\n[install]\nglobalStore = false\n" + bunfig.END_MARKER + "\n",
        encoding="utf-8",
    )
    r = bunfig.resolve(p)
    assert r.state == "update"
    assert "globalStore = true" in r.new_content
    assert 'linker = "isolated"' in r.new_content


def test_resolve_unbalanced_markers_is_conflict(tmp_path):
    p = tmp_path / "broken.toml"
    p.write_text(bunfig.BEGIN_MARKER + "\nglobalStore = true\n", encoding="utf-8")
    r = bunfig.resolve(p)
    assert r.state == "conflict"
    assert "execpolicy" not in r.detail
    assert "bun managed-block" in r.detail


def test_apply_creates_then_skips(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    target = tmp_path / ".bunfig.toml"
    first = _do_provision_bunfig(_action(target), "backup")
    assert first.status == "created"
    assert bunfig.has_required_install_keys(target.read_text(encoding="utf-8"))
    second = _do_provision_bunfig(_action(target), "backup")
    assert second.status == "skipped"


def test_apply_appends_to_handwritten_without_install_table(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    target = tmp_path / ".bunfig.toml"
    target.write_text("[test]\ncoverage = false\n", encoding="utf-8")
    res = _do_provision_bunfig(_action(target), "backup")
    assert res.status in {"created", "updated", "backed_up"}
    text = target.read_text(encoding="utf-8")
    assert "coverage = false" in text
    assert bunfig.has_required_install_keys(text)


def test_apply_does_not_clobber_existing_install_table(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    target = tmp_path / ".bunfig.toml"
    original = "[install]\nexact = true\n"
    target.write_text(original, encoding="utf-8")
    res = _do_provision_bunfig(_action(target), "backup")
    assert res.status == "skipped"
    assert "conflict" in res.detail
    assert "execpolicy" not in res.detail
    assert target.read_text(encoding="utf-8") == original


def test_apply_conflict_does_not_clobber(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    target = tmp_path / ".bunfig.toml"
    original = bunfig.BEGIN_MARKER + "\nglobalStore = true\n"
    target.write_text(original, encoding="utf-8")
    res = _do_provision_bunfig(_action(target), "backup")
    assert res.status == "skipped"
    assert "conflict" in res.detail
    assert target.read_text(encoding="utf-8") == original


def test_apply_updates_stale_managed_block(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    target = tmp_path / ".bunfig.toml"
    target.write_text(
        bunfig.BEGIN_MARKER + "\n[install]\nglobalStore = false\n" + bunfig.END_MARKER + "\n",
        encoding="utf-8",
    )
    res = _do_provision_bunfig(_action(target), "backup")
    assert res.status in {"updated", "backed_up"}
    assert bunfig.has_required_install_keys(target.read_text(encoding="utf-8"))


def test_plan_emits_bunfig_action_only_when_enabled(tmp_path):
    def _cfg(data):
        return LoadedConfig(data=data, repo_root=tmp_path)

    plan = InstallPlan()
    _build_bun(_cfg({}), plan)
    assert not plan.actions
    plan = InstallPlan()
    _build_bun(_cfg({"bun": {"enabled": False}}), plan)
    assert not plan.actions
    plan = InstallPlan()
    _build_bun(_cfg({"bun": {}}), plan)
    assert not plan.actions
    plan = InstallPlan()
    _build_bun(_cfg({"bun": {"enabled": True}}), plan)
    assert len(plan.actions) == 1
    act = plan.actions[0]
    assert act.kind == "provision_bunfig"
    assert act.category == "bun"


def test_validate_bun_accepts_and_rejects():
    validate({"version": 1, "bun": {"enabled": True}})
    validate({"version": 1, "bun": {}})
    with pytest.raises(ConfigError, match="unknown bun key"):
        validate({"version": 1, "bun": {"bogus": 1}})
    with pytest.raises(ConfigError, match="bun.enabled must be a bool"):
        validate({"version": 1, "bun": {"enabled": "yes"}})


def test_bun_is_wired_into_registries():
    from riglib.actions.runner import _HANDLERS
    from riglib.areas import AREAS
    from riglib.config import _VALID_TOP_KEYS
    from riglib.layers import GLOBAL, layer_for_category
    from riglib.verify import _VERIFIERS

    assert "bun" in _VALID_TOP_KEYS
    assert "provision_bunfig" in _HANDLERS
    assert "provision_bunfig" in _VERIFIERS
    assert any(a.key == "bun" and a.layer == GLOBAL for a in AREAS)
    assert layer_for_category("bun") == GLOBAL
