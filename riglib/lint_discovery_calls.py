"""Static analysis of the tool calls inside one project script command (never executed).

Why: ``rig lint`` may demote its own check of a tool only when a project script PROVABLY runs the same
tool in the same role over the whole scope (a script named ``lint`` that runs ``eslint src`` does not
cover Python, nor the rest of the tree), and it must never run a script that rewrites files while
pretending to be a check (``oxfmt .``, ``ruff format``, ``cargo fmt`` write by default). Both facts come
from tokenizing the command: shell operators split it into calls, each call names a tool, its
sub-command and its arguments. Anything not provable stays unclaimed, so the worst outcome of a
misparse is that a check runs twice, never that one is skipped or that a formatter runs unnoticed.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass
from typing import Mapping

from .lint_discovery_model import TOOL_ROLES, Role

_PUNCTUATION = frozenset("();<>|&")
_SUBCOMMAND_ROLES: Mapping[str, Mapping[str, tuple[Role, ...]]] = {
    "biome": {"check": ("lint", "format"), "ci": ("lint", "format"), "lint": ("lint",), "format": ("format",)},
    "ruff": {"check": ("lint",), "format": ("format",)},
}
# Formatters that rewrite files unless given a read-only flag, and the flags that make them read-only.
_READ_ONLY_FLAGS: Mapping[str, frozenset[str]] = {
    "oxfmt": frozenset({"--check", "--list-different", "-l"}),
    "black": frozenset({"--check", "--diff"}),
    "ruff format": frozenset({"--check", "--diff"}),
    "cargo fmt": frozenset({"--check"}),
    "prettier": frozenset({"--check", "-c", "--list-different", "-l"}),
}
_WRITES_BY_DEFAULT = frozenset({"oxfmt", "black", "ruff format", "cargo fmt", "dprint"})
# Flags whose value cannot narrow what a tool checks.
_HARMLESS_VALUE_FLAGS = frozenset({"--max-warnings", "-f", "--format", "--cache-location", "-o", "--output-file"})
_PROJECT_FLAGS = frozenset({"-p", "--project"})
_NEUTRAL_WORDS = frozenset({".", "./", "--", "check", "format", "lint", "fmt", "ci", "run", "exec"})
_SHORT_FLAG_CLUSTER = re.compile(r"^-[a-z]{2,}$")


@dataclass(frozen=True)
class Call:
    tool: str                 # tool id (TOOL_ROLES vocabulary) or "cargo" for ``cargo fmt``
    roles: tuple[Role, ...]   # roles this call provably runs in full over the scope
    unsafe: str               # formatter label when the call rewrites files without a read-only form, else ""


def words(command: str) -> tuple[str, ...]:
    """Shell-like words of ``command``; operators are separate words; newlines act as ``;``."""
    lexer = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return tuple(lexer)
    except ValueError:
        return tuple(command.split())


def segments(command: str) -> tuple[tuple[str, ...], ...]:
    """Words of each simple command (split at ``&& || ; | &``)."""
    out: list[tuple[str, ...]] = []
    current: list[str] = []
    for word in words(command):
        if word and set(word) <= _PUNCTUATION:
            if current:
                out.append(tuple(current))
            current = []
        else:
            current.append(word)
    if current:
        out.append(tuple(current))
    return tuple(out)


def basename(word: str) -> str:
    return word.strip().lower().replace("\\", "/").rsplit("/", 1)[-1]


def is_writer_word(word: str) -> bool:
    """``--fix``/``--write``/``-w``/``--watch`` (also ``--fix=...`` and short clusters such as ``-lw``)."""
    lowered = word.lower()
    if lowered in {"-w", "--write", "--fix", "--watch"} or lowered.startswith(("--fix", "--write")):
        return True
    return bool(_SHORT_FLAG_CLUSTER.match(lowered)) and "w" in lowered


def _subcommand(args: tuple[str, ...]) -> str:
    return next((a.lower() for a in args if not a.startswith("-")), "")


def _is_default_project(arg: str) -> bool:
    return posixpath.normpath(arg) == "tsconfig.json"


def _restricted(tool: str, args: tuple[str, ...]) -> bool:
    """Whether the call narrows what is checked (path, extension, config or project other than the default)."""
    skip_next = False
    expect_project = False
    for arg in args:
        if skip_next:
            skip_next = False
        elif expect_project:
            expect_project = False
            if not _is_default_project(arg):
                return True
        elif arg.startswith("-"):
            skip_next = arg in _HARMLESS_VALUE_FLAGS
            expect_project = tool == "tsc" and arg in _PROJECT_FLAGS
        elif arg.lower() not in _NEUTRAL_WORDS:
            return True
    return False


def _label(tool: str, args: tuple[str, ...]) -> str:
    """Name used for the read-only table: ``ruff format``, ``cargo fmt``, otherwise the tool."""
    sub = _subcommand(args)
    if tool == "ruff" and sub == "format":
        return "ruff format"
    if tool == "cargo" and sub == "fmt":
        return "cargo fmt"
    return tool


def _read_only(label: str, args: tuple[str, ...]) -> bool:
    if label == "dprint":
        return _subcommand(args) == "check"
    flags = _READ_ONLY_FLAGS.get(label, frozenset())
    return any(arg.lower() in flags for arg in args)


def _roles(tool: str, args: tuple[str, ...]) -> tuple[Role, ...]:
    table = _SUBCOMMAND_ROLES.get(tool)
    if table is not None:
        return table.get(_subcommand(args), ())
    return TOOL_ROLES.get(tool, ())


def analyse_call(tool: str, args: tuple[str, ...]) -> Call:
    """What one call of ``tool`` with ``args`` provably does."""
    label = _label(tool, args)
    read_only = _read_only(label, args)
    unsafe = label if label in _WRITES_BY_DEFAULT and not read_only else ""
    verifying = read_only or label not in _READ_ONLY_FLAGS
    roles = _roles(tool, args)
    if unsafe or not verifying or _restricted(tool, args):
        roles = ()
    return Call(tool, roles, unsafe)


def calls(command: str, tool_tokens: Mapping[str, str]) -> tuple[Call, ...]:
    """Every call of a known tool (or ``cargo``) in ``command``."""
    by_token = {token: tool for tool, token in tool_tokens.items()}
    found: list[Call] = []
    for segment in segments(command):
        for index, word in enumerate(segment):
            tool = by_token.get(basename(word)) or ("cargo" if basename(word) == "cargo" else None)
            if tool is not None:
                found.append(analyse_call(tool, tuple(segment[index + 1:])))
    return tuple(found)
