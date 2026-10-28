"""Classification of project scripts (``package.json`` scripts, ``Makefile`` targets) for the lint discovery.

Why: ``rig lint`` may run a project's own lint/typecheck script, but only when the NAME is on a strict
allow-list and the command is provably not self-referential or file-writing (decision D8). Scripts are
only ever *classified* here -- never executed, never passed through a shell, and a discovered name is
never interpolated into anything. Makefiles are read by a bounded regex, not by running make.
"""

from __future__ import annotations

import re
import shlex
from typing import Mapping, Sequence

from .lint_discovery_calls import Call, calls, is_writer_word, words
from .lint_discovery_model import ProjectScript, Role

# tool id -> executable/package tokens that mention it; exact token match so `standard-version` is not StandardJS
TOOL_TOKENS: Mapping[str, str] = {
    "eslint": "eslint", "biome": "biome", "prettier": "prettier", "oxlint": "oxlint", "oxfmt": "oxfmt",
    "dprint": "dprint", "tslint": "tslint", "xo": "xo", "standard": "standard", "tsc": "tsc",
    "ruff": "ruff", "black": "black", "flake8": "flake8", "pylint": "pylint", "mypy": "mypy", "pyright": "pyright",
}

_NPM_NAME = re.compile(r"^(lint|typecheck|type-check|check-types|tsc)(:[a-z0-9-]+)?$|^(format|fmt|prettier)[:-]check$|^check[:-](format|fmt|lint|types)$")
_MAKE_NAME = re.compile(r"^(lint|typecheck|type-check|check-types|tsc)(-[a-z0-9-]+)?$|^(format|fmt|prettier)-check$|^check-(format|fmt|lint|types)$")
_EXCLUDED_SEGMENTS = frozenset({"fix", "write", "watch", "staged", "changed", "dev", "build", "test"})
_SEGMENT_SPLIT = re.compile(r"[:\-]")
_RIG_LINT = re.compile(r"(?<![\w-])(?:rig|riglib)(?:\s+-{1,2}[\w=./-]+)*\s+lint\b")
_OPERATORS = frozenset({"&&", "||", ";", "|", "&"})
_MAKE_TARGET = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.\-]*)[ \t]*:(?![=:])(.*)$")
MAX_MAKEFILE_BYTES = 1024 * 1024


def tokenize(command: str) -> tuple[str, ...]:
    """Lowercased basenames of every word (flags included); shell operators dropped."""
    try:
        parts = shlex.split(command, posix=True)
    except ValueError:
        parts = command.split()
    out: list[str] = []
    for part in parts:
        token = part.strip().lower().replace("\\", "/").rsplit("/", 1)[-1]
        if token and token not in _OPERATORS:
            out.append(token)
    return tuple(out)


def covered_tools(tokens: tuple[str, ...]) -> tuple[str, ...]:
    present = set(tokens)
    return tuple(tool for tool, token in TOOL_TOKENS.items() if token in present)


def roles_for(name: str, *, makefile: bool) -> tuple[Role, ...]:
    """Roles of an allow-listed script name; empty when the name is not a lint/check script or is a writer/dev script."""
    if not (_MAKE_NAME if makefile else _NPM_NAME).match(name):
        return ()
    if _EXCLUDED_SEGMENTS & set(_SEGMENT_SPLIT.split(name)):
        return ()
    roles: list[Role] = []
    segments = set(_SEGMENT_SPLIT.split(name))
    if segments & {"typecheck", "type", "types", "tsc"}:
        roles.append("typecheck")
    if segments & {"format", "fmt", "prettier"}:
        roles.append("format")
    if "lint" in segments:
        roles.append("lint")
    return tuple(roles) or ("lint",)


def _writes(command: str) -> bool:
    return any(is_writer_word(word) for word in words(command))


def _unsafe_formatters(found: Sequence[Call]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(call.unsafe for call in found if call.unsafe))


def _covered_roles(found: Sequence[Call]) -> tuple[str, ...]:
    return tuple(sorted({f"{call.tool}:{role}" for call in found for role in call.roles}))


def classify(runner: str, name: str, command: str) -> ProjectScript | None:
    """A ``ProjectScript`` for an allow-listed name, else None."""
    roles = roles_for(name, makefile=runner == "Makefile")
    if not roles:
        return None
    tokens = tokenize(command)
    found = calls(command, TOOL_TOKENS)
    return ProjectScript(
        runner=runner,  # type: ignore[arg-type]
        name=name,
        command=command,
        tokens=tokens,
        invokes_rig_lint=bool(_RIG_LINT.search(command)),
        writes_files=_writes(command),
        roles=roles,
        covers_tools=covered_tools(tokens),
        covers_roles=_covered_roles(found),
        unsafe_formatters=_unsafe_formatters(found),
    )


def package_scripts(manifest: Mapping[str, object]) -> dict[str, str]:
    scripts = manifest.get("scripts")
    if not isinstance(scripts, dict):
        return {}
    return {str(k): v for k, v in scripts.items() if isinstance(v, str)}


def makefile_targets(text: str) -> dict[str, str]:
    """Target name -> joined recipe for every target whose name is on the allow-list (first definition wins)."""
    out: dict[str, str] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("\t") and current is not None:
            out[current] = (out[current] + "\n" + line.strip().lstrip("@-+ ")).strip()
            continue
        current = None
        match = _MAKE_TARGET.match(line)
        if match and roles_for(match.group(1), makefile=True) and match.group(1) not in out:
            current = match.group(1)
            out[current] = ""
    return out
