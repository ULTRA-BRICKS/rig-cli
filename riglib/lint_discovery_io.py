"""Bounded, no-follow file reads and stdlib-only manifest parsing for the lint discovery.

Trust boundary: everything read here comes from a cloned repository, i.e. is attacker-controlled.
Nothing is imported or executed: JSON/JSONC/TOML go through the stdlib parsers, YAML/JS/TS are never
parsed at all (callers match them by file NAME only), and the few text facts (a Makefile target, one
`typeCheck: true`) come from bounded regexes over size-capped reads. Every failure becomes a
``DiscoveryProblem``; nothing here raises for bad repository content.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .lint_discovery_model import DiscoveryProblem

MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_TEXT_BYTES = 1024 * 1024

# The TOML parser is imported lazily (stdlib ``tomllib`` on 3.11+, the ``tomli`` backport on 3.10): nothing
# third-party may load at import time, and a missing parser must be a reported problem, never silence.
NO_TOML_PARSER = (
    "no TOML parser is available (Python 3.10 needs the `tomli` package); run `pip install tomli` "
    "or reinstall rig, then rerun `rig lint`; tool facts were read from table headers only"
)


def _import_toml_parser():
    """The TOML parser module, or ``None`` when this Python has none."""
    try:
        import tomllib
    except ModuleNotFoundError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ModuleNotFoundError:
            return None
    return tomllib


def toml_available() -> bool:
    return _import_toml_parser() is not None


_STRING_OR_COMMENT = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/', re.S)
_STRING_OR_TRAILING_COMMA = re.compile(r'("(?:\\.|[^"\\])*")|,(\s*[}\]])')
_TABLE_HEADER = re.compile(r"^\s*\[\[?\s*([A-Za-z0-9_.\"'\- ]+?)\s*\]\]?\s*(?:#.*)?$", re.M)


def parse_jsonc(text: str) -> object:
    """Parse JSON with ``//`` and ``/* */`` comments and trailing commas; ``ValueError`` on failure."""
    cleaned = text.lstrip("\ufeff")
    cleaned = _STRING_OR_COMMENT.sub(lambda m: m.group(0) if m.group(0).startswith('"') else " ", cleaned)
    cleaned = _STRING_OR_TRAILING_COMMA.sub(lambda m: m.group(1) or m.group(2), cleaned)
    try:
        return json.loads(cleaned)
    except RecursionError as exc:
        raise ValueError("nesting too deep") from exc


def toml_tables(text: str) -> frozenset[str]:
    """Table header names (``tool.ruff``) found by a line scan: the no-parser fallback."""
    return frozenset(m.group(1).replace('"', "").replace("'", "").replace(" ", "") for m in _TABLE_HEADER.finditer(text))


def read_bounded(path: str, limit: int) -> str:
    """Read a regular, non-symlink file as UTF-8 (BOM stripped); ``OSError``/``ValueError`` otherwise."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        data = os.read(fd, limit + 1)
        while len(data) <= limit:
            chunk = os.read(fd, limit + 1 - len(data))
            if not chunk:
                break
            data += chunk
    finally:
        os.close(fd)
    if len(data) > limit:
        raise ValueError(f"larger than {limit} bytes")
    return data.decode("utf-8").lstrip("\ufeff")


class Reader:
    """Reads repo files by relative path, caches parses, and records problems instead of raising."""

    def __init__(self, root: Path, sink: Callable[[DiscoveryProblem], None]) -> None:
        self._root = root
        self._sink = sink
        self._text: dict[str, str | None] = {}
        self._parsed: dict[tuple[str, str], object | None] = {}

    def text(self, rel: str, *, limit: int = MAX_MANIFEST_BYTES, report: bool = True) -> str | None:
        if rel in self._text:
            return self._text[rel]
        try:
            value: str | None = read_bounded(str(self._root / rel), limit)
        except (OSError, ValueError) as exc:
            value = None
            if report:
                self._sink(DiscoveryProblem(rel, "unreadable", str(exc)))
        self._text[rel] = value
        return value

    def _parse(self, rel: str, fmt: str, kind: str, parse: Callable[[str], object]) -> object | None:
        key = (rel, fmt)
        if key in self._parsed:
            return self._parsed[key]
        text = self.text(rel)
        value: object | None = None
        if text is not None:
            try:
                value = parse(text)
            except (ValueError, TypeError, RecursionError) as exc:
                self._sink(DiscoveryProblem(rel, kind, f"cannot parse {fmt}: {exc}"))  # type: ignore[arg-type]
        self._parsed[key] = value
        return value

    def json(self, rel: str, *, kind: str = "malformed-manifest") -> dict[str, object] | None:
        """Strict JSON object; a non-object document is reported like a syntax error."""
        value = self._parse(rel, "json", kind, _strict_object)
        return value if isinstance(value, dict) else None

    def jsonc(self, rel: str, *, kind: str = "malformed-config") -> object | None:
        return self._parse(rel, "jsonc", kind, parse_jsonc)

    def toml(self, rel: str, *, kind: str = "malformed-manifest") -> dict[str, object] | None:
        parser = _import_toml_parser()
        if parser is None:
            return None
        value = self._parse(rel, "toml", kind, parser.loads)
        return value if isinstance(value, dict) else None

    def report(self, problem: DiscoveryProblem) -> None:
        self._sink(problem)


def _strict_object(text: str) -> object:
    try:
        value = json.loads(text)
    except RecursionError as exc:
        raise ValueError("nesting too deep") from exc
    if not isinstance(value, dict):
        raise ValueError("top level must be an object")
    return value


@dataclass(frozen=True)
class TomlView:
    """A TOML file as discovery needs it: the parsed document (None without a parser or when malformed) and its table names."""

    document: dict[str, object] | None
    tables: frozenset[str]             # top-level keys plus two-segment names such as ``tool.ruff``


def _two_segments(name: str) -> str:
    return ".".join(name.split(".")[:2])


def _document_tables(document: dict[str, object]) -> frozenset[str]:
    names = set(document)
    tool = document.get("tool")
    if isinstance(tool, dict):
        names.update(f"tool.{key}" for key in tool)
    return frozenset(names)


def load_toml(reader: Reader, rel: str) -> TomlView:
    """Parse with the available TOML parser, else scan headers AND report the missing parser (never silent)."""
    if toml_available():
        document = reader.toml(rel)
        return TomlView(document, _document_tables(document) if document is not None else frozenset())
    text = reader.text(rel)
    if text is not None:
        # The header scan keeps tool facts visible, but the run must not read as complete without a real parser.
        reader.report(DiscoveryProblem(rel, "unreadable", NO_TOML_PARSER))
    headers = toml_tables(text) if text is not None else frozenset()
    return TomlView(None, frozenset(_two_segments(h) for h in headers) | frozenset(h.split(".")[0] for h in headers))
