"""render_init_script / render_landing_entry — the JS/HTML the result-report skill's
report.html and index.html expect (SKILL.md "REPORT_DATA schema" and "Step 5 — Update
the landing page")."""

from __future__ import annotations

import json
import re

import pytest

from riglib.report.model import Badge, ReportData
from riglib.report.render import render_init_script, render_landing_entry


def _minimal_report_data() -> ReportData:
    return ReportData(
        repo="https://github.com/o/r",
        head_sha="a" * 40,
        title="Task Title",
        subtitle="Short description",
        date="2026-09-07",
        badge=Badge(label="feat", type="feat"),
        branch="feature-branch",
    )


def test_render_init_script_is_valid_json_payload():
    script = render_init_script(_minimal_report_data())
    assert script.startswith("Report.init(")
    assert script.rstrip().endswith(");")
    payload_text = script[len("Report.init(") : script.rfind(");")]
    payload = json.loads(payload_text)
    assert payload["title"] == "Task Title"


def test_render_init_script_renames_snake_case_fields_to_the_schema_names():
    script = render_init_script(_minimal_report_data())
    assert "headSha" in script
    assert "head_sha" not in script
    assert "productChanges" in script
    assert "product_changes" not in script


def test_render_init_script_defaults_to_reviewRounds_field_name():
    script = render_init_script(_minimal_report_data())
    assert '"reviewRounds"' in script


def test_render_init_script_supports_the_codexRounds_field_name_switch():
    # HyperIDE's live template names this field `codexRounds` for historical reasons
    # (SKILL.md footnote) — components.js reads whichever name it was built against.
    script = render_init_script(_minimal_report_data(), review_rounds_field="codexRounds")
    assert '"codexRounds"' in script
    assert '"reviewRounds"' not in script


def test_render_landing_entry_matches_the_report_card_shape():
    entry = render_landing_entry(
        _minimal_report_data(), "HYP-1", files_changed=3, insertions=10, deletions=2
    )
    assert '<li class="report-card">' in entry
    assert 'href="reports/HYP-1/"' in entry
    assert "HYP-1: Task Title" in entry
    assert re.search(r"\+10 / .2", entry)  # en-dash/minus rendering of "-2"


def test_render_init_script_neutralizes_a_closing_script_tag_in_report_text():
    # A commit message / ticket title is attacker-influenced content; a literal
    # </script> inside the JSON payload must not close the surrounding <script>
    # block when this is embedded verbatim in report.html (review-cli finding).
    data = _minimal_report_data()
    data.motivation = "why</script><script>alert(1)</script>"
    script = render_init_script(data)
    assert "</script>" not in script
    assert "<script>" not in script
    payload_text = script[len("Report.init(") : script.rfind(");")]
    payload = json.loads(payload_text.encode().decode("unicode_escape"))
    assert payload["motivation"] == "why</script><script>alert(1)</script>"


def test_render_landing_entry_escapes_html_in_task_id_and_title():
    # A hostile task_id must not break out of the href attribute or inject markup
    # into the surrounding HTML (review-cli finding: attribute injection / stored XSS).
    data = _minimal_report_data()
    data.title = '<img src=x onerror=alert(1)>'
    entry = render_landing_entry(
        data, 'x/"><img src=x onerror=alert(1)>', files_changed=1, insertions=1, deletions=1
    )
    assert "<img" not in entry  # no live tag: the "<" survives only as "&lt;"
    assert "&lt;img" in entry  # the title's markup is inert text, not a rendered tag
    assert '"><img' not in entry  # the quote-breakout that would end the attribute early
    href = re.search(r'href="([^"]*)"', entry).group(1)
    assert '"' not in href and "<" not in href and ">" not in href


def test_render_landing_entry_url_encodes_a_hash_in_the_task_id():
    # `rig-cli#123`-style IDs (see riglib/report/command.py's task_id help text)
    # contain a literal `#`; left bare in an href it is a URL FRAGMENT separator, so
    # `href="reports/rig-cli#123/"` requests `reports/rig-cli`, not
    # `reports/rig-cli#123/index.html` (review-cli finding).
    entry = render_landing_entry(
        _minimal_report_data(), "rig-cli#123", files_changed=1, insertions=1, deletions=0
    )
    href = re.search(r'href="([^"]*)"', entry).group(1)
    assert "#" not in href
    assert href == "reports/rig-cli%23123/"
    # the visible text still shows the human-readable, HTML-escaped task id
    assert "rig-cli#123: Task Title" in entry


@pytest.mark.parametrize("bad_kwargs", [
    {"files_changed": "1</span><script>alert(1)</script><span>"},
    {"insertions": "1<script>"},
    {"deletions": 1.5},
    {"files_changed": True},  # bool is an int subclass in Python; must still be rejected
])
def test_render_landing_entry_rejects_non_int_counts(bad_kwargs):
    # Counts are f-string interpolated without HTML-escaping (they're supposed to be
    # plain numbers) — a caller passing a string can otherwise smuggle markup through
    # them (review-cli finding).
    kwargs = {"files_changed": 1, "insertions": 1, "deletions": 1}
    kwargs.update(bad_kwargs)
    with pytest.raises(TypeError):
        render_landing_entry(_minimal_report_data(), "HYP-1", **kwargs)


@pytest.mark.parametrize("dot_task_id", [".", ".."])
def test_render_landing_entry_rejects_dot_segment_task_ids(dot_task_id):
    # `urllib.parse.quote()` deliberately leaves "." unescaped, so a task_id of "."
    # or ".." would otherwise render href="reports/../" — a dot-segment a browser
    # normalizes up to the site root, not into any report directory (review-cli
    # finding).
    with pytest.raises(ValueError):
        render_landing_entry(
            _minimal_report_data(), dot_task_id, files_changed=1, insertions=1, deletions=0
        )


@pytest.mark.parametrize("blank_task_id", ["", "   "])
def test_render_landing_entry_rejects_blank_task_ids(blank_task_id):
    # An empty/whitespace task_id would render href="reports//", targeting the
    # reports landing page (reports/index.html) instead of an isolated report
    # (review-cli finding).
    with pytest.raises(ValueError):
        render_landing_entry(
            _minimal_report_data(), blank_task_id, files_changed=1, insertions=1, deletions=0
        )


def test_render_init_script_rejects_an_unknown_review_rounds_field():
    with pytest.raises(ValueError):
        render_init_script(_minimal_report_data(), review_rounds_field="files")
