"""`rig report` CLI wiring — argparse-complete, subcommand dispatch, pinned "not yet
implemented" exit codes. Each pinned exit code is a red-first marker: a later real
implementation of `init`/`new`/`periodic` must change these tests deliberately, not
leave a scaffold stub passing by accident."""

from __future__ import annotations

import pytest

from riglib.cli import main
from riglib.report.command import NOT_IMPLEMENTED_EXIT_CODE


def test_report_help_lists_all_three_subcommands(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["report", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "init" in out
    assert "new" in out
    assert "periodic" in out


def test_report_bare_prints_usage_and_returns_not_implemented(capsys):
    rc = main(["report"])
    assert rc == NOT_IMPLEMENTED_EXIT_CODE
    assert "usage: rig report" in capsys.readouterr().err


def test_report_init_is_not_yet_implemented(capsys):
    rc = main(["report", "init", "alex-mextner/rig-reports"])
    assert rc == NOT_IMPLEMENTED_EXIT_CODE
    assert "not yet implemented" in capsys.readouterr().err


def test_report_new_requires_task_id(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["report", "new"])
    assert exc.value.code == 2  # argparse's own missing-required-arg exit code


def test_report_new_is_not_yet_implemented(capsys):
    rc = main(["report", "new", "HYP-1234"])
    assert rc == NOT_IMPLEMENTED_EXIT_CODE
    assert "not yet implemented" in capsys.readouterr().err


def test_report_periodic_is_not_yet_implemented(capsys):
    rc = main(["report", "periodic", "--since", "7d"])
    assert rc == NOT_IMPLEMENTED_EXIT_CODE
    assert "not yet implemented" in capsys.readouterr().err


def test_report_periodic_accepts_repeatable_repo_flag(capsys):
    # Argparse wiring only — must accept multiple --repo without erroring before
    # hitting the not-yet-implemented stub.
    rc = main(["report", "periodic", "--repo", "o/a", "--repo", "o/b"])
    assert rc == NOT_IMPLEMENTED_EXIT_CODE
