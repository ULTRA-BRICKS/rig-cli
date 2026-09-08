"""`rig report` — HyperIDE /result-report gather+HTML, as a rig command.

A trustworthy test drives the real CLI (`riglib.cli.main`) and the real git
binary against a throwaway repo. It asserts the observable contract: an HTML
file a human can open, with the commits/files that actually landed since the
merge-base with main. It does not assert argparse wiring, field copies, or
"the helper returned a dataclass".
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from riglib.cli import main
from riglib import errors


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _repo_with_feature(root: Path) -> Path:
    """A repo on a feature branch with one commit past `main`."""
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "init")
    _git(repo, "checkout", "-qb", "feat/thing")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-qm", "feat: add app")
    return repo


def test_report_help_lists_the_command(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["report", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "completion report" in out.lower() or "result-report" in out.lower()
    assert "--out" in out
    assert "--title" in out


def test_report_writes_openable_html_with_commits_and_files(tmp_path, capsys):
    repo = _repo_with_feature(tmp_path)
    out = tmp_path / "report.html"

    rc = main(
        ["report", "HYP-1", "-C", str(repo), "--out", str(out), "--title", "Add app"]
    )

    assert rc == 0
    printed = capsys.readouterr().out
    assert str(out) in printed
    assert out.is_file()
    html = out.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in html
    assert "HYP-1" in html
    assert "Add app" in html
    assert "feat: add app" in html
    assert "app.py" in html
    # Self-contained: opening file:// must not need a reports repo's CSS/JS.
    assert "components.js" not in html


def test_report_prints_a_text_summary(tmp_path, capsys):
    repo = _repo_with_feature(tmp_path)
    out = tmp_path / "report.html"

    rc = main(["report", "HYP-1", "-C", str(repo), "--out", str(out)])

    assert rc == 0
    printed = capsys.readouterr().out
    assert "feat: add app" in printed
    assert "app.py" in printed


def test_report_outside_a_git_repo_exits_not_a_repo(tmp_path, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    out = tmp_path / "report.html"

    rc = main(["report", "HYP-1", "-C", str(plain), "--out", str(out)])

    assert rc == errors.EXIT_NOT_A_REPO
    assert not out.exists()
    # Structured RigError output goes to stdout (errors.guard), not stderr.
    printed = capsys.readouterr().out.lower()
    assert "not a git" in printed or "git repository" in printed


def test_report_rejects_a_path_shaped_task_id(tmp_path):
    repo = _repo_with_feature(tmp_path)
    out_dir = tmp_path / "safe"
    out_dir.mkdir()

    rc = main(
        [
            "report",
            "../escape",
            "-C",
            str(repo),
            "--out",
            str(out_dir / "index.html"),
        ]
    )

    assert rc == errors.EXIT_CONFIG
    assert not (tmp_path / "escape").exists()
    assert not (tmp_path / "escape.html").exists()


def test_report_escapes_script_breakout_in_commit_message(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "init")
    _git(repo, "checkout", "-qb", "feat/xss")
    (repo / "x.py").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "x.py")
    _git(repo, "commit", "-qm", "feat: </script><script>alert(1)</script>")
    out = tmp_path / "report.html"

    rc = main(["report", "HYP-xss", "-C", str(repo), "--out", str(out)])

    assert rc == 0
    html = out.read_text(encoding="utf-8")
    assert "</script><script>alert(1)</script>" not in html
    assert "alert(1)" in html


def test_report_on_main_with_no_unique_commits_still_writes_html(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "init")
    out = tmp_path / "report.html"

    rc = main(
        ["report", "HYP-0", "-C", str(repo), "--out", str(out), "--title", "Empty"]
    )

    assert rc == 0
    html = out.read_text(encoding="utf-8")
    assert "HYP-0" in html
    assert "Empty" in html
    assert str(out) in capsys.readouterr().out


def test_report_strips_origin_credentials_from_html(tmp_path):
    repo = _repo_with_feature(tmp_path)
    _git(
        repo,
        "remote",
        "add",
        "origin",
        "https://user:TOKEN@github.com/org/secret-repo.git",
    )
    out = tmp_path / "report.html"

    rc = main(["report", "HYP-1", "-C", str(repo), "--out", str(out)])

    assert rc == 0
    html = out.read_text(encoding="utf-8")
    assert "TOKEN" not in html
    assert "user:" not in html
    assert "github.com/org/secret-repo" in html


def test_report_rejects_an_unknown_explicit_base(tmp_path, capsys):
    repo = _repo_with_feature(tmp_path)
    out = tmp_path / "report.html"

    rc = main(
        ["report", "HYP-1", "-C", str(repo), "--base", "no-such-ref", "--out", str(out)]
    )

    assert rc == errors.EXIT_CONFIG
    assert not out.exists()
    printed = capsys.readouterr().out.lower()
    assert "no-such-ref" in printed or "unknown" in printed


def test_report_rename_shows_destination_path_not_brace_form(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "old.py").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "old.py")
    _git(repo, "commit", "-qm", "init")
    _git(repo, "checkout", "-qb", "feat/rename")
    _git(repo, "mv", "old.py", "new.py")
    _git(repo, "commit", "-qm", "refactor: rename old to new")
    out = tmp_path / "report.html"

    rc = main(["report", "HYP-1", "-C", str(repo), "--out", str(out)])

    assert rc == 0
    html = out.read_text(encoding="utf-8")
    assert "new.py" in html
    assert "{old.py => new.py}" not in html
    assert "old.py => new.py" not in html


def test_report_unborn_head_is_a_structured_error(tmp_path, capsys):
    repo = tmp_path / "empty"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    out = tmp_path / "report.html"

    rc = main(["report", "HYP-1", "-C", str(repo), "--out", str(out)])

    assert rc == errors.EXIT_CONFIG
    assert not out.exists()
    printed = capsys.readouterr().out.lower()
    assert "no commits" in printed or "head" in printed
