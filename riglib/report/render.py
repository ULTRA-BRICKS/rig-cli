"""Renders a :class:`riglib.report.model.ReportData` into the ``Report.init({...})``
JS call the ``result-report`` skill's ``report.html`` template expects, plus the
landing-page ``<li class="report-card">`` snippet for a reports repo's ``index.html``.

The actual renderer is ``components.js`` in the TARGET reports repo (not part of this
codebase) -- this module only serializes :class:`~riglib.report.model.ReportData` into
the JS object literal ``Report.init()`` consumes. It does not write files; callers own
I/O (``rig report new`` / ``rig report periodic``, not yet implemented -- see
``riglib/report/command.py``).
"""

from __future__ import annotations

import html
import json
import urllib.parse
from dataclasses import asdict
from typing import Any

from .model import ReportData

# HyperIDE's live template names the review-rounds field `codexRounds` for historical
# reasons (skill doc, "REPORT_DATA schema" footnote): "check the `components.js` you're
# rendering against and match its actual field name, since the renderer reads
# `data.codexRounds` / `data.reviewRounds` literally." Callers targeting a repo whose
# components.js hasn't been renamed must pass review_rounds_field="codexRounds".
DEFAULT_REVIEW_ROUNDS_FIELD = "reviewRounds"

# The only two field names any known components.js build reads (skill doc footnote).
# Anything else either collides with an existing REPORT_DATA key (corrupting the
# payload) or is simply never read by the renderer -- reject it outright rather than
# accept an arbitrary caller-supplied key (review-cli finding, rig-report-scaffold).
_ALLOWED_REVIEW_ROUNDS_FIELDS = frozenset({"reviewRounds", "codexRounds"})


def _to_report_data_dict(data: ReportData, *, review_rounds_field: str) -> dict[str, Any]:
    """dataclasses.asdict(), then rename the handful of keys that differ between
    Python's snake_case convention and the REPORT_DATA schema's camelCase / reserved-word
    dodges (`head_sha` -> `headSha`, `del_` -> `del`, `product_changes` ->
    `productChanges`, `review_rounds` -> the caller-selected field name)."""
    if review_rounds_field not in _ALLOWED_REVIEW_ROUNDS_FIELDS:
        raise ValueError(
            f"review_rounds_field must be one of {sorted(_ALLOWED_REVIEW_ROUNDS_FIELDS)}, "
            f"got {review_rounds_field!r}"
        )
    d = asdict(data)
    d["headSha"] = d.pop("head_sha")
    d["productChanges"] = d.pop("product_changes")
    d[review_rounds_field] = d.pop("review_rounds")
    for file_stat in d["files"]:
        file_stat["del"] = file_stat.pop("del_")
    return d


def _json_for_inline_script(payload: dict[str, Any]) -> str:
    """``json.dumps`` a payload for embedding inside an HTML ``<script>`` block.

    A JSON object literal is valid JS, but plain ``json.dumps`` is NOT safe to embed
    inline: report text (a commit message, a ticket title, a finding description) is
    attacker-influenced content, and a literal ``</script>`` inside a JSON string
    closes the surrounding ``<script>`` tag early, letting an attacker inject and run
    arbitrary markup/JS after it (review-cli finding, rig-report-scaffold). Escaping
    ``<``, ``>`` and ``&`` as unicode escapes -- the standard OWASP approach for JSON
    embedded in HTML -- neutralizes `</script>`, HTML comments (`<!--`), and stray
    `&`-entities without touching the JSON's meaning (`\\u003c` etc. decode back to
    the original characters at JSON-parse time)."""
    return (
        json.dumps(payload, indent=2)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def render_init_script(
    data: ReportData, *, review_rounds_field: str = DEFAULT_REVIEW_ROUNDS_FIELD
) -> str:
    """Render the ``Report.init({...});`` call body, safe to embed inline inside a
    ``<script>`` block (see :func:`_json_for_inline_script`)."""
    payload = _to_report_data_dict(data, review_rounds_field=review_rounds_field)
    return f"Report.init({_json_for_inline_script(payload)});\n"


def render_landing_entry(
    data: ReportData,
    task_id: str,
    *,
    files_changed: int,
    insertions: int,
    deletions: int,
) -> str:
    """Render the ``<li class="report-card">`` snippet for a reports repo's
    ``index.html``, to be inserted at the TOP of the list between the
    ``<!-- REPORTS_START -->`` / ``<!-- REPORTS_END -->`` markers (skill doc Step 5).

    Every interpolated value is attacker-influenced (task_id/title come from a ticket
    tracker or a branch name; nothing here is a trusted literal) -- HTML-escape every
    visible text value (including inside the `href` attribute, to prevent attribute
    breakout / stored XSS) and additionally URL-encode `task_id` for the `href` path
    segment itself, since `rig-cli#123`-style task IDs (see riglib/report/command.py's
    `task_id` help text) contain a literal `#`, which a bare HTML-escape leaves intact
    as a URL FRAGMENT separator -- turning `href="reports/rig-cli#123/"` into a request
    for `reports/rig-cli` (browser drops everything from `#` on) rather than the
    intended `reports/rig-cli#123/index.html`. `files_changed`/`insertions`/`deletions`
    are validated as real `int`s (not just type-hinted) so a caller passing a string
    can't smuggle markup through them. `task_id` is rejected outright if it is empty,
    whitespace-only, or a dot-segment (`.`/`..`): an empty/blank id would render
    `href="reports//"`, targeting the reports landing page (`reports/index.html`)
    instead of an isolated report; `urllib.parse.quote()` deliberately leaves `.`
    unescaped, so `.`/`..` would render `href="reports/../"`, a dot-segment a browser
    normalizes up to the site root (review-cli findings, rig-report-scaffold)."""
    for name, value in (
        ("files_changed", files_changed),
        ("insertions", insertions),
        ("deletions", deletions),
    ):
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError(f"{name} must be an int, got {type(value).__name__}")
    if not task_id.strip():
        raise ValueError("task_id must not be empty or whitespace-only")
    if task_id in (".", ".."):
        raise ValueError(f"task_id must not be a dot-segment, got {task_id!r}")
    safe_task_id = html.escape(task_id, quote=True)
    safe_task_id_href = urllib.parse.quote(task_id, safe="")
    safe_title = html.escape(data.title, quote=True)
    safe_date = html.escape(data.date, quote=True)
    safe_badge_type = html.escape(data.badge.type, quote=True)
    return (
        '<li class="report-card">\n'
        f'  <a href="reports/{safe_task_id_href}/">\n'
        f'    <div class="report-title">{safe_task_id}: {safe_title}</div>\n'
        '    <div class="report-meta">\n'
        f"      <span>{safe_date}</span>\n"
        f'      <span class="badge">{safe_badge_type}</span>\n'
        f"      <span>{files_changed} files changed</span>\n"
        f"      <span>+{insertions} / −{deletions}</span>\n"
        "    </div>\n"
        "  </a>\n"
        "</li>"
    )
