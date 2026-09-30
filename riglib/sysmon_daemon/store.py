"""On-disk state of the monitor: heartbeat/status, persisted alert+heal state, incidents.

Layout under ``state_dir`` (default ``~/.config/rig/sysmon/state``)::

    status.json                 heartbeat + the latest sample and findings (``rig sysmon status``)
    memory.json                 alert dedup, heal progress, remediation cooldown (survives restarts)
    sessions.json               the latest live-session snapshot (restore after a crash)
    incidents/<id>/incident.json
                                one incident (schema below)

``incident.json`` keys: ``id``; ``kind`` (``live`` | ``post-crash`` | ``simulated``);
``opened_at`` / ``updated_at`` (epoch); ``severity``; ``findings`` (``model.Finding`` as JSON);
``sample`` (``probe.Sample.as_dict()``); ``sessions`` (``sessions.SessionRecord`` dicts active at
incident time); ``crash`` (``sessions.detect_crash`` result or null); ``heal`` (heal step results);
``remediation`` (spawn result or null); ``restore`` (restore step results); ``alerts`` (sent).

Every write is atomic (temp file + ``os.replace``) so a crash mid-write never leaves a
truncated JSON file that the next start would have to guess about.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

STATUS = "status.json"
MEMORY = "memory.json"
SESSIONS = "sessions.json"
INCIDENTS = "incidents"
INCIDENT_FILE = "incident.json"


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True, default=str)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def new_incident_id(now: Optional[float] = None, suffix: str = "") -> str:
    stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(time.time() if now is None else now))
    return f"{stamp}-{suffix}" if suffix else stamp


def incident_dir(state_dir: Path, incident_id: str) -> Path:
    return state_dir / INCIDENTS / incident_id


def save_incident(state_dir: Path, incident: Dict[str, Any]) -> Path:
    path = incident_dir(state_dir, str(incident["id"])) / INCIDENT_FILE
    write_json(path, incident)
    return path


def load_incident(state_dir: Path, incident_id: str) -> Optional[Dict[str, Any]]:
    data = read_json(incident_dir(state_dir, incident_id) / INCIDENT_FILE)
    return data if isinstance(data, dict) else None


def list_incidents(state_dir: Path) -> List[Dict[str, Any]]:
    """Every incident, oldest first."""
    root = state_dir / INCIDENTS
    if not root.is_dir():
        return []
    out = []
    for child in sorted(root.iterdir()):
        data = read_json(child / INCIDENT_FILE)
        if isinstance(data, dict):
            out.append(data)
    return out
