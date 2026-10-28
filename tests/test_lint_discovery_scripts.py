"""Script classification: which tool+role a script provably runs, and whether it can rewrite files.

Pure string analysis: scripts are classified here, never executed.
"""
from __future__ import annotations

import pytest

from riglib.lint_discovery_scripts import classify, makefile_targets


def _facts(command: str, name: str = "lint", runner: str = "package.json"):
    script = classify(runner, name, command)
    assert script is not None
    return script


@pytest.mark.parametrize(
    ("command", "covered"),
    [
        ("eslint .", {"eslint:lint"}),
        ("pnpm exec eslint . --max-warnings 0", {"eslint:lint"}),
        ("tsc --noEmit", {"tsc:typecheck"}),
        ("tsc --noEmit -p tsconfig.json", {"tsc:typecheck"}),
        ("eslint . && tsc --noEmit", {"eslint:lint", "tsc:typecheck"}),
        ("eslint .&&tsc --noEmit", {"eslint:lint", "tsc:typecheck"}),
        ("biome check .", {"biome:lint", "biome:format"}),
        ("biome lint .", {"biome:lint"}),
        ("biome format .", {"biome:format"}),
        ("ruff check", {"ruff:lint"}),
        ("ruff format --check", {"ruff:format"}),
        ("ruff check . && ruff format --check .", {"ruff:lint", "ruff:format"}),
        ("prettier --check .", {"prettier:format"}),
        ("prettier -l .", {"prettier:format"}),
        ("oxfmt --check", {"oxfmt:format"}),
        ("black --check .", {"black:format"}),
        ("oxlint", {"oxlint:lint"}),
        ("mypy .", {"mypy:typecheck"}),
    ],
)
def test_unrestricted_read_only_calls_cover_their_tool_and_role(command, covered):
    script = _facts(command)
    assert set(script.covers_roles) == covered
    assert script.unsafe_formatters == ()


@pytest.mark.parametrize(
    "command",
    [
        "eslint src",
        "eslint --ext .ts .",
        "eslint -c other.config.js .",
        "tsc --noEmit -p tsconfig.app.json",
        "ruff check scripts",
        "biome lint packages/web",
        "oxlint src/",
        "prettier --check src",
    ],
)
def test_path_or_config_restricted_calls_cover_nothing(command):
    script = _facts(command)
    assert script.covers_roles == ()
    assert script.covers_tools  # still named, so D3 sees the script as invoking the tool


def test_prettier_without_a_read_only_form_does_not_verify_anything():
    script = _facts("prettier .", name="format:check")
    assert script.covers_roles == () and script.unsafe_formatters == ()


@pytest.mark.parametrize(
    ("command", "unsafe"),
    [
        ("oxfmt .", ("oxfmt",)),
        ("oxfmt", ("oxfmt",)),
        ("ruff format .", ("ruff format",)),
        ("black .", ("black",)),
        ("cargo fmt", ("cargo fmt",)),
        ("cargo fmt --all", ("cargo fmt",)),
        ("dprint fmt", ("dprint",)),
        ("uvx ruff format src && eslint .", ("ruff format",)),
        ("npx oxfmt src; tsc --noEmit", ("oxfmt",)),
    ],
)
def test_formatter_without_read_only_form_is_unsafe(command, unsafe):
    script = _facts(command, name="format:check")
    assert script.unsafe_formatters == unsafe
    assert not any(pair.endswith(":format") and pair.split(":")[0] in {"oxfmt", "black"} for pair in script.covers_roles)


@pytest.mark.parametrize(
    "command",
    [
        "oxfmt --check .",
        "oxfmt -l",
        "oxfmt --list-different",
        "ruff format --diff",
        "black --check . ",
        "black --diff .",
        "cargo fmt --all -- --check",
        "cargo fmt --check",
        "dprint check",
        "prettier --check .",
        "gofmt -l .",
        "shfmt -d .",
    ],
)
def test_read_only_forms_are_not_unsafe(command):
    assert _facts(command, name="format:check").unsafe_formatters == ()


@pytest.mark.parametrize(
    "command",
    ["eslint --fix .", "eslint --fix=true .", "prettier --write .", "prettier -w .", "gofmt -w .", "gofmt -lw .",
     "eslint . --watch", "biome check --write ."],
)
def test_explicit_writers_are_flagged(command):
    assert _facts(command).writes_files


def test_read_only_commands_are_not_writers():
    assert not _facts("eslint . && tsc --noEmit").writes_files
    assert not _facts("gofmt -l .", name="format:check").writes_files


def test_rig_lint_self_reference_still_detected():
    assert _facts("rig lint --json").invokes_rig_lint
    assert not _facts("eslint .").invokes_rig_lint


def test_script_without_known_tool_covers_nothing_and_is_safe():
    script = _facts("./scripts/lint.sh")
    assert (script.covers_tools, script.covers_roles, script.unsafe_formatters) == ((), (), ())


def test_makefile_recipe_commands_are_analysed_too():
    targets = makefile_targets("lint:\n\truff format .\n\teslint .\n")
    script = _facts(targets["lint"], name="lint", runner="Makefile")
    assert script.unsafe_formatters == ("ruff format",)
    assert "eslint:lint" in script.covers_roles


def test_unbalanced_quotes_fall_back_without_raising():
    script = _facts("eslint '.")
    assert script.covers_tools == ("eslint",)
