"""Self-contained HTML for a gathered completion report.

Accessed via: ``rig report`` → :func:`render_html`. The original HyperIDE
template needed ``reports/shared/{styles.css,components.js}`` from
hyperide.github.io — that is why a ported stub could not produce a page you
can open. This renderer inlines CSS and HTML-escapes every git-derived
string so a commit message containing ``</script>`` cannot break out of the
page.

Assumptions: callers pass already-validated ``task_id`` / ``title`` (the
command layer rejects path-shaped ids). This module still escapes them.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone

from .gather import Gathered


def render_html(*, task_id: str, title: str, gathered: Gathered) -> str:
    esc = html.escape
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    commits_html = (
        "\n".join(
            (
                "<tr>"
                f"<td><code>{esc(c.sha[:8])}</code></td>"
                f"<td>{esc(c.message)}</td>"
                f"<td>{esc(c.date)}</td>"
                "</tr>"
            )
            for c in gathered.commits
        )
        or '<tr><td colspan="3">No commits since the merge-base.</td></tr>'
    )
    files_html = (
        "\n".join(
            (
                "<tr>"
                f"<td><code>{esc(f.path)}</code></td>"
                f"<td>{esc(f.status)}</td>"
                f"<td>+{f.added} / −{f.deleted}</td>"
                "</tr>"
            )
            for f in gathered.files
        )
        or '<tr><td colspan="3">No file changes since the merge-base.</td></tr>'
    )
    repo_line = ""
    if gathered.repo_url.startswith("https://") or gathered.repo_url.startswith(
        "http://"
    ):
        repo_line = (
            f'<p class="meta"><a href="{esc(gathered.repo_url, quote=True)}">'
            f"{esc(gathered.repo_url)}</a></p>"
        )
    elif gathered.repo_url:
        repo_line = f'<p class="meta">{esc(gathered.repo_url)}</p>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{esc(task_id)} — {esc(title)}</title>
  <style>
    :root {{ color-scheme: dark; }}
    body {{
      margin: 0; font-family: ui-sans-serif, system-ui, sans-serif;
      background: #0d1117; color: #e6edf3; line-height: 1.5;
    }}
    main {{ max-width: 880px; margin: 0 auto; padding: 32px 20px 64px; }}
    h1 {{ font-size: 1.6rem; margin: 0 0 8px; }}
    .meta {{ color: #8b949e; margin: 4px 0; }}
    .stats {{ display: flex; gap: 12px; flex-wrap: wrap; margin: 24px 0; }}
    .stat {{
      background: #161b22; border: 1px solid #30363d; border-radius: 8px;
      padding: 12px 16px; min-width: 120px;
    }}
    .stat b {{ display: block; font-size: 1.3rem; }}
    table {{ width: 100%; border-collapse: collapse; margin: 12px 0 28px; }}
    th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #30363d; }}
    th {{ color: #8b949e; font-weight: 600; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.9em; }}
    a {{ color: #58a6ff; }}
    footer {{ color: #8b949e; font-size: 0.85rem; margin-top: 32px; }}
  </style>
</head>
<body>
<main>
  <h1>{esc(task_id)} — {esc(title)}</h1>
  <p class="meta">branch <code>{esc(gathered.branch)}</code> · base <code>{esc(gathered.base_ref)}</code> · {esc(date)}</p>
  {repo_line}
  <div class="stats">
    <div class="stat"><b>{len(gathered.commits)}</b> commits</div>
    <div class="stat"><b>{len(gathered.files)}</b> files</div>
    <div class="stat"><b>+{gathered.insertions}</b> insertions</div>
    <div class="stat"><b>−{gathered.deletions}</b> deletions</div>
  </div>
  <h2>Commits</h2>
  <table>
    <thead><tr><th>SHA</th><th>Message</th><th>Date</th></tr></thead>
    <tbody>
      {commits_html}
    </tbody>
  </table>
  <h2>Files</h2>
  <table>
    <thead><tr><th>Path</th><th>Status</th><th>Diff</th></tr></thead>
    <tbody>
      {files_html}
    </tbody>
  </table>
  <footer>Generated {esc(date)} by <code>rig report</code> (HyperIDE /result-report gather).</footer>
</main>
</body>
</html>
"""


def render_text(*, task_id: str, title: str, gathered: Gathered) -> str:
    """Terminal summary printed alongside the HTML path."""
    lines = [
        f"{task_id} — {title}",
        f"{len(gathered.commits)} commit(s), {len(gathered.files)} file(s), "
        f"+{gathered.insertions} / −{gathered.deletions}",
        f"branch {gathered.branch}  base {gathered.base_ref}",
        "",
    ]
    for c in gathered.commits:
        lines.append(f"  {c.sha[:8]}  {c.message}")
    if gathered.files:
        lines.append("")
        for f in gathered.files:
            lines.append(f"  {f.path}  {f.status}  +{f.added} / −{f.deleted}")
    return "\n".join(lines).rstrip() + "\n"
