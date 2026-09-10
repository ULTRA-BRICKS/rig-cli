"""omp ``providers.webSearchExclude`` provisioning — the one owner of the default.

Accessed via: ``rig apply`` when ``harness`` is enabled and ``omp`` is a configured
kind → :func:`riglib.plan._build_omp_web_search` emits ``provision_omp_web_search``
→ the runner additively merges the exclude list into ``~/.omp/agent/config.yml``
(honoring ``PI_CODING_AGENT_DIR`` / ``PI_CONFIG_DIR``). Drift shares
:func:`exclude_lookup` so apply and status cannot disagree.

Assumptions: omp's web_search tool walks providers in order and never uses a name
listed in ``providers.webSearchExclude``, even as a fallback. Perplexity's
anonymous/cookie path returns a Sign-up wall without an account, so the baked
default excludes it. An explicit ``harness.web_search.exclude: []`` (or
``enabled: false``) is the opt-out; extras already on disk are never removed.

Past bugs: a one-off ``omp config set providers.webSearchExclude perplexity``
wrote the key into config.yml but was not reconcilable — the permissions receipt
only tracks ``tools.approvalMode``.
"""
from __future__ import annotations

from typing import Any

# omp's web_search walks this list and never uses a named provider, even as a
# fallback. Perplexity's anonymous transport returns a Sign-up wall without an
# account; excluding it is the durable default (rig-cli#447).
DEFAULT_WEB_SEARCH_EXCLUDE: tuple[str, ...] = ("perplexity",)
EXCLUDE_KEY: tuple[str, ...] = ("providers", "webSearchExclude")


def resolve_exclude(harness: dict[str, Any] | None) -> list[str]:
    """The exclude list this harness block wants written, or ``[]`` to skip.

    Absent ``web_search`` / absent ``exclude`` → the baked default. Explicit
    ``enabled: false`` or ``exclude: []`` → empty (no provision action).
    """
    if not isinstance(harness, dict):
        return []
    ws = harness.get("web_search")
    if ws is None:
        return list(DEFAULT_WEB_SEARCH_EXCLUDE)
    if not isinstance(ws, dict) or ws.get("enabled") is False:
        return []
    if "exclude" not in ws:
        return list(DEFAULT_WEB_SEARCH_EXCLUDE)
    raw = ws.get("exclude")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str) and item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def exclude_lookup(data: dict[str, Any], key_path: tuple[str, ...] = EXCLUDE_KEY) -> tuple[str, object]:
    """State of the exclude list on disk: ``missing`` / ``present`` / ``shape``.

    ``present`` returns the list (possibly empty). ``shape`` returns the non-dict
    parent or non-list value. Mirrors :func:`riglib.actions.runner.approval_lookup`
    so apply and drift share one walk.
    """
    cur: object = data
    for i, seg in enumerate(key_path):
        if not isinstance(cur, dict):
            return "shape", cur
        if seg not in cur or cur[seg] is None:
            return "missing", None
        if i == len(key_path) - 1:
            val = cur[seg]
            if not isinstance(val, list):
                return "shape", val
            return "present", val
        cur = cur[seg]
    return "missing", None


def missing_exclude_entries(data: dict[str, Any], desired: list[str]) -> tuple[str, list[str]]:
    """``(state, missing_names)``. ``state`` is ``ok`` / ``missing`` / ``shape``."""
    state, value = exclude_lookup(data)
    if state == "shape":
        return "shape", []
    if state == "missing":
        return "missing", list(desired)
    present = {item for item in value if isinstance(item, str)} if isinstance(value, list) else set()
    missing = [name for name in desired if name not in present]
    return ("ok" if not missing else "missing"), missing


def ensure_exclude(data: dict[str, Any], desired: list[str]) -> list[str]:
    """Additively append ``desired`` names that are not already in the list. Returns added."""
    added: list[str] = []
    cur: dict[str, Any] = data
    for seg in EXCLUDE_KEY[:-1]:
        nxt = cur.get(seg)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[seg] = nxt
        cur = nxt
    leaf = EXCLUDE_KEY[-1]
    existing = cur.get(leaf)
    if not isinstance(existing, list):
        existing = []
        cur[leaf] = existing
    have = {item for item in existing if isinstance(item, str)}
    for name in desired:
        if name not in have:
            existing.append(name)
            have.add(name)
            added.append(name)
    return added
