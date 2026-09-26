"""install-skill — writes the rig SKILL.md AND links it into the harness discovery dir.

These tests are HOME-isolated (the autouse `_isolate_home` fixture points HOME at a tmp dir),
so they exercise the real symlink logic without touching the developer's ~/.claude/skills.
"""

from __future__ import annotations

import os
from pathlib import Path

from riglib.install import SKILL_NAME, install_skill


def _harness_link(home: Path) -> Path:
    return home / ".claude" / "skills" / SKILL_NAME


def _codex_link(home: Path) -> Path:
    return home / ".codex" / "skills" / SKILL_NAME


def test_install_skill_writes_md_and_harness_link(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    rc = install_skill()
    assert rc == 0
    md = home / ".agents" / "skills" / SKILL_NAME / "SKILL.md"
    assert md.is_file()
    link = _harness_link(home)
    assert link.is_symlink(), "rig skill not symlinked into the harness dir"
    assert link.resolve() == md.parent.resolve()
    assert (link / "SKILL.md").is_file()  # the link resolves to the real skill
    codex_link = _codex_link(home)
    assert codex_link.is_symlink(), "rig skill not symlinked into the Codex skill dir"
    assert codex_link.resolve() == md.parent.resolve()


def test_install_skill_ignores_ambient_codex_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    ambient_codex_home = tmp_path / "ambient-codex-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(ambient_codex_home))

    assert install_skill() == 0

    md = home / ".agents" / "skills" / SKILL_NAME / "SKILL.md"
    codex_link = _codex_link(home)
    assert codex_link.is_symlink(), "rig skill not symlinked into the stable Codex user skills dir"
    assert codex_link.resolve() == md.parent.resolve()
    assert not (ambient_codex_home / "skills" / SKILL_NAME).exists()


def test_install_skill_links_explicit_rig_codex_home_when_set(tmp_path, monkeypatch):
    home = tmp_path / "home"
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("RIG_CODEX_HOME", str(codex_home))

    assert install_skill() == 0

    md = home / ".agents" / "skills" / SKILL_NAME / "SKILL.md"
    codex_link = codex_home / "skills" / SKILL_NAME
    assert codex_link.is_symlink(), "rig skill not symlinked into RIG_CODEX_HOME skills dir"
    assert codex_link.resolve() == md.parent.resolve()
    assert not _codex_link(home).exists()


def test_install_skill_expands_rig_codex_home_env_vars(tmp_path, monkeypatch):
    home = tmp_path / "home"
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv("RIG_CODEX_HOME", "$XDG_CONFIG_HOME/codex")

    assert install_skill() == 0

    md = home / ".agents" / "skills" / SKILL_NAME / "SKILL.md"
    codex_link = xdg / "codex" / "skills" / SKILL_NAME
    assert codex_link.is_symlink(), "rig skill not symlinked into expanded RIG_CODEX_HOME skills dir"
    assert codex_link.resolve() == md.parent.resolve()
    assert not (home / "$XDG_CONFIG_HOME").exists()


def test_install_skill_expands_rig_codex_home_xdg_config_prefix(tmp_path, monkeypatch):
    home = tmp_path / "home"
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv("RIG_CODEX_HOME", "~/.config/codex")

    assert install_skill() == 0

    md = home / ".agents" / "skills" / SKILL_NAME / "SKILL.md"
    codex_link = xdg / "codex" / "skills" / SKILL_NAME
    assert codex_link.is_symlink(), "rig skill not symlinked into XDG-expanded RIG_CODEX_HOME"
    assert codex_link.resolve() == md.parent.resolve()
    assert not (home / ".config" / "codex" / "skills" / SKILL_NAME).exists()


def test_install_skill_md_lists_config_command(tmp_path, monkeypatch):
    # the embedded SKILL.md is the agent-facing command catalog — it must mention `rig config`
    # so agents discover the recommended single-key edit path (help-docs-sync across files).
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    assert install_skill() == 0
    md = (home / ".agents" / "skills" / SKILL_NAME / "SKILL.md").read_text(encoding="utf-8")
    assert "rig config" in md
    assert "rig config set" in md


def test_install_skill_idempotent(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    assert install_skill() == 0
    capsys.readouterr()
    assert install_skill() == 0  # second run is a clean no-op
    out = capsys.readouterr().out
    assert "already current" in out
    # the link still resolves correctly
    link = _harness_link(home)
    assert link.is_symlink()
    assert _codex_link(home).is_symlink()


def test_install_skill_leaves_real_harness_dir(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    # a hand-authored rig skill already occupies the harness path as a REAL dir
    real = home / ".claude" / "skills" / SKILL_NAME
    real.mkdir(parents=True)
    (real / "SKILL.md").write_text("hand-authored\n", encoding="utf-8")
    assert install_skill() == 0
    assert not real.is_symlink()  # left untouched
    assert (real / "SKILL.md").read_text() == "hand-authored\n"
    assert "left untouched" in capsys.readouterr().out


def test_install_skill_repoints_wrong_harness_link(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    link = _harness_link(home)
    link.parent.mkdir(parents=True)
    bogus = home / "elsewhere"
    bogus.mkdir()
    link.symlink_to(bogus)  # stale link
    assert install_skill() == 0
    expected = (home / ".agents" / "skills" / SKILL_NAME)
    assert link.resolve() == expected.resolve()  # re-pointed to the real skill


def _set_home(monkeypatch, home: Path) -> None:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # expanduser's source on Windows


def test_link_falls_back_to_junction_without_symlink_privilege(tmp_path, monkeypatch):
    # Windows without Developer Mode refuses symlinks (WinError 1314); the skill must still reach
    # the harness dir, via a directory junction.
    import sys
    import types

    from riglib import install as install_mod

    def no_privilege(self, target, target_is_directory=False):
        err = OSError(1, "A required privilege is not held by the client")
        err.winerror = install_mod._ERROR_PRIVILEGE_NOT_HELD
        raise err

    junctions = []
    fake_winapi = types.SimpleNamespace(CreateJunction=lambda src, dst: junctions.append((src, dst)))
    monkeypatch.setattr(Path, "symlink_to", no_privilege)
    monkeypatch.setitem(sys.modules, "_winapi", fake_winapi)

    dest, link = tmp_path / "skill", tmp_path / "harness" / "rig"
    dest.mkdir()
    install_mod._link_dir(link, dest)
    assert junctions == [(str(dest), str(link))]


def test_link_reraises_other_symlink_errors(tmp_path, monkeypatch):
    import pytest

    from riglib import install as install_mod

    def boom(self, target, target_is_directory=False):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(Path, "symlink_to", boom)
    with pytest.raises(PermissionError):
        install_mod._link_dir(tmp_path / "l", tmp_path)


def test_install_skill_real_junction_is_current_on_rerun(tmp_path, monkeypatch, capsys):
    import pytest

    if os.name != "nt":
        pytest.skip("directory junctions are Windows-only")
    import _winapi

    home = tmp_path / "home"
    _set_home(monkeypatch, home)
    skill = home / ".agents" / "skills" / SKILL_NAME
    skill.mkdir(parents=True)
    link = _harness_link(home)
    link.parent.mkdir(parents=True)
    _winapi.CreateJunction(str(skill), str(link))

    assert install_skill() == 0
    assert "harness link already current" in capsys.readouterr().out
    assert (link / "SKILL.md").is_file()
