"""A backup rig leaves next to a replaced file must not keep the file's executable bits."""

from __future__ import annotations

from pathlib import Path

from riglib.actions.fsutil import write_file

OLD = "#!/bin/sh\nexit 1\n"
NEW = "#!/bin/sh\nexit 0\n"


def test_backup_of_an_executable_hook_is_not_executable(tmp_path):
    hook = tmp_path / "20-python-only"
    hook.write_text(OLD, encoding="utf-8")
    hook.chmod(0o755)

    outcome = write_file(hook, NEW, "backup")

    assert outcome.status == "backed_up"
    assert outcome.backup is not None
    assert outcome.backup.name.startswith("20-python-only.rig-bak-")
    assert outcome.backup.read_text(encoding="utf-8") == OLD
    assert outcome.backup.stat().st_mode & 0o111 == 0
    assert hook.read_text(encoding="utf-8") == NEW


def test_backup_keeps_read_write_bits(tmp_path):
    target = tmp_path / "config"
    target.write_text("a", encoding="utf-8")
    target.chmod(0o640)

    outcome = write_file(target, "b", "backup")

    assert outcome.backup is not None
    assert outcome.backup.stat().st_mode & 0o777 == 0o640


def test_backup_of_a_directory_is_left_as_it_was(tmp_path):
    target = tmp_path / "skill"
    target.mkdir()
    (target / "SKILL.md").write_text("x", encoding="utf-8")

    outcome = write_file(target, "now a file", "backup")

    assert outcome.backup is not None and outcome.backup.is_dir()
    assert outcome.backup.stat().st_mode & 0o111 != 0


def test_a_failing_chmod_still_writes_the_replacement(tmp_path, monkeypatch):
    hook = tmp_path / "20-python-only"
    hook.write_text(OLD, encoding="utf-8")
    hook.chmod(0o755)

    def refuse(self, mode, *, follow_symlinks=True):
        raise PermissionError("not the owner")

    monkeypatch.setattr(Path, "chmod", refuse)

    outcome = write_file(hook, NEW, "backup")

    assert outcome.status == "backed_up"
    assert hook.read_text(encoding="utf-8") == NEW
