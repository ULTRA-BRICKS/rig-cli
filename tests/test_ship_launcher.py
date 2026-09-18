"""Installed ship must execute with its canonical sibling helpers available."""
from pathlib import Path
import subprocess
from riglib.actions.runner import _do_install_ci
from riglib.drift import DriftReport, _check_ci
from riglib.plan import Action


def fixture(tmp_path):
    source = tmp_path / "catalog with spaces" / "ci" / "ship"
    source.mkdir(parents=True)
    (source / "helper.txt").write_text("sibling-policy-ok\n")
    script = source / "ship.sh"
    script.write_text('#!/bin/sh\ncat "$(dirname "$0")/helper.txt" || exit 12\nprintf "arg=%s\\n" "$1"\n')
    script.chmod(0o755)
    return Action("install_ci", "ci", "ship", source, tmp_path / "bin", {"slot": "ship"})


def test_installed_ship_preserves_helpers_and_argument_boundaries(tmp_path):
    action = fixture(tmp_path)
    _do_install_ci(action, "backup")
    result = subprocess.run([str(action.target / "ship"), "argument with spaces"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "sibling-policy-ok\narg=argument with spaces\n"


def test_reapply_is_idempotent_and_drift_tracks_the_launcher(tmp_path):
    action = fixture(tmp_path)
    _do_install_ci(action, "backup")
    assert _do_install_ci(action, "backup").status == "skipped"
    report = DriftReport()
    _check_ci(action, report)
    assert report.in_sync
    (action.target / "ship").write_text("changed launcher")
    _check_ci(action, report)
    assert not report.in_sync


def test_legacy_copy_is_backed_up_without_copying_any_siblings(tmp_path):
    action = fixture(tmp_path)
    action.target.mkdir()
    target = action.target / "ship"
    target.write_text("legacy standalone copy")
    result = _do_install_ci(action, "backup")
    assert result.backup.read_text() == "legacy standalone copy"
    assert not (action.target / "helper.txt").exists()


def test_canonical_update_is_used_without_reinstall(tmp_path):
    action = fixture(tmp_path)
    _do_install_ci(action, "backup")
    (action.source / "helper.txt").write_text("updated-policy\n")
    result = subprocess.run([str(action.target / "ship"), "x"], capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout.startswith("updated-policy\n")


def test_versioned_catalog_symlink_keeps_following_the_selected_version(tmp_path):
    first = fixture(tmp_path / "first")
    second = fixture(tmp_path / "second")
    (second.source / "helper.txt").write_text("new-version-policy\n")
    selected = tmp_path / "selected"
    selected.symlink_to(first.source.parent.parent, target_is_directory=True)
    action = Action("install_ci", "ci", "ship", selected / "ci" / "ship", tmp_path / "bin", {"slot": "ship"})
    _do_install_ci(action, "backup")
    selected.unlink()
    selected.symlink_to(second.source.parent.parent, target_is_directory=True)
    result = subprocess.run([str(action.target / "ship"), "x"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("new-version-policy\n")
