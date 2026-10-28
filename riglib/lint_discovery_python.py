"""Python facts of one project directory for the lint discovery.

Context: pyproject.toml/setup.cfg/tox.ini/requirements*.txt are data, parsed with the stdlib TOML parser
(or a table-header scan when none exists) and bounded text reads; `setup.py` is only noticed by name,
never run. Tool configuration is a file name (`ruff.toml`), a TOML table (`[tool.ruff]`) or an ini
section (`[flake8]`); declared tools come from every dependency list the common Python project
managers write. The package manager is chosen from lockfiles and `[tool.*]` tables, never guessed
from the environment.
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping

from .lint_discovery_io import Reader, load_toml
from .lint_discovery_model import Evidence, PackageManager, join
from .lint_discovery_tools import ToolCollector

PYTHON_TOOLS = frozenset({"ruff", "black", "flake8", "pylint", "mypy", "pyright"})
_CONFIG_NAMES: Mapping[str, str] = {
    "ruff.toml": "ruff", ".ruff.toml": "ruff", ".flake8": "flake8", ".pylintrc": "pylint", "pylintrc": "pylint",
    "mypy.ini": "mypy", ".mypy.ini": "mypy", "pyrightconfig.json": "pyright",
}
_PYPROJECT_TABLES: Mapping[str, str] = {
    "tool.ruff": "ruff", "tool.black": "black", "tool.pylint": "pylint", "tool.mypy": "mypy", "tool.pyright": "pyright",
}
_INI_SECTIONS: Mapping[str, Mapping[str, str]] = {"setup.cfg": {"flake8": "flake8", "mypy": "mypy"}, "tox.ini": {"flake8": "flake8"}}
_PY_LOCKS = (("uv.lock", "uv"), ("poetry.lock", "poetry"), ("pdm.lock", "pdm"))
_PY_TABLES = (("tool.uv", "uv"), ("tool.poetry", "poetry"), ("tool.pdm", "pdm"))
_REQUIREMENTS = re.compile(r"^requirements.*\.txt$")
_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_INI_SECTION = re.compile(r"^\[([^\]\s]+)\]", re.M)
_COMMENT = re.compile(r"(?:^|\s)#.*$")


def is_python_manifest(name: str) -> bool:
    return name in {"pyproject.toml", "setup.py", "setup.cfg"} or bool(_REQUIREMENTS.match(name))


def is_python_config_name(name: str) -> bool:
    return name in _CONFIG_NAMES


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _dict(value: object) -> Mapping[str, object]:
    return value if isinstance(value, dict) else {}


def _names_in(items: object) -> list[str]:
    if isinstance(items, dict):
        return [str(key) for key in items]
    if not isinstance(items, list):
        return []
    matches = (_REQUIREMENT_NAME.match(item) for item in items if isinstance(item, str))
    return [m.group(1) for m in matches if m]


def _nested(label: str, table: object) -> list[tuple[str, object]]:
    return [(f"{label}.{key}", value) for key, value in _dict(table).items()]


def _dependency_sources(document: Mapping[str, object]) -> list[tuple[str, object]]:
    project, tool = _dict(document.get("project")), _dict(document.get("tool"))
    poetry = _dict(tool.get("poetry"))
    sources: list[tuple[str, object]] = [
        ("project.dependencies", project.get("dependencies")),
        ("tool.poetry.dependencies", poetry.get("dependencies")),
        ("tool.poetry.dev-dependencies", poetry.get("dev-dependencies")),
        ("tool.uv.dev-dependencies", _dict(tool.get("uv")).get("dev-dependencies")),
    ]
    sources += _nested("project.optional-dependencies", project.get("optional-dependencies"))
    sources += _nested("dependency-groups", document.get("dependency-groups"))
    sources += _nested("tool.pdm.dev-dependencies", _dict(tool.get("pdm")).get("dev-dependencies"))
    sources += [(f"tool.poetry.group.{group}.dependencies", _dict(body).get("dependencies")) for group, body in _dict(poetry.get("group")).items()]
    return sources


def _declare(rel: str, detail: str, names: Iterable[str], collector: ToolCollector) -> None:
    for name in names:
        tool = _normalize(name)
        if tool in PYTHON_TOOLS:
            collector.declared(tool, rel, f"{detail}:{tool}")


def _pyproject(reader: Reader, rel: str, collector: ToolCollector) -> frozenset[str]:
    view = load_toml(reader, rel)
    for table, tool in _PYPROJECT_TABLES.items():
        if table in view.tables:
            collector.config(tool, rel, detail=table, kind="field")
    if view.document is not None:
        for detail, items in _dependency_sources(view.document):
            _declare(rel, detail, _names_in(items), collector)
    return view.tables


def _ini_sections(reader: Reader, rel: str, collector: ToolCollector, wanted: Mapping[str, str]) -> None:
    text = reader.text(rel)
    for section in _INI_SECTION.findall(text or ""):
        if section in wanted:
            collector.config(wanted[section], rel, detail=f"[{section}]", kind="field")


def _requirements(reader: Reader, rel: str, collector: ToolCollector) -> None:
    text = reader.text(rel)
    names: list[str] = []
    for raw in (text or "").splitlines():
        line = _COMMENT.sub("", raw).strip()
        match = None if not line or line.startswith("-") else _REQUIREMENT_NAME.match(line)
        if match:
            names.append(match.group(1))
    _declare(rel, "requirements", names, collector)


def collect_python(reader: Reader, directory: str, names: frozenset[str], collector: ToolCollector) -> frozenset[str]:
    """Collect every Python tool fact of ``directory``; returns the pyproject table names (for manager selection)."""
    tables: frozenset[str] = frozenset()
    for name in sorted(names):
        rel = join(directory, name)
        if name in _CONFIG_NAMES:
            collector.config(_CONFIG_NAMES[name], rel)
        elif name == "pyproject.toml":
            tables = _pyproject(reader, rel, collector)
        elif name in _INI_SECTIONS:
            _ini_sections(reader, rel, collector, _INI_SECTIONS[name])
        elif _REQUIREMENTS.match(name):
            _requirements(reader, rel, collector)
    return tables


def _own_manager(directory: str, names: frozenset[str], tables: frozenset[str]) -> tuple[PackageManager, tuple[Evidence, ...]] | None:
    locks = [(file, manager) for file, manager in _PY_LOCKS if file in names]
    if len(locks) > 1:
        return PackageManager("python", "", "ambiguous", None, directory, tuple(sorted(m for _, m in locks))), ()
    if locks:
        lock = join(directory, locks[0][0])
        return PackageManager("python", locks[0][1], "lockfile", lock, directory), (Evidence(lock, "lockfile"),)
    for table, manager in _PY_TABLES:
        if table in tables:
            return PackageManager("python", manager, "workspace marker", None, directory), (Evidence(join(directory, "pyproject.toml"), "field", f"[{table}]"),)
    return None


def _ancestor_manager(names_by_dir: Mapping[str, frozenset[str]], directory: str) -> tuple[PackageManager, tuple[Evidence, ...]] | None:
    """A child project without its own lock belongs to the nearest ancestor that owns one (uv/pdm workspaces)."""
    current = directory
    while current != ".":
        current = current.rsplit("/", 1)[0] if "/" in current else "."
        found = _own_manager(current, names_by_dir.get(current, frozenset()), frozenset())
        if found is not None and found[0].lockfile:
            return found
    return None


def select_python_manager(names_by_dir: Mapping[str, frozenset[str]], directory: str, tables: frozenset[str]) -> tuple[PackageManager, tuple[Evidence, ...]]:
    own = _own_manager(directory, names_by_dir.get(directory, frozenset()), tables)
    if own is not None:
        return own
    inherited = _ancestor_manager(names_by_dir, directory)
    if inherited is not None:
        return inherited
    return PackageManager("python", "pip", "default", None, directory), ()
