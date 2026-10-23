"""The files ``rig profile`` carries besides ``config.yaml`` — and the rules for how they travel.

``rig profile`` (:mod:`riglib.profile`) moves a declared ALLOW-LIST of files between machines
through one private git repo. This module is the pure half: what is on the list, what never
travels, how a file is turned into its portable form (``prepare``) and back into a local file
(``adopt``), and the secret scan that gates every push. It touches no disk and no network.

The allow-list (:data:`FILES`) — each entry has a ``kind``:

=============  ====================================  =============================================
id             local file                            portable form
=============  ====================================  =============================================
``config``     ``~/.config/rig/config.yaml``         ``config.yaml`` with ``env.vars`` withheld
``tmux``       ``~/.tmux.conf``                      ``files/tmux.conf``: the USER region only
``omp_config`` ``<omp agent dir>/config.yml``        ``files/omp/config.yml``
``omp_mcp``    ``<omp agent dir>/mcp.json``          ``files/omp/mcp.json`` with secrets MASKED
``sysmon``     ``~/.config/rig/sysmon/sysmon.json``  ``files/sysmon.json`` (rig re-generates it from
                                                     ``sysmon:``; it travels so a machine that has
                                                     not applied yet already has the same runtime)
=============  ====================================  =============================================

Everything else is ``secret-bearing`` and NEVER synced (:data:`NEVER_SYNCED`): ``env.vars`` values,
the omp auth broker token, the credential database, the remote-host and ssh-control state. The
registry is also what the tests walk to prove none of it ever enters the profile checkout.

Machine-specific values
-----------------------
One profile has to serve several machines. Three mechanisms, all reversible:

- **HOME paths** — every ``<home>/…`` in a file becomes ``{{rig:home}}/…`` in the repo and is
  expanded with the local home on the way back (``/home/me/x`` on Ubuntu, ``/Users/me/x`` on a Mac).
- **Machine values** — a registered key (``agent_tools_source``, ``tools.items.*.repo`` …) and any
  other absolute path OUTSIDE home differ per machine on purpose. They are ignored when two sides
  are compared, the local value always wins when a file is adopted, and the profile repo keeps each
  machine's own copy in ``machine/<host>.yaml`` (the **machine overlay**) so a reinstalled machine
  gets its values back. A registered optional key nobody on this machine ever set is dropped
  rather than inherited (``drop_when_unknown``).
- **Managed regions** — rig re-generates part of ``~/.tmux.conf`` (the sentinel block or the
  ``source-file …/rig.tmux.conf`` line); that part never leaves the machine.

Secrets
-------
``env.vars`` is removed before a config is pushed and merged back (kept) on a pull. ``mcp.json`` has
every secret-looking value replaced by :data:`MASK`; a pull restores the local value for each masked
leaf and reports the ones this machine still has to fill in. Last, :func:`scan_secrets` runs over
every byte about to be pushed and the push is REFUSED on a hit — a finding names the file, the line
and the rule, never the matched text.

Stdlib-only at import time; ``yaml`` is imported lazily.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .paths import expand_user_path

KIND_PORTABLE = "portable"
KIND_SECRET = "secret-bearing"

HOME_TOKEN = "{{rig:home}}"
MASK = "<rig:masked>"
MACHINE_DIR = "machine"


class PayloadError(ValueError):
    """A file that cannot cross the boundary. ``fix`` is the concrete next step."""

    def __init__(self, what: str, fix: str | None = None) -> None:
        super().__init__(what)
        self.what = what
        self.fix = fix


# ── the registry ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class MachineKey:
    """A config key whose value is per-machine. ``*`` in ``path`` matches any one key."""

    path: tuple[str, ...]
    drop_when_unknown: bool


@dataclass(frozen=True)
class FileSpec:
    id: str
    label: str
    repo_path: str
    fmt: str  # rigconfig | yaml | json | mcp | tmux
    local: Callable[[], Path]
    machine_keys: tuple[MachineKey, ...] = ()
    # subtrees that are the same on every machine even when they hold an absolute path (a local
    # clone URL in ``profile.repo``)
    portable_keys: tuple[tuple[str, ...], ...] = ()
    kind: str = KIND_PORTABLE


@dataclass(frozen=True)
class NeverSynced:
    name: str
    where: str
    why: str


def _config_path() -> Path:
    from .config import global_config_path

    return global_config_path()


def _tmux_path() -> Path:
    return Path.home() / ".tmux.conf"


def _omp_path(name: str) -> Callable[[], Path]:
    def resolve() -> Path:
        from .harness_skills import omp_user_path

        return expand_user_path(omp_user_path(name))

    return resolve


def _sysmon_path() -> Path:
    from .sysmon import CONFIG_NAME, DEFAULT_CONFIG_DIR

    return expand_user_path(DEFAULT_CONFIG_DIR) / CONFIG_NAME


FILES: tuple[FileSpec, ...] = (
    FileSpec(
        "config", "rig global config", "config.yaml", "rigconfig", _config_path,
        machine_keys=(
            MachineKey(("agent_tools_source",), True),
            MachineKey(("harness", "settings_paths"), True),
            MachineKey(("harness_wrappers", "real_bin"), True),
            MachineKey(("sysmon", "python_bin"), True),
            MachineKey(("sysmon", "reclaim", "worktree_roots"), True),
            # a tool checkout lives wherever that machine keeps it, but the entry itself is required
            MachineKey(("tools", "items", "*", "repo"), False),
            # which tools a machine runs is its own call (a profile default stays "on")
            MachineKey(("tools", "items", "*", "enabled"), True),
        ),
        portable_keys=(("profile",),),
    ),
    FileSpec("tmux", "tmux config (user region)", "files/tmux.conf", "tmux", _tmux_path),
    FileSpec("omp_config", "omp config", "files/omp/config.yml", "yaml", _omp_path("config.yml")),
    FileSpec("omp_mcp", "omp MCP servers (secrets masked)", "files/omp/mcp.json", "mcp", _omp_path("mcp.json")),
    FileSpec("sysmon", "sysmon runtime config", "files/sysmon.json", "json", _sysmon_path),
)

NEVER_SYNCED: tuple[NeverSynced, ...] = (
    NeverSynced("env.vars", "config.yaml (withheld on push, kept locally on pull)",
                "API keys and tokens in plaintext"),
    NeverSynced("rig.env.sh", "~/.config/rig/env/rig.env.sh", "generated from env.vars"),
    NeverSynced("auth-broker.token", "~/.omp/auth-broker.token", "omp authentication token"),
    NeverSynced("agent.db", "<omp agent dir>/agent.db", "stored provider credentials and sessions"),
    NeverSynced(".env", "<omp agent dir>/.env", "provider API keys"),
    NeverSynced("remote-host", "~/.omp/remote-host", "per-machine remote endpoint"),
    NeverSynced("ssh-control", "~/.omp/ssh-control", "live ssh control sockets"),
)

_BY_ID = {spec.id: spec for spec in FILES}


def spec_by_id(file_id: str) -> FileSpec:
    return _BY_ID[file_id]


def file_ids() -> tuple[str, ...]:
    return tuple(spec.id for spec in FILES)


# ── machine name ─────────────────────────────────────────────────────────────────────────────
def machine_name() -> str:
    """This machine's name in the overlay (``RIG_PROFILE_MACHINE``, else the hostname)."""
    raw = os.environ.get("RIG_PROFILE_MACHINE") or socket.gethostname() or ""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", raw.strip()).strip(".-")
    return cleaned or "unknown-host"


def record_repo_path(machine: str) -> str:
    return f"{MACHINE_DIR}/{machine}.yaml"


# ── digests ──────────────────────────────────────────────────────────────────────────────────
def digest(data: bytes | None) -> str | None:
    """sha256 over LF-normalized bytes (a CRLF checkout must not read as a change); None = absent."""
    if data is None:
        return None
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


# ── home tokens ──────────────────────────────────────────────────────────────────────────────
def _home() -> str:
    return str(Path.home())


def _tokenise_text(text: str, home: str) -> str:
    if len(home) <= 1:
        return text
    return re.sub(r"(?<![\w.~-])" + re.escape(home) + r"(?=/)", HOME_TOKEN, text)


def _detokenise_text(text: str, home: str) -> str:
    return text.replace(HOME_TOKEN, home)


def _map_strings(node: Any, fn: Callable[[str], str]) -> Any:
    if isinstance(node, str):
        return fn(node)
    if isinstance(node, dict):
        return {k: _map_strings(v, fn) for k, v in node.items()}
    if isinstance(node, list):
        return [_map_strings(v, fn) for v in node]
    return node


# ── structured parse / dump ──────────────────────────────────────────────────────────────────
def _parse(spec: FileSpec, data: bytes, what: str) -> dict[str, Any]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PayloadError(f"{what} is not UTF-8 text", fix="fix the file, then retry") from exc
    if spec.fmt in ("json", "mcp"):
        try:
            doc = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError as exc:
            raise PayloadError(f"{what} is not valid JSON: {exc}", fix="fix the file, then retry") from exc
    else:
        import yaml

        try:
            doc = yaml.safe_load(text) or {}
        except yaml.YAMLError as exc:
            raise PayloadError(f"{what} is not valid YAML: {exc}", fix="fix the file, then retry") from exc
    if not isinstance(doc, dict):
        raise PayloadError(f"{what} is not a mapping", fix="fix the file, then retry")
    return doc


def _dump(spec: FileSpec, doc: dict[str, Any]) -> bytes:
    if spec.fmt == "json":
        return (json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    if spec.fmt == "mcp":
        return (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    import yaml

    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, default_flow_style=False).encode("utf-8")


def _lf(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


# ── machine values ───────────────────────────────────────────────────────────────────────────
Path_ = tuple[str, ...]


def _matches(path: Path_, key: MachineKey) -> bool:
    return len(path) == len(key.path) and all(k in ("*", p) for k, p in zip(key.path, path))


def _is_foreign_path(value: Any) -> bool:
    """An absolute path that is NOT under home (home paths are tokenised before this runs)."""
    if isinstance(value, str):
        return value.startswith("/") and len(value) > 1 and not any(c.isspace() for c in value)
    if isinstance(value, list):
        return any(isinstance(v, str) and _is_foreign_path(v) for v in value)
    return False


def machine_paths(doc: dict[str, Any], spec: FileSpec) -> list[tuple[Path_, MachineKey | None]]:
    """Every key path in ``doc`` holding a machine value, with its registered key (None = foreign path)."""
    found: list[tuple[Path_, MachineKey | None]] = []

    def walk(node: Any, path: Path_) -> None:
        if path in spec.portable_keys:
            return
        for key in spec.machine_keys:
            if _matches(path, key):
                found.append((path, key))
                return
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, path + (str(k),))
        elif path and _is_foreign_path(node):
            found.append((path, None))

    walk(doc, ())
    return found


def _get(doc: Any, path: Path_) -> Any:
    for key in path:
        doc = doc[key]
    return doc


def _set(doc: dict[str, Any], path: Path_, value: Any) -> None:
    for key in path[:-1]:
        nxt = doc.get(key)
        if not isinstance(nxt, dict):
            nxt = doc[key] = {}
        doc = nxt
    doc[path[-1]] = value


def _del(doc: dict[str, Any], path: Path_) -> None:
    try:
        parent = _get(doc, path[:-1]) if len(path) > 1 else doc
        del parent[path[-1]]
    except (KeyError, TypeError):
        pass


def _stripped(doc: dict[str, Any], paths: list[tuple[Path_, MachineKey | None]]) -> dict[str, Any]:
    out = copy.deepcopy(doc)
    for path, _key in paths:
        _del(out, path)
    return out


def view_digest(spec: FileSpec, data: bytes | None) -> str | None:
    """The digest two sides are compared by: the bytes themselves, with machine values left out.

    A file without machine values hashes as the raw (LF-normalized) bytes; one with them hashes as a
    canonical dump of the document minus those keys, so a machine that merely has a different
    ``agent_tools_source`` never reads as changed.
    """
    if data is None:
        return None
    if spec.fmt == "tmux":
        return digest(data)
    doc = _parse(spec, data, spec.label)
    paths = machine_paths(doc, spec)
    if not paths:
        return digest(data)
    return digest(_dump(spec, _stripped(doc, paths)))


# ── secret scan ──────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    rule: str


_SECRET_KEY = re.compile(
    r"(?i)(api[_-]?key|secret|token|passw(?:or)?d|credential|authorization|bearer|private[_-]?key|"
    r"session[_-]?id|cookie)"
)
_PREFIXED_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b")),
    ("api-key", re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35})\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("bearer-token", re.compile(r"(?i)\bbearer\s+(?!<rig:)[A-Za-z0-9._~+/=-]{16,}")),
    ("url-credentials", re.compile(r"://[^/\s:@]+:(?!<rig:)[^/\s@]+@")),
)
_ASSIGNMENT = re.compile(r"""["']?([A-Za-z0-9_.-]+)["']?\s*[:=]\s*["']?([^"'\s,}#]+)""")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")


def _looks_like_secret_value(value: str) -> bool:
    if len(value) < 16 or value.startswith(("<rig:", "${", "{{", "~", "/", "http://", "https://", "$")):
        return False
    if _ENV_NAME.match(value):
        return False
    return any(c.isdigit() for c in value) and any(c.isalpha() for c in value)


def scan_secrets(name: str, text: str) -> list[Finding]:
    """Secret-looking content of ``text``. A finding carries the rule, never the matched text."""
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        hit = next((rule for rule, pattern in _PREFIXED_RULES if pattern.search(line)), None)
        if hit is None:
            for key, value in _ASSIGNMENT.findall(line):
                if _SECRET_KEY.search(key) and _looks_like_secret_value(value):
                    hit = "secret-assignment"
                    break
        if hit is not None:
            findings.append(Finding(name, number, hit))
    return findings


# ── mcp.json masking ─────────────────────────────────────────────────────────────────────────
_ENV_CONTAINERS = frozenset({"env", "headers", "environment", "envvars", "env_vars"})
_ENV_REFERENCE = re.compile(r"^\$\{[^}]+\}$|^\$[A-Z_][A-Z0-9_]*$")
_URL_SECRET = re.compile(r"(?i)([?&](?:token|key|api[_-]?key|access[_-]?token|secret|password)=)[^&#\s]+")
_URL_USERINFO = re.compile(r"(://[^/\s:@]+:)[^/\s@]+@")
_SECRET_FLAG = re.compile(r"(?i)^--?(?:[\w-]*(?:token|api[_-]?key|password|secret|auth)[\w-]*)$")
_SECRET_FLAG_VALUE = re.compile(r"(?i)^(--?[\w-]*(?:token|api[_-]?key|password|secret|auth)[\w-]*=)(.+)$")


def _mask_leaf(value: str) -> str:
    return value if value == MASK or _ENV_REFERENCE.match(value) else MASK


def _mask_url_secrets(value: str) -> str:
    return _URL_SECRET.sub(lambda m: m.group(1) + MASK, _URL_USERINFO.sub(lambda m: m.group(1) + MASK + "@", value))


def _mask_args(items: list[Any]) -> list[Any]:
    """Mask the value of a secret-looking flag, written ``--token=x`` or as ``--token x``."""
    out: list[Any] = []
    previous_flag = False
    for item in items:
        if not isinstance(item, str):
            out.append(_mask_json(item))
            previous_flag = False
            continue
        item = _mask_url_secrets(item)
        match = _SECRET_FLAG_VALUE.match(item)
        if previous_flag and not item.startswith("-"):
            item = _mask_leaf(item)
        elif match:
            item = match.group(1) + _mask_leaf(match.group(2))
        previous_flag = bool(_SECRET_FLAG.match(item))
        out.append(item)
    return out


def _mask_json(node: Any, *, container: bool = False, key: str = "") -> Any:
    if isinstance(node, dict):
        return {
            k: _mask_json(v, container=str(k).lower() in _ENV_CONTAINERS, key=str(k)) for k, v in node.items()
        }
    if isinstance(node, list):
        return _mask_args(node)
    if isinstance(node, str):
        node = _mask_url_secrets(node)
        if _SECRET_KEY.search(key) or (container and _looks_like_secret_value(node)):
            return _mask_leaf(node)
    return node


def _unmask(remote: Any, local: Any, missing: list[str], path: str = "") -> Any:
    """``remote`` with every masked leaf replaced by the local value at the same place."""
    if isinstance(remote, dict):
        return {
            k: _unmask(v, local.get(k) if isinstance(local, dict) else None, missing, f"{path}/{k}")
            for k, v in remote.items()
        }
    if isinstance(remote, list):
        return [
            _unmask(v, local[i] if isinstance(local, list) and i < len(local) else None, missing, f"{path}/{i}")
            for i, v in enumerate(remote)
        ]
    if isinstance(remote, str) and MASK in remote:
        if isinstance(local, str) and MASK not in local:
            return local
        missing.append(path or "/")
    return remote


# ── prepare (local → portable) ───────────────────────────────────────────────────────────────
MachineValue = tuple[Path_, Any]


@dataclass
class Prepared:
    """A local file in its portable form.

    ``data`` is what goes into the repo. ``machine`` are the machine values it still carries (they
    are recorded in the machine overlay). ``notes`` are user-facing remarks (``(level, text)``).
    """

    data: bytes
    machine: list[MachineValue] = field(default_factory=list)
    notes: list[tuple[str, str]] = field(default_factory=list)


def prepare(spec: FileSpec, local: bytes) -> Prepared:
    home = _home()
    if spec.fmt == "tmux":
        from .tmux import split_user_and_managed

        try:
            text = local.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PayloadError(f"{spec.label} is not UTF-8 text", fix="fix the file, then retry") from exc
        user, _managed = split_user_and_managed(text.replace("\r\n", "\n"))
        return Prepared(_tokenise_text(user, home).encode("utf-8"))

    doc = _parse(spec, local, spec.label)
    original = copy.deepcopy(doc)
    notes: list[tuple[str, str]] = []
    if spec.fmt == "rigconfig":
        env = doc.get("env")
        if isinstance(env, dict) and "vars" in env:
            names = sorted(str(k) for k in (env["vars"] or {}))
            del env["vars"]
            if not env:
                del doc["env"]
            if names:
                notes.append(("info", f"env.vars ({', '.join(names)}) stay on this machine — never pushed"))
    if spec.fmt == "mcp":
        doc = _mask_json(doc)
    doc = _map_strings(doc, lambda s: _tokenise_text(s, home))
    paths = machine_paths(doc, spec)
    machine = [(path, copy.deepcopy(_get(doc, path))) for path, _key in paths]
    if spec.fmt in ("rigconfig", "yaml") and doc == original:
        data = _lf(local)
    else:
        data = _dump(spec, doc)
    return Prepared(data, machine, notes)


# ── adopt (portable → local) ─────────────────────────────────────────────────────────────────
@dataclass
class Adopted:
    data: bytes
    notes: list[tuple[str, str]] = field(default_factory=list)


def adopt(spec: FileSpec, remote: bytes, local: bytes | None, record: list[MachineValue]) -> Adopted:
    """The local file that results from taking ``remote``: local-only parts and machine values kept."""
    home = _home()
    if spec.fmt == "tmux":
        from .tmux import split_user_and_managed

        try:
            user = _detokenise_text(remote.decode("utf-8"), home)
        except UnicodeDecodeError as exc:
            raise PayloadError(f"remote {spec.label} is not UTF-8 text", fix="fix the file in the profile repo") from exc
        managed = ""
        if local is not None:
            managed = split_user_and_managed(local.decode("utf-8", errors="replace").replace("\r\n", "\n"))[1]
        if not managed:
            return Adopted(user.encode("utf-8"))
        return Adopted((user.rstrip("\n") + "\n" + managed if user.strip() else managed).encode("utf-8"))

    remote_doc = _parse(spec, remote, f"remote {spec.label}")
    result = copy.deepcopy(remote_doc)
    notes: list[tuple[str, str]] = []

    local_doc: dict[str, Any] = {}
    if local is not None:
        local_doc = _map_strings(_parse(spec, local, f"local {spec.label}"), lambda s: _tokenise_text(s, home))
    local_values = {path: _get(local_doc, path) for path, _key in machine_paths(local_doc, spec)}
    record_values = dict(record)
    remote_paths = machine_paths(result, spec)
    for path, key in remote_paths:
        if path in local_values:
            _set(result, path, copy.deepcopy(local_values[path]))
        elif path in record_values:
            _set(result, path, copy.deepcopy(record_values[path]))
        elif key is not None and key.drop_when_unknown:
            _del(result, path)
    known = {path for path, _key in remote_paths}
    for path, value in {**record_values, **local_values}.items():
        if path not in known:
            _set(result, path, copy.deepcopy(value))

    if spec.fmt == "rigconfig":
        local_env = local_doc.get("env")
        if isinstance(local_env, dict) and local_env.get("vars"):
            env = result.setdefault("env", {})
            if isinstance(env, dict):
                env["vars"] = copy.deepcopy(local_env["vars"])
    if spec.fmt == "mcp":
        missing: list[str] = []
        result = _unmask(result, local_doc, missing)
        if missing:
            notes.append(("warn", f"{spec.label}: fill in the masked value(s) on this machine: "
                                  + ", ".join(sorted(missing))))

    if result == remote_doc:
        text = _detokenise_text(_lf(remote).decode("utf-8"), home)
        return Adopted(text.encode("utf-8"), notes)
    return Adopted(_dump(spec, _map_strings(result, lambda s: _detokenise_text(s, home))), notes)


# ── the machine overlay record ───────────────────────────────────────────────────────────────
def record_data(machine: str, values: dict[str, list[MachineValue]]) -> bytes | None:
    """``machine/<host>.yaml`` for the files that carry machine values, or None when none do."""
    entries = {fid: vals for fid, vals in values.items() if vals}
    if not entries:
        return None
    import yaml

    doc = {
        "host": machine,
        "files": {
            fid: [{"path": list(path), "value": value} for path, value in vals] for fid, vals in entries.items()
        },
    }
    return yaml.safe_dump(doc, sort_keys=True, allow_unicode=True, default_flow_style=False).encode("utf-8")


def parse_record(data: bytes | None) -> dict[str, list[MachineValue]]:
    if data is None:
        return {}
    import yaml

    try:
        doc = yaml.safe_load(data.decode("utf-8")) or {}
    except (UnicodeDecodeError, yaml.YAMLError):
        return {}
    files = doc.get("files") if isinstance(doc, dict) else None
    out: dict[str, list[MachineValue]] = {}
    for fid, entries in (files.items() if isinstance(files, dict) else []):
        values: list[MachineValue] = []
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict) and isinstance(entry.get("path"), list):
                values.append((tuple(str(p) for p in entry["path"]), entry.get("value")))
        out[str(fid)] = values
    return out
