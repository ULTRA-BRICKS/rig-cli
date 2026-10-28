"""Accumulator that turns scattered facts (config file, dependency, script mention) into ``ConfiguredTool`` rows.

Why: a tool is "configured" through any of several independent signals (decision D3), and a tool with
two roles (Biome, Ruff) must appear once per role. Collecting per ``(tool, role)`` keeps every consumer
a plain lookup and the evidence trail complete.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Literal, Mapping

from .lint_discovery_model import TOOL_ROLES, ConfiguredTool, Evidence, Role

# Tools discovery reports that the shared TOOL_ROLES vocabulary does not list. `oxlint-tsgolint` is the
# type-aware backend of Oxlint and `@oxlint/plugins` (needed by JS plugins such as anti-slop): providers and
# `linter_environment` only need to see that they are declared.
EXTRA_TOOL_ROLES: Mapping[str, tuple[Role, ...]] = {"oxlint-tsgolint": ("lint",), "oxlint-plugins": ("lint",)}


def roles_of(tool: str) -> tuple[Role, ...]:
    return TOOL_ROLES.get(tool) or EXTRA_TOOL_ROLES[tool]


@dataclass
class _Row:
    declared: bool = False
    config_files: list[str] = field(default_factory=list)
    scripts: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    options: dict[str, str] = field(default_factory=dict)


class ToolCollector:
    def __init__(self) -> None:
        self._rows: dict[tuple[str, Role], _Row] = {}

    def _each(self, tool: str) -> Iterator[_Row]:
        for role in roles_of(tool):
            yield self._rows.setdefault((tool, role), _Row())

    def config(self, tool: str, rel: str, *, detail: str = "", kind: Literal["config", "field"] = "config") -> None:
        """A config file, or a config field/table inside a manifest (``kind="field"``): both make ``rel`` a config file."""
        for row in self._each(tool):
            if rel not in row.config_files:
                row.config_files.append(rel)
            row.evidence.append(Evidence(rel, kind, detail))

    def declared(self, tool: str, manifest: str, detail: str) -> None:
        for row in self._each(tool):
            row.declared = True
            row.evidence.append(Evidence(manifest, "manifest", detail))

    def script(self, tool: str, name: str, manifest: str) -> None:
        for row in self._each(tool):
            if name not in row.scripts:
                row.scripts.append(name)
            row.evidence.append(Evidence(manifest, "script", name))

    def option(self, tool: str, key: str, value: str) -> None:
        for row in self._each(tool):
            row.options[key] = value

    def merge(self, other: "ToolCollector") -> None:
        for key, src in other._rows.items():
            dst = self._rows.setdefault(key, _Row())
            dst.declared = dst.declared or src.declared
            dst.config_files += [f for f in src.config_files if f not in dst.config_files]
            dst.scripts += [s for s in src.scripts if s not in dst.scripts]
            dst.evidence += src.evidence
            dst.options.update(src.options)

    def build(self) -> tuple[ConfiguredTool, ...]:
        return tuple(
            ConfiguredTool(
                tool=tool, role=role, declared=row.declared,
                config_files=tuple(sorted(row.config_files)), scripts=tuple(sorted(row.scripts)),
                evidence=tuple(row.evidence), options=dict(row.options),
            )
            for (tool, role), row in sorted(self._rows.items())
        )
